"""
Unit tests for _record_broker_floor_fill: the path that books an exit the
BROKER made. NO network.

ARM 2026-09-24 gapped through both stops at the open. The resting GTC floor
filled 90 @ 318.74 before the bot's first cycle, and reconcile_stops then
pruned the record with one INFO line. There was no exit line, no trades.log row
and no alert, so the ledger never saw the leg. These tests pin down the branches
of the fix: a filled floor is booked and alerted exactly once, a floor that is
still resting gets cancelled, and a failed or ambiguous lookup never fabricates
a trade.

Stubs tc.get_order_outcome / tc.get_order / tc.cancel_order and
strategy.log_trade (under _log_exit_trade), and
redirects _STOPS_PATH to a temp file.

Run:  python3 test_broker_floor_fill.py
"""

import logging
import os
import tempfile

import _testlib
import config
import strategy

_logged = []          # every log_trade call as (args, kwargs)
_cancelled = []
_outcome = {}         # what get_order_outcome returns for the floor id
_lookups = []         # order ids get_order_outcome was asked about
_critical = []        # CRITICAL messages, rendered


class _CriticalCatcher(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.CRITICAL:
            _critical.append(record.getMessage())


_catcher = _CriticalCatcher()


def _fake_outcome(account_id, order_id, expected_cancel=False):
    _lookups.append(order_id)
    return dict(_outcome)


def _fake_cancel(account_id, order_id):
    _cancelled.append(order_id)
    return True


def _fake_log_trade(*a, **k):
    _logged.append((a, k))


def _fake_get_order(account_id, order_id):
    return _outcome.get("fill_price")


_ORIG = (strategy.tc.get_order_outcome, strategy.tc.cancel_order,
         strategy.tc.get_order, strategy.log_trade)


def teardown_function(_fn):
    """pytest hook (no pytest import needed). Every test patches module globals
    that later test FILES share in the same process, so put them back."""
    (strategy.tc.get_order_outcome, strategy.tc.cancel_order,
     strategy.tc.get_order, strategy.log_trade) = _ORIG
    strategy.logger.removeHandler(_catcher)


# ARM's record as it stood at the 09-23 close.
_ARM = {"direction": "long", "entry_price": 266.73, "stop_price": 323.56,
        "high_water": 335.66, "atr_at_entry": 16.13, "atr_mult": 1.25,
        "broker_order_id": "972255862", "broker_floor_price": 319.53,
        "water_floor_active": True, "water_floor_price": 323.56,
        "profit_floor_active": False, "opened": "2026-09-18",
        "bootstrapped": False, "profit_taken": True}


def _reset(state="filled", fill=318.74, qty=90.0):
    _logged.clear(); _cancelled.clear(); _lookups.clear(); _critical.clear()
    _outcome.clear()
    _outcome.update({"state": state, "fill_price": fill if state == "filled" else None,
                     "filled_qty": qty if state == "filled" else None,
                     "reason": None, "status": "Filled"})
    strategy._floor_fires = 0
    strategy._floors_cancelled = 0
    strategy.tc.get_order_outcome = _fake_outcome
    strategy.tc.cancel_order = _fake_cancel
    strategy.tc.get_order = _fake_get_order     # _log_exit_trade's fill resolve
    strategy.log_trade = _fake_log_trade
    if _catcher not in strategy.logger.handlers:
        strategy.logger.addHandler(_catcher)
    strategy._save_stops({"ARM": dict(_ARM),
                          "SPY": {"direction": "long", "entry_price": 773.87,
                                  "stop_price": 754.86, "high_water": 774.9,
                                  "broker_order_id": "SPYFLOOR",
                                  "broker_floor_price": 749.82}})


_POSITIONS = [{"symbol": "SPY", "quantity": 61}]      # ARM gone, SPY held


def test_filled_floor_books_trade_and_alerts():
    _reset()
    strategy.reconcile_stops(_POSITIONS, "SIM")
    assert _lookups == ["972255862"], "only the vanished name's floor is looked up"
    assert len(_logged) == 1, "exactly one trades.log row"
    a, k = _logged[0]
    action, symbol, qty, price, order_type, order_id, notes = a
    assert (action, symbol, qty) == ("SELL", "ARM", 90)
    assert price == 323.56, "price = the bot stop, the level the bot meant to exit at"
    assert order_id == "972255862", "keyed by the broker order id so the ledger dedups"
    assert "broker gtc floor" in notes.lower(), "note must hit the broker_floor bucket"
    assert k["fill_price"] == 318.74 and k["signal_price"] == 323.56
    assert abs(k["slippage"] - 4.82) < 1e-9, "positive = worse than the bot stop"
    assert k["stop_attr"]["water_caused_exit"] is False, "the broker caused it"
    assert strategy._floor_fires == 1
    assert _critical == ["BROKER GTC FLOOR FIRED on ARM: fill 318.74, bot stop was "
                         "323.56, slippage +4.82 pts. Bot exit logic did not fire first."]
    stops = strategy._load_stops()
    assert "ARM" not in stops and "SPY" in stops


def test_second_cycle_does_not_rebook():
    _reset()
    strategy.reconcile_stops(_POSITIONS, "SIM")
    strategy.reconcile_stops(_POSITIONS, "SIM")
    assert len(_logged) == 1 and strategy._floor_fires == 1, \
        "record is gone after the first prune, so nothing to re-book"


def test_short_slippage_sign():
    _reset(fill=105.0, qty=10.0)
    strategy._save_stops({"XYZ": {"direction": "short", "entry_price": 110.0,
                                  "stop_price": 104.0, "low_water": 98.0,
                                  "broker_order_id": "F1",
                                  "broker_floor_price": 106.0}})
    strategy.reconcile_stops(_POSITIONS, "SIM")
    a, k = _logged[0]
    assert a[0] == "BUY_TO_COVER" and a[2] == 10
    assert abs(k["slippage"] - 1.0) < 1e-9, "short: fill above stop is worse"
    assert k["stop_attr"]["water_at_exit"] == 98.0


def test_dead_floor_books_nothing():
    """Cancelled floor: something else closed the position. Not a broker exit."""
    _reset(state="dead")
    strategy.reconcile_stops(_POSITIONS, "SIM")
    assert _logged == [] and _critical == [] and strategy._floor_fires == 0
    assert "ARM" not in strategy._load_stops(), "still pruned"


def test_unknown_lookup_never_fabricates():
    _reset(state="unknown")
    strategy.reconcile_stops(_POSITIONS, "SIM")
    assert _logged == [] and strategy._floor_fires == 0, \
        "a failed lookup must not be read as a fill"


def test_working_floor_is_cancelled():
    """A floor resting behind a position we no longer hold would open a fresh one."""
    _reset(state="working")
    strategy.reconcile_stops(_POSITIONS, "SIM")
    assert _cancelled == ["972255862"]
    assert _logged == [] and len(_critical) == 1


def test_unreadable_qty_alerts_but_writes_no_row():
    _reset(qty=None)
    strategy.reconcile_stops(_POSITIONS, "SIM")
    assert _logged == [], "the ledger pairs by quantity, so never guess one"
    assert len(_critical) == 1 and strategy._floor_fires == 1


def test_no_account_id_is_the_old_silent_prune():
    _reset()
    strategy.reconcile_stops(_POSITIONS)
    assert _lookups == [] and _logged == []
    assert "ARM" not in strategy._load_stops()


def test_no_floor_id_skips_lookup():
    _reset()
    rec = dict(_ARM); rec.pop("broker_order_id")
    strategy._save_stops({"ARM": rec})
    strategy.reconcile_stops(_POSITIONS, "SIM")
    assert _lookups == [] and _logged == []


# ── Standalone runner ─────────────────────────────────────────────────────────
# MUST STAY LAST IN THE FILE: it collects globals() at call time, so a test
# defined below it silently does not run. Append new tests ABOVE.
if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="floor_fill_test_")
    strategy._STOPS_PATH = _testlib.assert_disposable(
        os.path.join(_tmpdir, "stop_prices.json"))
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
        finally:
            teardown_function(t)
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"All {passed} assertions passed.")
