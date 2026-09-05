# Changelog

## 0.69.3

Multi-turn context was listed as a future item. It is not one — Home Assistant's
built-in agent cannot do it, so the note has been corrected to say so.

v0.69.0 shipped with "no conversation_id is threaded, so HA cannot ask a
follow-up question; multi-turn context is a deliberate v2 item". That framing was
wrong in a way worth fixing, because it implied the capability was one release
away. Measured against this Home Assistant: `continue_conversation` returns
**false** for every ambiguous utterance tried — "what is the temperature", "turn
on the lights", "is the light on", "what is the humidity" — and a second turn
sent WITH the previous turn's `conversation_id` is matched as a fresh
independent sentence rather than as a continuation.

The built-in agent is a one-shot sentence matcher, not a dialogue manager. It
never asks "which light?"; an ambiguous command simply fails. Threading a
conversation id would therefore change nothing, and the re-prompt-and-retry shape
the phone flow already uses is the only shape this agent supports.

Documentation only; no behaviour change.

## 0.69.2

Documentation for the voice assistant, corrected against how the system actually
behaves.

**Who can reach it.** The previous text left this to inference. Dial `47` is
reachable by anyone who can pick up a house phone — and *not* by an outside
caller, for two independent reasons that are worth stating rather than assuming:
the inbound trunk `Dial` uses `r` only and never `t`/`T`, so no in-call DTMF
transfer is armed for the outside party; and transfers are confined by
`__TRANSFER_CONTEXT` to `[internal-xfer]`, which contains only room extensions
and `0` — no feature codes at all.

**What the exposure list really is.** It is the security boundary, so the section
now says plainly that a garage door or lock does not belong on it, and that an
entity which silences an alarm deserves the same treatment even though it
actuates nothing.

**Duplicate names break commands.** Documented the `switch_as_x` trap: Home
Assistant generates a `light.*` wrapper from a `switch.*`, so one physical device
gets two entity IDs with the identical spoken name. Expose the light, not the
switch.

**Phrasings.** The built-in agent is a fixed-sentence matcher, not a language
model, and the difference shows: "is the front door motion on" works where "is
there motion at the front door" does not. Added a table of forms that work.

**Temperature needs a room.** A bare "what is the temperature" cannot resolve
when more than one thermostat is exposed — a smart speaker infers the room it
sits in, a phone call carries none. This also requires the thermostats to have an
area assigned at all, which is easy to miss.

## 0.69.1

The voice assistant answered nothing. Fixed.

`ha_client.converse()` was written against the raw REST convention
(`http://<ha>:8123/api/services/...`), but `_request()` prepends a base that
already ends in `/api` (`http://supervisor/core/api`). The path therefore
resolved to `.../core/api/api/services/conversation/process` and returned **404
on every request from inside the add-on container** — the only place it runs.
Every other call in the module already used the right convention; this one call
was the outlier.

It passed every check beforehand because both the unit test and a manual `curl`
from a laptop use the *other* convention, where the `/api` prefix is correct. The
add-on booted clean, the dialplan was right, the AGI was executable, piper spoke
and whisper listened — and the assistant still could not have answered a single
question.

The replacement test asserts a module-wide invariant rather than one call's
string: it reads every `_request()` path out of the source and requires that none
begins with `/api`. A per-call assertion cannot catch this class of bug, because
it only repeats whichever string the author chose. Mutation-verified against the
original defect and against the same mistake injected into two other call sites.

## 0.69.0

A phone can now talk to Home Assistant's voice assistant, with nothing on the
path leaving the machine. **Off by default** — set `assistant_enabled: true`.

Dial `47` (configurable via `assistant_ext`), say a command or a question in
plain language, and hear the answer. It re-asks "anything else?" after each
reply and hangs up on "goodbye", "cancel", "never mind", "that's all" or
"thanks".

The local part is the point. This add-on already ships a speech recognizer and
a voice, so the flow reuses both: its own whisper transcribes the utterance,
the *text* goes to Home Assistant's built-in `conversation.home_assistant`
agent, and its own piper speaks the reply. No cloud speech-to-text, no cloud
text-to-speech, no cloud conversation agent — an internet outage does not
affect it. This is deliberately not a Wyoming voice satellite: streaming raw
audio to Home Assistant would put a second recognizer and a second voice in a
path that already has both, and would hold an audio stream open per call.

Ending the call is matched against the whole utterance rather than by substring.
"stop the music" and "cancel my seven a.m. wake-up" both contain a terminator
word and are treated as commands; a goodbye is recognized only when every word
is a terminator or filler around one.

The loop is bounded at five recordings and gives up after two consecutive
silent turns, so an off-hook handset next to a television cannot hold a channel
and a recognizer slot open.

Speech recognition is biased toward the live area and light names, which
improves short commands; the bias is a decoding hint, not a restriction, and it
degrades to a plain command vocabulary when Home Assistant is unreachable. An
unreachable Home Assistant produces a spoken apology rather than dead air.

Unlike the other feature codes, a dial-code collision disables this one
outright. Nothing else routes into the context, so emitting it without a dial
code would leave unreachable dialplan that still reads as a working feature.

Note that the conversation agent can only act on entities exposed to Assist
(*Settings → Voice assistants → Expose*). Until something is exposed, every
command is answered "Sorry, I am not aware of any device called …". That, and
the fact that exposing an entity makes it reachable from every phone in the
house, is why the option ships off.

## 0.68.0

A call's recorded kind is no longer just the context it hung up in.

`[rooms]` hosts five distinct call types — room-to-room dialling, outbound PSTN
via the dial prefix, the operator, the talking clock and paging — and all five
hang up in `[rooms]`, so all five were filed as `tag=rooms`. An audit found that
**none of the four records tagged "rooms" was actually a room-to-room call**:
they were two outbound PSTN calls, an operator session and a paging leg. Any
per-type quality trend drawn from that field was wrong.

A context may now stamp `SW_TAG` to declare what kind of call it really is. The
hangup extension resolves it with the context name as the fallback, so a context
that stamps nothing behaves exactly as before, and the value is filtered to plain
letters and a dash because it reaches a shell argument.

Room-to-room dialling stamps `room`, before the `Dial`, so a hangup part-way
through still carries it.


## 0.67.0

A wake-up that rings out no longer vanishes.

The QoS ledger is written from the dialplan's hangup extension, so it can only
ever describe a leg that **answered**. The 06:18 wake-up on 2026-09-03 rang
extension 19 and produced no record of any kind — `ring queued=True`,
`Called 19`, `is ringing`, and then nothing at all for 98 minutes. Nothing
distinguished *"the phone never rang"* from *"the user ignored it"*, on an alarm
clock.

Every wake-up attempt now writes to `/share/switchboard/delivery-outcomes.jsonl`,
the same file and shape the announce path already used — the alarm clock and
announcements fail in the same ways and should be readable together:

| outcome | meaning |
|---|---|
| `ring-queued` | originated, with the scheduled time and ring window |
| `deferred` | the room was not idle at its appointed minute, with the state |
| `originate-refused` | AMI accepted the connection but refused the Originate |
| `originate-error` | the Originate raised |

A `ring-queued` record with no matching call record **is** the no-answer signal,
and both now live where they can be read together. A deferral was previously
only a line in a container log that carries no timestamps.

The writer moved to a shared `delivery` module so the scheduler and the web UI
cannot drift apart. Telemetry is best-effort throughout: an alarm clock must
never fail to ring because its record could not be written.


## 0.66.0

An alert that lapses is no longer reported as a recovery.

`classify_cordless` flags a poor call only while it sits inside `mos_window`, so
the level returns to `ok` on its own once the bad call is old enough — with
nothing new observed. That was being announced as **"Cordless recovered — back to
normal."** Live on 2026-09-01, a `cordless degraded: last call quality poor` was
followed immediately by `cordless recovered: recovered`, with no call in between.

A recovery notification is an affirmative claim about the present, and an
operator who sees one reasonably believes something was re-checked. Nothing was.
This is the frozen-sensor pattern wearing different clothes: silence would have
been more honest, and saying which of the two actually happened is better still.

`health_transition()` now takes `fresh_evidence` and returns `stale-clear` when
an alert lapses without new measurement. The cordless poller identifies the call
its MOS verdict rests on by that call's **end time**, so it can tell "a newer
call was measured" from "the same old call finally aged out". The notification
says so plainly and tells the reader to treat the state as unknown until the next
call.

The default stays `recovered`, so any caller that cannot judge freshness behaves
exactly as before.


## 0.65.0

The heartbeat now reports what the poller slept through.

On 2026-09-01 all eight wired extensions went Unreachable from 01:00:07 to
01:02:06 MST. The heartbeat records at 07:58:47Z and 08:03:47Z bracket that
outage and **both read `reachable: 9`** — the surface built to catch exactly this
published an unbroken all-clear across it. A 300-second point-sample has a duty
cycle near 0.3%: proving the poller is alive says nothing about the 299 seconds
it was not looking. **Poller liveness is not fleet coverage.**

Asterisk wrote every transition down as it happened, so the poller does not need
to have been awake — it only needs to read what it slept through. Each cycle now
advances a byte watermark over the forensic log and reports any endpoint
reachability transition it finds:

    "transitions": [{"ext": "13", "state": "Unreachable"}, ...],
    "went_unreachable": ["13"]

A quiet cycle omits both keys entirely, so their presence is itself the signal.
The watermark starts at the log's current size, so a fresh process does not
replay history as though it just happened, and a trimmed log (the 32 MB cap)
resets rather than going backwards forever.

This costs nothing: no AMI event subscription, no change to any poll interval,
and a missing or unreadable log yields an empty list rather than breaking the
poll that produces it.


## 0.64.0

The `/share` call mirror now carries the whole record.

It shipped as a curated 11-field subset of the ledger's 32 — "enough to confirm
it ran and see the verdict." That turned out to be the wrong shape for the job.
`/data` cannot be read from outside the container, so this file is the **only**
view any audit gets, and on a surface like that absence reads as loss: an audit
correctly observed 11 fields and reasonably concluded the other 21 were being
discarded before the record was written. They were not — they were in the ledger
it could not see.

A partial mirror of a durable record is not a smaller truth, it is a different
claim. At ~20 calls a day the full record costs a few hundred bytes each against
a 4 MB cap, so there is no reason to editorialise. `loss_rx_pct`, `jitter_rx_ms`,
`rtt_max_ms`, `rtt_stdev_ms`, `mes_rx`/`mes_tx`, `hcause`, `chan`, `reasons` and
the rest are now visible where they can actually be read.


## 0.63.2

The unsampled-RTT null now covers the whole family.

v0.60.0 published `null` instead of `0.0` when no RTCP round completed — but only
for `rtt_ms` and rx jitter. A live record proved the gap:

    {"rtt_ms": null, "rtt_mean_ms": 0.0, "rtt_samples": "none", "dur": 0}

`rtt_mean_ms` still read `0.0` on the same leg, so a consumer averaging the mean
saw a flawless 0 ms call. Half a fix is arguably worse than none here, because the
one nulled field implies the others were checked.

`rtt_mean_ms`, `rtt_min_ms`, `rtt_max_ms` and `rtt_stdev_ms` are now nulled with
`rtt_ms` whenever sampling resolves to `none`.

The test fixture was the reason this shipped incomplete: it omitted `normdevrtt`
and `minrtt` entirely, so they were `None` for the wrong reason and two mutants
survived undetected. It now passes the literal `"0.000000"` those fields actually
arrive as, matching the live record.


## 0.63.1

The backup hook now flushes the `/share` mirrors too.

`/share/switchboard` holds everything this add-on writes that can actually be
read from outside the container — the forensic Asterisk log, the heartbeat, the
callqos outcomes, the delivery outcomes. The Supervisor archives that folder at a
**different point** in the backup than the app image, so until now they were the
only Switchboard files copied with no flush barrier at all.

They are mirrors rather than sources of truth — the authoritative ledger lives
under `/data/state` and was already quiesced — but they are also the copies an
audit actually reads, and flushing them costs a handful of fsyncs on files
appended a few times an hour.


## 0.63.0

The wake-up scene fires on answer, before anything is spoken.

The delivery AGI ran only *after* the greeting and the time announcement, so
hanging up during either meant the configured scene never fired at all — and
hanging up is exactly what someone does once a wake-up call has already woken
them. Live evidence: **the scene fired on 1 of 3 delivered wake-ups**, with two
legs dying at dialplan steps 5 and 6.

The audio is dismissible. The scene is a real-world effect, arguably the part
that does the waking, and gating the least dismissible action behind the most
dismissible one had it backwards.

The AGI now takes a mode: `scene` fires the scene and returns, `speak` does the
weather and calendar only. `[wakeup-deliver]` calls it twice — the scene right
after `Answer()`, the spoken extras after the time, where they belong in the
narration. A bare call with no argument still does both, so an older dialplan
that has not been regenerated does not silently lose its scene.

`SW_STAGE=scene` marks the new step, so a hangup during it is attributable.


## 0.62.0

Announcements are capped, deduplicated, and honest about what they measure.

**A runaway announcement is refused, not truncated.** Four live announcements ran
66–72 s each on the cordless, roughly every 15 minutes. An announcement that long
holds the handset off-hook, so an inbound call arriving during one lands as call
waiting instead of a normal ring. Anything over 90 s is now refused with a named
outcome rather than played — and refused rather than clipped, because silently
truncating an announcement is the failure mode the audit could not rule out, and
audio that stops mid-sentence with no record is worse than a refusal that says so.

An unmeasurable clip is **allowed through**. Failing closed here would silence
alerts, which is the worse error.

**Identical repeats are suppressed.** Three of those four announcements were
bit-identical — `billsec=72`, `rxcount=3626` — from three *different* files,
because every render gets a fresh uuid, so the same payload looked like three
different ones. Rendered audio is now hashed, and an identical payload to the
same extension inside 5 minutes is suppressed and recorded. The window refreshes
on every attempt, so a producer retrying every 30 s cannot walk past it.

**A note where someone would trip over it.** On announce legs `jitter_rx_ms` is
the degenerate constant `0.019875 s` in 7 of 7 observed records, across durations
from 5 s to 72 s — pinned a hair under the 20 ms ulaw packet interval, i.e. the
estimator is not converging rather than measuring. A receive-jitter threshold
would read permanently green while measuring nothing, which is worse than no
check at all. `jitter_tx_ms` does vary and is the usable direction. Nothing
thresholds on jitter today; the note exists so that stays a decision rather than
an accident.


## 0.61.0

A delivery that never became a call now leaves a record.

The QoS ledger is written from the dialplan's hangup extension, so it can only
ever describe legs that **answered**. Everything upstream was invisible: an
Originate refused for want of a contact, a handset that rang and was never picked
up, an AMI error. One audit window held three refused Originates and a wake-up
that rang 60 s unanswered, and not one produced a ledger row, a sensor, or
anything an operator would read — nothing distinguished *"the phone never rang"*
from *"the user ignored it."*

That matters most for the alarm clock. A wake-up that silently fails is the
defect most likely to be noticed at 7 a.m., by which point the record that would
explain it does not exist.

**Announcements now pre-flight the contact.** An Originate to an endpoint with no
contact cannot create a channel — Asterisk logs `Could not create dialog to
invalid URI` and stops — and with no channel there is no dialplan, so no
`SW_STAGE`, no `rtpqos` and no ledger row. The handler now checks the device
state first, skips the doomed Originate, and records the outcome. `device_unreachable()`
fails **open** on an unreadable state, the same direction as the busy guard: a
failed state read must never silence an alarm.

Outcomes land in `/share/switchboard/delivery-outcomes.jsonl` alongside the other
readable records, distinguishing `unreachable`, `skipped-busy`,
`originate-error` and `originate-refused`.


## 0.60.1

Heartbeat field names corrected.

v0.60.0's heartbeat read `summ.get("expected")` and `summ.get("worst_rtt")`.
`summarize()` produces neither — the keys are `total` and `worst_rtt_ms` — so
both fields wrote `null` on every cycle. The live file exposed it within one
poll of deploying.

The unit test could not have caught it: it passed a **hand-made** summary dict
using the same invented keys, so it confirmed the mistake rather than finding it.
The test now builds its input with the real `summarize()`, and reintroducing
either wrong key fails the suite.

The record also gains `worst_ext`, so a reader can see *which* phone is the
slowest without cross-referencing another surface.


## 0.60.0

Silence is now distinguishable from death, and the call record says what it
measured rather than implying it.

**A heartbeat for the forensic log.** The `/share` Asterisk log went 19 h 42 m
with no entry while the PBX was perfectly healthy: steady-state contact refreshes
do not log, only initial registration does, so a quiet system and a wedged
Asterisk produce byte-identical output — none. rtpmon now appends a dated record
to `/share/switchboard/heartbeat.jsonl` **every cycle**, including cycles where
AMI was unreachable and nothing else published. Silence in *that* file means the
poller stopped.

The same record carries the trunk's registration state, which previously had **no
timestamped history on any surface** — it lived only in a pushed HA sensor with
no history, which freezes at its last value if the add-on dies. The trunk is the
outside line and the open E911 path.

An AMI-down cycle reports `reachable: null`, never `0`: no phones measured is not
the same as no phones up.

**The call record now shows the distribution, not one draw.** `--rtt` is the LAST
RTCP round. One live announce reported `rtt=0.007675` against
`maxrtt=0.146652` — a **19× spread** — so a threshold alarm on "the call's RTT"
was reading a single arbitrary sample while `rtt_samples` simultaneously
certified the leg as well-sampled. Records now carry `rtt_mean_ms` and
`rtt_min_ms` from Asterisk's own running mean and floor.

**Zero is no longer published as a measurement.** When no RTCP round completes,
Asterisk reports rtt, rxjitter, rxmes and txmes as `0.000000` *together* — four
fields exactly zero at once, a combination no real call produces. `_credible()`
already nulled the MES pair, but a 0 ms RTT and 0 ms jitter read as a flawless
call. Those now publish `null`.

**callqos leaves proof it ran.** The dialplan launches it as
`TrySystem(... --detach ... &)`; the trailing `&` makes the shell exit 0 at once,
so the dialplan proves only that the process *started* — never that it parsed its
arguments, scored the call, or wrote anything. An audit found 13 of 13 log hits
were invocations and **zero** were output from it. It now mirrors a compact
outcome to `/share/switchboard/callqos-outcomes.jsonl`.

**A refuted baseline, corrected.** The "~4.5 minute GXW re-registration window"
was folklore. Measured off the forensic log: `Asterisk Ready.` 20:31:27 → 8/8
Reachable 20:32:17 = **50 seconds**, with a second boot bounded at ≤67 s. The
grace period deliberately stays at 360 s — the measurement is n=2 and too short
resurrects the false-alarm storm it exists to stop — but it is now a documented
5.4× margin rather than a guess.

Also: the backup pre-hook self-checks that both hook paths exist and are
executable, because a hook the Supervisor cannot exec fails at backup time, not
build time, and fails silently.


## 0.59.0

The call record can now be trusted, and the Asterisk log can finally be read.

**A readable copy of the Asterisk log.** `/data/state/asterisk.log` is the durable
forensic log and belongs where it is — `/data` survives even if `/share` is
unmounted. But it could not be **read**: the container shell is blocked by
protection mode, backups are encrypted, and the add-on API returns 403 on every
path. A log written specifically for post-incident forensics was unreachable at
exactly the moment it was wanted.

