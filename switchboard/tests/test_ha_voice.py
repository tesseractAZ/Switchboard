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
    # Unavailable lights are NOT off: a dead lighting network must not be spoken as
    # "all lights are off" (audit data-correctness fix).
    check("house: all lights unavailable != off",
          ha_reports.format_house([], 0, 3, 3) == "The lights are unavailable right now.")
    check("house: some unavailable surfaced separately",
          ha_reports.format_house([], 1, 3, 1) == "1 light is on. 1 light is unavailable.")
    check("house: two unavailable plural",
          "2 lights are unavailable" in ha_reports.format_house([], 0, 4, 2))


def test_num_rejects_non_finite() -> None:
    # HA can report nan/inf; float() accepts them and they must not reach speech/math.
    for junk in ("nan", "inf", "-inf", "NaN", "Infinity"):
        check(f"_num({junk!r}) -> None", ha_reports._num(junk) is None)
    check("_num('28') -> 28.0", ha_reports._num("28") == 28.0)
    check("_num('2.5') -> 2.5", ha_reports._num("2.5") == 2.5)


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
    check("bye: goodbye", status_match.is_goodbye("goodbye"))
    check("bye: no", status_match.is_goodbye("no thanks"))
    check("bye: done", status_match.is_goodbye("i'm done"))
    check("bye: hang up", status_match.is_goodbye("hang up"))
    check("bye: 'power' is not goodbye", not status_match.is_goodbye("power"))
    check("bye: empty is not goodbye", not status_match.is_goodbye(""))
    # A polite filler alongside a real request must NOT hang up mid-menu (audit).
    check("bye: 'power thanks' serves power, not goodbye", not status_match.is_goodbye("power thanks"))
    check("bye: 'no the weather' serves weather", not status_match.is_goodbye("no the weather"))
    check("match: 'power thanks' -> power", status_match.match("power thanks") == "power")
    # An explicit category keyword beats a bare spoken ordinal (audit).
    check("match: 'the one about the weather' -> weather",
          status_match.match("the one about the weather") == "weather")
    check("match: bare 'one' -> power", status_match.match("one") == "power")


def test_format_calendar() -> None:
    check("cal: timed event",
          ha_reports.format_calendar([{"summary": "Dentist", "start": {"dateTime": "2026-07-01T09:00:00-07:00"}}]) ==
          "Your next event is Dentist at 9:00 AM.")
    check("cal: PM time",
          ha_reports.format_calendar([{"summary": "Meeting", "start": {"dateTime": "2026-07-01T14:30:00-07:00"}}]) ==
          "Your next event is Meeting at 2:30 PM.")
    check("cal: all-day omits time",
          ha_reports.format_calendar([{"summary": "Holiday", "start": {"date": "2026-07-01"}}]) ==
          "Your next event is Holiday.")
    check("cal: skips empty summary",
          ha_reports.format_calendar([{"summary": ""}, {"summary": "Real", "start": {}}]) ==
          "Your next event is Real.")
    check("cal: empty -> ''", ha_reports.format_calendar([]) == "")


def test_ha_client_guards() -> None:
    check("ha: valid entity id", ha_client.is_entity_id("sensor.foo_bar_1"))
    check("ha: rejects malformed id", not ha_client.is_entity_id("Sensor.Foo") and not ha_client.is_entity_id("nope"))
    # A non-allow-listed domain is rejected BEFORE any network I/O.
    check("ha: call_service rejects unlisted domain", ha_client.call_service("shell_command", "x", {}) is False)
    check("ha: call_service rejects bad service name", ha_client.call_service("light", "turn on!", {}) is False)
    check("ha: get_state rejects malformed id (no I/O)", ha_client.get_state("bad id") is None)


def test_announce_audio() -> None:
    import array
    import os
    import tempfile
    import wave

    import announce_audio
    rate = 8000  # small rate keeps the synth fast
    chime = announce_audio.synth_chime(rate)
    check("chime: non-empty int16 samples",
          isinstance(chime, array.array) and chime.typecode == "h" and len(chime) > 0)
    check("chime: samples in int16 range", all(-32768 <= x <= 32767 for x in chime[::40]))
    speech = array.array("h", [1200, -1200] * 4000)  # fake 8000-sample speech
    tmp = os.path.join(tempfile.mkdtemp(), "ann.wav")
    announce_audio.write_combined(tmp, speech, rate)
    with wave.open(tmp, "rb") as w:
        check("combined: mono 16-bit at the chosen rate",
              w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == rate)
        frames = w.getnframes()
    gap = int(0.45 * rate)
    check("combined: length = 2 chimes + speech + 2 gaps",
          frames == 2 * len(chime) + len(speech) + 2 * gap)
    check("lan_ip: returns a string", isinstance(announce_audio.lan_ip(), str))


def test_negative_cache() -> None:
    # With no candidate host reachable (no HA_BASE_URL / SUPERVISOR_TOKEN), a failed
    # _request negative-caches so back-to-back calls during an outage short-circuit
    # instead of each re-paying the connect-timeout penalty.
    import os
    os.environ.pop("HA_BASE_URL", None)
    os.environ.pop("HA_TOKEN", None)
    os.environ.pop("SUPERVISOR_TOKEN", None)
    ha_client._cached = None
    ha_client._dead_until = 0.0
    check("neg-cache: unreachable -> (None, None)", ha_client._request("GET", "/") == (None, None))
    check("neg-cache: sets a back-off window", ha_client._dead_until > 0)
    check("neg-cache: short-circuits while dead", ha_client._request("GET", "/states") == (None, None))
    ha_client._dead_until = 0.0  # don't leak state into other checks


def main() -> None:
    test_speak_weather()
    test_format_power()
    test_format_house()
    test_num_rejects_non_finite()
    test_status_match()
    test_format_calendar()
    test_ha_client_guards()
    test_announce_audio()
    test_negative_cache()
    print()
    if _failures:
        print(f"{_failures} FAILURE(S)")
        raise SystemExit(1)
    print("all HA-voice foundation tests passed")


if __name__ == "__main__":
    main()
