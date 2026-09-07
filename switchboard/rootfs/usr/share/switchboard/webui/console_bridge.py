"""Browser ↔ operator-console bridge, with no web framework in sight.

The operator console (``console/console.py``) is a telnet server on
127.0.0.1:2300. This module is everything needed to put a browser on the other
end of it: the telnet client preamble, the byte-level payload rules, and two
async pumps. ``webui/app.py`` supplies only the WebSocket object.

WHY IT IS SEPARATE. Same reason ``ami.py`` is: FastAPI is not installed on the
box the tests run on, so anything importable only alongside it is untestable.
The rules below are where the defects live — a route decorator is not. Every
public function here is drivable from plain ``python3``.

★ WHY NOT A THREAD, AND NOT ``select``. uvicorn runs ONE worker. Eleven of
app.py's routes are plain ``def`` and execute on anyio's 40-token thread
limiter — ``/``, ``/api/status``, ``/api/ring``, ``/api/connect``, ``/api/page``,
``/phonebook.xml`` among them; the rest are ``async def`` and share the loop
directly. A blocking ``recv()`` lifted from ``console-web/server.py`` stalls
both sets. And it would stall them *routinely*, not rarely: ``console.py``'s
``draw()`` suppresses unchanged frames and polls at 3 s, so a quiet board sends
nothing for seconds at a time. Opening the terminal would take the management UI
and call control offline for as long as the tab stayed open. Everything here is
awaited.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys

# Where the operator console listens. Parameters, not import-time constants
# baked into the callers, so a test can point the bridge at a fixture socket.
CONSOLE_HOST = os.environ.get("SWITCHBOARD_CONSOLE_HOST", "127.0.0.1")
CONSOLE_PORT = int(os.environ.get("SWITCHBOARD_CONSOLE_PORT") or 2300)

CONNECT_TIMEOUT = 5.0
# Browser INPUT idle, never socket idle. console.py stamps a clock into every
# frame it draws, so the socket is never quiet and a socket-idle timer would
# never fire. This is the single easiest thing to get wrong here.
IDLE_SECONDS = 900
# Absolute ceiling on one session, matching the TTL the standalone server used.
MAX_SESSION_SECONDS = 12 * 3600
# Concurrent browser terminals. Matches console.py's own MAX_SESSIONS so five
# forgotten tabs cannot lock a real telnet operator out of the board. Acquired
# BEFORE any socket to :2300 is opened.
MAX_SESSIONS = 5
# Bounded queue rather than a send timeout: a browser that stops reading is
# detected in bounded BYTES, with no cancellation reaching into the server's
# internals mid-frame.
OUTQ_MAX = 64
DRAIN_TIMEOUT = 30

_UNAVAILABLE = b"\r\n  Operator console unavailable (is it enabled?).\r\n"


# --------------------------------------------------------------------------- #
# The telnet helpers live with the standalone server. Load them BY PATH under a
# private name — never by putting that directory on sys.path, which is how the
# module-name collision fixed in 0.92.0 got its reach in the first place.
# --------------------------------------------------------------------------- #
def _load_telnet(path: str = "/usr/share/switchboard/console-web/consoleproto.py"):
    if not os.path.exists(path):          # the dev box has no /usr/share/switchboard
        return None
    spec = importlib.util.spec_from_file_location("switchboard_consoleproto", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["switchboard_consoleproto"] = mod   # register BEFORE exec_module
    spec.loader.exec_module(mod)
    return mod


tn = _load_telnet()

# Telnet constants, mirrored so this module's pure functions work with or
# without the helper present (the dev box has no container filesystem).
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA, OPT_NAWS = 1, 3, 31


def telnet_preamble() -> bytes:
    """The client half of the negotiation, sent once on connect.

    console.py opens with IAC WILL ECHO / WILL SGA / DO SGA / DO NAWS and then
    never inspects the answer — so this exists to be *polite*, not to be
    required. What IS load-bearing is stripping the console's own preamble out
    of the console→browser direction: its ten bytes arrive inside the first
    chunk and, unstripped, render as replacement characters in the terminal's
    primary buffer, visible on exit and on every reconnect.
    """
    return bytes([IAC, DO, OPT_ECHO, IAC, DO, OPT_SGA,
                  IAC, WILL, OPT_SGA, IAC, WILL, OPT_NAWS])


def naws(cols: int, rows: int) -> bytes:
    """NAWS window-size subnegotiation.

    Clamped to 200 so a dimension byte can never be 255 and collide with IAC —
    which would corrupt the very stream it is describing.
    """
    c = max(1, min(200, int(cols)))
    r = max(1, min(200, int(rows)))
    return bytes([IAC, SB, OPT_NAWS, 0, c, 0, r, IAC, SE])


def escape_iac(data: bytes) -> bytes:
    """Double 0xFF so a literal byte is never read as a telnet command.

    Applied unconditionally. The `if b"\\xff" in data` short-circuit the old
    server used is a second thing to get wrong for no measurable gain —
    ``bytes.replace`` on a miss is already cheap.
    """
    return data.replace(b"\xff", b"\xff\xff")


def classify_payload(data):
    """★ THE ONE THAT RINGS A PHONE IF YOU GET IT WRONG.

    Returns ``("resize", <naws bytes>)``, ``("keys", <bytes>)`` or
    ``("drop", b"")``.

    The standalone server sniffs ``data[:1] == b"{"``. That is correct there,
    because its own frame decoder hands it ``bytes``. Through a WebSocket it is
    not: ``term.onData`` sends JS strings and ``JSON.stringify`` sends a string,
    so both arrive as TEXT frames — and ``"{"[:1] == b"{"`` is ``False`` with no
    exception raised. A faithful port therefore falls through to the keystroke
    path and types ``{"type":"resize","cols":120,"rows":40}`` into the console.

    The console's key handler maps every byte 32..126 to a key, and binds
    ``r``→ring, ``c``→connect, ``h``→hang up, ``t``→transfer, ``p``→page-all,
    ``l``→lights, ``m``→MWI. That string contains r, e, s, i, z, c, o, l, t, y
    and p — and the page sends it inside ``ws.onopen``. Opening the sidebar panel
    would ring the selected room and then walk the board through connect, page
    and lights.

    So: normalise to bytes FIRST, and be stricter than the old server — any text
    frame that parses as a JSON object is a control message and is never
    forwarded as keystrokes, even when its ``type`` is unrecognised.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")       # NOT latin-1: that would let a typed
    if not isinstance(data, (bytes, bytearray)):   # "ÿ" become a bare 0xFF and
        return ("drop", b"")                       # desync the key parser.
    data = bytes(data)
    if not data:
        return ("drop", b"")

    if data[:1] == b"{":
        try:
            msg = json.loads(data.decode("utf-8", "strict"))
        except (ValueError, UnicodeDecodeError):
            return ("keys", data)          # not JSON after all; a literal "{"
        if isinstance(msg, dict):
            if msg.get("type") == "resize":
                try:
                    return ("resize", naws(msg.get("cols", 80), msg.get("rows", 24)))
                except (TypeError, ValueError):
                    return ("drop", b"")   # malformed size: keep the session
            return ("drop", b"")           # a control message we do not know
    return ("keys", data)


