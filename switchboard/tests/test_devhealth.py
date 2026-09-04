"""Behavioral tests for the device-health monitor's pure logic.

    python3 switchboard/tests/test_devhealth.py

Pins classify_cordless (the ok/degraded/critical rules that decide whether the alarm
cordless is healthy), classify_gateway (deriving GXW health from which ports are down),
health_transition (the alert state machine), and last_call_mos (newest-call MOS, recency-gated).
The WP826 HTTP client + the poll loop are I/O and are not exercised here (mirrors how
test_rtpmon.py leaves the AMI socket untested).
"""
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share" / "switchboard" / "devhealth" / "poller.py"
dh = SourceFileLoader("devhealth_poller", str(_SRC)).load_module()

_failures = 0


def check(name, cond):
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    # Under pytest the print + counter are DECORATIVE — only the __main__
    # runner reads _failures, so a failing check would still 'pass' the
    # test. Assert too, so both harnesses actually enforce every check.
    assert cond, name


TH = {"battery_crit": 15, "battery_warn": 30, "wifi_min": 2, "mos_min": 3.4, "mos_window": 900}


def test_classify_cordless():
    # Healthy: registered/reachable, charging-ish, good wifi, good MOS.
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
         "wifi_connected": True, "wifi_signal": 4, "last_mos": 4.4}, TH)
    check("cordless: all-good -> ok", lvl == "ok" and why == [])

    # Fully offline (no TCP, no API) -> CRITICAL (the alarm endpoint is gone).
    lvl, why = dh.classify_cordless({"reachable": False, "api_ok": False}, TH)
    check("cordless: unreachable -> critical", lvl == "critical" and "offline" in why[0])

    # Battery low AND discharging under crit -> CRITICAL (imminent drop).
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 10, "charging": False,
         "wifi_connected": True, "wifi_signal": 4}, TH)
    check("cordless: battery 10% discharging -> critical", lvl == "critical" and any("battery" in r for r in why))

    # Battery low but CHARGING -> not a battery alarm (only wifi/other could degrade).
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 10, "charging": True,
         "wifi_connected": True, "wifi_signal": 4}, TH)
    check("cordless: battery 10% but charging -> ok", lvl == "ok")

    # Battery in warn band, discharging -> DEGRADED (not critical).
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 25, "charging": False,
         "wifi_connected": True, "wifi_signal": 4}, TH)
    check("cordless: battery 25% discharging -> degraded", lvl == "degraded" and any("low" in r for r in why))

    # Wi-Fi disconnected -> DEGRADED.
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
         "wifi_connected": False}, TH)
    check("cordless: wifi disconnected -> degraded", lvl == "degraded" and any("Wi-Fi disconnected" in r for r in why))

    # Weak Wi-Fi signal -> DEGRADED.
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
         "wifi_connected": True, "wifi_signal": 1}, TH)
    check("cordless: weak wifi -> degraded", lvl == "degraded" and any("weak" in r for r in why))

    # RECENT poor MOS (last call 30s ago) -> DEGRADED.
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
         "wifi_connected": True, "wifi_signal": 4, "last_mos": 2.9, "last_mos_age_s": 30}, TH)
    check("cordless: recent poor MOS -> degraded", lvl == "degraded" and any("MOS" in r for r in why))

    # STALE poor MOS (last call 2h ago) must NOT flag — an old bad call can't pin it degraded.
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
         "wifi_connected": True, "wifi_signal": 4, "last_mos": 2.9, "last_mos_age_s": 7200}, TH)
    check("cordless: stale poor MOS -> ok (not latched)", lvl == "ok")

    # Poor MOS with unknown age -> conservatively NOT flagged.
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
         "wifi_connected": True, "wifi_signal": 4, "last_mos": 2.9}, TH)
    check("cordless: poor MOS unknown age -> ok", lvl == "ok")

    # Answers TCP but API auth fails -> DEGRADED (can't read deep health), NOT critical.
    lvl, why = dh.classify_cordless({"reachable": True, "api_ok": False}, TH)
    check("cordless: reachable but API unreadable -> degraded", lvl == "degraded" and any("password" in r for r in why))


def test_classify_gateway():
    gw = ["11", "12", "13", "14", "15", "16", "17", "18"]
    check("gateway: none down -> ok", dh.classify_gateway([], gw)[0] == "ok")
    check("gateway: a non-gateway ext down (20) -> ok", dh.classify_gateway(["20"], gw)[0] == "ok")
    lvl, why = dh.classify_gateway(["13"], gw)
    check("gateway: one port down -> degraded", lvl == "degraded" and "1 of 8" in why[0])
    lvl, why = dh.classify_gateway(gw, gw)
    check("gateway: all ports down -> critical", lvl == "critical" and "GXW" in why[0])
    check("gateway: no gateway configured -> ok", dh.classify_gateway(["11"], [])[0] == "ok")


