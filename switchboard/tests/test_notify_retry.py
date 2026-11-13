"""An alert Home Assistant refused is posted once it answers, and a recovery
never lets a stale alert land after it.

    python3 -m pytest switchboard/tests/test_notify_retry.py

★ THE INCIDENT. 2026-11-07 19:00 MST the host rebooted and Home Assistant Core
did not answer until 19:12. The add-on was up from 19:01. devhealth raised
'cordless CRITICAL' at 19:05:51 and 'cordless recovered' at 19:09:55; rtpmon
raised 'all phones dropped briefly' at 19:06:52. All three got a 502 from
`persistent_notification/create` and were gone: each poller raises an alert once,
on a transition, and ignored notify()'s False. The phones were back inside the
outage, so nothing was missed that time; a phone that STAYED down would never
have alerted at all.

The module tests pin webui/notify_retry.py's rules. The caller tests drive the
real `_notify` / `_notify_outage` / `_notify_mass_drop` / `_escalate` and the real
`run()` loops over TWO cycles, because a helper with its own test and no caller
has been the shape of this repo's escapes, and a flush that only ran once (before
the loop) would pass a one-cycle test.

Here notify_retry is registered in sys.modules only inside the tests that need
the pollers to find it, and restored after. (Other test files can still reach
the real module in a full-suite run, through a webui/ path another file adds;
their fakes accept every post, so nothing is kept either way.)
"""
import ast
import contextlib
import os
import sys
import tempfile
import time
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARE = ROOT / "rootfs" / "usr" / "share" / "switchboard"
nr = SourceFileLoader("notify_retry_under_test", str(SHARE / "webui" / "notify_retry.py")).load_module()
dh = SourceFileLoader("devhealth_poller_retry", str(SHARE / "devhealth" / "poller.py")).load_module()
pm = SourceFileLoader("rtpmon_poller_retry", str(SHARE / "rtpmon" / "poller.py")).load_module()

_failures = 0


def check(name, cond):
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    assert cond, name


class FakeHA:
    """A Home Assistant that refuses or accepts on demand and records each post."""

    def __init__(self, accept=True):
        self.accept = accept
        self.calls = []

    def notify(self, message, title="", notification_id=""):
        self.calls.append({"message": message, "title": title, "id": notification_id})
        return self.accept

    def push(self, *a, **k):  # the scheduler's audible path; never accepted here
        return False

    def set_state(self, *a, **k):  # sensors the pollers publish each cycle
        return True

    def get_state(self, *a, **k):
        return None


@contextlib.contextmanager
def installed(ha, retry=True):
    """ha as `ha_client`, and notify_retry importable by name (retry=True) or
    unimportable (retry=False: sys.modules[name] = None makes import raise)."""
    saved = {k: sys.modules.get(k, "absent") for k in ("ha_client", "notify_retry")}
    sys.modules["ha_client"] = ha
    sys.modules["notify_retry"] = nr if retry else None
    nr.clear()
    try:
        yield
    finally:
        nr.clear()
        for k, v in saved.items():
            if v == "absent":
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


T0 = 1794103551.0  # 2026-11-07 19:05:51 MST, the lost cordless CRITICAL
HHMM = lambda ts: time.strftime("%H:%M", time.localtime(ts))  # noqa: E731


# --- the module's rules ------------------------------------------------------

def test_accepted_at_once_keeps_nothing():
    nr.clear()
    ha = FakeHA(accept=True)
    ok = nr.send(ha, "Cordless critical: unreachable", "Switchboard: cordless CRITICAL",
                 "switchboard_cordless_health", now=T0)
    check("accepted: send returns True", ok is True)
    check("accepted: nothing kept", nr.pending() == [])
    check("accepted: the text is posted unchanged",
          ha.calls[0]["message"] == "Cordless critical: unreachable")
    check("accepted: the id is passed through", ha.calls[0]["id"] == "switchboard_cordless_health")


