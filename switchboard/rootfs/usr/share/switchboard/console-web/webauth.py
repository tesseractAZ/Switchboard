"""Login gate for the console web terminal — pure helpers, no sockets.

The web terminal fronts the operator console (ring / connect / hang up every
phone), so LAN exposure needs a credential. The design mirrors the household's
other TUI add-ons: a `console_users` options list of {username, password} rows
(password masked in the Configuration UI), a login page, an HttpOnly session
cookie, and a per-source-IP throttle. An EMPTY users list disables the gate —
backward-compatible with every existing install (the run script's boot NOTICE
says which mode is live).

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
import json
import secrets
import threading
import time
from urllib.parse import parse_qs

COOKIE_NAME = "sbconsole"
SESSION_TTL = 12 * 3600     # absolute session lifetime (seconds)
MAX_ATTEMPTS = 5            # login attempts allowed per source IP per window
WINDOW_SECONDS = 300        # rolling throttle window
MAX_FORM_BYTES = 4096       # a login form is tiny; anything bigger is abuse
_MAX_TRACKED_IPS = 1024     # throttle memory bound
_MAX_SESSIONS = 64          # session-store bound (a home has a handful of browsers)


def parse_users(raw: str) -> list:
    """CONSOLE_WEB_USERS env (compact JSON from switchboard-opt) → [(user, pw)].

    Malformed input yields [] — which DISABLES the login gate, matching the
    add-on-wide fail-open-to-previous-behavior convention for bad options; the
    run script logs which mode is active so a misconfiguration is visible."""
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        return []
    users = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("username", "")).strip()
        password = str(row.get("password", ""))
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


class LoginThrottle:
    """Rolling per-source-IP attempt limiter. charge() records the attempt and
    says whether it may proceed — call it BEFORE verifying credentials."""

    def __init__(self, max_attempts: int = MAX_ATTEMPTS,
                 window: float = WINDOW_SECONDS, now=time.monotonic) -> None:
        self._max = max_attempts
        self._window = window
        self._now = now
        self._hits = {}  # ip -> [monotonic, ...]
        # Every request runs on its own ThreadingTCPServer thread, and charge()
        # is a read-modify-write: without this lock two concurrent attempts can
        # both read the same pre-limit list and both write back, losing a count
        # (i.e. more attempts than the limit allows).
        self._lock = threading.Lock()

    def charge(self, ip: str) -> bool:
        t = self._now()
        with self._lock:
            hits = [x for x in self._hits.get(ip, []) if t - x < self._window]
            allowed = len(hits) < self._max
            hits.append(t)
            self._hits[ip] = hits
            if len(self._hits) > _MAX_TRACKED_IPS:
                # Bound memory under an address-spraying flood; dropping the
                # oldest tracked IP only ever FORGIVES attempts, never blocks a
                # fresh user.
                self._hits.pop(next(iter(self._hits)))
        return allowed

    def clear(self, ip: str) -> None:
        """A successful login stops penalizing its source."""
        with self._lock:
            self._hits.pop(ip, None)


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
