"""Tests for the wake-up spoken-time parser + store. Plain python3, no deps.

    python3 switchboard/tests/test_wakeup.py
"""
import datetime
import os
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

WK = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share" / "switchboard" / "wakeup"
timeparse = SourceFileLoader("sw_timeparse", str(WK / "timeparse.py")).load_module()

# Point the store at a throwaway file BEFORE loading it (PATH is read at import).
os.environ["SWITCHBOARD_WAKEUPS"] = os.path.join(tempfile.mkdtemp(), "wakeups.json")
store = SourceFileLoader("sw_store", str(WK / "store.py")).load_module()

_failures = 0


def check(name, cond):
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    # Under pytest the print + counter are DECORATIVE — only the __main__
    # runner reads _failures, so a failing check would still 'pass' the
    # test. Assert too, so both harnesses actually enforce every check.
    assert cond, name


def eq(spoken, expected):
    got = timeparse.parse(spoken)
    check(f"parse {spoken!r} -> {expected}  (got {got})", got == expected)


def test_timeparse():
    # Bare / am-pm
    eq("seven am", "07:00")
    eq("seven a m", "07:00")
    eq("seven", "07:00")
    eq("seven o'clock", "07:00")
    eq("seven oclock", "07:00")
    eq("seven p m", "19:00")
    # Hour + minutes
    eq("seven thirty", "07:30")
    eq("seven thirty am", "07:30")
    eq("seven thirty pm", "19:30")
    eq("six forty five", "06:45")
    eq("seven oh five", "07:05")
    eq("seven o five", "07:05")
    eq("seven fifteen pm", "19:15")
    # past / to / quarter / half
    eq("half past six", "06:30")
    eq("quarter past seven", "07:15")
    eq("quarter to eight", "07:45")
    eq("ten to seven", "06:50")
    eq("twenty past six", "06:20")
    # noon / midnight / twelve
    eq("noon", "12:00")
    eq("midnight", "00:00")
    eq("twelve thirty pm", "12:30")
    eq("twelve am", "00:00")
    eq("twelve pm", "12:00")
    # digit clocks
    eq("7:30", "07:30")
    eq("07:30", "07:30")
    eq("19:30", "19:30")
    # military
    eq("nineteen thirty", "19:30")
    eq("seven hundred", "07:00")
    eq("nineteen hundred", "19:00")
    # time-of-day words
    eq("eight in the morning", "08:00")
    eq("eight in the evening", "20:00")
    # "afternoon" must NOT collide with the "noon" substring (review HIGH)
    eq("two in the afternoon", "14:00")
    eq("five in the afternoon", "17:00")
    eq("four o'clock in the afternoon", "16:00")
    eq("at noon", "12:00")
    eq("high noon", "12:00")
    # military with a leading filler word
    eq("oh seven hundred", "07:00")
    eq("zero seven thirty", "07:30")
    # leading disfluency / lead-in must not reject a clearly-spoken time (audit)
    eq("um seven thirty", "07:30")
    eq("uh seven thirty", "07:30")
    eq("so seven thirty", "07:30")
    eq("okay seven thirty", "07:30")
    eq("make it seven thirty", "07:30")
    eq("how about seven thirty", "07:30")
    eq("around seven", "07:00")
    eq("um quarter past six", "06:15")
    # relative "to" must resolve am/pm on the TARGET hour before subtracting (audit):
    # "quarter to one pm" is 12:45, not 00:45.
    eq("quarter to one pm", "12:45")
    eq("quarter to twelve", "11:45")
    eq("twenty to eight", "07:40")
    # nonsense -> None
    eq("hello there operator", None)
    eq("", None)
    eq("kitchen please", None)
    eq("um uh so", None)


