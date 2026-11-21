"""The lights menu (dial 43): its whole voice flow, with Home Assistant stubbed.

    python3 -m pytest switchboard/tests/test_automation_agi.py

Lights with no area sit under the area key '' and are read out as
"Unassigned". Before v0.125.0 the same '' was pick_area's "the caller said
cancel" answer, so choosing "Unassigned" ended the call and those lights could
never be reached from the phone. These tests drive main() end to end: the real
lights_match matcher, with speech, recognition and Home Assistant stubbed.
"""
import sys
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGI = ROOT / "rootfs" / "var" / "lib" / "asterisk" / "agi-bin" / "switchboard-automation.agi"
OPERATOR = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "operator"
if str(OPERATOR) not in sys.path:
    sys.path.insert(0, str(OPERATOR))

BY_AREA = {
    "Kitchen": [{"entity_id": "light.kitchen_ceiling", "name": "Kitchen Ceiling"}],
    "": [{"entity_id": "light.porch_lamp", "name": "Porch Lamp"}],
}


def _run(heard: list[str], by_area=BY_AREA):
    """main() with the caller saying `heard`, in order. Returns (spoken, sets)."""
    hc = types.ModuleType("ha_client")
    sets: list[tuple[str, bool]] = []
    hc.lights_by_area = lambda: by_area
    hc.get_light_state = lambda entity_id: "off"
    hc.set_light = lambda entity_id, on: (sets.append((entity_id, on)), True)[1]
    saved = sys.modules.get("ha_client")
    sys.modules["ha_client"] = hc
    try:
        mod = SourceFileLoader("sb_automation_flow", str(AGI)).load_module()
    finally:
        if saved is None:
            sys.modules.pop("ha_client", None)
        else:
            sys.modules["ha_client"] = saved
    mod.ha_client = hc
    spoken: list[str] = []
    queue = list(heard)
    mod.read_env = lambda: {}
    mod.say = lambda text: (spoken.append(text), True)[1]
    mod.say_or = lambda text, fallback: spoken.append(text)
    mod.stream = lambda f: spoken.append(f"<{f}>")
    mod.listen = lambda tag, attempt, bias="": queue.pop(0) if queue else ""
    mod.main()
    return spoken, sets


def test_the_unassigned_area_can_be_chosen():
    spoken, sets = _run(["Unassigned", "Porch Lamp", "turn on"])
    assert sets == [("light.porch_lamp", True)], (spoken, sets)
    assert spoken[-1] == "Turned Porch Lamp on. Goodbye.", spoken
    assert "Okay, goodbye." not in spoken, spoken


def test_list_reads_the_unassigned_area_out():
    spoken, _ = _run(["list", "cancel"])
    assert any(s.startswith("The rooms are:") and "Unassigned" in s for s in spoken), spoken


def test_cancel_still_ends_the_call_politely():
    spoken, sets = _run(["cancel"])
    assert sets == [] and spoken[-1] == "Okay, goodbye.", spoken


def test_two_misses_still_end_with_the_retry_goodbye():
    spoken, sets = _run(["zzzz", "qqqq"])
    assert sets == [] and spoken[-1] == "Sorry, I didn't get that. Goodbye.", spoken


def test_a_named_area_still_works():
    spoken, sets = _run(["kitchen", "kitchen ceiling", "turn off"])
    assert sets == [("light.kitchen_ceiling", False)], (spoken, sets)


# Names that hold, or sound like, the cancel and list words: before v0.125.0
# the command words were checked first, so these could never be chosen.
WORDY = {
    "Top Floor": [{"entity_id": "light.all_lights", "name": "All Lights"},
                  {"entity_id": "light.stoop", "name": "Stoop Light"}],
    "Left Hallway": [{"entity_id": "light.exit_sign", "name": "Exit Sign"}],
}


def test_a_whole_name_beats_a_command_word_inside_it():
    spoken, sets = _run(["Top Floor", "All Lights", "turn on"], WORDY)
    assert sets == [("light.all_lights", True)], (spoken, sets)
    spoken, sets = _run(["top floor.", "Stoop Light", "turn off"], WORDY)
    assert sets == [("light.stoop", False)], (spoken, sets)
    spoken, sets = _run(["Left Hallway", "Exit Sign", "turn on"], WORDY)
    assert sets == [("light.exit_sign", True)], (spoken, sets)


