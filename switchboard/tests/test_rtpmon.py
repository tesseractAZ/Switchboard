"""Behavioral tests for the idle link-health poller (switchboard-rtpmon).

Run with plain Python (no pytest):

    python3 switchboard/tests/test_rtpmon.py

Pins the RoundtripUsec->ms parse, reachability mapping, per-phone + rollup shaping,
and the AMI/HA I/O shells (AMI-down cycle skips; publish emits per-phone + summary
sensors) using injected fakes.
"""
import json
import os
import re
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

P_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share" / "switchboard" / "rtpmon" / "poller.py"
pm = SourceFileLoader("rtpmon_poller", str(P_PATH)).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


# Contacts as ami.contacts_from_blocks returns them (rtt = RoundtripUsec µs). Only
# REGISTERED phones have a contact — note ext 19 (the cordless) has none here.
CONTACTS = {
    "11": {"status": "Avail", "uri": "sip:11@x", "rtt": "1831"},     # healthy wired
    "12": {"status": "Avail", "uri": "sip:12@x", "rtt": "8158"},     # reachable, high RTT (worst)
    "17": {"status": "Unavail", "uri": "sip:17@x", "rtt": ""},       # registered, qualify failing
    "trunk-aor": {"status": "NonQual", "uri": "sip:trunk", "rtt": "nan"},
}
# The CONFIGURED roster (PJSIPShowEndpoints). ext 19 (cordless) is configured but has
# NO contact -> de-registered -> must surface as 'offline', not vanish. 'trunk' filtered.
ENDPOINTS = [
    {"name": "11", "state": "Not in use", "channels": ""},
    {"name": "12", "state": "Not in use", "channels": ""},
    {"name": "17", "state": "Unavailable", "channels": ""},
    {"name": "19", "state": "Unavailable", "channels": ""},   # de-registered cordless
    {"name": "trunk", "state": "Unavailable", "channels": ""},
]
NAMES = {"11": "Kitchen", "12": "Living Room", "17": "Garage", "19": "Cordless"}


def test_rtt_ms() -> None:
    check("rtt: 1831us -> 1.83ms", pm.rtt_ms("1831") == 1.83)
    check("rtt: 8158us -> 8.16ms (cordless)", pm.rtt_ms("8158") == 8.16)
    for junk in ("", "nan", "unavailable", "abc", "-5"):
        check(f"rtt: {junk!r} -> None", pm.rtt_ms(junk) is None)
    check("rtt: None -> None", pm.rtt_ms(None) is None)


def test_is_reachable() -> None:
    for s in ("Avail", "avail", "Reachable", "Created", "Updated"):
        check(f"reachable: {s!r} -> True", pm.is_reachable(s) is True)
    for s in ("Unavail", "NonQual", "Removed", "Unknown", ""):
        check(f"reachable: {s!r} -> False", pm.is_reachable(s) is False)


def test_build_phone_health() -> None:
    rows = pm.build_phone_health(ENDPOINTS, CONTACTS, NAMES)
    by = {r["ext"]: r for r in rows}
    check("build: healthy wired phone reachable, low RTT",
          by["11"]["reachable"] and by["11"]["rtt_ms"] == 1.83)
    check("build: high-RTT phone still reachable", by["12"]["rtt_ms"] == 8.16 and by["12"]["reachable"])
    check("build: registered-but-qualify-failing -> not reachable, still registered",
          by["17"]["reachable"] is False and by["17"]["registered"] is True and by["17"]["rtt_ms"] is None)
    # THE point of v0.23.0: a configured phone with no contact is OFFLINE, not absent.
    check("build: de-registered cordless present as offline (not vanished)",
          "19" in by and by["19"]["registered"] is False and by["19"]["reachable"] is False)
    check("build: offline phone status is Unregistered", by["19"]["status"] == "Unregistered")
    check("build: SIP trunk filtered out", "trunk" not in by and "trunk-aor" not in by)
    check("build: names applied", by["19"]["name"] == "Cordless" and by["11"]["name"] == "Kitchen")


def test_summarize() -> None:
    s = pm.summarize(pm.build_phone_health(ENDPOINTS, CONTACTS, NAMES))
    check("summary: 2 reachable (11,12)", s["reachable"] == 2)
    check("summary: 2 unreachable (17 failing + 19 offline)", s["unreachable"] == 2)
    check("summary: 1 offline = the de-registered cordless", s["offline"] == 1 and s["offline_exts"] == ["19"])
    check("summary: worst RTT is the high-RTT phone", s["worst_rtt_ms"] == 8.16 and s["worst_ext"] == "12")
    check("summary: trunk never counted",
          "trunk" not in s["unreachable_exts"] and "trunk-aor" not in s["unreachable_exts"])


