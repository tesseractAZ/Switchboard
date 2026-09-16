"""Replacing a pending wake-up must leave a trace, from every setter.

    python3 -m pytest switchboard/tests/test_wakeup_replacement.py

★ 2026-09-15. Ext 19's 06:20 wake-up was set at 13:10:35Z and silently replaced
at 13:15:09Z by a 04:00 one — 4 minutes 51 seconds before the first was due. The
store keeps ONE entry per extension (`data[str(ext)] = entry`), so the 06:20
simply ceased to exist, and the delivery ledger showed `set 06:20` and then
nothing at all: no ring, no cancel, no second set.

That is indistinguishable from a wake-up the system LOST — which is the accusation
a reader of this ledger has to be able to rule out, because the alarm clock is the
one delivery path with a deadline. The whole point of recording who set a wake-up
and from where (v0.100.x) was to be able to reconstruct a morning; a replacement
is the one event in a wake-up's life that was still invisible.

THE FIX IS ONE ROW, not a new one. store.set_wakeup() hands back the entry it
displaced and delivery.record_wakeup_change() names both times on the `set` row
it already writes. A separate `replaced` row would double every replacement in
the ledger AND take it out of room_changed_wakeup()'s match — so a replacement
dialled on the ringing room's own phone, which is exactly what a snooze IS, would
stop counting as a change and the reconciler would go back to escalating at
somebody who had just spoken a new time into the handset.

★ EVERY TEST HERE DRIVES A REAL SETTER. There are three — the dial-42 AGI, the
dashboard's POST and the operator console's wake-up mode — and each reaches the
store through its own wrapper. Testing record_wakeup_change() directly would
prove only that the helper can do it; this repo has shipped a correct helper that
nothing called more than once.
"""
import asyncio
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHARE = ROOT / "rootfs" / "usr" / "share" / "switchboard"
AGI = ROOT / "rootfs" / "var" / "lib" / "asterisk" / "agi-bin" / "switchboard-wakeup.agi"


