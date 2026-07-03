"""Tests for the talking-clock field sequencer (clock_speak).

Plain python3, no deps:

    python3 switchboard/tests/test_clock_speak.py

Covers field_actions (the two-digit-group rule) and time_actions (the full
H:M:S readout) — the exact spoken sequence for a given time, verified without a
phone. Actions are ('oh',) [stream digits/oh] or ('num', n) [SAY NUMBER n].
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


OH = ("oh",)


def num(n):
    return ("num", n)


def test_field_actions() -> None:
    # 0 -> "oh oh"
    check("field 0 -> oh oh", cs.field_actions(0) == [OH, OH])
    # 1..9 -> "oh <n>"
    check("field 5 -> oh five", cs.field_actions(5) == [OH, num(5)])
    check("field 9 -> oh nine", cs.field_actions(9) == [OH, num(9)])
    check("field 1 -> oh one", cs.field_actions(1) == [OH, num(1)])
    # 10..59 -> "<n>"
    check("field 10 -> ten", cs.field_actions(10) == [num(10)])
    check("field 32 -> thirty two", cs.field_actions(32) == [num(32)])
    check("field 59 -> fifty nine", cs.field_actions(59) == [num(59)])
    # hour range values (14, 20, 23) speak as a single number
    check("field 14 -> fourteen", cs.field_actions(14) == [num(14)])
    check("field 20 -> twenty", cs.field_actions(20) == [num(20)])
    check("field 23 -> twenty three", cs.field_actions(23) == [num(23)])


def test_field_actions_never_bogus_saynumber() -> None:
    # A SAY NUMBER argument must always be 1..59 (never 0, negative, or >=60):
    # 0 is spoken as two "oh"s, and any out-of-range input is folded defensively
    # so the AGI can never emit `SAY NUMBER 0` / a negative / an oversized arg.
    for v in list(range(0, 60)) + [-1, 60, 99, 100, 1000]:
        acts = cs.field_actions(v)
        for a in acts:
            if a[0] == "num":
                check(f"field {v}: SAY NUMBER arg {a[1]} in 1..59",
                      isinstance(a[1], int) and 1 <= a[1] <= 59)
    # spot-check the fold targets a sane value, not a crash
    check("field 60 folds to 0 -> oh oh", cs.field_actions(60) == [OH, OH])
    check("field -1 folds without a negative SAY NUMBER",
          all(a[0] != "num" or a[1] >= 1 for a in cs.field_actions(-1)))


def test_time_actions() -> None:
    # 14:32:05 -> "fourteen  thirty-two  oh five"
    check("14:32:05",
          cs.time_actions(14, 32, 5) == [num(14), num(32), OH, num(5)])
    # 09:05:00 -> "oh nine  oh five  oh oh"
    check("09:05:00",
          cs.time_actions(9, 5, 0) == [OH, num(9), OH, num(5), OH, OH])
    # 23:59:59 -> three plain numbers
    check("23:59:59",
          cs.time_actions(23, 59, 59) == [num(23), num(59), num(59)])
    # midnight 00:00:00 -> six "oh"s
    check("00:00:00 -> six oh",
          cs.time_actions(0, 0, 0) == [OH] * 6)
    # top of the hour 14:00:00 -> "fourteen  oh oh  oh oh"
    check("14:00:00",
          cs.time_actions(14, 0, 0) == [num(14), OH, OH, OH, OH])
    # order is strictly hour, then minute, then second (never reordered)
    check("field order is H then M then S",
          cs.time_actions(1, 2, 3) == [OH, num(1), OH, num(2), OH, num(3)])


if __name__ == "__main__":
    test_field_actions()
    test_field_actions_never_bogus_saynumber()
    test_time_actions()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
