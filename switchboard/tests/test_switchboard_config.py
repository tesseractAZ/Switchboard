"""Behavioral tests for the switchboard-config generator's hardening.

Run with plain Python (no pytest needed):

    python3 switchboard/tests/test_switchboard_config.py

Exercises the input-validation / config-injection defenses so a regression that
re-opens an injection or drops a guard fails loudly.
"""
import re
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


def _bare_dial_flags(dial_line: str) -> str:
    """The actual Dial FLAG letters (3rd arg), with any (...) option-arguments
    stripped — so the toll-fraud flag checks see 'r', not the 't' inside the
    b(switchboard-rtpqos...) argument. Targets use '&' and the b-arg uses '^', so
    the options field is always the last comma-separated arg."""
    inner = dial_line[dial_line.index("Dial(") + 5: dial_line.rindex(")")]
    opts = inner.split(",")[-1]
    return re.sub(r"\([^)]*\)", "", opts)


def _ctx_body(conf: str, name: str) -> str:
    """The lines of the [name] context (until the next [context] header)."""
    out, inside = [], False
    for line in conf.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            inside = (s == f"[{name}]")
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def _context_of(conf: str, needle: str):
    """Name of the [context] that the first non-header line containing `needle`
    falls under (None if not found). Used to assert a dialplan rule lives in the
    right context, not merely that it exists somewhere."""
    ctx = None
    for line in conf.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            ctx = s[1:-1]
            continue
        if needle in s:
            return ctx
    return None


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
    # The injected CID payload must not produce its System(touch ...) call. (A
    # legitimate System(/usr/bin/switchboard-mwi clear ...) from the operator
    # MWI-clear may appear; assert on the actual injection string, not "System(".)
    check("caller_id command injection ignored",
          "touch /tmp/pwn" not in ext_conf and "System(touch" not in ext_conf)

    rtp = sbc.render_rtp(opts)
    check("junk rtp falls back to defaults", "rtpstart = 10000" in rtp and "rtpend = 10200" in rtp)

    mgr = sbc.render_manager("switchboard", "sek")
    write_line = [l for l in mgr.splitlines() if l.startswith("write =")][0]
    # 'command' (CLI/RCE) stays excluded. 'originate' IS granted (paired with
    # 'system') for the per-room test-ring button — the web app constrains it to
    # ringing a known ext with a fixed Playback, never an outside Dial.
    check("AMI write still excludes command (no CLI/RCE)", "command" not in write_line)
    check("AMI write grants originate (test-ring) + system", "originate" in write_line and "system" in write_line)
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


def test_outbound_rules_live_in_rooms_context() -> None:
    # REGRESSION: the outbound _9. rule + toll-fraud blocks MUST live in [rooms]
    # (the context the phones dial from). If they land in a feature context, [rooms]
    # has no _9., so an outbound number falls through the catch-all _X. room pattern
    # -> "not a known room" -> Congestion, which phones surface as "Service
    # Unavailable" (no call ever reaches the trunk). The inbound _X. is a SEPARATE
    # rule that belongs in [from-trunk].
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                             {"ext": "19", "name": "Cordless", "secret": "s2"}])
    opts = {"rooms": rooms, "operator": {"enabled": True}, "automation_enabled": True,
            "page_enabled": True, "clock_enabled": True, "wakeup_enabled": True,
            "trunk": {"enabled": True, "provider_host": "losangeles4.voip.ms",
                      "username": "553774_switchboard", "secret": "s", "dial_prefix": "9",
                      "outbound_caller_id": "5204855554", "inbound_ext": "19"}}
    e = sbc.render_extensions(opts)
    check("outbound _9. lives in [rooms]", _context_of(e, "exten = _9.,") == "rooms")
    check("Dial(...@trunk) lives in [rooms]", _context_of(e, "@trunk") == "rooms")
    check("toll-fraud block _9011. lives in [rooms]", _context_of(e, "exten = _9011.,") == "rooms")
    check("outbound _9. is NOT in a feature context (the regression)",
          _context_of(e, "exten = _9.,") not in ("automation", "page", "operator", "wakeup"))
    check("inbound ring rule lives in [from-trunk]", _context_of(e, "Inbound call") == "from-trunk")
    check("outbound CID set before the trunk Dial",
          e.index("CALLERID(num)=5204855554") < e.index("@trunk"))
    # A non-prefixed extension still rings a room (the _X. room pattern is intact).
    check("room pattern still present in [rooms]", _context_of(e, "Room call to") == "rooms")


def test_trunk_codec_pinned_to_ulaw() -> None:
    # The outside line is the PSTN (always narrowband). The trunk endpoint must
    # advertise ulaw ONLY (disallow=all) so the provider can't negotiate a
    # wideband codec and force a transcode against the analog FXS phones.
    rooms = sbc.valid_rooms([{"ext": "19", "name": "Cordless", "secret": "s2"}])
    trunk = {"enabled": True, "provider_host": "losangeles4.voip.ms",
             "username": "100000_pi", "secret": "x", "dial_prefix": "9"}
    pj = sbc.render_pjsip({"rooms": rooms, "trunk": trunk})
    ep = pj[pj.index("[trunk]\n"):pj.index("[trunk-identify]")]
    check("trunk endpoint disallows all then allows ulaw",
          "disallow = all" in ep and "allow = ulaw" in ep)
    check("trunk endpoint advertises no wideband/alaw codec",
          not any(c in ep for c in ("g722", "opus", "alaw")))


def test_rooms_are_ulaw_only() -> None:
    # µ-law only is HARD-CODED (no `codecs` option): the room-endpoint template must
    # render disallow=all + allow=ulaw, and NO wideband/alaw/opus codec appears
    # anywhere in the generated config — so no call ever transcodes.
    rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"}])
    pj = sbc.render_pjsip({"rooms": rooms, "trunk": {}})
    tmpl = pj[pj.index("[room-endpoint](!)"):pj.index("direct_media")]
    check("room endpoint disallows all then allows ulaw",
          "disallow = all" in tmpl and "allow = ulaw" in tmpl)
    check("generated config offers no wideband/alaw/opus codec anywhere",
          not any(c in pj for c in ("g722", "opus", "alaw", "g729", "gsm", "slin16")))
    # The codec is not configurable — a stray `codecs` option is simply ignored.
    pj2 = sbc.render_pjsip({"rooms": rooms, "trunk": {}, "codecs": ["opus", "g722"]})
    check("a leftover codecs option cannot re-enable HD codecs",
          "opus" not in pj2 and "g722" not in pj2 and "allow = ulaw" in pj2)


def test_trunk_aor_not_qualified() -> None:
    # The trunk's static contact must NOT be qualified. VoIP.ms doesn't reliably
    # answer OPTIONS keep-alives, so qualifying flaps the contact to Unavailable
    # and PJSIP then refuses outbound Dial(...@trunk) -> 503 "Service Unavailable"
    # even while the registration (inbound) stays healthy. Room AORs keep qualify
    # (LAN ATAs answer OPTIONS fine) — this disable is trunk-only.
    rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"}])
    trunk = {"enabled": True, "provider_host": "losangeles4.voip.ms",
             "username": "553774_switchboard", "secret": "x", "dial_prefix": "9"}
    pj = sbc.render_pjsip({"rooms": rooms, "trunk": trunk})
    aor = pj[pj.index("[trunk-aor]"):pj.index("[trunk]\n")]
    check("trunk AOR disables qualify (qualify_frequency=0)", "qualify_frequency = 0" in aor)
    check("trunk AOR keeps the static provider contact",
          "contact = sip:losangeles4.voip.ms:5060" in aor)
    check("room AORs still qualify (trunk-only change)", "qualify_frequency = 60" in pj)


