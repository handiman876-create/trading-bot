"""
Unit tests for bar-history outage reporting (_note_history_gap) — NO network.

Regression cover for the silent path found on 2026-08-04. Every evaluate_*
function opens with:

    history = tc.get_historical(symbol, days=90)
    if not history:
        return

That return aborts the cycle BEFORE any protective logic runs — the trailing
stop for equities and futures, the close-on-opposite-state for options. During a
TradeStation /barcharts outage (63 failures that session) PLTR was held and went
6 consecutive polls unevaluated between 14:46 and 15:00, and NOTHING in the log
said so. DXCM burned 7 polls the same way while flat, which cost nothing. The
two were indistinguishable.

The rule: a skipped poll on a HELD name logs and counts; on a flat name it stays
silent. Consecutive misses escalate WARNING -> ERROR at HISTORY_GAP_ERROR_STREAK,
mirroring the positions guard (test_positions_guard.py) which already escalates a
sustained outage for exactly this exposure.

This is observability only. These tests also pin that NO order is placed and no
trading behaviour changes on the empty-history path.

Run:  python3 test_history_gap.py
"""

import logging
import os
import tempfile

import _testlib
import strategy


# ── Test doubles ──────────────────────────────────────────────────────────────
_orders = []


def _fake_place(account_id, symbol, side, qty):
    _orders.append((symbol, side, qty))
    return {"order": {"id": "T1"}}


def _positions(symbol=None, qty=0):
    """Broker positions payload. Empty list = flat."""
    if not symbol or qty == 0:
        return []
    return [{"symbol": symbol, "quantity": str(qty), "AveragePrice": "100.0"}]


def _reset():
    _orders.clear()
    strategy._history_gaps_held = 0
    strategy._history_gap_streak.clear()
    strategy.config.HISTORY_GAP_ERROR_STREAK = 3
    strategy.tc.place_equity_order = _fake_place
    strategy.tc.get_historical = lambda *a, **k: []      # the outage
    strategy.tc.get_quote = lambda s: {"last": 100.0}
    for path in (strategy._STOPS_PATH, strategy._MOM_ENTRIES_PATH):
        _testlib.safe_remove(path)


class _Capture:
    """Collect (levelno, message) for records emitted inside the block."""

    def __init__(self):
        self.records = []

    def __enter__(self):
        self._h = logging.Handler()
        self._h.emit = lambda r: self.records.append((r.levelno, r.getMessage()))
        strategy.logger.addHandler(self._h)
        self._prev = strategy.logger.level
        strategy.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *a):
        strategy.logger.removeHandler(self._h)
        strategy.logger.setLevel(self._prev)

    def matching(self, needle):
        return [r for r in self.records if needle in r[1]]


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_flat_name_is_silent():
    """A skipped poll on a flat name protects nothing. Counting it would measure
    the broker's uptime, not our exposure — DXCM's 7 flat skips must produce
    nothing at all."""
    _reset()
    with _Capture() as cap:
        for _ in range(7):
            strategy.evaluate_stock("DXCM", "ACC", _positions(), 100_000.0)
    assert cap.matching("POSITION UNCHECKED") == [], cap.records
    assert strategy._history_gaps_held == 0
    assert _orders == []


def test_held_name_warns_and_counts():
    """PLTR's case: held, history unavailable, stop not evaluated. This is the
    line that did not exist on 2026-08-04."""
    _reset()
    with _Capture() as cap:
        strategy.evaluate_stock("PLTR", "ACC", _positions("PLTR", 310), 100_000.0)
    hits = cap.matching("POSITION UNCHECKED")
    assert len(hits) == 1, cap.records
    level, msg = hits[0]
    assert level == logging.WARNING, msg
    assert "PLTR" in msg and "held=310" in msg
    assert "unchecked #1" in msg, msg
    assert strategy._history_gaps_held == 1


def test_streak_escalates_to_error():
    """A blip is a WARNING; a sustained outage is an ERROR, because a stop is
    going unchecked the whole time. Same contract as the positions guard."""
    _reset()
    with _Capture() as cap:
        for _ in range(4):
            strategy.evaluate_stock("PLTR", "ACC", _positions("PLTR", 310), 100_000.0)
    hits = cap.matching("POSITION UNCHECKED")
    levels = [lv for lv, _ in hits]
    assert levels == [logging.WARNING, logging.WARNING,
                      logging.ERROR, logging.ERROR], levels
    assert strategy._history_gaps_held == 4


