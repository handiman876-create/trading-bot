"""
Unit tests for the broker-native stop FLOOR — NO network.

The floor is a single GTC stop placed at entry and never moved. Its whole
purpose is to sit BEYOND the bot's ATR stop so the bot always exits first; it
only ever fires if the bot is dead or price gaps clean through. The invariant
these tests defend is therefore not "the floor is at price X" but "the floor is
further from the market than the bot's stop, for longs and shorts alike".

Stubs tc.place_equity_order / tc.cancel_order / tc.get_working_orders and
redirects _STOPS_PATH to a temp file, so nothing here touches the API or the
live data/stop_prices.json.

Run:  python3 test_broker_floor.py
"""

import os
import tempfile

import _testlib
import config
import strategy

# ── Test doubles ──────────────────────────────────────────────────────────────
_placed = []          # every place_equity_order call, as a kwargs-bearing dict
_cancelled = []       # order ids passed to cancel_order
_cancel_ok = True     # flip to False to simulate a broker refusing the cancel
_working = []         # what get_working_orders reports as resting
_working_fails = False


def _fake_place(account_id, symbol, side, qty, order_type="market",
                duration="day", limit_price=None, stop_price=None):
    _placed.append({"symbol": symbol, "side": side, "qty": qty,
                    "order_type": order_type, "duration": duration,
                    "stop_price": stop_price})
    return {"order": {"id": f"FLOOR{len(_placed)}"}}


def _fake_cancel(account_id, order_id):
    _cancelled.append(order_id)
    return _cancel_ok


def _fake_working(account_id):
    return None if _working_fails else list(_working)


def _reset(enabled=True, buffer_=1.2):
    global _cancel_ok, _working_fails
    _placed.clear()
    _cancelled.clear()
    _working.clear()
    _cancel_ok = True
    _working_fails = False
    config.ENABLE_BROKER_STOP_FLOOR = enabled
    config.BROKER_STOP_FLOOR_BUFFER = buffer_
    strategy._floors_placed = 0
    strategy._floors_cancelled = 0
    strategy._floor_orphans = 0
    strategy._floor_cancel_failures = 0
    strategy._floors_reconciled = False
    strategy.tc.place_equity_order = _fake_place
    strategy.tc.cancel_order = _fake_cancel
    strategy.tc.get_working_orders = _fake_working
    _testlib.safe_remove(strategy._STOPS_PATH)


def _rec(entry, atr, mult, direction, stop):
    return {"entry_price": entry, "atr_at_entry": atr, "atr_mult": mult,
            "direction": direction, "stop_price": stop}


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_globals():
        """These tests flip module-level config flags and swap tc functions, and
        pytest runs every module in ONE process. Leaking ENABLE_BROKER_STOP_FLOOR
        as True would make _place_broker_floor fire inside other modules' tests,
        whose place_equity_order stubs take no kwargs — a TypeError in a file
        that never opted into this feature. Restore unconditionally."""
        saved_cfg = (config.ENABLE_BROKER_STOP_FLOOR,
                     config.BROKER_STOP_FLOOR_BUFFER,
                     config.ENABLE_PROFIT_TAKING)
        saved_tc = (getattr(strategy.tc, "place_equity_order", None),
                    getattr(strategy.tc, "cancel_order", None),
                    getattr(strategy.tc, "get_working_orders", None))
        yield
        (config.ENABLE_BROKER_STOP_FLOOR, config.BROKER_STOP_FLOOR_BUFFER,
         config.ENABLE_PROFIT_TAKING) = saved_cfg
        (strategy.tc.place_equity_order, strategy.tc.cancel_order,
         strategy.tc.get_working_orders) = saved_tc
except ImportError:                       # direct `python3 test_broker_floor.py`
    pass


# ── Floor arithmetic ──────────────────────────────────────────────────────────

def test_long_floor_is_below_entry_by_buffered_atr():
    """Real GOOGL numbers: 375.67 - (2.5 * 12.5477 * 1.2) = 338.03."""
    _reset()
    floor = strategy._floor_price(375.67, 12.5477, 2.5, "long")
    assert abs(floor - 338.03) < 0.01, floor


