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

    Returns (services_called, spoken_lines, delivery_records)."""
    services, spoken, recorded = [], [], []

    sp = types.ModuleType("agi_speech")
    sp.read_env = lambda: {"agi_channel": "PJSIP/19-00000001"}
    sp.load_features = lambda: {"wakeup": {"weather": weather, "calendar": ""}}
    sp.channel_ext = lambda env: "19"
    sp.wakeup_scene_for = lambda wk, ext: scene
    sp.say = lambda text: spoken.append(text) or True
    sp.log = lambda *a, **k: None

    ha = types.ModuleType("ha_client")
    ha.call_service = lambda dom, svc, data: services.append((dom, svc, data))

    # The AGI writes delivery milestones. Stubbing it here rather than letting
    # `import delivery` fall through to the real module is what makes the
    # `spoken` milestone testable at all: without a stub the import may or may
    # not resolve depending on sys.path, and a mutant that deleted the record
    # call survived the whole suite.
    dl = types.ModuleType("delivery")
    dl.record = lambda ext, kind, outcome, **kw: recorded.append(
        (ext, kind, outcome)) or True

    reports = types.ModuleType("ha_reports")
    reports.weather_line = lambda: "Sunny, high of 100."
    reports.next_event_line = lambda cal: ""

    saved_mods = {k: sys.modules.get(k)
                  for k in ("agi_speech", "ha_client", "ha_reports", "delivery")}
    saved_argv = sys.argv[:]
    sys.modules["agi_speech"] = sp
    sys.modules["ha_client"] = ha
    sys.modules["ha_reports"] = reports
    sys.modules["delivery"] = dl
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
    return services, spoken, recorded


def test_scene_mode_fires_the_scene_and_says_nothing() -> None:
    """Scene mode runs on ANSWER, before a word is spoken. If it also spoke, the
    weather would play before the greeting."""
    services, spoken, _ = _load(["scene"])
    check("scene mode: the scene fired",
          [s for s in services if s[0] == "scene" and s[1] == "turn_on"])
    check("scene mode: nothing was spoken", spoken == [])


def test_speak_mode_speaks_and_does_not_refire_the_scene() -> None:
    """Speak mode runs after the time. Re-firing the scene here would turn the
    lights on twice per wake-up."""
    services, spoken, _ = _load(["speak"])
    check("speak mode: the scene was NOT fired again", services == [])
    check("speak mode: the weather was spoken",
          any("weather" in s.lower() for s in spoken))


def test_no_argument_keeps_the_original_both_behaviour() -> None:
    """A bare AGI() call must still do both, so an older dialplan that has not
    been regenerated does not silently lose its scene."""
    services, spoken, _ = _load([])
    check("no-arg: the scene fired", len(services) == 1)
    check("no-arg: the weather was spoken", len(spoken) >= 1)


def test_scene_mode_is_a_no_op_when_no_scene_is_configured() -> None:
    services, spoken, _ = _load(["scene"], scene="")
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


def test_the_milestones_say_what_actually_happened() -> None:
    """`answered` means picked up. `spoken` means the time was played.

    Through v0.76.0 there was only `answered`, written on the SCENE pass — which
    the dialplan reaches immediately after Answer() and before a single word is
    played. The scheduler joined on it and therefore treated a pickup as a
    delivery. On 2026-09-02 at 06:15:21 the cordless answered, dropped one
    second later during the Wait before the greeting, transmitted ZERO audio
    packets, and the wake-up was filed as answered and consumed.

    The two passes must write DIFFERENT milestones, because they mean different
    things and only one of them means the alarm went off.
    """
    _, _, scene_recs = _load(["scene"])
    check("milestones: the scene pass records the ANSWER",
          [r for r in scene_recs if r[2] == "answered"])
    check("milestones: and does NOT claim anything was spoken",
          not [r for r in scene_recs if r[2] == "spoken"])

    _, _, heard_recs = _load(["heard"])
    check("milestones: the heard pass records SPOKEN",
          [r for r in heard_recs if r[2] == "spoken"])
    check("milestones: and does not re-record the answer (one pickup, one record)",
          not [r for r in heard_recs if r[2] == "answered"])
    check("milestones: recorded against the right extension and kind",
          ("19", "wakeup", "spoken") in heard_recs)

    _, _, speak_recs = _load(["speak"])
    check("milestones: the speak pass writes no milestone of its own",
          speak_recs == [])


def test_the_milestone_pass_touches_nothing_that_can_hang() -> None:
    """It runs INSIDE the call, between the greeting and the time.

    Anything it did here would be dead air the sleeper listens to, and anything
    that reached the network could hang the delivery it exists to record. So the
    pass must fire no scene and speak nothing — the record is all it does.
    """
    services, spoken, recorded = _load(["heard"])
    check("heard mode: no scene fired", services == [])
    check("heard mode: nothing spoken", spoken == [])
    check("heard mode: but the milestone IS written",
          [r for r in recorded if r[2] == "spoken"])


def test_the_milestone_fires_before_the_time_not_after() -> None:
    """WHERE it runs decides whether ordinary behaviour reads as failure.

    Asterisk abandons the extension the instant the channel drops, so the
    milestone's position in the dialplan IS its definition: everything before it
    is proven to have played. After the greeting, a sleeper who hangs up the
    moment they hear "Good morning" is correctly counted as woken. After
    SayUnixTime, that same person is counted as undelivered and earns a re-ring
    plus a Do-Not-Disturb-bypassing push at six in the morning.

    A source scan of the AGI cannot see this; it is a property of the generated
    dialplan, which is where the ordering actually lives.
    """
    import sys as _s
    from importlib.machinery import SourceFileLoader as _SFL
    _s.path.insert(0, str(_ROOT / "rootfs" / "usr" / "bin"))
    sbc = _SFL("switchboard_config",
               str(_ROOT / "rootfs" / "usr" / "bin" / "switchboard-config")).load_module()
    rooms = sbc.valid_rooms([{"ext": "19", "name": "Cordless", "secret": "s1"}])
    e = sbc.render_extensions({"rooms": rooms, "wakeup": {"enabled": True}})
    # Scope to [wakeup-deliver]. SayUnixTime also appears in the talking-clock
    # context, and searching the whole file found THAT one — a green test that
    # was comparing offsets in two unrelated contexts.
    start = e.index("[wakeup-deliver]")
    ctx = e[start:e.index("\n[", start + 1)]
    heard = ctx.index("switchboard-wakeup-deliver.agi,heard")
    greeting = ctx.index("Playback(switchboard/sw-wakeup-greeting)")
    time_stage = ctx.index("SayUnixTime")
    check("ordering: the milestone runs AFTER the greeting", greeting < heard)
    check("ordering: and BEFORE the time is read", heard < time_stage)
