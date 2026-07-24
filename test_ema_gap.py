"""
Unit tests for EMA cross hysteresis (config.EMA_CROSS_MIN_GAP_PCT) — NO network.

Regression cover for the defect found on 2026-07-22: CAH sold a 215-share
position (-$1,370) because EMA9 sat $0.01 below EMA21 at a price of $228 — a
0.004% separation, one poll after the two were exactly equal. A gap that small
is a rounding artefact, not a trend change.

The rule: a cross only counts when abs(ema_short - ema_long) / price >= 0.1%.
Applied symmetrically to all four equity signals plus the momentum-alignment
entry, which shares the same primitives.

Run:  python3 test_ema_gap.py
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
    strategy._cross_gap_blocks = 0
    strategy._stop_exits = 0
    strategy._state_only_exits = 0
    strategy._short_covers = 0
    strategy._short_entries = 0
    strategy._momentum_align_entries = 0
    strategy._entries_delayed = 0
    strategy._cross_gap_logged.clear()
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy._entry_delay_logged.clear()
    strategy.mh.entries_allowed = lambda *a, **k: True
    strategy.config.USE_MOMENTUM_ALIGNMENT = True
    strategy.config.ENABLE_SHORTING = True
    strategy.config.USE_TRAILING_STOP = False   # isolate signal logic from stops
    # CROSS_SUSTAIN_MINUTES=0 isolates these cases from cross persistence:
    # they assert on gap/edge/latch behaviour, not on how long a cross has
    # held, and would otherwise all need a 30-minute clock advance.
    strategy.config.CROSS_SUSTAIN_MINUTES = 0
    strategy.tc.place_equity_order = _fake_place
    strategy.tc.get_historical = lambda *a, **k: [{"bar": 1}]
    strategy.tc.get_quote = lambda s: {"last": 10_000.0}
    for path in (strategy._STOPS_PATH, strategy._MOM_ENTRIES_PATH):
        _testlib.safe_remove(path)


# Under `python3 test_ema_gap.py` the __main__ block below restores the stubs
# this module installs. Under pytest that block never runs, so without this
# fixture the stubs leak into every module that sorts after "ema_gap" —
# test_entry_delay.py inherits a compute_indicators pinned to a thin-gap sig and
# fails 10 assertions that pass in isolation. conftest.py isolates FILES; this
# isolates the monkeypatched module globals.
try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_strategy_globals():
        saved = {
            "place": strategy.tc.place_equity_order,
            "hist":  strategy.tc.get_historical,
            "quote": strategy.tc.get_quote,
            "ci":    strategy.ind.compute_indicators,
            "log":   strategy.log_trade,
            "ea":    strategy.mh.entries_allowed,
            "trail": strategy.config.USE_TRAILING_STOP,
            "align": strategy.config.USE_MOMENTUM_ALIGNMENT,
            "short": strategy.config.ENABLE_SHORTING,
            "sus":   strategy.config.CROSS_SUSTAIN_MINUTES,
        }
        strategy.log_trade = lambda *a, **k: None
        yield
        strategy.tc.place_equity_order  = saved["place"]
        strategy.tc.get_historical      = saved["hist"]
        strategy.tc.get_quote           = saved["quote"]
        strategy.ind.compute_indicators = saved["ci"]
        strategy.log_trade              = saved["log"]
        strategy.mh.entries_allowed     = saved["ea"]
        strategy.config.CROSS_SUSTAIN_MINUTES = saved["sus"]
        strategy._cross_first_seen.clear()
        strategy._cross_confirmed.clear()
        strategy.config.USE_TRAILING_STOP     = saved["trail"]
        strategy.config.USE_MOMENTUM_ALIGNMENT = saved["align"]
        strategy.config.ENABLE_SHORTING        = saved["short"]
        strategy._cross_gap_logged.clear()
except ImportError:      # direct `python3 test_ema_gap.py` run
    pass


def _set_sig(ema_short, ema_long, price=100.0, rsi=50.0, **kw):
    sig = {"close": price, "ema_short": ema_short, "ema_long": ema_long,
           "rsi": rsi, "bullish_cross": False, "bearish_cross": False,
           "atr": 4.0}
    sig.update(kw)
    strategy.ind.compute_indicators = lambda *a, **k: sig
    return sig


def _pos(symbol, qty):
    return [{"symbol": symbol, "quantity": qty, "cost_basis": 100.0 * abs(qty)}]


# ── The pure helper ───────────────────────────────────────────────────────────

def test_cah_one_cent_gap_is_not_a_cross():
    """The actual CAH numbers: EMA9 228.45 vs EMA21 228.46 at price 224.72."""
    assert strategy._valid_ema_cross(228.45, 228.46, 224.72) is False


def test_threshold_boundary():
    """>= is inclusive, so exactly 0.1% passes. Probed a hair either side rather
    than exactly on the boundary — an exact-boundary float comparison is a coin
    flip, not a specification."""
    assert strategy._valid_ema_cross(100.0, 100.0999, 100.0) is False   # 0.0999%
    assert strategy._valid_ema_cross(100.0, 100.1001, 100.0) is True    # 0.1001%
    assert strategy._valid_ema_cross(100.0, 100.11, 100.0) is True      # 0.11%


def test_symmetric_in_both_orientations():
    """Same magnitude, opposite sign — the rule must not favour a direction."""
    assert strategy._valid_ema_cross(100.11, 100.0, 100.0) is True
    assert strategy._valid_ema_cross(100.0, 100.11, 100.0) is True
    assert strategy._valid_ema_cross(100.01, 100.0, 100.0) is False
    assert strategy._valid_ema_cross(100.0, 100.01, 100.0) is False


def test_unusable_price_is_not_a_cross():
    """_live_price can return None; a gap cannot be normalised without a
    denominator, so we decline to call it a cross rather than divide by zero."""
    assert strategy._valid_ema_cross(95.0, 100.0, 0) is False
    assert strategy._valid_ema_cross(95.0, 100.0, None) is False


# ── Signal 1/4: long exit (bearish state) ─────────────────────────────────────

def test_long_exit_blocked_by_thin_gap():
    _reset(); _set_sig(228.45, 228.46, price=224.72)          # CAH, 0.004%
    strategy.evaluate_stock("CAH", "ACCT", _pos("CAH", 215), 100000.0)
    assert _orders == [], f"thin bearish gap must not sell, got {_orders}"
    assert strategy._cross_gap_blocks == 1


def test_long_exit_fires_on_wide_gap():
    _reset(); _set_sig(220.0, 228.46, price=224.72)           # 3.8% rollover
    strategy.evaluate_stock("CAH", "ACCT", _pos("CAH", 215), 100000.0)
    assert _sides("sell") == [("CAH", "sell", 215)], f"got {_orders}"
    assert strategy._cross_gap_blocks == 0


# ── Signal 2/4: short cover (bullish state) ───────────────────────────────────

def test_short_cover_blocked_by_thin_gap():
    _reset(); _set_sig(192.04, 191.98, price=176.99)          # DHR cover, 0.011%
    strategy.evaluate_stock("DHR", "ACCT", _pos("DHR", -276), 100000.0)
    assert _orders == [], f"thin bullish gap must not cover, got {_orders}"
    assert strategy._cross_gap_blocks == 1


def test_short_cover_fires_on_wide_gap():
    _reset(); _set_sig(196.0, 191.98, price=176.99)           # 2.3%
    strategy.evaluate_stock("DHR", "ACCT", _pos("DHR", -276), 100000.0)
    assert _sides("buy_to_cover") == [("DHR", "buy_to_cover", 276)], f"got {_orders}"


# ── Signal 3/4: long entry (bullish cross edge) ───────────────────────────────

def test_long_entry_blocked_by_thin_gap():
    """QQQ's 9:30:05 stub-bar cross: a real bullish EDGE, 0.017% wide."""
    _reset(); _set_sig(724.18, 724.06, price=724.06, bullish_cross=True)
    strategy.evaluate_stock("QQQ", "ACCT", [], 100000.0)
    assert _orders == [], f"thin bullish cross must not buy, got {_orders}"
    assert strategy._cross_gap_blocks == 1


