"""Idle link-health poller — tracks each phone's qualify round-trip time and
reachability BETWEEN calls, so a degrading link (e.g. the WiFi cordless's Wi-Fi
getting congested) is visible on a Home Assistant trend graph without waiting for
someone to place a call.

Complements the per-call telemetry (switchboard-callqos), which can only measure a
link while a call is up. Here we poll Asterisk's PJSIP endpoints + qualify (the
OPTIONS keepalive it already sends every ~30-60 s) via AMI — the same read the
dashboard uses — and publish:

  * ``sensor.switchboard_link_<ext>`` — that phone's qualify RTT in ms (graphable),
    with status + name as attributes. ``offline`` when a configured phone is
    de-registered (e.g. the WiFi cordless asleep), ``unavailable`` when registered
    but its qualify is failing.
  * ``sensor.switchboard_link_health`` — a rollup: worst reachable RTT as state,
    the reachable / unreachable / offline split + per-phone detail as attributes.
  * ``/data/state/linkhealth.jsonl`` — a capped history for offline analysis.

The roster is the set of CONFIGURED endpoints (PJSIPShowEndpoints), not just live
contacts, so a phone that drops its registration shows as ``offline`` — an
alertable state — instead of silently vanishing.

Why this and not ``pjsip show channelstats`` for a call's far leg: on this system
that command returns "not valid"/empty rows for bridged calls, and the initiating
phone's per-call record already carries BOTH directions — so idle qualify RTT is
the genuinely-additive signal a poller can provide.

Pure helpers (rtt/status parsing, per-phone + rollup shaping) are import-safe and
unit-tested; only run()'s loop does AMI/HA I/O.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, "/usr/share/switchboard/webui")

# ContactList Status wire values that mean a qualified phone is answering its
# OPTIONS keepalive. Anything else (Unavail / Removed / Unknown) is not reachable.
# NonQual is deliberately excluded — a qualified phone never reports it; only the
# qualify-off SIP trunk does, and we filter the trunk out of phone health entirely.
_REACHABLE = {"avail", "reachable", "created", "updated"}

STATE_PATH = os.environ.get("SWITCHBOARD_LINKHEALTH", "/data/state/linkhealth.jsonl")
OPTIONS_PATH = os.environ.get("SWITCHBOARD_OPTIONS", "/data/options.json")
MAX_RECORDS = 2000

# Startup warm-up: right after an add-on restart the poller can run its first cycle
# while the phones are still re-registering with Asterisk — publishing a misleading
# "all offline" snapshot that would then sit there for a whole interval. So poll on a
# short cadence until a phone actually registers (or a bounded cap elapses), then
# settle to the steady interval.
WARMUP_DELAY = 15          # seconds between warm-up polls
WARMUP_MAX_POLLS = 8       # ~2 min cap, so a genuinely all-down fleet still settles


def rtt_ms(raw) -> float | None:
    """A ContactList RoundtripUsec (microseconds, as a string) -> milliseconds.
    '' / 'nan' / non-numeric / negative -> None (qualify not yet measured / off)."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("", "nan", "unavailable", "unknown"):
        return None
    try:
        us = float(s)
    except ValueError:
        return None
    if us != us or us < 0 or us in (float("inf"), float("-inf")):
        return None
    return round(us / 1000.0, 2)


def is_reachable(status: str) -> bool:
    return str(status or "").strip().lower() in _REACHABLE


def room_names(opts: dict) -> dict:
    """ext -> friendly name, from the add-on options (best-effort labels)."""
    out = {}
    for r in (opts.get("rooms") or []):
        ext = str(r.get("ext", "")).strip()
        if ext:
            out[ext] = str(r.get("name", "") or ext).strip()
    return out


_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def ip_from_uri(uri) -> str | None:
    """The IPv4 host of a PJSIP contact URI, e.g. 'sip:19@192.168.1.84:18357' ->
    '192.168.1.84'. Published so the device-health monitor can auto-follow a
    DHCP-moved phone to its current IP. IPv4 only (the HTTP probe path is IPv4); a
    non-IPv4 / hostname contact returns None and the caller falls back to the
    configured static IP."""
    m = _IPV4.search(str(uri or ""))
    return m.group(1) if m else None


