"""The operator board must fit the terminal, and the keys must never be the
first thing it gives up.

    python3 -m pytest switchboard/tests/test_console_board_scroll.py

★ THE DEFECT. `center()` clamps the rendered page with `out[:h]` — it truncates
from the BOTTOM. Three lists on the board grow with the household (the roster,
the active calls, the wake-ups), and nothing bounded any of them, so past the
terminal's height the surplus was silently clipped. The bottom is where the key
bar lives, so the first thing an operator lost was the list of what they could
press — on a 24-row window, ten rooms plus one wake-up is already over.

v0.94.4 gave the LIGHTS list a scrolling viewport after exactly this was
reported from live use ("no way to scroll and truncates at bottom"). The board
itself, which is the screen you are on when you arrive, never got one.
"""
import os
import re
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

os.environ.setdefault("SWITCHBOARD_WAKEUPS",
                      os.path.join(tempfile.mkdtemp(), "wakeups.json"))

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "console"


def _console():
    saved = list(sys.path)
    sys.path.insert(0, str(CONSOLE))
    try:
        return SourceFileLoader("console_scroll", str(CONSOLE / "console.py")).load_module()
    finally:
        sys.path[:] = saved


c = _console()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(lines):
    return [ANSI.sub("", ln).rstrip() for ln in lines]


def _rooms(n):
    return [{"ext": str(10 + i), "label": f"Room {i}", "registered": True,
             "rtt": 2500, "call_state": None, "contact_status": "Reachable"}
            for i in range(1, n + 1)]


def _board(n_rooms=10, n_calls=1, n_wakes=2):
    return {
        "rooms": _rooms(n_rooms),
        "calls": [{"kind": "outside", "detail": f"Call {i}", "state": "Up",
                   "duration": "00:01:12", "codec": "ulaw"} for i in range(n_calls)],
        "wakeups": [{"label": f"Room {i}", "hhmm": "06:15"} for i in range(n_wakes)],
        # A polled board: ts non-zero, or render() shows the connecting notice
        # instead of the roster (v0.100.5).
        "ami_ok": True, "ts": 1_789_000_000.0, "trunk_reg": "Registered", "stt": "up",
    }


def _render(board, h=24, w=80, sel=0, mode="board", sess=None):
    sess = sess if sess is not None else {}
    sess.update({"w": w, "h": h, "sel": sel, "mode": mode})
    return _plain(c.render(board, sess, 1_789_000_000.0)), sess


KEY_BARS = ("R ring", "P page all", "Q quit")


# --------------------------------------------------------------------------- #
# 1. ★ The footer is never the thing that gets cut.
# --------------------------------------------------------------------------- #
def test_the_key_bar_survives_every_terminal_height():
    """The operator loses the ability to use the console before they lose a row
    of a list. That is backwards, and it was the behaviour."""
    missing = []
    for h in range(10, 41):
        out, _ = _render(_board(), h=h, sel=0)
        text = "\n".join(out)
        gone = [k for k in KEY_BARS if k not in text]
        if gone:
            missing.append((h, gone))
    assert not missing, f"the key bar was truncated at these heights: {missing}"


def test_a_modal_prompt_survives_too():
    """"type a time · Enter sets · Esc cancels" is not decoration — without it
    the operator is looking at a cursor with no way to know what to do."""
    for h in (11, 14, 18, 24):
        out, _ = _render(_board(), h=h, mode="wakeup",
                         sess={"wakeup_label": "Master Bed", "wakeup_buf": "615"})
        text = "\n".join(out)
        assert "SET WAKE-UP" in text, f"h={h}"
        assert "Enter sets" in text, f"h={h}: the instructions were truncated"


def test_the_page_never_exceeds_the_terminal():
    for h in range(10, 41):
        out, _ = _render(_board(n_rooms=24, n_calls=6, n_wakes=6), h=h)
        assert len(out) <= h, f"h={h} rendered {len(out)} rows"


# --------------------------------------------------------------------------- #
# 2. The roster scrolls, and the selection is always on screen.
# --------------------------------------------------------------------------- #
def _selected_row(out):
    return next((ln for ln in out if ln.lstrip().startswith("▸")), None)


def test_the_selection_is_visible_wherever_it_is():
    """★ The point of a viewport. Walking the cursor down a roster taller than
    the terminal must never leave the operator looking at rows they are not on."""
    board = _board(n_rooms=14)
    sess = {}
    for sel in range(14):
        out, sess = _render(board, h=20, sel=sel, sess=sess)
        row = _selected_row(out)
        assert row is not None, f"sel={sel}: the cursor is off screen"
        assert f"Room {sel + 1} " in row + " ", f"sel={sel}: showing {row!r}"


def test_walking_back_up_scrolls_back():
    """A viewport that only ever advances strands the operator at the bottom."""
    board = _board(n_rooms=14)
    sess = {}
    for sel in list(range(14)) + list(range(12, -1, -1)):
        out, sess = _render(board, h=20, sel=sel, sess=sess)
        assert _selected_row(out) is not None, f"sel={sel} scrolled off"


def test_it_says_how_many_rooms_are_out_of_view():
    """A list that just stops reads as a complete list — the defect one level
    down, and the reason the lights list counts too."""
    out, _ = _render(_board(n_rooms=14), h=20, sel=0)
    assert any("more below" in ln for ln in out), out
    out, _ = _render(_board(n_rooms=14), h=20, sel=13)
    assert any("more above" in ln for ln in out), out


