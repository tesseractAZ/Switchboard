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


if __name__ == "__main__":
    test_list_request()
    test_cancel()
    test_announce_text()
    test_directory_text()
    test_name_for()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
