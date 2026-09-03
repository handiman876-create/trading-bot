"""
Unit tests for the per-cycle timing instrumentation — NO network.

Scope is deliberately narrow: this is pure instrumentation, so the only
contracts worth pinning are the LOG LINE FORMAT (the analyzer / any future
grep depends on it) and the invariant that EVERY cycle emits exactly one such
line — including the two paths that historically produced no output at all,
a skipped cycle and a cycle that raised.

The format matters because the whole point of the change is to replace
inferring cycle cost from log-line spacing (1-second resolution, and blind to
throttling) with a measured number. A silently renamed field would put us back
on inference without anyone noticing.

Run:  python3 test_cycle_timing.py
"""

import logging
import re

import main
import strategy
import tradestation_client as tc

# The contract. Milliseconds are REQUIRED: at ~4s of work on 20 symbols the
# per-symbol cost is ~0.2s, so whole-second resolution cannot resolve the
# marginal cost of adding a name — which is the question this line exists to
# answer.
CYCLE_LINE = re.compile(
    r"^cycle work=(\d+\.\d{3})s symbols=(\d+) options=(\d+)$"
)


def _install(positions_result, *, symbols=("SPY", "QQQ", "AAPL"), eval_raises=False):
    """Stub the cycle's collaborators. Returns a restore callable."""
    orig = {
        "get_positions": tc.get_positions,
        "get_balance":   tc.get_account_balance,
        "log_perf":      main.log_performance,
        "eval_stock":    strategy.evaluate_stock,
        "eval_option":   strategy.evaluate_option,
        "rec_stops":     strategy.reconcile_stops,
        "rec_floors":    strategy.reconcile_broker_floors,
        "rec_mom":       strategy.reconcile_momentum_entries,
        "eff_watchlist": main.watchlist.effective_stock_watchlist,
        "mom_slot":      main.watchlist.momentum_slot,
        "regime":        strategy.current_regime,
        "eff_regime":    strategy.effective_regime,
        "note_regime":   strategy.note_regime,
        "sentiment":     main.sentiment_analyzer.current_sentiment,
        "sent_regime":   main.sentiment_analyzer.sentiment_regime,
        "blocked":       main.sentiment_analyzer.sectors_blocked,
    }

    def _eval_stock(symbol, *a, **k):
        if eval_raises:
            raise RuntimeError("simulated throttle")

    tc.get_positions = lambda acct: positions_result
    tc.get_account_balance = lambda acct: {"total_equity": 1000.0, "total_cash": 500.0}
    main.log_performance = lambda *a, **k: None
    strategy.evaluate_stock = _eval_stock
    strategy.evaluate_option = lambda *a, **k: None
    strategy.reconcile_stops = lambda *a, **k: None
    strategy.reconcile_broker_floors = lambda *a, **k: None
    strategy.reconcile_momentum_entries = lambda *a, **k: None
    main.watchlist.effective_stock_watchlist = lambda positions: list(symbols)
    main.watchlist.momentum_slot = lambda: ([], None)
    # No network: current_regime() otherwise refreshes a TradeStation token and
    # fetches $VIX.X, which makes the test slow, flaky, and dependent on the
    # throttled token endpoint.
    strategy.current_regime = lambda: (14.3, "risk_on")
    strategy.effective_regime = lambda *a, **k: "risk_on"
    strategy.note_regime = lambda *a, **k: None
    main.sentiment_analyzer.current_sentiment = lambda: {}
    main.sentiment_analyzer.sentiment_regime = lambda s: "risk_on"
    main.sentiment_analyzer.sectors_blocked = lambda s: set()

    def restore():
        tc.get_positions = orig["get_positions"]
        tc.get_account_balance = orig["get_balance"]
        main.log_performance = orig["log_perf"]
        strategy.evaluate_stock = orig["eval_stock"]
        strategy.evaluate_option = orig["eval_option"]
        strategy.reconcile_stops = orig["rec_stops"]
        strategy.reconcile_broker_floors = orig["rec_floors"]
        strategy.reconcile_momentum_entries = orig["rec_mom"]
        main.watchlist.effective_stock_watchlist = orig["eff_watchlist"]
        main.watchlist.momentum_slot = orig["mom_slot"]
        strategy.current_regime = orig["regime"]
        strategy.effective_regime = orig["eff_regime"]
        strategy.note_regime = orig["note_regime"]
        main.sentiment_analyzer.current_sentiment = orig["sentiment"]
        main.sentiment_analyzer.sentiment_regime = orig["sent_regime"]
        main.sentiment_analyzer.sectors_blocked = orig["blocked"]
    return restore


