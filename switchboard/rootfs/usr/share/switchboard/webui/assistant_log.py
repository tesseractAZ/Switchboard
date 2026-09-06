"""The voice assistant's own ledger — what it heard, what it said, and why a
turn ended the way it did.

    /data/state/assistant.jsonl   (add-on-private; NOT mirrored to /share)

WHY THIS EXISTS. Before v0.80.0 the assistant recorded nothing that survived the
call. Its diagnostics went through a private ``log()`` that is a bare
``sys.stderr.write``, and AGI stderr does not reach Asterisk's logger at all — it
is inherited fd 2, so it lands raw in the RAM-backed container log and evaporates
on rotation. Verified on the running system: ``/data/state/asterisk.log`` and
``/share/switchboard/asterisk.log`` contain ZERO ``[assistant]`` lines. The
assistant's REPLY was worse off still: ``reply_text(...)`` was evaluated inline as
a call argument and never bound to anything, so the half of "what the assistant
heard and said" that says whether it answered correctly did not exist anywhere.

★ WHY NOT ``Verbose()``. The obvious way to make an AGI diagnostic durable is to
emit it through Asterisk's logger. Do not do that here. logger.conf routes
``verbose`` to ``/share/switchboard/asterisk.log`` — and ``/share`` is host-mounted
and world-readable BY DESIGN, which is the entire point of that directory. Asterisk
runs ``-vvv``, so a single ``Verbose()`` carrying a transcript would move household
speech out of an ephemeral container log and into a 32 MB durable file readable
from outside the container and captured in Supervisor backups. This module writes
to ``/data`` instead, which is unreachable from outside: container shell blocked by
protection mode, backups encrypted, add-on API 403 on every path.

For the same reason there is NO ``/share`` mirror of this ledger, unlike
callqos.jsonl. That ledger's "mirror the FULL record, not a curated subset"
decision was reasoned about field completeness for auditing and never about
disclosure. It is the wrong template for speech.

WHAT IS RECORDED. One row per turn, written AS THE TURN ENDS rather than
accumulated and flushed at the end of the call. That ordering is not stylistic:
Asterisk sends SIGHUP on caller hangup, no AGI here installs a handler, and
``AGISIGHUP`` is never set in the dialplan — so Python is terminated outright with
no ``finally`` and no ``atexit``. A ledger that buffered would lose exactly the
calls that ended badly.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

try:
    import fcntl
except ImportError:  # noqa: BLE001  (non-POSIX; the lock degrades to none)
    fcntl = None

PATH = os.environ.get("SWITCHBOARD_ASSISTANT_LOG", "/data/state/assistant.jsonl")
MAX_RECORDS = 400

# Schema version. v1 is the first. Carried because callqos learned the hard way
# that a reader who has to infer a record's shape by counting keys will get it
# wrong and then reason confidently from the wrong answer.
SCHEMA = 1

# ★ THE ONLY TWO FIELDS THAT CARRY HOUSEHOLD SPEECH. Kept as an explicit named
# set so the privacy rules below are one grep away from anyone adding a field,
# and so a test can assert no other writer in the repo accepts them.
SPEECH_FIELDS = ("heard", "reply")

# Bound on stored speech. A transcript is a command, not a monologue; a runaway
# STT result should not be able to grow the ledger without limit.
MAX_SPEECH_CHARS = 300


def _redact(rec: dict, transcripts: bool) -> dict:
    """Apply the transcript policy to one record.

    With transcripts OFF the row still carries every timing, outcome and reason —
    everything needed to answer "is the assistant working?" — and drops only the
    words themselves, replaced by their length so a reader can still tell a
    one-word reply from a paragraph. Turning transcripts off must degrade the
    ledger's PRIVACY, not its DIAGNOSTIC value; a switch that blinded the
    operator would just get left on.
    """
    out = dict(rec)
    for f in SPEECH_FIELDS:
        if f not in out:
            continue
        text = out[f]
        if not isinstance(text, str):
            out.pop(f, None)
            continue
        if transcripts:
            out[f] = text[:MAX_SPEECH_CHARS]
        else:
            out.pop(f)
            out[f + "_chars"] = len(text)
    return out


def record(outcome: str, *, transcripts: bool = True, **fields) -> bool:
    """Append one row. Returns True only if it actually reached the disk.

    The return value is not decoration. delivery.record() used to return None,
    swallowed an EACCES on every call, and thereby made a shipped feature inert
    AND invisible for a full release — the caller could not tell success from a
    permission error. Best-effort telemetry must never break the call it
    describes, but "best-effort" must not mean "indistinguishable from success".
    """
    rec = {"v": SCHEMA, "ts": int(time.time()), "outcome": outcome}
    # Absent optionals are OMITTED, never written as null, so a reader can tell
    # "not applicable" from "measured as nothing" — the same convention
    # delivery.record() and switchboard-callqos use.
    rec.update({k: v for k, v in fields.items() if v is not None})
    rec = _redact(rec, transcripts)

    d = os.path.dirname(PATH) or "."
    lock_path = PATH + ".lock"
    fh = None
    try:
        os.makedirs(d, exist_ok=True)
        if fcntl is not None:
            fh = open(lock_path, "a+")
            fcntl.flock(fh, fcntl.LOCK_EX)
        lines = []
        try:
            with open(PATH, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            lines = []
        lines.append(json.dumps(rec, separators=(",", ":")))
        lines = lines[-MAX_RECORDS:]
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".assistant-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, PATH)
            # 0600, not the 0664 the shared ledgers use. This file has exactly
            # one writer — the assistant AGI, running as the asterisk user — and
            # it is the only file in this system that can contain the words
            # people say in their own home. Nothing else needs to read it.
            try:
                os.chmod(PATH, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except OSError:
        return False
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()