def test_store_set_get():
    now = 1781000000.0
    base = datetime.datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0)
    ahead = base + datetime.timedelta(hours=1)
    e = store.set_wakeup("11", ahead.strftime("%H:%M"), now_epoch=now)
    check("store: target is today when time is still ahead", e["target_epoch"] == int(ahead.timestamp()))
    check("store: get returns the entry", store.get("11")["hhmm"] == ahead.strftime("%H:%M"))
    check("store: all_wakeups includes it", "11" in store.all_wakeups())
    behind = base - datetime.timedelta(hours=1)
    e2 = store.set_wakeup("12", behind.strftime("%H:%M"), now_epoch=now)
    check("store: target rolls to tomorrow when time has passed",
          e2["target_epoch"] == int((behind + datetime.timedelta(days=1)).timestamp()))
    check("store: cancel returns True + removes", store.cancel("12") is True and store.get("12") is None)
    check("store: cancel of a missing ext is False", store.cancel("99") is False)


def test_store_cancel_if():
    now = 1781000000.0
    e = store.set_wakeup("13", "06:30", now_epoch=now)
    tgt = e["target_epoch"]
    # Wrong epoch (e.g. it was re-set) must NOT delete it.
    check("cancel_if: stale epoch does not remove", store.cancel_if("13", tgt - 999) is False and store.get("13") is not None)
    # Matching epoch removes it.
    check("cancel_if: matching epoch removes", store.cancel_if("13", tgt) is True and store.get("13") is None)


def test_store_due():
    for k in list(store.all_wakeups()):
        store.cancel(k)
    now = 1781000000.0
    store.set_wakeup("11", "07:00", now_epoch=now)
    tgt = store.get("11")["target_epoch"]
    fired, missed = store.due(tgt - 10)
    check("due: before the time -> nothing", not fired and not missed)
    fired, missed = store.due(tgt + 5)
    check("due: at/after the time within grace -> fired", any(x[0] == "11" for x in fired))
    check("due: a fired wake-up is left for the scheduler to remove", store.get("11") is not None)
    fired, missed = store.due(tgt + store.GRACE_SECONDS + 60)
    check("due: past the grace window -> missed and removed",
          any(x[0] == "11" for x in missed) and store.get("11") is None)


def main():
    test_timeparse()
    test_store_set_get()
    test_store_cancel_if()
    test_store_due()
    print()
    if _failures:
        print(f"{_failures} FAILURE(S)")
        raise SystemExit(1)
    print("all wakeup tests passed")


if __name__ == "__main__":
    main()


# ── per-room wake-up scenes (v0.45.0) ────────────────────────────────────────

import importlib.machinery as _mach  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_sp = _mach.SourceFileLoader(
    "agi_speech_forscenes",
    str(_Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
        / "switchboard" / "operator" / "agi_speech.py")).load_module()


def test_channel_ext_parses_pjsip_channels() -> None:
    assert _sp.channel_ext({"agi_channel": "PJSIP/12-0000000a"}) == "12"
    assert _sp.channel_ext({"agi_channel": "PJSIP/19-00000001"}) == "19"
    # Not a room endpoint → "" so the caller falls back instead of guessing.
    assert _sp.channel_ext({"agi_channel": "Local/s@wakeup-deliver-0001;2"}) == ""
    assert _sp.channel_ext({"agi_channel": "PJSIP/trunk-00000005"}) == ""
    assert _sp.channel_ext({"agi_channel": ""}) == ""
    assert _sp.channel_ext({}) == ""


def test_wakeup_scene_for_prefers_room_then_falls_back() -> None:
    wk = {"scene": "scene.house_default",
          "scenes": {"12": "scene.wakeup_kitchen", "19": "scene.wakeup_master_bedroom"}}
    assert _sp.wakeup_scene_for(wk, "12") == "scene.wakeup_kitchen"
    assert _sp.wakeup_scene_for(wk, "19") == "scene.wakeup_master_bedroom"
    # A room with no entry of its own keeps the whole-house scene: adding
    # per-room scenes must never silently drop existing behavior.
    assert _sp.wakeup_scene_for(wk, "13") == "scene.house_default"
    assert _sp.wakeup_scene_for(wk, "") == "scene.house_default"
    # No global scene configured and no per-room entry → nothing fires.
    assert _sp.wakeup_scene_for({"scenes": {"12": "scene.k"}}, "13") == ""
    assert _sp.wakeup_scene_for({}, "12") == ""
    # Malformed 'scenes' must not raise.
    assert _sp.wakeup_scene_for({"scene": "scene.a", "scenes": "nope"}, "12") == "scene.a"


