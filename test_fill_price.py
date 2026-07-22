"""
Unit tests for actual-fill-price stop arming — NO network.

Stubs tradestation_client._get / .time.sleep and strategy.tc.get_order so we can
exercise get_order() polling, _resolve_fill() slippage math (long AND short),
fill-vs-signal stop arming, the enriched trade log, and ledger backward
compatibility — without hitting the API or touching live state.

Run:  python3 test_fill_price.py
"""

import json
import os
import tempfile

import _testlib
import config
import tradestation_client as tc
import strategy
import trade_logger
import performance_analyzer as pa


# ── Fixtures / builders ───────────────────────────────────────────────────────

def _order_resp(status_desc, filled_price=None, exec_price=None):
    """Shape a TradeStation GetOrders response. Numerics are STRINGS, as the API
    returns them. `exec_price` populates Legs[0].ExecutionPrice (the FilledPrice
    fallback); omit FilledPrice to exercise that path."""
    o = {"OrderID": "T1", "StatusDescription": status_desc}
    if filled_price is not None:
        o["FilledPrice"] = str(filled_price)
    o["Legs"] = [{"ExecutionPrice": str(exec_price)}] if exec_price is not None else []
    return {"Orders": [o]}


# ── get_order() ───────────────────────────────────────────────────────────────

def test_get_order_immediate_fill():
    orig = tc._get
    tc._get = lambda path, params=None: _order_resp("Filled", filled_price=512.18)
    try:
        assert tc.get_order("ACCT", "T1") == 512.18
    finally:
        tc._get = orig


def test_get_order_leg_price_fallback():
    """No top-level FilledPrice -> fall back to the leg ExecutionPrice."""
    orig = tc._get
    tc._get = lambda path, params=None: _order_resp("Filled", exec_price=100.25)
    try:
        assert tc.get_order("ACCT", "T1") == 100.25
    finally:
        tc._get = orig


def test_get_order_pending_then_filled_retries_once():
    seq = [_order_resp("Received"), _order_resp("Filled", filled_price=200.0)]
    calls = {"n": 0}

    def fake_get(path, params=None):
        r = seq[calls["n"]] if calls["n"] < len(seq) else seq[-1]
        calls["n"] += 1
        return r

    orig_get, orig_sleep = tc._get, tc.time.sleep
    tc._get, tc.time.sleep = fake_get, lambda s: None
    try:
        assert tc.get_order("ACCT", "T1") == 200.0
        assert calls["n"] == 2, "should poll exactly twice (one retry)"
    finally:
        tc._get, tc.time.sleep = orig_get, orig_sleep


def test_get_order_still_pending_after_retry_returns_none():
    calls = {"n": 0}

    def fake_get(path, params=None):
        calls["n"] += 1
        return _order_resp("Received")

    orig_get, orig_sleep = tc._get, tc.time.sleep
    tc._get, tc.time.sleep = fake_get, lambda s: None
    try:
        assert tc.get_order("ACCT", "T1") is None
        assert calls["n"] == 2, "one retry then give up, no more"
    finally:
        tc._get, tc.time.sleep = orig_get, orig_sleep


def test_get_order_api_error_returns_none():
    orig = tc._get

    def boom(path, params=None):
        raise RuntimeError("HTTP 500")

    tc._get = boom
    try:
        assert tc.get_order("ACCT", "T1") is None
    finally:
        tc._get = orig


# ── _resolve_fill() slippage math ─────────────────────────────────────────────

def test_resolve_fill_long_positive_slippage_when_paid_more():
    orig = strategy.tc.get_order
    strategy.tc.get_order = lambda acct, oid: 100.50
    try:
        entry, fill, slip = strategy._resolve_fill("X", "ACCT", "OID", 100.0, "long")
        assert entry == 100.50 and fill == 100.50
        assert abs(slip - 0.50) < 1e-9, slip          # paid 0.50 more = +0.50 (worse)
    finally:
        strategy.tc.get_order = orig


def test_resolve_fill_short_positive_slippage_when_sold_cheaper():
    """A short that sold BELOW signal got a worse fill -> positive slippage."""
    orig = strategy.tc.get_order
    strategy.tc.get_order = lambda acct, oid: 99.50
    try:
        _, _, slip = strategy._resolve_fill("X", "ACCT", "OID", 100.0, "short")
        assert abs(slip - 0.50) < 1e-9, slip          # signal 100 - fill 99.50 = +0.50
    finally:
        strategy.tc.get_order = orig


def test_resolve_fill_short_negative_slippage_when_sold_higher():
    """AMD-style: short filled ABOVE signal is a BETTER fill -> negative slippage."""
    orig = strategy.tc.get_order
    strategy.tc.get_order = lambda acct, oid: 512.18
    try:
        _, fill, slip = strategy._resolve_fill("AMD", "ACCT", "OID", 512.15, "short")
        assert fill == 512.18
        assert abs(slip - (-0.03)) < 1e-9, slip        # 512.15 - 512.18 = -0.03 (better)
    finally:
        strategy.tc.get_order = orig