class _Capture(logging.Handler):
    """Collect just the timing lines off the 'bot' logger."""
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        msg = record.getMessage()
        if msg.startswith("cycle work="):
            self.lines.append(msg)


def _run(positions_result, **kw):
    cap = _Capture()
    log = logging.getLogger("bot")
    # Pin the level explicitly. Under pytest the logging plugin leaves the
    # effective level at WARNING, so logger.info() never creates a record at
    # all and the handler below would see nothing — the failure mode that made
    # these tests look broken when the instrumentation was in fact fine.
    prev_level, prev_propagate = log.level, log.propagate
    log.setLevel(logging.INFO)
    log.addHandler(cap)
    restore = _install(positions_result, **kw)
    try:
        main._run_cycle("ACCT")
    finally:
        restore()
        log.removeHandler(cap)
        log.setLevel(prev_level)
        log.propagate = prev_propagate
    return cap.lines


def test_timing_line_format_and_counts():
    """The happy path: one line, parseable, and the symbol count is real rather
    than a hardcoded guess."""
    lines = _run([], symbols=("SPY", "QQQ", "AAPL"))
    assert len(lines) == 1, f"expected exactly one timing line, got {lines}"
    m = CYCLE_LINE.match(lines[0])
    assert m, f"line does not match the contract: {lines[0]!r}"
    work, symbols, options = float(m.group(1)), int(m.group(2)), int(m.group(3))
    assert symbols == 3, f"symbols must count actual evaluations, got {symbols}"
    assert options == len(main.config.OPTIONS_WATCHLIST), options
    assert work >= 0.0


def test_symbol_count_tracks_watchlist_size():
    """The count must move with the watchlist — this is the whole point, since
    the open question is what happens when the list grows."""
    small = CYCLE_LINE.match(_run([], symbols=("SPY",))[0])
    large = CYCLE_LINE.match(_run([], symbols=tuple(f"S{i}" for i in range(25)))[0])
    assert int(small.group(2)) == 1, small.group(2)
    assert int(large.group(2)) == 25, large.group(2)


def test_skipped_cycle_still_logs_timing():
    """A failed positions fetch abandons the pass. It must STILL emit a line
    with symbols=0 — a cycle that produced no timing line at all is
    indistinguishable from the bot being wedged."""
    lines = _run(None)
    assert len(lines) == 1, f"skipped cycle must still log timing, got {lines}"
    m = CYCLE_LINE.match(lines[0])
    assert m, lines[0]
    assert int(m.group(2)) == 0, "nothing was evaluated on an unknown book"
    assert int(m.group(3)) == 0


def test_partial_cycle_reports_symbols_attempted():
    """Each loop swallows its own per-symbol exception, so a throttle that makes
    every evaluate fail must still be visible as full work, not zero."""
    m = CYCLE_LINE.match(_run([], symbols=("SPY", "QQQ"), eval_raises=True)[0])
    assert m, "a cycle whose evaluations all raised still logs timing"
    assert int(m.group(2)) == 2, \
        "counts ATTEMPTED evaluations — each one still spent an API call"


def test_counters_reset_between_cycles():
    """Module-level counters must not accumulate across cycles, or every number
    after the first is garbage."""
    first = CYCLE_LINE.match(_run([], symbols=("SPY", "QQQ"))[0])
    second = CYCLE_LINE.match(_run([], symbols=("SPY", "QQQ"))[0])
    assert int(first.group(2)) == int(second.group(2)) == 2, \
        "counters leaked between cycles"


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{'FAILED' if failed else 'OK'} ({failed} failure(s))")
    sys.exit(1 if failed else 0)
