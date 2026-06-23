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
    return score


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
    total = len(cases) + 1
    print(f"\n{passed}/{total} passed")
    raise SystemExit(0 if passed == total else 1)
