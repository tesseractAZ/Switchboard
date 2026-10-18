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
    """Two dumb devices genuinely need these and cannot ride Ingress: the WP826
    fetching its phonebook, and an HA media player fetching a SPEAKER clip
    (`a<digits>.wav`, written by the switchboard-announce AGI)."""
    assert app._scope_allowed(_http("192.168.1.50", "/phonebook.xml")) is True
    assert app._scope_allowed(_http("192.168.1.50", "/announce/a1234567890.wav")) is True


def test_handset_clips_are_not_fetchable_from_the_lan():
    """v0.106.0 — the handset clips (`ann-<ext>-<32hex>.wav`) are played by
    Asterisk from disk and never fetched over the LAN, and their names are in the
    shared-folder ledger: exempting them let anything that could read /share
    download what was said. Only the speaker-clip form is exempt."""
    for p in ("/announce/ann-19-" + "0123456789abcdef" * 2 + ".wav",
              "/announce/x.wav", "/announce/a12.wav/extra", "/announce/a.wav",
              "/announce/A123.wav", "/announce/a123.WAV"):
        assert app._scope_allowed(_http("192.168.1.50", p)) is False, p


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


def test_the_console_asset_route_never_builds_a_path_from_the_request():
    """★ CodeQL flagged the first version of this route as py/path-injection.

    It was safe by reasoning — the name had to be a key in a two-entry dict
    before it was used — but it still did `FileResponse(f"{DIR}/{name}")`, so
    the safety rested on a membership test and an interpolation staying in sync,
    and no analyser can see a dict lookup as a sanitizer. The table now stores
    the resolved path, so the request parameter never reaches a file operation.
    """
    import re
    src = (WEBUI / "app.py").read_text()
    body = src[src.index("def console_asset"):]
    body = body[:body.index("\n@app.")]
    assert "FileResponse(path," in body, "the served path must come from the table"
    assert not re.search(r"FileResponse\(f?\"", body), (
        "console_asset builds a path literal inline — if that interpolates the "
        "route parameter it is py/path-injection again")
    assert "{name}" not in body.replace('@app.get("/console/static/{name}")', ""), (
        "the route parameter is interpolated somewhere in the handler")
    # ...and the allowlist is still exactly the two vendored assets.
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_CONSOLE_ASSETS" for t in node.targets):
            keys = [k.value for k in node.value.keys]
            assert keys == ["xterm.js", "xterm.css"], keys
            break
    else:
        raise AssertionError("_CONSOLE_ASSETS not found")


def test_the_font_route_never_builds_a_path_from_the_request():
    """The dashboard's font route has the console asset route's shape, for the
    same CodeQL reason: the table stores the resolved path, so the request
    parameter never reaches a file operation, and the table is exactly the two
    shipped faces under the shared fonts directory."""
    import ast
    import re
    src = (WEBUI / "app.py").read_text()
    body = src[src.index("def font_asset"):]
    body = body[:body.index("\n\n\n")]
    assert "FileResponse(path," in body, "the served path must come from the table"
    assert not re.search(r"FileResponse\(f?\"", body), (
        "font_asset builds a path literal inline — if that interpolates the route "
        "parameter it is py/path-injection")
    assert "{name}" not in body, "the route parameter is interpolated in the handler"
    assert '@app.get("/fonts/{name}")\ndef font_asset' in src, "the route moved or was renamed"
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_FONT_ASSETS" for t in node.targets):
            keys = [k.value for k in node.value.keys]
            assert keys == ["Federo-Regular.ttf", "CutiveMono-Regular.ttf"], keys
            break
    else:
        raise AssertionError("_FONT_ASSETS not found")
    assert app.FONTS_DIR == "/usr/share/switchboard/fonts"
    assert app._FONT_ASSETS == {n: f"{app.FONTS_DIR}/{n}" for n in app._FONT_ASSETS}


def test_the_fonts_are_ingress_only():
    """No LAN exemption: the faces are asked for by a page that is itself
    ingress-only, so a LAN client gets the same refusal as for the page, and a
    traversal-shaped path is refused before routing ever sees it."""
    for name in ("Federo-Regular.ttf", "CutiveMono-Regular.ttf", "../app.py", "..%2Fapp.py"):
        assert app._scope_allowed(_http("192.168.1.50", "/fonts/" + name)) is False, name
        assert app._scope_allowed(_http(SUPERVISOR, "/fonts/" + name)) is True, name
    assert app._scope_allowed(_ws("192.168.1.50", "/fonts/Federo-Regular.ttf")) is False
    src = (WEBUI / "app.py").read_text()
    body = src[src.index("def _refusal_reason"):src.index("class RestrictToIngress")]
    assert "/fonts" not in body, (
        "_refusal_reason special-cases the fonts; they must stay behind the guard")