def test_trunk_registration_keepalive() -> None:
    # The re-REGISTER is the ONLY outbound traffic holding the router's UDP NAT
    # pinhole open (trunk qualify is deliberately off — VoIP.ms drops OPTIONS).
    # Asterisk's default expiration (3600s) leaves the pinhole closed ~55min of
    # every hour: VoIP.ms's reachability pings get dropped, it flags the account
    # "Unreachable", and INBOUND calls insta-fail ("Channel not available") with
    # the INVITE never reaching us. expiration=120 keeps the pinhole warm.
    rooms = sbc.valid_rooms([{"ext": "19", "name": "Cordless", "secret": "s2"}])
    trunk = {"enabled": True, "provider_host": "losangeles4.voip.ms",
             "username": "553774_switchboard", "secret": "x", "dial_prefix": "9",
             "registns": True}
    pj = sbc.render_pjsip({"rooms": rooms, "trunk": trunk})
    reg = pj[pj.index("[trunk-reg]"):]
    check("trunk registration present when registns on", "type = registration" in reg)
    check("trunk re-REGISTER every 120s (NAT keepalive, NOT the 3600s default)",
          "expiration = 120" in reg)
    check("trunk registration keeps retry_interval", "retry_interval = 60" in reg)


def test_trunk_inbound_routing() -> None:
    # trunk.inbound_ext pins an incoming call to one room (the cordless phone);
    # empty rings the whole house; an ext that isn't a room is ignored (rings
    # all) so a typo never silently drops inbound calls. The inbound Dial carries
    # 'r' only (see test_inbound_dial_has_no_transfer_flags) — NOT device-state
    # gated: gating would risk dropping a momentarily-unreachable primary handset.
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                             {"ext": "19", "name": "Cordless", "secret": "s2"}])
    base = {"enabled": True, "provider_host": "sip.x.com", "username": "u",
            "secret": "s", "dial_prefix": "9"}

    one = sbc.render_extensions({"rooms": rooms, "trunk": {**base, "inbound_ext": "19"}})
    ft = one[one.index("[from-trunk]"):]
    check("inbound_ext=19 rings only PJSIP/19",
          "Dial(PJSIP/19,30,r)" in ft and "PJSIP/11" not in ft)

    allr = sbc.render_extensions({"rooms": rooms, "trunk": base})
    fta = allr[allr.index("[from-trunk]"):]
    check("no inbound_ext rings every room",
          "Dial(PJSIP/11&PJSIP/19,30,r)" in fta)

    bad = sbc.render_extensions({"rooms": rooms, "trunk": {**base, "inbound_ext": "99"}})
    ftb = bad[bad.index("[from-trunk]"):]
    check("inbound_ext not a room -> rings every room (no silent drop)",
          "Dial(PJSIP/11&PJSIP/19,30,r)" in ftb and "Dial(PJSIP/99," not in ftb)

    # --- group ring: inbound_ext accepts a comma-separated list ----------------
    rooms3 = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                              {"ext": "19", "name": "Cordless", "secret": "s2"},
                              {"ext": "20", "name": "iPhone", "secret": "s3"}])
    grp = sbc.render_extensions({"rooms": rooms3, "trunk": {**base, "inbound_ext": "19,20"}})
    ftg = grp[grp.index("[from-trunk]"):]
    check("inbound_ext='19,20' rings the group PJSIP/19&PJSIP/20",
          "Dial(PJSIP/19&PJSIP/20,30,r)" in ftg and "PJSIP/11" not in ftg)

    grpws = sbc.render_extensions({"rooms": rooms3, "trunk": {**base, "inbound_ext": " 19 , 20 "}})
    ftws = grpws[grpws.index("[from-trunk]"):]
    check("inbound_ext list tolerates surrounding whitespace",
          "Dial(PJSIP/19&PJSIP/20,30,r)" in ftws)

    part = sbc.render_extensions({"rooms": rooms3, "trunk": {**base, "inbound_ext": "19,99"}})
    ftp = part[part.index("[from-trunk]"):]
    check("inbound_ext='19,99' drops the non-room and rings only PJSIP/19",
          "Dial(PJSIP/19,30,r)" in ftp and "PJSIP/99" not in ftp and "PJSIP/11" not in ftp)

    allbad = sbc.render_extensions({"rooms": rooms3, "trunk": {**base, "inbound_ext": "98,99"}})
    ftab = allbad[allbad.index("[from-trunk]"):]
    check("fully-invalid inbound_ext list rings every room",
          "Dial(PJSIP/11&PJSIP/19&PJSIP/20,30,r)" in ftab and "PJSIP/98" not in ftab)

    dup = sbc.render_extensions({"rooms": rooms3, "trunk": {**base, "inbound_ext": "20,19,20"}})
    ftd = dup[dup.index("[from-trunk]"):]
    check("inbound_ext list de-dups and preserves order",
          "Dial(PJSIP/20&PJSIP/19,30,r)" in ftd)


def test_inbound_dial_has_no_transfer_flags() -> None:
    # THE inbound-call bug: Dial(...,rtT) on the trunk armed the in-call DTMF
    # transfer codes for BOTH parties. 'T' let the outside PSTN caller invoke our
    # feature codes (toll-fraud / dialplan-injection), and 't' let the answering
    # cordless accidentally ## the caller into the operator mid-call. Inbound
    # Dials must carry 'r' only; SIP phones still transfer via REFER (their
    # Transfer button), which is independent of these flags.
    rooms3 = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                              {"ext": "19", "name": "Cordless", "secret": "s2"},
                              {"ext": "20", "name": "iPhone", "secret": "s3"}])
    base = {"enabled": True, "provider_host": "sip.x.com", "username": "u",
            "secret": "s", "dial_prefix": "9"}
    e = sbc.render_extensions({"rooms": rooms3, "trunk": {**base, "inbound_ext": "19,20"}})
    ft = e[e.index("[from-trunk]"):]
    inbound_dials = [ln for ln in ft.splitlines() if "Dial(PJSIP" in ln]
    check("[from-trunk] actually has Dial()s", len(inbound_dials) >= 1)
    # The bare flags (option-args like the QoS b() stripped) must carry neither
    # 't' nor 'T'. 'b' (the QoS hangup-handler) is fine; the 't' inside
    # b(switchboard-rtpqos...) is an argument, not a flag.
    for d in inbound_dials:
        fl = _bare_dial_flags(d)
        check(f"inbound Dial flags {fl!r} have no t/T", "t" not in fl and "T" not in fl and "r" in fl)
    check("no inbound Dial uses the transfer-arming rtT/rT flags", ",rtT)" not in ft and ",rT)" not in ft)

    # Outbound trunk Dial keeps 'T' (our caller may transfer) but drops 't' so
    # the far-end PSTN callee can't invoke our feature codes.
    out = [ln for ln in e.splitlines() if "@trunk" in ln and "Dial(" in ln]
    check("outbound trunk Dial exists", len(out) == 1)
    check("outbound trunk Dial is rT (caller may transfer, PSTN callee may not)",
          out and "T" in _bare_dial_flags(out[0]) and "t" not in _bare_dial_flags(out[0]))

    # Room-to-room and operator Dials are internal on both ends -> keep 'tT'.
    check("room-to-room Dial keeps tT (both ends internal)",
          "Dial(PJSIP/${EXTEN},30,rtT)" in e)


