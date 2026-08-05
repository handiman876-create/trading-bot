"""
Unit tests for the OCC-symbol separation between the stock loop and the options
loop — NO network.

WHAT THIS PROTECTS: on 2026-08-05 watchlist.effective_stock_watchlist folded the
held OCC symbol "NVDA 260821C220" into the STOCK list, because the held fold-in
reads broker positions verbatim and nothing filtered contracts out. Every
downstream equity mechanism then ran against an option contract:

  * EMAs/RSI computed on option premium instead of the underlying
  * _bootstrap_stop estimated entry as cost_basis/|qty| = 815.00 for a fill that
    was actually 8.15 premium (the x100 contract multiplier), arming a stop at
    808.20 that a ~7.20 premium breaches on the first cycle and every cycle after
  * the exit routed through place_equity_order -> TradeAction "SELL", which is
    invalid for an option, so TradeStation 400'd all 326 attempts while the
    contract sat unsellable from 14:02 to 19:59

Covers: contracts are filtered out of the stock watchlist; ordinary stocks and
the held-straggler orphan guard are untouched; _bootstrap_stop reads the stored
per-share entry for a contract rather than re-deriving it from cost basis; and
reconcile_stops clears OCC-keyed stop debris.

Run:  python3 test_occ_watchlist.py   (or via pytest)
"""

import json
import logging
import os
import tempfile

import _testlib
import config
import strategy
import watchlist


# ── log capture (works under pytest AND the __main__ runner) ─────────────────
class _LogCap:
    def __init__(self, target):
        self._target = target

    def __enter__(self):
        self.records = []
        self._h = logging.Handler()
        self._h.emit = lambda r: self.records.append(r.getMessage())
        self._prev = self._target.level
        self._target.addHandler(self._h)
        self._target.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self._target.removeHandler(self._h)
        self._target.setLevel(self._prev)
        return False


def _reset_watchlist():
    """Clear the OCC-filter counters so per-test log assertions are stable."""
    watchlist._occ_filtered = 0
    watchlist._occ_seen.clear()


def _write_option_store(records: dict):
    _testlib.assert_disposable(strategy._OPT_POSITIONS_PATH)
    with open(strategy._OPT_POSITIONS_PATH, "w") as fh:
        json.dump(records, fh)


# ── config.is_occ_symbol ─────────────────────────────────────────────────────

def test_is_occ_symbol_distinguishes_contracts_from_tickers():
    assert config.is_occ_symbol("NVDA 260821C220")
    assert config.is_occ_symbol("SPY 260717C540")
    # Stock and ETF tickers never contain a space.
    for ticker in ("NVDA", "SPY", "GOOGL", "BRK.B", ""):
        assert not config.is_occ_symbol(ticker), ticker


# ── Bug 1: the stock watchlist excludes contracts ────────────────────────────

def test_occ_symbol_filtered_from_stock_watchlist():
    """The exact 2026-08-05 position set: a held contract must NOT reach the
    stock loop."""
    _reset_watchlist()
    positions = [
        {"symbol": "NVDA 260821C220", "quantity": 1},
        {"symbol": "AAPL", "quantity": -156},
    ]
    result = watchlist.effective_stock_watchlist(positions)
    assert "NVDA 260821C220" not in result, result
    assert not any(config.is_occ_symbol(s) for s in result), result
    # The underlying itself is still traded — it is in CORE_WATCHLIST. Filtering
    # the contract must not filter NVDA the stock.
    assert "NVDA" in result, result


def test_occ_filter_logs_once_and_counts():
    """Safety nets carry a counter. The log line fires once per contract, not
    once per 65-second cycle (that was 326 lines/day in the incident)."""
    _reset_watchlist()
    positions = [{"symbol": "NVDA 260821C220", "quantity": 1}]
    with _LogCap(watchlist.logger) as cap:
        for _ in range(5):                       # five cycles
            watchlist.effective_stock_watchlist(positions)
    hits = [m for m in cap.records if "OCC FILTER" in m]
    assert len(hits) == 1, f"expected one log line across 5 cycles, got {hits}"
    assert "NVDA 260821C220" in hits[0], hits[0]
    assert watchlist._occ_filtered == 5, watchlist._occ_filtered


def test_regular_stocks_unaffected():
    """Core names, momentum names and held stragglers all still come through."""
    _reset_watchlist()
    core = [s.upper() for s in config.CORE_WATCHLIST]
    positions = [
        {"symbol": "AAPL",  "quantity": -156},
        {"symbol": "GOOGL", "quantity": 100},
        {"symbol": "MSFT",  "quantity": 50},
        {"symbol": "PLTR",  "quantity": 200},
    ]
    result = watchlist.effective_stock_watchlist(positions)
    for symbol in core:
        assert symbol in result, f"{symbol} missing from {result}"
    for p in positions:
        assert p["symbol"] in result, f"{p['symbol']} missing from {result}"
    assert len(result) == len(set(result)), f"duplicates in {result}"


def test_held_straggler_orphan_guard_still_works():
    """A held name that is NOT core and NOT in the momentum slot must still fold
    in — that is the orphan guard, and the OCC filter must not break it."""
    _reset_watchlist()
    straggler = "ZZZZ"                            # not core, not momentum
    assert straggler not in [s.upper() for s in config.CORE_WATCHLIST]
    result = watchlist.effective_stock_watchlist(
        [{"symbol": straggler, "quantity": 10}])
    assert straggler in result, result