def test_room_names() -> None:
    n = pm.room_names({"rooms": [{"ext": "11", "name": "Kitchen"}, {"ext": "12", "name": ""}]})
    check("names: mapped", n["11"] == "Kitchen")
    check("names: blank name falls back to ext", n["12"] == "12")


def test_poll_once_ami_down() -> None:
    class _Down:
        @staticmethod
        def get_status_bundle():
            raise RuntimeError("AMI down")
    sys.modules["ami"] = _Down
    try:
        phones, summ = pm.poll_once(NAMES)
        check("poll: AMI error -> (None, None), no crash", phones is None and summ is None)

        class _Empty:
            @staticmethod
            def get_status_bundle():
                return ([], {}, [])
        sys.modules["ami"] = _Empty
        phones, summ = pm.poll_once(NAMES)
        check("poll: empty roster -> skip cycle (don't blank sensors)", phones is None)

        class _Ok:
            @staticmethod
            def get_status_bundle():
                return (ENDPOINTS, CONTACTS, [])
        sys.modules["ami"] = _Ok
        phones, summ = pm.poll_once(NAMES)
        check("poll: roster+contacts -> phones + summary", phones is not None and summ["worst_ext"] == "12")
        check("poll: de-registered cordless included as offline",
              any(p["ext"] == "19" and not p["registered"] for p in phones))
    finally:
        sys.modules.pop("ami", None)


