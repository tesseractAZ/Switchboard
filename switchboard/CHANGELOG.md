# Changelog

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
