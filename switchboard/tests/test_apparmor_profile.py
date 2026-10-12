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
CHILD_HEAD = "\n  profile switchboard_config flags=(attach_disconnected,mediate_deleted) {\n"
_before, _head, _after = PROFILE.partition(CHILD_HEAD)
_child_body, _close, _rest = _after.partition("\n  }\n")
PARENT_TEXT = _before + "\n" + _rest
CHILD_TEXT = _child_body


def _rules(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines()
            if l.strip() and not l.strip().startswith("#")]


RULES = _rules(PARENT_TEXT)
CHILD_RULES = _rules(CHILD_TEXT)

ALLOWED_CAPS = {"chown", "dac_override", "fowner", "fsetid", "kill",
                "setgid", "setuid", "net_bind_service"}
MUST_DENY_CAPS = {"net_raw", "setfcap", "sys_admin", "sys_module", "sys_rawio",
                  "sys_ptrace", "sys_boot", "sys_time", "syslog", "mac_admin",
                  "mac_override", "net_admin", "dac_read_search", "bpf", "perfmon"}
WRITE_LOCKED = ["/init", "/bin/{,**}", "/sbin/{,**}", "/lib/{,**}", "/usr/{,**}",
                "/var/lib/asterisk/{agi-bin,sounds,moh}/{,**}",
                # wave 2 (v0.109.0), boot-tested on the host parser
                "/etc/asterisk/{,**}", "/etc/{localtime,timezone}",
                "/var/spool/asterisk/{,**}", "/command/{,**}", "/package/{,**}",
                "/etc/s6-overlay/{,**}",
                "/etc/{cont-init.d,cont-finish.d,services.d,fix-attrs.d,cont-profile.d}/{,**}"]
# s6 writes or executes from these, Asterisk and the services write there, or
# they are the persistent volumes: a deny on any of them is a crash-loop or a
# broken feature, and none was boot-tested.
NEVER_DENIED = ("/run", "/tmp", "/data", "/share", "/config", "/var/run/asterisk",
                "/var/log/asterisk")
# What switchboard-config may write, and nothing else. A new write in main()
# must be added here and to the child deliberately.
CHILD_WRITES = {
    "/etc/asterisk/",
    "/etc/asterisk/{pjsip,extensions,confbridge,modules,pjsip_notify,rtp,manager,logger,features}.conf",
    "/etc/localtime", "/etc/timezone", "/run/switchboard/{,**}", "/data/state/{,**}",
    "/data/{wakeups,mwi}.json", "/share/switchboard/{,**}",
}
CHILD_CAPS = {"chown", "dac_override", "fowner", "fsetid"}
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
    for rule in RULES + CHILD_RULES:
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


# --- the switchboard-config child (v0.109.0) ---------------------------------

def test_the_child_is_nested_once_and_closes_the_file():
    # The Supervisor's loader refuses a second column-0 profile line, so the
    # child is indented inside the parent, and the parent's brace ends the file.
    assert PROFILE.count(CHILD_HEAD) == 1
    heads = [l for l in PROFILE.splitlines() if l.lstrip().startswith("profile ")]
    assert len(heads) == 2, heads
    assert _close and _rest.strip() == "}", _rest[-200:]
    # The child inherits nothing, so it needs base itself (ld-musl, libpython).
    assert CHILD_TEXT.startswith("    #include <abstractions/base>\n")


def test_only_root_enters_the_child_and_by_one_exact_path():
    # Any transition or unconfined exec, in either spelling (path-first or
    # perms-first, with or without a mask), and any "->".
    exec_mode = re.compile(r"(^|\s)[a-zA-Z]*[PpCcUu]i?x(\s|,)|->")
    transitions = [r for r in RULES if exec_mode.search(r) and not r.startswith(("#", "deny ", "audit deny "))]
    assert transitions == ["owner /usr/bin/switchboard-config Cx -> switchboard_config,"], transitions