def test_short_floor_is_above_entry_by_buffered_atr():
    """Real AAPL numbers: 306.66 + (2.5 * 9.3428 * 1.2) = 334.69. A short's
    floor must sit ABOVE entry — the mirror of the long case, not a sign bug."""
    _reset()
    floor = strategy._floor_price(306.66, 9.3428, 2.5, "short")
    assert abs(floor - 334.69) < 0.01, floor


def test_buffer_is_configurable():
    """The buffer scales the distance and nothing else."""
    _reset(buffer_=1.0)
    at_1x = strategy._floor_price(100.0, 4.0, 2.5, "long")
    assert abs(at_1x - 90.0) < 1e-9, at_1x          # 100 - 2.5*4*1.0
    config.BROKER_STOP_FLOOR_BUFFER = 2.0
    at_2x = strategy._floor_price(100.0, 4.0, 2.5, "long")
    assert abs(at_2x - 80.0) < 1e-9, at_2x          # 100 - 2.5*4*2.0


def test_floor_sits_beyond_bot_stop_for_both_directions():
    """THE invariant. The bot's stop starts at entry -/+ mult*atr and ratchets
    AWAY from the floor, so if the floor is beyond the INITIAL stop it can never
    be overtaken. A floor inside the bot's stop would fire first and turn every
    exit into an untrailed market order."""
    _reset()
    for entry, atr, mult in ((375.67, 12.5477, 2.5), (86.99, 7.6699, 1.5),
                             (717.26, 15.3635, 2.5), (397.86, 11.8223, 2.0)):
        bot_stop = entry - mult * atr
        floor = strategy._floor_price(entry, atr, mult, "long")
        assert floor < bot_stop, f"long floor {floor} not below stop {bot_stop}"
    for entry, atr, mult in ((306.66, 9.3428, 2.5), (100.0, 3.0, 1.5)):
        bot_stop = entry + mult * atr
        floor = strategy._floor_price(entry, atr, mult, "short")
        assert floor > bot_stop, f"short floor {floor} not above stop {bot_stop}"


# ── Placement ─────────────────────────────────────────────────────────────────

def test_arm_places_gtc_stop_and_records_order_id():
    _reset()
    strategy._arm_stop_on_entry("TEST", 100.0, 4.0, regime="risk_on",
                                qty=10, account_id="ACCT")
    assert len(_placed) == 1, _placed
    o = _placed[0]
    assert o["order_type"] == "stop", o
    assert o["duration"] == "gtc", o
    assert o["side"] == "sell", o
    assert o["qty"] == 10, o

    rec = strategy._load_stops()["TEST"]
    expected = strategy._floor_price(rec["entry_price"], rec["atr_at_entry"],
                                     rec["atr_mult"], "long")
    assert abs(o["stop_price"] - expected) < 1e-9, (o, expected)
    assert rec["broker_order_id"] == "FLOOR1", rec
    assert abs(rec["broker_floor_price"] - round(expected, 2)) < 1e-9, rec
    assert rec["broker_floor_price"] < rec["stop_price"], rec
    assert strategy._floors_placed == 1


def test_short_arm_places_buy_to_cover_stop():
    _reset()
    strategy._arm_stop_on_entry("TSHORT", 100.0, 4.0, direction="short",
                                regime="risk_on", qty=7, account_id="ACCT")
    assert len(_placed) == 1, _placed
    assert _placed[0]["side"] == "buy_to_cover", _placed[0]
    rec = strategy._load_stops()["TSHORT"]
    assert rec["broker_floor_price"] > rec["stop_price"], rec


def test_disabled_flag_places_nothing():
    """The whole feature must be inert while the flag is False."""
    _reset(enabled=False)
    strategy._arm_stop_on_entry("TEST", 100.0, 4.0, regime="risk_on",
                                qty=10, account_id="ACCT")
    assert _placed == [], _placed
    rec = strategy._load_stops()["TEST"]
    assert "broker_order_id" not in rec, rec
    assert rec["stop_price"], "bot-managed stop must still be armed"