def test_health_transition():
    # Needs MIN_CYCLES consecutive unhealthy cycles before firing (rejects a blip).
    st = {}
    check("transition: 1st degraded cycle -> silent", dh.health_transition("degraded", st) == "")
    check("transition: 2nd degraded cycle -> fire 'degraded'", dh.health_transition("degraded", st) == "degraded")
    check("transition: 3rd degraded (already alerted) -> silent", dh.health_transition("degraded", st) == "")
    # Escalation degraded -> critical re-alerts (after its own cycles).
    dh.health_transition("critical", st)
    check("transition: critical escalation fires once", dh.health_transition("critical", st) == "critical")
    # Recovery fires once.
    check("transition: back to ok -> 'recovered'", dh.health_transition("ok", st) == "recovered")
    check("transition: staying ok -> silent", dh.health_transition("ok", st) == "")

    # A single degraded blip that clears next cycle never fires.
    st2 = {}
    dh.health_transition("degraded", st2)      # cycle 1
    check("transition: blip then ok -> never fired", dh.health_transition("ok", st2) == "" and not st2.get("alerted"))

    # A critical that persists 2 cycles fires 'critical' directly (no degraded first).
    st3 = {}
    dh.health_transition("critical", st3)
    check("transition: critical x2 -> fire critical", dh.health_transition("critical", st3) == "critical")


def test_last_call_mos():
    # Newest by stopTimeSecond wins (NOT the min) — record1 is the most recent call.
    rtp = {"record0": {"moscq": "4.4", "stopTimeSecond": "1000"},
           "record1": {"moscq": "3.1", "stopTimeSecond": "2000"},
           "record2": {"moscq": "bad", "stopTimeSecond": "3000"}}
    mos, age = dh.last_call_mos(rtp, now=2050)
    check("mos: picks the NEWEST call's moscq (not min)", mos == 3.1 and age == 50)
    check("mos: empty -> (None, None)", dh.last_call_mos({}) == (None, None))
    # An older good call doesn't get shadowed by an even-older bad one.
    mos2, _ = dh.last_call_mos({"a": {"moscq": "2.0", "stopTimeSecond": "10"},
                                "b": {"moscq": "4.5", "stopTimeSecond": "99"}}, now=100)
    check("mos: newest-good over older-bad", mos2 == 4.5)


def test_resolve_cordless_ip():
    # DHCP auto-follow: the probe IP comes from the cordless's live SIP registration
    # (rtpmon publishes contact_ip on sensor.switchboard_link_<ext>), with the static
    # cordless_ip as the fallback for every unavailable case.
    check("resolve: no cordless_ext -> static fallback (opt-out)",
          dh.resolve_cordless_ip("", "192.168.1.71") == "192.168.1.71")

    class _FakeHA:
        _state = None
        last = None
        @staticmethod
        def get_state(eid):
            _FakeHA.last = eid
            return _FakeHA._state
    sys.modules["ha_client"] = _FakeHA
    try:
        _FakeHA._state = {"state": "9.98", "attributes": {"contact_ip": "192.168.1.84", "registered": True}}
        check("resolve: follows the cordless's live registration IP",
              dh.resolve_cordless_ip("19", "192.168.1.71") == "192.168.1.84")
        check("resolve: reads the cordless's own link sensor",
              _FakeHA.last == "sensor.switchboard_link_19")
        _FakeHA._state = {"state": "offline", "attributes": {"registered": False}}
        check("resolve: cordless de-registered (no contact_ip) -> fallback",
              dh.resolve_cordless_ip("19", "192.168.1.71") == "192.168.1.71")
        _FakeHA._state = None  # rtpmon off / HA down / sensor not yet created
        check("resolve: sensor missing -> fallback",
              dh.resolve_cordless_ip("19", "192.168.1.71") == "192.168.1.71")
    finally:
        sys.modules.pop("ha_client", None)


