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
        text = queue.pop(0) if queue else ""
        # v0.80.0 — listen() returns (text, meta). The meta carries the reason an
        # empty turn was empty; five distinct failures used to collapse into the
        # same bare "" and be counted identically.
        return text, ({} if text else {"stt": "silence"})

    mod.listen = _listen
    # The ledger is captured, not written: these tests run with no /data.
    mod.ledger = []
    al = types.ModuleType("assistant_log")
    al.record = lambda outcome, **kw: (mod.ledger.append(dict(outcome=outcome, **kw)), True)[1]
    mod.assistant_log = al
    # No /run/switchboard/features.json in a test tree; default the policy on.
    mod.load_features = lambda: {}
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


# --------------------------------------------------------------------------- #
# v0.80.0 — the ledger. Before this, nothing the assistant did survived the call.
# --------------------------------------------------------------------------- #
def test_the_reply_is_recorded_not_just_the_question() -> None:
    """`reply_text(...)` used to be evaluated inline as a call argument and never
    bound to anything, so the half of "what the assistant heard and said" that
    tells you whether it ANSWERED CORRECTLY existed in no log, file or sensor."""
    mod, spoken, asked = _load(heard=["turn on the kitchen lights", "bye"],
                               converse=lambda t: ("Turned on 2 lights", "action_done"))
    mod.main()
    answered = [r for r in mod.ledger if r["outcome"] == "answered"]
    check("ledger: the turn was recorded", len(answered) == 1)
    check("ledger: it carries what the caller said",
          answered[0]["heard"] == "turn on the kitchen lights")
    check("ledger: and what the assistant replied",
          answered[0]["reply"] == "Turned on 2 lights")
    check("ledger: with the intent HA matched",
          answered[0]["response_type"] == "action_done")


def test_every_terminal_branch_is_distinguishable() -> None:
    """Two goodbyes used to be one event. The caller ringing off politely and the
    loop running out of turns both emitted the identical say_or("Goodbye.") and
    recorded nothing at all, so no log could tell a satisfied caller from one who
    gave up after five failed attempts."""
    mod, _, _ = _load(heard=["lights on", "goodbye"])
    mod.main()
    check("ledger: a caller who says goodbye is recorded as goodbye",
          [r for r in mod.ledger if r["outcome"] == "goodbye"])
    check("ledger: and not as an exhausted loop",
          not [r for r in mod.ledger if r["outcome"] == "ended-max-turns"])

    mod2, _, _ = _load(heard=["a", "b", "c", "d", "e"])
    mod2.main()
    check("ledger: five used turns end as ended-max-turns",
          [r for r in mod2.ledger if r["outcome"] == "ended-max-turns"])
    check("ledger: which records how many turns it cost the caller",
          [r for r in mod2.ledger
           if r.get("turns_used") == mod2.MAX_TURNS])

    mod3, _, _ = _load(heard=[])          # nothing heard at all
    mod3.main()
    check("ledger: two silent turns end as ended-unheard",
          [r for r in mod3.ledger if r["outcome"] == "ended-unheard"])


def test_an_empty_turn_records_WHY_it_was_empty() -> None:
    """Five distinct failures collapsed into the same empty string and were
    counted identically: a spawn error, a timeout, a whisper-server hang, an STT
    error, and genuine silence. A ledger that only said "empty" would reproduce
    exactly the blindness it exists to remove."""
    mod, _, _ = _load(heard=[])
    # The stub reports genuine silence; the AGI must carry the reason through.
    mod.main()
    empties = [r for r in mod.ledger if r["outcome"] == "no-speech"]
    check("ledger: the empty turn is recorded", empties)
    check("ledger: with a reason, not just a verdict",
          all(r.get("reason") for r in empties))
    check("ledger: and healthy silence is named as such",
          empties[0]["reason"] == "silence")


def test_rows_are_written_per_turn_not_buffered_to_the_end() -> None:
    """Asterisk sends SIGHUP on caller hangup, no AGI here installs a handler,
    and AGISIGHUP is never set in the dialplan — so this process is killed
    outright with no `finally` and no `atexit`. Anything held in memory for a
    flush at the end is lost on exactly the calls that ended badly.

    Simulated by making the SECOND turn raise: turn one's row must already exist.
    """
    mod, _, _ = _load(heard=["lights on", "and the fan"])

    calls = {"n": 0}
    real = mod.ha_client.converse

    def _boom(text, *a, **k):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("channel died mid-call")
        return real(text)
    mod.ha_client.converse = _boom
    try:
        mod.main()
    except RuntimeError:
        pass
    check("ledger: the first turn was already on disk when the call died",
          [r for r in mod.ledger
           if r["outcome"] == "answered" and r.get("turn") == 1])


def test_a_recording_that_ended_in_a_hangup_is_distinguishable() -> None:
    """Asterisk's RECORD FILE reply — `200 result=... (hangup) endpos=<samples>` —
    used to be discarded entirely. It carries the two facts most needed to explain
    an empty turn: how much audio was captured, and whether the caller stopped
    talking, hung up, or pressed a key."""
    mod, _, _ = _load(heard=[])
    r = mod._record_result("200 result=-1 (hangup) endpos=34400")
    check("record: the hangup reason is decoded", r["rec_end"] == "hangup")
    check("record: endpos becomes milliseconds at 8 kHz", r["rec_ms"] == 4300)
    t = mod._record_result("200 result=0 (timeout) endpos=64000")
    check("record: a timeout is not a hangup", t["rec_end"] == "timeout")
    check("record: a garbage reply degrades to nothing, not a crash",
          mod._record_result("") == {})