def test_failed_placement_leaves_position_armed_but_unfloored():
    """A broker rejection must not block the position from being stop-managed."""
    _reset()
    strategy.tc.place_equity_order = lambda *a, **k: None
    strategy._arm_stop_on_entry("TEST", 100.0, 4.0, regime="risk_on",
                                qty=10, account_id="ACCT")
    rec = strategy._load_stops()["TEST"]
    assert "broker_order_id" not in rec, rec
    assert rec["stop_price"], rec
    assert strategy._floors_placed == 0


# ── Teardown ──────────────────────────────────────────────────────────────────

def test_release_cancels_floor_and_drops_record():
    _reset()
    strategy._arm_stop_on_entry("TEST", 100.0, 4.0, regime="risk_on",
                                qty=10, account_id="ACCT")
    strategy._release_stop("TEST", "ACCT")
    assert _cancelled == ["FLOOR1"], _cancelled
    assert "TEST" not in strategy._load_stops()
    assert strategy._floors_cancelled == 1


def test_release_without_floor_is_a_noop_cancel():
    """Records predating the feature carry no order id — must not blow up."""
    _reset(enabled=False)
    strategy._arm_stop_on_entry("TEST", 100.0, 4.0, regime="risk_on",
                                qty=10, account_id="ACCT")
    strategy._release_stop("TEST", "ACCT")
    assert _cancelled == [], _cancelled
    assert "TEST" not in strategy._load_stops()


def test_failed_cancel_is_counted_and_reported():
    """A cancel that fails means a live orphan — it must be loud, not silent."""
    global _cancel_ok
    _reset()
    strategy._arm_stop_on_entry("TEST", 100.0, 4.0, regime="risk_on",
                                qty=10, account_id="ACCT")
    _cancel_ok = False
    ok = strategy._cancel_broker_floor("TEST", strategy._load_stops()["TEST"],
                                       "ACCT")
    assert ok is False
    assert strategy._floor_cancel_failures == 1


# ── Profit take resizes the floor ─────────────────────────────────────────────

def test_profit_take_resizes_floor_to_remaining_shares():
    """The floor is sized for the FULL position. After a partial sell it would
    sell more than we hold and open a short on the difference, so the quantity
    (never the price) is re-placed."""
    _reset()
    config.ENABLE_PROFIT_TAKING = True
    strategy._arm_stop_on_entry("TEST", 100.0, 4.0, regime="risk_on",
                                qty=10, account_id="ACCT")
    first_floor = _placed[0]["stop_price"]
    _placed.clear()
    sig = {"close": 100.0 * (1 + config.PROFIT_TAKE_PCT) + 1,
           "rsi": config.PROFIT_TAKE_RSI_MIN + 5, "atr": 4.0}
    fired = strategy._maybe_take_profit("TEST", 10, sig, "ACCT")
    assert fired is True
    sells = [p for p in _placed if p["order_type"] == "market"]
    floors = [p for p in _placed if p["order_type"] == "stop"]
    assert len(sells) == 1 and len(floors) == 1, _placed
    assert _cancelled == ["FLOOR1"], _cancelled
    remaining = 10 - sells[0]["qty"]
    assert floors[0]["qty"] == remaining, (floors, remaining)
    # Price unchanged: only the size moved.
    assert abs(floors[0]["stop_price"] - first_floor) < 1e-9, (floors, first_floor)


# ── Startup reconcile ─────────────────────────────────────────────────────────

def test_reconcile_cancels_orphaned_floor():
    """A floor we placed, with no position behind it, must be cancelled."""
    _reset()
    stops = {"GONE": _rec(100.0, 4.0, 2.5, "long", 90.0)}
    stops["GONE"]["broker_order_id"] = "OLD1"
    strategy._save_stops(stops)
    _working.append({"order_id": "OLD1", "symbol": "GONE",
                     "order_type": "StopMarket", "stop_price": 88.0})
    strategy.reconcile_broker_floors([{"symbol": "OTHER", "quantity": 5}], "ACCT")
    assert _cancelled == ["OLD1"], _cancelled
    assert strategy._floor_orphans == 1


