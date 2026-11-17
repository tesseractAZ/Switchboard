"""No root write into /data/state follows a symlink.

    python3 -m pytest switchboard/tests/test_state_dir_links.py

/data/state is `drwxrwsr-x asterisk:asterisk`: the asterisk user owns it, its
group can write it, and it is not sticky, so the asterisk user can put any name
in it and swap any name for a link at any moment. These root writers use it:

    linkhealth.jsonl       rtpmon/poller.py _append_history        root (rtpmon)
    ever-registered.json   rtpmon/poller.py save_ever_registered   root (rtpmon)
    cordless-mos.jsonl     devhealth/poller.py capture_low_mos     root (devhealth)
    wakeups.json(.lock)    wakeup/store.py                         root (scheduler, web UI,
                                                                   operator console), asterisk (AGI)
    mwi.json(.lock)        webui/mwi_store.py                      root (web UI, operator console,
                                                                   boot replay), asterisk (dialplan)

and each of them went through a name somewhere: a fixed `<file>.tmp` opened with
open(), an append or a trim by name, a chmod by name after an os.replace, a
`.lock` opened "a+" and chmodded by name, or a read of the store through a link
whose result was then written back into a file the asterisk group reads.

Each writer is driven the way its program drives it, with a link planted at the
name it used to follow, pointing at a file OUTSIDE the directory. That file must
come out byte-identical, mode unchanged. A positive control first proves every
writer really writes a regular file with the mode its readers need, so a writer
that silently wrote nothing could not pass the rest. The two v0.119.1 writers
(link-outage.json, devhealth-alerts.json) are covered next to their own tests.
"""
from __future__ import annotations

import json
import os
import stat
import threading
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "rootfs" / "usr/share/switchboard"

pm = SourceFileLoader("state_links_rtpmon", str(ROOT / "rtpmon/poller.py")).load_module()
dh = SourceFileLoader("state_links_devhealth", str(ROOT / "devhealth/poller.py")).load_module()
ws = SourceFileLoader("state_links_wakeup_store", str(ROOT / "wakeup/store.py")).load_module()
mw = SourceFileLoader("state_links_mwi_store", str(ROOT / "webui/mwi_store.py")).load_module()

SECRET = b'{"exts": ["99"], "secret": "root-only"}\n'


def _victim(tmp_path: Path, mode: int = 0o600) -> Path:
    """A file outside the state directory, as /data/options.json would be."""
    v = tmp_path / "outside" / "victim"
    v.parent.mkdir()
    v.write_bytes(SECRET)
    os.chmod(v, mode)
    return v


def _untouched(v: Path, mode: int = 0o600) -> bool:
    return v.read_bytes() == SECRET and stat.S_IMODE(os.stat(v).st_mode) == mode


