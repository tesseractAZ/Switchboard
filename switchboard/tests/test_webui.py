"""Behavioral tests for the Ingress UI's AMI client (webui/ami.py).

Run with plain Python (no pytest, no FastAPI needed):

    python3 switchboard/tests/test_webui.py

These pin the AMI wire-format handling that has historically broken the
dashboard: field-name casing, the ContactList identity field, DeviceState-based
registration, the stream terminator, and auth-failure surfacing. Each block of
bytes below is a realistic capture of what Asterisk 20 sends.
"""
from importlib.machinery import SourceFileLoader
from pathlib import Path

AMI_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "usr" / "share" / "switchboard" / "webui" / "ami.py"
ami = SourceFileLoader("switchboard_ami", str(AMI_PATH)).load_module()

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


# A PJSIPShowEndpoints capture: banner, Login ack, action ack, two EndpointList
# events (one online "Not in use", one offline "Unavailable"), the trunk, then
# the terminator. Note the real Asterisk field casing (ObjectName/DeviceState).
ENDPOINTS = (
    "Asterisk Call Manager/9.0.0\r\n\r\n"
    "Response: Success\r\nMessage: Authentication accepted\r\n\r\n"
    "Response: Success\r\nEventList: start\r\nMessage: A listing of Endpoints follows\r\n\r\n"
    "Event: EndpointList\r\nObjectType: endpoint\r\nObjectName: 11\r\n"
    "DeviceState: Not in use\r\nActiveChannels: \r\n\r\n"
    "Event: EndpointList\r\nObjectType: endpoint\r\nObjectName: 14\r\n"
    "DeviceState: Unavailable\r\nActiveChannels: \r\n\r\n"
    "Event: EndpointList\r\nObjectType: endpoint\r\nObjectName: trunk\r\n"
    "DeviceState: Unavailable\r\nActiveChannels: \r\n\r\n"
    "Event: EndpointListComplete\r\nEventList: Complete\r\nListItems: 3\r\n\r\n"
).encode()

# A PJSIPShowContacts capture. The identity is in the `Endpoint` field — there is
# NO `Aor` field. Three contacts exercise: (a) Endpoint field present,
# (b) Endpoint missing but ObjectName "<aor>@@<hash>", (c) ObjectName
# "<aor>/sip:..." form. Statuses cover Reachable and NonQualified.
CONTACTS = (
    "Asterisk Call Manager/9.0.0\r\n\r\n"
    "Response: Success\r\nMessage: Authentication accepted\r\n\r\n"
    "Response: Success\r\nEventList: start\r\nMessage: A listing of Contacts follows\r\n\r\n"
    "Event: ContactList\r\nObjectType: contact\r\nObjectName: 11@@a88df67525\r\n"
    "Endpoint: 11\r\nUri: sip:11@192.168.6.65:5060\r\nStatus: Reachable\r\nRoundtripUsec: 3706\r\n\r\n"
    "Event: ContactList\r\nObjectType: contact\r\nObjectName: 12@@b1c2d3e4f5\r\n"
    "Uri: sip:12@192.168.6.65:5062\r\nStatus: NonQualified\r\nRoundtripUsec: 0\r\n\r\n"
    "Event: ContactList\r\nObjectType: contact\r\nObjectName: 13/sip:13@192.168.6.65:5064\r\n"
    "Uri: sip:13@192.168.6.65:5064\r\nStatus: Reachable\r\nRoundtripUsec: 4210\r\n\r\n"
    "Event: ContactListComplete\r\nEventList: Complete\r\nListItems: 3\r\n\r\n"
).encode()

AUTH_FAIL = (
    "Asterisk Call Manager/9.0.0\r\n\r\n"
    "Response: Error\r\nMessage: Authentication failed\r\n\r\n"
).encode()

# A CoreShowChannels capture (one active leg) — the third read in a status bundle.
CHANNELS = (
    "Response: Success\r\nEventList: start\r\nMessage: A listing of channels follows\r\n\r\n"
    "Event: CoreShowChannel\r\nChannel: PJSIP/11-00000001\r\nChannelStateDesc: Up\r\n"
    "CallerIDNum: 11\r\nLinkedid: c-1\r\n\r\n"
    "Event: CoreShowChannelsComplete\r\nEventList: Complete\r\nListItems: 1\r\n\r\n"
).encode()


