# Switchboard

A self-hosted **Asterisk 21 + PJSIP** phone system for the analog phones in your
home, packaged as a Home Assistant add-on. Each FXS port on a **Grandstream
GXW4216 V2** gateway becomes a room extension; every phone can call every other
phone, reach a set of on-box voice features, and — optionally — an outside line
over a SIP trunk. Audio is **G.711 µ-law end to end** — no transcoding, no HD/Opus.

This document is the complete reference. For the security model and the accepted
LAN-local risks, see **[SECURITY.md](SECURITY.md)**.

**Contents**

1. [Quick start](#1-quick-start)
2. [Configuration reference](#2-configuration-reference)
3. [Extensions & feature codes](#3-extensions--feature-codes)
4. [The voice operator & directory](#4-the-voice-operator--directory-assistance)
5. [Wake-up calls & the talking clock](#5-wake-up-calls--the-talking-clock)
6. [Paging & announcements](#6-paging--announcements)
7. [Grandstream GXW4216 V2 provisioning](#7-grandstream-gxw4216-v2-provisioning)
8. [The WP826 WiFi cordless](#8-the-wp826-wifi-cordless-optional)
9. [Adding an outside line (SIP trunk)](#9-adding-an-outside-line-sip-trunk)
10. [The operator console](#10-the-operator-console-telnet--browser)
11. [Health monitoring & Home Assistant sensors](#11-health-monitoring--home-assistant-sensors)
12. [How it's built](#12-how-its-built)
13. [Codecs — G.711 µ-law only, on purpose](#13-codecs--g711-µ-law-only-on-purpose)
14. [Troubleshooting](#14-troubleshooting)
15. [Security](#15-security)

---

## 1. Quick start

1. **Install** the add-on (you already did if you're reading this from the add-on's
   Documentation tab).
2. Open the **Configuration** tab and define one entry under `rooms` per phone,
   each with a unique extension, a friendly name, and a **secret** (the password
   the Grandstream port will use to register). **Replace the `change-me-…`
   placeholder secrets.**
3. **Start** the add-on. Watch the **Log** tab; you should see
   `switchboard-config` render the configuration (ending with
   `codecs: u-law only (no transcoding)`), then Asterisk start.
4. **Provision the gateway** ([§7](#7-grandstream-gxw4216-v2-provisioning)) so
   each FXS port registers.
5. Open the **Switchboard** panel in the Home Assistant sidebar (Ingress) — each
   room flips to **Registered** as its port comes online.
6. Pick up a phone and dial another room's extension. Done.

Changing options and restarting the add-on regenerates the entire Asterisk
configuration — **the add-on options are the source of truth.** Editing
`/etc/asterisk/*.conf` by hand is pointless; every file is overwritten on start.

---

## 2. Configuration reference

Every option below appears in the **Configuration** tab with a friendly label and
inline help. Defaults are shown. A value is *optional* unless noted; leaving it at
its default is fine.

### Core

| Option | Default | Notes |
|--------|---------|-------|
| `log_level` | `info` | `trace \| debug \| info \| notice \| warning \| error \| critical`. Drop to `debug`/`trace` only to diagnose, then set back (`trace` is very noisy). |
| `rtp_start` | `10000` | First UDP port for live call audio (RTP). Must be below `rtp_end`. |
| `rtp_end` | `10200` | Last RTP port. The default 200-port window is far more than a home needs (~2 ports per call). |

### Rooms (the phones)

| Option | Default | Notes |
|--------|---------|-------|
| `rooms` | 2 placeholders | A list; one entry per handset. Each has `ext` (2–6 digits, unique), `name` (shown in the directory/operator/dashboard), and `secret` (the SIP password — **change it from the default**). The shipped defaults are `101 Kitchen` / `102 Living Room` with `change-me-…` secrets. |

### Voice operator & speech

| Option | Default | Notes |
|--------|---------|-------|
| `operator.enabled` | `true` | The voice operator on dial `0`. |
| `operator_synonyms` | `[]` | Extra spoken aliases mapped to a room, for accents/nicknames. Each entry has `ext` and `phrases` (e.g. "lounge" → the Living Room). |
| `stt_resident` | `true` | Keep the speech-to-text model resident in RAM for instant response. Turn off on a very memory-constrained host; recognition then loads the model on demand per call. |

### Call-quality & health monitoring

| Option | Default | Notes |
|--------|---------|-------|
| `call_quality_alerts` | `true` | Notify when a call's audio is poor (low MOS, high loss, one-way). Measurements are always recorded to a sensor regardless. |
| `link_health_enabled` | `true` | Poll every phone's registration + round-trip latency (RTT) between calls, published to sensors. |
| `link_health_interval` | `300` | Seconds between link-health polls. Range 30–86400. |
| `link_health_alerts` | `true` | Notify when many phones lose registration at once (a shared-gateway outage). |
| `device_health_enabled` | `true` | Watch the WP826 cordless (battery/WiFi/per-call MOS) and derive gateway health. Needs `cordless_password` for the deep checks. |
| `device_health_interval` | `120` | Seconds between device-health polls. Range 30–86400. |
| `device_health_alerts` | `true` | Notify when the cordless or gateway becomes unhealthy (and again on recovery). |
| `cordless_ip` | `192.168.6.71` | Fallback LAN address of the WP826 cordless. Only used if `cordless_ext` is blank or the cordless isn't registered — otherwise the monitor auto-follows the phone's live IP (see below). |
| `cordless_ext` | `19` | The extension the cordless registers as. When set, the device-health monitor takes the cordless's **current** IP from its live SIP registration and follows it automatically if DHCP moves the phone — so a changed lease no longer blinds battery/Wi-Fi/MOS monitoring. Blank = use `cordless_ip` only. |
| `cordless_password` | `""` | WP826 web-admin password; required for the deep battery/WiFi/MOS checks. Masked, never shown back. Without it the monitor still tracks reachability. |
| `gateway_ports` | `11,12,13,14,15,16,17,18` | Comma-separated extensions served by the wired GXW FXS ports, used to derive gateway health. |
| `cordless_battery_crit_pct` | `15` | Battery % (while discharging) that flags the cordless CRITICAL. Range 1–100. |
| `cordless_battery_warn_pct` | `30` | Battery % that flags it low/degraded. Should be higher than the critical %. |
| `cordless_wifi_min_signal` | `2` | Lowest acceptable WiFi bars (0–5) before flagging a weak link. |

### Announcements

| Option | Default | Notes |
|--------|---------|-------|
| `announce_enabled` | `true` | The announce feature on dial `46`. |
| `announce_ext` | `46` | The extension to dial to record an announcement. 2–6 digits. |
| `announce_players` | `media_player.west_hallway`, `media_player.guest_thermostat` | Home Assistant `media_player` entity IDs an announcement plays on. One per line. |
| `announce_token` | `""` | Optional shared secret required on the `/api/announce` HTTP endpoint (used to speak alerts onto a handset from Home Assistant / another add-on). **Blank disables LAN announce** — only the Supervisor can call it. Masked. |

### Operator console

| Option | Default | Notes |
|--------|---------|-------|
| `console_enabled` | `true` | Telnet operator console (ring/connect/hang up). **Unauthenticated on the LAN** — keep it trusted or bind to loopback, or disable. |
| `console_port` | `2300` | TCP port for the telnet console. |
| `console_bind` | `0.0.0.0` | Interface it listens on. `127.0.0.1` restricts it to the host. |
| `console_web_enabled` | `true` | Browser version of the console (xterm.js). Also unauthenticated on the LAN. Idles if `console_enabled` is off. |
| `console_web_port` | `8100` | TCP port for the web terminal. |
| `console_web_bind` | `""` | Blank = follow `console_bind` (→ all interfaces); `127.0.0.1` restricts it to the host. |

### Time, clock & wake-up

| Option | Default | Notes |
|--------|---------|-------|
| `timezone` | `""` | Blank = auto-detect the Home Assistant timezone. Set an IANA name (e.g. `America/Phoenix`) only to override. |
| `clock_enabled` / `clock_ext` | `true` / `41` | The talking clock and its dial code (2–6 digits). |
| `wakeup_enabled` / `wakeup_ext` | `true` / `42` | Wake-up calls and the dial code. |
| `wakeup_ring_seconds` | `60` | How long a wake-up rings before giving up. Range 10–600. |
| `wakeup_scene` | `""` | Optional HA `scene.*` entity activated when a wake-up fires. |
| `wakeup_weather` | `true` | Speak a short local weather summary during the wake-up call. |
| `wakeup_calendar` | `""` | Optional HA `calendar.*` entity whose next event is read out. |

### Extra feature codes

| Option | Default | Notes |
|--------|---------|-------|
| `automation_enabled` / `automation_ext` | `true` / `43` | Home-automation voice menu (control HA lights) and its dial code. |
| `page_enabled` / `page_ext` | `true` / `44` | All-call paging / intercom and its dial code. |
| `mwi_enabled` | `true` | Message-waiting indicator (stutter dial-tone). |
| `status_enabled` / `status_ext` | `true` / `45` | Dial-a-status voice menu (live HA readings) and its dial code. |
| `directory_enabled` / `directory_ext` | `true` / `411` | Voice directory (like 411) and its dial code. |

### Outside line (SIP trunk)

`trunk` is a group; leave `trunk.enabled: false` (the default) for a room-to-room
system. When you enable it, see [§9](#9-adding-an-outside-line-sip-trunk).

| Sub-field | Default | Notes |
|-----------|---------|-------|
| `enabled` | `false` | Turn the outside line on. **Required** when the group is present. |
| `provider_host` | `""` | Your SIP provider's host, e.g. `losangeles.voip.ms`. |
| `port` | `5060` | Provider SIP port. |
| `username` | `""` | Trunk auth username / sub-account. |
| `secret` | `""` | Trunk auth password. Must not contain `;` or leading/trailing whitespace (Asterisk would truncate it). |
| `from_user` | `""` | Outbound `From` user (defaults to `username`). |
| `from_domain` | `""` | Outbound `From` domain (defaults to `provider_host`). |
| `outbound_caller_id` | `""` | Number to present on outbound calls (digits/`+` only). |
| `inbound_ext` | `""` | Which extension(s) an inbound call rings — a single ext (`19`), a comma-separated list (`19,20`), or blank = **ring the whole house**. |
| `dial_prefix` | `9` | Digit(s) to dial first to reach an outside line. |
| `registns` | `true` | Register to the provider (most trunks need this). |

---

## 3. Extensions & feature codes

### Room extensions

An extension is **any 2–6 digit number you choose**, one per phone; they only need
to be unique. Pick a scheme that leaves room for the feature codes. The reference
home wires its eight GXW FXS ports to **`11`–`18`** and its WiFi cordless to
**`19`** (this matches the `gateway_ports` default). A `1xx` scheme (`101`, `102`,
…) works just as well — the shipped placeholder rooms use it.

Whatever you pick, keep the single-digit `0` for the operator and (if you use a
trunk) a leading digit like `9` for the outside line, and don't collide with the
feature codes below.

### Feature codes

Dial these from any room phone. All are configurable (`*_ext`) and can be disabled
(`*_enabled`); the defaults are:

| Dial | Feature | Section |
|-----:|---------|---------|
| `0`   | **Operator** — say a room, an extension, or any feature ("lights", "wake me up", "what time is it", "weather", "directory", "announce", "page") | [§4](#4-the-voice-operator--directory-assistance) |
| `41`  | **Talking clock** | [§5](#5-wake-up-calls--the-talking-clock) |
| `42`  | **Wake-up call** — set or cancel by speaking the time | [§5](#5-wake-up-calls--the-talking-clock) |
| `43`  | **Home-automation voice menu** — control your lights | [§4](#4-the-voice-operator--directory-assistance) |
| `44`  | **Page all** — house-wide intercom | [§6](#6-paging--announcements) |
| `45`  | **Dial-a-status** — hear live Home Assistant readings | [§4](#4-the-voice-operator--directory-assistance) |
| `46`  | **Announce** — speak a message out your Home Assistant speakers | [§6](#6-paging--announcements) |
| `411` | **Directory assistance** | [§4](#4-the-voice-operator--directory-assistance) |

A feature's dial code is skipped (with a log line) if it collides with a room
extension or another code, but the underlying feature still works via the
operator, the scheduler, or the dashboard.

### In-call transfer (analog phones)

On a live call, an analog phone can blind-transfer with **`##`** and
attended-transfer with **`*2`**. Transfers are confined to internal destinations —
you cannot transfer a call out to the trunk. IP phones (like the cordless) use
their own Transfer button.

---

## 4. The voice operator & directory assistance

All voice features share the same rotary-safe shape: a prompt plays, you speak one
short phrase after the beep, recognition runs **on-box** (whisper.cpp), and the
system acts. No keypress is ever required, and nothing connects a call or toggles a
light on a low-confidence guess — it asks again instead. Speech never leaves the
box.

### Operator — dial `0`

Say what you want:

- **A room name** ("Kitchen", "the study") or a **spoken extension** ("one four")
  — you're connected to that room. Add aliases with `operator_synonyms`.
- **"Wake me up"** — jumps to the wake-up flow ([§5](#5-wake-up-calls--the-talking-clock)).
- **"Lights"** / **"automation"** — jumps to the home-automation menu (below).
- **"What time is it"** — jumps to the talking clock ([§5](#5-wake-up-calls--the-talking-clock)).
- **"Weather"** / **"power"** / **"battery"** / **"house status"** — jumps to
  dial-a-status (below).
- **"Directory"** / **"who's here"** / **"list the rooms"** — jumps to directory
  assistance (below).
- **"Announce"** / **"over the speakers"** — jumps to announce ([§6](#6-paging--announcements)).
- **"Page everyone"** / **"intercom"** / **"all call"** — jumps to page-all ([§6](#6-paging--announcements)).

A **room name always wins** over a feature word, so a handset named "Office" or
"Garage" still connects normally. A feature you name but have disabled falls
through to a polite goodbye. If it can't understand you after two tries, it says
so and hangs up. Room-name matching is deliberately forgiving of narrowband
tail-clipping ("Base" → "Basement"), and refuses to guess between two similar
names.

### Directory assistance — dial `411`

Say a room name to be connected, or say **"list"** to hear every room and its
extension read out. Saying "list" doesn't burn a retry, and a mis-heard "list"
never accidentally dials a room — directory assistance only connects on a
confident match.

### Home-automation voice menu — dial `43` (or say "lights" to the operator)

A guided flow: it asks for a room, then a light in that room, tells you whether
it's on or off, and asks you to say "turn on", "turn off", or "cancel". It reads
your lights and areas live from Home Assistant. Say "list" at any step to hear the
options. Lights with no assigned area appear under "Unassigned".

### Dial-a-status — dial `45`

A looping voice menu that speaks live Home Assistant readings on demand. Say:

- **"power"** (or battery / solar / grid) — your power/battery status.
- **"weather"** — a short local forecast (from the U.S. National Weather Service,
  using your Home Assistant location).
- **"house"** — thermostats and a light count.

It keeps asking "anything else?" until you say "goodbye".

---

## 5. Wake-up calls & the talking clock

### Wake-up calls — dial `42`

Dial the code (or say "wake me up" to the operator) and, after the beep, say a
time — "seven thirty", "quarter past six", "six a.m.", "nineteen thirty", "noon".
The spoken-time parser is intentionally forgiving; the flow reads the parsed time
back so you can hear it and re-say it if it's wrong. Say "cancel" (or "clear",
"never mind") to remove a pending wake-up.

- **One wake-up per room.** Setting a new one replaces the old.
- At the set time the room rings for `wakeup_ring_seconds` (default 60). The call
  speaks a greeting and the time, runs any smart extras, then repeats the greeting
  and time.
- If the room is busy or offline through a **10-minute grace window**, the wake-up
  is dropped and surfaced as a Home Assistant persistent notification.

**Smart extras** (during the wake-up call):

- **Scene** (`wakeup_scene`) — activates a Home Assistant scene (e.g. gently raise
  the lights).
- **Weather** (`wakeup_weather`, on by default) — speaks a short local forecast.
- **Calendar** (`wakeup_calendar`) — reads your next event in the coming 18 hours.

You can also set and cancel wake-ups from the **dashboard** (the ⏰ box on each room
card). The operator console *displays* pending wake-ups read-only.

### Talking clock — dial `41`

An old-style speaking clock: "At the sound of the tone, the time will be …", then
the time as a 24-hour ("military") H-M-S readout, then a tone — looping until you
hang up. The time is spoken in your configured timezone (`timezone`, or the Home
Assistant timezone if blank).

---

## 6. Paging & announcements

### Page all — dial `44`

Dial the paging code to talk out of **every registered handset at once** — a
house-wide intercom, built on Asterisk ConfBridge with no join/leave beeps.

### Announcements — dial `46` (phone → Home Assistant speakers)

Dial the announce code, speak your message after the beep, and it plays out your
configured Home Assistant `media_player` speakers (`announce_players`), bracketed
by a three-note station chime. The message is transcribed and re-synthesized
on-box (espeak-ng), rendered to a WAV the add-on serves on your LAN, and pushed to
every speaker at once via `media_player.play_media`.

### Announce onto a handset — `POST /api/announce/{ext}` (Home Assistant → phone)

The add-on also exposes an HTTP endpoint that speaks a clip **onto a room handset**
— the integration that lets Home Assistant (or another add-on) announce to a phone,
including the WP826 cordless, the way it would to a smart speaker. The phone
auto-answers hands-free (an intercom `answer-after=0` header, caller ID `8000`) and
plays the clip.

```
POST http://<ha-host>:8099/api/announce/<ext>
Header: X-Announce-Token: <announce_token>
Body:   {"text": "Dinner is ready"}     # spoken on-box (espeak-ng), or
        {"url":  "http://…/clip.wav"}    # a WAV to fetch and play
```

- The `<ext>` must be a configured room. It can only play a local clip to a known
  handset — never place an outside call.
- **Authentication:** over the LAN this requires the `X-Announce-Token` header to
  match your `announce_token` option. If `announce_token` is blank (the default),
  LAN announce is **disabled** and only the Home Assistant Supervisor can call it.
- The `{url}` branch fetches `http`/`https` only, rejects loopback and link-local
  hosts (a private-LAN URL — such as Home Assistant's own TTS — is allowed), does
  not follow redirects, caps the body at 5 MB, and transcodes to 8 kHz for the
  phone line.

---

## 7. Grandstream GXW4216 V2 provisioning

Each FXS port becomes one SIP **user** that registers to this add-on. The GXW is
configured through its own web UI (this add-on does not push config to it).

### 7.1 Point the gateway at Home Assistant

**Profiles → Profile 1 → General Settings**

- **SIP Server**: the LAN IP of your Home Assistant host. (The add-on uses host
  networking, so Asterisk listens there on UDP 5060.)
- **SIP Transport**: UDP
- **NAT Traversal**: No (everything is on the LAN)

**Profiles → Profile 1 → Audio Settings**

- **Preferred Vocoder**: **PCMU (G.711 µ-law)**. Switchboard only offers µ-law, so
  PCMU must be in the gateway's list; anything else it advertises is simply never
  selected ([§13](#13-codecs--g711-µ-law-only-on-purpose)).
- **Disable** silence suppression / VAD for the cleanest analog audio and to keep
  antique sets' tones intact.

### 7.2 Configure each FXS port

For each wired port, under **FXS Ports**:

| Field | Value |
|-------|-------|
| **SIP User ID** | the extension, e.g. `11` |
| **Authenticate ID** | the same extension |
| **Authenticate Password** | the room's `secret` from the add-on options |
| **Name** | the room label, e.g. `Kitchen` |
| **Profile ID** | Profile 1 |
| **Enable Port** | Yes |

Save & **Apply**; reboot the gateway if ports don't register.

> **Message-waiting stutter tone (optional).** For the message-waiting indicator
> (dashboard ✉, console `M`) to produce the classic **stutter dial tone** on an
> antique handset, enable **"Send Stutter Dialtone for MWI"** / **"MWI → Stutter
> Tone"** in Profile 1 (or per port); the label varies by firmware. Without it the
> indicator still tracks in the dashboard, but the dial tone won't stutter.

### 7.3 Dialing behavior

In **Profile 1 → Dial Plan**, a starter pattern (adjust to your extensions and
outside-line prefix):

```
{ 1x | 4x | 411 | 0 | 9xxxxxxxxxx }
```

For **pulse/rotary** phones, enable the **Pulse Dialing** option on that FXS port.

### 7.4 Verify

On the **Switchboard** panel, each provisioned room shows **Registered** within
~30 s. If not, see [§14](#14-troubleshooting).

---

## 8. The WP826 WiFi cordless (optional)

A Grandstream **WP826** WiFi cordless can join as an ordinary room extension —
register it to the add-on the same way (SIP server = your Home Assistant IP, user
ID / auth ID = its extension, password = its `secret`). Beyond being a phone, the
cordless integrates in two extra ways:

- **Home Assistant announce endpoint** — with `POST /api/announce/{ext}` targeting
  the cordless's extension, Home Assistant can speak alerts on it hands-free
  ([§6](#6-paging--announcements)).
- **Device-health monitoring** — set `cordless_ext` (its extension) and
  `cordless_password` and the add-on polls the phone's own API for battery, WiFi
  signal, and per-call MOS, publishing `sensor.switchboard_cordless_health`. With
  `cordless_ext` set it **auto-follows the phone's IP** from its live registration,
  so DHCP moving the handset never breaks monitoring (`cordless_ip` is just the
  fallback)
  ([§11](#11-health-monitoring--home-assistant-sensors)).

For scripting the cordless's own settings (remote phonebook, distinctive ring,
custom ringtone, speed-dial keys), the repo ships a standalone tool and a P-code
reference at [`tools/wp826.mjs`](../tools/wp826.mjs) and
[`tools/wp826-pcodes.md`](../tools/wp826-pcodes.md).

---

## 9. Adding an outside line (SIP trunk)

1. Sign up with a SIP-trunk provider (host, username, password, a DID).
2. In **Configuration**, set:
   ```yaml
   trunk:
     enabled: true
     provider_host: losangeles.voip.ms
     port: 5060
     username: "100000_sub"
     secret: "provider-password"
     from_domain: losangeles.voip.ms
     outbound_caller_id: "15205551234"
     inbound_ext: "19"        # ring one room, or "19,20", or "" for the whole house
     dial_prefix: "9"
     registns: true
   ```
3. **Restart** the add-on.
4. **Outbound**: dial `9` then the number.
5. **Inbound**: rings the `inbound_ext` room(s), or every room if blank. Outside
   calls ring with a **distinctive ring** on the WP826 cordless (an
   `Alert-Info: …;info=outsideline` tag; analog handsets ignore it).

With `enabled: false`, none of the trunk config is emitted and the PBX is purely
room-to-room.

### Toll-fraud protection

The trunk is where the internet meets your phone bill, so several defenses are
layered on automatically (details in [SECURITY.md](SECURITY.md#toll-fraud-the-trunk-threat-model)):

- **Blocked prefixes** — international (`011`) and premium (`900`, `1-900`) are
  rejected before any outbound rule.
- **Inbound calls get no in-call feature codes** — an outside caller can't key
  `##` to reach an outbound path.
- **Transfers are internal-only** — a transferred outside caller lands in a context
  with no outbound rule at all.
- **Provider-initiated transfers (REFER) are rejected.**
- The trunk **re-registers every 120 s** to hold the router's NAT pinhole open
  (many providers, e.g. VoIP.ms, don't answer keep-alive OPTIONS reliably, so the
  AOR is deliberately not qualified).

---

## 10. The operator console (telnet + browser)

A live switchboard board an operator can drive by keystroke: see every phone's
status, **ring** a room, **connect** two rooms (patch a call), **hang up**,
**transfer**, **set/cancel a wake-up**, toggle **message-waiting**, **page all**,
and control **lights**. Two front-ends onto the same board:

- **Telnet** — `telnet <ha-host> 2300`. Keys: **↑↓ / j k** move, **R** ring,
  **C** connect, **H** hang up, **T** transfer, **W** set wake-up (type a time —
  `7:30`, `quarter past six`, `noon`), **X** cancel wake-up, **M** message-waiting,
  **P** page all, **L** lights, **?** help, **Q** / Ctrl-C quit. Toggle with
  `console_enabled`; restrict to the host with `console_bind: 127.0.0.1`.
- **Browser web terminal** — the same TUI rendered with xterm.js at
  `http://<ha-host>:8100/`. A tiny stdlib HTTP + WebSocket server bridges the
  browser to the telnet console on the host, so no telnet client is needed. Toggle
  with `console_web_enabled` / `console_web_port`. It idles if `console_enabled` is
  off (nothing to bridge to).

> **Security:** both the telnet console and the web terminal are **unauthenticated
> on the LAN** and can ring/connect/hang up phones. The web terminal's WebSocket
> upgrade is same-origin-gated (a cross-origin drive-by page is rejected), sessions
> are capped (5) and idle-timed-out (15 min), and the bind follows `console_bind` —
> but anyone who can reach the port from a same-origin page can drive the board.
> Keep it on a trusted LAN, or bind it to `127.0.0.1`, or disable it. Home
> Assistant's own Ingress dashboard (sidebar **Switchboard**) remains the
> authenticated management surface. See [SECURITY.md](SECURITY.md).

---

## 11. Health monitoring & Home Assistant sensors

Three independent monitors watch different things and publish Home Assistant
sensors. Sensors are always published; the **alert** toggles only control the
pop-up notifications.

### Link health (`link_health_*`)

Polls every phone's PJSIP registration and qualify round-trip latency **between
calls**, so a degrading link (especially the WiFi cordless) shows on a graph before
a call ever drops. Publishes:

- `sensor.switchboard_link_<ext>` — per phone; state is the RTT in ms, or
  `offline` / `unavailable`.
- `sensor.switchboard_link_health` — a rollup (worst RTT; counts of reachable /
  unreachable / offline; the down extensions).

It raises **one** notification on a mass outage — at least half the fleet *and* at
least 3 phones unreachable for 2 consecutive cycles — so a shared-gateway failure
(e.g. the GXW loses power) can't go unnoticed, and a recovery notice when it
clears.

### Per-call quality (`call_quality_alerts`)

After each call, scores the worse of the two audio directions from the RTP/RTCP
stats and publishes `sensor.switchboard_last_call` (an MES score, with loss,
jitter, RTT, codec, and duration as attributes). Notifies on a genuinely rough call
— low score, high loss, high latency (RTT over 400 ms), or **one-way audio**.

### Device health (`device_health_*`)

Covers the two blind spots the above can't see:

- The **WP826 cordless**'s own battery %, WiFi signal, and most-recent-call MOS
  (needs `cordless_password`) → `sensor.switchboard_cordless_health`. Flags
  CRITICAL when unreachable or the battery is dying, degraded on low battery / weak
  WiFi / a recent poor call.
- The **GXW gateway**'s port health, derived from the link-health rollup →
  `sensor.switchboard_gateway_health`. All wired ports down = the gateway likely
  lost power or its uplink.

Both use a 2-cycle hysteresis so a transient blip doesn't alert, and fire a
recovery notice when they return to normal.

| Sensor | What it tells you |
|--------|-------------------|
| `sensor.switchboard_link_<ext>` | Per-phone reachability + latency (ms) |
| `sensor.switchboard_link_health` | Fleet rollup (worst RTT, who's down) |
| `sensor.switchboard_last_call` | Last call's audio quality (MES) + details |
| `sensor.switchboard_cordless_health` | Cordless battery / WiFi / call quality |
| `sensor.switchboard_gateway_health` | GXW gateway port health |

> Pushed sensors are recreated after each poll and clear on a Home Assistant
> restart until the next push — that's expected.

---

## 12. How it's built

- **Asterisk 21 + PJSIP** is the only telephony engine; `chan_sip` is not used.
- On every start, **`switchboard-config`** regenerates
  `/etc/asterisk/{pjsip,extensions,confbridge,modules,pjsip_notify,rtp,manager,logger,features}.conf`
  from your add-on options (`/data/options.json`). The options are the source of
  truth; hand edits are overwritten.
- **Offline voice.** Speech-to-text is **whisper.cpp** (English `base.en` model,
  ~142 MB), kept resident in RAM by a loopback-only server with a per-call
  `whisper-cli` fallback. Text-to-speech is **espeak-ng**. Both run on-box under
  the unprivileged `asterisk` user — no cloud, no API key.
- The **Ingress dashboard** is a small single-worker FastAPI app that reads live
  state over the loopback-only Asterisk Manager (AMI) socket.
- **Services** run under s6-overlay: a one-shot config generator, then Asterisk,
  the web UI, the console (telnet + web), the resident recognizer, the wake-up
  scheduler, and the two health pollers. Each optional service idles when its
  feature is turned off.
- Built on the Home Assistant **Alpine 3.21** base image (a two-stage build that
  compiles whisper.cpp from source), with `fastapi` / `uvicorn` / `jinja2` and
  best-effort `espeak-ng`, `ffmpeg`, and the ConfBridge/Page modules.
- Runs under an **AppArmor** profile and **host networking** (required for SIP +
  RTP on your LAN). Architectures: `amd64`, `aarch64`.

---

## 13. Codecs — G.711 µ-law only, on purpose

Switchboard uses **G.711 µ-law only**, everywhere — every endpoint (rooms and the
trunk) is pinned to `disallow = all` / `allow = ulaw`. There is no codec option; it
isn't configurable, and no HD/Opus module is even installed. This is deliberate:

- **Antique analog handsets are narrowband by physics** — the carbon/electret
  element and the two-wire loop top out around 300–3400 Hz. Wrapping that in Opus
  or G.722 carries no extra fidelity; the analog transducer is the ceiling.
- **µ-law is the baseline every leg speaks** — the analog FXS ports, the cordless,
  and the PSTN trunk. Pinning one codec means **no call ever transcodes**: lowest
  latency, and dial tone / ringback / fax tones pass cleanly.

Because enforcement is server-side at the Asterisk endpoints, a phone's own codec
order doesn't matter — the negotiation can only ever land on µ-law. Just make sure
each device still *offers* G.711 µ-law (PCMU); a device configured to offer only a
non-µ-law codec would have no common codec and the call would fail. The dashboard
and operator console show the negotiated codec per active call, so you can confirm
it reads "µ-law".

---

## 14. Troubleshooting

| Symptom | Check |
|--------|-------|
| Room stays **Offline** | Gateway SIP Server = your HA host IP? FXS port enabled? Its Authenticate Password matches the room `secret` **exactly**? Reboot the gateway if a port raced the add-on's startup. |
| **Cannot reach Asterisk Manager** banner | The add-on is still starting, or Asterisk crashed — check the **Log** tab. |
| No / one-way audio | Host networking is required (set by the add-on) and `rtp_start`–`rtp_end` must not be blocked by a host firewall. NAT Traversal should be **No** on the LAN. |
| Rotary phone won't dial | Enable **Pulse Dialing** on that FXS port. |
| Calls drop after ~30 s | Usually a NAT/registration timer — set NAT Traversal = No on the LAN. |
| "No common codec" / call fails instantly | A device is offering only a non-µ-law codec. Make sure G.711 µ-law (PCMU) is enabled on it ([§13](#13-codecs--g711-µ-law-only-on-purpose)). |
| Voice features mis-hear you | Speak after the beep, in a quiet moment; the recognizer is narrowband. Add `operator_synonyms` for names it keeps missing. |
| Wake-up didn't ring | The room must be **registered and idle** at the set time; if busy/offline through the 10-minute grace window it's dropped and you get a persistent notification. |
| LAN announce (`/api/announce`) returns 403 | Set a non-empty `announce_token` and send it as the `X-Announce-Token` header. |
| The web terminal shows a `ValueError` at start once | Harmless — the console-web service self-recovers on the next s6 restart; the port comes up on `8100`. |

**Useful Asterisk CLI** (from the add-on's shell, if you have one):

```
asterisk -rx "pjsip show endpoints"
asterisk -rx "pjsip show contacts"
asterisk -rx "core show channels"
asterisk -rx "pjsip show registrations"   # trunk registration
```

---

## 15. Security

The security model, the toll-fraud threat model, the two **unauthenticated
LAN** services and their mitigations, secret handling, and the short list of things
**you** must configure are documented in **[SECURITY.md](SECURITY.md)**. The
essentials:

- The Ingress dashboard is reachable only from the Home Assistant Supervisor.
- The Asterisk Manager socket is loopback-only, with a fresh random secret every
  boot and no shell-command privilege.
- The trunk blocks international/premium prefixes and confines transfers to
  internal destinations.
- **Change the default room secrets** before your phones register.
- The telnet console and web terminal are **unauthenticated on your LAN by
  design** — bind them to `127.0.0.1` or disable them if the LAN isn't trusted.
