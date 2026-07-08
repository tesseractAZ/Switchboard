"""Switchboard operator — transcript → extension matcher.

Dependency-free (stdlib only, musl-safe). Takes whatever the speech recognizer
produced (whisper.cpp free text, biased by a room-name prompt/grammar) and the
configured rooms, and decides which extension the caller asked for. Engine- and
transport-agnostic: identical for any recognizer and whether invoked from AGI,
ARI, or a web endpoint.

Resolution order:
  1. An explicit extension number spoken ("extension eleven", "one one", "14").
  2. A room name / synonym, scored by token-overlap AND fuzzy ratio (max of the
     two), so filler ("uh, the living room please") and light misrecognition
     ("kitchin" -> Kitchen) still resolve.

Anti-misroute: if the top two candidates are within `margin` of each other and
both clear `threshold`, the result is AMBIGUOUS and we return None so the
operator re-prompts — connecting the *wrong* room is worse than asking again,
because the caller only finds out when a person answers.

Returns (ext|None, score, reason) where reason ∈
{number, name, nomatch, ambiguous, empty}.
"""

from __future__ import annotations

import difflib
import re

# Words that carry no routing signal.
FILLER = {
    "uh", "um", "er", "ah", "please", "the", "a", "an", "to", "connect",
    "me", "i", "id", "would", "like", "want", "reach", "call", "get", "put",
    "through", "extension", "ext", "room", "number", "operator", "hello",
    "hi", "with", "speak", "talk", "for", "my", "in", "on", "is", "this",
}

NUM_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20",
}