def test_the_real_listen_classifies_every_empty_turn() -> None:
    """★ Exercises listen() ITSELF, not the stub that stands in for it elsewhere.

    Every other test in this file replaces listen() wholesale, so they assert the
    stub's return value and say nothing about the function. A mutation that
    deleted the "silence" classification entirely survived the whole suite for
    exactly that reason: the stub was supplying the answer the test checked.

    Silence is the one empty turn that means the recogniser is HEALTHY. If it
    cannot be told apart from a spawn failure or a timeout, the ledger reproduces
    the blindness it was built to remove.
    """
    import subprocess as _sp
    import types
    # A FRESH module, not _load()'s — _load rebinds mod.listen to the stub, so
    # calling mod.listen() after it runs the stand-in and proves nothing about
    # the function. That is precisely how the mutant survived.
    mod = SourceFileLoader("sb_assistant_real", str(_AGI)).load_module()

    import os as _os
    import tempfile
    # A REAL directory with a REAL wav, so getsize() and unlink() run for real.
    # Patching mod.os.path.getsize would monkeypatch the global os module for
    # every other test in the process — the fake would outlive this function.
    tmpdir = tempfile.mkdtemp()
    mod.ASR_DIR = tmpdir

    rec_reply = {"v": '200 result=0 (timeout) endpos=34400'}

    def _agi(cmd):
        if cmd.startswith("RECORD"):
            # Asterisk writes the file; stand in for it so the size is real.
            name = cmd.split()[2] + ".wav"
            with open(name, "wb") as fh:
                fh.write(b"\0" * 68844)
            return rec_reply["v"]
        return ""
    mod.agi = _agi
    mod.stream = lambda sf: None

    class _Proc:
        def __init__(self, out="", err="", exc=None):
            self._o, self._e, self._exc = out, err, exc

        def communicate(self, timeout=None):
            if self._exc:
                raise self._exc
            return self._o, self._e

        def kill(self):
            pass

    def _popen(result):
        def _f(*a, **k):
            if isinstance(result, Exception):
                raise result
            return result
        return _f

    # 1. Genuine silence: the child ran fine and returned nothing.
    mod.subprocess = types.SimpleNamespace(
        Popen=_popen(_Proc(out="", err="")), PIPE=-1, DEVNULL=-3,
        TimeoutExpired=_sp.TimeoutExpired)
    text, meta = mod.listen("ask", 1)
    check("listen: nothing said and nothing wrong is 'silence'",
          text == "" and meta["stt"] == "silence")
    check("listen: the recording length survives (endpos/8 = ms)",
          meta["rec_ms"] == 4300)
    check("listen: and how the recording ended", meta["rec_end"] == "timeout")
    check("listen: the WAV size is measured BEFORE the unlink",
          meta["wav_bytes"] == 68844)
    check("listen: the inference is timed", "stt_ms" in meta)

    # 2. A spawn failure is NOT silence.
    mod.subprocess = types.SimpleNamespace(
        Popen=_popen(OSError("no such file")), PIPE=-1, DEVNULL=-3,
        TimeoutExpired=_sp.TimeoutExpired)
    _, meta = mod.listen("ask", 2)
    check("listen: a spawn failure is named, not called silence",
          meta["stt"] == "spawn-error")
    check("listen: and the OS error is kept", "no such file" in meta["stt_err"])

    # 3. Nor is a timeout — and whisper's own explanation is kept, which the
    #    timeout handler used to drop entirely.
    mod.subprocess = types.SimpleNamespace(
        Popen=_popen(_Proc(exc=_sp.TimeoutExpired("stt", 30))), PIPE=-1,
        DEVNULL=-3, TimeoutExpired=_sp.TimeoutExpired)
    _, meta = mod.listen("ask", 3)
    check("listen: a timeout is named", meta["stt"] == "timeout")

    # 4. A turn that DID produce speech carries no failure reason at all.
    mod.subprocess = types.SimpleNamespace(
        Popen=_popen(_Proc(out="turn on the lights\n", err="")), PIPE=-1,
        DEVNULL=-3, TimeoutExpired=_sp.TimeoutExpired)
    text, meta = mod.listen("ask", 4)
    check("listen: a heard turn returns the text", text == "turn on the lights")
    check("listen: and claims no fault", "stt" not in meta)

    # 5. A caller who hung up mid-recording is distinguishable from one who
    #    simply stopped talking — the whole reason the RECORD reply is decoded.
    rec_reply["v"] = '200 result=-1 (hangup) endpos=8000'
    mod.subprocess = types.SimpleNamespace(
        Popen=_popen(_Proc(out="", err="")), PIPE=-1, DEVNULL=-3,
        TimeoutExpired=_sp.TimeoutExpired)
    _, meta = mod.listen("ask", 5)
    check("listen: a hangup is not a timeout", meta["rec_end"] == "hangup")
    check("listen: the recording is deleted after transcription",
          not _os.listdir(tmpdir))
    import shutil as _sh
    _sh.rmtree(tmpdir, ignore_errors=True)
