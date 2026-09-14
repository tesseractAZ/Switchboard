"""webui/logscrub.py — the readable-log scrubber — and both of its callers.

★ /share/switchboard/asterisk.log is host-mounted and captured in backups. Until
v0.100.6 it was scrubbed once, at boot, by a copy of this logic that lived in
switchboard-config. Two gaps were found on the live system:

- a trunk registration retry wrote the SIP account nine and a half hours after
  the boot that had scrubbed the file, and nothing ran again to remove it;
- a failed qualify wrote a phone's LAN address at ERROR, and the scrubber had no
  rule for addresses at all.

Fixture lines are the real shapes from that deployment with the values replaced.
Every `@` is built rather than written, because this repo's email scanner matches
`<user>@<host>.<tld>` and cannot tell a SIP URI from an address.
"""

from __future__ import annotations

import os
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


# --------------------------------------------------------------------------- #
# The scrubber
# --------------------------------------------------------------------------- #

def test_every_leak_class_goes_and_nothing_else_does(tmp_path):
    f = tmp_path / "asterisk.log"
    f.write_text(VERBOSE_LINE + ACCT_LINE + QUALIFY_LINE + PRIVATE_VARIANTS
                 + KEPT_ADDRESSES + CLEAN)

    r = ls.scrub(f)
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


def test_a_clean_file_is_never_rewritten_and_a_second_pass_changes_nothing(tmp_path):
    f = tmp_path / "a.log"
    f.write_text(CLEAN + ACCT_LINE)
    first = ls.scrub(f)
    body, mtime = f.read_bytes(), f.stat().st_mtime_ns
    second = ls.scrub(f)
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


def test_a_line_still_being_written_is_carried_through_and_judged_once_complete(tmp_path):
    f = tmp_path / "a.log"
    half = "[Sep 13 18:41:26] WARNING[291] x.c: to 'sip:half_writ"
    f.write_bytes((ACCT_LINE + half).encode())

    r = ls.scrub(f)
    assert r.redacted == 1 and not r.deferred
    assert f.read_bytes().endswith(half.encode()), "the unfinished line was altered"
    assert r.clean_to == f.stat().st_size - len(half), "the watermark passed an unfinished line"

    with open(f, "ab") as fh:                       # Asterisk finishes the line
        fh.write(("ten" + AT + "example1.voip.ms'\n").encode())
    r2 = ls.scrub(f, since=r.clean_to)
    assert r2.redacted == 1, r2
    assert "half_written" not in f.read_text()
    assert r2.clean_to == f.stat().st_size


def test_an_incremental_pass_reads_only_what_was_appended(tmp_path):
    f = tmp_path / "a.log"
    f.write_text(ACCT_LINE + QUALIFY_LINE)
    since = len(ACCT_LINE.encode())

    r = ls.scrub(f, since=since)
    out = f.read_text()
    assert r.redacted == 1, r
    assert out.startswith(ACCT_LINE), "bytes before the watermark were rescanned"
    assert "<private-ip>" in out and r.clean_to == f.stat().st_size

    # Shorter than the watermark: trimmed or replaced, so start again from zero.
    f.write_text(QUALIFY_LINE)
    r = ls.scrub(f, since=10_000)
    assert r.redacted == 1 and "192.168.1.71" not in f.read_text(), r


def test_the_write_is_refused_if_the_file_grew_during_the_pass(tmp_path, monkeypatch):
    """The lines Asterisk appended between the read and the write would be
    truncated away. Report it and leave the watermark, so the next pass retries."""
    f = tmp_path / "a.log"
    f.write_text(CLEAN + ACCT_LINE)
    before = f.read_bytes()
    real = os.fstat
    calls = []

    def fstat(fd):
        st = real(fd)
        calls.append(st.st_size)
        if len(calls) == 1:
            return st
        return types.SimpleNamespace(st_size=st.st_size + 40)

    monkeypatch.setattr(ls, "os", types.SimpleNamespace(fstat=fstat))
    r = ls.scrub(f, since=0)
    assert len(calls) == 2, "the size was not re-checked before writing"
    assert (r.redacted, r.clean_to, r.deferred) == (1, 0, "grew"), r
    assert f.read_bytes() == before, "it wrote over a file that had grown"


def test_the_rewrite_is_in_place_so_asterisk_keeps_appending_to_the_same_file(tmp_path):
    """Asterisk opens its log with fopen(..., "a"). A rename would strand that
    handle on an unlinked inode; an in-place rewrite does not, and O_APPEND
    lands the next line at the new end with no gap."""
    f = tmp_path / "asterisk.log"
    with open(f, "ab") as asterisk:
        asterisk.write((CLEAN + VERBOSE_LINE + ACCT_LINE).encode())
        asterisk.flush()
        inode = f.stat().st_ino
        r = ls.scrub(f)
        assert (r.dropped, r.redacted) == (1, 1)
        after = "[Sep 13 19:00:00] NOTICE[1] y.c: written after the scrub\n"
        asterisk.write(after.encode())
        asterisk.flush()
    data = f.read_bytes()
    assert f.stat().st_ino == inode
    assert b"\x00" not in data, "the writer left a hole: the file was not appended to"
    assert data.decode().endswith("example1.voip.ms', retrying in '60'\n" + after)


