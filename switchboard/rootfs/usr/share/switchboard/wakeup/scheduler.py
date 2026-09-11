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

# ext -> {"target_epoch", "hhmm", "started", "retried"} for rings we have
# dispatched but not yet reconciled. In memory on purpose: the window is ~90 s,
# and a restart inside it loses at most one reconciliation rather than requiring
# a schema change to the on-disk store.
_ringing: dict = {}

_stop = False

# When THIS process started. The announce reconciler refuses to judge anything
# queued before it: the record that would resolve an announcement is written by a
# detached switchboard-callqos that an add-on restart kills, and before an
# upgrade it may have been a build that never wrote one. v0.98.0 shipped without
# this and filed a failure against an announcement that had played perfectly,
# within ten minutes, on the upgrade boundary itself.
_STARTED = time.time()


def log(msg: str) -> None:
    print(f"[switchboard-wakeup] {msg}", flush=True)


def _sig(*_):
    global _stop
    _stop = True


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
            state = ""
            try:
                state = {e.get("name"): (e.get("state") or "")
                         for e in ami.get_endpoints()}.get(ext, "")
            except Exception as exc:  # noqa: BLE001  (AMI down -> unknown, below)
                log(f"endpoint state unavailable before re-ring for ext {ext}: {exc}")
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


def _reconcile_announcements(now: float) -> None:
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
        stale = _delivery.unresolved_announcements(now, not_before=_STARTED)
    except Exception as exc:  # noqa: BLE001  (telemetry must never kill the loop)
        log(f"could not reconcile announcements: {exc}")
        return
    for rec in stale:
        ext, sound = rec.get("ext") or "?", rec.get("sound") or "?"
        log(f"announcement {sound} to ext {ext} was queued "
            f"{int(now - rec['_ts'])}s ago and never reached the handset")
        try:
            _delivery.record(ext, "announce", _delivery.ANNOUNCE_UNDELIVERED,
                             sound=sound, queued=rec.get("ts"))
        except Exception as exc:  # noqa: BLE001
            log(f"could not record the undelivered announcement {sound}: {exc}")


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


def main() -> None:
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    log(f"scheduler started (poll {POLL}s, ring {RING}s, store {store.PATH})")
    while not _stop:
        try:
            tick()
        except Exception as exc:  # never let the loop die
            log(f"tick error: {exc}")
        # Separate try: an announcement reconciler that raised must not be able
        # to stop wake-up calls from ringing. The alarm clock is the load-bearing
        # half of this service.
        try:
            _reconcile_announcements(time.time())
        except Exception as exc:  # noqa: BLE001
            log(f"announce reconcile error: {exc}")
        for _ in range(POLL):  # short sleeps so SIGTERM is responsive
            if _stop:
                break
            time.sleep(1)
    log("scheduler stopped")


if __name__ == "__main__":
    main()
