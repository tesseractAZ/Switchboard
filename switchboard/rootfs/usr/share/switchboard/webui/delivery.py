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
import time

OUTCOME_PATH = os.environ.get("SWITCHBOARD_DELIVERY_OUTCOME",
                              "/share/switchboard/delivery-outcomes.jsonl")
MAX_BYTES = 2 * 1024 * 1024

# ★ THE OUTCOME NAMES LIVE HERE BECAUSE TWO PROGRAMS MUST SPELL THEM THE SAME.
#
# switchboard-callqos writes AUDIO_DELIVERED from the hangup extension and
# wakeup/scheduler.py joins on it to decide whether to ring a phone again and
# push a critical alert. They are separate programs in separate directories, and
# a literal in each is a silent single-character failure: the reconciler simply
# stops seeing the record, every test stays green, and the symptom is a
# DND-bypassing push at six in the morning to somebody who is already awake.
# Both import this module already.
#
# These are also a DURABLE ON-DISK FORMAT. Renaming one orphans every historical
# record, so the value is pinned by test rather than treated as internal.
AUDIO_DELIVERED = "audio-delivered"
# What app.py writes when AMI ACCEPTS an announce Originate — which is all it can
# know at that moment — and what the reconciler below files when nothing ever
# answered it.
ANNOUNCE_QUEUED = "originate-queued"
ANNOUNCE_UNDELIVERED = "announce-undelivered"
# ...and the verdict for an announcement nobody can fairly judge: one queued
# while the PBX was still coming back up. See ANNOUNCE_SETTLE_SECONDS.
ANNOUNCE_UNSETTLED = "announce-unsettled"

# ★ WHO CHANGED A WAKE-UP, AND FROM WHERE (2026-09-14).
#
# A wake-up's history in this file used to begin at the ring. That morning ext
# 14's 05:50 wake-up rang twice unanswered and escalated with a critical push,
# and no ledger anywhere could say whether a person had set it, from which
# screen, or when. Every setter now writes one of these once its store write has
# succeeded — see record_wakeup_change().
WAKEUP_SET = "set"
WAKEUP_CANCELLED = "cancelled"
# ...and WHERE, because that decides what the change proves. A set or cancel
# dialled on the ringing room's own phone means somebody is at that phone and
# awake. The same change from the dashboard or the console proves nothing about
# the sleeper: whoever made it may be setting it FOR them. The dial-42 AGI writes
# this field and the wake-up reconciler reads it, so it is spelled once, here.
SOURCE_PHONE = "phone"
SOURCE_WEB = "web"
SOURCE_CONSOLE = "console"
# ...and the verdict for a ring the room answered by changing its wake-up instead
# of picking up. Live the same morning: ext 19 dialled 42 during three rings and
# set a later time each time, and the reconciler rang it again twice and sent
# three Do-Not-Disturb-bypassing pushes saying nobody had picked up — to the
# person who had just spoken a new time into that handset.
WAKEUP_SNOOZED = "snoozed"

# The longest an announcement may be. Read here rather than in app.py because the
# reconciler MUST NOT judge an announcement undelivered while it is still
# playing, and its horizon is derived from this number; a second copy of it over
# there would let the two drift into exactly that false alarm.
ANNOUNCE_MAX_SECONDS = float(os.environ.get("ANNOUNCE_MAX_SECONDS", "90") or 90)
# ami.announce_to_ext's Originate `Timeout` — how long the handset may ring
# before Asterisk gives up. An announcement can therefore legitimately take the
# ring plus the whole clip before its hangup record appears.
ANNOUNCE_RING_SECONDS = 30.0

# ...and the horizon: how long after the Originate was queued an announcement
# with no terminal record is judged undelivered.
#
# ★ DERIVED, not guessed. An earlier plan used a flat ~120 s, which is LESS than
# ring + the 90 s clip cap — so it would have cried wolf on precisely the longest
# announcements, the ones most worth getting right. Observed lag from originate
# to the hangup record tracks playback duration closely (23 s at a 22 s clip,
# 45 s at 44 s), so the clip cap is the term that matters and the margin covers
# TTS rendering, a slow answer and the detached sink's own scheduling.
ANNOUNCE_HORIZON = max(180.0, ANNOUNCE_MAX_SECONDS + ANNOUNCE_RING_SECONDS + 60.0)

