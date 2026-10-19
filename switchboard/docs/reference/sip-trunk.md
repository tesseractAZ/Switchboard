# Adding an outside line (SIP trunk)

1. Sign up with a SIP-trunk provider (host, username, password, a DID).
2. In **Configuration**, set:
   ```yaml
   trunk:
     enabled: true
     provider_host: losangeles.voip.ms
     port: 5060
     username: "100000_sub"
     secret: "provider-password"
     from_domain: losangeles.voip.ms
     outbound_caller_id: "12135550123"
     inbound_ext: "19"        # ring one room, or "19,20", or "" for the whole house
     dial_prefix: "9"         # or set direct_dial: true to drop the prefix (below)
     registns: true
   ```
3. **Restart** the add-on.
4. **Outbound**: dial `9` then the number. Prefer dialing **without** a prefix?
   Set `direct_dial: true` — then you dial **`1` + the 10-digit number** (like a
   cell), while your 2–3-digit extensions and feature codes still ring internally.
   The leading `1` is required (a bare 10-digit number won't dial out); that's what
   keeps extension 20 and every feature code except `41` (which overlaps `411`,
   §7.4) dialing instantly. `011` international and
   `1-900` premium stay blocked, and **911 is not routed** (no E911 — use a cell for
   emergencies). Don't try to disable the prefix by blanking `dial_prefix`; the
   options form reverts a cleared field to its default, so use the `direct_dial`
   toggle. On analog phones, extensions 12–19 send after a brief pause (or press
   `#`) because they start with `1` like an outside number does.
5. **Inbound**: rings the `inbound_ext` room(s), or every room if blank. Outside
   calls ring with a **distinctive ring** on the WP826 cordless (an
   `Alert-Info: …;info=outsideline` tag; analog handsets ignore it).

With `enabled: false`, none of the trunk config is emitted and the PBX is purely
room-to-room.

## Toll-fraud protection

The trunk is where the internet meets your phone bill, so several defenses are
layered on automatically (details in [SECURITY.md](SECURITY.md#toll-fraud-the-trunk-threat-model)):

- **Blocked prefixes** — international (`011`) and premium (`900`, `1-900`) are
  rejected before any outbound rule.
- **Inbound calls get no in-call feature codes** — an outside caller can't key
  `##` to reach an outbound path.
- **Transfers are internal-only** — a transferred outside caller lands in a context
  with no outbound rule at all.
- **Provider-initiated transfers (REFER) are rejected.**
- The trunk **re-registers every 120 s** to hold the router's NAT pinhole open
  (many providers, e.g. VoIP.ms, don't answer keep-alive OPTIONS reliably, so the
  AOR is deliberately not qualified).

## Emergency calls are not carried

**This PBX cannot reach 911.** The trunk provider has no service address
registered for it, so an emergency call could not be routed to the correct
dispatcher even if it completed. Dial `911` from any house phone and Switchboard
answers immediately with a spoken notice telling the caller to hang up and use a
mobile — in **both** dial modes, and before any trunk pattern can match.

That explicit handling matters: in prefix mode the outbound pattern `_9.` also
matches `911`, and before v0.49.0 it would strip the prefix and dial the
remainder (`11`) out to the PSTN — a wrong call placed during an emergency.
Anyone relying on these phones should be told to keep a mobile for
emergencies.

## Registration resilience

The REGISTER refresh is the trunk's lifeline (§ NAT note above) — and losing it
used to be permanent: Asterisk's default `max_retries=10` means a WAN outage
longer than ~10 minutes drove the registration into a **terminal** "Rejected"
state that nothing ever retried. That exact failure occurred live: a 75-minute
WAN blip silently killed inbound calling for 24 hours (outbound was unaffected —
it authenticates per-INVITE). v0.48.0 closes it from three sides:

1. the generated `[trunk-reg]` sets `max_retries = 10000` (≈ 7 days of retrying)
   plus fatal/forbidden retry intervals, so the terminal state effectively
   cannot arise;
2. the link-health poller watches the registration every cycle, publishes
   `sensor.switchboard_trunk_health`, and **auto-sends a re-register** if it
   ever sees Rejected/Stopped (via the native `PJSIPRegister` AMI action —
   v0.48.0 used the `Command` CLI bridge, which none of the add-on's AMI
   accounts is authorised to run, so that recovery was inert until
   v0.49.0);
3. if the registration stays down for 2 consecutive cycles, a persistent
   notification fires. Recovery does **not** dismiss it: the poller posts a
   *second* notification ("the outside line re-registered") under the same
   `notification_id`, which **replaces** the outage entry in the notification
   list. So the bell is never left showing a stale "outside line down", but you
   are left with a recovery notice to dismiss yourself.

---