def test_features_conf_transfer() -> None:
    # Analog (FXS) phones have no transfer button, so features.conf gives them
    # in-call DTMF transfer codes. The Dial t/T flags that arm these are set
    # per-Dial by trust (see test_inbound_dial_has_no_transfer_flags), never for
    # a PSTN party. SIP phones use their own Transfer button (REFER) regardless.
    feat = sbc.render_features()
    check("features: blind transfer code present", "blindxfer => ##" in feat)
    check("features: attended transfer code present", "atxfer => *2" in feat)
    check("features: has a [featuremap] section", "[featuremap]" in feat)


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
          'GotoIf($["${OP_TARGET}" : "^(11|12)$"]?:bye)' in on)
    check("operator connects via the room endpoint", "Dial(PJSIP/${OP_TARGET},30,rtT)" in on)
    check("operator speaks busy/no-answer/unavailable status (not a bland goodbye)",
          'GotoIf($["${DIALSTATUS}" = "BUSY"]?busy)' in on
          and "Playback(switchboard/sw-busy)" in on
          and "Playback(switchboard/sw-noanswer)" in on
          and "Playback(switchboard/sw-unavailable)" in on)
    check("operator short-circuits an engaged line to busy (DEVICE_STATE)",
          "DEVICE_STATE(PJSIP/${OP_TARGET})" in on
          and 'GotoIf($["${DS}" != "NOT_INUSE"]?busy)' in on)
    check("operator plays an end-of-call tone before hangup",
          "Playback(switchboard/sw-endtone)" in on and on.count("Hangup()") >= 1)
    # Defaults to on when the option is absent.
    default_on = sbc.render_extensions({"rooms": rooms, "trunk": {}})
    check("operator enabled by default", "[operator]" in default_on)
    # Disabled -> no operator artifacts at all.
    off = sbc.render_extensions({"rooms": rooms, "operator": {"enabled": False}, "trunk": {}})
    check("operator disabled removes dial-0 + [operator]",
          "[operator]" not in off and "exten = 0," not in off)


def test_talking_clock() -> None:
    rooms = [{"ext": "11", "name": "Kitchen", "secret": "s1"},
             {"ext": "12", "name": "Office", "secret": "s2"}]
    on = sbc.render_extensions({"rooms": rooms, "clock_enabled": True, "clock_ext": "41", "trunk": {}})
    check("clock: extension present", "exten = 41,1,NoOp(Talking clock)" in on)
    # Slice just the clock extension (start marker -> next blank line) so these
    # assertions don't accidentally match the wake-up feature (which legitimately
    # still uses SayUnixTime).
    _c0 = on.index("exten = 41,1,NoOp(Talking clock)")
    _cend = on.index("\n\n", _c0)
    clk = on[_c0:_cend]
    check("clock: 'at the sound of the tone' preamble + military-time AGI + pip",
          "Playback(switchboard/sw-at-sound-tone)" in clk
          and "AGI(switchboard-clock.agi)" in clk
          and "Playback(switchboard/sw-tone)" in clk)
    check("clock: loops until hangup (labelled loop + Goto back to it)",
          "n(loop),Playback(switchboard/sw-at-sound-tone)" in clk
          and "Goto(loop)" in clk)
    check("clock: no longer uses SayUnixTime in the clock block (quirky 24h format)",
          "SayUnixTime" not in clk)
    # Default-on.
    default_on = sbc.render_extensions({"rooms": rooms, "trunk": {}})
    check("clock: on by default at ext 41", "exten = 41,1,NoOp(Talking clock)" in default_on)
    # Disabled.
    off = sbc.render_extensions({"rooms": rooms, "clock_enabled": False, "trunk": {}})
    check("clock: disabled removes the extension", "Talking clock" not in off)
    # Collision with a room ext is skipped (12 is Office).
    collide = sbc.render_extensions({"rooms": rooms, "clock_ext": "12", "trunk": {}})
    check("clock: collision with a room ext is skipped", "Talking clock" not in collide)
    # Invalid ext skipped.
    bad = sbc.render_extensions({"rooms": rooms, "clock_ext": "9;evil", "trunk": {}})
    check("clock: invalid clock_ext is skipped", "Talking clock" not in bad)


def test_timezone_resolution() -> None:
    # An explicit option short-circuits any network lookup.
    check("tz: explicit option wins", sbc.resolve_timezone({"timezone": "America/Phoenix"}) == "America/Phoenix")
    check("tz: blank option falls through (no crash)", isinstance(sbc.resolve_timezone({"timezone": ""}), str))


def test_wakeup_dialplan() -> None:
    rooms = [{"ext": "11", "name": "Kitchen", "secret": "s1"},
             {"ext": "12", "name": "Office", "secret": "s2"}]
    on = sbc.render_extensions({"rooms": rooms, "wakeup_enabled": True, "wakeup_ext": "42", "trunk": {}})
    check("wakeup: dial code routes to [wakeup]",
          "exten = 42,1,NoOp(Wake-up call)" in on and "Goto(wakeup,s,1)" in on)
    check("wakeup: [wakeup] context runs the AGI", "[wakeup]" in on and "AGI(switchboard-wakeup.agi)" in on)
    check("wakeup: [wakeup-deliver] context greets + says the time",
          "[wakeup-deliver]" in on and "switchboard/sw-wakeup-greeting" in on and "SayUnixTime(,,IMp)" in on)
    default_on = sbc.render_extensions({"rooms": rooms, "trunk": {}})
    check("wakeup: on by default at 42", "exten = 42,1,NoOp(Wake-up call)" in default_on)
    off = sbc.render_extensions({"rooms": rooms, "wakeup_enabled": False, "trunk": {}})
    check("wakeup: disabled removes the contexts", "[wakeup]" not in off and "Wake-up call" not in off)
    # Colliding with the clock (default 41) skips the dial code but keeps delivery.
    coll = sbc.render_extensions({"rooms": rooms, "wakeup_ext": "41", "trunk": {}})
    check("wakeup: clock collision skips dial code, keeps delivery",
          "exten = 41,1,NoOp(Wake-up call)" not in coll and "[wakeup-deliver]" in coll)


def test_mwi_pjsip_notify() -> None:
    # MWI is delivered via PJSIPNotify (res_mwi_external isn't in the Alpine
    # build), so the endpoints carry NO mailboxes=/voicemail wiring, and the
    # on/off NOTIFY templates live in pjsip_notify.conf.
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                             {"ext": "12", "name": "Office", "secret": "s2"}])
    pj = sbc.render_pjsip({"rooms": rooms, "trunk": {}})
    check("mwi: no mailboxes=/aggregate_mwi on endpoints (PJSIPNotify path)",
          "mailboxes =" not in pj and "aggregate_mwi" not in pj)
    notify = sbc.render_pjsip_notify()
    check("mwi: on/off NOTIFY templates present",
          "[switchboard-mwi-on]" in notify and "[switchboard-mwi-off]" in notify)
    check("mwi: message-summary event with Messages-Waiting",
          "Event=message-summary" in notify and "Messages-Waiting: yes" in notify
          and "Messages-Waiting: no" in notify)


def test_confbridge_profiles() -> None:
    cb = sbc.render_confbridge()
    check("confbridge: bridge profile present",
          "[switchboard_bridge]" in cb and "type = bridge" in cb)
    check("confbridge: user profile present",
          "[switchboard_user]" in cb and "type = user" in cb)
    check("confbridge: quiet/no-spam intercom settings",
          "quiet = yes" in cb and "announce_join_leave = no" in cb)


