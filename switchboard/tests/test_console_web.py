"""Behavioral tests for the console web terminal's pure framing helpers
(console-web/consoleproto.py).

Run with plain Python (no deps):

    python3 switchboard/tests/test_console_web.py

Covers the side-effect-free pieces: the RFC 6455 handshake key, WebSocket frame
encode (server->client, unmasked) and decode (client->server, masked), the
telnet NAWS subnegotiation, and stripping the operator console's IAC negotiation
out of the console->browser stream. Of server.py's socket plumbing, only the
bridge to the operator console's Unix socket is driven here, against a fake
board; the HTTP side is test_console_web_auth.py's.
"""
import contextlib
import os
import sys
import struct
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

CONSOLEPROTO_PATH = (
    Path(__file__).resolve().parents[1]
    / "rootfs" / "usr" / "share" / "switchboard" / "console-web" / "consoleproto.py"
)
_spec = spec_from_loader("switchboard_consoleproto",
                        SourceFileLoader("switchboard_consoleproto", str(CONSOLEPROTO_PATH)))
ws = module_from_spec(_spec)
_spec.loader.exec_module(ws)

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    # Under pytest the print + counter are DECORATIVE — only the __main__
    # runner reads _failures, so a failing check would still 'pass' the
    # test. Assert too, so both harnesses actually enforce every check.
    assert cond, name


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
        b"GET /ws HTTP/1.1\r\nHost: 192.168.1.152:8100\r\n"
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


def test_bridge_write_is_bounded() -> None:
    # A wedged (non-reading) WS peer must not park the bridge in sendall forever.
    # The bridge socket gets a FINITE send timeout, not settimeout(None) — source
    # guard (the socket plumbing itself isn't exercised here, same split as above).
    src = (Path(__file__).resolve().parents[1]
           / "rootfs" / "usr" / "share" / "switchboard" / "console-web" / "server.py").read_text()
    check("bridge: SEND_TIMEOUT defined + finite positive", "SEND_TIMEOUT = " in src)
    check("bridge: no unbounded settimeout(None) on the bridge", "settimeout(None)" not in src)
    check("bridge: bridge socket bounded by SEND_TIMEOUT", "settimeout(SEND_TIMEOUT)" in src)
    # The browser socket must NOT be reset to blocking inside _bridge_ws — that
    # silently un-does the timeout (setblocking(True) == settimeout(None)). Only the
    # trusted localhost console socket keeps blocking.
    check("bridge: sock not reset to blocking (would nullify the timeout)",
          "sock.setblocking(True)" not in src)


SERVER_PATH = (Path(__file__).resolve().parents[1]
               / "rootfs" / "usr" / "share" / "switchboard" / "console-web" / "server.py")


