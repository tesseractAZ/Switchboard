# Switchboard

A self-hosted phone system for the **analog phones in your home** — antique
Western Electric sets, rotary desk phones, a wall phone in the kitchen — built on
**Asterisk 21 + PJSIP** and packaged as a native **Home Assistant add-on** with
**Ingress** and an **AppArmor** profile.

The phones connect through a **Grandstream GXW4216 V2** FXS gateway; each port
becomes a room extension. Every phone can call every other phone, dial a voice
operator, set a wake-up call by speaking the time, page the whole house, control
your lights, and — when you want it — reach the outside world over a SIP trunk.

> One add-on, running inside Home Assistant next to your others. No separate PBX
> box, no FreePBX, no LAMP stack. G.711 µ-law end to end, so a call never
> transcodes.

---

## What you get

- **Room-to-room calling** between every analog phone in the house.
- **A voice operator (dial `0`)** — say a room name ("Kitchen"), an extension
  ("one four"), or a command ("wake me up", "lights"). Speech recognition runs
  **entirely on-box** (whisper.cpp), so nothing leaves your network.
- **Directory assistance (dial `411`)** — say a room to be connected, or "list"
  to hear every room read out.
- **Talking clock (dial `41`)** — an old-style "at the sound of the tone" speaking
  clock.
- **Wake-up calls (dial `42`)** — say a time ("seven thirty", "quarter past six");
  the phone rings you at that time and speaks it back, and can raise a Home
  Assistant scene, read the weather, and announce your next calendar event.
- **Home-automation voice menu (dial `43`)** — turn your Home Assistant lights on
  and off by voice, by room.
- **Intercom paging (dial `44`)** — talk out of every handset at once.
- **Dial-a-status (dial `45`)** — hear live Home Assistant readings spoken aloud:
  power/battery, the weather, the state of the house.
- **Announcements (dial `46`)** — record a message and play it out your Home
  Assistant speakers, bracketed by a station chime. A companion HTTP endpoint lets
  Home Assistant (or another add-on) speak an alert *onto a handset* — turning any
  phone, including a cordless, into an announce target.
- **SIP-trunk outside line** — flip `trunk.enabled: true`, fill in your provider,
  and get real inbound and outbound calls, with layered toll-fraud protection.
  Off by default; room-to-room needs no trunk.
- **Live dashboard** in the Home Assistant sidebar (Ingress) — every phone's
  registration and call state, with test-ring, patch-two-rooms, hang-up,
  transfer, page, message-waiting, and wake-up controls.
- **Operator console** — a full-screen switchboard TUI over telnet, plus a
  browser version, for driving the board by keystroke.
- **Proactive health monitoring** — the add-on watches every phone's registration
  and latency, scores each call's audio quality, and tracks the WiFi cordless's
  battery/signal, publishing it all as Home Assistant sensors and raising a
  notification only when something is actually wrong.

## A note on "HD voice" and analog phones