A second channel now writes `/share/switchboard/asterisk.log`, readable from the
host side. It also carries `verbose`, which the `/data` copy omits on purpose:
NOTICE/WARNING/ERROR already reach the container log with timestamps, but
`ast_verbose` output — the `-- Executing [...]` dialplan trace, i.e. what a call
actually *did* — does not. **0 of 3438 container-log lines** in one audit window
carried a timestamp at all, so no add-on event could be placed in time or
correlated with the host. A file channel stamps every line.

Verbose is affordable here only because v0.56.0 turned off `displayconnects`, which
was 89.4% of the log. It is capped at 32 MB and trimmed at start regardless.

**Statistically empty measurements are now labelled.** `rtt=0.005538` with
`stdev=0.000000` and `maxrtt` equal to `rtt` is a *one-RTCP-round* reading — it
reached the ledger beside an ordinary `rxmes=82.5`, indistinguishable from a
well-sampled leg by any field the record carried, and every mean and percentile
was averaging it in at full weight. Records now carry `rtt_samples`:
`multi` / `single` / `none`, derived from fields already in hand. Asterisk exposes
no RTCP report count, and inventing a `CHANNEL(rtcp,...)` name that does not exist
is the v0.48.0 `rxoctetcount` mistake — four WARNINGs per call and a field null in
100% of records.

**Scripted legs record how far they got.** A wake-up delivery cut off before the
greeting was indistinguishable from one that ran to completion: the ledger held
only "ring queued=True" and "answered", while one snoozed wake-up produced three
attempts dying at dialplan steps 6, 5 and 9. `SW_STAGE` is now set at each
milestone and reported from the hangup extension, so a record carries the last
stage reached (`answering` → `greeting` → `time` → `extras` → `repeat` →
`complete`). Announce playback marks `playing` → `complete`.

**One audit finding corrected, not fixed.** It was reported that every wake-up
delivery is "filed under a nonexistent extension 0". It is not: `ext` comes from
the channel name and only falls back to `cid` for a non-numeric endpoint (an
inbound trunk leg, where the cid genuinely is the PSTN caller). `cid=0` appears
only in the human-readable Verbose line. A test now pins both halves of that
behaviour so it cannot be misread again.


## 0.58.1

The backup hooks now leave evidence that they ran.

A backup hook executes via `docker exec`, and an exec's stdout does **not** reach
the container's main log stream — so a hook that only prints is unverifiable from
outside, and a hook that silently never runs looks identical to one that ran
perfectly. Verifying 0.58.0 hit exactly that wall: the backup completed, the
hooks produced no log line, and neither outcome could be distinguished.

Both hooks now append a record to `/share/switchboard/backup-window.jsonl`,
which is readable from the host side unlike `/data`:

    {"phase": "pre",  "ts": "...", "synced_files": 12}
    {"phase": "post", "ts": "..."}

That also closes the gap the post-hook existed for. The Supervisor log shows the
image export starting, but nothing recorded when *this add-on's* state stopped
being copied — so a torn ledger line could never be attributed to a backup
window. Now both edges are on record.

Still unable to fail a backup: an unwritable stamp path is logged and swallowed,
and that path is covered by a test.


## 0.58.0

Backups now flush this add-on's durable state before the Supervisor copies it.

Every backup of Switchboard runs **hot**. An audit of 3975 Supervisor lines
found 18 `Stopping app_` entries and not one of them was Switchboard, and 13 of
16 add-on updates in the same window took no pre-update backup at all. So the
call-quality ledger and the durable Asterisk log at `/data/state/` were being
copied mid-append, with no flush and no fsync barrier.

Taking the add-on **cold** for a backup was rejected deliberately: it would drop
dial tone — including the 911 path — for the ~15 s the image export takes, every
night. A dropped phone line is a worse failure than a torn trailing log line, and
every reader already tolerates the latter (`load_callqos_ts()` and the ledger
readers skip malformed lines by design).

So `backup_pre` does the cheap, safe half instead: fsync every file in the state
directory, then fsync the directory itself so an atomic-replace writer's new name
is durable too, not just its data blocks. `backup_post` marks the closing edge,
because an audit could previously see the image export begin but had no record of
when this add-on's state stopped being copied — which made it impossible to tell
whether a torn ledger line fell inside a backup window.

Neither hook can fail a backup. Every error path is logged and swallowed: a
backup that runs is worth far more than one blocked by a flush that had nothing
to do.


## 0.57.0

Playback legs no longer vouch for themselves, announcements are measurable, and
the PBX stops waiting on Home Assistant to boot.

**A playback leg can no longer confirm a call.** `last_call_mos()` only trusts a
handset-side RTP record if a call-ledger leg lands within 90 s of it. That gate
exists for one reason, stated in its own docstring: HA announce playback leaves
handset records scoring 2.2–2.9, and three false `degraded` episodes fired on
2026-08-05/06 because of them. It worked because playback legs were *absent*
from the ledger — not because it excluded them.

v0.55.0 then added the hangup hook to `wakeup-deliver`, `page` and `announce`.
That was right on its own terms, but it wrote those legs into the ledger, so
they began confirming themselves and quietly restored the bug the gate was
written to close. Two audits saw the result — repeated `cordless degraded: last
call quality poor (MOS 2.2)` against RTCP showing **zero** packet loss, ~20 ms
jitter and Asterisk's own MES ≈ 87/100 — and could not explain it, because the
tempting `rxmes/40` arithmetic is refuted by other calls in the same window. The
2.2 was never Asterisk's number at all: it is the handset's own score for a
playback leg. `load_callqos_ts()` now filters on tag, so the gate no longer
depends on an accident of ledger coverage.

**Announcements are measurable.** An announcement was Originated straight to
`Application: Playback`, which never enters the dialplan — so it had no `h`
extension and `switchboard-rtpqos` was unreachable *by construction*. Two audit
windows found 23 and then 18 announce playbacks and not one quality record: an
announcement that was clipped, dropped mid-play or never delivered looked
exactly like one that played in full. That is the failure this handset is most
prone to, because its WiFi radio parks between calls.

Announcements now originate into a thin `[switchboard-announce-play]` context
that plays the clip and hangs up, so the existing hook yields a hangup cause, a
billsec and a QoS record. The tag stays `announce` — deliberately, so it remains
in `PLAYBACK_TAGS` and is recorded without notifying, without moving
`sensor.switchboard_last_call`, and without confirming a call. The clip path now
travels as a dialplan variable, so it is charset-validated (`$`, `{`, `}`, `,`
and whitespace barred) rather than only screened for CR/LF.

**`startup: services`.** The Supervisor default, `application`, held the whole
PBX until Home Assistant Core reported RUNNING — 66.4 s behind the first
services-phase add-on on the 2026-08-25 boot, and unbounded if Core stalls on a
recorder migration over a dirty database. Asterisk, the gateway and the trunk
have no dependency on Core, and dial tone — including the 911 path — should not
queue behind a home-automation server. Safe because the HA-facing pollers
degrade rather than exit when Core is absent: `set_state()` returns `False`
instead of raising, and each poller retries next cycle.

Every change is covered by tests verified against mutants.


## 0.56.0

Health sensors now say how old they are, and the add-on log is readable again.

**A pushed Home Assistant sensor never expires.** During the whole-host power
cut of 2026-08-25 the PBX did not exist for 60 minutes 39 seconds — no
extension could dial, the trunk was gone, the gateway was gone. Home Assistant
history over that hour shows `sensor.switchboard_trunk_health` with exactly two
rows, both `Registered`, and `sensor.switchboard_cordless_health` with three,
all `ok`. No `unavailable`, no `unknown`, no gap marker. Anything reading those
sensors during the outage — a person, a dashboard, an automation — got positive
confirmation that a dead phone system was healthy.

`rollup_is_stale()` already covered one poller dying while another survived to
notice and publish `unknown`. Nothing inside the add-on can cover the add-on
itself being gone: no code is left running to say anything. That is detectable
only from outside, by comparing a timestamp against now — so every sensor now
carries `measured_at` and `poll_interval_s`, not just `link_health`:

- `sensor.switchboard_trunk_health`
- `sensor.switchboard_cordless_health`
- `sensor.switchboard_gateway_health`, including its explicit `unknown` path —
  an unstamped `unknown` freezes exactly like an unstamped `ok` once nothing is
  publishing.

To act on it, alert when `now - measured_at` exceeds a small multiple of
`poll_interval_s`. Until something does, a frozen sensor still reads green; the
stamp makes the freeze *detectable*, it does not by itself make it *alarm*.

**AMI connect/disconnect logging is off.** Asterisk defaults
`displayconnects=yes`, which writes a `Manager 'switchboard' logged on/off` pair
for every AMI session. The pollers open one per cycle, so those pairs were
**82.4%** of the add-on log in one audit window and **89.4%** (3076 of 3438
lines) in the next — four of every five lines carrying no information. Two
separate audits each turned on single lines buried in that churn. AMI is
loopback-only with a random per-boot secret and every session is our own
poller, so the connect record has no forensic value here; genuine auth failures
log separately and are unaffected.

Both changes are covered by tests that were verified against mutants — removing
any of the five stamp sites, or the `displayconnects` line, fails the suite.

## 0.55.0

The call-quality ledger now covers every context that carries audio.

- Only four contexts emitted the hangup extension that feeds
  `switchboard-callqos`, so wake-ups, wake-up delivery, paging, announcements
  and the voice menus produced **no record at all**. In one observed window ten
  legs ran and exactly one was logged — the ledger was under-reporting activity
  roughly fivefold, which is why a 3 a.m. wake-up session traced in the journal
  was invisible to it. All six now report.

**Recorded is not the same as alerted.** The three legs the PBX originates to
play something *at* a phone — wake-up delivery, paging, announcements — are
scored and stored honestly, but they never raise a notification and never move
`sensor.switchboard_last_call`:

- Nobody is on the line to act on a real-time popup about a three-second chime.
- They are one-directional **by design** — the PBX talks, the handset mostly
  listens — so the one-way-audio detector, which exists to catch a *broken
  conversation*, would fire on their perfectly normal shape.

Conversations (rooms, operator, directory, outside calls) and the interactive
voice menus (wake-up, lights, status) alert exactly as before: bad audio in a
menu wrecks speech recognition, and the caller feels that immediately.

One path remains unmeasurable and is now documented as such: an announcement
pushed from Home Assistant via `/api/announce` is originated straight into
`Playback` with no dialplan context, so it has no hangup extension to report
from.

## 0.54.0

The 06:00 wake-up on 2026-08-21 played its greeting and the time, then said
nothing about the weather — and the logs recorded nothing about why.

**Why it went quiet.** `api.weather.gov` stalled, the fetch hit its 6-second
timeout, and `weather_report()` returned its human fallback sentence
("Weather is unavailable right now."). The wake-up AGI decided whether to speak
by testing `"unavailable" not in w.lower()` — a control decision hanging on the
exact wording of a spoken sentence — so it dropped the readout **silently**.
Three fixes, one per layer:

- **The wake-up now makes one network request instead of two.** The
  `/points` → forecast-URL lookup is static per location and was cached in a
  module-level dict — useless for the only caller that matters, since the
  wake-up runs as a *fresh AGI process* every morning and always started cold.
  It is now cached on disk, halving both the latency and the exposure to
  exactly the timeout that caused this.
- **The forecast is retried.** NWS is a free public service that intermittently
  stalls, and a wake-up gets one attempt per morning.
- **The decision is truth-valued, not prose.** A new `weather_line()` returns
  `''` when the weather cannot be determined; `weather_report()` keeps the
  spoken fallback for the dial-45 menu. Rewording that sentence can no longer
  change what a sleeping person hears.

**And it will never be silent about being silent again.** Every branch of the
delivery AGI now logs — scene fired or not configured, weather spoken, skipped
for want of a forecast, or skipped because text-to-speech failed (that last
return value was being discarded entirely). The failure that prompted this was
diagnosable only by inference from a 7-second gap in the call log.

## 0.53.1

`worst_rtt_is_partial` shipped in v0.52.0 as dead information, and took the
rollup's icon with it.

- The flag keyed on `unreachable > 0`, which conflates **"dropped out"** with
  **"never there"**. Extension 20 is a configured softphone that never
  registers by design, so it is permanently unreachable — the flag was
  therefore `True` in **100%** of live samples and carried no information at
  all. Worse, it was verified as "working" precisely *because* it read `True`
  with `unreachable_exts: ['20']`, which is the symptom, not the proof.
- The same expression drove the rollup's icon, so
  `sensor.switchboard_link_health` has been showing **`mdi:lan-disconnect` on a
  perfectly healthy fleet** — nine of nine phones up, permanently "disconnected"
  on the dashboard. That one predates the flag.
- Both now key on whether a phone **this process has actually measured** is
  missing from the sample. A phone that has never answered a qualify cannot
  have dropped out, so the softphone stops distorting anything; the cordless,
  which does answer, still raises the flag the moment it disappears — which is
  the case that matters, since losing the slowest phone makes `worst_rtt_ms`
  *improve*.
- The helper degrades deliberately rather than going quiet: given no history it
  falls back to "a registered phone stopped answering". That is strictly
  weaker — a phone that de-registers entirely is indistinguishable from one
  that never registered — and the limit is pinned by test so nobody "fixes" the
  fallback back into flagging the by-design-absent softphone.

## 0.53.0

Three fixes from the 2026-08-22 audit — the first found by watching a real
person fail to use the system.

**"Cancel" now works at the wake-up prompt.**

- The wake-up flow tells the caller "say a new time, **or say cancel**", and the
  parser accepts cancel/clear/remove/delete/never mind/stop/forget — but the
  recognizer was primed with time vocabulary only, so it had no prior for those
  words. On 2026-08-21 at 05:58 someone tried to call off a 06:00 wake-up and
  the transcripts came back **"Campbell."** then **"Campful."** Neither matched,
  both attempts were spent, and the wake-up they were cancelling stayed set.
  The prior now names the word the caller is invited to say. This is the same
  failure the lights menu already documents for "list" ("Left"/"Lift"); the
  lesson simply had not been carried across. A test now pins the contract that
  every word the caller is *told* to say appears in the prior.

**The link-health ledger is readable again.**

- v0.52.0's atomic rewrite copied `switchboard-callqos`'s temp-file idiom but
  dropped the `chmod` that follows it. `mkstemp` creates `0600` owned by the
  writer, and `os.replace` keeps the *temp* file's mode rather than the
  destination's — so `linkhealth.jsonl` silently became root-only `0600` where
  it had been `0664`. Nothing in the shipped image reads it, so nothing broke,
  but `/data/state` is the asterisk-owned directory precisely so non-root
  components can. Restored, with a test that asserts the mode.

**The sensor you are told to alert on describes its own freshness.**

- v0.51.0 stamped `measured_at`/`poll_interval_s` onto the rollup only, while
  the docs designate `sensor.switchboard_wired_link_health` as the one to graph
  and alert on. A pushed sensor never expires, so an alert built on it could not
  tell a current reading from a frozen one. Both sensors now carry the stamp.

## 0.52.1

- `worst_rtt_is_partial` shipped **dead** in v0.52.0. It was computed,
  unit-tested and mutation-verified in `summarize()` — and never added to the
  attribute payload `_publish()` actually sends, so Home Assistant showed
  nothing. Caught by live verification minutes after the deploy: the sensor
  reported `None` while a phone was demonstrably unreachable. Now wired to the
  publisher, with a test that asserts the **published** attribute rather than
  the computed one, because those are two different things and only the second
  is visible to anyone.

## 0.52.0

Four defects the 2026-08-19 log audit confirmed against 2¼ days of v0.51.0 in
production — including one in v0.51.0 itself.

**A monitor that goes blind now says so.**

- v0.51.0 taught the device-health monitor to reject a stale link-health
  rollup, but it then simply *skipped* the publish — and for a **pushed** Home
  Assistant sensor, not publishing means the last value stands. A monitor that
  had gone blind kept showing green, which is the same fail-open shape the
  staleness check was written to close. It now publishes an explicit `unknown`
  with the reason, and resets its transition state while keeping the alert
  latch so a later recovery still announces itself.

**One-way audio detection no longer trusts a number the code knows is wrong.**

- The gate required a leg to be ≥ 5 s by `billsec` — the very field this
  release corrects elsewhere, because a CDR reset can report 2 s for a leg
  whose packet counters show 75 s (both shapes seen live on 2026-08-08). A
  genuinely long one-way call with a mangled `billsec` was therefore exempt
  from the check that exists to catch it. The gate now takes the max of
  `billsec` and the RTP-derived duration. The two real abandoned-call false
  alarms measured ~1.1 s and ~1.2 s by *both* measures, so they stay
  suppressed. The regression test's old fixture claimed 2,600 received packets
  on a 1-second call — physically impossible, 2,600 packets is 52 seconds of
  audio — and has been replaced with the actual packet counts from the two
  production records.

**The fleet rollup admits when its sample is partial.**

- `sensor.switchboard_link_health`'s state is a max over *reachable* phones, so
  when the slowest phone drops off entirely it leaves the sample and the number
  **improves** — the sensor's all-time minimum can be its worst moment. The
  state is deliberately unchanged so its recorder history keeps one meaning;
  a new `worst_rtt_is_partial` attribute is `true` whenever any phone is
  missing, so an automation can refuse to threshold on a partial sample.

**The link-health ledger is written atomically.**

- `linkhealth.jsonl` was truncated in place and rewritten (~1.6 MB) on every
  poll — the one durable ledger in `/data/state` that skipped the temp-file +
  `os.replace` idiom `switchboard-callqos` already used. A crash, a full disk,
  or a container stop inside that window left a half-written file. Its test now
  proves the property rather than the happy path: it forces a failure at the
  commit point and asserts the existing ledger survives intact.

**Documentation caught up with the hardware.**

- The "~250 ms cordless idle under Wi-Fi power save" premise — the stated
  justification for splitting wired link health out of the rollup — is stale by
  roughly 30×: the handset now lives on its charger and idles near 9 ms. The
  gap narrowed; the masking did not, so the split stands and the reasoning now
  says why honestly.

All four code changes are mutation-verified.

## 0.51.0

Clears the remaining backlog from the log review and the documentation-parity
audit — the items that were real but not urgent enough to hold up v0.49/v0.50.

**Monitors can no longer look healthy while blind.**

- A pushed Home Assistant sensor never expires, so if the link-health poller
  died while Home Assistant stayed up, its rollup froze at the last good
  reading and everything downstream kept reporting it as current. The rollup
  now carries `measured_at` and `poll_interval_s`, and the device-health
  monitor **refuses to derive gateway health from a stale rollup** rather than
  republishing a frozen snapshot as live. (This is the same mechanism that
  turned a transient warm-up snapshot into a 4-minute false "gateway degraded"
  twice on 2026-08-11.)
- `gateway_ports` is now validated against the configured rooms at start-up.
  It never was, so the 1xx room numbering the docs themselves suggest left
  gateway health permanently "ok" and wired link health permanently "unknown"
  — two monitors reporting healthy precisely because they were watching
  nothing. A mismatch now logs exactly what is wrong.
- `device_health_alerts` was the last option still read with
  `bashio::config.true`, which treats an empty read as false. Bashio can return
  blank transiently at boot, and this value is exported once — so a single
  blank read would silence every cordless and gateway alert for the life of the
  container. It now tests `= "false"` like every other gate.

**The durable log gets denser.**

- Nine lines of per-boot noise are gone. The shipped static `modules.conf`
  noloaded six modules that decline to load anyway, but the generator
  **overwrites** that file, so the silencing was silently lost — the dead
  static copy is deleted and the generator owns the list, plus the two HEP
  capture agents for a protocol this box does not run. `res_http_media_cache`
  is deliberately left alone: its decline reports a genuine missing
  dependency, and a forensic log should keep a signal it might need.

