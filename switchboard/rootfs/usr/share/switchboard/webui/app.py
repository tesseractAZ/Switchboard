"""Switchboard — Ingress management UI.

A deliberately small FastAPI app that shows live PBX state inside Home
Assistant's sidebar:

* which room phones are registered (PJSIP contacts / qualify status),
* active calls right now,
* the configured rooms and trunk, straight from the add-on options.

It talks to Asterisk over the Manager Interface (AMI) on 127.0.0.1:5038 using a
tiny stdlib client (no extra dependencies). All links are relative so the app
works unmodified behind Home Assistant Ingress, whatever path it is mounted on.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# The AMI client lives in a framework-free sibling module so its wire-format
# parsing can be unit-tested without FastAPI. Ensure this directory is importable
# regardless of how uvicorn is launched.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/usr/share/switchboard/wakeup")

# FastAPI is optional at *import* time so the pure validation/shaping helpers
# below (valid_ext, channel_has_crlf, build_lights_payload, parse_wakeup_hhmm,
# is_light_entity passthrough) can be unit-tested with plain ``python3`` on a box
# where FastAPI/httpx aren't installed — exactly the constraint the test suite
# runs under. When FastAPI IS present (the add-on container) the real app and
# routes are wired up at the bottom of this module; when it's absent, importing
# this file still succeeds and ``app`` is None.
try:
    from fastapi import FastAPI, Request  # noqa: E402
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
    _HAVE_FASTAPI = True
except ImportError:  # pragma: no cover - exercised only on the test box
    FastAPI = Request = FileResponse = HTMLResponse = JSONResponse = None  # type: ignore
    _HAVE_FASTAPI = False

from ami import (  # noqa: E402
    AMIError,
    codecs_for_channels,
    connect_extensions,
    get_endpoints,
    get_status_bundle,
    hangup_channel,
    is_registered,
    page_all,
    peer_channels_by_ext,
    ring_extension,
    set_mwi,
    summarize_calls,
    transfer_channel,
)
try:
    import store as wakeup_store  # noqa: E402  (wake-up store; absent in dev)
except ImportError:  # pragma: no cover
    wakeup_store = None
try:
    import timeparse as wakeup_timeparse  # noqa: E402  (spoken-time parser; absent in dev)
except ImportError:  # pragma: no cover
    wakeup_timeparse = None
try:
    import mwi_store  # noqa: E402  (persistent MWI flags)
except ImportError:  # pragma: no cover
    mwi_store = None
try:
    import ha_client  # noqa: E402  (Home Assistant lights)
except ImportError:  # pragma: no cover
    ha_client = None

OPTIONS_PATH = Path("/data/options.json")

# A room extension is 2-6 digits. Every endpoint that interpolates an ext into an
# AMI call validates it against BOTH the configured room set AND this regex, so a
# CRLF-bearing / dial-string value can never reach Asterisk even via a path that
# skipped the room-set check.
_EXT_RE = re.compile(r"^[0-9]{2,6}$")

# Home Assistant Ingress proxies every request from the Supervisor's fixed
# internal IP (172.30.32.2). Because the add-on uses host_network, the port is
# also reachable directly on the LAN — which would bypass Ingress auth. Per the
# add-on docs ("Only connections from 172.30.32.2 must be allowed"), reject any
# client that isn't the Supervisor (loopback kept for local health checks). This
# closes the bypass without changing the bind, so Ingress keeps working.
INGRESS_CLIENT = "172.30.32.2"
_ALLOWED_CLIENTS = frozenset({INGRESS_CLIENT, "127.0.0.1", "::1"})


def _client_allowed(host: str) -> bool:
    return host in _ALLOWED_CLIENTS


class _NoApp:
    """Stand-in for the FastAPI app when FastAPI isn't installed (the test box).

    Its ``get``/``post``/``middleware`` return an identity decorator so the route
    *functions* below still define cleanly (and stay importable/testable), but no
    framework machinery is required. On the real add-on container ``app`` is a
    genuine FastAPI instance instead."""

    def _decorator(self, *_a, **_k):
        def wrap(fn):
            return fn
        return wrap

    get = post = middleware = _decorator


app = FastAPI(title="Switchboard", docs_url=None, redoc_url=None) if _HAVE_FASTAPI else _NoApp()


# --------------------------------------------------------------------------- #
# Pure validation / shaping helpers (no FastAPI, no I/O) — unit-tested directly.
# Every new POST endpoint funnels its untrusted path/body through one of these
# before anything reaches an AMI / HA call.
# --------------------------------------------------------------------------- #
def valid_ext(ext: str) -> bool:
    """A 2-6 digit room extension. Rejects empty, non-digit, CRLF-bearing, and
    over-long values so an ext can never smuggle a dial string or extra AMI line
    into an Originate/MWI Channel/Mailbox.

    Uses ``fullmatch`` (not ``match``): ``$`` matches *before* a trailing newline,
    so ``re.match(r"...$", "11\\n")`` would WRONGLY accept ``"11\\n"``. ``fullmatch``
    anchors at end-of-string and rejects any trailing CR/LF."""
    return bool(_EXT_RE.fullmatch(ext or ""))


def channel_has_crlf(channel: str) -> bool:
    """True if a channel name carries a CR/LF (AMI-injection) — used to reject a
    /api/hangup body before it reaches ami.hangup_channel."""
    return "\r" in (channel or "") or "\n" in (channel or "")


def is_light_entity(entity_id: str) -> bool:
    """Defence-in-depth ``light.*`` guard. Delegates to ha_client's validator
    when available (single source of truth) and falls back to an identical regex
    when ha_client can't be imported (the test box), so the guard is testable in
    isolation."""
    if ha_client is not None:
        return bool(ha_client.is_light_entity(entity_id))
    return bool(re.match(r"^light\.[a-z0-9_]+$", entity_id or ""))


def parse_wakeup_hhmm(raw: str):
    """Normalize a wake-up time from the GUI into canonical "HH:MM" (24h), or
    None if it can't be understood.

    Accepts both an already-formatted ``HH:MM`` (what the <input type=time> sends)
    and a free-form spoken-style string ("7:30 am", "quarter past six") by
    delegating to the shared wakeup timeparse. Always validates the final value
    so a hand-crafted body like "99:99" or "1:2\r\nevil" is rejected."""
    s = (raw or "").strip()
    if not s:
        return None
    # Reject control characters (CR/LF/etc.) up front: the shared timeparse is
    # deliberately lenient and would happily extract "07:30" out of a noisy body
    # like "07:30\r\nevil" — we never want a hand-crafted body with embedded
    # control bytes to be accepted as a clean time.
    if any(ord(c) < 0x20 for c in s):
        return None
    m = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})", s)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return f"{h:02d}:{mn:02d}"
        return None
    if wakeup_timeparse is not None:
        return wakeup_timeparse.parse(s)
    return None


def configured_room_exts(opts: dict) -> set:
    """The set of configured room extensions (strings) from the add-on options."""
    return {str(r.get("ext")) for r in (opts.get("rooms") or []) if r.get("ext") is not None}


def channels_by_ext(channels: list) -> dict:
    """Map each room ext to its active channel name (the longest-running leg, so a
    room in a real call wins over a momentary ring leg). Powers the per-room
    "Hang up" button, which needs the Asterisk channel name. Pure."""
    out: dict[str, str] = {}
    best: dict[str, int] = {}
    for ch in channels or []:
        ext = ch.get("ext", "")
        name = ch.get("channel", "")
        if not ext or not name:
            continue
        dur = 0
        try:
            for p in str(ch.get("duration", "0")).split(":"):
                dur = dur * 60 + int(p)
        except (ValueError, TypeError):
            dur = 0
        if ext not in best or dur >= best[ext]:
            best[ext] = dur
            out[ext] = name
    return out


def build_lights_payload(by_area: dict, available: bool) -> dict:
    """Shape ha_client.lights_by_area() into the /api/lights response.

    {"areas": {area_label: [{entity_id,name,state}, ...], ...}, "lights_ok": bool}
    — the empty-string "no area" bucket is surfaced under a friendly label, and
    only the three UI-facing fields are echoed (never raw HA internals). Pure, so
    the grouping shape is unit-tested without touching HA."""
    areas: dict[str, list] = {}
    for area, lights in (by_area or {}).items():
        label = area if area else "Other"
        areas[label] = [
            {
                "entity_id": lt.get("entity_id", ""),
                "name": lt.get("name") or lt.get("entity_id", ""),
                "state": lt.get("state") or "unknown",
            }
            for lt in lights
            if is_light_entity(str(lt.get("entity_id", "")))
        ]
    return {"areas": areas, "lights_ok": bool(available)}


def load_options() -> dict:
    try:
        with OPTIONS_PATH.open() as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def wakeups_list(rooms_by_ext: dict) -> list[dict]:
    """Pending wake-ups (room + time), soonest first."""
    if wakeup_store is None:
        return []
    try:
        data = wakeup_store.all_wakeups()
    except Exception:  # never let a store hiccup break /api/status
        return []
    out = [
        {
            "ext": ext,
            "label": rooms_by_ext.get(ext, ext),
            "hhmm": e.get("hhmm", ""),
            "target_epoch": e.get("target_epoch", 0),
        }
        for ext, e in data.items()
    ]
    out.sort(key=lambda w: w["target_epoch"])
    return out


ANNOUNCE_DIR = "/run/switchboard/announce"
_ANNOUNCE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\.wav$")


@app.middleware("http")
async def restrict_to_ingress(request: Request, call_next):
    # The announcement audio is fetched over the LAN by the media players (an
    # AirPlay speaker, not the Supervisor ingress client), so exempt just that GET
    # route from the ingress client-IP guard. It only ever serves an ephemeral,
    # name-validated *.wav from ANNOUNCE_DIR — see serve_announcement.
    if request.method == "GET" and (request.url.path or "").startswith("/announce/"):
        return await call_next(request)
    client = request.client.host if request.client else ""
    if not _client_allowed(client):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)


@app.get("/announce/{name}")
def serve_announcement(name: str):
    """Serve a generated announcement WAV to a LAN media player (play_media). Name
    is strictly validated (no path traversal); only files under ANNOUNCE_DIR."""
    if not _ANNOUNCE_NAME.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    path = os.path.join(ANNOUNCE_DIR, name)
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="audio/wav")


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/api/status")
def api_status() -> JSONResponse:
    opts = load_options()
    rooms_cfg = {str(r.get("ext")): r for r in (opts.get("rooms") or [])}
    trunk = opts.get("trunk") or {}

    ami_ok = True
    error = None
    try:
        # One AMI session for all three reads (endpoints + contacts + channels)
        # instead of three connect/login/logoff cycles per refresh.
        endpoints, contacts, channels = get_status_bundle()
    except (AMIError, OSError) as exc:
        ami_ok = False
        # Return a generic marker to the client; log the detail server-side only.
        error = "unreachable"
        print(f"[switchboard-webui] AMI unavailable: {exc}", flush=True)
        endpoints, contacts, channels = [], {}, []

    # Turn raw channel legs into readable calls ("Kitchen ↔ Office") and a
    # per-room "what is this phone doing right now" map. Tag each live leg with the
    # codec it negotiated (only runs while a call is up — no channels, no reads),
    # so the UI can show "↔ Outside · µ-law" and a transcode is visible at a glance.
    rooms_by_ext = {ext: (cfg.get("name") or ext) for ext, cfg in rooms_cfg.items()}
    codecs = codecs_for_channels(channels)
    for ch in channels:
        ch["codec"] = codecs.get(ch.get("channel", ""), "")
    summary = summarize_calls(channels, rooms_by_ext)
    by_ext = summary["by_ext"]
    # ext -> active channel name, so the "Hang up" button can target it; and
    # ext -> the FAR leg's channel, so "Transfer" can redirect the other party.
    chan_by_ext = channels_by_ext(channels)
    peer_by_ext = peer_channels_by_ext(channels, rooms_by_ext)

    # Which rooms have a "you have a message / call the operator" stutter-tone
    # flag set (persistent UI source of truth; the badge mirrors it).
    mwi_set = set()
    if mwi_store is not None:
        try:
            mwi_set = set(mwi_store.exts())
        except Exception:  # never let a store hiccup break /api/status
            mwi_set = set()

    # Registration is derived from DeviceState (the signal Asterisk already
    # aggregates from contact reachability); the contact row is enrichment
    # (status text + RTT) only. See ami.is_registered.
    rooms = []
    seen = set()
    for ep in endpoints:
        name = ep["name"]
        if name == "trunk":
            continue
        seen.add(name)
        cfg = rooms_cfg.get(name, {})
        contact = contacts.get(name, {})
        device_state = ep["state"]
        c_status = contact.get("status", "")
        registered = is_registered(device_state, c_status)
        call = by_ext.get(name, {})
        rooms.append(
            {
                "ext": name,
                "label": cfg.get("name", name),
                "device_state": device_state,
                "registered": registered,
                "contact_status": c_status or ("Reachable" if registered else "Unregistered"),
                "rtt": contact.get("rtt", ""),
                # What this phone is doing now (empty when idle): "Ringing" /
                # "Talking" and the other party ("Office", "Outside", "Operator").
                "call_state": call.get("state", ""),
                "call_peer": call.get("peer", ""),
                "call_codec": call.get("codec", ""),
                "channel": chan_by_ext.get(name, ""),
                "peer_channel": peer_by_ext.get(name, ""),
                "mwi": name in mwi_set,
            }
        )
    for ext, cfg in rooms_cfg.items():
        if ext not in seen:
            rooms.append(
                {
                    "ext": ext,
                    "label": cfg.get("name", ext),
                    "device_state": "Unavailable",
                    "registered": False,
                    "contact_status": "Unregistered",
                    "rtt": "",
                    "call_state": "",
                    "call_peer": "",
                    "channel": "",
                    "mwi": ext in mwi_set,
                }
            )
    rooms.sort(key=lambda r: r["ext"])

    return JSONResponse(
        {
            "ami_ok": ami_ok,
            "error": error,
            "rooms": rooms,
            "calls": summary["calls"],
            "wakeups": wakeups_list(rooms_by_ext),
            "trunk": {
                "enabled": bool(trunk.get("enabled")),
                "provider": trunk.get("provider_host", ""),
            },
        }
    )


@app.post("/api/ring/{ext}")
def api_ring(ext: str) -> JSONResponse:
    """Place a one-cycle test ring to a room phone.

    The ext is validated against the configured rooms before anything is sent to
    Asterisk, so this can only ring a known endpoint (never originate an outside
    call). Reached only via Ingress (the Supervisor-only middleware applies).
    """
    opts = load_options()
    known = {str(r.get("ext")) for r in (opts.get("rooms") or [])}
    if ext not in known:
        return JSONResponse({"ok": False, "error": "unknown extension"}, status_code=404)
    try:
        ok = ring_extension(ext)
    except (AMIError, OSError) as exc:
        print(f"[switchboard-webui] ring {ext} failed: {exc}", flush=True)
        return JSONResponse({"ok": False, "error": "unreachable"}, status_code=502)
    return JSONResponse({"ok": ok})


@app.post("/api/wakeup/{ext}/cancel")
def api_wakeup_cancel(ext: str) -> JSONResponse:
    """Cancel a room's pending wake-up. Ingress-only (middleware applies)."""
    if wakeup_store is None:
        return JSONResponse({"ok": False, "error": "unavailable"}, status_code=503)
    try:
        ok = wakeup_store.cancel(ext)
    except Exception as exc:
        print(f"[switchboard-webui] wakeup cancel {ext} failed: {exc}", flush=True)
        return JSONResponse({"ok": False, "error": "error"}, status_code=500)
    return JSONResponse({"ok": ok})


