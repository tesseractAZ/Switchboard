"""Pins the runner image of every GitHub Actions job.

    python3 -m pytest switchboard/tests/test_workflow_runners.py

Every build and check runs on Ubuntu 26.04: `ubuntu-26.04`, and
`ubuntu-26.04-arm` for the native aarch64 image builds. Named versions, never
`ubuntu-latest`, so the image a job runs on changes only in a reviewed diff, not
on the day GitHub moves the alias (it becomes 26.04 on 2026-10-19).

CodeQL included, since v0.110.0: 24.04 is retired. CodeQL's published system
requirements still list only 22.04 and 24.04, so its Python job must prove each
run analysed something (``min_accepted``: at least one baseline finding
reproduced), and this file pins that too.
"""
import re
from pathlib import Path

import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

RUNNER = "ubuntu-26.04"
NATIVE_MATRIX = "${{ matrix.arch == 'aarch64' && 'ubuntu-26.04-arm' || 'ubuntu-26.04' }}"

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


def test_every_job_runs_on_ubuntu_26_04():
    seen = 0
    for f, name, job in _jobs():
        runs_on = job["runs-on"]
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        if "arch" in matrix:
            assert runs_on == NATIVE_MATRIX, (f, name, runs_on)
            assert matrix["arch"] == ["amd64", "aarch64"], (f, name, matrix["arch"])
        else:
            assert runs_on == RUNNER, (f, name, runs_on)
        seen += 1
    assert seen == 14, seen


def test_both_image_builds_run_on_native_runners():
    native = {(f, name) for f, name, job in _jobs() if job["runs-on"] == NATIVE_MATRIX}
    # Both image builds, CI's smoke build and the release build, stay native.
    assert native == NATIVE_BUILD_JOBS, sorted(native ^ NATIVE_BUILD_JOBS)


def test_no_floating_or_older_runner_label_anywhere():
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        text = path.read_text()
        assert "ubuntu-latest" not in text, path.name
        for label in re.findall(r"ubuntu-\d\d\.\d\d(?:-arm)?", text):
            assert label in ("ubuntu-26.04", "ubuntu-26.04-arm"), (path.name, label)


def test_codeql_must_prove_it_analysed_something():
    doc = yaml.safe_load((_WORKFLOWS / "codeql.yml").read_text())
    job = doc["jobs"]["analyze"]
    gate = [s for s in job["steps"] if "check-sarif.mjs" in str(s.get("run", ""))]
    assert len(gate) == 1, gate
    assert "--min-accepted ${{ matrix.language == 'python' && 1 || 0 }}" in gate[0]["run"], gate[0]["run"]


def test_codeql_matrix_keys_are_the_ones_main_was_analysed_with():
    # Code scanning names each analysis configuration after the matrix values
    # (".../analyze/language:python/sarif:python.sarif"). A new key renames the
    # configuration: pull requests can then no longer be compared with main, and
    # main's existing alerts, the dismissed ones included, belong to a
    # configuration that no longer runs.
    doc = yaml.safe_load((_WORKFLOWS / "codeql.yml").read_text())
    rows = doc["jobs"]["analyze"]["strategy"]["matrix"]["include"]
    assert [sorted(r) for r in rows] == [["language", "sarif"], ["language", "sarif"]], rows


def test_codeql_scans_pull_requests_in_full():
    # Diff-informed PR scans drop dataflow results whose source and sink are both
    # outside the diff; the PR then passes and main's full scan finds them later.
    doc = yaml.safe_load((_WORKFLOWS / "codeql.yml").read_text())
    env = doc["jobs"]["analyze"].get("env") or {}
    assert env.get("CODEQL_ACTION_DIFF_INFORMED_QUERIES") == "false", env


def test_the_gate_refuses_a_partial_scan(tmp_path):
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node is not installed here; CI runs this")
    gate = _WORKFLOWS.parent / "scripts" / "check-sarif.mjs"
    base = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "CodeQL", "rules": []}}, "results": []}]}
    full = tmp_path / "full.sarif"
    full.write_text(json.dumps(base))
    diff = json.loads(json.dumps(base)); diff["runs"][0]["properties"] = {"incrementalMode": ["diff-informed"]}
    partial = tmp_path / "partial.sarif"
    partial.write_text(json.dumps(diff))
    ext = json.loads(json.dumps(base)); ext["runs"][0]["tool"]["extensions"] = [{"name": "codeql-action/pr-diff-range"}]
    ranged = tmp_path / "ranged.sarif"
    ranged.write_text(json.dumps(ext))
    run = lambda p: subprocess.run([node, str(gate), str(p)], capture_output=True, text=True).returncode
    assert run(full) == 0
    assert run(partial) == 2
    assert run(ranged) == 2

