# Switchboard — operator guide

A self-hosted **Asterisk 20 + PJSIP** phone system for the analog phones in your
home, packaged as a Home Assistant add-on. Each FXS port on a **Grandstream
GXW4216 V2** gateway becomes a room extension; every phone can call every other
phone, reach a set of voice features that run on the box, and, optionally, an
outside line over a SIP trunk. Audio is **G.711 µ-law end to end**.

This page is the short guide for everyday use. Everything in detail, every
option and every log line, is in the
**[full reference](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/README.md)**.
The security model is in
**[SECURITY.md](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/SECURITY.md)**.

> **These phones cannot reach emergency services.** Dialling `911` (or `933`,
> the test number) from any house phone plays a spoken notice saying so. Keep a
> mobile phone for emergencies.

**Contents**

1. [Quick start](#1-quick-start)
2. [What to dial](#2-what-to-dial)
3. [Everyday tasks](#3-everyday-tasks)
4. [Options you are most likely to change](#4-options-you-are-most-likely-to-change)
5. [Troubleshooting](#5-troubleshooting)
6. [Reference](#6-reference)

---

## 1. Quick start

1. **Install** the add-on (already done if you are reading this from its
   Documentation tab).
2. Open the **Configuration** tab and define one entry under `rooms` per phone,
   each with a unique extension (2–6 digits), a friendly name, and a **secret**
   (the password the gateway port uses to register). **Replace the
   `change-me-…` placeholder secrets.**
3. **Start** the add-on and watch the **Log** tab: `switchboard-config` renders
   the configuration (look for `codecs: u-law only (no transcoding)` near the
   end, closing with `done: N room(s), trunk=…`), then Asterisk starts.
4. **Provision the gateway** so each FXS port registers: SIP server = the Home
   Assistant host's LAN IP, user ID and authenticate ID = the extension,
   password = the room's `secret`, PCMU (G.711 µ-law) enabled. Step by step:
   [Grandstream GXW4216 V2 provisioning](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/gxw4216-gateway.md).
5. Open the **Switchboard** panel in the Home Assistant sidebar. Each room flips
   to **Registered** as its port comes online.
6. Pick up a phone and dial another room's extension. Done.

The add-on options are the source of truth. Every start regenerates the whole
Asterisk configuration from them, so editing `/etc/asterisk/*.conf` by hand is
pointless: each file is overwritten.

Full details: [Quick start](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/quick-start.md).

---

## 2. What to dial

The feature codes below are the defaults. Each feature has a `*_ext` option to
move its code and a `*_enabled` option to switch it off; the operator on `0`
has only `operator.enabled`.

| Dial | What it does |
|-----:|--------------|
| a room's extension | Rings that room. |
| `0` | **Operator.** Say a room, an extension, or a feature ("wake me up", "lights", "what time is it", "weather", "directory", "announce", "page everyone"). |
| `41` | **Talking clock.** |
| `42` | **Wake-up call.** Set or cancel one by speaking the time. |
| `43` | **Lights.** A voice menu for your Home Assistant lights. |
| `44` | **Page all.** A house-wide intercom. |
| `45` | **Dial-a-status.** Live readings on request: power, weather, house. |
| `46` | **Announce.** Speak a message out of your Home Assistant speakers. |
| `47` | **Local voice assistant.** Ask Home Assistant anything. *Off by default* (`assistant_enabled`). |
| `411` | **Directory assistance.** Say a name, or "list". |
| `911`, `933` | Not carried. Answered with a spoken notice that emergency calls cannot be made from these phones. |
| `9` + number | Outside line, only when the trunk is enabled. With `direct_dial` on, dial `1` + the 10-digit number instead, with no prefix. International (`011`) and premium (`1-900`) numbers are blocked. |
| `##` / `*2` during a call | Blind / attended transfer from an analog phone, to an internal destination only. IP phones use their own Transfer button. |

**Choosing extensions.** An extension is any 2–6 digit number, unique per phone.
Keep `0` for the operator, keep clear of the feature codes, and, with a trunk in
prefix mode, of extensions that start with its prefix digit. The shipped
placeholder rooms use `101` and `102`; the `gateway_ports` default
(`11`–`18`) and `cordless_ext` default (`19`) assume wired ports on `11`–`18` and
the cordless on `19`, so set those two options to match your own scheme. A
feature code that collides with a room or another code is skipped at start, with
a log line.

**`41` pauses before it connects.** Because `411` starts with the same two
digits, the gateway waits out its no-key-entry timeout before sending `41`.
Pressing `#` sends it at once when the gateway's `#` dial key is enabled.

Full details: [Extensions & feature codes](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/extensions.md)
· [dial plan & send delay](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/gxw4216-gateway.md#74-dial-plan--the-send-delay).

---

## 3. Everyday tasks

All the voice features work the same way: a prompt plays, you speak one short
phrase after the beep, and the speech is recognised on the box. No keypress is
ever needed, so rotary phones work. Nothing connects a call or switches a light
on a low-confidence guess; it asks again instead.

### Wake-up calls

Dial `42` (or say "wake me up" to the operator) and, after the beep, say a time:
"seven thirty", "quarter past six", "nineteen thirty", "noon". The time is read
back, so you can say it again if it is wrong, or say "cancel" to remove a
pending wake-up. Each room holds **one** wake-up; setting a new one replaces it.
The dashboard (the **Wake** box and **Set** on a room card) and the console
(**W** to set, **X** to cancel) set and cancel them too.

At the set time the room rings for `wakeup_ring_seconds` (60 s) and speaks a
greeting and the time, plus any extras you configured: a scene, the weather, the
next calendar event. If nobody hears it, the room is rung a second time
`wakeup_retry_seconds` (90 s) after the first ring started, provided the phone
is registered and idle; if it still is not heard, a **critical** alert goes to
`wakeup_push_target`, which sounds through Do Not Disturb. Setting or cancelling
a wake-up from the ringing room's own phone counts as a snooze: no second ring
and no alert. A room that stays busy or offline through the 10-minute grace
window is skipped, with a Home Assistant notification.

Full details: [Wake-up calls](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/wake-ups-and-clock.md#wake-up-calls--dial-42).

### The talking clock

Dial `41` (or ask the operator "what time is it") for an old-style speaking
clock: "At the sound of the tone, the time will be …", then the time as a
24-hour hours-minutes-seconds readout and a tone, looping until you hang up.
The tone sounds **on** the second just announced. The time follows the
`timezone` option, or Home Assistant's timezone when that is blank.

Full details: [Talking clock](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/wake-ups-and-clock.md#talking-clock--dial-41).

### The operator — dial `0`

Say a **room name** ("Kitchen") or a **spoken extension** ("one four") to be
connected, or name a feature: "wake me up", "lights", "what time is it",
"weather" or "power", "directory" or "list the rooms", "announce", "page
everyone". A name it keeps missing can be given extra spoken aliases with
`operator_synonyms`. If it cannot understand you after two tries, it says so and
hangs up.

Lights, dial-a-status, announce and page act on the house, so the operator hands
them only to a room phone that dialled `0` itself; a caller transferred in from
outside can still be connected to a room, use the directory, hear the clock or
set a wake-up. Dialling `0` also clears the calling room's own message-waiting
indicator (below).

Full details: [The voice operator](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/voice-operator.md#operator--dial-0).

### Directory, lights, status and the voice assistant

- **Directory (`411`).** Say a room name to be connected, or "list" to hear
  every room and its extension. It speaks the room and extension before
  connecting, which is your cue to hang up if it guessed wrong.
- **Lights (`43`).** A guided menu: it asks for a room, then a light, says
  whether it is on or off, and asks you to say "turn on", "turn off" or
  "cancel". Say "list" at any step to hear the choices.
- **Dial-a-status (`45`).** Say "power" (or battery, solar, grid), "weather"
  or "house". It asks "anything else?" after each answer; say "goodbye" to
  leave. The power readings come from the `status_power_*` options.
- **Voice assistant (`47`, off by default).** Set `assistant_enabled: true`,
  then say a whole command or question in plain language. The words go to Home
  Assistant's built-in conversation agent and the reply is spoken back; say
  "no" or "goodbye" to finish. Speech recognition and the voice both run on the
  box. `assistant_transcripts` (on by default) decides whether the logs keep
  the words people say to the voice features, or only their length.

Full details: [Directory](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/voice-operator.md#directory-assistance--dial-411)
· [Lights](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/voice-operator.md#home-automation-voice-menu--dial-43-or-say-lights-to-the-operator)
· [Dial-a-status](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/voice-operator.md#dial-a-status--dial-45)
· [Voice assistant](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/voice-operator.md#local-voice-assistant--dial-47).

### Paging

Dial `44` from a room phone to ring **every room phone at once** into a
house-wide intercom. It is duplex: anyone who picks up can talk back. The
**Page all** button on the dashboard and **P** in the console (confirm with
**Y**) page the house the same way.

Full details: [Page all](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/paging-and-announcements.md#page-all--dial-44).

### Announcements

- **Phone to speakers (`46`).** Dial `46`, speak your message after the beep,
  and it plays out of the Home Assistant speakers listed in `announce_players`,
  with a station chime before and after. The message is transcribed and
  re-spoken on the box.
  While `announce_players` is empty, dialling `46` answers "No speakers are set
  up for announcements" and hangs up.
- **Home Assistant to a phone.** An automation (or another add-on) can speak
  onto a room handset with
  `POST http://<ha-host>:8099/api/announce/<ext>`, body `{"text": "Dinner is
  ready"}` or `{"url": "http://…/clip.wav"}`, and header `X-Announce-Token`. From
  the LAN this needs a non-empty `announce_token`; while it is blank, only the
  Supervisor can call it. The WP826 cordless answers hands-free; an analog phone
  rings like a call.
- A clip whose audio never played is sent again, up to `announce_retry_attempts`
  times (2), once the phone reports itself registered and idle.
  An announcement nobody answers is retried too, so set
  `announce_retry_attempts` to `0` or `1` if that is too many rings for your
  house. The same audio sent to the same phone within five minutes is not
  played twice.

Full details: [Announcements](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/paging-and-announcements.md#announcements--dial-46-phone--home-assistant-speakers)
· [Announce onto a handset](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/paging-and-announcements.md#announce-onto-a-handset--post-apiannounceext-home-assistant--phone).

### The dashboard

Open **Switchboard** in the Home Assistant sidebar. Each room card shows whether
the phone is **Registered** or **Offline**, who it is talking to, and its idle
round-trip time, with buttons to **Test ring**, **Connect** (then pick a second
room to patch the two together), **Hang up**, **Transfer**, set or clear
**Message**-waiting, and a **Wake** time box with **Set** and **Cancel**. Below
the rooms are the active calls, the pending wake-ups, and a **Lights** panel:
every Home Assistant light the add-on can see, grouped by area, with on/off
toggles. **Console** and **Page all** sit at the top.

Full details: [Quick start](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/quick-start.md)
· [How it's built](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/how-its-built.md).

### The operator console

A full-screen switchboard you drive by keystroke: every room's status, any live
call, the outside line's registration, and the same actions as the dashboard.
There are three ways in:

- **In the sidebar (recommended).** Press **Console** on the dashboard, or open
  `/console/` under the panel's URL; the address must end in a slash. Home
  Assistant's own login is the only login, and it works in the mobile app.
- **SSH.** `ssh -p 2300 <user>@<ha-host>`, signing in as a `console_users`
  entry: its username, then its password at the prompt. While `console_users`
  has no valid entry, **every login is refused**, and the Log tab says so. On
  first connection, compare the host-key fingerprint your client shows with the
  one the add-on log prints at every start. A fresh install listens on every
  interface (`console_bind: 0.0.0.0`); an upgraded one keeps the bind it had
  saved, which may be `127.0.0.1` (the Home Assistant host only). The start-up
  line names the address actually in use. Recent OpenSSH clients warn that the
  connection does not use a post-quantum key exchange; that is expected, and
  `-o WarnWeakCrypto=no-pq-kex` silences it.
- **Standalone web terminal (port 8100).** Bound to `127.0.0.1` by default, so it
  answers only on the Home Assistant host itself; it is a fallback for the
  sidebar console.

Keys: **↑↓** / **j k** move · **R** ring · **C** connect · **H** hang up ·
**T** transfer · **W** set wake-up · **X** cancel wake-up · **M**
message-waiting · **P** page all · **L** lights · **?** help · **Q** /
**Ctrl-C** quit.

Full details: [The operator console](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/operator-console.md).

### Message-waiting

A room's message-waiting indicator means "call the operator". There is no
voicemail and no missed-call detection: the indicator is set by you, with
**Message** on the dashboard's room card or **M** in the console, never by a
missed call. On an analog phone it produces a **stutter dial tone** once the
gateway's "Send Stutter Dialtone for MWI" setting (the label varies by firmware)
is enabled; without it, the indicator still shows on the dashboard. Clear it
with **Clear** on the card or **M** again, or simply dial `0` from that room
(while `mwi_enabled` is on). Indicators survive an add-on restart.

Full details: [The stutter-tone setting](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/gxw4216-gateway.md#72-configure-each-fxs-port)
· [`mwi_enabled`](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/configuration.md#extra-feature-codes).

### Notifications and health sensors

Three monitors publish Home Assistant sensors and raise notifications: **link
health** (each phone's registration and round-trip time, and the outside line),
**call quality** (a call whose audio was poor) and **device health** (the WP826
cordless's battery and Wi-Fi, and the gateway). The `*_alerts` options switch
only the notifications; the sensors keep publishing. Turning
`link_health_enabled` off also silences gateway health, the trunk sensor and the
cordless half of device health, so leave it on unless you mean all of that.

Full details: [Health monitoring & sensors](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/health-monitoring.md).

---

## 4. Options you are most likely to change

Every option has a label and inline help in the **Configuration** tab. Changing
one and restarting the add-on regenerates the configuration. This table covers
the options people change most often; all of them, with their limits, are in the
[configuration reference](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/configuration.md).

| Option | Default | Meaning |
|--------|---------|---------|
| `rooms` | 2 placeholders | One entry per phone: `ext` (2–6 digits, unique), `name`, and `secret`, the SIP password its gateway port uses. Replace the `change-me-…` secrets. A room with an invalid `ext` or `secret` is skipped at start, with a log line. |
| `gateway_ports` | `11,12,13,14,15,16,17,18` | The extensions on the wired gateway ports, used to derive gateway health. |
| `cordless_ext` | `19` | The extension the WP826 cordless registers as. |
| `cordless_password` | `""` | The WP826's web-admin password, needed for its battery and Wi-Fi checks. |
| `timezone` | `""` | Blank follows Home Assistant's timezone; set an IANA name (such as `America/Phoenix`) only to override it. |
| `operator_synonyms` | `[]` | Extra spoken names for a room: `ext` plus a list of `phrases`. |
| `wakeup_ring_seconds` | `60` | How long a wake-up rings (10–600 s). |
| `wakeup_retry_seconds` | `90` | When an unheard wake-up is rung again, counted from the first ring. Raised to at least the ring time + 20 s. |
| `wakeup_push_target` | `mobile_app_iphone` | Notify service (without `notify.`) for the critical alert about an undelivered wake-up. Empty sends a Home Assistant notification card instead. |
| `wakeup_scene` | `""` | A `scene.*` activated when a wake-up is answered. `wakeup_scenes` sets one per room. |
| `wakeup_weather` | `true` | Speak a short local forecast during the wake-up. |
| `wakeup_calendar` | `""` | A `calendar.*` entity whose next event in the coming 18 hours is read out. |
| `announce_players` | `[]` | The `media_player` entities dial-`46` announcements play on. Required for `46` to work. |
| `announce_token` | `""` | Shared secret for `POST /api/announce` from the LAN. Blank: only the Supervisor can call it. |
| `announce_retry_attempts` | `2` | How many times a handset announcement that never played is sent again (0–3; `0` turns it off). |
| `assistant_enabled` | `false` | The local voice assistant on `47`. |
| `console_users` | `[]` | Accounts (`username`, `password`) for the SSH console and the standalone web terminal. SSH refuses every login while it is empty. |
| `console_bind` | `0.0.0.0` | The interface the SSH console listens on; `127.0.0.1` keeps it on the Home Assistant host. An upgraded install keeps the value it had saved. |
| `console_web_bind` | `127.0.0.1` | The interface the standalone web terminal (port 8100) listens on. |
| `log_level` | `info` | How much of Asterisk reaches the Log tab. `info` includes the dialplan trace, dialled numbers among it; `notice`, `warning`, `error` and `critical` leave the trace out; `debug` adds trunk detail for diagnosis. |
| `trunk` | off (`enabled: false`) | The outside line. Leave it off for a room-to-room system. |

Full details: [Configuration reference](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/configuration.md)
· [Adding an outside line](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/sip-trunk.md).

---

## 5. Troubleshooting

The ten most common problems, most common first.

| Symptom | Check |
|---------|-------|
| A room stays **Offline** | On the gateway: SIP Server = the Home Assistant host's LAN IP, the FXS port enabled, its Authenticate Password exactly the room's `secret`. Reboot the gateway if a port raced the add-on's start. The Log tab names any room skipped for a bad `ext` or `secret`. |
| No audio, or one-way audio | Host networking is required (the add-on sets it), and `rtp_start`–`rtp_end` must not be blocked by a host firewall. NAT Traversal should be **No** on the gateway. |
| "No common codec", or a call fails instantly | A device is offering only a codec other than µ-law. Enable G.711 µ-law (PCMU) on it. |
| A rotary phone will not dial | Enable **Pulse Dialing** on that FXS port. |
| The voice features mis-hear you | Speak after the beep, in a quiet moment; the line is narrowband. Add `operator_synonyms` for names it keeps missing. |
| A wake-up did not ring | The room must be **registered and idle** at the set time. A room busy or offline through the 10-minute grace window is skipped, with a persistent notification. If Switchboard itself was down at the set time, the wake-up still rings if it is back within that window; otherwise the miss is recorded (`missed`) and the notification says so. |
| Every wired phone went **Offline** for about a minute and came back on its own | Usually the gateway restarting, which takes about a minute on a GXW4216 V2, often at its own scheduled automatic reboot. The full page explains how to tell from the logs. |
| **Cannot reach Asterisk Manager** banner | The add-on is still starting, or Asterisk crashed. Check the **Log** tab. |
| A Home Assistant feature over the phone (lights, scenes, announcements, the status menu, the assistant) says it is unavailable, and the Log tab shows `[ha-broker] refused` | On a call transferred in from outside, or a `*2` consult, those features are refused by design: dial `0` or the feature code directly from a room phone. Otherwise the `refused` line names the request and the rule it failed; report it with that line. |
| Calls drop after about 30 s | Usually a NAT or registration timer. Set NAT Traversal = **No** on the LAN. |

Full details: [Troubleshooting](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/troubleshooting.md).

---

## 6. Reference

The full reference, one page per topic:

- [Reference index](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/README.md)
- [Quick start](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/quick-start.md)
- [Configuration reference](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/configuration.md)
- [Extensions & feature codes](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/extensions.md)
- [The voice operator & directory assistance](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/voice-operator.md)
- [Wake-up calls & the talking clock](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/wake-ups-and-clock.md)
- [Paging & announcements](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/paging-and-announcements.md)
- [Grandstream GXW4216 V2 provisioning](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/gxw4216-gateway.md)
- [The WP826 WiFi cordless (optional)](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/wp826-cordless.md)
- [Adding an outside line (SIP trunk)](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/sip-trunk.md)
- [The operator console (SSH + browser)](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/operator-console.md)
- [Health monitoring & Home Assistant sensors](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/health-monitoring.md)
- [How it's built](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/how-its-built.md)
- [Codecs — G.711 µ-law only, on purpose](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/codecs.md)
- [Troubleshooting](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/troubleshooting.md)
- [Security](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/security.md), and the full model in [SECURITY.md](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/SECURITY.md)
- [Reproducing on new hardware](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/docs/reference/reproducing.md)
