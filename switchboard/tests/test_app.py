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
    # Under pytest the print + counter are DECORATIVE — only the __main__
    # runner reads _failures, so a failing check would still 'pass' the
    # test. Assert too, so both harnesses actually enforce every check.
    assert cond, name


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


def test_a_pending_wakeup_can_be_cancelled_from_its_room_card() -> None:
    # 2026-09-21, owner report: "no way to cancel a wakeup call on the GUI". The
    # only Cancel lived in the list below the room grid; the card where the
    # wake-up was SET offered Set alone. The card now carries its own Cancel,
    # shown only when that room has one pending, and every cancel is a checked
    # request so a refusal cannot look like success.
    html = app.INDEX_HTML
    check("ui: card renders a Cancel beside Set when a wake-up is pending",
          "data-wakecancel=" in html and "wkPending ?" in html)
    check("ui: the card's Cancel is wired to the cancel endpoint",
          "getAttribute('data-wakecancel')" in html and "/cancel', {})" in html)
    check("ui: the card's Cancel confirms out loud", "'Cancelled ✓'" in html)
    check("ui: no cancel is a bare fetch any more (a 403 would look like success)",
          "/cancel', {method: 'POST'}" not in html)


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
    """★ EXACTLY ONE ADDRESS — and never a subnet.

    Loopback used to be allowed here "for local health checks". It was removed
    in 0.93.0 when this port gained a terminal onto the operator console:
    the add-on runs host_network, so 127.0.0.1 is the HOST's loopback, shared
    with every other host-network add-on and every process on the Pi (the SSH
    add-on's root shell included). It is a location, not an identity. Verified
    before removal that nothing dials 127.0.0.1:8099.

    The subnet assertions are the important ones. The sibling Z-Wave add-on
    shipped a check matching the whole 172.30.32.0/23 hassio bridge — which is
    where every sibling add-on container lives — so its address term was always
    true and the whole expression collapsed to "did the client send a header it
    chose to send". Any sibling add-on, or an SSRF in a third-party one, got a
    login-free operator session. A "helpful" widening here must fail.
    """
    check("guard: Supervisor IP allowed", app._client_allowed("172.30.32.2") is True)
    check("guard: LAN client rejected", app._client_allowed("192.168.1.10") is False)
    check("guard: empty rejected", app._client_allowed("") is False)
    check("guard: HOST loopback is NOT an identity under host_network",
          app._client_allowed("127.0.0.1") is False and app._client_allowed("::1") is False)
    # The /23 the zwave defect matched. Both ends and a middle address.
    for sibling in ("172.30.32.1", "172.30.32.3", "172.30.33.4", "172.30.32.255"):
        check(f"guard: sibling add-on {sibling} rejected (never a CIDR)",
              app._client_allowed(sibling) is False)
    check("guard: the allowlist is exactly one address",
          app._ALLOWED_CLIENTS == frozenset({"172.30.32.2"}))


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


def test_phonebook_xml() -> None:
    # The WP826 Remote-Phonebook source: one Contact per configured room, name ->
    # FirstName, ext -> phonenumber. Bad exts skipped; markup-bearing names escaped.
    rooms = [{"ext": "11", "name": "Family & Room"}, {"ext": "bad", "name": "x"},
             {"ext": "19", "name": "Cordless"}]
    xml = app.build_phonebook_xml(rooms)
    check("phonebook: valid XML declaration + AddressBook root",
          xml.startswith('<?xml') and "<AddressBook>" in xml and "</AddressBook>" in xml)
    check("phonebook: one Contact per VALID room (bad ext skipped)", xml.count("<Contact>") == 2)
    check("phonebook: name with & is XML-escaped (no raw ampersand)",
          "Family &amp; Room" in xml and "Family & Room" not in xml)
    check("phonebook: ext becomes the phonenumber", "<phonenumber>19</phonenumber>" in xml)
    check("phonebook: empty roster still yields a well-formed doc",
          "<AddressBook>" in app.build_phonebook_xml([]))


def test_announce_helpers() -> None:
    # The generated clip name must satisfy the strict announce-name regex AND
    # round-trip through safe_announce_path (so it can't escape the announce dir).
    name = app._announce_name("19")
    check("announce: generated name matches the *.wav name regex",
          app._ANNOUNCE_NAME.match(name) is not None)
    check("announce: generated name resolves safely under the announce dir",
          app.safe_announce_path(name) != "")
    # Collision-free: two names for the same ext in the same instant must differ.
    check("announce: names are unique per invocation (uuid, not pid+second)",
          app._announce_name("19") != app._announce_name("19"))
    check("announce: text cap is a sane bound", app.ANNOUNCE_MAX_TEXT == 500)
    # LAN announce is OFF by default (no token configured) — read per-request, so a
    # token edit applies without a process restart.
    check("announce: token empty by default (LAN announce disabled)", app._announce_token() == "")


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
    test_phonebook_xml()
    test_announce_helpers()
    print()
    if _failures:
        print(f"{_failures} FAILURE(S)")
        raise SystemExit(1)
    print("all app helper tests passed")


if __name__ == "__main__":
    main()