def test_every_wakeup_attempt_leaves_a_record(tmp_path) -> None:
    """A wake-up that rings out must not vanish.

    The QoS ledger is written from the dialplan's hangup extension, so it can
    only ever describe a leg that ANSWERED. The 06:18 wake-up on 2026-09-03 rang
    ext 19 and produced no record of any kind -- "ring queued=True", "Called 19",
    "is ringing", then silence for 98 minutes. Nothing distinguished "the phone
    never rang" from "the user ignored it", on an alarm clock.

    A ring-queued record with no matching call record IS the no-answer signal."""
    import json as _json
    import sys as _sys
    from importlib.machinery import SourceFileLoader

    webui = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
             / "switchboard" / "webui")
    _sys.path.insert(0, str(webui))
    delivery = SourceFileLoader("delivery", str(webui / "delivery.py")).load_module()

    out = tmp_path / "sub" / "delivery.jsonl"     # nested: the writer must mkdir
    real = delivery.OUTCOME_PATH
    delivery.OUTCOME_PATH = str(out)
    try:
        delivery.record("19", "wakeup", "ring-queued", hhmm="06:18", ring_seconds=60)
        delivery.record("14", "wakeup", "deferred", hhmm="07:00",
                        device_state="In use")
        delivery.record("19", "wakeup", "originate-refused", hhmm="06:30")
        delivery.record("19", "announce", "unreachable", device_state="UNAVAILABLE")
    finally:
        delivery.OUTCOME_PATH = real

    recs = [_json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    check("wakeup record: one line per attempt", len(recs) == 4)
    check("wakeup record: every line is dated", all(r.get("ts") for r in recs))
    check("wakeup record: the ring is recorded",
          recs[0]["kind"] == "wakeup" and recs[0]["outcome"] == "ring-queued")
    check("wakeup record: it carries the scheduled time", recs[0]["hhmm"] == "06:18")
    check("wakeup record: and the ring window, so a no-answer is datable",
          recs[0]["ring_seconds"] == 60)
    check("wakeup record: a deferral is recorded, not just logged",
          recs[1]["outcome"] == "deferred" and recs[1]["device_state"] == "In use")
    check("wakeup record: a refused originate is distinguishable from a ring",
          recs[2]["outcome"] == "originate-refused")
    # Wake-ups and announcements share the file and must stay distinguishable.
    check("wakeup record: kind separates the alarm clock from announcements",
          [r["kind"] for r in recs] == ["wakeup", "wakeup", "wakeup", "announce"])
    check("wakeup record: absent optionals are omitted, not written as null",
          "device_state" not in recs[0] and "hhmm" not in recs[3])

    # An unwritable path must never stop an alarm ringing.
    delivery.OUTCOME_PATH = "/proc/cannot/write/here.jsonl"
    try:
        delivery.record("19", "wakeup", "ring-queued")
        check("wakeup record: an unwritable path is swallowed", True)
    finally:
        delivery.OUTCOME_PATH = real


def test_tick_records_the_attempt_it_actually_made(tmp_path) -> None:
    """tick() must WRITE the record, not merely be able to.

    Recording is done by a shared helper with its own tests -- but a helper that
    is never called is worth nothing, and mutation testing showed both call
    sites in tick() surviving while the helper was fully covered. That is the
    eighth gap of this shape in this work, so the caller gets driven directly.

    Two paths matter: a wake-up that fires (ring-queued) and one deferred
    because the room was busy at its appointed minute. Both were previously only
    lines in a container log that carries no timestamps."""
    import json as _json
    import sys as _sys
    from importlib.machinery import SourceFileLoader

    webui = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
             / "switchboard" / "webui")
    _sys.path.insert(0, str(webui))
    delivery = SourceFileLoader("delivery", str(webui / "delivery.py")).load_module()

    out = tmp_path / "d.jsonl"
    saved_path = delivery.OUTCOME_PATH
    delivery.OUTCOME_PATH = str(out)

    # scheduler.py resolves store/ami/ha_client at IMPORT time from absolute
    # container paths, so pre-register stand-ins before loading it.
    class _Pre:
        @staticmethod
        def due(now): return ([], [])
        @staticmethod
        def cancel_if(ext, epoch): return True
        @staticmethod
        def get_endpoints(): return []
        @staticmethod
        def originate_wakeup(ext, ring): return True
        @staticmethod
        def notify(*a, **k): return True

    pre_saved = {k: _sys.modules.get(k) for k in ("store", "ami", "ha_client", "delivery")}
    for k in ("store", "ami", "ha_client"):
        _sys.modules[k] = _Pre
    _sys.modules["delivery"] = delivery
    try:
        sched = SourceFileLoader(
            "sw_scheduler",
            str(Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
                / "switchboard" / "wakeup" / "scheduler.py")).load_module()
    finally:
        for k, v in pre_saved.items():
            if v is None:
                _sys.modules.pop(k, None)
            else:
                _sys.modules[k] = v

    saved = {k: getattr(sched, k, None) for k in ("store", "ami", "ha_client", "_delivery")}
    try:
        sched._delivery = delivery

        class _Store:
            @staticmethod
            def due(now):
                return ([("19", {"hhmm": "06:18", "target_epoch": now}),
                         ("14", {"hhmm": "07:00", "target_epoch": now})], [])
            @staticmethod
            def cancel_if(ext, epoch): return True

        class _AMI:
            @staticmethod
            def get_endpoints():
                # The real shape: a list of {"name", "state"} dicts. An earlier
                # version of this stub invented endpoint_states(), which made
                # tick() take the AMI-down path and defer BOTH wake-ups -- the
                # recording was correct, the fixture was not.
                return [{"name": "19", "state": "Not in use"},   # idle -> rings
                        {"name": "14", "state": "In use"}]        # busy -> deferred
            @staticmethod
            def originate_wakeup(ext, ring): return True

        sched.store = _Store
        sched.ami = _AMI
        sched.ha_client = None
        sched.tick()
    finally:
        for k, v in saved.items():
            if v is not None:
                setattr(sched, k, v)
        delivery.OUTCOME_PATH = saved_path

    recs = [_json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    by_ext = {r["ext"]: r for r in recs}
    check("tick: the wake-up that fired is recorded", "19" in by_ext)
    check("tick: it is recorded as a queued ring",
          by_ext.get("19", {}).get("outcome") == "ring-queued")
    check("tick: with the scheduled time, so a no-answer is datable",
          by_ext.get("19", {}).get("hhmm") == "06:18")
    check("tick: the DEFERRED wake-up is recorded too", "14" in by_ext)
    check("tick: named as deferred, with the state that caused it",
          by_ext.get("14", {}).get("outcome") == "deferred"
          and by_ext.get("14", {}).get("device_state") == "In use")
    check("tick: both are tagged as wake-ups, not announcements",
          all(r["kind"] == "wakeup" for r in recs))


# ── v0.70.0: a ring that is never answered must be DETECTED ─────────────────
#
# THE FAILURE THIS PINS. `ami.originate_wakeup` returns True the moment AMI
# accepts the request. The scheduler treated that as delivery — it wrote
# `ring-queued` and consumed the wake-up — so a ring-out and a wake-up that
# actually woke somebody produced byte-identical records. Measured on the live
# system: 2026-09-03 06:18 and 2026-09-04 06:12 both rang ext 19 unanswered and
# every ledger read healthy.
#
# [wakeup-deliver] runs ONLY on answer and now records `answered`, so the two
# facts can finally be joined. These tests assert the join, not the strings.

def _reconciler(tmp_path):
    """Load the scheduler with delivery pointed at a temp ledger."""
    import sys as _sys
    from importlib.machinery import SourceFileLoader
    webui = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
             / "switchboard" / "webui")
    _sys.path.insert(0, str(webui))
    delivery = SourceFileLoader("delivery", str(webui / "delivery.py")).load_module()
    delivery.OUTCOME_PATH = str(tmp_path / "d.jsonl")

    class _Pre:
        @staticmethod
        def due(now): return ([], [])
        @staticmethod
        def cancel_if(ext, epoch): return True
        @staticmethod
        def get_endpoints(): return []
        @staticmethod
        def originate_wakeup(ext, ring): return True
        @staticmethod
        def notify(*a, **k): return True
        @staticmethod
        def push(*a, **k): return True

    pre = {k: _sys.modules.get(k) for k in ("store", "ami", "ha_client", "delivery")}
    for k in ("store", "ami", "ha_client"):
        _sys.modules[k] = _Pre
    _sys.modules["delivery"] = delivery
    try:
        sched = SourceFileLoader(
            "sw_sched_reconcile",
            str(Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
                / "switchboard" / "wakeup" / "scheduler.py")).load_module()
    finally:
        for k, v in pre.items():
            if v is None: _sys.modules.pop(k, None)
            else: _sys.modules[k] = v
    sched._delivery = delivery
    return sched, delivery


