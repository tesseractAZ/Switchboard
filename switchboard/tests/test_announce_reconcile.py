"""An announcement must end with a verdict, and the verdict must be joined right.

    python3 -m pytest switchboard/tests/test_announce_reconcile.py

★ THE HOLE. `POST /api/announce/<ext>` records `originate-queued` the instant AMI
ACCEPTS the Originate — which is all it can know at that moment — and records six
distinct ways for the Originate to be REFUSED. It has never had a way to record
what happened after acceptance.

That matters because an announcement that rings a handset nobody answers runs no
dialplan at all: `[switchboard-announce-play]` is never entered, so there is no
`h` extension, no rtpqos, no QoS row. Live on 2026-09-01 at 19:05:15 — `Called
19`, `is ringing`, AMI hung it up four seconds later. The announcement appears in
NEITHER ledger, and absence in a delivery ledger reads as "we never tried".

Two things close it: the hangup extension now names the clip it was playing, and
a reconciler files `announce-undelivered` for a queued announcement that never
produced one. The join between them is the whole design, and §2 below is why it
is keyed on the filename rather than on time.
"""
import json
import re
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "webui"
CONFIG = ROOT / "rootfs" / "usr" / "bin" / "switchboard-config"

cq = SourceFileLoader("switchboard_callqos_announce",
                      str(ROOT / "rootfs" / "usr" / "bin" / "switchboard-callqos")).load_module()

NOW = 1789050000.0


def _delivery(tmp_path):
    mod = SourceFileLoader("delivery_announce", str(WEBUI / "delivery.py")).load_module()
    mod.OUTCOME_PATH = str(tmp_path / "delivery-outcomes.jsonl")
    return mod


