# Switchboard

**An instrumented private telephone exchange for analog handsets.** Switchboard is
an **Asterisk 20 + PJSIP** PBX packaged as a native **Home Assistant add-on**. It
turns rotary desk sets, Western Electric wall phones and a WiFi cordless into one
exchange with an on-box voice operator. Then it measures that exchange: every link
between calls, both audio directions of every call leg, and every wake-up and
announcement, which it tracks until a ledger records whether it arrived.

> **Scale:** ten supervised services and about 25,000 lines of service code.
> **Verification:** 1,218 tests, mutation testing on selected releases, and CodeQL
> `security-extended` full scans on every pull request.
> **Runtime:** a narrowed AppArmor profile; G.711 µ-law is the only codec on the
> wire, so calls are never converted between codecs; speech recognition and
> synthesis run on the box, so no speech goes to a cloud service.

| | |
| --- | --- |
| **Telephony** | Room-to-room calling, a speech-driven operator, directory assistance, a talking clock, wake-up calls, house-wide paging, announcements onto speakers *and* handsets, a local voice assistant, and an optional SIP-trunk outside line (dial `9` first, or, with direct dial, `1` and the 10-digit number) |
| **Link measurement** | Registration and qualify round-trip time for every phone between calls; a second detector catches outages shorter than the poll interval; mass-outage detection with hysteresis; trunk watchdog with automatic re-registration |
| **Call quality** | One leg of every call is scored from RTP/RTCP: the worse direction's MES, loss in both directions, mean and peak round trip across the whole call, and one-way audio, with jitter recorded alongside. A filter discards the placeholder values Asterisk reports when it has nothing to measure |
| **Device health** | Gateway port health, plus the cordless's battery, WiFi signal and own MOS. A low score from the handset counts only when the PBX's record of the same call corroborates it |
| **Delivery integrity** | Wake-ups and handset announcements are written to a ledger. A reconciler judges each one it sees through, and a failure brings a re-ring, a retry or an escalation that names its cause |
| **Security** | A Supervisor-only dashboard, a split Manager interface with secrets rotated every boot, a PBX that never holds the Home Assistant token, LAN-only room registration, toll-fraud controls and a confined runtime |

---

## Screenshots

**The dashboard** in the Home Assistant sidebar. It shows every phone's registration
and call state, the trunk registration, the speech engine's health and each phone's
latency. One click rings a phone, patches two rooms together, hangs up, transfers,
pages, or sets and cancels a wake-up:

![The Switchboard dashboard in the Home Assistant sidebar](switchboard/docs/img/dashboard.png)

**The operator console** is the same board full-screen, driven by keystroke. It
opens from the Switchboard panel in the Home Assistant sidebar. It also answers
over SSH on port 2300, with a password:

![The full-screen operator console](switchboard/docs/img/console.png)

---

## Telephony

- **Room-to-room calling** between every analog phone in the house.
- **A voice operator (dial `0`).** Say a room name ("Kitchen"), a spoken extension
  ("one four"), or a feature: **"time"**, **"weather"**, **"wake me up"**,
  **"lights"**, **"directory"**, **"page the house"**, **"announce"**. Recognition
  is **whisper.cpp** on the box. A confident room name beats the time, weather,
  directory, page and announce words; "wake me up" and "lights" are checked
  before any room name. The operator asks again rather than guess wrong.
- **Directory assistance (`411`)**, a **talking clock (`41`)**, **dial-a-status
  (`45`)** for live Home Assistant readings, and a **lights menu (`43`)**.
