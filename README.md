# Switchboard

**A self-hosted phone system for the analog phones in your home** — antique
Western Electric sets, rotary desk phones, the wall phone in the kitchen — built on
**Asterisk 21 + PJSIP** and packaged as a native **Home Assistant add-on** with an
Ingress dashboard and an AppArmor profile.

Analog handsets plug into a **Grandstream GXW4216 V2** FXS gateway; each port
becomes a room extension. From any phone you can call any other phone, reach a
voice operator that actually understands you, set a wake-up call by speaking the
time, page the whole house, control your lights, hear the weather — and, when you
want it, dial the outside world over a SIP trunk like it's a cell phone.

> One add-on, running inside Home Assistant next to your others. No separate PBX
> box, no FreePBX, no LAMP stack. Speech recognition runs **on-box** — nothing
> leaves your network. G.711 µ-law end to end, so a call never transcodes.

---

## What you get

- **Room-to-room calling** between every analog phone in the house.
- **A voice operator (dial `0`)** that hands you off to *anything*. Say a room name
  ("Kitchen"), a spoken extension ("one four"), or just what you want — **"time"**,
  **"weather"**, **"wake me up"**, **"lights"**, **"directory"**, **"page the
  house"**, **"announce"**. Recognition is **whisper.cpp on-box**; a confident room
  name always wins over a feature word, and it re-asks rather than guess wrong.
- **Directory assistance (dial `411`)** — say a room to be connected, or "list" to
  hear every room and extension read out.
- **Talking clock (dial `41`)** — an old-style "at the sound of the tone" speaking
  clock in your local time.
- **Wake-up calls (dial `42`)** — say a time ("seven thirty", "quarter past six");
  the phone rings you and speaks it back, and can raise a Home Assistant scene, read
  the local weather, and announce your next calendar event.
- **Home-automation voice menu (dial `43`)** — turn your Home Assistant lights on
  and off by voice, room by room.
- **Intercom paging (dial `44`)** — talk out of every handset at once.
- **Dial-a-status (dial `45`)** — hear live Home Assistant readings spoken aloud:
  power/battery, the weather, the state of the house.
- **Announcements (dial `46`)** — record a message and play it out your Home
  Assistant speakers, bracketed by a station chime. A companion HTTP endpoint lets
  Home Assistant (or another add-on) speak an alert *onto a handset* — turning any
  phone, including a WiFi cordless, into an announce target.
- **An outside line, dialed like a cell phone.** Enable a SIP trunk for real
  inbound and outbound calls. With **direct dial** you dial `1` + the 10-digit
  number with no prefix, while your extensions and feature codes still ring
  instantly — all behind layered toll-fraud protection. Off by default; room-to-room
  needs no trunk.
- **A live dashboard** in the Home Assistant sidebar (Ingress): every phone's
  registration and call state, the trunk's registration and the speech engine's
  health, per-phone latency, and one-click test-ring, patch-two-rooms, hang-up,
  transfer, page, message-waiting, and wake-up controls.
- **A full-screen operator console** — a switchboard board you drive by keystroke,
  over telnet and in the browser, mirroring the same live signals.
- **Proactive health monitoring** — the add-on watches every phone's registration
  and round-trip latency, scores each call's audio quality, tracks the WiFi
  cordless's battery and signal, and watches the trunk registration — publishing it
  all as Home Assistant sensors and raising a notification only when something is
  actually wrong (a whole-gateway outage, a dying battery, a genuinely rough call).

## Why G.711 µ-law only

