# Switchboard — Project Status / Handoff

> Drop-in context for a new (e.g. **local**) Claude Code session. Read this, then
> skim `README.md` and `switchboard/DOCS.md` and you're current.

_Last updated: 2026-06-20_

## What this is

**Switchboard** is a self-hosted phone system for analog home phones (antique /
Western Electric sets), packaged as a **Home Assistant add-on**. Engine is
**Asterisk 21 + PJSIP**. The phones connect to a **Grandstream GXW4216 V2**
16-port FXS gateway; each FXS port registers to Asterisk as one SIP endpoint =
one room extension. Goal: room-to-room calling now, SIP trunk (outside line)
later. Runs inside HA with **Ingress** + an **AppArmor** profile.

## Current status — WORKING

- Add-on **installs, builds, and runs** in Home Assistant. Container boots
  cleanly (s6-overlay), Asterisk loads, the Ingress web UI serves on `:8099`.
- `main` is at **v0.1.2** (what's deployed). The working branch
  `claude/home-phone-system-design-3ultah` is one commit ahead at **v0.1.3**
  (cosmetic: quiets ALSA/JACK module log noise) — **pending merge**.
- **No phones are registered yet.** The add-on ships two placeholder rooms
  (`101 Kitchen`, `102 Living Room`) with `change-me` secrets. They show
  **Offline** in the UI until real config + gateway provisioning is done.

### Getting-here history (all resolved)
1. Repo was private → HA clone failed → made **public**.
2. URL pasted with a leading space → `protocol ' https' is not supported` → re-entered clean.
3. Build failed → removed non-existent Alpine packages `asterisk-pjsip` /
   `asterisk-sounds-en` (PJSIP is in the main `asterisk` package). (v0.1.1)
4. Crash loop `can't open '/init': Permission denied` → the s6 oneshot `up`
   file must be **execline**, not a bashio shebang script; and the AppArmor
   profile was too strict → switched to the documented HA pattern. (v0.1.2)
5. Scary-looking ALSA/JACK errors were just Asterisk probing absent hardware →
   curated `modules.conf`. (v0.1.3, cosmetic)

## Network facts (important)

- **Home Assistant host:** `192.168.5.152`, mask **/22** (subnet
  `192.168.4.0`–`192.168.7.255`). Asterisk listens here on **UDP 5060**
  (add-on uses host networking).
- **Grandstream GXW4216 V2:** `192.168.6.65` — **inside the same /22**, so HA
  and the gateway are on the same L2 network and can talk directly. No
  inter-VLAN routing needed for SIP. ✅

## Next steps (the actual remaining work)

1. **Firmware-update the GXW4216 V2** (before provisioning, so we don't redo it):
   - Web UI: `http://192.168.6.65`, user `admin`, password on the unit's sticker
     (or `admin`/`admin` on older units).
   - Note current firmware on the Status page; back up config under Maintenance.
   - Maintenance → Upgrade and Provisioning → Upgrade Via **HTTPS**, Firmware
     Server Path `firmware.grandstream.com` → Upgrade Now → let it reboot.
2. **Define real rooms** in the add-on **Configuration** (one per wired FXS port:
   `ext` / `name` / strong unique `secret`), Save, **Restart** the add-on
   (config changes only need a restart, not a rebuild).
3. **Provision the gateway** (DOCS §4):
   - Profile 1 → SIP Server `192.168.5.152`, transport UDP, NAT Traversal No.
   - Each FXS port: SIP User ID / Auth ID = extension, Password = that room's
     `secret`. Enable pulse dialing on ports with rotary phones.
4. **Verify** in the Ingress web UI — rooms flip to **Registered**; pick up a
   phone and dial another room's extension.
5. Later: voicemail, a whole-house ring group, intercom/paging, and the SIP
   trunk (`trunk.enabled: true`) for an outside line.

## A local session can do what the cloud one couldn't

This project was built in **Claude Code on the web** (cloud sandbox, no LAN
access). A **local** Claude Code session runs Bash on the user's own machine,
which is on the home network — so it can, **with the user's permission and the
device admin credentials**, reach the gateway directly, e.g.:

```bash
ping 192.168.6.65
curl -s http://192.168.6.65/        # login page
# Grandstream config can be read/scripted over its HTTP API once authenticated.
```

It doesn't connect "magically" — it issues HTTP/curl from the local machine.

## Repo layout

```
repository.yaml          # HA add-on repo manifest → url: …/tesseractAZ/Switchboard
README.md  LICENSE  STATUS.md
switchboard/             # the add-on
  config.yaml            # manifest: version, ingress, apparmor, host_network, options schema
  build.yaml  Dockerfile # Alpine base + Asterisk + Python UI
  apparmor.txt           # confinement profile (HA pattern)
  DOCS.md  CHANGELOG.md
  rootfs/
    etc/s6-overlay/s6-rc.d/...      # init-switchboard (oneshot) → asterisk + webui (longruns)
    etc/asterisk/modules.conf       # curated module loader (v0.1.3)
    usr/bin/switchboard-config      # renders /etc/asterisk/*.conf from add-on options
    usr/share/switchboard/webui/app.py   # FastAPI Ingress UI (AMI status)
```

## Key design decisions (don't relitigate)

- **PJSIP only** (chan_sip is removed in modern Asterisk).
- **Codecs:** prefer **G.711 µ-law** for the analog path (no transcode, best for
  narrowband antique handsets); also offer **G.722 + Opus** for any HD/IP
  endpoints. Configurable via the `codecs` option. Opus carries no extra
  fidelity from an analog handset — it only matters between IP endpoints.
- **Config is generated from add-on options** on every start by
  `switchboard-config` (the options are the source of truth; editing
  `/etc/asterisk/*.conf` by hand is pointless — regenerated each boot).
- **Trunk is off by default**; room-to-room needs no trunk.

## Dev / deploy workflow

- Validate locally before pushing:
  ```bash
  python3 -m py_compile switchboard/rootfs/usr/bin/switchboard-config \
      switchboard/rootfs/usr/share/switchboard/webui/app.py
  python3 -c "import yaml; yaml.safe_load(open('switchboard/config.yaml'))"
  ```
- **Bump `version:` in `switchboard/config.yaml`** (and the `io.hass.version`
  label in the Dockerfile) on any image change, so HA offers an Update.
- Deploy = push to `main` → in HA: Add-on Store → ⋮ → Reload → Update/Rebuild.
- Note: the **cloud** session's git proxy only accepts pushes to the working
  branch, so changes were merged to `main` via GitHub PRs. A **local** session
  with the user's normal git credentials can push to `main` directly.

## Pointers

- Full setup + GXW4216 V2 provisioning + dial plan + trunk: `switchboard/DOCS.md`
- Troubleshooting table: `switchboard/DOCS.md` §6
- Changelog: `switchboard/CHANGELOG.md`
