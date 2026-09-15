"""Pins the add-on's base image to ONE source, the Dockerfile, for every builder.

    python3 switchboard/tests/test_build_config.py

The add-on is built ON THE DEVICE (config.yaml has no `image:`), by the
Supervisor, from this directory. On the 2026-09-14 update to v0.100.6 the
Supervisor logged:

    App ..._switchboard uses build.yaml which is deprecated. Move build
    parameters into the Dockerfile directly.

build.yaml was then the only place the base image was written; the Dockerfile
declared a bare `ARG BUILD_FROM` for the Supervisor to fill in. Since Supervisor
2026.04.0 (home-assistant/supervisor#6694) BUILD_FROM is passed ONLY while a
build.yaml exists, so the release that drops that compatibility path would have
blanked both FROM lines and failed every on-device update. The running container
survives a failed build (the Supervisor builds before it stops it), so the
symptom is an update that never lands, not an outage, which is exactly the kind
of failure nobody notices.

What this pins, each against the builder that reads it:

  * no build.{yaml,yml,json} ANYWHERE under the add-on directory. The Supervisor
    finds one with a recursive `**/build.*` glob, and its presence both logs the
    warning and re-injects BUILD_FROM;
  * no Dockerfile.<arch>, which the Supervisor would build instead;
  * every FROM resolves, from the Dockerfile's global ARG defaults alone, to one
    non-empty, pinned, multi-platform base, through an ARG no builder injects.
    Not BUILD_FROM: Supervisors before 2026.04.0 always pass it, and with no
    build.yaml they fall back to an unpinned {arch}-base:latest;
  * CI and the release workflow pass no base image, so they build exactly what
    the device builds, for exactly the architectures config.yaml declares;
  * the labels build.yaml carried survived the move.
"""
import re
import shlex
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_DOCKERFILE = _ROOT / "Dockerfile"
_WORKFLOWS = _ROOT.parent / ".github" / "workflows"
# The workflows that build this Dockerfile. Named, not discovered, so a rename
# fails here instead of silently shrinking the set this file checks.
_BUILD_WORKFLOWS = ("ci.yml", "publish-release.yml")

