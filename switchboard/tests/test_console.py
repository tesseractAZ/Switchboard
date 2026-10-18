"""Behavioral tests for the telnet operator console (console/console.py).

Run with plain Python (no deps):

    python3 switchboard/tests/test_console.py

Covers the pure pieces: telnet/ANSI input parsing, board rendering, and the
action key handling (navigation + mode transitions + guarded actions). The AMI
side effects (ring/connect/hangup) are exercised only on their guard paths,
which never open a socket.
"""
import contextlib
import os
import re
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

# Point the wake-up + MWI stores at throwaway paths before the console imports them.
os.environ.setdefault("SWITCHBOARD_WAKEUPS", os.path.join(tempfile.mkdtemp(), "wakeups.json"))
os.environ.setdefault("SWITCHBOARD_MWI", os.path.join(tempfile.mkdtemp(), "mwi.json"))

CONSOLE_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share" / "switchboard" / "console" / "console.py"
console = SourceFileLoader("switchboard_console", str(CONSOLE_PATH)).load_module()

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


# ★ POLLED is the default, so `ts` is non-zero. Board() starts at ts=0.0 meaning
# "never polled", and since v0.100.5 render() tells that apart from "polled and
# AMI is down" — a fixture carrying rooms AND ts=0.0 describes a state the real
# system cannot reach. _unpolled() below is the one that wants the zero.
def _board(rooms, calls=None, ami_ok=True, ts=1_789_000_000.0):
    b = console.Board()
    b.set({"ami_ok": ami_ok, "rooms": rooms, "calls": calls or [], "ts": ts})
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
    # A lone trailing ESC is held in rest (can't yet tell it from an escape seq) —
    # serve_session flushes it as the Esc key on the recv-timeout tick so a single
    # Escape cancels a mode. A full arrow sequence is NOT mistaken for a bare Esc.
    ev, rest = console.parse_input(b"\x1b")
    check("parse: lone ESC held in rest for the escape-timeout flush",
          ev == [] and rest == b"\x1b")
    ev, rest = console.parse_input(b"\x1b[B")
    check("parse: full arrow not seen as bare ESC", ev == [("key", "down")] and rest == b"")


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
    check("render: registered room", "READY" in text and "Kitchen" in text)
    check("render: offline room", "OFFLINE" in text and "Garage" in text)
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
    """Polled, and the answer was no. This is the real alarm."""
    board = _board(ROOMS, ami_ok=False)
    text = "\n".join(console.render(board.get(), {"sel": 0, "mode": "normal", "w": 80}, 0.0))
    check("render: AMI-down banner", "unreachable" in text.lower())
    check("render: and it is not the connecting notice",
          "connecting to the pbx" not in text.lower())


def test_render_before_the_first_poll_says_connecting_not_unreachable() -> None:
    """★ NOT ASKED YET IS NOT THE SAME AS ASKED AND THE ANSWER WAS NO.

    poller_loop parks on ClientGate with no AMI traffic while nobody is watching,
    so every new session renders at least one frame before the first poll
    returns. That frame reported "Asterisk Manager unreachable" on a healthy
    system — seen live on 2026-09-11, where a 4-second capture showed 0/0 online
    and the warning while the heartbeat 90 s either side read 9/9 reachable.

    Board() starts at ts=0.0 and build_board stamps time.time() on every poll,
    INCLUDING a failed one, so `ts` separates the two cleanly.
    """
    b = console.Board()                      # exactly as a fresh session sees it
    text = "\n".join(console.render(b.get(), {"sel": 0, "mode": "normal", "w": 80}, 0.0))
    check("render: a never-polled board says it is connecting",
          "connecting to the pbx" in text.lower())
    check("render: and does NOT claim the PBX is unreachable",
          "unreachable" not in text.lower())
    check("render: the key bar is still there while connecting",
          "Q" in text and "quit" in text)


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


# A room actively on a call to an outside party — peer_channel is the far leg the
# operator would blind-transfer elsewhere.
ROOMS_ONCALL = [
    {"ext": "11", "label": "Kitchen", "registered": True, "device_state": "Not in use",
     "call_state": "", "peer": "", "channel": "", "peer_channel": ""},
    {"ext": "19", "label": "Cordless", "registered": True, "device_state": "In use",
     "call_state": "Talking", "peer": "Outside", "channel": "PJSIP/19-0001",
     "peer_channel": "PJSIP/trunk-0002"},
]


def test_render_transfer_mode() -> None:
    board = _board(ROOMS_ONCALL)
    sess = {"sel": 0, "mode": "transfer", "transfer_from_label": "Cordless", "w": 80}
    text = "\n".join(console.render(board.get(), sess, 0.0))
    check("render: transfer prompt", "TRANSFER Cordless" in text and "Enter" in text)


def test_transfer_mode_transitions() -> None:
    board = _board(ROOMS_ONCALL)
    # 't' on a room with NO call must not enter transfer mode (nothing to hand off).
    sess = {"sel": 0, "mode": "normal"}
    console.apply_key(sess, "t", board, lambda m: None)
    check("transfer: idle room does not arm", sess["mode"] == "normal" and "transfer_peer" not in sess)
    # 't' on the on-call room arms transfer with the FAR leg as the channel to move.
    sess = {"sel": 1, "mode": "normal"}
    console.apply_key(sess, "t", board, lambda m: None)
    check("transfer: on-call room arms with far leg",
          sess["mode"] == "transfer" and sess.get("transfer_peer") == "PJSIP/trunk-0002"
          and sess.get("transfer_from") == "19")
    console.apply_key(sess, "esc", board, lambda m: None)
    check("transfer: esc cancels", sess["mode"] == "normal" and "transfer_peer" not in sess)


def test_transfer_offline_target_refused() -> None:
    # ext 13 offline, ext 19 on a call. Arm transfer on 19, then pick the offline
    # room: it must be refused (a redirect there would just drop the caller), the
    # same way ring/page gate on registration — no AMI call.
    rooms = [
        {"ext": "13", "label": "Garage", "registered": False, "device_state": "Unavailable",
         "call_state": "", "peer": "", "channel": "", "peer_channel": ""},
        {"ext": "19", "label": "Cordless", "registered": True, "device_state": "In use",
         "call_state": "Talking", "peer": "Outside", "channel": "PJSIP/19-0001",
         "peer_channel": "PJSIP/trunk-0002"},
    ]
    board = _board(rooms)
    sess = {"sel": 1, "mode": "normal"}          # Cordless (on call)
    console.apply_key(sess, "t", board, lambda m: None)
    check("transfer: armed on on-call room", sess["mode"] == "transfer")
    sess["sel"] = 0                               # Garage (offline)
    console.apply_key(sess, "enter", board, lambda m: None)
    check("transfer: offline target refused",
          "offline" in sess.get("msg", "").lower() and sess["mode"] == "normal"
          and "transfer_peer" not in sess)


def test_fmt12() -> None:
    check("fmt12: 07:05 -> 7:05 AM", console.fmt12("07:05") == "7:05 AM")
    check("fmt12: 19:30 -> 7:30 PM", console.fmt12("19:30") == "7:30 PM")
    check("fmt12: 00:00 -> 12:00 AM", console.fmt12("00:00") == "12:00 AM")
    check("fmt12: 12:00 -> 12:00 PM", console.fmt12("12:00") == "12:00 PM")