@app.post("/api/connect/{a}/{b}")
def api_connect(a: str, b: str) -> JSONResponse:
    """Patch a call between two CONFIGURED room phones (operator "connect").

    Both exts must be in the configured room set; ami.connect_extensions enforces
    that again (with the digit guard) so the originate can only match the room
    ``_X.`` pattern, never the trunk's outbound pattern. Ingress-only."""
    opts = load_options()
    known = configured_room_exts(opts)
    if a not in known or b not in known:
        return JSONResponse({"ok": False, "error": "unknown extension"}, status_code=404)
    if a == b:
        return JSONResponse({"ok": False, "error": "same extension"}, status_code=400)
    try:
        ok = connect_extensions(a, b, known)
    except (AMIError, OSError) as exc:
        print(f"[switchboard-webui] connect {a}->{b} failed: {exc}", flush=True)
        return JSONResponse({"ok": False, "error": "unreachable"}, status_code=502)
    return JSONResponse({"ok": ok})


@app.post("/api/hangup")
async def api_hangup(request: Request) -> JSONResponse:
    """Hang up one active channel by its Asterisk channel name.

    The channel string is supplied by /api/status (Asterisk-derived), but we
    reject a CRLF-bearing value defensively before it reaches the AMI Hangup so
    it can't inject extra manager lines. Ingress-only."""
    body = await _json_body(request)
    channel = str(body.get("channel") or "")
    if not channel or channel_has_crlf(channel):
        return JSONResponse({"ok": False, "error": "bad channel"}, status_code=400)
    try:
        ok = hangup_channel(channel)
    except (AMIError, OSError) as exc:
        print(f"[switchboard-webui] hangup failed: {exc}", flush=True)
        return JSONResponse({"ok": False, "error": "unreachable"}, status_code=502)
    return JSONResponse({"ok": ok})


