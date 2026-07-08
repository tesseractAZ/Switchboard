"""Behavioral tests for the Ingress UI app's pure validation/shaping helpers
(webui/app.py).

Run with plain Python (no pytest, no FastAPI, no network):

    python3 switchboard/tests/test_app.py

FastAPI/httpx are NOT importable on the test box, and app.py is written to
tolerate that: its FastAPI import is guarded, so loading the module gives a stub
``app`` and the pure helpers below are importable and testable in isolation.
These pin exactly the input validation that every new operator/light POST funnels
its untrusted path/body through before anything reaches an AMI or HA call:

  * ext validation (2-6 digits, rejects CRLF / dial strings / over-long),
  * the /api/hangup channel CRLF guard,
  * the light.* entity guard,
  * the wake-up HH:MM parse/validate wrapper,
  * the /api/lights area-grouping response shape,
  * the ext->channel map that powers the Hang up button,
and that the embedded UI grew the new controls.
"""
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_WEBUI = _ROOT / "rootfs" / "usr" / "share" / "switchboard" / "webui"
_WAKEUP = _ROOT / "rootfs" / "usr" / "share" / "switchboard" / "wakeup"

# app.py inserts the (absolute, container-only) paths itself, but on the test box
# those don't exist — add the repo's real dirs so the sibling modules (ami,
# timeparse, store, mwi_store, ha_client) resolve and app.py loads fully wired.
for p in (str(_WEBUI), str(_WAKEUP)):
    if p not in sys.path:
        sys.path.insert(0, p)

app = SourceFileLoader("switchboard_app", str(_WEBUI / "app.py")).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def test_module_loads_without_fastapi() -> None:
    # The whole point of the import guard: app.py is importable even though the
    # test box has no FastAPI. The route functions still defined (stub app), and
    # the pure helpers are present.
    check("load: importable without FastAPI", app._HAVE_FASTAPI is False)
    check("load: app is the stub when FastAPI absent", type(app.app).__name__ == "_NoApp")
    for fn in ("valid_ext", "channel_has_crlf", "is_light_entity",
               "parse_wakeup_hhmm", "configured_room_exts", "channels_by_ext",
               "build_lights_payload"):
        check(f"load: helper {fn} present", callable(getattr(app, fn, None)))


def test_valid_ext() -> None:
    check("ext: 2-digit room ok", app.valid_ext("11") is True)
    check("ext: 6-digit ok", app.valid_ext("123456") is True)
    check("ext: 1-digit rejected (no single-digit rooms; '0'=operator)", app.valid_ext("1") is False)
    check("ext: empty rejected", app.valid_ext("") is False)
    check("ext: None rejected", app.valid_ext(None) is False)
    check("ext: 7-digit (over-long) rejected", app.valid_ext("1234567") is False)
    check("ext: non-digit rejected", app.valid_ext("9;evil") is False)
    check("ext: CRLF-injection rejected", app.valid_ext("11\r\nAction: x") is False)
    # Digits-only "9911" passes the REGEX (the room-set membership check is the
    # second, decisive gate that keeps it off the trunk's outbound pattern).
    check("ext: digits-only 9911 passes regex (room-set is 2nd gate)", app.valid_ext("9911") is True)
    check("ext: leading/trailing space rejected", app.valid_ext(" 11 ") is False)
    check("ext: a 'noon'-style word rejected", app.valid_ext("noon") is False)
    # Trailing-newline rejection: a "$"-anchored re.match() would WRONGLY accept
    # "11\n" ($ matches just before a trailing \n); fullmatch closes that hole.
    check("ext: trailing LF rejected", app.valid_ext("11\n") is False)
    check("ext: trailing CRLF rejected", app.valid_ext("11\r\n") is False)


