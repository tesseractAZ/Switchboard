# Wake-up calls & the talking clock

## Wake-up calls — dial `42`

Dial the code (or say "wake me up" to the operator) and, after the beep, say a
time — "seven thirty", "quarter past six", "six a.m.", "nineteen thirty", "noon".
The spoken-time parser is intentionally forgiving; the flow reads the parsed time
back so you can hear it and re-say it if it's wrong. Say "cancel" (or "clear",
"never mind") to remove a pending wake-up.

- **One wake-up per room.** Setting a new one replaces the old.
- At the set time the room rings for `wakeup_ring_seconds` (default 60). The call
  speaks a greeting and the time, runs any smart extras, then repeats the greeting
  and time.
- If the room is busy or offline through a **10-minute grace window**, the wake-up
  is dropped and surfaced as a Home Assistant persistent notification.
- **Every set and cancel is recorded**, in
  `/share/switchboard/delivery-outcomes.jsonl`, with where it was made: `phone`
  (dialled on the room's own phone, or asked of the voice operator from it), `web`
  (the dashboard) or `console` (the operator console). A set carries the time it
  will ring; a cancel says whether there was anything to remove. Until 2026-09-14
  a wake-up's record began at its first ring, so nothing could say who had set it.

**If nobody answers** (v0.70.0, corrected in v0.78.0). Ringing a phone is not
the same as waking somebody, and until v0.70.0 the system could not tell the
difference: an originate reports "queued" the moment the PBX accepts it, so a
wake-up that rang out and one that woke you were recorded identically.

v0.70.0 had the delivery leg record that it was answered, and joined on that.
That turned out to be the wrong fact. The record was written the instant the
handset was picked up, before a word had played — so a wake-up that was answered
in **silence** counted as delivered. It happened: on 2026-09-02 at 06:15:21 a
handset answered and the far end dropped a second later, before the greeting,
with zero audio packets transmitted. Somebody reached for a ringing phone at six
in the morning, heard nothing, and every ledger read healthy.

The delivery leg records a `spoken` milestone written immediately **after the
greeting has played** — Asterisk abandons the script the moment the channel
drops, so reaching that point proves audio reached a live line. Since v0.97.0
the hangup handler also records `audio-delivered` when it measures at least a
second of audio actually transmitted at or after the greeting. Both are read and
either is enough: `spoken` cannot be written by a sleeper who is woken **by** the
greeting and hangs up during it, because the line that writes it never runs, and
that was a real source of six-in-the-morning false alarms. The scheduler joins on
those two, not on the pickup:

1. The phone rings for `wakeup_ring_seconds` (default 60).
2. `wakeup_retry_seconds` after the ring started (default 90), if nothing
   answered, the scheduler re-checks the room and — if the handset is registered
   and idle — **rings it a second time**. The second attempt gets the same check
   the first one does, because an origination reports "queued" the moment
   Asterisk accepts it and says nothing about whether a channel was ever
   created. If the handset is not there, the re-ring is **skipped** and recorded
   as `re-ring-skipped` with the state that caused it.
3. If the wake-up still has not been delivered, it is recorded `undelivered` and
   pushed to `wakeup_push_target` (default `mobile_app_iphone`) as a **critical**
   alert, so it sounds through Do Not Disturb. The alert says only what is known
   — picked up but silent, rung twice, or rung once with the second attempt
   skipped — rather than asserting a second ring it cannot confirm.

**Every way an attempted wake-up can fail now raises that alert** (v0.100.0). There are four, and
until then only the last one did; the other three wrote a line to the ledger and
stopped, which is not what wakes anyone at six in the morning:

| what happened | recorded | the alert says |
| --- | --- | --- |
| Asterisk refused to place the call | `originate-refused` | the phone never rang |
| the handset was gone, so no second ring | `re-ring-skipped` | the second attempt was not made, and why |
| the second ring was attempted and failed | `re-ring-failed` | whether it was refused or unreachable |
| rang twice, or was picked up in silence | `no-answer` / `answered-silent` | which of the two |

A network blip while placing the call is deliberately **not** one of these: it is
recorded `originate-error`, the wake-up stays due, and the next tick tries again.
Treating an unreachable PBX as a failed alarm would cry wolf and then ring the
phone anyway.

A wake-up that is never placed within its 10-minute grace window falls outside
this table. The room may read busy, offline or unknown on every pass
(`deferred`; an Asterisk that cannot be reached at all is recorded this way, with
`device_state: unknown`), or the originate itself may raise on every pass
(`originate-error`). Either way, the
wake-up is dropped as missed with a Home Assistant persistent notification (see
above), not the critical push. Since 0.108.2 its ledger history closes with a
`missed` row (`reason: grace-expired`) after the last `deferred` or
`originate-error` row, and the notification is retried on every scheduler pass
until Home Assistant accepts it, for up to an hour.

The second ring comes first on purpose: the phone is the loudest thing in the
room and it is the device that was supposed to wake you. The push is the fallback
for when the handset itself is the problem. Set `wakeup_push_target` to empty and
the escalation falls back to a Home Assistant notification card instead — the
signal is not lost, it just stops sounding through Do Not Disturb.

If `wakeup_retry_seconds` is shorter than `wakeup_ring_seconds` plus one 20 s poll,
the scheduler judges rings at ring + 20 s instead and logs a WARNING at start-up
naming both values, so a call still ringing is never judged unanswered.

**Snoozing from the ringing phone.** If the room's own phone sets a new wake-up or
cancels one while its wake-up is ringing — or in the short gap before the
scheduler decides whether to ring it again — that is taken as proof somebody is
up. The phone is not rung again and no alert is sent. The ring is recorded
`snoozed`, with `change` (`set` or `cancelled`) and, when a new time was set,
`new_hhmm`. The new time then rings, re-rings and escalates like any other
wake-up. A cancel counts even when it removed nothing: by the time a wake-up
rings, its entry has already been used up.

Only the room's **own phone** counts:

| during the ring, the wake-up was changed from | the ring is |
| --- | --- |
| the ringing room's phone (dial `42`, or the voice operator) | `snoozed` — no second ring, no alert |
| the dashboard or the operator console | judged as usual — whoever changed it may be setting it for somebody still asleep |
| another room's phone | judged as usual |

A change made before the ring began does not count either, and a wake-up that
was heard stays delivered whatever the room does next. If the ledger cannot be
read, the ring is judged as if this rule did not exist: an unneeded alert to
somebody awake is the safer mistake than silence for somebody asleep.

A snooze is recorded once the phone has heard the new time, about ten seconds
after dialling `42`. So if the scheduler comes to judge a ring while the room's
own phone is **on a call** — usually somebody part-way through that call — it
waits up to **90 seconds** before judging, looking again on each scheduler pass
for the change to land. A room still on a call after 90 seconds is judged as
usual, so a handset that is genuinely busy delays the alert by at most that
long. A room the phone system cannot report on never waits, and a phone that is
merely ringing does not count as on a call.

This is a fix for a real morning. On 2026-09-14 one handset snoozed three of its
own wake-ups this way, and was rung a second time twice and sent three critical
alerts saying nobody had picked up.

Every step above is recorded in `/share/switchboard/delivery-outcomes.jsonl`:
`ring-queued`, `deferred` (the room was busy or offline at the appointed time),
`originate-refused` or `originate-error` when the call could not be placed at
all, `answered`, `spoken` and `audio-delivered`, `no-answer` or
`answered-silent`, `ring-requeued`, `re-ring-skipped` or `re-ring-failed`, and
finally `undelivered` — or `snoozed`, when the room's own phone changed the
wake-up during the ring. Each set and cancel is a row of its own, `set` or
`cancelled`, with its `source`. A wake-up whose time passed without it ringing is
`missed` (since 0.108.2), with a `reason`: `grace-expired` when the room stayed
busy or offline through its grace window, `not-running` when Switchboard itself was
down at that time (after a power cut, for example).

A room holds **one** pending wake-up, so setting a second one replaces the
first. Since v0.104.0 the `set` row for a replacement also names the time it
displaced, as `replaced_hhmm` and `replaced_target_epoch` — one row, not two,
and a first-ever set is unchanged. Before that a replacement left no trace at
all: on 2026-09-15 a 06:20 wake-up set at 13:10:35Z was replaced at 13:15:09Z by
a 04:00 one, and the ledger showed the 06:20 being set, then no ring and no
cancel — which is also exactly what a wake-up the system had lost would look
like. One more is worth knowing:
`unjudgeable` means the ledger itself could not be written, so the scheduler
refused to guess whether anyone answered rather than escalate on a broken
instrument. Since 0.108.2 that includes a missed wake-up: reading that file end to end tells you
what happened to a wake-up without needing the call log.

**Smart extras** (during the wake-up call):

- **Scene** (`wakeup_scene`) — activates a Home Assistant scene (e.g. gently raise
  the lights). It is fired the moment the call is answered, in the background, and
  the greeting follows within about a second rather than waiting for Home
  Assistant to reply; it still fires if the call is hung up a moment after
  answering (a hangup in the first fraction of a second, before the scene has
  been handed off, can still stop it). Until 2026-09-14 the greeting waited for
  the scene — 1.7 to 3.8 seconds of silence after picking up, longer when Home
  Assistant was slow — and one pickup hung up two seconds into that silence.
- **Weather** (`wakeup_weather`, on by default) — speaks a short local forecast.
- **Calendar** (`wakeup_calendar`) — reads your next event in the coming 18 hours.

You can also set wake-ups from the **dashboard** with the ⏰ time box and **Set** on
each room card. A room with a pending wake-up shows a **Cancel** button on its own
line on the card, and another in the Wake-up calls list; a cancel that fails says
so. The operator console also sets and cancels them (**W** to set, **X** to
cancel — see [The operator console](operator-console.md)).

## Talking clock — dial `41`

An old-style speaking clock: "At the sound of the tone, the time will be …", then
the time as a 24-hour ("military") H-M-S readout, then a tone — looping until you
hang up. The time is spoken in your configured timezone (`timezone`, or the Home
Assistant timezone if blank).

The tone sounds **on** the second just announced. Like a real speaking clock,
it names a moment a few seconds ahead: the first whole second that the readout
can finish before, worked out from the lengths of the recordings it is about to
play. The tone waits for that second, and always follows "…seconds" by the same
short beat. Any extra wait comes before the numbers instead. Each cycle writes
one line to the add-on log with the second it named and how far from it the tone
started, e.g. `[clock] 16:15:52 tone +2 ms`. Until v0.106.2 the tone followed a
readout of the time the readout **began**, so it sounded 4.5–8.6 seconds late,
by a different amount at each time of day.

---
