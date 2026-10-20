"""The manual as the content checks read it: DOCS.md and every reference page.

Home Assistant's Documentation tab shows DOCS.md, a short guide; the detail
lives in docs/reference/. A claim the tests require the manual to make may sit
in either, so they read both. Which page holds what is the link checks' concern
(test_docs_layout.py), not theirs.
"""
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]
REFERENCE = ADDON / "docs" / "reference"


def manual_pages() -> list[Path]:
    pages = [ADDON / "DOCS.md", *sorted(REFERENCE.glob("*.md"))]
    # Fail closed: an empty glob would make every "the manual says X" check
    # read only DOCS.md, and every "the manual never says Y" check pass.
    assert len(pages) > 10, f"the reference pages are missing from {REFERENCE}"
    return pages


def manual_text() -> str:
    return "\n\n".join(p.read_text(encoding="utf-8") for p in manual_pages())
