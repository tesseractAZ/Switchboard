"""Tests for the home-automation matcher + the operator automation-intent gate.

Plain python3, no deps:

    python3 switchboard/tests/test_lights_match.py

Covers:
  * lights_match.match_area  (exact / fuzzy / clipped / "the kitchen lights" /
    list / cancel / nomatch / ambiguous)
  * lights_match.match_light (by name, fuzzy, list, nomatch)
  * lights_match.match_intent (on / off / list / cancel / none + fuzzy fallback)
  * lights_match.is_automation_request and match.is_automation (the operator gate)
  * a guard that a real room name does NOT trip the automation gate
"""

from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OP = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "operator"
STT = ROOT / "rootfs" / "usr" / "bin" / "switchboard-stt"
# Load lights_match first so match.is_automation can import it off the same dir.
import sys
sys.path.insert(0, str(OP))
lm = SourceFileLoader("sw_lights_match", str(OP / "lights_match.py")).load_module()
matcher = SourceFileLoader("sw_match", str(OP / "match.py")).load_module()
# The STT wrapper's pure rooms-mode decision (automation token vs room ext). It
# does its own `import match`/`import lights_match` off the same operator dir,
# which resolves against OP (already on sys.path above).
stt = SourceFileLoader("sw_stt", str(STT)).load_module()

_failures = 0
_count = 0


def check(name, cond):
    global _failures, _count
    _count += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


# --------------------------------------------------------------------------- #
# Fixtures (shaped like ha_client.lights_by_area() output)
# --------------------------------------------------------------------------- #
AREAS = ["Kitchen", "Living Room", "Master Bedroom", "Office", "Garage", "Basement"]
KITCHEN_LIGHTS = [
    {"entity_id": "light.kitchen_main", "name": "Kitchen Main"},
    {"entity_id": "light.kitchen_under_cabinet", "name": "Under Cabinet"},
    {"entity_id": "light.kitchen_sink", "name": "Sink"},
]
OFFICE_LIGHTS = [
    {"entity_id": "light.office_lamp", "name": "Desk Lamp"},
    {"entity_id": "light.office_ceiling", "name": "Ceiling"},
]


def area(text):
    return lm.match_area(text, AREAS)


def light_id(text, lights):
    got = lm.match_light(text, lights)
    return got.get("entity_id") if got else None


# --------------------------------------------------------------------------- #
def test_area():
    check("area 'kitchen' -> Kitchen", area("kitchen") == "Kitchen")
    check("area 'the kitchen' -> Kitchen", area("the kitchen") == "Kitchen")
    check("area 'the kitchen lights' -> Kitchen", area("the kitchen lights") == "Kitchen")
    check("area 'living room' -> Living Room", area("living room") == "Living Room")
    check("area 'the living room please' -> Living Room", area("the living room please") == "Living Room")
    check("area 'master bedroom' -> Master Bedroom", area("master bedroom") == "Master Bedroom")
    check("area 'office' -> Office", area("office") == "Office")
    check("area 'garage' -> Garage", area("garage") == "Garage")
    # Narrowband clipping (whisper drops word tails).
    check("area clipped 'master bed' -> Master Bedroom", area("master bed") == "Master Bedroom")
    check("area clipped 'base' -> Basement", area("base") == "Basement")
    check("area fuzzy 'kitchin' -> Kitchen", area("kitchin") == "Kitchen")
    check("area fuzzy 'offise' -> Office", area("offise") == "Office")
    # Misc.
    check("area 'list' -> None (it's an intent)", area("list") is None)
    check("area 'nonsense' -> None", area("nonsense") is None)
    check("area '' -> None", area("") is None)


def test_area_ambiguous():
    amb = ["Bedroom One", "Bedroom Two"]
    check("area ambiguous 'bedroom' -> None", lm.match_area("bedroom", amb) is None)