def test_channel_has_crlf() -> None:
    check("chan: clean PJSIP channel ok", app.channel_has_crlf("PJSIP/11-0000000a") is False)
    check("chan: CR rejected", app.channel_has_crlf("PJSIP/11\rAction: Command") is True)
    check("chan: LF rejected", app.channel_has_crlf("PJSIP/11\nAction: Command") is True)
    check("chan: CRLF rejected", app.channel_has_crlf("PJSIP/11\r\nAction: Command") is True)
    check("chan: empty has no CRLF (rejected elsewhere by emptiness)", app.channel_has_crlf("") is False)
    check("chan: None tolerated", app.channel_has_crlf(None) is False)


def test_is_light_entity() -> None:
    check("light: light.kitchen ok", app.is_light_entity("light.kitchen") is True)
    check("light: light.x_2 ok", app.is_light_entity("light.lamp_2") is True)
    check("light: switch.* rejected", app.is_light_entity("switch.fan") is False)
    check("light: scene/script rejected", app.is_light_entity("script.evil") is False)
    check("light: uppercase rejected (HA entity ids are lower-case)", app.is_light_entity("light.Kitchen") is False)
    check("light: empty rejected", app.is_light_entity("") is False)
    check("light: None rejected", app.is_light_entity(None) is False)
    check("light: domain-only rejected", app.is_light_entity("light.") is False)
    check("light: injection rejected", app.is_light_entity("light.k; drop") is False)


def test_parse_wakeup_hhmm() -> None:
    # Canonical HH:MM (what <input type=time> sends) round-trips and is zero-padded.
    check("wake: 07:30 -> 07:30", app.parse_wakeup_hhmm("07:30") == "07:30")
    check("wake: 7:30 -> 07:30 (pads hour)", app.parse_wakeup_hhmm("7:30") == "07:30")
    check("wake: 23:59 -> 23:59", app.parse_wakeup_hhmm("23:59") == "23:59")
    check("wake: 00:00 -> 00:00", app.parse_wakeup_hhmm("00:00") == "00:00")
    # Out-of-range / malformed rejected.
    check("wake: 24:00 rejected", app.parse_wakeup_hhmm("24:00") is None)
    check("wake: 12:60 rejected", app.parse_wakeup_hhmm("12:60") is None)
    check("wake: 99:99 rejected", app.parse_wakeup_hhmm("99:99") is None)
    check("wake: empty -> None", app.parse_wakeup_hhmm("") is None)
    check("wake: None -> None", app.parse_wakeup_hhmm(None) is None)
    check("wake: 7:5 (one-digit minute) rejected", app.parse_wakeup_hhmm("7:5") is None)
    # A CRLF-bearing body can't slip through as a valid time.
    check("wake: CRLF body rejected", app.parse_wakeup_hhmm("07:30\r\nevil") is None)
    # Free-form spoken-style strings delegate to the shared timeparse (available
    # here because the wakeup dir is on sys.path).
    if app.wakeup_timeparse is not None:
        check("wake: '7:30 am' -> 07:30 (timeparse)", app.parse_wakeup_hhmm("7:30 am") == "07:30")
        check("wake: '7:30 pm' -> 19:30 (timeparse)", app.parse_wakeup_hhmm("7:30 pm") == "19:30")
        check("wake: 'quarter past six' -> 06:15", app.parse_wakeup_hhmm("quarter past six") == "06:15")
        check("wake: 'noon' -> 12:00", app.parse_wakeup_hhmm("noon") == "12:00")
        check("wake: gibberish -> None", app.parse_wakeup_hhmm("zxcv") is None)


def test_configured_room_exts() -> None:
    opts = {"rooms": [{"ext": "11", "name": "Kitchen"}, {"ext": 12, "name": "Living"},
                      {"name": "no-ext"}, {"ext": None}]}
    exts = app.configured_room_exts(opts)
    check("rooms: collects '11'", "11" in exts)
    check("rooms: coerces int ext 12 -> '12'", "12" in exts)
    check("rooms: skips ext-less + None entries", exts == {"11", "12"})
    check("rooms: empty options -> empty set", app.configured_room_exts({}) == set())
    check("rooms: missing rooms key -> empty set", app.configured_room_exts({"rooms": None}) == set())