def _queue(mod, ext, sound, ago):
    """Write an `originate-queued` record `ago` seconds before NOW."""
    import datetime
    ts = datetime.datetime.fromtimestamp(NOW - ago, datetime.timezone.utc)
    rec = {"ts": ts.isoformat(timespec="seconds"), "ext": ext, "kind": "announce",
           "outcome": mod.ANNOUNCE_QUEUED, "sound": sound}
    with open(mod.OUTCOME_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _played(mod, ext, sound, ago):
    import datetime
    ts = datetime.datetime.fromtimestamp(NOW - ago, datetime.timezone.utc)
    rec = {"ts": ts.isoformat(timespec="seconds"), "ext": ext, "kind": "announce",
           "outcome": mod.AUDIO_DELIVERED, "sound": sound, "stage": "complete",
           "txcount": 700}
    with open(mod.OUTCOME_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _sounds(recs):
    return [r["sound"] for r in recs]


# --------------------------------------------------------------------------- #
# 1. The horizon, and why it is derived.
# --------------------------------------------------------------------------- #
def test_the_horizon_outlasts_the_longest_possible_announcement(tmp_path):
    """★ A flat ~120 s was the obvious choice and it is WRONG.

    An announcement may ring for the Originate timeout and then play a clip up to
    the cap. 30 + 90 = 120 exactly, so a 120 s horizon would file "never arrived"
    against announcements that were still playing — and only against the longest
    ones, which are the alerts most worth getting right. Observed originate→record
    lag tracks clip duration closely (23 s at a 22 s clip, 45 s at 44 s), so the
    cap is the term that governs.
    """
    mod = _delivery(tmp_path)
    assert mod.ANNOUNCE_HORIZON > mod.ANNOUNCE_MAX_SECONDS + mod.ANNOUNCE_RING_SECONDS, (
        f"horizon {mod.ANNOUNCE_HORIZON}s does not outlast a full-length "
        f"announcement ({mod.ANNOUNCE_MAX_SECONDS}s clip + "
        f"{mod.ANNOUNCE_RING_SECONDS}s ring)")
    assert mod.ANNOUNCE_HORIZON >= 180


def test_the_cap_has_exactly_one_definition():
    """app.py enforces the clip length; delivery.py derives the horizon from it.
    Two copies would let the cap grow while the horizon stayed put — which is the
    false alarm the test above exists to prevent, reintroduced by drift."""
    src = (WEBUI / "app.py").read_text()
    code = re.sub(r'"""(?:.|\n)*?"""', "",
                  "\n".join(l.split("#", 1)[0] for l in src.split("\n")))
    assert 'os.environ.get("ANNOUNCE_MAX_SECONDS"' not in code, (
        "app.py reads the cap itself again instead of taking delivery's")
    assert "_delivery, \"ANNOUNCE_MAX_SECONDS\"" in code


def test_an_announcement_is_not_judged_while_it_could_still_be_playing(tmp_path):
    mod = _delivery(tmp_path)
    _queue(mod, "19", "ann-19-aaaa", ago=mod.ANNOUNCE_HORIZON - 5)
    assert mod.unresolved_announcements(now=NOW) == []


def test_an_announcement_that_never_arrived_is_reported(tmp_path):
    mod = _delivery(tmp_path)
    _queue(mod, "19", "ann-19-aaaa", ago=mod.ANNOUNCE_HORIZON + 5)
    assert _sounds(mod.unresolved_announcements(now=NOW)) == ["ann-19-aaaa"]


def test_one_that_played_is_not(tmp_path):
    mod = _delivery(tmp_path)
    _queue(mod, "19", "ann-19-aaaa", ago=400)
    _played(mod, "19", "ann-19-aaaa", ago=380)
    assert mod.unresolved_announcements(now=NOW) == []


# --------------------------------------------------------------------------- #
# 2. ★ The join is on the filename. This is the section that matters.
# --------------------------------------------------------------------------- #
def test_three_announcements_in_eight_minutes_are_paired_correctly(tmp_path):
    """★ WHY NOT TIME PROXIMITY.

    Pairing a hangup with "the most recent queued record for this extension"
    matches the live ledger perfectly — 22 of 22 — because the shortest gap
    between two announcements there happens to be about eight minutes. That is a
    property of a quiet week, not of the design. Three alerts inside eight
    minutes is exactly what a real incident looks like, and it is precisely when
    the ledger has to be right.

    Here the MIDDLE one played and the other two did not. A nearest-in-time join
    would credit whichever queue record sat closest to the delivery and report
    the wrong pair of failures.
    """
    mod = _delivery(tmp_path)
    _queue(mod, "19", "ann-19-first",  ago=460)
    _queue(mod, "19", "ann-19-second", ago=300)
    _played(mod, "19", "ann-19-second", ago=280)
    _queue(mod, "19", "ann-19-third",  ago=260)
    got = _sounds(mod.unresolved_announcements(now=NOW))
    assert got == ["ann-19-first", "ann-19-third"], got


def test_a_delivery_does_not_resolve_a_different_clip_on_the_same_phone(tmp_path):
    mod = _delivery(tmp_path)
    _queue(mod, "19", "ann-19-aaaa", ago=400)
    _played(mod, "19", "ann-19-bbbb", ago=390)
    assert _sounds(mod.unresolved_announcements(now=NOW)) == ["ann-19-aaaa"]


def test_a_queued_record_with_no_name_is_left_alone(tmp_path):
    """Records written before v0.98.0 carry no `sound`. They are unjoinable, and
    guessing about them would file failures for announcements that played
    perfectly well weeks ago."""
    mod = _delivery(tmp_path)
    import datetime
    ts = datetime.datetime.fromtimestamp(NOW - 400, datetime.timezone.utc)
    with open(mod.OUTCOME_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts.isoformat(timespec="seconds"), "ext": "19",
                             "kind": "announce", "outcome": mod.ANNOUNCE_QUEUED}) + "\n")
    assert mod.unresolved_announcements(now=NOW) == []


