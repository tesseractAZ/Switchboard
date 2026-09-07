"""Pure WebSocket (RFC 6455) + telnet-client helpers for the web terminal.

Everything here is socket-free and side-effect-free so it can be unit-tested
with plain `python3` (see tests/test_console_web.py), mirroring how the operator
console keeps its telnet parser (`console.parse_input`) pure. The socket
plumbing lives in `server.py`.

Frame encode/decode is the server half of RFC 6455: we never mask frames we
send (server->client is unmasked) and we always unmask frames we receive
(client->server is always masked). Telnet helpers let us act as a *client* to
the operator console at 127.0.0.1:2300 — answering its IAC negotiation and
emitting a NAWS subnegotiation on resize — so none of its IAC bytes leak to the
browser.
"""

from __future__ import annotations

import base64
import hashlib
import struct

# RFC 6455 magic GUID appended to Sec-WebSocket-Key before the SHA-1/Base64.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes we care about.
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# Telnet protocol bytes (we are the CLIENT side here).
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA, OPT_NAWS = 1, 3, 31


def accept_key(sec_websocket_key: str) -> str:
    """Compute the Sec-WebSocket-Accept header value for a client key."""
    digest = hashlib.sha1((sec_websocket_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_http_headers(raw: bytes):
    """Parse an HTTP request head into (method, path, headers_dict).

    headers keys are lower-cased. Returns (None, None, {}) if the request line
    is missing. Only the head (up to the first blank line) need be supplied.
    """
    text = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1", "replace")
    lines = text.split("\r\n")
    if not lines or not lines[0]:
        return None, None, {}
    parts = lines[0].split(" ")
    method = parts[0] if parts else ""
    path = parts[1] if len(parts) > 1 else ""
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return method, path, headers


def is_websocket_upgrade(headers: dict) -> bool:
    """True iff the request headers ask for a WebSocket upgrade."""
    return (
        headers.get("upgrade", "").lower() == "websocket"
        and "upgrade" in headers.get("connection", "").lower()
        and bool(headers.get("sec-websocket-key"))
    )


def origin_allowed(headers: dict, extra_allowed=()) -> bool:
    """Reject a cross-origin WebSocket upgrade (drive-by / CSWSH protection).

    This is a *browser-reachable, unauthenticated call-control* surface, so a
    malicious LAN web page must not be able to cross-origin-connect and drive the
    console. Browsers always send Origin; the HA panel_iframe loads the terminal
    page from this same host, so its Origin authority equals the Host header and
    is allowed. A page served from any other origin is rejected. Non-browser
    clients (CLI, tests) send no Origin and are allowed — the same trust as the
    raw telnet console they front. Extra origins can be whitelisted explicitly.
    """
    origin = headers.get("origin", "").strip()
    if not origin:
        return True  # non-browser client; browsers always set Origin
    authority = origin.split("://", 1)[-1].split("/", 1)[0].lower()
    host = headers.get("host", "").strip().lower()
    if authority and authority == host:
        return True
    return authority in {a.strip().lower() for a in extra_allowed if a.strip()}


def handshake_response(sec_websocket_key: str) -> bytes:
    """The full 101 Switching Protocols response for a valid client key."""
    accept = accept_key(sec_websocket_key)
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode("ascii")


def encode_frame(payload: bytes, opcode: int = OP_BIN) -> bytes:
    """Encode a single unmasked server->client frame (FIN set, no fragmentation)."""
    n = len(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


def decode_frames(buf: bytes):
    """Decode as many complete client->server frames as `buf` holds.

    Returns (frames, rest) where frames is a list of (opcode, payload) and rest
    is the trailing bytes of an incomplete frame to prepend to the next read.
    Client frames MUST be masked (RFC 6455 §5.1); an unmasked client frame is a
    protocol violation, surfaced as a ("error",) frame so the caller can close.
    """
    frames = []
    i = 0
    n = len(buf)
    while True:
        if n - i < 2:
            break
        b0 = buf[i]
        b1 = buf[i + 1]
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        j = i + 2
        if length == 126:
            if n - j < 2:
                break
            length = struct.unpack(">H", buf[j:j + 2])[0]
            j += 2
        elif length == 127:
            if n - j < 8:
                break
            length = struct.unpack(">Q", buf[j:j + 8])[0]
            j += 8
        if not masked:
            # Reject an unmasked client frame outright; don't try to recover.
            frames.append(("error", b""))
            return frames, b""
        if n - j < 4:
            break
        mask = buf[j:j + 4]
        j += 4
        if n - j < length:
            break
        raw = bytearray(buf[j:j + length])
        for k in range(length):
            raw[k] ^= mask[k & 3]
        j += length
        # We don't reassemble fragments here: the browser sends keystrokes as
        # whole text frames, so a non-FIN data frame is unexpected but harmless
        # to deliver as-is.
        frames.append((opcode, bytes(raw)))
        i = j
        if not fin:
            continue
    return frames, buf[i:]


def naws_subnegotiation(cols: int, rows: int) -> bytes:
    """Telnet NAWS (RFC 1073) subnegotiation: IAC SB NAWS w w h h IAC SE.

    A width/height byte equal to 255 would collide with IAC and must be doubled
    per the telnet spec; we clamp to 1..200 (well under 255) so it never can.
    """
    cols = max(1, min(200, int(cols)))
    rows = max(1, min(200, int(rows)))
    return bytes([
        IAC, SB, OPT_NAWS,
        (cols >> 8) & 0xFF, cols & 0xFF,
        (rows >> 8) & 0xFF, rows & 0xFF,
        IAC, SE,
    ])


def strip_telnet(buf: bytes):
    """Strip telnet IAC negotiation from a console->client byte stream.

    The operator console (the telnet *server*) sends IAC WILL/DO/SB sequences
    at connect and may send more later. We answer none of them inline here
    (the caller sends a fixed acceptance up front); we just remove every IAC
    command so only clean ANSI reaches the browser. Returns (clean, rest) where
    rest holds an incomplete trailing IAC sequence for the next chunk. A literal
    0xFF in the data stream is encoded by the server as IAC IAC and decoded back
    to a single 0xFF.
    """
    out = bytearray()
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        # b == IAC
        if i + 1 >= n:
            break  # incomplete: hold the lone IAC
        cmd = buf[i + 1]
        if cmd == IAC:  # escaped literal 0xFF
            out.append(IAC)
            i += 2
            continue
        if cmd == SB:
            # Skip to IAC SE.
            j = i + 2
            se = -1
            incomplete = False
            while j < n:
                if buf[j] == IAC:
                    if j + 1 >= n:
                        incomplete = True
                        break
                    if buf[j + 1] == SE:
                        se = j
                        break
                    j += 2
                    continue
                j += 1
            if incomplete or se < 0:
                break  # incomplete subnegotiation: hold it
            i = se + 2
            continue
        if WILL <= cmd <= DONT:  # WILL/WONT/DO/DONT take one option byte
            if i + 2 >= n:
                break  # incomplete: hold IAC + cmd
            i += 3
            continue
        # Bare 2-byte command (NOP, etc.).
        i += 2
    return bytes(out), buf[i:]