def test_page_intercom() -> None:
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                             {"ext": "12", "name": "Office", "secret": "s2"}])
    on = sbc.render_extensions({"rooms": rooms, "page_enabled": True, "page_ext": "44", "trunk": {}})
    check("page: [page] ConfBridge intercom context present",
          "[page]" in on
          and "ConfBridge(switchboard-page,switchboard_bridge,switchboard_user)" in on)
    check("page: dial code 44 builds PAGEPEERS from room exts and pages duplex",
          "exten = 44,1,NoOp(Page)" in on
          and "Set(PAGEPEERS=PJSIP/11&PJSIP/12)" in on
          and "Page(${PAGEPEERS},d)" in on)
    # Default-on.
    default_on = sbc.render_extensions({"rooms": rooms, "trunk": {}})
    check("page: on by default at 44 with a [page] context",
          "exten = 44,1,NoOp(Page)" in default_on and "[page]" in default_on)
    # Disabled removes both the dial code and the context.
    off = sbc.render_extensions({"rooms": rooms, "page_enabled": False, "trunk": {}})
    check("page: disabled removes dial code + [page] context",
          "exten = 44," not in off and "[page]" not in off)
    # Collision with a room ext skips the dial code but keeps the [page] target
    # (ami.page_all still originates rooms into it).
    coll = sbc.render_extensions({"rooms": rooms, "page_ext": "12", "trunk": {}})
    check("page: room collision skips dial code, keeps [page] context",
          "exten = 12,1,NoOp(Page)" not in coll and "[page]" in coll)


def test_automation_dialplan() -> None:
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                             {"ext": "12", "name": "Office", "secret": "s2"}])
    on = sbc.render_extensions({"rooms": rooms, "automation_enabled": True,
                                "automation_ext": "43", "trunk": {}})
    check("automation: dial code 43 routes to [automation]",
          "exten = 43,1,NoOp(Automation)" in on and "Goto(automation,s,1)" in on)
    check("automation: [automation] context runs the AGI",
          "[automation]" in on and "AGI(switchboard-automation.agi)" in on)
    # Operator routes a spoken "automation" into the flow (default operator on).
    check("automation: operator routes OP_RESULT=automation -> Goto(automation,s,1)",
          'GotoIf($["${OP_RESULT}" = "automation"]?automation,s,1)' in on)
    # Default-on.
    default_on = sbc.render_extensions({"rooms": rooms, "trunk": {}})
    check("automation: on by default at 43", "exten = 43,1,NoOp(Automation)" in default_on)
    # Disabled removes dial code, context, AND the operator route.
    off = sbc.render_extensions({"rooms": rooms, "automation_enabled": False, "trunk": {}})
    check("automation: disabled removes dial code + [automation] + operator route",
          "exten = 43," not in off and "[automation]" not in off
          and '"automation"]?automation' not in off)
    # Collision with a room ext skips the dial code but keeps the context (the
    # operator can still route a spoken "automation" here).
    coll = sbc.render_extensions({"rooms": rooms, "automation_ext": "12", "trunk": {}})
    check("automation: room collision skips dial code, keeps [automation] context",
          "exten = 12,1,NoOp(Automation)" not in coll and "[automation]" in coll)


def test_operator_mwi_clear() -> None:
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                             {"ext": "12", "name": "Office", "secret": "s2"}])
    on = sbc.render_extensions({"rooms": rooms, "trunk": {}})
    # The clear is backgrounded ('&'), placed AFTER Answer() so a wedged AMI can
    # never delay the answered caller, and GATED on the caller being a room ext:
    # an outside caller reaches the operator via transfer, and clearing MWI for
    # an external number just errors and queues a pointless replay.
    check("operator: clears the caller's MWI on dialing 0 (backgrounded)",
          "System(/usr/bin/switchboard-mwi clear ${CALLERID(num)} &)" in on)
    check("operator: MWI-clear gated on the caller being a room ext",
          'ExecIf($["${CALLERID(num)}" : "^(11|12)$"]?System(/usr/bin/switchboard-mwi clear' in on)
    lines = on.splitlines()
    answer_i = next(i for i, l in enumerate(lines) if l.strip() == "same = n,Answer()")
    clear_i = next(i for i, l in enumerate(lines) if "switchboard-mwi clear" in l)
    check("operator: MWI-clear runs AFTER Answer() (no ring-path latency)",
          clear_i > answer_i)
    # The injected CID payload must never appear (security regression guard).
    check("operator: no injection payload in the MWI-clear path",
          "System(touch" not in on and "rm -rf" not in on)
    # Gated on mwi_enabled.
    off = sbc.render_extensions({"rooms": rooms, "mwi_enabled": False, "trunk": {}})
    check("operator: no MWI-clear when MWI disabled",
          "switchboard-mwi clear" not in off and "[operator]" in off)


def test_feature_code_collisions() -> None:
    # page_ext and automation_ext must not collide with each other, '0', the
    # clock, the wakeup code, or a room ext. First-claimed wins; the loser's
    # dial code is dropped (but its standalone context stays).
    rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"},
                             {"ext": "12", "name": "O", "secret": "s2"}])
    # Both point at the same code: page (rendered first) takes it.
    same = sbc.render_extensions({"rooms": rooms, "page_ext": "55",
                                  "automation_ext": "55", "trunk": {}})
    check("collision: page claims a shared code, automation dial code dropped",
          "exten = 55,1,NoOp(Page)" in same and "exten = 55,1,NoOp(Automation)" not in same)
    # page_ext colliding with the clock (41) is skipped.
    clk = sbc.render_extensions({"rooms": rooms, "page_ext": "41", "trunk": {}})
    check("collision: page_ext == clock_ext is skipped",
          "exten = 41,1,NoOp(Page)" not in clk and "Talking clock" in clk)
    # automation_ext colliding with the wakeup code (42) is skipped.
    wk = sbc.render_extensions({"rooms": rooms, "automation_ext": "42", "trunk": {}})
    check("collision: automation_ext == wakeup_ext is skipped",
          "exten = 42,1,NoOp(Automation)" not in wk and "Wake-up call" in wk)


def test_modules_conf() -> None:
    # The generated modules.conf autoloads everything, then explicitly loads the
    # feature modules (so a missing one is obvious in the log) and NOLOADS the
    # local-sound-card channel drivers: with no ALSA hardware in the container
    # they spam ~50 'cannot find card 0' error lines at every startup, and this
    # PBX only ever does PJSIP/RTP.
    m = sbc.render_modules({})
    check("modules: autoload on", "autoload = yes" in m)
    check("modules: res_pjsip_notify loaded (MWI NOTIFY)", "load = res_pjsip_notify.so" in m)
    check("modules: confbridge + page loaded",
          "load = app_confbridge.so" in m and "load = app_page.so" in m)
    check("modules: chan_alsa noloaded (kills startup ALSA error spam)",
          "noload = chan_alsa.so" in m)
    check("modules: chan_console noloaded (no local sound card)",
          "noload = chan_console.so" in m)
    check("modules: cdr_csv noloaded (no local CDR consumer; dead per-call SD write)",
          "noload = cdr_csv.so" in m)


def test_logger_single_channel_no_duplicate_file() -> None:
    # Only the `console` channel: the add-on captures Asterisk's console stream
    # via journald, so a separate `messages =>` file would write every line a
    # SECOND time to the SD card (unrotated, read by nobody) — pure card wear.
    lg = sbc.render_logger({})
    check("logger: console channel present", "console =>" in lg)
    check("logger: no duplicate 'messages' file channel", "messages =>" not in lg)