def test_refused_alert_is_kept_and_posted_late():
    nr.clear()
    ha = FakeHA(accept=False)
    ok = nr.send(ha, "Cordless critical: unreachable", "Switchboard: cordless CRITICAL",
                 "switchboard_cordless_health", now=T0)
    check("refused: send returns False", ok is False)
    check("refused: kept", len(nr.pending()) == 1)
    check("refused: a flush while HA still refuses posts nothing",
          nr.flush(ha, now=T0 + 120) == 0 and len(nr.pending()) == 1)
    ha.accept = True
    n = nr.flush(ha, now=T0 + 420)
    last = ha.calls[-1]
    check("refused: posted once HA accepts", n == 1 and nr.pending() == [])
    check("refused: the late post keeps its id and title",
          last["id"] == "switchboard_cordless_health" and last["title"] == "Switchboard: cordless CRITICAL")
    check("refused: the late post says it is late and when it was written",
          last["message"].startswith("Cordless critical: unreachable")
          and "Sent late: written at " + HHMM(T0) in last["message"])
    calls = len(ha.calls)
    nr.flush(ha, now=T0 + 540)
    check("refused: posted exactly once", len(ha.calls) == calls)


def test_recovery_replaces_an_undelivered_alert_and_names_it():
    """The 11-07 sequence: CRITICAL refused, recovered refused, HA back."""
    nr.clear()
    ha = FakeHA(accept=False)
    nid = "switchboard_cordless_health"
    nr.send(ha, "Cordless critical: unreachable", "Switchboard: cordless CRITICAL", nid, now=T0)
    nr.send(ha, "Cordless recovered — back to normal.", "Switchboard: cordless OK", nid, now=T0 + 244)
    check("supersede: one kept per id", len(nr.pending()) == 1)
    ha.accept = True
    nr.flush(ha, now=T0 + 480)
    last = ha.calls[-1]
    check("supersede: exactly one post after HA returns (two refusals before it)", len(ha.calls) == 3)
    check("supersede: the RECOVERY is what lands, never the stale CRITICAL",
          last["title"] == "Switchboard: cordless OK"
          and last["message"].startswith("Cordless recovered"))
    check("supersede: the late time is the recovery's own, not the CRITICAL's",
          "written at " + HHMM(T0 + 244) in last["message"]
          and HHMM(T0 + 244) != HHMM(T0))
    check("supersede: the replaced alert is named with its time",
          "Not delivered at the time: cordless CRITICAL at " + HHMM(T0) in last["message"])


def test_recovery_accepted_at_once_still_names_the_lost_alert():
    nr.clear()
    ha = FakeHA(accept=False)
    nid = "switchboard_link_outage"
    nr.send(ha, "9 of 10 phones are unreachable", "Switchboard: phones unreachable", nid, now=T0)
    ha.accept = True
    ok = nr.send(ha, "Phones recovered — 9 of 10 reachable.", "Switchboard: phones recovered",
                 nid, now=T0 + 300)
    last = ha.calls[-1]
    check("direct recovery: accepted and nothing kept", ok and nr.pending() == [])
    check("direct recovery: not marked late (it was sent on time)", "Sent late" not in last["message"])
    check("direct recovery: names the outage alert nobody saw",
          "Not delivered at the time: phones unreachable at " + HHMM(T0) in last["message"])


def test_a_long_flap_names_where_it_began_and_how_many_more():
    nr.clear()
    ha = FakeHA(accept=False)
    nid = "switchboard_link_outage"
    titles = ["Switchboard: phones unreachable", "Switchboard: phones recovered"]
    for i in range(7):  # down, up, down, up, down, up, down: 6 replaced
        nr.send(ha, f"state {i}", titles[i % 2], nid, now=T0 + 600 * i)
    ha.accept = True
    nr.flush(ha, now=T0 + 600 * 7)
    msg = ha.calls[-1]["message"]
    named = msg.split("Not delivered at the time: ", 1)[1]
    check("flap: the episode's first alert is named", named.startswith("phones unreachable at " + HHMM(T0)))
    # 6 replaced, MISSED_KEEP=4: the first, "2 more", then the newest three
    check("flap: the ones between are counted, not silently cut", ", 2 more, " in named)
    check("flap: the newest replaced alerts are named",
          all(("at " + HHMM(T0 + 600 * i)) in named for i in (3, 4, 5))
          and ("at " + HHMM(T0 + 600 * 1)) not in named)
    check("flap: the posted text is the latest state", msg.startswith("state 6"))


