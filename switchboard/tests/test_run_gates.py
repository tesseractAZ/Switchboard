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

This pins the anti-pattern out — including the whisper-server RAM gate, a
multi-flag ``&&`` chain that idles only when NO speech feature is on. That gate was
previously exempted for spanning several lines, but the boot-race is identical: a
transient all-blank read would satisfy every ``! bashio::config.true`` clause and
permanently idle the resident recognizer. It now uses the explicit-``false`` form
too, and the lint below is multi-line-aware so any idle gate keying off
``bashio::config.true`` (single- or multi-flag) is caught.
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
    # Under pytest the print + counter are DECORATIVE — only the __main__
    # runner reads _failures, so a failing check would still 'pass' the
    # test. Assert too, so both harnesses actually enforce every check.
    assert cond, name


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

    # Multi-line-aware: an idle run script (one that `exec sleep infinity`s) must
    # not use `! bashio::config.true` ANYWHERE — that catches multi-flag `&&` idle
    # chains (e.g. the whisper-server RAM gate) the single-line regex above misses.
    multi = []
    for run in run_scripts:
        # Strip comment lines so a comment *describing* the anti-pattern (like the
        # one in whisper-server/run) isn't mistaken for a real gate.
        code = "\n".join(l for l in run.read_text().splitlines()
                         if not l.lstrip().startswith("#"))
        if "exec sleep infinity" in code and "! bashio::config.true" in code:
            multi.append(f"{run.parent.name}/run")
    check(f"no idle run-script uses `! bashio::config.true` (multi-flag; offenders: {multi or 'none'})",
          not multi)

    # Positive assertion: the console services (the ones that actually regressed)
    # AND the whisper-server RAM gate use the hardened explicit-false form.
    for svc, flag in [("console-web", "console_enabled"),
                      ("operator-console", "console_enabled"),
                      ("rtpmon", "link_health_enabled"),
                      ("devhealth", "device_health_enabled"),
                      ("wakeup-scheduler", "wakeup_enabled"),
                      ("whisper-server", "operator.enabled")]:
        txt = (_S6 / svc / "run").read_text()
        hardened = f'[ "$(bashio::config \'{flag}\')" = "false" ]' in txt
        check(f"{svc}/run idles only on explicit false for {flag}", hardened)


if __name__ == "__main__":
    test_no_unsafe_idle_gates()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    sys.exit(1 if _failures else 0)


# A VALUE read must go through switchboard-opt, never bashio::config: bashio
# parses /data/options.json directly and therefore BYPASSES the options overlay.
# This shipped as a real defect — devhealth read the cordless battery thresholds
# with bashio, so an overlay that set them merged cleanly, logged as "overriding",
# and changed nothing. Enable GATES deliberately stay on bashio (see above).
_VALUE_READ = re.compile(r"""^(?:export\s+)?[A-Z_]+="\$\(bashio::config\s+'([a-z_]+)'\)\"""")


def test_value_reads_use_switchboard_opt_not_bashio():
    offenders = []
    for run in sorted(_S6.glob("*/run")):
        for i, line in enumerate(run.read_text().splitlines(), 1):
            m = _VALUE_READ.match(line.strip())
            if m:
                offenders.append(f"{run.parent.name}/run:{i} reads '{m.group(1)}' via bashio::config")
    check(
        "run scripts: every VALUE read goes through switchboard-opt "
        f"(so the options overlay applies) — {len(offenders)} offender(s)",
        not offenders,
    )
    for o in offenders:
        print("   ", o)
    assert not offenders, "value reads bypassing the overlay: " + "; ".join(offenders)


# ── v0.70.0: an option is INERT until a run script EXPORTS it ────────────────
#
# The project's most-repeated footgun. Adding a feature knob takes FOUR edits —
# config.yaml options, config.yaml schema, translations/en.yaml, and an `export`
# in the service's run script — and the fourth is the one that gets forgotten,
# because everything still parses, the UI still shows the field, the tests still
# pass, and the service simply never sees the value.
#
# Unit tests cannot catch it: they set os.environ directly, so the code under
# test reads exactly the value the test just wrote no matter what the shell does.
# This asserts the SHELL side, which nothing else does.
def test_every_option_read_is_also_exported():
    root = Path(__file__).resolve().parents[1] / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d"
    assign = re.compile(r'^([A-Z_][A-Z0-9_]*)="\$\(switchboard-opt\s+([a-z0-9_.]+)\s*\)"', re.M)
    bad = []
    seen = 0
    for run in sorted(root.glob("*/run")):
        src = run.read_text()
        for var, opt in assign.findall(src):
            seen += 1
            # The captured value must reach the process. Either the shell var
            # itself is exported, or (the common idiom) it is defaulted into an
            # exported name: VAR="$(switchboard-opt x)"; export NAME="${VAR:-d}".
            exported = re.search(rf'^export\s+[A-Z_][A-Z0-9_]*="\$\{{{var}(:-[^}}]*)?\}}"', src, re.M) \
                or re.search(rf'^export\s+{var}\b', src, re.M)
            if not exported:
                bad.append(f"{run.parent.name}/run: reads option '{opt}' into ${var} but never exports it")
    check(f"run scripts: found switchboard-opt reads to check ({seen})", seen >= 5)
    check("run scripts: every option read reaches the process via export\n    "
          + "\n    ".join(bad), not bad)
