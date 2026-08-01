"""Unit tests for the console web terminal's login gate (webauth.py).

    python3 -m pytest switchboard/tests/test_webauth.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "rootfs" / "usr" / "share" / "switchboard" / "console-web"))
import webauth  # noqa: E402


# ── users parsing ────────────────────────────────────────────────────────────

def test_parse_users_happy() -> None:
    users = webauth.parse_users('[{"username":"eric","password":"pw1"},'
                                '{"username":"guest","password":"pw2"}]')
    assert users == [("eric", "pw1"), ("guest", "pw2")]


def test_parse_users_bad_inputs_disable_gate() -> None:
    # Malformed JSON, non-list roots, incomplete rows: all yield [] (gate off),
    # matching the add-on's fail-open-to-previous-behavior convention.
    assert webauth.parse_users("") == []
    assert webauth.parse_users("not json") == []
    assert webauth.parse_users('{"username":"x"}') == []
    assert webauth.parse_users('[{"username":"","password":"p"}]') == []
    assert webauth.parse_users('[{"username":"u","password":""}]') == []
    assert webauth.parse_users('[42, null, "x"]') == []


def test_parse_users_strips_username_not_password() -> None:
    users = webauth.parse_users('[{"username":"  eric  ","password":" p w "}]')
    assert users == [("eric", " p w ")]  # passwords keep their exact bytes


# ── credential check ─────────────────────────────────────────────────────────

def test_check_credentials() -> None:
    users = [("eric", "secret"), ("guest", "other")]
    assert webauth.check_credentials(users, "eric", "secret")
    assert webauth.check_credentials(users, "guest", "other")
    assert not webauth.check_credentials(users, "eric", "other")   # crossed pair
    assert not webauth.check_credentials(users, "eric", "")
    assert not webauth.check_credentials(users, "", "secret")
    assert not webauth.check_credentials([], "eric", "secret")     # gate off ≠ match
    assert not webauth.check_credentials(users, None, None)


# ── login form / cookies ─────────────────────────────────────────────────────

def test_parse_login_form() -> None:
    assert webauth.parse_login_form(b"username=eric&password=p%26w") == ("eric", "p&w")
    assert webauth.parse_login_form(b"") == ("", "")
    assert webauth.parse_login_form(b"password=only") == ("", "only")
    # Over the cap: rejected outright, not truncated (truncation could split an
    # escape and admit a mangled password).
    assert webauth.parse_login_form(b"a" * (webauth.MAX_FORM_BYTES + 1)) == ("", "")


def test_parse_cookies() -> None:
    got = webauth.parse_cookies("a=1; sbconsole=tok; b=")
    assert got["sbconsole"] == "tok" and got["a"] == "1" and got["b"] == ""
    assert webauth.parse_cookies("") == {}


def test_cookie_attributes() -> None:
    c = webauth.session_cookie("T0K")
    # HttpOnly (no script theft), SameSite=Strict (no cross-site sends), and the
    # session TTL as Max-Age. No Secure flag: plain-HTTP LAN service by design.
    assert c.startswith(f"{webauth.COOKIE_NAME}=T0K")
    for attr in ("HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={webauth.SESSION_TTL}"):
        assert attr in c
    assert "Max-Age=0" in webauth.clear_cookie()


# ── throttle ─────────────────────────────────────────────────────────────────

def test_throttle_charges_before_verify_and_recovers() -> None:
    t = [0.0]
    thr = webauth.LoginThrottle(max_attempts=3, window=100, now=lambda: t[0])
    assert thr.charge("ip1") and thr.charge("ip1") and thr.charge("ip1")
    assert not thr.charge("ip1")            # 4th inside the window: denied
    assert thr.charge("ip2")                # other sources unaffected
    t[0] = 101.0
    assert thr.charge("ip1")                # window rolled: allowed again


def test_throttle_denied_attempts_still_count() -> None:
    # A denied attempt is ALSO charged — hammering while locked out must keep
    # counting against the caller, not run down a free retry meter.
    t = [0.0]
    thr = webauth.LoginThrottle(max_attempts=2, window=100, now=lambda: t[0])
    thr.charge("ip"); thr.charge("ip")
    t[0] = 90.0
    assert not thr.charge("ip")             # denied — and charged at t=90
    t[0] = 101.0                            # the two t=0 attempts aged out;
    assert thr.charge("ip")                 # one live hit (the denial) → allowed
    t[0] = 102.0                            # now t=90 + t=101 are both live —
    assert not thr.charge("ip")             # the DENIED attempt fills the quota


def test_throttle_clear_on_success() -> None:
    thr = webauth.LoginThrottle(max_attempts=1, window=100, now=lambda: 0.0)
    thr.charge("ip")
    thr.clear("ip")
    assert thr.charge("ip")                 # forgiven after a successful login


# ── sessions ─────────────────────────────────────────────────────────────────

def test_sessions_issue_validate_expire() -> None:
    t = [0.0]
    s = webauth.Sessions(ttl=100, now=lambda: t[0])
    tok = s.issue()
    assert s.valid(tok)
    assert not s.valid("forged")
    assert not s.valid("")
    t[0] = 101.0
    assert not s.valid(tok)                 # absolute TTL, no refresh


def test_sessions_revoke() -> None:
    s = webauth.Sessions(ttl=100, now=lambda: 0.0)
    tok = s.issue()
    s.revoke(tok)
    assert not s.valid(tok)


def test_sessions_tokens_unique_and_opaque() -> None:
    s = webauth.Sessions()
    toks = {s.issue() for _ in range(32)}
    assert len(toks) == 32
    assert all(len(t) >= 40 for t in toks)  # 32 random bytes urlsafe-encoded


# ── login page ───────────────────────────────────────────────────────────────

def test_login_page_renders_with_and_without_error() -> None:
    page = webauth.login_page()
    assert 'name="username"' in page and 'name="password"' in page
    assert 'class="err"' not in page
    assert 'Wrong username' in webauth.login_page("Wrong username or password.")