# --------------------------------------------------------------------------- #
# 3. The reconciler runs on a timer, so it must not repeat itself.
# --------------------------------------------------------------------------- #
def test_a_verdict_already_filed_is_not_filed_again(tmp_path):
    """The loop ticks every 20 s. Without this the ledger would fill with one
    identical failure record per tick, forever."""
    mod = _delivery(tmp_path)
    _queue(mod, "19", "ann-19-aaaa", ago=400)
    first = mod.unresolved_announcements(now=NOW)
    assert _sounds(first) == ["ann-19-aaaa"]
    mod.record("19", "announce", mod.ANNOUNCE_UNDELIVERED, sound="ann-19-aaaa")
    assert mod.unresolved_announcements(now=NOW) == []


def test_nothing_older_than_the_lookback_is_judged(tmp_path):
    """A scheduler stopped for a day must not wake up and file a day of
    retroactive failures about a window nobody can act on.

    ★ The ages below are ABSOLUTE, not `ANNOUNCE_LOOKBACK + 60`. Deriving the
    fixture from the constant under test made this pass for any value at all:
    widening the window to three years moved the "old" record with it and the
    assertion still held. A test whose input is decided by its subject checks
    only that the subject is self-consistent.
    """
    mod = _delivery(tmp_path)
    assert mod.ANNOUNCE_HORIZON < mod.ANNOUNCE_LOOKBACK <= 6 * 3600, (
        f"lookback {mod.ANNOUNCE_LOOKBACK}s is outside the range that makes "
        f"sense: it must outlast the horizon, and stay short enough that a "
        f"restarted scheduler cannot file a backlog of retroactive failures")
    _queue(mod, "19", "ann-19-yesterday", ago=25 * 3600)
    _queue(mod, "19", "ann-19-six-hours", ago=6 * 3600 + 60)
    _queue(mod, "19", "ann-19-recent", ago=400)
    assert _sounds(mod.unresolved_announcements(now=NOW)) == ["ann-19-recent"]


def test_a_wakeup_row_is_never_mistaken_for_an_announcement(tmp_path):
    mod = _delivery(tmp_path)
    mod.record("19", "wakeup", "ring-queued")
    _queue(mod, "19", "ann-19-aaaa", ago=400)
    assert _sounds(mod.unresolved_announcements(now=NOW)) == ["ann-19-aaaa"]


# --------------------------------------------------------------------------- #
# 4. The other half: the hangup extension names the clip.
# --------------------------------------------------------------------------- #
def test_the_dialplan_passes_the_clip_name_to_the_sink():
    """★ The join key has to survive four hops: the Originate sets SW_ANN_NAME,
    the channel keeps it into the `h` extension, the dialplan passes it as
    --sound, and callqos puts it in the record. A break anywhere makes every
    announcement look undelivered — and the reconciler would then file a failure
    for every announcement that ever played."""
    # ★ v0.98.2 repointed this to the RENDERED dialplan. It used to match the
    # Python SOURCE literal — and the source literal looked perfectly correct
    # while the bytes Asterisk received silently dropped both hyphens from the
    # clip name. Asserting the source was asserting the wrong artifact, which is
    # the whole lesson of that incident.
    cfg = SourceFileLoader("swcfg_sound", str(ROOT / "rootfs" / "usr" / "bin"
                                              / "switchboard-config")).load_module()
    rendered = "\n".join(cfg.render_rtpqos_context())
    assert rendered.count('--sound "${FILTER(') == 2, (
        "both callqos invocations — the normal one and the no-media one — must "
        "pass the clip name; an unanswered announcement takes the no-media path")
    assert rendered.count("${SW_ANN_NAME}") == 2
    ami = (WEBUI / "ami.py").read_text()
    assert "SW_ANN_NAME={os.path.basename(sound)}" in ami, (
        "the Originate does not put the clip name on the channel")
    assert "--sound" in {a for a in vars(cq._parse_args([]))} or hasattr(
        cq._parse_args([]), "sound"), "callqos does not accept --sound"


