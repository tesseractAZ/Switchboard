# Changelog

Recent releases. Releases up to 0.118.0 are in the
[changelog archive](https://github.com/tesseractAZ/Switchboard/blob/main/switchboard/CHANGELOG-ARCHIVE.md).

## 0.129.0

**A wake-up can be cancelled when the phone line turns "cancel" into a
sound-alike, and a call that understood nothing says the wake-up is still set.**

- **A "cancel" that comes back as another word now cancels.** Telephone audio
  loses most of an "s", and the recogniser wrote "cancel" as "Campbell." and
  "Campful." on 2026-08-21 and as "Okay, I'm full." and "Campful." on
  2026-11-23, the second time with the prompt hint added after the first in
  place. Both times nothing matched and the wake-up being called off rang. When
  a wake-up is set, a reply shaped like "cancel" (the whole reply, at most two
  words besides fillers such as "okay") now cancels it, as the word does. With
  no wake-up set it counts as not understood. Times are read first, as before.
  Checked against 1,288 recordings of 28 synthetic voices sent through the same
  8 kHz channel and recogniser: 535 of 560 spoken cancels now cancel (516
  before), and none of the 728 spoken times and other replies becomes one.
- **A "don't" stops a cancel.** "I don't want to cancel" and "never mind, keep
  it" used to cancel. A "don't", "not", "keep" or "leave" anywhere in the reply
  now cancels nothing; the reply counts as not understood. An echo of the recogniser's own
  prompt, which read as a cancel or as 6:53 PM, now does nothing.
- **A call that understood nothing ends with the wake-up that is still set.**
  It used to end "No wake-up call was set", which was false whenever one was. It
  now plays the recording that opens the call, "You already have a wake-up
  call…", and the time.
- **Two kinds of reply no longer replace a wake-up with the wrong time.** A
  zero with no other number after it ("Oh.", or whisper's "Oh, oh, oh." on
  noise) was read as the hour 0 and set midnight. A length of time ("ten more
  minutes", "one second") was read as that hour. Both now count as not
  understood. "Oh seven hundred", "seven oh five", "zero zero thirty" and
  "ten past seven" read as before.
- **A recording with no speech in it is not transcribed.** When nothing rose
  above the silence threshold, whisper still answered something, and any
  answer could act on the wake-up. The attempt now counts as a miss.
- **Each wake-up recording leaves a line in the log** with its length and why
  it ended, never the words, as the operator's recordings already do.

## 0.128.0

**The web terminal listens on an IPv6 address; comments left behind by the
documentation pass are corrected.**

- **`console_web_bind` can be an IPv6 address** (`::1`, or `[::1]`). The
  terminal's server opened an IPv4 socket whatever the address, so an IPv6 bind
  failed before it listened and the service restarted over and over, while the
  start-up log said it had started. It now opens an IPv6 socket for an IPv6
  address, and the log writes it as `[::1]:8100`. A bind that fails for any
  reason logs one line naming the address and the reason. A value that is blank
  inside its brackets (`[]`) means `127.0.0.1`, as a blank value does, in the
  start-up log as well as in the server. IPv4
  binds, the default included, behave as before.
- Failed logins to the web terminal and the SSH console are still limited to
  5 per address in 5 minutes, and an IPv6 network (`/64`) now also has a
  ceiling of 30 in that time, since one host can pick a fresh address in it for
  every try. Only attempts their own address was allowed count toward it, and
  a correct password takes its attempt back, so a device on the same LAN
  retrying a stale password locks out only itself, and devices signing in
  never use the ceiling up. An
  IPv4 client reached through a dual-stack `::` bind counts as its IPv4
  address. IPv4 clients are counted as before.
- Code comments, test notes, the operator-console reference and two changelog
  archive entries that still repeated claims the 0.127.0 documentation pass
  corrected now match the code: the console's footer order, what a mistyped
  resize message would have done, the outside line not being the emergency path,
  the cordless battery rule, how announce and wake-up delivery records are named
  and joined, and whose telephone numbers the call-quality ledger held.
  PERFORMANCE.md no longer calls the 2026-09-01 outage the only one the system
  has had.
- `tools/wp826.mjs login` (not part of the add-on image) no longer prints the
  start of the session id.

## 0.127.0

**The documentation, checked against the code.**

- Every Markdown file was compared with the code it describes. 238 statements
  that were wrong, out of date or incomplete are corrected, each re-checked
  against this release's code first: in the README, DOCS.md, SECURITY.md,
  PERFORMANCE.md, the reference pages, the option help text, the changelog
  archive, and code comments that repeated a corrected claim. 18 of them are
  places where one page still disagreed with another after its own fix, among
  them the wake-up scene's help text, which said the scene fires when the call
  rings: it fires when the call is answered.
- Old release notes stay the record of their release: an entry was corrected
  only where it misdescribed that release when it was written, never to
  describe what later releases changed.
- Example values that looked real now use reserved forms (`sip.example.com`,
  555-01xx numbers).
- The device-health monitor's start-up note about an unset
  `cordless_cert_sha256` names the section with the pinning steps, instead of a
  numbered section that no longer exists, and says what an impostor would
  capture: the salted login hash, from which the password can be recovered
  offline (the password itself is never sent).
- Nothing else changes: calls, prompts and every option behave as in 0.126.0.

## 0.126.0

**What a caller said no longer reaches the add-on's log when a spoken reply fails.**

- The phone-to-speaker announce confirms with "Announcing on the speakers:"
  followed by the words it heard. When that reply could not be rendered, the
  line logged for the failure quoted the start of its text, and a render that
  timed out logged all of it, so the caller's words reached the add-on's log
  (the Log tab) whatever `assistant_transcripts` said.
- The line logged when a reply cannot be rendered, for dial-a-status, the
  directory, the announce and the wake-up readout, now follows the rule the
  speech recognizer and the voice assistant already follow: it quotes the reply
  only while `assistant_transcripts` is on, and otherwise gives its length and,
  for a render that failed with an error, only the kind of error. If the
  setting cannot be read, the reply is left out. The wake-up readout's own note
  that it skipped the weather or a calendar entry still names what it skipped.
- Calls sound the same; only the log line of a failed reply differs.

## 0.125.0

**Fixes found by a documentation review.**

- **Lights menu (`43`): rooms and lights that could not be chosen by name now
  can be.** Lights with no area are read out as "Unassigned", but saying it
  ended the call, because that area's internal name was also the menu's signal
  for "cancel". A room or light whose name holds or sounds like a command word
  ("Top Floor" is close to "stop"; "Exit Sign", "All Lights") could not be
  chosen either, because the command words were checked first. Saying a whole
  name now chooses it; a command word on its own still cancels or lists, unless
  something is named exactly that word. An area actually named "Unassigned" now
  takes the lights with no area (except one named like a light already in it),
  instead of standing beside a second "Unassigned" that could not be told apart. The
  goodbyes also say what happened: a cancel at the light step was answered as
  a mishearing, and two mishearings at the on/off step as a cancel.
- **The standalone web terminal (`:8100`) starts only with a login.** With
  `console_users` empty it used to run on `127.0.0.1:8100` with no login. Under
  host networking that address is the host's own, so every program on the host
  and every other host-network add-on could drive the operator console through
  it. It now stays off until `console_users` has a valid entry, and the start-up
  log says why. The operator console in the add-on's panel in Home Assistant is
  not affected; it is signed in by Home Assistant.
- **`tools/wp826.mjs` checks the handset's certificate on every connection.**
  With `WP826_CERT_SHA256` set, it compared the pin once on a separate
  connection, then sent the login, password included, on new connections it
  did not check. Each request now runs on a connection whose certificate was
  checked first, and a mismatch stops the command before that request is sent,
  naming it. Its `fingerprint` command, the documented way to get the pin, no
  longer needs a password file, and `WP826_PORT` sets a port other than 443 (a
  value that is not a port number is refused). The tool is not part of the
  add-on image.

## 0.124.0

**No feature code or room can take the place of the emergency notice.**

- `911` and `933` are always answered with the spoken notice that emergency
  calls cannot be made from these phones, and with a trunk in prefix mode so
  are the prefix followed by either (`9911` and `9933` by default). The check
  that skips a feature code colliding with a room or another code did not
  include them: a feature code set to `911` or `933` was written into the
  dialplan ahead of the notice and answered those calls in its place, and the
  start-up check said nothing about it. Now any of the eight feature codes set
  to one of these numbers is skipped at start, with a log line saying it is an
  emergency number, and the notice keeps the number.
- A room numbered `911`, `933`, or a prefixed form while the trunk is on, gets
  a WARNING at start. Dialling its number from a phone plays the notice, because
  the notice's extension outranks the room pattern, while the operator,
  directory assistance, transfers, paging and inbound calls still reach it.
  The room is kept, so its phone still registers and places calls. A prefixed
  form is an ordinary number while the trunk is off, since nothing answers it
  then.
- Configurations that use none of these numbers produce the same dialplan as
  before.

## 0.123.0

**A spoken reply no longer stays behind in the add-on after a hang-up.**

- Every reply a voice feature speaks on a call (the directory, home
  automation, dial-a-status, the phone-to-speaker announce, the wake-up
  readout and the voice assistant) is rendered to a WAV in the add-on's
  `/tmp` and deleted once it has played. A call that hung up while a reply
  was being rendered or played skipped that deletion, and the WAV stayed until
  the add-on next restarted: about 16 KB per second of speech, and a reply
  can repeat what the caller said: the announce line's read-back always does,
  and an assistant reply can. The web UI's once-a-minute housekeeping now
  removes such a file 10 to 11 minutes after it was written.
  It takes only regular files with the names the voice scripts give them,
  never a link or a directory, and leaves every other file in `/tmp` alone.

## 0.122.0

**Spoken replies on a call are shaped for the phone band too.**

- Everything the voice features say that has no recording is spoken by
  espeak-ng through `switchboard-tts`: the directory (`411`), the lights menu
  (`43`), dial-a-status (`45`), the announce line's prompts and read-back
  (`46`), the wake-up weather and calendar, and the voice assistant (`47`).
  That speech now goes through the handset announcements' mastering (0.121.0),
  by the same code: the ffmpeg chain to the 300–3400 Hz band with the
  consonants lifted and ffmpeg's resampler to 8 kHz, then the speech level set
  to the built-in prompts' -15 dBFS under a -1 dBFS peak limit. espeak-ng's own
  output measures -18.6 dBFS, so the replies are about 3.5 dB louder, level with
  the prompts around them. On a Home Assistant host a 2-second reply renders in
  about 0.20 s instead of 0.06 s, and a 9-second one in 0.28 s instead of
  0.08 s.
- If ffmpeg is missing, fails, runs past 3 seconds or returns unusable audio,
  the reply is spoken exactly as in 0.121.0, and the Log tab says so once per
  start, without the words. The limit is 3 seconds, not the announcements' 10,
  because the callers allow a reply's synthesis 20 seconds (the assistant 25)
  and espeak-ng alone may take 15. After a run past it the shaping is skipped
  for 10 minutes, so a stalled ffmpeg delays one reply, not every reply after
  it. Silence, replies under 20 ms and speech of 120 seconds or more are spoken
  unshaped and not logged.
- `switchboard-tts` now writes its own lines straight to the Log tab, so one
  is not lost when the caller hangs up while a reply is being made. The
  directory, dial-a-status, announce and wake-up features used to drop them; the
  lights menu and the assistant, which carried them on their own `tts failed`
  line, now show them on the line before it. They never contain the words.

## 0.121.0

**Announcements to the handsets are shaped for the phone band.**

- Every phone here hears 8 kHz narrowband audio, about 300–3400 Hz. The speech
  of a handset announcement (`POST /api/announce/<ext>`, `{text}` or `{url}`)
  now goes through one ffmpeg process: a 250 Hz high-pass, -2 dB at 500 Hz,
  +5 dB at 2.8 kHz for the consonants, a 3.5 kHz low-pass, 3:1 compression,
  and ffmpeg's resampler to 8 kHz, which keeps energy above 4 kHz from folding
  back into the band as hiss (the old resampler left a 6 kHz tone only about
  15 dB down at 2 kHz). Each clip's speech is then raised toward the
  built-in prompts' level, -15 dBFS, by at most 20 dB, under a look-ahead
  peak limiter at -1 dBFS. Speech up to about 15 dB quieter than typical TTS
  output reaches that level; a quieter clip gets the full 20 dB and stays
  below it. The chime is not processed and sits about 3 dB above
  the speech.
- If ffmpeg is missing, fails, runs past 10 seconds or returns unusable audio,
  the clip is rendered exactly as in 0.120.0, and the Log tab says so once per
  start. After a run past 10 seconds the shaping is skipped for 10 minutes, so
  a stalled ffmpeg holds back one announcement, not every one after it. Silence
  and clips under 20 ms are not shaped and not logged. The same input renders
  the same bytes, so duplicate suppression is unaffected. On a Home
  Assistant host, a 10-second `{text}` or WAV clip renders in about 0.2 s
  instead of 0.4 s, as the Python resampler is off its path, and a 10-second
  MP3 clip takes about 90 ms longer. Announcements to the Home Assistant
  speakers (`46`) are unchanged.
- A WAV `{url}` clip cut off mid-sample now plays up to the cut instead of
  failing with HTTP 502. A WAV whose chunks the reader cannot follow, or with
  no frame rate, no longer raises an error; it is handed to ffmpeg, which
  usually cannot decode it either, so it still fails with HTTP 502.

## 0.120.0

**Recorded prompts can be replaced with your own recordings.**

- A file in `/app_configs/<slug>/prompts` (`addon_configs` on older Home
  Assistant releases) named after a prompt (`sw-greeting.wav`,
  `sw-no-emergency.ulaw`, ...) replaces the built-in recording at the next
  start: an 8 kHz mono 16-bit PCM `.wav`, raw µ-law `.ulaw`, or both. A `.wav`
  given alone gets a µ-law copy, the phones' codec; given both, the phones play
  the `.ulaw`, and the summary line counts those names. Removing the file and
  restarting returns the built-in prompt. DOCS.md lists every prompt and what it
  says, with conversion commands.
- Each file is checked first: a built-in prompt's name, a regular file and not
  a link, 0.1 s to 1 MiB, some sound above about -50 dBFS, for a `.wav` the
  format above, and for a `.ulaw` not a WAV, MP3, FLAC, Ogg, AIFF, Sun/NeXT,
  CAF or MP4 file renamed. A file that fails is skipped with one log line, and
  that prompt keeps its built-in recording, the `911` notice included. Raw
  16-bit PCM or A-law named `.ulaw` cannot be told from µ-law and plays as
  noise; DOCS.md says how to make the file and to dial `933` to hear the
  notice. Anything unexpected in this step leaves every prompt built-in and the
  start carries on. One line sums up the start.
- The shipped recordings stay write-locked. The accepted files are installed
  under `/run/switchboard/prompts` and played from there by absolute path, so
  a custom `.wav` can never lose to the built-in `.ulaw` beside it. The
  dialplan, every voice flow, the test ring and the talking clock ask one
  resolver which recording a prompt plays; the clock measures that same file,
  so its tone stays on the second it names.

## 0.119.4

**The dashboard no longer shows the trunk provider's server.**

- The header and the Trunk position typed the host set in
  `trunk.provider_host`, and `/api/status` sent it to the browser, where the
  developer tools could read it whatever the page drew. A provider's host is
  usually a regional point of presence, so it can say roughly where the house
  is. The status now carries only whether the trunk is on and its
  registration; the header reads `trunk: on` and the Trunk position `on`,
  beside the registration as before.
- The operator console showed only the registration and is unchanged. The
  add-on's Log tab still names the host on a trunk registration retry.

## 0.119.3

**The readable Asterisk log no longer names the trunk provider's server.**

- `/share/switchboard/asterisk.log` kept the provider's host on every trunk
  registration retry, on the reasoning that it identified nobody. A provider's
  host is usually a regional point of presence, so it can say roughly where the
  house is. Both scrub passes now take out every whole-host occurrence, on any
  line, of the host name set in `trunk.provider_host` or `trunk.from_domain`
  (in the options or the options overlay, with or without a `sip:` scheme, a
  port or a trailing dot), and the host name in each `sip:` URI on a
  registration line, configured or not. An address set there is not a host
  name: a private one is masked as before, a public one stays. The boot pass
  writes `<trunk-host>`, the per-poll pass a mask of the same length; the port,
  the masked account and the retry interval stay.
- The boot pass scans the whole file at every start, so the lines already there
  go at the first start of this version: on a copy shaped like an affected log,
  130 retry lines naming the host 260 times, it left none and changed no other
  line. Backups taken before that start, the private `/data/state/asterisk.log`
  and the add-on's Log tab still carry the host.

## 0.119.2

**No root read or write in `/data/state` follows a link.**

- `/data/state` belongs to the `asterisk` user, its group can write it, and it
  is not sticky, so that user can put any name in it. Five root writers there
  still went through a name: the ever-registered set wrote a fixed
  `<file>.tmp`; the low-MOS capture appended, trimmed and changed its mode by
  name; the link-health ledger read the old file by name, which copied a link's
  target into a ledger the `asterisk` group reads, and set its mode by name
  after the replace; the wake-up and MWI stores opened and changed the mode of
  their `.lock` files by name, and read the store by name before rewriting it.
  Each now reads and appends through a no-follow descriptor on a regular file.
  A writer that replaces its file writes a new temporary file of its own, with
  its mode settled before the replace.
- A link or a FIFO at a store's lock name is removed and the lock created
  again, so wake-ups keep ringing; a directory there refuses the call. A FIFO at
  a store's name reads as an empty store instead of hanging the reader.
- Root reads from `/data/state` follow no link either, and none blocks on a
  FIFO: the two alert latches, the ever-registered set, the call ledger
  devhealth reads every cycle, the durable Asterisk log rtpmon reads for
  transitions, and the backup hook, which opens every file there and in
  `/share/switchboard` to flush it. A FIFO at a latch, the set or the call
  ledger stopped its poller, at start or on its next cycle. One swapped in after
  a check by name stopped rtpmon at the log, and hung the backup hook and the
  backup it runs in.
- The latches and the set are read up to 64 KiB and the log up to 512 KiB a
  cycle, so none of them is read whole, whatever its size. A set nested too
  deep to parse, or one whose `exts` is not a list, no longer stops rtpmon at
  start.
- rtpmon now saves `ever-registered.json` as `0600`: it is the file's only
  reader. A set saved before keeps its mode until an extension registers for the
  first time. The low-MOS capture goes back to root on its next write; on
  installs that ran v0.102.0 to v0.108.x it was owned by `asterisk`.

## 0.119.1

**The gateway alert needs two link-health samples, not two of its own cycles.**

- Gateway health is read every 120 s off a rollup published every 300 s, so
  a GXW reboot caught by one sample (a firmware auto-install at 01:00, the
  weekly reboot) was read twice and raised "all 8 gateway ports down", then
  its recovery. The alert now advances only on a sample with a new
  `measured_at`, as the cordless's registration does, and goes out on the
  first read of the second; a recovery comes on the first new sample that
  reads `ok`. A rollup without `measured_at` is judged cycle by cycle.
- No reading inside the 360 s startup grace counts. Its "re-registers"
  reading is still published but no longer raised as a card, and a gateway
  already down at start alerts 480 to 720 s after the start (was 480 s).
- The cause is decided a sample later. In 3,000 simulated outages the gateway
  and outage alerts named the same cause in all; before, they differed in
  198, each a cordless dropping one sample after the ports.
- Gateway and cordless alerts survive an add-on restart. Each is kept in
  `/data/state` with whether its card was shown and the host boot; a shown
  alert is not raised again, and its recovery still comes (a gateway already
  back clears on the first sample read after the grace, 360 s after the
  start). One not shown, raised over 7 days earlier, or raised before a host
  reboot, which clears Home Assistant's cards, is raised again if the
  condition persists.
- The fleet-outage alert is not restored after a host reboot either. Its
  latch now carries the host boot, so an outage still on is announced again,
  card and push, on the second settled sample, and one already over posts no
  recovery.

## 0.119.0

**A phone-system outage reaches the phone, and its alerts name what is down.**

- On 2026-11-12 the host moved to a new address at 18:28 MST (01:28 UTC on
  11-13) and all nine phones were unregistered from 18:31. Every alert was a
  silent notification card. The outage card counted "10 of 10", including
  ext 20, which has never registered; the gateway alert said the GXW had
  likely lost power; cordless health read `ok` throughout; and both
  link-health rollups read `unknown`.
- The fleet-outage alert and its recovery are also pushed to
  `wakeup_push_target` ("phones down", "phones back"): ordinary pushes, not
  critical ones, and not retried. The key is unchanged; its label is now
  "Phone Push Target". `link_health_alerts: false` silences the push too.
- The outage alert clears only once two fewer phones than its threshold are
  down (3 of 9) on two consecutive samples, and the recovery names any still
  down ("still down: ext 14"). It cleared on the first sample under the
  threshold, so four ports dead and a fifth re-registering every 15 minutes
  raised and cleared it four times in 100 minutes.
- The outage latch is kept in `/data/state`, with whether the alert was
  shown. A refused card counts as shown once its retry posts it. A restart
  during an outage does not raise or push a shown alert again, and its
  recovery still comes; one muted or lost unshown is raised after the
  restart.
- Extensions that have never registered no longer count toward the outage
  threshold or appear in its text ("9 of 9"). `link_health` gains `expected`
  and `never_registered_exts` attributes.
- Cordless health reads the cordless's SIP registration off the link-health
  rollup: unregistered on two consecutive rollup samples read after the 360 s
  startup grace is `critical`, even with no IP to probe. A reboot inside one
  sample, or a rollup that cannot say, raises no registration alert; the LAN
  probe can still report a rebooting handset unreachable. The sensor gains
  `sip_registered`. A cleared registration, reachability, battery or Wi-Fi
  alert now reads as a recovery, not "alert lapsed". No cordless alert clears
  while the newest sample shows the handset unregistered, and with nothing to
  probe, such a cycle does not publish `ok` over the alert.
- When every gateway port and the cordless drop together, the outage,
  mass-drop and gateway alerts name the Switchboard host or the network
  rather than the gateway. A port still up, a cordless already down before
  the outage began (even one sample before), or a drop healed before the next
  sample keeps the gateway wording.
- `link_health` reads `unavailable` when no phone that should be up is
  reachable, and `wired_link_health` when no gateway port is; `unknown` now
  means only reachable but not yet measured. Units and `state_class` do not
  change.
- A failed push no longer names its target in the add-on log.
