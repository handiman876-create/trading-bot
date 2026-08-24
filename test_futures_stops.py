"""
Unit tests for futures stop protection — NO network.

Covers the four things that had to land together for futures to be
stop-managed at all:

  1. STOP_PRICE_FILE is per-process, so the two bots cannot prune each other's
     records (the hazard main.py used to dodge by returning early).
  2. Order routing dispatches on instrument, so the shared stop machinery
     reaches the futures endpoint instead of the equity one.
  3. _bootstrap_stop refuses cost_basis for futures, where TotalCost is MARGIN
     and would arm an instantly-breached stop.
  4. evaluate_future arms on the real fill, trails each cycle, and exits
     floor-first.

Stubs strategy.tc entirely and redirects strategy._STOPS_PATH to a temp file, so
nothing here touches the live data/stop_prices*.json or places an order.

Run:  python3 test_futures_stops.py
"""

import json
import os
import subprocess
import sys
import tempfile

import _testlib
import config
import strategy

# pytest is not installed system-wide, and `python3 test_futures_stops.py` is a
# supported way to run this file (see _testlib's docstring). A hard import would
# make the standalone path die on line 1 — so the fixture below is registered
# only when pytest is actually present, and the __main__ block does the same
# restore by hand.
try:
    import pytest
except ModuleNotFoundError:
    pytest = None

# ── Stub containment ──────────────────────────────────────────────────────────
# _reset() assigns the test doubles onto the SHARED strategy.tc module object, so
# without this they outlive the module and every test file that sorts after this
# one inherits them. That is not theoretical: a get_order stub returning a dict
# instead of a float took out 38 tests across test_stops, test_profit_take,
# test_sentiment and test_vix_regime, and each looked like an unrelated
# TypeError deep in _resolve_fill. The other test modules get away with bare
# assignment because each one re-stubs what it uses in its own _reset(); this
# module stubs MORE of tc than they do, so it has to clean up after itself.
_TC_ATTRS = ("place_futures_order", "place_equity_order", "cancel_order",
             "get_order", "get_order_outcome", "get_quote",
             "get_working_orders")


if pytest is not None:
    @pytest.fixture(autouse=True)
    def _restore_tc():
        saved = {a: getattr(strategy.tc, a, None) for a in _TC_ATTRS}
        floor = config.ENABLE_BROKER_STOP_FLOOR
        yield
        for a, v in saved.items():
            setattr(strategy.tc, a, v)
        config.ENABLE_BROKER_STOP_FLOOR = floor

# Real measured values (2026-08-24), so the band/width assertions below are
# testing the actual production geometry rather than invented numbers.
NQ_ATR = 533.96          # ATR/price = 1.83% -> low-vol band (threshold 2%)
NQ_FILL = 29546.50       # the true NQU26 entry fill, from futures_trades.log
NQ_MARGIN = 43972.00     # what TradeStation reports as TotalCost for it

# ── Test doubles ──────────────────────────────────────────────────────────────
_futures_orders = []     # (symbol, side, qty, order_type, duration, stop_price)
_equity_orders = []      # (symbol, side, qty, order_type, duration, stop_price)
_cancelled = []
_outcomes = {}           # order_id -> state fed to get_order_outcome
_order_fails = False     # True = the broker refuses every order
_next_id = [0]

# Each order gets a DISTINCT id. Reusing one id across the floor and the exit
# makes _wait_floor_clear read the exit's outcome as the floor's, so a working
# floor-first exit reports "floor_filled" and the test looks like a real bug.


def _new_id():
    _next_id[0] += 1
    return f"F{_next_id[0]}"


def _result():
    return None if _order_fails else {"order": {"id": _new_id()}}


def _fake_futures_order(account_id, symbol, side, quantity,
                        order_type="market", duration="day",
                        limit_price=None, stop_price=None):
    _futures_orders.append((symbol, side, quantity, order_type, duration,
                            stop_price))
    return _result()


def _fake_equity_order(account_id, symbol, side, quantity,
                       order_type="market", duration="day",
                       limit_price=None, stop_price=None):
    _equity_orders.append((symbol, side, quantity, order_type, duration,
                           stop_price))
    return _result()


def _fake_cancel(account_id, order_id):
    _cancelled.append(order_id)
    return True


def _fake_outcome(account_id, order_id, expected_cancel=False):
    """Default "filled" for anything not explicitly scripted.

    A cancelled floor is polled with expected_cancel=True and wants "dead";
    an exit submission wants "filled". Defaulting per caller rather than
    globally keeps each test scripting only the id it cares about.
    """
    if order_id in _outcomes:
        return {"state": _outcomes[order_id]}
    return {"state": "dead" if expected_cancel else "filled"}