def test_the_sink_resolves_the_announcement_it_played(tmp_path, monkeypatch):
    """End to end: an announce leg that played now writes the record that makes
    the reconciler leave it alone."""
    mod = _delivery(tmp_path)
    monkeypatch.setitem(sys.modules, "delivery", mod)
    monkeypatch.setattr(cq, "append_record", lambda rec: None)
    monkeypatch.setattr(cq, "append_outcome", lambda rec: None)
    monkeypatch.setattr(cq, "push_ha", lambda rec: None)
    _queue(mod, "19", "ann-19-aaaa", ago=400)
    assert _sounds(mod.unresolved_announcements(now=NOW)) == ["ann-19-aaaa"]

    cq.main(["--source", "dialplan", "--tag", "announce",
             "--chan", "PJSIP/19-0000002a", "--cid", "19", "--billsec", "12",
             "--hcause", "16", "--stage", "complete", "--sound", "ann-19-aaaa",
             "--rxcount", "600", "--txcount", "620", "--rxmes", "88", "--txmes", "88"])
    assert mod.unresolved_announcements(now=NOW) == [], (
        "the played announcement was not resolved by its own hangup record")


def test_a_truncated_announcement_still_counts_as_arrived(tmp_path, monkeypatch):
    """★ Two different questions, and this is where they part.

    An 8-of-10-seconds clip did not meet its contract — the quality ledger says
    `undelivered` and always has. But it plainly REACHED the handset, and filing
    it beside an announcement that rang out unanswered would flatten the one
    distinction the reconciler exists to draw. The stage and packet count are on
    the record, so the truncation is still visible to a reader.
    """
    mod = _delivery(tmp_path)
    monkeypatch.setitem(sys.modules, "delivery", mod)
    monkeypatch.setattr(cq, "append_record", lambda rec: None)
    monkeypatch.setattr(cq, "append_outcome", lambda rec: None)
    monkeypatch.setattr(cq, "push_ha", lambda rec: None)
    _queue(mod, "19", "ann-19-cut", ago=400)
    cq.main(["--source", "dialplan", "--tag", "announce",
             "--chan", "PJSIP/19-0000002b", "--cid", "19", "--billsec", "8",
             "--hcause", "16", "--stage", "playing", "--sound", "ann-19-cut",
             "--rxcount", "400", "--txcount", "410", "--rxmes", "88", "--txmes", "88"])
    assert mod.unresolved_announcements(now=NOW) == []
    rec = [json.loads(l) for l in Path(mod.OUTCOME_PATH).read_text().splitlines()
           if l.strip()][-1]
    assert rec["outcome"] == mod.AUDIO_DELIVERED
    assert rec["stage"] == "playing" and rec["txcount"] == 410, (
        "the row must still show how far it got")


def test_an_announcement_answered_in_silence_is_not_counted_as_arrived(tmp_path, monkeypatch):
    """The same one-second floor the wake-up uses. A handset that answers and
    emits two packets played nothing to anybody."""
    mod = _delivery(tmp_path)
    monkeypatch.setitem(sys.modules, "delivery", mod)
    monkeypatch.setattr(cq, "append_record", lambda rec: None)
    monkeypatch.setattr(cq, "append_outcome", lambda rec: None)
    monkeypatch.setattr(cq, "push_ha", lambda rec: None)
    _queue(mod, "19", "ann-19-mute", ago=400)
    cq.main(["--source", "dialplan", "--tag", "announce",
             "--chan", "PJSIP/19-0000002c", "--cid", "19", "--billsec", "0",
             "--hcause", "16", "--stage", "playing", "--sound", "ann-19-mute",
             "--rxcount", "30", "--txcount", "2", "--rxmes", "0", "--txmes", "0"])
    assert _sounds(mod.unresolved_announcements(now=NOW)) == ["ann-19-mute"]


def test_the_announce_stage_ladder_matches_the_dialplan():
    """The same tripwire the wake-up ladder has: a stage added in the dialplan
    and not here scores as a failed delivery."""
    cfg = CONFIG.read_text()
    block = cfg[cfg.index('"[switchboard-announce-play]",'):
                cfg.index('rtpqos_h("announce")')]
    assert tuple(re.findall(r"Set\(SW_STAGE=([a-z]+)\)", block)) == cq.ANNOUNCE_STAGES
    assert cq.ANNOUNCE_DELIVERED_FROM in cq.ANNOUNCE_STAGES


