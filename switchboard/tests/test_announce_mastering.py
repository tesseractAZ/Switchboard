"""Phone-band mastering of the handset announcements (v0.121.0).

    python3 switchboard/tests/test_announce_mastering.py

Every phone here runs G.711 µ-law, so a handset announcement is 8 kHz speech.
announce_asterisk shapes the speech of every branch ({text} through espeak-ng, a
WAV {url}, any other {url}) for that band with one ffmpeg process, sets each
clip's level, and limits its peaks. These tests pin:

  * the chain is the approved one, and every branch runs it, once;
  * the level, gain cap, silence and limiter math (pure, no ffmpeg needed);
  * a missing, failing, stalled or garbage-returning ffmpeg gives the
    pre-0.121 output byte for byte, logs once per process, and never raises;
  * the chime is not processed, and the media-player announcement is unchanged;
  * with a real ffmpeg (skipped cleanly without one): the shape of the
    spectrum, the level reached, and byte-identical output for the same input,
    which the announce dedupe's content hash relies on.

espeak-ng, HTTP and (unless a test asks for the real one) ffmpeg are stubbed
through the module's `subprocess` and `_OPENER`.
"""
import array
import contextlib
import functools
import hashlib
import importlib.util
import io
import math
import os
import shutil
import subprocess as _sp
import sys
import tempfile
import types
import wave
from pathlib import Path

_WEBUI = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share" / "switchboard" / "webui"
if str(_WEBUI) not in sys.path:
    sys.path.insert(0, str(_WEBUI))
import announce_audio  # noqa: E402

_failures = 0
URL_WAV = "http://tts.example.com/api/tts_proxy/clip.wav"
URL_MP3 = "http://tts.example.com/api/tts_proxy/clip.mp3"
SECRET_TEXT = "Battery at nine percent, grid is down"


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    assert cond, name


