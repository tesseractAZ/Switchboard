"""Every test file's standalone entry point runs every test in it.

    python3 -m pytest switchboard/tests/test_standalone_runners.py
"""
import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
RUNNER = 'if __name__ == "__main__":\n    import _runner\n    raise SystemExit(_runner.run(globals()))\n'


def test_each_runner_is_the_shared_one_and_comes_last():
    files = [p for p in sorted(TESTS.glob("test_*.py"))
             if re.search(r'^if __name__ == "__main__":', p.read_text(), re.M)]
    assert len(files) >= 17, [p.name for p in files]
    for p in files:
        src = p.read_text()
        # A runner discovers tests from the module namespace at the moment it
        # runs, so anything defined below it is invisible to it.
        assert src.endswith(RUNNER), p.name
        assert len(re.findall(r'^if __name__ == "__main__":', src, re.M)) == 1, p.name
        # No hand-maintained list of tests left behind.
        assert not re.search(r"^def main\(", src, re.M), p.name


def test_no_test_definition_is_glued_into_another_line():
    # A test definition joined onto the end of a line can only parse as part of a
    # comment, and then the test silently stops existing: removing the old
    # main() blocks once did exactly that (v0.110.0 review).
    import io
    import tokenize
    for p in sorted(TESTS.glob("test_*.py")):
        toks = tokenize.generate_tokens(io.StringIO(p.read_text()).readline)
        bad = [t.start[0] for t in toks if t.type == tokenize.COMMENT and "def test_" in t.string]
        assert not bad, f"{p.name}: lines {bad}"
