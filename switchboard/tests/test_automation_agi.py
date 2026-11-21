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
