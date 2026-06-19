# Home PBX

A self-hosted Asterisk phone system for the analog phones in your home, packaged
as a Home Assistant add-on. It turns each FXS port on your **Grandstream
GXW4216 V2** into a room extension, lets every phone call every other phone, and
is ready to grow an outside line via a SIP trunk.

---

## 1. Quick start

1. **Install** the add-on (you already did if you're reading this).
2. Open the **Configuration** tab and define one entry under `rooms` per phone,
   each with a unique extension number, a friendly name, and a **secret**
   (password the Grandstream port will use to register). Example below.
3. **Start** the add-on. Watch the **Log** tab; you should see
   `pbx-generate-config done: N room(s)` then Asterisk starting.
4. Provision the GXW4216 V2 (section 4) so each FXS port registers to the
   add-on.
5. Open the **Web UI** (Ingress, sidebar) — each room flips to **Registered**
   as its port comes online.
6. Pick up a phone and dial another room's extension. Done.

---

## 2. Options reference

```yaml
log_level: info          # trace|debug|info|notice|warning|error|critical
rtp_start: 10000         # first UDP port for RTP media
rtp_end: 10200           # last UDP port for RTP media (allow ~2 per concurrent call)
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
- Secrets are per room. Use a different strong secret for each port; they only
  ever travel on your LAN but there's no reason to reuse them.
- Changing options and restarting the add-on regenerates the entire Asterisk
  config — the options are the source of truth.

---

## 3. Extension-numbering scheme

A simple, expandable plan for the 16 FXS ports:

| FXS port | Extension | Suggested room |
|---------:|:---------:|----------------|
| 1  | 101 | Kitchen |
| 2  | 102 | Living Room |
| 3  | 103 | Master Bedroom |
| 4  | 104 | Study / Office |
| 5  | 105 | Hallway |
| …  | …   | … |
| 16 | 116 | Workshop / Garage |

- **1xx** = rooms (room-to-room). Plenty of headroom to 199.
- Reserve **2xx** for future IP phones / softphones, **3xx** for ring groups or
  paging, **9** as the outside-line prefix (so `9` + number dials out once a
  trunk is enabled).
- Extensions are arbitrary — the table is a convention, not a requirement. Just
  keep each `ext` unique.

---

## 4. Grandstream GXW4216 V2 provisioning

The GXW4216 V2 has 16 FXS ports. Each port becomes one SIP **user** that
registers to this add-on. Do this once per port.

### 4.1 Point the gateway at Home Assistant

In the GXW4216 web UI:

1. **Profiles → Profile 1 → General Settings**
   - **SIP Server**: the LAN IP of your Home Assistant host (the add-on uses
     host networking, so Asterisk listens on that IP, UDP 5060).
   - **SIP Transport**: UDP
   - **NAT Traversal**: No (everything is on the LAN)
2. **Profiles → Profile 1 → Audio Settings**
   - **Preferred Vocoder**: PCMU (G.711 µ-law) first. You may add G.722 if you
     ever bridge to an HD IP endpoint, but analog handsets are narrowband so
     PCMU is the right primary.
   - **Disable** silence suppression / VAD for the cleanest analog audio and to
     keep antique sets' tones intact.

### 4.2 Configure each FXS port

Under **FXS Ports** (the per-port table), for port _n_:

| Field | Value |
|-------|-------|
| **SIP User ID** | the extension, e.g. `101` |
| **Authenticate ID** | the same extension, e.g. `101` |
| **Authenticate Password** | the room's `secret` from the add-on options |
| **Name** | the room label, e.g. `Kitchen` |
| **Profile ID** | Profile 1 |
| **Enable Port** | Yes |

Repeat for every connected port, matching each to a `rooms` entry. Save &
**Apply**, then **Reboot** the gateway if ports don't register.

### 4.3 Dialing behavior (optional but recommended)

Antique rotary/pulse phones and short extensions dial faster with a sensible
dial plan on the gateway. In **Profile 1 → Dial Plan**, a starter pattern:

```
{ 1xx | 9xxxxxxxxxx | 911 }
```

- `1xx` — three-digit room extensions send immediately.
- `9xxxxxxxxxx` — outside line (only matters once a trunk is on).
- Adjust to your local dialing. If you have **pulse/rotary** phones, make sure
  the port's **Pulse Dialing** option is enabled.

### 4.4 Verify

On the add-on **Web UI**, each provisioned room should show **Registered**
within ~30 seconds. If not, check section 6.

---

## 5. Adding an outside line (SIP trunk)

When you're ready for inbound/outbound PSTN:

1. Sign up with a SIP trunk provider (e.g. one that gives you a DID number,
   host, username, password).
2. In the add-on **Configuration**, set:
   ```yaml
   trunk:
     enabled: true
     provider_host: sip.yourprovider.com
     username: "1XXXXXXXXXX"     # often your DID
     secret: "provider-password"
     outbound_caller_id: "1XXXXXXXXXX"
     dial_prefix: "9"
     registns: true
   ```
3. **Restart** the add-on.
4. **Outbound**: dial `9` then the number (e.g. `9` `1` `555` `0123` …).
5. **Inbound**: an incoming call rings **every** room phone at once (a classic
   "whole-house ring"). You can later refine this into ring groups or route a
   DID to a specific room — open an issue / ask and we'll extend the dialplan.

The trunk is fully isolated: with `enabled: false` none of this config is
emitted and the PBX is purely room-to-room.

---

## 6. Troubleshooting

| Symptom | Check |
|--------|-------|
| Room stays **Offline** | Gateway SIP Server = HA host IP? Port enabled? Password matches the room `secret` exactly? |
| **Cannot reach Asterisk Manager** banner | The add-on is still starting, or Asterisk crashed — see the **Log** tab. |
| No audio / one-way audio | Confirm `direct_media = no` (default) and that `rtp_start..rtp_end` isn't blocked by a host firewall. Host networking is required (set by the add-on). |
| Rotary phone won't dial | Enable **Pulse Dialing** on that FXS port in the gateway. |
| Calls drop after ~30s | Usually a NAT/registration timer on the gateway — set NAT Traversal = No on the LAN. |

**Useful Asterisk CLI** (from the add-on console, if enabled, or via
`docker exec`):

```
asterisk -rx "pjsip show endpoints"
asterisk -rx "pjsip show contacts"
asterisk -rx "core show channels"
```

---

## 7. How it's built

- **Asterisk 21 + PJSIP** is the only moving part; `chan_sip` is not used.
- The add-on regenerates `/etc/asterisk/{pjsip,extensions,rtp,manager,logger}.conf`
  from your options on every start (`pbx-generate-config`).
- The **Ingress UI** is a small FastAPI app that reads live state over the
  localhost-only AMI socket.
- Runs under an **AppArmor** profile and **host networking** (required for SIP +
  RTP on your LAN).
