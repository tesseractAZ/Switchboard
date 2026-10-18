# Changelog

Recent releases. Releases up to 0.108.1 are in the
[changelog archive](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/CHANGELOG-ARCHIVE.md).

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

## 0.110.1

**Changelog opens quickly in Home Assistant.** No change to how the add-on runs.

- Home Assistant renders the add-on's `CHANGELOG.md` whole each time Changelog is
  opened, and it had grown to 207 releases (319 KB). It now holds the ten most
  recent releases; everything older is in `CHANGELOG-ARCHIVE.md`, linked from the
  top of the changelog, which Home Assistant never loads.
- `scripts/rotate-changelog.py` moves the oldest releases to the archive, and
  `tests/test_changelog.py` fails when the changelog grows past 15 releases or
  48 KB, or when a release is missing, duplicated or out of order across the
  two files.

## 0.110.0

**The asterisk user no longer holds the Home Assistant token, and an input-validation
review of the dial plan closed seven gaps.**

- **The Supervisor token stays with the root services.**
  - The token grants the whole Home Assistant Core API.
  - Asterisk, its AGIs, the speech recognizer and every program the dial plan runs
    (the `asterisk` user) now start with `SUPERVISOR_TOKEN` and `HASSIO_TOKEN`
    removed from their environment.
  - s6-overlay keeps a copy of every container variable in
    `/run/s6/container_environment`, which shipped world-readable. It is made
    readable by root only at boot. Before this, any of those programs could read
    the token from it.
  - What they need from Home Assistant they ask a new root service for, the
    Home Assistant broker. It listens on a Unix socket (`/run/switchboard/ha.sock`,
    `0660 root:asterisk`) and forwards only the requests those programs make, each
    with its arguments checked: entity reads, the lights template, the home
    location, calendar reads, `light.turn_on`/`turn_off`, `scene.turn_on`,
    `media_player.play_media` of this add-on's own clip URL, the built-in local
    conversation agent, `sensor.switchboard_last_call` and a persistent
    notification. Everything else is refused and logged as `[ha-broker] refused`.
    Reads are trimmed to what the callers use, so no camera access token, picture
    URL or tracker coordinate is handed back, and a clip must be this add-on's own,
    on this host's web UI port.
  - The callers did not change: `ha_client` uses the broker by itself when it has
    no token. A broker that is down costs those features, never calls, and is
    logged as `[ha_client] Home Assistant unreachable through the broker`.
- **Dial-plan input validation:**
  - **The operator identifies a room by its PJSIP endpoint, never by caller ID.**
    The message-waiting clear used to put the caller's caller ID into the expression
    that gated it and into a shell command line. An outside caller transferred to
    the operator sets their own caller ID. `switchboard-mwi` also refuses anything
    but an extension.
  - **An outside caller transferred to the operator gets no house features.**
    Lights, the status readout, announcements, paging and the assistant are for
    room phones; connecting to a room, the directory, the clock and wake-ups stay
    open. The operator asks which room is calling again after its speech step (an
    attended transfer can change the caller meanwhile), and each feature asks
    again when it starts. A room's `*2` consult to `0` is not a room phone for
    this, and no longer clears that room's message-waiting indicator: dial `0`
    directly.
  - **Only digits, `*` and `#` reach the trunk `Dial`, and only digits reach a room
    `Dial`.** The outbound and room patterns match any character, and `Dial` reads
    `&`, `/`, `@` and `,` in its argument. A number the filter changes is refused.
    `+` is not allowed, which also closes a path around the `011` international
    block.
  - **The call-quality hook filters the channel name** before the shell sees it,
    as it already filtered the caller ID, stage and clip name.
  - **Spoken text can never be read as an option:** every place that starts
    `espeak-ng` passes `--` before the text and closes its stdin.
  - **Operator synonyms are scrubbed** of control characters, like room names, and
    must name a 2-6 digit extension.
  - **The weather forecast URL must be `https://api.weather.gov`,** checked where it
    is fetched, cached and read back from the cache.
