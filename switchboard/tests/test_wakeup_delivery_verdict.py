""""Delivered" must mean the same thing in all three places that decide it.

    python3 -m pytest switchboard/tests/test_wakeup_delivery_verdict.py

Three components hold an opinion about whether a wake-up call reached the
sleeper, and until v0.97.0 no test compared them:

  the dialplan          sets SW_STAGE as the script advances
  switchboard-callqos   scores the quality ledger from (stage, txcount)
  wakeup/scheduler.py   decides whether to ring again and push a critical alert

They disagreed, on real calls, in both directions. Every fixture below is a row
copied out of the live ledger on 2026-09-10, because the failure this file exists
to prevent is exactly the kind that looks correct in a hand-written fixture.

  ts          stage      txcount   what actually happened
  1788354922  scene            0   picked up, heard SILENCE, dropped at 1 s
  1788873142  greeting        59   woken by the greeting, hung up at 1.2 s
  1788873274  extras         704   heard 14 s of the script, hung up
  1788960018  greeting       161   woken by the greeting, hung up at 3.2 s
  1788960149  extras         623   heard 12 s of the script, hung up
  1789046443  complete      1036   ran to the end

The first is the worst thing this feature can do and scored `notify: false`. The
middle four all woke somebody and every one of them scored `notify: true` — a
DND-bypassing push at six in the morning, plus a second ring of the phone.
"""
import json
import re
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "webui"

# Spelled out ONCE, here, as a literal. The two programs both read it from
# delivery.py so they cannot drift from each other — but they can still drift
# together, and the ledger on disk cannot: renaming the outcome orphans every
# historical record a reader might join against.
AUDIO_DELIVERED = "audio-delivered"

cq = SourceFileLoader("switchboard_callqos_verdict",
                      str(ROOT / "rootfs" / "usr" / "bin" / "switchboard-callqos")).load_module()


class _Args:
    """Derived from the real parser, like test_callqos's — a hand-copied field
    list silently lags the moment callqos gains an argument."""
    _FIELDS = sorted(vars(cq._parse_args([])))

    def __init__(self, **kw):
        for f in self._FIELDS:
            setattr(self, f, kw.get(f, ""))
        self.source = kw.get("source", "dialplan")


def _leg(stage, txcount, *, tag="wakeup-deliver", hcause="16", rxcount="1200",
         billsec="20", mes="88", chan="PJSIP/19-000000f1"):
    return cq.build_record(_Args(
        source="dialplan", tag=tag, chan=chan, cid="19", billsec=billsec,
        hcause=hcause, stage=stage, rxcount=str(rxcount), txcount=str(txcount),
        rxmes=mes, txmes=mes))


# The live rows, as (stage, txcount, delivered?). Ordered as the calls happened:
# the sequence is the evidence, so it is not sorted.
LIVE_LEGS = [
    ("scene",    0,    False),
    ("greeting", 59,   True),
    ("extras",   704,  True),
    ("greeting", 161,  True),
    ("extras",   623,  True),
    ("complete", 1036, True),
]


# --------------------------------------------------------------------------- #
# 1. The three components share one vocabulary.
# --------------------------------------------------------------------------- #
def test_the_stage_ladder_matches_the_dialplan_that_sets_it():
    """★ The tripwire that did not exist, and its absence is the whole bug.

    `SCRIPTED_TERMINAL` listed {"complete", "delivered", "repeat"}. "delivered"
    is not a stage the dialplan has ever set, and `time` and `extras` — two
    stages it does set, both meaning the script played — were missing. Nothing
    compared the list against the dialplan, so a fictional entry and two absent
    ones both survived review.

    Deriving the terminal set from this tuple fixes the omission; this test fixes
    the tuple, by pinning it to the source of truth.
    """
    cfg = (ROOT / "rootfs" / "usr" / "bin" / "switchboard-config").read_text()
    block = cfg[cfg.index('"[wakeup-deliver]",'):cfg.index('rtpqos_h("wakeup-deliver")')]
    from_dialplan = tuple(re.findall(r"Set\(SW_STAGE=([a-z]+)\)", block))
    assert from_dialplan == cq.WAKEUP_STAGES, (
        f"the dialplan sets {from_dialplan} but callqos scores against "
        f"{cq.WAKEUP_STAGES}. A stage the scorer has never heard of is scored as "
        f"a failed delivery — silently, at six in the morning.")
    assert cq.WAKEUP_DELIVERED_FROM in from_dialplan


