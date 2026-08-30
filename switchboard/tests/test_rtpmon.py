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
    # Under pytest the print + counter are DECORATIVE — only the __main__
    # runner reads _failures, so a failing check would still 'pass' the
    # test. Assert too, so both harnesses actually enforce every check.
    assert cond, name


# Contacts as ami.contacts_from_blocks returns them (rtt = RoundtripUsec µs). Only
# REGISTERED phones have a contact — note ext 19 (the cordless) has none here.
CONTACTS = {
    "11": {"status": "Avail", "uri": "sip:11@192.168.1.83:5060", "rtt": "1831"},  # healthy wired
    "12": {"status": "Avail", "uri": "sip:12@192.168.1.83:5062", "rtt": "8158"},  # reachable, high RTT (worst)
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
    check("build: contact_ip extracted from a registered phone's URI (DHCP auto-follow source)",
          by["11"]["contact_ip"] == "192.168.1.83")
    check("build: de-registered phone has no contact_ip", by["19"]["contact_ip"] is None)


def test_ip_from_uri() -> None:
    check("uri: user@ip:port", pm.ip_from_uri("sip:19@192.168.1.84:18357") == "192.168.1.84")
    check("uri: angle-bracket + params", pm.ip_from_uri("<sip:11@192.168.1.83:5060>;expires=120") == "192.168.1.83")
    check("uri: hostname (no ipv4) -> None (falls back to static IP)",
          pm.ip_from_uri("sip:19@cordless.local:5060") is None)
    check("uri: ipv6 -> None (IPv4-only probe path)", pm.ip_from_uri("sip:19@[2600::1]:5060") is None)
    check("uri: empty / None -> None", pm.ip_from_uri("") is None and pm.ip_from_uri(None) is None)


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
        link11 = next(a for e, s, a in sets if e == "sensor.switchboard_link_11")
        check("publish: per-phone sensor carries contact_ip (devhealth DHCP auto-follow)",
              link11.get("contact_ip") == "192.168.1.83")
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


def test_warmup_waits_for_every_wired_port() -> None:
    # THE Aug-11 BUG: warm-up ended on a false plateau. The reachable count sat
    # flat at 6 across two consecutive 15s polls while exts 15/17/18 were still
    # re-registering, so `reachable <= prev_reachable` said "stable", the poller
    # settled, and then FROZE that stale down-list for a full 300s interval —
    # devhealth read the rollup and the GXW falsely showed "degraded" for ~4
    # minutes, twice in one day. The precise gate is the wired ports themselves.
    check("warmup: count plateaued BUT wired ports still down -> keep warming",
          pm.warmup_done(False, 6, 6, 2, 3) is False)
    check("warmup: every wired port back -> settle (even on the first poll)",
          pm.warmup_done(False, -1, 8, 1, 0) is True)
    check("warmup: cap still wins over a permanently dead port (bounded wait)",
          pm.warmup_done(False, 6, 6, pm.WARMUP_MAX_POLLS, 3) is True)
    check("warmup: latch still wins",
          pm.warmup_done(True, 6, 6, 2, 3) is True)
    # wired_down=None must preserve the old heuristic for a caller with no roster.
    check("warmup: no wired roster -> falls back to the count heuristic",
          pm.warmup_done(False, 8, 8, 4, None) is True)


def test_wired_down_count() -> None:
    wired = ["11", "12", "13", "14", "15", "16", "17", "18"]
    # The cordless (19) sleeping and the never-registered softphone (20) must NOT
    # hold warm-up open — only real gateway ports count.
    summ = {"unreachable_exts": ["19", "20"], "offline_exts": ["19", "20"]}
    check("wired_down: cordless + softphone ignored", pm.wired_down_count(summ, wired) == 0)
    summ2 = {"unreachable_exts": ["15", "17"], "offline_exts": ["18", "20"]}
    check("wired_down: counts the 3 real gateway ports", pm.wired_down_count(summ2, wired) == 3)
    check("wired_down: de-dupes across both lists",
          pm.wired_down_count({"unreachable_exts": ["15"], "offline_exts": ["15"]}, wired) == 1)
    check("wired_down: no roster -> 0", pm.wired_down_count(summ2, []) == 0)
    check("wired_down: no summary -> 0", pm.wired_down_count(None, wired) == 0)


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


def test_outage_transition_warmup_never_counts() -> None:
    # During startup warm-up, polls run every WARMUP_DELAY=15s — the
    # consecutive-cycle gate (sized for the 300s steady cadence) would be
    # satisfiable ~30s after any restart, so a GXW taking a minute to
    # re-register would false-alarm as a fleet outage. Warm-up cycles must
    # NEVER count toward 'down'; recovery must still clear promptly.
    big = {"total": 10, "unreachable": 8, "unreachable_exts": ["11", "12"], "reachable": 2}
    ok = {"total": 10, "unreachable": 1, "reachable": 9}
    st = {"cycles": 0, "alerted": False}
    for _ in range(8):  # a full warm-up's worth of mass-outage samples
        check("warmup: outage cycles never fire during warm-up",
              pm.outage_transition(big, st, settled=False) == "")
    check("warmup: cycle counter untouched by warm-up samples", st["cycles"] == 0)
    # Settling with the outage ongoing: the gate starts fresh — still 2 cycles.
    check("warmup: 1st settled cycle silent", pm.outage_transition(big, st, settled=True) == "")
    check("warmup: 2nd settled cycle fires", pm.outage_transition(big, st, settled=True) == "down")
    # An alert latched before a restart must clear during warm-up, not wait it out.
    st2 = {"cycles": 0, "alerted": True}
    check("warmup: recovery clears even while warming up",
          pm.outage_transition(ok, st2, settled=False) == "up")


def test_partial_flag_actually_reaches_home_assistant() -> None:
    """Test the PUBLISHED attribute, not just summarize()'s return value.

    v0.52.0 shipped this flag computed, unit-tested and mutation-proven — and
    dead, because _publish()'s attribute literal never mentioned it, so Home
    Assistant showed nothing. Computing a value and publishing it are two
    different things; only the second one a user can see."""
    sets = []

    class _Fake:
        @staticmethod
        def set_state(eid, state, attrs=None):
            sets.append((eid, state, attrs or {})); return True

    sys.modules["ha_client"] = _Fake
    try:
        # ext 19 configured but de-registered => a partial sample.
        phones = pm.build_phone_health(ENDPOINTS, CONTACTS, NAMES)
        pm._publish(phones, pm.summarize(phones))
        rollup = next(a for eid, _, a in sets if eid == "sensor.switchboard_link_health")
        check("published: the partial flag is present as an attribute",
              "worst_rtt_is_partial" in rollup)
        check("published: and it is True when phones are missing",
              rollup["worst_rtt_is_partial"] is True)

        # A whole-fleet-reachable snapshot must publish it as False, not omit it.
        sets.clear()
        healthy = [{"ext": "11", "name": "A", "status": "Avail", "rtt_ms": 2.4,
                    "reachable": True, "registered": True}]
        pm._publish(healthy, pm.summarize(healthy))
        rollup = next(a for eid, _, a in sets if eid == "sensor.switchboard_link_health")
        check("published: False on a complete sample (not omitted)",
              rollup.get("worst_rtt_is_partial") is False)
        # The WIRED sensor is the one DOCS tells you to graph and alert on, so it
        # must describe its own freshness too — v0.51.0 stamped only the rollup.
        wired = next(a for eid, _, a in sets if eid == "sensor.switchboard_wired_link_health")
        check("published: wired sensor carries measured_at", "measured_at" in wired)
        check("published: wired sensor carries poll_interval_s",
              isinstance(wired.get("poll_interval_s"), int))
    finally:
        sys.modules.pop("ha_client", None)


def test_trunk_sensor_carries_its_own_freshness_stamp() -> None:
    """The trunk sensor must describe its own age.

    A pushed HA sensor never expires. devhealth's rollup_is_stale() covers one
    poller dying while another survives to notice — but when the WHOLE add-on
    dies, nothing is left running to publish "unknown", so every sensor freezes
    at its last value and keeps asserting it. Through the 60-minute whole-host
    outage of 2026-08-25 this sensor read "Registered" for the entire hour while
    the PBX did not exist: HA history shows exactly two rows, both "Registered",
    with no 'unavailable' and no gap marker. Only a consumer comparing the stamp
    against now can catch that, so the stamp has to be there to compare."""
    sets = []

    class _FakeHA:
        @staticmethod
        def set_state(eid, state, attrs=None):
            sets.append((eid, state, attrs or {})); return True

    class _FakeAMI:
        @staticmethod
        def get_registrations_or_none():
            return {pm.TRUNK_REG_NAME: {"status": "Registered", "next_reg": "0"}}

        @staticmethod
        def send_register(_name):
            return True

    sys.modules["ha_client"] = _FakeHA
    sys.modules["ami"] = _FakeAMI
    try:
        pm._trunk_check({}, True, False)
        eid = "sensor.switchboard_trunk_health"
        published = [(st, a) for e, st, a in sets if e == eid]
        check("trunk: the sensor was published at all", len(published) == 1)
        state, attrs = published[0]
        check("trunk: state is the registration status", state == "Registered")
        check("trunk: carries measured_at", "measured_at" in attrs)
        check("trunk: carries poll_interval_s",
              isinstance(attrs.get("poll_interval_s"), int))
        check("trunk: the advertised interval is a real cadence, not zero",
              attrs["poll_interval_s"] >= 30)
        # The stamp is only useful if it parses as a real instant — a consumer
        # has to subtract it from now.
        import datetime as _d
        parsed = _d.datetime.fromisoformat(str(attrs["measured_at"]))
        check("trunk: measured_at parses as an aware timestamp",
              parsed.tzinfo is not None)
    finally:
        sys.modules.pop("ha_client", None)
        sys.modules.pop("ami", None)


def test_worst_rtt_is_flagged_partial_when_phones_are_missing() -> None:
    """The rollup state is a max over REACHABLE phones, so when the slowest phone
    drops off it leaves the sample and the number IMPROVES — the sensor's
    all-time minimum can be its worst moment. The flag warns an automation off
    thresholding a partial sample.

    ★The v0.52.0 version keyed on `unreachable > 0`, which conflated "dropped
    out" with "never there": ext 20 is a configured softphone that NEVER
    registers by design, so the flag was True in 100% of live samples — dead
    information — and it pinned the rollup's icon to "disconnected" on a
    perfectly healthy fleet. The distinction is what this test protects."""
    def phone(ext, rtt=None, up=True, reg=True):
        return {"ext": ext, "name": ext, "status": "Avail" if up else "Unregistered",
                "rtt_ms": rtt, "reachable": up, "registered": reg}

    # The LIVE shape: eight wired + the cordless answering, ext 20 never registers.
    healthy = [phone(e, 2.4) for e in "11 12 13 14 15 16 17 18".split()]
    healthy.append(phone("19", 250.0))
    healthy.append(phone("20", None, up=False, reg=False))
    seen = {p["ext"] for p in healthy if p["reachable"]}

    s_ok = pm.summarize(healthy, ["11"], seen)
    check("partial: worst is the slow cordless", s_ok["worst_rtt_ms"] == 250.0)
    check("partial: a by-design-absent softphone does NOT flag a healthy fleet",
          s_ok["worst_rtt_is_partial"] is False)

    # The slow phone DROPS OFF: fleet worse, number better — this MUST flag.
    degraded = [p for p in healthy if p["ext"] != "19"]
    degraded.append(phone("19", None, up=False, reg=False))
    s_bad = pm.summarize(degraded, ["11"], seen)
    check("partial: losing the slow phone IMPROVES the state (the trap)",
          s_bad["worst_rtt_ms"] == 2.4 and s_bad["worst_rtt_ms"] < s_ok["worst_rtt_ms"])
    check("partial: ...and THAT is flagged", s_bad["worst_rtt_is_partial"] is True)
    check("partial: the missing phone is still named", "19" in s_bad["unreachable_exts"])

    # A registered phone whose qualify fails is also a lost sample.
    failing = [p for p in healthy if p["ext"] != "17"]
    failing.append(phone("17", None, up=False, reg=True))
    check("partial: registered-but-not-answering flags too",
          pm.summarize(failing, ["11"], seen)["worst_rtt_is_partial"] is True)

    # With no history offered, fall back to "any REGISTERED phone missing" —
    # which still ignores the never-registered softphone.
    check("partial: fallback ignores the never-registered softphone",
          pm.summarize(healthy, ["11"], None)["worst_rtt_is_partial"] is False)
    # An EMPTY set falls back to the heuristic rather than being taken
    # literally: the loop that maintains the set is not unit-testable, and an
    # empty EXPECTATION would make the flag silently False forever — the same
    # inert-but-green failure this fix removes.
    check("partial: empty history still ignores the softphone on a healthy fleet",
          pm.summarize(healthy, ["11"], set())["worst_rtt_is_partial"] is False)
    check("partial: empty history DOES catch a registered phone that stopped answering",
          pm.summarize(failing, ["11"], set())["worst_rtt_is_partial"] is True)
    # HONEST LIMIT: with no history, a phone that de-registered ENTIRELY is
    # indistinguishable from one that never registered — both are simply
    # `registered=False`. Only the measured-before set can tell them apart, and
    # it does (asserted above). Pinned so nobody "fixes" the fallback into
    # flagging the by-design-absent softphone again.
    check("partial: fallback cannot see a full de-registration (documented limit)",
          pm.summarize(degraded, ["11"], set())["worst_rtt_is_partial"] is False)


def test_rollup_icon_tracks_the_partial_flag_not_raw_unreachable() -> None:
    # Same conflation, second symptom: keyed on `unreachable` the rollup showed
    # mdi:lan-disconnect FOREVER, because ext 20 never registers. Verified live
    # on 2026-08-22: a 9-of-9 healthy fleet displaying the disconnected icon.
    sets = []

    class _Fake:
        @staticmethod
        def set_state(eid, state, attrs=None):
            sets.append((eid, attrs or {})); return True

    sys.modules["ha_client"] = _Fake
    try:
        def phone(ext, rtt=None, up=True, reg=True):
            return {"ext": ext, "name": ext, "status": "Avail" if up else "Unregistered",
                    "rtt_ms": rtt, "reachable": up, "registered": reg}
        healthy = [phone("11", 2.4), phone("19", 9.0),
                   phone("20", None, up=False, reg=False)]
        seen = {"11", "19"}
        pm._publish(healthy, pm.summarize(healthy, ["11"], seen))
        attrs = next(a for eid, a in sets if eid == "sensor.switchboard_link_health")
        check("icon: healthy fleet shows CONNECTED despite the dead softphone",
              attrs.get("icon") == "mdi:lan-connect")

        sets.clear()
        degraded = [phone("11", 2.4), phone("19", None, up=False, reg=False),
                    phone("20", None, up=False, reg=False)]
        pm._publish(degraded, pm.summarize(degraded, ["11"], seen))
        attrs = next(a for eid, a in sets if eid == "sensor.switchboard_link_health")
        check("icon: a real drop-out shows DISCONNECTED",
              attrs.get("icon") == "mdi:lan-disconnect")
    finally:
        sys.modules.pop("ha_client", None)


def test_history_is_written_atomically() -> None:
    # The one durable ledger in /data/state that used to truncate-in-place and
    # rewrite ~1.6 MB every poll. A reader must see the old file or the new one,
    # never a half-written one, and no temp file may be left behind.
    import tempfile as _tf
    d = _tf.mkdtemp()
    saved = pm.STATE_PATH
    try:
        pm.STATE_PATH = os.path.join(d, "linkhealth.jsonl")
        phones = [{"ext": "11", "name": "A", "status": "Avail", "rtt_ms": 2.4,
                   "reachable": True, "registered": True}]
        for _ in range(3):
            pm._append_history(phones)
        recs = [json.loads(l) for l in open(pm.STATE_PATH) if l.strip()]
        check("atomic: every record landed", len(recs) == 3)
        check("atomic: no temp files left behind",
              [f for f in os.listdir(d) if f.startswith(".linkhealth-")] == [])
        check("atomic: records are well-formed", all("ts" in r and "phones" in r for r in recs))

        # THE property, not just the happy path: a failure at the COMMIT point
        # must leave the existing ledger intact. The old in-place rewrite
        # truncated the destination BEFORE writing, so a crash, a full disk, or
        # a container stop inside that window destroyed 1.6 MB of history. Force
        # the commit to fail and prove the ledger survives untouched.
        real_replace = os.replace

        def _boom(*a, **k):
            raise OSError("simulated crash at commit")

        os.replace = _boom
        try:
            pm._append_history(phones)          # must fail SAFELY (errors swallowed)
        finally:
            os.replace = real_replace
        after = [json.loads(l) for l in open(pm.STATE_PATH) if l.strip()]
        check("atomic: a failed commit loses nothing — ledger still intact",
              len(after) == 3 and after == recs)
        check("atomic: and leaves no temp turd behind",
              [f for f in os.listdir(d) if f.startswith(".linkhealth-")] == [])
        # REGRESSION (v0.52.0): mkstemp makes 0600 owned by the writer, and
        # os.replace keeps the TEMP file's mode — not the destination's — so the
        # atomic rewrite silently turned a 0664 asterisk-readable ledger into
        # 0600 root-only. /data/state is asterisk-owned precisely so non-root
        # components can read it.
        import stat as _stat
        mode = _stat.S_IMODE(os.stat(pm.STATE_PATH).st_mode)
        # Least privilege for the actual access pattern: one writer (rtpmon, as
        # root), readers in group asterisk, nothing for world. NOT the sibling
        # ledger's 0664 — that one is written by asterisk-user AGIs.
        check(f"atomic: ledger is 0640 — root writes, group reads (got {oct(mode)})",
              mode == 0o640)
    finally:
        pm.STATE_PATH = saved


def test_trunk_transition() -> None:
    # Mirrors the outage state machine: 2 consecutive settled bad cycles fire
    # once; recovery fires once; warm-up never counts; a MISSING registration
    # object ('') is bad — a trunk configured to register that reports nothing
    # is exactly the silent-death shape found live 2026-08-09.
    st = {"cycles": 0, "alerted": False}
    check("trunk: 1st Rejected cycle silent", pm.trunk_transition("Rejected", st) == "")
    check("trunk: 2nd Rejected cycle fires", pm.trunk_transition("Rejected", st) == "down")
    check("trunk: sustained Rejected does not re-page", pm.trunk_transition("Rejected", st) == "")
    check("trunk: recovery fires 'up' once", pm.trunk_transition("Registered", st) == "up")
    check("trunk: steady Registered silent", pm.trunk_transition("Registered", st) == "")
    st2 = {"cycles": 0, "alerted": False}
    check("trunk: missing registration object is bad",
          pm.trunk_transition("", st2) == "" and st2["cycles"] == 1)
    check("trunk: Unregistered counts as bad too", pm.trunk_transition("Unregistered", st2) == "down")
    st3 = {"cycles": 0, "alerted": False}
    for _ in range(5):
        check("trunk: warm-up cycles never count",
              pm.trunk_transition("Rejected", st3, settled=False) == "")
    check("trunk: warm-up left the counter at zero", st3["cycles"] == 0)
    st4 = {"cycles": 0, "alerted": False}
    check("trunk: single bad cycle then recovery never pages",
          pm.trunk_transition("Rejected", st4) == "" and pm.trunk_transition("Registered", st4) == "")


def test_trunk_check_skips_an_ami_down_cycle() -> None:
    # v0.48.0's documented guard was DEAD CODE: get_registrations() returned {}
    # for BOTH "AMI unreachable" and "no trunk registration exists", so an
    # AMI-down cycle fell through and blanked the sensor to "unknown" — observed
    # on 6/6 restarts. Once settled it would also have counted toward the
    # down-alert and fired a false "outside line down".
    sets, notes = [], []

    class _HA:
        @staticmethod
        def set_state(eid, state, attrs=None):
            sets.append((eid, state)); return True
        @staticmethod
        def notify(msg, title="", notification_id=""):
            notes.append(title); return True

    class _AmiDown:
        @staticmethod
        def get_registrations_or_none():
            return None            # AMI unreachable

    class _AmiUpNoTrunk:
        @staticmethod
        def get_registrations_or_none():
            return {}              # AMI answered; nothing registered

    sys.modules["ha_client"] = _HA
    try:
        sys.modules["ami"] = _AmiDown
        st = {"cycles": 0, "alerted": False}
        pm._trunk_check(st, settled=True, alerts_on=True)
        check("trunk: AMI-down cycle publishes NOTHING", sets == [])
        check("trunk: AMI-down cycle never notifies", notes == [])
        check("trunk: AMI-down cycle does not count toward the alert", st["cycles"] == 0)

        sys.modules["ami"] = _AmiUpNoTrunk
        pm._trunk_check(st, settled=True, alerts_on=True)
        check("trunk: AMI-up-but-unregistered DOES publish (real signal)",
              any(e == "sensor.switchboard_trunk_health" for e, _ in sets))
        check("trunk: AMI-up-but-unregistered counts toward the alert", st["cycles"] == 1)
    finally:
        sys.modules.pop("ami", None); sys.modules.pop("ha_client", None)


def test_trunk_enabled_parses_options() -> None:
    check("trunk: enabled dict", pm.trunk_enabled({"trunk": {"enabled": True}}))
    check("trunk: disabled dict", not pm.trunk_enabled({"trunk": {"enabled": False}}))
    check("trunk: absent key", not pm.trunk_enabled({}))
    check("trunk: non-dict trunk value", not pm.trunk_enabled({"trunk": "yes"}))
    # registns:false is a SUPPORTED setup (auth per-INVITE, no REGISTER) and the
    # generator emits no [trunk-reg] for it. Watching a registration that by
    # design does not exist published `unknown` forever and fired a persistent,
    # never-clearing "outside line down" on a perfectly healthy trunk.
    check("trunk: registns false -> nothing to watch",
          not pm.trunk_enabled({"trunk": {"enabled": True, "registns": False}}))
    check("trunk: registns true -> watched",
          pm.trunk_enabled({"trunk": {"enabled": True, "registns": True}}))
    check("trunk: registns absent defaults to watched (generator's default)",
          pm.trunk_enabled({"trunk": {"enabled": True}}))


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
    test_ip_from_uri()
    test_build_phone_health()
    test_summarize()
    test_room_names()
    test_poll_once_ami_down()
    test_publish_routing()
    test_warmup_done()
    test_warmup_waits_for_every_wired_port()
    test_wired_down_count()
    test_history_append_caps()
    test_is_mass_outage()
    test_outage_transition()
    test_outage_transition_warmup_never_counts()
    test_partial_flag_actually_reaches_home_assistant()
    test_worst_rtt_is_flagged_partial_when_phones_are_missing()
    test_rollup_icon_tracks_the_partial_flag_not_raw_unreachable()
    test_history_is_written_atomically()
    test_trunk_transition()
    test_trunk_check_skips_an_ami_down_cycle()
    test_trunk_enabled_parses_options()
    test_outage_notify_routing()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)


