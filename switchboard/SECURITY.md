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

---

## Security model

Defense is layered. Each subsystem below states what is defended and how.

### 1. The Ingress management dashboard is Supervisor-only

The dashboard runs behind Home Assistant Ingress, but because the add-on uses host
networking, its port (`8099`) is *also* directly reachable on the LAN — which would
bypass Ingress authentication. To close that, an ASGI middleware rejects any
request whose source IP is not the Home Assistant Supervisor (`172.30.32.2`) or
loopback, returning **HTTP 403**. This preserves Ingress auth without changing the
bind.

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
origination is pinned to a **fixed internal target**: test-ring and announce run a
fixed `Playback` to a known room, while connect, wake-up, and page originate into a
fixed internal dialplan context (`rooms`, `wakeup-deliver`, and `page`
respectively). Every extension is validated against the configured room set before
the call, so an origination can only ever ring an internal phone — it cannot be
steered into an outside call even with the privilege.

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
  secret. Logging goes to the console channel captured by journald — there is no
  persistent on-disk log file.
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

- **Blocked prefixes.** International (`011`) and premium (`900`, `1-900`) numbers
  are matched *before* the general outbound rule and routed to congestion.
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
fit your network. Each logs a warning at start.

### The telnet operator console (`:2300`) and web terminal (`:8100`)

Both are **unauthenticated on the LAN by design** and can ring, connect, hang up,
transfer, page, set message-waiting, and control lights. The blast radius is
bounded — at most 5 concurrent sessions, a 15-minute idle reclaim, and (for the web
terminal) a same-origin WebSocket gate that blocks cross-site drive-by hijacking —
but anyone who can reach the port from a same-origin context can drive the board.

**Mitigations you control:** set `console_bind: 127.0.0.1` (the web terminal
follows it) to make them host-local, or disable them with
`console_enabled: false` / `console_web_enabled: false`. The Home Assistant Ingress
dashboard remains the authenticated management surface.

### The AppArmor profile is coarse

The add-on runs under a named AppArmor profile that mediates the container (no host
escape), but the profile grants broad file/signal/capability/network access — the
documented Home Assistant add-on pattern for an s6 + Asterisk workload. Treat it as
container mediation, not a least-privilege sandbox.

### Device tooling accepts a self-signed certificate

`tools/wp826.mjs` is a **developer-only utility** (not shipped in the add-on image)
that administers a Grandstream WP826 cordless over its HTTPS API. The phone presents
a self-signed certificate with no validatable chain, so the tool sets
`rejectUnauthorized: false` and reads its admin password from a local file. This is
an accepted LAN-local risk for a personal device-admin tool and is not part of the
add-on's request path. (Two CodeQL alerts flag this `rejectUnauthorized: false`;
they are dismissed with that justification.)

---

## What you must configure

1. **Change the default room secrets** (`change-me-101`, `change-me-102`, …) before
   your phones register. Use a strong, unique `secret` per room; the validator
   rejects `;`, whitespace, and control characters.
2. **If your LAN is not fully trusted**, bind the consoles to loopback
   (`console_bind: 127.0.0.1`) or disable them.
3. **To allow LAN-triggered announcements**, set a non-empty `announce_token`;
   otherwise `/api/announce` is Supervisor-only.
4. **Use a strong trunk secret** if you enable the outside line, and set
   `inbound_ext` to route inbound calls where you want them.
