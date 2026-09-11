# Security

Switchboard runs an Asterisk PBX inside a `host_network` Home Assistant add-on:
its ports are reachable directly on your LAN, and it can dial the outside world
through an optional SIP trunk. This document describes the security model, the
threats it defends against, the risks it deliberately accepts, and the handful of
things **you** must configure.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** by opening a
[GitHub security advisory](https://github.com/tesseractAZ/Switchboard/security/advisories/new)
(Security → Advisories → *Report a vulnerability* on this repository).
Do not open a public issue for an unpatched vulnerability. Include the version, the
component, and a reproduction if you have one. This is a personal open-source
project; expect a best-effort response rather than a guaranteed SLA.

## Supported versions

Only the latest release on `main` is supported. There are no back-ported security
fixes for older versions — update to the current release.

## Automated scanning

Every push and pull request is analysed by **CodeQL** (`security-extended`) for
both Python and JavaScript. There is **no** dependency-manifest scanning: every
runtime dependency is installed by `apk add` / `pip install` inside
`switchboard/Dockerfile`, which GitHub's dependency graph does not parse, so
Dependabot raises no advisories for this repository.
Findings surface in the repository's Security tab, and CI additionally **fails**
on any error-level or high-severity (>= 7.0) result that is not listed in
`.github/codeql-baseline.json` — a small, explicit exemption file (currently 5
rules, each pinned to specific paths) for findings that were reviewed and
accepted, such as the developer-only WP826 tool's `rejectUnauthorized: false`.
The gate fails closed if that file is unreadable, and reports how many findings
it silenced. An upload alone files an alert but would not stop a merge.

Most of the add-on's Python is executed by name. CodeQL's Python extractor
reads the shebang, so the extensionless scripts (`switchboard-config` and its
siblings) are analysed under their own names. Only the `.agi` voice flows need
help — a non-Python extension wins over the shebang — so the workflow stages an
analysis-only `.py` copy of each `.agi` before scanning; those copies exist
solely in CI. An alert against `switchboard-operator.py` refers to
`switchboard-operator.agi`.

---

## Security model

Defense is layered. Each subsystem below states what is defended and how.

### 1. The Ingress management dashboard is Supervisor-only

The dashboard runs behind Home Assistant Ingress, but because the add-on uses host
networking, its port (`8099`) is *also* directly reachable on the LAN — which would
bypass Ingress authentication. To close that, a pure-ASGI middleware rejects any
connection whose source IP is not the Home Assistant Supervisor (`172.30.32.2`),
returning **HTTP 403**. This preserves Ingress auth without changing the bind.

**It covers `http` *and* `websocket` scopes, and that is load-bearing.** Until
v0.93.0 this was a Starlette `@app.middleware("http")`, which returns early for
any scope that is not HTTP. That was correct while every route was HTTP and
became a hole the moment the operator console arrived on this port: a WebSocket
route would have inherited no guard at all, putting an unauthenticated terminal
onto the LAN — on the one port that cannot be switched off, because it is the
panel. Any new middleware here must dispatch on the scope type, and the LAN
exemptions below must stay gated behind `scope["type"] == "http"`; a WebSocket
scope carries no `method` key, and reaching for a default would exempt upgrades
outright.

**The allowlist is exactly one address, and must never become a subnet.** The
loopback entries were removed in v0.93.0: under `host_network` that loopback is
the *host's*, shared with every host-network add-on and every process on the box.
It is a location, not an identity. A CIDR would be worse still — the sibling
Z-Wave add-on shipped a check matching the whole `172.30.32.0/23` hassio bridge,
which is where every sibling add-on container lives, so its address term was
always true and the test collapsed to "did the client send a header it chose to
send".

Two narrow, read-only GET paths are exempt so "dumb" LAN devices can reach them: a
name-validated announcement WAV (`/announce/<name>.wav`) and the cordless's remote
phonebook (`/phonebook.xml`, which exposes only the internal extension directory).
`POST /api/announce/*` is exempt **only** when it carries a valid announce token
(see [below](#5-the-announce-endpoint)).

Every operator action validates its extension against both the configured room set
**and** a `^[0-9]{2,6}$` pattern before any Manager call, and rejects channel names
containing CR/LF — so the JSON API cannot be used to inject Manager protocol lines
or smuggle a dial string. Client-facing errors are generic (`forbidden`,
`unreachable`); detail is logged server-side only.

### 2. The Asterisk Manager (AMI) is loopback-only

The Manager socket that the dashboard, console, and monitors use is bound to
`127.0.0.1:5038` with a permit list restricted to loopback. Its password is a
**fresh 24-character random secret generated on every boot** and written only to
the generated config and a tmpfs file (`0640`, group `asterisk`) — never to
persistent disk.

The Manager account deliberately **withholds the `command` write class** — Asterisk's
CLI `Command` action is remote code execution, and it is the one dangerous
privilege the account does not have. The account holds `originate` (for the
test-ring, connect, page, wake-up, and announce actions), but every web-app
origination is pinned to a **fixed internal target**: test-ring runs a fixed
`Playback` to a known room, while connect, wake-up, page and announce originate
into a fixed internal dialplan context (`rooms`, `wakeup-deliver`, `page` and
`switchboard-announce-play` respectively). Announce moved off `Playback` in
v0.57.0 so that it has a hangup extension and therefore a quality record; the
target is no less fixed for it. Every extension is validated against the configured room set before
the call, so an origination can only ever ring an internal phone — it cannot be
steered into an outside call even with the privilege.

### The SIP listener (`5060/udp`) and the resident recognizer

The largest LAN surface is Asterisk's SIP listener on `5060/udp` (the add-on
runs with host networking). Every endpoint is a named room with its own secret
— a REGISTER must authenticate against that room's `secret`, there is no
anonymous or guest context, and the dial plan only exists for authenticated
endpoints. Live call audio uses the `rtp_start`–`rtp_end` UDP range. This is a
LAN-only surface by deployment (nothing here should ever be port-forwarded);
the per-room secrets are the authentication boundary, which is why the
placeholder `change-me-…` secrets must actually be changed.

The resident speech recognizer (`whisper-server`) binds `127.0.0.1:8126`
loopback-only and runs unprivileged — with host networking, a LAN-visible bind
would expose an unauthenticated inference server, so the bind address is the
control.

### 3. Secret handling & scrubbing

- Generated config files that contain cleartext secrets (SIP passwords, the AMI
  secret) are written **`0640` via `os.open`** (no world-readable umask window),
  owned by group `asterisk`.
- Each room is validated before it's written: a missing/duplicate/all-zero or
  non-2–6-digit extension is skipped, and a secret containing control characters,
  `;`, or leading/trailing whitespace is rejected (Asterisk would silently truncate
  `a;b` to `a`). Trunk credentials are charset-validated and **fail closed** (the
  whole trunk is skipped) on the same risks. Display names are stripped of control
  characters, `"`, and `;` before entering quoted caller-ID/comments.
- **No secrets in logs.** Validation failures log the extension only, never the
  secret. Asterisk logging goes to **three** destinations, and the difference
  between them matters:

  | Destination | Carries | Readable from outside the add-on |
  | --- | --- | --- |
  | console → journald | notice, warning, error, verbose | via the Supervisor |
  | `/data/state/asterisk.log` | notice, warning, error, **verbose(2)** | **no** |
  | `/share/switchboard/asterisk.log` | notice, warning, error — **no verbose** | **yes** |

  The `/data` copy takes exactly one level of verbose output, and the level is
  the point. Endpoint reachability — `Endpoint <n> is now Unreachable` and its
  `Contact` twin — is emitted by Asterisk at verbosity 2, and until v0.84.0 the
  durable log did not keep it: across twenty-five days it held no record of
  whether the phones were reachable, so the one whole-fleet outage this system
  has had could not be investigated from the copy that survives. `verbose(2)`
  selects those lines and stops short of the verb-3 dialplan trace, which is
  where the call detail — and any spoken content a future change might route
  through the logger — would be. The file is trimmed at boot to its newest half
  whenever it passes 8 MB. The `/share` copy is capped the same way at 32 MB —
  and until v0.100.3 that cap **emptied the file** rather than keeping its newest
  half, which would have discarded the whole readable history the first time it
  fired.

  With `res_security_log` not loaded there is no per-REGISTER flood, so still no
  secrets and negligible growth. The `/share` copy is the one to reason about: `/share` is host-mounted, readable by
  anything with access to the shared folder, and captured in add-on backups.

  **Since v0.94.7 it carries no verbose class at all.** It used to, on the
  reasoning that the link-health poller reconstructs endpoint outages from
  `Endpoint <n> is now Unreachable` — a verbose line — so moving the class to
  `/data` would blind that detector. That reasoning was sound and the conclusion
  was wrong: the fix was to repoint the poller at `/data/state/asterisk.log`
  (`ENDPOINT_LOG_PATH`), which it can read because it runs as root inside the
  container, and to take verbose off the readable copy entirely. The detector
  kept working and the dialplan trace stopped being published.

  ⚠ **Historical residue.** Nothing removes what was written before that change.
  On this deployment the readable copy still holds pre-v0.94.7 lines — the newest
  dialplan trace in it is dated 2026-09-09, two days before v0.94.7 shipped — and
  a scan of it counts 58 lines carrying a 10–11 digit number and 998 carrying a
  `sip:<number>@` URI. Stopping the leak did not drain what had already leaked.
  If that matters for your deployment, truncate the file; the authoritative copy
  is `/data/state/asterisk.log`, which is not readable from outside.

  What this means in practice: **since v0.94.7 the dialplan trace is no longer
  published to the shared folder, and the recognised speech never was.**
  Speech-recognition output is
  written by the AGIs to their own stderr, which never passes through Asterisk's
  logger — verified on a running system, both `asterisk.log` copies contain zero
  transcript lines. The voice assistant's transcripts go to
  `/data/state/assistant.jsonl`, which is not readable from outside at all, and
  are not mirrored to `/share`. A test (`test_privacy_invariants.py`) fails the
  build if any voice script starts routing speech through the logger, because
  that single change would move it into the readable copy.
- `/data/options.json` (which holds the SIP secrets, trunk secret, and announce
  token) stays root-only; runtime state that the voice AGIs need is written to a
  separate `asterisk`-owned `/data/state` directory instead.

### 4. Home Assistant access

Light/scene/media/climate control and sensor/notification pushes use the add-on's
own `SUPERVISOR_TOKEN` through the Supervisor's Core proxy — there is **no separate
stored credential**. Generic service calls are restricted to an allow-list of
domains (`light`, `scene`, `media_player`, `tts`, `climate`), the service name must
be `[a-z_]+`, and every entity ID is domain-validated before a call is made.

### 5. The announce endpoint

`POST /api/announce/{ext}` speaks a clip onto a room handset. Over the LAN it is
allowed **only** when the `X-Announce-Token` header equals your configured
`announce_token`. If that option is blank (the default), **LAN announce is disabled**
and only the Supervisor can call it. The extension must be a configured room; the
`{text}` is capped at 500 characters; the `{url}` branch accepts only `http`/`https`,
rejects loopback / link-local / reserved hosts (SSRF guard), does not follow
redirects, and caps the fetched body at 5 MB. The file server that returns the
rendered clip enforces a strict filename pattern plus a realpath-containment check
against path traversal.

---

## Toll-fraud (the trunk threat model)

The SIP trunk is where the internet meets your phone bill. When a trunk is enabled,
these defenses are generated automatically:

- **Blocked prefixes.** International (`011`) and premium `1-900` numbers are
  matched *before* the general outbound rule and routed to congestion, in both
  dial modes. Prefix-dial mode additionally blocks a bare `900`; direct-dial mode
  does not need to, because its outbound pattern (`_1NXXNXXXXXX`) requires the
  leading 1 and never matches a bare 10-digit number.
- **Dial-flag hygiene.** Inbound calls use `r`-only Dial flags — an outside caller
  is never given the in-call `##`/`*2` DTMF transfer/feature codes. Outbound calls
  use `rT` — your internal caller may transfer, but the far PSTN party may not
  invoke your feature codes.
- **Internal-only transfers.** DTMF transfers resolve in a dedicated context that
  contains only internal room extensions (plus the operator) and has **no outbound
  rule and no catch-all**, so a transferred-in outside caller keying `## 9 1 900…`
  matches nothing and the transfer fails cleanly. The transfer context is stamped
  as an inherited channel variable so it survives Asterisk's blind-transfer
  masquerade.
- **Origin guard.** Outbound origination is refused when the originating channel is
  the trunk endpoint itself — a version-independent backstop.
- **REFER rejection.** The trunk endpoint sets `allow_transfer = no`, so a
  provider-side / remote party can't REFER Switchboard into an outbound leg. (Room
  endpoints keep transfer enabled so the cordless's Transfer button still works.)
- **Caller-ID sanitization.** Attacker-controlled inbound caller-ID is filtered to
  phone characters before it reaches logs and HTML-escaped before the dashboard
  renders it.

---

## Accepted LAN-local risks

These are deliberate design choices, documented here so you can decide whether they
fit your network. Where the add-on can flag one at start-up it does — each console
logs its bind, escalating from INFO to a NOTICE only when it is actually reachable
from the LAN without a login, and a blank `cordless_cert_sha256` prints a NOTE — but the
AppArmor and developer-tool items are structural and emit no start-up line, so
this list is the record of them.

### The telnet operator console (`:2300`) and web terminal (`:8100`)

Since v0.94.0 both listeners default to loopback (`console_bind` and
`console_web_bind` are both `127.0.0.1`), so on a **fresh** install neither is
reachable from the LAN. An upgrade keeps whatever binds are already stored in its
options. This section describes what you take on if either is on the network.

Both can ring, connect, hang up,
transfer, page, set message-waiting, and control lights. The blast radius is
bounded — at most 5 concurrent sessions, a 15-minute idle reclaim, and (for the web
terminal) a same-origin WebSocket gate that blocks cross-site drive-by hijacking —
but anyone who can reach the port from a same-origin context can drive the board.

If either is on the LAN: the telnet console is **unauthenticated by design**; the
**web terminal supports a login** — configure `console_users` (username + masked
password) and both its page and its WebSocket require a signed-in session, with
per-address throttling of failed attempts. With an empty `console_users` the web
terminal is as open as the telnet console — **and so it is with a `console_users`
value that fails to parse, or whose rows are missing a username or a password**:
those rows are dropped, and a list left with no valid rows disables the gate
entirely. The start-up log line reports which mode is actually live
(`login gate ACTIVE (n user(s))` vs `no login gate`); trust that line over the
options page.

**Prefer the Ingress console.** Since v0.93.0 the same terminal is served at
`/console/` on the Ingress port, where Home Assistant's own session authenticates
it and the guard in §1 pins the caller to the Supervisor. It needs no
`console_users` entry because there is no second front door to protect — which
also means no login to leave disabled. The standalone `:8100` terminal remains
for now and is scheduled for removal.

**The cost of that move, stated plainly:** the terminal now runs inside the Home
Assistant frontend's *origin*. A cross-site-scripting bug in the console page is
no longer confined to a terminal on its own port — it is an XSS against the HA
session. Only `xterm.js` and `xterm.css` are served to that page, from an
exact-match allowlist of two filenames (no directory serving, no path joining on
the parameter), which is the surface that keeps that trade acceptable.

**Mitigations you control:** both listeners already default to loopback
(`console_bind: 127.0.0.1`, `console_web_bind: 127.0.0.1`), so neither is on the
LAN unless you change it. **`console_web_bind` is the web terminal's own setting
and takes precedence**; `console_bind` is consulted only when `console_web_bind`
is left empty — so set the one you mean. If you do put the `:8100` terminal on the
LAN, configure `console_users` to gate it.

`console_web_enabled: false` turns off the standalone terminal and does **not**
affect the Ingress console. `console_enabled: false` **does**: the Ingress
terminal bridges to the operator console on `127.0.0.1:2300`, so disabling that
console — or binding it to anything other than loopback — leaves `/console/`
showing "unavailable".

### The AppArmor profile is coarse

The add-on runs under a named AppArmor profile that mediates the container (no host
escape), but the profile grants broad file/signal/capability/network access — the
documented Home Assistant add-on pattern for an s6 + Asterisk workload. Treat it as
container mediation, not a least-privilege sandbox.

### The cordless handset's certificate

The WP826 presents a self-signed certificate that cannot be replaced, so the
device-health monitor and the maintenance tool cannot validate it by chain.
Set **`cordless_cert_sha256`** (obtain it with `WP826_HOST=<cordless-ip> node tools/wp826.mjs fingerprint`)
and both will verify the exact certificate **before** transmitting the admin
password, refusing to continue on a mismatch. Left blank, the connection still
works but is unauthenticated: a LAN-positioned attacker could impersonate the
handset and capture that password.

### Device tooling accepts a self-signed certificate

`tools/wp826.mjs` is a **developer-only utility** (not shipped in the add-on image)
that administers a Grandstream WP826 cordless over its HTTPS API. The phone presents
a self-signed certificate with no validatable chain, so the tool sets
`rejectUnauthorized: false` and reads its admin password from a local file. This is
an accepted LAN-local risk for a personal device-admin tool and is not part of the
add-on's request path. (Two CodeQL alerts flag this `rejectUnauthorized: false`;
the rule `js/disabling-certificate-validation` is listed for `tools/wp826.mjs` in
`.github/codeql-baseline.json` with that justification, so it does not fail CI.
The alerts are **not** dismissed — they stay visible in the Security tab.)

---

## What you must configure

1. **Change the default room secrets** (`change-me-101`, `change-me-102`, …) before
   your phones register. Use a strong, unique `secret` per room; the validator
   rejects `;`, whitespace, and control characters.
2. **Nothing, for the consoles, on a fresh install** — `console_bind` and
   `console_web_bind` both default to `127.0.0.1`. If you are upgrading from
   before 0.94.0 your stored binds are unchanged, so check them; and if you
   deliberately put either on the LAN, configure `console_users` for the web
   terminal (the telnet one cannot be authenticated at all).
3. **To allow LAN-triggered announcements**, set a non-empty `announce_token`;
   otherwise `/api/announce` is Supervisor-only.
4. **Use a strong trunk secret** if you enable the outside line, and set
   `inbound_ext` to route inbound calls where you want them.
