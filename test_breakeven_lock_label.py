"""
Unit tests for breakeven-lock EXIT ATTRIBUTION — NO network.

Same isolation contract as test_profit_floor.py: strategy._STOPS_PATH is
redirected to a throwaway temp file and the TradeStation client is stubbed, so
nothing here can touch data/stop_prices.json or place a real order.

WHAT THIS FILE EXISTS TO PIN
----------------------------
The "breakeven lock" exit label was unreachable from the day it shipped. The
label reused the ARMING flag, which carries a `price > entry` (long) /
`price < entry` (short) clamp — but a stop resting AT entry can only be breached
by price crossing back THROUGH entry, so the clamp is false on exactly the cycle
attribution runs. Every lock-held stop-out therefore logged as "atr trail".
Observed: QQQ 2026-08-18, stop == entry == 717.26 with the raw trail 21 points
away at 696.00, labelled "atr trail".

The fix reconstructs the trigger from the WATER marks (strategy._breakeven_reached),
which are monotonic and so survive the retrace. test_long_lock_exit_is_labelled_lock
is the direct regression; it fails on the pre-fix code.

GEOMETRY
--------
atr=3 on a 100 entry with mult=2.5 gives a 7.5-point trail and a 3-point
(1 ATR) lock trigger. That spread is deliberate: the excursion needed to arm the
lock (3%) stays UNDER the profit ladder's first rung (+15% long, +8% short), so
these cases exercise the lock with the ladder left ON, rather than switching a
second feature off and testing a configuration the bot never runs.

Run:  python3 test_breakeven_lock_label.py
"""

import os
import tempfile

import _testlib
import config
import performance_analyzer as pa
import strategy

# ── Test doubles ──────────────────────────────────────────────────────────────
_orders = []
_cancels = []
_order_result = {"order": {"id": "NEW1"}}   # flip to None to fail a placement
_cancel_ok = True


def _fake_place(account_id, symbol, side, qty, **kw):
    _orders.append((symbol, side, qty, kw.get("stop_price")))
    return _order_result


def _fake_cancel(account_id, order_id):
    return _cancel_ok


def _fake_outcome(account_id, order_id, expected_cancel=False):
    """Floor ids ("OLD*") poll for a clean cancel; exit ids poll for a fill.
    A global answer would make one of the two callers always wrong — see the
    same stub in test_profit_floor.py."""
    if str(order_id).startswith("OLD"):
        return {"state": "dead"}
    return {"state": "filled"}


def _fake_quote(price):
    return lambda symbol: ({"last": price} if price is not None else None)


def _capture_trades():
    """Capture (action, notes, stop_attr) from strategy.log_trade."""
    seen = []
    orig = strategy.log_trade
    strategy.log_trade = lambda a, s, q, p, ot, oid=None, notes="", **kw: seen.append(
        (a, notes, kw.get("stop_attr")))
    return seen, orig


def _capture_logs():
    msgs = []
    orig = strategy.logger.info
    strategy.logger.info = lambda fmt, *a: msgs.append(fmt % a if a else fmt)
    return msgs, orig


def _reset(quote_price=None):
    global _cancel_ok, _order_result
    _orders.clear()
    _cancels.clear()
    _cancel_ok = True
    _order_result = {"order": {"id": "NEW1"}}
    strategy._stop_exits = 0
    strategy._profit_floors = 0
    strategy._breakeven_locks = 0
    strategy._breakeven_lock_exits = 0
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy.tc.place_equity_order = _fake_place
    strategy.tc.cancel_order = _fake_cancel
    strategy.tc.get_order_outcome = _fake_outcome
    strategy.tc.get_quote = _fake_quote(quote_price)
    # Pinned, not assumed: other test modules flip these module-level globals and
    # do not always restore them, so under pytest (one process, all modules) the
    # expectations below would depend on collection order.
    config.ENABLE_BREAKEVEN_LOCK = True
    config.BREAKEVEN_LOCK_ATR = 1.0
    config.ENABLE_PROFIT_FLOOR = True
    config.ENABLE_PROFIT_FLOOR_BROKER_RAISE = False
    config.ENABLE_BROKER_STOP_FLOOR = True
    config.VIX_CRISIS_SHADOW = True     # crisis floor armed-only unless a test opts in
    _testlib.safe_remove(strategy._STOPS_PATH)


