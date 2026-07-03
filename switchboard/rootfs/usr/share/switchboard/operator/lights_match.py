"""Switchboard home-automation — transcript → area / light / intent matcher.

Dependency-free (stdlib only, musl-safe), sibling to ``match.py`` (the room
matcher) and built from the same forgiving techniques so it survives a narrowband
8 kHz telephone channel and whisper.cpp misrecognition:

  * token-overlap AND fuzzy ratio (max of the two),
  * a word-prefix bonus (the recognizer clips word tails: "Kitchen" -> "Kitch",
    "Bedroom" -> "Bed"),
  * number-word normalization (so "bedroom two" and "bedroom 2" both match),
  * filler stripping ("the", "lights", "please", ...).

Three pure entry points, each returns the best candidate or ``None`` (so the AGI
re-prompts rather than guessing — toggling the *wrong* light is worse than asking
again):

  * ``match_area(text, areas) -> area | None``         (areas: list[str])
  * ``match_light(text, lights) -> light | None``      (lights: list[{entity_id,name}])
  * ``match_intent(text) -> 'on'|'off'|'list'|'cancel'|None``

Engine- and transport-agnostic: identical whether invoked from the AGI, the TUI
or a web endpoint.
"""

from __future__ import annotations

import difflib
import re

# Words that carry no area/light routing signal. Superset of the room matcher's
# filler plus light-domain noise ("light", "lights", "lamp", "turn", ...).
FILLER = {
    "uh", "um", "er", "ah", "please", "the", "a", "an", "to", "of",
    "me", "i", "id", "would", "like", "want", "get", "put", "go",
    "room", "number", "operator", "hello", "hi", "with", "for", "my",
    "in", "on", "is", "this", "that", "and", "area", "zone",
    "light", "lights", "lamp", "lamps", "switch", "fixture",
}

# Number words -> digits, so "bedroom two" == "bedroom 2" and an area/light name
# that ends in a digit still matches a spoken word.
NUM_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12",
}

# Intent vocabularies. Each maps an intent to the phrases that signal it; longer
# phrases are checked first so "turn on" beats a stray "on".
_ON = ("turn on", "switch on", "power on", "lights on", "light on", "on")
_OFF = ("turn off", "switch off", "power off", "shut off", "lights off",
        "light off", "turn it off", "off")
_LIST = ("list", "what", "which", "options", "choices", "everything", "all",
         "tell me", "say them", "say again")
_CANCEL = ("cancel", "never mind", "nevermind", "stop", "exit", "goodbye",
           "good bye", "bye", "quit", "forget it", "forget", "done", "nothing")


def normalize(text: str) -> list[str]:
    """Lowercase, drop punctuation, map number words, split, strip filler."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    toks = []
    for t in text.split():
        if not t:
            continue
        t = NUM_WORDS.get(t, t)
        if t not in FILLER:
            toks.append(t)
    return toks


def _score(toks: list[str], spoken: str, ptoks: list[str]) -> float:
    """Score a heard token-list against one candidate's token-list. Same shape as
    match.py's scorer (overlap | fuzzy | exact-subset | word-prefix bonus)."""
    if not ptoks:
        return 0.0
    overlap = len(set(toks) & set(ptoks)) / len(set(ptoks))
    fuzzy = difflib.SequenceMatcher(None, spoken, " ".join(ptoks)).ratio()
    score = max(overlap, fuzzy)
    if set(ptoks) <= set(toks):  # every candidate word was heard
        score = max(score, 0.95)
    # Word-prefix bonus: narrowband STT clips word tails ("Kitchen" -> "Kitch",
    # "Bedroom" -> "Bed"). A clean prefix either way is a strong match.
    for st in toks:
        if len(st) >= 3:
            for pt in ptoks:
                if len(pt) >= 3 and (pt.startswith(st) or st.startswith(pt)):
                    score = max(score, 0.9)
    return score


