"""Directory-assistance helpers — pure, unit-tested (stdlib only, musl-safe).

The dial-411 directory service (switchboard-directory.agi) looks a room up by
voice: say a name, hear its extension, get connected. These helpers are the
data-shaping bits (no AGI I/O), so the exact spoken phrasing and the
list/cancel detection are testable without a phone. Room resolution itself
reuses the shared, unit-tested ``match`` matcher.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# Ask to hear the whole directory read out.
_LIST_PHRASES = (
    "list", "directory", "everyone", "everybody", "all the rooms", "all rooms",
    "what are the rooms", "which rooms", "the rooms", "read the list", "options",
)
# Bail out of the flow.
_CANCEL_PHRASES = (
    "cancel", "never mind", "nevermind", "goodbye", "good bye", "bye", "stop",
    "quit", "exit", "nothing", "hang up", "forget it", "no thanks", "no thank you",
)

# On a narrowband antique line whisper routinely mis-hears "list" as lift / least /
# last / wrist. The room matcher is FUZZY, so those mishearings used to clear the
# room threshold and CONNECT A CALL to a bedroom instead of reading the directory.
# We therefore accept a near-miss of the word "list" as a list request — but only
# for a token long enough that it can't be an ordinary short word (ratio("is",
# "list") is 0.67, so the min-length guard keeps stopwords out), and always AFTER a
# strong room match has had first refusal (see resolve()).
_LIST_FUZZ = 0.66
_LIST_FUZZ_MINLEN = 4
_STRONG_MATCH = 0.9   # an unambiguous room name beats every intent word


def _norm(text: str) -> str:
    low = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()) + " "
    return re.sub(r"\s+", " ", low)


def _fuzzy_list_token(tok: str) -> bool:
    if tok.startswith("list"):        # list, lists, listing
        return True
    return len(tok) >= _LIST_FUZZ_MINLEN and SequenceMatcher(None, tok, "list").ratio() >= _LIST_FUZZ


def is_list_request(text: str) -> bool:
    low = _norm(text)
    if any(f" {p} " in low for p in _LIST_PHRASES):
        return True
    return any(_fuzzy_list_token(t) for t in low.split())


def is_cancel(text: str) -> bool:
    low = _norm(text)
    return any(f" {p} " in low for p in _CANCEL_PHRASES)


def resolve(text: str, match_fn):
    """Decide what a 411 caller wants, failing SAFE (never connect on doubt).

    ``match_fn(text) -> (ext, score, reason)`` is the shared room matcher. Returns
    one of ('connect', ext) | ('list', None) | ('cancel', None) | ('reprompt', None).

    Order matters and is the whole fix for the mis-heard-"list"-dials-a-room bug:
      1. an UNAMBIGUOUS room match (score >= _STRONG_MATCH) wins outright, so a room
         literally named "List"/"Cancel"/"Office" is still reachable;
      2. otherwise a cancel word bails;
      3. otherwise a (fuzzy) list request reads the directory — this catches the
         lift/least/last mishearings that previously fell through to a weak room dial;
      4. otherwise a weak-but-real room match connects;
      5. otherwise re-prompt. Nothing here connects a call on a low-confidence guess.
    """
    ext, score, _reason = match_fn(text)
    if ext and score >= _STRONG_MATCH:
        return ("connect", ext)
    if is_cancel(text):
        return ("cancel", None)
    if is_list_request(text):
        return ("list", None)
    if ext:
        return ("connect", ext)
    return ("reprompt", None)


def announce_text(name: str, ext: str) -> str:
    """What the operator speaks for a single resolved room."""
    return f"{name}, extension {ext}."


def directory_text(rooms: list) -> str:
    """The full directory read-out: 'The rooms are: Kitchen, extension 11. ...'.

    ``rooms`` is a list of {'ext','name'} dicts (as staged in operator.json)."""
    parts = [f"{r.get('name') or r.get('ext')}, extension {r.get('ext')}"
             for r in rooms if r.get("ext")]
    if not parts:
        return "The directory is empty."
    return "The rooms are: " + ". ".join(parts) + "."


def name_for(rooms: list, ext: str) -> str:
    """ext -> room name (falls back to the ext itself)."""
    for r in rooms:
        if str(r.get("ext")) == str(ext):
            return r.get("name") or str(ext)
    return str(ext)
