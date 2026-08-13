"""
Unit tests for the profit-floor ladder — NO network.

Same isolation contract as test_stops.py: strategy._STOPS_PATH is redirected to a
throwaway temp file and the TradeStation client is stubbed, so nothing here can
touch data/stop_prices.json or place a real order.

The scenarios are built around ONE deliberate lever: the ATR trail width. The
ladder is designed to bind only when the trail is WIDER than the rung, so a wide
ATR (atr=10, mult=2.5 -> 25 points of trail on a 100-point entry) makes the floor
win, and a tight one (atr=1 -> 2.5 points) makes the trail win. Both directions
are covered because the ladder mirrors below entry for shorts.

Run:  python3 test_profit_floor.py
"""

import os
import tempfile

import _testlib
import config
import strategy

# ── Test doubles ──────────────────────────────────────────────────────────────
_orders = []          # (symbol, side, qty, stop_price) from place_equity_order
_cancels = []         # order ids passed to cancel_order
_order_result = {"order": {"id": "NEW1"}}   # flip to None to fail a placement
_cancel_ok = True
_outcome_state = "dead"     # get_order_outcome vocabulary: filled/working/dead/unknown.
                            # "dead" == cancelled cleanly. "filled"/"working"
                            # exercise the two raise guards.


def _fake_place(account_id, symbol, side, qty, **kw):
    _orders.append((symbol, side, qty, kw.get("stop_price")))
    return _order_result


def _fake_cancel(account_id, order_id):
    _cancels.append(order_id)
    return _cancel_ok


def _fake_outcome(account_id, order_id):
    return {"state": _outcome_state}


def _fake_quote(price):
    return lambda symbol: ({"last": price} if price is not None else None)


def _capture_logs():
    """Collect strategy.logger.info messages, fully formatted."""
    msgs = []
    orig = strategy.logger.info
    strategy.logger.info = lambda fmt, *a: msgs.append(fmt % a if a else fmt)
    return msgs, orig


def _reset(quote_price=None):
    global _cancel_ok, _outcome_state, _order_result
    _orders.clear()
    _cancels.clear()
    _cancel_ok = True
    _outcome_state = "dead"
    _order_result = {"order": {"id": "NEW1"}}
    strategy._stop_exits = 0
    strategy._profit_floors = 0
    strategy._floors_raised = 0
    strategy._floor_raise_failures = 0
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy.tc.place_equity_order = _fake_place
    strategy.tc.cancel_order = _fake_cancel
    strategy.tc.get_order_outcome = _fake_outcome
    strategy.tc.get_quote = _fake_quote(quote_price)
    config.ENABLE_PROFIT_FLOOR = True
    config.ENABLE_PROFIT_FLOOR_BROKER_RAISE = False
    # Pinned, not assumed: these are module-level globals that other test modules
    # flip and do not always restore, so under `pytest` (one process, all modules)
    # the raise tests would otherwise pass or fail depending on collection order.
    config.ENABLE_BROKER_STOP_FLOOR = True
    _testlib.safe_remove(strategy._STOPS_PATH)


def _rec(direction="long", entry=100.0, atr=10.0, mult=2.5, water=None,
         stop=None, **extra):
    """A stop record with a WIDE trail by default, so the ladder can bind."""
    water_key = "low_water" if direction == "short" else "high_water"
    rec = {"direction": direction, "entry_price": entry, "atr_at_entry": atr,
           "atr_mult": mult, water_key: water if water is not None else entry,
           "stop_price": stop if stop is not None else (
               entry + mult * atr if direction == "short" else entry - mult * atr),
           "opened": "2026-08-13", "bootstrapped": False}
    rec.update(extra)
    return rec


# ── Rung selection (the ladder itself, independent of the trail) ──────────────

def test_long_at_plus_15_activates_first_rung():
    """+15% is the first trigger: locks +10%, i.e. a floor at entry * 1.10."""
    _reset(quote_price=115.0)
    strategy._save_stops({"AAA": _rec(water=115.0)})
    exited = strategy._check_and_trail_stop(
        "AAA", 10, {"close": 115.0, "atr": 10.0}, "ACCT", [])
    assert exited is False, "a floor must never force an exit"
    rec = strategy._load_stops()["AAA"]
    # raw trail = 115 - 25 = 90; breakeven floor = 100; rung = 110 -> rung wins
    assert abs(rec["stop_price"] - 110.0) < 0.01, rec
    assert strategy._profit_floors == 1, strategy._profit_floors


