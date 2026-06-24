"""Behavioral tests for the telnet operator console (console/console.py).

Run with plain Python (no deps):

    python3 switchboard/tests/test_console.py

Covers the pure pieces: telnet/ANSI input parsing, board rendering, and the
action key handling (navigation + mode transitions + guarded actions). The AMI
side effects (ring/connect/hangup) are exercised only on their guard paths,
which never open a socket.
"""
from importlib.machinery import SourceFileLoader
from pathlib import Path

CONSOLE_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share" / "switchboard" / "console" / "console.py"
console = SourceFileLoader("switchboard_console", str(CONSOLE_PATH)).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def _board(rooms, calls=None, ami_ok=True):
    b = console.Board()
    b.set({"ami_ok": ami_ok, "rooms": rooms, "calls": calls or [], "ts": 0.0})
    return b


def test_parse_input() -> None:
    IAC, DO, OPT_NAWS, SB, SE = console.IAC, console.DO, console.OPT_NAWS, console.SB, console.SE
    # IAC negotiation is stripped; the trailing real key survives.
    ev, rest = console.parse_input(bytes([IAC, DO, OPT_NAWS]) + b"r")
    check("parse: IAC stripped, key kept", ev == [("key", "r")] and rest == b"")
    # Arrow key (CSI A).
    ev, _ = console.parse_input(b"\x1b[A")
    check("parse: up arrow", ev == [("key", "up")])
    # CRLF collapses to one enter.
    ev, _ = console.parse_input(b"\r\n")
    check("parse: CRLF -> one enter", ev == [("key", "enter")])
    # Printable run.
    ev, _ = console.parse_input(b"abc")
    check("parse: printable run", ev == [("key", "a"), ("key", "b"), ("key", "c")])
    # NAWS window size.
    naws = bytes([IAC, SB, OPT_NAWS, 0, 100, 0, 30, IAC, SE])
    ev, _ = console.parse_input(naws)
    check("parse: NAWS resize 100x30", ev == [("naws", 100, 30)])
    # Incomplete trailing IAC is held back in rest.
    ev, rest = console.parse_input(b"x" + bytes([IAC]))
    check("parse: incomplete IAC held in rest", ev == [("key", "x")] and rest == bytes([IAC]))


ROOMS = [
    {"ext": "11", "label": "Kitchen", "registered": True, "device_state": "Not in use",
     "call_state": "", "peer": "", "channel": ""},
    {"ext": "12", "label": "Office", "registered": True, "device_state": "In use",
     "call_state": "Talking", "peer": "Kitchen", "channel": "PJSIP/12-0001"},
    {"ext": "13", "label": "Garage", "registered": False, "device_state": "Unavailable",
     "call_state": "", "peer": "", "channel": ""},
]


def test_render() -> None:
    board = _board(ROOMS, calls=[{"detail": "Kitchen ↔ Office", "state": "Talking",
                                  "duration": "00:02:14", "kind": "internal"}])
    sess = {"sel": 0, "mode": "normal", "w": 80}
    text = "\n".join(console.render(board.get(), sess, 0.0))
    check("render: registered room", "Registered" in text and "Kitchen" in text)
    check("render: offline room", "Offline" in text and "Garage" in text)
    check("render: on-call peer shown", "↔ Kitchen" in text)
    check("render: active calls section", "ACTIVE CALLS" in text and "Kitchen ↔ Office" in text)
    check("render: duration trimmed of leading 00:", "02:14" in text)
    check("render: command bar", "ring" in text and "connect" in text and "hang up" in text)


def test_render_connect_mode() -> None:
    board = _board(ROOMS)
    sess = {"sel": 1, "mode": "connect", "connect_from_label": "Kitchen", "w": 80}
    text = "\n".join(console.render(board.get(), sess, 0.0))
    check("render: connect prompt", "CONNECT Kitchen" in text and "Enter" in text)


def test_render_ami_down() -> None:
    board = _board(ROOMS, ami_ok=False)
    text = "\n".join(console.render(board.get(), {"sel": 0, "mode": "normal", "w": 80}, 0.0))
    check("render: AMI-down banner", "unreachable" in text.lower())


def test_navigation() -> None:
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "normal"}
    console.apply_key(sess, "down", board, lambda m: None)
    check("nav: down -> 1", sess["sel"] == 1)
    console.apply_key(sess, "j", board, lambda m: None)
    check("nav: j -> 2", sess["sel"] == 2)
    console.apply_key(sess, "down", board, lambda m: None)
    check("nav: wraps to 0", sess["sel"] == 0)
    console.apply_key(sess, "up", board, lambda m: None)
    check("nav: up wraps to 2", sess["sel"] == 2)


def test_connect_mode_transitions() -> None:
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "normal"}
    console.apply_key(sess, "c", board, lambda m: None)
    check("connect: enters connect mode", sess["mode"] == "connect" and sess.get("connect_from") == "11")
    console.apply_key(sess, "esc", board, lambda m: None)
    check("connect: esc cancels", sess["mode"] == "normal" and "connect_from" not in sess)


def test_guarded_actions() -> None:
    board = _board(ROOMS)
    # Ring an OFFLINE room (sel=2 Garage) → guarded, no AMI, flash message.
    sess = {"sel": 2, "mode": "normal"}
    console.apply_key(sess, "r", board, lambda m: None)
    check("ring: offline room guarded", "offline" in sess.get("msg", "").lower())
    # Hang up a room with no active channel (sel=0 Kitchen) → guarded.
    sess = {"sel": 0, "mode": "normal"}
    console.apply_key(sess, "h", board, lambda m: None)
    check("hangup: no-call guarded", "no active call" in sess.get("msg", "").lower())


def main() -> None:
    test_parse_input()
    test_render()
    test_render_connect_mode()
    test_render_ami_down()
    test_navigation()
    test_connect_mode_transitions()
    test_guarded_actions()
    print()
    if _failures:
        print(f"{_failures} FAILURE(S)")
        raise SystemExit(1)
    print("all console tests passed")


if __name__ == "__main__":
    main()