@contextlib.contextmanager
def _server_module(env: dict):
    """Load server.py fresh with ``env`` set, and undo everything it leaks.

    server.py does its own sys.path.insert and imports its siblings by BARE
    name, so loading it here leaks both into the session. Snapshot the WHOLE
    list (one .remove() is not enough) and the bare names. Caught by the autouse
    tripwire in conftest.py — and note it only ERRORED when this file ran ALONE:
    in the full suite an earlier test had already put that path there, so it was
    inside the "before" snapshot and invisible. That masking is exactly what
    makes a leak like this survive.

    Yields the module, or None when importing it raised.
    """
    saved = {k: os.environ.get(k) for k in env}
    saved_path = list(sys.path)
    saved_mods = {n: sys.modules.get(n) for n in ("consoleproto", "webauth")}
    try:
        os.environ.update(env)
        srv = None
        try:
            loader = SourceFileLoader("switchboard_console_web_server", str(SERVER_PATH))
            srv = module_from_spec(spec_from_loader(loader.name, loader))
            loader.exec_module(srv)
        except Exception as exc:  # noqa: BLE001 — a raise here IS the regression
            srv = None
            print("  import raised:", repr(exc))
        yield srv
    finally:
        sys.path[:] = saved_path
        for n, m in saved_mods.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m
        sys.modules.pop("switchboard_console_web_server", None)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_env_int_tolerates_empty_port() -> None:
    """Regression: a config-derived port env arriving *empty* must not crash the
    service. bashio::config can hand back "" during a config reload / options.json
    rewrite (seen live on the v0.29.0 schema migration); the old
    ``int(os.environ.get("CONSOLE_WEB_PORT", "8100"))`` threw ``ValueError`` on ""
    because the 2-arg default only covers the *absent* key, not a set-but-empty one.

    The import-time crash site this used to exercise (the TCP target port) is
    gone with the TCP target; the socket path that replaced it is read the same
    tolerant way, so an empty export must fall back to the default rather than
    point the bridge at "".
    """
    with _server_module({"CONSOLE_SOCKET": "", "CONSOLE_WEB_PORT": "",
                         "T_BLANK": "", "T_VAL": "", "T_PAD": ""}) as srv:
        check("server.py imports with an empty CONSOLE_SOCKET (no crash)", srv is not None)
        if srv is None:
            return
        check("an empty CONSOLE_SOCKET falls back to the board's socket",
              srv.CONSOLE_SOCKET == "/run/switchboard/console.sock")
        check("_env_int: empty -> default", srv._env_int("CONSOLE_WEB_PORT", 8100) == 8100)
        os.environ["T_BLANK"] = "   "
        check("_env_int: whitespace-only -> default", srv._env_int("T_BLANK", 8100) == 8100)
        check("_env_int: absent key -> default", srv._env_int("T_UNSET_ZZ", 8100) == 8100)
        os.environ["T_VAL"] = "9000"
        check("_env_int: a real value wins", srv._env_int("T_VAL", 8100) == 9000)
        os.environ["T_PAD"] = "  9000  "
        check("_env_int: surrounding whitespace tolerated", srv._env_int("T_PAD", 8100) == 9000)


def _sock_dir() -> str:
    """A temp dir short enough for a Unix socket path (macOS caps it at 104)."""
    import tempfile
    d = tempfile.mkdtemp(prefix="sbcw-")
    if len(d) > 80:
        d = tempfile.mkdtemp(prefix="sbcw-", dir="/tmp")
    return d


