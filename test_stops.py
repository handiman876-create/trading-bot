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

import _testlib
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
    _testlib.safe_remove(strategy._STOPS_PATH)


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
    seeds the stop ABOVE entry.

    Adoption price 95 is entry 100 - 1.25 ATR: this short is ALREADY >1 ATR in
    profit at adoption AND below entry, so the breakeven lock caps the stop at
    entry (100) rather than the raw 2.5x trail (105). That is intended — a
    position we take on already in real profit should not be allowed to round-trip
    to a loss; the price<entry gate keeps the cap above the market (no cover)."""
    _reset(quote_price=95.0)
    positions = [{"symbol": "AAA", "quantity": -10, "cost_basis": 1000.0}]  # entry 100
    exited = strategy._check_and_trail_stop(
        "AAA", -10, {"close": 95.0, "atr": 4.0}, "ACCT", positions)
    assert exited is False, "95 below the ABOVE stop -> no cover on bootstrap"
    rec = strategy._load_stops()["AAA"]
    assert rec["direction"] == "short", rec
    assert abs(rec["entry_price"] - 100.0) < 1e-6, rec        # 1000/|−10|
    assert abs(rec["low_water"] - 95.0) < 1e-6, rec           # min(entry, price)
    assert abs(rec["stop_price"] - 100.0) < 1e-6, rec         # breakeven-locked (raw trail 105)
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
    # ATR widened 4.0 -> 8.0 with the volatility bands: 4.0/210 = 1.90% is now the
    # LOW band (3.0x). This case is about the record SHAPE, not the width, so it
    # takes a normal-band ATR (8.0/210 = 3.81%) to keep the default 2.5x.
    _reset()
    strategy._arm_stop_on_entry("AAPL", 210.0, 8.0)
    rec = strategy._load_stops()["AAPL"]
    assert abs(rec["stop_price"] - 190.0) < 1e-6, rec           # 210 - 2.5*8
    assert abs(rec["high_water"] - 210.0) < 1e-6, rec
    assert rec["bootstrapped"] is False, rec


def test_arm_stop_on_entry_atr_none_is_noop():
    _reset()
    strategy._arm_stop_on_entry("AAPL", 210.0, None)
    assert strategy._load_stops() == {}, "no record armed without ATR"


# ── Regime-based ATR multiplier at entry ──────────────────────────────────────

def test_arm_regime_risk_on_uses_2p5x():
    # ATR widened 4.0 -> 8.0 when the volatility bands landed. The old 4.0/210 =
    # 1.90% now falls in the LOW band and correctly arms 3.0x, which is the new
    # rule working, not a regression — see test_low_vol_name_arms_wider_3x. This
    # case exists to pin the REGIME axis, so it needs a normal-band ATR:
    # 8.0/210 = 3.81%.
    _reset()
    strategy._arm_stop_on_entry("AAA", 210.0, 8.0, regime="risk_on")
    rec = strategy._load_stops()["AAA"]
    assert rec["atr_mult"] == 2.5, rec
    assert abs(rec["stop_price"] - 190.0) < 1e-6, rec          # 210 - 2.5*8

def test_arm_regime_cautious_uses_2p0x():
    """User's worked example: entry 204, atr 7.05, cautious -> 2.0x -> stop 189.90."""
    _reset()
    strategy._arm_stop_on_entry("NVDA", 204.0, 7.05, regime="cautious")
    rec = strategy._load_stops()["NVDA"]
    assert rec["atr_mult"] == 2.0, rec
    assert abs(rec["stop_price"] - 189.90) < 1e-6, rec         # 204 - 2.0*7.05

def test_arm_regime_defensive_uses_1p5x():
    _reset()
    strategy._arm_stop_on_entry("AAA", 200.0, 10.0, regime="defensive")
    rec = strategy._load_stops()["AAA"]
    assert rec["atr_mult"] == 1.5, rec
    assert abs(rec["stop_price"] - 185.0) < 1e-6, rec          # 200 - 1.5*10

def test_arm_regime_crisis_uses_1p0x():
    _reset()
    strategy._arm_stop_on_entry("AAA", 200.0, 10.0, regime="crisis")
    rec = strategy._load_stops()["AAA"]
    assert rec["atr_mult"] == 1.0, rec
    assert abs(rec["stop_price"] - 190.0) < 1e-6, rec          # 200 - 1.0*10