Switchboard is **G.711 µ-law on every leg**, and it isn't configurable — on
purpose. An antique analog handset is narrowband *by physics*: the carbon/electret
element and the two-wire loop top out around 300–3400 Hz, so wrapping it in G.722 or
Opus buys nothing the transducer can reproduce. Pinning one codec everywhere means
**no call ever transcodes** — lowest latency, and dial tone, ringback, and fax/modem
tones pass through cleanly. See
[DOCS §13](switchboard/DOCS.md#13-codecs--g711-µ-law-only-on-purpose).

---

## Requirements

- **Home Assistant OS / Supervised** (this is a Supervisor add-on). Architectures:
  `amd64`, `aarch64`.
- A **Grandstream GXW4216 V2** (or compatible) FXS gateway on the same LAN — one FXS
  port per analog phone. The add-on uses host networking, so the gateway and Home
  Assistant speak SIP directly with no NAT.
- *Optional:* a **Grandstream WP826** WiFi cordless as a room extension and Home
  Assistant announce endpoint.
- *Optional:* a **SIP-trunk provider** (host, username, password, a DID) for an
  outside line.
- *Optional:* Home Assistant **media players** (for announcements) and **lights**
  (for the automation menu and console light control).

## Quick start

1. **Add the repository** to Home Assistant → Settings → Add-ons → Add-on Store →
   ⋮ → Repositories → `https://github.com/tesseractAZ/Switchboard`, then install
   **Switchboard**.
2. Open **Configuration** and define one entry under `rooms` per phone — a unique
   `ext` (2–6 digits), a friendly `name`, and a strong `secret` (the SIP password
   that port will use). **Change the `change-me-…` placeholder secrets.**
3. **Start** the add-on. The **Log** tab shows `switchboard-config` rendering the
   configuration, then Asterisk starting.
4. **Provision the gateway**
   ([DOCS §7](switchboard/DOCS.md#7-grandstream-gxw4216-v2-provisioning)) so each FXS
   port registers — SIP server = your Home Assistant IP, transport UDP, and per port
   the User ID / Auth ID = the extension and the password = that room's `secret`.
5. Open the **Switchboard** panel in the sidebar; each room flips to **Registered**
   as its port comes online. Pick up a phone and dial another room. Done.

Changing options and restarting the add-on regenerates the entire Asterisk
configuration — **the add-on options are the single source of truth**; hand edits to
`/etc/asterisk/*.conf` are overwritten on every start.

## Feature codes at a glance

| Dial  | Feature | Option |
|------:|---------|--------|
| `0`   | Voice operator — say a room, an extension, or any feature by name | `operator.enabled` |
| `41`  | Talking clock | `clock_ext` |
| `42`  | Set / cancel a wake-up call | `wakeup_ext` |
| `43`  | Home-automation voice menu (lights) | `automation_ext` |
| `44`  | Page all — house-wide intercom | `page_ext` |
| `45`  | Dial-a-status (live Home Assistant readings) | `status_ext` |
| `46`  | Announce out your Home Assistant speakers | `announce_ext` |
| `411` | Directory assistance | `directory_ext` |

Every feature code `41`–`411` is configurable and can be disabled; dial `0` (the
operator) can be turned off but not re-assigned. On a live call, analog phones
blind-transfer with `##` and attended-transfer with `*2` (internal destinations
only). The table shows the defaults.

## How it's built

- **Asterisk 21 + PJSIP** is the only telephony engine (`chan_sip` is not used). On
  every start, `switchboard-config` regenerates the entire Asterisk configuration
  from your add-on options, so the running PBX always matches what you declared.
- **Offline voice**: speech-to-text is **whisper.cpp** (English `base.en` model,
  kept resident in RAM with a per-call fallback), and text-to-speech is
  **espeak-ng** — both on-box, no cloud, no API key.
- The **dashboard** is a small FastAPI app talking to Asterisk over a loopback-only
  Manager (AMI) socket, served behind Home Assistant Ingress.
- The health monitors, wake-up scheduler, resident recognizer, and operator console
  run as separate supervised services under s6-overlay, each idling when its feature
  is off.
- Built on the Home Assistant **Alpine** base image (a two-stage build that compiles
  whisper.cpp from source), under an AppArmor profile and host networking (required
  for SIP + RTP on your LAN).

```
Antique analog phones ──FXS──▶ Grandstream GXW4216 V2 ─┐
WP826 WiFi cordless ──────WiFi─────────────────────────┤   each phone = 1 SIP extension
                                                        ▼
                             Home Assistant host (LAN, host network)
                             ┌─────────────────────────────────────────┐
                             │  Add-on: Switchboard                     │
                             │   • Asterisk 21 + PJSIP  (SIP / RTP)      │
                             │   • Ingress dashboard (FastAPI ⇄ AMI)     │
                             │   • Voice: whisper.cpp STT + espeak TTS   │
                             │   • Health monitors → Home Assistant      │
                             │   • Config generated from add-on options  │
                             └─────────────────────────────────────────┘
                                          │ (optional) SIP trunk
                                          ▼
                                    outside line (PSTN)
```

## Documentation

- **[Complete system reference](switchboard/DOCS.md)** — installation, every
  configuration option, the voice features, gateway and cordless provisioning, the
  SIP trunk and direct dial, the operator console, health monitoring and sensors,
  codecs, troubleshooting, and reproducing the system on new hardware.
- **[Security](switchboard/SECURITY.md)** — the security model, toll-fraud defenses,
  the accepted LAN-local risks, what you must configure, and how to report an issue.
- **[Changelog](switchboard/CHANGELOG.md)** — the full release history.
- **Printable manual** — every [GitHub Release](https://github.com/tesseractAZ/Switchboard/releases)
  ships the README + Security + reference assembled into a **Word (`.docx`)** and
  **PDF** you can download and read offline.

Every version is tagged and released, so `git checkout vX.Y.Z` reproduces the exact
source that built any release — see
[DOCS §16, *Reproducing on new hardware*](switchboard/DOCS.md#16-reproducing-on-new-hardware).

## Security in one paragraph

The Ingress dashboard is reachable only from the Home Assistant Supervisor; the
Asterisk Manager socket is loopback-only with a fresh random secret each boot and no
shell-command privilege; the SIP trunk blocks international/premium prefixes and
confines every transfer to internal destinations. The telnet operator console and
its browser terminal are **unauthenticated on your LAN by design** — keep them on a
trusted network or bind them to `127.0.0.1`. Change the default room secrets before
your phones register. Full details in [SECURITY.md](switchboard/SECURITY.md).

## License

[MIT](LICENSE) © Eric Paschal.
