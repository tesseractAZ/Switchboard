"""Home Assistant Core API client for the Switchboard home-automation feature.

Reaches HA via the Supervisor proxy using the add-on's SUPERVISOR_TOKEN (no
separate credential) — config.yaml grants `homeassistant_api: true`. A
host_network add-on can't resolve the `supervisor` DNS name, so we also try the
Supervisor's fixed IP 172.30.32.2 (the same fallback switchboard-config uses for
timezone auto-detect).

Framework-free (stdlib urllib) so the pure bits — URL building, the lights
template, response parsing, entity validation — are unit-testable; the socket
call is the only I/O. Everything degrades gracefully: if HA is unreachable,
get_lights() returns [] and set_light() returns False rather than raising, so a
voice/TUI/GUI caller can say "unavailable" instead of crashing.

Shared by webui/app.py (GUI), console/console.py (TUI lights view) and the
switchboard-automation.agi voice flow. For local testing against a real HA,
set HA_BASE_URL (e.g. http://192.168.5.152:8123/api) + HA_TOKEN.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request


def _log(msg: str) -> None:
    """Surface an HA failure to stderr (add-on log). These paths used to swallow
    every error silently, so a mis-configured entity or a rejected service call
    left NO trace anywhere — the caller just no-op'd."""
    sys.stderr.write(f"[ha_client] {msg}\n")

TIMEOUT = float(os.environ.get("HA_TIMEOUT", "5") or "5")
# When HA is unreachable, short-circuit further calls for this long instead of
# re-paying the (per-candidate × TIMEOUT) penalty on every call — otherwise a
# flow that makes several reads while HA is down (e.g. the smart wake-up: scene +
# weather + calendar) stacks a ~30s silent tail. Recovers automatically after TTL.
NEG_TTL = float(os.environ.get("HA_NEG_TTL", "20") or "20")

# Only ever toggle a real light entity; never interpolate arbitrary text into a
# service call (defence-in-depth even though we also send it as a JSON body).
_ENTITY_RE = re.compile(r"^light\.[a-z0-9_]+$")

# A Jinja template (rendered by POST /template) that returns every light entity
# WITH its HA area in one round-trip — the REST /states payload carries no area,
# and the area/entity registries are websocket-only. `| tojson` escapes names
# with quotes/unicode safely.
_LIGHTS_TEMPLATE = (
    "[{% for s in states.light %}"
    '{"entity_id": {{ s.entity_id | tojson }}, '
    '"name": {{ s.name | tojson }}, '
    '"state": {{ s.state | tojson }}, '
    '"area": {{ (area_name(s.entity_id) or "") | tojson }}}'
    '{{ "," if not loop.last else "" }}'
    "{% endfor %}]"
)


def is_light_entity(entity_id: str) -> bool:
    return bool(_ENTITY_RE.match(entity_id or ""))


def _candidates():
    """(base_url, token) pairs to try, most-specific first. An explicit
    HA_BASE_URL/HA_TOKEN (testing) wins; otherwise the Supervisor proxy."""
    override = os.environ.get("HA_BASE_URL", "").strip().rstrip("/")
    if override:
        yield override, os.environ.get("HA_TOKEN", "").strip()
        return
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if token:
        yield "http://supervisor/core/api", token
        yield "http://172.30.32.2/core/api", token


_cached = None  # the first base that answered — skip dead hosts on later calls
_dead_until = 0.0  # if >now and nothing cached, every candidate was just failing


def _request(method: str, path: str, body=None):
    """One HA Core API call. Returns (status:int, text:str) or (None, None) when
    no candidate host could be reached. Caches the first base that connects, and
    negative-caches a total failure for NEG_TTL so back-to-back calls during an HA
    outage don't each re-pay the full connect-timeout penalty."""
    global _cached, _dead_until
    if not _cached and time.time() < _dead_until:
        return None, None
    cands = [_cached] if _cached else list(_candidates())
    for cand in cands:
        if not cand:
            continue
        base, token = cand
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{base}{path}",
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                _cached = cand
                _dead_until = 0.0
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # reached HA, it complained (auth/404/template error)
            _cached = cand
            _dead_until = 0.0
            try:
                return exc.code, exc.read().decode("utf-8", "replace")
            except Exception:
                return exc.code, ""
        except Exception:
            continue  # connection/DNS error — try the next candidate
    _dead_until = time.time() + NEG_TTL  # all candidates unreachable — back off briefly
    return None, None