def test_dropped_only_after_a_refused_retry_past_its_window():
    nr.clear()
    ha = FakeHA(accept=False)
    nr.send(ha, "Outside line down", "Switchboard: outside line down",
            "switchboard_trunk_registration", now=T0)
    nr.flush(ha, now=T0 + nr.RETRY_FOR - 1)
    check("window: still kept inside the window", len(nr.pending()) == 1)
    calls = len(ha.calls)
    nr.flush(ha, now=T0 + nr.RETRY_FOR + 1)
    check("window: past it, a refused retry drops it", nr.pending() == [] and len(ha.calls) == calls + 1)


def test_past_its_window_but_ha_answering_it_is_posted():
    """A poller cycling once an hour (the options allow it) must still get one
    retry; and nothing is dropped in a cycle where Home Assistant answers."""
    nr.clear()
    ha = FakeHA(accept=False)
    nr.send(ha, "Outside line down", "Switchboard: outside line down",
            "switchboard_trunk_registration", now=T0)
    ha.accept = True
    n = nr.flush(ha, now=T0 + 2 * nr.RETRY_FOR)
    check("hourly poller: posted at its first retry", n == 1 and nr.pending() == [])


def test_the_window_runs_from_the_alerts_own_raise():
    """The current state must get its full window even if its id was refused an
    hour earlier (cordless degraded at 19:05, critical at 20:00, HA back 20:06)."""
    nr.clear()
    ha = FakeHA(accept=False)
    nid = "switchboard_cordless_health"
    nr.send(ha, "Cordless degraded: weak Wi-Fi", "Switchboard: cordless degraded", nid, now=T0)
    nr.send(ha, "Cordless critical: unreachable", "Switchboard: cordless CRITICAL", nid,
            now=T0 + nr.RETRY_FOR - 300)
    nr.flush(ha, now=T0 + nr.RETRY_FOR + 60)  # still refused: must NOT drop the CRITICAL
    check("own window: the newest alert survives a refusal past the first one's hour",
          len(nr.pending()) == 1)
    ha.accept = True
    nr.flush(ha, now=T0 + nr.RETRY_FOR + 180)
    last = ha.calls[-1]
    check("own window: the CRITICAL lands, naming the degraded it replaced",
          last["title"] == "Switchboard: cordless CRITICAL" and "cordless degraded at" in last["message"])


def test_an_alert_behind_a_refused_one_gets_its_own_retry():
    """Two alerts refused in one cycle; at the next (hourly) cycle the first is
    refused again and dropped, and the second must still get its own attempt."""
    nr.clear()
    ha = FakeHA(accept=False)
    nr.send(ha, "Cordless critical", "Switchboard: cordless CRITICAL", "switchboard_cordless_health", now=T0)
    nr.send(ha, "Gateway critical", "Switchboard: gxw gateway CRITICAL", "switchboard_gateway_health", now=T0 + 1)
    nr.flush(ha, now=T0 + nr.RETRY_FOR + 5)
    kept = [p["nid"] for p in nr.pending()]
    check("behind: the refused head is dropped, the one behind it is kept",
          kept == ["switchboard_gateway_health"])
    ha.accept = True
    nr.flush(ha, now=T0 + 2 * nr.RETRY_FOR + 5)
    check("behind: it lands at its own retry", ha.calls[-1]["id"] == "switchboard_gateway_health"
          and nr.pending() == [])


