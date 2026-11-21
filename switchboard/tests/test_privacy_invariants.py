"""Where household speech is allowed to go, pinned as tests.

    python3 -m pytest switchboard/tests/test_privacy_invariants.py

This repository is PUBLIC and runs in someone's house. It records what people
say in their own home. Until v0.80.0 there was no test, no hook and no CI job
enforcing any of that — the discipline was held by maintainer habit alone, which
by this project's own doctrine means it was inert.

THE SHAPE OF THE HAZARD. Asterisk produces verbose lines at every log_level (the
/data channel's verbose(3) holds that level). Until v0.94.7 logger.conf routed
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

WHERE THE VERBOSE CLASS GOES NOW. Since v0.94.7 `verbose` goes to the private
/data log and to the console, never to /share. `Endpoint <n> is now Unreachable`
is itself a VERBOSE-class line, so `rtpmon.endpoint_transitions()` reads /data to
reconstruct outages that fall between two health samples. (This paragraph used to
say /data carried none of those lines and the reader used /share — true before
v0.84.0 and v0.94.7 respectively, not since.) Speech is forbidden on every channel
that carries verbose, which is what the tests below pin.
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
    skip = {"switchboard/CHANGELOG.md", "switchboard/CHANGELOG-ARCHIVE.md"}   # historical, audited separately
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
    number does not filter it. (Since v0.107.0 the /data channel's number does
    set the level Asterisk produces verbose lines at, so it is not decoration.)

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
import json  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from importlib.machinery import SourceFileLoader  # noqa: E402

SPEECH = ADDON / "rootfs/usr/share/switchboard/operator/agi_speech.py"
ANNOUNCE = ADDON / "rootfs/var/lib/asterisk/agi-bin/switchboard-announce.agi"
VOICE_SCRIPTS = (sorted((ADDON / "rootfs/var/lib/asterisk/agi-bin").glob("*.agi"))
                 + [ADDON / "rootfs/usr/bin/switchboard-stt", SPEECH])
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


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _own_nodes(func):
    """`func`'s nodes without the bodies of the functions nested in it: where its
    own `return` statements are."""
    out, stack = [], list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, (*_DEFS, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))
    return out


def _carries(expr, tainted, recognisers) -> bool:
    """Is `expr` still the words: a recogniser's answer, the name, a slice or a
    string method of it, or a string built around it. A call that MAPS the words
    to something else (a room token, an HH:MM, a light) is not, which is why the
    operator may log the extension it resolved."""
    if isinstance(expr, ast.Name):
        return expr.id in tainted
    if isinstance(expr, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) and _carries(v.value, tainted, recognisers)
                   for v in expr.values)
    if isinstance(expr, ast.BinOp):
        return any(_carries(s, tainted, recognisers) for s in (expr.left, expr.right))
    if isinstance(expr, ast.Subscript):
        return _carries(expr.value, tainted, recognisers)
    if isinstance(expr, ast.IfExp):
        return any(_carries(s, tainted, recognisers) for s in (expr.body, expr.orelse))
    if isinstance(expr, (ast.BoolOp, ast.List, ast.Tuple)):
        parts = expr.values if isinstance(expr, ast.BoolOp) else expr.elts
        return any(_carries(s, tainted, recognisers) for s in parts)
    if isinstance(expr, ast.Call):
        f = expr.func
        if _call_name(expr) in recognisers:
            return True                                           # text = listen(...)
        if isinstance(f, ast.Attribute) and _carries(f.value, tainted, recognisers):
            return True                                           # text.strip()
        if ((isinstance(f, ast.Attribute) and isinstance(f.value, ast.Constant))
                or (isinstance(f, ast.Name) and f.id in ("str", "repr"))):
            return any(_carries(a, tainted, recognisers) for a in expr.args)  # " ".join([text])
    return False


def _recognised_names(nodes, recognisers) -> set:
    """The names in a scope that hold recognised words: bound from a recogniser,
    or from an expression that still carries them (to a fixed point)."""
    tainted, changed = set(), True
    while changed:
        changed = False
        for n in nodes:
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value is not None:
                if not _carries(n.value, tainted, recognisers):
                    continue
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                new = {t.id for tgt in targets for t in ast.walk(tgt)
                       if isinstance(t, ast.Name)} - tainted
                if new:
                    tainted |= new
                    changed = True
    return tainted


def _signature(func):
    a = func.args
    positional = [p.arg for p in a.posonlyargs + a.args]
    return positional, set(positional) | {p.arg for p in a.kwonlyargs}


def _into_leaky(call, names, local, shared):
    """The `names` that `call` hands to a parameter which reaches stderr ungated.
    A bare name resolves in this module first; `sp.say_or(...)` in the shared
    one (agi_speech)."""
    f = call.func
    if isinstance(f, ast.Name):
        entry = local.get(f.id) or shared.get(f.id)
    elif isinstance(f, ast.Attribute):
        entry = shared.get(f.attr)
    else:
        entry = None
    if not entry or not entry[1]:
        return []
    positional, leaky = entry
    out = []
    for i, arg in enumerate(call.args):
        if i < len(positional) and positional[i] in leaky:
            out += _names(arg, names, gated=False)
    for k in call.keywords:
        if k.arg in leaky:
            out += _names(k.value, names, gated=False)
    return out


def analyse_speech_module(src: str, shared=None):
    """(recognisers, leaky) for one module, to a fixed point.

    recognisers: the RECOGNISERS plus every function here whose return value
    still carries recognised words (the announce AGI's _record() returns what
    listen() heard). leaky: {function: (positional params, the params that reach
    stderr outside said(), directly or through another call)}. A first version
    of this scan looked inside one scope only, so the announce AGI's words, which
    reach agi_speech.say()'s log line through _record() and say_or(), passed it
    (v0.122.1)."""
    shared_rec, shared_leaky = shared or (frozenset(), {})
    tree = ast.parse(src)
    funcs = [n for n in ast.walk(tree) if isinstance(n, _DEFS)]
    rec = set(RECOGNISERS) | set(shared_rec)
    leaky = {fn.name: (_signature(fn)[0], set()) for fn in funcs}
    changed = True
    while changed:
        changed = False
        for fn in funcs:
            nodes = _scope_nodes(fn)
            tainted = _recognised_names(nodes, rec)
            if fn.name not in rec and any(
                    isinstance(n, ast.Return) and n.value is not None
                    and _carries(n.value, tainted, rec) for n in _own_nodes(fn)):
                rec.add(fn.name)
                changed = True
            params = _signature(fn)[1]
            hits = set()
            for n in nodes:
                if not isinstance(n, ast.Call):
                    continue
                if _is_stderr_sink(n):
                    for arg in list(n.args) + [k.value for k in n.keywords]:
                        hits |= set(_names(arg, params, gated=False))
                else:
                    hits |= set(_into_leaky(n, params, leaky, shared_leaky))
            if hits - leaky[fn.name][1]:
                leaky[fn.name][1].update(hits)
                changed = True
    return rec, leaky


def scan_speech_to_stderr(src: str, shared=None):
    """(ungated, gated) stderr writes that interpolate recognised text.

    A name is recognised text when a scope binds it from a recogniser, or from an
    expression that still carries the words. Ungated: such a name written to
    stderr outside said(), or handed to a parameter that is (`via say_or()`,
    resolved through `shared`, agi_speech's analyse_speech_module()). Gated: a
    stderr write that passes recognised text, or a parameter, through said().
    The AST is scanned, not the characters, so a comment explaining this rule
    cannot trip it."""
    tree = ast.parse(src)
    rec, leaky = analyse_speech_module(src, shared)
    shared_leaky = (shared or (None, {}))[1]
    scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, _DEFS)]
    ungated, gated = set(), set()
    for scope in scopes:
        nodes = _scope_nodes(scope)
        tainted = _recognised_names(nodes, rec)
        params = _signature(scope)[1] if isinstance(scope, _DEFS) else set()
        for n in nodes:
            if not isinstance(n, ast.Call):
                continue
            if _is_stderr_sink(n):
                for arg in list(n.args) + [k.value for k in n.keywords]:
                    bad = _names(arg, tainted, gated=False)
                    if bad:
                        ungated.add((n.lineno, tuple(sorted(set(bad)))))
                    if _names(arg, tainted | params, gated=True):
                        gated.add(n.lineno)
                continue
            via = _into_leaky(n, tainted, leaky, shared_leaky)
            if via:
                ungated.add((n.lineno, tuple(sorted({f"{v} via {_call_name(n)}()"
                                                     for v in via}))))
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


def test_the_speech_scanner_follows_the_words_across_calls():
    """Words reach a log line through a helper's return value or another
    function's parameter, as the announce AGI's did through _record() and
    agi_speech.say_or() -> say() until v0.122.1."""
    leaky_say = 'def say(text):\n    log("speech", f"tts failed for {text[:40]!r}")\n\n'
    gated_say = 'def say(text):\n    log("speech", f"tts failed for {said(text, w)}")\n\n'
    cases = {
        # A helper that returns what listen() heard is a recogniser too.
        ('def _record():\n    text = ""\n    text = sp.listen("x")\n    return text\n\n'
         'def main():\n    text = _record()\n    log(f"{text}")\n'): True,
        # The words handed to a parameter that is logged outside said()...
        leaky_say + 'def main():\n    text = listen("x")\n    say(f"You said {text}")\n': True,
        # ...through a second hop, under another name that still carries them...
        (leaky_say + 'def say_or(t):\n    if not say(t):\n        pass\n\n'
         'def main():\n    text = listen("x")\n    msg = "Heard: " + text.strip()\n'
         '    say_or(msg)\n'): True,
        # ...but not to a parameter that goes through said()...
        gated_say + 'def main():\n    text = listen("x")\n    say(f"You said {text}")\n': False,
        # ...nor once a call has mapped the words to something else.
        (leaky_say + 'def main():\n    text = listen("x")\n    room = resolve(text)\n'
         '    say(f"Calling {room}")\n'): False,
    }
    for src, flagged in cases.items():
        assert bool(scan_speech_to_stderr(src)[0]) is flagged, src
    # Across modules: an AGI's sp.say_or() resolves in agi_speech's summary.
    agi = 'def main():\n    text = sp.listen("x")\n    sp.say_or(f"Announcing: {text}")\n'
    for speech, flagged in ((leaky_say, True), (gated_say, False)):
        shared = analyse_speech_module(speech + 'def say_or(text):\n    return say(text)\n')
        assert bool(scan_speech_to_stderr(agi, shared)[0]) is flagged, speech


# agi_speech.say() before v0.122.1, in the shape the announce AGI's words reached.
_SAY_BEFORE_0_122_1 = (
    'def log(tag, msg):\n    sys.stderr.write(f"[{tag}] {msg}\\n")\n\n'
    'def say(text, prefix="sw-voice"):\n'
    '    log("speech", f"tts failed rc=1 for {text[:40]!r}")\n\n'
    'def say_or(text, fallback_prompt=None):\n'
    '    if not say(text):\n        stream(fallback_prompt)\n')


def test_the_scan_sees_the_announce_words_that_reached_say():
    """The real announce AGI against the pre-v0.122.1 say(): exactly its spoken
    confirmation is flagged. Run against the current agi_speech, the tree scan
    below must then find nothing; if this stops flagging, a refactor of the AGI
    or of the scanner blinded it, and the tree scan proves nothing about it."""
    src = ANNOUNCE.read_text()
    bad, _ = scan_speech_to_stderr(src, analyse_speech_module(_SAY_BEFORE_0_122_1))
    lines = src.splitlines()
    assert [("Announcing on the speakers" in lines[ln - 1], names) for ln, names in bad] \
        == [(True, ("text via say_or()",))], bad


def test_no_voice_script_writes_recognised_words_to_stderr_ungated():
    assert len(VOICE_SCRIPTS) >= 11, [p.name for p in VOICE_SCRIPTS]
    shared = analyse_speech_module(SPEECH.read_text())
    offenders, gated = [], {}
    for p in VOICE_SCRIPTS:
        bad, ok = scan_speech_to_stderr(p.read_text(), shared)
        offenders += [f"{p.name}:{line}: {names}" for line, names in bad]
        gated[p.name] = len(ok)
    assert not offenders, (
        "recognised speech reaches stderr, which is the add-on journal, without "
        "said():\n  " + "\n  ".join(offenders))
    # ...and the scan SEES the sites it is there for: the six heard= lines in
    # switchboard-stt, the assistant's per-turn line, and agi_speech.say()'s two
    # failure lines. Fewer means a rename blinded it, not that the lines went away.
    assert gated["switchboard-stt"] >= 6, gated
    assert gated["switchboard-assistant.agi"] >= 1, gated
    assert gated["agi_speech.py"] >= 2, gated


SECRET_WORDS = "meet me in the zebra room at six"


def _speech(tmp_path, monkeypatch, policy):
    """A fresh agi_speech whose features.json says `policy` (None: no file)."""
    sp = SourceFileLoader("agi_speech_privacy", str(SPEECH)).load_module()
    feats = tmp_path / "features.json"
    if policy is not None:
        feats.write_text(json.dumps({"announce": {"players": ["media_player.kitchen"]},
                                     "assistant": {"transcripts": policy}}))
    monkeypatch.setattr(sp, "FEATURES", str(feats))
    monkeypatch.setattr(sp, "agi", lambda cmd: "")
    monkeypatch.setattr(sp, "stream", lambda soundfile: None)
    return sp


@pytest.mark.parametrize("how", ["rc", "timeout"])
@pytest.mark.parametrize("policy", [False, None, "true", True])
def test_a_failed_reply_names_the_words_only_when_transcripts_are_on(
        how, policy, tmp_path, monkeypatch, capsys):
    """agi_speech.say() speaks whatever its caller hands it, and the announce
    flow hands it the caller's own words. Its failure lines follow the policy
    switchboard-stt and the assistant follow, failing closed (no file, or a
    truthy value that is not true, withholds). A TimeoutExpired renders its whole
    command line, the text included; real child processes, cut to 0.3 s."""
    sp = _speech(tmp_path, monkeypatch, policy)
    tts = tmp_path / "tts"
    tts.write_text("#!/bin/sh\nexec sleep 5\n" if how == "timeout" else "#!/bin/sh\nexit 1\n")
    tts.chmod(0o755)
    monkeypatch.setattr(sp, "TTS", str(tts))
    real_run = subprocess.run
    monkeypatch.setattr(sp.subprocess, "run",
                        lambda cmd, **kw: real_run(cmd, **{**kw, "timeout": 0.3}))
    text = f"Announcing on the speakers: {SECRET_WORDS}. Goodbye."
    capsys.readouterr()
    assert sp.say(text) is False
    err = capsys.readouterr().err
    assert ("tts failed rc=1" if how == "rc" else "tts error") in err, err
    if policy is True:
        assert SECRET_WORDS in err, err      # the gate opens, so this test can see words
    else:
        assert "zebra" not in err and f"<{len(text)} chars>" in err, err
        assert how == "rc" or err.rstrip().endswith(": TimeoutExpired"), err


@pytest.mark.parametrize("policy", [False, True])
def test_the_announce_flow_keeps_the_callers_words_out_of_the_journal(
        policy, tmp_path, monkeypatch, capsys):
    """End to end through the announce AGI: the words it heard, spoken back in its
    confirmation, reach stderr only when transcripts are on. Every reply's TTS
    times out (the real exception, with the real command line)."""
    sp = _speech(tmp_path, monkeypatch, policy)
    monkeypatch.setattr(sp, "read_env", lambda: {})
    monkeypatch.setattr(sp, "listen", lambda *a, **k: SECRET_WORDS)

    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(sp.subprocess, "run", run)
    hc = types.SimpleNamespace(resolve_entities=lambda players: (players, [], True),
                               call_service=lambda *a, **k: True)
    aa = types.SimpleNamespace(build_announcement=lambda text, out: True,
                               lan_ip=lambda: "192.168.1.20")
    ac = types.SimpleNamespace(ANNOUNCE_DIR=str(tmp_path), sweep=lambda: None,
                               speaker_clip_name=lambda: "clip.wav")
    for name, mod in (("agi_speech", sp), ("ha_client", hc), ("announce_audio", aa),
                      ("announce_clip", ac)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(sys, "path", list(sys.path))   # the AGI prepends its own
    agi = SourceFileLoader("switchboard_announce_privacy", str(ANNOUNCE)).load_module()
    capsys.readouterr()
    agi.main()
    err = capsys.readouterr().err
    assert err.count("tts error") == 2, err    # the opening prompt and the confirmation
    assert (SECRET_WORDS in err) is policy, err


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
                                           SubprocessError=_sp.SubprocessError,
                                           DEVNULL=_sp.DEVNULL)
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
        if p.resolve() == me or str(p.relative_to(ROOT)) in ("switchboard/CHANGELOG.md",
                                                             "switchboard/CHANGELOG-ARCHIVE.md"):
            continue
        flat = " ".join(re.sub(r"(?m)^\s*#+", " ", t).split())
        if claim in flat:
            hits.append(str(p.relative_to(ROOT)))
    assert not hits, (f"{claim!r} describes the add-on journal as ephemeral; it "
                      f"keeps about two days and survives a reboot. Found in: {hits}")
