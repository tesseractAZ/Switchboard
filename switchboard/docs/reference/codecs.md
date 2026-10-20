# Codecs — G.711 µ-law only, on purpose

Switchboard uses **G.711 µ-law only**, everywhere — every endpoint (rooms and the
trunk) is pinned to `disallow = all` / `allow = ulaw`. There is no codec option; it
isn't configurable, and no HD/Opus module is even installed. **Nothing is ever
transcoded between codecs, and nothing ever has been** — no other codec can be
negotiated. This is deliberate:

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

**One thing is converted, and it is not a codec.** Asterisk's internal working
format is signed linear, so anything that needs raw samples converts to and from
µ-law inside the box. Speech recognition does — a recording has to be decoded
before it can be transcribed, and that is unavoidable.

Playing a prompt used to as well, and that one was avoidable: all 28 shipped
prompts were 16-bit PCM with no µ-law copy, so every greeting, wake-up prompt and
page was converted in real time — eight legs at once during a house-wide page.
Since v0.82.0 each prompt also ships as `.ulaw`, and Asterisk picks the file that
matches the channel, so the conversion simply does not happen. The `.wav` masters
stay: they are the editable source, and a build check fails if a prompt and its
µ-law twin ever drift apart.
