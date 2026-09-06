"""Behavioral tests for switchboard-callqos — the per-call quality sink.

Run with plain Python (no pytest needed):

    python3 switchboard/tests/test_callqos.py

Pins down the quality classification, the tolerant parsing (RTCP can emit "" /
"unavailable" / non-finite), the durable JSONL ledger (append + cap), and the HA
routing (dialplan drives the sensor; the notification is gate-able + dedup-keyed).
"""
import json
import os
import shutil
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

CQ_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "bin" / "switchboard-callqos"
cq = SourceFileLoader("switchboard_callqos", str(CQ_PATH)).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    # Under pytest the print + counter are DECORATIVE — only the __main__
    # runner reads _failures, so a failing check would still 'pass' the
    # test. Assert too, so both harnesses actually enforce every check.
    assert cond, name


class _Args:
    """A stand-in for argparse.Namespace with the defaults callqos expects.

    The field list is DERIVED from the real parser rather than restated. It used
    to be a hand-maintained copy, which silently drifted the moment callqos gained
    an argument: adding --stage made build_record raise
    AttributeError: '_Args' object has no attribute 'stage' in 11 tests at once --
    a failure about the stub, not about the behaviour under test, which is pure
    noise to debug. Deriving it means the stub cannot lag the parser again."""
    _FIELDS = sorted(vars(cq._parse_args([])))

    def __init__(self, **kw):
        for f in self._FIELDS:
            setattr(self, f, kw.get(f, ""))
        self.source = kw.get("source", "dialplan")


def test_classify() -> None:
    # The real degraded call: rx MES 59, one-way. Must be POOR and must notify.
    label, notify, reasons = cq.classify(59.0, 1.4, 1.5)
    check("classify: MES 59 one-way -> poor + notify", label == "poor" and notify)
    check("classify: reason names the MES", any("MES" in r for r in reasons))
    # Pristine wired call.
    label, notify, _ = cq.classify(88.1, 0.0, 1.7)
    check("classify: MES 88, 0 loss -> excellent, no notify", label == "excellent" and not notify)
    # Good tier (slightly-off but fine).
    label, notify, _ = cq.classify(80.0, 0.5, 30.0)
    check("classify: MES 80 -> good, no notify", label == "good" and not notify)
    # Fair tier that still warrants a look (MES under 70).
    label, notify, reasons = cq.classify(66.0, 2.0, 10.0)
    check("classify: MES 66 -> fair but notifies (MES<70)", label == "fair" and notify)
    # Loss-driven alert even with a healthy-ish MES.
    _, notify, reasons = cq.classify(84.0, 5.0, 10.0)
    check("classify: 5% loss notifies + names loss", notify and any("loss" in r for r in reasons))
    # High RTT alert.
    _, notify, reasons = cq.classify(86.0, 0.0, 550.0)
    check("classify: 550ms RTT notifies + names RTT", notify and any("RTT" in r for r in reasons))
    # No MES at all (a leg with no RTCP) -> unknown, never a false alarm.
    label, notify, _ = cq.classify(None, None, None)
    check("classify: no data -> unknown, no notify", label == "unknown" and not notify)


def test_tolerant_parsing() -> None:
    check("_num: numeric string parses", cq._num("88.06") == 88.06)
    for junk in ("", "unavailable", "unknown", "(null)", "nan"):
        check(f"_num: {junk!r} -> None", cq._num(junk) is None)
    check("_num: +inf -> None (can't poison the sensor)", cq._num("inf") is None)
    check("_num: None -> None", cq._num(None) is None)
    check("_pct: 30 lost of 2131 -> ~1.408%", cq._pct(30, 2131) == 1.408)
    check("_pct: zero counter -> None (no divide-by-zero)", cq._pct(5, 0) is None)
    check("_ms: seconds -> milliseconds", cq._ms(0.019875) == 19.88)
    check("_ms: None passthrough", cq._ms(None) is None)


def test_build_record() -> None:
    # The MES-59 operator call, exactly as the dialplan would pass it.
    rec = cq.build_record(_Args(
        source="dialplan", tag="operator", chan="PJSIP/12-00000002", cid="12",
        codec="ulaw", billsec="53", hcause="16",
        rxcount="2689", txcount="2131", rxploss="0", txploss="30",
        rxjitter="0.020000", txjitter="0.000000", rtt="0.001525",
        rxmes="58.966802", txmes="88.071923"))
    check("build: worst MES is the degraded rx leg", rec["mes_worst"] == 59.0)
    check("build: worst loss is the tx direction (1.4%)", rec["loss_tx_pct"] == 1.408)
    check("build: quality poor, notify true", rec["quality"] == "poor" and rec["notify"])
    check("build: MOS derived from worst MES", rec["mos_worst"] == 2.95)
    check("build: absent richer fields are null, not crashy",
          rec["rtt_max_ms"] is None)
    # v0.77.0: a field the running Asterisk cannot supply is OMITTED, not
    # emitted as null in 100% of records. Asserting absence rather than
    # null-ness is the stronger claim -- `rec["rx_octets"] is None` passed
    # identically before and after the schema advertised a measurement that
    # could never be made.
    check("build: the uncollectable octet keys are absent, not null",
          "rx_octets" not in rec and "tx_octets" not in rec)
    with_octets = cq.build_record(_Args(source="dialplan", tag="rooms",
                                        chan="PJSIP/12-1", rxcount="100",
                                        txcount="100", rxmes="88", txmes="88",
                                        rxoctet="16000", txoctet="16000"))
    check("build: but a future Asterisk that DOES supply them still lands",
          with_octets["rx_octets"] == 16000)
    # A no-RTCP leg (e.g. the VoIP.ms trunk) must degrade gracefully to unknown.
    rec2 = cq.build_record(_Args(source="dialplan", tag="from-trunk", chan="PJSIP/trunk-1",
                                 rxcount="", txcount="", rxmes="", txmes=""))
    check("build: no-RTCP leg -> unknown, no notify", rec2["quality"] == "unknown" and not rec2["notify"])


def test_ledger_append_and_cap() -> None:
    d = tempfile.mkdtemp()
    path = os.path.join(d, "callqos.jsonl")
    orig = cq.PATH
    cq.PATH = path
    try:
        n = cq.MAX_RECORDS + 25
        for i in range(n):
            cq.append_record({"ts": i, "chan": f"PJSIP/x-{i:08x}", "mes_worst": 88.0})
        lines = [ln for ln in open(path).read().splitlines() if ln.strip()]
        check("ledger: capped at MAX_RECORDS", len(lines) == cq.MAX_RECORDS)
        last = json.loads(lines[-1])
        check("ledger: newest record retained (ring drops oldest)", last["ts"] == n - 1)
        first = json.loads(lines[0])
        check("ledger: oldest dropped", first["ts"] == n - cq.MAX_RECORDS)
    finally:
        cq.PATH = orig


def test_ha_routing() -> None:
    # Inject a fake ha_client so we can assert the routing without a live HA.
    calls = {"set_state": [], "notify": []}

    class _Fake:
        @staticmethod
        def set_state(eid, state, attrs=None):
            calls["set_state"].append((eid, state))
            return True

        @staticmethod
        def notify(msg, title="", notification_id=""):
            calls["notify"].append(notification_id)
            return True

    sys.modules["ha_client"] = _Fake
    orig_alerts = cq._alerts_enabled
    cq._alerts_enabled = lambda: True
    try:
        # Dialplan + poor -> updates the headline sensor AND notifies.
        poor = cq.build_record(_Args(source="dialplan", tag="operator", chan="PJSIP/12-2",
                                     cid="12", rxcount="2689", txcount="2131",
                                     rxploss="0", txploss="30", rxmes="59", txmes="88"))
        cq.push_ha(poor)
        check("ha: dialplan poor call sets sensor.switchboard_last_call",
              any(e == "sensor.switchboard_last_call" for e, _ in calls["set_state"]))
        # The id carries the leg TIMESTAMP as well as the channel: Asterisk
        # recycles channel names after a restart, so a channel-only id would let
        # a later bad call silently replace an earlier unread alert.
        check("ha: dialplan poor call notifies, id keyed by ts + channel",
              calls["notify"]
              and calls["notify"][-1].startswith("switchboard_callqos_")
              and calls["notify"][-1].endswith("_PJSIP_12-2")
              and str(poor["ts"]) in calls["notify"][-1])

        # Poll (far leg) must NOT drive the headline sensor (avoids stale flicker).
        calls["set_state"].clear()
        pollrec = cq.build_record(_Args(source="poll", tag="rooms", chan="PJSIP/19-9",
                                        cid="19", rxcount="100", txcount="100",
                                        rxploss="10", txploss="0", rxmes="55", txmes="80"))
        cq.push_ha(pollrec)
        check("ha: poll source does NOT touch the headline sensor", calls["set_state"] == [])

        # Alerts gated off -> sensor still updates, no notification.
        calls["notify"].clear()
        cq._alerts_enabled = lambda: False
        cq.push_ha(poor)
        check("ha: call_quality_alerts=false suppresses the notification", calls["notify"] == [])
    finally:
        cq._alerts_enabled = orig_alerts
        sys.modules.pop("ha_client", None)


