"""Minimal synchronous Asterisk Manager Interface (AMI) client for the
Switchboard Ingress UI.

Deliberately framework-free (no FastAPI import) so the parsing and
state-derivation logic can be unit-tested with plain ``python3`` against
captured AMI byte streams — the wire-format details (event names, field casing,
DeviceState semantics) are exactly where this dashboard has historically broken.

The pure helpers (``parse_ami_blocks``, ``*_from_blocks``, ``is_registered``,
``login_failed``) take/return plain data and do no I/O; ``_ami_command`` and the
``get_*`` wrappers add the single-shot socket conversation on top.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import socket

AMI_HOST = "127.0.0.1"
AMI_PORT = 5038
AMI_USER = os.environ.get("AMI_USER", "switchboard")
AMI_SECRET = os.environ.get("AMI_SECRET", "")

# A process spawned by the dialplan — Asterisk `System()`, e.g. the operator's
# MWI auto-clear running `switchboard-mwi clear <ext>` — inherits Asterisk's
# environment, which does NOT carry the AMI secret (the s6 run scripts source
# /run/switchboard/ami.env for the long-running services, but a System() child of
# Asterisk doesn't). Fall back to that generated env file so any AMI consumer
# works regardless of how it was launched. (`AMI_ENV` overrides the path in tests.)
if not AMI_SECRET:
    try:
        with open(os.environ.get("AMI_ENV", "/run/switchboard/ami.env")) as _fh:
            _env = dict(
                ln.strip().split("=", 1)
                for ln in _fh
                if "=" in ln and not ln.strip().startswith("#")
            )
        AMI_USER = _env.get("AMI_USER", AMI_USER)
        AMI_SECRET = _env.get("AMI_SECRET", AMI_SECRET)
    except OSError:
        pass

# A PJSIP endpoint's DeviceState is Asterisk's own aggregate of contact
# reachability: with no bound/qualified contact it reads "Unavailable", and the
# moment a contact is reachable it becomes "Not in use" (then "In use",
# "Ringing", etc. on a call). That makes device state the most robust
# "is this phone registered" signal — far more reliable than re-deriving it from
# the per-contact ContactList parse.
OFFLINE_STATES = frozenset({"unavailable", "invalid", "unknown", ""})

# Real ContactList Status wire values (res_pjsip/pjsip_options.c status_map):
# Reachable / Unreachable / NonQualified / Unknown / Removed. A qualify-disabled
# AOR reports "NonQualified" for an otherwise-up contact.
_REGISTERED_CONTACT_STATUSES = frozenset({"reachable", "nonqualified", "created"})

# A room extension is 1-6 digits. Used to guard anything that interpolates an
# ext into an AMI Channel string, so no caller can smuggle in CRLF / a dial
# string even if it skipped its own validation.
_EXT_RE = re.compile(r"[0-9]{1,6}")


class AMIError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O) — unit-tested.
# --------------------------------------------------------------------------- #
def parse_ami_blocks(data: bytes) -> list[dict]:
    """Split a raw AMI byte stream into a list of key/value blocks.

    Keys are lower-cased so callers never depend on Asterisk's inconsistent AMI
    field casing (``ObjectName`` vs ``Aor`` vs ``Uri`` vs ``RoundtripUsec``).
    Reading ``"URI"`` when the wire said ``"Uri"`` is what made every contact key
    on ``""`` and every room read "Unregistered" even while fully registered.
    Values are left intact.
    """
    blocks: list[dict] = []
    for raw in data.decode(errors="replace").split("\r\n\r\n"):
        raw = raw.strip()
        if not raw:
            continue
        block: dict[str, str] = {}
        for line in raw.split("\r\n"):
            if ": " in line:
                k, v = line.split(": ", 1)
                block[k.strip().lower()] = v.strip()
        if block:
            blocks.append(block)
    return blocks


def stream_complete(data: bytes) -> bool:
    """True once an AMI list action's terminator is present.

    Each list action (PJSIPShow* / CoreShowChannels) ends with a real
    ``Event: <X>Complete`` line. We match that LINE, not a bare ``Complete``
    substring anywhere in the buffer: field values are attacker-influenced (an
    inbound trunk ``CallerIDName`` or a phone's ``UserAgent`` could contain
    "Complete") and a substring match would truncate the stream early.
    """
    for ln in data.decode(errors="replace").split("\r\n"):
        if ln[:7].lower() == "event: " and ln.lower().endswith("complete"):
            return True
    return False


def actions_complete(data: bytes, action_ids: set[str]) -> bool:
    """True once every action in ``action_ids`` has emitted its own
    ``...Complete`` event.

    Used to read several list actions over ONE connection: each action is tagged
    with an ActionID and Asterisk echoes it on that action's events (including the
    terminating ``EndpointListComplete`` / ``ContactListComplete`` /
    ``CoreShowChannelsComplete``). We key the terminator on the ActionID rather
    than the event name so attacker-influenced field values (a ``UserAgent`` or
    ``CallerIDName`` containing "complete") can never end the read early, and an
    unsolicited event for some *other* ActionID can't either.
    """
    seen: set[str] = set()
    for b in parse_ami_blocks(data):
        aid = b.get("actionid", "")
        if aid in action_ids and b.get("event", "").lower().endswith("complete"):
            seen.add(aid)
    return action_ids <= seen


def actions_responded(data: bytes, action_ids: set[str]) -> bool:
    """True once every action in ``action_ids`` has produced its response block.

    The sibling of :func:`actions_complete` for SINGLE-response actions (Getvar):
    those return one ``Response:`` block tagged with the ActionID and emit NO
    ``...Complete`` event, so we terminate the multiplexed read on responses
    instead. Used to batch all the per-channel codec Getvars over one login.
    """
    seen = {b.get("actionid", "") for b in parse_ami_blocks(data) if "response" in b}
    return action_ids <= seen


def login_failed(blocks: list[dict]) -> bool:
    """True if the parsed blocks contain an AMI authentication failure.

    A wrong/rotated secret would otherwise look identical to "all phones
    offline" (zero events, no error) — callers raise on this so the UI can show
    a real banner instead of a fleet of false "Offline" pills.
    """
    for b in blocks:
        if b.get("response", "").lower() == "error" and "authentication" in b.get("message", "").lower():
            return True
    return False


def endpoints_from_blocks(blocks: list[dict]) -> list[dict]:
    """Registration state per PJSIP endpoint (room) from EndpointList events."""
    endpoints: list[dict] = []
    for b in blocks:
        if (b.get("event") or "").lower() == "endpointlist":
            endpoints.append(
                {
                    "name": b.get("objectname", "?"),
                    "state": b.get("devicestate", "Unknown"),
                    "channels": b.get("activechannels", ""),
                }
            )
    return endpoints


def contacts_from_blocks(blocks: list[dict]) -> dict[str, dict]:
    """Contact/qualify status keyed by endpoint (aor) id from ContactList events."""
    out: dict[str, dict] = {}
    for b in blocks:
        if (b.get("event") or "").lower() != "contactlist":
            continue
        # Identity: a ContactList event carries the endpoint/AOR name in its
        # `Endpoint` field (== the room ext "11".."18" here) — it has NO `Aor`
        # field. Fall back to the ObjectName prefix, which has appeared as
        # "<aor>/sip:...", "<aor>@@<hash>", and "<aor>;@<hash>" across Asterisk
        # versions, so split on every observed separator.
        aor = b.get("endpoint") or b.get("aor") or ""
        if not aor:
            aor = b.get("objectname", "").split("@@")[0].split("/")[0].split(";")[0]
        if not aor:
            continue
        out[aor] = {
            "status": b.get("status", "Unknown"),
            "uri": b.get("uri", ""),
            "rtt": b.get("roundtripusec", ""),
        }
    return out


def channels_from_blocks(blocks: list[dict]) -> list[dict]:
    """Active channels (calls in progress) from CoreShowChannel events."""
    chans: list[dict] = []
    for b in blocks:
        if (b.get("event") or "").lower() == "coreshowchannel":
            channel = b.get("channel", "")
            chans.append(
                {
                    "channel": channel,
                    "ext": channel_ext(channel),
                    "state": b.get("channelstatedesc", ""),
                    "caller": b.get("calleridnum", ""),
                    "caller_name": b.get("calleridname", ""),
                    "connected": b.get("connectedlinenum", ""),
                    "connected_name": b.get("connectedlinename", ""),
                    "duration": b.get("duration", ""),
                    # Linkedid ties every leg of one call together; context tells us
                    # an operator/IVR leg from a room-to-room leg.
                    "linkedid": b.get("linkedid", ""),
                    "context": b.get("context", ""),
                    "exten": b.get("exten", ""),
                }
            )
    return chans


def channel_ext(channel: str) -> str:
    """Endpoint id from a PJSIP channel name: "PJSIP/11-0000000a" -> "11",
    "PJSIP/trunk-..." -> "trunk". Empty for anything unexpected."""
    if "/" not in channel:
        return ""
    # Drop a ";1"/";2" half-channel marker (Local channels) before the uniqueid.
    tail = channel.split("/", 1)[1].split(";", 1)[0]
    return tail.rsplit("-", 1)[0] if "-" in tail else tail


def _leg_label(ch: dict, rooms_by_ext: dict) -> str:
    """Human label for one call leg: a configured room's name, or "Outside"
    (with the external number when we have it)."""
    ext = ch.get("ext", "")
    if ext in rooms_by_ext:
        return rooms_by_ext[ext]
    # Trunk / unknown leg → an outside party. Show the number that is genuinely
    # external (NOT one of our room exts, which is what connected/caller may echo
    # for the *other* leg of the call).
    for cand in (ch.get("caller", ""), ch.get("connected", "")):
        if cand and cand not in rooms_by_ext:
            return f"Outside ({cand})"
    return "Outside"


def _dur_secs(d: str) -> int:
    """AMI Duration ("HH:MM:SS") -> seconds, for picking a call's longest leg."""
    try:
        secs = 0
        for p in str(d).split(":"):
            secs = secs * 60 + int(p)
        return secs
    except (ValueError, TypeError):
        return 0


