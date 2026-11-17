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
# so the dialplan's System(switchboard-mwi clear ...) — run as that user — can write
# the lock + temp files here. (Directly in /data only root could write → EPERM.)
PATH = os.environ.get("SWITCHBOARD_MWI", "/data/state/mwi.json")


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
        # Group-writable so the root webui and the asterisk-user switchboard-mwi
        # (dialplan System()) can both open the lock. Best-effort (owner-only
        # chmod). By fd, never by name: see _open_no_follow.
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
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".mwi-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            # Widen mkstemp's 0600 so the other writer (root webui vs asterisk-user
            # switchboard-mwi, same group via the setgid /data/state dir) can
            # rewrite. Set on the fd BEFORE the replace: a chmod by name after it
            # followed a link swapped into the store's place in between.
            try:
                os.fchmod(fh.fileno(), 0o664)  # intentional: root services AND the asterisk-user AGIs share this file; group access is required
            except OSError:
                pass
            json.dump(data, fh)
        os.replace(tmp, PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