def test_area_unassigned():
    # HA buckets unassigned lights under the '' area, announced as "Unassigned".
    # Saying "Unassigned" must match and return the ORIGINAL '' key so it indexes
    # lights_by_area() correctly.
    check("area 'unassigned' -> '' (only unassigned)", lm.match_area("unassigned", [""]) == "")
    check("area 'unassigned' -> '' (mixed list)",
          lm.match_area("unassigned", ["Kitchen", "", "Office"]) == "")
    # A real area in a list that ALSO contains the unassigned bucket still matches.
    check("area 'kitchen' -> Kitchen (mixed w/ unassigned)",
          lm.match_area("kitchen", ["Kitchen", "", "Office"]) == "Kitchen")
    # The unassigned bucket must not steal a real-area utterance.
    check("area 'office' -> Office (mixed w/ unassigned)",
          lm.match_area("office", ["Kitchen", "", "Office"]) == "Office")


def test_light():
    check("light 'kitchen main'", light_id("kitchen main", KITCHEN_LIGHTS) == "light.kitchen_main")
    check("light 'under cabinet'", light_id("under cabinet", KITCHEN_LIGHTS) == "light.kitchen_under_cabinet")
    check("light 'the sink light'", light_id("the sink light", KITCHEN_LIGHTS) == "light.kitchen_sink")
    check("light 'desk lamp'", light_id("desk lamp", OFFICE_LIGHTS) == "light.office_lamp")
    check("light 'ceiling'", light_id("ceiling", OFFICE_LIGHTS) == "light.office_ceiling")
    # Clipped / fuzzy.
    check("light clipped 'cabinet'", light_id("cabinet", KITCHEN_LIGHTS) == "light.kitchen_under_cabinet")
    check("light 'list' -> None", lm.match_light("list", KITCHEN_LIGHTS) is None)
    check("light 'nonsense' -> None", lm.match_light("nonsense", KITCHEN_LIGHTS) is None)
    check("light '' -> None", lm.match_light("", KITCHEN_LIGHTS) is None)


def test_intent():
    # on
    check("intent 'on'", lm.match_intent("on") == "on")
    check("intent 'turn on'", lm.match_intent("turn on") == "on")
    check("intent 'switch on'", lm.match_intent("switch on") == "on")
    check("intent 'turn the lights on'", lm.match_intent("turn the lights on") == "on")
    # off
    check("intent 'off'", lm.match_intent("off") == "off")
    check("intent 'turn off'", lm.match_intent("turn off") == "off")
    check("intent 'switch off'", lm.match_intent("switch off") == "off")
    check("intent 'turn off the lights'", lm.match_intent("turn off the lights") == "off")
    check("intent 'shut off'", lm.match_intent("shut off") == "off")
    # list
    check("intent 'list'", lm.match_intent("list") == "list")
    check("intent 'what are my options'", lm.match_intent("what are my options") == "list")
    check("intent 'which ones'", lm.match_intent("which ones") == "list")
    # cancel
    check("intent 'cancel'", lm.match_intent("cancel") == "cancel")
    check("intent 'never mind'", lm.match_intent("never mind") == "cancel")
    check("intent 'stop'", lm.match_intent("stop") == "cancel")
    check("intent 'goodbye'", lm.match_intent("goodbye") == "cancel")
    check("intent 'exit'", lm.match_intent("exit") == "cancel")
    # none / precedence
    check("intent 'kitchen' -> None", lm.match_intent("kitchen") is None)
    check("intent '' -> None", lm.match_intent("") is None)
    check("intent cancel beats off ('stop, never mind')",
          lm.match_intent("stop never mind") == "cancel")
    # fuzzy single-word fallback
    check("intent fuzzy 'cancl' -> cancel", lm.match_intent("cancl") == "cancel")
    check("intent fuzzy 'lst' -> list (dropped vowel)", lm.match_intent("lst") == "list")
    # Live whisper mishears of a spoken "list" (observed on real calls: the
    # recognizer, biased toward room/light names, returned these sound-alikes).
    check("intent fuzzy 'Lift' -> list (live mishear)", lm.match_intent("Lift") == "list")
    check("intent fuzzy 'lisp' -> list", lm.match_intent("lisp") == "list")
    check("intent fuzzy 'lest' -> list (the docstring's own example)",
          lm.match_intent("lest") == "list")
    check("intent lone 'Left' -> list (known far mishear, single word only)",
          lm.match_intent("Left") == "list")
    check("'left hallway' is NOT hijacked to list (stays area/light-matchable)",
          lm.match_intent("left hallway") is None)
    check("'lamp' is NOT list (real light name must stay selectable)",
          lm.match_intent("lamp") is None)
    # The looser 0.75 ratio applies ONLY to the literal word 'list' — near
    # misses of the ACTING intents must not fire ('in' is 0.5 to 'on'; 'of' at
    # 0.8 was already accepted; something like 'awf' must not become off).
    check("intent 'awf' does not act as off", lm.match_intent("awf") is None)
    # Regression (review-caught): everyday words score exactly 0.75 vs "what"
    # ('heat'/'that'/'chat'/'watt') — they must stay None or a light named
    # "Heat Lamp" is misrouted to 'list' before match_light runs and becomes
    # unselectable by voice, looping the options list forever.
    check("intent 'heat' -> None (0.75 vs 'what' must not fire)",
          lm.match_intent("heat") is None)
    check("intent 'heat lamp' -> None", lm.match_intent("heat lamp") is None)
    check("intent 'that' -> None", lm.match_intent("that") is None)
    check("light 'Heat Lamp' remains selectable by voice",
          lm.match_light("heat lamp",
                         [{"entity_id": "light.bathroom_heat_lamp", "name": "Heat Lamp"},
                          {"entity_id": "light.vanity", "name": "Vanity"}])
          is not None)