def test_endpoints() -> None:
    eps = ami.endpoints_from_blocks(ami.parse_ami_blocks(ENDPOINTS))
    by = {e["name"]: e for e in eps}
    check("endpoints: all three parsed", len(eps) == 3)
    check("endpoints: device_state read with correct casing", by.get("11", {}).get("state") == "Not in use")
    check("endpoints: offline endpoint captured", by.get("14", {}).get("state") == "Unavailable")


def test_contacts() -> None:
    cs = ami.contacts_from_blocks(ami.parse_ami_blocks(CONTACTS))
    # The whole point: contacts key by the room ext, NOT by "" or "<aor>@@hash".
    check("contacts: keyed by Endpoint field -> '11'", "11" in cs)
    check("contacts: ObjectName '@@' fallback -> '12'", "12" in cs)
    check("contacts: ObjectName '/' fallback -> '13'", "13" in cs)
    check("contacts: not keyed by raw ObjectName", "11@@a88df67525" not in cs)
    check("contacts: status populated (real value, not empty)", cs.get("11", {}).get("status") == "Reachable")
    check("contacts: RTT populated", cs.get("11", {}).get("rtt") == "3706")


def test_registered() -> None:
    # DeviceState drives it: "Not in use" is registered; "Unavailable" is not.
    check("registered: Not in use -> True", ami.is_registered("Not in use") is True)
    check("registered: In use -> True", ami.is_registered("In use") is True)
    check("registered: Ringing -> True", ami.is_registered("Ringing") is True)
    check("registered: Unavailable -> False", ami.is_registered("Unavailable") is False)
    check("registered: Invalid -> False", ami.is_registered("Invalid") is False)
    check("registered: Unknown -> False", ami.is_registered("Unknown") is False)
    check("registered: empty -> False", ami.is_registered("") is False)
    # Secondary contact signal rescues a qualify-disabled (NonQualified) contact
    # even if device state were unknown.
    check("registered: NonQualified contact -> True", ami.is_registered("Unknown", "NonQualified") is True)
    check("registered: Reachable contact -> True", ami.is_registered("", "Reachable") is True)


def test_lowercasing() -> None:
    blocks = ami.parse_ami_blocks(
        b"Event: EndpointList\r\nObjectName: 11\r\nDeviceState: Not in use\r\n\r\n"
    )
    # Keys lower-cased, values preserved verbatim.
    check("parse: keys lower-cased", "objectname" in blocks[0] and "devicestate" in blocks[0])
    check("parse: values preserved", blocks[0]["devicestate"] == "Not in use")


def test_login_failure() -> None:
    check("auth: failure detected", ami.login_failed(ami.parse_ami_blocks(AUTH_FAIL)) is True)
    check("auth: success not flagged", ami.login_failed(ami.parse_ami_blocks(ENDPOINTS)) is False)


def test_stream_terminator() -> None:
    # Real terminator line ends the stream...
    check("term: real Event Complete line ends stream", ami.stream_complete(ENDPOINTS) is True)
    # ...but an attacker-controlled field VALUE containing "Complete" must not.
    spoof = (
        b"Event: CoreShowChannel\r\nChannel: PJSIP/11-0001\r\n"
        b"CallerIDName: Complete\r\nChannelStateDesc: Up\r\n\r\n"
    )
    check("term: spoofed CallerIDName 'Complete' does NOT end stream", ami.stream_complete(spoof) is False)
    check("term: partial stream (no terminator) not complete",
          ami.stream_complete(b"Event: ContactList\r\nEndpoint: 11\r\n\r\n") is False)


ROOMS_BY_EXT = {"11": "Kitchen", "16": "Office", "17": "Garage"}


def _coreshow(*legs: str) -> bytes:
    head = (
        "Asterisk Call Manager/9.0.0\r\n\r\n"
        "Response: Success\r\nMessage: Authentication accepted\r\n\r\n"
        "Response: Success\r\nEventList: start\r\nMessage: Channels will follow\r\n\r\n"
    )
    tail = "Event: CoreShowChannelsComplete\r\nEventList: Complete\r\nListItems: 0\r\n\r\n"
    return (head + "".join(legs) + tail).encode()


