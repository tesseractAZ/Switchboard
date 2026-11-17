"""Keep private detail out of the READABLE copy of the Asterisk log.

★ /share/switchboard/asterisk.log IS HOST-MOUNTED AND CAPTURED IN BACKUPS. The
private copy in /data keeps everything in full and is not readable from outside
the container; that is the whole reason the two copies differ. Four things must
not survive in the readable one, and nothing else is removed:

`VERBOSE` lines. v0.94.7 took the verbose class off this channel because it
carries the dialplan trace — who called whom, and every telephone number
dialled. That stopped new leakage and removed nothing already written: the
deployment this was found on still held 6,613 verbose lines, 58 of them carrying
a telephone number, two days after the fix. Closing a tap does not drain the
bucket.

The SIP ACCOUNT in a registration URI. A trunk registration retry writes the full
URI at WARNING, a class this channel still carries. Redacted rather than dropped:
that line is how you learn the trunk is flapping.

The TRUNK PROVIDER'S HOST (v0.119.3). Until then it was kept, on the reasoning
that it made the retry line diagnostic and identified nobody. The second half
was wrong: a provider's host is usually a regional point of presence, often
named for a city, so it can say roughly where the house is. On the deployment
this was found on, the readable copy named it 260 times, on 130 retry lines from
early September to mid-November, the account on each already masked. Two rules
take it out:

- every occurrence, on any line, of a host the options name —
  `trunk.provider_host` and `trunk.from_domain`, read from SWITCHBOARD_OPTIONS
  and /data/options.json, compared without regard to case, and matched only as a
  whole host (not inside `x<host>` or `<host>.example`; yes at the end of
  `_sip._udp.<host>`);
- on a res_pjsip_outbound_registration line, the host part of every sip: URI
  that is a DNS name, configured or not, so a host the options no longer name
  (trunk off, provider changed) goes too.

An address is never taken by these rules: a private one goes by the next rule,
a public one stays. What stays makes the line as useful as before: the port, the
masked account, the retry interval or the stop, the severity and the source,
which still say the trunk's registration failed and how.

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

★★ TWO WAYS TO WRITE, CHOSEN BY WHETHER ASTERISK CAN BE APPENDING (2026-09-14).
v0.100.6 used one byte-shifting rewrite for both callers and guarded it by
re-checking the file size once before seek/write/truncate. That re-check covered
only the gap between the read and the check: a line Asterisk appended after it
and before truncate() was cut off the readable copy, or overwritten when the
redacted body came out longer. Its changelog said the scrub could not lose a line
being written; the code did not support that.

- `writer_stopped=True` — the boot pass, which runs in the init oneshot before
  s6 starts Asterisk. Lines move: VERBOSE lines are dropped, and the account,
  the trunk host and addresses become the canonical `sip:***@`, `<trunk-host>`
  and `<private-ip>`. The size re-check stays as a tripwire for a broken
  precondition, and it is only a tripwire.
- The default — the per-poll pass, while Asterisk holds the file open for append.
  NOTHING MOVES. Every mask is exactly as many bytes as what it replaces and is
  written with os.pwrite at that offset; the file is never truncated and never
  written past the bytes this pass read. An O_APPEND writer lands at the end of
  the file whatever happens here, so no append can be cut or overwritten.

THE SAME-LENGTH FORMS, exactly (none of them can be matched again by the
patterns below, so a second pass leaves them alone):

- account: `sip:` then one `*` per character of the account, then `@`
  (`sip:123456_acct@host` -> `sip:***********@host`);
- trunk host: `<host`, then `*` to the original length, then `>`
  (`pop1.example.net` -> `<host**********>`); a host shorter than that frame,
  five characters or fewer, becomes one `*` per character;
- private address: `<ip`, then `*` to the original length, then `>`
  (`192.168.1.71` -> `<ip********>`, `10.0.0.5` -> `<ip****>`);
- VERBOSE line: the `[timestamp] ` prefix kept, every byte after it `*`.

A same-length mask keeps the LENGTH of what it hid: how many characters the
account had, or the host, or the address. Nothing else. The next boot pass
rewrites the account, host, address and VERBOSE forms into the canonical ones,
which carry no length, so that is at most one uptime. The all-`*` form of a host
of five characters or fewer is the exception: it cannot be told from other
asterisks, so it keeps that length.
"""