def test_one_way_audio() -> None:
    # Dead-receive: the phone SENT audio but HEARD nothing. Worst-direction MES
    # scoring alone would miss it (tx MES healthy, rx MES absent) -> must be caught.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-1",
                                rxcount="0", txcount="1500", txmes="88"))
    check("one-way: dead receive -> poor + notify",
          rec["quality"] == "poor" and rec["notify"])
    check("one-way: reason names the dead direction",
          any("one-way" in r and "receive" in r for r in rec["reasons"]))
    # Dead-transmit: the phone HEARD audio but sent nothing (dead mic path).
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-2",
                                rxcount="1500", txcount="0", rxmes="88"))
    check("one-way: dead transmit -> poor + notify + names transmit",
          rec["quality"] == "poor" and rec["notify"]
          and any("transmit" in r for r in rec["reasons"]))
    # A healthy two-way call must NOT be flagged one-way.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-3",
                                rxcount="1500", txcount="1500", rxmes="88", txmes="88"))
    check("one-way: healthy two-way call not flagged", rec["quality"] == "excellent")
    # A tiny call-setup blip (few packets, other side 0) is NOT a false alarm.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-4",
                                rxcount="8", txcount="0", rxmes="88"))
    check("one-way: sub-second blip is not flagged one-way",
          not any("one-way" in r for r in rec["reasons"]))
    # REGRESSION (seen live): a lone stray inbound packet across a 38s call — rx=1,
    # tx=542 — is effectively dead-receive and MUST flag. The old exactly-zero test
    # ("rxcount and rxcount > 0") let it read "excellent".
    rec = cq.build_record(_Args(source="dialplan", tag="operator", chan="PJSIP/12-5",
                                billsec="38", rxcount="1", txcount="542", txmes="88"))
    check("one-way: rx=1 (near-dead receive) -> poor + notify",
          rec["quality"] == "poor" and rec["notify"]
          and any("one-way" in r and "receive" in r for r in rec["reasons"]))
    # ...and the mirror: tx just under the dead threshold with a live receive side.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-6",
                                rxcount="900", txcount="3", rxmes="88"))
    check("one-way: tx=3 (near-dead transmit) -> poor + notify",
          rec["quality"] == "poor" and rec["notify"]
          and any("transmit" in r for r in rec["reasons"]))


def test_short_leg_never_one_way() -> None:
    # REGRESSION (2 live false alarms, Aug 7-8): a sub-second abandoned call to the
    # operator — the dialplan does Answer -> Wait(1), the caller hangs up during the
    # greeting — has a LEGITIMATELY silent transmit side (hcause 16, billsec <= 1).
    # PACKET COUNTS ARE THE REAL ONES from those two production records
    # (rx=58/tx=6 and rx=55/tx=2). The previous fixture used rxcount=2600 with
    # billsec=1, which is physically impossible: at ulaw/20 ms, 2600 packets is
    # 52 SECONDS of received audio, so that fixture described a genuine one-way
    # call while asserting it must not be flagged — it only "passed" because the
    # gate read billsec alone.
    rec = cq.build_record(_Args(source="dialplan", tag="operator", chan="PJSIP/12-a1",
                                cid="12", billsec="1", hcause="16",
                                rxcount="58", txcount="6"))
    check("shortleg: 1s abandoned call is NOT one-way",
          not any("one-way" in r for r in rec["reasons"]))
    check("shortleg: not poor, notify False",
          rec["quality"] != "poor" and not rec["notify"])
    # The other live false alarm, same family.
    rec = cq.build_record(_Args(source="dialplan", tag="operator", chan="PJSIP/11-a1",
                                cid="11", billsec="1", hcause="16",
                                rxcount="55", txcount="2"))
    check("shortleg: the second live abandoned call is NOT one-way",
          not any("one-way" in r for r in rec["reasons"]) and not rec["notify"])
    # THE HOLE THIS RELEASE CLOSES: a CDR reset reports 2s for a leg whose packet
    # counters show 75s (both shapes seen live 2026-08-08). Gating on billsec
    # alone exempted exactly the legs this release already knows lie about
    # duration — a genuinely long one-way call went unflagged.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/13-a1",
                                cid="13", billsec="2", hcause="16",
                                rxcount="3750", txcount="5"))
    check("shortleg: CDR-reset long one-way IS detected (billsec lied, RTP did not)",
          rec["quality"] == "poor" and rec["notify"]
          and any("one-way" in r and "transmit" in r for r in rec["reasons"]))
    check("shortleg: and its duration is RTP-corrected, with the raw value kept",
          rec["dur"] == 75 and rec["billsec_raw"] == 2)
    # The same packet shape on a normal-length call is a REAL dead transmit.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-a2",
                                cid="12", billsec="30", hcause="16",
                                rxcount="2600", txcount="5"))
    check("shortleg: same stats at 30s -> one-way still detected",
          rec["quality"] == "poor" and rec["notify"]
          and any("one-way" in r and "transmit" in r for r in rec["reasons"]))
    # Boundary: exactly 5s is long enough to trust the dead side.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-a3",
                                cid="12", billsec="5", hcause="16",
                                rxcount="2600", txcount="5"))
    check("shortleg: boundary billsec=5 still allows one-way",
          any("one-way" in r for r in rec["reasons"]))


def test_rtp_outranks_mangled_billsec() -> None:
    # REGRESSION (live): operator sessions that loop through STT re-records reset
    # the CDR, so billsec covers only the last segment — legs stored dur=2 while
    # the packet counters showed ~75s of RTP (ulaw 20ms ptime = 50 pkts/s). The
    # monotonic RTP evidence wins; the mangled value is kept as billsec_raw.
    rec = cq.build_record(_Args(source="dialplan", tag="operator", chan="PJSIP/12-b1",
                                cid="12", billsec="2", rxcount="3750", txcount="3700",
                                rxmes="88", txmes="88"))
    check("rtpdur: billsec 2 on 75s of RTP -> dur corrected to 75", rec["dur"] == 75)
    check("rtpdur: original billsec preserved as billsec_raw", rec["billsec_raw"] == 2)
    # A coherent billsec (20s of RTP on a 20s call) is left alone.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-b2",
                                cid="12", billsec="20", rxcount="1000", txcount="990",
                                rxmes="88", txmes="88"))
    check("rtpdur: coherent billsec kept verbatim", rec["dur"] == 20)
    check("rtpdur: no billsec_raw when uncorrected", rec["billsec_raw"] is None)


def test_ext_prefers_channel_endpoint() -> None:
    # REGRESSION (live, 30/95 ledger records): the dialplan rewrites CALLERID(num)
    # to the public DID before an outbound Dial, so the h-extension's --cid on a
    # trunk call is the DID, not the room. The channel name carries the true
    # originating endpoint.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-00000055",
                                cid="2135550100", billsec="30",
                                rxcount="1500", txcount="1500", rxmes="88", txmes="88"))
    check("ext: outbound leg attributes to the channel's room endpoint",
          rec["ext"] == "12")
    # Inbound trunk leg: PJSIP/trunk-... is not a digit endpoint -> cid stays the
    # PSTN caller (the old, correct behavior).
    rec = cq.build_record(_Args(source="dialplan", tag="from-trunk",
                                chan="PJSIP/trunk-00000056", cid="15551234567",
                                billsec="30", rxcount="1500", txcount="1500",
                                rxmes="88", txmes="88"))
    check("ext: inbound trunk leg keeps the PSTN caller id",
          rec["ext"] == "15551234567")


