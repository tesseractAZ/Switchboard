#!/usr/bin/env python3
"""Switchboard SSH operator console — the operator console's network door.

    ssh -p <console_port> <user>@<host>

An in-process SSH server (asyncssh, Alpine's py3-asyncssh) in front of the
operator console (console/console.py). The console itself no longer listens on
TCP: it listens only on the Unix socket /run/switchboard/console.sock (root
only) and speaks telnet there, exactly as it always has. Each SSH shell gets
one connection to that socket, and this server is the telnet client on it:

  * SSH input bytes go to the console with 0xFF doubled (IAC IAC), so a stray
    0xFF can never be read as a telnet command;
  * the console's IAC negotiation is answered once up front and stripped from
    everything it sends, so only clean ANSI reaches the SSH client (the same
    helpers the browser terminal uses, console-web/consoleproto.py);
  * the pty request and every window-change become a telnet NAWS
    subnegotiation (default 80x24).

AUTHENTICATION. Password only, against the console_users option: the same
rows, parsed by the same code (console-web/webauth.parse_users), and the same
per-address throttle the web terminal uses (webauth.LoginThrottle, charged
BEFORE the password is checked). No public keys, no keyboard-interactive, no
host-based, no GSSAPI, no default user and no anonymous access. When
console_users holds no valid row EVERY login is refused, and each connection
that tries logs one WARNING saying why. Usernames and passwords are never
logged, here or by asyncssh (its logger is held at WARNING, below which it
names users).

ONE SHELL PER CONNECTION. exec, subsystems (sftp), scp, port forwarding in
either direction, Unix-socket forwarding, tun/tap, agent forwarding and X11
are all refused. Connections are capped in total and per source address
before the SSH handshake starts, an unauthenticated connection is dropped
after LOGIN_TIMEOUT, and an authenticated one after IDLE_SECONDS without
input from the client.

HOST KEY. ed25519, generated on first start and stored at
/data/ssh_host_ed25519_key (0600, written to a temp file and renamed into
place). It is reused on every later start; a file that does not parse is
replaced with a WARNING (clients will then see a changed host key). The
SHA256 fingerprint is on the start-up line every boot, so a client's
"host key changed" prompt can be checked against the add-on log.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import signal
import stat
import sys
import tempfile
import time

# asyncssh is imported BEFORE console-web goes onto sys.path, so no file there
# can ever shadow it or one of its dependencies.
import asyncssh
from asyncssh.saslprep import SASLPrepError, saslprep

# webauth (users + throttle) and consoleproto (telnet client helpers) are the
# browser terminal's modules, reused rather than copied so the two front ends
# cannot drift apart on who may log in or how the console is spoken to.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..", "console-web")))
import consoleproto  # noqa: E402
import webauth  # noqa: E402

# ★ asyncssh logs "Beginning auth for user <name>", "Auth for user <name>
# succeeded" and an exec request's command line at INFO. Nothing in this
# process configures logging today, which would keep those lines silent anyway;
# this pins it, so a later logging.basicConfig() cannot start writing usernames
# into the add-on log.
logging.getLogger("asyncssh").setLevel(logging.WARNING)

DEFAULT_PORT = 2300
DEFAULT_BIND = "127.0.0.1"
HOST_KEY_PATH = "/data/ssh_host_ed25519_key"
CONSOLE_SOCKET = "/run/switchboard/console.sock"

# Connection caps, applied when the TCP connection arrives (before the SSH
# version exchange), so an unauthenticated flood cannot pile up key exchanges.
# The console behind this has its own session cap (32, shared by every front
# end); a shell over it past that gets the console's "busy" line and ends.
MAX_CONNECTIONS = 16
MAX_PER_ADDRESS = 4
# Seconds an unauthenticated connection may take to log in (OpenSSH's
# LoginGraceTime, shortened: a password prompt needs no two minutes).
LOGIN_TIMEOUT = 60
# Password attempts per connection. After the last one the server stops
# offering password auth and the client gives up. The per-address throttle
# (webauth.LoginThrottle: 5 attempts per 300 s) is the real bound; this only
# keeps one connection from spending all of an address's attempts.
MAX_AUTH_ATTEMPTS = 3
# An authenticated connection with no input from the client for this long is
# closed — the console's own idle reclaim, applied here too, so a session that
# never opened a shell (ssh -N) cannot hold a slot for ever.
IDLE_SECONDS = 900
# SSH-level liveness: a client that vanished without a FIN is detected after
# KEEPALIVE_INTERVAL * KEEPALIVE_COUNT_MAX seconds.
KEEPALIVE_INTERVAL = 60
KEEPALIVE_COUNT_MAX = 3
# A client that stops reading for this long while the console has output for
# it is disconnected (the web terminal's SEND_TIMEOUT).
SEND_TIMEOUT = 30
# Bounds on what one session buffers: input typed before the console answers,
# and an unterminated telnet sequence from the console.
MAX_PENDING_INPUT = 4096
MAX_TELNET_REST = 4096
# One refused-connection line per this many seconds, with a count, so a flood
# of refused connections cannot flood the log.
REFUSAL_LOG_INTERVAL = 30.0

DEFAULT_COLS, DEFAULT_ROWS = 80, 24

# What a telnet client says first: DO the console's WILLs (ECHO, SGA), and
# WILL its DOs (SGA, NAWS). The console's own negotiation is then stripped.
TELNET_CLIENT_HELLO = bytes([
    consoleproto.IAC, consoleproto.DO, consoleproto.OPT_ECHO,
    consoleproto.IAC, consoleproto.DO, consoleproto.OPT_SGA,
    consoleproto.IAC, consoleproto.WILL, consoleproto.OPT_SGA,
    consoleproto.IAC, consoleproto.WILL, consoleproto.OPT_NAWS,
])

_HOST_KEY_ALG = "ssh-ed25519"
_TEMP_SUFFIX = ".tmp"
_MAX_KEY_BYTES = 65536


def log(msg: str) -> None:
    print(f"[switchboard-console-ssh] {msg}", flush=True)


# Lines an unauthenticated client can cause (a failed or throttled login, the
# no-users warning) are written at most once per address and kind each
# QUIET_SECONDS, with the count of those held back; journald rate-limits every
# container's output together, so one host must not be able to spend it.
QUIET_SECONDS = 60.0
_QUIET_MAX = 256
_quiet: dict = {}


def quiet_log(addr: str, kind: str, msg: str, now: float | None = None) -> bool:
    """log(msg) unless (addr, kind) was logged in the last QUIET_SECONDS; True
    when written."""
    now = time.monotonic() if now is None else now
    key = (addr, kind)
    start, held = _quiet.get(key, (None, 0))
    if start is not None and now - start < QUIET_SECONDS:
        _quiet[key] = (start, held + 1)
        return False
    if len(_quiet) >= _QUIET_MAX and key not in _quiet:
        _quiet.pop(next(iter(_quiet)))
    _quiet[key] = (now, 0)
    log(msg + (f" (+{held} more in the {int(QUIET_SECONDS)} s before)" if held else ""))
    return True


def _env_int(name: str, default: int) -> int:
    """int() of a config-derived env var; unset, blank or junk -> default."""
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def escape_input(data: bytes) -> bytes:
    """SSH client bytes -> telnet data: a literal 0xFF is sent as IAC IAC."""
    return data.replace(b"\xff", b"\xff\xff") if b"\xff" in data else data


def normalize_address(addr) -> str:
    """The per-address key: an IPv4-mapped IPv6 peer counts as its IPv4 self,
    so a dual-stack bind cannot give one host two separate allowances."""
    text = str(addr or "?")
    if text.lower().startswith("::ffff:") and "." in text:
        return text[7:]
    return text


def ssh_users(rows):
    """console_users rows (webauth.parse_users output) as an SSH login will
    present them -> (usable rows, number skipped).

    asyncssh runs the username and the password a client sends through
    SASLprep (RFC 4013, as RFC 4252 asks) before validate_password sees them,
    so a configured value is prepared the same way or it could never match:
    a password typed with a no-break space arrives with a plain one. A row
    holding a character SASLprep forbids (a control character, say) cannot be
    sent over SSH at all, and is skipped and counted, never logged."""
    usable, skipped = [], 0
    for name, password in rows or []:
        try:
            name, password = saslprep(name), saslprep(password)
        except SASLPrepError:
            skipped += 1
            continue
        # SASLprep maps some characters to nothing (U+00AD, say): a row it
        # empties would accept an empty password, so it is skipped too.
        if not name or not password:
            skipped += 1
            continue
        usable.append((name, password))
    return usable, skipped


def _size(width, height):
    """A pty size from the client -> (cols, rows); zero/garbage -> 80x24."""
    try:
        cols, rows = int(width), int(height)
    except (TypeError, ValueError):
        return DEFAULT_COLS, DEFAULT_ROWS
    if cols <= 0 or rows <= 0:
        return DEFAULT_COLS, DEFAULT_ROWS
    return cols, rows


# --------------------------------------------------------------------------- #
# Host key
# --------------------------------------------------------------------------- #
def _key_prefix(path: str) -> str:
    return "." + os.path.basename(path) + "."


def _read_key_file(path: str):
    """The key file's bytes, None when there is no file, or raise ValueError
    for anything that is not a plain readable file (a symlink, a FIFO, a
    directory, an oversized file). O_NOFOLLOW + O_NONBLOCK: a planted link is
    never followed and a planted FIFO never blocks the start. A loose mode is
    tightened on the open descriptor, so the repair cannot be redirected."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("is a symbolic link") from None
        raise ValueError(exc.strerror or "cannot be opened") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("is not a regular file")
        if st.st_size > _MAX_KEY_BYTES:
            raise ValueError("is too large to be a host key")
        if st.st_mode & 0o077:
            os.fchmod(fd, 0o600)
            log(f"WARNING: SSH host key {path} was readable by others "
                f"(mode {oct(st.st_mode & 0o777)}); set to 0600")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_key_file(path: str, data: bytes) -> None:
    """Write the key beside its final name, 0600 from creation, then rename
    it into place: a crash leaves either the old file or the new one, never
    a half-written key. A symlink at `path` is replaced, never followed."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=_key_prefix(path), suffix=_TEMP_SUFFIX,
                               dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _remove_stale_temps(path: str) -> None:
    """A start interrupted between mkstemp and rename leaves a temp file that
    holds a private key. Remove ours (exact prefix and suffix, regular files
    only) before anything else."""
    directory = os.path.dirname(path) or "."
    prefix = _key_prefix(path)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not (name.startswith(prefix) and name.endswith(_TEMP_SUFFIX)):
            continue
        full = os.path.join(directory, name)
        try:
            if stat.S_ISREG(os.lstat(full).st_mode):
                os.unlink(full)
        except OSError:
            pass


def load_or_create_host_key(path: str = HOST_KEY_PATH):
    """The server's ed25519 host key: read from `path`, or generated and
    stored there on first start. Returns (key, created)."""
    _remove_stale_temps(path)
    problem = None
    try:
        data = _read_key_file(path)
    except ValueError as exc:
        data, problem = None, str(exc)
    if data is not None:
        try:
            return asyncssh.import_private_key(data), False
        except (ValueError, TypeError):
            problem = "could not be parsed"
    if problem is not None:
        log(f"WARNING: SSH host key {path} {problem}; replacing it with a new "
            "key. Clients that saved the old key will report a changed host key.")
    key = asyncssh.generate_private_key(_HOST_KEY_ALG)
    try:
        _write_key_file(path, key.export_private_key("openssh"))
    except OSError as exc:
        # Serving with a key that changes every start beats not serving: the
        # console stays reachable, and this line says why the key will change.
        log(f"WARNING: could not store the SSH host key at {path} "
            f"({exc.strerror or exc}); using a temporary key until the next start")
        return key, True
    if problem is None:
        log(f"generated a new SSH host key at {path}")
    return key, True


def fingerprint(key) -> str:
    return key.get_fingerprint("sha256")


# --------------------------------------------------------------------------- #
# Connection caps
# --------------------------------------------------------------------------- #
class ConnectionLimits:
    """Total and per-address connection counters. admit() returns None when a
    connection may proceed, else the reason it may not; release() undoes an
    admitted one. Everything runs on the event loop thread, so no lock."""

    def __init__(self, total: int = MAX_CONNECTIONS,
                 per_address: int = MAX_PER_ADDRESS) -> None:
        self.total = total
        self.per_address = per_address
        self._by_addr = {}
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    def count(self, addr: str) -> int:
        return self._by_addr.get(addr, 0)

    def admit(self, addr: str):
        if self._active >= self.total:
            return f"{self.total} connections already open"
        if self._by_addr.get(addr, 0) >= self.per_address:
            return f"{self.per_address} connections already open from this address"
        self._active += 1
        self._by_addr[addr] = self._by_addr.get(addr, 0) + 1
        return None

    def release(self, addr: str) -> None:
        n = self._by_addr.get(addr, 0)
        if n <= 1:
            self._by_addr.pop(addr, None)
        else:
            self._by_addr[addr] = n - 1
        if self._active > 0:
            self._active -= 1


class _RefusalLog:
    """At most one refused-connection line per interval, carrying how many
    were refused since the last one."""

    def __init__(self, interval: float = REFUSAL_LOG_INTERVAL, now=time.monotonic) -> None:
        self._interval = interval
        self._now = now
        self._last = None
        self._suppressed = 0

    def refused(self, addr: str, reason: str) -> None:
        t = self._now()
        if self._last is not None and t - self._last < self._interval:
            self._suppressed += 1
            return
        extra = f" (+{self._suppressed} more refused since the last line)" if self._suppressed else ""
        self._suppressed = 0
        self._last = t
        log(f"refused a connection from {addr}: {reason}{extra}")


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #
class ConsoleSSH:
    """Everything shared by the connections: the users, the throttle, the
    caps, and where the console is. One instance per process (or per test)."""

    def __init__(self, users, socket_path: str = CONSOLE_SOCKET, *,
                 throttle=None, limits=None,
                 login_timeout: float = LOGIN_TIMEOUT,
                 idle_seconds: float = IDLE_SECONDS,
                 max_auth_attempts: int = MAX_AUTH_ATTEMPTS,
                 send_timeout: float = SEND_TIMEOUT) -> None:
        self.users, self.skipped_users = ssh_users(users)
        self.socket_path = socket_path
        self.throttle = throttle if throttle is not None else webauth.LoginThrottle()
        self.limits = limits if limits is not None else ConnectionLimits()
        self.login_timeout = login_timeout
        self.idle_seconds = idle_seconds
        self.max_auth_attempts = max_auth_attempts
        self.send_timeout = send_timeout
        self.refusals = _RefusalLog()
        self.connections = set()

    def server_factory(self) -> "_ConnectionServer":
        return _ConnectionServer(self)

    def server_options(self, host_key) -> dict:
        """The asyncssh listen() options. Every door this service does not use
        is shut by name rather than left to a default, so an asyncssh upgrade
        that changes a default cannot open one."""
        return dict(
            server_factory=self.server_factory,
            server_host_keys=[host_key],
            # No OpenSSH server config is read (asyncssh loads none unless asked).
            config=None,
            # Password only.
            password_auth=True,
            public_key_auth=False,
            kbdint_auth=False,
            host_based_auth=False,
            gss_auth=False,
            gss_kex=False,
            gss_host=None,
            authorized_client_keys=None,
            # One interactive shell; nothing else.
            allow_pty=True,
            line_editor=False,
            agent_forwarding=False,
            x11_forwarding=False,
            sftp_factory=None,
            allow_scp=False,
            process_factory=None,
            session_factory=None,
            # Raw bytes both ways: the console speaks telnet, not UTF-8 text.
            encoding=None,
            rdns_lookup=False,
            login_timeout=self.login_timeout,
            keepalive_interval=KEEPALIVE_INTERVAL,
            keepalive_count_max=KEEPALIVE_COUNT_MAX,
            reuse_address=True,
        )

    async def listen(self, host: str, port: int, host_key):
        return await asyncssh.listen(host, port, **self.server_options(host_key))

    def close_all(self) -> None:
        for conn in list(self.connections):
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — shutting down regardless
                pass


class _ConnectionServer(asyncssh.SSHServer):
    """One per TCP connection: the caps, the login, the idle watchdog, and the
    one shell this connection may open."""

    def __init__(self, app: ConsoleSSH) -> None:
        self._app = app
        self._conn = None
        self._addr = "?"
        self._admitted = False
        self._attempts = 0
        self._authed = False
        self._warned_no_users = False
        self._sessions = 0
        self._last_input = 0.0
        self._idle_handle = None

    # -- connection lifetime ------------------------------------------------
    def connection_made(self, conn) -> None:
        self._conn = conn
        peer = conn.get_extra_info("peername")
        self._addr = normalize_address(peer[0] if peer else "?")
        reason = self._app.limits.admit(self._addr)
        if reason is not None:
            self._app.refusals.refused(self._addr, reason)
            # Before the version exchange: nothing has been sent, so a plain
            # abort is the whole refusal. asyncssh skips its version line once
            # the transport is gone.
            conn.abort()
            return
        self._admitted = True
        self._app.connections.add(conn)
        self._last_input = time.monotonic()

    def connection_lost(self, exc) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if not self._admitted:
            return
        self._admitted = False
        self._app.limits.release(self._addr)
        self._app.connections.discard(self._conn)
        if self._authed:
            log(f"connection from {self._addr} closed")

    # -- authentication -----------------------------------------------------
    def begin_auth(self, username: str) -> bool:
        return True  # every connection authenticates; there is no anonymous door

    def password_auth_supported(self) -> bool:
        return self._attempts < self._app.max_auth_attempts

    def public_key_auth_supported(self) -> bool:
        return False

    def kbdint_auth_supported(self) -> bool:
        return False

    def host_based_auth_supported(self) -> bool:
        return False

    def validate_password(self, username: str, password: str) -> bool:
        self._attempts += 1
        if not self._app.users:
            if not self._warned_no_users:
                self._warned_no_users = True
                quiet_log(self._addr, "no-users",
                          f"WARNING: refused a login from {self._addr}: console_users has "
                          "no valid entry, so every SSH login is refused. Add a username "
                          "and password under console_users.")
            # Charged all the same: whether users exist must not change how
            # quickly an address runs out of attempts.
            self._app.throttle.charge(self._addr)
            return False
        # Charge BEFORE verifying, as the web terminal does: a refused attempt
        # is never free to retry.
        if not self._app.throttle.charge(self._addr):
            quiet_log(self._addr, "throttled", f"login throttled for {self._addr}")
            return False
        if webauth.check_credentials(self._app.users, username, password):
            self._app.throttle.clear(self._addr)
            self._authed = True
            log(f"login ok from {self._addr}")
            self._arm_idle()
            return True
        quiet_log(self._addr, "failed", f"login FAILED from {self._addr}")
        return False

    # -- idle watchdog --------------------------------------------------------
    def touch(self) -> None:
        self._last_input = time.monotonic()

    def _arm_idle(self) -> None:
        loop = asyncio.get_running_loop()
        self._last_input = time.monotonic()
        self._idle_handle = loop.call_later(self._app.idle_seconds, self._idle_check)

    def _idle_check(self) -> None:
        self._idle_handle = None
        if not self._admitted or self._conn is None:
            return
        quiet = time.monotonic() - self._last_input
        if quiet >= self._app.idle_seconds:
            log(f"closing the connection from {self._addr}: no input for "
                f"{int(self._app.idle_seconds)} s")
            self._conn.close()
            return
        loop = asyncio.get_running_loop()
        self._idle_handle = loop.call_later(self._app.idle_seconds - quiet,
                                            self._idle_check)

    # -- channels -------------------------------------------------------------
    def session_requested(self):
        if self._sessions:
            log(f"refused a second session on one connection from {self._addr}")
            return False
        self._sessions += 1
        self.touch()
        return ConsoleSession(self._app, self)

    def shell_ended(self) -> None:
        """The one shell is over, so the connection has nothing left to do."""
        if self._conn is not None:
            asyncio.get_running_loop().call_soon(self._conn.close)

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        log(f"refused a port-forwarding request from {self._addr}")
        return False

    def server_requested(self, listen_host, listen_port):
        log(f"refused a remote port-forwarding request from {self._addr}")
        return False

    def unix_connection_requested(self, dest_path):
        log(f"refused a socket-forwarding request from {self._addr}")
        return False

    def unix_server_requested(self, listen_path):
        log(f"refused a remote socket-forwarding request from {self._addr}")
        return False

    def tun_requested(self, unit):
        log(f"refused a tunnel request from {self._addr}")
        return False

    def tap_requested(self, unit):
        log(f"refused a tunnel request from {self._addr}")
        return False

    @property
    def address(self) -> str:
        return self._addr


class ConsoleSession(asyncssh.SSHServerSession):
    """The one shell: a telnet client on the console's Unix socket."""

    def __init__(self, app: ConsoleSSH, owner: _ConnectionServer) -> None:
        self._app = app
        self._owner = owner
        self._chan = None
        self._cols, self._rows = DEFAULT_COLS, DEFAULT_ROWS
        self._writer = None
        self._pending = b""
        self._task = None
        self._closed = False
        self._can_write = asyncio.Event()
        self._can_write.set()

    # -- what the client may ask for ------------------------------------------
    def connection_made(self, chan) -> None:
        self._chan = chan

    def pty_requested(self, term_type, term_size, term_modes) -> bool:
        self._cols, self._rows = _size(term_size[0], term_size[1])
        return True

    def shell_requested(self) -> bool:
        return True

    def exec_requested(self, command) -> bool:
        # The command is the client's text and may hold anything; never logged.
        log(f"refused an exec request from {self._owner.address}")
        return False

    def subsystem_requested(self, subsystem) -> bool:
        log(f"refused a subsystem request (sftp or other) from {self._owner.address}")
        return False

    def break_received(self, msec) -> bool:
        return False

    def signal_received(self, signal) -> None:
        return None

    # -- the bridge -------------------------------------------------------------
    def session_started(self) -> None:
        self._task = asyncio.ensure_future(self._bridge())

    def data_received(self, data, datatype) -> None:
        self._owner.touch()
        payload = escape_input(data)
        if self._writer is not None:
            self._to_console(payload)
            return
        if len(self._pending) + len(payload) <= MAX_PENDING_INPUT:
            self._pending += payload

    def terminal_size_changed(self, width, height, pixwidth, pixheight) -> None:
        self._owner.touch()
        self._cols, self._rows = _size(width, height)
        if self._writer is not None:
            self._to_console(consoleproto.naws_subnegotiation(self._cols, self._rows))

    def _to_console(self, payload: bytes) -> None:
        transport = self._writer.transport if self._writer is not None else None
        if transport is None or transport.is_closing():
            return  # the bridge is ending; its own path closes the channel
        self._writer.write(payload)
        # A console that stopped reading must not make this grow for ever.
        if transport.get_write_buffer_size() > 65536:
            log(f"closing the shell from {self._owner.address}: the console "
                "is not reading input")
            self._close_channel(1)

    def eof_received(self) -> bool:
        return False  # the client closed its input: end the shell

    def pause_writing(self) -> None:
        self._can_write.clear()

    def resume_writing(self) -> None:
        self._can_write.set()

    def connection_lost(self, exc) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._can_write.set()
        self._owner.shell_ended()

    def _write(self, data: bytes) -> bool:
        if self._closed or self._chan is None:
            return False
        try:
            self._chan.write(data)
            return True
        except (OSError, asyncssh.Error):
            return False

    def _close_channel(self, status: int) -> None:
        if self._closed or self._chan is None:
            return
        try:
            self._chan.exit(status)
        except (OSError, asyncssh.Error):
            try:
                self._chan.close()
            except (OSError, asyncssh.Error):
                pass

    async def _bridge(self) -> None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self._app.socket_path), timeout=5)
        except (OSError, asyncio.TimeoutError) as exc:
            log(f"console connect failed: {getattr(exc, 'strerror', None) or type(exc).__name__}")
            self._write(b"\r\n  Operator console unavailable (is it running?).\r\n")
            self._close_channel(1)
            return
        if self._closed:
            writer.close()
            return
        writer.write(TELNET_CLIENT_HELLO
                     + consoleproto.naws_subnegotiation(self._cols, self._rows)
                     + self._pending)
        self._pending = b""
        self._writer = writer
        rest = b""
        status = 0
        try:
            while not self._closed:
                if not self._can_write.is_set():
                    try:
                        await asyncio.wait_for(self._can_write.wait(), self._app.send_timeout)
                    except asyncio.TimeoutError:
                        log(f"closing the shell from {self._owner.address}: the "
                            f"client stopped reading for {int(self._app.send_timeout)} s")
                        status = 1
                        break
                    if self._closed:
                        break
                chunk = await reader.read(4096)
                if not chunk:
                    break
                rest += chunk
                clean, rest = consoleproto.strip_telnet(rest)
                if len(rest) > MAX_TELNET_REST:
                    rest = b""  # an unterminated sequence that long is malformed
                if clean and not self._write(clean):
                    break
        except asyncio.CancelledError:
            raise
        except OSError:
            status = 1
        except Exception as exc:  # noqa: BLE001 — one session must never take down the server
            log(f"session error from {self._owner.address}: {type(exc).__name__}")
            status = 1
        finally:
            if self._writer is not None:
                self._writer.close()
                self._writer = None
        self._close_channel(status)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _gate_text(n_users: int) -> str:
    if n_users:
        return f"password login for {n_users} console user(s)"
    return "NO valid console_users — every login is refused"