def test_automation_gate():
    # lights_match.is_automation_request
    check("auto 'lights'", lm.is_automation_request("lights") is True)
    check("auto 'light'", lm.is_automation_request("light") is True)
    check("auto 'automation'", lm.is_automation_request("automation") is True)
    check("auto 'home automation'", lm.is_automation_request("home automation") is True)
    check("auto 'home control'", lm.is_automation_request("home control") is True)
    check("auto 'the lights please'", lm.is_automation_request("the lights please") is True)
    # Must NOT trip on a real room / unrelated speech.
    check("auto 'kitchen' -> False", lm.is_automation_request("kitchen") is False)
    check("auto 'master bedroom' -> False", lm.is_automation_request("master bedroom") is False)
    check("auto 'connect me to the office' -> False",
          lm.is_automation_request("connect me to the office") is False)
    check("auto '' -> False", lm.is_automation_request("") is False)
    # Ordinary words within fuzzy range of the short 'lights'/'lighting' targets
    # must NOT trip the gate (audit: 'flights'->'lights' was 0.92 and mis-routed).
    for w in ("flights", "nights", "rights", "sights", "the flights"):
        check(f"auto {w!r} -> False", lm.is_automation_request(w) is False)
    # ... but a genuine automation-family typo still recovers.
    check("auto 'automaton' -> True", lm.is_automation_request("automaton") is True)
    # bare 'control' no longer diverts a room name.
    check("auto 'control room' -> False", lm.is_automation_request("control room") is False)
    check("auto 'control the lights' -> True", lm.is_automation_request("control the lights") is True)

    # match.is_automation (operator gate) delegates to the same logic.
    check("match.is_automation 'lights'", matcher.is_automation("lights") is True)
    check("match.is_automation 'home automation'", matcher.is_automation("home automation") is True)
    check("match.is_automation 'kitchen' -> False", matcher.is_automation("kitchen") is False)
    check("match.is_automation '' -> False", matcher.is_automation("") is False)

    # Regression: the automation gate must not break ordinary room matching.
    rooms = [{"ext": "11", "name": "Kitchen"}, {"ext": "16", "name": "Office"}]
    ext, _score, _reason = matcher.match("the office please", rooms)
    check("room match still works ('office' -> 16)", ext == "16")