def test_channels_by_ext() -> None:
    # Two legs for ext 11: the longer-running one wins (a real call over a ring).
    chans = [
        {"ext": "11", "channel": "PJSIP/11-short", "duration": "00:00:03"},
        {"ext": "11", "channel": "PJSIP/11-long", "duration": "00:01:20"},
        {"ext": "16", "channel": "PJSIP/16-a", "duration": "00:00:09"},
        {"ext": "", "channel": "PJSIP/x", "duration": "00:00:01"},      # no ext -> skipped
        {"ext": "17", "channel": "", "duration": "00:00:05"},            # no channel -> skipped
    ]
    m = app.channels_by_ext(chans)
    check("chanmap: 11 -> longest leg", m.get("11") == "PJSIP/11-long")
    check("chanmap: 16 mapped", m.get("16") == "PJSIP/16-a")
    check("chanmap: leg without ext skipped", "" not in m)
    check("chanmap: leg without channel skipped", "17" not in m)
    check("chanmap: empty input -> {}", app.channels_by_ext([]) == {})
    check("chanmap: None input -> {}", app.channels_by_ext(None) == {})


def test_build_lights_payload() -> None:
    by_area = {
        "Kitchen": [{"entity_id": "light.kitchen", "name": "Kitchen", "state": "on"}],
        "": [{"entity_id": "light.hall", "name": "Hall", "state": "off"}],
    }
    out = app.build_lights_payload(by_area, True)
    check("lights: lights_ok True passthrough", out["lights_ok"] is True)
    check("lights: areas keyed by area label", "Kitchen" in out["areas"])
    check("lights: empty-area bucket relabeled 'Other'", "Other" in out["areas"])
    k = out["areas"]["Kitchen"][0]
    check("lights: only the 3 UI fields echoed",
          set(k.keys()) == {"entity_id", "name", "state"})
    check("lights: state carried", k["state"] == "on")
    # Unreachable HA -> empty areas + lights_ok False (UI shows "unavailable").
    down = app.build_lights_payload({}, False)
    check("lights: unreachable -> lights_ok False", down["lights_ok"] is False)
    check("lights: unreachable -> empty areas", down["areas"] == {})
    # A non-light entity that somehow slipped into HA's list is filtered out.
    bad = app.build_lights_payload({"X": [{"entity_id": "switch.evil", "name": "E", "state": "on"}]}, True)
    check("lights: non-light entity filtered out of the shape", bad["areas"]["X"] == [])
    # Missing name falls back to entity_id; missing state -> 'unknown'.
    fb = app.build_lights_payload({"A": [{"entity_id": "light.x"}]}, True)
    row = fb["areas"]["A"][0]
    check("lights: name falls back to entity_id", row["name"] == "light.x")
    check("lights: state falls back to 'unknown'", row["state"] == "unknown")


def test_index_html_controls() -> None:
    html = app.INDEX_HTML
    # The frontend grew the operator + light controls (each wired to its endpoint
    # by a data-* hook the delegated handlers read).
    check("ui: Page all button present", "id=\"pageall\"" in html and "Page all" in html)
    check("ui: Connect control present", "data-connect=" in html)
    check("ui: Hang up control present", "data-hangup=" in html)
    check("ui: wake-up time input present", "data-waketime=" in html and "type=\"time\"" in html)
    check("ui: wake-up Set control present", "data-wakeset=" in html)
    check("ui: MWI toggle present", "data-mwi=" in html)
    check("ui: MWI badge rendered when set", "mwibadge" in html)
    check("ui: Lights section present", "id=\"lights\"" in html and "💡 Lights" in html)
    check("ui: light toggle hook present", "data-light=" in html)
    check("ui: calls the new endpoints",
          "./api/connect/" in html and "./api/hangup" in html and
          "./api/page" in html and "./api/mwi/" in html and
          "./api/lights" in html and "./api/wakeup/" in html)
    check("ui: HA-unavailable message present",
          "Home Assistant unavailable" in html)
    # Untrusted values still escaped before innerHTML (XSS guard retained).
    check("ui: esc() still used", "function esc(" in html)


