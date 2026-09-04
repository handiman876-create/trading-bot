"""
Pytest-wide safety net: redirect every file the bot WRITES to a per-test temp
directory.

WHY THIS EXISTS: the test modules redirect their state-file paths only inside
their `if __name__ == "__main__"` runners. Under pytest that block never runs, so
the test functions used the REAL paths — a pytest run once overwrote
data/stop_prices.json (destroying live trailing stops) and appended fake trades
to logs/trades.log. This autouse fixture makes every test hermetic regardless of
how it's invoked, so the suite can never mutate live trade/stop/ledger state.

THE APP LOG IS A SPECIAL CASE — it cannot be fixed by the autouse fixture below.
trade_logger installs `logging.FileHandler(config.APP_LOG_FILE)` via
basicConfig() at MODULE IMPORT time. Under pytest that import runs during
COLLECTION, which is strictly before any fixture executes, so by the time
`isolate_bot_state` gets control the handler already holds an open fd on the
live logs/bot.log. Monkeypatching APP_LOG_FILE at that point changes nothing —
basicConfig captured the string, not the attribute. That is how 180 lines of
AAA/BBB fixture output (and a fabricated "STOP-LOSS EXIT NVDA ... sell order
failed") ended up in the production log and poisoned the CROSS SUSTAIN counters.

So the app log is handled in two earlier layers instead:
  1. config._IS_TEST  -> flips the log prefix to "test_" at config import, which
                         is the floor that holds even for `python3 test_x.py`.
  2. this module's import-time block below -> runs before pytest collects any
                         test module, so it lands before trade_logger is
                         imported and the FileHandler is built on a tmpdir path.
Layer 3 (_redirect_existing_log_handlers) catches anything that still slipped
through — e.g. a module imported by conftest itself, or an import order we did
not anticipate.
"""

import atexit
import logging
import os
import shutil
import tempfile

import pytest

# ── Layer 2: import-time app-log redirect ─────────────────────────────────────
# conftest.py is imported by pytest BEFORE it collects test modules, so this runs
# before anything can `import trade_logger` and bind a FileHandler.
_LOG_TMPDIR = tempfile.mkdtemp(prefix="bot-testlogs-")
atexit.register(shutil.rmtree, _LOG_TMPDIR, True)

import config  # noqa: E402  (must follow the tmpdir creation above)

config.LOG_DIR        = _LOG_TMPDIR
config.APP_LOG_FILE   = os.path.join(_LOG_TMPDIR, "bot.log")
config.TRADE_LOG_FILE = os.path.join(_LOG_TMPDIR, "trades.log")
config.PERF_LOG_FILE  = os.path.join(_LOG_TMPDIR, "performance.log")
# The CRITICAL sink needs the same treatment, and needs it MORE: it is the file a
# human reads to decide whether the bot is in trouble, so a fabricated fixture
# CRITICAL landing there is worse than one landing in bot.log.
config.CRITICAL_ALERT_FILE = os.path.join(_LOG_TMPDIR, "critical_alerts.log")
# The Discord watermark follows the sinks into the tmpdir. It is deliberately
# UNPREFIXED in production (both bots share one offset), so without this a test
# run would read and rewrite the live bots' watermark — and a test that advanced
# it past a real un-pushed CRITICAL would silently consume that alert.
config.DISCORD_WATERMARK_FILE = os.path.join(_LOG_TMPDIR, "alert_watermarks.json")
# Belt and braces: no test may ever reach the network here. Every test that
# exercises pushing sets this itself and restores it.
config.DISCORD_WEBHOOK_URL = ""


def _redirect_existing_log_handlers() -> int:
    """Re-point any FileHandler that is already writing outside the temp dir.

    Layer 3. Returns the number of handlers moved — 0 is the healthy result and
    means layers 1+2 did their job. Anything above 0 is a real finding: some
    import path bound a handler before this module ran.
    """
    moved = 0
    tmproot = os.path.realpath(tempfile.gettempdir())
    for handler in list(logging.root.handlers):
        if not isinstance(handler, logging.FileHandler):
            continue
        current = os.path.realpath(getattr(handler, "baseFilename", "") or "")
        if current.startswith(tmproot + os.sep):
            continue
        handler.close()
        logging.root.removeHandler(handler)
        replacement = logging.FileHandler(os.path.join(_LOG_TMPDIR, "bot.log"))
        replacement.setFormatter(handler.formatter)
        replacement.setLevel(handler.level)
        logging.root.addHandler(replacement)
        moved += 1
    return moved


