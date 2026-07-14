"""Build the phone-handset announcement audio for Asterisk Playback: a station
chime, the spoken message, then the chime again, as ONE 8 kHz mono 16-bit WAV.

Sibling of announce_audio.py. announce_audio builds the 22050 Hz WAV that HA media
players fetch over HTTP (play_media handles that rate). THIS module builds the
*Asterisk Playback* variant at 8 kHz — the only sample rate Asterisk's format_wav /
Playback reads for a plain ".wav" (a 22050 file plays at the wrong speed on the
FXS/ulaw path). Used by the announce-to-handset endpoint so an HA/ecoflow alert can
speak out the cordless speaker (the SIP equivalent of a media_player announcement).

Dependency-free and Python-3.14-safe on purpose: espeak-ng renders the speech (in
the image, native ~22050); the chime is generated NATIVELY at 8 kHz (announce_audio's
synth_chime/write_combined are rate-parametric); only the espeak speech is resampled
22050 -> 8000 in pure Python. We deliberately avoid sox/ffmpeg (not installed) and the
stdlib ``audioop`` module (removed in CPython 3.13; the image runs 3.14).
build_announcement_8k / render_url_to_8k shell to espeak / fetch over HTTP;
_resample_to_8k and _box_filter are pure and unit-tested with plain python3.
"""

from __future__ import annotations

import array
import io
import ipaddress
import os
import socket
import subprocess
import urllib.parse
import urllib.request
import wave

import announce_audio  # sibling module; provides RATE, synth_chime, write_combined

