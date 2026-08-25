# WP826 config automation — API + P-code reference

Scriptable, no-browser configuration of the Grandstream **WP826** WiFi cordless
(reference deployment: extension 19; **DHCP-assigned IP — currently `192.168.1.84`**,
previously `.71`; give it an Eero DHCP reservation, and override the tool with
`WP826_HOST=<ip>` if it moves). Reverse-engineered from the
phone's own React bundle + `tl.*.js` schema files (firmware Prog **1.0.3.35** /
Core 1.0.3.9). The admin password is read from `/tmp/.wp_pass` (persistent copy
`~/.wp_pass`).

This document pairs with the client at [`wp826.mjs`](wp826.mjs). Everything here is
device/runtime knowledge, not encoded in the tool — the tool is generic and
hard-codes no P-codes.

## The client — `tools/wp826.mjs`

```
node tools/wp826.mjs login                     # verify auth
node tools/wp826.mjs meta [keyword]            # dump the P-code → alias map (filterable)
node tools/wp826.mjs get P330,P331,P332        # read P-values
node tools/wp826.mjs set P332=60 P330=1        # write P-values (verifies read-back)
node tools/wp826.mjs rings [type]              # list ringtones (0 = system + user)
node tools/wp826.mjs ring-upload office_ring.wav   # upload a custom ringtone
node tools/wp826.mjs mpk                        # dump the virtual/quick-access keys
node tools/wp826.mjs speeddial <VKID> <num> [label]  # set a key to Speed Dial
node tools/wp826.mjs reboot                    # POST /api-sys_operation (request=REBOOT)
```

Importable (`import { login, get, set, reboot, uploadRing, listRings, mpkList,
mpkSave, speedDial, meta } from './wp826.mjs'`). The module is import-guarded so
importing it does **not** auto-run the CLI — running the CLI on import would fire
concurrent `/access` calls whose salts cross and fail as `wrong<N>`, burning login
attempts.

## Auth flow

All requests hit **`/cgi-bin/<path>`** and REQUIRE `Referer: https://<phone-ip>/`
and `Origin: https://<phone-ip>` headers (the tool derives both from its host) (else `403 Forbidden`). TLS is
self-signed (the client sets `rejectUnauthorized: false`). Login is a SHA-256
salted challenge-response:

1. `POST /cgi-bin/access`  body `access=sha256(username)`  → `{response:"success", body:"<salt>"}`
2. `POST /cgi-bin/dologin` body `username=admin&password=sha256(password + salt)` → `{response:"success", body:{sid, role}}`
3. `sid` gates the rest via `?sid=<sid>`. Bad password → `{response:"error", body:"wrong<N>"}` (N = attempts left before a 5-min lockout — **do not brute-force**).

## Config API

- **Read:** `GET /cgi-bin/config_get?pvalues=330,331,332&update_session=false&sid=…` → `{configs:[{pvalue,value}]}` (codes are the P-number WITHOUT the leading `P`).
- **Write:** `PUT /cgi-bin/config_update` (JSON) `{"alias":{},"pvalue":{"332":"60"}}` → `{response:"success", body:{status:"right"}}`. Applies immediately for most settings — no separate `commit` (unlike the SSH shell).
- **SSH alternative:** `ssh admin@<phone-ip>` → `config` → `CONFIG>` → `get N` / `set N v` / `commit` (an expect helper is committed at [`wp826-cli.exp`](wp826-cli.exp)). Same P-codes, but the SSH path needs an explicit `commit`.

## How to find any other code

The `tl.*.js` bundles (fetch
`https://<phone-ip>/tl/tl.<account|application|phoneSettings|proKeys|network|maintenance>.js`)
are the field → code schema: each field is
`{ lang:'LABEL_KEY', p:'PNNNN', el:{…allowed values…} }`. Grep for the label and
read `p`. `node tools/wp826.mjs meta <keyword>` dumps the live P-code → alias map.

---

## P-codes by feature

### Remote / XML phonebook