_HANDLERS_REDIRECTED = _redirect_existing_log_handlers()


@pytest.fixture(scope="session", autouse=True)
def isolate_app_log():
    """Session-scoped: catch handlers installed after conftest import (a test
    module calling basicConfig at import, say) and report the count so a
    regression is visible rather than silent."""
    _redirect_existing_log_handlers()
    yield


@pytest.fixture(autouse=True)
def isolate_bot_state(tmp_path, monkeypatch):
    import config
    import strategy

    monkeypatch.setattr(config, "APP_LOG_FILE",   str(tmp_path / "bot.log"), raising=False)
    monkeypatch.setattr(config, "TRADE_LOG_FILE", str(tmp_path / "trades.log"), raising=False)
    monkeypatch.setattr(config, "PERF_LOG_FILE",  str(tmp_path / "performance.log"), raising=False)
    monkeypatch.setattr(strategy, "_STOPS_PATH",       str(tmp_path / "stop_prices.json"), raising=False)
    monkeypatch.setattr(strategy, "_MOM_ENTRIES_PATH", str(tmp_path / "momentum_entries.json"), raising=False)
    monkeypatch.setattr(strategy, "_OPT_POSITIONS_PATH", str(tmp_path / "options_positions.json"), raising=False)

    # The broker stop floor is OFF for every test unless the test opts in. It
    # reaches the network through tc.place_equity_order with keyword arguments
    # (order_type/duration/stop_price), and the stubs across the older test
    # modules are positional-only `(account_id, symbol, side, qty)` — so with the
    # live flag True, every module that arms a stop dies on a TypeError from a
    # feature it never asked about. Pinning it here keeps the suite hermetic
    # against the live config value, which is the whole point of this fixture.
    # test_broker_floor.py sets it True per-test and restores it.
    monkeypatch.setattr(config, "ENABLE_BROKER_STOP_FLOOR", False, raising=False)

    # Same treatment, same reason. The profit-floor ladder is a THIRD stop source
    # that silently overrides the ATR trail whenever a rung is tighter, so with
    # the live flag True it changes the answer in tests that are not about it at
    # all. It did: 79589ca gave shorts an 8%->5% first rung, and two ATR-trail
    # tests in test_stops.py (a module with zero profit-floor references) started
    # failing because a short at +10% now floors at 95.00 instead of trailing to
    # 90 + 2.5*4 = 100.00. The trail was right and the ladder was right; the
    # tests were measuring both at once.
    # test_profit_floor.py owns this feature and sets the flag True per-test.
    monkeypatch.setattr(config, "ENABLE_PROFIT_FLOOR", False, raising=False)

    # Same treatment, same reason, and it bit harder than the other two: the
    # water floor is a FOURTH stop source and, unlike the ladder, it arms on any
    # position whose run has cleared WATER_FLOOR_K ATRs — which is most fixtures
    # that move price far enough to trail at all. Turning it on live took the
    # suite from 3 known failures to 40, across test_stops.py, the breakeven-lock
    # modules and the futures stops, none of which reference it. Every one was the
    # feature working: the floor is simply more protective than the level those
    # tests pinned.
    # test_water_floor.py owns this feature and sets the flag True per-test.
    monkeypatch.setattr(config, "ENABLE_WATER_FLOOR", False, raising=False)

    # performance_analyzer, if a test drives its file-writing paths.
    try:
        import performance_analyzer as pa
        monkeypatch.setattr(pa, "LEDGER_PATH", str(tmp_path / "trade_ledger.json"), raising=False)
        monkeypatch.setattr(pa, "REPORT_JSON", str(tmp_path / "performance_report.json"), raising=False)
        monkeypatch.setattr(pa, "REPORT_TXT",  str(tmp_path / "performance_report.txt"), raising=False)
        monkeypatch.setattr(pa, "STOPS_PATH",  str(tmp_path / "stop_prices.json"), raising=False)
    except ImportError:
        pass

    # A/B screen experiment writes (tracker + fundamentals cache) — redirect so a
    # pytest run can never read or clobber live experiment state.
    monkeypatch.setattr(config, "SCREEN_AB_TRACKING_FILE", str(tmp_path / "screen_ab_tracking.json"), raising=False)
    monkeypatch.setattr(config, "FUNDAMENTALS_CACHE_FILE", str(tmp_path / "fundamentals_cache.json"), raising=False)

    yield
