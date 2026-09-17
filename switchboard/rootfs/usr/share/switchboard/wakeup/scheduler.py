#!/usr/bin/python3
"""Wake-up scheduler — rings rooms at their set time.

A tiny long-running loop: every POLL seconds, ask the store which wake-ups are
due and originate each room into the [wakeup-deliver] dialplan (which speaks the
wake-up greeting + the time). One-shot: a wake-up is removed once its ring is
successfully queued. If the originate fails (AMI momentarily down) it's left for
the next tick and retries until its grace window passes, after which the store
reports it "missed" and drops it.
"""

from __future__ import annotations

import os
import signal
import sys
import time

sys.path.insert(0, "/usr/share/switchboard/wakeup")
sys.path.insert(0, "/usr/share/switchboard/webui")
import store  # noqa: E402
import ami  # noqa: E402

try:
    import delivery as _delivery  # noqa: E402
except Exception:  # noqa: BLE001 - the scheduler must run without it
    _delivery = None

# Where announcement clips live, and what a legal clip name is. The retry below
# is handed a clip NAME out of a ledger in group-writable /share and must decide
# whether it names a real, playable file — so the rules live once, beside the
# directory, in the module the webui uses to mint the same names.
try:
    import announce_clip  # noqa: E402
except Exception:  # noqa: BLE001 — no clip validator means no retry, never a guess
    announce_clip = None


def _record(ext: str, outcome: str, **extra) -> None:
    """Record a wake-up delivery attempt, shared shape with the announce path.

    Best-effort: an alarm clock must never fail to ring because its telemetry
    could not be written."""
    if _delivery is None:
        return
    try:
        _delivery.record(ext, "wakeup", outcome, **extra)
    except Exception as exc:  # noqa: BLE001
        log(f"could not record wake-up outcome for ext {ext}: {exc}")
try:
    import ha_client  # noqa: E402  (surface a missed wake-up as an HA notification)
except Exception:  # noqa: BLE001 — HA integration is optional; never break the loop
    ha_client = None

POLL = int(os.environ.get("WAKEUP_POLL_SECONDS", "20"))
RING = int(os.environ.get("WAKEUP_RING_SECONDS", "60"))
# v0.70.0 — how long after a ring STARTS before we decide it went unanswered.
# Must exceed RING or we would judge a call that is still ringing.
RETRY_AFTER = int(os.environ.get("WAKEUP_RETRY_SECONDS", str(RING + 30)))
# The notify service an unanswered wake-up escalates to, WITHOUT the `notify.`
# prefix. Empty disables the push and leaves only the second ring.
PUSH_TARGET = os.environ.get("WAKEUP_PUSH_TARGET", "mobile_app_iphone").strip()
# ★ 2026-09-14 — how long a ring's verdict may wait for a snooze that is still
# being DIALLED. The dial-42 AGI writes its `set`/`cancelled` row only once the
# recogniser has heard a time: about 11 s after dialling on that morning's
# evidence, and two attempts of a 7 s recording and its recognition come to about
# 75 s at worst. While the room's own phone is on a call, _reconcile_rings()
# waits this long at most for that row, then judges exactly as before. A literal,
# not an option: it covers one dial-42 call, which nothing configures.
SNOOZE_HOLD_SECONDS = 90

# ext -> {"target_epoch", "hhmm", "started", "retried", "held_since"} for rings
# we have dispatched but not yet reconciled. In memory on purpose: the window is
# ~90 s (plus SNOOZE_HOLD_SECONDS while the room is on a call), and a restart
# inside it loses at most one reconciliation rather than requiring a schema
# change to the on-disk store.
_ringing: dict = {}

_stop = False

# When THIS process started. The announce reconciler refuses to judge anything
# queued before it: the record that would resolve an announcement is written by a
# detached switchboard-callqos that an add-on restart kills, and before an
# upgrade it may have been a build that never wrote one. v0.98.0 shipped without
# this and filed a failure against an announcement that had played perfectly,
# within ten minutes, on the upgrade boundary itself.
_STARTED = time.time()

# clip_key -> {"clean": consecutive idle observations, "defers": how many ticks
# we have waited, "state": the last device state seen} for announcements the
# retry below is watching. In memory for the same reason _ringing is: the window
# is at most ANNOUNCE_RETRY_MAX_AGE, and a restart inside it MUST lose the retry
# — `not_before=_STARTED` already forbids the new process from judging that
# window, and a process that may not judge an announcement may certainly not
# replay one. Bounded by construction: pruned every tick to the clips the ledger
# still reports as candidates.
_retry_seen: dict = {}


def log(msg: str) -> None:
    print(f"[switchboard-wakeup] {msg}", flush=True)


def _sig(*_):
    global _stop
    _stop = True


# Device states meaning the room's own phone is ON A CALL, spelled the way
# webui/ami.py's device_busy() normalises them ("In use", "INUSE", "Ring+Inuse").
# RINGING is left out on purpose: a phone being rung is not a person at it.
_ON_A_CALL = frozenset({"inuse", "ringinuse", "busy", "onhold"})


def _room_state(ext: str) -> str:
    """The room's device state as AMI reports it, or "" when AMI cannot say."""
    try:
        return {e.get("name"): (e.get("state") or "")
                for e in ami.get_endpoints()}.get(ext, "")
    except Exception as exc:  # noqa: BLE001  (AMI down -> unknown)
        log(f"endpoint state unavailable for ext {ext}: {exc}")
        return ""


def _on_a_call(state: str) -> bool:
    norm = (state or "").strip().lower().replace(" ", "").replace("_", "").replace("+", "")
    return norm in _ON_A_CALL