def test_arm_regime_unknown_falls_back_to_default():
    _reset()
    strategy._arm_stop_on_entry("AAA", 210.0, 4.0, regime="banana")
    rec = strategy._load_stops()["AAA"]
    assert rec["atr_mult"] == strategy.config.STOP_LOSS_ATR_MULT, rec   # 2.5 fallback

def test_short_arm_regime_defensive_is_tighter_above():
    """A short in defensive arms 1.5x ABOVE entry (tighter than 2.5x)."""
    _reset()
    strategy._arm_stop_on_entry("AAA", 100.0, 4.0, direction="short", regime="defensive")
    rec = strategy._load_stops()["AAA"]
    assert rec["atr_mult"] == 1.5, rec
    assert abs(rec["stop_price"] - 106.0) < 1e-6, rec          # 100 + 1.5*4

def test_trail_uses_armed_mult_not_live_regime():
    """A position armed at cautious (2.0x) keeps trailing at 2.0x even when the
    live regime has since flipped to risk_on — width is fixed at entry."""
    _reset(quote_price=220.0)
    strategy._arm_stop_on_entry("AAA", 200.0, 10.0, regime="cautious")   # stop 180, mult 2.0
    # Trail on a new high with the LIVE regime now risk_on (2.5x). Width must stay 2.0x.
    strategy._check_and_trail_stop("AAA", 10, {"close": 220.0, "atr": 10.0},
                                   "ACCT", [], regime="risk_on")
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["high_water"] - 220.0) < 1e-6, rec
    assert abs(rec["stop_price"] - 200.0) < 1e-6, rec          # 220 - 2.0*10 (armed), NOT 195 (2.5)

