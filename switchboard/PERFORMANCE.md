# Measured behaviour

What this system does in service, taken from its own ledgers rather than from
design intent. [`DOCS.md`](DOCS.md) says what the add-on is supposed to do; this
says what it was observed doing, with the population each figure is drawn from
and the command that produces it again.

**Four rules govern every number here.**

*Nothing is projected.* Every figure is a count or a quantile over rows that
exist on disk. Where a mechanism has shipped but not been exercised, this says
so and states when accrual began. A blank is more useful than an estimate.

*Every figure names its population.* Not the section's window — its own. The
ledgers gained fields at different releases, so a column that exists on 288 of
1,704 rows describes those 288 and nothing more. The first draft of this document
got four figures wrong by taking a section header as the scope of the rows
beneath it, and one by counting a field's ship date as a measurement.

*Nothing here identifies anyone.* The ledgers hold extension numbers and, on some
legs, complete telephone numbers. Only aggregates appear below, no individual row
is reproduced, and see [Known exposure](#known-exposure).

*Everything is one deployment.* Ten configured endpoints — eight analog handsets
on one gateway, one WiFi cordless, and one softphone that has never registered —
plus one trunk. An existence proof, not a distribution.

**Software version 0.94.4.** Quantiles are nearest-rank.

There is deliberately no single "data as of" line. There was one, and it was the
document's own second rule being broken at file scope: §1's ledger figures were
cut on 2026-09-06 against 252 rows, while §5's hand-verified rows include calls
placed after that cut. One date over both would have described neither. Each
figure below states the population it covers.

---

## 1. Call quality

252 records spanning **2026-07-11 → 2026-09-06** (56.5 days), from
`/data/state/callqos.jsonl`. One row per hung-up leg, including two that carried
no media at all.

| Verdict | Legs | |
| --- | ---: | --- |
| `excellent` | 212 | 84.1% |
| `good` | 12 | 4.8% |
| `unknown` | 12 | 4.8% |
| `poor` | 11 | 4.4% |
| `fair` | 3 | 1.2% |
| `no-media` | 2 | 0.8% — new in 0.77.0 |
| **Alerted** | **11** | 4.4% of legs (all of them `poor`) |

This distribution is not evidence that the scoring works. For most of this window
the round-trip detector could not fire at all (§5), so a run of `excellent` is
partly a property of the instrument rather than of the calls.

**Round-trip — three different quantities.** `rtt_ms` is the final RTCP round
only; grading on it was the defect fixed in 0.77.0.

| | n | median | p90 | max |
| --- | ---: | ---: | ---: | ---: |
| `rtt_ms` (last round) | 244 | 5.7 ms | 33.20 ms | **311.66 ms** |
| `rtt_mean_ms` | 53 | 17.29 ms | 58.95 ms | 169.48 ms |
| `rtt_max_ms` (peak) | 95 | 32.99 ms | 146.96 ms | **845.93 ms** |

The three `n` values differ because the mean and peak fields were added later.

That last row is the whole argument for scoring on a distribution. The leg
carrying the 845.93 ms peak reported **10.65 ms** on its final round — an
eightieth of it — and would have been graded on that alone.

**Everything else.**

| | n | median | p90 | max |
| --- | ---: | ---: | ---: | ---: |
| Leg duration | 252 | 22 s | 72 s | 311 s |
| Receive loss | 250 | 0.00% | 0.00% | 6.00% |
| **Transmit loss** | 245 | 0.00% | 0.00% | **32.88%** |
| Worst-direction MES | 236 | 88.0 | 88.1 | 88.1 |

Loss is zero at the 90th percentile in both directions — the expected reading for
wired handsets on a quiet LAN. It is not, however, quiet: **the loss threshold
has fired five times**, and four of those five were the transmit direction, at
10.7%, 12.0% and 32.9%. A first draft of this section reported receive loss
alone and concluded from its 6% ceiling that the threshold had never fired. Both
halves were wrong, and the second was wrong *because* of the first — the column
that fired was the column not shown.

**Codec.** 227 legs `ulaw`, 23 `slin`, 2 with no codec (the no-media pair). Every
`slin` row predates 0.77.0, when the field carried Asterisk's internal read
format rather than the negotiated one — `slin` is a codec `pjsip.conf` forbids,
which is what exposed the bug. Six legs have been recorded since; **four carry
`codec: ulaw` and `read_format: ulaw`**, confirming on live calls that
`CHANNEL(audionativeformat)` resolves. The other two are the no-media legs, which
populate neither field.

**Sampling.** Of the 63 legs carrying the derived flag: 53 `multi`, 4 `single`,
6 `none`. `single` means the RTT figures rest on one RTCP round — statistically
empty and visually identical to a well-sampled leg, which is why the flag exists.

**Schema.** 6 records at `v: 3`, 246 with no version key. The unversioned rows
are why the key was added: their shape could only be told apart by counting
fields.

```
sudo docker exec app_<slug> sh -c 'cat /data/state/callqos.jsonl'
```

---

## 2. Link health

1,704 cycles spanning **2026-09-01 → 2026-09-06** (5.6 days), from
`/share/switchboard/heartbeat.jsonl`. The add-on has run since 2026-08-11, so
the file is younger than the deployment; at 350 KB against a 4 MB cap it has
never rotated, and why it begins on 1 September is not established here.

**Read the populations.** The `settled` and `phase` keys shipped mid-window and
exist on 288 rows only. Figures below are labelled with the set they cover;
mixing them is how the first draft of this section produced four wrong numbers.

| | Over all 1,704 cycles | Over the 255 settled cycles |
| --- | --- | --- |
| Window covered | 5.6 days | **0.90 days** (from 2026-09-05 19:46 UTC) |
| Trunk registered | 1,677 (98.4%) | 255 (100%) |
| Nine phones reachable | 1,617 (94.9%) | 255 (100%) |

The 100% columns are real and cover less than a day. The 98.4% and 94.9% are the
figures for the stated window, and the shortfall in both is restart convergence,
not fault: **27 poller restarts** occurred in the window, counted from the
`ami: unreachable` first-cycle signature that is present throughout. Only 8 of
those carry the `poller_started` flag, which shipped on 2026-09-05 — counting
the flag instead of the signature undercounts restarts by 3.4×.

**Wired round-trip.** The eight analog ports, restricted to the 262 cycles that
actually sampled all eight (14 further cycles report a wired median over fewer
ports mid-warmup and are excluded).

| n = 262 | min | median | p90 | max |
| --- | ---: | ---: | ---: | ---: |
| Median across the eight ports | 1.79 ms | 2.37 ms | 2.54 ms | 3.51 ms |
| Worst single port | 2.14 ms | 3.84 ms | 4.49 ms | **5.60 ms** |

**Receive-jitter peak, wired versus cordless.** The four legs recorded on wired
handsets since the field was renamed read 0.12, 0.88, 1.12 and 1.25 ms. The
cordless reaches 58.25 ms. Two orders of magnitude, on the same PBX, at the same
moment — which is why a single fleet figure is useless and the two are separated
everywhere in this document.

**Fleet worst, including the cordless** — a third population again:

| | n | median | p90 | max |
| --- | ---: | ---: | ---: | ---: |
| All cycles | 1,664 | 12.54 ms | 261.89 ms | **2,148 ms** |
| Settled cycles only | 255 | 9.90 ms | 30.50 ms | 136.93 ms |

The two-second figure is the WiFi handset. The gap between it and the 5.60 ms
wired ceiling is the entire reason the two are reported separately: a single
fleet maximum reports the cordless's radio and hides all eight wired ports
behind it.

**Fleet-wide drops detected: 0 — and the branch that would report one has never
received a candidate input.** All 174 transitions read in this window carry
`state: "Reachable"`; `went_unreachable` is empty in all 46 rows that carry the
key. So the harvesting half is demonstrably running and the deciding half is
not, and calling the detector "exercised" — as a first draft of this section did
— is the same error §6 describes as a test asserting its own scaffolding. The
transitions are recoveries from the 27 restarts; 100 of the 174 predate the first
restart carrying the flag.

The one fleet outage this system is known to have had — all eight ports for 119
seconds on 2026-09-01 at 01:00 MST — falls **inside** this file's window, at
08:00 UTC. It is absent because it fell entirely between two five-minute samples:
the cycles at 07:03 and 08:03 UTC both read `reachable: 9, total: 10`. That is
the defect 0.79.0 was written for, preserved here as the clearest evidence that a
point sampler cannot see it.

```
sudo docker exec app_<slug> sh -c 'tail -n 5 /share/switchboard/heartbeat.jsonl'
```

---

## 3. Delivery

7 records in `/share/switchboard/delivery-outcomes.jsonl`, spanning 2026-09-01 →
2026-09-06. A completeness check, not a rate.

| Outcome | n | |
| --- | ---: | --- |
| `originate-queued` (announce) | 3 | new in 0.78.0 — the announce path recorded only failures before it |
| `ring-queued` (wake-up) | 2 | |
| `unreachable` (announce) | 1 | the pre-flight device check refused to ring a dead endpoint |
| `permcheck` (self-test) | 1 | the writability probe added in 0.74.0 |

No `answered`, `spoken`, `no-answer` or `undelivered` rows: no wake-up has run
since 0.78.0 introduced the `spoken` milestone. **The reconciler is unexercised
in production.** Its behaviour is covered by tests, which is a different claim.

---

## 4. Voice assistant

**7 turns across 2 calls, 2026-09-06.** The ledger shipped in 0.80.0; before it,
nothing the assistant did survived the call, so this is the entire history.

| | n | min | median | max |
| --- | ---: | ---: | ---: | ---: |
| Speech recognition | 7 | 2.69 s | 2.72 s | **8.01 s** |
| Home Assistant round-trip | 5 | 0.02 s | 0.10 s | 1.30 s |
| Speech synthesis | 5 | 1.33 s | 1.63 s | 2.79 s |
| **Whole turn** | 5 | **10.2 s** | 10.6 s | **16.7 s** |
| Recording captured | 7 | 2.64 s | 3.56 s | 5.06 s |

Recognition is the floor and it is remarkably flat — six of seven turns landed
between 2.69 and 2.87 seconds regardless of how much was said, which says the
cost is model load and round-trip rather than audio length. The seventh took
8.01 s on the longest recording (5.06 s of audio).

A turn costs the caller **10 to 17 seconds**, of which roughly seven are the
machine thinking. All seven recordings ended on the silence timeout; none was cut
off by a hangup.

**Three of seven turns did not do what was asked.** Two were Home Assistant
declining to match a sentence (`response_type: error`) — notably, one phrasing of
a brightness command failed while a differently-worded version of the same
request, in the very next turn, succeeded. That is the sentence matcher's
behaviour, not this system's. The third was this system's fault: the caller
declined the *Anything else?* prompt and was told "Sorry, I couldn't understand
that", because a bare decline was not recognised as an ending.

(The utterances themselves are deliberately not quoted here. They are in the
ledger, which is why the ledger is not in the shared folder.) Fixed in 0.82.0 — and worth stating plainly that it was invisible
until this ledger existed, because nothing had ever recorded what the assistant
said back.

```
sudo docker exec app_<slug> sh -c 'tail /data/state/assistant.jsonl'
```

---

## 4a. Is anything transcoded?

**On the wire, never.** `pjsip.conf` sets `disallow = all` / `allow = ulaw` on
the shared endpoint template and on the trunk, so G.711 u-law is the only codec
that can be negotiated. Every leg recorded since 0.77.0 carries
`codec: ulaw`. No call has ever been converted between codecs, and none can be
without a configuration change.

**Inside Asterisk, on nearly every scripted call.** Two conversions run:

| | Why | Cost |
| --- | --- | ---: |
| u-law → slin | `RECORD` for speech recognition needs raw samples | 9,000 µs/s |
| slin → u-law | every prompt playback, until 0.82.0 | 9,000 µs/s |

The second was avoidable and is now gone. All 28 shipped prompts were 8 kHz
16-bit PCM with no u-law copy, so Asterisk converted each one in real time on
every playback — including all eight legs of a house-wide page, simultaneously.
0.82.0 ships a `.ulaw` sibling for each; Asterisk selects the file matching the
channel, so the conversion simply stops happening. Observed directly on
2026-09-06, in the log line for an emergency notice — `Playing
'switchboard/sw-no-emergency.ulaw'`, Asterisk naming the µ-law file rather than
the PCM master — on a wired FXS port (`PJSIP/12`) and again on the WiFi cordless
(`PJSIP/19`), which are the two handset families on this system and reach
Asterisk by different paths.

**The ledger under-reports this, and that is worth knowing before trusting it.**
`read_format` is sampled once, in the hangup extension. 23 of 246 legacy legs
recorded `slin`, concentrated entirely in the recognition paths — operator 17,
directory 3, wake-up 1, status 1, assistant 1, and zero on room-to-room,
announce or trunk legs. But *every* operator call runs `RECORD`, so if the field
reported occurrence it would read `slin` on all 83 of them, not 17. It captures
whichever format the channel happened to be in at hangup. Read it as evidence
that conversion happens on those paths, never as a count of how often.

---

## 5. Detectors: what has actually fired

The useful performance question about a monitoring system is not how fast it
runs. It is which alarms have ever gone off, and whether the silent ones are
quiet because the system is healthy or because they cannot fire.

| Detector | Fired | Why |
| --- | --- | --- |
| Assistant health | — | No ledger before 0.80.0 (§4). |
| Emergency notice (`911`, `933`) | 2 calls | **Both verified by hand** 2026-09-06, one per handset family: `911` from a wired FXS port (`PJSIP/12`), `933` from the WiFi cordless (`PJSIP/19`). Each ran its whole block — Answer, `SW_TAG=emergency`, `sw-no-emergency.ulaw`, `Congestion(5)` — and wrote an `emergency`-tagged row, with no WARNING, ERROR or NOTICE anywhere in either window. |
| Fleet drop (between samples) | 0 | Shipped 0.79.0. Its input half runs (174 transitions read); its deciding half has never seen a candidate — all 174 were recoveries (§2). |
| Fleet outage (point sample) | 0 | Structurally blind to an outage shorter than two poll intervals. The one real outage lasted 119 s. |
| Poor-call alert | 11 legs | Working. |
| Round-trip threshold | 0 | **Could not fire.** Tested against `rtt_ms`, whose maximum across all 244 legs is 311.66 ms, against a 400 ms threshold — while `rtt_max_ms` in the same records reaches 845.93 ms. Fixed in 0.77.0. |
| Unanswered-leg media gate | 2 legs | **Verified in BOTH directions** 2026-09-06. It skips when it should: two unanswered legs since it shipped, both routed to `no-media`, and the last media warning anywhere in the log is still 13:17:35 — the final unanswered call before the fix. It also does *not* skip when it shouldn't: on the answered `933` call the gate evaluated `GotoIf("0?nomedia")`, fell through, and read `RXC=621 TXC=621` over 12 s (≈52 packets/s, the expected rate at 20 ms ptime). That second half is the one worth having, because a gate stuck permanently open produces the same zero-warning reading while silently discarding every call's RTP telemetry. |
| Wake-up undelivered | 0 | Unexercised (§3) — and since 0.84.0 the re-ring it depends on is itself gated, so the path has two untested links, not one. |

**Five of eight have never fired.** One is genuinely quiet, two have not been
exercised, one has never received a candidate input, and one was structurally
incapable of firing and is now fixed. Two more were verified by hand across
three calls on 2026-09-06 — the emergency notice and the unanswered-leg gate —
because neither could be exercised without somebody picking up a telephone. That
is the honest limit of this table: a detector nobody can trigger from a shell
stays unproven until a person walks to a handset.

The gate is the one to learn from. Its first verification looked complete and
was half a result: "zero warnings" is equally consistent with a gate that works
and a gate jammed shut, and only an *answered* call — which nothing in a shell
can produce — separates them. Two of the five silent detectors above are silent
for exactly that reason, and no amount of reading the code would have told them
apart either.
Separating those three cases is the only reason this table is worth keeping — a
detector that cannot fire and a healthy system produce identical silence.

---

## 6. Test and mutation coverage

| | | Source |
| --- | --- | --- |
| Tests | **538**, 8.1 s | `pytest`, below |
| Mutants applied, 0.77.0–0.80.0 | 64 | release commits |
| Killed | 64 | release commits |
| Survived their first run | 10 | release commits |

The mutation figures are the one place this document breaks its own first rule:
they come from the release commit messages, not from a ledger on disk, and
cannot be reproduced by running anything in this repository. They are recorded
because the alternative is omitting the most informative number here.

A mutant is a deliberate defect applied to the source; it is *killed* when a
named test fails because of it. A clean run proves only what was entered — so
the ten first-run survivors matter more than the sixty-four kills, and they
clustered in one place: **the wiring between correct components.**

Deleting the entire fleet-drop consumer survived a green suite, because the
detector and the notifier were each tested and the call site joining them was
not — the same mistake as the bug being fixed. Deleting the assistant's silence
classification survived because every test stubbed the function that produces
it. Neither is a coverage gap in the usual sense; both are tests that asserted
their own scaffolding.

```
python3 -m pytest switchboard/tests/ -q
```

---

## Known exposure

Compiling §1 surfaced a defect. It is recorded here because this is where it was
found, not because it is a performance finding.

**45 of 252 call-quality rows carry a complete telephone number** in the `ext`
field. They divide three ways:

| | n | |
| --- | ---: | --- |
| Internal `rooms` legs | 32 | a full number reached a field meant for an extension |
| Inbound trunk legs | 10 | `ext` correctly holds the calling party — there is no extension to record |
| `operator` legs | 3 | two carry 12 digits: the trunk-access prefix plus a number **this house dialled out** |

`switchboard-callqos` mirrors the full record to
`/share/switchboard/callqos-outcomes.jsonl`, and `/share` is host-mounted,
readable from outside the container, and captured in add-on backups. That mirror
was made deliberately complete on the reasoning that a partial mirror is *"not a
smaller truth, it is a different claim"* — which was argued about auditing
completeness and never about disclosure.

Most belong to people who called this house. Two are numbers it called — which
is the more sensitive direction, and the one a first draft of this section
asserted was not present.

**Fixed across 0.81.0 and 0.86.0, and the gap between them is the lesson.** The
`/share` mirror keeps only the last four digits of anything longer than an
extension and marks the row redacted; the private `/data` ledger keeps the number
in full, and the one historical row already in the mirror was remediated.

That was 0.81.0, and it was only half. The same number reached the same directory
by a second route: the per-call hangup hook also writes a human-readable
`Verbose` line, and `/share/switchboard/asterisk.log` takes the full verbose
class. Measured on the live system after the "fix": **14 such lines carrying 2
distinct telephone numbers.** Redacting one writer and not the other left the
disclosure intact and the record of it looking closed. 0.86.0 truncates the
number in that line too, on the same six-digit rule.

> The commands in this document print raw ledger rows, which contain the data
> described above. Read them on the machine; do not paste the output anywhere.
