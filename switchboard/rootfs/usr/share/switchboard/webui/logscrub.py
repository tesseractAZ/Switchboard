"""Keep private detail out of the READABLE copy of the Asterisk log.

★ /share/switchboard/asterisk.log IS HOST-MOUNTED AND CAPTURED IN BACKUPS. The
private copy in /data keeps everything in full and is not readable from outside
the container; that is the whole reason the two copies differ. Three things must
not survive in the readable one, and nothing else is removed:

`VERBOSE` lines. v0.94.7 took the verbose class off this channel because it
carries the dialplan trace — who called whom, and every telephone number
dialled. That stopped new leakage and removed nothing already written: the
deployment this was found on still held 6,613 verbose lines, 58 of them carrying
a telephone number, two days after the fix. Closing a tap does not drain the
bucket.

The SIP ACCOUNT in a registration URI. A trunk registration retry writes the full
URI at WARNING, a class this channel still carries. Redacted rather than dropped
— that line is how you learn the trunk is flapping — and the provider host is
kept, because it is what makes the line diagnostic and it identifies nobody.

PRIVATE (RFC 1918) ADDRESSES. A failed qualify is logged at ERROR with the
phone's contact URI, so the house's LAN addressing was being published one
failed poll at a time. The port survives, and the endpoint name is already on
the line, so the line still says which phone.

★ ONE DEFINITION, TWO CALLERS, because a boot-only scrub has a window as long as
the uptime. v0.100.4 ran this from switchboard-config at start and nowhere else;
on the deployment it shipped to, a trunk registration retry wrote the account
nine and a half hours after the boot that had scrubbed the file clean, and it was
still there when the file was next read. The boot call removes what an earlier
build left behind; the link-health poller calls this every cycle for what
Asterisk writes while it runs.
"""

from __future__ import annotations

import ipaddress
import os
import re
from typing import NamedTuple

# A SIP account identifier in a URI: `sip:<user>@<host>`, as written by
# res_pjsip_outbound_registration.c — "No response received from ... on
# registration attempt to 'sip:<account>@<provider>'". Only the user part goes.
_SIP_ACCT_RE = re.compile(r"sip:[A-Za-z0-9_.+-]+@")
_SEVERITY_RE = re.compile(r"^\[[^\]]+\] ([A-Z]+)\[")

# A dotted quad that is not part of a longer run: not preceded by a digit or by
# `<digit>.`, not followed by a digit or by `.<digit>`. So a version string or an
# OID is never read as an address, while an address that ends a sentence — "from
# 10.1.2.3." — still is.
_IPV4_RE = re.compile(r"(?<!\d)(?<!\d\.)\d{1,3}(?:\.\d{1,3}){3}(?!\.?\d)")

# Named explicitly rather than `ipaddress.is_private`, which also covers the
# documentation ranges, loopback and link-local, and whose table changed between
# Python releases — so what got redacted would depend on the image's interpreter.
PRIVATE_NETS = tuple(ipaddress.IPv4Network(n) for n in
                     ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
PRIVATE_IP_TOKEN = "<private-ip>"


def _mask_ip(m: re.Match) -> str:
    try:
        ip = ipaddress.IPv4Address(m.group(0))
    except ValueError:          # 300.1.2.3 is not an address; leave it alone
        return m.group(0)
    return PRIVATE_IP_TOKEN if any(ip in net for net in PRIVATE_NETS) else m.group(0)


def redact_line(line: str) -> str:
    """The account and any private address masked; everything else untouched."""
    return _IPV4_RE.sub(_mask_ip, _SIP_ACCT_RE.sub("sip:***@", line))


class Scrub(NamedTuple):
    dropped: int = 0        # VERBOSE lines removed
    redacted: int = 0       # lines with an account or a private address masked
    clean_to: int = 0       # byte offset the file is known clean through; a line boundary
    deferred: str = ""      # "" once finished; otherwise why nothing was written


def scrub(path, since: int = 0) -> Scrub:
    """Scrub `path` from byte `since` to its last complete line.

    Pass the previous result's `clean_to` as `since` and only what Asterisk has
    appended in between is read. A file now shorter than `since` was trimmed or
    replaced, so it is scanned from the start.

    ★ ASTERISK HOLDS THIS FILE OPEN FOR APPEND WHILE THIS RUNS. So:

    - The rewrite is IN PLACE. A write-and-rename would leave Asterisk appending
      to an unlinked inode nobody can read; an O_APPEND writer lands at the new
      end of a file truncated under it, so in place is safe.
    - The write is REFUSED if the file grew between the read and the write. The
      lines appended in that gap would be truncated away. The result says
      `deferred="grew"` with `clean_to` unchanged, so the next call rescans the
      same bytes; it is never reported as clean.
    - A line still being written — no newline yet — is never judged. Its bytes
      are carried through untouched and it is scanned once complete; judging a
      fragment could miss an account split across two reads.

    Bytes are decoded with surrogateescape, so anything that is not UTF-8 is
    written back exactly as it was read.
    """
    try:
        with open(path, "r+b") as fh:
            size = os.fstat(fh.fileno()).st_size
            if since > size:
                since = 0
            fh.seek(since)
            chunk = fh.read(size - since)
            end = chunk.rfind(b"\n") + 1
            whole, partial = chunk[:end], chunk[end:]
            dropped = redacted = 0
            out = []
            for line in whole.decode("utf-8", "surrogateescape").split("\n")[:-1]:
                m = _SEVERITY_RE.match(line)
                if m and m.group(1) == "VERBOSE":
                    dropped += 1
                    continue
                new = redact_line(line)
                if new != line:
                    redacted += 1
                out.append(new + "\n")
            if not (dropped or redacted):
                return Scrub(0, 0, since + end, "")
            if os.fstat(fh.fileno()).st_size != size:
                return Scrub(dropped, redacted, since, "grew")
            body = "".join(out).encode("utf-8", "surrogateescape")
            fh.seek(since)
            fh.write(body + partial)
            fh.truncate()
            return Scrub(dropped, redacted, since + len(body), "")
    except FileNotFoundError:
        return Scrub()          # nothing written yet is nothing leaked
    except OSError as exc:
        return Scrub(0, 0, since, f"error: {exc}")
