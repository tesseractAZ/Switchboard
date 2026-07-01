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
import subprocess
import sys

STT = "/usr/bin/switchboard-stt"
TTS = "/usr/bin/switchboard-tts"
ASR_DIR = "/run/switchboard/asr"
FEATURES = "/run/switchboard/features.json"

PROMPT_BEEP = "switchboard/sw-beep"
PROMPT_ONEMOMENT = "switchboard/sw-onemoment"
PROMPT_GOODBYE = "switchboard/sw-goodbye"

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


def say(text: str, prefix: str = "sw-voice") -> bool:
    """Synthesize text with switchboard-tts and Playback it. True on success;
    never raises (caller falls back to a canned prompt)."""
    global _seq
    _seq += 1
    base = f"/tmp/{prefix}-{os.getpid()}-{_seq}"
    wav = base + ".wav"
    try:
        proc = subprocess.run([TTS, text, base], capture_output=True, text=True, timeout=20)
        if proc.returncode != 0 or not os.path.exists(wav):
            log("speech", f"tts failed rc={proc.returncode} for {text[:40]!r}")
            return False
        agi(f"EXEC Playback {base}")  # Playback wants the path WITHOUT extension
        return True
    except (subprocess.TimeoutExpired, OSError) as exc:
        log("speech", f"tts error: {exc}")
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


def listen(tag: str, attempt: int = 0, record_ms: int = 6000, silence_s: int = 2,
           bias: str = "") -> str:
    """Record one utterance (beep -> record -> 'one moment') and return the
    transcript ('' on nothing / STT failure)."""
    rec = f"{ASR_DIR}/{tag}-{os.getpid()}-{attempt}"
    stream(PROMPT_BEEP)
    agi(f'RECORD FILE {rec} wav "" {record_ms} s={silence_s}')
    stream(PROMPT_ONEMOMENT)
    wav = rec + ".wav"
    text = ""
    try:
        cmd = [STT, "--in", wav, "--mode", "transcribe"]
        if bias:
            cmd += ["--bias", bias]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        text = (proc.stdout or "").strip()
        if proc.stderr:
            sys.stderr.write(proc.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log("speech", f"stt error: {exc}")
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    return text
