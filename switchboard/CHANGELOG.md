# Changelog

Recent releases. Releases up to 0.113.0 are in the
[changelog archive](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/CHANGELOG-ARCHIVE.md).

## 0.122.0

**Spoken replies on a call are shaped for the phone band too.**

- Everything the voice features say that has no recording is spoken by
  espeak-ng through `switchboard-tts`: the directory (`411`), the lights menu
  (`43`), dial-a-status (`45`), the announce line's prompts and read-back
  (`46`), the wake-up weather and calendar, and the voice assistant (`47`).
  That speech now goes through the handset announcements' mastering (0.121.0),
  by the same code: the ffmpeg chain to the 300–3400 Hz band with the
  consonants lifted and ffmpeg's resampler to 8 kHz, then the speech level set
  to the built-in prompts' -15 dBFS under a -1 dBFS peak limit. espeak-ng's own
  output measures -18.6 dBFS, so the replies are about 3.5 dB louder, level with
  the prompts around them. On a Home Assistant host a 2-second reply renders in
  about 0.20 s instead of 0.06 s, and a 9-second one in 0.28 s instead of
  0.08 s.
- If ffmpeg is missing, fails, runs past 3 seconds or returns unusable audio,
  the reply is spoken exactly as in 0.121.0, and the Log tab says so once per
  start, without the words. The limit is 3 seconds, not the announcements' 10,
  because a reply's synthesis must finish within 20 seconds and espeak-ng alone
  may take 15. After a run past it the shaping is skipped for 10 minutes, so a
  stalled ffmpeg delays one reply, not every reply after it. Silence, replies
  under 20 ms and speech of 120 seconds or more are spoken unshaped and not
  logged.
- `switchboard-tts` now writes its own lines straight to the Log tab, so one
  is not lost when the caller hangs up while a reply is being made. The
  directory, dial-a-status, announce and wake-up features used to drop them; the
  lights menu and the assistant, which carried them on their own `tts failed`
  line, now show them on the line before it. They never contain the words.

## 0.121.0

**Announcements to the handsets are shaped for the phone band.**

- Every phone here hears 8 kHz narrowband audio, about 300–3400 Hz. The speech
  of a handset announcement (`POST /api/announce/<ext>`, `{text}` or `{url}`)
  now goes through one ffmpeg process: a 250 Hz high-pass, -2 dB at 500 Hz,
  +5 dB at 2.8 kHz for the consonants, a 3.5 kHz low-pass, 3:1 compression,
  and ffmpeg's resampler to 8 kHz, which keeps energy above 4 kHz from folding
  back into the band as hiss (the old resampler left a 6 kHz tone only about
  15 dB down at 2 kHz). Each clip's speech is then raised toward the
  built-in prompts' level, -15 dBFS, by at most 20 dB, under a look-ahead
  peak limiter at -1 dBFS. Speech up to about 15 dB quieter than typical TTS
  output reaches that level; a quieter clip gets the full 20 dB and stays
  below it. The chime is not processed and sits about 3 dB above
  the speech.
- If ffmpeg is missing, fails, runs past 10 seconds or returns unusable audio,
  the clip is rendered exactly as in 0.120.0, and the Log tab says so once per
  start. After a run past 10 seconds the shaping is skipped for 10 minutes, so
  a stalled ffmpeg holds back one announcement, not every one after it. Silence
  and clips under 20 ms are not shaped and not logged. The same input renders
  the same bytes, so duplicate suppression is unaffected. On a Home
  Assistant host, a 10-second `{text}` or WAV clip renders in about 0.2 s
  instead of 0.4 s, as the Python resampler is off its path, and a 10-second
  MP3 clip takes about 90 ms longer. Announcements to the Home Assistant
  speakers (`46`) are unchanged.
- A WAV `{url}` clip cut off mid-sample now plays up to the cut instead of
  failing with HTTP 502. A WAV whose chunks the reader cannot follow, or with
  no frame rate, no longer raises an error; it is handed to ffmpeg, which
  usually cannot decode it either, so it still fails with HTTP 502.

## 0.120.0

**Recorded prompts can be replaced with your own recordings.**

- A file in `/app_configs/<slug>/prompts` (`addon_configs` on older Home
  Assistant releases) named after a prompt (`sw-greeting.wav`,
  `sw-no-emergency.ulaw`, ...) replaces the built-in recording at the next
  start: an 8 kHz mono 16-bit PCM `.wav`, raw µ-law `.ulaw`, or both. A `.wav`
  given alone gets a µ-law copy, the phones' codec; given both, the phones play
  the `.ulaw`, and the summary line counts those names. Removing the file and
  restarting returns the built-in prompt. DOCS.md lists every prompt and what it
  says, with conversion commands.
