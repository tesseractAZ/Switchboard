"""Where household speech is allowed to go, pinned as tests.

    python3 -m pytest switchboard/tests/test_privacy_invariants.py

This repository is PUBLIC and runs in someone's house. It records what people
say in their own home. Until v0.80.0 there was no test, no hook and no CI job
enforcing any of that — the discipline was held by maintainer habit alone, which
by this project's own doctrine means it was inert.

THE SHAPE OF THE HAZARD. Asterisk runs `-vvv`. Until v0.94.7 logger.conf routed
the `verbose` class to `/share/switchboard/asterisk.log`, which is host-mounted
and world-readable BY DESIGN — that is the entire purpose of the directory — and
captured in Supervisor backups. So the obvious way to make an AGI diagnostic
durable, `Verbose()` or a dialplan `NoOp()`, was precisely the action that would
move spoken words into a 32 MB durable file readable from outside the add-on.

★ AND THE JOURNAL IS NOT THE SAFE PLACE THIS DOCSTRING CALLED IT (2026-09-14).
It described the container log as ephemeral. The Supervisor serves it to anyone
with log access, for about two days and across a host reboot, and every AGI's
stderr lands in it. Recognised words went there on every call, whatever
`assistant_transcripts` said. They now go there only when it is on; section 3
below pins that.

WHAT IS ACTUALLY TRUE TODAY, verified on the running system rather than reasoned
about: `/share/switchboard/asterisk.log` and `/data/state/asterisk.log` contain
ZERO occurrences of `heard=`, `transcribe` or `[assistant]`. AGI stderr never
reaches Asterisk's logger at all — it is inherited fd 2 and lands raw in the
container log. An audit of this repo concluded the opposite from
`switchboard-operator.agi`'s own comment, which claimed its child's stderr went
to the Asterisk log. That comment was wrong, and being wrong it nearly produced
two bad decisions: purging a file that never held the data, and reasoning "it
already leaks, so one more Verbose() costs nothing."

The safety therefore rests on a fact, not on a design — and a fact holds only
until someone adds one line. These tests are that line's tripwire.

WHY THE ROUTING IS NOT SIMPLY FLIPPED. Sending `verbose` to the private /data log
instead looks like the obvious fix and would break the fleet-outage detector:
`Endpoint <n> is now Unreachable` is itself a VERBOSE-class line, /data carries
zero of them, and `rtpmon.endpoint_transitions()` reads the /share file to
reconstruct outages that fall between two health samples. Keeping the channel and
forbidding speech on it is the arrangement that satisfies both.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADDON = ROOT / "switchboard"

# The household's real LAN. Documentation and tests use 192.168.1.x, which is why
# that prefix is NOT in this pattern — it is the agreed placeholder.
REAL_LAN = re.compile(r"\b192\.168\.[4-7]\.\d{1,3}\b")
# NANP numbers outside the block reserved for fiction. That block is NXX-555-01XX:
# 555 is the PREFIX, not the area code. A first draft of this pattern guarded the
# AREA code instead and flagged every 202-555-0100 in the test suite as a real
# number — a leak scan that cries wolf gets switched off, so the shape matters.
REAL_PHONE = re.compile(r"\b[2-9]\d{2}[-.\s]?(?!555)[2-9]\d{2}[-.\s]?\d{4}\b")
# ...and a SIP URI is not an email address: `19@cordless.local` is an AOR. The
# .local/.invalid/.test TLDs are reserved and cannot be a real mailbox.
EMAIL = re.compile(
    r"\b[\w.+-]+@(?!example\.|test\.|localhost)[\w-]+\.(?!local\b|invalid\b|test\b)[a-z]{2,}\b")

# Fields that carry what a person said. Named once, here, so the rules below are
# one grep away from anyone adding a field to any ledger.
SPEECH_FIELDS = ("heard", "reply", "transcript", "text", "utterance", "speech")


def _tracked():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return [ROOT / p for p in out.split("\n") if p.strip()]


def _text_files():
    # Vendored third-party bundles are not ours to police, and minified JS is a
    # digit soup that matches any numeric pattern by chance.
    VENDORED = ("console-web/static/xterm.js", "console-web/static/xterm.css")
    for p in _tracked():
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".wav", ".gif", ".ico",
                                ".pdf", ".docx", ".zip"):
            continue
        if any(v in str(p) for v in VENDORED):
            continue
        try:
            yield p, p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue


# --------------------------------------------------------------------------- #
# 1. Nothing personal reaches the public repository.
# --------------------------------------------------------------------------- #
def test_no_real_lan_address_is_committed():
    """The house is on 192.168.4-7.x. Docs and tests use 192.168.1.x."""
    hits = [f"{p.relative_to(ROOT)}:{i}: {m.group(0)}"
            for p, t in _text_files()
            for i, line in enumerate(t.split("\n"), 1)
            for m in [REAL_LAN.search(line)] if m]
    assert not hits, "real LAN addresses in a public repo:\n  " + "\n  ".join(hits)


def test_no_real_phone_number_is_committed():
    """555-01xx is the reserved-for-fiction block; anything else may be a real
    line. The trunk DID in particular must never appear."""
    skip = {"switchboard/CHANGELOG.md"}          # historical, audited separately
    hits = []
    for p, t in _text_files():
        rel = str(p.relative_to(ROOT))
        if rel in skip:
            continue
        for i, line in enumerate(t.split("\n"), 1):
            # Version strings, dates, hashes and byte counts are not phone numbers.
            if re.search(r"\b(v?\d+\.\d+\.\d+|20\d\d-\d\d-\d\d)\b", line):
                continue
            m = REAL_PHONE.search(line)
            if not m:
                continue
            digits = re.sub(r"\D", "", m.group(0))
            # No NANP number is ten identical digits; 9999999999 is a placeholder
            # in the screenshot generator. A scan that flags obvious filler is a
            # scan people learn to ignore.
            if len(set(digits)) <= 1:
                continue
            hits.append(f"{rel}:{i}: {m.group(0)}")
    assert not hits, "possible real phone numbers:\n  " + "\n  ".join(hits)


def test_no_personal_email_is_committed():
    hits = [f"{p.relative_to(ROOT)}:{i}: {m.group(0)}"
            for p, t in _text_files()
            for i, line in enumerate(t.split("\n"), 1)
            for m in [EMAIL.search(line)] if m]
    assert not hits, "email addresses in a public repo:\n  " + "\n  ".join(hits)


# --------------------------------------------------------------------------- #
# 2. Speech stays out of anything readable from outside the container.
# --------------------------------------------------------------------------- #
def test_the_assistant_ledger_lives_in_data_not_share():
    """/data is unreachable from outside — container shell blocked by protection
    mode, backups encrypted, add-on API 403. /share is the opposite by design."""
    src = (ADDON / "rootfs/usr/share/switchboard/webui/assistant_log.py").read_text()
    m = re.search(r'PATH = os\.environ\.get\([^,]+,\s*"([^"]+)"\)', src)
    assert m, "could not find the ledger path"
    path = m.group(1)
    assert path.startswith("/data/"), f"the speech ledger is at {path}"
    # Match a /share PATH, not the word — the module's own docstring explains at
    # length why it has no /share mirror, and a substring test flagged that prose.
    stray = re.findall(r'["\']/share/[^"\']*["\']', src)
    assert not stray, f"the speech ledger references a /share path: {stray}"


def test_no_share_writer_accepts_a_speech_field():
    """callqos mirrors its FULL record to /share on the stated principle that
    "there is no reason to editorialise". That was reasoned about field
    completeness for auditing and never about disclosure, and it is the natural
    template for the next ledger someone writes. It must not become one."""
    offenders = []
    for rel in ("rootfs/usr/bin/switchboard-callqos",
                "rootfs/usr/share/switchboard/webui/delivery.py",
                "rootfs/usr/share/switchboard/rtpmon/poller.py"):
        src = (ADDON / rel).read_text()
        # Only the record-construction regions matter, but a whole-file check is
        # the conservative one: these files should not mention speech at all.
        for f in SPEECH_FIELDS:
            if re.search(rf'"{f}"\s*:', src):
                offenders.append(f"{rel} builds a {f!r} field")
    assert not offenders, ("a /share writer carries speech:\n  "
                           + "\n  ".join(offenders))


def test_no_agi_routes_speech_through_asterisks_logger():
    """★ THE TRIPWIRE. Asterisk's `verbose` class lands in the world-readable
    /share log. An AGI that emits a transcript through VERBOSE or a dialplan NoOp
    publishes it. Today no AGI does — verified live, zero `heard=` in either log —
    and that is a fact about the current code, not a property of the design."""
    agi_dir = ADDON / "rootfs/var/lib/asterisk/agi-bin"
    offenders = []
    for p in sorted(agi_dir.glob("*.agi")):
        for i, line in enumerate(p.read_text().split("\n"), 1):
            if re.search(r"""agi\(\s*f?['"]\s*(VERBOSE|NOOP)""", line, re.I):
                offenders.append(f"{p.name}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "an AGI emits through Asterisk's logger, which is mirrored world-readable:\n  "
        + "\n  ".join(offenders))


def test_the_world_readable_log_carries_no_dialplan_trace():
    """★★★ THE LEAK, and why the obvious fix does not work.

    `/share` is host-mounted, readable by anything on the box, and captured in
    Supervisor backups. Its Asterisk log carried `verbose`, which admits the
    dialplan trace: `pbx.c: Executing [...]`, `Called PJSIP/<number>@trunk`,
    `Spawn extension (rooms, <number>, ...)`. Measured live 2026-09-09: **55
    lines, 74 occurrences, 3 distinct complete telephone numbers** -- the house's
    own DID and two third parties who had merely called it.

    ★ A LEVEL CAP DOES NOT WORK, and v0.94.6 shipped believing it did. The
    reasoning was that the wanted lines (`Endpoint <n> is now (Un)Reachable`) are
    level 2 and the leaking ones level 3, so `verbose(2)` would separate them. It
    does not. `/data` carried `verbose(2)` from v0.84.0 and still logged 208
    `pbx.c: Executing` lines on 2026-09-09 -- while receiving ZERO on Sep 4-5,
    when it had no `verbose` keyword at all. The KEYWORD admits the trace; the
    number is decoration.

    So the only lever is where the reader looks. `endpoint_transitions()` now
    reads the private `/data` copy, and this world-readable one carries
    severities only. Both halves are asserted here, because either alone is a
    silent regression: leaving `verbose` on `/share` republishes the numbers,
    and pointing the reader back at `/share` makes the fleet-outage detector
    depend on a channel that must not carry what it needs.
    """
    import re
    src = (ADDON / "rootfs/usr/bin/switchboard-config").read_text()

    share = re.search(r"/share/switchboard/asterisk\.log\s*=>\s*([a-z,()0-9]+)", src)
    assert share, "the /share logger channel is gone"
    assert "verbose" not in share.group(1), (
        f"the world-readable log carries {share.group(1)!r}. `verbose` admits the "
        f"dialplan trace, which quotes dialled and calling numbers in the clear. "
        f"A level cap does NOT filter it -- that was tried in v0.94.6 and was inert.")
    for sev in ("notice", "warning", "error"):
        assert sev in share.group(1), f"the /share copy lost {sev}"

    data = re.search(r"/data/state/asterisk\.log\s*=>\s*([a-z,()0-9]+)", src)
    assert data and "verbose" in data.group(1), (
        f"the private /data copy lost verbose ({data.group(1) if data else 'missing'!r}); "
        f"the dialplan trace and the endpoint transitions now exist nowhere.")

    poller = (ADDON / "rootfs/usr/share/switchboard/rtpmon/poller.py").read_text()
    m = re.search(r'ENDPOINT_LOG_PATH\s*=\s*os\.environ\.get\([^,]+,\s*\n?\s*"([^"]+)"', poller)
    assert m, "could not find the transition reader's log path"
    assert m.group(1).startswith("/data/"), (
        f"endpoint_transitions() reads {m.group(1)} -- but /share no longer "
        f"carries the VERBOSE lines it parses, so the fleet-outage detector is "
        f"blind. It must read the private copy.")


@pytest.mark.parametrize("claim", [
    "transcript/decision -> Asterisk log",
])
def test_no_source_claims_agi_stderr_reaches_the_asterisk_log(claim):
    """A comment that is wrong about where data goes is a privacy hazard.

    This exact sentence in switchboard-operator.agi led an audit to conclude that
    household transcripts were already in the world-readable log. They were not,
    and the false belief nearly justified adding more.
    """
    # ...excluding this file, which necessarily contains the string it forbids.
    # It passed locally and failed in CI for exactly that reason: `git ls-files`
    # does not list an untracked file, so the scan could not see itself until the
    # commit landed. A scanner that matches its own pattern is a self-inflicted
    # false positive, and the fix is scoping, not weakening the pattern.
    me = Path(__file__).resolve()
    hits = [str(p.relative_to(ROOT)) for p, t in _text_files()
            if claim in t and p.resolve() != me]
    assert not hits, (f"{claim!r} is false — AGI stderr is inherited fd 2 and "
                      f"never reaches Asterisk's logger. Found in: {hits}")


# The readable-log scrubber (webui/logscrub.py) and both of its callers are
# tested in test_logscrub.py.


# --------------------------------------------------------------------------- #
# 3. Recognised words reach the add-on journal only when transcripts are on.
#
# The journal is where every AGI's stderr goes, and it is not private: the
# Supervisor serves it to anyone with log access, for about two days and across a
# host reboot. On 2026-09-14 it held six recognitions in one day, verbatim, from
# the wake-up flow as well as the assistant. The behaviour is driven end to end in
# test_switchboard_stt_server.py and test_assistant_agi.py; the scan below is the
# tripwire for the NEXT log line, the one nobody has written yet.
# --------------------------------------------------------------------------- #
import ast  # noqa: E402

VOICE_SCRIPTS = (sorted((ADDON / "rootfs/var/lib/asterisk/agi-bin").glob("*.agi"))
                 + [ADDON / "rootfs/usr/bin/switchboard-stt",
                    ADDON / "rootfs/usr/share/switchboard/operator/agi_speech.py"])
# Calls whose return value IS what the caller said. Not `recognize`: the operator
# and wake-up AGIs name that for helpers that return a room token or HH:MM.
RECOGNISERS = {"listen", "_recording_to_text", "transcribe"}
GATE = "said"


def _call_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _is_stderr_sink(call: ast.Call) -> bool:
    f = call.func
    if _call_name(call) == "log":                                 # log(...), sp.log(...)
        return True
    if (isinstance(f, ast.Attribute) and f.attr == "write"
            and isinstance(f.value, ast.Attribute) and f.value.attr == "stderr"):
        return True                                               # sys.stderr.write(...)
    return _call_name(call) == "print" and any(                   # print(..., file=sys.stderr)
        k.arg == "file" and isinstance(k.value, ast.Attribute) and k.value.attr == "stderr"
        for k in call.keywords)


def _names(expr, tainted, *, gated: bool):
    """Tainted names in `expr` outside (gated=False) or inside (True) a said() call."""
    out, stack = [], [(expr, False)]
    while stack:
        node, inside = stack.pop()
        if isinstance(node, ast.Call) and _call_name(node) == GATE:
            inside = True
        if isinstance(node, ast.Name) and node.id in tainted and inside is gated:
            out.append(node.id)
        stack.extend((c, inside) for c in ast.iter_child_nodes(node))
    return out


def _scope_nodes(scope):
    """Every node in `scope`. The module scope stops at function bodies; a
    function keeps the functions nested in it, which can read its names.

    A first draft walked the whole module for the module scope, so the `text`
    that automation's main() binds from listen() tainted the unrelated `text`
    parameter of its say(), which speaks light names."""
    out, stack = [], list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(scope, ast.Module) and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def scan_speech_to_stderr(src: str):
    """(ungated, gated) stderr writes that interpolate recognised text.

    A name is recognised text when a scope binds it from a RECOGNISERS call. The
    AST is scanned, not the characters, so a comment explaining this rule cannot
    trip it."""
    tree = ast.parse(src)
    scopes = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    ungated, gated = set(), set()
    for scope in scopes:
        nodes = _scope_nodes(scope)
        tainted = set()
        for n in nodes:
            if (isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Call)
                    and _call_name(n.value) in RECOGNISERS):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                tainted |= {t.id for tgt in targets for t in ast.walk(tgt)
                            if isinstance(t, ast.Name)}
        for n in nodes:
            if isinstance(n, ast.Call) and _is_stderr_sink(n):
                for arg in list(n.args) + [k.value for k in n.keywords]:
                    bad = _names(arg, tainted, gated=False)
                    if bad:
                        ungated.add((n.lineno, tuple(sorted(set(bad)))))
                    if _names(arg, tainted, gated=True):
                        gated.add(n.lineno)
    return sorted(ungated), sorted(gated)


def test_the_speech_scanner_can_see_what_it_forbids():
    """A scanner that has never flagged anything proves nothing about the tree."""
    cases = {
        'def f():\n    text = listen("x", 1)\n    log(f"heard {text!r}")\n': True,
        'def f():\n    text, meta = listen("x", 1)\n    sys.stderr.write(f"{text}\\n")\n': True,
        'def f():\n    text = sp.listen("x")\n    sp.log("tag", f"{text[:40]}")\n': True,
        'def f():\n    text = _recording_to_text(a, b)\n    print(text, file=sys.stderr)\n': True,
        'def f():\n    text = listen("x", 1)\n    log(f"heard {said(text, w)}")\n': False,
        'def f():\n    text = listen("x", 1)\n    log(f"heard {len(text)}")\n': True,
        'def f(text):\n    log(f"tts failed for {text!r}")\n': False,   # not recognised here
        # Another function's parameter that shares the name is not recognised
        # text (automation's say(text) speaks light names)...
        ('def say(text):\n    log(f"tts failed for {text!r}")\n\n'
         'def main():\n    text = listen("x", 1)\n    log(f"{said(text, w)}")\n'): False,
        # ...but a closure over the recognised name is.
        ('def main():\n    text = listen("x", 1)\n'
         '    def inner():\n        log(f"{text}")\n    inner()\n'): True,
    }
    for src, flagged in cases.items():
        ungated, gated = scan_speech_to_stderr(src)
        assert bool(ungated) is flagged, (src, ungated)
        if "said(" in src:
            assert gated, f"the gated form was not seen as gated:\n{src}"


def test_no_voice_script_writes_recognised_words_to_stderr_ungated():
    assert len(VOICE_SCRIPTS) >= 11, [p.name for p in VOICE_SCRIPTS]
    offenders, gated = [], {}
    for p in VOICE_SCRIPTS:
        bad, ok = scan_speech_to_stderr(p.read_text())
        offenders += [f"{p.name}:{line}: {names}" for line, names in bad]
        gated[p.name] = len(ok)
    assert not offenders, (
        "recognised speech reaches stderr, which is the add-on journal, without "
        "said():\n  " + "\n  ".join(offenders))
    # ...and the scan SEES the sites it is there for: the six heard= lines in
    # switchboard-stt and the assistant's per-turn line. Fewer means a rename
    # blinded it, not that the lines went away.
    assert gated["switchboard-stt"] >= 6, gated
    assert gated["switchboard-assistant.agi"] >= 1, gated


def test_the_tts_helper_does_not_echo_the_text_it_failed_to_speak(tmp_path, capsys):
    """switchboard-tts's stderr reaches the journal through whichever AGI ran it.
    Its timeout message rendered the espeak command line, and the text is an
    argument on it."""
    import subprocess as _sp
    import types
    from importlib.machinery import SourceFileLoader
    tts = SourceFileLoader("switchboard_tts_privacy",
                           str(ADDON / "rootfs/usr/bin/switchboard-tts")).load_module()
    tts._espeak_bin = lambda: "/usr/bin/espeak-ng"

    def _run(cmd, **kw):
        raise _sp.TimeoutExpired(cmd, 15)
    tts.subprocess = types.SimpleNamespace(run=_run, TimeoutExpired=_sp.TimeoutExpired,
                                           SubprocessError=_sp.SubprocessError)
    capsys.readouterr()
    assert tts.synthesize("the zebra lamp is on", str(tmp_path / "out")) is False
    err = capsys.readouterr().err
    assert "timed out" in err and "zebra" not in err, err


@pytest.mark.parametrize("claim", [
    "RAM-backed container log",
    "evaporates on rotation",
    "rotates in hours",
    "wiped by a reboot",
])
def test_no_source_calls_the_add_on_journal_ephemeral(claim):
    """★ The belief that the journal forgets is what let recognised speech go
    there unexamined. Four comments said it; the live system kept a previous
    boot's log for 30 h after a host reboot. Matched across line wraps and
    comment markers. CHANGELOG.md is history and is not rewritten."""
    me = Path(__file__).resolve()
    hits = []
    for p, t in _text_files():
        if p.resolve() == me or str(p.relative_to(ROOT)) == "switchboard/CHANGELOG.md":
            continue
        flat = " ".join(re.sub(r"(?m)^\s*#+", " ", t).split())
        if claim in flat:
            hits.append(str(p.relative_to(ROOT)))
    assert not hits, (f"{claim!r} describes the add-on journal as ephemeral; it "
                      f"keeps about two days and survives a reboot. Found in: {hits}")
