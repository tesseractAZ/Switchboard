"""A wake-up the room snoozes from its own phone is not a failed wake-up.

    python3 -m pytest switchboard/tests/test_wakeup_snooze.py

★ 2026-09-14, 05:40 to 06:25 MST. Ext 19 dialled 42 during three of its own
ringing wake-ups and each time spoke a later time into the handset (12:42:13Z,
13:10:20Z, 13:20:35Z). The reconciler joined only on the delivery milestones, so
it rang the phone a second time after two of those snoozes and sent three
critical, Do-Not-Disturb-bypassing pushes saying nobody had picked up — to the
person who had just picked the phone up to say when to call back.

THE RULE (an owner decision): a wake-up SET or CANCEL dialled on the ringing
room's OWN phone, at or after the judged ring started, means somebody there is
awake. No second ring, no push, and its own outcome, `snoozed`. The same change
from the dashboard or the console does NOT count — whoever made it may be setting
it for a sleeper — and neither does a change dialled before the ring began.

These tests drive the real dial-42 AGI (the only thing that writes `phone`) into
the real scheduler tick, against a real store and a real ledger, on a clock that
replays the morning. The dashboard and console setters are driven through their
own callers in test_app.py and test_console.py.
"""
import datetime
import json
import os
import sys
import time
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHARE = ROOT / "rootfs" / "usr" / "share" / "switchboard"
AGI = ROOT / "rootfs" / "var" / "lib" / "asterisk" / "agi-bin" / "switchboard-wakeup.agi"


def Z(hms: str, day: int = 14) -> float:
    """An epoch for HH:MM:SS UTC on 2026-09-<day>."""
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime.datetime(2026, 9, day, h, m, s,
                             tzinfo=datetime.timezone.utc).timestamp()


@pytest.fixture
def mst():
    """The live system's zone: MST, UTC-7, no daylight saving.

    store.next_epoch() turns "06:20" into the NEXT local 06:20, so the replay's
    spoken times only land on the incident's epochs in the zone they were spoken
    in. A POSIX TZ string rather than a zone name, so no tz database is needed.
    """
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "MST7"
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def _clocked(clock):
    """A `datetime` module whose now() is the replay clock, for store + delivery."""
    class _DT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime.fromtimestamp(clock[0], tz)
    return types.SimpleNamespace(datetime=_DT, timezone=datetime.timezone,
                                 timedelta=datetime.timedelta)


