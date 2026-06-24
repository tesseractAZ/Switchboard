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

import os
import socket

AMI_HOST = "127.0.0.1"
AMI_PORT = 5038
AMI_USER = os.environ.get("AMI_USER", "switchboard")
AMI_SECRET = os.environ.get("AMI_SECRET", "")

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
            chans.append(
                {
                    "channel": b.get("channel", ""),
                    "state": b.get("channelstatedesc", ""),
                    "caller": b.get("calleridnum", ""),
                    "connected": b.get("connectedlinenum", ""),
                    "duration": b.get("duration", ""),
                }
            )
    return chans


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
def _ami_command(action_lines: list[str], timeout: float = 4.0) -> list[dict]:
    """Run one AMI action and return the list of event/response blocks."""
    if not AMI_SECRET:
        raise AMIError("AMI secret not configured")

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
            # Stop at the list terminator, and only log off AFTER — logging off
            # before the stream finishes makes Asterisk close the socket and
            # truncate the events (the original "all Unregistered" bug).
            if stream_complete(bytes(data)):
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
