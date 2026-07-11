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


# Contacts exactly as ami.contacts_from_blocks returns them (rtt = RoundtripUsec µs).
CONTACTS = {
    "11": {"status": "Avail", "uri": "sip:11@x", "rtt": "1831"},
    "19": {"status": "Avail", "uri": "sip:19@x", "rtt": "8158"},      # WiFi cordless
    "17": {"status": "Unavail", "uri": "sip:17@x", "rtt": ""},        # offline
    "trunk-aor": {"status": "NonQual", "uri": "sip:trunk", "rtt": "nan"},
}
NAMES = {"11": "Kitchen", "19": "Cordless", "17": "Study"}


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
    rows = pm.build_phone_health(CONTACTS, NAMES)
    by = {r["ext"]: r for r in rows}
    check("build: cordless RTT parsed to ms", by["19"]["rtt_ms"] == 8.16 and by["19"]["reachable"])
    check("build: wired phone reachable low RTT", by["11"]["rtt_ms"] == 1.83)
    check("build: offline phone -> not reachable, rtt None",
          by["17"]["reachable"] is False and by["17"]["rtt_ms"] is None)
    # The SIP trunk (a qualify-off, NonQual static AOR keyed 'trunk-aor') is NOT a
    # phone link — it must be filtered out entirely (else it pins the rollup down
    # forever and mints an invalid hyphenated entity id).
    check("build: SIP trunk filtered out (non-digit AOR)", "trunk-aor" not in by)
    check("build: names applied", by["19"]["name"] == "Cordless" and by["11"]["name"] == "Kitchen")


def test_summarize() -> None:
    s = pm.summarize(pm.build_phone_health(CONTACTS, NAMES))
    check("summary: 2 reachable (11,19)", s["reachable"] == 2)
    check("summary: 1 unreachable (17; trunk excluded)", s["unreachable"] == 1)
    check("summary: worst RTT is the cordless", s["worst_rtt_ms"] == 8.16 and s["worst_ext"] == "19")
    check("summary: unreachable exts = just the offline phone", set(s["unreachable_exts"]) == {"17"})
    check("summary: trunk never counted", "trunk-aor" not in s["unreachable_exts"])


def test_room_names() -> None:
    n = pm.room_names({"rooms": [{"ext": "11", "name": "Kitchen"}, {"ext": "12", "name": ""}]})
    check("names: mapped", n["11"] == "Kitchen")
    check("names: blank name falls back to ext", n["12"] == "12")


def test_poll_once_ami_down() -> None:
    class _Down:
        @staticmethod
        def get_contacts():
            raise RuntimeError("AMI down")
    sys.modules["ami"] = _Down
    try:
        phones, summ = pm.poll_once(NAMES)
        check("poll: AMI error -> (None, None), no crash", phones is None and summ is None)

        class _Empty:
            @staticmethod
            def get_contacts():
                return {}
        sys.modules["ami"] = _Empty
        phones, summ = pm.poll_once(NAMES)
        check("poll: empty contacts -> skip cycle", phones is None)

        class _Ok:
            @staticmethod
            def get_contacts():
                return CONTACTS
        sys.modules["ami"] = _Ok
        phones, summ = pm.poll_once(NAMES)
        check("poll: contacts -> phones + summary", phones is not None and summ["worst_ext"] == "19")
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
        phones = pm.build_phone_health(CONTACTS, NAMES)
        pm._publish(phones, pm.summarize(phones))
        ids = {eid for eid, _, _ in sets}
        check("publish: per-phone sensor for the cordless", "sensor.switchboard_link_19" in ids)
        check("publish: rollup sensor emitted", "sensor.switchboard_link_health" in ids)
        check("publish: no sensor for the filtered trunk", "sensor.switchboard_link_trunk-aor" not in ids)
        # Every published id must be a valid HA entity id (ha_client.is_entity_id
        # rejects hyphens -> the trunk-aor bug). Guard it here since the fake
        # set_state doesn't apply the real regex.
        check("publish: every entity id is HA-valid (no hyphens)",
              all(re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", e) for e in ids))
        cord = next(s for e, s, _ in sets if e == "sensor.switchboard_link_19")
        check("publish: cordless state is its RTT in ms", cord == 8.16)
        off = next((s for e, s, _ in sets if e == "sensor.switchboard_link_17"), None)
        check("publish: offline phone state is 'unavailable'", off == "unavailable")
        health = next(s for e, s, _ in sets if e == "sensor.switchboard_link_health")
        check("publish: health rollup state is the worst RTT", health == 8.16)
    finally:
        sys.modules.pop("ha_client", None)


def test_history_append_caps() -> None:
    d = tempfile.mkdtemp()
    pm.STATE_PATH, orig = os.path.join(d, "lh.jsonl"), pm.STATE_PATH
    origmax = pm.MAX_RECORDS
    pm.MAX_RECORDS = 5
    try:
        for _ in range(8):
            pm._append_history(pm.build_phone_health(CONTACTS, NAMES))
        lines = [l for l in open(pm.STATE_PATH).read().splitlines() if l.strip()]
        check("history: capped to MAX_RECORDS", len(lines) == 5)
        rec = json.loads(lines[-1])
        check("history: records per-phone rtt", rec["phones"]["19"]["rtt_ms"] == 8.16)
    finally:
        pm.STATE_PATH, pm.MAX_RECORDS = orig, origmax


if __name__ == "__main__":
    test_rtt_ms()
    test_is_reachable()
    test_build_phone_health()
    test_summarize()
    test_room_names()
    test_poll_once_ami_down()
    test_publish_routing()
    test_history_append_caps()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
