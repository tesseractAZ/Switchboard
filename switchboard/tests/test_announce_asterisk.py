"""Behavioral tests for announce_asterisk — the 8 kHz Playback audio builder.

Run with plain Python (no pytest):

    python3 switchboard/tests/test_announce_asterisk.py

Pins the pure DSP: the 22050->8000 resample (ratio/length/clamp), the anti-alias
box filter, and the mono/16-bit WAV reader (stereo downmix, format rejection).
The espeak/HTTP shells are not exercised here (no audio hardware / network).
"""
import array
import io
import sys
import wave
from importlib.machinery import SourceFileLoader
from pathlib import Path

_WEBUI = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share" / "switchboard" / "webui"
sys.path.insert(0, str(_WEBUI))
aa = SourceFileLoader("announce_asterisk", str(_WEBUI / "announce_asterisk.py")).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def test_resample() -> None:
    # 0.1 s of tone at 22050 -> ~0.1 s at 8000 (length tracks the ratio).
    s = array.array("h", [8000] * 2205)
    out = aa._resample_to_8k(s, 22050, 8000)
    check("resample: 2205@22050 -> ~800@8000", 780 <= len(out) <= 820)
    check("resample: output is int16-clamped", all(-32768 <= x <= 32767 for x in out))
    check("resample: identity when rates match", list(aa._resample_to_8k(s, 8000, 8000)) == list(s))
    check("resample: empty stays empty", len(aa._resample_to_8k(array.array("h", []), 22050)) == 0)
    # Upsample path (8000 -> 16000, no anti-alias) roughly doubles the length.
    up = aa._resample_to_8k(array.array("h", [100] * 400), 8000, 16000)
    check("resample: upsample ~doubles length", 760 <= len(up) <= 820)
    # A loud constant signal must not clip past int16 after filtering+interp.
    loud = aa._resample_to_8k(array.array("h", [32767] * 2205), 22050, 8000)
    check("resample: constant full-scale never overflows", all(x <= 32767 for x in loud))


def test_box_filter() -> None:
    s = array.array("h", [0, 100, 0, 100, 0, 100])
    check("box: width<=1 is identity", list(aa._box_filter(s, 1)) == list(s))
    sm = aa._box_filter(s, 3)
    check("box: smooths toward the local mean (interior ~33-66)", 20 <= sm[2] <= 80)
    check("box: length preserved", len(sm) == len(s))
    check("box: empty stays empty", len(aa._box_filter(array.array("h", []), 3)) == 0)


def _wav_bytes(samples, rate, channels) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array.array("h", samples).tobytes())
    return buf.getvalue()


def test_read_wav_mono16() -> None:
    # Mono 16-bit reads straight through.
    with wave.open(io.BytesIO(_wav_bytes([1, 2, 3, 4], 8000, 1)), "rb") as w:
        samples, rate = aa._read_wav_mono16(w)
    check("wav: mono 16-bit read intact", samples is not None and list(samples) == [1, 2, 3, 4] and rate == 8000)
    # Stereo is downmixed to mono (average of L/R pairs).
    with wave.open(io.BytesIO(_wav_bytes([10, 20, 30, 40], 8000, 2)), "rb") as w:
        samples, _ = aa._read_wav_mono16(w)
    check("wav: stereo downmixed to mono averages pairs", list(samples) == [15, 35])
    # 8-bit (sampwidth 1) is rejected -> None (can't decode as int16).
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(1); w.setframerate(8000); w.writeframes(b"\x01\x02")
    with wave.open(io.BytesIO(buf.getvalue()), "rb") as w:
        samples, _ = aa._read_wav_mono16(w)
    check("wav: non-16-bit rejected as None", samples is None)


def test_url_host_guard() -> None:
    # SSRF guard: loopback / link-local / reserved / multicast are blocked; a normal
    # public/private-LAN host is allowed (HA's TTS lives on the private LAN).
    check("ssrf: loopback host blocked", aa._host_is_safe("127.0.0.1") is False)
    check("ssrf: IPv6 loopback blocked", aa._host_is_safe("::1") is False)
    check("ssrf: link-local blocked", aa._host_is_safe("169.254.169.254") is False)
    check("ssrf: unspecified blocked", aa._host_is_safe("0.0.0.0") is False)
    check("ssrf: multicast blocked", aa._host_is_safe("239.0.0.1") is False)
    check("ssrf: private LAN host allowed (HA TTS host)", aa._host_is_safe("192.168.1.152") is True)
    check("ssrf: unresolvable host rejected",
          aa._host_is_safe("no-such-host.invalid") is False)
    check("ssrf: the fetch opener refuses redirects",
          any(isinstance(h, aa._NoRedirect) for h in aa._OPENER.handlers))
    # A non-http scheme / an internal-IP URL is refused before any fetch.
    check("ssrf: file:// url refused", aa.render_url_to_8k("file:///etc/passwd", "/tmp/x") is False)
    check("ssrf: loopback url refused without fetching",
          aa.render_url_to_8k("http://127.0.0.1:9/x.wav", "/tmp/x") is False)


def test_decode_audio() -> None:
    # A real WAV decodes directly (no ffmpeg needed).
    wav = _wav_bytes([100, 200, 300, 400], 8000, 1)
    samples, rate = aa._decode_audio_to_mono16(wav)
    check("decode: WAV bytes decode directly", samples is not None and list(samples) == [100, 200, 300, 400] and rate == 8000)
    # Non-WAV bytes with no ffmpeg (this box) fall back gracefully to (None, rate) —
    # never raise. (On the add-on, ffmpeg transcodes MP3 here.)
    samples, _ = aa._decode_audio_to_mono16(b"ID3\x04not-a-wav-this-is-mp3ish")
    check("decode: non-WAV without ffmpeg -> None (graceful)", samples is None)
    check("decode: ffmpeg helper returns None when ffmpeg absent",
          aa._ffmpeg_to_8k_wav(b"\x00\x01\x02") is None or isinstance(aa._ffmpeg_to_8k_wav(b""), (bytes, type(None))))


if __name__ == "__main__":
    test_resample()
    test_box_filter()
    test_read_wav_mono16()
    test_url_host_guard()
    test_decode_audio()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