# --------------------------------------------------------------------------- #
# A refusal leaves one line, and a flood does not leave a flood (2026-10-14).
#
# Until this release the guard wrote its 403 and nothing else, and uvicorn runs
# with --no-access-log, so a LAN probe or a wrong announce token tried a thousand
# times left no trace at all. The fix had to be rate-limited: journald limits
# docker.service as a whole, and one looping host must not be able to push every
# other container's lines past that limit.
# --------------------------------------------------------------------------- #
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402


def _reset_refusal_log():
    with app._refusal_lock:
        app._refusals.clear()
        app._refusals_overflow[:] = [0.0, 0]


def _drive(scope):
    """Run one scope through the real middleware.

    Returns (messages sent, lines printed, whether the wrapped app was reached)."""
    sent, reached = [], []

    async def inner(scope, receive, send):
        reached.append(scope)

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(app.RestrictToIngress(inner)(scope, receive, send))
    return sent, out.getvalue().splitlines(), bool(reached)


def _printed(fn, *a, **k):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*a, **k)
    return result, out.getvalue().splitlines()


def test_a_refused_request_leaves_exactly_one_line():
    _reset_refusal_log()
    try:
        sent, lines, reached = _drive(_http("192.168.1.50", "/api/status"))
        assert not reached
        assert sent[0]["status"] == 403
        assert sent[1]["body"] == b'{"error":"forbidden"}', "the client is told nothing more"
        assert len(lines) == 1, lines
        line = lines[0]
        assert line.startswith(
            "[switchboard-webui] refused GET /api/status from 192.168.1.50: "), line
        assert app.REFUSED_NOT_INGRESS in line, line
        # ...and an admitted request writes nothing: this is not an access log.
        _, lines, reached = _drive(_http(SUPERVISOR, "/api/status"))
        assert reached and lines == []
        _, lines, reached = _drive(_http("192.168.1.51", "/announce/a1234567890.wav"))
        assert reached and lines == [], "an exempt LAN fetch is not a refusal"
    finally:
        _reset_refusal_log()


def test_an_ipv4_mapped_address_is_logged_as_the_address():
    _reset_refusal_log()
    try:
        _, lines, _ = _drive(_http("::ffff:192.168.1.50", "/api/status"))
        assert len(lines) == 1 and " from 192.168.1.50: " in lines[0], lines
    finally:
        _reset_refusal_log()


def test_a_live_clip_name_never_reaches_the_log():
    """Only a GET is exempt, so a HEAD (or anything else) for a live speaker clip
    is refused and logged — and the clip's name is the one credential a LAN fetch
    needs. Every clip-shaped name in a logged path is written as a placeholder."""
    name = "a" + "09182736450918273645" + ".wav"
    digits = name[1:-4]
    paths = [
        ("HEAD", f"/announce/{name}"),
        ("POST", f"/announce/{name}"),
        ("GET", f"/announce/{name}/"),              # not a fullmatch: refused
        ("GET", f"//announce/{name}"),
        # A clip name straddling the truncation point: cut first, ten of its
        # digits would survive. It is hidden first, so none do.
        ("GET", "/" + "x" * (app._REFUSAL_LOG_PATH_MAX - 22) + f"/announce/{name}"),
    ]
    try:
        for i, (method, path) in enumerate(paths):
            _reset_refusal_log()
            host = f"192.168.1.{80 + i}"
            sent, lines, reached = _drive(_http(host, path, method))
            assert not reached and sent[0]["status"] == 403, (method, path)
            assert len(lines) == 1, lines
            line = lines[0]
            assert f"from {host}: " in line, line
            # No run of the clip's digits long enough to narrow a guess survives.
            for k in range(len(digits) - 5):
                assert digits[k:k + 6] not in line, (method, path, line)
            if not path.startswith("/x"):
                assert "a<digits>.wav" in line, line
        # The method and the rest of the path are still there to read.
        _reset_refusal_log()
        _, lines, _ = _drive(_http("192.168.1.90", f"/announce/{name}", "HEAD"))
        assert lines[0].startswith(
            "[switchboard-webui] refused HEAD /announce/a<digits>.wav from 192.168.1.90: "), lines
        # The exempt GET itself is still admitted, and still silent.
        _, lines, reached = _drive(_http("192.168.1.91", f"/announce/{name}"))
        assert reached and lines == []
    finally:
        _reset_refusal_log()