def normalize(text: str) -> list[str]:
    """Lowercase, drop punctuation, split, and remove filler words."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return [t for t in text.split() if t and t not in FILLER]


def _spoken_ext(toks: list[str], valid: set[str]) -> str | None:
    """Pull an extension number out of the tokens, if one was clearly spoken."""
    cands: set[str] = set()
    singles: list[str] = []
    for t in toks:
        if t.isdigit():
            cands.add(t)
            if len(t) == 1:
                singles.append(t)
        elif t in NUM_WORDS:
            d = NUM_WORDS[t]
            cands.add(d)
            if len(d) == 1:
                singles.append(d)
    if len(singles) >= 2:  # "one one" / "1 4" -> "11" / "14"
        cands.add("".join(singles[:2]))
    for c in cands:
        if c in valid:
            return c
    return None


def _candidate_phrases(rooms: list[dict], synonyms: dict | None) -> dict[str, list[list[str]]]:
    """ext -> list of normalized token-lists (the room name + any synonyms)."""
    out: dict[str, list[list[str]]] = {}
    for r in rooms:
        ext = str(r.get("ext", "")).strip()
        if not ext:
            continue
        phrases = [str(r.get("name", ext))]
        if synonyms and ext in synonyms:
            phrases += list(synonyms[ext])
        out[ext] = [p for p in (normalize(x) for x in phrases) if p]
    return out


def _score(toks: list[str], spoken: str, ptoks: list[str]) -> float:
    if not ptoks:
        return 0.0
    overlap = len(set(toks) & set(ptoks)) / len(set(ptoks))
    fuzzy = difflib.SequenceMatcher(None, spoken, " ".join(ptoks)).ratio()
    score = max(overlap, fuzzy)
    if set(ptoks) <= set(toks):  # every candidate word was heard
        score = max(score, 0.95)
    # Word-prefix bonus: the recognizer clips word tails on a narrowband line
    # ("Basement" -> "Base", "Dining" -> "Din"). If a heard word is a clean
    # prefix of a candidate word (or vice versa), treat it as a strong match so
    # the right room wins decisively over incidental fuzzy overlap.
    for st in toks:
        if len(st) >= 3:
            for pt in ptoks:
                if len(pt) >= 3 and (pt.startswith(st) or st.startswith(pt)):
                    score = max(score, 0.9)
    return score


# Automation-intent phrases. A spoken "lights"/"automation"/"home control" at
# the operator jumps into the voice home-automation flow instead of room
# matching. Kept conservative: only a clear automation phrase wins, so a real
# room name ("Office", "Garage") always falls through to match() unchanged.
_AUTOMATION_WORDS = (
    "automation", "automations", "home automation", "home control",
    "control", "lights", "light", "lighting", "the lights",
)


def is_automation(transcript: str) -> bool:
    """True only when the transcript clearly asks for home automation / lights
    (so the operator sets OP_RESULT=automation); False otherwise → fall through
    to room matching. Delegates to lights_match when present, with a stdlib-only
    fallback so this module never hard-depends on the sibling at import."""
    try:
        import lights_match  # noqa: PLC0415  (sibling on the same sys.path)
        return lights_match.is_automation_request(transcript)
    except Exception:  # noqa: BLE001  (matcher missing/broken → safe local check)
        low = " " + re.sub(r"[^a-z0-9 ]+", " ", (transcript or "").lower()) + " "
        low = re.sub(r"\s+", " ", low)
        return any(f" {w} " in low for w in _AUTOMATION_WORDS)


# Multi-word first so "wake up call" matches before a bare "wake"/"call" could;
# all are phrases no room name contains, so a real room never trips this.
_WAKEUP_PHRASES = (
    "wake up call", "wakeup call", "wake me up", "wake up", "wakeup",
    "morning call", "alarm call", "set a wake", "set an alarm", "wake call",
)


def is_wakeup_request(transcript: str) -> bool:
    """True only when the caller clearly asks the operator for a wake-up/alarm,
    so it routes into the wake-up flow instead of a room connection. Conservative
    (whole-phrase, word-boundary) so an ordinary room name never trips it."""
    low = " " + re.sub(r"[^a-z0-9 ]+", " ", (transcript or "").lower()) + " "
    low = re.sub(r"\s+", " ", low)
    return any(f" {w} " in low for w in _WAKEUP_PHRASES)


def match(transcript: str, rooms: list[dict], synonyms: dict | None = None,
          threshold: float = 0.6, margin: float = 0.08) -> tuple[str | None, float, str]:
    """Resolve a transcript to an extension. See module docstring."""
    valid = {str(r.get("ext", "")).strip() for r in rooms if str(r.get("ext", "")).strip()}
    toks = normalize(transcript)

    ext = _spoken_ext(toks, valid)
    if ext:
        return ext, 1.0, "number"
    if not toks:
        return None, 0.0, "empty"

    spoken = " ".join(toks)
    scored = sorted(
        ((ext, max((_score(toks, spoken, p) for p in phrases), default=0.0))
         for ext, phrases in _candidate_phrases(rooms, synonyms).items()),
        key=lambda kv: kv[1], reverse=True,
    )
    if not scored or scored[0][1] < threshold:
        return None, round(scored[0][1], 3) if scored else 0.0, "nomatch"
    # Anti-misroute: reject a near-tie between two plausible rooms.
    if len(scored) > 1 and scored[1][1] >= threshold and (scored[0][1] - scored[1][1]) < margin:
        return None, round(scored[0][1], 3), "ambiguous"
    return scored[0][0], round(scored[0][1], 3), "name"


# --------------------------------------------------------------------------- #
# Self-test:  python3 match.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    rooms = [
        {"ext": "11", "name": "Kitchen"},
        {"ext": "12", "name": "Living Room"},
        {"ext": "14", "name": "Master Bedroom"},
        {"ext": "15", "name": "Study"},
        {"ext": "17", "name": "Workshop"},
    ]
    syn = {"15": ["office", "den"], "17": ["garage", "shop"]}
    cases = [
        ("kitchen", "11"), ("the living room please", "12"), ("uh, master bedroom", "14"),
        ("connect me to the study", "15"), ("office", "15"), ("the garage", "17"),
        ("kitchin", "11"), ("living", "12"), ("extension eleven", "11"),
        ("one four", "14"), ("17", "17"), ("master", "14"),
        ("kitch", "11"), ("the work", "17"),       # recognizer-clipped word -> prefix match
        ("nobody", None), ("the basement", None), ("", None),
    ]
    passed = 0
    for transcript, expected in cases:
        got, score, reason = match(transcript, rooms, syn)
        ok = got == expected
        passed += ok
        print(f"  [{'ok ' if ok else 'FAIL'}] {transcript!r:34} -> ext={got} ({reason}, {score})  exp {expected}")
    # Ambiguity: two rooms whose names collide under fuzzing should re-prompt, not guess.
    amb_rooms = [{"ext": "21", "name": "Bedroom One"}, {"ext": "22", "name": "Bedroom Two"}]
    got, score, reason = match("bedroom", amb_rooms)
    amb_ok = got is None and reason == "ambiguous"
    passed += amb_ok
    print(f"  [{'ok ' if amb_ok else 'FAIL'}] {'bedroom (ambiguous)':34} -> ext={got} ({reason}, {score})  exp None/ambiguous")
    # Real-world regression: whisper clipped "Basement" -> "Base." over the
    # narrowband line; the prefix bonus must resolve it to Basement (was an
    # ambiguous tie with Master Bedroom before).
    prod = [{"ext": "14", "name": "Master Bedroom"}, {"ext": "18", "name": "Basement"}]
    got, score, reason = match("Base.", prod)
    base_ok = got == "18"
    passed += base_ok
    print(f"  [{'ok ' if base_ok else 'FAIL'}] {'Base. -> Basement':34} -> ext={got} ({reason}, {score})  exp 18")
    total = len(cases) + 2
    print(f"\n{passed}/{total} passed")
    raise SystemExit(0 if passed == total else 1)