- Each file is checked first: a built-in prompt's name, a regular file and not
  a link, 0.1 s to 1 MiB, some sound above about -50 dBFS, for a `.wav` the
  format above, and for a `.ulaw` not a WAV, MP3, FLAC, Ogg, AIFF, CAF or MP4
  file renamed. A file that fails is skipped with one log line, and that prompt
  keeps its built-in recording, the `911` notice included. Raw 16-bit PCM or
  A-law named `.ulaw` cannot be told from µ-law and plays as noise; DOCS.md says
  how to make the file and to dial `933` to hear the notice. Anything unexpected in this step leaves every prompt built-in and
  the start carries on. One line sums up the start.
- The shipped recordings stay write-locked. The accepted files are installed
  under `/run/switchboard/prompts` and played from there by absolute path, so
  a custom `.wav` can never lose to the built-in `.ulaw` beside it. The
  dialplan, every voice flow, the test ring and the talking clock ask one
  resolver which recording a prompt plays; the clock measures that same file,
  so its tone stays on the second it names.

## 0.119.4

**The dashboard no longer shows the trunk provider's server.**

- The header and the Trunk position typed the host set in
  `trunk.provider_host`, and `/api/status` sent it to the browser, where the
  developer tools could read it whatever the page drew. A provider's host is
  usually a regional point of presence, so it can say roughly where the house
  is. The status now carries only whether the trunk is on and its
  registration; the header reads `trunk: on` and the Trunk position `on`,
  beside the registration as before.
- The operator console showed only the registration and is unchanged. The
  add-on's Log tab still names the host on a trunk registration retry.

## 0.119.3

**The readable Asterisk log no longer names the trunk provider's server.**

- `/share/switchboard/asterisk.log` kept the provider's host on every trunk
  registration retry, on the reasoning that it identified nobody. A provider's
  host is usually a regional point of presence, so it can say roughly where the
  house is. Both scrub passes now take out every whole-host occurrence, on any
  line, of the host name set in `trunk.provider_host` or `trunk.from_domain`
  (in the options or the options overlay, with or without a `sip:` scheme, a
  port or a trailing dot), and the host name in each `sip:` URI on a
  registration line, configured or not. An address set there is not a host
  name: a private one is masked as before, a public one stays. The boot pass
  writes `<trunk-host>`, the per-poll pass a mask of the same length; the port,
  the masked account and the retry interval stay.
- The boot pass scans the whole file at every start, so the lines already there
  go at the first start of this version: on a copy shaped like an affected log,
  130 retry lines naming the host 260 times, it left none and changed no other
  line. Backups taken before that start, the private `/data/state/asterisk.log`
  and the add-on's Log tab still carry the host.

## 0.119.2

**No root read or write in `/data/state` follows a link.**

- `/data/state` belongs to the `asterisk` user, its group can write it, and it
  is not sticky, so that user can put any name in it. Five root writers there
  still went through a name: the ever-registered set wrote a fixed
  `<file>.tmp`; the low-MOS capture appended, trimmed and changed its mode by
  name; the link-health ledger read the old file by name, which copied a link's
  target into a ledger the `asterisk` group reads, and set its mode by name
  after the replace; the wake-up and MWI stores opened and changed the mode of
  their `.lock` files by name, and read the store by name before rewriting it.
  Each now reads and appends through a no-follow descriptor on a regular file.
  A writer that replaces its file writes a new temporary file of its own, with
  its mode settled before the replace.
- A link or a FIFO at a store's lock name is removed and the lock created
  again, so wake-ups keep ringing; a directory there refuses the call. A FIFO at
  a store's name reads as an empty store instead of hanging the reader.
- Root reads from `/data/state` follow no link either, and none blocks on a
  FIFO: the two alert latches, the ever-registered set, the call ledger
  devhealth reads every cycle, the durable Asterisk log rtpmon reads for
  transitions, and the backup hook, which opens every file there and in
  `/share/switchboard` to flush it. A FIFO at a latch, the set or the call
  ledger stopped its poller, at start or on its next cycle. One swapped in after
  a check by name stopped rtpmon at the log, and hung the backup hook and the
  backup it runs in.
- The latches and the set are read up to 64 KiB and the log up to 512 KiB a
  cycle, so none of them is read whole, whatever its size. A set nested too
  deep to parse, or one whose `exts` is not a list, no longer stops rtpmon at
  start.
