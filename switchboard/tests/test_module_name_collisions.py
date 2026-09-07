"""No module we ship may share a name with a package pip installs into the image.

    python3 -m pytest switchboard/tests/test_module_name_collisions.py

★ THE DEFECT THIS EXISTS FOR. Until v0.92.0 the console web terminal's protocol
helpers lived in ``console-web/wsproto.py`` — the exact top-level module name of
the PyPI package ``wsproto``, which is one of the two WebSocket implementations
uvicorn can be asked to use. Nothing was broken, because that package was not
installed. The collision was a tripwire lying in the path of a change nobody had
made yet.

WHY IT WOULD HAVE BEEN NASTY. It resolves DIFFERENTLY in the two places it
matters, so a green test run proves nothing about the container:

  * In the add-on, ``server.py`` puts its own directory FIRST on ``sys.path``
    (``sys.path.insert(0, ...)``), so the local file wins and uvicorn's
    ``--ws wsproto`` gets a module with no ``wsproto.Connection`` — an import-time
    crash in the process that serves the sidebar panel and every call-control
    route.
  * In CI the local file is loaded by explicit path under a distinct name, so the
    real package wins and every test passes.

A name collision is therefore invisible to exactly the harness that would have to
catch it. The guard has to be structural: compare the names we SHIP against the
names we INSTALL, and never mind which one happens to win today.

WHAT THIS DOES NOT COVER. Only top-level module names, and only for
distributions this file knows about — the ones named in the Dockerfile plus the
transitive imports listed below. It cannot see a package added to the image by
some other means. That is why it also fails when the Dockerfile's pip list stops
being parseable: a check that silently stops checking is the failure mode this
repo has hit more than once.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
SHIPPED = ROOT / "rootfs"

# Top-level modules pulled in transitively by the Dockerfile's direct pins. A
# shipped `h11.py` would break uvicorn just as surely as a shipped `uvicorn.py`,
# and nothing in the Dockerfile names it. Kept explicit rather than discovered,
# because the dev box does not have the container's site-packages.
TRANSITIVE = frozenset({
    "annotated_types", "anyio", "certifi", "click", "h11", "idna",
    "markupsafe", "pydantic", "pydantic_core", "sniffio", "starlette",
    "typing_extensions", "typing_inspection",
})

# Distribution name -> the top-level module it actually imports as, where they
# differ. Everything else is assumed to import as its own (normalised) name.
IMPORT_NAME = {"jinja2": "jinja2", "python-multipart": "multipart"}


def _installed_distributions():
    """Distribution names from every `pip install` in the Dockerfile.

    Deliberately strict: if this returns nothing the test FAILS rather than
    passing vacuously over an empty set.
    """
    text = DOCKERFILE.read_text()
    names = set()
    # Quoted requirement specs: "fastapi==0.115.*", "websockets==17.1"
    for m in re.finditer(r'"([A-Za-z][A-Za-z0-9._-]*)(?:\[[^\]]*\])?[=<>!~]{1,2}[^"]*"', text):
        names.add(m.group(1))
    return names


def _shipped_module_names():
    """Importable top-level names under rootfs/, i.e. every `<name>.py`.

    Directory packages are not included: nothing here ships an `__init__.py`,
    and a directory only shadows if it contains one.
    """
    out = {}
    for p in SHIPPED.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        out.setdefault(p.stem, []).append(p.relative_to(ROOT))
    return out


def test_the_dockerfile_pip_list_is_still_parseable():
    """The guard must not degrade into a no-op if the Dockerfile is reformatted."""
    dists = _installed_distributions()
    assert dists, (
        "no pip requirements parsed out of the Dockerfile — the collision check "
        "below would pass over an empty set and prove nothing")
    # The three that have been there since the web UI shipped; if these stop
    # matching, the regex has drifted from the file.
    for expected in ("fastapi", "uvicorn", "jinja2"):
        assert expected in dists, f"{expected} not found by the parser: {sorted(dists)}"


def test_no_shipped_module_shadows_an_installed_package():
    """★ The invariant. A file we ship must not be importable under the name of a
    package the image installs — in EITHER direction, since which one wins
    depends on whose sys.path entry comes first."""
    dists = _installed_distributions()
    reserved = {IMPORT_NAME.get(d.lower(), d.lower().replace("-", "_")) for d in dists}
    reserved |= TRANSITIVE

    shipped = _shipped_module_names()
    clashes = [
        f"{name} — shipped at {', '.join(str(p) for p in paths)}"
        for name, paths in sorted(shipped.items())
        if name.lower() in reserved
    ]
    assert not clashes, (
        "these shipped modules share a name with a package installed into the "
        "image; whichever is first on sys.path wins, and that differs between "
        "the container and CI:\n  " + "\n  ".join(clashes))


def test_the_check_can_actually_see_a_collision():
    """A guard nobody has ever watched fail is not a guard.

    `test_no_shipped_module_shadows_an_installed_package` passes on a clean tree,
    which is also exactly what it would do if `_shipped_module_names()` returned
    nothing or the reserved set were empty. Prove it discriminates.
    """
    shipped = _shipped_module_names()
    assert len(shipped) > 20, f"only found {len(shipped)} shipped modules — the walk is broken"
    assert "server" in shipped, "console-web/server.py not seen by the walk"

    reserved = {"uvicorn", "fastapi"}
    fake = dict(shipped)
    fake["uvicorn"] = [Path("rootfs/usr/share/switchboard/webui/uvicorn.py")]
    hits = [n for n in fake if n.lower() in reserved]
    assert hits == ["uvicorn"], (
        "the comparison does not detect a planted collision, so its silence on "
        "the real tree means nothing")


def test_the_websocket_helpers_are_not_named_for_a_ws_package():
    """The specific rename, pinned by NAME rather than by the generic rule above.

    The generic test only fires once `wsproto` or `websockets` is actually in the
    Dockerfile. This module is the one that would be reached for when someone
    wires uvicorn to a real WebSocket implementation, so it must not carry either
    name even while neither package is installed.
    """
    cw = ROOT / "rootfs" / "usr" / "share" / "switchboard" / "console-web"
    assert cw.is_dir()
    present = {p.stem for p in cw.glob("*.py")}
    for forbidden in ("wsproto", "websockets", "websocket"):
        assert forbidden not in present, (
            f"console-web/{forbidden}.py shadows the PyPI package of the same "
            f"name; it is the module uvicorn would import for --ws")
    assert "consoleproto" in present, (
        "console-web/consoleproto.py is missing — server.py imports it by bare name")