| P-code | field | value |
|---|---|---|
| **P330** | ENABLE_XML_PHONEBOOK_DL | `0`=Disabled, `1`=HTTP, `2`=TFTP, `3`=HTTPS |
| **P331** | PHONEBOOK_XML_PATH | server path/URL (≤256 chars) |
| **P332** | PHONEBOOK_DL_INTERVAL | minutes; `0`=off, `5`–`720` |
| P8462 | IMPORT_GROUP_METHOD | Replace / Append |
| P8036 | Remote Phonebook 1 URL | (P8037–40 for 2–5) |
| P22426 | REMOTE_PHONEBOOK_XML_UPDATE_TIME_INTERVAL | |

The Switchboard add-on serves the room directory at
`http://<ha-host>:8099/phonebook.xml`, so the cordless shows every room by name.

### Distinctive ring / Alert-Info

| P-code | field | note |
|---|---|---|
| **P95026** | ALERT_INFO_BELLCORE_MAPPING | `1` = `Bellcore-drN` → predefined cadence; `0` = `drN`/`info=text` → a custom ringtone. Either way, a plain-text `info=` tag routes to a **Match Incoming Caller ID** rule. |
| **P1488 / P1489** | Match rule 1: pattern / ringtone | Ringtone accepts a custom id (>=1001). |
| P104 | device default ringtone | the fallback when an account sets no ring of its own |
| P26018 | IGNORE_ALERT_INFO | |
| P26072 | PLAY_TONE_ON_CALL_ALERT_INFO | |

Match-Incoming-Caller-ID (pattern, ringtone) pairs, **as measured on firmware
1.0.3.39** — three separate blocks, all with the same signature (pattern empty,
ringtone defaulting to `2`):

| block | pairs |
|---|---|
| `P1488/P1489` .. `P1522/P1523` | 18 |
| `P6716/P6717` .. `P6750/P6751` | 18 |
| `P26064/P26065`, `P26066/P26067`, `P26068/P26069`, `P26096/P26097` | 4 |

The pattern matches the Alert-Info `info=` tag OR the caller-ID number. Rule 1
(`P1488/P1489`) is the one this project uses.

#### Two gotchas that make this look broken

**1. Register the handset on ACCOUNT 1.** The match rule has no effect on the
other account slots — the phone rings its normal tone and nothing you change in
the rule makes any difference (verified by ear, 2026-08-23).

**2. The rule's ringtone must DIFFER from the account's default ringtone.**
Otherwise every call — inside and outside — plays the same tone, and the two are
audibly identical *whether or not the rule fires*, so no listening test can tell
you anything. Account 1 leaves its ring unset and falls back to `P104`, so with
`P104=2` and the rule pointing at a custom id the two stay distinct.

#### Account 1 identity codes

Account 1 uses a scattered legacy set (the other slots use clean 100-code blocks
based at `P401`, `P501`, `P601`, but this project uses only account 1):

| field | P-code |
|---|---|
| active | `P271` |
| SIP server | `P47` |
| SIP user ID | `P35` |
| auth ID | `P36` |
| auth password | `P34` |
| account name | `P270` |
| display name | `P3` |
| local SIP port | `P40` |
| ring tone | *(leave unset -> falls back to `P104`)* |

Password fields read back as `\r******\r` when set and `""` when empty, so a mask
is proof a value is stored. A blank `P35`/`P47` means that account is genuinely
unconfigured — not an API fault.

> Firmware **1.0.3.35 is below 1.0.3.98**, so the remote-URL ringtone trick
> (`Alert-Info: <http://host/x.wav>;info=ringN`) is **not** available — a custom
> ring needs an on-device upload (below).

### Ringtones (custom WAV upload)

- **Upload:** `node tools/wp826.mjs ring-upload <file.wav>` → `POST /cgi-bin/ringtone?tags=&sid=` (multipart, field `file`). **GOTCHA: `tags` MUST be empty** — a non-empty `tags` value returns `400`. WAV is accepted (16 kHz mono 16-bit PCM works); the format was never the problem, the `tags` field was.
- **List:** `node tools/wp826.mjs rings 0` (system `ring1`–N + user `1001`+).
- **Delete:** `DELETE /cgi-bin/ringtone?id=<id>` (raw API; no CLI subcommand).
- Custom (`type:"user"`) ids are ≥ `1001` and are valid values for ringtone
  selectors like `P1489` (a match rule) and `P104` (the account default).