def test_trail_legacy_record_defaults_to_2p5x():
    """A pre-existing record with no atr_mult key trails at the 2.5x default."""
    _reset(quote_price=110.0)
    strategy._save_stops({"AAA": {
        "entry_price": 100.0, "atr_at_entry": 4.0, "high_water": 100.0,
        "stop_price": 90.0, "opened": "2026-07-13", "bootstrapped": False}})
    strategy._check_and_trail_stop("AAA", 10, {"close": 110.0, "atr": 4.0}, "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 100.0) < 1e-6, rec          # 110 - 2.5*4 (default)


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
    # Guarded like the deletes: writing this to the live file would corrupt real
    # stops just as thoroughly as removing it.
    with open(_testlib.assert_disposable(strategy._STOPS_PATH), "w") as f:
        f.write("{not valid json")
    assert strategy._load_stops() == {}, "corrupt file -> empty, no crash"


# ── Volatility-band ATR multiplier (ATR/price at entry) ───────────────────────
# Two axes now: regime (how afraid the market is) x band (how wide this name's
# daily range is). These lock all 12 cells plus the fallbacks.

def test_atr_band_classification():
    """<=2% low, 2-5% normal, >5% high; uncomputable -> None (no banding)."""
    assert strategy._atr_band(1.5, 100.0) == "low"        # 1.5%
    assert strategy._atr_band(2.0, 100.0) == "low"        # 2.0% — boundary is <=
    assert strategy._atr_band(2.01, 100.0) == "normal"    # just over low
    assert strategy._atr_band(5.0, 100.0) == "normal"     # 5.0% — boundary is >
    assert strategy._atr_band(5.01, 100.0) == "high"      # just over high
    assert strategy._atr_band(7.0, 100.0) == "high"       # 7%
    for bad in [(None, 100.0), (5.0, None), (0.0, 100.0), (5.0, 0.0), (-1.0, 100.0)]:
        assert strategy._atr_band(*bad) is None, bad


def test_all_twelve_table_cells():
    """Every regime x band cell resolves to the approved multiplier."""
    expected = {
        # regime:      (low,  normal, high)
        "risk_on":     (3.0,  2.5,    1.5),
        "cautious":    (2.5,  2.0,    1.25),
        "defensive":   (2.0,  1.5,    1.0),
        "crisis":      (1.5,  1.0,    0.75),
    }
    # price 100 -> atr 1.0 = 1% (low), 3.0 = 3% (normal), 7.0 = 7% (high)
    for regime, (lo, norm, hi) in expected.items():
        assert strategy._get_atr_mult(regime, 1.0, 100.0) == lo,   (regime, "low")
        assert strategy._get_atr_mult(regime, 3.0, 100.0) == norm, (regime, "normal")
        assert strategy._get_atr_mult(regime, 7.0, 100.0) == hi,   (regime, "high")


def test_normal_band_equals_plain_regime_mult():
    """The normal column must agree with the un-banded regime lookup, so the two
    sources of truth can never drift apart."""
    for regime in ["risk_on", "cautious", "defensive", "crisis"]:
        assert (strategy._get_atr_mult(regime, 3.0, 100.0)
                == strategy._regime_atr_mult(regime)), regime


def test_missing_atr_or_price_falls_back_to_regime_width():
    """An uncomputable ratio must give the OLD behaviour, never a tighter stop."""
    assert strategy._get_atr_mult("cautious", None, 100.0) == 2.0
    assert strategy._get_atr_mult("cautious", 7.0, None) == 2.0
    assert strategy._get_atr_mult("cautious", 0.0, 100.0) == 2.0
    assert strategy._get_atr_mult("banana", 7.0, 100.0) == strategy.config.STOP_LOSS_ATR_MULT


def test_crwd_scenario_high_vol_short_arms_1p25x():
    """CRWD: ATR 13.42 / price 191 = 7.03% -> high band, cautious -> 1.25x.
    Short stop sits ABOVE entry: 191 + 1.25*13.42 = 207.775 (not 217.84 at 2.0x)."""
    _reset()
    strategy._arm_stop_on_entry("CRWD", 191.0, 13.42, direction="short",
                                regime="cautious")
    rec = strategy._load_stops()["CRWD"]
    assert rec["atr_mult"] == 1.25, rec
    assert abs(rec["stop_price"] - 207.775) < 1e-6, rec
    assert rec["stop_price"] > rec["entry_price"], "short stop must be above entry"
    # the whole point: strictly tighter than the un-banded cautious width
    assert rec["stop_price"] < 191.0 + 2.0 * 13.42, rec


def test_aapl_scenario_normal_vol_arms_regime_width():
    """AAPL: ATR 7.78 / entry 307.15 = 2.53% -> normal band, risk_on -> 2.5x."""
    _reset()
    strategy._arm_stop_on_entry("AAPL", 307.15, 7.78, regime="risk_on")
    rec = strategy._load_stops()["AAPL"]
    assert rec["atr_mult"] == 2.5, rec
    assert abs(rec["stop_price"] - (307.15 - 2.5 * 7.78)) < 1e-6, rec


def test_low_vol_name_arms_wider_3x():
    """A 1.5%-ATR name in risk_on gets MORE room: 3.0x, not 2.5x."""
    _reset()
    strategy._arm_stop_on_entry("BND", 100.0, 1.5, regime="risk_on")
    rec = strategy._load_stops()["BND"]
    assert rec["atr_mult"] == 3.0, rec
    assert abs(rec["stop_price"] - 95.5) < 1e-6, rec        # 100 - 3.0*1.5


def test_bootstrap_bands_off_estimated_entry():
    """_bootstrap_stop bands off the cost-basis entry it writes, not live price."""
    _reset()
    positions = [{"symbol": "XYZ", "quantity": 10, "cost_basis": 1000.0}]  # entry 100
    sig = {"atr": 7.0, "close": 100.0}                                     # 7% -> high
    rec = strategy._bootstrap_stop("XYZ", 10, sig, positions, 100.0, regime="cautious")
    assert rec["atr_mult"] == 1.25, rec
    assert abs(rec["stop_price"] - (100.0 - 1.25 * 7.0)) < 1e-6, rec


def test_existing_records_are_untouched_by_banding():
    """The 9 live records have NO atr_mult key. Banding must not reach them: they
    keep trailing at the 2.5x fallback regardless of their ATR/price ratio."""
    _reset(quote_price=200.0)
    strategy._save_stops({"CRWD": {
        "entry_price": 205.49, "atr_at_entry": 10.61,      # 5.16% -> would be "high"
        "stop_price": 183.015, "high_water": 209.54,
        "opened": "2026-07-15", "bootstrapped": False, "direction": "long",
    }})
    before = strategy._load_stops()["CRWD"]["stop_price"]
    sig = {"atr": 10.61, "close": 200.0, "rsi": 50.0}
    strategy._check_and_trail_stop("CRWD", 242, sig, "acct", [], "cautious")
    rec = strategy._load_stops()["CRWD"]
    assert "atr_mult" not in rec, "trailing must not back-fill a width"
    # high_water unchanged (200 < 209.54) so the stop must be unchanged too
    assert rec["stop_price"] == before, (rec["stop_price"], before)


def test_counters_tick_on_off_normal_arms():
    """Observability: both off-normal bands must be countable, or we can't tell
    whether this rule ever binds."""
    _reset()
    hi_before, lo_before = strategy._high_vol_stops, strategy._low_vol_stops
    strategy._get_atr_mult("cautious", 7.0, 100.0)     # high
    strategy._get_atr_mult("cautious", 1.0, 100.0)     # low
    strategy._get_atr_mult("cautious", 3.0, 100.0)     # normal — must NOT tick
    assert strategy._high_vol_stops == hi_before + 1, strategy._high_vol_stops
    assert strategy._low_vol_stops == lo_before + 1, strategy._low_vol_stops


# ── Trail logging + counter ───────────────────────────────────────────────────
# Added 2026-07-22: NVDA's stop moved 195.53 -> 196.67 with no trace in bot.log,
# and had to be reconstructed from stop_prices.json plus per-day highs out of the
# rotated logs. The trail is the primary risk control; it should not be invisible.

def _capture_logs(monkeypatch=None):
    """Collect strategy.logger.info messages, fully formatted."""
    msgs = []
    orig = strategy.logger.info
    strategy.logger.info = lambda fmt, *a: msgs.append(fmt % a if a else fmt)
    return msgs, orig


def test_trail_logs_and_counts_when_stop_moves():
    _reset(quote_price=110.0)
    strategy._stops_trailed = 0
    strategy._save_stops({"AAA": {
        "entry_price": 100.0, "atr_at_entry": 4.0, "high_water": 100.0,
        "stop_price": 90.0, "opened": "2026-07-13", "bootstrapped": False}})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 110.0, "atr": 4.0},
                                       "ACCT", [])
    finally:
        strategy.logger.info = orig

    trail = [m for m in msgs if "STOP TRAIL" in m]
    assert len(trail) == 1, f"expected one trail line, got {msgs}"
    assert "AAA" in trail[0] and "90.00 → 100.00" in trail[0], trail[0]
    assert "high_water=110.00" in trail[0], "longs report high_water"
    assert "trail=2.50x4.00" in trail[0], trail[0]
    assert strategy._stops_trailed == 1, strategy._stops_trailed