def test_long_at_plus_25_takes_the_higher_rung():
    """A gain clearing several rungs takes the HIGHEST, not the first listed."""
    _reset(quote_price=125.0)
    strategy._save_stops({"AAA": _rec(water=125.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 125.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    # 25% clears the 15/20/25 rungs; the 25% rung locks 20% -> 120, not 110/115
    assert abs(rec["stop_price"] - 120.0) < 0.01, rec


def test_long_at_plus_14_arms_no_rung():
    """Just under the first trigger: breakeven still floors at entry, no rung."""
    _reset(quote_price=114.0)
    strategy._save_stops({"AAA": _rec(water=114.0)})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 114.0, "atr": 10.0},
                                       "ACCT", [])
    finally:
        strategy.logger.info = orig
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 100.0) < 0.01, rec   # breakeven, not a rung
    assert strategy._profit_floors == 0, strategy._profit_floors
    assert not [m for m in msgs if "PROFIT FLOOR" in m], msgs


def test_short_rungs_mirror_below_entry():
    """A short gains as price FALLS; its rung sits below entry and above price."""
    _reset(quote_price=85.0)
    strategy._save_stops({"SSS": _rec(direction="short", water=85.0)})
    exited = strategy._check_and_trail_stop(
        "SSS", -10, {"close": 85.0, "atr": 10.0}, "ACCT", [])
    assert exited is False, "short floor must not force an exit"
    rec = strategy._load_stops()["SSS"]
    # gain = (100-85)/100 = 15% -> lock 10% -> floor = 100 * 0.90 = 90
    # raw trail = 85 + 25 = 110; min(110, 90) = 90
    assert abs(rec["stop_price"] - 90.0) < 0.01, rec
    assert rec["stop_price"] < 100.0, "short rung must sit BELOW entry"
    assert rec["stop_price"] > 85.0, "short stop must stay ABOVE the market"
    assert strategy._profit_floors == 1, strategy._profit_floors


# ── Composition with the ATR trail (the whole point of the feature) ───────────

def test_tight_trail_beats_the_rung():
    """Floor BELOW the ATR trail: the trail wins, ladder is inert, no log."""
    _reset(quote_price=130.0)
    strategy._save_stops({"AAA": _rec(atr=1.0, water=130.0)})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 1.0},
                                       "ACCT", [])
    finally:
        strategy.logger.info = orig
    rec = strategy._load_stops()["AAA"]
    # raw trail = 130 - 2.5 = 127.5; rung (+30% -> lock 25%) = 125 -> trail wins
    assert abs(rec["stop_price"] - 127.5) < 0.01, rec
    assert strategy._profit_floors == 0, "inert ladder must not count a fire"
    assert not [m for m in msgs if "PROFIT FLOOR" in m], msgs


def test_wide_trail_loses_to_the_rung():
    """Floor ABOVE the ATR trail: the rung wins and the event is logged once."""
    _reset(quote_price=130.0)
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0)})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                       "ACCT", [])
    finally:
        strategy.logger.info = orig
    rec = strategy._load_stops()["AAA"]
    # raw trail = 130 - 25 = 105; rung (+30% -> lock 25%) = 125 -> rung wins
    assert abs(rec["stop_price"] - 125.0) < 0.01, rec
    floors = [m for m in msgs if "PROFIT FLOOR" in m]
    assert len(floors) == 1, f"expected one floor line, got {msgs}"
    assert "trail would be 105.00" in floors[0], floors[0]
    assert "floors #1" in floors[0], floors[0]


def test_rung_does_not_re_fire_on_later_polls():
    """Idempotent: once the stop sits on a rung, later polls do not re-count."""
    _reset(quote_price=130.0)
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0)})
    for _ in range(3):
        strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                       "ACCT", [])
    assert strategy._profit_floors == 1, strategy._profit_floors


