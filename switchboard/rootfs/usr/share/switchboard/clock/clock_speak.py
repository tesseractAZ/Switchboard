"""Speaking-clock field sequencer — pure, unit-tested (stdlib only, musl-safe).

Turns a wall-clock H:M:S (24-hour) into the ordered list of prompt ACTIONS the
talking-clock AGI plays, phrased as spoken 24-hour ("military") time WITH the
connective words 'hundred' / 'hours' / 'seconds':

    14:32:05 -> "fourteen  thirty-two  hours and  five   seconds"
    14:00:05 -> "fourteen  hundred     hours and  five   seconds"   (top of the hour)
    09:05:30 -> "oh nine   oh five     hours and  thirty seconds"
    23:59:59 -> "twenty-three  fifty-nine  hours and  fifty-nine  seconds"
    00:00:00 -> "oh oh     hundred     hours and  zero   seconds"   (midnight)

Building blocks: the professional recorded digit files Asterisk ships
(``SAY NUMBER`` + ``digits/oh`` + ``digits/hundred``) plus two short espeak
prompts for the words that don't exist in core-sounds (``sw-hours-and`` =
"hours, and", ``sw-seconds`` = "seconds").

Each action is one of:
    ("num", n)     -> AGI: SAY NUMBER n     (0 <= n <= 59; 0 speaks "zero")
    ("stream", f)  -> AGI: STREAM FILE f    (a sound-file path)

Hour and minute are read as two-digit groups (0 -> "oh oh", 1-9 -> "oh <n>",
10-59 -> "<n>"); a :00 minute becomes "hundred". The seconds are read as a plain
cardinal ("five", "thirty", "zero") set off by the "hours, and" prompt, so they
never blend into the H:M groups. Pure data transform (no AGI I/O, no clock read)
-> the exact spoken sequence for any time is testable without a phone.
"""
from __future__ import annotations

OH = "digits/oh"
HUNDRED = "digits/hundred"
HOURS_AND = "switchboard/sw-hours-and"   # espeak: "hours, and"
SECONDS = "switchboard/sw-seconds"       # espeak: "seconds"


def two_digit_group(value: int) -> list:
    """One hour (0-23) or a NON-zero minute, spoken as a two-digit group.

    Defensive: any out-of-range input is folded into 0-59 so the AGI is never
    handed a bogus ``SAY NUMBER`` argument."""
    v = int(value) % 60
    if v == 0:
        return [("stream", OH), ("stream", OH)]      # "oh oh"
    if v < 10:
        return [("stream", OH), ("num", v)]          # "oh five"
    return [("num", v)]                              # "thirty two" / "fourteen"


def time_actions(hour: int, minute: int, second: int) -> list:
    """The full readout: <H group> <M group | "hundred"> "hours and" <sec> "seconds"."""
    acts = two_digit_group(hour)
    if int(minute) % 60 == 0:
        acts.append(("stream", HUNDRED))             # "... hundred" (top of the hour)
    else:
        acts += two_digit_group(minute)
    acts.append(("stream", HOURS_AND))               # "hours, and"
    acts.append(("num", int(second) % 60))           # seconds as a cardinal (0 -> "zero")
    acts.append(("stream", SECONDS))                 # "seconds"
    return acts