def _fake_get_order(account_id, order_id):
    """tc.get_order returns Optional[FLOAT] — the average fill price — not a
    dict. Returning a dict here is silent until some other module's exit path
    does arithmetic on it, which is exactly how this stub leaked into
    test_stops/test_vix_regime as a TypeError."""
    return NQ_FILL


def _reset(quote_price=None):
    _futures_orders.clear()
    _equity_orders.clear()
    _cancelled.clear()
    _outcomes.clear()
    global _order_fails
    _order_fails = False
    _next_id[0] = 0
    # conftest.py turns the broker floor OFF for every test (it is an autouse
    # fixture, so it re-applies per test and restores after). These tests are
    # specifically about the floor travelling with the stop, so turn it back on
    # here — inside the test body, where monkeypatch will still undo it.
    config.ENABLE_BROKER_STOP_FLOOR = True
    strategy._stop_exits = 0
    strategy._stops_trailed = 0
    strategy._floors_placed = 0
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy.tc.place_futures_order = _fake_futures_order
    strategy.tc.place_equity_order = _fake_equity_order
    strategy.tc.cancel_order = _fake_cancel
    strategy.tc.get_order_outcome = _fake_outcome
    strategy.tc.get_order = _fake_get_order
    strategy.tc.get_working_orders = lambda aid: []
    strategy._floors_reconciled = False
    strategy.tc.get_quote = (lambda s: {"last": quote_price}) if quote_price \
        else (lambda s: None)
    _testlib.safe_remove(strategy._STOPS_PATH)


# ── 1. Per-process stop file ──────────────────────────────────────────────────

def _stop_path_for(mode):
    """config.STOP_PRICE_FILE as resolved in a FRESH interpreter for `mode`.

    A subprocess, not importlib.reload: _PROC_SUFFIX is read at import time from
    the environment, and reloading config in-process would leave the already
    imported strategy module holding a stale _STOPS_PATH built from the old
    value — a test that passes while corrupting every later test in the file.
    """
    env = dict(os.environ, BOT_MODE=mode)
    out = subprocess.run(
        [sys.executable, "-c", "import config; print(config.STOP_PRICE_FILE)"],
        capture_output=True, text=True, env=env, cwd=os.path.dirname(
            os.path.abspath(__file__)))
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_equities_stop_file_path_is_unchanged():
    """The suffix must be EMPTY for equities. This is the no-migration
    guarantee: four live records and their resting GTC order ids sit in
    data/stop_prices.json, and a renamed path would orphan every one of them."""
    assert _stop_path_for("equities") == "data/stop_prices.json"


def test_futures_stop_file_path_is_separate():
    assert _stop_path_for("futures") == "data/stop_prices.futures.json"


def test_the_two_paths_differ():
    """The whole point, asserted directly rather than inferred from the two
    tests above — if both ever resolve to the same file, reconcile_stops in
    either process deletes the other's records within a cycle."""
    assert _stop_path_for("equities") != _stop_path_for("futures")


def test_no_cross_contamination_between_the_two_files():
    """An equity reconcile against equity positions must not touch a futures
    record, and vice versa. Written against ONE file deliberately: if the paths
    ever collapse to a shared file this is the test that fails, because each
    reconcile call would prune the other instrument's record."""
    _reset(quote_price=100.0)
    strategy._save_stops({
        "TSLA":   {"entry_price": 359.92, "atr_at_entry": 13.5, "atr_mult": 2.5,
                   "direction": "long", "high_water": 366.41,
                   "stop_price": 332.633, "bootstrapped": False},
        "NQU26":  {"entry_price": NQ_FILL, "atr_at_entry": NQ_ATR,
                   "atr_mult": 3.0, "direction": "long", "high_water": NQ_FILL,
                   "stop_price": 27944.62, "bootstrapped": False},
    })
    # Equities process view: it holds TSLA and knows nothing of NQU26.
    strategy.reconcile_stops([{"symbol": "TSLA", "quantity": 100}])
    survived = strategy._load_stops()
    assert "NQU26" not in survived, (
        "the equities reconcile pruned the futures record — the stop files are "
        "shared again, or reconcile_stops lost its per-process scoping")
    assert "TSLA" in survived


# ── 2. Instrument routing ─────────────────────────────────────────────────────