from __future__ import annotations

import functools
import ipaddress
import json
import os
import re
import stat
from typing import NamedTuple

# A SIP account identifier in a URI: `sip:<user>@<host>`, as written by
# res_pjsip_outbound_registration.c — "No response received from ... on
# registration attempt to 'sip:<account>@<provider>'". The user part goes here;
# the host part goes by the trunk-host rule below.
_SIP_ACCT_RE = re.compile(r"sip:[A-Za-z0-9_.+-]+@")
_SEVERITY_RE = re.compile(r"^\[[^\]]+\] ([A-Z]+)\[")

# The add-on options, for the trunk's configured hosts. SWITCHBOARD_OPTIONS is
# the post-overlay snapshot the s6 services are pointed at; the init oneshot has
# no such variable. Both files are read and their hosts joined, so a host an
# overlay replaced still goes from the lines written while it was in use.
OPTIONS_PATH = "/data/options.json"
HOST_TOKEN = "<trunk-host>"

# A configured host is used only if it is a DNS name: dotted labels, at least
# one letter. That leaves out an address (this rule does not touch addresses)
# and a one-label word, which could match inside the masks themselves.
_DNS_NAME_RE = re.compile(r"(?=[a-z0-9_.-]*[a-z])[a-z0-9_-]+(?:\.[a-z0-9_-]+)+")

# The trunk's registration lines, and the host part of a sip: URI on one —
# after an optional `user@`, the account already masked or not. A host here is
# taken whether or not it is still configured.
_REG_LINE = "res_pjsip_outbound_registration"
_URI_HOST_RE = re.compile(r"(sip:(?:[^\s@'\"<>]*@)?)([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)")
_LETTER_RE = re.compile(r"[A-Za-z]")

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
ACCT_TOKEN = "sip:***@"

# What the in-place pass leaves behind, recognised by the boot pass so it can
# turn each into its canonical, length-free form.
_MASKED_ACCT_RE = re.compile(r"sip:\*+@")
_MASKED_IP_RE = re.compile(r"<ip\*+>")
_MASKED_HOST_RE = re.compile(r"<host\**>")
_BLANKED_RE = re.compile(r"^\[[^\]]+\] \*+$")


