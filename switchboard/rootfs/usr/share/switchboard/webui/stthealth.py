"""Whisper-server (resident STT) health probe — framework-free stdlib, importable
by both app.py and console.py (which already put the webui dir on sys.path).

The whisper.cpp ``whisper-server`` loads the ~142 MB model BEFORE it starts
listening and binds loopback only (127.0.0.1:8126). So a port that answers ⇒ the
model is resident and ready; connection refused ⇒ the server is idled
(``stt_resident:false`` or no speech feature → ``sleep infinity``), still loading,
or crashed — in which case switchboard-stt silently falls back to the per-call
whisper-cli (correct, but slow: it reloads the model each utterance and can blow
the AGI's ~25 s kill). We interpret the probe AGAINST config so an intentionally
idle server reports ``disabled``, never ``down`` — no crying wolf on a phones-only
setup with STT turned off.
"""
import http.client
import os
import socket

HOST = os.environ.get("SW_WHISPER_SERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("SW_WHISPER_PORT", "8126") or "8126")

# Speech features that keep the resident server up. This MIRRORS the RAM gate in
# whisper-server/run, and a mirror is a copy: this one drifted. `assistant_enabled`
# was added to the run script with the v0.69.0 dial-47 assistant and never added
# here, so on an install where the assistant is the only speech feature turned on,
# `_enabled()` returned False, `status()` short-circuited to "disabled", and the
# probe could report the recognizer as switched off but never as DOWN. The health
# check for the feature was blind to exactly the install that needed it.
#
# `test_the_stt_feature_gate_mirrors_the_run_script` now DERIVES this list from
# the run script and fails on any divergence, so the copy cannot silently lag
# again — a shell script is invisible to Python and only a test can see both.
_FEATURE_FLAGS = ("wakeup_enabled", "automation_enabled", "status_enabled",
                  "announce_enabled", "directory_enabled", "assistant_enabled")


def _truthy(value, default: bool) -> bool:
    """options.json values may be real bools (JSON) or strings; treat missing as
    the schema default and only an explicit false-ish token as False."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


def probe(timeout: float = 1.0) -> bool:
    """True if the whisper-server answers on the loopback port (resident model up).
    A bare GET / is enough — whisper.cpp's httplib serves its index without blocking
    behind an in-flight inference. Any connect/timeout/HTTP error ⇒ not resident."""
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    try:
        conn.connect()
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
        return 200 <= resp.status < 500
    except (OSError, socket.timeout, http.client.HTTPException):
        return False
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _enabled(opts: dict) -> bool:
    """Should the resident server be running? False only when STT is deliberately
    off (stt_resident:false, or operator AND every speech feature disabled)."""
    if not _truthy(opts.get("stt_resident"), True):
        return False
    operator_on = _truthy((opts.get("operator") or {}).get("enabled"), True)
    features_on = any(_truthy(opts.get(f), True) for f in _FEATURE_FLAGS)
    return operator_on or features_on


def status(opts: dict, timeout: float = 1.0) -> str:
    """'disabled' (intentionally idle — do not alarm), 'up' (resident model ready),
    or 'down' (probe failed → running on the slow per-call whisper-cli fallback)."""
    if not _enabled(opts):
        return "disabled"
    return "up" if probe(timeout) else "down"
