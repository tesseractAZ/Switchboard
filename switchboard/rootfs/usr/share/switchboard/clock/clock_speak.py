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

import math
import time

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


# --------------------------------------------------------------------------- #
# ★ THE TONE MARKS THE TIME IT ANNOUNCED (v0.106.2).
#
# The readout used to be the time the AGI STARTED speaking, and the tone came
# after it — so the tone sounded as late as the readout was long: 4.5 to 8.6 s
# with the shipped prompts (median 6.8 s), different for every time of day, and
# never on the second it had just named. A speaking clock's contract runs the
# other way: it names a moment a little in the FUTURE and sounds the tone exactly
# then. plan() picks that moment from the real lengths of the files the readout
# will play, so the tone can wait for it.
# --------------------------------------------------------------------------- #
# What each readout command costs beyond its audio: the AGI round trip plus the
# 20 ms frame alignment of the next playback.
ACTION_OVERHEAD_S = 0.05
# Headroom between the end of the readout and the tone, so a readout that runs a
# little long still finishes before the second it named. The pause the caller
# hears before the tone is this plus the rounding up to a whole second.
TONE_MARGIN_S = 0.4


def number_files(n: int) -> list:
    """The prompt files Asterisk's English ``SAY NUMBER n`` plays, 0-59: one
    file up to twenty, then the tens and (if any) the units."""
    n = int(n) % 60
    if n <= 20:
        return [f"digits/{n}"]
    tens, units = n // 10 * 10, n % 10
    return [f"digits/{tens}"] + ([f"digits/{units}"] if units else [])


def readout_seconds(actions, duration_of, per_action: float = ACTION_OVERHEAD_S) -> float:
    """How long ``actions`` take to play, from each file's length (``duration_of``
    maps a sound path to seconds) plus a fixed cost per command."""
    total = 0.0
    for kind, arg in actions:
        files = number_files(arg) if kind == "num" else [arg]
        total += sum(duration_of(f) for f in files) + per_action
    return total


def plan(now: float, duration_of, localtime=time.localtime,
         margin: float = TONE_MARGIN_S) -> tuple:
    """(target, actions): the first whole second whose own readout, started at
    ``now``, ends at least ``margin`` before it — and that readout.

    Each candidate second is judged on ITS readout, because the length changes
    with the digits: "fifty-seven" is longer than "ten", and a new minute or hour
    changes the groups too. Bounded: no readout is anywhere near 30 s."""
    t = math.ceil(now + margin)
    actions = []
    for _ in range(30):
        lt = localtime(t)
        actions = time_actions(lt.tm_hour, lt.tm_min, lt.tm_sec)
        if t >= now + readout_seconds(actions, duration_of) + margin:
            return t, actions
        t += 1
    return t, actions


def lead_pause_s(now: float, target: int, actions, duration_of,
                 margin: float = TONE_MARGIN_S) -> float:
    """How long to wait BEFORE the readout so that it ends ``margin`` before the
    tone. The slack a planned second leaves is placed ahead of the numbers, not
    behind them: rounding up to a whole second, and a candidate whose readout was
    too long for the time left (the top of the hour, where "fifty-nine … fifty-
    nine seconds" gives way to the much shorter "… hundred hours and zero
    seconds"), leave up to four seconds of it. After "…the time will be" a beat
    reads naturally; between "…seconds" and the tone it reads as a broken clock.
    So the tone always follows the readout by the same short gap."""
    return max(0.0, target - now - readout_seconds(actions, duration_of) - margin)