def test_is_futures_symbol_classifies_contracts_and_continuous():
    assert strategy.tc.is_futures_symbol("NQU26") is True
    assert strategy.tc.is_futures_symbol("@ES") is True
    assert strategy.tc.is_futures_symbol("RTYU26") is True
    # Equities, ETFs and option contracts must all read as NOT futures, or a
    # stock exit would be routed to the futures endpoint.
    for s in ("TSLA", "AAPL", "SPY", "GOOGL", "NVDA 260821C220", "", None):
        assert strategy.tc.is_futures_symbol(s) is False, s


def test_stop_order_routes_futures_to_futures_endpoint():
    _reset()
    strategy._stop_order_for("NQU26", "ACCT", "sell", 1, 27624.24)
    assert _equity_orders == [], f"leaked to the equity endpoint: {_equity_orders}"
    assert len(_futures_orders) == 1, _futures_orders
    sym, side, qty, otype, dur, stop = _futures_orders[0]
    assert (sym, side, qty) == ("NQU26", "sell", 1)
    assert otype == "stop" and dur == "gtc", _futures_orders[0]
    assert stop == 27624.24, _futures_orders[0]


def test_stop_order_still_routes_equities_to_equity_endpoint():
    """The regression that would matter most: this is the call protecting four
    live positions, and it now goes through a dispatcher."""
    _reset()
    strategy._stop_order_for("TSLA", "ACCT", "sell", 100, 332.63)
    assert _futures_orders == [], f"leaked to the futures endpoint: {_futures_orders}"
    assert len(_equity_orders) == 1, _equity_orders
    sym, side, qty, otype, dur, stop = _equity_orders[0]
    assert (sym, side, qty, otype, dur, stop) == ("TSLA", "sell", 100, "stop",
                                                  "gtc", 332.63)


def test_exit_order_routes_by_instrument():
    _reset()
    strategy._exit_order_for("NQU26", "ACCT", "sell", 1)
    strategy._exit_order_for("TSLA", "ACCT", "sell", 100)
    assert [o[0] for o in _futures_orders] == ["NQU26"], _futures_orders
    assert [o[0] for o in _equity_orders] == ["TSLA"], _equity_orders


def test_futures_short_exit_side_is_plain_sell():
    """Futures have no short path, so buy_to_cover must never reach the futures
    endpoint — TradeStation would reject the action for that instrument."""
    _reset()
    strategy._exit_order_for("NQU26", "ACCT", "buy_to_cover", 1)
    assert _futures_orders[0][1] == "sell", _futures_orders


# ── 3. Bootstrap refuses margin-as-entry ──────────────────────────────────────

def test_bootstrap_refuses_cost_basis_for_futures():
    """NQU26's TotalCost is 43,972 (margin) against a real fill of 29,546.50.
    basis/|qty| would put entry at 43,972 and the stop ~1,600 points BELOW that
    — still ~13,000 points above a 29,102 market, so the record is breached on
    its first cycle and market-sells the contract. Entry must come from the live
    price instead."""
    _reset(quote_price=29102.00)
    positions = [{"symbol": "NQU26", "quantity": 1, "cost_basis": NQ_MARGIN}]
    sig = {"close": 29102.00, "atr": NQ_ATR}
    exited = strategy._check_and_trail_stop("NQU26", 1, sig, "ACCT", positions)
    assert exited is False, "adopted futures position must NOT stop out on cycle 1"
    assert _futures_orders == [] or all(o[3] == "stop" for o in _futures_orders), \
        f"an exit order was placed on bootstrap: {_futures_orders}"

    rec = strategy._load_stops()["NQU26"]
    assert abs(rec["entry_price"] - 29102.00) < 0.01, (
        f"entry anchored on margin, not price: {rec}")
    assert abs(rec["entry_price"] - NQ_MARGIN) > 1000, (
        f"entry looks like the margin figure {NQ_MARGIN}: {rec}")
    assert rec["bootstrapped"] is True, rec
    # stop = high_water - 3.0 * ATR, and it must sit BELOW the market.
    assert rec["stop_price"] < 29102.00, rec
    assert abs(rec["stop_price"] - (29102.00 - 3.0 * NQ_ATR)) < 0.01, rec


def test_bootstrap_still_uses_cost_basis_for_equities():
    """The futures branch must not swallow the equity path — shares really do
    report TotalCost as price x quantity."""
    _reset(quote_price=203.40)
    positions = [{"symbol": "NVDA", "quantity": 238, "cost_basis": 49858.62}]
    sig = {"close": 203.53, "atr": 7.22}
    strategy._check_and_trail_stop("NVDA", 238, sig, "ACCT", positions)
    rec = strategy._load_stops()["NVDA"]
    assert abs(rec["entry_price"] - 209.49) < 0.01, rec