def test_ringout_is_detected_and_rung_again(tmp_path):
    sched, delivery = _reconciler(tmp_path)
    rings, pushes = [], []

    class _AMI:
        @staticmethod
        def originate_wakeup(ext, ring): rings.append(ext); return True
    class _HA:
        @staticmethod
        def push(msg, title="", target="", critical=False):
            pushes.append({"critical": critical, "target": target}); return True
        @staticmethod
        def notify(*a, **k): return True
    sched.ami, sched.ha_client = _AMI, _HA

    t0 = 1_000_000.0
    sched._ringing.clear()
    sched._ringing["19"] = {"target_epoch": t0, "hhmm": "06:18", "started": t0, "retried": False}

    # Too early to judge — the phone may still be ringing.
    sched._reconcile_rings(t0 + sched.RETRY_AFTER - 1)
    check("reconcile: does not judge a ring that is still in its window",
          rings == [] and "19" in sched._ringing)

    # Past the window with no `answered` record: ring again.
    sched._reconcile_rings(t0 + sched.RETRY_AFTER + 1)
    check("reconcile: an unanswered ring is rung a SECOND time", rings == ["19"])
    check("reconcile: the retry is remembered", sched._ringing["19"]["retried"] is True)
    check("reconcile: no push yet — the second ring has not been judged", pushes == [])