def test_delivery_outcomes_are_recorded_where_they_can_be_read(tmp_path) -> None:
    """Attempts that never became a call must still leave a record.

    The QoS ledger is written from the dialplan's hangup extension, so it can
    only ever describe legs that ANSWERED. An Originate refused for want of a
    contact, a handset that rang unanswered, an AMI error -- all invisible. One
    audit window held three refused Originates and a wake-up that rang 60 s
    unanswered, and none of them produced a ledger row, a sensor, or anything an
    operator would read. Nothing distinguished "the phone never rang" from "the
    user ignored it".

    Written under /share because /data cannot be read from outside the
    container."""
    import json as _json

    out = tmp_path / "sub" / "delivery.jsonl"        # nested: the writer must mkdir
    real = app._delivery.OUTCOME_PATH
    app._delivery.OUTCOME_PATH = str(out)
    try:
        app._record_delivery("19", "announce", "unreachable",
                             device_state="UNAVAILABLE")
        app._record_delivery("19", "announce", "skipped-busy", device_state="INUSE")
        app._record_delivery("14", "wakeup", "noanswer")
    finally:
        app._delivery.OUTCOME_PATH = real

    recs = [_json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    check("delivery: one record per attempt", len(recs) == 3)
    check("delivery: every record is dated", all(r.get("ts") for r in recs))
    check("delivery: names the extension",
          [r["ext"] for r in recs] == ["19", "19", "14"])
    check("delivery: distinguishes the failure modes",
          [r["outcome"] for r in recs] == ["unreachable", "skipped-busy", "noanswer"])
    check("delivery: carries the device state that caused the skip",
          recs[0]["device_state"] == "UNAVAILABLE")
    check("delivery: omits absent optional fields rather than writing null",
          "device_state" not in recs[2])
    check("delivery: distinguishes a wake-up from an announcement",
          recs[2]["kind"] == "wakeup")

    # A record path that cannot be written must never fail a delivery.
    app._delivery.OUTCOME_PATH = "/proc/cannot/write/here.jsonl"
    try:
        app._record_delivery("19", "announce", "unreachable")
        check("delivery: an unwritable record path is swallowed", True)
    finally:
        app._delivery.OUTCOME_PATH = real


def test_announce_handler_refuses_a_contactless_endpoint_and_records_it(tmp_path) -> None:
    """The HANDLER must run the unreachable pre-flight -- not merely be able to.

    Mutation testing caught this: disabling the pre-flight in api_announce left
    the whole suite green, because the guard function had its own test while the
    call site had none. That is the fourth time in this work that a function was
    covered and its caller was not, so the caller gets driven directly here.

    An Originate to a contactless endpoint cannot create a channel, and with no
    channel there is no dialplan, no SW_STAGE, no rtpqos and no ledger row --
    the failure's only trace was an Asterisk ERROR line."""
    import asyncio as _aio
    import json as _json

    out = tmp_path / "delivery.jsonl"
    originated = []

    class _Req:
        headers = {}
        @staticmethod
        async def json():
            return {"text": "hello"}

    class _Resp:
        """Stand-in for fastapi's JSONResponse, which is None without fastapi."""
        def __init__(self, payload, status_code=200):
            self.payload, self.status_code = payload, status_code

    class _Renderer:
        """Stand-in for the TTS renderer: the handler renders BEFORE it guards,
        and announce_asterisk is None in the test environment."""
        @staticmethod
        def build_announcement_8k(text, path):
            with open(path, "wb") as fh:
                fh.write(b"\0" * 32)
            return True

    saved = {k: getattr(app, k) for k in
             ("load_options", "configured_room_exts", "valid_ext",
              "get_device_state", "device_busy", "device_unreachable",
              "announce_to_ext", "announce_asterisk",
              "ANNOUNCE_DIR", "JSONResponse")}
    try:
        app.JSONResponse = _Resp
        app.announce_asterisk = _Renderer
        app.ANNOUNCE_DIR = str(tmp_path / "ann")
        import os as _os
        _os.makedirs(app.ANNOUNCE_DIR, exist_ok=True)
        app.load_options = lambda: {}
        app.configured_room_exts = lambda o: {"19"}
        app.valid_ext = lambda e: True
        app.device_busy = lambda s: False
        app.device_unreachable = lambda s: s == "UNAVAILABLE"
        app.get_device_state = lambda e: "UNAVAILABLE"
        app.announce_to_ext = lambda e, s: originated.append(e) or True
        app._delivery.OUTCOME_PATH = str(out)

        resp = _aio.run(app.api_announce("19", _Req()))
    finally:
        for k, v in saved.items():
            setattr(app, k, v)

    check("handler: refuses rather than firing a doomed Originate",
          originated == [])
    check("handler: reports it did not deliver",
          getattr(resp, "status_code", None) == 503)
    check("handler: wrote a delivery record", out.exists())
    rec = _json.loads(out.read_text().splitlines()[-1])
    check("handler: the record names the failure mode",
          rec["outcome"] == "unreachable")
    check("handler: the record names the extension", rec["ext"] == "19")
    check("handler: the record carries the device state",
          rec["device_state"] == "UNAVAILABLE")


def test_announce_duration_guard_and_dedup(tmp_path) -> None:
    """A runaway or repeating announcement must be refused, and named.

    Four live announcements ran 66-72 s each and three were bit-identical
    (billsec 72, rxcount 3626) from three DIFFERENT files -- each render gets a
    fresh uuid, so identical payloads looked like different ones and the repeat
    went unnoticed until a duration histogram exposed it. A 72-second unsolicited
    announcement also holds the cordless off-hook, so an inbound call arriving
    during one lands as call-waiting instead of a normal ring."""
    hdr = b"\0" * 44

    short = tmp_path / "short.wav"
    short.write_bytes(hdr + b"\1" * (16000 * 5))          # 5 s
    long_ = tmp_path / "long.wav"
    long_.write_bytes(hdr + b"\1" * (16000 * 120))        # 120 s

    check("duration: a 5 s clip measures ~5 s",
          abs(app._announce_seconds(str(short)) - 5.0) < 0.01)
    check("duration: a 120 s clip measures ~120 s",
          abs(app._announce_seconds(str(long_)) - 120.0) < 0.01)
    check("duration: over the cap is detected",
          app._announce_seconds(str(long_)) > app.ANNOUNCE_MAX_SECONDS)
    check("duration: under the cap is allowed",
          app._announce_seconds(str(short)) < app.ANNOUNCE_MAX_SECONDS)
    # An unmeasurable clip must NOT be refused -- failing closed would silence
    # alerts, which is worse than letting a long one through.
    check("duration: an unmeasurable clip returns None, not a refusal",
          app._announce_seconds(str(tmp_path / "nope.wav")) is None)

    # Identical bytes under different names must hash the same -- that is the
    # whole point, since every render gets a fresh uuid.
    twin = tmp_path / "twin.wav"
    twin.write_bytes(short.read_bytes())
    check("digest: identical payloads under different names hash alike",
          app._announce_digest(str(short)) == app._announce_digest(str(twin)) != "")
    check("digest: different payloads differ",
          app._announce_digest(str(short)) != app._announce_digest(str(long_)))

    d1, d2 = app._announce_digest(str(short)), app._announce_digest(str(long_))
    app._ANNOUNCE_LAST.clear()
    try:
        check("dedup: the first announcement is never a duplicate",
              app._is_duplicate_announce("19", d1, now=1000.0) is False)
        # v0.85.0 — the check is PURE. Nothing is suppressed until something has
        # actually played, which is what _mark_announce_played records.
        check("dedup: merely CHECKING does not start the window",
              app._is_duplicate_announce("19", d1, now=1001.0) is False)
        app._mark_announce_played("19", d1, now=1000.0)
        check("dedup: the same payload moments after a PLAYBACK is a duplicate",
              app._is_duplicate_announce("19", d1, now=1030.0) is True)
        check("dedup: a DIFFERENT payload is not suppressed",
              app._is_duplicate_announce("19", d2, now=1040.0) is False)

        # ★ THE LOCKOUT. Until v0.85.0 the check recorded the attempt itself,
        # before any guard had run — so an announcement REFUSED (handset
        # unreachable, 503) started a suppression window anyway, and every retry
        # pushed that window forward. The announcement could never be delivered
        # again while the caller kept trying, and the callers that retry are the
        # alerting ones.
        app._ANNOUNCE_LAST.clear()
        t = 0.0
        for _ in range(20):                 # twenty refused attempts, 30 s apart
            check_silent = app._is_duplicate_announce("19", d1, now=t)
            assert check_silent is False, (
                f"a refused announcement was called a duplicate at t={t} — "
                "nothing had ever played")
            t += 30.0
        check("dedup: a refused announcement never suppresses its own retry",
              app._is_duplicate_announce("19", d1, now=t) is False)

        # ...and once it DOES play, retrying inside the window is suppressed
        # WITHOUT extending it, so the window is a rate limit and not a lockout.
        app._ANNOUNCE_LAST.clear()
        app._mark_announce_played("19", d1, now=0.0)
        t = 0.0
        for _ in range(20):
            t += 30.0
            app._is_duplicate_announce("19", d1, now=t)   # suppressed, no effect
        check("dedup: retrying while suppressed does not renew the window",
              app._is_duplicate_announce(
                  "19", d1, now=app.ANNOUNCE_DEDUP_WINDOW_S + 1) is False)
        # ...and a genuine repeat after the window is allowed through.
        check("dedup: the same payload after the window is allowed",
              app._is_duplicate_announce(
                  "19", d1, now=app.ANNOUNCE_DEDUP_WINDOW_S + 5) is False)
        # Suppression must be per-extension.
        app._ANNOUNCE_LAST.clear()
        app._is_duplicate_announce("19", d1, now=1000.0)
        check("dedup: another extension is unaffected",
              app._is_duplicate_announce("14", d1, now=1001.0) is False)
    finally:
        app._ANNOUNCE_LAST.clear()


def _drive_announce(tmp_path, *, payload: bytes, state="NOT_INUSE", out=None,
                    originate=None, real_state_guards=False):
    """Run the real api_announce handler with the renderer producing `payload`.

    The guards live INSIDE the handler, so testing the helper functions proves
    nothing about whether they run -- mutation testing showed both call sites
    surviving while the helpers were fully covered.

    `originate` replaces what ami.announce_to_ext does: the default appends the
    ext and returns True. Pass a callable that raises, or returns False, to
    replay an Originate that never became a call.

    `real_state_guards` leaves device_busy / device_unreachable unstubbed so the
    handler classifies `state` for itself. The stubs below are what let a caller
    say "busy" without spelling an Asterisk state; a test ABOUT the states has to
    turn them off, or it is asserting against its own lambdas."""
    import asyncio as _aio

    class _Resp:
        def __init__(self, p, status_code=200):
            self.payload, self.status_code = p, status_code

    class _Req:
        headers = {}
        @staticmethod
        async def json():
            return {"text": "hello"}

    class _Renderer:
        @staticmethod
        def build_announcement_8k(text, path):
            with open(path, "wb") as fh:
                fh.write(payload)
            return True

    originated = []
    saved_outcome = app._delivery.OUTCOME_PATH
    saved = {k: getattr(app, k) for k in
             ("load_options", "configured_room_exts", "valid_ext",
              "get_device_state", "device_busy", "device_unreachable",
              "announce_to_ext", "announce_asterisk",
              "ANNOUNCE_DIR", "JSONResponse")}
    import os as _os
    try:
        app.JSONResponse = _Resp
        app.announce_asterisk = _Renderer
        app.ANNOUNCE_DIR = str(tmp_path / "ann")
        _os.makedirs(app.ANNOUNCE_DIR, exist_ok=True)
        app.load_options = lambda: {}
        app.configured_room_exts = lambda o: {"19"}
        app.valid_ext = lambda e: True
        if not real_state_guards:
            app.device_busy = lambda s: False
            app.device_unreachable = lambda s: False
        app.get_device_state = lambda e: state
        if originate is None:
            app.announce_to_ext = lambda e, s: originated.append(e) or True
        else:
            def _originate(e, s, _fn=originate):
                originated.append(e)
                return _fn(e, s)
            app.announce_to_ext = _originate
        # The handler READS the ledger now (the content half of the duplicate
        # check), so every drive gets its own — a shared one would carry one
        # test's deliveries into the next.
        app._delivery.OUTCOME_PATH = str(out if out is not None
                                         else tmp_path / "delivery-outcomes.jsonl")
        resp = _aio.run(app.api_announce("19", _Req()))
    finally:
        for k, v in saved.items():
            setattr(app, k, v)
        # The outcome path lives on the shared module, not on app, so it needs
        # restoring explicitly -- otherwise it leaks into every later test.
        app._delivery.OUTCOME_PATH = saved_outcome
    return resp, originated


def test_announce_handler_refuses_a_runaway_clip(tmp_path) -> None:
    """The handler must apply the duration cap -- not merely be able to.

    Mutation testing showed this call site surviving while _announce_seconds()
    was fully covered. A 72-second unsolicited announcement holds the cordless
    off-hook, so an inbound call arriving during one lands as call-waiting."""
    out = tmp_path / "d.jsonl"
    payload = b"\0" * 44 + b"\1" * (16000 * 200)      # 200 s, well over the cap
    resp, originated = _drive_announce(tmp_path, payload=payload, out=out)

    check("runaway: refused rather than played",
          getattr(resp, "status_code", None) == 413)
    check("runaway: the Originate never fired", originated == [])
    check("runaway: recorded", out.exists())
    import json as _json
    rec = _json.loads(out.read_text().splitlines()[-1])
    check("runaway: named as too-long", rec["outcome"] == "too-long")
    check("runaway: the record says how long it was", rec["seconds"] > 190)


def test_announce_handler_suppresses_an_identical_repeat(tmp_path) -> None:
    """The handler must dedup -- three bit-identical live announcements looked
    like three different ones because each render gets a fresh uuid."""
    out = tmp_path / "d2.jsonl"
    payload = b"\0" * 44 + b"\1" * (16000 * 5)        # 5 s, under the cap
    app._ANNOUNCE_LAST.clear()
    try:
        r1, o1 = _drive_announce(tmp_path, payload=payload, out=out)
        r2, o2 = _drive_announce(tmp_path, payload=payload, out=out)
    finally:
        app._ANNOUNCE_LAST.clear()

    check("dedup: the first announcement plays", o1 == ["19"])
    check("dedup: the identical repeat does NOT play", o2 == [])
    check("dedup: the repeat still reports ok (nothing to retry)",
          getattr(r2, "status_code", 200) == 200)
    import json as _json
    rec = _json.loads(out.read_text().splitlines()[-1])
    # v0.106.0: the first copy is still on its way, so the repeat is PENDING —
    # kept as a retry candidate (it carries its clip) in case the first never
    # arrives, and retired by the retry's content check if it does.
    check("dedup: the suppression is recorded as pending behind the first copy",
          rec["outcome"] == "duplicate-pending" and rec["basis"] == "pending")
    check("dedup: the record carries the clip and the content tag",
          rec.get("sound") and len(rec["digest"]) == 12)


# --------------------------------------------------------------------------- #
# ★ 2026-09-15 — AN ANNOUNCEMENT THAT FAILED TO ORIGINATE, AND A GUARD THAT
#   COULD NOT JUDGE.
#
# 01:42:12Z, 8.4 s after an add-on restart. The webui queued an announcement to
# the cordless before ext 19 had re-registered. Asterisk logged
# `ast_sip_create_dialog_uac: Endpoint '19': Could not create dialog to invalid
# URI '19'` and `Failed to create outgoing session`, the clip never played, and
# nobody was told. Third occurrence of that shape.
#
# The pre-flight guard written for exactly this (device_unreachable) PASSED,
# because ami.get_device_state() returns "" when it cannot read the state and
# AMI was not answering yet — and an empty state is deliberately not
# "unreachable", so that a state-read hiccup can never silence an alarm. The
# fail-open is kept. What these pin is that it stops being SILENT, and that the
# rows carry the clip so they join to the announcement they are about.
# --------------------------------------------------------------------------- #
def _announce_rows(out):
    import json as _json
    return [_json.loads(l) for l in out.read_text().splitlines() if l.strip()]


def test_the_outcome_names_are_the_shared_ones() -> None:
    """These rows are read by a different program from the one that writes them,
    so app.py must be spelling `delivery`'s constants and not its own fallbacks.
    A silent divergence here is a ledger nobody can join — the exact failure the
    clip-name canonicalisation exists to prevent, one layer up."""
    check("names: the originate failure is delivery's constant",
          app.ANNOUNCE_ORIGINATE_FAILED == app._delivery.ANNOUNCE_ORIGINATE_FAILED)
    check("names: the unjudged guard is delivery's constant",
          app.ANNOUNCE_GUARD_UNJUDGED == app._delivery.ANNOUNCE_GUARD_UNJUDGED)
    check("names: the busy refusal is delivery's constant",
          app.ANNOUNCE_SKIPPED_BUSY == app._delivery.ANNOUNCE_SKIPPED_BUSY)
    check("names: the unreachable refusal is delivery's constant",
          app.ANNOUNCE_UNREACHABLE == app._delivery.ANNOUNCE_UNREACHABLE)
    check("names: both refusals are the retry's refusal set",
          set(app._delivery.ANNOUNCE_GUARD_REFUSED)
          == {app.ANNOUNCE_SKIPPED_BUSY, app.ANNOUNCE_UNREACHABLE})


def test_a_guard_refusal_carries_its_clip(tmp_path) -> None:
    """A refused announcement never played, so the scheduler's retry replays it —
    and the retry can only find a clip whose row NAMES it. Both refusals, driven
    through the real handler with the real device-state classification, must
    write the clip exactly as the Originate would have been given it, and must
    still not originate. (Before v0.105.2 both rows carried no clip, which was
    harmless only because the state read that feeds them came back "".)"""
    import re as _re
    for state, outcome, status in (("INUSE", "skipped-busy", 200),
                                   ("UNAVAILABLE", "unreachable", 503)):
        out = tmp_path / f"refusal-{outcome}.jsonl"
        payload = b"\0" * 44 + b"\1" * (16000 * 5)
        app._ANNOUNCE_LAST.clear()
        try:
            resp, originated = _drive_announce(tmp_path, payload=payload, out=out,
                                               state=state, real_state_guards=True)
        finally:
            app._ANNOUNCE_LAST.clear()
        rows = _announce_rows(out)
        check(f"refusal {outcome}: no Originate", originated == [])
        check(f"refusal {outcome}: status {status}",
              getattr(resp, "status_code", None) == status)
        check(f"refusal {outcome}: one row, the refusal",
              len(rows) == 1 and rows[0]["outcome"] == outcome)
        check(f"refusal {outcome}: the row names the clip the Originate would get",
              bool(_re.fullmatch(r"ann-19-[0-9a-f]{32}", rows[0].get("sound") or "")))
        check(f"refusal {outcome}: the device state is kept",
              rows[0].get("device_state") == state)


def test_an_originate_that_raises_is_recorded_against_its_clip(tmp_path) -> None:
    """The Originate raised. Before this the row said `originate-error` with no
    clip on it — the same name the WAKE-UP path writes for its own failures, and
    unjoinable to the announcement it belonged to."""
    out = tmp_path / "d3.jsonl"
    payload = b"\0" * 44 + b"\1" * (16000 * 5)
    app._ANNOUNCE_LAST.clear()

    def _boom(e, s):
        raise app.AMIError("Could not create dialog to invalid URI '19'")

    try:
        resp, originated = _drive_announce(tmp_path, payload=payload, out=out,
                                           originate=_boom)
    finally:
        app._ANNOUNCE_LAST.clear()
    check("raise: reported as a failure to the caller",
          getattr(resp, "status_code", None) == 502)
    check("raise: the Originate was attempted", originated == ["19"])
    rows = _announce_rows(out)
    check("raise: exactly one row for one attempt", len(rows) == 1)
    rec = rows[-1]
    check("raise: named as an announce originate failure",
          rec["outcome"] == app._delivery.ANNOUNCE_ORIGINATE_FAILED)
    check("raise: the row names the extension", rec["ext"] == "19")
    check("raise: the row carries the clip, so it joins",
          rec.get("sound", "").startswith("ann-19-"))
    check("raise: the row carries the reason", rec["reason"] == "ami-error")
    check("raise: and what Asterisk said", "invalid URI" in rec["detail"])
    check("raise: nothing claims the announcement was queued",
          all(r["outcome"] != app._delivery.ANNOUNCE_QUEUED for r in rows))


def test_an_originate_the_pbx_refuses_is_recorded_against_its_clip(tmp_path) -> None:
    """AMI answered and declined. Same row, different reason — one name per
    event with the cause ON it, because writing two rows for one attempt is a
    defect this repo has already shipped once (test_boundary_audit_fixes)."""
    out = tmp_path / "d4.jsonl"
    payload = b"\0" * 44 + b"\1" * (16000 * 5)
    app._ANNOUNCE_LAST.clear()
    try:
        resp, originated = _drive_announce(tmp_path, payload=payload, out=out,
                                           originate=lambda e, s: False)
    finally:
        app._ANNOUNCE_LAST.clear()
    check("refused: the caller is told it did not play",
          getattr(resp, "payload", {}).get("ok") is False)
    rows = _announce_rows(out)
    check("refused: exactly one row for one attempt", len(rows) == 1)
    rec = rows[-1]
    check("refused: named as an announce originate failure",
          rec["outcome"] == app._delivery.ANNOUNCE_ORIGINATE_FAILED)
    check("refused: the row carries the clip",
          rec.get("sound", "").startswith("ann-19-"))
    check("refused: the row says the PBX refused it", rec["reason"] == "refused")
    check("refused: a refusal carries no Asterisk text", "detail" not in rec)


def test_an_unreadable_device_state_records_that_the_guard_could_not_judge(tmp_path) -> None:
    """★ THE LIVE SHAPE. get_device_state() returns "" seconds after a restart.

    Two things must hold at once, and they pull in opposite directions: the
    announcement STILL GOES OUT (a guard that refuses because it could not ask
    would silence an alarm), and the fact that the check was skipped rather than
    passed is now on the record.

    The real device_busy / device_unreachable run here — with them stubbed this
    test would be asserting against its own lambdas.
    """
    out = tmp_path / "d5.jsonl"
    payload = b"\0" * 44 + b"\1" * (16000 * 5)
    app._ANNOUNCE_LAST.clear()
    try:
        resp, originated = _drive_announce(tmp_path, payload=payload, out=out,
                                           state="", real_state_guards=True)
    finally:
        app._ANNOUNCE_LAST.clear()
    check("unjudged: the announcement still went out", originated == ["19"])
    check("unjudged: and is reported as placed",
          getattr(resp, "payload", {}).get("ok") is True)
    rows = _announce_rows(out)
    check("unjudged: two rows — the note and the dispatch", len(rows) == 2)
    note, queued = rows
    check("unjudged: the guard's row comes first",
          note["outcome"] == app._delivery.ANNOUNCE_GUARD_UNJUDGED)
    check("unjudged: it says why", note["reason"] == "state-unreadable")
    check("unjudged: an empty state is omitted rather than written as ''",
          "device_state" not in note)
    check("unjudged: the announcement was still queued",
          queued["outcome"] == app._delivery.ANNOUNCE_QUEUED)
    check("unjudged: both rows name the SAME clip, so they join",
          note["sound"] == queued["sound"] and note["sound"].startswith("ann-19-"))


def test_a_state_asterisk_does_not_use_is_also_unjudged(tmp_path) -> None:
    """A spelling nothing classifies is exactly as much of an answer as no
    spelling at all. Without this, "" is the only case and a future Asterisk
    that renames a state would go back to failing open in silence."""
    out = tmp_path / "d6.jsonl"
    payload = b"\0" * 44 + b"\1" * (16000 * 5)
    app._ANNOUNCE_LAST.clear()
    try:
        _drive_announce(tmp_path, payload=payload, out=out,
                        state="NOT_A_REAL_STATE", real_state_guards=True)
    finally:
        app._ANNOUNCE_LAST.clear()
    rows = _announce_rows(out)
    check("unknown state: the guard records that it could not judge",
          rows[0]["outcome"] == app._delivery.ANNOUNCE_GUARD_UNJUDGED)
    check("unknown state: and the state it could not make sense of is kept",
          rows[0]["device_state"] == "NOT_A_REAL_STATE")


def test_a_normal_announcement_writes_nothing_new(tmp_path) -> None:
    """★ THE CONTROL, and the one that matters most.

    A registered, idle handset is the overwhelmingly common case. If the guard
    row appeared on those too it would be noise in the ledger every single time
    and a reader would learn to ignore it — which is the same as not having it.
    """
    out = tmp_path / "d7.jsonl"
    payload = b"\0" * 44 + b"\1" * (16000 * 5)
    app._ANNOUNCE_LAST.clear()
    try:
        resp, originated = _drive_announce(tmp_path, payload=payload, out=out,
                                           state="NOT_INUSE", real_state_guards=True)
    finally:
        app._ANNOUNCE_LAST.clear()
    check("normal: it played", originated == ["19"])
    rows = _announce_rows(out)
    check("normal: exactly one row", len(rows) == 1)
    check("normal: and it is the one the ledger has always had",
          rows[0]["outcome"] == app._delivery.ANNOUNCE_QUEUED)


def test_an_idle_handset_in_the_pretty_spelling_is_judged_too(tmp_path) -> None:
    """Asterisk spells the same state two ways ("NOT_INUSE" from DEVICE_STATE(),
    "Not in use" from PJSIPShowEndpoints). Treating one of them as unreadable
    would put the guard row on every ordinary announcement."""
    out = tmp_path / "d8.jsonl"
    payload = b"\0" * 44 + b"\1" * (16000 * 5)
    app._ANNOUNCE_LAST.clear()
    try:
        _drive_announce(tmp_path, payload=payload, out=out,
                        state="Not in use", real_state_guards=True)
    finally:
        app._ANNOUNCE_LAST.clear()
    rows = _announce_rows(out)
    check("pretty spelling: one row, and it is the dispatch",
          len(rows) == 1 and rows[0]["outcome"] == app._delivery.ANNOUNCE_QUEUED)


def test_an_unreachable_handset_is_still_refused_outright(tmp_path) -> None:
    """The new row must not have softened the guard that DOES have an answer.
    `UNAVAILABLE` means no contact: that announcement is still skipped, not
    announced-with-a-note."""
    out = tmp_path / "d9.jsonl"
    payload = b"\0" * 44 + b"\1" * (16000 * 5)
    app._ANNOUNCE_LAST.clear()
    try:
        resp, originated = _drive_announce(tmp_path, payload=payload, out=out,
                                           state="UNAVAILABLE", real_state_guards=True)
    finally:
        app._ANNOUNCE_LAST.clear()
    check("unreachable: no Originate was attempted", originated == [])
    check("unreachable: refused with 503",
          getattr(resp, "status_code", None) == 503)
    rows = _announce_rows(out)
    check("unreachable: one row, the refusal",
          len(rows) == 1 and rows[0]["outcome"] == "unreachable")


def test_the_suppression_window_starts_only_where_something_played() -> None:
    """★ The wiring, not the function.

    `_is_duplicate_announce` being pure is necessary and not sufficient: the
    window still has to be started in exactly one place, and that place has to
    be after every refusal path. A version that made the check pure and then
    called `_mark_announce_played` at the top would behave identically to the
    lockout it replaced, and every behavioural test above would still pass.

    The refusal paths this must sit below: too-long (413), duplicate,
    skipped-busy, unreachable (503), and both flavours of
    announce-originate-failed — the raise (502) and the refusal.

    announce-guard-unjudged is deliberately NOT in that list: it is a note that
    the pre-flight could not answer, not a refusal, and the announcement goes on
    to be dispatched. Adding it here would pin the opposite of what it means.
    """
    import inspect
    import re
    src = inspect.getsource(app)
    # A CALL passes arguments, so require a non-`)` after the paren — that
    # excludes the bare `_mark_announce_played()` written in prose in the
    # sibling docstring, which a looser pattern counts as a third call site.
    calls = [m.start() for m in re.finditer(r"(?<!def )_mark_announce_played\([^)]", src)]
    check(f"wiring: exactly one call site starts the window ({len(calls)})",
          len(calls) == 1)

    # It must come after the success record, which is the last thing on the
    # dispatch path — and therefore after every early return above it.
    queued = src.index('"announce", "originate-queued"')
    check("wiring: the window starts AFTER the announcement was dispatched",
          calls and calls[0] > queued)

    # And every refusal must return before reaching it.
    pos = src.index('"announce", "too-long"')
    check("wiring: the too-long path precedes the window start", pos < calls[0])
    # Both duplicate answers are written through delivery's constants (the retry
    # joins on them): the ledger's heard/pending row and the memory fallback's.
    for anchor in ("ANNOUNCE_DUPLICATE_SUPPRESSED if heard else ANNOUNCE_DUPLICATE_PENDING",
                   '"announce", ANNOUNCE_DUPLICATE_SUPPRESSED, basis="memory"'):
        pos = src.index(anchor)
        check(f"wiring: the duplicate path ({anchor[:40]}...) precedes the window start",
              pos < calls[0])
    # The two guard refusals are written through delivery's constants (the retry
    # joins on them), so they are matched by the constant's name.
    for const in ("ANNOUNCE_SKIPPED_BUSY", "ANNOUNCE_UNREACHABLE"):
        pos = src.index(f'"announce", {const}')
        check(f"wiring: the {const} path precedes the window start",
              pos < calls[0])
    # The two originate failures are written through the shared constant rather
    # than a literal (the name has to be spelled identically by the programs that
    # read the ledger), so they are matched by the constant's name. Both of them
    # — the raise and the refusal — must still sit above the window start.
    failed = [m.start() for m in
              re.finditer(r'"announce", ANNOUNCE_ORIGINATE_FAILED', src)]
    check(f"wiring: both originate-failure paths write the row ({len(failed)})",
          len(failed) == 2)
    for pos in failed:
        check("wiring: the announce-originate-failed path precedes the window start",
              pos < calls[0])

    # The checker itself must not write. A single assignment inside it is how
    # the lockout existed in the first place.
    body = src[src.index("def _is_duplicate_announce("):
               src.index("def _mark_announce_played(")]
    check("wiring: the duplicate CHECK does not touch the window",
          "_ANNOUNCE_LAST[" not in body)


def test_the_room_grid_is_not_rebuilt_while_a_field_is_focused() -> None:
    """★ Setting a wake-up in the panel was a race against the 4-second poll.

    `refresh()` replaces every room card wholesale, and each card carries a
    wake-up `<input type="time">`. Type the hour, and four seconds later the node
    you were typing into no longer existed and the box had snapped back to the
    server's value. Reported from live use: "the numbers keep overwriting."

    Save-and-restore is not available as a fix: a partially-entered
    `<input type="time">` reports `value === ""` until every segment is filled,
    so mid-entry there is nothing to preserve. The node has to be left alone.

    THIS IS A SOURCE-STRUCTURE ASSERTION, and it is a last resort. The panel's
    JavaScript lives inside a Python string literal and there is no DOM in this
    suite, so no behavioural test is available. It is written to pin the three
    things that would actually regress rather than to match a phrase: that the
    assignment is guarded, that the guard consults the focused element, and —
    the one a careless fix gets wrong — that the guard does NOT swallow the rest
    of the refresh.
    """
    html = app.INDEX_HTML
    body = html[html.index("async function refresh()"):html.index("async function refreshLights()")]

    assert "grid.innerHTML = gridHtml" in body, "the grid assignment was renamed"
    assert "grid.innerHTML = data.rooms.map" not in body, (
        "the room grid is assigned unconditionally again — a poll landing "
        "mid-entry will destroy the field being typed into")
    assert "if (!typingInGrid) grid.innerHTML" in body, "the assignment is not guarded"
    assert "document.activeElement" in body and "grid.contains(active)" in body, (
        "the guard does not consult the focused element")

    # ...and the guard must not become an early return. The active-call list and
    # the wake-up list render AFTER the grid; a `return` here would freeze both
    # for as long as a field is focused, which is a worse bug than the one being
    # fixed and looks identical in a screenshot.
    guard_at = body.index("const typingInGrid")
    assign_at = body.index("if (!typingInGrid) grid.innerHTML")
    between = body[guard_at:assign_at]
    assert "return;" not in between, (
        "the focus guard short-circuits refresh() instead of skipping just the "
        "grid assignment — the calls and wake-up lists would stop updating")
    for later in ("getElementById('calls')", "getElementById('wakeups')"):
        assert body.index(later) > assign_at, (
            f"{later} now renders before the grid assignment; the guard's "
            f"'everything else keeps updating' claim no longer holds")


def test_no_grid_action_depends_on_the_rebuild_to_re_enable_its_button() -> None:
    """★ The bug that made "clicking Set doesn't set anything" true.

    Every action in the room grid disabled its button, restored it only in the
    `catch`, and let the 4-second refresh replace the whole card on success.
    That worked by accident: a stranded button self-healed within four seconds.

    Once the grid stopped being rebuilt while a field inside it holds focus
    (v0.94.0, so typing a wake-up time is not destroyed mid-entry), the success
    path had nothing left to re-enable the button — and this handler opens with
    `if (!btn || btn.disabled) return;`, so a stranded button silently swallows
    every later click.

    It bites hardest exactly where the wake-up field is: macOS Safari and
    Firefox do not move focus to a `<button>` when it is clicked, so after
    typing a time the focus is STILL in the time input when refresh() runs, the
    grid is held, and the Set button never comes back.

    Source-structure assertion (the panel's JS lives in a Python string and
    there is no DOM here), but it pins the invariant rather than a phrase: the
    handler must own its button state and never write `btn.disabled` directly.
    """
    html = app.INDEX_HTML
    start = html.index("document.getElementById('rooms').addEventListener")
    end = html.index("document.getElementById('pageall').addEventListener")
    assert end > start, "handler slice is empty — this check would measure nothing"
    handler = html[start:end]

    assert "function busy(btn, label)" in html, "the busy() helper is gone"
    assert "btn.disabled = true" not in handler, (
        "a grid action disables its button directly again; on success nothing "
        "re-enables it while a field in the grid holds focus, and every later "
        "click on it is swallowed")
    assert "btn.disabled = false" not in handler, (
        "a grid action restores its button directly; use the done() callback so "
        "success and failure cannot diverge")

    # Every busy() must be paired — one call on success, one on failure.
    n_busy = handler.count("busy(btn")
    n_done = handler.count("done(")
    assert n_busy >= 6, f"expected at least 6 grid actions, found {n_busy}"
    assert n_done == n_busy * 2, (
        f"{n_busy} busy() sites but {n_done} done() calls — each action needs "
        f"one on the success path and one on the failure path; an unpaired site "
        f"is a button that never comes back")

    # The reported symptom specifically: setting a wake-up must confirm itself.
    assert "done('Set ✓')" in handler, (
        "the wake-up Set button gives no success feedback — a silent success is "
        "indistinguishable from a dead button, which is how this shipped")


def test_the_wakeup_time_field_cannot_be_squeezed_below_a_time() -> None:
    """★ Reported from live use: the time read "12:3" with no AM/PM.

    It looked like a truncated value; it was a too-small box. The field had
    `flex: 1 1 auto; min-width: 0` while the Set button beside it inherited
    `.ringbtn { width: 100% }` with no `.wakerow` override — so the button
    claimed the whole row and flexbox squeezed the input past its own content
    width. A native `<input type="time">` does not scroll or ellipsise when it
    is too narrow; it clips, and the first thing lost is the meridiem.
    """
    css = app.INDEX_HTML
    rule = css[css.index(".wakerow input[type=time]"):]
    rule = rule[:rule.index("}") + 1]
    assert "min-width: 0" not in rule, (
        "the wake-up time field can be shrunk below its content again — it will "
        "clip the AM/PM indicator with no visual sign that it did")
    assert "flex: 0 0 auto" in rule, "the field is allowed to shrink"
    import re
    m = re.search(r"min-width:\s*([\d.]+)rem", rule)
    assert m and float(m.group(1)) >= 6.0, (
        f"min-width is {m.group(1) if m else 'unset'}rem — too narrow for "
        f"'12:30 PM' plus the stepper the browser draws inside the field")
    assert ".wakerow .ringbtn" in css, (
        "the Set button has no .wakerow override, so it inherits width:100% and "
        "squeezes the field again")


def test_a_dashboard_wakeup_change_is_recorded_and_is_not_a_snooze(tmp_path) -> None:
    """★ 2026-09-14, through the real handlers.

    Two gaps from one morning. Ext 14's 05:50 wake-up escalated and no ledger
    could say who had set it; every dashboard set and cancel now writes a row,
    marked `web`. And ext 19 snoozed three ringing wake-ups from its own phone and
    was pushed anyway — the fix stands a ring down for the room's PHONE only, so a
    set from the dashboard during a ring, which may be somebody setting it for a
    sleeper, must still escalate. The control beside it makes the same change as
    a phone row, to show this set-up can see a snooze at all.
    """
    import asyncio as _aio
    import json as _json
    import time as _time

    check("dashboard: the ledger module loaded", app._delivery is not None)

    class _Req:
        headers = {}

        @staticmethod
        async def json():
            return {"hhmm": "06:20"}

    class _Resp:
        def __init__(self, payload, status_code=200):
            self.payload, self.status_code = payload, status_code

    def rows(path):
        return ([_json.loads(l) for l in path.read_text().splitlines() if l.strip()]
                if path.exists() else [])

    ledger = tmp_path / "delivery-outcomes.jsonl"
    saved = {k: getattr(app, k) for k in
             ("load_options", "configured_room_exts", "JSONResponse")}
    saved_out = app._delivery.OUTCOME_PATH
    saved_mods = {k: sys.modules.get(k) for k in ("store", "ami", "ha_client", "delivery")}
    saved_path = list(sys.path)
    try:
        app.JSONResponse = _Resp
        app.load_options = lambda: {}
        app.configured_room_exts = lambda o: {"19"}
        app._delivery.OUTCOME_PATH = str(ledger)

        resp = _aio.run(app.api_wakeup_set("19", _Req()))
        check("dashboard: the set succeeded", resp.status_code == 200)
        [s] = rows(ledger)
        check("dashboard: the set is recorded as web, with the time it rings",
              (s["kind"], s["outcome"], s["source"], s["hhmm"])
              == ("wakeup", "set", "web", "06:20")
              and s["target_epoch"] == app.wakeup_store.get("19")["target_epoch"])
        app.api_wakeup_cancel("19")
        c = rows(ledger)[-1]
        check("dashboard: the cancel is recorded as web, and says it removed one",
              (c["outcome"], c["source"], c["removed"]) == ("cancelled", "web", True))
        app.api_wakeup_cancel("19\r\nX")
        check("dashboard: a malformed extension never reaches the ledger",
              len(rows(ledger)) == 2)

        pushed, rang = [], []

        class _AMI:
            @staticmethod
            def get_endpoints():
                return [{"name": "19", "state": "Not in use"}]

            @staticmethod
            def originate_wakeup(ext, ring):
                rang.append(ext)
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
            sys.modules[k] = _AMI
        sys.modules["delivery"] = app._delivery
        sched = SourceFileLoader(
            "sched_dashboard_snooze",
            str(_ROOT / "rootfs" / "usr" / "share" / "switchboard" / "wakeup"
                / "scheduler.py")).load_module()
        sched.ami, sched.ha_client, sched.log = _AMI, _HA, lambda m: None

        def judge(change):
            """A second ring started 30 s ago; make `change`; judge it."""
            pushed.clear()
            rang.clear()
            sched._ringing.clear()
            started = _time.time() - 30
            sched._ringing["19"] = {"target_epoch": int(started), "hhmm": "06:10",
                                    "started": started, "retried": True}
            change()
            sched._reconcile_rings(started + sched.RETRY_AFTER + 1)
            return list(pushed)

        check("dashboard: a set from the dashboard during the ring still escalates",
              len(judge(lambda: _aio.run(app.api_wakeup_set("19", _Req())))) == 1)
        entry = app.wakeup_store.get("19")
        check("control: the same change from the room's own phone is a snooze",
              judge(lambda: app._delivery.record_wakeup_change(
                  "19", app._delivery.SOURCE_PHONE, entry=entry)) == [])
    finally:
        sys.path[:] = saved_path
        for k, v in saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        for k, v in saved.items():
            setattr(app, k, v)
        app._delivery.OUTCOME_PATH = saved_out
        try:
            app.wakeup_store.cancel("19")
        except Exception:
            pass


def _announce_bench(tmp_path, state):
    """api_announce, driven for real, with everything outside it stubbed.

    Returns (call, originated): `call(text)` runs one POST and hands back the
    response, `originated` collects the clips that reached the Originate. The
    device-state PREDICATES are the real ones — the whole question here is what
    the handler does when they cannot judge."""
    import asyncio as _aio
    import os as _os

    originated = []

    class _Req:
        headers = {}

        def __init__(self, text):
            self._text = text

        async def json(self):
            return {"text": self._text}

    class _Resp:
        def __init__(self, payload, status_code=200):
            self.payload, self.status_code = payload, status_code

    class _Renderer:
        """Byte-IDENTICAL output for identical text, which is what makes the
        content digest — and therefore the suppression window — meaningful."""
        @staticmethod
        def build_announcement_8k(text, path):
            with open(path, "wb") as fh:
                fh.write(b"\0" * 44 + text.encode() * 100)
            return True

    saved = {k: getattr(app, k) for k in
             ("load_options", "configured_room_exts", "valid_ext",
              "get_device_state", "announce_to_ext", "announce_asterisk",
              "ANNOUNCE_DIR", "JSONResponse")}
    saved_out = app._delivery.OUTCOME_PATH
    app.JSONResponse = _Resp
    app.announce_asterisk = _Renderer
    app.ANNOUNCE_DIR = str(tmp_path / "ann")
    _os.makedirs(app.ANNOUNCE_DIR, exist_ok=True)
    app.load_options = lambda: {}
    app.configured_room_exts = lambda o: {"19"}
    app.valid_ext = lambda e: True
    app.get_device_state = lambda e: state
    app.announce_to_ext = lambda e, s: originated.append(s) or True
    app._delivery.OUTCOME_PATH = str(tmp_path / "delivery.jsonl")
    app._ANNOUNCE_LAST.clear()

    def call(text="dinner is ready"):
        return _aio.run(app.api_announce("19", _Req(text)))

    def restore():
        for k, v in saved.items():
            setattr(app, k, v)
        app._delivery.OUTCOME_PATH = saved_out
        app._ANNOUNCE_LAST.clear()

    return call, originated, restore


def test_a_judged_announcement_still_arms_the_duplicate_window(tmp_path) -> None:
    """The window is unchanged for the normal case: the handset answered the
    pre-flight, the clip was dispatched, and an identical payload moments later is
    still suppressed. Without this the change below would be indistinguishable
    from deleting the suppression window."""
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        first = call()
        second = call()
    finally:
        restore()
    check("dedup: the first announcement is dispatched",
          getattr(first, "payload", {}).get("ok") is True and len(originated) == 1)
    check("dedup: an identical payload right after a PLAYBACK is suppressed",
          getattr(second, "payload", {}).get("skipped") == "duplicate")
    check("dedup: and it is not sent twice", len(originated) == 1)


def test_an_unjudged_announcement_does_not_arm_the_duplicate_window(tmp_path) -> None:
    """★ THE v0.85.0 LOCKOUT IN A NEW DRESS, and the 2026-09-15 incident armed it.

    _mark_announce_played() was called on AMI ACCEPTANCE. That night the
    reachability pre-flight read "" because AMI was not answering 8 s after a
    restart, the Originate was accepted, `originate-queued` was written — and
    nothing played, because ext 19 had no contact. The window was armed anyway, so
    an identical re-send from Home Assistant inside the 300 s window would have
    been answered {"ok": true, "skipped": "duplicate"} for audio nobody heard.

    The window means "this exact payload ALREADY PLAYED". When the guard could not
    judge, acceptance is not evidence that anything played, so the window stays
    shut and the producer's retry is answered on its merits. The only producers
    that re-send an identical payload are the alerting ones.
    """
    call, originated, restore = _announce_bench(tmp_path, "")      # AMI says nothing
    try:
        first = call()
        second = call()
        third = call()
    finally:
        restore()
    check("unjudged: the announcement still goes out (the guard fails open)",
          getattr(first, "payload", {}).get("ok") is True)
    check("unjudged: the in-memory window was not armed", "19" not in app._ANNOUNCE_LAST)
    # v0.106.0 — the lockout this test was written against was an identical
    # re-send answered "duplicate" and then DROPPED, for audio nobody heard. The
    # ledger now knows the first copy is still in flight, so a re-send is answered
    # "duplicate" but KEPT: recorded duplicate-pending with its clip, a retry
    # candidate that is replayed if the first copy never plays and retired if it
    # does. Not dropped, and not a second call on top of the first either.
    import json as _json
    recs = [_json.loads(l) for l in
            open(str(tmp_path / "delivery.jsonl"), encoding="utf-8").read().splitlines()
            if l.strip()]
    outcomes = [r["outcome"] for r in recs]
    check("unjudged: only the first reached the Originate", len(originated) == 1)
    check("unjudged: the re-sends are PENDING, never plain suppressed",
          outcomes.count("duplicate-pending") == 2 and "duplicate-suppressed" not in outcomes)
    check("unjudged: each pending copy keeps its own clip (a retry candidate)",
          all(r.get("sound") for r in recs if r["outcome"] == "duplicate-pending")
          and len({r["sound"] for r in recs if r["outcome"] == "duplicate-pending"}) == 2)
    check("unjudged: the first attempt recorded the unjudged guard, with its clip",
          outcomes.count("announce-guard-unjudged") == 1
          and all(r.get("sound") for r in recs if r["outcome"] == "announce-guard-unjudged"))


# --------------------------------------------------------------------------- #
# ★ THE LEDGER HALF OF THE DUPLICATE CHECK (v0.106.0). The in-memory window is
# armed only by an Originate from this process whose pre-flight judged; these
# drive the real handler past it (a cleared _ANNOUNCE_LAST stands in for a
# restart) and pin what the ledger decides, and that it decides NOTHING when it
# cannot answer.
# --------------------------------------------------------------------------- #
def _ledger_row(path, outcome, sound, ago, ext="19", **extra):
    import datetime as _dt
    import json as _json
    import time as _t
    ts = _dt.datetime.fromtimestamp(_t.time() - ago, _dt.timezone.utc)
    rec = {"ts": ts.isoformat(timespec="seconds"), "ext": ext, "kind": "announce",
           "outcome": outcome, "sound": sound}
    rec.update(extra)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(_json.dumps(rec) + "\n")


def _ledger_rows(tmp_path):
    import json as _json
    p = tmp_path / "delivery.jsonl"
    return ([_json.loads(l) for l in p.read_text().splitlines() if l.strip()]
            if p.exists() else [])


def _heard(tmp_path, sound):
    _ledger_row(tmp_path / "delivery.jsonl", "audio-delivered", sound, 0,
                stage="complete", txcount=300)


def test_an_identical_announcement_after_a_restart_is_a_duplicate(tmp_path) -> None:
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        call("dinner is ready")
        a = _ledger_rows(tmp_path)[-1]
        check("ledger dedupe: the ask row carries a 12-hex tag",
              a["outcome"] == "originate-queued" and len(a.get("digest") or "") == 12)
        _heard(tmp_path, a["sound"])
        app._ANNOUNCE_LAST.clear()                          # the restart
        r = call("dinner is ready")
    finally:
        restore()
    check("ledger dedupe: one Originate", len(originated) == 1)
    check("ledger dedupe: answered as a duplicate, unchanged shape",
          r.status_code == 200 and r.payload == {"ok": True, "skipped": "duplicate"})
    last = _ledger_rows(tmp_path)[-1]
    check("ledger dedupe: recorded with its basis and evidence",
          last["outcome"] == "duplicate-suppressed" and last["basis"] == "delivered"
          and last["matched"] == a["sound"] and last.get("delivered_at"))
    check("ledger dedupe: the suppression row carries its clip (the room's newest ask)",
          bool(last.get("sound")) and last["sound"] != a["sound"])


def test_the_content_tag_is_keyed_not_a_plain_hash(tmp_path) -> None:
    import hashlib as _h
    import os as _os
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        call("dinner is ready")
    finally:
        restore()
    a = _ledger_rows(tmp_path)[-1]
    payload = b"\0" * 44 + b"dinner is ready" * 100
    check("tag: not the plain sha256 prefix of the audio",
          a["digest"] != _h.sha256(payload).hexdigest()[:12])
    st = _os.stat(app.ANNOUNCE_TAG_KEY_PATH)
    check("tag: the key is 32 bytes, owner-only", st.st_mode & 0o077 == 0 and st.st_size == 32)


def test_the_memory_window_decides_only_when_the_ledger_cannot(tmp_path) -> None:
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    real = app._delivery.read_content_tail
    try:
        app._delivery.read_content_tail = lambda since, max_bytes=None: None
        call("dinner is ready")
        call("dinner is ready")                              # the in-memory window
    finally:
        app._delivery.read_content_tail = real
        restore()
    check("memory fallback: the repeat did not play", len(originated) == 1)
    a, d = _ledger_rows(tmp_path)[-2:]
    check("memory dedupe: suppressed with basis=memory",
          d["outcome"] == "duplicate-suppressed" and d["basis"] == "memory")
    check("memory dedupe: the same keyed tag, never a plain hash",
          d["digest"] == a["digest"])


def test_something_different_in_flight_is_not_a_duplicate(tmp_path) -> None:
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        call("door open")
        _heard(tmp_path, _ledger_rows(tmp_path)[-1]["sound"])
        call("door closed")                                  # dispatched, still playing
        app._ANNOUNCE_LAST.clear()
        r = call("door open")
    finally:
        restore()
    check("in flight: 'door open' after 'door closed' is said again",
          r.payload.get("skipped") != "duplicate" and len(originated) == 3)


def test_something_different_heard_since_is_not_a_duplicate(tmp_path) -> None:
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        call("door open")
        _heard(tmp_path, _ledger_rows(tmp_path)[-1]["sound"])
        call("door closed")
        _heard(tmp_path, _ledger_rows(tmp_path)[-1]["sound"])
        app._ANNOUNCE_LAST.clear()
        r = call("door open")
    finally:
        restore()
    check("sequential: open, closed, open are three announcements",
          r.payload.get("skipped") != "duplicate" and len(originated) == 3)


def test_the_ledger_is_read_after_the_state_read(tmp_path) -> None:
    """A delivery that lands while AMI is being asked must be seen."""
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        call("dinner is ready")
        a = _ledger_rows(tmp_path)[-1]
        app._ANNOUNCE_LAST.clear()

        def _state(_e):
            _heard(tmp_path, a["sound"])
            return "Not in use"
        app.get_device_state = _state
        r = call("dinner is ready")
    finally:
        restore()
    check("ordering: the delivery written during the state read suppresses",
          r.payload == {"ok": True, "skipped": "duplicate"} and len(originated) == 1)


def _tag_of(text):
    import hashlib as _h
    return app._content_tag(_h.sha256(b"\0" * 44 + text.encode() * 100).hexdigest())


def test_both_refusals_carry_the_content_tag(tmp_path) -> None:
    """The retry recognises a copy only by its tag, so both guard refusals must
    carry it — the unreachable one is the post-restart shape this release is for."""
    for state, outcome in (("In use", "skipped-busy"), ("Unavailable", "unreachable")):
        call, originated, restore = _announce_bench(tmp_path / outcome, state)
        try:
            call("x")
        finally:
            restore()
        b = _ledger_rows(tmp_path / outcome)[-1]
        check(f"{outcome}: refused, not originated",
              b["outcome"] == outcome and originated == [])
        check(f"{outcome}: carries the keyed tag of its content", b["digest"] == _tag_of("x"))


def test_the_tag_depends_on_the_key(tmp_path) -> None:
    """The privacy guarantee: the same audio under a different key gives a
    different tag, so a reader of /share without the key cannot compute it."""
    import hashlib as _h
    saved = app.ANNOUNCE_TAG_KEY_PATH
    digest = _h.sha256(b"door open").hexdigest()
    tags = []
    try:
        for i in (1, 2):
            app._TAG_KEY[0] = b""
            app.ANNOUNCE_TAG_KEY_PATH = str(tmp_path / f"key{i}")
            tags.append(app._content_tag(digest))
    finally:
        app.ANNOUNCE_TAG_KEY_PATH = saved
        app._TAG_KEY[0] = b""
    check("tag: 12 hex", all(len(t) == 12 for t in tags))
    check("tag: a different key gives a different tag", tags[0] != tags[1])


def test_an_identical_announcement_that_never_played_is_sent_again(tmp_path) -> None:
    """The first copy was handed over more than a horizon ago and never arrived:
    it can no longer be heard, so the same words are said now."""
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        p = tmp_path / "delivery.jsonl"
        horizon = app._delivery.ANNOUNCE_HORIZON
        _ledger_row(p, "originate-queued", "ann-19-" + "d" * 32, horizon + 20,
                    digest=_tag_of("x"))
        r = call("x")
    finally:
        restore()
    check("rang out: the second copy is originated",
          len(originated) == 1 and r.payload.get("skipped") is None)


def test_an_identical_copy_behind_one_still_in_flight_waits_for_it(tmp_path) -> None:
    """Handed over moments ago, not yet heard: the copy is answered duplicate but
    KEPT as a retry candidate (it may be the only one that plays)."""
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        p = tmp_path / "delivery.jsonl"
        first = "ann-19-" + "d" * 32
        _ledger_row(p, "originate-queued", first, 5, digest=_tag_of("x"))
        r = call("x")
    finally:
        restore()
    last = _ledger_rows(tmp_path)[-1]
    check("in flight: not originated, answered duplicate",
          originated == [] and r.payload == {"ok": True, "skipped": "duplicate"})
    check("in flight: recorded pending, with its clip and the one it waits for",
          last["outcome"] == "duplicate-pending" and last.get("sound")
          and last["matched"] == first and last.get("asked_at"))


def test_the_same_words_to_another_room_are_not_a_duplicate(tmp_path) -> None:
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        p = tmp_path / "delivery.jsonl"
        other = "ann-18-" + "c" * 32
        _ledger_row(p, "originate-queued", other, 5, ext="18", digest=_tag_of("x"))
        _ledger_row(p, "audio-delivered", other, 0, ext="18", stage="complete", txcount=300)
        call("x")
    finally:
        restore()
    check("other room: originated", len(originated) == 1)


def test_a_ledger_that_cannot_be_read_decides_nothing(tmp_path) -> None:
    import os as _os
    for kind in ("dir", "fifo", "link"):
        call, originated, restore = _announce_bench(tmp_path / kind, "Not in use")
        try:
            (tmp_path / kind).mkdir(exist_ok=True)
            target = tmp_path / kind / "planted"
            if kind == "dir":
                _os.mkdir(target)
            elif kind == "fifo":
                _os.mkfifo(target)
            else:
                # A link whose TARGET says "already heard": following it would
                # suppress, so this case can actually fail.
                real = tmp_path / kind / "real.jsonl"
                real.write_text("")
                _ledger_row(real, "originate-queued", "ann-19-" + "e" * 32, 30,
                            digest=_tag_of("x"))
                _ledger_row(real, "audio-delivered", "ann-19-" + "e" * 32, 10,
                            stage="complete", txcount=300)
                _os.symlink(real, target)
            app._delivery.OUTCOME_PATH = str(target)
            r = call("x")
            # ..."as before" means the in-memory window is still deciding.
            r2 = call("x")
        finally:
            restore()
        check(f"unreadable ({kind}): announced as before, no hang", len(originated) == 1)
        check(f"unreadable ({kind}): the memory window still catches the repeat",
              r2.payload == {"ok": True, "skipped": "duplicate"})


def test_a_duplicate_check_that_raises_decides_nothing(tmp_path) -> None:
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    real = app._delivery.content_verdict
    try:
        app._delivery.content_verdict = lambda *a, **k: 1 / 0
        call("x")
    finally:
        app._delivery.content_verdict = real
        restore()
    check("raising predicate: announced as before", len(originated) == 1)


def test_no_key_means_no_tag_and_memory_only(tmp_path) -> None:
    import os as _os
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    saved = app.ANNOUNCE_TAG_KEY_PATH
    try:
        app._TAG_KEY[0] = b""
        # A key path whose parent is a FILE: ENOTDIR, for root as well.
        (tmp_path / "notadir").write_text("x")
        app.ANNOUNCE_TAG_KEY_PATH = str(tmp_path / "notadir" / "k")
        call("x")
        a = _ledger_rows(tmp_path)[-1]
        call("x")                                   # ...and the repeat
        b = _ledger_rows(tmp_path)[-1]
    finally:
        app.ANNOUNCE_TAG_KEY_PATH = saved
        app._TAG_KEY[0] = b""
        restore()
    check("no key: the first announcement plays", len(originated) == 1)
    check("no key: no row carries a tag", "digest" not in a)
    check("no key: identical repeats are still caught, in memory",
          b["outcome"] == "duplicate-suppressed" and b["basis"] == "memory"
          and "digest" not in b)


def test_a_planted_link_in_place_of_the_key_is_refused(tmp_path) -> None:
    import os as _os
    saved = app.ANNOUNCE_TAG_KEY_PATH
    try:
        app._TAG_KEY[0] = b""
        (tmp_path / "attacker").write_bytes(b"k" * 32)
        _os.symlink(tmp_path / "attacker", tmp_path / "key")
        app.ANNOUNCE_TAG_KEY_PATH = str(tmp_path / "key")
        check("key link: refused, no tag", app._content_tag("ab" * 32) == "")
        check("key link: the target was not used or replaced",
              (tmp_path / "attacker").read_bytes() == b"k" * 32
              and _os.path.islink(tmp_path / "key"))
    finally:
        app.ANNOUNCE_TAG_KEY_PATH = saved
        app._TAG_KEY[0] = b""


def test_the_duplicate_window_bounds_the_ledger_half(tmp_path) -> None:
    """Five minutes, either side of it — and 0 turns the ledger half off."""
    W = app.ANNOUNCE_DEDUP_WINDOW_S
    check("window: taken from delivery, not a second copy",
          W == app._delivery.ANNOUNCE_DEDUP_WINDOW_S and W == 300)
    for ago, dup in ((W - 30, True), (W + 30, False)):
        call, originated, restore = _announce_bench(tmp_path / f"w{ago}", "Not in use")
        try:
            p = tmp_path / f"w{ago}" / "delivery.jsonl"
            old = "ann-19-" + "f" * 32
            _ledger_row(p, "originate-queued", old, ago + 20, digest=_tag_of("x"))
            _ledger_row(p, "audio-delivered", old, ago, stage="complete", txcount=300)
            r = call("x")
        finally:
            restore()
        check(f"window: heard {int(ago)}s ago -> duplicate={dup}",
              (r.payload.get("skipped") == "duplicate") is dup)
    # ...and with the window at 0 the ledger is never consulted.
    call, originated, restore = _announce_bench(tmp_path / "off", "Not in use")
    saved = app.ANNOUNCE_DEDUP_WINDOW_S
    try:
        app.ANNOUNCE_DEDUP_WINDOW_S = 0
        p = tmp_path / "off" / "delivery.jsonl"
        old = "ann-19-" + "f" * 32
        _ledger_row(p, "originate-queued", old, 30, digest=_tag_of("x"))
        _ledger_row(p, "audio-delivered", old, 10, stage="complete", txcount=300)
        r = call("x")
    finally:
        app.ANNOUNCE_DEDUP_WINDOW_S = saved
        restore()
    check("window: 0 turns the ledger half off", r.payload.get("skipped") is None
          and len(originated) == 1)


def test_the_read_reaches_back_far_enough_to_find_the_ask(tmp_path) -> None:
    """A replayed clip is heard long after it was ASKED for. The read floor must
    reach back past the window by the retry's age cap plus a playing horizon, or
    the delivery is read with no ask row to give it a content tag."""
    reach = app._delivery.CONTENT_ASK_REACH
    check("reach: the retry's age cap plus a horizon",
          reach == app._delivery.ANNOUNCE_RETRY_MAX_AGE + app._delivery.ANNOUNCE_HORIZON)
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        p = tmp_path / "delivery.jsonl"
        slow = "ann-19-" + "9" * 32
        _ledger_row(p, "originate-queued", slow, 400, digest=_tag_of("x"))   # outside the window
        _ledger_row(p, "announce-retry-attempted", slow, 120, attempt=1)
        _ledger_row(p, "audio-delivered", slow, 60, stage="complete", txcount=300)
        r = call("x")
    finally:
        restore()
    check("reach: the old ask row is still read, so the delivery counts",
          r.payload.get("skipped") == "duplicate" and originated == [])


def test_a_duplicate_is_answered_before_the_busy_and_unreachable_guards(tmp_path) -> None:
    """DOCS: a duplicate is answered as a duplicate whatever the handset is doing."""
    for state in ("In use", "Unavailable"):
        call, originated, restore = _announce_bench(tmp_path / state, state)
        try:
            p = tmp_path / state / "delivery.jsonl"
            old = "ann-19-" + "7" * 32
            _ledger_row(p, "originate-queued", old, 40, digest=_tag_of("x"))
            _ledger_row(p, "audio-delivered", old, 20, stage="complete", txcount=300)
            r = call("x")
        finally:
            restore()
        rows = _ledger_rows(tmp_path / state)
        check(f"order: {state} still answers duplicate",
              r.payload == {"ok": True, "skipped": "duplicate"})
        check(f"order: {state} records the duplicate, not the refusal",
              rows[-1]["outcome"] == "duplicate-suppressed")


def test_a_key_that_cannot_be_written_leaves_nothing_behind(tmp_path) -> None:
    """A half-made key must never be renamed into place, and no temp file may
    accumulate in /data when the write or the rename fails."""
    import os as _os
    saved, saved_replace = app.ANNOUNCE_TAG_KEY_PATH, _os.replace
    try:
        app._TAG_KEY[0] = b""
        app.ANNOUNCE_TAG_KEY_PATH = str(tmp_path / "keydir" / "k")
        _os.makedirs(tmp_path / "keydir")
        app.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("no rename"))
        check("key: a failed rename yields no tag", app._content_tag("ab" * 32) == "")
        check("key: and no key file", not (tmp_path / "keydir" / "k").exists())
        check("key: and no temp file left behind",
              [p.name for p in (tmp_path / "keydir").iterdir()] == [])
    finally:
        app.os.replace = saved_replace
        app.ANNOUNCE_TAG_KEY_PATH = saved
        app._TAG_KEY[0] = b""
    # ...and with the rename working, the key is created once and reused.
    try:
        app._TAG_KEY[0] = b""
        app.ANNOUNCE_TAG_KEY_PATH = str(tmp_path / "keydir" / "k2")
        first = app._content_tag("ab" * 32)
        app._TAG_KEY[0] = b""                        # a restart: read it back
        check("key: reused across a restart, same tag",
              first and app._content_tag("ab" * 32) == first)
        check("key: exactly one file", [p.name for p in (tmp_path / "keydir").iterdir()] == ["k2"])
    finally:
        app.ANNOUNCE_TAG_KEY_PATH = saved
        app._TAG_KEY[0] = b""


