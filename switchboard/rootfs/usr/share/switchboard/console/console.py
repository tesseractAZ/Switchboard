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
try:
    import stthealth  # noqa: E402  (resident whisper-server probe; webui/ on sys.path)
except ImportError:  # pragma: no cover
    stthealth = None

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
def load_options() -> dict:
    """The full add-on options (rooms + trunk + feature toggles), or {}.

    Reads the post-overlay snapshot first (SWITCHBOARD_OPTIONS, written by
    init-switchboard) and FALLS BACK to /data/options.json. The fallback is not
    optional: if the snapshot is ever missing, returning {} here would empty the
    board of every room."""
    for path in (OPTIONS_PATH, "/data/options.json"):
        try:
            with open(path) as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data:
                return data
        except (OSError, ValueError):
            continue
    return {}


def load_rooms_cfg(opts: dict | None = None) -> dict:
    if opts is None:
        opts = load_options()
    return {str(r.get("ext")): r for r in (opts.get("rooms") or [])}


def _rtt_ms(us) -> float | None:
    """Contact round-trip µs (AMI RoundtripUsec, a raw string) → ms, 1 decimal.
    None for missing/''/non-numeric/negative (mirrors rtpmon.poller.rtt_ms)."""
    try:
        v = float(us)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v != v:  # non-positive or NaN
        return None
    return round(v / 1000.0, 1)


def _fill_row(left: str, right: str, width: int, min_gap: int = 2) -> str:
    """Left text + a RIGHT-aligned right text within `width` visible columns
    (ANSI-aware via vis_width, so color codes don't skew the math). If they'd
    collide (narrow terminal), drop the right detail rather than show a fragment."""
    if not right:
        return left
    pad = width - vis_width(left) - vis_width(right)
    if pad < min_gap:
        return left
    return left + (" " * pad) + right


# The trunk registration + resident-STT health change slowly and each costs an
# extra AMI login / loopback probe, so refresh them on their own throttle instead
# of every ~3s board poll. Module-level (the poller is a single thread).
_SLOW_TTL = 20.0
_slow_cache: dict = {"ts": -1e9, "trunk_reg": "", "stt": ""}


def _slow_signals(opts: dict) -> tuple[str, str]:
    """(trunk_registration, stt_state), throttled to _SLOW_TTL. trunk_reg is ''
    when the trunk is disabled; stt is stthealth.status (up/down/disabled/'')."""
    now = time.time()
    if now - _slow_cache["ts"] < _SLOW_TTL:
        return _slow_cache["trunk_reg"], _slow_cache["stt"]
    trunk_reg = ""
    if (opts.get("trunk") or {}).get("enabled"):
        try:
            trunk_reg = (ami.get_registrations().get("trunk-reg") or {}).get("status") or "Unknown"
        except (ami.AMIError, OSError):
            trunk_reg = ""
    stt = stthealth.status(opts) if stthealth is not None else ""
    _slow_cache.update(ts=now, trunk_reg=trunk_reg, stt=stt)
    return trunk_reg, stt


_CODEC_NAMES = {"ulaw": "µ-law", "alaw": "A-law", "g722": "G.722", "g729": "G.729",
                "g723": "G.723", "g726": "G.726", "opus": "Opus", "ilbc": "iLBC", "slin16": "L16"}


def _codec_label(codec: str) -> str:
    """Pretty per-call codec for the board: "ulaw" -> "µ-law"; "g722/ulaw" (a
    transcode across the two legs) -> "G.722/µ-law"; "" -> "" (no live leg yet)."""
    codec = str(codec or "")
    return "/".join(_CODEC_NAMES.get(x, x) for x in codec.split("/")) if codec else ""