- rtpmon now saves `ever-registered.json` as `0600`: it is the file's only
  reader. A set saved before keeps its mode until an extension registers for the
  first time. The low-MOS capture goes back to root on its next write; on
  installs that ran v0.102.0 to v0.108.x it was owned by `asterisk`.

## 0.119.1

**The gateway alert needs two link-health samples, not two of its own cycles.**

- Gateway health is read every 120 s off a rollup published every 300 s, so
  a GXW reboot caught by one sample (a firmware auto-install at 01:00, the
  weekly reboot) was read twice and raised "all 8 gateway ports down", then
  its recovery. The alert now advances only on a sample with a new
  `measured_at`, as the cordless's registration does, and goes out on the
  first read of the second; a recovery comes on the first new sample that
  reads `ok`. A rollup without `measured_at` is judged cycle by cycle.
- No reading inside the 360 s startup grace counts. Its "re-registers"
  reading is still published but no longer raised as a card, and a gateway
  already down at start alerts 480 to 720 s after the start (was 480 s).
- The cause is decided a sample later. In 3,000 simulated outages the gateway
  and outage alerts named the same cause in all; before, they differed in
  198, each a cordless dropping one sample after the ports.
- Gateway and cordless alerts survive an add-on restart. Each is kept in
  `/data/state` with whether its card was shown and the host boot; a shown
  alert is not raised again, and its recovery still comes (a gateway already
  back clears on the first sample read after the grace, 360 s after the
  start). One not shown, raised over 7 days earlier, or raised before a host
  reboot, which clears Home Assistant's cards, is raised again if the
  condition persists.
- The fleet-outage alert is not restored after a host reboot either. Its
  latch now carries the host boot, so an outage still on is announced again,
  card and push, on the second settled sample, and one already over posts no
  recovery.

## 0.119.0

**A phone-system outage reaches the phone, and its alerts name what is down.**

- On 2026-11-12 the host moved to a new address at 18:28 MST (01:28 UTC on
  11-13) and all nine phones were unregistered from 18:31. Every alert was a
  silent notification card. The outage card counted "10 of 10", including
  ext 20, which has never registered; the gateway alert said the GXW had
  likely lost power; cordless health read `ok` throughout; and both
  link-health rollups read `unknown`.
- The fleet-outage alert and its recovery are also pushed to
  `wakeup_push_target` ("phones down", "phones back"): ordinary pushes, not
  critical ones, and not retried. The key is unchanged; its label is now
  "Phone Push Target". `link_health_alerts: false` silences the push too.
- The outage alert clears only once two fewer phones than its threshold are
  down (3 of 9) on two consecutive samples, and the recovery names any still
  down ("still down: ext 14"). It cleared on the first sample under the
  threshold, so four ports dead and a fifth flapping raised and cleared it
  every 15 minutes.
- The outage latch is kept in `/data/state`, with whether the alert was
  shown. A refused card counts as shown once its retry posts it. A restart
  during an outage does not raise or push a shown alert again, and its
  recovery still comes; one muted or lost unshown is raised after the
  restart.
- Extensions that have never registered no longer count toward the outage
  threshold or appear in its text ("9 of 9"). `link_health` gains `expected`
  and `never_registered_exts` attributes.
- Cordless health reads the cordless's SIP registration off the link-health
  rollup: unregistered on two consecutive rollup samples read after the 360 s
  startup grace is `critical`, even with no IP to probe. A reboot inside one
  sample, or a rollup that cannot say, raises no registration alert; the LAN
  probe can still report a rebooting handset unreachable. The sensor gains
  `sip_registered`. A cleared registration, reachability, battery or Wi-Fi
  alert now reads as a recovery, not "alert lapsed". No cordless alert clears
  while the newest sample shows the handset unregistered, and with nothing to
  probe, such a cycle does not publish `ok` over the alert.
- When every gateway port and the cordless drop together, the outage,
  mass-drop and gateway alerts name the Switchboard host or the network
  rather than the gateway. A port still up, a cordless already down before
  the outage began (even one sample before), or a drop healed before the next
  sample keeps the gateway wording.
- `link_health` reads `unavailable` when no phone that should be up is
  reachable, and `wired_link_health` when no gateway port is; `unknown` now
  means only reachable but not yet measured. Units and `state_class` do not
  change.
- A failed push no longer names its target in the add-on log.

## 0.118.0

**The "phones dropped" card says only what is true of each phone it names.**