# How long after this process started the PBX is still considered to be coming
# back up, during which an announcement is recorded as UNJUDGED rather than as
# failed.
#
# ★ MEASURED, and the number is borrowed rather than invented. Asterisk restarts
# with the add-on; its endpoints do not come back instantly, and an announcement
# originated into that gap genuinely does not arrive — the record would be true
# and useless, because the cause is a restart the operator already knows about.
# Live on 2026-09-11: an announcement queued 18 s after a restart failed with
# `Could not create dialog to invalid URI '19'`, and another was refused
# pre-flight 24 s after the next one. Over the same ledger the cordless was
# unreachable in 2 of 1,714 STEADY-state polls (0.12 %) — so the failures belong
# to the restart, not to the handset.
#
# 120 s is rtpmon's own settling cap for exactly this condition
# (WARMUP_MAX_POLLS * WARMUP_DELAY = 8 * 15), the point at which the fleet
# monitor stops waiting for ports to re-register and calls the fleet steady.
# Two subsystems answering "has the PBX come back yet?" should not answer it with
# two different numbers.
ANNOUNCE_SETTLE_SECONDS = 120.0

# ...and how far back to look at all. Without this, a scheduler that was stopped
# for a day would wake up and file every announcement it had missed as a fresh
# failure — a burst of alarming records about a period nobody can act on any
# more. Beyond this the answer is "unknown", which is not the same as "failed".
ANNOUNCE_LOOKBACK = 3600.0


