"""Tests for the wake-up spoken-time parser + store. Plain python3, no deps.

    python3 switchboard/tests/test_wakeup.py
"""
import datetime
import os
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

WK = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share" / "switchboard" / "wakeup"
timeparse = SourceFileLoader("sw_timeparse", str(WK / "timeparse.py")).load_module()

# Point the store at a throwaway file BEFORE loading it (PATH is read at import).
os.environ["SWITCHBOARD_WAKEUPS"] = os.path.join(tempfile.mkdtemp(), "wakeups.json")
store = SourceFileLoader("sw_store", str(WK / "store.py")).load_module()

_failures = 0


def check(name, cond):
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def eq(spoken, expected):
    got = timeparse.parse(spoken)
    check(f"parse {spoken!r} -> {expected}  (got {got})", got == expected)


def test_timeparse():
    # Bare / am-pm
    eq("seven am", "07:00")
    eq("seven a m", "07:00")
    eq("seven", "07:00")
    eq("seven o'clock", "07:00")
    eq("seven oclock", "07:00")
    eq("seven p m", "19:00")
    # Hour + minutes
    eq("seven thirty", "07:30")
    eq("seven thirty am", "07:30")
    eq("seven thirty pm", "19:30")
    eq("six forty five", "06:45")
    eq("seven oh five", "07:05")
    eq("seven o five", "07:05")
    eq("seven fifteen pm", "19:15")
    # past / to / quarter / half
    eq("half past six", "06:30")
    eq("quarter past seven", "07:15")
    eq("quarter to eight", "07:45")
    eq("ten to seven", "06:50")
    eq("twenty past six", "06:20")
    # noon / midnight / twelve
    eq("noon", "12:00")
    eq("midnight", "00:00")
    eq("twelve thirty pm", "12:30")
    eq("twelve am", "00:00")
    eq("twelve pm", "12:00")
    # digit clocks
    eq("7:30", "07:30")
    eq("07:30", "07:30")
    eq("19:30", "19:30")
    # military
    eq("nineteen thirty", "19:30")
    eq("seven hundred", "07:00")
    eq("nineteen hundred", "19:00")
    # time-of-day words
    eq("eight in the morning", "08:00")
    eq("eight in the evening", "20:00")
    # "afternoon" must NOT collide with the "noon" substring (review HIGH)
    eq("two in the afternoon", "14:00")
    eq("five in the afternoon", "17:00")
    eq("four o'clock in the afternoon", "16:00")
    eq("at noon", "12:00")
    eq("high noon", "12:00")
    # military with a leading filler word
    eq("oh seven hundred", "07:00")
    eq("zero seven thirty", "07:30")
    # nonsense -> None
    eq("hello there operator", None)
    eq("", None)
    eq("kitchen please", None)


def test_store_set_get():
    now = 1781000000.0
    base = datetime.datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0)
    ahead = base + datetime.timedelta(hours=1)
    e = store.set_wakeup("11", ahead.strftime("%H:%M"), now_epoch=now)
    check("store: target is today when time is still ahead", e["target_epoch"] == int(ahead.timestamp()))
    check("store: get returns the entry", store.get("11")["hhmm"] == ahead.strftime("%H:%M"))
    check("store: all_wakeups includes it", "11" in store.all_wakeups())
    behind = base - datetime.timedelta(hours=1)
    e2 = store.set_wakeup("12", behind.strftime("%H:%M"), now_epoch=now)
    check("store: target rolls to tomorrow when time has passed",
          e2["target_epoch"] == int((behind + datetime.timedelta(days=1)).timestamp()))
    check("store: cancel returns True + removes", store.cancel("12") is True and store.get("12") is None)
    check("store: cancel of a missing ext is False", store.cancel("99") is False)


def test_store_cancel_if():
    now = 1781000000.0
    e = store.set_wakeup("13", "06:30", now_epoch=now)
    tgt = e["target_epoch"]
    # Wrong epoch (e.g. it was re-set) must NOT delete it.
    check("cancel_if: stale epoch does not remove", store.cancel_if("13", tgt - 999) is False and store.get("13") is not None)
    # Matching epoch removes it.
    check("cancel_if: matching epoch removes", store.cancel_if("13", tgt) is True and store.get("13") is None)


def test_store_due():
    for k in list(store.all_wakeups()):
        store.cancel(k)
    now = 1781000000.0
    store.set_wakeup("11", "07:00", now_epoch=now)
    tgt = store.get("11")["target_epoch"]
    fired, missed = store.due(tgt - 10)
    check("due: before the time -> nothing", not fired and not missed)
    fired, missed = store.due(tgt + 5)
    check("due: at/after the time within grace -> fired", any(x[0] == "11" for x in fired))
    check("due: a fired wake-up is left for the scheduler to remove", store.get("11") is not None)
    fired, missed = store.due(tgt + store.GRACE_SECONDS + 60)
    check("due: past the grace window -> missed and removed",
          any(x[0] == "11" for x in missed) and store.get("11") is None)


def main():
    test_timeparse()
    test_store_set_get()
    test_store_cancel_if()
    test_store_due()
    print()
    if _failures:
        print(f"{_failures} FAILURE(S)")
        raise SystemExit(1)
    print("all wakeup tests passed")


if __name__ == "__main__":
    main()