def test_card_action_buttons_labeled() -> None:
    # Every per-card action button carries a text label (not a bare emoji), and the
    # action row is an even 2-column grid — so the controls read clearly instead of
    # wrapping into an uneven, cluttered stack of mystery icons.
    html = app.INDEX_HTML
    check("ui: hang-up button labelled", "📵 Hang up" in html)
    check("ui: transfer button labelled", "↪ Transfer" in html)
    check("ui: message-waiting button labelled (set + clear)",
          "✉ Message" in html and "✉ Clear" in html)
    check("ui: MWI is no longer a bare envelope", "'✉ on' : '✉'" not in html)
    check("ui: action row is an even 2-col grid", "grid-template-columns: 1fr 1fr" in html)


def test_wakeup_ui_formatting() -> None:
    # Wake-ups read clearly: each pending call is a clean ".wakeitem" row with a
    # today/tomorrow "when", the empty state points to how to set one, and each
    # card's time box is clock-labelled so it isn't a stray unlabelled field.
    html = app.INDEX_HTML
    check("ui: wake-up list uses the clean .wakeitem rows", ".wakeitem" in html and "wakelist" in html)
    check("ui: wake-up shows when it rings (today/tomorrow)",
          "function wakeDay(" in html and "wkday" in html)
    check("ui: card wake-up box is clock-labelled", "wklab" in html and "data-waketime=" in html)
    check("ui: empty state explains how to set a wake-up", "No wake-ups set" in html)
    check("ui: cancel hook retained", "data-cancel=" in html)


def test_announce_serve_guard() -> None:
    # The LAN-exempt /announce route resolves names via safe_announce_path: strict
    # *.wav name regex AND realpath containment to the announce dir — so no
    # user-supplied value (traversal, absolute path, other extension) escapes it.
    check("announce: dir constant", app.ANNOUNCE_DIR == "/run/switchboard/announce")
    good = app.safe_announce_path("a12345.wav")
    check("announce: valid name resolves under the announce dir",
          good.startswith(app.ANNOUNCE_DIR + "/") and good.endswith("/a12345.wav"))
    for bad in ("../etc/passwd", "..%2fetc%2fpasswd", "a.mp3", "a.wav/x",
                "/etc/passwd", "", "A.wav", ".wav", "a..wav", "a" * 70 + ".wav"):
        if app.safe_announce_path(bad):
            check(f"announce: rejects {bad!r}", False)
            break
    else:
        check("announce: rejects traversal/absolute/ext/case/overlong names", True)


def test_dark_mode_covers_light_cards() -> None:
    # The room cards (.card), the lights area cards (.areacard) and the wake-up time
    # input all paint their background from var(--card). In dark mode, --card MUST be
    # set at a scope they ALL inherit (body) — scoping it to .card alone leaves the
    # lights section white with light text on it (unreadable), which is what shipped.
    import re
    html = app.INDEX_HTML
    check("ui: lights area cards paint from --card", ".areacard" in html and "var(--card" in html)
    dark = html[html.index("prefers-color-scheme: dark"):]
    check("ui: dark mode sets --card on body (so lights cards darken too)",
          bool(re.search(r"body\s*\{[^}]*--card\s*:", dark)))
    check("ui: dark mode does NOT scope --card to .card only",
          not re.search(r"\.card\s*\{\s*--card\s*:", dark))


