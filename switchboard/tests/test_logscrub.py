"""webui/logscrub.py — the readable-log scrubber — and both of its callers.

★ /share/switchboard/asterisk.log is host-mounted and captured in backups. Until
v0.100.6 it was scrubbed once, at boot, by a copy of this logic that lived in
switchboard-config. Two gaps were found on the live system:

- a trunk registration retry wrote the SIP account nine and a half hours after
  the boot that had scrubbed the file, and nothing ran again to remove it;
- a failed qualify wrote a phone's LAN address at ERROR, and the scrubber had no
  rule for addresses at all.

★★ And v0.100.6's per-poll pass used the byte-shifting rewrite while Asterisk was
appending (found 2026-09-14). Its one size re-check did not cover the gap before
truncate(), so an append landing there was cut from the readable copy. The pass
that runs while Asterisk runs now masks in place with same-length bytes; the
rewrite is kept for the boot pass, which runs before Asterisk starts. Both modes
are tested here, and the race is injected for real.

Fixture lines are the real shapes from that deployment with the values replaced.
Every `@` is built rather than written, because this repo's email scanner matches
`<user>@<host>.<tld>` and cannot tell a SIP URI from an address.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import time
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parents[1]
WEBUI = ADDON / "rootfs/usr/share/switchboard/webui"
POLLER = ADDON / "rootfs/usr/share/switchboard/rtpmon/poller.py"
CONFIG = ADDON / "rootfs/usr/bin/switchboard-config"
S6 = ADDON / "rootfs/etc/s6-overlay/s6-rc.d"

ls = SourceFileLoader("logscrub_under_test", str(WEBUI / "logscrub.py")).load_module()

AT = "@"
VERBOSE_LINE = ("[Sep  9 19:50:47] VERBOSE[2232][C-01] pbx.c: Executing [s@rooms:1] "
                "Dial(\"PJSIP/12-0000\", \"PJSIP/16025551234@trunk\")\n")
ACCT_LINE = ("[Sep  7 18:39:35] WARNING[309] res_pjsip_outbound_registration.c: No "
             "response received from 'sip:example1.voip.ms:5060' on registration "
             "attempt to 'sip:123456_acct" + AT + "example1.voip.ms', retrying in '60'\n")
QUALIFY_LINE = ("[Sep  7 04:06:41] ERROR[618] res_pjsip/pjsip_options.c: Unable to send "
                "request to qualify contact sip:19" + AT + "192.168.1.71:11909 on AOR 19\n")
PRIVATE_VARIANTS = ("[Sep 10 20:42:35] WARNING[1] x.c: 10.0.0.5 172.16.0.1 "
                    "172.31.255.254 192.168.0.1 ended at 10.1.2.3.\n")
KEPT_ADDRESSES = ("[Sep 10 20:42:34] NOTICE[108] manager.c: 127.0.0.1 192.0.2.10 "
                  "172.32.0.1 172.15.255.254 build 10.10.0.0.1 Asterisk 20.11.1\n")
CLEAN = ("[Sep 10 20:42:34] NOTICE[108] cel_custom.c: No mappings found.\n"
         "[Sep 11 15:49:20] ERROR[309] chan_pjsip.c: Failed to create outgoing session\n"
         "[Sep  9 19:50:48] Asterisk 20.11.1 built by buildozer @ builder\n")

BOTH_MODES = pytest.mark.parametrize("writer_stopped", [False, True],
                                     ids=["in-place", "boot-rewrite"])


class _OsProxy:
    """The real os module with one or two calls replaced. The scrubber uses
    os.open/pread/pwrite/ftruncate, so a namespace holding only the replaced
    call would break it for a reason unrelated to the test."""

    def __init__(self, **over):
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(os, name)


# --------------------------------------------------------------------------- #
# The scrubber: the boot rewrite
# --------------------------------------------------------------------------- #

def test_the_boot_rewrite_removes_every_leak_class_and_nothing_else(tmp_path):
    f = tmp_path / "asterisk.log"
    f.write_text(VERBOSE_LINE + ACCT_LINE + QUALIFY_LINE + PRIVATE_VARIANTS
                 + KEPT_ADDRESSES + CLEAN)

    r = ls.scrub(f, writer_stopped=True)
    out = f.read_text()

    assert (r.dropped, r.redacted, r.deferred) == (1, 3, ""), r
    assert r.clean_to == f.stat().st_size, "a finished pass is clean to the end"

    assert "16025551234" not in out and "VERBOSE" not in out
    assert "123456_acct" not in out, "the trunk SIP account survived"
    assert "sip:***" + AT + "example1.voip.ms" in out, "the account was dropped, not redacted"
    assert "retrying in '60'" in out, "the WARNING itself must survive redaction"

    # The live qualify line: the address goes, the port and the AOR stay, so it
    # still says which phone.
    assert "192.168.1.71" not in out
    assert "sip:***" + AT + "<private-ip>:11909 on AOR 19" in out
    for addr in ("10.0.0.5", "172.16.0.1", "172.31.255.254", "192.168.0.1", "10.1.2.3"):
        assert addr not in out, f"private address {addr} survived"
    assert "ended at <private-ip>." in out, "an address ending a sentence is still an address"

    # Not RFC 1918, so not touched: loopback, documentation, and the two
    # neighbours either side of 172.16.0.0/12. A run longer than four parts is
    # not an address.
    for kept in ("127.0.0.1", "192.0.2.10", "172.32.0.1", "172.15.255.254",
                 "build 10.10.0.0.1", "Asterisk 20.11.1"):
        assert kept in out, f"{kept!r} was redacted but is not a private address"
    assert CLEAN in out


# --------------------------------------------------------------------------- #
# The scrubber: the in-place pass that runs while Asterisk appends
# --------------------------------------------------------------------------- #

def test_while_asterisk_runs_every_mask_is_the_same_length_and_nothing_moves(tmp_path):
    """The exact forms are pinned as literals, not built from the module's
    constants, so changing a mask changes this test on purpose."""
    f = tmp_path / "asterisk.log"
    original = (VERBOSE_LINE + ACCT_LINE + QUALIFY_LINE + PRIVATE_VARIANTS
                + KEPT_ADDRESSES + CLEAN)
    f.write_text(original)
    inode = f.stat().st_ino

    r = ls.scrub(f)
    out = f.read_text()

    assert (r.dropped, r.redacted, r.clean_to, r.deferred) == (1, 3, len(original), ""), r
    assert f.stat().st_ino == inode
    assert len(out) == len(original), "a byte moved, so an append could have been cut"
    assert [len(x) for x in out.split("\n")] == [len(x) for x in original.split("\n")]

    # VERBOSE: the timestamp stays, every byte after it is masked.
    blanked = "[Sep  9 19:50:47] " + "*" * (len(VERBOSE_LINE) - 1 - 18)
    assert out.split("\n")[0] == blanked
    assert "16025551234" not in out and "VERBOSE" not in out

    assert "123456_acct" not in out
    assert "'sip:***********" + AT + "example1.voip.ms', retrying in '60'" in out
    assert "192.168.1.71" not in out
    assert "sip:**" + AT + "<ip********>:11909 on AOR 19" in out
    assert ("x.c: <ip****> <ip******> <ip**********> <ip*******> ended at <ip****>."
            in out), out.split("\n")[3]
    for kept in ("127.0.0.1", "192.0.2.10", "172.32.0.1", "172.15.255.254",
                 "build 10.10.0.0.1", "Asterisk 20.11.1"):
        assert kept in out, f"{kept!r} was masked but is not a private address"
    assert CLEAN in out


def test_an_append_that_lands_mid_pass_survives_whole(tmp_path, monkeypatch):
    """★ THE RACE v0.100.6 LOST. An Asterisk append is injected at the moment
    the pass writes, which is after every size check it makes. The in-place pass
    must leave it intact at the end of the file; the next pass then masks it."""
    f = tmp_path / "asterisk.log"
    before = CLEAN + VERBOSE_LINE + ACCT_LINE + QUALIFY_LINE
    f.write_text(before)
    appended = ("[Sep 14 01:41:26] ERROR[1] pjsip_options.c: qualify failed for "
                "sip:77" + AT + "192.168.1.9:5060 written mid-pass\n")
    asterisk = open(f, "ab")                    # Asterisk's O_APPEND handle
    fired = []

    def pwrite(fd, data, offset):
        if not fired:
            fired.append(offset)
            asterisk.write(appended.encode())
            asterisk.flush()
        return os.pwrite(fd, data, offset)

    monkeypatch.setattr(ls, "os", _OsProxy(pwrite=pwrite))
    try:
        r = ls.scrub(f)
    finally:
        asterisk.close()
    monkeypatch.setattr(ls, "os", os)

    data = f.read_text()
    assert fired, "the pass wrote nothing, so nothing was raced"
    assert data.endswith(appended), "the line appended during the pass was cut or overwritten"
    assert len(data) == len(before) + len(appended)
    assert "123456_acct" not in data and "VERBOSE" not in data[:len(before)]
    assert r.clean_to == len(before), "the watermark passed a line this pass never read"

    r2 = ls.scrub(f, since=r.clean_to)
    assert (r2.redacted, r2.deferred) == (1, ""), r2
    assert f.read_text().endswith("sip:**" + AT + "<ip*******>:5060 written mid-pass\n")


def test_the_same_injection_does_cut_the_boot_rewrite_which_is_why_it_is_boot_only(
        tmp_path, monkeypatch):
    """The injection above is real: aimed at the byte-shifting rewrite, it loses
    the line. That is the v0.100.6 defect, and the reason `writer_stopped=True`
    belongs to the init oneshot alone."""
    f = tmp_path / "asterisk.log"
    f.write_text(CLEAN + VERBOSE_LINE + ACCT_LINE)
    appended = "[Sep 14 01:41:26] ERROR[1] x.c: appended during the boot rewrite\n"
    asterisk = open(f, "ab")

    def pwrite(fd, data, offset):
        asterisk.write(appended.encode())
        asterisk.flush()
        return os.pwrite(fd, data, offset)

    monkeypatch.setattr(ls, "os", _OsProxy(pwrite=pwrite))
    try:
        ls.scrub(f, writer_stopped=True)
    finally:
        asterisk.close()
    assert "appended during the boot rewrite" not in f.read_text()


def test_a_mask_is_never_matched_again_and_the_boot_pass_makes_it_canonical(tmp_path):
    f = tmp_path / "asterisk.log"
    f.write_text(CLEAN + VERBOSE_LINE + QUALIFY_LINE + ACCT_LINE)
    first = ls.scrub(f)
    masked = f.read_bytes()
    assert (first.dropped, first.redacted) == (1, 2), first

    # A restarted poller scans from zero. Its own masks must read as clean.
    again = ls.scrub(f, since=0)
    assert again == ls.Scrub(0, 0, len(masked), ""), again
    assert f.read_bytes() == masked

    # The next boot takes the length out: the blanked line goes, the masks
    # become the canonical tokens.
    boot = ls.scrub(f, writer_stopped=True)
    out = f.read_text()
    assert (boot.dropped, boot.redacted, boot.deferred) == (1, 2, ""), boot
    assert out == (CLEAN
                   + QUALIFY_LINE.replace("sip:19" + AT + "192.168.1.71",
                                          "sip:***" + AT + "<private-ip>")
                   + ACCT_LINE.replace("sip:123456_acct" + AT, "sip:***" + AT))
    assert ls.scrub(f, writer_stopped=True) == ls.Scrub(0, 0, len(out.encode()), "")
    assert ls.scrub(f) == ls.Scrub(0, 0, len(out.encode()), "")


def test_a_mask_lands_on_the_right_bytes_after_text_that_is_not_ascii(tmp_path):
    """Offsets are bytes; the patterns run on decoded text. A multibyte
    character, or a byte that is not UTF-8 at all, before the match on the same
    line must not shift where the mask is written."""
    f = tmp_path / "a.log"
    line = (b"[Sep  1 00:00:00] ERROR[1] x.c: caf\xc3\xa9 \xff\xfe to sip:ab"
            + AT.encode() + b"192.168.1.71:5060 end\n")
    f.write_bytes(CLEAN.encode() + line)
    assert ls.scrub(f).redacted == 1
    assert f.read_bytes() == CLEAN.encode() + (
        b"[Sep  1 00:00:00] ERROR[1] x.c: caf\xc3\xa9 \xff\xfe to sip:**"
        + AT.encode() + b"<ip********>:5060 end\n")


# --------------------------------------------------------------------------- #
# Both modes
# --------------------------------------------------------------------------- #

@BOTH_MODES
def test_a_clean_file_is_never_rewritten_and_a_second_pass_changes_nothing(tmp_path, writer_stopped):
    f = tmp_path / "a.log"
    f.write_text(CLEAN + ACCT_LINE)
    first = ls.scrub(f, writer_stopped=writer_stopped)
    body, mtime = f.read_bytes(), f.stat().st_mtime_ns
    second = ls.scrub(f, writer_stopped=writer_stopped)
    assert (first.redacted, second) == (1, ls.Scrub(0, 0, len(body), "")), (first, second)
    assert f.read_bytes() == body
    assert f.stat().st_mtime_ns == mtime, "a clean file was rewritten anyway"


def test_missing_is_clean_but_unreadable_is_reported_not_passed_off_as_clean(tmp_path):
    assert ls.scrub(tmp_path / "missing.log") == ls.Scrub()
    d = tmp_path / "dir.log"
    d.mkdir()
    r = ls.scrub(d, since=7)
    assert r.deferred.startswith("error:"), r
    assert r.clean_to == 7, "a failed pass must not advance the watermark"


@BOTH_MODES
def test_a_line_still_being_written_is_carried_through_and_judged_once_complete(tmp_path, writer_stopped):
    f = tmp_path / "a.log"
    half = "[Sep 13 18:41:26] WARNING[291] x.c: to 'sip:half_writ"
    f.write_bytes((ACCT_LINE + half).encode())

    r = ls.scrub(f, writer_stopped=writer_stopped)
    assert r.redacted == 1 and not r.deferred
    assert f.read_bytes().endswith(half.encode()), "the unfinished line was altered"
    assert r.clean_to == f.stat().st_size - len(half), "the watermark passed an unfinished line"

    with open(f, "ab") as fh:                       # Asterisk finishes the line
        fh.write(("ten" + AT + "example1.voip.ms'\n").encode())
    r2 = ls.scrub(f, since=r.clean_to, writer_stopped=writer_stopped)
    assert r2.redacted == 1, r2
    assert "half_written" not in f.read_text()
    assert r2.clean_to == f.stat().st_size


@pytest.mark.parametrize("writer_stopped,token", [(False, "<ip********>"),
                                                  (True, "<private-ip>")],
                         ids=["in-place", "boot-rewrite"])
def test_an_incremental_pass_reads_only_what_was_appended(tmp_path, writer_stopped, token):
    f = tmp_path / "a.log"
    f.write_text(ACCT_LINE + QUALIFY_LINE)
    since = len(ACCT_LINE.encode())

    r = ls.scrub(f, since=since, writer_stopped=writer_stopped)
    out = f.read_text()
    assert r.redacted == 1, r
    assert out.startswith(ACCT_LINE), "bytes before the watermark were rescanned"
    assert token in out and r.clean_to == f.stat().st_size

    # Shorter than the watermark: trimmed or replaced, so start again from zero.
    f.write_text(QUALIFY_LINE)
    r = ls.scrub(f, since=10_000, writer_stopped=writer_stopped)
    assert r.redacted == 1 and "192.168.1.71" not in f.read_text(), r


def test_the_boot_rewrite_refuses_a_file_that_grew_before_its_write(tmp_path, monkeypatch):
    """A tripwire for a broken precondition — something appending while the
    boot pass runs. It covers only the gap up to its own check; the test above
    shows what it cannot cover."""
    f = tmp_path / "a.log"
    f.write_text(CLEAN + ACCT_LINE)
    before = f.read_bytes()
    calls = []

    def fstat(fd):
        st = os.fstat(fd)
        calls.append(st.st_size)
        if len(calls) == 1:
            return st
        return types.SimpleNamespace(st_size=st.st_size + 40, st_mode=st.st_mode)

    monkeypatch.setattr(ls, "os", _OsProxy(fstat=fstat))
    r = ls.scrub(f, since=0, writer_stopped=True)
    assert len(calls) == 2, "the size was not re-checked before writing"
    assert (r.redacted, r.clean_to, r.deferred) == (1, 0, "grew"), r
    assert f.read_bytes() == before, "it wrote over a file that had grown"


@BOTH_MODES
def test_the_rewrite_is_in_place_so_asterisk_keeps_appending_to_the_same_file(tmp_path, writer_stopped):
    """Asterisk opens its log with fopen(..., "a"). A rename would strand that
    handle on an unlinked inode; an in-place rewrite does not, and O_APPEND
    lands the next line at the new end with no gap."""
    f = tmp_path / "asterisk.log"
    with open(f, "ab") as asterisk:
        asterisk.write((CLEAN + VERBOSE_LINE + ACCT_LINE).encode())
        asterisk.flush()
        inode = f.stat().st_ino
        r = ls.scrub(f, writer_stopped=writer_stopped)
        assert (r.dropped, r.redacted) == (1, 1)
        after = "[Sep 13 19:00:00] NOTICE[1] y.c: written after the scrub\n"
        asterisk.write(after.encode())
        asterisk.flush()
    data = f.read_bytes()
    assert f.stat().st_ino == inode
    assert b"\x00" not in data, "the writer left a hole: the file was not appended to"
    assert data.decode().endswith("example1.voip.ms', retrying in '60'\n" + after)


@BOTH_MODES
def test_bytes_that_are_not_utf8_are_written_back_exactly(tmp_path, writer_stopped):
    f = tmp_path / "a.log"
    odd = b"[Sep  1 00:00:00] NOTICE[1] x.c: caf\xe9 \xff\n"
    f.write_bytes(odd + ACCT_LINE.encode())
    assert ls.scrub(f, writer_stopped=writer_stopped).redacted == 1
    assert f.read_bytes().startswith(odd)


@BOTH_MODES
def test_a_symlink_or_a_fifo_is_refused_and_what_it_points_at_is_untouched(tmp_path, writer_stopped):
    """★ Root runs this in a directory the `asterisk` user can write. A link
    planted under the log's name must not become a root rewrite of its target."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "options.json"
    target.write_text(VERBOSE_LINE + ACCT_LINE + QUALIFY_LINE)
    before = target.read_bytes()
    share = tmp_path / "share"
    share.mkdir()
    link = share / "asterisk.log"
    link.symlink_to(target)

    r = ls.scrub(link, since=5, writer_stopped=writer_stopped)
    assert r.deferred.startswith("error:") and r.clean_to == 5, r
    assert target.read_bytes() == before, "the scrub followed a symlink out of the share dir"

    dangling = share / "dangling.log"
    dangling.symlink_to(outside / "nothing-here")
    assert ls.scrub(dangling).deferred.startswith("error:"), "a planted link read as 'nothing written'"

    fifo = share / "fifo.log"
    os.mkfifo(fifo)
    r = ls.scrub(fifo, since=3, writer_stopped=writer_stopped)
    assert (r.deferred, r.clean_to) == ("error: not a regular file", 3), r