def test_channel_ext() -> None:
    check("channel_ext: PJSIP/11-0000000a -> 11", ami.channel_ext("PJSIP/11-0000000a") == "11")
    check("channel_ext: trunk", ami.channel_ext("PJSIP/trunk-00000001") == "trunk")
    check("channel_ext: drops ;1 half-channel marker", ami.channel_ext("PJSIP/11-0000000a;1") == "11")
    check("channel_ext: junk -> ''", ami.channel_ext("") == "")


def test_calls_internal() -> None:
    blocks = ami.parse_ami_blocks(_coreshow(
        "Event: CoreShowChannel\r\nChannel: PJSIP/11-00000001\r\nChannelStateDesc: Up\r\n"
        "CallerIDNum: 11\r\nConnectedLineNum: 16\r\nContext: rooms\r\nExten: 16\r\n"
        "Linkedid: call-A\r\nDuration: 00:00:42\r\n\r\n",
        "Event: CoreShowChannel\r\nChannel: PJSIP/16-00000002\r\nChannelStateDesc: Up\r\n"
        "CallerIDNum: 16\r\nConnectedLineNum: 11\r\nContext: rooms\r\nExten: s\r\n"
        "Linkedid: call-A\r\nDuration: 00:00:42\r\n\r\n",
    ))
    summary = ami.summarize_calls(ami.channels_from_blocks(blocks), ROOMS_BY_EXT)
    calls = summary["calls"]
    check("calls: one internal call", len(calls) == 1)
    check("calls: detail Kitchen <-> Office", calls[0]["detail"] == "Kitchen ↔ Office")
    check("calls: state Talking", calls[0]["state"] == "Talking")
    check("calls: kind internal", calls[0]["kind"] == "internal")
    check("calls: by_ext peer for Kitchen is Office", summary["by_ext"].get("11", {}).get("peer") == "Office")
    check("calls: by_ext peer for Office is Kitchen", summary["by_ext"].get("16", {}).get("peer") == "Kitchen")


def test_calls_outside() -> None:
    blocks = ami.parse_ami_blocks(_coreshow(
        "Event: CoreShowChannel\r\nChannel: PJSIP/17-00000003\r\nChannelStateDesc: Up\r\n"
        "CallerIDNum: 17\r\nConnectedLineNum: 14805551234\r\nContext: rooms\r\n"
        "Linkedid: call-B\r\nDuration: 00:01:05\r\n\r\n",
        "Event: CoreShowChannel\r\nChannel: PJSIP/trunk-00000004\r\nChannelStateDesc: Up\r\n"
        "CallerIDNum: 14805551234\r\nConnectedLineNum: 17\r\nContext: from-trunk\r\n"
        "Linkedid: call-B\r\nDuration: 00:01:05\r\n\r\n",
    ))
    summary = ami.summarize_calls(ami.channels_from_blocks(blocks), ROOMS_BY_EXT)
    call = summary["calls"][0]
    check("calls: outside kind", call["kind"] == "outside")
    check("calls: outside detail names Garage + Outside",
          call["detail"].startswith("Garage ↔ Outside") and "14805551234" in call["detail"])
    check("calls: Garage peer is the external number",
          "14805551234" in (summary["by_ext"].get("17", {}).get("peer") or ""))


def test_calls_operator() -> None:
    blocks = ami.parse_ami_blocks(_coreshow(
        "Event: CoreShowChannel\r\nChannel: PJSIP/11-00000005\r\nChannelStateDesc: Up\r\n"
        "CallerIDNum: 11\r\nContext: operator\r\nExten: s\r\nLinkedid: call-C\r\nDuration: 00:00:08\r\n\r\n",
    ))
    summary = ami.summarize_calls(ami.channels_from_blocks(blocks), ROOMS_BY_EXT)
    call = summary["calls"][0]
    check("calls: operator kind", call["kind"] == "operator")
    check("calls: operator detail Kitchen -> Operator", call["detail"] == "Kitchen → Operator")
    check("calls: Kitchen peer Operator", summary["by_ext"].get("11", {}).get("peer") == "Operator")


