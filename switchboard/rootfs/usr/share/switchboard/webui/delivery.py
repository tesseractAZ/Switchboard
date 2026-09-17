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

# ★ ONE DEFINITION OF "SETTLED" (2026-09-16). unresolved_announcements() spelled
# this as an inline tuple of its own, and the automatic retry needs the SAME
# three names: a clip this set covers is exactly a clip that must never be
# replayed, and one it does not cover is exactly a clip the reconciler will still
# speak a verdict for. Two copies of a three-name tuple is how the retry starts
# replaying announcements the reconciler has already judged, or stops retrying
# ones it has not, with every test still green.
#
# NOT a list to add rows to. An outcome belongs here only when it is a FINAL
# verdict about the audio — which is why ANNOUNCE_ORIGINATE_FAILED,
# ANNOUNCE_GUARD_UNJUDGED and both retry outcomes below are outside it.
ANNOUNCE_TERMINAL = (AUDIO_DELIVERED, ANNOUNCE_UNDELIVERED, ANNOUNCE_UNSETTLED)

# ★ THE ORIGINATE THAT NEVER BECAME A CALL (2026-09-15).
#
# The announce path used to write two bare literals here, `originate-error` when
# the Originate raised and `originate-refused` when AMI accepted the connection
# and refused the action. Neither carried the CLIP NAME, and both spell the same
# two strings the WAKE-UP path writes for its own originates — so an announce
# failure could be joined to nothing and read like a wake-up row. One name, one
# row per attempt (writing both was a live defect once: see
# test_boundary_audit_fixes), and the cause moves onto the row as `reason`:
#
#     reason="ami-error"  the Originate raised — AMI down, auth, a socket
#     reason="refused"    AMI answered and declined the Originate
#
# The clip name rides along under `sound`, exactly as ANNOUNCE_QUEUED carries it,
# so a reader joining an announcement's history on the clip sees the failure in
# the same place a success would be. Deliberately NOT in the `resolved` set of
# unresolved_announcements(): this outcome is written INSTEAD of ANNOUNCE_QUEUED,
# never after it, so there is no queued row for it to resolve.
#
# The wake-up path keeps `originate-error` / `originate-refused` untouched —
# those are read by the escalation logic and by DOCS.md's table.
ANNOUNCE_ORIGINATE_FAILED = "announce-originate-failed"

# ★ THE GUARD THAT COULD NOT JUDGE (2026-09-15).
#
# Live at 01:42:12Z, 8.4 s after an add-on restart: the webui queued an
# announcement to the cordless before ext 19 had re-registered, Asterisk logged
# `Could not create dialog to invalid URI '19'`, and the clip never played.
# app.py HAS a pre-flight guard written for exactly that (device_unreachable),
# and it passed — because ami.get_device_state() returns "" on any failure to
# read, AMI was not answering seconds after the restart, and an empty state is
# deliberately NOT unreachable. The guard fails OPEN so that an AMI hiccup can
# never silence an alarm, which is the right direction and is kept.
#
# What was missing is that failing open left NO TRACE. The ledger showed
# `originate-queued` and then, three minutes later, `announce-unsettled` — and
# nothing anywhere said the reachability check had been skipped rather than
# passed. This row says so: the guard ran, it could not judge, and the
# announcement went out anyway.
#
# NOT a refusal and NOT a verdict. It is written BEFORE the Originate and the
# announcement still proceeds, so a clip can carry both this row and a later
# terminal one. It is therefore kept OUT of the `resolved` set below — treating
# it as resolving would silently retire the reconciler for every announcement
# whose state read hiccuped, which is precisely the population it exists for.
ANNOUNCE_GUARD_UNJUDGED = "announce-guard-unjudged"

