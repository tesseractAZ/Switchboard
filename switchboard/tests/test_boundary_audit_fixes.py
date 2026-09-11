"""Four defects found by auditing what crosses the dialplan boundary.

    python3 -m pytest switchboard/tests/test_boundary_audit_fixes.py

The audit that produced these was prompted by a live one: an announcement's clip
name was silently reshaped in transit and then compared for equality on the other
side, so every delivered announcement was reported as never delivered (v0.98.2).
That shape — A VALUE IS TRANSFORMED IN TRANSIT AND THEN COMPARED, OR TESTED, ON
THE OTHER SIDE — turned out to have three more instances and one close relative.

Each test below carries the concrete input the audit named. None of them is
hypothetical: every one is a value this system can actually produce.
"""
import json
import re
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGI = ROOT / "rootfs" / "var" / "lib" / "asterisk" / "agi-bin"
cq = SourceFileLoader("callqos_audit",
                      str(ROOT / "rootfs" / "usr" / "bin" / "switchboard-callqos")).load_module()


class _Args:
    _FIELDS = sorted(vars(cq._parse_args([])))

    def __init__(self, **kw):
        for f in self._FIELDS:
            setattr(self, f, kw.get(f, ""))
        self.source = kw.get("source", "dialplan")


# --------------------------------------------------------------------------- #
# A. ★ HIGH — a telephone number reaching the host-mounted mirror unmasked.
# --------------------------------------------------------------------------- #
def _mirror(tmp_path, cid, chan="PJSIP/trunk-0000001f"):
    """Run one inbound trunk leg through the real build_record + append_outcome
    and return the line as it lands in /share."""
    out = tmp_path / "callqos-outcomes.jsonl"
    saved = cq.SHARE_OUTCOME_PATH
    cq.SHARE_OUTCOME_PATH = str(out)
    try:
        rec = cq.build_record(_Args(
            source="dialplan", tag="from-trunk", chan=chan, cid=cid,
            billsec="30", hcause="16", rxcount="1500", txcount="1500",
            rxmes="88", txmes="88"))
        cq.append_outcome(rec)
    finally:
        cq.SHARE_OUTCOME_PATH = saved
    return json.loads(out.read_text().splitlines()[-1])


def test_an_e164_caller_number_is_masked_in_the_shared_mirror(tmp_path):
    """★ THE LEAK. `/share` is host-mounted and captured in add-on backups, and
    this masking exists so other people's telephone numbers stay out of it.

    The dialplan filters caller-ID through `FILTER(0-9+*#,...)` — a charset that
    exists to pass `+`, `*` and `#`. The mask then gated on `.isdigit()`, and
    `"+16025551234".isdigit()` is False, so an E.164 number was written verbatim
    while the same number in bare form was masked. The sibling rule that masks
    this value in the Asterisk log already gates on LENGTH ALONE, so the two
    halves of one privacy rule disagreed — and the half that leaked was the
    durable one.
    """
    line = _mirror(tmp_path, "+16025551234")
    assert "6025551234" not in json.dumps(line), (
        f"the full number reached the shared mirror: {line['ext']!r}")
    assert line["ext"].endswith("1234") and line["ext"].startswith("*")
    assert line.get("ext_redacted") is True


def test_every_character_the_dialplan_can_pass_is_masked(tmp_path):
    """The charset is `0-9+*#`. A rule that depends on which of those a caller
    chose is not a privacy rule. `*67` and a `#` suffix are ordinary dialling."""
    for cid in ("+16025551234", "*6716025551234", "16025551234#", "16025551234"):
        line = _mirror(tmp_path, cid)
        assert line.get("ext_redacted") is True, f"{cid!r} was not masked"
        assert "6025551234" not in json.dumps(line), f"{cid!r} leaked"


def test_a_real_extension_is_still_never_masked(tmp_path):
    """The mask must not start eating the thing it exists to preserve."""
    line = _mirror(tmp_path, "19", chan="PJSIP/19-0000000a")
    assert line["ext"] == "19" and "ext_redacted" not in line


def test_the_two_halves_of_the_privacy_rule_agree():
    """★ The dialplan masks the same value in the Asterisk log on LEN > 6 with
    no charset test at all. The ledger side now matches it. A divergence here is
    how the leak happened."""
    src = (ROOT / "rootfs" / "usr" / "bin" / "switchboard-callqos").read_text()
    body = src[src.index("def append_outcome"):src.index("def append_record")]
    code = re.sub(r'"""(?:.|\n)*?"""', "",
                  "\n".join(l.split("#", 1)[0] for l in body.split("\n")))
    assert ".isdigit()" not in code, (
        "the shared-mirror mask is charset-dependent again — `+16025551234` is "
        "not .isdigit() and would be written out in full")
    assert "len(_ext) > MAX_EXT_DIGITS" in code