# --------------------------------------------------------------------------- #
# 5. The scheduler drives it, safely.
# --------------------------------------------------------------------------- #
def _load_scheduler(delivery_mod):
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
            "sw_scheduler_announce",
            str(ROOT / "rootfs" / "usr" / "share" / "switchboard" / "wakeup"
                / "scheduler.py")).load_module()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_the_scheduler_files_the_verdict(tmp_path):
    mod = _delivery(tmp_path)
    sched = _load_scheduler(mod)
    logged = []
    sched._delivery = mod
    sched.log = lambda m: logged.append(m)
    # The process has been up for an hour as far as this test is concerned; the
    # boundary rule is exercised on its own in §6.
    sched._STARTED = NOW - 3600
    _queue(mod, "19", "ann-19-aaaa", ago=400)
    sched._reconcile_announcements(NOW)
    recs = [json.loads(l) for l in Path(mod.OUTCOME_PATH).read_text().splitlines()
            if l.strip()]
    assert recs[-1]["outcome"] == mod.ANNOUNCE_UNDELIVERED
    assert recs[-1]["sound"] == "ann-19-aaaa" and recs[-1]["ext"] == "19"
    assert any("ann-19-aaaa" in m for m in logged), logged
    # ...and a second tick adds nothing.
    sched._reconcile_announcements(NOW)
    again = [json.loads(l) for l in Path(mod.OUTCOME_PATH).read_text().splitlines()
             if l.strip()]
    assert len(again) == len(recs)


def test_an_unwritable_ledger_files_nothing(tmp_path):
    """★ THE FAIL-SAFE, and it matters more here than on the wake-up side.

    The join asks "was a delivery record written?". If the ledger cannot be
    written at all, the answer is no for EVERY announcement — including every one
    that played perfectly — so an unwritable ledger would turn this reconciler
    into a machine for manufacturing failure records about a healthy system.
    That permission fault was live on this box for two releases.
    """
    mod = _delivery(tmp_path)
    sched = _load_scheduler(mod)
    sched._delivery = mod
    sched.log = lambda m: None
    sched._STARTED = NOW - 3600
    _queue(mod, "19", "ann-19-aaaa", ago=400)
    before = Path(mod.OUTCOME_PATH).read_text()
    mod.is_writable = lambda: False
    sched._reconcile_announcements(NOW)
    assert Path(mod.OUTCOME_PATH).read_text() == before


def test_the_reconciler_never_pushes(tmp_path):
    """An alarm clock has a deadline; an announcement does not. The record is the
    deliverable — a push at whatever hour the announcement was is not."""
    mod = _delivery(tmp_path)
    sched = _load_scheduler(mod)
    pushed = []

    class _HA:
        @staticmethod
        def notify(*a, **k):
            pushed.append((a, k))
            return True

    sched._delivery = mod
    sched.ha_client = _HA
    sched.log = lambda m: None
    sched._STARTED = NOW - 3600
    _queue(mod, "19", "ann-19-aaaa", ago=400)
    sched._reconcile_announcements(NOW)
    assert pushed == [], pushed


def test_a_raising_reconciler_cannot_stop_wake_up_calls():
    """The alarm clock is the load-bearing half of this service. The announce
    reconciler is wrapped in its own try in the main loop so a fault in the
    newer, quieter feature cannot take the older, louder one down with it."""
    src = (ROOT / "rootfs" / "usr" / "share" / "switchboard" / "wakeup"
           / "scheduler.py").read_text()
    loop = src[src.index("    while not _stop:"):]
    assert loop.count("try:") >= 2, (
        "tick() and the announce reconciler share one try — a raise in either "
        "would skip the other")
    assert loop.index("tick()") < loop.index("_reconcile_announcements("), (
        "the wake-up ring must be dispatched before anything else in the loop")


