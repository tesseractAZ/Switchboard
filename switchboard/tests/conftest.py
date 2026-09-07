"""Shared test bootstrap: point the state stores at a temp dir BEFORE any
product module is imported.

wakeup/store.py and webui/mwi_store.py resolve their PATH from
SWITCHBOARD_WAKEUPS / SWITCHBOARD_MWI **at import time**, defaulting to
/data/state/... — a path that only exists inside the add-on container (and is
read-only or absent on a dev machine). Because Python caches modules, whichever
test file imports a store first fixes its PATH for the whole session: without
this conftest, test_app.py (imports the webui app, no env override) preceded
test_console.py alphabetically and poisoned the cache with /data, so the
console tests' own env overrides were silently ignored and every store write
died with EROFS on macOS — but only in full-suite order, never in isolation.

conftest.py is imported by pytest before any test module, so setting the env
here guarantees every first-import resolves to a writable temp path regardless
of test order. Individual files keep their own os.environ.setdefault(...) lines
as a fallback for direct (non-pytest) imports; setdefault means they defer to
the values set here.
"""

import os
import tempfile

_STATE_DIR = tempfile.mkdtemp(prefix="switchboard-test-state-")
os.environ.setdefault("SWITCHBOARD_WAKEUPS", os.path.join(_STATE_DIR, "wakeups.json"))
os.environ.setdefault("SWITCHBOARD_MWI", os.path.join(_STATE_DIR, "mwi.json"))


# --------------------------------------------------------------------------- #
# Tripwire: a test must not leave an add-on source directory on sys.path.
#
# Several tests load modules that live under rootfs/usr/share/switchboard and
# import their siblings by BARE name, which necessarily puts a directory on
# sys.path. Leaving it there is how a later `import consoleproto` — or, before
# v0.92.0, `import wsproto` — silently resolves to a copy some earlier test
# cached. That is the exact class of defect v0.92.0 removed, and the harness
# should catch the next one at the LEAKING test rather than at an unrelated one
# much later.
# --------------------------------------------------------------------------- #
import sys  # noqa: E402

import pytest  # noqa: E402

_ADDON_SRC = "rootfs/usr/share/switchboard"
# test_ingress_guard.py inserts webui/ at module scope and keeps it for the
# whole session by design; anything else is a leak.
_PERMITTED = ("/webui",)


@pytest.fixture(autouse=True)
def _no_leaked_sys_path():
    before = list(sys.path)
    yield
    leaked = [p for p in sys.path
              if p not in before
              and _ADDON_SRC in p.replace("\\", "/")
              and not p.replace("\\", "/").endswith(_PERMITTED)]
    assert not leaked, (
        "this test left an add-on source directory on sys.path:\n  "
        + "\n  ".join(leaked)
        + "\nA later `import <sibling>` anywhere in the session can now resolve "
          "to this test's copy. Snapshot and restore sys.path (and sys.modules "
          "for any bare names) in the loader that inserted it.")