def _recv_until(sock, marker: bytes, limit: float = 5.0) -> bytes:
    import time
    buf, end = b"", time.monotonic() + limit
    sock.settimeout(limit)
    while marker not in buf and time.monotonic() < end:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def test_the_bridge_reaches_the_board_over_its_unix_socket() -> None:
    """★ The real _bridge_ws against a fake board on a real Unix socket.

    The board no longer listens on TCP at all, so a bridge still dialling
    127.0.0.1:2300 would show "unavailable" on every connect. What the board
    receives is pinned byte for byte: the client preamble, the resize as NAWS
    (never as typed text) and the keystroke. What the browser receives is the
    board's output with its telnet negotiation stripped.
    """
    import socket
    import threading
    path = os.path.join(_sock_dir(), "console.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    got, done = bytearray(), threading.Event()

    def board():
        conn, _ = listener.accept()
        with conn:
            conn.sendall(bytes([ws.IAC, ws.WILL, ws.OPT_ECHO, ws.IAC, ws.WILL, ws.OPT_SGA,
                                ws.IAC, ws.DO, ws.OPT_SGA, ws.IAC, ws.DO, ws.OPT_NAWS])
                         + b"BOARD")
            conn.settimeout(5)
            try:
                while not got.endswith(b"j"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    got.extend(chunk)
            finally:
                done.set()
            conn.recv(1)          # hold the board open until the bridge closes it

    bt = threading.Thread(target=board, daemon=True)
    bt.start()
    try:
        with _server_module({"CONSOLE_SOCKET": path, "CONSOLE_WEB_USERS": ""}) as srv:
            check("server.py loads", srv is not None)
            check("the bridge targets the socket it was given", srv.CONSOLE_SOCKET == path)
            browser, ours = socket.socketpair()
            t = threading.Thread(target=srv._bridge_ws, args=(ours, b""), daemon=True)
            t.start()
            first = _recv_until(browser, b"BOARD")
            check(f"the browser gets the board's output, telnet stripped ({first!r})",
                  first == ws.encode_frame(b"BOARD", ws.OP_BIN))
            browser.sendall(_mask(b'{"type":"resize","cols":100,"rows":30}'))
            browser.sendall(_mask(b"j"))
            done.wait(5)
            want = bytes([ws.IAC, ws.DO, ws.OPT_ECHO, ws.IAC, ws.DO, ws.OPT_SGA,
                          ws.IAC, ws.WILL, ws.OPT_SGA, ws.IAC, ws.WILL, ws.OPT_NAWS]) \
                + ws.naws_subnegotiation(100, 30) + b"j"
            check(f"the board gets preamble + NAWS + keystroke ({bytes(got)!r})",
                  bytes(got) == want)
            browser.close()       # the browser goes away
            t.join(5)
            check("the bridge exits when the browser goes away", not t.is_alive())
            ours.close()
    finally:
        listener.close()
        bt.join(5)


def test_a_missing_board_socket_says_unavailable() -> None:
    """The board idles (both console switches off) or is still starting: no socket
    file at all. The browser must get the notice, not a silent dead terminal."""
    import socket
    path = os.path.join(_sock_dir(), "absent.sock")
    with _server_module({"CONSOLE_SOCKET": path, "CONSOLE_WEB_USERS": ""}) as srv:
        check("server.py loads", srv is not None)
        browser, ours = socket.socketpair()
        with browser, ours:
            srv._bridge_ws(ours, b"")
            got = _recv_until(browser, b"unavailable")
        check(f"the browser is told the console is unavailable ({got!r})",
              b"Operator console unavailable" in got)


def test_the_startup_line_reports_reachability_not_just_the_gate():
    """★ Two log lines from one service must not disagree about a security fact.

    This line read "DISABLED (open on the LAN)" whenever no user was configured,
    whatever the bind. When console_web_bind moved to loopback in v0.94.0 it
    contradicted the NOTICE printed three lines above it, which correctly said
    the terminal was not reachable from the LAN. Whichever line a reader
    believes, they have no way to tell which is stale — and this is exactly the
    class of wrong-about-where-data-goes comment that has misled an audit of
    this repo before.
    """
    src = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
           / "switchboard" / "console-web" / "server.py").read_text()
    # Scan CODE, not comments. The block above this line in server.py explains
    # the defect by quoting the very string being forbidden, so a whole-file
    # substring test matches its own explanation — the third self-match of this
    # shape in this repo (test_privacy_invariants and test_ingress_guard were
    # the others). Forbid the CONSTRUCT, not the characters.
    code = "\n".join(ln for ln in src.split("\n")
                     if not ln.lstrip().startswith("#"))
    assert 'gate = "DISABLED (open on the LAN)"' not in code, (
        "the startup line still claims LAN reachability from the gate state "
        "alone, independent of the bind")
    block = src[src.index("loopback = host in"):src.index("console web terminal listening on")]
    assert '"127.0.0.1"' in block and '"::1"' in block, (
        "the reachability check does not recognise both loopback forms")
    assert "AUTH_REQUIRED" in block, "the gate state is no longer consulted"
    assert "not reachable from the LAN" in src and "reachable from the LAN" in src


def test_the_terminal_always_announces_its_size_on_a_new_socket():
    """★ Why the LIGHTS screen stopped part-way down the list.

    The page measured itself at startup with `applySize(false)`, which filled
    `lastCols`/`lastRows` BEFORE any socket existed. `applySize` then returned
    early whenever a later measurement matched that cache — including the call
    from `ws.onopen`, the only one that actually transmits. So on a fresh
    connection the console was never told the window size and stayed at its
    built-in 80x24 for the life of the page, with everything below row 24 simply
    absent. Verified live before the fix: a raw socket that sends no resize gets
    a 79-column board; one that sends `{"type":"resize","cols":180}` gets 179.

    "Unchanged locally" is not "the server knows". The cache is a guard against
    redundant term.resize() work and must never gate the handshake.

    Source-structure assertion — the page's JS has no DOM in this suite — but it
    pins the two things that would regress rather than a phrase.
    """
    html = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
            / "switchboard" / "console-web" / "static" / "index.html").read_text()

    assert "function applySize(send, force)" in html, (
        "applySize lost its force parameter; onopen can no longer guarantee a send")
    assert "(changed || force)" in html, (
        "the send is gated on the size having CHANGED again — a fresh socket "
        "with an unchanged measurement will never be told the size")
    assert "applySize(true, true)" in html, (
        "ws.onopen does not force the size announcement")
    assert "if (s.cols === lastCols && s.rows === lastRows) return;" not in html, (
        "the unconditional early return is back")

    # The parent sizes this iframe after the card lays out, which grows the
    # element without the iframe's own window firing a resize event.
    assert "ResizeObserver" in html, (
        "nothing observes the terminal element; embedded in a dashboard the "
        "iframe is resized by its parent with no window resize event")
    assert 'observe(document.getElementById("term"))' in html, (
        "the ResizeObserver is constructed but never attached to anything")