def test_repeats_from_one_address_are_counted_not_logged():
    """The first refusal per address is a line; the next ten minutes of them are
    one summary line; after that the address is logged afresh."""
    _reset_refusal_log()
    host, t0 = "192.168.1.50", 1000.0
    results, lines = [], []
    for i in range(50):
        r, out = _printed(app._log_refusal, host, "GET", "/api/status",
                          app.REFUSED_NOT_INGRESS, now=t0 + i)
        results.append(r)
        lines += out
    assert results.count(True) == 1 and results[0] is True, results
    assert len(lines) == 1, lines
    # Another address is not hidden behind the first one's window.
    r, out = _printed(app._log_refusal, "192.168.1.60", "GET", "/", app.REFUSED_NOT_INGRESS,
                      now=t0 + 60)
    assert r is True and len(out) == 1 and "192.168.1.60" in out[0], out
    # Still inside the window: nothing to summarise yet.
    _, out = _printed(app._flush_refusal_summaries, now=t0 + 599)
    assert out == [], out
    # The window closes: one summary line carrying the count, then silence.
    _, out = _printed(app._flush_refusal_summaries, now=t0 + app._REFUSAL_LOG_WINDOW_S)
    assert len(out) == 1, out
    assert "refused 49 more request(s) from 192.168.1.50" in out[0], out
    _, out = _printed(app._flush_refusal_summaries, now=t0 + app._REFUSAL_LOG_WINDOW_S + 1)
    assert out == [], "a summary is written once"
    # The second address was refused only once, so it has nothing to summarise...
    _, out = _printed(app._flush_refusal_summaries, now=t0 + 60 + app._REFUSAL_LOG_WINDOW_S)
    assert out == [], out
    # ...and the first, refused again after its window, is logged in full again.
    r, out = _printed(app._log_refusal, host, "POST", "/api/ring/11", app.REFUSED_NOT_INGRESS,
                      now=t0 + 2 * app._REFUSAL_LOG_WINDOW_S)
    assert r is True and len(out) == 1 and "POST /api/ring/11" in out[0], out
    _reset_refusal_log()


def test_a_burst_through_the_middleware_is_one_line():
    _reset_refusal_log()
    total = []
    for _ in range(25):
        sent, lines, _ = _drive(_http("192.168.1.50", "/api/status"))
        assert sent[0]["status"] == 403, "rate limiting the LOG must never admit anything"
        total += lines
    assert len(total) == 1, total
    _reset_refusal_log()


def test_addresses_past_the_table_share_one_counter():
    """A scan from many addresses costs a bounded number of lines too."""
    _reset_refusal_log()
    saved = app._REFUSAL_LOG_HOSTS
    try:
        app._REFUSAL_LOG_HOSTS = 4
        lines = []
        for i in range(14):
            _, out = _printed(app._log_refusal, f"192.168.1.{100 + i}", "GET", "/",
                              app.REFUSED_NOT_INGRESS, now=1000.0)
            lines += out
        assert len(lines) == 4, lines
        assert len(app._refusals) == 4, "the table grew past its bound"
        _, out = _printed(app._flush_refusal_summaries, now=1000.0 + app._REFUSAL_LOG_WINDOW_S)
        assert len(out) == 1, out
        assert "refused 10 request(s) from addresses past the first 4" in out[0], out
        assert app._refusals == {} and app._refusals_overflow == [0.0, 0]
    finally:
        app._REFUSAL_LOG_HOSTS = saved
        _reset_refusal_log()


def test_the_announce_token_never_reaches_the_log():
    """The reason is named; the token, configured or presented, never is."""
    configured = "tok-0123456789abcdef"
    guess = "tok-0123456789abcdeX"
    saved = app._announce_token
    _reset_refusal_log()
    try:
        app._announce_token = lambda: configured
        path = "/api/announce/19"
        cases = [
            ("192.168.1.71", [("x-announce-token", guess)], app.REFUSED_HEADER_WRONG),
            ("192.168.1.72", [], app.REFUSED_HEADER_MISSING),
        ]
        for host, headers, reason in cases:
            sent, lines, reached = _drive(_http(host, path, "POST", headers))
            assert not reached and sent[0]["status"] == 403
            assert len(lines) == 1 and lines[0].endswith(
                f"from {host}: {reason} (further refusals from this address for "
                f"this reason are counted for 10 min)"), lines
            for secret in (configured, guess, "0123456789abcde", "tok-"):
                assert secret not in lines[0], (secret, lines[0])
        # The right token from the LAN is admitted, and admission writes nothing.
        _, lines, reached = _drive(_http("192.168.1.73", path, "POST",
                                         [("x-announce-token", configured)]))
        assert reached and lines == []
        # With no token configured the LAN announce path is off, and says so.
        app._announce_token = lambda: ""
        _, lines, reached = _drive(_http("192.168.1.74", path, "POST",
                                         [("x-announce-token", guess)]))
        assert not reached and len(lines) == 1, lines
        assert app.REFUSED_LAN_ANNOUNCE_OFF in lines[0] and guess not in lines[0], lines
    finally:
        app._announce_token = saved
        _reset_refusal_log()


