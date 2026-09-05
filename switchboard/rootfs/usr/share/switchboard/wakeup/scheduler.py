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


def log(msg: str) -> None:
    print(f"[switchboard-wakeup] {msg}", flush=True)


def _sig(*_):
    global _stop
    _stop = True


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
        answered = False
        if _delivery is not None:
            try:
                answered = _delivery.outcomes_since(ext, "wakeup", "answered", r["started"])
            except Exception as exc:  # noqa: BLE001  (never let telemetry break the alarm)
                log(f"could not read delivery outcomes for ext {ext}: {exc}")
                _ringing.pop(ext, None)                 # unknowable -> stop tracking, do not guess
                continue
        if answered:
            log(f"wake-up for ext {ext} ({r['hhmm']}) ANSWERED")
            _ringing.pop(ext, None)
            continue
        if not r["retried"]:
            log(f"wake-up for ext {ext} ({r['hhmm']}) went unanswered — ringing again")
            _record(ext, "no-answer", hhmm=r["hhmm"], attempt=1)
            try:
                if ami.originate_wakeup(ext, RING):
                    r["retried"] = True
                    r["started"] = now
                    _record(ext, "ring-requeued", hhmm=r["hhmm"], attempt=2)
                    continue
            except Exception as exc:  # noqa: BLE001
                log(f"re-ring for ext {ext} failed: {exc}")
            _ringing.pop(ext, None)
            continue
        # Second ring also unanswered — this is a genuinely undelivered alarm.
        log(f"wake-up for ext {ext} ({r['hhmm']}) UNDELIVERED after two rings")
        _record(ext, "undelivered", hhmm=r["hhmm"], attempt=2)
        _ringing.pop(ext, None)
        msg = (f"The {r['hhmm']} wake-up call for extension {ext} was not answered. "
               f"The phone rang twice and nobody picked up.")
        pushed = False
        if ha_client is not None and PUSH_TARGET:
            try:
                # critical=True so it sounds through Do Not Disturb. An alarm
                # clock that failed is exactly the case DND should not swallow.
                pushed = ha_client.push(msg, title="Switchboard: wake-up not answered",
                                        target=PUSH_TARGET, critical=True)
            except Exception as exc:  # noqa: BLE001
                log(f"could not push the undelivered wake-up: {exc}")
        if not pushed and ha_client is not None:
            # Fall back to the drawer card rather than losing the signal entirely.
            try:
                ha_client.notify(msg, title="Switchboard: wake-up not answered",
                                 notification_id=f"switchboard_undelivered_wakeup_{ext}")
            except Exception as exc:  # noqa: BLE001
                log(f"could not post the undelivered-wake-up card: {exc}")


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
        ok = False
        try:
            ok = ami.originate_wakeup(ext, RING)
        except Exception as exc:  # AMI hiccup — leave it for the next tick
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
        elif not ok:
            _record(ext, "originate-refused", hhmm=entry.get("hhmm"))
        if ok:
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
        for _ in range(POLL):  # short sleeps so SIGTERM is responsive
            if _stop:
                break
            time.sleep(1)
    log("scheduler stopped")


if __name__ == "__main__":
    main()
