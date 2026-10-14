"""The speech synthesizer never reads spoken text as an option, nor its stdin.

    python3 -m pytest switchboard/tests/test_tts_argv.py

The text spoken back comes from callers (an announcement's transcript, the
assistant's reply). Every place that starts espeak-ng passes "--" before it and
closes stdin.
"""
import subprocess as _sp
import sys
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]
WEBUI = ADDON / "rootfs" / "usr" / "share" / "switchboard" / "webui"
if str(WEBUI) not in sys.path:
    sys.path.insert(0, str(WEBUI))
TEXT = "-v xx --stdout"


def _capture(module):
    calls = []

    def run(cmd, **kw):
        calls.append((list(cmd), kw))
        raise _sp.SubprocessError("captured")
    module.subprocess = types.SimpleNamespace(run=run, DEVNULL=_sp.DEVNULL,
                                              SubprocessError=_sp.SubprocessError,
                                              TimeoutExpired=_sp.TimeoutExpired)
    return calls


def _assert_safe(calls):
    assert calls, "espeak-ng was never started"
    cmd, kw = calls[0]
    assert cmd[-2:] == ["--", TEXT], cmd
    assert kw.get("stdin") is _sp.DEVNULL, kw


def test_announce_audio(tmp_path):
    import announce_audio
    calls = _capture(announce_audio)
    announce_audio.build_announcement(TEXT, str(tmp_path / "a.wav"))
    _assert_safe(calls)


def test_announce_asterisk(tmp_path):
    import announce_asterisk
    calls = _capture(announce_asterisk)
    announce_asterisk.build_announcement_8k(TEXT, str(tmp_path / "a.wav"))
    _assert_safe(calls)


def test_switchboard_tts(tmp_path):
    tts = SourceFileLoader("sbtts", str(ADDON / "rootfs" / "usr" / "bin" / "switchboard-tts")).load_module()
    tts._espeak_bin = lambda: "espeak-ng"
    calls = _capture(tts)
    tts.synthesize(TEXT, str(tmp_path / "out"))
    _assert_safe(calls)