def test_mes_zero_is_no_data() -> None:
    # Asterisk returns MES=0.0 for a direction it couldn't score (short call / no
    # RTCP). A real 4s/6s call must NOT be scored "poor" off that sentinel.
    # #21: a 4s operator setup leg, both MES 0 -> unknown (no data), not poor.
    rec = cq.build_record(_Args(source="dialplan", tag="operator", chan="PJSIP/19-1",
                                cid="19", billsec="4", codec="slin",
                                rxcount="227", txcount="135", rxmes="0", txmes="0"))
    check("mes0: both-zero MES -> unknown, not poor",
          rec["mes_worst"] is None and rec["quality"] == "unknown" and not rec["notify"])
    # #18: a 6s call, tx MES 0 (unmeasured) but rx MES 88 -> excellent, not poor.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/x-2",
                                cid="2025550100", billsec="6",
                                rxcount="248", txcount="332", rxmes="88.1", txmes="0"))
    check("mes0: one-zero MES falls back to the measured direction",
          rec["mes_worst"] == 88.1 and rec["quality"] == "excellent" and not rec["notify"])


def test_incoherent_low_mes_filtered() -> None:
    # #3: wired Kitchen leg, MES 27.6 but 0% loss, 1.57ms rtt, only-packetization
    # jitter -> a re-INVITE/transfer glitch, physically impossible as real audio.
    # The collapsed reading must not drive "poor".
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-3",
                                cid="12", billsec="38", rxcount="1907", txcount="1870",
                                rxploss="0", txploss="41", rxjitter="0.019875",
                                txjitter="0", rtt="0.00157", rxmes="27.6", txmes="88.1"))
    check("incoherent: MES 27 w/ clean transport dropped -> not poor",
          rec["mes_worst"] == 88.1 and rec["quality"] != "poor")
    check("incoherent: raw mes_rx still recorded verbatim", rec["mes_rx"] == 27.6)
    # A genuinely-lossy low MES (real loss present) MUST still flag.
    rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/19-4",
                                cid="19", billsec="30", rxcount="1500", txcount="1500",
                                rxploss="90", txploss="0", rxjitter="0.02", txjitter="0",
                                rtt="0.02", rxmes="20", txmes="88"))
    check("incoherent: low MES WITH real loss is kept (still poor)",
          rec["quality"] == "poor" and rec["notify"])
    # A real WiFi dip (MES 59 with ~1% loss) is coherent -> kept, still poor.
    rec = cq.build_record(_Args(source="dialplan", tag="operator", chan="PJSIP/19-5",
                                cid="19", billsec="53", rxcount="2634", txcount="2088",
                                rxploss="28", txploss="11", rxjitter="0.02", txjitter="0.013",
                                rtt="0.037", rxmes="59.2", txmes="73.2"))
    check("incoherent: real MES-59 WiFi dip (loss>0.5%) kept -> poor",
          rec["mes_worst"] == 59.2 and rec["quality"] == "poor")


def test_argv_sanitizes_nonfinite() -> None:
    # glibc can print 0.0/0.0 as "-nan"; the dialplan then passes --rtt "-nan".
    # argparse would treat "-nan" as an unknown option and SystemExit, dropping the
    # WHOLE record for the degraded call. Must instead null it and still record.
    d = tempfile.mkdtemp()
    cq.PATH, orig = os.path.join(d, "cq.jsonl"), cq.PATH
    try:
        for bad in ("-nan", "-inf", "-1.#IND"):
            rc = cq.main(["--source", "dialplan", "--chan", "PJSIP/12-9",
                          "--rxcount", "1265", "--txcount", "1265",
                          "--rtt", bad, "--rxjitter", bad, "--rxmes", "88", "--txmes", "88"])
            check(f"argv: {bad!r} does not drop the record (rc=0)", rc == 0)
        recs = [json.loads(l) for l in open(cq.PATH)]
        check("argv: a record was written for every degraded call", len(recs) == 3)
        check("argv: the -nan RTT became null, not a crash", recs[0]["rtt_ms"] is None)
        check("argv: the rest of the record survived", recs[0]["mes_worst"] == 88.0)
    finally:
        cq.PATH = orig


def test_alerts_option_read_from_features() -> None:
    # The opt-out must be honored via the asterisk-readable features.json, since the
    # dialplan runs callqos as the asterisk user (root-only options.json is
    # unreadable). Confirm the flag is actually read from that file.
    d = tempfile.mkdtemp()
    fpath = os.path.join(d, "features.json")
    orig = cq.FEATURES
    cq.FEATURES = fpath
    try:
        with open(fpath, "w") as f:
            f.write(json.dumps({"callqos": {"alerts": False}}))
        check("alerts: features.json alerts=false honored", cq._alerts_enabled() is False)
        with open(fpath, "w") as f:
            f.write(json.dumps({"callqos": {"alerts": True}}))
        check("alerts: features.json alerts=true honored", cq._alerts_enabled() is True)
        with open(fpath, "w") as f:
            f.write(json.dumps({"announce": {}}))  # key absent
        check("alerts: missing callqos key defaults on", cq._alerts_enabled() is True)
        cq.FEATURES = os.path.join(d, "nope.json")
        check("alerts: unreadable features.json fails open (default on)", cq._alerts_enabled() is True)
    finally:
        cq.FEATURES = orig


def test_detach_gating() -> None:
    # The dialplan passes --detach so the sink forks into its own session (survives
    # channel teardown). Unit tests call main() WITHOUT it, so they must never fork.
    # Spy on the PARENT branch: fork returns a pid, os._exit raises a sentinel so we
    # stop at the parent path without running setsid/stdio-redirect on the runner.
    class _Forked(Exception):
        pass
    calls = {"fork": 0}
    saved = (cq.os.fork, cq.os._exit)
    cq.os.fork = lambda: (calls.__setitem__("fork", calls["fork"] + 1), 4321)[1]
    cq.os._exit = lambda code: (_ for _ in ()).throw(_Forked())
    d = tempfile.mkdtemp()
    cq.PATH, origp = os.path.join(d, "cq.jsonl"), cq.PATH
    try:
        # No --detach -> no fork; record still written inline.
        cq.main(["--source", "dialplan", "--chan", "PJSIP/nd-1", "--rxcount", "5", "--txcount", "5"])
        check("detach: main() without --detach never forks", calls["fork"] == 0)
        check("detach: inline run still records", os.path.exists(cq.PATH))
        # --detach -> _detach() forks; the parent branch hits os._exit (our sentinel),
        # which propagates out of main() (it is raised before main's try).
        raised = False
        try:
            cq.main(["--detach", "--source", "dialplan", "--chan", "PJSIP/nd-2",
                     "--rxcount", "5", "--txcount", "5"])
        except _Forked:
            raised = True
        check("detach: --detach forks and the parent exits immediately",
              calls["fork"] == 1 and raised)
    finally:
        cq.os.fork, cq.os._exit = saved
        cq.PATH = origp


def test_main_never_raises() -> None:
    # A hangup handler must never fail loudly, even on garbage input.
    d = tempfile.mkdtemp()
    cq.PATH, orig = os.path.join(d, "cq.jsonl"), cq.PATH
    try:
        rc = cq.main(["--source", "dialplan", "--rxmes", "garbage", "--rtt", "",
                      "--rxcount", "5", "--chan", "PJSIP/x-1"])
        check("main: returns 0 on messy input", rc == 0)
        check("main: still wrote a record", os.path.exists(cq.PATH))
    finally:
        cq.PATH = orig