def test_feature_intent():
    # The operator hands a caller off to any other feature. feature_intent maps a
    # spoken request to a feature token (clock/status/directory/announce/page);
    # wake-up + lights are handled separately (is_wakeup_request/is_automation).
    fi = matcher.feature_intent
    check("feature: 'what time is it' -> clock", fi("what time is it") == "clock")
    check("feature: 'the time please' -> clock", fi("the time please") == "clock")
    check("feature: 'clock' -> clock", fi("clock") == "clock")
    # REGRESSION: a caller who just says "time" (what whisper transcribed as
    # 'Time.' on a real call) must reach the clock — bare "time" was missing.
    check("feature: bare 'time' -> clock", fi("time") == "clock")
    check("feature: 'Time.' (whisper punctuation) -> clock", fi("Time.") == "clock")
    # ...but "time" as a word-fragment inside another word must NOT trip it.
    for w in ("anytime", "overtime", "sometime", "bedtime"):
        check(f"feature: {w!r} -> None (no substring hijack)", fi(w) is None)
    check("feature: 'the weather' -> status", fi("what's the weather") == "status")
    check("feature: 'power' -> status", fi("power") == "status")
    check("feature: 'house status' -> status", fi("house status") == "status")
    check("feature: 'directory assistance' -> directory", fi("directory assistance") == "directory")
    check("feature: 'phone book' -> directory", fi("phone book") == "directory")
    check("feature: 'make an announcement' -> announce", fi("make an announcement") == "announce")
    check("feature: 'page everyone' -> page", fi("page everyone") == "page")
    check("feature: 'intercom' -> page", fi("intercom") == "page")
    # Apostrophes/contractions must normalize (the transcript drops apostrophes so
    # "who's" -> "whos"): a phrase written with an apostrophe would otherwise be
    # dead. Both the straight and curly apostrophe, and the spelled-out form.
    check("feature: \"who's here\" -> directory", fi("who's here") == "directory")
    check("feature: 'who is here' -> directory", fi("who is here") == "directory")
    check("feature: \"what's the time\" -> clock", fi("what's the time") == "clock")
    check("feature: \"how's the house\" -> status", fi("how's the house") == "status")
    # Real room names / a plain connect request must NOT trip a feature — they
    # have to keep resolving to a room, exactly like the automation gate above.
    for room in ("kitchen", "living room", "master bedroom", "office", "garage",
                 "basement", "study", "the office please", "connect me to the kitchen"):
        check(f"feature: {room!r} -> None (stays a room)", fi(room) is None)
    check("feature: '' -> None", fi("") is None)