@app.post("/api/transfer")
async def api_transfer(request: Request) -> JSONResponse:
    """Blind-transfer one active call to a room extension.

    Body: {"channel": <far-leg channel>, "target": <room ext>}. The channel is
    the OTHER party's leg (peer_channel from /api/status) so redirecting it sends
    that caller to the chosen room while the operator drops out. Both the channel
    (CRLF-rejected) and the target (must be a currently-configured room ext) are
    validated before they reach the AMI Redirect — transfer_channel re-checks the
    target against the same allow-list as defence in depth. Ingress-only."""
    body = await _json_body(request)
    channel = str(body.get("channel") or "")
    target = str(body.get("target") or "")
    if not channel or channel_has_crlf(channel):
        return JSONResponse({"ok": False, "error": "bad channel"}, status_code=400)
    allowed = configured_room_exts(load_options())
    if target not in allowed:
        return JSONResponse({"ok": False, "error": "bad target"}, status_code=400)
    # Refuse a transfer to a room that's offline — a redirect there would just drop
    # the caller. This keeps the accepted set aligned with the UI's registered-only
    # target list. Fail-open: if the status read itself errors we proceed, so a
    # transient AMI hiccup can't block a transfer to a room that's actually up.
    try:
        endpoints, contacts, _channels = get_status_bundle()
        target_reg = {
            ep["name"]: is_registered(ep["state"], contacts.get(ep["name"], {}).get("status", ""))
            for ep in endpoints
        }
        if target_reg.get(target) is False:
            return JSONResponse({"ok": False, "error": "target offline"}, status_code=409)
    except (AMIError, OSError):
        pass
    try:
        ok = transfer_channel(channel, target, allowed)
    except (AMIError, OSError) as exc:
        print(f"[switchboard-webui] transfer failed: {exc}", flush=True)
        return JSONResponse({"ok": False, "error": "unreachable"}, status_code=502)
    return JSONResponse({"ok": ok})


