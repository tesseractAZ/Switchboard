"""The AppArmor profile's invariants, and the image facts it depends on.

    python3 -m pytest switchboard/tests/test_apparmor_profile.py

A profile the host parser rejects does not show in the add-on log: the add-on
starts under docker-default, or under the previously loaded profile until the
next host boot, when it fails to start. A deny on the wrong tree is an init
crash-loop. These tests pin the shape that was boot-tested; the CI job
`apparmor` compiles the file with a real apparmor_parser.
"""
import re
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]
PROFILE = (ADDON / "apparmor.txt").read_text()
RULES = [l.strip() for l in PROFILE.splitlines()
         if l.strip() and not l.strip().startswith("#")]

ALLOWED_CAPS = {"chown", "dac_override", "fowner", "fsetid", "kill",
                "setgid", "setuid", "net_bind_service"}
MUST_DENY_CAPS = {"net_raw", "setfcap", "sys_admin", "sys_module", "sys_rawio",
                  "sys_ptrace", "sys_boot", "sys_time", "syslog", "mac_admin",
                  "mac_override", "net_admin", "dac_read_search", "bpf", "perfmon"}
WRITE_LOCKED = ["/init", "/bin/{,**}", "/sbin/{,**}", "/lib/{,**}", "/usr/{,**}",
                "/var/lib/asterisk/{agi-bin,sounds,moh}/{,**}"]
# s6 writes or executes from these, Asterisk and the services write there, or
# they are the persistent volumes: a deny on any of them is a crash-loop or a
# broken feature, and none was boot-tested.
NEVER_DENIED = ("/run", "/tmp", "/data", "/share", "/config", "/command",
                "/package", "/etc/s6-overlay", "/etc/asterisk", "/var/run/asterisk",
                "/var/log/asterisk", "/var/spool/asterisk")
FILE_RULE = re.compile(r"^(audit\s+)?(deny\s+)?(/\S*)\s+([a-zA-Z]+),$")


def test_exactly_one_column_zero_profile_line_named_switchboard():
    # The Supervisor renames the first column-0 "profile <name>" line to the
    # slug and its loader refuses a file with more than one.
    heads = [l for l in PROFILE.splitlines() if l.startswith("profile ")]
    assert len(heads) == 1, heads
    assert heads[0].startswith("profile switchboard flags=(attach_disconnected,mediate_deleted")


def test_the_three_blanket_grants_stay():
    # Removing "file," crash-looped a sibling add-on: s6 re-opens /init for READ.
    for rule in ("file,", "signal,", "network,"):
        assert rule in RULES, f"{rule} is gone"
    assert "capability," not in RULES, "the blanket capability grant came back"


def test_no_mask_combines_append_and_write():
    # The parser rejects a mask holding both "a" and "w" — the profile then
    # fails to load and the add-on cannot start after the next host boot.
    for rule in RULES:
        m = FILE_RULE.match(rule)
        if m:
            perms = m.group(4)
            assert not ("a" in perms and "w" in perms), rule


def test_capabilities_are_an_explicit_list():
    allowed = {m.group(1) for r in RULES for m in [re.match(r"^capability (\w+),$", r)] if m}
    denied = {m.group(1) for r in RULES for m in [re.match(r"^(?:audit )?deny capability (\w+),$", r)] if m}
    assert allowed == ALLOWED_CAPS, sorted(allowed ^ ALLOWED_CAPS)
    assert MUST_DENY_CAPS <= denied, sorted(MUST_DENY_CAPS - denied)
    assert not (allowed & denied), sorted(allowed & denied)


def test_the_write_locks_are_present_and_wl_only():
    locks = {m.group(3): (m.group(1), m.group(4)) for r in RULES
             for m in [FILE_RULE.match(r)] if m and m.group(2)}
    for path in WRITE_LOCKED:
        assert path in locks, f"write-lock on {path} is gone"
        audit, perms = locks[path]
        assert perms == "wl", f"{path} mask is {perms!r}, must be exactly 'wl'"
        assert audit, f"{path} must be 'audit deny' so a firing rule is visible"


def test_no_deny_touches_the_trees_that_must_stay_writable():
    for rule in RULES:
        m = FILE_RULE.match(rule)
        if not (m and m.group(2)):
            continue
        path = m.group(3)
        for root in NEVER_DENIED:
            assert not (path == root or path.startswith(root + "/") or path.startswith(root + "{")), rule
        # Only the named subtrees of /var/lib/asterisk; the top level holds astdb.
        if path.startswith("/var/lib/asterisk"):
            assert path == "/var/lib/asterisk/{agi-bin,sounds,moh}/{,**}", rule


def test_ptrace_denies_trace_only_and_no_network_deny_without_abi():
    # A bare "deny ptrace," would silently take away same-profile ptrace read.
    assert "deny ptrace," not in RULES and "audit deny ptrace," not in RULES
    assert "audit deny ptrace (trace)," in RULES
    # Without an abi line network rules are not enforced; a network deny would
    # look like protection and be none.
    if any(re.match(r"^(audit )?deny network", r) for r in RULES):
        assert any(r.startswith("abi ") for r in RULES)


def test_the_image_never_writes_bytecode_under_usr():
    # The /usr write-lock relies on it: otherwise every import tries to create a
    # __pycache__ there and every attempt is refused and logged.
    dockerfile = (ADDON / "Dockerfile").read_text()
    assert re.search(r"^ENV PYTHONDONTWRITEBYTECODE=1$", dockerfile, re.M)
    assert "compileall -q -j 0 /usr/share/switchboard" in dockerfile


def test_the_asterisk_run_script_does_not_walk_the_locked_trees():
    # A "w" deny also covers chown: a recursive chown of /var/lib/asterisk would
    # be refused once per shipped file, on every Asterisk start.
    run = (ADDON / "rootfs/etc/s6-overlay/s6-rc.d/asterisk/run").read_text()
    for line in run.splitlines():
        if line.lstrip().startswith("chown -R"):
            assert not re.search(r"/var/lib/asterisk(\s|$)", line), line
    assert re.search(r"^chown asterisk:asterisk /var/lib/asterisk ", run, re.M)


def test_asterisk_starts_unprivileged():
    # With -U/-G Asterisk kept root's whole permitted capability set.
    run = (ADDON / "rootfs/etc/s6-overlay/s6-rc.d/asterisk/run").read_text()
    execs = [l.strip() for l in run.splitlines() if l.strip().startswith("exec ")]
    assert execs == ['exec s6-setuidgid asterisk asterisk -f "$@"'], execs
