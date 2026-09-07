"""The Ingress guard, driven at the ASGI scope level.

    python3 -m pytest switchboard/tests/test_ingress_guard.py

★ THE DEFECT. Until 0.93.0 this guard was `@app.middleware("http")` — Starlette's
BaseHTTPMiddleware, which returns early for any scope whose type is not "http".
That was correct while every route was HTTP, and became a hole the instant a
WebSocket route was added: the new route inherited NO guard at all.

The consequence was not theoretical. This add-on runs `host_network: true` with
`--host 0.0.0.0`, so :8099 answers on the LAN; the guard is the only thing that
makes it Supervisor-only. A WebSocket carrying a terminal onto the operator
console would therefore have been reachable, unauthenticated, from any machine
on the network — on the ONE port that cannot be turned off, because it is the
Ingress panel itself. That is strictly worse than the :8100 door the change
exists to close.

`_scope_allowed` is pure so this file can drive it with hand-built scopes and no
web framework.
"""
import sys
from pathlib import Path

WEBUI = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
         / "switchboard" / "webui")
sys.path.insert(0, str(WEBUI))
import app  # noqa: E402

SUPERVISOR = "172.30.32.2"


def _http(client, path="/", method="GET", headers=()):
    return {"type": "http", "client": (client, 40000) if client else None,
            "path": path, "method": method,
            "headers": [(k.encode(), v.encode()) for k, v in headers]}


def _ws(client, path="/console/ws", headers=()):
    return {"type": "websocket", "client": (client, 40000) if client else None,
            "path": path,
            "headers": [(k.encode(), v.encode()) for k, v in headers]}


# --------------------------------------------------------------------------- #
# The hole itself.
# --------------------------------------------------------------------------- #
def test_a_websocket_from_the_lan_is_refused():
    """★ The whole reason the guard moved to the ASGI layer."""
    assert app._scope_allowed(_ws("192.168.1.50")) is False


def test_a_forged_ingress_header_does_not_help():
    """★ The zwave lesson, entered as a case rather than assumed.

    That add-on's ingress check ANDed a header test with a subnet test over
    172.30.32.0/23 — the network every sibling add-on container lives on. The
    subnet term was therefore always true, and the expression collapsed to "did
    the client send a header it chose to send". `X-Ingress-Path` is forgeable by
    anything that can reach the port; it must never be sufficient.
    """
    assert app._scope_allowed(
        _ws("192.168.1.50", headers=[("x-ingress-path", "/api/hassio_ingress/abc")])) is False
    assert app._scope_allowed(
        _http("192.168.1.50", headers=[("x-ingress-path", "/api/hassio_ingress/abc")])) is False


def test_the_supervisor_is_allowed_on_both_transports():
    """A guard that only ever refuses is indistinguishable from a dead console."""
    assert app._scope_allowed(_http(SUPERVISOR)) is True
    assert app._scope_allowed(_ws(SUPERVISOR)) is True


def test_an_ipv4_mapped_supervisor_address_is_recognised():
    """uvicorn reports `::ffff:172.30.32.2` on a dual-stack listener. Without
    normalisation the panel simply stops working."""
    assert app._scope_allowed(_ws("::ffff:" + SUPERVISOR)) is True
    assert app._scope_allowed(_http("::ffff:" + SUPERVISOR)) is True


def test_a_missing_client_is_denied():
    """`scope["client"]` is None for a UNIX socket or an in-process test client.
    Absence of an identity is not an identity."""
    assert app._scope_allowed(_http(None)) is False
    assert app._scope_allowed(_ws(None)) is False


def test_lifespan_passes_through():
    """Denying the lifespan scope prevents the app from starting at all."""
    assert app._scope_allowed({"type": "lifespan"}) is True


# --------------------------------------------------------------------------- #
# The LAN exemptions are HTTP-only, and that ordering is load-bearing.
# --------------------------------------------------------------------------- #
def test_the_lan_exemptions_still_work_over_http():
    """Two dumb devices genuinely need these and cannot ride Ingress."""
    assert app._scope_allowed(_http("192.168.1.50", "/phonebook.xml")) is True
    assert app._scope_allowed(_http("192.168.1.50", "/announce/x.wav")) is True


def test_the_exemptions_do_not_leak_onto_websockets():
    """★ The trap. A websocket scope has NO "method" key.

    Reaching for `scope.get("method", "GET")` to reuse the HTTP branch would
    make the /announce/ and /phonebook.xml prefix tests apply to WebSocket
    upgrades too — so `ws://<pi>:8099/phonebook.xml` from the LAN would be
    exempted outright, reopening the hole one layer down.
    """
    assert app._scope_allowed(_ws("192.168.1.50", "/phonebook.xml")) is False
    assert app._scope_allowed(_ws("192.168.1.50", "/announce/x.wav")) is False


def test_a_write_method_on_an_exempt_path_is_not_exempt():
    assert app._scope_allowed(_http("192.168.1.50", "/phonebook.xml", "POST")) is False
    assert app._scope_allowed(_http("192.168.1.50", "/announce/x.wav", "DELETE")) is False


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #
def test_the_guard_is_a_pure_asgi_middleware_not_an_http_one():
    """`@app.middleware("http")` is precisely what did not cover WebSockets.
    Re-introducing that decorator anywhere in app.py reopens the hole."""
    # Match a DECORATOR at line start, not the string anywhere. app.py's own
    # docstring explains the defect by naming it, and a substring test flagged
    # that prose — the same self-match that made test_privacy_invariants pass
    # locally and fail in CI. A scanner that matches its own explanation is a
    # self-inflicted false positive; the fix is anchoring, not softening.
    import re
    src = (WEBUI / "app.py").read_text()
    assert not re.search(r'^@app\.middleware\(', src, re.M), (
        "an http-only middleware is registered again — it will not see "
        "WebSocket scopes")
    assert "app.add_middleware(RestrictToIngress)" in src, (
        "the ASGI guard is defined but never registered")


def test_the_websocket_route_is_not_exempted_anywhere():
    """The console route must be subject to the same guard as everything else —
    it has no bypass of its own."""
    src = (WEBUI / "app.py").read_text()
    body = src[src.index("def _scope_allowed"):src.index("class RestrictToIngress")]
    assert "/console" not in body, (
        "_scope_allowed special-cases /console; the console must not be able to "
        "opt itself out of the guard that protects it")