- The link-health monitor also catches a fleet that drops between two samples,
  from Asterisk's log. Its card said the phones "came back before the next
  health check" and were "up now, not a current outage" whenever enough phones
  had gone Unreachable since the last sample, without looking at the sample it
  was judged on. Twice that sample showed them down: 2026-11-02 01:01 MST
  (08:01 UTC), the gateway's weekly reboot caught mid-reboot (8 wired ports
  down), and 2026-11-07 19:06 MST (02:06 UTC on 11-08), a router reboot (all 9
  phones down). Both fleets were back by the next sample.
- The card is now judged per phone against the sample. A "dropped briefly ...
  up now" card is posted only when every phone that dropped is reachable in
  it. Otherwise a "phones dropped" card is posted at once, naming the phones
  still unreachable at that check, and a later check replaces it under the
  same id: who was back, and who, if anyone, still was not. "Not a current
  outage" is said only once every one of them is back and the sample is not a
  fleet outage. Phones re-register one at a time, so a sample can show one to
  three still down while the fleet is below the outage threshold; judged per
  fleet, that read as healed.
- "All phones" and "nothing was reachable" are said only when every
  registered phone dropped. The threshold is half the fleet, and a phone that
  stayed up (often the cordless, which the outside line rings) kept working.
- If the phones stay down, the outage alert ("phones unreachable") follows as
  before and the card says so when the outage threshold is met. A second drop
  while a card is open gets a card of its own; each is updated under its own
  id.
- Asterisk's log is now read just before each sample rather than after it, so
  every drop counted was logged before the sample that judges it.

## 0.117.0

**An alert Home Assistant could not accept is posted once it answers.**

- The link-health and device-health monitors raise each alert once, when a
  state changes: the cordless goes critical, the phones become unreachable, the
  outside line drops. When Home Assistant refused the notification, the alert
  was lost. On 2026-11-07 a host reboot left Home Assistant Core unanswered for
  twelve minutes while the add-on was already running, and three alerts
  (cordless critical, all phones dropped briefly, cordless recovered) were
  refused and never shown. The phones recovered inside that window; a phone
  that went down during it and stayed down would have raised no alert at all.
- A refused alert is now kept and posted at the start of each later cycle of
  the monitor that raised it (by default every 2 minutes for device health and
  every 5 for link health). It is dropped only if Home Assistant refuses a retry
  of it more than an hour after it was raised, so every alert gets at least one
  retry of its own whatever the poll interval. A late post says so, and gives
  the time it was written.
- A newer alert with the same `notification_id` replaces a kept one, as Home
  Assistant itself does, so a stale "critical" never lands after its
  "recovered". The message that replaces it names the alerts that were never
  shown: the first, how many came between, and the latest three. An alert that
  was dropped is still named by the next alert for the same device or line, for
  12 hours.
- The "wake-up not delivered" card, which is posted when the critical push
  cannot be sent, is retried the same way. The push itself is not: an alarm
  that failed should not sound an hour later. Call-quality cards are not
  retried, since each call's recorder runs once and exits; their
  `notify_status` still records `failed`. The missed-wake-up notification keeps
  the retry it has had since the 2026-10-04 power cut.

## 0.116.0

**Each call-quality score is judged against its own stream; loss alerts at
1.5 %; a conversation with an abnormal jitter peak alerts.**

- Asterisk names its two quality scores the opposite way round to its loss
  counters: `rxmes` scores the audio the PBX sends and `txmes` the audio it
  receives, while `rxploss` and `txploss` count the reverse. The filter that
  discards a collapsed score as a re-INVITE glitch paired them by name, so each
  score was checked against the other stream's loss. A wake-up on 2026-09-25 that
  lost 43.8 % of its audio on the way to the cordless scored 20 for that stream;
  the filter compared the 20 with the microphone's 0 % loss and dropped it. Each
  score is now checked against its own stream, and that wake-up's record would
  show the 20.
- A short call that loses a few dozen packets now alerts in either direction:
  the loss alert moves from 3 % to 1.5 %, where loss alone already takes a call
  below `good`. Asterisk's score could not be relied on to catch it. The score
  for the audio the PBX sends falls on the running total of lost packets (41
  lost early in a 38-second call take it from 88 to 28); the score for the
  audio it receives falls on the loss per 5-second report (the same burst leaves
  it near 81). Across the last 300 recorded legs this adds two notifications,
  both short calls from the cordless to set a wake-up.
