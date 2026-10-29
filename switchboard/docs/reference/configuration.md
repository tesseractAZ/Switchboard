# Configuration reference

Every option below appears in the **Configuration** tab with a friendly label and
inline help. Defaults are shown. A value is *optional* unless noted; leaving it at
its default is fine.

## Core

| Option | Default | Notes |
|--------|---------|-------|
| `log_level` | `info` | `trace \| debug \| info \| notice \| warning \| error \| critical` — how much of Asterisk reaches the add-on's **Log** tab. Only **three** of the seven behave differently. `info` shows notices, warnings and errors plus Asterisk's verbose trace: the dialplan trace, every dialled and calling number included. `debug` and `trace` are identical to each other: they add Asterisk's debug class, which (the add-on leaves Asterisk's debug level at 0) holds only the messages Asterisk logs at debug unconditionally — each outbound trunk REGISTER attempt among them, naming the trunk's SIP address. `notice`, `warning`, `error` and `critical` are identical too: notices, warnings and errors, **without** the verbose trace — the only other lines are a few of Asterisk's own start-up and shutdown messages, which it prints at any level. Since v0.107.0 those four start Asterisk without `-v`, which is what keeps the trace off the Log tab, and the add-on's own per-call lines (`[rtpqos]`, `Operator dial`) are written at verbosity 1 so they leave with it; before v0.107.0 Asterisk always started with `-vvv` and the trace stayed there at every level. This is not a severity filter, so picking `error` does *not* silence notices. Drop to `debug` to diagnose the trunk, then set it back. The durable `/data/state/asterisk.log` does not follow this setting: it is fixed at `notice,warning,error,verbose(3)` and holds the same content at every level — endpoint reachability (`Endpoint <n> is now Unreachable`, verbosity 2), the `Contact <n>/… is now Unreachable` and `Added`/`Removed contact` lines (verbosity 3), and the whole dialplan trace, numbers included (see SECURITY.md). The `(3)` filters nothing; it keeps Asterisk producing verbosity-3 lines when the console runs without `-v`. The file is private to the add-on, though included in its backups, and trimmed at boot, before Asterisk opens it, keeping the newest half whenever it has grown past 8 MB. The Log tab keeps about two days across a host reboot and is readable by Home Assistant administrators. Since v0.107.0 every line in Asterisk's two log files, and every notice, warning and error in the Log tab, is stamped with the full date, milliseconds and UTC offset — `[2026-10-01 01:00:17.289 -0700]`. |
| `rtp_start` | `10000` | First UDP port for live call audio (RTP). Must be below `rtp_end`. |
| `rtp_end` | `10200` | Last RTP port. The default 200-port window is far more than a home needs (~2 ports per call). |

## Rooms (the phones)

| Option | Default | Notes |
|--------|---------|-------|
| `rooms` | 2 placeholders | A list; one entry per handset. Each has `ext` (2–6 digits, unique), `name` (shown in the directory/operator/dashboard), and `secret` (the SIP password — **change it from the default**). The shipped defaults are `101 Kitchen` / `102 Living Room` with `change-me-…` secrets. An entry is skipped at start, with a log line, when its `ext` is all zeros or repeats an earlier entry, or when its `secret` is empty, contains `;` or control characters, or has leading/trailing whitespace (Asterisk would truncate it). A skipped room does not register. |

## Voice operator & speech

| Option | Default | Notes |
|--------|---------|-------|
| `operator.enabled` | `true` | The voice operator on dial `0`. |
| `operator_synonyms` | `[]` | Extra spoken aliases mapped to a room, for accents/nicknames. Each entry has `ext` and `phrases` (e.g. "lounge" → the Living Room). |
| `stt_resident` | `true` | Keep the speech-to-text model resident in RAM for instant response. Turn off on a very memory-constrained host; recognition then loads the model on demand per call. |

## Call-quality & health monitoring

