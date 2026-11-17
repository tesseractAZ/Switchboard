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
import errno
import json
import os
import stat
import tempfile

try:
    import fcntl  # POSIX only; the add-on runs on Linux
except ImportError:  # pragma: no cover
    fcntl = None

# /data/state is owned by the asterisk user (see switchboard-config ensure_state_dir),
# so the dial-42 wake-up AGI — which Asterisk runs as that user — can write the lock
# + atomic-rename temp files here. (Directly in /data only root could write → EPERM.)
PATH = os.environ.get("SWITCHBOARD_WAKEUPS", "/data/state/wakeups.json")
GRACE_SECONDS = 600  # fire a due wake-up only within 10 min of its time


def _open_no_follow(path: str, flags: int) -> int:
    """An fd for the REGULAR file at `path`, never through a symlink. Raises OSError.

    A copy of rtpmon/poller.py's helper, carried by wakeup/store.py and
    webui/mwi_store.py alike. ★ THE SAME DIRECTORY, TWO USERS. /data/state is
    owned by `asterisk`, group-writable and not sticky, and root services (the
    wake-up scheduler, the web UI, the operator console, the boot-time MWI
    replay) use both stores, so any name in it may be a link the asterisk user
    planted. By name, the lock's open and chmod, the read, and the chmod after a
    write all followed such a link onto its target; the lock's chmod made
    whatever it pointed at 0664. O_NOFOLLOW makes a link fail to open; the
    fstat refuses a FIFO, a device or a directory; O_NONBLOCK keeps a planted
    FIFO from hanging the open.
    """
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o664)  # intentional: root services AND the asterisk-user AGIs share this file; group access is required
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path}: not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_lock(path: str) -> int:
    """The lock file's fd: a regular file at `path`, opened read-write, created
    if missing, and never reached through a link. Raises OSError to refuse the
    store call. (wakeup/store.py and webui/mwi_store.py carry the same copy;
    tests/test_state_dir_links.py holds the two to the same source.)

    The open fails with ELOOP when `path` is a symlink, with EISDIR when it is
    a directory, and with an errno-less OSError for a FIFO; anything else
    (EACCES, ENOSPC) is an ordinary failure and is raised. Only the asterisk
    user or root can put a name in /data/state, so a link or a FIFO at the lock's
    name was put there on purpose. It is removed — unlink acts on the name, never
    on what a link points at — and the lock is created afresh, exclusively, once.
    Raising instead would have kept the name, and the wake-up scheduler takes its
    lock every tick (20 s by default) and only logs a raise as a `tick error`, so
    no wake-up would ring until someone cleared it. A name planted again in between
    fails the exclusive create, and a directory cannot be unlinked; both raise."""
    try:
        return _open_no_follow(path, os.O_RDWR | os.O_CREAT)
    except OSError as exc:
        planted = exc.errno in (errno.ELOOP, errno.EISDIR) or exc.errno is None
        if not planted:
            raise
    os.unlink(path)
    return _open_no_follow(path, os.O_RDWR | os.O_CREAT | os.O_EXCL)


class _Lock:
    def __init__(self, path):
        self.path = path + ".lock"
        self.fd = None

    def __enter__(self):
        if fcntl is None:
            return self
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = _open_lock(self.path)
        # Keep the lock group-writable so the root services and the asterisk-user
        # AGI can both open it (the open is read-write). Best-effort: only the
        # file's owner (or root) can chmod. By fd, never by name: see
        # _open_no_follow.
        try:
            os.fchmod(fd, 0o664)  # intentional: root services AND the asterisk-user AGIs share this file; group access is required
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)                # __exit__ never runs for a failed __enter__
            raise
        self.fd = fd
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def _read() -> dict:
    # By fd, never through a symlink. A link planted in place of the store read
    # as its target, and the next _write copied that target's JSON (any root-
    # readable file that parses as an object) into this group-readable store.
    # A link now reads as an empty store; the next write replaces it.
    try:
        with os.fdopen(_open_no_follow(PATH, os.O_RDONLY)) as fh:
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
            # mkstemp makes the temp 0600; widen to 0664 so the OTHER writer (root vs
            # the asterisk-user AGI, same group via the setgid /data/state dir) can
            # rewrite it next time. Set on the fd BEFORE the replace: a chmod by
            # name after it followed a link swapped into the store's place in
            # between. Best-effort — the writer owns the temporary file.
            try:
                os.fchmod(fh.fileno(), 0o664)  # intentional: root services AND the asterisk-user AGIs share this file; group access is required
            except OSError:
                pass
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
    """Set (or REPLACE) the room's one pending wake-up. Returns the stored entry.

    ★ THE REPLACEMENT IS REPORTED, NOT JUST PERFORMED (2026-09-15). One entry per
    extension means a second set overwrites the first with no trace anywhere.
    Live that day: ext 19's 06:20, set at 13:10:35Z, was replaced at 13:15:09Z by
    a 04:00 one — 4 m 51 s before the first was due. The delivery ledger held
    `set 06:20` and then nothing: no ring, no cancel. A reader cannot tell that
    from a wake-up the scheduler dropped on the floor, which is the failure this
    store's callers most need to be able to rule out.

    So the displaced entry comes back with the new one, under `replaced`, and
    delivery.record_wakeup_change() puts both times on the row it already writes.

    ★ ON THE RETURNED DICT ONLY. `data[str(ext)]` and the returned value are
    deliberately different objects: what goes to disk is the entry alone. A
    `replaced` key in the FILE would be read back by the next set and chain, so
    each entry would carry the whole history of the room's wake-ups into a file
    that is rewritten whole on every write.
    """
    entry = {
        "hhmm": hhmm,
        "target_epoch": next_epoch(hhmm, now_epoch),
        "set_at": int(now_epoch if now_epoch is not None else datetime.datetime.now().timestamp()),
    }
    with _Lock(PATH):
        data = _read()
        previous = data.get(str(ext))
        data[str(ext)] = entry
        _write(data)
    if isinstance(previous, dict):
        # A copy, so the persisted entry above cannot acquire the key.
        return dict(entry, replaced=dict(previous))
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
