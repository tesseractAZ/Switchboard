# Health monitoring & Home Assistant sensors

Three monitors watch different things and publish Home Assistant
sensors. Within each monitor the **alert** toggles only control the pop-up
notifications — the sensors keep publishing either way.

> **They are not independent.** The link-health poller is the only one of these
> three monitors with AMI access, so three things it publishes are inputs to the
> others:
> `sensor.switchboard_gateway_health` is *derived* from the link-health rollup,
> the cordless's IP auto-follow reads `contact_ip` off the per-phone sensors,
> and the trunk watchdog runs inside the same poller. Setting
> **`link_health_enabled: false` therefore also silences gateway health, the
> trunk sensor and its auto-recovery, and the cordless half of device health** —
> with no alert to tell you so. Leave it on unless you mean to turn all of that
> off.
>
> One thing it does **not** stop: the same service keeps the readable log copy,
> `/share/switchboard/asterisk.log`, free of SIP accounts and LAN addresses (see
> SECURITY.md). With link health off it runs that scrub alone, every 300 s,
> rather than idling.

## Link health (`link_health_*`)

Polls every phone's PJSIP registration and qualify round-trip latency **between
calls**, so a degrading link (especially the WiFi cordless) shows on a graph before
a call ever drops. Publishes:

- `sensor.switchboard_link_<ext>` — per phone; state is the RTT in ms, or
  `offline` / `unavailable`.
- `sensor.switchboard_link_health` — a rollup (worst RTT; counts of reachable /
  unreachable / offline; the down extensions).

A **second detector**, added in v0.79.0, covers the outage no sample can see.
Asterisk logs every reachability change as it happens, and those lines are read
each cycle; if half the fleet went unreachable *between* two polls and was back
before the next one, that is reported too. The one whole-house outage this
system has had lasted 119 seconds and fell entirely inside a single five-minute
gap, so both surrounding samples read healthy. Only **phone** extensions count
toward that half: the outside line logs its own reachability changes in the same
place, and it is not one of the phones the threshold is measured against. Its
changes still appear in `heartbeat.jsonl`.

**After a restart** the poller checks every 15 s instead of every
`link_health_interval` until every gateway port, and every other phone that has
ever registered (in practice the cordless), is back — or eight checks (about two minutes)
have passed, whichever is first. An extension that has never registered (a
configured softphone that is never used) does not hold it open. Before this, the
fast checks stopped as soon as the wired ports were back. Three restarts in a row
ended warm-up with the cordless still missing, so the next check came five minutes
later. After one of them the cordless showed `offline` for more than four and a
half minutes after it had re-registered, and the rollup's `worst_rtt_is_partial`
read `false` with the cordless missing. The published `poll_interval_s` stays at the steady interval
throughout. The last fast check is always followed by a full-length sleep, and a
15 s figure would make every staleness check call the sensors stale after every
restart.

