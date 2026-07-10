"""Match a spoken phrase from the dial-a-status voice menu to one category:
``power`` | ``weather`` | ``house`` (or '' when nothing is plausible, so the AGI
re-prompts). Stdlib only, narrowband-aware (fuzzy + word-prefix) like the other
matchers — antique handsets clip word tails ("weath" for "weather").
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Primary word(s) the menu asks for, plus a few natural synonyms. Kept distinct
# per category so "temperature" (inside) reads as house, "outside/forecast" as
# weather, and "battery/solar/grid" as power.
_CATS = {
    "power": ["power", "battery", "solar", "grid", "energy", "electricity", "charge"],
    "weather": ["weather", "forecast", "outside", "rain", "sun", "hot", "cold", "degrees"],
    "house": ["house", "home", "status", "thermostat", "lights", "climate", "inside", "temperature"],
}

# Spoken digits, so "one/two/three" map to the menu order too.
_DIGIT_WORDS = {"one": "power", "two": "weather", "three": "house"}
_DIGIT_KEYS = {"1": "power", "2": "weather", "3": "house"}

# Words that unambiguously end the menu loop.
_GOODBYE_STRONG = {"goodbye", "bye", "done", "exit", "quit", "cancel",
                   "nothing", "stop", "finished"}
# Polite fillers that end the loop ONLY when the caller isn't also asking for a
# category — "power, thanks" / "no, the weather" must still serve the request
# instead of hanging up on the caller mid-menu.
_GOODBYE_WEAK = {"no", "nope", "thanks", "thank"}


def is_goodbye(text: str) -> bool:
    """True if the caller is asking to end (so the status menu can stop looping)."""
    words = set(normalize(text))
    t = " ".join(normalize(text))
    if words & _GOODBYE_STRONG or "hang up" in t or "thats all" in t:
        return True
    # A bare "no"/"thanks" ends it; a "no" or "thanks" alongside a real request
    # (which match() resolves) does not.
    return bool(words & _GOODBYE_WEAK) and not match(text)


def normalize(text: str) -> list:
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split() if w]


def from_digit(key: str) -> str:
    """A DTMF keypress (SIP phones) -> category; '' if not 1/2/3."""
    return _DIGIT_KEYS.get((key or "").strip(), "")


def match(text: str) -> str:
    """Best category for a spoken phrase, or '' if none is confident enough."""
    words = normalize(text)
    if not words:
        return ""
    wset = set(words)
    best, best_score = "", 0.0
    for cat, keys in _CATS.items():
        score = 0.0
        for k in keys:
            if k in wset:
                score = 1.0
                break
            for w in words:
                if len(w) >= 3 and (k.startswith(w) or w.startswith(k)):
                    score = max(score, 0.85)
                score = max(score, SequenceMatcher(None, w, k).ratio())
        if score > best_score:
            best, best_score = cat, score
    # An explicit category keyword ("the one about the weather") beats a bare
    # spoken ordinal, so resolve exact keyword hits first.
    if best_score >= 1.0:
        return best
    # Otherwise a bare spoken "one/two/three" maps to the menu order.
    for w in words:
        if w in _DIGIT_WORDS:
            return _DIGIT_WORDS[w]
    return best if best_score >= 0.7 else ""
