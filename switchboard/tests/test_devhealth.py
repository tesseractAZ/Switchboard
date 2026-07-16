"""Behavioral tests for the device-health monitor's pure logic.

    python3 switchboard/tests/test_devhealth.py

Pins classify_cordless (the ok/degraded/critical rules that decide whether the alarm
cordless is healthy), classify_gateway (deriving GXW health from which ports are down),
health_transition (the consecutive-cycle one-shot alert state machine), and _latest_mos.
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


TH = {"battery_crit": 15, "battery_warn": 30, "wifi_min": 2, "mos_min": 3.4}


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

    # Recent poor MOS -> DEGRADED.
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
         "wifi_connected": True, "wifi_signal": 4, "last_mos": 2.9}, TH)
    check("cordless: poor MOS -> degraded", lvl == "degraded" and any("MOS" in r for r in why))

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


def test_latest_mos():
    rtp = {"record0": {"moscq": "4.4"}, "record1": {"moscq": "3.1"}, "record2": {"moscq": "bad"}}
    check("mos: picks the lowest valid moscq", dh._latest_mos(rtp) == 3.1)
    check("mos: empty -> None", dh._latest_mos({}) is None)


if __name__ == "__main__":
    test_classify_cordless()
    test_classify_gateway()
    test_health_transition()
    test_latest_mos()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
