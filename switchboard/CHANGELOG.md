# Changelog

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
