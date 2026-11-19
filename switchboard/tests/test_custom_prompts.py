"""Custom prompts (v0.120.0): recordings in the add-on's config folder replace
the built-in ones, and everything that plays or measures a prompt asks ONE
resolver which recording that is.

    python3 -m pytest switchboard/tests/test_custom_prompts.py

What is pinned here:

  * switchboard-config installs only files that pass every check, each refusal
    is one log line, and a refused name keeps its built-in prompt (the 911
    notice included): no file in the wrong format, no silent one and no other
    container named .ulaw can replace a prompt;
  * a .wav given alone gets a G.711 µ-law sibling, so it plays in the phones'
    codec and the talking clock can measure it;
  * anything unexpected leaves every prompt built-in and config generation
    completes;
  * the dialplan, the AGIs, their helpers, the web UI's test ring and the
    clock's measurement all resolve through prompt_ref.prompt(), and a
    source scan keeps every future reference on that path.
"""
import contextlib
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

ADDON = Path(__file__).resolve().parents[1]
ROOTFS = ADDON / "rootfs"
SBC_PATH = ROOTFS / "usr" / "bin" / "switchboard-config"
PROMPT_REF_PATH = ROOTFS / "usr" / "share" / "switchboard" / "prompts" / "prompt_ref.py"
CLOCK_SPEAK = ROOTFS / "usr" / "share" / "switchboard" / "clock" / "clock_speak.py"
SPEECH = ROOTFS / "usr" / "share" / "switchboard" / "operator" / "agi_speech.py"
AMI = ROOTFS / "usr" / "share" / "switchboard" / "webui" / "ami.py"
STORE = ROOTFS / "usr" / "share" / "switchboard" / "wakeup" / "store.py"
AGI = ROOTFS / "var" / "lib" / "asterisk" / "agi-bin"
BUNDLED = ROOTFS / "var" / "lib" / "asterisk" / "sounds" / "en" / "switchboard"

sbc = SourceFileLoader("switchboard_config_prompts", str(SBC_PATH)).load_module()

NAMES = sorted({p.stem for p in BUNDLED.iterdir() if p.suffix in (".wav", ".ulaw")})
FOLDER = "/app_configs/0123abcd_switchboard"

_failures = 0


def check(name: str, cond: bool) -> None:
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1
    assert cond, name


# --------------------------------------------------------------------------- #
# Audio fixtures. The µ-law values are audioop.lin2ulaw's own output for these
# samples (CPython 3.9), the G.711 coding the image's 3.12 uses.
# --------------------------------------------------------------------------- #
G711 = {0: 0xFF, 4: 0xFE, -1: 0x7E, 100: 0xF2, 255: 0xE7, 1000: 0xCE, -1000: 0x4E,
        2048: 0xBE, 8191: 0x9F, -8192: 0x1F, 16384: 0x8F, 32767: 0x80, -32768: 0x00}
# Either side of every segment boundary, both signs: where a wrong segment
# table or bias changes the code.
G711.update({120: 0xF0, 124: 0xEF, -120: 0x70, -124: 0x6F, 376: 0xE0, 380: 0xDF,
             -376: 0x60, -380: 0x5F, 888: 0xD0, 892: 0xCF, -888: 0x50, -892: 0x4F,
             1912: 0xC0, 1916: 0xBF, -1912: 0x40, -1916: 0x3F, 3960: 0xB0, 3964: 0xAF,
             -3960: 0x30, -3964: 0x2F, 8056: 0xA0, 8060: 0x9F, -8056: 0x20, -8060: 0x1F,
             16248: 0x90, 16252: 0x8F, -16248: 0x10, -16252: 0x0F})
SAMPLES = list(G711) * 20                     # 820 samples, just over 0.1 s


