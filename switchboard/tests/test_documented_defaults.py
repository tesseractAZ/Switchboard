"""The manual's option tables must agree with config.yaml.

    python3 -m pytest switchboard/tests/test_documented_defaults.py

Nothing checked this before. The manual's `| Option | Default | Notes |` tables
are hand-maintained beside a `config.yaml` that changes, and this repo has
already shipped a documented default that had been wrong for two releases — the
kind of error that is invisible in review (both files look right on their own)
and expensive in use, because the reader trusts the manual over the schema.

The full option tables live in docs/reference/configuration.md. DOCS.md, the
short guide Home Assistant's Documentation tab shows, is checked too: any table
of its own headed `| Option | Default |` is held to the same schema, since that
is the page most readers see.

The bind assertions at the bottom are separate: they pin a SECURITY default
rather than merely a consistent one.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.yaml"
DOCS = ROOT / "docs" / "reference" / "configuration.md"
GUIDE = ROOT / "DOCS.md"

# Options whose documented "default" is deliberately prose rather than a value
# (a derived default, or one whose meaning needs a sentence).
PROSE_DEFAULTS = {"rooms", "trunk", "operator_synonyms", "wakeup_scenes",
                  "console_users", "gateway_ports"}

# A default that changed in config.yaml while its manual row is rewritten in a
# separate change: {option: (the stale value the manual still shows, the new
# config.yaml value)}. Checked in BOTH directions so an entry cannot outlive the
# edit it waits for: once the manual shows the config.yaml default, the comparison
# below fails until the entry is deleted. Never a way to excuse a mismatch with
# no documentation change on its way.
DOCS_PENDING = {}

# The header of a table whose rows are `| \`option\` | default | ... |`.
_OPTION_TABLE = re.compile(r"^\|\s*(?:Option|Sub-field)\s*\|\s*Default\s*\|", re.I)


def _config_options():
    """The `options:` block of config.yaml as {name: literal-as-written}.

    Parsed textually rather than with yaml.safe_load so the comparison is
    against what a reader sees, and so this file has no dependency the CI job
    might not install.
    """
    src = CONFIG.read_text()
    block = src[src.index("\noptions:"):]
    block = block[:block.index("\nschema:")]
    out = {}
    for line in block.split("\n"):
        m = re.match(r"^  ([a-z0-9_]+):\s*(.*?)\s*$", line)
        if m and m.group(2) and not m.group(2).startswith("#"):
            out[m.group(1)] = m.group(2)
    return out


def _option_rows(path: Path, headed_only: bool):
    """[(option, default-cell)] from the `| \\`name\\` | default | ... |` rows of
    `path`. With `headed_only`, only rows of a table whose header is
    `| Option | Default |` (or `| Sub-field | Default |`) count, so a guide's
    feature-code or sensor table is not mistaken for a list of defaults."""
    rows, in_table = [], not headed_only
    for line in path.read_text(encoding="utf-8").split("\n"):
        if headed_only:
            if _OPTION_TABLE.match(line):
                in_table = True
                continue
            if not line.startswith("|"):
                in_table = False
        m = re.match(r"^\|\s*`([a-z0-9_]+)`\s*\|\s*(.*?)\s*\|", line)
        if m and in_table:
            rows.append((m.group(1), m.group(2)))
    return rows


def _documented_defaults():
    """{option: default-cell} from every option row of the configuration
    reference (the first, where an option appears twice)."""
    out = {}
    for name, cell in _option_rows(DOCS, headed_only=False):
        out.setdefault(name, cell)
    return out


def _every_documented_default():
    """[(where, option, default-cell)] for every option row the manual shows:
    the configuration reference, then any option table in DOCS.md."""
    return ([(DOCS.name, n, c) for n, c in _option_rows(DOCS, headed_only=False)]
            + [(GUIDE.name, n, c) for n, c in _option_rows(GUIDE, headed_only=True)])


def _norm(v: str) -> str:
    v = v.strip().strip("`").strip()
    if v in ('""', "''"):
        return ""
    if v and v[0] in "\"'" and v[-1] == v[0]:
        v = v[1:-1]
    return v.lower()


def test_both_files_parse():
    """Fail closed. A parser that silently finds nothing agrees with anything."""
    cfg, doc = _config_options(), _documented_defaults()
    assert len(cfg) > 40, f"config.yaml options block parsed to {len(cfg)} entries"
    assert len(doc) > 30, f"{DOCS.name} tables parsed to {len(doc)} entries"
    for expected in ("console_bind", "console_web_bind", "wakeup_ring_seconds"):
        assert expected in cfg, f"{expected} missing from the config parse"
        assert expected in doc, f"{expected} missing from the {DOCS.name} parse"


def test_a_guide_table_is_read_only_under_an_option_header(tmp_path):
    """The DOCS.md scan must see an option table and nothing else: a feature-code
    table also starts its rows with a backticked name."""
    page = tmp_path / "DOCS.md"
    page.write_text("| Dial | Feature |\n|---|---|\n| `41` | clock |\n\n"
                    "| Option | Default | What it does |\n|---|---|---|\n"
                    "| `wakeup_ring_seconds` | `45` | ring time |\n\nprose\n"
                    "| `console_bind` | `x` | not in a table with a header |\n")
    assert _option_rows(page, headed_only=True) == [("wakeup_ring_seconds", "`45`")]


def test_every_documented_default_matches_config_yaml():
    cfg = _config_options()
    mismatches = []
    for where, name, documented in sorted(_every_documented_default()):
        if name in PROSE_DEFAULTS or name not in cfg:
            continue
        actual = cfg[name]
        if name in DOCS_PENDING:
            stale, new = DOCS_PENDING[name]
            if _norm(documented) == _norm(stale) and _norm(actual) == _norm(new):
                continue
            mismatches.append(
                f"{name}: the DOCS_PENDING entry {DOCS_PENDING[name]!r} no longer "
                f"describes the files ({where} says {documented!r}, config.yaml has "
                f"{actual!r}). Delete the entry once the manual shows the default.")
            continue
        if _norm(documented) != _norm(actual):
            mismatches.append(f"{name}: {where} says {documented!r}, config.yaml has {actual!r}")
    assert not mismatches, (
        "the manual documents a default the schema does not ship:\n  "
        + "\n  ".join(mismatches))


def test_the_comparison_can_actually_fail():
    """Prove it discriminates rather than trusting its silence."""
    assert _norm("`0.0.0.0`") != _norm('`"127.0.0.1"`')
    assert _norm("`true`") == _norm("true")
    assert _norm('`""`') == _norm('""')


# --------------------------------------------------------------------------- #
# The security default, pinned by value.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("option", ["console_web_bind"])
def test_the_unauthenticated_console_listener_defaults_to_loopback(option):
    """★ The web terminal drives call control — ring, connect, hang up, page.

    It has never had authentication of its own worth the name (its
    `console_users` gate is opt-in and ships empty), and it used to default to
    every interface. Since 0.93.0 the browser console is served through the Home
    Assistant sidebar, where HA's own session authenticates it, so the web
    terminal does not need to be on the network at all.

    A change here puts an unauthenticated switchboard back on the LAN, so it
    must be deliberate enough to edit a test that says so.
    """
    value = _norm(_config_options()[option])
    assert value in ("127.0.0.1", "::1"), (
        f"{option} defaults to {value!r}. The web terminal must default to "
        f"loopback: it drives ring/connect/hang-up with no login until "
        f"console_users has an entry, and the authenticated console lives on "
        f"the Ingress port.")


def test_the_ssh_console_bind_is_on_the_lan_only_because_ssh_authenticates():
    """console_bind is the SSH console's bind, and defaults to every interface.

    That is safe only because SSH refuses every login without a console_users
    password; the board itself listens on a root-only Unix socket. Two things
    would make it unsafe, and both are pinned here: an operator console run
    script that binds console_bind itself (a second, unauthenticated listener),
    and a web terminal whose blank bind follows console_bind onto the LAN.
    """
    assert _norm(_config_options()["console_bind"]) == "0.0.0.0"
    s6 = ROOT / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d"
    for svc in ("operator-console", "console-web"):
        code = "\n".join(ln for ln in (s6 / svc / "run").read_text().splitlines()
                         if not ln.lstrip().startswith("#"))
        assert "console_bind" not in code, (
            f"{svc}/run reads console_bind, the SSH bind that defaults to every "
            f"interface; only the SSH service may listen on it")


def test_the_bind_fallback_also_fails_closed():
    """The run script's terminal fallback, for a blank console_web_bind or a
    blank read during a config reload. It used to be 0.0.0.0 — so a transient
    blank read put the terminal on the LAN until the next restart — and later it
    followed console_bind, which is now the SSH bind and defaults to 0.0.0.0.

    Runs the script's REAL bind chain under bash with switchboard-opt stubbed:
    console_bind is set to every interface throughout, so a chain that still
    consulted it would land there."""
    import subprocess
    run = (ROOT / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "console-web" / "run").read_text()
    chain = run[run.index('BIND="$(switchboard-opt console_web_bind)"'):]
    chain = chain[:chain.index("export CONSOLE_WEB_BIND")]
    assert 'BIND="0.0.0.0"' not in chain, (
        "the console-web bind chain still falls back to all interfaces")
    for web_bind, want in (("", "127.0.0.1"), ("null", "127.0.0.1"),
                           ("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "0.0.0.0")):
        stub = ('switchboard-opt() { case "$1" in '
                'console_web_bind) printf %s "$WEB_BIND" ;; '
                'console_bind) printf %s 0.0.0.0 ;; esac; }\n')
        r = subprocess.run(["bash", "-c", stub + chain + '\nprintf "[%s]" "$BIND"'],
                           capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "WEB_BIND": web_bind})
        assert r.stdout == f"[{want}]", (
            f"console_web_bind {web_bind!r} resolved to {r.stdout!r}, expected "
            f"[{want}] (stderr: {r.stderr!r})")
