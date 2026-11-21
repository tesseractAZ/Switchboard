"""Shared AGI speech plumbing for the newer Switchboard voice flows (dial-a-status,
phone->speaker announce, smart wake-up delivery). Same shape as the per-AGI helpers
in switchboard-automation.agi, factored out so these flows don't each re-implement
it. I/O is against Asterisk's AGI stdin/stdout; say()/listen() shell out to
switchboard-tts / switchboard-stt. Nothing here raises into the channel — a TTS or
STT failure degrades to a canned prompt or ''.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import wave

# Which recording each prompt plays, built-in or custom (v0.120.0).
sys.path.insert(0, "/usr/share/switchboard/prompts")
try:
    from prompt_ref import prompt  # noqa: E402  (one answer for every prompt played)
except Exception:  # noqa: BLE001  (the built-in prompts, if the resolver cannot load)
    def prompt(name: str) -> str:
        return "switchboard/" + name

STT = "/usr/bin/switchboard-stt"
TTS = "/usr/bin/switchboard-tts"
ASR_DIR = "/run/switchboard/asr"
FEATURES = "/run/switchboard/features.json"

# RECORD's leading-silence remainder. With s=N, res_agi stops once N s of
# consecutive frames have stayed under the DSP silence threshold, then rewinds
# N-1 s, so a recording in which nothing ever crossed the threshold keeps only
# the last 1.02 s (16364 bytes) WHATEVER N is; any speech adds its own length.
SILENT_REMAINDER_S = (0.95, 1.1)

PROMPT_BEEP = prompt("sw-beep")
PROMPT_ONEMOMENT = prompt("sw-onemoment")
PROMPT_GOODBYE = prompt("sw-goodbye")

_seq = 0


def read_env() -> dict:
    env: dict[str, str] = {}
    while True:
        line = sys.stdin.readline()
        if not line or not line.strip():
            break
        if ":" in line:
            k, v = line.split(":", 1)
            env[k.strip()] = v.strip()
    return env


def agi(command: str) -> str:
    sys.stdout.write(command + "\n")
    sys.stdout.flush()
    return sys.stdin.readline().strip()


def stream(soundfile: str) -> None:
    agi(f'STREAM FILE {soundfile} ""')


def log(tag: str, msg: str) -> None:
    sys.stderr.write(f"[{tag}] {msg}\n")


def load_features() -> dict:
    """The staged /run/switchboard/features.json (asterisk-readable), or {}."""
    try:
        with open(FEATURES) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def channel_ext(env: dict) -> str:
    """The extension this AGI is running against, from the AGI environment.

    ``agi_channel`` looks like ``PJSIP/12-0000000a`` — the technology, the
    endpoint (our room extension), then Asterisk's per-channel suffix. Returns
    "" when the channel isn't a recognizable PJSIP endpoint (Local/…, a bare
    name), so callers fall back rather than guess."""
    chan = str(env.get("agi_channel", "") or "")
    if "/" not in chan:
        return ""
    endpoint = chan.split("/", 1)[1].rsplit("-", 1)[0]
    return endpoint if endpoint.isdigit() else ""


def wakeup_scene_for(wk: dict, ext: str) -> str:
    """The HA scene to fire for a wake-up delivered to `ext`.

    Per-room scene (``wakeup_scenes``) wins; the single ``wakeup_scene`` is the
    fallback for rooms with no entry of their own — so adding per-room scenes
    never silently drops the whole-house behavior an install already had."""
    scenes = wk.get("scenes") or {}
    if isinstance(scenes, dict) and ext:
        per_room = scenes.get(str(ext), "")
        if per_room:
            return str(per_room)
    return str(wk.get("scene", "") or "")


def transcripts_allowed() -> bool:
    """May what a caller said reach this process's stderr, the add-on journal?

    The same policy as switchboard-stt's transcripts_allowed() and the assistant
    AGI's _journal_words: the `assistant_transcripts` option, and FAILS CLOSED —
    only a policy that was read and says true lets the words through, because
    the journal is served to anyone with log access and kept about two days."""
    section = load_features().get("assistant")
    return isinstance(section, dict) and section.get("transcripts") is True


def said(text: str, words: bool) -> str:
    """What a log line may say about a text: the words, or only their length.
    The assistant AGI and switchboard-stt keep twins."""
    return repr(text) if words else f"<{len(text or '')} chars>"


def say(text: str, prefix: str = "sw-voice") -> bool:
    """Synthesize text with switchboard-tts and Playback it. True on success;
    never raises (caller falls back to a canned prompt).

    A failure names the text only when transcripts are on (v0.126.0): the
    announce flow speaks the caller's own words back ("Announcing on the
    speakers: …"), and this line cannot tell those from a room name. The policy
    is read on the failure path only, so a spoken reply costs no file read."""
    global _seq
    _seq += 1
    base = f"/tmp/{prefix}-{os.getpid()}-{_seq}"
    wav = base + ".wav"
    try:
        # Not captured (v0.122.0): switchboard-tts's stderr, which never names the
        # text, is this process's, the journal, so its lines land even if a
        # hangup ends this AGI first. Its stdout would be the AGI channel.
        proc = subprocess.run([TTS, text, base], stdout=subprocess.DEVNULL, timeout=20)
        if proc.returncode != 0 or not os.path.exists(wav):
            log("speech", f"tts failed rc={proc.returncode} for "
                          f"{said(text, transcripts_allowed())}")
            return False
        agi(f"EXEC Playback {base}")  # Playback wants the path WITHOUT extension
        return True
    except (subprocess.TimeoutExpired, OSError) as exc:
        # A TimeoutExpired renders its whole command line, and the text is an
        # argument on it, so with the words withheld only the error's type goes.
        words = transcripts_allowed()
        log("speech", f"tts error for {said(text, words)}: "
                      f"{exc if words else type(exc).__name__}")
        return False
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass


def say_or(text: str, fallback_prompt: str = PROMPT_GOODBYE) -> None:
    """Speak dynamic text; on TTS failure play a canned prompt so there's no dead air."""
    if not say(text):
        stream(fallback_prompt)