def test_rollup_staleness_gate() -> None:
    """A pushed HA sensor never expires — if rtpmon dies while HA stays up, the
    link-health rollup freezes at its last reading and every consumer keeps
    treating it as current. Gateway health is DERIVED from that rollup, so a
    snapshot frozen mid-restart is republished as a live 'degraded' gateway
    (exactly what produced two false 4-minute alarms on 2026-08-11)."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    def stamped(age_s, interval=300):
        return {"measured_at": (now - _dt.timedelta(seconds=age_s)).isoformat(timespec="seconds"),
                "poll_interval_s": interval}

    check("stale: a just-published rollup is fresh", dh.rollup_is_stale(stamped(0)) is False)
    check("stale: 2 intervals old is still fresh (one missed poll is normal)",
          dh.rollup_is_stale(stamped(600)) is False)
    check("stale: beyond 2.5 intervals is stale", dh.rollup_is_stale(stamped(800)) is True)
    check("stale: hours old is stale", dh.rollup_is_stale(stamped(20000)) is True)
    # Judged against the rollup's OWN advertised interval, so changing
    # link_health_interval can't silently disable the gate.
    check("stale: honours a faster advertised interval",
          dh.rollup_is_stale(stamped(400, interval=60)) is True)
    check("stale: honours a slower advertised interval",
          dh.rollup_is_stale(stamped(400, interval=3600)) is False)
    # Backward/forward compatibility: never refuse to work with a rollup that
    # simply has no stamp (an older rtpmon), and never crash on a bad one.
    check("stale: unstamped rollup treated as fresh (old rtpmon)",
          dh.rollup_is_stale({}) is False)
    check("stale: unparseable stamp treated as fresh, not a crash",
          dh.rollup_is_stale({"measured_at": "not-a-date"}) is False)
    check("stale: bad poll_interval falls back to the default",
          dh.rollup_is_stale(stamped(800, interval="nonsense")) is True)


def test_blind_gateway_publishes_unknown_not_silence() -> None:
    """When the rollup is stale/absent the sensor must SAY unknown.

    v0.51.0 added the staleness check but then simply skipped the publish — and
    for a PUSHED Home Assistant sensor, not publishing means the last value
    stands. A monitor that has gone blind kept showing green, which is the exact
    fail-open shape the staleness check was written to close."""
    sets = []

    class _HA:
        @staticmethod
        def set_state(eid, state, attrs=None):
            sets.append((eid, state, attrs or {})); return True

    import sys as _sys
    _sys.modules["ha_client"] = _HA
    try:
        dh._publish_gateway_unknown("rollup stale", ["11", "12", "13"])
        check("blind: publishes exactly one state", len(sets) == 1)
        eid, state, attrs = sets[0]
        check("blind: to the gateway sensor", eid == "sensor.switchboard_gateway_health")
        check("blind: state is the literal 'unknown'", state == "unknown")
        check("blind: health attribute agrees", attrs.get("health") == "unknown")
        check("blind: carries the reason", "stale" in " ".join(attrs.get("reasons") or []))
        check("blind: does NOT claim ports are up", "ports_up" not in attrs)
        check("blind: keeps the port total for context", attrs.get("ports_total") == 3)
    finally:
        _sys.modules.pop("ha_client", None)


def test_blind_reset_keeps_the_alert_latch_so_recovery_still_fires() -> None:
    # Going blind must not silently clear an outstanding alert: if we paged
    # 'degraded' and then lost sight of the fleet, the eventual return to ok
    # still owes the user a 'recovered'.
    st = {"cycles": 0, "level": None, "alerted": None}
    for _ in range(2):
        dh.health_transition("degraded", st)
    check("latch: degraded alerted", st.get("alerted") == "degraded")
    st["level"] = None; st["cycles"] = 0          # what the blind branch does
    check("latch: survives the blind reset", st.get("alerted") == "degraded")
    check("latch: recovery still fires after blindness",
          dh.health_transition("ok", st) == "recovered")


if __name__ == "__main__":
    test_classify_cordless()
    test_classify_gateway()
    test_health_transition()
    test_last_call_mos()
    test_resolve_cordless_ip()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)


# ── WP826 certificate pinning (v0.46.0) ──────────────────────────────────────

def test_normalize_pin_accepts_pasted_shapes() -> None:
    want = "ab" * 32
    for shape in (want, want.upper(), "sha256:" + want,
                  ":".join(want[i:i + 2] for i in range(0, 64, 2)),
                  " ".join(want[i:i + 2] for i in range(0, 64, 2)),
                  f"  {want}  "):
        assert dh.normalize_pin(shape) == want, shape
    assert dh.normalize_pin("") == ""
    assert dh.normalize_pin(None) == ""


def test_cert_fingerprint_is_sha256_of_der() -> None:
    import hashlib
    der = b"\x30\x82 not a real cert, but bytes are bytes"
    assert dh.cert_fingerprint(der) == hashlib.sha256(der).hexdigest()


def test_pin_matches() -> None:
    der = b"the-handset-certificate"
    good = dh.cert_fingerprint(der)
    assert dh.pin_matches(good, der)
    assert dh.pin_matches(good.upper(), der)                  # case-insensitive
    assert dh.pin_matches("sha256:" + good, der)              # prefixed
    assert dh.pin_matches(":".join(good[i:i+2] for i in range(0, 64, 2)), der)
    # A DIFFERENT certificate must not satisfy the pin — this is the whole point.
    assert not dh.pin_matches(good, b"an-impostor-certificate")
    assert not dh.pin_matches("0" * 64, der)
    # Pinning is opt-in: an empty pin keeps existing installs working.
    assert dh.pin_matches("", der)
    assert dh.pin_matches("   ", der)


def test_probe_cordless_refuses_wrong_cert_before_sending_password(monkeypatch) -> None:
    """The pin must be checked BEFORE the login body (which carries the admin
    password) is written — a mismatched handset must receive no credentials."""
    sent: list = []

    class FakeSock:
        def getpeercert(self, binary_form=False):
            return b"an-impostor-certificate"

    class FakeConn:
        def __init__(self, *a, **kw):
            self.sock = None

        def connect(self):
            self.sock = FakeSock()

        def request(self, method, path, body=None, headers=None):
            sent.append((path, body))          # must never run for a bad pin

        def getresponse(self):
            raise AssertionError("unreachable")

        def close(self):
            pass

    monkeypatch.setattr(dh.http.client, "HTTPSConnection", FakeConn)
    monkeypatch.setattr(dh, "_tcp_open", lambda ip, port: True)
    good_pin = dh.cert_fingerprint(b"the-real-certificate")
    snap = dh.probe_cordless("192.168.1.71", "hunter2", good_pin)
    assert snap["api_ok"] is False, "must not report a successful API session"
    assert sent == [], f"credentials were sent to an unpinned certificate: {sent}"


# ── v0.46.2: three defects found by live log analysis ────────────────────────

def test_last_call_mos_survives_a_string_rtpstatus() -> None:
    """The handset sometimes answers with rtpStatus as a plain STRING. That used
    to reach .values() and raise "'str' object has no attribute 'values'",
    aborting the whole cordless poll cycle (observed live 2026-08-03)."""
    assert dh.last_call_mos({}) == (None, None)
    assert dh.last_call_mos(None) == (None, None)
    # The regression itself: a non-empty string must not raise.
    for bad in ("none", "no records", "0"):
        assert dh.last_call_mos(bad) == (None, None), bad
    # A real mapping still works.
    got = dh.last_call_mos({"a": {"moscq": "4.3", "stopTimeSecond": "100"}}, now=160)
    assert got[0] == 4.3 and got[1] == 60


def test_gateway_all_down_is_not_critical_during_startup_grace() -> None:
    """After OUR restart the GXW re-registers on its own timer (~4.5 min), so
    'all ports down' is expected, not an outage. Claiming the gateway 'lost
    power' then is both wrong and alarm-fatiguing."""
    gw = ["11", "12", "13", "14", "15", "16", "17", "18"]
    lvl, why = dh.classify_gateway(gw, gw, uptime_s=30)
    assert lvl == "degraded", lvl
    assert "re-registers" in why[0]
    # Past the window the same reading IS critical.
    lvl, why = dh.classify_gateway(gw, gw, uptime_s=dh.GATEWAY_STARTUP_GRACE_S + 1)
    assert lvl == "critical" and "lost power" in why[0]
    # No uptime supplied (older callers / tests) keeps the strict behaviour.
    assert dh.classify_gateway(gw, gw)[0] == "critical"
    # A PARTIAL outage is never suppressed, even one second after start.
    lvl, why = dh.classify_gateway(["11", "12"], gw, uptime_s=1)
    assert lvl == "degraded" and "2 of 8" in why[0].replace("2/8", "2 of 8") or lvl == "degraded"
    # Healthy stays healthy.
    assert dh.classify_gateway([], gw, uptime_s=1)[0] == "ok"


