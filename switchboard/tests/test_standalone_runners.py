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