def test_no_trail_log_when_stop_unchanged():
    """_save_stops runs every poll for every held name; an unguarded log here
    would emit ~55k lines a week. Only a real new extreme may log."""
    _reset(quote_price=104.0)
    strategy._stops_trailed = 0
    strategy._save_stops({"AAA": {
        "entry_price": 100.0, "atr_at_entry": 4.0, "high_water": 110.0,
        "stop_price": 100.0, "opened": "2026-07-13", "bootstrapped": False}})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 104.0, "atr": 4.0},
                                       "ACCT", [])
    finally:
        strategy.logger.info = orig

    assert [m for m in msgs if "STOP TRAIL" in m] == [], \
        "pullback must not log a trail — the stop did not move"
    assert strategy._stops_trailed == 0


def test_short_trail_reports_low_water():
    """Shorts ratchet the other way; the label must say which water it tracks."""
    _reset(quote_price=90.0)
    strategy._stops_trailed = 0
    strategy._save_stops({"SSS": {
        "direction": "short", "entry_price": 100.0, "atr_at_entry": 4.0,
        "low_water": 100.0, "stop_price": 110.0, "opened": "2026-07-13",
        "bootstrapped": False}})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop("SSS", -10, {"close": 90.0, "atr": 4.0},
                                       "ACCT", [])
    finally:
        strategy.logger.info = orig

    trail = [m for m in msgs if "STOP TRAIL" in m]
    assert len(trail) == 1, f"expected one trail line, got {msgs}"
    assert "110.00 → 100.00" in trail[0], trail[0]      # 90 + 2.5*4, ratchet down
    assert "low_water=90.00" in trail[0], "shorts report low_water"
    assert strategy._stops_trailed == 1