def test_render_wakeups() -> None:
    b = console.Board()
    b.set({"ami_ok": True, "rooms": ROOMS, "calls": [],
           "wakeups": [{"ext": "11", "label": "Kitchen", "hhmm": "07:00", "target_epoch": 0}], "ts": 0.0})
    text = "\n".join(console.render(b.get(), {"sel": 0, "mode": "normal", "w": 80}, 0.0))
    check("render: wake-ups section", "WAKE-UPS" in text and "Kitchen" in text and "7:00 AM" in text)
    check("render: set/cancel wake-up + help in command bar",
          "set wake-up" in text and "cancel wake-up" in text and "help" in text)


def test_render_signals() -> None:
    # Full-screen board (v0.41.0) surfaces per-room idle RTT, the trunk SIP
    # registration, and resident-STT health.
    rooms = [{"ext": "11", "label": "Family Room", "registered": True,
              "device_state": "Not in use", "rtt": "2800", "contact_status": "Reachable"},
             {"ext": "20", "label": "Computer", "registered": False, "device_state": "Unavailable"}]
    b = console.Board()
    b.set({"ami_ok": True, "rooms": rooms, "calls": [], "wakeups": [],
           "trunk_reg": "Registered", "stt": "up", "ts": 0.0})
    text = "\n".join(console.render(b.get(), {"sel": 0, "mode": "normal", "w": 100, "h": 30}, 0.0))
    check("signals: per-room RTT rendered", "2.8 ms" in text)
    check("signals: an offline room shows no RTT", text.count("ms") == 1)  # only the registered one
    check("signals: trunk registration rendered", "trunk" in text and "Registered" in text)
    check("signals: STT health rendered", "STT" in text and "resident" in text)
    # A down trunk is shown; STT 'down' reads as the slow CLI fallback.
    b.set({"ami_ok": True, "rooms": rooms, "calls": [], "wakeups": [],
           "trunk_reg": "Unregistered", "stt": "down", "ts": 0.0})
    text = "\n".join(console.render(b.get(), {"sel": 0, "mode": "normal", "w": 100, "h": 30}, 0.0))
    check("signals: unregistered trunk shown", "Unregistered" in text)
    check("signals: STT down -> CLI fallback", "CLI fallback" in text)
    # 'disabled' STT is hidden; a board WITHOUT the new keys (old poll) doesn't crash.
    b.set({"ami_ok": True, "rooms": rooms, "calls": [], "wakeups": [],
           "trunk_reg": "", "stt": "disabled", "ts": 0.0})
    text = "\n".join(console.render(b.get(), {"sel": 0, "mode": "normal", "w": 100, "h": 30}, 0.0))
    check("signals: disabled STT hidden", "CLI fallback" not in text and "resident" not in text)
    check("signals: no trunk line when trunk_reg empty", "trunk" not in text)
    # µs -> ms helper.
    check("rtt: 2800us -> 2.8ms", console._rtt_ms("2800") == 2.8)
    check("rtt: ''/0/neg/NaN -> None",
          console._rtt_ms("") is None and console._rtt_ms("0") is None
          and console._rtt_ms("-5") is None and console._rtt_ms("nan") is None)


def test_cancel_wakeup_key() -> None:
    console.wakeup_store.set_wakeup("11", "07:00")
    board = console.Board()
    board.set({"ami_ok": True, "rooms": ROOMS, "calls": [], "wakeups": [], "ts": 1_789_000_000.0})
    sess = {"sel": 0, "mode": "normal"}  # sel 0 -> ext 11 (Kitchen)
    console.apply_key(sess, "x", board, lambda m: None)
    check("x: cancels the selected room's wake-up",
          "cancelled wake-up" in sess.get("msg", "").lower() and console.wakeup_store.get("11") is None)
    console.apply_key(sess, "x", board, lambda m: None)
    check("x: no wake-up -> message", "no wake-up" in sess.get("msg", "").lower())


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


def test_parse_input_backspace() -> None:
    # Both DEL (0x7f, what xterm.js sends) and BS (0x08, a raw telnet client)
    # now surface as a backspace key (they used to be dropped).
    ev, _ = console.parse_input(bytes([0x7f]))
    check("parse: DEL 0x7f -> backspace", ev == [("key", "backspace")])
    ev, _ = console.parse_input(bytes([0x08]))
    check("parse: BS 0x08 -> backspace", ev == [("key", "backspace")])
    ev, _ = console.parse_input(b"7" + bytes([0x7f]))
    check("parse: char then backspace", ev == [("key", "7"), ("key", "backspace")])


def test_is_quit() -> None:
    check("quit: ctrl-c always quits", console.is_quit("ctrl-c", "normal") and console.is_quit("ctrl-c", "wakeup"))
    check("quit: q/Q quit from the board", console.is_quit("q", "normal") and console.is_quit("Q", "normal"))
    check("quit: q/Q still quit from connect + help", console.is_quit("q", "connect") and console.is_quit("Q", "help"))
    check("quit: q does NOT quit while typing a time", not console.is_quit("q", "wakeup") and not console.is_quit("Q", "wakeup"))
    check("quit: other keys never quit", not console.is_quit("r", "normal") and not console.is_quit("enter", "normal"))


def test_wakeup_when() -> None:
    import datetime
    now = datetime.datetime(2026, 6, 24, 9, 0, 0).timestamp()
    later_today = datetime.datetime(2026, 6, 24, 17, 30, 0).timestamp()
    next_day = datetime.datetime(2026, 6, 25, 6, 0, 0).timestamp()
    check("when: later today -> today", console.wakeup_when(later_today, now) == "today")
    check("when: next day -> tomorrow", console.wakeup_when(next_day, now) == "tomorrow")


def test_set_wakeup_mode() -> None:
    console.wakeup_store.cancel("11")
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "normal"}  # sel 0 -> Kitchen (ext 11)
    console.apply_key(sess, "W", board, lambda m: None)
    check("wakeup: W enters mode + captures target",
          sess["mode"] == "wakeup" and sess.get("wakeup_ext") == "11"
          and sess.get("wakeup_label") == "Kitchen" and sess.get("wakeup_buf") == "")
    for ch in "730":
        console.apply_key(sess, ch, board, lambda m: None)
    check("wakeup: printable keys typed into buffer", sess.get("wakeup_buf") == "730")
    # A nav key (j) is typed as text, not navigation, while in wakeup mode.
    sel_before = sess["sel"]
    console.apply_key(sess, "j", board, lambda m: None)
    check("wakeup: j typed literally (no nav)", sess.get("wakeup_buf") == "730j" and sess["sel"] == sel_before)
    console.apply_key(sess, "backspace", board, lambda m: None)
    check("wakeup: backspace deletes last char", sess.get("wakeup_buf") == "730")
    console.apply_key(sess, "enter", board, lambda m: None)
    e = console.wakeup_store.get("11")
    msg = sess.get("msg", "")
    check("wakeup: Enter commits to the store + reads back, mode back to normal",
          sess["mode"] == "normal" and e is not None and e.get("hhmm") == "07:30"
          and "7:30 AM" in msg and ("today" in msg or "tomorrow" in msg) and "wakeup_buf" not in sess)
    console.wakeup_store.cancel("11")


def test_set_wakeup_invalid_and_empty() -> None:
    console.wakeup_store.cancel("11")
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "normal"}
    console.apply_key(sess, "W", board, lambda m: None)
    console.apply_key(sess, "enter", board, lambda m: None)  # empty buffer
    check("wakeup: empty Enter stays in mode + hints, nothing written",
          sess["mode"] == "wakeup" and "didn't catch" in sess.get("msg", "").lower()
          and console.wakeup_store.get("11") is None)
    for ch in "zzz":
        console.apply_key(sess, ch, board, lambda m: None)
    console.apply_key(sess, "enter", board, lambda m: None)  # unparseable
    check("wakeup: invalid stays in mode, buffer intact, nothing written",
          sess["mode"] == "wakeup" and sess.get("wakeup_buf") == "zzz"
          and console.wakeup_store.get("11") is None)
    console.apply_key(sess, "esc", board, lambda m: None)
    check("wakeup: Esc cancels + clears state",
          sess["mode"] == "normal" and "wakeup_buf" not in sess
          and "cancelled" in sess.get("msg", "").lower())


