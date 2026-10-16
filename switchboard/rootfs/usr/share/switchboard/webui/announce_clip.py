"""Where announcement clips live, what a legal clip name is, and how long one is.

★ WHY THIS FILE EXISTS (v0.105.0). Until the automatic retry there was exactly
one program that wrote this directory and one that read it, and both were
webui/app.py, so a literal there was a single definition. The retry lives in
wakeup/scheduler.py — a different process, on a 20 s loop — and it plays a clip
whose NAME it read out of a ledger in /share, a group-writable directory. Two
things follow, and they are the whole reason this module is not two more lines in
app.py:

  * a second copy of the directory literal is how the scheduler ends up playing
    out of a directory the webui no longer writes to, and
  * app.py never had to VALIDATE a clip name, because it minted every name it
    played. The retry does not; it is handed one. So the name rules live beside
    the directory rather than being reinvented at the new call site.

Nothing here touches AMI or the network: it is a pure filesystem question, so the
retry can refuse a forged or vanished clip before it spends a single AMI round
trip on it.
"""
from __future__ import annotations

import os
import re
import secrets
import stat
import time

# Under /run (container-local, discarded when the Supervisor recreates the
# container at every add-on start): clips are ephemeral by design and never durable
# — they are rendered household speech, and this add-on does not persist that.
# The same path inside the same container for the webui, the scheduler and the
# speaker-announce AGI.
ANNOUNCE_DIR = "/run/switchboard/announce"

# ★ HOW LONG A CLIP LIVES. Every clip older than this is deleted by sweep(): at
# the top of each announcement of either kind, and once a minute by the web UI
# whether or not anything is announced, so a clip is gone between five and six
# minutes after it was written.
#
# Before the timed sweep the only sweeps were the per-announcement ones, so the
# last clip of the day stayed on disk until the next announcement or the next
# restart — and a speaker clip is fetchable from the LAN without credentials
# (the media player cannot present any), so "until the next announcement" could
# mean days.
#
# The floor is the time a clip can still be in use, and it binds on the HANDSET
# side: a retry may start 150 s after the original send, ring for 30 s and play
# a 90 s clip (tests/test_announce_retry.py pins 150 + 30 + 90 < this). A
# speaker clip is a chime, the words recognised from a recording of at most
# twelve seconds, and a chime, and the speaker fetches it as play_media is
# called; a fetch already running when its file is unlinked keeps reading the
# open file to the end. app.py's _cleanup_announce_dir carries the same number
# as its default, pinned equal by tests/test_app.py.
CLIP_MAX_AGE_S = 300


def speaker_clip_name() -> str:
    """A fresh name for a SPEAKER clip: `a` + 20 random digits + `.wav`.

    The name is the only thing between the clip and the LAN — the guard exempts
    `/announce/a<digits>.wav` from authentication because a media player cannot
    authenticate — so it is drawn from `secrets`: 64 bits, written as decimal so
    the digits-only form the guard (app.py), the name check (app.py) and the
    play_media allowlist (ha_broker.py) all accept is unchanged. It used to be
    the AGI's pid followed by the epoch second modulo 100000, which anyone who
    knew roughly when an announcement was made could enumerate."""
    return f"a{secrets.randbits(64):020d}.wav"


def sweep(directory: str | None = None, max_age: float = CLIP_MAX_AGE_S,
          now: float | None = None) -> int:
    """Delete every clip in `directory` (default ANNOUNCE_DIR) last written more
    than `max_age` seconds ago; return how many went. Best-effort: it never
    raises, so neither an announcement nor the web UI's timer can be stopped by
    a file that vanished or could not be removed. lstat, so a planted link is
    judged and removed as itself and its target is never touched."""
    directory = directory or ANNOUNCE_DIR
    # Wall clock, because mtime is: a forward step of more than max_age while a
    # clip is in use would delete it early. HAOS sets the clock before add-ons
    # start, so the step this could meet is a small NTP correction.
    now = time.time() if now is None else now
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    removed = 0
    for name in names:
        path = os.path.join(directory, name)
        try:
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                continue
            if now - st.st_mtime > max_age:
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    return removed


# 8 kHz mono 16-bit PCM — what Asterisk Playback reads, and all the arithmetic a
# duration needs. One definition: app.py's _announce_seconds() delegates here.
ANNOUNCE_BYTES_PER_SECOND = 16000

# The clip cap comes from `delivery`, never from this module's own environment
# read: the reconciler's horizon is derived from that number, and a second
# definition would let a longer clip outlive the horizon that judges it.
try:
    import delivery as _delivery
except Exception:  # noqa: BLE001 — importable on a dev box without the sibling
    _delivery = None
ANNOUNCE_MAX_SECONDS = float(getattr(_delivery, "ANNOUNCE_MAX_SECONDS", 90.0))

# What app.py mints: ann-<ext>-<uuid4 hex>. The ext is bound to the CALLER'S ext
# rather than left as a wildcard, so a row claiming ext 20 cannot play ext 19's
# clip. 32 lowercase hex characters exactly — uuid4().hex, nothing looser.
_EXT_RE = re.compile(r"[0-9]{2,6}")
_HEX32 = "[0-9a-f]{32}"


def clip_seconds(path: str) -> float | None:
    """Approximate clip duration from the rendered WAV's size, or None if it
    cannot be measured. A byte count is enough to catch a runaway announcement
    and needs no audio library."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    # 44-byte canonical WAV header; ignore anything smaller than that.
    return max(0.0, (size - 44)) / ANNOUNCE_BYTES_PER_SECOND


def retry_clip_path(ext: str, sound: str) -> str:
    """The clip a RETRY may play for `ext`, as ami.announce_to_ext wants it, or "".

    ★ THE RETURN VALUE IS THE EXTENSIONLESS FULL PATH, NOT THE BASENAME, and that
    is not cosmetic. app.py hands the Originate `path[:-4]` — the full path
    without ".wav" — while the LEDGER stores only os.path.basename(sound). A
    retry that passed the bare name would set SW_ANN_FILE to something Asterisk
    cannot resolve, and the failure is silent in the worst way: the call still
    auto-answers, Playback finds nothing, the handset hangs up, txcount stays
    under callqos's delivered floor, so NO audio-delivered row is written and the
    attempt is burned on a call that played nothing. (The same threshold is why a
    wrong path cannot forge a false success either.)

    "" on anything unproven, because this name arrived from a ledger in
    group-writable /share:
      * the strict `ann-<ext>-<32 hex>` shape, with <ext> bound to the row's own
        ext — so no traversal, no absolute path, no other room's clip;
      * realpath containment inside ANNOUNCE_DIR;
      * lstat says a REGULAR FILE — never a symlink, whose target could be
        anything on the filesystem readable by Asterisk;
      * non-zero size;
      * re-measured duration within ANNOUNCE_MAX_SECONDS, because the clip is
        being admitted a second time and the cap is the handset's protection.
    """
    ext = str(ext or "")
    if not _EXT_RE.fullmatch(ext):
        return ""
    name = str(sound or "")
    if not re.fullmatch("ann-" + re.escape(ext) + "-" + _HEX32, name):
        return ""
    try:
        base = os.path.realpath(ANNOUNCE_DIR)
        path = os.path.join(base, name + ".wav")
        if not os.path.realpath(path).startswith(base + os.sep):
            return ""
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            return ""
    except OSError:
        return ""
    secs = clip_seconds(path)
    if secs is None or secs > ANNOUNCE_MAX_SECONDS:
        return ""
    return path[:-4]
