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
| `0`   | **Operator** — say a room, an extension, or any feature ("lights", "wake me up", "what time is it", "weather", "directory", "announce", "page") | [§4](#4-the-voice-operator--directory-assistance) |
| `41`  | **Talking clock** | [§5](#5-wake-up-calls--the-talking-clock) |
| `42`  | **Wake-up call** — set or cancel by speaking the time | [§5](#5-wake-up-calls--the-talking-clock) |
| `43`  | **Home-automation voice menu** — control your lights | [§4](#4-the-voice-operator--directory-assistance) |
| `44`  | **Page all** — house-wide intercom | [§6](#6-paging--announcements) |
| `45`  | **Dial-a-status** — hear live Home Assistant readings | [§4](#4-the-voice-operator--directory-assistance) |
| `46`  | **Announce** — speak a message out your Home Assistant speakers | [§6](#6-paging--announcements) |
| `47`  | **Local voice assistant** — ask Home Assistant anything (*off by default*) | [§4](#4-the-voice-operator--directory-assistance) |
| `411` | **Directory assistance** | [§4](#4-the-voice-operator--directory-assistance) |

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
