"""
Unit tests for cross persistence (config.CROSS_SUSTAIN_MINUTES) — NO network.

Backs the rule added 2026-07-24: a gap-valid ENTRY cross must still be a cross
CROSS_SUSTAIN_MINUTES after it first appears. EMA_CROSS_MIN_GAP_PCT filters a
cross by MAGNITUDE; this filters one by PERSISTENCE, and the ledger says
persistence is where the money went — the 8 round-trips held under 30 hours lost
-$10,953.34 with zero winners. AVGO on 2026-07-23 is the archetype: a clean
0.10%-clearing cross at 10:00, reversed and stopped out 88 minutes later.

The rule, and the things it must NOT do:
  * ENTRY crosses are deferred until the cross has held N minutes.
  * EXITS are never gated. An exit-side version of this idea backtested NEGATIVE
    (age gate -$3,765.53, losing-position gate -$6,928.56) because delaying an
    exit on a bad position books a bigger loss. If a change makes an exit test
    here fail, the change is wrong, not the test.
  * A cross that lapses restarts the clock from zero — no credit carried over
    from an earlier, unrelated cross.

Run:  python3 test_cross_sustain.py
"""

import os
import tempfile

import _testlib
import strategy

_orders = []


def _fake_place(account_id, symbol, side, qty):
    _orders.append((symbol, side, qty))
    return {"order": {"id": "T1"}}


def _sides(side):
    return [o for o in _orders if o[1] == side]


def _reset(sustain=30):
    _orders.clear()
    strategy._cross_gap_blocks = 0
    strategy._cross_sustain_blocks = 0
    strategy._stop_exits = 0
    strategy._state_only_exits = 0
    strategy._short_covers = 0
    strategy._short_entries = 0
    strategy._momentum_align_entries = 0
    strategy._entries_delayed = 0
    strategy._cross_gap_logged.clear()
    strategy._cross_first_seen.clear()
    strategy._cross_confirmed.clear()
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy._entry_delay_logged.clear()
    strategy.mh.entries_allowed = lambda *a, **k: True
    strategy.config.CROSS_SUSTAIN_MINUTES = sustain
    strategy.config.ENABLE_CROSS_SUSTAIN = True
    strategy.config.USE_MOMENTUM_ALIGNMENT = False
    strategy.config.ENABLE_SHORTING = True
    strategy.config.USE_TRAILING_STOP = False
    strategy.tc.place_equity_order = _fake_place
    strategy.tc.get_historical = lambda *a, **k: [{"bar": 1}]
    strategy.tc.get_quote = lambda s: {"last": 10_000.0}
    for path in (strategy._STOPS_PATH, strategy._MOM_ENTRIES_PATH):
        _testlib.safe_remove(path)


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
            "sus":   getattr(strategy.config, "CROSS_SUSTAIN_MINUTES", 0),
            "ecs":   getattr(strategy.config, "ENABLE_CROSS_SUSTAIN", True),
            "time":  strategy.time.time,
        }
        strategy.log_trade = lambda *a, **k: None
        yield
        strategy.tc.place_equity_order  = saved["place"]
        strategy.tc.get_historical      = saved["hist"]
        strategy.tc.get_quote           = saved["quote"]
        strategy.ind.compute_indicators = saved["ci"]
        strategy.log_trade              = saved["log"]
        strategy.mh.entries_allowed     = saved["ea"]
        strategy.time.time              = saved["time"]
        strategy.config.USE_TRAILING_STOP      = saved["trail"]
        strategy.config.USE_MOMENTUM_ALIGNMENT = saved["align"]
        strategy.config.ENABLE_SHORTING        = saved["short"]
        strategy.config.CROSS_SUSTAIN_MINUTES  = saved["sus"]
        strategy.config.ENABLE_CROSS_SUSTAIN   = saved["ecs"]
        strategy._cross_gap_logged.clear()
        strategy._cross_first_seen.clear()
        strategy._cross_confirmed.clear()
