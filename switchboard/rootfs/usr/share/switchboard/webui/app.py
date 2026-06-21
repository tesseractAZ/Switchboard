"""Switchboard — Ingress management UI.

A deliberately small FastAPI app that shows live PBX state inside Home
Assistant's sidebar:

* which room phones are registered (PJSIP contacts / qualify status),
* active calls right now,
* the configured rooms and trunk, straight from the add-on options.

It talks to Asterisk over the Manager Interface (AMI) on 127.0.0.1:5038 using a
tiny stdlib client (no extra dependencies). All links are relative so the app
works unmodified behind Home Assistant Ingress, whatever path it is mounted on.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

OPTIONS_PATH = Path("/data/options.json")
AMI_HOST = "127.0.0.1"
AMI_PORT = 5038
AMI_USER = os.environ.get("AMI_USER", "switchboard")
AMI_SECRET = os.environ.get("AMI_SECRET", "")

app = FastAPI(title="Switchboard", docs_url=None, redoc_url=None)

# Home Assistant Ingress proxies every request from the Supervisor's fixed
# internal IP (172.30.32.2). Because the add-on uses host_network, the port is
# also reachable directly on the LAN — which would bypass Ingress auth. Per the
# add-on docs ("Only connections from 172.30.32.2 must be allowed"), reject any
# client that isn't the Supervisor (loopback kept for local health checks). This
# closes the bypass without changing the bind, so Ingress keeps working.
INGRESS_CLIENT = "172.30.32.2"
_ALLOWED_CLIENTS = frozenset({INGRESS_CLIENT, "127.0.0.1", "::1"})


def _client_allowed(host: str) -> bool:
    return host in _ALLOWED_CLIENTS


@app.middleware("http")
async def restrict_to_ingress(request: Request, call_next):
    client = request.client.host if request.client else ""
    if not _client_allowed(client):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)


# --------------------------------------------------------------------------- #
# Minimal AMI client (synchronous, single request/response).
# --------------------------------------------------------------------------- #
class AMIError(Exception):
    pass


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
        send(["Action: Logoff"])

        data = bytearray(buf)
        while True:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if b"Goodbye" in data or b"Logoff" in chunk:
                break

    blocks: list[dict] = []
    for raw in data.decode(errors="replace").split("\r\n\r\n"):
        raw = raw.strip()
        if not raw:
            continue
        block: dict[str, str] = {}
        for line in raw.split("\r\n"):
            if ": " in line:
                k, v = line.split(": ", 1)
                block[k.strip()] = v.strip()
        if block:
            blocks.append(block)
    return blocks


def get_endpoints() -> list[dict]:
    """Registration state per PJSIP endpoint (room)."""
    try:
        blocks = _ami_command(["Action: PJSIPShowEndpoints"])
    except (OSError, AMIError) as exc:
        raise AMIError(str(exc)) from exc

    endpoints: list[dict] = []
    for b in blocks:
        if b.get("Event") == "EndpointList":
            endpoints.append(
                {
                    "name": b.get("ObjectName", "?"),
                    "state": b.get("DeviceState", "Unknown"),
                    "channels": b.get("ActiveChannels", ""),
                }
            )
    return endpoints


def get_contacts() -> dict[str, dict]:
    """Contact/qualify status keyed by endpoint (aor) id."""
    try:
        blocks = _ami_command(["Action: PJSIPShowContacts"])
    except (OSError, AMIError):
        return {}
    out: dict[str, dict] = {}
    for b in blocks:
        if b.get("Event") == "ContactList":
            aor = b.get("AOR", "")
            out[aor] = {
                "status": b.get("Status", "Unknown"),
                "uri": b.get("URI", ""),
                "rtt": b.get("RoundtripUsec", ""),
            }
    return out


def get_channels() -> list[dict]:
    """Currently active channels (calls in progress)."""
    try:
        blocks = _ami_command(["Action: CoreShowChannels"])
    except (OSError, AMIError):
        return []
    chans: list[dict] = []
    for b in blocks:
        if b.get("Event") == "CoreShowChannel":
            chans.append(
                {
                    "channel": b.get("Channel", ""),
                    "state": b.get("ChannelStateDesc", ""),
                    "caller": b.get("CallerIDNum", ""),
                    "connected": b.get("ConnectedLineNum", ""),
                    "duration": b.get("Duration", ""),
                }
            )
    return chans


def load_options() -> dict:
    try:
        with OPTIONS_PATH.open() as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/api/status")
def api_status() -> JSONResponse:
    opts = load_options()
    rooms_cfg = {str(r.get("ext")): r for r in (opts.get("rooms") or [])}
    trunk = opts.get("trunk") or {}

    ami_ok = True
    error = None
    try:
        endpoints = get_endpoints()
    except AMIError as exc:
        ami_ok = False
        # Return a generic marker to the client; log the detail server-side only.
        error = "unreachable"
        print(f"[switchboard-webui] AMI unavailable: {exc}", flush=True)
        endpoints = []

    contacts = get_contacts() if ami_ok else {}
    channels = get_channels() if ami_ok else []

    rooms = []
    seen = set()
    for ep in endpoints:
        name = ep["name"]
        if name == "trunk":
            continue
        seen.add(name)
        cfg = rooms_cfg.get(name, {})
        contact = contacts.get(name, {})
        rooms.append(
            {
                "ext": name,
                "label": cfg.get("name", name),
                "device_state": ep["state"],
                "registered": contact.get("status", "").lower() in ("reachable", "created", "non_qualified"),
                "contact_status": contact.get("status", "Unregistered"),
                "rtt": contact.get("rtt", ""),
            }
        )
    for ext, cfg in rooms_cfg.items():
        if ext not in seen:
            rooms.append(
                {
                    "ext": ext,
                    "label": cfg.get("name", ext),
                    "device_state": "Unavailable",
                    "registered": False,
                    "contact_status": "Unregistered",
                    "rtt": "",
                }
            )
    rooms.sort(key=lambda r: r["ext"])

    return JSONResponse(
        {
            "ami_ok": ami_ok,
            "error": error,
            "rooms": rooms,
            "channels": channels,
            "trunk": {
                "enabled": bool(trunk.get("enabled")),
                "provider": trunk.get("provider_host", ""),
            },
        }
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Switchboard</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, Segoe UI, Roboto, sans-serif;
         margin: 0; padding: 1.25rem; background: var(--bg, #f6f7f9); }
  h1 { font-size: 1.3rem; margin: 0 0 .25rem; }
  .sub { color: #888; font-size: .85rem; margin-bottom: 1rem; }
  .grid { display: grid; gap: .6rem;
          grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }
  .card { background: var(--card, #fff); border-radius: 12px; padding: .8rem .9rem;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .card .ext { font-size: .75rem; color: #999; }
  .card .name { font-weight: 600; font-size: 1rem; margin: .1rem 0 .4rem; }
  .pill { display: inline-block; font-size: .72rem; padding: .15rem .5rem;
          border-radius: 999px; font-weight: 600; }
  .up { background: #e3f7e8; color: #1a7f37; }
  .down { background: #fde7e7; color: #b42318; }
  .busy { background: #fff4e0; color: #b25e00; }
  section { margin-top: 1.5rem; }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #eee; }
  .muted { color: #999; }
  .banner { background: #fde7e7; color: #b42318; padding: .6rem .8rem;
            border-radius: 8px; margin-bottom: 1rem; font-size: .85rem; }
  @media (prefers-color-scheme: dark) {
    body { --bg:#111418; color:#e6e6e6; }
    .card { --card:#1b1f24; }
    th,td { border-color:#2a2f36; }
  }
</style>
</head>
<body>
  <h1>🔌 Switchboard</h1>
  <div class="sub" id="sub">Loading…</div>
  <div id="banner"></div>

  <div class="grid" id="rooms"></div>

  <section>
    <h2 style="font-size:1rem;">Active calls</h2>
    <table id="calls"><tbody><tr><td class="muted">—</td></tr></tbody></table>
  </section>

<script>
// Escape any server-supplied value before it touches innerHTML. Room labels and
// AMI caller-ID (attacker-controlled on inbound trunk calls) are untrusted.
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function refresh() {
  try {
    const res = await fetch('./api/status', {cache: 'no-store'});
    const data = await res.json();

    const banner = document.getElementById('banner');
    banner.innerHTML = data.ami_ok ? '' :
      '<div class="banner">Cannot reach Asterisk Manager: ' +
      esc(data.error || 'unknown') + '. The PBX may still be starting.</div>';

    const reg = data.rooms.filter(r => r.registered).length;
    document.getElementById('sub').textContent =
      reg + ' of ' + data.rooms.length + ' phones registered' +
      (data.trunk.enabled ? ' · trunk: ' + (data.trunk.provider || 'on') : ' · no trunk');

    const grid = document.getElementById('rooms');
    grid.innerHTML = data.rooms.map(r => {
      let cls = r.registered ? 'up' : 'down';
      let txt = r.registered ? 'Registered' : 'Offline';
      if ((r.device_state||'').toLowerCase().includes('use') ||
          (r.device_state||'').toLowerCase().includes('ring')) {
        cls = 'busy'; txt = r.device_state;
      }
      return '<div class="card"><div class="ext">ext ' + esc(r.ext) + '</div>' +
             '<div class="name">' + esc(r.label) + '</div>' +
             '<span class="pill ' + cls + '">' + esc(txt) + '</span></div>';
    }).join('');

    const calls = document.querySelector('#calls tbody');
    if (!data.channels.length) {
      calls.innerHTML = '<tr><td class="muted">No active calls</td></tr>';
    } else {
      calls.innerHTML =
        '<tr><th>Channel</th><th>State</th><th>From</th><th>To</th><th>Dur</th></tr>' +
        data.channels.map(c =>
          '<tr><td>' + esc(c.channel) + '</td><td>' + esc(c.state) + '</td><td>' +
          esc(c.caller) + '</td><td>' + esc(c.connected) + '</td><td>' + esc(c.duration) +
          '</td></tr>').join('');
    }
  } catch (e) {
    document.getElementById('sub').textContent = 'Status unavailable';
  }
}
refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""
