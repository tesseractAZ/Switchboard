"""The local voice assistant AGI: goodbye detection, bias, and the turn loop.

    python3 -m pytest switchboard/tests/test_assistant_agi.py

The point of this feature is that a phone can reach Home Assistant's intent
matcher with NOTHING on the path leaving the Pi -- this add-on's whisper, this
add-on's piper, Home Assistant's built-in agent. The dialplan wiring is asserted
in test_switchboard_config.py; THIS file asserts the AGI's own behaviour, which
a source scan cannot: that a command containing a goodbye word is not mistaken
for a hangup, that the turn loop is bounded, and that an unreachable Home
Assistant produces a spoken apology rather than dead air on the line.
"""
import sys
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_AGI = _ROOT / "rootfs" / "var" / "lib" / "asterisk" / "agi-bin" / "switchboard-assistant.agi"

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    assert cond, name


def _load(*, available=True, converse=None, by_area=None, heard=None):
    """Load the AGI with ha_client stubbed and its channel I/O captured.

    Returns (module, spoken, asked) where `spoken` is every line handed to say()
    and `asked` is every transcript sent to the conversation agent."""
    spoken: list[str] = []
    asked: list[str] = []

    hc = types.ModuleType("ha_client")
    hc.available = lambda: available
    hc.lights_by_area = lambda: (by_area if by_area is not None else {})

    def _converse(text, *a, **k):
        asked.append(text)
        return (converse or (lambda t: ("ok", "action_done")))(text)

    hc.converse = _converse
    sys.modules["ha_client"] = hc

    mod = SourceFileLoader("sb_assistant", str(_AGI)).load_module()

    # Replace channel I/O: no Asterisk, no subprocesses, no /run/switchboard.
    mod.read_env = lambda: {}
    mod.agi = lambda cmd: ""
    mod.stream = lambda sf: spoken.append(f"<canned:{sf}>")
    mod.say = lambda text: (spoken.append(text), True)[1]
    queue = list(heard or [])
    # `listened` counts RECORDINGS, which is what actually costs the caller time
    # and holds the channel -- an off-by-one in the empty-turn threshold shows up
    # here and nowhere else.
    mod.listened = []

    def _listen(tag, attempt, bias=""):
        mod.listened.append((tag, attempt))
        return queue.pop(0) if queue else ""

    mod.listen = _listen
    return mod, spoken, asked


# (utterance, ends the call?) -- the whole contract of is_goodbye in one table.
# The FALSE half is the important half: every one of those is a real command that
# contains a terminator word, and a substring test would hang up on all of them.
GOODBYE_CASES = [
    ("Goodbye.", True),
    ("bye", True),
    ("That's all, thanks.", True),
    ("That's all.", True),
    ("never mind", True),
    ("okay, never mind", True),
    ("thank you", True),
    ("I'm done", True),
    ("thanks a lot", True),
    ("stop", True),
    ("cancel", True),
    ("no thanks", True),
    ("all done", True),
    ("turn off the porch light", False),
    ("stop the music", False),
    ("cancel my seven a m wake up", False),
    ("turn on the kitchen light thanks", False),
    ("never turn on that light", False),
    ("what is the temperature", False),
    ("okay", False),          # filler with no terminator is not a goodbye
    ("", False),              # silence is handled by the empty-turn path, not here
]


def test_a_command_containing_a_goodbye_word_is_not_a_hangup():
    """"stop the music" and "cancel my wake up" both CONTAIN a terminator word.
    A substring test would hang up on them -- ordinary commands would end the
    call. is_goodbye requires the whole utterance to be terminators + filler."""
    mod, _, _ = _load()
    for text, want in GOODBYE_CASES:
        verb = "ends" if want else "does NOT end"
        check(f"{text!r} {verb} the call", mod.is_goodbye(text) is want)


def test_bias_carries_live_names_and_survives_an_unreachable_ha():
    """The bias is a decoding PRIOR. Live names improve short commands; when HA
    is down the verb list alone must still be handed to whisper."""
    mod, _, _ = _load()
    bias = mod.build_bias({"Kitchen": [{"name": "Sink Light"}],
                           "": [{"name": "Sink Light"}, {"name": "Porch"}]})
    check("area name is in the bias", "Kitchen" in bias)
    check("light name is in the bias", "Sink Light" in bias)
    check("verbs are always in the bias", "turn off" in bias)
    check("duplicate names appear once", bias.count("Sink Light") == 1)
    check("the empty area name is dropped", ", ," not in bias)
    check("no areas still yields the verb list", mod.build_bias({}) == mod.BIAS_VERBS)
    check("None yields the verb list", mod.build_bias(None) == mod.BIAS_VERBS)