def test_a_ledger_full_of_garbage_is_refused_rather_than_ground_through(tmp_path) -> None:
    """Anything that can write /share can fill the ledger. The read must give up,
    not parse a megabyte of it on the announce path."""
    D = app._delivery
    p = tmp_path / "garbage.jsonl"
    p.write_text("x\n" * (D.CONTENT_TAIL_MAX_BAD + 50))
    saved = D.OUTCOME_PATH
    try:
        D.OUTCOME_PATH = str(p)
        check("garbage: refused (None), not an empty answer",
              D.read_content_tail(0) is None)
        p.write_text("x\n" * 3)
        check("garbage: a few bad lines are still tolerated", D.read_content_tail(0) == [])
    finally:
        D.OUTCOME_PATH = saved


def test_the_tail_reader_takes_the_newest_bytes(tmp_path) -> None:
    D = app._delivery
    p = tmp_path / "big.jsonl"
    for i in range(200):
        _ledger_row(p, "originate-queued", f"ann-19-{i:032d}", 100 - i * 0.1)
    saved = D.OUTCOME_PATH
    try:
        D.OUTCOME_PATH = str(p)
        tail = D.read_content_tail(0, max_bytes=2000)
        check("tail: bounded by max_bytes", 0 < len(tail) < 200)
        check("tail: the NEWEST rows, oldest-first, no partial line",
              tail[-1]["sound"] == "ann-19-" + "0" * 29 + "199"
              and all(r.get("ts") for r in tail))
    finally:
        D.OUTCOME_PATH = saved