# ── Global-state containment ──────────────────────────────────────────────────
# _reset() stubs attributes on `strategy.tc`, which IS the real
# tradestation_client module object, and flips live config flags. Those patches
# are process-global and outlive the test that made them.
#
# The older modules that do this (test_profit_floor.py, test_stops.py) never had
# to care, purely because they sort AFTER the modules that would notice. This
# file sorts second, so without the fixture below it leaves fake brokers in place
# for test_critical_sink_and_attribution, test_fill_price and test_order_outcome
# — 18 failures in modules that never mention the breakeven lock. Cleaning up is
# this module's job, not the next module's problem.
_STUBBED_TC_ATTRS = ("place_equity_order", "get_quote", "cancel_order",
                     "get_order_outcome")
_PINNED_CONFIG_FLAGS = ("ENABLE_BREAKEVEN_LOCK", "BREAKEVEN_LOCK_ATR",
                        "VIX_CRISIS_SHADOW",
                        "ENABLE_PROFIT_FLOOR", "ENABLE_PROFIT_FLOOR_BROKER_RAISE",
                        "ENABLE_BROKER_STOP_FLOOR")

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_global_stubs():
        saved_tc = {n: getattr(strategy.tc, n) for n in _STUBBED_TC_ATTRS}
        saved_cfg = {n: getattr(config, n) for n in _PINNED_CONFIG_FLAGS}
        saved_log = strategy.log_trade
        yield
        for n, v in saved_tc.items():
            setattr(strategy.tc, n, v)
        for n, v in saved_cfg.items():
            setattr(config, n, v)
        strategy.log_trade = saved_log
except ImportError:                     # direct `python3 test_...py` run
    pass                                # the __main__ block restores instead


def _rec(direction="long", entry=100.0, atr=3.0, mult=2.5, water=None,
         stop=None, **extra):
    water_key = "low_water" if direction == "short" else "high_water"
    rec = {"direction": direction, "entry_price": entry, "atr_at_entry": atr,
           "atr_mult": mult, water_key: water if water is not None else entry,
           "stop_price": stop if stop is not None else (
               entry + mult * atr if direction == "short" else entry - mult * atr),
           "opened": "2026-08-19", "bootstrapped": False}
    rec.update(extra)
    return rec


# ── _breakeven_reached: the water-based predicate itself ──────────────────────

def test_reached_is_true_after_price_retraces_through_entry():
    """The whole point: monotonic water keeps this true once the excursion
    happened, regardless of where price is NOW. A `price`-aware version returns
    False here and that is the bug."""
    _reset()
    rec = _rec(water=104.0)
    assert strategy._breakeven_reached(rec, 100.0, "long") is True, rec


def test_reached_is_false_below_one_atr():
    _reset()
    rec = _rec(water=102.9)          # +2.9 < 1 ATR (3.0)
    assert strategy._breakeven_reached(rec, 100.0, "long") is False, rec


def test_reached_short_mirrors_below_entry():
    _reset()
    rec = _rec(direction="short", water=96.0)
    assert strategy._breakeven_reached(rec, 100.0, "short") is True, rec


def test_reached_is_false_when_feature_disabled():
    _reset()
    config.ENABLE_BREAKEVEN_LOCK = False
    rec = _rec(water=104.0)
    assert strategy._breakeven_reached(rec, 100.0, "long") is False, rec