def _best(text: str, candidates: list, key, threshold: float, margin: float):
    """Generic forgiving matcher: pick the best-scoring candidate, or None on an
    empty transcript, a below-threshold best, or a near-tie (ambiguous).

    ``key(candidate)`` -> the display string to match against. Returns the winning
    candidate object (not the string), so callers get back the dict/str they
    passed in."""
    toks = normalize(text)
    if not toks:
        return None
    spoken = " ".join(toks)
    scored = []
    for cand in candidates:
        ptoks = normalize(str(key(cand)))
        if not ptoks:
            continue
        scored.append((cand, _score(toks, spoken, ptoks)))
    if not scored:
        return None
    scored.sort(key=lambda kv: kv[1], reverse=True)
    if scored[0][1] < threshold:
        return None
    # Anti-misroute: reject a near-tie between two plausible candidates.
    if len(scored) > 1 and scored[1][1] >= threshold and (scored[0][1] - scored[1][1]) < margin:
        return None
    return scored[0][0]


def match_area(text: str, areas: list[str], threshold: float = 0.6,
               margin: float = 0.08) -> str | None:
    """Resolve a transcript to one of ``areas`` (a list of area-name strings).
    Returns the matched area string, or None (no/ambiguous/empty match).

    The empty-string area key ('') is HA's bucket for unassigned lights, which
    the flow announces as "Unassigned" — so it must be scoreable (caller can say
    "Unassigned"). We score against the display name but return the ORIGINAL key
    ('' for the unassigned bucket) so it indexes ``lights_by_area()`` correctly."""
    if match_intent(text) == "list":
        return None
    # Dedup on the display name ('' -> "Unassigned"); keep the raw key to return.
    seen, pairs = set(), []  # pairs: (display, raw)
    for a in areas:
        raw = (a or "").strip()
        display = raw or "Unassigned"
        if display not in seen:
            seen.add(display)
            pairs.append((display, raw))
    match = _best(text, pairs, key=lambda p: p[0], threshold=threshold, margin=margin)
    return match[1] if match is not None else None


def match_light(text: str, lights: list[dict], threshold: float = 0.6,
                margin: float = 0.08) -> dict | None:
    """Resolve a transcript to one light dict from ``lights`` (each {entity_id,
    name}). Returns the matched dict, or None."""
    if match_intent(text) == "list":
        return None
    cands = [lt for lt in lights if isinstance(lt, dict) and (lt.get("name") or lt.get("entity_id"))]
    return _best(text, cands, key=lambda lt: lt.get("name") or lt.get("entity_id", ""),
                 threshold=threshold, margin=margin)


def match_intent(text: str) -> str | None:
    """Classify a transcript as 'on' | 'off' | 'list' | 'cancel' | None.

    Cancel is checked first (a caller bailing out trumps everything), then off
    before on (so "turn off" / "lights off" aren't shadowed by a substring "on"
    inside "off"... they can't be, but ordering keeps intent precedence explicit),
    then list. Matching is on the normalized-ish lowered text via word-boundary
    phrase search, with a fuzzy fallback for single misheard intent words."""
    low = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()) + " "
    low = re.sub(r"\s+", " ", low)
    if low.strip() == "":
        return None

    def has(phrases):
        for p in phrases:
            if f" {p} " in low:
                return True
        return False

    if has(_CANCEL):
        return "cancel"
    if has(_OFF):
        return "off"
    if has(_ON):
        return "on"
    if has(_LIST):
        return "list"

    # Fuzzy fallback: a single short utterance that's a near-miss of an intent
    # word ("of" -> "off", "lest" -> "list", "cancl" -> "cancel").
    words = low.split()
    if len(words) <= 2:
        for w in words:
            if len(w) < 2:
                continue
            # Per-WORD ratio: everything keeps the strict 0.8 except the literal
            # word "list", which accepts 0.75 — that's what catches the example
            # above ("lest" scores exactly 0.75) and the live whisper mishears
            # of a spoken "list" ("lift"/"lisp", also 0.75). The looser ratio
            # must NOT extend to the rest of the list vocab: at 0.75 vs "what",
            # everyday words land exactly on the line ("heat"/"that"/"chat"/
            # "watt" are all 0.75) and a light named "Heat Lamp" would become
            # unselectable — misrouted to 'list' before match_light ever runs.
            # on/off/cancel stay strict regardless: they ACT on the house.
            for key, vocab in (("cancel", ("cancel", "stop", "exit", "bye")),
                               ("off", ("off",)),
                               ("on", ("on",)),
                               ("list", ("list", "what", "which"))):
                for v in vocab:
                    thr = 0.75 if v == "list" else 0.8
                    if difflib.SequenceMatcher(None, w, v).ratio() >= thr:
                        return key
        # Known whisper near-miss too far for any sane ratio ('left' vs 'list'
        # is only 0.5): a LONE such word is treated as the list request. Single
        # word only — "left hallway" must stay matchable as an area/light name.
        if len(words) == 1 and words[0] in ("left",):
            return "list"
    return None