def _escalate(ext: str, hhmm: str, detail: str, reason: str, attempt: int) -> None:
    """Record an undelivered wake-up AND tell somebody about it.

    ★ v0.100.0 — EVERY WAY A WAKE-UP CAN FAIL NOW ARRIVES HERE. Until this
    release exactly one of four did. The other three wrote a ledger row and
    stopped: a re-ring the handset was not available for, a re-ring the PBX
    refused, and an originate refused before the phone ever rang. All three are
    a missed alarm clock, and all three were silent — the row is in a file
    nobody reads at 6am.

    The give-away was in the code itself. The wording below for "the second
    attempt was not made" was UNREACHABLE: `retried` is set True only in the
    branch that also sets `rang_again` True, so the ternary that chose it could
    never take that arm. Careful phrasing had been written, reviewed and fixed
    once (v0.84.0) for a case that never got as far as being said out loud.

    Split out so the record and the notification cannot drift apart: a caller
    that records an undelivered wake-up without telling anyone is the defect,
    and now there is no way to write one.
    """
    log(f"wake-up for ext {ext} ({hhmm}) UNDELIVERED — {reason}")
    _record(ext, "undelivered", hhmm=hhmm, attempt=attempt, reason=reason)
    msg = f"The {hhmm} wake-up call for extension {ext} was not delivered. {detail}"
    pushed = False
    if ha_client is not None and PUSH_TARGET:
        try:
            # critical=True so it sounds through Do Not Disturb. An alarm clock
            # that failed is exactly the case DND should not swallow.
            pushed = ha_client.push(msg, title="Switchboard: wake-up not delivered",
                                    target=PUSH_TARGET, critical=True)
        except Exception as exc:  # noqa: BLE001
            log(f"could not push the undelivered wake-up: {exc}")
    if not pushed and ha_client is not None:
        # Fall back to the drawer card rather than losing the signal entirely.
        try:
            ha_client.notify(msg, title="Switchboard: wake-up not delivered",
                             notification_id=f"switchboard_undelivered_wakeup_{ext}")
        except Exception as exc:  # noqa: BLE001
            log(f"could not post the undelivered-wake-up card: {exc}")