# The Dockerfile's base-image ARG (its header says why it is not BUILD_FROM).
_BASE_ARG = "SWITCHBOARD_BASE_IMAGE"
# Build args a builder injects from OUTSIDE the Dockerfile, so a FROM keyed on
# one is not single-sourced. supervisor/apps/build.py get_docker_args(): every
# Supervisor passes BUILD_ARCH and BUILD_VERSION; BUILD_FROM is passed by every
# Supervisor before 2026.04.0, and by later ones while a build.yaml exists.
_INJECTED = ("BUILD_FROM", "BUILD_ARCH", "BUILD_VERSION")
# supervisor/const.py FILE_SUFFIX_CONFIGURATION, which find_one_filetype() uses
# to locate the build file.
_BUILD_FILE_SUFFIXES = (".yaml", ".yml", ".json")
# Anything in a workflow that reads or passes a base image outside the Dockerfile.
_WORKFLOW_NEEDLES = ("build.yaml", "build_from", "BUILD_FROM", _BASE_ARG)
# Architecture-prefixed HA base repos (amd64-base, aarch64-base, ...) hold one
# platform each; one Dockerfile built for several arches needs the
# multi-platform name.
_ARCH_PREFIXED_REPO = re.compile(r"(?:^|/)(?:amd64|aarch64|armv7|armhf|i386)-[^/]*$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

_failures = 0


def check(name, cond):
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    # Under pytest the print + counter are DECORATIVE — only the __main__
    # runner reads _failures, so a failing check would still 'pass' the
    # test. Assert too, so both harnesses actually enforce every check.
    assert cond, name


def _instructions(text):
    """(KEYWORD, arguments) for each Dockerfile instruction.

    Joins `\\` continuations and drops comment and blank lines, including the
    ones inside a continuation, as BuildKit does.
    """
    out, buf = [], []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.endswith("\\"):
            buf.append(s[:-1].strip())
            continue
        buf.append(s)
        keyword, _, rest = " ".join(buf).partition(" ")
        out.append((keyword.upper(), rest.strip()))
        buf = []
    return out


def _resolve(expr, args):
    """Expand ${NAME} / $NAME from `args`; None if any reference has no value."""
    missing = []

    def sub(m):
        value = args.get(m.group(1) or m.group(2))
        if not value:
            missing.append(m.group(0))
            return ""
        return value

    out = _VAR.sub(sub, expr)
    return None if (missing or "$" in out) else out


def _parse_dockerfile(text=None):
    """(global ARG defaults, [(image expression, resolved, is_stage_ref)] per FROM).

    Only an ARG declared BEFORE the first FROM is in scope for a FROM line; one
    declared inside a stage is invisible to it, so it is deliberately not
    collected. An ARG with no default maps to None.
    """
    if text is None:
        text = _DOCKERFILE.read_text()
    global_args, froms, stages, seen_from = {}, [], set(), False
    for keyword, rest in _instructions(text):
        if keyword == "ARG" and not seen_from:
            name, eq, default = rest.partition("=")
            global_args[name.strip()] = default.strip().strip('"') if eq else None
        elif keyword == "FROM":
            seen_from = True
            toks = [t for t in rest.split() if not t.startswith("--")]
            expr = toks[0]
            froms.append((expr, _resolve(expr, global_args), expr in stages))
            if len(toks) >= 3 and toks[1].upper() == "AS":
                stages.add(toks[2])
    return global_args, froms


def _config_arches():
    return list(yaml.safe_load((_ROOT / "config.yaml").read_text())["arch"])


def _strings(node):
    """Every string scalar in a parsed YAML document. Comments are not in it."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def test_no_build_file_where_the_supervisor_looks():
    found = sorted(str(p.relative_to(_ROOT)) for p in _ROOT.glob("**/build.*")
                   if p.suffix in _BUILD_FILE_SUFFIXES)
    check(f"no Supervisor build file anywhere under the add-on directory {found}",
          not found)


def test_no_per_arch_dockerfile():
    shadows = sorted(p.name for p in _ROOT.glob("Dockerfile.*"))
    check(f"no Dockerfile.<arch> that the Supervisor would build instead {shadows}",
          not shadows)


def test_every_from_is_the_one_pinned_multiplatform_base():
    args, froms = _parse_dockerfile()
    external = [(expr, resolved) for expr, resolved, is_stage in froms if not is_stage]
    # Two: the whisper.cpp builder stage and the add-on image. They must share a
    # base, or the static whisper-cli is linked against a different Alpine than
    # the libstdc++ it runs with.
    check(f"parsed the Dockerfile's base FROM lines ({len(external)})", len(external) >= 2)

    check(f"{_BASE_ARG} is not a name any builder injects", _BASE_ARG not in _INJECTED)
    check("the Dockerfile declares no global ARG BUILD_FROM", "BUILD_FROM" not in args)
    for expr, resolved in external:
        check(f"FROM {expr} names the base ARG rather than a copy of its value",
              expr in ("${%s}" % _BASE_ARG, "$" + _BASE_ARG))
        check(f"FROM {expr} resolves from the Dockerfile alone ({resolved!r})",
              bool(resolved))

    base = args.get(_BASE_ARG) or ""
    check(f"{_BASE_ARG} is a global ARG with a default ({base!r})", bool(base))
    repo, _, tag = base.rpartition(":")
    check(f"the base is a multi-platform repo, not an {{arch}}-base ({repo!r})",
          bool(repo) and not _ARCH_PREFIXED_REPO.search(repo))
    check(f"the base tag is pinned to a release, not latest ({tag!r})",
          bool(re.fullmatch(r"\d+\.\d+(?:\.\d+)?", tag)))


def test_workflows_build_what_the_device_builds():
    arches = set(_config_arches())
    check(f"config.yaml declares architectures {sorted(arches)}", bool(arches))

    for wf in _BUILD_WORKFLOWS:
        doc = yaml.safe_load((_WORKFLOWS / wf).read_text())
        builds = [(name, job, step)
                  for name, job in (doc.get("jobs") or {}).items()
                  for step in (job.get("steps") or [])
                  if str(step.get("uses", "")).startswith("docker/build-push-action@")]
        check(f"{wf}: found its docker build step(s) ({len(builds)})", len(builds) >= 1)
        for name, job, step in builds:
            w = step.get("with") or {}
            check(f"{wf}/{name}: builds the add-on directory's Dockerfile",
                  w.get("context") == "./switchboard"
                  and w.get("file") == "./switchboard/Dockerfile")
            keys = {ln.split("=", 1)[0].strip()
                    for ln in str(w.get("build-args") or "").splitlines() if "=" in ln}
            # Fail closed: an unparsed build-args block would make the next check
            # pass over an empty set.
            check(f"{wf}/{name}: build-args parsed {sorted(keys)}", "BUILD_ARCH" in keys)
            passed = sorted(keys & {"BUILD_FROM", _BASE_ARG})
            check(f"{wf}/{name}: passes no base image {passed}", not passed)
            matrix = ((job.get("strategy") or {}).get("matrix") or {}).get("arch")
            check(f"{wf}/{name}: builds every config.yaml arch ({matrix})",
                  set(matrix or ()) == arches)

    # ...and no workflow at all still reads build.yaml or hands a base image to a
    # build some other way (tag-release, docs and CodeQL included).
    every = sorted(_WORKFLOWS.glob("*.y*ml"))
    check(f"found the workflows ({len(every)})", len(every) >= len(_BUILD_WORKFLOWS))
    for path in every:
        strings = list(_strings(yaml.safe_load(path.read_text())))
        hits = sorted({n for s in strings for n in _WORKFLOW_NEEDLES if n in s})
        check(f"{path.name}: nothing reads or passes a base image outside the "
              f"Dockerfile {hits}", not hits)


def test_build_yaml_labels_moved_into_the_dockerfile():
    labels = {}
    for keyword, rest in _instructions(_DOCKERFILE.read_text()):
        if keyword == "LABEL":
            for tok in shlex.split(rest):
                key, eq, value = tok.partition("=")
                if eq:
                    labels[key] = value
    # build.yaml's `labels:`, which the Supervisor used to pass as --label.
    want = {
        "org.opencontainers.image.title": "Switchboard",
        "org.opencontainers.image.description": "Asterisk PBX for analog home phones",
        "org.opencontainers.image.source": "https://github.com/tesseractAZ/Switchboard",
    }
    for key, value in want.items():
        check(f"LABEL {key}={value!r} ({labels.get(key)!r})", labels.get(key) == value)
    check("LABEL io.hass.version is still declared", bool(labels.get("io.hass.version")))


def test_the_parser_reads_from_the_way_buildkit_does():
    """The checks above are only as good as _parse_dockerfile's reading of FROM.

    BuildKit resolves a FROM from ARGs declared before the first FROM; an ARG
    redeclared inside a stage does not reach a later FROM (docs.docker.com,
    "Understand how ARG and FROM interact"). The real Dockerfile has no such
    redeclaration, so a parser that ignored the rule would pass it and every
    check above would stay green. A mutation that dropped the rule did exactly
    that, so the rule is pinned here on a Dockerfile written to exercise it.
    """
    args, froms = _parse_dockerfile(
        "ARG BASE=ghcr.io/home-assistant/base:3.21\n"
        "FROM ${BASE} AS one\n"
        "ARG BASE=ghcr.io/home-assistant/aarch64-base:3.21\n"
        "# a comment, then a FROM split across a continuation\n"
        "FROM \\\n    ${BASE}\n"
        "FROM one\n")
    check("parser: an in-stage ARG does not replace the global default "
          f"({args.get('BASE')!r})", args.get("BASE") == "ghcr.io/home-assistant/base:3.21")
    check("parser: both base FROMs resolve to the global default",
          [r for _, r, is_stage in froms if not is_stage]
          == ["ghcr.io/home-assistant/base:3.21"] * 2)
    check("parser: a FROM naming an earlier stage is a stage reference",
          [is_stage for _, _, is_stage in froms] == [False, False, True])
    _, froms = _parse_dockerfile("ARG BASE\nFROM ${BASE}\n")
    check("parser: an ARG with no default leaves its FROM unresolved", froms[0][1] is None)


if __name__ == "__main__":
    test_no_build_file_where_the_supervisor_looks()
    test_no_per_arch_dockerfile()
    test_every_from_is_the_one_pinned_multiplatform_base()
    test_workflows_build_what_the_device_builds()
    test_build_yaml_labels_moved_into_the_dockerfile()
    test_the_parser_reads_from_the_way_buildkit_does()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    sys.exit(1 if _failures else 0)