def test_the_ledger_overrides_the_memory_window(tmp_path) -> None:
    """★ The memory window remembers only "this web UI sent that audio", which is
    not the same as "the room heard it". Here it did NOT arrive — the reconciler
    filed announce-undelivered — so the same words must be said again even though
    memory holds them. Whenever the ledger can answer, it is the one that decides."""
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        call("x")
        first = _ledger_rows(tmp_path)[-1]
        check("override: the first copy armed the memory window",
              "19" in app._ANNOUNCE_LAST)
        _ledger_row(tmp_path / "delivery.jsonl", "announce-undelivered",
                    first["sound"], 0, reason="ring-no-answer")
        r = call("x")
    finally:
        restore()
    check("override: the second copy is announced, not called a duplicate",
          len(originated) == 2 and r.payload.get("skipped") is None)


def test_a_ledger_with_no_history_for_this_room_decides_nothing(tmp_path) -> None:
    """★ An emptied, rotated or unwritable ledger reads exactly like a room that
    has never been announced to. Answering "not a duplicate" from that would turn
    the memory window off as well — the only half left when rows are not being
    written. Unless this room's own history is there, memory decides."""
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    real = app._delivery.record
    try:
        app._delivery.record = lambda *a, **k: False          # every write fails
        call("x")
        r2 = call("x")
    finally:
        app._delivery.record = real
        restore()
    check("no history: the first copy played", len(originated) == 1)
    check("no history: the repeat is still caught in memory",
          r2.payload == {"ok": True, "skipped": "duplicate"})