class _World:
    """The real store, ledger, scheduler and dial-42 AGI on one fake clock."""

    def __init__(self, tmp_path):
        self.clock = [Z("00:00:00")]
        self.rings, self.pushes, self.cards, self.logs = [], [], [], []

        self.delivery = SourceFileLoader(
            "delivery_snooze", str(SHARE / "webui" / "delivery.py")).load_module()
        self.delivery.OUTCOME_PATH = str(tmp_path / "delivery-outcomes.jsonl")
        self.delivery.datetime = _clocked(self.clock)

        self.store = SourceFileLoader(
            "store_snooze", str(SHARE / "wakeup" / "store.py")).load_module()
        self.store.PATH = str(tmp_path / "wakeups.json")
        self.store.datetime = _clocked(self.clock)

        world = self

        class _AMI:
            @staticmethod
            def get_endpoints():
                return [{"name": "14", "state": "Not in use"},
                        {"name": "19", "state": "Not in use"}]

            @staticmethod
            def originate_wakeup(ext, ring):
                world.rings.append((world.clock[0], ext))
                return True

        class _HA:
            @staticmethod
            def push(msg, **k):
                world.pushes.append(msg)
                return True

            @staticmethod
            def notify(msg, **k):
                world.cards.append(msg)
                return True

        saved_mods = {k: sys.modules.get(k)
                      for k in ("store", "ami", "ha_client", "delivery")}
        saved_path = list(sys.path)
        sys.modules["store"] = self.store
        sys.modules["ami"] = _AMI
        sys.modules["ha_client"] = _HA
        sys.modules["delivery"] = self.delivery
        try:
            self.sched = SourceFileLoader(
                "sched_snooze", str(SHARE / "wakeup" / "scheduler.py")).load_module()
            self.agi = SourceFileLoader("wakeup_agi_snooze", str(AGI)).load_module()
        finally:
            sys.path[:] = saved_path
            for k, v in saved_mods.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

        # The live options on 2026-09-14: wakeup_ring_seconds 90,
        # wakeup_retry_seconds 120.
        self.sched.RING, self.sched.RETRY_AFTER = 90, 120
        self.sched.time = types.SimpleNamespace(time=lambda: self.clock[0])
        self.sched.log = self.logs.append
        self.sched._ringing.clear()
        assert self.sched.store is self.store and self.sched._delivery is self.delivery
        assert self.agi.store is self.store and self.agi.delivery is self.delivery

    def at(self, t):
        self.clock[0] = t
        return self

    def dial_42(self, ext, heard):
        """`ext` dials the wake-up code and the recogniser hears `heard`."""
        self.agi.read_env = lambda: {"agi_channel": f"PJSIP/{ext}-0000000d"}
        self.agi.agi = lambda cmd: "200 result=0"
        self.agi.recognize = lambda attempt: heard
        self.agi.main()

    def milestone(self, ext, outcome):
        """What [wakeup-deliver] and the hangup handler write on a live leg."""
        self.delivery.record(ext, "wakeup", outcome)

    def tick(self):
        self.sched.tick()

    def rows(self, **match):
        p = Path(self.delivery.OUTCOME_PATH)
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] \
            if p.exists() else []
        return [r for r in rows if all(r.get(k) == v for k, v in match.items())]

    def run(self, events, start, end):
        """Replay `events` [(t, fn)] between scheduler ticks every 20 s."""
        events = sorted(events, key=lambda e: e[0])
        t = start
        while t <= end:
            while events and events[0][0] <= t:
                et, fn = events.pop(0)
                self.at(et)
                fn()
            self.at(t).tick()
            t += 20
        assert not events, "an event fell after the last tick"


# --------------------------------------------------------------------------- #
# 1. The morning itself.
# --------------------------------------------------------------------------- #
def test_the_morning_of_2026_09_14_replayed(tmp_path, mst):
    """★ The whole 12:40-13:29Z sequence, through the real tick.

    Before the fix this exact replay rings ext 19 at 13:12:03 and 13:22:03 and
    pushes four times. After it, the only push is ext 14's — which nobody
    answered and nobody snoozed — and all three snoozes stand down.
    """
    w = _World(tmp_path)
    # The night before: ext 19 set its 05:40 by phone (21:34 MST), and ext 14's
    # 05:50 was already in the store with no ledger row, as it was that morning.
    w.at(Z("16:53:40", day=13)).store.set_wakeup("14", "05:50")
    w.at(Z("04:34:25")).dial_42("19", "05:40")

    events = [
        (Z("12:42:25"), lambda: w.dial_42("19", "06:00")),   # snooze during the RE-ring
        (Z("13:00:11"), lambda: w.milestone("19", "answered")),
        (Z("13:00:18"), lambda: w.milestone("19", "spoken")),
        (Z("13:00:45"), lambda: w.dial_42("19", "06:10")),   # after a DELIVERED ring
        (Z("13:10:31"), lambda: w.dial_42("19", "06:20")),   # snooze during the ring
        (Z("13:20:46"), lambda: w.dial_42("19", "06:25")),   # snooze during the ring
        (Z("13:25:10"), lambda: w.milestone("19", "answered")),  # picked up in silence
        (Z("13:27:13"), lambda: w.milestone("19", "answered")),
        (Z("13:27:18"), lambda: w.milestone("19", "spoken")),
    ]
    w.run(events, Z("12:40:03"), Z("13:29:03"))

    rang = [(datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
             .strftime("%H:%M:%S"), ext) for t, ext in w.rings]
    assert rang == [
        ("12:40:03", "19"), ("12:42:03", "19"),   # 05:40, re-rung: no set yet
        ("12:50:03", "14"), ("12:52:03", "14"),   # 05:50, ext 14 never snoozed
        ("13:00:03", "19"),                       # 06:00, delivered
        ("13:10:03", "19"),                       # 06:10 — NOT rung again at 13:12:03
        ("13:20:03", "19"),                       # 06:20 — NOT rung again at 13:22:03
        ("13:25:03", "19"), ("13:27:03", "19"),   # 06:25, silent pickup still re-rung
    ], rang

    assert len(w.pushes) == 1, w.pushes
    assert "05:50" in w.pushes[0] and "extension 14" in w.pushes[0], w.pushes
    assert w.cards == []
    assert w.rows(ext="19", outcome="undelivered") == []
    assert [r["reason"] for r in w.rows(ext="14", outcome="undelivered")] == ["no-answer"]

    snoozed = [(r["hhmm"], r["new_hhmm"], r["attempt"], r["change"])
               for r in w.rows(outcome="snoozed")]
    assert snoozed == [("05:40", "06:00", 2, "set"),
                       ("06:10", "06:20", 1, "set"),
                       ("06:20", "06:25", 1, "set")], snoozed
    assert all(r["ext"] == "19" and r["kind"] == "wakeup"
               for r in w.rows(outcome="snoozed"))

    # A heard wake-up stays DELIVERED even though the room set another after it.
    delivered = [m for m in w.logs if m.endswith(") DELIVERED")]
    assert len(delivered) == 2 and "(06:00)" in delivered[0] and "(06:25)" in delivered[1]
    # ...and the phone set from the night before did not excuse the 05:40 ring.
    assert w.rows(ext="19", outcome="no-answer", hhmm="05:40")


