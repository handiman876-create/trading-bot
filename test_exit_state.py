"""
Unit tests for STATE-based exits + the split buy/sell signal gate — NO network.

Regression cover for the defect found on 2026-07-15: exits keyed off the EDGE
(`bearish_cross`) were unreachable for any position whose crossover happened
inside a single live bar, because `prev` is yesterday's CLOSED bar. HCA and QQQ
both crossed up and back down intraday, never transitioned relative to
yesterday's close, and so could never exit on a cross again — stop-only, for as
long as they were held.

Run:  python3 test_exit_state.py
"""

import os
import tempfile

import _testlib
import strategy

# ── Test doubles ──────────────────────────────────────────────────────────────
_orders = []
_order_result = {"order": {"id": "T1"}}


def _fake_place(account_id, symbol, side, qty):
    _orders.append((symbol, side, qty))
    return _order_result


def _sides(side):
    return [o for o in _orders if o[1] == side]


def _reset():
    _orders.clear()
    strategy._stop_exits = 0
    strategy._state_only_exits = 0
    strategy._short_covers = 0
    strategy._short_entries = 0
    strategy._momentum_align_entries = 0
    strategy._entries_delayed = 0
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy._entry_delay_logged.clear()
    strategy.mh.entries_allowed = lambda *a, **k: True   # clock tested separately
    strategy.config.USE_MOMENTUM_ALIGNMENT = True
    strategy.config.ENABLE_SHORTING = True
    strategy.config.USE_TRAILING_STOP = False   # isolate signal logic from stops
    strategy.tc.place_equity_order = _fake_place
    strategy.tc.get_historical = lambda *a, **k: [{"bar": 1}]
    strategy.tc.get_quote = lambda s: {"last": 10_000.0}
    for path in (strategy._STOPS_PATH, strategy._MOM_ENTRIES_PATH):
        _testlib.safe_remove(path)


def _set_sig(**kw):
    """Default sig = BEARISH state (EMA9 < EMA21), RSI 50, NO edge — i.e. exactly
    the HCA/QQQ shape: the trend has rolled over but no cross fired this bar."""
    sig = {"close": 100.0, "ema_short": 95.0, "ema_long": 100.0, "rsi": 50.0,
           "bullish_cross": False, "bearish_cross": False, "atr": 4.0}
    sig.update(kw)
    strategy.ind.compute_indicators = lambda *a, **k: sig
    return sig


def _pos(symbol, qty):
    return [{"symbol": symbol, "quantity": qty, "cost_basis": 100.0 * abs(qty)}]


# ── The core fix: exit with no edge ───────────────────────────────────────────

def test_long_exits_on_bearish_state_without_edge():
    """THE HCA CASE. Bearish state, no bearish_cross this bar. Old code: stuck
    forever. New code: sells."""
    _reset(); _set_sig()
    strategy.evaluate_stock("HCA", "ACCT", _pos("HCA", 136), 100000.0)
    assert _sides("sell") == [("HCA", "sell", 136)], _orders
    assert strategy._state_only_exits == 1, "state-only exit counted"


def test_short_covers_on_bullish_state_without_edge():
    """Mirror of the above for a short — a missed bullish edge stranded it."""
    _reset(); _set_sig(ema_short=105.0, ema_long=100.0)
    strategy.evaluate_stock("META", "ACCT", _pos("META", -50), 100000.0)
    assert _sides("buy_to_cover") == [("META", "buy_to_cover", 50)], _orders
    assert strategy._state_only_exits == 1, "state-only cover counted"


def test_edge_exit_does_not_count_as_state_only():
    """When the edge IS present the old logic would also have caught it, so the
    counter must NOT tick — it measures only what the edge missed."""
    _reset(); _set_sig(bearish_cross=True)
    strategy.evaluate_stock("HCA", "ACCT", _pos("HCA", 136), 100000.0)
    assert _sides("sell") == [("HCA", "sell", 136)], _orders
    assert strategy._state_only_exits == 0, "edge-backed exit is not state-only"


# ── Split gate: an entry must never block its own exit ────────────────────────

