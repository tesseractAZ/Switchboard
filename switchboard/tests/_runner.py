"""The shared standalone runner behind every test file's ``__main__`` block.

    python3 switchboard/tests/<file>.py

It runs every ``test_*`` function the module defines, in definition order, so a
new test can never be missed by a hand-maintained list, which is how 182 tests
sat unrun by these entry points until v0.110.0 (counted by tracing each runner). Tests that take pytest fixtures
are listed as skipped, so the gap stays visible. pytest remains the full gate.
"""
import inspect


def run(namespace: dict) -> int:
    ran, skipped = 0, []
    for name, fn in list(namespace.items()):
        if not (name.startswith("test_") and inspect.isfunction(fn)):
            continue
        if inspect.signature(fn).parameters:
            skipped.append(name)
            continue
        fn()
        ran += 1
    failures = namespace.get("_failures", 0)
    if skipped:
        print(f"\nSKIPPED (pytest fixtures required; run pytest for these): {', '.join(skipped)}")
    print(f"\n{'FAILED' if failures else 'OK'}: {ran} test(s) run, {failures} failure(s)")
    return 1 if failures else 0
