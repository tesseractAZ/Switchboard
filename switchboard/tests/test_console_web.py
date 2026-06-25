"""Behavioral tests for the console web terminal's pure framing helpers
(console-web/wsproto.py).

Run with plain Python (no deps):

    python3 switchboard/tests/test_console_web.py

Covers the side-effect-free pieces: the RFC 6455 handshake key, WebSocket frame
encode (server->client, unmasked) and decode (client->server, masked), the
telnet NAWS subnegotiation, and stripping the operator console's IAC negotiation
out of the console->browser stream. The socket plumbing in server.py is not
exercised here (same split as console.py's pure parser vs. its socket server).
"""
import os
import struct
from importlib.machinery import SourceFileLoader
from pathlib import Path

WSPROTO_PATH = (
    Path(__file__).resolve().parents[1]
    / "rootfs" / "usr" / "share" / "switchboard" / "console-web" / "wsproto.py"
)
ws = SourceFileLoader("switchboard_wsproto", str(WSPROTO_PATH)).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def _mask(payload: bytes, opcode: int = ws.OP_TEXT, mask=b"\x37\xfa\x21\x3d") -> bytes:
    """Build a masked client->server frame the way a browser would."""
    n = len(payload)
    header = bytearray([0x80 | opcode])
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return bytes(header) + masked