def test_rung_never_loosens_a_higher_stop():
    """Price falling back un-arms the rung, but the ratchet keeps the stop."""
    _reset(quote_price=130.0)
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])
    assert abs(strategy._load_stops()["AAA"]["stop_price"] - 125.0) < 0.01
    strategy.tc.get_quote = _fake_quote(126.0)          # +26%: rung drops to 120
    strategy._check_and_trail_stop("AAA", 10, {"close": 126.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 125.0) < 0.01, "stop must not loosen to 120"


# ── Feature flag ──────────────────────────────────────────────────────────────

def test_disabled_has_no_effect():
    """ENABLE_PROFIT_FLOOR=False leaves the pre-existing behaviour untouched."""
    _reset(quote_price=130.0)
    config.ENABLE_PROFIT_FLOOR = False
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    # breakeven floor at entry still applies; the rung (125) does not
    assert abs(rec["stop_price"] - 105.0) < 0.01, rec    # raw trail, > entry
    assert strategy._profit_floors == 0, strategy._profit_floors


# ── Config validation ─────────────────────────────────────────────────────────

def test_rung_with_lock_at_or_above_trigger_is_rejected():
    """A rung that would arm the stop AT or THROUGH the market fails at import."""
    for bad in ([(0.15, 0.15)], [(0.20, 0.25)]):
        try:
            config._validate_profit_floor_steps(bad)
        except ValueError as e:
            assert "lock < trigger" in str(e), str(e)
        else:
            raise AssertionError(f"{bad} should have been rejected")


def test_shipped_ladder_is_valid_and_sorted_descending():
    triggers = [t for t, _ in config.PROFIT_FLOOR_STEPS_DESC]
    assert triggers == sorted(triggers, reverse=True), triggers
    assert all(lk < t for t, lk in config.PROFIT_FLOOR_STEPS_DESC)


# ── Broker GTC raise ──────────────────────────────────────────────────────────

def test_broker_floor_raised_behind_the_rung_not_onto_it():
    """The new GTC sits a buffer BELOW the rung, so the bot's stop trips first."""
    _reset(quote_price=130.0)
    config.ENABLE_PROFIT_FLOOR_BROKER_RAISE = True
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0,
                                      broker_order_id="OLD1",
                                      broker_floor_price=70.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    # rung = 125; gap = (1.2-1) * 2.5 * 10 = 5 -> new floor 120, bot stop 125
    assert _cancels == ["OLD1"], _cancels
    assert len(_orders) == 1, _orders
    assert _orders[0][1] == "sell" and abs(_orders[0][3] - 120.0) < 0.01, _orders
    assert abs(rec["broker_floor_price"] - 120.0) < 0.01, rec
    assert rec["broker_floor_price"] < rec["stop_price"], \
        "GTC must rest BELOW the bot stop, never on it"
    assert rec["broker_order_id"] == "NEW1", rec
    assert strategy._floors_raised == 1


def test_broker_floor_raise_is_one_shot_per_rung():
    """Re-polling the same rung must not churn cancel/replace at the broker."""
    _reset(quote_price=130.0)
    config.ENABLE_PROFIT_FLOOR_BROKER_RAISE = True
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0,
                                      broker_order_id="OLD1",
                                      broker_floor_price=70.0)})
    for _ in range(3):
        strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                       "ACCT", [])
    assert len(_orders) == 1, f"expected one raise, got {_orders}"
    assert strategy._floors_raised == 1


def test_broker_floor_raise_off_by_default_leaves_gtc_alone():
    _reset(quote_price=130.0)                    # flag stays False from _reset
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0,
                                      broker_order_id="OLD1",
                                      broker_floor_price=70.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert _cancels == [] and _orders == [], (_cancels, _orders)
    assert rec["broker_floor_price"] == 70.0, rec
    assert abs(rec["stop_price"] - 125.0) < 0.01, "bot-side rung still applies"


def test_failed_replace_leaves_position_unfloored_and_counted():
    """Cancel succeeded, placement failed: the record must not claim protection."""
    global _order_result
    _reset(quote_price=130.0)
    config.ENABLE_PROFIT_FLOOR_BROKER_RAISE = True
    _order_result = None                         # broker refuses the new stop
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0,
                                      broker_order_id="OLD1",
                                      broker_floor_price=70.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert "broker_order_id" not in rec and "broker_floor_price" not in rec, rec
    assert strategy._floor_raise_failures == 1
    assert abs(rec["stop_price"] - 125.0) < 0.01, "bot stop still protects"


def test_old_floor_filled_mid_raise_places_nothing():
    """If the floor fills while being raised the position is closed — placing a
    new GTC would open a fresh short."""
    global _outcome_state
    _reset(quote_price=130.0)
    config.ENABLE_PROFIT_FLOOR_BROKER_RAISE = True
    _outcome_state = "filled"
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0,
                                      broker_order_id="OLD1",
                                      broker_floor_price=70.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])
    assert _orders == [], f"nothing may be placed after a fill, got {_orders}"
    assert strategy._floors_raised == 0


if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="profit_floor_test_")
    strategy._STOPS_PATH = os.path.join(_tmpdir, "stop_prices.json")
    _orig = (strategy.tc.place_equity_order, strategy.tc.get_quote,
             strategy.tc.cancel_order, strategy.tc.get_order_outcome,
             strategy.log_trade, config.ENABLE_PROFIT_FLOOR,
             config.ENABLE_PROFIT_FLOOR_BROKER_RAISE,
             config.ENABLE_BROKER_STOP_FLOOR)
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
        (strategy.tc.place_equity_order, strategy.tc.get_quote,
         strategy.tc.cancel_order, strategy.tc.get_order_outcome,
         strategy.log_trade, config.ENABLE_PROFIT_FLOOR,
         config.ENABLE_PROFIT_FLOOR_BROKER_RAISE,
         config.ENABLE_BROKER_STOP_FLOOR) = _orig