def test_the_terminal_set_is_the_tail_of_the_ladder_and_nothing_else():
    """Derived, not enumerated — an enumerated set is what went wrong."""
    terminal = cq.SCRIPTED_TERMINAL["wakeup-deliver"]
    cut = cq.WAKEUP_STAGES.index(cq.WAKEUP_DELIVERED_FROM)
    assert terminal == frozenset(cq.WAKEUP_STAGES[cut:])
    # The stages BEFORE the greeting have played nothing and must never be in it.
    assert not (terminal & {"answering", "scene"}), (
        "a wake-up that stopped before the greeting played no audio at all")


# --------------------------------------------------------------------------- #
# 2. The live legs, scored the way the sleeper experienced them.
# --------------------------------------------------------------------------- #
def test_every_live_leg_is_scored_the_way_the_sleeper_experienced_it():
    wrong = []
    for stage, txc, delivered in LIVE_LEGS:
        rec = _leg(stage, txc)
        got = rec["quality"] != "undelivered"
        if got is not delivered:
            wrong.append(f"stage={stage} txcount={txc}: delivered={got}, "
                         f"expected {delivered} — {rec['reasons']}")
    assert not wrong, "\n  ".join([""] + wrong)


def test_the_four_false_alarms_no_longer_wake_anybody():
    """All four of these rang the phone a second time and then pushed."""
    for stage, txc in (("greeting", 59), ("greeting", 161),
                       ("extras", 704), ("extras", 623)):
        rec = _leg(stage, txc)
        assert rec["notify"] is False, (
            f"stage={stage} txcount={txc} ({txc / 50.0:.1f}s of audio) still "
            f"raises a critical 6am push: {rec['reasons']}")


def test_the_silent_pickup_is_still_the_loudest_thing_in_the_ledger():
    """★ The one failure that matters, and the one this change must not launder.

    2026-09-02 06:15:22: answered, stage `scene`, txcount 0, duration 0. The
    sleeper picked up an alarm clock and heard nothing.
    """
    rec = _leg("scene", 0, rxcount=34, billsec="0", mes="0")
    assert rec["quality"] == "undelivered"
    assert rec["notify"] is True
    assert any("no audio" in r for r in rec["reasons"]), rec["reasons"]


def test_the_verdict_is_not_keyed_on_the_hangup_cause():
    """★ THE TRAP, entered as a case rather than trusted as a note.

    Hangup cause is the obvious discriminator and it is worthless here: every
    wake-up leg in the live ledger carries hcause=16 (normal clearing) — the
    zero-audio pickup, the early hangups, and the completed calls alike. A
    verdict keyed on it would re-file the worst outcome this feature has as a
    success.
    """
    causes = {_leg(stage, txc, hcause="16")["quality"] != "undelivered"
              for stage, txc, _ in LIVE_LEGS}
    assert causes == {True, False}, (
        "with the hangup cause held constant at 16 the verdicts must still "
        "differ — if they do not, something is reading hcause")
    # ...and it is genuinely ignored: change nothing but the cause.
    for hc in ("16", "31", "127", ""):
        assert _leg("greeting", 59, hcause=hc)["quality"] != "undelivered"
        assert _leg("scene", 0, hcause=hc, mes="0")["quality"] == "undelivered"


def test_the_threshold_is_a_duration_not_a_boolean():
    """`txcount > 0` would admit a handset that emitted two packets inside the
    codec's own startup and heard nothing. One second of ulaw is 50 packets."""
    assert cq.DELIVERED_MIN_TXCOUNT == 50
    assert _leg("greeting", 49)["quality"] == "undelivered"
    assert _leg("greeting", 50)["quality"] != "undelivered"
    # ...and the reason says how little, in the unit a person thinks in.
    assert any("0.2s" in r for r in _leg("greeting", 10)["reasons"]), \
        _leg("greeting", 10)["reasons"]


def test_an_ordinary_call_is_never_dragged_into_this_rule():
    """A room-to-room call sets no stage. It is not a scripted delivery."""
    assert cq.delivery_failures("rooms", "", 0) == []
    assert cq.delivery_failures("wakeup-deliver", "", 0) == []
    assert _leg("", 0, tag="rooms", mes="88")["quality"] != "undelivered"