def test_buy_then_sell_same_day_both_allowed():
    """THE QQQ CASE. Bought at 9:30, trend rolls over by 10:14 — under the single
    gate the sell was blocked all day. Now both fire."""
    _reset(); _set_sig(bullish_cross=True, ema_short=105.0, ema_long=100.0)
    strategy.evaluate_stock("QQQ", "ACCT", [], 100000.0)
    assert _sides("buy") == [("QQQ", "buy", 50)], "entry fires"
    assert strategy._already_bought_today("QQQ")

    _set_sig(ema_short=95.0, ema_long=100.0)      # same day, trend rolls over
    strategy.evaluate_stock("QQQ", "ACCT", _pos("QQQ", 50), 100000.0)
    assert _sides("sell") == [("QQQ", "sell", 50)], "exit NOT blocked by the buy"


def test_no_duplicate_sell_while_order_pending():
    """held stays non-zero until the sell fills, so the next 60s poll must not
    fire a second order. This is what the sell gate is actually for."""
    _reset(); _set_sig()
    strategy.evaluate_stock("HCA", "ACCT", _pos("HCA", 136), 100000.0)
    strategy.evaluate_stock("HCA", "ACCT", _pos("HCA", 136), 100000.0)   # still held
    assert _sides("sell") == [("HCA", "sell", 136)], f"exactly one sell, got {_orders}"


def test_no_reentry_after_exit_same_day():
    """After exiting, a later bullish blip must not re-buy the same name today."""
    _reset(); _set_sig()
    strategy.evaluate_stock("HCA", "ACCT", _pos("HCA", 136), 100000.0)
    assert _sides("sell"), "exited"
    _set_sig(bullish_cross=True, ema_short=105.0, ema_long=100.0)
    strategy.evaluate_stock("HCA", "ACCT", [], 100000.0)       # now flat
    assert _sides("buy") == [], "no same-day re-entry after an exit"


def test_momentum_name_does_not_reenter_after_state_exit_in_rotation():
    _reset(); _set_sig(ema_short=105.0, ema_long=100.0)
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _sides("buy") == [("DAL", "buy", 50)], "alignment entry fires"
    _set_sig(ema_short=95.0, ema_long=100.0)                  # rolls over
    strategy.evaluate_stock("DAL", "ACCT", _pos("DAL", 50), 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _sides("sell") == [("DAL", "sell", 50)], "state exit fires"
    _set_sig(ema_short=105.0, ema_long=100.0)                 # aligns again
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _sides("buy") == [("DAL", "buy", 50)], "latch + gate block re-entry"


# ── Entries stay EDGE-based ───────────────────────────────────────────────────

def test_short_entry_still_requires_edge():
    """SELLSHORT is an ENTRY. On state it would re-short on every poll."""
    _reset(); _set_sig()                       # bearish state, NO edge, flat
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0)
    assert _sides("sell_short") == [], "no short without a fresh death cross"


def test_short_entry_fires_on_edge():
    _reset(); _set_sig(bearish_cross=True)
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0)
    assert _sides("sell_short") == [("AAPL", "sell_short", 50)], _orders


def test_long_entry_still_requires_edge():
    _reset(); _set_sig(ema_short=105.0, ema_long=100.0)   # bullish state, no edge
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0, is_momentum=False)
    assert _sides("buy") == [], "core names still need a fresh cross to enter"


# ── RSI gate now defers rather than cancels ───────────────────────────────────

def test_oversold_defers_exit_then_fires_on_recovery():
    """RSI < oversold holds the exit. Under the edge that exit was lost forever;
    under state it fires as soon as RSI recovers, trend still bearish."""
    _reset(); _set_sig(rsi=25.0)
    strategy.evaluate_stock("HCA", "ACCT", _pos("HCA", 136), 100000.0)
    assert _sides("sell") == [], "deeply oversold — do not panic-sell the low"
    _set_sig(rsi=35.0)                        # recovered, still bearish state
    strategy.evaluate_stock("HCA", "ACCT", _pos("HCA", 136), 100000.0)
    assert _sides("sell") == [("HCA", "sell", 136)], "exit fires on recovery"


# ── Exits are not subject to the entry delay ──────────────────────────────────

