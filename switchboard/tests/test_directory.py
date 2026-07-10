"""Tests for the directory-assistance helpers (operator/directory.py).

    python3 switchboard/tests/test_directory.py
"""
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "operator" / "directory.py"
d = SourceFileLoader("directory", str(D)).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


ROOMS = [{"ext": "11", "name": "Kitchen"}, {"ext": "16", "name": "Office"},
         {"ext": "19", "name": "Cordless Phone"}]


def test_list_request() -> None:
    for t in ("list", "the rooms", "read the list", "what are the rooms", "directory", "everyone"):
        check(f"is_list_request({t!r})", d.is_list_request(t) is True)
    for t in ("kitchen", "office", "connect me to the garage", ""):
        check(f"NOT list: {t!r}", d.is_list_request(t) is False)


def test_cancel() -> None:
    for t in ("goodbye", "never mind", "cancel", "hang up", "nothing"):
        check(f"is_cancel({t!r})", d.is_cancel(t) is True)
    check("room name is not cancel", d.is_cancel("kitchen") is False)


def test_announce_text() -> None:
    check("announce one room", d.announce_text("Kitchen", "11") == "Kitchen, extension 11.")


def test_directory_text() -> None:
    txt = d.directory_text(ROOMS)
    check("directory reads each room + ext",
          "Kitchen, extension 11" in txt and "Office, extension 16" in txt
          and "Cordless Phone, extension 19" in txt and txt.startswith("The rooms are:"))
    check("empty directory is graceful", d.directory_text([]) == "The directory is empty.")
    check("directory skips rooms with no ext",
          "extension 12" not in d.directory_text([{"name": "Bad"}]))


def test_name_for() -> None:
    check("ext -> name", d.name_for(ROOMS, "16") == "Office")
    check("unknown ext -> the ext itself", d.name_for(ROOMS, "99") == "99")


# The room matcher used by resolve(): reuse the real one so tests exercise the
# actual fuzzy behaviour that produced the wrong-room-dial bug.
_m = SourceFileLoader("match", str(ROOT / "rootfs" / "usr" / "share" / "switchboard" / "operator" / "match.py")).load_module()
_ROOMS = [{"ext": "11", "name": "Kitchen"}, {"ext": "12", "name": "Living Room"},
          {"ext": "14", "name": "Master Bedroom"}, {"ext": "15", "name": "Guest Room"},
          {"ext": "16", "name": "Office"}, {"ext": "19", "name": "Cordless Phone"}]


def _resolve(text, rooms=_ROOMS):
    return d.resolve(text, lambda t: _m.match(t, rooms, {}, 0.6))


def test_resolve_misheard_list_never_dials() -> None:
    # THE BUG: a mis-heard "list" used to clear the fuzzy room threshold and CONNECT.
    # 'listing'->Living Room(0.77), 'least'->Guest Room, 'last'->Master were live dials.
    for t in ["list", "List", "lists", "listing", "Lift", "lift", "least", "last",
              "wrist", "the list", "read the list", "directory", "everyone"]:
        act, ext = _resolve(t)
        check(f"misheard-list {t!r} -> list/never-connect", act == "list")


def test_resolve_real_rooms_still_connect() -> None:
    for name, ext in [("Kitchen", "11"), ("Living Room", "12"), ("Office", "16"),
                      ("Master Bedroom", "14"), ("Guest Room", "15"), ("Cordless Phone", "19")]:
        act, got = _resolve(name)
        check(f"room {name!r} -> connect {ext}", act == "connect" and got == ext)
    for t, ext in [("Kitch", "11"), ("Off", "16"), ("Guest", "15")]:
        act, got = _resolve(t)
        check(f"clip {t!r} -> connect {ext}", act == "connect" and got == ext)


def test_resolve_unshadows_intent_named_rooms() -> None:
    # A room literally named like an intent keyword must still be reachable (strong
    # match wins before the intent gates).
    for name in ["List", "Directory", "Everyone", "Cancel", "Goodbye"]:
        rooms = _ROOMS + [{"ext": "21", "name": name}]
        act, ext = _resolve(name, rooms)
        check(f"room named {name!r} reachable -> connect 21", act == "connect" and ext == "21")


def test_resolve_cancel_and_garbage_never_dial() -> None:
    for t in ["goodbye", "cancel", "never mind", "hang up", "nothing", "forget it", "no thanks"]:
        act, _ = _resolve(t)
        check(f"cancel {t!r}", act == "cancel")
    for t in ["", "   ", "banana", "zzzz", "hello there", "what"]:
        act, ext = _resolve(t)
        check(f"garbage {t!r} never connects", act != "connect")


def test_resolve_stopword_guard() -> None:
    # ratio('is','list')==0.667 but len<4 so 'is' can't trigger a list request.
    act, ext = _resolve("where is the office")
    check("'where is the office' -> connect Office (stopword 'is' not a list req)",
          act == "connect" and ext == "16")


if __name__ == "__main__":
    test_list_request()
    test_cancel()
    test_announce_text()
    test_directory_text()
    test_name_for()
    test_resolve_misheard_list_never_dials()
    test_resolve_real_rooms_still_connect()
    test_resolve_unshadows_intent_named_rooms()
    test_resolve_cancel_and_garbage_never_dial()
    test_resolve_stopword_guard()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
