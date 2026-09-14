"""The published screenshots are rendered from fixtures — keep those fixtures
describing a state the live system can actually be in.

★ v0.100.5 taught the console that a board with no poll time has never been
polled, and to say "Connecting to the PBX…". The test suite's fixtures were fixed
in that release; scripts/build-screenshots.py was not, and the release workflow
re-rendered console.png from it. The README then showed "Connecting to the PBX…"
above a board reading "trunk Registered", with two rooms pushed off the bottom.
Nothing failed: the script is not a test, so nothing ran it before it published.
"""

from __future__ import annotations

import os
import re
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _console_text() -> tuple[str, int]:
    saved_path, saved_mods = list(sys.path), dict(sys.modules)
    saved_tz = os.environ.get("TZ")
    try:
        bs = SourceFileLoader("build_screenshots_under_test",
                              str(ROOT / "scripts/build-screenshots.py")).load_module()
        html = bs.build_console_html(ROOT)
        now = bs.NOW
    finally:
        # The script puts console/, webui/ and wakeup/ on sys.path, caches a module
        # named `console`, and pins TZ=UTC — none of which may outlive this test.
        sys.path[:] = saved_path
        for k in set(sys.modules) - set(saved_mods):
            del sys.modules[k]
        sys.modules.update(saved_mods)
        if saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved_tz
        time.tzset()
    return re.sub(r"<[^>]+>", "", html), now


def test_the_console_screenshot_shows_a_polled_board_not_a_connecting_banner():
    text, _ = _console_text()
    assert "Connecting to the PBX" not in text, "console.png would show the never-polled banner"
    assert "unreachable" not in text.lower(), "console.png would show the PBX as unreachable"
    assert "Registered" in text, "the fixture's trunk state did not render"