TARGET_RATE = 8000  # Asterisk format_wav / Playback native rate for a plain .wav


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx must NOT pivot the URL fetch onto an internal host (SSRF), so we refuse
    to follow redirects — the caller's URL is fetched or nothing is."""

    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _host_is_safe(host: str) -> bool:
    """False if `host` resolves to a loopback / link-local / multicast / reserved /
    unspecified address — blocked so the {url} branch can't reach host-loopback
    services or odd ranges from this host-network add-on. Private LAN IS allowed:
    the only legitimate producer is HA's own TTS, whose URL is the Core host on the
    private LAN / Supervisor network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return False
    return bool(infos)


# --------------------------------------------------------------------------- #
# Pure DSP helpers (no I/O) — unit-tested directly.
# --------------------------------------------------------------------------- #
def _box_filter(samples: "array.array", width: int) -> "array.array":
    """A tiny centered moving-average low-pass over int16 samples.

    Applied before decimation as a cheap anti-alias pre-filter: without it,
    downsampling 22050 -> 8000 folds espeak's >4 kHz energy back into the band as
    hiss. ``width`` tracks the decimation factor (~3 here). O(n*width), width ~3, so
    negligible for a few seconds of speech. Pure."""
    if width <= 1 or not samples:
        return array.array("h", samples)
    n = len(samples)
    out = array.array("h", bytes(n * 2))
    half = width // 2
    for i in range(n):
        s = 0
        cnt = 0
        for k in range(i - half, i - half + width):
            if 0 <= k < n:
                s += samples[k]
                cnt += 1
        out[i] = max(-32768, min(32767, int(s / cnt)))
    return out


def _resample_to_8k(samples: "array.array", in_rate: int,
                    out_rate: int = TARGET_RATE) -> "array.array":
    """Resample a mono int16 sample array to ``out_rate`` by linear interpolation.

    22050 -> 8000 is a non-integer ratio (~2.756), so we can't just decimate. On a
    downsample we first run a box-filter anti-alias pass, then linearly interpolate
    at the output sample positions. Values are clamped to int16. Pure — the ratio
    math and length are unit-tested without touching audio hardware."""
    if not samples or in_rate == out_rate:
        return array.array("h", samples)
    src = samples
    if out_rate < in_rate:
        width = max(1, round(in_rate / out_rate))
        src = _box_filter(samples, width)
    n_in = len(src)
    n_out = max(1, round(n_in * out_rate / in_rate))
    out = array.array("h", bytes(n_out * 2))
    step = (n_in - 1) / (n_out - 1) if n_out > 1 else 0.0
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        if j + 1 < n_in:
            v = src[j] * (1.0 - frac) + src[j + 1] * frac
        else:
            v = src[j]
        out[i] = max(-32768, min(32767, int(round(v))))
    return out


def _read_wav_mono16(reader) -> tuple["array.array | None", int]:
    """Read a wave reader into a mono int16 sample array + its sample rate.

    Returns (samples, rate) or (None, rate) if the WAV isn't 16-bit. Stereo is
    downmixed to mono defensively (espeak is mono, but a fetched TTS clip may be
    stereo). Pure given the reader."""
    rate = reader.getframerate()
    if reader.getsampwidth() != 2:
        return None, rate
    frames = reader.readframes(reader.getnframes())
    samples = array.array("h", frames)
    ch = reader.getnchannels()
    if ch == 2:
        samples = array.array(
            "h",
            [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)],
        )
    elif ch != 1:
        return None, rate
    return samples, rate


# --------------------------------------------------------------------------- #
# Builders (shell to espeak / fetch over HTTP).
# --------------------------------------------------------------------------- #
def build_announcement_8k(text: str, out_path: str,
                          voice: str = "en-us", speed: int = 150) -> bool:
    """chime + espeak(text) + chime -> out_path as an 8 kHz mono 16-bit WAV.

    True on success. The message is a subprocess ARG (no shell), so it can't inject
    a command. The chime is generated natively at 8 kHz by write_combined; only the
    espeak speech is resampled. out_path's dir must exist and be readable by the
    asterisk user (see the announce endpoint / ANNOUNCE_DIR)."""
    tmp = out_path + ".speech.wav"
    speech: "array.array | None" = None
    in_rate = announce_audio.RATE
    try:
        subprocess.run(
            ["espeak-ng", "-v", voice, "-s", str(speed), "-w", tmp, text],
            check=True, capture_output=True, timeout=20,
        )
        with wave.open(tmp, "rb") as w:
            speech, in_rate = _read_wav_mono16(w)
    except (subprocess.SubprocessError, OSError, wave.Error):
        speech = None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if speech is None:
        return False
    speech8k = _resample_to_8k(speech, in_rate, TARGET_RATE)
    try:
        announce_audio.write_combined(out_path, speech8k, TARGET_RATE)
        return True
    except (OSError, wave.Error):
        return False


def render_url_to_8k(url: str, out_path: str,
                     max_bytes: int = 5_000_000, timeout: int = 15) -> bool:
    """Fetch a WAV over http(s), transcode to 8 kHz mono 16-bit, wrap with chimes.

    Used by the {url} branch of the announce endpoint (e.g. an HA TTS clip). Only WAV
    is decodable here — the image has NO ffmpeg — so the caller should hand us a WAV
    (what tts.piper produces) or use the self-contained {text} branch. Scheme is
    restricted to http/https and the body is size-capped (defence-in-depth even
    though the endpoint is token/loopback gated)."""
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    host = urllib.parse.urlsplit(url).hostname or ""
    if not host or not _host_is_safe(host):
        return False
    speech: "array.array | None" = None
    in_rate = TARGET_RATE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Switchboard/announce"})
        # _OPENER refuses redirects (no 3xx pivot to an internal host).
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 (scheme + host checked)
            raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return False
        with wave.open(io.BytesIO(raw), "rb") as w:
            speech, in_rate = _read_wav_mono16(w)
    except (OSError, ValueError, wave.Error):
        return False
    if speech is None:
        return False
    speech8k = _resample_to_8k(speech, in_rate, TARGET_RATE)
    try:
        announce_audio.write_combined(out_path, speech8k, TARGET_RATE)
        return True
    except (OSError, wave.Error):
        return False