def _is_private(text: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(text)
    except ValueError:          # 300.1.2.3 is not an address; leave it alone
        return False
    return any(ip in net for net in PRIVATE_NETS)


def _mask_ip(m: re.Match) -> str:
    return PRIVATE_IP_TOKEN if _is_private(m.group(0)) else m.group(0)


def _mask_ip_same_length(m: re.Match) -> str:
    s = m.group(0)
    return "<ip" + "*" * (len(s) - 4) + ">" if _is_private(s) else s


def _mask_acct_same_length(m: re.Match) -> str:
    return "sip:" + "*" * (len(m.group(0)) - 5) + "@"


def _host_same_length(host: str) -> str:
    n = len(host)
    return "<host" + "*" * (n - 6) + ">" if n >= 6 else "*" * n


def configured_hosts() -> tuple:
    """`trunk.provider_host` and `trunk.from_domain` from every options file that
    can be read, as written. Never raises: an unreadable or malformed file, or
    one with no trunk, adds nothing."""
    found = []
    for path in (os.environ.get("SWITCHBOARD_OPTIONS", OPTIONS_PATH), OPTIONS_PATH):
        try:
            with open(path, encoding="utf-8") as fh:
                trunk = json.load(fh).get("trunk")
            found += [trunk.get("provider_host"), trunk.get("from_domain")]
        except Exception:  # noqa: BLE001 — a scrub pass must never stop here
            continue
    return tuple(h for h in found if isinstance(h, str))


@functools.lru_cache(maxsize=8)
def _hosts_re(hosts: tuple):
    """One case-insensitive pattern for every configured host that is a DNS
    name, matched only as a whole host: not inside a longer label or name, but
    yes as the tail of one (`_sip._udp.<host>`). None if there is none.

    re.ASCII matters: Unicode case-folding would let the Kelvin sign match `k`,
    and a mask written over it would be shorter in bytes than what it hid."""
    names = set()
    for h in hosts:
        h = h.strip().lower().split(":", 1)[0]
        if _DNS_NAME_RE.fullmatch(h):
            names.add(h)
    if not names:
        return None
    # No order needed: the lookahead lets one alternative at most match at a
    # position, since one host extending another goes on with a label.
    alt = "|".join(re.escape(h) for h in sorted(names))
    return re.compile(rf"(?<![A-Za-z0-9_-])(?:{alt})(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])",
                      re.IGNORECASE | re.ASCII)


def _drop_hosts(line: str, hosts, mask) -> str:
    """Every configured host anywhere on the line, and on the trunk's
    registration lines every DNS name in a sip: URI's host part."""
    pat = _hosts_re(tuple(hosts))
    if pat is not None:
        line = pat.sub(lambda m: mask(m.group(0)), line)
    if _REG_LINE in line:
        line = _URI_HOST_RE.sub(
            lambda m: m.group(1) + (mask(m.group(2)) if _LETTER_RE.search(m.group(2))
                                    else m.group(2)), line)
    return line


def redact_line(line: str, hosts=()) -> str:
    """The account, the trunk host and any private address masked; everything
    else untouched. `hosts` are the configured trunk hosts (configured_hosts())."""
    line = _drop_hosts(_SIP_ACCT_RE.sub(ACCT_TOKEN, line), hosts, lambda h: HOST_TOKEN)
    return _IPV4_RE.sub(_mask_ip, line)


def mask_line(line: str, hosts=()) -> str:
    """redact_line(), with every mask exactly as long as what it replaces.

    Same patterns, same order, so the two cannot disagree about WHAT is private
    — only about how the hole is drawn. Every pattern matches ASCII only, and
    each mask is ASCII, so the encoded line keeps its byte length too."""
    line = _drop_hosts(_SIP_ACCT_RE.sub(_mask_acct_same_length, line), hosts,
                       _host_same_length)
    return _IPV4_RE.sub(_mask_ip_same_length, line)


class Scrub(NamedTuple):
    dropped: int = 0        # VERBOSE lines removed at boot, or blanked in place
    redacted: int = 0       # lines with an account or a private address masked
    clean_to: int = 0       # byte offset the file is known clean through; a line boundary
    deferred: str = ""      # "" once finished; otherwise why nothing was written


def _pread(fd: int, n: int, offset: int) -> bytes:
    parts = []
    while n > 0:
        b = os.pread(fd, min(n, 1 << 20), offset)
        if not b:
            break
        parts.append(b)
        offset += len(b)
        n -= len(b)
    return b"".join(parts)


def _pwrite(fd: int, data: bytes, offset: int) -> None:
    while data:
        n = os.pwrite(fd, data, offset)
        data, offset = data[n:], offset + n


def _changed_runs(old: bytes, new: bytes):
    """(offset, bytes) for each run where `new` differs from `old`, same length."""
    i, n = 0, len(old)
    while i < n:
        if old[i] == new[i]:
            i += 1
            continue
        j = i
        while j < n and old[j] != new[j]:
            j += 1
        yield i, new[i:j]
        i = j


def _lines(whole: bytes):
    """Each complete line of `whole` as (byte offset, raw bytes, decoded text)."""
    offset = 0
    for raw in whole.split(b"\n")[:-1]:
        yield offset, raw, raw.decode("utf-8", "surrogateescape")
        offset += len(raw) + 1


def _rewrite(fd: int, since: int, size: int, whole: bytes, partial: bytes,
             hosts: tuple) -> Scrub:
    """The boot pass: lines move, so nothing may be appending."""
    dropped = redacted = 0
    out = []
    for _, _raw, line in _lines(whole):
        m = _SEVERITY_RE.match(line)
        if (m and m.group(1) == "VERBOSE") or _BLANKED_RE.match(line):
            dropped += 1
            continue
        new = _MASKED_HOST_RE.sub(HOST_TOKEN, _MASKED_IP_RE.sub(
            PRIVATE_IP_TOKEN, _MASKED_ACCT_RE.sub(ACCT_TOKEN, redact_line(line, hosts))))
        if new != line:
            redacted += 1
        out.append(new + "\n")
    if not (dropped or redacted):
        return Scrub(0, 0, since + len(whole), "")
    # A tripwire, not a guard: an append after this check and before the
    # truncate below is still lost. That is why this path runs only while the
    # writer is stopped.
    if os.fstat(fd).st_size != size:
        return Scrub(dropped, redacted, since, "grew")
    body = "".join(out).encode("utf-8", "surrogateescape")
    _pwrite(fd, body + partial, since)
    os.ftruncate(fd, since + len(body) + len(partial))
    return Scrub(dropped, redacted, since + len(body), "")


def _mask_in_place(fd: int, since: int, whole: bytes, hosts: tuple) -> Scrub:
    """The per-poll pass: every byte stays where it is."""
    blanked = masked = 0
    writes = []
    for offset, raw, line in _lines(whole):
        m = _SEVERITY_RE.match(line)
        if m and m.group(1) == "VERBOSE":
            keep = len(line[:m.start(1)].encode("utf-8", "surrogateescape"))
            new = raw[:keep] + b"*" * (len(raw) - keep)
            blanked += 1
        else:
            new = mask_line(line, hosts).encode("utf-8", "surrogateescape")
            if new == raw:
                continue
            masked += 1
        writes.extend((since + offset + i, run) for i, run in _changed_runs(raw, new))
    for at, run in writes:
        _pwrite(fd, run, at)
    return Scrub(blanked, masked, since + len(whole), "")


def scrub(path, since: int = 0, *, writer_stopped: bool = False) -> Scrub:
    """Scrub `path` from byte `since` to its last complete line.

    Pass the previous result's `clean_to` as `since` and only what Asterisk has
    appended in between is read. A file now shorter than `since` was trimmed or
    replaced, so it is scanned from the start.

    ★ ASTERISK HOLDS THIS FILE OPEN FOR APPEND WHILE IT RUNS. So:

    - Every write is IN PLACE. A write-and-rename would leave Asterisk appending
      to an unlinked inode nobody can read.
    - Unless the caller says the writer is stopped, the pass only overwrites
      bytes it has already read, with the same number of bytes, and never
      truncates. See the module docstring for the exact masks.
    - A line still being written — no newline yet — is never judged. Its bytes
      are carried through untouched and it is scanned once complete; judging a
      fragment could miss an account split across two reads.

    ★ ROOT OPENS THIS IN A DIRECTORY THE `asterisk` USER CAN WRITE (2026-09-14).
    The file is opened with O_NOFOLLOW, and anything that is not a regular file
    is refused, so a symlink planted in /share/switchboard cannot turn this pass
    into a root write to whatever it points at. O_NONBLOCK so a FIFO planted
    under the same name cannot hang the open.

    Bytes are decoded with surrogateescape, so anything that is not UTF-8 is
    written back exactly as it was read.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return Scrub()          # nothing written yet is nothing leaked
    except OSError as exc:
        return Scrub(0, 0, since, f"error: {exc}")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return Scrub(0, 0, since, "error: not a regular file")
        size = st.st_size
        if since > size:
            since = 0
        chunk = _pread(fd, size - since, since)
        end = chunk.rfind(b"\n") + 1
        whole, partial = chunk[:end], chunk[end:]
        hosts = configured_hosts()
        if writer_stopped:
            return _rewrite(fd, since, size, whole, partial, hosts)
        return _mask_in_place(fd, since, whole, hosts)
    except OSError as exc:
        return Scrub(0, 0, since, f"error: {exc}")
    finally:
        os.close(fd)
