"""Phone-band mastering of the spoken replies (v0.122.0).

    python3 -m pytest switchboard/tests/test_tts_mastering.py

switchboard-tts speaks every dynamic reply on a call (the operator, directory,
dial-a-status, wake-up, the announce confirmation, the lights menu, the
assistant). Since v0.122.0 it runs the handset announcements' mastering
(announce_asterisk, v0.121.0) on espeak-ng's output. These tests pin:

  * with a real ffmpeg (skipped cleanly without one): a reply comes out at the
    built-in prompts' speech level, peaks limited, the same length, and the same
    bytes for the same input;
  * a missing, failing, stalled or garbage-returning ffmpeg, or mastering code
    that cannot load, gives the pre-0.122 output byte for byte, never raises,
    and is noted once per add-on start, without the text;
  * ffmpeg's limit for a reply is MASTER_TIMEOUT_S (3 s), and a run past it
    pauses the mastering across processes for MASTER_PAUSE_S;
  * silence and over-long speech skip the mastering without a log line;
  * the three callers pass the note on after a successful reply.

espeak-ng is stubbed through switchboard-tts's `subprocess`; ffmpeg, unless a
test asks for the real one, through a private copy of announce_asterisk.
"""
import array
import functools
import importlib.util
import io
import math
import os
import shutil
import subprocess as _sp
import sys
import types
import warnings
import wave
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parents[1]
WEBUI = ADDON / "rootfs" / "usr" / "share" / "switchboard" / "webui"
OPERATOR = ADDON / "rootfs" / "usr" / "share" / "switchboard" / "operator"
AGI_BIN = ADDON / "rootfs" / "var" / "lib" / "asterisk" / "agi-bin"
TTS_PATH = ADDON / "rootfs" / "usr" / "bin" / "switchboard-tts"
for _p in (WEBUI, OPERATOR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

ESPEAK_RATE = 22050  # what espeak-ng writes
SECRET = "the zebra lamp is on"
NOTE = ("[switchboard-tts] phone-band mastering failed (ffmpeg not found), "
        "so a reply was spoken unmastered; later fallbacks are not logged")


@functools.lru_cache(maxsize=None)
def _speechlike(seconds: float = 2.0, rate: int = ESPEAK_RATE, peak: float = 0.9) -> bytes:
    """Deterministic voiced bursts separated by silence, as
    test_announce_mastering builds them: the harmonics of a 120 Hz voice under
    three formant peaks, with spread phases as in speech (not a buzz whose every
    harmonic peaks at once), reaching past 4 kHz; peak-normalized to `peak`, as
    TTS output is. 16-bit little-endian PCM."""
    harmonics = []
    for k in range(1, 46):
        f = 120.0 * k
        env = sum(a / (1.0 + ((f - fc) / bw) ** 2)
                  for fc, bw, a in ((550, 150, 1.0), (1500, 200, 0.5), (2600, 300, 0.35)))
        harmonics.append((2 * math.pi * f / rate, env, 0.7 * k * k))
    vals = []
    for i in range(int(seconds * rate)):
        if (i // int(0.25 * rate)) % 2:
            vals.append(0.0)
            continue
        vals.append(sum(a * math.sin(w * i + ph) for w, a, ph in harmonics))
    scale = peak * 32767 / max(abs(v) for v in vals)
    out = array.array("h", [round(v * scale) for v in vals])
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def _wav(pcm: bytes, rate: int = ESPEAK_RATE, ch: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _read(path) -> tuple[bytes, int, int, int]:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels(), w.getsampwidth()


def _pre_0122(pcm: bytes, rate: int = ESPEAK_RATE) -> bytes:
    """What switchboard-tts wrote for mono espeak output before v0.122.0,
    restated independently: audioop's ratecv, or the linear resampler where
    audioop is gone (Python 3.13+)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            import audioop
        except ImportError:
            audioop = None
    if audioop is None:
        tts = SourceFileLoader("sbtts_ref", str(TTS_PATH)).load_module()
        return tts._linear_resample(pcm, rate, 8000)
    return audioop.ratecv(pcm, 2, 1, rate, 8000, None)[0]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """switchboard-tts with espeak stubbed to `rig.espeak_pcm`, its pause and note
    files under tmp_path, and a private announce_asterisk as the mastering."""
    tts = SourceFileLoader(f"sbtts_{tmp_path.name}", str(TTS_PATH)).load_module()
    spec = importlib.util.spec_from_file_location(
        f"announce_asterisk_tts_{tmp_path.name}", str(WEBUI / "announce_asterisk.py"))
    aa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aa)
    monkeypatch.setitem(sys.modules, "announce_asterisk", aa)
    tts.MASTER_DIR = str(WEBUI)
    tts.PAUSE_FILE = str(tmp_path / "paused")
    tts.NOTED_FILE = str(tmp_path / "noted")
    tts._espeak_bin = lambda: "/usr/bin/espeak-ng"
    state = types.SimpleNamespace(tts=tts, aa=aa, espeak_pcm=_speechlike(),
                                  ffmpeg_calls=[], out=tmp_path / "reply")

    def espeak_run(cmd, **kw):
        assert cmd[-2:] == ["--", SECRET] or cmd[-2] == "--", cmd
        return types.SimpleNamespace(returncode=0, stdout=_wav(state.espeak_pcm), stderr=b"")
    tts.subprocess = types.SimpleNamespace(run=espeak_run, DEVNULL=_sp.DEVNULL,
                                           TimeoutExpired=_sp.TimeoutExpired,
                                           SubprocessError=_sp.SubprocessError)
    return state


def _stub_ffmpeg(rig, behave):
    """Replace announce_asterisk's ffmpeg with `behave(cmd, kw)`, counting calls."""
    def run(cmd, **kw):
        rig.ffmpeg_calls.append((cmd, kw))
        return behave(cmd, kw)
    rig.aa.subprocess = types.SimpleNamespace(run=run, TimeoutExpired=_sp.TimeoutExpired,
                                              SubprocessError=_sp.SubprocessError)


def _speak(rig) -> bytes:
    assert rig.tts.synthesize(SECRET, str(rig.out)) is True
    data, rate, ch, width = _read(str(rig.out) + ".wav")
    assert (rate, ch, width) == (8000, 1, 2)
    return data


def _le(samples) -> bytes:
    a = array.array("h", samples)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def _err(capsys) -> str:
    """stderr minus the pre-0.122 path's own line where audioop is gone
    (Python 3.13+; the add-on runs 3.12)."""
    return "".join(line for line in capsys.readouterr().err.splitlines(True)
                   if "audioop unavailable" not in line)


def _level_and_peak(pcm: bytes, aa) -> tuple[float, float]:
    s = array.array("h")
    s.frombytes(pcm)
    if sys.byteorder == "big":
        s.byteswap()
    f = array.array("f", (v / 32768.0 for v in s))
    return aa._speech_level_dbfs(f, 8000), max(abs(min(s)), max(s)) / 32768.0


# --------------------------------------------------------------------------- #
# The mastered path, with a real ffmpeg.
# --------------------------------------------------------------------------- #
def test_a_reply_is_mastered_to_the_prompts_level(rig, capsys):
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg on PATH")
    capsys.readouterr()
    got = _speak(rig)
    old = _pre_0122(rig.espeak_pcm)
    assert got != old, "the reply came out unmastered"
    level, peak = _level_and_peak(got, rig.aa)
    assert abs(level - rig.aa.SPEECH_TARGET_DBFS) < 1.0, level
    assert peak <= rig.aa.LIMIT_CEILING + 1 / 32768, peak
    n_in = len(rig.espeak_pcm) // 2
    assert abs(len(got) // 2 - n_in * 8000 / ESPEAK_RATE) <= 8, (len(got) // 2, n_in)
    assert _err(capsys) == ""
    assert not os.path.exists(rig.tts.NOTED_FILE)
    assert _speak(rig) == got, "the same reply rendered different bytes"
    # ...and it is the announcements' mastering, not a copy of it.
    ann, why = rig.aa._master_speech(rig.espeak_pcm, ESPEAK_RATE, n_in)
    assert why == "" and _le(ann) == got


def test_a_quieter_voice_is_raised_too(rig):
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg on PATH")
    rig.espeak_pcm = _speechlike(peak=0.25)  # about 11 dB under the first test's
    level, peak = _level_and_peak(_speak(rig), rig.aa)
    assert abs(level - rig.aa.SPEECH_TARGET_DBFS) < 1.0, level
    assert peak <= rig.aa.LIMIT_CEILING + 1 / 32768, peak


# --------------------------------------------------------------------------- #
# Every fallback is the pre-0.122 output, byte for byte, noted once.
# --------------------------------------------------------------------------- #
def _missing(cmd, kw):
    raise FileNotFoundError("ffmpeg")


def _exit_1(cmd, kw):
    return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")


def _short(cmd, kw):  # a quarter of the samples the input implies
    n = len(kw["input"]) // 2 * 8000 // ESPEAK_RATE // 4
    return types.SimpleNamespace(returncode=0, stdout=array.array("f", [0.1] * n).tobytes(),
                                 stderr=b"")


def _zeros(cmd, kw):
    n = len(kw["input"]) // 2 * 8000 // ESPEAK_RATE
    return types.SimpleNamespace(returncode=0, stdout=bytes(4 * n), stderr=b"")


@pytest.mark.parametrize("behave,reason", [
    (_missing, "ffmpeg not found"),
    (_exit_1, "ffmpeg exited 1"),
    (_short, "ffmpeg returned unusable audio"),
    (_zeros, "ffmpeg returned unusable audio"),
])
def test_a_failed_master_speaks_the_old_bytes_and_says_so_once(rig, capsys, behave, reason):
    _stub_ffmpeg(rig, behave)
    capsys.readouterr()
    assert _speak(rig) == _pre_0122(rig.espeak_pcm)
    err = _err(capsys)
    assert err.count("\n") == 1 and f"phone-band mastering failed ({reason})" in err, err
    assert "later fallbacks are not logged" in err and "zebra" not in err, err
    assert _speak(rig) == _pre_0122(rig.espeak_pcm)
    assert _err(capsys) == "", "a second fallback was logged"
    assert len(rig.ffmpeg_calls) == 2, "a failure that cost no time paused the mastering"


def test_mastering_code_that_cannot_load_is_a_fallback(rig, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "announce_asterisk", None)  # import raises
    capsys.readouterr()
    assert _speak(rig) == _pre_0122(rig.espeak_pcm)
    err = _err(capsys)
    assert "phone-band mastering failed (the mastering code could not be loaded)" in err, err
    assert "later fallbacks are not logged" in err and "zebra" not in err, err


def test_an_error_inside_the_mastering_is_a_fallback(rig, capsys, monkeypatch):
    def boom(*a, **k):
        raise ZeroDivisionError
    monkeypatch.setattr(rig.aa, "_clip_gain_db", boom)
    _stub_ffmpeg(rig, lambda cmd, kw: types.SimpleNamespace(
        returncode=0, stderr=b"",
        stdout=array.array("f", [0.1] * (len(kw["input"]) // 2 * 8000 // ESPEAK_RATE)).tobytes()))
    capsys.readouterr()
    assert _speak(rig) == _pre_0122(rig.espeak_pcm)
    assert "(mastering error ZeroDivisionError)" in _err(capsys)


def test_without_the_note_file_every_fallback_is_logged(rig, capsys, tmp_path):
    rig.tts.NOTED_FILE = str(tmp_path / "no-such-dir" / "noted")
    _stub_ffmpeg(rig, _missing)
    capsys.readouterr()
    _speak(rig)
    _speak(rig)
    err = _err(capsys)
    assert err.count("phone-band mastering failed (ffmpeg not found)") == 2, err
    assert "later fallbacks are not logged" not in err, err


# --------------------------------------------------------------------------- #
# The reply's time limit, and the pause that outlives the process.
# --------------------------------------------------------------------------- #
def test_a_stalled_ffmpeg_costs_one_reply_its_wait(rig, capsys):
    def stall(cmd, kw):
        raise _sp.TimeoutExpired(cmd, kw["timeout"])
    _stub_ffmpeg(rig, stall)
    capsys.readouterr()
    assert _speak(rig) == _pre_0122(rig.espeak_pcm)
    assert rig.ffmpeg_calls[0][1]["timeout"] == rig.tts.MASTER_TIMEOUT_S == 3
    err = _err(capsys)
    assert "phone-band mastering failed (ffmpeg timed out)" in err, err
    assert os.path.isfile(rig.tts.PAUSE_FILE)

    # A later reply is another process: a fresh copy of both modules.
    later = SourceFileLoader("sbtts_later", str(TTS_PATH)).load_module()
    for name in ("MASTER_DIR", "PAUSE_FILE", "NOTED_FILE", "_espeak_bin", "subprocess"):
        setattr(later, name, getattr(rig.tts, name))
    rig.aa._master_paused_until = 0.0  # the new process has no in-memory pause
    rig.tts = later
    assert _speak(rig) == _pre_0122(rig.espeak_pcm)
    assert len(rig.ffmpeg_calls) == 1, "the pause did not hold in the next process"
    assert _err(capsys) == ""

    # Just before the pause ends it still holds; once it has, ffmpeg runs again.
    t = os.stat(rig.tts.PAUSE_FILE).st_mtime
    os.utime(rig.tts.PAUSE_FILE, (t, t - rig.tts.MASTER_PAUSE_S + 5))
    _speak(rig)
    assert len(rig.ffmpeg_calls) == 1
    os.utime(rig.tts.PAUSE_FILE, (t, t - rig.tts.MASTER_PAUSE_S - 1))
    _speak(rig)
    assert len(rig.ffmpeg_calls) == 2


def test_announcements_keep_their_own_limit(rig):
    seen = []

    def run(cmd, **kw):
        seen.append(kw.get("timeout"))
        raise FileNotFoundError
    rig.aa.subprocess = types.SimpleNamespace(run=run, TimeoutExpired=_sp.TimeoutExpired,
                                              SubprocessError=_sp.SubprocessError)
    rig.aa._master_speech(rig.espeak_pcm, ESPEAK_RATE, len(rig.espeak_pcm) // 2)
    assert seen == [rig.aa.MASTER_TIMEOUT_S] == [10]


def test_a_link_is_never_a_pause_nor_followed(rig, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("x")
    os.utime(target, (0, 0))
    os.symlink(target, rig.tts.PAUSE_FILE)
    assert rig.tts._paused() is False
    rig.tts._pause()
    assert target.stat().st_mtime == 0, "the pause followed a link"


def test_a_future_pause_stamp_is_no_pause(rig):
    open(rig.tts.PAUSE_FILE, "w").close()
    t = os.stat(rig.tts.PAUSE_FILE).st_mtime
    os.utime(rig.tts.PAUSE_FILE, (t, t + 3600))  # a clock set back after the stall
    assert rig.tts._paused() is False


# --------------------------------------------------------------------------- #
# What the mastering leaves alone, quietly.
# --------------------------------------------------------------------------- #
def test_silence_and_long_speech_skip_the_mastering_silently(rig, capsys, monkeypatch):
    _stub_ffmpeg(rig, _missing)
    capsys.readouterr()
    rig.espeak_pcm = bytes(2 * ESPEAK_RATE)  # one second of silence
    assert _speak(rig) == _pre_0122(rig.espeak_pcm)
    monkeypatch.setattr(rig.aa, "MASTER_MAX_SECONDS", 1)
    rig.espeak_pcm = _speechlike(seconds=1.5)
    assert _speak(rig) == _pre_0122(rig.espeak_pcm)
    assert rig.ffmpeg_calls == [] and _err(capsys) == ""
    assert not os.path.exists(rig.tts.NOTED_FILE)


def test_stereo_is_never_handed_to_the_mastering(rig, capsys):
    called = []
    rig.tts._master = lambda *a: called.append(a)
    stereo = _speechlike(seconds=0.5) * 2  # any even-length PCM reads as stereo frames
    rig.tts.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
        returncode=0, stdout=_wav(stereo, ch=2), stderr=b"")
    assert rig.tts.synthesize(SECRET, str(rig.out)) is True
    assert called == []


# --------------------------------------------------------------------------- #
# The callers pass the note on after a successful reply.
# --------------------------------------------------------------------------- #
def _ok_tts(stderr: str):
    def run(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout="", stderr=stderr)
    return run


def _load_agi(name: str):
    hc = types.ModuleType("ha_client")
    hc.available = lambda: True
    hc.lights_by_area = lambda: {}
    hc.converse = lambda *a, **k: ("ok", "action_done")
    sys.modules.setdefault("ha_client", hc)
    return SourceFileLoader(f"sb_{name}_note", str(AGI_BIN / f"switchboard-{name}.agi")).load_module()


@pytest.mark.parametrize("which", ["agi_speech", "assistant", "automation"])
def test_each_caller_logs_the_note_after_a_successful_reply(which, capsys, monkeypatch):
    if which == "agi_speech":
        import agi_speech as mod
    else:
        mod = _load_agi(which)
    played = []
    monkeypatch.setattr(mod, "agi", lambda cmd: played.append(cmd) or "")
    monkeypatch.setattr(mod.subprocess, "run", _ok_tts(NOTE + "\n"))
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    capsys.readouterr()
    assert mod.say(SECRET) is True
    err = _err(capsys)
    assert NOTE in err and "zebra" not in err, err
    assert played and played[0].startswith("EXEC Playback "), played

    monkeypatch.setattr(mod.subprocess, "run", _ok_tts(""))
    assert mod.say(SECRET) is True
    assert _err(capsys) == "", "a clean reply logged something"
