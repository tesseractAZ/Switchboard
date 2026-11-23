"""Login gate for the console web terminal — pure helpers, no sockets.

The web terminal fronts the operator console (ring / connect / hang up every
phone), so LAN exposure needs a credential. The design mirrors the household's
other TUI add-ons: a `console_users` options list of {username, password} rows
(password masked in the Configuration UI), a login page, an HttpOnly session
cookie, and a per-source-IP throttle. An EMPTY users list disables the gate in
server.py, but since v0.125.0 the console-web run script counts users with
parse_users first and does not start the server at all when it finds none (it
idles with a NOTICE). The SSH console reads the same rows and refuses every
login while there are none.

Everything stateful or judgemental lives here so it unit-tests without a
server; server.py only wires sockets to these helpers.

Security notes, hard-won elsewhere and applied from the start:
  * The throttle is charged BEFORE credential verification, so a rejected
    attempt can never be retried for free (charging after the check lets an
    attacker race the window).
  * Credential comparison checks every configured row with hmac.compare_digest
    on BOTH fields and no early exit — response time does not reveal whether a
    username exists.
  * Session tokens are 256-bit secrets.token_urlsafe values with an absolute
    TTL; nothing about them is derived from the credentials.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import threading
import time
from urllib.parse import parse_qs

COOKIE_NAME = "sbconsole"
SESSION_TTL = 12 * 3600     # absolute session lifetime (seconds)
MAX_ATTEMPTS = 5            # login attempts allowed per source IP per window
MAX_NET_ATTEMPTS = 30       # per IPv6 /64 per window (see throttle_keys)
WINDOW_SECONDS = 300        # rolling throttle window
MAX_FORM_BYTES = 4096       # a login form is tiny; anything bigger is abuse
_MAX_TRACKED_IPS = 1024     # throttle memory bound
_MAX_TRACKED_NETS = 256     # /64 counters, kept apart (see LoginThrottle)
_MAX_SESSIONS = 64          # session-store bound (a home has a handful of browsers)


def parse_users(raw: str) -> list:
    """CONSOLE_WEB_USERS env (compact JSON from switchboard-opt) → [(user, pw)].

    Malformed input yields [], and both readers fail closed on it: the web
    terminal's run script does not start the terminal, and the SSH console
    refuses every login. A row counts only when both fields are text or a whole
    number (a hand-written overlay may say 1234): null, true/false, a list or an
    object is dropped, never turned into a password such as "None"."""
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        return []

    def _text(v) -> str:
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            return ""
        return str(v)

    users = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("username", "")).strip()
        password = _text(row.get("password", ""))
        if name and password:
            users.append((name, password))
    return users


def check_credentials(users: list, username: str, password: str) -> bool:
    """Constant-shape check: every row is compared on both fields, no early
    exit, so timing does not reveal which usernames exist."""
    ok = False
    for name, pw in users:
        name_ok = hmac.compare_digest(name.encode(), str(username or "").encode())
        pw_ok = hmac.compare_digest(pw.encode(), str(password or "").encode())
        if name_ok and pw_ok:
            ok = True
    return ok


def parse_login_form(body: bytes) -> tuple:
    """POST /login body (application/x-www-form-urlencoded) → (username, password)."""
    if not body or len(body) > MAX_FORM_BYTES:
        return "", ""
    try:
        fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    except ValueError:
        return "", ""
    return (fields.get("username", [""])[0], fields.get("password", [""])[0])


def parse_cookies(header: str) -> dict:
    """A Cookie: header value → {name: value}. Tolerates the usual sloppiness."""
    out = {}
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


def throttle_keys(ip: str) -> tuple:
    """(address key, network key) a login attempt is counted under.

    The address key is the address itself; an IPv4-mapped IPv6 address (what
    a dual-stack ``::`` listener reports for an IPv4 client) counts as its IPv4
    address. The network key is the /64 of any other IPv6 address, and None for
    IPv4 or a value that is not an address (``"?"``).

    The /64 gets its own, higher ceiling (MAX_NET_ATTEMPTS), never the
    per-address limit: on a home LAN the router advertises ONE /64 and every
    IPv6 device on it takes its addresses from it, so a per-/64 limit of 5
    would let one device with a stale password lock all the others out. One
    host rotating through fresh addresses in that /64 still meets the ceiling
    (v0.128.0, when the web terminal first became reachable over IPv6)."""
    try:
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return ip, None
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped), None
        return str(addr), str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr), None


class LoginThrottle:
    """Rolling attempt limiter: MAX_ATTEMPTS per source address per window,
    and for IPv6 also MAX_NET_ATTEMPTS per /64 (see throttle_keys). charge()
    records the attempt and says whether it may proceed — call it BEFORE
    verifying credentials; call clear() after a successful sign-in."""

    def __init__(self, max_attempts: int = MAX_ATTEMPTS,
                 window: float = WINDOW_SECONDS, now=time.monotonic,
                 max_net_attempts: int = MAX_NET_ATTEMPTS) -> None:
        self._max = max_attempts
        self._net_max = max_net_attempts
        self._window = window
        self._now = now
        self._hits = {}      # address -> [monotonic, ...]
        # The /64 counts live apart from the address counts, with their own
        # bound: in one table, an attacker spraying fresh addresses evicted its
        # own /64 entry after ~1024 of them and the ceiling started again
        # (v0.128.0 review). A home LAN has a handful of /64s, never hundreds.
        self._net_hits = {}  # IPv6 /64 -> [monotonic, ...]
        # Every request runs on its own ThreadingTCPServer thread, and charge()
        # is a read-modify-write: without this lock two concurrent attempts can
        # both read the same pre-limit list and both write back, losing a count
        # (i.e. more attempts than the limit allows).
        self._lock = threading.Lock()

    def _store(self, table: dict, key: str, hits: list, bound: int) -> None:
        """Write ``hits`` as ``key``'s newest entry, then trim to ``bound``.
        Re-inserting moves the key to the end, so the trim drops the entry
        used longest ago, never one still being charged. Dropping a key only
        ever FORGIVES attempts, never blocks a fresh user."""
        table.pop(key, None)
        table[key] = hits
        while len(table) > bound:
            table.pop(next(iter(table)))

    def charge(self, ip: str) -> bool:
        key, net = throttle_keys(ip)
        t = self._now()
        with self._lock:
            hits = [x for x in self._hits.get(key, []) if t - x < self._window]
            allowed = len(hits) < self._max
            hits.append(t)
            self._store(self._hits, key, hits, _MAX_TRACKED_IPS)
            # Only an attempt its own address was allowed counts toward the /64:
            # an address already refused (a neighbour retrying a stale password)
            # must not spend the budget of every other device on the LAN.
            if net is not None and allowed:
                net_hits = [x for x in self._net_hits.get(net, []) if t - x < self._window]
                allowed = len(net_hits) < self._net_max
                net_hits.append(t)
                self._store(self._net_hits, net, net_hits, _MAX_TRACKED_NETS)
        return allowed

    def clear(self, ip: str) -> None:
        """A successful sign-in stops penalizing its own address, and takes back
        the one /64 attempt it spent, so devices signing in correctly never use
        up the LAN's budget. The rest of the /64 count stays: one device signing
        in does not reset a rotating neighbour's count."""
        key, net = throttle_keys(ip)
        with self._lock:
            self._hits.pop(key, None)
            if net is not None and self._net_hits.get(net):
                self._net_hits[net].pop()


