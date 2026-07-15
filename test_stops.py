"""
Unit tests for the bot-managed ATR trailing stop — NO network.

Monkeypatches strategy._STOPS_PATH to a throwaway temp file and stubs the
TradeStation client (tc.get_quote / tc.place_equity_order) and log_trade, so we
can exercise bootstrap / ratchet / exit / reconcile without hitting the API,
placing orders, or touching the live data/stop_prices.json.

Run:  python3 test_stops.py
"""

import os
import tempfile

import strategy

# ── Test doubles ──────────────────────────────────────────────────────────────
_orders = []          # (symbol, side, qty) captured from place_equity_order
_order_result = {"order": {"id": "T1"}}   # flip to None to simulate a failed order


def _fake_place(account_id, symbol, side, qty):
    _orders.append((symbol, side, qty))
    return _order_result


def _fake_quote(price):
    return lambda symbol: ({"last": price} if price is not None else None)


def _reset(quote_price=None):
    """Fresh empty stop file + cleared captured state before each test."""
    _orders.clear()
    strategy._stop_exits = 0
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy.tc.place_equity_order = _fake_place
    strategy.tc.get_quote = _fake_quote(quote_price)
    if os.path.exists(strategy._STOPS_PATH):
        os.remove(strategy._STOPS_PATH)


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def test_bootstrap_nvda_arms_stop_no_immediate_exit():
    """Real NVDA numbers: entry≈209.49, ATR 7.22, current ~203.4 -> stop 191.44,
    which is below price, so NO exit on the first cycle."""
    _reset(quote_price=203.40)
    positions = [{"symbol": "NVDA", "quantity": 238, "cost_basis": 49858.62}]
    sig = {"close": 203.53, "atr": 7.22}
    exited = strategy._check_and_trail_stop("NVDA", 238, sig, "ACCT", positions)
    assert exited is False, "NVDA should NOT stop out on bootstrap"
    assert _orders == [], f"no order expected, got {_orders}"

    rec = strategy._load_stops()["NVDA"]
    assert abs(rec["entry_price"] - 209.49) < 0.01, rec
    assert abs(rec["atr_at_entry"] - 7.22) < 0.001, rec
    assert abs(rec["high_water"] - 209.49) < 0.01, rec          # max(entry, current)
    assert abs(rec["stop_price"] - 191.44) < 0.01, rec          # 209.49 - 2.5*7.22
    assert rec["bootstrapped"] is True, rec


def test_bootstrap_atr_none_arms_nothing():
    _reset(quote_price=100.0)
    positions = [{"symbol": "XYZ", "quantity": 10, "cost_basis": 1000.0}]
    sig = {"close": 100.0, "atr": None}
    exited = strategy._check_and_trail_stop("XYZ", 10, sig, "ACCT", positions)
    assert exited is False
    assert "XYZ" not in strategy._load_stops(), "no record without ATR"


# ── Ratchet ───────────────────────────────────────────────────────────────────

