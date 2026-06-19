# Switchboard

A lean, self-hosted phone system for your home, built on **Asterisk 21 + PJSIP**
and packaged as a native **Home Assistant add-on** with **Ingress** and an
**AppArmor** profile. Designed for analog phones — antique / Western Electric
sets — connected through a **Grandstream GXW4216 V2** FXS gateway, with
room-to-room calling today and a **SIP trunk** outside line when you want it.

> One add-on. Runs inside Home Assistant alongside your other add-ons. No
> separate PBX box, no LAMP stack, no FreePBX.

## What you get

- **Room-to-room calling** between every analog phone in the house.
- **Grandstream GXW4216 V2** ready — 16 FXS ports → 16 SIP endpoints.
- **Smart codecs** — G.711 µ-law preferred for the analog path (no transcode,
  cleanest for antique handsets), with **G.722 and Opus** offered so any
  HD-capable or IP endpoint negotiates up. Codec list is configurable.
- **Ingress web UI** — live registration/call status from Home Assistant's
  sidebar. No extra port, no extra login.
- **AppArmor confined** — tight profile, minimal surface.
- **SIP trunk ready** — flip `trunk.enabled: true` and fill in your provider to
  get an outside line. Off by default.
- **Declarative config** — your extensions live in the add-on options, so the
  whole PBX is reproducible and version-controlled.

## A note on "HD voice" and analog phones

The GXW4216 **V2** really does support wideband codecs (G.722, **Opus**) on its
SIP side. But antique analog handsets are **narrowband by physics** (~300–3400
Hz at the 2-wire loop), so a wideband codec carries no extra fidelity from them.
Opus/G.722 matter between *IP* endpoints (softphones, IP phones, a future HD
intercom). For analog room-to-room, G.711 µ-law is the best choice — which is
why Switchboard prefers it while still **offering** Opus/G.722 for endpoints
that can use it. See [`switchboard/DOCS.md`](switchboard/DOCS.md) §codecs.

## Install

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** menu (top right) → **Repositories**.
3. Add: `https://github.com/tesseractAZ/Switchboard`
4. Find **Switchboard** in the store and click **Install**.
5. Open the **Configuration** tab, define your rooms, then **Start**.
6. Open the **Web UI** (Ingress) to watch endpoints register.

Full setup — options reference, **GXW4216 V2** provisioning, the dial plan, and
the SIP trunk — is in [`switchboard/DOCS.md`](switchboard/DOCS.md).

## Architecture

```
Antique analog phones ──FXS──> Grandstream GXW4216 V2 (16 FXS ports)
                                     │  each port = 1 SIP endpoint
                                     ▼
                       Home Assistant host (LAN)
                       ┌──────────────────────────────┐
                       │  Add-on: Switchboard          │
                       │   • Asterisk 21 + PJSIP        │  host network (SIP/RTP)
                       │   • Ingress UI (FastAPI)        │  status / extensions
                       │   • AppArmor confined           │
                       │   • config from add-on options  │
                       └──────────────────────────────┘
                                     │
                              (optional) SIP trunk ──> outside line
```

## License

MIT — see [LICENSE](LICENSE).
