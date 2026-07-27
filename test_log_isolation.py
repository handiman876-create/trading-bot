"""
Regression tests for test-log isolation.

WHY THIS EXISTS: trade_logger installs logging.FileHandler(config.APP_LOG_FILE)
at module import time. Under pytest that import happens during COLLECTION,
before any fixture runs, so conftest's autouse redirect was structurally too
late and every test's log output was appended to the live logs/bot.log. The
damage was not hypothetical: 180 lines of AAA/BBB fixture chatter landed in the
production log, including a fabricated

    [WARNING] STOP-LOSS EXIT NVDA long x238 @ 205.00 (stop=210.00 ...)
    [ERROR]   STOP-LOSS EXIT NVDA: sell order failed — retrying next cycle

that reads exactly like a live incident (NVDA was never sold; it is still held
at 238 shares), and it poisoned every grep-based counter audit — a plain
`grep -c "SUSTAIN"` over the production log reported sustain activity that had
never occurred in a live session.

These tests pin the two properties that prevent a recurrence.
"""

import logging
import os
import tempfile

import config


def test_config_detects_test_run():
    """config._IS_TEST must be true here — this IS a test run."""
    assert config._IS_TEST is True, (
        "config did not detect a test run; the log prefix will not be applied "
        "and production logs are writable from the suite"
    )


def test_log_paths_are_not_production():
    """No log path may point at the live logs/ directory."""
    for name in ("APP_LOG_FILE", "TRADE_LOG_FILE", "PERF_LOG_FILE"):
        path = os.path.realpath(getattr(config, name))
        assert not path.endswith("/logs/bot.log"), f"{name} points at the live app log"
        assert not path.endswith("/logs/trades.log"), f"{name} points at the live trade log"
        assert not path.endswith("/logs/performance.log"), f"{name} points at the live perf log"


def test_log_paths_are_in_tmpdir():
    """conftest redirects all three into the system temp dir."""
    tmproot = os.path.realpath(tempfile.gettempdir())
    for name in ("APP_LOG_FILE", "TRADE_LOG_FILE", "PERF_LOG_FILE"):
        path = os.path.realpath(getattr(config, name))
        assert path.startswith(tmproot + os.sep), (
            f"{name} = {path} is outside {tmproot}; a test write would escape the sandbox"
        )


def test_prefix_applies_without_conftest():
    """The config-level prefix is the floor that holds for `python3 test_x.py`,
    where conftest never loads. Recompute the raw path config would produce."""
    assert config._LOG_PREFIX == "test_", (
        f"expected the test log prefix, got {config._LOG_PREFIX!r} — a direct "
        f"`python3 test_x.py` run would write to the production log"
    )


def test_no_root_file_handler_writes_outside_tmpdir():
    """The live FileHandler itself — not just the config string — must be in tmp.

    This is the assertion that would have caught the original bug: APP_LOG_FILE
    could be patched to a tmpdir while the already-installed handler kept its fd
    on logs/bot.log."""
    tmproot = os.path.realpath(tempfile.gettempdir())
    offenders = []
    for handler in logging.root.handlers:
        if not isinstance(handler, logging.FileHandler):
            continue
        path = os.path.realpath(getattr(handler, "baseFilename", "") or "")
        if not path.startswith(tmproot + os.sep):
            offenders.append(path)
    assert not offenders, f"root logger writes outside the temp dir: {offenders}"


def test_emitting_a_log_does_not_touch_production_log():
    """End-to-end: import trade_logger (which calls basicConfig), emit a record
    with the exact fixture symbols that polluted the log, and assert the live
    file did not grow."""
    prod = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "bot.log")
    before = os.path.getsize(prod) if os.path.exists(prod) else None

    import trade_logger  # noqa: F401  — imported for its basicConfig side effect
    logging.getLogger("strategy").warning(
        "STOP-LOSS EXIT AAA long x10 @ 100.00 (stop=110.00 entry=100.00) — exit #1")
    logging.getLogger("strategy").info(
        "CROSS SUSTAIN BLOCK BBB — bullish cross held 0.0 min (sustain blocks #99)")
    for handler in logging.root.handlers:
        handler.flush()

    after = os.path.getsize(prod) if os.path.exists(prod) else None
    assert after == before, (
        f"production log {prod} changed size {before} -> {after} during a test; "
        f"test output is leaking into live logs again"
    )


if __name__ == "__main__":
    # Direct-run path: no conftest, so this exercises the config-level floor only.
    test_config_detects_test_run()
    test_log_paths_are_not_production()
    test_prefix_applies_without_conftest()
    print("OK (direct run: config-level prefix verified)")