def test_calls_ringing() -> None:
    # Caller (Kitchen) up, callee (Office) ringing — two legs, one call.
    blocks = ami.parse_ami_blocks(_coreshow(
        "Event: CoreShowChannel\r\nChannel: PJSIP/11-00000006\r\nChannelStateDesc: Up\r\n"
        "CallerIDNum: 11\r\nConnectedLineNum: 16\r\nContext: rooms\r\nExten: 16\r\nLinkedid: call-D\r\nDuration: 00:00:03\r\n\r\n",
        "Event: CoreShowChannel\r\nChannel: PJSIP/16-00000007\r\nChannelStateDesc: Ringing\r\n"
        "CallerIDNum: 11\r\nConnectedLineNum: 11\r\nContext: rooms\r\nLinkedid: call-D\r\nDuration: 00:00:03\r\n\r\n",
    ))
    summary = ami.summarize_calls(ami.channels_from_blocks(blocks), ROOMS_BY_EXT)
    check("calls: ringing state", summary["calls"][0]["state"] == "Ringing")
    check("calls: ringing by_ext (callee)", summary["by_ext"].get("16", {}).get("state") == "Ringing")


def test_lone_leg_excluded() -> None:
    # A single test-ring (Playback) leg shows on the room card (by_ext) but is
    # NOT listed as an active call.
    blocks = ami.parse_ami_blocks(_coreshow(
        "Event: CoreShowChannel\r\nChannel: PJSIP/11-00000009\r\nChannelStateDesc: Ringing\r\n"
        "CallerIDNum: 11\r\nContext: \r\nLinkedid: ring-1\r\nDuration: 00:00:02\r\n\r\n",
    ))
    summary = ami.summarize_calls(ami.channels_from_blocks(blocks), ROOMS_BY_EXT)
    check("lone leg: not listed as an active call", summary["calls"] == [])
    check("lone leg: still shown on the room card", summary["by_ext"].get("11", {}).get("state") == "Ringing")


def test_ring_ext_guard() -> None:
    # The regex guard rejects a non-numeric ext before any AMI socket is opened.
    check("ring: rejects injection-y ext", ami.ring_extension("9;evil") is False)
    check("ring: rejects empty ext", ami.ring_extension("") is False)


def test_connect_hangup_guards() -> None:
    allowed = {"11", "12"}
    # connect_extensions only patches exts in the configured room set — a value
    # like "9911" must NOT be accepted (it could reach the trunk's _9. pattern).
    check("connect: rejects target not in room set", ami.connect_extensions("11", "9911", allowed) is False)
    check("connect: rejects source not in room set", ami.connect_extensions("99", "11", allowed) is False)
    check("connect: rejects injection-y ext", ami.connect_extensions("9;evil", "11", allowed) is False)
    check("connect: rejects empty to", ami.connect_extensions("11", "", allowed) is False)
    # hangup_channel rejects empty + CRLF (AMI-injection) channel strings.
    check("hangup: rejects empty channel", ami.hangup_channel("") is False)
    check("hangup: rejects CRLF channel", ami.hangup_channel("PJSIP/11\r\nAction: Command") is False)
    # originate_wakeup digit-guards the room ext before any socket.
    check("wakeup: originate rejects injection-y ext", ami.originate_wakeup("9;evil") is False)
    check("wakeup: originate rejects empty ext", ami.originate_wakeup("") is False)


def test_page_all_guard() -> None:
    # These all short-circuit on the _EXT_RE guard BEFORE any AMI socket: an
    # empty list has nothing to originate, and an injection-y / all-invalid list
    # is skipped entirely. No I/O happens, so the return is a pure False.
    check("page_all: empty list -> False (no I/O)", ami.page_all([]) is False)
    check("page_all: injection-y ext skipped -> False", ami.page_all(["9;evil"]) is False)
    check("page_all: CRLF ext skipped -> False", ami.page_all(["11\r\nAction: x"]) is False)
    check("page_all: all-invalid list -> False", ami.page_all(["", "abc", "1234567"]) is False)