- **Wake-up calls (`42`).** Speak a time ("seven thirty", "quarter past six"). The
  phone rings at that time and speaks it. It can also raise a Home Assistant scene,
  read the weather and announce your next calendar event. Wake-ups can be set and
  cancelled from the dashboard and the console too. Delivery is verified (see
  [Delivery integrity](#delivery-integrity)).
- **Paging (`44`)** from every handset at once, and **announcements (`46`)**
  played out of Home Assistant speakers between station chimes. An HTTP endpoint
  lets Home Assistant or another add-on speak an alert *onto a handset*, which
  makes any phone, the WiFi cordless included, an announce target.
- **A local voice assistant (`47`, off by default)** that answers through Home
  Assistant's built-in conversation agent.
- **An outside line.** Enable a SIP trunk for inbound and outbound calls. Dial
  the prefix (`9` by default) first, or turn on **direct dial** and dial `1` and
  the 10-digit number like a cell phone. Requiring the leading `1` keeps the
  operator and most feature codes dialling instantly. A room numbered `12`–`19`
  is also the start of an 11-digit number, so it sends after the gateway's
  inter-digit timer, or at once on `#`. Toll-fraud controls apply throughout. The
  trunk is off by default; room-to-room calling doesn't need it
  ([dial plan & the send delay](switchboard/docs/reference/gxw4216-gateway.md#74-dial-plan--the-send-delay)).
- **A live dashboard and a full-screen operator console.** The console runs in the
  Home Assistant sidebar, over SSH, or in a standalone browser terminal, and shows
  the same live signals as the dashboard. The dashboard adds a Lights panel:
  every Home Assistant light, grouped by area, each with its own on/off toggle.

---

## Measurement and monitoring

Three monitors publish their headline results as Home Assistant sensors and keep
the detail in their ledgers. They raise a notification only when something is
actually wrong, and the recovery
notice replaces the outage notice rather than piling up beside it. Full detail is
in [Health monitoring & sensors](switchboard/docs/reference/health-monitoring.md).

### Link health, measured between calls

- **Per-phone registration and qualify round-trip time.** These are polled
  every five minutes by default (`link_health_interval`), so a degrading link (usually the WiFi cordless) shows on a graph
  before a call ever drops. The monitor publishes `sensor.switchboard_link_<ext>`
  for each phone and a rollup with the worst RTT, counts of reachable,
  unreachable and offline phones, and the extensions that are down.
- **A detector for outages shorter than the poll interval.** A point sample can't
  see an outage that starts and ends between two polls. The one outage on record
  that began and ended between two samples took all eight wired phones down for
  119 seconds and fell entirely inside one five-minute gap. So each cycle also reads the reachability changes Asterisk logs as they
  happen, and it reports half the fleet going dark *between* samples even when
  both samples look healthy.
- **Mass-outage alerting with hysteresis.** It needs at least half the fleet
  *and* at least three phones unreachable for two consecutive cycles. A shared
  gateway failure can't go unnoticed, and a single blip doesn't page anyone.
- **Restart-aware sampling.** After a restart the poller checks every 15 s until
  every gateway port and every phone that has ever registered is back, bounded to
  eight checks. Contacts are restored from a persisted Asterisk database and
  re-qualified within 4 s of start-up, so phones are callable at once instead of
  waiting out their re-register interval.
- **Trunk self-recovery.** The registration retries for about seven days rather
  than Asterisk's default ten attempts. A watchdog sends a native `PJSIPRegister`
  whenever the registration reads Rejected or Stopped, and publishes
  `sensor.switchboard_trunk_health`.

### Per-call media quality

- **One leg of every call is recorded** in a schema-versioned ledger. It is the
  leg that runs the dial plan: the calling phone, the inbound trunk, or the phone
  the PBX rings for a wake-up, page or announcement. That covers conversations,
  the operator and voice menus, outside calls, transfers, wake-up delivery, paging
  and announcements. The answering phone's side of a dialled call is not
  measured.
- **The worse direction decides.** The leg is graded on the *worse* of its two
  audio directions. The inputs are Asterisk's RTCP-derived Media Experience Score
  and loss in each direction. Jitter is recorded beside them and feeds only the
  glitch filter below.
- **Round trip is scored across the whole call, not its last moment.** It alerts
  on a **mean over 250 ms** or a **peak over 500 ms** across every RTCP round. In
  this deployment one leg peaked at 845.93 ms and reported 10.65 ms on its final
  round. A round-trip check on that final round alone stayed silent; checked on
  the peak, the leg raises an alert.
- **One-way audio** is detected from packet counts, so it fires even when no
  score is available.
- **Credibility filtering.** Asterisk reports `0.0` for a direction it couldn't
  score and the literal `88.087887` when no RTCP round completed. The first reads
  as a dead call and the second as a flawless one. Both are recognised and
  discarded. So is the collapsed-score signature of a re-INVITE or transfer glitch
  (MES under 40 with loss under 0.5 %, jitter under 40 ms and a round trip under
  80 ms). Each record
  also says whether its RTT figures rest on one RTCP round or several.
- **The alert path checks itself.** Each leg records whether its notification was
  actually posted (`notify_status`) and whether its sensor update was accepted
  (`sensor_status`). A failure that used to vanish silently now stays on record.

### Device health

- **Gateway.** Port health is derived from the link-health rollup, and all wired
  ports down reads as the gateway losing power or its uplink.
- **WP826 cordless.** The monitor reads the handset's battery, WiFi signal and its
  own MOS for the last call. A low MOS from the handset marks it degraded only
  when the PBX's record of the same call corroborates it. The cordless's IP
  address is followed automatically when it changes.
- **Two-cycle hysteresis** applies on both devices. A worsening from degraded to
  critical alerts again, and a recovery notice closes the episode.
- **Speech engine.** The dashboard and console probe the resident whisper.cpp
  server and read the result against the configuration. A server idled on purpose
  shows `disabled`, never `down`, and recognition falls back to a per-call process if it is
  missing.

---

## Delivery integrity

An alarm clock that never went off is a failure with a deadline. Switchboard
doesn't stop when it has queued a call; it records what actually happened.

- **Every wake-up and handset announcement is written to a delivery ledger.** A
  reconciler in the wake-up scheduler judges each call it sees through:
  delivered, undelivered, or not judged when the PBX was still restarting. A call
  in flight across an add-on restart ends without a verdict, and nothing is judged
  while wake-up calls are disabled.
- **A wake-up counts as delivered** only once the script has reached the greeting
  *and* at least a second of audio has left the box. A phone picked up in silence
  is undelivered. An unanswered wake-up is rung again if the phone is registered
  and idle. One that still isn't delivered becomes a critical push notification,
  or a Home Assistant notification if no push target is set or the push fails.
  The wake-up may never ring at all, because the phone stayed busy or offline
  through the 10-minute grace window or because Switchboard itself was down. Then
  it is recorded as missed, with the cause, and the notification is retried for
  up to an hour until Home Assistant accepts it.
- **Announcements are checked before and after.** A pre-flight device check
  refuses to ring an endpoint that is already unreachable. An announcement whose
  audio never played is sent again once the phone reports itself registered and
  idle (`announce_retry_attempts`, default 2, each within 150 s of the original
  send). An identical repeat to the same phone within five minutes, with
  nothing different played to it in between, is answered as a duplicate, not
  played twice.
- **[Measured behaviour](switchboard/PERFORMANCE.md)** is built from these
  ledgers. Every figure names the rows it is drawn from, and a table lists which
  detectors have ever fired, with the reason each silent one is silent: healthy,
  never exercised, or unable to fire.

---

## Security engineering

- **The dashboard is Supervisor-only.** Its port refuses every peer that isn't the
  Supervisor, WebSocket upgrades included. Three narrow LAN exceptions exist:
  - speaker clips (`/announce/a<digits>.wav`), a read-only GET;
  - the cordless phonebook (`/phonebook.xml`), a read-only GET;
  - `POST /api/announce/<ext>`, which needs the `announce_token`. That token is
    blank by default, which keeps the path closed.

  Host networking means the port *does* listen on your LAN. The guard, not the
  bind, is what closes it.
- **The Asterisk Manager is loopback-only and split.** Call control and status
  reads use separate accounts, and each gets a fresh random secret on every boot.
  Neither account can run a program or use the CLI `command` class.
- **The PBX never holds the Home Assistant token.** Asterisk runs as an
  unprivileged user with no capabilities. A root broker forwards only the requests
  Switchboard's features make.
- **The runtime is confined.** The AppArmor profile grants capabilities from an
  explicit allow list and refuses every other one. It also refuses any write to
  the shipped code or the Asterisk configuration, root included. The only
  exception is the configuration renderer, which runs under its own child
  profile.
- **Network policy:**
  - Room phones may register only from private LAN addresses.
  - The trunk blocks international and premium prefixes.
  - Every transfer stays on internal destinations.
  - `911` and `933` are answered locally with a spoken notice that these phones
    can't reach emergency services.
- **Secret handling.** Generated files that hold secrets are created `0640`, or
  `0600` where only root reads them, with no umask window. Room and trunk credentials are validated and fail closed on
  characters Asterisk would silently truncate. Secrets never reach a log, and the
  readable log copy is scrubbed of SIP accounts and LAN addresses.
- **Consoles.**
  - **SSH console (`2300`):** password-only, checked against `console_users`. It
    refuses every login until a user is configured, and also refuses exec, SFTP
    and port forwarding.
  - **Browser terminal (`8100`):** defaults to `127.0.0.1` and has no login until
    `console_users` has an entry, so configure one before you move its bind.
- **Change the default room secrets before your phones register.**

The threat model, the toll-fraud defences, the accepted LAN-local risks and the
reporting process are in [SECURITY.md](switchboard/SECURITY.md).

---

## How it's verified

- **1,218 tests** (about 31,000 lines) run in CI on pull requests and pushes to
  `main`.
- **Mutation testing on selected releases.** Releases 0.77–0.80 and 0.96–0.100
  applied 130 deliberate defects to the source, and their release records count
  every one as caught by a named test. The 17 that survived their first run each
  drove a new test or a code change, and the failure patterns
  are written up in [PERFORMANCE.md §6](switchboard/PERFORMANCE.md#6-test-and-mutation-coverage).
- **CodeQL `security-extended`** scans Python and JavaScript. Pull-request scans
  are full scans, not diff-informed ones. The gate fails on any high-severity
  finding missing from a small, path-pinned baseline, and it fails closed if that
  baseline can't be read. It also refuses a Python analysis that reproduces none
  of the known findings, so an extractor that silently scanned nothing can't pass
  as clean.
- **Supply chain.** Every GitHub Action is pinned by full commit SHA, and every
  job names its runner image instead of a floating `latest`.
- **These gates also run in CI on pull requests and pushes to `main`:**
  - the AppArmor profile compiles;
  - the image builds natively on `amd64` and `aarch64`;
  - the version is consistent.

  The test suite also holds the documentation to its size caps and a complete
  index, with no dead links or anchors.

---

## Why G.711 µ-law only

Switchboard is **G.711 µ-law on every leg**, and it isn't configurable — on
purpose. An antique analog handset is narrowband *by physics*: the carbon/electret
element and the two-wire loop top out around 300–3400 Hz, so wrapping it in G.722 or
Opus buys nothing the transducer can reproduce. Pinning one codec everywhere means
**no call is converted between codecs on the wire** — lowest latency, and dial tone, ringback, and fax/modem
tones pass through cleanly. See
[Codecs — G.711 µ-law only, on purpose](switchboard/docs/reference/codecs.md).

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

1. **Add the repository** to Home Assistant → Settings → Apps → Install app →
   ⋮ → Repositories → `https://github.com/tesseractAZ/Switchboard`, then install
   **Switchboard**. (Before Home Assistant 2026.2, apps were called add-ons: Settings →
   Add-ons → Add-on Store → ⋮ → Repositories.)
2. Open **Configuration** and define one entry under `rooms` per phone — a unique
   `ext` (2–6 digits), a friendly `name`, and a strong `secret` (the SIP password
   that port will use). **Change the `change-me-…` placeholder secrets.**
3. **Start** the add-on. The **Log** tab shows `switchboard-config` rendering the
   configuration, then Asterisk starting.
4. **Provision the gateway**
   ([Grandstream GXW4216 V2 provisioning](switchboard/docs/reference/gxw4216-gateway.md))
   so each FXS port registers — SIP server = your Home Assistant IP, transport UDP,
   and per port the User ID / Auth ID = the extension and the password = that
   room's `secret`.
5. Open the **Switchboard** panel in the sidebar; each room flips to **Registered**
   as its port comes online. Pick up a phone and dial another room. Done.

Changing options and restarting the add-on regenerates the Asterisk
configuration files the add-on controls — **the add-on options, plus the optional
[options overlay](switchboard/docs/reference/configuration.md#the-options-overlay-advanced)
file, are the single source of truth**; hand edits to those files are
overwritten on every start.

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
| `47`  | Local voice assistant — ask Home Assistant anything (**off by default**) | `assistant_ext` |
| `411` | Directory assistance | `directory_ext` |
| `911` | Answered with a spoken notice that these phones **cannot reach emergency services** | always on |
| `933` | The FCC's number for testing an emergency line — answered with the **same spoken notice as `911`**, never dialled out. Nothing is read back: there is no carrier registration to read. | always on |

Every feature code `41`–`47` and `411` is configurable (`*_ext`) and can be
disabled (`*_enabled`); dial `0` (the operator) can be turned off but not
re-assigned. `911` and `933` are always emitted and cannot be moved. On a live call, analog phones
blind-transfer with `##` and attended-transfer with `*2` (internal destinations
only). The table shows the defaults.

## How it's built

- **Asterisk 20 + PJSIP** is the only telephony engine; `chan_sip` is not used.
  On every start, `switchboard-config` regenerates the nine Asterisk configuration
  files the add-on controls (PJSIP, dial plan, Manager, modules, logger and four
  more) from the add-on options. The running PBX always matches what you declared,
  and hand edits can't drift.
- **Ten supervised services under s6-overlay.** Those behind an option idle when
  it is off. `asterisk`, `webui` and `ha-broker` always run, and `rtpmon` keeps
  scrubbing the readable log when link health is off:

  | Service | Role |
  | --- | --- |
  | `asterisk` | The PBX |
  | `rtpmon` | Link health, the trunk watchdog and log scrubbing |
  | `devhealth` | Gateway and cordless health |
  | `wakeup-scheduler` | Wake-ups, announcement retry and the delivery reconciler |
  | `whisper-server` | The resident speech recognizer |
  | `webui` | The Ingress dashboard (FastAPI over the Manager interface) |
  | `ha-broker` | The token broker |
  | `operator-console` | The operator console |
  | `console-ssh` | The console over SSH |
  | `console-web` | The console in a browser terminal |

  A per-call scorer, `switchboard-callqos`, runs from the dial plan at every
  hangup.
- **Offline voice.** Speech-to-text is **whisper.cpp** (the English `base.en`
  model, kept resident in RAM with a per-call fallback). Text-to-speech is
  **espeak-ng**. Both run on the box, with no cloud service and no API key.
- **Durable, bounded ledgers.** Link heartbeats and delivery outcomes are
  append-only JSONL ledgers, each capped in size and trimmed at a line boundary.
  The call-quality ledger keeps the newest 300 legs, schema-versioned and
  rewritten atomically on each write; its `/share` mirror is append-only and
  size-capped. The voice
  assistant keeps its own private ledger of what it heard and why each turn
  ended.
- **The image** builds on the Home Assistant **Alpine** base in two stages,
  compiling whisper.cpp from source. It runs under an AppArmor profile, with host
  networking for SIP and RTP on your LAN.

```
Antique analog phones ──FXS──▶ Grandstream GXW4216 V2 ─┐
WP826 WiFi cordless ──────WiFi─────────────────────────┤   each phone = 1 SIP extension
                                                        ▼
                             Home Assistant host (LAN, host network)
                             ┌─────────────────────────────────────────┐
                             │  Add-on: Switchboard                     │
                             │   • Asterisk 20 + PJSIP  (SIP / RTP)      │
                             │   • Link + call-quality measurement       │
                             │   • Delivery ledger + reconciler          │
                             │   • Voice: whisper.cpp STT + espeak TTS   │
                             │   • Ingress dashboard, SSH + web console  │
                             │   • Token broker → Home Assistant sensors │
                             │   • Config generated from add-on options  │
                             └─────────────────────────────────────────┘
                                          │ (optional) SIP trunk
                                          ▼
                                    outside line (PSTN)
```

## Documentation

- **[Operator guide](switchboard/DOCS.md)** — the short guide the add-on's
  Documentation tab in Home Assistant shows.
- **[Complete system reference](switchboard/docs/reference/README.md)** —
  installation, every configuration option, the voice features, gateway and
  cordless provisioning, the SIP trunk and direct dial, the operator console,
  health monitoring and sensors, codecs, troubleshooting, and reproducing the
  system on new hardware.
- **[Security](switchboard/SECURITY.md)** — the security model, toll-fraud defenses,
  the accepted LAN-local risks, what you must configure, and how to report an issue.
- **[Changelog](switchboard/CHANGELOG.md)** — recent releases; earlier ones are in the [changelog archive](switchboard/CHANGELOG-ARCHIVE.md).
- **[Measured behaviour](switchboard/PERFORMANCE.md)** — what the system is
  observed doing in service, from its own ledgers: call quality, link health,
  wake-up and announcement delivery, the voice assistant, whether anything is
  transcoded, which alarms have ever actually fired, and test coverage.
- **Printable manual** — a [GitHub Release](https://github.com/tesseractAZ/Switchboard/releases)
  carries the README, the operator guide, the reference and the security model
  assembled into a **Word (`.docx`)** and **PDF** you can read offline. (Releases v0.69.0 through v0.80.0 shipped
  without one — the build waited on an event a bot-created release does not
  emit. Repaired in v0.81.0; every release since carries both files.)

Every version from v0.43.0 on is tagged and released, so `git checkout vX.Y.Z`
reproduces the exact source that built it. One older version, **v0.42.1**, was
never tagged: tagging was manual until v0.46.1 and that one was missed. See
[*Reproducing on new hardware*](switchboard/docs/reference/reproducing.md).

## License

[MIT](LICENSE) © Eric Paschal.
