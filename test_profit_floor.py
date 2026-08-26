"""
Unit tests for the profit-floor ladder — NO network.

Same isolation contract as test_stops.py: strategy._STOPS_PATH is redirected to a
throwaway temp file and the TradeStation client is stubbed, so nothing here can
touch data/stop_prices.json or place a real order.

The scenarios are built around ONE deliberate lever: the ATR trail width. The
ladder is designed to bind only when the trail is WIDER than the rung, so a wide
ATR (atr=10, mult=2.5 -> 25 points of trail on a 100-point entry) makes the floor
win, and a tight one (atr=1 -> 2.5 points) makes the trail win.

Longs and shorts run SEPARATE ladders (asymmetric since 2026-08-14), so neither
direction's expectations can be derived from the other's — the short cases below
read their values from PROFIT_FLOOR_STEPS_SHORT and must be asserted explicitly.
The geometry is still mirrored (a short's rung sits below entry); only the
trigger/lock values differ.

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


def _fake_outcome(account_id, order_id, expected_cancel=False):
    """`state` means opposite things to the two callers that poll it, so the stub
    must answer per-order rather than globally.

      floor ids ("OLD*")  — _wait_floor_clear: "dead" == the cancel took effect.
      exit order ids      — _submit_exit_order step 3: "dead" == the broker
                            REJECTED the exit, so a global "dead" would make
                            every stop-out silently fail to exit.
    """
    if str(order_id).startswith("OLD"):
        return {"state": _outcome_state}
    return {"state": "filled"}


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
    strategy._profit_floors_long = 0
    strategy._profit_floors_short = 0
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


def _floors():
    """(long, short) profit-floor counts as a tuple.

    Every counter assertion below reads this rather than one counter, so each
    test proves BOTH that its own ladder counted AND that the other ladder did
    not — cross-contamination is checked at every site for free instead of in one
    dedicated test that only covers one direction pair. The counters are split
    because the two ladders are different instruments (short's first rung is a
    1pp gap at +2%, long's is 5pp at +15%), so a combined total cannot say
    whether the short micro-rungs earn their keep.
    """
    return (strategy._profit_floors_long, strategy._profit_floors_short)


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
    assert _floors() == (1, 0), _floors()   # long ladder only


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
    assert _floors() == (0, 0), _floors()
    assert not [m for m in msgs if "PROFIT FLOOR" in m], msgs


def test_short_rungs_mirror_below_entry():
    """A short gains as price FALLS; its rung sits below entry and above price."""
    _reset(quote_price=85.0)
    strategy._save_stops({"SSS": _rec(direction="short", water=85.0)})
    exited = strategy._check_and_trail_stop(
        "SSS", -10, {"close": 85.0, "atr": 10.0}, "ACCT", [])
    assert exited is False, "short floor must not force an exit"
    rec = strategy._load_stops()["SSS"]
    # gain = (100-85)/100 = 15% -> SHORT ladder locks 13% -> floor = 100*0.87 = 87
    # (the LONG ladder would lock 10% -> 90; asserting 87 is what proves the
    # short side reads its own ladder rather than the mirrored long one)
    # raw trail = 85 + 25 = 110; min(110, 87) = 87
    assert abs(rec["stop_price"] - 87.0) < 0.01, rec
    assert rec["stop_price"] < 100.0, "short rung must sit BELOW entry"
    assert rec["stop_price"] > 85.0, "short stop must stay ABOVE the market"
    assert _floors() == (0, 1), _floors()   # short ladder only


def test_short_arms_at_plus_8_where_a_long_would_not():
    """The discriminating case: +8% clears the short ladder's first rung but is
    below the long ladder's first trigger (+15%) entirely."""
    _reset(quote_price=92.0)
    strategy._save_stops({"SSS": _rec(direction="short", water=92.0)})
    strategy._check_and_trail_stop("SSS", -10, {"close": 92.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["SSS"]
    # gain = 8% -> SHORT rung locks 5% -> floor = 95. Under the long ladder NO
    # rung would arm and the breakeven lock would floor at entry (100) instead.
    assert abs(rec["stop_price"] - 95.0) < 0.01, rec
    assert _floors() == (0, 1), _floors()   # short ladder only


def test_long_and_short_fires_count_on_separate_counters():
    """The split itself: one long fire and one short fire in the same process
    land on different counters, and each log line names its own direction.

    This is the prerequisite for judging the short micro-rungs (AVGO 2026-08-26
    was the first floor-caused short exit; the report needs 3 before it will
    report a verdict). With a single total, a long-ladder fire at +15% and a
    short-ladder fire at +2% are indistinguishable, so the total can never say
    which ladder is doing the work."""
    _reset(quote_price=115.0)
    strategy._save_stops({"AAA": _rec(water=115.0)})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 115.0, "atr": 10.0},
                                       "ACCT", [])
        assert _floors() == (1, 0), _floors()

        # Same process, no reset: a short fire must not touch the long counter.
        strategy.tc.get_quote = _fake_quote(85.0)
        strategy._save_stops({"SSS": _rec(direction="short", water=85.0)})
        strategy._check_and_trail_stop("SSS", -10, {"close": 85.0, "atr": 10.0},
                                       "ACCT", [])
    finally:
        strategy.logger.info = orig
    assert _floors() == (1, 1), _floors()

    floors = [m for m in msgs if "PROFIT FLOOR" in m]
    assert len(floors) == 2, floors
    # Each line carries its own ladder's count, not a shared running total —
    # both read #1. The suffix is the only durable per-direction record: the
    # counters reset on restart and bot.log holds one day.
    assert "— long floors #1" in floors[0], floors[0]
    assert "— short floors #1" in floors[1], floors[1]


def test_long_at_plus_8_still_arms_no_rung():
    """The converse guard: the short ladder must not leak onto longs."""
    _reset(quote_price=108.0)
    strategy._save_stops({"AAA": _rec(water=108.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 108.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    # +8% is 8 points, short of the breakeven lock's 1-ATR (10 point) arm as well,
    # so the RAW trail is all that is left: 108 - 25 = 83. No rung, no breakeven.
    assert abs(rec["stop_price"] - 83.0) < 0.01, rec
    assert rec["profit_floor_active"] is False, rec
    assert _floors() == (0, 0), _floors()   # the short ladder must not leak here


# ── Composition with the ATR trail (the whole point of the feature) ───────────

def test_tight_trail_beats_the_rung():
    """Floor BELOW the ATR trail: the trail wins, ladder is inert, no log."""
    _reset(quote_price=125.0)
    strategy._save_stops({"AAA": _rec(atr=1.0, water=125.0)})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 125.0, "atr": 1.0},
                                       "ACCT", [])
    finally:
        strategy.logger.info = orig
    rec = strategy._load_stops()["AAA"]
    # raw trail = 125 - 2.5 = 122.5; rung (+25% -> lock 20%) = 120 -> trail wins.
    # Must sit in the 5pp band: the trail only wins when it is narrower than the
    # rung gap, and from +30% up the gap is 2pp < the 2.5-point trail here.
    assert abs(rec["stop_price"] - 122.5) < 0.01, rec
    assert _floors() == (0, 0), "inert ladder must not count a fire"
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
    # raw trail = 130 - 25 = 105; rung (+30% -> lock 28%) = 128 -> rung wins
    assert abs(rec["stop_price"] - 128.0) < 0.01, rec
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
    assert _floors() == (1, 0), _floors()


def test_rung_never_loosens_a_higher_stop():
    """Price falling back un-arms the rung, but the ratchet keeps the stop."""
    _reset(quote_price=130.0)
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])
    assert abs(strategy._load_stops()["AAA"]["stop_price"] - 128.0) < 0.01
    # 129, not 126: the armed stop is now 128, so 126 would BREACH it and exit
    # the position rather than test the ratchet. 129 un-arms the +30% rung
    # (falling back to the +25% rung at 120) while staying above the stop.
    strategy.tc.get_quote = _fake_quote(129.0)          # +29%: rung drops to 120
    strategy._check_and_trail_stop("AAA", 10, {"close": 129.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 128.0) < 0.01, "stop must not loosen to 120"


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
    assert _floors() == (0, 0), _floors()


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


def test_shipped_ladders_are_valid_and_sorted_descending():
    """BOTH shipped ladders, so a bad edit to either fails here and at import."""
    for name, ladder in (("long", config.PROFIT_FLOOR_STEPS_LONG_DESC),
                         ("short", config.PROFIT_FLOOR_STEPS_SHORT_DESC)):
        triggers = [t for t, _ in ladder]
        assert triggers == sorted(triggers, reverse=True), (name, triggers)
        assert all(lk < t for t, lk in ladder), (name, ladder)


def test_short_ladder_stays_within_reach():
    """A short's gain caps at 100% (the stock at zero), so a short rung above
    that is dead code. This is the premise the asymmetry exists for."""
    assert max(t for t, _ in config.PROFIT_FLOOR_STEPS_SHORT) <= 1.0, \
        config.PROFIT_FLOOR_STEPS_SHORT


def test_short_ladder_locks_earlier_than_long():
    """The asymmetry itself: shorts must arm at a lower gain than longs do."""
    assert (min(t for t, _ in config.PROFIT_FLOOR_STEPS_SHORT)
            < min(t for t, _ in config.PROFIT_FLOOR_STEPS_LONG))


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
    # rung = 128; gap = (1.2-1) * 2.5 * 10 = 5 -> new floor 123, bot stop 128
    assert _cancels == ["OLD1"], _cancels
    assert len(_orders) == 1, _orders
    assert _orders[0][1] == "sell" and abs(_orders[0][3] - 123.0) < 0.01, _orders
    assert abs(rec["broker_floor_price"] - 123.0) < 0.01, rec
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


def test_broker_floor_raise_disabled_leaves_gtc_alone():
    _reset(quote_price=130.0)   # _reset pins the flag False; config default is now True
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0,
                                      broker_order_id="OLD1",
                                      broker_floor_price=70.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert _cancels == [] and _orders == [], (_cancels, _orders)
    assert rec["broker_floor_price"] == 70.0, rec
    assert abs(rec["stop_price"] - 128.0) < 0.01, "bot-side rung still applies"


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
    assert abs(rec["stop_price"] - 128.0) < 0.01, "bot stop still protects"


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


def test_rearm_after_failed_exit_lets_the_ladder_raise_again():
    """Regression: a floor torn down and RE-ARMED at the entry-time disaster
    level must not keep the old broker_floor_lock. A surviving lock makes the
    raise guard return early forever, pinning the GTC at the disaster level for
    the life of the position with nothing logged."""
    _reset(quote_price=130.0)
    config.ENABLE_PROFIT_FLOOR_BROKER_RAISE = True
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0,
                                      broker_order_id="OLD1",
                                      broker_floor_price=70.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["broker_floor_price"] - 123.0) < 0.01, rec
    assert rec["broker_floor_lock"] == 0.28, rec

    # Tear the floor down the way every exit route does.
    strategy._forget_broker_floor(rec)
    assert "broker_floor_lock" not in rec, "lock must not outlive the floor"

    # Re-arm at the entry-time level, then let the ladder raise it again.
    rec["broker_order_id"], rec["broker_floor_price"] = "OLD2", 70.0
    strategy._save_stops({"AAA": rec})
    _orders.clear()
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert len(_orders) == 1, f"the raise must re-fire, got {_orders}"
    assert abs(rec["broker_floor_price"] - 123.0) < 0.01, rec


