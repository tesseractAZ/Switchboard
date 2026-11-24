"""Cancelling a wake-up when the phone line turns "cancel" into a sound-alike.

    python3 -m pytest switchboard/tests/test_wakeup_cancel.py

Every call reaches whisper as G.711 at 8 kHz, where most of an /s/ is gone, so
"cancel" comes back as a word that only sounds like it. Live, at the change
prompt ("To set a new time, say it now. To cancel, say cancel."):

  2026-08-21 05:58:35  'Campbell.'         -> nothing matched
  2026-08-21 05:58:51  'Campful.'          -> nothing matched; the 06:00 rang
  2026-11-23 06:29:45  "Okay, I'm full."   -> nothing matched
  2026-11-23 06:30:00  'Campful.'          -> nothing matched; the 06:30 rang

The caller said "cancel" each time. The prompt prime added after the first
morning was in place for the second. switchboard-stt's time_reply now answers
CANCEL-LIKE for a reply shaped like "cancel", and the wake-up AGI acts on it
only when a wake-up is set, which is when cancel was just offered.

A false cancel is the expensive mistake: the wake-up is deleted and the caller
oversleeps. Most of this file is about what must NOT cancel.
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import types
import wave
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHARE = ROOT / "rootfs" / "usr" / "share" / "switchboard"
STT = ROOT / "rootfs" / "usr" / "bin" / "switchboard-stt"
AGI = ROOT / "rootfs" / "var" / "lib" / "asterisk" / "agi-bin" / "switchboard-wakeup.agi"


def _load_stt():
    """switchboard-stt imports `match`/`lights_match` from operator/; restore
    sys.path afterwards (conftest's tripwire)."""
    saved = list(sys.path)
    sys.path.insert(0, str(SHARE / "operator"))
    try:
        return SourceFileLoader("stt_wakeup_cancel", str(STT)).load_module()
    finally:
        sys.path[:] = saved


stt = _load_stt()
# The REAL parser: the guarantee "a time is never a cancel" is only as good as
# the parser it is checked against.
tp = SourceFileLoader("timeparse_wakeup_cancel", str(SHARE / "wakeup" / "timeparse.py")).load_module()


def reply(text):
    return stt.time_reply(text, tp.parse)


# --------------------------------------------------------------------------- #
# 1. What the line did to "cancel".
# --------------------------------------------------------------------------- #

LIVE = ["Campbell.", "Campful.", "Okay, I'm full."]


@pytest.mark.parametrize("heard", LIVE)
def test_the_live_mishearings_are_sound_alikes(heard):
    assert reply(heard) == ("CANCEL-LIKE", "cancel-like")


@pytest.mark.parametrize("heard", [
    "O.K., I'm full.", "OK, I'm full.", "Okay, I’m full.", "okay im full",
    "Campbell, Campbell.", "Okay, Campbell.", "Campful, please.",
])
def test_the_same_mishearings_written_differently(heard):
    assert reply(heard) == ("CANCEL-LIKE", "cancel-like")


# What synthetic voices saying "cancel" produced through the same channel (8 kHz
# G.711 u-law) and the add-on's own recogniser (whisper.cpp base.en, the live
# prompt), 2026-11-23. Each is a reply a caller would have given in full.
SYNTHETIC = [
    "Pencil.", "Pencil, please.", "Pencil the wake up.", "Cancellate.",
]


@pytest.mark.parametrize("heard", SYNTHETIC)
def test_what_the_channel_did_to_synthetic_voices(heard):
    assert reply(heard) == ("CANCEL-LIKE", "cancel-like")


@pytest.mark.parametrize("heard", [
    "Canful.", "Kansel.", "Can sell.", "Camp full.", "Camp bell.", "Can fall.",
    "Council.", "Counsel.", "Canceled.", "Cancelled.", "Cancels.", "Cancelling.",
    "Okay, canceled.", "Cancellation, please.",
])
def test_the_shape_generalises_a_little(heard):
    assert reply(heard) == ("CANCEL-LIKE", "cancel-like")


# --------------------------------------------------------------------------- #
# 2. What must never become a cancel.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("heard", [
    # English words near "cancel" that the shape is built to refuse.
    "Candle.", "Castle.", "Camel.", "Canal.", "Channel.", "Cancer.", "Kansas.",
    "Campus.", "Capful.", "Careful.", "Handful.", "Gamble.", "Cannibal.",
    "Kennel.", "Chancel.", "Compel.", "Hansel.", "Tinsel.", "Dental.", "Kendall.",
    "Damsel.", "Console.", "Casual.", "Just casual.", "Castle the wake up!",
    # What is left of the word, alone, is not the word.
    "Full.", "Fell.", "Bell.", "Fill.", "Call.", "Can call.", "I'm full.", "Cool.",
    # A sound-alike inside a longer reply is not the whole reply.
    "Can sell tomorrow.", "Pencil sharpener.", "Council meeting.", "Campbell soup.",
    "Okay, I'm full now.", "I'm full, okay.", "Okay, I'm cool.",
    # Real replies that are not cancel.
    "Okay, I'm fine.", "Okay, I'm up.", "Okay, I'm awake.", "Okay, I'm well.",
    "Keep it.", "Leave it.", "No.", "Yes.", "Okay.", "Thank you.",
    "Thank you for watching.", "Goodbye.", "Hello?", "What time is it?", "Snooze.",
    "You", "Hmm.", "...", "", "God bless you.", "Is it cancelled?",
    "Is it canceled now?",
])
def test_near_words_and_ordinary_replies_do_nothing(heard):
    assert reply(heard) == ("", "none"), heard