def test_a_dropped_alert_is_named_by_the_next_one_for_its_id():
    nr.clear()
    ha = FakeHA(accept=False)
    nid = "switchboard_cordless_health"
    nr.send(ha, "Cordless critical", "Switchboard: cordless CRITICAL", nid, now=T0)
    nr.flush(ha, now=T0 + nr.RETRY_FOR + 120)  # refused past its hour: dropped
    check("lost: dropped from the queue", nr.pending() == [])
    ha.accept = True
    nr.send(ha, "Cordless recovered — back to normal.", "Switchboard: cordless OK", nid,
            now=T0 + nr.RETRY_FOR + 1500)
    check("lost: the recovery names the CRITICAL nobody saw",
          "Not delivered at the time: cordless CRITICAL at " + HHMM(T0) in ha.calls[-1]["message"])
    nr.send(ha, "Cordless recovered — back to normal.", "Switchboard: cordless OK", nid,
            now=T0 + nr.RETRY_FOR + 1600)
    check("lost: named once, not on every later alert", "Not delivered" not in ha.calls[-1]["message"])
    nr.clear()
    nr.send(FakeHA(accept=False), "Cordless critical", "Switchboard: cordless CRITICAL", nid, now=T0)
    nr.flush(FakeHA(accept=False), now=T0 + nr.RETRY_FOR + 120)
    nr.send(ha, "Cordless recovered", "Switchboard: cordless OK", nid, now=T0 + nr.LOST_KEEP + nr.RETRY_FOR + 600)
    check("lost: the note expires, so an undated time cannot mislead a day later",
          "Not delivered" not in ha.calls[-1]["message"])
    nr.clear()


def test_a_full_queue_evicts_one_off_cards_before_the_current_state():
    nr.clear()
    ha = FakeHA(accept=False)
    nr.send(ha, "Outside line down", "Switchboard: outside line down", "switchboard_trunk_registration", now=T0)
    for i in range(nr.MAX_PENDING + 3):
        nr.send(ha, f"drop {i}", "Switchboard: all phones dropped briefly",
                f"switchboard_mass_drop_{i}", now=T0 + 60 * (i + 1), episodic=True)
    kept = [p["nid"] for p in nr.pending()]
    check("evict: the outside-line alert survives a run of one-off cards",
          "switchboard_trunk_registration" in kept and len(kept) == nr.MAX_PENDING)
    check("evict: the oldest one-off cards are the ones dropped", "switchboard_mass_drop_0" not in kept)
    nr.clear()


def test_flush_stops_at_the_first_refusal():
    nr.clear()
    ha = FakeHA(accept=False)
    for i in range(3):
        nr.send(ha, f"drop {i}", "Switchboard: all phones dropped briefly",
                f"switchboard_mass_drop_{i}", now=T0 + i)
    calls = len(ha.calls)
    nr.flush(ha, now=T0 + 120)
    check("still down: one attempt per flush, not one per kept alert", len(ha.calls) == calls + 1)
    ha.accept = True
    check("back up: all three posted", nr.flush(ha, now=T0 + 240) == 3 and nr.pending() == [])
    check("back up: oldest first",
          [c["id"] for c in ha.calls[-3:]] == [f"switchboard_mass_drop_{i}" for i in range(3)])


def test_ages_use_the_monotonic_clock():
    """A wall-clock step after boot (NTP) must not drop or age an alert."""
    nr.clear()
    ha = FakeHA(accept=False)
    real = nr.time
    clock = {"wall": T0, "mono": 1000.0}
    nr.time = types.SimpleNamespace(time=lambda: clock["wall"], monotonic=lambda: clock["mono"],
                                    strftime=real.strftime, localtime=real.localtime)
    try:
        nr.send(ha, "Outside line down", "Switchboard: outside line down",
                "switchboard_trunk_registration")
        clock["wall"] += 2 * nr.RETRY_FOR  # NTP steps the clock two hours forward
        clock["mono"] += 120               # ...two minutes have really passed
        nr.flush(ha)
        check("clock step: still kept after a refused retry", len(nr.pending()) == 1)
        clock["mono"] += nr.RETRY_FOR      # an hour really passes
        nr.flush(ha)
        check("clock step: dropped once an hour has really passed", nr.pending() == [])
    finally:
        nr.time = real
        nr.clear()