def test_alert_id_distinguishes_calls_across_a_restart() -> None:
    """Asterisk restarts its channel counter at boot, so a name like
    PJSIP/12-00000003 recurs after every restart. Keyed on the channel alone, a
    later bad call would silently REPLACE an earlier unread alert about a
    DIFFERENT call (Home Assistant collapses same-id notifications). The leg's
    timestamp keeps distinct calls distinct."""
    ids = []

    class _Fake:
        @staticmethod
        def set_state(eid, state, attrs=None):
            return True

        @staticmethod
        def notify(msg, title="", notification_id=""):
            ids.append(notification_id); return True

    sys.modules["ha_client"] = _Fake
    orig = cq._alerts_enabled
    cq._alerts_enabled = lambda: True
    try:
        def poor_leg(ts):
            rec = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-00000003",
                                        cid="12", rxcount="2689", txcount="2131",
                                        rxploss="0", txploss="30", rxmes="59", txmes="88"))
            rec["ts"] = ts          # same recycled channel name, different calls
            return rec
        cq.push_ha(poor_leg(1786700000))          # before a restart
        cq.push_ha(poor_leg(1786786400))          # a day later, channel name reused
        check("alert id: notified for both legs", len(ids) == 2)
        check("alert id: recycled channel name still yields distinct ids", ids[0] != ids[1])
        check("alert id: both namespaced to callqos",
              all(i.startswith("switchboard_callqos_") for i in ids))
        check("alert id: both carry the channel", all(i.endswith("_PJSIP_12-00000003") for i in ids))
        # A repeat report of the SAME leg must still collapse onto one entry.
        ids.clear()
        same = poor_leg(1786700000)
        cq.push_ha(same); cq.push_ha(same)
        check("alert id: the same leg reported twice collapses to one id",
              len(ids) == 2 and ids[0] == ids[1])
    finally:
        cq._alerts_enabled = orig
        sys.modules.pop("ha_client", None)


def test_playback_legs_are_recorded_but_never_alert() -> None:
    """Every leg the system carries belongs in the ledger; not every leg
    deserves a notification.

    v0.55.0 gave the machine-initiated contexts an h-extension so the ledger
    stops under-reporting (ten legs ran in one window and one was recorded).
    But a wake-up delivery, an intercom page and a recorded announcement are
    the PBX talking AT a phone: nobody is on the line to act on a popup, and
    the one-way-audio detector — which exists to catch a broken CONVERSATION —
    would fire on their perfectly normal one-directional shape."""
    # The shape that would look "one-way" on a conversation: we sent plenty,
    # the handset sent almost nothing back.
    def leg(tag):
        return cq.build_record(_Args(source="dialplan", tag=tag, chan="PJSIP/19-1",
                                     cid="19", billsec="30", hcause="16",
                                     rxcount="5", txcount="2600", rxmes="88"))
    for tag in ("wakeup-deliver", "page", "announce"):
        rec = leg(tag)
        check(f"{tag}: recorded with its tag", rec["tag"] == tag)
        check(f"{tag}: NOT flagged one-way (that shape is its design)",
              not any("one-way" in r for r in rec["reasons"]))
        check(f"{tag}: never notifies", rec["notify"] is False)

    # A real conversation with the same shape is still a genuine fault.
    conv = leg("rooms")
    check("rooms: the same shape on a conversation IS one-way",
          any("one-way" in r for r in conv["reasons"]) and conv["notify"] is True)
    # And the interactive voice menus keep alerting — bad audio there wrecks
    # speech recognition, which the caller feels immediately.
    for tag in ("wakeup", "automation", "status"):
        check(f"{tag}: interactive menu still alerts", leg(tag)["notify"] is True)

    # Genuinely BAD audio on a playback leg: it is still scored and recorded
    # honestly, but it must not raise an alert. (Without this the suppression
    # is untested — a healthy playback leg would not notify anyway.)
    def rough(tag):
        return cq.build_record(_Args(source="dialplan", tag=tag, chan="PJSIP/19-2",
                                     cid="19", billsec="20", hcause="16",
                                     rxcount="900", txcount="900",
                                     rxploss="0", txploss="30",
                                     rxmes="59", txmes="59"))
    for tag in ("wakeup-deliver", "page", "announce"):
        rec = rough(tag)
        check(f"{tag}: poor audio is still SCORED honestly", rec["quality"] == "poor")
        check(f"{tag}: ...and recorded with its reasons", bool(rec["reasons"]))
        check(f"{tag}: ...but raises no alert", rec["notify"] is False)
    conv = rough("operator")
    check("operator: the same poor audio DOES alert",
          conv["quality"] == "poor" and conv["notify"] is True)


def test_playback_legs_do_not_drive_the_last_call_sensor() -> None:
    # sensor.switchboard_last_call answers "how was the last CALL". A daily
    # announcement chime overwriting that with its own score would make the
    # sensor describe the PBX talking to itself.
    sets = []

    class _Fake:
        @staticmethod
        def set_state(eid, state, attrs=None):
            sets.append(eid); return True

        @staticmethod
        def notify(*a, **k):
            return True

    sys.modules["ha_client"] = _Fake
    orig = cq._alerts_enabled
    cq._alerts_enabled = lambda: True
    try:
        cq.push_ha(cq.build_record(_Args(source="dialplan", tag="announce",
                                         chan="PJSIP/19-1", cid="19", billsec="4",
                                         rxcount="200", txcount="200", rxmes="88", txmes="88")))
        check("announce: does not update last_call",
              "sensor.switchboard_last_call" not in sets)
        sets.clear()
        cq.push_ha(cq.build_record(_Args(source="dialplan", tag="rooms",
                                         chan="PJSIP/11-1", cid="11", billsec="40",
                                         rxcount="2000", txcount="2000", rxmes="88", txmes="88")))
        check("rooms: a real conversation still updates last_call",
              "sensor.switchboard_last_call" in sets)
    finally:
        cq._alerts_enabled = orig
        sys.modules.pop("ha_client", None)


if __name__ == "__main__":
    test_classify()
    test_tolerant_parsing()
    test_build_record()
    test_ledger_append_and_cap()
    test_ha_routing()
    test_one_way_audio()
    test_short_leg_never_one_way()
    test_rtp_outranks_mangled_billsec()
    test_ext_prefers_channel_endpoint()
    test_mes_zero_is_no_data()
    test_incoherent_low_mes_filtered()
    test_argv_sanitizes_nonfinite()
    test_alerts_option_read_from_features()
    test_detach_gating()
    test_main_never_raises()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)


def test_rtt_sampling_flags_a_statistically_empty_measurement() -> None:
    """A one-RTCP-round RTT must be distinguishable from a well-sampled one.

    Live example that motivated this: rtt=0.005538 with stdev=0.000000 and maxrtt
    equal to rtt reached the ledger beside an ordinary rxmes=82.5. Statistically
    empty, yet indistinguishable from a real reading by any field the record
    carried -- so every mean, percentile and trend built on the ledger was
    averaging it in at full weight.

    Asterisk exposes no RTCP report count, and inventing a CHANNEL(rtcp,...) name
    that does not exist is the v0.48.0 rxoctetcount mistake (four WARNINGs per
    call, field null in 100% of records). So this is derived from fields already
    in hand."""
    check("sampling: spread present -> multi", cq.rtt_sampling(5.5, 9.0, 1.2) == "multi")
    check("sampling: zero spread, max == mean -> single",
          cq.rtt_sampling(5.538, 5.538, 0.0) == "single")
    check("sampling: nothing measured at all -> none",
          cq.rtt_sampling(0.0, 0.0, 0.0) == "none")
    check("sampling: all-None -> none", cq.rtt_sampling(None, None, None) == "none")
    # A max that disagrees with the mean means real spread, whatever stdev says --
    # claim 'multi' rather than assert a single sample we cannot support.
    check("sampling: max above mean with zero stdev -> multi",
          cq.rtt_sampling(5.0, 9.0, 0.0) == "multi")

    rec = cq.build_record(_Args(source="dialplan", tag="wakeup-deliver",
                                chan="PJSIP/19-1", billsec="3", rxcount="150",
                                txcount="150", rtt="0.005538", maxrtt="0.005538",
                                stdevrtt="0.000000", rxmes="82.5", txmes="82.5"))
    check("record: carries rtt_samples", rec.get("rtt_samples") == "single")

    rec2 = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-1",
                                 billsec="30", rxcount="1500", txcount="1500",
                                 rtt="0.004", maxrtt="0.012", stdevrtt="0.003",
                                 rxmes="88.0", txmes="88.0"))
    check("record: a well-sampled leg reads multi", rec2.get("rtt_samples") == "multi")