def test_api_unreadable_reason_names_the_cert_pin_too() -> None:
    """Since v0.46.0 an unreadable admin API can also mean a certificate-pin
    mismatch, not only a wrong password — the message must not mis-diagnose."""
    snap = {"reachable": True, "api_ok": False}
    _, reasons = dh.classify_cordless(snap, {"battery_crit": 20, "battery_warn": 35,
                                             "wifi_min": 2, "mos_min": 3.4, "mos_window": 3600})
    joined = " ".join(reasons)
    assert "cordless_password" in joined and "cordless_cert_sha256" in joined, joined


# ── MOS sentinel + call-ledger gate + level-string state (live defects) ──────

def test_last_call_mos_skips_no_measurement_sentinel() -> None:
    """The WP826 emits moscq 0.0 as a no-measurement sentinel (real MOS floors
    at 1.0); it reached a live alert as "MOS 0.0" on 2026-08-05."""
    assert dh.last_call_mos({"a": {"moscq": "0.0", "stopTimeSecond": "100"}}, now=150) == (None, None)
    assert dh.last_call_mos({"a": {"moscq": "0.99", "stopTimeSecond": "100"}}, now=150) == (None, None)
    # Exactly 1.0 is the scale floor — a real (terrible) measurement.
    assert dh.last_call_mos({"a": {"moscq": "1.0", "stopTimeSecond": "100"}}, now=150) == (1.0, 50)
    # A sentinel NEWEST record is ignored as a candidate, so an older valid
    # record within the window is picked — it must neither win nor shadow.
    got = dh.last_call_mos({"old": {"moscq": "4.1", "stopTimeSecond": "100"},
                            "new": {"moscq": "0.0", "stopTimeSecond": "200"}}, now=260)
    assert got == (4.1, 160)
    # All records sentinel -> nothing at all.
    assert dh.last_call_mos({"a": {"moscq": "0.0", "stopTimeSecond": "100"},
                             "b": {"moscq": "0.0", "stopTimeSecond": "200"}}, now=260) == (None, None)