# Every rule the child may hold, by shape. Anything else (an exec mode, a
# transition, change_profile, ptrace, mount, signal send, a blanket grant) fails.
CHILD_SHAPES = (
    re.compile(r"^/\S* (r|w|m)+,$"),                       # read / write / map
    re.compile(r"^(audit )?deny /\S+ (r|rw|wl),$"),         # the read denies
    re.compile(r"^capability [a-z_]+,$"),
    re.compile(r"^signal \(receive\),$"),
    re.compile(r"^network inet6? (stream|dgram),$"),
)


def test_the_child_runs_nothing_and_has_no_blanket_grant():
    for rule in CHILD_RULES:
        assert any(p.match(rule) for p in CHILD_SHAPES), f"rule outside the child's allowed shapes: {rule}"
    assert "/usr/bin/python3.{1,}[0-9] m," in CHILD_RULES


def test_the_child_writes_exactly_what_main_writes():
    # a, l and k are write-class too; "owner" is caught by the shape test.
    writes = {m.group(3) for r in CHILD_RULES for m in [FILE_RULE.match(r)]
              if m and not m.group(2) and set(m.group(4)) & set("walk")}
    assert writes == CHILD_WRITES, sorted(writes ^ CHILD_WRITES)
    src = (ADDON / "rootfs/usr/bin/switchboard-config").read_text()
    confs = set(re.findall(r'write\(ASTERISK_ETC / "([a-z_]+)\.conf"', src))
    listed = re.search(r"/etc/asterisk/\{([a-z_,]+)\}\.conf w,", CHILD_TEXT).group(1).split(",")
    assert confs == set(listed), sorted(confs ^ set(listed))


def test_the_child_capabilities_and_read_denies():
    caps = {m.group(1) for r in CHILD_RULES for m in [re.match(r"^capability (\w+),$", r)] if m}
    assert caps == CHILD_CAPS, sorted(caps ^ CHILD_CAPS)
    for rule in ("audit deny /etc/{shadow,gshadow}{,-} r,", "deny /root/** rw,",
                 "deny /home/** rw,", "audit deny /sys/firmware/** r,",
                 "audit deny /sys/kernel/security/** r,", "signal (receive),"):
        assert rule in CHILD_RULES, rule


def test_the_init_step_execs_the_child_path_directly():
    # Run as "python3 <path>" or through an env shebang, the exec would not
    # match the Cx rule (or the child could not map env) and the boot would fail.
    up = (ADDON / "rootfs/etc/s6-overlay/s6-rc.d/init-switchboard/up").read_text()
    assert up.startswith('/bin/sh -c "/usr/bin/switchboard-config && '), up
    src = (ADDON / "rootfs/usr/bin/switchboard-config").read_text()
    # -I: no PYTHON* variables, user site or script-dir imports in the child.
    assert src.splitlines()[0] == "#!/usr/bin/python3 -IB"


def test_the_image_hands_etc_asterisk_to_root():
    dockerfile = (ADDON / "Dockerfile").read_text()
    m = re.search(r"^RUN chown -R root:asterisk /etc/asterisk \\\n\s+&& chmod -R u=rwX,g=rX,o= /etc/asterisk$",
                  dockerfile, re.M)
    assert m, "the /etc/asterisk ownership layer is gone"
    # After every layer that installs packages or copies files in.
    assert m.start() > dockerfile.rindex("\nCOPY rootfs /")
    assert m.start() > dockerfile.rindex("\nRUN apk add")


def test_s6_runs_with_the_settings_the_s6_locks_were_checked_against():
    for name in ("Dockerfile", "config.yaml"):
        text = (ADDON / name).read_text()
        for var in ("S6_LOGGING", "S6_RUNTIME_PROFILE", "S6_READ_ONLY_ROOT"):
            assert var not in text, f"{var} in {name}"


def test_pbx_spool_is_noloaded_for_the_spool_lock():
    src = (ADDON / "rootfs/usr/bin/switchboard-config").read_text()
    assert '"noload = pbx_spool.so",' in src