@app.post("/api/wakeup/{ext}/set")
async def api_wakeup_set(ext: str, request: Request) -> JSONResponse:
    """Schedule (or replace) a room's wake-up. Body: {"hhmm": ...} or {"time": ...}.

    The ext is validated against the configured room set; the time is parsed and
    re-validated via the shared timeparse/store so a hand-crafted "99:99" or a
    CRLF-bearing value is rejected. Ingress-only."""
    if wakeup_store is None:
        return JSONResponse({"ok": False, "error": "unavailable"}, status_code=503)
    opts = load_options()
    if ext not in configured_room_exts(opts):
        return JSONResponse({"ok": False, "error": "unknown extension"}, status_code=404)
    body = await _json_body(request)
    raw = body.get("hhmm")
    if raw is None:
        raw = body.get("time")
    hhmm = parse_wakeup_hhmm(str(raw or ""))
    if not hhmm:
        return JSONResponse({"ok": False, "error": "bad time"}, status_code=400)
    try:
        entry = wakeup_store.set_wakeup(ext, hhmm)
    except Exception as exc:
        print(f"[switchboard-webui] wakeup set {ext} failed: {exc}", flush=True)
        return JSONResponse({"ok": False, "error": "error"}, status_code=500)
    return JSONResponse({"ok": True, "hhmm": entry.get("hhmm", hhmm)})


@app.post("/api/page")
def api_page() -> JSONResponse:
    """Page every REGISTERED room phone at once (intercom). Ingress-only.

    Only registered rooms are paged (an unreachable handset would just ring out),
    and ami.page_all digit-guards each ext before it reaches an Originate."""
    opts = load_options()
    known = configured_room_exts(opts)
    targets = sorted(ext for ext in _registered_exts() if ext in known) or sorted(known)
    if not targets:
        return JSONResponse({"ok": False, "error": "no rooms"}, status_code=404)
    try:
        ok = page_all(targets)
    except (AMIError, OSError) as exc:
        print(f"[switchboard-webui] page failed: {exc}", flush=True)
        return JSONResponse({"ok": False, "error": "unreachable"}, status_code=502)
    return JSONResponse({"ok": ok, "count": len(targets)})


@app.post("/api/mwi/{ext}/{state}")
def api_mwi(ext: str, state: str) -> JSONResponse:
    """Set or clear a room's message-waiting indicator (stutter dial tone).

    Validates the ext against BOTH the configured room set and the digit regex,
    and the state against on|off, before touching Asterisk. Updates the AMI MWI
    and the persistent UI flag with an "optimistic CLEAR, honest SET" rule:
    persist the flag when the AMI call succeeded OR when clearing, but when a SET
    is rejected by Asterisk leave the badge OFF and report the failure — so the ✉
    badge never claims a stutter tone is playing when the tone was never set.
    Ingress-only."""
    opts = load_options()
    if ext not in configured_room_exts(opts) or not valid_ext(ext):
        return JSONResponse({"ok": False, "error": "unknown extension"}, status_code=404)
    if state not in ("on", "off"):
        return JSONResponse({"ok": False, "error": "bad state"}, status_code=400)
    on = state == "on"
    try:
        ok = set_mwi(ext, on)
    except (AMIError, OSError) as exc:
        print(f"[switchboard-webui] mwi {ext} {state} failed: {exc}", flush=True)
        return JSONResponse({"ok": False, "error": "unreachable"}, status_code=502)
    # Optimistic CLEAR, honest SET: persist the flag when AMI accepted it, or
    # always when clearing (on=False — a failed clear should still drop the
    # badge). When a SET is REFUSED (ok is False), do NOT set the badge: the
    # stutter tone never started, so claiming it did would be a lie the init
    # replay would then re-push to Asterisk on the next restart.
    if ok or not on:
        if mwi_store is not None:
            try:
                mwi_store.set_flag(ext, on)
            except Exception as exc:
                print(f"[switchboard-webui] mwi flag {ext} failed: {exc}", flush=True)
    elif not ok:  # a SET that Asterisk rejected — leave the badge off, report it
        return JSONResponse({"ok": False, "error": "ami_rejected"}, status_code=502)
    return JSONResponse({"ok": True, "mwi": on})


@app.get("/api/lights")
def api_lights() -> JSONResponse:
    """All light entities grouped by HA area, plus a lights_ok reachability flag.

    {"areas": {area: [{entity_id,name,state}]}, "lights_ok": bool}. lights_ok is
    False (and areas empty) when HA is unreachable, so the UI can say
    "HA unavailable" rather than render a misleading empty list. Ingress-only."""
    if ha_client is None:
        return JSONResponse(build_lights_payload({}, False))
    try:
        ok = ha_client.available()
        by_area = ha_client.lights_by_area() if ok else {}
    except Exception as exc:
        print(f"[switchboard-webui] lights fetch failed: {exc}", flush=True)
        return JSONResponse(build_lights_payload({}, False))
    return JSONResponse(build_lights_payload(by_area, ok))


@app.post("/api/lights/{entity_id}/{state}")
def api_light_set(entity_id: str, state: str) -> JSONResponse:
    """Turn one light on/off. The entity must be a real ``light.*`` entity (guard)
    and the state must be on|off. Ingress-only."""
    if ha_client is None:
        return JSONResponse({"ok": False, "error": "unavailable"}, status_code=503)
    if not is_light_entity(entity_id):
        return JSONResponse({"ok": False, "error": "not a light"}, status_code=400)
    if state not in ("on", "off"):
        return JSONResponse({"ok": False, "error": "bad state"}, status_code=400)
    try:
        ok = ha_client.set_light(entity_id, state == "on")
    except Exception as exc:
        print(f"[switchboard-webui] light {entity_id} {state} failed: {exc}", flush=True)
        return JSONResponse({"ok": False, "error": "unreachable"}, status_code=502)
    return JSONResponse({"ok": ok})