def test_ha_speech_is_spoken_even_when_the_response_type_is_an_error():
    """HA answers an unmatched command with a perfectly good sentence and
    response_type 'error'. Speaking a generic fallback instead would throw away
    the only useful thing the agent said -- including the message that tells
    Eric no entities are exposed yet."""
    mod, _, _ = _load()
    check("speech wins over an error response_type",
          mod.reply_text("I am not aware of any device called porch", "error")
          == "I am not aware of any device called porch")
    check("a typed reply with no speech gets a spoken fallback",
          mod.reply_text("", "error") == "Sorry, I didn't understand that.")
    check("a transport failure is reported as unreachable",
          mod.reply_text(None, None) == "Sorry, I couldn't reach Home Assistant.")


def test_an_unreachable_home_assistant_apologizes_instead_of_dead_air():
    """A silent channel is the worst outcome on a phone: the caller has no way to
    tell a broken feature from a slow one."""
    mod, spoken, asked = _load(available=False, heard=["turn on the porch light"])
    mod.main()
    check("nothing was asked of a down HA", asked == [])
    check("the caller heard an apology",
          any("unavailable" in s.lower() for s in spoken))


def test_the_turn_loop_is_bounded_and_speaks_every_answer():
    mod, spoken, asked = _load(
        converse=lambda t: (f"Done: {t}", "action_done"),
        heard=["turn on the porch light", "turn off the porch light", "goodbye"])
    mod.main()
    check("both commands reached the conversation agent",
          asked == ["turn on the porch light", "turn off the porch light"])
    check("both answers were spoken",
          "Done: turn on the porch light" in spoken
          and "Done: turn off the porch light" in spoken)
    check("the goodbye ended the call", spoken[-1] == "Goodbye.")


def test_a_line_that_never_stops_talking_cannot_hold_the_channel_forever():
    """MAX_TURNS is the backstop: an open mic (or a phone left off-hook next to a
    television) must not keep an Asterisk channel and a whisper slot alive."""
    mod, spoken, asked = _load(heard=["hello"] * 50)
    mod.main()
    check("the loop stopped at MAX_TURNS", len(asked) == mod.MAX_TURNS)
    check("MAX_TURNS bounds RECORDINGS, not just answered commands",
          len(mod.listened) == mod.MAX_TURNS)
    check("the call ended with a goodbye", spoken[-1] == "Goodbye.")


def test_two_silent_turns_end_the_call_but_one_re_prompts():
    """Rotary handsets and hard-of-hearing callers miss the beep. One retry is
    courteous; retrying forever is a stuck channel."""
    mod, spoken, asked = _load(heard=["", "turn on the porch light", "", ""])
    mod.main()
    check("the single silence re-prompted rather than hanging up",
          asked == ["turn on the porch light"])
    check("the caller was asked to repeat",
          any("say that again" in s.lower() for s in spoken))
    check("two consecutive silences ended the call",
          any("didn't catch that" in s.lower() for s in spoken))


def test_the_silent_turn_threshold_is_exactly_two():
    """Off-by-one here is invisible in normal use and costly on a real call: one
    extra beep-and-wait cycle is ~11 s of a caller holding a dead handset."""
    mod, spoken, asked = _load(heard=["", ""])
    mod.main()
    check("exactly two recordings were taken, not three",
          len(mod.listened) == 2)
    check("nothing was sent to the conversation agent", asked == [])
    check("the call ended on the second silence",
          any("didn't catch that" in s.lower() for s in spoken))


def test_a_successful_turn_resets_the_silence_counter():
    """Silences must be CONSECUTIVE. Counting them cumulatively would hang up on
    a caller in a noisy room partway through a working conversation -- the exact
    situation the retry exists for."""
    # Five recordings, because MAX_TURNS bounds RECORDINGS rather than successful
    # commands -- a silent turn costs the channel just as much as a spoken one.
    mod, spoken, asked = _load(
        converse=lambda t: (f"Done: {t}", "action_done"),
        heard=["", "one", "", "two", "goodbye"])
    mod.main()
    check("both commands got through despite a silence before each",
          asked == ["one", "two"])
    check("the caller was never told the call was being given up on",
          not any("didn't catch that" in s.lower() for s in spoken))
    check("the call ended on the spoken goodbye", spoken[-1] == "Goodbye.")


def test_everything_on_the_path_is_local():
    """The whole reason this exists: an internet outage must not disturb it."""
    src = _AGI.read_text()
    check("the default agent is HA's built-in matcher (ha_client.converse default)",
          "conversation.home_assistant" not in src or "ha_client.converse(text)" in src)
    check("STT is the add-on's own binary", 'STT = "/usr/bin/switchboard-stt"' in src)
    check("TTS is the add-on's own binary", 'TTS = "/usr/bin/switchboard-tts"' in src)
    check("no cloud stt entity is referenced", "home_assistant_cloud" not in src)


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print(f"\n{_failures} failure(s)")
    sys.exit(1 if _failures else 0)
