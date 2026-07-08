# Changelog

## 0.13.0

Performance + log/SD-card hygiene, from a deep multi-agent audit. Headline: the
operator console no longer hammers Asterisk's manager 24/7.

- **The console AMI poller now runs only while a client is connected.** It used
  to log into the manager, read status, and log off **every 3 seconds around the
  clock even with nobody watching** — ~28,800 login/logoff cycles a day. That
  churn filled the Asterisk log ring buffer (real call/error events scrolled out
  within ~2 minutes) and was constant SD-card write pressure on a Pi already
  prone to card wear. A `ClientGate` (threading.Condition, no lost-wakeup race)
  parks the poller with zero AMI traffic when no telnet/ttyd client is attached,
  and wakes it on connect so the first frame is fresh. Idle console → zero churn.
- **`logger.conf` writes one channel, not two.** The redundant `messages =>` file
  duplicated every log line to the SD card (unrotated, read by nobody — the
  console stream already reaches the add-on log via journald). Dropped.
- **`cdr_csv` no longer loads.** It appended `Master.csv` to the SD synchronously
  per call for records nothing reads (VoIP.ms keeps the authoritative CDR).
- **`/api/status` has a short-TTL single-flight cache.** An open dashboard
  refreshes every 4s and the transfer pre-check reads the same data; they (and
  extra browser tabs) now coalesce onto one AMI session instead of each opening
  their own. Errors propagate uncached, so callers keep their fail-open handling.
- **Config-generator correctness:** a room/trunk secret containing `;` or
  leading/trailing whitespace is now rejected loudly (Asterisk would silently
  truncate it and break registration); a **disabled** clock no longer falsely
  blocks a wake-up code at the same ext; trunk `from_user`/`from_domain` are
  charset-validated (falling back to the validated username/host) like the
  other trunk fields.

## 0.12.7

Talking clock (dial 41): fuller military phrasing.

- **"&lt;hour&gt; &lt;minute&gt; hours, and &lt;n&gt; seconds", with "hundred" on the hour.**
  14:32:05 → "fourteen thirty-two hours, and five seconds"; 14:00:05 → "fourteen
  **hundred** hours, and five seconds"; 09:05:30 → "oh nine oh five hours, and
  thirty seconds". A :00 minute is spoken "hundred"; the seconds are now a plain
  cardinal ("five", "thirty", "zero") set off by the "hours, and" prompt so they
  no longer blend into the hour/minute groups.
- The words that don't exist in Asterisk's core-sounds ("hours"/"and"/"seconds")
  are two short espeak prompts (`sw-hours-and`, `sw-seconds`) in the same voice
  as the other synthesized prompts; the number digits keep the professional
  recorded voice. `clock_speak` and its 400-case test sweep were updated to the
  new phrasing.

## 0.12.6

Fancier talking clock (dial 41): 24-hour time, with seconds, on a loop.

- **"At the sound of the tone, the time will be &lt;HH MM SS&gt;" &lt;tone&gt;, repeating
  until you hang up.** The clock now speaks 24-hour ("military") time including
  seconds and loops, instead of announcing the 12-hour time once and hanging up.
- **Clean military readout, no `SayUnixTime` quirks.** Each field is spoken as a
  natural two-digit group — 14:32:05 → "fourteen, thirty-two, oh five"; 09:05:00
  → "oh nine, oh five, oh oh". The old `SayUnixTime` 24-hour format was avoided
  because its minute specifier says "o'clock" for :00 and its seconds support is
  version-dependent. The readout is a small AGI (`switchboard-clock.agi`) over a
  pure, unit-tested sequencer (`clock_speak.time_actions`) that emits only the
  digit sound files Asterisk ships (there are no "hours"/"minutes"/"seconds" word
  files), so the exact spoken sequence for any time is testable without a phone.
- The loop lives in the dialplan, so hanging up simply ends the call. New espeak
  preamble prompt `sw-at-sound-tone` (same voice/format as the existing prompts).

## 0.12.5

Dial-43 lights: saying "list" now actually lists the rooms/lights.

- **Command words are now in the recognizer's bias.** Whisper was primed with
  only the area/light names, so a spoken "list" had no prior and came back as a
  sound-alike ('Left', 'Lift' — observed live) that matched nothing and burned
  the caller's retries. Every stage's bias now includes its command words
  ("list", "cancel"; the action stage — which had **no** bias — gets
  "turn on turn off cancel").
- **Fuzzy intent fallback fixed for 'list'.** The 0.8 similarity cutoff
  rejected the docstring's own example ("lest" scores exactly 0.75). Intents
  that *act* on the house (on/off/cancel) keep the strict 0.8; the benign
  'list' (speaks options, re-prompts, never acts) accepts 0.75, catching the
  live mishears lift/lisp/lest. A lone "left" also maps to list ('left' vs
  'list' is only 0.5 — too far for any sane ratio) — single word only, so
  "left hallway" stays matchable as a real area/light name, and "lamp" is
  untouched (real light names must stay selectable).

## 0.12.4

Operator polish for outside callers, from a packet-level audit of the inbound
call path (which came back healthy end-to-end: continuous 50 pkt/s caller
audio through answer → hold → transfer → operator, speech recognized and
connected on every attempt).

- **Operator listens with more patience.** The recording window is now 10 s
  with a 4 s silence cutoff (was 7 s / 3 s). An outside caller who hesitates
  after the beep — cell latency, unfamiliar flow — no longer gets cut off
  before speaking (a too-early cutoff transcribes as silence and reads as
  "extension not found").
- **Recording diagnostics.** The operator logs each recording's byte size
  (`[operator] rec attempt=N bytes=B`) before transcription; the WAV itself is
  deleted after STT, so this breadcrumb is what distinguishes "caller said
  nothing" from "audio never arrived" after the fact.
- **MWI-clear is gated on room callers.** Dial-0 from an outside line (via
  transfer) ran an MWI clear against the external caller ID — a guaranteed
  failure plus a queued replay. It now runs only when the caller is a
  configured room ext.

## 0.12.3

Inbound calls failed outright ("Channel not available") — fixed by keeping the
router's NAT pinhole open.