# ── Exit attribution ──────────────────────────────────────────────────────────

def _capture_trades():
    """Capture (action, notes, stop_attr) from strategy.log_trade."""
    seen = []
    orig = strategy.log_trade
    strategy.log_trade = lambda a, s, q, p, ot, oid=None, notes="", **kw: seen.append(
        (a, notes, kw.get("stop_attr")))
    return seen, orig


def test_floor_caused_exit_when_trail_would_not_have_fired():
    """Stop held by the rung, price above the raw trail -> the ladder CAUSED it."""
    _reset(quote_price=130.0)
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])          # arms rung at 128
    strategy.tc.get_quote = _fake_quote(124.0)          # breaches 128, trail is 105
    seen, orig = _capture_trades()
    try:
        exited = strategy._check_and_trail_stop("AAA", 10,
                                                {"close": 124.0, "atr": 10.0},
                                                "ACCT", [])
    finally:
        strategy.log_trade = orig
    assert exited is True, "price 124 must breach the 128 floor"
    assert len(seen) == 1, seen
    action, notes, attr = seen[0]
    assert attr["profit_floor_active"] is True, attr
    assert abs(attr["profit_floor_price"] - 128.0) < 0.01, attr
    assert abs(attr["atr_trail_at_exit"] - 105.0) < 0.01, attr
    assert attr["floor_caused_exit"] is True, attr
    assert "profit floor" in notes, notes
    assert "trailing stop" in notes, "must stay parseable by _exit_reason"


