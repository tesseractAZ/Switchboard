"""The assistant ledger: what it keeps, what it withholds, and what it admits.

    python3 -m pytest switchboard/tests/test_assistant_log.py

Before v0.80.0 the voice assistant recorded nothing that survived the call. Its
diagnostics went to a bare `sys.stderr.write`, and AGI stderr never reaches
Asterisk's logger — it lands in the RAM-backed container log and evaporates.
Verified on the running system: zero `[assistant]` lines in either durable log.
The reply was worse off than the transcript: `reply_text(...)` was evaluated
inline as a call argument and never bound, so the half of "what it heard and
said" that tells you whether it answered CORRECTLY existed nowhere at all.
"""
import json
import os
import shutil
import stat
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_MOD = (Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share"
        / "switchboard" / "webui" / "assistant_log.py")
al = SourceFileLoader("assistant_log", str(_MOD)).load_module()


@pytest.fixture
def ledger(tmp_path):
    real = al.PATH
    al.PATH = str(tmp_path / "assistant.jsonl")
    yield al
    al.PATH = real


def _rows(mod):
    return [json.loads(l) for l in
            open(mod.PATH, encoding="utf-8").read().splitlines() if l.strip()]


def test_a_turn_is_recorded_with_both_halves(ledger):
    """"What it heard AND what it said." Only the first half ever existed."""
    assert ledger.record("answered", turn=1, ext="19",
                         heard="turn on the kitchen lights",
                         reply="Turned on 2 lights",
                         converse_ms=410, stt_ms=1840) is True
    r = _rows(ledger)[0]
    assert r["heard"] == "turn on the kitchen lights"
    assert r["reply"] == "Turned on 2 lights", "the reply is the half that was missing"
    assert r["outcome"] == "answered" and r["turn"] == 1
    assert r["v"] == al.SCHEMA, "a reader must be told the shape, not infer it"
    assert isinstance(r["ts"], int)


def test_absent_optionals_are_omitted_not_nulled(ledger):
    """Same convention as delivery.record() and switchboard-callqos: a reader
    must be able to tell "not applicable" from "measured as nothing"."""
    ledger.record("no-speech", turn=2, reason="silence", converse_ms=None)
    r = _rows(ledger)[0]
    assert "converse_ms" not in r
    assert r["reason"] == "silence"


# --------------------------------------------------------------------------- #
# The transcript policy.
# --------------------------------------------------------------------------- #
def test_transcripts_off_withholds_words_but_keeps_every_diagnostic(ledger):
    """★ The switch must cost privacy, not observability.

    A privacy control that also blinds the operator is one that gets left on, and
    then the feature it protects is the feature nobody can debug. Everything that
    answers "is the assistant working?" survives; only the words go.
    """
    ledger.record("answered", transcripts=False, turn=1, ext="19",
                  heard="turn on the kitchen lights", reply="Turned on 2 lights",
                  stt_ms=1840, converse_ms=410, tts_ms=900, rec_ms=4300,
                  rec_end="timeout", wav_bytes=68844, response_type="action_done")
    r = _rows(ledger)[0]
    assert "heard" not in r and "reply" not in r, "words must not be stored"
    # ...but the SHAPE of what was said is still knowable.
    assert r["heard_chars"] == len("turn on the kitchen lights")
    assert r["reply_chars"] == len("Turned on 2 lights")
    for k in ("stt_ms", "converse_ms", "tts_ms", "rec_ms", "rec_end",
              "wav_bytes", "response_type", "outcome", "turn", "ext"):
        assert k in r, f"{k} is a diagnostic, not speech — it must survive"


def test_transcripts_on_is_the_default(ledger):
    ledger.record("answered", turn=1, heard="hello", reply="hi")
    r = _rows(ledger)[0]
    assert r["heard"] == "hello" and r["reply"] == "hi"


def test_stored_speech_is_bounded(ledger):
    """A transcript is a command, not a monologue. A runaway recogniser result
    must not be able to grow this file without limit."""
    long = "x" * 5000
    ledger.record("answered", turn=1, heard=long, reply=long)
    r = _rows(ledger)[0]
    assert len(r["heard"]) == al.MAX_SPEECH_CHARS
    assert len(r["reply"]) == al.MAX_SPEECH_CHARS


def test_every_speech_field_is_covered_by_the_policy(ledger):
    """SPEECH_FIELDS is the allowlist the redaction walks. If a field carrying
    words is added to a record but not to that tuple, it would be written
    verbatim with transcripts OFF — a silent policy hole."""
    for f in al.SPEECH_FIELDS:
        ledger.record("answered", transcripts=False, **{f: "some words"})
        r = _rows(ledger)[-1]
        assert f not in r, f"{f} is in SPEECH_FIELDS but survived redaction"
        assert r[f + "_chars"] == 10


# --------------------------------------------------------------------------- #
# Durability and honesty.
# --------------------------------------------------------------------------- #
def test_the_ledger_is_capped_and_keeps_the_newest(ledger):
    for i in range(al.MAX_RECORDS + 25):
        ledger.record("answered", turn=i)
    rows = _rows(ledger)
    assert len(rows) == al.MAX_RECORDS
    assert rows[-1]["turn"] == al.MAX_RECORDS + 24, "the newest turn must survive"
    assert rows[0]["turn"] == 25, "and the oldest must be the ones dropped"


