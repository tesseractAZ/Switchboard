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
import urllib.error
import urllib.request

TIMEOUT = float(os.environ.get("HA_TIMEOUT", "5") or "5")

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


def _request(method: str, path: str, body=None):
    """One HA Core API call. Returns (status:int, text:str) or (None, None) when
    no candidate host could be reached. Caches the first base that connects."""
    global _cached
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
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # reached HA, it complained (auth/404/template error)
            _cached = cand
            try:
                return exc.code, exc.read().decode("utf-8", "replace")
            except Exception:
                return exc.code, ""
        except Exception:
            continue  # connection/DNS error — try the next candidate
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
    only if HA accepted it. `data` is passed through as the service payload."""
    if domain not in _ALLOWED_SERVICE_DOMAINS:
        return False
    if not re.fullmatch(r"[a-z_]+", service or ""):
        return False
    status, _ = _request("POST", f"/services/{domain}/{service}", data or {})
    return status in (200, 201)


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