# --------------------------------------------------------------------------- #
# 3. The two ledgers cannot disagree.
# --------------------------------------------------------------------------- #
def _delivery_module(tmp_path):
    mod = SourceFileLoader("delivery_verdict",
                           str(WEBUI / "delivery.py")).load_module()
    mod.OUTCOME_PATH = str(tmp_path / "delivery-outcomes.jsonl")
    return mod


def _written(tmp_path, rec, monkeypatch):
    """Run record_delivery against a temp ledger and return what it wrote."""
    mod = _delivery_module(tmp_path)
    monkeypatch.setitem(sys.modules, "delivery", mod)
    cq.record_delivery(rec)
    p = Path(mod.OUTCOME_PATH)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_the_quality_ledger_and_the_delivery_ledger_agree_on_every_live_leg(
        tmp_path, monkeypatch):
    """★ ONE DECISION, TWO LEDGERS.

    They disagreed on 2026-09-08 06:12:22: the reconciler called the leg
    undelivered and re-rang the phone, while the quality ledger — looking at the
    same two numbers — could see 59 packets of greeting had reached it. Writing
    both from `delivery_failures` is what makes that structurally impossible;
    this asserts it rather than trusting it.
    """
    for stage, txc, delivered in LIVE_LEGS:
        rec = _leg(stage, txc)
        recs = _written(tmp_path / f"{stage}{txc}", rec, monkeypatch)
        wrote = [r for r in recs if r["outcome"] == AUDIO_DELIVERED]
        assert bool(wrote) is delivered, (
            f"stage={stage} txcount={txc}: quality says delivered={delivered} "
            f"but the delivery ledger got {recs}")
        if wrote:
            assert wrote[0]["ext"] == "19" and wrote[0]["kind"] == "wakeup"
            assert wrote[0]["stage"] == stage and wrote[0]["txcount"] == txc


def test_only_the_alarm_clock_reports_its_own_delivery(tmp_path, monkeypatch):
    """A page or an announcement has no reconciler waiting on this ledger, and
    an extra `wakeup` row for a leg that was not a wake-up would be a lie about
    which extension was woken."""
    for tag in ("announce", "page", "rooms"):
        rec = _leg("complete", 900, tag=tag)
        assert _written(tmp_path / tag, rec, monkeypatch) == []


def test_a_redacted_extension_is_never_reported_as_a_wake_up(tmp_path, monkeypatch):
    """`ext` holds a telephone number on trunk legs and callqos masks it. A
    masked value is not an extension and must not be written to a ledger the
    reconciler keys on."""
    rec = _leg("complete", 900, chan="PJSIP/trunk-0000001")
    rec["ext"], rec["ext_redacted"] = "******0147", True
    assert _written(tmp_path, rec, monkeypatch) == []


# --------------------------------------------------------------------------- #
# 4. The reconciler acts on it.
# --------------------------------------------------------------------------- #
def _load_scheduler(delivery_mod):
    """scheduler.py resolves store/ami/ha_client at IMPORT time from absolute
    container paths, so pre-register stand-ins before loading it."""
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
    sys.modules["delivery"] = delivery_mod
    try:
        return SourceFileLoader(
            "sw_scheduler_verdict",
            str(ROOT / "rootfs" / "usr" / "share" / "switchboard" / "wakeup"
                / "scheduler.py")).load_module()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _reconcile_with(tmp_path, outcome):
    """Put one `outcome` record in the ledger for a ring that started 5 minutes
    ago, run the reconciler, and report what it decided."""
    mod = _delivery_module(tmp_path)
    sched = _load_scheduler(mod)
    rang = []
    pushed = []

    class _AMI:
        @staticmethod
        def get_endpoints(): return [{"name": "19", "state": "Not in use"}]
        @staticmethod
        def originate_wakeup(ext, ring):
            rang.append(ext)
            return True

    now = 1789046443.0
    started = now - 300                      # well past RETRY_AFTER
    if outcome:
        mod.record("19", "wakeup", outcome)
    sched._delivery = mod
    sched.ami = _AMI
    sched.ha_client = None
    sched.log = lambda m: pushed.append(m)
    sched._ringing.clear()
    sched._ringing["19"] = {"target_epoch": started, "hhmm": "06:12",
                            "started": started, "retried": False}
    sched._reconcile_rings(now)
    recs = [json.loads(l) for l in Path(mod.OUTCOME_PATH).read_text().splitlines()
            if l.strip()]
    return rang, [r["outcome"] for r in recs], pushed