def test_reached_is_false_without_an_entry_price():
    """Adopted positions carry entry 0.0/None; they must not claim a lock."""
    _reset()
    rec = _rec(water=104.0)
    assert strategy._breakeven_reached(rec, None, "long") is False, rec
    assert strategy._breakeven_reached(rec, 0.0, "long") is False, rec


# ── The regression: a lock-held stop-out must not read as "atr trail" ─────────

def test_long_lock_exit_is_labelled_lock():
    """THE regression (QQQ 2026-08-18). Water 104 clears +1 ATR, so the floor
    pins the stop at entry 100 while the raw trail sits down at 96.5. Price
    breaching 100 puts it BELOW entry — the arming clamp's false condition — and
    the pre-fix label read 'atr trail' every time."""
    _reset(quote_price=104.0)
    strategy._save_stops({"AAA": _rec(water=104.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 104.0, "atr": 3.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 100.0) < 0.01, ("floor must pin at entry", rec)

    strategy.tc.get_quote = _fake_quote(99.5)     # breaches 100; trail is 96.5
    seen, orig = _capture_trades()
    try:
        exited = strategy._check_and_trail_stop("AAA", 10,
                                                {"close": 99.5, "atr": 3.0},
                                                "ACCT", [])
    finally:
        strategy.log_trade = orig
    assert exited is True, "99.5 must breach the 100 floor"
    _, notes, attr = seen[0]
    assert "breakeven lock" in notes, ("regression: lock exit mislabelled", notes)
    assert "atr trail" not in notes, notes
    assert "trailing stop" in notes, "must stay parseable by _exit_reason"
    assert attr["breakeven_lock_held"] is True, attr
    assert attr["lock_caused_exit"] is True, attr
    assert attr["profit_floor_active"] is False, attr
    assert abs(attr["atr_trail_at_exit"] - 96.5) < 0.01, attr


def test_short_lock_exit_is_labelled_lock():
    """Mirror image: low_water 96 clears 1 ATR, floor pins at 100, price breaks
    back UP through entry to breach it."""
    _reset(quote_price=96.0)
    strategy._save_stops({"SSS": _rec(direction="short", water=96.0)})
    strategy._check_and_trail_stop("SSS", -10, {"close": 96.0, "atr": 3.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["SSS"]
    assert abs(rec["stop_price"] - 100.0) < 0.01, ("short floor at entry", rec)

    strategy.tc.get_quote = _fake_quote(100.5)    # breaches 100; trail is 103.5
    seen, orig = _capture_trades()
    try:
        exited = strategy._check_and_trail_stop("SSS", -10,
                                                {"close": 100.5, "atr": 3.0},
                                                "ACCT", [])
    finally:
        strategy.log_trade = orig
    assert exited is True, "100.5 must breach the 100 short floor"
    _, notes, attr = seen[0]
    assert "breakeven lock" in notes, notes
    assert attr["lock_caused_exit"] is True, attr
    assert abs(attr["atr_trail_at_exit"] - 103.5) < 0.01, attr


# ── Negative cases: what must NOT be credited to the lock ────────────────────

def test_stop_above_entry_is_the_trail_not_the_lock():
    """Reaching +1 ATR is necessary but NOT sufficient. Water 110 leaves the raw
    trail at 102.5, ABOVE entry, so the trail — not the floor — owns the stop and
    the exit belongs to the trail even though the lock trigger was met."""
    _reset(quote_price=110.0)
    strategy._save_stops({"AAA": _rec(water=110.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 110.0, "atr": 3.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 102.5) < 0.01, ("trail must win", rec)

    strategy.tc.get_quote = _fake_quote(102.0)
    seen, orig = _capture_trades()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 102.0, "atr": 3.0},
                                       "ACCT", [])
    finally:
        strategy.log_trade = orig
    _, notes, attr = seen[0]
    assert "atr trail" in notes, notes
    assert attr["breakeven_lock_held"] is False, attr
    assert attr["lock_caused_exit"] is False, attr
    assert strategy._breakeven_lock_exits == 0, strategy._breakeven_lock_exits


def test_never_reached_one_atr_is_not_a_lock():
    """A stop that happens to sit near entry without the excursion ever having
    occurred is not a lock. Guards against labelling on `stop == entry` alone."""
    _reset(quote_price=101.0)
    strategy._save_stops({"AAA": _rec(water=101.0)})     # +1.0 < 1 ATR
    strategy._check_and_trail_stop("AAA", 10, {"close": 101.0, "atr": 3.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 93.5) < 0.01, ("no floor, bare trail", rec)

    strategy.tc.get_quote = _fake_quote(93.0)
    seen, orig = _capture_trades()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 93.0, "atr": 3.0},
                                       "ACCT", [])
    finally:
        strategy.log_trade = orig
    _, notes, attr = seen[0]
    assert "atr trail" in notes, notes
    assert attr["breakeven_lock_held"] is False, attr


def test_profit_floor_outranks_the_lock():
    """When a ladder rung holds the stop, the exit is the ladder's. Both floors
    are 'active' in a loose sense; the label must name the one actually holding
    the stop, and the rung sits above entry so it is that one."""
    _reset(quote_price=130.0)
    strategy._save_stops({"AAA": _rec(atr=10.0, water=130.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 130.0, "atr": 10.0},
                                   "ACCT", [])          # +30% -> rung locks 28%
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 128.0) < 0.01, rec

    strategy.tc.get_quote = _fake_quote(124.0)
    seen, orig = _capture_trades()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 124.0, "atr": 10.0},
                                       "ACCT", [])
    finally:
        strategy.log_trade = orig
    _, notes, attr = seen[0]
    assert "profit floor" in notes, notes
    assert "breakeven lock" not in notes, notes
    assert attr["profit_floor_active"] is True, attr
    assert attr["breakeven_lock_held"] is False, ("stop is at the rung, "
                                                    "not at entry", attr)
    assert strategy._breakeven_lock_exits == 0, strategy._breakeven_lock_exits


def test_disabled_feature_never_labels_a_lock():
    _reset(quote_price=104.0)
    config.ENABLE_BREAKEVEN_LOCK = False
    strategy._save_stops({"AAA": _rec(water=104.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 104.0, "atr": 3.0},
                                   "ACCT", [])
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 96.5) < 0.01, ("no floor when off", rec)

    strategy.tc.get_quote = _fake_quote(96.0)
    seen, orig = _capture_trades()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 96.0, "atr": 3.0},
                                       "ACCT", [])
    finally:
        strategy.log_trade = orig
    _, notes, attr = seen[0]
    assert "atr trail" in notes, notes
    assert attr["breakeven_lock_held"] is False, attr
    assert strategy._breakeven_lock_exits == 0, strategy._breakeven_lock_exits