except ImportError:
    pass


class _Clock:
    """Controllable stand-in for time.time() so a 30-minute rule costs no wall
    clock. Installed on strategy.time, the module the rule actually calls."""

    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * 60.0


def _clock(t=1_000_000.0):
    c = _Clock(t)
    strategy.time.time = c
    return c


def _set_sig(ema_short, ema_long, price=100.0, rsi=50.0, **kw):
    sig = {"close": price, "ema_short": ema_short, "ema_long": ema_long,
           "rsi": rsi, "bullish_cross": False, "bearish_cross": False,
           "atr": 4.0}
    sig.update(kw)
    strategy.ind.compute_indicators = lambda *a, **k: sig
    return sig


def _pos(symbol, qty):
    return [{"symbol": symbol, "quantity": qty, "cost_basis": 100.0 * abs(qty)}]


# ── Entry: young cross is deferred, mature cross fires ────────────────────────

def test_long_entry_blocked_while_cross_is_young():
    _reset()
    c = _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)          # 1.0% gap, well clear
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert _sides("buy") == [], "a 0-minute-old cross must not enter"
    c.advance(10)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert _sides("buy") == [], "a 10-minute-old cross must not enter"
    assert strategy._cross_sustain_blocks == 0, \
        "still pending, not blocked — a block is a cross that DIED before maturing"


def test_long_entry_fires_once_cross_matures():
    _reset()
    c = _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert _sides("buy") == []
    c.advance(31)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert len(_sides("buy")) == 1, "a 31-minute-old cross must enter"


def test_short_entry_blocked_then_fires():
    _reset()
    c = _clock()
    _set_sig(99.0, 100.0, bearish_cross=True)
    strategy.evaluate_stock("BBB", "acct", [], 100_000.0)
    assert _sides("sell_short") == [], "young death cross must not short"
    c.advance(31)
    strategy.evaluate_stock("BBB", "acct", [], 100_000.0)
    assert len(_sides("sell_short")) == 1, "mature death cross must short"


def test_exactly_at_threshold_is_allowed():
    _reset()
    c = _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    c.advance(30)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert len(_sides("buy")) == 1, ">= threshold, not > threshold"


# ── Exits are NEVER gated (exit-side variants backtested negative) ────────────

def test_long_exit_not_gated_by_sustain():
    _reset()
    _clock()
    _set_sig(99.0, 100.0, rsi=50.0)          # bearish STATE, no edge key
    strategy.evaluate_stock("AAA", "acct", _pos("AAA", 100), 100_000.0)
    assert len(_sides("sell")) == 1, "a long exit must fire immediately"
    assert strategy._cross_sustain_blocks == 0, "exits must not count as blocks"


def test_short_cover_not_gated_by_sustain():
    _reset()
    _clock()
    _set_sig(101.0, 100.0, rsi=50.0)         # bullish STATE
    strategy.evaluate_stock("BBB", "acct", _pos("BBB", -100), 100_000.0)
    assert len(_sides("buy_to_cover")) == 1, "a short cover must fire immediately"
    assert strategy._cross_sustain_blocks == 0


# ── Clock restarts when the cross lapses ─────────────────────────────────────

def test_lapsed_cross_restarts_the_clock():
    _reset()
    c = _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    c.advance(25)

    _set_sig(100.0, 100.0, bullish_cross=False)      # cross evaporates
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    c.advance(10)

    _set_sig(101.0, 100.0, bullish_cross=True)       # reappears
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert _sides("buy") == [], "35 min of wall clock, but only a fresh cross"
    c.advance(31)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert len(_sides("buy")) == 1, "clock restarted, then matured"


