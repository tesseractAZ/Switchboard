"""The changelog stays short enough for Home Assistant to open quickly.

    python3 -m pytest switchboard/tests/test_changelog.py

Home Assistant renders the add-on's CHANGELOG.md whole every time Changelog is
opened. At 207 releases (319 KB) that took long enough to look broken. The file
keeps the newest releases; older ones live in CHANGELOG-ARCHIVE.md, which the
Supervisor never reads. `python3 scripts/rotate-changelog.py` moves them.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]
REPO = ADDON.parent
CHANGELOG = ADDON / "CHANGELOG.md"
ARCHIVE = ADDON / "CHANGELOG-ARCHIVE.md"
MAX_RELEASES = 15
MAX_BYTES = 48 * 1024


def _versions(path: Path) -> list[str]:
    return re.findall(r"(?m)^## (\S+)\s*$", path.read_text(encoding="utf-8"))


def _key(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def test_the_changelog_is_short():
    assert len(_versions(CHANGELOG)) <= MAX_RELEASES, \
        "run: python3 scripts/rotate-changelog.py"
    assert CHANGELOG.stat().st_size <= MAX_BYTES, "run: python3 scripts/rotate-changelog.py"


def test_the_current_version_heads_the_changelog():
    cfg = re.search(r'(?m)^version:\s*"?([0-9.]+)"?', (ADDON / "config.yaml").read_text()).group(1)
    assert _versions(CHANGELOG)[0] == cfg


def test_no_release_is_lost_duplicated_or_out_of_order():
    recent, archived = _versions(CHANGELOG), _versions(ARCHIVE)
    assert recent and archived
    assert not set(recent) & set(archived), sorted(set(recent) & set(archived))
    allv = recent + archived
    assert len(allv) == len(set(allv)), "a release appears twice"
    assert allv == sorted(allv, key=_key, reverse=True), "releases out of order across the two files"
    assert allv[-1] == "0.1.0", "the history no longer reaches the first release"


def test_the_changelog_links_the_archive_absolutely():
    head = CHANGELOG.read_text(encoding="utf-8").split("\n## ", 1)[0]
    # Home Assistant renders this file outside GitHub, so a relative link would
    # lead nowhere there.
    assert "https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/CHANGELOG-ARCHIVE.md" in head
    assert f"Releases up to {_versions(ARCHIVE)[0]} are in the" in head


def test_the_rotation_script_moves_only_the_oldest_and_is_idempotent(tmp_path):
    scripts = tmp_path / "scripts"
    addon = tmp_path / "switchboard"
    scripts.mkdir()
    addon.mkdir()
    shutil.copy(REPO / "scripts" / "rotate-changelog.py", scripts)
    secs = "".join(f"## 0.{n}.0\n\n- change {n}\n\n" for n in range(12, 0, -1))
    (addon / "CHANGELOG.md").write_text("# Changelog\n\n" + secs)
    run = lambda: subprocess.run([sys.executable, str(scripts / "rotate-changelog.py"), "--keep", "10"],
                                 capture_output=True, text=True, check=True).stdout
    assert "moved 2" in run()
    assert _versions(addon / "CHANGELOG.md") == [f"0.{n}.0" for n in range(12, 2, -1)]
    assert _versions(addon / "CHANGELOG-ARCHIVE.md") == ["0.2.0", "0.1.0"]
    before = ((addon / "CHANGELOG.md").read_text(), (addon / "CHANGELOG-ARCHIVE.md").read_text())
    assert "moved 0" in run()
    assert before == ((addon / "CHANGELOG.md").read_text(), (addon / "CHANGELOG-ARCHIVE.md").read_text())
