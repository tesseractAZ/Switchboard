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


def test_the_dashboard_screenshot_serves_the_page_python_serves() -> None:
    """★ INDEX_HTML is an ordinary (non-raw) Python string, so Python processes
    its escapes before a browser sees the page: the source's `\\d` is served as
    `\d`. The builder copied the SOURCE TEXT, so the pictured page carried
    `\\d` — a JS regex matching nothing — and no room card in a published
    screenshot ever showed its pending wake-up, or the Cancel beside it. The very
    layout bug that button later had could not have shown up in a screenshot."""
    saved_path, saved_mods = list(sys.path), dict(sys.modules)
    saved_tz = os.environ.get("TZ")
    try:
        bs = SourceFileLoader("build_screenshots_dash",
                              str(ROOT / "scripts/build-screenshots.py")).load_module()
        html = bs.build_dashboard_html(ROOT)
    finally:
        sys.path[:] = saved_path
        for k in set(sys.modules) - set(saved_mods):
            del sys.modules[k]
        sys.modules.update(saved_mods)
        if saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved_tz
        time.tzset()
    assert r"/^\d{1,2}:\d{2}$/" in html, "the wake-up time regex is not served as Python serves it"
    assert "\\\\d" not in html, "the page still carries source-text escapes"


# The committed preview of the dashboard: the screenshot fixture, written to disk
# so the page can be opened in a browser with no PBX behind it. Its font urls are
# RELATIVE to the fixtures directory (never a checkout's absolute path), so it
# shows the real faces when opened from a checkout that has them.
PREVIEW = ROOT / "switchboard/tests/fixtures/dashboard_preview.html"
PREVIEW_FONT_BASE = "../../rootfs/usr/share/switchboard/fonts/"


def _dashboard_html(**kw) -> str:
    """build_dashboard_html(ROOT, **kw), leaving no trace of the script's
    sys.path, module cache or TZ behind."""
    saved_path, saved_mods = list(sys.path), dict(sys.modules)
    saved_tz = os.environ.get("TZ")
    try:
        bs = SourceFileLoader("build_screenshots_preview",
                              str(ROOT / "scripts/build-screenshots.py")).load_module()
        return bs.build_dashboard_html(ROOT, **kw)
    finally:
        sys.path[:] = saved_path
        for k in set(sys.modules) - set(saved_mods):
            del sys.modules[k]
        sys.modules.update(saved_mods)
        if saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved_tz
        time.tzset()


def _served_page() -> str:
    """INDEX_HTML as Python serves it (evaluated, not copied: see above)."""
    import ast
    src = (ROOT / "switchboard/rootfs/usr/share/switchboard/webui/app.py").read_text()
    m = re.search(r'^INDEX_HTML = ("""(?:.*?)^""")', src, re.S | re.M)
    assert m, "INDEX_HTML not found"
    return ast.literal_eval(m.group(1))


def test_the_dashboard_screenshot_does_not_restyle_the_page() -> None:
    """★ The builder pinned a light theme: it made the page's dark block
    unmatchable and appended a <style> forcing a light body. Once the page had
    one theme (the walnut board), that pin would have painted a grey body under
    it, and the docs would have pictured a page nobody is served. The fixture may
    add its stub script; it may not add or change a style."""
    html = _dashboard_html(font_base="fonts/")      # "fonts/" -> "fonts/": no-op
    page = _served_page()
    styles = re.findall(r"<style>.*?</style>", html, re.S)
    assert styles == re.findall(r"<style>.*?</style>", page, re.S), (
        "the screenshot fixture restyles the dashboard")
    assert html.count("<script>") == page.count("<script>") + 1, (
        "the fixture adds more than its one API stub")


def test_the_screenshot_loads_the_shipped_fonts_not_the_ingress_route() -> None:
    """The page asks for `fonts/<name>`, which only the web UI's route answers. A
    file:// screenshot has no route, so the builder points the faces at the
    repo's font directory instead; otherwise the docs would picture the fallback
    faces."""
    html = _dashboard_html()
    fonts = (ROOT / "switchboard/rootfs/usr/share/switchboard/fonts").as_uri() + "/"
    assert 'url("fonts/' not in html
    assert html.count('url("' + fonts) == 2


def test_the_committed_dashboard_preview_is_the_page_as_served() -> None:
    """The preview is checked, not trusted: a stale copy pictures a page nobody
    is served, which is the failure this file exists for. Regenerate with

        SWITCHBOARD_WRITE_PREVIEW=1 python3 -m pytest switchboard/tests/test_screenshot_fixtures.py
    """
    html = _dashboard_html(font_base=PREVIEW_FONT_BASE)
    if os.environ.get("SWITCHBOARD_WRITE_PREVIEW") == "1":
        PREVIEW.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW.write_text(html, encoding="utf-8")
    assert PREVIEW.is_file() and PREVIEW.read_text(encoding="utf-8") == html, (
        "tests/fixtures/dashboard_preview.html is stale; regenerate it with "
        "SWITCHBOARD_WRITE_PREVIEW=1 python3 -m pytest "
        "switchboard/tests/test_screenshot_fixtures.py")
    assert "file://" not in html and str(ROOT) not in html, (
        "the preview carries a checkout's absolute path")