def _reconcile_rings(now: float) -> None:
    """Decide what happened to every ring we dispatched but never confirmed.

    THE BUG THIS FIXES. `ami.originate_wakeup` returns True the instant AMI
    ACCEPTS the request -- not when the phone rings, and certainly not when
    anyone picks it up. The scheduler treated that as delivery: it wrote
    `ring-queued` and immediately consumed the wake-up. So a wake-up that rang
    out and one that woke somebody produced byte-identical records, and the
    entry was gone either way. Measured on this system: 2026-09-03 06:18 and
    2026-09-04 06:12 both rang ext 19 unanswered, and every ledger, sensor and
    notification path read healthy through both.

    The answer is now knowable because [wakeup-deliver] -- which the dialplan
    reaches ONLY on answer -- records `answered`. This joins the two.

    Escalation is deliberately audible-first: ring the phone a SECOND time
    before pushing. The phone is the device that failed to wake someone, and it
    is also the loudest thing in the room; a push is the fallback for when the
    handset itself is the problem.
    """
    for ext in list(_ringing):
        r = _ringing[ext]
        if now - r["started"] < RETRY_AFTER:
            continue                                    # still ringing; too early to judge
        # v0.74.0 — FAIL SAFE ON AN UNUSABLE LEDGER.
        #
        # The join below asks "was an `answered` record written?". If the ledger
        # cannot be WRITTEN in the first place, the answer is always no — for a
        # wake-up somebody picked up as much as for one that rang out. That is
        # not a missing answer, it is a missing instrument, and treating the two
        # alike escalates every successful wake-up with a critical push.
        #
        # This was live: /share/switchboard was created root-owned 0644 while the
        # AGI runs as `asterisk`, so every `answered` write failed with EACCES and
        # was swallowed. Escalating on an unreadable instrument is worse than not
        # escalating at all, so say so loudly and stop tracking.
        if _delivery is not None and not _delivery.is_writable():
            log(f"wake-up for ext {ext} ({r['hhmm']}): the delivery ledger is NOT "
                f"WRITABLE, so an answer could not have been recorded — refusing to "
                f"judge this ring. Fix the permissions on the ledger; until then a "
                f"ring-out cannot be detected.")
            _record(ext, "unjudgeable", hhmm=r["hhmm"], reason="ledger-not-writable")
            _ringing.pop(ext, None)
            continue
        # ★ THE JOIN IS ON `spoken`, NOT ON `answered`.
        #
        # `answered` is written the instant the leg is picked up, before a word
        # is played, so it proves a pickup and not a delivery. Judging on it
        # filed the single worst outcome this feature has as a success: on
        # 2026-09-02 at 06:15:21 the cordless answered, dropped one second later
        # during the Wait before the greeting, transmitted ZERO audio packets,
        # and was recorded ANSWERED and consumed. `spoken` is written only after
        # the greeting and the time have both played.
        #
        # Both are read, because the two failures are different and the person
        # woken by the escalation deserves to be told which one happened: a
        # phone that never rang through is a delivery problem, a phone that was
        # picked up in silence is an audio problem.
        answered = spoken = False
        if _delivery is not None:
            try:
                spoken = _delivery.outcomes_since(ext, "wakeup", "spoken", r["started"])
                # ★ v0.97.0 — ...OR the hangup extension measured the audio.
                #
                # `spoken` is written by the delivery AGI from the line AFTER
                # Playback(sw-wakeup-greeting), so it is unreachable for the
                # sleeper this alarm clock works best on: woken BY the greeting,
                # hangs up during it. Asterisk abandons the extension the moment
                # the channel drops, the AGI never runs, and ninety seconds later
                # this function rang the phone again and then pushed a critical
                # DND-bypassing "it did not go through" at a person who was
                # already up. Live 2026-09-08 06:12:22 — and the h-extension
                # could see 59 transmitted packets of greeting on that very leg.
                #
                # switchboard-callqos writes AUDIO_DELIVERED from that
                # measurement, using the same verdict it scores the quality
                # ledger with, so the two ledgers cannot disagree about one call.
                #
                # OR, not replace: `spoken` proves the greeting played to the
                # END, which this does not, and it is written in-band by the AGI
                # rather than by a detached process. Two instruments, and the
                # wake-up is delivered if EITHER of them says so.
                spoken = spoken or _delivery.outcomes_since(
                    ext, "wakeup", _delivery.AUDIO_DELIVERED, r["started"])
                answered = spoken or _delivery.outcomes_since(
                    ext, "wakeup", "answered", r["started"])
            except Exception as exc:  # noqa: BLE001  (never let telemetry break the alarm)
                log(f"could not read delivery outcomes for ext {ext}: {exc}")
                _ringing.pop(ext, None)                 # unknowable -> stop tracking, do not guess
                continue
        if spoken:
            log(f"wake-up for ext {ext} ({r['hhmm']}) DELIVERED")
            _ringing.pop(ext, None)
            continue
        # ★ 2026-09-14 — THE ROOM ANSWERED BY SNOOZING.
        #
        # Ext 19 dialled 42 during three of that morning's rings (12:42:13Z,
        # 13:10:20Z, 13:20:35Z) and each time spoke a later time into the
        # handset. Nothing here looked. The join above reads only the delivery
        # milestones, so this function rang the phone again after two of those
        # snoozes and sent three critical, Do-Not-Disturb-bypassing pushes saying
        # nobody had picked up — to the person who had just picked it up to say
        # when to call back.
        #
        # A set or cancel dialled on THIS room's own phone at or after this ring
        # started proves somebody is at that phone and awake. Standing down loses
        # nothing: a new time gets its own ring, re-ring and escalation, and a
        # cancel was the person's own choice. A change from the dashboard or the
        # console does NOT count — whoever made it may be setting it for a
        # sleeper — which is why the ledger records where a change came from and
        # room_changed_wakeup() returns phone rows only.
        #
        # Read AFTER `spoken`: a wake-up that was heard is DELIVERED whatever the
        # room did next. Read BEFORE both failure branches, so a snooze stands
        # down the second ring as well as the push.
        #
        # ★ FAILS TOWARD THE ALARM. An unreadable ledger, or a read that raises,
        # is "no snooze", and the ring is judged exactly as it was before this
        # existed. The join above stops tracking on a failed read; this must not,
        # because the two mistakes are not the same size — a missed snooze is one
        # unneeded push to somebody awake, a false one is silence for a sleeper.
        snooze = None
        if _delivery is not None:
            try:
                snooze = _delivery.room_changed_wakeup(ext, r["started"])
            except Exception as exc:  # noqa: BLE001
                log(f"could not read wake-up changes for ext {ext}: {exc} — "
                    f"judging the ring as usual")
                snooze = None
        if snooze is not None:
            new_hhmm = (snooze.get("hhmm")
                        if snooze.get("outcome") == _delivery.WAKEUP_SET else None)
            did = (f"set a new wake-up for {new_hhmm}" if new_hhmm
                   else "cancelled its wake-up")
            log(f"wake-up for ext {ext} ({r['hhmm']}) SNOOZED — the room's own "
                f"phone {did} while it was ringing; not ringing again, not "
                f"escalating")
            _record(ext, _delivery.WAKEUP_SNOOZED, hhmm=r["hhmm"],
                    attempt=2 if r["retried"] else 1,
                    change=snooze.get("outcome"), new_hhmm=new_hhmm)
            _ringing.pop(ext, None)
            continue
        # ★ ...AND A SNOOZE STILL BEING DIALLED (review of the fix above).
        #
        # The row that check reads lands when the dial-42 AGI has heard a time,
        # about 11 s after the room dialled (12:42:13 -> 12:42:25Z, 13:10:20 ->
        # 13:10:31Z and 13:20:35 -> 13:20:46Z that morning). A judging tick
        # inside that call found no row and went straight on: the room read
        # "In use", so the re-ring was skipped and a critical push said nobody
        # had picked up — seconds before the set landed. On a wired phone that is
        # the natural order: the ring times out, somebody lifts the handset and
        # dials 42, and the verdict falls due in the middle of the call.
        #
        # So while the room's own phone is ON A CALL the verdict waits, and every
        # tick reads the ledger again above. BOUNDED: a room still on a call
        # after SNOOZE_HOLD_SECONDS is judged exactly as it was before this
        # existed, so a handset that is genuinely busy delays the alert by that
        # much and no more. Never on an unknown state — an AMI that cannot answer
        # is not evidence that anybody is at the phone.
        state = _room_state(ext)
        if _on_a_call(state):
            if "held_since" not in r:
                r["held_since"] = now
                log(f"wake-up for ext {ext} ({r['hhmm']}): the room is "
                    f"'{state}' — holding the verdict up to "
                    f"{SNOOZE_HOLD_SECONDS}s for a change from its phone")
            if now - r["held_since"] < SNOOZE_HOLD_SECONDS:
                continue
            log(f"wake-up for ext {ext} ({r['hhmm']}): still '{state}' after "
                f"{SNOOZE_HOLD_SECONDS}s with no change from its phone — judging")
        # Everything below is an undelivered wake-up. `answered` now only
        # changes what we CALL it and what the escalation says.
        how = ("was answered but played nothing" if answered
               else "went unanswered")
        if not r["retried"]:
            log(f"wake-up for ext {ext} ({r['hhmm']}) {how} — ringing again")
            _record(ext, "answered-silent" if answered else "no-answer",
                    hhmm=r["hhmm"], attempt=1)
            # ★ THE RE-RING GETS THE SAME GATE THE FIRST RING GETS.
            #
            # The initial fire defers unless the room reads "Not in use"
            # (see tick()), precisely because an Async Originate reports
            # "queued" the instant AMI accepts it and says nothing about
            # whether a channel was ever created. This path had no such check:
            # it re-ran the identical unguarded originate ninety seconds later.
            #
            # That is not hypothetical. Five ERRORs of the form
            #   Endpoint '19': Could not create dialog to invalid URI '19'
            # are in the durable log, four of them for the cordless and three
            # inside an add-on restart window — an originate that returned True
            # to a caller that had no way to learn it had failed.
            #
            # `state` is the reading taken above for the hold, on this same tick;
            # "" when AMI could not say, which is not "Not in use".
            # ★ Written onto `r`, never a local. v0.84.0 set a local here and
            # stored it AFTER the if/else — but the success branch `continue`s,
            # so the store was unreachable on the one path that needed it. The
            # flag was therefore False on every re-ring that actually went out,
            # and the escalation this release added to stop the alert
            # over-claiming ended up under-claiming instead: it told the owner
            # "the second attempt was not made" about a phone that rang twice.
            r["rang_again"] = False
            if state.strip().lower() != "not in use":
                # Deliberately NOT a deferral. The wake-up is already late and
                # this is its last chance; recording that the second ring never
                # went out is what keeps the escalation from claiming it did.
                log(f"re-ring for ext {ext} SKIPPED — room '{state or 'unknown'}'")
                _record(ext, "re-ring-skipped", hhmm=r["hhmm"], attempt=2,
                        device_state=state or "unknown")
                # ★ ...AND SAY SO. This path recorded the skip and stopped. The
                # alarm did not go off, the second attempt was never made, and
                # the owner was told nothing — the exact sentence below had been
                # written for this case and was unreachable from it.
                _escalate(ext, r["hhmm"],
                          "The phone was rung once and nobody picked up; the "
                          "second attempt was not made because the handset was "
                          "not available.",
                          reason="re-ring-skipped", attempt=2)
            else:
                refused = ""
                try:
                    if ami.originate_wakeup(ext, RING):
                        r["retried"] = True
                        r["started"] = now
                        r["rang_again"] = True
                        # The second ring's verdict gets a hold of its own, not
                        # whatever the first one's left behind.
                        r.pop("held_since", None)
                        _record(ext, "ring-requeued", hhmm=r["hhmm"], attempt=2)
                        continue
                    refused = "the phone system refused it"
                except Exception as exc:  # noqa: BLE001
                    log(f"re-ring for ext {ext} failed: {exc}")
                    refused = "the phone system could not be reached"
                # The handset WAS available and the second ring still did not go
                # out. Same verdict, different cause, and neither was reported.
                _record(ext, "re-ring-failed", hhmm=r["hhmm"], attempt=2,
                        detail=refused)
                _escalate(ext, r["hhmm"],
                          f"The phone was rung once and nobody picked up; the "
                          f"second attempt was not made because {refused}.",
                          reason="re-ring-failed", attempt=2)
            _ringing.pop(ext, None)
            continue
        # Second ring also failed — this is a genuinely undelivered alarm.
        _escalate(ext, r["hhmm"],
                  # ★ SAY ONLY WHAT IS KNOWN. This used to assert "The phone rang
                  # twice and nobody picked up" on every undelivered wake-up — a
                  # claim the scheduler cannot support. An Async Originate returns
                  # success the moment AMI accepts it; if Asterisk then fails to
                  # create the dialog (five such ERRORs are in the durable log) the
                  # phone never rang at all, and the person woken by this push at
                  # 6am was being told something false about their own house.
                  "The phone was picked up but played no audio." if answered
                  else "The phone was rung twice and nobody picked up.",
                  reason="answered-silent" if answered else "no-answer",
                  attempt=2)
        _ringing.pop(ext, None)


