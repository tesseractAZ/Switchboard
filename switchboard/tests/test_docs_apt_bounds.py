"""Bounds and mirror choice for the documentation workflow's package installs.

    python3 -m pytest switchboard/tests/test_docs_apt_bounds.py

On 2026-11-19 the docs job's pandoc and LibreOffice installs took 10 and 16 min
against a normal 1: the runner's first apt mirror trickled packages at
53-215 kB/s. That mirror is regional and depends on the cloud the runner lands
on (azure.archive.ubuntu.com on Azure, <region>.ec2.archive.ubuntu.com on AWS),
and apt leaves it only when it fails outright, never when it is slow. Nothing
bounded the job, so a worse day would have held the check for GitHub's 6 h
default.

This file pins the repair in .github/workflows/docs.yml:
- the regional mirror is dropped, whichever cloud's it is, but only while the
  main archive stays listed;
- pandoc comes from the upstream .deb pinned by sha256, with apt as the fallback;
- every apt-get and dpkg command is bounded, every install is split into a
  bounded download and an install from the cache, every step is bounded, and
  every job's bound covers the sum of its steps' bounds.
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DOCS_YML = REPO / ".github" / "workflows" / "docs.yml"

# The slack a job bound keeps above the sum of its steps: runner set-up and
# the post-job steps, which no step bound covers.
JOB_SLACK_MIN = 5

APT_LINE = re.compile(
    r"^sudo timeout -k (\d+) (\d+) apt-get \$APT_OPTS (update|install -y( --download-only)?( --no-install-recommends)? (.+))$")
BOUND = re.compile(r"timeout -k (\d+) (\d+) ")
CURL = re.compile(r"curl .*--retry (\d+) --max-time (\d+) ")

# The mirror lists the runner images ship (format: one URL and a priority per
# line). The first line is the regional mirror.
AZURE_LIST = ("http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
              "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
              "https://security.ubuntu.com/ubuntu/\tpriority:3\n")
AWS_LIST = AZURE_LIST.replace("azure.archive", "us-east-2.ec2.archive")


def _doc():
    return yaml.safe_load(DOCS_YML.read_text())


def _steps(job):
    return _doc()["jobs"][job]["steps"]


def _step(job, name):
    found = [s for s in _steps(job) if s.get("name") == name]
    assert len(found) == 1, (job, name, len(found))
    return found[0]


def _lines(run):
    """The run script's commands, one per line, continuations joined."""
    out, cur = [], ""
    for raw in str(run).splitlines():
        line = raw.strip()
        if line.endswith("\\"):
            cur += line[:-1].strip() + " "
            continue
        out.append((cur + line).strip())
        cur = ""
    return [line for line in out if line]


def _mirror_patterns():
    run = _step("docs", "Prefer the main Ubuntu archive")["run"]
    guard = re.search(r"grep -q '([^']+)' \"\$f\"", run)
    shown = re.search(r"grep -E '([^']+)' \"\$f\"", run)
    dropped = re.search(r"sudo sed -i -E '\\#([^#]+)#d' \"\$f\"", run)
    assert guard and shown and dropped, run
    return guard.group(1), shown.group(1), dropped.group(1)


def _apply_mirror_step(text):
    guard, _, dropped = _mirror_patterns()
    if not re.search(guard, text):
        return text
    return "".join(line for line in text.splitlines(keepends=True) if not re.search(dropped, line))


def test_the_regional_mirror_is_dropped_on_either_cloud():
    for listing, regional in ((AZURE_LIST, "azure.archive.ubuntu.com"),
                              (AWS_LIST, "us-east-2.ec2.archive.ubuntu.com")):
        kept = _apply_mirror_step(listing)
        assert regional not in kept, kept
        assert kept.splitlines() == listing.splitlines()[1:], kept


def test_the_main_archive_and_security_mirrors_are_kept():
    kept = _apply_mirror_step(AZURE_LIST)
    assert "https://archive.ubuntu.com/ubuntu/" in kept
    assert "https://security.ubuntu.com/ubuntu/" in kept


def test_a_list_without_the_main_archive_is_left_alone():
    # Dropping the regional mirror then could leave apt with nothing, or with
    # only the security pocket.
    only_regional = "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n" \
                    "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
    assert _apply_mirror_step(only_regional) == only_regional


def test_the_log_shows_exactly_what_the_step_drops():
    _, shown, dropped = _mirror_patterns()
    assert shown == dropped


