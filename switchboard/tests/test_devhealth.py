"""Behavioral tests for the device-health monitor's pure logic.

    python3 switchboard/tests/test_devhealth.py

Pins classify_cordless (the ok/degraded/critical rules that decide whether the alarm
cordless is healthy), classify_gateway (deriving GXW health from which ports are down),
health_transition (the alert state machine), and judge_rtp_records / newest_call (newest
ledger-matched call MOS, recency-gated and corroborated against the ledger).
The poll loop itself, run(), is driven against a fake WP826 API and a real ledger
file (see _drive_cordless); the WP826's HTTPS socket is exercised only as far as the
certificate-pin check (mirrors how test_rtpmon.py leaves the AMI socket untested).
"""
import json
import os
import stat
import sys
import time
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


def _last_call_mos(rtp, now=None, ledger_ts=None):
    """The (mos, age) view these long-standing assertions were written against,
    over the functions probe_cordless actually calls. `ledger_ts` numbers become
    untagged legs; None keeps the ungated behaviour."""
    ledger = None if ledger_ts is None else [{"ts": float(t), "tag": ""} for t in ledger_ts]
    best = dh.newest_call(dh.judge_rtp_records(rtp, ledger), now=now)
    return (None, None) if best is None else (best["mos"], best["age_s"])


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
         "wifi_connected": True, "wifi_signal": 4, "last_mos": 2.9, "last_mos_age_s": 30,
         "last_mos_ledger_tx": "impaired"}, TH)
    check("cordless: recent poor MOS -> degraded", lvl == "degraded" and any("MOS" in r for r in why))

    # ...but ONLY when the ledger's own measurement of that leg agrees. Three
    # false 'degraded' episodes on 2026-09-14 rested on a handset 2.2 for legs
    # the ledger measured at 0 % loss on the stream the handset hears
    # (loss_tx_pct); on the 12:42:31Z leg that stream's score, mes_rx, was 88.0.
    # A missing judgement is no support either.
    for tx in ("clean", "unmeasured", None):
        snap = {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
                "wifi_connected": True, "wifi_signal": 4, "last_mos": 2.9, "last_mos_age_s": 30}
        if tx is not None:
            snap["last_mos_ledger_tx"] = tx
        lvl, why = dh.classify_cordless(snap, TH)
        check(f"cordless: recent poor MOS the ledger does not support ({tx}) -> ok",
              lvl == "ok" and why == [])

    # STALE poor MOS (last call 2h ago) must NOT flag — an old bad call can't pin it degraded.
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
         "wifi_connected": True, "wifi_signal": 4, "last_mos": 2.9, "last_mos_age_s": 7200,
         "last_mos_ledger_tx": "impaired"}, TH)
    check("cordless: stale poor MOS -> ok (not latched)", lvl == "ok")

    # Poor MOS with unknown age -> conservatively NOT flagged.
    lvl, why = dh.classify_cordless(
        {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
         "wifi_connected": True, "wifi_signal": 4, "last_mos": 2.9,
         "last_mos_ledger_tx": "impaired"}, TH)
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
    mos, age = _last_call_mos(rtp, now=2050)
    check("mos: picks the NEWEST call's moscq (not min)", mos == 3.1 and age == 50)
    check("mos: empty -> (None, None)", _last_call_mos({}) == (None, None))
    # An older good call doesn't get shadowed by an even-older bad one.
    mos2, _ = _last_call_mos({"a": {"moscq": "2.0", "stopTimeSecond": "10"},
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


def test_a_rollup_measured_before_this_process_started_is_stale() -> None:
    """★ 2026-10-14: the sensor outlives an add-on restart. After the v0.110.1
    restart rtpmon's first cycle found AMI down and published nothing, so
    devhealth's first cycle read the previous instance's rollup, measured two
    minutes earlier and well inside the 750 s age limit, and published gateway
    health 'ok, 8 of 8' while all eight ports were down."""
    import datetime as _dt
    start = 1_760_000_000.4                 # part-way through a second, as a real start is

    def at(ts):
        return {"measured_at": _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
                .isoformat(timespec="seconds"), "poll_interval_s": 300}

    now = start + 2
    old = at(start - 125)
    check("restart: the previous instance's rollup passes the age test alone "
          "(the defect's precondition)", dh.rollup_is_stale(old, now=now) is False)
    check("restart: ...and is stale once it is known to predate this process",
          dh.rollup_is_stale(old, now=now, not_before=start) is True)
    check("restart: a rollup measured after the start is fresh",
          dh.rollup_is_stale(at(start + 1), now=now, not_before=start) is False)
    # rtpmon truncates measured_at to the second, so a measurement 0.3 s AFTER
    # the start carries the start's own second and reads 0.4 s before it.
    same = at(start + 0.3)
    check("restart: a same-second stamp really reads earlier than the start",
          _dt.datetime.fromisoformat(same["measured_at"]).timestamp() < start)
    check("restart: ...and gets the benefit of the doubt",
          dh.rollup_is_stale(same, now=now, not_before=start) is False)
    check("restart: a stamp whose whole second ended before the start is earlier",
          dh.rollup_is_stale(at(start - 1), now=now, not_before=start) is True)
    check("restart: an unstamped rollup is still treated as fresh (older rtpmon)",
          dh.rollup_is_stale({}, now=now, not_before=start) is False)
    check("restart: the age test still applies to a rollup from after the start",
          dh.rollup_is_stale(at(start + 10), now=start + 810, not_before=start) is True)


_GW = ["11", "12", "13", "14", "15", "16", "17", "18"]


def _iso(ts):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="seconds")


class _Clock:
    """The loop's wall and monotonic clocks, moved only by its own sleep, so the
    start, each read and each rollup are different moments as they are live.
    `steps` lists (seconds after the start, jump): from that moment the wall
    clock reads `jump` seconds off, as after an NTP correction, and the
    monotonic clock does not. The start is part-way through a second, as a real
    one is."""

    def __init__(self, steps=(), wall=1_760_000_000.4, mono=50_000.0):
        self.start_wall, self.start_mono = wall, mono
        self.mono, self.steps = mono, list(steps)

    def wall_at(self, mono):
        since = mono - self.start_mono
        return self.start_wall + since + sum(j for at, j in self.steps if since >= at)

    def time(self):
        return self.wall_at(self.mono)

    def monotonic(self):
        return self.mono


def _drive_gateway(rollup_at, cycles, interval=30, clock=None):
    """Run the REAL poll loop with the cordless disabled, so only the gateway
    half runs: the rollup read, the staleness gate, classify, publish and the
    alert state machine. `rollup_at(cycle, clock)` gives the link-health
    attributes the fake HA holds on that cycle. The loop's time and datetime are
    a _Clock that each sleep advances by `interval`: rollup_is_stale() reads
    'now' from datetime, run() reads its start from time, and both must move
    together. Returns (gateway publishes, notifications)."""
    sets, notes = _drive_loop(rollup_at, cycles, interval=interval, clock=clock)
    gw = [(st, a) for e, st, a in sets if e == "sensor.switchboard_gateway_health"]
    return gw, notes


def _drive_loop(rollup_at, cycles, interval=30, clock=None, env=None, probe=None,
                contact_at=None):
    """_drive_gateway's loop, returning every publish as (entity, state, attrs)
    plus the notifications. `env` is applied after the cordless is disabled
    (so CORDLESS_EXT can be set with no IP to probe), and `probe`, when given,
    replaces probe_cordless. `rollup_at` may return None for no rollup at all.
    `contact_at(cycle, clock)`, when given, is the contact_ip rtpmon publishes
    on the cordless's own link sensor (None: the attribute is absent, as for
    an unregistered handset)."""
    import datetime as _dt
    import types
    clk = clock or _Clock()
    sets, notes = [], []
    n = {"c": 0}
    ha = types.ModuleType("ha_client")
    ha.set_state = lambda eid, state, attrs=None: sets.append((eid, state, dict(attrs or {}))) or True
    ha.notify = lambda msg, title=None, notification_id=None: notes.append((title or "", msg)) or True

    def _get(eid):
        if eid != "sensor.switchboard_link_health":
            if contact_at is None or not eid.startswith("sensor.switchboard_link_"):
                return None
            ip = contact_at(n["c"], clk)
            return {"state": "9.1", "attributes": {"contact_ip": ip} if ip else {}}
        attrs = rollup_at(n["c"], clk)
        return None if attrs is None else {"state": "2.5", "attributes": attrs}
    ha.get_state = _get

    class _Stop(Exception):
        pass

    def _sleep(s):
        n["c"] += 1
        if n["c"] >= cycles:
            raise _Stop()
        clk.mono += s

    class _Now(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime.fromtimestamp(clk.time(), tz)

    real_time, real_datetime = dh.time, dh.datetime
    fake_time = types.ModuleType("time")
    fake_time.__dict__.update({k: v for k, v in vars(real_time).items() if not k.startswith("__")})
    fake_time.time, fake_time.monotonic, fake_time.sleep = clk.time, clk.monotonic, _sleep
    fake_datetime = types.SimpleNamespace(datetime=_Now, timezone=_dt.timezone)
    real_ha = sys.modules.get("ha_client")
    real_env = dict(os.environ)
    real_probe = dh.probe_cordless
    try:
        for k in ("CORDLESS_EXT", "GATEWAY_PORTS", "DEVICE_HEALTH_ALERTS"):
            os.environ.pop(k, None)
        os.environ.update({"CORDLESS_IP": "", "CORDLESS_PASSWORD": "",
                           "DEVICE_HEALTH_INTERVAL": str(interval)})
        os.environ.update(env or {})
        if probe is not None:
            dh.probe_cordless = probe
        sys.modules["ha_client"] = ha
        dh.time, dh.datetime = fake_time, fake_datetime
        try:
            dh.run()
        except _Stop:
            pass
    finally:
        dh.time, dh.datetime = real_time, real_datetime
        dh.probe_cordless = real_probe
        if real_ha is None:
            sys.modules.pop("ha_client", None)
        else:
            sys.modules["ha_client"] = real_ha
        os.environ.clear()
        os.environ.update(real_env)
    return sets, notes


def test_after_a_restart_the_previous_instances_rollup_reads_unknown_not_ok() -> None:
    """The 2026-10-14 restart through the real loop. Cycle 1 finds only the old
    instance's last rollup (all eight up, measured 125 s before this process
    started): that must read 'unknown', never 'ok, 8 of 8'. Cycle 2 has the new
    rtpmon's first rollup (all eight still re-registering) and cycle 3 its next
    (all back), each measured after the start and a few seconds before the read,
    and both are used as normal."""
    def rollup(cycle, clk):
        if cycle == 0:
            return {"measured_at": _iso(clk.start_wall - 125), "poll_interval_s": 300,
                    "unreachable_exts": [], "offline_exts": []}
        return {"measured_at": _iso(clk.time() - 5), "poll_interval_s": 300,
                "unreachable_exts": list(_GW) if cycle == 1 else [], "offline_exts": []}

    gw, notes = _drive_gateway(rollup, cycles=3)
    states = [st for st, _a in gw]
    check("restart: one gateway publish per cycle", len(states) == 3)
    check("restart: the previous instance's rollup reads unknown, not ok",
          states[0] == "unknown")
    check("restart: ...and claims no ports up", "ports_up" not in gw[0][1])
    check("restart: the new instance's all-down rollup is used, inside the startup grace",
          states[1] == "degraded" and "re-registers" in " ".join(gw[1][1].get("reasons") or []))
    check("restart: the next fresh rollup reads ok with every port up",
          states[2] == "ok" and gw[2][1].get("ports_up") == 8)
    check("restart: no notification from any of it", notes == [])


def test_a_fresh_rollup_still_drives_gateway_health_from_the_first_cycle() -> None:
    """The normal path the restart gate must not block: rollups rtpmon publishes
    after this process started are judged from the first cycle, the first one
    stamped just before the start in the start's own second (measured_at is
    truncated to the second), and a real partial outage still alerts after its
    two consecutive cycles."""
    def rollup(cycle, clk):
        at = clk.start_wall - 0.3 if cycle == 0 else clk.time() - 20
        return {"measured_at": _iso(at), "poll_interval_s": 300,
                "unreachable_exts": [] if cycle == 0 else ["13", "14"], "offline_exts": []}

    gw, notes = _drive_gateway(rollup, cycles=3)
    states = [st for st, _a in gw]
    check("fresh: the first cycle reads ok, not unknown",
          states[0] == "ok" and gw[0][1].get("ports_up") == 8)
    check("fresh: a partial outage reads degraded", states[1:] == ["degraded", "degraded"])
    check("fresh: ...and alerts once, after two consecutive cycles",
          len(notes) == 1 and "degraded" in notes[0][0])


def test_steady_rollups_are_read_on_every_cycle_whatever_the_wall_clock_does() -> None:
    """The normal path at the production cadence: rtpmon publishes every 300 s
    and devhealth reads every 120 s, so the rollup it reads was measured after
    this process started and is anything up to 240 s old. No cycle may read
    'unknown'.

    The clocks have to move for this to mean anything. With both frozen at the
    start, `not_before=time.time()` (the moment of each read taken for the
    start) and a sign-flipped re-expression passed every test, and either holds
    the sensor at 'unknown' for good at this cadence. The wall clock also steps
    back an hour between the second and third cycles: a start captured once on
    the wall clock would then sit an hour after every new rollup and refuse
    them all, which is why run() re-expresses it from the monotonic clock on
    every cycle."""
    def rollup(cycle, clk):
        # rtpmon's newest publish by now, on ITS schedule: the first just before
        # this process started, inside the same second, then one every 300 s,
        # each stamped with the wall clock of its own moment.
        k = int((clk.mono - clk.start_mono + 0.3) // 300)
        published = clk.start_mono - 0.3 + 300 * k
        served.append(_iso(clk.wall_at(published)))
        return {"measured_at": served[-1], "poll_interval_s": 300,
                "unreachable_exts": [], "offline_exts": []}

    served = []
    clk = _Clock(steps=[(150, -3600)])
    gw, notes = _drive_gateway(rollup, cycles=12, interval=120, clock=clk)
    states = [st for st, _a in gw]
    check("steady: one gateway publish per cycle", len(states) == 12)
    check("steady: the run reads five rtpmon publishes and spans the wall-clock step",
          len(set(served)) == 5 and clk.time() < clk.start_wall)
    check("steady: every cycle reads ok with all eight ports up, none unknown",
          states == ["ok"] * 12 and all(a.get("ports_up") == 8 for _st, a in gw))
    check("steady: no notification", notes == [])


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
    assert _last_call_mos({}) == (None, None)
    assert _last_call_mos(None) == (None, None)
    # The regression itself: a non-empty string must not raise.
    for bad in ("none", "no records", "0"):
        assert _last_call_mos(bad) == (None, None), bad
    # A real mapping still works.
    got = _last_call_mos({"a": {"moscq": "4.3", "stopTimeSecond": "100"}}, now=160)
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
    assert _last_call_mos({"a": {"moscq": "0.0", "stopTimeSecond": "100"}}, now=150) == (None, None)
    assert _last_call_mos({"a": {"moscq": "0.99", "stopTimeSecond": "100"}}, now=150) == (None, None)
    # Exactly 1.0 is the scale floor — a real (terrible) measurement.
    assert _last_call_mos({"a": {"moscq": "1.0", "stopTimeSecond": "100"}}, now=150) == (1.0, 50)
    # A sentinel NEWEST record is ignored as a candidate, so an older valid
    # record within the window is picked — it must neither win nor shadow.
    got = _last_call_mos({"old": {"moscq": "4.1", "stopTimeSecond": "100"},
                            "new": {"moscq": "0.0", "stopTimeSecond": "200"}}, now=260)
    assert got == (4.1, 160)
    # All records sentinel -> nothing at all.
    assert _last_call_mos({"a": {"moscq": "0.0", "stopTimeSecond": "100"},
                             "b": {"moscq": "0.0", "stopTimeSecond": "200"}}, now=260) == (None, None)


def test_last_call_mos_requires_a_ledger_matched_call() -> None:
    """HA announce playback legs leave low-MOS phone RTP records that are NOT
    calls (nowhere in the call ledger) — three false 'degraded' episodes fired
    2026-08-05/06. Only a ledger-confirmed record may drive health."""
    rtp = {"a": {"moscq": "2.5", "stopTimeSecond": "1000"}}
    # A leg within the window confirms the record (90 s inclusive).
    assert _last_call_mos(rtp, now=1100, ledger_ts=[1080]) == (2.5, 100)
    assert _last_call_mos(rtp, now=1100, ledger_ts=[1000 + dh.CALLQOS_MATCH_WINDOW_S]) == (2.5, 100)
    # No leg near it (announce playback) -> skipped entirely.
    assert _last_call_mos(rtp, now=1100, ledger_ts=[1091]) == (None, None)
    assert _last_call_mos(rtp, now=1100, ledger_ts=[2000]) == (None, None)
    # Ledger readable but empty -> NO record qualifies.
    assert _last_call_mos(rtp, now=1100, ledger_ts=[]) == (None, None)
    # ledger_ts=None keeps the legacy ungated behaviour.
    assert _last_call_mos(rtp, now=1100) == (2.5, 100)
    # An unconfirmed NEWER record (the announce leg) must not shadow the
    # confirmed real call before it.
    rtp2 = {"announce": {"moscq": "2.2", "stopTimeSecond": "2000"},
            "call": {"moscq": "4.0", "stopTimeSecond": "1000"}}
    assert _last_call_mos(rtp2, now=2100, ledger_ts=[1005]) == (4.0, 1100)


def test_load_callqos_legs(tmp_path) -> None:
    # Missing / unreadable ledger -> [] (and downstream, no MOS drives health).
    assert dh.load_callqos_legs(str(tmp_path / "nope.jsonl")) == []
    p = tmp_path / "callqos.jsonl"
    p.write_text('{"ts": 100, "ext": "19"}\n'
                 'not json at all\n'
                 '{"no_ts_field": true}\n'
                 '[1, 2]\n'
                 '{"ts": "wat"}\n'
                 '{"ts": 200.5}\n')
    assert [lg["ts"] for lg in dh.load_callqos_legs(str(p))] == [100.0, 200.5]
    # Only the tail is read (the ledger is append-only and unbounded): a leg
    # older than the tail window is not returned, the newest still is.
    big = tmp_path / "big.jsonl"
    filler = "".join('{"pad": "%s"}\n' % ("x" * 120) for _ in range(700))
    big.write_text('{"ts": 1}\n' + filler + '{"ts": 2}\n')
    got = [lg["ts"] for lg in dh.load_callqos_legs(str(big), max_bytes=65536)]
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

    A phone-side RTP record is accepted only if its nearest ledger leg lands
    within CALLQOS_MATCH_WINDOW_S of it. That gate exists for exactly one reason:
    HA announce playback leaves phone-side records with moscq 2.2-2.9 that fired
    three false 'degraded' episodes on 2026-08-05/06.

    It originally worked because playback legs were simply ABSENT from the
    ledger. v0.55.0 then added the rtpqos hook to wakeup-deliver, page and
    announce -- so those legs ARE written now, and began confirming themselves.
    v0.57.0 filtered them out of the loaded list, which opened the next hole: a
    playback leg beside a real call was confirmed BY that call. So the legs are
    now kept, tagged, and a record whose nearest leg is playback is skipped."""
    import json as _json

    led = tmp_path / "callqos.jsonl"
    led.write_text(
        _json.dumps({"ts": 1000.0, "tag": "wakeup-deliver"}) + "\n" +
        _json.dumps({"ts": 2000.0, "tag": "page"}) + "\n" +
        _json.dumps({"ts": 3000.0, "tag": "announce"}) + "\n" +
        _json.dumps({"ts": 4000.0, "tag": "rooms"}) + "\n" +
        _json.dumps({"ts": 5000.0, "tag": ""}) + "\n",
        encoding="utf-8")

    legs = dh.load_callqos_legs(str(led))
    tags = {lg["ts"]: lg["tag"] for lg in legs}
    # Kept, with their tags -- dropping them is what let a neighbour vouch for them.
    assert tags == {1000.0: "wakeup-deliver", 2000.0: "page", 3000.0: "announce",
                    4000.0: "rooms", 5000.0: ""}, tags

    for stop, tag in (("1000", "wakeup-deliver"), ("2000", "page"), ("3000", "announce")):
        judged = dh.judge_rtp_records({"0": {"moscq": "2.2", "stopTimeSecond": stop}}, legs)
        assert judged[0]["match"] == "playback", (tag, judged)
        assert dh.newest_call(judged, now=float(stop) + 60) is None, \
            f"{tag} leg confirmed a 2.2 record"

    # ...while a real conversation at the same score is still reported, because
    # suppressing genuine bad audio would be the opposite failure. So is an
    # untagged legacy record.
    for stop in ("4000", "5000"):
        best = dh.newest_call(dh.judge_rtp_records(
            {"0": {"moscq": "2.2", "stopTimeSecond": stop}}, legs), now=float(stop) + 60)
        assert best is not None and best["mos"] == 2.2 and best["match"] == "call", best


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
         "wifi_signal": 4, "last_mos": 2.2, "last_mos_ledger_tx": "impaired", "last_mos_age_s": 100},
        {"reachable": True, "api_ok": True, "battery_pct": 80, "wifi_connected": True,
         "wifi_signal": 4, "last_mos": 2.2, "last_mos_ledger_tx": "impaired", "last_mos_age_s": 220},
        {"reachable": True, "api_ok": True, "battery_pct": 80, "wifi_connected": True,
         "wifi_signal": 4, "last_mos": 2.2, "last_mos_ledger_tx": "impaired", "last_mos_age_s": 1000},
    ]
    calls = {"n": 0, "sleeps": 0}

    def _probe(ip, pw, pin, cordless_ext=""):
        i = min(calls["n"], len(snaps) - 1)
        calls["n"] += 1
        return dict(snaps[i])

    def _sleep(_n):
        # The sleep cap is what turns a broken stub into a FAILURE rather than a
        # hang: run() catches every exception from the probe, so a stub whose
        # signature no longer matches the call site never counts a cycle, and a
        # stop keyed only on probe calls never came (it hung the suite once).
        calls["sleeps"] += 1
        if calls["n"] >= len(snaps) or calls["sleeps"] > 20:
            raise _Stop()

    saved = {k: getattr(dh, k) for k in
             ("probe_cordless", "resolve_cordless_ip", "_publish_cordless",
              "_publish_gateway", "read_rollup", "_notify")}
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
        dh.read_rollup = lambda not_before=None: None
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


# ── 2026-09-14: a low handset score needs the ledger's agreement, and is kept ─
#
# The WP826 scored moscq 2.2 on an assistant leg (02:51:58Z) and two dial-42
# wake-up legs (12:42:31Z, 13:10:37Z). The handset's own receiver reports showed
# 0 % loss on all three and the ledger scored them MES 87.9-88.0 (that was
# mes_tx, the microphone stream; the judgement reads mes_rx since v0.116.0), and
# three 'degraded' episodes followed. The mechanism is unknown. These pin what
# the monitor does about it: no alert without corroboration, and a capture of
# every low score so the mechanism can be read off the data.

def test_the_corroboration_lines_are_pinned_and_judged_at_their_edges() -> None:
    # Literals, not the constants: a test that reads its bound from its subject
    # passes for any value of it.
    assert dh.CORROBORATE_LOSS_TX_PCT == 1.0
    assert dh.CORROBORATE_MES_RX == 78.0
    j = dh.ledger_tx_judgement
    # The shape of the live legs behind the false episodes: clean on loss and MES.
    for mes, jit in ((88.0, 2.38), (87.9, 5.88), (87.9, 6.5)):
        assert j({"loss_tx_pct": 0.0, "mes_rx": mes, "jitter_tx_last_ms": jit}) == "clean"
    assert j({"loss_tx_pct": 0.99, "mes_rx": 88.0}) == "clean"
    assert j({"loss_tx_pct": 1.0, "mes_rx": 88.0}) == "impaired"
    # The 2026-08-25 wake-up leg's 1.339 % (since rotated out of the ledger).
    # Its handset-bound MES was not kept; loss alone decides, so the MES here
    # is illustrative and deliberately above the line.
    assert j({"loss_tx_pct": 1.339, "mes_rx": 85.8}) == "impaired"
    assert j({"loss_tx_pct": 0.0, "mes_rx": 78.0}) == "clean"
    assert j({"loss_tx_pct": 0.0, "mes_rx": 77.9}) == "impaired"
    # Asterisk writes 0.0 for a direction no RTCP round scored: not a clean leg.
    assert j({"loss_tx_pct": 0.0, "mes_rx": 0.0}) == "unmeasured"
    assert j({"loss_tx_pct": None, "mes_rx": None}) == "unmeasured"
    # Loss is evidence on its own, measured MES or not.
    assert j({"loss_tx_pct": 2.5, "mes_rx": 0.0}) == "impaired"
    assert j({"loss_tx_pct": True, "mes_rx": True}) == "unmeasured"
    assert j({}) == "unmeasured"
    assert j(None) == "unmeasured"


def test_the_handset_score_is_judged_on_the_stream_the_handset_heard() -> None:
    """★ v0.116.0. ledger_tx_judgement read `mes_tx`, which is Asterisk's score
    for the stream it RECEIVES, the handset's microphone. The score for what the
    handset HEARD, built from the handset's own receiver reports, is `mes_rx`:
    in ast_rtp_get_stat() (Asterisk 20.11.1) rxmes is reported_mes, "RTP we sent
    and they received". The loss half was always right, since `loss_tx_pct` is
    the handset's reported loss of the PBX's stream.

    Each pair below disagrees on purpose, so only the right field can pass."""
    j = dh.ledger_tx_judgement
    # The stream the handset heard was bad and its microphone was fine.
    assert j({"loss_tx_pct": 0.0, "mes_rx": 70.0, "mes_tx": 88.0}) == "impaired"
    # The microphone stream was bad and what the handset heard was fine.
    assert j({"loss_tx_pct": 0.0, "mes_rx": 88.0, "mes_tx": 70.0}) == "clean"
    # Scored only in the microphone direction: nothing about what it heard.
    assert j({"loss_tx_pct": 0.0, "mes_rx": 0.0, "mes_tx": 88.0}) == "unmeasured"
    assert j({"loss_tx_pct": 0.0, "mes_tx": 88.0}) == "unmeasured"
    # The loss that counts is the handset's, not the PBX's receive loss.
    assert j({"loss_rx_pct": 5.0, "loss_tx_pct": 0.0, "mes_rx": 88.0}) == "clean"


def test_a_playback_leg_beside_a_real_call_cannot_borrow_its_confirmation() -> None:
    """The live precondition: a wakeup-deliver leg hung up at 13:00:19Z and an
    operator leg at 13:00:31Z. Dropping playback legs and accepting ANY leg
    within 90 s let the delivery's handset score ride in on the operator leg."""
    deliver, operator = 1789390819.0, 1789390831.0
    ledger = [{"ts": deliver, "tag": "wakeup-deliver", "loss_tx_pct": 0.0, "mes_rx": 88.1},
              {"ts": operator, "tag": "operator", "loss_tx_pct": 0.0, "mes_rx": 0.0}]
    assert abs(operator - deliver) <= dh.CALLQOS_MATCH_WINDOW_S   # the old rule's hole
    judged = dh.judge_rtp_records({"r": {"moscq": "2.2", "stopTimeSecond": str(int(deliver))}}, ledger)
    assert judged[0]["match"] == "playback" and judged[0]["leg"]["ts"] == deliver, judged
    assert dh.newest_call(judged, now=operator + 60) is None
    # A record nearest the operator leg IS the operator call.
    j2 = dh.judge_rtp_records({"r": {"moscq": "4.4", "stopTimeSecond": str(int(operator) + 2)}}, ledger)
    assert j2[0]["match"] == "call" and j2[0]["leg"]["tag"] == "operator", j2
    # An exact tie goes to playback, whichever order the ledger lists them in.
    pair = [{"ts": 2000.0, "tag": "rooms"}, {"ts": 2010.0, "tag": "announce"}]
    rec = {"r": {"moscq": "2.2", "stopTimeSecond": "2005"}}
    assert dh.judge_rtp_records(rec, pair)[0]["match"] == "playback"
    assert dh.judge_rtp_records(rec, list(reversed(pair)))[0]["match"] == "playback"
    # No leg near enough -> unmatched, skipped.
    far = dh.judge_rtp_records({"r": {"moscq": "2.2", "stopTimeSecond": "9000"}}, pair)
    assert far[0]["match"] == "unmatched" and dh.newest_call(far) is None


def _live_leg(ts, **over):
    """The 2026-09-14 12:42:31Z dial-42 wake-up leg, as the ledger wrote it."""
    leg = {"v": 3, "ts": ts, "source": "dialplan", "tag": "wakeup", "ext": "19", "dur": 18,
           "rxcount": 908, "txcount": 458, "loss_rx_pct": 0.0, "loss_tx_pct": 0.0,
           "jitter_rx_last_ms": 19.0, "jitter_tx_last_ms": 5.88, "mes_rx": 88.0,
           "mes_tx": 87.9, "rtt_ms": 5.64, "rtt_max_ms": 9.1, "rtt_samples": "multi",
           "quality": "excellent"}
    leg.update(over)
    return leg


def _handset(stop, mos):
    """A WP826 rtpStatus entry. Only moscq/stopTimeSecond are read; the rest is
    what the capture must keep verbatim."""
    return {"moscq": mos, "startTimeSecond": str(stop - 18), "stopTimeSecond": str(stop),
            "packetLost": "0", "jitter": "3", "codec": "PCMU"}


def _drive_cordless(tmp_path, legs, records, cycles=3, cordless_ext=None):
    """Run the REAL poll loop -- probe_cordless, the ledger read, the judgement,
    classify, publish, capture and the alert state machine -- against a fake
    handset API. Returns (cordless publishes, notifications, capture rows, path)."""
    import types
    led = tmp_path / "callqos.jsonl"
    led.write_text("".join(json.dumps(lg) + "\n" for lg in legs))
    cap = tmp_path / "state" / "cordless-mos.jsonl"
    sets, notes = [], []
    ha = types.ModuleType("ha_client")
    ha.set_state = lambda eid, state, attrs=None: sets.append((eid, state, dict(attrs or {}))) or True
    ha.notify = lambda msg, title=None, notification_id=None: notes.append((title or "", msg)) or True
    ha.get_state = lambda eid: None

    class _WP:
        def __init__(self, ip, password, user="admin", timeout=6.0, cert_pin=""):
            pass

        def login(self):
            return True

        def get(self, path):
            if path.startswith("/api-get_battery_status"):
                return {"battery": {"capacity": "100", "status": "Charging", "health": "Good"}}
            if path.startswith("/api-wifi_status_get"):
                return {"status": {"connected": True, "signal": 4, "connection": {"ssid": "test"}}}
            if path.startswith("/api-get_rtp_status"):
                return {"rtpStatus": records}
            return None

    class _Stop(Exception):
        pass
    n = {"c": 0}

    def _sleep(_s):
        n["c"] += 1
        if n["c"] >= cycles:
            raise _Stop()

    saved = {k: getattr(dh, k) for k in ("_WP", "_tcp_open")}
    saved_seen = dict(dh._capture_seen)
    real_sleep, real_ha = dh.time.sleep, sys.modules.get("ha_client")
    real_env = dict(os.environ)
    try:
        for k in ("CORDLESS_EXT", "DEVICE_HEALTH_ALERTS", "CORDLESS_MOS_MIN", "CORDLESS_MOS_WINDOW_S"):
            os.environ.pop(k, None)
        os.environ.update({"CORDLESS_IP": "192.0.2.1", "CORDLESS_PASSWORD": "x",
                           "DEVICE_HEALTH_INTERVAL": "30", "SWITCHBOARD_CALLQOS": str(led),
                           "SWITCHBOARD_CORDLESS_MOS_LOG": str(cap)})
        if cordless_ext is not None:
            os.environ["CORDLESS_EXT"] = cordless_ext
        dh._capture_seen.update(path=None, keys=set())
        dh._WP = _WP
        dh._tcp_open = lambda ip, port, timeout=3.0: True
        sys.modules["ha_client"] = ha
        dh.time.sleep = _sleep
        try:
            dh.run()
        except _Stop:
            pass
    finally:
        for k, v in saved.items():
            setattr(dh, k, v)
        dh._capture_seen.clear()
        dh._capture_seen.update(saved_seen)
        dh.time.sleep = real_sleep
        if real_ha is None:
            sys.modules.pop("ha_client", None)
        else:
            sys.modules["ha_client"] = real_ha
        os.environ.clear()
        os.environ.update(real_env)
    cordless = [(st, a) for e, st, a in sets if e == "sensor.switchboard_cordless_health"]
    rows = [json.loads(ln) for ln in cap.read_text().splitlines()] if cap.exists() else []
    return cordless, notes, rows, cap


def test_the_live_false_degraded_shape_no_longer_degrades_and_is_captured(tmp_path) -> None:
    end = int(time.time()) - 100
    legs = [_live_leg(end - 1800, tag="operator"), _live_leg(end)]
    records = {"record0": _handset(end + 1, "2.2"), "record1": _handset(end - 1800, "4.4")}
    cordless, notes, rows, cap = _drive_cordless(tmp_path, legs, records)

    check("live shape: three cycles published", len(cordless) == 3)
    check("live shape: the state never left ok", all(st == "ok" for st, _ in cordless))
    check("live shape: no degraded alert was sent",
          not any("degraded" in title for title, _ in notes))
    attrs = cordless[-1][1]
    check("live shape: the handset's score is still shown", attrs.get("last_mos") == 2.2)
    check("live shape: ...and marked uncorroborated", attrs.get("last_mos_uncorroborated") is True)
    check("live shape: its age is published at last",
          isinstance(attrs.get("last_mos_age_s"), int) and 90 <= attrs["last_mos_age_s"] <= 400)

    check("capture: one row across three cycles of the same record", len(rows) == 1)
    r = rows[0] if rows else {}
    leg = r.get("leg") or {}
    rec = r.get("record") or {}
    check("capture: the verdict names why it did not count", r.get("verdict") == "uncorroborated")
    check("capture: the matched ledger leg's ts and tag",
          leg.get("ts") == end and leg.get("tag") == "wakeup")
    check("capture: the ledger's figures for both streams ride along",
          leg.get("mes_rx") == 88.0 and leg.get("mes_tx") == 87.9
          and leg.get("txcount") == 458)
    check("capture: the whole handset record, verbatim",
          rec.get("packetLost") == "0" and rec.get("moscq") == "2.2" and rec.get("codec") == "PCMU")
    check("capture: the clock offset between handset and ledger", r.get("leg_offset_s") == 1.0)
    check("capture: root-only, like the private state it sits beside",
          cap.exists() and stat.S_IMODE(os.stat(cap).st_mode) == 0o600)


def test_a_corroborated_low_score_still_degrades(tmp_path) -> None:
    """Suppressing a real problem would be the opposite failure."""
    end = int(time.time()) - 100
    cordless, notes, rows, _ = _drive_cordless(
        tmp_path, [_live_leg(end, loss_tx_pct=2.5)], {"record0": _handset(end + 1, "2.2")})
    check("corroborated: the state is degraded", any(st == "degraded" for st, _ in cordless))
    check("corroborated: the degraded alert fired",
          any("degraded" in title for title, _ in notes))
    check("corroborated: not marked uncorroborated",
          bool(cordless) and cordless[-1][1].get("last_mos_uncorroborated") is False)
    check("corroborated: captured with its verdict",
          [r["verdict"] for r in rows] == ["corroborated"])


def test_a_playback_score_beside_a_real_call_is_skipped_through_the_loop(tmp_path) -> None:
    deliver = int(time.time()) - 100
    legs = [_live_leg(deliver - 600, tag="rooms"),
            _live_leg(deliver, tag="wakeup-deliver"),
            _live_leg(deliver + 12, tag="operator", mes_tx=0.0, mes_rx=0.0)]
    records = {"record0": _handset(deliver, "2.2"), "record1": _handset(deliver - 600, "4.4")}
    cordless, notes, rows, _ = _drive_cordless(tmp_path, legs, records)
    attrs = cordless[-1][1] if cordless else {}
    check("playback: the delivery's 2.2 did not become the last call", attrs.get("last_mos") == 4.4)
    check("playback: a good score publishes uncorroborated as false, not absent",
          attrs.get("last_mos_uncorroborated") is False)
    check("playback: captured, and says it was skipped as playback",
          [(r["verdict"], r["leg"]["tag"]) for r in rows] == [("playback", "wakeup-deliver")])


def test_the_capture_is_private_capped_and_does_not_duplicate_across_a_restart(tmp_path) -> None:
    saved_env = os.environ.pop("SWITCHBOARD_CORDLESS_MOS_LOG", None)
    try:
        check("capture: defaults to the private state directory, never /share",
              dh._capture_path() == "/data/state/cordless-mos.jsonl")
    finally:
        if saved_env is not None:
            os.environ["SWITCHBOARD_CORDLESS_MOS_LOG"] = saved_env
    check("capture: the cap is pinned", dh.CAPTURE_MAX_BYTES == 512 * 1024)

    p = tmp_path / "c.jsonl"
    leg = {"ts": 5001.0, "tag": "wakeup", "loss_tx_pct": 0.0, "mes_rx": 88.0}
    judged = dh.judge_rtp_records(
        {"a": {"moscq": "2.2", "stopTimeSecond": "5000", "startTimeSecond": "4980"}}, [leg])
    saved = dict(dh._capture_seen)
    try:
        dh._capture_seen.update(path=None, keys=set())
        check("capture: first sighting is written",
              dh.capture_low_mos(judged, 3.4, now=5100, path=str(p)) == 1)
        check("capture: the next poll of the same record is not",
              dh.capture_low_mos(judged, 3.4, now=5220, path=str(p)) == 0)
        dh._capture_seen.update(path=None, keys=set())      # a new process
        check("capture: nor after a restart",
              dh.capture_low_mos(judged, 3.4, now=5340, path=str(p)) == 0)
        rejudged = [dict(judged[0], match="unmatched", leg=None, ledger_tx="unmeasured")]
        check("capture: a different judgement of the same record is a new finding",
              dh.capture_low_mos(rejudged, 3.4, now=5400, path=str(p)) == 1)
        at_line = dh.judge_rtp_records({"b": {"moscq": "3.4", "stopTimeSecond": "6000"}},
                                       [{"ts": 6000.0, "tag": "rooms"}])
        check("capture: a score at mos_min is not low",
              dh.capture_low_mos(at_line, 3.4, now=6100, path=str(p)) == 0)
        rows = [json.loads(ln) for ln in p.read_text().splitlines()]
        check("capture: rows in order with their verdicts",
              [r["verdict"] for r in rows] == ["uncorroborated", "unmatched"])

        # Past the cap the OLDEST rows go and the new one lands.
        big = tmp_path / "big.jsonl"
        with open(big, "w", encoding="utf-8") as fh:
            for i in range(700):
                fh.write(json.dumps({"dedupe": f"old{i}", "pad": "x" * 1000}) + "\n")
        assert big.stat().st_size > dh.CAPTURE_MAX_BYTES
        dh._capture_seen.update(path=None, keys=set())
        check("capture: writes past the cap",
              dh.capture_low_mos(judged, 3.4, now=5100, path=str(big)) == 1)
        check("capture: and the file is back under it", big.stat().st_size <= dh.CAPTURE_MAX_BYTES)
        tail = big.read_text().splitlines()
        check("capture: the newest row survives the trim",
              json.loads(tail[-1])["verdict"] == "uncorroborated")
        check("capture: the oldest rows do not", json.loads(tail[0])["dedupe"] != "old0")
    finally:
        dh._capture_seen.clear()
        dh._capture_seen.update(saved)


def test_an_unmeasured_leg_publishes_the_low_score_as_uncorroborated(tmp_path) -> None:
    """The 13:00:31Z operator leg's shape: Asterisk wrote a score of 0.0 because
    no RTCP round scored that direction. That is no support for a low score and no
    evidence the leg was clean either, and the flag must still say so."""
    end = int(time.time()) - 100
    cordless, notes, rows, _ = _drive_cordless(
        tmp_path, [_live_leg(end, tag="operator", mes_tx=0.0, mes_rx=0.0)],
        {"record0": _handset(end + 1, "2.2")})
    check("unmeasured: three cycles published", len(cordless) == 3)
    check("unmeasured: the state stays ok", all(st == "ok" for st, _ in cordless))
    check("unmeasured: no degraded alert", not any("degraded" in t for t, _ in notes))
    attrs = cordless[-1][1] if cordless else {}
    check("unmeasured: the score is shown", attrs.get("last_mos") == 2.2)
    check("unmeasured: and flagged uncorroborated", attrs.get("last_mos_uncorroborated") is True)
    check("unmeasured: captured with its verdict", [r["verdict"] for r in rows] == ["unmeasured"])


# ── Whose leg: a neighbour's figures must not judge the cordless's score ─────
#
# A ledger leg's `ext` is the channel that ran the context: the cordless for its
# own calls and for the playbacks aimed at it, the CALLER for a call made to it,
# the dialling phone for a page dialled from a handset. Matching on time alone
# let a wired phone's leg that hung
# up a second nearer decide the cordless's score with that phone's figures.

def test_the_cordless_is_judged_by_its_own_leg_not_a_neighbours() -> None:
    assert dh.CALLQOS_CLOCK_SLOP_S == 5          # a literal: see the slop edge below
    T = 50000

    def rec(stop, mos="2.2"):
        return {"r": {"moscq": mos, "stopTimeSecond": str(stop)}}
    own_bad = {"ts": float(T), "tag": "wakeup", "ext": "19", "loss_tx_pct": 2.5, "mes_rx": 85.0}
    wired_clean = {"ts": float(T + 3), "tag": "operator", "ext": "12",
                   "loss_tx_pct": 0.0, "mes_rx": 88.0}
    ledger = [own_bad, wired_clean]
    # Precondition: time alone picks the wired phone's leg, one second nearer.
    j0 = dh.judge_rtp_records(rec(T + 2), ledger)[0]
    assert (j0["leg"]["ext"], j0["ledger_tx"], j0["match_rule"]) == ("12", "clean", "nearest"), j0
    j = dh.judge_rtp_records(rec(T + 2), ledger, "19")[0]
    assert (j["match"], j["leg"]["ext"], j["match_rule"], j["ledger_tx"]) == (
        "call", "19", "own-ext", "impaired"), j
    assert dh.mos_verdict(j) == "corroborated"
    # ...and the reverse: a neighbour's loss cannot corroborate a clean own leg.
    j = dh.judge_rtp_records(rec(T + 2), [dict(own_bad, loss_tx_pct=0.0, mes_rx=88.0),
                                          dict(wired_clean, loss_tx_pct=4.0)], "19")[0]
    assert (j["leg"]["ext"], j["ledger_tx"]) == ("19", "clean"), j

    # The slop edge. Record at T+10, wired leg at T+10: an own leg 5 s away still
    # wins; 6 s away it is an earlier call, and the record is the nearer one's.
    edge = [dict(own_bad, ts=float(T + 5)), dict(wired_clean, ts=float(T + 10))]
    assert dh.judge_rtp_records(rec(T + 10), edge, "19")[0]["leg"]["ext"] == "19"
    past = [dict(own_bad, ts=float(T + 4)), dict(wired_clean, ts=float(T + 10))]
    jp = dh.judge_rtp_records(rec(T + 10), past, "19")[0]
    # A call made TO the cordless is logged under the caller.
    assert (jp["match"], jp["leg"]["ext"], jp["match_rule"]) == ("call", "12", "nearest"), jp
    # ...and the caller's transmit figures say nothing about what the cordless heard.
    assert jp["ledger_tx"] == "unmeasured" and dh.mos_verdict(jp) == "unmeasured", jp
    assert dh.judge_rtp_records(rec(T + 10), past)[0]["ledger_tx"] == "clean"   # time alone

    # Preferring the own leg must not reopen the playback gate. A page dialled
    # from a handset is logged under the DIALLER, so a nearest playback leg of any
    # extension still skips...
    page = [dict(own_bad, loss_tx_pct=0.0, mes_rx=88.0),
            {"ts": float(T + 3), "tag": "page", "ext": "12", "mes_rx": 88.0}]
    jg = dh.judge_rtp_records(rec(T + 3), page, "19")[0]
    assert (jg["match"], jg["leg"]["tag"]) == ("playback", "page"), jg
    # ...and an own playback leg within the slop skips even beside a nearer call.
    deliver = [{"ts": float(T), "tag": "wakeup-deliver", "ext": "19", "mes_rx": 88.1},
               {"ts": float(T + 3), "tag": "operator", "ext": "12", "mes_rx": 88.0}]
    jd = dh.judge_rtp_records(rec(T + 2), deliver, "19")[0]
    assert (jd["match"], jd["leg"]["tag"]) == ("playback", "wakeup-deliver"), jd
    assert dh.judge_rtp_records(rec(T + 2), deliver)[0]["match"] == "call"   # time alone let it in

    # No leg, no rule. A padded configured ext and a numeric ledger ext still match.
    assert dh.judge_rtp_records(rec(T + 2), None, "19")[0]["match_rule"] is None
    jn = dh.judge_rtp_records(rec(T + 2), [dict(own_bad, ext=19), wired_clean], " 19 ")[0]
    assert (jn["leg"]["ext"], jn["match_rule"]) == (19, "own-ext"), jn


def test_the_loop_judges_the_cordless_by_its_own_leg(tmp_path) -> None:
    """Through run(): CORDLESS_EXT has to reach the matcher. A wired phone's clean
    leg one second nearer must not clear the cordless's own impaired leg."""
    end = int(time.time()) - 100
    legs = [_live_leg(end, loss_tx_pct=2.5),                     # the cordless's own leg, impaired
            _live_leg(end + 3, tag="operator", ext="12")]        # a wired phone's leg, clean
    records = {"record0": _handset(end + 2, "2.2")}
    cordless, notes, rows, _ = _drive_cordless(tmp_path, legs, records, cordless_ext="19")
    check("own leg: the cordless's impaired leg corroborates its score",
          any(st == "degraded" for st, _ in cordless))
    check("own leg: the degraded alert fired", any("degraded" in t for t, _ in notes))
    check("own leg: the capture names the rule and the leg",
          [(r["verdict"], r["match_rule"], r["leg"]["ext"]) for r in rows]
          == [("corroborated", "own-ext", "19")])


def _misdial_leg(ts, ext="19"):
    """A 2026-09-15 misdial, as the ledger wrote it: ext 19 dialled a room number
    that does not exist, Congestion() rejected it, no channel was ever created.
    Three of these landed within twenty minutes (12:50:54Z, 13:11:49Z, 13:11:52Z).
    Zero duration, no RTP, no MES in either direction."""
    return {"v": 5, "ts": ts, "source": "dialplan", "tag": "rooms", "ext": ext,
            "dur": 0, "billsec_raw": None, "hcause": 34,
            "rxcount": None, "txcount": None,
            "loss_rx_pct": None, "loss_tx_pct": None,
            "mes_rx": None, "mes_tx": None, "mes_worst": None,
            "rtt_ms": None, "rtt_max_ms": None, "rtt_samples": "none",
            "quality": "unreachable", "notify": False,
            "reasons": ["channel could not be created"]}


def test_a_misdial_cannot_be_the_leg_that_judges_the_cordless() -> None:
    """★ 2026-09-15 — a call that never happened was suppressing a real degrade.

    A zero-duration misdial reaches the ledger like anything else, and the
    matcher only ever asked which leg hung up NEAREST. When one of these landed
    closer to a handset RTP record than the genuine call it belonged to, it won,
    `ledger_tx_judgement` had nothing to read, the verdict came back
    'unmeasured', and a low score that a real leg WOULD have corroborated was
    dropped in silence.

    It is not a hypothetical near-miss: all three live misdials were dialled BY
    THE CORDLESS, so the own-extension preference added the same day would have
    handed them the verdict even where distance did not.
    """
    assert dh.leg_carries_no_measurement(_misdial_leg(0.0)) is True
    T = 60000

    def rec(stop, mos="2.2"):
        return {"r": {"moscq": mos, "stopTimeSecond": str(stop)}}

    genuine = {"ts": float(T), "tag": "rooms", "ext": "19",
               "dur": 18, "loss_tx_pct": 0.0, "mes_rx": 70.0, "quality": "fair"}
    misdial = _misdial_leg(float(T + 10))

    # The record belongs to the genuine leg; the misdial hung up 10 s nearer.
    ledger = [genuine, misdial]
    j = dh.judge_rtp_records(rec(T + 12), ledger, "19")[0]
    check("misdial: the leg that carried audio is the one that judges",
          j["leg"]["ts"] == float(T) and j["leg"]["quality"] == "fair")
    check("misdial: so the low score is corroborated, not swallowed",
          j["ledger_tx"] == "impaired" and dh.mos_verdict(j) == "corroborated")
    check("misdial: ledger order cannot change the answer",
          dh.judge_rtp_records(rec(T + 12), list(reversed(ledger)), "19")[0]
          ["leg"]["ts"] == float(T))
    check("misdial: nor can matching on time alone, with no cordless_ext",
          dh.judge_rtp_records(rec(T + 12), ledger)[0]["leg"]["ts"] == float(T))

    # Without the filter the misdial is genuinely nearer — the precondition this
    # test would be vacuous without.
    check("misdial: ...and it really was the nearer leg",
          dh._nearest_leg(T + 12, ledger)["ts"] == float(T + 10))

    # BOTH halves of "carried no call" have to stand on their own, because the
    # live misdial satisfies both at once and would have hidden either one being
    # dead. `_bs = billsec or 0` in callqos means a leg reaches `unreachable`
    # with billsec ABSENT as readily as with billsec 0, and then `dur` is null.
    for name, variant in (("with no duration field at all", dict(misdial, dur=None)),
                          ("with no quality label", dict(misdial, quality="unknown"))):
        check(f"misdial: a misdial {name} carries nothing too",
              dh.leg_carries_no_measurement(variant) is True)
        check(f"misdial: ...and is filtered out {name}",
              dh.judge_rtp_records(rec(T + 12), [genuine, variant], "19")[0]
              ["leg"]["ts"] == float(T))

    # A misdial is still MATCHED when it is all there is: the record is honestly
    # unmeasured, which is a different claim from being dropped.
    lone = dh.judge_rtp_records(rec(T + 12), [misdial], "19")[0]
    check("misdial: alone, it still matches and reads unmeasured",
          lone["match"] == "call" and dh.mos_verdict(lone) == "unmeasured")

    # ...and the playback gate is untouched. A page is not a measurement of the
    # link, but it IS the reason a handset score must be skipped, so the filter
    # must never drop one in favour of a call further away.
    page = [{"ts": float(T + 10), "tag": "page", "ext": "12", "dur": 0,
             "mes_rx": None, "quality": "unreachable"},
            dict(genuine)]
    check("misdial: a playback leg is never filtered out of the way",
          dh.judge_rtp_records(rec(T + 12), page, "19")[0]["match"] == "playback")

    # The narrow edge that keeps the 13:00:31Z operator leg working: a REAL call
    # Asterisk could not score is a measurement that came back empty, not a call
    # that never happened. It stays eligible and stays 'unmeasured'.
    unscored = {"ts": float(T + 10), "tag": "operator", "ext": "19",
                "dur": 18, "loss_tx_pct": 0.0, "mes_rx": 0.0, "quality": "unknown"}
    check("misdial: an answered leg Asterisk could not score is still a leg",
          dh.leg_carries_no_measurement(unscored) is False)
    ju = dh.judge_rtp_records(rec(T + 12), [genuine, unscored], "19")[0]
    check("misdial: ...and it still wins on distance, as it always did",
          ju["leg"]["ts"] == float(T + 10) and dh.mos_verdict(ju) == "unmeasured")


# ── v0.119.0: the 2026-11-12 outage ──────────────────────────────────────────
#
# The host moved to a new address at 18:28 MST and every phone was unregistered
# from 18:31:04. cordless_health read 'ok' throughout: nothing in it reads SIP
# registration, and its LAN and admin API still answered. The gateway alert
# said the GXW "likely lost power or its uplink", with the cordless down too.

_HEALTHY_SNAP = {"reachable": True, "api_ok": True, "battery_pct": 80, "charging": True,
                 "wifi_connected": True, "wifi_signal": 4}
_PAST_GRACE = 360          # GATEWAY_STARTUP_GRACE_S, pinned as a literal


def test_an_unregistered_cordless_is_critical_after_the_startup_grace() -> None:
    check("B: the grace is the gateway's, 360 s", dh.GATEWAY_STARTUP_GRACE_S == _PAST_GRACE)
    snap = {**_HEALTHY_SNAP, "sip_registered": False}
    lvl, why = dh.classify_cordless(snap, TH, uptime_s=_PAST_GRACE)
    check("B: LAN and API fine but not registered -> critical", lvl == "critical")
    check("B: ...saying it cannot ring",
          why and "not registered with Switchboard" in why[0] and "cannot ring" in why[0])
    lvl, why = dh.classify_cordless(snap, TH, uptime_s=_PAST_GRACE - 1)
    check("B: inside the grace it is still re-registering -> nothing raised",
          lvl == "ok" and why == [])
    check("B: no uptime given keeps the strict reading",
          dh.classify_cordless(snap, TH)[0] == "critical")
    for reg in (True, None):
        lvl, why = dh.classify_cordless({**_HEALTHY_SNAP, "sip_registered": reg}, TH,
                                        uptime_s=_PAST_GRACE)
        check(f"B: registration {reg} -> ok (unknown never raises)", lvl == "ok" and why == [])
    lvl, why = dh.classify_cordless({**_HEALTHY_SNAP, "battery_pct": 25, "charging": False,
                                     "sip_registered": False}, TH, uptime_s=_PAST_GRACE)
    check("B: with a degraded reason beside it, still critical, both named",
          lvl == "critical" and len(why) == 2 and "low" in why[1])
    lvl, why = dh.classify_cordless({"reachable": False, "api_ok": False, "sip_registered": False},
                                    TH, uptime_s=_PAST_GRACE)
    check("B: unreachable AND unregistered names both, unreachable first",
          lvl == "critical" and "offline" in why[0] and "not registered" in why[1])


def test_a_cordless_that_was_not_probed_is_judged_on_registration_alone() -> None:
    """With cordless_ip blank (the default) and no contact_ip (an unregistered
    handset publishes none), there is nothing to probe. Its LAN state is then
    unknown, not 'unreachable'."""
    skipped = {"reachable": None, "api_ok": False}
    lvl, why = dh.classify_cordless({**skipped, "sip_registered": True}, TH, uptime_s=_PAST_GRACE)
    check("B: not probed, registered -> ok, no 'unreachable', no API reason",
          lvl == "ok" and why == [])
    lvl, why = dh.classify_cordless({**skipped, "sip_registered": False}, TH, uptime_s=_PAST_GRACE)
    check("B: not probed, unregistered -> critical on registration alone",
          lvl == "critical" and len(why) == 1 and "not registered" in why[0])
    check("B: a probed handset that answers nothing is still 'unreachable'",
          "offline" in dh.classify_cordless({"reachable": False, "api_ok": False}, TH)[1][0])


def test_the_cordless_registration_is_read_off_the_rollup() -> None:
    reg = dh.cordless_registration
    base = {"unreachable_exts": ["13", "19", "20"], "offline_exts": ["19", "20"],
            "never_registered_exts": ["20"], "other_rtt_ms": None}
    check("B: in offline_exts -> not registered", reg(base, "19") is False)
    check("B: unreachable but not offline -> registered (qualify failing)",
          reg({**base, "offline_exts": ["20"]}, "19") is True)
    check("B: reachable with an RTT -> registered",
          reg({"unreachable_exts": [], "offline_exts": [], "other_rtt_ms": {"19": 9.1}}, "19") is True)
    check("B: not mentioned at all -> unknown",
          reg({"unreachable_exts": [], "offline_exts": [], "other_rtt_ms": None}, "19") is None)
    check("B: a never-registered extension -> unknown, even though offline", reg(base, "20") is None)
    check("B: no rollup -> unknown", reg(None, "19") is None)
    check("B: no cordless_ext -> unknown", reg(base, "") is None)
    down = dh.cordless_down_in
    check("C: the cordless offline is down", down(base, "19") is True)
    check("C: unreachable but registered is down too", down({**base, "offline_exts": []}, "19"))
    check("C: a never-registered extension is not", down(base, "20") is False)
    check("C: up, or nothing to judge, is not",
          down({"unreachable_exts": [], "offline_exts": []}, "19") is False
          and down(None, "19") is False and down(base, "") is False)


def test_the_gateway_critical_names_the_host_when_the_cordless_dropped_too() -> None:
    gw = list(_GW)
    lvl, why = dh.classify_gateway(gw, gw, uptime_s=_PAST_GRACE, cordless_dropped_too=True)
    check("C: every port and the cordless down -> critical, naming the host or network",
          lvl == "critical" and "Switchboard host or the network" in why[0]
          and "not the GXW gateway" in why[0] and "lost power" not in why[0])
    lvl, why = dh.classify_gateway(gw, gw, uptime_s=_PAST_GRACE)
    check("C: every port down, cordless up -> the gateway wording",
          lvl == "critical" and "GXW" in why[0] and "lost power" in why[0])
    check("C: 'ports down', not 'unregistered' (the list holds unreachable ports too)",
          "8 gateway ports down" in why[0] and "unregistered" not in why[0])
    lvl, why = dh.classify_gateway(["11", "12"], gw, uptime_s=_PAST_GRACE, cordless_dropped_too=True)
    check("C: a partial outage is unchanged by the cordless",
          lvl == "degraded" and "2 of 8" in why[0])
    lvl, why = dh.classify_gateway(gw, gw, uptime_s=30, cordless_dropped_too=True)
    check("C: inside the grace it is still the re-registering wording",
          lvl == "degraded" and "re-registers" in why[0])


def test_only_a_poor_call_can_lapse_every_other_clear_is_a_recovery() -> None:
    fresh = dh.clear_is_fresh
    mos = [f"{dh.MOS_REASON} (MOS 2.2, 129s ago)"]
    check("B: a poor call that aged out, no newer call -> a lapse",
          fresh(mos, 1000.0, 1000.0) is False)
    check("B: a poor call followed by a newer call -> fresh", fresh(mos, 1500.0, 1000.0) is True)
    check("B: a poor call with no call end at all -> a lapse", fresh(mos, None, None) is False)
    for r in (dh.UNREGISTERED_REASON, "cordless unreachable (alarm/announce endpoint offline)",
              "battery 10% and discharging (below 15% — endpoint will drop)",
              "Wi-Fi disconnected", "Wi-Fi signal weak (1/5)"):
        check(f"B: a clear of {r[:24]!r} is fresh, with no call at all", fresh([r], None, None))
    check("B: a poor call beside a re-measured reason -> fresh",
          fresh(mos + ["Wi-Fi disconnected"], 1000.0, 1000.0) is True)
    check("B: the MOS reason the classifier gives starts with MOS_REASON",
          dh.classify_cordless({**_HEALTHY_SNAP, "last_mos": 2.2, "last_mos_age_s": 30,
                                "last_mos_ledger_tx": "impaired"}, TH)[1][0]
          .startswith(dh.MOS_REASON))


def test_the_cordless_sensor_carries_its_registration() -> None:
    calls = []

    class _FakeHA:
        @staticmethod
        def set_state(eid, state, attrs=None):
            calls.append((eid, state, attrs or {}))
            return True
    sys.modules["ha_client"] = _FakeHA
    try:
        dh._publish_cordless("critical", [dh.UNREGISTERED_REASON],
                             {"reachable": None, "api_ok": False, "sip_registered": False})
        dh._publish_cordless("ok", [], {**_HEALTHY_SNAP, "sip_registered": True})
        dh._publish_cordless("ok", [], dict(_HEALTHY_SNAP))
    finally:
        sys.modules.pop("ha_client", None)
    (_e1, _s1, a1), (_e2, _s2, a2), (_e3, _s3, a3) = calls
    check("B: sip_registered published as False", a1["sip_registered"] is False)
    check("B: not probed -> reachable is null, not False", a1["reachable"] is None)
    check("B: registered and probed", a2["sip_registered"] is True and a2["reachable"] is True)
    check("B: unknown registration is published as null", "sip_registered" in a3
          and a3["sip_registered"] is None)
    check("B: one sensor, no new entity",
          {e for e, _s, _a in calls} == {"sensor.switchboard_cordless_health"})


def _reg_rollup(offline_19, ports_down=False, stale=False):
    """A link-health rollup at each cycle: ext 19 offline or up, the gateway's
    ports up or all down, measured 5 s before the read but not before this
    process started (the first, in the start's own second), or long ago."""
    def at(cycle, clk):
        off = offline_19(cycle) if callable(offline_19) else offline_19
        down = list(_GW) if (ports_down(cycle) if callable(ports_down) else ports_down) else []
        is_stale = stale(cycle) if callable(stale) else stale
        return {"measured_at": _iso(clk.time() - 5000 if is_stale
                                    else max(clk.time() - 5, clk.start_wall)),
                "poll_interval_s": 300,
                "unreachable_exts": down + (["19"] if off else []) + ["20"],
                "offline_exts": down + (["19"] if off else []) + ["20"],
                "never_registered_exts": ["20"],
                "other_rtt_ms": None if off else {"19": 9.1}}
    return at


def test_the_real_loop_raises_and_clears_an_unregistered_cordless() -> None:
    """Through run(), with nothing to probe (cordless_ip blank, no contact_ip):
    the registration-only path publishes, alerts on the second unregistered
    sample read after the grace (each read here is a new sample), and reports
    the re-registration as a recovery, not a lapse."""
    sets, notes = _drive_loop(_reg_rollup(lambda c: c < 4), cycles=6, interval=200,
                              env={"CORDLESS_EXT": "19"})
    cl = [(st, a) for e, st, a in sets if e == "sensor.switchboard_cordless_health"]
    check("loop B: the cordless is published every cycle with nothing to probe", len(cl) == 6)
    check("loop B: inside the grace, ok; one sample past it, still ok; two, critical; back, ok",
          [st for st, _a in cl] == ["ok", "ok", "ok", "critical", "ok", "ok"])
    check("loop B: sip_registered tracks the rollup",
          [a.get("sip_registered") for _st, a in cl] == [False] * 4 + [True] * 2)
    check("loop B: LAN reachability is unknown, not False",
          all(a.get("reachable") is None for _st, a in cl))
    cordless_notes = [(t, m) for t, m in notes if "cordless" in t]
    check("loop B: one CRITICAL, then a recovery",
          [t for t, _m in cordless_notes] == ["Switchboard: cordless CRITICAL",
                                             "Switchboard: cordless OK"])
    check("loop B: the alert says it cannot ring",
          cordless_notes and "not registered with Switchboard" in cordless_notes[0][1])
    check("loop B: the recovery is 'back to normal', not 'alert lapsed'",
          len(cordless_notes) == 2 and "back to normal" in cordless_notes[1][1])


def test_the_real_loop_raises_nothing_for_the_cordless_without_a_rollup() -> None:
    sets, notes = _drive_loop(lambda c, clk: None, cycles=4, interval=200,
                              env={"CORDLESS_EXT": "19"})
    check("loop B: no rollup and nothing to probe -> no cordless publish",
          not any(e == "sensor.switchboard_cordless_health" for e, _s, _a in sets))
    stale_sets, stale_notes = _drive_loop(_reg_rollup(True, stale=True), cycles=4, interval=200,
                                          env={"CORDLESS_EXT": "19"})
    check("loop B: a stale rollup saying offline raises nothing",
          stale_notes == [] and notes == []
          and not any(e == "sensor.switchboard_cordless_health" for e, _s, _a in stale_sets))

    def malformed(cycle, clk):
        return {"measured_at": _iso(max(clk.time() - 5, clk.start_wall)),
                "poll_interval_s": 300, "unreachable_exts": [], "offline_exts": 5}
    bad_sets, bad_notes = _drive_loop(malformed, cycles=3, interval=200,
                                      env={"CORDLESS_EXT": "19"})
    gw = [st for e, st, _a in bad_sets if e == "sensor.switchboard_gateway_health"]
    check("loop B: a malformed rollup reads as none: the loop runs on, gateway unknown",
          gw == ["unknown"] * 3 and bad_notes == [])


def test_unknown_registration_neither_clears_nor_raises_a_registration_alert() -> None:
    """The LAN probe alone cannot say the cordless can ring again: while the
    rollup is unusable an outstanding registration alert is held."""
    def probe(ip, pw, pin, cordless_ext=""):
        return dict(_HEALTHY_SNAP)

    def drive(cycles):
        sets, notes = _drive_loop(_reg_rollup(lambda c: c < 4, stale=lambda c: c in (4, 5)),
                                  cycles=cycles, interval=200, probe=probe,
                                  env={"CORDLESS_EXT": "19", "CORDLESS_IP": "192.0.2.1"})
        cl = [(st, a) for e, st, a in sets if e == "sensor.switchboard_cordless_health"]
        return cl, notes, [t for t, _m in notes if "cordless" in t]
    # Cycles 2-3 unregistered past the grace, 4-5 no usable rollup, 6 registered.
    cl, _notes, blind = drive(6)
    check("hold: precondition — the probe ran every cycle", len(cl) == 6)
    check("hold: unknown registration publishes as null", cl[4][1].get("sip_registered") is None)
    check("hold: precondition — the LAN probe alone reads ok while blind",
          [st for st, _a in cl][4:] == ["ok", "ok"])
    check("hold: no recovery while registration is unknown",
          blind == ["Switchboard: cordless CRITICAL"])
    _cl, notes, titles = drive(7)
    check("hold: the recovery comes once the rollup shows it registered",
          titles == ["Switchboard: cordless CRITICAL", "Switchboard: cordless OK"])
    ok_note = [m for t, m in notes if t == "Switchboard: cordless OK"]
    check("hold: the recovery is a recovery", ok_note and "back to normal" in ok_note[0])


def test_a_blind_cycle_neither_unlocks_a_false_all_clear_nor_counts_toward_an_alert() -> None:
    """The hold's own guard: a blind cycle whose LAN probe is unhealthy must
    not replace the registration reason it is holding, or the next blind,
    healthy cycle has nothing to hold and posts 'Cordless recovered — back to
    normal' while registration is still unknown."""
    env = {"CORDLESS_EXT": "19", "CORDLESS_IP": "192.0.2.1"}
    calls = {"n": 0}

    def probe(ip, pw, pin, cordless_ext=""):
        c = calls["n"]
        calls["n"] += 1
        return {"reachable": False, "api_ok": False} if c == 4 else dict(_HEALTHY_SNAP)
    # Cycles 2-3 unregistered past the grace (the CRITICAL); 4 blind with the
    # handset unreachable on the LAN; 5 blind and healthy.
    sets, notes = _drive_loop(_reg_rollup(lambda c: c < 4, stale=lambda c: c in (4, 5)),
                              cycles=6, interval=200, probe=probe, env=env)
    cl = [st for e, st, _a in sets if e == "sensor.switchboard_cordless_health"]
    check("hold: precondition — the blind cycle read critical on the LAN probe",
          cl[4] == "critical" and cl[5] == "ok")
    check("hold: no all-clear while registration is unknown",
          [t for t, _m in notes if "cordless" in t] == ["Switchboard: cordless CRITICAL"])
    # Unregistered in the samples of cycles 2 and 4 (past the grace) with a
    # blind cycle 3 between: two unregistered samples, but not consecutive ones.
    blind = _reg_rollup(lambda c: c in (2, 4), stale=lambda c: c == 3)
    _s, notes = _drive_loop(blind, cycles=5, interval=200,
                            probe=lambda *a, **k: dict(_HEALTHY_SNAP), env=env)
    check("hold: a blind cycle before the alert resets the count",
          [t for t, _m in notes if "cordless" in t] == [])
    _s, notes = _drive_loop(_reg_rollup(lambda c: c in (2, 4)), cycles=5, interval=200,
                            probe=lambda *a, **k: dict(_HEALTHY_SNAP), env=env)
    check("hold: a registered sample between resets it too",
          [t for t, _m in notes if "cordless" in t] == [])
    _s, notes = _drive_loop(_reg_rollup(lambda c: c in (2, 4, 5), stale=lambda c: c == 3),
                            cycles=6, interval=200,
                            probe=lambda *a, **k: dict(_HEALTHY_SNAP), env=env)
    check("hold: control — two fresh samples in a row after the blind cycle raise it",
          [t for t, _m in notes if "cordless" in t] == ["Switchboard: cordless CRITICAL"])


def test_the_registration_count_is_by_distinct_rollup_sample() -> None:
    note, MIN = dh.note_registration, dh.REGISTRATION_MIN_SAMPLES
    check("count: two samples", MIN == 2)
    st = {}
    check("count: the first unregistered sample counts one", note(st, False, 1000.0) == 1)
    check("count: the same sample read again does not count again",
          note(st, False, 1000.0) == 1 and note(st, False, 1000.0) == 1)
    check("count: the next sample counts two", note(st, False, 1300.0) == 2)
    check("count: a registered sample resets it", note(st, True, 1600.0) == 0)
    check("count: ...and a later loss starts again from one", note(st, False, 1900.0) == 1)
    check("count: unknown below the minimum resets it",
          note(st, None, None) == 0 and note(st, False, 2200.0) == 1)
    st = {}
    note(st, False, 1000.0)
    note(st, False, 1300.0)
    check("count: unknown at the minimum keeps it (the alert is held)",
          note(st, None, None) == 2 and note(st, False, 1300.0) == 2)
    check("count: the sample after the blind cycle counts on", note(st, False, 1600.0) == 3)
    st = {}
    check("count: a rollup with no stamp never counts",
          note(st, False, None) == 0 and note(st, False, None) == 0)
    st = {}
    check("count: ...nor after a stamped one",
          note(st, False, 1000.0) == 1 and note(st, False, None) == 1)
    snap = {**_HEALTHY_SNAP, "sip_registered": False}
    check("count: one sample is not enough for the critical",
          dh.classify_cordless({**snap, "sip_unregistered_samples": 1}, TH,
                               uptime_s=_PAST_GRACE) == ("ok", []))
    check("count: two are",
          dh.classify_cordless({**snap, "sip_unregistered_samples": 2}, TH,
                               uptime_s=_PAST_GRACE)[0] == "critical")
    check("count: one sample beside an unreachable LAN names only the LAN",
          dh.classify_cordless({"reachable": False, "api_ok": False, "sip_registered": False,
                                "sip_unregistered_samples": 1}, TH, uptime_s=_PAST_GRACE)
          == ("critical", ["cordless unreachable (alarm/announce endpoint offline)"]))


def _sampled_rollup(offline_in, period=300, stale=None):
    """rtpmon's rollup as Home Assistant holds it: sample k is measured `period`
    seconds after sample k - 1 (sample 0 in this process's start second) and
    stays until the next, so a 120 s poller reads most samples two or three
    times. `offline_in(k)`: ext 19 unregistered in sample k. `stale(cycle)`: no
    usable rollup on that read."""
    def at(cycle, clk):
        k = int((clk.time() - clk.start_wall) // period)
        off = offline_in(k)
        is_stale = stale(cycle) if stale else False
        return {"measured_at": _iso(clk.start_wall + period * k - (5000 if is_stale else 0)),
                "poll_interval_s": period,
                "unreachable_exts": (["19"] if off else []) + ["20"],
                "offline_exts": (["19"] if off else []) + ["20"],
                "never_registered_exts": ["20"],
                "other_rtt_ms": None if off else {"19": 9.1}}
    return at


def _cordless_at_cadence(offline_in, cycles, stale=None, probed=False):
    """The real loop at the default 120 s against a 300 s rollup: the
    cordless's published states, its sip_registered attribute, and its
    notification titles and texts. `probed`: a handset with an IP whose LAN
    probe always reads healthy, so the cordless is published on a blind read."""
    env = {"CORDLESS_EXT": "19"}
    if probed:
        env["CORDLESS_IP"] = "192.0.2.1"
    sets, notes = _drive_loop(_sampled_rollup(offline_in, stale=stale), cycles=cycles,
                              interval=120, env=env,
                              probe=(lambda *a, **k: dict(_HEALTHY_SNAP)) if probed else None)
    cl = [(st, a) for e, st, a in sets if e == "sensor.switchboard_cordless_health"]
    return ([st for st, _a in cl], [a.get("sip_registered") for _st, a in cl],
            [(t, m) for t, m in notes if "cordless" in t])


def test_a_handset_rebooting_inside_one_sample_raises_nothing() -> None:
    """A WP826 firmware auto-upgrade reboots it at 01:00 for two to four
    minutes. One 300 s sample catches it; this poller reads that sample two or
    three times, and two of its own cycles used to make the CRITICAL."""
    # Reads at 0, 120, ... s; sample k covers [300k, 300k + 300). Sample 2 is
    # read at 600, 720 and 840 s, all past the 360 s grace.
    states, reg, notes = _cordless_at_cadence(lambda k: k == 2, cycles=10)
    check("reboot: precondition — the unregistered sample was read three times",
          reg.count(False) == 3)
    check("reboot: nothing is raised", notes == [])
    check("reboot: the sensor stays ok throughout, with the rollup's reading beside it",
          states == ["ok"] * 10 and reg[5:8] == [False, False, False])


def test_a_registration_lost_on_two_samples_is_critical_until_a_fresh_one_clears_it() -> None:
    states, _reg, notes = _cordless_at_cadence(lambda k: k in (2, 3), cycles=11)
    check("two samples: critical from the first read of the second sample (960 s), "
          "and back with the next sample (1200 s)",
          states == ["ok"] * 8 + ["critical", "critical", "ok"])
    check("two samples: one CRITICAL, then a recovery",
          [t for t, _m in notes] == ["Switchboard: cordless CRITICAL", "Switchboard: cordless OK"])
    check("two samples: the recovery is a recovery",
          len(notes) == 2 and "back to normal" in notes[1][1])
    _s, _r, early = _cordless_at_cadence(lambda k: k in (2, 3), cycles=9)
    check("two samples: the CRITICAL goes out on that first read, not a cycle later",
          [t for t, _m in early] == ["Switchboard: cordless CRITICAL"])
    # Unregistered from the start: inside the 360 s grace nothing counts, so the
    # two samples are the ones read after it (300 s at 360 s, 600 s at 600 s).
    states, _reg, notes = _cordless_at_cadence(lambda k: True, cycles=7)
    check("grace: nothing counts inside it; the second sample read after it is critical",
          states == ["ok"] * 5 + ["critical", "critical"]
          and [t for t, _m in notes] == ["Switchboard: cordless CRITICAL"])
    # A blind read while the alert is out holds it, though the LAN probe alone
    # reads ok, and re-reading the sample that raised it does not clear it.
    states, reg, notes = _cordless_at_cadence(lambda k: k in (2, 3), cycles=11,
                                              stale=lambda c: c == 9, probed=True)
    check("hold: precondition — the blind read published ok off the LAN probe",
          reg[9] is None and states[9] == "ok")
    check("hold: a blind read neither clears nor repeats the alert",
          [t for t, _m in notes] == ["Switchboard: cordless CRITICAL", "Switchboard: cordless OK"]
          and states[8] == "critical" and states[10] == "ok")
    states, _reg, notes = _cordless_at_cadence(lambda k: k in (2, 3, 4), cycles=11,
                                               stale=lambda c: c == 9, probed=True)
    check("hold: still unregistered after the blind read -> still critical, no recovery",
          [t for t, _m in notes] == ["Switchboard: cordless CRITICAL"] and states[10] == "critical")


def test_only_a_registration_loss_skips_the_two_cycle_gate() -> None:
    """The registration CRITICAL has its own gate (two samples); every other
    reason still needs two consecutive cycles, as before."""
    def flaky(bad):
        calls = {"n": 0}

        def probe(ip, pw, pin, cordless_ext=""):
            c = calls["n"]
            calls["n"] += 1
            return {"reachable": False, "api_ok": False} if c in bad else dict(_HEALTHY_SNAP)
        return probe
    env = {"CORDLESS_EXT": "19", "CORDLESS_IP": "192.0.2.1"}
    for bad, want in (({6}, []), ({6, 7}, ["Switchboard: cordless CRITICAL",
                                            "Switchboard: cordless OK"])):
        _s, notes = _drive_loop(_sampled_rollup(lambda k: False), cycles=9, interval=120,
                                probe=flaky(bad), env=env)
        check(f"gate: unreachable on the LAN for {len(bad)} cycle(s) -> "
              f"{'an alert' if want else 'nothing'}",
              [t for t, _m in notes if "cordless" in t] == want)


def _gateway_critical(offline_19, ports_down, cycles, stale=False):
    """The real loop at 200 s a cycle (a fresh rollup each read, rtpmon's
    interval 300 s): the gateway CRITICAL texts, and the gateway sensor's
    reasons on the last cycle."""
    sets, notes = _drive_loop(_reg_rollup(offline_19, ports_down=ports_down, stale=stale),
                              cycles=cycles, interval=200, env={"CORDLESS_EXT": "19"})
    gw = [st_a for e, *st_a in sets if e == "sensor.switchboard_gateway_health"]
    return ([m for t, m in notes if t == "Switchboard: gxw gateway CRITICAL"],
            gw[-1][1].get("reasons") if gw else None)


def test_the_real_loop_names_the_host_when_the_ports_and_the_cordless_drop() -> None:
    gw, _r = _gateway_critical(lambda c: c >= 1, lambda c: c >= 1, cycles=5)
    check("loop C: one gateway CRITICAL", len(gw) == 1)
    check("loop C: it names the host or the network, not a gateway power loss",
          gw and "Switchboard host or the network" in gw[0] and "lost power" not in gw[0])
    gw2, _r = _gateway_critical(False, lambda c: c >= 1, cycles=5)
    check("loop C: the cordless up -> the gateway wording",
          len(gw2) == 1 and "GXW gateway likely lost power" in gw2[0])


def test_the_gateway_critical_names_the_host_only_for_a_cordless_that_dropped_with_the_ports() -> None:
    """The cordless merely down in the same rollup was enough: one asleep (or
    with a dead battery) half an hour before the gateway lost power made the
    gateway CRITICAL say "not the GXW gateway"."""
    host = lambda m: "Switchboard host or the network" in m and "not the GXW gateway" in m  # noqa: E731
    gw, _r = _gateway_critical(lambda c: c >= 1, lambda c: c >= 6, cycles=9)
    check("cause 10: a cordless down 1000 s before the ports -> the gateway wording",
          len(gw) == 1 and "GXW gateway likely lost power" in gw[0] and not host(gw[0]))
    gw, _r = _gateway_critical(lambda c: c >= 2, lambda c: c >= 1, cycles=5)
    check("cause: the cordless one rollup after the ports still dropped with them",
          len(gw) == 1 and host(gw[0]))
    gw, _r = _gateway_critical(True, True, cycles=5)
    check("cause: both already down at the first reading -> the gateway wording",
          len(gw) == 1 and not host(gw[0]))
    gw, _r = _gateway_critical(lambda c: c >= 2, lambda c: c >= 2, cycles=6,
                               stale=lambda c: c == 1)
    check("cause: both first seen down after a blind cycle -> the gateway wording",
          len(gw) == 1 and not host(gw[0]))
    gw, reasons = _gateway_critical(lambda c: c >= 6, lambda c: c >= 1, cycles=9)
    check("cause: a cordless that drops long after the ports leaves the alert as it was",
          len(gw) == 1 and not host(gw[0]))
    check("cause: ...and the sensor's reasons do not turn to the host later",
          reasons and "GXW gateway likely lost power" in reasons[0])
    # Two ports down for a while (handsets unplugged), then the rest of the
    # ports and the cordless at once: the ports' run starts when the LAST one
    # drops, which is when the gateway reads all-down.
    together = _reg_rollup(lambda c: c >= 5, ports_down=lambda c: c >= 5)

    def two_first(cycle, clk):
        attrs = together(cycle, clk)
        if 1 <= cycle < 5:
            for key in ("unreachable_exts", "offline_exts"):
                attrs[key] = ["11", "12"] + attrs[key]
        return attrs
    _sets, notes = _drive_loop(two_first, cycles=8, interval=200, env={"CORDLESS_EXT": "19"})
    gw = [m for t, m in notes if t == "Switchboard: gxw gateway CRITICAL"]
    check("cause: two ports down earlier, then the rest with the cordless -> the host",
          len(gw) == 1 and host(gw[0]))
    run, other = {}, {}
    dh.note_down_run(run, True, 1000.0)
    check("runs: a run in progress at the first reading has no start",
          run.get("since") is None)
    dh.note_down_run(run, False, 1300.0)
    dh.note_down_run(run, True, 1600.0)
    dh.note_down_run(run, True, 1900.0)
    check("runs: seen up, then down -> the first down rollup's time", run["since"] == 1600.0)
    dh.note_down_run(other, False, 1300.0)
    dh.note_down_run(other, True, 2050.0)
    check("runs: the cordless 450 s after the ports is one rollup (300 s) with jitter",
          dh.dropped_together(other, run, 300.0))
    check("runs: ...450 s before them is not", not dh.dropped_together(run, other, 300.0))
    dh.note_down_run(other, False, 2100.0)
    dh.note_down_run(other, True, 2200.0)
    check("runs: 600 s apart is two rollups", not dh.dropped_together(other, run, 300.0))
    blind = {}
    dh.note_down_run(blind, False, 1000.0)
    dh.note_down_run(blind, None, 0.0)
    dh.note_down_run(blind, True, 1600.0)
    check("runs: a blind cycle between up and down hides the start", blind.get("since") is None)
    check("runs: the rollup's stamp is read as an epoch",
          dh.rollup_measured_at({"measured_at": _iso(1_760_000_000)}) == 1_760_000_000.0
          and dh.rollup_measured_at({}) is None
          and dh.rollup_measured_at({"measured_at": "not a time"}) is None)


# --------------------------------------------------------------------------- #
# v0.119.0 review, round 2: no "recovered" off a cycle that cannot say the
# cordless can ring, and one cause rule for devhealth and rtpmon.
# --------------------------------------------------------------------------- #
_POOR_CALL = {**_HEALTHY_SNAP, "last_mos": 2.2, "last_mos_age_s": 30,
              "last_mos_ledger_tx": "impaired"}


def _phased_cordless(phases, interval=200):
    """The real loop with cordless_ip blank (the default) and ext 19's IP
    followed from the contact_ip rtpmon publishes for it, which an unregistered
    handset does not have. Each read is a fresh rollup sample. phases[c]:
      up       registered, LAN and API healthy;
      unreach  registered but failing its qualify (an 'Unavail' contact), and
               its LAN probe answers nothing;
      badcall  registered, a corroborated poor recent call;
      offline  unregistered: no contact_ip, so nothing to probe.
    Returns the cordless's (state, attrs) publishes per cycle (None for a
    cycle that published nothing) and its notifications."""
    clk = _Clock()

    def phase(cycle):
        return phases[min(cycle, len(phases) - 1)]

    def rollup(cycle, _clk):
        p = phase(cycle)
        down = p in ("offline", "unreach")
        return {"measured_at": _iso(max(clk.time() - 5, clk.start_wall)),
                "poll_interval_s": 300,
                "unreachable_exts": ["19"] if down else [],
                "offline_exts": ["19"] if p == "offline" else [],
                "never_registered_exts": [],
                "other_rtt_ms": {} if down else {"19": 9.1}}

    def probe(ip, pw, pin, cordless_ext=""):
        # The loop's clock moves only in its sleep, by `interval`.
        p = phase(int(round((clk.mono - clk.start_mono) / interval)))
        if p == "unreach":
            return {"reachable": False, "api_ok": False}
        return dict(_POOR_CALL if p == "badcall" else _HEALTHY_SNAP)

    def contact(cycle, _clk):
        return None if phase(cycle) == "offline" else "192.0.2.19"
    sets, notes = _drive_loop(rollup, cycles=len(phases), interval=interval, clock=clk,
                              probe=probe, contact_at=contact,
                              env={"CORDLESS_EXT": "19"})
    # The gateway is published once at the end of every cycle, after the
    # cordless.
    per_cycle: list = [None] * len(phases)
    cycle = 0
    for e, st, a in sets:
        if e == "sensor.switchboard_cordless_health":
            per_cycle[cycle] = (st, a)
        elif e == "sensor.switchboard_gateway_health":
            cycle += 1
    return per_cycle, [(t, m) for t, m in notes if "cordless" in t]


def test_an_open_cordless_alert_is_not_cleared_by_a_cycle_with_nothing_probed() -> None:
    """cordless_ip blank, cordless_ext 19. The handset goes off the LAN (the
    probe of its last contact_ip fails twice: CRITICAL 'unreachable'), then
    unregisters. With no contact_ip there is nothing to probe, and one
    unregistered sample is below the registration gate, so the cycle read
    'ok' and posted "Cordless recovered — back to normal" between two
    CRITICALs for one continuous outage."""
    per, notes = _phased_cordless(["up", "up", "unreach", "unreach", "offline", "offline", "up"])
    check("nothing probed: precondition — the 'unreachable' CRITICAL at cycle 3",
          notes[:1] and notes[0][0] == "Switchboard: cordless CRITICAL"
          and "unreachable" in notes[0][1])
    check("nothing probed: one alert, then one recovery once it is registered again",
          [t for t, _m in notes] == ["Switchboard: cordless CRITICAL", "Switchboard: cordless OK"])
    check("nothing probed: the cycle that measured nothing publishes no fresh 'ok'",
          per[4] is None)
    check("nothing probed: the second unregistered sample reads critical, the return ok",
          per[5] and per[5][0] == "critical" and per[6] and per[6][0] == "ok")
    # A poor-call 'degraded' alert open when the handset unregisters: its
    # lapse is not reported off a cycle that measured nothing.
    per, notes = _phased_cordless(["up", "badcall", "badcall", "badcall", "offline", "offline",
                                   "up"])
    check("nothing probed: an open poor-call alert does not 'lapse', it escalates",
          [t for t, _m in notes] == ["Switchboard: cordless degraded",
                                     "Switchboard: cordless CRITICAL",
                                     "Switchboard: cordless OK"])
    check("nothing probed: ...to the registration reason", "not registered" in notes[1][1])
    # The registration CRITICAL, then a sample with the handset registered but
    # failing its qualify (the count restarts, the LAN keeps the alert open),
    # then unregistered again on one sample.
    per, notes = _phased_cordless(["up", "up", "offline", "offline", "unreach", "offline",
                                   "up"])
    check("nothing probed: an 'Unavail' sample between does not unlock a false recovery",
          [t for t, _m in notes] == ["Switchboard: cordless CRITICAL", "Switchboard: cordless OK"]
          and per[5] is None and per[6] and per[6][0] == "ok")


def test_an_open_cordless_alert_is_not_cleared_while_the_rollup_has_it_unregistered() -> None:
    """The 11-07 shape with the LAN probe: the handset's Wi-Fi rides a
    rebooting router, two failed probes raise 'unreachable', and the rollup
    sample in between shows it unregistered. The LAN answering again on the
    next cycle is no evidence that it can ring: the clear waits for a sample
    that shows it registered."""
    calls = {"n": 0}

    def probe(ip, pw, pin, cordless_ext=""):
        c = calls["n"]
        calls["n"] += 1
        return {"reachable": False, "api_ok": False} if c in (10, 11) else dict(_HEALTHY_SNAP)

    def drive(cycles):
        calls["n"] = 0
        _s, notes = _drive_loop(_sampled_rollup(lambda k: k in (4, 5)), cycles=cycles,
                                interval=120, probe=probe,
                                env={"CORDLESS_EXT": "19", "CORDLESS_IP": "192.0.2.1"})
        return [t for t, _m in notes if "cordless" in t]
    # Reads every 120 s against a 300 s rollup: sample 4 (unregistered) is read
    # by cycles 10-12, sample 5 by 13-14, sample 6 (registered) from cycle 15.
    check("LAN back, one unregistered sample: the CRITICAL stays out (no recovery yet)",
          drive(15) == ["Switchboard: cordless CRITICAL"])
    check("...and the recovery comes on the sample that shows it registered",
          drive(16) == ["Switchboard: cordless CRITICAL", "Switchboard: cordless OK"])


def test_devhealth_and_rtpmon_name_one_cause_for_one_outage() -> None:
    """The cordless left the Wi-Fi a sample before the GXW lost power: devhealth
    named the host (the cordless dropped "within one rollup" of the ports)
    while rtpmon's outage card and push named the gateway. Both components
    now use one rule: the cordless was up in the last sample before every
    port was down, and was down by the next one."""
    import test_rtpmon as trt
    period = 300

    def verdicts(samples):
        pushes = []
        rt_notes = trt._drive_samples([(s, []) for s in samples],
                                      target=trt._TARGET, pushes=pushes)

        def rollup_at(cycle, clk):
            k = min(int((clk.time() - clk.start_wall) // period), len(samples) - 1)
            attrs = dict(trt._published(samples[k])["sensor.switchboard_link_health"][1])
            attrs["measured_at"] = _iso(clk.start_wall + period * k)
            attrs["poll_interval_s"] = period
            return attrs
        _sets, dh_notes = _drive_loop(rollup_at, cycles=14, interval=120,
                                      env={"CORDLESS_EXT": "19"})
        gw = [m for t, m in dh_notes if t == "Switchboard: gxw gateway CRITICAL"]
        out = [m for t, m in rt_notes if t == "Switchboard: phones unreachable"]
        push = [m for t, m, _kw in pushes if t == "Switchboard: phones down"]
        host = "Switchboard host or the network"
        return (len(gw), len(out), len(push),
                bool(gw) and host in gw[0], bool(out) and host in out[0],
                bool(push) and host in push[0])
    cordless_only = trt._sum(trt._WIRED8)
    early = verdicts([trt._S_UP] * 3 + [cordless_only] + [trt._S_ALL_DOWN] * 4)
    check("one cause: precondition — one alert from each", early[:3] == (1, 1, 1))
    check("one cause: the cordless down a sample before the ports -> the gateway, in both",
          early[3:] == (False, False, False))
    same = verdicts([trt._S_UP] * 4 + [trt._S_ALL_DOWN] * 4)
    check("one cause: the cordless and the ports in one sample -> the host, in both",
          same == (1, 1, 1, True, True, True))
    gw, _r = _gateway_critical(lambda c: c >= 1, lambda c: c >= 2, cycles=6)
    check("one cause: devhealth alone, the cordless one sample early -> the gateway wording",
          len(gw) == 1 and "GXW gateway likely lost power" in gw[0])
    run, ports = {}, {}
    for r in (run, ports):
        dh.note_down_run(r, False, 1000.0)
    dh.note_down_run(run, True, 1300.0)
    dh.note_down_run(ports, True, 1600.0)
    check("runs: the cordless's run beginning a sample before the ports' is not together",
          not dh.dropped_together(run, ports, 300.0))
    check("runs: ...one beginning a sample after them is",
          dh.dropped_together(ports, {"since": 1900.0}, 300.0) is False
          and dh.dropped_together({"since": 1900.0}, ports, 300.0))
    check("runs: ...and one beginning on the ports' own sample is",
          dh.dropped_together({"since": 1600.0}, ports, 300.0))


def test_no_served_text_says_a_reboot_inside_one_sample_raises_nothing() -> None:
    """With an IP to probe, a WP826 reboot of three to four minutes still fails
    two LAN probes 120 s apart and raises 'unreachable'. Only the registration
    alert waits for two samples."""
    root = _SRC.parents[5]
    served = [root / "CHANGELOG.md", root.parent / "README.md",
              root / "docs" / "reference" / "wp826-cordless.md",
              root / "docs" / "reference" / "health-monitoring.md"]
    for path in served:
        text = " ".join(path.read_text(encoding="utf-8").split())
        check(f"reboot claim: {path.name} does not say a reboot inside one sample raises nothing",
              "inside one sample raises nothing" not in text
              and "inside one sample, or a rollup that cannot say, raises nothing" not in text)
    doc = " ".join((dh.classify_cordless.__doc__ or "").split())
    check("reboot claim: nor does classify_cordless's docstring",
          "inside one sample raises nothing" not in doc)


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