class _Setters:
    """The three real setters, all wired to one store and one ledger.

    Each caller imports `store` and `delivery` by BARE NAME, so the instances
    below are injected into sys.modules before each one is loaded — the same
    idiom test_wakeup_snooze.py uses to drive the AGI. sys.path and sys.modules
    are restored afterwards (conftest fails a test that leaks either).
    """

    def __init__(self, tmp_path):
        self.store = SourceFileLoader(
            "store_replacement", str(SHARE / "wakeup" / "store.py")).load_module()
        self.store.PATH = str(tmp_path / "wakeups.json")
        self.delivery = SourceFileLoader(
            "delivery_replacement", str(SHARE / "webui" / "delivery.py")).load_module()
        self.delivery.OUTCOME_PATH = str(tmp_path / "delivery-outcomes.jsonl")

        saved_mods = {k: sys.modules.get(k) for k in ("store", "delivery")}
        saved_path = list(sys.path)
        sys.modules["store"] = self.store
        sys.modules["delivery"] = self.delivery
        for p in (str(SHARE / "webui"), str(SHARE / "wakeup"),
                  str(SHARE / "console")):
            if p not in sys.path:
                sys.path.insert(0, p)
        try:
            self.agi = SourceFileLoader(
                "wakeup_agi_replacement", str(AGI)).load_module()
            self.app = SourceFileLoader(
                "app_replacement", str(SHARE / "webui" / "app.py")).load_module()
            self.console = SourceFileLoader(
                "console_replacement", str(SHARE / "console" / "console.py")).load_module()
        finally:
            sys.path[:] = saved_path
            for k, v in saved_mods.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

        # Each caller must be holding OUR store and OUR ledger, or every
        # assertion below would be reading a file nothing under test wrote.
        assert self.agi.store is self.store and self.agi.delivery is self.delivery
        assert (self.app.wakeup_store is self.store
                and self.app._delivery is self.delivery)
        assert (self.console.wakeup_store is self.store
                and self.console.delivery is self.delivery)

    # -- the three setters ------------------------------------------------- #
    def by_phone(self, ext, hhmm):
        """Dial 42 and speak a time: the real AGI, as the dialplan runs it."""
        self.agi.read_env = lambda: {"agi_channel": f"PJSIP/{ext}-0000000d"}
        self.agi.agi = lambda cmd: "200 result=0"
        self.agi.recognize = lambda attempt: hhmm
        self.agi.main()

    def by_web(self, ext, hhmm):
        """POST /api/wakeup/<ext>: the real handler, FastAPI absent."""
        class _Resp:
            def __init__(self, payload, status_code=200):
                self.payload, self.status_code = payload, status_code

        class _Req:
            headers = {}

            @staticmethod
            async def json():
                return {"hhmm": hhmm}

        app = self.app
        saved = {k: getattr(app, k) for k in
                 ("JSONResponse", "load_options", "configured_room_exts")}
        try:
            app.JSONResponse = _Resp
            app.load_options = lambda: {}
            app.configured_room_exts = lambda o: {str(ext)}
            resp = asyncio.run(app.api_wakeup_set(str(ext), _Req()))
        finally:
            for k, v in saved.items():
                setattr(app, k, v)
        assert getattr(resp, "status_code", 200) == 200, resp.payload
        return resp

    def by_console(self, ext, name, digits):
        """W, type a time, Enter — the real key handler on a real board."""
        console = self.console
        board = console.Board()
        board.set({"ami_ok": True, "ts": 1_789_000_000.0, "calls": [],
                   "rooms": [{"ext": str(ext), "label": name, "registered": True,
                              "device_state": "Not in use", "call_state": "",
                              "peer": "", "channel": ""}]})
        sess = {"sel": 0, "mode": "normal"}
        console.apply_key(sess, "W", board, lambda m: None)
        assert sess["mode"] == "wakeup" and sess.get("wakeup_ext") == str(ext)
        # W pre-fills the room's existing time so it can be edited, so the
        # operator clears it before typing a different one. Done with the real
        # backspace key rather than by poking the buffer.
        while sess.get("wakeup_buf"):
            console.apply_key(sess, "backspace", board, lambda m: None)
        for ch in digits:
            console.apply_key(sess, ch, board, lambda m: None)
        console.apply_key(sess, "enter", board, lambda m: None)
        assert sess["mode"] == "normal", sess

    # -- reading back ------------------------------------------------------- #
    def rows(self, **match):
        p = Path(self.delivery.OUTCOME_PATH)
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] \
            if p.exists() else []
        return [r for r in rows if all(r.get(k) == v for k, v in match.items())]

    def stored(self):
        return json.loads(Path(self.store.PATH).read_text())


@pytest.fixture
def setters(tmp_path):
    return _Setters(tmp_path)


# --------------------------------------------------------------------------- #
# 1. The incident, through each setter in turn.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("source, first, second", [
    ("phone", "06:20", "04:00"),
    ("web", "06:20", "04:00"),
    ("console", "06:20", "04:00"),
])
def test_a_replacement_names_the_time_it_displaced(setters, source, first, second):
    """★ THE INCIDENT. Two sets, one room, and the ledger has to show the first
    time going away rather than just stopping."""
    if source == "phone":
        setters.by_phone("19", first)
        setters.by_phone("19", second)
    elif source == "web":
        setters.by_web("19", first)
        setters.by_web("19", second)
    else:
        setters.by_console("19", "Bedroom", "620")
        setters.by_console("19", "Bedroom", "400")

    rows = setters.rows(ext="19", kind="wakeup")
    assert len(rows) == 2, rows
    opening, replacing = rows

    # The first set is untouched — a wake-up that displaced nothing must not
    # claim to have displaced something.
    assert opening["outcome"] == "set" and opening["hhmm"] == first
    assert "replaced_hhmm" not in opening and "replaced_target_epoch" not in opening

    # ...and the second names both times, on ONE row.
    assert replacing["outcome"] == "set", replacing
    assert replacing["hhmm"] == second
    assert replacing["replaced_hhmm"] == first
    assert replacing["replaced_target_epoch"] == opening["target_epoch"]
    assert replacing["source"] == source, (
        "a replacement must carry the same source the set carried")


