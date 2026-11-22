# Codecs — G.711 µ-law only, on purpose

Switchboard uses **G.711 µ-law only**, everywhere — every endpoint (rooms and the
trunk) is pinned to `disallow = all` / `allow = ulaw`. There is no codec option; it
isn't configurable, and no HD/Opus module is even installed. **Nothing is ever
transcoded between codecs**, and nothing has been since v0.10.0 removed the
`codecs` option (before v0.9.9 the room endpoints also offered G.722 and Opus by
default) — no other codec can be negotiated. This is deliberate:

- **Antique analog handsets are narrowband by physics** — the carbon/electret
  element and the two-wire loop top out around 300–3400 Hz. Wrapping that in Opus
  or G.722 carries no extra fidelity; the analog transducer is the ceiling.
- **µ-law is the baseline every leg speaks** — the analog FXS ports, the cordless,
  and the PSTN trunk. Pinning one codec means **no call ever transcodes**: lowest
  latency, and dial tone / ringback / fax tones pass cleanly.

Because enforcement is server-side at the Asterisk endpoints, a phone's own codec
order doesn't matter — the negotiation can only ever land on µ-law. Just make sure
each device still *offers* G.711 µ-law (PCMU); a device configured to offer only a
non-µ-law codec would have no common codec and the call would fail. The dashboard
and operator console show the negotiated codec per active call, so you can confirm
it reads "µ-law".

---

**Some audio is converted inside the box, and none of it is a codec.** Asterisk's
internal working format is signed linear, so anything that needs raw samples
converts to and from µ-law inside the box. Speech recognition does — a recording
has to be decoded before it can be transcribed, and that is unavoidable. So does
synthesized speech: spoken replies (`switchboard-tts`) and handset announcements
(`POST /api/announce`) are rendered as 8 kHz 16-bit PCM WAV and encoded to µ-law
as they play.

Playing a prompt used to as well, and that one was avoidable: all 28 shipped
prompts were 16-bit PCM with no µ-law copy, so every greeting, wake-up prompt and
page was converted in real time — eight legs at once during a house-wide page.
Since v0.82.0 each prompt also ships as `.ulaw`, and Asterisk picks the file that
matches the channel, so the conversion simply does not happen. The `.wav` masters
stay: they are the editable source, and a test fails if a prompt has no µ-law
twin or the two differ in length, which re-recording a prompt almost always
changes. Only the recorded prompts have a µ-law copy.