# ★ THE AUTOMATIC RETRY (2026-09-16), and why it needs two names of its own.
#
# The owner's decision after the third occurrence of the shape above: an
# announcement whose audio never played must be RETRIED, not merely recorded.
# wakeup/scheduler.py's _retry_announcements() does it, and these are the only
# two rows it writes itself.
#
# ANNOUNCE_RETRY_ATTEMPTED is written BEFORE each retry Originate and GATES it —
# an attempt that could not be counted is an attempt that could repeat forever,
# so if record() returns False the Originate does not happen. It carries the
# ORIGINAL clip name, so every row about one announcement stays in one join, and
# `attempt`/`of` say which of how many. It is NOT a verdict: the audio may still
# arrive, and ANNOUNCE_TERMINAL deliberately excludes it.
#
# ★ IT IS ALSO THE BUDGET. The count of these rows on disk is what bounds the
# retry — never a counter in memory, because a restart inside the window would
# hand the same clip a fresh budget and the announcement could be replayed
# without limit. (The v1.159.0 lesson from the power add-on: a retry slot that
# does not survive the process cannot count.)
ANNOUNCE_RETRY_ATTEMPTED = "announce-retry-attempted"
# ...and why the retry gave up, exactly once per clip: reason=too-old |
# budget-exhausted | ext-superseded | clip-gone. TRANSIENT deferrals (the handset
# is not idle yet, AMI could not be read, only one clean observation so far) are
# LOGGED and NOT recorded — a row every 20 s would trim the history this ledger
# exists to keep. Terminal for the RETRY only, and deliberately NOT in
# ANNOUNCE_TERMINAL: the announcement still never arrived, so the reconciler must
# still file announce-undelivered / announce-unsettled for it. The candidate scan
# excludes any clip that already has one of these, which is what makes "exactly
# once" hold across a restart without any in-memory bookkeeping.
ANNOUNCE_RETRY_SKIPPED = "announce-retry-skipped"

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

# ─── the automatic retry's bounds ──────────────────────────────────────────
# Read by wakeup/scheduler.py's _retry_announcements(). Here rather than there
# for the same reason the horizon is here: the retry and the reconciler judge the
# same population from opposite ends, and a number that decides both must have
# one home. Every one of these is env-overridable, and
# ANNOUNCE_RETRY_MAX_ATTEMPTS=0 is a complete kill switch.

# How many real Originates a missed announcement may get, counted from
# ANNOUNCE_RETRY_ATTEMPTED rows ON DISK.
#
# ★ TWO, FROM THE MEASURED RE-REGISTRATION BAND. Every handset was back inside
# ~46 s of the scheduler starting on the build that produced the 2026-09-15
# incident: ext 19's contact returned 37.9 s after the failed Originate, ext 14
# last at 45.6 s, the other seven between 0.1 s and 30 s; the 09-15 review
# measures the cordless at 30-45 s on this build. Two attempts spaced at least
# one poll apart straddle that band from either side. A third buys nothing —
# past ~110 s the cause is no longer the restart, and steady-state
# unreachability for this handset measured 2 of 1,714 polls (0.12 %), where a
# fourth INVITE is noise and announce-undelivered is the honest record.
ANNOUNCE_RETRY_MAX_ATTEMPTS = int(os.environ.get("ANNOUNCE_RETRY_MAX_ATTEMPTS", "2") or 2)

# How old an announcement must be before its first retry, and how long after one
# attempt before the next.
#
# ★ ONE POLL (WAKEUP_POLL_SECONDS is 20). Two things make this the floor rather
# than a preference. The resolving row lands essentially AT hangup — the journal
# has the `h` extension at 04:45:21.897 and the detached callqos spawned at
# .898, and record() stamps whole seconds — so 20 s is four orders of magnitude
# of headroom over the race between "it played" and "we decided it had not". And
# as the inter-attempt spacing it guarantees attempt 1 is visible before attempt
# 2 is considered: 20 s in, attempt 1 is either still ringing (not idle, see
# ANNOUNCE_RING_SECONDS) or has already failed.
ANNOUNCE_RETRY_MIN_AGE = float(os.environ.get("ANNOUNCE_RETRY_MIN_AGE", "20") or 20)

