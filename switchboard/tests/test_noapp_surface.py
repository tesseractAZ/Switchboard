"""Every `@app.<name>` used in app.py must exist on the no-FastAPI stand-in.

    python3 -m pytest switchboard/tests/test_noapp_surface.py

★ THIS FILE MUST NEVER IMPORT app.py. That is the whole point.

`webui/app.py` builds a `_NoApp()` shim when FastAPI is absent — the case on the
box these tests run on — so its route functions still define and its pure
helpers stay testable. The shim aliases a handful of names to an identity
decorator. Add a route with a decorator the shim does not have, and the module
raises AttributeError AT IMPORT.

That is not a one-test failure. Every test file that imports app.py fails during
COLLECTION, and pytest aborts the session rather than running the rest, so a
one-character mistake reads as "the entire suite is broken" with no indication
of which line did it. Adding `@app.websocket` for the Ingress console was
exactly that mistake waiting to happen.

So this file reads app.py as TEXT and compares the decorators it uses against
the names the shim provides. It survives the very breakage it is diagnosing.

Do NOT "fix" `_NoApp` with a catch-all `__getattr__`. That would make a typo
like `@app.gett` import cleanly here and fail only inside the container.
"""
import re
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
       / "switchboard" / "webui" / "app.py")


def _decorators_used():
    """Names used as `@app.<name>` anywhere in app.py."""
    return set(re.findall(r"^@app\.(\w+)", APP.read_text(), re.M))


def _names_on_the_shim():
    """Names the _NoApp class binds — both `a = b = _decorator` aliases and
    ordinary `def`s inside the class body."""
    src = APP.read_text()
    body = src[src.index("class _NoApp:"):src.index("\napp = ")]
    names = set(re.findall(r"^\s{4}def (\w+)", body, re.M))
    for line in body.split("\n"):
        m = re.match(r"^\s{4}((?:\w+\s*=\s*)+)_decorator\s*$", line)
        if m:
            names |= {n.strip() for n in m.group(1).split("=") if n.strip()}
    return names


def test_every_app_decorator_exists_on_the_shim():
    used, have = _decorators_used(), _names_on_the_shim()
    missing = sorted(used - have)
    assert not missing, (
        f"app.py uses @app.{{{','.join(missing)}}} but _NoApp does not provide "
        f"them. On a box without FastAPI this is an AttributeError at import, "
        f"which aborts pytest COLLECTION for the whole suite. Add the name to "
        f"the `get = post = ... = _decorator` alias line.")


def test_the_scan_actually_finds_things():
    """A guard that silently matches nothing always passes."""
    used, have = _decorators_used(), _names_on_the_shim()
    assert {"get", "post"} <= used, f"decorator scan found only {used}"
    assert {"get", "post", "middleware"} <= have, f"shim scan found only {have}"
    assert "websocket" in used, "the Ingress console route is missing from app.py"
    assert "add_middleware" in have, (
        "_NoApp needs add_middleware — the Ingress guard registers through it")


def test_a_planted_decorator_is_detected():
    """Prove the comparison discriminates, rather than trusting its silence."""
    have = _names_on_the_shim()
    assert "totally_not_a_real_method" not in have
    missing = sorted({"get", "totally_not_a_real_method"} - have)
    assert missing == ["totally_not_a_real_method"]