def test_publish_routing() -> None:
    sets = []

    class _Fake:
        @staticmethod
        def set_state(eid, state, attrs=None):
            sets.append((eid, state, attrs or {}))
            return True
    sys.modules["ha_client"] = _Fake
    try:
        phones = pm.build_phone_health(ENDPOINTS, CONTACTS, NAMES)
        pm._publish(phones, pm.summarize(phones))
        by = {eid: state for eid, state, _ in sets}
        check("publish: reachable phone -> numeric RTT state", by["sensor.switchboard_link_12"] == 8.16)
        check("publish: de-registered cordless -> 'offline' state (visible, alertable)",
              by.get("sensor.switchboard_link_19") == "offline")
        check("publish: registered-but-failing phone -> 'unavailable'",
              by.get("sensor.switchboard_link_17") == "unavailable")
        check("publish: rollup sensor emitted", "sensor.switchboard_link_health" in by)
        health_attrs = next(a for e, s, a in sets if e == "sensor.switchboard_link_health")
        check("publish: rollup carries offline detail", health_attrs["offline_exts"] == ["19"])
        check("publish: no sensor for the filtered trunk",
              "sensor.switchboard_link_trunk-aor" not in by and "sensor.switchboard_link_trunk" not in by)
        # Every published id must be a valid HA entity id (ha_client.is_entity_id
        # rejects hyphens -> the trunk-aor bug). Guard it here since the fake
        # set_state doesn't apply the real regex.
        check("publish: every entity id is HA-valid (no hyphens)",
              all(re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", e) for e in by))
    finally:
        sys.modules.pop("ha_client", None)


def test_warmup_done() -> None:
    # Right after a restart the phones re-register over a few seconds; the poller must
    # keep the fast warm-up cadence until the registered count STABILIZES, so a
    # straggler port isn't frozen 'offline' for a whole interval — and it must not
    # publish an all-offline snapshot that persists.
    # (settled, prev_reachable, reachable, polls)
    check("warmup: AMI down (0 reachable), under cap -> keep warming",
          pm.warmup_done(False, -1, 0, 1) is False)
    check("warmup: count still GROWING (7 then 8) -> keep warming (straggler incoming)",
          pm.warmup_done(False, 7, 8, 3) is False)
    check("warmup: count STABILIZED (8 == 8) -> settle",
          pm.warmup_done(False, 8, 8, 4) is True)
    check("warmup: first poll (prev=-1) with some up -> keep warming (not yet stable)",
          pm.warmup_done(False, -1, 7, 1) is False)
    check("warmup: cap reached with nothing up -> settle anyway (genuinely-down fleet)",
          pm.warmup_done(False, 0, 0, pm.WARMUP_MAX_POLLS) is True)
    check("warmup: latches — never drops back into warm-up if a phone later leaves",
          pm.warmup_done(True, 8, 0, 1) is True)


def test_history_append_caps() -> None:
    d = tempfile.mkdtemp()
    pm.STATE_PATH, orig = os.path.join(d, "lh.jsonl"), pm.STATE_PATH
    origmax = pm.MAX_RECORDS
    pm.MAX_RECORDS = 5
    try:
        for _ in range(8):
            pm._append_history(pm.build_phone_health(ENDPOINTS, CONTACTS, NAMES))
        lines = [l for l in open(pm.STATE_PATH).read().splitlines() if l.strip()]
        check("history: capped to MAX_RECORDS", len(lines) == 5)
        rec = json.loads(lines[-1])
        check("history: records per-phone rtt", rec["phones"]["12"]["rtt_ms"] == 8.16)
        check("history: records the offline cordless", rec["phones"]["19"]["registered"] is False)
    finally:
        pm.STATE_PATH, pm.MAX_RECORDS = orig, origmax


def test_is_mass_outage() -> None:
    # The real outage: 8 of 10 phones down (GXW dropped) -> mass outage.
    check("outage: 8/10 down is a fleet outage",
          pm.is_mass_outage({"total": 10, "unreachable": 8}) is True)
    # One handset asleep (+ one empty port) is NOT an outage.
    check("outage: 1/10 down (one port) is not an outage",
          pm.is_mass_outage({"total": 10, "unreachable": 1}) is False)
    check("outage: 2/10 down (cordless asleep + empty port) is not an outage",
          pm.is_mass_outage({"total": 10, "unreachable": 2}) is False)
    # Small fleet still needs the absolute floor (>= OUTAGE_MIN_PORTS).
    check("outage: 2/4 down < min-ports floor -> not an outage",
          pm.is_mass_outage({"total": 4, "unreachable": 2}) is False)
    check("outage: 3/4 down clears both thresholds", pm.is_mass_outage({"total": 4, "unreachable": 3}) is True)
    check("outage: empty summary is not an outage", pm.is_mass_outage({}) is False)


def test_outage_transition() -> None:
    st = {"cycles": 0, "alerted": False}
    big = {"total": 10, "unreachable": 8, "unreachable_exts": list("11 12 13 14 15 16 17 18".split()), "reachable": 2}
    ok = {"total": 10, "unreachable": 1, "reachable": 9}
    # First mass-outage cycle: gated (no page on a single sample).
    check("transition: 1st outage cycle stays silent", pm.outage_transition(big, st) == "" and st["cycles"] == 1)
    # Second consecutive: fire ONCE.
    check("transition: 2nd consecutive cycle fires 'down'", pm.outage_transition(big, st) == "down")
    # Still down: don't re-page.
    check("transition: sustained outage does not re-page", pm.outage_transition(big, st) == "")
    # Recovery: fire 'up' once, then silent.
    check("transition: recovery fires 'up' once", pm.outage_transition(ok, st) == "up")
    check("transition: after recovery, steady state silent", pm.outage_transition(ok, st) == "")
    # A single-sample blip (one bad cycle then recovery) must NOT page.
    st2 = {"cycles": 0, "alerted": False}
    check("transition: single-cycle blip then ok never pages",
          pm.outage_transition(big, st2) == "" and pm.outage_transition(ok, st2) == "")


def test_outage_notify_routing() -> None:
    calls = []

    class _Fake:
        @staticmethod
        def notify(msg, title="", notification_id=""):
            calls.append((title, notification_id, msg)); return True
    sys.modules["ha_client"] = _Fake
    try:
        summ = {"total": 10, "unreachable": 8, "reachable": 2, "unreachable_exts": ["11", "12"]}
        pm._notify_outage("down", summ)
        pm._notify_outage("up", summ)
        check("notify: down + up both fire", len(calls) == 2)
        check("notify: same notification_id so recovery replaces outage",
              calls[0][1] == calls[1][1] == "switchboard_link_outage")
        check("notify: outage message names the gateway", "GXW" in calls[0][2] or "gateway" in calls[0][2])
    finally:
        sys.modules.pop("ha_client", None)


if __name__ == "__main__":
    test_rtt_ms()
    test_is_reachable()
    test_build_phone_health()
    test_summarize()
    test_room_names()
    test_poll_once_ami_down()
    test_publish_routing()
    test_warmup_done()
    test_history_append_caps()
    test_is_mass_outage()
    test_outage_transition()
    test_outage_notify_routing()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