Switchboard is **G.711 µ-law only**, on every leg, and it is not configurable.
That is deliberate: an antique analog handset is narrowband *by physics* — the
carbon/electret element and the two-wire loop top out around 300–3400 Hz — so
wrapping it in G.722 or Opus buys nothing. Pinning one codec everywhere means **no
call ever transcodes**: lowest latency, and dial tone, ringback, and fax/modem
tones pass through cleanly. See [DOCS §13](switchboard/DOCS.md#13-codecs--g711-µ-law-only-on-purpose).

---

## Requirements

- **Home Assistant OS / Supervised** (this is a Supervisor add-on). Architectures:
  `amd64`, `aarch64`.
- A **Grandstream GXW4216 V2** (or compatible) FXS gateway on the same LAN, one
  FXS port per analog phone. Host networking is used, so the gateway and Home
  Assistant talk SIP directly with no NAT.
- Optional: a **Grandstream WP826** WiFi cordless as a room extension and
  Home-Assistant announce endpoint.
- Optional: a **SIP-trunk provider** (host, username, password, a DID) for an
  outside line.
- Optional: Home Assistant **media players** (for announcements) and **lights**
  (for the automation menu and console light control).

## Quick start

1. **Add the repository** to Home Assistant → Settings → Add-ons → Add-on Store →
   ⋮ → Repositories → `https://github.com/tesseractAZ/Switchboard`, then install
   **Switchboard**.
2. Open **Configuration** and define one entry under `rooms` per phone — a unique
   `ext` (2–6 digits), a friendly `name`, and a strong `secret` (the SIP password
   that port will use). **Change the `change-me-…` placeholder secrets.**
3. **Start** the add-on. The **Log** tab shows `switchboard-config` rendering the
   config, then Asterisk starting.
4. **Provision the gateway** ([DOCS §7](switchboard/DOCS.md#7-grandstream-gxw4216-v2-provisioning))
   so each FXS port registers — SIP server = your Home Assistant IP, transport
   UDP, and per port the User ID / Auth ID = the extension and the password = that
   room's `secret`.
5. Open the **Switchboard** panel in the sidebar; each room flips to **Registered**
   as its port comes online. Pick up a phone and dial another room. Done.

## Feature codes at a glance

| Dial | Feature | Option |
|-----:|---------|--------|
| `0`   | Voice operator — say a room, extension, "lights", or "wake me up" | `operator.enabled` |
| `41`  | Talking clock | `clock_ext` |
| `42`  | Set / cancel a wake-up call | `wakeup_ext` |
| `43`  | Home-automation voice menu (lights) | `automation_ext` |
| `44`  | Page all — house-wide intercom | `page_ext` |
| `45`  | Dial-a-status (live Home Assistant readings) | `status_ext` |
| `46`  | Announce out your Home Assistant speakers | `announce_ext` |
| `411` | Directory assistance | `directory_ext` |

Every feature code `41`–`411` is configurable and can be disabled; dial `0` (the
operator) can be turned off but not re-assigned. The table shows the defaults.

## How it's built

- **Asterisk 21 + PJSIP** is the only moving part (`chan_sip` is not used). On
  every start, `switchboard-config` regenerates the entire Asterisk configuration
  from your add-on options, so the running PBX always matches what you declared.
- **Offline voice**: speech-to-text is **whisper.cpp** (English `base.en` model,
  kept resident in RAM), and text-to-speech is **espeak-ng** — both on-box, no
  cloud, no API key.
- The **dashboard** is a small FastAPI app talking to Asterisk over a
  loopback-only Manager (AMI) socket; it runs behind Home Assistant Ingress.
- The health monitors, wake-up scheduler, voice recognizer, and operator console
  run as separate supervised services under s6-overlay.
- Built on the Home Assistant **Alpine 3.21** base image, under an AppArmor
  profile and host networking (required for SIP + RTP on your LAN).

```
Antique analog phones ──FXS──▶ Grandstream GXW4216 V2 ─┐
WP826 WiFi cordless ──────WiFi─────────────────────────┤   each phone = 1 SIP extension
                                                        ▼
                             Home Assistant host (LAN, host network)
                             ┌───────────────────────────────────────┐
                             │  Add-on: Switchboard                   │
                             │   • Asterisk 21 + PJSIP  (SIP/RTP)      │
                             │   • Ingress dashboard (FastAPI ⇄ AMI)   │
                             │   • Voice: whisper.cpp STT + espeak TTS │
                             │   • Health monitors → HA sensors        │
                             │   • Config generated from add-on options│
                             └───────────────────────────────────────┘
                                          │ (optional) SIP trunk
                                          ▼
                                    outside line (PSTN)
```

## Documentation

- **[Add-on documentation](switchboard/DOCS.md)** — installation, the complete
  configuration reference (every option), the voice features, gateway and cordless
  provisioning, the SIP trunk, the operator console, health monitoring and
  sensors, codecs, and troubleshooting.
- **[Security](switchboard/SECURITY.md)** — the security model, toll-fraud
  defenses, the accepted LAN-local risks, what you must configure, and how to
  report an issue.
- **[Changelog](switchboard/CHANGELOG.md)** — release history.
- **[Project status / handoff](STATUS.md)** — current state and the operational
  runbook.

## Security in one paragraph

The Ingress dashboard is reachable only from the Home Assistant Supervisor; the
Asterisk Manager socket is loopback-only with a fresh random secret each boot and
no shell-command privilege; the SIP trunk blocks international/premium prefixes and
confines every transfer to internal destinations. The telnet operator console and
its browser terminal are **unauthenticated on your LAN by design** — keep them on a
trusted network or bind them to `127.0.0.1`. Change the default room secrets before
your phones register. Full details in [SECURITY.md](switchboard/SECURITY.md).

## License

[MIT](LICENSE).
