"""The three append-only ledgers must ROTATE, not vanish.

    python3 -m pytest switchboard/tests/test_ledger_rotation.py

Switchboard writes three unbounded JSONL ledgers, each with its own byte cap:

    heartbeat.jsonl          rtpmon/poller.py            4 MiB
    delivery-outcomes.jsonl  webui/delivery.py           2 MiB
    callqos-outcomes.jsonl   usr/bin/switchboard-callqos 4 MiB

Through v0.76.0 all three "enforced" the cap with `open(path, "w")` followed by
`pass` — which truncates the file to ZERO BYTES. The comment above the poller's
copy read "Truncating keeps the newest records, which are the ones a reader
wants", describing the exact opposite of what the code did. At the cap the whole
forensic record disappeared, and it disappeared SILENTLY: an empty ledger and a
quiet, healthy system are indistinguishable to every reader in this repo, which
is the precise failure mode these ledgers exist to rule out.

The helper is duplicated rather than imported because the three files live in
three directories with no shared package and are loaded by three different
interpreters at runtime. `test_the_three_implementations_do_not_drift` is what
makes the duplication safe: it runs all three over one input and requires the
resulting bytes to be identical.
"""
import json
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "rootfs"
SOURCES = {
    "poller": ROOT / "usr/share/switchboard/rtpmon/poller.py",
    "delivery": ROOT / "usr/share/switchboard/webui/delivery.py",
    "callqos": ROOT / "usr/bin/switchboard-callqos",
}


ROTATORS = {}
for _n, _p in SOURCES.items():
    _mod = SourceFileLoader(f"switchboard_rot_{_n}", str(_p)).load_module()
    ROTATORS[_n] = _mod._rotate_tail

CAP = 4096


def _ledger(tmp_path, n_records, pad=100):
    """A JSONL ledger of `n_records` numbered records, oldest first."""
    p = tmp_path / "ledger.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        for i in range(n_records):
            fh.write(json.dumps({"seq": i, "pad": "x" * pad}) + "\n")
    return p


@pytest.mark.parametrize("impl", sorted(ROTATORS))
def test_a_ledger_under_the_cap_is_not_touched(tmp_path, impl):
    p = _ledger(tmp_path, 5)
    before = p.read_bytes()
    ROTATORS[impl](str(p), CAP)
    assert p.read_bytes() == before


@pytest.mark.parametrize("impl", sorted(ROTATORS))
def test_a_ledger_over_the_cap_is_not_emptied(tmp_path, impl):
    """The regression. `open(path, "w")` + `pass` made this file zero bytes."""
    p = _ledger(tmp_path, 400)
    assert p.stat().st_size > CAP, "fixture must actually exceed the cap"
    ROTATORS[impl](str(p), CAP)
    assert p.stat().st_size > 0, (
        f"{impl} emptied the ledger — this is the v0.77.0 bug returning")


@pytest.mark.parametrize("impl", sorted(ROTATORS))
def test_rotation_keeps_the_newest_records(tmp_path, impl):
    p = _ledger(tmp_path, 400)
    ROTATORS[impl](str(p), CAP)
    seqs = [json.loads(l)["seq"] for l in p.read_text().splitlines() if l]
    assert seqs, "nothing survived"
    assert seqs[-1] == 399, "the newest record must survive"
    assert seqs == list(range(seqs[0], 400)), "survivors must be contiguous"
    assert seqs[0] > 0, "the fixture should have forced some loss"


@pytest.mark.parametrize("impl", sorted(ROTATORS))
def test_every_surviving_line_is_a_whole_record(tmp_path, impl):
    """Cutting at a byte offset lands mid-record. The first line must be whole.

    A reader that json.loads() its way down the file would raise on a partial
    first line — and every reader in this repo does exactly that.
    """
    for pad in range(0, 40):          # sweep the cut across a record boundary
        p = _ledger(tmp_path, 400, pad=pad)
        ROTATORS[impl](str(p), CAP)
        for line in p.read_text().splitlines():
            json.loads(line)          # raises if the cut landed mid-record


@pytest.mark.parametrize("impl", sorted(ROTATORS))
def test_rotation_gets_the_file_back_under_the_cap(tmp_path, impl):
    p = _ledger(tmp_path, 400)
    ROTATORS[impl](str(p), CAP)
    assert p.stat().st_size <= CAP


@pytest.mark.parametrize("impl", sorted(ROTATORS))
def test_rotation_is_stable_under_repetition(tmp_path, impl):
    """Rotating an already-rotated file must be a no-op, not another bite."""
    p = _ledger(tmp_path, 400)
    ROTATORS[impl](str(p), CAP)
    once = p.read_bytes()
    for _ in range(3):
        ROTATORS[impl](str(p), CAP)
    assert p.read_bytes() == once


@pytest.mark.parametrize("impl", sorted(ROTATORS))
def test_a_missing_ledger_does_not_raise(tmp_path, impl):
    """Trimming is best-effort: it must never fail the write it precedes."""
    ROTATORS[impl](str(tmp_path / "nope.jsonl"), CAP)


@pytest.mark.parametrize("impl", sorted(ROTATORS))
def test_an_unreadable_ledger_does_not_raise(tmp_path, impl):
    p = _ledger(tmp_path, 400)
    os.chmod(p, 0o000)
    try:
        ROTATORS[impl](str(p), CAP)
    finally:
        os.chmod(p, 0o644)


@pytest.mark.parametrize("impl", sorted(ROTATORS))
def test_the_file_keeps_its_inode(tmp_path, impl):
    """Rewrite in place — a reader holding the open path must not be orphaned.

    Renaming a rotated file away is the textbook approach and is wrong here:
    `switchboard-console` and the web UI tail these paths, and a rename leaves
    them reading a file nobody writes to any more.
    """
    p = _ledger(tmp_path, 400)
    ino = p.stat().st_ino
    ROTATORS[impl](str(p), CAP)
    assert p.stat().st_ino == ino


def test_the_three_implementations_do_not_drift(tmp_path):
    """The helper is copied into three files. Copies rot; this is the guard."""
    results = {}
    for impl, fn in sorted(ROTATORS.items()):
        d = tmp_path / impl
        d.mkdir()
        p = _ledger(d, 400)
        fn(str(p), CAP)
        results[impl] = p.read_bytes()
    distinct = set(results.values())
    assert len(distinct) == 1, (
        "the three _rotate_tail copies disagree: "
        + ", ".join(f"{k}={len(v)}B" for k, v in results.items()))


def test_no_ledger_still_truncates_to_empty():
    """A source-level guard against the exact shape that caused the data loss.

    Three files, three authors, one idiom. Grepping for it is cheap insurance
    that a fourth ledger does not reintroduce it.
    """
    offenders = []
    for name, path in SOURCES.items():
        text = path.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if 'open(' in line and '"w"' in line and 'with' in line:
                nxt = text.splitlines()[i] if i < len(text.splitlines()) else ""
                if nxt.strip() == "pass":
                    offenders.append(f"{name}:{i}")
    assert not offenders, f"truncate-to-empty idiom is back at {offenders}"
