"""Guards the s6 service run-script enable-gates against a boot-race regression.

    python3 switchboard/tests/test_run_gates.py

Each optional longrun idles (``exec sleep infinity``) when its feature is turned
off. The idle branch must key off an EXPLICIT ``false``, not ``bashio::config.true``:
bashio can momentarily read a blank options value during a config reload / at boot,
and ``bashio::config.true`` treats that empty read as false — which would then
``exec sleep infinity`` and PERMANENTLY idle an ENABLED service (s6 sees an idle
process as "successfully started" and never restarts it). This actually happened to
the console-web terminal (v0.30.1). So: a single-flag idle gate must use
``[ "$(bashio::config 'flag')" = "false" ]``, never ``! bashio::config.true 'flag'``.

This pins the anti-pattern out. The whisper-server RAM gate is a deliberate
multi-flag ``&&`` chain (idle only when NO speech feature is on) spread across
several lines, so it does not match the single-line ``; then`` form this guards and
is intentionally not flagged.
"""
import re
import sys
from pathlib import Path

_S6 = (Path(__file__).resolve().parents[1]
       / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d")
_failures = 0

# A self-contained single-line negated gate: `if ! bashio::config.true 'flag'; then`
_UNSAFE_GATE = re.compile(r"^\s*if\s*!\s*bashio::config\.true\s*'([^']+)'\s*;\s*then\s*$")


def check(name, cond):
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def test_no_unsafe_idle_gates():
    run_scripts = sorted(_S6.glob("*/run"))
    check(f"found s6 run scripts to scan ({len(run_scripts)})", len(run_scripts) >= 6)

    offenders = []
    for run in run_scripts:
        lines = run.read_text().splitlines()
        for i, line in enumerate(lines):
            m = _UNSAFE_GATE.match(line)
            if not m:
                continue
            # Does this gate's block idle the service? Look at the next few lines.
            block = "\n".join(lines[i + 1:i + 5])
            if "exec sleep infinity" in block:
                svc = run.parent.name
                offenders.append(f"{svc}/run: `! bashio::config.true '{m.group(1)}'` "
                                 f"idle gate (use [ \"$(bashio::config '{m.group(1)}')\" = \"false\" ])")

    check(f"no single-flag idle gate keys off bashio::config.true (offenders: {offenders or 'none'})",
          not offenders)

    # Positive assertion: the console services (the ones that actually regressed)
    # use the hardened explicit-false form.
    for svc, flag in [("console-web", "console_enabled"),
                      ("operator-console", "console_enabled"),
                      ("rtpmon", "link_health_enabled"),
                      ("devhealth", "device_health_enabled"),
                      ("wakeup-scheduler", "wakeup_enabled")]:
        txt = (_S6 / svc / "run").read_text()
        hardened = f'[ "$(bashio::config \'{flag}\')" = "false" ]' in txt
        check(f"{svc}/run idles only on explicit false for {flag}", hardened)


if __name__ == "__main__":
    test_no_unsafe_idle_gates()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    sys.exit(1 if _failures else 0)
