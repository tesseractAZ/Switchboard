# The voice operator & directory assistance

All voice features share the same rotary-safe shape: a prompt plays, you speak one
short phrase after the beep, recognition runs **on-box** (whisper.cpp), and the
system acts. No keypress is ever required, and nothing connects a call or toggles a
light on a low-confidence guess — it asks again instead. Speech never leaves the
box.

## Operator — dial `0`

Say what you want:

- **A room name** ("Kitchen", "the study") or a **spoken extension** ("one four")
  — you're connected to that room. Add aliases with `operator_synonyms`.
- **"Wake me up"** — jumps to the wake-up flow ([Wake-up calls](wake-ups-and-clock.md#wake-up-calls--dial-42)).
- **"Lights"** / **"automation"** — jumps to the home-automation menu (below).
- **"What time is it"** — jumps to the talking clock ([Talking clock](wake-ups-and-clock.md#talking-clock--dial-41)).
- **"Weather"** / **"power"** / **"battery"** / **"house status"** — jumps to
  dial-a-status (below).
- **"Directory"** / **"who's here"** / **"list the rooms"** — jumps to directory
  assistance (below).
- **"Announce"** / **"over the speakers"** — jumps to announce ([Announcements](paging-and-announcements.md#announcements--dial-46-phone--home-assistant-speakers)).
- **"Page everyone"** / **"intercom"** / **"all call"** — jumps to page-all ([Page all](paging-and-announcements.md#page-all--dial-44)).

A **confident** room match wins over a feature word — an exact name, a configured
synonym, a full-word hit, or a recognizer-clipped prefix (all score ≥ 0.9) — so a
handset named "Office", "Garage", even "Weather", still connects normally. A
weaker, merely-fuzzy room match does *not* win: the feature word takes it. That
ordering exists because neither pure precedence is right — a spoken "page" scores
0.67 against a room named "Garage", clearing the 0.6 room threshold, so
room-first would send the caller to the Garage phone instead of the intercom.
The two exceptions sit *ahead* of room matching entirely: "wake me up" and
"lights"/"automation" are tested first, so a room you literally name "Lights"
would be shadowed by the automation flow.

A feature you name but have disabled falls through to a polite goodbye. If it
can't understand you after two tries, it says so and hangs up. Room-name matching
is deliberately forgiving of narrowband tail-clipping ("Base" → "Basement"), and
refuses to guess between two similar names.

Dialing `0` also **clears the caller's own message-waiting indicator** — the
stutter tone means "call the operator", and they just did. It is cleared after
the call is answered and in the background, so it never delays the greeting. It
applies only to a call from a configured room's own phone, identified by the PJSIP
endpoint configuration assigns, never by caller ID, which a caller can set. So an
outside caller transferred to `0` is skipped, and so is a `*2` consult to `0`
(dial `0` directly instead). It also applies only while `mwi_enabled` is on.

**House features are for the room phones** (since 0.110.0). Lights, dial-a-status,
announce and page act on the house, so the operator hands them only to a room
phone that dialled `0` itself. An outside caller transferred to the operator, or a
room's `*2` consult to `0`, can still be connected to a room, use the directory,
hear the clock or set a wake-up; asked for anything else, they hear the operator's
goodbye. The same check runs again when each of those features starts, including
when it is dialled by its own code.

## Directory assistance — dial `411`

Say a room name to be connected, or say **"list"** to hear every room and its
extension read out. Resolution is ordered so a mishearing degrades safely:

1. an **unambiguous** room match (score ≥ 0.9 — exact name, synonym, or a
   recognizer-clipped prefix) connects outright, so a room genuinely called
   "List" or "Cancel" is still reachable by name;
2. otherwise a cancel word ("goodbye", "never mind", "stop") ends the call;
3. otherwise a fuzzy "list" reads the directory — the narrowband line turns
   *list* into *lift* / *least* / *last*, and those used to clear the room
   threshold and dial a bedroom;
4. otherwise a **weaker but above-threshold** room match connects.

So it does connect on a less-than-confident match — step 4 is the point of the
service. What it will not do is connect on a name the matcher rejected outright
or found ambiguous between two similar rooms; those re-prompt. It speaks the room
name and extension before connecting, which is your cue to hang up if it guessed
wrong. Saying "list" doesn't burn a retry (capped at three read-outs); three
unresolved replies end the call.

## Home-automation voice menu — dial `43` (or say "lights" to the operator)

A guided flow: it asks for a room, then a light in that room, tells you whether
it's on or off, and asks you to say "turn on", "turn off", or "cancel". It reads
your lights and areas live from Home Assistant. Say "list" at any step to hear the
options. Lights with no assigned area appear under "Unassigned".

## Dial-a-status — dial `45`

A looping voice menu that speaks live Home Assistant readings on demand. Say:

- **"power"** (or battery / solar / grid) — your power/battery status.
- **"weather"** — a short local forecast (from the U.S. National Weather Service,
  using your Home Assistant location).
- **"house"** — thermostats and a light count.

It re-asks "anything else?" after each answer, but the loop is bounded: it stops
after **8 answers**, and it gives up (with "okay, goodbye") as soon as **two
replies in a row** go unrecognized at the same prompt. Say "goodbye" to leave
early. The cap is a backstop against a noisy or wedged line holding a channel
open indefinitely.

## Local voice assistant — dial `47`

**Off by default.** Set `assistant_enabled: true` to switch it on.

Dial `47`, say a command or a question in plain language, and hear the answer.
Unlike the [home-automation menu](#home-automation-voice-menu--dial-43-or-say-lights-to-the-operator) (dial `43`) there is no guided room →
light → action script: you say the whole thing at once, and anything Home
Assistant's own voice assistant understands works here too.

It re-asks "anything else?" after each answer. The loop is bounded: it stops
after **5 recordings**, and it gives up as soon as **two turns in a row** are
silent or unintelligible. Say "goodbye", "cancel", "never mind", "that's all"
or "thanks" to leave early — and, since v0.82.0, simply **"no"** works too, along
with "nope", "nah", "no thanks" and "that's it". That is the natural answer to
"anything else?", and until then it was sent to Home Assistant, matched nothing,
and came back "Sorry, I couldn't understand that". A decline only ends the call
when it is the *whole* utterance, so "no, turn on the kitchen light" is still a
command. The same whole-utterance rule applies to the other endings, so
"turn off the porch light" and "stop the music" are treated as commands, not
as hang-ups.

**Nothing on this path leaves the machine.** The recording is transcribed by
this add-on's own speech recognition, the text goes to Home Assistant's
built-in `conversation.home_assistant` agent, and the reply is spoken by this
add-on's own voice. No cloud speech-to-text, no cloud text-to-speech, no cloud
conversation agent — an internet outage does not affect it. (Home Assistant
Cloud's assistant is *not* used even if you have a subscription.)

**Where it leaves — and does not leave — a record.** "Nothing leaves the
machine" is a statement about the network. It is not, on its own, a statement
about what stays behind, so here is that separately.

Each turn writes one line to `/data/state/assistant.jsonl` inside the add-on:
what was heard, what was replied, how long recognition, Home Assistant and the
voice took, and — when a turn produced nothing — which of the several possible
reasons applied. It holds the last 400 turns and then discards the oldest.

That file is inside the add-on's private data, not the shared folder. It cannot
be read from a file browser, a Samba share, or another add-on; the container
shell is closed while protection mode is on, and add-on backups are encrypted.
It is also never copied to `/share`, which the rest of this add-on uses
precisely *because* that directory is readable from outside — the call-quality
and delivery ledgers live there for that reason, and this one deliberately does
not. Nothing spoken is written to either `asterisk.log`.

The add-on's log is a different matter. Each voice feature's recognizer writes a
line there for everything it hears, and the assistant adds one per turn. That log
is readable by Home Assistant administrators through the Supervisor, and it is
kept for about two days, across a restart of the host. With
`assistant_transcripts` on, those lines quote the words; with it off, they give
only the outcome and the length.

Set `assistant_transcripts: false` to keep the diagnostics without the words.
Every timing, outcome and failure reason is still recorded, in the file and in
the add-on's log; only what was said is withheld, replaced by its length. The switch is meant to cost privacy, not
visibility — a setting that also made the feature undebuggable would just be
left on.

To read it you need host-level access to the container — which is exactly the
access the paragraph above says an ordinary reader does not have, and both
statements are meant to be true at once. The file is out of reach of a file
browser, a Samba share or another add-on; it is not out of reach of whoever
administers the machine. With the add-on's protection mode turned off and a
shell on the host:

```
sudo docker exec addon_<slug> tail -n 20 /data/state/assistant.jsonl
```

A turn that worked looks like this (wrapped for print):

```json
{"v":1,"ts":1788700000,"outcome":"answered","chan":"PJSIP/19-0000000c",
 "call":"1788700000.42","ext":"19","turn":1,
 "heard":"turn on the kitchen lights","reply":"Turned on 2 lights",
 "response_type":"action_done","stt_ms":1840,"converse_ms":410,"tts_ms":900,
 "turn_ms":9200,"rec_ms":4300,"rec_end":"timeout","wav_bytes":68844}
```

`outcome` is the field to read first:

| `outcome` | What happened |
| --- | --- |
| `answered` | A command was heard, sent to Home Assistant, and the reply spoken. |
| `bias-failed` | The recogniser's hint list could not be built. The call continued with a smaller one. |
| `ended-max-turns` | Five recordings were used without a goodbye. |
| `ended-unheard` | Two turns in a row produced nothing, so it gave up. |
| `fatal` | The assistant crashed. `detail` carries the exception. |
| `goodbye` | The caller ended it themselves. |
| `ha-unavailable` | Home Assistant could not be reached at all; the call was apologised out. |
| `no-speech` | The turn produced no text. `reason` says which: `silence` (nothing was said, and the recogniser is healthy), `timeout`, `spawn-error` or `error`. |
| `tts-failed` | The voice failed twice on one call, so it apologised and hung up rather than continuing in silence. `tts_fails` carries the count. One failure is tolerated: a long answer can outrun the synthesiser's time limit while it is perfectly healthy, so one is a slow sentence and two is a broken voice. |

`rec_end` says how the recording finished — `timeout` (the caller stopped
talking), `hangup` (they put the phone down mid-sentence) or `dtmf` (they
pressed a key). Those three used to be one event, which made an abandoned call
and a silent one impossible to tell apart.

`call` is Asterisk's unique id for the leg, so a row here lines up with the
matching row in the call-quality ledger ([Per-call quality](health-monitoring.md#per-call-quality-call_quality_alerts)) for the same call — "it gave a
wrong answer" and "the audio was bad" become one question rather than two.

**Who can reach it.** Anyone who can pick up a house phone: family, guests, a
child, a visitor left alone in a room with an extension. **Not** an outside
caller. Two independent mechanisms stop that: the inbound trunk `Dial` uses
`r` only (never `t`/`T`), so no in-call DTMF transfer is ever armed for the
outside party; and even if one were, transfers are confined by
`__TRANSFER_CONTEXT` to `[internal-xfer]`, which contains only room extensions
and `0` — no feature codes at all. See [Toll-fraud protection](sip-trunk.md#toll-fraud-protection).

**What it can act on.** Only entities exposed to Assist
(*Settings → Voice assistants → Expose*). Exposing an entity makes it reachable
from **every phone in the house**, so treat that list as the real security
boundary. Two things worth knowing about it:

- A **garage door, lock, or anything that opens or unlocks should not be
  exposed.** A voice command is not authenticated — the phone in the guest room
  is as good as the one in the kitchen.
- Entities that **silence an alarm or disable a safety automation** deserve the
  same treatment even though they actuate nothing: a switch that turns off
  critical power alerts is a security control, and turning it off by voice
  leaves no visible trace.

**Duplicate names break commands.** The intent matcher resolves by spoken name,
so two exposed entities sharing a name make both unreliable. This bites in a
non-obvious way: Home Assistant's `switch_as_x` helper creates a `light.*`
wrapper *from* a `switch.*`, giving one physical device two entity IDs with the
identical friendly name. Expose the `light.*` (it answers brightness intents
too), not the switch.

**Phrasings that work.** The built-in agent is a fixed-sentence matcher, not a
language model, so the wording matters:

| Goal | Say |
|---|---|
| A binary sensor | "is the front door motion on" (not "is there motion at the front door") |
| Check something | "is the master bath fan on" |
| Control a light | "turn on the kitchen lights" |
| Read a sensor | "what is the camera health" |
| Survey | "what lights are on" |
| Temperature | "what is the temperature in the hallway" |

**Temperature needs a room.** A bare "what is the temperature" fails when more
than one thermostat is exposed. A smart speaker resolves this from the room it
sits in; a phone call carries no room, so name the area. This also means your
thermostats must have an **area** assigned — without one, even the room-qualified
question fails.

**Say the whole command in one go.** Each utterance is a fresh request, and that
is a property of Home Assistant's built-in agent rather than a shortcut taken
here: it is a sentence matcher, not a dialogue manager. Measured on this system,
`continue_conversation` comes back false for every ambiguous phrasing, and a
second turn sent with the previous turn's conversation id is matched as a new
independent sentence. It never asks "which light?" — an ambiguous command simply
fails, so name the room or the device the first time.

---