def test_an_answered_wakeup_is_never_escalated(tmp_path):
    """The control. Without this, a fix that escalates everything would pass."""
    sched, delivery = _reconciler(tmp_path)
    rings, pushes = [], []

    class _AMI:
        @staticmethod
        def originate_wakeup(ext, ring): rings.append(ext); return True
    class _HA:
        @staticmethod
        def push(*a, **k): pushes.append(1); return True
        @staticmethod
        def notify(*a, **k): return True
    sched.ami, sched.ha_client = _AMI, _HA

    t0 = 1_000_000.0
    sched._ringing.clear()
    sched._ringing["19"] = {"target_epoch": t0, "hhmm": "06:18", "started": t0, "retried": False}
    # [wakeup-deliver] reached — the leg answered.
    delivery.record("19", "wakeup", "answered")

    sched._reconcile_rings(t0 + sched.RETRY_AFTER + 1)
    check("reconcile: an ANSWERED wake-up is not rung again", rings == [])
    check("reconcile: an ANSWERED wake-up raises no alert", pushes == [])
    check("reconcile: the answered ring stops being tracked", "19" not in sched._ringing)


def test_two_unanswered_rings_escalate_audibly(tmp_path):
    sched, delivery = _reconciler(tmp_path)
    pushes, cards = [], []

    class _AMI:
        @staticmethod
        def originate_wakeup(ext, ring): return True
    class _HA:
        @staticmethod
        def push(msg, title="", target="", critical=False):
            pushes.append({"critical": critical, "target": target, "msg": msg}); return True
        @staticmethod
        def notify(msg, title="", notification_id=""): cards.append(msg); return True
    sched.ami, sched.ha_client = _AMI, _HA

    t0 = 1_000_000.0
    sched._ringing.clear()
    sched._ringing["19"] = {"target_epoch": t0, "hhmm": "06:18", "started": t0, "retried": True}
    sched._reconcile_rings(t0 + sched.RETRY_AFTER + 1)

    check("escalate: a second unanswered ring pushes to the phone", len(pushes) == 1)
    check("escalate: the push is CRITICAL so it sounds through Do Not Disturb",
          pushes and pushes[0]["critical"] is True)
    check("escalate: it names the time so the message is actionable",
          pushes and "06:18" in pushes[0]["msg"])
    check("escalate: no duplicate drawer card when the push succeeded", cards == [])
    check("escalate: tracking stops after the final verdict", "19" not in sched._ringing)