# ── Breakeven lock (floor a winner's stop at entry after +1 ATR of profit) ────
# Added 2026-07-23: DDOG rode high_water $273 back down to a $241 stop, giving
# back every gain. This floors the stop at entry once a position has BOTH proven
# +1 ATR of profit (high/low-water) AND is still on the profit side of entry, so a
# winner can't round-trip to a loss. Retroactive-safe: an underwater name gates
# itself out (arming the floor there would exit through the market).

def test_breakeven_lock_long_floors_at_entry():
    """CRL, real numbers: high_water 237.61 >= entry 225.68 + 1*8.5252, price
    229.09 still > entry. Raw 2.5x trail is 216.30 (below entry) -> floor to entry."""
    _reset(quote_price=229.09)
    strategy._breakeven_locks = 0
    strategy._save_stops({"CRL": {
        "direction": "long", "entry_price": 225.68, "atr_at_entry": 8.5252,
        "high_water": 237.61, "stop_price": 216.297,
        "opened": "2026-07-16", "bootstrapped": False}})
    exited = strategy._check_and_trail_stop(
        "CRL", 221, {"close": 229.09, "atr": 8.5252}, "ACCT", [])
    rec = strategy._load_stops()["CRL"]
    assert exited is False, "must not exit — floor sits below the market"
    assert abs(rec["stop_price"] - 225.68) < 1e-6, rec          # floored at entry
    assert strategy._breakeven_locks == 1, strategy._breakeven_locks


def test_breakeven_lock_long_blocked_when_underwater():
    """DDOG: high_water qualifies but price is now BELOW entry. Flooring would arm
    a stop ABOVE the market and force an instant exit — so it must NOT apply."""
    _reset(quote_price=244.40)
    strategy._breakeven_locks = 0
    strategy._save_stops({"DDOG": {
        "direction": "long", "entry_price": 254.64, "atr_at_entry": 12.68,
        "high_water": 273.39, "stop_price": 241.69,
        "opened": "2026-07-14", "bootstrapped": False}})
    exited = strategy._check_and_trail_stop(
        "DDOG", 195, {"close": 244.40, "atr": 12.68}, "ACCT", [])
    rec = strategy._load_stops()["DDOG"]
    assert exited is False, "must not exit"
    assert abs(rec["stop_price"] - 241.69) < 1e-6, rec          # unchanged, no floor
    assert strategy._breakeven_locks == 0, "underwater name must not lock"


def test_breakeven_lock_not_armed_until_one_atr_profit():
    """NVDA: high_water 214.30 < entry 209.49 + 1*7.05 (216.54) — not qualified yet.
    Stop trails normally at 2.5x, no floor, no lock event."""
    _reset(quote_price=208.83)
    strategy._breakeven_locks = 0
    strategy._save_stops({"NVDA": {
        "direction": "long", "entry_price": 209.49, "atr_at_entry": 7.05,
        "high_water": 214.295, "stop_price": 196.67,
        "opened": "2026-07-14", "bootstrapped": False}})
    strategy._check_and_trail_stop(
        "NVDA", 238, {"close": 208.83, "atr": 7.05}, "ACCT", [])
    rec = strategy._load_stops()["NVDA"]
    assert abs(rec["stop_price"] - 196.67) < 1e-6, rec          # untouched
    assert strategy._breakeven_locks == 0


def test_breakeven_lock_short_caps_at_entry():
    """Short mirror: low_water 115 <= entry 122.99 - 1*6.59 (116.40), price 118
    still < entry -> stop capped DOWN to entry (raw trail would be 131.48)."""
    _reset(quote_price=118.0)
    strategy._breakeven_locks = 0
    strategy._save_stops({"PLTR": {
        "direction": "short", "entry_price": 122.99, "atr_at_entry": 6.59,
        "low_water": 115.0, "stop_price": 131.475,
        "opened": "2026-07-23", "bootstrapped": False}})
    exited = strategy._check_and_trail_stop(
        "PLTR", -392, {"close": 118.0, "atr": 6.59}, "ACCT", [])
    rec = strategy._load_stops()["PLTR"]
    assert exited is False, "must not cover — cap sits above the market"
    assert abs(rec["stop_price"] - 122.99) < 1e-6, rec          # capped at entry
    assert strategy._breakeven_locks == 1


