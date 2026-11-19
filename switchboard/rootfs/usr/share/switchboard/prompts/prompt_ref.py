"""Which recording a Switchboard prompt plays: the built-in one or a custom one.

v0.120.0. A prompt file dropped into the add-on's config folder
(``/addon_configs/<slug>/prompts``, ``/config/prompts`` inside the container)
replaces the built-in recording of the same name. switchboard-config validates
those files at every start and installs the ones it accepts into PROMPTS_DIR
below; the shipped recordings under ``/var/lib/asterisk/sounds/en/switchboard``
are never touched (the AppArmor profile write-locks that tree).

★ ONE ANSWER FOR EVERYTHING THAT PLAYS OR MEASURES A PROMPT. The generated
dialplan, every AGI, the AGI helpers, the web UI's test ring and the talking
clock all ask ``prompt()``, so a name plays from one place everywhere. The clock
matters most: it times its tone from the LENGTH of the files it plays, so if it
played a custom recording but measured the built-in one, the tone would land off
the second it announced.

★ A WHOLE NAME RESOLVES TO ONE DIRECTORY. Asterisk picks the cheapest format it
finds for a sound name, and for the phones' µ-law channel a ``.ulaw`` beats a
``.wav``. A custom ``.wav`` placed next to the built-in ``.ulaw`` would therefore
never play. Returning an absolute path (Asterisk uses a name starting with "/"
as-is) makes the custom directory the only candidate for that name, and every
installed name has a ``.ulaw`` there (switchboard-config derives one from a
``.wav`` given alone), which is also the file the clock measures.

stdlib only; runs as root (switchboard-config, the web UI) and as the asterisk
user (the AGIs). Never raises.
"""
from __future__ import annotations

import os
import re

# Written only by switchboard-config, as root, at every start. /run is rebuilt
# with the container at every add-on start, so a custom prompt removed from the
# config folder is gone after the next restart.
PROMPTS_DIR = "/run/switchboard/prompts"

# The built-in recordings, as Asterisk names them: relative to sounds/en/.
BUILTIN_PREFIX = "switchboard/"

# Every built-in prompt is named like this, and nothing that could leave
# PROMPTS_DIR ("/", "..") can match it.
NAME_RE = re.compile(r"sw-[a-z0-9]+(?:-[a-z0-9]+)*")


def prompt(name: str) -> str:
    """The sound reference to play for prompt `name` (for example ``sw-greeting``).

    ``/run/switchboard/prompts/<name>`` when a custom recording of that name was
    installed at start, otherwise the built-in ``switchboard/<name>``. Both are
    given without an extension, the way Playback and STREAM FILE take them."""
    try:
        if NAME_RE.fullmatch(name) and os.path.isfile(
                os.path.join(PROMPTS_DIR, name + ".ulaw")):
            return os.path.join(PROMPTS_DIR, name)
    except (TypeError, ValueError, OSError):
        pass
    return BUILTIN_PREFIX + str(name)