def test_client_exception_is_a_refusal():
    nr.clear()

    class Boom:
        def notify(self, *a, **k):
            raise OSError("supervisor unreachable")

    ok = nr.send(Boom(), "x", "Switchboard: cordless CRITICAL", "switchboard_cordless_health", now=T0)
    check("exception: send returns False and does not raise", ok is False)
    check("exception: kept for retry", len(nr.pending()) == 1)
    nr.clear()


def test_distinct_ids_are_kept_apart():
    nr.clear()
    ha = FakeHA(accept=False)
    nr.send(ha, "cordless", "Switchboard: cordless CRITICAL", "switchboard_cordless_health", now=T0)
    nr.send(ha, "drop", "Switchboard: all phones dropped briefly", "switchboard_mass_drop_1", now=T0 + 61)
    check("ids: both kept", len(nr.pending()) == 2)
    ha.accept = True
    nr.flush(ha, now=T0 + 300)
    check("ids: both posted, neither names the other",
          all("Not delivered" not in c["message"] for c in ha.calls[-2:]))


def test_note_survives_the_500_char_limit():
    """ha_client.notify() cuts at 500; the note must not be what is cut."""
    nr.clear()
    ha = FakeHA(accept=False)
    nr.send(ha, "A" * 480, "Switchboard: phones unreachable", "switchboard_link_outage", now=T0)
    ha.accept = True
    nr.flush(ha, now=T0 + 300)
    msg = ha.calls[-1]["message"]
    check("limit: within 500 characters", len(msg) <= 500)
    check("limit: the late note is intact", msg.endswith("not accepting notifications."))
    check("limit: the main text was shortened instead", msg.startswith("AAAA") and "…" in msg)


def test_the_real_mass_drop_text_fits_late_without_a_cut():
    """The longest alert the pollers send, posted late, must arrive whole."""
    sent = []
    ha = FakeHA(accept=False)
    with installed(ha):
        pm._notify_mass_drop({"kind": "healed", "nid": "switchboard_mass_drop_1",
                              "exts": [str(e) for e in range(11, 20)], "down": [],
                              "at": T0, "still": [], "all": True}, {"reachable": 9, "total": 10})
        ha.accept = True
        pm._flush_notifications()
        sent = ha.calls[-1]["message"]
    check("mass-drop late: under the limit", len(sent) <= 500)
    check("mass-drop late: the body is not shortened", "…" not in sent and "not a current outage." in sent)


def test_queue_is_bounded():
    nr.clear()
    ha = FakeHA(accept=False)
    for i in range(nr.MAX_PENDING + 5):
        nr.send(ha, f"drop {i}", "Switchboard: all phones dropped briefly",
                f"switchboard_mass_drop_{i}", now=T0 + i)
    kept = sorted(p["nid"] for p in nr.pending())
    check("bound: never more than MAX_PENDING kept", len(kept) == nr.MAX_PENDING)
    check("bound: the oldest are the ones dropped", "switchboard_mass_drop_0" not in kept
          and f"switchboard_mass_drop_{nr.MAX_PENDING + 4}" in kept)
    nr.clear()


# --- the real callers --------------------------------------------------------

def test_devhealth_alert_refused_then_posted_by_its_flush():
    ha = FakeHA(accept=False)
    with installed(ha):
        dh._notify("cordless", "critical", ["cordless unreachable"])
        dh._notify("cordless", "recovered", [])
        check("devhealth: both refused, one kept", len(nr.pending()) == 1)
        ha.accept = True
        dh._flush_notifications()
        left = nr.pending()
    last = ha.calls[-1]
    check("devhealth: the flush posts the recovery under the device's id",
          last["id"] == "switchboard_cordless_health" and last["title"] == "Switchboard: cordless OK")
    check("devhealth: the lost CRITICAL is named", "cordless CRITICAL" in last["message"])
    check("devhealth: nothing left", left == [])


