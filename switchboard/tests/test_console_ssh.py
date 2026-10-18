"""The SSH operator console (console-ssh/server.py) and its s6 wiring.

    python3 -m pytest switchboard/tests/test_console_ssh.py
    python3 switchboard/tests/test_console_ssh.py

The behaviour tests drive the REAL server with a REAL SSH client (asyncssh's
own) on a loopback port, bridged to a stand-in for console.py on a Unix
socket, because what matters is what a client can and cannot get through the
door: who may log in, what is refused, and what bytes reach the console.

asyncssh is an Alpine package in the image (py3-asyncssh). On a dev box without
it, every test that needs it prints a SKIP line (pytest reports it skipped) and
the structural checks — run script, s6 wiring, Dockerfile — still run. To run
the whole file, use a throwaway venv:

    python3 -m venv --system-site-packages /tmp/sbssh && \\
        /tmp/sbssh/bin/pip install "asyncssh==2.18.*" && \\
        /tmp/sbssh/bin/python -m pytest switchboard/tests/test_console_ssh.py
"""

import asyncio
import contextlib
import importlib.machinery
import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]
SHARE = ADDON / "rootfs" / "usr" / "share" / "switchboard"
SERVER = SHARE / "console-ssh" / "server.py"
S6 = ADDON / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d"
RUN = S6 / "console-ssh" / "run"

# Test-only credentials; nothing here is a real account.
USER = "operator"
PASSWORD = "correct-horse-battery"
USERS = [(USER, PASSWORD)]

# Every client here ignores the developer's own SSH setup: no ~/.ssh/config, no
# keys, no agent, no known_hosts (the server's key is new on every run).
CLIENT = dict(known_hosts=None, client_keys=None, agent_path=None, config=None)

try:
    import asyncssh
except ImportError:  # pragma: no cover - the dev box may not have it
    asyncssh = None

_MOD = None
_BARE_SIBLINGS = ("consoleproto", "webauth")


def _skip_without_asyncssh(name: str) -> bool:
    """True (after reporting the skip) when asyncssh is not importable."""
    if asyncssh is not None:
        return False
    reason = f"{name}: asyncssh not installed here (it is in the image)"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import pytest
        pytest.skip(reason)
    print(f"SKIP {reason}")
    return True