def test_long_entry_fires_on_wide_gap():
    _reset(); _set_sig(730.0, 724.06, price=724.06, bullish_cross=True)
    strategy.evaluate_stock("QQQ", "ACCT", [], 100000.0)
    assert _sides("buy"), f"wide cross must buy, got {_orders}"


# ── Signal 4/4: short entry (bearish cross edge) ──────────────────────────────

def test_short_entry_blocked_by_thin_gap():
    _reset(); _set_sig(191.98, 192.04, price=176.99, bearish_cross=True)
    strategy.evaluate_stock("DHR", "ACCT", [], 100000.0)
    assert _orders == [], f"thin death cross must not short, got {_orders}"
    assert strategy._cross_gap_blocks == 1


def test_short_entry_fires_on_wide_gap():
    _reset(); _set_sig(188.0, 192.04, price=176.99, bearish_cross=True)
    strategy.evaluate_stock("DHR", "ACCT", [], 100000.0)
    assert _sides("sell_short"), f"wide death cross must short, got {_orders}"


# ── 5th site: momentum alignment (was a bare inline EMA comparison) ───────────

def test_momentum_alignment_blocked_by_thin_gap():
    _reset(); _set_sig(100.05, 100.0, price=100.0, rsi=55.0)   # 0.05%, aligned
    strategy.evaluate_stock("HOOD", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="g1")
    assert _orders == [], f"thin alignment must not enter, got {_orders}"