def test_stt_rooms_decision():
    """switchboard-stt --mode rooms now does ONE whisper pass: it prints the
    literal token 'automation' for a lights request, otherwise a room extension.
    resolve_rooms_text is that pure decision (no I/O), unit-tested here."""
    rooms = [{"ext": "11", "name": "Kitchen"},
             {"ext": "13", "name": "Garage"},
             {"ext": "16", "name": "Office"}]
    # Automation phrases short-circuit to the 'automation' token.
    check("stt 'turn on the lights' -> automation",
          stt.resolve_rooms_text("turn on the lights", rooms) == "automation")
    check("stt 'home automation' -> automation",
          stt.resolve_rooms_text("home automation", rooms) == "automation")
    check("stt 'lights' -> automation",
          stt.resolve_rooms_text("lights", rooms) == "automation")
    # Room names resolve to their extension (gate stays conservative).
    check("stt 'garage' -> 13", stt.resolve_rooms_text("garage", rooms) == "13")
    check("stt 'office' -> 16", stt.resolve_rooms_text("the office please", rooms) == "16")
    check("stt 'kitchen' -> 11", stt.resolve_rooms_text("kitchen", rooms) == "11")
    # No match -> '' (empty, so the AGI re-prompts).
    check("stt nonsense -> ''", stt.resolve_rooms_text("zzzzz qqqq", rooms) == "")
    check("stt '' -> ''", stt.resolve_rooms_text("", rooms) == "")
    # A wake-up request short-circuits to the 'wakeup' token (operator hand-off).
    check("stt 'wake up call' -> wakeup",
          stt.resolve_rooms_text("wake up call", rooms) == "wakeup")
    check("stt 'wake me up' -> wakeup",
          stt.resolve_rooms_text("wake me up at seven", rooms) == "wakeup")
    check("stt 'set an alarm' -> wakeup",
          stt.resolve_rooms_text("set an alarm", rooms) == "wakeup")
    # ... and a plain room name is NOT mistaken for a wake-up.
    check("stt 'kitchen' is not a wake-up", stt.resolve_rooms_text("kitchen", rooms) == "11")
    # Every other feature request short-circuits to its token (operator hand-off);
    # checked AFTER wakeup/automation AND after a confident room match.
    check("stt 'what time is it' -> clock",
          stt.resolve_rooms_text("what time is it", rooms) == "clock")
    check("stt bare 'time' -> clock (real-call regression)",
          stt.resolve_rooms_text("time", rooms) == "clock")
    check("stt weather -> status",
          stt.resolve_rooms_text("what's the weather", rooms) == "status")
    check("stt 'directory assistance' -> directory",
          stt.resolve_rooms_text("directory assistance", rooms) == "directory")
    check("stt 'make an announcement' -> announce",
          stt.resolve_rooms_text("make an announcement", rooms) == "announce")
    check("stt 'page everyone' -> page",
          stt.resolve_rooms_text("page everyone", rooms) == "page")
    # A room name still wins over a feature (an exact room name scores ~1.0).
    check("stt 'garage' still -> 13 (not a feature)",
          stt.resolve_rooms_text("garage", rooms) == "13")

    # --- intent-vs-entity collision (the reason resolution is score-based) ------
    # REGRESSION: a bare feature word must NOT be swallowed by an unrelated room
    # it only fuzzy-rhymes with. "page" scores ~0.67 against a room named "Garage"
    # (both clear the 0.6 room threshold) — but 0.67 < ROOM_CONFIDENT, so the
    # feature word wins and the caller gets the intercom, not the Garage phone.
    # (This fixture HAS a Garage at ext 13, exactly the live-config collision.)
    check("stt 'page' -> page (NOT Garage-by-fuzz)",
          stt.resolve_rooms_text("page", rooms) == "page")
    check("stt 'solar' -> status (NOT Garage-by-fuzz)",
          stt.resolve_rooms_text("solar", rooms) == "status")
    check("stt 'charge' -> status (NOT Garage-by-fuzz)",
          stt.resolve_rooms_text("charge", rooms) == "status")
    check("stt 'weather' -> status (no confident room)",
          stt.resolve_rooms_text("weather", rooms) == "status")
    # ...yet a handset genuinely NAMED after a feature keyword still connects by
    # name: an exact room match scores ~1.0 >= ROOM_CONFIDENT and wins outright.
    kw_rooms = rooms + [{"ext": "22", "name": "Weather"}]
    check("stt 'weather' room -> 22 (exact room wins over status)",
          stt.resolve_rooms_text("weather", kw_rooms) == "22")
    # ...and a LOW-confidence but real room match (misheard/clipped, not a feature
    # word) still resolves via the third tier — "kitchin" -> Kitchen.
    check("stt 'kitchin' -> 11 (fuzzy room, tier 3)",
          stt.resolve_rooms_text("kitchin", rooms) == "11")


def test_wakeup_gate():
    check("wakeup 'wake up call'", matcher.is_wakeup_request("wake up call") is True)
    check("wakeup 'wakeup'", matcher.is_wakeup_request("wakeup") is True)
    check("wakeup 'wake me up'", matcher.is_wakeup_request("please wake me up") is True)
    check("wakeup 'morning call'", matcher.is_wakeup_request("morning call") is True)
    check("wakeup not a room", matcher.is_wakeup_request("kitchen") is False)
    check("wakeup not lights", matcher.is_wakeup_request("turn on the lights") is False)
    check("wakeup empty", matcher.is_wakeup_request("") is False)


if __name__ == "__main__":
    test_area()
    test_area_ambiguous()
    test_area_unassigned()
    test_light()
    test_intent()
    test_automation_gate()
    test_feature_intent()
    test_stt_rooms_decision()
    test_wakeup_gate()
    print(f"\n{_count - _failures}/{_count} passed")
    raise SystemExit(1 if _failures else 0)
