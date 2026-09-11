"""Every way a wake-up can fail must reach a person, not just a file.

    python3 -m pytest switchboard/tests/test_wakeup_escalation_paths.py

★ THE INVARIANT: if the alarm did not go off, somebody is told.

Four distinct paths end a wake-up in failure, and until v0.100.0 exactly ONE of
them escalated. The other three wrote a row to the delivery ledger and stopped —
a file nobody reads at six in the morning, which is the hour that matters:

    originate-refused   the PBX refused the call; the phone never rang
    re-ring-skipped     the handset was not available for the second attempt
    re-ring-failed      the handset WAS available and the second ring still
                        did not go out
    no-answer /         rang twice, or was picked up in silence
    answered-silent     — the one path that did escalate

The give-away was in the source. The message arm for "the second attempt was not
made" was UNREACHABLE: `retried` is set True only in the branch that also sets
`rang_again` True, so the ternary choosing between them could never take that
arm. Wording had been written, reviewed, and fixed once (v0.84.0) for a case that
never got as far as being said out loud — and the test covering it reached that
arm by writing the impossible pair into the fixture by hand.
"""
import json
import re
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHED_SRC = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "wakeup" / "scheduler.py"

T0 = 1_000_000.0


def _load(tmp_path):
    """Load the scheduler with a temp ledger and recording stand-ins."""
    delivery = SourceFileLoader("delivery_esc", str(
        ROOT / "rootfs" / "usr" / "share" / "switchboard" / "webui"
        / "delivery.py")).load_module()
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

    saved = {k: sys.modules.get(k) for k in ("store", "ami", "ha_client", "delivery")}
    for k in ("store", "ami", "ha_client"):
        sys.modules[k] = _Pre
    sys.modules["delivery"] = delivery
    try:
        sched = SourceFileLoader("sched_esc", str(SCHED_SRC)).load_module()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    pushes, cards = [], []

    class _HA:
        @staticmethod
        def push(msg, **k):
            pushes.append((msg, k))
            return True

        @staticmethod
        def notify(msg, **k):
            cards.append((msg, k))
            return True

    sched._delivery = delivery
    sched.ha_client = _HA
    sched.log = lambda m: None
    sched._ringing.clear()
    return sched, delivery, pushes, cards


def _rows(delivery):
    p = Path(delivery.OUTCOME_PATH)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _ring(sched, retried=False):
    sched._ringing["19"] = {"target_epoch": T0, "hhmm": "06:15",
                            "started": T0, "retried": retried}


# --------------------------------------------------------------------------- #
# 1. The three paths that were silent.
# --------------------------------------------------------------------------- #
def test_a_skipped_re_ring_escalates(tmp_path):
    """The handset was not available for the second attempt. The alarm did not
    go off. Recording that and saying nothing is the defect."""
    sched, delivery, pushes, _ = _load(tmp_path)

    class _AMI:
        @staticmethod
        def get_endpoints(): return [{"name": "19", "state": "Unavailable"}]
        @staticmethod
        def originate_wakeup(ext, ring):
            raise AssertionError("must not re-ring an unavailable handset")

    sched.ami = _AMI
    _ring(sched)
    sched._reconcile_rings(T0 + sched.RETRY_AFTER + 1)

    assert len(pushes) == 1, f"a skipped re-ring raised {len(pushes)} pushes"
    msg = pushes[0][0]
    assert "second attempt was not made" in msg
    assert "handset was not available" in msg
    assert "rung twice" not in msg, "it must not claim a ring that never happened"
    outcomes = [(r["outcome"], r.get("reason")) for r in _rows(delivery)]
    assert ("undelivered", "re-ring-skipped") in outcomes, outcomes


