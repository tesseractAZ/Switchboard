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

## 7. How it's built

- **Asterisk 21 + PJSIP** is the only moving part; `chan_sip` is not used.
- The add-on regenerates `/etc/asterisk/{pjsip,extensions,rtp,manager,logger}.conf`
  from your options on every start (`switchboard-config`).
- The **Ingress UI** is a small FastAPI app reading live state over the
  localhost-only AMI socket.
- Runs under an **AppArmor** profile and **host networking** (required for SIP +
  RTP on your LAN).

---

## 8. Codecs and "HD voice" on analog phones — the honest version

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