def test_the_counts_are_never_negative_or_overstated():
    """An earlier version of the lights viewport printed "↓ -1 below" by
    subtracting a window clamped against a different size."""
    for n in range(1, 26):
        for h in (11, 14, 18, 24, 30):
            for sel in (0, n // 2, n - 1):
                out, _ = _render(_board(n_rooms=n), h=h, sel=sel)
                for ln in out:
                    m = re.search(r"↑ (-?\d+) more above", ln)
                    if m:
                        assert 0 < int(m.group(1)) < n, (n, h, sel, ln)
                    m = re.search(r"↓ (-?\d+) more below", ln)
                    if m:
                        assert 0 < int(m.group(1)) < n, (n, h, sel, ln)


def test_a_roster_that_fits_is_not_scrolled_and_says_nothing():
    """No note, no viewport, no behaviour change for the ordinary case."""
    out, sess = _render(_board(n_rooms=5), h=30, sel=0)
    assert not any("more below" in ln or "more above" in ln for ln in out)
    assert sess["rtop"] == 0
    assert sum(1 for ln in out if re.search(r"^\s+[▸ ]\s*1\d\s", ln)) == 5


# --------------------------------------------------------------------------- #
# 3. What the calls and wake-up lists give up, and in what order.
# --------------------------------------------------------------------------- #
def test_the_roster_keeps_a_workable_window_before_the_lists_are_cut():
    """A one-row porthole is not a roster. The lists give up rows first.

    ★ The bound is written out as 3, not as `c.ROSTER_MIN_ROWS`. Comparing
    against the constant under test passes for any value of it — setting it to 0
    satisfied this assertion while removing the guarantee entirely. A test whose
    threshold is supplied by its subject checks only self-consistency.
    """
    assert c.ROSTER_MIN_ROWS == 3
    out, _ = _render(_board(n_rooms=14, n_calls=3, n_wakes=4), h=20, sel=0)
    rows = [ln for ln in out if re.search(r"^\s+[▸ ]\s*\d\d\s+Room", ln)]
    assert len(rows) >= 3, (len(rows), out)


def test_a_collapsed_section_still_says_it_exists():
    """Dropping the wake-ups silently would tell the operator there are none,
    which is worse than telling them there are two they cannot see."""
    out, _ = _render(_board(n_rooms=18, n_calls=2, n_wakes=3), h=14, sel=0)
    text = "\n".join(out)
    assert "3 wake-ups" in text, text
    assert "2 calls" in text, text


def test_a_capped_list_names_what_it_hides():
    out, _ = _render(_board(n_rooms=10, n_calls=8, n_wakes=1), h=18, sel=0)
    text = "\n".join(out)
    assert ("more calls" in text) or ("8 calls" in text), text


def test_an_empty_call_list_still_shows_the_heading():
    """"— none —" is information. Collapsing it to a count of zero is not."""
    out, _ = _render(_board(n_rooms=4, n_calls=0, n_wakes=0), h=30)
    text = "\n".join(out)
    assert "ACTIVE CALLS" in text and "— none —" in text


# --------------------------------------------------------------------------- #
# 4. The shared viewport rule.
# --------------------------------------------------------------------------- #
def test_the_scroll_rule_is_shared_with_the_lights_list():
    """It took three attempts to get right there. The roster has the identical
    problem and no business solving it a second time."""
    src = (CONSOLE / "console.py").read_text()
    code = re.sub(r'"""(?:.|\n)*?"""', "",
                  "\n".join(l.split("#", 1)[0] for l in src.split("\n")))
    assert code.count("def _scroll_top(") == 1
    assert code.count("_scroll_top(") >= 3, (
        "both the lights list and the roster must call the shared helper")


def test_the_scroll_rule_clamps_at_both_ends():
    """Reused against a smaller window than it was computed for, it must not
    return a negative or past-the-end offset."""
    assert c._scroll_top(10, 0, 99, 4) == 0
    assert c._scroll_top(10, 9, 0, 4) == 6
    assert c._scroll_top(10, 5, -5, 4) == 2
    assert c._scroll_top(3, 0, 0, 10) == 0
    assert c._scroll_top(0, 0, 5, 4) == 0
    assert c._scroll_top(10, 4, 4, 0) == 4          # size floored at 1
    # ★ THE CASE THE FINAL CLAMP EXISTS FOR, and the only one that needs it: a
    # selection past the end of the list. Every other input above is already
    # clamped on the way in, so dropping the trailing clamp changed nothing and
    # this rule went untested. `sel` outliving its list is not hypothetical —
    # `sess["sel"]` persists across redraws and a room can leave the config.
    assert c._scroll_top(10, 50, 0, 4) == 6, "a stale selection scrolled past the end"
    assert c._scroll_top(10, 50, 0, 20) == 0


def test_the_selection_stays_put_when_it_is_already_in_view():
    """Smallest nudge: the window must not jump on every keystroke."""
    assert c._scroll_top(20, 7, 5, 5) == 5
    assert c._scroll_top(20, 4, 5, 5) == 4          # one above -> scroll up one
    assert c._scroll_top(20, 10, 5, 5) == 6         # one below -> scroll down one
