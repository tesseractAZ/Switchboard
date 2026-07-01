"""Build the phone->speaker announcement audio: a station/airport-style chime, the
spoken message (espeak-ng), then the chime again — as ONE WAV so it plays back
seamlessly via media_player.play_media (no cross-file timing races on AirPlay).

The chime is synthesized from sine tones (stdlib only) — a bell-like rising motif,
the "attention please" preamble you hear in European stations. synth_chime and
write_combined are pure + unit-tested; build_announcement shells to espeak-ng.
"""

from __future__ import annotations

import array
import math
import os
import socket
import subprocess
import wave

RATE = 22050  # espeak-ng's native mono rate; the chime is generated to match.


def lan_ip() -> str:
    """The Pi's LAN IP — the address a media player uses to fetch the announcement
    from the webui. '' on failure."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP connect sends nothing; just picks the egress iface
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()

# A three-note bell chime (D5 -> G5 -> D5, a rising perfect-fourth that resolves) —
# the classic pre-announcement "bing bong bong". (freq_hz, seconds).
_CHIME_NOTES = [(587.33, 0.42), (783.99, 0.42), (587.33, 0.75)]


def _tone(freq: float, dur: float, rate: int) -> list:
    n = int(dur * rate)
    out = []
    for i in range(n):
        t = i / rate
        env = math.exp(-3.2 * t)                      # exponential decay -> bell-like
        s = (math.sin(2 * math.pi * freq * t)
             + 0.35 * math.sin(2 * math.pi * 2 * freq * t)
             + 0.12 * math.sin(2 * math.pi * 3 * freq * t))
        if t < 0.006:                                  # tiny attack ramp -> no click
            s *= t / 0.006
        out.append(s * env)
    return out


def synth_chime(rate: int = RATE) -> "array.array":
    """The chime as a mono int16 sample array."""
    samples: list = []
    for freq, dur in _CHIME_NOTES:
        samples += _tone(freq, dur, rate)
    peak = max((abs(x) for x in samples), default=1.0) or 1.0
    scale = 0.72 * 32767 / peak
    return array.array("h", [max(-32768, min(32767, int(x * scale))) for x in samples])


def _silence(sec: float, rate: int) -> "array.array":
    return array.array("h", bytes(int(sec * rate) * 2))


def write_combined(out_path: str, speech: "array.array", rate: int) -> None:
    """Write chime + gap + speech + gap + chime as one mono 16-bit WAV. Pure."""
    chime = synth_chime(rate)
    frames = array.array("h")
    frames += chime
    frames += _silence(0.45, rate)
    frames += speech
    frames += _silence(0.45, rate)
    frames += chime
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())


def build_announcement(text: str, out_path: str, voice: str = "en-us", speed: int = 150) -> bool:
    """chime + espeak(text) + chime -> out_path (WAV). True on success. The message
    is passed as a subprocess arg (no shell), so it can't inject a command."""
    tmp = out_path + ".speech.wav"
    speech = None
    rate = RATE
    try:
        subprocess.run(["espeak-ng", "-v", voice, "-s", str(speed), "-w", tmp, text],
                       check=True, capture_output=True, timeout=20)
        with wave.open(tmp, "rb") as w:
            rate = w.getframerate()
            if w.getnchannels() == 1 and w.getsampwidth() == 2:
                speech = array.array("h", w.readframes(w.getnframes()))
    except (subprocess.SubprocessError, OSError, wave.Error):
        speech = None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if speech is None:
        return False
    try:
        write_combined(out_path, speech, rate)
        return True
    except (OSError, wave.Error):
        return False
