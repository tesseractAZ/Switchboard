#!/usr/bin/env python3
"""
build-docs-docx.py — assemble Switchboard's Markdown docs into one printable
manual: an editable Microsoft Word (.docx) and, with --pdf, a reader-friendly
PDF alongside it (LibreOffice converts the .docx — no LaTeX needed).

Used in CI (.github/workflows/docs.yml) on every push/PR: build the manual and
upload it as an artifact, so a page that no longer converts cleanly fails the
PR — and reviewers get an offline copy of the branch's manual in both formats.
On a version tag (v*) the same generator attaches both files to the GitHub
Release, so every release ships its documentation.

It concatenates, in reading order:
    README.md                     (the tour + install / quick-start)
    switchboard/DOCS.md           (the operator guide the add-on's Documentation tab shows)
    switchboard/docs/reference/*  (every reference page, in the order its README.md index lists them)
    switchboard/SECURITY.md       (the security posture + reporting)
with a hard page break between each, strips any hand-maintained contents block
from DOCS.md (Pandoc's generated Word TOC is the single source of navigation),
and runs Pandoc with a title block + depth-2 TOC. The reference index page
itself is not included: it is a list of links, which the generated TOC replaces.

Links are rewritten for the merged document (see `link_merged()`): a link to a
heading in any merged page — relative, `#anchor`, or an absolute
github.com/tesseractAZ/Switchboard/blob/main/… URL — becomes an internal link
to that heading; a link to another file in the repository becomes an absolute
GitHub URL; an image becomes a repository-root-relative path Pandoc can embed.
Every heading gets an explicit id: its GitHub anchor, made unique across the
whole manual, because the pages share headings (README's "Quick start" and the
reference's "Quick start" would otherwise both answer to #quick-start).

The same Markdown helpers (`github_slug`, `heading_anchors`, `iter_links`,
`reference_pages`) back the link checks in switchboard/tests/test_docs_layout.py,
so the manual and the tests agree on what a heading's anchor is.

Pandoc notes:
  * `-f markdown` (NOT `gfm`) so the `{=openxml}` raw blocks carrying the Word
    page breaks pass through verbatim — `raw_attribute` is off in strict GFM.
    `+pipe_tables` keeps the feature-code tables; `+gfm_auto_identifiers` is a
    fallback for any heading this script did not give an id.
  * The title-block "date" is the version + source ref rather than a wall-clock
    timestamp, so the header is stable across rebuilds.

Requires the `pandoc` binary on PATH (CI installs it; `brew install pandoc`
locally). `--pdf` additionally needs `soffice`/`libreoffice`.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import zipfile
from pathlib import Path
from urllib.parse import unquote

TITLE = "Switchboard — Complete System Reference"
SUBTITLE_TMPL = "Self-hosted Asterisk + PJSIP phone system for analog home phones, as a Home Assistant add-on — v{version}"

# A Word hard page break, expressed as a Pandoc raw-openxml block.
PAGE_BREAK = '\n\n```{=openxml}\n<w:p><w:r><w:br w:type="page"/></w:r></w:p>\n```\n\n'

REPO_URL = "https://github.com/tesseractAZ/Switchboard"
REFERENCE_DIR = Path("switchboard") / "docs" / "reference"
REFERENCE_INDEX = REFERENCE_DIR / "README.md"

# An absolute link into this repository's main branch: the form DOCS.md has to
# use, because Home Assistant renders it outside GitHub. Group 1 is the path,
# group 2 the anchor.
REPO_LINK = re.compile(
    r"^https?://github\.com/tesseractAZ/Switchboard/(?:blob|tree)/main/([^?#]*)(?:\?[^#]*)?(?:#(.*))?$",
    re.I)
# Anything with a URI scheme (https:, mailto:, …) or protocol-relative.
_EXTERNAL = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//)")


# --------------------------------------------------------------------------- #
# Markdown helpers — shared with the link checks in the test suite.
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
# The `](target)` half of an inline link or image, with an optional title;
# `<…>` around the target is allowed. Matched without its `[text` half, so a
# link whose text wraps onto the previous line is still found.
_LINK_TARGET = re.compile(r"\]\(\s*(<[^>]*>|[^)\s]*)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
# A reference definition `[label]: target`, not a footnote `[^1]: text`.
_REF_DEF = re.compile(r"^ {0,3}\[(?!\^)[^\]]+\]:\s*(<[^>]*>|\S+)")
_HTML_ATTR = re.compile(r"""\b(href|src)\s*=\s*["']([^"']*)["']""", re.I)
_HTML_ANCHOR = re.compile(r"""<[a-zA-Z][^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""")


def _lines_outside_fences(md: str):
    """Yield (index, line, in_fence) for every line; fence lines count as inside."""
    fence = None
    for i, line in enumerate(md.split("\n")):
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
                yield i, line, True
                continue
            yield i, line, False
        else:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not line.strip()[len(m.group(1)):].strip():
                fence = None
            yield i, line, True


def _mask_code_spans(line: str) -> str:
    """The line with every inline code span blanked out, same length, so a match
    position in the result is a position in the original."""
    return _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)


def _is_image(masked: str, close: int) -> bool:
    """Whether the `]` at `close` ends an image's `![alt`: walk back to the `[`
    that opens it (the alt text may hold balanced brackets)."""
    depth = 0
    for j in range(close - 1, -1, -1):
        c = masked[j]
        if c == "]":
            depth += 1
        elif c == "[":
            if depth == 0:
                return j > 0 and masked[j - 1] == "!"
            depth -= 1
    return False


def _heading_text(raw: str) -> str:
    """A heading's rendered text, which is what GitHub builds the anchor from:
    code spans keep their content verbatim, links and emphasis keep only their
    text, HTML tags and backslash escapes go."""
    out = []
    pos = 0
    for m in _CODE_SPAN.finditer(raw):
        out.append(("t", raw[pos:m.start()]))
        code = m.group(2)
        if len(code) > 2 and code[0] == code[-1] == " " and code.strip():
            code = code[1:-1]                        # CommonMark strips one space each side
        out.append(("c", code))
        pos = m.end()
    out.append(("t", raw[pos:]))
    text = []
    for kind, s in out:
        if kind == "c":
            text.append(s)
            continue
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)                  # images: no text
        s = re.sub(r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\([^)]*\)", r"\1", s)  # links: their text
        s = re.sub(r"<[^>]+>", "", s)                               # HTML tags
        s = re.sub(r"(?<![\w])__([^_]+?)__(?![\w])", r"\1", s)        # __strong__
        s = re.sub(r"(?<![\w])_([^_]+?)_(?![\w])", r"\1", s)          # _emphasis_
        s = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", s)                 # backslash escapes
        s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text.append(s)
    return "".join(text).strip()


def github_slug(text: str) -> str:
    """The anchor GitHub gives a heading (before any -1/-2 duplicate suffix).

    github-slugger's rule: lowercase, delete everything that is not a letter, a
    mark, a decimal/letter number, a space, '-' or '_', then turn each space into
    '-'. Spaces are not collapsed, which is why "Dial plan & the send delay" is
    `dial-plan--the-send-delay`."""
    out = []
    for ch in _heading_text(text).lower():
        if ch == " ":
            out.append("-")
        elif ch in "-_":
            out.append(ch)
        else:
            cat = unicodedata.category(ch)
            if cat[0] in "LM" or cat in ("Nd", "Nl"):
                out.append(ch)
    return "".join(out)


def iter_headings(md: str):
    """Yield (line index, level, raw heading text) for every ATX and setext
    heading outside fenced code."""
    lines = list(_lines_outside_fences(md))
    for k, (i, line, fenced) in enumerate(lines):
        if fenced:
            continue
        m = _ATX.match(line)
        if m:
            yield i, len(m.group(1)), (m.group(2) or "").strip()
            continue
        m = _SETEXT.match(line)
        if m and k > 0:
            _, prev, prev_fenced = lines[k - 1]
            before_blank = k < 2 or not lines[k - 2][1].strip()
            if (not prev_fenced and prev.strip() and before_blank
                    and not re.match(r"^\s*(?:[-*+>|#]|\d+[.)]\s)", prev)):
                yield k - 1, 1 if m.group(1)[0] == "=" else 2, prev.strip()


def _anchored_headings(md: str):
    """[(line index, level, raw text, GitHub anchor)] with duplicate suffixes."""
    seen: dict[str, int] = {}
    out = []
    for i, level, raw in iter_headings(md):
        base = github_slug(raw)
        slug = base
        while slug in seen:
            seen[base] += 1
            slug = f"{base}-{seen[base]}"
        seen[slug] = 0
        out.append((i, level, raw, slug))
    return out


def heading_anchors(md: str) -> list[str]:
    """Every anchor a page offers on GitHub: one per heading, in order, plus any
    explicit HTML id/name outside fenced code."""
    anchors = [a for *_, a in _anchored_headings(md)]
    for _, line, fenced in _lines_outside_fences(md):
        if not fenced:
            anchors += _HTML_ANCHOR.findall(_mask_code_spans(line))
    return anchors


def iter_links(md: str):
    """Yield (line number, target, is_image) for every link outside code: inline
    links and images, reference definitions, and HTML href/src attributes."""
    for i, line, fenced in _lines_outside_fences(md):
        if fenced:
            continue
        masked = _mask_code_spans(line)
        for m in _LINK_TARGET.finditer(masked):
            yield i + 1, line[m.start(1):m.end(1)].strip("<>"), _is_image(masked, m.start())
        m = _REF_DEF.match(masked)
        if m:
            yield i + 1, line[m.start(1):m.end(1)].strip("<>"), False
        for m in _HTML_ATTR.finditer(masked):
            yield i + 1, line[m.start(2):m.end(2)], m.group(1).lower() == "src"


def resolve_link(source: Path, target: str, repo_root: Path):
    """Where a non-external link in `source` points: (path, anchor).

    `path` is None for a same-page `#anchor`; `anchor` is None when the link
    names none. Relative paths resolve against `source`'s directory, a leading
    '/' and an absolute blob/main URL against the repository root, the way
    GitHub resolves them. Nothing is checked for existence. Call `is_external()`
    first: an external link comes back as (None, None)."""
    m = REPO_LINK.match(target)
    if m:
        return (repo_root / unquote(m.group(1))).resolve(), (unquote(m.group(2)) if m.group(2) is not None else None)
    if _EXTERNAL.match(target):
        return None, None
    path_part, _, anchor = target.partition("#")
    anchor = unquote(anchor) if "#" in target else None
    path_part = unquote(path_part.split("?", 1)[0])
    if not path_part:
        return None, anchor
    base = repo_root if path_part.startswith("/") else source.parent
    return (base / path_part.lstrip("/")).resolve(), anchor


def is_external(target: str) -> bool:
    """A link outside this repository's main branch — not ours to check."""
    return bool(_EXTERNAL.match(target)) and not REPO_LINK.match(target)


def reference_pages(repo_root: Path) -> list[Path]:
    """The reference pages, in the order the reference index lists them.

    Raises if the index and the directory disagree, so a page can neither drop
    out of the manual nor be listed without existing."""
    ref_dir = (repo_root / REFERENCE_DIR).resolve()
    index = (repo_root / REFERENCE_INDEX).resolve()
    listed: list[Path] = []
    for _, target, is_image in iter_links(index.read_text(encoding="utf-8")):
        if is_image or is_external(target):
            continue
        path, _ = resolve_link(index, target, repo_root)
        if path is not None and path.suffix == ".md" and path.parent == ref_dir and path not in listed:
            listed.append(path)
    on_disk = {p.resolve() for p in ref_dir.glob("*.md")} - {index}
    missing = sorted(p.name for p in on_disk - set(listed))
    absent = sorted(p.name for p in set(listed) - on_disk)
    if missing or absent:
        raise RuntimeError(
            f"{REFERENCE_INDEX} is out of step with its directory: "
            f"not listed {missing}, listed but absent {absent}")
    return listed


def manual_sources(repo_root: Path) -> list[Path]:
    """Every page the manual is built from, in reading order."""
    root = repo_root.resolve()
    return ([root / "README.md", root / "switchboard" / "DOCS.md"]
            + reference_pages(root)
            + [root / "switchboard" / "SECURITY.md"])


# --------------------------------------------------------------------------- #
# The merged manual.
# --------------------------------------------------------------------------- #
def read_version(repo_root: Path) -> str:
    """Parse the add-on version from switchboard/config.yaml (same regex the
    deploy path uses)."""
    cfg = (repo_root / "switchboard" / "config.yaml").read_text(encoding="utf-8")
    m = re.search(r'^version:\s*"?([^"#\s]+)"?', cfg, re.M)
    if not m:
        raise SystemExit("could not parse version from switchboard/config.yaml")
    return m.group(1)


_CONTENTS = re.compile(r"^(?:#{1,6}[ \t]+(?:Table of Contents|Contents)[ \t]*|\*\*(?:Table of )?Contents\*\*[ \t]*)$",
                       re.M | re.I)


def strip_manual_toc(docs: str) -> str:
    """Remove a hand-maintained contents block so it does not duplicate Pandoc's
    generated one. Cuts from the contents anchor (a bold '**Contents**'
    paragraph or a 'Contents' / 'Table of Contents' heading) up to the next
    heading; title-agnostic so it survives chapter renames.

    A page with no contents block is left as it is. One that HAS the anchor but
    no heading after it is reported LOUDLY: until v0.81.0 this function matched
    only '## Table of Contents' while DOCS.md's list was a bold '**Contents**'
    paragraph, so it was a silent no-op and every manual carried two contents
    lists, one of them stale."""
    m_toc = _CONTENTS.search(docs)
    if not m_toc:
        return docs
    after = docs[m_toc.end():]
    nxt = next((i for i, lvl, _ in iter_headings(after)), None)
    if nxt is None:
        print("WARNING: a hand-maintained contents block was found but no heading "
              "follows it — the manual will carry a duplicate contents list.", file=sys.stderr)
        return docs
    rest = "\n".join(after.split("\n")[nxt:])
    return docs[: m_toc.start()].rstrip() + "\n\n" + rest


def _merged_ids(pages: list[tuple[Path, str]]):
    """{(page, GitHub anchor): manual-wide id} and {page: its first heading's id}.

    The id is the GitHub anchor wherever that is still free in the merged
    manual, so most links read exactly as on GitHub; a repeat gets the page's
    name appended."""
    used: set[str] = set()
    ids: dict[tuple[Path, str], str] = {}
    first: dict[Path, str] = {}
    for path, md in pages:
        for _, _, _, anchor in _anchored_headings(md):
            if not anchor:
                continue
            gid = anchor
            if gid in used:
                stem = github_slug(path.stem)
                gid = f"{anchor}--{stem}"
                n = 1
                while gid in used:
                    n += 1
                    gid = f"{anchor}--{stem}-{n}"
            used.add(gid)
            ids[(path, anchor)] = gid
            first.setdefault(path, gid)
    return ids, first


def link_merged(path: Path, target: str, is_image: bool, repo_root: Path,
                ids: dict, first: dict) -> str:
    """The target a link in `path` should have inside the merged manual.

    A heading in a merged page becomes `#id`, and a whole merged page its first
    heading. The reference index is not merged, so a link to it lands on the
    first reference page, where the reference starts. Any other file in the
    repository becomes an absolute GitHub URL, since no relative path exists
    beside a distributed .docx. An image becomes repository-root-relative: one
    spelling per file, because Pandoc keys embedded media by the literal path
    string and two spellings of the same PNG would embed it twice."""
    if is_external(target):
        return target
    dest, anchor = resolve_link(path, target, repo_root)
    if dest is None:                               # same page
        dest = path
    if dest == (repo_root / REFERENCE_INDEX).resolve() and not anchor:
        refs = [p for p in first if p.parent == dest.parent]
        if refs:
            dest = refs[0]
    if dest in first or (dest == path and anchor):
        if anchor:
            gid = ids.get((dest, anchor))
            if gid is None:
                print(f"WARNING: {path.relative_to(repo_root)}: no heading #{anchor} in "
                      f"{dest.relative_to(repo_root)}", file=sys.stderr)
                return f"#{first.get(dest, anchor)}"
            return f"#{gid}"
        return f"#{first[dest]}"
    try:
        rel = dest.relative_to(repo_root).as_posix()
    except ValueError:
        return target
    if is_image:
        return rel                                 # embedded via --resource-path
    kind = "tree" if dest.is_dir() else "blob"
    return f"{REPO_URL}/{kind}/main/{rel}" + (f"#{anchor}" if anchor else "")


def _rewrite(path: Path, md: str, repo_root: Path, ids: dict, first: dict) -> str:
    """Give every heading its manual-wide id and every link its merged target."""
    heading_ids = {i: ids.get((path, a)) for i, _, _, a in _anchored_headings(md)}
    lines = md.split("\n")
    for i, line, fenced in _lines_outside_fences(md):
        if fenced:
            continue
        if heading_ids.get(i):
            line = re.sub(r"(?:[ \t]+#+)?[ \t]*$", "", line) + f" {{#{heading_ids[i]}}}"
        masked = _mask_code_spans(line)
        out, pos = [], 0
        for m in _LINK_TARGET.finditer(masked):
            raw = line[m.start(1):m.end(1)]
            new = link_merged(path, raw.strip("<>"), _is_image(masked, m.start()),
                              repo_root, ids, first)
            out.append(line[pos:m.start(1)] + new)
            pos = m.end(1)
        out.append(line[pos:])
        lines[i] = "".join(out)
    return "\n".join(lines)


def assemble(repo_root: Path) -> str:
    repo_root = repo_root.resolve()
    pages = []
    for path in manual_sources(repo_root):
        md = path.read_text(encoding="utf-8").strip()
        if path.name == "DOCS.md":
            md = strip_manual_toc(md)
        pages.append((path, md))
    ids, first = _merged_ids(pages)
    return PAGE_BREAK.join(_rewrite(path, md, repo_root, ids, first) for path, md in pages)


# The manual's typefaces, pinned in the .docx itself. pandoc 3.7 (Ubuntu 26.04)
# changed its template's theme to Aptos Display / Aptos, which no Linux machine
# has, and stopped making headings bold: the first manual built on 26.04 came out
# with plain serif headings and proportional inline code while CI stayed green.
# Calibri and Cambria are what pandoc 3.1's template asked for; Word has them,
# and the PDF build renders them with their metric-compatible clones (Carlito,
# Caladea), which the docs workflow installs and checks.
THEME_FONTS = {"majorFont": "Calibri", "minorFont": "Cambria"}
BOLD_STYLES = ("Title",) + tuple(f"Heading{n}" for n in range(1, 10))


def pin_fonts(docx: Path) -> None:
    """Rewrite the theme's Latin typefaces and make the heading styles bold, in
    place. Raises if the template's shape changed (a theme font missing or
    ambiguous, or no Heading1 style), so a future pandoc fails the build instead
    of quietly un-pinning the fonts."""
    with zipfile.ZipFile(docx) as z:
        entries = [(info, z.read(info.filename)) for info in z.infolist()]
    seen = set()
    out = []
    for info, data in entries:
        if info.filename == "word/theme/theme1.xml":
            xml = data.decode("utf-8")
            for element, face in THEME_FONTS.items():
                pat = re.compile(rf'(<a:{element}>\s*<a:latin typeface=")[^"]*(")')
                xml, n = pat.subn(rf"\g<1>{face}\g<2>", xml)
                if n != 1:
                    raise RuntimeError(f"theme1.xml: expected one <a:{element}> latin typeface, found {n}")
            data = xml.encode("utf-8")
            seen.add("theme")
        elif info.filename == "word/styles.xml":
            xml = data.decode("utf-8")
            bolded = 0
            for sid in BOLD_STYLES:
                m = re.search(rf'(<w:style [^>]*w:styleId="{sid}"[^>]*>)(.*?)(</w:style>)', xml, re.S)
                if not m:
                    continue
                body = m.group(2)
                if "<w:rPr>" not in body:
                    body = body + "<w:rPr></w:rPr>"
                if not re.search(r"<w:b\s*/>", body):
                    body = body.replace("<w:rPr>", "<w:rPr><w:b/><w:bCs/>", 1)
                xml = xml[:m.start(2)] + body + xml[m.end(2):]
                bolded += 1
            if not re.search(r'w:styleId="Heading1"', xml):
                raise RuntimeError("styles.xml: no Heading1 style to make bold")
            data = xml.encode("utf-8")
            seen.add("styles")
        out.append((info, data))
    if seen != {"theme", "styles"}:
        raise RuntimeError(f"docx is missing {sorted({'theme', 'styles'} - seen)}")
    with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in out:
            z.writestr(info, data)


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble Switchboard docs into a .docx (+ optional .pdf)")
    ap.add_argument("--repo-root", default=None,
                    help="repo root (default: parent of this script's dir)")
    ap.add_argument("--version", default=None,
                    help="version string for title/filename (default: read from config.yaml)")
    ap.add_argument("--ref", default="local",
                    help="source ref/SHA to stamp into the title block")
    ap.add_argument("--output", default=None,
                    help="output .docx path (default: Switchboard-Documentation-v<version>.docx in CWD)")
    ap.add_argument("--pdf", action="store_true",
                    help="ALSO render a PDF next to the .docx (via LibreOffice headless; "
                         "errors if 'soffice' is not on PATH). Reader-friendly, opens anywhere.")
    args = ap.parse_args()

    if not shutil.which("pandoc"):
        print("error: pandoc not found on PATH (CI installs it; locally: brew install pandoc)",
              file=sys.stderr)
        return 2

    repo_root = Path(args.repo_root).resolve() if args.repo_root \
        else Path(__file__).resolve().parent.parent
    version = args.version or read_version(repo_root)
    output = Path(args.output).resolve() if args.output \
        else Path.cwd() / f"Switchboard-Documentation-v{version}.docx"

    combined = assemble(repo_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    combined_path = output.with_suffix(".combined.md")
    combined_path.write_text(combined, encoding="utf-8")

    # assemble() rewrites every image to a repository-root-relative path; the
    # switchboard/ root stays as a fallback for anything it did not touch.
    resource_path = os.pathsep.join([str(repo_root), str(repo_root / "switchboard")])
    cmd = [
        "pandoc", str(combined_path), "-o", str(output),
        "-f", "markdown+pipe_tables+gfm_auto_identifiers",
        "--standalone", "--toc", "--toc-depth=2",
        "--resource-path", resource_path,
        "--metadata", f"title={TITLE}",
        "--metadata", f"subtitle={SUBTITLE_TMPL.format(version=version)}",
        "--metadata", f"date=Version {version} · source ref {args.ref}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    combined_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)  # Pandoc warnings — non-fatal, surface in CI

    pin_fonts(output)
    size = output.stat().st_size
    print(f"wrote {output} ({size:,} bytes) — v{version} @ {args.ref}; theme fonts "
          f"{THEME_FONTS['majorFont']}/{THEME_FONTS['minorFont']}, bold headings")

    # Optional: a reader-friendly PDF, converted from the .docx we just built via
    # LibreOffice headless (no LaTeX). The .docx stays the editable primary; the
    # PDF opens/prints anywhere with no Word. Pandoc→PDF would need a TeX engine,
    # so reusing the .docx keeps identical layout + the Word TOC.
    if args.pdf:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            print("error: --pdf requested but 'soffice'/'libreoffice' not on PATH "
                  "(CI installs libreoffice; locally: brew install --cask libreoffice)",
                  file=sys.stderr)
            return 3
        pdf_out = output.with_suffix(".pdf")
        pcmd = [soffice, "--headless", "-env:UserInstallation=file:///tmp/switchboard-loprofile",
                "--convert-to", "pdf", "--outdir", str(pdf_out.parent), str(output)]
        pproc = subprocess.run(pcmd, capture_output=True, text=True)
        if pproc.returncode != 0 or not pdf_out.exists():
            sys.stderr.write(pproc.stdout + pproc.stderr)
            print("error: LibreOffice PDF conversion failed", file=sys.stderr)
            return pproc.returncode or 4
        print(f"wrote {pdf_out} ({pdf_out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
