"""Keep the Home Assistant notifications that Home Assistant did not accept, and
post them once it does.

WHY. The device-health poller (devhealth) and the link-health poller (rtpmon)
raise an alert ONCE, on a state change: the cordless goes critical, the phones
become unreachable, the outside line drops. `ha_client.notify()` returns False
when Home Assistant does not accept the notification, and both pollers ignored
it. A host reboot on 2026-11-07 showed the cost: Home Assistant Core was not
answering from 19:00 to 19:12 MST, the add-on was already up, and three alerts
(cordless critical, all phones dropped, cordless recovered) got a 502 and were
gone. The phones recovered inside the outage, so nothing was missed that time;
but a phone that went down during a reboot and STAYED down would never have
raised an alert at all, because its transition had already happened. The
wake-up scheduler hit the same gap after the 2026-10-04 power cut and keeps its
own retry (wakeup/scheduler.py, `_pending_missed`); this module is the same idea
for the pollers.

HOW. `send()` posts at once and, if Home Assistant refuses, keeps the
notification keyed by its `notification_id`. `flush()`, called at the top of
each poll cycle, posts what is kept. Keying by id copies Home Assistant's own
rule that a notification REPLACES an earlier one with the same id, so a
"recovered" sent while a "critical" is still undelivered replaces it and a
stale "critical" can never land after its recovery. The replaced alert is not
dropped silently: the message that replaces it says it was not delivered, and
when. Anything still undelivered after RETRY_FOR seconds is dropped with a log
line. The queue lives in memory: an add-on restart starts it empty, as the
scheduler's does.

The client is passed in rather than imported, so the module works with
anything that has `notify(message, title=, notification_id=) -> bool`.
"""
from __future__ import annotations

import os
import time


def _env_int(name: str, default: int) -> int:
    try:
        return max(60, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


# Same default window as the scheduler's missed-wake-up retry.
RETRY_FOR = _env_int("SWITCHBOARD_NOTIFY_RETRY_SECONDS", 3600)
# Bound on kept notifications. Most ids are fixed (one per device or line), but
# the mass-drop alert uses a fresh id per event, so a long outage could add one
# per cycle; the oldest is dropped first.
MAX_PENDING = 20
# ha_client.notify() cuts a message at 500 characters, and a note at the end
# would be the first thing lost, so the main text is shortened instead.
MESSAGE_LIMIT = 500
# How many replaced alerts one message names (newest last).
MISSED_SHOWN = 3

_pending: dict[str, dict] = {}


def _log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", flush=True)


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _short_title(title: str) -> str:
    t = (title or "").strip()
    return t[len("Switchboard: "):] if t.startswith("Switchboard: ") else t


def compose(item: dict, late: bool) -> str:
    """The text to post: the message itself, then a note saying it is late and
    naming any alert it replaced that was never delivered."""
    notes = []
    if late:
        notes.append(f"Sent late: raised at {_hhmm(item['raised'])}, while Home "
                     "Assistant was not accepting notifications.")
    if item["missed"]:
        shown = ", ".join(f"{_short_title(t)} at {_hhmm(ts)}"
                          for t, ts in item["missed"][-MISSED_SHOWN:])
        notes.append(f"Not delivered at the time: {shown}.")
    text = str(item["message"])
    if not notes:
        return text[:MESSAGE_LIMIT]
    tail = "\n\n" + " ".join(notes)
    room = MESSAGE_LIMIT - len(tail)
    if len(text) > room:
        text = text[:max(0, room - 1)].rstrip() + "…"
    return (text + tail)[:MESSAGE_LIMIT]


def _post(ha, item: dict, late: bool) -> bool:
    try:
        return bool(ha.notify(compose(item, late), title=item["title"],
                              notification_id=item["nid"]))
    except Exception:  # noqa: BLE001 — a client error is a refusal, never a crash
        return False


def send(ha, message: str, title: str = "Switchboard", notification_id: str = "",
         *, tag: str = "notify", now: float | None = None) -> bool:
    """Post a notification now; keep it for `flush()` if Home Assistant refuses.

    Returns True only if Home Assistant accepted it on this call."""
    now = time.time() if now is None else now
    key = notification_id or f"(no id) {title} {now}"
    prior = _pending.pop(key, None)
    item = {
        "nid": notification_id,
        "message": message,
        "title": title,
        "raised": now,
        # The give-up clock runs from the FIRST refusal of this id, so an alert
        # that keeps flapping while Home Assistant is down still ends.
        "first": prior["first"] if prior else now,
        "missed": (prior["missed"] + [(prior["title"], prior["raised"])]) if prior else [],
    }
    if _post(ha, item, late=False):
        return True
    if len(_pending) >= MAX_PENDING:
        oldest = min(_pending, key=lambda k: _pending[k]["raised"])
        _log(tag, f"notification queue full; dropping the oldest undelivered "
                  f"'{_pending[oldest]['title']}'")
        del _pending[oldest]
    _pending[key] = item
    _log(tag, f"Home Assistant did not accept '{title}'; kept, retried every cycle "
              f"for up to {RETRY_FOR // 60} min")
    return False


def flush(ha, *, tag: str = "notify", now: float | None = None) -> int:
    """Post the kept notifications, oldest first. Stops at the first refusal:
    if Home Assistant is still down, every other attempt would fail the same way.
    Returns how many were posted."""
    now = time.time() if now is None else now
    posted = 0
    for key in sorted(_pending, key=lambda k: _pending[k]["raised"]):
        item = _pending[key]
        if now - item["first"] > RETRY_FOR:
            _log(tag, f"'{item['title']}' still not accepted after "
                      f"{RETRY_FOR // 60} min; giving up")
            del _pending[key]
            continue
        if not _post(ha, item, late=True):
            break
        del _pending[key]
        posted += 1
        _log(tag, f"posted '{item['title']}' {int(now - item['raised'])} s late")
    return posted


def pending() -> list[dict]:
    """A copy of what is kept, for tests and diagnostics."""
    return [dict(v) for v in _pending.values()]


def clear() -> None:
    _pending.clear()