def test_the_terminal_wears_the_tokens_and_fits_after_its_font():
    """The web terminal's look, pinned to the page's own design tokens.

    Source-structure, like the test above: the theme literals must equal the
    :root tokens they name, the typewriter face must be first in the stack,
    the page must make no external request, and the terminal must open and fit
    only after the font wait — measuring the fallback face and then swapping to
    Cutive Mono would size the grid, and the NAWS sent to the console, for the
    wrong glyphs.
    """
    import re
    html = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
            / "switchboard" / "console-web" / "static" / "index.html").read_text()
    tok = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-f]{6});", html))
    for want in ("walnut-0", "enamel", "lamp", "brass"):
        assert want in tok, f"--{want} is not declared on :root"

    assert f'background: "{tok["walnut-0"]}"' in html, "terminal ground is not --walnut-0"
    assert f'foreground: "{tok["enamel"]}"' in html, "terminal text is not --enamel"
    assert f'cursor: "{tok["lamp"]}"' in html, "cursor is not the amber --lamp"
    r, g, b = (int(tok["brass"][i:i + 2], 16) for i in (1, 3, 5))
    assert f'selectionBackground: "rgba({r}, {g}, {b}, 0.3)"' in html, (
        "selection is not --brass at 30 %")
    assert "minimumContrastRatio: 4.5" in html, "terminal text may fall under 4.5:1"
    assert re.search(r"fontFamily: \"'Cutive Mono',", html), (
        "Cutive Mono is not first in the terminal's font stack")

    # Self-hosted only: no absolute or protocol-relative URL in any src, href
    # or url(); every face swaps rather than blocking the first paint.
    refs = re.findall(r'(?:src|href)="([^"]*)"|url\("([^"]*)"\)', html)
    for ref in (a or b for a, b in refs):
        assert not re.match(r"(?:[a-z]+:)?//", ref, re.I), f"external request: {ref}"
    faces = re.findall(r"@font-face\s*{[^}]*}", html)
    assert len(faces) == 2 and all("font-display: swap" in f for f in faces)

    # Open + fit only inside the font wait, and the wait is capped.
    gate = html.index("whenFontsReady(function")
    assert html.index("term.open(") > gate, "the terminal opens before the font wait"
    assert html.index("applySize(false);") > gate, "the first fit runs before the font wait"
    assert html.index("connect();") > gate, "the socket opens before the font wait"
    assert "document.fonts.ready" in html and "setTimeout(once," in html, (
        "the font wait is uncapped or no longer waits on document.fonts")

    # The element ids the script hooks are unchanged.
    assert 'id="term"' in html and 'id="status"' in html


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