def test_devhealth_alerts_off_queues_nothing():
    ha = FakeHA(accept=False)
    old = os.environ.get("DEVICE_HEALTH_ALERTS")
    os.environ["DEVICE_HEALTH_ALERTS"] = "false"
    try:
        with installed(ha):
            dh._notify("gateway", "critical", ["all ports down"])
            left = nr.pending()
    finally:
        if old is None:
            os.environ.pop("DEVICE_HEALTH_ALERTS", None)
        else:
            os.environ["DEVICE_HEALTH_ALERTS"] = old
    check("muted: nothing posted and nothing kept", ha.calls == [] and left == [])


def test_rtpmon_alerts_refused_then_posted_by_its_flush():
    ha = FakeHA(accept=False)
    summ = {"reachable": 0, "total": 10, "unreachable": 9,
            "unreachable_exts": [str(e) for e in range(11, 20)]}
    with installed(ha):
        pm._notify_mass_drop({"kind": "healed", "nid": "switchboard_mass_drop_2",
                              "exts": [str(e) for e in range(11, 20)], "down": [],
                              "at": T0, "still": [], "all": True}, summ)
        pm._notify_outage("down", summ)
        check("rtpmon: both refused and kept", len(nr.pending()) == 2)
        kinds = {p["title"]: p["episodic"] for p in nr.pending()}
        check("rtpmon: only the one-off mass-drop card is episodic",
              kinds == {"Switchboard: all phones dropped briefly": True,
                        "Switchboard: phones unreachable": False})
        ha.accept = True
        pm._flush_notifications()
        left = nr.pending()
    titles = [c["title"] for c in ha.calls][-2:]
    check("rtpmon: both posted after HA returns",
          titles == ["Switchboard: all phones dropped briefly", "Switchboard: phones unreachable"])
    check("rtpmon: nothing left", left == [])


def test_without_the_module_each_caller_still_sends_once():
    """The ImportError fallback: no retry, but the alert is not swallowed."""
    for name, fire in (("devhealth", lambda: dh._notify("cordless", "critical", ["x"])),
                       ("rtpmon", lambda: pm._notify_outage("down", {"reachable": 0, "total": 10,
                                                                     "unreachable": 9,
                                                                     "unreachable_exts": ["11"]}))):
        ha = FakeHA(accept=True)
        with installed(ha, retry=False):
            fire()
        check(f"{name} fallback: posted exactly once", len(ha.calls) == 1)


def _notify_calls_outside_send(path, allowed=("_send", "_card", "_flush_missed_notifications")):
    tree = ast.parse(path.read_text())
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "notify" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "ha_client" and fn.name not in allowed):
                bad.append(f"{fn.name}:{node.lineno}")
    return bad


def test_every_long_running_alert_goes_through_the_retry():
    """The trunk alert has no behavioural test of its own here; this keeps any
    alert, present or future, from calling ha_client.notify() directly. The
    scheduler's missed-wake-up flush keeps its own retry and is allowed."""
    for rel in ("devhealth/poller.py", "rtpmon/poller.py", "wakeup/scheduler.py"):
        bad = _notify_calls_outside_send(SHARE / rel)
        check(f"{rel}: no ha_client.notify() outside the retry paths ({bad})", bad == [])


class _Stop(Exception):
    pass


def _two_cycles(mod, ha, stubs):
    """Run the real run() loop for two cycles. HA refuses during the first and
    accepts from the first sleep on, so a kept alert can ONLY be posted by the
    second cycle's flush: a flush hoisted above the loop fails this."""
    sleeps = []

    def _sleep(_s):
        sleeps.append(_s)
        ha.accept = True
        if len(sleeps) >= 2:
            raise _Stop()

    real = {k: getattr(mod, k) for k in stubs}
    real_sleep = mod.time.sleep
    for k, v in stubs.items():
        setattr(mod, k, v)
    mod.time.sleep = _sleep
    try:
        mod.run()
    except _Stop:
        pass
    finally:
        mod.time.sleep = real_sleep
        for k, v in real.items():
            setattr(mod, k, v)
    return sleeps


