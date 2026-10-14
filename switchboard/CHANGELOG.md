# Changelog

Recent releases. Releases up to 0.106.2 are in the
[changelog archive](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/CHANGELOG-ARCHIVE.md).

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

## 0.108.1

**Documentation brought in line with the AppArmor review.** No change to how the
add-on runs.

- **The manual's troubleshooting table has a row for a profile refusal.** A feature
  that fails with `Permission denied` after an update leaves a `DENIED` record in
  the host's audit journal (`journalctl _TRANSPORT=audit`), not in the Log tab or
  `dmesg`. The row says how to find the record for this add-on and what its
  fields mean. It also notes that `apk add`, `pip install` and editing a shipped
  file in place now fail inside the container by design.
- **The restart-or-stall row describes the verified gateway behaviour.** With
  `P81` set, the gateway clears its registrations as it boots. So
  `Removed contact … due to request` and `Added contact` appear together, in the
  same second, at the end of the outage, and a power cut leaves them too.
- **SECURITY.md describes the profile exactly:**
  - no execute is ever refused, and no read outside `/root`, `/home` and the
    secret and kernel paths it lists;
  - what a write-lock refuses (writes, create, delete, rename, `mkdir`,
    truncation, hardlinks, `chmod`, `chown`);
  - that `/run` and `/tmp` are deliberately left unlocked;
  - how it was checked after deployment.
  The 0.108.0 notes said no read is ever refused, which was not true of those
  paths; that entry is corrected.
- **The profile's header gives the test procedure:** the CI job, the tests that
  pin the profile, and the isolated boot test under a test name that a release
  must pass.
- **The README and the manual's summaries mention** that Asterisk runs unprivileged
  and that the profile refuses unused capabilities and writes to shipped code.
- **Corrections:**
  - PERFORMANCE.md's test count is 979, not 960.
  - Asterisk's `Unable to install capabilities` start-up warning was confirmed
    gone after the 0.108.0 deploy.

## 0.108.0

**The AppArmor profile is narrowed.** It granted every capability, and every file,
signal and network access. It now removes what no process in the add-on uses:
- **Capabilities are an explicit list.** chown, dac_override, fowner, fsetid,
  kill, setgid, setuid and net_bind_service are allowed. Everything else is
  refused, including net_raw, which with host networking would allow sniffing
  on the LAN.
- **Shipped code is write-locked** for every process, root included: `/usr`,
  `/bin`, `/sbin`, `/lib`, `/init`, and Asterisk's AGI, sound and
  music-on-hold trees. A lock refuses writes, hardlinks, chmod and chown.
- **Explicit refusals:** reading `/etc/shadow`, writing kernel tunables,
  reading firmware and LSM state, mount, and tracing another process.

`file,`, `signal,` and `network,` stay broad, so no execute is ever refused,
and no read except of the secret and kernel paths above and of `/root` and
`/home`. s6-overlay re-reads `/init` when the container stops, and a per-path
list that misses one read crash-loops the container. Every new deny is audited
except the `/root` and `/home` ones, which are quiet: interactive `docker exec`
shells try to keep their history there.

The profile was compiled fresh with the host's own parser (AppArmor 3.1.7) and
loaded under a test name. An isolated copy of the add-on then ran under it, with
its own network and volumes, through:
- boot;
- one call to each feature code;
- the backup hooks;
- service restarts;
- a full stop and start.

The only denials recorded were the three deliberate write probes. On HAOS those
records go to the host's audit journal (`journalctl _TRANSPORT=audit`), not to
`dmesg`; the profile header and SECURITY.md now say so. A new CI job compiles
the profile on every change, and tests pin its shape.

**Asterisk starts as the `asterisk` user, with no permitted capabilities.** It
used to start as root and switch user itself (`-U`/`-G`). That kept root's
permitted capability set, because Asterisk asks to keep capabilities so that it
can hold on to net_admin, which the container never grants. A compromised
Asterisk — the process that talks to the SIP trunk — could then have raised
dac_override, setuid or chown again. It now starts through `s6-setuidgid`, as
whisper-server already did. Verified in the boot test: its permitted and effective
capability sets are both empty.

**Python bytecode is compiled into the image.** Nothing wrote `__pycache__` at
build time, so root services wrote it under `/usr` at runtime. The AGIs, running
as the asterisk user under root-owned code, could not write it, so they
recompiled every module on every call. `PYTHONDONTWRITEBYTECODE` is now set and
the bytecode is precompiled.

**The Asterisk start script no longer chowns its sounds and AGIs.** Only
`/var/lib/asterisk` itself (for astdb) and `keys/` are given back to the
asterisk user. A recursive chown walked the whole sound tree on every start, and
under the write-lock each file would be refused and logged.

