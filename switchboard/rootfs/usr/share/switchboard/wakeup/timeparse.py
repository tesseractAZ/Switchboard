"""Parse a spoken time into "HH:MM" (24-hour), or None.

Rotary phones can't key in digits mid-call, so a wake-up time is *spoken* and
transcribed by whisper. whisper's output for a time is varied — "seven a m",
"7:30", "half past six", "quarter to eight", "nineteen thirty", "noon" — so this
parser is deliberately forgiving. It never has to be perfect: the wake-up flow
reads the parsed time back to the caller, who re-says it if it's wrong.

Pure + stdlib; unit-tested in tests/test_wakeup.py.
"""

from __future__ import annotations

import re

_ONES = {
    "zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}


def _normalize(text: str) -> str:
    t = (text or "").lower().strip()
    t = t.replace("a.m.", " am ").replace("p.m.", " pm ")
    t = t.replace("o'clock", " oclock ").replace("o' clock", " oclock ").replace("o clock", " oclock ")
    t = re.sub(r"\ba\.?\s*m\b", " am ", t)
    t = re.sub(r"\bp\.?\s*m\b", " pm ", t)
    t = t.replace("-", " ")
    t = re.sub(r"[^a-z0-9: ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _ampm(text: str):
    """Return 'am'/'pm'/None inferred from the phrase."""
    if " pm " in f" {text} " or "evening" in text or "tonight" in text or "afternoon" in text or "night" in text:
        return "pm"
    if " am " in f" {text} " or "morning" in text:
        return "am"
    return None


def _two_word_number(tokens):
    """Consume a 1-or-2 word cardinal from the front of tokens -> (value, rest).
    Handles "twenty three", "thirty", "seven". Returns (None, tokens) if none."""
    if not tokens:
        return None, tokens
    w = tokens[0]
    if w.isdigit():
        return int(w), tokens[1:]
    if w in _TENS:
        if len(tokens) > 1 and tokens[1] in _ONES and 1 <= _ONES[tokens[1]] <= 9:
            return _TENS[w] + _ONES[tokens[1]], tokens[2:]
        return _TENS[w], tokens[1:]
    if w in _ONES:
        return _ONES[w], tokens[1:]
    return None, tokens


def _apply_ampm(h: int, m: int, ampm) -> str:
    if not (0 <= m <= 59):
        return None
    if ampm == "am":
        if h == 12:
            h = 0
        elif not (1 <= h <= 12):
            return None
    elif ampm == "pm":
        if h == 12:
            pass
        elif 1 <= h <= 11:
            h += 12
        elif not (0 <= h <= 23):
            return None
    else:
        # No am/pm said. A wake-up is almost always a morning, so 1-11 -> AM;
        # 12 -> noon; 13-23 already 24h.
        if not (0 <= h <= 23):
            return None
    if not (0 <= h <= 23):
        return None
    return f"{h:02d}:{m:02d}"


def parse(text: str):
    t = _normalize(text)
    if not t:
        return None
    ampm = _ampm(t)
    # Strip the am/pm / time-of-day words now that we've captured intent.
    core = re.sub(r"\b(am|pm|morning|evening|afternoon|night|tonight|at|the|in|please|wake|me|up|call|for|a|set)\b", " ", t)
    core = re.sub(r"\s+", " ", core).strip()

    # Word-boundary match so "afternoon" doesn't trigger "noon". Skip when a
    # relative phrase is present ("half past noon") so it falls through rather
    # than confidently returning the wrong 12:00.
    _rel = re.search(r"\b(past|to|after|til|till|before)\b", core)
    if not _rel:
        if re.search(r"\bnoon\b", t):
            return "12:00"
        if re.search(r"\bmidnight\b", t):
            return "00:00"

    # Digit clock: 7:30, 07:30, 19:30
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        return _apply_ampm(int(m.group(1)), int(m.group(2)), ampm)

    # Bare 3-4 digit military-ish: "0730", "730", "1930"
    m = re.fullmatch(r"(\d{3,4})", core)
    if m:
        v = m.group(1).zfill(4)
        return _apply_ampm(int(v[:2]), int(v[2:]), ampm)

    tokens = core.split()

    # A leading "oh"/"zero"/"o" is filler before a military-style time
    # ("oh seven hundred" -> 07:00, "zero seven thirty" -> 07:30) — don't consume
    # it as hour 0. (An "oh" between hour and minutes is handled separately.)
    if len(tokens) > 1 and tokens[0] in ("oh", "o", "zero"):
        tokens = tokens[1:]

    # "(quarter|half) (past|to) <hour>"  and  "<minutes> (past|to) <hour>"
    if "past" in tokens or "to" in tokens or "after" in tokens or "til" in tokens or "till" in tokens or "before" in tokens:
        rel = "to" if ("to" in tokens or "til" in tokens or "till" in tokens or "before" in tokens) else "past"
        idx = next((i for i, w in enumerate(tokens) if w in ("past", "to", "after", "til", "till", "before")), None)
        if idx is not None:
            left = tokens[:idx]
            right = tokens[idx + 1:]
            # minutes from the left side
            if left and left[0] == "quarter":
                mins = 15
            elif left and left[0] == "half":
                mins = 30
            else:
                mins, _ = _two_word_number(left)
            hour, _ = _two_word_number(right)
            if mins is not None and hour is not None:
                if rel == "to":
                    hour = (hour - 1) % 24
                    mins = 60 - mins
                return _apply_ampm(hour, mins % 60, ampm)

    # "<hour> hundred"  (e.g. "seven hundred" -> 7:00, "nineteen hundred")
    if "hundred" in tokens:
        hi = tokens.index("hundred")
        hour, _ = _two_word_number(tokens[:hi])
        if hour is not None:
            return _apply_ampm(hour, 0, ampm)

    # "<hour> oclock"
    if "oclock" in tokens:
        hour, _ = _two_word_number(tokens[: tokens.index("oclock")])
        if hour is not None:
            return _apply_ampm(hour, 0, ampm)

    # "<hour> [oh] <minutes>" / "<hour> <minutes>" / bare "<hour>"
    hour, rest = _two_word_number(tokens)
    if hour is None:
        return None
    if not rest:
        return _apply_ampm(hour, 0, ampm)
    # "<hour> oh <m>" -> minutes 0X
    if rest[0] in ("oh", "o", "zero") and len(rest) > 1:
        lo, _ = _two_word_number(rest[1:])
        if lo is not None and lo < 10:
            return _apply_ampm(hour, lo, ampm)
    mins, _ = _two_word_number(rest)
    if mins is not None:
        # A "military" reading like "nineteen thirty" -> 19:30 keeps hour as-is.
        return _apply_ampm(hour, mins, ampm)
    return _apply_ampm(hour, 0, ampm)