def test_the_mirror_step_runs_before_any_package_is_fetched():
    names = [s.get("name") for s in _steps("docs")]
    first_fetch = min(i for i, s in enumerate(_steps("docs"))
                      if re.search(r"apt-get|dpkg|curl", str(s.get("run", ""))))
    assert names[first_fetch] == "Prefer the main Ubuntu archive", names[first_fetch]
    # The job's one index update runs there, after the list changed.
    run = _step("docs", "Prefer the main Ubuntu archive")["run"]
    assert _lines(run)[-1].endswith("apt-get $APT_OPTS update"), _lines(run)[-1]
    for s in _steps("docs"):
        if s.get("name") != "Prefer the main Ubuntu archive":
            assert "apt-get $APT_OPTS update" not in str(s.get("run", "")), s.get("name")


def test_pandoc_is_the_pinned_upstream_deb_with_apt_as_a_visible_fallback():
    step = _step("docs", "Ensure pandoc")
    env = step["env"]
    url, sha = env["PANDOC_DEB_URL"], env["PANDOC_DEB_SHA256"]
    m = re.fullmatch(r"https://github\.com/jgm/pandoc/releases/download/([0-9.]+)/pandoc-\1-1-amd64\.deb", url)
    assert m, url
    # The version Ubuntu 26.04 packages, so the fallback builds the same manual.
    assert m.group(1) == "3.7.0.2", m.group(1)
    assert re.fullmatch(r"[0-9a-f]{64}", sha), sha
    run = step["run"]
    assert 'sha256sum -c -' in run and "$PANDOC_DEB_SHA256" in run
    # The fallback announces itself: a pin that stopped matching must not pass
    # as a green check with apt quietly doing the work.
    fallback = run.index("::warning::")
    assert fallback < run.index("apt-get"), run


def test_every_apt_and_dpkg_command_is_bounded_and_reads_apt_opts():
    doc = _doc()
    seen = 0
    for job_name, job in doc["jobs"].items():
        uses_apt = False
        for s in job["steps"]:
            for line in _lines(s.get("run", "")):
                if re.search(r"\btimeout\b.*\bsudo\b", line):
                    # `timeout … sudo apt-get`: the -k SIGKILL reaches sudo, which
                    # cannot pass it on, and apt-get outlives the bound.
                    raise AssertionError(f"{job_name}: timeout wraps sudo: {line}")
                if "apt-get" in line:
                    uses_apt = True
                    seen += 1
                    assert APT_LINE.match(line), f"{job_name}/{s.get('name')}: {line}"
                if re.search(r"\bdpkg\b", line):
                    assert re.search(r"sudo timeout -k \d+ \d+ dpkg -i ", line), line
        if uses_apt:
            opts = (job.get("env") or {}).get("APT_OPTS", "")
            for want in ("Acquire::Retries=3", "Acquire::http::Timeout=20",
                         "Acquire::https::Timeout=20", "DPkg::Lock::Timeout=60"):
                assert want in opts, (job_name, want)
    # Named rather than "at least one", so an apt-get that slipped out of the
    # pattern (and out of this check) shows as a count change: 9 in the docs
    # job, 3 in each screenshots job.
    assert seen == 15, seen


def test_every_install_downloads_first_then_installs_from_the_cache():
    # A bound that fires while dpkg unpacks leaves a half-configured package and
    # a dpkg still holding the lock. Downloading first puts the long, network-
    # bound part where a kill is harmless.
    for job_name, job in _doc()["jobs"].items():
        for s in job["steps"]:
            installs = [APT_LINE.match(line) for line in _lines(s.get("run", ""))
                        if "apt-get" in line and " install " in line]
            pending = {}
            for m in installs:
                pkgs = m.group(6)
                if m.group(4):
                    pending[pkgs] = True
                else:
                    assert pending.pop(pkgs, False), (job_name, s.get("name"), pkgs)
            assert not pending, (job_name, s.get("name"), pending)


def test_every_step_bound_covers_its_commands_bounds():
    for job_name, job in _doc()["jobs"].items():
        for s in job["steps"]:
            run = str(s.get("run", ""))
            need = sum(int(k) + int(n) for k, n in BOUND.findall(run))
            for retries, per_try in CURL.findall(run):
                need += (int(retries) + 1) * int(per_try)
            label = s.get("name") or s.get("uses")
            assert "timeout-minutes" in s, f"{job_name}/{label} is unbounded"
            assert need <= s["timeout-minutes"] * 60, (job_name, label, need, s["timeout-minutes"])


def test_every_job_bound_covers_the_sum_of_its_steps():
    for job_name, job in _doc()["jobs"].items():
        total = sum(s["timeout-minutes"] for s in job["steps"])
        assert job.get("timeout-minutes", 0) >= total + JOB_SLACK_MIN, (job_name, job.get("timeout-minutes"), total)