# --------------------------------------------------------------------------- #
# B. An outside caller storing a wake-up under a non-room key.
# --------------------------------------------------------------------------- #
def _wakeup_agi():
    """Load the wake-up AGI with its container-only import stubbed.

    The module does `sys.path.insert("/usr/share/switchboard/wakeup")` and
    `import store` at load, and that path does not exist off the box. Register
    the real store module under the bare name first so the import resolves to
    the same code the add-on runs, rather than to a stand-in.
    """
    real = SourceFileLoader("store_audit", str(
        ROOT / "rootfs" / "usr" / "share" / "switchboard" / "wakeup"
        / "store.py")).load_module()
    saved = sys.modules.get("store")
    sys.modules["store"] = real
    try:
        return SourceFileLoader("wakeup_agi",
                                str(AGI / "switchboard-wakeup.agi")).load_module()
    finally:
        if saved is None:
            sys.modules.pop("store", None)
        else:
            sys.modules["store"] = saved


def test_a_trunk_channel_yields_no_extension():
    """★ An inbound PSTN caller can reach [wakeup]: the handset that answers can
    blind-transfer to 0, [internal-xfer] routes 0 to the operator, and the
    operator's voice menu routes "wake up call" into [wakeup].

    `channel_ext` was a byte-for-byte copy of a DISPLAY helper whose docstring
    says it returns "trunk" for a trunk channel. "trunk" is truthy, so the
    caller's `if not ext` bail never fired: the wake-up was stored under the key
    "trunk" and CONFIRMED ALOUD by SayUnixTime. It can never ring.
    """
    m = _wakeup_agi()
    assert m.channel_ext("PJSIP/trunk-0000000c") == ""
    assert m.channel_ext("PJSIP/voipms-000001") == ""
    assert m.channel_ext("Local/12@internal-xfer-0000000f;1") == ""


def test_a_room_channel_still_yields_its_extension():
    """The guard must not make the feature unreachable from a real handset."""
    m = _wakeup_agi()
    assert m.channel_ext("PJSIP/11-0000000a") == "11"
    assert m.channel_ext("PJSIP/19-0000001b;2") == "19"
    assert m.channel_ext("") == "" and m.channel_ext("PJSIP") == ""


def test_every_agi_that_derives_an_extension_requires_digits():
    """★ THE CLASS. Three consumers already required digits and one did not,
    which is exactly how this survived. A new AGI that reads agi_channel must
    not reintroduce the permissive form."""
    lax = []
    for path in sorted(AGI.glob("*.agi")):
        src = path.read_text()
        if "agi_channel" not in src:
            continue
        if not re.search(r"isdigit\(\)|PJSIP/\(<digits>\)|\\d\{2,6\}|\(\\d", src):
            lax.append(path.name)
    assert not lax, (
        f"{lax} derive an extension from agi_channel without requiring digits; "
        f"a trunk channel yields the truthy string 'trunk'")


# --------------------------------------------------------------------------- #
# C. The wake-up that re-fires every twenty seconds, forever.
# --------------------------------------------------------------------------- #
def _scheduler(delivery_mod, ami_mod, store_mod):
    class _Pre:
        @staticmethod
        def due(now): return ([], [])
        @staticmethod
        def cancel_if(ext, epoch): return True
        @staticmethod
        def get_endpoints(): return []
        @staticmethod
        def originate_wakeup(ext, ring): return True
        @staticmethod
        def notify(*a, **k): return True

    saved = {k: sys.modules.get(k) for k in ("store", "ami", "ha_client", "delivery")}
    for k in ("store", "ami", "ha_client"):
        sys.modules[k] = _Pre
    sys.modules["delivery"] = delivery_mod
    try:
        sched = SourceFileLoader(
            "sched_audit", str(ROOT / "rootfs" / "usr" / "share" / "switchboard"
                               / "wakeup" / "scheduler.py")).load_module()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    sched.store = store_mod
    sched.ami = ami_mod
    sched.ha_client = None
    sched.log = lambda m: None
    return sched


class _Store:
    """Just enough store to watch whether an entry is consumed."""
    def __init__(self):
        self.entries = {"19": {"hhmm": "07:00", "target_epoch": 1.0}}
        self.cancelled = []

    def due(self, now):
        return ([(e, v) for e, v in self.entries.items()], [])

    def cancel_if(self, ext, epoch):
        self.cancelled.append(ext)
        self.entries.pop(ext, None)
        return True


def _run_tick(tmp_path, originate):
    mod = SourceFileLoader("delivery_audit", str(
        ROOT / "rootfs" / "usr" / "share" / "switchboard" / "webui"
        / "delivery.py")).load_module()
    mod.OUTCOME_PATH = str(tmp_path / "d.jsonl")
    store = _Store()

    class _AMI:
        @staticmethod
        def get_endpoints(): return [{"name": "19", "state": "Not in use"}]
        originate_wakeup = staticmethod(originate)

    sched = _scheduler(mod, _AMI, store)
    sched._delivery = mod
    sched._ringing.clear()
    sched.tick()
    recs = []
    p = Path(mod.OUTCOME_PATH)
    if p.exists():
        recs = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return store, [r["outcome"] for r in recs]


