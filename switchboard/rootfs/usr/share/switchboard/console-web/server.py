#!/usr/bin/env python3
"""Switchboard console web terminal — a browser front-end for the telnet
operator console (console/console.py, listening on 127.0.0.1:2300).

A small stdlib-only HTTP + WebSocket server (no pip deps; the add-on is musl):

  * GET /            → the xterm.js terminal page (static/index.html).
  * GET /static/*    → vendored xterm.js / xterm.css (committed, no CDN).
  * GET /healthz     → "ok" (liveness).
  * WS  /ws          → one TCP connection to the operator console per browser
                       socket. We act as the telnet CLIENT: answer/ignore the
                       console's IAC negotiation so it never reaches the
                       browser, pipe clean ANSI console→browser as binary
                       frames, and raw keystrokes browser→console. A browser
                       {"type":"resize",...} message becomes a telnet NAWS
                       subnegotiation the console already understands.

This server is reachable on the LAN (host_network). It fronts what the operator
console already exposes unauthenticated on :2300 — but on a *browser* transport,
which the raw telnet port is not. To avoid handing a drive-by web page that
call-control reach, the WS upgrade is same-origin-gated (see wsproto.origin_allowed),
the bind is configurable (CONSOLE_WEB_BIND, default follows console_bind), sessions
are capped (MAX_SESSIONS) and browser-idle-timed-out. The pure framing/telnet
helpers live in wsproto.py and are unit-tested; this file is only socket plumbing.
"""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import socketserver
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webauth  # noqa: E402
import wsproto  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

# Login gate (see webauth.py). CONSOLE_WEB_USERS is compact JSON exported by the
# run script from the console_users option; an empty list disables the gate
# (backward-compatible — the run script's NOTICE says which mode is live).
_USERS = webauth.parse_users(os.environ.get("CONSOLE_WEB_USERS", ""))
AUTH_REQUIRED = bool(_USERS)
_SESSIONS = webauth.Sessions()
_THROTTLE = webauth.LoginThrottle()


def _env_int(name: str, default: int) -> int:
    """int() of a config-derived env var, tolerating unset *or* set-but-empty.

    These ports come from switchboard-opt in the s6 run script; during a config
    reload / options.json rewrite (e.g. an add-on schema migration) that export
    can momentarily be an empty string. The 2-arg ``os.environ.get(name, "8100")``
    default only covers the *absent* case, so ``int(get(...))`` still throws on
    ``""`` — which crash-loops this longrun until s6 restarts it. Fall back to
    ``default`` for empty/blank too, matching console.py / rtpmon / devhealth.
    """
    return int(os.environ.get(name, "").strip() or default)


# The operator console we bridge to. Loopback only: the browser never connects
# to :2300 directly, this server does, on the same host.
CONSOLE_HOST = os.environ.get("CONSOLE_WEB_TARGET_HOST", "127.0.0.1")
CONSOLE_PORT = _env_int("CONSOLE_WEB_TARGET_PORT", 2300)

# Match the operator console's own session cap so the browser front-end can't be
# used to exhaust console sessions; this is an unauthenticated LAN service.
MAX_SESSIONS = 5

# Close a browser session that has sent no input for this long. The board redraws
# ~1 Hz so the bridge is never select-idle — we time out on lack of *browser*
# input, independently of the console's own idle reclaim.
IDLE_SECONDS = 900

# Upper bound on a single blocking write to the browser socket. A peer that
# completes the WS handshake then stops reading fills our send buffer; without a
# bound, sock.sendall() parks the bridge thread FOREVER — which both leaks one of
# the MAX_SESSIONS slots (and a console telnet session) and defeats the idle
# reclaim (never re-reached while parked in sendall). With a finite timeout the
# stalled write raises socket.timeout (an OSError), send_ws() returns False, and
# the session is torn down like any other dead peer.
SEND_TIMEOUT = 30

