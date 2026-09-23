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
    # Under pytest the print + counter are DECORATIVE — only the __main__
    # runner reads _failures, so a failing check would still 'pass' the
    # test. Assert too, so both harnesses actually enforce every check.
    assert cond, name


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



# --------------------------------------------------------------------------- #
# ★ THE TONE MARKS THE TIME IT ANNOUNCED (v0.106.2). The tone used to follow
# the readout of the time it was STARTED at, so it sounded 4.5-8.6 s late. These
# pin the plan (announce a moment in the future, long enough ahead for its own
# readout) and the AGI that waits for that moment before sounding the tone.
# --------------------------------------------------------------------------- #
import time as _time

# The measured lengths of the shipped prompts (seconds), from the live PBX.
_DIGITS = {0: .87, 1: .91, 2: .75, 3: .84, 4: .80, 5: .82, 6: .88, 7: .82, 8: .69,
           9: .86, 10: .66, 11: .97, 12: .80, 13: .93, 14: 1.06, 15: 1.15, 16: 1.18,
           17: 1.20, 18: 1.10, 19: 1.24, 20: .93, 30: .90, 40: .95, 50: 1.22}
_OTHER = {cs.OH: .58, cs.HUNDRED: .85, cs.HOURS_AND: 1.46, cs.SECONDS: 1.05,
          "switchboard/sw-tone": .45}


def _dur(stem):
    if stem.startswith("digits/") and stem[7:].isdigit():
        return _DIGITS[int(stem[7:])]
    return _OTHER[stem]


def test_number_files_match_what_say_number_plays() -> None:
    check("files: 0", cs.number_files(0) == ["digits/0"])
    check("files: 7", cs.number_files(7) == ["digits/7"])
    check("files: 20 is one file", cs.number_files(20) == ["digits/20"])
    check("files: 21 is tens + units", cs.number_files(21) == ["digits/20", "digits/1"])
    check("files: 47", cs.number_files(47) == ["digits/40", "digits/7"])
    check("files: 50 is one file", cs.number_files(50) == ["digits/50"])


def test_readout_seconds_is_the_files_plus_a_cost_per_command() -> None:
    acts = cs.time_actions(16, 15, 47)      # 16, 15, hours-and, 47 (=40+7), seconds
    got = cs.readout_seconds(acts, _dur, per_action=0.0)
    check("readout: 16:15:47 is 6.61 s of audio", abs(got - 6.61) < 1e-9)
    check("readout: each command adds its overhead",
          abs(cs.readout_seconds(acts, _dur, per_action=0.1) - (6.61 + 0.5)) < 1e-9)