**Corrections:**
- `/run` is not a tmpfs in this container. Several comments and SECURITY.md
  called it RAM-backed. The AMI secret and the announcement clips there sit in
  the container's own writable layer on disk, which the Supervisor discards
  when it recreates the container at every add-on start. They never go into
  `/data`, `/share`, `/config` or a backup.
- The gateway's command shell takes bare numbers: `get 4200`, not `get P4200`,
  which prints `(null)` whatever the value. The manual (§7.3) said otherwise.
- `.dockerignore` sat at the repository root, outside the `./switchboard` build
  context, so no build read it. It now lives in `switchboard/` and also
  excludes `.pytest_cache`.

## 0.107.0

**`log_level` now does what its documentation says.** The manual, SECURITY.md
and the option's help text all said that a `log_level` of `notice`, `warning`,
`error` or `critical` keeps Asterisk's verbose trace out of the add-on's Log tab.
That trace includes every dialled and calling number. It did not: Asterisk
always sends verbose lines to its console and drops there only the ones above
its `-v` count, and the add-on started it with `-vvv` at every level. Those four
levels now start Asterisk without `-v`. Asterisk still prints level-0 verbose
lines at any `-v`, so the add-on's own per-call `[rtpqos]` summary and
`Operator dial` lines move from level 0 to level 1: they leave the Log tab with
the trace at those four levels and stay in the durable log. A few of
Asterisk's own start-up and shutdown messages are level 0 and still appear.
`info` (the default), `debug` and `trace` are unchanged. The count is decided in one place, next to the logger
configuration, and handed to the start script through
`/run/switchboard/asterisk-verbosity`. Verified on Asterisk 20.11.1 with a
second, module-less instance: without `-v` its console printed none of the
verbosity-3 lines that its log file received.

**The durable log keeps its diagnostic lines at every `log_level`.**
`/data/state/asterisk.log` is now configured `verbose(3)` (it was `verbose(2)`).
In Asterisk 20.11.1 the number filters nothing that reaches the file. It sets
the level Asterisk produces verbose lines at, so the file keeps its
verbosity-3 lines — `Contact <n>/… is now Unreachable`, `Added contact` and
`Removed contact … due to request` — when the console runs without `-v`. The
documentation called the `Contact` line verbosity 2; it is verbosity 3.

**Asterisk's log lines carry the full date, milliseconds and UTC offset.**
`[Oct  1 01:00:17]` is now `[2026-10-01 01:00:17.289 -0700]`, in both log files
and on the notices, warnings and errors in the Log tab. A line can now be
placed against `heartbeat.jsonl` (UTC), the gateway's own log, and lines from
another year. The scrub of the readable copy and the outage reader key on the
message, not the date, and are tested with the new stamp.

**The manual explains how to tell a gateway restart from a network stall.** A
gateway that is back within its registration window renews most registrations
instead of re-creating them, so a restart can look like a short outage in which
most phones stayed registered. The troubleshooting table now says where to look:
the gateway's System Up Time, its Automatic Reboot schedule (`P21929`, `P21930`,
`P28118`), and `P81` (Unregister On Reboot). With `P81` set to `1`, a restart
leaves `Removed contact … due to request` in the durable log, which a stall
never does. `P81` is added to the gateway P-code table.

## 0.106.3

**With wake-up calls turned off, a repeated announcement is no longer held back
for a replay that cannot happen.**

The automatic announcement retry runs inside the wake-up scheduler, and that
service idles when wake-up calls are off. The web UI did not know that. If an
announcement never played, it still answered an identical repeat "duplicate,
already on its way", waiting for a replay that would never come, for up to about
three minutes. The web UI now reads the same switch the scheduler does, and with
wake-up calls off it treats the retry as off.

**The documentation was brought up to date with the code.** An audit of the
README, the manual (DOCS.md), PERFORMANCE.md, SECURITY.md and the configuration
help text found statements that had fallen behind recent releases, all now
corrected. Among them:
- The web UI port has three narrow LAN exceptions: speaker announcement clips,
  the cordless phonebook, and the token-gated announce API. It does not refuse
  every non-Supervisor connection.
- The talking clock code `41` waits for the dialling timeout, because it is also
  the start of directory assistance `411`.
- Call-quality alerts now cover wake-up, page and announcement calls too.
- The console bind defaults and what they affect.
- The announcement responses, retry limits and duplicate rules.
- Which services read and write the shared ledgers.
- Test counts, and several dates and version numbers.

**The release manual now shows the released interface.** The manual attached to
each release used to be built before that release's screenshots were rendered,
so it pictured the previous version's interface. The documentation build now
renders the screenshots itself before building the manual.