def test_flat_and_blank_positions_ignored():
    _reset_watchlist()
    result = watchlist.effective_stock_watchlist([
        {"symbol": "ZZZZ", "quantity": 0},        # flat -> not a straggler
        {"symbol": "",     "quantity": 5},        # blank symbol
    ])
    assert "ZZZZ" not in result, result
    assert "" not in result, result


# ── Bug 2: stop bootstrap uses the stored option entry, not cost basis ───────

def test_bootstrap_uses_option_store_entry_not_cost_basis():
    """The incident numbers. Broker cost basis is 815.00 dollars for one
    contract; the real fill was 8.15 premium. Entry must read 8.15."""
    _write_option_store({"NVDA_call": {
        "occ_symbol": "NVDA 260821C220", "entry_price": 8.15,
        "contracts": 1, "strike": 220.0, "opt_type": "call",
        "expiration": "2026-08-21", "entry_date": "2026-08-05",
    }})
    positions = [{"symbol": "NVDA 260821C220", "quantity": 1,
                  "cost_basis": 815.00}]
    rec = strategy._bootstrap_stop("NVDA 260821C220", 1,
                                   {"close": 7.20, "atr": 2.2661},
                                   positions, 7.20)
    assert rec is not None
    assert rec["entry_price"] == 8.15, rec          # NOT 815.0
    # And the resulting stop is in premium units, so a ~7.20 price does not sit
    # 800 points below it the way the shipped 808.20 record did.
    assert rec["stop_price"] < 8.15, rec
    assert rec["stop_price"] < 20, f"stop still in dollar units: {rec}"


def test_bootstrap_refuses_contract_with_no_stored_entry():
    """No stored fill -> refuse, rather than fall back to the cost-basis estimate
    that produced the 100x-off stop."""
    _write_option_store({})
    with _LogCap(strategy.logger) as cap:
        rec = strategy._bootstrap_stop("NVDA 260821C220", 1,
                                       {"close": 7.20, "atr": 2.2661},
                                       [{"symbol": "NVDA 260821C220",
                                         "quantity": 1, "cost_basis": 815.00}],
                                       7.20)
    assert rec is None, rec
    assert any("no stored entry price" in m for m in cap.records), cap.records


def test_bootstrap_stock_path_unchanged():
    """Regression guard: real NVDA stock numbers still take the cost-basis path
    and produce the same record as before the change."""
    positions = [{"symbol": "NVDA", "quantity": 238, "cost_basis": 49858.62}]
    rec = strategy._bootstrap_stop("NVDA", 238,
                                   {"close": 203.53, "atr": 7.22},
                                   positions, 203.40)
    assert rec is not None
    assert abs(rec["entry_price"] - 209.4900) < 0.001, rec   # 49858.62 / 238
    assert rec["direction"] == "long", rec
    assert rec["stop_price"] < 203.40, rec        # no immediate exit


def test_option_entry_price_lookup_by_occ_symbol():
    """Lookup is BY stored occ_symbol, so it survives an underlying drift that
    would rename a recomputed key."""
    _write_option_store({"NVDA_call": {"occ_symbol": "NVDA 260821C220",
                                       "entry_price": 8.15}})
    assert strategy._option_entry_price("NVDA 260821C220") == 8.15
    assert strategy._option_entry_price("NVDA 260821C230") is None
    _write_option_store({"NVDA_call": {"occ_symbol": "NVDA 260821C220",
                                       "entry_price": 0}})
    assert strategy._option_entry_price("NVDA 260821C220") is None


# ── reconcile: OCC stop debris is cleared ────────────────────────────────────

def test_reconcile_drops_occ_stop_records():
    """The stale 815.00 record survives the not-held prune (the contract IS
    held), so reconcile must drop it on the OCC rule instead."""
    strategy._occ_stop_prunes = 0
    _testlib.assert_disposable(strategy._STOPS_PATH)
    strategy._save_stops({
        "NVDA 260821C220": {"entry_price": 815.0, "stop_price": 808.2017,
                            "direction": "long", "atr_at_entry": 2.2661,
                            "atr_mult": 3.0, "high_water": 815.0},
        "AAPL": {"entry_price": 306.66, "stop_price": 330.0169,
                 "direction": "short", "atr_at_entry": 9.3428,
                 "atr_mult": 2.5, "low_water": 306.66},
    })
    positions = [{"symbol": "NVDA 260821C220", "quantity": 1},
                 {"symbol": "AAPL", "quantity": -156}]
    strategy.reconcile_stops(positions)
    stops = strategy._load_stops()
    assert "NVDA 260821C220" not in stops, stops
    assert "AAPL" in stops, stops                  # equity stop untouched
    assert stops["AAPL"]["stop_price"] == 330.0169, stops
    assert strategy._occ_stop_prunes == 1


def test_reconcile_empty_positions_still_a_noop():
    """Guard preserved: an API-failure [] must not wipe stops, OCC rule or not."""
    strategy._save_stops({"NVDA 260821C220": {"entry_price": 815.0},
                          "AAPL": {"entry_price": 306.66}})
    strategy.reconcile_stops([])
    assert set(strategy._load_stops()) == {"NVDA 260821C220", "AAPL"}


if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="occ_test_")
    strategy._STOPS_PATH = os.path.join(_tmpdir, "stop_prices.json")
    strategy._OPT_POSITIONS_PATH = os.path.join(_tmpdir, "options_positions.json")
    strategy._MOM_ENTRIES_PATH = os.path.join(_tmpdir, "momentum_entries.json")
    _orig_logtrade = strategy.log_trade
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
        strategy.log_trade = _orig_logtrade
