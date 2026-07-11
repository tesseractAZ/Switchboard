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

import json
import os
import sys
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
        else:
            status = "Unregistered"  # configured but no contact -> offline
            rtt = None
            reachable = False
        rows.append({
            "ext": ext,
            "name": names.get(ext, ext),
            "status": status,
            "rtt_ms": rtt,
            "reachable": reachable,
            "registered": registered,
        })
    return sorted(rows, key=lambda r: r["ext"])


def summarize(phones: list) -> dict:
    """Rollup for the summary sensor: reachable/unreachable/offline split + worst RTT.
    'offline' (configured but de-registered) is called out separately from merely
    'unreachable' (registered but its qualify is failing) — a dropped cordless is
    the actionable case."""
    reachable = [p for p in phones if p["reachable"]]
    unreachable = [p for p in phones if not p["reachable"]]
    offline = [p for p in phones if not p["registered"]]
    rtts = [(p["rtt_ms"], p) for p in reachable if p["rtt_ms"] is not None]
    worst = max(rtts, key=lambda t: t[0]) if rtts else None
    return {
        "total": len(phones),
        "reachable": len(reachable),
        "unreachable": len(unreachable),
        "unreachable_exts": [p["ext"] for p in unreachable],
        "offline": len(offline),
        "offline_exts": [p["ext"] for p in offline],
        "worst_rtt_ms": worst[0] if worst else None,
        "worst_ext": worst[1]["ext"] if worst else None,
    }


def _load_options() -> dict:
    try:
        with open(OPTIONS_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _append_history(phones: list) -> None:
    rec = {"ts": int(time.time()),
           "phones": {p["ext"]: {"rtt_ms": p["rtt_ms"], "reachable": p["reachable"],
                                 "registered": p["registered"], "status": p["status"]}
                      for p in phones}}
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
        with open(STATE_PATH, "w") as f:
            f.write("\n".join(lines[-MAX_RECORDS:]) + "\n")
    except Exception:
        pass


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
             "icon": "mdi:lan-connect" if not summ["unreachable"] else "mdi:lan-disconnect",
             "reachable": summ["reachable"], "unreachable": summ["unreachable"],
             "unreachable_exts": summ["unreachable_exts"],
             "offline": summ["offline"], "offline_exts": summ["offline_exts"],
             "worst_ext": summ["worst_ext"], "total_phones": summ["total"]})
    except Exception:
        pass


def poll_once(names: dict) -> tuple:
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
    return phones, summarize(phones)


def warmup_done(settled: bool, summ, polls: int) -> bool:
    """True once the poller should switch from the startup fast cadence to the
    steady interval: a phone has registered (reachable) or the warm-up cap elapsed.
    Latches — it never drops back into warm-up if a phone later de-registers."""
    if settled:
        return True
    return bool((summ and summ.get("reachable", 0) > 0) or polls >= WARMUP_MAX_POLLS)


def run() -> int:
    try:
        interval = int(os.environ.get("LINK_HEALTH_INTERVAL", "300") or "300")
    except ValueError:
        interval = 300
    interval = max(30, interval)  # floor: never hammer AMI
    sys.stderr.write(f"switchboard-rtpmon: idle link-health poller up (every {interval}s)\n")
    settled = False
    polls = 0
    while True:
        names = room_names(_load_options())
        phones, summ = poll_once(names)
        if phones is not None:
            _append_history(phones)
            _publish(phones, summ)
        polls += 1
        settled = warmup_done(settled, summ, polls)
        time.sleep(interval if settled else WARMUP_DELAY)


if __name__ == "__main__":
    sys.exit(run())