Related tone P-codes: `P345` SYS_RING_TONE, `P346` RING_BACK_TONE, `P347`
CALL_WAITING_TONE, `P343` DIAL_TONE, `P348` BUSY_TONE, `P349` REORDER_TONE,
`P8509` TOTAL_NUM_OF_CUS_RING_UPDATE (custom-ringtone count).

### Virtual keys / Quick Access speed-dial

Not P-values — a JSON key array with its own endpoints:

- **Read:** `node tools/wp826.mjs mpk` (`GET /api-mpk_download` → ~76 keys). Key
  shape: `{VKID, DisplayIndex, Priority, Locked, TypeMode, Description, ValueName,
  Account, AutoType}`.
- **Save:** `POST /api-save_mpk` (JSON array; merges by VKID, so send only changed
  keys).
- **Set a speed dial:** `node tools/wp826.mjs speeddial <VKID> <number> [label]`.
- **Key groups by Priority:** `1xxx` = VKID 1–10 = classic **MPK / Programmable
  Keys** (needs `P1339` ENABLE_MPK=1); `2xxx` = VKID 11–50 = the on-screen **Quick
  Access grid** (VKID 11–15 = default Quick-Jump apps, 16–50 empty); plus
  `3xxx`/`4xxx`/`5xxx`/`10xxx` groups.
- **`TypeMode` enum** (from `client.config.js`): `-1`=none · `0`=DEFAULT ·
  `1`=SHARED_LINE_ACCOUNT · **`2`=SPEED_DIAL** (ValueName=number, Account=SIP acct
  index) · `3`=BLF · `6`=SPEED_DIAL_ACTIVE_ACCOUNT (no Account) · `7`=DIAL_DTMF ·
  `8`=VOICE_MAIL (no ValueName) · `9`=CALL_RETURN · `10`=TRANSFER · `11`=CALL_PARK ·
  `39`=QUICK_JUMP app (ValueName=app id, e.g. `contacts.fav` / `wifi.settings`).

Related layout P-codes: `P1339` ENABLE_MPK, `P2939` CUST_IDLE_KEY_LAYOUT (Quick
Access grid), `P2923` CUST_CALL_KEY_LAYOUT, `P22639` QUICK_APP_LONG_PRESS.

### WiFi / QoS (verified optimal on the reference cordless)

| P-code | field | value |
|---|---|---|
| P25438 | WiFi band | `0`=5G+2.4G, `1`=2.4G, `2`=5G-only |
| P82269 | Power-save | `0`=Generic-PSM, `3`=Disabled, `4`=U-APSD |
| P82332 | Roaming mode | `0`=Default, `1`=Boost |
| P82281 | VoWLAN target delay | `0`=Low, `1`=Med, `2`=High |
| P1559 | Layer-3 QoS (RTP DSCP) | `46` = EF |
| P1558 | Layer-3 QoS (SIP DSCP) | `26` |

---

## Reference configured state

- **Phonebook:** `P330=1` (HTTP), `P331=192.168.1.152:8099/phonebook.xml`,
  `P332=60` (auto-refresh).
- **Distinctive outside-line ring + vintage tone:** the cordless registers on
  **account 1** (required — see the gotchas above); `office_ring.wav` uploaded as
  custom **id 1001**; match rule 1 = `P1488="outsideline"` / `P1489="1001"`. The
  Switchboard add-on tags inbound-trunk INVITEs with
  `Alert-Info: <http://127.0.0.1>;info=outsideline`, so outside-line calls ring the
  cordless with the vintage warble. (Text-match mode requires `P95026=0`; Bellcore
  mode ignores the `info=outsideline` tag.) The committed `tools/office_ring.wav` is
  regenerated by `tools/make-office-ring.py`.
- **Quick-Access speed dials:** VKID 16 = Speed Dial `0` "Operator", VKID 17 =
  Speed Dial `12` "Kitchen" (grid positions 6–7). Re-point with
  `node tools/wp826.mjs speeddial 17 <ext> <name>`.
