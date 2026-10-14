#!/usr/bin/env python3
"""Keep switchboard/CHANGELOG.md short by moving older releases to the archive.

    python3 scripts/rotate-changelog.py [--keep 10]

Home Assistant renders the add-on's CHANGELOG.md whole every time Changelog is
opened. At 207 releases (319 KB) that took long enough to look broken, so the
file holds only the newest releases and everything older lives, newest first, in
switchboard/CHANGELOG-ARCHIVE.md, which the Supervisor never reads.

Moves every release section beyond the newest --keep to the top of the archive.
Idempotent: with nothing to move it changes nothing. Refuses to run when a
version would end up in both files.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "switchboard" / "CHANGELOG.md"
ARCHIVE = ROOT / "switchboard" / "CHANGELOG-ARCHIVE.md"
ARCHIVE_URL = "https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/CHANGELOG-ARCHIVE.md"
ARCHIVE_HEADER = ("# Changelog archive\n\n"
                  "Earlier Switchboard releases, newest first. The most recent ones are in\n"
                  "[CHANGELOG.md](CHANGELOG.md).\n\n")


def changelog_header(newest_archived: str) -> str:
    return ("# Changelog\n\n"
            f"Recent releases. Releases up to {newest_archived} are in the\n"
            f"[changelog archive]({ARCHIVE_URL}).\n\n")


def split(text: str) -> tuple[str, list[str]]:
    """(text before the first release, [release sections, each starting '## '])."""
    parts = re.split(r"(?m)^(?=## )", text)
    return parts[0], [p if p.endswith("\n") else p + "\n" for p in parts[1:]]


def version_of(section: str) -> str:
    return section.split("\n", 1)[0][3:].strip()


def rotate(keep: int, changelog: Path = CHANGELOG, archive: Path = ARCHIVE) -> int:
    _, recent = split(changelog.read_text(encoding="utf-8"))
    old_head, archived = split(archive.read_text(encoding="utf-8")) if archive.exists() else ("", [])
    moving = recent[keep:]
    both = {version_of(s) for s in recent} & {version_of(s) for s in archived}
    if both:
        raise SystemExit(f"refusing: in both files already: {sorted(both)}")
    if not moving and changelog.read_text(encoding="utf-8").startswith("# Changelog\n\nRecent releases."):
        return 0
    archived = moving + archived
    newest_archived = version_of(archived[0]) if archived else ""
    changelog.write_text(changelog_header(newest_archived) + "".join(recent[:keep]).rstrip("\n") + "\n",
                         encoding="utf-8")
    archive.write_text(ARCHIVE_HEADER + "".join(archived).rstrip("\n") + "\n", encoding="utf-8")
    return len(moving)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--keep", type=int, default=10, help="releases kept in CHANGELOG.md (default 10)")
    args = ap.parse_args()
    moved = rotate(args.keep)
    print(f"moved {moved} release(s) to {ARCHIVE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
