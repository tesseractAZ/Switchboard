"""The documentation's shape: a short DOCS.md, a complete reference index, and
links that resolve.

    python3 -m pytest switchboard/tests/test_docs_layout.py

Home Assistant renders the add-on's DOCS.md whole, client-side, every time
Documentation is opened. At 137 KB that took long enough to look broken, so the
detail moved into docs/reference/ and DOCS.md became a short guide. What keeps it
that way:

  * a size cap on DOCS.md;
  * a reference index (docs/reference/README.md) that lists every page, which is
    also the order the printable manual is built in;
  * a link check over every page, because moving sections between files is
    exactly the change that leaves links pointing at headings that moved. GitHub
    resolves a relative link against the linking file, and Home Assistant renders
    DOCS.md outside GitHub, where only an absolute URL leads anywhere, so a link
    from DOCS.md into the reference must be absolute — and is checked like any
    other.

Anchors follow GitHub's rule (scripts/build-docs-docx.py `github_slug`), the same
function the printable manual uses to build its internal links.
"""
import importlib.util
import os
import re
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]
REPO = ADDON.parent
REFERENCE = ADDON / "docs" / "reference"
GUIDE = ADDON / "DOCS.md"
_spec = importlib.util.spec_from_file_location("build_docs_docx", REPO / "scripts" / "build-docs-docx.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

# Home Assistant's Documentation tab renders this file on every open.
DOCS_MAX_BYTES = 32 * 1024


def _pages() -> list[Path]:
    """Every page whose links are checked."""
    pages = [REPO / "README.md", REPO / "SECURITY.md", GUIDE, ADDON / "SECURITY.md",
             ADDON / "PERFORMANCE.md", *sorted(REFERENCE.glob("*.md")),
             *sorted((REPO / "tools").glob("*.md"))]
    for p in pages:
        assert p.is_file(), f"{p.relative_to(REPO)} is gone; update this list"
    assert len([p for p in pages if p.parent == REFERENCE]) > 10, "the reference pages are missing"
    return pages


def _exists_exactly(path: Path) -> bool:
    """`path` exists inside the repository with exactly this spelling.

    GitHub's paths are case-sensitive and a Mac's are not: `SECURITY.md` beside
    `security.md` resolves locally and 404s on GitHub."""
    try:
        parts = path.relative_to(REPO).parts
    except ValueError:
        return False
    here = REPO
    for part in parts:
        if not here.is_dir() or part not in os.listdir(here):
            return False
        here = here / part
    return True


_ANCHORS: dict[Path, set] = {}


def _anchors(path: Path) -> set:
    if path not in _ANCHORS:
        _ANCHORS[path] = set(build.heading_anchors(path.read_text(encoding="utf-8")))
    return _ANCHORS[path]


def _link_problems(source: Path, md: str) -> list[str]:
    """Every link in `md` (as if it were `source`) that does not resolve: a
    missing file, or a missing heading in a Markdown file. A link to another
    site is not checked; an absolute link into this repository's main branch is
    checked like a relative one."""
    problems = []
    where = source.relative_to(REPO)
    for line, target, _ in build.iter_links(md):
        if not target:
            problems.append(f"{where}:{line}: empty link target")
            continue
        if build.is_external(target):
            continue
        dest, anchor = build.resolve_link(source, target, REPO)
        dest = dest or source
        if not _exists_exactly(dest):
            problems.append(f"{where}:{line}: {target} — no such file in the repository")
        elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
            problems.append(f"{where}:{line}: {target} — {dest.name} has no heading #{anchor}")
    return problems


# --------------------------------------------------------------------------- #
# DOCS.md stays short.
# --------------------------------------------------------------------------- #
def test_docs_md_is_small_enough_for_home_assistant():
    size = len(GUIDE.read_bytes())
    assert size <= DOCS_MAX_BYTES, (
        f"DOCS.md is {size:,} bytes, over the {DOCS_MAX_BYTES:,}-byte cap. Home "
        f"Assistant renders it whole on every Documentation click: move detail into "
        f"docs/reference and link to it.")


def test_docs_md_links_into_the_reference_absolutely():
    """Home Assistant shows DOCS.md outside GitHub, where a relative link to
    docs/reference/… resolves against Home Assistant's own URL and leads nowhere."""
    relative = [f"line {line}: {target}"
                for line, target, _ in build.iter_links(GUIDE.read_text(encoding="utf-8"))
                if not build.is_external(target) and not build.REPO_LINK.match(target)
                and (build.resolve_link(GUIDE, target, REPO)[0] or GUIDE).is_relative_to(REFERENCE)]
    assert not relative, (
        "DOCS.md links into docs/reference with a relative path; use "
        "https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/<page>.md "
        "instead:\n  " + "\n  ".join(relative))


# --------------------------------------------------------------------------- #
# The reference index lists every page.
# --------------------------------------------------------------------------- #
def test_the_reference_index_lists_every_page_and_nothing_else():
    index = REFERENCE / "README.md"
    listed = []
    for _, target, _ in build.iter_links(index.read_text(encoding="utf-8")):
        if not build.is_external(target):
            dest, _ = build.resolve_link(index, target, REPO)
            if dest is not None and dest.suffix == ".md":
                listed.append(dest.name)
    on_disk = sorted(p.name for p in REFERENCE.glob("*.md") if p.name != "README.md")
    assert len(on_disk) > 10, on_disk
    assert sorted(set(listed) - {"README.md"}) == on_disk, (
        f"docs/reference/README.md lists {sorted(set(listed))}, the directory holds "
        f"{on_disk}: list each page once, and only pages that exist")
    assert len(listed) == len(set(listed)), f"a page is listed twice: {listed}"
    # ...and the manual is built from that list, in that order.
    assert [p.name for p in build.reference_pages(REPO)] == listed


def test_the_docs_workflow_runs_on_every_change():
    """The manual draws on pages all over the repository; a `paths:` filter on
    the docs workflow that missed one would let a page that no longer converts
    merge with the build never having run."""
    import yaml
    doc = yaml.safe_load((REPO / ".github" / "workflows" / "docs.yml").read_text())
    on = doc.get("on", doc.get(True))          # YAML 1.1 reads a bare `on:` as True
    for event in ("push", "pull_request"):
        assert event in on, event
        assert not {"paths", "paths-ignore"} & set(on[event] or {}), (
            f"docs.yml filters its {event} trigger by path")
    run = " ".join(str(s.get("run", "")) for s in doc["jobs"]["docs"]["steps"])
    assert "scripts/build-docs-docx.py" in run


def test_the_manual_is_readme_guide_reference_then_security():
    names = [p.relative_to(REPO).as_posix() for p in build.manual_sources(REPO)]
    assert names[:2] == ["README.md", "switchboard/DOCS.md"], names
    assert names[-1] == "switchboard/SECURITY.md", names
    assert names[2:-1] == [p.relative_to(REPO).as_posix() for p in build.reference_pages(REPO)]


def test_reference_pages_refuses_an_index_out_of_step(tmp_path):
    """The build must not quietly drop a page the index forgot."""
    ref = tmp_path / "switchboard" / "docs" / "reference"
    ref.mkdir(parents=True)
    (ref / "a.md").write_text("# A\n")
    (ref / "b.md").write_text("# B\n")
    (ref / "README.md").write_text("- [A](a.md)\n")
    for index in ("- [A](a.md)\n", "- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n"):
        (ref / "README.md").write_text(index)
        try:
            build.reference_pages(tmp_path)
        except RuntimeError:
            continue
        raise AssertionError(f"accepted an index that disagrees with the directory: {index!r}")
    (ref / "README.md").write_text("- [B](b.md)\n- [A](a.md)\n")
    assert [p.name for p in build.reference_pages(tmp_path)] == ["b.md", "a.md"]


# --------------------------------------------------------------------------- #
# Every link resolves.
# --------------------------------------------------------------------------- #
def test_every_link_and_anchor_resolves():
    problems = []
    for page in _pages():
        problems += _link_problems(page, page.read_text(encoding="utf-8"))
    assert not problems, "links that lead nowhere:\n  " + "\n  ".join(problems)


def test_the_link_check_can_fail():
    """Prove it discriminates rather than trusting its silence."""
    src = REFERENCE / "codecs.md"
    blob = "https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference"
    md = "\n".join([
        "[ok](configuration.md#the-options-overlay-advanced)",
        "[ok](#codecs--g711-µ-law-only-on-purpose)",
        "[ok](../../SECURITY.md#5-the-announce-endpoint)",
        f"[ok]({blob}/sip-trunk.md#emergency-calls-are-not-carried)",
        "[ok](https://example.com/not-ours.md#nothing)",
        "[ok](https://github.com/tesseractAZ/Switchboard/releases)",
        "`[skipped](inside-a-code-span.md)`",
        "```",
        "[skipped](inside-a-fence.md)",
        "```",
        "[bad anchor](configuration.md#2-configuration-reference)",
        "[bad file](configuraton.md)",
        "[bad case](SECURITY.md)",
        "[bad same-page](#13-codecs--g711-µ-law-only-on-purpose)",
        f"[bad absolute]({blob}/sip-trunk.md#9-adding-an-outside-line-sip-trunk)",
        f"[bad absolute file]({blob}/sip_trunk.md)",
        "![bad image](docs/img/console.png)",
        "[^1]: a footnote, not a link",
        "[bad definition]: ./nowhere.md",
    ])
    problems = _link_problems(src, md)
    assert [p.split(": ", 1)[1].split(" — ")[0] for p in problems] == [
        "configuration.md#2-configuration-reference",
        "configuraton.md",
        "SECURITY.md",
        "#13-codecs--g711-µ-law-only-on-purpose",
        f"{blob}/sip-trunk.md#9-adding-an-outside-line-sip-trunk",
        f"{blob}/sip_trunk.md",
        "docs/img/console.png",
        "./nowhere.md",
    ], problems


def test_github_anchor_rules():
    """Pinned against anchors GitHub is known to generate: the old DOCS.md
    contents list linked these, and they worked."""
    for heading, anchor in (
            ("13. Codecs — G.711 µ-law only, on purpose", "13-codecs--g711-µ-law-only-on-purpose"),
            ("10. The operator console (SSH + browser)", "10-the-operator-console-ssh--browser"),
            ("12. How it's built", "12-how-its-built"),
            ("7.4 Dial plan & the send delay", "74-dial-plan--the-send-delay"),
            ("Link health (`link_health_*`)", "link-health-link_health_"),
            ("The SSH operator console (`:2300`) and web terminal (`:8100`)",
             "the-ssh-operator-console-2300-and-web-terminal-8100"),
            ("Toll-fraud (the trunk threat model)", "toll-fraud-the-trunk-threat-model"),
            ("A [linked](x.md) and _emphasised_ **word**", "a-linked-and-emphasised-word")):
        assert build.github_slug(heading) == anchor, heading
    md = "# Same\n\n## Same\n\n```\n# not a heading\n```\n\n## Same\n\n<a id=\"explicit\"></a>\n"
    assert build.heading_anchors(md) == ["same", "same-1", "same-2", "explicit"]


def test_reference_pages_cite_no_vanished_section_numbers():
    """DOCS.md used to be numbered, and its text said "see §9". The numbers left
    with the split, so a `§N` in a reference page must name a numbered heading
    on that same page (the gateway page keeps 7.1–7.5); anything else is a
    pointer to a section that no longer exists — link the page instead."""
    stale = []
    for page in sorted(REFERENCE.glob("*.md")):
        md = page.read_text(encoding="utf-8")
        numbered = {m.group(1).rstrip(".") for _, _, raw in build.iter_headings(md)
                    for m in [re.match(r"(\d+(?:\.\d+)*\.?)\s", raw)] if m}
        for m in re.finditer(r"(?<!RFC )(?<!RFC \d{4} )§\s?(\d+(?:\.\d+)*)", md):
            if m.group(1) not in numbered:
                stale.append(f"{page.name}: {m.group(0)}")
    assert not stale, "section numbers that no longer exist:\n  " + "\n  ".join(stale)


# --------------------------------------------------------------------------- #
# The printable manual's links stay inside it.
# --------------------------------------------------------------------------- #
def test_the_assembled_manual_links_only_to_its_own_headings_or_the_web(capsys):
    md = build.assemble(REPO)
    assert "WARNING" not in capsys.readouterr().err
    ids = re.findall(r"^#{1,6} .*\{#([^}\s]+)\}\s*$", md, re.M)
    assert len(ids) > 40, f"only {len(ids)} heading ids: the assembly lost its headings"
    assert len(ids) == len(set(ids)), "heading ids must be unique"
    for _, target, is_image in build.iter_links(md):
        if target.startswith("#"):
            assert target[1:] in ids, f"{target} is not a heading in the manual"
        elif is_image:
            assert (REPO / target).is_file(), f"image {target} does not embed"
        else:
            assert re.match(r"^(?:https?:|mailto:)", target), (
                f"{target} is relative, and nothing sits beside a distributed .docx")


def test_the_contents_block_is_stripped_and_its_absence_is_quiet(capsys):
    old = "# T\n\nintro\n\n**Contents**\n\n1. [A](#1-a)\n\n---\n\n## 1. A\n\ntext\n"
    assert build.strip_manual_toc(old) == "# T\n\nintro\n\n## 1. A\n\ntext\n"
    plain = "# T\n\nintro\n\n## Setup\n\ntext\n"
    assert build.strip_manual_toc(plain) == plain
    assert capsys.readouterr().err == ""


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