def build_phone_health(endpoints: list, contacts: dict, names: dict) -> list:
    """One row per CONFIGURED phone: {ext, name, status, rtt_ms, reachable, registered}.

    The roster is the set of configured PJSIP endpoints (from PJSIPShowEndpoints),
    NOT just the live contacts — so a phone that has DE-REGISTERED (e.g. the WiFi
    cordless dropping off Wi-Fi when idle) shows as ``offline`` instead of silently
    vanishing. Registration + RTT come from the contact (absent contact == not
    registered). The SIP trunk (a static, qualify-off "trunk" endpoint) is excluded
    by the digit-only filter — it isn't a phone link and its hyphenated AOR isn't a
    valid HA entity id."""
    rows = []
    seen = set()
    for ep in (endpoints or []):
        ext = str(ep.get("name", "")).strip()
        if not ext.isdigit() or ext in seen:  # digit-only == real phone; dedupe
            continue
        seen.add(ext)
        c = (contacts or {}).get(ext)
        registered = bool(c)
        if registered:
            status = c.get("status", "Unknown")
            rtt = rtt_ms(c.get("rtt"))
            reachable = is_reachable(status)
            contact_ip = ip_from_uri(c.get("uri"))
        else:
            status = "Unregistered"  # configured but no contact -> offline
            rtt = None
            reachable = False
            contact_ip = None
        rows.append({
            "ext": ext,
            "name": names.get(ext, ext),
            "status": status,
            "rtt_ms": rtt,
            "reachable": reachable,
            "registered": registered,
            "contact_ip": contact_ip,  # current registered IP (auto-follows DHCP)
        })
    return sorted(rows, key=lambda r: r["ext"])


def _median(vals: list):
    """Median of a list of numbers, or None. Median, not mean: one handset waking
    from Wi-Fi power-save produces a single huge sample that would drag a mean."""
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else round((xs[mid - 1] + xs[mid]) / 2.0, 2)


def _sample_is_partial(reachable: list, phones: list,
                       measured_before: set | None) -> bool:
    """True when a phone we HAVE measured is missing from the RTT sample.

    The distinction that matters is "dropped" vs "never there". v0.52.0 used
    `unreachable > 0`, which conflated them: ext 20 is a configured softphone
    that never registers BY DESIGN, so it is permanently unreachable and the
    flag was True in 100% of samples — informationally dead, and it pinned the
    rollup's icon to "disconnected" on a perfectly healthy fleet.

    `measured_before` is the set of extensions this process has actually seen
    answer a qualify. A phone that has never answered cannot have "dropped out"
    of the sample, so it can never raise the flag; the cordless, which does
    answer, still raises it the moment it disappears — which is the whole point,
    since losing the slowest phone makes worst_rtt_ms IMPROVE.

    Falling back (measured_before=None) to "any REGISTERED phone missing" keeps
    a sensible answer for a caller with no history to offer."""
    contributing = {p["ext"] for p in reachable if p["rtt_ms"] is not None}
    # An EMPTY set falls back too, not just None. If the caller's wiring ever
    # stops feeding this (the loop that maintains it is not unit-testable), an
    # empty expectation would make the flag silently False FOREVER — the same
    # inert-but-green failure this fix exists to remove. The heuristic is
    # strictly weaker, not equivalent: it catches a REGISTERED phone that
    # stopped answering, but a phone that de-registered entirely looks exactly
    # like one that never registered, so only the measured-before set separates
    # the cordless dropping off from the softphone that was never there.
    expected = set(measured_before) if measured_before else {
        p["ext"] for p in phones if p.get("registered")}
    return bool(expected - contributing)