def test_a_re_ring_the_pbx_refuses_escalates(tmp_path):
    """★ The handset WAS available and the second ring still did not go out.
    This path recorded nothing at all — not even a row — and returned."""
    sched, delivery, pushes, _ = _load(tmp_path)

    class _AMI:
        @staticmethod
        def get_endpoints(): return [{"name": "19", "state": "Not in use"}]
        @staticmethod
        def originate_wakeup(ext, ring): return False

    sched.ami = _AMI
    _ring(sched)
    sched._reconcile_rings(T0 + sched.RETRY_AFTER + 1)

    assert len(pushes) == 1, f"a refused re-ring raised {len(pushes)} pushes"
    assert "refused" in pushes[0][0], pushes[0][0]
    outcomes = [(r["outcome"], r.get("reason")) for r in _rows(delivery)]
    assert ("undelivered", "re-ring-failed") in outcomes, outcomes
    assert any(r["outcome"] == "re-ring-failed" for r in _rows(delivery)), (
        "the attempt itself must leave a row, not only the verdict")


def test_a_re_ring_that_raises_escalates_and_says_which(tmp_path):
    """AMI unreachable on the second attempt is a different cause from AMI
    refusing it, and the person woken at 6am deserves the right one."""
    sched, delivery, pushes, _ = _load(tmp_path)

    class _AMI:
        @staticmethod
        def get_endpoints(): return [{"name": "19", "state": "Not in use"}]
        @staticmethod
        def originate_wakeup(ext, ring):
            raise OSError("AMI unreachable")

    sched.ami = _AMI
    _ring(sched)
    sched._reconcile_rings(T0 + sched.RETRY_AFTER + 1)
    assert len(pushes) == 1
    assert "could not be reached" in pushes[0][0], pushes[0][0]
    assert "refused it" not in pushes[0][0], "the two causes must not be conflated"


def test_an_originate_the_pbx_refuses_escalates(tmp_path):
    """★ The quietest of the four: the phone never rang at all.

    Safe to push here ONLY because v0.99.0 made the store entry be consumed on
    refusal. Before that this path re-fired every twenty seconds, and a push on
    each would have been a notification storm rather than an alarm — so this
    test also pins the consumption it depends on.
    """
    sched, delivery, pushes, _ = _load(tmp_path)
    cancelled = []

    class _Store:
        @staticmethod
        def due(now):
            return ([("19", {"hhmm": "06:15", "target_epoch": T0})], [])

        @staticmethod
        def cancel_if(ext, epoch):
            cancelled.append(ext)
            return True

    class _AMI:
        @staticmethod
        def get_endpoints(): return [{"name": "19", "state": "Not in use"}]
        @staticmethod
        def originate_wakeup(ext, ring): return False

    sched.store, sched.ami = _Store, _AMI
    sched.tick()

    assert len(pushes) == 1, f"a refused originate raised {len(pushes)} pushes"
    assert "never rang" in pushes[0][0], pushes[0][0]
    assert cancelled == ["19"], "without the consumption this would push every tick"
    outcomes = [(r["outcome"], r.get("reason")) for r in _rows(delivery)]
    assert ("undelivered", "originate-refused") in outcomes, outcomes


# --------------------------------------------------------------------------- #
# 2. ...without escalating things that are NOT failures.
# --------------------------------------------------------------------------- #
def test_a_delivered_wakeup_escalates_nothing(tmp_path):
    """The obvious control. A guard that fires on everything is not a guard."""
    sched, delivery, pushes, cards = _load(tmp_path)
    delivery.record("19", "wakeup", "spoken")

    class _AMI:
        @staticmethod
        def get_endpoints(): return [{"name": "19", "state": "Not in use"}]
        @staticmethod
        def originate_wakeup(ext, ring):
            raise AssertionError("a delivered wake-up must not be rung again")

    sched.ami = _AMI
    _ring(sched)
    sched._reconcile_rings(T0 + sched.RETRY_AFTER + 1)
    assert pushes == [] and cards == []
    assert not any(r["outcome"] == "undelivered" for r in _rows(delivery))


