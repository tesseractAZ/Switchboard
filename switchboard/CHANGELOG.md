# Changelog

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