# ── 4. Arming, width, trailing, floor-first exit ──────────────────────────────

def test_futures_arms_at_low_vol_band_3x():
    """NQ ATR/price is 1.83%, under the 2% low-vol threshold, so in risk_on the
    width is the low-band 3.0x — NOT the 2.5x STOP_LOSS_ATR_MULT default."""
    _reset()
    strategy._arm_stop_on_entry("NQU26", NQ_FILL, NQ_ATR, regime="risk_on",
                                signal_price=29567.0, fill_price=NQ_FILL,
                                slippage=-20.5, qty=1, account_id="ACCT")
    rec = strategy._load_stops()["NQU26"]
    assert rec["atr_mult"] == 3.0, f"expected the low-vol band, got {rec}"
    assert rec["direction"] == "long", rec
    assert abs(rec["entry_price"] - NQ_FILL) < 0.01, rec
    assert abs(rec["stop_price"] - (NQ_FILL - 3.0 * NQ_ATR)) < 0.01, rec
    assert rec["bootstrapped"] is False, rec


def test_futures_arms_off_the_fill_not_the_signal_price():
    """One NQ point is $20 and the last NQU26 entry slipped 20.5 points, so a
    stop armed at the signal price is $410 wrong before it starts."""
    _reset()
    strategy._arm_stop_on_entry("NQU26", NQ_FILL, NQ_ATR, regime="risk_on",
                                signal_price=29567.0, fill_price=NQ_FILL,
                                slippage=-20.5, qty=1, account_id="ACCT")
    rec = strategy._load_stops()["NQU26"]
    assert rec["entry_price"] != 29567.0, "armed at the signal price"
    assert abs(rec["entry_price"] - NQ_FILL) < 0.01, rec


def test_futures_arm_places_gtc_floor_on_the_futures_endpoint():
    _reset()
    strategy._arm_stop_on_entry("NQU26", NQ_FILL, NQ_ATR, regime="risk_on",
                                fill_price=NQ_FILL, signal_price=NQ_FILL,
                                slippage=0.0, qty=1, account_id="ACCT")
    assert _equity_orders == [], f"floor leaked to equities: {_equity_orders}"
    gtc = [o for o in _futures_orders if o[3] == "stop"]
    assert len(gtc) == 1, _futures_orders
    # floor = entry - atr*mult*BROKER_STOP_FLOOR_BUFFER, wider than the bot stop
    expected = NQ_FILL - NQ_ATR * 3.0 * config.BROKER_STOP_FLOOR_BUFFER
    assert abs(gtc[0][5] - round(expected, 2)) < 0.01, gtc[0]
    rec = strategy._load_stops()["NQU26"]
    assert gtc[0][5] < rec["stop_price"], (
        "the GTC floor must sit BELOW the bot stop so the bot exits first")
    assert rec["broker_order_id"] == "F1", rec


def test_futures_stop_trails_up_as_price_rises():
    _reset()
    strategy._arm_stop_on_entry("NQU26", NQ_FILL, NQ_ATR, regime="risk_on",
                                fill_price=NQ_FILL, signal_price=NQ_FILL,
                                slippage=0.0, qty=1, account_id="ACCT")
    armed = strategy._load_stops()["NQU26"]["stop_price"]

    # Price rises 500 points; the stop follows by the same 500.
    strategy.tc.get_quote = lambda s: {"last": NQ_FILL + 500}
    positions = [{"symbol": "NQU26", "quantity": 1, "cost_basis": NQ_MARGIN}]
    exited = strategy._check_and_trail_stop(
        "NQU26", 1, {"close": NQ_FILL + 500, "atr": NQ_ATR}, "ACCT", positions)
    assert exited is False
    rec = strategy._load_stops()["NQU26"]
    assert abs(rec["high_water"] - (NQ_FILL + 500)) < 0.01, rec
    assert abs(rec["stop_price"] - (armed + 500)) < 0.01, rec

    # Price falls back; the ratchet holds the stop where it was.
    strategy.tc.get_quote = lambda s: {"last": NQ_FILL + 100}
    strategy._check_and_trail_stop(
        "NQU26", 1, {"close": NQ_FILL + 100, "atr": NQ_ATR}, "ACCT", positions)
    held = strategy._load_stops()["NQU26"]
    assert abs(held["stop_price"] - (armed + 500)) < 0.01, (
        f"stop loosened on a pullback: {held}")


