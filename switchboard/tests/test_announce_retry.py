"""An announcement whose audio never played is SENT AGAIN — and one that played never is.

    python3 -m pytest switchboard/tests/test_announce_retry.py

★ THE INCIDENT, for the third time in a fortnight. 2026-09-15T01:42:12Z, 8.4 s
after the add-on restarted for a deploy, an announcement was queued to ext 19
before that handset had re-registered. Asterisk logged
`ast_sip_create_dialog_uac: Endpoint 19: Could not create dialog to invalid URI
19` and gave up; no channel, so no dialplan, so no `h` extension, so no audio and
no delivery record. ext 19's contact came back 37.9 s later, at 01:42:50.875Z.
The ledger held `originate-queued` at 01:42:12Z and `announce-unsettled
(pbx-restarting)` at 01:45:25Z, and nobody was told anything.

v0.104.0 shipped the RECORD half of that (`announce-guard-unjudged`). The owner's
decision after it: an announcement whose audio never played must be retried
automatically. This file is the guard rail around that decision, and the rail that
matters most is the one pointing the other way — a duplicate announcement in a
quiet house at 03:00 is worse than the original miss, so §1 is about never
replaying audio that played.

Every test here drives the REAL caller: `scheduler._retry_announcements()`, the
function main()'s third try block calls, with real `delivery`, real
`announce_clip` and the real `ami` classification. A helper with its own test and
no caller has been the shape of this repo's last several escapes.
"""
import datetime
import json
import re
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "webui"
WAKEUP = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "wakeup"
for _p in (str(WEBUI), str(WAKEUP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The REAL ami module: the retry's green light must be ami.device_idle and not a
# fourth private copy of the device-state classification, so the classification
# under test here is the shipped one. Only get_endpoints/announce_to_ext — the two
# functions that talk to a socket — are replaced per test.
AMI = SourceFileLoader("sw_ami_retry", str(WEBUI / "ami.py")).load_module()

NOW = 1789050000.0
POLL = 20.0                      # WAKEUP_POLL_SECONDS; asserted against the real one
HEX = "0123456789abcdef" * 2     # a 32-char lowercase hex tail
SOUND = "ann-19-" + HEX
# The live pair from 2026-09-11: what app.py wrote, and what came back through the
# dialplan's FILTER() with both hyphens eaten.
LIVE_QUEUED = "ann-19-1b411fcd0c42455d9816c29ae1f581e4"
LIVE_ARRIVED = "ann191b411fcd0c42455d9816c29ae1f581e4"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
# ★ THE LEDGER MUST BE ON THE FIXTURE'S CLOCK, not the wall clock. record()
# stamps datetime.now(), so a row written by the code under test would land
# wherever "today" happens to be — and every age these tests are about is
# measured from a row's own timestamp. Left alone, the second retry attempt was
# unreachable in this suite for a reason that exists nowhere in the product: the
# attempt row was stamped a day into the fixture's future, so the minimum-age
# test could never pass again. Not a fixture convenience; the alternative is a
# suite whose result depends on the date it runs.
_CLOCK = [NOW]


class _FakeDT(datetime.datetime):
    """datetime.datetime with now() pinned to _CLOCK. Subclassed rather than
    stubbed so fromisoformat() — which _read_records uses on every row — is the
    real one."""
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime.fromtimestamp(_CLOCK[0], tz)


_REAL_TZ = datetime.timezone


class _FakeTimeModule:
    """What `delivery` sees when it says `import datetime`."""
    datetime = _FakeDT
    timezone = _REAL_TZ


def _delivery(tmp_path):
    mod = SourceFileLoader("delivery_retry", str(WEBUI / "delivery.py")).load_module()
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    mod.OUTCOME_PATH = str(Path(tmp_path) / "delivery-outcomes.jsonl")
    return mod


def _clip_mod(tmp_path):
    mod = SourceFileLoader("announce_clip_retry",
                           str(WEBUI / "announce_clip.py")).load_module()
    mod.ANNOUNCE_DIR = str(Path(tmp_path) / "announce")
    Path(mod.ANNOUNCE_DIR).mkdir(parents=True, exist_ok=True)
    return mod


def _write_clip(clip_mod, name, seconds=5.0):
    p = Path(clip_mod.ANNOUNCE_DIR) / (name + ".wav")
    p.write_bytes(b"\0" * 44 + b"\1" * int(16000 * seconds))
    return p


class _Store:
    PATH = "/dev/null"
    @staticmethod
    def due(now): return ([], [])
    @staticmethod
    def cancel_if(ext, epoch): return True


class _HA:
    @staticmethod
    def notify(*a, **k): return True


def _load_scheduler(delivery_mod, clip_mod):
    saved = {k: sys.modules.get(k)
             for k in ("store", "ami", "ha_client", "delivery", "announce_clip")}
    sys.modules["store"] = _Store
    sys.modules["ha_client"] = _HA
    sys.modules["ami"] = AMI
    sys.modules["delivery"] = delivery_mod
    sys.modules["announce_clip"] = clip_mod
    try:
        sched = SourceFileLoader("sw_scheduler_retry",
                                 str(WAKEUP / "scheduler.py")).load_module()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    sched._delivery = delivery_mod
    sched.announce_clip = clip_mod
    sched.ami = AMI
    sched._retry_seen.clear()
    # What the MODULE stamped, before the fixture overrides it: the one thing no
    # test that sets _STARTED by hand can check (see the stamp test below).
    sched._STAMPED_AT = sched._STARTED
    sched._STARTED = NOW - 600
    sched.LOGGED = []
    sched.log = lambda m: sched.LOGGED.append(m)
    return sched


class _Bench:
    """One scheduler wired to one ledger and one clip directory, plus the two AMI
    calls the retry makes. `state` is what every endpoint reports."""

    def __init__(self, tmp_path, state="Not in use", channels="", exts=("19",)):
        self.delivery = _delivery(tmp_path)
        self.delivery.datetime = _FakeTimeModule      # rows land on the fixture clock
        _CLOCK[0] = NOW
        self.clips = _clip_mod(tmp_path)
        self.sched = _load_scheduler(self.delivery, self.clips)
        self.state, self.channels, self.exts = state, channels, list(exts)
        self.originated = []
        self.endpoint_reads = 0
        self.on_read = None
        self.originate = lambda ext, sound: True
        AMI.get_endpoints = self._endpoints
        AMI.announce_to_ext = self._announce

    # -- the two AMI calls -------------------------------------------------
    def _endpoints(self):
        self.endpoint_reads += 1
        if self.on_read is not None:
            self.on_read()
        return [{"name": e, "state": self.state, "channels": self.channels}
                for e in self.exts]

    def _announce(self, ext, sound):
        self.originated.append((ext, sound))
        return self.originate(ext, sound)

    # -- the ledger --------------------------------------------------------
    def row(self, outcome, sound=SOUND, ext="19", ago=0.0, kind="announce", **extra):
        ts = datetime.datetime.fromtimestamp(NOW - ago, datetime.timezone.utc)
        rec = {"ts": ts.isoformat(timespec="seconds"), "ext": ext, "kind": kind,
               "outcome": outcome, "sound": sound}
        rec.update({k: v for k, v in extra.items() if v is not None})
        with open(self.delivery.OUTCOME_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def queue(self, sound=SOUND, ext="19", ago=60.0, clip=True):
        self.row(self.delivery.ANNOUNCE_QUEUED, sound=sound, ext=ext, ago=ago)
        if clip:
            _write_clip(self.clips, sound)

    def delivered(self, sound=SOUND, ext="19", ago=50.0):
        self.row(self.delivery.AUDIO_DELIVERED, sound=sound, ext=ext, ago=ago,
                 stage="complete", txcount=700)

    def attempted(self, sound=SOUND, ext="19", ago=40.0, attempt=1):
        self.row(self.delivery.ANNOUNCE_RETRY_ATTEMPTED, sound=sound, ext=ext,
                 ago=ago, attempt=attempt)

    def iso(self, ago=0.0):
        return datetime.datetime.fromtimestamp(
            NOW - ago, datetime.timezone.utc).isoformat(timespec="seconds")

    def rows(self):
        try:
            return [json.loads(l) for l in
                    Path(self.delivery.OUTCOME_PATH).read_text().splitlines() if l.strip()]
        except OSError:
            return []

    def outcomes(self):
        return [r["outcome"] for r in self.rows()]

    # -- driving -----------------------------------------------------------
    def tick(self, at=NOW, shared=False):
        _CLOCK[0] = at
        recs = (self.delivery.announce_records(at, not_before=self.sched._STARTED)
                if shared else None)
        self.sched._retry_announcements(at, recs)

    def ticks(self, n, first=NOW, step=POLL):
        for i in range(n):
            self.tick(first + i * step)


# --------------------------------------------------------------------------- #
# 1. ★ NEVER REPLAY AUDIO THAT PLAYED. The hard constraint, from every angle.
# --------------------------------------------------------------------------- #
def test_a_clip_that_played_is_never_replayed_at_any_age(tmp_path):
    """The join that already exists and is already live-proven: switchboard-callqos
    stamps the SAME --sound onto the row it writes from the hangup extension, so
    an `audio-delivered` row for a clip excludes it permanently."""
    b = _Bench(tmp_path)
    b.queue(ago=60)
    b.delivered(ago=50)
    for age in (60, 100, 149, 151, 400):
        b.tick(NOW + (age - 60))
    assert b.originated == [], b.originated
    assert b.outcomes() == ["originate-queued", "audio-delivered"], b.outcomes()


def test_a_delivered_row_that_lands_after_an_attempt_still_stops_the_next_one(tmp_path):
    """Order must not matter. The first retry rang the handset, it answered, the
    clip played — so the delivered row is NEWER than the attempt row. A budget
    with one attempt left must not spend it."""
    b = _Bench(tmp_path)
    b.queue(ago=90)
    b.attempted(ago=60, attempt=1)
    b.delivered(ago=40)
    b.ticks(4)
    assert b.originated == []
    assert "announce-retry-attempted" not in b.outcomes()[2:]


def test_the_filter_mangled_spelling_still_stops_a_replay(tmp_path):
    """★ THE 2026-09-11 INCIDENT, reused as the strongest form of this test.

    The clip name crosses the dialplan through a FILTER() charset that once ate
    both its hyphens, so the delivered row can legitimately name the same clip
    differently. clip_key() canonicalises both sides. If the retry compared raw
    strings it would see an unresolved announcement that had played in full, and
    replay it."""
    b = _Bench(tmp_path)
    b.queue(sound=LIVE_QUEUED, ago=60)
    b.delivered(sound=LIVE_ARRIVED, ago=50)
    b.ticks(3)
    assert b.originated == []


def test_a_delivered_row_that_lands_while_the_retry_is_deciding_stops_it(tmp_path):
    """★ THE ORDERING RULE. The endpoint state is read over a socket with a 2.5 s
    budget; a hangup can land inside that window. The veto read happens AFTER the
    state read, so a delivered row arriving mid-decision can only appear, never be
    missed. Here it is written BY the endpoint read itself."""
    b = _Bench(tmp_path)
    b.queue(ago=60)
    b.tick(NOW)                                   # first clean observation
    b.on_read = lambda: b.delivered(ago=0)        # ...it plays during the second
    b.tick(NOW + POLL)
    assert b.originated == [], "a clip settled mid-decision was replayed"
    assert "announce-retry-attempted" not in b.outcomes()


def test_a_verdict_of_any_kind_stops_a_replay(tmp_path):
    """ANNOUNCE_TERMINAL is three names, and all three are terminal for the retry:
    a clip the reconciler has already judged must not be re-originated behind its
    verdict."""
    for outcome in ("announce-undelivered", "announce-unsettled"):
        b = _Bench(tmp_path / outcome)
        b.queue(ago=60)
        b.row(outcome, ago=55)
        b.ticks(3)
        assert b.originated == [], (outcome, b.originated)


def test_the_terminal_set_has_one_definition_and_three_members(tmp_path):
    """The retry's veto and the reconciler's exclusion must be the SAME set. Two
    copies of a three-name tuple is how one starts replaying what the other has
    already judged, with every test green."""
    mod = _delivery(tmp_path)
    assert mod.ANNOUNCE_TERMINAL == (mod.AUDIO_DELIVERED, mod.ANNOUNCE_UNDELIVERED,
                                     mod.ANNOUNCE_UNSETTLED)
    # ...and the rows the retry writes itself are deliberately NOT in it: the
    # announcement still never arrived, so the reconciler must still speak.
    for outcome in (mod.ANNOUNCE_RETRY_ATTEMPTED, mod.ANNOUNCE_RETRY_SKIPPED,
                    mod.ANNOUNCE_ORIGINATE_FAILED, mod.ANNOUNCE_GUARD_UNJUDGED,
                    mod.ANNOUNCE_QUEUED):
        assert outcome not in mod.ANNOUNCE_TERMINAL, outcome
    src = (WEBUI / "delivery.py").read_text()
    body = src[src.index("def unresolved_announcements("):src.index("def retryable_announcements(")]
    assert "ANNOUNCE_TERMINAL" in body
    assert "ANNOUNCE_UNSETTLED)" not in body, (
        "the reconciler spells the terminal set inline again — it must share "
        "ANNOUNCE_TERMINAL with the retry")


def test_the_room_having_been_spoken_to_since_retires_the_clip(tmp_path):
    """EXT FRESHNESS. A newer announcement to the same room has been delivered, so
    replaying the older one speaks stale content into a room that has moved on.
    34 of the 35 announcements on this build went to one extension."""
    b = _Bench(tmp_path)
    b.queue(ago=90)
    b.queue(sound="ann-19-" + "b" * 32, ago=45)          # the newer message...
    b.delivered(sound="ann-19-" + "b" * 32, ago=30)      # ...and it arrived
    b.ticks(3)
    assert b.originated == []
    skipped = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"]
    assert len(skipped) == 1 and skipped[0]["reason"] == "ext-superseded", skipped


def test_a_newer_announcement_to_the_same_room_is_the_one_replayed(tmp_path):
    """★ ONE LIVE CANDIDATE PER ROOM, even when NEITHER has played.

    Two unresolved announcements to one handset are two Originates that would
    both be cleared to fire by ONE endpoint read: neither can see the other's
    call, and a second INVITE to the cordless does not auto-answer, it rings as
    call waiting. The newest is the message the house is currently owed; the
    older is retired with a row that says why, rather than replayed after it and
    out of order.

    Not a hypothetical population: an originate whose pre-flight could not judge
    deliberately stops arming the duplicate window (test_app.py), so a Home
    Assistant re-send during exactly the AMI-blind window this feature exists for
    now lands as several queued rows where v0.104.0 collapsed them to one."""
    b = _Bench(tmp_path)
    older, newer = "ann-19-" + "a" * 32, "ann-19-" + "b" * 32
    b.queue(sound=older, ago=90)
    b.queue(sound=newer, ago=45)
    b.ticks(3)
    assert [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [newer], b.originated
    skipped = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"]
    assert len(skipped) == 1, skipped
    assert (skipped[0]["sound"], skipped[0]["reason"]) == (older, "ext-superseded")


def test_two_announcements_queued_in_the_same_second_still_replay_once(tmp_path):
    """The tie a whole-second stamp makes possible — and the one shape that can
    put two candidates for one handset in front of the scheduler in one tick.
    Ledger order, which is the producer's own order inside that second, decides;
    the loser is retired rather than played after the winner."""
    b = _Bench(tmp_path)
    first, second = "ann-19-" + "1" * 32, "ann-19-" + "2" * 32
    b.queue(sound=first, ago=40)
    b.queue(sound=second, ago=40)
    b.ticks(3)
    assert [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [second], b.originated
    skipped = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"]
    assert [(r["sound"], r["reason"]) for r in skipped] == [(first, "ext-superseded")]


def test_a_late_delivery_of_an_older_announcement_keeps_the_newer_replay(tmp_path):
    """★ THE LOCK READS THE QUEUE TIMES, NOT WHEN AUDIO ARRIVED. A 60 s clip
    answered before this announcement was even queued writes its `audio-delivered`
    row a minute AFTER it — which read as "the room has been spoken to since"
    while the truth was the reverse, and retired the one message the room had not
    heard. Reachable only through the fail-open busy guard, which is precisely the
    read this whole feature exists because of."""
    b = _Bench(tmp_path)
    older = "ann-19-" + "d" * 32
    b.queue(sound=older, ago=120)                 # queued FIRST...
    b.queue(ago=60)                               # ...this one is newer...
    b.delivered(sound=older, ago=55)              # ...and the older one lands last
    b.ticks(3)
    assert [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [SOUND], b.originated
    assert [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"] == []


def test_at_most_one_replay_per_extension_per_tick(tmp_path):
    """★ THE SECOND LOCK ON THE SAME HAZARD, downstream of the ledger.

    delivery.retryable_announcements returns only the newest clip per extension,
    so this guard is what stands if that ever changes — and being wrong here
    means unsolicited ringing in a house of antique phones. The candidate list is
    widened here (real rows, `stale_reason` stripped) precisely because the pass
    above would not hand the scheduler two live clips for one handset.

    The held-back clip gives its observations BACK rather than banking them: they
    were earned against the one endpoint read this tick has just invalidated by
    calling that handset."""
    b = _Bench(tmp_path)
    one, two = "ann-19-" + "3" * 32, "ann-19-" + "4" * 32
    b.queue(sound=one, ago=40)
    b.queue(sound=two, ago=40)
    real = b.delivery.retryable_announcements

    def _uncollapsed(now, not_before=None, recs=None, **kw):
        """Both clips, each judged on its OWN rows — what the ledger pass would
        hand over if its one-per-extension rule were ever removed. Every other
        rule (the budget, the ages, the terminal join) is still the real one."""
        rows = b.delivery.announce_records(now, not_before=not_before)
        out = []
        for name in (one, two):
            out += real(now, not_before=not_before,
                        recs=[r for r in rows if r.get("sound") == name], **kw)
        return sorted(out, key=lambda c: c["queued_ts"])
    b.delivery.retryable_announcements = _uncollapsed
    try:
        per_tick = []
        for i in range(6):
            before = len(b.originated)
            b.tick(NOW + i * POLL)
            per_tick.append(len(b.originated) - before)
            if len(b.originated) == 1 and per_tick[-1] == 1:
                played = b.originated[0][1].rsplit("/", 1)[-1]
                held = one if played == two else two
                assert b.sched._retry_seen[b.delivery.clip_key(held)]["clean"] == 0
        # ★ Never two INVITEs to one handset off one endpoint read...
        assert max(per_tick) <= 1, per_tick
        # ...and the guard DEFERS rather than drops: the clip it held back goes
        # out on a later tick, against a read taken after the earlier call.
        assert {s.rsplit("/", 1)[-1] for _e, s in b.originated} == {one, two}, \
            b.originated
        assert any("has already been re-originated this pass" in m
                   for m in b.sched.LOGGED), b.sched.LOGGED
    finally:
        b.delivery.retryable_announcements = real


def test_a_delivery_to_another_room_does_not_retire_it(tmp_path):
    """...and the lock is per-extension. Somebody else's announcement arriving is
    not this room being spoken to."""
    b = _Bench(tmp_path, exts=("19", "18"))
    b.queue(ago=60)
    b.delivered(sound="ann-18-" + "c" * 32, ext="18", ago=30)
    b.ticks(2)
    assert [e for e, _s in b.originated] == ["19"], b.originated


def _sink_leg(b, sound, txcount, stage="complete", billsec=7):
    """Run the REAL switchboard-callqos sink over one announce leg, so what does
    and does not write an `audio-delivered` row is decided by the shipped code."""
    cq = SourceFileLoader("sw_callqos_retry",
                          str(ROOT / "rootfs" / "usr" / "bin" / "switchboard-callqos")
                          ).load_module()
    saved = (sys.modules.get("delivery"), cq.append_record, cq.append_outcome, cq.push_ha)
    sys.modules["delivery"] = b.delivery
    cq.append_record = lambda rec: None
    cq.append_outcome = lambda rec: None
    cq.push_ha = lambda rec: None
    try:
        cq.main(["--source", "dialplan", "--tag", "announce",
                 "--chan", "PJSIP/19-0000002a", "--cid", "19",
                 "--billsec", str(billsec), "--hcause", "16", "--stage", stage,
                 "--sound", sound, "--rxcount", "600", "--txcount", str(txcount),
                 "--rxmes", "88", "--txmes", "88"])
    finally:
        if saved[0] is None:
            sys.modules.pop("delivery", None)
        else:
            sys.modules["delivery"] = saved[0]
        cq.append_record, cq.append_outcome, cq.push_ha = saved[1], saved[2], saved[3]
    return cq


def test_a_leg_that_played_writes_the_row_that_stops_the_replay(tmp_path):
    """The lock, end to end through the real sink: a leg that played writes
    `audio-delivered` with the same clip name, and the retry never lists it."""
    b = _Bench(tmp_path)
    b.queue(ago=40)
    _sink_leg(b, SOUND, txcount=600)
    assert b.delivery.AUDIO_DELIVERED in b.outcomes(), b.outcomes()
    b.ticks(4)
    assert b.originated == []


def test_a_playback_too_short_to_be_recorded_IS_replayed(tmp_path):
    """★ THE ACCEPTED RESIDUAL, named and pinned rather than discovered later.

    callqos only writes `audio-delivered` once at least a second of audio came out
    of the handset (DELIVERED_MIN_TXCOUNT = 50 packets). A leg that answered and
    transmitted 1-49 packets therefore writes NO row, so the retry sees an
    unresolved announcement and replays it — audio that DID partly reach the
    handset is spoken again, up to twice.

    That is the owner's decision applied literally ("whose audio never played"),
    and a sub-second burst is not a delivered announcement. It is also a genuine
    disagreement with test_announce_reconcile's "a truncated announcement still
    counts as arrived", which is about a leg ABOVE that floor. Two readers, one
    threshold between them: behaviour left as it is, and written down here so the
    next person does not have to rediscover which side of 50 they are on.
    """
    b = _Bench(tmp_path)
    b.queue(ago=40)
    cq = _sink_leg(b, SOUND, txcount=2, stage="playing", billsec=0)
    assert cq.DELIVERED_MIN_TXCOUNT == 50, (
        "the threshold this residual is measured against has moved")
    assert b.delivery.AUDIO_DELIVERED not in b.outcomes(), (
        "fixture premise: a 2-packet leg writes no delivered row")
    b.ticks(2)
    assert len(b.originated) == 1, "the accepted residual has changed shape"


# --------------------------------------------------------------------------- #
# 2. ★ The restart boundary. A run that may not JUDGE a window may not REPLAY
#    into it either (feedback_reconciler_upgrade_boundary).
# --------------------------------------------------------------------------- #
def test_nothing_from_before_this_process_started_is_replayed_or_judged(tmp_path):
    """A restart inside the retry window abandons the clip, deliberately: the clip
    lives in tmpfs the restart clears, the resolving record comes from a detached
    process the restart kills, and before an upgrade it may have been a build that
    wrote none. So the new process neither replays it nor counts its attempts nor
    files a verdict for it."""
    b = _Bench(tmp_path)
    started = b.sched._STARTED
    b.row(b.delivery.ANNOUNCE_QUEUED, ago=NOW - (started - 60))
    b.row(b.delivery.ANNOUNCE_RETRY_ATTEMPTED, ago=NOW - (started - 40), attempt=1)
    _write_clip(b.clips, SOUND)
    b.ticks(4)
    assert b.originated == [], "an announcement from before the restart was replayed"
    assert b.outcomes() == ["originate-queued", "announce-retry-attempted"], b.outcomes()


def test_attempts_after_the_boundary_are_counted_from_the_ledger(tmp_path):
    """★ THE BUDGET LIVES ON DISK. Counting attempts in memory would let the
    restart that causes this defect hand the same clip a fresh budget — and the
    clip is still on the tmpfs for the first five minutes, so it would play."""
    b = _Bench(tmp_path)
    b.queue(ago=100)
    b.attempted(ago=80, attempt=1)
    b.attempted(ago=50, attempt=2)
    b.ticks(3)
    assert b.originated == []
    skipped = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"]
    assert len(skipped) == 1 and skipped[0]["reason"] == "budget-exhausted", skipped
    assert skipped[0]["attempts"] == 2


def test_the_process_start_is_actually_stamped(tmp_path):
    """★ The mutant feedback_reconciler_upgrade_boundary names: every test above
    sets _STARTED by hand to control its fixture, so none of them can tell whether
    the module stamps it at all. `_STARTED = 0.0` would make the boundary inert in
    production with the whole suite green — and the first restart would replay an
    announcement out of a window nobody may judge."""
    before = time.time()
    b = _Bench(tmp_path)
    after = time.time()
    assert before - 60 <= b.sched._STAMPED_AT <= after + 1, (
        f"_STARTED is {b.sched._STAMPED_AT}, not this process's start time — the "
        f"restart boundary is inert")


def test_the_retry_asks_for_the_boundary_by_name(tmp_path):
    """The parameter is useless if the one caller does not pass it, and the one
    caller is what stands between a restart and a replay out of a window nobody
    can judge."""
    body = _retry_code()
    assert body.count("not_before=_STARTED") == 2, (
        "both the candidate scan and the final veto read must carry the boundary")


def _retry_code() -> str:
    """_retry_announcements' CODE, with its docstring and comments stripped.

    The prose in this file explains the mechanism by name, so a scanner that reads
    it finds the words whether or not the code does them — which is how a test
    that asserts a call site passes over a call site that is gone."""
    src = (WAKEUP / "scheduler.py").read_text()
    body = src[src.index("def _retry_announcements("):src.index("def _record_retry_failure(")]
    body = re.sub(r'"""(?:.|\n)*?"""', "", body)
    return "\n".join(l.split("#", 1)[0] for l in body.split("\n"))


# --------------------------------------------------------------------------- #
# 3. ★ The green light: POSITIVE only, twice, one poll apart.
# --------------------------------------------------------------------------- #
def test_one_clean_observation_is_not_enough(tmp_path):
    b = _Bench(tmp_path)
    b.queue(ago=60)
    b.tick(NOW)
    assert b.originated == [], "replayed on a single observation"
    b.tick(NOW + POLL)
    assert [e for e, _s in b.originated] == ["19"], b.originated


def test_a_not_green_tick_resets_the_count(tmp_path):
    """Two CONSECUTIVE observations. A handset that reads idle, then rings, then
    reads idle again has not been quiet for a poll."""
    b = _Bench(tmp_path)
    b.queue(ago=40)
    b.tick(NOW)                       # clean 1
    b.state = "Ringing"
    b.tick(NOW + POLL)                # reset
    b.state = "Not in use"
    b.tick(NOW + 2 * POLL)            # clean 1 again
    assert b.originated == [], "the counter was not reset by a busy observation"
    b.tick(NOW + 3 * POLL)            # clean 2
    assert len(b.originated) == 1


def test_only_an_idle_endpoint_is_a_green_light(tmp_path):
    """★ THE INVERSION THAT MATTERS. The announce pre-flight fails OPEN — refusing
    to announce because it could not ask would silence an alarm — and that open
    door is exactly what put an announcement into a void on 2026-09-15. A REPLAY
    inverts it: anything that is not positively "registered, idle, nothing in
    progress" defers."""
    for state in ("", "Unavailable", "Invalid", "Unknown", "Ringing", "In use",
                  "Ring+Inuse", "Busy", "On hold", "Wibbling", "UNAVAILABLE",
                  "RINGING", "INUSE"):
        b = _Bench(tmp_path / f"s{abs(hash(state))}", state=state)
        b.queue(ago=40)
        b.ticks(5)
        assert b.originated == [], f"{state!r} was treated as a green light"
    for state in ("Not in use", "NOT_INUSE", "not in use"):
        b = _Bench(tmp_path / f"g{abs(hash(state))}", state=state)
        b.queue(ago=40)
        b.ticks(2)
        assert len(b.originated) == 1, f"{state!r} was not a green light"


def test_an_endpoint_missing_from_the_read_is_not_green(tmp_path):
    b = _Bench(tmp_path, exts=("18", "14"))
    b.queue(ago=40)
    b.ticks(5)
    assert b.originated == []


def test_an_endpoint_with_an_active_channel_is_not_green(tmp_path):
    """ActiveChannels is SECONDARY and fail-closed: nothing in this repo
    establishes what it holds during a playback, so it may only ever make the
    retry more conservative."""
    b = _Bench(tmp_path, channels="1")
    b.queue(ago=40)
    b.ticks(5)
    assert b.originated == []


def test_an_unreadable_ami_is_a_deferral_and_never_a_verdict(tmp_path):
    """An AMI hiccup must not authorise a replay, and must not record anything
    either — a verdict from a read that failed is the defect that produced this
    feature, written the other way round."""
    b = _Bench(tmp_path)
    b.queue(ago=40)

    def _boom():
        raise RuntimeError("AMI down")
    AMI.get_endpoints = _boom
    b.ticks(4)
    assert b.originated == []
    assert b.outcomes() == ["originate-queued"], b.outcomes()
    # ...and once AMI answers again, the two clean observations still have to be
    # earned from scratch.
    AMI.get_endpoints = b._endpoints
    b.tick(NOW + 4 * POLL)
    assert b.originated == []
    b.tick(NOW + 5 * POLL)
    assert len(b.originated) == 1


def test_the_green_light_is_amis_own_classification(tmp_path):
    """Not a fourth hand-rolled copy of _norm_device_state: the scheduler asks
    ami.device_idle, which reads the same frozenset the pre-flight does."""
    body = _retry_code()
    assert "ami.device_idle(state)" in body
    assert "notinuse" not in body.lower().replace(" ", ""), (
        "the scheduler spells a device state itself instead of asking ami")
    assert AMI.device_idle("Not in use") and AMI.device_idle("NOT_INUSE")
    for s in ("", "Unavailable", "Ringing", "In use", "Ring+Inuse", "Busy",
              "On hold", "Invalid", "Unknown", "wat"):
        assert not AMI.device_idle(s), s


# --------------------------------------------------------------------------- #
# 4. ★ The clip. A name out of a group-writable ledger reaches an Originate.
# --------------------------------------------------------------------------- #
def test_the_originate_gets_the_extensionless_full_path(tmp_path):
    """★ NOT THE BASENAME, and a wrong choice here fails SILENTLY. app.py passes
    `path[:-4]` — the full path without ".wav" — while the ledger stores only the
    basename. A retry that passed the bare name would auto-answer, play nothing,
    keep txcount under callqos's delivered floor, write no delivered row, and burn
    an attempt on a silent call."""
    b = _Bench(tmp_path)
    b.queue(ago=40)
    b.ticks(2)
    assert len(b.originated) == 1
    ext, sound = b.originated[0]
    assert sound.startswith(b.clips.ANNOUNCE_DIR + "/"), sound
    assert not sound.endswith(".wav"), sound
    assert sound.rsplit("/", 1)[-1] == SOUND, sound
    assert Path(sound + ".wav").is_file()


def test_a_forged_or_vanished_clip_name_yields_no_originate(tmp_path):
    """Every one of these arrived as a `sound` field in a ledger that lives in a
    group-writable directory, and each must be refused by the validator rather
    than by luck. `clip-gone` is written once and the clip is retired."""
    # ★ `plant` writes a REAL, PLAYABLE file under that name. Without it these
    # cases pass for the wrong reason — the file's absence rather than the rule —
    # and a mutant that drops the ext binding survives. It did: the row claiming
    # ext 19 named ext 18's clip, and only the missing file refused it.
    cases = [
        ("traversal", "ann-19-../../etc/passwd", False),
        ("another-rooms-clip", "ann-18-" + HEX, True),
        ("not-32-hex", "ann-19-" + "zz" + HEX[2:], True),
        ("short-hex", "ann-19-" + HEX[:31], True),
        ("uppercase-hex", "ann-19-" + HEX.upper(), True),
        ("absent-file", "ann-19-" + "d" * 32, False),
        ("with-extension", "ann-19-" + HEX + ".wav", True),
        ("absolute", "/etc/passwd", False),
    ]
    for label, sound, plant in cases:
        b = _Bench(tmp_path / label)
        b.queue(sound=sound, ago=40, clip=False)
        if plant:
            _write_clip(b.clips, sound)
        b.ticks(3)
        assert b.originated == [], (label, b.originated)
        skipped = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"]
        assert len(skipped) == 1, (label, b.rows())
        assert skipped[0]["reason"] == "clip-gone", (label, skipped)


def test_a_symlinked_clip_is_refused(tmp_path):
    """A link in the announce directory resolves to whatever it points at, and
    Asterisk would happily play that. The retry admits a REGULAR FILE only."""
    b = _Bench(tmp_path)
    b.queue(ago=40, clip=False)
    target = tmp_path / "elsewhere.wav"
    target.write_bytes(b"\0" * 44 + b"\1" * 16000)
    (Path(b.clips.ANNOUNCE_DIR) / (SOUND + ".wav")).symlink_to(target)
    b.ticks(3)
    assert b.originated == []
    assert [r["reason"] for r in b.rows() if r["outcome"] == "announce-retry-skipped"] == ["clip-gone"]


def test_an_empty_or_over_long_clip_is_refused(tmp_path):
    b = _Bench(tmp_path / "empty")
    b.queue(ago=40, clip=False)
    (Path(b.clips.ANNOUNCE_DIR) / (SOUND + ".wav")).write_bytes(b"")
    b.ticks(3)
    assert b.originated == []

    b2 = _Bench(tmp_path / "long")
    b2.queue(ago=40, clip=False)
    _write_clip(b2.clips, SOUND, seconds=b2.delivery.ANNOUNCE_MAX_SECONDS + 10)
    b2.ticks(3)
    assert b2.originated == [], "a clip over the length cap was replayed"


def test_the_clip_directory_has_one_definition():
    """app.py mints clips there and the scheduler plays them from there. A second
    copy of the literal is how one ends up reading a directory the other does not
    write to."""
    app_src = (WEBUI / "app.py").read_text()
    assert 'ANNOUNCE_DIR = announce_clip.ANNOUNCE_DIR' in app_src
    assert app_src.count('"/run/switchboard/announce"') == 0
    clip_src = (WEBUI / "announce_clip.py").read_text()
    assert clip_src.count('ANNOUNCE_DIR = "/run/switchboard/announce"') == 1
    # ...and the length cap still comes from delivery, not from a second env read.
    code = re.sub(r'"""(?:.|\n)*?"""', "",
                  "\n".join(l.split("#", 1)[0] for l in clip_src.split("\n")))
    assert 'os.environ.get("ANNOUNCE_MAX_SECONDS"' not in code
    assert '_delivery, "ANNOUNCE_MAX_SECONDS"' in code


# --------------------------------------------------------------------------- #
# 5. ★ Bounded: attempts, age, and exactly one row when it gives up.
# --------------------------------------------------------------------------- #
def test_two_attempts_and_no_more(tmp_path):
    """The whole budget, driven through the real loop: two Originates, then one
    terminal row and silence."""
    b = _Bench(tmp_path)
    b.queue(ago=20)
    for i in range(12):                       # 240 s of ticks, well past the cap
        b.tick(NOW + i * POLL)
    assert len(b.originated) == 2, (b.originated, b.sched.LOGGED)
    attempts = [r for r in b.rows() if r["outcome"] == "announce-retry-attempted"]
    assert [r["attempt"] for r in attempts] == [1, 2]
    assert [r["of"] for r in attempts] == [2, 2]
    assert all(r["sound"] == SOUND for r in attempts), "the retry re-named the clip"
    skipped = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"]
    assert len(skipped) == 1, skipped


def test_the_two_attempts_are_a_poll_apart(tmp_path):
    """One poll between attempts, so attempt 1's channel is visible before
    attempt 2 is considered: 20 s in it is either still ringing or already dead."""
    b = _Bench(tmp_path)
    b.queue(ago=20)
    b.tick(NOW)
    b.tick(NOW + POLL)
    assert len(b.originated) == 1
    b.tick(NOW + 2 * POLL)
    assert len(b.originated) == 1, "a second attempt inside one poll"


def test_nothing_is_replayed_before_the_minimum_age(tmp_path):
    """★ THE RACE THIS CLOSES. The resolving row lands essentially AT hangup — the
    journal has the `h` extension at 04:45:21.897 and the detached sink spawned at
    .898 — but record() stamps whole seconds, and the sink is a separate process on
    a Pi. One poll before the first attempt is four orders of magnitude of headroom
    over that, and it is the smallest spacing at which this loop can produce two
    independent observations at all."""
    b = _Bench(tmp_path)
    b.queue(ago=5)
    for t in (0.0, 1.0, 2.0, 5.0, 14.0):          # still younger than one poll
        b.tick(NOW + t)
    assert b.originated == [], b.originated
    b.tick(NOW + 16.0)                            # age 21: old enough, first look
    assert b.originated == []
    b.tick(NOW + 36.0)
    assert len(b.originated) == 1


def test_a_second_attempt_waits_a_poll_after_the_first(tmp_path):
    """The same number as the inter-attempt spacing, and it is load-bearing there
    too: 20 s after attempt 1 the handset is either still ringing (30 s Originate
    timeout) or has already failed, so attempt 2 is never fired blind into a call
    that is still in progress. Isolated here by requiring only ONE clean
    observation, so nothing but the minimum age can be what holds it back."""
    b = _Bench(tmp_path)
    b.delivery.ANNOUNCE_RETRY_CLEAN_TICKS = 1
    b.queue(ago=25)
    b.tick(NOW)
    assert len(b.originated) == 1, b.originated
    for t in (1.0, 5.0, 15.0, 19.0):
        b.tick(NOW + t)
    assert len(b.originated) == 1, "a second attempt inside one poll of the first"
    b.tick(NOW + 21.0)
    assert len(b.originated) == 2


def test_a_ledger_that_vanishes_mid_decision_stops_the_replay(tmp_path):
    """The candidate came out of this file moments ago, so an EMPTY tail is not
    "nothing has been recorded" — it is "the ledger is gone", and neither the
    delivered row nor the attempt count can be known. The answer that plays
    nothing is the only safe one, because a missing ledger would otherwise also
    hand the clip a fresh budget."""
    b = _Bench(tmp_path)
    b.queue(ago=60)
    b.tick(NOW)
    b.on_read = lambda: Path(b.delivery.OUTCOME_PATH).unlink()
    b.tick(NOW + POLL)
    assert b.originated == [], "replayed out of a ledger that had disappeared"


def test_a_clip_older_than_the_cap_is_retired_once(tmp_path):
    """An announcement is time-sensitive: "dinner is ready" twenty minutes late is
    worse than never. Past the cap it is retired, exactly once, and the reconciler
    still files the verdict."""
    b = _Bench(tmp_path)
    b.queue(ago=b.delivery.ANNOUNCE_RETRY_MAX_AGE + 1)
    b.ticks(6)
    assert b.originated == []
    skipped = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"]
    assert len(skipped) == 1, b.rows()
    assert skipped[0]["reason"] == "too-old" and skipped[0]["attempts"] == 0
    assert skipped[0]["ext"] == "19" and skipped[0]["sound"] == SOUND
    assert skipped[0]["age_s"] >= int(b.delivery.ANNOUNCE_RETRY_MAX_AGE)


def test_a_clip_inside_the_cap_is_not_retired(tmp_path):
    """The premise of the test above: the cap is what retires it, not the scan."""
    b = _Bench(tmp_path, state="Unavailable")
    b.queue(ago=b.delivery.ANNOUNCE_RETRY_MAX_AGE - 25)
    b.tick(NOW)
    assert [r["outcome"] for r in b.rows()] == ["originate-queued"], b.rows()


def test_the_age_bound_closes_with_no_ami_at_all(tmp_path):
    """A clip must age out correctly while AMI is down: the staleness branch is
    pure ledger arithmetic and needs no endpoint read."""
    b = _Bench(tmp_path)
    b.queue(ago=b.delivery.ANNOUNCE_RETRY_MAX_AGE + 5)

    def _boom():
        raise RuntimeError("AMI down")
    AMI.get_endpoints = _boom
    b.tick(NOW)
    assert b.endpoint_reads == 0, "the retirement path spent an AMI round trip"
    assert [r["reason"] for r in b.rows() if r["outcome"] == "announce-retry-skipped"] == ["too-old"]


def test_a_transient_deferral_records_nothing(tmp_path):
    """★ WHY: a row per deferral is a row every 20 s, and this ledger trims its
    OLDEST records at a 2 MB cap — so a chatty retry would delete the wake-up and
    announcement history the file exists to keep."""
    b = _Bench(tmp_path, state="Unavailable")
    b.queue(ago=25)
    b.ticks(5, step=1.0)                 # five deferrals, all inside the cap
    assert b.outcomes() == ["originate-queued"], b.outcomes()
    assert any("waiting for the handset" in m for m in b.sched.LOGGED)


def test_the_terminal_row_is_written_exactly_once_however_long_it_ticks(tmp_path):
    """announce-retry-skipped is idempotent through the ledger itself: the
    candidate scan excludes any clip that already has one, so no in-memory
    bookkeeping is needed and a restart cannot double it."""
    b = _Bench(tmp_path)
    b.queue(ago=b.delivery.ANNOUNCE_RETRY_MAX_AGE + 1)
    b.ticks(20)
    assert b.outcomes().count("announce-retry-skipped") == 1
    # ...and it survives the scheduler being reloaded (a restart) with the same ledger.
    b.sched = _load_scheduler(b.delivery, b.clips)
    b.ticks(3)
    assert b.outcomes().count("announce-retry-skipped") == 1


def test_the_kill_switch_retries_nothing(tmp_path):
    """ANNOUNCE_RETRY_MAX_ATTEMPTS=0 must be total — no Originate, and no rows
    either, so turning it off does not itself write a history."""
    b = _Bench(tmp_path)
    b.delivery.ANNOUNCE_RETRY_MAX_ATTEMPTS = 0
    b.queue(ago=40)
    b.ticks(10)
    assert b.originated == []
    assert b.outcomes() == ["originate-queued"], b.outcomes()
    assert b.endpoint_reads == 0
    b.sched._delivery = b.delivery
    b.sched.LOGGED = []
    b.sched._log_retry_bounds()
    assert any("announce retry is OFF" in m for m in b.sched.LOGGED), b.sched.LOGGED


def test_a_missing_clip_validator_turns_the_retry_off_and_says_so(tmp_path):
    """announce_clip is imported in a try, like `delivery`, so the scheduler still
    runs without it — but then the retry can neither validate a name nor find a
    file, and must not guess. Silently inert looks identical to working and idle,
    so the startup line says which."""
    b = _Bench(tmp_path)
    b.sched.announce_clip = None
    b.queue(ago=40)
    b.ticks(4)
    assert b.originated == [] and b.outcomes() == ["originate-queued"]
    assert b.endpoint_reads == 0
    b.sched.LOGGED = []
    b.sched._log_retry_bounds()
    assert any("announce retry is OFF" in m and "clip validator" in m
               for m in b.sched.LOGGED), b.sched.LOGGED


# --------------------------------------------------------------------------- #
# 6. ★ The attempt row GATES the Originate, and the ledger gates the pass.
# --------------------------------------------------------------------------- #
def test_an_unrecordable_attempt_is_not_originated(tmp_path):
    """★ TELEMETRY GATES THE ACTION, uniquely here. Everywhere else in this
    codebase a failed record must never stop the delivery it describes — but the
    budget IS these rows, so an attempt that cannot be counted cannot be
    bounded."""
    b = _Bench(tmp_path)
    b.queue(ago=40)
    b.tick(NOW)
    real = b.delivery.record
    b.delivery.record = lambda *a, **k: False
    try:
        b.tick(NOW + POLL)
    finally:
        b.delivery.record = real
    assert b.originated == [], "an uncountable retry was originated anyway"
    assert any("cannot be counted" in m for m in b.sched.LOGGED), b.sched.LOGGED


def test_the_attempt_row_is_written_before_the_originate(tmp_path):
    """Written first, on purpose: a crash between the row and the call costs one
    unused attempt, while a crash the other way round costs an uncounted one."""
    order = []
    b = _Bench(tmp_path)
    b.queue(ago=40)
    real = b.delivery.record

    def _record(ext, kind, outcome, **extra):
        order.append(outcome)
        return real(ext, kind, outcome, **extra)
    b.delivery.record = _record
    b.originate = lambda ext, sound: order.append("originate") or True
    try:
        b.ticks(2)
    finally:
        b.delivery.record = real
    assert order == ["announce-retry-attempted", "originate"], order


def test_an_unwritable_ledger_retries_nothing(tmp_path):
    """The same fail-safe the reconciler has, for a stronger reason: a ledger that
    cannot be written can hold neither the attempt row that bounds the retry nor
    the delivered row that would prove a replay arrived, so its silence proves
    nothing and authorises nothing."""
    b = _Bench(tmp_path)
    b.queue(ago=40)
    before = Path(b.delivery.OUTCOME_PATH).read_text()
    b.delivery.is_writable = lambda: False
    b.ticks(4)
    assert b.originated == []
    assert b.endpoint_reads == 0
    assert Path(b.delivery.OUTCOME_PATH).read_text() == before


def test_an_originate_that_raises_or_is_refused_is_recorded(tmp_path):
    """Both carry the ORIGINAL clip and the attempt number, and reuse
    announce-originate-failed rather than inventing a name: it already means
    exactly this and is already outside ANNOUNCE_TERMINAL, so the announcement
    still gets its verdict."""
    b = _Bench(tmp_path)
    b.queue(ago=40)

    def _raise(ext, sound):
        raise RuntimeError("socket gone")
    b.originate = _raise
    b.ticks(2)
    row = [r for r in b.rows() if r["outcome"] == "announce-originate-failed"]
    assert len(row) == 1 and row[0]["reason"] == "retry-ami-error", b.rows()
    assert row[0]["sound"] == SOUND and row[0]["attempt"] == 1

    b2 = _Bench(tmp_path / "refused")
    b2.queue(ago=40)
    b2.originate = lambda ext, sound: False
    b2.ticks(2)
    row = [r for r in b2.rows() if r["outcome"] == "announce-originate-failed"]
    assert len(row) == 1 and row[0]["reason"] == "retry-refused", b2.rows()
    assert row[0]["attempt"] == 1
    # A refused attempt is still an attempt: it is counted, not retried for free.
    assert [r["outcome"] for r in b2.rows()].count("announce-retry-attempted") == 1


def test_the_attempt_row_carries_the_join_and_the_evidence(tmp_path):
    b = _Bench(tmp_path)
    b.queue(ago=40)
    b.ticks(2)
    row = [r for r in b.rows() if r["outcome"] == "announce-retry-attempted"][0]
    assert row["sound"] == SOUND and row["ext"] == "19" and row["kind"] == "announce"
    assert row["attempt"] == 1 and row["of"] == 2
    assert row["device_state"] == "Not in use"
    assert row["age_s"] == 60 and row["queued"]


# --------------------------------------------------------------------------- #
# 7. ★ The live incident, replayed end to end.
# --------------------------------------------------------------------------- #
def test_the_2026_09_15_incident_now_ends_in_audio(tmp_path):
    """★ ALL TIMINGS FROM host/addon-journal-boot0.txt.

    scheduler start 01:42:05.536 → ticks at :25.5, :45.5, 01:43:05.5, :25.5.
    The announcement was queued at 01:42:12 (t=0 below) and its Originate failed
    at 01:42:12.979 with `Could not create dialog to invalid URI '19'`. ext 19's
    contact returned at 01:42:50.875, 37.9 s later.

      t=13.5  younger than the minimum age — not a candidate
      t=33.5  candidate; ext 19 still Unavailable → deferred
      t=53.5  idle: observation 1
      t=73.5  idle: observation 2 → RETRY, clip plays, delivered row lands
      t=193   the reconciler's horizon from the ATTEMPT has not passed, and the
              delivered row has resolved it anyway: no verdict, ever.
    """
    b = _Bench(tmp_path, state="Unavailable")
    b.sched._STARTED = NOW - 6.5                  # 6.5 s of add-on uptime, as live
    b.queue(ago=0)                                # queued at t=0
    b.row("announce-guard-unjudged", ago=0, reason="state-unreadable")
    b.tick(NOW + 13.5)
    assert b.originated == [] and b.outcomes().count("announce-retry-attempted") == 0
    b.tick(NOW + 33.5)
    assert b.originated == []
    b.state = "Not in use"                        # 37.9 s in, the contact is back
    b.tick(NOW + 53.5)
    assert b.originated == [], "replayed on one observation"
    b.tick(NOW + 73.5)
    assert [e for e, _s in b.originated] == ["19"], b.originated
    # The clip plays, so callqos writes the delivered row against the same name.
    b.delivered(ago=-75.0)
    b.sched._reconcile_announcements(NOW + 193.0,
                                    b.delivery.announce_records(
                                        NOW + 193.0, not_before=b.sched._STARTED))
    assert "announce-unsettled" not in b.outcomes(), b.outcomes()
    assert "announce-undelivered" not in b.outcomes(), b.outcomes()


def _ring_out_run(b, cap=None, unavailable_until=0.0, phase=13.5):
    """Drive the real loop through a RING-OUT: every attempt rings the handset for
    ANNOUNCE_RING_SECONDS and is never answered, so the endpoint reads Ringing for
    30 s after each originate. `unavailable_until` is how long the handset has no
    contact at all first. Returns the age of each attempt."""
    if cap is not None:
        b.delivery.ANNOUNCE_RETRY_MAX_AGE = cap
    b.queue(ago=0)
    fired = []
    b.originate = lambda ext, sound: fired.append(b.now) or True
    t = phase
    while t <= 200.0:
        b.now = t
        if t < unavailable_until:
            b.state = "Unavailable"          # not re-registered yet
        elif any(f <= t < f + 30.0 for f in fired) or t < 30.0:
            b.state = "Ringing"              # an attempt (or the original) is ringing
        else:
            b.state = "Not in use"
        b.tick(NOW + t)
        t += POLL
    return fired


def test_the_ring_out_shape_reaches_both_attempts(tmp_path):
    """A handset that is registered and never answers reads Ringing for the full
    30 s Originate timeout, so each attempt costs 30 s of deferrals plus two clean
    observations. Both attempts must still fit."""
    b = _Bench(tmp_path)
    fired = _ring_out_run(b)
    assert len(fired) == 2, (fired, b.sched.LOGGED)
    assert fired[1] - fired[0] >= b.delivery.ANNOUNCE_RETRY_MIN_AGE
    # ★ AND THE MANUAL MUST SAY SO. This is not only the post-restart shape: the
    # gate is the AUDIO, not the reason it was missing, so a registered room phone
    # that nobody picks up is a green light the moment it stops ringing — up to
    # three rings for one message on the FXS phones, which have no auto-answer.
    # The manual motivated the feature entirely with the restart incident, which a
    # reader would fairly take as its only trigger.
    docs = (ROOT / "DOCS.md").read_text()
    assert "announcement nobody answers is retried too" in docs, (
        "DOCS.md does not tell the owner that an UNANSWERED announcement is "
        "replayed as well — the behaviour they will actually notice")


def test_the_second_attempt_would_be_unreachable_at_the_settling_window(tmp_path):
    """★ WHY THE CAP IS 150 s AND NOT ANNOUNCE_SETTLE_SECONDS, driven rather than
    argued.

    The measured re-registration band on this build is 30-45 s, and the top of it
    is the case that decides the number: with the handset back at 46 s, attempt 1
    cannot fire before ~74 s, it then rings for 30 s, and the two clean
    observations after that put attempt 2 at ~134 s. Inside the shipped 150 s cap;
    OUTSIDE the 120 s settling window both earlier designs reused. A bound that
    cannot fire for a whole population is a feature that looks shipped and is not,
    and this codebase has already done that once.
    """
    b = _Bench(tmp_path)
    fired = _ring_out_run(b, unavailable_until=46.0)
    assert len(fired) == 2, (fired, b.sched.LOGGED)
    assert fired[1] > b.delivery.ANNOUNCE_SETTLE_SECONDS, (
        f"attempt 2 landed at {fired[1]}s, so this shape would not have proved "
        f"anything about a {b.delivery.ANNOUNCE_SETTLE_SECONDS}s cap")
    # ...and the same run under that cap reaches ONE attempt.
    b2 = _Bench(tmp_path / "at120")
    fired2 = _ring_out_run(b2, cap=b2.delivery.ANNOUNCE_SETTLE_SECONDS,
                           unavailable_until=46.0)
    assert len(fired2) == 1, (fired2, b2.sched.LOGGED)


# --------------------------------------------------------------------------- #
# 8. ★ Invariants. Each of these is a number that could quietly stop working.
# --------------------------------------------------------------------------- #
def _cleanup_max_age_default() -> int:
    """_cleanup_announce_dir's own default, read out of app.py's SIGNATURE.

    Written as a literal here it would pin nothing: the clip's life is what the
    retry's age cap is derived from, so the test has to fail when THAT number
    moves, not when a copy of it does."""
    import ast
    tree = ast.parse((WEBUI / "app.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cleanup_announce_dir":
            return int(ast.literal_eval(node.args.defaults[-1]))
    raise AssertionError("_cleanup_announce_dir not found in app.py")


def test_i1_the_retry_and_the_reconciler_never_fight_over_a_clip(tmp_path):
    """The retry works below its age cap and the reconciler above the horizon, so
    the two populations are DISJOINT — which is what makes one shared ledger read
    safe and the order of the two passes irrelevant. Raise the cap above the
    horizon and they start racing over the same clip."""
    mod = _delivery(tmp_path)
    assert mod.ANNOUNCE_RETRY_MAX_AGE < mod.ANNOUNCE_HORIZON, (
        f"retry cap {mod.ANNOUNCE_RETRY_MAX_AGE}s reaches into the reconciler's "
        f"horizon ({mod.ANNOUNCE_HORIZON}s)")


def test_i2_a_retry_can_never_outlive_its_own_clip(tmp_path):
    """The binding physical ceiling. The clip is pruned by mtime at the top of
    every announce POST, and a retry's clip must survive the ring plus the whole
    clip after the Originate: 150 + 30 + 90 = 270 < 300."""
    mod = _delivery(tmp_path)
    ceiling = _cleanup_max_age_default()
    assert (mod.ANNOUNCE_RETRY_MAX_AGE + mod.ANNOUNCE_RING_SECONDS
            + mod.ANNOUNCE_MAX_SECONDS) < ceiling, (
        f"a retry started at {mod.ANNOUNCE_RETRY_MAX_AGE}s could still be playing "
        f"when the clip is pruned at {ceiling}s")


def test_i3_the_retry_is_not_inert(tmp_path):
    """★ A BOUND THAT CANNOT FIRE. The first attempt cannot happen before
    MIN_AGE + CLEAN_TICKS polls; if that already exceeds the age cap, every
    announcement is retired as too-old before it can be replayed — and every
    number is individually sane, so nothing else notices. This codebase has
    shipped one such bound already."""
    mod = _delivery(tmp_path)
    sched = _load_scheduler(mod, _clip_mod(tmp_path))
    assert sched.POLL == 20, f"poll is {sched.POLL}s; the derivation below assumes 20"
    reach = mod.ANNOUNCE_RETRY_MIN_AGE + mod.ANNOUNCE_RETRY_CLEAN_TICKS * sched.POLL
    assert reach < mod.ANNOUNCE_RETRY_MAX_AGE, (
        f"the first attempt cannot be reached before {reach}s, past the "
        f"{mod.ANNOUNCE_RETRY_MAX_AGE}s cap — the retry is inert")
    # ...and there is room for a SECOND attempt too, which is the whole reason
    # the cap is 150 s rather than the settling window's 120 s.
    assert reach + mod.ANNOUNCE_RETRY_MIN_AGE < mod.ANNOUNCE_RETRY_MAX_AGE
    # The same check at runtime, so a bad environment override says so in the log.
    sched.LOGGED = []
    sched.log = lambda m: sched.LOGGED.append(m)
    sched._delivery = mod
    sched._log_retry_bounds()
    assert any("announce retry: up to 2 attempt(s)" in m for m in sched.LOGGED), sched.LOGGED
    assert not any("INERT" in m for m in sched.LOGGED), sched.LOGGED
    mod.ANNOUNCE_RETRY_MAX_AGE = 40.0
    sched.LOGGED = []
    sched._log_retry_bounds()
    assert any("INERT" in m for m in sched.LOGGED), sched.LOGGED


def test_the_age_cap_is_not_the_settling_window(tmp_path):
    """★ THE TWO NUMBERS ANSWER DIFFERENT QUESTIONS. 120 s is the right answer to
    "has the PBX come back yet?" and the wrong quantity for "how late may a replay
    still start?", because the endpoint is not even available until 38-46 s into
    that window. Written down so the next reader does not helpfully collapse
    them."""
    mod = _delivery(tmp_path)
    assert mod.ANNOUNCE_RETRY_MAX_AGE != mod.ANNOUNCE_SETTLE_SECONDS
    assert mod.ANNOUNCE_RETRY_MAX_AGE == 150.0
    src = (WEBUI / "delivery.py").read_text()
    block = src[src.index("# ★ HOW LATE A REPLAY MAY STILL START"):
                src.index("ANNOUNCE_RETRY_MAX_AGE = float(")]
    assert "ANNOUNCE_SETTLE_SECONDS" in block, (
        "the constant does not say why it is not the settling window")


def test_every_bound_can_be_overridden_from_the_environment():
    """Every bound is an environment read, so a bad one can be corrected on the
    box without a build. Only the attempt count is exposed as an add-on option
    (below); the timings are derived from each other and from this loop's poll,
    and are deliberately not four independent dials."""
    src = (WEBUI / "delivery.py").read_text()
    for name in ("ANNOUNCE_RETRY_MAX_ATTEMPTS", "ANNOUNCE_RETRY_MIN_AGE",
                 "ANNOUNCE_RETRY_CLEAN_TICKS", "ANNOUNCE_RETRY_MAX_AGE"):
        assert f'os.environ.get("{name}"' in src, name


def test_the_off_switch_actually_reaches_the_scheduler():
    """★ AN OPTION IS INERT UNTIL A RUN SCRIPT EXPORTS IT — and an environment
    variable nothing in the add-on ever sets is not a kill switch, it is a
    sentence in a manual. The scheduler reads os.environ; the Supervisor writes
    options; the `export` in the run script is the only bridge between them, and
    it is the edit this project has forgotten more often than any other. It
    matters here because this is the owner's way to stop a feature that PLACES
    PHONE CALLS in a house of antique phones.

    Four edits, and this pins all four plus the name that joins them."""
    import yaml
    run = (ROOT / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d"
           / "wakeup-scheduler" / "run").read_text()
    m = re.search(r'^([A-Z_][A-Z0-9_]*)="\$\(switchboard-opt\s+'
                  r'announce_retry_attempts\s*\)"', run, re.M)
    assert m, "the scheduler's run script never reads announce_retry_attempts"
    assert re.search(rf'^export\s+ANNOUNCE_RETRY_MAX_ATTEMPTS='
                     rf'"\$\{{{m.group(1)}:-2\}}"', run, re.M), run
    # The exported NAME must be the one the code reads: a typo here is a knob
    # that turns nothing, with every test green.
    assert ('os.environ.get("ANNOUNCE_RETRY_MAX_ATTEMPTS"'
            in (WEBUI / "delivery.py").read_text())
    # ★ v0.106.0 — and the WEB UI needs it too, because its duplicate check asks
    # whether a replay is still coming before it holds back an identical
    # announcement. Unbridged there, `delivery` reads the built-in default and
    # the web UI holds back repeats that no retry will ever send — the option
    # would be half-off: the retry stopped, the repeats still suppressed.
    webui_run = (ROOT / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d"
                 / "webui" / "run").read_text()
    w = re.search(r'^([A-Z_][A-Z0-9_]*)="\$\(switchboard-opt\s+'
                  r'announce_retry_attempts\s*\)"', webui_run, re.M)
    assert w, "the web UI's run script never reads announce_retry_attempts"
    assert re.search(rf'^export\s+ANNOUNCE_RETRY_MAX_ATTEMPTS='
                     rf'"\$\{{{w.group(1)}:-2\}}"', webui_run, re.M), webui_run
    # ...and it must be exported BEFORE the exec that replaces the shell.
    assert (webui_run.index("export ANNOUNCE_RETRY_MAX_ATTEMPTS=")
            < webui_run.index("exec python3 -m uvicorn")), webui_run
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert cfg["options"]["announce_retry_attempts"] == 2
    # 0 must be INSIDE the range, or the documented off switch cannot be set.
    assert cfg["schema"]["announce_retry_attempts"] == "int(0,3)?"
    # ...and anything ANNOUNCE_RETRY_* the manual tells the owner to set must be
    # bridged by that same script. The manual used to promise all four.
    docs = (ROOT / "DOCS.md").read_text()
    for name in sorted(set(re.findall(r"ANNOUNCE_RETRY_[A-Z_]+", docs))):
        assert f"export {name}=" in run, (
            f"DOCS.md tells the reader about {name}, which no run script exports")


# --------------------------------------------------------------------------- #
# 9. ★ The cost on the alarm-clock loop, and the wiring that keeps it separate.
# --------------------------------------------------------------------------- #
def test_one_ledger_read_per_tick_and_no_ami_when_nothing_is_wrong(tmp_path):
    """The loop's load-bearing job is ringing wake-ups. A quiet tick must cost one
    tail read and zero AMI traffic — at the measured rate (3 candidates in 4.5
    days) that is every tick."""
    b = _Bench(tmp_path)
    b.queue(ago=60)
    b.delivered(ago=50)
    reads = []
    real = b.delivery._read_records
    b.delivery._read_records = lambda since: reads.append(since) or real(since)
    try:
        now = NOW
        recs = b.delivery.announce_records(now, not_before=b.sched._STARTED)
        b.sched._reconcile_announcements(now, recs)
        b.sched._retry_announcements(now, recs)
    finally:
        b.delivery._read_records = real
    assert len(reads) == 1, f"{len(reads)} ledger reads in one tick"
    assert b.endpoint_reads == 0


def test_the_shared_read_and_the_file_read_agree(tmp_path):
    """Both callers must be able to pass `recs` or not. If the two paths could
    disagree, every test here would be testing the wrong one."""
    b = _Bench(tmp_path)
    b.queue(ago=60)
    direct = b.delivery.retryable_announcements(NOW, not_before=b.sched._STARTED)
    shared = b.delivery.retryable_announcements(
        NOW, not_before=b.sched._STARTED,
        recs=b.delivery.announce_records(NOW, not_before=b.sched._STARTED))
    assert direct == shared and len(direct) == 1


def test_a_caller_cannot_widen_the_boundary_with_its_own_read(tmp_path):
    """The floor is applied to a caller-supplied list too, rather than trusted:
    one of these boundaries is the restart rule, and a pass that inherited a wider
    window from its caller would replay out of a window nobody may judge."""
    b = _Bench(tmp_path)
    started = b.sched._STARTED
    b.row(b.delivery.ANNOUNCE_QUEUED, ago=NOW - (started - 60))     # before the start
    b.queue(sound="ann-19-" + "e" * 32, ago=60)                     # after it
    wide = b.delivery.announce_records(NOW, not_before=None, lookback=7200)
    got = b.delivery.retryable_announcements(NOW, not_before=started, recs=wide)
    assert [c["sound"] for c in got] == ["ann-19-" + "e" * 32], got


def test_the_retry_gets_its_own_try_block_after_the_reconciler(tmp_path):
    """Three separate try blocks, because a retry that raises must not stop
    verdicts being filed, and neither may stop tick() ringing alarm clocks."""
    src = (WAKEUP / "scheduler.py").read_text()
    loop = src[src.index("    while not _stop:"):]
    assert loop.count("try:") >= 3, loop
    assert (loop.index("tick()") < loop.index("_reconcile_announcements(")
            < loop.index("_retry_announcements(")), "the alarm clock must come first"
    # ...and one read, one clock, shared by both passes.
    assert loop.count("_announce_records(") == 1
    assert "_reconcile_announcements(announce_now, announce_recs)" in loop
    assert "_retry_announcements(announce_now, announce_recs)" in loop


def test_a_raising_retry_cannot_stop_the_verdicts_or_the_alarm(tmp_path):
    """Driven through main() itself, not merely read out of the source: one
    iteration of the real loop body with a retry that raises. The alarm clock
    still rings and the verdict is still filed."""
    import types
    b = _Bench(tmp_path)
    b.queue(ago=400)                       # older than the horizon: a verdict is due
    rang = []

    def _tick():
        rang.append("tick")
        b.sched._stop = True               # exactly one iteration
    b.sched.tick = _tick

    def _boom(now, recs=None):
        raise RuntimeError("retry exploded")
    b.sched._retry_announcements = _boom
    b.sched._stop = False
    # The loop's own clock, so the fixture ages are the ones written above rather
    # than whatever the wall clock says while the suite runs.
    b.sched.time = types.SimpleNamespace(time=lambda: NOW, sleep=lambda s: None)
    b.sched.main()
    assert rang == ["tick"], "the alarm clock did not run"
    assert "announce-undelivered" in b.outcomes(), b.outcomes()
    assert any("announce retry error" in m for m in b.sched.LOGGED), b.sched.LOGGED
    # ...and main() PRINTS the retry's bounds. That line is the only production
    # signal separating "live and idle" from "OFF because an import failed",
    # "OFF because the attempt count is 0" and "INERT at this poll" — and its
    # own test called it directly, so deleting the call site kept the suite green.
    assert any("announce retry: up to" in m for m in b.sched.LOGGED), b.sched.LOGGED


def test_the_retry_never_pushes(tmp_path):
    """No new notifications, by instruction and by consistency: an announcement is
    not an alarm clock, and the reconciler beside it deliberately does not push."""
    pushed = []
    b = _Bench(tmp_path)
    b.sched.ha_client = type("H", (), {"notify": staticmethod(
        lambda *a, **k: pushed.append(a) or True)})
    b.queue(ago=40)
    b.ticks(3)
    assert len(b.originated) == 1 and pushed == []
    body = _retry_code()
    assert "ha_client" not in body and "notify" not in body


# --------------------------------------------------------------------------- #
# ★ THE PRE-SEND GUARD'S REFUSALS ARE REPLAYED TOO (v0.105.2).
#
# Until v0.105.2 app.py's device-state read came back "" on 28 of 29
# announcements — the AMI reader stopped between Asterisk's two writes of the
# Getvar reply — so neither refusal ever fired and every announcement reached
# Originate, where this retry could see it. Repairing the read makes both
# refusals live. Each is an announcement whose audio never played, so each must
# be replayed under exactly the rules above, or the repair would have turned
# "originated, failed, replayed" into "refused, never replayed".
# --------------------------------------------------------------------------- #
def _refuse(b, outcome, sound=SOUND, ext="19", ago=60.0, clip=True, state=None):
    b.row(outcome, sound=sound, ext=ext, ago=ago, device_state=state)
    if clip and sound:
        _write_clip(b.clips, sound)


def test_both_refusals_are_the_retry_population(tmp_path):
    b = _Bench(tmp_path)
    assert set(b.delivery.ANNOUNCE_GUARD_REFUSED) == {"skipped-busy", "unreachable"}
    # ...and neither is a verdict about audio, or it would retire its own clip.
    assert not set(b.delivery.ANNOUNCE_GUARD_REFUSED) & set(b.delivery.ANNOUNCE_TERMINAL)


def test_an_unreachable_refusal_is_replayed_when_the_handset_returns(tmp_path):
    """The restart window, as it now plays out: ext 19 has no contact for the
    30-45 s after an add-on restart, the repaired guard REFUSES the announcement
    instead of originating into the void, and the replay is what delivers it."""
    b = _Bench(tmp_path, state="Unavailable")
    b.sched._STARTED = NOW - 6.5
    _refuse(b, "unreachable", ago=0, state="UNAVAILABLE")
    b.tick(NOW + 13.5)
    assert b.originated == [], "replayed before the minimum age"
    b.tick(NOW + 33.5)
    assert b.originated == [], "replayed to a handset with no contact"
    b.state = "Not in use"
    b.tick(NOW + 53.5)
    assert b.originated == [], "replayed on one observation"
    b.tick(NOW + 73.5)
    assert [e for e, _s in b.originated] == ["19"], b.originated
    att = [r for r in b.rows() if r["outcome"] == "announce-retry-attempted"]
    assert len(att) == 1 and att[0]["sound"] == SOUND, att


def test_a_busy_refusal_is_replayed_once_the_line_is_free(tmp_path):
    b = _Bench(tmp_path, state="In use")
    _refuse(b, "skipped-busy", ago=40, state="INUSE")
    b.ticks(3)
    assert b.originated == [], "replayed on top of a call"
    b.state = "Not in use"
    b.ticks(2, first=NOW + 3 * POLL)
    assert [e for e, _s in b.originated] == ["19"], b.originated


def test_a_refusal_row_without_a_clip_is_never_a_candidate(tmp_path):
    """The rows written before v0.105.2 carry no clip: unjoinable, so never
    replayed and never retired — the same rule as a queue row without one."""
    b = _Bench(tmp_path)
    for outcome in ("skipped-busy", "unreachable"):
        _refuse(b, outcome, sound=None, ago=40)
    b.ticks(4)
    assert b.originated == []
    assert "announce-retry-skipped" not in b.outcomes(), b.outcomes()


def test_a_refused_clip_that_played_is_never_replayed(tmp_path):
    b = _Bench(tmp_path)
    _refuse(b, "skipped-busy", ago=90)
    b.delivered(ago=30)
    b.ticks(3)
    assert b.originated == []


def test_a_newer_refusal_supersedes_an_older_queue_to_the_same_room(tmp_path):
    """One live clip per room, whichever row made it a candidate: the newest ASK
    is the message the house is owed."""
    b = _Bench(tmp_path)
    older, newer = "ann-19-" + "a" * 32, "ann-19-" + "b" * 32
    b.queue(sound=older, ago=90)
    _refuse(b, "skipped-busy", sound=newer, ago=45)
    b.ticks(3)
    assert [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [newer], b.originated
    skipped = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"]
    assert [(r["sound"], r["reason"]) for r in skipped] == [(older, "ext-superseded")]


def test_a_newer_queue_supersedes_an_older_refusal(tmp_path):
    b = _Bench(tmp_path)
    older, newer = "ann-19-" + "a" * 32, "ann-19-" + "b" * 32
    _refuse(b, "unreachable", sound=older, ago=90)
    b.queue(sound=newer, ago=45)
    b.ticks(3)
    assert [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [newer], b.originated
    skipped = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"]
    assert [(r["sound"], r["reason"]) for r in skipped] == [(older, "ext-superseded")]


def test_a_refusal_the_handset_never_recovers_from_is_retired_once(tmp_path):
    """ext 20 never registers, by design: a refusal to it defers, then ages out
    with ONE retirement row, and never originates."""
    b = _Bench(tmp_path, state="Unavailable", exts=("20",))
    snd = "ann-20-" + HEX
    _refuse(b, "unreachable", sound=snd, ext="20", ago=30)
    cap = b.delivery.ANNOUNCE_RETRY_MAX_AGE
    b.ticks(int(cap // POLL) + 4)
    assert b.originated == []
    skipped = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"]
    assert [(r["sound"], r["reason"]) for r in skipped] == [(snd, "too-old")], skipped


def _reconcile(b, at=NOW):
    b.sched._reconcile_announcements(at, b.delivery.announce_records(
        at, not_before=b.sched._STARTED))


def test_a_refusal_that_was_never_replayed_gets_no_verdict(tmp_path):
    """The pre-send guard never handed the clip to Asterisk, so there is no call
    to reach a verdict about: the retry's own rows are its whole record."""
    b = _Bench(tmp_path)
    _refuse(b, "unreachable", ago=400)
    _reconcile(b)
    assert "announce-undelivered" not in b.outcomes(), b.outcomes()
    assert "announce-unsettled" not in b.outcomes(), b.outcomes()


def test_a_replayed_refusal_that_never_played_is_judged(tmp_path):
    """The REPLAY did hand it to Asterisk. From the first attempt on, a refused
    clip is judged exactly like a queued one — horizon from the newest attempt —
    so a replay that rang out ends in a verdict carrying its retries, not in
    silence."""
    b = _Bench(tmp_path)
    _refuse(b, "skipped-busy", ago=400)
    b.attempted(ago=60)
    _reconcile(b)
    assert "announce-undelivered" not in b.outcomes(), \
        "judged a replay that could still be ringing"
    b.attempted(ago=300, attempt=2)
    _reconcile(b, at=NOW + 300)
    verdicts = [r for r in b.rows() if r["outcome"] == "announce-undelivered"]
    assert len(verdicts) == 1, b.outcomes()
    assert verdicts[0]["sound"] == SOUND and verdicts[0].get("retries") == 2, verdicts
    _reconcile(b, at=NOW + 320)
    assert b.outcomes().count("announce-undelivered") == 1, "re-filed on the next tick"


def test_a_replayed_refusal_that_played_is_not_judged(tmp_path):
    b = _Bench(tmp_path)
    _refuse(b, "unreachable", ago=400)
    b.attempted(ago=300)
    b.delivered(ago=280)
    _reconcile(b)
    assert "announce-undelivered" not in b.outcomes(), b.outcomes()


# --------------------------------------------------------------------------- #
# ★ THE SAME WORDS, NOT ONLY THE SAME CLIP (v0.106.0).
#
# A replay is retired `content-delivered` when an announcement with the SAME
# content tag reached the SAME room inside ANNOUNCE_DEDUP_WINDOW_S of the
# candidate's request, with nothing different asked of that room since that
# played or can still play (delivery.content_verdict). The shape it closes: the
# first copy is itself a replay, an identical re-send arrives while it plays, is
# refused as busy, and would otherwise be replayed straight after it.
# --------------------------------------------------------------------------- #
TAG, OTHER = "a" * 12, "b" * 12
CA, CB, CC = "ann-19-" + "1" * 32, "ann-19-" + "2" * 32, "ann-19-" + "3" * 32


def _skipped(b):
    return [(r["sound"], r["reason"]) for r in b.rows()
            if r["outcome"] == "announce-retry-skipped"]


def _refused_copy(b, sound=CB, ago=45, tag=TAG, outcome="skipped-busy"):
    b.row(outcome, sound=sound, ago=ago, digest=tag)
    _write_clip(b.clips, sound)


def test_an_identical_copy_refused_behind_a_replay_is_not_replayed_after_it(tmp_path):
    b = _Bench(tmp_path)
    b.row("unreachable", sound=CA, ago=90, digest=TAG)     # the first copy: refused...
    b.attempted(sound=CA, ago=50)                          # ...then replayed
    _refused_copy(b)                                       # identical copy, busy behind it
    b.delivered(sound=CA, ago=40)                          # the replay played
    b.ticks(5)
    assert b.originated == [], b.originated
    assert _skipped(b).count((CB, "content-delivered")) == 1, _skipped(b)
    row = [r for r in b.rows() if r["outcome"] == "announce-retry-skipped"][0]
    assert row["matched"] == CA and row.get("delivered_at"), row


def test_the_filter_mangled_delivery_still_counts_as_heard(tmp_path):
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=LIVE_QUEUED, ago=90, digest=TAG)
    _refused_copy(b)
    b.delivered(sound=LIVE_ARRIVED, ago=40)
    b.ticks(3)
    assert b.originated == []


def test_an_identical_copy_whose_first_never_played_IS_replayed(tmp_path):
    """The first copy rang out: the words never reached the room, so the rule
    that an unplayed announcement is retried wins."""
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=90, digest=TAG)
    _refused_copy(b)
    b.ticks(3)
    assert [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [CB]


def test_something_different_heard_since_means_it_is_said_again(tmp_path):
    """Door open (heard), door closed (heard), door open (refused): the room was
    last told something else, so the third is replayed."""
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=140, digest=TAG)
    b.delivered(sound=CA, ago=130)
    b.row("originate-queued", sound=CC, ago=60, digest=OTHER)
    _refused_copy(b)
    b.delivered(sound=CC, ago=40)
    b.ticks(3)
    assert [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [CB], (b.originated, _skipped(b))


def test_a_different_ask_that_can_no_longer_play_is_transparent(tmp_path):
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=100, digest=TAG)
    _refused_copy(b, sound=CC, ago=97, tag=OTHER)
    _refused_copy(b, sound=CB, ago=95)
    b.delivered(sound=CA, ago=90)
    b.ticks(4)
    assert b.originated == [], (b.originated, _skipped(b))
    assert (CC, "ext-superseded") in _skipped(b) and (CB, "content-delivered") in _skipped(b)


def test_a_superseded_clip_that_then_played_still_counts_as_heard(tmp_path):
    """C (different) was mid-replay when B superseded it, and then C's audio
    arrived: the room last heard C, so B must still be replayed."""
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=140, digest=TAG)
    b.delivered(sound=CA, ago=135)
    b.row("unreachable", sound=CC, ago=100, digest=OTHER)
    b.attempted(sound=CC, ago=60)
    _refused_copy(b, ago=55)
    b.row("announce-retry-skipped", sound=CC, ago=30, reason="ext-superseded")
    b.delivered(sound=CC, ago=25)
    b.ticks(3)
    assert [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [CB], (b.originated, _skipped(b))


def test_the_same_content_in_another_room_is_not_a_duplicate(tmp_path):
    b = _Bench(tmp_path, exts=("19", "16"))
    a16 = "ann-16-" + "1" * 32
    b.row("originate-queued", sound=a16, ext="16", ago=90, digest=TAG)
    b.delivered(sound=a16, ext="16", ago=80)
    _refused_copy(b)
    b.ticks(3)
    assert [e for e, _s in b.originated] == ["19"]


def test_the_window_bounds_it_both_ways(tmp_path):
    W = 300
    for gap, replay in ((W + 2, True), (W - 2, False)):
        b = _Bench(tmp_path / str(gap))
        b.sched._STARTED = NOW - 2000
        b.row("originate-queued", sound=CA, ago=45 + gap + 10, digest=TAG)
        b.delivered(sound=CA, ago=45 + gap)
        _refused_copy(b)
        b.ticks(3)
        assert (len(b.originated) == 1) is replay, (gap, b.originated, _skipped(b))


def test_evidence_from_before_the_process_started_still_retires(tmp_path):
    """Positive evidence that the room HEARD it may only retire a replay, so it
    is not bounded by the restart boundary the candidate scan uses."""
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=NOW - (b.sched._STARTED - 20), digest=TAG)
    _refused_copy(b)
    b.delivered(sound=CA, ago=40)
    b.ticks(3)
    assert b.originated == [] and (CB, "content-delivered") in _skipped(b)


def test_a_delivery_landing_mid_decision_retires_the_replay(tmp_path):
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=60, digest=TAG)
    _refused_copy(b)
    b.tick(NOW)
    b.on_read = lambda: b.delivered(sound=CA, ago=0)       # lands during the AMI read
    b.tick(NOW + POLL)
    b.on_read = None
    b.ticks(4, first=NOW + 2 * POLL)
    assert b.originated == [] and _skipped(b).count((CB, "content-delivered")) == 1


def test_a_partial_playback_counts_as_heard(tmp_path):
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=60, digest=TAG)
    _refused_copy(b)
    cq = _sink_leg(b, CA, txcount=75, stage="playing", billsec=2)
    assert cq.DELIVERED_MIN_TXCOUNT == 50
    b.ticks(3)
    assert b.originated == []


def test_a_missing_or_malformed_tag_is_never_a_duplicate(tmp_path):
    for d in (None, "", "a" * 11, "A" * 12, "g" * 12):
        b = _Bench(tmp_path / str(d))
        b.row("originate-queued", sound=CA, ago=60, digest=d)
        _refused_copy(b, tag=d)
        b.delivered(sound=CA, ago=40)
        b.ticks(3)
        assert len(b.originated) == 1, d


def test_a_delivery_stamped_in_the_future_is_ignored(tmp_path):
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=60, digest=TAG)
    _refused_copy(b)
    b.row("audio-delivered", sound=CA, ago=-3600, stage="complete", txcount=700)
    b.ticks(3)
    assert len(b.originated) == 1


def test_a_zero_window_turns_the_content_rule_off(tmp_path):
    b = _Bench(tmp_path)
    b.delivery.ANNOUNCE_DEDUP_WINDOW_S = 0
    b.row("originate-queued", sound=CA, ago=60, digest=TAG)
    _refused_copy(b)
    b.delivered(sound=CA, ago=40)
    b.ticks(3)
    assert len(b.originated) == 1


def test_an_unreadable_ledger_defers_the_replay(tmp_path):
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=60, digest=TAG)
    _refused_copy(b)
    b.delivery.read_content_tail = lambda since: None
    b.ticks(3)
    assert b.originated == [] and "announce-retry-attempted" not in b.outcomes()
    assert any("could not read the delivery ledger" in m for m in b.sched.LOGGED)


def test_a_content_retirement_is_not_a_verdict(tmp_path):
    """content-delivered retires the REPLAY; B's own call still gets its verdict."""
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=400, digest=TAG)
    b.delivered(sound=CA, ago=390)
    b.row("originate-queued", sound=CB, ago=200, digest=TAG)
    _write_clip(b.clips, CB)
    b.row("announce-retry-skipped", sound=CB, ago=150, reason="content-delivered")
    _reconcile(b)
    assert [r["sound"] for r in b.rows() if r["outcome"] == "announce-undelivered"] == [CB]


def test_candidates_carry_their_content_tag(tmp_path):
    b = _Bench(tmp_path)
    _refused_copy(b)
    cands = b.delivery.retryable_announcements(NOW, not_before=b.sched._STARTED)
    assert [c["digest"] for c in cands] == [TAG]


def test_the_startup_line_names_the_content_rule(tmp_path):
    b = _Bench(tmp_path)
    b.sched._log_retry_bounds()
    assert any("identical content reached the room within 300s" in m for m in b.sched.LOGGED)


# --------------------------------------------------------------------------- #
# ★ WHAT THE LEDGER SAYS ABOUT SAYING IT AGAIN — delivery.content_verdict, the
# pure half, driven directly. Every earlier ask to a room is HEARD, IN FLIGHT,
# WAITING or GONE, and only "heard" or "in flight" DIFFERENT content stops the
# walk. The classification is the whole rule, so it is pinned here row by row.
# --------------------------------------------------------------------------- #
def _rec(D, outcome, sound, ago, ext="19", **extra):
    r = {"ts": "-", "ext": ext, "kind": "announce", "outcome": outcome,
         "sound": sound, "_ts": NOW - ago}
    r.update(extra)
    return r


def _verdict(D, *rows, tag=TAG, window=300.0, ext="19", own=None):
    return D.content_verdict(ext, tag, NOW - window, list(rows), now=NOW, own=own)


def test_the_walk_classifies_an_earlier_ask(tmp_path):
    D = _Bench(tmp_path).delivery
    horizon, max_age = D.ANNOUNCE_HORIZON, D.ANNOUNCE_RETRY_MAX_AGE
    heard = _rec(D, "audio-delivered", CA, 30)
    ask_a = _rec(D, "originate-queued", CA, 60, digest=TAG)
    # HEARD: the same content arrived inside the window.
    assert _verdict(D, ask_a, heard)[0] == "heard"
    # IN FLIGHT: handed over moments ago, no verdict yet — the same content is on
    # its way, so this one waits for it rather than doubling it.
    assert _verdict(D, ask_a)[0] == "pending"
    # GONE: handed over longer ago than the horizon and never heard.
    assert _verdict(D, _rec(D, "originate-queued", CA, horizon + 30, digest=TAG)) is None
    # WAITING: refused and still the retry's to send.
    assert _verdict(D, _rec(D, "skipped-busy", CA, 30, digest=TAG))[0] == "pending"
    # ...but not once it is older than the retry could ever start it.
    assert _verdict(D, _rec(D, "skipped-busy", CA, max_age + 30, digest=TAG)) is None
    # ...nor once the retry has retired it AND its last hand-off is a horizon old.
    retired = [_rec(D, "unreachable", CA, horizon + 60, digest=TAG),
               _rec(D, "announce-retry-attempted", CA, horizon + 40),
               _rec(D, "announce-retry-skipped", CA, horizon + 20, reason="too-old")]
    assert _verdict(D, *retired) is None


def test_a_retired_replay_that_is_still_playing_is_not_gone(tmp_path):
    """★ The retry may retire a clip 20 s after an attempt whose call is still
    ringing or playing. Treating that as "can no longer play" let a DIFFERENT
    message become transparent, so an identical copy of an older announcement was
    called a duplicate and the room was left on the stale message."""
    D = _Bench(tmp_path).delivery
    heard_a = [_rec(D, "originate-queued", CA, 200, digest=TAG),
               _rec(D, "audio-delivered", CA, 190)]
    playing_c = [_rec(D, "unreachable", CC, 170, digest=OTHER),
                 _rec(D, "announce-retry-attempted", CC, 20),
                 _rec(D, "announce-retry-skipped", CC, 1, reason="too-old")]
    assert _verdict(D, *heard_a, *playing_c) is None, "suppressed behind a playing clip"
    # Once that attempt is a horizon old with nothing delivered, it really is gone.
    old_c = [_rec(D, "unreachable", CC, 500, digest=OTHER),
             _rec(D, "announce-retry-attempted", CC, D.ANNOUNCE_HORIZON + 30),
             _rec(D, "announce-retry-skipped", CC, D.ANNOUNCE_HORIZON + 10, reason="too-old")]
    assert _verdict(D, *heard_a, *old_c)[0] == "heard"


def test_a_judged_different_ask_is_transparent_even_when_recent(tmp_path):
    """A verdict — undelivered or unsettled — is the reconciler saying the audio
    never arrived and never will, so it stops being a barrier at once."""
    D = _Bench(tmp_path).delivery
    heard_a = [_rec(D, "originate-queued", CA, 200, digest=TAG),
               _rec(D, "audio-delivered", CA, 190)]
    for verdict_row in ("announce-undelivered", "announce-unsettled"):
        rows = [*heard_a,
                _rec(D, "originate-queued", CC, 100, digest=OTHER),
                _rec(D, verdict_row, CC, 2)]
        assert _verdict(D, *rows)[0] == "heard", verdict_row
    # Without the verdict the same in-flight clip is still a barrier.
    assert _verdict(D, *heard_a, _rec(D, "originate-queued", CC, 100, digest=OTHER)) is None


def test_an_orphaned_different_ask_stops_being_a_barrier(tmp_path):
    """A restart leaves clips nobody will ever judge or replay (the retry and the
    reconciler both ignore what predates them). Past the horizon and the retry's
    age cap they cannot be heard, so they must not block a duplicate forever."""
    D = _Bench(tmp_path).delivery
    rows = [_rec(D, "originate-queued", CA, 200, digest=TAG),
            _rec(D, "audio-delivered", CA, 190),
            _rec(D, "unreachable", CC, 400, digest=OTHER)]     # orphaned, never judged
    assert _verdict(D, *rows)[0] == "heard"


def test_a_different_ask_still_waiting_for_the_retry_is_transparent(tmp_path):
    """It has not been heard and this announcement's own row will supersede it,
    so it does not stop the room being told what it already heard."""
    D = _Bench(tmp_path).delivery
    rows = [_rec(D, "originate-queued", CA, 200, digest=TAG),
            _rec(D, "audio-delivered", CA, 190),
            _rec(D, "skipped-busy", CC, 30, digest=OTHER)]     # waiting, never handed over
    assert _verdict(D, *rows)[0] == "heard"


def test_a_delivery_counts_only_after_its_own_ask(tmp_path):
    D = _Bench(tmp_path).delivery
    stray = _rec(D, "audio-delivered", CA, 30)                  # no ask row at all
    assert _verdict(D, stray) is None
    assert _verdict(D, stray, _rec(D, "originate-queued", CA, 10, digest=TAG))[0] == "pending"


def test_the_candidates_own_rows_are_left_out(tmp_path):
    D = _Bench(tmp_path).delivery
    own = [_rec(D, "skipped-busy", CB, 45, digest=TAG)]
    assert _verdict(D, *own, own=CB) is None
    assert _verdict(D, *own)[0] == "pending"


def test_a_pending_copy_never_displaces_the_one_it_waits_for(tmp_path):
    """★ THE LOCKOUT THIS RULE ALMOST SHIPPED WITH. A pending duplicate is a
    RECORD, not a candidate. When it was one, every identical repeat minted a
    fresh candidate that superseded the last and reset the two clean
    observations a replay needs — so an alert re-sent every 30 s to a busy or
    unregistered handset played NOTHING, for as long as the producer kept
    repeating. The copy the room is owed keeps its place, and it is the one that
    plays."""
    b = _Bench(tmp_path)
    b.row("skipped-busy", sound=CA, ago=90, digest=TAG)       # the copy the room is owed
    _write_clip(b.clips, CA)
    b.row("duplicate-pending", sound=CB, ago=45, digest=TAG, matched=CA)
    _write_clip(b.clips, CB)
    b.ticks(3)
    assert [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [CA], (b.originated, _skipped(b))
    assert _skipped(b) == [], "a pending record was treated as an ask"


def test_a_producer_repeating_every_30s_is_still_heard(tmp_path):
    """The same thing end to end, on the live shape: the first copy is refused
    while the handset is busy, the producer repeats every 30 s, and the handset
    frees up. The room must hear it — once."""
    b = _Bench(tmp_path, state="In use")
    b.row("skipped-busy", sound=CA, ago=0, digest=TAG)
    _write_clip(b.clips, CA)
    # A replay that connects plays the clip, so callqos writes its delivery.
    def _played(ext, sound):
        b.row(b.delivery.AUDIO_DELIVERED, sound=sound.rsplit("/", 1)[-1], ago=0,
              stage="complete", txcount=700)
        return True
    b.originate = _played
    seq, repeat = [], 0
    for step in range(1, 10):                      # 20 s ticks, repeats every 30 s
        t = step * POLL
        while repeat + 30 <= t:                    # what app.py writes for a repeat
            repeat += 30
            _CLOCK[0] = NOW + repeat
            b.row("duplicate-pending", sound=f"ann-19-{repeat:032d}", ago=-repeat,
                  digest=TAG, matched=CA)
        if t >= 60:
            b.state = "Not in use"                 # the call ends
        b.tick(NOW + t)
        seq.append(len(b.originated))
    assert b.originated and [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [CA], (seq, _skipped(b))
    assert len(b.originated) == 1, "replayed more than once"


def test_a_pending_copy_is_not_replayed_after_the_first_arrives(tmp_path):
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=90, digest=TAG)
    b.row("duplicate-pending", sound=CB, ago=45, digest=TAG, matched=CA)
    _write_clip(b.clips, CB)
    b.delivered(sound=CA, ago=40)
    b.ticks(4)
    assert b.originated == [] and _skipped(b) == [], _skipped(b)


def test_with_the_retry_off_a_repeat_is_never_held_back(tmp_path):
    """"On its way" assumes something will send it. With announce_retry_attempts
    at 0 nothing will, so a repeat is the room's only chance and must not be
    answered as a duplicate."""
    D = _Bench(tmp_path).delivery
    waiting = [_rec(D, "skipped-busy", CA, 30, digest=TAG)]
    assert _verdict(D, *waiting)[0] == "pending"
    saved = D.ANNOUNCE_RETRY_MAX_ATTEMPTS
    try:
        D.ANNOUNCE_RETRY_MAX_ATTEMPTS = 0
        assert _verdict(D, *waiting) is None
        # ...but content the room HEARD is still a duplicate, retry or no retry.
        heard = [_rec(D, "originate-queued", CA, 60, digest=TAG),
                 _rec(D, "audio-delivered", CA, 30)]
        assert _verdict(D, *heard)[0] == "heard"
    finally:
        D.ANNOUNCE_RETRY_MAX_ATTEMPTS = saved


def test_a_repeat_never_retires_a_message_asked_after_the_audio(tmp_path):
    """The room heard X, was then asked for Y (still waiting), and X is repeated.
    Y is NEWER than that audio and has never played, so the repeat must not take
    its place: only the clip a duplicate refers to ranks, at its own queue time."""
    b = _Bench(tmp_path)
    b.row("originate-queued", sound=CA, ago=200, digest=TAG)
    b.delivered(sound=CA, ago=190)
    b.row("skipped-busy", sound=CC, ago=100, digest=OTHER)     # asked AFTER that audio
    _write_clip(b.clips, CC)
    b.row("duplicate-suppressed", sound=CB, ago=45, digest=TAG, basis="delivered",
          matched=CA, delivered_at=b.iso(190))
    _write_clip(b.clips, CB)
    b.ticks(3)
    assert [s.rsplit("/", 1)[-1] for _e, s in b.originated] == [CC], (b.originated, _skipped(b))
    assert _skipped(b) == [], _skipped(b)


def test_a_suppressed_duplicate_is_a_record_and_nothing_more(tmp_path):
    """It is never replayed — the room heard that content — and it retires
    nothing. The clip it refers to is already in L6 at its own queue time, which
    is what supersedes an OLDER waiting message."""
    b = _Bench(tmp_path)
    b.row("skipped-busy", sound=CC, ago=200, digest=OTHER)     # asked BEFORE the audio
    _write_clip(b.clips, CC)
    b.row("originate-queued", sound=CA, ago=150, digest=TAG)
    b.delivered(sound=CA, ago=140)
    b.row("duplicate-suppressed", sound=CB, ago=45, digest=TAG, basis="delivered",
          matched=CA, delivered_at=b.iso(140))
    _write_clip(b.clips, CB)
    b.ticks(4)
    assert b.originated == [], b.originated
    assert (CC, "ext-superseded") in _skipped(b), _skipped(b)
    assert not any(s == CB for s, _r in _skipped(b)), "the suppressed record was replayed"


def test_a_repeat_is_not_held_behind_a_copy_the_retry_has_ruled_out(tmp_path):
    """"On its way" has to agree with L6: only the room's NEWEST ask is ever
    replayed, so a repeat must not wait behind an older copy that a different
    message has already superseded — nobody is going to send that one."""
    D = _Bench(tmp_path).delivery
    x_alone = [_rec(D, "skipped-busy", CA, 30, digest=TAG)]
    assert _verdict(D, *x_alone)[0] == "pending"
    superseded = x_alone + [_rec(D, "skipped-busy", CC, 20, digest=OTHER)]
    assert _verdict(D, *superseded) is None, "held behind a superseded copy"


