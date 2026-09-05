"""Records for delivery attempts that never became a call.

The QoS ledger is written from the dialplan's hangup extension, so it can only
ever describe legs that ANSWERED. Everything upstream is invisible to it: an
Originate refused for want of a contact, a handset that rang and was never
picked up, an AMI error.

That gap is not theoretical. The 06:18 wake-up on 2026-09-03 rang ext 19 and
produced no record of any kind -- `ring queued=True`, `Called 19`, `is ringing`,
and then nothing at all for 98 minutes. No `h` extension ran, so no rtpqos, so no
callqos row, so no sensor. Nothing distinguished "the phone never rang" from
"the user ignored it", on an alarm clock.

Records land under /share because /data cannot be read from outside the
container, and this file exists to be read.
"""
from __future__ import annotations

import datetime
import json
import os
import stat

OUTCOME_PATH = os.environ.get("SWITCHBOARD_DELIVERY_OUTCOME",
                              "/share/switchboard/delivery-outcomes.jsonl")
MAX_BYTES = 2 * 1024 * 1024


def record(ext: str, kind: str, outcome: str, **extra) -> bool:
    """Append one delivery-attempt record. Returns True iff it was WRITTEN.

    v0.74.0 — the return value exists because this function swallowing an error
    made a shipped feature inert and invisible. `/share/switchboard` was created
    root-owned 0644 while the AGI runs as `asterisk`, so the `answered` record
    v0.70.0 depends on failed with EACCES on every write — and because the caller
    could not tell, every ANSWERED wake-up looked unanswered to the reconciler,
    would have been rung a second time, and then escalated with a critical
    DND-bypassing push saying nobody had picked up.

    Still best-effort: telemetry must never fail the delivery it describes. But
    "best-effort" must not mean "indistinguishable from success"."""
    rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(
               timespec="seconds"),
           "ext": ext, "kind": kind, "outcome": outcome}
    # Absent optionals are OMITTED rather than written as null, so a reader can
    # tell "not applicable" from "measured as nothing".
    rec.update({k: v for k, v in extra.items() if v is not None})
    try:
        os.makedirs(os.path.dirname(OUTCOME_PATH), exist_ok=True)
        try:
            if os.path.getsize(OUTCOME_PATH) > MAX_BYTES:
                with open(OUTCOME_PATH, "w", encoding="utf-8"):
                    pass
        except OSError:
            pass
        with open(OUTCOME_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        # Group-writable: the scheduler (root) and the AGI (asterisk) both append
        # here, and whichever creates the file decides whether the other can.
        #
        # ADD the group-write bit rather than asserting a literal mode. Writing
        # 0o664 would also assert world-readable, which is a broader claim than
        # this needs to make — the file inherits whatever the umask and the
        # setgid directory already decided, and this only ensures the second
        # writer is not locked out.
        try:
            os.chmod(OUTCOME_PATH, os.stat(OUTCOME_PATH).st_mode | stat.S_IWGRP)
        except OSError:
            pass
        return True
    except OSError as exc:
        print(f"[switchboard-delivery] record FAILED ({exc}) — "
              f"{kind}/{outcome} for ext {ext} was NOT written", flush=True)
        return False


def outcomes_since(ext: str, kind: str, outcome: str, since_ts: float) -> bool:
    """True if `ext` has a `kind`/`outcome` record at or after `since_ts` (epoch).

    v0.70.0 — THE READER THIS FILE NEVER HAD.

    The v0.67.0 design is written down in the changelog: "a ring-queued record
    with no matching call record IS the no-answer signal, and both now live in
    /share where they can be read together." Nothing ever read them together.
    `grep -rn delivery-outcomes` found only writers, so the join was defined in
    prose and computed by nobody -- which is why the 2026-09-04 06:12 ring-out
    sat in this file, correctly recorded, and raised nothing.

    Scans from the END backwards and stops at the first record older than
    `since_ts`: the file is append-only and the caller always asks about the last
    couple of minutes, so this touches a handful of lines rather than the whole
    2 MB cap. A malformed line is skipped, never fatal -- this is consulted on
    the alarm-clock path and must not raise there.
    """
    try:
        with open(OUTCOME_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
            ts = datetime.datetime.fromisoformat(rec["ts"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if ts < since_ts:
            break          # append-only: everything earlier is older still
        if rec.get("ext") == ext and rec.get("kind") == kind and rec.get("outcome") == outcome:
            return True
    return False


def is_writable() -> bool:
    """Can this process actually append to the ledger?

    The reconciler MUST distinguish "no answer was recorded" from "the ledger
    cannot be written, so no answer could have been recorded". Treating the
    second as the first escalates every successful wake-up.
    """
    try:
        d = os.path.dirname(OUTCOME_PATH)
        os.makedirs(d, exist_ok=True)
        if os.path.exists(OUTCOME_PATH):
            return os.access(OUTCOME_PATH, os.W_OK)
        return os.access(d, os.W_OK)
    except OSError:
        return False