def test_a_first_ever_set_is_byte_for_byte_the_row_it_always_was(setters):
    """The new fields are OMITTED, not written as null. A reader that has to tell
    "no previous wake-up" from "a previous wake-up with no time" has lost the
    distinction the omission exists to keep — and every historical row in the
    live ledger has this exact shape."""
    setters.by_phone("19", "06:20")
    row, = setters.rows(ext="19", kind="wakeup")
    assert set(row) == {"ts", "ext", "kind", "outcome", "source", "hhmm",
                        "target_epoch"}, row


def test_the_replacement_is_one_row_and_not_two(setters):
    """A separate `replaced` row is the obvious shape and it is wrong twice: it
    doubles every replacement, and it falls outside room_changed_wakeup()'s
    match — so a replacement dialled on the ringing room's own phone, which is
    what a snooze is, would stop being seen as a change at all."""
    setters.by_phone("19", "06:20")
    setters.by_phone("19", "04:00")
    rows = setters.rows(ext="19", kind="wakeup")
    assert [r["outcome"] for r in rows] == ["set", "set"], rows

    # ...and the snooze reader still sees the replacement as a change from the
    # room's own phone. This is the assertion a separate outcome would break.
    since = min(r["target_epoch"] for r in rows) - 86400
    seen = setters.delivery.room_changed_wakeup("19", since)
    assert seen is not None and seen["hhmm"] == "04:00", seen


# --------------------------------------------------------------------------- #
# 2. What must NOT have changed.
# --------------------------------------------------------------------------- #
def test_nothing_named_replaced_is_ever_written_to_the_store(setters):
    """★ The returned entry and the stored one are different objects on purpose.

    A `replaced` key in /data/state/wakeups.json would be read back by the next
    set and chain: entry two carries entry one, entry three carries both, and a
    file that is rewritten whole on every write grows the room's entire wake-up
    history. It also has to survive a THIRD set to prove the chain never starts.
    """
    setters.by_phone("19", "06:20")
    setters.by_phone("19", "04:00")
    setters.by_phone("19", "07:15")
    entry = setters.stored()["19"]
    assert set(entry) == {"hhmm", "target_epoch", "set_at"}, entry
    assert entry["hhmm"] == "07:15"
    # ...and the third row names the second time, not the first.
    assert setters.rows(ext="19", kind="wakeup")[-1]["replaced_hhmm"] == "04:00"


def test_a_set_for_another_room_replaces_nothing(setters):
    """One entry per EXTENSION. Ext 14 setting a wake-up must not be recorded as
    having displaced ext 19's — the store is keyed by room and the row has to
    agree with it."""
    setters.by_phone("19", "06:20")
    setters.by_phone("14", "05:50")
    row, = setters.rows(ext="14", kind="wakeup")
    assert "replaced_hhmm" not in row, row


def test_a_cancel_row_is_unchanged(setters):
    """The replacement lives on the SET row. A cancel displaces nothing — it
    removes — and its row must keep the shape the reconciler already reads."""
    setters.by_phone("19", "06:20")
    setters.by_phone("19", "CANCEL")
    cancel = setters.rows(ext="19", kind="wakeup")[-1]
    assert set(cancel) == {"ts", "ext", "kind", "outcome", "source", "removed"}
    assert (cancel["outcome"], cancel["removed"]) == ("cancelled", True)


def test_setting_the_same_time_again_is_still_a_replacement(setters):
    """Re-setting 06:20 over 06:20 moves the target a day forward if the first
    has passed, and in any case it is a person touching the alarm. Recording it
    as a first-ever set would say the room had no wake-up, which is false."""
    setters.by_phone("19", "06:20")
    setters.by_phone("19", "06:20")
    assert setters.rows(ext="19", kind="wakeup")[-1]["replaced_hhmm"] == "06:20"