def test_crisis_floor_outranks_the_lock_at_the_same_level():
    """The guard that is latent TODAY and a silent mislabel the day
    VIX_CRISIS_SHADOW flips to False. floor_srcs collapses the crisis floor and
    the breakeven lock to the SAME level (entry), so price alone cannot separate
    them — the label has to test crisis first. Here BOTH conditions hold: water
    104 clears +1 ATR and the regime is crisis."""
    _reset(quote_price=104.0)
    config.VIX_CRISIS_SHADOW = False
    strategy._save_stops({"AAA": _rec(water=104.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 104.0, "atr": 3.0},
                                   "ACCT", [], regime="crisis")
    rec = strategy._load_stops()["AAA"]
    assert abs(rec["stop_price"] - 100.0) < 0.01, ("both floors sit at entry", rec)
    assert strategy._breakeven_reached(rec, 100.0, "long") is True, \
        "the lock condition must ALSO hold, or this proves nothing"

    strategy.tc.get_quote = _fake_quote(99.5)
    seen, orig = _capture_trades()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 99.5, "atr": 3.0},
                                       "ACCT", [], regime="crisis")
    finally:
        strategy.log_trade = orig
    _, notes, attr = seen[0]
    assert "crisis floor" in notes, notes
    assert "breakeven lock" not in notes, notes
    assert attr["breakeven_lock_held"] is False, attr
    assert attr["lock_caused_exit"] is False, attr
    assert strategy._breakeven_lock_exits == 0, ("a crisis exit must not be "
                                                 "booked as a lock exit")


