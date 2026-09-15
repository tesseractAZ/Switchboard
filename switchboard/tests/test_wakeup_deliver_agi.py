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


# --------------------------------------------------------------------------- #
# 2026-09-14 — the scene must not hold the greeting hostage.
# --------------------------------------------------------------------------- #
import os as _os
import re as _re
import signal as _signal
import subprocess as _subprocess
import time as _time

_WEBUI = _ROOT / "rootfs" / "usr" / "share" / "switchboard" / "webui"
_OPERATOR = _ROOT / "rootfs" / "usr" / "share" / "switchboard" / "operator"
# How long the stand-in Home Assistant takes to fire the scene. The blocking step
# measured 0.7 to 3.8 s live; two seconds is inside that range and far longer
# than a Python AGI takes to start and exit.
_SCENE_SECONDS = 2.0


def _run_scene_pass(tmp_path, argv):
    """Run the REAL AGI as its own process, the way Asterisk does.

    Asterisk decides an AGI has finished when its stdout reaches EOF, so that is
    what this measures — not the process exit, which a detached grandchild does
    not affect. The Home Assistant stand-in takes _SCENE_SECONDS and then writes
    a mark; the real agi_speech and delivery modules are used, with only the
    features file (a fixed container path) and HA replaced.

    Returns (seconds to EOF, stdout, whether the scene had fired by EOF, the mark
    path, the delivery ledger path, the process).
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "agi_speech.py").write_text(
        "import importlib.util, os\n"
        "_s = importlib.util.spec_from_file_location(\n"
        "    '_real_agi_speech', os.environ['SW_TEST_AGI_SPEECH'])\n"
        "_m = importlib.util.module_from_spec(_s)\n"
        "_s.loader.exec_module(_m)\n"
        "globals().update({k: v for k, v in vars(_m).items()\n"
        "                  if not k.startswith('__')})\n"
        "def load_features():\n"
        "    return {'wakeup': {'scene': 'scene.wakeup_test'}}\n")
    (stubs / "ha_client.py").write_text(
        "import os, time\n"
        "def call_service(domain, service, data):\n"
        "    time.sleep(float(os.environ['SW_TEST_SCENE_SECONDS']))\n"
        "    with open(os.environ['SW_TEST_SCENE_MARK'], 'w') as fh:\n"
        "        fh.write(domain + '.' + service + ' ' + data['entity_id'])\n"
        "    return True\n")
    mark = tmp_path / "scene-fired"
    ledger = tmp_path / "delivery-outcomes.jsonl"
    env = dict(_os.environ,
               PYTHONPATH=_os.pathsep.join([str(stubs), str(_WEBUI)]),
               PYTHONDONTWRITEBYTECODE="1",
               SW_TEST_AGI_SPEECH=str(_OPERATOR / "agi_speech.py"),
               SW_TEST_SCENE_SECONDS=str(_SCENE_SECONDS),
               SW_TEST_SCENE_MARK=str(mark),
               SWITCHBOARD_DELIVERY_OUTCOME=str(ledger))
    with open(tmp_path / "stderr.txt", "wb") as err:
        t0 = _time.monotonic()
        # A session of its own, standing in for the channel's process group.
        proc = _subprocess.Popen([sys.executable, str(_AGI)] + list(argv),
                                 stdin=_subprocess.PIPE, stdout=_subprocess.PIPE,
                                 stderr=err, env=env, start_new_session=True)
        proc.stdin.write(b"agi_channel: PJSIP/19-00000012\nagi_arg_1: scene\n\n")
        proc.stdin.close()
        out = proc.stdout.read()
        eof = _time.monotonic() - t0
        fired_by_eof = mark.exists()
        proc.wait(timeout=10)
    return eof, out, fired_by_eof, mark, ledger, proc


def _wait_for(path, seconds):
    deadline = _time.monotonic() + seconds
    while not path.exists() and _time.monotonic() < deadline:
        _time.sleep(0.05)
    return path.exists()


def test_the_scene_fires_after_the_agi_has_already_returned(tmp_path) -> None:
    """★ THE DEAD AIR, driven as a real process.

    2026-09-14: the scene pass waited on Home Assistant before the dialplan could
    play a word — 1.7 to 3.8 s of silence after each pickup — and a pickup that
    hung up 2.0 s in cut the AGI off inside the HTTP call.

    Detached, the AGI must return while the scene is still in flight, must have
    written `answered` before it did, and the scene must still fire after the
    call's whole process group is sent the hangup signal. Each of the three
    mechanisms has a mutant this catches: no detach (EOF waits), no setsid (the
    SIGHUP kills the scene), and stdout left open (EOF waits for the grandchild).
    """
    eof, out, fired_by_eof, mark, ledger, proc = _run_scene_pass(
        tmp_path, ["scene", "detach"])
    try:
        _os.killpg(proc.pid, _signal.SIGHUP)
    except (ProcessLookupError, PermissionError):
        pass                        # nothing left in the group: the scene is not in it
    import json as _json
    answered = [_json.loads(l)["outcome"] for l in ledger.read_text().splitlines()
                if l.strip()] if ledger.exists() else []
    fired = _wait_for(mark, _SCENE_SECONDS + 8)

    check(f"detach: the AGI returned while HA was still busy ({eof:.2f}s)",
          eof < _SCENE_SECONDS * 0.6 and not fired_by_eof)
    check("detach: the scene pass sent Asterisk no commands", out == b"")
    check("detach: `answered` was written before the AGI returned",
          answered == ["answered"])
    check("detach: the scene still fired, after the hangup signal",
          fired and mark.read_text() == "scene.turn_on scene.wakeup_test")


def test_the_harness_can_see_a_scene_pass_that_blocks(tmp_path) -> None:
    """The self-check. Without `detach` the same process must hold its stdout
    until the scene has fired; if this harness could not see that, the test above
    would pass for the code it exists to catch."""
    eof, out, fired_by_eof, mark, ledger, proc = _run_scene_pass(tmp_path, ["scene"])
    check(f"inline: the AGI held the call until HA answered ({eof:.2f}s)",
          eof >= _SCENE_SECONDS * 0.9 and fired_by_eof)


def test_the_greeting_follows_the_answer_within_a_second() -> None:
    """★ Asserted on the RENDERED dialplan, where the ordering lives.

    Between Answer() and the greeting there is exactly one AGI — the scene,
    detached — and one media settle of at most half a second, after the scene has
    been fired, so a hangup during the settle cannot skip the scene. Before
    2026-09-14 this span held a blocking scene call and a whole second's Wait.
    """
    sbc = SourceFileLoader(
        "switchboard_config_settle",
        str(_ROOT / "rootfs" / "usr" / "bin" / "switchboard-config")).load_module()
    rooms = sbc.valid_rooms([{"ext": "19", "name": "Cordless", "secret": "s1"}])
    e = sbc.render_extensions({"rooms": rooms, "wakeup": {"enabled": True}})
    start = e.index("[wakeup-deliver]")
    lines = [l.strip() for l in e[start:e.index("\n[", start + 1)].splitlines()]
    answer = lines.index("same = n,Answer()")
    greet = next(i for i, l in enumerate(lines)
                 if "Playback(switchboard/sw-wakeup-greeting)" in l)
    between = lines[answer + 1:greet]
    agis = [l for l in between if "AGI(" in l]
    waits = [(i, float(m.group(1))) for i, l in enumerate(between)
             for m in [_re.search(r"\bWait\(([0-9.]+)\)", l)] if m]
    check(f"settle: the only AGI before the greeting is the detached scene ({agis})",
          agis == ["same = n,AGI(switchboard-wakeup-deliver.agi,scene,detach)"])
    check(f"settle: one short media settle, not a pause ({waits})",
          len(waits) == 1 and 0 < waits[0][1] <= 0.5)
    check("settle: the scene is fired before the settle, not after it",
          between.index(agis[0]) < waits[0][0])