# --------------------------------------------------------------------------- #
# 6. ★ The upgrade boundary. v0.98.0 shipped without this and was wrong in ten
#    minutes, on a live box, about a real announcement.
# --------------------------------------------------------------------------- #
def test_nothing_queued_before_this_process_started_is_judged(tmp_path):
    """★ THE REGRESSION, from the live incident that produced it.

    2026-09-11T01:36:24Z: an announcement was queued to ext 19 under v0.97.0,
    whose hangup extension did not name the clip and whose sink wrote no delivery
    record. It PLAYED — the call-quality ledger scored that leg `excellent`.
    v0.98.0 was deployed at 01:45, its reconciler looked back an hour, found a
    queued record with no resolving half, and filed `announce-undelivered`
    against an announcement that had worked.

    A longer horizon does not fix that; nothing would have appeared however long
    it waited. The rule is that a window this process was not running for is
    UNKNOWABLE, not failed — the resolving record comes from a detached
    switchboard-callqos that an add-on restart kills outright, quite apart from
    an upgrade changing what it writes.
    """
    mod = _delivery(tmp_path)
    started = NOW - 600                       # this process came up 10 min ago
    _queue(mod, "19", "ann-19-before-upgrade", ago=1140)   # 19 min ago: older
    _queue(mod, "19", "ann-19-after-upgrade", ago=400)     # 6.7 min ago: newer
    got = _sounds(mod.unresolved_announcements(now=NOW, not_before=started))
    assert got == ["ann-19-after-upgrade"], got


def test_the_boundary_is_not_merely_the_lookback_in_disguise(tmp_path):
    """Both records below sit comfortably inside the one-hour lookback, so the
    lookback cannot be what separates them. Only the process start can."""
    mod = _delivery(tmp_path)
    assert mod.ANNOUNCE_LOOKBACK >= 3600
    _queue(mod, "19", "ann-19-old", ago=1200)
    _queue(mod, "19", "ann-19-new", ago=400)
    assert len(mod.unresolved_announcements(now=NOW)) == 2, (
        "fixture premise: with no boundary both are judged")
    assert _sounds(mod.unresolved_announcements(
        now=NOW, not_before=NOW - 600)) == ["ann-19-new"]


def test_the_scheduler_passes_its_own_start_time(tmp_path):
    """The parameter is useless if the one caller does not use it — and the one
    caller is the only thing standing between a restart and a burst of false
    failure records."""
    mod = _delivery(tmp_path)
    sched = _load_scheduler(mod)
    sched._delivery = mod
    sched.log = lambda m: None
    sched._STARTED = NOW - 600
    _queue(mod, "19", "ann-19-before", ago=1200)
    sched._reconcile_announcements(NOW)
    recs = [json.loads(l) for l in Path(mod.OUTCOME_PATH).read_text().splitlines()
            if l.strip()]
    assert [r["outcome"] for r in recs] == [mod.ANNOUNCE_QUEUED], (
        f"an announcement from before the process started was judged: {recs}")
    # ...and one from after it still is.
    _queue(mod, "19", "ann-19-after", ago=400)
    sched._reconcile_announcements(NOW)
    recs = [json.loads(l) for l in Path(mod.OUTCOME_PATH).read_text().splitlines()
            if l.strip()]
    assert recs[-1]["outcome"] == mod.ANNOUNCE_UNDELIVERED
    assert recs[-1]["sound"] == "ann-19-after"


def test_the_process_start_is_actually_stamped():
    """★ P4. Every test above sets `_STARTED` by hand to control its fixture, so
    none of them can tell whether the module stamps it at all. Setting it to 0.0
    — or forgetting it — makes the boundary inert in production while the whole
    suite stays green, and the first upgrade files a false failure for every
    announcement in the preceding hour. Which is what happened."""
    import time as _time
    mod = SourceFileLoader("delivery_start", str(WEBUI / "delivery.py")).load_module()
    before = _time.time()
    sched = _load_scheduler(mod)
    after = _time.time()
    assert before - 60 <= sched._STARTED <= after + 1, (
        f"_STARTED is {sched._STARTED}, not this process's start time — the "
        f"restart/upgrade boundary is inert")