**Alerts identify the right call.**

- A poor-call notification's id was keyed on the Asterisk channel name, which
  restarts its counter at boot — so a later bad call could silently replace an
  earlier unread alert about a different call. The id now carries the leg's
  timestamp; a repeat report of the same leg still collapses to one entry.
- `next_reg` is published as `next_reg_s` and only when it is a real countdown.
  AMI reports "0" whenever no refresh is scheduled, which read like "overdue".

**Documentation now matches the build.**

- Both consoles were described as unauthenticated; the web terminal takes a
  sign-in whenever `console_users` is set (§10, §15, and a §2 row that
  contradicted the row below it).
- `/phonebook.xml` — the cordless handset's remote directory, and the one
  unauthenticated LAN GET on the ingress port — is documented for the first
  time, with the handset settings that point at it.
- `log_level` documents its three real outcomes: four of the seven values are
  identical to each other.
- The options overlay changes **values**, not the six service enable gates
  (those read `bashio::config`, deliberately, for blank-read safety).
- Corrected: 411 does connect on a weaker-but-above-threshold match; dial-a-
  status is capped, not unbounded; dialing 0 clears that room's message-waiting
  indicator; recovery notifications *replace* rather than "clear"; `41` waits
  out the interdigit timer because it is a prefix of `411`; dialing 44 and
  paging from the dashboard are two different implementations; only a
  *confident* room match beats a feature word; `sensor.switchboard_last_call`
  skips calls whose MES is not credible; the fingerprint command needs
  `WP826_HOST=`; and the status/automation labels no longer promise doors or
  "other entities".

Every code change is mutation-verified.

## 0.50.0

A documentation-vs-reality audit compared every promise in the README, the
reference, the security model and the Configuration-tab labels against what the
code actually does. Where they disagreed, this release fixes whichever side was
wrong — usually the code.

**Dial-a-status "power" worked on exactly one installation: this author's.**

- The power branch of the status menu was hardwired to four EcoFlow entity IDs
  from one particular home, behind a source comment claiming they were
  "overridable via features.json". Nothing implemented that override, so on any
  other install the branch silently queried entities that do not exist and read
  back an empty report. There are now four real options —
  `status_power_grid` / `_battery` / `_runway` / `_solar` — staged to the voice
  flows like every other entity setting, with **no built-in defaults**, and an
  explicit spoken "power reporting isn't set up" when none are configured.

**A supported trunk setup produced a permanent false alarm.**

- `trunk.registns: false` (a trunk that authenticates per-INVITE and never
  REGISTERs) is a documented option, and the generator correctly emits no
  registration section for it. The v0.48.0 watchdog gated only on
  `trunk.enabled`, read "no registration object" as failure, published
  `unknown` forever and fired a persistent, never-clearing "outside line down"
  on a perfectly healthy trunk. It now checks `registns` too.

**"Say list at any step" now works at every step.**

- The lights menu's final prompt (turn on / turn off / cancel) had no `list`
  handler: saying it matched nothing, burned a retry, and after two tries hung
  up on a caller who had only asked to hear their options. It now re-speaks the
  choices for free, matching the two earlier steps exactly.

**Documentation corrected where the code was right.**

- The three health monitors are **not** independent, and the reference now says
  so plainly: gateway health is derived from the link-health rollup, the
  cordless IP auto-follow reads a link-health sensor, and the trunk watchdog
  runs inside that poller — so `link_health_enabled: false` silently disables
  all of it.
- `mwi_enabled` only controls the dial-0 auto-clear; it never switched the
  message-waiting indicator off. The Configuration-tab label also promised a
  "voicemail or missed-call" indicator — this system has neither.
- `inbound_ext` fails open: a typo rings the whole house rather than nothing.
- SECURITY.md no longer claims every accepted LAN-local risk logs a start-up
  warning (two of four do), and now names `.github/codeql-baseline.json`, the
  exemption file the CI gate consults before failing a build.
- The dashboard's Lights panel is documented in both the README and the
  reference for the first time.

Every code change is mutation-verified, including the tempting wrong fixes.

## 0.49.0

Fixes four defects that the v0.48.0 durable log and three days of production
telemetry exposed — including one in v0.48.0 itself.

**The new log caught a bug in the release that shipped it.**

- v0.48.0 asked Asterisk for `CHANNEL(rtcp,rxoctetcount)` / `txoctetcount` to
  fill the ledger's `rx_octets` / `tx_octets`. PJSIP in Asterisk 20.11.1 does
  not expose those names: both fields stayed null in **100%** of records and
  every call leg wrote four `Unrecognized argument 'rxoctetcount' for 'rtcp'`
  warnings into the brand-new durable log. The flags are dropped — byte volume
  is derivable from the packet counts, which do populate. The old test passed
  the entire time because asserting that a *string was rendered* proves only
  that we asked, never that Asterisk answered; the replacement test asserts we
  never ask for the rejected names again.

**The gateway no longer reads "degraded" after a restart.**

- The link-health poller left its startup warm-up as soon as the reachable
  count stopped growing. On 2026-08-11 that count sat flat at 6 across two
  consecutive 15 s polls while three FXS ports were still re-registering — a
  false plateau. The poller settled early, froze that stale down-list for a
  full 300 s interval, and the device-health monitor (which reads the rollup)
  reported **"GXW gateway degraded" for ~4 minutes, twice in one day**.
  Warm-up now ends on the precise condition — every *wired* gateway port
  reachable — with the existing poll cap still bounding the wait. The cordless
  and the unused softphone are excluded, since neither is expected to register.

**An unreachable AMI is no longer reported as a dead outside line.**

- `get_registrations()` returned an empty dict for BOTH "AMI unreachable" and
  "no registration configured", so the trunk watchdog's documented skip guard
  was unreachable code: an AMI-down cycle blanked
  `sensor.switchboard_trunk_health` to `unknown` (seen on 6 of 6 restarts) and,
  once settled, would have counted toward the alert and fired a false "outside
  line down". A new `get_registrations_or_none()` distinguishes the two;
  the watchdog now skips the cycle for real.

**Honest margins.**

- The speech-to-text read budget's comments claimed 24 s was ~3× the worst
  observed decode. Production has since measured a **16 s** decode of a 10 s
  recording — the real margin is 1.5×, and a slow decode can no longer be ruled
  out as a cause of a post-connect failure. The budget itself is vindicated:
  that request would have failed outright under the old 8 s limit.

**Emergency calls now fail loudly instead of quietly wrong.**

- This PBX has no E911 service, which the docs have always said — but nothing
  in the dial plan matched `911`. In the default **prefix** mode the outbound
  pattern `_9.` matched it and dialed the remainder, `11`, out to the PSTN: a
  wrong call placed during an emergency. In direct-dial mode it fell through to
  an unexplained congestion tone. Both modes now match `911` (and the prefixed
  `9911`) explicitly and answer with a spoken "this line cannot reach emergency
  services — hang up and use a mobile" before any trunk pattern can match.

**The trunk auto-re-register was inert and is now real.**

- v0.48.0's recovery kick used `Action: Command` ("pjsip send register"). The
  add-on grants its AMI account `command` for READ but deliberately withholds
  it from WRITE, and Command is authorised against WRITE — so every kick would
  have been rejected "Permission denied", silently, on the only path that
  recovers a dead outside line. It now uses the native `PJSIPRegister` action,
  authorised against `system`, which the account already holds: the recovery
  works with **no widening of AMI privilege**. A new test ties the chosen action
  to the granted classes, so neither half can drift again.

All four fixes are mutation-verified: reintroducing each defect fails its test.

## 0.48.1

- The config-directory mapping now uses the Supervisor's current `app_config`
  map type. The old `addon_config` spelling still worked but logged
  "App 'Switchboard' uses legacy map type 'addon_config'; use 'app_config'
  instead" on every load — and legacy spellings eventually stop working.
  Same directory, same permissions; the options overlay is unaffected.

## 0.48.0

Closes every defect from the August log audit — headlined by the one that
silently killed inbound calling for 24 hours.

**The outside line can no longer die silently.**

- A 75-minute WAN outage on Aug 9 exhausted Asterisk's default registration
  retry budget (`max_retries=10`), which is a **terminal** state: nothing ever
  retries, and inbound calling stayed dead for 24 hours with zero alerts
  (outbound was unaffected — it authenticates per call). Three fixes:
  - the generated `[trunk-reg]` now sets `max_retries = 10000` plus
    fatal/forbidden retry intervals — the terminal state effectively cannot
    arise;
  - the link-health poller now watches the registration, publishes a new
    **`sensor.switchboard_trunk_health`**, and auto-sends a re-register if it
    ever sees Rejected/Stopped;
  - a persistent notification fires after 2 consecutive bad cycles and clears
    itself on recovery.

**Logging actually logs now.**

- The generated `logger.conf` declared its channels under `[general]`, but
  Asterisk only reads them from `[logfiles]` — so the add-on has been running
  with **zero** configured log channels and the durable
  `/data/state/asterisk.log` never existed (console output survived only via a
  block-buffered stdout fallback). Fixed; the file appears on first boot of
  this version.

**Fewer false alarms.**

- The fleet-outage alert's consecutive-cycle gate is sized for the 300 s steady
  cadence, but startup warm-up polls every 15 s — so any restart where the
  gateway took >30 s to re-register could page a false outage. Warm-up cycles
  no longer count.
- Sub-5-second abandoned calls (caller hangs up during the operator greeting)
  no longer classify as "one-way audio" — the transmit side is legitimately
  silent during Answer→Wait(1). Two false pages, since audited, came from this.
- The cordless monitor no longer treats the handset's `moscq: 0.0`
  no-measurement sentinel as a real MOS (scale floor is 1.0), and phone-side
  MOS records now count only when they match a real call in the ledger —
  Home Assistant announce playbacks to the handset were driving false
  "degraded (last call quality poor)" episodes.

**Truer data.**

- `sensor.switchboard_cordless_health` state is now always the health level
  (`ok`/`degraded`/`critical`); it previously flipped between the battery
  number and level strings, which made a battery-driven critical invisible in
  the state. Battery % stays in `battery_pct`. **Breaking** if you graphed the
  state as battery — repoint that graph at the attribute.
- Outbound trunk calls are now attributed to the room that dialed, not the
  outbound caller-ID the dialplan stamps on them.
- Call legs mangled by CDR resets store their RTP-derived duration (a 75 s
  operator session no longer logs as 2 s); the raw value is kept in
  `billsec_raw`.
- The dialplan now passes the extended RTCP telemetry (max/stdev RTT, max rx
  jitter, octet counts) the ledger has carried null columns for since they
  were added.

**Speech that waits its turn.**

- The operator's speech-to-text read budget rises 8 s → 24 s: 16 % of voice
  requests (33 % on the worst day) were timing out mid-decode and forcing
  re-record loops.
- whisper-server now runs at `nice 10` so inference bursts can't inflate RTP
  jitter on concurrent calls.

**Test integrity.**

- 17 of 18 test files used a print-only `check()` helper, so under pytest —
  what CI runs — their checks could never fail the suite. Every `check()` now
  asserts. (Arming them surfaced zero hidden failures.) Tests 276 → 293.

## 0.47.0

Reports the wired phones' latency separately from the Wi-Fi cordless.

- New **`sensor.switchboard_wired_link_health`** — the median round-trip latency
  of the gateway's FXS ports alone (`gateway_ports`), with `max_rtt_ms` and
  `ports_measured` attributes. The same figures also appear as attributes on
  `sensor.switchboard_link_health`.
- Why: the existing rollup is a fleet **worst case**, and the cordless idles
  around 250 ms under Wi-Fi power save while its *calls* run 7–18 ms. That is
  honest as a worst case but useless as a trend, and it masked the wired fleet —
  eight ports degrading from 2 ms to 40 ms would not have moved a number pinned
  at 256 by one handset.
- Median rather than mean, so a single handset waking from power save cannot
  drag the figure.
- `sensor.switchboard_link_health` keeps its existing state and meaning, so
  history and any automations built on it are unaffected.

## 0.46.2

Three defects found by analysing the live logs. No feature changes.

- **Cordless poll cycle could abort entirely.** The handset sometimes answers
  with `rtpStatus` as a plain string; `or {}` catches `None`/`""` but not a
  non-empty string, so it reached `.values()` and raised `'str' object has no
  attribute 'values'`. That killed the whole cycle, leaving battery, Wi-Fi and
  MOS unpublished until the next one. Guarded at the call site and inside
  `last_call_mos` (which also now skips non-dict records).
- **Every add-on restart raised a false gateway alarm.** After a restart the GXW
  re-registers its ports on its own timer (~4.5 minutes measured), during which
  "all ports down" is expected — but it was reported as *"the GXW gateway likely
  lost power or its uplink"*, naming the wrong component. An all-down reading in
  the first 6 minutes is now `degraded` with an accurate reason; after that it
  is critical as before, and a partial outage is never suppressed.
- **"wrong password?" became a mis-diagnosis in v0.46.0.** An unreadable admin
  API can now equally mean a `cordless_cert_sha256` mismatch, so the reason
  names both causes.

## 0.46.1

Fixes the options overlay being silently ignored for most service settings.

- The s6 run scripts read their VALUES with `bashio::config`, which parses
  `/data/options.json` directly and therefore **bypassed the overlay**: an
  overlay setting e.g. `cordless_battery_warn_pct` merged cleanly, was logged as
  "overriding", and then changed nothing at all. Ten reads across `devhealth`,
  `rtpmon`, and `wakeup-scheduler` now go through `switchboard-opt`, which reads
  the post-overlay snapshot.
- Service enable **gates** deliberately stay on `bashio::config` — their
  empty-read semantics are load-bearing against a boot race.
- A new test scans every run script and fails on any value read that still uses
  `bashio::config`, so this cannot silently return.

## 0.46.0

Certificate pinning for the WP826 cordless.

- New `cordless_cert_sha256` option. The handset serves a self-signed
  certificate that cannot be replaced, so the health monitor could not validate
  it and connected unverified — while sending the **admin password** on every
  poll. With a fingerprint set, the monitor now compares the presented
  certificate **before** writing the login body and refuses to send credentials
  on a mismatch, so a LAN device impersonating the cordless learns nothing.
- `tools/wp826.mjs` gains the same check (`WP826_CERT_SHA256`), verified once
  before login, plus a `fingerprint` command that prints the value to pin.
- Pinning is opt-in: with the option blank, behaviour is unchanged and the
  monitor logs a one-line note at start saying the certificate is unverified.
- Fingerprints may be pasted in any usual shape — colon- or space-separated,
  upper or lower case, with or without a `sha256:` prefix.

## 0.45.0

Per-room wake-up scenes.

- New `wakeup_scenes` option: a list of `{ext, scene}` rows binding a Home
  Assistant scene to a room extension. When a wake-up call rings that room, its
  own scene fires — so a bedroom wake-up raises the bedroom, and a kitchen
  wake-up raises the kitchen, instead of one whole-house scene firing for every
  room.
- `wakeup_scene` remains the fallback for rooms without an entry, so existing
  installs keep their current behavior unchanged.
- Rows are validated against the configured rooms: an unknown extension or a
  non-`scene.` entity is logged and dropped rather than staged into a wake-up
  that would silently fail at 6 a.m.
- The delivering AGI derives its room from the channel it is running on
  (`PJSIP/<ext>-…`) and falls back to the whole-house scene when the channel is
  not a room endpoint.

## 0.44.0

Console web terminal sign-in, and an administrator options overlay.

- **Web terminal login** (`console_users`): a list of `{username, password}`
  accounts (masked in the Configuration UI). When any account is configured the
  web terminal shows a sign-in page and — the part that matters — the WebSocket
  carrying the actual console session requires the signed-in cookie too.
  Sessions are HttpOnly/SameSite=Strict with a 12-hour lifetime; failed
  attempts are throttled per source address, charged before verification;
  credential comparison is constant-shape. An empty list preserves the
  historical open behavior, and the boot log states which mode is live. The
  telnet console is unchanged — bind it to loopback where the LAN isn't fully
  trusted.
- **Options overlay** (`/config/options-overlay.json`, i.e.
  `/addon_configs/<slug>/options-overlay.json` from the host): a JSON object
  deep-merged over the saved options at every start, for administration when
  the Supervisor options API is unavailable and for scripted config. Dicts
  merge recursively, scalars/lists replace, every overridden key path is logged
  at boot, and a malformed overlay is ignored loudly. The console services read
  their values through the merged snapshot (new `switchboard-opt` helper); the
  service enable flags deliberately stay on the Supervisor options only.
- Test-harness hardening: the config test-suite's `check()` helper now asserts,
  so a failing check fails the run under pytest as well as under the script
  runner, and its script runner auto-discovers tests instead of relying on a
  hand-maintained list that silently skipped new ones.

Hardening applied after an adversarial review of the above, before release:

- The overlay is **type-checked** against the saved options. Previously a
  syntactically valid but wrong-typed value (`{"trunk": "host"}`, a float port)
  raised inside a renderer, failed the init step, and stopped every service —
  Asterisk included. Mismatches are now rejected with a warning, and any other
  overlay failure degrades to "ignore the overlay".
- `GET /static/index.html` served the terminal page with no session, bypassing
  the gate on `/`. The page is now reachable only through the gated route.
- A live console session is re-validated continuously, so `/logout` and session
  expiry now close an **already-open** terminal instead of only blocking new
  ones.
- `POST /login` is same-origin gated: without it, any page a household browser
  visited could burn this address's login attempts and lock the console out.
- The login throttle and session store are lock-protected (every request runs on
  its own thread), and the boot log now derives "gate ACTIVE" from the same
  parser the server uses — a `console_users` list of only invalid rows no longer
  reports a gate that isn't running. The server also logs its true mode.
- `switchboard-opt` falls back to `options.json` when the snapshot lacks a key,
  and the snapshot write can no longer fail the start; the dashboard, console,
  and monitors read the effective view with the same fallback, so a missing
  snapshot degrades to the saved config rather than to nothing.
- New route-level tests drive the real server over a socket (the helper-only
  suite is what let the `/static/index.html` hole through); every fix above is
  mutation-verified.

## 0.43.2

Completes the 0.43.1 sample-config cleanup. No feature changes.

