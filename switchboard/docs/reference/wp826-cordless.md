# The WP826 WiFi cordless (optional)

A Grandstream **WP826** WiFi cordless can join as an ordinary room extension —
register it to the add-on the same way (SIP server = your Home Assistant IP, user
ID / auth ID = its extension, password = its `secret`). Beyond being a phone, the
cordless integrates in three extra ways:

- **Home Assistant announce endpoint** — with `POST /api/announce/{ext}` targeting
  the cordless's extension, Home Assistant can speak alerts on it hands-free
  ([§6](#6-paging--announcements)).
- **Remote phonebook** — the add-on serves your rooms as a Grandstream GS
  Phonebook XML document at `http://<ha-host>:8099/phonebook.xml` (one `<Contact>`
  per room: the room `name`, the `ext` as its number). Point the handset's
  **Remote/XML Phonebook** at that URL — `P330 = 1` (HTTP) and
  `P331 = <ha-host>:8099/phonebook.xml`, see
  [`tools/wp826-pcodes.md`](../tools/wp826-pcodes.md) — and it shows every room by
  name, on caller ID and in its own directory. It is rendered from your live
  `rooms` option on each fetch, so renaming a room needs no re-upload; rooms whose
  `ext` fails validation are skipped and names are XML-escaped. This URL is
  **LAN-reachable and unauthenticated**. The handset cannot ride Ingress, so this
  read-only GET is exempt from the Supervisor-only guard on `:8099`. The guard
  has two other exemptions: a Home Assistant media player fetching a speaker
  announcement clip (`GET /announce/a<digits>.wav`), and
  `POST /api/announce/{ext}` carrying a valid `X-Announce-Token`. Every other
  request on the port is refused unless it comes from the Supervisor (see
  [SECURITY.md](SECURITY.md)). The phonebook exposes room names and internal
  extensions only — the same directory already printed on every handset — and no
  secrets.
- **Device-health monitoring** — set `cordless_ext` (its extension) and
  `cordless_password` and the add-on polls the phone's own API for battery, WiFi
  signal, and per-call MOS, publishing `sensor.switchboard_cordless_health`. With
  `cordless_ext` set it **auto-follows the phone's IP** from its live registration,
  so DHCP moving the handset never breaks monitoring (`cordless_ip` is just the
  fallback)
  ([§11](#11-health-monitoring--home-assistant-sensors)).

For scripting the cordless's own settings (remote phonebook, distinctive ring,
custom ringtone, speed-dial keys), the repo ships a standalone tool and a P-code
reference at [`tools/wp826.mjs`](../tools/wp826.mjs) and
[`tools/wp826-pcodes.md`](../tools/wp826-pcodes.md).

---

## Pinning the cordless certificate

The WP826 serves its **own self-signed certificate** and offers no way to install
a CA-signed one, so ordinary TLS validation can never succeed against it. Both
the health monitor and `tools/wp826.mjs` therefore skip chain validation — which
means, unpinned, a device on your LAN could impersonate the handset and capture
the **admin password** the monitor sends every poll.

Pinning the certificate closes that. Read the fingerprint:

```
WP826_HOST=<cordless-ip> node tools/wp826.mjs fingerprint
```

Set the value as `cordless_cert_sha256` (and, for the tool,
`export WP826_CERT_SHA256=<fingerprint>`). From then on the monitor compares the
presented certificate **before** the login body is written, and refuses to send
credentials on a mismatch; the tool exits non-zero before logging in. Colons,
spaces, upper case and a `sha256:` prefix are all accepted.

A factory reset regenerates the certificate — re-run the
`WP826_HOST=<cordless-ip> node tools/wp826.mjs fingerprint` command above and
re-pin after one, or the monitor will (correctly) stop authenticating:
`sensor.switchboard_cordless_health` then reads `degraded`, with the reason that
the cordless answers on the network but its admin API is unreadable (wrong
`cordless_password`, or a `cordless_cert_sha256` mismatch), and its battery,
Wi-Fi and call-score attributes are no longer published. The
`WP826_HOST=` prefix is not optional: without it the tool falls back to a
built-in default address and would fingerprint whatever answers there.