def test_playback_leg_is_attributed_to_its_extension_not_to_cid_zero() -> None:
    """A wake-up delivery leg must be filed under its extension.

    An audit reported that all three tag=wakeup-deliver records carried cid=0 and
    concluded "every wake-up delivery measurement is filed under a nonexistent
    extension 0". That is WRONG, and this pins why: ext is taken from the CHANNEL
    NAME, and only falls back to cid when the endpoint is not numeric (an inbound
    PJSIP/trunk-... leg, where the cid genuinely is the PSTN caller). cid=0 shows
    up only in the human-readable Verbose line."""
    rec = cq.build_record(_Args(source="dialplan", tag="wakeup-deliver",
                                chan="PJSIP/19-00000025", cid="0", billsec="8",
                                rxcount="400", txcount="400", rxmes="88", txmes="88"))
    check("attribution: filed under the channel's extension, not cid 0",
          rec["ext"] == "19")
    # The inbound-trunk case must keep the old behaviour.
    rec2 = cq.build_record(_Args(source="dialplan", tag="from-trunk",
                                 chan="PJSIP/trunk-0000001", cid="5551234",
                                 billsec="20", rxcount="1000", txcount="1000"))
    check("attribution: non-numeric endpoint still keeps the caller id",
          rec2["ext"] == "5551234")


def test_stage_records_how_far_a_scripted_leg_got() -> None:
    """A wake-up cut off before the greeting must not look like a completed one.

    The ledger previously recorded only "ring queued=True" plus "answered". One
    snoozed wake-up produced three attempts that died at dialplan steps 6, 5 and
    9, and nothing in the record distinguished them from a delivery that ran to
    completion. A wake-up that half-played is a wake-up that failed."""
    cut = cq.build_record(_Args(source="dialplan", tag="wakeup-deliver",
                                chan="PJSIP/19-1", stage="greeting", billsec="2",
                                rxcount="100", txcount="100"))
    done = cq.build_record(_Args(source="dialplan", tag="wakeup-deliver",
                                 chan="PJSIP/19-2", stage="complete", billsec="21",
                                 rxcount="1050", txcount="1050"))
    check("stage: a cut-off delivery records where it stopped",
          cut["stage"] == "greeting")
    check("stage: a completed delivery says so", done["stage"] == "complete")
    check("stage: the two are distinguishable", cut["stage"] != done["stage"])
    # An ordinary room call defines no stages and must not invent one.
    plain = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-1",
                                  billsec="30", rxcount="1500", txcount="1500"))
    check("stage: empty for a context with no stages", plain["stage"] == "")


def test_unsampled_rtt_is_null_not_a_convincing_zero() -> None:
    """A leg with no completed RTCP round must not publish 0.0 as a measurement.

    Asterisk reports rtt, rxjitter, rxmes and txmes as 0.000000 TOGETHER when no
    round completed. _credible() already nulls the MES pair, but a 0 ms RTT and
    0 ms jitter read as a flawless call rather than as no measurement, and were
    forwarded unqualified into every aggregate. Live exemplar: a 3-second
    wakeup-deliver leg with rxcount=156, all four fields exactly zero."""
    rec = cq.build_record(_Args(source="dialplan", tag="wakeup-deliver",
                                chan="PJSIP/19-1", billsec="3",
                                rxcount="156", txcount="109",
                                rxploss="0", txploss="0",
                                rxjitter="0.000000", txjitter="0.003750",
                                # ALL of these arrive as the literal "0.000000",
                                # not absent -- that is what the live record
                                # showed (rtt_ms=null beside rtt_mean_ms=0.0).
                                # Omitting them from the fixture made them None
                                # for the wrong reason and hid two mutants.
                                rtt="0.000000", maxrtt="0.000000",
                                stdevrtt="0.000000", normdevrtt="0.000000",
                                minrtt="0.000000",
                                rxmes="0.000000", txmes="0.000000"))
    check("unsampled: labelled as no sampling", rec["rtt_samples"] == "none")
    check("unsampled: RTT is null, not 0.0", rec["rtt_ms"] is None)
    # The WHOLE family, not just the headline. A live record showed rtt_ms=null
    # beside rtt_mean_ms=0.0 on the same leg -- a consumer averaging the mean saw
    # a flawless 0 ms call. Half a fix is arguably worse than none here, because
    # the one nulled field implies the others were checked.
    for f in ("rtt_mean_ms", "rtt_min_ms", "rtt_max_ms", "rtt_stdev_ms"):
        check(f"unsampled: {f} is null too, not 0.0", rec[f] is None)
    check("unsampled: rx jitter is null, not 0.0", rec["jitter_rx_last_ms"] is None)
    check("unsampled: MES stays null (pre-existing _credible behaviour)",
          rec["mes_worst"] is None)
    check("unsampled: quality reads unknown, not excellent",
          rec["quality"] == "unknown")
    # A direction that WAS measured must survive -- do not null the whole record.
    check("unsampled: the measured tx jitter is kept", rec["jitter_tx_last_ms"] == 3.75)

    # ...and a genuinely fast call must NOT be nulled just for being fast.
    good = cq.build_record(_Args(source="dialplan", tag="rooms", chan="PJSIP/12-1",
                                 billsec="30", rxcount="1500", txcount="1500",
                                 rtt="0.004", maxrtt="0.012", stdevrtt="0.003",
                                 rxjitter="0.002", txjitter="0.002",
                                 rxmes="88", txmes="88"))
    check("measured: a real low RTT is preserved", good["rtt_ms"] == 4.0)
    check("measured: the mean survives on a sampled leg", good["rtt_max_ms"] == 12.0)
    check("measured: real jitter is preserved", good["jitter_rx_last_ms"] == 2.0)


def test_mean_and_min_rtt_are_recorded() -> None:
    """--rtt is the LAST RTCP round, not the mean.

    A live announce reported rtt=0.007675 against maxrtt=0.146652 -- a 19x
    spread -- so publishing --rtt as "the call's RTT" published one arbitrary
    draw from the distribution. A threshold alarm on it under-fires while
    rtt_samples simultaneously certifies the leg as well-sampled."""
    rec = cq.build_record(_Args(source="dialplan", tag="announce",
                                chan="PJSIP/19-1", billsec="72",
                                rxcount="3626", txcount="3629",
                                rtt="0.007675", maxrtt="0.146652",
                                minrtt="0.004100", normdevrtt="0.031000",
                                stdevrtt="0.036807", rxmes="88", txmes="88"))
    check("rtt: the last round is still recorded", rec["rtt_ms"] == 7.68)
    check("rtt: the MEAN is now recorded too", rec["rtt_mean_ms"] == 31.0)
    check("rtt: the floor is recorded", rec["rtt_min_ms"] == 4.1)
    check("rtt: the peak is recorded", rec["rtt_max_ms"] == 146.65)
    check("rtt: a consumer can now see the spread, not one draw",
          rec["rtt_max_ms"] / max(rec["rtt_ms"], 0.01) > 15)
    check("rtt: still labelled multi (stdev is non-zero)",
          rec["rtt_samples"] == "multi")