def test_secret_semicolon_or_whitespace_rejected() -> None:
    # A ';' starts a comment mid-line in pjsip.conf (password = a;b -> 'a'), and
    # leading/trailing whitespace is dropped — either silently truncates the
    # secret and breaks registration. valid_rooms must drop such rooms loudly.
    rooms = sbc.valid_rooms([
        {"ext": "11", "name": "Good", "secret": "Str0ngPass"},
        {"ext": "12", "name": "Semi", "secret": "pa;ss"},
        {"ext": "13", "name": "Lead", "secret": " leadingspace"},
        {"ext": "14", "name": "Trail", "secret": "trailingspace "},
    ])
    exts = {r["ext"] for r in rooms}
    check("secret: clean room kept", "11" in exts)
    check("secret: ';' secret dropped", "12" not in exts)
    check("secret: leading-whitespace secret dropped", "13" not in exts)
    check("secret: trailing-whitespace secret dropped", "14" not in exts)


def test_wakeup_does_not_collide_with_disabled_clock() -> None:
    # A DISABLED clock must not reserve its ext (documented invariant): wakeup at
    # the same ext as a disabled clock should still render its dial code.
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"}])
    on = sbc.render_extensions({"rooms": rooms, "trunk": {},
                                "clock_enabled": False, "clock_ext": "42",
                                "wakeup_enabled": True, "wakeup_ext": "42"})
    check("wakeup: renders at ext 42 despite a DISABLED clock also set to 42",
          "exten = 42,1,NoOp(Wake-up call)" in on and "Talking clock" not in on)
    # But an ENABLED clock at 42 still blocks the wakeup code (real collision).
    both = sbc.render_extensions({"rooms": rooms, "trunk": {},
                                  "clock_enabled": True, "clock_ext": "42",
                                  "wakeup_enabled": True, "wakeup_ext": "42"})
    check("wakeup: still blocked by an ENABLED clock at the same ext",
          "exten = 42,1,NoOp(Wake-up call)" not in both)


def test_trunk_from_user_domain_validated() -> None:
    # from_user/from_domain feed pjsip.conf directives; a bad explicit value must
    # fall back to the already-validated username/host, never emit unchecked.
    rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"}])
    trunk = {"enabled": True, "provider_host": "losangeles4.voip.ms",
             "username": "553774_switchboard", "secret": "x", "dial_prefix": "9",
             "from_user": "bad user;evil", "from_domain": "ok.example.com"}
    pj = sbc.render_pjsip({"rooms": rooms, "trunk": trunk})
    tk = pj[pj.index("[trunk]\n"):]
    check("trunk: injecting from_user falls back to username",
          "from_user = 553774_switchboard" in tk and "evil" not in tk)
    check("trunk: valid from_domain is kept", "from_domain = ok.example.com" in tk)


def test_call_audio_qos_rtp_jitter() -> None:
    # v0.13.1 call-audio tuning. QoS marks (help only if the AP honours DSCP, but
    # zero-risk), an RTP watchdog on the NAT'd trunk, and an adaptive jitter
    # buffer on the public-internet trunk leg toward the answering handset.
    rooms = sbc.valid_rooms([{"ext": "19", "name": "Cordless", "secret": "s2"}])
    trunk = {"enabled": True, "provider_host": "losangeles4.voip.ms",
             "username": "553774_switchboard", "secret": "x", "dial_prefix": "9"}
    pj = sbc.render_pjsip({"rooms": rooms, "trunk": trunk})
    transport = pj[pj.index("[transport-udp]"):pj.index("[room-endpoint]")]
    check("qos: SIP signalling marked CS3 on the transport",
          "tos = cs3" in transport and "cos = 3" in transport)
    tmpl = pj[pj.index("[room-endpoint](!)"):pj.index("[room-aor]")]
    check("qos: room RTP audio marked EF", "tos_audio = ef" in tmpl and "cos_audio = 5" in tmpl)
    tk = pj[pj.index("[trunk]\n"):pj.index("[trunk-identify]")]
    check("qos: trunk RTP audio marked EF", "tos_audio = ef" in tk)
    check("rtp: trunk has a 60s RTP watchdog (not 30 -> survives ring-time early media)",
          "rtp_timeout = 60" in tk and "rtp_timeout_hold = 300" in tk)
    # Jitter buffer on the inbound trunk channel, set BEFORE the Dial.
    e = sbc.render_extensions({"rooms": rooms, "trunk": {**trunk, "inbound_ext": "19"}})
    ft = e[e.index("[from-trunk]"):]
    check("jitter: adaptive jitterbuffer set on the inbound trunk channel",
          "Set(JITTERBUFFER(adaptive)=default)" in ft)
    check("jitter: set BEFORE the Dial (buffers before bridging to the handset)",
          ft.index("JITTERBUFFER") < ft.index("Dial("))


def _lines(text):
    return [ln.strip() for ln in text.splitlines()]


def _set_precedes_dial(lines, dial_needle):
    """True iff a `Set(__TRANSFER_CONTEXT=internal-xfer)` precedes the first line
    containing dial_needle with only benign RTP-QoS hangup-handler pushes between
    (those don't touch TRANSFER_CONTEXT, so they're allowed between the guard Set
    and the Dial). The priority label — bare `n` or `n(dial)` — doesn't matter."""
    for i, ln in enumerate(lines):
        if dial_needle in ln:
            j = i - 1
            while j >= 0 and "hangup_handler_push" in lines[j]:
                j -= 1
            return j >= 0 and "Set(__TRANSFER_CONTEXT=internal-xfer)" in lines[j]
    return False


def test_transfer_toll_fraud_defense() -> None:
    # v0.14.x anti-toll-fraud: an outside caller transferred into a room must not
    # be able to ## / *2 a 9-number out through the trunk. Three complementary
    # layers, ALL gated on trunk.enabled so non-trunk output is byte-identical.
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                             {"ext": "19", "name": "Cordless", "secret": "s2"}])
    trunk = {"enabled": True, "provider_host": "losangeles4.voip.ms",
             "username": "u", "secret": "x", "dial_prefix": "9",
             "outbound_caller_id": "5204855554"}
    opts = {"rooms": rooms, "trunk": trunk, "operator": {"enabled": True}}
    e = sbc.render_extensions(opts)
    pj = sbc.render_pjsip(opts)
    el = _lines(e)

    # LAYER A: version-independent origin guard on the outbound rule.
    check("toll: outbound _9. blocks origination from the trunk endpoint",
          'GotoIf($["${CHANNEL(endpoint)}" = "trunk"]?blocked)' in e
          and "same = n(blocked),Congestion(5)" in el)

    # LAYER B1: the internal-only transfer-target context.
    ix = e[e.index("[internal-xfer]"):]
    ix = ix[:ix.index("\n\n")]
    check("toll: [internal-xfer] context emitted", "[internal-xfer]" in e)
    check("toll: internal-xfer lists literal room exts",
          "exten = 11,1,NoOp(Transfer to 11)" in ix and "exten = 19,1,NoOp(Transfer to 19)" in ix)
    check("toll: internal-xfer routes 0 -> operator", "exten = 0,1,NoOp(Transfer to operator)" in ix)
    check("toll: internal-xfer has NO outbound/wildcard (the whole point)",
          "_9." not in ix and "_X." not in ix and "@trunk" not in ix
          and "_9011." not in ix and "_9900." not in ix)

    # LAYER B2/B3/B4: __TRANSFER_CONTEXT stamped everywhere (double-underscore).
    check("toll: uses inherited __TRANSFER_CONTEXT (survives blind-transfer)",
          "TRANSFER_CONTEXT=internal-xfer" in e and "__TRANSFER_CONTEXT" in e)
    check("toll: Set precedes the room Dial", _set_precedes_dial(el, "Dial(PJSIP/${EXTEN},30,rtT"))
    check("toll: Set precedes the operator Dial", _set_precedes_dial(el, "Dial(PJSIP/${OP_TARGET},30,rtT"))
    check("toll: Set precedes the outbound @trunk Dial", _set_precedes_dial(el, "@trunk,60,rT"))
    tk = pj[pj.index("[trunk]\n"):pj.index("[trunk-identify]")]
    check("toll: trunk endpoint stamps __TRANSFER_CONTEXT at birth (inherited)",
          "set_var = __TRANSFER_CONTEXT=internal-xfer" in tk)
    tmpl = pj[pj.index("[room-endpoint](!)"):pj.index("[room-aor]")]
    check("toll: room-endpoint template also stamps __TRANSFER_CONTEXT (defence-in-depth)",
          "set_var = __TRANSFER_CONTEXT=internal-xfer" in tmpl)

    # LAYER C: reject provider-side REFER on the trunk only.
    check("toll: trunk rejects provider REFER (allow_transfer=no)", "allow_transfer = no" in tk)
    check("toll: room endpoints keep transfer (Transfer button) — allow_transfer NOT no",
          "allow_transfer = no" not in tmpl)

    # operator-off omits the exten 0 target.
    e_noop = sbc.render_extensions({"rooms": rooms, "trunk": trunk, "operator": {"enabled": False}})
    ixn = e_noop[e_noop.index("[internal-xfer]"):]
    ixn = ixn[:ixn.index("\n\n")]
    check("toll: operator disabled -> no 'exten = 0' in internal-xfer",
          "exten = 0,1,NoOp(Transfer to operator)" not in ixn and "exten = 11,1" in ixn)

    # Trunk DISABLED: none of the toll-fraud machinery appears (byte-identity).
    e_off = sbc.render_extensions({"rooms": rooms, "trunk": {}, "operator": {"enabled": True}})
    pj_off = sbc.render_pjsip({"rooms": rooms, "trunk": {}})
    check("toll: trunk disabled -> no internal-xfer / TRANSFER_CONTEXT in dialplan",
          "internal-xfer" not in e_off and "TRANSFER_CONTEXT" not in e_off)
    check("toll: trunk disabled -> no set_var/allow_transfer in pjsip",
          "TRANSFER_CONTEXT" not in pj_off and "allow_transfer" not in pj_off)


