"""The browser↔console bridge, driven directly — no web framework required.

    python3 -m pytest switchboard/tests/test_console_bridge.py

``webui/console_bridge.py`` is framework-free for exactly this reason: FastAPI
is not installed on the box these tests run on, so any rule living inside a
route decorator would be untestable. Every defect below is a real one the port
from ``console-web/server.py`` would otherwise have shipped.
"""
import asyncio
import socket
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

_PATH = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
         / "switchboard" / "webui" / "console_bridge.py")
_spec = spec_from_loader("switchboard_console_bridge",
                         SourceFileLoader("switchboard_console_bridge", str(_PATH)))
cb = module_from_spec(_spec)
_spec.loader.exec_module(cb)

IAC, SB, SE, NAWS = 255, 250, 240, 31


# --------------------------------------------------------------------------- #
# ★ The one that rings a phone.
# --------------------------------------------------------------------------- #
def test_a_resize_never_reaches_the_console_as_keystrokes():
    """★★★ THE DEFECT THIS MODULE EXISTS TO PREVENT.

    ``console-web/server.py`` sniffs ``data[:1] == b"{"``. That is right there,
    because its own frame decoder hands it bytes. Through a WebSocket it is not:
    xterm's ``onData`` and ``JSON.stringify`` both produce JS strings, which
    arrive as TEXT frames — and ``"{"[:1] == b"{"`` is ``False`` with NO
    exception. A faithful port therefore falls through to the keystroke path.

    What gets typed is ``{"type":"resize","cols":120,"rows":40}``. The console's
    ``parse_input`` maps every byte 32..126 to a key and ``apply_key`` binds
    ``r``→ring, ``c``→connect, ``h``→hang up, ``t``→transfer, ``p``→page-all,
    ``l``→lights. That string contains r, e, s, i, z, c, o, l, t, y and p — and
    the page sends it from inside ``ws.onopen``.

    So: opening the sidebar panel would ring a phone, then walk the board
    through connect, page and lights. This test is the reason the sniff
    normalises to bytes first.
    """
    # The TEXT-frame shape — a str, which is what the browser actually sends.
    out = cb.encode_for_console('{"type":"resize","cols":120,"rows":40}')
    assert out == bytes([IAC, SB, NAWS, 0, 120, 0, 40, IAC, SE]), out
    assert b"resize" not in out and b"cols" not in out
    # And the specific keys that would have fired.
    for ch in b"rchtpl":
        assert bytes([ch]) not in out, f"{chr(ch)!r} would have reached apply_key"


def test_the_second_resize_is_also_a_resize():
    """The live page sends one on open and another on every layout change; a
    stateful sniff that only worked the first time would look fine in a
    single-shot test and ring a phone on the second."""
    first = cb.encode_for_console('{"type":"resize","cols":80,"rows":24}')
    second = cb.encode_for_console('{"type":"resize","cols":80,"rows":24}')
    assert first == second == bytes([IAC, SB, NAWS, 0, 80, 0, 24, IAC, SE])


def test_a_real_keystroke_still_gets_through():
    """The guard above must not be so broad that the terminal goes deaf."""
    assert cb.encode_for_console("r") == b"r"
    assert cb.encode_for_console(b"\x1b[A") == b"\x1b[A"      # arrow key
    assert cb.encode_for_console("{") == b"{"                 # a literal brace
    assert cb.encode_for_console("{not json") == b"{not json"


def test_an_unknown_control_message_is_dropped_not_typed():
    """Stricter than the old server, deliberately: a JSON object we do not
    recognise is still a control message, and must never be typed."""
    assert cb.encode_for_console('{"type":"resizeX","cols":9}') == b""
    assert cb.encode_for_console('{"hello":"world"}') == b""


def test_a_malformed_resize_keeps_the_session():
    """naws() calls int(); a bad payload must not tear down a live terminal."""
    assert cb.encode_for_console('{"type":"resize","cols":"wide","rows":40}') == b""


def test_the_naws_size_can_never_collide_with_iac():
    """A dimension of 255 would put a bare 0xFF inside the subnegotiation that
    describes the window — corrupting the stream it is meant to size."""
    out = cb.encode_for_console('{"type":"resize","cols":255,"rows":300}')
    assert out == bytes([IAC, SB, NAWS, 0, 200, 0, 200, IAC, SE])
    assert 0xFF not in out[3:7]


# --------------------------------------------------------------------------- #
# Telnet byte hygiene.
# --------------------------------------------------------------------------- #
def test_the_text_leg_uses_utf8_so_0xff_is_unreachable_from_a_keypress():
    """★ latin-1 would make a typed 'ÿ' into a bare 0xFF the console eats as IAC.

    This is the whole reason the encoding is named explicitly. In UTF-8 'ÿ' is
    C3 BF and contains no 0xFF at all, so no keystroke can synthesise a telnet
    command; under latin-1 it is a single FF, the console's parser treats it as
    IAC, and the character typed AFTER it is swallowed as the command's operand.
    """
    out = cb.encode_for_console("ÿ5")
    assert out == b"\xc3\xbf5", out
    assert 0xFF not in out, "a keypress produced a bare IAC byte — latin-1?"
    assert out.endswith(b"5"), "the character after it was swallowed"