def available() -> bool:
    """True if the HA Core API answers at all (any of the candidate hosts)."""
    status, _ = _request("GET", "/")
    return status == 200


def parse_lights(text: str) -> list[dict]:
    """Pure: parse the /template JSON array into a sorted, light-only list.
    Tolerates a non-JSON / error body by returning []."""
    try:
        rows = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if isinstance(r, dict) and is_light_entity(str(r.get("entity_id", ""))):
            out.append({
                "entity_id": r.get("entity_id", ""),
                "name": (r.get("name") or r.get("entity_id", "")),
                "state": (r.get("state") or "unknown"),
                "area": (r.get("area") or ""),
            })
    out.sort(key=lambda r: (str(r["area"] or "~"), str(r["name"])))
    return out


def get_lights() -> list[dict]:
    """All light entities as [{entity_id, name, state, area}], or [] if HA is
    unreachable."""
    status, text = _request("POST", "/template", {"template": _LIGHTS_TEMPLATE})
    if status != 200 or not text:
        return []
    return parse_lights(text)


def lights_by_area() -> dict:
    """{area_name: [light, ...]} (area '' = no area), areas in sorted order."""
    out: dict[str, list] = {}
    for light in get_lights():
        out.setdefault(light["area"], []).append(light)
    return out


def get_light_state(entity_id: str):
    """Current state string of one light ('on'/'off'/...), or None if unknown/
    unreachable."""
    if not is_light_entity(entity_id):
        return None
    status, text = _request("GET", f"/states/{entity_id}")
    if status != 200 or not text:
        return None
    try:
        return (json.loads(text) or {}).get("state")
    except (ValueError, TypeError):
        return None


def set_light(entity_id: str, turn_on: bool) -> bool:
    """Call light.turn_on / light.turn_off for a single validated entity.
    Returns True only if HA accepted the service call."""
    if not is_light_entity(entity_id):
        return False
    service = "turn_on" if turn_on else "turn_off"
    status, _ = _request("POST", f"/services/light/{service}", {"entity_id": entity_id})
    return status in (200, 201)


# --------------------------------------------------------------------------- #
# Generic reads + service calls (used by the dial-a-status menu, smart wake-up,
# and phone->speaker announce). Kept narrow: only well-formed entity ids, and
# services only on a small allow-list of domains — a voice flow can never reach
# an arbitrary domain (shell_command, notify, homeassistant, ...).
# --------------------------------------------------------------------------- #
_EID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_ALLOWED_SERVICE_DOMAINS = frozenset({"light", "scene", "media_player", "tts", "climate"})


def is_entity_id(entity_id: str) -> bool:
    return bool(_EID_RE.match(entity_id or ""))


def get_state(entity_id: str):
    """Full state dict {state, attributes, ...} for any entity, or None when the
    id is malformed / the entity is missing / HA is unreachable."""
    if not is_entity_id(entity_id):
        return None
    status, text = _request("GET", f"/states/{entity_id}")
    if status != 200 or not text:
        return None
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def get_states() -> list:
    """Every entity state [{entity_id, state, attributes}], or [] if unreachable."""
    status, text = _request("GET", "/states")
    if status != 200 or not text:
        return []
    try:
        d = json.loads(text)
        return d if isinstance(d, list) else []
    except (ValueError, TypeError):
        return []


def call_service(domain: str, service: str, data: dict | None = None) -> bool:
    """Call a HA service, restricted to the allow-listed domains. Returns True
    only if HA accepted it. `data` is passed through as the service payload.

    NOTE: HA returns 200 even when the target entity does not exist, so a True
    result means "HA accepted the call", NOT "something actually happened". For a
    user-supplied entity list (announce speakers, a wake-up scene) pre-check with
    entity_exists()/filter_existing() to tell a real action from a silent no-op."""
    if domain not in _ALLOWED_SERVICE_DOMAINS:
        _log(f"refused service on non-allow-listed domain {domain!r}")
        return False
    if not re.fullmatch(r"[a-z_]+", service or ""):
        _log(f"refused malformed service name {service!r}")
        return False
    status, _ = _request("POST", f"/services/{domain}/{service}", data or {})
    ok = status in (200, 201)
    if not ok:
        _log(f"{domain}.{service} rejected (status={status}) target={(data or {}).get('entity_id')}")
    return ok


