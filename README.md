# Home PBX — Home Assistant Add-on

A lean, self-hosted phone system for your home, built on **Asterisk 21 + PJSIP**
and packaged as a native Home Assistant add-on with **Ingress** and an
**AppArmor** profile. Designed for analog phones (antique / Western Electric
sets) connected through a **Grandstream GXW4216 V2** FXS gateway, with
room-to-room calling today and **SIP trunk** outside-line support when you want
it.

> One add-on. Runs inside Home Assistant alongside your other add-ons. No
> separate PBX box, no LAMP stack, no FreePBX.

## What you get

- **Room-to-room calling** between every analog phone in the house.
- **Grandstream GXW4216 V2** ready — 16 FXS ports → 16 SIP endpoints, G.711
  µ-law on the analog side, G.722 HD voice available for IP endpoints.
- **Ingress web UI** — live registration/call status straight from Home
  Assistant's sidebar. No extra port, no extra login.
- **AppArmor confined** — tight profile, minimal surface.
- **SIP trunk ready** — flip `trunk.enabled: true` and fill in your provider
  to get an outside line. Off by default.
- **Declarative config** — your extensions live in the add-on options, so the
  whole PBX is reproducible and version-controlled.

## Install

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** menu (top right) → **Repositories**.
3. Add: `https://github.com/tesseractAZ/GPTTesting`
4. Find **Home PBX** in the store and click **Install**.
5. Open the **Configuration** tab, define your rooms (see below), then
   **Start** the add-on.
6. Open the **Web UI** (Ingress) to watch endpoints register.

## Configure your phones

See [`home-pbx/DOCS.md`](home-pbx/DOCS.md) for:

- the add-on options reference (rooms, secrets, trunk),
- a step-by-step **Grandstream GXW4216 V2** provisioning guide,
- the dial plan and extension-numbering scheme,
- how to turn on a SIP trunk for an outside line.

## Architecture

```
Antique analog phones ──FXS──> Grandstream GXW4216 V2 (16 FXS ports)
                                     │  each port = 1 SIP endpoint
                                     ▼
                       Home Assistant host (LAN)
                       ┌──────────────────────────────┐
                       │  Add-on: Home PBX             │
                       │   • Asterisk 21 + PJSIP       │  host network (SIP/RTP)
                       │   • Ingress UI (FastAPI)      │  status / extensions
                       │   • AppArmor confined         │
                       │   • config from add-on options│
                       └──────────────────────────────┘
                                     │
                              (optional) SIP trunk ──> outside line
```

## License

MIT — see [LICENSE](LICENSE).