def test_set_wakeup_buffer_capped() -> None:
    console.wakeup_store.cancel("11")
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "normal"}
    console.apply_key(sess, "W", board, lambda m: None)
    for _ in range(50):
        console.apply_key(sess, "1", board, lambda m: None)
    check("wakeup: buffer capped at 32 chars", len(sess.get("wakeup_buf", "")) == 32)


def test_set_wakeup_edit_prefill() -> None:
    console.wakeup_store.set_wakeup("11", "06:15")
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "normal"}
    console.apply_key(sess, "w", board, lambda m: None)
    check("wakeup: W on an existing wake-up pre-fills its time", sess.get("wakeup_buf") == "06:15")
    console.wakeup_store.cancel("11")


def test_render_wakeup_mode() -> None:
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "wakeup", "wakeup_label": "Kitchen", "wakeup_buf": "730", "w": 80}
    text = "\n".join(console.render(board.get(), sess, 0.0))
    check("render: wakeup prompt shows room + buffer", "SET WAKE-UP Kitchen" in text and "730" in text)
    check("render: wakeup live preview of parsed time", "7:30 AM" in text)


def test_help_mode() -> None:
    board = _board(ROOMS)
    text = "\n".join(console.render(board.get(), {"sel": 0, "mode": "help", "w": 80}, 0.0))
    check("render: help overlay content", "HELP" in text and "Set a wake-up" in text and "Connect" in text)
    sess = {"sel": 0, "mode": "normal"}
    console.apply_key(sess, "?", board, lambda m: None)
    check("help: ? opens help", sess["mode"] == "help")
    console.apply_key(sess, "x", board, lambda m: None)  # any key dismisses
    check("help: any key returns to the board", sess["mode"] == "normal")


def test_vis_width() -> None:
    check("vis: plain ascii width", console.vis_width("Kitchen") == 7)
    check("vis: ANSI codes don't count", console.vis_width(console.color(console.GREEN, "Registered")) == 10)
    check("vis: wide emoji counts 2", console.vis_width("🔌X") == 3)
    check("vis: ambiguous box/arrow glyphs count 1", console.vis_width("▸→●█") == 4)


def test_center() -> None:
    check("center: horizontal indent", console.center(["abcd"], 10, 1) == ["   abcd"])
    check("center: vertical padding", console.center(["a", "b"], 1, 6) == ["", "", "a", "b"])
    check("center: no padding when content fills", console.center(["abcd", "efgh"], 4, 2) == ["abcd", "efgh"])
    check("center: empty input unchanged", console.center([], 80, 24) == [])
    # On a wide+tall terminal the board now FILLS the width (full-screen, v0.41.0)
    # and is still vertically centered (blank top rows), instead of floating in a
    # narrow centered column.
    board = _board(ROOMS)
    lines = console.render(board.get(), {"sel": 0, "mode": "normal", "w": 120, "h": 40}, 0.0)
    check("full-screen: board fills the width on a wide screen",
          lines[0] == "" and max(console.vis_width(ln) for ln in lines) >= 116)


def test_truncate_and_clamp() -> None:
    # A line wider than the terminal is truncated to its visible width (no wrap).
    check("truncate: over-wide line cut to width",
          console.vis_width(console._truncate_visible("abcdefghij", 5)) == 5)
    check("truncate: short line untouched", console._truncate_visible("abc", 10) == "abc")
    check("truncate: zero width -> empty", console._truncate_visible("abc", 0) == "")
    # ANSI codes are preserved and a reset is re-appended when color was open.
    colored = console.color(console.GREEN, "Registered")
    cut = console._truncate_visible(colored, 4)
    check("truncate: colored line stays <= width", console.vis_width(cut) <= 4)
    check("truncate: colored line re-resets", cut.endswith(console.RESET))
    check("truncate: wide emoji not split mid-cell", console.vis_width(console._truncate_visible("🔌abc", 2)) <= 2)
    # center() never returns a line wider than w, nor more than h rows.
    board = _board(ROOMS)
    for (w, h) in [(60, 20), (40, 12), (80, 24)]:
        lines = console.render(board.get(), {"sel": 0, "mode": "normal", "w": w, "h": h}, 0.0)
        check(f"clamp: no line wider than {w}", all(console.vis_width(ln) <= w for ln in lines))
        check(f"clamp: no more than {h} rows", len(lines) <= h)
    # Every mode stays within a narrow terminal.
    for mode in ("help", "connect", "transfer", "pageconfirm"):
        lines = console.render(board.get(), {"sel": 0, "mode": mode, "w": 60, "h": 20, "buf": "16"}, 0.0)
        check(f"clamp: mode {mode} fits 60x20",
              all(console.vis_width(ln) <= 60 for ln in lines) and len(lines) <= 20)


def test_page_confirm_accept() -> None:
    # P opens the confirm gate (does not page yet), Y fires page_all over the
    # REGISTERED rooms only.
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "normal"}
    console.apply_key(sess, "P", board, lambda m: None)
    check("page: P enters confirm mode", sess["mode"] == "pageconfirm")
    captured = {}
    orig = console.ami.page_all
    console.ami.page_all = lambda exts: captured.setdefault("exts", list(exts)) or True
    try:
        console.apply_key(sess, "y", board, lambda m: None)
    finally:
        console.ami.page_all = orig
    check("page: Y calls page_all with registered exts only",
          captured.get("exts") == ["11", "12"])  # 13 Garage is offline
    check("page: Y returns to normal + flashes paging",
          sess["mode"] == "normal" and "paging" in sess.get("msg", "").lower())


def test_page_confirm_cancel() -> None:
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "normal"}
    console.apply_key(sess, "p", board, lambda m: None)
    called = {"n": 0}
    orig = console.ami.page_all
    console.ami.page_all = lambda exts: called.__setitem__("n", called["n"] + 1) or True
    try:
        console.apply_key(sess, "n", board, lambda m: None)
    finally:
        console.ami.page_all = orig
    check("page: N cancels without paging",
          sess["mode"] == "normal" and called["n"] == 0 and "cancel" in sess.get("msg", "").lower())
    # Esc also cancels.
    console.apply_key(sess, "p", board, lambda m: None)
    console.apply_key(sess, "esc", board, lambda m: None)
    check("page: Esc cancels too", sess["mode"] == "normal")


def test_mwi_toggle() -> None:
    console.mwi_store.set_flag("11", False)  # ensure clear
    captured = []
    orig = console.ami.set_mwi
    console.ami.set_mwi = lambda ext, on: captured.append((ext, on)) or True
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "normal"}  # sel 0 -> Kitchen (ext 11)
    try:
        console.apply_key(sess, "M", board, lambda m: None)
        set_ok = (console.mwi_store.is_set("11") and captured == [("11", True)]
                  and "message set" in sess.get("msg", "").lower())
        check("mwi: M sets the store + ami + flashes 'message set'", set_ok)
        console.apply_key(sess, "M", board, lambda m: None)
        clear_ok = (not console.mwi_store.is_set("11") and captured == [("11", True), ("11", False)]
                    and "cleared message" in sess.get("msg", "").lower())
        check("mwi: M again clears the store + ami + flashes 'cleared message'", clear_ok)
    finally:
        console.ami.set_mwi = orig
        console.mwi_store.set_flag("11", False)