def test_last_call_mos_requires_a_ledger_matched_call() -> None:
    """HA announce playback legs leave low-MOS phone RTP records that are NOT
    calls (nowhere in the call ledger) — three false 'degraded' episodes fired
    2026-08-05/06. Only a ledger-confirmed record may drive health."""
    rtp = {"a": {"moscq": "2.5", "stopTimeSecond": "1000"}}
    # A leg within the window confirms the record (90 s inclusive).
    assert dh.last_call_mos(rtp, now=1100, ledger_ts=[1080]) == (2.5, 100)
    assert dh.last_call_mos(rtp, now=1100, ledger_ts=[1000 + dh.CALLQOS_MATCH_WINDOW_S]) == (2.5, 100)
    # No leg near it (announce playback) -> skipped entirely.
    assert dh.last_call_mos(rtp, now=1100, ledger_ts=[1091]) == (None, None)
    assert dh.last_call_mos(rtp, now=1100, ledger_ts=[2000]) == (None, None)
    # Ledger readable but empty -> NO record qualifies.
    assert dh.last_call_mos(rtp, now=1100, ledger_ts=[]) == (None, None)
    # ledger_ts=None keeps the legacy ungated behaviour.
    assert dh.last_call_mos(rtp, now=1100) == (2.5, 100)
    # An unconfirmed NEWER record (the announce leg) must not shadow the
    # confirmed real call before it.
    rtp2 = {"announce": {"moscq": "2.2", "stopTimeSecond": "2000"},
            "call": {"moscq": "4.0", "stopTimeSecond": "1000"}}
    assert dh.last_call_mos(rtp2, now=2100, ledger_ts=[1005]) == (4.0, 1100)


def test_load_callqos_ts(tmp_path) -> None:
    # Missing / unreadable ledger -> [] (and downstream, no MOS drives health).
    assert dh.load_callqos_ts(str(tmp_path / "nope.jsonl")) == []
    p = tmp_path / "callqos.jsonl"
    p.write_text('{"ts": 100, "ext": "19"}\n'
                 'not json at all\n'
                 '{"no_ts_field": true}\n'
                 '[1, 2]\n'
                 '{"ts": "wat"}\n'
                 '{"ts": 200.5}\n')
    assert dh.load_callqos_ts(str(p)) == [100.0, 200.5]
    # Only the tail is read (the ledger is append-only and unbounded): a leg
    # older than the tail window is not returned, the newest still is.
    big = tmp_path / "big.jsonl"
    filler = "".join('{"pad": "%s"}\n' % ("x" * 120) for _ in range(700))
    big.write_text('{"ts": 1}\n' + filler + '{"ts": 2}\n')
    got = dh.load_callqos_ts(str(big), max_bytes=65536)
    assert 2.0 in got and 1.0 not in got


