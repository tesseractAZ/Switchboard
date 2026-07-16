"""Guards the HA Configuration-tab translation file against drift.

    python3 switchboard/tests/test_translations.py

HA renders `translations/en.yaml` `configuration:` entries as the field label + helper
text for each option in config.yaml. A key that doesn't byte-match a config option is
silently ignored (the field falls back to its raw key); a missing key shows no help. So
this pins: every top-level option has a name+description, and there are no stray keys.
"""
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PASS (pyyaml unavailable — skipping translation completeness check)")
    raise SystemExit(0)

_ROOT = Path(__file__).resolve().parents[1]
_failures = 0


def check(name, cond):
    global _failures
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures += 1


def test_translation_completeness():
    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text())
    tr = yaml.safe_load((_ROOT / "translations" / "en.yaml").read_text())
    opts = set((cfg.get("options") or {}).keys())
    conf = (tr or {}).get("configuration") or {}
    keys = set(conf.keys())

    missing = sorted(opts - keys)
    extra = sorted(keys - opts)
    check(f"translations: every option is translated (missing: {missing or 'none'})", not missing)
    check(f"translations: no stray/typo'd keys (extra: {extra or 'none'})", not extra)

    incomplete = [k for k, v in conf.items()
                  if not (isinstance(v, dict) and str(v.get("name", "")).strip() and str(v.get("description", "")).strip())]
    check(f"translations: each entry has name + description (incomplete: {incomplete or 'none'})", not incomplete)


if __name__ == "__main__":
    test_translation_completeness()
    print(f"\n{'FAILED' if _failures else 'OK'} — {_failures} failure(s)")
    sys.exit(1 if _failures else 0)