def _reconcile_announcements(now: float, recs: list | None = None) -> None:
    """File a terminal outcome for announcements that never arrived.

    ★ THE ANNOUNCE PATH HAD NO ENDING. app.py records `originate-queued` the
    instant AMI accepts the Originate — all it can know then — and records six
    distinct REFUSALS. It has never had a way to record that an accepted
    announcement was answered, or that it was not. So an announcement that rings
    a handset nobody picks up runs no dialplan at all: no `h` extension, no QoS
    record, no ledger row of any kind. Live on 2026-09-01 at 19:05:15 — `Called
    19`, `is ringing`, AMI hung it up four seconds later, and the announcement is
    absent from BOTH ledgers. Absence in a delivery ledger reads as "we never
    tried", which is the one thing that was not true.

    This lives in the wake-up scheduler because it is the only loop in the add-on
    whose job is already "decide what happened to a delivery we dispatched". The
    webui is request-driven and has no timer; giving it one to watch its own past
    requests would be a second such loop for no gain.

    Deliberately does NOT notify. An alarm clock is the one playback path with a
    deadline (see PLAYBACK_TAGS in switchboard-callqos); an announcement that did
    not arrive is worth a durable record, not a push at whatever hour it was.
    """
    if _delivery is None:
        return
    # ★ SAME FAIL-SAFE AS THE RING RECONCILER, and it matters MORE here. The join
    # asks "was a delivery record written?". If the ledger cannot be written at
    # all, the answer is no for every announcement — including every one that
    # played perfectly — so an unwritable ledger would turn this into a machine
    # for manufacturing failure records about a system that is fine. That exact
    # permission fault was live for two releases.
    try:
        if not _delivery.is_writable():
            return
        stale = _delivery.unresolved_announcements(now, not_before=_STARTED,
                                                  recs=recs)
    except Exception as exc:  # noqa: BLE001  (telemetry must never kill the loop)
        log(f"could not reconcile announcements: {exc}")
        return
    settle_until = _STARTED + _delivery.ANNOUNCE_SETTLE_SECONDS
    for rec in stale:
        ext, sound = rec.get("ext") or "?", rec.get("sound") or "?"
        # ★ THE RESTART WINDOW IS NOT A FAILURE, AND IT IS NOT A SUCCESS EITHER.
        #
        # Asterisk restarts with the add-on and its endpoints take up to two
        # minutes to re-register. An announcement originated into that gap really
        # does not arrive — so `announce-undelivered` is TRUE, and useless: the
        # cause is a restart the operator already performed, and nothing about it
        # is actionable. Live 2026-09-11, an announcement queued 18 s after a
        # restart failed with `Could not create dialog to invalid URI '19'` while
        # the same handset was unreachable in 2 of 1,714 steady-state polls.
        #
        # `not_before=_STARTED` does not cover this: that guard asks whether THIS
        # PROCESS was running, and it was — it is Asterisk that had not finished
        # coming back. A second condition, not a longer horizon.
        #
        # Recorded rather than skipped, because a queued row with no verdict at
        # all is indistinguishable from one the reconciler forgot. This says
        # plainly that it was not judged, and why.
        if rec["_ts"] < settle_until:
            # Report the distance from the START of the window, not from its end.
            # The first live firing of this branch logged "queued 117s inside the
            # settling window" for an announcement queued 3 s after start — the
            # number was the remaining window, which reads as its opposite.
            log(f"announcement {sound} to ext {ext} was queued "
                f"{int(rec['_ts'] - _STARTED)}s after start, inside the "
                f"{int(_delivery.ANNOUNCE_SETTLE_SECONDS)}s post-restart settling "
                f"window — not judged")
            try:
                _delivery.record(ext, "announce", _delivery.ANNOUNCE_UNSETTLED,
                                 sound=sound, queued=rec.get("ts"),
                                 reason="pbx-restarting",
                                 # How hard the system tried before filing this.
                                 # Omitted when it never retried (record() drops
                                 # None extras), so a row about an announcement
                                 # nobody could retry is the row it always was.
                                 retries=rec.get("retries"))
            except Exception as exc:  # noqa: BLE001
                log(f"could not record the unsettled announcement {sound}: {exc}")
            continue
        log(f"announcement {sound} to ext {ext} was queued "
            f"{int(now - rec['_ts'])}s ago and never reached the handset")
        try:
            _delivery.record(ext, "announce", _delivery.ANNOUNCE_UNDELIVERED,
                             sound=sound, queued=rec.get("ts"),
                             retries=rec.get("retries"))
        except Exception as exc:  # noqa: BLE001
            log(f"could not record the undelivered announcement {sound}: {exc}")