def test_a_new_reason_from_the_same_address_gets_its_own_line():
    """A missing token, then a mistyped one, from one address: the second reason
    is named on a line of its own instead of being counted under the first, and
    the summary says which reason it counted."""
    _reset_refusal_log()
    try:
        _, out = _printed(app._log_refusal, "192.168.1.80", "POST", "/api/announce/19",
                          app.REFUSED_HEADER_MISSING, now=1000.0)
        assert len(out) == 1 and app.REFUSED_HEADER_MISSING in out[0], out
        _, out = _printed(app._log_refusal, "192.168.1.80", "POST", "/api/announce/19",
                          app.REFUSED_HEADER_WRONG, now=1001.0)
        assert len(out) == 1 and app.REFUSED_HEADER_WRONG in out[0], out
        _, out = _printed(app._log_refusal, "192.168.1.80", "POST", "/api/announce/19",
                          app.REFUSED_HEADER_WRONG, now=1002.0)
        assert out == [], out
        _, out = _printed(app._flush_refusal_summaries, now=1001.0 + app._REFUSAL_LOG_WINDOW_S)
        assert len(out) == 1, out
        assert f"refused 1 more request(s) from 192.168.1.80 ({app.REFUSED_HEADER_WRONG})" in out[0], out
    finally:
        _reset_refusal_log()


def test_a_window_still_open_at_exit_is_summarised():
    """The housekeeping tick closes only windows that have run their 10 minutes;
    the exit flush closes every one, so a restart does not lose the count."""
    _reset_refusal_log()
    try:
        for i in range(4):
            _printed(app._log_refusal, "192.168.1.81", "GET", "/", "not from ingress",
                     now=2000.0 + i)
        _, out = _printed(app._flush_refusal_summaries, now=2100.0)
        assert out == [], "the tick must not close an open window"
        _, out = _printed(app._flush_refusal_summaries, now=2100.0, force=True)
        assert len(out) == 1 and "refused 3 more request(s) from 192.168.1.81" in out[0] \
            and "(stopping)" in out[0], out
        assert app._refusals == {}
    finally:
        _reset_refusal_log()


def test_the_announce_token_is_compared_in_constant_time():
    """`==` returns at the first differing character, so its timing says how much
    of a guess was right. console-web/webauth.py already used compare_digest."""
    import ast
    import inspect
    body = inspect.getsource(app._refusal_reason)
    assert "_token_matches(sent, tok)" in body
    assert not re.search(r"==\s*tok\b|\btok\s*==", body), "a plain == on the token is back"
    tree = ast.parse(inspect.getsource(app._token_matches))
    calls = {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)}
    assert "hmac.compare_digest" in calls, calls
    assert app._token_matches("same", "same") is True
    assert app._token_matches("same", "samf") is False
    assert app._token_matches("short", "longer-token") is False
    # A token the str form of compare_digest would raise TypeError on.
    assert app._token_matches("tökén", "tökén") is True
    assert app._token_matches("tökén", "token") is False


def test_a_logged_path_cannot_forge_a_line():
    """scope["path"] is percent-decoded, so /%0a arrives as a real newline."""
    _reset_refusal_log()
    try:
        forged = "/x\n[switchboard-webui] web: test ring to 11 queued\r" + "A" * 500
        _, lines, _ = _drive(_http("192.168.1.50", forged))
        assert len(lines) == 1, lines
        line = lines[0]
        assert "\r" not in line
        assert "/x?[switchboard-webui]?web:?test?ring" in line, line
        assert "A" * (app._REFUSAL_LOG_PATH_MAX + 1) not in line, line
        assert "... from 192.168.1.50" in line, line
        _reset_refusal_log()
        _, lines, _ = _drive(_http("192.168.1.50", "/\x1b[2J‮gnp.x"))
        assert len(lines) == 1 and "\x1b" not in lines[0] and "‮" not in lines[0], lines
        # A space would let a path carry a " from <address>" of its own.
        _reset_refusal_log()
        _, lines, _ = _drive(_http("192.168.1.50", "/x from 192.0.2.1: ok"))
        assert " from 192.0.2.1" not in lines[0] and "/x?from?192.0.2.1:?ok" in lines[0], lines
    finally:
        _reset_refusal_log()


def test_a_refused_websocket_adds_no_line_of_its_own():
    """uvicorn already writes '<client> - "WebSocket <path>" 403' for a handshake
    closed before accept; a second line from the guard would double it."""
    _reset_refusal_log()
    try:
        sent, lines, reached = _drive(_ws("192.168.1.50"))
        assert not reached
        assert sent == [{"type": "websocket.close", "code": 1008}], sent
        assert lines == [], lines
        assert app._refusals == {}, "a WebSocket refusal must not open a window"
    finally:
        _reset_refusal_log()


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
