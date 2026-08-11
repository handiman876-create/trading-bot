"""
Unit tests for REJECTED exits and the floor-first exit ordering — NO network.

WHY THIS EXISTS — the GOOGL incident, 2026-08-11 13:38 UTC:

  1. The ATR trailing stop fired on GOOGL (long x127, stop 352.67).
  2. The exit was SUBMITTED FIRST and the broker floor cancelled second. A
     resting GTC stop reserves every share it covers, so the market sell was
     refused outright: "You are long 127 shares with 127 remaining on sell
     orders!". This was not a race that could be won — the ordering guaranteed
     it, and it had never fired before because this was the first stop exit
     since the floor feature (7d088b4) landed.
  3. get_order() decided filled-vs-pending purely on "is there a FilledPrice?",
     a test a REJECTED order fails for the same reason a slow one does. It
     called the rejection "still pending" and returned None.
  4. The caller read that None as "it filled but we could not read the price",
     logged a completed SELL of 127 shares that never happened at a price 2.55
     better than reality, and released the trailing stop.
  5. The floor was already cancelled and nothing rolled it back, so the position
     sat OPEN and completely UNPROTECTED for three hours, drifting from
     -$2,948 to -$3,272 before a human noticed.

Every test below pins one link in that chain. The invariant they defend
together: an exit the broker did not execute must change NOTHING — no trade
logged, no stop record dropped, no protection left down.

Run:  python3 test_exit_rejection.py
"""

import tempfile

import _testlib
import config
import strategy
import tradestation_client as tc


# ── Response builders ─────────────────────────────────────────────────────────

def _resp(status_desc, status=None, filled_price=None, reject_reason=None):
    """Shape a TradeStation GetOrders response. Numerics are STRINGS, as the API
    returns them — a rejected order really does carry FilledPrice '0'."""
    o = {"OrderID": "X1", "StatusDescription": status_desc, "Legs": []}
    if status is not None:
        o["Status"] = status
    if filled_price is not None:
        o["FilledPrice"] = str(filled_price)
    if reject_reason is not None:
        o["RejectReason"] = reject_reason
    return {"Orders": [o]}


# The verbatim broker response that started all of this.
_GOOGL_REJECTION = _resp("Rejected", status="REJ", filled_price=0,
                         reject_reason="You are long 127 shares with 127 "
                                       "remaining on sell orders!")


# ── Exit ordering and rollback ────────────────────────────────────────────────

_events = []          # ordered log of every broker interaction
_outcome = {}         # what get_order_outcome reports, per id prefix


_STUBBED = ("place_equity_order", "cancel_order", "get_order_outcome", "get_order")


def _snapshot():
    """Save every tc attribute these tests replace.

    `strategy.tc` IS the tradestation_client module object, not a copy, so
    assigning through it rewrites the real module for every test that runs
    afterwards in the same pytest process. Leaking a stubbed get_order_outcome
    broke 19 unrelated tests across four files the first time this ran.
    """
    return {name: getattr(tc, name, None) for name in _STUBBED} | {
        "flag": config.ENABLE_BROKER_STOP_FLOOR}


def _restore(saved):
    for name in _STUBBED:
        if saved[name] is not None:
            setattr(tc, name, saved[name])
    config.ENABLE_BROKER_STOP_FLOOR = saved["flag"]


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_stubs():
        saved = _snapshot()
        yield
        _restore(saved)
except ImportError:                       # direct `python3 test_exit_rejection.py`
    pass


def _fake_place(account_id, symbol, side, qty, order_type="market",
                duration="day", limit_price=None, stop_price=None):
    kind = "floor" if order_type == "stop" else "exit"
    _events.append((f"place_{kind}", symbol, qty))
    return {"order": {"id": f"{kind.upper()}{len(_events)}"}}


def _fake_cancel(account_id, order_id):
    _events.append(("cancel", order_id, None))
    return True


def _fake_outcome(account_id, order_id):
    key = "floor" if str(order_id).startswith("FLOOR") else "exit"
    return dict(_outcome[key])


def _setup(exit_state="executed", floor_state="dead"):
    _events.clear()
    _outcome["floor"] = {"state": floor_state, "fill_price": 90.0,
                         "reason": None, "status": floor_state}
    _outcome["exit"] = {
        "state": "dead" if exit_state == "rejected" else "filled",
        "fill_price": None if exit_state == "rejected" else 100.0,
        "reason": "You are long 127 shares with 127 remaining on sell orders!"
                  if exit_state == "rejected" else None,
        "status": "Rejected" if exit_state == "rejected" else "Filled"}
    config.ENABLE_BROKER_STOP_FLOOR = True
    strategy._exit_rejections = 0
    strategy._floor_rearms = 0
    strategy._floor_clear_stuck = 0
    strategy.tc.place_equity_order = _fake_place
    strategy.tc.cancel_order = _fake_cancel
    strategy.tc.get_order_outcome = _fake_outcome
    strategy.tc.time = tc.time
    return {"entry_price": 100.0, "atr_at_entry": 4.0, "atr_mult": 2.5,
            "direction": "long", "stop_price": 90.0,
            "broker_order_id": "FLOOR0"}