def encode_for_console(data) -> bytes:
    """Browser payload → bytes on the wire, or b"" for a control message.

    The IAC doubling happens AFTER the resize sniff, so a NAWS subnegotiation
    this module built is never mangled by it.
    """
    kind, payload = classify_payload(data)
    if kind == "resize":
        return payload                      # already well-formed telnet
    if kind == "keys":
        return escape_iac(payload)
    return b""


def strip_console_output(buf: bytes, carry: bytes = b""):
    """Console → browser, telnet stripped. Returns ``(clean, new_carry)``.

    ``carry`` MUST be threaded back in by the caller. ``clean, _ = strip(...)``
    silently discards a trailing partial IAC sequence, which then reappears as
    stray bytes in the next chunk.
    """
    if tn is None:                          # no helper (dev box): pass through
        return (carry + buf, b"")
    return tn.strip_telnet(carry + buf)


# --------------------------------------------------------------------------- #
# The async halves.
# --------------------------------------------------------------------------- #
_slots = asyncio.Semaphore(MAX_SESSIONS)


async def connect_console(host: str = None, port: int = None):
    """Open the console socket. Returns ``(reader, writer)`` or ``None``.

    Never raises: the caller is inside a live WebSocket and a traceback there
    would close it with no explanation.
    """
    try:
        return await asyncio.wait_for(
            asyncio.open_connection(host or CONSOLE_HOST, port or CONSOLE_PORT),
            CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return None


def unavailable_notice() -> bytes:
    """Sent as BYTES. Every frame to the browser is binary (see run_bridge)."""
    return _UNAVAILABLE


async def run_bridge(ws, *, host: str = None, port: int = None, now=None) -> None:
    """Pump one browser session against the operator console.

    ``ws`` is duck-typed on the three methods a Starlette WebSocket exposes —
    ``send_bytes``, ``receive`` and ``close`` — so a test can drive this with a
    fake and never import a web framework.

    Structure, and why each piece is the way it is:

      * The session slot is taken BEFORE the console socket, so five abandoned
        browser tabs cannot occupy console.py's own five slots.
      * Both directions are tasks passed to ``asyncio.wait``. Passing bare
        coroutines there raises ``TypeError`` on Python 3.12 (the base image),
        so they are wrapped in ``create_task`` first.
      * Console → browser goes through a BOUNDED queue rather than a send
        timeout. A browser that stops reading is then detected in bounded bytes,
        and nothing has to cancel a send half-way through writing a frame.
      * Console EOF closes the WebSocket. Without it, pressing ``q`` on the board
        leaves the page hung with ``onclose`` never firing.
    """
    loop = now or (lambda: asyncio.get_event_loop().time())
    if _slots.locked() and _slots._value <= 0:      # non-blocking check
        await ws.accept()
        await ws.send_bytes(b"\r\n  Console busy (all %d sessions in use).\r\n"
                            % MAX_SESSIONS)
        await ws.close(code=1013)
        return
    await _slots.acquire()
    conn = None
    try:
        await ws.accept()
        conn = await connect_console(host, port)
        if conn is None:
            await ws.send_bytes(unavailable_notice())
            await ws.close(code=1011)
            return
        reader, writer = conn
        writer.write(telnet_preamble())
        await asyncio.wait_for(writer.drain(), DRAIN_TIMEOUT)

        outq: asyncio.Queue = asyncio.Queue(maxsize=OUTQ_MAX)
        deadline = loop() + MAX_SESSION_SECONDS

        async def console_to_queue():
            carry = b""
            while True:
                chunk = await reader.read(4096)
                if not chunk:                     # console hung up
                    await outq.put(None)
                    return
                clean, carry = strip_console_output(chunk, carry)
                if clean:
                    outq.put_nowait(clean)        # QueueFull -> tear the session down

        async def queue_to_ws():
            while True:
                item = await outq.get()
                if item is None:
                    return
                # BINARY, always. A per-chunk .decode() splits mid-codepoint on
                # essentially every draw (the board renders ☎ 💡 ↔ ─ ▸ ● and a
                # colored frame exceeds one 4 KiB read), and burns replacement
                # characters into the operator's screen. xterm's own decoder
                # carries continuation state across term.write(Uint8Array).
                await ws.send_bytes(item)

        async def ws_to_console():
            while True:
                if loop() > deadline:
                    return
                msg = await asyncio.wait_for(ws.receive(), IDLE_SECONDS)
                if msg.get("type") == "websocket.disconnect":
                    return
                raw = msg.get("bytes")
                if raw is None:
                    raw = msg.get("text")
                if raw is None:
                    continue
                out = encode_for_console(raw)
                if not out:
                    continue                      # control message; never typed
                writer.write(out)
                await asyncio.wait_for(writer.drain(), DRAIN_TIMEOUT)

        tasks = [asyncio.create_task(console_to_queue()),
                 asyncio.create_task(queue_to_ws()),
                 asyncio.create_task(ws_to_console())]
        try:
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            # Awaiting the cancellations is not optional: skipping it leaves
            # tasks alive holding the console socket after this returns.
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:                             # noqa: BLE001 — never escape
        pass
    finally:
        if conn is not None:
            try:
                conn[1].close()
            except Exception:                     # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(ws.close(code=1000), 5)
        except Exception:                         # noqa: BLE001
            pass
        _slots.release()
