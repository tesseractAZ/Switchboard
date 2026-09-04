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

OUTCOME_PATH = os.environ.get("SWITCHBOARD_DELIVERY_OUTCOME",
                              "/share/switchboard/delivery-outcomes.jsonl")
MAX_BYTES = 2 * 1024 * 1024


def record(ext: str, kind: str, outcome: str, **extra) -> None:
    """Append one delivery-attempt record. Best-effort: telemetry must never
    fail the delivery it is describing."""
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
    except OSError as exc:
        print(f"[switchboard-delivery] record: {exc}", flush=True)