def test_the_file_is_private_to_its_writer(ledger):
    """0600, not the 0664 the shared ledgers use. This is the only file in the
    system that can contain the words people say in their own home, it has
    exactly one writer, and nothing else needs to read it."""
    ledger.record("answered", turn=1, heard="hello")
    mode = stat.S_IMODE(os.stat(ledger.PATH).st_mode)
    assert mode == 0o600, f"mode is {oct(mode)}"


def test_a_failed_write_says_so(ledger):
    """delivery.record() returned None, swallowed an EACCES on every call, and
    made a shipped feature inert AND invisible for a whole release. Best-effort
    must not mean indistinguishable from success."""
    real = ledger.PATH
    try:
        ledger.PATH = "/proc/cannot/write/here/assistant.jsonl"
        assert ledger.record("answered", turn=1) is False
    finally:
        ledger.PATH = real


def test_telemetry_failure_never_raises(ledger):
    """It runs inside a live phone call. Nothing here may propagate."""
    real = ledger.PATH
    try:
        ledger.PATH = "/proc/cannot/write/here/assistant.jsonl"
        ledger.record("answered", turn=1, heard="hello")   # must not raise
    finally:
        ledger.PATH = real


def test_the_ledger_never_names_a_share_path():
    """Enforced again here, next to the module, because the consequence of
    getting it wrong is publishing household speech: /share is host-mounted and
    world-readable by design, /data is unreachable from outside the container."""
    src = _MOD.read_text()
    assert al.PATH.startswith("/data/") or "SWITCHBOARD_ASSISTANT_LOG" in os.environ
    import re
    assert not re.findall(r'["\']/share/[^"\']*["\']', src)


def test_the_boot_sweep_does_not_reopen_the_speech_ledger(tmp_path):
    """★ The mode is set correctly and was being undone at boot.

    `ensure_state_dir()` runs as root on every start and used to chmod EVERY file
    in /data/state to 0664 — correct for the stores a root service and an
    asterisk-user AGI genuinely share, wrong for this one. So `record()` created
    the ledger 0600, the next restart reopened it to the group, and
    `test_the_file_is_private_to_its_writer` above passed the whole time, because
    it only ever saw the file its own call had just written.

    Observed on the running system: `-rw-rw-r--` on
    `/data/state/assistant.jsonl`, hours after a release whose tests asserted
    0600. A test that agrees with the code and disagrees with production is worse
    than no test — it is evidence pointing the wrong way.

    This RUNS the function rather than reading it. A first version asserted only
    that the allowlist existed and what was in it, and a mutant that restored the
    blanket sweep — leaving the allowlist untouched and simply not consulting it
    — survived the whole suite.
    """
    import os
    import stat
    from importlib.machinery import SourceFileLoader
    from pathlib import Path
    sbc = SourceFileLoader("switchboard_config", str(
        Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "bin"
        / "switchboard-config")).load_module()

    d = tmp_path / "state"
    d.mkdir()
    # The .lock siblings matter as much as the stores. callqos.jsonl.lock is
    # flock'd by a root service AND an asterisk-user AGI, so it needs the same
    # group access its store does; assistant.jsonl.lock has one writer and does
    # not. The sweep decides by stripping the suffix, and a mutant that stopped
    # stripping survived a fixture that had no lock files in it.
    for name in ("assistant.jsonl", "callqos.jsonl", "wakeups.json", "mwi.json",
                 "assistant.jsonl.lock", "callqos.jsonl.lock",
                 "wakeups.json.lock", "mwi.json.lock"):
        p = d / name
        p.write_text("{}\n")
        os.chmod(p, 0o600)

    # An oversized durable log in the same directory. ensure_state_dir() is the
    # only thing that trims it, and it is the only place it CAN be trimmed
    # safely — this oneshot runs before Asterisk opens the file for writing.
    # Asserting the trim function in isolation is not enough: a mutant that
    # deleted the CALL, leaving the function intact, survived that test.
    big = d / "asterisk.log"
    line = "[Sep  6 00:00:00] VERBOSE[1] Endpoint 14 is now Reachable\n"
    with open(big, "w") as fh:
        for _ in range((sbc.DURABLE_LOG_MAX_BYTES // len(line)) + 400):
            fh.write(line)
    oversized = big.stat().st_size
    assert oversized > sbc.DURABLE_LOG_MAX_BYTES, "fixture must exceed the cap"

    real = sbc.STATE_DIR
    try:
        sbc.STATE_DIR = d
        sbc.ensure_state_dir()      # the chown to `asterisk` fails off-box and warns
    finally:
        sbc.STATE_DIR = real

    assert big.stat().st_size < oversized, (
        "ensure_state_dir() did not trim the durable log — the boot path is the "
        "only place it is bounded, and Asterisk holds it open afterwards")
    assert big.stat().st_size > 0, "trimmed to zero — the v0.77.0 defect"

    def mode(name):
        return stat.S_IMODE(os.stat(d / name).st_mode)

    assert mode("assistant.jsonl") == 0o600, (
        f"the boot sweep reopened the speech ledger to "
        f"{oct(mode('assistant.jsonl'))} — it has one writer and must stay 0600")
    # ...and the stores that DO need group access must still get it, or the
    # dial-42 wake-up and the MWI clear go back to failing with EPERM.
    for shared in ("callqos.jsonl", "wakeups.json", "mwi.json",
                   "callqos.jsonl.lock", "wakeups.json.lock", "mwi.json.lock"):
        assert mode(shared) == 0o664, (
            f"{shared} is written by BOTH a root service and an asterisk-user "
            f"AGI and needs group access; got {oct(mode(shared))}")
    assert mode("assistant.jsonl.lock") == 0o600, (
        "the speech ledger's lock was widened too — it has one writer")
