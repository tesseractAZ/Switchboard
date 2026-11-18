# Changelog

Recent releases. Releases up to 0.110.1 are in the
[changelog archive](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/CHANGELOG-ARCHIVE.md).

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

## 0.113.0

**A new look for the dashboard, the telnet console and the browser terminal: the
operator's position of a 1940s cord switchboard.** Nothing behaves differently.

- **Dashboard.** A walnut board with an engraved brass nameplate. Each line is a
  jack position: a lamp cap above a brass jack ring, beside a cream designation
  strip with the typed number, room name and state. Lamps are unlit for unknown,
  white for registered, amber for in use, flickering amber for ringing and red
  for offline, and every lamp also states its condition in words. On first paint
  the lamps warm up in turn; with reduced motion they appear lit. Buttons are
  typed words instead of emoji. The page keeps one theme whatever Home
  Assistant's light or dark setting is.
- **Telnet console.** A brass nameplate rule, lamp glyphs on each position
  (`◉` lit, `○` unlit, `✕` fault) with dotted leaders, amber for calls and
  ringing, in 256-colour ANSI. Every key binding and view is unchanged.
- **Browser terminal.** The terminal takes the same palette and typeface, with
  the page chrome as the brass nameplate.
- **Fonts are part of the add-on.** Federo (the nameplates) and Cutive Mono (the
  typed text), both under the SIL Open Font License, are served by the add-on
  itself: nothing is fetched from the internet, so the interface looks the same
  with the WAN down and makes no request that would reveal the household's
  address. Both web servers answer only the two file names, behind their
  existing guards.

## 0.112.0

**Phones may register only from the LAN, and registrations survive a restart.**

- **LAN-only room phones.** Every room endpoint now refuses a request from, or a
  contact at, an address outside the private ranges (`10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16`). A room secret, including the
  never-registered test extension's, is useless from outside even where the SIP
  port is reachable. The trunk has its own endpoint and is not affected.
- **Registrations are kept across updates and restarts.** Asterisk's internal
  database, which holds each phone's registered contact, moved from the container
  layer the Supervisor discards to `/data/state/astdb.sqlite3`. Restored contacts
  are qualified within 4 s of start-up (`max_initial_qualify_time`), so a phone
  still registered when the add-on stopped is reachable again within seconds
  instead of after its own next REGISTER (about a minute). The first start on
  this version begins with an empty database. The move is made in the image
  build: the boot pass's AppArmor profile may not write `asterisk.conf`.

## 0.111.0

**Fixes from a log review: log lines that say what actually happened, health sensors
that stay honest across a restart, and two web-UI hardenings.**

- **Web UI**
  - Refused requests on the web UI port are now logged with the client address, the
    method, the path and a reason (not from ingress, missing token, bad token, or
    LAN announce off), never the token, the headers or the query string. Only the
    first refusal per address and reason in each 10 minutes is logged in full; the
    rest are counted into one summary line (also written when the web UI stops), so
    these lines cannot use up the journal rate limit every container shares. The
    web server's own line for each refused WebSocket handshake or malformed request
    is unchanged and still written once per attempt.
  - The announce token is compared in constant time.
  - Speaker announcement clips are named from 64 random bits instead of the speaker
    script's process id and the clock, and are deleted five to six minutes after
    they are written. A once-a-minute sweep removes them even when no further
    announcement follows; handset clips are swept on the same schedule.
  - A test ring and an MWI change from the dashboard each leave a `web:` line in the
    log, including when the change was a no-op or Asterisk refused it.
- **Wake-ups and announcements**
  - `scene <id> fired for ext N` is logged only when Home Assistant accepted the
    call; otherwise the line says the scene was NOT fired. Before, `fired` was
    logged whatever the outcome.
  - `wakeup_retry_seconds` is raised to at least `wakeup_ring_seconds` plus one 20 s
    poll, with a WARNING naming both values at every start, and the start-up line
    shows the effective retry. With a ring within 20 s of the retry option's
    600 s maximum the clamp is expected, and the WARNING says so instead of asking
    for a value the form refuses.
  - While the handset is busy, the announcement-retry line says the replay is
    deferred because the handset is busy, instead of "is waiting for the handset".
  - The retry's `ActiveChannels` check stays. Asterisk 20.11.1 always reports that
    field blank; the upstream fix (first in 20.15.0) makes it a channel count, and
    the check already treats blank and `0` as idle.
- **Health monitors**
  - On a cycle where Asterisk's manager interface did not answer, `heartbeat.jsonl`
    wrote `wired_down: []`, which on every other row means no gateway port is down.
    It is now `null`, like `reachable`, and `down` is present and `null` too.
  - Gateway health no longer reports the previous run's reading after a restart. A
    link-health rollup measured before the device-health monitor started reads
    `unknown`, which never alerts, until a new one arrives, instead of `ok` with
    every port up while the ports were still re-registering. A real gateway alert
    straight after a restart can come one cycle later.
- **Logs and ledgers**
  - Each call-quality record now says what became of the
    `sensor.switchboard_last_call` update, as `sensor_status` (`posted`, `failed`
    or `skipped`) next to `notify_status`. The recorder runs detached with its
    output discarded, so a failed update used to leave no trace. Schema version 6.
  - The Home Assistant broker writes one traffic line an hour when there was
    traffic, counting requests by kind and outcome (`ok`, `http<code>`,
    `unreachable`, `refused`), and the partial interval when it stops. A broker or
    Home Assistant failure seen by the call-quality recorder never reached the log,
    contrary to a 0.110.0 note; the hourly line and `sensor_status` now cover it.
  - `switchboard-mwi` says when nothing changed (`MWI for 13 already clear ...`)
    instead of `cleared MWI for 13 (store + Asterisk)`.
  - The `rec` lines of the operator and of the status, directory and speaker-announce
    menus give the recording's length and
    how it ended (`bytes=... secs=... end=<reason>`), and end in `no-speech` when
    the phone sent nothing above the silence threshold, so "said nothing" can be
    told from "spoke briefly".
  - whisper-server's standard output is discarded. It was block-buffered, so its
    per-request lines reached the log hours late under one timestamp, were spliced
    into other lines, or were lost at stop. Its standard error and
    `switchboard-stt`'s own line per request are unchanged.
  - `backup-window.jsonl`, the one `/share/switchboard` ledger with no cap, is
    trimmed to its newest half once it passes 256 KiB.
- **Documentation and comments**
  - `backup-window.jsonl` brackets the Supervisor's copy of this add-on's own image
    and `/data`. When a backup includes `/share`, the Supervisor archives it after
    every add-on in that backup, with its writers still appending.
  - The telnet console and the web terminal listen on the host's loopback, which
    every host-network add-on and process shares.
  - With `log_level` `info` or more verbose, the add-on journal also records the
    host's LAN IPv4 address and its IPv6 address at every Asterisk start, and at
    every level the web UI's refused-request lines (the requester's address,
    method and path).
  - Heartbeat `transitions` are per poller process. Corrected comments on silent
    recordings (about 16 kB, 1.02 s), the low-MOS ledger (no comparison set is
    kept), the call-quality jitter fields and leg identity, and the durable log's
    growth.
