"""Idle link-health poller — tracks each phone's qualify round-trip time and
reachability BETWEEN calls, so a degrading link (e.g. the WiFi cordless's Wi-Fi
getting congested) is visible on a Home Assistant trend graph without waiting for
someone to place a call.

Complements the per-call telemetry (switchboard-callqos), which can only measure a
link while a call is up. Here we poll Asterisk's own PJSIP qualify (an OPTIONS
keepalive it already sends every ~30-60 s) via AMI ``PJSIPShowContacts`` — the same
read the dashboard uses — and publish:

  * ``sensor.switchboard_link_<ext>`` — that phone's qualify RTT in ms (graphable),
    with status + name as attributes. "unavailable" state when the phone is offline.
  * ``sensor.switchboard_link_health`` — a rollup: worst reachable RTT as state,
    the reachable/unreachable split + per-phone detail as attributes.
  * ``/data/state/linkhealth.jsonl`` — a capped history for offline analysis.

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


def build_phone_health(contacts: dict, names: dict) -> list:
    """One row per contacted endpoint: {ext, name, status, rtt_ms, reachable}.

    Keyed by the AOR/endpoint id (== the room ext). Contacts are the source of
    truth for "which phones exist right now" — a phone with no contact isn't
    registered, so it simply doesn't appear (distinct from reachable-but-slow)."""
    rows = []
    for ext, c in sorted((contacts or {}).items()):
        # Only real phone extensions (digit AORs, per valid_rooms' ^[0-9]{2,6}$).
        # The SIP trunk registers as a static, qualify-off AOR ("trunk-aor") that is
        # NonQual by design — it is NOT a phone link to health-check. Including it
        # would pin the rollup to "unreachable" forever and mint an invalid HA entity
        # id (the hyphen). Skipping it keeps the rollup honestly "all healthy" and
        # keeps the trunk out of the per-phone sensors + history.
        if not str(ext).isdigit():
            continue
        st = c.get("status", "Unknown")
        rows.append({
            "ext": ext,
            "name": names.get(ext, ext),
            "status": st,
            "rtt_ms": rtt_ms(c.get("rtt")),
            "reachable": is_reachable(st),
        })
    return rows


def summarize(phones: list) -> dict:
    """Rollup for the summary sensor: reachable/unreachable split + worst RTT."""
    reachable = [p for p in phones if p["reachable"]]
    unreachable = [p for p in phones if not p["reachable"]]
    rtts = [(p["rtt_ms"], p) for p in reachable if p["rtt_ms"] is not None]
    worst = max(rtts, key=lambda t: t[0]) if rtts else None
    return {
        "total": len(phones),
        "reachable": len(reachable),
        "unreachable": len(unreachable),
        "unreachable_exts": [p["ext"] for p in unreachable],
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
                                 "status": p["status"]} for p in phones}}
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
        state = p["rtt_ms"] if (p["reachable"] and p["rtt_ms"] is not None) else "unavailable"
        attrs = {
            "friendly_name": f"Switchboard link — {p['name']} ({p['ext']})",
            "unit_of_measurement": "ms" if state != "unavailable" else None,
            "icon": "mdi:phone-in-talk" if p["reachable"] else "mdi:phone-off",
            "status": p["status"], "extension": p["ext"], "name": p["name"],
            "reachable": p["reachable"],
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
             "worst_ext": summ["worst_ext"], "total_phones": summ["total"]})
    except Exception:
        pass


def poll_once(names: dict) -> tuple:
    """One measurement cycle. Returns (phones, summary) or (None, None) if AMI is
    down this cycle (caller just skips — no publish, no crash)."""
    import ami
    try:
        contacts = ami.get_contacts()
    except Exception:
        return None, None
    if not contacts:
        return None, None
    phones = build_phone_health(contacts, names)
    return phones, summarize(phones)


def run() -> int:
    try:
        interval = int(os.environ.get("LINK_HEALTH_INTERVAL", "300") or "300")
    except ValueError:
        interval = 300
    interval = max(30, interval)  # floor: never hammer AMI
    sys.stderr.write(f"switchboard-rtpmon: idle link-health poller up (every {interval}s)\n")
    got_first = False
    while True:
        names = room_names(_load_options())
        phones, summ = poll_once(names)
        if phones is not None:
            _append_history(phones)
            _publish(phones, summ)
            got_first = True
        # Poll promptly at startup; until the first successful read (Asterisk/AMI
        # may still be coming up), retry on a short cadence rather than idling the
        # full interval and leaving the sensors blank for minutes.
        time.sleep(interval if got_first else 15)


if __name__ == "__main__":
    sys.exit(run())