- **The manual's typefaces are restored.** The 0.109.1 PDF, the first built on
  Ubuntu 26.04, came out with plain serif headings and proportional inline code:
  pandoc 3.7's template asks for Aptos (absent on Linux) and no longer bolds
  headings. The build now pins the template to Calibri and Cambria with bold
  headings, installs their metric-compatible clones (Carlito, Caladea) for the
  PDF, and maps code to DejaVu Sans Mono. The docs workflow checks each role
  before the build and the embedded faces after it, so a fallback fails the
  check instead of shipping.
- **CodeQL runs on Ubuntu 26.04**, like every other job; 24.04 is retired. Its
  published system requirements still list only 22.04 and 24.04, so the CI gate
  now refuses a Python analysis that reproduces none of the baseline's known,
  accepted findings (`check-sarif.mjs --min-accepted`): an analysis that extracted
  nothing can no longer pass as a clean one.
- **Tests:**
  - Every test file's standalone runner is one shared, discovering runner
    (`tests/_runner.py`). Hand-maintained lists and runner blocks defined above
    later tests had left 182 tests unrun by those entry points (counted by tracing
    each runner); CI's pytest run always ran them. `tests/test_standalone_runners.py` keeps it that way.
  - New: `test_ha_broker.py` (the broker accepts exactly what the programs send,
    generated by calling the real client functions), `test_tts_argv.py`, and tests
    for each dial-plan fix.

## 0.109.1

**Every build and check runs on Ubuntu 26.04.** No change to how the add-on runs.

- The tests, the AppArmor compile, the image builds, the documentation build and
  the release, tag and Dependabot workflows run on `ubuntu-26.04`. The aarch64
  image builds run natively on `ubuntu-26.04-arm`.
- Each job names its runner image instead of GitHub's floating `latest` label,
  which becomes 26.04 on 2026-10-19. The operating system a build runs on now
  changes only in a reviewed diff.
- CodeQL stays on `ubuntu-24.04`: its system requirements list only Ubuntu 22.04
  and 24.04.
- `tests/test_workflow_runners.py` pins every job's runner, including CodeQL's.
  SECURITY.md's CI supply-chain section describes the rule.
- This release's images are the first built on the new runners.

## 0.109.0

**The Asterisk Manager is split into two accounts, and the AppArmor profile's
second wave locks the Asterisk configuration, its spool and the s6 init trees.**

- **No Manager account can run a program.**
  - The single account held `originate` and `system`. With `system`, Asterisk lets
    an `Originate` run `System`, `Exec` or `AGI` and lets `Getvar` read `SHELL()`,
    so one secret could both place calls and run programs.
  - `switchboard-call` (`originate`, `call`) places, ends and transfers calls and
    reads device state and codecs. Asterisk refuses it `System`, `Exec`, `AGI`,
    `SHELL()` and `EVAL()`. Its credentials file is readable by root only.
  - `switchboard-status` (`system`) reads endpoints, contacts, channels and the
    trunk registration, re-registers the trunk and sets message-waiting. It
    cannot originate or run `Getvar`. Its credentials file is also readable by
    the `asterisk` group, for the message-waiting helper the dial plan runs.
  - Both secrets are also in `manager.conf`, which Asterisk must read, and
    `system` lets the status account read Asterisk's configuration. SECURITY.md
    lists what the split does and does not separate.
  - The dialplan functions that read configuration and the environment
    (`func_env`, `func_config`, `func_sorcery`) are no longer loaded. Nothing
    used them, and they let a call-class `Getvar` read configuration values.
  - Neither account holds `command` or `config`, and neither subscribes to
    events. Each account's access list now denies every address before it
    permits loopback; a lone `permit` was a no-op.
  - The secrets live in `/run/switchboard/ami-call.env` and `ami-status.env`, read
    on first use. The services no longer export a shared secret, and the old
    `ami.env` is removed at boot.
  - An action refused for want of a class is reported as an error, not as an
    empty board. Hang up and transfer accept only a real channel name, so
    Asterisk's `/regex/` form, leading blanks included, is refused.
