"""Guards the HA Configuration-tab translation file against drift.

    python3 switchboard/tests/test_translations.py

HA renders `translations/en.yaml` `configuration:` entries as the field label + helper
text for each option in config.yaml. A key that doesn't byte-match a config option is
silently ignored (the field falls back to its raw key); a missing key shows no help. So
this pins: every top-level option has a name+description, and there are no stray keys.
"""
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PASS (pyyaml unavailable — skipping translation completeness check)")
    raise SystemExit(0)

_ROOT = Path(__file__).resolve().parents[1]
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


def test_translation_completeness():
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text())
    tr = yaml.safe_load((_ROOT / "translations" / "en.yaml").read_text())
    opts = set((cfg.get("options") or {}).keys())
    conf = (tr or {}).get("configuration") or {}
    keys = set(conf.keys())

    missing = sorted(opts - keys)
    extra = sorted(keys - opts)
    check(f"translations: every option is translated (missing: {missing or 'none'})", not missing)
    check(f"translations: no stray/typo'd keys (extra: {extra or 'none'})", not extra)

    incomplete = [k for k, v in conf.items()
                  if not (isinstance(v, dict) and str(v.get("name", "")).strip() and str(v.get("description", "")).strip())]
    check(f"translations: each entry has name + description (incomplete: {incomplete or 'none'})", not incomplete)


if __name__ == "__main__":
    test_translation_completeness()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    sys.exit(1 if _failures else 0)


def test_addon_starts_in_the_services_phase() -> None:
    """Dial tone must not queue behind Home Assistant Core.

    The Supervisor default is startup: application, which holds the add-on until
    Core reports RUNNING. Asterisk, the GXW gateway and the SIP trunk have no
    dependency on Core, so that gate delays every extension -- and the 911 path --
    for no benefit: 66.4 s behind the first services-phase add-on on the
    2026-08-25 boot, and unbounded if Core stalls on a recorder migration over a
    dirty database or a wedged custom integration.

    Safe because the HA-facing pollers degrade instead of exiting when Core is
    absent (ha_client.set_state returns False rather than raising, and each
    poller retries next cycle). If that ever changes, this test should be
    revisited deliberately rather than silently reverted."""
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text())
    assert cfg.get("startup") == "services", (
        f"startup is {cfg.get('startup')!r}; 'application' queues the PBX behind "
        "HA Core reaching RUNNING")


def test_backup_hooks_are_declared() -> None:
    """Backups must flush this add-on's durable state before being copied.

    Every backup runs hot -- an audit found 18 "Stopping app_" lines in a window
    and none was Switchboard -- so the call-quality ledger and the durable
    Asterisk log are snapshotted mid-append. Going cold was rejected on purpose:
    it would drop dial tone, including the 911 path, for the ~15 s of image
    export, nightly. A torn trailing log line is the cheaper failure, and every
    reader already skips malformed lines."""
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text())
    assert cfg.get("backup") == "hot", (
        "cold backups would drop dial tone during every nightly snapshot")
    assert cfg.get("backup_pre") == "/usr/bin/switchboard-backup-pre", cfg.get("backup_pre")
    assert cfg.get("backup_post") == "/usr/bin/switchboard-backup-post", cfg.get("backup_post")
    for hook in ("switchboard-backup-pre", "switchboard-backup-post"):
        p = _ROOT / "rootfs" / "usr" / "bin" / hook
        assert p.is_file(), f"{hook} declared in config.yaml but not shipped"
    # ...and the image must make them executable, or the Supervisor's exec fails.
    dockerfile = (_ROOT / "Dockerfile").read_text()
    for hook in ("switchboard-backup-pre", "switchboard-backup-post"):
        assert f"chmod +x /usr/bin/{hook}" in dockerfile, f"{hook} not chmod +x in Dockerfile"


def test_backup_pre_hook_never_fails_a_backup() -> None:
    """The hook must exit 0 even when its state directory is missing.

    A hook that returns non-zero fails the Supervisor's backup. A backup that
    runs is worth far more than one blocked by a flush that had nothing to do,
    so every error path here is logged and swallowed."""
    import subprocess
    hook = str(_ROOT / "rootfs" / "usr" / "bin" / "switchboard-backup-pre")
    for state, why in (("/nonexistent-switchboard-state", "missing dir"),
                       ("/tmp", "populated dir")):
        r = subprocess.run([sys.executable, hook], capture_output=True, text=True,
                           env={"SWITCHBOARD_STATE": state, "PATH": "/usr/bin:/bin"})
        assert r.returncode == 0, f"{why}: exit {r.returncode}, stderr={r.stderr[:200]}"
        assert "switchboard-backup-pre" in r.stdout, f"{why}: no log line emitted"

    post = str(_ROOT / "rootfs" / "usr" / "bin" / "switchboard-backup-post")
    r = subprocess.run([sys.executable, post], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:200]


def test_backup_hooks_leave_evidence_outside_the_container() -> None:
    """Both hooks must record the backup window to a file under /share.

    A hook runs via `docker exec`, and an exec's stdout does NOT reach the
    container's main log stream -- so a hook that only prints is unverifiable
    from outside, and a hook that silently never runs looks identical to one
    that ran perfectly. /share is mapped writable and readable from the host
    side, so the stamp is the only evidence an audit can actually check.

    It also closes the gap that motivated the post-hook: the Supervisor log
    shows the image export starting, but nothing recorded when THIS add-on's
    state stopped being copied, so a torn ledger line could not be attributed
    to a backup window."""
    import json as _json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        stamp = str(Path(td) / "sub" / "window.jsonl")   # nested: hook must mkdir
        env = {"SWITCHBOARD_STATE": "/tmp", "SWITCHBOARD_BACKUP_STAMP": stamp,
               "PATH": "/usr/bin:/bin"}
        for hook in ("switchboard-backup-pre", "switchboard-backup-post"):
            r = subprocess.run(
                [sys.executable, str(_ROOT / "rootfs" / "usr" / "bin" / hook)],
                capture_output=True, text=True, env=env)
            assert r.returncode == 0, f"{hook}: {r.stderr[:200]}"

        recs = [_json.loads(l) for l in Path(stamp).read_text().splitlines() if l.strip()]
        phases = [r["phase"] for r in recs]
        assert phases == ["pre", "post"], phases
        assert all("ts" in r for r in recs), recs
        assert recs[0].get("synced_files", -1) >= 0, "pre must record what it flushed"

    # An unwritable stamp path must still not fail the backup.
    r = subprocess.run(
        [sys.executable, str(_ROOT / "rootfs" / "usr" / "bin" / "switchboard-backup-pre")],
        capture_output=True, text=True,
        env={"SWITCHBOARD_STATE": "/tmp",
             "SWITCHBOARD_BACKUP_STAMP": "/proc/cannot/write/here.jsonl",
             "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, f"unwritable stamp failed the backup: {r.stderr[:200]}"
