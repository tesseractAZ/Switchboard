# Changelog

## 0.1.0

Initial release.

- Asterisk 21 + PJSIP packaged as a Home Assistant add-on (Ingress + AppArmor).
- Room-to-room calling: each `rooms` entry becomes a PJSIP endpoint for one
  Grandstream GXW4216 V2 FXS port.
- Config generated from add-on options on every start (`pbx-generate-config`).
- Ingress web UI showing per-room registration and active calls (FastAPI + AMI).
- SIP trunk support (disabled by default) for an outside line: outbound via a
  dial prefix, inbound rings all rooms.
- Codecs: G.711 µ-law/a-law for the analog path, G.722 available for HD IP
  endpoints.