# ── v0.47.0: report the wired fleet apart from the Wi-Fi cordless ────────────

def _ph(ext, rtt, reachable=True, registered=True):
    return {"ext": ext, "name": f"Room {ext}", "rtt_ms": rtt,
            "reachable": reachable, "registered": registered}


def test_median_helper() -> None:
    assert pm._median([]) is None
    assert pm._median([None, None]) is None
    assert pm._median([5]) == 5
    assert pm._median([1, 3]) == 2.0            # even → mean of the middle pair
    assert pm._median([9, 1, 3]) == 3           # unsorted input
    # A single huge sample must NOT drag it (the whole point vs a mean).
    assert pm._median([2, 2, 3, 3, 256]) == 3


def test_wired_summary_is_not_masked_by_the_cordless() -> None:
    """THE REGRESSION THIS EXISTS FOR: the rollup state is a fleet worst case, so
    the cordless idling at ~256 ms pins it and hides the wired ports entirely."""
    wired = ["11", "12", "13", "14", "15", "16", "17", "18"]
    phones = [_ph(e, r) for e, r in zip(wired, [2.0, 2.4, 2.3, 2.5, 2.4, 2.5, 2.0, 4.2])]
    phones.append(_ph("19", 256.6))             # the Wi-Fi cordless, idle
    s = pm.summarize(phones, wired)
    assert s["worst_rtt_ms"] == 256.6 and s["worst_ext"] == "19"   # unchanged
    assert s["wired_median_rtt_ms"] == 2.4, s["wired_median_rtt_ms"]
    assert s["wired_max_rtt_ms"] == 4.2
    assert s["wired_count"] == 8
    assert s["other_rtt_ms"] == {"19": 256.6}
    # And the wired figure MOVES when the gateway degrades, even though the
    # cordless still pins the rollup — which is exactly what was impossible before.
    worse = [_ph(e, 40.0) for e in wired] + [_ph("19", 256.6)]
    s2 = pm.summarize(worse, wired)
    assert s2["worst_rtt_ms"] == 256.6, "rollup still pinned by the cordless"
    assert s2["wired_median_rtt_ms"] == 40.0, "wired median must react"


def test_wired_summary_edge_cases() -> None:
    wired = ["11", "12"]
    # No wired list configured → no wired figures, rollup unaffected.
    s = pm.summarize([_ph("19", 12.0)], None)
    assert s["wired_median_rtt_ms"] is None and s["wired_count"] == 0
    assert s["worst_rtt_ms"] == 12.0
    # Unreachable wired ports contribute no RTT.
    s = pm.summarize([_ph("11", None, reachable=False), _ph("12", 3.0)], wired)
    assert s["wired_median_rtt_ms"] == 3.0 and s["wired_count"] == 1
    # Nothing but wired → other_rtt_ms is None, not an empty dict.
    assert pm.summarize([_ph("11", 3.0)], wired)["other_rtt_ms"] is None


def test_wired_exts_parses_the_gateway_ports_option() -> None:
    assert pm.wired_exts({"gateway_ports": "11,12,13"}) == ["11", "12", "13"]
    assert pm.wired_exts({"gateway_ports": " 11 , 12 ,, "}) == ["11", "12"]
    assert pm.wired_exts({}) == []
    assert pm.wired_exts({"gateway_ports": None}) == []