def _announce_records(now: float):
    """The ledger tail this tick's two announce passes SHARE, or None.

    One read per tick, for two reasons. _read_records() does a full readlines()
    of a ledger capped at 2 MB, and this loop's real job is ringing alarm clocks
    on a Pi; and two reads would let the reconciler and the retry see two
    different ledgers when a row lands between them — the retry deciding a clip
    is unresolved from an older view than the one the verdict was filed from.
    None means "could not read", which both passes treat as a deferral.
    """
    if _delivery is None:
        return None
    try:
        return _delivery.announce_records(now, not_before=_STARTED)
    except Exception as exc:  # noqa: BLE001  (telemetry must never kill the loop)
        log(f"could not read the delivery ledger: {exc}")
        return None


def _retry_max_attempts() -> int:
    return int(getattr(_delivery, "ANNOUNCE_RETRY_MAX_ATTEMPTS", 0) or 0)


def _retire_announcement(cand: dict, reason: str, seen: dict | None,
                         now: float) -> None:
    """File the ONE terminal retry row for an announcement past helping.

    Only for a reason that cannot change: too old, out of attempts, superseded by
    a newer announcement to the same room, or a clip that is no longer on disk. A
    TRANSIENT deferral — the handset is not idle yet, AMI could not be read, one
    clean observation so far — is logged and NOT recorded: a row every 20 s would
    trim away the history this ledger exists to keep.

    Not a verdict on the announcement. ANNOUNCE_RETRY_SKIPPED is deliberately
    outside ANNOUNCE_TERMINAL, so the reconciler still speaks its
    announce-undelivered / announce-unsettled for the same clip afterwards. This
    row says only what the RETRY did, and it is what excludes the clip from every
    later candidate scan without any in-memory bookkeeping.
    """
    seen = seen or {}
    log(f"announcement {cand['sound']} to ext {cand['ext']} will not be retried "
        f"({reason}); {cand['attempts']} attempt(s), "
        f"{int(now - cand['queued_ts'])}s old")
    try:
        _delivery.record(cand["ext"], "announce", _delivery.ANNOUNCE_RETRY_SKIPPED,
                         sound=cand["sound"], reason=reason,
                         attempts=cand["attempts"], queued=cand.get("queued"),
                         age_s=int(now - cand["queued_ts"]),
                         device_state=seen.get("state") or None,
                         defers=seen.get("defers") or None)
    except Exception as exc:  # noqa: BLE001
        log(f"could not record the skipped retry of {cand['sound']}: {exc}")