def test_reconcile_leaves_foreign_orders_alone():
    """An order we did not place is not ours to cancel."""
    _reset()
    strategy._save_stops({})
    _working.append({"order_id": "MANUAL9", "symbol": "AAPL",
                     "order_type": "StopMarket", "stop_price": 1.0})
    strategy.reconcile_broker_floors([], "ACCT")
    assert _cancelled == [], _cancelled


def test_reconcile_rearms_position_whose_floor_vanished():
    """Broker says the order is gone but we still hold the shares -> re-arm."""
    _reset()
    stops = {"HELD": _rec(100.0, 4.0, 2.5, "long", 90.0)}
    stops["HELD"]["broker_order_id"] = "DEAD1"
    strategy._save_stops(stops)
    # _working is empty: DEAD1 is not resting anymore.
    strategy.reconcile_broker_floors([{"symbol": "HELD", "quantity": 10}], "ACCT")
    assert len(_placed) == 1, _placed
    assert _placed[0]["order_type"] == "stop" and _placed[0]["qty"] == 10, _placed
    rec = strategy._load_stops()["HELD"]
    assert rec["broker_order_id"] != "DEAD1", rec


def test_reconcile_skips_position_already_protected():
    _reset()
    stops = {"HELD": _rec(100.0, 4.0, 2.5, "long", 90.0)}
    stops["HELD"]["broker_order_id"] = "LIVE1"
    strategy._save_stops(stops)
    _working.append({"order_id": "LIVE1", "symbol": "HELD",
                     "order_type": "StopMarket", "stop_price": 88.0})
    strategy.reconcile_broker_floors([{"symbol": "HELD", "quantity": 10}], "ACCT")
    assert _placed == [], _placed
    assert _cancelled == [], _cancelled


def test_reconcile_bails_when_working_order_fetch_fails():
    """None means 'unknown', not 'nothing resting'. Acting on it would cancel
    nothing while re-arming duplicates on top of live floors."""
    global _working_fails
    _reset()
    stops = {"HELD": _rec(100.0, 4.0, 2.5, "long", 90.0)}
    stops["HELD"]["broker_order_id"] = "LIVE1"
    strategy._save_stops(stops)
    _working_fails = True
    strategy.reconcile_broker_floors([{"symbol": "HELD", "quantity": 10}], "ACCT")
    assert _placed == [] and _cancelled == [], (_placed, _cancelled)
    assert strategy._floors_reconciled is False, "must retry, not latch, on failure"


def test_reconcile_runs_once_per_process():
    _reset()
    strategy._save_stops({})
    strategy.reconcile_broker_floors([], "ACCT")
    assert strategy._floors_reconciled is True
    _working.append({"order_id": "X", "symbol": "Y", "order_type": "StopMarket"})
    strategy.reconcile_broker_floors([], "ACCT")   # second call: no-op
    assert _cancelled == [], _cancelled


def test_reconcile_disabled_flag_does_nothing():
    _reset(enabled=False)
    stops = {"GONE": _rec(100.0, 4.0, 2.5, "long", 90.0)}
    stops["GONE"]["broker_order_id"] = "OLD1"
    strategy._save_stops(stops)
    _working.append({"order_id": "OLD1", "symbol": "GONE",
                     "order_type": "StopMarket"})
    strategy.reconcile_broker_floors([], "ACCT")
    assert _cancelled == [], _cancelled


if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="floor_test_")
    strategy._STOPS_PATH = os.path.join(_tmpdir, "stop_prices.json")
    _orig = (strategy.tc.place_equity_order, strategy.log_trade,
             config.ENABLE_BROKER_STOP_FLOOR, config.BROKER_STOP_FLOOR_BUFFER)
    strategy.log_trade = lambda *a, **k: None
    strategy._log_exit_trade = lambda *a, **k: None
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
        (strategy.tc.place_equity_order, strategy.log_trade,
         config.ENABLE_BROKER_STOP_FLOOR, config.BROKER_STOP_FLOOR_BUFFER) = _orig