def test_operator_wakeup_route() -> None:
    # Saying "wake up call" to the operator (dial 0) hands off to the wake-up
    # flow; the route is gated on the wake-up feature being enabled (so the
    # [wakeup] context it Gotos actually exists).
    rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"}])
    on = sbc.render_extensions({"rooms": rooms, "operator": {"enabled": True},
                                "wakeup_enabled": True, "trunk": {}})
    check("operator routes a spoken wake-up to the wake-up flow",
          'GotoIf($["${OP_RESULT}" = "wakeup"]?wakeup,s,1)' in on)
    off = sbc.render_extensions({"rooms": rooms, "operator": {"enabled": True},
                                 "wakeup_enabled": False, "trunk": {}})
    check("operator has NO wake-up route when wake-ups are disabled",
          '"${OP_RESULT}" = "wakeup"' not in off)


def test_directory_dialplan() -> None:
    # Directory assistance (dial 411): a room-lookup-by-voice context that only
    # ever Dials a validated room ext.
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"},
                             {"ext": "16", "name": "Office", "secret": "s2"}])
    e = sbc.render_extensions({"rooms": rooms, "trunk": {}})
    check("directory dial code 411 present",
          "exten = 411,1,NoOp(Directory assistance)" in e and "Goto(directory,s,1)" in e)
    check("[directory] context runs the AGI",
          "[directory]" in e and "AGI(switchboard-directory.agi)" in e)
    dctx = e[e.index("[directory]"):]
    dctx = dctx[:dctx.index("\n\n")]
    check("directory only dials a KNOWN room ext (allow-list)",
          'GotoIf($["${DIR_TARGET}" : "^(11|16)$"]?:bye)' in dctx)
    check("directory connects only on an explicit connect result",
          'GotoIf($["${DIR_RESULT}" = "connect"]?:bye)' in dctx and "Dial(PJSIP/${DIR_TARGET}" in dctx)
    # Disabled -> no dial code, no context.
    off = sbc.render_extensions({"rooms": rooms, "directory_enabled": False, "trunk": {}})
    check("directory disabled removes the dial code + context",
          "Directory assistance" not in off and "[directory]" not in off)
    # A directory_ext that collides with a room is skipped.
    coll = sbc.render_extensions({"rooms": rooms, "directory_ext": "11", "trunk": {}})
    check("directory_ext colliding with a room ext is skipped (no dial code)",
          "exten = 11,1,NoOp(Directory assistance)" not in coll)
    # With the trunk live, the connect leg is confined (anti-toll-fraud).
    et = sbc.render_extensions({"rooms": rooms, "trunk": {"enabled": True,
                                "provider_host": "h", "username": "u", "secret": "x", "dial_prefix": "9"}})
    dt = et[et.index("[directory]"):]
    dt = dt[:dt.index("\n\n")]
    check("directory connect leg confined to internal-xfer when trunk on",
          "Set(__TRANSFER_CONTEXT=internal-xfer)" in dt)


def test_status_announce_dialplan() -> None:
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"}])
    e = sbc.render_extensions({"rooms": rooms, "operator": {"enabled": True}, "trunk": {}})
    check("status: dial 45 -> [status] AGI",
          "exten = 45,1,NoOp(Status menu)" in e and "Goto(status,s,1)" in e
          and _context_of(e, "AGI(switchboard-status.agi)") == "status")
    check("announce: dial 46 -> [announce] AGI",
          "exten = 46,1,NoOp(Announce)" in e and "Goto(announce,s,1)" in e
          and _context_of(e, "AGI(switchboard-announce.agi)") == "announce")
    check("smart wake-up delivery AGI wired into [wakeup-deliver]",
          _context_of(e, "AGI(switchboard-wakeup-deliver.agi)") == "wakeup-deliver")


def test_status_announce_collisions() -> None:
    # A room ext equal to status_ext (default 45) drops the dial code but keeps
    # the context (the operator/other routes still reach it).
    rooms = sbc.valid_rooms([{"ext": "45", "name": "Room45", "secret": "s1"}])
    e = sbc.render_extensions({"rooms": rooms, "operator": {"enabled": True}, "trunk": {}})
    check("status: room-ext collision drops dial code, keeps context",
          "exten = 45,1,NoOp(Status menu)" not in e and "[status]" in e)
    # announce_ext colliding with the clock code (41) is likewise dropped.
    e2 = sbc.render_extensions({"rooms": sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s"}]),
                               "announce_ext": "41", "operator": {"enabled": True}, "trunk": {}})
    check("announce: clock-code collision drops dial code, keeps context",
          "exten = 41,1,NoOp(Announce)" not in e2 and "[announce]" in e2)