# --------------------------------------------------------------------------- #
# 7. ★ The join key crossed the dialplan boundary and came back different.
#    Found by a live test fire, 2026-09-11. Every fixture here is that incident.
# --------------------------------------------------------------------------- #
LIVE_QUEUED = "ann-19-1b411fcd0c42455d9816c29ae1f581e4"     # what app.py wrote
LIVE_ARRIVED = "ann191b411fcd0c42455d9816c29ae1f581e4"      # what the dialplan delivered


def test_the_live_mangled_pair_still_joins(tmp_path):
    """★ THE INCIDENT.

    02:00:27  originate-queued      sound=ann-19-1b411fcd...
    02:00:36  audio-delivered       sound=ann191b411fcd...     stage=complete, 389 packets
    02:03:44  announce-undelivered  sound=ann-19-1b411fcd...

    The announcement played in full and was reported as never delivered 197
    seconds later, because `FILTER(A-Za-z0-9_.-,...)` reads a hyphen as a range
    separator and — after the single character `.` — consumed it instead of
    admitting it. Every announcement would have gone the same way, forever.

    The charset is fixed. This test pins the OTHER half: the join no longer
    depends on that fix being right, because the live ledger shows `a-z-`
    PRESERVING hyphens while `A-Za-z0-9_.-` dropped them — the behaviour turns on
    parse order, and an alarm path should not rest on that.
    """
    mod = _delivery(tmp_path)
    _queue(mod, "19", LIVE_QUEUED, ago=400)
    _played(mod, "19", LIVE_ARRIVED, ago=390)
    assert mod.unresolved_announcements(now=NOW) == [], (
        "the delivered announcement did not resolve its own queued record")


def test_the_canonical_key_is_collision_safe():
    """Canonicalising throws away characters, so it must not throw away
    IDENTITY. app.py mints `ann-<ext>-<uuid4 hex>`, so 128 bits survive."""
    mod = SourceFileLoader("delivery_key", str(WEBUI / "delivery.py")).load_module()
    assert mod.clip_key(LIVE_QUEUED) == mod.clip_key(LIVE_ARRIVED)
    assert mod.clip_key("ann-19-" + "a" * 32) != mod.clip_key("ann-19-" + "b" * 32)
    assert mod.clip_key("ann-19-" + "a" * 32) != mod.clip_key("ann-20-" + "a" * 32)
    assert mod.clip_key("") == "" and mod.clip_key(None) == ""


def test_a_mangled_name_does_not_resolve_a_different_clip(tmp_path):
    """Canonicalising must not turn the join into a wildcard."""
    mod = _delivery(tmp_path)
    _queue(mod, "19", "ann-19-" + "a" * 32, ago=400)
    _played(mod, "19", "ann19" + "b" * 32, ago=390)
    assert _sounds(mod.unresolved_announcements(now=NOW)) == ["ann-19-" + "a" * 32]


def test_the_sink_resolves_a_name_the_dialplan_mangled(tmp_path, monkeypatch):
    """End to end with the bytes the live dialplan actually produced."""
    mod = _delivery(tmp_path)
    monkeypatch.setitem(sys.modules, "delivery", mod)
    monkeypatch.setattr(cq, "append_record", lambda rec: None)
    monkeypatch.setattr(cq, "append_outcome", lambda rec: None)
    monkeypatch.setattr(cq, "push_ha", lambda rec: None)
    _queue(mod, "19", LIVE_QUEUED, ago=400)
    cq.main(["--source", "dialplan", "--tag", "announce",
             "--chan", "PJSIP/19-00000000", "--cid", "19", "--billsec", "7",
             "--hcause", "16", "--stage", "complete", "--sound", LIVE_ARRIVED,
             "--rxcount", "385", "--txcount", "389", "--rxmes", "70", "--txmes", "88"])
    assert mod.unresolved_announcements(now=NOW) == []