# --------------------------------------------------------------------------- #
# Caller 1: switchboard-config, once at boot
# --------------------------------------------------------------------------- #

def test_boot_scrubs_the_share_log_with_the_shared_module(tmp_path, monkeypatch):
    sbc = SourceFileLoader("sbc_logscrub", str(CONFIG)).load_module()
    assert Path(sbc.LOGSCRUB_PY).resolve() == (WEBUI / "logscrub.py").resolve()
    assert not hasattr(sbc, "scrub_share_log"), "a second copy of the scrubber is back"

    share = tmp_path / "share"
    share.mkdir()
    original = CLEAN + VERBOSE_LINE + ACCT_LINE + QUALIFY_LINE
    (share / "asterisk.log").write_text(original)
    logged = []
    monkeypatch.setattr(sbc, "SHARE_DIR", share)
    monkeypatch.setattr(sbc, "SHARE_LOG", share / "asterisk.log")
    monkeypatch.setattr(sbc, "log", logged.append)

    sbc.ensure_share_log_dir()

    out = (share / "asterisk.log").read_text()
    assert "123456_acct" not in out and "192.168.1.71" not in out and "VERBOSE" not in out
    assert any("dropped 1 verbose line(s), redacted 2 line(s)" in m for m in logged), logged
    assert not any(m.startswith(("trimmed ", "WARN could not trim")) for m in logged), \
        "a log under the cap was trimmed"
    # ★ The boot pass is the REWRITE, and only these assertions tell it from the
    # in-place pass, which would also leave no account, address or "VERBOSE".
    assert out == CLEAN + ACCT_LINE.replace("sip:123456_acct" + AT, "sip:***" + AT) \
        + QUALIFY_LINE.replace("sip:19" + AT + "192.168.1.71", "sip:***" + AT + "<private-ip>")


