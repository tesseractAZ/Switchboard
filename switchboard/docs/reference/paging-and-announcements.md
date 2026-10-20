# Paging & announcements

## Page all — dial `44`

Dial the paging code to talk out of **every configured room phone at once** — a
house-wide intercom, duplex, so anyone paged can answer back.

Paging has **two implementations**, and which one you get depends on where you
start it:

- **Dialing `44`** runs Asterisk's `Page()` application against every room
  endpoint at once. `Page()` assembles its own conference.
- **The dashboard's and console's page buttons** instead Originate each phone
  into the add-on's `[page]` dialplan context, which answers the line and joins
  it to a ConfBridge under the generated `switchboard_bridge` /
  `switchboard_user` profiles.

Those ConfBridge profiles (in the generated `confbridge.conf`: no music-on-hold,
no join/leave name announcements, no "you are the only person in this
conference") therefore shape the **dashboard/console** page only — the dial-`44`
path does not read them, so the two can behave slightly differently on prompts
and tones.

## Announcements — dial `46` (phone → Home Assistant speakers)

Dial the announce code, speak your message after the beep, and it plays out your
configured Home Assistant `media_player` speakers (`announce_players`), bracketed
by a three-note station chime. The message is transcribed and re-synthesized
on-box (espeak-ng), rendered to a WAV the add-on serves on your LAN, and pushed to
every speaker at once via `media_player.play_media`.

## Announce onto a handset — `POST /api/announce/{ext}` (Home Assistant → phone)

The add-on also exposes an HTTP endpoint that speaks a clip **onto a room handset**
— the integration that lets Home Assistant (or another add-on) announce to a phone,
including the WP826 cordless, the way it would to a smart speaker. The phone
auto-answers hands-free (an intercom `answer-after=0` header, caller ID `8000`) and
plays the clip.

```
POST http://<ha-host>:8099/api/announce/<ext>
Header: X-Announce-Token: <announce_token>
Body:   {"text": "Dinner is ready"}     # spoken on-box (espeak-ng), or
        {"url":  "http://…/clip.wav"}    # audio to fetch and play (WAV; MP3 and other formats via ffmpeg)
```

- The `<ext>` must be a configured room. It can only play a local clip to a known
  handset — never place an outside call.
- **Limits and responses.** `text` is capped at 500 characters (HTTP 400). A
  missing `text`/`url`, or a `url` that is not `http`/`https`, also returns HTTP
  400. An extension that is not a configured room returns 404. A clip that cannot
  be rendered, such as a `url` that cannot be fetched or decoded, returns 502
  `render failed`. A rendered clip longer than 90 seconds is refused with HTTP 413
  `{"ok": false, "skipped": "too-long", "seconds": N}` and recorded `too-long`. A
  handset with no contact returns 503
  `{"ok": false, "skipped": "unreachable", "device_state": …}`. Otherwise the
  response is HTTP 200 `{"ok": true|false, "sound": "<clip name>"}`, with `ok`
  saying whether Asterisk accepted the call (the busy and duplicate answers below
  are also HTTP 200). A request refused before its clip is rendered leaves no
  ledger row; only the HTTP status records it.
- **Authentication:** over the LAN this requires the `X-Announce-Token` header to
  match your `announce_token` option. If `announce_token` is blank (the default),
  LAN announce is **disabled** and only the Home Assistant Supervisor can call it.
- The `{url}` branch fetches `http`/`https` only. It rejects loopback,
  link-local, multicast, reserved and unspecified hosts; a private-LAN URL such
  as Home Assistant's own TTS is allowed. It does not follow redirects and caps
  the body at 5 MB. A WAV is decoded directly and other formats (such as the MP3
  Home Assistant's TTS proxy serves) with ffmpeg, which the image installs on a
  best-effort basis. The result is transcoded to 8 kHz for the phone line and
  bracketed by the same chime as a `{text}` clip.
- **Busy-guard:** when the target phone is already on (or being offered) a call —
  its device state reads `INUSE`, `RINGING`, `RINGINUSE`, `BUSY` or `ONHOLD` — the
  call is **not** placed. A second INVITE cannot auto-answer mid-call and would
  ring the handset as call waiting. The skip is logged and the response is
  `{"ok": true, "skipped": "busy", "device_state": …}` (HTTP 200), so callers
  treat the announcement as handled instead of re-queueing identical content
  behind the call in progress. If the device state cannot be read, the call
  proceeds (the guard fails open, so a state-read hiccup never suppresses an
  alert). The guard applies only to this endpoint — ordinary inbound/outbound
  calling, paging, and wake-up calls are unaffected.
- **Duplicate suppression:** the same audio sent to the same phone again within
  five minutes is not played twice. Every render gets a fresh filename, so the
  clip is compared by content rather than by name.

  A request alone never starts the window. The ledger check below measures it
  from the earlier copy's `audio-delivered` row. The in-memory fallback starts it
  when Asterisk accepts the call, and only if the handset's state was read first;
  since v0.105.0 it does not start when that state could not be read. Until
  v0.85.0 the window started when the request arrived — before the guards above
  had run — so an announcement refused because the handset was not
  registered began a suppression window anyway, and each retry pushed that
  window forward. An announcement turned away once could never be delivered
  while the caller kept trying, which is the opposite of what a retry is for.
  Every refusal path now leaves the window untouched.

  It is a rate limit, not a lock: identical content repeated on a timer plays
  once per window, rather than once and then never again.

  Since v0.106.0 the check **reads the ledger** below whenever it can, and the
  in-memory window decides only when it cannot. The window alone knew one thing,
  that this web UI had sent the same audio, and that was wrong in both
  directions. Past it, the same words reached a room twice: the first copy was
  an automatic replay (the retry below, which runs in another process), went out while
  Asterisk could not report the handset's state (the window is deliberately not
  started then), or played before a restart. Inside it, the window said
  "duplicate" even after the room had since been asked for something else, or
  when the first copy never played at all.

  The ledger sorts every earlier announcement to that phone into **heard**
  (its audio arrived), **in flight** (handed to Asterisk within the last
  3 minutes, no verdict yet, so it may still ring or play), **waiting** (the
  retry below may still send it), or **gone**. An announcement is a duplicate
  when the same audio was **heard** inside the window with nothing different
  heard or in flight since. It is also a duplicate while the same audio is
  still **in flight or waiting**. "Door open, door closed, door open" is
  therefore three announcements. An identical announcement whose first copy is
  gone unheard is sent at once.

  Both cases answer the caller `{"ok": true, "skipped": "duplicate"}` and record
  the clip:
  - `duplicate-suppressed` (`basis: delivered`) names the delivery it matched
    (`matched`, `delivered_at`) and is never replayed.
  - `duplicate-pending` (`basis: pending`) names the copy it is waiting for
    (`matched`, `asked_at`). It is a record, not a second announcement: the copy
    already queued is the one the room hears, keeping its place in the retry and
    the idle observations it has gathered. If that copy can never play, the next
    repeat finds it gone and is announced normally.

  A repeat is only held back when something really will send it: the retry
  must be on (`announce_retry_attempts` above `0`) and running, and it runs in the
  wake-up scheduler, which idles while `wakeup_enabled` is off. If either is not
  so, no replay is coming, so repeats are never held back — they are the only way
  the room hears anything.

  Neither duplicate row is a request of its own: it is never replayed, and it
  never displaces or retires another announcement. A stale, different
  announcement still waiting for the retry is superseded by the original request
  for the matched audio when that request is the newer one, ranked at the time
  that request was made, not when the repeat arrived. A different message asked
  for after that request still plays.

  When the ledger cannot be read, or holds no history for that room at all, the
  in-memory window decides as before, and its row says `basis: memory`.

  The ledger check runs after the handset's state is read and before the busy
  and registration checks. A duplicate is therefore answered as a duplicate
  whatever the handset is doing, and a copy whose audio finishes while the state
  is being read is caught.

  Rows name the content by a **keyed tag** (`digest`, 12 hex characters), never
  by a plain hash of the audio. The spoken text is predictable, so a plain hash
  in a shared folder would let anything that can read `/share` confirm a guessed
  sentence. The key is created on first use and kept in the add-on's private
  `/data` folder. If it cannot be created, no row carries a tag and duplicates
  are recognised in memory only. The clips themselves are no longer served to
  the LAN: only the separate speaker announcements that Home Assistant media
  players fetch are.
- **Every outcome is recorded**, in `/share/switchboard/delivery-outcomes.jsonl`:
  `originate-queued` when the call was placed, and `too-long`,
  `duplicate-suppressed`, `duplicate-pending`, `skipped-busy`, `unreachable` or
  `announce-originate-failed` when it was not. An announcement that never became
  a call has no call-quality record, so once its clip has been rendered, this
  ledger is the only place it appears.

  `announce-originate-failed` carries the reason on the row, and the name of the
  clip, so a failed send appears in the same place in an announcement's history
  as a successful one. The reason is `refused` whenever Asterisk did not accept
  the call. That currently includes a request that could not reach Asterisk at
  all, because the Asterisk client reports a connection or login failure as a
  refusal; the caller then receives HTTP 200 with
  `{"ok": false, "sound": "<clip>"}`. The `ami-error` reason, answered with
  HTTP 502, is written only when the request itself raises. Before v0.104.0 those
  two cases were written as `originate-error` and `originate-refused`, the same
  two names the wake-up call uses for its own failures and with no clip on
  either, so an announce failure could not be joined to the announcement it
  belonged to. (The wake-up rows are unchanged; [Wake-up calls](wake-ups-and-clock.md#wake-up-calls--dial-42) still lists them.)
- **...and when the pre-flight check could not answer.** Before placing the call
  the add-on asks Asterisk whether the handset is registered, and if the answer
  is "no contact" the call is skipped and recorded `unreachable`. If that
  question cannot be answered at all — Asterisk not yet talking to the add-on, a
  few seconds after a restart — the announcement goes out anyway, because a check
  that refuses to announce when it cannot ask would silence an alert.

  Since v0.104.0 that also writes an `announce-guard-unjudged` row naming the
  clip, so "the handset looked fine" and "nobody could tell" stop looking
  identical in the ledger. Live on 2026-09-15 at 01:42:12Z, 8.4 seconds after an
  add-on restart, an announcement went to the cordless before it had
  re-registered; Asterisk logged one error line, the clip never played, and
  nothing recorded that the check had been skipped. It is **not** a refusal: the
  announcement still goes out and is still judged normally afterwards.

  Until v0.105.2 the question came back unanswered almost every time — on 28 of
  29 announcements — because Asterisk writes that reply in two pieces a few
  microseconds apart and the add-on stopped reading after the first. So the
  `busy` and `unreachable` refusals above almost never happened, and nearly every
  announcement carried an `announce-guard-unjudged` row. The reader now waits for
  the whole reply, and that row is back to meaning what it says: Asterisk really
  was not answering.
- **...and a refusal is sent again** (v0.105.2). `skipped-busy` and `unreachable`
  are announcements whose audio never played, so both now carry the clip name
  and are replayed by the retry described below — `unreachable` once the phone
  has contact again, `skipped-busy` once the line is free — under the same limits
  as any other replay. The response to the caller is unchanged. A refusal that
  is never replayed gets no `announce-undelivered` verdict, because it was never
  handed to Asterisk; its history ends with the retry's own
  `announce-retry-skipped` row, or with the refusal row itself when
  `announce_retry_attempts` is `0`, `wakeup_enabled` is off, or the add-on
  restarted before it could be
  replayed. Once it has been replayed it has been handed
  over, and from then on it is judged like any other announcement.
- **...including how it ended** (v0.98.0). `originate-queued` only means Asterisk
  accepted the request. The handset's hangup now records `audio-delivered` once at
  least a second of the clip has actually played, and anything still unaccounted
  for after the deadline below is recorded `announce-undelivered`. Before this, an
  announcement that rang a phone nobody answered ran no dialplan at all and so
  left **no record anywhere** — it looked exactly like one that was never sent.
  Live on 2026-09-01 at 19:05:15.

  The two halves are matched **by the name of the clip**, which the call now
  carries, not by which records happen to sit near each other in time — three
  alerts inside a few minutes is what an incident looks like, and that is when
  this has to be right. Since v0.98.2 the comparison uses the name's letters and
  digits only: the name crosses the dialplan, which was silently stripping the
  hyphens out of it, and an alert's delivery verdict should not depend on which
  punctuation survives that trip.

  The deadline is **ring + clip cap + a minute, and never under 180 s**, measured
  from the newest send: the original, or the latest automatic replay (v0.105.0). An
  announcement may ring for 30 s and then speak for up to `90 s`, so a flat
  two-minute deadline would report a failure for announcements still playing.
  Nothing older than an hour is judged at all, so a scheduler that was stopped
  overnight cannot file a night of retroactive failures — and, since v0.98.1,
  **nothing sent before the current run started** is judged either. A window the
  reconciler was not running for is unknown rather than failed: the confirming
  record comes from a short-lived helper that an add-on restart kills, and across
  an upgrade it may have been a build that never wrote one. Skipping that window
  is why an add-on restart does not produce a burst of false failures.

  An announcement **cut off partway through still counts as arrived**, with the
  stage and packet count on the record. It did not meet its contract — [per-call quality](health-monitoring.md#per-call-quality-call_quality_alerts) scores
  it `undelivered` as it always has — but it did reach the phone, and filing it
  beside one that rang out unanswered would erase the distinction this exists for.

  Unlike a missed wake-up, this is **recorded and not pushed**. An alarm clock has
  a deadline; an announcement does not.
- **...and one it declines to judge** (v0.100.2). Asterisk restarts with the
  add-on, and its handsets take up to two minutes to re-register (since v0.112.0 a
  handset still registered when the add-on stopped is reachable again within
  seconds of the restart, because registrations are kept). An announcement
  sent into that gap genuinely does not arrive — but `announce-undelivered` would
  be a true and useless record, because the cause is a restart you just performed.
  Anything queued within **120 seconds** of the add-on starting is recorded
  `announce-unsettled` instead, which says plainly that it was not judged and why.
  It is recorded rather than skipped: a request with no verdict at all cannot be
  told apart from one the system forgot about.

  120 s is the same settling cap the fleet monitor uses to decide the phones have
  finished re-registering, rather than a second number for the same question.
- **...and one it sends AGAIN** (v0.105.0). An announcement whose audio never
  played is now **retried automatically**, because three times in a fortnight one
  was sent to the cordless in the seconds after an add-on restart, before that
  handset had re-registered — the phone never rang, nothing played, and the only
  outcome was a ledger row minutes later. The handset in question was back 38
  seconds after the send that failed.

  The retry runs in the same 20-second loop as the reconciler above, and it will
  only send again once the phone **positively reports itself registered and idle,
  twice in a row**. Anything else — ringing, in a call, unregistered, or a state
  Asterisk could not report at all — waits. A check that cannot ask is what caused
  the original miss, and for a replay the safe answer to "we could not ask" is no.

  It is the *audio* that decides, not the reason it was missing — so **an
  announcement nobody answers is retried too**. A room phone that is registered
  and idle is a green light as soon as it stops ringing, which means one
  unanswered announcement can ring that room up to one more time than
  `announce_retry_attempts` (three times by default) inside the 150 seconds
  below. Set `announce_retry_attempts` to `1`, or to `0`, if that is
  not what you want in your house.

  **An announcement whose audio played is never replayed.** The confirming record
  carries the name of the clip (above), and a clip that has one is excluded
  permanently, at any age, whichever order the records arrive in.

  Bounded at `announce_retry_attempts` attempts (default **two**, at most three),
  and **never started more than 150 seconds** after
  the original send: an announcement is often time-sensitive, and dinner being
  ready is not worth saying twenty minutes late. Two attempts spaced a poll apart
  straddle the 30-45 seconds a cordless handset takes to come back after a
  restart; past that the cause is no longer the restart. The 150 seconds is
  measured against how long a clip lives on disk (five minutes), not against the
  settling window above — the phone is not even available until most of a minute
  into that window. The attempt count is the `announce_retry_attempts` option and
  `0` switches the whole thing off; the two timings are fixed in the code, where
  they are derived from the clip's life and from this loop's own 20-second poll
  rather than being independent dials.

  Each attempt is recorded as `announce-retry-attempted` with the clip, the
  attempt number and the handset state that cleared it; the row is written
  **before** the call is placed, and the call is abandoned if it could not be
  written, because an attempt that cannot be counted cannot be limited. A replay
  that Asterisk does not accept is recorded `announce-originate-failed` with
  `reason: retry-refused` and the attempt number. This includes an Asterisk that
  could not be reached at all; `retry-ami-error` is written only when the request
  itself raises. The attempt still counts against the limit, and the
  announcement is still judged afterwards. When the retry gives up it records `announce-retry-skipped` once, saying why: `too-old`,
  `budget-exhausted`, `ext-superseded` (a newer announcement was sent to the same
  room, so only that one is replayed — never two at once to one phone),
  `clip-gone`, or `content-delivered` (v0.106.0: the same audio reached that
  room under another announcement's name, within the duplicate window around
  this one's request and with nothing different heard since, so replaying it
  would say it twice; the row names the clip that was heard as `matched`). None
  of these rows is a verdict — the reconciler above still files
  `announce-undelivered` or `announce-unsettled` for the announcement itself, now
  carrying how many retries it had.

  A restart inside the window abandons the retry, deliberately: the clip lives in
  memory-backed storage that the restart clears, and a run that may not judge an
  announcement may certainly not replay one.

---
