"""Behavioral tests for the telnet operator console (console/console.py).

Run with plain Python (no deps):

    python3 switchboard/tests/test_console.py

Covers the pure pieces: telnet/ANSI input parsing, board rendering, and the
action key handling (navigation + mode transitions + guarded actions). The AMI
side effects (ring/connect/hangup) are exercised only on their guard paths,
which never open a socket.
"""
import os
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


def test_cancel_wakeup_key() -> None:
    console.wakeup_store.set_wakeup("11", "07:00")
    board = console.Board()
    board.set({"ami_ok": True, "rooms": ROOMS, "calls": [], "wakeups": [], "ts": 0.0})
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
    # On a wide+tall terminal the board floats off the top-left corner.
    board = _board(ROOMS)
    lines = console.render(board.get(), {"sel": 0, "mode": "normal", "w": 120, "h": 40}, 0.0)
    check("center: board indented on a wide screen", lines[0] == "" and any(ln.startswith("   ") for ln in lines if ln.strip()))


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
        text = "\n".join(console.render(board.get(), sess, 0.0))
        check("render: lights view shows names + on/off",
              "LIGHTS" in text and "Kitchen Main" in text and "● on" in text and "○ off" in text)
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


def main() -> None:
    test_parse_input()
    test_render()
    test_render_connect_mode()
    test_render_ami_down()
    test_navigation()
    test_connect_mode_transitions()
    test_render_transfer_mode()
    test_transfer_mode_transitions()
    test_transfer_offline_target_refused()
    test_fmt12()
    test_render_wakeups()
    test_cancel_wakeup_key()
    test_guarded_actions()
    test_parse_input_backspace()
    test_is_quit()
    test_wakeup_when()
    test_set_wakeup_mode()
    test_set_wakeup_invalid_and_empty()
    test_set_wakeup_buffer_capped()
    test_set_wakeup_edit_prefill()
    test_render_wakeup_mode()
    test_help_mode()
    test_vis_width()
    test_center()
    test_page_confirm_accept()
    test_page_confirm_cancel()
    test_mwi_toggle()
    test_mwi_set_failure()
    test_mwi_badge_render()
    test_lights_entry_and_unavailable()
    test_lights_nav_toggle_exit()
    test_lights_toggle_failure_no_flip()
    test_help_and_bar_have_new_keys()
    test_command_bar_fits_80()
    print()
    if _failures:
        print(f"{_failures} FAILURE(S)")
        raise SystemExit(1)
    print("all console tests passed")


if __name__ == "__main__":
    main()
