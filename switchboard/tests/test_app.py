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


def _drive_announce(tmp_path, *, payload: bytes, state="NOT_INUSE", out=None):
    """Run the real api_announce handler with the renderer producing `payload`.

    The guards live INSIDE the handler, so testing the helper functions proves
    nothing about whether they run -- mutation testing showed both call sites
    surviving while the helpers were fully covered."""
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
        app.device_busy = lambda s: False
        app.device_unreachable = lambda s: False
        app.get_device_state = lambda e: state
        app.announce_to_ext = lambda e, s: originated.append(e) or True
        if out is not None:
            app._delivery.OUTCOME_PATH = str(out)
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
    check("dedup: the suppression is recorded",
          rec["outcome"] == "duplicate-suppressed")
    check("dedup: the record carries the payload digest", len(rec["digest"]) == 12)


def test_the_suppression_window_starts_only_where_something_played() -> None:
    """★ The wiring, not the function.

    `_is_duplicate_announce` being pure is necessary and not sufficient: the
    window still has to be started in exactly one place, and that place has to
    be after every refusal path. A version that made the check pure and then
    called `_mark_announce_played` at the top would behave identically to the
    lockout it replaced, and every behavioural test above would still pass.

    The refusal paths this must sit below: too-long (413), duplicate,
    skipped-busy, unreachable (503), originate-error (502), originate-refused.
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
    for outcome in ("too-long", "duplicate-suppressed", "skipped-busy",
                    "unreachable", "originate-error", "originate-refused"):
        pos = src.index(f'"announce", "{outcome}"')
        check(f"wiring: the {outcome} path precedes the window start",
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