def test_publish_cordless_state_is_always_the_level_string() -> None:
    """The state used to be the battery % when the battery read succeeded and
    the level word otherwise, so a battery-driven 'critical' was invisible in
    the state itself (live 2026-08-03: 3% discharging showed state '3')."""
    class _FakeHA:
        calls: list = []
        @staticmethod
        def set_state(eid, state, attrs):
            _FakeHA.calls.append((eid, state, attrs))
    sys.modules["ha_client"] = _FakeHA
    try:
        # Battery readable: the state must STILL be the level word.
        dh._publish_cordless("ok", [], {"reachable": True, "api_ok": True,
                                        "battery_pct": 80, "charging": True})
        eid, state, attrs = _FakeHA.calls[-1]
        assert eid == "sensor.switchboard_cordless_health"
        assert state == "ok"
        assert attrs["battery_pct"] == 80 and attrs["health"] == "ok"
        # Battery unreadable: same shape, no phantom battery attribute.
        dh._publish_cordless("degraded", ["Wi-Fi disconnected"], {"reachable": True, "api_ok": True})
        _, state, attrs = _FakeHA.calls[-1]
        assert state == "degraded" and "battery_pct" not in attrs
        assert attrs["reasons"] == ["Wi-Fi disconnected"]
        # The regression end-to-end: 3% discharging on a live handset must SHOW
        # critical in the state BEFORE the handset dies.
        snap = {"reachable": True, "api_ok": True, "battery_pct": 3, "charging": False,
                "wifi_connected": True, "wifi_signal": 4}
        lvl, reasons = dh.classify_cordless(snap, TH)
        dh._publish_cordless(lvl, reasons, snap)
        _, state, attrs = _FakeHA.calls[-1]
        assert state == "critical"
        assert attrs["battery_pct"] == 3 and attrs["health"] == "critical"
    finally:
        sys.modules.pop("ha_client", None)


def test_every_published_sensor_carries_a_freshness_stamp() -> None:
    """Each pushed sensor must describe its own age.

    rollup_is_stale() above closes the case where ONE poller dies while another
    survives to notice and publish "unknown". Nothing inside the add-on can
    close the case where the ADD-ON ITSELF is gone: no code is left running to
    say anything, so every sensor freezes at its last value and keeps asserting
    it. Through the 60-minute whole-host outage of 2026-08-25 the PBX did not
    exist, yet cordless_health read "ok" for the entire hour -- HA history shows
    three rows, all "ok", with no 'unavailable' and no gap marker. Anything
    reading it during the outage got positive confirmation that a dead phone
    system was healthy.

    Only a consumer OUTSIDE the add-on can catch that, by subtracting this stamp
    from now -- so the stamp has to be present, parseable and timezone-aware on
    every sensor, including the explicit-unknown path."""
    import datetime as _d

    class _FakeHA:
        calls = []

        @staticmethod
        def set_state(eid, state, attrs=None):
            _FakeHA.calls.append((eid, state, attrs or {})); return True

    sys.modules["ha_client"] = _FakeHA
    try:
        _FakeHA.calls.clear()
        dh._publish_cordless("ok", [], {"reachable": True, "api_ok": True, "battery_pct": 80})
        dh._publish_gateway("ok", [], [], ["11", "12"])
        dh._publish_gateway_unknown("rollup stale", ["11", "12"])

        # Assert PER CALL, not via a dict keyed on entity id: _publish_gateway
        # and _publish_gateway_unknown both write sensor.switchboard_gateway_health,
        # so a dict silently overwrites and only the last call is ever checked.
        # Mutation testing caught exactly that -- dropping the stamp from
        # _publish_gateway left this test green.
        assert len(_FakeHA.calls) == 3, _FakeHA.calls
        seen = {eid for eid, _s, _a in _FakeHA.calls}
        assert seen == {
            "sensor.switchboard_cordless_health",
            "sensor.switchboard_gateway_health",
        }, seen

        for eid, _state, attrs in _FakeHA.calls:
            assert "measured_at" in attrs, f"{eid} has no measured_at"
            assert isinstance(attrs.get("poll_interval_s"), int), \
                f"{eid} has no integer poll_interval_s"
            # A cadence of 0 would make any staleness rule divide-by-zero or
            # treat the sensor as permanently overdue.
            assert attrs["poll_interval_s"] >= 30, f"{eid} interval too small"
            # The stamp is only useful if a consumer can subtract it from now.
            when = _d.datetime.fromisoformat(str(attrs["measured_at"]))
            assert when.tzinfo is not None, f"{eid} measured_at is naive"

        # The explicit-unknown path is the one that fires when the monitor has
        # gone blind, so it must be stamped too -- an unstamped "unknown" is
        # just as frozen as an unstamped "ok" once the add-on stops publishing.
        unknown = [a for e, s, a in _FakeHA.calls
                   if e == "sensor.switchboard_gateway_health" and s == "unknown"]
        assert len(unknown) == 1, _FakeHA.calls
        assert "measured_at" in unknown[0] and "poll_interval_s" in unknown[0]
    finally:
        sys.modules.pop("ha_client", None)