def test_breakeven_lock_short_not_armed_until_one_atr():
    """Real PLTR today: low_water 120.84 is only ~0.3 ATR of profit -> no cap."""
    _reset(quote_price=121.0)
    strategy._breakeven_locks = 0
    strategy._save_stops({"PLTR": {
        "direction": "short", "entry_price": 122.99, "atr_at_entry": 6.59,
        "low_water": 120.84, "stop_price": 130.73,
        "opened": "2026-07-23", "bootstrapped": False}})
    strategy._check_and_trail_stop(
        "PLTR", -392, {"close": 121.0, "atr": 6.59}, "ACCT", [])
    rec = strategy._load_stops()["PLTR"]
    assert strategy._breakeven_locks == 0, "0.3 ATR is not +1 ATR"
    assert rec["stop_price"] > 122.99, rec                       # not capped to entry


def test_breakeven_lock_noop_when_trail_already_above_entry():
    """AAPL: qualified and in profit, but the normal 2.5x trail already sits above
    entry (315.34 > 307.15). The floor changes nothing and must NOT count a lock."""
    _reset(quote_price=321.70)
    strategy._breakeven_locks = 0
    strategy._save_stops({"AAPL": {
        "direction": "long", "entry_price": 307.15, "atr_at_entry": 7.78,
        "high_water": 334.785, "stop_price": 315.335,
        "opened": "2026-07-14", "bootstrapped": False}})
    strategy._check_and_trail_stop(
        "AAPL", 100, {"close": 321.70, "atr": 7.78}, "ACCT", [])
    rec = strategy._load_stops()["AAPL"]
    assert abs(rec["stop_price"] - 315.335) < 1e-6, rec          # trail wins, unchanged
    assert strategy._breakeven_locks == 0, "no lock event when trail already > entry"


def test_breakeven_lock_fires_once_then_idempotent():
    """Once floored at entry, a later poll still in the lock window must NOT re-count."""
    _reset(quote_price=229.09)
    strategy._breakeven_locks = 0
    strategy._save_stops({"CRL": {
        "direction": "long", "entry_price": 225.68, "atr_at_entry": 8.5252,
        "high_water": 237.61, "stop_price": 216.297,
        "opened": "2026-07-16", "bootstrapped": False}})
    strategy._check_and_trail_stop("CRL", 221, {"close": 229.09, "atr": 8.5252}, "ACCT", [])
    assert strategy._breakeven_locks == 1
    strategy._check_and_trail_stop("CRL", 221, {"close": 229.09, "atr": 8.5252}, "ACCT", [])
    rec = strategy._load_stops()["CRL"]
    assert strategy._breakeven_locks == 1, "must not re-count once locked"
    assert abs(rec["stop_price"] - 225.68) < 1e-6, rec          # holds at entry


def test_breakeven_lock_not_armed_at_exactly_entry():
    """At price == entry the floor would equal the market, and the breach check
    would then exit at breakeven. The strict > / < gate must leave it un-armed."""
    _reset(quote_price=100.0)
    strategy._breakeven_locks = 0
    strategy._save_stops({"AAA": {
        "direction": "long", "entry_price": 100.0, "atr_at_entry": 4.0,
        "high_water": 105.0, "stop_price": 95.0,       # raw trail 95, below entry
        "opened": "2026-07-13", "bootstrapped": False}})
    exited = strategy._check_and_trail_stop("AAA", 10, {"close": 100.0, "atr": 4.0}, "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert exited is False, "must not exit at breakeven"
    assert strategy._breakeven_locks == 0, "price == entry must not arm the lock"
    assert abs(rec["stop_price"] - 95.0) < 1e-6, rec            # floor NOT applied


def test_breakeven_lock_logs_the_event():
    """The lock emits one BREAKEVEN LOCK line naming entry, the would-be trail, #N."""
    _reset(quote_price=229.09)
    strategy._breakeven_locks = 0
    strategy._save_stops({"CRL": {
        "direction": "long", "entry_price": 225.68, "atr_at_entry": 8.5252,
        "high_water": 237.61, "stop_price": 216.297,
        "opened": "2026-07-16", "bootstrapped": False}})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop("CRL", 221, {"close": 229.09, "atr": 8.5252}, "ACCT", [])
    finally:
        strategy.logger.info = orig
    locks = [m for m in msgs if "BREAKEVEN LOCK" in m]
    assert len(locks) == 1, f"expected one lock line, got {msgs}"
    assert "CRL" in locks[0] and "225.68" in locks[0], locks[0]
    assert "trail would be 216.30" in locks[0], locks[0]
    assert "locks #1" in locks[0], locks[0]


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
