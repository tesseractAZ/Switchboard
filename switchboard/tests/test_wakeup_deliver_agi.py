"""The wake-up delivery AGI's mode gating.

    python3 -m pytest switchboard/tests/test_wakeup_deliver_agi.py

The AGI is invoked TWICE per delivery: once in "scene" mode on answer, once in
"speak" mode after the time announcement. Before that split it ran only after
the greeting and the time, so hanging up during either meant the configured
scene never fired -- and hanging up is exactly what someone does once a wake-up
call has already woken them. Live evidence: the scene fired on 1 of 3 delivered
wake-ups.

The dialplan wiring is asserted in test_switchboard_config.py. THIS file asserts
the AGI actually honours the mode it is given, which a source scan cannot.
"""
import sys
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_AGI = _ROOT / "rootfs" / "var" / "lib" / "asterisk" / "agi-bin" / "switchboard-wakeup-deliver.agi"

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    assert cond, name


def _load(mode_argv, *, scene="scene.wakeup_master_bedroom", weather=True):
    """Load the AGI with its dependencies stubbed and run main() once.

    Returns (services_called, spoken_lines)."""
    services, spoken = [], []

    sp = types.ModuleType("agi_speech")
    sp.read_env = lambda: {"agi_channel": "PJSIP/19-00000001"}
    sp.load_features = lambda: {"wakeup": {"weather": weather, "calendar": ""}}
    sp.channel_ext = lambda env: "19"
    sp.wakeup_scene_for = lambda wk, ext: scene
    sp.say = lambda text: spoken.append(text) or True
    sp.log = lambda *a, **k: None

    ha = types.ModuleType("ha_client")
    ha.call_service = lambda dom, svc, data: services.append((dom, svc, data))

    reports = types.ModuleType("ha_reports")
    reports.weather_line = lambda: "Sunny, high of 100."
    reports.next_event_line = lambda cal: ""

    saved_mods = {k: sys.modules.get(k) for k in ("agi_speech", "ha_client", "ha_reports")}
    saved_argv = sys.argv[:]
    sys.modules["agi_speech"] = sp
    sys.modules["ha_client"] = ha
    sys.modules["ha_reports"] = reports
    sys.argv = ["switchboard-wakeup-deliver.agi"] + list(mode_argv)
    try:
        mod = SourceFileLoader("wakeup_deliver_agi", str(_AGI)).load_module()
        mod.main()
    finally:
        sys.argv = saved_argv
        for k, v in saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return services, spoken


def test_scene_mode_fires_the_scene_and_says_nothing() -> None:
    """Scene mode runs on ANSWER, before a word is spoken. If it also spoke, the
    weather would play before the greeting."""
    services, spoken = _load(["scene"])
    check("scene mode: the scene fired",
          [s for s in services if s[0] == "scene" and s[1] == "turn_on"])
    check("scene mode: nothing was spoken", spoken == [])


def test_speak_mode_speaks_and_does_not_refire_the_scene() -> None:
    """Speak mode runs after the time. Re-firing the scene here would turn the
    lights on twice per wake-up."""
    services, spoken = _load(["speak"])
    check("speak mode: the scene was NOT fired again", services == [])
    check("speak mode: the weather was spoken",
          any("weather" in s.lower() for s in spoken))


def test_no_argument_keeps_the_original_both_behaviour() -> None:
    """A bare AGI() call must still do both, so an older dialplan that has not
    been regenerated does not silently lose its scene."""
    services, spoken = _load([])
    check("no-arg: the scene fired", len(services) == 1)
    check("no-arg: the weather was spoken", len(spoken) >= 1)


def test_scene_mode_is_a_no_op_when_no_scene_is_configured() -> None:
    services, spoken = _load(["scene"], scene="")
    check("scene mode: no scene configured -> no service call", services == [])
    check("scene mode: and still says nothing", spoken == [])


if __name__ == "__main__":
    for fn in (test_scene_mode_fires_the_scene_and_says_nothing,
               test_speak_mode_speaks_and_does_not_refire_the_scene,
               test_no_argument_keeps_the_original_both_behaviour,
               test_scene_mode_is_a_no_op_when_no_scene_is_configured):
        fn()
    print("FAILURES:", _failures)
    raise SystemExit(1 if _failures else 0)
