# WP826 config automation — API + P-code reference

Scriptable, no-browser configuration of the WP826 cordless (ext 19, `192.168.6.71`).
Reverse-engineered from the phone's own React bundle + `tl.*.js` schema files
(firmware Prog **1.0.3.35** / Core 1.0.3.9). Password lives at `/tmp/.wp_pass`
(persistent copy `~/.wp_pass`).

## The client — `tools/wp826.mjs`

```
node tools/wp826.mjs login                 # verify auth
node tools/wp826.mjs get P330,P331,P332     # read P-values
node tools/wp826.mjs set P332=60 P330=1     # write P-values (verifies read-back)
node tools/wp826.mjs reboot                 # /api-reboot
```
Importable: `import { login, get, set, reboot } from './wp826.mjs'` (guarded so
import does NOT auto-run the CLI — running it on import causes concurrent `/access`
calls whose salts cross and fail as `wrong<N>`, which also burns login attempts).

## Auth flow (the crackable bit)

All requests hit **`/cgi-bin/<path>`** and REQUIRE `Referer: https://192.168.6.71/`
+ `Origin: https://192.168.6.71` headers (else `403 Forbidden`). Cookies via
`withCredentials`. Login is a SHA-256 salted challenge-response:

1. `POST /cgi-bin/access`  body `access=sha256(username)`  → `{response:"success", body:"<salt>"}`
2. `POST /cgi-bin/dologin` body `username=admin&password=sha256(password + salt)` → `{response:"success", body:{sid, role}}`
3. `sid` gates the rest via `?sid=<sid>`. Bad password → `{response:"error", body:"wrong<N>"}` (N = attempts left before a 5-min lockout — **do not brute-force**).

## Config API

- **Read:** `GET /cgi-bin/config_get?pvalues=330,331,332&update_session=false&sid=…` → `{configs:[{pvalue,value}]}` (codes are the P-number WITHOUT the leading `P`).
- **Write:** `PUT /cgi-bin/config_update` (JSON) `{"alias":{},"pvalue":{"332":"60"}}` → `{response:"success", body:{status:"right"}}`. Applies immediately for most settings (P332 verified live). No separate `commit` needed (unlike the SSH `CONFIG>` shell).
- SSH CLI alternative: `ssh admin@192.168.6.71` → `config` → `CONFIG>` → `get N` / `set N v` / `commit` (see `wpcli.exp`). Same P-codes; needs an explicit `commit`.

## Discovered P-codes (from `tl.*.js` `{lang, p, el}` schema)

### Remote / XML phonebook  (item 3 — DONE)
| P-code | field | value |
|---|---|---|
| **P330** | ENABLE_XML_PHONEBOOK_DL | `0`=Disabled, `1`=HTTP, `2`=TFTP, `3`=HTTPS |
| **P331** | PHONEBOOK_XML_PATH | server path/URL (≤256 chars) |
| **P332** | PHONEBOOK_DL_INTERVAL | minutes; `0`=off, `5`–`720` |
| P8462 | IMPORT_GROUP_METHOD | Replace/Append |
| P8036 | Remote Phonebook 1 URL | (P8037-40 for 2–5, likely) |
| P22426 | REMOTE_PHONEBOOK_XML_UPDATE_TIME_INTERVAL | |

Live state 2026-07-15: `P330=1, P331="192.168.5.152:8099/phonebook.xml", P332=60`
(interval set from 0→60 via this client — the write-path proof).

### Distinctive ring / Alert-Info  (item 1)
| P-code | field | note |
|---|---|---|
| **P95026** | ALERT_INFO_BELLCORE_MAPPING | `1` (current) = `Bellcore-drN` → predefined Bellcore cadence; `0` = `drN`/`info=text` → **custom** ringtone. This is why the switchboard's `info=Bellcore-dr2` sounded only mildly different — it maps to a cadence, not a distinct tone. For a distinct TONE: set `0` + a custom ringtone + a Match-Caller-ID rule. |
| P104 | ACC_RING_TONE | account default ringtone (currently `2`) |
| P26018 | IGNORE_ALERT_INFO | |
| P26072 | PLAY_TONE_ON_CALL_ALERT_INFO | |
| P22472 / P22473 | PB_MATCHING_RULES / ENABLE_PB_MATCHING_RULES | |

Per-account "Match Incoming Caller ID → ringtone" rule array code: not yet pinned
(further extraction from `tl.account.js` needed). Firmware **1.0.3.35 is below
1.0.3.98**, so the remote-URL ringtone trick (`Alert-Info:<http://host/x.wav>;info=ringN`)
is NOT available here — a custom ring needs an on-device upload.

### Programmable keys / speed dial  (item 5)
| P-code | field |
|---|---|
| P1339 | ENABLE_MPK (currently `0`) |
| **P2939** | CUST_IDLE_KEY_LAYOUT (holds the QuickAccess/idle key assignments; currently `1`=custom-enabled — layout string format TBD) |
| P2923 | CUST_CALL_KEY_LAYOUT |
| P8444 | ALLOW_KEY_CONFIG_VIA_LCD |
| P22639 | QUICK_APP_LONG_PRESS (`contacts.fav` / `quickAccess`) |

### Ringtones / tones  (item 39 — needs file upload, not a P-value)
P345 SYS_RING_TONE · P346 RING_BACK_TONE · P347 CALL_WAITING_TONE · P343 DIAL_TONE ·
P348 BUSY_TONE · P349 REORDER_TONE · **P8509** TOTAL_NUM_OF_CUS_RING_UPDATE (custom-ringtone count).
Custom WAV upload is a file operation (web UI / provisioning), not a `config_update`.

## How to find any other code
`tl.*.js` (fetch `https://192.168.6.71/tl/tl.<account|application|phoneSettings|proKeys|network|maintenance>.js`)
are the field→code schema: each field is `{ lang:'LABEL_KEY', p:'PNNNN', el:{…allowed values…} }`.
Grep for the LABEL and read `p`.

## Ringtone upload (item 39 — DONE)
- **Upload:** `node tools/wp826.mjs ring-upload office_ring.wav`  → `POST /cgi-bin/ringtone?tags=&sid=` (multipart, field `file`). ★★**GOTCHA: `tags` MUST be empty** — a non-empty tags value returns `400`. WAV is accepted (16 kHz mono 16-bit PCM); the format was never the problem.
- **List:** `node tools/wp826.mjs rings 0` (system ring1-N + user 1001+) · **Delete:** `DELETE /cgi-bin/ringtone?id=<id>`.
- Custom (`type:"user"`) ids are ≥1001 and are valid values for ringtone selectors like `P1489` (match rule) and `P104` (account default).

## Configured state (2026-07-15)
- **Item 3 phonebook:** P330=1 (HTTP), P331=`192.168.5.152:8099/phonebook.xml`, **P332=60** (auto-refresh).
- **Item 1 distinctive ring + Item 39 vintage tone:** `office_ring.wav` uploaded → custom **id 1001**; match rule 1 = **P1488="outsideline" / P1489="1001"**; switchboard v0.26.0 tags inbound-trunk INVITEs `Alert-Info: …;info=outsideline`. → outside-line calls ring the cordless with the vintage warble.
- **Item 5 speed-dial:** OPEN — QuickAccess key assignments live in a layout string (P2939); format not yet decoded.
