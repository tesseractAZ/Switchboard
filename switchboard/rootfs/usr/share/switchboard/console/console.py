"""Switchboard operator console — a live telnet TUI for a switchboard operator.

Connect with `telnet <host> <port>` (default 2300). A raw TCP server speaking
just enough of the telnet protocol to put a standard client into
character-at-a-time mode, then a live ANSI board of every room phone with
operator actions: ring a room, connect two rooms (patch a call), and hang up.

No third-party deps: stdlib socket/threading + the framework-free AMI client in
the sibling webui module. The render + input parsing are pure and unit-tested
(see tests/test_console.py); only the socket plumbing and the AMI side effects
touch the outside world.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
import unicodedata
from datetime import date

# Reuse the AMI engine that backs the web dashboard.
sys.path.insert(0, "/usr/share/switchboard/webui")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "webui"))
sys.path.insert(0, "/usr/share/switchboard/wakeup")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wakeup"))
import ami  # noqa: E402
try:
    import store as wakeup_store  # noqa: E402
except ImportError:  # pragma: no cover
    wakeup_store = None
try:
    import timeparse  # noqa: E402
except ImportError:  # pragma: no cover
    timeparse = None
try:
    import mwi_store  # noqa: E402
except ImportError:  # pragma: no cover
    mwi_store = None
try:
    import ha_client  # noqa: E402
except ImportError:  # pragma: no cover
    ha_client = None

OPTIONS_PATH = os.environ.get("SWITCHBOARD_OPTIONS", "/data/options.json")
# Board refresh cadence. A phone board's registration/call state changes on the
# order of seconds, and operator actions (ring/connect/hangup/page) refresh
# immediately, so a few seconds of passive freshness is plenty — and a longer
# interval (vs the old 1.5s) keeps Asterisk's manager log from filling with
# poll-driven logon/logoff churn. Paired with the single-session get_status_bundle.
POLL_SECONDS = 3.0
# This is an unauthenticated LAN service; bound the blast radius of a misbehaving
# or hostile client.
MAX_SESSIONS = 5
IDLE_SECONDS = 900  # drop a session after 15 min with no input

# ── ANSI ──────────────────────────────────────────────────────────────────── #
ESC = "\x1b"
HIDE_CURSOR = f"{ESC}[?25l"
SHOW_CURSOR = f"{ESC}[?25h"
CLEAR_SCREEN = f"{ESC}[2J"
CURSOR_HOME = f"{ESC}[H"
CLEAR_EOL = f"{ESC}[K"
CLEAR_BELOW = f"{ESC}[J"
RESET = f"{ESC}[0m"
BOLD = f"{ESC}[1m"
DIM = f"{ESC}[2m"
ENTER_ALT = f"{ESC}[?1049h"
EXIT_ALT = f"{ESC}[?1049l"
GREEN = f"{ESC}[32m"
RED = f"{ESC}[31m"
YELLOW = f"{ESC}[33m"
CYAN = f"{ESC}[36m"
BLUE = f"{ESC}[34m"
GREY = f"{ESC}[90m"


def color(code: str, text: str) -> str:
    return f"{code}{text}{RESET}"


# ── Telnet protocol bytes ───────────────────────────────────────────────────── #
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA, OPT_NAWS = 1, 3, 31


def parse_input(buf: bytes):
    """Parse a raw telnet input buffer into (events, rest), stripping IAC
    negotiation. events are ("key", <name>) or ("naws", w, h). An incomplete
    trailing sequence is returned in rest to prepend to the next chunk."""
    events = []
    n = len(buf)
    i = 0
    while i < n:
        b = buf[i]
        if b == IAC:
            if i + 1 >= n:
                break
            cmd = buf[i + 1]
            if cmd == IAC:
                i += 2
                continue
            if cmd == SB:
                j = i + 2
                se = -1
                incomplete = False
                while j < n:
                    if buf[j] == IAC:
                        if j + 1 >= n:
                            incomplete = True
                            break
                        if buf[j + 1] == SE:
                            se = j
                            break
                        j += 2
                        continue
                    j += 1
                if incomplete or se < 0:
                    break
                sub = buf[i + 2:se]
                if len(sub) >= 5 and sub[0] == OPT_NAWS:
                    events.append(("naws", (sub[1] << 8) | sub[2], (sub[3] << 8) | sub[4]))
                i = se + 2
                continue
            if WILL <= cmd <= DONT:
                if i + 2 >= n:
                    break
                i += 3
                continue
            i += 2
            continue
        if b == 0x1b:
            if i + 1 >= n:
                break
            b1 = buf[i + 1]
            if b1 in (0x5b, 0x4f):  # CSI / SS3
                if i + 2 >= n:
                    break
                arrow = {0x41: "up", 0x42: "down", 0x43: "right", 0x44: "left"}.get(buf[i + 2])
                if arrow:
                    events.append(("key", arrow))
                i += 3
                continue
            events.append(("key", "esc"))
            i += 1
            continue
        if b == 13:  # CR
            events.append(("key", "enter"))
            i += 1
            if i < n and buf[i] in (10, 0):
                i += 1
            continue
        if b == 10:  # LF
            events.append(("key", "enter"))
            i += 1
            continue
        if b == 3:  # Ctrl-C
            events.append(("key", "ctrl-c"))
            i += 1
            continue
        if b in (8, 127):  # Backspace / Delete (xterm sends 0x7f; raw telnet 0x08)
            events.append(("key", "backspace"))
            i += 1
            continue
        if 32 <= b < 127:
            events.append(("key", chr(b)))
            i += 1
            continue
        i += 1
    return events, buf[i:]


# ── Board model (shared snapshot, refreshed by a poller thread) ─────────────── #
def load_rooms_cfg() -> dict:
    try:
        with open(OPTIONS_PATH) as fh:
            opts = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {str(r.get("ext")): r for r in (opts.get("rooms") or [])}


_CODEC_NAMES = {"ulaw": "µ-law", "alaw": "A-law", "g722": "G.722", "g729": "G.729",
                "g723": "G.723", "g726": "G.726", "opus": "Opus", "ilbc": "iLBC", "slin16": "L16"}


def _codec_label(codec: str) -> str:
    """Pretty per-call codec for the board: "ulaw" -> "µ-law"; "g722/ulaw" (a
    transcode across the two legs) -> "G.722/µ-law"; "" -> "" (no live leg yet)."""
    codec = str(codec or "")
    return "/".join(_CODEC_NAMES.get(x, x) for x in codec.split("/")) if codec else ""


def build_board(rooms_cfg: dict) -> dict:
    """One AMI poll → a board dict the renderer consumes. Pure given the AMI
    helpers; isolated here so the renderer/tests never touch a socket."""
    rooms_by_ext = {ext: (cfg.get("name") or ext) for ext, cfg in rooms_cfg.items()}
    try:
        # One AMI session for the whole board read (endpoints + contacts +
        # channels) instead of three per poll — this is the dominant source of
        # the manager logon/logoff churn since the poller runs every few seconds.
        endpoints, contacts, channels = ami.get_status_bundle()
        ami_ok = True
    except (ami.AMIError, OSError):
        endpoints, contacts, channels, ami_ok = [], {}, [], False
    # Tag each live leg with its negotiated codec (only reads while a call is up).
    codecs = ami.codecs_for_channels(channels)
    for ch in channels:
        ch["codec"] = codecs.get(ch.get("channel", ""), "")
    summary = ami.summarize_calls(channels, rooms_by_ext)
    by_ext = summary["by_ext"]

    # First channel per room = the leg to hang up.
    chan_by_ext: dict[str, str] = {}
    for ch in channels:
        e = ch.get("ext", "")
        if e in rooms_by_ext and e not in chan_by_ext:
            chan_by_ext[e] = ch.get("channel", "")

    def _mwi(ext: str) -> bool:
        if mwi_store is None:
            return False
        try:
            return mwi_store.is_set(ext)
        except Exception:
            return False

    rooms = []
    seen = set()
    for ep in endpoints:
        name = ep["name"]
        if name == "trunk":
            continue
        seen.add(name)
        contact = contacts.get(name, {})
        ds = ep["state"]
        registered = ami.is_registered(ds, contact.get("status", ""))
        call = by_ext.get(name, {})
        rooms.append({
            "ext": name, "label": rooms_by_ext.get(name, name), "registered": registered,
            "device_state": ds, "call_state": call.get("state", ""), "peer": call.get("peer", ""),
            "channel": chan_by_ext.get(name, ""), "mwi": _mwi(name),
        })
    for ext, cfg in rooms_cfg.items():
        if ext not in seen:
            rooms.append({
                "ext": ext, "label": cfg.get("name") or ext, "registered": False,
                "device_state": "Unavailable", "call_state": "", "peer": "", "channel": "",
                "mwi": _mwi(ext),
            })
    rooms.sort(key=lambda r: r["ext"])

    wakeups = []
    if wakeup_store is not None:
        try:
            for ext, e in wakeup_store.all_wakeups().items():
                wakeups.append({"ext": ext, "label": rooms_by_ext.get(ext, ext),
                                "hhmm": e.get("hhmm", ""), "target_epoch": e.get("target_epoch", 0)})
            wakeups.sort(key=lambda w: w["target_epoch"])
        except Exception:
            wakeups = []
    return {"ami_ok": ami_ok, "rooms": rooms, "calls": summary["calls"],
            "wakeups": wakeups, "ts": time.time()}


def fmt12(hhmm: str) -> str:
    """'07:05' -> '7:05 AM'."""
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except (ValueError, AttributeError):
        return hhmm or ""
    ap = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{m:02d} {ap}"


def wakeup_when(target_epoch: float, now: float) -> str:
    """'today' or 'tomorrow' for a wake-up, by local-date comparison of its
    next-occurrence epoch against now (consistent with store.next_epoch's roll)."""
    return "tomorrow" if date.fromtimestamp(target_epoch) != date.fromtimestamp(now) else "today"


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def vis_width(s: str) -> int:
    """Visible column width of a line: strip ANSI SGR codes, count East-Asian
    wide/fullwidth glyphs (our 🔌 / ⏰ emoji) as 2 and everything else — including
    'ambiguous' box/arrow glyphs, which terminals render narrow — as 1."""
    s = _ANSI_RE.sub("", s)
    return sum(0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in ("W", "F") else 1)
               for c in s)