def test_push_failure_falls_back_to_the_drawer_card(tmp_path):
    """A dead push target must not swallow the signal entirely."""
    sched, delivery = _reconciler(tmp_path)
    cards = []

    class _AMI:
        @staticmethod
        def originate_wakeup(ext, ring): return True
    class _HA:
        @staticmethod
        def push(*a, **k): return False        # e.g. the companion app is gone
        @staticmethod
        def notify(msg, title="", notification_id=""): cards.append(msg); return True
    sched.ami, sched.ha_client = _AMI, _HA

    t0 = 1_000_000.0
    sched._ringing.clear()
    sched._ringing["19"] = {"target_epoch": t0, "hhmm": "06:18", "started": t0, "retried": True}
    sched._reconcile_rings(t0 + sched.RETRY_AFTER + 1)
    check("escalate: a failed push falls back to the notification card", len(cards) == 1)


def test_outcomes_since_respects_ext_kind_outcome_and_time(tmp_path):
    """The join itself. A reader that matches too loosely would mark a ring-out
    answered — the exact failure it exists to catch, inverted."""
    import sys as _sys
    from importlib.machinery import SourceFileLoader
    webui = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
             / "switchboard" / "webui")
    delivery = SourceFileLoader("delivery_join", str(webui / "delivery.py")).load_module()
    delivery.OUTCOME_PATH = str(tmp_path / "j.jsonl")
    import time as _t
    t0 = _t.time()
    delivery.record("19", "wakeup", "answered")
    check("join: finds the matching record", delivery.outcomes_since("19", "wakeup", "answered", t0 - 5))
    check("join: a different ext does not match", not delivery.outcomes_since("14", "wakeup", "answered", t0 - 5))
    check("join: a different kind does not match", not delivery.outcomes_since("19", "announce", "answered", t0 - 5))
    check("join: a different outcome does not match", not delivery.outcomes_since("19", "wakeup", "ring-queued", t0 - 5))
    check("join: a record from BEFORE the ring does not count",
          not delivery.outcomes_since("19", "wakeup", "answered", t0 + 3600))
    check("join: a missing file is not an answer",
          not SourceFileLoader("d2", str(webui / "delivery.py")).load_module().outcomes_since("19", "wakeup", "answered", 0))