def _retry_announcements(now: float, recs: list | None = None) -> None:
    """Replay an announcement whose audio never played, while that still helps.

    ★ THE OWNER'S DECISION (2026-09-15), after the third occurrence of one shape:
    an announcement was queued to the cordless 8.4 s after an add-on restart,
    before that handset had re-registered; Asterisk logged `Could not create
    dialog to invalid URI '19'`, no channel existed, no audio played, and the only
    thing anybody was told was a ledger row three minutes later. v0.104.0 shipped
    the RECORD half of that. This is the other half: the announcement is sent
    again once the handset can actually take it.

    ★ WHY HERE and not in the webui, which is where the announcement came from.
    This loop already ticks every POLL seconds, already reads the ledger with
    `not_before=_STARTED`, already owns the settling window, already reads
    endpoint states and already originates. Decisively, it SURVIVES the restart
    that causes this defect: a retry held in the webui's own process would be
    killed by the next deploy, and the evidence for this one is a deploy storm —
    four releases in 29 minutes, plus a restart eight minutes later.

    ★ WHAT MAKES A REPLAY SAFE, in order, because the ORDER is the guarantee:
      1. the ledger, alone: a clip with any ANNOUNCE_TERMINAL row — above all the
         `audio-delivered` row callqos writes with the SAME clip name — is never a
         candidate, at any age, in any order (delivery.retryable_announcements);
      2. the clip, before any AMI traffic: a name out of group-writable /share
         must still resolve to a real, contained, non-empty, short-enough file;
      3. the handset, POSITIVELY: `ami.device_idle` — the single state "not in
         use" — twice, one POLL apart. Ringing or in-use is never idle, so a
         replay cannot land on top of audio in progress, and an unreadable state
         is a deferral rather than a green light. The fail-OPEN pre-flight that
         caused the incident is exactly what must not be reused here;
      4. the ledger again, narrowly, AFTER the state read, so a delivered row
         landing mid-decision can only appear, never be missed;
      5. the attempt row BEFORE the Originate, and the Originate only if that row
         was actually written — an attempt that cannot be counted cannot be
         bounded.

    Deliberately silent: like the reconciler it writes ledger rows and journal
    lines and notifies nobody. An announcement is not an alarm clock.
    """
    if _delivery is None:
        return
    if announce_clip is None:
        return
    # SAME FAIL-SAFE AS THE RECONCILER. A ledger that cannot be written cannot
    # hold the attempt row that bounds the retry, nor the delivered row that
    # proves a replay arrived — so its silence proves nothing and authorises
    # nothing.
    try:
        if not _delivery.is_writable():
            return
        cands = _delivery.retryable_announcements(now, not_before=_STARTED, recs=recs)
    except Exception as exc:  # noqa: BLE001
        log(f"could not look for retryable announcements: {exc}")
        return
    max_attempts = _retry_max_attempts()
    clean_ticks = int(getattr(_delivery, "ANNOUNCE_RETRY_CLEAN_TICKS", 2) or 2)

    # PHASE 1+2 — the ledger, then the clip. Neither costs AMI traffic, so a clip
    # ages out correctly even while AMI is down, and a forged row is refused for
    # free.
    live, watched = [], set()
    for cand in cands:
        key = _delivery.clip_key(cand["sound"])
        if cand.get("stale_reason"):
            _retire_announcement(cand, cand["stale_reason"], _retry_seen.pop(key, None), now)
            continue
        clip = announce_clip.retry_clip_path(cand["ext"], cand["sound"])
        if not clip:
            _retire_announcement(cand, "clip-gone", _retry_seen.pop(key, None), now)
            continue
        live.append((cand, key, clip))
        watched.add(key)
    # Nothing to watch is nothing to remember: a clip that left the candidate set
    # was either answered or retired ON DISK, so this dict cannot grow.
    for key in [k for k in _retry_seen if k not in watched]:
        _retry_seen.pop(key, None)
    if not live:
        return

    # PHASE 3 — ONE endpoint read for the whole tick, and only now that a
    # candidate has survived the two free phases. At the measured rate (3 in 4.5
    # days) a quiet tick adds no AMI traffic at all.
    try:
        eps = {e.get("name"): e for e in ami.get_endpoints()}
    except Exception as exc:  # noqa: BLE001
        log(f"endpoint states unavailable ({exc}); deferring "
            f"{len(live)} announcement retr{'y' if len(live) == 1 else 'ies'}")
        for _cand, key, _clip in live:
            seen = _retry_seen.setdefault(key, {"clean": 0, "defers": 0, "state": ""})
            seen["clean"] = 0            # an unreadable state is not an observation
            seen["defers"] += 1
        return

    for cand, key, clip in live:
        seen = _retry_seen.setdefault(key, {"clean": 0, "defers": 0, "state": ""})
        ep = eps.get(cand["ext"])
        state = str((ep or {}).get("state") or "")
        seen["state"] = state
        # ActiveChannels from the same EndpointList event, SECONDARY and
        # fail-closed. What this field holds while a clip is playing is not
        # established by anything in this repo (the captured fixture has it empty
        # for an idle endpoint and no in-use endpoint at all), so it can only ever
        # make the retry more conservative, never less — and if a live probe shows
        # it empty during a playback it should be deleted rather than kept as a
        # check that cannot discriminate.
        chans = str((ep or {}).get("channels", "") or "").strip()
        if ep is None or not ami.device_idle(state) or chans not in ("", "0"):
            seen["clean"] = 0
            seen["defers"] += 1
            log(f"announcement {cand['sound']} to ext {cand['ext']} is waiting for "
                f"the handset — state '{state or 'unknown'}', channels "
                f"'{chans}', {int(now - cand['queued_ts'])}s old")
            continue
        seen["clean"] += 1
        if seen["clean"] < clean_ticks:
            log(f"announcement {cand['sound']} to ext {cand['ext']}: ext idle "
                f"({seen['clean']} of {clean_ticks} clean observations)")
            continue

        # PHASE 4 — the last look, then the row, then the call. This order is the
        # duplicate guarantee: the ledger is consulted AFTER the state read, so a
        # delivered row that lands while we were asking AMI can only appear.
        try:
            if _delivery.announce_clip_resolved(cand["sound"], now=now,
                                                not_before=_STARTED):
                log(f"announcement {cand['sound']} was settled while the retry was "
                    f"deciding — not replaying it")
                _retry_seen.pop(key, None)
                continue
        except Exception as exc:  # noqa: BLE001
            log(f"could not re-check {cand['sound']} before replaying it: {exc}")
            continue
        attempt = cand["attempts"] + 1
        try:
            wrote = _delivery.record(
                cand["ext"], "announce", _delivery.ANNOUNCE_RETRY_ATTEMPTED,
                sound=cand["sound"], attempt=attempt, of=max_attempts,
                queued=cand.get("queued"), device_state=state or None,
                age_s=int(now - cand["queued_ts"]))
        except Exception as exc:  # noqa: BLE001
            log(f"could not record the retry of {cand['sound']}: {exc}")
            continue
        if not wrote:
            # ★ TELEMETRY GATES THE ACTION, uniquely here. Everywhere else in
            # this file a failed record must never stop the delivery it describes.
            # The budget IS these rows, so an unwritten one is an unbounded retry.
            log(f"the retry of {cand['sound']} was NOT recorded — not replaying "
                f"it: an attempt that cannot be counted cannot be bounded")
            continue
        seen["clean"] = 0        # this attempt must be observed out before another
        try:
            ok = ami.announce_to_ext(cand["ext"], clip)
        except Exception as exc:  # noqa: BLE001
            log(f"retry originate of {cand['sound']} to ext {cand['ext']} "
                f"failed: {exc}")
            _record_retry_failure(cand, attempt, "retry-ami-error", str(exc)[:120])
            continue
        if ok:
            log(f"announcement {cand['sound']} re-originated to ext {cand['ext']} "
                f"(attempt {attempt} of {max_attempts}, "
                f"{int(now - cand['queued_ts'])}s after it was queued)")
        else:
            log(f"the phone system refused the retry of {cand['sound']} to ext "
                f"{cand['ext']} (attempt {attempt} of {max_attempts})")
            _record_retry_failure(cand, attempt, "retry-refused")