def test_the_command_words_alone_still_win():
    # "stop" is not the whole name "Top Floor": it cancels.
    spoken, sets = _run(["stop"], WORDY)
    assert sets == [] and spoken[-1] == "Okay, goodbye.", spoken
    # A lone "left" (a mishearing of "list") lists, beside "Left Hallway".
    spoken, _ = _run(["left", "cancel"], WORDY)
    assert any(s.startswith("The rooms are:") for s in spoken), spoken
    # "all" is not the whole name "All Lights": it lists the lights.
    spoken, _ = _run(["Top Floor", "all", "cancel"], WORDY)
    assert any(s.startswith("The lights are:") for s in spoken), spoken


def test_a_name_that_is_exactly_a_command_word_is_chosen():
    # Documented: the whole name wins, so a light named "Exit" is reachable;
    # cancel and the other command words still cancel beside it.
    by_area = {"Hall": [{"entity_id": "light.exit", "name": "Exit"},
                        {"entity_id": "light.hall", "name": "Ceiling"}]}
    spoken, sets = _run(["Hall", "exit", "turn on"], by_area)
    assert sets == [("light.exit", True)], (spoken, sets)
    spoken, sets = _run(["Hall", "cancel"], by_area)
    assert sets == [] and spoken[-1] == "Okay, goodbye.", spoken


def test_cancel_at_the_light_step_says_okay():
    spoken, sets = _run(["Kitchen", "cancel"])
    assert sets == [] and spoken[-1] == "Okay, goodbye.", spoken


def test_two_misses_at_the_action_step_say_not_understood():
    spoken, sets = _run(["Kitchen", "Kitchen Ceiling", "zzzz", "qqqq"])
    assert sets == [] and spoken[-1] == "Sorry, I didn't get that. Goodbye.", spoken


def test_cancel_at_the_action_step_says_okay():
    spoken, sets = _run(["Kitchen", "Kitchen Ceiling", "cancel"])
    assert sets == [] and spoken[-1] == "Okay, goodbye.", spoken


def test_a_real_area_named_unassigned_takes_the_lights_with_no_area():
    for real in ("Unassigned", "unassigned"):
        by_area = {real: [{"entity_id": "light.desk", "name": "Desk Lamp"}],
                   "": [{"entity_id": "light.porch_lamp", "name": "Porch Lamp"}]}
        spoken, sets = _run(["list", "Unassigned", "Porch Lamp", "turn on"], by_area)
        assert f"The rooms are: {real}." in spoken, spoken
        assert sets == [("light.porch_lamp", True)], (real, spoken, sets)


def test_a_no_area_light_named_like_one_in_the_real_unassigned_area_stays_out():
    # Joining would tie the two "Desk Lamp"s, and neither could be chosen; the
    # area's own one could before v0.125.0, and still can.
    by_area = {"Unassigned": [{"entity_id": "light.desk", "name": "Desk Lamp"}],
               "": [{"entity_id": "light.desk_2", "name": "desk lamp"},
                    {"entity_id": "light.porch_lamp", "name": "Porch Lamp"}]}
    spoken, sets = _run(["Unassigned", "Desk Lamp", "turn on"], by_area)
    assert sets == [("light.desk", True)], (spoken, sets)
    spoken, sets = _run(["Unassigned", "Porch Lamp", "turn on"], by_area)
    assert sets == [("light.porch_lamp", True)], (spoken, sets)


def test_a_near_twin_with_no_area_stays_out_too():
    # Same words once "light"/"lamp" are set aside: joining would tie them for
    # every reply but the exact names, and "porch" reached the area's own before.
    for own, twin, said in (("Porch Light", "Porch Lights", "porch"),
                            ("Porch Light", "Porch Lights", "the porch light"),
                            ("Desk Lamp", "Desk Light", "desk"),
                            ("Ceiling Light", "The Ceiling", "ceiling")):
        by_area = {"Unassigned": [{"entity_id": "light.own", "name": own}],
                   "": [{"entity_id": "light.twin", "name": twin}]}
        spoken, sets = _run(["Unassigned", said, "turn on"], by_area)
        assert sets == [("light.own", True)], (own, twin, said, spoken, sets)