def test_resolve_fill_unavailable_falls_back_to_signal():
    orig = strategy.tc.get_order
    strategy.tc.get_order = lambda acct, oid: None
    try:
        entry, fill, slip = strategy._resolve_fill("X", "ACCT", "OID", 100.0, "long")
        assert entry == 100.0, "falls back to signal price"
        assert fill is None and slip is None
    finally:
        strategy.tc.get_order = orig


# ── Stop armed at fill, not signal ────────────────────────────────────────────

def test_arm_stop_uses_fill_not_signal():
    _testlib.safe_remove(strategy._STOPS_PATH)
    # short AMD: fill 512.18 (not signal 512.15), atr 36.91.
    # Width is 1.5x, not risk_on's plain 2.5x: 36.91/512.18 = 7.21% puts AMD in the
    # HIGH volatility band. This case pins WHICH PRICE the stop is armed off (the
    # fill), so it just carries whatever width the band rules produce.
    strategy._arm_stop_on_entry("AMD", 512.18, 36.91, direction="short",
                                regime="risk_on", signal_price=512.15,
                                fill_price=512.18, slippage=-0.03)
    rec = strategy._load_stops()["AMD"]
    assert rec["entry_price"] == 512.18, rec                       # the FILL
    assert rec["atr_mult"] == 1.5, rec                             # high band
    assert abs(rec["stop_price"] - (512.18 + 1.5 * 36.91)) < 1e-6, rec


def test_arm_stop_fallback_uses_signal_when_no_fill():
    _testlib.safe_remove(strategy._STOPS_PATH)
    strategy._arm_stop_on_entry("XYZ", 100.0, 4.0, direction="long",
                                regime="risk_on", signal_price=100.0,
                                fill_price=None, slippage=None)
    rec = strategy._load_stops()["XYZ"]
    assert rec["entry_price"] == 100.0, rec
    assert abs(rec["stop_price"] - (100.0 - 2.5 * 4.0)) < 1e-6, rec


# ── Enriched trade log + backward compat ──────────────────────────────────────

def _last_trade_record():
    with open(config.TRADE_LOG_FILE) as f:
        lines = [ln for ln in f if ln.strip()]
    return json.loads(lines[-1])


def test_log_trade_records_fill_fields():
    _testlib.safe_remove(config.TRADE_LOG_FILE)
    trade_logger.log_trade("SELL_SHORT", "AMD", 96, 512.15, "market", "OID",
                           "EMA cross down", fill_price=512.18,
                           signal_price=512.15, slippage=-0.03)
    rec = _last_trade_record()
    assert rec["price"] == 512.15, "price stays the SIGNAL close for ledger compat"
    assert rec["fill_price"] == 512.18
    assert rec["signal_price"] == 512.15
    assert rec["slippage"] == -0.03


def test_log_trade_exit_has_null_fill_fields():
    """Exits pass no fill data: keys present but null (uniform new schema)."""
    _testlib.safe_remove(config.TRADE_LOG_FILE)
    trade_logger.log_trade("SELL", "DAL", 573, 84.67, "market", "OID", "EMA bearish")
    rec = _last_trade_record()
    assert rec["price"] == 84.67
    assert rec["fill_price"] is None
    assert rec["signal_price"] is None
    assert rec["slippage"] is None


# ── Ledger normalize: backward compat + new fields ────────────────────────────

def test_normalize_old_record_without_fill_keys():
    raw = {"timestamp": "2026-07-10 09:30:00 EDT", "action": "BUY", "symbol": "AAA",
           "quantity": 10, "price": 100.0, "order_type": "market",
           "order_id": "X", "notes": "EMA cross up"}
    ev = pa._normalize(raw)
    assert ev is not None
    assert ev["fill_price"] is None
    assert ev["signal_price"] is None
    assert ev["slippage"] is None


def test_normalize_new_record_carries_fill_fields():
    raw = {"timestamp": "2026-07-20 14:00:16 EDT", "action": "SELL_SHORT",
           "symbol": "AMD", "quantity": 96, "price": 512.15, "order_type": "market",
           "order_id": "962545929", "notes": "EMA cross down (short), RSI=49.2",
           "signal_price": 512.15, "fill_price": 512.18, "slippage": -0.03}
    ev = pa._normalize(raw)
    assert ev is not None
    assert ev["fill_price"] == 512.18
    assert ev["signal_price"] == 512.15
    assert ev["slippage"] == -0.03


if __name__ == "__main__":
    _tmp = tempfile.mkdtemp(prefix="fill_test_")
    strategy._STOPS_PATH = os.path.join(_tmp, "stop_prices.json")
    config.TRADE_LOG_FILE = os.path.join(_tmp, "trades.log")
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"All {passed} assertions passed.")