def test_exit_fires_while_entries_are_delayed():
    """The 9:30-10:00 window must still close bad positions."""
    _reset(); _set_sig()
    strategy.mh.entries_allowed = lambda *a, **k: False
    strategy.evaluate_stock("HCA", "ACCT", _pos("HCA", 136), 100000.0)
    assert _sides("sell") == [("HCA", "sell", 136)], "exit ignores the entry delay"


def test_entry_blocked_while_entries_are_delayed():
    _reset(); _set_sig(bullish_cross=True, ema_short=105.0, ema_long=100.0)
    strategy.mh.entries_allowed = lambda *a, **k: False
    strategy.evaluate_stock("QQQ", "ACCT", [], 100000.0)
    assert _sides("buy") == [], "entry waits for the bar to form"
    assert strategy._entries_delayed == 1, "delayed entries counted"


def test_delayed_counter_counts_entries_not_polls():
    """The counter must measure would-be ENTRIES deferred, not quiet polls — a
    number that ticks 600x every morning regardless would say nothing about
    whether the delay is earning its keep."""
    _reset(); _set_sig(bullish_cross=True, ema_short=105.0, ema_long=100.0)
    strategy.mh.entries_allowed = lambda *a, **k: False
    for _ in range(30):                       # 30 polls across the window
        strategy.evaluate_stock("QQQ", "ACCT", [], 100000.0)
    assert strategy._entries_delayed == 1, \
        f"one deferred entry, not one per poll (got {strategy._entries_delayed})"


def test_delayed_counter_ignores_quiet_polls():
    """No entry signal + no position = nothing was deferred, so nothing counts."""
    _reset(); _set_sig(ema_short=95.0, ema_long=100.0)   # bearish, flat, no edge
    strategy.mh.entries_allowed = lambda *a, **k: False
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0)
    assert strategy._entries_delayed == 0, "quiet poll must not tick the counter"


def test_delayed_counter_ignores_held_positions():
    """A held name inside the window is not a deferred entry."""
    _reset(); _set_sig(ema_short=105.0, ema_long=100.0)  # bullish, held
    strategy.mh.entries_allowed = lambda *a, **k: False
    strategy.evaluate_stock("DDOG", "ACCT", _pos("DDOG", 195), 100000.0)
    assert strategy._entries_delayed == 0, "already-held name is not an entry"


def test_momentum_entry_also_blocked_while_delayed():
    """HCA was a momentum alignment entry on a 5-minute-old bar at RSI 35. A stub
    bar is a stub bar whether it is read as an edge or a state."""
    _reset(); _set_sig(ema_short=105.0, ema_long=100.0)
    strategy.mh.entries_allowed = lambda *a, **k: False
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _sides("buy") == [], "momentum path is gated too"
    assert not strategy._momentum_entry_taken("DAL", "G1"), \
        "latch NOT consumed — the shot survives to retry once the bar forms"


if __name__ == "__main__":
    # Point state files at a throwaway tmpdir BEFORE any test runs. conftest.py
    # does this under pytest; this block is the only thing standing between a
    # direct `python3 test_exit_state.py` and the live data/ files.
    _tmpdir = tempfile.mkdtemp(prefix="exit_state_test_")
    strategy._STOPS_PATH       = os.path.join(_tmpdir, "stop_prices.json")
    strategy._MOM_ENTRIES_PATH = os.path.join(_tmpdir, "momentum_entries.json")
    _orig = {
        "place": strategy.tc.place_equity_order,
        "hist":  strategy.tc.get_historical,
        "quote": strategy.tc.get_quote,
        "ci":    strategy.ind.compute_indicators,
        "log":   strategy.log_trade,
        "ea":    strategy.mh.entries_allowed,
        "trail": strategy.config.USE_TRAILING_STOP,
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
        strategy.tc.place_equity_order = _orig["place"]
        strategy.tc.get_historical    = _orig["hist"]
        strategy.tc.get_quote         = _orig["quote"]
        strategy.ind.compute_indicators = _orig["ci"]
        strategy.log_trade            = _orig["log"]
        strategy.mh.entries_allowed   = _orig["ea"]
        strategy.config.USE_TRAILING_STOP = _orig["trail"]