def test_stop_ratchets_up_only():
    """Stop rises with a new high-water mark and never falls back."""
    _reset(quote_price=110.0)      # entry 100, atr 4 -> stop 90; price 110
    strategy._save_stops({"AAA": {
        "entry_price": 100.0, "atr_at_entry": 4.0, "high_water": 100.0,
        "stop_price": 90.0, "opened": "2026-07-13", "bootstrapped": False}})
    sig = {"close": 110.0, "atr": 4.0}
    strategy._check_and_trail_stop("AAA", 10, sig, "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["high_water"] - 110.0) < 1e-6, rec
    assert abs(rec["stop_price"] - 100.0) < 1e-6, rec          # 110 - 2.5*4

    # Price pulls back to 104: stop must NOT drop below the ratcheted 100.
    strategy.tc.get_quote = _fake_quote(104.0)
    strategy._check_and_trail_stop("AAA", 10, {"close": 104.0, "atr": 4.0}, "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["high_water"] - 110.0) < 1e-6, "high_water should not fall"
    assert abs(rec["stop_price"] - 100.0) < 1e-6, "stop should not fall"
    assert _orders == [], "no exit — price 104 still above stop 100"


# ── Exit ──────────────────────────────────────────────────────────────────────

def test_exit_when_price_breaches_stop():
    _reset(quote_price=205.0)       # price 205 <= stop 210 -> exit
    strategy._save_stops({"NVDA": {
        "entry_price": 209.0, "atr_at_entry": 7.22, "high_water": 215.0,
        "stop_price": 210.0, "opened": "2026-07-13", "bootstrapped": True}})
    exited = strategy._check_and_trail_stop(
        "NVDA", 238, {"close": 205.0, "atr": 7.22}, "ACCT", [])
    assert exited is True, "should exit when price <= stop"
    assert _orders == [("NVDA", "sell", 238)], _orders
    assert "NVDA" not in strategy._load_stops(), "record cleared after exit"
    assert strategy._stop_exits == 1, "counter incremented"
    # A stop-out marks BOTH gates: the buy mark is the one that blocks the
    # same-day re-entry this has always been about; the sell mark preserves the
    # old single gate's "no further signals today for this name" behaviour.
    assert strategy._already_bought_today("NVDA"), "same-day re-buy blocked"
    assert strategy._already_sold_today("NVDA"), "same-day re-sell blocked"


def test_failed_exit_order_keeps_record():
    global _order_result
    _reset(quote_price=205.0)
    _order_result = None            # simulate order rejection
    try:
        strategy._save_stops({"NVDA": {
            "entry_price": 209.0, "atr_at_entry": 7.22, "high_water": 215.0,
            "stop_price": 210.0, "opened": "2026-07-13", "bootstrapped": True}})
        exited = strategy._check_and_trail_stop(
            "NVDA", 238, {"close": 205.0, "atr": 7.22}, "ACCT", [])
        assert exited is False, "failed order -> not treated as exited"
        assert "NVDA" in strategy._load_stops(), "record kept to retry next cycle"
        assert strategy._stop_exits == 0, "counter not incremented on failure"
    finally:
        _order_result = {"order": {"id": "T1"}}


# ── Short direction ───────────────────────────────────────────────────────────

def test_short_arms_above_and_ratchets_down_only():
    """A short's stop sits ABOVE entry and only ever falls with a new low-water
    mark — it must never rise back when price bounces."""
    _reset(quote_price=90.0)          # entry 100, atr 4 -> stop 110; price drops to 90
    strategy._arm_stop_on_entry("AAA", 100.0, 4.0, direction="short")
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 110.0) < 1e-6, rec         # 100 + 2.5*4, ABOVE entry
    assert rec["direction"] == "short", rec

    strategy._check_and_trail_stop("AAA", -10, {"close": 90.0, "atr": 4.0}, "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["low_water"] - 90.0) < 1e-6, rec
    assert abs(rec["stop_price"] - 100.0) < 1e-6, rec         # 90 + 2.5*4, ratcheted DOWN
    assert _orders == [], "price 90 below stop 100 -> no cover"

    # Price bounces to 96 (still below stop 100): stop must NOT rise back up.
    strategy.tc.get_quote = _fake_quote(96.0)
    strategy._check_and_trail_stop("AAA", -10, {"close": 96.0, "atr": 4.0}, "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["low_water"] - 90.0) < 1e-6, "low_water should not rise"
    assert abs(rec["stop_price"] - 100.0) < 1e-6, "short stop should not rise"
    assert _orders == [], "96 still below stop 100 -> no cover"


def test_short_covers_when_price_rises_into_stop():
    _reset(quote_price=112.0)          # price 112 >= stop 110 -> cover
    strategy._save_stops({"AAA": {
        "entry_price": 100.0, "atr_at_entry": 4.0, "low_water": 100.0,
        "stop_price": 110.0, "opened": "2026-07-14", "bootstrapped": False,
        "direction": "short"}})
    exited = strategy._check_and_trail_stop(
        "AAA", -10, {"close": 112.0, "atr": 4.0}, "ACCT", [])
    assert exited is True, "should cover when price >= stop"
    assert _orders == [("AAA", "buy_to_cover", 10)], _orders
    assert "AAA" not in strategy._load_stops(), "record cleared after cover"
    assert strategy._stop_exits == 1, "counter incremented"


def test_short_bootstrap_from_negative_held():
    """Adopting a pre-existing short (no record) infers direction from held<0 and
    seeds the stop ABOVE entry."""
    _reset(quote_price=95.0)
    positions = [{"symbol": "AAA", "quantity": -10, "cost_basis": 1000.0}]  # entry 100
    exited = strategy._check_and_trail_stop(
        "AAA", -10, {"close": 95.0, "atr": 4.0}, "ACCT", positions)
    assert exited is False, "95 below the ABOVE stop -> no cover on bootstrap"
    rec = strategy._load_stops()["AAA"]
    assert rec["direction"] == "short", rec
    assert abs(rec["entry_price"] - 100.0) < 1e-6, rec        # 1000/|−10|
    assert abs(rec["low_water"] - 95.0) < 1e-6, rec           # min(entry, price)
    assert abs(rec["stop_price"] - 105.0) < 1e-6, rec         # 95 + 2.5*4
    assert rec["bootstrapped"] is True, rec


# ── Live-quote fallback ───────────────────────────────────────────────────────

def test_daily_close_fallback_when_quote_fails():
    _reset(quote_price=None)        # get_quote returns None -> use sig['close']
    positions = [{"symbol": "MMM", "quantity": 5, "cost_basis": 500.0}]
    strategy._check_and_trail_stop("MMM", 5, {"close": 100.0, "atr": 4.0}, "ACCT", positions)
    rec = strategy._load_stops()["MMM"]
    # entry = 500/5 = 100; high_water = max(100, 100) = 100; stop = 100 - 10 = 90
    assert abs(rec["stop_price"] - 90.0) < 1e-6, rec


# ── Arm on entry / clear on exit ──────────────────────────────────────────────

def test_arm_stop_on_entry():
    _reset()
    strategy._arm_stop_on_entry("AAPL", 210.0, 4.0)
    rec = strategy._load_stops()["AAPL"]
    assert abs(rec["stop_price"] - 200.0) < 1e-6, rec           # 210 - 2.5*4
    assert abs(rec["high_water"] - 210.0) < 1e-6, rec
    assert rec["bootstrapped"] is False, rec


def test_arm_stop_on_entry_atr_none_is_noop():
    _reset()
    strategy._arm_stop_on_entry("AAPL", 210.0, None)
    assert strategy._load_stops() == {}, "no record armed without ATR"


def test_clear_stop():
    _reset()
    strategy._save_stops({"AAPL": {"entry_price": 1, "atr_at_entry": 1,
                                   "high_water": 1, "stop_price": 1,
                                   "opened": "d", "bootstrapped": False}})
    strategy._clear_stop("AAPL")
    assert "AAPL" not in strategy._load_stops()
    strategy._clear_stop("AAPL")     # idempotent — no crash on missing key


# ── Reconcile ─────────────────────────────────────────────────────────────────

def test_reconcile_prunes_unheld():
    _reset()
    strategy._save_stops({
        "NVDA": {"stop_price": 1, "entry_price": 1, "atr_at_entry": 1,
                 "high_water": 1, "opened": "d", "bootstrapped": True},
        "QQQ":  {"stop_price": 1, "entry_price": 1, "atr_at_entry": 1,
                 "high_water": 1, "opened": "d", "bootstrapped": True}})
    positions = [{"symbol": "NVDA", "quantity": 238, "cost_basis": 1},
                 {"symbol": "QQQ",  "quantity": 0,   "cost_basis": 0}]   # QQQ flat
    strategy.reconcile_stops(positions)
    stops = strategy._load_stops()
    assert "NVDA" in stops, "held position kept"
    assert "QQQ" not in stops, "flat position pruned"


def test_reconcile_empty_positions_guard():
    """An API blip returns [] — reconcile must NOT wipe live stops."""
    _reset()
    strategy._save_stops({"NVDA": {"stop_price": 191.44, "entry_price": 209.49,
                                   "atr_at_entry": 7.22, "high_water": 209.49,
                                   "opened": "d", "bootstrapped": True}})
    strategy.reconcile_stops([])
    assert "NVDA" in strategy._load_stops(), "empty positions must not prune"


# ── Persistence ───────────────────────────────────────────────────────────────

def test_save_load_roundtrip():
    _reset()
    rec = {"X": {"entry_price": 1.0, "atr_at_entry": 2.0, "high_water": 3.0,
                 "stop_price": 4.0, "opened": "2026-07-13", "bootstrapped": False}}
    strategy._save_stops(rec)
    assert strategy._load_stops() == rec


def test_load_corrupt_file_degrades_to_empty():
    _reset()
    with open(strategy._STOPS_PATH, "w") as f:
        f.write("{not valid json")
    assert strategy._load_stops() == {}, "corrupt file -> empty, no crash"


if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="stops_test_")
    strategy._STOPS_PATH = os.path.join(_tmpdir, "stop_prices.json")
    _orig_place = strategy.tc.place_equity_order
    _orig_quote = strategy.tc.get_quote
    _orig_logtrade = strategy.log_trade
    strategy.log_trade = lambda *a, **k: None      # silence trade-log file writes
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
        strategy.tc.get_quote = _orig_quote
        strategy.log_trade = _orig_logtrade
