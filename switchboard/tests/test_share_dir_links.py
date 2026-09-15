"""No append into /share/switchboard follows a symlink.

    python3 -m pytest switchboard/tests/test_share_dir_links.py

★ 2026-09-14. /share/switchboard is owned by `asterisk` and group-writable, and
whatever can write the shared folder from the host can add entries to it too.
Five programs append ledgers there, and most of them run as root:

    backup-window.jsonl      usr/bin/switchboard-backup-pre, -post  root (Supervisor hooks)
    callqos-outcomes.jsonl   usr/bin/switchboard-callqos            asterisk (dialplan)
    delivery-outcomes.jsonl  webui/delivery.py record()             root (scheduler, web UI), asterisk (AGIs)
    heartbeat.jsonl          rtpmon/poller.py _heartbeat()          root

Every one of them appended, trimmed and (delivery) chmodded BY NAME, and a name
follows a link. Reproduced in review: with delivery-outcomes.jsonl made a link to
a 0600 file outside the directory, record() returned True, appended a line to
that file and changed its mode to 0620. SECURITY.md said the opposite.

Each writer is driven the way its program drives it and pointed at a link to a
file outside the directory. That file must come out byte-identical, mode
unchanged. The caps are shrunk so that a trim which followed the link WOULD
rewrite the target. A positive control first proves every writer really writes a
regular ledger, so a writer that silently wrote nothing could not pass the rest.

The boot pass and the log scrub are covered in test_logscrub.py, and the wake-up
reconciler's view of a linked ledger in test_wakeup_escalation_paths.py.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "rootfs"
BACKUP_PRE = ROOT / "usr/bin/switchboard-backup-pre"
BACKUP_POST = ROOT / "usr/bin/switchboard-backup-post"

pm = SourceFileLoader("share_links_poller",
                      str(ROOT / "usr/share/switchboard/rtpmon/poller.py")).load_module()
dl = SourceFileLoader("share_links_delivery",
                      str(ROOT / "usr/share/switchboard/webui/delivery.py")).load_module()
cq = SourceFileLoader("share_links_callqos",
                      str(ROOT / "usr/bin/switchboard-callqos")).load_module()

# Far below any fixture below, so a trim that followed a link would bite.
TINY_CAP = 64


def _heartbeat(path, monkeypatch):
    monkeypatch.setattr(pm, "HEARTBEAT_PATH", str(path))
    monkeypatch.setattr(pm, "HEARTBEAT_MAX_BYTES", TINY_CAP)
    pm._heartbeat(None, "Registered", transitions=[])


def _delivery(path, monkeypatch):
    monkeypatch.setattr(dl, "OUTCOME_PATH", str(path))
    monkeypatch.setattr(dl, "MAX_BYTES", TINY_CAP)
    return dl.record("19", "wakeup", "answered")


def _callqos(path, monkeypatch):
    monkeypatch.setattr(cq, "SHARE_OUTCOME_PATH", str(path))
    monkeypatch.setattr(cq, "SHARE_OUTCOME_MAX_BYTES", TINY_CAP)
    cq.append_outcome({"ts": 1, "ext": "19"})


def _hook(script):
    """The hook as the Supervisor runs it: the whole program, in its own process."""
    def run(path, monkeypatch):
        state = Path(path).parent.parent / "hook-state"
        state.mkdir(exist_ok=True)
        env = dict(os.environ, SWITCHBOARD_STATE=str(state),
                   SWITCHBOARD_SHARE=str(Path(path).parent),
                   SWITCHBOARD_BACKUP_STAMP=str(path), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, str(script)], env=env,
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr    # never fails a backup
        return proc.stdout
    return run


APPENDERS = {
    "backup-post-stamp": _hook(BACKUP_POST),
    "backup-pre-stamp": _hook(BACKUP_PRE),
    "callqos-outcome": _callqos,
    "delivery-record": _delivery,
    "rtpmon-heartbeat": _heartbeat,
}


def _outside_file(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "options.json"
    secret.write_text('{"secret": "x"}\n' * 20)             # 320 B, over TINY_CAP
    os.chmod(secret, 0o600)
    return secret


@pytest.mark.parametrize("writer", sorted(APPENDERS))
def test_each_writer_creates_and_appends_a_regular_ledger(tmp_path, monkeypatch, writer):
    """Positive control, through the same calls the tests below make."""
    share = tmp_path / "share"
    share.mkdir()
    ledger = share / "ledger.jsonl"
    APPENDERS[writer](ledger, monkeypatch)
    assert ledger.is_file() and not ledger.is_symlink(), "the ledger was not created"
    APPENDERS[writer](ledger, monkeypatch)
    lines = ledger.read_text().splitlines()
    # Two records; a trim may have taken the first, never the newest.
    assert 1 <= len(lines) <= 2, lines
    assert isinstance(json.loads(lines[-1]), dict)
    if writer == "delivery-record":
        # The group-write bit the AGI needs goes on through the fd now.
        assert ledger.stat().st_mode & stat.S_IWGRP, oct(ledger.stat().st_mode)


@pytest.mark.parametrize("writer", sorted(APPENDERS))
def test_a_link_planted_in_place_of_a_ledger_is_not_written_through(tmp_path, monkeypatch, writer):
    secret = _outside_file(tmp_path)
    before = secret.read_bytes()
    share = tmp_path / "share"
    share.mkdir()
    link = share / "ledger.jsonl"
    link.symlink_to(secret)

    result = APPENDERS[writer](link, monkeypatch)

    assert secret.read_bytes() == before, "an append or a trim went through the link"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600, "a mode change went through the link"
    assert link.is_symlink(), "the link was replaced rather than refused"
    if writer == "delivery-record":
        assert result is False, "a refused write reported success"


@pytest.mark.parametrize("writer", sorted(APPENDERS))
def test_a_dangling_link_does_not_create_its_target(tmp_path, monkeypatch, writer):
    """O_CREAT through a link creates the file the link names, wherever it is."""
    target = tmp_path / "outside" / "created-by-root"
    target.parent.mkdir()
    share = tmp_path / "share"
    share.mkdir()
    link = share / "ledger.jsonl"
    link.symlink_to(target)

    APPENDERS[writer](link, monkeypatch)

    assert not target.exists(), "a write through a dangling link created its target"


@pytest.mark.parametrize("writer", sorted(APPENDERS))
def test_a_fifo_planted_in_place_of_a_ledger_neither_hangs_nor_takes_a_record(
        tmp_path, monkeypatch, writer):
    """A write-only open of a FIFO blocks until something reads it. Without
    O_NONBLOCK a planted FIFO would stop the poll loop, the wake-up scheduler or
    a backup at that line, indefinitely."""
    share = tmp_path / "share"
    share.mkdir()
    fifo = share / "ledger.jsonl"
    os.mkfifo(fifo)
    done = threading.Event()
    errors = []

    def _go():
        try:
            APPENDERS[writer](fifo, monkeypatch)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    threading.Thread(target=_go, daemon=True).start()
    hung = not done.wait(5)
    if hung:
        # Give the blocked open a reader so the thread can finish, then fail.
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            done.wait(10)
        finally:
            os.close(fd)
    assert not hung, "a planted FIFO hung the writer"
    assert not errors, errors
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)


@pytest.mark.parametrize("writer", sorted(APPENDERS))
def test_a_fifo_with_a_reader_attached_is_not_fed_a_record(tmp_path, monkeypatch, writer):
    """With a reader on the other end a write-only open no longer fails, so the
    regular-file check is all that stands between the record and whoever reads."""
    share = tmp_path / "share"
    share.mkdir()
    fifo = share / "ledger.jsonl"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        result = APPENDERS[writer](fifo, monkeypatch)
        try:
            got = os.read(reader, 65536)
        except BlockingIOError:
            got = b""
    finally:
        os.close(reader)
    assert got == b"", f"a record went down the FIFO: {got[:80]!r}"
    if writer == "delivery-record":
        assert result is False, "a refused write reported success"


def test_the_reconciler_is_told_a_linked_ledger_cannot_be_written(tmp_path, monkeypatch):
    """record() refuses a link, dangling or not, so is_writable() must agree: the
    wake-up reconciler decides whether to escalate on what it says. exists() and
    access() both follow a link, which is how a planted one read as writable."""
    share = tmp_path / "share"
    share.mkdir()
    ledger = share / "delivery-outcomes.jsonl"
    monkeypatch.setattr(dl, "OUTCOME_PATH", str(ledger))
    assert dl.is_writable(), "an absent ledger in a writable directory can be created"
    ledger.write_text("")
    assert dl.is_writable(), "a regular ledger is writable"
    ledger.unlink()
    target = tmp_path / "outside.jsonl"
    ledger.symlink_to(target)
    assert not dl.is_writable(), "a dangling link was called writable"
    target.write_text("")
    os.chmod(target, 0o600)
    assert not dl.is_writable(), "a link to a writable file was called writable"