# Extra origins explicitly allowed for the WS upgrade (comma-separated env).
_ALLOWED_ORIGINS = [o for o in os.environ.get("CONSOLE_WEB_ALLOWED_ORIGINS", "").split(",") if o.strip()]

# Static assets we will serve, by URL path → (filesystem name, content type).
# NOTE: index.html is deliberately NOT in this map. It is the terminal page and
# is served only through the session-gated "/" route — listing it here would
# publish an ungated second door to the same page (it did, before v0.44.0
# shipped; the WS stayed gated, but the page itself must not leak either).
_STATIC_FILES = {
    "/static/xterm.js": ("xterm.js", "application/javascript; charset=utf-8"),
    "/static/xterm.css": ("xterm.css", "text/css; charset=utf-8"),
}
# Served by "/" only, after the session check.
_INDEX_FILE = ("index.html", "text/html; charset=utf-8")


def log(msg: str) -> None:
    print(f"[switchboard-console-web] {msg}", flush=True)


def _read_http_head(sock: socket.socket):
    """Read bytes until the end of the HTTP request head (CRLFCRLF).

    Returns (head_bytes, leftover_body_bytes) or (b"", b"") on a closed/oversized
    request. Capped so a client can't make us buffer unbounded headers.
    """
    buf = b""
    while b"\r\n\r\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except OSError:
            return b"", b""
        if not chunk:
            return b"", b""
        buf += chunk
        if len(buf) > 16384:  # 16 KiB of headers is already pathological
            return b"", b""
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def _serve_static(sock: socket.socket, path: str, entry=None) -> None:
    name, ctype = entry if entry is not None else _STATIC_FILES[path]
    full = os.path.join(STATIC_DIR, name)
    try:
        with open(full, "rb") as fh:
            body = fh.read()
    except OSError:
        _send_simple(sock, 404, "Not Found")
        return
    head = (
        f"HTTP/1.1 200 OK\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Cache-Control: no-cache\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        sock.sendall(head + body)
    except OSError:
        pass


def _send_simple(sock: socket.socket, code: int, reason: str, body: str = "") -> None:
    payload = body.encode("utf-8")
    head = (
        f"HTTP/1.1 {code} {reason}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        sock.sendall(head + payload)
    except OSError:
        pass


def _send_html(sock: socket.socket, code: int, reason: str, html: str,
               set_cookie: str = "") -> None:
    payload = html.encode("utf-8")
    cookie = f"Set-Cookie: {set_cookie}\r\n" if set_cookie else ""
    head = (
        f"HTTP/1.1 {code} {reason}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Cache-Control: no-store\r\n"
        f"{cookie}"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        sock.sendall(head + payload)
    except OSError:
        pass


def _send_redirect(sock: socket.socket, location: str, set_cookie: str = "") -> None:
    cookie = f"Set-Cookie: {set_cookie}\r\n" if set_cookie else ""
    head = (
        f"HTTP/1.1 303 See Other\r\n"
        f"Location: {location}\r\n"
        f"Content-Length: 0\r\n"
        f"{cookie}"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        sock.sendall(head)
    except OSError:
        pass


def _read_body(sock: socket.socket, headers: dict, leftover: bytes) -> bytes:
    """Read a small request body (Content-Length-driven), starting from any
    bytes already read past the head. Capped: a login form is tiny."""
    try:
        want = int(headers.get("content-length", "0") or "0")
    except ValueError:
        return b""
    if want <= 0 or want > webauth.MAX_FORM_BYTES:
        return b""
    body = leftover[:want]
    while len(body) < want:
        try:
            chunk = sock.recv(min(4096, want - len(body)))
        except OSError:
            return b""
        if not chunk:
            return b""
        body += chunk
    return body


def _session_ok(headers: dict) -> bool:
    """True when the request carries a valid session cookie (or the gate is off)."""
    if not AUTH_REQUIRED:
        return True
    token = webauth.parse_cookies(headers.get("cookie", "")).get(webauth.COOKIE_NAME, "")
    return _SESSIONS.valid(token)


def _peer_ip(sock: socket.socket) -> str:
    try:
        return sock.getpeername()[0]
    except OSError:
        return "?"


def _bridge_ws(sock: socket.socket, leftover: bytes, token: str = "") -> None:
    """Pump bytes between an upgraded browser WebSocket and the operator console.

    `leftover` is any bytes already read past the HTTP head (usually empty —
    the browser sends frames only after the 101 response).
    """
    try:
        console = socket.create_connection((CONSOLE_HOST, CONSOLE_PORT), timeout=5)
    except OSError as exc:
        log(f"console connect failed: {exc}")
        try:
            sock.sendall(wsproto.encode_frame(
                b"\r\n  Operator console unavailable (is it enabled?).\r\n", wsproto.OP_TEXT))
        except OSError:
            pass
        return

    # We are the telnet client: pre-accept the console's negotiation so it puts
    # us in character mode and starts sizing from NAWS. The console sends
    # IAC WILL ECHO / WILL SGA / DO SGA / DO NAWS at connect; mirror a sane
    # client reply (DO its WILLs, WILL SGA, WILL NAWS) once up front. strip_telnet
    # then removes everything the console sends so the browser sees clean ANSI.
    try:
        console.sendall(bytes([
            wsproto.IAC, wsproto.DO, wsproto.OPT_ECHO,
            wsproto.IAC, wsproto.DO, wsproto.OPT_SGA,
            wsproto.IAC, wsproto.WILL, wsproto.OPT_SGA,
            wsproto.IAC, wsproto.WILL, wsproto.OPT_NAWS,
        ]))
    except OSError:
        console.close()
        return

    # Bound writes to the browser so a peer that stops reading can't park sendall
    # forever (see SEND_TIMEOUT). This must be a settimeout, NOT setblocking(True):
    # the latter resets the timeout to None and silently un-does the caller's bound.
    # recv() only runs after select() reports the socket readable, so the timeout
    # effectively bounds the write side only.
    sock.settimeout(SEND_TIMEOUT)
    console.setblocking(True)
    ws_in = leftover  # undecoded client->server frame bytes
    tn_in = b""       # console bytes pending telnet strip
    open_ = True

    def send_ws(payload: bytes, opcode: int = wsproto.OP_BIN) -> bool:
        try:
            sock.sendall(wsproto.encode_frame(payload, opcode))
            return True
        except OSError:
            return False

    # Process any frames that arrived glued to the handshake before we block.
    def drain_ws_frames() -> bool:
        nonlocal ws_in
        frames, ws_in = wsproto.decode_frames(ws_in)
        for opcode, data in frames:
            if opcode == "error" or opcode == wsproto.OP_CLOSE:
                return False
            if opcode == wsproto.OP_PING:
                if not send_ws(data, wsproto.OP_PONG):
                    return False
                continue
            if opcode == wsproto.OP_PONG:
                continue
            if not _handle_client_payload(console, data):
                return False
        return True

    last_input = time.monotonic()
    try:
        if not drain_ws_frames():
            open_ = False
        while open_:
            if time.monotonic() - last_input > IDLE_SECONDS:
                break  # browser idle too long — reclaim the session slot
            # Re-check the login session on every loop pass (<=30 s, the select
            # timeout). Validating only at upgrade would leave a live console
            # bridge fully usable after /logout or after the session's absolute
            # TTL expired — there would be no way to kick a session short of
            # restarting the service.
            if AUTH_REQUIRED and not _SESSIONS.valid(token):
                log("session ended (logout or expiry) — closing console bridge")
                break
            try:
                readable, _, _ = select.select([sock, console], [], [], 30)
            except (OSError, ValueError):
                break
            if not readable:
                continue  # idle tick; keep the bridge alive
            if console in readable:
                try:
                    chunk = console.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                tn_in += chunk
                clean, tn_in = wsproto.strip_telnet(tn_in)
                if clean and not send_ws(clean):
                    break
            if sock in readable:
                try:
                    chunk = sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                last_input = time.monotonic()  # browser is active
                ws_in += chunk
                if len(ws_in) > 65536:  # malformed flood; drop the connection
                    break
                if not drain_ws_frames():
                    break
    finally:
        try:
            console.close()
        except OSError:
            pass
        try:
            sock.sendall(wsproto.encode_frame(b"", wsproto.OP_CLOSE))
        except OSError:
            pass


def _handle_client_payload(console: socket.socket, data: bytes) -> bool:
    """A decoded browser→console payload. JSON {"type":"resize"} becomes a NAWS
    subnegotiation; anything else is raw keystroke bytes forwarded verbatim.
    Returns False if the console write fails."""
    if data[:1] == b"{" and b"resize" in data:
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            msg = None
        if isinstance(msg, dict) and msg.get("type") == "resize":
            cols = msg.get("cols", 80)
            rows = msg.get("rows", 24)
            try:
                console.sendall(wsproto.naws_subnegotiation(cols, rows))
                return True
            except OSError:
                return False
            except (TypeError, ValueError):
                return True  # ignore a malformed resize, keep the session
    # Raw keystrokes. A literal 0xFF from the browser must be doubled so the
    # telnet server doesn't read it as IAC.
    payload = data.replace(b"\xff", b"\xff\xff") if b"\xff" in data else data
    try:
        console.sendall(payload)
        return True
    except OSError:
        return False


def main() -> None:
    port = _env_int("CONSOLE_WEB_PORT", 8100)
    host = os.environ.get("CONSOLE_WEB_BIND", "0.0.0.0") or "0.0.0.0"
    slots = threading.BoundedSemaphore(MAX_SESSIONS)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            # Only the WS path is session-capped (it ties up a console session);
            # static GETs are cheap and short-lived, so don't gate them.
            try:
                self.request.settimeout(30)
            except OSError:
                return
            try:
                _handle_connection_gated(self.request, slots)
            except Exception as exc:  # never let one client crash the server
                log(f"connection error: {exc}")

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server((host, port), Handler)

    def shutdown(*_):
        log("shutting down")
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"console web terminal listening on {host}:{port} "
        f"(bridging to {CONSOLE_HOST}:{CONSOLE_PORT}) — login gate "
        f"{'ENABLED (' + str(len(_USERS)) + ' user(s))' if AUTH_REQUIRED else 'DISABLED (open on the LAN)'}")
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


def _handle_connection_gated(sock: socket.socket, slots: threading.BoundedSemaphore) -> None:
    """Read the request head, then gate only WebSocket upgrades on the session
    semaphore (static assets stay ungated)."""
    head, rest = _read_http_head(sock)
    if not head:
        return
    method, path, headers = wsproto.parse_http_headers(head)
    if method is None:
        _send_simple(sock, 400, "Bad Request")
        return
    path = path.split("?", 1)[0]
    if method == "GET" and path in ("/ws", "/ws/"):
        if not wsproto.is_websocket_upgrade(headers):
            _send_simple(sock, 426, "Upgrade Required", "Expected a WebSocket upgrade.")
            return
        if headers.get("sec-websocket-version", "").strip() != "13":
            try:
                sock.sendall(b"HTTP/1.1 426 Upgrade Required\r\n"
                             b"Sec-WebSocket-Version: 13\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            return
        # Drive-by / cross-site-WebSocket-hijack guard: this fronts a
        # call-control console, so reject a cross-origin upgrade.
        if not wsproto.origin_allowed(headers, _ALLOWED_ORIGINS):
            _send_simple(sock, 403, "Forbidden", "Cross-origin WebSocket rejected.")
            return
        # Login gate: the WS carries the actual console session, so it MUST be
        # cookie-checked itself — gating only the HTML page would leave the
        # terminal one hand-rolled upgrade away for anyone on the LAN.
        if not _session_ok(headers):
            _send_simple(sock, 403, "Forbidden", "Sign in first.")
            return
        if not slots.acquire(blocking=False):
            _send_simple(sock, 503, "Service Unavailable",
                         "Console busy (too many sessions). Try again shortly.")
            return
        try:
            key = headers.get("sec-websocket-key", "")
            try:
                sock.sendall(wsproto.handshake_response(key))
            except OSError:
                return
            # Bound blocking writes so a peer that stops reading can't park the
            # bridge in sendall forever (see SEND_TIMEOUT). Reads are still driven
            # by select(), which only calls recv() once the socket is readable, so
            # the timeout effectively only bounds the write side.
            try:
                sock.settimeout(SEND_TIMEOUT)
            except OSError:
                pass
            token = webauth.parse_cookies(
                headers.get("cookie", "")).get(webauth.COOKIE_NAME, "")
            _bridge_ws(sock, rest, token)
        finally:
            slots.release()
        return
    # Non-WS request: dispatch as a normal HTTP request using the already-read head.
    _dispatch_http(sock, method, path, headers, rest)


def _dispatch_http(sock, method, path, headers, leftover: bytes = b"") -> None:
    if AUTH_REQUIRED and method == "POST" and path == "/login":
        _handle_login(sock, headers, leftover)
        return
    if method != "GET":
        _send_simple(sock, 405, "Method Not Allowed")
        return
    if AUTH_REQUIRED and path == "/logout":
        token = webauth.parse_cookies(headers.get("cookie", "")).get(webauth.COOKIE_NAME, "")
        _SESSIONS.revoke(token)
        _send_html(sock, 200, "OK", webauth.login_page("Signed out."),
                   set_cookie=webauth.clear_cookie())
        return
    if path in ("/", "/index.html"):
        # The terminal page itself is gated: an unauthenticated visitor gets the
        # sign-in form. (The real enforcement is on /ws above — this is UX.)
        if not _session_ok(headers):
            _send_html(sock, 200, "OK", webauth.login_page())
            return
        _serve_static(sock, "/", _INDEX_FILE)
        return
    if path in _STATIC_FILES:
        # Vendored xterm.js/css only — public, harmless, and needed by the page
        # the moment the login redirect lands.
        _serve_static(sock, path)
        return
    if path in ("/healthz", "/health"):
        _send_simple(sock, 200, "OK", "ok")
        return
    _send_simple(sock, 404, "Not Found")


def _handle_login(sock, headers, leftover: bytes) -> None:
    ip = _peer_ip(sock)
    # Same-origin gate, mirroring the WS upgrade. A cross-origin form POST can
    # never read the response, but WITHOUT this any web page a household browser
    # visits could burn this IP's login attempts and lock the console out.
    if not wsproto.origin_allowed(headers, _ALLOWED_ORIGINS):
        _send_simple(sock, 403, "Forbidden", "Cross-origin login rejected.")
        return
    # Charge the throttle BEFORE verifying: a denied or failed attempt must
    # never be free to retry (charging after the check races the window).
    if not _THROTTLE.charge(ip):
        log(f"login throttled for {ip}")
        _send_html(sock, 429, "Too Many Requests",
                   webauth.login_page("Too many attempts — wait a few minutes."))
        return
    username, password = webauth.parse_login_form(_read_body(sock, headers, leftover))
    if webauth.check_credentials(_USERS, username, password):
        _THROTTLE.clear(ip)
        token = _SESSIONS.issue()
        log(f"login ok for {username!r} from {ip}")
        _send_redirect(sock, "/", set_cookie=webauth.session_cookie(token))
        return
    log(f"login FAILED from {ip}")
    _send_html(sock, 200, "OK", webauth.login_page("Wrong username or password."))


if __name__ == "__main__":
    main()