# --------------------------------------------------------------------------- #
# 2. What counts, and what does not.
# --------------------------------------------------------------------------- #
def _ring(w, ext, hhmm, t_set, t_ring):
    """Put `hhmm` in the store at `t_set` and let the tick at `t_ring` ring it."""
    w.at(t_set).store.set_wakeup(ext, hhmm)
    w.at(t_ring).tick()
    assert w.rings[-1] == (t_ring, ext), w.rings


def test_a_phone_cancel_during_the_ring_is_a_snooze_too(tmp_path, mst):
    """"Cancel" said to stop the ringing removes NOTHING — the scheduler already
    consumed the entry when it rang — and is exactly as awake as a new time."""
    w = _World(tmp_path)
    _ring(w, "19", "06:10", Z("13:00:45"), Z("13:10:03"))
    w.at(Z("13:10:31")).dial_42("19", "CANCEL")
    cancel = w.rows(outcome="cancelled")
    assert [(r["source"], r["removed"]) for r in cancel] == [("phone", False)], cancel

    w.at(Z("13:12:03")).tick()
    assert len(w.rings) == 1 and w.pushes == [] and w.cards == []
    [s] = w.rows(outcome="snoozed")
    assert s["change"] == "cancelled" and "new_hhmm" not in s, s


def test_a_dashboard_set_during_the_ring_still_escalates(tmp_path, mst):
    """Somebody setting a later time from a screen may be doing it FOR a sleeper.
    The ring is judged exactly as it was before snoozing existed."""
    w = _World(tmp_path)
    _ring(w, "19", "06:10", Z("13:00:45"), Z("13:10:03"))
    w.at(Z("13:10:31"))
    entry = w.store.set_wakeup("19", "06:20")
    w.delivery.record_wakeup_change("19", w.delivery.SOURCE_WEB, entry=entry)
    w.at(Z("13:12:03")).tick()
    w.at(Z("13:14:03")).tick()
    assert [e for _, e in w.rings] == ["19", "19"], "the dashboard set stood down the re-ring"
    assert len(w.pushes) == 1 and w.rows(outcome="snoozed") == []


def test_another_rooms_phone_does_not_snooze_this_one(tmp_path, mst):
    w = _World(tmp_path)
    _ring(w, "19", "06:10", Z("13:00:45"), Z("13:10:03"))
    w.at(Z("13:10:31")).dial_42("14", "06:20")
    w.at(Z("13:12:03")).tick()
    assert [e for _, e in w.rings] == ["19", "19"]
    assert w.rows(outcome="snoozed") == []