def test_futures_state_exit_cancels_the_gtc_before_selling():
    """The GOOGL failure shape, one instrument over: the resting GTC reserves
    the contract, so a bare market sell is refused. _signal_exit must cancel and
    CONFIRM the floor is gone first, then submit."""
    _reset()
    strategy._arm_stop_on_entry("NQU26", NQ_FILL, NQ_ATR, regime="risk_on",
                                fill_price=NQ_FILL, signal_price=NQ_FILL,
                                slippage=0.0, qty=1, account_id="ACCT")
    floor_id = strategy._load_stops()["NQU26"]["broker_order_id"]
    _futures_orders.clear()
    _outcomes[floor_id] = "dead"          # the cancel is confirmed gone

    order_id, status = strategy._signal_exit("NQU26", "sell", 1, "ACCT")
    assert status == "executed", status
    assert order_id != floor_id, "the exit reused the floor's order id"
    assert _cancelled == [floor_id], (
        f"floor was not cancelled before the exit: cancelled={_cancelled}")
    market = [o for o in _futures_orders if o[3] == "market"]
    assert len(market) == 1 and market[0][:3] == ("NQU26", "sell", 1), _futures_orders
    assert "NQU26" not in strategy._load_stops(), "stop record survived the exit"


def test_floor_reconcile_does_not_latch_on_an_empty_stop_file():
    """The bug that shipped with futures stops on 2026-08-24.

    reconcile_broker_floors is a one-shot per process and runs BEFORE the
    per-symbol evaluation that bootstraps records for adopted positions. On the
    first cycle of a fresh deploy the stop file is empty, so there is nothing to
    re-arm — and latching there spends the process's only attempt on an empty
    file, leaving every adopted position with a bot stop but NO resting GTC
    until someone restarts. ES/NQ/RTY all came up unfloored exactly this way."""
    _reset(quote_price=29102.00)
    positions = [{"symbol": "NQU26", "quantity": 1, "cost_basis": NQ_MARGIN}]

    # Cycle 1: file is empty (nothing bootstrapped yet).
    strategy.reconcile_broker_floors(positions, "ACCT")
    assert strategy._floors_reconciled is False, (
        "latched on an empty stop file — adopted positions will never be floored")
    assert _futures_orders == [], _futures_orders

    # Bootstrap creates the record, as the eval loop would.
    strategy._check_and_trail_stop("NQU26", 1, {"close": 29102.00, "atr": NQ_ATR},
                                   "ACCT", positions)
    assert "NQU26" in strategy._load_stops()

    # Cycle 2: now there IS something to re-arm, and the floor gets placed.
    strategy.reconcile_broker_floors(positions, "ACCT")
    gtc = [o for o in _futures_orders if o[3] == "stop"]
    assert len(gtc) == 1, f"no GTC floor placed on the retry: {_futures_orders}"
    assert gtc[0][0] == "NQU26", gtc[0]
    assert strategy._load_stops()["NQU26"].get("broker_order_id"), \
        "floor placed but the id was not persisted"
    assert strategy._floors_reconciled is True, "clean pass should latch"


def test_futures_exit_failure_leaves_the_record_alone():
    """A refused exit must not tear down state — the position is still open and
    still needs its stop."""
    _reset()
    strategy._arm_stop_on_entry("NQU26", NQ_FILL, NQ_ATR, regime="risk_on",
                                fill_price=NQ_FILL, signal_price=NQ_FILL,
                                slippage=0.0, qty=1, account_id="ACCT")
    floor_id = strategy._load_stops()["NQU26"]["broker_order_id"]
    _outcomes[floor_id] = "dead"
    global _order_fails
    _order_fails = True                              # broker refuses the exit
    try:
        order_id, status = strategy._signal_exit("NQU26", "sell", 1, "ACCT")
    finally:
        _order_fails = False
    assert status == "failed", status
    assert "NQU26" in strategy._load_stops(), "record dropped on a failed exit"


if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="futures_stops_test_")
    strategy._STOPS_PATH = os.path.join(_tmpdir, "stop_prices.json")
    # Same restore the autouse fixture does under pytest, by hand — conftest and
    # its fixtures never load on this path.
    _orig = {a: getattr(strategy.tc, a, None) for a in _TC_ATTRS}
    _orig_logtrade = strategy.log_trade
    _orig_floor = config.ENABLE_BROKER_STOP_FLOOR
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
        for _a, _v in _orig.items():
            setattr(strategy.tc, _a, _v)
        strategy.log_trade = _orig_logtrade
        config.ENABLE_BROKER_STOP_FLOOR = _orig_floor
