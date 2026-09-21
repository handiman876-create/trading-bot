#!/usr/bin/env python3
"""Standalone test runner that isolates state files before anything imports.

    python3 run_test.py test_sentiment.py       # one file
    python3 run_test.py test_stops.py test_vix_regime.py
    python3 run_test.py                          # all test_*.py, one per process

WHY THIS EXISTS
    `python3 test_foo.py` is a deliberate convention (docs/runbook.md): pytest
    imports every module before running anything, so it structurally cannot see
    __main__ ordering bugs, and a direct run can. But conftest.py is a PYTEST
    hook, so the direct form never got conftest's redirects — on 2026-09-21 a
    standalone run wrote a synthetic SPY position into the live
    data/stop_prices.json and the equities bot loaded it on the next restart.

    config.py now floors this on its own: any detected test run writes under
    data/test/, so the bare `python3 test_foo.py` form is safe too. This wrapper
    is the ceiling — a throwaway tmpdir, plus an assertion that the redirect is
    actually in effect before a single test body runs.

WHY SUBPROCESSES AND NOT exec_module
    The convention's whole value is ONE FRESH PROCESS PER FILE. Exec'ing several
    targets in one interpreter shares module state between them and reintroduces
    exactly the cross-file ordering coupling the convention exists to expose.

THE argv0 TRAP
    config._detect_test_run() keys on sys.argv[0] starting with "test_". Running
    under this wrapper makes argv[0] "run_test.py", which would read as NOT a
    test run — and because TB_TEST_TMPDIR is deliberately ignored outside a
    detected test run, the redirect would silently not apply. Each target is
    therefore spawned as `python3 test_foo.py` so the child's own argv0 triggers
    detection; this parent only ever verifies, it never hosts a test.
"""
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))

# These must all resolve inside the tmpdir once config is imported under test
# detection. Listed by ATTRIBUTE NAME rather than checked reflectively so that
# adding an eighth state file to config without adding it here is a visible
# omission rather than a silently unguarded path.
_GUARDED = (
    "STOP_PRICE_FILE",
    "OPTIONS_POSITION_FILE",
    "MOMENTUM_ENTRY_FILE",
    "MOMENTUM_WATCHLIST_FILE",
    "SCREEN_AB_TRACKING_FILE",
    "SENTIMENT_REPORT_FILE",
    "DISCORD_WATERMARK_FILE",
    "TRADE_LEDGER_FILE",
)


def _verify_redirect(tmpdir: str, sample_target: str) -> None:
    """Import config the way a child will see it and prove every state path
    landed in the tmpdir. Raises SystemExit(2) rather than returning a flag —
    a redirect that did not take must stop the run, not warn during it."""
    saved_argv0 = sys.argv[0]
    sys.argv[0] = sample_target          # make _detect_test_run() say yes
    sys.path.insert(0, _HERE)
    try:
        import config
    finally:
        sys.argv[0] = saved_argv0

    problems = []
    if not config._IS_TEST:
        problems.append("config._IS_TEST is False — test detection did not fire")
    for attr in _GUARDED:
        path = getattr(config, attr, None)
        if path is None:
            problems.append(f"{attr}: missing from config")
        elif not os.path.abspath(path).startswith(os.path.abspath(tmpdir)):
            problems.append(f"{attr}: {path}")

    if problems:
        print("REDIRECT FAILED — production state at risk, refusing to run:",
              file=sys.stderr)
        for p in problems:
            print(f"    {p}", file=sys.stderr)
        print(f"  expected everything under: {tmpdir}", file=sys.stderr)
        print("  config.py and this wrapper have drifted apart; fix before "
              "running tests on a box with live state.", file=sys.stderr)
        raise SystemExit(2)


def main() -> int:
    targets = sys.argv[1:] or sorted(
        f for f in os.listdir(_HERE)
        if f.startswith("test_") and f.endswith(".py"))
    if not targets:
        print("no test files found", file=sys.stderr)
        return 2

    missing = [t for t in targets if not os.path.exists(os.path.join(_HERE, t))]
    if missing:
        print(f"no such test file(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    tmpdir = tempfile.mkdtemp(prefix="tbtest_")
    env = dict(os.environ, TB_TEST_TMPDIR=tmpdir)
    # The parent must see it too, or the verification below checks the wrong
    # thing — it would import config with the variable unset and pass trivially.
    os.environ["TB_TEST_TMPDIR"] = tmpdir
    _verify_redirect(tmpdir, targets[0])

    failed = []
    for target in targets:
        proc = subprocess.run([sys.executable, target], cwd=_HERE, env=env)
        status = "PASS" if proc.returncode == 0 else f"FAIL ({proc.returncode})"
        print(f"  {status:12} {target}")
        if proc.returncode != 0:
            failed.append(target)

    print(f"\n{len(targets) - len(failed)}/{len(targets)} passed   "
          f"state isolated to {tmpdir}")
    if failed:
        print("failed: " + ", ".join(failed), file=sys.stderr)
    # Non-zero on ANY failure. A runner that swallows a failing test and exits 0
    # is worse than no runner: CI and the runbook check both read this code.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