def test_playback_legs_never_confirm_a_call_for_last_call_mos(tmp_path) -> None:
    """The ledger gate must exclude PLAYBACK legs by TAG, not by luck.

    last_call_mos() only accepts a phone-side RTP record if some ledger leg lands
    within CALLQOS_MATCH_WINDOW_S of it. That gate exists for exactly one reason,
    per its own docstring: HA announce playback leaves phone-side records with
    moscq 2.2-2.9 that fired three false 'degraded' episodes on 2026-08-05/06.

    It originally worked because playback legs were simply ABSENT from the
    ledger. v0.55.0 then added the rtpqos hook to wakeup-deliver, page and
    announce -- so those legs ARE written now, and began confirming themselves,
    silently restoring the bug. The gate must therefore filter on tag."""
    import json as _json

    led = tmp_path / "callqos.jsonl"
    led.write_text(
        _json.dumps({"ts": 1000.0, "tag": "wakeup-deliver"}) + "\n" +
        _json.dumps({"ts": 2000.0, "tag": "page"}) + "\n" +
        _json.dumps({"ts": 3000.0, "tag": "announce"}) + "\n" +
        _json.dumps({"ts": 4000.0, "tag": "rooms"}) + "\n" +
        _json.dumps({"ts": 5000.0, "tag": ""}) + "\n",
        encoding="utf-8")

    ts = dh.load_callqos_ts(str(led))
    assert 1000.0 not in ts, "wakeup-deliver leg must not confirm a call"
    assert 2000.0 not in ts, "page leg must not confirm a call"
    assert 3000.0 not in ts, "announce leg must not confirm a call"
    assert 4000.0 in ts, "a real room conversation MUST still confirm"
    assert 5000.0 in ts, "an untagged legacy record must still confirm"
    assert sorted(ts) == [4000.0, 5000.0], ts

    # End to end: a handset record scoring 2.2 that lines up ONLY with a playback
    # leg must be skipped entirely -- that is the false-'degraded' shape.
    rtp = {"0": {"moscq": "2.2", "stopTimeSecond": "3000"}}
    mos, _age = dh.last_call_mos(rtp, now=3060.0, ledger_ts=ts)
    assert mos is None, f"playback leg confirmed a 2.2 record: {mos}"

    # ...while a real conversation at the same score is still reported, because
    # suppressing genuine bad audio would be the opposite failure.
    rtp_real = {"0": {"moscq": "2.2", "stopTimeSecond": "4000"}}
    mos2, _ = dh.last_call_mos(rtp_real, now=4060.0, ledger_ts=ts)
    assert mos2 == 2.2, f"real call should still report poor MOS, got {mos2}"


def test_a_clear_driven_by_staleness_is_not_reported_as_recovery() -> None:
    """An alert that lapses because its evidence aged out is NOT a recovery.

    classify_cordless flags a poor call only while it is inside mos_window, so
    the level returns to 'ok' on its own once the bad call is old enough -- with
    nothing new observed. Reporting that as "recovered - back to normal" is an
    affirmative claim about the present made on no present evidence, and an
    operator who sees a recovery notification reasonably believes something was
    re-checked. Live on 2026-09-01: "cordless degraded: last call quality poor"
    was followed immediately by "cordless recovered: recovered" with no call in
    between.

    This is the frozen-sensor pattern wearing different clothes."""
    # Alert first: two consecutive unhealthy cycles arm it.
    st = {}
    dh.health_transition("degraded", st)
    check("transition: first unhealthy cycle is held by the gate",
          dh.health_transition("degraded", st) == "degraded")

    # Now ok WITHOUT new evidence -> must not claim recovery.
    st_stale = dict(st)
    check("transition: an aged-out clear reports stale-clear, not recovered",
          dh.health_transition("ok", st_stale, fresh_evidence=False) == "stale-clear")

    # And ok WITH new evidence -> a genuine recovery.
    st_fresh = dict(st)
    check("transition: a clear backed by a new measurement IS a recovery",
          dh.health_transition("ok", st_fresh, fresh_evidence=True) == "recovered")

    # Either way the alert latch clears, so neither repeats.
    check("transition: stale-clear clears the latch",
          dh.health_transition("ok", st_stale, fresh_evidence=False) == "")
    check("transition: recovered clears the latch",
          dh.health_transition("ok", st_fresh, fresh_evidence=True) == "")

    # Default stays backward-compatible for callers that cannot judge freshness.
    st2 = {}
    dh.health_transition("critical", st2)
    dh.health_transition("critical", st2)
    check("transition: the default is still 'recovered' (backward compatible)",
          dh.health_transition("ok", st2) == "recovered")