def _record_retry_failure(cand: dict, attempt: int, reason: str,
                          detail: str | None = None) -> None:
    """A retry Originate that raised or was declined, in the ledger.

    ANNOUNCE_ORIGINATE_FAILED reused rather than renamed: it already means exactly
    this, already carries the clip, and is already correctly outside
    ANNOUNCE_TERMINAL — so the announcement still gets its verdict. The reason
    says which side of the retry it came from.
    """
    try:
        _delivery.record(cand["ext"], "announce",
                         _delivery.ANNOUNCE_ORIGINATE_FAILED, sound=cand["sound"],
                         reason=reason, attempt=attempt, detail=detail)
    except Exception as exc:  # noqa: BLE001
        log(f"could not record the failed retry of {cand['sound']}: {exc}")


def tick() -> None:
    now = time.time()
    _reconcile_rings(now)
    fired, missed = store.due(now)
    for ext, entry in missed:
        late = int((now - entry.get("target_epoch", now)) / 60)
        hhmm = entry.get("hhmm")
        log(f"missed wake-up for ext {ext} ({hhmm}) — {late} min late; skipped")
        # A missed wake-up used to be log-only (invisible unless you tailed the
        # add-on log). Surface it in Home Assistant's notifications so the user
        # actually learns the phone never got its wake-up call.
        if ha_client is not None:
            try:
                ha_client.notify(
                    f"Extension {ext}'s {hhmm} wake-up call could not be delivered — "
                    f"the phone stayed busy or offline through its grace window "
                    f"(gave up {late} minutes late).",
                    title="Switchboard: missed wake-up",
                    notification_id=f"switchboard_missed_wakeup_{ext}",
                )
            except Exception as exc:  # noqa: BLE001
                log(f"could not post missed-wake-up notification: {exc}")
    if not fired:
        return

    # An Async Originate reports "queued" the instant it's accepted, not when the
    # phone rings — so we must NOT consume a wake-up to an offline or busy room.
    # Only fire when the room is registered AND idle ("Not in use"); otherwise
    # leave the entry for a later tick, retrying within its grace window.
    try:
        states = {ep.get("name"): (ep.get("state") or "") for ep in ami.get_endpoints()}
    except Exception as exc:  # AMI down -> treat all as not-ready, defer
        states = {}
        log(f"endpoint states unavailable ({exc}); deferring this tick")
    for ext, entry in fired:
        state = states.get(ext, "")
        if state.strip().lower() != "not in use":
            log(f"wake-up for ext {ext} ({entry.get('hhmm')}) deferred — room '{state or 'unknown'}'")
            # A deferral is a wake-up that did NOT happen at its appointed time.
            # It was previously only a line in an untimestamped container log.
            _record(ext, "deferred", hhmm=entry.get("hhmm"),
                    device_state=state or "unknown")
            continue
        # ★ THREE OUTCOMES, NOT TWO. `ok` alone cannot tell "AMI answered and
        # refused" from "AMI was unreachable", and the difference decides both
        # what is recorded and whether the wake-up stays due. Before v0.99.0 an
        # AMI hiccup recorded `originate-error` AND then fell into `elif not ok`
        # and recorded `originate-refused` for the same attempt — two rows
        # claiming different causes for one event.
        ok = False
        errored = False
        try:
            ok = ami.originate_wakeup(ext, RING)
        except Exception as exc:  # AMI hiccup — leave it for the next tick
            errored = True
            log(f"originate wake-up for ext {ext} failed: {exc}")
            _record(ext, "originate-error", hhmm=entry.get("hhmm"),
                    detail=str(exc)[:120])
        log(f"wake-up for ext {ext} ({entry.get('hhmm')}): ring queued={ok}")
        # Record the ATTEMPT, not just the log line. The QoS ledger is written
        # from the dialplan's hangup extension, so it can only ever describe a
        # leg that ANSWERED -- a wake-up that rings out leaves nothing there at
        # all. The 06:18 wake-up on 2026-09-03 rang ext 19 and produced no
        # record of any kind: "ring queued=True", "Called 19", "is ringing",
        # then silence for 98 minutes. Nothing distinguished "the phone never
        # rang" from "the user ignored it", on an alarm clock.
        #
        # A ring-queued record with no matching call record IS the no-answer
        # signal, and both now live in /share where they can be read together.
        if ok:
            _record(ext, "ring-queued", hhmm=entry.get("hhmm"),
                    ring_seconds=RING)
            # Track it for reconciliation. The store entry is still consumed
            # below (so the next 20 s tick cannot re-fire it into a ring storm);
            # this is what remembers that the ring is unresolved.
            _ringing[ext] = {"target_epoch": entry.get("target_epoch"),
                             "hhmm": entry.get("hhmm"), "started": now,
                             "retried": False}
        elif not errored:
            _record(ext, "originate-refused", hhmm=entry.get("hhmm"))
            # ★ The phone never rang at all, and until v0.100.0 this was the
            # quietest failure of the four: one ledger row and nothing else.
            # Safe to escalate only because the entry is now consumed below —
            # before that fix this path re-fired every 20 s, and a push on each
            # would have been a notification storm rather than an alarm.
            _escalate(ext, entry.get("hhmm") or "?",
                      "The phone system refused to place the call, so the "
                      "phone never rang.",
                      reason="originate-refused", attempt=1)
        # ★ v0.99.0 — A REFUSED WAKE-UP IS ALSO CONSUMED. The comment above says
        # the store entry is consumed "so the next 20 s tick cannot re-fire it
        # into a ring storm", and the consumption sat inside `if ok:` — so on the
        # one path where the ring demonstrably did NOT go out, it did exactly
        # what it says it prevents. A wake-up AMI refuses stays due forever and
        # every tick writes another `originate-refused` row: ~4,320 a day against
        # a 2 MB ledger that trims its OLDEST records at the cap. That does not
        # merely add noise, it deletes the wake-up and announcement history the
        # ledger exists to keep.
        #
        # An AMI hiccup is the exception, and keeps the old behaviour: the ring
        # may genuinely never have been attempted, so the entry stays due and the
        # next tick tries again. That is why `errored` is tracked separately —
        # `not ok` alone cannot tell the two apart.
        if not errored:
            try:
                store.cancel_if(ext, entry.get("target_epoch"))  # one-shot; don't clobber a re-set one
            except Exception as exc:
                log(f"could not clear wake-up for ext {ext}: {exc}")