def test_shadowed_crisis_still_reads_as_the_lock():
    """With VIX_CRISIS_SHADOW True (today's live config) the crisis floor is
    armed-only, so an identical setup is the lock's. Pins that the guard above
    changes nothing about current behaviour."""
    _reset(quote_price=104.0)
    strategy._save_stops({"AAA": _rec(water=104.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 104.0, "atr": 3.0},
                                   "ACCT", [], regime="crisis")
    strategy.tc.get_quote = _fake_quote(99.5)
    seen, orig = _capture_trades()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 99.5, "atr": 3.0},
                                       "ACCT", [], regime="crisis")
    finally:
        strategy.log_trade = orig
    _, notes, attr = seen[0]
    assert "breakeven lock" in notes, notes
    assert attr["breakeven_lock_held"] is True, attr


def test_stop_attr_carries_the_water_and_stop_levels():
    """water_at_exit exists nowhere else in the ledger and the verdict depends on
    it, so its absence would silently disable the report."""
    _reset(quote_price=104.0)
    strategy._save_stops({"AAA": _rec(water=104.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 104.0, "atr": 3.0},
                                   "ACCT", [])
    strategy.tc.get_quote = _fake_quote(99.5)
    seen, orig = _capture_trades()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 99.5, "atr": 3.0},
                                       "ACCT", [])
    finally:
        strategy.log_trade = orig
    _, _, attr = seen[0]
    assert abs(attr["water_at_exit"] - 104.0) < 0.01, attr
    assert abs(attr["stop_at_exit"] - 100.0) < 0.01, attr


# ── Counter and log line ─────────────────────────────────────────────────────

def test_counter_increments_once_on_a_confirmed_exit():
    _reset(quote_price=104.0)
    strategy._save_stops({"AAA": _rec(water=104.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 104.0, "atr": 3.0},
                                   "ACCT", [])
    strategy.tc.get_quote = _fake_quote(99.5)
    msgs, orig_log = _capture_logs()
    _, orig_trade = _capture_trades()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 99.5, "atr": 3.0},
                                       "ACCT", [])
    finally:
        strategy.logger.info = orig_log
        strategy.log_trade = orig_trade
    assert strategy._breakeven_lock_exits == 1, strategy._breakeven_lock_exits
    hits = [m for m in msgs if "BREAKEVEN LOCK EXIT" in m]
    assert len(hits) == 1, msgs
    assert "lock exits #1" in hits[0], hits[0]
    assert "peak given back 4.00" in hits[0], hits[0]