def test_floor_is_cancelled_before_the_exit_is_submitted():
    """THE ordering fix. The floor reserves the shares the exit needs, so the
    cancel must land first. Reversed — as it was — the broker refuses the exit
    every single time, deterministically."""
    rec = _setup()
    order_id, status = strategy._submit_exit_order("GOOGL", "sell", 127,
                                                   "ACCT", rec)
    assert status == "executed", status
    kinds = [e[0] for e in _events]
    assert kinds.index("cancel") < kinds.index("place_exit"), _events


def test_rejected_exit_reports_failure_and_rearms_the_floor():
    """A refused exit must leave the position PROTECTED. GOOGL's floor was
    already down when the rejection came back and nothing put it back."""
    rec = _setup(exit_state="rejected")
    order_id, status = strategy._submit_exit_order("GOOGL", "sell", 127,
                                                   "ACCT", rec)
    assert status == "failed", status
    assert order_id is None
    assert strategy._exit_rejections == 1
    assert strategy._floor_rearms == 1
    assert [e[0] for e in _events].count("place_floor") == 1, _events
    assert rec.get("broker_order_id"), "floor id must be restored on the record"


def test_stuck_floor_blocks_the_exit_and_keeps_the_order_id():
    """If we cannot confirm the floor is gone we must not submit — but we also
    must not drop the order id, or the floor is orphaned beyond recall. Keeping
    it means the next cycle retries the whole cancel-and-confirm."""
    rec = _setup(floor_state="working")
    orig_sleep = strategy.time.sleep
    strategy.time.sleep = lambda s: None
    try:
        order_id, status = strategy._submit_exit_order("GOOGL", "sell", 127,
                                                       "ACCT", rec)
    finally:
        strategy.time.sleep = orig_sleep
    assert status == "failed", status
    assert "place_exit" not in [e[0] for e in _events], _events
    assert rec["broker_order_id"] == "FLOOR0", rec
    assert strategy._floor_clear_stuck == 1


def test_floor_that_filled_during_exit_stops_a_second_sell():
    """If the floor executed, the position is already flat. Selling again would
    open an unintended SHORT — the exact hazard _cancel_broker_floor exists to
    prevent, arriving from the other direction."""
    rec = _setup(floor_state="filled")
    order_id, status = strategy._submit_exit_order("GOOGL", "sell", 127,
                                                   "ACCT", rec)
    assert status == "floor_filled", status
    assert "place_exit" not in [e[0] for e in _events], _events


def test_unconfirmable_exit_is_assumed_done_not_retried():
    """An exit we cannot confirm must NOT be resubmitted: double-selling into a
    fill is worse than a stale record, which the next cycle reconciles."""
    rec = _setup()
    _outcome["exit"] = {"state": "unknown", "fill_price": None,
                        "reason": "503", "status": None}
    order_id, status = strategy._submit_exit_order("GOOGL", "sell", 127,
                                                   "ACCT", rec)
    assert status == "executed", status


# ── End-to-end: a rejected stop exit must change nothing ──────────────────────

def test_rejected_stop_exit_keeps_the_stop_record():
    """The whole incident in one assertion. A refused exit must leave the stop
    record intact — GOOGL's was dropped, and the next cycle re-bootstrapped the
    position with its high-water mark reset (384.04 -> 375.67) and its stop
    loosened from 352.67 to 346.17, so the exit could not even re-fire."""
    rec = _setup(exit_state="rejected")
    full = dict(rec, high_water=100.0, low_water=100.0)
    strategy._save_stops({"GOOGL": full})
    sig = {"close": 89.0, "atr": 4.0, "rsi": 40.0}
    fired = strategy._check_and_trail_stop("GOOGL", 127, sig, "ACCT",
                                           [{"symbol": "GOOGL", "quantity": 127}],
                                           "risk_on")
    assert fired is False, "a refused exit must not report success"
    assert "GOOGL" in strategy._load_stops(), "stop record was dropped"


if __name__ == "__main__":
    strategy._STOPS_PATH = _testlib.assert_disposable(
        tempfile.mkdtemp()) + "/stop_prices.json"
    import sys
    mod = sys.modules[__name__]
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        saved = _snapshot()            # same isolation pytest's fixture gives
        try:
            getattr(mod, name)()
        finally:
            _restore(saved)
        print("ok", name)
    print("all passed")