def test_set_mwi_guard() -> None:
    # set_mwi validates the ext with _EXT_RE.fullmatch before any AMI socket, so
    # an empty or CRLF-bearing ext is rejected without touching the network.
    check("set_mwi: empty ext -> False", ami.set_mwi("", True) is False)
    check("set_mwi: CRLF ext -> False", ami.set_mwi("1\r\n2", True) is False)
    check("set_mwi: non-digit ext -> False", ami.set_mwi("9;evil", False) is False)
    check("set_mwi: over-long ext -> False", ami.set_mwi("1234567", True) is False)


def test_no_calls() -> None:
    summary = ami.summarize_calls([], ROOMS_BY_EXT)
    check("calls: empty -> no calls", summary["calls"] == [] and summary["by_ext"] == {})


def test_actions_complete() -> None:
    # The status bundle reads three list actions over one connection; the read may
    # only end once ALL three have emitted their own "...Complete" (matched by
    # ActionID), never on a spoofed field value or some other action's Complete.
    ids = {"A", "B", "C"}
    partial = (
        "Event: EndpointListComplete\r\nActionID: A\r\n\r\n"
        "Event: ContactListComplete\r\nActionID: B\r\n\r\n"
    ).encode()
    check("multi-term: not done until all three Complete", ami.actions_complete(partial, ids) is False)
    full = partial + b"Event: CoreShowChannelsComplete\r\nActionID: C\r\n\r\n"
    check("multi-term: done when all three Complete present", ami.actions_complete(full, ids) is True)
    # A field value ending in "Complete" (not an Event name) must not satisfy C.
    spoof = (
        "Event: CoreShowChannel\r\nActionID: C\r\nCallerIDName: Job Complete\r\n\r\n"
        "Event: EndpointListComplete\r\nActionID: A\r\n\r\n"
        "Event: ContactListComplete\r\nActionID: B\r\n\r\n"
    ).encode()
    check("multi-term: spoofed field 'Complete' does not satisfy C", ami.actions_complete(spoof, ids) is False)
    # A Complete for an ActionID we didn't ask for can't satisfy a wanted one.
    foreign = (
        "Event: EndpointListComplete\r\nActionID: A\r\n\r\n"
        "Event: ContactListComplete\r\nActionID: B\r\n\r\n"
        "Event: CoreShowChannelsComplete\r\nActionID: Z\r\n\r\n"
    ).encode()
    check("multi-term: foreign ActionID Complete is ignored", ami.actions_complete(foreign, ids) is False)


def test_status_bundle_parse() -> None:
    # get_status_bundle reads endpoints + contacts + channels over ONE socket.
    # The concatenated stream (each action keeps its own ack + events) must still
    # parse cleanly per event type — the parsers filter by Event, so merging them
    # is safe. (The socket wiring of _ami_actions mirrors the tested _ami_command.)
    combined = ENDPOINTS + CONTACTS + CHANNELS
    blocks = ami.parse_ami_blocks(combined)
    eps = ami.endpoints_from_blocks(blocks)
    cs = ami.contacts_from_blocks(blocks)
    chans = ami.channels_from_blocks(blocks)
    check("bundle: endpoints parsed from combined stream", [e["name"] for e in eps] == ["11", "14", "trunk"])
    check("bundle: contacts parsed from combined stream", set(cs) == {"11", "12", "13"})
    check("bundle: channels parsed from combined stream", [c["ext"] for c in chans] == ["11"])


