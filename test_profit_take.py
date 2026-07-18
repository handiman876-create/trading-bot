"""
Unit tests for the profit-taking scale-out (strategy._maybe_take_profit) — NO network.

Same harness as test_stops.py: monkeypatch strategy._STOPS_PATH to a throwaway
temp file and stub tc.place_equity_order / log_trade, so we exercise the trigger,
the idempotency latch, and the share math without hitting the API or placing orders.

Run:  python3 test_profit_take.py
"""

import os
import tempfile

import _testlib
import config
import strategy

# ── Test doubles ──────────────────────────────────────────────────────────────
_orders = []
_order_result = {"order": {"id": "P1"}}      # flip to None to simulate a failed order


def _fake_place(account_id, symbol, side, qty):
    _orders.append((symbol, side, qty))
    return _order_result


def _reset():
    global _order_result
    _orders.clear()
    _order_result = {"order": {"id": "P1"}}
    strategy._profit_takes = 0
    strategy.tc.place_equity_order = _fake_place
    _testlib.safe_remove(strategy._STOPS_PATH)
    # ensure config is at documented defaults for the arithmetic below
    config.ENABLE_PROFIT_TAKING = True
    config.PROFIT_TAKE_PCT = 0.12
    config.PROFIT_TAKE_FRACTION = 0.50
    config.PROFIT_TAKE_RSI_MIN = 60.0


def _seed(symbol, entry_price, profit_taken=None):
    """Write a minimal long stop record. profit_taken=None omits the field to
    exercise the missing-flag back-compat path."""
    rec = {"entry_price": entry_price, "atr_at_entry": 5.0, "opened": "2026-07-17",
           "bootstrapped": False, "direction": "long",
           "high_water": entry_price, "stop_price": entry_price - 12.5}
    if profit_taken is not None:
        rec["profit_taken"] = profit_taken
    strategy._save_stops({symbol: rec})


def _sig(close, rsi):
    return {"close": close, "rsi": rsi}


# ── Trigger boundary ──────────────────────────────────────────────────────────

def test_triggers_at_12pct_and_rsi_60():
    _reset(); _seed("DDOG", 100.0)
    took = strategy._maybe_take_profit("DDOG", 100, _sig(112.0, 60.0), "ACCT")
    assert took is True, "should trigger at +12% and RSI 60"
    assert _orders == [("DDOG", "sell", 50)], _orders          # floor(100*0.5)
    assert strategy._load_stops()["DDOG"]["profit_taken"] is True
    assert strategy._profit_takes == 1


def test_does_not_trigger_at_11_9pct():
    _reset(); _seed("DDOG", 100.0)
    took = strategy._maybe_take_profit("DDOG", 100, _sig(111.9, 70.0), "ACCT")
    assert took is False, "below +12% must not trigger"
    assert _orders == []
    assert "profit_taken" not in strategy._load_stops()["DDOG"], "no latch when it didn't fire"


def test_does_not_trigger_at_12pct_but_rsi_59():
    _reset(); _seed("DDOG", 100.0)
    took = strategy._maybe_take_profit("DDOG", 100, _sig(112.0, 59.0), "ACCT")
    assert took is False, "RSI below floor must not trigger even at +12%"
    assert _orders == []


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_does_not_trigger_twice():
    _reset(); _seed("DDOG", 100.0)
    first = strategy._maybe_take_profit("DDOG", 100, _sig(120.0, 70.0), "ACCT")
    # broker now reflects the partial sell; second cycle sees the remaining 50
    second = strategy._maybe_take_profit("DDOG", 50, _sig(125.0, 72.0), "ACCT")
    assert first is True and second is False, (first, second)
    assert _orders == [("DDOG", "sell", 50)], f"exactly one order, got {_orders}"
    assert strategy._profit_takes == 1


def test_respects_profit_taken_flag_in_file():
    _reset(); _seed("DDOG", 100.0, profit_taken=True)
    took = strategy._maybe_take_profit("DDOG", 195, _sig(130.0, 75.0), "ACCT")
    assert took is False, "profit_taken:true in the stop file must block a re-take"
    assert _orders == []


# ── Share arithmetic ──────────────────────────────────────────────────────────

def test_floor_of_half_odd_count():
    _reset(); _seed("HCA", 100.0)
    took = strategy._maybe_take_profit("HCA", 97, _sig(115.0, 66.0), "ACCT")
    assert took is True
    assert _orders == [("HCA", "sell", 48)], f"floor(97*0.5)=48, got {_orders}"  # keeps 49


def test_single_share_does_not_trip():
    _reset(); _seed("SPY", 100.0)
    took = strategy._maybe_take_profit("SPY", 1, _sig(150.0, 80.0), "ACCT")
    assert took is False, "floor(1*0.5)=0 -> nothing to sell, no order"
    assert _orders == []
    # did not latch: a position too small to halve is left free to trim later
    assert "profit_taken" not in strategy._load_stops()["SPY"]


# ── Backward compatibility ────────────────────────────────────────────────────

def test_missing_profit_taken_field_treated_as_false():
    _reset(); _seed("MSFT", 100.0, profit_taken=None)     # field omitted entirely
    assert "profit_taken" not in strategy._load_stops()["MSFT"]
    took = strategy._maybe_take_profit("MSFT", 100, _sig(113.0, 61.0), "ACCT")
    assert took is True, "a record with no profit_taken field must be takeable"
    assert _orders == [("MSFT", "sell", 50)]


# ── Guards ────────────────────────────────────────────────────────────────────

def test_no_stop_record_is_noop():
    _reset()                                              # no record seeded
    took = strategy._maybe_take_profit("NVDA", 100, _sig(999.0, 90.0), "ACCT")
    assert took is False, "no entry basis -> cannot size the gain -> no-op"
    assert _orders == []


def test_disabled_flag_is_noop():
    _reset(); _seed("DDOG", 100.0); config.ENABLE_PROFIT_TAKING = False
    took = strategy._maybe_take_profit("DDOG", 100, _sig(120.0, 80.0), "ACCT")
    assert took is False and _orders == []


def test_failed_order_does_not_latch():
    global _order_result
    _reset(); _seed("DDOG", 100.0)
    _order_result = None                                   # broker rejects
    took = strategy._maybe_take_profit("DDOG", 100, _sig(120.0, 70.0), "ACCT")
    assert took is False, "a failed order must not report success"
    assert "profit_taken" not in strategy._load_stops()["DDOG"], "must retry next cycle"
    assert strategy._profit_takes == 0


if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="ptake_test_")
    strategy._STOPS_PATH = os.path.join(_tmpdir, "stop_prices.json")
    _orig_place = strategy.tc.place_equity_order
    _orig_logtrade = strategy.log_trade
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
        strategy.tc.place_equity_order = _orig_place
        strategy.log_trade = _orig_logtrade