def test_bytes_that_are_not_utf8_are_written_back_exactly(tmp_path):
    f = tmp_path / "a.log"
    odd = b"[Sep  1 00:00:00] NOTICE[1] x.c: caf\xe9 \xff\n"
    f.write_bytes(odd + ACCT_LINE.encode())
    assert ls.scrub(f).redacted == 1
    assert f.read_bytes().startswith(odd)


# --------------------------------------------------------------------------- #
# Caller 1: switchboard-config, once at boot
# --------------------------------------------------------------------------- #

def test_boot_scrubs_the_share_log_with_the_shared_module(tmp_path, monkeypatch):
    sbc = SourceFileLoader("sbc_logscrub", str(CONFIG)).load_module()
    assert Path(sbc.LOGSCRUB_PY).resolve() == (WEBUI / "logscrub.py").resolve()
    assert not hasattr(sbc, "scrub_share_log"), "a second copy of the scrubber is back"

    share = tmp_path / "share"
    share.mkdir()
    (share / "asterisk.log").write_text(CLEAN + VERBOSE_LINE + ACCT_LINE + QUALIFY_LINE)
    logged = []
    monkeypatch.setattr(sbc, "SHARE_DIR", share)
    monkeypatch.setattr(sbc, "SHARE_LOG", share / "asterisk.log")
    monkeypatch.setattr(sbc, "log", logged.append)

    sbc.ensure_share_log_dir()

    out = (share / "asterisk.log").read_text()
    assert "123456_acct" not in out and "192.168.1.71" not in out and "VERBOSE" not in out
    assert any("dropped 1 verbose line(s), redacted 2 line(s)" in m for m in logged), logged


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


# --------------------------------------------------------------------------- #
# Caller 2: the link-health poller, every cycle
# --------------------------------------------------------------------------- #

pm = SourceFileLoader("rtpmon_poller_logscrub", str(POLLER)).load_module()


def _fake_logscrub(results):
    calls = []
    it = iter(results)

    def scrub(path, since=0):
        calls.append((path, since))
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    return types.SimpleNamespace(scrub=scrub), calls


def test_the_poller_imports_the_scrubber_from_where_the_image_puts_it():
    src = POLLER.read_text()
    assert 'sys.path.insert(0, "/usr/share/switchboard/webui")' in src
    assert (ADDON / "rootfs/usr/share/switchboard/webui/logscrub.py").is_file()


class _Stop(Exception):
    pass


def test_every_cycle_scrubs_with_the_advancing_watermark_even_when_ami_is_down(monkeypatch):
    fake, calls = _fake_logscrub([ls.Scrub(0, 0, 100, ""), ls.Scrub(0, 1, 180, "")])
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


def test_a_persistent_problem_is_logged_once_and_a_recovery_rearms_it(monkeypatch, capsys):
    fake, calls = _fake_logscrub([
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
    assert "dropped 2 verbose line(s), redacted 1 line(s)" in err
    assert "boom" in err and "(grew)" in err, err
    assert [s for _, s in calls] == [0, 50, 50, 90, 90], "an exception moved the watermark"
    assert st == {"to": 90, "problem": "grew"}


def test_scrub_only_mode_keeps_scrubbing_on_its_own_cadence(monkeypatch):
    fake, calls = _fake_logscrub([ls.Scrub(0, 0, 40, ""), ls.Scrub(0, 0, 60, "")])
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
    assert sleeps == [300, 300] and pm.SCRUB_ONLY_INTERVAL == 300


def test_disabling_link_health_runs_the_scrub_instead_of_idling():
    run = (S6 / "rtpmon" / "run").read_text()
    code = "\n".join(l for l in run.splitlines() if not l.lstrip().startswith("#"))
    gate = """[ "$(bashio::config 'link_health_enabled')" = "false" ]; then"""
    block = code.split(gate, 1)[1].split("\nfi", 1)[0]
    assert "exec python3 /usr/share/switchboard/rtpmon/poller.py --scrub-only" in block
    assert "sleep infinity" not in block


def test_the_scrub_only_entry_point_really_scrubs(tmp_path):
    """End to end through `__main__`: the flag the run script passes must reach
    run_scrub_only(), and that must rewrite a real file."""
    f = tmp_path / "asterisk.log"
    f.write_text(CLEAN + QUALIFY_LINE)
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
    assert "192.168.1.71" not in f.read_text(), err.decode()
    assert b"scrubbing the readable log only" in err
