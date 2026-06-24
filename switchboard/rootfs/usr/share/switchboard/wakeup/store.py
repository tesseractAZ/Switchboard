"""Persistent wake-up store, shared by the setter (AGI), the scheduler service,
and the read-only UIs (console / dashboard).

One pending wake-up per room (setting a new one replaces it). Stored in
/data/wakeups.json so it survives restarts. Access is flock-serialized (the AGI
writes while the scheduler reads/removes) and writes are atomic (temp + replace).

The target is stored as an absolute epoch (the next occurrence of HH:MM in local
time) so a brief outage can't make the scheduler fire a stale wake-up — past the
grace window it's treated as missed, not fired late at the wrong hour.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile

try:
    import fcntl  # POSIX only; the add-on runs on Linux
except ImportError:  # pragma: no cover
    fcntl = None

PATH = os.environ.get("SWITCHBOARD_WAKEUPS", "/data/wakeups.json")
GRACE_SECONDS = 600  # fire a due wake-up only within 10 min of its time


class _Lock:
    def __init__(self, path):
        self.path = path + ".lock"
        self.fh = None

    def __enter__(self):
        if fcntl is None:
            return self
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.fh = open(self.path, "a+")
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
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".wakeups-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def next_epoch(hhmm: str, now_epoch: float | None = None) -> int:
    """Epoch of the next local-time occurrence of HH:MM (today if still ahead,
    else tomorrow). Naive local datetime is correct here because the container's
    /etc/localtime is set to the configured zone."""
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime.datetime.now() if now_epoch is None else datetime.datetime.fromtimestamp(now_epoch)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return int(target.timestamp())


# --------------------------------------------------------------------------- #
# Public API (each call takes the lock).
# --------------------------------------------------------------------------- #
def all_wakeups() -> dict:
    with _Lock(PATH):
        return _read()


def get(ext: str):
    with _Lock(PATH):
        return _read().get(str(ext))


def set_wakeup(ext: str, hhmm: str, now_epoch: float | None = None) -> dict:
    entry = {
        "hhmm": hhmm,
        "target_epoch": next_epoch(hhmm, now_epoch),
        "set_at": int(now_epoch if now_epoch is not None else datetime.datetime.now().timestamp()),
    }
    with _Lock(PATH):
        data = _read()
        data[str(ext)] = entry
        _write(data)
    return entry


def cancel(ext: str) -> bool:
    with _Lock(PATH):
        data = _read()
        if str(ext) in data:
            del data[str(ext)]
            _write(data)
            return True
        return False


def cancel_if(ext: str, target_epoch: int) -> bool:
    """Cancel only if the stored wake-up still has this exact target_epoch — so
    the scheduler, removing a wake-up it just fired, can't delete a *different*
    wake-up the caller re-set for the same room in the meantime."""
    with _Lock(PATH):
        data = _read()
        e = data.get(str(ext))
        if e and e.get("target_epoch") == target_epoch:
            del data[str(ext)]
            _write(data)
            return True
        return False


def due(now_epoch: float, grace: int = GRACE_SECONDS):
    """Return [(ext, entry)] for wake-ups whose time has arrived and is within
    the grace window. Removes silently-missed (older than grace) entries."""
    fired, missed = [], []
    with _Lock(PATH):
        data = _read()
        changed = False
        for ext, entry in list(data.items()):
            tgt = entry.get("target_epoch", 0)
            if now_epoch >= tgt:
                if now_epoch <= tgt + grace:
                    fired.append((ext, entry))
                else:
                    missed.append((ext, entry))
                    del data[ext]
                    changed = True
        if changed:
            _write(data)
    return fired, missed