- The generated `modules.conf` also noloads the stock voicemail family
  (`app_voicemail` and its IMAP/ODBC variants, `app_minivm`): voicemail is not
  part of this system — message-waiting is delivered via PJSIPNotify and no
  dialplan line calls `VoiceMail()`. Loaded, these modules only parsed the
  package's sample `voicemail.conf` and logged `Couldn't find mailbox 1234`
  plus a duplicate-application warning at every boot.

## 0.43.1

Attack-surface hardening and a documentation correction, from a full post-migration
settings-and-logs audit. No feature changes.

- The generated `modules.conf` now noloads the legacy protocol stacks that the
  distro sample configs would otherwise autoload: `chan_iax2` (udp/4569),
  `chan_unistim` (udp/5000), `pbx_dundi` (udp/4520), `pbx_ael`, and `pbx_lua`.
  On a host-networked add-on each of those was an open, unused listener directly
  on the LAN; the AEL/Lua demo dialplans (and their sample mailbox hints) also
  spammed the boot log. This is a PJSIP-only system — the entire dialplan is
  generated into `extensions.conf`.
- Corrected the documented Asterisk major version: the add-on ships **Asterisk
  20** (from the Home Assistant Alpine 3.21 base image), not 21, as the boot log
  has always reported. README, DOCS, and the image comments now match the
  runtime.

## 0.43.0

Busy-guard on the handset announce endpoint: an announcement is no longer
originated to a phone that is already on a call.

- `POST /api/announce/{ext}` now reads the target's device state
  (`DEVICE_STATE(PJSIP/<ext>)` via a channel-less AMI `Getvar`) immediately
  before the originate. When the phone is on or being offered a call
  (`INUSE` / `RINGING` / `RINGINUSE` / `BUSY` / `ONHOLD`), the call is not
  placed: an INVITE to a busy phone cannot auto-answer (the intercom
  `Call-Info` header acts only on an idle device) and instead rings the
  handset as call waiting — a phantom ring right after an alarm announcement
  when a duplicate dispatch reaches a handset still in its announce call.
- A skipped announcement is logged and reported as
  `{"ok": true, "skipped": "busy", "device_state": …}` (HTTP 200), so callers
  treat it as handled rather than re-queueing identical content behind the
  call in progress.
- The guard fails open — an unreadable device state (AMI hiccup) never blocks
  an announcement — and applies only to the announce endpoint: ordinary
  inbound/outbound calling, paging, test rings and wake-up calls are
  unchanged.
- New `ami.get_device_state` / `ami.device_busy` helpers, covered by
  wire-level and truth-table tests in `tests/test_webui.py`.

## 0.42.3

Repository cleanup: unused code removed and documentation reconciled with the
source. No add-on behaviour changes.

- Removed three unused helpers with no callers — `ami.get_contacts`,
  `ami.get_channels`, and `mwi_store.all_flags` — each superseded by
  `ami.get_status_bundle` / `mwi_store.exts`. Their underlying `*_from_blocks`
  parsers remain in use and are unaffected.
- Dropped an obsolete `DOCS.md` §14 troubleshooting row describing a web-terminal
  `ValueError` at start as harmless; that startup race was eliminated in 0.29.1, so
  such a `ValueError` now indicates a genuine fault rather than a benign one.
- Removed the inert `armv7` base image from `build.yaml`; the manifest, README, and
  DOCS all declare `amd64`/`aarch64` only.
- Corrected a stale `switchboard-config` comment that described the tmpfs AMI secret
  file as `0600`; it is created `0640`, as `SECURITY.md` already documents.
- Genericized the SIP-trunk POP hostname in the test fixtures to `losangeles.voip.ms`,
  matching the documentation and completing the 0.42.2 hygiene pass.

## 0.42.2

Public-repo hygiene: no real home network details or personal contact in the tree.

- Genericized every real LAN address to the example range `192.168.1.x` across the
  documentation, the shipped monitors (their fallback defaults), the tools, and the
  test fixtures. No add-on behaviour changes — the affected values are fallback
  defaults and examples that a live install overrides.
- Corrected the documented `cordless_ip` default to `""` (matches the shipped
  manifest; a live install auto-follows the cordless via `cordless_ext`).
- Vulnerability reports now route through a GitHub Security Advisory rather than a
  personal email; the maintainer field no longer carries a personal address.
- Removed `STATUS.md` — an internal, stale maintainer handoff that documented the
  reference deployment; `README.md` and `switchboard/DOCS.md` are the reference.
- The `wp826-cli.exp` tool reads `WP826_HOST` (matching `wp826.mjs`) instead of a
  hard-coded address.


## 0.42.1

Fix: one-way-audio detection missed a near-dead direction (not just a fully dead one).

A live call scored "excellent" despite receiving **one** inbound RTP packet across
38 seconds — the caller's audio never reached the PBX. The one-way detector treated
a direction as dead only when its packet count was *exactly* zero, so a lone stray
packet (`rxcount=1`) read as healthy. It now treats a direction carrying fewer than
~10 packets over the whole call as dead — a live G.711 leg streams ~50 packets/s
even in silence (VAD is off), so a handful of packets is effectively no audio. The
existing guard (require >50 packets on the *live* side) still prevents a call-setup
blip from false-alarming. Found by the durable `callqos.jsonl` ledger during a log
audit; the monitor otherwise correctly flagged 9 genuinely rough calls out of 66.


## 0.42.0

Documentation: a generated Word + PDF manual, built and attached automatically.

`scripts/build-docs-docx.py` assembles README + SECURITY + DOCS.md into one
printable manual — an editable **.docx** (via pandoc) and a reader-friendly
**.pdf** (via LibreOffice, no LaTeX) — and a new `docs` CI workflow builds both on
every push/PR (uploaded as an artifact, so a DOCS.md that stops converting fails
the check) and attaches them to each published GitHub Release. Same process the
ecoflow-panel and zwave repos use.

The reference itself was refreshed for the current release: the real
prefix-free **direct-dial GXW dial plan** and the **SSH CLI** config path (with
the `P85 = 3 s` rotary-safe send delay), the **full-screen operator console** with
its trunk-registration / STT-health / per-room-RTT signals, and a new
**"Reproducing on new hardware"** chapter (rebuild from a release tag; restore
config from a Home Assistant backup).


## 0.41.1

Fix: saying just "time" to the operator now reaches the talking clock.

Whisper transcribed a caller's "time" request correctly (heard `Time.`), but the
operator's clock trigger only listed multi-word forms ("the time", "what time
is it", "time please"…) and not the bare word **"time"**, so the most natural
utterance matched nothing and the operator gave up. Added `time` (plus "the
current time" / "got the time") to the clock phrases. Whole-word matching still
protects fragments — "anytime", "overtime", "bedtime" don't trip it — and a
confident room match still wins first, so nothing named after the word is
shadowed. No other feature had a bare-word gap (weather/power/directory/announce/
page/intercom all already resolve as single words).


## 0.41.0

Full-screen operator console + the new signals mirrored into the TUI.

The telnet/browser console now uses the **whole terminal width** instead of a
72-column board floating in the middle: the rules span the full width, the header
puts the title on the left and the online/on-call/clock stats on the right edge,
and each room row right-aligns its live **link RTT** (idle qualify round-trip) and
any notable contact status. A compact status line under the header shows the same
two signals the web dashboard got in 0.40.0 — **trunk SIP registration** (green
Registered / red Unregistered / grey Unknown) and **resident-STT health** (green
resident / amber CLI fallback). It's still vertically centered and degrades
cleanly on a narrow window (the right-aligned detail drops rather than wrapping).

The trunk-registration and STT-health reads are on their own 20s throttle (they
each cost an extra AMI login / loopback probe), separate from the ~3s board poll,
so console AMI churn stays negligible. No behavior change to calls or routing.


## 0.40.0

Dashboard: trunk registration status + resident-STT health.

The header sub-line now shows two things it never surfaced before:
- **Trunk registration** — for an enabled outside line, its live SIP registration
  state (green *Registered*, red *Rejected/Unregistered*, grey *Unknown*), read via
  the AMI `PJSIPShowRegistrationsOutbound` action. A dead trunk registration
  (wrong secret, NAT pinhole, provider down) kills all PSTN calls but was
  previously invisible in the UI.
- **Resident STT** — an amber *"STT: CLI fallback"* when the whisper-server isn't
  answering (so voice recognition is silently running on the slow per-call
  whisper-cli); quiet when it's resident or intentionally disabled.

Both ride their own longer-TTL caches (20s / 8s) so the hot `/api/status` path and
its 3-tuple bundle are untouched; the whisper probe is a 1s loopback GET. Completes
the dashboard observability alongside the per-phone link health added in 0.39.0.
(The telnet/browser TUI mirror of these is coming next.)


## 0.39.0

Dashboard now shows per-phone link health (contact status + round-trip time).

Each registered, idle room card now displays its SIP contact status and idle
round-trip time (e.g. "Reachable · 2.4 ms") beneath the status pill — the data was
already collected (AMI ContactList RoundtripUsec, surfaced in /api/status) but never
rendered. At a glance you can now see which handsets are responding quickly and spot
a laggy one (e.g. the Wi-Fi cordless) without opening logs. Rendering-only change to
the web dashboard; no new polling. (Trunk-registration status and whisper-server
health indicators are coming next.)


## 0.38.0

Voice operator feels snappier — the ~1s recognition now hides behind the prompt.

Every voice flow (dial-0 operator, dial-a-status 45, directory 411, announce 46,
wake-up 42, automation 43) recorded your speech, played "one moment," and *then*
ran whisper — so the ~1s of recognition was dead air added after the prompt. Now
the transcription is started (as a background process) just BEFORE the "one moment"
prompt and collected just after, so the ~0.9s of prompt audio plays OVER the
inference instead of before it. That shaves roughly the prompt's length off every
voice interaction, cutting the post-speech wait from ~2s to ~1s.

Safe by construction: the STT child reads its input from the recorded WAV file
(stdin=/dev/null) and only talks to the loopback whisper-server — it never touches
the AGI's stdin/stdout, so it runs concurrently with the (blocking) prompt playback
without any chance of corrupting the Asterisk AGI protocol stream. The 25s STT
backstop, error handling, and the recognition result contract are unchanged.


## 0.37.0

Operator greeting now advertises the voice features, plus public-repo hygiene.

- **Greeting** — dial-0 now says "Say a room name, or ask for the time, weather,
  directory, or lights," so the voice features (added in 0.33.0) are discoverable
  instead of hidden. Inbound outside calls ring only the cordless by default now
  (`inbound_ext` example trimmed to a single ext; re-add a second to group-ring).
- **Public-readiness** (the add-on is open-source): genericized the example
  provider username and caller-ID in the test fixtures to fictional values
  (`100000_switchboard`, a 555-0100 reserved number) so no real account identifier
  or home phone number ships in the repo; neutralized the shipped default
  `cordless_ip` (was a real-home address) to empty (device-health auto-follows the
  cordless's registration anyway); hardened `.gitignore` against accidentally
  committing secrets/state. No secret was ever committed — nothing required
  rotation (verified against full git history).


## 0.36.0

Hardened the whisper-server RAM gate against the boot-race that already bit the
console services (v0.30.1).

The resident speech recognizer idles (`exec sleep infinity`) only when no voice
feature is enabled. That gate was the last one still written as a multi-flag
`! bashio::config.true` chain — and bashio can momentarily read a **blank** options
value at boot / during a reload, which `! bashio::config.true` treats as "not
enabled". A transient all-blank read would therefore satisfy every clause and
**permanently idle the recognizer even with features on** (s6 sees the idle process
as "started" and never restarts it), silently forcing the slow per-call
`whisper-cli`. The gate now idles only when every feature reads an explicit literal
`false`; a blank read falls through to "stay resident" (the safe default). The gate
lint (`test_run_gates.py`) is now multi-line-aware so this class can't creep back.


## 0.35.0

Direct dial now **requires a leading `1`** (NANP 11-digit) instead of also
accepting a bare 10-digit number.

0.34.0's direct mode routed both 10- and 11-digit numbers. The bare-10-digit
pattern was the problem: it made feature codes **41–46** and any extension
starting **2–9** (i.e. **20**) look like the *start* of a phone number, so on
analog phones the gateway had to wait out its inter-digit timer before dialing
them. Requiring the `1` (dial **`1` + the 10-digit number**) removes that: with no
bare-10 pattern, feature codes and extension 20 dial instantly. Only extensions
**12–19** still pause briefly — unavoidable, since they start with `1` exactly
like an 11-digit number.

A bare 10-digit number is no longer routed (falls to Congestion — add the `1`).
`011` international and `1-900` premium stay blocked; the anti-toll-fraud guards
and the 911-not-routed behavior are unchanged. Prefix mode (the default) is
untouched.


## 0.34.0

Outside line: **dial numbers directly, without the `9` prefix** (opt-in).

A new trunk toggle **`direct_dial`** (default off) lets you dial a phone number
straight, like a cell phone — no outside-line prefix. When it's on, a **10-digit**
(`602-555-1234`) or **11-digit** (`1-602-555-1234`) US/Canada number routes out the
trunk, while your 2–3-digit extensions and feature codes still ring internally. The
system tells them apart by number **length** (NANP patterns), since without a prefix
that's the only signal. `011` international and `900`/`1-900` premium stay blocked,
and the anti-toll-fraud guards (transferred-in callers can't reach the trunk) are
unchanged. **911 is intentionally not routed** — this trunk has no E911, so use a
cell phone for emergencies.

Why a toggle and not just blanking `dial_prefix`: the Home Assistant options form
reverts a cleared optional field to its default (`9`), so blanking the prefix didn't
actually disable it — it silently reverted. `direct_dial` is the dependable switch;
it overrides `dial_prefix` when on. Prefix mode (the default) is byte-for-byte
unchanged.


## 0.33.1

Cosmetic: the Switchboard title icon is now a **classic telephone** (☎️) instead
of an electrical plug (🔌), across both the web dashboard header and the operator
console (telnet/browser) headers — matching the `mdi:phone-classic` sidebar icon
and the antique-phone theme. The console uses the emoji-presentation form (with
its variation selector) so it still measures two terminal cells and the board
stays aligned.


## 0.33.0

The operator now recognizes and hands off to **any** feature, not just rooms.

Before, dialing **0** and speaking only routed to a room (or the lights/wake-up
intents). Now the voice operator also understands requests for the **talking clock**
("what time is it"), **house status** ("weather", "power", "battery", "solar",
"thermostat"…), **directory assistance** ("directory", "who's here", "room list"),
**announce** ("make an announcement", "over the speakers"), and **page/intercom**
("page everyone", "all call"). It classifies the spoken request and hands the caller
to that feature's own menu — same as if they had dialed the feature's code directly —
so there's nothing new to memorize. Room names still win over feature words, and a
feature the caller names but that's disabled falls through to a graceful goodbye.

Recognition is offline (whisper.cpp, no cloud). Room-vs-feature is resolved by
match *confidence*, not a fixed order: a confident room match (an exact name or
synonym) always wins, so a handset named "Weather" or "Battery" still connects by
name; only then do the feature words get a look, so a bare word like "page" isn't
misrouted to an unrelated room it merely shares letters with ("Garage"); and a
lower-confidence but genuine room match ("kitchin" → Kitchen) still connects after
that. Wake-up and lights already worked; this generalizes the same hand-off to
clock, status, directory, announce, and page.


## 0.32.0

Device-health monitor auto-follows the cordless's IP (no DHCP reservation needed).

The WP826 cordless is on Wi-Fi and can take a new DHCP lease after a reboot, which
used to break battery/Wi-Fi/MOS monitoring until `cordless_ip` was hand-edited (and
falsely flag the alarm endpoint "unreachable"). Now: the link-health monitor
publishes each phone's registered contact IP (`contact_ip` on
`sensor.switchboard_link_<ext>`), and the device-health monitor takes the cordless's
**current** IP from its live SIP registration via the new **`cordless_ext`** option
(default `19`) — so it auto-follows wherever DHCP puts the phone. `cordless_ip`
remains as the fallback (used when the cordless is unregistered / rtpmon is off /
`cordless_ext` is blank). No reservation required; a lease change no longer blinds
monitoring. IP changes are logged for the durable forensic trail added in 0.31.0.


## 0.31.0

Forensics: much longer log retention + a durable log that survives reboots.

- **Dropped the SIP security-event flood.** `ChallengeSent` + `SuccessfulAuth` on
  every ~60 s re-registration across the fleet were **~97% of all log volume**,
  capping the add-on log's retention at ~11 h and crowding out the operational
  events that matter in a post-mortem. `res_security_log` is now noloaded, so the
  live log holds **weeks** instead of hours. Registration state is unaffected and
  still tracked durably by the link-health monitor (`linkhealth.jsonl`) — a phone
  that fails to authenticate still shows as unregistered there — so nothing useful
  is lost, only the redundant per-attempt spam.
- **Added a durable Asterisk log on the persistent `/data` volume**
  (`/data/state/asterisk.log`). Unlike the container journal (RAM-backed, rotates
  in hours) and the host journal (wiped by a reboot), this survives add-on restarts
  **and host reboots** — so the next time something like the RTP `Network
  unreachable` collapse happens, the evidence is still there afterward. Scoped to
  `notice,warning,error` (no verbose/debug, no security flood), so it grows
  glacially: negligible SD wear, no rotation needed.

No functional/telephony change. Note: the *host* systemd journal is still
RAM-backed and erased by a reboot — capturing host/kernel/Supervisor logs across a
reboot needs off-box syslog forwarding, which is a Home Assistant host-side setup.


## 0.30.2

Honesty cleanup — no behavior change. (1) Removed the `announce_tts_engine` option:
it was write-only dead config (staged into `features.json` but read by nothing — the
dial-46 announcement voice is, and has always been, on-box espeak-ng), so a setting
that claimed to pick the announcement TTS engine actually did nothing. Dropped from
`config.yaml`, `translations/en.yaml`, and the config generator (49 options now). (2)
Fixed three stale "no ffmpeg / WAV only" comments in `announce_asterisk.py` and
`app.py`: ffmpeg *is* best-effort installed and the `/api/announce` `{url}` branch
uses it to decode a non-WAV clip (e.g. an MP3 from HA's tts_proxy) — the docstrings
predated that and contradicted the code. Tests updated to pin the removal.


## 0.30.1

Service enable-gates survive a boot-time config-read race. Each optional longrun
(console-web, operator-console, rtpmon, devhealth, wakeup-scheduler, whisper-server)
idles with `exec sleep infinity` when its feature is off. The gate keyed off
`bashio::config.true 'flag'`, which returns false both for a genuine `false` **and**
for an EMPTY read — and `bashio` can momentarily read blank options at boot (seen
live: the console-web run script logged `console_enabled: false` while the stored
value was `true`). Because an idle process looks "successfully started" to s6, s6
never restarts it, so an *enabled* service could be **permanently idled** until the
next lucky restart — which is exactly what took the console-web browser terminal
offline after the 0.30.0 deploy (the phone system, dashboard, and telnet console
were unaffected). The gates now idle only on an explicit `false`, so a transient
empty read runs (these features all default enabled). New `test_run_gates.py` pins
the anti-pattern out. This is the enable-gate analogue of the v0.29.1 port-parse
hardening.


## 0.30.0

Documentation rewritten from scratch, verified against source. `README.md`,
`switchboard/DOCS.md`, and `STATUS.md` were fully rewritten to match the current
system, and a new `switchboard/SECURITY.md` documents the security model, the
toll-fraud threat model, and the accepted LAN-local risks (the unauthenticated
consoles). The old docs had drifted: they advertised "G.722 and Opus offered" and a
configurable codec list (the add-on has been **G.711 µ-law only** for many releases),
listed only feature codes 0/41–44 (the real set is 0, 41–46, and 411), and STATUS.md
still described v0.1.2 with "no phones registered." Every option, sensor, extension,
port, and default in the new docs is drawn from the source. `tools/wp826-pcodes.md`
was reconciled to its final state (the earlier file appended "DONE" sections without
clearing the contradicting "TBD/OPEN" text) and its stale `wpcli.exp` filename
reference fixed to `wp826-cli.exp`. Hygiene: the Dockerfile `io.hass.version` label,
which had been left at a stale `0.5.0`, now matches the manifest version;
`.pytest_cache/` is gitignored. No functional code change — the add-on image behaves
identically.


## 0.29.1

Console web terminal: survive an empty port env. During the v0.29.0 config-schema
migration, `bashio::config 'console_web_port'` briefly returned an empty string while
Home Assistant rewrote `options.json`, and `server.py` did
`int(os.environ.get("CONSOLE_WEB_PORT", "8100"))` — whose default only covers an
*absent* key, not a set-but-empty one — so it raised `ValueError: invalid literal for
int() with base 10: ''` and the longrun crash-looped until s6 restarted it (the service
self-recovered once the config settled, but the traceback was alarming and recurs on
every schema-changing deploy). Both port parses now use a shared `_env_int()` helper
(`int(get(name, "").strip() or default)`, the same idiom already used by console.py,
rtpmon, and devhealth), and the s6 run script defaults the port in bash too so the log
line is never blank. New regression test loads `server.py` with an empty port env and
asserts it imports without raising. No config or alarm-path change.


## 0.29.0

Friendlier Configuration tab. Added `translations/en.yaml` so every option shows a
proper label + inline help instead of its raw key (`device_health_enabled` →
"Device-Health Monitor" with a sentence explaining it), across all 50 options — core,
voice operator, the call-quality + device-health monitors, announcements, the consoles,
clock/wake-up, feature codes, rooms, and the SIP trunk. Input-type polish: the announce
API token is now a masked `password` field, and the single-extension feature-code fields
(clock/wakeup/automation/page/status/directory/announce _ext) validate as 2–6 digit
numbers in the form. `trunk.inbound_ext` stays a free string — it legitimately holds a
comma-separated list (e.g. `19,20`). A new test pins the translation file to the option
list so a future option can't ship unlabeled.


## 0.28.0

devhealth refinement (live-tuning). The cordless MOS signal now uses the NEWEST call
(by stopTimeSecond) and only flags it when that call was BOTH poor AND recent (within
15 min), instead of the min across the phone's retained RTP history — an old bad call
was pinning the sensor 'degraded'. callqos still owns per-call alerting; here MOS is a
supporting current-state signal. Unknown-age MOS is not flagged (conservative).


## 0.27.0

Proactive device-health monitor for the fleet's two "smart" devices — the WP826
cordless (the alarm/announce endpoint) and the GXW4216 gateway. rtpmon already
watched SIP registration + RTT and fired a FLEET-outage alert (>= half the fleet
down), but two blind spots remained: (1) the cordless is a battery + Wi-Fi device
where power alarms are announced, and its battery dying / Wi-Fi weakening / per-call
audio degrading are all invisible to Asterisk (the callee RTP leg is unmeasurable
from the PBX); (2) a SINGLE critical device offline (the cordless alone; the whole
gateway) never trips the half-the-fleet gate.

New `devhealth` service polls the WP826's OWN HTTP API (the same one `tools/wp826.mjs`
uses) for battery %, Wi-Fi RSSI, and per-call MOS/jitter/loss, and derives GXW health
from rtpmon's rollup (the reliable, already-gathered registration signal — the GXW
blocks ICMP/HTTP off its subnet, so an independent ping would false-alarm on a healthy
gateway). It publishes `sensor.switchboard_cordless_health` + `sensor.switchboard_gateway_health`
(graphable), and fires a one-shot `persistent_notification` on an unhealthy transition
(consecutive-cycle hysteresis, escalation re-alerts, recovery collapses the entry) —
CRITICAL when the cordless is offline or its battery is discharging under 15%, or all
gateway ports drop; DEGRADED for weak/lost Wi-Fi, a low-but-charging battery, poor recent
MOS, or some gateway ports down. Off by default until `cordless_password` is set for the
deep (battery/Wi-Fi/MOS) checks; reachability + gateway health work without it. New
options: `device_health_enabled|interval|alerts`, `cordless_ip|password`, `gateway_ports`,
`cordless_battery_crit_pct|warn_pct`, `cordless_wifi_min_signal`.


## 0.26.0

Distinctive ring for outside-line calls, done properly. The `[sw-alert]` pre-dial
subroutine now tags inbound-trunk INVITEs with a plain-text `Alert-Info: <…>;info=outsideline`
instead of `info=Bellcore-drN` (which only changed the ring *cadence*, not the tone —
the reason it "didn't sound different"). The WP826 cordless has a Match-Incoming-Caller-ID
rule (account P1488="outsideline" → ring tone 3) that plays an obviously different ring
for any call carrying that tag. The WP826 side was set with the new `tools/wp826.mjs`
scriptable config client (no browser) — see `tools/wp826-pcodes.md`.


## 0.25.2

The announce URL branch now transcodes non-WAV audio: Home Assistant's tts_proxy
serves MP3 by default (even for Piper), which the pure-Python WAV reader can't
decode. Added ffmpeg to the image and an ffmpeg fallback in render_url_to_8k
(fetch -> WAV directly, else ffmpeg -> 8 kHz mono WAV), so the HA media_player path
(tts.speak / the ecoflow-panel alerts -> media_player.cordless_speaker) actually
plays on the cordless. Best-effort: without ffmpeg the {url} branch is WAV-only; the
{text} branch is unaffected.

## 0.25.1

Announce now auto-answers onto the cordless speaker (it was ringing instead): the
originate carries the standard SIP intercom header `Call-Info: <sip:...>;answer-after=0`,
which the WP826 honors ("Allow Auto Answer by Call-Info/Alert-Info" is on) — so an
alert plays hands-free. The distinctive-ring Alert-Info now uses the proper
`<uri>;info=Bellcore-dr2` form (the bare token wasn't recognized); note Bellcore only
changes ring CADENCE — a different ring TONE for outside calls is a handset
Match-Caller-ID rule (GUI).

## 0.25.0

Make the cordless a home-wide announcer and give it a distinctive outside-line ring.

**Announce endpoint (`POST /api/announce/{ext}`).** Speaks a message OUT a room
handset: render TTS (`{"text": ...}` via espeak, or `{"url": ...}` fetching a WAV,
e.g. `tts.piper`) to an 8 kHz clip Asterisk `Playback` can read, then originate an
auto-answer call so it plays on the speaker. This is the SIP equivalent of a
`media_player` — a companion HA custom-component exposes it as
`media_player.cordless_speaker`, so any HA TTS/automation (and the ecoflow-panel's
audible alerts) can announce to the cordless exactly like the ecobee speakers. The
endpoint is Supervisor/loopback-only, or reachable over the LAN with the new
`announce_token` (so the Core-container component can trigger it); it can only play
a local clip to a configured ext, never place an outside call.

**Room directory (`GET /phonebook.xml`).** Serves the configured rooms as Grandstream
GS-Phonebook XML for the WP826's Remote Phonebook, so the cordless shows room NAMES
on caller-ID and dials by name.

**Distinctive ring for outside calls.** Inbound trunk calls tag the INVITE to the
answering handset with a Bellcore `Alert-Info` (via a `b()` pre-dial subroutine), so
the WP826 cordless rings differently for an outside call than a room-to-room call.
Analog handsets ignore the header; the inbound leg stays `r`-only (no re-armed
DTMF-transfer toll-fraud path).

New `announce_asterisk.py` (dependency-free 8 kHz resample; no ffmpeg/audioop).
Adversarial review before ship fixed: the async handler now offloads its blocking
render/originate off the single event loop (no webui freeze); the URL fetch refuses
redirects and blocks loopback/link-local/reserved hosts (SSRF); clip names are uuid
(no same-second collision); the token is read per-request. Suite: 1489 checks, 0 failures.

## 0.24.0

Findings from a 24h health + call-quality review (multi-agent, adversarially
verified against the live callqos/linkhealth ledgers).

**Fleet-outage availability alert (the real gap).** The link-health poller had
*recorded* an ~11h overnight window where all 8 wired GXW FXS ports lost SIP
registration together (the gateway's SIP stack wedged — the same-subnet WiFi
cordless stayed up, and the inbound DID routes to it, so nothing looked wrong) —
but nothing *alerted*. The poller now fires one persistent notification when a
large fraction of the fleet is unreachable at once (a shared gateway dropping, not
one handset asleep), and a recovery notice when it clears. A two-consecutive-cycle
gate rejects the single-sample "all Unregistered" collector blips. Gate with the
new `link_health_alerts` option (default on).

**Fewer false poor-call alerts.** The 24h ledger showed half the "poor" pushes were
telemetry artifacts, not real audio:

- Asterisk reports `MES=0.0` for a direction it couldn't score yet (a short / setup
  leg with no RTCP) — that sentinel was fed straight into the worst-of score and
  flagged "poor" (e.g. a 4s operator greeting). `MES=0` is now treated as no-data.
- A *collapsed* MES (<40 ≈ MOS 2.0) alongside ~0% loss, only-packetization jitter,
  and a low RTT is a re-INVITE/transfer glitch, not real audio (that MOS is
  physically impossible without heavy loss/jitter). Such a reading is dropped from
  the score. Genuine degradation — which always brings real loss and/or jitter — is
  kept, and one-way audio is still caught by packet counts. The raw `mes_rx`/`mes_tx`
  stay in the ledger verbatim.

The review also confirmed the wired path is already at the latency/jitter floor
(G.711 u-law, VAD off, RTP marked DSCP 46/EF, ~2.5ms LAN RTT), so no gateway audio
settings were changed; the only real call degradation is the WiFi cordless, which
is an access-point/RF matter, not a PBX one.

## 0.23.2

Refine the v0.23.1 warm-up so a *straggler* phone isn't frozen `offline` after a
restart. v0.23.1 settled to the steady interval as soon as the FIRST phone
registered — but the GXW's eight FXS ports re-register over a short window, so a port
that registered a beat late (seen live: ext 17 read `offline` while its siblings were
already up) got stuck offline until the next 300 s poll.

The poller now settles only once the registered count **stabilizes** (stops growing
across a poll), or the ~2 min cap elapses — so all re-registering phones are counted
before it drops to the steady cadence. Genuinely-offline phones (a de-registered
cordless, an unregistered softphone) still settle correctly at the plateau.

## 0.23.1

Fix the link-health poller showing every phone `offline` for a full interval after a
restart. The first poll can run while the phones are still re-registering with
Asterisk (they re-REGISTER a few seconds after it boots), so it published an
all-offline snapshot that then sat there until the next 300 s cycle — verified live:
right after the v0.23.0 deploy all 10 phones read `offline` even though they came
back reachable seconds later.

The poller now runs a short **warm-up cadence** (every 15 s, up to ~2 min) at startup
until a phone actually registers, then settles to the steady interval — so the
sensors reflect reality within seconds of a restart instead of minutes. A genuinely
all-down fleet still settles at the cap.

## 0.23.0

Make a de-registered phone **visible** in link-health instead of vanishing. The
v0.22.0 poller keyed off live contacts only, so a phone that dropped its
registration — notably the WiFi cordless, which de-registers when idle — simply
disappeared from the sensors (the one phone you'd most want to watch).

The poller now builds its roster from Asterisk's **configured endpoints**
(`PJSIPShowEndpoints`) and cross-references registrations, so every configured
phone always has a sensor:

- `sensor.switchboard_link_<ext>` reads its **RTT** when reachable, or a
  non-numeric state when not: **`offline`** (configured but de-registered) or
  `unavailable` (registered but its qualify is failing / just dropped). Each sensor
  also carries a `reachable` attribute — trigger an HA automation on
  `reachable: false` to catch a phone the moment it stops answering (a dropped
  cordless flips within ~2 qualify cycles), whichever non-numeric label it lands in.
- `sensor.switchboard_link_health` gains an `offline` / `offline_exts` split
  alongside reachable/unreachable.

Still read-only and off the call path; the SIP trunk stays filtered out.

## 0.22.0

Add an **idle link-health poller** (`switchboard-rtpmon`) so a degrading link — the
WiFi cordless especially — is visible on a Home Assistant trend graph *between*
calls, not only while one is up. (Live testing showed the cordless swing from MOS
1.4 to 4.3 on back-to-back calls; this catches that variation continuously.)

Every `link_health_interval` seconds (default 300) it reads Asterisk's own PJSIP
qualify — the OPTIONS keepalive it already sends each phone — over AMI, and publishes:

- **`sensor.switchboard_link_<ext>`** per phone — qualify RTT in ms (graphable),
  `unavailable` when the phone is offline, with status/name as attributes.
- **`sensor.switchboard_link_health`** — a rollup: worst reachable RTT as state, the
  reachable/unreachable split + which extensions are down as attributes.
- **`/data/state/linkhealth.jsonl`** — a capped history for offline analysis.

Read-only and off the call path: an AMI hiccup just skips that cycle. Gate with
`link_health_enabled` (default on). This replaces the originally-planned
channelstats-based "both-legs" capture, which proved unusable on this system
(`pjsip show channelstats` returns no valid rows for bridged calls) — and was
redundant anyway, since each call's initiating record already carries both
directions.

## 0.21.1

Fix poor-call notifications silently not firing. Live-verified v0.21.0 on a real
degraded call (a cordless call that scored MES 59 — the telemetry caught it): the
`[rtpqos]` log line, the JSONL ledger, and `sensor.switchboard_last_call` all
populated correctly, but the persistent notification never appeared.

Root cause: the sink does two HA calls on hangup — set the sensor, then create the
notification. The dialplan backgrounds it with `&`, but Asterisk destroys the call
channel the instant it hangs up, and that cut the process off *after* the sensor
push but *before* the notification. The sink now **detaches into its own session**
(`--detach` → `fork`+`setsid`) so channel teardown can't kill it mid-push. Verified
the notify path itself is correct (it fires cleanly when run to completion).

## 0.21.0

Turn the per-call `[rtpqos]` log line into **visible, proactive telemetry** in Home
Assistant — you no longer have to grep the add-on log to know how a call went.

Each phone-originated call's `h`-extension now also pushes its numbers to a new
`switchboard-callqos` sink (backgrounded via `TrySystem`, so it can never delay or
wedge a hangup):

- **`sensor.switchboard_last_call`** — the worst-direction Media Experience Score
  (numeric, so HA's Recorder graphs the trend), with codec, duration, per-leg
  loss/jitter/RTT/MES carried as attributes.
- **A persistent notification** (the bell menu) when a call scores poorly — MES
  below ~70 (≈ MOS 3.5), over 3% loss, or 400 ms+ round-trip — naming the reason
  and extension. Keyed by channel so it can't spam; gate it with the new
  **`call_quality_alerts`** option (default on).
- **`/data/state/callqos.jsonl`** — a durable, capped ledger of the last 300 legs
  (readers dedupe by channel), so the raw record is always there for analysis.

Quality is scored on the *worse* direction and *worse* loss, so a partial one-way
problem (e.g. a WiFi-cordless call that read MES 59 in only the receive direction)
can't hide behind a healthy reverse path — and a *total* one-way call (one direction
carrying real traffic while the other is dead) is detected explicitly and flagged
poor, since a dead direction reports no MES for the worst-of scoring to catch. The
context also passes its originating tag (`rooms`/`operator`/`directory`/`from-trunk`)
through the `h`-extension Gosub, so every record and log line attributes the leg.

The `call_quality_alerts` opt-out is honored through the asterisk-readable
`features.json` (the dialplan runs the sink as the asterisk user, which can't read
root-only `options.json`), and non-finite RTCP values (`-nan`/`-inf`) are neutralized
before argument parsing so a degraded leg is still recorded rather than dropped.

## 0.20.0

Make the per-call RTP quality logging from 0.19.0 actually work.

0.19.0 wired the telemetry but it silently logged nothing — diagnosed live over the
Asterisk CLI:

- It used `CHANNEL(rtpqos,audio,…)` (the old chan_sip accessor), which returns
  "unavailable" on chan_pjsip. The correct accessor is **`CHANNEL(rtcp,…)`**.
- It read the stats in a hangup *handler*, which runs *after* Asterisk has already
  torn down the RTP instance. The read now happens in the context's **`h` (hangup)
  extension**, while the RTP is still alive.

So every phone-originated call now really does log a `[rtpqos]` line — grep the
add-on log for it — with jitter, packet loss, round-trip, codec, duration, hangup
cause, and the **Media Experience Score** (rxmes/txmes, ~88 ≈ MOS 4.3). Verified on
live room-to-room calls (0 loss, ~2 ms RTT, MES ~88). Trunk legs are skipped because
VoIP.ms sends no RTCP — nothing to measure there.

This also simplified the dialplan: the 0.19.0 per-Dial `b()` handler and caller-side
pushes are gone (they were the broken path), so the Dials are back to their plain,
already-reviewed flags — the toll-fraud `r`-only inbound / `rT` outbound posture is
byte-for-byte what it was before 0.19.0.

## 0.19.0

Per-call RTP quality telemetry — the numbers to tune call quality precisely.

- **Every call now logs a `[rtpqos]` line per leg when it ends** with the metrics
  that actually characterize audio quality: received/transmitted packet counts and
  packet loss, jitter and round-trip time, the negotiated codec, the call duration,
  the hangup cause, and Asterisk's **Media Experience Score** (rxmes/txmes, a
  0–100 MOS-like rating). Grep the add-on log for `[rtpqos]` to see, for any call,
  exactly what each end experienced.
- **Both legs are captured.** A hangup handler is registered on the caller (before
  each Dial) and on the callee (via each Dial's pre-bridge gosub), so a room-to-room
  call reports both phones, an outbound call reports both your phone and the trunk
  leg, and an inbound call reports the provider leg and the answering handset.
- **No noise on calls that never connect** — a leg that carried no media (a
  ring-no-answer) is skipped.

The telemetry is read-only and off the call's critical path (it runs during
teardown), so it can't affect a call in progress; the existing toll-fraud transfer
guards are unchanged (verified: the added handler is a Dial *option argument*, not
a flag, so inbound legs remain `r`-only).

## 0.18.0

Observability and hardening from the audit.

- **A missed wake-up now tells you.** If a wake-up call can't be delivered (the
  phone stays busy or offline through its whole grace window), it used to be
  dropped log-only — invisible unless you were tailing the add-on log. It now
  raises a Home Assistant persistent notification naming the extension and time.
- **Room phones get an RTP watchdog.** If a call's media stalls mid-call — the
  Wi-Fi cordless drops off the access point, an analog port wedges — the channel
  is now torn down instead of leaving a dead-air call up forever and leaking the
  RTP port (the SIP trunk already had this; the room phones didn't).
- **Two config traps closed.** A trunk password containing `;` or leading/trailing
  whitespace (which Asterisk silently truncates, breaking registration with no
  obvious cause) is now rejected the same way room secrets already were; and an
  all-zero extension ("00"), which passes the digit check but is undialable, is
  rejected instead of silently never ringing.
- **Every voice AGI is belt-and-suspenders executable.** The announce, status, and
  wake-up-delivery scripts are now in the image's explicit `chmod +x` list too, so
  a dropped execute bit can't silently disable a feature.

## 0.17.0

Operator-console (TUI + browser) robustness, from the audit.

- **The console no longer garbles on a small window.** A line wider than the
  terminal is truncated to fit (colors preserved) and the whole board is clamped
  to the terminal height, so a 60-column or short window degrades gracefully
  instead of wrapping lines and scrolling the header off. Wide terminals are
  unchanged.
- **A single Escape now cancels.** Pressing Esc once to back out of connect /
  transfer / wake-up / lights used to do nothing until the next key — a lone Esc
  couldn't be told apart from the start of an arrow-key sequence, so it sat
  buffered. It's now flushed on the next idle tick (a normal terminal's
  escape-timeout).
- **A stalled browser tab can't lock everyone out.** A web-terminal peer that
  completed the WebSocket handshake then stopped reading used to block the bridge
  in `sendall` forever, leaking one of the five console sessions (and a telnet
  slot) until the add-on restarted — five such peers locked out all operators.
  Writes are now bounded and a stuck peer is reclaimed like any dead connection.

## 0.16.0

Correctness pass from a full-system audit — fixes voice mis-recognitions that
could take the wrong action, and a broken announce that failed silently.

- **Announcements (dial 46) no longer fail silently.** The configured speakers
  were two entity IDs that no longer exist in Home Assistant, and HA returns
  "success" for a play to a missing entity — so the operator said "announcing on
  the speakers" while nothing played. The default now points at the real speakers,
  the announce flow verifies each speaker exists before recording (a stale ID is
  now an honest "the speakers are unavailable"), and `ha_client` logs every
  rejected HA call instead of swallowing it.
- **Directory (411) can't mis-dial a room when it mishears "list".** On a
  narrowband line whisper hears "list" as *lift* / *least* / *last* / *listing*,
  which used to clear the fuzzy room threshold and **connect a call to the wrong
  room**. Resolution now fails safe: an unambiguous room name wins, then cancel,
  then a (fuzzy) list request reads the directory, then a weak room match — a
  mis-hear reads the list or re-prompts, never dials. A room literally named
  "List"/"Cancel" is reachable again too.
- **Wake-up times parse the way people actually say them.** A leading filler word
  ("um seven thirty", "make it seven thirty", "around seven") no longer rejects a
  clearly-spoken time, and "quarter to one p.m." now resolves to 12:45 instead of
  00:45 (am/pm is applied to the target hour before subtracting).
- **Dial-a-status (45) tells the truth about the lights.** Lights Home Assistant
  can't reach are reported as *unavailable*, not silently counted as "off" — so a
  dead lighting network no longer says "all lights are off". A wedged sensor that
  reports `nan`/`inf` is treated as "no reading" rather than spoken verbatim.
- **Voice menus are less trigger-happy.** Asking the operator for a room no longer
  diverts to the lights flow just because the word sounds a little like "lights"
  ("flights" ≠ lights), a "Control Room" is no longer swallowed by home-control,
  and answering the status menu "power, thanks" serves power instead of hanging
  up. An explicit category ("the one about the weather") beats a bare "one/two".

## 0.15.0

Two voice additions: the operator now understands "wake-up call", and a new
**directory assistance** service at **411** looks up a room by name.

- **Say "wake-up call" to the operator.** Dial 0 and ask for a "wake-up call"
  (also "wake me up", "morning call", "set an alarm") and the operator hands you
  straight to the wake-up flow (dial-42) — no need to remember the number. The
  intent is matched as a whole phrase, so it won't fire on an unrelated sentence
  that merely contains the word "wake".
- **Directory assistance at 411.** Dial **411**, say a room name, and you hear
  its extension and get connected ("Kitchen, extension 11. Connecting you now.").
  Say **"list"** to hear the whole directory; **"goodbye"** to leave. Recognition
  reuses the resident whisper-server and the same validated room list as the
  operator, biased toward your room names for accuracy. The dial code is
  configurable (`directory_ext`, default `411`) and skips itself if it would
  collide with a room or another feature's extension.
- **Safe by construction.** The 411 flow never dials on its own — the AGI only
  proposes a room ext, and the `[directory]` dialplan re-validates it against the
  known-room allow-list before connecting, so a mis-recognition can't reach an
  outside number. When the trunk is on, the connect leg is pinned to the
  outbound-free `internal-xfer` transfer context (same toll-fraud guard as 0.13.2).
- **Feature-independent.** The shared rooms map is now staged whenever the
  operator **or** the directory is enabled, so turning the operator off no longer
  leaves 411 with an empty directory. The `list` re-prompt is capped so a noisy
  line can't loop.
- **Resident STT now covers every voice feature.** The whisper-server RAM gate
  previously listed only operator/wake-up/automation, so an install running only
  dial-a-status (45) or announce (46) fell back to the slow per-call `whisper-cli`.
  The gate now enumerates all six STT consumers (0/42/43/45/46/411) — each keeps
  the recognizer resident when enabled.

## 0.14.0

Voice recognition is now resident — the operator, wake-up, and automation flows
respond noticeably faster.

- **whisper stays loaded in RAM instead of reloading per call.** `switchboard-stt`
  used to spawn `whisper-cli` for every utterance, reloading the ~142 MB `base.en`
  model from disk each time (seconds of latency on every dial-0 / 42 / 43 step). A
  new supervised **`whisper-server`** (whisper.cpp's HTTP server) keeps the model
  resident on loopback `127.0.0.1:8126`; `switchboard-stt` POSTs the recording to
  it and gets a transcription back without the reload.
- **Fails safe, always.** If the server is down or still loading at boot,
  `switchboard-stt` falls back to the unchanged `whisper-cli` path (nothing
  depends on the server, so a crash-looping recognizer can never gate a call). A
  post-connect server hang returns empty so the caller's re-record loop handles
  it — the server-timeout + any fallback is budgeted to stay under the AGI's hard
  kill, never stacking a slow CLI run on top.
- **Least privilege + resource-aware.** Binds loopback only (never the LAN),
  runs as the unprivileged `asterisk` user, and idles (holding no RAM) when the
  new `stt_resident` option is off or when no speech feature is enabled.
- **One-flip rollback:** set `stt_resident: false` to idle the server and revert
  to exactly the old per-call `whisper-cli` behaviour, no other change needed.

## 0.13.2

Close a toll-fraud path: an outside caller transferred in can no longer dial out.

The v0.12.2 fix made the inbound trunk `Dial` `r`-only so an outside caller can't
invoke feature codes directly. But once a household member transfers that outside
caller to a room (or the operator), the caller could land on a leg where the
`##`/`*2` DTMF transfer feature is armed — and a transfer target used to resolve
in `[rooms]`, which carries the `9`-prefix outbound rule. So a transferred-in
caller keying `## 9 <number>` could place a call on the trunk (toll fraud). Three
complementary, defence-in-depth layers now close this, all gated on the trunk
being enabled (non-trunk installs render byte-identically):