def build_board(rooms_cfg: dict, opts: dict | None = None) -> dict:
    """One AMI poll → a board dict the renderer consumes. Pure given the AMI
    helpers; isolated here so the renderer/tests never touch a socket. `opts` (the
    full options) enables the slow trunk-registration + STT-health signals; omit
    it (tests) and those board keys are simply absent and the renderer skips them."""
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
    # ext -> the FAR party's channel, so Transfer redirects the outside/answered
    # leg (not a sibling ringing handset in a ring-group) — needs the room set.
    peer_by_ext = ami.peer_channels_by_ext(channels, rooms_by_ext)

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
            "codec": call.get("codec", ""),
            "channel": chan_by_ext.get(name, ""), "peer_channel": peer_by_ext.get(name, ""),
            "rtt": contact.get("rtt", ""), "contact_status": contact.get("status", ""),
            "mwi": _mwi(name),
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
    trunk_reg, stt = _slow_signals(opts) if opts is not None else ("", "")
    return {"ami_ok": ami_ok, "rooms": rooms, "calls": summary["calls"],
            "wakeups": wakeups, "trunk_reg": trunk_reg, "stt": stt, "ts": time.time()}


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
    wide/fullwidth glyphs (our ☎️ / ⏰ emoji) as 2 and everything else — including
    'ambiguous' box/arrow glyphs, which terminals render narrow — as 1. Note ☎️
    counts 2 only WITH its U+FE0F variation selector; a bare ☎ is ambiguous → 1."""
    s = _ANSI_RE.sub("", s)
    return sum(0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in ("W", "F") else 1)
               for c in s)


def _truncate_visible(line: str, maxw: int) -> str:
    """Cut `line` to at most `maxw` visible columns, preserving ANSI SGR codes and
    East-Asian width, and re-appending a reset if a color was left open. Without
    this a line wider than the terminal (a long help row, the header, a modal
    prompt) wraps and garbles the frame on a narrow (e.g. 60-col) terminal."""
    if maxw <= 0:
        return ""
    if vis_width(line) <= maxw:
        return line
    out, width, i, n, had_sgr = [], 0, 0, len(line), False
    while i < n:
        m = _ANSI_RE.match(line, i)
        if m:
            out.append(m.group(0))
            had_sgr = True
            i = m.end()
            continue
        c = line[i]
        cw = 0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in ("W", "F") else 1)
        if width + cw > maxw:
            break
        out.append(c)
        width += cw
        i += 1
    s = "".join(out)
    if had_sgr and not s.endswith(RESET):
        s += RESET
    return s


def center(lines: list[str], w: int, h: int) -> list[str]:
    """Center a rendered block in a w×h terminal — the roster is small, so on a
    big screen it would otherwise sit jammed in the top-left. Indent every line by
    a common left margin and prepend blank lines, but never push content off-screen
    (no padding once it's as wide/tall as the terminal). Any line still wider than
    the terminal is truncated, and the whole frame is clamped to h rows, so a
    narrow/short window degrades gracefully instead of wrapping or scrolling the
    header off. Pure; unit-tested."""
    if not lines:
        return lines
    lines = [_truncate_visible(ln, w) for ln in lines]
    content_w = max((vis_width(ln) for ln in lines), default=0)
    left = max(0, (w - content_w) // 2)
    body = [(" " * left) + ln for ln in lines] if left else list(lines)
    top = max(0, (h - len(lines)) // 2)
    out = [""] * top + body
    return out[:h] if len(out) > h else out


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


class ClientGate:
    """Tracks connected console clients so the AMI poller does ZERO work while
    nobody is watching.

    The operator board only changes in response to calls/registrations, which no
    one can see unless a telnet/ttyd client is attached — and every poll is a
    full manager login + logoff, i.e. constant SecurityEvent log churn and
    SD-card writes on a Pi already prone to card wear. So with no client
    connected there is no reason to poll AMI at all. A ``threading.Condition``
    (not a bare ``Event``) closes the lost-wakeup race between the poller's
    ``count == 0`` check and its wait.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._n = 0
        self._stopped = False

    def enter(self) -> None:
        """A client connected — wake the poller so it refreshes for them."""
        with self._cond:
            self._n += 1
            self._cond.notify_all()

    def leave(self) -> None:
        with self._cond:
            self._n = max(0, self._n - 1)

    def wake(self) -> None:
        """Unblock a parked poller at shutdown so its thread exits cleanly."""
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def wait_active(self) -> bool:
        """Block until >=1 client is connected. Returns False iff woken to stop."""
        with self._cond:
            while self._n == 0 and not self._stopped:
                self._cond.wait()
            return not self._stopped


def poller_loop(board: Board, stop: threading.Event, gate: ClientGate, log) -> None:
    while not stop.is_set():
        # Idle (no telnet/ttyd client) -> park here with NO AMI traffic at all,
        # instead of logging in/out every POLL_SECONDS around the clock.
        if not gate.wait_active():
            break  # woken for shutdown
        try:
            opts = load_options()
            board.set(build_board(load_rooms_cfg(opts), opts))
        except Exception as exc:  # never let the poller die
            log(f"poll error: {exc}")
        stop.wait(POLL_SECONDS)


# ── Rendering (pure) ─────────────────────────────────────────────────────────── #
def _room_status(room: dict):
    """(glyph, color, text, peer_suffix) for a room row."""
    ds = (room.get("device_state") or "").lower()
    cs = (room.get("call_state") or "")
    peer = room.get("peer") or ""
    codec = _codec_label(room.get("codec", ""))
    suffix = (f"  ↔ {peer}" if peer else "") + (f"  · {codec}" if (peer and codec) else "")
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
        f" {BOLD}☎️ SWITCHBOARD OPERATOR — HELP{RESET}",
        rule,
        f"  {b('↑ ↓')} / {b('j k')}   Move the selection between rooms",
        f"  {b('R')}         Ring the selected room (a short test ring)",
        f"  {b('C')}         Connect — then pick another room and press Enter to patch",
        f"  {b('H')}         Hang up the selected room's active call",
        f"  {b('T')}         Transfer — hand the selected call's other party to a room",
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


def _scroll_top(n: int, sel: int, top, size: int) -> int:
    """Smallest nudge to `top` that keeps row `sel` inside a `size`-row window.

    Shared by the lights list and the room roster. It was written for the lights
    list, where getting it right took three attempts; the roster has exactly the
    same problem and no reason to solve it a second time.

    Clamped at both ends, so a window computed against one size and then reused
    against a smaller one cannot return a negative or past-the-end offset — an
    earlier version did, and printed "↓ -1 below".
    """
    size = max(1, size)
    t = max(0, min(int(top or 0), max(0, n - size)))
    if sel < t:
        t = sel
    elif sel >= t + size:
        t = sel - size + 1
    return max(0, min(t, max(0, n - size)))


def _lights_lines(sess: dict, width: int, height: int = 24) -> list[str]:
    """The `L` lights view — an area-grouped list of HA lights with a cursor.

    ★ THIS LIST SCROLLS, and before v0.94.4 it did not.

    Every light was rendered from the first one, and ``center()`` then clamped
    the frame to the terminal height — so with 34 lights across a dozen areas
    the tail was simply cut off, at ANY terminal size, and nothing could bring it
    back. The cursor moved past the fold (``lsel`` kept incrementing) but the
    viewport never followed, so the rows below were unreachable rather than
    merely off-screen. Reported from live use: "no way to scroll and truncates at
    bottom".

    The window is kept in ``sess['ltop']`` so it stays put between redraws
    instead of recentring on every frame, and it is nudged only far enough to
    keep the selection visible. When the window starts mid-area the area heading
    is repeated at the top, because a scrolled list whose group heading has
    scrolled away tells you what is on but not where it is.

    Pure: reads the list already fetched into sess['lights'] (render never does
    I/O), so it works the same in tests as it does live.
    """
    lights = sess.get("lights", []) or []
    lsel = max(0, min(sess.get("lsel", 0), max(0, len(lights) - 1)))
    rule = color(GREY, "─" * min(width, 72))
    on_n = sum(1 for li in lights if str(li.get("state", "")).lower() == "on")
    head_right = color(GREY, f"{on_n}/{len(lights)} on ")
    head = [f" {BOLD}💡 LIGHTS{RESET}   {head_right}", rule]
    foot = [rule,
            "  " + color(GREY, "[↑↓] select   ") + color(BOLD, "Enter") + color(GREY, " toggle   ")
            + color(BOLD, "r") + color(GREY, " refresh   ")
            + color(BOLD, "Esc") + color(GREY, " back   ") + color(BOLD, "Q") + color(GREY, " quit")]

    if not lights:
        return head + ["  " + color(GREY, "— no lights —")] + foot

    # Build the body once, remembering which row is which light and which area
    # heading is in force at each row (for the repeat-heading-when-scrolled rule).
    rows: list[tuple[str, int | None, str]] = []
    last_area = object()
    cur_area = ""
    for idx, li in enumerate(lights):
        area = li.get("area") or ""
        if area != last_area:
            last_area = area
            cur_area = area or "Unassigned"
            rows.append(("  " + color(BOLD, cur_area), None, cur_area))
        cursor = color(CYAN, "▸") if idx == lsel else " "
        is_on = str(li.get("state", "")).lower() == "on"
        badge = color(GREEN, "● on") if is_on else color(GREY, "○ off")
        name = (li.get("name") or li.get("entity_id") or "")[:28].ljust(28)
        rows.append((f"    {cursor} {name}  {badge}", idx, cur_area))

    # Room for the body: the chrome above and below, plus one line reserved for
    # the "more" indicator. Never less than one row, so a tiny terminal still
    # shows the selection rather than nothing.
    avail = max(1, height - len(head) - len(foot) - 1)
    if len(rows) <= avail:
        return head + [r[0] for r in rows] + foot

    sel_row = next((i for i, r in enumerate(rows) if r[1] == lsel), 0)

    def _window(size: int) -> int:
        """Smallest nudge to `ltop` that keeps the selection inside `size` rows."""
        return _scroll_top(len(rows), sel_row, sess.get("ltop", 0), size)

    # Two passes, because the continuation heading costs a row and whether it is
    # needed depends on where the window lands. A first version prepended the
    # heading and then truncated the body back to `avail` — which drops the
    # BOTTOM row, and after scrolling down the bottom row is the selection. The
    # cursor vanished at exactly the moment scrolling started to matter.
    top = _window(avail)
    need_head = top > 0 and rows[top][1] is not None
    if need_head:
        top = _window(avail - 1)
        need_head = top > 0 and rows[top][1] is not None
    body_n = avail - 1 if need_head else avail
    sess["ltop"] = top

    body = [r[0] for r in rows[top:top + body_n]]
    # If the window opens inside an area, say which area — its heading has
    # scrolled off, and a name with no room is half the information.
    if need_head:
        body[0:0] = ["  " + color(BOLD, rows[top][2]) + color(GREY, " (cont.)")]

    # Count LIGHTS, not rows. The rows include area headings, so a row count
    # tells the operator a number that matches nothing they can see. And clamp:
    # an earlier version subtracted a window that had already been clamped
    # against a different size and cheerfully printed "↓ -1 below".
    end = min(len(rows), top + body_n)
    hidden_above = sum(1 for r in rows[:top] if r[1] is not None)
    hidden_below = sum(1 for r in rows[end:] if r[1] is not None)
    if hidden_above and hidden_below:
        note = f"↑ {hidden_above} more above · ↓ {hidden_below} more below"
    elif hidden_above:
        note = f"↑ {hidden_above} more above"
    elif hidden_below:
        note = f"↓ {hidden_below} more below"
    else:
        note = ""
    return head + body + ([("  " + color(GREY, note))] if note else []) + foot


# The roster never shrinks below this. Fewer rows than this and the operator is
# scrolling a porthole: the selection is visible but nothing around it, so there
# is no way to see where in the house you are. The calls and wake-up lists give
# up their rows first.
ROSTER_MIN_ROWS = 3


def _capped(rows: list, cap: int, noun: str) -> list:
    """At most `cap` rows, and say what is missing rather than just stopping.

    A list that silently ends at the terminal's edge reads as a complete list.
    That is the whole defect this release is about, one level down.
    """
    cap = max(0, int(cap))
    if len(rows) <= cap:
        return list(rows)
    hidden = len(rows) - cap
    plural = noun if hidden == 1 else noun + "s"
    return list(rows[:cap]) + ["    " + color(GREY, f"… {hidden} more {plural}")]


def render(board: dict, sess: dict, now: float) -> list[str]:
    """Return the screen as a list of plain+ANSI lines. CLEAR_EOL per line means
    we don't pad to full width; content is kept within it."""
    width = sess.get("w", 80)
    height = sess.get("h", 24)
    if sess.get("mode") == "help":
        return center(_help_lines(width), width, height)
    if sess.get("mode") == "lights":
        lines = _lights_lines(sess, width, height)
        msg = sess.get("msg", "")
        lines.append("  " + color(CYAN, "› " + msg) if (msg and sess.get("msg_until", 0) > now) else "")
        return center(lines, width, height)
    rooms = board.get("rooms", [])
    calls = board.get("calls", [])
    sel = sess.get("sel", 0)
    # Full-screen board: the rules and rows span (almost) the whole terminal, and
    # live per-row detail is right-aligned to this edge. center() still gives a
    # 1-col margin + vertical centering. A wider label column uses the new room.
    bw = max(24, width - 2)
    label_w = 16 if width < 64 else 22
    rule = color(GREY, "─" * bw)
    lines: list[str] = []

    on_calls = sum(1 for r in rooms if r.get("call_state"))
    online = sum(1 for r in rooms if r.get("registered"))
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    head_left = f" {BOLD}☎️ SWITCHBOARD OPERATOR{RESET}"
    head_right = color(GREY, f"{online}/{len(rooms)} online · {on_calls} on call · {clock}")
    head = _fill_row(head_left, head_right, bw)
    lines.append(head if head != head_left else f"{head_left}   {head_right}")  # narrow: left-cluster
    lines.append(rule)

    # Slow signals (present only when build_board was given opts): trunk SIP
    # registration + resident-STT health, as a compact status line under the head.
    trunk_reg = board.get("trunk_reg", "")
    stt = board.get("stt", "")
    bits = []
    if trunk_reg:
        tcol = GREEN if trunk_reg == "Registered" else (GREY if trunk_reg == "Unknown" else RED)
        bits.append(color(GREY, "trunk ") + color(tcol, "● " + trunk_reg))
    if stt and stt != "disabled":
        bits.append(color(GREY, "STT ") + color(GREEN if stt == "up" else YELLOW,
                                                "● " + ("resident" if stt == "up" else "CLI fallback")))
    # Droppable: informative, but the last chrome to keep when the terminal is
    # too short to hold the roster, the calls and the keys at once.
    signal_lines = (["  " + color(GREY, "     ").join(bits), rule] if bits else [])

    # ★ THREE STATES, NOT TWO. `ami_ok` is False both before the first poll and
    # when the PBX is genuinely unreachable, and those read very differently to
    # somebody who has just opened the console.
    #
    # The poller is gated on a connected client (poller_loop parks on ClientGate
    # with no AMI traffic while nobody is watching), so EVERY new session renders
    # at least one frame before the first poll returns. That frame claimed
    # "Asterisk Manager unreachable" on a perfectly healthy system — observed on
    # 2026-09-11, where a 4 s capture showed 0/0 online and the warning while the
    # heartbeat 90 s earlier and 90 s later both read 9/9 reachable.
    #
    # `ts` is the discriminator and already exists: Board starts at 0.0 and
    # build_board stamps time.time() on every poll, including a failed one. So a
    # falsy `ts` means "not asked yet", which is not the same as "asked and the
    # answer was no" — the distinction this codebase keeps having to relearn.
    if not board.get("ts"):
        lines.append("  " + color(GREY, "Connecting to the PBX…"))
        lines.append(rule)
    elif not board.get("ami_ok", False):
        lines.append("  " + color(RED, "Asterisk Manager unreachable — the PBX may still be starting."))
        lines.append(rule)

    room_rows: list[str] = []
    for idx, r in enumerate(rooms):
        cursor = color(CYAN, "▸") if idx == sel else " "
        glyph, col, txt, suffix = _room_status(r)
        # Pad the PLAIN text first; wrap in color after, so ANSI codes don't
        # throw off the column width.
        ext = f"{r['ext']:<3}"
        label = (r.get("label") or r["ext"])[:label_w].ljust(label_w)
        status = color(col, f"{glyph} {txt}")
        # ✉ marker for a room with a message-waiting flag set. The glyph is a
        # narrow (width-1) char, so it lines up like any other trailing badge.
        mwi = color(YELLOW, "  ✉") if r.get("mwi") else ""
        left = f"  {cursor} {BOLD}{ext}{RESET}  {label}  {status}{color(GREY, suffix)}{mwi}"
        # Right-aligned live link detail: idle qualify RTT + a notable contact
        # status (anything other than the healthy "Reachable"). Registered only.
        detail = ""
        if r.get("registered"):
            parts = []
            rttms = _rtt_ms(r.get("rtt"))
            if rttms is not None:
                parts.append(f"{rttms:g} ms")
            cs = (r.get("contact_status") or "")
            if cs and cs.lower() not in ("reachable", "created", ""):
                parts.append(cs)
            detail = color(GREY, "  ·  ".join(parts)) if parts else ""
        row = _fill_row(left, detail, bw)
        room_rows.append(row)

    # ── Everything below the roster, built separately so the roster can be
    # given whatever height is left over rather than running off the bottom.
    call_rows: list[str] = []
    if not calls:
        call_rows.append("    " + color(GREY, "— none —"))
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
            call_rows.append(f"    {g}  {c.get('detail','')}   {tail}")

    wake_rows: list[str] = []
    for w in board.get("wakeups", []) or []:
        wake_rows.append(f"    ⏰  {w.get('label','')}   " + color(GREY, fmt12(w.get("hhmm", ""))))

    def _section(rows: list, cap: int, title: str, noun: str, glyph: str) -> list[str]:
        """A titled list, a capped list, or — at cap 0 — a single-line count.

        A heading plus a rule plus "… 2 more" is three rows spent saying nothing
        actionable. At that point the honest thing is one row that says the
        section exists and how big it is, and to give the rows to the roster.
        """
        if not rows:
            return []
        if cap <= 0:
            n = len(rows)
            return ["  " + color(GREY, f"{glyph} {n} {noun}{'' if n == 1 else 's'}")]
        return [rule, "  " + color(BOLD, title)] + _capped(rows, cap, noun)

    def _tail(cap_calls: int, cap_wakes: int) -> list[str]:
        out: list[str] = []
        if call_rows and calls:
            out += _section(call_rows, cap_calls, "ACTIVE CALLS", "call", "📞")
        else:                               # the "— none —" placeholder: 3 rows
            out += [rule, "  " + color(BOLD, "ACTIVE CALLS")] + call_rows
        out += _section(wake_rows, cap_wakes, "WAKE-UPS", "wake-up", "⏰")
        out.append(rule)
        return out

    footer: list[str] = []
    if sess.get("mode") == "wakeup":
        label = sess.get("wakeup_label", "?")
        buf = sess.get("wakeup_buf", "")
        hhmm = timeparse.parse(buf) if (timeparse is not None and buf) else None
        # Live preview: show the (forgiving) parser's reading before committing —
        # the same parse()+fmt12() the commit path uses, so they can't disagree.
        preview = color(GREY, f"   → {fmt12(hhmm)}") if hhmm else ""
        footer.append("  " + color(YELLOW, f"SET WAKE-UP {label}:  {buf}█") + preview)
        footer.append("  " + color(GREY, "type a time · Enter sets · Esc cancels · Backspace deletes"))
    elif sess.get("mode") == "connect":
        frm = sess.get("connect_from_label", "?")
        footer.append("  " + color(YELLOW, f"CONNECT {frm} → pick a room with ↑↓ and press Enter") + color(GREY, "  (Esc cancels)"))
    elif sess.get("mode") == "transfer":
        frm = sess.get("transfer_from_label", "?")
        footer.append("  " + color(YELLOW, f"TRANSFER {frm}'s call → pick a room with ↑↓ and press Enter") + color(GREY, "  (Esc cancels)"))
    elif sess.get("mode") == "pageconfirm":
        footer.append("  " + color(YELLOW, "PAGE ALL — ring every phone into the intercom?")
                     + color(GREY, "   ") + color(BOLD, "[Y]") + color(GREY, " yes    ")
                     + color(BOLD, "[N]") + color(GREY, " cancel"))
    else:
        bar1 = ("  " + color(GREY, "[↑↓] select   ") + color(BOLD, "R") + color(GREY, " ring   ")
                + color(BOLD, "C") + color(GREY, " connect   ") + color(BOLD, "H") + color(GREY, " hang up   ")
                + color(BOLD, "T") + color(GREY, " transfer"))
        bar2 = ("  " + color(BOLD, "W") + color(GREY, " set wake-up   ")
                + color(BOLD, "X") + color(GREY, " cancel wake-up   ")
                + color(BOLD, "M") + color(GREY, " message   ")
                + color(BOLD, "P") + color(GREY, " page all"))
        bar3 = ("  " + color(BOLD, "L") + color(GREY, " lights   ")
                + color(BOLD, "?") + color(GREY, " help   ")
                + color(BOLD, "Q") + color(GREY, " quit"))
        footer.append(bar1)
        footer.append(bar2)
        footer.append(bar3)
    msg = sess.get("msg", "")
    if msg and sess.get("msg_until", 0) > now:
        footer.append("  " + color(CYAN, "› " + msg))
    else:
        footer.append("")
    # ── Fit the roster to what is left. ────────────────────────────────────────
    #
    # ★ THE BOARD DID NOT SCROLL, AND center() TRUNCATES FROM THE BOTTOM.
    #
    # Everything here is fixed except three lists that all grow with the
    # household: the roster, the active calls and the wake-ups. Past the
    # terminal's height the surplus was simply clipped — and the bottom is where
    # the key bar lives, so the first thing an operator lost was the list of what
    # they could press. Ten rooms plus one wake-up already overflows 24 rows.
    #
    # What is given up, in order:
    #   1. the roster scrolls, following the selection — the lights list's rule,
    #      reused rather than re-derived — and says how many are out of view;
    #   2. the call and wake-up lists are capped, each saying how many it hides;
    #   3. the footer is never given up. It is how you operate the thing, and a
    #      modal prompt ("type a time · Enter sets") is load-bearing text.
    # Each rung gives up one more thing; the first that leaves the roster a
    # workable window wins. Recomputed rather than estimated — a section's rule
    # and heading disappear with its last row, so the arithmetic is not linear.
    # The footer is absent from this ladder on purpose.
    head_lines, tail_lines = lines, _tail(len(call_rows), len(wake_rows)) + footer
    for cap_wakes, cap_calls, signals in (
            (len(wake_rows), len(call_rows), True),    # everything
            (1,              len(call_rows), True),    # one wake-up, then a count
            (0,              len(call_rows), True),
            (0,              1,              True),    # one call, then a count
            (0,              0,              True),
            (0,              0,              False),   # finally, the signals line
    ):
        head_lines = lines + (signal_lines if signals else [])
        tail_lines = _tail(cap_calls, cap_wakes) + footer
        if height - len(head_lines) - len(tail_lines) >= ROSTER_MIN_ROWS + 1:
            break
    lines = head_lines

    avail = height - len(lines) - len(tail_lines)
    if avail < 1:
        avail = 1          # a terminal shorter than its own chrome; keep one row
    if len(room_rows) > avail:
        avail = max(1, avail - 1)                     # one row for the note
        top = _scroll_top(len(room_rows), sel, sess.get("rtop", 0), avail)
        sess["rtop"] = top
        above, below = top, max(0, len(room_rows) - (top + avail))
        note = (f"↑ {above} more above · ↓ {below} more below" if above and below
                else f"↑ {above} more above" if above
                else f"↓ {below} more below" if below else "")
        lines += room_rows[top:top + avail]
        if note:
            lines.append("  " + color(GREY, note))
    else:
        sess["rtop"] = 0
        lines += room_rows
    lines += tail_lines
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
            for k in ("lights", "lsel", "ltop"):
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

    if sess.get("mode") == "transfer":
        if key == "esc":
            sess["mode"] = "normal"
            for k in ("transfer_peer", "transfer_from", "transfer_from_label"):
                sess.pop(k, None)
            flash("Transfer cancelled")
            return
        if key == "enter":
            peer = sess.get("transfer_peer", "")
            src = sess.get("transfer_from")
            src_label = sess.get("transfer_from_label", src)
            target = room["ext"]
            sess["mode"] = "normal"
            for k in ("transfer_peer", "transfer_from", "transfer_from_label"):
                sess.pop(k, None)
            if target == src:
                flash("Pick a different room to transfer to")
                return
            if not room.get("registered"):
                # A redirect to an offline room would just drop the caller; refuse
                # it the same way ring/page already gate on registration.
                flash(f"{room['label']} is offline")
                return
            ok = False
            try:
                ok = ami.transfer_channel(peer, target, {r["ext"] for r in rooms})
            except (ami.AMIError, OSError) as exc:
                log(f"transfer {src}->{target} failed: {exc}")
            flash(f"Transferred {src_label}'s call → {room['label']}" if ok else "Transfer failed")
            return
        return  # ignore other keys while choosing the destination

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
    if key in ("t", "T"):
        # Blind-transfer the FAR party of this room's call to a room you pick.
        if not room.get("peer_channel"):
            flash(f"{room['label']} has no call to transfer")
            return
        sess["mode"] = "transfer"
        sess["transfer_peer"] = room["peer_channel"]
        sess["transfer_from"] = room["ext"]
        sess["transfer_from_label"] = room["label"]
        flash(f"Transfer {room['label']}'s call to… (pick a room, Enter)")
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
                # A lone trailing ESC can't be told apart from the start of an
                # escape sequence at parse time, so parse_input leaves it in inbuf.
                # If no continuation byte arrived within the recv timeout, treat it
                # as the Esc key — a real terminal's escape-timeout — so a single
                # Escape cancels a mode instead of dead-keying until the next press.
                if inbuf == b"\x1b":
                    inbuf = b""
                    apply_key(sess, "esc", board, log)
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
    # `or "2300"` (not just a default) so an EMPTY CONSOLE_PORT env — which crashed
    # the console with int('') on boot until s6 restarted it — falls back cleanly.
    try:
        port = int(os.environ.get("CONSOLE_PORT") or "2300")
    except ValueError:
        port = 2300
    host = os.environ.get("CONSOLE_HOST", "0.0.0.0")

    def log(msg):
        print(f"[switchboard-console] {msg}", flush=True)

    board = Board()
    stop = threading.Event()
    gate = ClientGate()
    threading.Thread(target=poller_loop, args=(board, stop, gate, log), daemon=True).start()

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
            gate.enter()  # start (or wake) the AMI poller for as long as we're attached
            try:
                serve_session(self.request, board, stop, log)
            except Exception as exc:  # never let one session take down the server
                log(f"session error: {exc}")
            finally:
                gate.leave()  # last client out -> poller parks, idle AMI churn stops
                slots.release()
                log("client disconnected")

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server((host, port), Handler)

    def shutdown(*_):
        log("shutting down")
        stop.set()
        gate.wake()  # unblock a parked (idle) poller so its thread exits cleanly
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
