"""Pins the runner image of every GitHub Actions job.

    python3 -m pytest switchboard/tests/test_workflow_runners.py

Every build and check runs on Ubuntu 26.04: `ubuntu-26.04`, and
`ubuntu-26.04-arm` for the native aarch64 image builds. Named versions, never
`ubuntu-latest`, so the image a job runs on changes only in a reviewed diff, not
on the day GitHub moves the alias (it becomes 26.04 on 2026-10-19).

The one exception is CodeQL. Its system requirements list Ubuntu 22.04 and 24.04
only (checked 2026-10-12), so its analysis job stays on `ubuntu-24.04` until 26.04
is listed. Both halves are pinned here: CodeQL may not move early, and nothing
else may stay behind with it.
"""
import re
from pathlib import Path

import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

RUNNER = "ubuntu-26.04"
NATIVE_MATRIX = "${{ matrix.arch == 'aarch64' && 'ubuntu-26.04-arm' || 'ubuntu-26.04' }}"
CODEQL_RUNNER = "ubuntu-24.04"

# Every job, by file. Named rather than counted, so a renamed or deleted job
# fails here instead of quietly shrinking what this file checks.
EXPECTED_JOBS = {
    "ci.yml": {"tests", "apparmor", "docker-smoke", "version-consistency"},
    "codeql.yml": {"analyze"},
    "dependabot-automerge.yml": {"merge"},
    "docs.yml": {"docs", "screenshots", "screenshots-refresh"},
    "publish-release.yml": {"resolve", "tests", "images", "release"},
    "tag-release.yml": {"tag"},
}
# The jobs that build the add-on image for both architectures on native runners.
NATIVE_BUILD_JOBS = {("ci.yml", "docker-smoke"), ("publish-release.yml", "images")}


def _jobs():
    out = []
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        for name, job in (doc.get("jobs") or {}).items():
            out.append((path.name, name, job))
    return out


def test_the_workflow_set_is_the_one_this_file_knows():
    found = {p.name for p in _WORKFLOWS.glob("*.yml")}
    assert found == set(EXPECTED_JOBS), sorted(found ^ set(EXPECTED_JOBS))
    by_file = {}
    for f, name, _ in _jobs():
        by_file.setdefault(f, set()).add(name)
    for f, names in EXPECTED_JOBS.items():
        assert by_file.get(f) == names, (f, sorted((by_file.get(f) or set()) ^ names))


def test_every_job_names_its_runner():
    for f, name, job in _jobs():
        assert "runs-on" in job, f"{f}:{name} has no runs-on"


def test_every_job_runs_on_ubuntu_26_04_except_codeql():
    seen = 0
    for f, name, job in _jobs():
        runs_on = job["runs-on"]
        if (f, name) == ("codeql.yml", "analyze"):
            assert runs_on == CODEQL_RUNNER, (f, name, runs_on)
            continue
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        if "arch" in matrix:
            assert runs_on == NATIVE_MATRIX, (f, name, runs_on)
            assert matrix["arch"] == ["amd64", "aarch64"], (f, name, matrix["arch"])
        else:
            assert runs_on == RUNNER, (f, name, runs_on)
        seen += 1
    assert seen == 13, seen


def test_both_image_builds_run_on_native_runners():
    native = {(f, name) for f, name, job in _jobs() if job["runs-on"] == NATIVE_MATRIX}
    # Both image builds, CI's smoke build and the release build, stay native.
    assert native == NATIVE_BUILD_JOBS, sorted(native ^ NATIVE_BUILD_JOBS)


def test_no_floating_or_older_runner_label_anywhere():
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        text = path.read_text()
        assert "ubuntu-latest" not in text, path.name
        for label in re.findall(r"ubuntu-\d\d\.\d\d(?:-arm)?", text):
            if path.name == "codeql.yml" and label == CODEQL_RUNNER:
                continue
            assert label in ("ubuntu-26.04", "ubuntu-26.04-arm"), (path.name, label)
