"""Route-level tests for the console web terminal's login gate.

    python3 -m pytest switchboard/tests/test_console_web_auth.py

These drive the REAL server over a real socket, because the unit tests in
test_webauth.py cover only the pure helpers — and a helper-only suite is exactly
what let an ungated `/static/index.html` (a second door to the terminal page)
ship in the first place. Anything that decides "is this request allowed" belongs
here.

The server module is loaded twice, under two different CONSOLE_WEB_USERS
environments (the gate is decided at import time), each on its own ephemeral
port.
"""

import http.client
import importlib.machinery
import importlib.util
import os
import socket
import sys
import threading
from pathlib import Path

CONSOLE_WEB = (Path(__file__).resolve().parents[1]
               / "rootfs" / "usr" / "share" / "switchboard" / "console-web")
USERS_JSON = '[{"username":"eric","password":"s3cret"}]'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _load_server(mod_name: str, users_env: str):
    """Import server.py fresh with a given CONSOLE_WEB_USERS (import-time gate)."""
    sys.path.insert(0, str(CONSOLE_WEB))
    old = os.environ.get("CONSOLE_WEB_USERS")
    os.environ["CONSOLE_WEB_USERS"] = users_env
    try:
        loader = importlib.machinery.SourceFileLoader(mod_name, str(CONSOLE_WEB / "server.py"))
        spec = importlib.util.spec_from_loader(mod_name, loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            os.environ.pop("CONSOLE_WEB_USERS", None)
        else:
            os.environ["CONSOLE_WEB_USERS"] = old


class _Server:
    """The real request handler on a real port, in a background thread."""

    def __init__(self, mod):
        import socketserver
        self.mod = mod
        self.port = _free_port()
        slots = threading.BoundedSemaphore(mod.MAX_SESSIONS)

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(10)
                try:
                    mod._handle_connection_gated(self.request, slots)
                except Exception:
                    pass

        class Srv(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.srv = Srv(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if body is not None:
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        out = (r.status, dict(r.getheaders()), data)
        c.close()
        return out

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


# ── gate ON ──────────────────────────────────────────────────────────────────

def test_gated_routes():
    mod = _load_server("sbserver_gated", USERS_JSON)
    assert mod.AUTH_REQUIRED is True
    srv = _Server(mod)
    try:
        # The terminal page is replaced by the sign-in form when signed out.
        st, _, body = srv.req("GET", "/")
        assert st == 200 and b'name="password"' in body and b"xterm" not in body

        # THE REGRESSION THAT SHIPPED: the page must not be reachable by its
        # static path either.
        st, _, _ = srv.req("GET", "/static/index.html")
        assert st == 404, "static/index.html must not bypass the page gate"

        # Vendored assets stay public (the login page itself needs nothing, but
        # the terminal page pulls them the moment the session lands).
        assert srv.req("GET", "/static/xterm.js")[0] == 200
        assert srv.req("GET", "/healthz")[0] == 200

        # Wrong credentials: no cookie, error shown, still no terminal.
        st, hdrs, body = srv.req("POST", "/login", "username=eric&password=WRONG")
        assert st == 200 and b"Wrong username" in body
        assert "set-cookie" not in {k.lower() for k in hdrs}

        # Right credentials: redirect + session cookie.
        st, hdrs, _ = srv.req("POST", "/login", "username=eric&password=s3cret")
        assert st == 303
        cookie = hdrs.get("Set-Cookie", "")
        assert cookie.startswith(mod.webauth.COOKIE_NAME + "=")
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        token = cookie.split(";")[0].split("=", 1)[1]

        # With the session, "/" serves the real terminal page.
        st, _, body = srv.req("GET", "/", headers={"Cookie": f"{mod.webauth.COOKIE_NAME}={token}"})
        assert st == 200 and b"xterm" in body

        # A forged cookie does not.
        st, _, body = srv.req("GET", "/", headers={"Cookie": f"{mod.webauth.COOKIE_NAME}=forged"})
        assert b'name="password"' in body

        # The WebSocket — which carries the actual console session — is gated
        # too, not just the page.
        ws_headers = {"Upgrade": "websocket", "Connection": "Upgrade",
                      "Sec-WebSocket-Version": "13",
                      "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                      "Origin": f"http://127.0.0.1:{srv.port}"}
        assert srv.req("GET", "/ws", headers=ws_headers)[0] == 403
        st, _, _ = srv.req("GET", "/ws", headers={**ws_headers,
                                                  "Cookie": f"{mod.webauth.COOKIE_NAME}=forged"})
        assert st == 403

        # Cross-origin login is refused (else any page could burn this IP's
        # attempts and lock the household out).
        st, _, _ = srv.req("POST", "/login", "username=eric&password=s3cret",
                           {"Origin": "http://evil.example"})
        assert st == 403

        # Logout revokes: the same token no longer opens the page.
        srv.req("GET", "/logout", headers={"Cookie": f"{mod.webauth.COOKIE_NAME}={token}"})
        st, _, body = srv.req("GET", "/", headers={"Cookie": f"{mod.webauth.COOKIE_NAME}={token}"})
        assert b'name="password"' in body
    finally:
        srv.stop()


def test_login_throttled_after_repeated_failures():
    mod = _load_server("sbserver_throttle", USERS_JSON)
    srv = _Server(mod)
    try:
        codes = [srv.req("POST", "/login", "username=eric&password=no")[0]
                 for _ in range(mod.webauth.MAX_ATTEMPTS + 2)]
        assert 429 in codes, f"expected a throttled response, got {codes}"
        # Even the CORRECT password is refused while throttled.
        st, hdrs, _ = srv.req("POST", "/login", "username=eric&password=s3cret")
        assert st == 429 and "set-cookie" not in {k.lower() for k in hdrs}
    finally:
        srv.stop()


# ── gate OFF (backward compatibility) ────────────────────────────────────────

def test_open_mode_unchanged():
    mod = _load_server("sbserver_open", "[]")
    assert mod.AUTH_REQUIRED is False
    srv = _Server(mod)
    try:
        # No login anywhere: "/" is the terminal page, exactly as before v0.44.0.
        st, _, body = srv.req("GET", "/")
        assert st == 200 and b"xterm" in body
        assert srv.req("GET", "/healthz")[0] == 200
        # /static/index.html stays 404 in both modes (one door, not two).
        assert srv.req("GET", "/static/index.html")[0] == 404
        # POST /login is not a route when the gate is off.
        assert srv.req("POST", "/login", "username=x&password=y")[0] == 405
    finally:
        srv.stop()


def test_users_with_blank_fields_disable_the_gate():
    # parse_users drops rows lacking a username or password; the server must
    # then run OPEN — and the run script derives its boot log from this same
    # parser so it can never claim "gate ACTIVE" over an open terminal.
    mod = _load_server("sbserver_blank", '[{"username":"","password":"pw"}]')
    assert mod.AUTH_REQUIRED is False