- **Trunk re-REGISTER every 120s instead of the 3600s Asterisk default.** The
  VoIP.ms CDR showed inbound calls dying at the provider with `Status is
  'Channel not available' / Failover due to 'Unreachable' status` — the INVITE
  never reached Asterisk (nothing in our logs). Cause: the REGISTER is the only
  outbound traffic that holds the router's UDP NAT pinhole open (trunk qualify
  is deliberately off because VoIP.ms drops OPTIONS), and an hourly REGISTER
  leaves the pinhole closed ~55 minutes of every hour, so the provider's
  reachability pings get dropped and it marks the line dead. Especially bitten
  after a power-event router restart clears the NAT table. `expiration = 120`
  (VoIP.ms's own NAT guidance and their accepted minimum) keeps the path warm
  and the provider's reachability view fresh.

## 0.12.2

Inbound trunk calls no longer connect oddly on the cordless, plus a startup
log-noise fix surfaced while reviewing the logs.

- **Inbound calls: no more accidental transfer-to-operator (the reported bug).**
  An incoming call rang the cordless, connected, then mid-call the caller was put
  on hold and blind-transferred to the operator ("goodbye", hang up). Cause: the
  inbound `Dial()` carried the `t`/`T` flags, which arm Asterisk's in-call DTMF
  transfer codes (`##`/`*2`) for **both** parties — so the answering phone could
  accidentally `##` the caller away, and, worse, the **outside caller** (`T`)
  could invoke feature codes and reach the internal dialplan (a toll-fraud /
  dialplan-injection path). The inbound `Dial()` now uses `r` only. SIP phones
  still transfer intentionally via their own Transfer button (SIP REFER, which is
  independent of these flags).
- **Feature-code flags are now scoped by trust, never armed for a PSTN party.**
  Room-to-room and operator Dials keep `tT` (both ends internal); the **outbound**
  trunk Dial drops `t` (the far PSTN callee can't invoke our codes) but keeps `T`
  (our caller still may transfer).
- **Silenced ~50 ALSA errors per startup.** With no sound card in the container,
  the ALSA/console channel drivers spammed `cannot find card 0` / `Unknown PCM
  sysdefault` at every boot. `modules.conf` now `noload`s `chan_alsa.so` /
  `chan_console.so` — this PBX is PJSIP/RTP only.

Note: the offline iPhone (ext 20) still logs one harmless `invalid URI … No route
to destination` per inbound call for its own leg while the reachable phones ring;
it clears once that softphone registers. An earlier cut of this release gated the
ring group on `DEVICE_STATE` to suppress that line, but adversarial review found
it could also drop a *registered* WiFi cordless from a call after a single missed
`qualify` keep-alive — so the gate was removed in favor of the harmless log line.

## 0.12.1

Security sweep: resolve all open CodeQL code-scanning alerts.

- **`/announce` route: realpath containment** (py/path-injection ×2). The name was
  already regex-validated; the resolver (`safe_announce_path`) now ALSO resolves
  via `realpath` and requires the result to stay inside the announce directory —
  two independent layers against traversal/symlink escape.
- **Generated configs no longer world-readable** (py/clear-text-storage ×2
  hardening). `write()` creates every generated config **0640 root:asterisk from
  the first byte** (`os.open` with mode — no umask window), and re-pins the mode on
  rewrite; same for `/run/switchboard/ami.env` (now group-readable by the asterisk
  user, which the dialplan-spawned AMI consumers needed anyway). Plaintext secrets
  in pjsip.conf/manager.conf remain — Asterisk requires them — documented and
  dismissed with justification.
- **Test regex swap** (py/bad-tag-filter). The JS-parse test extracts our own
  template's `<script>` block by string slicing instead of an HTML-ish regex.

## 0.12.0

Phone->speaker announce now plays on the **ecobee** speakers, bracketed by a
station/airport-style **chime**.

- **Chime + message + chime, as one seamless clip.** Dial 46, record your message;
  the add-on builds a single WAV — a bell "attention please" chime, your spoken
  message, then the chime again — and plays it via `media_player.play_media`. One
  file means no cross-file timing races on AirPlay.
- **Targets the ecobees** by default (`media_player.hallway_thermostat`,
  `media_player.guest_hallway_thermostat`) — configurable via `announce_players`.
- The combined WAV is served to the media players over the LAN by the webui on a
  single **`/announce/<name>.wav`** route, exempt from the ingress guard but
  strictly name-validated (no path traversal, `*.wav` only, ephemeral files).
- New `webui/announce_audio.py` (stdlib sine-synth chime + espeak-ng message +
  WAV combiner); `/run/switchboard/announce` staged asterisk-writable.

## 0.11.2

Dial-a-status menu now **loops back to the menu** after each answer instead of
hanging up.

- After speaking power / weather / house, it asks *"Anything else? Say power,
  weather, house, or goodbye"* and keeps going until you say **goodbye** (or the
  line goes quiet) — no more redialing for a second status. Capped at 8 queries
  as a safety stop.

## 0.11.1

Announce UX + a console boot-crash fix, from testing the new voice features.

- **Phone→speaker announce now retries and is more patient.** A test call recorded
  empty (`transcribe heard=''`) because the caller paused after the tone and the
  3-second silence detector ended the recording before they spoke — and the AGI was
  single-shot. It now gives **two tries** with a clearer prompt ("After the tone,
  say your announcement") and a longer, more forgiving window (12 s, 4 s silence).
- **Operator console no longer crashes on boot with an empty `CONSOLE_PORT`.**
  `int('')` raised `ValueError` (s6 restarted it, so it recovered) — now falls back
  to 2300 cleanly.

## 0.11.0

Three Home-Assistant-integrated voice features — pick up any phone and talk to
your house.

- **Dial-a-status voice menu (dial 45).** *"Status menu. Say power, weather, or
  house."* — then hear live state spoken back: **power** (grid up/down, home
  battery %, hours of runway, solar coverage — from your EcoFlow), **weather**
  (fetched from the National Weather Service for the home's coordinates; no HA
  weather entity required), or **house** (thermostat temps + how many lights are
  on). Rotary-safe (voice, whisper.cpp STT).
- **Smart wake-up.** Your dial-42 wake-up now also fires a configurable HA
  **scene** (`wakeup_scene`), reads today's **weather**, and — if you add a
  calendar to HA — your **next event** (`wakeup_calendar`). All optional and
  degrade gracefully; the greeting + time always play.
- **Phone → HA speakers (dial 46).** Record a short message and it plays out to
  your chosen media players (HomePod, Family Room Soundbar, Garage, …) via your
  local Piper TTS — an intercom from any handset to the whole house.

New config: `status_ext`/`announce_ext` (dial codes 45/46, collision-checked like
the others), `announce_players` + `announce_tts_engine`, and `wakeup_scene` /
`wakeup_weather` / `wakeup_calendar`. Feature settings are staged to an
asterisk-readable `/run/switchboard/features.json` (the AGIs run as the asterisk
user and can't read root-only `/data/options.json`). `ha_client` gains generic
`get_state` / `call_service` (allow-listed domains) / calendar / location helpers;
new `weather.py` (NWS), `ha_reports.py` (spoken read-outs), and a shared
`agi_speech.py` for the voice flows.

## 0.10.4

Make the wake-up UI clearer and less busy.

- **Wake-up list rows redesigned.** Each pending wake-up is now a clean card row:
  **room name** on the left, the **time + when it rings** (`6:00 AM · tomorrow`) on
  the right, then Cancel — instead of the old cramped `⏰ Name … 6:00 AM Cancel`
  with a big empty gap. The "when" (today / tomorrow / weekday) is new, so it's
  obvious when the call actually fires.
- **Card wake-up box is labelled.** The per-room time field gets a ⏰ prefix and a
  tooltip so it reads as "set a wake-up", not a stray empty box.
- **Friendlier empty state** that points to both ways to set one (the ⏰ box on a
  card, or dialing 42).

## 0.10.3

Tidy up the per-room cards on the dashboard.

- **Even button grid.** The action buttons were a wrapping flex row, so in narrow
  cards they stacked onto uneven lines (Test ring full-width, Connect + a lone icon
  wrapping, etc.). They're now a clean 2-column grid — every button the same width,
  aligned, with over-long labels ellipsised instead of blowing out the column.
- **No more mystery icons.** The bare-emoji buttons are fully labelled: `📵 Hang up`,
  `↪ Transfer`, and the message-waiting toggle reads `✉ Message` / `✉ Clear` (was a
  lone `✉`).
- **Roomier cards.** Bumped the card min-width 180→215px so the labelled controls
  fit and the grid shows fewer, wider cards per row.

## 0.10.2

Fix the Lights section being unreadable in dark mode.

- The room cards, the lights **area cards**, and the wake-up time input all paint
  their background from `var(--card)`, but the dark-mode override set `--card` only
  on `.card` — so the lights cards (`.areacard`) stayed **white**, with the page's
  light text on top → unreadable. Moved `--card` onto `body` in the dark block so
  every card-like surface inherits the dark value.
- Added a regression test asserting dark mode sets `--card` at a scope the lights
  cards inherit.

## 0.10.1

Fix the dashboard (GUI) going blank — a JavaScript syntax error blanked the whole
page.

- The transfer prompt was written `prompt('Transfer call to which room?\n' + ...)`
  in the Python source. The dashboard JS lives in a regular (non-raw) Python
  string, so that `\n` became a **real newline inside a single-quoted JS string
  literal** — a syntax error that aborted the entire inline `<script>`, so nothing
  rendered. Escaped it to `\\n` so the browser gets a proper `\n`. (Latent since
  the transfer button landed in 0.9.7; surfaced the first time the GUI was opened.)
- Added a regression test that parses the rendered dashboard `<script>` with
  `node --check`, so a bare newline in an embedded-JS string can't ship again.

## 0.10.0

Remove HD/Opus support entirely — Switchboard is now **G.711 µ-law only**, and the
codec is no longer configurable.

- **Removed the `codecs` option** (config + schema). Every endpoint — rooms and the
  trunk — is hard-pinned to `allow = ulaw` in the generated `pjsip.conf`, so no
  call can negotiate anything but G.711 µ-law and nothing ever transcodes.
- **Dropped the Opus codec** from the image build (no `asterisk-opus` package) and
  removed the `codec_allow` / `KNOWN_CODECS` / `DEFAULT_CODECS` machinery from the
  generator. Simpler and one-codec-clean, as intended.
- The per-call codec indicator on the dashboard/console stays — it now simply
  always reads "µ-law", a live confirmation that the pin is working.
- Docs updated (§9). Note: a phone must still *offer* G.711 µ-law (PCMU); a device
  configured to offer only a non-µ-law codec would have no common codec.

## 0.9.9

Default the whole system to **G.711 µ-law only** — no transcoding, anywhere.

- The shipped `codecs` default is now just `ulaw` (was `ulaw, alaw, g722, opus`),
  and the generator's fallback matches — so every room endpoint renders
  `allow = !all,ulaw`. Combined with the already-µ-law-only trunk, every call
  (analog FXS port, cordless, softphone, and the PSTN trunk) negotiates G.711
  µ-law with no transcode, regardless of what codec order a phone advertises —
  enforcement is server-side at the Asterisk endpoints.
- Extra codecs are not removed, just off by default: set the `codecs` option
  (e.g. `["ulaw", "g722"]`) to re-enable wideband for internal SIP-to-SIP calls.

## 0.9.8

Fix the voice **wake-up** (dial 42) and the dial-0 **MWI auto-clear**, which both
crashed instantly with a permission error.

- **Root cause.** The wake-up store (`/data/wakeups.json`) and MWI store
  (`/data/mwi.json`) live in `/data`, which only **root** can write — but the
  dial-42 wake-up AGI and the dialplan's `System(switchboard-mwi clear …)` run as
  the **`asterisk`** user (Asterisk drops privileges). So the very first store
  touch raised `EPERM` on the `.lock` file, the AGI's `except` set the result to
  "none", and the dialplan skipped straight to "no wake-up → goodbye → hang up"
  **with no pause to speak a time**. (Found in the add-on log: `[wakeup] fatal:
  [Errno 13] Permission denied: '/data/wakeups.json.lock'` and the matching
  `'/data/mwi.json.lock'`.)
- **Fix.** Both stores now live in a dedicated **`/data/state/`** directory created
  by the init step, owned by the `asterisk` user and **setgid + group-writable**, so
  the root services (scheduler, webui) and the asterisk-user processes can all
  read/write them. The lock + JSON files are pre-created group-writable, and each
  atomic write re-applies `0664`, so neither user can lock the other out across the
  flock + temp-file-rename. A pre-existing `/data/{wakeups,mwi}.json` is migrated in.
  `/data/options.json` (which holds the SIP secrets) stays root-only.
- **Defence in depth.** The wake-up AGI no longer aborts before recording if the
  store *read* hiccups — it degrades to the "say a time" prompt so the caller always
  gets their pause for input.

## 0.9.7

One-touch operator call transfer from the GUI dashboard and the TUI console.

- **Transfer an active call from the dashboard.** A room that's on a call now
  shows a ↪ Transfer button: pick a destination room and the *other* party (the
  outside caller, or whoever the room is talking to) is handed off there while
  the original handset drops out. Implemented as an AMI `Redirect` of the FAR
  leg into the `[rooms]` dialplan at the chosen extension — a blind transfer.
- **Transfer from the TUI too.** The console gains a `T` key mirroring the
  dashboard: press `T` on an on-call room, then pick a destination with ↑↓ and
  Enter (Esc cancels) — the same modal target-pick gesture as `C` Connect.
- **Guarded to room extensions only.** The transfer target is validated against
  the configured room set on both the API and the AMI engine, so a redirect can
  only ever land on a known room's `_X.` pattern — never the trunk's outbound
  `_9.` pattern (no transferring a call out to the PSTN). The channel name is
  CRLF-rejected before it reaches the manager socket, as with hang-up.
- **Picks the right party in a ring group.** When an inbound trunk call rings
  more than one room (e.g. cordless **and** iPhone), the call is one bridge with
  the outside leg plus a ringing leg per room. Transfer now always hands off the
  *outside* leg (preferring the trunk/answered leg, skipping a same-ext sibling)
  so it can never accidentally redirect a sibling ringing handset instead of the
  caller — and the redirected leg matches the "↔ Outside" label on the card.
- **Refuses a transfer to an offline room** (both UIs and the API), so a redirect
  can't silently drop the caller onto an unregistered extension.
- This complements the per-device transfer methods already available: analog
  FXS phones use the DTMF feature codes (`##`/`*2`) from v0.9.6, and SIP phones
  (cordless, iPhone) use their own native Transfer button (SIP REFER).

## 0.9.6

Inbound ring-group, analog call-transfer, and an AMI-churn fix from the audit.

- **Inbound ring group.** `trunk.inbound_ext` now accepts a comma-separated list
  (e.g. "19,20") so an incoming outside call can ring the cordless **and** the
  iPhone softphone together (Dial(PJSIP/19&PJSIP/20)). A single ext and empty
  (=all rooms) work exactly as before; a typo'd/non-room entry is dropped+logged,
  and a fully-invalid list falls back to ringing the whole house.
- **Analog call transfer (features.conf).** The FXS phones have no transfer
  button, so a generated features.conf gives them in-call DTMF transfer codes —
  blind `##`+ext, attended `*2`+ext — armed by the Dial t/T flags already in the
  dialplan. SIP phones (cordless, iPhone) keep using their own Transfer button
  (SIP REFER, native to chan_pjsip).
- **Codec read no longer multiplies AMI sessions during a call** (audit MEDIUM).
  `codecs_for_channels` previously opened one connect/login/logoff PER active
  channel on top of the status bundle — so a 2-leg call tripled the AMI logins
  every poll, re-introducing the churn v0.9.3 removed exactly when busiest. It now
  multiplexes all the codec Getvars over ONE login (ActionID-keyed), with a
  response-based terminator. Idle polls still do zero codec AMI work.

## 0.9.5

Show the active-call codec on the per-room tiles too (not just the calls list).

- The room card / console row for a phone on a call now appends its live codec —
  "↔ Outside · µ-law" — so you can see at a glance which codec each handset is on,
  with a transcode showing as a slashed value ("G.722/µ-law"). The codec was
  already carried in `by_ext`; this just surfaces it on the tile (`call_codec`).
  No new AMI work.

## 0.9.4

Show the live codec on active calls — so "is this call µ-law?" is verifiable.

- **Per-call codec on the dashboard and operator console.** Each active call now
  reads the codec its legs negotiated (via AMI `Getvar CHANNEL(audioreadformat)`)
  and shows it, e.g. "📞 Cordless ↔ Outside · Talking · µ-law". One value means
  no transcoding; two (e.g. "G.722/µ-law") reveals a transcode at a glance.
- Uses only the `call` privilege the AMI account already holds — NOT the
  deliberately-withheld `command`/CLI class, so the security boundary is unchanged.
- Read **only while a call is up** (no active channels → no extra AMI work), so
  the idle-poll churn reduction from 0.9.3 is preserved.
- `/api/status` calls (and the console board) gain a `codec` field; tests cover
  the Getvar value parse, the no-transcode vs transcode summary, the idle no-I/O
  path, and the CRLF/empty-channel injection guard.

## 0.9.3

Quiet the Asterisk manager log; cut AMI connection churn.

- **One AMI session per status poll instead of three.** The dashboard and the
  operator console each read endpoints + contacts + channels every refresh, and
  each `get_*` opened its own connect→login→logoff cycle — so a steady stream of
  "Manager 'switchboard' logged on/off" / `SuccessfulAuth` events filled the log
  (~8 every 1.5s). New `ami.get_status_bundle()` runs all three list actions over
  a single connection (one login, one logoff), with the read terminated only once
  every action's own `...Complete` has arrived (matched by ActionID, so a spoofed
  field value or an unrelated action can't end it early). The web `/api/status`
  and the console poller both use it.
- **Console board poll slowed 1.5s → 3s.** Registration/call state changes on the
  order of seconds and operator actions refresh immediately, so this is invisible
  in use but roughly halves the remaining poll rate. Net effect ≈ 6× fewer AMI
  connections.
- No behavior change to the dashboard, console, MWI, paging, or originate paths;
  the stateless `/run/switchboard/ami.env` fallback for dialplan-spawned consumers
  is untouched.

## 0.9.2

Fix intermittent outbound "Service Unavailable" on the SIP trunk.

- **Stop qualifying the trunk's static contact.** VoIP.ms does not reliably answer
  OPTIONS keep-alives, so Asterisk's qualify would flap the trunk contact to
  "Unavailable" — and PJSIP then refuses to route outbound calls to it, so
  `Dial(...@trunk)` fails with **503 "Service Unavailable"** even though the
  registration (and therefore *inbound* calling) stays perfectly healthy. The
  trunk AOR now sets `qualify_frequency = 0`; inbound liveness is covered by the
  periodic re-REGISTER instead. Room AORs still qualify (LAN ATAs answer OPTIONS
  fine) — this is a trunk-only change.

## 0.9.1

Fix outbound calling on the SIP trunk (regression caught on 0.9.0's first live use).

- **Outbound dialplan was emitted into the wrong context.** The `_9.` outbound
  rule and the toll-fraud blocks were appended after the feature contexts, so they
  landed in `[automation]` instead of `[rooms]`. With no `_9.` in `[rooms]`, a
  dialed outside number fell through the catch-all `_X.` room pattern, didn't match
  a known room, and hit `Congestion` — which phones report as **"Service
  Unavailable"**, and no call ever reached the carrier. Outbound rules now render
  inside `[rooms]`, where the literal-`9` patterns out-prioritize `_X.`; inbound
  stays in its own `[from-trunk]` context. Inbound calling was unaffected.
- Tests now assert the *context* each dialplan rule lives in (not just that it
  exists), so this can't regress silently.

## 0.9.0

Outside-line (SIP trunk) refinements for clean, low-latency PSTN calls.

- **Trunk pinned to G.711 µ-law.** The outside line is the PSTN — always
  narrowband — so the trunk endpoint now advertises `ulaw` only (`disallow=all`).
  The provider can no longer negotiate a wideband codec and force a transcode
  against the analog FXS phones (which only adds latency, never quality). HD
  codecs (G.722/Opus) stay available for internal SIP-to-SIP calls between the
  cordless/desk phones.
- **Configurable inbound destination.** New `trunk.inbound_ext` routes an
  incoming call to a single room (e.g. the cordless phone) instead of ringing the
  whole house. Empty (default) keeps the ring-everyone behavior; an ext that
  isn't a configured room is ignored (rings all) and logged.

## 0.8.3

MWI stutter tone now works — switch from `res_mwi_external` to `PJSIPNotify`.
Live testing proved `res_mwi_external` (the `MWIUpdate` action) is **not built
into the Alpine Asterisk package** ("Invalid/unknown command: MWIUpdate"). The
message-waiting indicator is now delivered the portable way: `ami.set_mwi` sends
an unsolicited `message-summary` NOTIFY to the room's contact via **`PJSIPNotify`**
(`res_pjsip_notify`, part of the core PJSIP stack), using on/off templates
generated into `pjsip_notify.conf`. Endpoints no longer carry `mailboxes=`
(unused without res_mwi_external); `modules.conf` loads `res_pjsip_notify` instead.
The Grandstream still needs its "MWI → stutter tone" port setting for the audible
tone (DOCS §4.2).

## 0.8.1

Fix the message-waiting (MWI) stutter tone, found by live testing v0.8.0.
`MWIUpdate` was rejected because Asterisk's `res_mwi_external` **declines to load
while `app_voicemail` is loaded** (they both own a mailbox's MWI), and the stock
autoload loads `app_voicemail`. The add-on now generates `modules.conf` that
noloads the voicemail apps (we run no voicemail) and explicitly loads
`res_mwi_external` + `res_mwi_external_ami` (and `app_confbridge`/`app_page` for
the page intercom). No other behavior change.

## 0.8.0

Operator superpowers — voice home-automation, a full-featured web dashboard,
a house-wide page intercom, and message-waiting stutter tones.

- **Control your lights by voice.** Dial **0** and say "automation" (or dial
  **43**) → say a room → say a light → hear its state → say "turn it on/off".
  It reads the live state and toggles it through Home Assistant. Offline
  throughout: whisper for listening, **espeak-ng** for speaking the light names,
  state and lists (no cloud, no canned-prompt-per-light). The add-on now uses
  the Home Assistant Core API (`homeassistant_api: true`) via the Supervisor
  proxy with its own token — no separate credential.
- **The web dashboard caught up to the console.** Each room card can now
  **connect** two rooms, **hang up** a call, **set/cancel a wake-up**, and toggle
  a **message-waiting** indicator; plus a **Page all** button and a **Lights**
  panel (grouped by room, on/off toggles). The operator console (telnet/browser
  TUI) gained the matching **P** page-all, **M** message, and **L** lights keys.
- **Page all — a house-wide intercom.** Press **P** in the console / **Page all**
  in the dashboard (or dial **44** from any phone): every phone rings and whoever
  answers joins one shared intercom (Asterisk ConfBridge / `Page`).
- **"You have a message" stutter tone (MWI).** The operator can flag a room
  (TUI **M** / dashboard ✉) so its phone gives the classic **stutter dial tone** —
  "call the operator". It **clears automatically** when that room dials 0, and the
  ✉ badge persists across restarts (re-asserted on startup). Requires the
  Grandstream's "MWI → stutter tone" setting (see DOCS §; one-time per port).
- New options: `automation_enabled`/`automation_ext` (43), `page_enabled`/
  `page_ext` (44), `mwi_enabled`. No change to existing room/trunk config.
- New shared modules (`ha_client`, `mwi_store`) + CLIs (`switchboard-tts`,
  `switchboard-mwi`); all pure logic unit-tested (suite 411 → 495 checks).
  Built as five parallel workstreams, then hardened by a five-dimension
  adversarial review (10 findings fixed, incl. restart MWI-replay, the offline
  "Unassigned"-area voice path, and operator-answer latency).

## 0.7.0

Bigger, centered board — the operator console no longer sits jammed in the
top-left of a large terminal.

- **Larger text in the browser terminal:** the xterm.js font goes 14 → 18px, so
  the board reads comfortably on a full-size screen / the HA sidebar panel.
- **Centered board:** the roster is small (8 rooms), so on a wide terminal it
  used to float in the top-left with a big empty void. `render` now centers the
  whole board — horizontal indent + vertical padding sized from the terminal's
  NAWS dimensions — so it sits balanced with even margins. Falls back to no
  padding on a terminal too small to center into (never pushes content
  off-screen). Helps the telnet console too, not just the browser.
- New pure helpers `vis_width()` (ANSI-stripping, wide-glyph-aware column count)
  and `center()`, both unit-tested (test_console.py: 64 checks).

## 0.6.1

Cosmetic: the wake-up entry hint read `⌫ deletes`, but the backspace glyph
(U+232B) has no character in the browser terminal's font and rendered as a
circled-×. Replaced it with plain text — `Backspace deletes`. Caught by
in-browser testing; no behavior change.

## 0.6.0

Set wake-up calls right from the operator console (telnet + browser) — plus a
help overlay and a live time preview.

- **Set a wake-up in the TUI:** select a room and press **W**, then *type* a
  time — `7:30`, `quarter past six`, `0730`, `noon`, `nineteen thirty` (the same
  forgiving parser the dial-42 voice flow uses, so the two paths can never
  disagree). Enter sets it; the board reads back the 12-hour time and whether
  it's today or tomorrow. Press **W** on a room that already has a wake-up to
  edit it (its time is pre-filled); **X** still cancels. Esc aborts with nothing
  written.
- **Live preview while typing:** as you type, the prompt shows the parser's
  reading (`→ 7:30 AM`) so a mistyped time is obvious before you commit.
- **Help overlay:** press **?** for a one-screen key reference.
- This is the TUI's first text-entry mode, which needed two small enabling
  fixes: `parse_input` now recognizes Backspace/Delete (the web terminal sends
  `0x7f`), and **q**/**Q** only quit from the board — a literal `q` while typing
  a time (e.g. "quarter") stays text (Ctrl-C is always a hard exit).
- No new options, services, or dependencies. Wake-ups set in the TUI are
  delivered by the existing scheduler exactly like voice-set ones.

## 0.5.0

Operator console in the browser — a sidebar web terminal.

- **New `console-web` service** serves the existing operator console TUI in a
  browser via **xterm.js** (vendored offline, no CDN). It's a tiny stdlib-only
  HTTP + WebSocket server (no new pip deps; the add-on is musl) that bridges your
  browser to the telnet operator console on the host: WebSocket ⇄ telnet,
  answering/stripping the console's IAC negotiation so only clean ANSI reaches
  the page, forwarding keystrokes, and mapping the terminal's resize to a telnet
  NAWS subnegotiation. Reachable on the LAN at `http://<ha-host>:8100/`.
- **Add it to the Home Assistant sidebar** with a `panel_iframe` ("Switchboard
  TUI") — see DOCS §7. The Ingress UI (`:8099`) is unchanged.
- New options `console_web_enabled` (default true) and `console_web_port`
  (default 8100). The web terminal idles if the operator console is disabled.
- Same LAN-trust posture as the telnet console (unauthenticated; can
  ring/connect/hang up). Session-capped, and turn-off-able via the new option.

## 0.4.1

Old-style speaking clock. The talking clock (dial 41) now announces "At the
tone, the time will be …" followed by a clean 1 kHz pip — the classic
speaking-clock cadence — instead of a plain "The time is …". New `sw-at-the-tone`
prompt + a generated `sw-tone` pip.

## 0.4.0

Wake-up calls — set by voice, delivered on schedule.

- **Request by voice:** dial **42** (`wakeup_ext`) and *say* the time — "seven
  a.m.", "six thirty", "quarter past seven", "noon". Rotary phones can't key in
  digits mid-call, so it uses the same offline whisper STT as the operator, with
  a forgiving spoken-time parser; it reads the time back to confirm. Say a new
  time to change it, or "cancel" to clear it.
- **Delivery:** a new `wakeup-scheduler` service rings the room at the set time
  (AMI Originate into a `[wakeup-deliver]` dialplan) and speaks "Good morning,
  this is your wake-up call, the time is …". One-shot, with a grace window so a
  brief outage can't fire a stale wake-up at the wrong hour. `wakeup_ring_seconds`
  controls how long it rings (default 60).
- **See & cancel anywhere:** pending wake-ups show on the web dashboard and the
  telnet console, each cancelable there (or by dialing 42 and saying "cancel").
- Pure, unit-tested cores (`wakeup/timeparse.py`, `wakeup/store.py`) +
  `tests/test_wakeup.py`. Stored in `/data/wakeups.json` (survives restarts).
- Timezone auto-detect now also tries the Supervisor IP (`172.30.32.2`) so it
  works on this host-network add-on (otherwise set `timezone` explicitly).

## 0.3.1

Talking clock + a real local timezone.

- **Talking clock:** dial **41** (configurable `clock_ext`) and hear the current
  local time — "The time is eight oh five p.m." Uses Asterisk `SayUnixTime`.
- **Local timezone:** the add-on container runs in UTC, which made the console
  clock (and anything time-based) wrong. The init step now resolves a zone —
  explicit `timezone` option, else the Home Assistant timezone (auto-detected via
  the Supervisor), else UTC — and points `/etc/localtime` at it, so Asterisk and
  the operator console both read local time. `tzdata` is now bundled.
- **Core sounds:** Asterisk's core English sound files (digits + time words) are
  now included — needed by `SayUnixTime`/`SayNumber` (and the upcoming wake-up
  calls). µ-law, matching the analog path.
- New options: `timezone`, `clock_enabled`, `clock_ext`. Lays the groundwork for
  wake-up calls (v0.4.0).

## 0.3.0

Add a **telnet switchboard operator console** — a live TUI for working the
board like a cord-board operator.

- **Connect over telnet:** `telnet <host> <port>` (default **2300**,
  `console_port` / `console_enabled` options). A raw-TCP ANSI TUI — no client
  install, works from any terminal.
- **Live board:** every room with real-time status — ● Registered, ○ Offline,
  ◐ Ringing, ◉ On call ↔ *peer* — plus an Active-calls panel ("Kitchen ↔
  Office · 02:14"), refreshed ~1.5 s.
- **Operator actions:** **R** ring/page the selected room, **C** connect two
  rooms (rings A, then dials B via the room dialplan), **H** hang up the
  selected room's call, **↑↓ / j k** select, **Q** quit. Connect/ring/hangup go
  through the same room-validated AMI helpers as the web button, so the console
  can't place an outside call.
- Implemented in Python stdlib (telnet IAC negotiation, NAWS resize,
  frame-hash anti-flicker, alt-screen) as a new s6 `operator-console` service,
  reusing the `webui/ami.py` engine. New `connect_extensions` / `hangup_channel`
  AMI helpers + `tests/test_console.py`.
- **Security note:** like the EcoFlow telnet console, this is **unauthenticated
  on the LAN** and performs call-control (ring/connect/hang up) — so it assumes a
  trusted home network. It's hardened to that scope: connect is validated against
  the configured room set (never the trunk's outbound pattern, even with a trunk
  enabled), sessions are capped (5) and idle-timed-out (15 min), and the bind is
  configurable via `console_bind` (set `127.0.0.1` to keep it host-local, or
  `console_enabled: false` to turn it off).

## 0.2.8

Make the dashboard interactive and call-aware.

- **Test-ring button per room:** each phone card has a 🔔 **Test ring** that
  places a one-cycle ring to that extension (AMI `Originate` → a short
  `sw-test` prompt if you pick up). The button is disabled for offline phones
  and shows "Ringing…". The originate is constrained server-side to ringing a
  *known room ext* with a fixed `Playback` — it can never dial an outside line —
  and the AMI account's new `originate` privilege is paired with `system` for
  that (still no `command`/CLI).
- **Readable call details:** the "Active calls" list now shows who's talking to
  whom by room name — "Kitchen ↔ Office", "Garage ↔ Outside (+1…)", or
  "Kitchen → Operator" — with state (Ringing/Talking) and duration, grouped per
  call via Linkedid instead of dumping raw channel names.
- **Per-card "talking to":** a busy room's card shows its current peer
  (↔ Office / ↔ Outside / ↔ Operator) and the live call state.
- New `POST /api/ring/{ext}` (Ingress-only, validates the ext); new
  `summarize_calls`/`channel_ext` helpers + test coverage in `tests/test_webui.py`.

## 0.2.7

Actually fix the dashboard showing every registered phone as "Offline" (the
0.2.6 read-until-Complete change addressed the wrong layer).

- **Root cause (diagnosed live):** `PJSIPShowEndpoints` parsing was fine —
  `/api/status` already returned `DeviceState: "Not in use"` for all 8 rooms.
  The failure was isolated to two places: (1) `registered` was derived *only*
  from a `PJSIPShowContacts` match that never landed because the AMI client read
  fields with the wrong casing (`AOR`/`URI`/`Status` vs Asterisk's `Aor`/`Uri`),
  so every contact keyed on `""`; and (2) the browser pill used
  `device_state.includes('use')`, which matches `"Not in use"` and painted idle
  phones orange.
- **Case-insensitive AMI parse:** `_ami_command` now lower-cases every response
  key, so no caller can be broken by Asterisk's inconsistent field casing again
  (applies to endpoints, contacts, and channels at once).
- **Registration from device state:** a PJSIP endpoint reads `Unavailable` with
  no reachable contact and `Not in use` once one binds — so `registered` is now
  taken from `DeviceState` (the signal Asterisk already aggregates), with
  contact reachability as a secondary confirm. The per-contact row is enrichment
  (status text + RTT) only. Contacts are keyed by `aor`/`endpointname` with an
  `objectname` fallback so a renamed field can't silently drop them.
- **Pill fix:** `"Not in use"` → green **Registered**; only an active call state
  (`In use`/`Ringing`/`Busy`/`On Hold`) → orange; otherwise red **Offline**.
- **Contacts keyed correctly:** the `ContactList` event has no `Aor` field — the
  endpoint identity is its `Endpoint` field — so RTT and real contact status now
  populate instead of silently dropping (an adversarial review caught that the
  prior keying only worked by `ObjectName` accident).
- **Auth failures are now visible:** a wrong/rotated AMI secret previously read
  as `ami_ok=true` with every phone "Offline" and no banner — indistinguishable
  from a real outage. A failed AMI login now surfaces the "cannot reach Asterisk
  Manager" banner.
- **Hardened stream read:** the list terminator is matched on a real
  `Event: …Complete` line (not a bare substring), so an attacker-influenced field
  value — an inbound trunk `CallerIDName` or a phone `UserAgent` containing
  "Complete" — can't truncate the live view; plus an upper bound on the buffer.
- **Testability:** the AMI client moved to a framework-free `webui/ami.py` and
  gained a plain-`python3` test suite (`tests/test_webui.py`) covering the
  casing, ContactList identity, DeviceState registration, terminator, and
  auth-failure paths — the regression net that was missing.

## 0.2.6

Fix the Ingress dashboard always showing rooms as "Offline".

- **AMI event truncation:** the web UI sent `Login → action → Logoff`
  back-to-back, but `PJSIPShowContacts`/`PJSIPShowEndpoints` stream their
  results as async events ending in a "...Complete" event. Sending `Logoff`
  immediately made Asterisk close the socket before the events finished, so the
  contact list arrived empty and every room read "Unregistered" even when fully
  registered. Now reads until the action's "...Complete" event, then logs off.

## 0.2.5

Engaged-line handling + end-of-call tone, from live testing.

- **Instant busy on an engaged line:** the operator now checks the room's
  `DEVICE_STATE` before dialing — if it's already on a call, it plays "That line
  is busy" immediately instead of dialing (which made the gateway *call-waiting-
  ring* the busy line, so the caller heard rings then a delayed message). No
  gateway change needed.
- **End-of-call tone:** every operator hangup path (and the end of a connected
  call, when the far end hangs up) now plays a short two-tone cue (`sw-endtone`)
  so a caller on an antique handset hears that the line is down.

## 0.2.4

Operator now tells the caller *why* a connection didn't complete.

- **Busy / no-answer / unavailable handling:** when the operator dialed a room
  that was busy, didn't answer, or wasn't registered, the dialplan fell through
  to a bland "Goodbye" — indistinguishable outcomes. Now it branches on
  `${DIALSTATUS}` and plays a spoken status: "That line is busy…",
  "There's no answer…", or "That room isn't available right now." (new prompts
  `sw-busy`, `sw-noanswer`, `sw-unavailable`).
- **Observability:** Asterisk now runs at `-vvv`, so the log shows dial outcomes
  (`Operator dial <ext> -> <DIALSTATUS>`) and full call tracing — call volume on
  a home PBX is low, so the extra verbosity is worth the diagnosability.

## 0.2.3

Tune the operator from the first real on-Pi calls (whisper.cpp recognized
"Kitchen", "Living Room", and a full sentence at 1.0 — these are polish fixes).

- **Clipped-word recognition (prefix match):** the narrowband line drops soft
  word tails, so whisper heard "Basement" as "Base." The matcher scored that an
  ambiguous tie (Basement vs incidental fuzzy overlap with Master Bedroom) and
  refused to connect. Added a word-prefix bonus so a heard word that's a clean
  prefix of a room name wins decisively ("Base"->Basement, "Din"->Dining).
- **Double "Goodbye":** on a no-match the AGI prompt already ended with
  "Goodbye" and the dialplan played another — dropped it from
  `sw-no-such-room` ("Sorry, I couldn't reach that room.") so there's one.
- **Log noise:** silence the `audioop` DeprecationWarning the STT wrapper
  emitted on every call.

## 0.2.2

Fix the voice operator never recording (no pause to speak, prompts running
together).

- **`RECORD FILE` aborted before recording:** the AGI passed an offset arg and
  `BEEP` to `RECORD FILE`. The minimal Alpine `asterisk-sounds` has no built-in
  `beep` file, and — worse — the offset positional makes `res_agi` treat it as a
  beep request and abort the record, so no audio file was ever written. The STT
  wrapper then ran on a nonexistent file and both retry prompts played
  back-to-back with no chance to speak. Fixed: record as
  `RECORD FILE … <timeout> s=<silence>` (no offset, no `BEEP`), and play a
  bundled `sw-beep` "speak now" cue instead of the absent system beep. Also
  bumped the record window to 7 s / 3 s trailing-silence for slower speakers.

## 0.2.1

Fix an Asterisk crash-loop introduced in 0.2.0.

- **astdb ownership (regression fix):** the operator added files under
  `rootfs/var/lib/asterisk/` (the AGI + prompt audio), and `COPY rootfs /` reset
  `/var/lib/asterisk` to root-owned. Asterisk runs as the `asterisk` user and
  could no longer create `astdb.sqlite3` there (`ASTdb initialization failed —
  ASTERISK EXITING`), crash-looping with 0 phones registered. The asterisk
  service's startup chown now covers `/var/lib/asterisk` alongside run/log.

## 0.2.0

Add a **voice operator** — dial `0`, say a room name, get connected.

- **Why:** rotary/pulse antique phones can't drive DTMF menus (no `*`/`#`), so
  voice is the natural interface. Dial `0`, the operator greets you, you say the
  room ("Kitchen", "the study"), and it connects the call.
- **Fully offline.** Speech recognition is **whisper.cpp**, built from source in
  the image (Vosk was evaluated first but ships only glibc wheels — no musl/apk —
  so it can't run on the Alpine base). No cloud, nothing leaves the house.
- **Architecture:** dial `0` → `[operator]` dialplan context → a stdlib Python
  **AGI** that plays prompts, records the caller, and shells out to
  `switchboard-stt` (the only component that touches whisper). The AGI sets
  channel vars; the **dialplan does the Dial, and only to a known room ext** —
  so a recognizer error can never dial an arbitrary endpoint. The recognizer is
  biased toward your room names and a fuzzy matcher resolves near-misses; a
  near-tie between two rooms re-prompts rather than guess.
- **New options:** `operator.enabled` (default true) and `operator_synonyms`
  (extra spoken names per room, e.g. "office"/"den" → the study).
- Prompts are pre-recorded audio (no runtime TTS dependency). Build is
  multi-stage so the C++ toolchain doesn't ship in the final image.

## 0.1.6

Fix an AMI regression from v0.1.4.

- **AMI privileges (regression fix):** v0.1.4 emptied the manager `write` classes
  on the assumption the Ingress UI was read-only. But Asterisk gates the UI's
  status actions (`PJSIPShowEndpoints`, `PJSIPShowContacts`, `CoreShowChannels`)
  on *write* authority, so every poll was denied (`RequestNotAllowed` in the
  log) and the dashboard could never show a phone as registered. Restore the
  minimum needed (`write = system,call,reporting`) while still excluding the
  dangerous `command` (CLI/RCE) and `originate` (place-calls) classes — keeping
  the least-privilege intent without breaking status.

## 0.1.5

Close the two deferred high-severity items from the v0.1.4 review.

- **Ingress access control (H1):** the web UI is host-network-exposed, so its
  port was reachable directly on the LAN, bypassing Home Assistant Ingress auth
  (the `/api/status` roster/call data leaked). Per the add-on docs, the app now
  rejects any client other than the Supervisor (`172.30.32.2`, plus loopback)
  with `403`. The bind is unchanged, so Ingress is unaffected.
- **Toll-fraud guard (H2):** the outbound trunk dialplan now denies
  international (`011`) and premium-rate (`900` / `1-900`) destinations before
  the general outbound rule. Normal dialing is unchanged. (Trunk is still off by
  default.)

## 0.1.4

Security & robustness hardening of the config generator and Ingress UI
(hardening only — no feature changes; for valid inputs the only generated-config
change is the AMI least-privilege tightening below).

- **Input validation / config-injection defense:** room names, secrets, and all
  trunk fields are now scrubbed of control characters before being written into
  `pjsip.conf` / `extensions.conf`; entries that can't be made safe are skipped
  with a log instead of corrupting the config. `dial_prefix` and
  `outbound_caller_id` are charset-validated.
- **Rooms validated once (`valid_rooms`)** and shared by both renderers: dedupes
  colliding extensions, enforces the 2–6 digit ext rule, and keeps `pjsip.conf`
  and the dialplan in sync. Warns when zero valid rooms remain.
- **No secrets in logs:** the skip log no longer prints the room dict (which
  contained the plaintext secret) — only the extension.
- **Least-privilege AMI:** the Ingress UI's manager account drops all write
  classes (was `system,call,command,reporting,originate`); it only ever reads.
- **Robust parsing:** a malformed `options.json` and non-numeric port/RTP values
  no longer crash the init oneshot (which would take the whole add-on down);
  they fall back to defaults with a clear log.
- **Web UI XSS fix:** room labels and AMI caller-ID (attacker-controlled on
  inbound trunk calls) are HTML-escaped before rendering; AMI errors are
  reported to the browser as a generic message and logged server-side.
- **Dialplan correctness:** multi-character `dial_prefix` now strips the whole
  prefix (`${EXTEN:N}`) instead of a single character.

## 0.1.3

Log cleanup (cosmetic — the add-on already runs).

- Add a curated `modules.conf` that autoloads everything except the modules
  that only probe for hardware that isn't present in a container: `chan_alsa`
  and `chan_console` (the ALSA/JACK error spam), `chan_dahdi`, and a few
  deprecated ADSI / unused SQLite CDR-CEL backends. Cleaner logs, less memory.

## 0.1.2

Startup fix.

- Rework the AppArmor profile. The previous strict path allowlist blocked the
  s6-overlay init (`/bin/sh: can't open '/init': Permission denied`, crash
  loop). The profile now follows the documented HA add-on pattern: broad
  file/capability/signal/network access under a named, mediated profile, with
  explicit exec rules for the s6-overlay boot chain.

## 0.1.1

Build fix.

- Drop the non-existent `asterisk-pjsip` and `asterisk-sounds-en` Alpine
  packages that broke the image build. PJSIP ships inside the main `asterisk`
  package; `asterisk-sample-config` provides modules.conf so it autoloads.
- Music-on-hold sounds and the Opus codec are now installed best-effort, so a
  missing optional package can never fail the build.
- Invalid-extension handling uses a generated congestion tone instead of a
  prompt sound file (no sounds package required for core calling).

## 0.1.0

Initial release.

- Asterisk 21 + PJSIP packaged as a Home Assistant add-on (Ingress + AppArmor).
- Room-to-room calling: each `rooms` entry becomes a PJSIP endpoint for one
  Grandstream GXW4216 V2 FXS port.
- Configurable codecs (`codecs` option). Default prefers G.711 µ-law for the
  analog path and also offers G.722 and **Opus** for HD-capable / IP endpoints.
  Opus codec module installed best-effort.
- Config generated from add-on options on every start (`switchboard-config`).
- Ingress web UI showing per-room registration and active calls (FastAPI + AMI).
- SIP trunk support (disabled by default): outbound via a dial prefix, inbound
  rings all rooms.