def _log_retry_bounds() -> None:
    """Print the retry's bounds at startup, and WARN when they cannot fire.

    ★ A BOUND THAT CANNOT FIRE IS A FEATURE THAT LOOKS SHIPPED AND IS NOT. The
    first attempt cannot happen before ANNOUNCE_RETRY_MIN_AGE plus one POLL per
    required clean observation; if that already exceeds ANNOUNCE_RETRY_MAX_AGE,
    every announcement is retired as `too-old` before it can ever be replayed —
    and every test stays green, because each number is individually sane. Pinned
    by test as well (invariant I3); this is the half a reader sees in the journal.
    """
    if _delivery is None:
        log("announce retry is OFF (the delivery ledger module is unavailable)")
        return
    if announce_clip is None:
        # Said out loud rather than left as a silent no-op: a feature that is
        # inert because an import failed looks exactly like one that is working
        # and has nothing to do.
        log("announce retry is OFF (the clip validator module is unavailable)")
        return
    attempts = _retry_max_attempts()
    if attempts <= 0:
        log("announce retry is OFF (ANNOUNCE_RETRY_MAX_ATTEMPTS=0)")
        return
    min_age = float(getattr(_delivery, "ANNOUNCE_RETRY_MIN_AGE", 0.0))
    clean = int(getattr(_delivery, "ANNOUNCE_RETRY_CLEAN_TICKS", 0) or 0)
    max_age = float(getattr(_delivery, "ANNOUNCE_RETRY_MAX_AGE", 0.0))
    log(f"announce retry: up to {attempts} attempt(s), no sooner than "
        f"{int(min_age)}s after the originate, {clean} clean endpoint "
        f"observation(s) required, never started later than {int(max_age)}s")
    if min_age + clean * POLL >= max_age:
        log(f"WARNING: announce retry is INERT at poll {POLL}s — "
            f"{int(min_age)}s + {clean} x {POLL}s >= {int(max_age)}s, so no "
            f"attempt can be reached before the age cap retires the clip")


def main() -> None:
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    log(f"scheduler started (poll {POLL}s, ring {RING}s, store {store.PATH})")
    _log_retry_bounds()
    while not _stop:
        try:
            tick()
        except Exception as exc:  # never let the loop die
            log(f"tick error: {exc}")
        # ONE ledger read, shared by the two announce passes below. Also one
        # `now`: the retry and the reconciler judge the same clips from opposite
        # ends of the same timeline, and two clocks a read apart is how a clip
        # falls between them.
        announce_now = time.time()
        announce_recs = _announce_records(announce_now)
        # Separate try: an announcement reconciler that raised must not be able
        # to stop wake-up calls from ringing. The alarm clock is the load-bearing
        # half of this service.
        try:
            _reconcile_announcements(announce_now, announce_recs)
        except Exception as exc:  # noqa: BLE001
            log(f"announce reconcile error: {exc}")
        # ...and a THIRD try, for the same reason again one level down: the retry
        # is the newest and the only one of the three that WRITES on the announce
        # path. A fault in it must not stop verdicts being filed, and neither may
        # stop the alarm clock ringing.
        try:
            _retry_announcements(announce_now, announce_recs)
        except Exception as exc:  # noqa: BLE001
            log(f"announce retry error: {exc}")
        for _ in range(POLL):  # short sleeps so SIGTERM is responsive
            if _stop:
                break
            time.sleep(1)
    log("scheduler stopped")


if __name__ == "__main__":
    main()