def center(lines: list[str], w: int, h: int) -> list[str]:
    """Center a rendered block in a w×h terminal — the roster is small, so on a
    big screen it would otherwise sit jammed in the top-left. Indent every line by
    a common left margin and prepend blank lines, but never push content off-screen
    (no padding once it's as wide/tall as the terminal). Pure; unit-tested."""
    if not lines:
        return lines
    content_w = max((vis_width(ln) for ln in lines), default=0)
    left = max(0, (w - content_w) // 2)
    body = [(" " * left) + ln for ln in lines] if left else list(lines)
    top = max(0, (h - len(lines)) // 2)
    return [""] * top + body


class Board:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {"ami_ok": False, "rooms": [], "calls": [], "ts": 0.0}

    def get(self) -> dict:
        with self._lock:
            return self._data

    def set(self, data: dict) -> None:
        with self._lock:
            self._data = data


def poller_loop(board: Board, stop: threading.Event, log) -> None:
    while not stop.is_set():
        try:
            board.set(build_board(load_rooms_cfg()))
        except Exception as exc:  # never let the poller die
            log(f"poll error: {exc}")
        stop.wait(POLL_SECONDS)


# ── Rendering (pure) ─────────────────────────────────────────────────────────── #
def _room_status(room: dict):
    """(glyph, color, text, peer_suffix) for a room row."""
    ds = (room.get("device_state") or "").lower()
    cs = (room.get("call_state") or "")
    peer = room.get("peer") or ""
    suffix = f"  ↔ {peer}" if peer else ""
    if not room.get("registered"):
        return "○", RED, "Offline", ""
    if cs == "Ringing" or "ring" in ds:
        return "◐", YELLOW, "Ringing", suffix
    active = bool(cs) or (("use" in ds and ds != "not in use") or ds in ("busy", "on hold"))
    if active:
        return "◉", CYAN, (cs or "On call"), suffix
    return "●", GREEN, "Registered", ""


def _help_lines(width: int) -> list[str]:
    """The `?` help overlay — a one-screen key reference, dismissed by any key."""
    rule = color(GREY, "─" * min(width, 72))
    b = lambda s: color(BOLD, s)  # noqa: E731 (tiny local alias for the key glyphs)
    return [
        f" {BOLD}🔌 SWITCHBOARD OPERATOR — HELP{RESET}",
        rule,
        f"  {b('↑ ↓')} / {b('j k')}   Move the selection between rooms",
        f"  {b('R')}         Ring the selected room (a short test ring)",
        f"  {b('C')}         Connect — then pick another room and press Enter to patch",
        f"  {b('H')}         Hang up the selected room's active call",
        f"  {b('W')}         Set a wake-up — type a time (7:30, \"quarter past six\",",
        "            0730, noon), then Enter. Esc cancels.",
        f"  {b('X')}         Cancel the selected room's wake-up",
        f"  {b('M')}         Toggle a message-waiting ✉ (stutter tone) on the room",
        f"  {b('P')}         Page all — ring every phone into the intercom (confirm)",
        f"  {b('L')}         Lights — control Home Assistant lights",
        f"  {b('?')}         This help",
        f"  {b('Q')} / {b('Ctrl-C')}  Quit",
        rule,
        "  " + color(BOLD, "In the lights view:") + color(GREY, "  ↑↓/jk move · Enter/Space toggle"),
        "  " + color(GREY, "                     r refresh · Esc back to the board"),
        rule,
        "  " + color(GREY, "Wake-ups place a spoken “good morning” call at the set time."),
        "  " + color(CYAN, "Press any key to return to the board."),
    ]


def _lights_lines(sess: dict, width: int) -> list[str]:
    """The `L` lights view — an area-grouped list of HA lights with a cursor.
    Pure: reads the list already fetched into sess['lights'] (render never does
    I/O), so it works the same in tests as it does live."""
    lights = sess.get("lights", []) or []
    lsel = max(0, min(sess.get("lsel", 0), max(0, len(lights) - 1)))
    rule = color(GREY, "─" * min(width, 72))
    on_n = sum(1 for li in lights if str(li.get("state", "")).lower() == "on")
    head_right = color(GREY, f"{on_n}/{len(lights)} on ")
    lines = [
        f" {BOLD}💡 LIGHTS{RESET}   {head_right}",
        rule,
    ]
    if not lights:
        lines.append("  " + color(GREY, "— no lights —"))
    else:
        last_area = object()  # sentinel so a real "" area still prints once
        for idx, li in enumerate(lights):
            area = li.get("area") or ""
            if area != last_area:
                last_area = area
                lines.append("  " + color(BOLD, area or "Unassigned"))
            cursor = color(CYAN, "▸") if idx == lsel else " "
            is_on = str(li.get("state", "")).lower() == "on"
            badge = color(GREEN, "● on") if is_on else color(GREY, "○ off")
            name = (li.get("name") or li.get("entity_id") or "")[:28].ljust(28)
            lines.append(f"    {cursor} {name}  {badge}")
    lines.append(rule)
    lines.append("  " + color(GREY, "[↑↓] select   ") + color(BOLD, "Enter") + color(GREY, " toggle   ")
                 + color(BOLD, "r") + color(GREY, " refresh   ")
                 + color(BOLD, "Esc") + color(GREY, " back   ") + color(BOLD, "Q") + color(GREY, " quit"))
    return lines


def render(board: dict, sess: dict, now: float) -> list[str]:
    """Return the screen as a list of plain+ANSI lines. CLEAR_EOL per line means
    we don't pad to full width; content is kept within it."""
    width = sess.get("w", 80)
    height = sess.get("h", 24)
    if sess.get("mode") == "help":
        return center(_help_lines(width), width, height)
    if sess.get("mode") == "lights":
        lines = _lights_lines(sess, width)
        msg = sess.get("msg", "")
        lines.append("  " + color(CYAN, "› " + msg) if (msg and sess.get("msg_until", 0) > now) else "")
        return center(lines, width, height)
    rooms = board.get("rooms", [])
    calls = board.get("calls", [])
    sel = sess.get("sel", 0)
    rule = color(GREY, "─" * min(width, 72))
    lines: list[str] = []

    on_calls = sum(1 for r in rooms if r.get("call_state"))
    online = sum(1 for r in rooms if r.get("registered"))
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    head_left = f" {BOLD}🔌 SWITCHBOARD OPERATOR{RESET}"
    head_right = color(GREY, f"{online}/{len(rooms)} online · {on_calls} on call · {clock} ")
    lines.append(f"{head_left}   {head_right}")
    lines.append(rule)

    if not board.get("ami_ok", False):
        lines.append("  " + color(RED, "Asterisk Manager unreachable — the PBX may still be starting."))
        lines.append(rule)

    for idx, r in enumerate(rooms):
        cursor = color(CYAN, "▸") if idx == sel else " "
        glyph, col, txt, suffix = _room_status(r)
        # Pad the PLAIN text first; wrap in color after, so ANSI codes don't
        # throw off the column width.
        ext = f"{r['ext']:<3}"
        label = (r.get("label") or r["ext"])[:16].ljust(16)
        status = color(col, f"{glyph} {txt}")
        # ✉ marker for a room with a message-waiting flag set. The glyph is a
        # narrow (width-1) char, so it lines up like any other trailing badge.
        mwi = color(YELLOW, "  ✉") if r.get("mwi") else ""
        row = f"  {cursor} {BOLD}{ext}{RESET}  {label}  {status}{color(GREY, suffix)}{mwi}"
        lines.append(row)

    lines.append(rule)
    lines.append("  " + color(BOLD, "ACTIVE CALLS"))
    if not calls:
        lines.append("    " + color(GREY, "— none —"))
    else:
        glyphs = {"outside": "📞", "operator": "🎧", "internal": "🏠"}
        for c in calls:
            g = glyphs.get(c.get("kind", "internal"), "•")
            dur = str(c.get("duration", "") or "")
            if dur.startswith("00:"):
                dur = dur[3:]
            cname = _codec_label(c.get("codec", ""))
            meta = c.get("state", "") + (f"  {dur}" if dur else "") + (f"  {cname}" if cname else "")
            tail = color(GREY, meta)
            lines.append(f"    {g}  {c.get('detail','')}   {tail}")

    wakeups = board.get("wakeups", [])
    if wakeups:
        lines.append(rule)
        lines.append("  " + color(BOLD, "WAKE-UPS"))
        for w in wakeups:
            lines.append(f"    ⏰  {w.get('label','')}   " + color(GREY, fmt12(w.get("hhmm", ""))))

    lines.append(rule)
    if sess.get("mode") == "wakeup":
        label = sess.get("wakeup_label", "?")
        buf = sess.get("wakeup_buf", "")
        hhmm = timeparse.parse(buf) if (timeparse is not None and buf) else None
        # Live preview: show the (forgiving) parser's reading before committing —
        # the same parse()+fmt12() the commit path uses, so they can't disagree.
        preview = color(GREY, f"   → {fmt12(hhmm)}") if hhmm else ""
        lines.append("  " + color(YELLOW, f"SET WAKE-UP {label}:  {buf}█") + preview)
        lines.append("  " + color(GREY, "type a time · Enter sets · Esc cancels · Backspace deletes"))
    elif sess.get("mode") == "connect":
        frm = sess.get("connect_from_label", "?")
        lines.append("  " + color(YELLOW, f"CONNECT {frm} → pick a room with ↑↓ and press Enter") + color(GREY, "  (Esc cancels)"))
    elif sess.get("mode") == "pageconfirm":
        lines.append("  " + color(YELLOW, "PAGE ALL — ring every phone into the intercom?")
                     + color(GREY, "   ") + color(BOLD, "[Y]") + color(GREY, " yes    ")
                     + color(BOLD, "[N]") + color(GREY, " cancel"))
    else:
        bar1 = ("  " + color(GREY, "[↑↓] select   ") + color(BOLD, "R") + color(GREY, " ring   ")
                + color(BOLD, "C") + color(GREY, " connect   ") + color(BOLD, "H") + color(GREY, " hang up"))
        bar2 = ("  " + color(BOLD, "W") + color(GREY, " set wake-up   ")
                + color(BOLD, "X") + color(GREY, " cancel wake-up   ")
                + color(BOLD, "M") + color(GREY, " message   ")
                + color(BOLD, "P") + color(GREY, " page all"))
        bar3 = ("  " + color(BOLD, "L") + color(GREY, " lights   ")
                + color(BOLD, "?") + color(GREY, " help   ")
                + color(BOLD, "Q") + color(GREY, " quit"))
        lines.append(bar1)
        lines.append(bar2)
        lines.append(bar3)
    msg = sess.get("msg", "")
    if msg and sess.get("msg_until", 0) > now:
        lines.append("  " + color(CYAN, "› " + msg))
    else:
        lines.append("")
    return center(lines, width, height)


# ── Session / input handling ─────────────────────────────────────────────────── #
def _label_for(rooms: list, ext: str) -> str:
    for r in rooms:
        if r["ext"] == ext:
            return r.get("label") or ext
    return ext


def apply_key(sess: dict, key: str, board: Board, log) -> None:
    """Mutate session state / fire AMI actions for a keypress. Pure-ish: the
    only side effects are the explicit ami.* calls."""
    snap = board.get()
    rooms = snap.get("rooms", [])
    n = len(rooms)

    def flash(m):
        sess["msg"] = m
        sess["msg_until"] = time.time() + 4

    # Help overlay: any key dismisses it (works even with no rooms).
    if sess.get("mode") == "help":
        sess["mode"] = "normal"
        return

    # Page-all confirm — a y/n gate, intercepted before the nav block (like the
    # wake-up field) so the arrows/hotkeys can't leak through. Works even with no
    # rooms (an empty page is just a no-op).
    if sess.get("mode") == "pageconfirm":
        if key in ("y", "Y", "enter"):
            sess["mode"] = "normal"
            exts = [r["ext"] for r in rooms if r.get("registered")]
            ok = False
            try:
                ok = ami.page_all(exts)
            except (ami.AMIError, OSError) as exc:
                log(f"page_all failed: {exc}")
            flash("Paging all rooms into the intercom…" if ok else "Page failed")
            return
        if key in ("n", "N", "esc"):
            sess["mode"] = "normal"
            flash("Page cancelled")
            return
        return  # ignore other keys while confirming

    # Lights view — its own mode, driven entirely off the list fetched into
    # sess['lights'] (render stays pure; the only I/O is ha_client.set_light /
    # re-fetch here). Intercepted before nav so j/k drive the light cursor.
    if sess.get("mode") == "lights":
        lights = sess.get("lights", []) or []
        ln = len(lights)
        if key == "esc":
            sess["mode"] = "normal"
            for k in ("lights", "lsel"):
                sess.pop(k, None)
            return
        if ln == 0:
            if key == "r" and ha_client is not None:
                try:
                    sess["lights"] = ha_client.get_lights() or []
                except Exception as exc:
                    log(f"lights refresh failed: {exc}")
                    sess["lights"] = []
                sess["lsel"] = 0
            return
        sess["lsel"] = max(0, min(sess.get("lsel", 0), ln - 1))
        if key in ("up", "k"):
            sess["lsel"] = max(0, sess["lsel"] - 1)
            return
        if key in ("down", "j"):
            sess["lsel"] = min(ln - 1, sess["lsel"] + 1)
            return
        if key in ("enter", " "):
            li = lights[sess["lsel"]]
            is_on = str(li.get("state", "")).lower() == "on"
            ok = False
            if ha_client is not None:
                try:
                    ok = ha_client.set_light(li.get("entity_id", ""), not is_on)
                except Exception as exc:
                    log(f"set_light {li.get('entity_id','')} failed: {exc}")
            if ok:
                li["state"] = "off" if is_on else "on"  # optimistic flip
                flash(f"{li.get('name','Light')} → {'off' if is_on else 'on'}")
            else:
                flash("Light control failed")
            return
        if key == "r":
            if ha_client is not None:
                try:
                    sess["lights"] = ha_client.get_lights() or []
                except Exception as exc:
                    log(f"lights refresh failed: {exc}")
                    sess["lights"] = []
            sess["lsel"] = max(0, min(sess.get("lsel", 0), max(0, len(sess.get("lights", [])) - 1)))
            flash("Lights refreshed")
            return
        return  # ignore other keys in the lights view

    if n == 0:
        return
    sess["sel"] = max(0, min(sess.get("sel", 0), n - 1))

    # Wake-up text entry — the TUI's one typed field. Capture EVERY key as text
    # or editing, so the room hotkeys (r/c/h/x) and nav (j/k) are typed
    # literally. Must precede the nav block below (unlike connect mode, which
    # deliberately reuses the arrows).
    if sess.get("mode") == "wakeup":
        if key == "esc":
            sess["mode"] = "normal"
            for k in ("wakeup_ext", "wakeup_label", "wakeup_buf"):
                sess.pop(k, None)
            flash("Wake-up cancelled")
            return
        if key == "backspace":
            sess["wakeup_buf"] = sess.get("wakeup_buf", "")[:-1]
            return
        if key == "enter":
            buf = sess.get("wakeup_buf", "")
            hhmm = timeparse.parse(buf) if timeparse is not None else None
            if hhmm is None:
                flash('Didn\'t catch a time — try 7:30 or "quarter past six"')
                return  # stay in wakeup mode, buffer intact so they can fix it
            ext = sess.get("wakeup_ext", "")
            label = sess.get("wakeup_label", ext)
            try:
                entry = wakeup_store.set_wakeup(ext, hhmm)
            except Exception as exc:
                log(f"wakeup set {ext} failed: {exc}")
                sess["mode"] = "normal"
                for k in ("wakeup_ext", "wakeup_label", "wakeup_buf"):
                    sess.pop(k, None)
                flash("Set wake-up failed")
                return
            tgt = entry.get("target_epoch", time.time())
            flash(f"Wake-up for {label} at {fmt12(hhmm)} {wakeup_when(tgt, time.time())}")
            sess["mode"] = "normal"
            for k in ("wakeup_ext", "wakeup_label", "wakeup_buf"):
                sess.pop(k, None)
            return
        if len(key) == 1 and 32 <= ord(key) < 127:
            if len(sess.get("wakeup_buf", "")) < 32:  # bound the buffer (flood guard)
                sess["wakeup_buf"] = sess.get("wakeup_buf", "") + key
            return
        return  # ignore arrows / unknown keys while typing

    if key in ("up", "k"):
        sess["sel"] = (sess["sel"] - 1) % n
        return
    if key in ("down", "j"):
        sess["sel"] = (sess["sel"] + 1) % n
        return

    room = rooms[sess["sel"]]

    if sess.get("mode") == "connect":
        if key == "esc":
            sess["mode"] = "normal"
            sess.pop("connect_from", None)
            flash("Connect cancelled")
            return
        if key == "enter":
            a = sess.get("connect_from")
            b = room["ext"]
            sess["mode"] = "normal"
            sess.pop("connect_from", None)
            if a == b:
                flash("Pick a different room to connect to")
                return
            ok = False
            try:
                ok = ami.connect_extensions(a, b, {r["ext"] for r in rooms})
            except (ami.AMIError, OSError) as exc:
                log(f"connect {a}->{b} failed: {exc}")
            flash(f"Connecting {_label_for(rooms, a)} ↔ {room['label']}…" if ok else "Connect failed")
            return
        return  # ignore other keys while choosing the target

    if key in ("r", "R"):
        if not room["registered"]:
            flash(f"{room['label']} is offline")
            return
        ok = False
        try:
            ok = ami.ring_extension(room["ext"])
        except (ami.AMIError, OSError) as exc:
            log(f"ring {room['ext']} failed: {exc}")
        flash(f"Ringing {room['label']}…" if ok else "Ring failed")
        return
    if key in ("c", "C"):
        sess["mode"] = "connect"
        sess["connect_from"] = room["ext"]
        sess["connect_from_label"] = room["label"]
        flash(f"Connect {room['label']} to…")
        return
    if key in ("h", "H"):
        if not room.get("channel"):
            flash(f"{room['label']} has no active call")
            return
        ok = False
        try:
            ok = ami.hangup_channel(room["channel"])
        except (ami.AMIError, OSError) as exc:
            log(f"hangup {room['ext']} failed: {exc}")
        flash(f"Hung up {room['label']}" if ok else "Hang up failed")
        return
    if key in ("x", "X"):
        if wakeup_store is None:
            return
        try:
            cancelled = wakeup_store.cancel(room["ext"])
        except Exception as exc:
            log(f"wakeup cancel {room['ext']} failed: {exc}")
            flash("Wake-up cancel failed")
            return
        flash(f"Cancelled wake-up for {room['label']}" if cancelled
              else f"{room['label']} has no wake-up set")
        return
    if key in ("w", "W"):
        if wakeup_store is None or timeparse is None:
            return  # can't store or can't parse a time — don't enter a dead mode
        # A wake-up can be set for an OFFLINE room (the scheduler defers delivery
        # until it's back), so — unlike ring — W is not gated on registration.
        sess["mode"] = "wakeup"
        sess["wakeup_ext"] = room["ext"]
        sess["wakeup_label"] = room["label"]
        seed = ""
        try:  # editing an existing wake-up pre-fills its time (set_wakeup replaces)
            existing = wakeup_store.get(room["ext"])
            if existing:
                seed = existing.get("hhmm", "")
        except Exception:
            seed = ""
        sess["wakeup_buf"] = seed
        return
    if key in ("m", "M"):
        # Toggle the room's message-waiting indicator (stutter tone) with an
        # "optimistic CLEAR, honest SET" rule: only persist the badge for a SET
        # when Asterisk actually accepted it, so the ✉ never claims a stutter
        # tone is playing when the tone never started. A CLEAR always drops the
        # badge regardless.
        if mwi_store is None:
            return
        ext = room["ext"]
        try:
            was_set = mwi_store.is_set(ext)
        except Exception as exc:
            log(f"mwi read {ext} failed: {exc}")
            flash("Message toggle failed")
            return
        new_on = not was_set
        try:
            ok = ami.set_mwi(ext, new_on)
        except (ami.AMIError, OSError) as exc:
            log(f"set_mwi {ext} failed: {exc}")
            ok = False
        # Honest SET: a refused SET leaves the badge off and reports the failure.
        if new_on and not ok:
            flash("Message set failed (PBX)")
            return
        try:
            mwi_store.set_flag(ext, new_on)
        except Exception as exc:
            log(f"mwi store {ext} failed: {exc}")
            flash("Message toggle failed")
            return
        flash(f"Message set for {room['label']} — stutter tone" if new_on
              else f"Cleared message for {room['label']}")
        return
    if key in ("p", "P"):
        sess["mode"] = "pageconfirm"
        flash("Page all rooms? Press Y to confirm.")
        return
    if key in ("l", "L"):
        if ha_client is None:
            flash("Home Assistant unavailable")
            return
        try:
            lights = ha_client.get_lights() or []
        except Exception as exc:
            log(f"get_lights failed: {exc}")
            lights = []
        if not lights:
            flash("Home Assistant unavailable")
            return
        sess["mode"] = "lights"
        sess["lights"] = lights
        sess["lsel"] = 0
        return
    if key == "?":
        sess["mode"] = "help"
        return


def _frame(lines: list[str]) -> str:
    body = HIDE_CURSOR + CURSOR_HOME
    for idx, ln in enumerate(lines):
        body += ln + CLEAR_EOL
        if idx < len(lines) - 1:
            body += "\r\n"
    return body + CLEAR_BELOW


def is_quit(key: str, mode: str) -> bool:
    """Whether a keypress should quit the session. Ctrl-C is always a hard exit;
    q/Q quit from any screen EXCEPT the wake-up text field, where a literal 'q'
    (as in "quarter past six") must be typed, not treated as a quit. (Connect and
    help are not text fields, so q/Q still quit there, matching the help card.)"""
    return key == "ctrl-c" or (key in ("q", "Q") and mode != "wakeup")


def serve_session(sock: socket.socket, board: Board, stop: threading.Event, log) -> None:
    sock.sendall(bytes([IAC, WILL, OPT_ECHO, IAC, WILL, OPT_SGA, IAC, DO, OPT_SGA, IAC, DO, OPT_NAWS]))
    sock.sendall((ENTER_ALT + HIDE_CURSOR + CLEAR_SCREEN).encode())
    sess = {"sel": 0, "mode": "normal", "msg": "", "msg_until": 0.0, "w": 80, "h": 24}
    inbuf = b""
    last = None
    sock.settimeout(1.0)

    def draw(force=False):
        nonlocal last
        frame = _frame(render(board.get(), sess, time.time()))
        h = hash(frame)
        if not force and h == last:
            return
        last = h
        sock.sendall(frame.encode("utf-8", "replace"))

    idle_deadline = time.time() + IDLE_SECONDS
    try:
        draw(force=True)
        while not stop.is_set():
            try:
                data = sock.recv(1024)
                if not data:
                    break
            except socket.timeout:
                if time.time() > idle_deadline:
                    break  # idle too long — reclaim the session
                draw()
                continue
            idle_deadline = time.time() + IDLE_SECONDS
            inbuf += data
            # On overflow, drop the whole buffer rather than keep an arbitrary
            # tail — a flood is already malformed, and a tail slice could desync
            # the telnet parser mid-escape.
            if len(inbuf) > 4096:
                inbuf = b""
            events, inbuf = parse_input(inbuf)
            quit_ = False
            for ev in events:
                if ev[0] == "naws":
                    _, w, h = ev
                    if w > 0 and h > 0:
                        sess["w"] = max(60, min(200, w))
                        sess["h"] = max(16, min(80, h))
                elif is_quit(ev[1], sess.get("mode", "normal")):
                    quit_ = True
                    break
                else:
                    apply_key(sess, ev[1], board, log)
            if quit_:
                break
            draw()
    except OSError:
        pass
    finally:
        try:
            sock.sendall((SHOW_CURSOR + RESET + EXIT_ALT + "\r\n").encode())
        except OSError:
            pass


def main() -> None:
    port = int(os.environ.get("CONSOLE_PORT", "2300"))
    host = os.environ.get("CONSOLE_HOST", "0.0.0.0")

    def log(msg):
        print(f"[switchboard-console] {msg}", flush=True)

    board = Board()
    stop = threading.Event()
    threading.Thread(target=poller_loop, args=(board, stop, log), daemon=True).start()

    # Cap concurrent sessions — this is an unauthenticated LAN listener.
    slots = threading.BoundedSemaphore(MAX_SESSIONS)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            if not slots.acquire(blocking=False):
                try:
                    self.request.sendall(b"\r\nSwitchboard console busy (too many sessions). Try later.\r\n")
                except OSError:
                    pass
                return
            log(f"client connected from {self.client_address[0]}")
            try:
                serve_session(self.request, board, stop, log)
            except Exception as exc:  # never let one session take down the server
                log(f"session error: {exc}")
            finally:
                slots.release()
                log("client disconnected")

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server((host, port), Handler)

    def shutdown(*_):
        log("shutting down")
        stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"operator console listening on {host}:{port}")
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
