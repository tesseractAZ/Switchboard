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
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# The AMI client lives in a framework-free sibling module so its wire-format
# parsing can be unit-tested without FastAPI. Ensure this directory is importable
# regardless of how uvicorn is launched.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ami import (  # noqa: E402
    AMIError,
    get_channels,
    get_contacts,
    get_endpoints,
    is_registered,
    ring_extension,
    summarize_calls,
)

OPTIONS_PATH = Path("/data/options.json")

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

    # Turn raw channel legs into readable calls ("Kitchen ↔ Office") and a
    # per-room "what is this phone doing right now" map.
    rooms_by_ext = {ext: (cfg.get("name") or ext) for ext, cfg in rooms_cfg.items()}
    summary = summarize_calls(channels, rooms_by_ext)
    by_ext = summary["by_ext"]

    # Registration is derived from DeviceState (the signal Asterisk already
    # aggregates from contact reachability); the contact row is enrichment
    # (status text + RTT) only. See ami.is_registered.
    rooms = []
    seen = set()
    for ep in endpoints:
        name = ep["name"]
        if name == "trunk":
            continue
        seen.add(name)
        cfg = rooms_cfg.get(name, {})
        contact = contacts.get(name, {})
        device_state = ep["state"]
        c_status = contact.get("status", "")
        registered = is_registered(device_state, c_status)
        call = by_ext.get(name, {})
        rooms.append(
            {
                "ext": name,
                "label": cfg.get("name", name),
                "device_state": device_state,
                "registered": registered,
                "contact_status": c_status or ("Reachable" if registered else "Unregistered"),
                "rtt": contact.get("rtt", ""),
                # What this phone is doing now (empty when idle): "Ringing" /
                # "Talking" and the other party ("Office", "Outside", "Operator").
                "call_state": call.get("state", ""),
                "call_peer": call.get("peer", ""),
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
                    "call_state": "",
                    "call_peer": "",
                }
            )
    rooms.sort(key=lambda r: r["ext"])

    return JSONResponse(
        {
            "ami_ok": ami_ok,
            "error": error,
            "rooms": rooms,
            "calls": summary["calls"],
            "trunk": {
                "enabled": bool(trunk.get("enabled")),
                "provider": trunk.get("provider_host", ""),
            },
        }
    )