# How many CONSECUTIVE idle observations, one poll apart, clear a handset for a
# replay. Two, at a cost of one poll of latency, because a duplicate
# announcement in a quiet house at 03:00 is worse than the original miss.
# Honest about what it buys: it closes the hangup-versus-ledger race, and it does
# NOT close a stale AOR contact that still reads idle for a handset that has left
# the network.
ANNOUNCE_RETRY_CLEAN_TICKS = int(os.environ.get("ANNOUNCE_RETRY_CLEAN_TICKS", "2") or 2)

# ★ HOW LATE A REPLAY MAY STILL START, measured from the ORIGINAL queued row.
#
# NOT ANNOUNCE_SETTLE_SECONDS, and that is the whole point of this comment.
# 120 s is the right answer to "has the PBX come back yet?" and the wrong
# quantity for this question, because the endpoint is not even available until
# 38-46 s into that window: at 120 s a SECOND attempt is structurally
# unreachable for two real shapes. A handset that is registered and never
# answers reads Ringing for the full ANNOUNCE_RING_SECONDS, so the first attempt
# cannot start before ~60 s and the second cannot be reached before ~120 s; a
# 46 s re-registration (the top of the measured band) puts attempt 1 at ~86 s
# and attempt 2 at ~106 s. A bound that cannot fire for a whole population is a
# feature that looks shipped and is not.
#
# 150 s is pinned against the HARD PHYSICAL CEILING instead: the clip's life.
# app.py's _cleanup_announce_dir prunes by mtime at the top of every announce
# POST, and a retry's clip must survive until Playback runs — the ring plus the
# clip cap after the Originate. 150 + 30 + 90 = 270 < that 300 s, so at these
# bounds the clip cannot be pruned out from under a retry, which demotes the
# clip-gone check to defence in depth against a forged or stale ledger name.
# Pinned by test, both ways (see tests/test_announce_retry.py, §invariants).
#
# In household terms it is 2.5 minutes — the same moment in a house. The owner's
# "20 minutes late is worse than never" is eight times further away.
ANNOUNCE_RETRY_MAX_AGE = float(os.environ.get("ANNOUNCE_RETRY_MAX_AGE", "150") or 150)


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

    ★ A SET THAT REPLACED ONE IS STILL ONE ROW (2026-09-15). store.set_wakeup
    keeps one entry per extension, so setting a second wake-up for a room
    silently overwrites the first. Live that day: a 06:20 set at 13:10:35Z was
    replaced by a 04:00 one at 13:15:09Z, and the ledger held `set 06:20`, then
    no ring and no cancel — indistinguishable from a wake-up the system LOST.
    When the store reports an entry it displaced, the row it already writes names
    the time that went away as well as the one that replaced it.

    ONE row, not two. A separate `replaced` row would be the obvious shape and it
    is wrong twice over: it doubles every replacement in the ledger, and
    room_changed_wakeup() below matches on WAKEUP_SET / WAKEUP_CANCELLED, so a
    replacement dialled on the room's own phone — a snooze, the exact thing that
    reader exists to catch — would stop counting as a change at all.

    Deliberately not carried: anything that was said. This file is in /share.
    """
    if entry is not None:
        # store.set_wakeup returns the displaced entry under `replaced` on the
        # dict it HANDS BACK only; nothing of the sort is ever persisted. Absent
        # on a first-ever set, and record() omits None extras, so a first set is
        # byte-for-byte the row it always was.
        replaced = entry.get("replaced") or {}
        return record(str(ext), "wakeup", WAKEUP_SET, source=source,
                      hhmm=entry.get("hhmm"), target_epoch=entry.get("target_epoch"),
                      replaced_hhmm=replaced.get("hhmm"),
                      replaced_target_epoch=replaced.get("target_epoch"))
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


def announce_floor(now: float, lookback: float | None = None,
                   not_before: float | None = None) -> float:
    """The oldest timestamp an announce pass may look at. One expression, because
    the two passes below MUST share a floor to be able to share one read."""
    lookback = ANNOUNCE_LOOKBACK if lookback is None else lookback
    floor = now - lookback
    if not_before is not None:
        floor = max(floor, not_before)
    return floor


def announce_records(now: float | None = None, lookback: float | None = None,
                     not_before: float | None = None) -> list:
    """The ledger tail both announce passes read, for ONE caller to read once.

    The reconciler and the retry ask different questions of the same handful of
    rows, and _read_records() does a full readlines() of a ledger capped at 2 MB.
    Two reads per 20 s tick doubles that on a Pi for no gain, and — worse — lets
    the two passes see two different ledgers when a row lands between them.
    Pass the result to both as `recs=`.
    """
    now = time.time() if now is None else now
    return _read_records(announce_floor(now, lookback, not_before))


def _announce_tail(recs: list | None, floor: float) -> list:
    """Records at or after `floor`, read from disk when the caller has none.

    The floor is applied to a caller-supplied list too, rather than trusted: a
    caller that read with a wider window (a longer lookback, an earlier
    not_before) would otherwise silently widen the pass's own boundary — and one
    of those boundaries is the restart rule that keeps this reconciler from
    judging a window it was not running for. Pinned by test with a list that
    reaches back further than the floor.
    """
    if recs is None:
        return _read_records(floor)
    return [r for r in recs if r.get("_ts", 0.0) >= floor]


def _retry_attempts(recs: list) -> dict:
    """clip_key -> (how many retry Originates, the newest one's timestamp).

    Counted from the rows on disk and nowhere else. A counter in memory would be
    refilled by the restart that causes this defect in the first place, and an
    uncountable attempt is an unbounded one.
    """
    out: dict = {}
    for r in recs:
        if (r.get("kind") == "announce"
                and r.get("outcome") == ANNOUNCE_RETRY_ATTEMPTED and r.get("sound")):
            key = clip_key(r["sound"])
            n, ts = out.get(key, (0, 0.0))
            out[key] = (n + 1, max(ts, r.get("_ts", 0.0)))
    return out


def unresolved_announcements(now: float | None = None, horizon: float | None = None,
                             lookback: float | None = None,
                             not_before: float | None = None,
                             recs: list | None = None) -> list:
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

    ★ v0.105.0 — THE HORIZON RUNS FROM THE NEWEST ATTEMPT. Since the scheduler
    may now RETRY a missed announcement (ANNOUNCE_RETRY_ATTEMPTED), a clip can be
    re-originated up to ANNOUNCE_RETRY_MAX_AGE after it was queued. Measuring the
    horizon from the queue alone would then file `announce-undelivered` against a
    replay that was still ringing or still playing — the same false verdict this
    function's own horizon exists to prevent, arriving through the other door.
    The returned record carries `retries` so the verdict can say how hard the
    system tried before filing it.

    Returns the queued records, oldest first. Already-judged ones are excluded by
    the same join, so calling this on a timer does not re-file anything.
    """
    now = time.time() if now is None else now
    horizon = ANNOUNCE_HORIZON if horizon is None else horizon
    # One floor, computed once and applied once — by _announce_tail, whether the
    # rows came from the file or from the caller's shared read. A second
    # `_ts >= floor` test in the loop below read as belt and braces and was
    # provably dead, which makes it a line that invites a reader to believe it is
    # load-bearing.
    floor = announce_floor(now, lookback, not_before)
    recs = _announce_tail(recs, floor)
    # Sounds that already have an answer, either way.
    # Any terminal verdict resolves a clip — including "not judged". Leaving
    # ANNOUNCE_UNSETTLED out would re-file it on every 20 s tick forever, which
    # is the failure this exclusion set exists to prevent. ANNOUNCE_TERMINAL is
    # that set, shared with the retry so the two cannot drift apart.
    resolved = {clip_key(r.get("sound")) for r in recs
                if r.get("kind") == "announce"
                and r.get("outcome") in ANNOUNCE_TERMINAL
                and r.get("sound")}
    attempts = _retry_attempts(recs)
    out = []
    for r in recs:
        if not (r.get("kind") == "announce" and r.get("outcome") == ANNOUNCE_QUEUED
                and r.get("sound")):
            continue
        key = clip_key(r["sound"])
        if key in resolved:
            continue
        tried, last_attempt = attempts.get(key, (0, 0.0))
        if now - max(r["_ts"], last_attempt) < horizon:
            continue
        rec = dict(r)          # a copy: the caller's shared list is not ours to mark
        rec["retries"] = tried or None
        out.append(rec)
    return out


def retryable_announcements(now: float | None = None,
                            not_before: float | None = None,
                            lookback: float | None = None,
                            recs: list | None = None,
                            max_attempts: int | None = None,
                            min_age: float | None = None,
                            max_age: float | None = None) -> list:
    """Announcements whose audio never played and which may still be REPLAYED.

    ★ THE OWNER'S DECISION, 2026-09-15, after the third occurrence. An
    announcement whose audio never played must be retried automatically. This is
    the ledger half of it: which clip, how many attempts it has already had, and
    — when it is past helping — why it is being retired. The scheduler decides
    whether the handset can take a call; nothing here touches AMI, so a forged or
    stale row costs no traffic and a clip still ages out correctly while AMI is
    down.

    Every condition below is a LEDGER fact, and all of them must hold:

    L1  a newest `originate-queued` row with a non-empty `sound`, at or after
        `not_before`. Inherited unchanged from unresolved_announcements: a window
        this process was not running for is unknowable, and is never replayed.
        (Live reason, not theory: the 47 announce QoS legs before 2026-09-11
        carry `sound: None`, so no delivered row could ever exist for them.)
    L2  no row for that clip in ANNOUNCE_TERMINAL — which is what makes "an
        announcement whose audio DID play is never replayed" true at any age, in
        any order, including a delivered row that lands AFTER an attempt row —
        and no ANNOUNCE_RETRY_SKIPPED row, which is what makes the retirement
        below happen exactly once per clip without any memory.
    L3  fewer than `max_attempts` ANNOUNCE_RETRY_ATTEMPTED rows on disk.
    L4  at least `min_age` since the queue AND since the newest attempt. This
        gates the retirements as well, so a clip is never retired while its own
        last attempt could still be ringing.
    L5  no more than `max_age` since the QUEUED row.
    L6  it is the NEWEST announcement QUEUED to its extension. A newer one to
        the same room supersedes it: the producer has moved on, and replaying the
        older message now would speak stale content into that room, after the
        newer one, out of order. Exactly ONE clip per extension is ever live, so
        a burst of announcements to one room cannot each earn their own replay —
        which the app no longer collapses either, since an originate whose
        pre-flight could not judge deliberately stops arming the duplicate
        window. Ties (record() stamps whole seconds) are broken by ledger order,
        the producer's own order within that second. (Not idle chatter — 34 of
        the 35 announcements on this build went to one extension.)

        Judged on the QUEUE times, not on when audio arrived. A long clip queued
        BEFORE this one writes its `audio-delivered` row AFTER it, which read as
        "the room has been spoken to since" while the truth was the reverse, and
        retired a newer announcement that had never played. A newer clip that
        DID play still supersedes this one — its queue row is newer too — and
        its own delivered row is what retires it under L2 in any case.

    A clip that fails only L3/L5/L6 is returned with a `stale_reason`, for the
    caller to retire with ONE ANNOUNCE_RETRY_SKIPPED row. Precedence is
    ext-superseded, then budget-exhausted, then too-old: the most specific fact
    about why replaying it would be wrong, rather than the first one tested.

    `max_attempts <= 0` returns nothing at all — the kill switch is here, in the
    function that defines the population, so it cannot be bypassed by a caller.
    """
    now = time.time() if now is None else now
    max_attempts = (ANNOUNCE_RETRY_MAX_ATTEMPTS if max_attempts is None
                    else max_attempts)
    if max_attempts <= 0:
        return []
    min_age = ANNOUNCE_RETRY_MIN_AGE if min_age is None else min_age
    max_age = ANNOUNCE_RETRY_MAX_AGE if max_age is None else max_age
    floor = announce_floor(now, lookback, not_before)
    recs = _announce_tail(recs, floor)

    queued: dict = {}            # clip_key -> its newest queued row
    order: dict = {}             # clip_key -> where that row sits in the ledger
    retired: set = set()         # clip_key -> already answered, or already retired
    for i, r in enumerate(recs):
        if r.get("kind") != "announce" or not r.get("sound"):
            continue
        key = clip_key(r["sound"])
        outcome = r.get("outcome")
        if outcome == ANNOUNCE_QUEUED:
            prev = queued.get(key)
            if prev is None or r.get("_ts", 0.0) >= prev.get("_ts", 0.0):
                queued[key] = r
                order[key] = i
        elif outcome in ANNOUNCE_TERMINAL or outcome == ANNOUNCE_RETRY_SKIPPED:
            retired.add(key)
    attempts = _retry_attempts(recs)

    # ★ ONE LIVE CANDIDATE PER EXTENSION (L6), and it is the NEWEST queue to that
    # room. Two candidates for one handset are two Originates deciding they may
    # fire from ONE endpoint read — neither can see the other's call, and a second
    # INVITE to the cordless cannot auto-answer, it rings as call waiting. The
    # newest wins because it is the message the house is currently owed; the
    # others are retired with a row that says so.
    #
    # Retired clips are deliberately still counted as superseders: an announcement
    # that has already been ANSWERED is the strongest possible evidence that the
    # room has moved on.
    newest: dict = {}            # ext -> (rank, clip_key) of its newest queue
    for key, r in queued.items():
        ext = str(r.get("ext") or "")
        rank = (r.get("_ts", 0.0), order.get(key, 0))
        if ext not in newest or rank > newest[ext][0]:
            newest[ext] = (rank, key)

    out = []
    for key, r in queued.items():
        if key in retired:                                          # L2
            continue
        ext = str(r.get("ext") or "")
        queued_ts = r.get("_ts", 0.0)
        tried, last_attempt = attempts.get(key, (0, 0.0))
        if now - max(queued_ts, last_attempt) < min_age:            # L4
            continue
        if newest.get(ext, (None, key))[1] != key:
            reason = "ext-superseded"                               # L6
        elif tried >= max_attempts:
            reason = "budget-exhausted"                             # L3
        elif now - queued_ts > max_age:
            reason = "too-old"                                      # L5
        else:
            reason = None
        out.append({"sound": r["sound"], "ext": ext, "queued": r.get("ts"),
                    "queued_ts": queued_ts, "attempts": tried,
                    "stale_reason": reason})
    out.sort(key=lambda c: c["queued_ts"])
    return out


def announce_clip_resolved(sound, now: float | None = None,
                           not_before: float | None = None,
                           lookback: float | None = None) -> bool:
    """Does this clip ALREADY have a terminal verdict? The last look before a replay.

    ★ WHY A SECOND, NARROWER READ. The retry decides on three things in order: the
    ledger tail, then the handset's device state (an AMI round trip), then this.
    Reading the veto LAST means a delivered row that lands while we are deciding
    can only APPEAR, never be missed — the ordering rule that keeps the
    duplicate-safety join honest across the AMI latency.

    An empty tail is NOT a green light. A candidate was just derived from this
    same file in this same tick, so no rows at all means the ledger has gone away
    or become unreadable — in which case neither "it played" nor the attempt
    count can be known, and the answer must be the one that plays nothing.
    """
    now = time.time() if now is None else now
    key = clip_key(sound)
    if not key:
        return True                       # unjoinable: never replayable
    tail = _read_records(announce_floor(now, lookback, not_before))
    if not tail:
        return True
    for r in reversed(tail):
        if (r.get("kind") == "announce" and r.get("outcome") in ANNOUNCE_TERMINAL
                and clip_key(r.get("sound")) == key):
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