def test_features_staging() -> None:
    # write_features_runtime stages an asterisk-readable features.json for the AGIs,
    # validating entity ids (a bad player id is dropped).
    import json as _json
    import tempfile
    from pathlib import Path as _P
    run = _P(tempfile.mkdtemp())
    orig = sbc.RUN_DIR
    sbc.RUN_DIR = run
    try:
        sbc.write_features_runtime({
            "wakeup_scene": "scene.wake_up", "wakeup_weather": True, "wakeup_calendar": "calendar.family",
            "announce_players": ["media_player.homepod", "bad id!", "light.not_a_speaker", "media_player.garage"],
            "announce_tts_engine": "tts.piper",
        })
        data = _json.loads((run / "features.json").read_text())
        check("features: wake-up scene/weather/calendar staged",
              data["wakeup"] == {"scene": "scene.wake_up", "weather": True, "calendar": "calendar.family"})
        check("features: announce players validated (malformed + wrong-domain dropped)",
              data["announce"]["players"] == ["media_player.homepod", "media_player.garage"])
        check("features: tts engine staged", data["announce"]["tts_engine"] == "tts.piper")
        # call_quality_alerts is staged HERE (not read from root-only options.json)
        # so the dialplan's switchboard-callqos — running as the asterisk user — can
        # honor the poor-call alert opt-out. Defaults on when unset.
        check("features: callqos alerts default on when unset",
              data.get("callqos", {}).get("alerts") is True)
        sbc.write_features_runtime({"call_quality_alerts": False,
                                    "announce_players": [], "announce_tts_engine": "tts.piper"})
        check("features: call_quality_alerts=false staged for the asterisk-user dialplan",
              _json.loads((run / "features.json").read_text())["callqos"]["alerts"] is False)
        # A wrong-domain scene (e.g. a mistyped homeassistant.restart) is dropped.
        sbc.write_features_runtime({"wakeup_scene": "homeassistant.restart",
                                    "announce_players": [], "announce_tts_engine": "tts.piper"})
        check("features: wrong-domain scene dropped",
              _json.loads((run / "features.json").read_text())["wakeup"]["scene"] == "")
    finally:
        sbc.RUN_DIR = orig


def test_write_perms_tight() -> None:
    # Generated configs carry secrets (pjsip.conf SIP passwords, manager.conf AMI
    # secret) — write() must create them 0640 with NO world access, from the first
    # byte (os.open with mode, not write_text-then-chmod).
    import stat
    import tempfile
    from pathlib import Path as _P
    d = _P(tempfile.mkdtemp())
    p = d / "sub" / "secret.conf"
    sbc.write(p, "secret = hunter2\n")
    mode = p.stat().st_mode
    check("write: content lands", p.read_text() == "secret = hunter2\n")
    check("write: no world access", (mode & stat.S_IRWXO) == 0)
    check("write: mode is 0640", (mode & 0o777) == 0o640)
    # Rewrite over an existing file keeps the tight mode.
    p.chmod(0o666)
    sbc.write(p, "secret = again\n")
    check("write: rewrite re-pins 0640", (p.stat().st_mode & 0o777) == 0o640)


def test_voice_dirs_independent_of_operator() -> None:
    # The ASR record dir + announce-audio dir must be created regardless of the
    # operator toggle — dial-45 (status) and dial-46 (announce) record + write there
    # even when operator.enabled is false. (Regression: they used to live in the
    # operator-gated write_operator_runtime.)
    import tempfile
    from pathlib import Path as _P
    run = _P(tempfile.mkdtemp())
    orig = sbc.RUN_DIR
    sbc.RUN_DIR = run
    try:
        sbc.ensure_voice_dirs()
        check("voice-dirs: asr record dir created", (run / "asr").is_dir())
        check("voice-dirs: announce audio dir created", (run / "announce").is_dir())
    finally:
        sbc.RUN_DIR = orig


def test_rooms_map_staged_for_directory() -> None:
    # The shared rooms map (operator.json) must be staged when EITHER the operator
    # OR directory assistance is enabled — dial-411 resolves room names against it,
    # so operator-off + directory-on must still write it (else "Directory is
    # unavailable"). Both-off writes nothing. (Regression: it used to gate on
    # operator.enabled alone.)
    import json as _json
    import tempfile
    from pathlib import Path as _P
    rooms = sbc.valid_rooms([{"ext": "11", "name": "Kitchen", "secret": "s1"}])
    orig = sbc.RUN_DIR
    try:
        # operator OFF, directory ON -> staged
        run = _P(tempfile.mkdtemp()); sbc.RUN_DIR = run
        sbc.write_operator_runtime({"rooms": rooms, "operator": {"enabled": False},
                                    "directory_enabled": True})
        op = run / "operator.json"
        check("rooms-map: staged when operator off but directory on", op.is_file())
        if op.is_file():
            check("rooms-map: contains the room for the directory to read",
                  _json.loads(op.read_text())["rooms"] == [{"ext": "11", "name": "Kitchen"}])
        # both OFF -> not staged
        run2 = _P(tempfile.mkdtemp()); sbc.RUN_DIR = run2
        sbc.write_operator_runtime({"rooms": rooms, "operator": {"enabled": False},
                                    "directory_enabled": False})
        check("rooms-map: NOT staged when operator and directory both off",
              not (run2 / "operator.json").exists())
    finally:
        sbc.RUN_DIR = orig


def test_rtpqos_telemetry() -> None:
    # Per-call RTP quality is logged from each context's `h` (hangup) extension,
    # where the RTP instance is still alive — reading CHANNEL(rtcp,...) (the
    # chan_pjsip accessor; the old CHANNEL(rtpqos,audio,...) returns "unavailable").
    rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"},
                             {"ext": "19", "name": "C", "secret": "s2"}])
    opts = {"rooms": rooms, "operator": {"enabled": True}, "directory_enabled": True,
            "trunk": {"enabled": True, "provider_host": "sip.x.com", "username": "u",
                      "secret": "s", "dial_prefix": "9", "inbound_ext": "19"}}
    e = sbc.render_extensions(opts)
    check("rtpqos: [switchboard-rtpqos] context emitted", "[switchboard-rtpqos]" in e)
    check("rtpqos: uses the chan_pjsip CHANNEL(rtcp,...) accessor, not the old rtpqos one",
          "${CHANNEL(rtcp,rxjitter)}" in e and "rtpqos,audio" not in e)
    check("rtpqos: logs the key quality metrics",
          all(k in e for k in ("rxjitter=", "txjitter=", "rxploss=", "txploss=", "rtt=",
                               "rxmes=", "txmes=", "rxcount=", "txcount=")))
    check("rtpqos: skips a leg with NO RTCP stats (no media / no-RTCP trunk); logs one-way legs",
          '("${RXC}" = "" | "${RXC}" = "0") & ("${TXC}" = "" | "${TXC}" = "0")]?done' in e)
    check("rtpqos: attacker-controlled inbound cid is FILTER-sanitized",
          "cid=${FILTER(0-9+*#,${CALLERID(num)})}" in e and "cid=${CALLERID(num)}" not in e)
    # It is read in an h-extension (not a hangup handler — the RTP is gone by then),
    # in every context a call can hang up in. The Gosub passes the originating
    # context as ARG1 so the sink/log can attribute the leg.
    for ctx in ("rooms", "operator", "directory", "from-trunk"):
        body = _ctx_body(e, ctx)
        check(f"rtpqos: [{ctx}] h-extension Gosubs the logger with its context tag",
              "exten = h,1" in body and f"Gosub(switchboard-rtpqos,s,1({ctx}))" in body)
    # v0.21.0: the telemetry context ALSO pushes each leg to switchboard-callqos
    # (HA sensor + poor-call notification + durable JSONL ledger). Backgrounded via
    # TrySystem so a script error can never delay or wedge the hangup, with every
    # ${...} double-quoted so an empty/absent RTCP field is an argparse-safe empty
    # arg (and can't word-split or inject).
    check("rtpqos: pushes each leg to switchboard-callqos, backgrounded via TrySystem",
          "TrySystem(/usr/bin/switchboard-callqos --detach --source dialplan" in e and " &)" in e)
    check("rtpqos: sink is passed the context tag + quoted rtcp fields",
          '--tag "${ARG1}"' in e and '--rxmes "${CHANNEL(rtcp,rxmes)}"' in e
          and '--cid "${FILTER(0-9+*#,${CALLERID(num)})}"' in e)
    check("rtpqos: human log line carries the context tag too", "tag=${ARG1}" in e)
    # The Dials must be CLEAN — no b()/hangup-handler machinery (that approach read
    # too late and never logged); the h-extension replaces it.
    dials = [ln for ln in e.splitlines() if "Dial(PJSIP" in ln]
    check("rtpqos: Dials carry no b()/hangup-handler cruft",
          dials and not any("rtpqos^push" in d or "hangup_handler" in d for d in dials))
    # Non-trunk installs still get the telemetry (it's unconditional).
    e2 = sbc.render_extensions({"rooms": rooms, "operator": {"enabled": True}, "trunk": {}})
    check("rtpqos: present on non-trunk installs too", "[switchboard-rtpqos]" in e2)