@app.post("/api/ring/{ext}")
def api_ring(ext: str) -> JSONResponse:
    """Place a one-cycle test ring to a room phone.

    The ext is validated against the configured rooms before anything is sent to
    Asterisk, so this can only ring a known endpoint (never originate an outside
    call). Reached only via Ingress (the Supervisor-only middleware applies).
    """
    opts = load_options()
    known = {str(r.get("ext")) for r in (opts.get("rooms") or [])}
    if ext not in known:
        return JSONResponse({"ok": False, "error": "unknown extension"}, status_code=404)
    try:
        ok = ring_extension(ext)
    except (AMIError, OSError) as exc:
        print(f"[switchboard-webui] ring {ext} failed: {exc}", flush=True)
        return JSONResponse({"ok": False, "error": "unreachable"}, status_code=502)
    return JSONResponse({"ok": ok})


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
          box-shadow: 0 1px 3px rgba(0,0,0,.08); display: flex; flex-direction: column; }
  .card .ext { font-size: .75rem; color: #999; }
  .card .name { font-weight: 600; font-size: 1rem; margin: .1rem 0 .4rem; }
  .pill { display: inline-block; font-size: .72rem; padding: .15rem .5rem;
          border-radius: 999px; font-weight: 600; align-self: flex-start; }
  .up { background: #e3f7e8; color: #1a7f37; }
  .down { background: #fde7e7; color: #b42318; }
  .busy { background: #fff4e0; color: #b25e00; }
  .conn { font-size: .75rem; color: #b25e00; margin-top: .4rem; }
  .ringbtn { margin-top: .6rem; width: 100%; font-size: .75rem; font-weight: 600;
             padding: .35rem .5rem; border-radius: 8px; border: 1px solid var(--bd, #d4d7dd);
             background: var(--btn, #f3f4f6); color: inherit; cursor: pointer; }
  .ringbtn:hover:not(:disabled) { background: var(--btnh, #e8eaed); }
  .ringbtn:disabled { opacity: .5; cursor: default; }
  section { margin-top: 1.5rem; }
  .muted { color: #999; font-size: .85rem; }
  .calllist { list-style: none; padding: 0; margin: 0; }
  .calllist li { display: flex; justify-content: space-between; gap: .6rem; align-items: baseline;
                 padding: .5rem .2rem; border-bottom: 1px solid var(--bd, #eee); font-size: .95rem; }
  .calllist .detail { font-weight: 600; }
  .calllist .meta { color: #888; font-size: .8rem; white-space: nowrap; }
  .kind-outside .detail::before { content: "📞 "; }
  .kind-operator .detail::before { content: "🎧 "; }
  .kind-internal .detail::before { content: "🏠 "; }
  .banner { background: #fde7e7; color: #b42318; padding: .6rem .8rem;
            border-radius: 8px; margin-bottom: 1rem; font-size: .85rem; }
  @media (prefers-color-scheme: dark) {
    body { --bg:#111418; color:#e6e6e6; }
    .card { --card:#1b1f24; }
    :root { --bd:#2a2f36; --btn:#262b31; --btnh:#2f353c; }
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
    <div id="calls"><div class="muted">—</div></div>
  </section>

<script>
// Escape any server-supplied value before it touches innerHTML. Room labels and
// AMI caller-ID (attacker-controlled on inbound trunk calls) are untrusted.
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtDur(d) { return String(d || '').replace(/^00:/, ''); }

// Keep a "Ringing…" button disabled across the 4s auto-refreshes for ~one ring
// cycle, so clicking Test ring gives durable feedback.
const ringingUntil = {};

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

    const now = Date.now();
    const grid = document.getElementById('rooms');
    grid.innerHTML = data.rooms.map(r => {
      // "Not in use" means registered-and-idle (green) — only an active call
      // state ("In use", "Ringing", "Busy", "On Hold") is busy (orange).
      const ds = (r.device_state||'').toLowerCase();
      const active = (ds.includes('use') && ds !== 'not in use') ||
                     ds.includes('ring') || ds === 'busy' || ds === 'on hold';
      let cls, txt;
      if (active) { cls = 'busy'; txt = r.call_state || r.device_state; }
      else if (r.registered) { cls = 'up'; txt = 'Registered'; }
      else { cls = 'down'; txt = 'Offline'; }
      // Who this phone is talking to / ringing, when known.
      const conn = r.call_peer ? '<div class="conn">↔ ' + esc(r.call_peer) + '</div>' : '';
      const ringing = (ringingUntil[r.ext] || 0) > now;
      const dis = (!r.registered || ringing) ? ' disabled' : '';
      const label = ringing ? 'Ringing…' : '🔔 Test ring';
      const btn = '<button class="ringbtn" data-ext="' + esc(r.ext) + '"' + dis + '>' + label + '</button>';
      return '<div class="card"><div class="ext">ext ' + esc(r.ext) + '</div>' +
             '<div class="name">' + esc(r.label) + '</div>' +
             '<span class="pill ' + cls + '">' + esc(txt) + '</span>' +
             conn + btn + '</div>';
    }).join('');

    const callsEl = document.getElementById('calls');
    if (!data.calls || !data.calls.length) {
      callsEl.innerHTML = '<div class="muted">No active calls</div>';
    } else {
      callsEl.innerHTML = '<ul class="calllist">' + data.calls.map(c =>
        '<li class="kind-' + esc(c.kind || 'internal') + '">' +
        '<span class="detail">' + esc(c.detail) + '</span>' +
        '<span class="meta">' + esc(c.state || '') +
        (c.duration ? ' · ' + esc(fmtDur(c.duration)) : '') + '</span></li>'
      ).join('') + '</ul>';
    }
  } catch (e) {
    document.getElementById('sub').textContent = 'Status unavailable';
  }
}

// Test-ring: delegated click handler survives the innerHTML rebuilds.
document.getElementById('rooms').addEventListener('click', async (e) => {
  const btn = e.target.closest('.ringbtn');
  if (!btn || btn.disabled) return;
  const ext = btn.getAttribute('data-ext');
  btn.disabled = true; btn.textContent = 'Ringing…';
  ringingUntil[ext] = Date.now() + 9000;
  try {
    const res = await fetch('./api/ring/' + encodeURIComponent(ext), {method: 'POST'});
    const j = await res.json();
    if (!j.ok) throw new Error(j.error || 'failed');
  } catch (err) {
    ringingUntil[ext] = 0;
    btn.textContent = 'Failed';
    setTimeout(() => { btn.disabled = false; btn.textContent = '🔔 Test ring'; }, 1800);
  }
});

refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""
