"""Behavioral tests for switchboard-callqos — the per-call quality sink.

Run with plain Python (no pytest needed):

    python3 switchboard/tests/test_callqos.py

Pins down the quality classification, the tolerant parsing (RTCP can emit "" /
"unavailable" / non-finite), the durable JSONL ledger (append + cap), and the HA
routing (dialplan drives the sensor; the notification is gate-able + dedup-keyed).
"""
import json
import os
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


class _Args:
    """A stand-in for argparse.Namespace with the defaults callqos expects."""
    _FIELDS = ("source tag chan cid codec billsec hcause rxcount txcount rxploss "
               "txploss rxjitter txjitter rtt rxmes txmes maxrtt stdevrtt rxmaxjitter "
               "rxoctet txoctet").split()

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
          rec["rtt_max_ms"] is None and rec["rx_octets"] is None)
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
        check("ha: dialplan poor call notifies, id keyed by channel",
              calls["notify"] and calls["notify"][-1] == "switchboard_callqos_PJSIP_12-2")

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


if __name__ == "__main__":
    test_classify()
    test_tolerant_parsing()
    test_build_record()
    test_ledger_append_and_cap()
    test_ha_routing()
    test_one_way_audio()
    test_argv_sanitizes_nonfinite()
    test_alerts_option_read_from_features()
    test_detach_gating()
    test_main_never_raises()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    raise SystemExit(1 if _failures else 0)