def _pcm(samples=SAMPLES) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _wav(pcm: bytes = None, rate=8000, channels=1, bits=16, tag=1, extra=b"",
         data_size=None) -> bytes:
    pcm = _pcm() if pcm is None else pcm
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * channels * bits // 8,
                      channels * bits // 8, bits)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + extra
            + b"data" + struct.pack("<I", len(pcm) if data_size is None else data_size) + pcm)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _ulaw(n: int = 2000, byte: int = 0x55) -> bytes:
    """n bytes of raw µ-law: `byte` and its mirror image in turn, a square wave
    (0xFF is silence either way)."""
    return bytes([byte, byte ^ 0x80] * (n // 2 + 1))[:n]


def _resolver(prompts_dir):
    """A private copy of the real resolver, pointed at `prompts_dir`."""
    mod = SourceFileLoader(f"prompt_ref_{id(prompts_dir)}", str(PROMPT_REF_PATH)).load_module()
    mod.PROMPTS_DIR = str(prompts_dir)
    return mod


@contextlib.contextmanager
def _box():
    """A config folder and an install directory of their own, and the log."""
    root = Path(tempfile.mkdtemp(prefix="sw-prompts-"))
    src = root / "config" / "prompts"
    dest = root / "run" / "switchboard" / "prompts"
    src.mkdir(parents=True)
    saved = (sbc.PROMPTS_SRC, sbc.PROMPT_REF.PROMPTS_DIR, sbc.log, sbc.addon_config_folder)
    logs = []
    sbc.PROMPTS_SRC, sbc.PROMPT_REF.PROMPTS_DIR = src, str(dest)
    sbc.log = logs.append
    sbc.addon_config_folder = lambda: FOLDER
    try:
        yield types.SimpleNamespace(root=root, src=src, dest=dest, logs=logs)
    finally:
        sbc.PROMPTS_SRC, sbc.PROMPT_REF.PROMPTS_DIR, sbc.log, sbc.addon_config_folder = saved
        shutil.rmtree(root, ignore_errors=True)


@contextlib.contextmanager
def _injected(resolver, **extra):
    """Make `import prompt_ref` (and any `extra` bare names) resolve to these
    modules while an add-on module loads, as they would in the image."""
    mods = {"prompt_ref": resolver, **extra}
    saved = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _summary(logs):
    lines = [line for line in logs if line.startswith("prompts: ") and " custom from " in line]
    assert len(lines) == 1, logs
    return lines[0]


# --------------------------------------------------------------------------- #
# The resolver.
# --------------------------------------------------------------------------- #
def test_the_resolver_answers_custom_only_for_an_installed_name():
    d = Path(tempfile.mkdtemp())
    try:
        pr = _resolver(d)
        check("resolver: nothing installed -> the built-in reference",
              pr.prompt("sw-greeting") == "switchboard/sw-greeting")
        (d / "sw-greeting.ulaw").write_bytes(_ulaw())
        check("resolver: an installed name -> its absolute path, without extension",
              pr.prompt("sw-greeting") == str(d / "sw-greeting"))
        check("resolver: another name stays built-in",
              pr.prompt("sw-busy") == "switchboard/sw-busy")
        (d / "sw-busy.wav").write_bytes(_wav())
        check("resolver: a name with no .ulaw there stays built-in",
              pr.prompt("sw-busy") == "switchboard/sw-busy")
        (d / "sw-goodbye.ulaw").mkdir()
        check("resolver: a directory is not an installed prompt",
              pr.prompt("sw-goodbye") == "switchboard/sw-goodbye")
        # Nothing that could leave the directory is ever made absolute.
        (d / "x.ulaw").write_bytes(_ulaw())
        for odd in ("sw-../x", "../sw-greeting", "sw-greeting/../x", "SW-GREETING", "x", ""):
            check(f"resolver: {odd!r} is never resolved into the directory",
                  not pr.prompt(odd).startswith("/"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_the_install_directory_is_the_one_the_resolver_reads():
    pr = SourceFileLoader("prompt_ref_defaults", str(PROMPT_REF_PATH)).load_module()
    check("resolver: installs under /run/switchboard (rebuilt at every start)",
          pr.PROMPTS_DIR == "/run/switchboard/prompts" == str(sbc.RUN_DIR / "prompts"))
    check("config: installs where its own resolver looks",
          sbc.PROMPT_REF is not None and sbc.PROMPT_REF.PROMPTS_DIR == pr.PROMPTS_DIR)
    check("config: reads the add-on's own config folder", str(sbc.PROMPTS_SRC) == "/config/prompts")
    check("config: the allow-list is the shipped set", sorted(sbc.bundled_prompt_names()) == NAMES)
    check("the shipped set is the 28 prompts", len(NAMES) == 28 and "sw-no-emergency" in NAMES)


# --------------------------------------------------------------------------- #
# What gets installed.
# --------------------------------------------------------------------------- #
def test_a_wav_alone_installs_with_a_derived_g711_ulaw():
    with _box() as b:
        # A metadata chunk before the audio, as recorders write.
        (b.src / "sw-greeting.wav").write_bytes(_wav(extra=b"LIST" + struct.pack("<I", 4) + b"INFO"))
        sbc.install_custom_prompts()
        ulaw = (b.dest / "sw-greeting.ulaw").read_bytes()
        check("wav-only: a .ulaw is derived", len(ulaw) == len(SAMPLES))
        check("wav-only: the derived .ulaw is G.711 µ-law",
              ulaw == bytes(G711[s] for s in SAMPLES))
        wav = (b.dest / "sw-greeting.wav").read_bytes()
        check("wav-only: the .wav is installed with the same audio under a plain header",
              wav[:4] == b"RIFF" and wav[8:16] == b"WAVEfmt " and wav[36:40] == b"data"
              and wav[44:] == _pcm() and sbc.wav_pcm(wav) == (_pcm(), ""))
        check("wav-only: the dialplan's resolver now answers the custom path",
              sbc.prompt("sw-greeting") == str(b.dest / "sw-greeting"))
        check("wav-only: the files are root-written and readable by Asterisk (uid 100)",
              stat.S_IMODE((b.dest / "sw-greeting.ulaw").stat().st_mode) == 0o644
              and stat.S_IMODE((b.dest / "sw-greeting.wav").stat().st_mode) == 0o644
              and stat.S_IMODE(b.dest.stat().st_mode) == 0o755)
        check("wav-only: one summary line naming the folder as Home Assistant shows it",
              _summary(b.logs) == f"prompts: 1 custom from {FOLDER}/prompts, "
                                  f"{len(NAMES) - 1} built-in")
        check("wav-only: nothing else is logged", len(b.logs) == 1)
        check("wav-only: no build directory is left behind",
              [p.name for p in b.dest.parent.iterdir()] == ["prompts"])


def test_the_install_directory_holds_prompt_files_and_nothing_else():
    """Asterisk tries <dir>/<language>/<name> before <dir>/<name> for an
    absolute name, so a subdirectory there could shadow an installed prompt:
    the install is regular files named <prompt>.wav|ulaw only."""
    with _box() as b:
        (b.src / "sw-greeting.wav").write_bytes(_wav())
        (b.src / "sw-busy.ulaw").write_bytes(_ulaw())
        (b.src / "sw-goodbye.wav").write_bytes(_wav())
        (b.src / "sw-goodbye.ulaw").write_bytes(_ulaw())
        (b.src / "en").mkdir()
        (b.src / "en" / "sw-greeting.ulaw").write_bytes(_ulaw())
        sbc.install_custom_prompts()
        entries = sorted(os.listdir(b.dest))
        check(f"install dir: exactly the prompt files ({entries})",
              entries == ["sw-busy.ulaw", "sw-goodbye.ulaw", "sw-goodbye.wav",
                          "sw-greeting.ulaw", "sw-greeting.wav"])
        check("install dir: each a regular file, none a link or directory",
              all(stat.S_ISREG(os.lstat(b.dest / e).st_mode) for e in entries))


def test_a_ulaw_alone_installs_as_given():
    with _box() as b:
        body = _ulaw(1234, 0x2A)
        (b.src / "sw-no-emergency.ulaw").write_bytes(body)
        sbc.install_custom_prompts()
        check("ulaw-only: installed byte for byte", (b.dest / "sw-no-emergency.ulaw").read_bytes() == body)
        check("ulaw-only: no .wav is invented", not (b.dest / "sw-no-emergency.wav").exists())
        check("ulaw-only: resolves custom", sbc.prompt("sw-no-emergency") == str(b.dest / "sw-no-emergency"))


def test_both_install_and_the_owners_ulaw_is_kept():
    with _box() as b:
        own = _ulaw(1500, 0x33)
        (b.src / "sw-busy.wav").write_bytes(_wav())
        (b.src / "sw-busy.ulaw").write_bytes(own)
        sbc.install_custom_prompts()
        check("both: the owner's .ulaw is installed, not one derived from the .wav",
              (b.dest / "sw-busy.ulaw").read_bytes() == own)
        check("both: the .wav is installed too", sbc.wav_pcm((b.dest / "sw-busy.wav").read_bytes())[0] == _pcm())
        check("both: one name counted once, and the summary says the .ulaw plays",
              _summary(b.logs) == f"prompts: 1 custom from {FOLDER}/prompts, {len(NAMES) - 1} "
                                  "built-in; 1 given as both .wav and .ulaw, the .ulaw plays")
        # A re-recording: a new .wav beside an earlier .ulaw. The .ulaw still
        # plays (it is what the phones take), and the summary says so.
        (b.src / "sw-greeting.wav").write_bytes(_wav())
        (b.src / "sw-greeting.ulaw").write_bytes(_ulaw(3000, 0x11))
        b.logs.clear()
        sbc.install_custom_prompts()
        check("both: two names given as both are counted",
              _summary(b.logs).endswith("built-in; 2 given as both .wav and .ulaw, the .ulaw plays"))
        check("both: the earlier .ulaw is what plays",
              (b.dest / "sw-greeting.ulaw").read_bytes() == _ulaw(3000, 0x11))


def test_every_bad_file_is_skipped_with_one_line_and_its_name_stays_built_in():
    with _box() as b:
        s = b.src
        elsewhere = b.root / "elsewhere.ulaw"
        elsewhere.write_bytes(_ulaw())
        bad = {
            "sw-nonexistent.wav": ("not the name of a built-in prompt", None),
            "readme.txt": ("not a .wav or .ulaw file", None),
            "sw-greeting.mp3": ("not a .wav or .ulaw file", None),
            "sw-didnt-catch.WAV": ("lower-case", None),
            "sw-goodbye.ulaw": ("a link, not a file", "sw-goodbye"),
            "sw-noanswer.ulaw": ("not a regular file", "sw-noanswer"),
            "sw-unavailable.wav": ("not a regular file", "sw-unavailable"),
            "sw-test.ulaw": ("larger than 1 MiB", "sw-test"),
            "sw-connecting.wav": ("22050 Hz (must be 8000 Hz)", "sw-connecting"),
            "sw-wakeup-ask.wav": ("2 channels (must be mono)", "sw-wakeup-ask"),
            "sw-wakeup-set.wav": ("8-bit (must be 16-bit)", "sw-wakeup-set"),
            "sw-seconds.ulaw": ("too short", "sw-seconds"),
            "sw-hours-and.wav": ("too short", "sw-hours-and"),
            "sw-tone.wav": ("truncated", "sw-tone"),
            "sw-beep.wav": ("truncated", "sw-beep"),
            "sw-endtone.wav": ("not 16-bit PCM (WAV format 7)", "sw-endtone"),
            "sw-wakeup-none.wav": ("WAV format 65534", "sw-wakeup-none"),
            "sw-wakeup-change.ulaw": ("a WAV file named .ulaw", "sw-wakeup-change"),
            "sw-wakeup-exists.wav": ("not a WAV file", "sw-wakeup-exists"),
            "sw-dial-one-first.ulaw": ("too short", "sw-dial-one-first"),
            "sw-wakeup-greeting.wav": ("format header is too short", "sw-wakeup-greeting"),
            "sw-wakeup-cancelled.wav": ("no format header before the audio", "sw-wakeup-cancelled"),
        }
        (s / "sw-nonexistent.wav").write_bytes(_wav())
        (s / "readme.txt").write_text("my recordings\n")
        (s / "sw-greeting.mp3").write_bytes(b"ID3" + b"\0" * 2000)
        (s / "sw-greeting.ulaw").write_bytes(_ulaw())          # a good file beside it
        (s / "sw-didnt-catch.WAV").write_bytes(_wav())
        (s / "sw-goodbye.ulaw").symlink_to(elsewhere)
        os.mkfifo(s / "sw-noanswer.ulaw")
        (s / "sw-unavailable.wav").mkdir()
        (s / "sw-test.ulaw").write_bytes(_ulaw(sbc.PROMPT_MAX_BYTES + 1))
        (s / "sw-connecting.wav").write_bytes(_wav(rate=22050))
        (s / "sw-wakeup-ask.wav").write_bytes(_wav(channels=2))
        (s / "sw-wakeup-set.wav").write_bytes(_wav(_ulaw(2000), bits=8))
        (s / "sw-seconds.ulaw").write_bytes(b"")
        (s / "sw-hours-and.wav").write_bytes(_wav(_pcm(SAMPLES[:100])))
        (s / "sw-tone.wav").write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00")
        (s / "sw-beep.wav").write_bytes(_wav(data_size=len(_pcm()) + 400))
        (s / "sw-endtone.wav").write_bytes(_wav(_ulaw(2000), bits=8, tag=7))
        (s / "sw-wakeup-none.wav").write_bytes(_wav(tag=0xFFFE))
        (s / "sw-wakeup-change.ulaw").write_bytes(_wav())
        (s / "sw-wakeup-exists.wav").write_bytes(_ulaw())
        (s / "sw-dial-one-first.wav").write_bytes(_wav())      # good, but its .ulaw is not
        (s / "sw-dial-one-first.ulaw").write_bytes(_ulaw(100))
        # A 14-byte WAVEFORMAT header (no bits-per-sample), complete as written.
        short_fmt = struct.pack("<HHIIH", 1, 1, 8000, 16000, 2)
        body = (b"WAVEfmt " + struct.pack("<I", 14) + short_fmt
                + b"data" + struct.pack("<I", len(_pcm())) + _pcm())
        (s / "sw-wakeup-greeting.wav").write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        # The audio before its format header.
        good = _wav()
        fmt_chunk, data_chunk = good[12:36], good[36:]
        (s / "sw-wakeup-cancelled.wav").write_bytes(good[:12] + data_chunk + fmt_chunk)
        sbc.install_custom_prompts()
        for fname, (why, name) in bad.items():
            lines = [line for line in b.logs if line.startswith(f"prompts: skipped {fname}: ")]
            check(f"skip: {fname} is named in exactly one line", len(lines) == 1)
            check(f"skip: {fname} says why ({why})", why in lines[0])
            if name:
                check(f"skip: {fname} -> {name} keeps its built-in prompt",
                      lines[0].endswith(f"; {name} keeps its built-in prompt")
                      and sbc.prompt(name) == f"switchboard/{name}"
                      and not any(b.dest.glob(f"{name}.*")))
        check("skip: a bad file in another format does not block a good sibling",
              sbc.prompt("sw-greeting") == str(b.dest / "sw-greeting"))
        check("skip: a pair with one bad file stays built-in, neither file installed",
              not any(b.dest.glob("sw-dial-one-first.*")))
        check("skip: the link's target was not copied anywhere",
              sorted(p.name for p in b.dest.iterdir()) == ["sw-greeting.ulaw"])
        check("skip: the summary counts the custom name and every skipped file",
              _summary(b.logs) == f"prompts: 1 custom from {FOLDER}/prompts, "
                                  f"{len(NAMES) - 1} built-in; {len(bad)} file(s) skipped")
        check("skip: one line per skipped file plus the summary, nothing else",
              len(b.logs) == len(bad) + 1)


def test_a_skip_line_never_echoes_what_the_file_holds():
    """The reason names a cut-short chunk by where it is, unless it is one of
    the two the reader looks for: a chunk id is four bytes of the file."""
    with _box() as b:
        good = _wav()
        planted = (b"RIFF" + struct.pack("<I", 9999) + b"WAVE" + b"pw=h" + struct.pack("<I", 999999)
                   + b"unter2-secret-token")
        (b.src / "sw-greeting.wav").write_bytes(planted)
        (b.src / "sw-busy.wav").write_bytes(good[:36] + b"data" + struct.pack("<I", 99999) + _pcm())
        (b.src / "sw-tone.wav").write_bytes(good[:12] + b"fmt " + struct.pack("<I", 16) + b"\x01\x00")
        sbc.install_custom_prompts()
        text = "\n".join(b.logs)
        for secret in ("pw=h", "unter2", "secret", "token"):
            check(f"no echo: {secret!r} from the file is not in the log", secret not in text)
        check("no echo: an unknown chunk is named by its place in the file",
              "prompts: skipped sw-greeting.wav: truncated (the chunk at byte 12 is cut short); "
              "sw-greeting keeps its built-in prompt" in b.logs)
        check("no echo: the audio and format chunks are still named",
              "prompts: skipped sw-busy.wav: truncated (its 'data' chunk is cut short); "
              "sw-busy keeps its built-in prompt" in b.logs
              and "prompts: skipped sw-tone.wav: truncated (its 'fmt ' chunk is cut short); "
              "sw-tone keeps its built-in prompt" in b.logs)


def test_a_silent_or_renamed_recording_is_refused():
    """Well-formed but silent audio, and other containers named .ulaw, would
    play as silence or noise: refused like any other bad file."""
    with _box() as b:
        s = b.src
        bad = {
            # Digital silence, as a recorder that captured nothing writes it.
            "sw-no-emergency.wav": ("silent", _wav(_pcm([0] * 16000))),
            "sw-goodbye.ulaw": ("silent", _ulaw(8000, 0xFF)),
            # µ-law's two zeros and the smallest steps either side.
            "sw-connecting.ulaw": ("silent", bytes([0xFF, 0x7F, 0xFE, 0x7E]) * 2000),
            # A hiss far under any speech: about -50 dBFS at its peak.
            "sw-greeting.wav": ("silent", _wav(_pcm([0, 8, -8, 96, -96] * 3200))),
            # A file of zero bytes, sparse or not: µ-law 0x00 is one constant
            # level, which plays as nothing.
            "sw-busy.ulaw": ("silent", None),
            "sw-noanswer.ulaw": ("an MP3 file", b"ID3\x04\x00" + _ulaw(4000)),
            "sw-unavailable.ulaw": ("a FLAC file", b"fLaC" + _ulaw(4000)),
            "sw-test.ulaw": ("an Ogg file", b"OggS" + _ulaw(4000)),
            "sw-wakeup-ask.ulaw": ("an AIFF file", b"FORM" + _ulaw(4000)),
            "sw-wakeup-set.ulaw": ("a Sun/NeXT audio file", b".snd" + _ulaw(4000)),
            "sw-wakeup-none.ulaw": ("a CAF file", b"caff" + _ulaw(4000)),
            "sw-wakeup-change.ulaw": ("an MP4/M4A file", b"\0\0\0\x20ftypM4A " + _ulaw(4000)),
        }
        for fname, (_why, body) in bad.items():
            if body is not None:
                (s / fname).write_bytes(body)
        with open(s / "sw-busy.ulaw", "wb") as fh:
            fh.truncate(16000)
        good = {
            # Quiet but audible (about -38 dBFS), and leading silence.
            "sw-wakeup-exists.wav": _wav(_pcm([0] * 4000 + [400, -400] * 2000)),
            "sw-seconds.ulaw": _ulaw(4000, 0xFF) + _ulaw(4000),
            "sw-hours-and.ulaw": _ulaw(1600),
        }
        for fname, body in good.items():
            (s / fname).write_bytes(body)
        sbc.install_custom_prompts()
        for fname, (why, _body) in bad.items():
            name = fname.rsplit(".", 1)[0]
            lines = [line for line in b.logs if line.startswith(f"prompts: skipped {fname}: ")]
            check(f"refuse: {fname} is named in exactly one line", len(lines) == 1)
            check(f"refuse: {fname} says why ({why})", why in lines[0])
            check(f"refuse: {fname} -> {name} keeps its built-in prompt",
                  lines[0].endswith(f"; {name} keeps its built-in prompt")
                  and sbc.prompt(name) == f"switchboard/{name}"
                  and not any(b.dest.glob(f"{name}.*")))
        for fname in good:
            name = fname.rsplit(".", 1)[0]
            check(f"refuse: {fname} is audible and installed", sbc.prompt(name) == str(b.dest / name))
        rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"}])
        ext = sbc.render_extensions({"rooms": rooms, "trunk": {}})
        check("refuse: a silent 911 notice leaves the built-in one playing",
              ext.count("Playback(switchboard/sw-no-emergency)") == 2)


def test_every_built_in_recording_passes_the_checks_a_custom_one_must():
    """The checks refuse silence and the wrong format, never real audio: each
    shipped recording, the three tones included, would be accepted."""
    for p in sorted(BUNDLED.iterdir()):
        data = p.read_bytes()
        if p.suffix == ".wav":
            pcm, why = sbc.wav_pcm(data)
            why = why or sbc.silence_check(pcm, "wav")
        elif p.suffix == ".ulaw":
            why = sbc.ulaw_check(data)
        else:
            continue
        check(f"built-in {p.name} passes ({why or 'ok'})", why == "")


def test_the_911_notice_cannot_be_made_silent_by_a_bad_file():
    with _box() as b:
        # Each given alone, under the one extension it could otherwise pass as.
        for ext_, content in (("ulaw", b""), ("ulaw", _ulaw(10)), ("wav", _wav(rate=16000)),
                              ("wav", b"RIFF" + b"\0" * 900), ("ulaw", b"RIFF" + b"\0" * 900),
                              ("wav", _wav(_pcm([0] * 16000))), ("ulaw", _ulaw(16000, 0xFF)),
                              ("ulaw", b"\0" * 16000), ("ulaw", b"ID3\x04\x00" + _ulaw(4000))):
            for f in b.src.iterdir():
                f.unlink()
            (b.src / f"sw-no-emergency.{ext_}").write_bytes(content)
            sbc.install_custom_prompts()
            rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"}])
            ext = sbc.render_extensions({"rooms": rooms, "trunk": {}})
            check(f"911: a bad .{ext_} ({len(content)} bytes, {content[:4]!r}) leaves the built-in notice",
                  ext.count("Playback(switchboard/sw-no-emergency)") == 2
                  and not b.dest.exists())


def test_an_absent_or_linked_folder_leaves_every_prompt_built_in():
    with _box() as b:
        shutil.rmtree(b.src)
        sbc.install_custom_prompts()
        check("absent: the summary names the folder and says it is not there",
              _summary(b.logs) == f"prompts: 0 custom from {FOLDER}/prompts (no such folder), "
                                  f"{len(NAMES)} built-in")
        check("absent: nothing installed", not b.dest.exists())
        real = b.root / "real"
        real.mkdir()
        (real / "sw-greeting.ulaw").write_bytes(_ulaw())
        b.src.symlink_to(real)
        b.logs.clear()
        sbc.install_custom_prompts()
        check("linked folder: refused with one line",
              any(line.startswith(f"prompts: skipped {FOLDER}/prompts: a link or not a folder")
                  for line in b.logs))
        check("linked folder: nothing read through it", not b.dest.exists()
              and sbc.prompt("sw-greeting") == "switchboard/sw-greeting")
        check("linked folder: the summary counts no file for it",
              _summary(b.logs) == f"prompts: 0 custom from {FOLDER}/prompts, {len(NAMES)} built-in")


def test_a_restart_drops_what_the_folder_no_longer_holds():
    with _box() as b:
        (b.src / "sw-greeting.ulaw").write_bytes(_ulaw())
        (b.src / "sw-busy.ulaw").write_bytes(_ulaw())
        sbc.install_custom_prompts()
        check("rerun: both installed", sorted(p.name for p in b.dest.iterdir())
              == ["sw-busy.ulaw", "sw-greeting.ulaw"])
        (b.src / "sw-greeting.ulaw").unlink()
        sbc.install_custom_prompts()
        check("rerun: a removed file returns its name to the built-in prompt",
              sorted(p.name for p in b.dest.iterdir()) == ["sw-busy.ulaw"]
              and sbc.prompt("sw-greeting") == "switchboard/sw-greeting")
        (b.src / "sw-busy.ulaw").unlink()
        sbc.install_custom_prompts()
        check("rerun: an empty folder installs nothing", not b.dest.exists())


def test_an_unexpected_failure_leaves_every_prompt_built_in():
    def boom(*_a, **_k):
        raise RuntimeError("synthetic")
    for target in ("bundled_prompt_names", "lin2ulaw", "_wav_file", "_read_prompt_file"):
        with _box() as b:
            (b.src / "sw-no-emergency.wav").write_bytes(_wav())
            (b.src / "sw-busy.ulaw").write_bytes(_ulaw())
            # An earlier start in this container installed something.
            b.dest.mkdir(parents=True)
            (b.dest / "sw-goodbye.ulaw").write_bytes(_ulaw())
            saved = getattr(sbc, target)
            setattr(sbc, target, boom)
            try:
                sbc.install_custom_prompts()            # must not raise
            finally:
                setattr(sbc, target, saved)
            check(f"failure in {target}: one ERROR line",
                  sum(line.startswith("ERROR: custom prompts unusable") for line in b.logs) == 1)
            check(f"failure in {target}: nothing installed, the earlier install removed too",
                  not b.dest.exists())
            check(f"failure in {target}: no build directory left behind",
                  list(b.dest.parent.iterdir()) == [])
            rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"}])
            ext = sbc.render_extensions({"rooms": rooms, "operator": {"enabled": True}, "trunk": {}})
            check(f"failure in {target}: the dialplan renders, every prompt built-in",
                  "Playback(switchboard/sw-no-emergency)" in ext
                  and "Playback(switchboard/sw-busy)" in ext
                  and "Playback(switchboard/sw-goodbye)" in ext
                  and not re.search(r"Playback\(/", ext))


def test_a_failed_cleanup_never_raises_and_never_claims_all_built_in():
    """A second failure, removing what is installed, must not stop the start
    either, and the log must not say every prompt is built-in when files an
    earlier start installed are still there to play."""
    def boom(*_a, **_k):
        raise RuntimeError("synthetic")
    for kind in (RuntimeError, OSError):
        with _box() as b:
            (b.src / "sw-busy.wav").write_bytes(_wav())
            b.dest.mkdir(parents=True)
            (b.dest / "sw-goodbye.ulaw").write_bytes(_ulaw())        # an earlier start's
            saved = (sbc.lin2ulaw, sbc._remove_prompt_tree)
            real_remove = sbc._remove_prompt_tree

            def remove(path, _kind=kind, _dest=b.dest):
                if path == _dest:
                    raise _kind("synthetic cleanup failure")
                real_remove(path)
            sbc.lin2ulaw, sbc._remove_prompt_tree = boom, remove
            try:
                sbc.install_custom_prompts()                        # must not raise
            finally:
                sbc.lin2ulaw, sbc._remove_prompt_tree = saved
            errors = [line for line in b.logs if line.startswith("ERROR:")]
            check(f"cleanup {kind.__name__}: one ERROR line", len(errors) == 1)
            check(f"cleanup {kind.__name__}: it does not claim every prompt is built-in",
                  "every prompt is the built-in one" not in errors[0])
            check(f"cleanup {kind.__name__}: it says what may still play",
                  f"could not remove {b.dest}" in errors[0] and "may still play" in errors[0])
            check(f"cleanup {kind.__name__}: which is true", sbc.prompt("sw-goodbye") == str(b.dest / "sw-goodbye"))


def test_a_resolver_that_cannot_load_leaves_config_generation_working():
    saved = sbc.PROMPT_REF
    sbc.PROMPT_REF = None
    try:
        sbc.install_custom_prompts()
        rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"}])
        ext = sbc.render_extensions({"rooms": rooms, "trunk": {}})
        check("no resolver: the dialplan plays the built-in prompts",
              ext.count("Playback(switchboard/sw-no-emergency)") == 2)
    finally:
        sbc.PROMPT_REF = saved


def _drive_main(b, explode=False):
    """Run switchboard-config's main() with every path it writes inside `b`."""
    names = ("OPTIONS_PATH", "OVERLAY_PATH", "EFFECTIVE_PATH", "ASTERISK_ETC", "RUN_DIR",
             "DATA_DIR", "STATE_DIR", "SHARE_DIR", "SHARE_LOG", "apply_timezone",
             "resolve_timezone", "_install_custom_prompts")
    saved = {n: getattr(sbc, n) for n in names}
    opts = b.root / "options.json"
    opts.write_text('{"rooms": [{"ext": "11", "name": "Kitchen", "secret": "s3cret11"},'
                    ' {"ext": "12", "name": "Den", "secret": "s3cret12"}]}')
    sbc.OPTIONS_PATH = opts
    sbc.OVERLAY_PATH = b.root / "config" / "options-overlay.json"
    sbc.RUN_DIR = b.dest.parent
    sbc.EFFECTIVE_PATH = sbc.RUN_DIR / "options-effective.json"
    sbc.ASTERISK_ETC = b.root / "etc" / "asterisk"
    sbc.DATA_DIR = b.root / "data"
    sbc.STATE_DIR = sbc.DATA_DIR / "state"
    sbc.SHARE_DIR = b.root / "share" / "switchboard"
    sbc.SHARE_LOG = sbc.SHARE_DIR / "asterisk.log"
    sbc.apply_timezone = lambda tz: "UTC"
    sbc.resolve_timezone = lambda o: "UTC"
    if explode:
        def boom(_dest):
            raise OSError(28, "No space left on device")
        sbc._install_custom_prompts = boom
    try:
        sbc.main()
        return (sbc.ASTERISK_ETC / "extensions.conf").read_text()
    finally:
        for n, v in saved.items():
            setattr(sbc, n, v)


def test_main_installs_before_it_renders_and_completes_when_the_step_fails():
    with _box() as b:
        (b.src / "sw-no-emergency.ulaw").write_bytes(_ulaw())
        ext = _drive_main(b)
        check("main: the dialplan plays the custom 911 notice from the install directory",
              ext.count(f"Playback({b.dest}/sw-no-emergency)") == 2)
        check("main: every other prompt plays the built-in one",
              "Playback(switchboard/sw-goodbye)" in ext and "Playback(switchboard/sw-busy)" in ext)
        check("main: the summary line is in the boot log", "prompts: 1 custom" in _summary(b.logs))
    with _box() as b:
        (b.src / "sw-no-emergency.ulaw").write_bytes(_ulaw())
        ext = _drive_main(b, explode=True)
        check("main: a failed prompt step still writes the whole configuration",
              "[operator]" in ext and ext.count("Playback(switchboard/sw-no-emergency)") == 2)
        check("main: and says so once", sum("ERROR: custom prompts unusable" in line
                                            for line in b.logs) == 1)
        check("main: ...and finishes", any(line.startswith("done: 2 room(s)") for line in b.logs))
    src = SBC_PATH.read_text()
    body = src[src.index("def main() -> None:"):]
    check("main: the prompt step runs before extensions.conf is written",
          0 < body.index("install_custom_prompts()") < body.index('write(ASTERISK_ETC / "extensions.conf"'))


# --------------------------------------------------------------------------- #
# The places that play a prompt.
# --------------------------------------------------------------------------- #
def _install_dir(names, seconds=None):
    d = Path(tempfile.mkdtemp(prefix="sw-installed-"))
    for n in names:
        (d / f"{n}.ulaw").write_bytes(_ulaw(int(8000 * (seconds or {}).get(n, 1.0))))
    return d


def test_the_dialplan_embeds_the_resolved_reference():
    custom = {"sw-no-emergency", "sw-busy", "sw-tone", "sw-at-sound-tone", "sw-wakeup-greeting"}
    d = _install_dir(custom)
    saved = sbc.PROMPT_REF.PROMPTS_DIR
    sbc.PROMPT_REF.PROMPTS_DIR = str(d)
    try:
        rooms = sbc.valid_rooms([{"ext": "11", "name": "K", "secret": "s1"},
                                 {"ext": "12", "name": "L", "secret": "s2"}])
        ext = sbc.render_extensions({"rooms": rooms, "operator": {"enabled": True},
                                     "trunk": {"enabled": True, "provider_host": "sip.example.com",
                                               "username": "u", "secret": "x"}})
        played = re.findall(r"Playback\(([^)$]+)\)", ext)
        for ref in played:
            name = ref.rsplit("/", 1)[-1]
            want = f"{d}/{name}" if name in custom else f"switchboard/{name}"
            check(f"dialplan: {name} plays {want}", ref == want)
        check("dialplan: all four 911/933 notices (bare and prefixed) are custom",
              played.count(f"{d}/sw-no-emergency") == 4)
        check("dialplan: the clock's fallback tone stays well-formed",
              f' same = n,ExecIf($["${{SW_CLOCK_TONE}}" != "1"]?Playback({d}/sw-tone))' in ext)
        check("dialplan: every shipped prompt the dialplan plays was seen",
              {r.rsplit("/", 1)[-1] for r in played} >= custom | {"sw-goodbye", "sw-endtone"})
    finally:
        sbc.PROMPT_REF.PROMPTS_DIR = saved
        shutil.rmtree(d, ignore_errors=True)


def _clock(resolver):
    """The clock AGI and its sequencer, loaded the way the image loads them."""
    with _injected(resolver):
        cs = SourceFileLoader("clock_speak_custom", str(CLOCK_SPEAK)).load_module()
        with _injected(resolver, clock_speak=cs):
            mod = SourceFileLoader("sw_clock_agi_custom",
                                   str(AGI / "switchboard-clock.agi")).load_module()
    return mod, cs


def test_the_clock_measures_the_custom_file_it_plays():
    d = _install_dir({"sw-hours-and", "sw-seconds", "sw-tone"},
                     {"sw-hours-and": 3.0, "sw-seconds": 2.25, "sw-tone": 0.5})
    try:
        mod, cs = _clock(_resolver(d))
        check("clock: an absolute reference is measured at that path",
              mod.duration_of(str(d / "sw-hours-and")) == 3.0)
        check("clock: the readout words resolve to the custom recordings",
              cs.HOURS_AND == str(d / "sw-hours-and") and cs.SECONDS == str(d / "sw-seconds"))
        check("clock: the tone resolves too", mod.TONE == str(d / "sw-tone"))
        check("clock: and each is measured as the custom length",
              (mod.duration_of(cs.HOURS_AND), mod.duration_of(cs.SECONDS), mod.duration_of(mod.TONE))
              == (3.0, 2.25, 0.5))
        check("clock: an unknown prompt is still assumed long",
              mod.duration_of(str(d / "sw-missing")) == mod.UNKNOWN_PROMPT_S)
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _PlayingAGI:
    """Plays AGI stdin/stdout on a fake clock. A STREAM FILE takes as long as the
    file Asterisk would play for that reference really is, whatever the AGI
    measured; a digit takes 0.8 s."""

    def __init__(self, start):
        self.now, self.log, self.pending = start, [], ""
        self.env = ["agi_request: switchboard-clock.agi", ""]

    @staticmethod
    def _len(ref):
        path = ref if ref.startswith("/") else str(ROOTFS / "var/lib/asterisk/sounds/en" / ref)
        return os.path.getsize(path + ".ulaw") / 8000 if os.path.exists(path + ".ulaw") else 0.8

    def readline(self):
        if self.env:
            return self.env.pop(0) + "\n"
        cmd = self.pending
        if cmd.startswith("STREAM FILE "):
            self.now += self._len(cmd.split()[2])
        elif cmd.startswith("SAY NUMBER "):
            self.now += 0.8 * (1 if int(cmd.split()[2]) <= 20 or int(cmd.split()[2]) % 10 == 0 else 2)
        return "200 result=0\n"

    def write(self, s):
        self.pending = s.strip()
        self.log.append((round(self.now, 3), self.pending))

    def flush(self):
        pass


def test_the_clock_tone_lands_on_the_second_it_named_with_long_custom_words():
    import time as _time
    d = _install_dir({"sw-hours-and", "sw-seconds", "sw-tone"},
                     {"sw-hours-and": 3.0, "sw-seconds": 2.25, "sw-tone": 0.5})
    try:
        mod, cs = _clock(_resolver(d))
        # The AGI's own measurement for every switchboard prompt; the core
        # digits, which this checkout does not ship, as long as the fake plays them.
        measured = mod.duration_of
        digits = lambda stem: (0.8 if stem.startswith("digits/") else measured(stem))  # noqa: E731
        start = 86400.0 * 20000 + 58547.3            # 16:15:47.3 UTC
        fake = _PlayingAGI(start)
        mod.duration_of = digits
        mod.time = types.SimpleNamespace(
            time=lambda: fake.now, sleep=lambda s: setattr(fake, "now", fake.now + s),
            localtime=_time.gmtime, strftime=_time.strftime)
        saved_defaults = cs.plan.__defaults__
        cs.plan.__defaults__ = (_time.gmtime, cs.TONE_MARGIN_S)
        saved_io = sys.stdin, sys.stdout
        try:
            sys.stdin, sys.stdout = fake, fake
            mod.main()
        finally:
            sys.stdin, sys.stdout = saved_io
            cs.plan.__defaults__ = saved_defaults
        target, _acts = cs.plan(start, digits, localtime=_time.gmtime)
        cmds = [c for _t, c in fake.log]
        check("clock: the readout streams the custom words",
              f'STREAM FILE {d}/sw-hours-and ""' in cmds and f'STREAM FILE {d}/sw-seconds ""' in cmds)
        tone = [t for t, c in fake.log if c == f'STREAM FILE {d}/sw-tone ""']
        check("clock: the custom tone is played", len(tone) == 1)
        check(f"clock: the tone starts on the announced second ({tone[0]} vs {target})",
              abs(tone[0] - target) < 0.01)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_the_web_ui_test_ring_resolves_at_use_time():
    d = Path(tempfile.mkdtemp())
    try:
        with _injected(_resolver(d)):
            ami = SourceFileLoader("sw_ami_custom_prompts", str(AMI)).load_module()
        sent = []

        def fake(lines, single_response=False, action_id=None, **_k):
            sent.append(lines)
            return [{"actionid": action_id, "response": "Success"}]
        ami._ami_command = fake
        check("ring: queued", ami.ring_extension("11") is True)
        check("ring: nothing installed -> the built-in test prompt",
              "Data: switchboard/sw-test" in sent[-1])
        (d / "sw-test.ulaw").write_bytes(_ulaw())
        ami.ring_extension("11")
        check("ring: resolved when it rings, not when the web UI started",
              f"Data: {d}/sw-test" in sent[-1])
        ami.ring_extension("11", sound="switchboard/sw-beep")
        check("ring: an explicit sound is still played as given",
              "Data: switchboard/sw-beep" in sent[-1])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_every_agi_and_helper_resolves_through_the_one_resolver():
    d = _install_dir(NAMES)
    try:
        pr = _resolver(d)
        with _injected(pr):
            speech = SourceFileLoader("agi_speech_custom", str(SPEECH)).load_module()
            auto = SourceFileLoader("sb_automation_custom", str(AGI / "switchboard-automation.agi")).load_module()
            asst = SourceFileLoader("sb_assistant_custom", str(AGI / "switchboard-assistant.agi")).load_module()
            op = SourceFileLoader("sb_operator_custom", str(AGI / "switchboard-operator.agi")).load_module()
            store = SourceFileLoader("store_custom_prompts", str(STORE)).load_module()
            with _injected(pr, store=store):
                wake = SourceFileLoader("sb_wakeup_custom", str(AGI / "switchboard-wakeup.agi")).load_module()
        for label, mod in (("agi_speech", speech), ("automation", auto), ("assistant", asst)):
            check(f"{label}: the canned prompts are the custom recordings",
                  (mod.PROMPT_BEEP, mod.PROMPT_ONEMOMENT, mod.PROMPT_GOODBYE)
                  == tuple(str(d / n) for n in ("sw-beep", "sw-onemoment", "sw-goodbye")))
        for label, mod in (("operator", op), ("wakeup", wake), ("agi_speech", speech),
                           ("automation", auto), ("assistant", asst)):
            check(f"{label}: prompt() is the shared resolver", mod.prompt is pr.prompt)
        played = []
        op.read_env = lambda: {}
        op.agi = lambda cmd: played.append(cmd) or "200 result=0"
        op.recognize = lambda attempt: ("", "")
        op.main()
        streams = [c.split()[2] for c in played if c.startswith("STREAM FILE ")]
        check("operator: greeting, retry and no-such-room all play the custom recordings",
              streams == [str(d / n) for n in ("sw-greeting", "sw-didnt-catch", "sw-no-such-room")])
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# ★ No reference may bypass the resolver.
# --------------------------------------------------------------------------- #
CANONICAL = (
    'sys.path.insert(0, "/usr/share/switchboard/prompts")\n'
    "try:\n"
    "    from prompt_ref import prompt  # noqa: E402  (one answer for every prompt played)\n"
    "except Exception:  # noqa: BLE001  (the built-in prompts, if the resolver cannot load)\n"
    "    def prompt(name: str) -> str:\n"
    '        return "switchboard/" + name\n'
)
# Shipped and documented as not played by anything (DOCS.md's prompt table).
UNUSED = {"sw-at-the-tone", "sw-time-is"}


def _sources():
    """Every text file the image ships, except the prompts themselves."""
    out = {}
    for p in sorted(ROOTFS.rglob("*")):
        if not p.is_file() or BUNDLED in p.parents or "__pycache__" in p.parts:
            continue
        try:
            out[p.relative_to(ROOTFS).as_posix()] = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    return out


def bypasses(files: dict, names) -> list:
    """Every way a prompt is referenced other than through prompt()."""
    found = []
    for rel, text in files.items():
        for m in re.finditer(r"switchboard/sw-", text):
            found.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: a literal built-in reference")
        for m in re.finditer(r"(?P<q>[\"'])(?P<n>sw-[a-z0-9-]+)(?P=q)", text):
            if m.group("n") in names and not text[:m.start()].endswith("prompt("):
                found.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: "
                             f"{m.group('n')} named outside prompt()")
        # A call that names a prompt. (app.py's page script calls the browser's
        # own prompt() with a question; that is not this one.)
        calls = [m.start() for m in re.finditer(r"(?<![\w.])prompt\([\"']sw-", text)]
        if calls and rel not in ("usr/bin/switchboard-config",
                                 "usr/share/switchboard/prompts/prompt_ref.py"):
            at = text.find(CANONICAL)
            if at == -1:
                found.append(f"{rel}: calls prompt() without the shared resolver import")
            elif at > calls[0]:
                found.append(f"{rel}: calls prompt() before importing it")
            if text.count('"switchboard/" + name') != 1:
                found.append(f"{rel}: builds a built-in reference outside the one fallback")
    return found


def test_every_prompt_reference_in_rootfs_goes_through_the_resolver():
    files = _sources()
    check("scan: the walk sees the dialplan generator, the AGIs and the helpers",
          {"usr/bin/switchboard-config", "usr/share/switchboard/webui/ami.py",
           "var/lib/asterisk/agi-bin/switchboard-clock.agi"} <= set(files))
    found = bypasses(files, set(NAMES))
    check("scan: every prompt reference goes through prompt():\n  " + "\n  ".join(found), not found)
    named = set()
    for text in files.values():
        named |= set(re.findall(r"(?<![\w.])prompt\([\"'](sw-[a-z0-9-]+)[\"']\)", text))
    check(f"scan: every name passed to prompt() is a shipped prompt: {sorted(named - set(NAMES))}",
          named <= set(NAMES))
    check(f"scan: every shipped prompt is played except the two unused ones: "
          f"{sorted(set(NAMES) - named - UNUSED)}", named == set(NAMES) - UNUSED)
    users = [rel for rel, text in files.items() if CANONICAL in text]
    check(f"scan: the eight AGIs and helpers carry the one import ({len(users)})", len(users) == 8)
    check("scan: the resolver lives where they look for it",
          PROMPT_REF_PATH == ROOTFS / "usr/share/switchboard/prompts/prompt_ref.py"
          and not (ROOTFS / "usr/share/switchboard/prompts/__init__.py").exists())
    src = SBC_PATH.read_text()
    check("scan: switchboard-config loads that same file",
          'PROMPT_REF_PY = Path(__file__).resolve().parents[1] / "share/switchboard/prompts/prompt_ref.py"'
          in src and sbc.PROMPT_REF_PY == PROMPT_REF_PATH)


def test_the_scan_can_see_a_bypass():
    """A guard nobody has watched fail is not a guard."""
    names = {"sw-beep", "sw-goodbye"}
    planted = {
        "a.agi": 'stream("switchboard/sw-beep")\n',
        "b.py": 'X = "sw-goodbye"\n',
        "c.py": "x = prompt('sw-beep')\n",
        "d.py": "import sys\n" + CANONICAL + 'y = prompt("sw-beep")\n',
        "e.py": "import sys\n" + CANONICAL + 'y = prompt("sw-beep")\nz = "switchboard/" + name\n',
        "f.py": 'y = prompt("sw-beep")\n' + CANONICAL,
        "g.py": "import sys\n" + CANONICAL + "def f():\n    return prompt('sw-goodbye')\n",
    }
    found = bypasses(planted, names)
    for key, why in (("a.agi", "a literal built-in reference"), ("b.py", "named outside prompt()"),
                     ("c.py", "without the shared resolver import"),
                     ("e.py", "outside the one fallback"), ("f.py", "before importing it")):
        check(f"scan self-test: {key} is caught ({why})", any(f.startswith(key) and why in f for f in found))
    check("scan self-test: the correct shapes pass", not any(f.startswith(("d.py", "g.py")) for f in found))


# --------------------------------------------------------------------------- #
# G.711, the folder name, the profile and the docs.
# --------------------------------------------------------------------------- #
def test_the_derived_ulaw_is_g711_with_or_without_audioop():
    pcm = _pcm()
    want = bytes(G711[s] for s in SAMPLES)
    check("g711: the pure encoder", sbc.lin2ulaw(pcm, use_audioop=False) == want)
    check("g711: the encoder the image uses", sbc.lin2ulaw(pcm) == want)
    check("g711: an odd trailing byte is dropped, not coded",
          sbc.lin2ulaw(pcm + b"\x01", use_audioop=False) == want)
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop
    except ImportError:
        print("SKIP g711: no audioop on this interpreter; the vectors above are its output")
        return
    every = struct.pack("<65536h", *range(-32768, 32768))
    check("g711: the pure encoder equals audioop on every 16-bit sample",
          sbc.lin2ulaw(every, use_audioop=False) == audioop.lin2ulaw(every, 2))
    check("g711: the silence check's decoder equals audioop on every code",
          audioop.ulaw2lin(bytes(range(256)), 2)
          == struct.pack("<256h", *(sbc._ulaw_linear(c) for c in range(256))))


def test_the_silence_check_decodes_ulaw_as_g711():
    """audioop.ulaw2lin's output for these codes (CPython 3.9)."""
    want = {0xFF: 0, 0x7F: 0, 0xFE: 8, 0x7E: -8, 0xF3: 96, 0xF2: 104, 0xF0: 120,
            0x80: 32124, 0x00: -32124, 0x9F: 8316, 0x1F: -8316, 0xCE: 988, 0x4E: -988}
    got = {c: sbc._ulaw_linear(c) for c in want}
    check(f"g711 decode: {got}", got == want)
    check("silence: the swing is peak to peak, so a constant level is silent",
          sbc.silence_check(bytes([0x00]) * 2000, "ulaw") != ""
          and sbc.silence_check(bytes([0x00, 0x80]) * 1000, "ulaw") == "")
    check("silence: the floor is a 200 swing on 16-bit samples, either format",
          sbc.silence_check(_pcm([-100, 99] * 500), "wav") != ""
          and sbc.silence_check(_pcm([-100, 100] * 500), "wav") == ""
          and sbc.silence_check(bytes([0xF3, 0x73]) * 500, "ulaw") != ""     # +-96
          and sbc.silence_check(bytes([0xF2, 0x72]) * 500, "ulaw") == "")    # +-104
    check("silence: no audio at all is silent", sbc.silence_check(b"", "wav") != ""
          and sbc.silence_check(b"", "ulaw") != "")


def test_the_config_folder_is_named_as_home_assistant_shows_it():
    d = Path(tempfile.mkdtemp())
    try:
        mi = d / "mountinfo"
        mi.write_text(
            "601 590 0:52 / / rw,relatime master:1 - overlay overlay rw\n"
            "639 601 8:8 /supervisor/share /share rw - ext4 /dev/sda8 rw\n"
            "640 601 8:8 /supervisor/addons/data/0123abcd_switchboard /data rw - ext4 /dev/sda8 rw\n"
            "641 601 8:8 /supervisor/app_configs/0123abcd_switchboard /config rw - ext4 /dev/sda8 rw\n")
        check("folder: from the /config mount's source",
              sbc.addon_config_folder(str(mi)) == "/app_configs/0123abcd_switchboard")
        # The Supervisor's name for that folder before it renamed it.
        mi.write_text("641 601 8:8 /supervisor/addon_configs/0123abcd_switchboard /config rw - ext4 /dev/sda8 rw\n")
        check("folder: an older Supervisor's name for it",
              sbc.addon_config_folder(str(mi)) == "/addon_configs/0123abcd_switchboard")
        mi.write_text("641 601 8:8 /x/app_configs/local_switchboard/ /config rw - ext4 /dev/sda8 rw\n")
        check("folder: a local install", sbc.addon_config_folder(str(mi)) == "/app_configs/local_switchboard")
        mi.write_text("641 601 8:8 /x/elsewhere/local_switchboard /config rw - ext4 /dev/sda8 rw\n")
        check("folder: an unknown parent is not echoed; the current name is used",
              sbc.addon_config_folder(str(mi)) == "/app_configs/local_switchboard")
        mi.write_text("641 601 8:8 /x/Bad\\040Name /config rw - ext4 /dev/sda8 rw\n")
        check("folder: anything unexpected is not echoed",
              sbc.addon_config_folder(str(mi)) == "/app_configs/<slug>")
        check("folder: no mount table", sbc.addon_config_folder(str(d / "absent")) == "/app_configs/<slug>")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _glob_body(glob: str) -> str:
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**", i):
            out, i = out + ".*", i + 2
            continue
        c = glob[i]
        if c == "*":
            out += "[^/]*"
        elif c == "{":
            j = glob.index("}", i)
            out += "(?:" + "|".join(_glob_body(a) for a in glob[i + 1:j].split(",")) + ")"
            i = j
        else:
            out += re.escape(c)
        i += 1
    return out


def _glob_re(glob: str):
    """An AppArmor path glob as a regex: {a,b} alternatives, ** and *."""
    return re.compile(_glob_body(glob) + r"\Z")


def test_the_profile_already_allows_every_path_the_feature_uses():
    profile = (ADDON / "apparmor.txt").read_text()
    head = "\n  profile switchboard_config flags=(attach_disconnected,mediate_deleted) {\n"
    before, _h, after = profile.partition(head)
    child, _c, rest = after.partition("\n  }\n")

    def rules(text):
        out = []
        for line in text.splitlines():
            m = re.match(r"^\s*(audit\s+)?(deny\s+)?(owner\s+)?(/\S*)\s+([a-zA-Z]+),$", line)
            if m:
                out.append((bool(m.group(2)), _glob_re(m.group(4)), m.group(5), m.group(4)))
        return out

    def allowed(rule_set, path, perm):
        grants = any(not deny and rx.match(path) and perm in mask for deny, rx, mask, _ in rule_set)
        denied = any(deny and rx.match(path) and perm in mask for deny, rx, mask, _ in rule_set)
        return grants and not denied

    kid, parent = rules(child), rules(before + rest)
    check("apparmor: the glob reader understands the rules it reads",
          _glob_re("/var/lib/asterisk/{agi-bin,sounds,moh}/{,**}").match(
              "/var/lib/asterisk/sounds/en/switchboard/sw-greeting.ulaw") is not None
          and _glob_re("/run/switchboard/{,**}").match("/run/switchboard/") is not None)
    dest = sbc.PROMPT_REF.PROMPTS_DIR
    for path in (f"{dest}/", f"{dest}/sw-greeting.ulaw", "/run/switchboard/.prompts-x1y2/",
                 "/run/switchboard/.prompts-x1y2/sw-greeting.wav"):
        check(f"apparmor: switchboard-config may write {path}", allowed(kid, path, "w"))
    for path in ("/config/prompts/", "/config/prompts/sw-greeting.wav",
                 "/proc/self/mountinfo", str(PROMPT_REF_PATH).replace(str(ROOTFS), "")):
        check(f"apparmor: switchboard-config may read {path}", allowed(kid, path, "r"))
    # The parent's blanket "file," grants everything its denies do not take
    # away: Asterisk and the AGIs (uid 100) read the installed prompts and
    # the resolver there.
    for path in (f"{dest}/sw-greeting.ulaw", "/usr/share/switchboard/prompts/prompt_ref.py"):
        check(f"apparmor: nothing denies reading {path}",
              not any(deny and rx.match(path) and "r" in mask for deny, rx, mask, _ in parent))
    check("apparmor: the shipped prompts stay write-locked",
          any(deny and rx.match("/var/lib/asterisk/sounds/en/switchboard/sw-greeting.wav")
              and "w" in mask for deny, rx, mask, _ in parent))


def test_the_guide_documents_every_prompt():
    docs = (ADDON / "DOCS.md").read_text(encoding="utf-8")
    section = docs[docs.index("### Custom prompts"):]
    section = section[:section.index("\n## ")]
    rows = {m.group(1): m.group(2) for m in re.finditer(r"^\| `(sw-[a-z0-9-]+)` \| (.*) \|$", section, re.M)}
    check(f"docs: one row per shipped prompt: {sorted(set(NAMES) ^ set(rows))}", set(rows) == set(NAMES))
    for tone in ("sw-beep", "sw-tone", "sw-endtone"):
        check(f"docs: {tone} is marked as a tone", "tone" in rows[tone].lower())
    for name in UNUSED:
        check(f"docs: {name} is marked as not played", "not played" in rows[name].lower())
    # agi_speech.listen() and the operator and wake-up AGIs play it after
    # RECORD FILE returns, while the speech is transcribed.
    check("docs: sw-onemoment plays after the caller speaks, not while it listens",
          "after you speak" in rows["sw-onemoment"] and "while it listens" not in rows["sw-onemoment"])
    check("docs: says which file plays when both are given",
          "When both are given, the phones play the `.ulaw`" in section)
    check("docs: no File editor (it cannot browse app_configs unless enforce_basepath is off)",
          "File editor" not in section)
    for words in ("/app_configs/<slug>/prompts", "`addon_configs`", "8 kHz", "mono", "16-bit", "µ-law",
                  "ffmpeg", "sox", "restart"):
        check(f"docs: the section says {words!r}", words in section)


if __name__ == "__main__":
    import _runner
    raise SystemExit(_runner.run(globals()))