def test_an_ami_outage_at_dispatch_does_not_escalate(tmp_path):
    """AMI being unreachable when the wake-up is due does not mean it failed —
    the entry stays due and the next tick tries again. Escalating here would
    cry wolf during a blip and then ring the phone anyway."""
    sched, delivery, pushes, _ = _load(tmp_path)
    cancelled = []

    class _Store:
        @staticmethod
        def due(now):
            return ([("19", {"hhmm": "06:15", "target_epoch": T0})], [])
        @staticmethod
        def cancel_if(ext, epoch):
            cancelled.append(ext)
            return True

    class _AMI:
        @staticmethod
        def get_endpoints(): return [{"name": "19", "state": "Not in use"}]
        @staticmethod
        def originate_wakeup(ext, ring):
            raise OSError("AMI unreachable")

    sched.store, sched.ami = _Store, _AMI
    sched.tick()
    assert pushes == [], "an AMI blip woke somebody up"
    assert cancelled == [], "and it dropped the wake-up instead of retrying"


def test_a_ring_still_in_flight_is_left_alone(tmp_path):
    """Before RETRY_AFTER there is nothing to judge."""
    sched, delivery, pushes, _ = _load(tmp_path)
    sched.ami = type("A", (), {
        "get_endpoints": staticmethod(lambda: []),
        "originate_wakeup": staticmethod(lambda e, r: True)})
    _ring(sched)
    sched._reconcile_rings(T0 + 1)
    assert pushes == [] and _rows(delivery) == []


# --------------------------------------------------------------------------- #
# 3. The structural guard: no path may record `undelivered` and stay quiet.
# --------------------------------------------------------------------------- #
def test_every_undelivered_record_goes_through_the_escalation():
    """★ THE CLASS, not the four instances.

    The defect was a caller writing an `undelivered` row and returning. There is
    now exactly one place that writes that outcome, and it notifies before it
    returns — so a future path cannot record a missed alarm silently without
    deleting this test first.
    """
    src = SCHED_SRC.read_text()
    code = re.sub(r'"""(?:.|\n)*?"""', "",
                  "\n".join(l.split("#", 1)[0] for l in src.split("\n")))
    writes = re.findall(r'_record\(\s*ext\s*,\s*"undelivered"', code)
    assert len(writes) == 1, (
        f"{len(writes)} places write an `undelivered` record; it must be written "
        f"only by _escalate(), which notifies before it returns")
    body = code[code.index("def _escalate"):code.index("def _reconcile_rings")]
    assert '_record(ext, "undelivered"' in body
    assert "ha_client.push(" in body and "ha_client.notify(" in body


def test_the_escalation_falls_back_to_a_card_when_the_push_fails(tmp_path):
    """A failed push must not lose the signal — that was the v0.70.0 design and
    it has to survive the refactor that gave three more callers access to it."""
    sched, delivery, pushes, cards = _load(tmp_path)

    class _HA:
        @staticmethod
        def push(msg, **k):
            raise OSError("no route to HA")
        @staticmethod
        def notify(msg, **k):
            cards.append((msg, k))
            return True

    sched.ha_client = _HA
    sched.ami = type("A", (), {
        "get_endpoints": staticmethod(lambda: [{"name": "19", "state": "Unavailable"}]),
        "originate_wakeup": staticmethod(lambda e, r: True)})
    _ring(sched)
    sched._reconcile_rings(T0 + sched.RETRY_AFTER + 1)
    assert pushes == [] and len(cards) == 1, (cards, pushes)
    assert cards[0][1].get("notification_id", "").endswith("_19")


def test_the_push_is_critical_so_it_beats_do_not_disturb(tmp_path):
    """Every caller, not just the original one. An alarm clock that failed is
    exactly the case Do Not Disturb should not swallow."""
    sched, delivery, pushes, _ = _load(tmp_path)
    sched.ami = type("A", (), {
        "get_endpoints": staticmethod(lambda: [{"name": "19", "state": "Unavailable"}]),
        "originate_wakeup": staticmethod(lambda e, r: True)})
    _ring(sched)
    sched._reconcile_rings(T0 + sched.RETRY_AFTER + 1)
    assert pushes[0][1].get("critical") is True, pushes[0][1]