async def _json_body(request: Request) -> dict:
    """Best-effort JSON body as a dict ({} on empty / malformed)."""
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _registered_exts() -> list[str]:
    """The room exts Asterisk currently reports as registered (DeviceState online).
    Empty if AMI is unreachable — page falls back to all configured rooms."""
    try:
        eps = get_endpoints()
    except (AMIError, OSError):
        return []
    out = []
    for ep in eps:
        name = ep.get("name", "")
        if name and name != "trunk" and is_registered(ep.get("state", "")):
            out.append(name)
    return out


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Switchboard</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, Segoe UI, Roboto, sans-serif;
         margin: 0; padding: 1.25rem; background: var(--bg, #f6f7f9); }
  h1 { font-size: 1.3rem; margin: 0 0 .25rem; }
  .sub { color: #888; font-size: .85rem; margin-bottom: 1rem; }
  .grid { display: grid; gap: .6rem;
          grid-template-columns: repeat(auto-fill, minmax(215px, 1fr)); }
  .card { background: var(--card, #fff); border-radius: 12px; padding: .8rem .9rem;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); display: flex; flex-direction: column; }
  .card .ext { font-size: .75rem; color: #999; }
  .card .name { font-weight: 600; font-size: 1rem; margin: .1rem 0 .4rem; }
  .pill { display: inline-block; font-size: .72rem; padding: .15rem .5rem;
          border-radius: 999px; font-weight: 600; align-self: flex-start; }
  .up { background: #e3f7e8; color: #1a7f37; }
  .down { background: #fde7e7; color: #b42318; }
  .busy { background: #fff4e0; color: #b25e00; }
  .conn { font-size: .75rem; color: #b25e00; margin-top: .4rem; }
  .conn .codec { color: #888; font-weight: 600; }
  .ringbtn { margin-top: .6rem; width: 100%; font-size: .75rem; font-weight: 600;
             padding: .35rem .5rem; border-radius: 8px; border: 1px solid var(--bd, #d4d7dd);
             background: var(--btn, #f3f4f6); color: inherit; cursor: pointer; }
  .ringbtn:hover:not(:disabled) { background: var(--btnh, #e8eaed); }
  .ringbtn:disabled { opacity: .5; cursor: default; }
  /* Per-card action row: a tidy 2-up grid so every button is the same width and
     the row never wraps unevenly. Each button is fully labelled (no bare icons);
     an over-long label ellipsises rather than blowing out the column. */
  .actions { display: grid; grid-template-columns: 1fr 1fr; gap: .35rem; margin-top: .6rem; }
  .actions .ringbtn { margin-top: 0; width: auto; padding: .35rem .4rem;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mwibadge { font-size: .9rem; line-height: 1; margin-left: .35rem; }
  .ringbtn.armed { background: #fff4e0; border-color: #e2a23a; color: #b25e00; }
  .wakerow { display: flex; gap: .35rem; margin-top: .45rem; align-items: center; }
  .wakerow .wklab { font-size: .9rem; flex: 0 0 auto; opacity: .8; }
  .wakerow input[type=time] { flex: 1 1 auto; min-width: 0; font: inherit; font-size: .75rem;
             padding: .3rem .4rem; border-radius: 8px; border: 1px solid var(--bd, #d4d7dd);
             background: var(--card, #fff); color: inherit; }
  /* Wake-up list: one clean "Room — time · when [Cancel]" row per pending call. */
  .wakelist { list-style: none; padding: 0; margin: 0; display: grid; gap: .4rem; }
  .wakeitem { display: flex; align-items: center; gap: .6rem; padding: .5rem .7rem;
              background: var(--card, #fff); border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .wakeitem .wkname { font-weight: 600; }
  .wakeitem .wkwhen { margin-left: auto; font-variant-numeric: tabular-nums; }
  .wakeitem .wkday { color: #888; font-weight: 500; font-size: .85em; margin-left: .25rem; }
  .wakeitem .wkcancel { width: auto; margin: 0; padding: .2rem .6rem; flex: 0 0 auto; }
  .toolbar { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; margin-bottom: 1rem; }
  .toolbar .ringbtn { width: auto; margin-top: 0; padding: .45rem .8rem; font-size: .82rem; }
  .lightgrid { display: grid; gap: .6rem;
          grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); }
  .areacard { background: var(--card, #fff); border-radius: 12px; padding: .8rem .9rem;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .areacard h3 { font-size: .8rem; margin: 0 0 .5rem; color: #888; text-transform: uppercase;
          letter-spacing: .03em; }
  .lightrow { display: flex; justify-content: space-between; align-items: center;
          gap: .5rem; padding: .25rem 0; }
  .lightrow .lname { font-size: .9rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .toggle { font-size: .72rem; font-weight: 600; padding: .25rem .6rem; border-radius: 999px;
          border: 1px solid var(--bd, #d4d7dd); background: var(--btn, #f3f4f6); color: inherit;
          cursor: pointer; flex: 0 0 auto; }
  .toggle.on { background: #e3f7e8; color: #1a7f37; border-color: #a8e0b8; }
  .toggle:disabled { opacity: .5; cursor: default; }
  section { margin-top: 1.5rem; }
  .muted { color: #999; font-size: .85rem; }
  .calllist { list-style: none; padding: 0; margin: 0; }
  .calllist li { display: flex; justify-content: space-between; gap: .6rem; align-items: baseline;
                 padding: .5rem .2rem; border-bottom: 1px solid var(--bd, #eee); font-size: .95rem; }
  .calllist .detail { font-weight: 600; }
  .calllist .meta { color: #888; font-size: .8rem; white-space: nowrap; }
  .kind-outside .detail::before { content: "📞 "; }
  .kind-operator .detail::before { content: "🎧 "; }
  .kind-internal .detail::before { content: "🏠 "; }
  .banner { background: #fde7e7; color: #b42318; padding: .6rem .8rem;
            border-radius: 8px; margin-bottom: 1rem; font-size: .85rem; }
  @media (prefers-color-scheme: dark) {
    /* Set --card on body so EVERY card-like surface inherits it — the room cards
       (.card), the lights area cards (.areacard), and the wake-up time input all
       paint from var(--card). Scoping it to .card alone left the lights section
       white with light text on it (unreadable) in dark mode. */
    body { --bg:#111418; color:#e6e6e6; --card:#1b1f24; }
    :root { --bd:#2a2f36; --btn:#262b31; --btnh:#2f353c; }
  }
</style>
</head>
<body>
  <h1>🔌 Switchboard</h1>
  <div class="sub" id="sub">Loading…</div>
  <div id="banner"></div>

  <div class="toolbar">
    <button class="ringbtn" id="pageall">📢 Page all</button>
    <span class="muted" id="connecthint"></span>
  </div>

  <div class="grid" id="rooms"></div>

  <section>
    <h2 style="font-size:1rem;">Active calls</h2>
    <div id="calls"><div class="muted">—</div></div>
  </section>

  <section>
    <h2 style="font-size:1rem;">⏰ Wake-up calls</h2>
    <div id="wakeups"><div class="muted">—</div></div>
  </section>

  <section>
    <h2 style="font-size:1rem;">💡 Lights</h2>
    <div id="lights"><div class="muted">—</div></div>
  </section>

<script>
// Escape any server-supplied value before it touches innerHTML. Room labels and
// AMI caller-ID (attacker-controlled on inbound trunk calls) are untrusted.
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtDur(d) { return String(d || '').replace(/^00:/, ''); }
function codecName(c) {
  const m = {ulaw:'µ-law', alaw:'A-law', g722:'G.722', g729:'G.729', g723:'G.723', g726:'G.726', opus:'Opus', ilbc:'iLBC', slin16:'L16'};
  // "g722/ulaw" (two legs, i.e. a transcode) renders as "G.722/µ-law".
  return String(c || '').split('/').map(x => m[x.toLowerCase()] || x).join('/');
}
function fmt12(hhmm) {
  const m = /^(\\d{1,2}):(\\d{2})$/.exec(hhmm || '');
  if (!m) return hhmm || '';
  let h = +m[1]; const ap = h < 12 ? 'AM' : 'PM';
  h = h % 12 || 12;
  return h + ':' + m[2] + ' ' + ap;
}
// "today" / "tomorrow" / a weekday for a wake-up's next-occurrence epoch, so the
// list says WHEN it will actually ring, not just a bare clock time.
function wakeDay(epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000), now = new Date();
  const day = new Date(now); day.setHours(0,0,0,0);
  const diff = Math.round((new Date(d).setHours(0,0,0,0) - day.getTime()) / 86400000);
  if (diff <= 0) return 'today';
  if (diff === 1) return 'tomorrow';
  return d.toLocaleDateString([], {weekday: 'short'});
}
// The HH:MM of a room's pending wake-up (for prefilling its <input type=time>),
// or '' when none is set.
function pendingHHMM(wakeups, ext) {
  const w = (wakeups || []).find(x => x.ext === ext);
  return (w && /^\\d{1,2}:\\d{2}$/.test(w.hhmm || '')) ? w.hhmm : '';
}

// Keep a "Ringing…" button disabled across the 4s auto-refreshes for ~one ring
// cycle, so clicking Test ring gives durable feedback.
const ringingUntil = {};

// Operator "connect" is a two-click gesture: click Connect on room A (arms it),
// then click Connect on room B to patch them together. connectArm holds A's ext
// while we wait for the second pick; clicking the armed room again cancels.
let connectArm = null;

// ext -> active Asterisk channel name (for the Hang up button), refreshed from
// /api/status each cycle.
const roomChannels = {};
// ext -> the FAR leg's channel (for the Transfer button): redirecting it sends
// the OTHER party to a chosen room. Refreshed alongside roomChannels.
const roomPeers = {};
// [{ext,label,registered}] snapshot of all rooms, used to offer transfer targets.
let roomDirectory = [];

function updateConnectHint() {
  const el = document.getElementById('connecthint');
  if (!el) return;
  el.textContent = connectArm ? ('Connecting ext ' + connectArm + ' — pick another room…') : '';
}

async function refresh() {
  try {
    const res = await fetch('./api/status', {cache: 'no-store'});
    const data = await res.json();

    const banner = document.getElementById('banner');
    banner.innerHTML = data.ami_ok ? '' :
      '<div class="banner">Cannot reach Asterisk Manager: ' +
      esc(data.error || 'unknown') + '. The PBX may still be starting.</div>';

    const reg = data.rooms.filter(r => r.registered).length;
    document.getElementById('sub').textContent =
      reg + ' of ' + data.rooms.length + ' phones registered' +
      (data.trunk.enabled ? ' · trunk: ' + (data.trunk.provider || 'on') : ' · no trunk');

    const now = Date.now();
    // Refresh the ext->channel maps (Hang up uses the room's own leg; Transfer
    // uses the far leg) and the room directory used to pick a transfer target.
    for (const k in roomChannels) delete roomChannels[k];
    for (const k in roomPeers) delete roomPeers[k];
    data.rooms.forEach(r => {
      if (r.channel) roomChannels[r.ext] = r.channel;
      if (r.peer_channel) roomPeers[r.ext] = r.peer_channel;
    });
    roomDirectory = data.rooms.map(r => ({ext: r.ext, label: r.label, registered: r.registered}));
    const grid = document.getElementById('rooms');
    grid.innerHTML = data.rooms.map(r => {
      // "Not in use" means registered-and-idle (green) — only an active call
      // state ("In use", "Ringing", "Busy", "On Hold") is busy (orange).
      const ds = (r.device_state||'').toLowerCase();
      const active = (ds.includes('use') && ds !== 'not in use') ||
                     ds.includes('ring') || ds === 'busy' || ds === 'on hold';
      let cls, txt;
      if (active) { cls = 'busy'; txt = r.call_state || r.device_state; }
      else if (r.registered) { cls = 'up'; txt = 'Registered'; }
      else { cls = 'down'; txt = 'Offline'; }
      // Who this phone is talking to / ringing, when known — plus the live codec.
      const conn = r.call_peer
        ? '<div class="conn">↔ ' + esc(r.call_peer) +
            (r.call_codec ? ' <span class="codec">· ' + esc(codecName(r.call_codec)) + '</span>' : '') +
          '</div>'
        : '';
      const ex = esc(r.ext);
      const ringing = (ringingUntil[r.ext] || 0) > now;
      const ringDis = (!r.registered || ringing) ? ' disabled' : '';
      const ringLbl = ringing ? 'Ringing…' : '🔔 Test ring';
      const ringBtn = '<button class="ringbtn" data-ring="' + ex + '"' + ringDis + '>' + ringLbl + '</button>';

      // Connect: arm this room, then pick another to patch them. The armed card
      // highlights; offline rooms can't start/receive a patch.
      const armed = connectArm === r.ext;
      const connDis = (!r.registered) ? ' disabled' : '';
      const connLbl = armed ? '✖ Cancel' : '🔗 Connect';
      const connBtn = '<button class="ringbtn' + (armed ? ' armed' : '') +
                      '" data-connect="' + ex + '"' + connDis + '>' + connLbl + '</button>';

      // Hang up: only meaningful when this room has an active call leg.
      const hangBtn = active
        ? '<button class="ringbtn" data-hangup="' + ex + '" title="Hang up">📵 Hang up</button>' : '';

      // Transfer: only when there's a far party to hand off (peer_channel set).
      // Sends the OTHER party to a room you pick, dropping this leg out.
      const xferBtn = (active && r.peer_channel)
        ? '<button class="ringbtn" data-transfer="' + ex + '" title="Transfer call">↪ Transfer</button>' : '';

      // Message-waiting (stutter dial tone): toggle on/off, badge when set. Labelled
      // so it doesn't read as a stray icon — "Message" sets it, "Clear" removes it.
      const mwiState = r.mwi ? 'off' : 'on';
      const mwiTitle = r.mwi ? 'Clear message-waiting' : 'Set message-waiting (stutter tone)';
      const mwiBtn = '<button class="ringbtn" data-mwi="' + ex +
                     '" data-state="' + mwiState + '" title="' + mwiTitle + '">' +
                     (r.mwi ? '✉ Clear' : '✉ Message') + '</button>';
      const mwiBadge = r.mwi ? '<span class="mwibadge" title="message waiting">✉️</span>' : '';

      // Per-room wake-up setter: a small time input + Set. fill the input with
      // any pending time so it round-trips.
      const wkVal = esc(pendingHHMM(data.wakeups, r.ext));
      // Clock prefix so the time box reads as "set a wake-up", not a stray field.
      const wakeRow = '<div class="wakerow">' +
        '<span class="wklab" title="Set a wake-up for this room">⏰</span>' +
        '<input type="time" data-waketime="' + ex + '" value="' + wkVal + '" title="Wake-up time">' +
        '<button class="ringbtn" data-wakeset="' + ex + '" title="Set wake-up">Set</button></div>';

      return '<div class="card"><div class="ext">ext ' + ex + mwiBadge + '</div>' +
             '<div class="name">' + esc(r.label) + '</div>' +
             '<span class="pill ' + cls + '">' + esc(txt) + '</span>' +
             conn +
             '<div class="actions">' + ringBtn + connBtn + hangBtn + xferBtn + mwiBtn + '</div>' +
             wakeRow + '</div>';
    }).join('');
    updateConnectHint();

    const callsEl = document.getElementById('calls');
    if (!data.calls || !data.calls.length) {
      callsEl.innerHTML = '<div class="muted">No active calls</div>';
    } else {
      callsEl.innerHTML = '<ul class="calllist">' + data.calls.map(c =>
        '<li class="kind-' + esc(c.kind || 'internal') + '">' +
        '<span class="detail">' + esc(c.detail) + '</span>' +
        '<span class="meta">' + esc(c.state || '') +
        (c.duration ? ' · ' + esc(fmtDur(c.duration)) : '') +
        (c.codec ? ' · ' + esc(codecName(c.codec)) : '') + '</span></li>'
      ).join('') + '</ul>';
    }

    const wakeEl = document.getElementById('wakeups');
    const wk = data.wakeups || [];
    if (!wk.length) {
      wakeEl.innerHTML = '<div class="muted">No wake-ups set. Use the ⏰ time box on a room card, ' +
        'or dial 42 from a phone and say a time.</div>';
    } else {
      wakeEl.innerHTML = '<ul class="wakelist">' + wk.map(w => {
        const day = wakeDay(w.target_epoch);
        return '<li class="wakeitem">' +
          '<span class="wkname">' + esc(w.label) + '</span>' +
          '<span class="wkwhen">' + esc(fmt12(w.hhmm)) +
          (day ? ' <span class="wkday">' + esc(day) + '</span>' : '') + '</span>' +
          '<button class="ringbtn wkcancel" data-cancel="' + esc(w.ext) + '">Cancel</button>' +
        '</li>';
      }).join('') + '</ul>';
    }
  } catch (e) {
    document.getElementById('sub').textContent = 'Status unavailable';
  }
}

async function post(url) {
  const res = await fetch(url, {method: 'POST'});
  let j = {}; try { j = await res.json(); } catch (e) {}
  if (!res.ok || !j.ok) throw new Error((j && j.error) || ('HTTP ' + res.status));
  return j;
}
async function postJSON(url, body) {
  const res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(body)});
  let j = {}; try { j = await res.json(); } catch (e) {}
  if (!res.ok || !j.ok) throw new Error((j && j.error) || ('HTTP ' + res.status));
  return j;
}
function flash(btn, msg) {
  const prev = btn.textContent;
  btn.textContent = msg;
  setTimeout(() => { if (btn.textContent === msg) btn.textContent = prev; }, 1600);
}

// All per-room actions are delegated off the (rebuilt-every-refresh) grid.
document.getElementById('rooms').addEventListener('click', async (e) => {
  const btn = e.target.closest('button');
  if (!btn || btn.disabled) return;

  // Test ring.
  let ext = btn.getAttribute('data-ring');
  if (ext) {
    btn.disabled = true; btn.textContent = 'Ringing…';
    ringingUntil[ext] = Date.now() + 9000;
    try { await post('./api/ring/' + encodeURIComponent(ext)); }
    catch (err) { ringingUntil[ext] = 0; btn.disabled = false; flash(btn, 'Failed'); }
    return;
  }

  // Connect (two-click patch): first click arms, second click on a different
  // room patches them; clicking the armed room again cancels.
  ext = btn.getAttribute('data-connect');
  if (ext) {
    if (connectArm === null) { connectArm = ext; updateConnectHint(); refresh(); return; }
    if (connectArm === ext) { connectArm = null; updateConnectHint(); refresh(); return; }
    const a = connectArm, b = ext; connectArm = null; updateConnectHint();
    btn.disabled = true; btn.textContent = 'Connecting…';
    try { await post('./api/connect/' + encodeURIComponent(a) + '/' + encodeURIComponent(b)); refresh(); }
    catch (err) { btn.disabled = false; flash(btn, 'Failed'); }
    return;
  }

  // Hang up an active call leg on this room.
  ext = btn.getAttribute('data-hangup');
  if (ext) {
    const ch = (roomChannels[ext] || '');
    if (!ch) { flash(btn, '—'); return; }
    btn.disabled = true;
    try { await postJSON('./api/hangup', {channel: ch}); refresh(); }
    catch (err) { btn.disabled = false; flash(btn, '✖'); }
    return;
  }

  // Transfer the far party of this room's call to another room. We redirect the
  // PEER leg (roomPeers), so the chosen room rings while this leg drops out.
  ext = btn.getAttribute('data-transfer');
  if (ext) {
    const ch = (roomPeers[ext] || '');
    if (!ch) { flash(btn, '—'); return; }
    const opts = roomDirectory
      .filter(d => d.ext !== ext && d.registered)
      .map(d => d.ext + ' — ' + d.label);
    // Accept "12" or a pasted "12 — Office" line: take the LEADING digit run only,
    // so a free-typed label like "Room 11" yields "" (a no-op) rather than a stray
    // digit that could misdial to a valid-but-wrong room.
    const raw = (prompt('Transfer call to which room?\\n' + opts.join('\\n'), '') || '');
    const target = (raw.match(/^\\s*([0-9]+)/) || ['', ''])[1];
    if (!target) return;
    btn.disabled = true;
    // postJSON throws on a non-ok response (bad target / Redirect failed).
    try { await postJSON('./api/transfer', {channel: ch, target: target}); refresh(); }
    catch (err) { btn.disabled = false; flash(btn, '✖'); }
    return;
  }

  // Message-waiting toggle (data-state is the target on/off).
  ext = btn.getAttribute('data-mwi');
  if (ext) {
    const state = btn.getAttribute('data-state');
    btn.disabled = true;
    try { await post('./api/mwi/' + encodeURIComponent(ext) + '/' + state); refresh(); }
    catch (err) { btn.disabled = false; flash(btn, '✖'); }
    return;
  }

  // Set a wake-up from this card's time input.
  ext = btn.getAttribute('data-wakeset');
  if (ext) {
    const inp = document.querySelector('input[data-waketime="' + ext + '"]');
    const hhmm = inp ? inp.value : '';
    if (!hhmm) { flash(btn, 'pick a time'); return; }
    btn.disabled = true;
    try { await postJSON('./api/wakeup/' + encodeURIComponent(ext) + '/set', {hhmm: hhmm}); refresh(); }
    catch (err) { btn.disabled = false; flash(btn, 'Failed'); }
    return;
  }
});

// Page all: one click intercoms every registered room.
document.getElementById('pageall').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true; const prev = btn.textContent; btn.textContent = 'Paging…';
  try { await post('./api/page'); btn.textContent = '📢 Paging…'; }
  catch (err) { flash(btn, 'Failed'); }
  finally { setTimeout(() => { btn.disabled = false; btn.textContent = prev; }, 2500); }
});

// Cancel a wake-up (delegated; the section is rebuilt each refresh).
document.getElementById('wakeups').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-cancel]');
  if (!btn || btn.disabled) return;
  const ext = btn.getAttribute('data-cancel');
  btn.disabled = true; btn.textContent = '…';
  try {
    await fetch('./api/wakeup/' + encodeURIComponent(ext) + '/cancel', {method: 'POST'});
    refresh();
  } catch (err) { btn.disabled = false; btn.textContent = 'Cancel'; }
});

// ---- Lights (separate endpoint + cadence; a slow HA call must not stall the
// fast PBX status refresh). Optimistic toggle on click, reconciled on refresh.
async function refreshLights() {
  const el = document.getElementById('lights');
  let data;
  try {
    const res = await fetch('./api/lights', {cache: 'no-store'});
    data = await res.json();
  } catch (e) { el.innerHTML = '<div class="muted">Lights unavailable.</div>'; return; }
  if (!data.lights_ok) { el.innerHTML = '<div class="muted">Home Assistant unavailable.</div>'; return; }
  const areas = data.areas || {};
  const names = Object.keys(areas).sort();
  if (!names.length) { el.innerHTML = '<div class="muted">No light entities found.</div>'; return; }
  el.innerHTML = '<div class="lightgrid">' + names.map(area =>
    '<div class="areacard"><h3>' + esc(area) + '</h3>' +
    (areas[area] || []).map(lt => {
      const on = (lt.state || '').toLowerCase() === 'on';
      const target = on ? 'off' : 'on';
      return '<div class="lightrow"><span class="lname" title="' + esc(lt.entity_id) + '">' +
             esc(lt.name) + '</span>' +
             '<button class="toggle' + (on ? ' on' : '') + '" data-light="' + esc(lt.entity_id) +
             '" data-target="' + target + '">' + (on ? 'On' : 'Off') + '</button></div>';
    }).join('') + '</div>'
  ).join('') + '</div>';
}

document.getElementById('lights').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-light]');
  if (!btn || btn.disabled) return;
  const id = btn.getAttribute('data-light');
  const target = btn.getAttribute('data-target');
  // Optimistic flip so the UI feels instant; reconcile from HA on next refresh.
  const wantOn = target === 'on';
  btn.disabled = true;
  btn.classList.toggle('on', wantOn);
  btn.textContent = wantOn ? 'On' : 'Off';
  btn.setAttribute('data-target', wantOn ? 'off' : 'on');
  try { await post('./api/lights/' + encodeURIComponent(id) + '/' + target); }
  catch (err) { flash(btn, '✖'); }
  finally { btn.disabled = false; setTimeout(refreshLights, 600); }
});

refresh();
refreshLights();
setInterval(refresh, 4000);
setInterval(refreshLights, 15000);
</script>
</body>
</html>
"""
