"""Spoken Home-Assistant status read-outs for the dial-a-status menu (and reused
by smart wake-up for the weather line).

The ``format_*`` functions are PURE (given numbers/strings) so they're unit-tested
without HA; the ``*_report`` wrappers fetch live state via ha_client + weather and
degrade to an "unavailable" sentence rather than raising, so a voice flow always
has something to say.
"""

from __future__ import annotations

import ha_client
import weather

# This home's EcoFlow power entities (discovered live). Overridable via the staged
# features.json so another install can point at its own sensors; a missing/blank
# entity is just skipped in the spoken summary.
DEFAULT_POWER = {
    "grid": "input_boolean.grid_available",
    "battery": "sensor.ecoflow_panel_ecoflow_backup_pool",
    "runway": "sensor.ecoflow_panel_ecoflow_runway_to_reserve",
    "solar": "sensor.ecoflow_panel_ecoflow_solar_fraction_of_load",
}


def _num(state):
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


def _cap(s: str) -> str:
    return (s[:1].upper() + s[1:]) if s else s


def _and_join(parts: list) -> str:
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    if len(parts) == 2:
        return parts[0] + " and " + parts[1]
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _hours(h: float) -> str:
    r = round(h, 1)
    if abs(r - round(r)) < 0.05:
        r = int(round(r))
    return f"{r} hour" + ("" if r == 1 else "s")


# --------------------------------------------------------------------------- #
# Power
# --------------------------------------------------------------------------- #
def format_power(grid, batt, runway, solar) -> str:
    """Pure. grid: 'on'/'off'/None; batt %, runway hours, solar % (numbers or None)."""
    sentences = []
    if grid == "on":
        sentences.append("Grid power is connected.")
    elif grid == "off":
        sentences.append("Grid power is out. You're running on battery.")
    stats = []
    if batt is not None:
        stats.append(f"the home battery is at {int(round(batt))} percent")
    if runway is not None:
        stats.append(f"there's about {_hours(runway)} of runway")
    if solar is not None and solar > 0:
        stats.append(f"solar is covering {int(round(solar))} percent of the load")
    if stats:
        sentences.append(_cap(_and_join(stats)) + ".")
    if not sentences:
        return "Power status is unavailable right now."
    return " ".join(sentences)


def power_report(entities: dict | None = None) -> str:
    e = {**DEFAULT_POWER, **(entities or {})}

    def st(key):
        eid = e.get(key)
        s = ha_client.get_state(eid) if eid else None
        return (s or {}).get("state") if isinstance(s, dict) else None

    grid = st("grid")
    return format_power(grid if grid in ("on", "off") else None,
                        _num(st("battery")), _num(st("runway")), _num(st("solar")))


# --------------------------------------------------------------------------- #
# House (thermostats + how many lights are on)
# --------------------------------------------------------------------------- #
def format_house(climates: list, lights_on: int, lights_total: int) -> str:
    """Pure. climates: [(name, current_temp_or_None, hvac_mode)]; light counts ints."""
    sentences = []
    for name, temp, _mode in climates or []:
        if temp is not None and name:
            sentences.append(f"The {name} is {int(round(temp))} degrees.")
    if lights_total:
        if lights_on == 0:
            sentences.append("All lights are off.")
        elif lights_on == 1:
            sentences.append("1 light is on.")
        else:
            sentences.append(f"{lights_on} lights are on.")
    if not sentences:
        return "House status is unavailable right now."
    return " ".join(sentences)


def house_report() -> str:
    climates = []
    for s in ha_client.get_states():
        if isinstance(s, dict) and str(s.get("entity_id", "")).startswith("climate."):
            a = s.get("attributes") or {}
            climates.append((a.get("friendly_name") or s.get("entity_id", "").split(".")[-1],
                             _num(a.get("current_temperature")), s.get("state")))
    lights = ha_client.get_lights()
    on = sum(1 for l in lights if str(l.get("state")) == "on")
    return format_house(climates, on, len(lights))


# --------------------------------------------------------------------------- #
# Weather (NWS via the home's coordinates)
# --------------------------------------------------------------------------- #
def weather_report() -> str:
    lat, lon, _unit = ha_client.ha_location()
    line = weather.speak_weather(weather.fetch_forecast(lat, lon))
    return line or "Weather is unavailable right now."
