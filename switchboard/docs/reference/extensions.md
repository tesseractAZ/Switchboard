# Extensions & feature codes

## Room extensions

An extension is **any 2–6 digit number you choose**, one per phone; they only need
to be unique. Pick a scheme that leaves room for the feature codes. The reference
home wires its eight GXW FXS ports to **`11`–`18`** and its WiFi cordless to
**`19`** (this matches the `gateway_ports` default). A `1xx` scheme (`101`, `102`,
…) works just as well — the shipped placeholder rooms use it.

Whatever you pick, keep the single-digit `0` for the operator and (if you use a
trunk) a leading digit like `9` for the outside line, and don't collide with the
feature codes below.

## Feature codes

Dial these from any room phone. All are configurable (`*_ext`) and can be disabled
(`*_enabled`); the defaults are:

| Dial | Feature | Section |
|-----:|---------|---------|
| `0`   | **Operator** — say a room, an extension, or any feature ("lights", "wake me up", "what time is it", "weather", "directory", "announce", "page") | [Operator](voice-operator.md#operator--dial-0) |
| `41`  | **Talking clock** | [Talking clock](wake-ups-and-clock.md#talking-clock--dial-41) |
| `42`  | **Wake-up call** — set or cancel by speaking the time | [Wake-up calls](wake-ups-and-clock.md#wake-up-calls--dial-42) |
| `43`  | **Home-automation voice menu** — control your lights | [Home-automation voice menu](voice-operator.md#home-automation-voice-menu--dial-43-or-say-lights-to-the-operator) |
| `44`  | **Page all** — house-wide intercom | [Page all](paging-and-announcements.md#page-all--dial-44) |
| `45`  | **Dial-a-status** — hear live Home Assistant readings | [Dial-a-status](voice-operator.md#dial-a-status--dial-45) |
| `46`  | **Announce** — speak a message out your Home Assistant speakers | [Announcements](paging-and-announcements.md#announcements--dial-46-phone--home-assistant-speakers) |
| `47`  | **Local voice assistant** — ask Home Assistant anything (*off by default*) | [Local voice assistant](voice-operator.md#local-voice-assistant--dial-47) |
| `411` | **Directory assistance** | [Directory assistance](voice-operator.md#directory-assistance--dial-411) |

A feature's dial code is skipped (with a log line) if it collides with a room
extension or another code, but the underlying feature usually still works via
the operator, the scheduler, or the dashboard. Two exceptions: the talking
clock is fully disabled on a collision, and a collided page code stays
reachable only from the dashboard/console (the operator's "page" route is
dropped with it). The local voice assistant is also fully disabled on a
collision: nothing else routes into it, so a context with no dial code would be
unreachable dialplan that still reads as a working feature.

## In-call transfer (analog phones)

On a live call, an analog phone can blind-transfer with **`##`** and
attended-transfer with **`*2`**. Transfers are confined to internal destinations —
you cannot transfer a call out to the trunk. IP phones (like the cordless) use
their own Transfer button. A `*2` consult call to the operator counts as an
outside line for the house features and the message-waiting clear (see the
operator below): to use those, dial `0` directly.

---