def test_gap_suppressed_cross_does_not_bank_time():
    _reset()
    c = _clock()
    # Real edge key but a 0.01% gap — suppressed by EMA_CROSS_MIN_GAP_PCT, so it
    # must not quietly accrue sustain credit while it sits below the gap floor.
    _set_sig(100.01, 100.0, bullish_cross=True)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    c.advance(40)
    _set_sig(101.0, 100.0, bullish_cross=True)       # now clears the gap
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert _sides("buy") == [], "sustain starts when the cross becomes VALID"
    c.advance(31)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert len(_sides("buy")) == 1


# ── Independence and disable switch ──────────────────────────────────────────

def test_symbols_track_independently():
    _reset()
    c = _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    c.advance(31)
    strategy.evaluate_stock("BBB", "acct", [], 100_000.0)   # BBB's first sighting
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)   # AAA's cross is mature
    assert [o for o in _sides("buy") if o[0] == "BBB"] == [], \
        "BBB must not inherit AAA's elapsed time"
    assert len([o for o in _sides("buy") if o[0] == "AAA"]) == 1, \
        "AAA waited the full window and must enter"


def test_directions_track_independently():
    _reset()
    c = _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    c.advance(31)
    _set_sig(99.0, 100.0, bearish_cross=True)     # opposite direction, fresh
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert _sides("sell_short") == [], "bear clock must not inherit bull's time"


def test_reversal_after_firing_does_not_unwind_the_entry():
    """Cross reverses at 31 min — the signal already fired at 30, and a later
    reversal must not retroactively count as a block."""
    _reset()
    c = _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    c.advance(30)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert len(_sides("buy")) == 1, "matured cross fires"

    c.advance(1)
    _set_sig(100.0, 100.0, bullish_cross=False)      # cross evaporates at 31 min
    strategy.evaluate_stock("AAA", "acct", _pos("AAA", 100), 100_000.0)
    assert len(_sides("buy")) == 1, "no second entry"
    assert strategy._cross_sustain_blocks == 0, \
        "a cross that already fired is not a block when it later reverses"


def test_clock_cleared_when_position_opens():
    _reset()
    c = _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    c.advance(31)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert len(_sides("buy")) == 1
    assert ("AAA", "bull") not in strategy._cross_first_seen, \
        "entry must drop the pre-entry clock, not leave it running for the trade"


def test_block_counts_only_crosses_that_never_fired():
    _reset()
    c = _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)   # PENDING
    c.advance(25)
    _set_sig(100.0, 100.0, bullish_cross=False)             # dies at 25 min
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert strategy._cross_sustain_blocks == 1, \
        "a cross that appeared and died young is exactly one block"
    assert _sides("buy") == []


def test_zero_disables_the_rule():
    _reset(sustain=0)
    _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)
    strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
    assert len(_sides("buy")) == 1, "CROSS_SUSTAIN_MINUTES=0 must enter at once"
    assert strategy._cross_sustain_blocks == 0


def test_counter_counts_dead_crosses_not_polls():
    """12 polls inside the window is not 12 blocks — and while the cross is still
    alive it is not a block at all. One dead cross is exactly one block."""
    _reset()
    c = _clock()
    _set_sig(101.0, 100.0, bullish_cross=True)
    for _ in range(12):
        strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
        c.advance(1)
    assert strategy._cross_sustain_blocks == 0, "still pending after 12 polls"

    _set_sig(100.0, 100.0, bullish_cross=False)      # dies at 12 min
    for _ in range(5):
        strategy.evaluate_stock("AAA", "acct", [], 100_000.0)
        c.advance(1)
    assert strategy._cross_sustain_blocks == 1, \
        "one dead cross = one block, however many polls observe the corpse"


if __name__ == "__main__":
    tmp = tempfile.mkdtemp()
    strategy._STOPS_PATH = os.path.join(tmp, "stops.json")
    strategy._MOM_ENTRIES_PATH = os.path.join(tmp, "mom.json")
    strategy.log_trade = lambda *a, **k: None
    _saved_time = strategy.time.time

    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
            finally:
                strategy.time.time = _saved_time
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