def test_counter_does_not_increment_when_the_exit_fails():
    """A rejected exit leaves the position OPEN. Booking a lock exit there would
    inflate the counter with trades that never closed — the same fail-safe rule
    _stop_exits follows."""
    global _order_result
    _reset(quote_price=104.0)
    strategy._save_stops({"AAA": _rec(water=104.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 104.0, "atr": 3.0},
                                   "ACCT", [])
    strategy.tc.get_quote = _fake_quote(99.5)
    _order_result = None                      # broker refuses the exit
    _, orig = _capture_trades()
    try:
        strategy._check_and_trail_stop("AAA", 10, {"close": 99.5, "atr": 3.0},
                                       "ACCT", [])
    finally:
        strategy.log_trade = orig
    assert strategy._breakeven_lock_exits == 0, strategy._breakeven_lock_exits
    assert strategy._stop_exits == 0, strategy._stop_exits


def test_arm_counter_and_exit_counter_are_distinct():
    """_breakeven_locks counts ARMING, _breakeven_lock_exits counts FIRING. A
    position that arms and never breaches must move only the first."""
    _reset(quote_price=104.0)
    strategy._save_stops({"AAA": _rec(water=104.0)})
    strategy._check_and_trail_stop("AAA", 10, {"close": 104.0, "atr": 3.0},
                                   "ACCT", [])
    assert strategy._breakeven_locks == 1, strategy._breakeven_locks
    assert strategy._breakeven_lock_exits == 0, strategy._breakeven_lock_exits


# ── Analyzer section ─────────────────────────────────────────────────────────

def _trip(**kw):
    """A lock-caused stop trip: entry 100, trail 96.5, peak 104, exited flat.
    protected = 3.5*10 = 35 per trip; given_back = 4.0*10 = 40 per trip."""
    t = {"exit_reason": "stop", "qty": 10, "entry_price": 100.0,
         "atr_trail_at_exit": 96.5, "water_at_exit": 104.0, "pnl": 0.0,
         "breakeven_lock_held": True, "lock_caused_exit": True}
    t.update(kw)
    return t


def test_analyzer_excludes_pre_fix_exits_from_every_denominator():
    """A None flag is 'unknown', never 'the lock was inactive'. Every lock exit
    before 2026-08-19 is unknown because the label could not fire."""
    trips = [_trip(breakeven_lock_held=None, lock_caused_exit=None)
             for _ in range(4)]
    st = pa._breakeven_lock_stats(trips)
    assert st["attributed"] == 0, st
    assert st["unattributed"] == 4, st
    assert st["lock_held"] == 0, st
    lines = "\n".join(pa._breakeven_lock_lines(st))
    assert "BREAKEVEN LOCK ANALYSIS" in lines
    assert "predate the attribution fix" in lines, lines
    assert "not evidence the lock never held a stop" in lines, lines


def test_analyzer_separates_caused_from_merely_held():
    trips = [_trip(), _trip(lock_caused_exit=False)]
    st = pa._breakeven_lock_stats(trips)
    assert st["attributed"] == 2, st
    assert st["lock_held"] == 2, st
    assert st["lock_caused"] == 1, st
    assert st["trail_would_fire"] == 1, st


def test_analyzer_verdict_ignores_realized_and_uses_peak_given_back():
    """The point of the inverted verdict. These three trips book a small PROFIT
    yet gave back more peak (40/trip) than they protected (35/trip), so the
    honest read is HURTING — a realized-P&L verdict would say HELPING."""
    trips = [_trip(pnl=0.10), _trip(pnl=0.20), _trip(pnl=0.10)]
    st = pa._breakeven_lock_stats(trips)
    assert st["realized_on_caused"] > 0, st
    assert abs(st["principal_protected"] - 105.0) < 0.01, st   # 3.5 * 10 * 3
    assert abs(st["peak_given_back"] - 120.0) < 0.01, st       # 4.0 * 10 * 3
    assert st["verdict"] == "HURTING", st
    lines = "\n".join(pa._breakeven_lock_lines(st))
    assert "surrendered more peak excursion" in lines, lines


def test_analyzer_helping_when_it_protects_more_than_it_surrenders():
    """Mirror: a wide trail (protected 25/trip) and a shallow peak (given back
    1/trip) is the lock doing exactly what it is for."""
    trips = [_trip(atr_trail_at_exit=75.0, water_at_exit=101.0) for _ in range(3)]
    st = pa._breakeven_lock_stats(trips)
    assert abs(st["principal_protected"] - 750.0) < 0.01, st
    assert abs(st["peak_given_back"] - 30.0) < 0.01, st
    assert st["verdict"] == "HELPING", st
    assert st["scratches_on_caused"] == 3, st
    lines = "\n".join(pa._breakeven_lock_lines(st))
    assert "near-zero is the DESIGN" in lines, lines
    assert "of notional" in lines, lines
    assert "neither figure is a counterfactual" in lines, lines


def test_analyzer_excludes_trips_with_no_water_rather_than_zeroing_them():
    """water_at_exit is unrecoverable pre-fix. Counting a missing peak as $0
    given back is the most FAVOURABLE possible reading of a trip we know nothing
    about, so those trips must be excluded and the exclusion reported."""
    trips = [_trip(), _trip(water_at_exit=None), _trip(water_at_exit=None)]
    st = pa._breakeven_lock_stats(trips)
    assert st["lock_caused"] == 3, st
    assert st["given_back_measured"] == 1, st
    assert st["given_back_excluded"] == 2, st
    assert abs(st["peak_given_back"] - 40.0) < 0.01, ("only the measurable "
                                                      "trip counts", st)
    lines = "\n".join(pa._breakeven_lock_lines(st))
    assert "2 pre-2026-08-19 with no water_at_exit" in lines, lines


def test_analyzer_refuses_a_verdict_with_no_measurable_peak():
    trips = [_trip(water_at_exit=None) for _ in range(3)]
    st = pa._breakeven_lock_stats(trips)
    assert st["verdict"] == "INSUFFICIENT DATA", st
    lines = "\n".join(pa._breakeven_lock_lines(st))
    assert "peak given back cannot be measured" in lines, lines


def test_analyzer_refuses_a_verdict_on_a_thin_sample():
    st = pa._breakeven_lock_stats([_trip(), _trip()])
    assert st["verdict"] == "INSUFFICIENT DATA", st
    lines = "\n".join(pa._breakeven_lock_lines(st))
    assert f"need {pa.MIN_LOCK_TRIPS_FOR_VERDICT}+" in lines, lines


def test_analyzer_ingests_the_new_keys_at_both_sites():
    """The section can compute perfectly and still report nothing forever if the
    keys never survive ingest. Both trip-building sites must carry them, or
    every real trip reads breakeven_lock_held=None and the report says 'no
    attributed stop exits' on a bot that is firing locks."""
    import inspect
    src = inspect.getsource(pa)
    for key in ("breakeven_lock_held", "lock_caused_exit", "water_at_exit",
                "stop_at_exit"):
        assert src.count(f'raw.get("{key}")') == 1, f"{key} missing at raw ingest"
        assert src.count(f'ev.get("{key}")') == 1, f"{key} missing at ev ingest"


def test_analyzer_section_is_wired_into_the_text_report():
    """Guards the assembly call, not just the renderer: a section that computes
    correctly but is never appended shows up as a passing test and a missing
    report."""
    lines = pa._breakeven_lock_lines(pa._breakeven_lock_stats([_trip()] * 3))
    assert lines[0] == "=== BREAKEVEN LOCK ANALYSIS ===", lines[0]
    import inspect
    src = inspect.getsource(pa)
    assert '_breakeven_lock_lines(report.get("breakeven_lock"))' in src, \
        "section computed but not appended to the text report"
    assert '"breakeven_lock": _breakeven_lock_stats(closed)' in src, \
        "section not added to the JSON report dict"


if __name__ == "__main__":
    strategy._STOPS_PATH = os.path.join(tempfile.mkdtemp(), "stop_prices.json")
    _orig = (strategy.tc.place_equity_order, strategy.tc.get_quote,
             strategy.tc.cancel_order, strategy.tc.get_order_outcome,
             strategy.log_trade, config.ENABLE_BREAKEVEN_LOCK,
             config.BREAKEVEN_LOCK_ATR, config.ENABLE_PROFIT_FLOOR,
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
         strategy.log_trade, config.ENABLE_BREAKEVEN_LOCK,
         config.BREAKEVEN_LOCK_ATR, config.ENABLE_PROFIT_FLOOR,
         config.ENABLE_PROFIT_FLOOR_BROKER_RAISE,
         config.ENABLE_BROKER_STOP_FLOOR) = _orig