def test_trail_exit_is_not_credited_to_the_floor():
    """A stop-out the ATR trail would ALSO have caused is not the ladder's."""
    # +25%, not +30%: the trail only out-runs the rung inside the 5pp band (see
    # test_tight_trail_beats_the_rung), and this test needs the trail to own
    # the stop so the exit is genuinely not the ladder's.
    _reset(quote_price=125.0)
    strategy._save_stops({"AAA": _rec(atr=1.0, water=125.0)})   # tight trail wins
    strategy._check_and_trail_stop("AAA", 10, {"close": 125.0, "atr": 1.0},
                                   "ACCT", [])          # stop = 122.5 (trail)
    strategy.tc.get_quote = _fake_quote(122.0)
    seen, orig = _capture_trades()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 122.0, "atr": 1.0},
                                       "ACCT", [])
    finally:
        strategy.log_trade = orig
    _, notes, attr = seen[0]
    assert attr["profit_floor_active"] is False, attr
    assert attr["floor_caused_exit"] is False, attr
    assert "atr trail" in notes, notes


def test_short_exit_attribution_does_not_default_to_true():
    """Regression: a short with NO floor must not read as floor-caused. Comparing
    exit price against a `profit_floor_price` defaulted to 0 makes every short
    exit look caused by the ladder."""
    _reset(quote_price=99.0)
    strategy._save_stops({"SSS": _rec(direction="short", atr=1.0, water=99.0)})
    strategy._check_and_trail_stop("SSS", -10, {"close": 99.0, "atr": 1.0},
                                   "ACCT", [])          # stop = 101.5, no rung
    strategy.tc.get_quote = _fake_quote(102.0)
    seen, orig = _capture_trades()
    try:
        strategy._check_and_trail_stop("SSS", -10, {"close": 102.0, "atr": 1.0},
                                       "ACCT", [])
    finally:
        strategy.log_trade = orig
    _, _, attr = seen[0]
    assert attr["profit_floor_active"] is False, attr
    assert attr["floor_caused_exit"] is False, attr
    assert attr["profit_floor_price"] is None, attr


