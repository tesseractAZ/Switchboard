"""Tests for the HA-integrated voice foundation: NWS weather formatting, the
power/house spoken read-outs, the dial-a-status matcher, and ha_client's generic
guards. All pure (no HA / no network) — run with plain python3.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in ("rootfs/usr/share/switchboard/webui", "rootfs/usr/share/switchboard/operator"):
    d = str(_ROOT / p)
    if d not in sys.path:
        sys.path.insert(0, d)

import ha_client        # noqa: E402
import ha_reports       # noqa: E402
import status_match     # noqa: E402
import weather          # noqa: E402

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def test_speak_weather() -> None:
    periods = [
        {"name": "Tonight", "temperature": 75, "temperatureUnit": "F", "shortForecast": "Clear"},
        {"name": "Wednesday", "temperature": 101, "temperatureUnit": "F", "shortForecast": "Sunny"},
    ]
    got = weather.speak_weather(periods)
    check("weather: two-period sentence",
          got == "Tonight, clear, 75 degrees. Wednesday, sunny, 101 degrees.")
    check("weather: empty -> ''", weather.speak_weather([]) == "")
    check("weather: count trims", weather.speak_weather(periods, count=1) == "Tonight, clear, 75 degrees.")
    check("weather: bad rows tolerated", weather.speak_weather([None, {"name": "Now"}]) == "Now.")


def test_format_power() -> None:
    check("power: grid on + all stats",
          ha_reports.format_power("on", 41, 5.8, 71.8) ==
          "Grid power is connected. The home battery is at 41 percent, "
          "there's about 5.8 hours of runway, and solar is covering 72 percent of the load.")
    check("power: grid out",
          ha_reports.format_power("off", 41, None, None).startswith(
              "Grid power is out. You're running on battery. The home battery is at 41 percent."))
    check("power: nothing -> unavailable",
          ha_reports.format_power(None, None, None, None) == "Power status is unavailable right now.")
    check("power: zero solar omitted", "solar" not in ha_reports.format_power("on", 50, None, 0))
    check("power: whole-hour runway", "1 hour of runway" in ha_reports.format_power(None, None, 1.0, None))


def test_format_house() -> None:
    check("house: thermostat + lights",
          ha_reports.format_house([("West Hallway", 74, "cool")], 3, 34) ==
          "The West Hallway is 74 degrees. 3 lights are on.")
    check("house: all off", ha_reports.format_house([], 0, 10) == "All lights are off.")
    check("house: one light", ha_reports.format_house([], 1, 5) == "1 light is on.")
    check("house: nothing -> unavailable",
          ha_reports.format_house([], 0, 0) == "House status is unavailable right now.")


def test_status_match() -> None:
    check("match: power", status_match.match("power") == "power")
    check("match: weather", status_match.match("weather") == "weather")
    check("match: house", status_match.match("house") == "house")
    check("match: home -> house", status_match.match("home") == "house")
    check("match: battery -> power", status_match.match("battery") == "power")
    check("match: forecast -> weather", status_match.match("forecast") == "weather")
    check("match: clipped 'weath' -> weather", status_match.match("weath") == "weather")
    check("match: spoken 'two' -> weather", status_match.match("two") == "weather")
    check("match: empty -> ''", status_match.match("") == "")
    check("match: gibberish -> ''", status_match.match("banana potato") == "")
    check("digit: 1 -> power", status_match.from_digit("1") == "power")
    check("digit: 3 -> house", status_match.from_digit("3") == "house")
    check("digit: 9 -> ''", status_match.from_digit("9") == "")


def test_ha_client_guards() -> None:
    check("ha: valid entity id", ha_client.is_entity_id("sensor.foo_bar_1"))
    check("ha: rejects malformed id", not ha_client.is_entity_id("Sensor.Foo") and not ha_client.is_entity_id("nope"))
    # A non-allow-listed domain is rejected BEFORE any network I/O.
    check("ha: call_service rejects unlisted domain", ha_client.call_service("shell_command", "x", {}) is False)
    check("ha: call_service rejects bad service name", ha_client.call_service("light", "turn on!", {}) is False)
    check("ha: get_state rejects malformed id (no I/O)", ha_client.get_state("bad id") is None)


def main() -> None:
    test_speak_weather()
    test_format_power()
    test_format_house()
    test_status_match()
    test_ha_client_guards()
    print()
    if _failures:
        print(f"{_failures} FAILURE(S)")
        raise SystemExit(1)
    print("all HA-voice foundation tests passed")


if __name__ == "__main__":
    main()
