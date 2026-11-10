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
real `_notify` / `_notify_outage` / `_notify_mass_drop` and the real `run()`
loops, because a helper with its own test and no caller has been the shape of
this repo's escapes.
"""
import ast
import os
import sys
import tempfile
import time
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARE = ROOT / "rootfs" / "usr" / "share" / "switchboard"
nr = SourceFileLoader("notify_retry", str(SHARE / "webui" / "notify_retry.py")).load_module()
# The pollers `import notify_retry`; the real module, by name, without putting an
# add-on directory on sys.path (conftest's tripwire forbids that).
sys.modules["notify_retry"] = nr
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


def _fresh():
    nr.clear()
    sys.modules["notify_retry"] = nr


T0 = 1794103551.0  # 2026-11-07 19:05:51 MST, the lost cordless CRITICAL
HHMM = lambda ts: time.strftime("%H:%M", time.localtime(ts))  # noqa: E731


# --- the module's rules ------------------------------------------------------

def test_accepted_at_once_keeps_nothing():
    _fresh()
    ha = FakeHA(accept=True)
    ok = nr.send(ha, "Cordless critical: unreachable", "Switchboard: cordless CRITICAL",
                 "switchboard_cordless_health", now=T0)
    check("accepted: send returns True", ok is True)
    check("accepted: nothing kept", nr.pending() == [])
    check("accepted: the text is posted unchanged",
          ha.calls[0]["message"] == "Cordless critical: unreachable")
    check("accepted: the id is passed through", ha.calls[0]["id"] == "switchboard_cordless_health")


def test_refused_alert_is_kept_and_posted_late():
    _fresh()
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
    check("refused: the late post says it is late and when it was raised",
          last["message"].startswith("Cordless critical: unreachable")
          and "Sent late" in last["message"] and HHMM(T0) in last["message"])
    calls = len(ha.calls)
    nr.flush(ha, now=T0 + 540)
    check("refused: posted exactly once", len(ha.calls) == calls)


def test_recovery_replaces_an_undelivered_alert_and_names_it():
    """The 11-07 sequence: CRITICAL refused, recovered refused, HA back."""
    _fresh()
    ha = FakeHA(accept=False)
    nid = "switchboard_cordless_health"
    nr.send(ha, "Cordless critical: unreachable", "Switchboard: cordless CRITICAL", nid, now=T0)
    nr.send(ha, "Cordless recovered — back to normal.", "Switchboard: cordless OK", nid, now=T0 + 244)
    check("supersede: one kept per id", len(nr.pending()) == 1)
    ha.accept = True
    nr.flush(ha, now=T0 + 480)
    last = ha.calls[-1]
    # two refused attempts, then one accepted post
    check("supersede: exactly one post after HA returns", len(ha.calls) == 3)
    check("supersede: the RECOVERY is what lands, never the stale CRITICAL",
          last["title"] == "Switchboard: cordless OK"
          and last["message"].startswith("Cordless recovered"))
    check("supersede: the replaced alert is named with its time",
          "Not delivered at the time: cordless CRITICAL at " + HHMM(T0) in last["message"])


def test_recovery_accepted_at_once_still_names_the_lost_alert():
    _fresh()
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


def test_gives_up_after_the_retry_window():
    _fresh()
    ha = FakeHA(accept=False)
    nr.send(ha, "Outside line down", "Switchboard: outside line down",
            "switchboard_trunk_registration", now=T0)
    nr.flush(ha, now=T0 + nr.RETRY_FOR - 1)
    check("give-up: still kept inside the window", len(nr.pending()) == 1)
    ha.accept = True
    calls = len(ha.calls)
    nr.flush(ha, now=T0 + nr.RETRY_FOR + 1)
    check("give-up: dropped after the window, not posted", nr.pending() == [] and len(ha.calls) == calls)


def test_give_up_clock_runs_from_the_first_refusal():
    """An alert that flaps while HA is down must still end."""
    _fresh()
    ha = FakeHA(accept=False)
    nid = "switchboard_link_outage"
    nr.send(ha, "down", "Switchboard: phones unreachable", nid, now=T0)
    nr.send(ha, "up", "Switchboard: phones recovered", nid, now=T0 + nr.RETRY_FOR - 10)
    ha.accept = True
    calls = len(ha.calls)
    nr.flush(ha, now=T0 + nr.RETRY_FOR + 1)
    check("flap: the window is not reset by a replacing alert",
          nr.pending() == [] and len(ha.calls) == calls)


def test_flush_stops_at_the_first_refusal():
    _fresh()
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


def test_client_exception_is_a_refusal():
    _fresh()

    class Boom:
        def notify(self, *a, **k):
            raise OSError("supervisor unreachable")

    ok = nr.send(Boom(), "x", "Switchboard: cordless CRITICAL", "switchboard_cordless_health", now=T0)
    check("exception: send returns False and does not raise", ok is False)
    check("exception: kept for retry", len(nr.pending()) == 1)


def test_distinct_ids_are_kept_apart():
    _fresh()
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
    _fresh()
    ha = FakeHA(accept=False)
    long = "A" * 480
    nr.send(ha, long, "Switchboard: phones unreachable", "switchboard_link_outage", now=T0)
    ha.accept = True
    nr.flush(ha, now=T0 + 300)
    msg = ha.calls[-1]["message"]
    check("limit: within 500 characters", len(msg) <= 500)
    check("limit: the late note is intact", msg.endswith("not accepting notifications."))
    check("limit: the main text was shortened instead", msg.startswith("AAAA") and "…" in msg)


def test_queue_is_bounded():
    _fresh()
    ha = FakeHA(accept=False)
    for i in range(nr.MAX_PENDING + 5):
        nr.send(ha, f"drop {i}", "Switchboard: all phones dropped briefly",
                f"switchboard_mass_drop_{i}", now=T0 + i)
    kept = sorted(p["nid"] for p in nr.pending())
    check("bound: never more than MAX_PENDING kept", len(kept) == nr.MAX_PENDING)
    check("bound: the oldest are the ones dropped", "switchboard_mass_drop_0" not in kept
          and f"switchboard_mass_drop_{nr.MAX_PENDING + 4}" in kept)


# --- the real callers --------------------------------------------------------

def _with_ha(ha):
    saved = sys.modules.get("ha_client")
    sys.modules["ha_client"] = ha
    return saved


def _restore_ha(saved):
    if saved is None:
        sys.modules.pop("ha_client", None)
    else:
        sys.modules["ha_client"] = saved


def test_devhealth_alert_refused_then_posted_by_its_flush():
    _fresh()
    ha = FakeHA(accept=False)
    saved = _with_ha(ha)
    try:
        dh._notify("cordless", "critical", ["cordless unreachable"])
        dh._notify("cordless", "recovered", [])
        check("devhealth: both refused, one kept", len(nr.pending()) == 1)
        ha.accept = True
        dh._flush_notifications()
    finally:
        _restore_ha(saved)
    last = ha.calls[-1]
    check("devhealth: the flush posts the recovery under the device's id",
          last["id"] == "switchboard_cordless_health" and last["title"] == "Switchboard: cordless OK")
    check("devhealth: the lost CRITICAL is named", "cordless CRITICAL" in last["message"])
    check("devhealth: nothing left", nr.pending() == [])


def test_devhealth_alerts_off_queues_nothing():
    _fresh()
    ha = FakeHA(accept=False)
    saved = _with_ha(ha)
    old = os.environ.get("DEVICE_HEALTH_ALERTS")
    os.environ["DEVICE_HEALTH_ALERTS"] = "false"
    try:
        dh._notify("gateway", "critical", ["all ports down"])
    finally:
        _restore_ha(saved)
        if old is None:
            os.environ.pop("DEVICE_HEALTH_ALERTS", None)
        else:
            os.environ["DEVICE_HEALTH_ALERTS"] = old
    check("muted: nothing posted and nothing kept", ha.calls == [] and nr.pending() == [])


def test_rtpmon_alerts_refused_then_posted_by_its_flush():
    _fresh()
    ha = FakeHA(accept=False)
    saved = _with_ha(ha)
    summ = {"reachable": 0, "total": 10, "unreachable": 9,
            "unreachable_exts": [str(e) for e in range(11, 20)]}
    try:
        pm._notify_mass_drop([str(e) for e in range(11, 20)], summ)
        pm._notify_outage("down", summ)
        check("rtpmon: both refused and kept", len(nr.pending()) == 2)
        ha.accept = True
        pm._flush_notifications()
    finally:
        _restore_ha(saved)
    titles = [c["title"] for c in ha.calls if c is not None][-2:]
    check("rtpmon: both posted after HA returns",
          titles == ["Switchboard: all phones dropped briefly", "Switchboard: phones unreachable"])
    check("rtpmon: nothing left", nr.pending() == [])


def _notify_calls_outside_send(path):
    tree = ast.parse(path.read_text())
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "notify" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "ha_client" and fn.name != "_send"):
                bad.append(f"{fn.name}:{node.lineno}")
    return bad


def test_every_poller_alert_goes_through_the_retry():
    """The trunk alert has no behavioural test of its own here; this keeps any
    alert, present or future, from calling ha_client.notify() directly."""
    for name in ("devhealth", "rtpmon"):
        bad = _notify_calls_outside_send(SHARE / name / "poller.py")
        check(f"{name}: no ha_client.notify() outside _send ({bad})", bad == [])


class _Stop(Exception):
    pass


def test_devhealth_run_flushes_at_the_top_of_each_cycle():
    _fresh()
    ha = FakeHA(accept=False)
    nr.send(ha, "Cordless critical: unreachable", "Switchboard: cordless CRITICAL",
            "switchboard_cordless_health", now=time.time())
    ha.accept = True
    saved, real = _with_ha(ha), dh.resolve_cordless_ip

    def _stop(*a, **k):
        raise _Stop()

    dh.resolve_cordless_ip = _stop  # the first thing after the flush
    try:
        dh.run()
    except _Stop:
        pass
    finally:
        dh.resolve_cordless_ip = real
        _restore_ha(saved)
    check("devhealth run(): the kept alert was posted by the loop",
          nr.pending() == [] and ha.calls[-1]["id"] == "switchboard_cordless_health")


def test_rtpmon_run_flushes_at_the_top_of_each_cycle():
    _fresh()
    ha = FakeHA(accept=False)
    nr.send(ha, "Phones unreachable", "Switchboard: phones unreachable",
            "switchboard_link_outage", now=time.time())
    ha.accept = True
    saved = _with_ha(ha)
    real_load, real_path = pm._load_options, pm.EVER_REGISTERED_PATH
    tmp = tempfile.mkdtemp(prefix="sb-retry-")

    def _stop():
        raise _Stop()

    pm._load_options = _stop  # the first thing after the flush
    pm.EVER_REGISTERED_PATH = os.path.join(tmp, "ever.json")
    try:
        pm.run()
    except _Stop:
        pass
    finally:
        pm._load_options, pm.EVER_REGISTERED_PATH = real_load, real_path
        _restore_ha(saved)
    check("rtpmon run(): the kept alert was posted by the loop",
          nr.pending() == [] and ha.calls[-1]["id"] == "switchboard_link_outage")
    _fresh()


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