def summarize(phones: list, wired_exts: list | None = None,
              measured_before: set | None = None) -> dict:
    """Rollup for the summary sensor: reachable/unreachable/offline split + worst RTT.
    'offline' (configured but de-registered) is called out separately from merely
    'unreachable' (registered but its qualify is failing) — a dropped cordless is
    the actionable case.

    `worst_rtt_ms` (the sensor's state) is a WORST-CASE across the whole fleet, so
    the slowest handset dominates it — in practice the Wi-Fi cordless, whose idle
    RTT is both higher and far more variable than the wired ports'. When this
    split was introduced the cordless idled around 250 ms under Wi-Fi power save
    (its CALLS ran 7-18 ms); since it started living on its charger it idles
    around 9 ms with an occasional spike, so the gap is smaller than it was —
    but the SHAPE is unchanged and so is the argument: one handset's variance
    still sets the number, and it MASKS the wired fleet, which could degrade
    from 2 ms to 40 ms without moving a state pinned by the cordless.

    NOTE `worst_rtt_ms` is a max over REACHABLE phones only, so it is not
    monotonic in fleet health — see `worst_rtt_is_partial` below.

    So the wired ports (`wired_exts`, i.e. the gateway_ports option) are also
    summarised on their own: median + max + count. The median is the number worth
    graphing."""
    reachable = [p for p in phones if p["reachable"]]
    unreachable = [p for p in phones if not p["reachable"]]
    offline = [p for p in phones if not p["registered"]]
    rtts = [(p["rtt_ms"], p) for p in reachable if p["rtt_ms"] is not None]
    worst = max(rtts, key=lambda t: t[0]) if rtts else None

    wired_set = {str(e).strip() for e in (wired_exts or []) if str(e).strip()}
    wired_rtts = [p["rtt_ms"] for p in reachable
                  if p["rtt_ms"] is not None and str(p["ext"]) in wired_set]
    other_rtts = [(str(p["ext"]), p["rtt_ms"]) for p in reachable
                  if p["rtt_ms"] is not None and str(p["ext"]) not in wired_set]
    return {
        "wired_median_rtt_ms": _median(wired_rtts),
        "wired_max_rtt_ms": max(wired_rtts) if wired_rtts else None,
        "wired_count": len(wired_rtts),
        # Everything not on the gateway — in practice the Wi-Fi cordless.
        "other_rtt_ms": {e: v for e, v in other_rtts} or None,
        "total": len(phones),
        "reachable": len(reachable),
        "unreachable": len(unreachable),
        "unreachable_exts": [p["ext"] for p in unreachable],
        "offline": len(offline),
        "offline_exts": [p["ext"] for p in offline],
        "worst_rtt_ms": worst[0] if worst else None,
        "worst_ext": worst[1]["ext"] if worst else None,
        # worst_rtt_ms is a max over REACHABLE phones only, so it is NOT monotonic
        # in fleet health: when the slowest phone (in practice the Wi-Fi cordless)
        # drops off entirely, it leaves the sample and the number IMPROVES even
        # though the fleet just got worse — the sensor's all-time minimum can be
        # its worst moment. The state is left as-is so its recorder history keeps
        # one meaning, and this flag is published so an automation can refuse to
        # threshold on a partial sample. For latency use wired_link_health; for
        # availability use unreachable_exts.
        "worst_rtt_is_partial": _sample_is_partial(reachable, phones, measured_before),
    }


# --------------------------------------------------------------------------- #
# Fleet-outage availability alert.
#
# The link-health poller RECORDED an ~11h outage where all 8 wired GXW FXS ports
# (a single gateway) lost registration together — but nothing ALERTED, because the
# WiFi cordless and the inbound DID (which routes to the cordless) kept working, so
# it went unnoticed. This fires ONE persistent notification when a large fraction of
# the fleet is unreachable at once (a shared gateway dropping, not one handset
# asleep), and a recovery notice when it clears. A consecutive-cycle gate rejects
# the single-sample "all Unregistered" collector blips that AMI occasionally emits.
# --------------------------------------------------------------------------- #
OUTAGE_MIN_PORTS = 3       # at least this many phones down ...
OUTAGE_MIN_CYCLES = 2      # ... for this many consecutive cycles before alerting