def _stt_start(cmd: list[str]):
    """Spawn switchboard-stt WITHOUT waiting (Popen or None). The child reads its
    input from --in (stdin=DEVNULL) and never touches the AGI stdin/stdout, so it
    runs CONCURRENTLY with a blocking STREAM FILE. Pair with _stt_finish()."""
    try:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True)
    except OSError as exc:
        log("speech", f"stt spawn error: {exc}")
        return None


def _stt_finish(proc) -> str:
    """Collect a _stt_start() process; return stdout (stripped), '' on any error."""
    if proc is None:
        return ""
    try:
        out, err = proc.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate()
        except OSError:
            pass
        log("speech", "stt timeout")
        return ""
    except OSError as exc:
        try:
            proc.kill()
            proc.communicate()
        except OSError:
            pass
        log("speech", f"stt error: {exc}")
        return ""
    if err:
        sys.stderr.write(err)
    return (out or "").strip()


def rec_summary(reply: str, wav: str) -> str:
    """The `bytes=B secs=S end=E [no-speech]` part of a rec breadcrumb.

    `secs` comes from the WAV header, else from the size (8 kHz 16-bit mono:
    16000 B/s behind a 44-byte header). `end` is the bracketed word in
    Asterisk's RECORD reply, whichever it is: "timeout" for both the silence
    cutoff and the record_ms cap (secs tells them apart), "hangup", "dtmf", or
    a failure such as "waitfor" or "writefile". A reply without one (an empty
    read) logs no `end`. `no-speech` marks a file that is only the silence
    remainder: nothing crossed the silence threshold, which a silent caller and
    a muted or dead microphone produce alike. Only the silence cutoff leaves
    that remainder, so no reply but "timeout" earns the flag, whatever the
    size. switchboard-operator.agi keeps a twin."""
    # Letters only: Asterisk's own reason word, never free text in the journal.
    m = re.search(r"\(([a-z]+)\)", reply or "")
    end = m.group(1) if m else ""
    tail = f" end={end}" if end else ""
    try:
        size = os.path.getsize(wav)
    except OSError:
        return "bytes=-1" + tail          # RECORD wrote nothing
    secs = None
    try:
        with wave.open(wav, "rb") as w:
            if w.getframerate() > 0 and w.getnframes() > 0:
                secs = w.getnframes() / w.getframerate()
    except Exception:  # noqa: BLE001  (a breadcrumb must never end the call)
        pass           # a short or odd header: the size below still answers
    if secs is None:
        secs = max(size - 44, 0) / 16000
    out = f"bytes={size} secs={secs:.2f}{tail}"
    lo, hi = SILENT_REMAINDER_S
    if end == "timeout" and lo <= secs <= hi:
        out += " no-speech"
    return out


def listen(tag: str, attempt: int = 0, record_ms: int = 6000, silence_s: int = 2,
           bias: str = "") -> str:
    """Record one utterance (beep -> record -> 'one moment') and return the
    transcript ('' on nothing / STT failure). The transcription runs CONCURRENTLY
    with the 'one moment' prompt — started just before it, collected just after —
    so the recognition latency hides behind audio instead of being added after.

    Each recording leaves a `[<tag>] rec` line (see rec_summary) before STT
    runs: the WAV is deleted afterwards, and an empty transcript alone cannot
    tell a caller who said nothing from one whose words STT missed. A hang-up
    while recording usually leaves no line at all rather than end=hangup:
    Asterisk SIGHUPs the AGI once it has replied, and no caller handles it."""
    rec = f"{ASR_DIR}/{tag}-{os.getpid()}-{attempt}"
    stream(PROMPT_BEEP)
    reply = agi(f'RECORD FILE {rec} wav "" {record_ms} s={silence_s}')
    wav = rec + ".wav"
    log(tag, f"rec attempt={attempt} {rec_summary(reply, wav)}")
    cmd = [STT, "--in", wav, "--mode", "transcribe"]
    if bias:
        cmd += ["--bias", bias]
    proc = _stt_start(cmd)       # start whisper BEFORE the prompt
    stream(PROMPT_ONEMOMENT)     # ~0.9 s of audio overlaps the inference
    try:
        return _stt_finish(proc)
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