# --------------------------------------------------------------------------- #
# Convenience: the automation-intent gate, also used by the operator AGI to
# decide whether a spoken "lights"/"automation" should jump into this flow.
# Kept here (data-adjacent) but match.py re-exports a thin wrapper.
# --------------------------------------------------------------------------- #
_AUTOMATION_WORDS = (
    "automation", "automations", "home automation", "home control",
    "control", "lights", "light", "lighting", "the lights",
)


def is_automation_request(text: str) -> bool:
    """True only when the transcript is *clearly* an automation/lights request
    (so the operator routes to the lights flow); otherwise False and the caller
    falls through to room matching unchanged."""
    low = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()) + " "
    low = re.sub(r"\s+", " ", low)
    for w in _AUTOMATION_WORDS:
        if f" {w} " in low:
            return True
    # Fuzzy single-word: "automaton"/"lite" -> automation/lights, but require a
    # high ratio so a real room name never trips it.
    words = low.split()
    if 1 <= len(words) <= 2:
        for w in words:
            for target in ("automation", "lights", "lighting"):
                if len(w) >= 4 and difflib.SequenceMatcher(None, w, target).ratio() >= 0.82:
                    return True
    return False


# --------------------------------------------------------------------------- #
# Self-test:  python3 lights_match.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    areas = ["Kitchen", "Living Room", "Master Bedroom", "Office", "Garage"]
    lights = [
        {"entity_id": "light.kitchen_main", "name": "Kitchen Main"},
        {"entity_id": "light.kitchen_under_cabinet", "name": "Under Cabinet"},
        {"entity_id": "light.office_lamp", "name": "Office Lamp"},
    ]
    passed = total = 0

    def chk(name, cond):
        global passed, total
        total += 1
        passed += bool(cond)
        print(("  ok   " if cond else "  FAIL ") + name)

    chk("area kitchen", match_area("kitchen", areas) == "Kitchen")
    chk("area the kitchen lights", match_area("the kitchen lights", areas) == "Kitchen")
    chk("area clipped", match_area("master bed", areas) == "Master Bedroom")
    chk("area list -> None", match_area("list", areas) is None)
    chk("area nomatch", match_area("nonsense", areas) is None)
    chk("light by name", (match_light("office lamp", lights) or {}).get("entity_id") == "light.office_lamp")
    chk("light fuzzy", (match_light("under cabinet", lights) or {}).get("entity_id") == "light.kitchen_under_cabinet")
    chk("intent on", match_intent("turn on") == "on")
    chk("intent off", match_intent("turn off the lights") == "off")
    chk("intent list", match_intent("list") == "list")
    chk("intent cancel", match_intent("never mind") == "cancel")
    chk("intent none", match_intent("kitchen") is None)
    chk("auto lights", is_automation_request("lights") is True)
    chk("auto phrase", is_automation_request("home automation") is True)
    chk("auto not-a-room", is_automation_request("kitchen") is False)
    print(f"\n{passed}/{total} passed")
    raise SystemExit(0 if passed == total else 1)