def test_a_change_the_same_second_the_ring_started_counts(tmp_path, mst):
    """The ledger stamps whole seconds and the tick does not start on one. A
    snooze inside that second must not read as older than the ring."""
    w = _World(tmp_path)
    _ring(w, "19", "06:10", Z("13:00:45"), Z("13:10:03") + 0.6)
    w.at(Z("13:10:03") + 0.9).dial_42("19", "06:20")
    w.at(Z("13:12:04")).tick()
    assert len(w.rings) == 1 and w.rows(outcome="snoozed")


def test_a_ledger_that_cannot_be_read_fails_toward_the_alarm(tmp_path, mst):
    """★ The asymmetric failure. The milestone join stops tracking when its read
    raises; the snooze read must not, or a broken instrument silences a sleeper."""
    w = _World(tmp_path)
    _ring(w, "19", "06:10", Z("13:00:45"), Z("13:10:03"))
    w.at(Z("13:10:31")).dial_42("19", "06:20")     # a real snooze, unreadable below

    def _broken(ext, since):
        raise OSError("ledger unreadable")
    w.delivery.room_changed_wakeup = _broken
    w.at(Z("13:12:03")).tick()
    assert [e for _, e in w.rings] == ["19", "19"], "an unreadable ledger stood the alarm down"
    assert any("could not read wake-up changes" in m for m in w.logs), w.logs
    assert w.rows(outcome="snoozed") == []


# --------------------------------------------------------------------------- #
# 3. The rows themselves.
# --------------------------------------------------------------------------- #
def test_the_names_are_a_durable_on_disk_format():
    """Renaming one orphans every historical row, and SOURCE_PHONE is read by a
    different program from the one that writes it."""
    d = SourceFileLoader("delivery_names", str(SHARE / "webui" / "delivery.py")).load_module()
    assert (d.WAKEUP_SET, d.WAKEUP_CANCELLED, d.WAKEUP_SNOOZED) == ("set", "cancelled", "snoozed")
    assert (d.SOURCE_PHONE, d.SOURCE_WEB, d.SOURCE_CONSOLE) == ("phone", "web", "console")


def test_a_phone_row_says_who_and_when_and_nothing_that_was_said(tmp_path, mst):
    """These rows land in /share. The recogniser heard a sentence; the row keeps
    only the time it parsed to."""
    w = _World(tmp_path)
    w.at(Z("13:10:31")).dial_42("19", "06:20")
    w.at(Z("13:11:00")).dial_42("19", "CANCEL")
    s, c = w.rows(ext="19")
    assert set(s) == {"ts", "ext", "kind", "outcome", "source", "hhmm", "target_epoch"}, s
    assert (s["outcome"], s["source"], s["hhmm"], s["target_epoch"]) == \
        ("set", "phone", "06:20", int(Z("13:20:00")))
    assert set(c) == {"ts", "ext", "kind", "outcome", "source", "removed"}, c
    assert (c["outcome"], c["source"], c["removed"]) == ("cancelled", "phone", True)


def test_a_failed_ledger_write_never_reaches_the_agi_channel(tmp_path, mst, capsys):
    """★ stdout IS the AGI protocol.

    delivery.record() reports a failed write with print(). In the dial-42 AGI that
    line would reach Asterisk as a command, and the next reply the script read
    would be the answer to it rather than to its own SET VARIABLE. The wake-up
    must still be set, and the failure must be reported on stderr instead.
    """
    w = _World(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    w.delivery.OUTCOME_PATH = str(blocker / "delivery-outcomes.jsonl")
    sent = []
    w.agi.read_env = lambda: {"agi_channel": "PJSIP/19-0000000d"}
    w.agi.agi = lambda cmd: sent.append(cmd) or "200 result=0"
    w.agi.recognize = lambda attempt: "06:20"
    capsys.readouterr()
    w.at(Z("13:10:31")).agi.main()
    out, err = capsys.readouterr()
    assert out == "", f"the AGI channel received {out!r}"
    assert "record FAILED" in err, err
    assert w.store.get("19")["hhmm"] == "06:20"
    assert 'SET VARIABLE WAKEUP_RESULT "set"' in sent, sent
