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
own retry for MISSED wake-ups (wakeup/scheduler.py, `_pending_missed`); its
undelivered-wake-up card, the pollers' alerts and anything later use this one.

HOW. `send()` posts at once and, if Home Assistant refuses, keeps the
notification keyed by its `notification_id`. `flush()`, called at the top of
each cycle, posts what is kept, oldest first.

- Keying by id copies Home Assistant's own rule that a notification REPLACES an
  earlier one with the same id, so a "recovered" sent while a "critical" is
  still undelivered replaces it and a stale "critical" can never land after its
  recovery. The replaced alert is not dropped silently: the message that
  replaces it names it, and when it was raised.
- An alert is dropped only when Home Assistant has REFUSED a retry and the
  alert is more than RETRY_FOR seconds past its OWN raise. Not the first
  refusal of its id: the newest alert for an id is the current state, and it
  must get its full window even if the id was refused an hour earlier. Not
  before a retry: a poller that cycles once an hour must still get one.
- Ages are measured on the monotonic clock, which an NTP step after boot does
  not move; the wall clock is used only for the "written at" time in the text.
- The queue lives in memory: an add-on restart starts it empty, as the
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
# Replaced alerts kept per id: the first (where the episode began) and the
# newest few, with a count of the ones between.
MISSED_KEEP = 4

_pending: dict[str, dict] = {}


def _log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", flush=True)


def _clocks(now: float | None) -> tuple[float, float]:
    """(wall time for the text, monotonic time for ages). A test passes `now`
    and gets it for both."""
    if now is not None:
        return now, now
    return time.time(), time.monotonic()


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
        # "written at", not "raised at": the message may say "the phones are up
        # now", and that was true when it was written, not when it lands.
        notes.append(f"Sent late: written at {_hhmm(item['raised'])}, while Home "
                     "Assistant was not accepting notifications.")
    missed, omitted = item["missed"], item["omitted"]
    if missed:
        named = [f"{_short_title(t)} at {_hhmm(ts)}" for t, ts in missed]
        if omitted:
            named.insert(1, f"{omitted} more")
        notes.append(f"Not delivered at the time: {', '.join(named)}.")
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
    wall, mono = _clocks(now)
    key = notification_id or f"(no id) {title} {wall}"
    prior = _pending.pop(key, None)
    missed, omitted = [], 0
    if prior is not None:
        missed = prior["missed"] + [(prior["title"], prior["raised"])]
        omitted = prior["omitted"]
        if len(missed) > MISSED_KEEP:
            omitted += len(missed) - MISSED_KEEP
            missed = missed[:1] + missed[-(MISSED_KEEP - 1):]
    item = {"nid": notification_id, "message": message, "title": title,
            "raised": wall, "age_from": mono, "missed": missed, "omitted": omitted}
    if _post(ha, item, late=False):
        return True
    if len(_pending) >= MAX_PENDING:
        oldest = min(_pending, key=lambda k: _pending[k]["age_from"])
        _log(tag, f"notification queue full; dropping the oldest undelivered "
                  f"'{_pending[oldest]['title']}'")
        del _pending[oldest]
    _pending[key] = item
    _log(tag, f"Home Assistant did not accept '{title}'; kept and retried each "
              f"cycle until {RETRY_FOR // 60} min after it was raised")
    return False


def flush(ha, *, tag: str = "notify", now: float | None = None) -> int:
    """Post the kept notifications, oldest first, and return how many landed.

    Stops at the first refusal: if Home Assistant is still not accepting, every
    other attempt would fail the same way. That refusal is also what drops an
    alert past its window, so nothing is dropped in a cycle where Home Assistant
    answers, and nothing is dropped before it has had a retry."""
    _wall, mono = _clocks(now)
    posted = 0
    for key in sorted(_pending, key=lambda k: _pending[k]["age_from"]):
        item = _pending[key]
        if _post(ha, item, late=True):
            del _pending[key]
            posted += 1
            _log(tag, f"posted '{item['title']}' {int(mono - item['age_from'])} s late")
            continue
        for k in list(_pending):
            if mono - _pending[k]["age_from"] > RETRY_FOR:
                _log(tag, f"'{_pending[k]['title']}' still not accepted "
                          f"{RETRY_FOR // 60} min after it was raised; giving up")
                del _pending[k]
        break
    return posted


def pending() -> list[dict]:
    """A copy of what is kept, for tests and diagnostics."""
    return [dict(v) for v in _pending.values()]


def clear() -> None:
    _pending.clear()
