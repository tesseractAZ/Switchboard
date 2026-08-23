"""Weather for the dial-a-status menu + smart wake-up, straight from the U.S.
National Weather Service (api.weather.gov). No HA ``weather.*`` entity is needed —
we fetch for the home's coordinates (from ``ha_client.ha_location()``). Free, no
API key; NWS just wants a descriptive User-Agent. Everything degrades to '' / []
on any error so a voice flow can say "weather is unavailable" instead of crashing.

The pure formatter ``speak_weather`` is unit-tested; only ``fetch_forecast`` does I/O.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

TIMEOUT = float(os.environ.get("SW_WEATHER_TIMEOUT", "6") or "6")
_UA = "Switchboard-HA-addon (github.com/tesseractAZ/Switchboard)"
# The NWS forecast URL is static per location, so cache it and skip the /points
# lookup. The in-process dict alone was USELESS for the path that matters: the
# wake-up runs as a fresh AGI process every morning, so the cache was always
# cold and every wake-up made TWO requests (points, then forecast) — doubling
# both the latency and the chance of a timeout at the one moment it counts.
# Persist it, so a warm install makes exactly one request.
_grid_cache: dict = {}
GRID_CACHE_PATH = os.environ.get("SW_WEATHER_GRID_CACHE",
                                 "/data/state/weather-grid.json")


def _grid_cache_load() -> dict:
    try:
        with open(GRID_CACHE_PATH) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _grid_cache_store(key: str, url: str) -> None:
    """Best-effort persist. Never raises — a read-only or full volume must not
    break the forecast, it just costs the extra /points hop next time."""
    try:
        d = _grid_cache_load()
        if d.get(key) == url:
            return
        d[key] = url
        tmp = GRID_CACHE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, GRID_CACHE_PATH)
    except OSError:
        pass


def _get(url: str):
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _forecast_url(lat, lon) -> str:
    key = (round(float(lat), 4), round(float(lon), 4))
    if key in _grid_cache:
        return _grid_cache[key]
    skey = f"{key[0]},{key[1]}"
    disk = _grid_cache_load().get(skey)
    if disk:
        _grid_cache[key] = disk
        return disk
    pts = _get(f"https://api.weather.gov/points/{skey}")
    url = (((pts or {}).get("properties") or {}).get("forecast")) or ""
    if url:
        _grid_cache[key] = url
        _grid_cache_store(skey, url)
    return url


def fetch_forecast(lat, lon, attempts: int = 2) -> list:
    """NWS forecast periods [{name, temperature, temperatureUnit, shortForecast}],
    or [] on any error (missing coords, network, parse).

    Tried more than once by default: api.weather.gov is a free public service
    that intermittently stalls, and the caller that matters most — the morning
    wake-up — gets exactly one shot at it. A single 6 s timeout on 2026-08-21
    silently cost that morning's weather readout entirely."""
    last: list = []
    for attempt in range(max(1, attempts)):
        last = _fetch_forecast_once(lat, lon)
        if last:
            return last
    return last


def _fetch_forecast_once(lat, lon) -> list:
    try:
        if lat is None or lon is None:
            return []
        url = _forecast_url(lat, lon)
        if not url:
            return []
        data = _get(url)
        periods = (((data or {}).get("properties") or {}).get("periods")) or []
        return periods if isinstance(periods, list) else []
    except (urllib.error.URLError, ValueError, TypeError, OSError, KeyError):
        return []


def speak_weather(periods: list, count: int = 2) -> str:
    """Pure: a spoken sentence from NWS periods, e.g.
    'Tonight, clear, 75 degrees. Wednesday, sunny, 101 degrees.' '' if no data."""
    if not periods:
        return ""
    out = []
    for p in periods[:count]:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name", "")).strip()
        temp = p.get("temperature")
        short = str(p.get("shortForecast", "")).strip().rstrip(".")
        piece = name
        if short:
            piece = (piece + ", " + short.lower()) if piece else short.lower()
        if isinstance(temp, (int, float)) and not isinstance(temp, bool):
            piece += f", {int(temp)} degrees"
        piece = piece.strip().strip(",").strip()
        if piece:
            out.append(piece)
    return (". ".join(out) + ".") if out else ""