def test_a_boot_scrub_that_could_not_run_says_so(tmp_path, monkeypatch):
    """(0, 0) from a pass that never ran is not "clean", and the boot log must
    not let the two read the same."""
    sbc = SourceFileLoader("sbc_logscrub_fail", str(CONFIG)).load_module()
    share = tmp_path / "share"
    (share / "asterisk.log").mkdir(parents=True)      # unreadable as a file
    logged = []
    monkeypatch.setattr(sbc, "SHARE_DIR", share)
    monkeypatch.setattr(sbc, "SHARE_LOG", share / "asterisk.log")
    monkeypatch.setattr(sbc, "log", logged.append)

    sbc.ensure_share_log_dir()

    assert any(m.startswith("WARN could not scrub") and "error:" in m for m in logged), logged
    assert not any(m.startswith("scrubbed ") for m in logged), logged


def _boot_share(sbc, tmp_path, monkeypatch):
    """A share dir, the boot pass pointed at it, and `asterisk` resolved to the
    user running the test — so the ownership loop RUNS, instead of stopping at
    a KeyError on a machine with no asterisk user, where it could prove nothing."""
    share = tmp_path / "share"
    share.mkdir()
    logged = []
    me = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(sbc, "pwd", types.SimpleNamespace(getpwnam=lambda name: me))
    monkeypatch.setattr(sbc, "SHARE_DIR", share)
    monkeypatch.setattr(sbc, "SHARE_LOG", share / "asterisk.log")
    monkeypatch.setattr(sbc, "log", logged.append)
    return share, logged