- The cordless's own low call score degrades its health sensor only when the
  PBX's record agrees. That check read `mes_tx`, the PBX's score for the
  handset's microphone, and now reads `mes_rx`, the score for what the handset
  heard. Replayed on the 42 low scores captured since 2026-09-15, every verdict
  is unchanged.
- A conversation whose receive-jitter peak goes above 60 ms now raises a
  call-quality notification naming the peak. That covers answered room-to-room,
  outside, inbound and transferred calls, calls connected by the operator or
  directory, and pages. An operator or directory call counts only when its
  connecting Dial() reports `ANSWER`, since one that connected nothing spoke
  only to the speech recogniser. Each leg now records that value as
  `dialstatus` (ledger schema 7). The
  excellent/good/fair/poor grade is unchanged, as with a round-trip alert.
  Wake-ups, announcements and the voice menus are not checked: the figure
  measures the stream the PBX receives, which on those legs is a handset
  microphone nobody hears. There the WiFi cordless often peaks at 40–58 ms, 50
  times in 33 days with under 0.5 % loss. Across the last 300 recorded legs the
  check would have raised one notification.
- The filter that discards a collapsed score caused by a re-INVITE or transfer
  now reads the same receive-jitter peak, at the same 60 ms line. It used two
  single-moment readings before. One of those, the far end's last report, read
  as high as 136 ms on talking-clock and assistant legs whose received audio
  was steady, so a glitch on such a leg could stay in the score as a false
  "poor". No verdict in the current ledger changes.
- Asterisk 20.11.1's own score (MES) gives jitter almost no weight, because it
  multiplies the jitter in seconds. The README and the health-monitoring
  reference now say so.

## 0.115.0

**Documentation opens quickly in Home Assistant.** No change to how the add-on runs.

- Home Assistant renders the add-on's `DOCS.md` whole each time Documentation is
  opened, and it had grown to 137 KB. It is now a short operator guide (about
  25 KB): what to dial, everyday tasks, the options most often changed and the
  most common fixes, each linking to its full reference page.
- The full reference moved, unchanged in substance, to `docs/reference/` (sixteen
  pages and an index). README and SECURITY links point there, the Word and PDF
  manual is built from the guide plus every reference page, and tests keep the
  guide under 32 KiB and fail on any dead link or anchor.

## 0.114.2

**A way back from the console to the dashboard.**

- The dashboard's Console button replaced the page with the terminal, and nothing
  on the terminal led back. Its nameplate now carries a "← Dashboard" link when it
  is the dashboard's own console; the standalone terminal on its own port, which
  has no dashboard behind it, does not show one.

## 0.114.1

**The dashboard keeps the wake-up time you entered.**

- The dashboard rebuilds every line each 4 s, and held off only while a wake-up
  time box had focus. Safari moves focus off the box when Set is clicked, so a
  completed time could be wiped back to empty before the click landed, and Set
  answered "pick a time" without saving anything. A completed time is now kept
  for its line across rebuilds until Set or Cancel succeeds.

## 0.114.0

**The operator console is reached over SSH instead of telnet, and the board no
longer listens on the network at all.**

- **SSH on `console_port` (2300):** `ssh -p 2300 <user>@<ha-host>`, as a
  `console_users` entry. Password login only, with the web terminal's
  per-address lockout (counted separately); while `console_users` has no valid
  entry, every login is refused. Commands, sftp/scp and all forwarding are
  refused. The ed25519 host key is created on first start in `/data` (0600) and
  its fingerprint is logged at every start. No username, password or command
  line is logged.
- **The board listens only on a 0600 root Unix socket**,
  `/run/switchboard/console.sock`, which SSH, the web terminal and the sidebar
  console all connect to. It stops only when `console_enabled` and
  `console_web_enabled` are both `false`.
- **Upgrading:** telnet on 2300 is gone; that port now speaks SSH. An existing
  install keeps its saved `console_bind`, probably `127.0.0.1`, so set it to
  `0.0.0.0` for SSH from the LAN (the new default). SSH needs at least one
  `console_users` entry (now labelled "Console Users"). A blank
  `console_web_bind` now means `127.0.0.1`. `console_enabled: false` no longer
  turns off the web terminal. Sessions: SSH 16 connections (4 per address),
  web terminal 5, sidebar 5; the board's own cap rose from 5 to 32.
- Lines a stranger can cause on the SSH port (a failed or throttled login, the
  no-users warning) are written at most once per address per minute, with a
  count of those held back; a password that SSH's text preparation reduces to
  nothing is skipped, so it can never accept an empty password.