def test_config_hardening_v018() -> None:
    # D4: room endpoints get an RTP watchdog (mid-call media loss tears down instead
    # of leaking the port), like the trunk already had.
    rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "goodsecret1"}])
    pj = sbc.render_pjsip({"rooms": rooms, "trunk": {}})
    check("D4: room-endpoint has rtp_timeout", "rtp_timeout = 120" in pj)
    check("D4: room-endpoint has rtp_timeout_hold", "rtp_timeout_hold = 300" in pj)
    # D3: an all-zero ext passes the digit regex but is undialable -> rejected.
    v = sbc.valid_rooms([{"ext": "00", "name": "Bad", "secret": "s1"},
                         {"ext": "11", "name": "K", "secret": "s2"}])
    check("D3: all-zero ext '00' rejected", [r["ext"] for r in v] == ["11"])
    check("D3: all-zero ext '000' rejected", sbc.valid_rooms([{"ext": "000", "name": "B", "secret": "s"}]) == [])
    # D2: a trunk secret with ';' or whitespace would be silently truncated -> skip.
    base = {"enabled": True, "provider_host": "sip.x.com", "username": "u"}
    check("D2: trunk secret with ';' skips trunk", sbc.render_trunk_pjsip({**base, "secret": "ab;cd"}) == [])
    check("D2: trunk secret with whitespace skips trunk", sbc.render_trunk_pjsip({**base, "secret": " abc "}) == [])
    check("D2: good trunk secret still renders",
          any("[trunk]" in l for l in sbc.render_trunk_pjsip({**base, "secret": "goodsecret"})))


def test_disabled_feature_frees_ext() -> None:
    # A disabled clock no longer reserves its ext, so another feature may reuse it.
    rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s"}])
    e = sbc.render_extensions({"rooms": rooms, "clock_enabled": False, "status_ext": "41",
                               "operator": {"enabled": True}, "trunk": {}})
    check("reserve: disabled clock frees its ext for another feature",
          "exten = 41,1,NoOp(Status menu)" in e)


def test_state_dir_setup() -> None:
    # /data/state must be created (asterisk-owned, so the dial-42 wake-up AGI and
    # the dialplan MWI-clear can write their stores) and a pre-existing
    # /data/{wakeups,mwi}.json migrated into it. The chown-to-asterisk LookupErrors
    # off-box and is swallowed, but the dir + migration still happen.
    import tempfile
    from pathlib import Path as _P
    root = _P(tempfile.mkdtemp())
    data = root / "data"
    data.mkdir()
    (data / "wakeups.json").write_text('{"19": {"hhmm": "07:30", "target_epoch": 111}}')
    (data / "mwi.json").write_text('{"12": {"set_at": 5}}')
    orig_data, orig_state = sbc.DATA_DIR, sbc.STATE_DIR
    sbc.DATA_DIR, sbc.STATE_DIR = data, data / "state"
    try:
        sbc.ensure_state_dir()
        state = data / "state"
        check("state: subdir created", state.is_dir())
        check("state: wakeups.json migrated with contents",
              (state / "wakeups.json").exists() and '"19"' in (state / "wakeups.json").read_text())
        check("state: mwi.json migrated", (state / "mwi.json").exists())
        check("state: old /data/wakeups.json moved out", not (data / "wakeups.json").exists())
        # Lock files PRE-created (append-mode open needs write on the file; a
        # root-created 0644 lock would otherwise lock out the asterisk user).
        check("state: wakeups lock pre-created", (state / "wakeups.json.lock").exists())
        check("state: mwi lock pre-created", (state / "mwi.json.lock").exists())
        import stat as _stat
        m = (state / "wakeups.json").stat().st_mode
        check("state: store file is group-writable (0664)", bool(m & _stat.S_IWGRP))
        dmode = state.stat().st_mode
        check("state: dir is setgid + group-writable", bool(dmode & _stat.S_ISGID) and bool(dmode & _stat.S_IWGRP))
        # Idempotent: a second run (old files gone) must not crash or clobber.
        sbc.ensure_state_dir()
        check("state: idempotent re-run keeps migrated file", (state / "wakeups.json").exists())
        # Default store paths now point under /data/state.
        check("state: wakeup store default path is under /data/state",
              "/data/state/wakeups.json" in sbc_open_store_path())
    finally:
        sbc.DATA_DIR, sbc.STATE_DIR = orig_data, orig_state


def test_state_dir_setup_failure_is_graceful() -> None:
    # If the state dir can't be created (here: its parent is a FILE), ensure_state_dir
    # must degrade without raising — the add-on still boots; it just logs loudly.
    import tempfile
    from pathlib import Path as _P
    root = _P(tempfile.mkdtemp())
    blocker = root / "data"
    blocker.write_text("not a directory")  # mkdir(STATE_DIR) under a file -> OSError
    orig_data, orig_state = sbc.DATA_DIR, sbc.STATE_DIR
    sbc.DATA_DIR, sbc.STATE_DIR = blocker, blocker / "state"
    try:
        sbc.ensure_state_dir()
        check("state: setup failure degrades without raising", True)
    except Exception:
        check("state: setup failure degrades without raising", False)
    finally:
        sbc.DATA_DIR, sbc.STATE_DIR = orig_data, orig_state


def sbc_open_store_path() -> str:
    """Read the wake-up store's source (it isn't importable here without a /data) —
    proves the default PATH moved to /data/state."""
    src = SBC_PATH.parents[1] / "share" / "switchboard" / "wakeup" / "store.py"
    return src.read_text()


if __name__ == "__main__":
    test_status_announce_dialplan()
    test_status_announce_collisions()
    test_features_staging()
    test_write_perms_tight()
    test_voice_dirs_independent_of_operator()
    test_rooms_map_staged_for_directory()
    test_rtpqos_telemetry()
    test_config_hardening_v018()
    test_disabled_feature_frees_ext()
    test_state_dir_setup()
    test_state_dir_setup_failure_is_graceful()
    test_hostile_inputs()
    test_whitespace_dial_prefix()
    test_outbound_toll_fraud_blocks()
    test_outbound_rules_live_in_rooms_context()
    test_trunk_codec_pinned_to_ulaw()
    test_rooms_are_ulaw_only()
    test_trunk_aor_not_qualified()
    test_trunk_registration_keepalive()
    test_trunk_inbound_routing()
    test_inbound_dial_has_no_transfer_flags()
    test_features_conf_transfer()
    test_clean_config_unchanged()
    test_operator_voice_dialplan()
    test_talking_clock()
    test_timezone_resolution()
    test_wakeup_dialplan()
    test_mwi_pjsip_notify()
    test_confbridge_profiles()
    test_page_intercom()
    test_automation_dialplan()
    test_operator_mwi_clear()
    test_feature_code_collisions()
    test_modules_conf()
    test_logger_single_channel_no_duplicate_file()
    test_secret_semicolon_or_whitespace_rejected()
    test_wakeup_does_not_collide_with_disabled_clock()
    test_trunk_from_user_domain_validated()
    test_call_audio_qos_rtp_jitter()
    test_transfer_toll_fraud_defense()
    test_operator_wakeup_route()
    test_directory_dialplan()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