# --------------------------------------------------------------------------- #
# 8. ...and the class-level guard, over the RENDERED dialplan.
# --------------------------------------------------------------------------- #
def _filter_charsets():
    """Every FILTER() charset in the dialplan Asterisk actually loads.

    Read from the RENDERED text, not the Python source: the source spells the
    escape through two layers of string literal, and what matters is the byte
    sequence that reaches Asterisk.
    """
    cfg = SourceFileLoader("swcfg_render", str(ROOT / "rootfs" / "usr" / "bin"
                                               / "switchboard-config")).load_module()
    text = "\n".join(cfg.render_rtpqos_context() + cfg.rtpqos_h("rooms")
                     + cfg.render_wakeup_context() + cfg.render_announce_play_context())
    return set(re.findall(r"FILTER\(([^,]*),", text))


def test_no_filter_charset_contains_an_ambiguous_hyphen():
    """★ THE CLASS, not the instance.

    A hyphen in a FILTER charset is a RANGE SEPARATOR. To be admitted as a
    literal it must be written `\\-`; the FILTER documentation says so outright.
    Two charsets in this dialplan wrote it bare, and they did NOT behave the
    same way — `a-z-` preserved the hyphens in `room-to-room` (9 rows in the live
    ledger) while `A-Za-z0-9_.-` silently dropped both hyphens from an
    announcement's clip name and broke the reconciler that joins on it.

    The difference is only where the hyphen sat relative to a completed range.
    Nothing in the charset tells a reader which they are getting, and the failure
    is silent — so a bare hyphen is banned outright rather than reasoned about
    case by case.
    """
    bad = []
    for cs in sorted(_filter_charsets()):
        i, n = 0, len(cs)
        while i < n:
            if cs[i] == "\\":
                i += 2                       # an escape; whatever it is, it is explicit
                continue
            # A hyphen is fine ONLY strictly between two literal characters.
            if cs[i] == "-":
                lo, hi = cs[i - 1] if i else "", cs[i + 1] if i + 1 < n else ""
                if not lo or not hi or hi == "-" or ord(lo) >= ord(hi):
                    bad.append(f"{cs!r}: bare '-' at index {i} is not a valid range")
            i += 1
    assert not bad, (
        "a bare hyphen in a FILTER charset is a range separator, not a literal — "
        "write it as \\\\- :\n  " + "\n  ".join(bad))


def test_the_guard_actually_rejects_the_two_real_defects():
    """A scanner that silently matches nothing agrees with everything. These are
    the exact two charsets this release fixed."""
    def flags(cs):
        i, n, bad = 0, len(cs), []
        while i < n:
            if cs[i] == "\\":
                i += 2
                continue
            if cs[i] == "-":
                lo, hi = cs[i - 1] if i else "", cs[i + 1] if i + 1 < n else ""
                if not lo or not hi or hi == "-" or ord(lo) >= ord(hi):
                    bad.append(i)
            i += 1
        return bad
    assert flags("A-Za-z0-9_.-"), "the charset that broke the announce join is not flagged"
    assert flags("a-z-"), "the SW_TAG charset is not flagged"
    assert not flags("A-Za-z0-9_.\\-"), "the fixed charset is flagged"
    assert not flags("a-z\\-"), "the fixed SW_TAG charset is flagged"
    assert not flags("0-9+*#") and not flags("a-z")


def test_the_announce_charset_reaches_asterisk_escaped():
    """The escape crosses two layers of Python string literal on its way into
    extensions.conf. Assert the RENDERED bytes, not the source."""
    charsets = _filter_charsets()
    assert "A-Za-z0-9_.\\-" in charsets, sorted(charsets)
    assert "a-z\\-" in charsets, sorted(charsets)
    assert "A-Za-z0-9_.-" not in charsets and "a-z-" not in charsets
