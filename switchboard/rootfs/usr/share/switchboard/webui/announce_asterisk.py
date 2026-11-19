"""Build the phone-handset announcement audio for Asterisk Playback: a station
chime, the spoken message, then the chime again, as ONE 8 kHz mono 16-bit WAV.

Sibling of announce_audio.py. announce_audio builds the 22050 Hz WAV that HA media
players fetch over HTTP (play_media handles that rate). THIS module builds the
*Asterisk Playback* variant at 8 kHz — the only sample rate Asterisk's format_wav /
Playback reads for a plain ".wav" (a 22050 file plays at the wrong speed on the
FXS/ulaw path). Used by the announce-to-handset endpoint so an HA/ecoflow alert can
speak out the cordless speaker (the SIP equivalent of a media_player announcement).

espeak-ng renders the {text} speech (in the image, native ~22050); a {url} clip is a
WAV (tts.piper, 22050) decoded in pure Python or another format (the MP3 HA's
tts_proxy serves) decoded by ffmpeg, which the image installs best-effort. The chime
is generated NATIVELY at 8 kHz (announce_audio's synth_chime/write_combined are
rate-parametric) and is never processed.

Every phone here runs G.711 µ-law, so the speech reaches the earpiece as 8 kHz
narrowband (300-3400 Hz) whatever rate it was rendered at. Since v0.121.0 the speech
of every branch is MASTERED for that band (see "Phone-band mastering" below): one
ffmpeg process shapes and resamples it, then a per-clip gain and a peak limiter set
its level in pure Python. When ffmpeg is missing, fails, times out or returns
something unusable, the speech takes the pre-0.121 path unchanged: the pure-Python
resample (_box_filter + _resample_to_8k, kept so this module needs no audio library:
the image runs Python 3.12, and the stdlib ``audioop`` is gone from 3.13), and for a
non-WAV {url} the plain ffmpeg decode. That fallback is logged once per process.
build_announcement_8k / render_url_to_8k shell to espeak / ffmpeg / fetch over HTTP;
the DSP helpers are pure and unit-tested with plain python3.
"""

from __future__ import annotations

import array
import io
import ipaddress
import math
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import wave

import announce_audio  # sibling module; provides RATE, synth_chime, write_combined

TARGET_RATE = 8000  # Asterisk format_wav / Playback native rate for a plain .wav

# --------------------------------------------------------------------------- #
# Phone-band mastering (v0.121.0) — the chain used to master recorded prompts for
# these phones. Against 300-1000 Hz it lifts 2-3.4 kHz, where the consonants are.
#
#   2 x highpass 250 Hz      4th order: what the earpieces do not reproduce only
#                            muddies the band and spends headroom
#   equalizer 500 Hz, -2 dB  boxiness
#   equalizer 2.8 kHz, +5 dB consonants
#   2 x lowpass 3.5 kHz      keeps the energy inside the band
#   acompressor 3:1 above -24 dBFS
#   aresample 8000           ffmpeg's windowed-sinc resampler: unlike the box
#                            filter, nothing above 4 kHz folds back into the band
#
# The aformat stage makes the biquads run in double precision (from 16-bit input
# they would otherwise run, and clip, in 16-bit), and the output is 32-bit float
# so the +5 dB lift cannot clip before the gain below sets the level.
# --------------------------------------------------------------------------- #
MASTER_FILTERS = ",".join((
    "aformat=sample_fmts=dblp:channel_layouts=mono",
    "highpass=f=250:poles=2",
    "highpass=f=250:poles=2",
    "equalizer=f=500:t=q:w=1:g=-2",
    "equalizer=f=2800:t=q:w=0.9:g=5",
    "lowpass=f=3500:poles=2",
    "lowpass=f=3500:poles=2",
    "acompressor=threshold=-24dB:ratio=3:attack=5:release=80:makeup=1",
    f"aresample={TARGET_RATE}",
))
# A ceiling on the one mastering process. Mastering a clip takes a small fraction
# of its length, so this only bounds a stalled ffmpeg: the clip then takes the
# pre-0.121 path, at most this much later than it would have.
MASTER_TIMEOUT_S = 10
# After a master that runs into that ceiling, mastering is skipped for this
# long, so a wedged ffmpeg costs one announcement the wait, not every alarm
# after it; then it is tried again. Failures that cost no time pause nothing.
MASTER_PAUSE_S = 600
_PAUSED = "mastering paused after an ffmpeg timeout"
# Speech longer than this is not mastered: the announce endpoint refuses any clip
# over 90 seconds, so the work would be thrown away, and the limiter's cost grows
# with the clip. Such a clip takes the pre-0.121 path, without a log line.
MASTER_MAX_SECONDS = 120
_TOO_LONG = "clip too long to master"
# The level every mastered clip is set to: the speech level of the built-in
# prompts, measured the same way (RMS over the 20 ms frames above the gate).
SPEECH_TARGET_DBFS = -15.0
SPEECH_GATE_DBFS = -30.0
SPEECH_FRAME_MS = 20
# The most a clip is ever raised, so a near-silent one is not blown up into noise.
MAX_GAIN_DB = 20.0
# Peak limiter: -1 dBFS ceiling, 2 ms look-ahead attack, 40 ms release.
LIMIT_CEILING = 0.891
LIMIT_ATTACK_MS = 2.0
LIMIT_RELEASE_MS = 40.0
# Full scale of the float samples, as ffmpeg converts 16-bit PCM (x / 32768).
_FS = 32768.0
# No sound the chain can make from real audio comes near this (the largest lift
# is +5 dB); a float stream that does is not audio.
_MASTER_PEAK_SANITY = 8.0


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


