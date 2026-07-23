"""Behavioral tests for the device-health monitor's pure logic.

    python3 switchboard/tests/test_devhealth.py

Pins classify_cordless (the ok/degraded/critical rules that decide whether the alarm
cordless is healthy), classify_gateway (deriving GXW health from which ports are down),
health_transition (the alert state machine), and last_call_mos (newest-call MOS, recency-gated).
The WP826 HTTP client + the poll loop are I/O and are not exercised here (mirrors how
test_rtpmon.py leaves the AMI socket untested).
"""
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


if __name__ == "__main__":
    test_classify_cordless()
    test_classify_gateway()
    test_health_transition()
    test_last_call_mos()
    test_resolve_cordless_ip()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
