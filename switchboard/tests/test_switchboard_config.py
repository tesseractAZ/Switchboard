"""Behavioral tests for the switchboard-config generator's hardening.

Run with plain Python (no pytest needed):

    python3 switchboard/tests/test_switchboard_config.py

Exercises the input-validation / config-injection defenses so a regression that
re-opens an injection or drops a guard fails loudly.
"""
from importlib.machinery import SourceFileLoader
from pathlib import Path

# Load the extensionless generator script as a module.
SBC_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "bin" / "switchboard-config"
sbc = SourceFileLoader("switchboard_config", str(SBC_PATH)).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def test_hostile_inputs() -> None:
    opts = {
        "rooms": [
            {"ext": "101", "name": "Kitchen", "secret": "goodsecret1"},
            {"ext": "101", "name": "Dup", "secret": "x"},                  # duplicate -> skip
            {"ext": "10a", "name": "Bad", "secret": "y"},                  # bad ext  -> skip
            {"ext": "102", "name": 'Den\n[evil](room-endpoint)\ntype=endpoint;rm -rf" <999>',
             "secret": "ok2"},                                             # name injection (+ ; ")
            {"ext": "103", "name": "Hack", "secret": "se\ncret"},         # secret ctrl-char -> skip
        ],
        "trunk": {"enabled": True,
                  "provider_host": "sip.example.com\nmatch=1.2.3.4",      # host injection
                  "username": "u1", "secret": "ts", "dial_prefix": "99",
                  "outbound_caller_id": "555);System(touch /tmp/pwn"},    # CID injection
        "rtp_start": "abc", "rtp_end": "def",                             # junk -> defaults
    }
    opts["rooms"] = sbc.valid_rooms(opts["rooms"])
    check("only 101,102 survive validation", [r["ext"] for r in opts["rooms"]] == ["101", "102"])

    pj = sbc.render_pjsip(opts)
    pj_lines = [l.strip() for l in pj.splitlines()]
    check("no rogue section header injected via name", not any(l.startswith("[evil") for l in pj_lines))
    check("no bare injected directive line", "type=endpoint" not in pj_lines)
    callerid_lines = [l for l in pj_lines if l.startswith("callerid =")]
    check("no ';' or stray quote survives in any callerid line",
          all(";" not in l and l.count('"') == 2 for l in callerid_lines))
    check("ext 101 endpoint rendered exactly once", pj.count("[101](room-endpoint)") == 1)
    check("trunk skipped on injected host", "[trunk]\n" not in pj and "match = 1.2.3.4" not in pj)

    ext_conf = sbc.render_extensions(opts)
    check("dial_prefix 99 strips two chars (EXTEN:2)", "${EXTEN:2}" in ext_conf and "_99." in ext_conf)
    check("caller_id command injection ignored", "System(" not in ext_conf)

    rtp = sbc.render_rtp(opts)
    check("junk rtp falls back to defaults", "rtpstart = 10000" in rtp and "rtpend = 10200" in rtp)

    mgr = sbc.render_manager("switchboard", "sek")
    write_line = [l for l in mgr.splitlines() if l.startswith("write =")][0]
    check("AMI write excludes command + originate", "command" not in write_line and "originate" not in write_line)
    check("AMI write keeps the status-action classes", "system" in write_line and "reporting" in write_line)


def test_whitespace_dial_prefix() -> None:
    # A whitespace-only prefix must fall back to '9', never produce a match-all '_.'.
    o = {"rooms": sbc.valid_rooms([{"ext": "101", "name": "K", "secret": "s1"}]),
         "trunk": {"enabled": True, "provider_host": "sip.x.com", "username": "u",
                   "secret": "s", "dial_prefix": "   "}}
    e = sbc.render_extensions(o)
    check("whitespace dial_prefix -> _9. / EXTEN:1",
          "_9." in e and "${EXTEN:1}" in e and "exten = _.," not in e)


def test_outbound_toll_fraud_blocks() -> None:
    # Trunk outbound must deny international (011) and premium (900 / 1-900),
    # while still allowing general dialing — and the blocks must precede the
    # general rule so Asterisk's most-specific match denies first.
    o = {"rooms": sbc.valid_rooms([{"ext": "101", "name": "K", "secret": "s1"}]),
         "trunk": {"enabled": True, "provider_host": "sip.x.com", "username": "u",
                   "secret": "s", "dial_prefix": "9"}}
    e = sbc.render_extensions(o)
    check("international (011) is blocked", "_9011." in e)
    check("premium 1-900 is blocked", "_91900." in e)
    check("premium 900 is blocked", "_9900." in e)
    check("general outbound still allowed", "exten = _9.," in e)
    check("blocks precede the general outbound rule", e.index("_9011.") < e.index("exten = _9.,"))


def test_clean_config_unchanged() -> None:
    # A clean room still renders the expected endpoint/auth/aor (no regression).
    o = {"rooms": sbc.valid_rooms([{"ext": "201", "name": "Office", "secret": "Str0ngPass"}]), "trunk": {}}
    pj = sbc.render_pjsip(o)
    check("clean room renders endpoint+auth+aor",
          all(s in pj for s in ["[201](room-endpoint)", "[201-auth]",
                                "password = Str0ngPass", 'callerid = "Office" <201>']))


def test_operator_voice_dialplan() -> None:
    # Voice operator (dial 0) wires into [rooms] and adds a validated [operator].
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                             {"ext": "12", "name": "Living Room", "secret": "s2"}])
    on = sbc.render_extensions({"rooms": rooms, "operator": {"enabled": True}, "trunk": {}})
    check("dial 0 routes to the operator",
          "exten = 0,1,NoOp(Operator" in on and "Goto(operator,s,1)" in on)
    check("[operator] context present",
          "[operator]" in on and "AGI(switchboard-operator.agi)" in on)
    check("operator only dials a known room ext (allow-list)",
          'GotoIf($["${OP_TARGET}" : "^(11|12)$"]?connect:bye)' in on)
    check("operator connects via the room endpoint", "Dial(PJSIP/${OP_TARGET},30,rtT)" in on)
    check("operator speaks busy/no-answer/unavailable status (not a bland goodbye)",
          'GotoIf($["${DIALSTATUS}" = "BUSY"]?busy)' in on
          and "Playback(switchboard/sw-busy)" in on
          and "Playback(switchboard/sw-noanswer)" in on
          and "Playback(switchboard/sw-unavailable)" in on)
    # Defaults to on when the option is absent.
    default_on = sbc.render_extensions({"rooms": rooms, "trunk": {}})
    check("operator enabled by default", "[operator]" in default_on)
    # Disabled -> no operator artifacts at all.
    off = sbc.render_extensions({"rooms": rooms, "operator": {"enabled": False}, "trunk": {}})
    check("operator disabled removes dial-0 + [operator]",
          "[operator]" not in off and "exten = 0," not in off)


if __name__ == "__main__":
    test_hostile_inputs()
    test_whitespace_dial_prefix()
    test_outbound_toll_fraud_blocks()
    test_clean_config_unchanged()
    test_operator_voice_dialplan()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
