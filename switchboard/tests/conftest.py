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