def test_outcome_is_mirrored_somewhere_readable(tmp_path) -> None:
    """callqos must leave readable proof it ran and what it decided.

    The dialplan launches it as TrySystem(... --detach ... &). The trailing '&'
    makes the shell exit 0 immediately, so the dialplan proves only that the
    process STARTED -- never that it parsed its arguments, scored the call, or
    wrote anything. An audit found 13/13 log hits were invocation lines and ZERO
    were output from this program. The full ledger lives in /data, which cannot
    be read from outside the container."""
    import json as _json

    out = tmp_path / "sub" / "outcomes.jsonl"          # nested: must mkdir
    real = cq.SHARE_OUTCOME_PATH
    cq.SHARE_OUTCOME_PATH = str(out)
    try:
        rec = cq.build_record(_Args(source="dialplan", tag="announce",
                                    chan="PJSIP/19-1", stage="complete",
                                    billsec="72", rxcount="3626", txcount="3629",
                                    rtt="0.0077", maxrtt="0.1467",
                                    normdevrtt="0.031", stdevrtt="0.0368",
                                    rxmes="88", txmes="88"))
        cq.append_outcome(rec)
    finally:
        cq.SHARE_OUTCOME_PATH = real

    recs = [_json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    check("outcome: one line written", len(recs) == 1)
    r = recs[0]
    # The mirror must carry the WHOLE record. A curated subset made an audit
    # conclude the omitted fields were being discarded before the ledger write --
    # /data is unreadable from outside, so this file is the only view there is,
    # and on that surface absence reads as loss.
    check("outcome: mirrors every ledger field, not a curated subset",
          set(r) == set(rec))
    for k in ("ts", "ext", "tag", "stage", "quality", "rtt_samples",
              "loss_rx_pct", "jitter_rx_last_ms", "rtt_max_ms", "mes_rx", "reasons",
              "chan", "hcause", "rxcount"):
        check(f"outcome: carries {k}", k in r)
    check("outcome: says which extension", r["ext"] == "19")
    check("outcome: says how far the leg got", r["stage"] == "complete")
    check("outcome: carries the verdict, not just the inputs",
          r["quality"] in ("excellent", "good", "fair", "poor", "unknown"))
    check("outcome: carries the mean RTT so the spread is visible outside",
          r.get("rtt_mean_ms") == 31.0)

    # An unwritable mirror must never break the ledger path.
    cq.SHARE_OUTCOME_PATH = "/proc/cannot/write/here.jsonl"
    try:
        cq.append_outcome({"ts": 1, "ext": "19"})   # must not raise
        check("outcome: an unwritable mirror is swallowed", True)
    finally:
        cq.SHARE_OUTCOME_PATH = real


def test_main_writes_both_the_ledger_and_the_readable_outcome(tmp_path) -> None:
    """Drive main() as the dialplan does -- the call site must be exercised.

    Unit-testing append_outcome() proves the function works and says nothing
    about whether main() calls it. Mutation testing caught exactly that:
    replacing the call with `pass` left the entire suite green. This project has
    shipped a feature that was wired, tested and mutation-proven while the
    production call site never referenced it, so the call site gets its own test.

    Note --detach is deliberately NOT passed: it forks, which would take the test
    runner out from under itself."""
    import json as _json

    ledger = tmp_path / "callqos.jsonl"
    outcome = tmp_path / "sub" / "outcomes.jsonl"
    real_path, real_out = cq.PATH, cq.SHARE_OUTCOME_PATH

    class _FakeHA:
        @staticmethod
        def set_state(eid, state, attrs=None): return True
        @staticmethod
        def notify(*a, **k): return True

    sys.modules["ha_client"] = _FakeHA
    cq.PATH, cq.SHARE_OUTCOME_PATH = str(ledger), str(outcome)
    try:
        rc = cq.main(["--source", "dialplan", "--tag", "announce",
                      "--chan", "PJSIP/19-00000007", "--cid", "8000",
                      "--codec", "ulaw", "--billsec", "72", "--hcause", "16",
                      "--rxcount", "3626", "--txcount", "3629",
                      "--stage", "complete",
                      "--rtt", "0.0077", "--maxrtt", "0.1467",
                      "--normdevrtt", "0.031", "--minrtt", "0.0041",
                      "--stdevrtt", "0.0368",
                      "--rxmes", "88.0", "--txmes", "88.0"])
    finally:
        cq.PATH, cq.SHARE_OUTCOME_PATH = real_path, real_out
        sys.modules.pop("ha_client", None)

    check("main: returns 0 (a hangup handler must never fail loudly)", rc == 0)
    check("main: wrote the durable ledger", ledger.exists())
    check("main: ALSO wrote the readable outcome mirror", outcome.exists())

    o = _json.loads(outcome.read_text().splitlines()[-1])
    check("main: the mirror names the extension", o["ext"] == "19")
    check("main: the mirror carries the stage", o["stage"] == "complete")
    check("main: the mirror carries the verdict", o["quality"] == "excellent")
    check("main: the mirror carries the mean RTT", o["rtt_mean_ms"] == 31.0)
    check("main: playback tags are recorded but never notify",
          o["notify"] is False)


# --------------------------------------------------------------------------- #
# v0.77.0 — the three scoring defects an audit measured against the live ledger.
# Every fixture below is a REAL record's raw values, not an invented shape: the
# point of the audit was that these calls were misgraded in production, so the
# regression test has to be the call that was actually misgraded.
# --------------------------------------------------------------------------- #
def test_rtt_is_scored_on_the_distribution_not_the_last_draw() -> None:
    """F06. `rtt_ms` is CHANNEL(rtcp,rtt) — the FINAL round, one arbitrary draw.

    Across 55 measured legs its maximum was 163.62 ms, so the 400 ms threshold
    tested against it was unreachable by construction while `rtt_max_ms` in the
    same records reached 845.93 ms. The detector could not fire. Ledger record 30
    (Sep 3 06:00:08, wake-up, 39 s) is the exemplar: classify() saw 1.3% of the
    peak, a 79.4x understatement.
    """
    a = _Args(chan="PJSIP/19-0000001e", tag="wakeup", billsec="39",
              rxcount="1950", txcount="1950", rxmes="86.0", txmes="86.0",
              rtt="0.01065", minrtt="0.00462", normdevrtt="0.16948",
              maxrtt="0.84593", stdevrtt="0.30441")
    rec = cq.build_record(a)
    check("F06: the 846ms peak reaches the record", rec["rtt_max_ms"] > 800)
    check("F06: a call whose RTT peaked at 846ms now raises something",
          rec["notify"] is True)
    check("F06: and the reason names the peak, not the last round",
          any("peak" in r for r in rec["reasons"]))
    # The old threshold, on the old field, on the same record: still silent.
    _, old_notify, _ = cq.classify(86.0, 0.0, rec["rtt_ms"])
    check("F06: precondition — the OLD single-draw test really was unreachable",
          old_notify is False)
    # The mean and the peak are INDEPENDENT tests. No record in the measured
    # population crosses the mean threshold (its observed maximum was 169.48 ms
    # against a 250 ms line), so without this case the mean's threshold could be
    # set to any number at all and every test would still pass — the mutation
    # harness found exactly that. A sustained 300 ms mean with an unremarkable
    # peak is a real shape: a steadily distant or congested path rather than a
    # bursty one, and it is the one the peak test cannot see.
    slow = _Args(chan="PJSIP/19-0000002a", tag="rooms", billsec="60",
                 rxcount="3000", txcount="3000", rxmes="86.0", txmes="86.0",
                 rtt="0.30", minrtt="0.28", normdevrtt="0.30",
                 maxrtt="0.34", stdevrtt="0.02")
    slow_rec = cq.build_record(slow)
    check("F06: a sustained high MEAN alerts on its own",
          slow_rec["notify"] is True)
    check("F06: and says it was the mean, not the peak",
          any("mean" in r for r in slow_rec["reasons"]))
    check("F06: precondition — its peak is BELOW the peak threshold",
          slow_rec["rtt_max_ms"] < 500)
    # ...and a well-sampled call with an ordinary distribution stays quiet.
    b = _Args(chan="PJSIP/16-00000004", tag="rooms", billsec="60",
              rxcount="3000", txcount="3000", rxmes="88.0", txmes="88.0",
              rtt="0.012", minrtt="0.008", normdevrtt="0.014",
              maxrtt="0.031", stdevrtt="0.004")
    check("F06: an ordinary well-sampled call is untouched",
          cq.build_record(b)["notify"] is False)


def test_the_no_measurement_mes_constant_is_not_graded_as_excellent() -> None:
    """F27/F24. Asterisk leaves MES at 88.087887 when it has nothing to report.

    Six of 55 legs carried that exact float, always beside rtt=0.000000 or
    maxrtt==rtt with stdevrtt=0.000000. Three of them were graded `excellent`
    while a fourth no-RTCP leg was graded `unknown` — the verdict was decided by
    which of Asterisk's two no-data constants happened to land in the field.

    Raw invocation, verbatim from asterisk.log (Sep 1 23:34:46, the second
    zero-billsec call, which an earlier finding asserted did not exist):
      codec=ulaw billsec=0 hcause=16 rxcount=53 txcount=54 rxploss=0 txploss=0
      rxjitter=0.007250 txjitter=0.002625 rtt=0.000000
      rxmes=88.087887 txmes=0.000000 tag=rooms
    """
    a = _Args(chan="PJSIP/19-00000014", cid="19", tag="rooms", codec="ulaw",
              billsec="0", hcause="16", rxcount="53", txcount="54",
              rxploss="0", txploss="0", rxjitter="0.007250",
              txjitter="0.002625", rtt="0.000000",
              rxmes="88.087887", txmes="0.000000")
    rec = cq.build_record(a)
    check("F27: no RTCP round completed", rec["rtt_samples"] == "none")
    check("F27: the sentinel MES is no longer graded excellent",
          rec["quality"] == "unknown")
    check("F27: and no MES is published from it", rec["mes_worst"] is None)
    check("F27: which is the SAME verdict the 0.0 sentinel already produced",
          cq.build_record(_Args(chan="PJSIP/19-00000015", tag="rooms",
                                rxcount="53", txcount="54",
                                rxmes="0.0", txmes="0.0"))["quality"]
          == rec["quality"])
    # The guard must be narrow: a genuinely excellent WELL-SAMPLED call that
    # happens to land near 88.09 keeps its score.
    # BOTH directions at the sentinel value, on a well-sampled leg. With only
    # one direction at 88.087887 the other one carries the score, so a guard
    # that had dropped the `sampling != "multi"` condition entirely still looked
    # correct — the mutation harness caught that. Requiring both closes it: if
    # the guard widens, this call has no MES left at all and reads `unknown`.
    b = _Args(chan="PJSIP/16-00000009", tag="rooms", billsec="45",
              rxcount="2250", txcount="2250", rxmes="88.087887",
              txmes="88.087887", rtt="0.012", maxrtt="0.031", stdevrtt="0.004")
    rec_b = cq.build_record(b)
    check("F27: precondition — this leg really is well sampled",
          rec_b["rtt_samples"] == "multi")
    check("F27: a well-sampled 88.09 is NOT discarded",
          rec_b["quality"] == "excellent" and rec_b["mes_worst"] is not None)


def test_a_demoted_record_always_explains_itself() -> None:
    """F30. Loss between 0.5% and 3.0% moved the label and recorded no reason.

    Sep 1 06:05:49, wake-up to ext 19: rxploss=10 against rxcount=765 = 1.307%
    receive loss, rxmes 87.614 / txmes 84.417. It reached the ledger as
    `quality: "good", reasons: [], notify: false` — the label said something was
    wrong and every field that could say what was empty. Hit twice, both on
    wake-ups, which is the one call kind nobody is awake to notice.
    """
    a = _Args(chan="PJSIP/19-00000011", tag="wakeup", billsec="15",
              rxcount="765", txcount="750", rxploss="10", txploss="0",
              rxmes="87.614", txmes="84.417",
              rtt="0.02", maxrtt="0.04", stdevrtt="0.006")
    rec = cq.build_record(a)
    check("F30: still 'good' — the LABEL was never the bug",
          rec["quality"] == "good")
    check("F30: the demotion now explains itself", rec["reasons"] != [])
    check("F30: and it names the loss that caused it",
          any("loss" in r for r in rec["reasons"]))
    check("F30: 1.3% loss still does NOT wake anyone — alerting is unchanged",
          rec["notify"] is False)
    # An excellent call explains nothing, because there is nothing to explain.
    b = _Args(chan="PJSIP/16-0000000a", tag="rooms", billsec="30",
              rxcount="1500", txcount="1500", rxmes="88.0", txmes="88.0",
              rtt="0.01", maxrtt="0.02", stdevrtt="0.003")
    rec_b = cq.build_record(b)
    check("F30: an excellent record stays silent",
          rec_b["quality"] == "excellent" and rec_b["reasons"] == [])


def test_the_jitter_field_names_do_not_assert_a_false_relationship() -> None:
    """F52. `jitter_rx_max_ms` read as the maximum OF `jitter_rx_ms`. It is not.

    In 22 of 28 live records the "max" was SMALLER than the field it appeared to
    bound — idx37: 19.88 vs 0.38, a 52.3x inversion. They are different
    quantities: one is Asterisk's interarrival estimate read once at hangup, the
    other its maxrxjitter, the peak of that estimate sampled at every RTCP report
    instant. A name that implies an ordering the data contradicts sends a reader
    looking for a bug in the data.
    """
    a = _Args(source="dialplan", tag="rooms", chan="PJSIP/16-00000025",
              billsec="30", rxcount="1500", txcount="1500",
              rxmes="88", txmes="88", rtt="0.01", maxrtt="0.02",
              stdevrtt="0.003",
              rxjitter="0.01988", rxmaxjitter="0.00038")   # the real idx37 pair
    rec = cq.build_record(a)
    check("F52: the honest names are what the record carries",
          {"jitter_rx_last_ms", "jitter_rx_peak_local_ms",
           "jitter_tx_last_ms"} <= set(rec))
    check("F52: and the names that asserted a false ordering are gone",
          not {"jitter_rx_ms", "jitter_rx_max_ms", "jitter_tx_ms"} & set(rec))
    check("F52: the inversion itself is preserved, not 'corrected' away",
          rec["jitter_rx_last_ms"] > rec["jitter_rx_peak_local_ms"])
    check("F52: the usable peak reaches HA, not just the ledger",
          "jitter_rx_peak_local_ms" in _ha_attrs_for(rec))


def _ha_attrs_for(rec):
    """The attribute dict push_ha would set, without touching HA."""
    seen = {}

    class _Stub:
        @staticmethod
        def set_state(eid, val, attrs):
            seen.update(attrs)

        @staticmethod
        def notify(*a, **k):
            pass
    import sys
    sys.modules["ha_client"] = _Stub
    try:
        cq.push_ha(dict(rec, source="dialplan", tag="rooms"))
    finally:
        sys.modules.pop("ha_client", None)
    return seen


def test_every_record_says_which_schema_it_is() -> None:
    """F54. The /share mirror carried two shapes and no way to tell them apart.

    An audit found 20 lines of 11 fields and 30 of 32 in one file, separated them
    by counting keys, and reasonably concluded the other 21 fields were being
    discarded before the write. They were not — they were in a ledger the audit
    could not read. A version stamp is the difference between a reader inferring
    the shape and being told it.
    """
    rec = cq.build_record(_Args(source="dialplan", tag="rooms",
                                chan="PJSIP/12-1", rxcount="100",
                                txcount="100", rxmes="88", txmes="88"))
    check("F54: the ledger record is versioned", rec.get("v") == 3)
    d = tempfile.mkdtemp()
    try:
        cq.SHARE_OUTCOME_PATH = os.path.join(d, "callqos-outcomes.jsonl")
        cq.append_outcome(rec)
        line = json.loads(open(cq.SHARE_OUTCOME_PATH).read().splitlines()[0])
        check("F54: and so is the /share mirror, which is the only view an "
              "outside audit gets", line.get("v") == 3)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_codec_reports_the_wire_format_not_the_read_format() -> None:
    """F51/F57. `codec` carried CHANNEL(audioreadformat) — a different quantity.

    pjsip.conf sets `disallow = all` / `allow = ulaw` on the shared
    [room-endpoint](!) template and again on [trunk] — the only allow lines in
    the whole generated file — so ulaw is the only codec that can ever be
    negotiated, for handsets and trunk alike. Yet 3 of 30 records named "slin",
    and two assistant rows on the SAME handset 87 seconds apart disagreed with
    each other, which is impossible on the wire. All three slin rows were
    record()-driven AGI legs: the field was reporting transcoding, not the
    codec. That signal is real and is kept — under a name that means it.
    """
    a = _Args(source="dialplan", tag="assistant", chan="PJSIP/19-00000003",
              codec="(ulaw)", readformat="slin", billsec="66",
              rxcount="3300", txcount="3300", rxmes="88", txmes="88")
    rec = cq.build_record(a)
    check("F51: codec is the negotiated wire format", rec["codec"] == "ulaw")
    check("F51: Asterisk's list rendering is unwrapped, not stored raw",
          "(" not in rec["codec"])
    check("F57: the transcoding signal survives under an honest name",
          rec["read_format"] == "slin")
    # Fail-safe: a channel that reports no native format must not regress the
    # field to empty — being slightly wrong beats going blank.
    b = _Args(source="dialplan", tag="rooms", chan="PJSIP/16-1",
              codec="", readformat="ulaw", rxcount="100", txcount="100",
              rxmes="88", txmes="88")
    check("F51: falls back to the read format rather than emitting nothing",
          cq.build_record(b)["codec"] == "ulaw")


def test_a_call_that_carried_no_media_still_reaches_the_ledger() -> None:
    """F31. An abandoned intercom call was indistinguishable from no call.

    Sep 1 23:31, three genuine room-to-room calls: ext 19 dialled 14, then 15,
    then 16; each rang and was abandoned before answer. asterisk.log has all
    three (`PJSIP/14-0000000a is ringing`, then `Spawn extension (rooms, 14, 4)
    exited non-zero`, then `has no audio media/RTP session`). The ledger has
    none of them, because the no-RTCP guard jumped straight to `done`.

    Silence in a quality ledger has to mean "nothing happened". Here it meant
    "three calls happened and nothing was written", which is the one thing a
    forensic record must never be able to say.
    """
    a = _Args(source="dialplan", tag="rooms", chan="PJSIP/19-00000009",
              cid="19", billsec="0", hcause="16", nomedia=True)
    rec = cq.build_record(a)
    check("F31: the abandoned call IS a record", isinstance(rec, dict))
    check("F31: labelled for what it is, not as a measurement failure",
          rec["quality"] == "no-media")
    check("F31: which is distinct from 'we measured and could not tell'",
          rec["quality"] != "unknown")
    check("F31: it says who", rec["ext"] == "19")
    check("F31: and which leg", rec["tag"] == "rooms")
    check("F31: it explains itself", rec["reasons"] == ["leg carried no RTP media"])
    check("F31: and it never wakes anyone", rec["notify"] is False)
    check("F31: no metrics are invented from a leg that measured nothing",
          rec["mes_worst"] is None and rec["rtt_ms"] is None)
    # The notify guard has to hold even when the scoring path WOULD have fired.
    # As the dialplan calls it today no-media rows carry no packet counts, so
    # nothing can raise an alert and the guard is unreachable — which means a
    # test written against today's invocation proves nothing about it (the
    # mutation harness confirmed: deleting the guard changed no result). Feed it
    # the shape that trips one-way detection, so the guard is what keeps an
    # abandoned call from paging the house if the invocation ever grows counts.
    loud = _Args(source="dialplan", tag="rooms", chan="PJSIP/19-0000000b",
                 cid="19", billsec="10", hcause="16", nomedia=True,
                 rxcount="1000", txcount="0", rxmes="88", txmes="0")
    check("F31: precondition — this shape DOES alert without the no-media flag",
          cq.build_record(_Args(source="dialplan", tag="rooms",
                                chan="PJSIP/19-0000000b", cid="19",
                                billsec="10", hcause="16", rxcount="1000",
                                txcount="0", rxmes="88",
                                txmes="0"))["notify"] is True)
    check("F31: a media-less leg never alerts, whatever else is in the record",
          cq.build_record(loud)["notify"] is False)


def test_an_answered_wakeup_that_played_nothing_is_not_scored_quietly() -> None:
    """F04/F36/F50/F62. The worst outcome this feature has, scored as the least.

    Sep 2 06:15:21 MST, verbatim from the ledger:

      {"tag": "wakeup-deliver", "stage": "scene", "ext": "19", "dur": 0,
       "rxcount": 34, "txcount": 0, "quality": "unknown", "notify": false,
       "reasons": [], "mes_worst": null, "hcause": 16}

    The cordless answered and the far end dropped one second later, during the
    `Wait(1)` that precedes the greeting Playback. Asterisk transmitted ZERO
    audio packets: somebody picked up an alarm call and heard nothing.

    It scored `unknown` with `notify: false` — the quietest verdict the ledger
    can produce — because MES-based scoring has nothing to score when no audio
    was sent, and no rule read `stage`. Both facts were already in the record.
    """
    a = _Args(source="dialplan", tag="wakeup-deliver", chan="PJSIP/19-00000002",
              cid="19", billsec="0", hcause="16", stage="scene",
              rxcount="34", txcount="0", rxmes="0.000000", txmes="0.000000",
              rtt="0.000000")
    rec = cq.build_record(a)
    check("F04: no longer the quietest verdict in the ledger",
          rec["quality"] == "undelivered")
    check("F04: and it is not 'unknown' — we know exactly what happened",
          rec["quality"] != "unknown")
    check("F04: the alarm-clock contract makes this alertable",
          rec["notify"] is True)
    check("F04: it says no audio was transmitted",
          any("no audio" in r for r in rec["reasons"]))
    check("F50/F62: and names the stage it stopped at",
          any("scene" in r for r in rec["reasons"]))


def test_a_truncated_delivery_is_named_but_only_the_alarm_clock_alerts() -> None:
    """F36. `wakeup-deliver` is the one playback tag with a hard deadline.

    A page or an announcement cut short is worth recording and not worth waking
    anyone over. A wake-up call that did not wake anyone is the opposite.
    """
    def leg(tag, stage, txcount="1500"):
        return cq.build_record(_Args(
            source="dialplan", tag=tag, chan="PJSIP/19-00000004", cid="19",
            billsec="30", stage=stage, rxcount="1500", txcount=txcount,
            rxmes="88", txmes="88", rtt="0.01", maxrtt="0.02", stdevrtt="0.003"))

    cut = leg("wakeup-deliver", "greeting")
    check("F36: a wake-up cut off mid-script is undelivered",
          cut["quality"] == "undelivered")
    check("F36: and it alerts", cut["notify"] is True)

    ann = leg("announce", "playing")
    check("F36: a truncated announcement is recorded as undelivered",
          ann["quality"] == "undelivered")
    check("F36: but stays silent — no alarm-clock contract",
          ann["notify"] is False)

    done = leg("wakeup-deliver", "complete")
    check("F36: a wake-up that ran to completion is untouched",
          done["quality"] == "excellent" and done["notify"] is False)
    # The control that matters most: a leg with no stage at all is an ordinary
    # call, not a scripted delivery, and must not be dragged into this rule.
    plain = leg("rooms", "")
    check("F36: an ordinary call with no stage is unaffected",
          plain["quality"] == "excellent" and plain["notify"] is False)


def test_a_telephone_number_is_not_mirrored_to_the_readable_folder() -> None:
    """v0.81.0. `ext` holds a full telephone number on trunk legs, and the
    /share mirror is readable from outside the container.

    Measured on the live ledger: 45 of 252 rows carried a complete number — 10
    inbound trunk legs where `ext` correctly holds the calling party, 32 internal
    `rooms` legs where a number had reached the field via the caller-ID rewrite,
    and 3 `operator` legs, two of which carry the trunk prefix plus a number this
    house DIALLED.

    /share is host-mounted and captured in add-on backups. The private /data
    ledger keeps the number in full; the mirror keeps only enough to correlate
    two legs of one call.
    """
    d = tempfile.mkdtemp()
    try:
        cq.SHARE_OUTCOME_PATH = os.path.join(d, "callqos-outcomes.jsonl")
        inbound = cq.build_record(_Args(source="dialplan", tag="from-trunk",
                                        chan="PJSIP/trunk-0000000a",
                                        cid="2025550147", billsec="30",
                                        rxcount="1500", txcount="1500",
                                        rxmes="88", txmes="88"))
        check("redact: the PRIVATE ledger keeps the number in full",
              inbound["ext"] == "2025550147")
        cq.append_outcome(inbound)
        line = json.loads(open(cq.SHARE_OUTCOME_PATH).read().splitlines()[-1])
        check("redact: the readable mirror does NOT",
              line["ext"] != "2025550147")
        check("redact: only the last four survive", line["ext"] == "******0147")
        check("redact: and the mirror says it was redacted",
              line.get("ext_redacted") is True)
        check("redact: nothing else is withheld — this is not a curated mirror",
              set(line) - {"ext_redacted"} == set(inbound))

        # An ordinary extension is untouched: it is not personal data, and
        # withholding it would break every reader of this file.
        room = cq.build_record(_Args(source="dialplan", tag="rooms",
                                     chan="PJSIP/16-00000004", billsec="30",
                                     rxcount="1500", txcount="1500",
                                     rxmes="88", txmes="88"))
        cq.append_outcome(room)
        line2 = json.loads(open(cq.SHARE_OUTCOME_PATH).read().splitlines()[-1])
        check("redact: a real extension is mirrored unchanged",
              line2["ext"] == "16" and "ext_redacted" not in line2)

        # A 6-digit extension is the longest the schema allows and must survive.
        long_ext = dict(room, ext="123456")
        cq.append_outcome(long_ext)
        line3 = json.loads(open(cq.SHARE_OUTCOME_PATH).read().splitlines()[-1])
        check("redact: the longest legal extension is not mistaken for a number",
              line3["ext"] == "123456")
    finally:
        shutil.rmtree(d, ignore_errors=True)
