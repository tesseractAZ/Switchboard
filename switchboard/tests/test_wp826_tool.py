"""tools/wp826.mjs against a local stand-in for the WP826 cordless (v0.125.0).

    python3 -m pytest switchboard/tests/test_wp826_tool.py

The tool pins the handset's self-signed certificate (WP826_CERT_SHA256). Before
v0.125.0 it checked the pin on one throwaway connection and then sent the login,
password included, on fresh connections nobody checked; and it read its password
file at start, so `fingerprint` (the documented way to get the pin, which sends
no credentials) failed without one. A local HTTPS server stands in for the
handset, and can switch certificates after its first connection. Skipped
without node or openssl.
"""
import json
import os
import shutil
import socketserver
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "wp826.mjs"
pytestmark = pytest.mark.skipif(not (shutil.which("node") and shutil.which("openssl")),
                                reason="needs node and openssl")


def _cert(d: Path, name: str):
    key, crt = d / f"{name}.key", d / f"{name}.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", f"/CN={name}", "-keyout", str(key), "-out", str(crt)],
                   check=True, capture_output=True)
    der = subprocess.run(["openssl", "x509", "-in", str(crt), "-outform", "DER"],
                         check=True, capture_output=True).stdout
    import hashlib
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    return ctx, hashlib.sha256(der).hexdigest()


class _Handset(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode()
        self.server.seen.append((self.path.split("?")[0], body))
        if self.path.startswith("/cgi-bin/access"):
            self._reply({"body": "SALT"})
        elif self.path.startswith("/cgi-bin/dologin"):
            self._reply({"response": "success", "body": {"sid": "sid123456789"}})
        else:
            self._reply({})

    def do_GET(self):
        self.server.seen.append((self.path.split("?")[0], ""))
        self._reply({"configs": [{"pvalue": "1", "value": "stand-in"}]})


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, contexts):
        super().__init__(("127.0.0.1", 0), _Handset)
        self.contexts, self.conns, self.seen = contexts, 0, []

    def get_request(self):
        sock, addr = self.socket.accept()
        ctx = self.contexts[min(self.conns, len(self.contexts) - 1)]
        self.conns += 1
        return ctx.wrap_socket(sock, server_side=True), addr


@pytest.fixture
def handset():
    d = Path(tempfile.mkdtemp(prefix="wp826-"))
    a, fp_a = _cert(d, "handset")
    b, fp_b = _cert(d, "impostor")
    servers = []

    def start(*contexts):
        srv = _Server(list(contexts))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv
    try:
        yield d, start, (a, fp_a), (b, fp_b)
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
        shutil.rmtree(d, ignore_errors=True)


def _tool(port, *args, pin=None, pass_file=None, timeout=30):
    env = dict(os.environ, WP826_HOST="127.0.0.1", WP826_PORT=str(port),
               WP826_PASS_FILE=str(pass_file) if pass_file else "/nonexistent/wp-pass")
    env.pop("WP826_CERT_SHA256", None)
    if pin:
        env["WP826_CERT_SHA256"] = pin
    return subprocess.run(["node", str(TOOL), *args], capture_output=True, text=True,
                          env=env, timeout=timeout)


def test_fingerprint_needs_no_password_file(handset):
    d, start, (a, fp_a), _ = handset
    srv = start(a)
    r = _tool(srv.server_address[1], "fingerprint")
    assert r.returncode == 0, r.stdout + r.stderr
    assert fp_a in r.stdout.replace(":", "").lower(), r.stdout
    assert srv.seen == [], "fingerprint sent an HTTP request"


def test_a_correct_pin_logs_in_and_reads(handset):
    d, start, (a, fp_a), _ = handset
    pw = d / "pass"
    pw.write_text("not-a-real-password\n")
    srv = start(a)
    r = _tool(srv.server_address[1], "get", "P1", pin=fp_a, pass_file=pw)
    assert r.returncode == 0 and "stand-in" in r.stdout, r.stdout + r.stderr
    assert [p for p, _ in srv.seen][:2] == ["/cgi-bin/access", "/cgi-bin/dologin"], srv.seen
    # One checked connection per request, plus verifyPin's up-front check. An
    # agent opening its own, unchecked connection (what `agent: false` does)
    # shows up here as extra connections.
    assert srv.conns == len(srv.seen) + 1, (srv.conns, srv.seen)


def test_a_wrong_pin_sends_nothing(handset):
    d, start, (a, fp_a), (_, fp_b) = handset
    pw = d / "pass"
    pw.write_text("not-a-real-password\n")
    srv = start(a)
    r = _tool(srv.server_address[1], "get", "P1", pin=fp_b, pass_file=pw)
    assert r.returncode != 0, r.stdout
    assert srv.seen == [], f"a request reached a peer with the wrong certificate: {srv.seen}"


def test_a_certificate_swapped_after_the_check_gets_no_password(handset):
    """The gap the old one-time check left: the handset's certificate on the
    first connection, another one on every connection after it."""
    d, start, (a, fp_a), (b, _) = handset
    pw = d / "pass"
    pw.write_text("not-a-real-password\n")
    srv = start(a, b)
    r = _tool(srv.server_address[1], "get", "P1", pin=fp_a, pass_file=pw)
    assert r.returncode != 0, r.stdout
    assert not any(p == "/cgi-bin/dologin" for p, _ in srv.seen), srv.seen
    assert "mismatch" in (r.stdout + r.stderr), r.stdout + r.stderr
