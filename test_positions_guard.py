"""
Unit tests for the failed-positions-fetch guard — NO network.

Covers the root cause of the 2026-07-16 CRL/LII doubling: get_positions()
returned [] when the API 503'd, so `held == 0` read as "flat" for every symbol
and the entry paths re-entered positions the account already held.

Two halves:
  * the CLIENT contract — None means "fetch failed", [] means "genuinely flat"
  * the CYCLE guard     — main._run_cycle abandons the pass on None

Run:  python3 test_positions_guard.py
"""

import logging

import main
import strategy
import tradestation_client as tc


# ── Client contract: None != [] ──────────────────────────────────────────────

def _client_with(get_impl):
    """Point tc._get at a fake and hand back a restore callable."""
    orig = tc._get
    tc._get = get_impl
    return lambda: setattr(tc, "_get", orig)


def test_get_positions_returns_none_on_api_error():
    """The 503 that caused the incident. MUST NOT be [] — that is what made the
    bot believe the account was flat."""
    def boom(path):
        raise RuntimeError("503 Server Error: Service Unavailable")
    restore = _client_with(boom)
    try:
        assert tc.get_positions("ACCT") is None, \
            "a failed fetch must be None (unknown), never [] (flat)"
    finally:
        restore()


def test_get_positions_returns_empty_list_for_genuinely_flat_account():
    """A successful fetch of an empty account is [], and must stay distinguishable
    from the error case."""
    restore = _client_with(lambda path: {"Positions": []})
    try:
        result = tc.get_positions("ACCT")
        assert result == [], result
        assert result is not None, "a real empty account is [], not None"
    finally:
        restore()


def test_get_positions_parses_long_and_short():
    """Regression: the happy path still maps quantity sign and cost basis."""
    payload = {"Positions": [
        {"Symbol": "CRL", "Quantity": "440", "LongShort": "Long",
         "TotalCost": "99788.0"},
        {"Symbol": "AAPL", "Quantity": "50", "LongShort": "Short",
         "TotalCost": "15000.0"},
    ]}
    restore = _client_with(lambda path: payload)
    try:
        out = tc.get_positions("ACCT")
        assert out[0] == {"symbol": "CRL", "quantity": 440, "cost_basis": 99788.0}, out
        assert out[1]["quantity"] == -50, "shorts are negative"
    finally:
        restore()


# ── Cycle guard: None abandons the whole pass ────────────────────────────────

class _Spy:
    """Records whether the cycle proceeded past the positions fetch."""
    def __init__(self):
        self.evaluated, self.perf_logged, self.reconciled = [], 0, 0


def _install_cycle_spies(spy, positions_result):
    orig = {
        "get_positions": tc.get_positions,
        "get_balance":   tc.get_account_balance,
        "log_perf":      main.log_performance,
        "eval_stock":    strategy.evaluate_stock,
        "eval_option":   strategy.evaluate_option,
        "rec_stops":     strategy.reconcile_stops,
        "rec_mom":       strategy.reconcile_momentum_entries,
    }

    def _log_perf(*a, **k):
        spy.perf_logged += 1

    def _eval_stock(symbol, *a, **k):
        spy.evaluated.append(symbol)

    def _rec_mom(*a, **k):
        spy.reconciled += 1

    tc.get_positions = lambda acct: positions_result
    tc.get_account_balance = lambda acct: {"total_equity": 1000.0, "total_cash": 500.0}
    main.log_performance = _log_perf
    strategy.evaluate_stock = _eval_stock
    strategy.evaluate_option = lambda *a, **k: None
    strategy.reconcile_stops = lambda *a, **k: None
    strategy.reconcile_momentum_entries = _rec_mom

    def restore():
        tc.get_positions = orig["get_positions"]
        tc.get_account_balance = orig["get_balance"]
        main.log_performance = orig["log_perf"]
        strategy.evaluate_stock = orig["eval_stock"]
        strategy.evaluate_option = orig["eval_option"]
        strategy.reconcile_stops = orig["rec_stops"]
        strategy.reconcile_momentum_entries = orig["rec_mom"]
    return restore


def _reset_counters():
    main._positions_fetch_failures = 0
    main._positions_fetch_consecutive = 0


def test_cycle_skipped_when_positions_fetch_fails():
    """The whole pass is abandoned — not just entries. Nothing downstream may run
    on holdings we could not read."""
    _reset_counters()
    spy = _Spy()
    restore = _install_cycle_spies(spy, None)
    try:
        main._run_cycle("ACCT")
        assert spy.evaluated == [], "no symbol may be evaluated on an unknown book"
        assert spy.perf_logged == 0, \
            "no performance snapshot — it would record a false 'Open positions: 0'"
        assert spy.reconciled == 0, "no reconcile against an unknown book"
        assert main._positions_fetch_failures == 1, "skip counted"
    finally:
        restore()


def test_cycle_proceeds_on_genuinely_empty_account():
    """[] is a real answer: a flat account still evaluates its watchlist, or the
    bot could never open its first position."""
    _reset_counters()
    spy = _Spy()
    restore = _install_cycle_spies(spy, [])
    try:
        main._run_cycle("ACCT")
        assert spy.evaluated, "an empty account must still be evaluated"
        assert spy.perf_logged == 1, "flat is a loggable state"
        assert main._positions_fetch_failures == 0, "[] is not a failure"
    finally:
        restore()


def test_consecutive_failures_reset_after_a_good_cycle():
    """The consecutive counter drives the ERROR escalation; a recovered cycle must
    clear it, while the lifetime total keeps accumulating."""
    _reset_counters()
    spy = _Spy()
    restore = _install_cycle_spies(spy, None)
    try:
        main._run_cycle("ACCT")
        main._run_cycle("ACCT")
        assert main._positions_fetch_consecutive == 2, "consecutive tracked"
    finally:
        restore()

    restore = _install_cycle_spies(spy, [])
    try:
        main._run_cycle("ACCT")
        assert main._positions_fetch_consecutive == 0, "a good cycle resets the streak"
        assert main._positions_fetch_failures == 2, "lifetime total is not reset"
    finally:
        restore()


def test_sustained_outage_escalates_to_error(caplog=None):
    """A blip is a WARNING; a sustained outage is an ERROR, because stops are
    bot-managed and go unenforced for its whole duration."""
    _reset_counters()
    spy = _Spy()
    restore = _install_cycle_spies(spy, None)
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    main.logger.addHandler(handler)
    try:
        for _ in range(main._POSITIONS_FAILURE_ESCALATE_AFTER):
            main._run_cycle("ACCT")
    finally:
        main.logger.removeHandler(handler)
        restore()

    levels = [r.levelno for r in records]
    assert levels[0] == logging.WARNING, f"first skip is a warning, got {levels}"
    assert levels[-1] == logging.ERROR, \
        f"skip #{main._POSITIONS_FAILURE_ESCALATE_AFTER} must escalate, got {levels}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"All {passed} assertions passed.")