def _group_calls(channels: list[dict]) -> list[list[dict]]:
    """Group channel legs into calls by Linkedid (falling back to the channel
    name), preserving first-seen order."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for ch in channels:
        key = ch.get("linkedid") or ch.get("channel") or ""
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ch)
    return [groups[k] for k in order]


def summarize_calls(channels: list[dict], rooms_by_ext: dict) -> dict:
    """Turn raw channel legs into (a) a readable list of calls and (b) a per-ext
    map of what each room is currently doing — so the UI can say "Kitchen ↔
    Office", "Garage ↔ Outside", or "Kitchen → Operator" instead of dumping
    channel names. Returns {"calls": [...], "by_ext": {ext: {state, peer}}}.
    """
    calls: list[dict] = []
    by_ext: dict[str, dict] = {}
    for legs in _group_calls(channels):
        operator = any(
            (ch.get("context") or "").lower() == "operator" or ch.get("exten") == "0"
            for ch in legs
        )
        outside = any(ch.get("ext") == "trunk" for ch in legs)

        labels: list[str] = []
        for ch in legs:
            lab = _leg_label(ch, rooms_by_ext)
            if lab not in labels:
                labels.append(lab)

        joined = " ".join((ch.get("state") or "") for ch in legs).lower()
        if "ring" in joined:
            state = "Ringing"
        elif "up" in joined:
            state = "Talking"
        else:
            state = (legs[0].get("state") or "") if legs else ""

        duration = max((ch.get("duration", "") for ch in legs), key=_dur_secs, default="")

        # Distinct codec(s) in use across the call's legs. One value means every
        # leg shares it — no transcoding (e.g. "ulaw" end-to-end); two different
        # values reveal a transcode (e.g. "g722/ulaw"). Empty until a leg reports.
        codec = "/".join(sorted({(ch.get("codec") or "").lower() for ch in legs if ch.get("codec")}))

        # Only list real connections/sessions as "active calls". A lone leg with
        # a single room party (a test ring's Playback, or the &lt;1s before a
        # callee leg is created) is reflected on the room card via by_ext below,
        # but isn't a call worth listing.
        if operator and len(labels) == 1:
            calls.append({"detail": f"{labels[0]} → Operator", "state": state,
                          "duration": duration, "kind": "operator", "codec": codec})
        elif len(labels) >= 2:
            kind = "outside" if outside else "internal"
            calls.append({"detail": " ↔ ".join(labels[:2]), "state": state,
                          "duration": duration, "kind": kind, "codec": codec})

        # Per-room view: each room leg's peer is the other party (or the operator).
        for ch in legs:
            ext = ch.get("ext", "")
            if ext not in rooms_by_ext:
                continue
            peers = [_leg_label(o, rooms_by_ext) for o in legs if o is not ch]
            peers = [p for p in peers if p != rooms_by_ext[ext]]
            if peers:
                peer = peers[0]
            elif operator:
                peer = "Operator"
            else:
                peer = ""
            leg_state = "Ringing" if "ring" in (ch.get("state") or "").lower() else (
                "Talking" if "up" in (ch.get("state") or "").lower() else (ch.get("state") or "")
            )
            by_ext[ext] = {"state": leg_state, "peer": peer, "codec": (ch.get("codec") or "").lower()}
    return {"calls": calls, "by_ext": by_ext}


def is_registered(device_state: str, contact_status: str = "") -> bool:
    """Whether a room phone is registered/online.

    Primary signal is DeviceState (online unless Unavailable/Invalid/Unknown);
    a reachable contact status is a secondary confirm for the rare qualify-off
    case where the contact row is the only evidence.
    """
    return (
        (device_state or "").strip().lower() not in OFFLINE_STATES
        or (contact_status or "").lower() in _REGISTERED_CONTACT_STATUSES
    )


# --------------------------------------------------------------------------- #
# Socket conversation.
# --------------------------------------------------------------------------- #
_action_seq = itertools.count(1)


def _next_action_id() -> str:
    # next() on an itertools.count is GIL-atomic, so this is safe across the
    # operator console's concurrent session threads without an explicit lock.
    return f"sb-{os.getpid()}-{next(_action_seq)}"


def _ami_command(
    action_lines: list[str],
    timeout: float = 4.0,
    single_response: bool = False,
    action_id: str = "",
) -> list[dict]:
    """Run one AMI action and return the list of event/response blocks.

    List actions (PJSIPShow* / CoreShowChannels) stream events terminated by a
    "...Complete" event — read until that. Single-response actions (Originate /
    Hangup) have no Complete event, so they tag the action with an ActionID and
    stop as soon as that action's own response block arrives (instead of waiting
    out the full socket timeout). Callers that need to attribute the response
    pass their own ``action_id``.
    """
    if not AMI_SECRET:
        raise AMIError("AMI secret not configured")

    if single_response:
        if not action_id:
            action_id = _next_action_id()
        action_lines = list(action_lines) + [f"ActionID: {action_id}"]

    with socket.create_connection((AMI_HOST, AMI_PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        buf = b""

        def send(lines: list[str]) -> None:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        try:
            buf += sock.recv(4096)
        except socket.timeout:
            pass

        send(["Action: Login", f"Username: {AMI_USER}", f"Secret: {AMI_SECRET}"])
        send(action_lines)

        data = bytearray(buf)
        while True:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            # Defense-in-depth: AMI is loopback-only and these result sets are
            # tiny, but never buffer without an upper bound.
            if len(data) > 1_000_000:
                break
            if single_response:
                # Stop once our action's own response (matched by ActionID) lands.
                if any(
                    b.get("actionid") == action_id and "response" in b
                    for b in parse_ami_blocks(bytes(data))
                ):
                    break
            # Stop at the list terminator, and only log off AFTER — logging off
            # before the stream finishes makes Asterisk close the socket and
            # truncate the events (the original "all Unregistered" bug).
            elif stream_complete(bytes(data)):
                break
        try:
            send(["Action: Logoff"])
        except OSError:
            pass

    blocks = parse_ami_blocks(bytes(data))
    if login_failed(blocks):
        # Generic message; the caller logs detail server-side.
        raise AMIError("authentication failed")
    return blocks


def _ami_actions(actions: list[list[str]], timeout: float = 2.5) -> list[dict]:
    """Run several LIST actions over a SINGLE AMI connection and return every
    parsed block.

    One login + one logoff for the whole batch, instead of a fresh
    connect/login/logoff per action. Each action is tagged with its own ActionID
    and we read until *all* of them have signalled their ``...Complete`` event
    (see :func:`actions_complete`). This is what collapses the dashboard/console
    status poll — endpoints + contacts + channels — from three AMI sessions down
    to one, which is the dominant source of the manager logon/logoff churn.

    Mirrors :func:`_ami_command`'s read discipline (read until the real
    terminator, then log off — never before, or Asterisk truncates the stream).
    Raises :class:`AMIError` on a missing secret or an auth failure; lets socket
    errors propagate so callers can fall back to an "AMI down" state.

    The default ``timeout`` is deliberately kept *below* the console's
    ``POLL_SECONDS`` cadence: the normal read completes in milliseconds on
    loopback, and on the (spec-says-can't-happen) case where Asterisk failed to
    echo an ActionID on the terminating event, a single stalled poll then falls
    through to the timeout WITHOUT outrunning the poll interval and backing the
    poller up. The parsed buffer is still correct in that case — the failure mode
    is a little latency, never wrong data.
    """
    if not AMI_SECRET:
        raise AMIError("AMI secret not configured")

    want: set[str] = set()
    tagged: list[list[str]] = []
    for act in actions:
        aid = _next_action_id()
        want.add(aid)
        tagged.append(list(act) + [f"ActionID: {aid}"])

    with socket.create_connection((AMI_HOST, AMI_PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)

        def send(lines: list[str]) -> None:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        buf = b""
        try:
            buf += sock.recv(4096)  # banner
        except socket.timeout:
            pass

        send(["Action: Login", f"Username: {AMI_USER}", f"Secret: {AMI_SECRET}"])
        for action_lines in tagged:
            send(action_lines)

        data = bytearray(buf)
        while True:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            # Loopback-only and tiny per action, but three lists stacked — keep a
            # higher-than-single but still bounded cap so we never buffer forever.
            if len(data) > 2_000_000:
                break
            if actions_complete(bytes(data), want):
                break
        try:
            send(["Action: Logoff"])
        except OSError:
            pass

    blocks = parse_ami_blocks(bytes(data))
    if login_failed(blocks):
        raise AMIError("authentication failed")
    return blocks


def ring_extension(ext: str, sound: str = "switchboard/sw-test", ring_seconds: int = 8) -> bool:
    """Place a short "test ring" to a room phone via AMI Originate.

    The caller MUST have validated ``ext`` against the configured rooms — this
    only ever rings a known endpoint and runs a fixed Playback (never Dial), so
    it cannot place an outside call even though the AMI account holds the
    originate privilege. Async: the phone rings for ``ring_seconds`` (≈ one ring
    cycle) and, if answered, hears the test prompt. Returns True if Asterisk
    accepted (queued) the originate.
    """
    if not _EXT_RE.fullmatch(ext or ""):
        return False
    action_id = _next_action_id()
    blocks = _ami_command(
        [
            "Action: Originate",
            f"Channel: PJSIP/{ext}",
            "Application: Playback",
            f"Data: {sound}",
            f"CallerID: Switchboard Test <{ext}>",
            f"Timeout: {int(ring_seconds * 1000)}",
            "Async: true",
        ],
        single_response=True,
        action_id=action_id,
    )
    # Success = THIS originate's own response block came back Success (scoped by
    # ActionID, not by matching brittle message wording).
    return any(
        b.get("actionid") == action_id and b.get("response", "").lower() == "success"
        for b in blocks
    )


def announce_to_ext(ext: str, sound: str, caller_num: str = "8000", timeout_s: int = 30) -> bool:
    """Originate an ANNOUNCEMENT to one ext: Asterisk calls the phone and, on
    answer, plays ``sound`` (a rendered TTS clip) straight out the handset.

    Paired with the phone configured to AUTO-ANSWER calls from ``caller_num``
    (Grandstream "Auto Answer Numbers"), so an alert speaks on the speaker with
    nobody lifting the handset — the SIP equivalent of a media_player announcement,
    which is what lets HA / the ecoflow-panel target the cordless like an ecobee.

    Like ring_extension this runs a fixed Playback (never Dial), so it can only
    play a LOCAL file to a KNOWN endpoint — never an outside call — even though the
    AMI account holds originate privilege. The caller MUST have validated ext; we
    additionally reject a sound/caller_num that could smuggle extra AMI headers
    (CR/LF) or a non-digit caller number. Returns True if Asterisk queued it."""
    if not _EXT_RE.fullmatch(ext or ""):
        return False
    if any(c in (sound or "") for c in "\r\n"):  # no AMI header injection via the path
        return False
    cnum = caller_num if (caller_num or "").isdigit() else "8000"
    action_id = _next_action_id()
    try:
        blocks = _ami_command(
            [
                "Action: Originate",
                f"Channel: PJSIP/{ext}",
                "Application: Playback",
                f"Data: {sound}",
                f"CallerID: Switchboard Announce <{cnum}>",
                f"Timeout: {int(timeout_s * 1000)}",
                "Async: true",
            ],
            single_response=True,
            action_id=action_id,
        )
    except (OSError, AMIError) as exc:
        logging.warning("announce_to_ext: originate to %s failed: %s", ext, exc)
        return False
    return any(
        b.get("actionid") == action_id and b.get("response", "").lower() == "success"
        for b in blocks
    )


def connect_extensions(a: str, b: str, allowed_exts, caller_id: str = "Operator <0>") -> bool:
    """Patch a call between two CONFIGURED room phones (operator "connect").

    Originates room ``a`` into the generated ``[rooms]`` dialplan at extension
    ``b``: ``a`` rings, and when it answers the dialplan dials ``b`` — the
    room-to-room path. Both exts MUST be in ``allowed_exts`` (the configured
    room set). This matters: when the SIP trunk is enabled, the ``[rooms]``
    context also holds the outbound ``_<prefix>.`` pattern, so a digits-only
    guard would let a value like "9911" reach the trunk. Restricting ``b`` to
    the actual room set means it can only match the room ``_X.`` pattern, never
    the trunk pattern. Returns True if Asterisk accepted the originate.
    """
    allowed = set(allowed_exts or ())
    if a not in allowed or b not in allowed:
        return False
    if not (_EXT_RE.fullmatch(a) and _EXT_RE.fullmatch(b)):
        return False
    action_id = _next_action_id()
    blocks = _ami_command(
        [
            "Action: Originate",
            f"Channel: PJSIP/{a}",
            "Context: rooms",
            f"Exten: {b}",
            "Priority: 1",
            f"CallerID: {caller_id}",
            "Timeout: 30000",
            "Async: true",
        ],
        single_response=True,
        action_id=action_id,
    )
    return any(
        blk.get("actionid") == action_id and blk.get("response", "").lower() == "success"
        for blk in blocks
    )


def originate_wakeup(room_ext: str, ring_seconds: int = 60) -> bool:
    """Ring a room and deliver its wake-up: originate the room into the fixed
    [wakeup-deliver] dialplan context. room_ext is digit-guarded; the context is
    constant (not caller-supplied), so this can only ring a known room."""
    if not _EXT_RE.fullmatch(room_ext or ""):
        return False
    action_id = _next_action_id()
    blocks = _ami_command(
        [
            "Action: Originate",
            f"Channel: PJSIP/{room_ext}",
            "Context: wakeup-deliver",
            "Exten: s",
            "Priority: 1",
            "CallerID: Wake-up <0>",
            f"Timeout: {int(ring_seconds * 1000)}",
            "Async: true",
        ],
        single_response=True,
        action_id=action_id,
    )
    return any(
        blk.get("actionid") == action_id and blk.get("response", "").lower() == "success"
        for blk in blocks
    )


def page_all(exts: list[str]) -> bool:
    """Page every room phone at once (intercom): originate each valid ext into
    the fixed ``[page]`` dialplan context, which auto-answers the FXS line and
    joins it to the page ConfBridge so the pager is heard on every handset.

    Each ext is digit-guarded with ``_EXT_RE`` before it is interpolated into a
    Channel string, so an invalid/CRLF-bearing ext is silently skipped (never
    smuggles extra AMI lines) — and the context is constant, so this can only
    ring known room endpoints, never place an outside call. Returns True if at
    least one originate was accepted (Success, scoped by its own ActionID);
    per-ext failures are logged but don't abort the rest. Empty / all-invalid
    input → False.
    """
    accepted = 0
    for ext in exts or ():
        if not _EXT_RE.fullmatch(ext or ""):
            continue
        action_id = _next_action_id()
        try:
            blocks = _ami_command(
                [
                    "Action: Originate",
                    f"Channel: PJSIP/{ext}",
                    "Context: page",
                    "Exten: s",
                    "Priority: 1",
                    "CallerID: Page <0>",
                    "Timeout: 30000",
                    "Async: true",
                ],
                single_response=True,
                action_id=action_id,
            )
        except (OSError, AMIError) as exc:
            logging.warning("page_all: originate to %s failed: %s", ext, exc)
            continue
        ok = any(
            blk.get("actionid") == action_id and blk.get("response", "").lower() == "success"
            for blk in blocks
        )
        if ok:
            accepted += 1
        else:
            logging.warning("page_all: originate to %s not accepted", ext)
    return accepted > 0


def set_mwi(ext: str, on: bool) -> bool:
    """Set or clear a room's message-waiting indicator in Asterisk.

    Sends an unsolicited ``message-summary`` NOTIFY straight to the room's
    registered contact via ``PJSIPNotify`` (res_pjsip_notify — part of the core
    PJSIP stack), using the ``switchboard-mwi-on`` / ``switchboard-mwi-off``
    templates generated into pjsip_notify.conf. The FXS gateway renders
    "Messages-Waiting: yes" as a stutter dial tone (and "no" clears it). We use
    PJSIPNotify rather than res_mwi_external's MWIUpdate because that module isn't
    built into the Alpine Asterisk package. ``ext`` is digit-guarded with
    ``_EXT_RE`` before it is interpolated into the Endpoint, so a CRLF-bearing
    value can't inject extra AMI lines. Returns True on Success.
    """
    if not _EXT_RE.fullmatch(ext or ""):
        return False
    action_id = _next_action_id()
    blocks = _ami_command(
        [
            "Action: PJSIPNotify",
            f"Endpoint: {ext}",
            f"Option: switchboard-mwi-{'on' if on else 'off'}",
        ],
        single_response=True,
        action_id=action_id,
    )
    ok = any(
        blk.get("actionid") == action_id and blk.get("response", "").lower() == "success"
        for blk in blocks
    )
    if not ok:
        # Surface WHY Asterisk rejected it — almost always "unknown command"
        # (res_mwi_external not loaded) or "permission denied" (manager class).
        msg = next(
            (blk.get("message", "") for blk in blocks
             if blk.get("actionid") == action_id and blk.get("response", "").lower() == "error"),
            "no matching response",
        )
        logging.warning("set_mwi %s on=%s rejected by AMI: %s", ext, on, msg)
    return ok


def hangup_channel(channel: str) -> bool:
    """Hang up one channel by its Asterisk channel name (operator "hang up").

    The channel string comes from CoreShowChannels (Asterisk-supplied), but we
    still reject CRLF defensively so it can't inject extra AMI lines.
    """
    if not channel or "\r" in channel or "\n" in channel:
        return False
    action_id = _next_action_id()
    blocks = _ami_command(
        ["Action: Hangup", f"Channel: {channel}"],
        single_response=True,
        action_id=action_id,
    )
    return any(
        blk.get("actionid") == action_id and blk.get("response", "").lower() == "success"
        for blk in blocks
    )


def transfer_channel(channel: str, target_ext: str, allowed_exts) -> bool:
    """Blind-transfer a live channel to a configured room via AMI Redirect.

    Moves ``channel`` into the generated ``[rooms]`` dialplan at ``target_ext`` — it
    leaves its current bridge and rings/dials that room. The operator GUI/TUI picks
    an active call and a destination room and redirects the FAR leg (see
    :func:`peer_channels_by_ext`), so the outside/other party ends up on the chosen
    room while the original handset drops — a one-touch operator blind transfer.

    ``target_ext`` MUST be in ``allowed_exts`` (the configured room set), exactly
    like :func:`connect_extensions` — so a redirect can only ever land on a known
    room's ``_X.`` pattern, never the trunk's outbound ``_<prefix>.`` pattern. The
    ``channel`` comes from CoreShowChannels but CRLF is rejected defensively. Uses
    the AMI ``Redirect`` action (the ``call`` privilege the account holds) — NOT the
    withheld ``command`` class. Returns True on Success.
    """
    allowed = set(allowed_exts or ())
    if target_ext not in allowed or not _EXT_RE.fullmatch(target_ext or ""):
        return False
    if not channel or "\r" in channel or "\n" in channel:
        return False
    action_id = _next_action_id()
    blocks = _ami_command(
        ["Action: Redirect", f"Channel: {channel}", "Context: rooms",
         f"Exten: {target_ext}", "Priority: 1"],
        single_response=True,
        action_id=action_id,
    )
    return any(
        blk.get("actionid") == action_id and blk.get("response", "").lower() == "success"
        for blk in blocks
    )


def peer_channels_by_ext(channels: list[dict], rooms_by_ext: dict | None = None) -> dict[str, str]:
    """``{room ext -> the OTHER party's channel name}`` for active calls, so the
    operator can blind-transfer the far party of a room's call elsewhere.

    Legs are grouped into a call by ``Linkedid``. Picking the *right* far leg
    matters once a call has more than two legs: an inbound trunk call that rings a
    group (cordless + iPhone) is ONE Linkedid carrying the trunk leg PLUS a ringing
    leg per room, so "the first other leg" (CoreShowChannels order is arbitrary)
    could be a sibling ringing handset instead of the outside caller. Among a
    room's peers we therefore prefer (1) the outside/trunk leg, then (2) an
    answered (``Up``) leg, then (3) the longest-running leg. A leg sharing the
    room's OWN ext (a transient duplicate mid-transfer) is skipped, so a room is
    never mapped to its own sibling. ``rooms_by_ext`` tells a room leg from an
    outside one; without it we still prefer the literal ``trunk`` leg. This mirrors
    :func:`summarize_calls`' peer selection, so the channel we redirect matches the
    "↔ Outside" / "↔ Office" label shown on the card.
    """
    rooms = set(rooms_by_ext or ())

    def _is_outside(o: dict) -> bool:
        oext = o.get("ext", "")
        return oext == "trunk" or (bool(rooms) and bool(oext) and oext not in rooms)

    def _rank(o: dict) -> tuple:
        return (
            0 if _is_outside(o) else 1,                          # outside party first
            0 if "up" in (o.get("state") or "").lower() else 1,  # then an answered leg
            -_dur_secs(o.get("duration", "")),                   # then the longest leg
        )

    by_link: dict[str, list[dict]] = {}
    for ch in channels:
        key = ch.get("linkedid") or ch.get("channel") or ""
        by_link.setdefault(key, []).append(ch)
    out: dict[str, str] = {}
    for ch in channels:
        ext = ch.get("ext", "")
        if not ext:
            continue
        key = ch.get("linkedid") or ch.get("channel") or ""
        peers = [
            o for o in by_link.get(key, [])
            if o.get("channel") and o.get("channel") != ch.get("channel")
            and o.get("ext") != ext  # never the room's own (sibling) leg
        ]
        if peers:
            out[ext] = min(peers, key=_rank)["channel"]
    return out


def get_endpoints() -> list[dict]:
    try:
        blocks = _ami_command(["Action: PJSIPShowEndpoints"])
    except (OSError, AMIError) as exc:
        raise AMIError(str(exc)) from exc
    return endpoints_from_blocks(blocks)


def get_contacts() -> dict[str, dict]:
    try:
        blocks = _ami_command(["Action: PJSIPShowContacts"])
    except (OSError, AMIError):
        return {}
    return contacts_from_blocks(blocks)


def get_channels() -> list[dict]:
    try:
        blocks = _ami_command(["Action: CoreShowChannels"])
    except (OSError, AMIError):
        return []
    return channels_from_blocks(blocks)


def get_status_bundle() -> tuple[list[dict], dict[str, dict], list[dict]]:
    """The whole dashboard/console status read — endpoints, contacts, channels —
    over ONE AMI connection. Returns ``(endpoints, contacts, channels)``.

    This is the hot path: the operator console polls it every few seconds and the
    web dashboard on every refresh. Doing it as one login/logoff instead of three
    (one per ``get_*``) is what keeps Asterisk's manager log from filling with
    logon/logoff churn. Raises :class:`AMIError` (or lets ``OSError`` propagate)
    on failure, exactly like :func:`get_endpoints`, so callers keep their existing
    ``ami_ok`` fallback — set the trio empty and mark AMI down on either.
    """
    blocks = _ami_actions(
        [
            ["Action: PJSIPShowEndpoints"],
            ["Action: PJSIPShowContacts"],
            ["Action: CoreShowChannels"],
        ]
    )
    return (
        endpoints_from_blocks(blocks),
        contacts_from_blocks(blocks),
        channels_from_blocks(blocks),
    )


def get_channel_codec(channel: str) -> str:
    """The audio codec a live channel is using (e.g. "ulaw", "g722"), read via
    Getvar of ``CHANNEL(audioreadformat)``. "" when unavailable (channel gone /
    AMI down).

    Lets the dashboard show — and lets us *verify* — that a call is G.711 µ-law
    with no transcoding: a leg reading "ulaw" bridged to a trunk leg also reading
    "ulaw" is un-transcoded; a mismatch (e.g. "g722" ↔ "ulaw") reveals a transcode.

    Uses only the ``call`` privilege the AMI account already holds — NOT the
    deliberately-withheld ``command`` (CLI/RCE) class. ``channel`` comes from
    CoreShowChannels (Asterisk-supplied); CRLF is rejected defensively so it can
    never inject extra AMI lines.
    """
    if not channel or "\r" in channel or "\n" in channel:
        return ""
    action_id = _next_action_id()
    try:
        blocks = _ami_command(
            ["Action: Getvar", f"Channel: {channel}", "Variable: CHANNEL(audioreadformat)"],
            single_response=True,
            action_id=action_id,
        )
    except (OSError, AMIError):
        return ""
    for b in blocks:
        if b.get("actionid") == action_id and "value" in b:
            return b.get("value", "")
    return ""


def codecs_for_channels(channels: list[dict]) -> dict[str, str]:
    """{channel name -> codec} for the active channels, read over a SINGLE AMI
    login — one Getvar CHANNEL(audioreadformat) per channel, multiplexed by
    ActionID — instead of a fresh connect/login/logoff per channel.

    Empty input -> empty output, so an idle poll does NO AMI work; and during a
    call this is ONE login total, not one per leg — so the codec read no longer
    multiplies the AMI sessions exactly when the PBX is busy (it kept undoing the
    v0.9.3 single-session churn win). Best-effort: returns what it got (or {}) on
    any error or auth failure — never raises into the status path.
    """
    items: list[tuple[str, str]] = []  # (action_id, channel)
    for ch in channels:
        name = ch.get("channel", "")
        if name and "\r" not in name and "\n" not in name:
            items.append((_next_action_id(), name))
    if not items or not AMI_SECRET:
        return {}
    by_id = {aid: name for aid, name in items}
    want = set(by_id)
    out: dict[str, str] = {}
    try:
        with socket.create_connection((AMI_HOST, AMI_PORT), timeout=2.5) as sock:
            sock.settimeout(2.5)

            def send(lines: list[str]) -> None:
                sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

            buf = b""
            try:
                buf += sock.recv(4096)
            except socket.timeout:
                pass
            send(["Action: Login", f"Username: {AMI_USER}", f"Secret: {AMI_SECRET}"])
            for aid, name in items:
                send(["Action: Getvar", f"Channel: {name}",
                      "Variable: CHANNEL(audioreadformat)", f"ActionID: {aid}"])
            data = bytearray(buf)
            while True:
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
                if len(data) > 1_000_000:
                    break
                if actions_responded(bytes(data), want):
                    break
            try:
                send(["Action: Logoff"])
            except OSError:
                pass
    except (OSError, AMIError):
        return {}
    blocks = parse_ami_blocks(bytes(data))
    if login_failed(blocks):
        return {}
    for b in blocks:
        aid = b.get("actionid", "")
        if aid in by_id and "value" in b:
            out[by_id[aid]] = b.get("value", "")
    return out