@pytest.mark.parametrize("heard", [
    "Don't cancel.", "No, don't cancel it.", "Do not cancel.",
    "I don't want to cancel.", "Never mind, keep it.", "Cancel? No, keep it.",
    "Don’t cancel.", "Not cancel.", "Leave it, don't clear it.",
])
def test_a_negated_cancel_is_only_a_retry(heard):
    assert reply(heard) == ("", "negated"), heard


@pytest.mark.parametrize("heard", [
    "No, cancel it.", "Say cancel.", "Okay, say cancel.", "I want to clear it",
    "Or say cancel.", "Cancel, cancel.", "Cancel my wake-up call.",
])
def test_real_cancels_that_look_like_vetoes_still_cancel(heard):
    assert reply(heard) == ("CANCEL", "cancel"), heard


def test_the_literal_family_is_unchanged():
    """The words the flow has always accepted, said alone, still cancel at once,
    with the same regex."""
    assert stt._CANCEL_WORDS.pattern == \
        r"\b(cancel|clear|remove|delete|never mind|nevermind|stop|forget)\b"
    for w in ("Cancel.", "Clear.", "Remove.", "Delete.", "Never mind.",
              "Nevermind.", "Stop.", "Forget it.", "Okay, cancel."):
        assert reply(w) == ("CANCEL", "cancel"), w


def test_a_prompt_echo_does_nothing():
    """whisper can hand its prompt back on a near-silent recording. Read as a
    reply, the full prompt parses as 18:53 and its tail as a cancel."""
    assert tp.parse(stt._TIME_PROMPT) == "18:53"          # what used to happen
    assert reply(stt._TIME_PROMPT) == ("", "echo")
    assert reply("Or say cancel to clear it.") == ("", "echo")
    assert reply("A time of day, for example, six thirty.") == ("", "echo")
    words = " ".join(stt._reply_tokens(stt._TIME_PROMPT))
    for framing in stt._PROMPT_FRAMING:
        assert framing in words, framing    # edit the prompt, update the veto


# --------------------------------------------------------------------------- #
# 3. A time is always a time.
# --------------------------------------------------------------------------- #

_HOURS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
          "ten", "eleven", "twelve"]
_MINUTES = ["", "oh five", "ten", "fifteen", "twenty", "twenty five", "thirty",
            "thirty five", "forty", "forty five", "fifty", "fifty five"]
_LEADS = ["", "um ", "okay ", "okay, ", "make it ", "set it for ", "wake me at ",
          "so ", "uh, "]
_TAILS = ["", " a.m.", " p.m.", " am", " in the morning", ".", "!"]


