"""Where household speech is allowed to go, pinned as tests.

    python3 -m pytest switchboard/tests/test_privacy_invariants.py

This repository is PUBLIC and runs in someone's house. It records what people
say in their own home. Until v0.80.0 there was no test, no hook and no CI job
enforcing any of that — the discipline was held by maintainer habit alone, which
by this project's own doctrine means it was inert.

THE SHAPE OF THE HAZARD. Asterisk runs `-vvv`, and logger.conf routes the
`verbose` class to `/share/switchboard/asterisk.log`. `/share` is host-mounted
and world-readable BY DESIGN — that is the entire purpose of the directory — and
it is captured in Supervisor backups. So the obvious way to make an AGI
diagnostic durable, `Verbose()` or a dialplan `NoOp()`, is precisely the action
that would move spoken words out of an ephemeral RAM-backed container log and
into a 32 MB durable file readable from outside the add-on.

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


def test_the_verbose_channel_arrangement_is_deliberate():
    """Verbose goes to the world-readable /share log ON PURPOSE, and moving it is
    not a typo fix.

    It looks inverted — the private log gets less than the public one — and the
    reason it stays is a dependency: `Endpoint <n> is now Unreachable` is itself a
    VERBOSE-class line, /data/state/asterisk.log carries none of them, and
    rtpmon.endpoint_transitions() reads the /share file to reconstruct fleet
    outages that fall entirely between two health samples. Flipping the routing
    silently disables that detector. The protection is the tripwire above, not
    the channel.
    """
    src = (ADDON / "rootfs/usr/bin/switchboard-config").read_text()
    share = re.search(r"/share/switchboard/asterisk\.log\s*=>\s*([a-z,]+)", src)
    data = re.search(r"/data/state/asterisk\.log\s*=>\s*([a-z,]+)", src)
    assert share and data, "logger.conf routing not found"
    assert "verbose" in share.group(1), (
        "verbose left the /share log — endpoint_transitions() reads VERBOSE lines "
        "from it and is now blind. If this is intentional, move the reader too.")
    poller = (ADDON / "rootfs/usr/share/switchboard/rtpmon/poller.py").read_text()
    assert "/share/switchboard/asterisk.log" in poller, (
        "the transition reader no longer reads the /share log — re-check whether "
        "verbose still needs to be there")


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


def test_the_world_readable_log_caps_verbose_to_level_2():
    """★★★ THE LEAK THIS CLOSES, measured on the live system 2026-09-09.

    `/share` is host-mounted, readable by anything on the box, and captured in
    Supervisor backups. Its Asterisk log carried a BARE `verbose`, which inherits
    the console level (Asterisk runs `-vvv`) and therefore took level-3 lines:

        pbx.c: Executing [...]              <- every dialplan app, with arguments
        Called PJSIP/<number>@trunk
        Spawn extension (rooms, <number>, ...)

    Those quote the dialled and calling numbers verbatim. The live file held
    **55 lines, 74 occurrences, 3 distinct telephone numbers** — including the
    house's own DID and two third parties who had merely called it.

    ★ IT ALSO DEFEATED THE MASKING ONE LINE ABOVE. The rtpqos summary masks the
    caller ID to `****<last4>` (14 lines in that file), and the `TrySystem` on
    the very next dialplan priority passes the FULL number as an argument — which
    the uncapped trace then logged in full (7 lines). Masking a field is
    worthless while the trace that quotes it is uncapped. That is why the fix is
    the channel level and not the argument.

    ★ AND THE CAP WAS ON THE WRONG CHANNEL. v0.84.0 reasoned correctly that a
    bare `verbose` "dumps the whole dialplan trace onto the persistent volume"
    and capped it — on `/data`, the PRIVATE log — while leaving the
    world-readable one bare. The private log was the one that did not need it.

    WHY 2 AND NOT LESS. `rtpmon.endpoint_transitions()` reads
    `Endpoint <n> is now (Un)Reachable` from this file to reconstruct fleet
    outages between health samples; those are level 2. Verified on the same
    file: 550 such lines, 0 carrying a number, and 0 NOTICE/WARNING/ERROR lines
    carrying one — so the cap removes every leaking line and nothing else.
    """
    import re
    src = (ADDON / "rootfs/usr/bin/switchboard-config").read_text()

    m = re.search(r"/share/switchboard/asterisk\.log\s*=>\s*([a-z,()0-9]+)", src)
    assert m, "the /share logger channel is gone"
    spec = m.group(1)

    lvl = re.search(r"verbose\((\d+)\)", spec)
    assert lvl, (
        f"the world-readable log takes a BARE `verbose` ({spec!r}). It inherits "
        f"Asterisk's -vvv console level and will log the dialplan trace, which "
        f"quotes dialled and calling numbers in the clear.")
    assert int(lvl.group(1)) == 2, (
        f"verbose({lvl.group(1)}) on the world-readable log. Level 3 is the "
        f"dialplan trace (numbers in the clear); below 2 loses the "
        f"`Endpoint <n> is now Unreachable` lines that endpoint_transitions() "
        f"is the entire reason verbose is on this channel.")

    # ...and the private twin stays capped too, for byte rate rather than privacy.
    d = re.search(r"/data/state/asterisk\.log\s*=>\s*([a-z,()0-9]+)", src)
    assert d and "verbose(2)" in d.group(1), (
        f"the /data channel lost its level cap: {d.group(1) if d else 'missing'!r}")