def test_mwi_set_failure() -> None:
    # A SET that Asterisk REFUSES (ami.set_mwi → False) must NOT set the badge,
    # and must flash a PBX-failure message. Optimistic-CLEAR, honest-SET.
    console.mwi_store.set_flag("11", False)  # ensure clear
    captured = []
    orig = console.ami.set_mwi
    console.ami.set_mwi = lambda ext, on: captured.append((ext, on)) or False
    board = _board(ROOMS)
    sess = {"sel": 0, "mode": "normal"}  # sel 0 -> Kitchen (ext 11)
    try:
        console.apply_key(sess, "M", board, lambda m: None)
        fail_ok = (not console.mwi_store.is_set("11")  # badge stayed OFF
                   and captured == [("11", True)]      # the SET was attempted
                   and "failed" in sess.get("msg", "").lower())
        check("mwi: a refused SET leaves the badge off + flashes failure", fail_ok)
        # Now make set_mwi succeed → the badge DOES set.
        captured.clear()
        console.ami.set_mwi = lambda ext, on: captured.append((ext, on)) or True
        console.apply_key(sess, "M", board, lambda m: None)
        set_ok = (console.mwi_store.is_set("11") and captured == [("11", True)]
                  and "message set" in sess.get("msg", "").lower())
        check("mwi: an accepted SET sets the badge + flashes 'message set'", set_ok)
    finally:
        console.ami.set_mwi = orig
        console.mwi_store.set_flag("11", False)


def test_mwi_badge_render() -> None:
    rooms = [dict(ROOMS[0], mwi=True), dict(ROOMS[1], mwi=False)]
    board = _board(rooms)
    text = "\n".join(console.render(board.get(), {"sel": 0, "mode": "normal", "w": 80}, 0.0))
    check("render: ✉ badge on a room with MWI set", "✉" in text)
    # Only the flagged room's row carries it.
    row = [ln for ln in console.render(board.get(), {"sel": 0, "mode": "normal", "w": 80}, 0.0)
           if "Office" in ln][0]
    check("render: ✉ absent from a room without MWI", "✉" not in row)


_FAKE_LIGHTS = [
    {"entity_id": "light.kitchen", "name": "Kitchen Main", "state": "on", "area": "Kitchen"},
    {"entity_id": "light.office", "name": "Office Lamp", "state": "off", "area": "Office"},
]


def test_lights_entry_and_unavailable() -> None:
    board = _board(ROOMS)
    # HA returns nothing → flash unavailable, stay normal.
    orig = console.ha_client.get_lights
    console.ha_client.get_lights = lambda: []
    try:
        sess = {"sel": 0, "mode": "normal"}
        console.apply_key(sess, "L", board, lambda m: None)
        check("lights: empty list → unavailable, stays normal",
              sess["mode"] == "normal" and "unavailable" in sess.get("msg", "").lower())
        # HA returns lights → enter lights mode with the list stashed in sess.
        console.ha_client.get_lights = lambda: [dict(li) for li in _FAKE_LIGHTS]
        console.apply_key(sess, "l", board, lambda m: None)
        check("lights: L enters lights mode + stashes the fetched list",
              sess["mode"] == "lights" and len(sess.get("lights", [])) == 2 and sess.get("lsel") == 0)
        text = "\n".join(_plain(console.render(board.get(), sess, 0.0)))
        check("render: lights view shows names + on/off",
              "LIGHTS" in text and "Kitchen Main" in text and "◉ on" in text and "○ off" in text)
    finally:
        console.ha_client.get_lights = orig


def test_lights_nav_toggle_exit() -> None:
    board = _board(ROOMS)
    sess = {"mode": "lights", "lsel": 0, "lights": [dict(li) for li in _FAKE_LIGHTS], "w": 80}
    console.apply_key(sess, "j", board, lambda m: None)
    check("lights: j moves cursor down (clamped)", sess["lsel"] == 1)
    console.apply_key(sess, "j", board, lambda m: None)
    check("lights: j clamps at the last light", sess["lsel"] == 1)
    console.apply_key(sess, "k", board, lambda m: None)
    check("lights: k moves cursor up", sess["lsel"] == 0)
    # Toggle the first (on → off) optimistically when set_light accepts.
    calls = []
    orig = console.ha_client.set_light
    console.ha_client.set_light = lambda eid, on: calls.append((eid, on)) or True
    try:
        console.apply_key(sess, "enter", board, lambda m: None)
        check("lights: Enter calls set_light(turn off) on the on-light",
              calls == [("light.kitchen", False)])
        check("lights: optimistic flip on → off + flash",
              sess["lights"][0]["state"] == "off" and "off" in sess.get("msg", "").lower())
    finally:
        console.ha_client.set_light = orig
    # Esc exits + clears the lights state.
    console.apply_key(sess, "esc", board, lambda m: None)
    check("lights: Esc exits to normal + clears state",
          sess["mode"] == "normal" and "lights" not in sess and "lsel" not in sess)


def test_lights_toggle_failure_no_flip() -> None:
    board = _board(ROOMS)
    sess = {"mode": "lights", "lsel": 0, "lights": [dict(li) for li in _FAKE_LIGHTS], "w": 80}
    orig = console.ha_client.set_light
    console.ha_client.set_light = lambda eid, on: False  # HA refused
    try:
        console.apply_key(sess, " ", board, lambda m: None)  # space toggles too
    finally:
        console.ha_client.set_light = orig
    check("lights: a refused toggle does NOT flip + flashes failure",
          sess["lights"][0]["state"] == "on" and "failed" in sess.get("msg", "").lower())


def test_help_and_bar_have_new_keys() -> None:
    board = _board(ROOMS)
    help_text = "\n".join(console.render(board.get(), {"sel": 0, "mode": "help", "w": 80}, 0.0))
    check("help: documents M / P / L + lights-mode keys",
          "message-waiting" in help_text and "Page all" in help_text
          and "Lights" in help_text and "toggle" in help_text)
    bar_text = "\n".join(console.render(board.get(), {"sel": 0, "mode": "normal", "w": 80}, 0.0))
    check("bar: command bar shows message / page all / lights",
          "message" in bar_text and "page all" in bar_text and "lights" in bar_text)


def test_command_bar_fits_80() -> None:
    board = _board(ROOMS)
    for mode in ("normal", "pageconfirm"):
        lines = console.render(board.get(), {"sel": 0, "mode": mode, "w": 80, "h": 24}, 0.0)
        widest = max((console.vis_width(ln) for ln in lines), default=0)
        check(f"bar: {mode} render fits 80 cols (widest={widest})", widest <= 80)
    lsess = {"mode": "lights", "lsel": 0, "w": 80, "h": 24,
             "lights": [dict(li) for li in _FAKE_LIGHTS]}
    lines = console.render(board.get(), lsess, 0.0)
    widest = max((console.vis_width(ln) for ln in lines), default=0)
    check(f"bar: lights view fits 80 cols (widest={widest})", widest <= 80)


def test_client_gate() -> None:
    import threading
    import time
    gate = console.ClientGate()

    result = {}

    def waiter(bucket):
        bucket["active"] = gate.wait_active()

    t = threading.Thread(target=waiter, args=(result,), daemon=True)
    t.start()
    time.sleep(0.05)
    check("gate: waiter blocks while no client is connected",
          t.is_alive() and "active" not in result)
    gate.enter()
    t.join(timeout=1.0)
    check("gate: enter() releases the waiter (active)", result.get("active") is True)

    # Reference counting: second client keeps it active; one leave still active.
    gate.enter()  # n=2
    check("gate: active returns immediately while a client is connected",
          gate.wait_active() is True)
    gate.leave()  # n=1
    check("gate: still active after one of two clients leaves",
          gate.wait_active() is True)
    gate.leave()  # n=0 -> parks again

    result2 = {}
    t2 = threading.Thread(target=waiter, args=(result2,), daemon=True)
    t2.start()
    time.sleep(0.05)
    check("gate: parks again after the last client leaves", t2.is_alive())
    gate.wake()  # shutdown
    t2.join(timeout=1.0)
    check("gate: wake() releases a parked waiter as inactive (shutdown)",
          result2.get("active") is False)