class _FakeAMISocket:
    """A canned AMI peer for driving the _ami_actions socket loop without a real
    Asterisk: banner, then a response built from the ActionIDs the caller actually
    sent, then a read timeout. ``echo`` toggles whether the terminating events
    carry the ActionID (the load-bearing assumption); ``auth_ok`` the login."""

    def __init__(self, echo: bool = True, auth_ok: bool = True) -> None:
        self.echo, self.auth_ok, self.sent, self._n = echo, auth_ok, b"", 0

    def settimeout(self, _t) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def __enter__(self):
        return self

    def __exit__(self, *_a) -> bool:
        return False

    def close(self) -> None:
        pass

    def recv(self, _n: int) -> bytes:
        self._n += 1
        if self._n == 1:
            return b"Asterisk Call Manager/9.0.0\r\n\r\n"
        if self._n == 2:
            return self._response()
        import socket as _s
        raise _s.timeout()  # a real read would block here, then time out

    def _response(self) -> bytes:
        if not self.auth_ok:
            return b"Response: Error\r\nMessage: Authentication failed\r\n\r\n"
        import re
        ep, ct, ch = (re.findall(r"ActionID: (\S+)", self.sent.decode()) + ["", "", ""])[:3]
        def tag(a: str) -> str:
            return f"ActionID: {a}\r\n" if (self.echo and a) else ""
        return (
            "Response: Success\r\nMessage: Authentication accepted\r\n\r\n"
            f"Event: EndpointList\r\n{tag(ep)}ObjectName: 11\r\nDeviceState: Not in use\r\n\r\n"
            f"Event: EndpointListComplete\r\n{tag(ep)}ListItems: 1\r\n\r\n"
            f"Event: ContactList\r\n{tag(ct)}Endpoint: 11\r\nStatus: Reachable\r\nUri: sip:11@x\r\n\r\n"
            f"Event: ContactListComplete\r\n{tag(ct)}ListItems: 1\r\n\r\n"
            f"Event: CoreShowChannel\r\n{tag(ch)}Channel: PJSIP/11-1\r\nChannelStateDesc: Up\r\nLinkedid: c1\r\n\r\n"
            f"Event: CoreShowChannelsComplete\r\n{tag(ch)}ListItems: 1\r\n\r\n"
        ).encode()


def test_status_bundle_socket_loop() -> None:
    # Drive get_status_bundle()/_ami_actions end-to-end over a fake socket — the
    # one genuinely new branch (send ActionIDs, read until each action's tagged
    # Complete). Covers: happy path; the spec-says-impossible "ActionID not echoed"
    # case (must still parse correct data via the timeout, not hang/corrupt); and
    # an auth failure (must raise). Guards a future Asterisk that drops ActionID
    # echo from silently slowing the live board with no test signal.
    orig_conn, orig_secret = ami.socket.create_connection, ami.AMI_SECRET
    ami.AMI_SECRET = "test-secret"

    def run(fake):
        ami.socket.create_connection = lambda *a, **k: fake
        try:
            return ami.get_status_bundle()
        finally:
            ami.socket.create_connection = orig_conn

    try:
        fake = _FakeAMISocket(echo=True)
        eps, cs, chans = run(fake)
        check("bundle e2e: endpoints parsed over the socket", [e["name"] for e in eps] == ["11"])
        check("bundle e2e: contacts parsed over the socket", set(cs) == {"11"})
        check("bundle e2e: channels parsed over the socket", [c["ext"] for c in chans] == ["11"])
        check("bundle e2e: three ActionIDs were actually sent", fake.sent.count(b"ActionID:") == 3)

        eps2, cs2, chans2 = run(_FakeAMISocket(echo=False))
        check("bundle e2e: no-echo still yields correct data (fail-safe latency, not bad data)",
              [e["name"] for e in eps2] == ["11"] and set(cs2) == {"11"} and [c["ext"] for c in chans2] == ["11"])

        raised = False
        try:
            run(_FakeAMISocket(auth_ok=False))
        except ami.AMIError:
            raised = True
        check("bundle e2e: auth failure raises AMIError", raised)
    finally:
        ami.socket.create_connection = orig_conn
        ami.AMI_SECRET = orig_secret


def main() -> None:
    test_endpoints()
    test_actions_complete()
    test_status_bundle_parse()
    test_status_bundle_socket_loop()
    test_contacts()
    test_registered()
    test_lowercasing()
    test_login_failure()
    test_stream_terminator()
    test_channel_ext()
    test_calls_internal()
    test_calls_outside()
    test_calls_operator()
    test_calls_ringing()
    test_lone_leg_excluded()
    test_ring_ext_guard()
    test_connect_hangup_guards()
    test_page_all_guard()
    test_set_mwi_guard()
    test_no_calls()
    print()
    if _failures:
        print(f"{_failures} FAILURE(S)")
        raise SystemExit(1)
    print("all webui AMI tests passed")


if __name__ == "__main__":
    main()