def _spoken_times():
    for lead in _LEADS:
        for h in _HOURS:
            for m in _MINUTES:
                for tail in _TAILS:
                    yield f"{lead}{h} {m}".strip() + tail
        for h in _HOURS:
            for rel in ("quarter past", "half past", "quarter to", "ten past",
                        "twenty to"):
                yield f"{lead}{rel} {h}"
    for h in range(1, 13):
        for m in range(0, 60, 5):
            yield f"{h}:{m:02d}"
            yield f"{h}:{m:02d} a.m."
            yield f"{h}.{m:02d}."


# Live and synthetic renderings of spoken times, as whisper wrote them.
WRITTEN_TIMES = [
    "Six thirty-five, eight, seven.", "Five forty a.m.", "Six a.m.",
    "Six twenty a.m.", "Six forty a.m.", "eight a.m.", "Six fifteen a.m.",
    "Six twenty five a.m.", "Six thirty a.m.", "six thirty.", "6.15 a.m.",
    "6. 25 a.m.", "5.45.", ", six thirty.", "Quarter past seven.",
    "Half past six.", "eight o'clock", "Eight o'clock.", "seven a.m.",
    "noon", "midnight", "oh seven hundred", "zero seven thirty",
    "zero zero thirty", "seven oh five", "nineteen thirty",
]


def test_no_spoken_time_is_ever_touched():
    n = 0
    for phrase in list(_spoken_times()) + WRITTEN_TIMES:
        hhmm = tp.parse(phrase)
        got = reply(phrase)
        n += 1
        if hhmm:
            assert got == (hhmm, "time"), phrase
        else:
            assert got[0] not in ("CANCEL", "CANCEL-LIKE"), phrase
            assert got[1] != "echo", phrase
    assert n > 10_000, n


# --------------------------------------------------------------------------- #
# 4. A lone zero is not midnight.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("heard", ["Oh.", "Oh!", "O.", "oh", "zero", "Zero.", "0.", "0", "00"])
def test_a_lone_zero_sets_nothing(heard):
    """whisper writes a breath or a clipped word as "Oh." Read as the hour 0, it
    REPLACED a set wake-up with midnight; at the change prompt that is a lost
    alarm, as costly as a false cancel."""
    assert tp.parse(heard) is None
    assert reply(heard) == ("", "none")


@pytest.mark.parametrize("heard, hhmm", [
    ("oh seven hundred", "07:00"), ("zero seven thirty", "07:30"),
    ("oh five", "05:00"), ("seven oh five", "07:05"), ("zero zero thirty", "00:30"),
    ("midnight", "00:00"), ("twelve am", "00:00"), ("0:30", "00:30"),
])
def test_zero_inside_a_time_still_reads(heard, hhmm):
    assert tp.parse(heard) == hhmm


# --------------------------------------------------------------------------- #
# 5. The wake-up AGI: a sound-alike cancels only where cancel was offered, and a
#    call that understood nothing says the wake-up is still set.
# --------------------------------------------------------------------------- #

class _Call:
    """The real AGI on the real store and ledger, with its channel stubbed."""

    def __init__(self, tmp_path):
        self.store = SourceFileLoader("store_wakeup_cancel",
                                      str(SHARE / "wakeup" / "store.py")).load_module()
        self.store.PATH = str(tmp_path / "wakeups.json")
        self.delivery = SourceFileLoader("delivery_wakeup_cancel",
                                         str(SHARE / "webui" / "delivery.py")).load_module()
        self.delivery.OUTCOME_PATH = str(tmp_path / "delivery-outcomes.jsonl")
        saved_mods = {k: sys.modules.get(k) for k in ("store", "delivery")}
        saved_path = list(sys.path)
        sys.modules["store"] = self.store
        sys.modules["delivery"] = self.delivery
        try:
            self.agi = SourceFileLoader("wakeup_agi_cancel", str(AGI)).load_module()
        finally:
            sys.path[:] = saved_path
            for k, v in saved_mods.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        assert self.agi.store is self.store and self.agi.delivery is self.delivery
        self.sent = []

    def dial(self, ext, heard):
        """`ext` dials the wake-up code; the recogniser answers each attempt
        with the next item of `heard`. Returns how many attempts ran."""
        answers = iter(heard)
        attempts = []
        self.sent = []

        def recognize(attempt):
            attempts.append(attempt)
            return next(answers)

        def agi(cmd):
            self.sent.append(cmd)
            return "200 result=0"

        self.agi.read_env = lambda: {"agi_channel": f"PJSIP/{ext}-0000000d"}
        self.agi.agi = agi
        self.agi.recognize = recognize
        self.agi.main()
        return len(attempts)

    def var(self, name):
        vals = [m.group(1) for c in self.sent
                for m in [re.fullmatch(rf'SET VARIABLE {name} "(.*)"', c)] if m]
        return vals[-1] if vals else None

    def rows(self):
        p = Path(self.delivery.OUTCOME_PATH)
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] \
            if p.exists() else []