- **Origin guard (version-independent hard stop).** The outbound rule refuses
  origination from the trunk endpoint itself: `CHANNEL(endpoint) == "trunk"` →
  Congestion. Doesn't depend on any transfer-context behaviour.
- **Internal-only transfer context.** A new `[internal-xfer]` context (literal
  room extensions + `0`→operator, and *no* outbound/`_X.` rule) is where all
  `##`/`*2` transfer targets resolve, stamped via the inherited
  `__TRANSFER_CONTEXT` on the trunk endpoint (birth-time) and before every armed
  Dial. Keying `## 9 <number>` matches nothing and the transfer fails cleanly.
- **REFER rejection on the trunk.** `allow_transfer = no` on the `[trunk]`
  endpoint blocks a provider-side SIP REFER. Room endpoints keep transfers, so
  the cordless/iPhone **Transfer button still works** to rooms/operator.

Legitimate internal transfers are unchanged — room↔room and to-operator `##`/`*2`
still work; a room dialing `9`+number directly is unaffected (transfer-context
only governs transfer-target resolution, never normal dialing). What's newly
blocked is `##`/`*2`-transferring an active call *out to a PSTN number* — never an
advertised feature, and itself a toll vector.

## 0.13.1

Call-audio tuning, the second batch from the deep audit (the call-path changes,
kept separate from the v0.13.0 control-plane fixes).