def test_an_unwritable_ledger_is_not_read_as_nobody_answered(tmp_path):
    """THE REGRESSION THIS CLOSES, and it was live.

    v0.70.0's reconciler asks "was an `answered` record written?". If the ledger
    cannot be WRITTEN at all, that answer is always no — for a wake-up somebody
    picked up exactly as much as for one that rang out.

    On the live system /share/switchboard was created root-owned 0644 while the
    AGI runs as `asterisk`, so every `answered` write failed with EACCES and
    `delivery.record()` swallowed it. Every ANSWERED wake-up would therefore have
    been rung a second time and then escalated with a critical, DND-bypassing
    push telling the owner nobody had picked up.

    A missing instrument is not a measurement. Escalating on one is worse than
    not escalating at all."""
    sched, delivery = _reconciler(tmp_path)
    rings, pushes = [], []

    class _AMI:
        @staticmethod
        def originate_wakeup(ext, ring): rings.append(ext); return True
    class _HA:
        @staticmethod
        def push(*a, **k): pushes.append(1); return True
        @staticmethod
        def notify(*a, **k): return True
    sched.ami, sched.ha_client = _AMI, _HA
    delivery.is_writable = lambda: False        # the live EACCES condition

    t0 = 1_000_000.0
    sched._ringing.clear()
    sched._ringing["19"] = {"target_epoch": t0, "hhmm": "06:18", "started": t0, "retried": False}
    sched._reconcile_rings(t0 + sched.RETRY_AFTER + 1)

    check("an unwritable ledger does not trigger a second ring", rings == [])
    check("an unwritable ledger does not fire a critical push", pushes == [])
    check("the unjudgeable ring stops being tracked", "19" not in sched._ringing)

    # And the control: with a writable ledger the same setup DOES escalate, so
    # the guard cannot be a blanket "never escalate".
    delivery.is_writable = lambda: True
    sched._ringing["19"] = {"target_epoch": t0, "hhmm": "06:18", "started": t0, "retried": False}
    sched._reconcile_rings(t0 + sched.RETRY_AFTER + 1)
    check("a WRITABLE ledger still detects the ring-out", rings == ["19"])


def test_record_reports_whether_it_actually_wrote(tmp_path):
    """`record()` swallowing EACCES is what made the live failure invisible."""
    import sys as _sys
    from importlib.machinery import SourceFileLoader
    webui = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
             / "switchboard" / "webui")
    d = SourceFileLoader("delivery_rep", str(webui / "delivery.py")).load_module()
    d.OUTCOME_PATH = str(tmp_path / "ok.jsonl")
    check("a successful write reports True", d.record("19", "wakeup", "answered") is True)
    # Point it at a path that cannot be created: a FILE used as a directory.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    d.OUTCOME_PATH = str(blocker / "nested" / "x.jsonl")
    check("a failed write reports False, it does not pretend to have succeeded",
          d.record("19", "wakeup", "answered") is False)


def test_is_writable_actually_probes_the_filesystem(tmp_path):
    """The reconciler's fail-safe is only as good as this probe.

    A previous version of this suite stubbed `is_writable` in every test, so a
    mutant that made it return True unconditionally survived — the guard would
    have been re-armed into the exact regression it exists to prevent, with a
    green suite. This exercises the real function."""
    import os as _os
    from importlib.machinery import SourceFileLoader
    webui = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
             / "switchboard" / "webui")
    d = SourceFileLoader("delivery_probe", str(webui / "delivery.py")).load_module()

    ok = tmp_path / "w" / "d.jsonl"
    d.OUTCOME_PATH = str(ok)
    check("a creatable path is writable", d.is_writable() is True)

    d.record("19", "wakeup", "answered")
    check("an existing writable file is writable", d.is_writable() is True)

    # Now make the real file unwritable and confirm the probe NOTICES.
    _os.chmod(ok, 0o444)
    try:
        # root ignores the mode bits, so only assert when the test user is not root.
        if _os.geteuid() != 0:
            check("a read-only ledger reports NOT writable", d.is_writable() is False)
        else:
            check("running as root — mode bits do not apply, probe skipped", True)
    finally:
        _os.chmod(ok, 0o644)

    # A path whose parent is a FILE cannot be created at all.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    d.OUTCOME_PATH = str(blocker / "nested" / "x.jsonl")
    check("an uncreatable path reports NOT writable", d.is_writable() is False)