async def _serve(host: str, port: int, users, key_path: str, socket_path: str) -> None:
    # Signal handlers first: a stop that lands while the key is generated or
    # the port bound is then a clean shutdown, not a default-action kill.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    key, _created = load_or_create_host_key(key_path)
    app = ConsoleSSH(users, socket_path)
    try:
        acceptor = await app.listen(host, port, key)
    except OSError as exc:
        log(f"cannot listen on {host}:{port}: {exc.strerror or exc}")
        raise SystemExit(1) from None
    log(f"SSH operator console listening on {host}:{port} — host key "
        f"{fingerprint(key)} ({key.get_algorithm()}) — {_gate_text(len(app.users))} "
        f"— bridging to {socket_path}")
    if app.skipped_users:
        log(f"WARNING: {app.skipped_users} console_users entr"
            f"{'y holds' if app.skipped_users == 1 else 'ies hold'} a character an "
            "SSH client cannot send (a control character, for example) and cannot "
            "log in over SSH.")
    if not app.users:
        log("WARNING: console_users has no valid entry (a username and a password "
            "are both required), so every SSH login will be refused.")
    await stop.wait()
    log("shutting down")
    acceptor.close()
    app.close_all()
    try:
        await asyncio.wait_for(acceptor.wait_closed(), timeout=5)
    except asyncio.TimeoutError:
        pass


def main() -> None:
    port = _env_int("CONSOLE_SSH_PORT", DEFAULT_PORT)
    if not 0 < port < 65536:
        port = DEFAULT_PORT
    host = os.environ.get("CONSOLE_SSH_BIND", "").strip() or DEFAULT_BIND
    users = webauth.parse_users(os.environ.get("CONSOLE_SSH_USERS", ""))
    key_path = os.environ.get("CONSOLE_SSH_HOST_KEY", "").strip() or HOST_KEY_PATH
    socket_path = (os.environ.get("CONSOLE_SSH_TARGET_SOCKET", "").strip()
                   or CONSOLE_SOCKET)
    asyncio.run(_serve(host, port, users, key_path, socket_path))


if __name__ == "__main__":
    main()