def test_devhealth_run_retries_on_a_later_cycle():
    ha = FakeHA(accept=False)
    with installed(ha):
        nr.send(ha, "Cordless critical: unreachable", "Switchboard: cordless CRITICAL",
                "switchboard_cordless_health")
        sleeps = _two_cycles(dh, ha, {"resolve_cordless_ip": lambda *a, **k: None,
                                      "read_rollup": lambda **k: None,
                                      "_publish_gateway_unknown": lambda *a, **k: None})
        left = nr.pending()
    posted = [c for c in ha.calls if c["id"] == "switchboard_cordless_health"]
    check("devhealth run(): two cycles ran", len(sleeps) == 2)
    check("devhealth run(): refused in cycle 1, posted in cycle 2",
          len(posted) == 3 and left == [] and "Sent late" in posted[-1]["message"])


def test_rtpmon_run_retries_on_a_later_cycle():
    ha = FakeHA(accept=False)
    ha.set_state = lambda *a, **k: True
    tmp = tempfile.mkdtemp(prefix="sb-retry-")
    with installed(ha):
        nr.send(ha, "Phones unreachable", "Switchboard: phones unreachable", "switchboard_link_outage")
        sleeps = _two_cycles(pm, ha, {
            "_load_options": lambda: {"link_health_alerts": False},
            "poll_once": lambda *a, **k: (None, None),
            "endpoint_transitions": lambda: [],
            "_trunk_check": lambda *a, **k: None,
            "_heartbeat": lambda *a, **k: None,
            "_scrub_share_log": lambda st: None,
            "EVER_REGISTERED_PATH": os.path.join(tmp, "ever.json"),
        })
        left = nr.pending()
    posted = [c for c in ha.calls if c["id"] == "switchboard_link_outage"]
    check("rtpmon run(): two cycles ran", len(sleeps) == 2)
    check("rtpmon run(): refused in cycle 1, posted in cycle 2",
          len(posted) == 3 and left == [] and "Sent late" in posted[-1]["message"])


def _scheduler():
    class _Pre:
        @staticmethod
        def due(now): return ([], [])
        @staticmethod
        def get_endpoints(): return []
        @staticmethod
        def notify(*a, **k): return True

    saved = {k: sys.modules.get(k, "absent") for k in ("store", "ami", "ha_client", "notify_retry")}
    for k in ("store", "ami", "ha_client"):
        sys.modules[k] = _Pre
    sys.modules["notify_retry"] = nr
    try:
        return SourceFileLoader("sw_scheduler_retry", str(SHARE / "wakeup" / "scheduler.py")).load_module()
    finally:
        for k, v in saved.items():
            if v == "absent":
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_undelivered_wake_up_card_is_kept_and_posted_by_tick():
    sched = _scheduler()
    ha = FakeHA(accept=False)
    saved = sched.ha_client, sched.notify_retry, sched.PUSH_TARGET
    sched.ha_client, sched.notify_retry, sched.PUSH_TARGET = ha, nr, "mobile_app_test"
    nr.clear()
    try:
        sched._escalate("19", "06:18", "The phone rang out twice.", "no-answer", 2)
        check("wake-up: the card was refused and kept", len(nr.pending()) == 1)
        ha.accept = True
        sched._ringing.clear()
        sched.tick()
        left = nr.pending()
    finally:
        sched.ha_client, sched.notify_retry, sched.PUSH_TARGET = saved
        nr.clear()
    last = ha.calls[-1]
    check("wake-up: tick() posted the kept card under its id",
          last["id"] == "switchboard_undelivered_wakeup_19" and left == [])
    check("wake-up: the card says it is late", "Sent late" in last["message"])


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
