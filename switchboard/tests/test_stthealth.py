"""stthealth.status() config-awareness + probe interpretation (no real socket).

    python3 switchboard/tests/test_stthealth.py

The point of stthealth is to report 'disabled' when STT is intentionally off (so a
phones-only setup never cries wolf), 'up' when the resident model answers, and
'down' only when it SHOULD be running but the loopback probe fails (= slow
per-call whisper-cli fallback). We monkeypatch probe() so no port is touched.
"""
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

SH = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
      / "switchboard" / "webui" / "stthealth.py")
sh = SourceFileLoader("stthealth", str(SH)).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def test_status() -> None:
    all_off = {"operator": {"enabled": False}, "wakeup_enabled": False,
               "automation_enabled": False, "status_enabled": False,
               "announce_enabled": False, "directory_enabled": False}

    # 'disabled' short-circuits BEFORE probing, regardless of the port state.
    sh.probe = lambda timeout=1.0: True
    check("stt_resident=False -> disabled", sh.status({"stt_resident": False}) == "disabled")
    check("stt_resident='false' string -> disabled", sh.status({"stt_resident": "false"}) == "disabled")
    check("operator + all features off -> disabled", sh.status(all_off) == "disabled")

    # Enabled (defaults) + probe up -> 'up'.
    sh.probe = lambda timeout=1.0: True
    check("default opts + probe up -> up", sh.status({}) == "up")
    check("operator on + probe up -> up", sh.status({"operator": {"enabled": True}}) == "up")

    # Enabled + probe down -> 'down' (running on the slow CLI fallback).
    sh.probe = lambda timeout=1.0: False
    check("default opts + probe down -> down", sh.status({}) == "down")
    # A single feature on is enough to be 'enabled' (so 'down' not 'disabled').
    one_on = {**all_off, "status_enabled": True}
    check("one feature on + probe down -> down", sh.status(one_on) == "down")


if __name__ == "__main__":
    test_status()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    sys.exit(1 if _failures else 0)