def test_accept_key() -> None:
    # The canonical example from RFC 6455 §1.3.
    check("accept_key: RFC 6455 example",
          ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


def test_parse_http_headers() -> None:
    raw = (
        b"GET /ws HTTP/1.1\r\nHost: 192.168.5.152:8100\r\n"
        b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
    )
    method, path, headers = ws.parse_http_headers(raw)
    check("headers: method", method == "GET")
    check("headers: path", path == "/ws")
    check("headers: lower-cased keys", headers.get("upgrade") == "websocket")
    check("headers: ws key preserved", headers.get("sec-websocket-key") == "dGhlIHNhbXBsZSBub25jZQ==")
    check("headers: is_websocket_upgrade true", ws.is_websocket_upgrade(headers) is True)
    # A plain GET is not an upgrade.
    _, _, h2 = ws.parse_http_headers(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    check("headers: plain GET not an upgrade", ws.is_websocket_upgrade(h2) is False)


def test_handshake_response() -> None:
    resp = ws.handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
    check("handshake: 101 status", resp.startswith(b"HTTP/1.1 101 "))
    check("handshake: accept header present",
          b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n" in resp)
    check("handshake: ends with blank line", resp.endswith(b"\r\n\r\n"))


def test_encode_frame() -> None:
    # Small payload: 1-byte length, FIN set, server frames are NEVER masked.
    f = ws.encode_frame(b"hello", ws.OP_BIN)
    check("encode: FIN + binary opcode", f[0] == 0x82)
    check("encode: unmasked length byte", f[1] == 5)
    check("encode: payload appended verbatim", f[2:] == b"hello")
    # 126..65535 uses the 2-byte extended length.
    big = ws.encode_frame(b"x" * 200, ws.OP_TEXT)
    check("encode: text opcode", big[0] == 0x81)
    check("encode: 126 marker", big[1] == 126)
    check("encode: 16-bit length", struct.unpack(">H", big[2:4])[0] == 200)
    # >65535 uses the 8-byte extended length.
    huge = ws.encode_frame(b"y" * 70000)
    check("encode: 127 marker", huge[1] == 127)
    check("encode: 64-bit length", struct.unpack(">Q", huge[2:10])[0] == 70000)


def test_decode_frames_roundtrip() -> None:
    frames, rest = ws.decode_frames(_mask(b"abc"))
    check("decode: single text frame unmasked", frames == [(ws.OP_TEXT, b"abc")] and rest == b"")
    # Two frames back-to-back in one buffer.
    frames, rest = ws.decode_frames(_mask(b"hi") + _mask(b"there"))
    check("decode: two frames", frames == [(ws.OP_TEXT, b"hi"), (ws.OP_TEXT, b"there")])
    check("decode: no leftover", rest == b"")
    # Extended 16-bit length round-trips.
    payload = b"z" * 300
    frames, _ = ws.decode_frames(_mask(payload))
    check("decode: 300-byte payload", frames == [(ws.OP_TEXT, payload)])


def test_decode_partial_held() -> None:
    full = _mask(b"keystrokes")
    # Feed all but the last 3 bytes: nothing complete yet, all held in rest.
    frames, rest = ws.decode_frames(full[:-3])
    check("decode: partial frame yields nothing", frames == [])
    check("decode: partial frame fully held in rest", rest == full[:-3])
    # Now append the tail: the frame completes.
    frames, rest = ws.decode_frames(rest + full[-3:])
    check("decode: completing the frame", frames == [(ws.OP_TEXT, b"keystrokes")] and rest == b"")


def test_decode_control_frames() -> None:
    frames, _ = ws.decode_frames(_mask(b"", ws.OP_CLOSE))
    check("decode: close opcode surfaced", frames == [(ws.OP_CLOSE, b"")])
    frames, _ = ws.decode_frames(_mask(b"pingdata", ws.OP_PING))
    check("decode: ping opcode + payload", frames == [(ws.OP_PING, b"pingdata")])


def test_decode_rejects_unmasked_client_frame() -> None:
    # A client frame with the mask bit clear is a protocol violation.
    unmasked = bytes([0x81, 0x03]) + b"abc"  # FIN+text, len 3, NO mask bit
    frames, rest = ws.decode_frames(unmasked)
    check("decode: unmasked client frame -> error", frames == [("error", b"")] and rest == b"")


def test_naws() -> None:
    sub = ws.naws_subnegotiation(120, 40)
    check("naws: framed IAC SB NAWS .. IAC SE",
          sub[0] == ws.IAC and sub[1] == ws.SB and sub[2] == ws.OPT_NAWS
          and sub[-2] == ws.IAC and sub[-1] == ws.SE)
    check("naws: width big-endian", sub[3] == 0 and sub[4] == 120)
    check("naws: height big-endian", sub[5] == 0 and sub[6] == 40)
    # Clamp keeps every size byte well under 255 (so it never collides with IAC).
    sub = ws.naws_subnegotiation(99999, 0)
    check("naws: clamps oversize width", sub[4] == 200)
    check("naws: clamps zero height to >=1", sub[6] == 1)


def test_strip_telnet_negotiation() -> None:
    # The console's connect-time negotiation, then a real ANSI payload.
    stream = (
        bytes([ws.IAC, ws.WILL, ws.OPT_ECHO, ws.IAC, ws.WILL, ws.OPT_SGA,
               ws.IAC, ws.DO, ws.OPT_SGA, ws.IAC, ws.DO, ws.OPT_NAWS])
        + b"\x1b[2JBOARD"
    )
    clean, rest = ws.strip_telnet(stream)
    check("strip: all IAC negotiation removed", clean == b"\x1b[2JBOARD" and rest == b"")


def test_strip_telnet_subnegotiation() -> None:
    stream = bytes([ws.IAC, ws.SB, ws.OPT_NAWS, 0, 80, 0, 24, ws.IAC, ws.SE]) + b"hi"
    clean, _ = ws.strip_telnet(stream)
    check("strip: IAC SB..SE removed", clean == b"hi")


def test_strip_telnet_escaped_ff() -> None:
    # IAC IAC is a literal 0xFF byte in the data and must survive as one 0xFF.
    stream = b"a" + bytes([ws.IAC, ws.IAC]) + b"b"
    clean, _ = ws.strip_telnet(stream)
    check("strip: IAC IAC -> single 0xFF", clean == b"a\xffb")


def test_strip_telnet_partial_held() -> None:
    # A lone trailing IAC (start of a command) is held for the next chunk.
    clean, rest = ws.strip_telnet(b"data" + bytes([ws.IAC]))
    check("strip: trailing lone IAC held", clean == b"data" and rest == bytes([ws.IAC]))
    # An IAC + cmd split across the boundary completes when the option arrives.
    clean, rest = ws.strip_telnet(b"x" + bytes([ws.IAC, ws.DO]))
    check("strip: IAC+cmd without option held", clean == b"x" and rest == bytes([ws.IAC, ws.DO]))
    clean2, rest2 = ws.strip_telnet(rest + bytes([ws.OPT_NAWS]) + b"y")
    check("strip: completes across boundary", clean2 == b"y" and rest2 == b"")


def test_origin_allowed() -> None:
    H = "homeassistant.local:8100"
    # Same-origin (the HA panel_iframe loads the page from this host) -> allowed.
    check("origin: same-origin allowed",
          ws.origin_allowed({"host": H, "origin": f"http://{H}"}) is True)
    # A drive-by page on another origin -> rejected (the key fix).
    check("origin: cross-origin rejected",
          ws.origin_allowed({"host": H, "origin": "http://evil.lan"}) is False)
    check("origin: cross-origin (other port) rejected",
          ws.origin_allowed({"host": H, "origin": "http://homeassistant.local:9999"}) is False)
    # Non-browser client (no Origin) -> allowed (same trust as the telnet console).
    check("origin: missing Origin allowed (CLI)", ws.origin_allowed({"host": H}) is True)
    # Explicit allowlist entry honored.
    check("origin: explicit allowlist entry",
          ws.origin_allowed({"host": H, "origin": "http://dash.lan"}, ["dash.lan"]) is True)
    # https origin same authority still matches.
    check("origin: https same-host allowed",
          ws.origin_allowed({"host": H, "origin": f"https://{H}"}) is True)


def main() -> None:
    test_accept_key()
    test_parse_http_headers()
    test_handshake_response()
    test_encode_frame()
    test_decode_frames_roundtrip()
    test_decode_partial_held()
    test_decode_control_frames()
    test_decode_rejects_unmasked_client_frame()
    test_naws()
    test_strip_telnet_negotiation()
    test_strip_telnet_subnegotiation()
    test_strip_telnet_escaped_ff()
    test_strip_telnet_partial_held()
    test_origin_allowed()
    print()
    if _failures:
        print(f"{_failures} FAILURE(S)")
        raise SystemExit(1)
    print("all console-web tests passed")


if __name__ == "__main__":
    main()