class Sessions:
    """In-memory bearer sessions: token -> expiry. Restarting the service logs
    everyone out, which is exactly right for an operator console."""

    def __init__(self, ttl: float = SESSION_TTL, now=time.monotonic) -> None:
        self._ttl = ttl
        self._now = now
        self._tok = {}  # token -> expiry (monotonic)
        self._lock = threading.Lock()  # shared across request threads

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            if len(self._tok) >= _MAX_SESSIONS:
                self._prune_locked()
                while len(self._tok) >= _MAX_SESSIONS:
                    self._tok.pop(next(iter(self._tok)))  # evict oldest-issued
            self._tok[token] = self._now() + self._ttl
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            expiry = self._tok.get(token)
            if expiry is None:
                return False
            if self._now() > expiry:
                self._tok.pop(token, None)
                return False
        return True

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tok.pop(token, None)

    def _prune_locked(self) -> None:
        """Caller holds self._lock."""
        t = self._now()
        for token in [k for k, exp in self._tok.items() if t > exp]:
            self._tok.pop(token, None)


def session_cookie(token: str) -> str:
    """Set-Cookie value for a fresh session. HttpOnly (no script access),
    SameSite=Strict (no cross-site sends), Path=/ . No `Secure` flag: the
    terminal is plain HTTP on the LAN by design."""
    return (f"{COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/; "
            f"Max-Age={SESSION_TTL}")


def clear_cookie() -> str:
    return f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"


LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Switchboard Console</title><style>
  body{margin:0;background:#0f1216;color:#c6ccd6;font:16px -apple-system,system-ui,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh}
  form{background:#12151b;border-radius:10px;padding:34px 38px;box-shadow:0 8px 30px rgba(0,0,0,.4);width:290px}
  h1{font-size:19px;margin:0 0 6px;color:#e8ebf0}
  p.sub{margin:0 0 22px;color:#7d828c;font-size:13px}
  label{display:block;font-size:12px;color:#7d828c;margin:14px 0 4px;text-transform:uppercase;letter-spacing:.06em}
  input{width:100%%;box-sizing:border-box;background:#0f1216;border:1px solid #2a2f38;border-radius:6px;
        color:#e8ebf0;padding:10px 12px;font-size:15px}
  input:focus{outline:none;border-color:#4ec9a0}
  button{margin-top:22px;width:100%%;background:#4ec9a0;color:#0f1216;border:0;border-radius:6px;
         padding:11px;font-size:15px;font-weight:700;cursor:pointer}
  .err{background:#2a1518;border:1px solid #e06c75;color:#e06c75;border-radius:6px;
       padding:8px 12px;font-size:13px;margin-bottom:6px}
</style></head><body>
<form method="post" action="/login" autocomplete="off">
  <h1>&#9742; Switchboard</h1>
  <p class="sub">Operator console sign-in</p>
  %(error)s
  <label for="u">Username</label>
  <input id="u" name="username" autofocus autocapitalize="none" autocorrect="off">
  <label for="p">Password</label>
  <input id="p" name="password" type="password">
  <button type="submit">Sign in</button>
</form></body></html>"""


def login_page(error: str = "") -> str:
    block = f'<div class="err">{error}</div>' if error else ""
    return LOGIN_PAGE % {"error": block}
