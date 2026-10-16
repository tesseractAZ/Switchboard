"""The recording breadcrumb the operator AGI and agi_speech.listen() leave.

    python3 -m pytest switchboard/tests/test_operator_agi.py

The WAV is deleted after transcription, so the `rec` line is the only record of
what RECORD captured. Until this file existed the operator logged only a byte
count, beside a comment that described a silent capture as about 64 kB/s for
SILENCE_S seconds, and agi_speech.listen() logged nothing at all. A real silent
capture is 16364 bytes: RECORD FILE s=N rewinds N-1 s off a recording that
ended on silence, so one in which nothing crossed the silence threshold keeps
1.02 s whatever N is (status at s=2 and the operator at s=4 both produce it).
A review reading those bytes could not tell "said nothing" from "spoke briefly".
The line now states the length in seconds and flags that remainder `no-speech`.
"""
import contextlib
import io
import os
import re
import shutil
import tempfile
import wave
from importlib.machinery import SourceFileLoader
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OPERATOR_AGI = _ROOT / "rootfs" / "var" / "lib" / "asterisk" / "agi-bin" / "switchboard-operator.agi"
_SPEECH = _ROOT / "rootfs" / "usr" / "share" / "switchboard" / "operator" / "agi_speech.py"

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    assert cond, name


def _operator():
    # A fresh module per test: the end-to-end tests rebind its channel I/O.
    return SourceFileLoader("sb_operator_agi", str(_OPERATOR_AGI)).load_module()


def _speech():
    # Not under the bare name `agi_speech`, which other tests stub in sys.modules.
    return SourceFileLoader("agi_speech_recdiag", str(_SPEECH)).load_module()