def is_mass_outage(summ: dict) -> bool:
    """A fleet-level outage: at least half the phones (and >= OUTAGE_MIN_PORTS)
    unreachable at once — i.e. a shared gateway dropped, not one handset asleep or a
    lone unconfigured port. One cordless napping (+ an empty port) never qualifies."""
    total = summ.get("total", 0) if summ else 0
    down = summ.get("unreachable", 0) if summ else 0
    return total > 0 and down >= max(OUTAGE_MIN_PORTS, (total + 1) // 2)


def outage_transition(summ: dict, st: dict, settled: bool = True) -> str:
    """Pure state machine for the availability alert. `st` carries
    {'cycles', 'alerted'} across calls. Returns 'down' (fire the outage alert, once,
    after OUTAGE_MIN_CYCLES consecutive mass-outage cycles), 'up' (fire recovery,
    once, when it clears after having alerted), or '' (nothing to do).

    `settled=False` (startup warm-up) must NOT count outage cycles: warm-up polls
    run every WARMUP_DELAY=15 s, so the "consecutive cycles" gate — sized for the
    300 s steady cadence — would otherwise be satisfiable ~30 s after any restart,
    turning a GXW that takes a minute to re-register into a false fleet-outage
    alarm. Recovery ('up') still fires during warm-up: a real pre-restart outage
    that heals while warming up must clear its notification promptly."""
    if is_mass_outage(summ):
        if not settled:
            return ""
        st["cycles"] = st.get("cycles", 0) + 1
        if st["cycles"] >= OUTAGE_MIN_CYCLES and not st.get("alerted"):
            st["alerted"] = True
            return "down"
        return ""
    st["cycles"] = 0
    if st.get("alerted"):
        st["alerted"] = False
        return "up"
    return ""


def _notify_outage(event: str, summ: dict) -> None:
    try:
        import ha_client
    except Exception:
        return
    # Same notification_id both ways so the recovery REPLACES the outage in the bell
    # menu (no stale "unreachable" left behind).
    nid = "switchboard_link_outage"
    if event == "down":
        exts = ", ".join(summ.get("unreachable_exts", []))
        msg = (f"{summ['unreachable']} of {summ['total']} phones are unreachable "
               f"(exts {exts}). If these are the wired gateway ports, the GXW gateway "
               f"likely dropped its SIP registrations — check the gateway's power/uplink.")
        try:
            ha_client.notify(msg, title="Switchboard: phones unreachable", notification_id=nid)
        except Exception:
            pass
    elif event == "up":
        try:
            ha_client.notify(f"Phones recovered — {summ['reachable']} of {summ['total']} reachable.",
                             title="Switchboard: phones recovered", notification_id=nid)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Trunk registration watchdog.
#
# Found live 2026-08-09: a 75-minute WAN outage exhausted Asterisk's default
# max_retries and the trunk registration entered its TERMINAL "Rejected" state —
# inbound calling was dead for 24 hours and NOTHING alerted, because this poller
# deliberately tracks only the digit-named phone endpoints and no other component
# watches registrations at all. Two defenses now exist: switchboard-config sets
# max_retries=10000 (so the terminal state ~never arises), and this watchdog
# publishes the status as a sensor, auto-kicks a Rejected registration
# (`pjsip send register`), and notifies if it stays down.
# --------------------------------------------------------------------------- #
TRUNK_REG_NAME = "trunk-reg"
TRUNK_MIN_CYCLES = 2       # consecutive bad settled cycles before notifying


def _positive_int(v):
    """A positive int, or None. AMI reports NextReg as "0" when no refresh is
    scheduled/counting; publishing that 0 reads like "due now" on a dashboard."""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def trunk_enabled(opts: dict) -> bool:
    """True only when there IS an outbound registration to watch.

    `registns: false` is a documented, supported setup (a trunk authenticated
    per-INVITE with no REGISTER). switchboard-config emits no [trunk-reg]
    section for it, so AMI reports no registration object — which this watchdog
    read as "the outside line is down", publishing `unknown` forever and firing
    a persistent, never-clearing "outside line down" notification on a perfectly
    healthy trunk. Gate on registns too: nothing to watch, nothing to alarm."""
    t = opts.get("trunk")
    if not isinstance(t, dict) or not t.get("enabled"):
        return False
    return bool(t.get("registns", True))


def trunk_transition(status: str, st: dict, settled: bool = True) -> str:
    """Pure state machine for the trunk-registration alert. `status` is the
    OutboundRegistrationDetail status (Registered / Unregistered / Rejected /
    Stopped) or '' when AMI had no registration to report. Only 'Registered'
    counts as healthy; '' is treated as bad (a trunk configured to register that
    reports NO registration object is exactly the silent-death shape). Mirrors
    outage_transition's discipline: warm-up cycles never count toward the alert,
    recovery always clears promptly."""
    if (status or "").strip().lower() == "registered":
        st["cycles"] = 0
        if st.get("alerted"):
            st["alerted"] = False
            return "up"
        return ""
    if not settled:
        return ""
    st["cycles"] = st.get("cycles", 0) + 1
    if st["cycles"] >= TRUNK_MIN_CYCLES and not st.get("alerted"):
        st["alerted"] = True
        return "down"
    return ""


def _trunk_check(st: dict, settled: bool, alerts_on: bool) -> str | None:
    """One watchdog cycle: read status, auto-kick if Rejected, publish, notify.
    Every step best-effort — the link-health loop must never die on trunk work.

    Returns the registration status string (or None when AMI could not answer),
    so the caller can put the outside line's state into the heartbeat record —
    the trunk previously had no timestamped health history on any surface."""
    try:
        import ami
        regs = ami.get_registrations_or_none()
    except Exception:
        return None  # AMI down this cycle: skip entirely (don't blank the sensor)
    if regs is None:
        # AMI unreachable — NOT the same as "the trunk has no registration".
        # Publishing here would blank the sensor to "unknown" (observed on 6/6
        # restarts before this distinction existed) and, once settled, would
        # count toward the down-alert and fire a false "outside line down".
        return None
    reg = regs.get(TRUNK_REG_NAME) or {}
    status = (reg.get("status") or "").strip()
    kicked = False
    if status.lower() in ("rejected", "stopped"):
        # Terminal states: Asterisk will never retry on its own. Kick first,
        # then let next cycle's status read tell us whether it worked.
        try:
            import ami
            kicked = ami.send_register(TRUNK_REG_NAME)
        except Exception:
            kicked = False
        st["kicks"] = st.get("kicks", 0) + (1 if kicked else 0)
    try:
        import ha_client
        ha_client.set_state(
            "sensor.switchboard_trunk_health",
            status if status else "unknown",
            {"friendly_name": "Switchboard trunk registration",
             "icon": "mdi:phone-voip" if status.lower() == "registered"
                     else "mdi:phone-alert",
             # AMI reports NextReg as seconds-until-refresh, and it reads "0"
             # whenever the registration is not currently counting down — which
             # is most of the time we look at it. Publish it only when it is a
             # real countdown, so the attribute never implies "refresh overdue".
             "next_reg_s": _positive_int(reg.get("next_reg")),
             "auto_reregister_attempts": st.get("kicks", 0),
             "last_kick_sent": kicked or None,
             # Freshness stamp. A pushed HA sensor never expires, so if the
             # WHOLE add-on dies this sensor keeps saying "Registered" forever —
             # which is exactly what happened through the 60-minute outage of
             # 2026-08-25: the PBX did not exist and trunk_health read
             # "Registered" for the entire hour, with no 'unavailable' and no
             # gap marker. rollup_is_stale() in devhealth covers one poller
             # dying while another survives to notice; nothing INSIDE the add-on
             # can cover the add-on itself being gone. Only a consumer comparing
             # this stamp against now can, so publish it on every sensor a human
             # or automation actually trusts.
             "measured_at": _now_iso(),
             "poll_interval_s": _poll_interval()})
    except Exception:
        pass
    event = trunk_transition(status, st, settled)
    if not (event and alerts_on):
        return status or "unknown"
    try:
        import ha_client
        nid = "switchboard_trunk_registration"
        if event == "down":
            ha_client.notify(
                f"The outside line's SIP registration is {status or 'missing'} — "
                "inbound calls will fail until it recovers. An automatic "
                "re-register has been sent; if this alert does not clear within "
                "a few minutes, check the WAN link and the VoIP.ms portal.",
                title="Switchboard: outside line down", notification_id=nid)
        elif event == "up":
            ha_client.notify("The outside line re-registered — inbound calling is back.",
                             title="Switchboard: outside line recovered",
                             notification_id=nid)
    except Exception:
        pass
    return status or "unknown"


def _load_options() -> dict:
    # Post-overlay snapshot first, then the raw options as a fallback so a
    # missing snapshot degrades to the saved config rather than to {}.
    for path in (OPTIONS_PATH, "/data/options.json"):
        try:
            with open(path) as f:
                d = json.load(f)
            if isinstance(d, dict) and d:
                return d
        except Exception:
            continue
    return {}


def _append_history(phones: list) -> None:
    rec = {"ts": int(time.time()),
           "phones": {p["ext"]: {"rtt_ms": p["rtt_ms"], "reachable": p["reachable"],
                                 "registered": p["registered"], "status": p["status"]}
                      for p in phones}}
    # Rewrite ATOMICALLY (temp file + os.replace), the same idiom
    # switchboard-callqos.append_record() uses on its ledger. This function used
    # to truncate STATE_PATH in place and rewrite ~1.6 MB every poll: a crash,
    # a full disk, or a container stop landing inside that window would leave a
    # half-written file, and a read error midway silently dropped every record
    # it had not reached yet. os.replace() is atomic on the same filesystem, so
    # a reader sees either the old file or the new one, never a partial one.
    try:
        d = os.path.dirname(STATE_PATH) or "."
        os.makedirs(d, exist_ok=True)
        lines = []
        try:
            with open(STATE_PATH) as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            lines = []
        lines.append(json.dumps(rec, separators=(",", ":")))
        lines = lines[-MAX_RECORDS:]
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".linkhealth-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, STATE_PATH)
            try:
                # mkstemp creates 0600 owned by THIS process (root), and
                # os.replace keeps the TEMP file's mode/owner — not the
                # destination's. v0.52.0 copied callqos's atomic idiom but
                # dropped this chmod, silently turning an asterisk-readable
                # ledger into 0600 root-only. /data/state is the asterisk-owned
                # directory (setgid) precisely so non-root components can read.
                # 0640, NOT the 0664 the sibling ledger uses — the requirements
                # genuinely differ. callqos.jsonl is WRITTEN by asterisk-user
                # AGIs, so it needs group write; THIS ledger has exactly one
                # writer (rtpmon, as root) and only ever needs to be READ by
                # group asterisk. So root writes, group reads, world gets
                # nothing. CodeQL flagged both the world bit and the group-write
                # bit (py/overly-permissive-file, sev 7.8) and was right twice.
                os.chmod(STATE_PATH, 0o640)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception:
        pass


def _poll_interval() -> int:
    try:
        return max(30, int(os.environ.get("LINK_HEALTH_INTERVAL", "300") or "300"))
    except ValueError:
        return 300


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


HEARTBEAT_PATH = os.environ.get("SWITCHBOARD_HEARTBEAT",
                                "/share/switchboard/heartbeat.jsonl")
HEARTBEAT_MAX_BYTES = 4 * 1024 * 1024


def _heartbeat(summ: dict | None, trunk_status: str | None,
               wired_down: list | None = None) -> None:
    """Append one liveness record to /share, readable from OUTSIDE the container.

    Two problems, one file.

    (1) The forensic log cannot tell idle from dead. It went 19 h 42 m with no
    entry on 2026-08-30/31 while the PBX was perfectly healthy -- steady-state
    contact refreshes do not log, only initial registration does, so a quiet
    system and a wedged Asterisk produce byte-identical output: none. Reading
    that log alone, those two cases are indistinguishable.

    (2) The outside line had no timestamped health history on ANY surface. Its
    state lived only in a pushed HA sensor, which has no history a log reader can
    consult and which freezes at its last value if the add-on dies. The trunk is
    the outside line and the open E911 path.

    A dated record every poll cycle fixes both: silence in THIS file means the
    poller stopped, and the trunk's state is on the record with a timestamp. It
    lives beside the forensic log precisely because /data cannot be read.

    Best-effort throughout -- a liveness record must never break the poll that
    produces it."""
    rec = {
        "ts": _now_iso(),
        "poller": "rtpmon",
        "interval_s": _poll_interval(),
        "trunk": trunk_status or "unknown",
    }
    if summ:
        rec["reachable"] = summ.get("reachable")
        rec["expected"] = summ.get("expected")
        rec["worst_rtt_ms"] = summ.get("worst_rtt")
    else:
        # AMI unreachable this cycle: say so rather than omitting the fields,
        # so a reader can tell "no data" from "zero phones up".
        rec["reachable"] = None
        rec["ami"] = "unreachable"
    if wired_down:
        rec["wired_down"] = list(wired_down)
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
        # Cap: this file is append-only and unbounded otherwise. Truncating keeps
        # the newest records, which are the ones a reader wants.
        try:
            if os.path.getsize(HEARTBEAT_PATH) > HEARTBEAT_MAX_BYTES:
                with open(HEARTBEAT_PATH, "w", encoding="utf-8"):
                    pass
        except OSError:
            pass
        with open(HEARTBEAT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError as exc:
        sys.stderr.write(f"[switchboard-rtpmon] heartbeat: {exc}\n")


def _publish(phones: list, summ: dict) -> None:
    try:
        import ha_client
    except Exception:
        return
    for p in phones:
        eid = f"sensor.switchboard_link_{p['ext']}"
        if not p["registered"]:
            state = "offline"           # configured but de-registered (e.g. cordless asleep)
        elif p["reachable"] and p["rtt_ms"] is not None:
            state = p["rtt_ms"]         # graphable RTT
        else:
            state = "unavailable"       # registered but qualify failing
        numeric = not isinstance(state, str)
        attrs = {
            "friendly_name": f"Switchboard link — {p['name']} ({p['ext']})",
            "unit_of_measurement": "ms" if numeric else None,
            "icon": "mdi:phone-in-talk" if p["reachable"] else "mdi:phone-off",
            "status": p["status"], "extension": p["ext"], "name": p["name"],
            "reachable": p["reachable"], "registered": p["registered"],
            "contact_ip": p.get("contact_ip"),  # for devhealth DHCP auto-follow
        }
        try:
            ha_client.set_state(eid, state, {k: v for k, v in attrs.items() if v is not None})
        except Exception:
            pass
    try:
        ha_client.set_state(
            "sensor.switchboard_link_health",
            summ["worst_rtt_ms"] if summ["worst_rtt_ms"] is not None else "unknown",
            {"friendly_name": "Switchboard link health",
             "unit_of_measurement": "ms",
             # Same conflation as the flag: keyed on `unreachable` this showed
             # "disconnected" forever, because ext 20 never registers by design.
             "icon": "mdi:lan-disconnect" if summ["worst_rtt_is_partial"] else "mdi:lan-connect",
             "reachable": summ["reachable"], "unreachable": summ["unreachable"],
             "unreachable_exts": summ["unreachable_exts"],
             "offline": summ["offline"], "offline_exts": summ["offline_exts"],
             "worst_ext": summ["worst_ext"], "total_phones": summ["total"],
             # True when any phone is missing from the sample the state was
             # computed over — see summarize(). Without this, a consumer cannot
             # tell an improving fleet from a shrinking one.
             "worst_rtt_is_partial": summ.get("worst_rtt_is_partial", False),
             # The wired fleet, apart from the cordless — see the sensor below.
             "wired_median_rtt_ms": summ.get("wired_median_rtt_ms"),
             "wired_max_rtt_ms": summ.get("wired_max_rtt_ms"),
             "wired_count": summ.get("wired_count"),
             "other_rtt_ms": summ.get("other_rtt_ms"),
             # A pushed sensor has NO expiry: if this poller dies while Home
             # Assistant stays up, every value below freezes at its last good
             # reading and the fleet reads healthy forever. Stamp each publish
             # so a consumer can tell "measured just now" from "frozen since".
             # devhealth uses this to refuse to derive gateway health from a
             # stale rollup; a template alert can use it the same way.
             "measured_at": _now_iso(),
             "poll_interval_s": _poll_interval()})
    except Exception:
        pass
    # Dedicated wired sensor. The rollup above is a fleet WORST CASE, which the
    # Wi-Fi cordless pins with its far larger latency variance — so a wired
    # gateway degrading from 2 ms to 40 ms would never move it. This is the
    # number to graph and alert on for the 8 analog ports.
    try:
        wm = summ.get("wired_median_rtt_ms")
        ha_client.set_state(
            "sensor.switchboard_wired_link_health",
            wm if wm is not None else "unknown",
            {"friendly_name": "Switchboard wired link health",
             "unit_of_measurement": "ms",
             "icon": "mdi:lan-connect",
             "state_class": "measurement",
             "median_rtt_ms": wm,
             "max_rtt_ms": summ.get("wired_max_rtt_ms"),
             "ports_measured": summ.get("wired_count"),
             # DOCS designates THIS sensor as the one to graph and alert on, so
             # it needs the same self-describing freshness the rollup got in
             # v0.51.0 — a pushed sensor never expires, and an alert built on a
             # frozen value is worse than no alert.
             "measured_at": _now_iso(),
             "poll_interval_s": _poll_interval(),
             "excludes": "Wi-Fi cordless and any non-gateway extension"})
    except Exception:
        pass


def wired_exts(opts: dict) -> list:
    """The gateway's FXS-port extensions, from the `gateway_ports` option — the
    same list devhealth derives gateway health from. These are the WIRED phones,
    summarised apart from the Wi-Fi cordless (see summarize)."""
    raw = str(opts.get("gateway_ports", "") or "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def poll_once(names: dict, wired: list | None = None,
              measured_before: set | None = None) -> tuple:
    """One measurement cycle. Returns (phones, summary) or (None, None) if AMI is
    down this cycle (caller just skips — no publish, no crash)."""
    import ami
    try:
        endpoints, contacts, _channels = ami.get_status_bundle()
    except Exception:
        return None, None
    if not endpoints:
        return None, None  # AMI up but no roster -> skip (don't blank the sensors)
    phones = build_phone_health(endpoints, contacts, names)
    return phones, summarize(phones, wired, measured_before)


def wired_down_count(summ: dict, wired: list | None) -> int:
    """How many WIRED gateway ports are not currently reachable. The cordless and
    the unused softphone are deliberately excluded: the cordless is often asleep
    and ext 20 never registers, so counting them would hold warm-up open forever."""
    if not summ or not wired:
        return 0
    down = {str(e) for e in (summ.get("unreachable_exts") or [])}
    down |= {str(e) for e in (summ.get("offline_exts") or [])}
    return len({str(e) for e in wired} & down)


def warmup_done(settled: bool, prev_reachable: int, reachable: int, polls: int,
                wired_down: int | None = None) -> bool:
    """True once the poller should switch from the startup fast cadence to the steady
    interval. Settling on the FIRST reachable phone would freeze stragglers still
    re-registering after a restart as 'offline' for a whole interval — e.g. one GXW
    FXS port lagging its siblings. Latches once true.

    `wired_down` (count of gateway ports still not reachable) is the PRECISE gate:
    stay in warm-up until every wired port is back. The old heuristic — settle as
    soon as the reachable COUNT stops growing — mistook a plateau for stability:
    on 2026-08-11 the count sat flat at 6 across two 15 s polls while exts 15/17/18
    were still re-registering, so the poller settled early and then froze that
    stale down-list for a full 300 s interval. devhealth reads this rollup, so the
    GXW falsely read "degraded" for ~4 minutes, twice. WARMUP_MAX_POLLS still caps
    the wait, so a genuinely dead port cannot hold warm-up open forever.

    `wired_down=None` keeps the old count-plateau heuristic, for a caller that has
    no gateway_ports list to check against."""
    if settled or polls >= WARMUP_MAX_POLLS:
        return True
    if wired_down is not None:
        return wired_down == 0
    return reachable > 0 and reachable <= prev_reachable


def run() -> int:
    try:
        interval = int(os.environ.get("LINK_HEALTH_INTERVAL", "300") or "300")
    except ValueError:
        interval = 300
    interval = max(30, interval)  # floor: never hammer AMI
    sys.stderr.write(f"switchboard-rtpmon: idle link-health poller up (every {interval}s)\n")
    settled = False
    polls = 0
    prev_reachable = -1
    outage_st = {"cycles": 0, "alerted": False}
    trunk_st = {"cycles": 0, "alerted": False}
    # Extensions this process has actually seen answer a qualify. A phone that
    # has never answered cannot have "dropped out" of the RTT sample, so it must
    # not raise worst_rtt_is_partial — see _sample_is_partial. Grows only; a
    # restart deliberately starts empty rather than trusting stale history.
    measured_before: set = set()
    while True:
        opts = _load_options()
        names = room_names(opts)
        wired = wired_exts(opts)
        phones, summ = poll_once(names, wired, measured_before)
        if phones:
            measured_before |= {p["ext"] for p in phones
                                if p["reachable"] and p["rtt_ms"] is not None}
        reachable = summ.get("reachable", 0) if summ else 0
        if phones is not None:
            _append_history(phones)
            _publish(phones, summ)
            # Fleet-outage alert. Advance the state machine every cycle (so the
            # consecutive-cycle gate and one-shot latch stay correct even when
            # alerts are muted); only the notification itself honors the opt-out.
            event = outage_transition(summ, outage_st, settled)
            if event and opts.get("link_health_alerts", True):
                _notify_outage(event, summ)
        trunk_status = None
        if trunk_enabled(opts):
            trunk_status = _trunk_check(trunk_st, settled,
                                        opts.get("link_health_alerts", True))
        # Liveness record, EVERY cycle -- including cycles where AMI was down and
        # nothing else was published. Silence in the heartbeat file is the only
        # signal that separates "the PBX is idle" from "the poller stopped": the
        # forensic log went 19 h 42 m with no entry while perfectly healthy,
        # because steady-state contact refreshes do not log.
        _heartbeat(summ, trunk_status,
                   wired_down=[p["ext"] for p in (phones or [])
                               if p.get("ext") in set(wired) and not p.get("reachable")])
        polls += 1
        # A cycle where AMI was down (summ is None) tells us nothing about the
        # fleet — don't let it end warm-up on a phantom "all wired ports up".
        settled = warmup_done(settled, prev_reachable, reachable, polls,
                              wired_down_count(summ, wired) if summ else None)
        prev_reachable = reachable
        time.sleep(interval if settled else WARMUP_DELAY)


if __name__ == "__main__":
    sys.exit(run())