| Option | Default | Notes |
|--------|---------|-------|
| `call_quality_alerts` | `true` | Notify when a call's audio is poor (low MOS, high loss, high round-trip time, one-way audio), or when a conversation's receive-jitter peak exceeds 60 ms (v0.116.0). Every leg is measured and written to the ledger. Legs the PBX originates to play something at a phone (wake-up delivery, paging, announcements) skip the one-way-audio check and never update the last-call sensor, but a poor connection under one of them alerts like any other leg (v0.104.0). A clip that was cut short stays quiet, except an *undelivered* wake-up, which alerts (v0.78.0). See [Per-call quality](health-monitoring.md#per-call-quality-call_quality_alerts). |
| `cordless_battery_crit_pct` | `15` | Battery % (while discharging) that flags the cordless CRITICAL. Range 1–100. |
| `cordless_battery_warn_pct` | `30` | Battery % that flags it low/degraded. Should be higher than the critical %. |
| `cordless_cert_sha256` | `""` | SHA-256 fingerprint of the WP826's TLS certificate. When set, the monitor verifies the handset presents exactly that certificate **before** sending the admin password. Blank skips verification. See [*Pinning the cordless certificate*](wp826-cordless.md#pinning-the-cordless-certificate). |
| `cordless_ext` | `19` | The extension the cordless registers as. When set, the device-health monitor takes the cordless's **current** IP from its live SIP registration and follows it automatically if DHCP moves the phone — so a changed lease no longer blinds battery/Wi-Fi/MOS monitoring. Blank = use `cordless_ip` only. |
| `cordless_ip` | `""` | Fallback LAN address of the WP826 cordless (e.g. `192.168.1.71`). Only used if `cordless_ext` is blank or the cordless isn't registered — otherwise the monitor auto-follows the phone's live IP (see below). |
| `cordless_password` | `""` | WP826 web-admin password; required for the deep battery/WiFi/MOS checks. Masked, never shown back. Without it the monitor still tracks reachability. |
| `cordless_wifi_min_signal` | `2` | Lowest acceptable WiFi bars (0–5) before flagging a weak link. |
| `device_health_alerts` | `true` | Notify when the cordless or gateway becomes unhealthy (and again on recovery). |
| `device_health_enabled` | `true` | Watch the WP826 cordless (battery/WiFi/per-call MOS) and derive gateway health. Needs `cordless_password` for the deep checks. |
| `device_health_interval` | `120` | Seconds between device-health polls. Range 30–86400. |
| `gateway_ports` | `11,12,13,14,15,16,17,18` | Comma-separated extensions served by the wired GXW FXS ports, used to derive gateway health. |
| `link_health_alerts` | `true` | Notify when many phones lose registration at once (a shared-gateway outage), including a mass drop that healed between polls, and when the outside line stops or resumes registering. |
| `link_health_enabled` | `true` | Poll every phone's registration + round-trip latency (RTT) between calls, published to sensors. |
| `link_health_interval` | `300` | Seconds between link-health polls. Range 30–86400. |

## Announcements

| Option | Default | Notes |
|--------|---------|-------|
| `announce_enabled` | `true` | The announce feature on dial `46` (phone → Home Assistant speakers). It does not affect `POST /api/announce` (Home Assistant → handset, see [Announce onto a handset](paging-and-announcements.md#announce-onto-a-handset--post-apiannounceext-home-assistant--phone)), which `announce_token` governs. |
| `announce_ext` | `46` | The extension to dial to record an announcement. 2–6 digits. |
| `announce_players` | `[]` | Home Assistant `media_player` entity IDs an announcement plays on, e.g. `media_player.kitchen_speaker`. One per line. **Required for the feature to work:** while this list is empty, dialling the announce code answers "No speakers are set up for announcements" and hangs up. |
| `announce_retry_attempts` | `2` | How many times a handset announcement (`POST /api/announce`) whose audio never played is **sent again** (see [Announce onto a handset](paging-and-announcements.md#announce-onto-a-handset--post-apiannounceext-home-assistant--phone)), once the phone reports itself registered and idle. Range 0–3; `0` turns retries off and leaves only the record. An announcement that did play is never replayed, whatever this is set to. |
| `announce_token` | `""` | Optional shared secret required on the `/api/announce` HTTP endpoint (used to speak alerts onto a handset from Home Assistant / another add-on). **Blank disables LAN announce** — only the Supervisor can call it. Masked. |

## Operator console

| Option | Default | Notes |
|--------|---------|-------|
| `console_bind` | `0.0.0.0` | Interface the SSH console listens on. Every login needs a `console_users` password; `127.0.0.1` restricts it to the Home Assistant host. Only SSH uses it. An upgraded install keeps the bind it had saved, and the default from 0.94.0 to 0.113.0 was `127.0.0.1`: set `0.0.0.0` to reach SSH from the LAN. |
| `console_enabled` | `true` | The SSH operator console (ring/connect/hang up; see [The operator console](operator-console.md)). Password login only, against `console_users`; while that list has no valid entry, **every login is refused**. Turning it off stops SSH only: the sidebar console and the web terminal keep working. With this and `console_web_enabled` both off, the board itself stops and the sidebar console shows "unavailable". |
| `console_port` | `2300` | TCP port of the SSH console. The sidebar console and the web terminal do not use it. |
| `console_users` | `[]` | Sign-in accounts for the **SSH console** and the **web terminal** — each entry has `username` and `password` (masked); a row missing either is ignored. SSH accepts only these accounts and refuses every login while the list is empty. The web terminal with the list empty has no login (the historical open behavior); when any user is configured, its page **and its WebSocket** require a signed-in session. Repeated wrong attempts from one address are throttled (5 per 5 minutes, counted separately by SSH and the web terminal). The sidebar console needs none of this. |
| `console_web_bind` | `127.0.0.1` | Interface the web terminal listens on. Blank also means `127.0.0.1`; since 0.114.0 it no longer follows `console_bind`. The browser console is served through the Home Assistant sidebar instead ([The operator console](operator-console.md)), so this terminal does not need to be on your network. |
| `console_web_enabled` | `true` | Browser version of the console (xterm.js), bound to loopback by default. Its sign-in is off until `console_users` has an entry, so configure one before moving the bind. |
| `console_web_port` | `8100` | TCP port for the web terminal. |

## Time, clock & wake-up

| Option | Default | Notes |
|--------|---------|-------|
| `clock_enabled` / `clock_ext` | `true` / `41` | The talking clock and its dial code (2–6 digits). |
| `timezone` | `""` | Blank = auto-detect the Home Assistant timezone. Set an IANA name (e.g. `America/Phoenix`) only to override. |
| `wakeup_calendar` | `""` | Optional HA `calendar.*` entity whose next event is read out. |
| `wakeup_enabled` / `wakeup_ext` | `true` / `42` | Wake-up calls and the dial code. Off also stops the announcement verdicts and automatic retry ([Announce onto a handset](paging-and-announcements.md#announce-onto-a-handset--post-apiannounceext-home-assistant--phone)), which run in the same scheduler, and identical repeats are then not held back. |
| `wakeup_push_target` | `mobile_app_iphone` | Notify service (no `notify.` prefix) an undelivered wake-up escalates to, as a critical alert that sounds through Do Not Disturb. Empty falls back to a Home Assistant notification card. |
| `wakeup_retry_seconds` | `90` | Seconds after a wake-up starts ringing before an unanswered call is rung a **second** time. At least `wakeup_ring_seconds` + 20 (one scheduler poll); a shorter value is raised to that and a WARNING naming both values is logged at every start. With a ring above 580 s every allowed retry is raised, which is expected. |
| `wakeup_ring_seconds` | `60` | How long a wake-up rings before giving up. Range 10–600. |
| `wakeup_scene` | `""` | Optional HA `scene.*` entity activated when a wake-up fires. |
| `wakeup_scenes` | `[]` | **Per-room** wake-up scenes. Each entry has `ext` (the room extension) and `scene` (a scene entity id). The room's own scene fires when that room's wake-up rings; a room with no entry falls back to `wakeup_scene` above, so adding per-room scenes never drops the whole-house behavior. Entries naming an extension that is not a configured room are logged and ignored. |
| `wakeup_weather` | `true` | Speak a short local weather summary during the wake-up call. |

## Extra feature codes

| Option | Default | Notes |
|--------|---------|-------|
| `assistant_enabled` / `assistant_ext` | **`false`** / `47` | Local voice assistant — talk to Home Assistant's built-in conversation agent from a phone. Off by default; see [Local voice assistant](voice-operator.md#local-voice-assistant--dial-47). |
| `assistant_transcripts` | `true` | Keep the words people say to the voice features. On: the assistant's private diagnostic log (`/data/state/assistant.jsonl`, last 400 turns, never copied to the shared folder) records what was said and replied, and the add-on's log — shown by the Supervisor to Home Assistant administrators, kept about two days across a host reboot — quotes what the recognizer heard at the operator, wake-up, home-automation, status, directory and assistant prompts. Off: both still record every timing, outcome and failure reason, with each utterance replaced by its length. If the setting cannot be read, the add-on's log leaves the words out. |
| `automation_enabled` / `automation_ext` | `true` / `43` | Home-automation voice menu (control HA lights) and its dial code. |
| `directory_enabled` / `directory_ext` | `true` / `411` | Voice directory (like 411) and its dial code. |
| `mwi_enabled` | `true` | **Dial-0 auto-clear only.** When on, a room that dials `0` has its own message-waiting indicator cleared. It does **not** switch the indicator feature off: the dashboard button, the console's `M` key, the NOTIFY templates and the boot-time replay all stay live either way. There is no voicemail and no missed-call detection in this system — the indicator is set by you (or another integration), never by a missed call. |
| `page_enabled` / `page_ext` | `true` / `44` | All-call paging / intercom and its dial code. |
| `status_enabled` / `status_ext` | `true` / `45` | Dial-a-status voice menu (live HA readings) and its dial code. |
| `status_power_battery` | `""` | `sensor` giving battery charge as a **percentage** — the menu says "the home battery is at N percent", so a sensor reporting kWh will be read out as if it were a percent. |
| `status_power_grid` | `""` | Entity whose on/off state says whether utility power is present, spoken by the **power** branch of dial-`45`. Accepts `input_boolean`, `binary_sensor`, `sensor` or `switch`. |
| `status_power_runway` | `""` | `sensor` giving hours of runway remaining. |
| `status_power_solar` | `""` | `sensor` giving the solar share of current load as a **percentage** — spoken as "solar is covering N percent of the load". |

## Outside line (SIP trunk)

`trunk` is a group; leave `trunk.enabled: false` (the default) for a room-to-room
system. When you enable it, see [Adding an outside line (SIP trunk)](sip-trunk.md).

| Sub-field | Default | Notes |
|-----------|---------|-------|
| `dial_prefix` | `9` | Digit(s) to dial first to reach an outside line (prefix mode). Ignored when `direct_dial` is on. |
| `direct_dial` | `false` | Turn **on** to dial phone numbers with **no outside-line prefix** — dial **`1` + the 10-digit** US/Canada number (`16025551234`), like a cell phone. Extensions and feature codes still ring internally. A **leading `1` is required**: a bare 10-digit number is not routed. This is what keeps the feature codes (`41`–`47`, `411`) and any extension starting `2`–`9` from looking like the start of a phone number, which would stall their dialing on analog phones. Extensions starting `12` through `19` still overlap the outside-number pattern, and `41` waits for its own reason (the `411` overlap); see [Dial plan & the send delay](gxw4216-gateway.md#74-dial-plan--the-send-delay). `011` international and `1-900` premium stay blocked. **911 is not routed** (no E911). Overrides `dial_prefix`. |
| `enabled` | `false` | Turn the outside line on. **Required** when the group is present. |
| `from_domain` | `""` | Outbound `From` domain (defaults to `provider_host`). |
| `from_user` | `""` | Outbound `From` user (defaults to `username`). |
| `inbound_ext` | `""` | Which extension(s) an incoming outside call rings: one extension or a comma-separated list. Blank rings every room. An entry that is not a configured room is dropped (logged at start) and the call rings the remaining entries. **Fails open:** when no entry is valid, the call rings the whole house. Check the start-up log after changing it. |
| `outbound_caller_id` | `""` | Number to present on outbound calls (digits/`+` only). |
| `port` | `5060` | Provider SIP port. |
| `provider_host` | `""` | Your SIP provider's host, e.g. `losangeles.voip.ms`. |
| `registns` | `true` | Register to the provider (most trunks need this). |
| `secret` | `""` | Trunk auth password. Must not contain `;`, control characters or leading/trailing whitespace (Asterisk would truncate it). Such a secret skips the whole trunk at start, with a log line. |
| `username` | `""` | Trunk auth username / sub-account. |

---

## The options overlay (advanced)

Everything above is normally edited in the add-on's **Configuration** tab. For
automation (or when the Supervisor's options API is unreachable), the add-on
also honors an **overlay file** the administrator can place at
`/addon_configs/<slug>/options-overlay.json` (visible to the add-on as
`/config/options-overlay.json`): a JSON object that is **deep-merged over the
saved options at every start**. Dictionaries merge recursively (so
`{"trunk": {"inbound_ext": "11,19"}}` refines the trunk without touching its
credentials); scalars and lists replace.

Because the overlay bypasses the add-on's schema, it is **type-checked against
the saved options**: an entry whose JSON type differs from the value it would
replace (a string where the trunk object belongs, `8100.5` for a port, `true`
for a number) is rejected with a warning and the saved value is kept. Any other
overlay problem — unparseable JSON, a non-object root — is logged and the
overlay is ignored wholesale. The overlay can never fail the start: the whole
add-on, Asterisk included, hangs off that step.

The boot log lists every key path whose value actually **changes** (a no-op
restatement is reported as changing nothing), so an override the Configuration
tab can't show you is still visible. Remove the file and restart to return to
the saved options.

Services read the merged result for **values**, not `options.json` — the config
renderer, the dashboard, the operator console, and the health monitors all read
the post-overlay snapshot (via `switchboard-opt`), so they share one effective
view.

**The overlay cannot turn a service on or off.** Each s6 `run` script decides
whether to start or idle by reading its enable flag with `bashio::config`, which
parses the saved `options.json` directly and never sees the overlay. That is
deliberate — `bashio::config` can momentarily return an empty string during a
config reload, and the run scripts' literal `= "false"` comparison is what stops
a blank read from permanently idling an enabled service (s6 treats an idle
process as "started" and never restarts it) — but it means these keys are read
from the **saved** options even when the overlay names them:

- `console_enabled`, `console_web_enabled` (SSH console, web terminal; the board
  behind them idles only when both are `false`),
- `link_health_enabled`, `device_health_enabled` (the two pollers),
- `wakeup_enabled` (the wake-up scheduler, and the web UI's retry gate),
- `stt_resident` and the seven speech-feature flags the resident recognizer gates
  its RAM on (`operator.enabled`, `wakeup_enabled`, `automation_enabled`,
  `status_enabled`, `announce_enabled`, `directory_enabled`,
  `assistant_enabled`),
- `device_health_alerts`.

Note the asymmetry: an overlay `{"link_health_enabled": false}` still leaves the
poller running, while an overlay `{"link_health_interval": 60}` does take effect.
The boot log reports the first one as an applied override anyway — it lists what
the **merge** changed, not what each consumer honored. Turn a service off in the
Configuration tab, not in the overlay.
