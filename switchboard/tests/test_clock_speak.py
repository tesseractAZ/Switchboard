"""Tests for the talking-clock field sequencer (clock_speak).

Plain python3, no deps:

    python3 switchboard/tests/test_clock_speak.py

Covers two_digit_group (the hour/minute rule) and time_actions (the full
"military time with seconds" readout) — the exact spoken sequence for a given
time, verified without a phone. Actions are ("stream", path) [STREAM FILE] or
("num", n) [SAY NUMBER n].
"""

from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CS = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "clock" / "clock_speak.py"
cs = SourceFileLoader("clock_speak", str(CS)).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def oh():
    return ("stream", cs.OH)


def num(n):
    return ("num", n)


HA = ("stream", cs.HOURS_AND)
SEC = ("stream", cs.SECONDS)
HUN = ("stream", cs.HUNDRED)


def test_two_digit_group() -> None:
    check("group 0 -> oh oh", cs.two_digit_group(0) == [oh(), oh()])
    check("group 5 -> oh five", cs.two_digit_group(5) == [oh(), num(5)])
    check("group 9 -> oh nine", cs.two_digit_group(9) == [oh(), num(9)])
    check("group 10 -> ten", cs.two_digit_group(10) == [num(10)])
    check("group 32 -> thirty two", cs.two_digit_group(32) == [num(32)])
    check("group 59 -> fifty nine", cs.two_digit_group(59) == [num(59)])
    check("group 14 -> fourteen", cs.two_digit_group(14) == [num(14)])
    check("group 23 -> twenty three", cs.two_digit_group(23) == [num(23)])


def test_full_readouts() -> None:
    # 14:32:05 -> "fourteen  thirty-two  hours and  five  seconds"
    check("14:32:05",
          cs.time_actions(14, 32, 5) == [num(14), num(32), HA, num(5), SEC])
    # 14:00:05 -> "fourteen  hundred  hours and  five  seconds" (top of the hour)
    check("14:00:05 -> hundred",
          cs.time_actions(14, 0, 5) == [num(14), HUN, HA, num(5), SEC])
    # 09:05:30 -> "oh nine  oh five  hours and  thirty  seconds"
    check("09:05:30",
          cs.time_actions(9, 5, 30)
          == [oh(), num(9), oh(), num(5), HA, num(30), SEC])
    # 23:59:59 -> three plain numbers, then "hours and fifty-nine seconds"
    check("23:59:59",
          cs.time_actions(23, 59, 59) == [num(23), num(59), HA, num(59), SEC])
    # midnight 00:00:00 -> "oh oh  hundred  hours and  zero  seconds"
    check("00:00:00 -> oh oh hundred ... zero",
          cs.time_actions(0, 0, 0) == [oh(), oh(), HUN, HA, num(0), SEC])


def test_structure_invariants() -> None:
    # Every readout ends with the "hours and" prompt, the seconds number, and
    # the "seconds" prompt — in that order — and there is exactly one of each.
    for h in range(0, 24):
        for m in (0, 5, 30, 59):
            for s in (0, 7, 30, 59):
                acts = cs.time_actions(h, m, s)
                check(f"{h:02d}:{m:02d}:{s:02d} ends hours-and, <sec>, seconds",
                      acts[-3] == HA and acts[-2] == num(s) and acts[-1] == SEC)


def test_saynumber_ranges() -> None:
    # Sweep every H:M:S. The LAST num-action is always the seconds; any earlier
    # num-action belongs to an hour/minute group. Invariants:
    #   * hour/minute SAY NUMBER args are 1..59 (0 is spoken "oh oh", never 0)
    #   * the seconds SAY NUMBER arg is 0..59 (0 -> "zero") — the only place a 0
    #     is legitimately spoken via SAY NUMBER.
    hm_bad = sec_bad = 0
    for h in range(0, 24):
        for m in range(0, 60):
            for s in range(0, 60):
                nums = [a[1] for a in cs.time_actions(h, m, s) if a[0] == "num"]
                if any(not (1 <= n <= 59) for n in nums[:-1]):
                    hm_bad += 1
                if not (0 <= nums[-1] <= 59):
                    sec_bad += 1
    check("hour/minute SAY NUMBER args always 1..59 (0 -> 'oh oh')", hm_bad == 0)
    check("seconds SAY NUMBER arg always 0..59", sec_bad == 0)
    check("seconds 0 IS spoken as SAY NUMBER 0 ('zero')",
          cs.time_actions(12, 30, 0)[-2] == num(0))
    check("folding: minute 60 treated as 0 -> hundred",
          ("stream", cs.HUNDRED) in cs.time_actions(12, 60, 5))


if __name__ == "__main__":
    test_two_digit_group()
    test_full_readouts()
    test_structure_invariants()
    test_saynumber_ranges()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