def test_good_fetch_clears_the_streak():
    """Escalation must describe a CURRENT outage, not accumulate unrelated blips
    across a session. Two misses, one good poll, one miss => back to WARNING."""
    _reset()
    pos = _positions("PLTR", 310)
    with _Capture() as cap:
        strategy.evaluate_stock("PLTR", "ACC", pos, 100_000.0)
        strategy.evaluate_stock("PLTR", "ACC", pos, 100_000.0)
        # A good fetch that still yields no indicators: reaches _clear_history_gap
        # and returns before any trading logic.
        strategy.tc.get_historical = lambda *a, **k: [{"bar": 1}]
        strategy.ind.compute_indicators = lambda *a, **k: None
        strategy.evaluate_stock("PLTR", "ACC", pos, 100_000.0)
        strategy.tc.get_historical = lambda *a, **k: []
        strategy.evaluate_stock("PLTR", "ACC", pos, 100_000.0)
    levels = [lv for lv, _ in cap.matching("POSITION UNCHECKED")]
    assert levels == [logging.WARNING, logging.WARNING, logging.WARNING], levels


def test_streak_is_per_symbol():
    """One name's outage must not escalate another's first miss."""
    _reset()
    with _Capture() as cap:
        for _ in range(3):
            strategy.evaluate_stock("PLTR", "ACC", _positions("PLTR", 310), 100_000.0)
        strategy.evaluate_stock("MSFT", "ACC", _positions("MSFT", 50), 100_000.0)
    msft = [lv for lv, m in cap.matching("POSITION UNCHECKED") if "MSFT" in m]
    assert msft == [logging.WARNING], msft


def test_no_orders_placed_on_empty_history():
    """Observability only. The empty-history path must still return early and
    trade nothing — this fix adds a log line, not a decision."""
    _reset()
    for _ in range(5):
        strategy.evaluate_stock("PLTR", "ACC", _positions("PLTR", 310), 100_000.0)
        strategy.evaluate_stock("DXCM", "ACC", _positions(), 100_000.0)
    assert _orders == [], _orders


def test_counter_only_counts_held():
    """_history_gaps_held must measure exposure. A session of mostly-flat skips
    should leave it at the number of HELD skips, not the number of failures."""
    _reset()
    for _ in range(6):
        strategy.evaluate_stock("DXCM", "ACC", _positions(), 100_000.0)
    for _ in range(2):
        strategy.evaluate_stock("PLTR", "ACC", _positions("PLTR", 310), 100_000.0)
    assert strategy._history_gaps_held == 2


# ── pytest isolation (mirrors test_ema_gap.py) ────────────────────────────────
try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_strategy_globals():
        saved = {
            "place":  strategy.tc.place_equity_order,
            "hist":   strategy.tc.get_historical,
            "quote":  strategy.tc.get_quote,
            "ci":     strategy.ind.compute_indicators,
            "log":    strategy.log_trade,
            "streak": strategy.config.HISTORY_GAP_ERROR_STREAK,
        }
        strategy.log_trade = lambda *a, **k: None
        yield
        strategy.tc.place_equity_order  = saved["place"]
        strategy.tc.get_historical      = saved["hist"]
        strategy.tc.get_quote           = saved["quote"]
        strategy.ind.compute_indicators = saved["ci"]
        strategy.log_trade              = saved["log"]
        strategy.config.HISTORY_GAP_ERROR_STREAK = saved["streak"]
        strategy._history_gap_streak.clear()
        strategy._history_gaps_held = 0
except ImportError:
    pass


if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="history_gap_test_")
    strategy._STOPS_PATH       = os.path.join(_tmpdir, "stop_prices.json")
    strategy._MOM_ENTRIES_PATH = os.path.join(_tmpdir, "momentum_entries.json")
    _orig = {
        "place":  strategy.tc.place_equity_order,
        "hist":   strategy.tc.get_historical,
        "quote":  strategy.tc.get_quote,
        "ci":     strategy.ind.compute_indicators,
        "log":    strategy.log_trade,
        "streak": strategy.config.HISTORY_GAP_ERROR_STREAK,
    }
    strategy.log_trade = lambda *a, **k: None
    try:
        tests = [v for k, v in sorted(globals().items())
                 if k.startswith("test_") and callable(v)]
        passed = 0
        for t in tests:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        print(f"All {passed} assertions passed.")
    finally:
        strategy.tc.place_equity_order  = _orig["place"]
        strategy.tc.get_historical      = _orig["hist"]
        strategy.tc.get_quote           = _orig["quote"]
        strategy.ind.compute_indicators = _orig["ci"]
        strategy.log_trade              = _orig["log"]
        strategy.config.HISTORY_GAP_ERROR_STREAK = _orig["streak"]
