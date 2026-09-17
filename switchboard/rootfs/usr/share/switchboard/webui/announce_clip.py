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
import stat

# tmpfs under /run: clips are ephemeral by design and deliberately never durable
# — they are rendered household speech, and this add-on does not persist that.
# The same path inside the same container for the webui and the scheduler.
ANNOUNCE_DIR = "/run/switchboard/announce"

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