def _mod():
    """server.py, loaded once. It imports its console-web siblings by BARE
    name, so the directory it puts on sys.path and those names in sys.modules
    are put back afterwards (conftest's tripwire fails a leak)."""
    global _MOD
    if _MOD is not None:
        return _MOD
    saved_path = list(sys.path)
    saved_mods = {n: sys.modules.get(n) for n in _BARE_SIBLINGS}
    try:
        name = "switchboard_console_ssh_server"
        loader = importlib.machinery.SourceFileLoader(name, str(SERVER))
        spec = importlib.util.spec_from_loader(name, loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
        for n, m in saved_mods.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m
    _MOD = mod
    # Per-connection tests count one line per refused connection; the rate
    # limiter is off for them and has its own test, which turns it on.
    mod.QUIET_SECONDS = 0.0
    return mod


def _run(fn):
    """Run `await fn(mod, lines)` on a fresh loop with the server's log
    captured into `lines`; return the lines. Every run also asserts that no
    log line names the test user or holds the test password."""
    mod = _mod()
    lines = []
    real = mod.log
    mod.log = lines.append
    try:
        asyncio.run(asyncio.wait_for(fn(mod, lines), timeout=30))
    finally:
        mod.log = real
    leaked = [l for l in lines if USER in l or PASSWORD in l]
    assert not leaked, f"a log line names the user or holds the password: {leaked}"
    return lines


def _short_tmpdir() -> str:
    # A Unix socket path is limited to ~104 bytes on macOS; keep it short.
    base = "/tmp" if os.path.isdir("/tmp") else None
    return tempfile.mkdtemp(prefix="sbssh-", dir=base)


# What console.py sends first (serve_session): WILL ECHO, WILL SGA, DO SGA,
# DO NAWS — then its screen.
_IAC, _SB, _SE, _WILL, _DO = 255, 250, 240, 251, 253
CONSOLE_HELLO = bytes([_IAC, _WILL, 1, _IAC, _WILL, 3, _IAC, _DO, 3, _IAC, _DO, 31])


def _naws(cols: int, rows: int) -> bytes:
    return bytes([_IAC, _SB, 31, cols >> 8, cols & 0xFF, rows >> 8, rows & 0xFF, _IAC, _SE])


class _FakeConsole:
    """A stand-in for console.py on its Unix socket. It opens with the real
    console's negotiation and a screen holding an escaped 0xFF, records every
    byte it is sent, and hangs up when it reads a 'q' (the console's quit)."""

    SCREEN = b"BOARD\xff\xffREADY"

    def __init__(self, path: str) -> None:
        self.path = path
        self.received = bytearray()
        self.connections = 0
        self._server = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, self.path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        self.connections += 1
        writer.write(CONSOLE_HELLO + self.SCREEN)
        await writer.drain()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                self.received.extend(chunk)
                if b"q" in chunk:
                    writer.write(b"\r\nBYE")
                    await writer.drain()
                    break
        finally:
            writer.close()

    async def wait_for(self, needle: bytes, timeout: float = 5.0) -> None:
        async def _poll():
            while needle not in self.received:
                await asyncio.sleep(0.02)
        await asyncio.wait_for(_poll(), timeout)


class _Svc:
    def __init__(self, app, port, console, key, tmp):
        self.app, self.port, self.console, self.key, self.tmp = app, port, console, key, tmp


@contextlib.asynccontextmanager
async def _service(mod, users=USERS, console=True, **app_kwargs):
    tmp = _short_tmpdir()
    sock = os.path.join(tmp, "console.sock")
    fake = _FakeConsole(sock) if console else None
    if fake is not None:
        await fake.start()
    key, _ = mod.load_or_create_host_key(os.path.join(tmp, "ssh_host_ed25519_key"))
    app = mod.ConsoleSSH(list(users), sock, **app_kwargs)
    acceptor = await app.listen("127.0.0.1", 0, key)
    try:
        yield _Svc(app, acceptor.get_port(), fake, key, tmp)
    finally:
        acceptor.close()
        app.close_all()
        await acceptor.wait_closed()
        if fake is not None:
            await fake.stop()
        shutil.rmtree(tmp, ignore_errors=True)


async def _connect(port, username=USER, password=PASSWORD, **extra):
    return await asyncssh.connect("127.0.0.1", port, username=username,
                                  password=password, **CLIENT, **extra)


async def _close(conn) -> None:
    conn.close()
    await conn.wait_closed()


async def _refused(port, **kw) -> bool:
    try:
        conn = await _connect(port, **kw)
    except asyncssh.PermissionDenied:
        return True
    await _close(conn)
    return False


def _sprayer(n_max: int = 20):
    """A client that answers every password prompt with a new wrong password,
    to count how many attempts one connection is given."""
    class _Spray(asyncssh.SSHClient):
        asked = 0

        def password_auth_requested(self):
            type(self).asked += 1
            return f"wrong-{type(self).asked}" if type(self).asked <= n_max else None
    return _Spray


class _Collect(asyncssh.SSHClientSession if asyncssh else object):
    """Client session that keeps what the server sends."""

    def __init__(self) -> None:
        self.out = bytearray()

    def data_received(self, data, datatype) -> None:
        self.out.extend(data)


async def _wait_until(pred, timeout: float = 5.0) -> None:
    async def _poll():
        while not pred():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(_poll(), timeout)


# ── pure helpers ───────────────────────────────────────────────────────────

def test_input_escaping_address_key_and_window_default():
    if _skip_without_asyncssh("pure helpers"):
        return
    mod = _mod()
    assert mod.escape_input(b"abc") == b"abc"
    assert mod.escape_input(b"a\xffb\xff") == b"a\xff\xffb\xff\xff"
    assert mod.normalize_address("::ffff:192.0.2.10") == "192.0.2.10"
    assert mod.normalize_address("2001:db8::1") == "2001:db8::1"
    assert mod.normalize_address(None) == "?"
    assert mod._size(132, 50) == (132, 50)
    for w, h in ((0, 0), (0, 40), (100, 0), (None, 24), ("x", "y")):
        assert mod._size(w, h) == (80, 24), (w, h)


def test_connection_limits_count_total_and_per_address():
    if _skip_without_asyncssh("connection limits"):
        return
    mod = _mod()
    lim = mod.ConnectionLimits(total=3, per_address=2)
    assert lim.admit("198.51.100.1") is None
    assert lim.admit("198.51.100.1") is None
    assert "this address" in lim.admit("198.51.100.1")
    assert lim.admit("198.51.100.2") is None
    assert "3 connections" in lim.admit("198.51.100.3")  # total reached
    assert lim.active == 3
    lim.release("198.51.100.1")
    assert lim.count("198.51.100.1") == 1 and lim.active == 2
    assert lim.admit("198.51.100.3") is None
    for a in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
        lim.release(a)
    assert lim.active == 0 and lim.count("198.51.100.1") == 0
    lim.release("198.51.100.9")  # an unknown release never goes negative
    assert lim.active == 0


def test_a_refused_connection_flood_logs_one_line_per_interval():
    if _skip_without_asyncssh("refusal log"):
        return
    mod = _mod()
    clock = [100.0]
    lines = []
    real = mod.log
    mod.log = lines.append
    try:
        rl = mod._RefusalLog(interval=30, now=lambda: clock[0])
        for _ in range(50):
            rl.refused("198.51.100.1", "full")
        clock[0] += 31
        rl.refused("198.51.100.1", "full")
    finally:
        mod.log = real
    assert len(lines) == 2, lines
    assert "+49 more" in lines[1]


# ── host key ─────────────────────────────────────────────────────────────────

def _load_key(mod, path):
    lines = []
    real = mod.log
    mod.log = lines.append
    try:
        key, created = mod.load_or_create_host_key(path)
    finally:
        mod.log = real
    return key, created, lines


def test_the_host_key_is_created_0600_and_reused():
    if _skip_without_asyncssh("host key create/reuse"):
        return
    mod = _mod()
    tmp = _short_tmpdir()
    try:
        path = os.path.join(tmp, "ssh_host_ed25519_key")
        key, created, lines = _load_key(mod, path)
        assert created and any("generated a new SSH host key" in l for l in lines), lines
        assert key.get_algorithm() == "ssh-ed25519"
        fp = mod.fingerprint(key)
        assert fp.startswith("SHA256:"), fp
        st = os.lstat(path)
        assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600, oct(st.st_mode)
        # Written by temp file + rename: nothing else is left in the directory.
        assert sorted(os.listdir(tmp)) == ["ssh_host_ed25519_key"], os.listdir(tmp)
        assert asyncssh.import_private_key(Path(path).read_bytes()).get_fingerprint("sha256") == fp

        again, created2, lines2 = _load_key(mod, path)
        assert not created2 and mod.fingerprint(again) == fp
        assert not lines2, f"a reused key logs nothing of its own: {lines2}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_unparseable_host_key_is_replaced_with_a_warning():
    if _skip_without_asyncssh("unparseable host key"):
        return
    mod = _mod()
    tmp = _short_tmpdir()
    try:
        path = os.path.join(tmp, "ssh_host_ed25519_key")
        for junk in (b"", b"not a key at all", b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n"):
            Path(path).write_bytes(junk)
            os.chmod(path, 0o600)
            key, created, lines = _load_key(mod, path)
            assert created
            assert any(l.startswith("WARNING:") and "replacing it" in l for l in lines), lines
            assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
            reloaded, created2, lines2 = _load_key(mod, path)
            assert not created2 and not lines2
            assert mod.fingerprint(reloaded) == mod.fingerprint(key)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_symlinked_host_key_is_replaced_and_its_target_never_touched():
    if _skip_without_asyncssh("symlinked host key"):
        return
    mod = _mod()
    tmp = _short_tmpdir()
    try:
        target = os.path.join(tmp, "elsewhere")
        Path(target).write_bytes(b"precious")
        os.chmod(target, 0o644)
        path = os.path.join(tmp, "ssh_host_ed25519_key")
        os.symlink(target, path)
        key, created, lines = _load_key(mod, path)
        assert created and any("symbolic link" in l for l in lines), lines
        assert not os.path.islink(path) and stat.S_IMODE(os.lstat(path).st_mode) == 0o600
        assert Path(target).read_bytes() == b"precious"
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_host_key_readable_by_others_is_tightened_and_kept():
    if _skip_without_asyncssh("loose host key mode"):
        return
    mod = _mod()
    tmp = _short_tmpdir()
    try:
        path = os.path.join(tmp, "ssh_host_ed25519_key")
        key, _, _ = _load_key(mod, path)
        os.chmod(path, 0o644)
        again, created, lines = _load_key(mod, path)
        assert not created and mod.fingerprint(again) == mod.fingerprint(key)
        assert any("readable by others" in l for l in lines), lines
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_temp_key_left_by_an_interrupted_start_is_removed():
    if _skip_without_asyncssh("stale temp key"):
        return
    mod = _mod()
    tmp = _short_tmpdir()
    try:
        path = os.path.join(tmp, "ssh_host_ed25519_key")
        stale = os.path.join(tmp, ".ssh_host_ed25519_key.abc123.tmp")
        other = os.path.join(tmp, "unrelated.tmp")
        Path(stale).write_bytes(b"half a private key")
        Path(other).write_bytes(b"keep me")
        _load_key(mod, path)
        assert not os.path.exists(stale)
        assert Path(other).read_bytes() == b"keep me"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_unwritable_key_location_still_serves_with_a_temporary_key():
    if _skip_without_asyncssh("unwritable key dir"):
        return
    mod = _mod()
    tmp = _short_tmpdir()
    try:
        path = os.path.join(tmp, "no-such-dir", "ssh_host_ed25519_key")
        key, created, lines = _load_key(mod, path)
        assert created and key.get_algorithm() == "ssh-ed25519"
        assert any("temporary key" in l for l in lines), lines
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── who may log in ─────────────────────────────────────────────────────────────

def test_every_login_is_refused_when_no_user_is_configured():
    if _skip_without_asyncssh("no users"):
        return

    async def body(mod, lines):
        # parse_users drops rows without both fields, so these are "no users".
        users = mod.webauth.parse_users(
            '[{"username":"","password":"x"},{"username":"' + USER + '"}]')
        assert users == []
        throttle = mod.webauth.LoginThrottle(max_attempts=100)
        async with _service(mod, users=users, throttle=throttle) as svc:
            assert await _refused(svc.port)
            assert await _refused(svc.port, password="x")
            spray = _sprayer()
            assert await _refused(svc.port, password=None, client_factory=spray)
            assert svc.console.connections == 0
        return lines

    lines = _run(body)
    warnings = [l for l in lines if l.startswith("WARNING:") and "console_users" in l]
    # One per connection that tried, however many passwords it sent.
    assert len(warnings) == 3, warnings
    assert not any(l.startswith("login ok") for l in lines), lines


def test_a_wrong_password_is_refused_and_the_right_one_accepted():
    if _skip_without_asyncssh("password check"):
        return

    async def body(mod, lines):
        throttle = mod.webauth.LoginThrottle(max_attempts=100)
        async with _service(mod, throttle=throttle) as svc:
            assert await _refused(svc.port, password="wrong")
            assert await _refused(svc.port, password=PASSWORD + " ")
            assert await _refused(svc.port, username="someone-else")
            assert await _refused(svc.port, username=USER.upper())
            # No password at all: the client can only try the "none" method,
            # and there is no anonymous door.
            assert await _refused(svc.port, password=None)
            conn = await _connect(svc.port)
            await _close(conn)

    lines = _run(body)
    assert sum(1 for l in lines if l.startswith("login FAILED from 127.0.0.1")) == 4, lines
    assert sum(1 for l in lines if l.startswith("login ok from 127.0.0.1")) == 1, lines


def test_repeated_failures_from_one_address_lock_out_even_the_right_password():
    if _skip_without_asyncssh("throttle"):
        return

    async def body(mod, lines):
        clock = [1000.0]
        throttle = mod.webauth.LoginThrottle(max_attempts=2, window=300, now=lambda: clock[0])
        async with _service(mod, throttle=throttle) as svc:
            assert await _refused(svc.port, password="wrong-1")
            assert await _refused(svc.port, password="wrong-2")
            # Charged BEFORE the check: the right password is refused unseen.
            assert await _refused(svc.port)
            assert any(l == "login throttled for 127.0.0.1" for l in lines), lines
            clock[0] += 301  # the window passes
            conn = await _connect(svc.port)
            await _close(conn)
            # A success clears the address: two fresh mistakes are allowed again.
            assert await _refused(svc.port, password="wrong-3")
            conn = await _connect(svc.port)
            await _close(conn)

    _run(body)


def test_a_configured_password_is_compared_the_way_ssh_delivers_it():
    """asyncssh SASLpreps what the client sends; the configured side must be
    prepared the same way, or a password with a no-break space in it could
    never log in. A row SSH cannot carry at all is skipped, not logged."""
    if _skip_without_asyncssh("saslprep"):
        return
    mod = _mod()
    nbsp = "horse battery-staple"
    usable, skipped = mod.ssh_users([("bell", "a\x07b"), (USER, nbsp)])
    assert skipped == 1 and usable == [(USER, "horse battery-staple")]

    async def body(mod, lines):
        async with _service(mod, users=[(USER, nbsp)]) as svc:
            assert svc.app.skipped_users == 0
            conn = await _connect(svc.port, password=nbsp)
            await _close(conn)

    _run(body)


def test_main_logs_the_host_key_fingerprint_on_every_start():
    if _skip_without_asyncssh("start-up line"):
        return
    import queue
    import signal
    import threading

    tmp = _short_tmpdir()
    try:
        key_path = os.path.join(tmp, "ssh_host_ed25519_key")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = dict(os.environ, CONSOLE_SSH_PORT=str(port), CONSOLE_SSH_BIND="127.0.0.1",
                   CONSOLE_SSH_USERS="[]", CONSOLE_SSH_HOST_KEY=key_path,
                   CONSOLE_SSH_TARGET_SOCKET=os.path.join(tmp, "console.sock"),
                   PYTHONDONTWRITEBYTECODE="1")
        starts = []
        for _ in range(2):
            proc = subprocess.Popen([sys.executable, "-B", str(SERVER)], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            q = queue.Queue()
            threading.Thread(target=lambda: [q.put(l) for l in proc.stdout], daemon=True).start()
            out = []
            try:
                while not any("listening on" in l for l in out):
                    out.append(q.get(timeout=20))
                # Nothing else is needed before the stop: the warning follows
                # the listening line immediately.
                out.append(q.get(timeout=5))
            finally:
                proc.send_signal(signal.SIGTERM)
                rc = proc.wait(timeout=20)
            assert rc == 0, (rc, out)
            starts.append(out)
        fp = asyncssh.import_private_key(Path(key_path).read_bytes()).get_fingerprint("sha256")
        for out in starts:
            line = next(l for l in out if "listening on" in l)
            assert f"127.0.0.1:{port}" in line and f"host key {fp} (ssh-ed25519)" in line, line
            assert "NO valid console_users" in line, line
            assert any(l.startswith("[switchboard-console-ssh] WARNING: console_users has no valid entry")
                       for l in out), out
        assert any("generated a new SSH host key" in l for l in starts[0])
        assert not any("generated" in l for l in starts[1]), "the second start reuses the key"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_web_terminals_throttle_is_the_default():
    if _skip_without_asyncssh("default throttle"):
        return
    mod = _mod()
    app = mod.ConsoleSSH(USERS)
    assert isinstance(app.throttle, mod.webauth.LoginThrottle)
    assert app.throttle._max == mod.webauth.MAX_ATTEMPTS
    assert app.throttle._window == mod.webauth.WINDOW_SECONDS


def test_one_connection_gets_a_bounded_number_of_password_attempts():
    if _skip_without_asyncssh("attempts per connection"):
        return

    async def body(mod, lines):
        throttle = mod.webauth.LoginThrottle(max_attempts=100)
        async with _service(mod, throttle=throttle) as svc:
            spray = _sprayer()
            assert await _refused(svc.port, password=None, client_factory=spray)
            assert spray.asked >= mod.MAX_AUTH_ATTEMPTS

    lines = _run(body)
    failed = [l for l in lines if l.startswith("login FAILED")]
    assert len(failed) == _mod().MAX_AUTH_ATTEMPTS, failed


def test_no_username_reaches_any_log_including_asyncssh_s():
    if _skip_without_asyncssh("asyncssh logger"):
        return
    records = []

    class _Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    mod = _mod()
    lg = logging.getLogger("asyncssh")
    assert lg.getEffectiveLevel() >= logging.WARNING
    handler = _Grab(level=logging.DEBUG)
    lg.addHandler(handler)
    try:
        async def body(mod, lines):
            throttle = mod.webauth.LoginThrottle(max_attempts=100)
            async with _service(mod, throttle=throttle) as svc:
                assert await _refused(svc.port, password="wrong")
                conn = await _connect(svc.port)
                try:
                    await conn.run("echo " + PASSWORD)
                except asyncssh.Error:
                    pass
                await _close(conn)
        _run(body)  # also asserts the server's own lines
    finally:
        lg.removeHandler(handler)
    assert not [r for r in records if USER in r or PASSWORD in r], records


# ── what an authenticated client may not do ──────────────────────────────────

def test_exec_sftp_and_forwarding_are_refused():
    if _skip_without_asyncssh("refusals"):
        return

    async def body(mod, lines):
        async with _service(mod) as svc:
            conn = await _connect(svc.port)
            try:
                await conn.run("cat /data/options.json")
                raise AssertionError("exec was allowed")
            except asyncssh.ChannelOpenError:
                pass
            await _close(conn)

            conn = await _connect(svc.port)
            try:
                await conn.start_sftp_client()
                raise AssertionError("sftp was allowed")
            except asyncssh.ChannelOpenError:
                pass
            await _close(conn)

            conn = await _connect(svc.port)
            try:
                await conn.create_connection(asyncssh.SSHTCPSession, "127.0.0.1", svc.port)
                raise AssertionError("direct-tcpip was allowed")
            except asyncssh.ChannelOpenError:
                pass
            try:
                await conn.forward_remote_port("127.0.0.1", 0, "127.0.0.1", 9)
                raise AssertionError("remote forwarding was allowed")
            except asyncssh.ChannelListenError:
                pass
            try:
                await conn.create_unix_connection(asyncssh.SSHUNIXSession, svc.console.path)
                raise AssertionError("socket forwarding to the console was allowed")
            except asyncssh.ChannelOpenError:
                pass
            await _close(conn)
            # None of that reached the console.
            assert svc.console.connections == 0

    lines = _run(body)
    for what in ("an exec request", "a subsystem request", "a port-forwarding request",
                 "a remote port-forwarding request", "a socket-forwarding request"):
        assert any(f"refused {what}" in l for l in lines), (what, lines)
    assert not any("cat /data" in l for l in lines), "an exec command line was logged"


def test_the_listen_options_shut_every_other_door():
    if _skip_without_asyncssh("listen options"):
        return
    mod = _mod()
    opts = mod.ConsoleSSH(USERS).server_options(host_key=object())
    want = {
        "password_auth": True, "public_key_auth": False, "kbdint_auth": False,
        "host_based_auth": False, "gss_auth": False, "gss_kex": False,
        "agent_forwarding": False, "x11_forwarding": False, "allow_scp": False,
        "sftp_factory": None, "process_factory": None, "session_factory": None,
        "line_editor": False, "encoding": None, "config": None,
        "authorized_client_keys": None, "login_timeout": mod.LOGIN_TIMEOUT,
    }
    for k, v in want.items():
        assert k in opts and opts[k] == v, (k, opts.get(k))
    assert opts["keepalive_interval"] > 0


def test_one_shell_per_connection():
    if _skip_without_asyncssh("one shell"):
        return

    async def body(mod, lines):
        async with _service(mod) as svc:
            conn = await _connect(svc.port)
            chan, _ = await conn.create_session(_Collect, term_type="xterm", encoding=None)
            await svc.console.wait_for(b"\xff\xfb\x1f")  # the bridge is up
            try:
                await conn.create_session(_Collect, encoding=None)
                raise AssertionError("a second session was allowed")
            except asyncssh.ChannelOpenError:
                pass
            chan.write(b"q")
            await chan.wait_closed()
            # The shell is over, so the server closes the connection itself.
            await asyncio.wait_for(conn.wait_closed(), 5)
            assert svc.console.connections == 1

    lines = _run(body)
    assert any("refused a second session" in l for l in lines), lines


# ── connection caps ──────────────────────────────────────────────────────────

async def _raw(port):
    """A bare TCP connection: the server's version line, or b'' if refused."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        line = await asyncio.wait_for(reader.readline(), 5)
    except (ConnectionError, OSError):
        line = b""
    return reader, writer, line


def test_connections_are_capped_per_address_and_in_total_before_the_handshake():
    if _skip_without_asyncssh("caps"):
        return

    async def body(mod, lines):
        for total, per in ((10, 2), (2, 10)):
            limits = mod.ConnectionLimits(total=total, per_address=per)
            async with _service(mod, limits=limits) as svc:
                cap = min(total, per)
                held = []
                for _ in range(cap):
                    r, w, line = await _raw(svc.port)
                    assert line.startswith(b"SSH-2.0-"), line
                    held.append(w)
                r, w, line = await _raw(svc.port)
                assert line == b"", f"connection {cap + 1} got {line!r}"
                w.close()
                assert limits.active == cap
                held.pop().close()
                await _wait_until(lambda: limits.active == cap - 1)
                r, w, line = await _raw(svc.port)
                assert line.startswith(b"SSH-2.0-"), "a freed slot is usable again"
                held.append(w)
                for w in held:
                    w.close()
                await _wait_until(lambda: limits.active == 0)

    lines = _run(body)
    assert any(l.startswith("refused a connection from 127.0.0.1") for l in lines), lines


# ── the bridge to the console ────────────────────────────────────────────────

def test_end_to_end_a_shell_reaches_the_console_over_its_socket():
    if _skip_without_asyncssh("end to end"):
        return

    async def body(mod, lines):
        async with _service(mod) as svc:
            conn = await _connect(svc.port)
            chan, sess = await conn.create_session(
                _Collect, term_type="xterm-256color", term_size=(100, 30), encoding=None)
            # The client's telnet hello and the pty's size come first.
            await svc.console.wait_for(mod.TELNET_CLIENT_HELLO + _naws(100, 30))
            assert bytes(svc.console.received).startswith(mod.TELNET_CLIENT_HELLO + _naws(100, 30))
            # A literal 0xFF typed by the client reaches the console doubled.
            chan.write(b"a\xffb")
            await svc.console.wait_for(b"a\xff\xffb")
            # A window change becomes a NAWS subnegotiation.
            chan.change_terminal_size(120, 40)
            await svc.console.wait_for(_naws(120, 40))
            chan.write(b"q")
            await chan.wait_closed()
            out = bytes(sess.out)
            # The console's negotiation is stripped; its escaped 0xFF is one byte.
            assert out == b"BOARD\xffREADY\r\nBYE", out
            assert chan.get_exit_status() == 0
            await asyncio.wait_for(conn.wait_closed(), 5)

    _run(body)


def test_input_typed_before_the_console_answers_is_kept_in_order():
    """Bytes that arrive between the shell request and the console connection
    are held (bounded) and sent right after the telnet hello and the size."""
    if _skip_without_asyncssh("pending input"):
        return

    class _Chan:
        def __init__(self):
            self.out, self.status = bytearray(), None

        def write(self, data):
            self.out.extend(data)

        def exit(self, status):
            self.status = status

        def close(self):
            pass

    class _Owner:
        address = "198.51.100.7"

        def touch(self):
            pass

        def shell_ended(self):
            pass

    async def body(mod, lines):
        tmp = _short_tmpdir()
        try:
            fake = _FakeConsole(os.path.join(tmp, "console.sock"))
            await fake.start()
            app = mod.ConsoleSSH(USERS, fake.path)
            sess = mod.ConsoleSession(app, _Owner())
            chan = _Chan()
            sess.connection_made(chan)
            sess.pty_requested("xterm", (90, 33, 0, 0), {})
            sess.data_received(b"early\xff", None)
            sess.data_received(b"x" * (mod.MAX_PENDING_INPUT + 1), None)  # over the bound: dropped
            task = asyncio.ensure_future(sess._bridge())
            await fake.wait_for(b"early")
            sess.data_received(b"q", None)
            await asyncio.wait_for(task, 5)
            assert bytes(fake.received) == (mod.TELNET_CLIENT_HELLO + _naws(90, 33)
                                            + b"early\xff\xff" + b"q"), bytes(fake.received)
            assert chan.status == 0 and bytes(chan.out) == b"BOARD\xffREADY\r\nBYE"
            await fake.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    _run(body)


def test_a_shell_without_a_usable_pty_size_is_80x24():
    if _skip_without_asyncssh("default size"):
        return

    async def body(mod, lines):
        async with _service(mod) as svc:
            for kwargs in ({}, {"term_type": "vt100", "term_size": (0, 0)}):
                svc.console.received.clear()
                conn = await _connect(svc.port)
                chan, _ = await conn.create_session(_Collect, encoding=None, **kwargs)
                await svc.console.wait_for(mod.TELNET_CLIENT_HELLO + _naws(80, 24))
                chan.write(b"q")
                await chan.wait_closed()
                await _close(conn)

    _run(body)


def test_a_missing_console_is_reported_to_the_client():
    if _skip_without_asyncssh("console down"):
        return

    async def body(mod, lines):
        async with _service(mod, console=False) as svc:
            conn = await _connect(svc.port)
            chan, sess = await conn.create_session(_Collect, term_type="xterm", encoding=None)
            await chan.wait_closed()
            assert b"Operator console unavailable" in bytes(sess.out), bytes(sess.out)
            assert chan.get_exit_status() == 1
            await _close(conn)

    lines = _run(body)
    assert any(l.startswith("console connect failed") for l in lines), lines


def test_an_idle_connection_is_closed_with_or_without_a_shell():
    if _skip_without_asyncssh("idle timeout"):
        return

    async def body(mod, lines):
        async with _service(mod, idle_seconds=1.0) as svc:
            # Logged in, never opened a shell (ssh -N).
            conn = await _connect(svc.port)
            await asyncio.wait_for(conn.wait_closed(), 5)
            # A shell whose user stopped typing. The console's redraws are
            # output, which does not count as activity.
            conn = await _connect(svc.port)
            chan, _ = await conn.create_session(_Collect, term_type="xterm", encoding=None)
            await svc.console.wait_for(b"\xff\xfb\x1f")
            await asyncio.wait_for(chan.wait_closed(), 5)
            await asyncio.wait_for(conn.wait_closed(), 5)

    lines = _run(body)
    assert sum(1 for l in lines if "no input for" in l) == 2, lines


def test_input_keeps_a_shell_alive_past_the_idle_limit():
    if _skip_without_asyncssh("idle reset"):
        return

    async def body(mod, lines):
        async with _service(mod, idle_seconds=1.0) as svc:
            conn = await _connect(svc.port)
            chan, _ = await conn.create_session(_Collect, term_type="xterm", encoding=None)
            for _ in range(10):  # 2 s of typing, twice the limit
                await asyncio.sleep(0.2)
                chan.write(b"j")
            await svc.console.wait_for(b"j" * 10)
            assert not conn.is_closed() and not chan.is_closing()
            chan.write(b"q")
            await chan.wait_closed()
            await asyncio.wait_for(conn.wait_closed(), 5)

    lines = _run(body)
    assert not any("no input for" in l for l in lines), lines


# ── the s6 service and the image ─────────────────────────────────────────────

def test_the_service_is_a_longrun_after_init_in_the_user_bundle():
    d = S6 / "console-ssh"
    assert (d / "type").read_text() == "longrun\n"
    assert (d / "dependencies.d" / "init-switchboard").exists()
    assert (S6 / "user" / "contents.d" / "console-ssh").exists()
    run = RUN.read_text()
    assert run.startswith("#!/command/with-contenv bashio\n")
    assert '[ "$(bashio::config \'console_enabled\')" = "false" ]' in run
    code = "\n".join(l for l in run.splitlines() if not l.lstrip().startswith("#"))
    assert "bashio::config.true" not in code
    # Root: it reads /data and the console_users passwords.
    assert "s6-setuidgid" not in run
    execs = [l.strip() for l in run.splitlines() if l.strip().startswith("exec ")]
    assert execs == [
        "exec sleep infinity",
        "exec env -u SUPERVISOR_TOKEN -u HASSIO_TOKEN python3 /usr/share/switchboard/console-ssh/server.py",
    ], execs


def test_the_image_installs_asyncssh_and_marks_the_service_executable():
    dockerfile = (ADDON / "Dockerfile").read_text()
    final = dockerfile[dockerfile.rindex("\nFROM "):]
    m = re.search(r"^RUN apk add --no-cache \\\n(.*?)\n(?=\S)", final, re.M | re.S)
    assert m, "the final stage's main apk RUN was not found"
    block = m.group(1)
    assert re.search(r"^\s+py3-asyncssh \\$", block, re.M), block
    # Imported in the SAME layer as the install, so a broken package fails the
    # build instead of crash-looping the service on the device.
    assert '&& python3 -c "import asyncssh"' in block, block
    assert "chmod +x /etc/s6-overlay/s6-rc.d/console-ssh/run" in dockerfile
    assert "chmod +x /usr/share/switchboard/console-ssh/server.py" in dockerfile
    # The service script is not a pip dependency: nothing installs asyncssh twice.
    assert not re.search(r'"asyncssh[=<>~!]', dockerfile)


def test_no_shipped_module_shadows_asyncssh_or_what_it_imports():
    # server.py puts console-web/ first on sys.path (after importing asyncssh),
    # so a shipped file named like asyncssh or one of its dependencies would be
    # what a LATER import of that name finds.
    shipped = {p.stem for p in (ADDON / "rootfs").rglob("*.py")}
    reserved = {"asyncssh", "cryptography", "cffi", "_cffi_backend", "typing_extensions",
                "nacl", "bcrypt", "gssapi", "OpenSSL"}
    assert not shipped & reserved, shipped & reserved
    src = SERVER.read_text()
    assert src.index("\nimport asyncssh\n") < src.index("sys.path.insert(")


def _stub_bin(tmp: Path, options: dict) -> Path:
    """A switchboard-opt stand-in reading `options` (same output rules)."""
    data = tmp / "options.json"
    data.write_text(json.dumps(options))
    stub = tmp / "switchboard-opt"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"opts = json.load(open({str(data)!r}))\n"
        "key = sys.argv[1]; default = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        "v = opts.get(key)\n"
        "if v is None: print(default)\n"
        "elif isinstance(v, bool): print('true' if v else 'false')\n"
        "elif isinstance(v, (list, dict)): print(json.dumps(v, separators=(',', ':')))\n"
        "else: print(v)\n")
    stub.chmod(0o755)
    return tmp


def _run_script(options: dict, enabled: str):
    """Run the REAL run script under bash with bashio and switchboard-opt
    stubbed; report whether it idled and the environment it would exec with."""
    src = RUN.read_text()
    exec_line = "exec env -u SUPERVISOR_TOKEN -u HASSIO_TOKEN python3 /usr/share/switchboard/console-ssh/server.py"
    assert src.count("exec sleep infinity") == 1 and src.count(exec_line) == 1
    body = "\n".join(src.splitlines()[1:])
    body = body.replace("exec sleep infinity", "echo IDLE; exit 0")
    body = body.replace(exec_line, 'echo EXEC; env | grep "^CONSOLE_SSH_"; exit 0')
    prelude = (
        "bashio::config() { printf '%s\\n' \"$SBTEST_ENABLED\"; }\n"
        "bashio::log.info() { :; }\n"
        "bashio::log.notice() { :; }\n")
    with tempfile.TemporaryDirectory() as d:
        bindir = _stub_bin(Path(d), options)
        env = {"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
               "SBTEST_ENABLED": enabled}
        r = subprocess.run(["bash", "-c", prelude + body], capture_output=True,
                           text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    exported = dict(l.split("=", 1) for l in out if l.startswith("CONSOLE_SSH_"))
    return ("IDLE" in out), exported


def test_the_run_script_idles_only_on_an_explicit_false():
    if shutil.which("bash") is None:
        print("SKIP run script: bash not on PATH")
        return
    idled, _ = _run_script({}, "false")
    assert idled
    for transient in ("", "null", "true"):
        idled, env = _run_script({}, transient)
        assert not idled, f"an enable read of {transient!r} idled the SSH console"
        assert env, transient


def test_the_run_script_bridges_port_bind_and_users():
    if shutil.which("bash") is None:
        print("SKIP run script: bash not on PATH")
        return
    users = [{"username": USER, "password": PASSWORD}]
    _, env = _run_script({"console_port": 2222, "console_bind": "0.0.0.0",
                          "console_users": users}, "true")
    assert env["CONSOLE_SSH_PORT"] == "2222"
    assert env["CONSOLE_SSH_BIND"] == "0.0.0.0"
    assert json.loads(env["CONSOLE_SSH_USERS"]) == users
    # Blank reads: the default port, a loopback-only bind (fail closed), no users.
    for blank in ({}, {"console_port": "", "console_bind": "", "console_users": None},
                  {"console_port": "null", "console_bind": "null"}):
        _, env = _run_script(blank, "true")
        assert env["CONSOLE_SSH_PORT"] == "2300", (blank, env)
        assert env["CONSOLE_SSH_BIND"] == "127.0.0.1", (blank, env)
        assert json.loads(env["CONSOLE_SSH_USERS"]) == [], (blank, env)


def test_the_server_reads_what_the_run_script_exports():
    """The env names the run script exports are the ones server.py reads, and
    a blank or junk value falls back instead of crashing the service."""
    src = SERVER.read_text()
    run = RUN.read_text()
    for name in ("CONSOLE_SSH_PORT", "CONSOLE_SSH_BIND", "CONSOLE_SSH_USERS"):
        assert f'export {name}="' in run, name
        assert f'"{name}"' in src, name
    if _skip_without_asyncssh("env parsing"):
        return
    mod = _mod()
    old = os.environ.get("CONSOLE_SSH_PORT")
    try:
        for raw, want in (("", 2300), ("  ", 2300), ("junk", 2300), ("2222", 2222)):
            os.environ["CONSOLE_SSH_PORT"] = raw
            assert mod._env_int("CONSOLE_SSH_PORT", 2300) == want, raw
    finally:
        if old is None:
            os.environ.pop("CONSOLE_SSH_PORT", None)
        else:
            os.environ["CONSOLE_SSH_PORT"] = old

def test_a_row_saslprep_empties_is_skipped_not_an_empty_password():
    """SASLprep maps a soft hyphen to nothing: a password made only of such
    characters would otherwise accept an empty password."""
    if _skip_without_asyncssh("saslprep"):
        return
    mod = _mod()
    usable, skipped = mod.ssh_users([(USER, "\u00ad\u00ad"), ("\u00ad", "pw"), (USER, "ok-pass")])
    assert skipped == 2 and usable == [(USER, "ok-pass")], (usable, skipped)


def test_lines_a_stranger_can_cause_are_rate_limited_per_address():
    """A failed or throttled login, and the no-users warning, are written once
    per address and kind per QUIET_SECONDS, with the count held back: the
    journal's rate limit is shared by every container."""
    if _skip_without_asyncssh("quiet_log"):
        return
    mod = _mod()
    mod._quiet.clear()
    saved_quiet = mod.QUIET_SECONDS
    mod.QUIET_SECONDS = 60.0
    out = []
    saved = mod.log
    mod.log = out.append
    try:
        assert mod.quiet_log("192.0.2.7", "failed", "login FAILED", now=100.0)
        assert not mod.quiet_log("192.0.2.7", "failed", "login FAILED", now=110.0)
        assert not mod.quiet_log("192.0.2.7", "failed", "login FAILED", now=120.0)
        assert mod.quiet_log("192.0.2.8", "failed", "login FAILED", now=121.0)
        assert mod.quiet_log("192.0.2.7", "throttled", "login throttled", now=122.0)
        assert mod.quiet_log("192.0.2.7", "failed", "login FAILED", now=100.0 + mod.QUIET_SECONDS)
    finally:
        mod.log = saved
        mod.QUIET_SECONDS = saved_quiet
        mod._quiet.clear()
    assert len(out) == 4 and out[-1].endswith("(+2 more in the 60 s before)"), out


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
