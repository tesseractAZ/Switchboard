"""Tests for switchboard-stt's resident-whisper-server dispatch + fallback.

Plain python3, no deps:

    python3 switchboard/tests/test_switchboard_stt_server.py

Monkeypatches http.client so no real server/model is needed. Verifies the
timeout-budget contract that keeps server-timeout + CLI-fallback under the AGI's
~25s hard kill: a connect-phase miss falls back to whisper-cli, but a POST-connect
read hang returns '' (the AGI re-records) instead of stacking a 20s CLI run.
"""
import socket
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
# switchboard-stt imports `match`/`lights_match` from the operator dir.
sys.path.insert(0, str(ROOT / "rootfs" / "usr" / "share" / "switchboard" / "operator"))
STT = ROOT / "rootfs" / "usr" / "bin" / "switchboard-stt"
stt = SourceFileLoader("switchboard_stt", str(STT)).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def _fake_conn(behavior: str, sink: dict):
    """A stand-in http.client.HTTPConnection whose behavior is scripted."""
    class _Resp:
        def __init__(self, status, data):
            self.status = status
            self._data = data

        def read(self):
            return self._data

    class _Conn:
        def __init__(self, host, port, timeout=None):
            self.sock = types.SimpleNamespace(settimeout=lambda t: sink.__setitem__("read_to", t))

        def connect(self):
            if behavior == "refused":
                raise ConnectionRefusedError("connection refused")

        def request(self, method, url, body=None, headers=None):
            sink["url"] = url
            sink["body"] = body
            sink["headers"] = headers

        def getresponse(self):
            if behavior == "hang":
                raise socket.timeout("read timed out")
            if behavior == "reset":
                raise ConnectionResetError("server crashed mid-request")
            if behavior == "http500":
                return _Resp(500, b"upstream error")
            if behavior == "okempty":
                return _Resp(200, b'{"text":""}')
            return _Resp(200, b'{"text":"[BLANK_AUDIO] Cordless."}')

        def close(self):
            pass

    return _Conn


def _run(behavior: str, prompt: str = "kitchen office"):
    """Drive stt.transcribe() with a scripted fake server; return
    (result, cli_calls, sink)."""
    sink = {"cli": 0}
    orig_conn = stt.http.client.HTTPConnection
    orig_cli = stt._transcribe_cli
    stt.http.client.HTTPConnection = _fake_conn(behavior, sink)

    def fake_cli(wav16, p):
        sink["cli"] += 1
        return "CLI-RESULT"

    stt._transcribe_cli = fake_cli
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(b"RIFF....WAVEfmt ")
            wav = tf.name
        result = stt.transcribe(wav, prompt)
    finally:
        stt.http.client.HTTPConnection = orig_conn
        stt._transcribe_cli = orig_cli
    return result, sink["cli"], sink


def test_server_ok_no_cli() -> None:
    r, cli, _ = _run("ok")
    check("server 200 -> normalized text, whisper-cli NOT run", r == "Cordless." and cli == 0)


def test_connect_refused_falls_back_to_cli() -> None:
    r, cli, _ = _run("refused")
    check("connect refused (server down) -> falls back to whisper-cli",
          r == "CLI-RESULT" and cli == 1)


def test_read_hang_returns_empty_without_cli() -> None:
    # THE budget guard: a post-connect hang must NOT stack a 20s CLI run under
    # the AGI's 25s kill — return '' so the AGI's re-record loop handles it.
    r, cli, _ = _run("hang")
    check("server accepted then hung -> '' and NO whisper-cli (budget guard)",
          r == "" and cli == 0)


def test_http_error_returns_empty_without_cli() -> None:
    # A non-200 arrives after connect; falling back to a 20s CLI here could stack
    # over the read budget and blow the 25s AGI kill, so return '' (re-record).
    r, cli, _ = _run("http500")
    check("server HTTP 500 -> '' and NO whisper-cli (budget-safe)", r == "" and cli == 0)


def test_midrequest_reset_returns_empty_without_cli() -> None:
    # The reviewed defect: a mid-request reset (ConnectionResetError — an OSError,
    # NOT a socket.timeout) must NOT fall through to a stacked 20s CLI run.
    r, cli, _ = _run("reset")
    check("server reset mid-request -> '' and NO whisper-cli (the budget fix)",
          r == "" and cli == 0)


def test_empty_transcript_not_double_charged() -> None:
    r, cli, _ = _run("okempty")
    check("server 200 empty text is a VALID '' (silence) -> no CLI re-charge",
          r == "" and cli == 0)


def test_multipart_prompt_only_when_bias_set() -> None:
    _, _, sink = _run("ok", prompt="kitchen office")
    check("multipart carries the prompt field when bias set",
          b'name="prompt"' in sink["body"] and b"kitchen office" in sink["body"])
    _, _, sink2 = _run("ok", prompt="")
    check("multipart omits the prompt field when bias empty",
          b'name="prompt"' not in sink2["body"])
    check("multipart carries the WAV file + json response_format",
          b'filename="a.wav"' in sink["body"] and b"json" in sink["body"])


def test_normalize_identical_on_server_path() -> None:
    # The [BLANK_AUDIO] bracket whisper emits must be stripped on the server path
    # exactly as on the CLI path (shared _normalize).
    r, _, _ = _run("ok")
    check("server transcript passes through _normalize (brackets stripped)",
          "[" not in r and r == "Cordless.")


def test_server_disabled_uses_cli() -> None:
    orig = stt.WHISPER_SERVER
    stt.WHISPER_SERVER = False
    try:
        r, cli, _ = _run("ok")  # even with a working fake server, must skip it
    finally:
        stt.WHISPER_SERVER = orig
    check("SW_WHISPER_SERVER off -> whisper-cli only (kill-switch)",
          r == "CLI-RESULT" and cli == 1)


if __name__ == "__main__":
    test_server_ok_no_cli()
    test_connect_refused_falls_back_to_cli()
    test_read_hang_returns_empty_without_cli()
    test_http_error_returns_empty_without_cli()
    test_midrequest_reset_returns_empty_without_cli()
    test_empty_transcript_not_double_charged()
    test_multipart_prompt_only_when_bias_set()
    test_normalize_identical_on_server_path()
    test_server_disabled_uses_cli()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