def test_poller_gated_on_clients() -> None:
    import threading
    import time
    calls = {"n": 0}

    def fake_build(cfg, opts=None):
        calls["n"] += 1
        return {"ami_ok": True, "rooms": [], "calls": [], "ts": 1_789_000_000.0}

    orig_build, orig_rooms, orig_opts, orig_poll = (
        console.build_board, console.load_rooms_cfg, console.load_options, console.POLL_SECONDS)
    console.build_board = fake_build
    console.load_rooms_cfg = lambda opts=None: []
    console.load_options = lambda: {}
    console.POLL_SECONDS = 0.02
    try:
        board = console.Board()
        stop = threading.Event()
        gate = console.ClientGate()
        t = threading.Thread(target=console.poller_loop,
                             args=(board, stop, gate, lambda m: None), daemon=True)
        t.start()
        time.sleep(0.1)
        check("poller: ZERO AMI builds while no client is connected — the churn fix",
              calls["n"] == 0)
        gate.enter()
        time.sleep(0.1)
        check("poller: builds the board once a client connects", calls["n"] >= 1)
        gate.leave()
        n_at_leave = calls["n"]
        time.sleep(0.1)
        check("poller: stops building after the last client leaves (<=1 in-flight)",
              calls["n"] <= n_at_leave + 1)
        stop.set()
        gate.wake()
        t.join(timeout=1.0)
        check("poller: thread exits cleanly on stop+wake", not t.is_alive())
    finally:
        console.build_board = orig_build
        console.load_rooms_cfg = orig_rooms
        console.load_options = orig_opts
        console.POLL_SECONDS = orig_poll


# --------------------------------------------------------------------------- #
# The lights list scrolls.
# --------------------------------------------------------------------------- #
def _many_lights(n=34):
    areas = ["Bathroom", "Dining Room", "Family Room", "Garage", "Guest Bedroom",
             "Guest Hallway", "Gym", "Hallway", "Kitchen", "Living Room",
             "Master Bedroom", "Office"]
    out = []
    for a in areas:
        for i in range(1, 4):
            if len(out) < n:
                out.append({"name": f"{a} Light {i}", "area": a, "state": "off"})
    return out


def _plain(lines):
    import re
    return [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines]


def test_every_light_is_reachable_however_many_there_are():
    """★ Reported from live use: "no way to scroll and truncates at bottom".

    Before v0.94.4 `_lights_lines` rendered every light from the first one, and
    `center()` then clamped the frame to the terminal height. With 34 lights
    across twelve areas — 46 rows of content — everything past the terminal's
    last row was cut off at ANY size. `lsel` kept incrementing past the fold, so
    the rows below were not merely off-screen, they were unreachable: the cursor
    moved somewhere you could not see and no key brought it back.

    The invariant is the one that matters to a person using it: whatever is
    selected must be on screen, and the frame must fit.
    """
    lights = _many_lights(34)
    for height in (12, 24, 30, 60):
        sess = {"lights": lights, "lsel": 0, "mode": "lights", "w": 100, "h": height}
        for sel in range(len(lights)):
            sess["lsel"] = sel
            out = console._lights_lines(sess, 100, height)
            txt = _plain(out)
            assert len(out) <= height, (
                f"h={height} lsel={sel}: frame is {len(out)} rows, taller than the "
                f"terminal — center() will clip it and the bottom is lost")
            assert any("▸" in ln for ln in txt), (
                f"h={height} lsel={sel}: the selected light is not on screen; the "
                f"cursor moved somewhere the operator cannot see")


def test_the_scroll_position_persists_between_redraws():
    """The console redraws on a timer. Recomputing the window from scratch each
    frame would make a long list twitch under the cursor."""
    lights = _many_lights(34)
    sess = {"lights": lights, "lsel": 20, "mode": "lights", "w": 100, "h": 24}
    console._lights_lines(sess, 100, 24)
    first = sess.get("ltop")
    assert first is not None and first > 0, "no scroll offset was recorded"
    console._lights_lines(sess, 100, 24)
    assert sess["ltop"] == first, "the window moved without the selection moving"


def test_a_scrolled_list_says_what_is_hidden_and_which_area_it_is_in():
    """A list that scrolls with no indication looks exactly like one that is
    truncated — which is the bug this replaced. And a name with no area heading
    tells you a light is on but not where."""
    lights = _many_lights(34)
    sess = {"lights": lights, "lsel": 33, "mode": "lights", "w": 100, "h": 24}
    txt = _plain(console._lights_lines(sess, 100, 24))
    joined = "\n".join(txt)
    assert "more above" in joined, "nothing tells the operator there is more above"
    # The top visible light must be attributable to an area: either the window
    # happens to open ON that area's heading, or the heading is repeated. Only
    # asserting "(cont.)" would fail whenever the window lands on a boundary,
    # which is a property of the fixture rather than of the code.
    body = [ln for ln in txt if "○ off" in ln or "◉ on" in ln]
    first_light_at = txt.index(body[0])
    assert any(("(cont.)" in ln) or (ln.strip() and "○" not in ln and "◉" not in ln
                                     and "LIGHTS" not in ln and "─" not in ln)
               for ln in txt[:first_light_at]), (
        "the first visible light has no area heading above it — the operator can "
        "see that a light is on but not where it is")

    top = _plain(console._lights_lines({"lights": lights, "lsel": 0, "mode": "lights",
                                        "w": 100, "h": 24}, 100, 24))
    assert "below" in "\n".join(top), "nothing indicates there is more below"


def test_a_short_list_is_not_windowed_at_all():
    """Six lights on a 24-row terminal must render plainly — no scroll note, no
    continuation heading, nothing to explain."""
    sess = {"lights": _many_lights(6), "lsel": 0, "mode": "lights", "w": 100, "h": 24}
    txt = _plain(console._lights_lines(sess, 100, 24))
    joined = "\n".join(txt)
    assert "above" not in joined and "below" not in joined
    assert "(cont.)" not in joined
    assert sum(1 for ln in txt if "○ off" in ln) == 6, "not every light rendered"