def test_the_reconciler_accepts_the_measured_delivery(tmp_path):
    """★ The 06:12:22 leg. `spoken` is absent — the AGI that writes it never ran,
    because the sleeper hung up during the greeting that was waking them."""
    rang, outcomes, logged = _reconcile_with(tmp_path, AUDIO_DELIVERED)
    assert rang == [], "the phone was rung a second time for a delivered wake-up"
    assert "no-answer" not in outcomes and "answered-silent" not in outcomes
    assert any("DELIVERED" in m for m in logged), logged


def test_a_spoken_record_still_works_on_its_own(tmp_path):
    """The new join is OR, not replace. `spoken` proves the greeting played to
    the END; the measurement does not, and it is written by a detached process.
    Neither instrument may be disarmed by the other's absence."""
    rang, outcomes, logged = _reconcile_with(tmp_path, "spoken")
    assert rang == []
    assert any("DELIVERED" in m for m in logged), logged


def test_a_silent_pickup_still_rings_again(tmp_path):
    """The 06:15:21 shape: `answered` was written on pickup, and nothing else.
    This is the case the whole reconciler exists for and it must survive."""
    rang, outcomes, _ = _reconcile_with(tmp_path, "answered")
    assert rang == ["19"], "a wake-up that played nothing was not re-rung"
    assert "answered-silent" in outcomes, outcomes


def test_a_ring_that_was_never_answered_still_rings_again(tmp_path):
    rang, outcomes, _ = _reconcile_with(tmp_path, None)
    assert rang == ["19"]
    assert "no-answer" in outcomes, outcomes


def test_main_actually_writes_it(tmp_path, monkeypatch):
    """★ The wiring, driven rather than grepped.

    `record_delivery` can be perfect and unreachable. main() is the only caller,
    and it swallows every exception by design (a hangup handler must never fail
    loudly) — so a missing call, a typo'd name and a raised import all look
    identical from outside: a silent no-op that re-rings the phone at 6am.
    """
    mod = _delivery_module(tmp_path)
    monkeypatch.setitem(sys.modules, "delivery", mod)
    monkeypatch.setattr(cq, "append_record", lambda rec: None)
    monkeypatch.setattr(cq, "append_outcome", lambda rec: None)
    monkeypatch.setattr(cq, "push_ha", lambda rec: None)

    assert cq.main(["--source", "dialplan", "--tag", "wakeup-deliver",
                    "--chan", "PJSIP/19-00000007", "--cid", "19",
                    "--billsec", "2", "--hcause", "16", "--stage", "greeting",
                    "--rxcount", "132", "--txcount", "59",
                    "--rxmes", "0", "--txmes", "0"]) == 0
    recs = [json.loads(l) for l in Path(mod.OUTCOME_PATH).read_text().splitlines()
            if l.strip()]
    assert [r["outcome"] for r in recs] == [AUDIO_DELIVERED], recs
    assert recs[0]["ext"] == "19" and recs[0]["txcount"] == 59


def test_both_programs_read_the_outcome_name_from_the_ledger_module():
    """★ The mutant that survived the first battery.

    `DELIVERED_OUTCOME` in switchboard-callqos and a `"audio-delivered"` literal
    in scheduler.py were two independent strings that had to match forever. A
    one-character drift in either would not fail a single test — the writer would
    keep writing, the reader would keep reading nothing, and the symptom would be
    a critical 6am push to someone already awake.
    """
    mod = SourceFileLoader("delivery_name", str(WEBUI / "delivery.py")).load_module()
    assert mod.AUDIO_DELIVERED == AUDIO_DELIVERED, (
        "the on-disk outcome name changed; historical records are now orphaned")

    for path in (ROOT / "rootfs" / "usr" / "bin" / "switchboard-callqos",
                 ROOT / "rootfs" / "usr" / "share" / "switchboard" / "wakeup"
                 / "scheduler.py"):
        src = path.read_text()
        # Strip comments and docstrings: both files EXPLAIN this rule in prose
        # that names the string, and a scanner that matches its own explanation
        # is a self-inflicted false positive.
        code = "\n".join(l.split("#", 1)[0] for l in src.split("\n"))
        code = re.sub(r'"""(?:.|\n)*?"""', "", code)
        assert f'"{AUDIO_DELIVERED}"' not in code, (
            f"{path.name} spells the outcome name itself instead of reading "
            f"delivery.AUDIO_DELIVERED — the two can now drift apart silently")
        assert "AUDIO_DELIVERED" in code, f"{path.name} does not use the shared name"