def _open_no_follow(path: str, flags: int) -> int:
    """An fd for the REGULAR file at `path`, never through a symlink. Raises OSError.

    ★ A LEDGER IN A DIRECTORY OTHERS CAN WRITE (2026-09-14). /share/switchboard
    is owned by `asterisk` and group-writable, and whatever can write the shared
    folder from the host can add entries to it as well. The wake-up
    scheduler and the web UI call record() as root; the wake-up AGIs and
    switchboard-callqos call it as `asterisk`.

    By name, an append, a trim or a chmod follows a symlink planted in place of
    the ledger and lands on its target. O_NOFOLLOW makes a link fail to open;
    the fstat refuses a FIFO, a device or a directory; O_NONBLOCK keeps a planted
    FIFO from hanging the open. Everything after goes through the fd, so a name
    swapped after the check cannot redirect it. switchboard-config's
    open_share_file() applies the same rule to the boot pass.
    """
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o664)  # intentional: root services AND the asterisk-user AGIs share these ledgers; group write, never world
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path}: not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _rotate_tail(path: str, max_bytes: int, keep_frac: float = 0.5) -> None:
    """Trim an append-only ledger to its newest records. Best-effort.

    v0.77.0 — this used to be `open(path, "w")` followed by `pass`, which
    truncates the file to ZERO BYTES. The comment above it read "Truncating keeps
    the newest records, which are the ones a reader wants" — the exact opposite
    of what the code did. At the cap the entire forensic history disappeared, and
    silently: an empty ledger and a quiet system look identical, which is the
    failure mode this whole file exists to prevent.

    Keeps the last `keep_frac` of the cap, cut at a line boundary so the first
    surviving record is not half a JSON object. Rewrites in place rather than
    renaming, so a reader holding the path keeps reading the same inode.

    ★ BY FD, NEVER THROUGH A SYMLINK (2026-09-14). This opened the path twice by
    name, and both opens follow a link: a link planted in place of the ledger
    turned the trim into a rewrite of whatever it pointed at. See
    _open_no_follow(). The bytes it leaves are unchanged, and
    test_ledger_rotation still holds every copy to the same output.
    """
    try:
        fd = _open_no_follow(path, os.O_RDWR)
    except OSError:
        return                            # absent, a link, or not a file: nothing to trim
    try:
        with os.fdopen(fd, "r+b") as fh:
            if os.fstat(fh.fileno()).st_size <= max_bytes:
                return
            keep = max(1, int(max_bytes * keep_frac))
            fh.seek(-keep, os.SEEK_END)
            tail = fh.read()
            # Everything before the first newline is half a record. Drop it: a
            # reader must never have to guess whether the first line is complete.
            nl = tail.find(b"\n")
            tail = tail[nl + 1:] if nl != -1 else b""
            # Written first and cut after, so the file is never empty.
            fh.seek(0)
            fh.write(tail)
            fh.truncate()
    except OSError:
        pass                              # a trim must never break the write


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
        _rotate_tail(OUTCOME_PATH, MAX_BYTES)
        # By fd, never through a symlink: the append AND the chmod below. Both
        # went by name, so a link planted in place of this ledger took a root
        # append and a root group-write bit to whatever it pointed at. A link now
        # fails the write, which returns False and says so, as EACCES does.
        with os.fdopen(_open_no_follow(OUTCOME_PATH,
                                       os.O_WRONLY | os.O_APPEND | os.O_CREAT),
                       "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            # Group-writable: the scheduler (root) and the AGI (asterisk) both
            # append here, and whichever creates the file decides whether the
            # other can.
            #
            # ADD the group-write bit rather than asserting a literal mode.
            # Writing 0o664 would also assert world-readable, which is a broader
            # claim than this needs to make — the file inherits whatever the
            # umask and the setgid directory already decided, and this only
            # ensures the second writer is not locked out.
            try:
                os.fchmod(fh.fileno(), os.fstat(fh.fileno()).st_mode | stat.S_IWGRP)
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


def record_wakeup_change(ext: str, source: str, entry: dict | None = None,
                         removed: bool | None = None) -> bool:
    """Record a wake-up SET (pass the `entry` store.set_wakeup returned) or a
    CANCEL (pass `removed`, what store.cancel returned). Returns record()'s bool.

    One writer for all three setters — dial 42, the dashboard and the console —
    kept beside room_changed_wakeup(), the reader that acts on it, so the shape
    of the row cannot drift between the program writing it and the one deciding
    whether to push a critical alert because of it.

    `removed` is kept on a cancel because a cancel that found nothing is still a
    person. While a wake-up is ringing the scheduler has ALREADY consumed its
    entry, so somebody who dials 42 and says "cancel" to stop the ringing removes
    nothing — and is exactly as awake as somebody who did.

    Deliberately not carried: anything that was said. This file is in /share.
    """
    if entry is not None:
        return record(str(ext), "wakeup", WAKEUP_SET, source=source,
                      hhmm=entry.get("hhmm"), target_epoch=entry.get("target_epoch"))
    return record(str(ext), "wakeup", WAKEUP_CANCELLED, source=source,
                  removed=removed)


def room_changed_wakeup(ext: str, since_ts: float):
    """The newest wake-up set or cancel dialled on `ext`'s OWN phone at or after
    `since_ts`, or None.

    ★ THE SNOOZE READER (2026-09-14). The store cannot answer this: `set_at` is
    written the same whoever set the wake-up, and a cancel leaves no entry at
    all. Only this ledger records where a change came from, and only a change
    from the room's own phone says anything about whether its sleeper is awake.

    The cut is floored to the whole second because record() stamps whole seconds:
    a set made in the same second the ring started would otherwise read as older
    than the ring.

    An unreadable ledger returns None — "no snooze" — and the caller judges the
    ring exactly as it did before this existed. That is the safe direction: a
    missed snooze costs somebody awake one unneeded push, a false one silences the
    alarm for somebody asleep.
    """
    for rec in reversed(_read_records(int(since_ts))):
        if (rec.get("ext") == str(ext) and rec.get("kind") == "wakeup"
                and rec.get("outcome") in (WAKEUP_SET, WAKEUP_CANCELLED)
                and rec.get("source") == SOURCE_PHONE):
            return rec
    return None


def clip_key(sound) -> str:
    """The canonical form of an announcement clip name, for joining across the
    dialplan boundary.

    ★ v0.98.2 — WHY THIS IS NOT JUST `sound`. The name is written here by app.py
    as `ann-19-1b411fcd...` and travels to the other half of the join through an
    Asterisk channel variable and a `FILTER()` charset. On 2026-09-11 at 02:00:36
    it arrived as `ann191b411fcd...` — BOTH HYPHENS GONE — because FILTER reads a
    hyphen as a range separator and the charset spelled it in a position where it
    was consumed rather than admitted. The reconciler compares for equality, so
    the announcement that had just played in full (stage `complete`, 389 packets)
    was reported as never delivered 197 seconds later. Every announcement would
    have been, forever.

    The charset is fixed too. This exists because that fix is a claim about how a
    particular Asterisk build parses a particular escape, and the correctness of
    an alarm path should not rest on one: the live ledger shows `a-z-` PRESERVING
    the hyphens in `room-to-room` while `A-Za-z0-9_.-` dropped them, so the
    behaviour turns on parse order, not on a rule anyone could read off the
    charset. Comparing canonical forms is immune to all of it.

    Case-folded and reduced to letters and digits — the only characters a charset
    like this can be relied on to pass. Collision-safe for the names actually
    minted: app.py builds `ann-<ext>-<uuid4 hex>`, so 128 bits of the key survive
    canonicalisation.
    """
    return "".join(c for c in str(sound or "").lower() if c.isalnum())


def _read_records(since_ts: float) -> list:
    """Every parseable record at or after `since_ts`, oldest first.

    Scans from the END backwards and stops at the first record older than the
    cut, like outcomes_since: the file is append-only and every caller asks about
    a bounded recent window, so this touches a handful of lines rather than the
    2 MB cap. A malformed line is skipped, never fatal.
    """
    try:
        with open(OUTCOME_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
            rec["_ts"] = datetime.datetime.fromisoformat(rec["ts"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if rec["_ts"] < since_ts:
            break
        out.append(rec)
    out.reverse()
    return out


def unresolved_announcements(now: float | None = None, horizon: float | None = None,
                             lookback: float | None = None,
                             not_before: float | None = None) -> list:
    """Announcements queued long enough ago to be judged, with nothing to show.

    ★ THE HOLE THIS CLOSES. app.py records `originate-queued` the moment AMI
    ACCEPTS the Originate — which is all it can know then — and records six ways
    for it to be REFUSED. It has never had a way to record success or silence.
    So when an announcement rings a handset that is never answered, the dialplan
    never runs, there is no `h` extension, no QoS record, and no ledger entry of
    any kind. Live on 2026-09-01 at 19:05:15: `Called 19` -> `is ringing` -> AMI
    hung it up four seconds later, and the announcement is absent from BOTH
    ledgers. Absence in a delivery ledger reads as "we never tried".

    ★ THE JOIN IS ON THE SOUND FILENAME, NOT ON TIME PROXIMITY. Every queued
    record carries `sound` (`ann-<ext>-<32 hex>`), and switchboard-callqos stamps
    the same name onto the record it writes from the hangup extension, so the two
    halves of one announcement identify each other exactly. Pairing them by
    "closest in time on the same extension" would work today only by luck — the
    minimum observed spacing between announcements is about eight minutes — and
    would mis-pair the moment two alerts land together, which is exactly when
    something is going wrong and the ledger matters most.

    `not_before` is the caller's own start time, and it is REQUIRED in practice —
    see the warning below. Records queued before it are left alone.

    ★ v0.98.1 — WHAT THE CALLER COULD NOT HAVE SEEN IS UNKNOWN, NOT FAILED.
    Caught live within ten minutes of shipping v0.98.0, by this very function:
    it filed `announce-undelivered` against an announcement that had played
    perfectly. The announcement was queued at 01:36:24Z under v0.98.0's
    PREDECESSOR, whose hangup extension did not name the clip and whose sink did
    not write a delivery record — so the resolving half could not exist, and the
    upgrade at 01:45 then looked back an hour and judged it.

    The rule that fixes it is not a longer horizon. It is that a window the
    caller was not running for is unknowable: the resolving record is written by
    a detached `switchboard-callqos` that an add-on restart kills outright, and
    before an upgrade it may have been a build that never wrote one at all.
    Judging across that boundary manufactures failures about announcements that
    worked, which is precisely the noise a delivery ledger cannot afford.

    Returns the queued records, oldest first. Already-judged ones are excluded by
    the same join, so calling this on a timer does not re-file anything.
    """
    now = time.time() if now is None else now
    horizon = ANNOUNCE_HORIZON if horizon is None else horizon
    lookback = ANNOUNCE_LOOKBACK if lookback is None else lookback
    floor = now - lookback
    if not_before is not None:
        floor = max(floor, not_before)
    # One floor, applied once: _read_records stops at it, so every record below
    # has already passed it. A second `_ts >= floor` test here read as belt and
    # braces and was provably dead — no mutation of it could change a result —
    # which makes it a line that invites a reader to believe it is load-bearing.
    recs = _read_records(floor)
    # Sounds that already have an answer, either way.
    # Any terminal verdict resolves a clip — including "not judged". Leaving
    # ANNOUNCE_UNSETTLED out would re-file it on every 20 s tick forever, which
    # is the failure this exclusion set exists to prevent.
    resolved = {clip_key(r.get("sound")) for r in recs
                if r.get("kind") == "announce"
                and r.get("outcome") in (AUDIO_DELIVERED, ANNOUNCE_UNDELIVERED,
                                         ANNOUNCE_UNSETTLED)
                and r.get("sound")}
    out = []
    for r in recs:
        if (r.get("kind") == "announce" and r.get("outcome") == ANNOUNCE_QUEUED
                and r.get("sound") and clip_key(r["sound"]) not in resolved
                and now - r["_ts"] >= horizon):
            out.append(r)
    return out


def is_writable() -> bool:
    """Can this process actually append to the ledger?

    The reconciler MUST distinguish "no answer was recorded" from "the ledger
    cannot be written, so no answer could have been recorded". Treating the
    second as the first escalates every successful wake-up.
    """
    try:
        d = os.path.dirname(OUTCOME_PATH)
        os.makedirs(d, exist_ok=True)
        # ★ A LINK IS NOT WRITABLE (2026-09-14). record() refuses anything that
        # is not a regular file, so no answer can land there. exists() and
        # access() both follow a link and called a planted one writable — and
        # the reconciler would then have escalated a wake-up somebody answered.
        if os.path.lexists(OUTCOME_PATH):
            if not stat.S_ISREG(os.lstat(OUTCOME_PATH).st_mode):
                return False
            return os.access(OUTCOME_PATH, os.W_OK)
        return os.access(d, os.W_OK)
    except OSError:
        return False
