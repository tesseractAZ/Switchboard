#!/usr/bin/env python3
"""Proactive device-health monitor for the phone fleet's two "smart" devices —
the WP826 cordless (the alarm/announce endpoint) and the GXW4216 FXS gateway.

WHY this exists (the gap it fills): switchboard-rtpmon already watches SIP
registration + qualify RTT for every extension and fires a FLEET-outage alert
when >= half the fleet drops. But two blind spots remain:

  1. The cordless is a battery + Wi-Fi device that is ALSO where power alarms are
     announced. Its battery dying, its Wi-Fi weakening, or its per-call audio
     quality degrading are invisible to Asterisk (the callee RTP leg is
     unmeasurable from the PBX) — yet they directly threaten alarm delivery.
     The WP826's own HTTP API reports all three (battery %, Wi-Fi RSSI, per-call
     MOS/jitter/loss). We poll it.
  2. A SINGLE critical device going offline (the cordless alone; the whole GXW)
     never trips the half-the-fleet fleet-outage gate. We add a per-device alert.

Design mirrors rtpmon (the foundation): pure classify/transition functions
(unit-tested), ha_client.set_state() pushed sensors for graphing, and a
consecutive-cycle one-shot notify() on unhealthy transitions (copied idiom from
rtpmon.outage_transition so an alert + its recovery collapse to one bell entry).
Gateway registration health is DERIVED from rtpmon's rollup sensor (the reliable,
already-gathered signal) rather than re-probed — the GXW blocks ICMP/HTTP off its
subnet, so an independent ping would false-alarm on a healthy gateway.

Env (bridged by the s6 run script from config.yaml):
  DEVICE_HEALTH_INTERVAL   poll seconds (default 120, floor 30)
  CORDLESS_IP              WP826 IP (default 192.168.6.71); '' disables cordless checks
  CORDLESS_PASSWORD        WP826 admin password; '' -> cordless API checks skipped
                           (reachability + registration still covered by rtpmon)
  GATEWAY_PORTS            comma ext range for the GXW ports (default '11,12,...,18')
  CORDLESS_BATTERY_CRIT    battery %% considered critical when discharging (default 15)
  CORDLESS_BATTERY_WARN    battery %% considered low when discharging (default 30)
  CORDLESS_WIFI_MIN        min acceptable Wi-Fi signal 0-5 (default 2)
  CORDLESS_MOS_MIN         min acceptable recent-call MOS (default 3.4)
  DEVICE_HEALTH_ALERTS     'false'/'0' -> publish sensors but never notify()
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import time

sys.path.insert(0, "/usr/share/switchboard/webui")  # ha_client lives with the webui

# --------------------------------------------------------------------------- #
# WP826 HTTP API client (pure-stdlib; mirrors tools/wp826.mjs).
# Auth: POST /cgi-bin/access {access:sha256(user)} -> salt; POST /cgi-bin/dologin
#   {username, password:sha256(pw+salt)} -> {sid}; sid + cookie gate the reads.
#   REQUIRES Referer/Origin headers or the phone 403s. TLS is the device's own
#   self-signed cert -> unverified context (LAN, no CA).
# --------------------------------------------------------------------------- #
_SHA = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731


def _ctx() -> ssl.SSLContext:
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


class _WP:
    def __init__(self, ip: str, password: str, user: str = "admin", timeout: float = 6.0):
        self.ip, self.pw, self.user, self.timeout = ip, password, user, timeout
        self.cookies: dict[str, str] = {}
        self.sid = None

    def _req(self, method: str, path: str, body: str | None = None):
        conn = http.client.HTTPSConnection(self.ip, 443, timeout=self.timeout, context=_ctx())
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://{self.ip}/",
            "Origin": f"https://{self.ip}",
        }
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            conn.request(method, "/cgi-bin" + path, body, headers)
            resp = conn.getresponse()
            for k, v in resp.getheaders():
                if k.lower() == "set-cookie":
                    kv = v.split(";", 1)[0]
                    if "=" in kv:
                        ck, cvv = kv.split("=", 1)
                        self.cookies[ck.strip()] = cvv.strip()
            data = resp.read().decode(errors="replace")
            return resp.status, data
        finally:
            conn.close()

    def login(self) -> bool:
        try:
            st, a = self._req("POST", "/access", f"access={_SHA(self.user)}")
            salt = (json.loads(a) or {}).get("body")
            if not salt:
                return False
            st, d = self._req("POST", "/dologin", f"username={self.user}&password={_SHA(self.pw + salt)}")
            dj = json.loads(d)
            if dj.get("response") != "success":
                return False
            self.sid = dj["body"]["sid"]
            return True
        except Exception:
            return False

    def get(self, path: str):
        sep = "&" if "?" in path else "?"
        try:
            st, d = self._req("GET", f"{path}{sep}sid={self.sid}")
            return json.loads(d) if st == 200 else None
        except Exception:
            return None


def probe_cordless(ip: str, password: str) -> dict:
    """Return a raw device-health snapshot for the WP826, best-effort. Keys:
    reachable(bool: TCP:443 open), api_ok(bool: logged in + read), and — when api_ok —
    battery_pct/charging/battery_health, wifi_connected/wifi_signal/wifi_ssid, last_mos."""
    out = {"reachable": _tcp_open(ip, 443), "api_ok": False}
    if not password:
        return out
    wp = _WP(ip, password)
    if not wp.login():
        return out
    out["api_ok"] = True
    bat = wp.get("/api-get_battery_status") or {}
    b = bat.get("battery") or {}
    if b:
        out["battery_pct"] = _int(b.get("capacity"))
        out["charging"] = str(b.get("status", "")).lower() == "charging"
        out["battery_health"] = b.get("health")
    wifi = (wp.get("/api-wifi_status_get") or {}).get("status") or {}
    if wifi:
        out["wifi_connected"] = bool(wifi.get("connected"))
        out["wifi_signal"] = _int(wifi.get("signal"))
        out["wifi_ssid"] = (wifi.get("connection") or {}).get("ssid")
    rtp = (wp.get("/api-get_rtp_status") or {}).get("rtpStatus") or {}
    mos, age = last_call_mos(rtp, now=time.time())
    if mos is not None:
        out["last_mos"] = mos           # the phone's own conversational MOS for its leg
        if age is not None:
            out["last_mos_age_s"] = age  # seconds since that call ended (for a recency gate)
    return out


def last_call_mos(rtp_status: dict, now: float | None = None):
    """(moscq, age_seconds) for the MOST RECENT call in the phone's retained RTP
    records — NOT the min across history (an old bad call must not pin the sensor
    'degraded' forever). Picked by the latest stopTimeSecond. age is seconds since
    that call ended (None if `now` not given / no timestamp). (None, None) if no
    record carries a MOS. moscq is the phone's own conversational MOS for its leg —
    the callee-side quality Asterisk cannot measure."""
    best = None  # (stop_ts, mos)
    for rec in (rtp_status or {}).values():
        try:
            m = float(rec.get("moscq"))
        except (TypeError, ValueError):
            continue
        try:
            ts = int(rec.get("stopTimeSecond"))
        except (TypeError, ValueError):
            ts = 0
        if best is None or ts > best[0]:
            best = (ts, m)
    if best is None:
        return None, None
    age = int(now - best[0]) if (now is not None and best[0]) else None
    return best[1], age


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _tcp_open(ip: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Pure classification (unit-tested — no I/O).
# --------------------------------------------------------------------------- #
def classify_cordless(snap: dict, th: dict) -> tuple[str, list[str]]:
    """(level, reasons) for the cordless. level ∈ {'ok','degraded','critical'}.
    CRITICAL = the alarm endpoint is offline, or its battery is about to die
    (discharging under the critical %%). DEGRADED = a quality/robustness risk that
    doesn't yet threaten delivery (weak/lost Wi-Fi, low-but-charging battery,
    recent poor call MOS, or API unreadable while the device still answers TCP)."""
    reasons: list[str] = []
    if not snap.get("reachable") and not snap.get("api_ok"):
        return "critical", ["cordless unreachable (alarm/announce endpoint offline)"]

    bp, charging = snap.get("battery_pct"), snap.get("charging")
    if bp is not None and not charging:
        if bp <= th["battery_crit"]:
            reasons.append(f"battery {bp}% and discharging (below {th['battery_crit']}% — endpoint will drop)")
            level = "critical"
        elif bp <= th["battery_warn"]:
            reasons.append(f"battery {bp}% and discharging (low)")
        # else fine

    if snap.get("api_ok"):
        if snap.get("wifi_connected") is False:
            reasons.append("Wi-Fi disconnected")
        elif snap.get("wifi_signal") is not None and snap["wifi_signal"] < th["wifi_min"]:
            reasons.append(f"Wi-Fi signal weak ({snap['wifi_signal']}/5)")
        # Call quality: only flag when the phone's LAST call was BOTH poor AND recent
        # (within mos_window). The phone retains a few historical RTP records, so a bad
        # call hours ago must not pin this sensor 'degraded' — and callqos already owns
        # per-call alerting; here it's a supporting, current-state signal only.
        m, age = snap.get("last_mos"), snap.get("last_mos_age_s")
        if m is not None and m < th["mos_min"] and age is not None and age <= th["mos_window"]:
            reasons.append(f"last call quality poor (MOS {m:.1f}, {age}s ago)")
    elif snap.get("reachable"):
        reasons.append("cordless answers on the network but its admin API is unreadable (wrong password?)")

    if any("battery" in r and "will drop" in r for r in reasons):
        return "critical", reasons
    return ("degraded", reasons) if reasons else ("ok", [])


def classify_gateway(down_exts: list[str], gw_exts: list[str]) -> tuple[str, list[str]]:
    """(level, reasons) for the GXW, DERIVED from which of its FXS-port extensions
    are currently down per rtpmon's rollup. All ports down = the gateway itself
    dropped (critical); some down = degraded (a handset unplugged or a partial
    fault); none = ok."""
    if not gw_exts:
        return "ok", []
    down = [e for e in gw_exts if e in set(down_exts or [])]
    if not down:
        return "ok", []
    if len(down) >= len(gw_exts):
        return "critical", [f"all {len(gw_exts)} gateway ports unregistered — the GXW gateway likely lost power or its uplink"]
    return "degraded", [f"{len(down)} of {len(gw_exts)} gateway ports down (exts {', '.join(down)})"]


_RANK = {"ok": 0, "degraded": 1, "critical": 2}


def health_transition(level: str, st: dict, min_cycles: int = 2) -> str:
    """One-shot, hysteretic device-alert state machine (mirrors rtpmon.outage_transition
    but for a 3-level single device). `st` carries {'cycles','level','alerted'}.
    Returns an event to notify on:
      'critical' / 'degraded' — fire once, after `min_cycles` consecutive unhealthy
        cycles at a level AT OR ABOVE the last alerted one (a worsening re-alerts);
      'recovered' — fire once when it returns to ok after having alerted;
      '' — nothing.
    The consecutive-cycle gate rejects a single flaky poll (one dropped Wi-Fi frame,
    a transient API timeout)."""
    if level == "ok":
        st["cycles"] = 0
        st["level"] = "ok"
        if st.get("alerted"):
            st["alerted"] = None
            return "recovered"
        return ""
    # unhealthy (degraded/critical)
    if level == st.get("level"):
        st["cycles"] = st.get("cycles", 0) + 1
    else:
        st["cycles"] = 1
        st["level"] = level
    if st["cycles"] < min_cycles:
        return ""
    prev = st.get("alerted")
    # fire when we haven't alerted yet, or the situation escalated above what we alerted
    if prev is None or _RANK[level] > _RANK[prev]:
        st["alerted"] = level
        return level
    return ""


# --------------------------------------------------------------------------- #
# I/O helpers.
# --------------------------------------------------------------------------- #
def gateway_down_exts_from_rollup() -> list[str] | None:
    """Read rtpmon's rollup sensor for the currently-down extensions (the reliable,
    already-gathered registration signal). None if unavailable (HA down / rtpmon off)."""
    try:
        import ha_client
        s = ha_client.get_state("sensor.switchboard_link_health")
    except Exception:
        return None
    if not s:
        return None
    a = s.get("attributes", {}) if isinstance(s, dict) else {}
    exts = list(a.get("unreachable_exts", []) or []) + list(a.get("offline_exts", []) or [])
    return [str(e) for e in dict.fromkeys(exts)]  # de-dup, stringify


def resolve_cordless_ip(cordless_ext: str, fallback_ip: str) -> str:
    """The cordless's CURRENT IP, taken from its live SIP registration so a
    DHCP-moved handset is auto-followed without editing cordless_ip. rtpmon
    publishes the registered contact IP as ``contact_ip`` on
    sensor.switchboard_link_<ext>; use it when present, otherwise fall back to the
    configured static IP (also covers: no cordless_ext set, HA down, rtpmon off,
    or the cordless de-registered)."""
    if not cordless_ext:
        return fallback_ip
    try:
        import ha_client
        s = ha_client.get_state(f"sensor.switchboard_link_{cordless_ext}")
    except Exception:
        return fallback_ip
    ip = (s.get("attributes", {}) or {}).get("contact_ip") if isinstance(s, dict) else None
    return str(ip).strip() if ip else fallback_ip


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _thresholds() -> dict:
    return {
        "battery_crit": _env_int("CORDLESS_BATTERY_CRIT", 15),
        "battery_warn": _env_int("CORDLESS_BATTERY_WARN", 30),
        "wifi_min": _env_int("CORDLESS_WIFI_MIN", 2),
        "mos_min": float(os.environ.get("CORDLESS_MOS_MIN", "3.4") or 3.4),
        "mos_window": _env_int("CORDLESS_MOS_WINDOW_S", 900),  # only a call within 15 min counts
    }


def _publish_cordless(level: str, reasons: list[str], snap: dict) -> None:
    try:
        import ha_client
    except Exception:
        return
    attrs = {
        "friendly_name": "Cordless health",
        "icon": "mdi:phone-in-talk" if level == "ok" else ("mdi:phone-alert" if level == "critical" else "mdi:phone-cog"),
        "reasons": reasons,
        "reachable": bool(snap.get("reachable") or snap.get("api_ok")),
    }
    for k in ("battery_pct", "charging", "battery_health", "wifi_connected", "wifi_signal", "wifi_ssid", "last_mos"):
        if snap.get(k) is not None:
            attrs[k] = snap[k]
    if snap.get("battery_pct") is not None:
        attrs["unit_of_measurement"] = "%"
        ha_client.set_state("sensor.switchboard_cordless_health", snap["battery_pct"], {**attrs, "health": level})
    else:
        ha_client.set_state("sensor.switchboard_cordless_health", level, attrs)


def _publish_gateway(level: str, reasons: list[str], down: list[str], gw_exts: list[str]) -> None:
    try:
        import ha_client
    except Exception:
        return
    ha_client.set_state("sensor.switchboard_gateway_health", level, {
        "friendly_name": "GXW gateway health",
        "icon": "mdi:router-network" if level == "ok" else "mdi:router-network-wireless",
        "ports_total": len(gw_exts),
        "ports_up": len(gw_exts) - len([e for e in gw_exts if e in set(down or [])]),
        "down_exts": [e for e in gw_exts if e in set(down or [])],
        "reasons": reasons,
        "health": level,
    })


def _notify(device: str, event: str, reasons: list[str]) -> None:
    if os.environ.get("DEVICE_HEALTH_ALERTS", "true").lower() in ("false", "0", "no"):
        return
    try:
        import ha_client
    except Exception:
        return
    nid = f"switchboard_{device}_health"
    label = "Cordless" if device == "cordless" else "GXW gateway"
    if event == "recovered":
        ha_client.notify(f"{label} recovered — back to normal.", title=f"Switchboard: {label.lower()} OK", notification_id=nid)
    else:
        why = "; ".join(reasons) or event
        title = f"Switchboard: {label.lower()} {'CRITICAL' if event == 'critical' else 'degraded'}"
        ha_client.notify(f"{label} {event}: {why}", title=title, notification_id=nid)


def run() -> None:
    interval = max(30, _env_int("DEVICE_HEALTH_INTERVAL", 120))
    cordless_ip = os.environ.get("CORDLESS_IP", "192.168.6.71").strip()
    cordless_ext = os.environ.get("CORDLESS_EXT", "").strip()
    cordless_pw = os.environ.get("CORDLESS_PASSWORD", "")
    gw_exts = [e.strip() for e in os.environ.get("GATEWAY_PORTS", "11,12,13,14,15,16,17,18").split(",") if e.strip()]
    th = _thresholds()
    cst: dict = {}
    gst: dict = {}
    last_ip = None
    follow = f"auto-follow ext {cordless_ext}" if cordless_ext else "static"
    print(f"[devhealth] up: cordless={cordless_ip or '(disabled)'} ({follow}) "
          f"api={'yes' if cordless_pw else 'no-password'} "
          f"gateway_ports={','.join(gw_exts)} every {interval}s", flush=True)
    while True:
        # --- cordless (IP auto-followed from its live SIP registration) ---
        probe_ip = resolve_cordless_ip(cordless_ext, cordless_ip)
        if probe_ip != last_ip:
            if last_ip is not None:
                print(f"[devhealth] cordless IP now {probe_ip} (was {last_ip}) — following DHCP", flush=True)
            last_ip = probe_ip
        if probe_ip:
            try:
                snap = probe_cordless(probe_ip, cordless_pw)
                level, reasons = classify_cordless(snap, th)
                _publish_cordless(level, reasons, snap)
                ev = health_transition(level, cst)
                if ev:
                    print(f"[devhealth] cordless {ev}: {'; '.join(reasons) or ev}", flush=True)
                    _notify("cordless", ev, reasons)
            except Exception as e:
                print(f"[devhealth] cordless poll error: {e}", flush=True)
        # --- gateway (derived from rtpmon rollup) ---
        try:
            down = gateway_down_exts_from_rollup()
            if down is not None:
                level, reasons = classify_gateway(down, gw_exts)
                _publish_gateway(level, reasons, down, gw_exts)
                ev = health_transition(level, gst)
                if ev:
                    print(f"[devhealth] gateway {ev}: {'; '.join(reasons) or ev}", flush=True)
                    _notify("gateway", ev, reasons)
        except Exception as e:
            print(f"[devhealth] gateway poll error: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    run()