def _state(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _swap_in_a_link_after_replace(monkeypatch, victim: Path, name: str) -> None:
    """os.replace, then what the asterisk user can do the instant after it:
    unlink the new file and put a link to `victim` in its place. A chmod by
    name after the replace would now land on the victim."""
    real = os.replace

    def replace(src, dst, *a, **k):
        real(src, dst, *a, **k)
        if os.path.basename(str(dst)) == name:
            os.unlink(dst)
            os.symlink(victim, dst)

    monkeypatch.setattr(os, "replace", replace)


# --------------------------------------------------------------------------- #
# rtpmon: the link-health ledger
# --------------------------------------------------------------------------- #
_PHONES = [{"ext": "11", "name": "A", "status": "Avail", "rtt_ms": 2.4,
            "reachable": True, "registered": True}]


def test_linkhealth_positive_control(tmp_path, monkeypatch):
    p = _state(tmp_path) / "linkhealth.jsonl"
    monkeypatch.setattr(pm, "STATE_PATH", str(p))
    pm._append_history(_PHONES)
    pm._append_history(_PHONES)
    assert p.is_file() and not p.is_symlink()
    assert len(p.read_text().splitlines()) == 2
    assert _mode(p) == 0o640, "root writes, group asterisk reads, world nothing"


def test_linkhealth_does_not_copy_a_linked_target_into_the_ledger(tmp_path, monkeypatch):
    """The read went by name: a link in the ledger's place had root copy its
    target's lines into a fresh ledger the asterisk group reads."""
    v = _victim(tmp_path)
    p = _state(tmp_path) / "linkhealth.jsonl"
    p.symlink_to(v)
    monkeypatch.setattr(pm, "STATE_PATH", str(p))
    pm._append_history(_PHONES)
    assert _untouched(v)
    assert p.is_file() and not p.is_symlink(), "the replace puts a regular file back"
    assert b"root-only" not in p.read_bytes(), "the link's target was copied in"
    assert len(p.read_text().splitlines()) == 1
    assert _mode(p) == 0o640


def test_linkhealth_mode_is_set_before_the_name_is_visible(tmp_path, monkeypatch):
    """The chmod ran by name AFTER os.replace, so a link swapped in between
    took mode 0640 onto its target."""
    v = _victim(tmp_path)
    p = _state(tmp_path) / "linkhealth.jsonl"
    monkeypatch.setattr(pm, "STATE_PATH", str(p))
    _swap_in_a_link_after_replace(monkeypatch, v, p.name)
    pm._append_history(_PHONES)
    assert _untouched(v)


# --------------------------------------------------------------------------- #
# rtpmon: the ever-registered set
# --------------------------------------------------------------------------- #
def test_ever_registered_positive_control(tmp_path):
    d = _state(tmp_path)
    p = d / "ever-registered.json"
    assert pm.save_ever_registered({"12", "11"}, str(p)) is True
    assert json.loads(p.read_text()) == {"exts": ["11", "12"]}
    assert pm.load_ever_registered(str(p)) == {"11", "12"}
    assert _mode(p) == 0o600, "rtpmon, as root, is the only reader"
    assert os.listdir(d) == [p.name], "no temporary file left behind"


def test_ever_registered_never_writes_through_the_old_fixed_temp_name(tmp_path):
    """The save wrote `<file>.tmp` with open(): a link planted there got a
    truncation and the JSON, and os.replace then moved the link into place."""
    v = _victim(tmp_path)
    p = _state(tmp_path) / "ever-registered.json"
    planted = Path(str(p) + ".tmp")
    planted.symlink_to(v)
    assert pm.save_ever_registered({"11"}, str(p)) is True
    assert _untouched(v)
    assert p.is_file() and not p.is_symlink()
    assert json.loads(p.read_text()) == {"exts": ["11"]}
    assert planted.is_symlink(), "the planted name is neither used nor moved"


def test_ever_registered_does_not_read_or_keep_a_link_at_its_name(tmp_path):
    v = _victim(tmp_path)          # holds {"exts": ["99"], ...}
    p = _state(tmp_path) / "ever-registered.json"
    p.symlink_to(v)
    assert pm.load_ever_registered(str(p)) == set(), "read the link's target"
    assert pm.save_ever_registered({"11"}, str(p)) is True
    assert _untouched(v)
    assert p.is_file() and not p.is_symlink()
    assert json.loads(p.read_text()) == {"exts": ["11"]}


def test_ever_registered_failed_save_leaves_no_temporary_file(tmp_path):
    d = _state(tmp_path)
    (d / "ever-registered.json").mkdir()      # the move cannot land
    assert pm.save_ever_registered({"11"}, str(d / "ever-registered.json")) is False
    assert os.listdir(d) == ["ever-registered.json"]


# --------------------------------------------------------------------------- #
# devhealth: the private low-MOS capture
# --------------------------------------------------------------------------- #
def _judged():
    leg = {"ts": 5001.0, "tag": "wakeup", "loss_tx_pct": 0.0, "mes_rx": 88.0}
    return dh.judge_rtp_records(
        {"a": {"moscq": "2.2", "stopTimeSecond": "5000", "startTimeSecond": "4980"}}, [leg])


@pytest.fixture
def fresh_capture(monkeypatch):
    monkeypatch.setattr(dh, "_capture_seen", {"path": None, "keys": set()})


def test_capture_positive_control(tmp_path, fresh_capture):
    p = _state(tmp_path) / "cordless-mos.jsonl"
    assert dh.capture_low_mos(_judged(), 3.4, now=5100, path=str(p)) == 1
    assert p.is_file() and len(p.read_text().splitlines()) == 1
    assert _mode(p) == 0o600, "root writes, nobody else reads"


def test_capture_narrows_a_wider_existing_file_through_its_fd(tmp_path, fresh_capture):
    p = _state(tmp_path) / "cordless-mos.jsonl"
    p.write_text("")
    os.chmod(p, 0o644)
    assert dh.capture_low_mos(_judged(), 3.4, now=5100, path=str(p)) == 1
    assert _mode(p) == 0o600


def test_capture_never_appends_or_chmods_through_a_link(tmp_path, fresh_capture):
    """The append and the chmod after it went by name: root's rows, then mode
    0600, landed on the link's target. 0644 so a chmod to 0600 would show."""
    v = _victim(tmp_path, 0o644)
    p = _state(tmp_path) / "cordless-mos.jsonl"
    p.symlink_to(v)
    assert dh.capture_low_mos(_judged(), 3.4, now=5100, path=str(p)) == 0, \
        "a refused write is reported as not written, and retried next cycle"
    assert _untouched(v, 0o644)
    assert p.is_symlink(), "the planted name is left alone, never written through"


def test_capture_never_trims_through_a_link(tmp_path, fresh_capture, monkeypatch):
    """The trim read and rewrote the ledger by name, so a link turned it into a
    rewrite of its target. The cap is shrunk so a trim WOULD bite."""
    v = _victim(tmp_path, 0o644)
    p = _state(tmp_path) / "cordless-mos.jsonl"
    p.symlink_to(v)
    monkeypatch.setattr(dh, "CAPTURE_MAX_BYTES", 8)
    dh.capture_low_mos(_judged(), 3.4, now=5100, path=str(p))
    assert _untouched(v, 0o644)


def test_capture_does_not_read_its_dedupe_keys_through_a_link(tmp_path):
    v = tmp_path / "outside.jsonl"
    v.write_text(json.dumps({"dedupe": "from-the-target"}) + "\n")
    p = _state(tmp_path) / "cordless-mos.jsonl"
    p.symlink_to(v)
    assert dh._load_capture_keys(str(p)) == set()


# --------------------------------------------------------------------------- #
# The two shared stores: wakeup/store.py and webui/mwi_store.py
# --------------------------------------------------------------------------- #
def _set_wakeup(mod):
    mod.set_wakeup("11", "06:30")


def _set_mwi(mod):
    mod.set_flag("11", True)


STORES = [
    pytest.param(ws, _set_wakeup, "wakeups.json", id="wakeup"),
    pytest.param(mw, _set_mwi, "mwi.json", id="mwi"),
]


@pytest.mark.parametrize("mod,write,name", STORES)
def test_store_positive_control(tmp_path, monkeypatch, mod, write, name):
    d = _state(tmp_path)
    monkeypatch.setattr(mod, "PATH", str(d / name))
    write(mod)
    p, lock = d / name, d / (name + ".lock")
    assert "11" in json.loads(p.read_text())
    assert _mode(p) == 0o664, "root services and asterisk-user AGIs both write it"
    assert lock.is_file() and _mode(lock) == 0o664
    assert sorted(os.listdir(d)) == [name, name + ".lock"], "no temporary file left"


@pytest.mark.parametrize("mod,write,name", STORES)
def test_store_lock_never_opens_or_chmods_through_a_link(tmp_path, monkeypatch,
                                                         mod, write, name):
    """The lock was opened "a+" and chmodded 0664 BY NAME, at a fixed name: a
    link planted there made its target 0664 the next time a root service took
    the lock, with no race to win. Whatever the store call then does, the
    link's target is not touched."""
    v = _victim(tmp_path)
    d = _state(tmp_path)
    (d / (name + ".lock")).symlink_to(v)
    monkeypatch.setattr(mod, "PATH", str(d / name))
    try:
        write(mod)
    except OSError:
        pass
    assert _untouched(v)


@pytest.mark.parametrize("mod,write,name", STORES)
def test_store_does_not_copy_a_linked_target_into_itself(tmp_path, monkeypatch,
                                                        mod, write, name):
    """_read() followed a link in the store's place, and the write that came
    next copied the target's JSON (any root-readable object) into the
    group-readable store."""
    v = _victim(tmp_path)
    d = _state(tmp_path)
    (d / name).symlink_to(v)
    monkeypatch.setattr(mod, "PATH", str(d / name))
    write(mod)
    p = d / name
    assert _untouched(v)
    assert p.is_file() and not p.is_symlink(), "the replace puts a regular file back"
    held = json.loads(p.read_text())
    assert "secret" not in held and "exts" not in held, f"the target was copied in: {held}"
    assert "11" in held


@pytest.mark.parametrize("mod,write,name", STORES)
def test_store_mode_is_set_before_the_name_is_visible(tmp_path, monkeypatch,
                                                     mod, write, name):
    v = _victim(tmp_path)
    d = _state(tmp_path)
    monkeypatch.setattr(mod, "PATH", str(d / name))
    _swap_in_a_link_after_replace(monkeypatch, v, name)
    write(mod)
    assert _untouched(v)


@pytest.mark.parametrize("mod,write,name", STORES)
def test_store_read_refuses_a_fifo_without_hanging(tmp_path, monkeypatch,
                                                  mod, write, name):
    """O_NONBLOCK: a FIFO planted at the store's name must not hang a root
    service at the open. It reads as an empty store."""
    d = _state(tmp_path)
    os.mkfifo(d / name)
    monkeypatch.setattr(mod, "PATH", str(d / name))
    got = []
    # In a thread, so a regression fails here in seconds instead of hanging the
    # suite: open(2) on a FIFO with no writer blocks without O_NONBLOCK.
    t = threading.Thread(target=lambda: got.append(mod._read()), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "the read blocked on a planted FIFO"
    assert got == [{}]


# --------------------------------------------------------------------------- #
# The lock's policy for a planted name (see _open_lock): remove it, never what
# it points at, and create the lock afresh once; otherwise refuse the call.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mod,write,name", STORES)
def test_store_replaces_a_planted_lock_link_and_still_writes(tmp_path, monkeypatch,
                                                             mod, write, name):
    """Refusing would have kept the link, and the scheduler would log a tick
    error every 20 s while no wake-up rang."""
    v = _victim(tmp_path)
    d = _state(tmp_path)
    lock = d / (name + ".lock")
    lock.symlink_to(v)
    monkeypatch.setattr(mod, "PATH", str(d / name))
    write(mod)
    assert _untouched(v)
    assert lock.is_file() and not lock.is_symlink() and _mode(lock) == 0o664
    assert "11" in json.loads((d / name).read_text())


@pytest.mark.parametrize("mod,write,name", STORES)
def test_store_replaces_a_planted_lock_fifo(tmp_path, monkeypatch, mod, write, name):
    d = _state(tmp_path)
    lock = d / (name + ".lock")
    os.mkfifo(lock)
    monkeypatch.setattr(mod, "PATH", str(d / name))
    write(mod)
    assert lock.is_file()
    assert "11" in json.loads((d / name).read_text())


@pytest.mark.parametrize("mod,write,name", STORES)
def test_store_refuses_a_directory_at_the_lock(tmp_path, monkeypatch, mod, write, name):
    d = _state(tmp_path)
    lock = d / (name + ".lock")
    lock.mkdir()
    (lock / "kept").write_text("")
    monkeypatch.setattr(mod, "PATH", str(d / name))
    with pytest.raises(OSError):
        write(mod)
    assert (lock / "kept").exists(), "a directory is never removed"


@pytest.mark.parametrize("mod,write,name", STORES)
def test_store_lock_name_planted_again_in_the_gap_is_refused(tmp_path, monkeypatch,
                                                             mod, write, name):
    """Between the unlink and the create the asterisk user can plant the name
    again. The create is exclusive, so that fails the call; nothing follows."""
    v = _victim(tmp_path)
    d = _state(tmp_path)
    lock = d / (name + ".lock")
    lock.symlink_to(v)
    monkeypatch.setattr(mod, "PATH", str(d / name))
    real = os.unlink

    def unlink_then_replant(p, *a, **k):
        real(p, *a, **k)
        if os.path.basename(str(p)) == lock.name:
            os.symlink(v, p)

    monkeypatch.setattr(os, "unlink", unlink_then_replant)
    with pytest.raises(OSError):
        write(mod)
    assert _untouched(v)


@pytest.mark.parametrize("mod,write,name", STORES)
def test_store_lock_raises_an_ordinary_failure_and_keeps_the_name(tmp_path, monkeypatch,
                                                                 mod, write, name):
    """Only a link, a FIFO or a directory is a planted name. EACCES, ENOSPC
    and the rest are raised as they were, and the lock file is not removed."""
    import errno as _errno
    d = _state(tmp_path)
    lock = d / (name + ".lock")
    lock.write_text("")
    monkeypatch.setattr(mod, "PATH", str(d / name))

    def denied(path, flags):
        raise PermissionError(_errno.EACCES, "denied", path)

    monkeypatch.setattr(mod, "_open_no_follow", denied)
    with pytest.raises(PermissionError):
        write(mod)
    assert lock.is_file()


@pytest.mark.parametrize("helper", ["_open_no_follow", "_open_lock"])
def test_the_two_stores_carry_the_same_lock_helpers(helper):
    """Duplicated rather than imported: the stores live in separate directories
    and are loaded by separate interpreters (AGIs, services, CLIs). This keeps
    the copies from drifting."""
    import inspect
    assert inspect.getsource(getattr(ws, helper)) == inspect.getsource(getattr(mw, helper))


def test_capture_takes_back_a_file_the_asterisk_user_was_given(tmp_path, fresh_capture,
                                                               monkeypatch):
    """The live capture is `0600 asterisk:asterisk`: from v0.102.0 until v0.109.0
    the boot pass chowned every file in /data/state to the asterisk user, so the
    file the docs call root-only was theirs to read. Running as root, the append
    hands it back to root, through the fd. (Simulated: the suite is not root.)"""
    p = _state(tmp_path) / "cordless-mos.jsonl"
    p.write_text("")
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))
    assert dh.capture_low_mos(_judged(), 3.4, now=5100, path=str(p)) == 1
    assert calls == [(0, -1)], "owner taken back to root, group left as it is"
    calls.clear()
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)   # not root: no chown
    dh._capture_seen.update(path=None, keys=set())
    p.write_text("")
    assert dh.capture_low_mos(_judged(), 3.4, now=5100, path=str(p)) == 1
    assert calls == []