def entity_exists(entity_id: str) -> bool:
    """True if the id resolves to a real HA entity right now (not merely well-formed).
    None on an HA outage is treated as 'not confirmed' -> False."""
    return get_state(entity_id) is not None


def set_state(entity_id: str, state, attributes: dict | None = None) -> bool:
    """Push an entity state into HA via the Core API (POST /states/<id>).

    This creates/updates a 'pushed' entity — it appears in the UI and is captured
    by the Recorder (so you can GRAPH it), but it is not backed by an integration,
    so it clears on an HA restart until the next push repopulates it. That is fine
    for call-quality telemetry, where the latest value plus history is all we want.

    Returns True on 200/201. HA rejects a non-finite / oversized state, so callers
    should pass a plain number or short string (state is capped at 255 chars)."""
    if not is_entity_id(entity_id):
        _log(f"set_state refused malformed entity id {entity_id!r}")
        return False
    body = {"state": str(state)[:255]}
    if attributes:
        body["attributes"] = attributes
    status, _ = _request("POST", f"/states/{entity_id}", body)
    ok = status in (200, 201)
    if not ok:
        _log(f"set_state {entity_id} failed (status={status})")
    return ok


def resolve_entities(entity_ids):
    """(present, missing, ha_up) for a configured entity list, via ONE get_states().

    HA 200s a service call to a nonexistent entity, so a caller acting on a user-
    supplied list (announce speakers) must check existence to tell a real action
    from a silent no-op. But get_state() can't distinguish 'entity missing' from
    'HA unreachable' — both look absent — so a naive existence filter would drop
    every speaker during a transient Core restart. This probes reachability once:
      * HA reachable (states non-empty): existence is meaningful -> real present/missing.
      * HA unreachable (ha_up False): existence is UNKNOWN; present == the input,
        missing == [] — the caller should proceed best-effort (attempt the call and
        let it fail honestly) rather than suppress a valid action on a brief outage.
    Missing ids are logged when HA is reachable."""
    states = get_states()
    if not states:
        return list(entity_ids or []), [], False
    present_ids = {s.get("entity_id") for s in states if isinstance(s, dict)}
    present = [e for e in (entity_ids or []) if e in present_ids]
    missing = [e for e in (entity_ids or []) if e not in present_ids]
    if missing:
        _log(f"configured entities not found in HA (dropped): {missing}")
    return present, missing, True


def notify(message: str, title: str = "Switchboard", notification_id: str = "") -> bool:
    """Create/replace a Home Assistant persistent notification (the bell menu), so
    an event that is otherwise log-only — a missed wake-up — is actually surfaced
    to the user. Deliberately its own path, NOT via call_service's voice-flow
    domain allow-list. Returns True only if HA accepted it."""
    data = {"message": str(message)[:500], "title": str(title)[:120]}
    if notification_id:
        data["notification_id"] = re.sub(r"[^a-z0-9_]", "_", str(notification_id).lower())[:64]
    status, _ = _request("POST", "/services/persistent_notification/create", data)
    ok = status in (200, 201)
    if not ok:
        _log(f"persistent_notification.create failed (status={status})")
    return ok


def ha_location():
    """(latitude, longitude, temp_unit) from /config, or (None, None, None) — used
    to fetch weather from NWS for the home's coordinates."""
    status, text = _request("GET", "/config")
    if status != 200 or not text:
        return None, None, None
    try:
        d = json.loads(text) or {}
        return d.get("latitude"), d.get("longitude"), (d.get("unit_system") or {}).get("temperature")
    except (ValueError, TypeError):
        return None, None, None


def get_calendar_events(entity_id: str, start_iso: str, end_iso: str) -> list:
    """Events in [start,end) for a calendar.* entity, or [] on any error. Used by
    smart wake-up to read the next event; graceful when no calendar is configured."""
    if not is_entity_id(entity_id):
        return []
    from urllib.parse import quote
    path = f"/calendars/{entity_id}?start={quote(start_iso)}&end={quote(end_iso)}"
    status, text = _request("GET", path)
    if status != 200 or not text:
        return []
    try:
        d = json.loads(text)
        return d if isinstance(d, list) else []
    except (ValueError, TypeError):
        return []