def test_a_literal_0xff_on_the_binary_leg_is_doubled():
    """The other half: a genuine 0xFF byte (a paste, a binary escape) must be
    escaped, or the console reads it as IAC and eats the next byte."""
    out = cb.encode_for_console(b"\xff5")
    assert out == b"\xff\xff5", out
    # ...and doubling is unconditional, not gated on a containment check.
    assert cb.escape_iac(b"\xff\xff") == b"\xff\xff\xff\xff"


def test_a_payload_without_0xff_is_byte_identical():
    """The doubling must not perturb ordinary input."""
    assert cb.encode_for_console(b"hello world") == b"hello world"


def test_the_telnet_preamble_is_the_client_half():
    p = cb.telnet_preamble()
    assert p == bytes([IAC, 253, 1, IAC, 253, 3, IAC, 251, 3, IAC, 251, 31])
    assert len(p) == 12


# --------------------------------------------------------------------------- #
# Console → browser.
# --------------------------------------------------------------------------- #
def test_console_output_is_never_decoded_per_chunk():
    """★ A per-chunk ``.decode()`` corrupts the operator's screen.

    The board renders ☎ 💡 ↔ ─ ▸ ● and a colored frame comfortably exceeds one
    4096-byte read, so a multi-byte character straddles the boundary on
    essentially every draw. Decoding each chunk raises UnicodeDecodeError
    (killing the session) or, with errors="replace", burns U+FFFD into the
    screen at random offsets.

    The fixture asserts its OWN premise: at least one chunk must be invalid
    UTF-8 on its own, or the test would pass on an all-ASCII board and prove
    nothing.
    """
    body = ("┌" + "─" * 78 + "┐\n"
            + "".join(f"│ ☎️  Room {i:02d}  💡 on   ↔ 12.3 ms  ● ✉  µ \n" for i in range(60))
            + "└" + "─" * 78 + "┘\n")
    # Pad so a 3-byte box-drawing character DETERMINISTICALLY straddles the
    # 4096-byte read boundary. Left to chance, a fixture can happen to align and
    # the test then passes for the wrong reason.
    pad = (4095 - len("┌".encode("utf-8"))) % 4096
    raw = (b"." * pad) + body.encode("utf-8")
    assert len(raw) > 4096, f"fixture is only {len(raw)} bytes; it must span a read"

    chunks = [raw[i:i + 4096] for i in range(0, len(raw), 4096)]
    assert len(chunks) > 1
    bad = [c for c in chunks if not _is_utf8(c)]
    assert bad, ("no chunk splits a codepoint — this fixture cannot detect the "
                 "defect it exists for")

    out, carry = b"", b""
    for c in chunks:
        clean, carry = cb.strip_console_output(c, carry)
        out += clean
    assert out == raw, "the bridge did not reproduce the console bytes exactly"


def _is_utf8(b: bytes) -> bool:
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def test_the_carry_buffer_is_threaded_back():
    """``clean, _ = strip(...)`` silently drops a trailing partial IAC sequence,
    which then reappears as stray bytes on the operator's screen."""
    import inspect
    src = inspect.getsource(cb.run_bridge)
    assert "clean, carry = strip_console_output(chunk, carry)" in src, (
        "console_to_queue must thread the carry buffer back in")


# --------------------------------------------------------------------------- #
# Wiring the port, and failing safely.
# --------------------------------------------------------------------------- #
def test_the_console_port_is_2300_on_loopback():
    """A value assertion on the imported module, not a grep of the source."""
    assert cb.CONSOLE_PORT == 2300
    assert cb.CONSOLE_HOST == "127.0.0.1"


def test_connecting_to_a_dead_console_returns_none_and_never_raises():
    """This runs inside a live WebSocket; a traceback would close it with no
    explanation. The caller needs a value it can turn into a notice."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    # ...socket closed, so nothing is listening on `dead`.
    got = asyncio.run(cb.connect_console("127.0.0.1", dead))
    assert got is None


def test_the_unavailable_notice_is_bytes():
    """Every frame to the browser is binary; a str here would raise inside the
    error path, i.e. exactly when something is already wrong."""
    assert isinstance(cb.unavailable_notice(), bytes)
    assert b"unavailable" in cb.unavailable_notice()


def test_the_session_caps_match_the_console_and_the_idle_timer_is_on_input():
    """The idle timeout must be armed by BROWSER INPUT, not socket traffic.

    console.py stamps a clock into every frame it draws, so the socket is never
    quiet — a socket-idle timer would simply never fire, and the cap would be
    decorative. This is the single easiest thing here to get wrong.
    """
    assert cb.MAX_SESSIONS == 5, "must match console.py's own session cap"
    assert cb.IDLE_SECONDS == 900
    import inspect
    src = inspect.getsource(cb.run_bridge)
    assert "asyncio.wait_for(ws.receive(), IDLE_SECONDS)" in src, (
        "the idle deadline is not on the browser-input pump")


def test_the_pumps_are_tasks_not_coroutines():
    """``asyncio.wait`` raises TypeError on bare coroutines in Python 3.12,
    which is what the base image ships."""
    import inspect
    src = inspect.getsource(cb.run_bridge)
    assert src.count("asyncio.create_task(") >= 3
    assert "asyncio.wait(tasks" in src