def test_a_console_wakeup_change_is_recorded_and_is_not_a_snooze(tmp_path):
    """★ 2026-09-14, through the real keys.

    Every console set and cancel writes a delivery-ledger row marked `console`,
    so a wake-up that escalates can be traced to whoever set it — that morning
    nothing could say who had set ext 14's. And because an operator setting a
    wake-up during a ring may be setting it for somebody still asleep, that change
    must NOT stand the ring down the way the room's own phone does. The control
    makes the same change as a phone row, to show the set-up can see a snooze.
    """
    import json as _json
    import sys as _sys
    import time as _time

    d = console.delivery
    check("console: the ledger module loaded", d is not None)
    ledger = tmp_path / "delivery-outcomes.jsonl"

    def rows():
        return ([_json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
                if ledger.exists() else [])

    def set_620(sess, board):
        for key in ("W", "6", "2", "0", "enter"):
            console.apply_key(sess, key, board, lambda m: None)

    saved_out = d.OUTCOME_PATH
    saved_mods = {k: _sys.modules.get(k) for k in ("store", "ami", "ha_client", "delivery")}
    saved_path = list(_sys.path)
    try:
        d.OUTCOME_PATH = str(ledger)
        console.wakeup_store.cancel("11")
        board = _board(ROOMS)
        sess = {"sel": 0, "mode": "normal"}          # sel 0 -> Kitchen (ext 11)
        set_620(sess, board)
        [s] = rows()
        check("console: the set is recorded as console, with the time it rings",
              (s["ext"], s["kind"], s["outcome"], s["source"], s["hhmm"])
              == ("11", "wakeup", "set", "console", "06:20")
              and s["target_epoch"] == console.wakeup_store.get("11")["target_epoch"])
        console.apply_key(sess, "x", board, lambda m: None)
        console.apply_key(sess, "x", board, lambda m: None)
        c1, c2 = rows()[-2:]
        check("console: the cancel is recorded as console, and says it removed one",
              (c1["outcome"], c1["source"], c1["removed"]) == ("cancelled", "console", True))
        check("console: a cancel that found nothing is still recorded, and says so",
              (c2["outcome"], c2["removed"]) == ("cancelled", False))

        pushed = []

        class _AMI:
            @staticmethod
            def get_endpoints():
                return [{"name": "11", "state": "Not in use"}]

            @staticmethod
            def originate_wakeup(ext, ring):
                return True

        class _HA:
            @staticmethod
            def push(msg, **k):
                pushed.append(msg)
                return True

            @staticmethod
            def notify(msg, **k):
                return True

        for k in ("store", "ami", "ha_client"):
            _sys.modules[k] = _AMI
        _sys.modules["delivery"] = d
        sched = SourceFileLoader(
            "sched_console_snooze",
            str(Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
                / "switchboard" / "wakeup" / "scheduler.py")).load_module()
        sched.ami, sched.ha_client, sched.log = _AMI, _HA, lambda m: None

        def judge(change):
            """A second ring started 30 s ago; make `change`; judge it."""
            pushed.clear()
            sched._ringing.clear()
            started = _time.time() - 30
            sched._ringing["11"] = {"target_epoch": int(started), "hhmm": "06:10",
                                    "started": started, "retried": True}
            change()
            sched._reconcile_rings(started + sched.RETRY_AFTER + 1)
            return list(pushed)

        check("console: a set from the console during the ring still escalates",
              len(judge(lambda: set_620({"sel": 0, "mode": "normal"}, board))) == 1)
        entry = console.wakeup_store.get("11")
        check("control: the same change from the room's own phone is a snooze",
              judge(lambda: d.record_wakeup_change("11", d.SOURCE_PHONE,
                                                   entry=entry)) == [])
    finally:
        _sys.path[:] = saved_path
        for k, v in saved_mods.items():
            if v is None:
                _sys.modules.pop(k, None)
            else:
                _sys.modules[k] = v
        d.OUTCOME_PATH = saved_out
        console.wakeup_store.cancel("11")


# ── The cord-board presentation ──────────────────────────────────────────────── #
# State is shown by LAMPS — ◉ white-lit ready, ◉ amber in use / ringing, ✕ red
# offline, ○ unlit unknown — with the state word typed beside every one, under a
# brass nameplate. These pin the presentation rules, not the wording of a row.
_RINGING = {"ext": "14", "label": "Hall", "registered": True, "device_state": "Ringing",
            "call_state": "Ringing", "peer": "", "channel": ""}


def _views(w: int, h: int):
    """Every screen the console draws, at w×h."""
    board = _board(ROOMS + [_RINGING], calls=[
        {"detail": "Kitchen ↔ Office", "state": "Talking", "duration": "00:02:14", "kind": "internal"}])
    board.get().update(trunk_reg="Registered", stt="up",
                       wakeups=[{"ext": "11", "label": "Kitchen", "hhmm": "06:30", "target_epoch": 0}])
    lights = [dict(li) for li in _FAKE_LIGHTS]
    for mode, extra in (("normal", {}), ("help", {}), ("connect", {"connect_from_label": "Kitchen"}),
                        ("pageconfirm", {}), ("lights", {"lights": lights, "lsel": 0})):
        sess = {"sel": 0, "mode": mode, "w": w, "h": h, **extra}
        yield mode, console.render(board.get(), sess, 0.0)


def test_the_palette_is_the_board_palette() -> None:
    """The terminal mapping of the shared design tokens, and nothing else: a
    16-colour code left behind renders terminal green on a walnut board."""
    want = {"BRASS": 178, "LAMP": 214, "CREAM": 230, "BRASS_DIM": 94,
            "IDLE": 58, "FAULT": 160, "MUTED": 137}
    for name, n in want.items():
        check(f"palette: {name} is 256-colour {n}", getattr(console, name) == f"\x1b[38;5;{n}m")
    legacy = re.compile(r"\x1b\[(?:3[0-7]|9[0-7])m")
    stray = [(mode, ln) for mode, lines in _views(80, 30) for ln in lines if legacy.search(ln)]
    check("palette: no view still emits a 16-colour foreground", not stray)


def test_every_view_wears_the_nameplate_and_it_fits() -> None:
    """The masthead is the one bold element, so it must survive every width —
    and never run past the rule under it, or center() loses the margin and the
    frame shifts a column."""
    for w in (40, 60, 80, 120):
        for mode, lines in _views(w, 30):
            plain = [ln.strip() for ln in _plain(lines)]
            first = next(ln for ln in plain if ln)
            check(f"nameplate: {mode} at {w} opens with the brass plate",
                  first.startswith("═══ ") and "SWITCHBOARD" in first.replace(" ", ""))
            rule = next(ln for ln in plain[plain.index(first) + 1:] if ln)
            check(f"nameplate: {mode} at {w} is no wider than its rule ({len(first)} vs {len(rule)})",
                  console.vis_width(first) <= console.vis_width(rule))
            check(f"nameplate: {mode} at {w} fits the terminal",
                  all(console.vis_width(ln) <= w for ln in lines))
    wide = _plain([console._nameplate(78, "", "3/3 online · 1 on call · 07:00:00")])[0]
    check("nameplate: letter-spaced with the whole tally when it fits",
          wide.startswith("═══ S W I T C H B O A R D ═══") and wide.endswith("07:00:00 ═══")
          and console.vis_width(wide) == 78)
    narrow = _plain([console._nameplate(38, "", "3/3 online · 1 on call · 07:00:00")])[0]
    check("nameplate: a narrow plate drops the clock before the room count",
          "3/3 online" in narrow and "07:00" not in narrow and console.vis_width(narrow) == 38)
    check("nameplate: never wider than asked, however narrow",
          all(console.vis_width(console._nameplate(n, "LIGHTS", "1/2 on")) <= n for n in range(1, 90)))


def test_each_position_has_a_lamp_and_a_word() -> None:
    """Colour is never the only signal: every row carries its lamp AND its state
    word, the lamp is the right colour for the state, and the lamps form one
    column the eye can run down."""
    board = _board(ROOMS + [_RINGING])
    lines = console.render(board.get(), {"sel": 0, "mode": "normal", "w": 80, "h": 30}, 0.0)
    rows = {ext: ln for ln in lines for ext in ("11", "12", "13", "14")
            if re.search(rf"[◉○✕]\S*\s+(?:\x1b\[[0-9;]*m)*{ext}\b", ln)}
    expect = {"11": ("◉", console.CREAM, "READY"), "12": ("◉", console.LAMP, "TALKING"),
              "13": ("✕", console.FAULT, "OFFLINE"), "14": ("◉", console.LAMP, "RINGING")}
    check("lamp: every position is on the board", set(rows) == set(expect))
    for ext, (glyph, col, word) in expect.items():
        ln = rows.get(ext, "")
        check(f"lamp: ext {ext} shows {glyph} in its state colour", (col + glyph) in ln)
        check(f"lamp: ext {ext} says {word} beside it", word in _plain([ln])[0])
    plain = {ext: _plain([ln])[0] for ext, ln in rows.items()}
    check("lamp: the lamps line up in one column",
          len({plain[x].index(expect[x][0]) for x in plain}) == 1)
    check("lamp: the state words line up in one column",
          len({plain[x].index(expect[x][2]) for x in plain}) == 1)


def test_a_ringing_lamp_flickers_but_its_word_does_not() -> None:
    """Amber, then unlit, on the console's existing once-a-second redraw. The
    word stays put so a ringing room is never read from the lamp alone, and the
    row keeps its width so nothing beside it jitters."""
    board = _board([_RINGING])
    a, b = (console.render(board.get(), {"sel": 1, "mode": "normal", "w": 80, "h": 24}, t)
            for t in (100.0, 101.0))
    row_a = next(ln for ln in a if "RINGING" in ln)
    row_b = next(ln for ln in b if "RINGING" in ln)
    check("flicker: the lamp is amber one second and unlit the next",
          (console.LAMP + "◉") in row_a and (console.IDLE + "◉") in row_b)
    check("flicker: the word is amber both times",
          (console.LAMP + "RINGING") in row_a and (console.LAMP + "RINGING") in row_b)
    check("flicker: the row does not change width", console.vis_width(row_a) == console.vis_width(row_b))
    steady = _board([ROOMS[0]])
    still = [console.render(steady.get(), {"sel": 1, "mode": "normal", "w": 80, "h": 24}, t)
             for t in (100.0, 101.0)]
    check("flicker: an idle room's lamp holds steady",
          next(ln for ln in still[0] if "READY" in ln) == next(ln for ln in still[1] if "READY" in ln))


def test_the_board_holds_at_40_80_and_120_columns() -> None:
    """The narrow/ambiguous-glyph width rule still holds with the new glyphs:
    every line within the terminal, the roster complete, the keys on screen."""
    for w in (40, 80, 120):
        for mode, lines in _views(w, 30):
            text = "\n".join(_plain(lines))
            check(f"width: {mode} at {w} cols stays inside the terminal",
                  all(console.vis_width(ln) <= w for ln in lines) and len(lines) <= 30)
            if mode == "normal":
                check(f"width: every room is on the board at {w}",
                      all(lbl in text for lbl in ("Kitchen", "Office", "Garage", "Hall")))
                check(f"width: the keys are on the board at {w}", "R ring" in text and "Q quit" in text)
    check("width: the lamps and leaders are one column each",
          console.vis_width("◉○✕═─·") == 6)


def test_a_narrow_board_keeps_the_calls_state_word() -> None:
    """Below 64 columns the call's KIND column gives way, so the state word
    is not the part of the row that falls off the edge."""
    board = _board(ROOMS, calls=[
        {"detail": "Kitchen ↔ Office", "state": "Talking", "duration": "00:02:14", "kind": "internal"}])
    for w, kind_shown in ((40, False), (80, True)):
        lines = console.render(board.get(), {"sel": 0, "mode": "normal", "w": w, "h": 30}, 0.0)
        row = next(ln for ln in _plain(lines) if "Kitchen ↔ Office" in ln)
        check(f"calls: the state word is whole at {w} cols", "Talking" in row)
        check(f"calls: the kind column is {'shown' if kind_shown else 'dropped'} at {w} cols",
              ("INTERNAL" in row) == kind_shown)


def test_the_help_card_dismiss_hint_fits_21_rows() -> None:
    """The lamp legend is set into a rule rather than added as a row, so the
    help card is still 21 rows and its dismiss hint is on a 21-row terminal."""
    check("help: the card is 21 rows", len(console._help_lines(80)) == 21)
    lines = _plain(console.render(_board(ROOMS).get(), {"sel": 0, "mode": "help", "w": 80, "h": 21}, 0.0))
    check("help: the dismiss hint is on a 21-row terminal",
          any("Press any key to return to the board." in ln for ln in lines))
    check("help: the lamp legend is still there",
          any("LAMPS" in ln and "ready" in ln and "offline" in ln for ln in lines))


def test_a_lights_badge_colours_only_its_lamp() -> None:
    """The on/off word stays in the terminal's own foreground; only the lamp
    glyph carries the colour, as on the roster."""
    lights = [dict(li) for li in _FAKE_LIGHTS]
    lines = console.render(_board(ROOMS).get(),
                           {"sel": 0, "mode": "lights", "w": 80, "h": 30, "lights": lights, "lsel": 0}, 0.0)
    on = [ln for ln in lines if "◉" in ln and ln.endswith(" on")]
    off = [ln for ln in lines if "○" in ln and ln.endswith(" off")]
    check("lights: a lit light's word is uncoloured", bool(on) and all(
        ln.endswith(console.CREAM + "◉" + console.RESET + " on") for ln in on))
    check("lights: an unlit light's word is uncoloured", bool(off) and all(
        ln.endswith(console.MUTED + "○" + console.RESET + " off") for ln in off))


SOCKET = "/run/switchboard/console.sock"

PREAMBLE = bytes([255, 251, 1, 255, 251, 3, 255, 253, 3, 255, 253, 31])

def _sock_dir() -> str:
    """A temp dir short enough for a Unix socket path (macOS caps it at 104)."""
    d = tempfile.mkdtemp(prefix="sbc-")
    if len(d) > 80:
        d = tempfile.mkdtemp(prefix="sbc-", dir="/tmp")
    return d

@contextlib.contextmanager
def _running_board(path, max_sessions=None):
    import threading
    logs = []
    stop = threading.Event()
    srv = console.make_server(path, _board([]), stop, console.ClientGate(), logs.append,
                              max_sessions=max_sessions or console.MAX_SESSIONS)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        yield srv, logs
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
        t.join(5)

def _client(path):
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(path)
    return s

def _read_all(sock, limit=5.0) -> bytes:
    import time
    buf, end = b"", time.monotonic() + limit
    while time.monotonic() < end:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf

def test_the_board_has_no_tcp_listener() -> None:
    import socket
    import socketserver
    check("listener: the server class is a Unix stream server",
          issubclass(console.ConsoleServer, socketserver.UnixStreamServer)
          and console.ConsoleServer.address_family == socket.AF_UNIX)
    check("listener: the default path is the board's socket", console.SOCKET_PATH == SOCKET)
    code = "\n".join(ln for ln in CONSOLE_PATH.read_text().splitlines()
                     if not ln.lstrip().startswith("#"))
    for gone in ("TCPServer", 'environ.get("CONSOLE_PORT"', 'environ.get("CONSOLE_HOST"'):
        check(f"listener: no {gone} left in console.py", gone not in code)

def test_the_socket_is_0600_whatever_the_umask() -> None:
    """bind() creates the inode as 0777 & ~umask. Everything here runs under
    umask 0, the most permissive there is.

    The first half binds with bind_owner_only() alone, with no chmod after it,
    so what it sees is the mode the inode was CREATED with: a bind that relied
    on the inherited umask, or only on a later chmod, leaves a window in which
    any local process can connect, and shows here as 0777. The second half
    proves the server binds through it and ends at 0600."""
    import socket
    import stat
    old = os.umask(0)
    try:
        path = os.path.join(_sock_dir(), "console.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            console.bind_owner_only(s, path)
            created = stat.S_IMODE(os.stat(path).st_mode)
            restored = os.umask(0)
        check(f"socket: created 0600, before any chmod ({created:04o})", created == 0o600)
        check("socket: the bind put the caller's umask back", restored == 0)

        calls = []
        real = console.bind_owner_only
        console.bind_owner_only = lambda sock, p: (calls.append(p), real(sock, p))
        try:
            path = os.path.join(_sock_dir(), "console.sock")
            with _running_board(path):
                st = os.stat(path)
                check("socket: the path is a socket", stat.S_ISSOCK(st.st_mode))
                check(f"socket: mode is 0600 ({stat.S_IMODE(st.st_mode):04o})",
                      stat.S_IMODE(st.st_mode) == 0o600)
        finally:
            console.bind_owner_only = real
        check(f"socket: the server binds through bind_owner_only ({calls})", calls == [path])
    finally:
        os.umask(old)

def test_a_client_gets_the_telnet_board_and_can_quit() -> None:
    """The protocol on the socket is unchanged: telnet negotiation first, then the
    board, and `q` ends the session cleanly. A Unix-socket client has no peer
    address, so the connect log line must not try to print one."""
    path = os.path.join(_sock_dir(), "console.sock")
    with _running_board(path) as (_srv, logs):
        with _client(path) as c:
            first = b""
            while len(first) < len(PREAMBLE):
                first += c.recv(4096)
            check("session: the first bytes are the telnet preamble",
                  first[:len(PREAMBLE)] == PREAMBLE)
            c.sendall(bytes([255, 250, 31, 0, 120, 0, 40, 255, 240]) + b"q")
            tail = _read_all(c)
            check("session: q restores the screen and hangs up",
                  console.EXIT_ALT.encode() in tail)
        import time
        deadline = time.monotonic() + 5
        while "client disconnected" not in logs and time.monotonic() < deadline:
            time.sleep(0.02)
        check(f"session: connect and disconnect are logged ({logs})",
              logs[:1] == ["client connected"] and "client disconnected" in logs)
        check("session: no session error", not any("error" in m for m in logs))

def test_a_stale_socket_from_an_earlier_run_is_replaced() -> None:
    """A crash skips server_close, and bind() on an existing path fails, so
    without the unlink one crash would keep the board down for good."""
    import socket
    path = os.path.join(_sock_dir(), "console.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as dead:
        dead.bind(path)                    # leaves the file behind, nobody listening
    with _running_board(path):
        with _client(path) as c:
            check("stale: a client reaches the new board",
                  c.recv(len(PREAMBLE)) == PREAMBLE[:len(PREAMBLE)])

def test_a_file_that_is_not_a_socket_is_never_deleted() -> None:
    import threading
    path = os.path.join(_sock_dir(), "console.sock")
    Path(path).write_text("not ours\n")
    raised = None
    try:
        console.make_server(path, _board([]), threading.Event(), console.ClientGate(),
                            lambda m: None)
    except RuntimeError as exc:
        raised = exc
    check(f"refuse: a regular file at the path is refused ({raised})", raised is not None)
    check("refuse: and left exactly as it was", Path(path).read_text() == "not ours\n")

def test_closing_removes_its_own_socket_and_never_a_successors() -> None:
    import socket
    path = os.path.join(_sock_dir(), "console.sock")
    with _running_board(path):
        pass
    check("close: the socket is removed on shutdown", not os.path.exists(path))

    with _running_board(path):
        os.unlink(path)                    # a successor takes the path over
        successor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        successor.bind(path)
    try:
        check("close: a successor's socket at the same path survives",
              os.path.exists(path))
    finally:
        successor.close()
        os.unlink(path)

def test_a_client_running_as_another_user_is_refused() -> None:
    """The second lock behind the 0600 mode. The uid lookup is the kernel's, so
    the other user is simulated by replacing it."""
    path = os.path.join(_sock_dir(), "console.sock")
    real = console.peer_uid
    console.peer_uid = lambda sock: os.geteuid() + 1
    try:
        with _running_board(path) as (_srv, logs):
            with _client(path) as c:
                got = _read_all(c)
            check(f"peer: another user gets nothing, not even the preamble ({got!r})",
                  got == b"")
            check(f"peer: the refusal is logged ({logs})",
                  any(m.startswith("refused a local client") for m in logs)
                  and "client connected" not in logs)
    finally:
        console.peer_uid = real

def test_peer_uid_reports_this_process_where_the_kernel_can() -> None:
    import socket
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with a, b:
        got = console.peer_uid(a)
    if hasattr(socket, "SO_PEERCRED"):
        check(f"peer_uid: SO_PEERCRED reports this process's uid ({got})", got == os.geteuid())
    else:
        print("  (no SO_PEERCRED on this platform; the 0600 mode alone guards here)")
        check("peer_uid: None where the kernel has no SO_PEERCRED", got is None)

def test_a_full_board_says_busy() -> None:
    path = os.path.join(_sock_dir(), "console.sock")
    with _running_board(path, max_sessions=1):
        with _client(path) as first:
            check("busy: the first session is served", first.recv(len(PREAMBLE)) == PREAMBLE)
            with _client(path) as second:
                got = _read_all(second)
            check(f"busy: the second is told so and hung up ({got!r})",
                  b"busy" in got and PREAMBLE not in got)

def _top_level_ints(path: Path) -> dict:
    import ast
    out = {}
    for node in ast.parse(path.read_text()).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, int)):
            out[node.targets[0].id] = node.value.value
    return out

def test_the_board_cap_holds_every_front_door_at_once() -> None:
    """Every front end caps its own sessions; the board's cap is the backstop
    behind all of them. Below their sum, one front end's sessions could lock the
    others out (at the old cap of 5, five sidebar tabs left nothing for SSH).

    The SSH service's cap is read from its module once it exists (a top-level
    MAX_*CONNECTIONS / MAX_*SESSIONS / MAX_*CLIENTS int that is not a per-address
    one); until then the design figure of 16 stands in."""
    import re as _re
    share = CONSOLE_PATH.parents[1]
    web = _top_level_ints(share / "console-web" / "server.py")["MAX_SESSIONS"]
    sidebar = _top_level_ints(share / "webui" / "console_bridge.py")["MAX_SESSIONS"]
    ssh = 16
    for py in sorted((share / "console-ssh").glob("*.py")) if (share / "console-ssh").is_dir() else []:
        for name, value in _top_level_ints(py).items():
            if (_re.fullmatch(r"MAX_\w*(CONNECTIONS|SESSIONS|CLIENTS)", name)
                    and "PER" not in name):
                ssh = max(ssh, value)
    need = web + sidebar + ssh
    check(f"caps: board {console.MAX_SESSIONS} >= web {web} + sidebar {sidebar} + SSH {ssh}",
          console.MAX_SESSIONS >= need)

def test_every_reference_names_the_same_socket() -> None:
    """★ The interface between the board and its clients is one path. A client
    pointed anywhere else shows "unavailable" with every unit test still green,
    so every mention of a console socket in the shipped tree must be this one."""
    import re as _re
    root = CONSOLE_PATH.parents[4]           # .../rootfs
    seen, files = set(), []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if "console.sock" not in text:
            continue
        # The lookbehind anchors each match at the start of a path token, so a
        # long minified line is scanned once rather than once per character.
        found = set(_re.findall(r"(?<![\w./-])[\w./-]*console\.sock\b", text))
        if found:
            seen |= found
            files.append(p.relative_to(root).as_posix())
    check(f"socket: every reference is {SOCKET} (found {sorted(seen)})", seen == {SOCKET})
    for must in ("usr/share/switchboard/console/console.py",
                 "usr/share/switchboard/console-web/server.py",
                 "usr/share/switchboard/webui/console_bridge.py"):
        check(f"socket: {must} names it", must in files)


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