def test_momentum_alignment_fires_on_wide_gap():
    _reset(); _set_sig(103.0, 100.0, price=100.0, rsi=55.0)    # 3%
    strategy.evaluate_stock("HOOD", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="g1")
    assert _sides("buy"), f"wide alignment must enter, got {_orders}"
    assert strategy._momentum_align_entries == 1


# ── Deferral: a suppressed exit is not a lost exit ────────────────────────────

def test_suppressed_exit_fires_once_gap_widens():
    """The property that makes symmetric blocking safe on exits. Exits are STATE,
    re-derived every poll, so nothing is latched and nothing is lost: the same
    position that could not exit at 0.004% exits as soon as the trend separates.
    Without this a thin-gap position would be stranded on its stop — the exact
    HCA/QQQ failure this module's exits were rewritten to prevent."""
    _reset()
    _set_sig(228.45, 228.46, price=224.72)
    strategy.evaluate_stock("CAH", "ACCT", _pos("CAH", 215), 100000.0)
    assert _orders == [], "poll 1: gap too thin, exit deferred"

    _set_sig(220.0, 228.46, price=224.72)        # trend separates
    strategy.evaluate_stock("CAH", "ACCT", _pos("CAH", 215), 100000.0)
    assert _sides("sell") == [("CAH", "sell", 215)], \
        f"poll 2: gap widened, deferred exit must fire — got {_orders}"


# ── Counter ───────────────────────────────────────────────────────────────────

def test_gap_block_counter_latches_per_symbol_per_day():
    """A name can sit in the deadband all session (4.2% of polls in the 8-session
    replay). The counter must measure suppressed symbol-days, not polls."""
    _reset(); _set_sig(228.45, 228.46, price=224.72)
    for _ in range(10):
        strategy.evaluate_stock("CAH", "ACCT", _pos("CAH", 215), 100000.0)
    assert strategy._cross_gap_blocks == 1, \
        f"10 polls, 1 symbol-day = 1 block, got {strategy._cross_gap_blocks}"

    _set_sig(529.6, 529.83, price=529.83)        # LII, 0.042%
    strategy.evaluate_stock("LII", "ACCT", _pos("LII", 89), 100000.0)
    assert strategy._cross_gap_blocks == 2, "a second symbol counts separately"


if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="ema_gap_test_")
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
