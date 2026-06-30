"""Persistent message-waiting (MWI) flags — which rooms have a "you have a
message, call the operator" stutter-tone indicator set.

The actual signal is Asterisk's MWI (ami.set_mwi -> the FXS gateway renders a
stutter dial tone). This little store is the UI source of truth: it lets the TUI
and web dashboard show a ✉ badge, survives add-on restarts (Asterisk's MWI state
is in-memory and resets on restart, so an init step replays this store), and is
updated in lock-step with Asterisk by the `switchboard-mwi` CLI.

Same shape/locking as wakeup/store.py: a flock-serialized, atomically-written
JSON object at /data/mwi.json mapping ext -> {set_at}. Pure-ish + stdlib only.
"""

from __future__ import annotations

import json
import os
import tempfile

try:
    import fcntl  # POSIX only; the add-on runs on Linux
except ImportError:  # pragma: no cover
    fcntl = None

# /data/state is owned by the asterisk user (see switchboard-config ensure_state_dir),
# so the dialplan's System(switchboard-mwi clear ...) — run as that user — can write
# the lock + temp files here. (Directly in /data only root could write → EPERM.)
PATH = os.environ.get("SWITCHBOARD_MWI", "/data/state/mwi.json")


class _Lock:
    def __init__(self, path):
        self.path = path + ".lock"
        self.fh = None

    def __enter__(self):
        if fcntl is None:
            return self
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.fh = open(self.path, "a+")
        # Group-writable so the root webui and the asterisk-user switchboard-mwi
        # (dialplan System()) can both open the lock. Best-effort (owner-only chmod).
        try:
            os.chmod(self.path, 0o664)
        except OSError:
            pass
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()


def _read() -> dict:
    try:
        with open(PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
    d = os.path.dirname(PATH) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".mwi-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, PATH)
        # Widen mkstemp's 0600 so the other writer (root webui vs asterisk-user
        # switchboard-mwi, same group via the setgid /data/state dir) can rewrite.
        try:
            os.chmod(PATH, 0o664)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def all_flags() -> dict:
    with _Lock(PATH):
        return _read()


def is_set(ext: str) -> bool:
    with _Lock(PATH):
        return str(ext) in _read()


def set_flag(ext: str, on: bool) -> bool:
    """Set (on=True) or clear (on=False) the MWI flag for a room. Returns the new
    state (True=set). Idempotent."""
    import time
    with _Lock(PATH):
        data = _read()
        key = str(ext)
        if on:
            if key not in data:
                data[key] = {"set_at": int(time.time())}
                _write(data)
            return True
        if key in data:
            del data[key]
            _write(data)
        return False


def exts() -> list[str]:
    """Sorted list of rooms with an MWI flag set."""
    with _Lock(PATH):
        return sorted(_read().keys())
