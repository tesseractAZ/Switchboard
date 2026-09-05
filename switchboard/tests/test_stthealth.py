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
    # Under pytest the print + counter are DECORATIVE — only the __main__
    # runner reads _failures, so a failing check would still 'pass' the
    # test. Assert too, so both harnesses actually enforce every check.
    assert cond, name


def test_status() -> None:
    # v0.77.0 — `assistant_enabled` joined the gate. Omitting it here does not
    # make it off: _truthy() treats a missing key as the SCHEMA DEFAULT, so this
    # fixture stopped meaning "all off" the moment the flag was added, which is
    # the same drift the mirror itself suffered.
    all_off = {"operator": {"enabled": False}, "wakeup_enabled": False,
               "automation_enabled": False, "status_enabled": False,
               "announce_enabled": False, "directory_enabled": False,
               "assistant_enabled": False}

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


def test_the_stt_feature_gate_mirrors_the_run_script() -> None:
    """F34. Two copies of one gate, in two languages, and one of them drifted.

    `whisper-server/run` idles the resident recognizer when every speech feature
    is off. `stthealth._FEATURE_FLAGS` restates that list so the probe can report
    "disabled" rather than "down" — and it was restating a stale version of it.
    `assistant_enabled` reached the run script with the v0.69.0 dial-47 assistant
    and never reached here, so on an assistant-only install `_enabled()` returned
    False and `status()` short-circuited to "disabled" without ever probing. The
    health check could say the recognizer was switched off. It could not say it
    had died.

    Restating the list correctly fixes today. Deriving it from the run script is
    what stops it drifting again: Python cannot see a shell script, unit tests
    set the flags directly, and nothing else in the build compares the two.
    """
    import re as _re
    from pathlib import Path as _P
    run = (_P(__file__).resolve().parents[1] / "rootfs" / "etc" / "s6-overlay"
           / "s6-rc.d" / "whisper-server" / "run").read_text()
    # The RAM gate is a chain of `bashio::config 'NAME'` tests inside one `if`.
    # Anchor on operator.enabled, which appears only there — slicing from the
    # first `if [ "$(bashio::config` instead lands on the earlier stt_resident
    # master-switch gate, a one-clause `if` that mirrors nothing. That version
    # of this test "failed" for a reason that had nothing to do with the flags.
    op = run.index("bashio::config 'operator.enabled'")
    gate = run[run.rindex("if [", 0, op):]
    gate = gate[:gate.index("then")]
    in_script = {m for m in _re.findall(r"bashio::config '([a-z_.]+)'", gate)}
    # operator.enabled is nested config, not a flat feature flag; stthealth reads
    # it separately. Everything else in the gate must appear in the mirror.
    flat = {f for f in in_script if "." not in f}
    missing = flat - set(sh._FEATURE_FLAGS)
    extra = set(sh._FEATURE_FLAGS) - flat
    check(f"stt gate: no flag in the run script is missing here ({missing})",
          not missing)
    check(f"stt gate: and none here is absent from the run script ({extra})",
          not extra)
    check("stt gate: precondition — the run script really does gate on the "
          "assistant", "assistant_enabled" in flat)
    check("stt gate: precondition — the slice found the FEATURE gate, not the "
          "one-clause master switch", len(flat) >= 6)
    # The master switch is a separate `if` and stthealth handles it separately.
    check("stt gate: and the master switch is honoured too",
          sh.status({"stt_resident": False}) == "disabled")


def test_an_assistant_only_install_can_report_the_recognizer_as_DOWN() -> None:
    """The consequence of the drift, stated as behaviour rather than as a list.

    Dial 47 is the only speech feature on. The recognizer is dead. Before
    v0.77.0 `_FEATURE_FLAGS` did not mention the assistant, so `_enabled()` saw
    nothing enabled, `status()` short-circuited to "disabled", and the probe
    never ran — the health check reported the recognizer as switched off while
    the one feature depending on it was broken.
    """
    only_assistant = {"operator": {"enabled": False}, "wakeup_enabled": False,
                      "automation_enabled": False, "status_enabled": False,
                      "announce_enabled": False, "directory_enabled": False,
                      "assistant_enabled": True}
    sh.probe = lambda timeout=1.0: False
    check("assistant-only: a dead recognizer reads DOWN, not 'disabled'",
          sh.status(only_assistant) == "down")
    sh.probe = lambda timeout=1.0: True
    check("assistant-only: a live recognizer reads UP",
          sh.status(only_assistant) == "up")