It raises **one** notification on a mass outage — at least half the fleet *and* at
least 3 phones unreachable for 2 consecutive cycles — so a shared-gateway failure
(e.g. the GXW loses power) can't go unnoticed. Recovery posts a second
notification ("phones recovered") under the **same** `notification_id`, which
replaces the outage entry rather than dismissing it; the same replace-on-recovery
pattern is used by the trunk watchdog ([Registration resilience](sip-trunk.md#registration-resilience))
and by device health below.

## Per-call quality (`call_quality_alerts`)

Every context that can carry audio writes a record to the ledger
(`/data/state/callqos.jsonl`): room-to-room calls, the operator, directory
assistance and outside calls, **and** — since v0.55.0 — wake-up set and
delivery, paging, announcements and the voice menus — and, since v0.77.0,
**transfers**, which were one of three contexts without the hook. Before v0.55.0
only the first four reported, so the ledger under-reported activity roughly
fivefold.

**Recorded is not the same as alerted.** The legs the PBX originates to play
something *at* a phone — wake-up delivery, paging, announcements — never move
`sensor.switchboard_last_call`. A three-second chime is not "the last call", and
those legs are one-directional by design, so the one-way-audio detector (which
exists to catch a broken *conversation*) would fire on their perfectly normal
shape. Both of those exemptions are about the shape of the leg.

**A bad line under one of them now speaks up.** Until 2026-09-15 those same tags
also silenced the ordinary poor-quality alert, and that turned out to be the
wrong thing to silence. The rule had been written for a clip that got *cut
short*, which is a fair thing to stay quiet about — but a cut-short clip is
handled earlier and never reached the rule. All the rule could actually suppress
was a **bad connection to the handset**, and that is the same connection the next
real conversation on that phone will use. Over fifty days it hid five of them.
The worst was an announcement at 00:26 that played all the way through with
round trips peaking near nine tenths of a second. Asterisk scored the stream it
received from the cordless at MOS 3.4, and the clip the cordless heard at about
3.8. Nothing else noticed, because the phone was reachable the whole time. Those legs now raise a card like any other. On the same fifty
days of real calls that is a change from about **1.8 to 4.8 cards a month**.

**A clip that was cut short still stays quiet, with one exception, added in
v0.78.0.** A wake-up delivery that was answered and then failed to deliver is
recorded `undelivered` and *does* alert. A page cut short is not worth waking
anyone over; an alarm clock that did not go off is the one thing on this list
with a deadline.

Where that line falls was corrected in v0.97.0. A wake-up counts as delivered
once the script has reached the **greeting** *and* at least a second of audio has
left the box — so somebody woken by the greeting who hangs up on it is a success,
not an alarm. It is `undelivered` when the call stopped before the greeting, or
when it was answered and carried no audio at all. That second case is the one
this whole mechanism exists for: an alarm clock picked up in silence.
Conversations and the interactive menus alert exactly as before.

**Whether the card was actually posted** is recorded on the leg itself, as
`notify_status`: `posted` (Home Assistant accepted it), `failed` (the post was
refused or could not be made), `disabled` (an alert was called for and
`call_quality_alerts` is off) or `skipped` (the leg called for none). The
recorder runs detached from the call with nowhere to write an error, so until
2026-09-14 a card that failed to post left no trace at all. The field is in the
`/share` mirror too; records carrying it are schema version `4` or later.

**Whether the sensor was updated** is recorded the same way, as `sensor_status`:
`posted` (Home Assistant accepted the state), `failed` (the update was refused or
could not be made) or `skipped` (the leg does not drive the sensor: not the dial
plan's record, a playback leg, or no credible score). Before v0.111.0 a failed
update left no trace. The field is in the `/share` mirror too; records carrying it
are schema version `6`.

**Two spellings of the time, on purpose.** Each record's `ts` is plain epoch
seconds, and has to stay that way because the cordless health monitor reads it as
a number on every line. But every *other* ledger in `/share/switchboard` writes
its timestamp as an ordinary `2026-09-15T00:26:35+00:00` date string, so anyone
filtering the folder by date quietly matched nothing here and read the file as
empty — one review reported "no calls" for a stretch that held eighteen. Records
from 2026-09-15 carry `ts_iso` as well: the same instant, in the same spelling as
its neighbours. Those records are schema version `5`.


After each call, scores the worse of the two audio directions from the RTP/RTCP
stats and publishes `sensor.switchboard_last_call` (an MES score, with loss,
jitter, RTT, codec, and duration as attributes). Notifies on a genuinely rough call
— low score (MES under 70), loss of **1.5 % or more** in either direction (it was
3 % through v0.115.0), high latency (a round-trip **mean over 250 ms** or a
**peak over 500 ms**, scored across the whole call rather than from the last
RTCP round), **one-way audio**, or a receive-jitter **peak over 60 ms** on a
conversation.

**Jitter alerts only on conversations, and never changes the grade.** The
figure used is `jitter_rx_peak_local_ms`, the highest value Asterisk's own
receive-jitter estimate reached during the call. It measures the stream the PBX
receives, which reaches a listener only on room-to-room, outside (`outbound`),
inbound (`from-trunk`) and transferred calls, and on calls the operator or
directory connects. On a wake-up, page, announcement or voice menu it is the
handset's microphone, and there the WiFi cordless routinely peaks at 40–58 ms
with no loss. A jitter alert names the peak and leaves the excellent/good/fair/poor
label where MES and loss put it, as an RTT alert does. Asterisk 20.11.1's MES
gives jitter almost no weight, because it multiplies the jitter in seconds. The
two other jitter fields, `jitter_rx_last_ms` and `jitter_tx_last_ms`, are
recorded and drive nothing.

**Not every call updates the sensor.** Each leg is appended to the durable JSONL
ledger, but the sensor is only pushed when the leg produced a *credible* MES, and
only from the authoritative hangup record written by the dialplan
(`source=dialplan`). A reading is discarded as not credible when Asterisk reports
`0.0` for a direction it could not score — a short call, or RTCP that never
converged, never a genuine 0 MOS — or when a collapsed MES (< 40) arrives
alongside ~0 % loss in the stream it scores (`mes_rx` with `loss_tx_pct`,
`mes_tx` with `loss_rx_pct`; through v0.115.0 each was checked against the
other stream's loss), a receive-jitter peak no higher than 60 ms and a low RTT,
which is the
signature of a re-INVITE/transfer glitch rather than of bad audio — or, since
v0.77.0, when it reports its *other* nothing-to-report constant, the literal
`88.087887`, on a leg where no RTCP round completed. That one reads as a flawless
call rather than an absent one: three such legs were scored `excellent` while a
fourth, identical but carrying the `0.0` constant, was scored `unknown`. The
verdict was decided by which of two placeholders Asterisk happened to leave
behind. If neither
direction is credible there is no score to publish and the sensor simply keeps
its previous value; the raw per-direction numbers are still in the ledger. The
alerting is independent of this: a call with no credible MES can still notify on
loss, latency, or one-way audio, since one-way is detected from packet counts.

## Device health (`device_health_*`)

Covers the two blind spots the above can't see:

- The **WP826 cordless**'s own battery %, WiFi signal, and most-recent-call MOS
  (needs `cordless_password`) → `sensor.switchboard_cordless_health`. Flags
  CRITICAL when unreachable or the battery is dying, degraded on low battery / weak
  WiFi / a recent poor call.
- The **GXW gateway**'s port health, derived from the link-health rollup →
  `sensor.switchboard_gateway_health`. All wired ports down = the gateway likely
  lost power or its uplink.

Both use a 2-cycle hysteresis so a transient blip doesn't alert; a worsening from
degraded to critical alerts again. Both post a recovery notice when they return
to normal, again under that device's shared `notification_id`, so the recovery
replaces the alert rather than removing it. For the cordless, "recovered" is
reserved for a clear backed by a newer scored call. A clear with no newer call
(typically because the poor call simply aged out of the 15-minute window) is
titled "alert lapsed" instead, and says the state is unknown until the next call.

**When a poor call counts.** The handset scores every call itself. Each score is
matched to a call-quality ledger leg that hung up within 90 s of it: the
**nearest** one, except that the cordless's own leg (`cordless_ext`) is preferred
unless another phone's leg hung up more than 5 s nearer. On the three occasions
measured, the handset's clock and the phone system's put the same hangup within
2 s of each other. The score is skipped when there is no such leg,
or when the nearest leg, or the cordless's own leg preferred over it, is a wake-up
delivery, page or announcement: the handset has been seen to score those playback
legs between 2.2 and 2.9.

Calls that never happened are left out of that search, as of 2026-09-15. A
misdialled room number still leaves a row in the ledger — dialled, rejected, zero
seconds long, no audio either way — and one of those landing a few seconds nearer
than the real call used to win the match. When it did, there was nothing in it to
agree or disagree with, so a genuinely poor score was quietly written off as
unmeasured. The nearest leg that actually carried audio is now preferred. A real
call the phone system simply could not score is *not* in this category: that is a
measurement that came back empty, and it still matches and still reads
`unmeasured`.

A score below `mos_min` (a fixed 3.4, not an add-on option) from a call that ended
within the last 15 minutes degrades the sensor only when the same leg agrees in
the ledger, in the direction the handset hears. That means at least 1 % loss on
the way to the handset (`loss_tx_pct`), or an MES below 78 for that same stream
(`mes_rx`). Both come from the handset's own receiver reports. The two names
point opposite ways because Asterisk names its loss counters after the direction
of travel and its scores after the report they were built from. `mes_tx` is the
PBX's score for the handset's microphone, and through v0.115.0 it was the MES
this check read. A call made **to** the cordless is logged under the caller's
extension, so its figures describe the caller's phone and never count either way.

Otherwise the score is still published (`last_mos`, with `last_mos_age_s` and
`last_mos_uncorroborated: true`), but the state stays `ok`. On 2026-09-14 the
handset scored 2.2 on three calls the ledger measured at 0 % loss and MES 88, and
raised three false `degraded` alerts. Why it does that is not yet known.

To find out, every score below `mos_min` is captured once, whether it counted or
not, to `/data/state/cordless-mos.jsonl`. That file is private to the add-on,
readable by root only, capped at 512 KB, and never copied to `/share`. Each row
carries the handset's whole RTP record, the ledger leg it matched, `match_rule`
(`own-ext` for the cordless's own leg; `nearest` for another phone's leg, or when
`cordless_ext` is not set), the gap between the two clocks, and a `verdict`:

| `verdict` | Meaning |
|-----------|---------|
| `corroborated` | The ledger leg was impaired too; the score counts toward `degraded`. |
| `playback` | The nearest leg, or the cordless's own leg preferred over it, was a wake-up delivery, page or announcement; skipped. |
| `uncorroborated` | The ledger measured that direction clean; published, does not degrade. |
| `unmatched` | No ledger leg within 90 s; skipped. |
| `unmeasured` | No usable transmit figure for the cordless: Asterisk could not score that direction, or the leg belongs to another phone (such as a call made to the cordless). Published, does not degrade. |

```
sudo docker exec addon_<slug> tail -n 5 /data/state/cordless-mos.jsonl
```

| Sensor | What it tells you |
|--------|-------------------|
| `sensor.switchboard_cordless_health` | Cordless health **level** (`ok`/`degraded`/`critical`) as the state — battery %, Wi-Fi signal, and the reason live in the attributes. `last_mos` is the handset's own score for its most recent matched call, `last_mos_age_s` how many seconds ago that call ended, and `last_mos_uncorroborated` is `true` when that score is below `mos_min` but the call-quality ledger did not measure the same problem (see *When a poor call counts* above). (Before v0.48.0 the state was the raw battery number, which made a battery-driven `critical` invisible without opening the attributes.) |
| `sensor.switchboard_gateway_health` | GXW gateway port health |
| `sensor.switchboard_last_call` | Last **conversation's** audio quality (MES) + details. Machine-initiated legs (wake-up delivery, paging, announcements) are recorded in the ledger but never drive this sensor. They are also exempt from the one-way-audio check, because their one-directional shape would trip it by design. Since v0.104.0 a poor line under one of them (low MES, loss or latency) raises an ordinary call-quality card like any other leg, while a clip that was merely cut short stays quiet. The exception to that silence is a wake-up delivery that was answered but never got a second of audio out, or that stopped before the greeting: that is scored `undelivered` and does alert, because an alarm clock that did not go off is the one thing on this list with a deadline. |
| `sensor.switchboard_link_<ext>` | Per-phone reachability + latency (ms) |
| `sensor.switchboard_link_health` | Fleet rollup (worst RTT, who's down) **Its state is a max over *reachable* phones only, so it is not monotonic in fleet health:** when the slowest phone drops off entirely it leaves the sample and the number *improves*. The `worst_rtt_is_partial` attribute is `true` whenever a phone that has ever registered is missing from the sample — including straight after a restart, before that phone has answered again — don't threshold on the state alone. Use `wired_link_health` for latency and `unreachable_exts` for availability. |
| `sensor.switchboard_trunk_health` | Outside-line SIP registration status (`Registered`/`Rejected`/`Unregistered`/…). Published only when the trunk is enabled **and** registers (`registns: true`); a trunk that authenticates per-INVITE has nothing to watch. `unknown` means Asterisk answered but reported no registration object, which counts as down. A cycle in which Asterisk could not answer at all (for example in the seconds after a restart) publishes nothing, and the sensor keeps its last value. `auto_reregister_attempts` counts the re-registers Asterisk accepted (a refused one is not counted), and `next_reg_s` is the refresh countdown when one is running. A ~24 h silent inbound outage motivated this sensor — see [Registration resilience](sip-trunk.md#registration-resilience). The watchdog lives inside the link-health poller: `link_health_enabled: false` disables this sensor, the automatic re-register, **and** its notification; the notification also honors `link_health_alerts`. |
| `sensor.switchboard_wired_link_health` | Median round-trip latency of the **wired GXW ports only** (`gateway_ports`), with `max_rtt_ms` and `ports_measured` attributes. Reported apart from the rollup above because that one is a fleet **worst case**, which the Wi-Fi cordless pins with its far larger latency variance — so the wired ports could degrade from 2 ms to 40 ms without moving it. (When the split was introduced the cordless idled near 250 ms under Wi-Fi power save; on its charger it now idles near 9 ms. The gap narrowed, the masking did not.) This is the number to graph and alert on for the analog phones. |

> Pushed sensors are recreated after each poll and clear on a Home Assistant
> restart until the next push — that's expected. A pushed sensor never expires,
> though: if the add-on stops while Home Assistant keeps running, every value
> freezes at its last reading. `link_health`, `wired_link_health`,
> `trunk_health`, `cordless_health` and `gateway_health` therefore carry
> `measured_at` (when the value was measured) and `poll_interval_s` (the poller's
> steady interval). Compare the two against the current time before trusting the
> state. Gateway health does this itself: it publishes `unknown` when the
> link-health rollup is missing, more than 2.5 poll intervals old (at least
> 60 s), or measured before the device-health monitor last started. That last case
> covers the rollup a previous run left behind across an add-on restart; it reads
> `unknown` until the link-health poller publishes again, and `unknown` never
> raises an alert.

---

**Where to read them.** The full call-quality ledger is
`/data/state/callqos.jsonl`, inside the add-on and not readable from outside.
Three ledgers in the host-mounted `/share/switchboard/` can be read without a
shell. `callqos-outcomes.jsonl` mirrors the call-quality ledger record for
record, except that an `ext` longer than an extension (a telephone number on a
trunk leg) is masked to its last four digits and flagged `ext_redacted: true`.
v0.99.0 made that rule length-only; before then a number arriving as `+1…` went
through unmasked. `delivery-outcomes.jsonl` is not a mirror but the delivery
ledger itself: every wake-up and announcement outcome, the only copy, and the
record that the wake-up and announcement reconcilers, the announcement retry
and the duplicate check read. `heartbeat.jsonl` holds one row per health cycle.
The same folder also holds `asterisk.log` (the scrubbed readable log) and
`backup-window.jsonl` (when the Supervisor began and finished copying this add-on's own image and `/data` in each backup; when a backup includes `/share`, the Supervisor archives it after every add-on in that backup, with its writers still appending; trimmed to its newest half past 256 KiB). Each heartbeat row's `interval_s`, `settled` and `phase` describe the
cycle the row ran in, and `since_prev_s` how long it actually waited. `next_sleep_s`
is the wait that follows the row. The row that ends warm-up therefore reads
`phase: warmup` beside `next_sleep_s: 300`. `transitions` lists every reachability
change Asterisk logged since the previous row written by the same poller process,
the outside line included. The first row after a restart starts fresh, so a change
logged between the old process's last row and the new one's first cycle does not
appear there (the durable Asterisk log still holds it). With `remove_existing`, a
phone re-registering from a new contact logs Unreachable and then Reachable a few
milliseconds apart; an entry carries no time, so that pair reads like a real drop.
On a row with `ami: unreachable` nothing was measured, so `reachable`, `down` and
`wired_down` are `null`; an empty list on any other row means the cycle measured
and found nothing down.
The assistant's own ledger is deliberately **not** mirrored — see
[Local voice assistant](voice-operator.md#local-voice-assistant--dial-47).