def _wav(path: str, frames: int) -> str:
    """The file Asterisk's `wav` format writes: 8 kHz, 16-bit, mono."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * frames)
    return path


# The four shapes a capture takes, at the sizes Asterisk actually writes.
SILENT = 8160        # 1.02 s: the silence remainder, 16364 bytes on disk
SPOKEN = 32800       # 4.10 s: 65644 bytes, a short request plus its trailing 1 s
CAPPED = 80000       # 10.00 s: the operator's RECORD_MS cap
SILENCE_REPLY = "200 result=0 (timeout) endpos=8160"


def _twins():
    """Both copies of the rule, each under its own name."""
    op, sp = _operator(), _speech()
    return (("operator", op._rec_summary), ("speech", sp.rec_summary))


def test_a_silent_capture_is_sized_and_flagged() -> None:
    tmp = tempfile.mkdtemp()
    try:
        for name, summary in _twins():
            line = summary(SILENCE_REPLY, _wav(os.path.join(tmp, "a.wav"), SILENT))
            check(f"{name}: the silence remainder is 16364 bytes and 1.02 s, flagged",
                  line == "bytes=16364 secs=1.02 end=timeout no-speech")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_hangup_and_the_cap_are_not_called_silence() -> None:
    """`no-speech` has to mean the remainder and nothing else, or a review is
    back to guessing. A hang-up one second in is the same length and is not it."""
    tmp = tempfile.mkdtemp()
    try:
        for name, summary in _twins():
            line = summary("200 result=0 (timeout) endpos=32800",
                           _wav(os.path.join(tmp, "b.wav"), SPOKEN))
            check(f"{name}: a spoken capture states its length",
                  line == "bytes=65644 secs=4.10 end=timeout")
            line = summary("200 result=-1 (hangup) endpos=8000",
                           _wav(os.path.join(tmp, "c.wav"), 8000))
            check(f"{name}: a hang-up at the same length is not no-speech",
                  line == "bytes=16044 secs=1.00 end=hangup")
            line = summary("200 result=0 (timeout) endpos=80000",
                           _wav(os.path.join(tmp, "d.wav"), CAPPED))
            check(f"{name}: the record cap reads as the full window, not silence",
                  line == "bytes=160044 secs=10.00 end=timeout")
            check(f"{name}: a capture with one short sound in it is not the remainder",
                  "no-speech" not in summary(
                      SILENCE_REPLY, _wav(os.path.join(tmp, "e.wav"), SILENT + 1600)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_only_the_silence_cutoff_earns_the_flag() -> None:
    """Only RECORD's silence cutoff leaves the 1.02 s remainder, and it replies
    (timeout). A lost reply or a failure word beside a file of that size shows
    nothing about why it is that size, so the flag stays off; the word itself
    is logged, so a RECORD failure is visible rather than missing."""
    tmp = tempfile.mkdtemp()
    try:
        for name, summary in _twins():
            path = _wav(os.path.join(tmp, "f.wav"), SILENT)
            check(f"{name}: a lost reply logs no end and no flag",
                  summary("", path) == "bytes=16364 secs=1.02")
            check(f"{name}: a waitfor failure is named, and not called silence",
                  summary("200 result=-1 (waitfor) endpos=8160", path)
                  == "bytes=16364 secs=1.02 end=waitfor")
            check(f"{name}: a writefile failure is named beside the missing file",
                  summary("200 result=-1 (writefile)", os.path.join(tmp, "absent.wav"))
                  == "bytes=-1 end=writefile")
            check(f"{name}: only a bracketed word of letters is taken from the reply",
                  summary("200 result=-1 (bad word) endpos=8160", path)
                  == "bytes=16364 secs=1.02")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_missing_or_headerless_file_still_reads() -> None:
    tmp = tempfile.mkdtemp()
    try:
        for name, summary in _twins():
            check(f"{name}: no file is still bytes=-1, beside the reply's own word",
                  summary(SILENCE_REPLY, os.path.join(tmp, "absent.wav"))
                  == "bytes=-1 end=timeout")
            check(f"{name}: no file and no reply is bare bytes=-1",
                  summary("", os.path.join(tmp, "absent.wav")) == "bytes=-1")
            raw = os.path.join(tmp, "raw.wav")
            with open(raw, "wb") as fh:
                fh.write(b"\0" * 16364)
            check(f"{name}: with no readable header the size gives the length",
                  summary(SILENCE_REPLY, raw) == "bytes=16364 secs=1.02 end=timeout no-speech")
            # A RIFF header cut short: the breadcrumb runs before STT, inside the
            # call, so a parse error here must fall back, never raise.
            import struct
            odd = os.path.join(tmp, "odd.wav")
            with open(odd, "wb") as fh:
                fh.write(b"RIFF" + struct.pack("<I", 16340) + b"WAVEfmt "
                         + struct.pack("<I", 2) + b"\x01\x00" + b"\0" * 16342)
            check(f"{name}: a malformed header falls back to the size",
                  summary(SILENCE_REPLY, odd)
                  == "bytes=16364 secs=1.02 end=timeout no-speech")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_operator_and_the_shared_helper_agree() -> None:
    """The operator AGI imports nothing of ours, so it keeps its own copy. Two
    copies of one rule drift unless something holds them together; this does."""
    op, sp = _operator(), _speech()
    check("twins: the same remainder window", op.SILENT_REMAINDER_S == sp.SILENT_REMAINDER_S)
    tmp = tempfile.mkdtemp()
    try:
        cases = [(SILENCE_REPLY, SILENT), ("200 result=0 (timeout) endpos=32800", SPOKEN),
                 ("200 result=-1 (hangup) endpos=8000", 8000), ("", SILENT),
                 ("200 result=-1 (waitfor) endpos=8160", SILENT),
                 ("200 result=0 (timeout) endpos=80000", CAPPED), ("520 Invalid", 0)]
        for i, (reply, frames) in enumerate(cases):
            path = _wav(os.path.join(tmp, f"t{i}.wav"), frames)
            check(f"twins: {reply!r} with {frames} frames",
                  op._rec_summary(reply, path) == sp.rec_summary(reply, path))
        absent = os.path.join(tmp, "absent.wav")
        for reply in ("", "200 result=-1 (writefile)"):
            check(f"twins: a missing file with {reply!r}",
                  op._rec_summary(reply, absent) == sp.rec_summary(reply, absent))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_operator_logs_the_breadcrumb_for_a_real_recording() -> None:
    """Drives recognize() itself, with only Asterisk and whisper stood in for."""
    mod = _operator()
    tmp = tempfile.mkdtemp()
    mod.ASR_DIR = tmp
    sent = []

    def _agi(cmd):
        sent.append(cmd)
        if cmd.startswith("RECORD FILE"):
            _wav(cmd.split()[2] + ".wav", SILENT)
            return SILENCE_REPLY
        return "200 result=0"

    mod.agi = _agi
    mod.stream = lambda sf: None
    mod._stt_start = lambda args, wav: None      # whisper heard nothing
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            got = mod.recognize(1)
        check("operator: nothing heard is nothing usable", got == ("", ""))
        check("operator: RECORD still runs with the SILENCE_S cutoff",
              any(c.endswith(f"s={mod.SILENCE_S}") for c in sent if c.startswith("RECORD")))
        check("operator: the rec line carries the length and the flag",
              "[operator] rec attempt=1 bytes=16364 secs=1.02 end=timeout no-speech\n"
              in err.getvalue())
        check("operator: the recording is still deleted", not os.listdir(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_listen_now_says_what_it_recorded() -> None:
    """agi_speech.listen() serves the status, directory and announce menus and
    used to log nothing about a recording. It now leaves the same line as the
    operator, written before STT runs, so it cannot carry the transcript."""
    sp = _speech()
    tmp = tempfile.mkdtemp()
    sp.ASR_DIR = tmp

    def _agi(cmd):
        if cmd.startswith("RECORD FILE"):
            _wav(cmd.split()[2] + ".wav", SPOKEN)
            return "200 result=0 (timeout) endpos=32800"
        return "200 result=0"

    class _Proc:
        def communicate(self, timeout=None):
            return "weather report please\n", ""

        def kill(self):
            pass

    sp.agi = _agi
    sp.stream = lambda sf: None
    sp._stt_start = lambda cmd: _Proc()
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            text = sp.listen("status", 0)
        check("listen: still returns the transcript", text == "weather report please")
        check("listen: the rec line names the flow and states the length",
              "[status] rec attempt=0 bytes=65644 secs=4.10 end=timeout\n" in err.getvalue())
        check("listen: and does not quote what was said", "weather" not in err.getvalue())
        check("listen: the recording is still deleted", not os.listdir(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_docstring_states_the_silence_window_the_code_uses() -> None:
    """It said 2 s from v0.2.0 on. v0.2.2 made SILENCE_S 3 and v0.12.4 made it
    4, and neither change reached the one sentence a reader meets first."""
    mod = _operator()
    m = re.search(r"(\d+) s of silence", mod.__doc__ or "")
    check("operator: the docstring names a silence window", bool(m))
    check("operator: and it is SILENCE_S", bool(m) and int(m.group(1)) == mod.SILENCE_S)
    src = _OPERATOR_AGI.read_text()
    check("operator: the 64 kB/s silent-capture figure is gone", "64 kB/s" not in src)


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