def _frame_powers(samples, rate: int = TARGET_RATE) -> list:
    """The mean square of each whole SPEECH_FRAME_MS frame of `samples` (floats,
    full scale 1.0); a clip shorter than one frame has none. Pure."""
    n = max(1, rate * SPEECH_FRAME_MS // 1000)
    out = []
    for i in range(0, len(samples) - n + 1, n):
        frame = samples[i:i + n]
        out.append(math.sumprod(frame, frame) / n)
    return out


def _gated_level_dbfs(powers: list, gain_db: float = 0.0) -> "float | None":
    """The speech level, in dB re full scale, of a clip with these frame powers
    once it is raised by `gain_db`: the RMS over the frames that are then above
    SPEECH_GATE_DBFS, so pauses and silence do not pull it down. None when no
    frame is. Pure."""
    k = 10.0 ** (gain_db / 10.0)
    gate = 10.0 ** (SPEECH_GATE_DBFS / 10.0)
    voiced = [p * k for p in powers if p * k > gate]
    if not voiced:
        return None
    return 10.0 * math.log10(math.fsum(voiced) / len(voiced))


def _speech_level_dbfs(samples, rate: int = TARGET_RATE) -> "float | None":
    """A clip's speech level as it stands (see _gated_level_dbfs). Pure."""
    return _gated_level_dbfs(_frame_powers(samples, rate))


def _clip_gain_db(powers: list) -> float:
    """The gain that puts a clip's speech level, measured AFTER the gain (as the
    built-in prompts' level is measured on the files that play), at
    SPEECH_TARGET_DBFS, never more than MAX_GAIN_DB. Pure.

    Raising a clip lifts quiet frames over the gate, which lowers the level the
    gate measures, so the gain is a fixed point: the level of the loudest j
    frames asks for a gain at which exactly those j frames are over the gate.
    Taking the frames loudest first, the first j for which that holds gives the
    smallest such gain, exactly, with no iteration to stop short. Each further
    frame only asks for more gain, so the cap is reached as soon as any j asks
    for it. The gain depends on the frames only relative to each other: a
    copy of a clip N dB quieter gets N dB more, up to the cap, so the level
    never falls off a step as a clip gets quieter. Only a clip with no sound at
    all (or shorter than one frame) gets 0 dB."""
    gate = 10.0 ** (SPEECH_GATE_DBFS / 10.0)
    loud = sorted((p for p in powers if p > 0.0), reverse=True)
    total = 0.0
    for j, p in enumerate(loud, 1):
        total += p
        gain = SPEECH_TARGET_DBFS - 10.0 * math.log10(total / j)
        if gain >= MAX_GAIN_DB:
            return MAX_GAIN_DB
        if j == len(loud) or loud[j] * 10.0 ** (gain / 10.0) <= gate:
            return gain
    return 0.0


def _limit_to_int16(samples, gain: float, rate: int = TARGET_RATE,
                    ceiling: float = LIMIT_CEILING) -> "array.array":
    """`samples` (floats, full scale 1.0) times `gain`, peak-limited to `ceiling`,
    as int16. Pure and deterministic.

    A look-ahead peak limiter computed over the whole clip, so the look-ahead
    needs no delay line. Each sample's own limit is the gain that puts it at the
    ceiling; a forward pass lets the gain recover from a peak over
    LIMIT_RELEASE_MS, and a backward pass starts its fall LIMIT_ATTACK_MS before
    the peak arrives. Both passes only ever take the gain DOWN to a sample's own
    limit, never above it, so no output sample exceeds the ceiling. A clip whose
    peaks stay under the ceiling gets the plain gain."""
    n = len(samples)
    scale = gain * _FS
    if not n:
        return array.array("h")
    if max(max(samples), -min(samples)) * gain <= ceiling:
        return array.array("h", [round(v * scale) for v in samples])
    lim = ceiling / gain  # a |sample| above this needs its gain reduced
    rel = math.exp(-1000.0 / (LIMIT_RELEASE_MS * rate))
    att = math.exp(-1000.0 / (LIMIT_ATTACK_MS * rate))
    env = array.array("d", [1.0]) * n  # 8 bytes a sample, not a float object each
    g = 1.0
    for i, v in enumerate(samples):
        g = 1.0 - (1.0 - g) * rel
        a = v if v >= 0.0 else -v
        if a > lim and lim / a < g:
            g = lim / a
        env[i] = g
    out = array.array("h", bytes(2 * n))
    g = 1.0
    for i in range(n - 1, -1, -1):
        g = 1.0 - (1.0 - g) * att
        if env[i] < g:
            g = env[i]
        out[i] = round(samples[i] * g * scale)
    return out


def _ffmpeg_to_8k_wav(raw: bytes) -> "bytes | None":
    """Transcode arbitrary audio bytes to 8 kHz mono 16-bit PCM WAV via ffmpeg (reads
    stdin, writes WAV to stdout — no temp files), or None if ffmpeg is absent/fails.
    Used when the fetched clip isn't a plain WAV (HA's tts_proxy serves MP3)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-ar", str(TARGET_RATE), "-ac", "1", "-acodec", "pcm_s16le", "-f", "wav", "pipe:1"],
            input=raw, capture_output=True, timeout=20,
        )
        if proc.returncode == 0 and proc.stdout[:4] == b"RIFF":
            return proc.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _master_argv(rate: "int | None") -> list:
    """The one ffmpeg process that masters a clip, stdin to stdout (no shell, no
    temp file). With `rate`, stdin is raw mono 16-bit PCM at that rate (speech
    already decoded here); with None, an encoded clip ffmpeg decodes itself, so a
    non-WAV {url} is decoded and mastered by the same process. Out: mono 32-bit
    float at TARGET_RATE, cut at MASTER_MAX_SECONDS (an encoded clip's length is
    known only once it is decoded)."""
    argv = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error"]
    if rate is not None:
        argv += ["-f", "s16le", "-ar", str(rate), "-ac", "1"]
    return argv + ["-i", "pipe:0", "-vn", "-af", MASTER_FILTERS,
                   "-ac", "1", "-ar", str(TARGET_RATE), "-t", str(MASTER_MAX_SECONDS),
                   "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]


_master_paused_until = 0.0  # time.monotonic(); see MASTER_PAUSE_S


def _ffmpeg_master(payload: bytes, rate: "int | None" = None,
                   n_in: "int | None" = None) -> tuple["array.array | None", str]:
    """Run MASTER_FILTERS over `payload` (see _master_argv). `n_in` is the number
    of input samples when it is PCM, so the output length can be checked.

    Returns (float samples at TARGET_RATE, "") or (None, reason), where the
    reason is a fixed phrase that names nothing about the clip. Output is refused
    unless ffmpeg exited 0 and wrote whole float samples, of the length the input
    implies, finite, not all zero, and within a range real audio can reach. Output
    that reaches the MASTER_MAX_SECONDS cut is (None, _TOO_LONG). For
    MASTER_PAUSE_S after a timeout, ffmpeg is not run: (None, _PAUSED)."""
    global _master_paused_until
    if time.monotonic() < _master_paused_until:
        return None, _PAUSED
    try:
        proc = subprocess.run(_master_argv(rate), input=payload,
                              capture_output=True, timeout=MASTER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _master_paused_until = time.monotonic() + MASTER_PAUSE_S
        return None, "ffmpeg timed out"
    except FileNotFoundError:
        return None, "ffmpeg not found"
    except (subprocess.SubprocessError, OSError):
        return None, "ffmpeg could not run"
    if proc.returncode != 0:
        return None, f"ffmpeg exited {proc.returncode}"
    bad = None, "ffmpeg returned unusable audio"
    if len(proc.stdout) % 4:
        return bad
    y = array.array("f", proc.stdout)
    if sys.byteorder == "big":
        y.byteswap()  # pcm_f32le
    if n_in is not None and rate:
        want = n_in * TARGET_RATE / rate
        if abs(len(y) - want) > max(8.0, want / 100):
            return bad
    if len(y) >= (MASTER_MAX_SECONDS - 0.01) * TARGET_RATE:
        return None, _TOO_LONG  # stopped at the cut: longer than that
    # Empty, all zero or NaN anywhere fails "> 0"; an infinity, or any value
    # real audio cannot reach, fails the range check.
    if not math.sumprod(y, y) > 0.0:
        return bad
    if max(max(y), -min(y)) > _MASTER_PEAK_SANITY:
        return bad
    return y, ""


def _master_speech(payload: bytes, rate: "int | None" = None,
                   n_in: "int | None" = None) -> tuple["array.array | None", str]:
    """The mastered speech as 8 kHz int16 — ffmpeg's chain, then the per-clip gain
    and the limiter — and "", or (None, reason). Never raises: an announcement
    must not be lost to the mastering, whatever goes wrong in it."""
    try:
        y, reason = _ffmpeg_master(payload, rate, n_in)
        if y is None:
            return None, reason
        gain_db = _clip_gain_db(_frame_powers(y, TARGET_RATE))
        return _limit_to_int16(y, 10.0 ** (gain_db / 20.0), TARGET_RATE), ""
    except Exception as exc:  # noqa: BLE001 — fall back rather than lose the clip
        return None, f"mastering error {type(exc).__name__}"


_UNMASTERED_LOCK = threading.Lock()
_unmastered_logged = False


def _note_unmastered(reason: str) -> None:
    """Log, ONCE per process, that a clip was rendered without the mastering. One
    line per announcement would bury the log when ffmpeg is simply absent; the
    reason is a fixed phrase, never the message, the URL or its host."""
    global _unmastered_logged
    with _UNMASTERED_LOCK:
        if _unmastered_logged:
            return
        _unmastered_logged = True
    try:
        print(f"[switchboard-webui] announce: phone-band mastering failed ({reason}), "
              "so the clip was rendered unmastered; later fallbacks are not logged",
              flush=True)
    except (OSError, ValueError):  # a log that cannot be written must not cost the clip
        pass


def _nothing_to_master(samples, rate: int) -> bool:
    """True for speech with nothing to master: shorter than one SPEECH_FRAME_MS
    frame (a click, not speech, and ffmpeg's resampler can return nothing at all
    for a few ms), or silent. Pure."""
    return len(samples) * 1000 < SPEECH_FRAME_MS * rate or not any(samples)


def _speech_to_8k(samples: "array.array", in_rate: int) -> "array.array":
    """Decoded mono int16 speech -> 8 kHz int16 for the handset: mastered, or, when
    mastering fails, the unmastered pure-Python resample exactly as before
    v0.121.0. Speech with nothing to master (see _nothing_to_master), and speech
    of MASTER_MAX_SECONDS or more, take that path without a log line, so the
    one line is kept for a real failure."""
    if (_nothing_to_master(samples, in_rate)
            or len(samples) >= MASTER_MAX_SECONDS * in_rate):
        return _resample_to_8k(samples, in_rate, TARGET_RATE)
    pcm = array.array("h", samples)
    if sys.byteorder == "big":
        pcm.byteswap()  # s16le
    mastered, reason = _master_speech(pcm.tobytes(), in_rate, len(samples))
    if mastered is not None:
        return mastered
    speech8k = _resample_to_8k(samples, in_rate, TARGET_RATE)
    if reason != _TOO_LONG:
        _note_unmastered(reason)
    return speech8k


def _fetched_speech_to_8k(raw: bytes) -> "array.array | None":
    """A fetched clip's speech as 8 kHz int16, or None when it cannot be decoded.

    A plain 16-bit WAV is decoded here and goes through _speech_to_8k. Anything
    else is decoded AND mastered by one ffmpeg process; if that fails, the clip
    takes the pre-0.121 path unchanged (_decode_audio_to_mono16: a WAV, else the
    plain ffmpeg decode to 8 kHz). Only that decode shows whether the clip had
    anything to master; a silent or tiny one fails the mastering's output check
    and is not logged."""
    samples, rate = _read_plain_wav(raw)
    if samples is not None:
        return _speech_to_8k(samples, rate)
    mastered, reason = _master_speech(raw)
    if mastered is not None:
        return mastered
    samples, rate = _decode_audio_to_mono16(raw)
    if samples is None:
        return None
    speech8k = _resample_to_8k(samples, rate, TARGET_RATE)
    if reason != _TOO_LONG and not _nothing_to_master(samples, rate):
        _note_unmastered(reason)
    return speech8k


def _read_plain_wav(raw: bytes) -> tuple["array.array | None", int]:
    """(mono int16 samples, rate) when `raw` is a WAV the stdlib reader decodes as
    16-bit mono/stereo PCM, else (None, TARGET_RATE). Never raises on a damaged
    body: the stdlib reader raises wave.Error, EOFError, or RuntimeError for a
    chunk that claims more bytes than its parent holds, and ValueError is caught
    with them. A body it declines, or one with no frame rate, goes to ffmpeg
    like any format this reader does not take."""
    try:
        with wave.open(io.BytesIO(raw), "rb") as w:
            samples, rate = _read_wav_mono16(w)
        if samples is not None and rate > 0:
            return samples, rate
    except (wave.Error, EOFError, OSError, ValueError, RuntimeError):
        pass
    return None, TARGET_RATE


def _decode_audio_to_mono16(raw: bytes) -> tuple["array.array | None", int]:
    """Decode fetched audio bytes to (mono int16 samples, rate). Tries a plain WAV
    first; if that fails (the bytes are MP3/OGG/etc. — HA's tts_proxy serves MP3),
    falls back to ffmpeg, which yields an 8 kHz mono WAV. (None, rate) on failure."""
    samples, rate = _read_plain_wav(raw)
    if samples is not None:
        return samples, rate
    wav = _ffmpeg_to_8k_wav(raw)
    if wav is None:
        return None, TARGET_RATE
    return _read_plain_wav(wav)


def _read_wav_mono16(reader) -> tuple["array.array | None", int]:
    """Read a wave reader into a mono int16 sample array + its sample rate.

    Returns (samples, rate) or (None, rate) if the WAV isn't 16-bit. Stereo is
    downmixed to mono defensively (espeak is mono, but a fetched TTS clip may be
    stereo). A file cut off mid-frame keeps its whole frames. Pure given the
    reader."""
    rate = reader.getframerate()
    if reader.getsampwidth() != 2:
        return None, rate
    ch = reader.getnchannels()
    frames = reader.readframes(reader.getnframes())
    frames = frames[:len(frames) - len(frames) % (2 * ch)]
    samples = array.array("h", frames)
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
    espeak speech is mastered (or, failing that, resampled). out_path's dir must
    exist and be readable by the asterisk user (see the announce endpoint /
    ANNOUNCE_DIR)."""
    tmp = out_path + ".speech.wav"
    speech: "array.array | None" = None
    in_rate = announce_audio.RATE
    try:
        subprocess.run(
            # "--": never read the text as an option, even when it starts with "-".
            ["espeak-ng", "-v", voice, "-s", str(speed), "-w", tmp, "--", text],
            check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=20,
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
    speech8k = _speech_to_8k(speech, in_rate)
    try:
        announce_audio.write_combined(out_path, speech8k, TARGET_RATE)
        return True
    except (OSError, wave.Error):
        return False


def render_url_to_8k(url: str, out_path: str,
                     max_bytes: int = 5_000_000, timeout: int = 15) -> bool:
    """Fetch a clip over http(s), master it to 8 kHz mono 16-bit, wrap with chimes.

    Used by the {url} branch of the announce endpoint (e.g. an HA TTS clip). A WAV is
    decoded in pure Python; a non-WAV body (e.g. an MP3 from HA's tts_proxy) is decoded
    via ffmpeg if it is present (best-effort installed in the image) — otherwise hand
    us a WAV (what tts.piper produces) or use the self-contained {text} branch. Scheme
    is restricted to http/https and the body is size-capped (defence-in-depth even
    though the endpoint is token/loopback gated)."""
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    host = urllib.parse.urlsplit(url).hostname or ""
    if not host or not _host_is_safe(host):
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Switchboard/announce"})
        # _OPENER refuses redirects (no 3xx pivot to an internal host).
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 (scheme + host checked)
            raw = resp.read(max_bytes + 1)
    except (OSError, ValueError):
        return False
    if len(raw) > max_bytes:
        return False
    # WAV directly, else ffmpeg (HA's tts_proxy serves MP3); mastered either way
    # when ffmpeg works.
    speech8k = _fetched_speech_to_8k(raw)
    if speech8k is None:
        return False
    try:
        announce_audio.write_combined(out_path, speech8k, TARGET_RATE)
        return True
    except (OSError, wave.Error):
        return False