@pytest.fixture
def call(tmp_path):
    return _Call(tmp_path)


def test_the_morning_of_2026_11_23_now_cancels(call):
    """ext 19 had 06:30 set and said "cancel"; the line made it a sound-alike."""
    call.store.set_wakeup("19", "06:30")
    assert call.dial("19", ["CANCEL-LIKE"]) == 1
    assert call.store.get("19") is None
    assert call.var("WAKEUP_RESULT") == "cancelled"
    [row] = call.rows()
    assert (row["outcome"], row["source"], row["removed"]) == ("cancelled", "phone", True)


def test_a_sound_alike_with_nothing_set_is_only_a_miss(call):
    """With no wake-up, the caller was asked for a TIME, not offered cancel."""
    assert call.dial("19", ["CANCEL-LIKE", "CANCEL-LIKE"]) == 2
    assert call.var("WAKEUP_RESULT") == "none"
    assert call.rows() == [] and call.store.get("19") is None


def test_a_sound_alike_then_a_time_with_nothing_set_sets_the_time(call):
    assert call.dial("19", ["CANCEL-LIKE", "06:45"]) == 2
    assert call.var("WAKEUP_RESULT") == "set"
    assert call.store.get("19")["hhmm"] == "06:45"


def test_two_misses_with_a_wake_up_set_say_it_is_still_set(call):
    call.store.set_wakeup("19", "06:30")
    epoch = call.store.get("19")["target_epoch"]
    assert call.dial("19", ["", ""]) == 2
    assert call.var("WAKEUP_RESULT") == "kept"
    assert call.var("WAKEUP_EPOCH") == str(epoch)
    assert call.store.get("19")["target_epoch"] == epoch      # untouched
    assert call.rows() == []                                  # nothing changed
    # It was offered change-or-cancel both times, as before.
    streamed = [c for c in call.sent if c.startswith("STREAM FILE")]
    assert [s.split()[2].rsplit("/", 1)[-1] for s in streamed] == [
        "sw-wakeup-exists", "sw-wakeup-change", "sw-wakeup-didnt-catch", "sw-wakeup-change"]


def test_kept_never_claims_a_wake_up_that_is_gone(call, monkeypatch):
    call.store.set_wakeup("19", "06:30")
    real_get = call.store.get
    reads = []

    def vanish(ext):
        reads.append(ext)
        return real_get(ext) if len(reads) == 1 else None
    monkeypatch.setattr(call.store, "get", vanish)
    call.dial("19", ["", ""])
    assert call.var("WAKEUP_RESULT") == "none" and len(reads) == 2


@pytest.mark.parametrize("bad", [OSError("disk"), {"target_epoch": "x"},
                                 {"target_epoch": True}, {"target_epoch": 0}, []])
def test_kept_with_an_unreadable_or_odd_entry_is_none(call, monkeypatch, bad):
    call.store.set_wakeup("19", "06:30")
    real_get = call.store.get
    reads = []

    def odd(ext):
        reads.append(ext)
        if len(reads) == 1:
            return real_get(ext)
        if isinstance(bad, Exception):
            raise bad
        return bad
    monkeypatch.setattr(call.store, "get", odd)
    call.dial("19", ["", ""])
    assert call.var("WAKEUP_RESULT") == "none"