def test_index_html_js_parses() -> None:
    # The dashboard JS is embedded in a (regular, non-raw) Python string, so a bare
    # `\n` in the SOURCE emits a real newline into the browser — which, inside a JS
    # string literal, is a syntax error that kills the ENTIRE inline <script> and
    # blanks the GUI. (This actually shipped: the transfer prompt used '...\n'.)
    # py_compile can't see it, so parse the rendered JS with node as a guard.
    import shutil
    import subprocess
    import tempfile
    # Plain string slicing (not an HTML-filter regex): we're extracting our OWN
    # template's single lowercase <script> block, not sanitizing untrusted HTML.
    html = app.INDEX_HTML
    start = html.find("<script>")
    end = html.find("</script>", start)
    found = start != -1 and end != -1
    check("ui: inline <script> block found", found)
    if not found:
        return
    node = shutil.which("node")
    if not node:
        print("SKIP ui: dashboard JS parses (node not on PATH)")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(html[start + len("<script>"):end])
        js_path = fh.name
    proc = subprocess.run([node, "--check", js_path], capture_output=True, text=True)
    check("ui: dashboard JS parses (no syntax error in the served <script>)",
          proc.returncode == 0)
    if proc.returncode != 0:
        print("   " + (proc.stderr or "").strip().splitlines()[0] if proc.stderr else "")


def test_route_handlers_defined() -> None:
    # The route functions are defined as plain module-level callables even under
    # the stub app, so the wiring is at least syntactically present/importable.
    for fn in ("api_status", "api_ring", "api_connect", "api_hangup",
               "api_wakeup_set", "api_wakeup_cancel", "api_page", "api_mwi",
               "api_lights", "api_light_set", "index"):
        check(f"route: {fn} defined", callable(getattr(app, fn, None)))


def test_client_guard() -> None:
    # The Ingress/Supervisor-only client guard is unchanged and reused by every
    # new POST (via the middleware).
    check("guard: Supervisor IP allowed", app._client_allowed("172.30.32.2") is True)
    check("guard: loopback allowed", app._client_allowed("127.0.0.1") is True)
    check("guard: LAN client rejected", app._client_allowed("192.168.5.10") is False)
    check("guard: empty rejected", app._client_allowed("") is False)


def test_cached_status_bundle() -> None:
    import time as _t
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return (["ep"], {"c": 1}, ["ch"])

    orig = app.get_status_bundle
    app.get_status_bundle = fake
    app._status_cache["ts"] = 0.0
    app._status_cache["value"] = None
    try:
        r1 = app.cached_status_bundle()
        r2 = app.cached_status_bundle()
        check("cache: two reads within TTL open ONE AMI session",
              calls["n"] == 1 and r1 == r2 == (["ep"], {"c": 1}, ["ch"]))
        # Expire the TTL -> refetch.
        app._status_cache["ts"] = _t.monotonic() - (app._STATUS_TTL + 1.0)
        app.cached_status_bundle()
        check("cache: refetches once the TTL expires", calls["n"] == 2)
        # An AMI error propagates (callers stay fail-open) and is NOT cached.
        app._status_cache["ts"] = 0.0
        app._status_cache["value"] = None

        def boom():
            calls["n"] += 1
            raise app.AMIError("down")

        app.get_status_bundle = boom
        raised = False
        try:
            app.cached_status_bundle()
        except app.AMIError:
            raised = True
        check("cache: AMIError propagates uncached (never serve stale ok)",
              raised and app._status_cache["value"] is None)
    finally:
        app.get_status_bundle = orig
        app._status_cache["ts"] = 0.0
        app._status_cache["value"] = None


def main() -> None:
    test_module_loads_without_fastapi()
    test_cached_status_bundle()
    test_valid_ext()
    test_channel_has_crlf()
    test_is_light_entity()
    test_parse_wakeup_hhmm()
    test_configured_room_exts()
    test_channels_by_ext()
    test_build_lights_payload()
    test_index_html_controls()
    test_card_action_buttons_labeled()
    test_wakeup_ui_formatting()
    test_dark_mode_covers_light_cards()
    test_announce_serve_guard()
    test_index_html_js_parses()
    test_route_handlers_defined()
    test_client_guard()
    print()
    if _failures:
        print(f"{_failures} FAILURE(S)")
        raise SystemExit(1)
    print("all app helper tests passed")


if __name__ == "__main__":
    main()