- **The Asterisk configuration has one writer.**
  - `/etc/asterisk`, `/etc/localtime` and `/etc/timezone` are write-locked for every
    process, root included.
  - The configuration generator runs at boot under a nested child profile,
    `<slug>//switchboard_config`. The child lists exactly the files it writes and
    four capabilities, and it runs no program.
  - Only a root process that runs the generator by its path enters the child.
  - `/etc/asterisk` is now root-owned (group `asterisk`, read-only) in the image.
    The package had given it to the `asterisk` user, so Asterisk could rewrite
    its own dial plan.
  - The generator runs Python with `-I`, so environment variables cannot load
    other code into it.
- **Asterisk's spool and the s6 init trees are write-locked:**
  - `/var/spool/asterisk`;
  - `/command` and `/package`;
  - `/etc/s6-overlay` and s6's legacy `/etc` directories.

  The call-file loader `pbx_spool` is no longer loaded: nothing uses call files,
  and a dropped file could have placed a call.
- **The boot step no longer follows links in `/data/state`.** That directory is
  writable by the `asterisk` user. The root step that creates the shared stores,
  repairs their modes and owners and trims the durable log now acts only
  through a no-follow descriptor on a regular file. Before, a link planted there
  could redirect a root `chmod`, `chown` or truncation to any file.
- **Verified on the host's own AppArmor parser** with an isolated copy of the
  add-on.
  - Both accounts were probed action by action; every web-app Manager call and
    each feature code ran.
  - Message-waiting ran as the `asterisk` user, and the backup hooks, restarts,
    an options change and a stop/start all passed.
  - The audit journal held only the six deliberate write probes.

## 0.108.3

**Documentation audit.** No change to how the add-on runs.

- **The Asterisk Manager account is described accurately.** The README, the
  manual and SECURITY.md said the account has no shell-command privilege.
  - It withholds the CLI `command` class, but it holds `system`, which the
    trunk re-register needs.
  - With `system`, Asterisk lets an `Originate` run the `System` application,
    so whoever holds the secret can run a command as the `asterisk` user.
  - That is no more than Asterisk's own dialplan can already do, and the secret
    is readable only by root and the `asterisk` group.
- **SECURITY.md describes the CI supply chain:**
  - actions are pinned by commit SHA;
  - weekly grouped Dependabot version updates keep the pins current;
  - the auto-merge rules: CI passed on the exact commit, Dependabot's own pull
    request, workflow files only, no major-version change.
- **Missed wake-ups are described as they behave since 0.108.2.**
  - The ledger closes with a `missed` row.
  - A wake-up still rings if Switchboard is back within the 10-minute grace
    window.
  - The notification is retried until Home Assistant accepts it.
  - The README's wake-up summary covers misses too.
- **The gateway P-code table lists the Automatic Reboot codes**, including
  `P21931`, the day of the week a weekly schedule uses. The troubleshooting row
  explains a scheduled weekly reboot.
- **The README's install steps use Home Assistant's current menu names**
  (Settings → Apps → Install app), with the older add-on path for reference.
- **Smaller corrections:** a stale section reference; PERFORMANCE.md's test
  count (980); the cordless firmware note in the WP826 tool notes; a CI comment
  that renaming the CI workflow would switch off the Dependabot auto-merge.

## 0.108.2

**A wake-up missed while Switchboard was down is now recorded and reported.**
On 2026-10-04 a power cut took the host down from 01:46 to 08:15, and an 08:00
wake-up did not ring. When Switchboard started again it dropped the wake-up as
too late, which is right, but it left no record except one log line. Its single
notification attempt was made while Home Assistant was still starting, and went
nowhere.

- **The miss is recorded in the delivery ledger** as `missed`, with a `reason`:
  - `not-running`: the wake-up was due before Switchboard started;
  - `grace-expired`: the phone stayed busy or offline through its grace window.
- **The notification names the cause.** It no longer blames a busy phone for an
  outage.
- **The notification is retried every tick until Home Assistant accepts it,** for
  up to an hour (`WAKEUP_MISSED_NOTIFY_RETRY_SECONDS`).