def test_plan_announces_a_second_its_readout_can_reach() -> None:
    lt = _time.gmtime
    for now in [1790254200.0 + k * 97.37 for k in range(900)]:   # spread over a day
        target, acts = cs.plan(now, _dur, localtime=lt)
        need = cs.readout_seconds(acts, _dur)
        tm = lt(target)
        assert acts == cs.time_actions(tm.tm_hour, tm.tm_min, tm.tm_sec), now
        assert target >= now + need + cs.TONE_MARGIN_S, (now, target, need)
        # ...and it is the FIRST such second: nothing between the earliest
        # candidate and the target would have fitted.
        for earlier in range(int(-(-(now + cs.TONE_MARGIN_S) // 1)), target):
            e = lt(earlier)
            ea = cs.time_actions(e.tm_hour, e.tm_min, e.tm_sec)
            assert earlier < now + cs.readout_seconds(ea, _dur) + cs.TONE_MARGIN_S, (now, earlier)
        # With the slack placed BEFORE the readout, the tone follows "…seconds"
        # by exactly the margin, whatever the time of day.
        lead = cs.lead_pause_s(now, target, acts, _dur)
        assert abs(target - (now + lead + need) - cs.TONE_MARGIN_S) < 1e-6, (now, target, lead)
        # ...and the beat before the numbers stays short: under 5 s even at the
        # top of the hour, where the longest readout gives way to the shortest.
        assert 0.0 <= lead < 5.0, (now, lead)
    check("plan: every target is reachable, minimal, and the tone follows the readout by the margin", True)


def test_plan_across_midnight_speaks_the_new_day() -> None:
    now = 86400.0 * 20000 - 3.2          # 23:59:56.8 UTC
    target, acts = cs.plan(now, _dur, localtime=_time.gmtime)
    tm = _time.gmtime(target)
    check("midnight: the target is past midnight", (tm.tm_hour, tm.tm_min) == (0, 0))
    check("midnight: read as 'oh oh hundred hours and ...'",
          acts[:3] == [oh(), oh(), HUN])


class _FakeAGI:
    """Plays the AGI's stdin/stdout on a fake clock: every STREAM FILE / SAY
    NUMBER advances the clock by that audio's length, sleep() advances it too,
    and each command is recorded with the time it was issued."""
    def __init__(self, start, hang_after=None):
        self.now, self.log, self.hang_after = start, [], hang_after
        self.env = ["agi_request: switchboard-clock.agi", ""]

    def readline(self):
        if self.env:
            return self.env.pop(0) + "\n"
        cmd = self.pending
        if self.hang_after is not None and len(self.log) > self.hang_after:
            return ""
        if cmd.startswith("STREAM FILE "):
            self.now += _dur(cmd.split()[2])
        elif cmd.startswith("SAY NUMBER "):
            self.now += sum(_dur(f) for f in cs.number_files(int(cmd.split()[2])))
        return "200 result=0\n"

    def write(self, s):
        self.pending = s.strip()
        self.log.append((round(self.now, 3), self.pending))

    def flush(self):
        pass


def _run_agi(start, hang_after=None):
    import sys as _sys
    agi_path = ROOT / "rootfs" / "var" / "lib" / "asterisk" / "agi-bin" / "switchboard-clock.agi"
    saved = (_sys.stdin, _sys.stdout, _sys.modules.get("clock_speak"))
    _sys.modules["clock_speak"] = cs
    try:
        mod = SourceFileLoader("sw_clock_agi", str(agi_path)).load_module()
    finally:
        if saved[2] is None:
            _sys.modules.pop("clock_speak", None)
        else:
            _sys.modules["clock_speak"] = saved[2]
    fake = _FakeAGI(start, hang_after)
    mod.clock_speak = cs
    mod.duration_of = _dur
    mod.time = type("T", (), {
        "time": staticmethod(lambda: fake.now),
        "sleep": staticmethod(lambda s: setattr(fake, "now", fake.now + s)),
        "localtime": staticmethod(_time.gmtime), "strftime": staticmethod(_time.strftime)})
    cs_localtime = cs.plan.__defaults__
    cs.plan.__defaults__ = (_time.gmtime, cs.TONE_MARGIN_S)
    try:
        _sys.stdin, _sys.stdout = fake, fake
        mod.main()
    finally:
        _sys.stdin, _sys.stdout = saved[0], saved[1]
        cs.plan.__defaults__ = cs_localtime
    return fake


def test_the_agi_sounds_the_tone_on_the_second_it_announced() -> None:
    start = 86400.0 * 20000 + 58547.3            # 16:15:47.3 UTC
    fake = _run_agi(start)
    cmds = [c for _t, c in fake.log]
    tone_at = next(t for t, c in fake.log if c.startswith("STREAM FILE switchboard/sw-tone"))
    target, acts = cs.plan(start, _dur, localtime=_time.gmtime)
    check("agi: the readout names the planned second",
          cmds[:len(acts)] == [f'STREAM FILE {a[1]} ""' if a[0] == "stream"
                               else f'SAY NUMBER {a[1]} ""' for a in acts])
    check("agi: the dialplan is told the tone is handled, before the wait",
          cmds[len(acts)] == "SET VARIABLE SW_CLOCK_TONE 1")
    check(f"agi: the tone starts ON the announced second (at {tone_at}, target {target})",
          abs(tone_at - target) < 0.01)
    last_word = max(t for t, c in fake.log if c == f'STREAM FILE {cs.SECONDS} ""')
    check("agi: the tone follows '…seconds' by the margin, not by the slack",
          abs(tone_at - (last_word + _dur(cs.SECONDS)) - cs.TONE_MARGIN_S) < 0.3)
    check("agi: the tone is the last thing it does", cmds[-1].startswith("STREAM FILE switchboard/sw-tone"))


def test_a_hangup_mid_readout_sounds_no_tone() -> None:
    fake = _run_agi(86400.0 * 20000 + 58547.3, hang_after=2)
    check("hangup: nothing after the hangup", not any("sw-tone" in c or "SET VARIABLE" in c
                                                      for _t, c in fake.log))


if __name__ == "__main__":
    test_two_digit_group()
    test_full_readouts()
    test_structure_invariants()
    test_saynumber_ranges()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