def test_stale_clear_notification_does_not_say_back_to_normal() -> None:
    """The wording is the whole point: a lapsed alert must not read as an
    all-clear, and must not be formatted as a degraded alert either."""
    sent = []

    class _FakeHA:
        @staticmethod
        def notify(msg, title=None, notification_id=None):
            sent.append((title or "", msg)); return True

    sys.modules["ha_client"] = _FakeHA
    try:
        dh._notify("cordless", "stale-clear", [])
        dh._notify("cordless", "recovered", [])
        dh._notify("cordless", "degraded", ["last call quality poor (MOS 2.2, 129s ago)"])
    finally:
        sys.modules.pop("ha_client", None)

    stale_title, stale_msg = sent[0]
    check("stale-clear: does NOT claim back to normal",
          "back to normal" not in stale_msg.lower())
    check("stale-clear: says nothing was measured",
          "nothing new was measured" in stale_msg.lower())
    check("stale-clear: tells the reader the state is unknown",
          "unknown" in stale_msg.lower())
    check("stale-clear: is not titled as a degraded alert",
          "degraded" not in stale_title.lower() and "critical" not in stale_title.lower())

    rec_title, rec_msg = sent[1]
    check("recovered: still says back to normal", "back to normal" in rec_msg.lower())
    check("recovered: is not confused with the lapse wording",
          "aged out" not in rec_msg.lower())

    deg_title, deg_msg = sent[2]
    check("degraded: still names the reason", "MOS 2.2" in deg_msg)
    check("degraded: is still titled as degraded", "degraded" in deg_title.lower())


def test_run_loop_judges_freshness_before_clearing_the_cordless_alert() -> None:
    """The run loop must WORK OUT whether anything was re-measured.

    health_transition() accepts fresh_evidence, but a caller that never supplies
    it silently reverts to always claiming recovery. Mutation testing showed
    exactly that: dropping the argument at the call site left the whole suite
    green, because the decision logic lives in run() and nothing drove run().
    That is the seventh call-site gap of this kind in this work, so the caller
    gets driven directly.

    Scenario: a poor call is measured, the alert arms, and then the SAME call
    simply ages out of mos_window. Nothing new is observed, so the clear must be
    reported as a lapse, not a recovery."""
    import time as _t
    events = []

    class _Stop(Exception):
        pass

    now = _t.time()
    # Cycle 1 and 2: the same poor call, 100 s old -> arms the alert.
    # Cycle 3: identical call, now 1000 s old -> outside mos_window (900) -> ok,
    # with NO newer measurement.
    snaps = [
        {"reachable": True, "api_ok": True, "battery_pct": 80, "wifi_connected": True,
         "wifi_signal": 4, "last_mos": 2.2, "last_mos_age_s": 100},
        {"reachable": True, "api_ok": True, "battery_pct": 80, "wifi_connected": True,
         "wifi_signal": 4, "last_mos": 2.2, "last_mos_age_s": 220},
        {"reachable": True, "api_ok": True, "battery_pct": 80, "wifi_connected": True,
         "wifi_signal": 4, "last_mos": 2.2, "last_mos_age_s": 1000},
    ]
    calls = {"n": 0}

    def _probe(ip, pw, pin):
        i = min(calls["n"], len(snaps) - 1)
        calls["n"] += 1
        return dict(snaps[i])

    def _sleep(_n):
        if calls["n"] >= len(snaps):
            raise _Stop()

    saved = {k: getattr(dh, k) for k in
             ("probe_cordless", "resolve_cordless_ip", "_publish_cordless",
              "_publish_gateway", "gateway_down_exts_from_rollup", "_notify")}
    real_sleep = dh.time.sleep
    real_env = dict(os.environ)
    try:
        os.environ["CORDLESS_IP"] = "192.0.2.1"
        os.environ["CORDLESS_PASSWORD"] = "x"
        os.environ["DEVICE_HEALTH_INTERVAL"] = "30"
        dh.probe_cordless = _probe
        dh.resolve_cordless_ip = lambda ext, ip: ip
        dh._publish_cordless = lambda *a, **k: None
        dh._publish_gateway = lambda *a, **k: None
        dh.gateway_down_exts_from_rollup = lambda: None
        dh._notify = lambda device, event, reasons: events.append((device, event))
        dh.time.sleep = _sleep
        try:
            dh.run()
        except _Stop:
            pass
    finally:
        for k, v in saved.items():
            setattr(dh, k, v)
        dh.time.sleep = real_sleep
        os.environ.clear(); os.environ.update(real_env)

    kinds = [e for d, e in events if d == "cordless"]
    check("run loop: the poor call armed a degraded alert", "degraded" in kinds)
    check("run loop: the aged-out clear is a LAPSE, not a recovery",
          "stale-clear" in kinds)
    check("run loop: it did NOT claim recovery on no new evidence",
          "recovered" not in kinds)