- **Adaptive jitter buffer on the inbound trunk leg.** Audio from VoIP.ms crosses
  the public internet (jittery); the LAN legs don't. `[from-trunk]` now sets
  `JITTERBUFFER(adaptive)` on the trunk channel before dialing the handset, so
  the answering cordless/FXS hears a de-jittered stream instead of choppy audio.
- **RTP watchdog on the NAT'd trunk.** `rtp_timeout = 60` / `rtp_timeout_hold =
  300` on the `[trunk]` endpoint tear a channel down if its media stalls
  (provider drops it, NAT pinhole closes mid-call) instead of hanging forever and
  leaking the RTP port. 60s (not 30) so one-way early media during the 30s inbound
  ring can't trip it before answer.
- **DSCP/QoS marking.** RTP audio marked EF and SIP signalling CS3 on the
  endpoints/transport, so the Wi-Fi cordless's voice gets WMM priority over bulk
  LAN traffic. Only helps if the AP honours DSCP (most do); zero-risk otherwise.

## 0.13.0

Performance + log/SD-card hygiene, from a deep multi-agent audit. Headline: the
operator console no longer hammers Asterisk's manager 24/7.

- **The console AMI poller now runs only while a client is connected.** It used
  to log into the manager, read status, and log off **every 3 seconds around the
  clock even with nobody watching** — ~28,800 login/logoff cycles a day. That
  churn filled the Asterisk log ring buffer (real call/error events scrolled out
  within ~2 minutes) and was constant SD-card write pressure on a Pi already
  prone to card wear. A `ClientGate` (threading.Condition, no lost-wakeup race)
  parks the poller with zero AMI traffic when no telnet/ttyd client is attached,
  and wakes it on connect so the first frame is fresh. Idle console → zero churn.
- **`logger.conf` writes one channel, not two.** The redundant `messages =>` file
  duplicated every log line to the SD card (unrotated, read by nobody — the
  console stream already reaches the add-on log via journald). Dropped.
- **`cdr_csv` no longer loads.** It appended `Master.csv` to the SD synchronously
  per call for records nothing reads (VoIP.ms keeps the authoritative CDR).
- **`/api/status` has a short-TTL single-flight cache.** An open dashboard
  refreshes every 4s and the transfer pre-check reads the same data; they (and
  extra browser tabs) now coalesce onto one AMI session instead of each opening
  their own. Errors propagate uncached, so callers keep their fail-open handling.
- **Config-generator correctness:** a room/trunk secret containing `;` or
  leading/trailing whitespace is now rejected loudly (Asterisk would silently
  truncate it and break registration); a **disabled** clock no longer falsely
  blocks a wake-up code at the same ext; trunk `from_user`/`from_domain` are
  charset-validated (falling back to the validated username/host) like the
  other trunk fields.

## 0.12.7

Talking clock (dial 41): fuller military phrasing.

- **"&lt;hour&gt; &lt;minute&gt; hours, and &lt;n&gt; seconds", with "hundred" on the hour.**
  14:32:05 → "fourteen thirty-two hours, and five seconds"; 14:00:05 → "fourteen
  **hundred** hours, and five seconds"; 09:05:30 → "oh nine oh five hours, and
  thirty seconds". A :00 minute is spoken "hundred"; the seconds are now a plain
  cardinal ("five", "thirty", "zero") set off by the "hours, and" prompt so they
  no longer blend into the hour/minute groups.
- The words that don't exist in Asterisk's core-sounds ("hours"/"and"/"seconds")
  are two short espeak prompts (`sw-hours-and`, `sw-seconds`) in the same voice
  as the other synthesized prompts; the number digits keep the professional
  recorded voice. `clock_speak` and its 400-case test sweep were updated to the
  new phrasing.

## 0.12.6

Fancier talking clock (dial 41): 24-hour time, with seconds, on a loop.

- **"At the sound of the tone, the time will be &lt;HH MM SS&gt;" &lt;tone&gt;, repeating
  until you hang up.** The clock now speaks 24-hour ("military") time including
  seconds and loops, instead of announcing the 12-hour time once and hanging up.
- **Clean military readout, no `SayUnixTime` quirks.** Each field is spoken as a
  natural two-digit group — 14:32:05 → "fourteen, thirty-two, oh five"; 09:05:00
  → "oh nine, oh five, oh oh". The old `SayUnixTime` 24-hour format was avoided
  because its minute specifier says "o'clock" for :00 and its seconds support is
  version-dependent. The readout is a small AGI (`switchboard-clock.agi`) over a
  pure, unit-tested sequencer (`clock_speak.time_actions`) that emits only the
  digit sound files Asterisk ships (there are no "hours"/"minutes"/"seconds" word
  files), so the exact spoken sequence for any time is testable without a phone.
- The loop lives in the dialplan, so hanging up simply ends the call. New espeak
  preamble prompt `sw-at-sound-tone` (same voice/format as the existing prompts).

## 0.12.5

Dial-43 lights: saying "list" now actually lists the rooms/lights.

- **Command words are now in the recognizer's bias.** Whisper was primed with
  only the area/light names, so a spoken "list" had no prior and came back as a
  sound-alike ('Left', 'Lift' — observed live) that matched nothing and burned
  the caller's retries. Every stage's bias now includes its command words
  ("list", "cancel"; the action stage — which had **no** bias — gets
  "turn on turn off cancel").
- **Fuzzy intent fallback fixed for 'list'.** The 0.8 similarity cutoff
  rejected the docstring's own example ("lest" scores exactly 0.75). Intents
  that *act* on the house (on/off/cancel) keep the strict 0.8; the benign
  'list' (speaks options, re-prompts, never acts) accepts 0.75, catching the
  live mishears lift/lisp/lest. A lone "left" also maps to list ('left' vs
  'list' is only 0.5 — too far for any sane ratio) — single word only, so
  "left hallway" stays matchable as a real area/light name, and "lamp" is
  untouched (real light names must stay selectable).

## 0.12.4

Operator polish for outside callers, from a packet-level audit of the inbound
call path (which came back healthy end-to-end: continuous 50 pkt/s caller
audio through answer → hold → transfer → operator, speech recognized and
connected on every attempt).

- **Operator listens with more patience.** The recording window is now 10 s
  with a 4 s silence cutoff (was 7 s / 3 s). An outside caller who hesitates
  after the beep — cell latency, unfamiliar flow — no longer gets cut off
  before speaking (a too-early cutoff transcribes as silence and reads as
  "extension not found").
- **Recording diagnostics.** The operator logs each recording's byte size
  (`[operator] rec attempt=N bytes=B`) before transcription; the WAV itself is
  deleted after STT, so this breadcrumb is what distinguishes "caller said
  nothing" from "audio never arrived" after the fact.
- **MWI-clear is gated on room callers.** Dial-0 from an outside line (via
  transfer) ran an MWI clear against the external caller ID — a guaranteed
  failure plus a queued replay. It now runs only when the caller is a
  configured room ext.

## 0.12.3

Inbound calls failed outright ("Channel not available") — fixed by keeping the
router's NAT pinhole open.

- **Trunk re-REGISTER every 120s instead of the 3600s Asterisk default.** The
  VoIP.ms CDR showed inbound calls dying at the provider with `Status is
  'Channel not available' / Failover due to 'Unreachable' status` — the INVITE
  never reached Asterisk (nothing in our logs). Cause: the REGISTER is the only
  outbound traffic that holds the router's UDP NAT pinhole open (trunk qualify
  is deliberately off because VoIP.ms drops OPTIONS), and an hourly REGISTER
  leaves the pinhole closed ~55 minutes of every hour, so the provider's
  reachability pings get dropped and it marks the line dead. Especially bitten
  after a power-event router restart clears the NAT table. `expiration = 120`
  (VoIP.ms's own NAT guidance and their accepted minimum) keeps the path warm
  and the provider's reachability view fresh.

## 0.12.2

Inbound trunk calls no longer connect oddly on the cordless, plus a startup
log-noise fix surfaced while reviewing the logs.

- **Inbound calls: no more accidental transfer-to-operator (the reported bug).**
  An incoming call rang the cordless, connected, then mid-call the caller was put
  on hold and blind-transferred to the operator ("goodbye", hang up). Cause: the
  inbound `Dial()` carried the `t`/`T` flags, which arm Asterisk's in-call DTMF
  transfer codes (`##`/`*2`) for **both** parties — so the answering phone could
  accidentally `##` the caller away, and, worse, the **outside caller** (`T`)
  could invoke feature codes and reach the internal dialplan (a toll-fraud /
  dialplan-injection path). The inbound `Dial()` now uses `r` only. SIP phones
  still transfer intentionally via their own Transfer button (SIP REFER, which is
  independent of these flags).
- **Feature-code flags are now scoped by trust, never armed for a PSTN party.**
  Room-to-room and operator Dials keep `tT` (both ends internal); the **outbound**
  trunk Dial drops `t` (the far PSTN callee can't invoke our codes) but keeps `T`
  (our caller still may transfer).
- **Silenced ~50 ALSA errors per startup.** With no sound card in the container,
  the ALSA/console channel drivers spammed `cannot find card 0` / `Unknown PCM
  sysdefault` at every boot. `modules.conf` now `noload`s `chan_alsa.so` /
  `chan_console.so` — this PBX is PJSIP/RTP only.

Note: the offline iPhone (ext 20) still logs one harmless `invalid URI … No route
to destination` per inbound call for its own leg while the reachable phones ring;
it clears once that softphone registers. An earlier cut of this release gated the
ring group on `DEVICE_STATE` to suppress that line, but adversarial review found
it could also drop a *registered* WiFi cordless from a call after a single missed
`qualify` keep-alive — so the gate was removed in favor of the harmless log line.

## 0.12.1

Security sweep: resolve all open CodeQL code-scanning alerts.

- **`/announce` route: realpath containment** (py/path-injection ×2). The name was
  already regex-validated; the resolver (`safe_announce_path`) now ALSO resolves
  via `realpath` and requires the result to stay inside the announce directory —
  two independent layers against traversal/symlink escape.
- **Generated configs no longer world-readable** (py/clear-text-storage ×2
  hardening). `write()` creates every generated config **0640 root:asterisk from
  the first byte** (`os.open` with mode — no umask window), and re-pins the mode on
  rewrite; same for `/run/switchboard/ami.env` (now group-readable by the asterisk
  user, which the dialplan-spawned AMI consumers needed anyway). Plaintext secrets
  in pjsip.conf/manager.conf remain — Asterisk requires them — documented and
  dismissed with justification.
- **Test regex swap** (py/bad-tag-filter). The JS-parse test extracts our own
  template's `<script>` block by string slicing instead of an HTML-ish regex.

## 0.12.0

Phone->speaker announce now plays on the **ecobee** speakers, bracketed by a
station/airport-style **chime**.

- **Chime + message + chime, as one seamless clip.** Dial 46, record your message;
  the add-on builds a single WAV — a bell "attention please" chime, your spoken
  message, then the chime again — and plays it via `media_player.play_media`. One
  file means no cross-file timing races on AirPlay.
- **Targets the ecobees** by default (`media_player.hallway_thermostat`,
  `media_player.guest_hallway_thermostat`) — configurable via `announce_players`.
- The combined WAV is served to the media players over the LAN by the webui on a
  single **`/announce/<name>.wav`** route, exempt from the ingress guard but
  strictly name-validated (no path traversal, `*.wav` only, ephemeral files).
- New `webui/announce_audio.py` (stdlib sine-synth chime + espeak-ng message +
  WAV combiner); `/run/switchboard/announce` staged asterisk-writable.

## 0.11.2

Dial-a-status menu now **loops back to the menu** after each answer instead of
hanging up.

- After speaking power / weather / house, it asks *"Anything else? Say power,
  weather, house, or goodbye"* and keeps going until you say **goodbye** (or the
  line goes quiet) — no more redialing for a second status. Capped at 8 queries
  as a safety stop.

## 0.11.1

Announce UX + a console boot-crash fix, from testing the new voice features.

- **Phone→speaker announce now retries and is more patient.** A test call recorded
  empty (`transcribe heard=''`) because the caller paused after the tone and the
  3-second silence detector ended the recording before they spoke — and the AGI was
  single-shot. It now gives **two tries** with a clearer prompt ("After the tone,
  say your announcement") and a longer, more forgiving window (12 s, 4 s silence).
- **Operator console no longer crashes on boot with an empty `CONSOLE_PORT`.**
  `int('')` raised `ValueError` (s6 restarted it, so it recovered) — now falls back
  to 2300 cleanly.

## 0.11.0

Three Home-Assistant-integrated voice features — pick up any phone and talk to
your house.

- **Dial-a-status voice menu (dial 45).** *"Status menu. Say power, weather, or
  house."* — then hear live state spoken back: **power** (grid up/down, home
  battery %, hours of runway, solar coverage — from your EcoFlow), **weather**
  (fetched from the National Weather Service for the home's coordinates; no HA
  weather entity required), or **house** (thermostat temps + how many lights are
  on). Rotary-safe (voice, whisper.cpp STT).