# --------------------------------------------------------------------------- #
# Root READS from /data/state: no link followed, no FIFO blocks the poller, no
# file of any size read whole. The two v0.119.1 latch reads, the ever-registered
# set, the call ledger devhealth reads every cycle, and the durable Asterisk log
# rtpmon reads for transitions.
# --------------------------------------------------------------------------- #
def _returns_quickly(fn):
    got = []
    t = threading.Thread(target=lambda: got.append(fn()), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "the read blocked on a planted FIFO"
    return got[0]


def _fresh_latch_json(now: float) -> str:
    return json.dumps({"alerted": True, "down_at": int(now) - 60, "announced": True,
                       "boot": None})


def test_outage_latch_read_positive_control(tmp_path):
    p = _state(tmp_path) / "link-outage.json"
    now = 1794103912.0
    p.write_text(_fresh_latch_json(now))
    assert pm.load_outage_alerted(str(p), now=now) is True


def test_outage_latch_read_refuses_a_link_a_fifo_and_an_oversized_file(tmp_path):
    now = 1794103912.0
    d = _state(tmp_path)
    target = tmp_path / "outside-latch.json"
    target.write_text(_fresh_latch_json(now))
    link = d / "link.json"
    link.symlink_to(target)
    assert pm.load_outage_alerted(str(link), now=now) is False, "read through a link"
    fifo = d / "fifo.json"
    os.mkfifo(fifo)
    assert _returns_quickly(lambda: pm.load_outage_alerted(str(fifo), now=now)) is False
    big = d / "big.json"
    big.write_text(" " * pm.STATE_JSON_MAX_BYTES + _fresh_latch_json(now))
    assert pm.load_outage_alerted(str(big), now=now) is False, "read past the bound"


def test_alert_latch_read_refuses_a_link_a_fifo_and_an_oversized_file(tmp_path):
    d = _state(tmp_path)
    assert dh.read_alert_latches(str(d / "absent.json")) == {}, "no file is no latch"
    ok = d / "ok.json"
    ok.write_text("{}")
    assert dh.read_alert_latches(str(ok)) == {}, "positive control"
    target = tmp_path / "outside-latches.json"
    target.write_text('{"gateway": {}}')
    link = d / "link.json"
    link.symlink_to(target)
    assert dh.read_alert_latches(str(link)) is None, "read through a link"
    fifo = d / "fifo.json"
    os.mkfifo(fifo)
    assert _returns_quickly(lambda: dh.read_alert_latches(str(fifo))) is None
    big = d / "big.json"
    big.write_text(" " * dh.ALERT_STATE_MAX_BYTES + "{}")
    assert dh.read_alert_latches(str(big)) is None, "read past the bound"


def test_ever_registered_read_survives_a_fifo_and_deep_nesting(tmp_path):
    """A file nested too deep raised RecursionError out of the load, at
    rtpmon's start, which the old except did not catch."""
    d = _state(tmp_path)
    fifo = d / "fifo.json"
    os.mkfifo(fifo)
    assert _returns_quickly(lambda: pm.load_ever_registered(str(fifo))) == set()
    deep = d / "deep.json"
    deep.write_text("[" * 30000 + "]" * 30000)
    assert pm.load_ever_registered(str(deep)) == set()


def test_call_ledger_read_does_not_block_on_a_fifo(tmp_path):
    fifo = _state(tmp_path) / "callqos.jsonl"
    os.mkfifo(fifo)
    assert _returns_quickly(lambda: dh.load_callqos_legs(str(fifo))) == []


def test_transitions_are_not_read_through_a_link(tmp_path, monkeypatch):
    target = tmp_path / "outside.log"
    target.write_text("[x] VERBOSE[1] res_pjsip: Endpoint 11 is now Unreachable\n")
    link = _state(tmp_path) / "asterisk.log"
    link.symlink_to(target)
    monkeypatch.setattr(pm, "ENDPOINT_LOG_PATH", str(link))
    monkeypatch.setattr(pm, "_transition_offset", 0)
    assert pm.endpoint_transitions() == []
    regular = tmp_path / "state" / "real.log"
    regular.write_text(target.read_text())
    monkeypatch.setattr(pm, "ENDPOINT_LOG_PATH", str(regular))
    monkeypatch.setattr(pm, "_transition_offset", 0)
    assert pm.endpoint_transitions() == [{"ext": "11", "state": "Unreachable"}], \
        "positive control: a regular log is read"
