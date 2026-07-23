#!/usr/bin/env python3
"""
build-docs-docx.py — assemble Switchboard's Markdown docs into one printable
manual: an editable Microsoft Word (.docx) and, with --pdf, a reader-friendly
PDF alongside it (LibreOffice converts the .docx — no LaTeX needed).

Used in CI (.github/workflows/docs.yml) on every push/PR: build the manual and
upload it as an artifact, so a DOCS.md that no longer converts cleanly fails the
PR — and reviewers get an offline copy of the branch's manual in both formats.
On a version tag (v*) the same generator attaches both files to the GitHub
Release, so every release ships its documentation.

It concatenates, in reading order:
    README.md              (the tour + install / quick-start)
    switchboard/SECURITY.md (the security posture + reporting)
    switchboard/DOCS.md    (the full system reference)
with a hard page break between each, strips DOCS.md's hand-maintained
"## Table of Contents" (Pandoc's generated Word TOC is the single source of
navigation), and runs Pandoc with a title block + depth-2 TOC.

Pandoc notes:
  * `-f markdown` (NOT `gfm`) so the `{=openxml}` raw blocks carrying the Word
    page breaks pass through verbatim — `raw_attribute` is off in strict GFM.
    `+pipe_tables` keeps the feature-code tables, `+gfm_auto_identifiers` gives
    headings GitHub-style ids so the README's cross-references resolve as
    internal links once `internalize_links()` has stripped their path prefixes.
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
from pathlib import Path

TITLE = "Switchboard — Complete System Reference"
SUBTITLE_TMPL = "Self-hosted Asterisk + PJSIP phone system for analog home phones, as a Home Assistant add-on — v{version}"

# A Word hard page break, expressed as a Pandoc raw-openxml block.
PAGE_BREAK = '\n\n```{=openxml}\n<w:p><w:r><w:br w:type="page"/></w:r></w:p>\n```\n\n'


def read_version(repo_root: Path) -> str:
    """Parse the add-on version from switchboard/config.yaml (same regex the
    deploy path uses)."""
    cfg = (repo_root / "switchboard" / "config.yaml").read_text(encoding="utf-8")
    m = re.search(r'^version:\s*"?([^"#\s]+)"?', cfg, re.M)
    if not m:
        raise SystemExit("could not parse version from switchboard/config.yaml")
    return m.group(1)


def strip_manual_toc(docs: str) -> str:
    """Remove DOCS.md's hand-maintained '## Table of Contents' block so it does
    not duplicate Pandoc's generated one. Cuts from that heading up to the first
    numbered chapter heading ('## 1. ...'); title-agnostic so it survives
    chapter renames. If either anchor is missing, leaves the text untouched."""
    m_toc = re.search(r'^## Table of Contents\b', docs, re.M)
    m_ch1 = re.search(r'^## \d+\.\s', docs, re.M)
    if m_toc and m_ch1 and m_ch1.start() > m_toc.start():
        return docs[: m_toc.start()].rstrip() + "\n\n" + docs[m_ch1.start():]
    return docs


def internalize_links(md: str) -> str:
    """Rewrite in-repo cross-file Markdown links so the *merged* document links
    within itself rather than at relative paths that don't exist beside the
    distributed .docx.

    Two cases: (1) `](switchboard/DOCS.md#anchor)` / `](switchboard/SECURITY.md#anchor)`
    — strip the path prefix so the link becomes an internal `](#anchor)` (resolved
    by Pandoc's gfm_auto_identifiers); (2) whole-file links and the link to
    DOCS.md's stripped manual `#table-of-contents` — demote to plain text (their
    targets don't survive the merge). Fence-unaware: assumes these link forms
    appear only in prose, never inside a code block (true for this doc corpus)."""
    md = re.sub(r'\[([^\]]+)\]\(switchboard/DOCS\.md#table-of-contents\)', r'\1', md)
    md = re.sub(r'\[([^\]]+)\]\(switchboard/DOCS\.md\)', r'\1', md)
    md = re.sub(r'\[([^\]]+)\]\(switchboard/SECURITY\.md\)', r'\1', md)
    md = re.sub(r'\[([^\]]+)\]\(SECURITY\.md\)', r'\1', md)
    md = re.sub(r'\[([^\]]+)\]\(README\.md\)', r'\1', md)
    md = md.replace('](switchboard/DOCS.md#', '](#')
    md = md.replace('](switchboard/SECURITY.md#', '](#')
    md = md.replace('](SECURITY.md#', '](#')
    md = md.replace('](DOCS.md#', '](#')
    return md


def assemble(repo_root: Path) -> str:
    readme = (repo_root / "README.md").read_text(encoding="utf-8").strip()
    security = (repo_root / "switchboard" / "SECURITY.md").read_text(encoding="utf-8").strip()
    docs = strip_manual_toc((repo_root / "switchboard" / "DOCS.md").read_text(encoding="utf-8").strip())
    return internalize_links(readme + PAGE_BREAK + security + PAGE_BREAK + docs)


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
    combined_path = output.with_suffix(".combined.md")
    combined_path.write_text(combined, encoding="utf-8")

    # Images are referenced repo-root-relative in README (switchboard/docs/img/…)
    # and switchboard-relative in DOCS.md (docs/img/…); give pandoc both roots so
    # every screenshot embeds into the .docx regardless of which file cited it.
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

    size = output.stat().st_size
    print(f"wrote {output} ({size:,} bytes) — v{version} @ {args.ref}")

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
