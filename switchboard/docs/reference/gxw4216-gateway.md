# Grandstream GXW4216 V2 provisioning

Each FXS port becomes one SIP **user** that registers to this add-on. The GXW is
configured through its own web UI (this add-on does not push config to it).

## 7.1 Point the gateway at Home Assistant

**Profiles → Profile 1 → General Settings**

- **SIP Server**: the LAN IP of your Home Assistant host. (The add-on uses host
  networking, so Asterisk listens there on UDP 5060.)
- **SIP Transport**: UDP
- **NAT Traversal**: No (everything is on the LAN)

**Profiles → Profile 1 → Audio Settings**

- **Preferred Vocoder**: **PCMU (G.711 µ-law)**. Switchboard only offers µ-law, so
  PCMU must be in the gateway's list; anything else it advertises is simply never
  selected ([§13](#13-codecs--g711-µ-law-only-on-purpose)).
- **Disable** silence suppression / VAD for the cleanest analog audio and to keep
  antique sets' tones intact.

## 7.2 Configure each FXS port

For each wired port, under **FXS Ports**:

| Field | Value |
|-------|-------|
| **SIP User ID** | the extension, e.g. `11` |
| **Authenticate ID** | the same extension |
| **Authenticate Password** | the room's `secret` from the add-on options |
| **Name** | the room label, e.g. `Kitchen` |
| **Profile ID** | Profile 1 |
| **Enable Port** | Yes |

Save & **Apply**; reboot the gateway if ports don't register.

> **Message-waiting stutter tone (optional).** For the message-waiting indicator
> (dashboard ✉, console `M`) to produce the classic **stutter dial tone** on an
> antique handset, enable **"Send Stutter Dialtone for MWI"** / **"MWI → Stutter
> Tone"** in Profile 1 (or per port); the label varies by firmware. Without it the
> indicator still tracks in the dashboard, but the dial tone won't stutter.

## 7.3 Configuring the gateway from the CLI (reliable path)

The GXW's web UI works, but its **SSH command shell** is the authoritative,
scriptable way to read and write settings — and, importantly, the **only** way to
*confirm* a value. Grandstream's `export`/HTTP config views show the *firmware
default* for many Profile-1 codes, not the committed value; only `get <n>` in the
shell reflects what's actually running. The shell takes the **bare number**:
`get 4200` reads the dial plan, while `get P4200` prints `(null)` whatever the
value is.

```
ssh admin@<gateway-ip>        # password = the GXW admin password
> config                      # enter config mode
CONFIG> get 4200              # read a value (authoritative; no "P")
CONFIG> set 85 3              # change a value
CONFIG> commit                # persist
CONFIG> exit
```

Useful P-codes (Profile 1 is shared by every FXS port assigned to it in §7.2; type
them at the shell without the `P`):

| P-code | Setting | Reference value |
|--------|---------|-----------------|
| `P32` | Register Expiration (minutes) | `2` (fast re-register after a restart) |
| `P37` | **Voice Frames per TX** (G.711 ptime = value × 10 ms) | `2` (20 ms) |
| `P57` | Codec preference | µ-law first |
| `P72` | Use `#` as dial key | `1` (enabled) |
| `P81` | **Unregister On Reboot** (Profile 1) | `1` — a gateway restart then leaves `Removed contact … due to request` in `/data/state/asterisk.log` (and in the Log tab at `info`, `debug` or `trace`); see §14 |
| `P85` | **No Key Entry Timeout** (global; seconds the gateway waits after the last digit before sending) | `3` — see §7.4 |
| `P4200`–`P4203` | **Dial Plan** (per profile; the FXS ports use Profile 1 = `P4200`) | see below |
| `P21929` | **Automatic Reboot** (global; `0` off, `1` daily, `2` weekly, `3` monthly) | `2` (weekly). Optional: every wired phone drops for about a minute at each scheduled reboot; see §14 |
| `P21930` | Automatic Reboot hour | `1` (01:00) |
| `P21931` | Automatic Reboot day of the week (weekly schedule) | `1` (Monday) |
| `P28118` | Automatic Reboot day of the month (monthly schedule) | not used with a weekly schedule |

## 7.4 Dial plan & the send delay

A dial plan that supports **prefix-free direct dial** (`direct_dial: true`, §9) —
2-digit rooms, feature codes, and `1` + 10-digit outside numbers — for the
reference home's `11`–`20` extensions:

```
{ 0 | 1[1-9] | 20 | 4[1-7] | 411 | 911 | 933 | 1[2-9]xxxxxxxxx | \+x+ | *x+ | *xx*x+ }
```

- `1[1-9]` = rooms 11–19; `20` = the softphone; `4[1-7]`/`411` = feature codes
  (`47` is the voice assistant, added in 0.69.0); `911`/`933` are always
  emitted by the add-on and must be dialable from the gateway or the spoken
  emergency notice cannot be reached at all.
- `1[2-9]xxxxxxxxx` = 1 + 10-digit direct dial. This **overlaps** rooms 12–19
  (each is both a complete room *and* the start of an 11-digit number), so those
  extensions send only after the **No Key Entry Timeout** (`P85`) — or immediately
  if you press `#` (`P72` is enabled). Room 11, `20`, `0`, and feature codes
  `42`–`47` are unambiguous and send instantly.
- **`41` is the one feature code that is not instant.** With the talking clock on
  `41` and directory assistance on `411`, `41` is both a complete code and the
  first two digits of a longer one, so the gateway cannot know you are finished
  and waits out `P85` exactly like rooms 12–19 (press `#` to send at once). `411`
  itself is unambiguous once the third digit lands. If that pause on the clock
  bothers you, move one of the two off the collision. Pick a code that is not
  already taken — `47` is the local voice assistant, and setting `clock_ext: 47`
  would collide with it and silently disable one of the two (a collided feature
  code is skipped with a log line, not an error). `48` or `49` are free; or move
  `directory_ext` to something that isn't a `41…` prefix.
- **`P85 = 3` seconds** is the reference value: it trims that 12–19 pause from the
  firmware default of 4 s while staying above a **rotary/pulse** phone's
  inter-digit gap, so a slow rotary dial of a long number isn't cut off mid-number.
  Dropping to 2 s risks that on rotary sets. It's a single global value (no
  per-port setting).

For **pulse/rotary** phones, enable the **Pulse Dialing** option on that FXS port.

If you're not using direct dial, a simpler prefix-mode plan works:
`{ 1x | 20 | 4[1-7] | 411 | 911 | 933 | 0 | 9911 | 9933 | 9xxxxxxxxxx }` (dial
`9` for an outside line; `911` and `933` are accepted both bare and prefixed, and
both forms wait for `P85` because they also begin `9xxxxxxxxxx`).
Here rooms `11`–`19` send instantly (nothing longer starts with `1`), but the
`41`/`411` overlap above is unchanged — it comes from the feature codes
themselves, not from direct dial.

## 7.5 Verify

On the **Switchboard** panel, each provisioned room shows **Registered** within
~30 s. If not, see [§14](#14-troubleshooting).

---
