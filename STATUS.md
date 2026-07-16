# Switchboard — project status / handoff

> Drop-in context for a new (e.g. local) session. Read this, then skim
> [`README.md`](README.md) and [`switchboard/DOCS.md`](switchboard/DOCS.md) and
> you're current.

_Version **0.30.0** · deployed and running._

## What this is

**Switchboard** is a self-hosted phone system for analog home phones (antique /
Western Electric sets), packaged as a **Home Assistant add-on**. Engine is
**Asterisk 21 + PJSIP**. Analog phones connect through a **Grandstream GXW4216 V2**
FXS gateway (one FXS port per extension) plus a **WP826 WiFi cordless**; the add-on
regenerates all Asterisk config from its options on every start. Runs inside Home
Assistant with **Ingress** and an **AppArmor** profile, on host networking. Audio
is **G.711 µ-law end to end** — no transcoding.

## Current status — working

The add-on installs, builds, and runs. Beyond room-to-room calling, the live
feature set is:

- **Voice operator** (dial `0`), **directory** (`411`), **talking clock** (`41`),
  **wake-up calls** (`42`), **home-automation/lights** (`43`), **paging** (`44`),
  **dial-a-status** (`45`), and **announcements** (`46`). Speech is on-box
  (whisper.cpp STT + espeak-ng TTS).
- **Announce-onto-a-handset** HTTP endpoint (`POST /api/announce/{ext}`) so Home
  Assistant / another add-on can speak alerts on any phone, including the cordless.
- **SIP trunk** outside line (optional) with layered toll-fraud protection.
- **Ingress dashboard** (FastAPI ⇄ loopback AMI) and an **operator console**
  (telnet `:2300` + browser terminal `:8100`).
- **Health monitoring** → Home Assistant sensors + notifications (link health,
  per-call MOS, cordless + gateway device health).

## Reference deployment

- **Home Assistant host:** `192.168.1.152`, `/22` (subnet `192.168.1.0`–`192.168.1.255`).
  Asterisk listens on UDP 5060 (host networking).
- **GXW4216 V2 FXS gateway:** on the same `/22` (DHCP-assigned; re-check the IP if
  unreachable). Eight wired ports → extensions **`11`–`18`**.
- **WP826 WiFi cordless:** `192.168.1.71`, extension **`19`** (also the Home
  Assistant announce endpoint and the device-health target).
- Feature codes at their defaults (`0`, `41`–`46`, `411`).

LAN IPs are RFC-1918 private addresses; device admin passwords live outside the
repo (local `/tmp` files), not in git.

## Repo layout

```
repository.yaml            # HA add-on repository manifest → this GitHub repo
README.md  LICENSE  STATUS.md
switchboard/               # the add-on
  config.yaml              # manifest: version, ingress, apparmor, host_network, options + schema
  build.yaml  Dockerfile   # HA Alpine 3.21 base; two-stage build (compiles whisper.cpp)
  apparmor.txt             # confinement profile (HA pattern; deliberately broad)
  DOCS.md  SECURITY.md  CHANGELOG.md
  translations/en.yaml     # Configuration-tab labels + inline help (byte-matches config.yaml)
  tests/                   # 16 plain-python3 suites (225 test fns; no pytest)
  rootfs/
    etc/s6-overlay/s6-rc.d/…    # init-switchboard (oneshot) + 8 longruns
    etc/asterisk/modules.conf   # curated module loader
    usr/bin/switchboard-config  # renders every /etc/asterisk/*.conf from options
    usr/bin/switchboard-{stt,tts,callqos,mwi}
    usr/share/switchboard/       # webui, console, console-web, operator, wakeup,
                                 # rtpmon, devhealth, clock
    var/lib/asterisk/agi-bin/    # the voice AGIs (operator, wakeup, clock, …)
tools/                     # WP826 cordless scripting (wp826.mjs + P-code reference)
```

## Key design decisions (don't relitigate)

- **PJSIP only** (`chan_sip` is dead in modern Asterisk).
- **G.711 µ-law only, everywhere**, not configurable — no HD/Opus module is
  installed. Antique handsets are narrowband by physics; pinning one codec means no
  call ever transcodes. (See DOCS §13.)
- **Config is generated from add-on options on every start** by
  `switchboard-config` — options are the source of truth; hand-editing
  `/etc/asterisk/*.conf` is pointless.
- **Speech is on-box** — whisper.cpp STT (`base.en`, resident + CLI fallback) and
  espeak-ng TTS. No cloud, no API key. (The `tts.piper` announce option is retained
  for reference but the on-phone announce voice is espeak-ng.)
- **Trunk is off by default**; room-to-room needs no trunk. When on, it re-registers
  every 120 s and blocks international/premium prefixes.
- **The consoles are unauthenticated on the LAN by design** — see SECURITY.md.

## Dev / deploy workflow

Validate locally, then merge to `main` (the add-on rebuilds on the host from git):

```bash
# quick syntax checks
python3 -m py_compile switchboard/rootfs/usr/bin/switchboard-config \
    switchboard/rootfs/usr/share/switchboard/webui/app.py
python3 -c "import yaml; yaml.safe_load(open('switchboard/config.yaml'))"

# the test suite (16 modules, plain python3, no deps)
for t in switchboard/tests/test_*.py; do python3 "$t" || break; done
```

- **Bump `version:` in `switchboard/config.yaml`** on any image change so Home
  Assistant offers an Update.
- Deploy = merge to `main` → in Home Assistant: Add-on Store → ⋮ → Reload →
  Update/Rebuild. (A store-add-on rebuild happens on the host from the merged git
  ref.)
- Live add-on state is reachable over the Home Assistant WebSocket `supervisor/api`
  proxy (the REST `/api/hassio/*` path needs a Supervisor token a normal user token
  doesn't have). The add-on slug is prefixed on the installed instance.

## Pointers

- Full setup, config reference, gateway/cordless provisioning, trunk, console,
  monitoring: [`switchboard/DOCS.md`](switchboard/DOCS.md)
- Security model + accepted risks: [`switchboard/SECURITY.md`](switchboard/SECURITY.md)
- Release history: [`switchboard/CHANGELOG.md`](switchboard/CHANGELOG.md)
- WP826 cordless scripting: [`tools/wp826.mjs`](tools/wp826.mjs) +
  [`tools/wp826-pcodes.md`](tools/wp826-pcodes.md)