def test_a_refused_wakeup_is_consumed_not_retried_forever(tmp_path):
    """★ THE FLOOD. The comment above the consumption says the entry is consumed
    "so the next 20 s tick cannot re-fire it into a ring storm" — and the
    consumption sat inside `if ok:`, so on the one path where the ring provably
    did NOT go out it did exactly what it claims to prevent.

    A refused wake-up stayed due forever: ~4,320 `originate-refused` rows a day
    against a 2 MB ledger that trims its OLDEST records at the cap. That is not
    noise, it deletes the wake-up and announcement history the ledger is for.
    """
    store, outcomes = _run_tick(tmp_path, lambda ext, ring: False)
    assert store.cancelled == ["19"], "the refused wake-up is still due next tick"
    assert "19" not in store.entries
    assert outcomes == ["originate-refused"], outcomes


def test_an_ami_outage_still_leaves_the_wakeup_due(tmp_path):
    """★ ...and the exception path keeps its old behaviour, deliberately. AMI
    being unreachable does not mean the ring was refused — it may never have
    been attempted — so the entry stays and the next tick tries again. `not ok`
    alone cannot tell the two apart, which is why they are tracked separately."""
    def boom(ext, ring):
        raise OSError("AMI unreachable")
    store, outcomes = _run_tick(tmp_path, boom)
    assert store.cancelled == [], "an AMI outage consumed the wake-up"
    assert "19" in store.entries


def test_an_ami_outage_is_recorded_once_not_twice(tmp_path):
    """It used to record `originate-error` and then fall into `elif not ok` and
    record `originate-refused` too — two rows claiming different causes for one
    attempt, in the ledger a person reads to find out what happened."""
    def boom(ext, ring):
        raise OSError("AMI unreachable")
    _, outcomes = _run_tick(tmp_path, boom)
    assert outcomes == ["originate-error"], outcomes


def test_a_successful_wakeup_is_unchanged(tmp_path):
    store, outcomes = _run_tick(tmp_path, lambda ext, ring: True)
    assert store.cancelled == ["19"]
    assert outcomes == ["ring-queued"], outcomes


# --------------------------------------------------------------------------- #
# D. The interactive announce menu wearing a playback tag.
# --------------------------------------------------------------------------- #
def _cfg():
    return SourceFileLoader("cfg_audit", str(ROOT / "rootfs" / "usr" / "bin"
                                             / "switchboard-config")).load_module()


def test_the_interactive_announce_menu_has_its_own_tag():
    """★ One is a human speaking into a handset for as long as they like; the
    other is the PBX playing a clip AT a handset. They shared the tag
    `announce`, which callqos lists in PLAYBACK_TAGS — so the two-way call
    inherited the one-directional exemptions."""
    cfg = _cfg()
    menu = "\n".join(cfg.render_announce_context())
    play = "\n".join(cfg.render_announce_play_context())
    assert "rtpqos announce-menu" in menu
    assert "rtpqos announce)" in play and "announce-menu" not in play


def test_a_dead_transmit_path_on_the_menu_call_now_alerts():
    """The exact leg the audit named: a resident records a 12-second
    announcement on a handset whose transmit path is dead — rxcount high,
    txcount zero. That is precisely what the one-way-audio detector exists for,
    and PLAYBACK_TAGS was switching it off."""
    def leg(tag):
        return cq.build_record(_Args(
            source="dialplan", tag=tag, chan="PJSIP/16-0000000e", cid="16",
            billsec="12", hcause="16", rxcount="600", txcount="0",
            rxmes="88", txmes="0"))
    menu = leg("announce-menu")
    assert menu["notify"] is True, f"a dead-transmit menu call is still silent: {menu['reasons']}"
    assert any("one-way" in r for r in menu["reasons"]), menu["reasons"]
    # ...and the unattended playback leg stays exempt, which is correct: it IS
    # one-directional by design and nobody is on the line to act on an alert.
    assert leg("announce")["notify"] is False


def test_the_new_tag_is_not_treated_as_a_scripted_delivery():
    """`announce-menu` sets no SW_STAGE and has no reconciler waiting on it. It
    must not acquire a delivery verdict or write a delivery record."""
    assert "announce-menu" not in cq.SCRIPTED_TERMINAL
    assert "announce-menu" not in cq.DELIVERY_KIND
    assert "announce-menu" not in cq.PLAYBACK_TAGS
    assert cq.delivery_failures("announce-menu", "", 0) == []
