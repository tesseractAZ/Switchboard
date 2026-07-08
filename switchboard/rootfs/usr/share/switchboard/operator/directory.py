"""Directory-assistance helpers — pure, unit-tested (stdlib only, musl-safe).

The dial-411 directory service (switchboard-directory.agi) looks a room up by
voice: say a name, hear its extension, get connected. These helpers are the
data-shaping bits (no AGI I/O), so the exact spoken phrasing and the
list/cancel detection are testable without a phone. Room resolution itself
reuses the shared, unit-tested ``match`` matcher.
"""
from __future__ import annotations

import re

# Ask to hear the whole directory read out.
_LIST_PHRASES = (
    "list", "directory", "everyone", "everybody", "all the rooms", "all rooms",
    "what are the rooms", "which rooms", "the rooms", "read the list", "options",
)
# Bail out of the flow.
_CANCEL_PHRASES = (
    "cancel", "never mind", "nevermind", "goodbye", "good bye", "bye", "stop",
    "quit", "exit", "nothing", "hang up",
)


def _norm(text: str) -> str:
    low = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()) + " "
    return re.sub(r"\s+", " ", low)


def is_list_request(text: str) -> bool:
    low = _norm(text)
    return any(f" {p} " in low for p in _LIST_PHRASES)


def is_cancel(text: str) -> bool:
    low = _norm(text)
    return any(f" {p} " in low for p in _CANCEL_PHRASES)


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