def test_a_room_with_history_is_judged_from_the_ledger(tmp_path) -> None:
    """The other side of it: rows for THIS room are evidence, so the ledger
    decides and a first-ever announcement is not called a duplicate."""
    call, originated, restore = _announce_bench(tmp_path, "Not in use")
    try:
        p = tmp_path / "delivery.jsonl"
        _ledger_row(p, "originate-queued", "ann-19-" + "5" * 32, 40, digest=_tag_of("y"))
        _ledger_row(p, "audio-delivered", "ann-19-" + "5" * 32, 20,
                    stage="complete", txcount=300)
        r = call("x")                                        # different content
    finally:
        restore()
    check("history: different content is announced", len(originated) == 1
          and r.payload.get("skipped") is None)


def test_the_key_file_shape_is_checked(tmp_path) -> None:
    import os as _os
    saved = app.ANNOUNCE_TAG_KEY_PATH
    try:
        # A FIFO must not hang the open, and must not be used as a key.
        app._TAG_KEY[0] = b""
        _os.mkfifo(tmp_path / "fifo")
        app.ANNOUNCE_TAG_KEY_PATH = str(tmp_path / "fifo")
        check("key: a FIFO is refused, not read", app._content_tag("ab" * 32) == "")
        # A file of the wrong size is replaced by a fresh 32-byte key.
        app._TAG_KEY[0] = b""
        short = tmp_path / "short"
        short.write_bytes(b"x" * 8)
        app.ANNOUNCE_TAG_KEY_PATH = str(short)
        tag = app._content_tag("ab" * 32)
        check("key: a wrong-size key is regenerated",
              len(tag) == 12 and short.stat().st_size == 32)
        # A short write must never be renamed into place.
        app._TAG_KEY[0] = b""
        app.ANNOUNCE_TAG_KEY_PATH = str(tmp_path / "keyd" / "k")
        _os.makedirs(tmp_path / "keyd")
        real_write = app.os.write
        app.os.write = lambda fd, data: real_write(fd, data[:8])
        try:
            check("key: a short write yields no tag", app._content_tag("ab" * 32) == "")
            check("key: and no file of any kind is left",
                  [p.name for p in (tmp_path / "keyd").iterdir()] == [])
        finally:
            app.os.write = real_write
    finally:
        app.ANNOUNCE_TAG_KEY_PATH = saved
        app._TAG_KEY[0] = b""


def test_the_tail_reader_drops_only_the_partial_first_line(tmp_path) -> None:
    D = app._delivery
    p = tmp_path / "cut.jsonl"
    for i in range(50):
        _ledger_row(p, "originate-queued", f"ann-19-{i:032d}", 50 - i)
    saved = D.OUTCOME_PATH
    try:
        D.OUTCOME_PATH = str(p)
        whole = D.read_content_tail(0)
        size = p.stat().st_size
        one = len(p.read_text().splitlines()[-1]) + 1
        cut = D.read_content_tail(0, max_bytes=int(one * 4.5))   # lands mid-line
        check("cut: the newest rows only, in order",
              cut == whole[-4:] and [r["sound"] for r in cut] == [r["sound"] for r in whole[-4:]])
        check("cut: nothing malformed survives the cut",
              all(r.get("sound") and r.get("_ts") for r in cut) and size > one * 4.5)
    finally:
        D.OUTCOME_PATH = saved