- **Smart wake-up.** Your dial-42 wake-up now also fires a configurable HA
  **scene** (`wakeup_scene`), reads today's **weather**, and — if you add a
  calendar to HA — your **next event** (`wakeup_calendar`). All optional and
  degrade gracefully; the greeting + time always play.
- **Phone → HA speakers (dial 46).** Record a short message and it plays out to
  your chosen media players (HomePod, Family Room Soundbar, Garage, …) via your
  local Piper TTS — an intercom from any handset to the whole house.

New config: `status_ext`/`announce_ext` (dial codes 45/46, collision-checked like
the others), `announce_players` + `announce_tts_engine`, and `wakeup_scene` /
`wakeup_weather` / `wakeup_calendar`. Feature settings are staged to an
asterisk-readable `/run/switchboard/features.json` (the AGIs run as the asterisk
user and can't read root-only `/data/options.json`). `ha_client` gains generic
`get_state` / `call_service` (allow-listed domains) / calendar / location helpers;
new `weather.py` (NWS), `ha_reports.py` (spoken read-outs), and a shared
`agi_speech.py` for the voice flows.

## 0.10.4

Make the wake-up UI clearer and less busy.

- **Wake-up list rows redesigned.** Each pending wake-up is now a clean card row:
  **room name** on the left, the **time + when it rings** (`6:00 AM · tomorrow`) on
  the right, then Cancel — instead of the old cramped `⏰ Name … 6:00 AM Cancel`
  with a big empty gap. The "when" (today / tomorrow / weekday) is new, so it's
  obvious when the call actually fires.
- **Card wake-up box is labelled.** The per-room time field gets a ⏰ prefix and a
  tooltip so it reads as "set a wake-up", not a stray empty box.
- **Friendlier empty state** that points to both ways to set one (the ⏰ box on a
  card, or dialing 42).

## 0.10.3

Tidy up the per-room cards on the dashboard.

- **Even button grid.** The action buttons were a wrapping flex row, so in narrow
  cards they stacked onto uneven lines (Test ring full-width, Connect + a lone icon
  wrapping, etc.). They're now a clean 2-column grid — every button the same width,
  aligned, with over-long labels ellipsised instead of blowing out the column.
- **No more mystery icons.** The bare-emoji buttons are fully labelled: `📵 Hang up`,
  `↪ Transfer`, and the message-waiting toggle reads `✉ Message` / `✉ Clear` (was a
  lone `✉`).
- **Roomier cards.** Bumped the card min-width 180→215px so the labelled controls
  fit and the grid shows fewer, wider cards per row.

## 0.10.2

Fix the Lights section being unreadable in dark mode.

- The room cards, the lights **area cards**, and the wake-up time input all paint
  their background from `var(--card)`, but the dark-mode override set `--card` only
  on `.card` — so the lights cards (`.areacard`) stayed **white**, with the page's
  light text on top → unreadable. Moved `--card` onto `body` in the dark block so
  every card-like surface inherits the dark value.
- Added a regression test asserting dark mode sets `--card` at a scope the lights
  cards inherit.

## 0.10.1

Fix the dashboard (GUI) going blank — a JavaScript syntax error blanked the whole
page.

- The transfer prompt was written `prompt('Transfer call to which room?\n' + ...)`
  in the Python source. The dashboard JS lives in a regular (non-raw) Python
  string, so that `\n` became a **real newline inside a single-quoted JS string
  literal** — a syntax error that aborted the entire inline `<script>`, so nothing
  rendered. Escaped it to `\\n` so the browser gets a proper `\n`. (Latent since
  the transfer button landed in 0.9.7; surfaced the first time the GUI was opened.)
- Added a regression test that parses the rendered dashboard `<script>` with
  `node --check`, so a bare newline in an embedded-JS string can't ship again.

## 0.10.0

Remove HD/Opus support entirely — Switchboard is now **G.711 µ-law only**, and the
codec is no longer configurable.

- **Removed the `codecs` option** (config + schema). Every endpoint — rooms and the
  trunk — is hard-pinned to `allow = ulaw` in the generated `pjsip.conf`, so no
  call can negotiate anything but G.711 µ-law and nothing ever transcodes.
- **Dropped the Opus codec** from the image build (no `asterisk-opus` package) and
  removed the `codec_allow` / `KNOWN_CODECS` / `DEFAULT_CODECS` machinery from the
  generator. Simpler and one-codec-clean, as intended.
- The per-call codec indicator on the dashboard/console stays — it now simply
  always reads "µ-law", a live confirmation that the pin is working.
- Docs updated (§9). Note: a phone must still *offer* G.711 µ-law (PCMU); a device
  configured to offer only a non-µ-law codec would have no common codec.

## 0.9.9

Default the whole system to **G.711 µ-law only** — no transcoding, anywhere.

- The shipped `codecs` default is now just `ulaw` (was `ulaw, alaw, g722, opus`),
  and the generator's fallback matches — so every room endpoint renders
  `allow = !all,ulaw`. Combined with the already-µ-law-only trunk, every call
  (analog FXS port, cordless, softphone, and the PSTN trunk) negotiates G.711
  µ-law with no transcode, regardless of what codec order a phone advertises —
  enforcement is server-side at the Asterisk endpoints.
- Extra codecs are not removed, just off by default: set the `codecs` option
  (e.g. `["ulaw", "g722"]`) to re-enable wideband for internal SIP-to-SIP calls.

## 0.9.8

Fix the voice **wake-up** (dial 42) and the dial-0 **MWI auto-clear**, which both
crashed instantly with a permission error.

- **Root cause.** The wake-up store (`/data/wakeups.json`) and MWI store
  (`/data/mwi.json`) live in `/data`, which only **root** can write — but the
  dial-42 wake-up AGI and the dialplan's `System(switchboard-mwi clear …)` run as
  the **`asterisk`** user (Asterisk drops privileges). So the very first store
  touch raised `EPERM` on the `.lock` file, the AGI's `except` set the result to
  "none", and the dialplan skipped straight to "no wake-up → goodbye → hang up"
  **with no pause to speak a time**. (Found in the add-on log: `[wakeup] fatal:
  [Errno 13] Permission denied: '/data/wakeups.json.lock'` and the matching
  `'/data/mwi.json.lock'`.)
- **Fix.** Both stores now live in a dedicated **`/data/state/`** directory created
  by the init step, owned by the `asterisk` user and **setgid + group-writable**, so
  the root services (scheduler, webui) and the asterisk-user processes can all
  read/write them. The lock + JSON files are pre-created group-writable, and each
  atomic write re-applies `0664`, so neither user can lock the other out across the
  flock + temp-file-rename. A pre-existing `/data/{wakeups,mwi}.json` is migrated in.
  `/data/options.json` (which holds the SIP secrets) stays root-only.
- **Defence in depth.** The wake-up AGI no longer aborts before recording if the
  store *read* hiccups — it degrades to the "say a time" prompt so the caller always
  gets their pause for input.

## 0.9.7

One-touch operator call transfer from the GUI dashboard and the TUI console.

- **Transfer an active call from the dashboard.** A room that's on a call now
  shows a ↪ Transfer button: pick a destination room and the *other* party (the
  outside caller, or whoever the room is talking to) is handed off there while
  the original handset drops out. Implemented as an AMI `Redirect` of the FAR
  leg into the `[rooms]` dialplan at the chosen extension — a blind transfer.
- **Transfer from the TUI too.** The console gains a `T` key mirroring the
  dashboard: press `T` on an on-call room, then pick a destination with ↑↓ and
  Enter (Esc cancels) — the same modal target-pick gesture as `C` Connect.
- **Guarded to room extensions only.** The transfer target is validated against
  the configured room set on both the API and the AMI engine, so a redirect can
  only ever land on a known room's `_X.` pattern — never the trunk's outbound
  `_9.` pattern (no transferring a call out to the PSTN). The channel name is
  CRLF-rejected before it reaches the manager socket, as with hang-up.
- **Picks the right party in a ring group.** When an inbound trunk call rings
  more than one room (e.g. cordless **and** iPhone), the call is one bridge with
  the outside leg plus a ringing leg per room. Transfer now always hands off the
  *outside* leg (preferring the trunk/answered leg, skipping a same-ext sibling)
  so it can never accidentally redirect a sibling ringing handset instead of the
  caller — and the redirected leg matches the "↔ Outside" label on the card.
- **Refuses a transfer to an offline room** (both UIs and the API), so a redirect
  can't silently drop the caller onto an unregistered extension.
- This complements the per-device transfer methods already available: analog
  FXS phones use the DTMF feature codes (`##`/`*2`) from v0.9.6, and SIP phones
  (cordless, iPhone) use their own native Transfer button (SIP REFER).

## 0.9.6

Inbound ring-group, analog call-transfer, and an AMI-churn fix from the audit.

- **Inbound ring group.** `trunk.inbound_ext` now accepts a comma-separated list
  (e.g. "19,20") so an incoming outside call can ring the cordless **and** the
  iPhone softphone together (Dial(PJSIP/19&PJSIP/20)). A single ext and empty
  (=all rooms) work exactly as before; a typo'd/non-room entry is dropped+logged,
  and a fully-invalid list falls back to ringing the whole house.
- **Analog call transfer (features.conf).** The FXS phones have no transfer
  button, so a generated features.conf gives them in-call DTMF transfer codes —
  blind `##`+ext, attended `*2`+ext — armed by the Dial t/T flags already in the
  dialplan. SIP phones (cordless, iPhone) keep using their own Transfer button
  (SIP REFER, native to chan_pjsip).
- **Codec read no longer multiplies AMI sessions during a call** (audit MEDIUM).
  `codecs_for_channels` previously opened one connect/login/logoff PER active
  channel on top of the status bundle — so a 2-leg call tripled the AMI logins
  every poll, re-introducing the churn v0.9.3 removed exactly when busiest. It now
  multiplexes all the codec Getvars over ONE login (ActionID-keyed), with a
  response-based terminator. Idle polls still do zero codec AMI work.

## 0.9.5

Show the active-call codec on the per-room tiles too (not just the calls list).

- The room card / console row for a phone on a call now appends its live codec —
  "↔ Outside · µ-law" — so you can see at a glance which codec each handset is on,
  with a transcode showing as a slashed value ("G.722/µ-law"). The codec was
  already carried in `by_ext`; this just surfaces it on the tile (`call_codec`).
  No new AMI work.

## 0.9.4

Show the live codec on active calls — so "is this call µ-law?" is verifiable.

- **Per-call codec on the dashboard and operator console.** Each active call now
  reads the codec its legs negotiated (via AMI `Getvar CHANNEL(audioreadformat)`)
  and shows it, e.g. "📞 Cordless ↔ Outside · Talking · µ-law". One value means
  no transcoding; two (e.g. "G.722/µ-law") reveals a transcode at a glance.
- Uses only the `call` privilege the AMI account already holds — NOT the
  deliberately-withheld `command`/CLI class, so the security boundary is unchanged.
- Read **only while a call is up** (no active channels → no extra AMI work), so
  the idle-poll churn reduction from 0.9.3 is preserved.
- `/api/status` calls (and the console board) gain a `codec` field; tests cover
  the Getvar value parse, the no-transcode vs transcode summary, the idle no-I/O
  path, and the CRLF/empty-channel injection guard.

## 0.9.3

Quiet the Asterisk manager log; cut AMI connection churn.

- **One AMI session per status poll instead of three.** The dashboard and the
  operator console each read endpoints + contacts + channels every refresh, and
  each `get_*` opened its own connect→login→logoff cycle — so a steady stream of
  "Manager 'switchboard' logged on/off" / `SuccessfulAuth` events filled the log
  (~8 every 1.5s). New `ami.get_status_bundle()` runs all three list actions over
  a single connection (one login, one logoff), with the read terminated only once
  every action's own `...Complete` has arrived (matched by ActionID, so a spoofed
  field value or an unrelated action can't end it early). The web `/api/status`
  and the console poller both use it.
- **Console board poll slowed 1.5s → 3s.** Registration/call state changes on the
  order of seconds and operator actions refresh immediately, so this is invisible
  in use but roughly halves the remaining poll rate. Net effect ≈ 6× fewer AMI
  connections.
- No behavior change to the dashboard, console, MWI, paging, or originate paths;
  the stateless `/run/switchboard/ami.env` fallback for dialplan-spawned consumers
  is untouched.

## 0.9.2

Fix intermittent outbound "Service Unavailable" on the SIP trunk.

- **Stop qualifying the trunk's static contact.** VoIP.ms does not reliably answer
  OPTIONS keep-alives, so Asterisk's qualify would flap the trunk contact to
  "Unavailable" — and PJSIP then refuses to route outbound calls to it, so
  `Dial(...@trunk)` fails with **503 "Service Unavailable"** even though the
  registration (and therefore *inbound* calling) stays perfectly healthy. The
  trunk AOR now sets `qualify_frequency = 0`; inbound liveness is covered by the
  periodic re-REGISTER instead. Room AORs still qualify (LAN ATAs answer OPTIONS
  fine) — this is a trunk-only change.

## 0.9.1

Fix outbound calling on the SIP trunk (regression caught on 0.9.0's first live use).

- **Outbound dialplan was emitted into the wrong context.** The `_9.` outbound
  rule and the toll-fraud blocks were appended after the feature contexts, so they
  landed in `[automation]` instead of `[rooms]`. With no `_9.` in `[rooms]`, a
  dialed outside number fell through the catch-all `_X.` room pattern, didn't match
  a known room, and hit `Congestion` — which phones report as **"Service
  Unavailable"**, and no call ever reached the carrier. Outbound rules now render
  inside `[rooms]`, where the literal-`9` patterns out-prioritize `_X.`; inbound
  stays in its own `[from-trunk]` context. Inbound calling was unaffected.
- Tests now assert the *context* each dialplan rule lives in (not just that it
  exists), so this can't regress silently.

## 0.9.0

Outside-line (SIP trunk) refinements for clean, low-latency PSTN calls.

- **Trunk pinned to G.711 µ-law.** The outside line is the PSTN — always
  narrowband — so the trunk endpoint now advertises `ulaw` only (`disallow=all`).
  The provider can no longer negotiate a wideband codec and force a transcode
  against the analog FXS phones (which only adds latency, never quality). HD
  codecs (G.722/Opus) stay available for internal SIP-to-SIP calls between the
  cordless/desk phones.
- **Configurable inbound destination.** New `trunk.inbound_ext` routes an
  incoming call to a single room (e.g. the cordless phone) instead of ringing the
  whole house. Empty (default) keeps the ring-everyone behavior; an ext that
  isn't a configured room is ignored (rings all) and logged.

## 0.8.3

MWI stutter tone now works — switch from `res_mwi_external` to `PJSIPNotify`.
Live testing proved `res_mwi_external` (the `MWIUpdate` action) is **not built
into the Alpine Asterisk package** ("Invalid/unknown command: MWIUpdate"). The
message-waiting indicator is now delivered the portable way: `ami.set_mwi` sends
an unsolicited `message-summary` NOTIFY to the room's contact via **`PJSIPNotify`**
(`res_pjsip_notify`, part of the core PJSIP stack), using on/off templates
generated into `pjsip_notify.conf`. Endpoints no longer carry `mailboxes=`
(unused without res_mwi_external); `modules.conf` loads `res_pjsip_notify` instead.
The Grandstream still needs its "MWI → stutter tone" port setting for the audible
tone (DOCS §4.2).

## 0.8.1

Fix the message-waiting (MWI) stutter tone, found by live testing v0.8.0.
`MWIUpdate` was rejected because Asterisk's `res_mwi_external` **declines to load
while `app_voicemail` is loaded** (they both own a mailbox's MWI), and the stock
autoload loads `app_voicemail`. The add-on now generates `modules.conf` that
noloads the voicemail apps (we run no voicemail) and explicitly loads
`res_mwi_external` + `res_mwi_external_ami` (and `app_confbridge`/`app_page` for
the page intercom). No other behavior change.

## 0.8.0

Operator superpowers — voice home-automation, a full-featured web dashboard,
a house-wide page intercom, and message-waiting stutter tones.

- **Control your lights by voice.** Dial **0** and say "automation" (or dial
  **43**) → say a room → say a light → hear its state → say "turn it on/off".
  It reads the live state and toggles it through Home Assistant. Offline
  throughout: whisper for listening, **espeak-ng** for speaking the light names,
  state and lists (no cloud, no canned-prompt-per-light). The add-on now uses
  the Home Assistant Core API (`homeassistant_api: true`) via the Supervisor
  proxy with its own token — no separate credential.
- **The web dashboard caught up to the console.** Each room card can now
  **connect** two rooms, **hang up** a call, **set/cancel a wake-up**, and toggle
  a **message-waiting** indicator; plus a **Page all** button and a **Lights**
  panel (grouped by room, on/off toggles). The operator console (telnet/browser
  TUI) gained the matching **P** page-all, **M** message, and **L** lights keys.
- **Page all — a house-wide intercom.** Press **P** in the console / **Page all**
  in the dashboard (or dial **44** from any phone): every phone rings and whoever
  answers joins one shared intercom (Asterisk ConfBridge / `Page`).
- **"You have a message" stutter tone (MWI).** The operator can flag a room
  (TUI **M** / dashboard ✉) so its phone gives the classic **stutter dial tone** —
  "call the operator". It **clears automatically** when that room dials 0, and the
  ✉ badge persists across restarts (re-asserted on startup). Requires the
  Grandstream's "MWI → stutter tone" setting (see DOCS §; one-time per port).
- New options: `automation_enabled`/`automation_ext` (43), `page_enabled`/
  `page_ext` (44), `mwi_enabled`. No change to existing room/trunk config.
- New shared modules (`ha_client`, `mwi_store`) + CLIs (`switchboard-tts`,
  `switchboard-mwi`); all pure logic unit-tested (suite 411 → 495 checks).
  Built as five parallel workstreams, then hardened by a five-dimension
  adversarial review (10 findings fixed, incl. restart MWI-replay, the offline
  "Unassigned"-area voice path, and operator-answer latency).

## 0.7.0

Bigger, centered board — the operator console no longer sits jammed in the
top-left of a large terminal.

- **Larger text in the browser terminal:** the xterm.js font goes 14 → 18px, so
  the board reads comfortably on a full-size screen / the HA sidebar panel.
- **Centered board:** the roster is small (8 rooms), so on a wide terminal it
  used to float in the top-left with a big empty void. `render` now centers the
  whole board — horizontal indent + vertical padding sized from the terminal's
  NAWS dimensions — so it sits balanced with even margins. Falls back to no
  padding on a terminal too small to center into (never pushes content
  off-screen). Helps the telnet console too, not just the browser.
- New pure helpers `vis_width()` (ANSI-stripping, wide-glyph-aware column count)
  and `center()`, both unit-tested (test_console.py: 64 checks).

## 0.6.1

Cosmetic: the wake-up entry hint read `⌫ deletes`, but the backspace glyph
(U+232B) has no character in the browser terminal's font and rendered as a
circled-×. Replaced it with plain text — `Backspace deletes`. Caught by
in-browser testing; no behavior change.

## 0.6.0

Set wake-up calls right from the operator console (telnet + browser) — plus a
help overlay and a live time preview.

- **Set a wake-up in the TUI:** select a room and press **W**, then *type* a
  time — `7:30`, `quarter past six`, `0730`, `noon`, `nineteen thirty` (the same
  forgiving parser the dial-42 voice flow uses, so the two paths can never
  disagree). Enter sets it; the board reads back the 12-hour time and whether
  it's today or tomorrow. Press **W** on a room that already has a wake-up to
  edit it (its time is pre-filled); **X** still cancels. Esc aborts with nothing
  written.
- **Live preview while typing:** as you type, the prompt shows the parser's
  reading (`→ 7:30 AM`) so a mistyped time is obvious before you commit.
- **Help overlay:** press **?** for a one-screen key reference.
- This is the TUI's first text-entry mode, which needed two small enabling
  fixes: `parse_input` now recognizes Backspace/Delete (the web terminal sends
  `0x7f`), and **q**/**Q** only quit from the board — a literal `q` while typing
  a time (e.g. "quarter") stays text (Ctrl-C is always a hard exit).
- No new options, services, or dependencies. Wake-ups set in the TUI are
  delivered by the existing scheduler exactly like voice-set ones.

## 0.5.0

Operator console in the browser — a sidebar web terminal.

- **New `console-web` service** serves the existing operator console TUI in a
  browser via **xterm.js** (vendored offline, no CDN). It's a tiny stdlib-only
  HTTP + WebSocket server (no new pip deps; the add-on is musl) that bridges your
  browser to the telnet operator console on the host: WebSocket ⇄ telnet,
  answering/stripping the console's IAC negotiation so only clean ANSI reaches
  the page, forwarding keystrokes, and mapping the terminal's resize to a telnet
  NAWS subnegotiation. Reachable on the LAN at `http://<ha-host>:8100/`.
- **Add it to the Home Assistant sidebar** with a `panel_iframe` ("Switchboard
  TUI") — see DOCS §7. The Ingress UI (`:8099`) is unchanged.
- New options `console_web_enabled` (default true) and `console_web_port`
  (default 8100). The web terminal idles if the operator console is disabled.
- Same LAN-trust posture as the telnet console (unauthenticated; can
  ring/connect/hang up). Session-capped, and turn-off-able via the new option.

## 0.4.1

Old-style speaking clock. The talking clock (dial 41) now announces "At the
tone, the time will be …" followed by a clean 1 kHz pip — the classic
speaking-clock cadence — instead of a plain "The time is …". New `sw-at-the-tone`
prompt + a generated `sw-tone` pip.

## 0.4.0

Wake-up calls — set by voice, delivered on schedule.

- **Request by voice:** dial **42** (`wakeup_ext`) and *say* the time — "seven
  a.m.", "six thirty", "quarter past seven", "noon". Rotary phones can't key in
  digits mid-call, so it uses the same offline whisper STT as the operator, with
  a forgiving spoken-time parser; it reads the time back to confirm. Say a new
  time to change it, or "cancel" to clear it.
- **Delivery:** a new `wakeup-scheduler` service rings the room at the set time
  (AMI Originate into a `[wakeup-deliver]` dialplan) and speaks "Good morning,
  this is your wake-up call, the time is …". One-shot, with a grace window so a
  brief outage can't fire a stale wake-up at the wrong hour. `wakeup_ring_seconds`
  controls how long it rings (default 60).
- **See & cancel anywhere:** pending wake-ups show on the web dashboard and the
  telnet console, each cancelable there (or by dialing 42 and saying "cancel").
- Pure, unit-tested cores (`wakeup/timeparse.py`, `wakeup/store.py`) +
  `tests/test_wakeup.py`. Stored in `/data/wakeups.json` (survives restarts).
- Timezone auto-detect now also tries the Supervisor IP (`172.30.32.2`) so it
  works on this host-network add-on (otherwise set `timezone` explicitly).

## 0.3.1

Talking clock + a real local timezone.

- **Talking clock:** dial **41** (configurable `clock_ext`) and hear the current
  local time — "The time is eight oh five p.m." Uses Asterisk `SayUnixTime`.
- **Local timezone:** the add-on container runs in UTC, which made the console
  clock (and anything time-based) wrong. The init step now resolves a zone —
  explicit `timezone` option, else the Home Assistant timezone (auto-detected via
  the Supervisor), else UTC — and points `/etc/localtime` at it, so Asterisk and
  the operator console both read local time. `tzdata` is now bundled.
- **Core sounds:** Asterisk's core English sound files (digits + time words) are
  now included — needed by `SayUnixTime`/`SayNumber` (and the upcoming wake-up
  calls). µ-law, matching the analog path.
- New options: `timezone`, `clock_enabled`, `clock_ext`. Lays the groundwork for
  wake-up calls (v0.4.0).

## 0.3.0

Add a **telnet switchboard operator console** — a live TUI for working the
board like a cord-board operator.

- **Connect over telnet:** `telnet <host> <port>` (default **2300**,
  `console_port` / `console_enabled` options). A raw-TCP ANSI TUI — no client
  install, works from any terminal.
- **Live board:** every room with real-time status — ● Registered, ○ Offline,
  ◐ Ringing, ◉ On call ↔ *peer* — plus an Active-calls panel ("Kitchen ↔
  Office · 02:14"), refreshed ~1.5 s.
- **Operator actions:** **R** ring/page the selected room, **C** connect two
  rooms (rings A, then dials B via the room dialplan), **H** hang up the
  selected room's call, **↑↓ / j k** select, **Q** quit. Connect/ring/hangup go
  through the same room-validated AMI helpers as the web button, so the console
  can't place an outside call.
- Implemented in Python stdlib (telnet IAC negotiation, NAWS resize,
  frame-hash anti-flicker, alt-screen) as a new s6 `operator-console` service,
  reusing the `webui/ami.py` engine. New `connect_extensions` / `hangup_channel`
  AMI helpers + `tests/test_console.py`.
- **Security note:** like the EcoFlow telnet console, this is **unauthenticated
  on the LAN** and performs call-control (ring/connect/hang up) — so it assumes a
  trusted home network. It's hardened to that scope: connect is validated against
  the configured room set (never the trunk's outbound pattern, even with a trunk
  enabled), sessions are capped (5) and idle-timed-out (15 min), and the bind is
  configurable via `console_bind` (set `127.0.0.1` to keep it host-local, or
  `console_enabled: false` to turn it off).

## 0.2.8

Make the dashboard interactive and call-aware.

- **Test-ring button per room:** each phone card has a 🔔 **Test ring** that
  places a one-cycle ring to that extension (AMI `Originate` → a short
  `sw-test` prompt if you pick up). The button is disabled for offline phones
  and shows "Ringing…". The originate is constrained server-side to ringing a
  *known room ext* with a fixed `Playback` — it can never dial an outside line —
  and the AMI account's new `originate` privilege is paired with `system` for
  that (still no `command`/CLI).
- **Readable call details:** the "Active calls" list now shows who's talking to
  whom by room name — "Kitchen ↔ Office", "Garage ↔ Outside (+1…)", or
  "Kitchen → Operator" — with state (Ringing/Talking) and duration, grouped per
  call via Linkedid instead of dumping raw channel names.
- **Per-card "talking to":** a busy room's card shows its current peer
  (↔ Office / ↔ Outside / ↔ Operator) and the live call state.
- New `POST /api/ring/{ext}` (Ingress-only, validates the ext); new
  `summarize_calls`/`channel_ext` helpers + test coverage in `tests/test_webui.py`.

## 0.2.7

Actually fix the dashboard showing every registered phone as "Offline" (the
0.2.6 read-until-Complete change addressed the wrong layer).

- **Root cause (diagnosed live):** `PJSIPShowEndpoints` parsing was fine —
  `/api/status` already returned `DeviceState: "Not in use"` for all 8 rooms.
  The failure was isolated to two places: (1) `registered` was derived *only*
  from a `PJSIPShowContacts` match that never landed because the AMI client read
  fields with the wrong casing (`AOR`/`URI`/`Status` vs Asterisk's `Aor`/`Uri`),
  so every contact keyed on `""`; and (2) the browser pill used
  `device_state.includes('use')`, which matches `"Not in use"` and painted idle
  phones orange.
- **Case-insensitive AMI parse:** `_ami_command` now lower-cases every response
  key, so no caller can be broken by Asterisk's inconsistent field casing again
  (applies to endpoints, contacts, and channels at once).
- **Registration from device state:** a PJSIP endpoint reads `Unavailable` with
  no reachable contact and `Not in use` once one binds — so `registered` is now
  taken from `DeviceState` (the signal Asterisk already aggregates), with
  contact reachability as a secondary confirm. The per-contact row is enrichment
  (status text + RTT) only. Contacts are keyed by `aor`/`endpointname` with an
  `objectname` fallback so a renamed field can't silently drop them.
- **Pill fix:** `"Not in use"` → green **Registered**; only an active call state
  (`In use`/`Ringing`/`Busy`/`On Hold`) → orange; otherwise red **Offline**.
- **Contacts keyed correctly:** the `ContactList` event has no `Aor` field — the
  endpoint identity is its `Endpoint` field — so RTT and real contact status now
  populate instead of silently dropping (an adversarial review caught that the
  prior keying only worked by `ObjectName` accident).
- **Auth failures are now visible:** a wrong/rotated AMI secret previously read
  as `ami_ok=true` with every phone "Offline" and no banner — indistinguishable
  from a real outage. A failed AMI login now surfaces the "cannot reach Asterisk
  Manager" banner.
- **Hardened stream read:** the list terminator is matched on a real
  `Event: …Complete` line (not a bare substring), so an attacker-influenced field
  value — an inbound trunk `CallerIDName` or a phone `UserAgent` containing
  "Complete" — can't truncate the live view; plus an upper bound on the buffer.
- **Testability:** the AMI client moved to a framework-free `webui/ami.py` and
  gained a plain-`python3` test suite (`tests/test_webui.py`) covering the
  casing, ContactList identity, DeviceState registration, terminator, and
  auth-failure paths — the regression net that was missing.

## 0.2.6

Fix the Ingress dashboard always showing rooms as "Offline".

- **AMI event truncation:** the web UI sent `Login → action → Logoff`
  back-to-back, but `PJSIPShowContacts`/`PJSIPShowEndpoints` stream their
  results as async events ending in a "...Complete" event. Sending `Logoff`
  immediately made Asterisk close the socket before the events finished, so the
  contact list arrived empty and every room read "Unregistered" even when fully
  registered. Now reads until the action's "...Complete" event, then logs off.

## 0.2.5

Engaged-line handling + end-of-call tone, from live testing.

- **Instant busy on an engaged line:** the operator now checks the room's
  `DEVICE_STATE` before dialing — if it's already on a call, it plays "That line
  is busy" immediately instead of dialing (which made the gateway *call-waiting-
  ring* the busy line, so the caller heard rings then a delayed message). No
  gateway change needed.
- **End-of-call tone:** every operator hangup path (and the end of a connected
  call, when the far end hangs up) now plays a short two-tone cue (`sw-endtone`)
  so a caller on an antique handset hears that the line is down.

## 0.2.4

Operator now tells the caller *why* a connection didn't complete.

- **Busy / no-answer / unavailable handling:** when the operator dialed a room
  that was busy, didn't answer, or wasn't registered, the dialplan fell through
  to a bland "Goodbye" — indistinguishable outcomes. Now it branches on
  `${DIALSTATUS}` and plays a spoken status: "That line is busy…",
  "There's no answer…", or "That room isn't available right now." (new prompts
  `sw-busy`, `sw-noanswer`, `sw-unavailable`).
- **Observability:** Asterisk now runs at `-vvv`, so the log shows dial outcomes
  (`Operator dial <ext> -> <DIALSTATUS>`) and full call tracing — call volume on
  a home PBX is low, so the extra verbosity is worth the diagnosability.

## 0.2.3

Tune the operator from the first real on-Pi calls (whisper.cpp recognized
"Kitchen", "Living Room", and a full sentence at 1.0 — these are polish fixes).

- **Clipped-word recognition (prefix match):** the narrowband line drops soft
  word tails, so whisper heard "Basement" as "Base." The matcher scored that an
  ambiguous tie (Basement vs incidental fuzzy overlap with Master Bedroom) and
  refused to connect. Added a word-prefix bonus so a heard word that's a clean
  prefix of a room name wins decisively ("Base"->Basement, "Din"->Dining).
- **Double "Goodbye":** on a no-match the AGI prompt already ended with
  "Goodbye" and the dialplan played another — dropped it from
  `sw-no-such-room` ("Sorry, I couldn't reach that room.") so there's one.
- **Log noise:** silence the `audioop` DeprecationWarning the STT wrapper
  emitted on every call.

## 0.2.2

Fix the voice operator never recording (no pause to speak, prompts running
together).

- **`RECORD FILE` aborted before recording:** the AGI passed an offset arg and
  `BEEP` to `RECORD FILE`. The minimal Alpine `asterisk-sounds` has no built-in
  `beep` file, and — worse — the offset positional makes `res_agi` treat it as a
  beep request and abort the record, so no audio file was ever written. The STT
  wrapper then ran on a nonexistent file and both retry prompts played
  back-to-back with no chance to speak. Fixed: record as
  `RECORD FILE … <timeout> s=<silence>` (no offset, no `BEEP`), and play a
  bundled `sw-beep` "speak now" cue instead of the absent system beep. Also
  bumped the record window to 7 s / 3 s trailing-silence for slower speakers.

## 0.2.1

Fix an Asterisk crash-loop introduced in 0.2.0.

- **astdb ownership (regression fix):** the operator added files under
  `rootfs/var/lib/asterisk/` (the AGI + prompt audio), and `COPY rootfs /` reset
  `/var/lib/asterisk` to root-owned. Asterisk runs as the `asterisk` user and
  could no longer create `astdb.sqlite3` there (`ASTdb initialization failed —
  ASTERISK EXITING`), crash-looping with 0 phones registered. The asterisk
  service's startup chown now covers `/var/lib/asterisk` alongside run/log.

## 0.2.0

Add a **voice operator** — dial `0`, say a room name, get connected.

- **Why:** rotary/pulse antique phones can't drive DTMF menus (no `*`/`#`), so
  voice is the natural interface. Dial `0`, the operator greets you, you say the
  room ("Kitchen", "the study"), and it connects the call.
- **Fully offline.** Speech recognition is **whisper.cpp**, built from source in
  the image (Vosk was evaluated first but ships only glibc wheels — no musl/apk —
  so it can't run on the Alpine base). No cloud, nothing leaves the house.
- **Architecture:** dial `0` → `[operator]` dialplan context → a stdlib Python
  **AGI** that plays prompts, records the caller, and shells out to
  `switchboard-stt` (the only component that touches whisper). The AGI sets
  channel vars; the **dialplan does the Dial, and only to a known room ext** —
  so a recognizer error can never dial an arbitrary endpoint. The recognizer is
  biased toward your room names and a fuzzy matcher resolves near-misses; a
  near-tie between two rooms re-prompts rather than guess.
- **New options:** `operator.enabled` (default true) and `operator_synonyms`
  (extra spoken names per room, e.g. "office"/"den" → the study).
- Prompts are pre-recorded audio (no runtime TTS dependency). Build is
  multi-stage so the C++ toolchain doesn't ship in the final image.

## 0.1.6

Fix an AMI regression from v0.1.4.

- **AMI privileges (regression fix):** v0.1.4 emptied the manager `write` classes
  on the assumption the Ingress UI was read-only. But Asterisk gates the UI's
  status actions (`PJSIPShowEndpoints`, `PJSIPShowContacts`, `CoreShowChannels`)
  on *write* authority, so every poll was denied (`RequestNotAllowed` in the
  log) and the dashboard could never show a phone as registered. Restore the
  minimum needed (`write = system,call,reporting`) while still excluding the
  dangerous `command` (CLI/RCE) and `originate` (place-calls) classes — keeping
  the least-privilege intent without breaking status.

## 0.1.5

Close the two deferred high-severity items from the v0.1.4 review.

- **Ingress access control (H1):** the web UI is host-network-exposed, so its
  port was reachable directly on the LAN, bypassing Home Assistant Ingress auth
  (the `/api/status` roster/call data leaked). Per the add-on docs, the app now
  rejects any client other than the Supervisor (`172.30.32.2`, plus loopback)
  with `403`. The bind is unchanged, so Ingress is unaffected.
- **Toll-fraud guard (H2):** the outbound trunk dialplan now denies
  international (`011`) and premium-rate (`900` / `1-900`) destinations before
  the general outbound rule. Normal dialing is unchanged. (Trunk is still off by
  default.)

## 0.1.4

Security & robustness hardening of the config generator and Ingress UI
(hardening only — no feature changes; for valid inputs the only generated-config
change is the AMI least-privilege tightening below).

- **Input validation / config-injection defense:** room names, secrets, and all
  trunk fields are now scrubbed of control characters before being written into
  `pjsip.conf` / `extensions.conf`; entries that can't be made safe are skipped
  with a log instead of corrupting the config. `dial_prefix` and
  `outbound_caller_id` are charset-validated.
- **Rooms validated once (`valid_rooms`)** and shared by both renderers: dedupes
  colliding extensions, enforces the 2–6 digit ext rule, and keeps `pjsip.conf`
  and the dialplan in sync. Warns when zero valid rooms remain.
- **No secrets in logs:** the skip log no longer prints the room dict (which
  contained the plaintext secret) — only the extension.
- **Least-privilege AMI:** the Ingress UI's manager account drops all write
  classes (was `system,call,command,reporting,originate`); it only ever reads.
- **Robust parsing:** a malformed `options.json` and non-numeric port/RTP values
  no longer crash the init oneshot (which would take the whole add-on down);
  they fall back to defaults with a clear log.
- **Web UI XSS fix:** room labels and AMI caller-ID (attacker-controlled on
  inbound trunk calls) are HTML-escaped before rendering; AMI errors are
  reported to the browser as a generic message and logged server-side.
- **Dialplan correctness:** multi-character `dial_prefix` now strips the whole
  prefix (`${EXTEN:N}`) instead of a single character.

## 0.1.3

Log cleanup (cosmetic — the add-on already runs).

- Add a curated `modules.conf` that autoloads everything except the modules
  that only probe for hardware that isn't present in a container: `chan_alsa`
  and `chan_console` (the ALSA/JACK error spam), `chan_dahdi`, and a few
  deprecated ADSI / unused SQLite CDR-CEL backends. Cleaner logs, less memory.

## 0.1.2

Startup fix.

- Rework the AppArmor profile. The previous strict path allowlist blocked the
  s6-overlay init (`/bin/sh: can't open '/init': Permission denied`, crash
  loop). The profile now follows the documented HA add-on pattern: broad
  file/capability/signal/network access under a named, mediated profile, with
  explicit exec rules for the s6-overlay boot chain.

## 0.1.1

Build fix.

- Drop the non-existent `asterisk-pjsip` and `asterisk-sounds-en` Alpine
  packages that broke the image build. PJSIP ships inside the main `asterisk`
  package; `asterisk-sample-config` provides modules.conf so it autoloads.
- Music-on-hold sounds and the Opus codec are now installed best-effort, so a
  missing optional package can never fail the build.
- Invalid-extension handling uses a generated congestion tone instead of a
  prompt sound file (no sounds package required for core calling).

## 0.1.0

Initial release.

- Asterisk 21 + PJSIP packaged as a Home Assistant add-on (Ingress + AppArmor).
- Room-to-room calling: each `rooms` entry becomes a PJSIP endpoint for one
  Grandstream GXW4216 V2 FXS port.
- Configurable codecs (`codecs` option). Default prefers G.711 µ-law for the
  analog path and also offers G.722 and **Opus** for HD-capable / IP endpoints.
  Opus codec module installed best-effort.
- Config generated from add-on options on every start (`switchboard-config`).
- Ingress web UI showing per-room registration and active calls (FastAPI + AMI).
- SIP trunk support (disabled by default): outbound via a dial prefix, inbound
  rings all rooms.
