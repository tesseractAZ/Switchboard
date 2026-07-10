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
    import ha_client  # noqa: E402  (surface a missed wake-up as an HA notification)
except Exception:  # noqa: BLE001 — HA integration is optional; never break the loop
    ha_client = None

POLL = int(os.environ.get("WAKEUP_POLL_SECONDS", "20"))
RING = int(os.environ.get("WAKEUP_RING_SECONDS", "60"))

_stop = False


def log(msg: str) -> None:
    print(f"[switchboard-wakeup] {msg}", flush=True)


def _sig(*_):
    global _stop
    _stop = True


def tick() -> None:
    now = time.time()
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
            continue
        ok = False
        try:
            ok = ami.originate_wakeup(ext, RING)
        except Exception as exc:  # AMI hiccup — leave it for the next tick
            log(f"originate wake-up for ext {ext} failed: {exc}")
        log(f"wake-up for ext {ext} ({entry.get('hhmm')}): ring queued={ok}")
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
