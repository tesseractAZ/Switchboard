"""Speaking-clock field sequencer — pure, unit-tested (stdlib only, musl-safe).

Turns a wall-clock H:M:S (24-hour) into the ordered list of prompt actions the
talking-clock AGI streams. Each of the three fields is spoken as a natural
two-digit group, the way a 24-hour ("military") clock is read aloud, using ONLY
the digit sound files that ship with Asterisk's core-sounds (no "hours" /
"minutes" / "seconds" word files exist, and SayUnixTime's own 24h format is too
quirky — its ``M`` says "o'clock" for :00):

    0        -> "oh oh"        (double-oh; e.g. midnight hour, or top of a minute)
    1..9     -> "oh" + <n>     ("oh five")
    10..59   -> <n>            ("thirty two", "fourteen", "twenty")

So 14:32:05 speaks "fourteen  thirty-two  oh five", 09:05:00 speaks
"oh nine  oh five  oh oh".

Each action is one of:
  ("oh",)     -> the AGI streams ``digits/oh``
  ("num", n)  -> the AGI issues ``SAY NUMBER n`` (0 < n < 60)

Keeping this a pure data transform (no AGI I/O, no clock read) means the exact
spoken sequence for any time is testable without a phone or a wall clock.
"""
from __future__ import annotations


def field_actions(value: int) -> list:
    """Spoken actions for one clock field (hour 0-23, minute/second 0-59).

    Defensive: any out-of-range input is folded into 0-59 so the AGI can never
    be handed a bogus ``SAY NUMBER`` argument (localtime never produces one, but
    a caller of this helper might)."""
    v = int(value) % 60
    if v == 0:
        return [("oh",), ("oh",)]
    if v < 10:
        return [("oh",), ("num", v)]
    return [("num", v)]


def time_actions(hour: int, minute: int, second: int) -> list:
    """The full H:M:S readout: three two-digit groups, in order."""
    return field_actions(hour) + field_actions(minute) + field_actions(second)