def test_floor_active_is_not_sticky_once_trail_overtakes():
    """The rung sets the stop, then the trail ratchets past it: attribution must
    hand the credit back to the trail.

    Note where the crossover actually is, because the 2026-08-14 rungs moved it.
    The trail overtakes only when it is NARROWER than the live rung's gap:
    trail beats floor iff (gain - lock) > mult * atr / entry. The long ladder's
    widest gap is 5pp, so the wide atr=10 trail used elsewhere in this file (25
    points = 25%) can no longer overtake at ANY gain — it did before only because
    the ladder capped at +50%, and it no longer caps there.

    So this uses a NARROW trail (atr=2.5, mult=2.5 -> 6.25 points) and a gain
    parked BETWEEN rungs, where the floor sits still while the trail keeps
    rising: at +49% the highest cleared rung is +40% -> lock 38% -> floor 138,
    while the trail has reached 142.75. A test that expects a wide trail to win
    is testing the pre-2026-08-14 capped ladder, which is not this one."""
    _reset(quote_price=120.0)
    strategy._save_stops({"AAA": _rec(atr=2.5, water=120.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 120.0, "atr": 2.5},
                                   "ACCT", [])
    # +20% -> lock 15% -> floor 115 beats trail 113.75
    assert strategy._load_stops()["AAA"]["profit_floor_active"] is True
    strategy.tc.get_quote = _fake_quote(149.0)   # trail 142.75 > between-rung 138
    strategy._check_and_trail_stop("AAA", 10, {"close": 149.0, "atr": 2.5},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 142.75) < 0.01, rec
    assert rec["profit_floor_active"] is False, rec


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