def test_the_boot_pass_never_follows_a_link_planted_in_the_share_dir(tmp_path, monkeypatch):
    """★ /share/switchboard belongs to `asterisk` and is group-writable; the boot
    pass runs as root. Every root action there — group-write, chown, trim,
    scrub — used to go by path, and a path follows a symlink."""
    sbc = SourceFileLoader("sbc_logscrub_links", str(CONFIG)).load_module()
    share, logged = _boot_share(sbc, tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "options.json"
    secret.write_text('{"secret": "x"}\n' + VERBOSE_LINE + ACCT_LINE * 4)
    os.chmod(secret, 0o600)
    ledger_target = outside / "other.jsonl"
    ledger_target.write_text("{}\n")
    os.chmod(ledger_target, 0o600)
    (share / "asterisk.log").symlink_to(secret)           # the log itself is a link
    (share / "heartbeat.jsonl").symlink_to(ledger_target)  # so is a ledger
    real = share / "delivery-outcomes.jsonl"
    real.write_text("{}\n")
    os.chmod(real, 0o644)
    sub = share / "subdir"
    sub.mkdir()
    os.chmod(sub, 0o755)
    fifo = share / "fifo"
    os.mkfifo(fifo, 0o644)
    # Small enough that a followed link WOULD be trimmed.
    monkeypatch.setattr(sbc, "SHARE_LOG_MAX_BYTES", 64)
    secret_before = secret.read_bytes()

    by_path = []
    real_chown = shutil.chown
    monkeypatch.setattr(shutil, "chown", lambda p, *a, **k: (by_path.append(str(p)),
                                                             real_chown(p, *a, **k))[1])
    # Every os-level ownership or mode change, by the inode it LANDED on. The
    # shutil recorder cannot see these, and the final chown of the log is one:
    # opened by path, it follows the link to `secret` and nothing else notices.
    landed = []

    def _by_fd(name):
        def call(fd, *a):
            landed.append((name, os.fstat(fd).st_ino))
            return getattr(os, name)(fd, *a)
        return call

    def _by_path(name):
        def call(path, *a, **k):
            landed.append((name, os.stat(path).st_ino))
            return getattr(os, name)(path, *a, **k)
        return call

    monkeypatch.setattr(sbc, "os", _OsProxy(fchown=_by_fd("fchown"), fchmod=_by_fd("fchmod"),
                                            chown=_by_path("chown"), chmod=_by_path("chmod")))

    sbc.ensure_share_log_dir()

    assert secret.read_bytes() == secret_before, "a root trim or scrub went through the link"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600, "group-write was added through a link"
    assert stat.S_IMODE(ledger_target.stat().st_mode) == 0o600, "group-write was added through a link"
    assert stat.S_IMODE(sub.stat().st_mode) == 0o755, "a directory was treated as a ledger"
    assert stat.S_IMODE(os.lstat(fifo).st_mode) == 0o644, "a FIFO was treated as a ledger"
    # Positive control: the loop ran, so the checks above are not vacuous.
    assert stat.S_IMODE(real.stat().st_mode) & stat.S_IWGRP, "the ownership loop never ran"
    assert any(m.startswith("WARN could not scrub") and "error:" in m for m in logged), logged
    assert by_path in ([], [str(share)]), f"chown by path inside the share dir: {by_path}"
    outside_inodes = {secret.stat().st_ino, ledger_target.stat().st_ino}
    assert not [c for c in landed if c[1] in outside_inodes], (
        f"an ownership or mode change landed outside the share dir: {landed}")
    # ...and the spy does see them: the loop's change to the real ledger is there.
    assert ("fchown", real.stat().st_ino) in landed, landed


def test_the_boot_trim_still_keeps_the_newest_half_through_the_fd(tmp_path, monkeypatch):
    """The trim now opens the log by fd. Driven for real, because the older trim
    test reimplements the arithmetic and never calls ensure_share_log_dir()."""
    sbc = SourceFileLoader("sbc_logscrub_trim", str(CONFIG)).load_module()
    share, logged = _boot_share(sbc, tmp_path, monkeypatch)
    lines = [f"[Sep 11 00:00:00] NOTICE[1] x.c: line {i} " + "x" * 60 + "\n" for i in range(400)]
    (share / "asterisk.log").write_text("".join(lines))
    inode = (share / "asterisk.log").stat().st_ino
    monkeypatch.setattr(sbc, "SHARE_LOG_MAX_BYTES", 8000)

    sbc.ensure_share_log_dir()

    out = (share / "asterisk.log").read_text()
    assert 0 < len(out) <= 4000, len(out)
    assert out.endswith(lines[-1]), "the newest line did not survive"
    assert out.startswith("[Sep 11 00:00:00] NOTICE[1] x.c: line "), "cut mid-line"
    assert "line 0 " not in out
    assert (share / "asterisk.log").stat().st_ino == inode, "the trim renamed the file"
    assert any(m.startswith("trimmed ") for m in logged), logged


# --------------------------------------------------------------------------- #
# Caller 2: the link-health poller, every cycle
# --------------------------------------------------------------------------- #

pm = SourceFileLoader("rtpmon_poller_logscrub", str(POLLER)).load_module()


def _fake_logscrub(results):
    calls = []
    kwargs = []
    it = iter(results)

    def scrub(path, since=0, **kw):
        calls.append((path, since))
        kwargs.append(kw)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    return types.SimpleNamespace(scrub=scrub), calls, kwargs


def test_the_poller_imports_the_scrubber_from_where_the_image_puts_it():
    src = POLLER.read_text()
    assert 'sys.path.insert(0, "/usr/share/switchboard/webui")' in src
    assert (ADDON / "rootfs/usr/share/switchboard/webui/logscrub.py").is_file()


class _Stop(Exception):
    pass


def test_every_cycle_scrubs_in_place_with_the_advancing_watermark_even_when_ami_is_down(monkeypatch):
    fake, calls, kwargs = _fake_logscrub([ls.Scrub(0, 0, 100, ""), ls.Scrub(0, 1, 180, "")])
    monkeypatch.setitem(sys.modules, "logscrub", fake)
    samples = iter([([{"ext": "11", "reachable": True, "registered": True,
                       "rtt_ms": 2.0}], {"reachable": 1, "total": 1}),
                    (None, None)])                          # cycle 2: AMI down
    sleeps = []

    def _sleep(n):
        sleeps.append(n)
        if len(sleeps) == 2:
            raise _Stop

    for name, value in {
        "_load_options": lambda: {"link_health_alerts": False},
        "room_names": lambda o: {},
        "wired_exts": lambda o: ["11"],
        "poll_once": lambda a, b, c, _ever=None: next(samples),
        "_append_history": lambda p: None,
        "_publish": lambda p, s: None,
        "_heartbeat": lambda *a, **k: None,
        "trunk_enabled": lambda o: False,
        "endpoint_transitions": lambda: [],
        "warmup_done": lambda *a, **k: True,
        "outage_transition": lambda *a, **k: "",
        "load_ever_registered": lambda: set(),
        "save_ever_registered": lambda s: None,
    }.items():
        monkeypatch.setattr(pm, name, value)
    monkeypatch.setattr(pm.time, "sleep", _sleep)

    with pytest.raises(_Stop):
        pm.run()
    assert calls == [(pm.SHARE_LOG_PATH, 0), (pm.SHARE_LOG_PATH, 100)], calls
    # ★ Asterisk is running, so the poller must never ask for the rewrite.
    assert kwargs == [{}, {}], f"the per-poll pass asked for {kwargs}"


def test_the_heartbeat_is_never_written_through_a_link(tmp_path, monkeypatch, capsys):
    """★ rtpmon runs as root and appends and trims heartbeat.jsonl in the
    asterisk-writable share dir — by name until 2026-09-14, so a link planted in
    its place took the append and the trim to its target. Driven through run()
    with the REAL _heartbeat(), which the test above stubs: one cycle against a
    regular ledger (the positive control), one against a link."""
    fake, _, _ = _fake_logscrub([ls.Scrub(0, 0, 0, "")] * 4)
    monkeypatch.setitem(sys.modules, "logscrub", fake)
    share = tmp_path / "share"
    share.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "options.json"
    secret.write_text('{"secret": "x"}\n' * 20)
    os.chmod(secret, 0o600)
    before = secret.read_bytes()
    monkeypatch.setattr(pm, "HEARTBEAT_MAX_BYTES", 64)    # a followed trim WOULD bite

    def _one_cycle(path):
        monkeypatch.setattr(pm, "HEARTBEAT_PATH", str(path))
        for name, value in {
            "_load_options": lambda: {"link_health_alerts": False},
            "room_names": lambda o: {},
            "wired_exts": lambda o: ["11"],
            "poll_once": lambda a, b, c, _ever=None: (
                [{"ext": "11", "reachable": True, "registered": True, "rtt_ms": 2.0}],
                {"reachable": 1, "total": 1}),
            "_append_history": lambda p: None,
            "_publish": lambda p, s: None,
            "trunk_enabled": lambda o: False,
            "endpoint_transitions": lambda: [],
            "warmup_done": lambda *a, **k: True,
            "outage_transition": lambda *a, **k: "",
            "load_ever_registered": lambda: set(),
            "save_ever_registered": lambda s: None,
        }.items():
            monkeypatch.setattr(pm, name, value)

        def _sleep(n):
            raise _Stop
        monkeypatch.setattr(pm.time, "sleep", _sleep)
        with pytest.raises(_Stop):
            pm.run()

    real = share / "heartbeat.jsonl"
    _one_cycle(real)
    rows = real.read_text().splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["poller"] == "rtpmon", rows

    link = share / "linked.jsonl"
    link.symlink_to(secret)
    capsys.readouterr()
    _one_cycle(link)

    assert secret.read_bytes() == before, "the heartbeat append or trim went through the link"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert link.is_symlink()
    assert "[switchboard-rtpmon] heartbeat: " in capsys.readouterr().err, "the refusal was not logged"


def test_a_persistent_problem_is_logged_once_and_a_recovery_rearms_it(monkeypatch, capsys):
    fake, calls, _ = _fake_logscrub([
        ls.Scrub(0, 0, 50, "error: [Errno 13] Permission denied"),
        ls.Scrub(0, 0, 50, "error: [Errno 13] Permission denied"),
        ls.Scrub(2, 1, 90, ""),
        RuntimeError("boom"),
        ls.Scrub(0, 1, 90, "grew"),
    ])
    monkeypatch.setitem(sys.modules, "logscrub", fake)
    st = {"to": 0, "problem": ""}
    for _ in range(5):
        pm._scrub_share_log(st)
    err = capsys.readouterr().err

    assert err.count("Permission denied") == 1, err
    assert err.count("switchboard-rtpmon: scrubbed ") == 1, "a deferred pass was reported as done"
    assert "in place: blanked 2 verbose line(s), masked 1 line(s)" in err
    assert "boom" in err and "(grew)" in err, err
    assert [s for _, s in calls] == [0, 50, 50, 90, 90], "an exception moved the watermark"
    assert st == {"to": 90, "problem": "grew"}


def test_scrub_only_mode_keeps_scrubbing_on_its_own_cadence(monkeypatch):
    fake, calls, kwargs = _fake_logscrub([ls.Scrub(0, 0, 40, ""), ls.Scrub(0, 0, 60, "")])
    monkeypatch.setitem(sys.modules, "logscrub", fake)
    sleeps = []

    def _sleep(n):
        sleeps.append(n)
        if len(sleeps) == 2:
            raise _Stop
    monkeypatch.setattr(pm.time, "sleep", _sleep)
    with pytest.raises(_Stop):
        pm.run_scrub_only()
    assert [s for _, s in calls] == [0, 40]
    assert kwargs == [{}, {}], kwargs
    assert sleeps == [300, 300] and pm.SCRUB_ONLY_INTERVAL == 300


def test_disabling_link_health_runs_the_scrub_instead_of_idling():
    run = (S6 / "rtpmon" / "run").read_text()
    code = "\n".join(l for l in run.splitlines() if not l.lstrip().startswith("#"))
    gate = """[ "$(bashio::config 'link_health_enabled')" = "false" ]; then"""
    block = code.split(gate, 1)[1].split("\nfi", 1)[0]
    assert "exec python3 /usr/share/switchboard/rtpmon/poller.py --scrub-only" in block
    assert "sleep infinity" not in block


def test_the_scrub_only_entry_point_really_scrubs_in_place(tmp_path):
    """End to end through `__main__`: the flag the run script passes must reach
    run_scrub_only(), and that must mask a real file without moving a byte."""
    f = tmp_path / "asterisk.log"
    original = CLEAN + VERBOSE_LINE + QUALIFY_LINE
    f.write_text(original)
    env = dict(os.environ, SWITCHBOARD_SHARE_LOG=str(f), PYTHONPATH=str(WEBUI),
               PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen([sys.executable, str(POLLER), "--scrub-only"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "192.168.1.71" in f.read_text():
            time.sleep(0.1)
    finally:
        proc.terminate()
        _, err = proc.communicate(timeout=10)
    out = f.read_text()
    assert "192.168.1.71" not in out, err.decode()
    assert b"scrubbing the readable log only" in err
    assert len(out) == len(original), "the running poller moved bytes under Asterisk"
    assert "sip:**" + AT + "<ip********>:11909" in out and "VERBOSE" not in out