def test_the_literal_word_and_a_time_behave_as_before(call):
    call.store.set_wakeup("19", "06:30")
    assert call.dial("19", ["CANCEL"]) == 1
    assert call.var("WAKEUP_RESULT") == "cancelled" and call.store.get("19") is None
    assert call.dial("19", ["07:15"]) == 1
    assert call.var("WAKEUP_RESULT") == "set" and call.store.get("19")["hhmm"] == "07:15"
    # A literal CANCEL with nothing set still answers "cancelled", as it always has.
    call.store.cancel("19")
    assert call.dial("19", ["CANCEL"]) == 1 and call.var("WAKEUP_RESULT") == "cancelled"


def test_an_unknown_answer_is_a_miss_not_a_cancel(call):
    """Anything that is not exactly CANCEL, CANCEL-LIKE or HH:MM changes nothing."""
    call.store.set_wakeup("19", "06:30")
    assert call.dial("19", ["cancel", "CANCEL-LIKE?"]) == 2
    assert call.var("WAKEUP_RESULT") == "kept" and call.store.get("19") is not None


def test_the_recording_leaves_a_breadcrumb_without_words(call, monkeypatch, capsys):
    """recognize() itself: the RECORD reply and the file's length reach stderr,
    the recognised words do not."""
    tmp = tempfile.mkdtemp()
    try:
        monkeypatch.setattr(call.agi, "ASR_DIR", tmp)

        def agi(cmd):
            m = re.match(r"RECORD FILE (\S+) wav", cmd)
            if m:
                with wave.open(m.group(1) + ".wav", "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(8000)
                    w.writeframes(b"\0\0" * 8160)       # the 1.02 s silence remainder
                return "200 result=0 (timeout) endpos=8160"
            return "200 result=0"

        class _Proc:
            def communicate(self, timeout=None):
                return ("CANCEL-LIKE\n",
                        "[switchboard-stt] heard=<8 chars> -> CANCEL-LIKE\n")

        monkeypatch.setattr(call.agi, "agi", agi)
        monkeypatch.setattr(call.agi.subprocess, "Popen", lambda *a, **k: _Proc())
        capsys.readouterr()
        assert call.agi.recognize(0) == "CANCEL-LIKE"
        err = capsys.readouterr().err
        assert "[wakeup] rec attempt=0 bytes=16364 secs=1.02 end=timeout no-speech" in err
        assert "campful" not in err.lower()
        assert not os.listdir(tmp)                       # the WAV is still deleted
    finally:
        for f in os.listdir(tmp):
            os.unlink(os.path.join(tmp, f))
        os.rmdir(tmp)


def test_the_agi_decision_log_holds_no_words(call, capsys):
    call.store.set_wakeup("19", "06:30")
    capsys.readouterr()
    call.dial("19", ["CANCEL-LIKE"])
    err = capsys.readouterr().err
    assert "[wakeup] a sound-alike of cancel, with a wake-up set: cancelling" in err


# --------------------------------------------------------------------------- #
# 6. switchboard-stt's own log line for each new outcome follows the policy.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("heard, outcome, stdout", [
    ("Campful.", "-> CANCEL-LIKE", "CANCEL-LIKE"),
    ("I don't want to cancel the zebra.", "-> ignored (negated cancel)", ""),
    ("Or say cancel to clear it, zebra.", "-> ignored (prompt echo)", ""),
])
@pytest.mark.parametrize("policy", [True, False])
def test_the_new_outcomes_log_words_only_when_allowed(tmp_path, monkeypatch, capsys,
                                                     heard, outcome, stdout, policy):
    feats = tmp_path / "features.json"
    feats.write_text(json.dumps({"assistant": {"transcripts": policy}}))
    monkeypatch.setattr(stt, "FEATURES", str(feats))
    monkeypatch.setattr(stt, "_recording_to_text", lambda infile, prompt: heard)
    monkeypatch.setattr(sys, "argv", ["switchboard-stt", "--in", "r.wav", "--mode", "time"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "timeparse", types.SimpleNamespace(parse=tp.parse))
    capsys.readouterr()
    assert stt.main() == 0
    got = capsys.readouterr()
    assert outcome in got.err and got.out.strip() == stdout
    if policy:
        assert repr(heard) in got.err
    else:
        assert f"<{len(heard)} chars>" in got.err
        assert "zebra" not in got.err and "campful" not in got.err.lower()


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