def _load():
    """A private copy of announce_asterisk: its once-per-process log flag and
    any stub a test installs stay with this copy."""
    spec = importlib.util.spec_from_file_location(
        "announce_asterisk_mastering_t", str(_WEBUI / "announce_asterisk.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _skip_without_ffmpeg(name: str) -> bool:
    """True (after reporting the skip) when no ffmpeg is on PATH."""
    if shutil.which("ffmpeg"):
        return False
    reason = f"{name}: ffmpeg not on PATH here (the image installs it)"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import pytest
        pytest.skip(reason)
    print(f"SKIP {reason}")
    return True


# --------------------------------------------------------------------------- #
# Signals and stubs.
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=None)
def _speechlike(seconds: float = 1.5, rate: int = 22050) -> array.array:
    """Deterministic voiced bursts separated by silence: the harmonics of a 120 Hz
    voice, shaped by three formant peaks, with spread phases as in speech (not a
    buzz whose every harmonic peaks at once), reaching past 4 kHz."""
    harmonics = []
    for k in range(1, 46):
        f = 120.0 * k
        env = sum(a / (1.0 + ((f - fc) / bw) ** 2)
                  for fc, bw, a in ((550, 150, 1.0), (1500, 200, 0.5), (2600, 300, 0.35)))
        harmonics.append((2 * math.pi * f / rate, env, 0.7 * k * k))
    out = []
    for i in range(int(seconds * rate)):
        if (i // int(0.25 * rate)) % 2:
            out.append(0.0)
            continue
        out.append(sum(a * math.sin(w * i + ph) for w, a, ph in harmonics))
    scale = 0.9 * 32767 / max(abs(v) for v in out)  # peak-normalized, as TTS output is
    return array.array("h", [round(v * scale) for v in out])


def _wav_bytes(samples, rate, channels=1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array.array("h", samples).tobytes())
    return buf.getvalue()


def _f32(values) -> bytes:
    a = array.array("f", values)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def _tone_f32(n: int, amp: float = 0.05) -> list:
    return [amp * math.sin(2 * math.pi * 440 * i / 8000) for i in range(n)]


class _Fake:
    """Stands in for subprocess.run. espeak-ng writes `speech` (22050 Hz) to its
    -w path. ffmpeg's MASTER call (the one with -af) answers per `master`:
    "real" runs the real ffmpeg, a callable returns what it returns (or raises),
    and the default answers a well-formed float stream of the right length.
    ffmpeg's plain DECODE call (the pre-0.121 path) answers per `decode`."""

    def __init__(self, speech=None, master=None, decode=None):
        self.speech = speech
        self.master = master
        self.decode = decode
        self.calls = []

    def __call__(self, cmd, **kw):
        cmd = list(cmd)
        self.calls.append((cmd, kw))
        if cmd[0] == "espeak-ng":
            path = cmd[cmd.index("-w") + 1]
            with open(path, "wb") as fh:
                fh.write(_wav_bytes(self.speech, 22050))
            return _sp.CompletedProcess(cmd, 0, b"", b"")
        if cmd[0] == "ffmpeg" and "-af" in cmd:
            if self.master == "real":
                return _sp.run(cmd, **kw)
            if callable(self.master):
                return self.master(cmd, kw)
            if "s16le" in cmd[:cmd.index("-i")]:
                n_out = round(len(kw["input"]) // 2 * 8000 / int(cmd[cmd.index("-ar") + 1]))
            else:
                n_out = 8000  # an encoded clip: one second
            return _sp.CompletedProcess(cmd, 0, _f32(_tone_f32(n_out)), b"")
        if cmd[0] == "ffmpeg":
            if self.decode == "real":
                return _sp.run(cmd, **kw)
            if self.decode is None:
                raise FileNotFoundError("ffmpeg")
            return _sp.CompletedProcess(cmd, 0, self.decode, b"")
        raise AssertionError(f"unexpected command {cmd[0]}")

    def masters(self):
        return [c for c, _ in self.calls if c[0] == "ffmpeg" and "-af" in c]

    def decodes(self):
        return [c for c, _ in self.calls if c[0] == "ffmpeg" and "-af" not in c]


@contextlib.contextmanager
def _stubbed(mod, fake, fetched: bytes = b""):
    """Route the module's subprocess through `fake`, and its HTTP fetch to
    `fetched`; capture what it prints."""
    class _Resp:
        def read(self, n=-1):
            return fetched if n < 0 else fetched[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    saved = (mod.subprocess, mod._OPENER, mod._host_is_safe)
    mod.subprocess = types.SimpleNamespace(
        run=fake, DEVNULL=_sp.DEVNULL, SubprocessError=_sp.SubprocessError,
        TimeoutExpired=_sp.TimeoutExpired)
    mod._OPENER = types.SimpleNamespace(open=lambda req, timeout=None: _Resp(), handlers=[])
    mod._host_is_safe = lambda host: host.endswith(".example.com")
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            yield out
    finally:
        mod.subprocess, mod._OPENER, mod._host_is_safe = saved


def _render(mod, branch: str) -> tuple:
    """Render one announcement through the public builder of `branch` ("text",
    "wav" or "mp3"); (whether it rendered, the file's bytes)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "a.wav")
        if branch == "text":
            ok = mod.build_announcement_8k(SECRET_TEXT, path)
        else:
            ok = mod.render_url_to_8k(URL_WAV if branch == "wav" else URL_MP3, path)
        if not os.path.exists(path):
            return ok, b""
        with open(path, "rb") as fh:
            return ok, fh.read()


def _today_bytes(mod, speech8k) -> bytes:
    """What the pre-0.121 code wrote for this 8 kHz speech: write_combined is
    unchanged, so this is the old output byte for byte."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.wav")
        announce_audio.write_combined(path, speech8k, 8000)
        with open(path, "rb") as fh:
            return fh.read()


def _speech_of(wav: bytes) -> array.array:
    """The speech between the chimes and their 0.45 s gaps."""
    with wave.open(io.BytesIO(wav), "rb") as w:
        s = array.array("h", w.readframes(w.getnframes()))
    head = len(announce_audio.synth_chime(8000)) + int(0.45 * 8000)
    return s[head:len(s) - head]


def _run(mod, branch, fake, speech, mp3=b"ID3\x04 encoded clip"):
    """Render `branch` with `fake` standing in for subprocess; (bytes, what the
    module printed)."""
    fetched = _wav_bytes(speech, 22050) if branch == "wav" else mp3
    with _stubbed(mod, fake, fetched) as log:
        ok, data = _render(mod, branch)
    check(f"{branch}: the announcement rendered", ok is True and data[:4] == b"RIFF")
    return data, log.getvalue()


# --------------------------------------------------------------------------- #
# The chain and the process.
# --------------------------------------------------------------------------- #
def test_the_chain_is_the_approved_one() -> None:
    mod = _load()
    check("chain: exactly the approved filters, in order", mod.MASTER_FILTERS.split(",") == [
        "aformat=sample_fmts=dblp:channel_layouts=mono",
        "highpass=f=250:poles=2", "highpass=f=250:poles=2",
        "equalizer=f=500:t=q:w=1:g=-2",
        "equalizer=f=2800:t=q:w=0.9:g=5",
        "lowpass=f=3500:poles=2", "lowpass=f=3500:poles=2",
        "acompressor=threshold=-24dB:ratio=3:attack=5:release=80:makeup=1",
        "aresample=8000"])
    check("level: the built-in prompts' -15 dBFS, gate -30, 20 ms frames, cap +20 dB",
          (mod.SPEECH_TARGET_DBFS, mod.SPEECH_GATE_DBFS, mod.SPEECH_FRAME_MS, mod.MAX_GAIN_DB)
          == (-15.0, -30.0, 20, 20.0))
    check("limiter: -1 dBFS ceiling, 2 ms attack, 40 ms release",
          (mod.LIMIT_CEILING, mod.LIMIT_ATTACK_MS, mod.LIMIT_RELEASE_MS) == (0.891, 2.0, 40.0)
          and abs(20 * math.log10(mod.LIMIT_CEILING) + 1.0) < 0.01)


def test_the_master_is_one_ffmpeg_process_over_pipes() -> None:
    mod = _load()
    raw = mod._master_argv(22050)
    enc = mod._master_argv(None)
    check("argv: the input rate is the speech's own",
          mod._master_argv(16000)[mod._master_argv(16000).index("-ar") + 1] == "16000")
    for name, argv in (("raw PCM", raw), ("encoded clip", enc)):
        check(f"argv ({name}): a list of strings starting with ffmpeg (no shell)",
              isinstance(argv, list) and all(isinstance(a, str) for a in argv)
              and argv[0] == "ffmpeg")
        check(f"argv ({name}): stdin to stdout, no file anywhere",
              argv[argv.index("-i") + 1] == "pipe:0" and argv[-1] == "pipe:1"
              and not any("/" in a for a in argv))
        check(f"argv ({name}): the chain, then mono 32-bit float at 8000 Hz",
              argv[argv.index("-af") + 1] == mod.MASTER_FILTERS
              and argv[argv.index("-c:a") + 1] == "pcm_f32le"
              and argv[argv.index("-f", argv.index("-i")) + 1] == "f32le"
              and argv[argv.index("-ar", argv.index("-i")) + 1] == "8000")
    i = raw.index("-i")
    check("argv (raw PCM): stdin is mono s16le at the speech's own rate",
          raw[raw.index("-f"):i] == ["-f", "s16le", "-ar", "22050", "-ac", "1"])
    check("argv (encoded clip): ffmpeg reads the container itself",
          "s16le" not in enc[:enc.index("-i")])
    seen = {}

    def master(cmd, kw):
        seen.update(kw)
        raise _sp.TimeoutExpired(cmd, kw.get("timeout"))
    fake = _Fake(speech=_speechlike(0.6), master=master)
    _run(mod, "text", fake, fake.speech)
    check("the master's timeout is MASTER_TIMEOUT_S (10 s)",
          seen.get("timeout") == mod.MASTER_TIMEOUT_S == 10)
    check("the master gets its input on stdin and captures stdout",
          isinstance(seen.get("input"), bytes) and seen.get("capture_output") is True)
    espeak = [kw for c, kw in fake.calls if c[0] == "espeak-ng"]
    check("espeak keeps its 20 s timeout", espeak and espeak[0]["timeout"] == 20)


def test_every_branch_is_mastered_once() -> None:
    """The regression this release exists for: on 0.120.0 no branch ran the
    chain (it fails there)."""
    speech = _speechlike(0.8)
    for branch in ("text", "wav", "mp3"):
        mod = _load()
        fake = _Fake(speech=speech)
        data, log = _run(mod, branch, fake, speech)
        check(f"{branch}: exactly one ffmpeg process, and it ran the chain",
              len(fake.masters()) == 1 and not fake.decodes()
              and fake.masters()[0][fake.masters()[0].index("-af") + 1] == mod.MASTER_FILTERS)
        cmd = fake.masters()[0]
        if branch == "mp3":
            check("mp3: the fetched clip itself is ffmpeg's input (decode and master in one)",
                  [kw for c, kw in fake.calls if c is cmd][0]["input"].startswith(b"ID3"))
        else:
            check(f"{branch}: the decoded speech is ffmpeg's input, at 22050 Hz",
                  cmd[cmd.index("-ar") + 1] == "22050"
                  and [kw for c, kw in fake.calls if c is cmd][0]["input"] == speech.tobytes())
        y = array.array("f", _f32(_tone_f32(round(len(speech) * 8000 / 22050)
                                            if branch != "mp3" else 8000)))
        gain = mod._clip_gain_db(mod._frame_powers(y))
        want = mod._limit_to_int16(y, 10 ** (gain / 20))
        check(f"{branch}: the speech that plays is the chain's output at the set level",
              _speech_of(data) == want)
        check(f"{branch}: nothing logged on the mastered path", log == "")


def test_a_wav_at_another_rate_is_mastered_at_its_own_rate() -> None:
    mod = _load()
    speech = _speechlike(0.8, 16000)
    fake = _Fake()
    with _stubbed(mod, fake, _wav_bytes(speech, 16000)) as log:
        ok, data = _render(mod, "wav")
    check("16 kHz WAV: rendered", ok is True)
    cmd = fake.masters()[0] if fake.masters() else []
    check("16 kHz WAV: ffmpeg is told 16000 Hz", cmd[cmd.index("-ar") + 1] == "16000")
    y = array.array("f", _f32(_tone_f32(round(len(speech) * 8000 / 16000))))
    gain = mod._clip_gain_db(mod._frame_powers(y))
    check("16 kHz WAV: mastered, not the fallback",
          _speech_of(data) == mod._limit_to_int16(y, 10 ** (gain / 20)) and log.getvalue() == "")


def test_the_chime_is_not_processed() -> None:
    mod = _load()
    speech = _speechlike(0.5)
    fake = _Fake(speech=speech)
    data, _ = _run(mod, "text", fake, speech)
    with wave.open(io.BytesIO(data), "rb") as w:
        s = array.array("h", w.readframes(w.getnframes()))
        check("output: 8 kHz mono 16-bit", (w.getframerate(), w.getnchannels(), w.getsampwidth())
              == (8000, 1, 2))
    chime = announce_audio.synth_chime(8000)
    check("chime: both chimes are synth_chime(8000) sample for sample",
          s[:len(chime)] == chime and s[len(s) - len(chime):] == chime)
    check("chime: ffmpeg is given the speech only",
          [kw for c, kw in fake.calls if c[0] == "ffmpeg"][0]["input"] == speech.tobytes())


# --------------------------------------------------------------------------- #
# The fallback: the pre-0.121 output exactly, logged once, never raised.
# --------------------------------------------------------------------------- #
def _garbage(kind):
    def master(cmd, kw):
        n = round(len(kw["input"]) // 2 * 8000 / 22050)
        body = {
            "empty": b"",
            "odd length": _f32(_tone_f32(n))[:-1],
            "NaN": _f32([float("nan")] + _tone_f32(n - 1)),
            "infinity": _f32(_tone_f32(n - 1) + [float("inf")]),
            "not audio (huge)": _f32([9.0] + _tone_f32(n - 1)),
            "all zero": _f32([0.0] * n),
            "too short": _f32(_tone_f32(n // 2)),
            "too long": _f32(_tone_f32(n * 2)),
        }[kind]
        return _sp.CompletedProcess(cmd, 0, body, b"")
    return master


def _failing(kind):
    def master(cmd, kw):
        if kind == "missing":
            raise FileNotFoundError(2, "No such file or directory", "ffmpeg")
        if kind == "unstartable":
            raise PermissionError(13, "Permission denied", "ffmpeg")
        if kind == "timeout":
            raise _sp.TimeoutExpired(cmd, kw.get("timeout"))
        return _sp.CompletedProcess(cmd, 1, b"", b"pipe:0: Invalid data")
    return master


_FAILURES = [("missing", _failing("missing")), ("unstartable", _failing("unstartable")),
             ("exit 1", _failing("exit")), ("timeout", _failing("timeout"))] + [
    (f"garbage: {k}", _garbage(k)) for k in
    ("empty", "odd length", "NaN", "infinity", "not audio (huge)", "all zero",
     "too short", "too long")]


def test_a_failed_master_renders_the_old_output_exactly() -> None:
    speech = _speechlike(0.7)
    for branch in ("text", "wav"):
        mod = _load()
        today = _today_bytes(mod, mod._resample_to_8k(speech, 22050, 8000))
        for name, master in _FAILURES:
            fake = _Fake(speech=speech, master=master)
            data, _ = _run(mod, branch, fake, speech)
            check(f"{branch} / {name}: the pre-0.121 bytes exactly", data == today)


def test_a_failed_master_on_a_non_wav_clip_takes_the_old_decode() -> None:
    mod = _load()
    decoded = array.array("h", [int(3000 * math.sin(i / 3)) for i in range(4000)])
    for name, master in _FAILURES:
        if name.startswith("garbage") and name.endswith(("short", "long")):
            continue  # an encoded input has no known length to check against
        fake = _Fake(master=master, decode=_wav_bytes(decoded, 8000))
        data, _ = _run(mod, "mp3", fake, None)
        check(f"mp3 / {name}: then the plain decode, once", len(fake.decodes()) == 1)
        check(f"mp3 / {name}: the pre-0.121 bytes exactly", data == _today_bytes(mod, decoded))
    fresh = _load()
    fake = _Fake(master=_failing("exit"), decode=_wav_bytes(decoded, 8000))
    _, log = _run(fresh, "mp3", fake, None)
    check("mp3: the fallback is logged like the others",
          "mastering failed (ffmpeg exited 1)" in log and "example.com" not in log)
    # ffmpeg absent altogether: a non-WAV clip cannot be decoded, as before.
    fake = _Fake(master=_failing("missing"), decode=None)
    with _stubbed(mod, fake, b"ID3\x04 encoded clip") as log:
        with tempfile.TemporaryDirectory() as d:
            ok = mod.render_url_to_8k(URL_MP3, os.path.join(d, "a.wav"))
    check("mp3 without ffmpeg: render failed, as in 0.120.0", ok is False)
    check("mp3 without ffmpeg: nothing rendered, so no unmastered line", log.getvalue() == "")


def test_the_fallback_is_logged_once_per_process() -> None:
    speech = _speechlike(0.5)
    mod = _load()
    lines = []
    for branch in ("text", "wav", "text"):
        for name, master in _FAILURES[:3]:
            fake = _Fake(speech=speech, master=master)
            _, log = _run(mod, branch, fake, speech)
            lines += [x for x in log.splitlines() if x]
    check(f"log: one line for nine fallbacks ({len(lines)})", len(lines) == 1)
    line = lines[0]
    check("log: names the cause", "mastering failed (ffmpeg not found)" in line)
    check("log: says what was played instead", "unmastered" in line)
    check("log: never the message, the URL or its host",
          SECRET_TEXT not in line and "example.com" not in line and "tts_proxy" not in line)
    mod2 = _load()
    _, log2 = _run(mod2, "text", _Fake(speech=speech, master=_failing("timeout")), speech)
    check("log: a new process logs again, with its own cause",
          "(ffmpeg timed out)" in log2 and log2.count("\n") == 1)


def test_a_mastering_error_never_raises() -> None:
    speech = _speechlike(0.5)
    mod = _load()
    today = _today_bytes(mod, mod._resample_to_8k(speech, 22050, 8000))

    def boom(*a, **k):
        raise RuntimeError("limiter bug")
    mod._limit_to_int16 = boom
    data, log = _run(mod, "text", _Fake(speech=speech), speech)
    check("error: the announcement still renders, unmastered, byte for byte", data == today)
    check("error: logged with the exception's type only",
          "(mastering error RuntimeError)" in log and "limiter bug" not in log)
    with _stubbed(mod, _Fake()):
        got = mod._master_speech(b"\x00\x01" * 2205, 22050, 2205)
    check("error: _master_speech returns the reason instead of raising",
          got == (None, "mastering error RuntimeError"))


def test_an_unwritable_log_does_not_cost_the_clip() -> None:
    speech = _speechlike(0.5)
    mod = _load()
    today = _today_bytes(mod, mod._resample_to_8k(speech, 22050, 8000))
    closed = io.StringIO()
    closed.close()
    fake = _Fake(speech=speech, master=_failing("missing"))
    with _stubbed(mod, fake, b""):
        with contextlib.redirect_stdout(closed):
            ok, data = _render(mod, "text")
    check("closed stdout: the fallback clip still renders, byte for byte",
          ok is True and data == today)


def test_speech_past_the_mastering_cap_takes_the_old_path_quietly() -> None:
    """The endpoint refuses any clip over 90 s, so speech of MASTER_MAX_SECONDS
    (120 s) or more is not worth mastering, and the limiter's cost grows with
    the clip. It renders as before, and the one log line is kept for a real
    failure."""
    mod = _load()
    check("cap: 120 s, and ffmpeg is told to stop there",
          mod.MASTER_MAX_SECONDS == 120
          and mod._master_argv(None)[mod._master_argv(None).index("-t") + 1] == "120")
    long_pcm = array.array("h", [1000, -1000, 500, 0] * (120 * 22050 // 4))
    fake = _Fake(speech=long_pcm)
    with _stubbed(mod, fake) as log:
        got = mod._speech_to_8k(long_pcm, 22050)
    check("cap: 120 s of decoded speech is not sent to ffmpeg", not fake.masters())
    check("cap: rendered as before, no log line",
          got == mod._resample_to_8k(long_pcm, 22050, 8000) and log.getvalue() == "")
    edge = long_pcm[:120 * 22050 - 100]  # just under: sent, and ffmpeg's output reaches the cut
    fake = _Fake(speech=edge)
    with _stubbed(mod, fake) as log:
        got = mod._speech_to_8k(edge, 22050)
    check("cap: speech that reaches the cut in ffmpeg also renders as before, unlogged",
          len(fake.masters()) == 1 and got == mod._resample_to_8k(edge, 22050, 8000)
          and log.getvalue() == "")
    decoded = array.array("h", [int(3000 * math.sin(i / 3)) for i in range(4000)])

    def stopped_at_the_cut(cmd, kw):
        return _sp.CompletedProcess(cmd, 0, _f32(_tone_f32(120 * 8000)), b"")
    fake = _Fake(master=stopped_at_the_cut, decode=_wav_bytes(decoded, 8000))
    data, log = _run(mod, "mp3", fake, None)
    check("cap: an encoded clip cut at 120 s takes the old decode",
          len(fake.decodes()) == 1 and data == _today_bytes(mod, decoded))
    check("cap: and is not logged as a failure", log == "")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod._note_unmastered("ffmpeg not found")
    check("cap: the log line is still free for a real failure",
          "mastering failed (ffmpeg not found)" in buf.getvalue())
    if shutil.which("ffmpeg"):  # the real ffmpeg stops at the cut and exits cleanly
        pcm = array.array("h", [1000, -1000, 500, 0] * (125 * 22050 // 4))
        flac = _sp.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le",
                        "-ar", "22050", "-ac", "1", "-i", "pipe:0", "-f", "flac", "pipe:1"],
                       input=pcm.tobytes(), capture_output=True, timeout=20).stdout
        check("cap: a real 125 s encoded clip stops at the cut",
              mod._ffmpeg_master(flac) == (None, mod._TOO_LONG))


def test_silent_or_empty_speech_is_not_sent_to_ffmpeg() -> None:
    mod = _load()
    for name, speech in (("all zero", array.array("h", [0] * 5000)),
                         ("empty", array.array("h"))):
        fake = _Fake(speech=speech)
        data, log = _run(mod, "text", fake, speech)
        check(f"{name}: no ffmpeg process", not fake.masters())
        check(f"{name}: the pre-0.121 bytes, no log line",
              data == _today_bytes(mod, mod._resample_to_8k(speech, 22050, 8000)) and log == "")


# --------------------------------------------------------------------------- #
# Level, gain and limiter (pure).
# --------------------------------------------------------------------------- #
def _sine(amp, n, f=1000.0, rate=8000):
    return [amp * math.sin(2 * math.pi * f * i / rate) for i in range(n)]


def test_the_speech_level_is_gated_rms() -> None:
    mod = _load()
    tone = _sine(0.1, 8000)
    check("level: 0.1-peak sine = -23.0 dBFS RMS",
          abs(mod._speech_level_dbfs(tone) - (20 * math.log10(0.1 / math.sqrt(2)))) < 0.01)
    check("level: 20 ms frames (160 samples at 8 kHz)",
          len(mod._frame_powers([0.0] * 1600)) == 10 and len(mod._frame_powers([0.0] * 159)) == 0)
    half = tone + [0.0] * 8000 + _sine(0.001, 8000)
    check("level: pauses and frames under -30 dBFS are left out",
          abs(mod._speech_level_dbfs(half) - mod._speech_level_dbfs(tone)) < 0.01)
    check("level: nothing above the gate -> None",
          mod._speech_level_dbfs(_sine(0.04, 8000)) is None)  # -31 dBFS
    check("level: just above the gate counts",
          mod._speech_level_dbfs(_sine(0.05, 8000)) is not None)  # -29 dBFS
    check("level: shorter than one frame -> None", mod._speech_level_dbfs(_sine(0.5, 100)) is None)


def _gained(samples, gain_db):
    k = 10 ** (gain_db / 20)
    return [v * k for v in samples]


def test_the_gain_sets_the_level_measured_after_it() -> None:
    mod = _load()
    # Speech at three loudnesses, with quiet frames that only count once raised.
    clip = _sine(0.08, 4000) + _sine(0.02, 4000) + _sine(0.04, 4000) + [0.0] * 4000
    g = mod._clip_gain_db(mod._frame_powers(clip))
    check(f"gain: the raised clip measures -15.0 dBFS ({g:+.2f} dB)",
          abs(mod._speech_level_dbfs(_gained(clip, g)) - (-15.0)) < 0.01)
    one_shot = -15.0 - mod._speech_level_dbfs(clip)
    check("gain: more than a one-shot estimate, since raised frames join the gate",
          g > one_shot + 0.5)
    loud = _sine(0.9, 8000)
    g = mod._clip_gain_db(mod._frame_powers(loud))
    check(f"gain: a loud clip is brought DOWN to -15 ({g:+.2f} dB)",
          g < 0 and abs(mod._speech_level_dbfs(_gained(loud, g)) + 15.0) < 0.01)
    check("gain: silence is left as it is",
          mod._clip_gain_db(mod._frame_powers([0.0] * 8000)) == 0.0
          and mod._clip_gain_db(mod._frame_powers(_sine(0.03, 8000))) == 0.0
          and mod._clip_gain_db([]) == 0.0)
    # One frame just over the gate and a long quiet passage: the fixed point
    # would be about +25 dB, and the cap holds it at +20.
    faint = _sine(0.05, 160) + _sine(0.012, 16000)
    g = mod._clip_gain_db(mod._frame_powers(faint))
    check(f"gain: capped at +20 dB ({g:+.2f})", g == 20.0)
    check("gain: deterministic", mod._clip_gain_db(mod._frame_powers(clip))
          == mod._clip_gain_db(mod._frame_powers(clip)))


def test_the_limiter() -> None:
    mod = _load()
    ceiling = round(0.891 * 32768)
    n, p = 8000, 4000
    y = [0.1] * n
    y[p] = 2.0
    out = mod._limit_to_int16(y, 1.0)
    plain = round(0.1 * 32768)
    check(f"limiter: the peak lands on the -1 dBFS ceiling ({out[p]})", out[p] == ceiling)
    check("limiter: no sample over the ceiling", max(abs(v) for v in out) <= ceiling)
    d = 1 - 0.891 / 2.0
    att = math.exp(-1 / 16)
    check("limiter: looks ahead (the sample before the peak is already lowered)",
          out[p - 1] < plain and abs(out[p - 1] - 0.1 * (1 - d * att) * 32768) <= 1)
    check("limiter: 2 ms attack (63 % of the way down 16 samples ahead)",
          abs(out[p - 16] - 0.1 * (1 - d * math.exp(-1)) * 32768) <= 1)
    check("limiter: 40 ms release (63 % recovered 320 samples on)",
          abs(out[p + 320] - 0.1 * (1 - d * math.exp(-1)) * 32768) <= 1)
    check("limiter: back to the plain gain well away from the peak",
          out[100] == plain and abs(out[p + 3000] - plain) <= 1)
    y[p] = -2.0
    neg = mod._limit_to_int16(y, 1.0)
    check("limiter: a negative peak the same", neg[p] == -ceiling and neg[p - 16] == out[p - 16])
    quiet = _sine(0.3, 800)
    check("limiter: under the ceiling it is the plain gain exactly",
          list(mod._limit_to_int16(quiet, 2.0)) == [round(v * 2.0 * 32768) for v in quiet])
    check("limiter: the gain is applied before the ceiling",
          max(abs(v) for v in mod._limit_to_int16(quiet, 4.0)) == ceiling)
    check("limiter: empty stays empty", len(mod._limit_to_int16([], 3.0)) == 0)
    dense = [0.95 * math.sin(i * 0.9) for i in range(4000)]
    lim = mod._limit_to_int16(dense, 10 ** (12 / 20))
    check("limiter: +12 dB into a dense loud signal never passes the ceiling",
          max(abs(v) for v in lim) <= ceiling)
    check("limiter: deterministic", mod._limit_to_int16(dense, 3.0) == mod._limit_to_int16(dense, 3.0))


def test_the_output_check_on_ffmpeg() -> None:
    mod = _load()
    payload = b"\x00\x10" * 2205

    def answer(body, rc=0):
        return lambda cmd, **kw: _sp.CompletedProcess(cmd, rc, body, b"")
    def raising(exc):
        def run(cmd, **kw):
            raise exc
        return run
    good = _f32(_tone_f32(800))
    unusable = "ffmpeg returned unusable audio"
    cases = [("well-formed", answer(good), ""),
             ("1 % long is within tolerance", answer(_f32(_tone_f32(808))), ""),
             ("2 % long is not", answer(_f32(_tone_f32(816))), unusable),
             ("2 % short is not", answer(_f32(_tone_f32(784))), unusable),
             ("a peak at the sanity bound passes", answer(_f32([8.0] + _tone_f32(799))), ""),
             ("just past it does not", answer(_f32([-8.01] + _tone_f32(799))), unusable),
             ("an infinity", answer(_f32(_tone_f32(799) + [float("-inf")])), unusable),
             ("a NaN", answer(_f32(_tone_f32(799) + [float("nan")])), unusable),
             ("all zero", answer(_f32([0.0] * 800)), unusable),
             ("empty", answer(b""), unusable),
             ("not whole float samples", answer(good[:-2]), unusable),
             ("exit 3 with audio", answer(good, 3), "ffmpeg exited 3"),
             ("missing", raising(FileNotFoundError(2, "No such file", "ffmpeg")),
              "ffmpeg not found"),
             ("not startable", raising(PermissionError(13, "Permission denied")),
              "ffmpeg could not run"),
             ("stalled", raising(_sp.TimeoutExpired(["ffmpeg"], 10)), "ffmpeg timed out")]
    for name, run, why in cases:
        with _stubbed(mod, run):
            y, got = mod._ffmpeg_master(payload, 22050, 2205)
        check(f"output check: {name} -> {why or 'accepted'}",
              got == why and (y is not None) is (why == ""))
    # An encoded clip has no known length: any well-formed stream is taken.
    with _stubbed(mod, answer(_f32(_tone_f32(123)))):
        y, got = mod._ffmpeg_master(b"ID3", None, None)
    check("output check: an encoded clip's stream of any length", got == "" and len(y) == 123)


# --------------------------------------------------------------------------- #
# The media-player announcement (announce_audio) is not touched.
# --------------------------------------------------------------------------- #
def test_the_media_player_announcement_is_unchanged() -> None:
    speech = _speechlike(0.5)
    fake = _Fake(speech=speech)
    saved = announce_audio.subprocess
    announce_audio.subprocess = types.SimpleNamespace(
        run=fake, DEVNULL=_sp.DEVNULL, SubprocessError=_sp.SubprocessError,
        TimeoutExpired=_sp.TimeoutExpired)
    try:
        with tempfile.TemporaryDirectory() as d:
            path, ref = os.path.join(d, "m.wav"), os.path.join(d, "r.wav")
            ok = announce_audio.build_announcement(SECRET_TEXT, path)
            announce_audio.write_combined(ref, speech, 22050)
            same = open(path, "rb").read() == open(ref, "rb").read()
    finally:
        announce_audio.subprocess = saved
    check("speakers: still rendered at 22050 Hz", ok and announce_audio.RATE == 22050)
    check("speakers: no ffmpeg", not [c for c, _ in fake.calls if c[0] == "ffmpeg"])
    check("speakers: the espeak speech exactly as before", same)


# --------------------------------------------------------------------------- #
# With a real ffmpeg.
# --------------------------------------------------------------------------- #
def _goertzel_db(x, f, rate):
    n = len(x)
    w = 2 * math.pi * f / rate
    c = 2 * math.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s1, s2 = v + c * s1 - s2, s1
    p = s1 * s1 + s2 * s2 - c * s1 * s2
    return 10 * math.log10(max(p, 1e-30)) - 20 * math.log10(n / 2)


def _multitone(tones, amp, seconds=2.0, rate=22050):
    return array.array("h", [int(round(32767 * sum(amp * math.sin(2 * math.pi * f * i / rate)
                                                    for f in tones)))
                             for i in range(int(seconds * rate))])


def test_real_ffmpeg_shapes_the_band() -> None:
    """Fails on 0.120.0: its box filter let a 6 kHz tone fold back to 2 kHz only
    about 15 dB down, and nothing cut 100 Hz or lifted 2.8 kHz."""
    if _skip_without_ffmpeg("band shape"):
        return
    mod = _load()
    tones = (100, 700, 2800, 6000)
    clip = _multitone(tones, 0.1)
    fake = _Fake(master="real")
    with _stubbed(mod, fake, _wav_bytes(clip, 22050)) as log:
        ok, data = _render(mod, "wav")
    check("band: rendered", ok is True)
    speech = _speech_of(data)
    steady = [v / 32768 for v in speech[4000:12000]]  # 1 s, the compressor settled
    db = {f: _goertzel_db(steady, f, 8000) for f in (100, 700, 2000, 2800)}
    check(f"band: 100 Hz cut at least 20 dB against 700 Hz ({db[100] - db[700]:.1f})",
          db[100] - db[700] < -20)
    check(f"band: 2.8 kHz lifted at least 2 dB against 700 Hz ({db[2800] - db[700]:+.1f})",
          db[2800] - db[700] > 2.0)
    check(f"band: the 6 kHz tone leaves nothing at its 2 kHz alias ({db[2000] - db[700]:.1f})",
          db[2000] - db[700] < -60)
    check("band: mastered (one real ffmpeg run, no fallback)",
          len(fake.masters()) == 1 and log.getvalue() == "")


def test_real_ffmpeg_does_not_clip_inside_the_chain() -> None:
    """A loud clip (TTS output is peak-normalized) gains up to +5 dB at 2.8 kHz.
    The chain runs in double precision, so nothing clips before the gain stage
    sets the level; run in 16-bit, the clipping shows up as intermodulation
    products inside the band, about 30 dB below the tones."""
    if _skip_without_ffmpeg("no clipping in the chain"):
        return
    mod = _load()
    rate = 22050
    clip = array.array("h", [int(round(32767 * (0.45 * math.sin(2 * math.pi * 700 * i / rate)
                                                + 0.45 * math.sin(2 * math.pi * 2800 * i / rate))))
                             for i in range(2 * rate)])
    y, why = mod._ffmpeg_master(clip.tobytes(), rate, len(clip))
    check("loud: mastered", y is not None and why == "")
    steady = y[4000:12000]
    db = {f: _goertzel_db(steady, f, 8000) for f in (700, 1400, 2100)}
    check(f"loud: no intermodulation at 2.1 kHz ({db[2100] - db[700]:.1f} dB)",
          db[2100] - db[700] < -45)
    check(f"loud: none at 1.4 kHz ({db[1400] - db[700]:.1f} dB)", db[1400] - db[700] < -45)


def _real_branch(mod, branch, speech, encoded=b""):
    fake = _Fake(speech=speech, master="real", decode="real")
    data, log = _run(mod, branch, fake, speech, encoded)
    return data, log, fake


def test_real_ffmpeg_every_branch_reaches_the_level_and_is_deterministic() -> None:
    if _skip_without_ffmpeg("every branch, real ffmpeg"):
        return
    speech = _speechlike(2.0)
    flac = _sp.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", "22050",
                    "-ac", "1", "-i", "pipe:0", "-f", "flac", "pipe:1"],
                   input=speech.tobytes(), capture_output=True, timeout=20).stdout
    check("setup: an encoded (non-WAV) clip", flac[:4] == b"fLaC")
    for branch in ("text", "wav", "mp3"):
        mod = _load()
        first, log, fake = _real_branch(mod, branch, speech, flac)
        second, _, _ = _real_branch(mod, branch, speech, flac)
        check(f"{branch}: mastered by the real ffmpeg, nothing logged",
              len(fake.masters()) == 1 and not fake.decodes() and log == "")
        s = [v / 32768 for v in _speech_of(first)]
        level = mod._speech_level_dbfs(s)
        check(f"{branch}: speech at the built-in prompts' level ({level:.2f} dBFS)",
              abs(level + 15.0) < 0.5)
        check(f"{branch}: peaks at or under -1 dBFS",
              max(abs(v) for v in _speech_of(first)) <= round(0.891 * 32768))
        check(f"{branch}: the same input renders byte-identical audio, so the "
              "announce dedupe's content hash matches",
              first == second
              and hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest())


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
