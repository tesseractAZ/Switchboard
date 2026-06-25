# Switchboard

A self-hosted Asterisk phone system for the analog phones in your home, packaged
as a Home Assistant add-on. It turns each FXS port on your **Grandstream
GXW4216 V2** into a room extension, lets every phone call every other phone, and
is ready to grow an outside line via a SIP trunk.

---

## 1. Quick start

1. **Install** the add-on (you already did if you're reading this).
2. Open the **Configuration** tab and define one entry under `rooms` per phone,
   each with a unique extension number, a friendly name, and a **secret**
   (password the Grandstream port uses to register). Example below.
3. **Start** the add-on. Watch the **Log** tab; you should see
   `switchboard-config done: N room(s)` then Asterisk starting.
4. Provision the GXW4216 V2 (section 4) so each FXS port registers.
5. Open the **Web UI** (Ingress, sidebar) — each room flips to **Registered**
   as its port comes online.
6. Pick up a phone and dial another room's extension. Done.

---

## 2. Options reference

```yaml
log_level: info          # trace|debug|info|notice|warning|error|critical
rtp_start: 10000         # first UDP port for RTP media
rtp_end: 10200           # last UDP port for RTP media (~2 per concurrent call)
codecs:                  # preference order offered to endpoints (see §8)
  - ulaw                 #   G.711 µ-law — best for analog, no transcode
  - alaw                 #   G.711 a-law
  - g722                 #   wideband (HD) for IP endpoints
  - opus                 #   Opus (HD) for IP endpoints / softphones
console_enabled: true    # telnet operator console on the LAN (see §7)
console_port: 2300       # TCP port for the telnet operator console
console_bind: "0.0.0.0"  # 127.0.0.1 to restrict the telnet console to the host
console_web_enabled: true # browser version of the operator console (xterm.js)
console_web_port: 8100   # TCP port for the console web terminal (LAN)
rooms:
  - ext: "101"           # extension number you dial (2–6 digits)
    name: Kitchen        # friendly label shown in the UI / caller ID
    secret: s0me-Strong-Pass   # MUST match the Grandstream port's SIP password
  - ext: "102"
    name: Living Room
    secret: an0ther-Pass
trunk:
  enabled: false         # set true to enable an outside line
  provider_host: ""      # e.g. sip.mytrunkprovider.com
  port: 5060
  username: ""           # trunk auth username / DID
  secret: ""             # trunk auth password
  from_user: ""          # usually same as username (optional)
  from_domain: ""        # usually same as provider_host (optional)
  outbound_caller_id: "" # number to present on outbound calls (optional)
  dial_prefix: "9"       # dial this digit first to reach the outside line
  registns: true         # register to the provider (most trunks need this)
```

**Notes**

- `rtp_end - rtp_start` caps simultaneous calls; the default 200-port window is
  far more than a home needs.
- Secrets are per room. Use a different strong secret for each port.
- Changing options and restarting the add-on regenerates the entire Asterisk
  config — the options are the source of truth.

---

## 3. Extension-numbering scheme

| FXS port | Extension | Suggested room |
|---------:|:---------:|----------------|
| 1  | 101 | Kitchen |
| 2  | 102 | Living Room |
| 3  | 103 | Master Bedroom |
| 4  | 104 | Study / Office |
| …  | …   | … |
| 16 | 116 | Workshop / Garage |

- **1xx** = rooms (room-to-room), headroom to 199.
- Reserve **2xx** for future IP phones / softphones, **3xx** for ring groups or
  paging, **9** as the outside-line prefix.
- Extensions are arbitrary — keep each `ext` unique.

**Feature codes** (configurable; dial from any room phone):

| Dial | Feature | Option |
|-----:|---------|--------|
| `0`  | Operator — speak a room to be connected, or say "automation" for lights | `operator.enabled` |
| `41` | Talking clock | `clock_ext` |
| `42` | Set/cancel a wake-up call (speak the time) | `wakeup_ext` |
| `43` | Home automation — control your lights by voice | `automation_ext` |
| `44` | Page all — ring every phone into one house-wide intercom | `page_ext` |

---

## 4. Grandstream GXW4216 V2 provisioning

Each FXS port becomes one SIP **user** that registers to this add-on.

### 4.1 Point the gateway at Home Assistant

1. **Profiles → Profile 1 → General Settings**
   - **SIP Server**: LAN IP of your Home Assistant host (the add-on uses host
     networking, so Asterisk listens there on UDP 5060).
   - **SIP Transport**: UDP
   - **NAT Traversal**: No (everything is on the LAN)
2. **Profiles → Profile 1 → Audio Settings**
   - **Preferred Vocoder**: **PCMU (G.711 µ-law) first.** The handsets are
     narrowband, so PCMU is the right primary and avoids transcoding. You may
     add G.722 / Opus lower in the list for completeness, but it won't add
     fidelity from an analog set (see §8).
   - **Disable** silence suppression / VAD for the cleanest analog audio and to
     keep antique sets' tones intact.

### 4.2 Configure each FXS port

For port _n_, under **FXS Ports**:

| Field | Value |
|-------|-------|
| **SIP User ID** | the extension, e.g. `101` |
| **Authenticate ID** | the same extension, e.g. `101` |
| **Authenticate Password** | the room's `secret` from the add-on options |
| **Name** | the room label, e.g. `Kitchen` |
| **Profile ID** | Profile 1 |
| **Enable Port** | Yes |

Save & **Apply**, reboot the gateway if ports don't register.

> **Message-waiting stutter tone (optional).** For the operator's "you have a
> message — call the operator" feature (TUI `M` / dashboard ✉) to produce the
> classic **stutter dial tone** on an antique handset, the gateway must turn a
> SIP MWI notification into the analog stutter. In **Profile 1** (or per-port),
> enable **"Send Stutter Dialtone for MWI"** / **"MWI → Stutter Tone"** (the exact
> label varies by firmware). Without it the indicator still tracks in the
> dashboard, but the phone's dial tone won't stutter.

### 4.3 Dialing behavior

In **Profile 1 → Dial Plan**, a starter pattern:

```
{ 1xx | 9xxxxxxxxxx | 911 }
```

For **pulse/rotary** phones, enable the port's **Pulse Dialing** option.

### 4.4 Verify

On the **Web UI**, each provisioned room shows **Registered** within ~30 s. If
not, see section 6.

---

## 5. Adding an outside line (SIP trunk)

1. Sign up with a SIP trunk provider (host, username, password, a DID).
2. In **Configuration**, set:
   ```yaml
   trunk:
     enabled: true
     provider_host: sip.yourprovider.com
     username: "1XXXXXXXXXX"
     secret: "provider-password"
     outbound_caller_id: "1XXXXXXXXXX"
     dial_prefix: "9"
     registns: true
   ```
3. **Restart** the add-on.
4. **Outbound**: dial `9` then the number.
5. **Inbound**: rings every room phone at once (whole-house ring). Ring groups
   or per-DID routing can be added later.

With `enabled: false` none of this config is emitted; the PBX is purely
room-to-room.

---

## 6. Troubleshooting

| Symptom | Check |
|--------|-------|
| Room stays **Offline** | Gateway SIP Server = HA host IP? Port enabled? Password matches the room `secret` exactly? |
| **Cannot reach Asterisk Manager** banner | Add-on still starting, or Asterisk crashed — see the **Log** tab. |
| No / one-way audio | `direct_media = no` (default) and `rtp_start..rtp_end` not blocked by a host firewall. Host networking is required (set by the add-on). |
| Rotary phone won't dial | Enable **Pulse Dialing** on that FXS port. |
| Calls drop after ~30 s | Usually a NAT/registration timer — set NAT Traversal = No on the LAN. |
| Opus not negotiated | The prebuilt Opus module may be absent on your arch (the build logs a NOTE). G.722 still gives wideband; G.711 still works. |

**Useful Asterisk CLI**:

```
asterisk -rx "pjsip show endpoints"
asterisk -rx "pjsip show contacts"
asterisk -rx "core show channels"
asterisk -rx "core show codecs"
```

---

## 7. Operator console (telnet + browser)

A live switchboard board an operator can drive: see every room phone's status,
**ring** a room, **connect** two rooms (patch a call), **hang up**, **set/cancel
a wake-up**, **page all** phones, leave a **message-waiting** stutter tone, and
control **lights**. Two front-ends, same board:

- **Telnet** — `telnet <ha-host> 2300`. Arrow keys select; `R` ring, `C`
  connect, `H` hang up, `W` set a wake-up (type a time — `7:30`, `quarter past
  six`, `noon`), `X` cancel a wake-up, `M` message (toggle the room's
  call-the-operator stutter tone), `P` page all (ring everyone into one
  intercom), `L` lights (browse Home Assistant lights and toggle them), `?`
  help, `Q` quit. Toggle with `console_enabled`; restrict to the host with
  `console_bind: 127.0.0.1`. (`M`/`P`/`L` drive your phones and home — keep the
  console on a trusted LAN, or bind it to `127.0.0.1`.)
- **Browser web terminal** — the same TUI rendered with xterm.js at
  `http://<ha-host>:8100/`. It runs a tiny stdlib HTTP + WebSocket server that
  bridges your browser to the telnet console on the host (WebSocket ⇄ telnet),
  so no telnet client is needed. Toggle with `console_web_enabled` /
  `console_web_port`. It idles if `console_enabled` is false (nothing to bridge).

### Add it to the Home Assistant sidebar

The web terminal is a plain web page, so add it as a sidebar panel with a
`panel_iframe` in your HA `configuration.yaml` (replace the host/IP):

```yaml
panel_iframe:
  switchboard_tui:
    title: "Switchboard TUI"
    icon: mdi:console
    url: "http://homeassistant.local:8100/"
    require_admin: true
```

Restart Home Assistant; **Switchboard TUI** appears in the sidebar.

> **Security:** both the telnet console and the web terminal are
> **unauthenticated on the LAN** and can ring/connect/hang up phones. The web
> terminal fronts the same console, but over a *browser* transport the raw telnet
> port is not — so the WebSocket upgrade is **same-origin-gated** (a cross-origin
> drive-by web page is rejected), and the bind follows `console_bind` (set
> `console_web_bind`/`console_bind` to `127.0.0.1` to keep it host-local).
> Sessions are capped and idle-timed-out. Still: anyone who can reach the port
> with a same-origin page can drive the board — keep it on a trusted LAN, set
> `require_admin: true` on the iframe panel, and use `console_*_enabled: false`
> to turn either off. Home Assistant's own Ingress UI (sidebar **Switchboard**)
> remains the authenticated management surface.

---

## 8. How it's built

- **Asterisk 21 + PJSIP** is the only moving part; `chan_sip` is not used.
- The add-on regenerates `/etc/asterisk/{pjsip,extensions,rtp,manager,logger}.conf`
  from your options on every start (`switchboard-config`).
- The **Ingress UI** is a small FastAPI app reading live state over the
  localhost-only AMI socket.
- Runs under an **AppArmor** profile and **host networking** (required for SIP +
  RTP on your LAN).

---

## 9. Codecs and "HD voice" on analog phones — the honest version

The GXW4216 **V2** genuinely supports wideband codecs (**G.722, Opus**) on its
SIP side. But "HD" is bounded by the audio *source*:

- **Antique analog handsets are narrowband by physics** — the carbon/electret
  element and the 2-wire loop top out around 300–3400 Hz. Wrapping that in Opus
  or G.722 carries no extra fidelity; the analog transducer is the ceiling.
- **G.711 µ-law is the right primary** for analog room-to-room: no transcoding,
  lowest latency, and it cleanly carries dial tone / ringback / fax tones.
- **Opus / G.722 pay off between *IP* endpoints** — softphones, IP phones, a
  future HD intercom — or to save bandwidth on a remote SIP leg.

Switchboard's default `codecs` list reflects this: ulaw first (analog), then
alaw, g722, opus (so HD endpoints can still negotiate up). Reorder or trim the
list to taste. If you add IP/softphone extensions later, putting `opus` first
for *those* devices is where you'll actually hear the difference.

> Transcoding note: if an ATA offers Opus and the far end is G.711, Asterisk
> transcodes (CPU + latency). For analog-to-analog, keeping both ends on G.711
> is best — which is exactly what the default preference does.
