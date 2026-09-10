"""
Unit tests for OPTION ENTRY fill resolution — NO network.

WHAT THIS PROTECTS. Until 2026-09-10 `_open_option` called log_trade directly
with the ASK QUOTE and resolved no broker fill at all, making the option entry
the last unresolved fill site in the bot. The 2026-07-27 correction (5f26dcd)
resolved equity/futures entries and `_log_exit_trade` later covered all eight
exit paths; this one site was missed because there is no `_log_entry_trade`
counterpart to be dragged through.

Two defects, only one of them visible:

  * the ledger recorded a quote as a fill — `fill_price`/`slippage` were null on
    every option entry ever written, so option entry slippage had never been
    measured even once; and
  * the caller persisted that same quote as the store's `entry_price`, which is
    what `_option_exit_reason` arms BOTH premium thresholds off. So the -50% stop
    and +50% target were computed off the ask rather than off what was paid.

THE INCIDENT THESE PIN. AMD 260918C520, entered 2026-09-09: ask 18.90 stored
against a true broker fill of 12.55. The -50% stop therefore sat at 9.45 instead
of 6.28 and fired on a 9.25 bid — a ~3.2-point-early exit on a position that was
down 26%, not 50%. test_amd_incident_* is the regression.

Note the failure was SILENT in both directions: nothing in the ledger, the store
or the logs distinguished a quote-priced entry from a fill-priced one. That is
why `entry_fill_resolved` is persisted rather than inferred, and why the
fallback is counted.

Run:  python3 test_option_entry_fill.py   (or via pytest)
"""

import logging
import os
import tempfile
from datetime import date, timedelta

import market_hours as mh

import _testlib
import config
import strategy


# ── log capture (works under pytest AND the __main__ runner) ──────────────────
class _LogCap:
    def __enter__(self):
        self.records = []
        self._h = logging.Handler()
        self._h.emit = lambda r: self.records.append(r.getMessage())
        self._prev = strategy.logger.level
        strategy.logger.addHandler(self._h)
        strategy.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        strategy.logger.removeHandler(self._h)
        strategy.logger.setLevel(self._prev)

    @property
    def text(self):
        return "\n".join(self.records)


ORDERS = []
TRADES = []
_EXP = (date.today() + timedelta(days=40)).isoformat()


def _bars(closes):
    return [{"open": c, "high": c, "low": c, "close": c, "volume": 1_000_000}
            for c in closes]


def _reset(sig=None, underlying=100.0, quote=None, fill=None, history=None):
    """Install the doubles and clear module state.

    `fill` is what the broker reports for the entry order: a float for a resolved
    fill, None for the lookup miss that forces the ask fallback. It is stubbed at
    tc.get_order because that is what _resolve_fill calls — conftest blocks tc._get
    outright, so an unstubbed test cannot silently reach the live broker (which is
    exactly how this path shipped its own network access unnoticed).
    """
    ORDERS.clear()
    TRADES.clear()
    strategy._save_option_positions({})
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy._option_adoptions = 0
    strategy._option_entry_unresolved = 0
    strategy._option_entries_repaired = 0
    strategy._option_entries_reconciled = False

    config.ENABLE_OPTION_EXIT_TARGETS = True
    config.OPTION_PROFIT_TARGET_PCT = 1.50
    config.OPTION_STOP_LOSS_PCT = 0.50
    config.OPTION_MIN_DAYS_TO_EXPIRY = 5

    q = quote or {"symbol": "X", "last": 5.00, "bid": 4.90, "ask": 5.20, "close": 5.0}

    strategy.config.CROSS_SUSTAIN_MINUTES = 0
    strategy._cross_first_seen.clear()
    strategy._cross_confirmed.clear()
    strategy._cross_gap_logged.clear()
    strategy._entry_delay_logged.clear()

    strategy.tc.get_historical = lambda s, days=90: _bars([underlying] * 60)
    strategy.ind.compute_indicators = lambda *a, **k: dict(sig or {})
    strategy.tc.get_option_quote = lambda occ: q
    strategy.tc.find_option_symbol = (
        lambda sym, exp, strike, ot: f"{sym} {exp.replace('-', '')[2:]}"
                                     f"{'C' if ot.lower() == 'call' else 'P'}{int(strike)}")
    strategy.tc.place_option_order = lambda acct, occ, side, qty, **k: (
        ORDERS.append((side, occ, qty)) or {"order": {"id": f"o{len(ORDERS)}"}})
    strategy.tc.get_order = lambda acct, oid: fill
    strategy.tc.get_historical_orders = lambda acct, since: history
    strategy.log_trade = lambda *a, **k: TRADES.append((a, k))
    strategy._log_exit_trade = lambda *a, **k: None
    strategy.mh.entries_allowed = lambda: True


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_strategy_globals():
        saved = {
            "hist": strategy.tc.get_historical,
            "ci":   strategy.ind.compute_indicators,
            "oq":   strategy.tc.get_option_quote,
            "fos":  strategy.tc.find_option_symbol,
            "poo":  strategy.tc.place_option_order,
            "go":   strategy.tc.get_order,
            "gho":  strategy.tc.get_historical_orders,
            "log":  strategy.log_trade,
            "xlog": strategy._log_exit_trade,
            "ea":   strategy.mh.entries_allowed,
            "sus":  getattr(strategy.config, "CROSS_SUSTAIN_MINUTES", 0),
            "en":   config.ENABLE_OPTION_EXIT_TARGETS,
            "tgt":  config.OPTION_PROFIT_TARGET_PCT,
            "stp":  config.OPTION_STOP_LOSS_PCT,
            "dte":  config.OPTION_MIN_DAYS_TO_EXPIRY,
        }
        yield
        strategy.tc.get_historical         = saved["hist"]
        strategy.ind.compute_indicators    = saved["ci"]
        strategy.tc.get_option_quote       = saved["oq"]
        strategy.tc.find_option_symbol     = saved["fos"]
        strategy.tc.place_option_order     = saved["poo"]
        strategy.tc.get_order              = saved["go"]
        strategy.tc.get_historical_orders  = saved["gho"]
        strategy.log_trade                 = saved["log"]
        strategy._log_exit_trade           = saved["xlog"]
        strategy.mh.entries_allowed        = saved["ea"]
        strategy.config.CROSS_SUSTAIN_MINUTES = saved["sus"]
        config.ENABLE_OPTION_EXIT_TARGETS = saved["en"]
        config.OPTION_PROFIT_TARGET_PCT   = saved["tgt"]
        config.OPTION_STOP_LOSS_PCT       = saved["stp"]
        config.OPTION_MIN_DAYS_TO_EXPIRY  = saved["dte"]
        strategy._cross_first_seen.clear()
        strategy._cross_confirmed.clear()
except ImportError:
    pass


def _cross_up(close=100.0, rsi=55.0):
    return {"close": close, "rsi": rsi, "ema_short": close * 1.02,
            "ema_long": close, "bullish_cross": True, "bearish_cross": False}


def _stored():
    return strategy._load_option_positions().get("SPY_call") or {}


# ── PART A: the entry resolves a real fill ────────────────────────────────────

def test_resolved_fill_is_stored_not_the_ask():
    """The store carries what the broker executed, not the quote we lifted."""
    _reset(sig=_cross_up(), fill=3.40)          # ask is 5.20
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    rec = _stored()
    assert rec["entry_price"] == 3.40
    assert rec["entry_fill_resolved"] is True
    assert rec["entry_order_id"] == "o1"


def test_resolved_fill_reaches_the_ledger_with_slippage():
    """fill_price/signal_price/slippage stop being null on option entries.

    Slippage is signed so POSITIVE is worse; a BUY_TO_OPEN that paid 6.00 against
    a 5.20 ask is 0.80 WORSE, matching _slippage_sign's contract for buys.
    """
    _reset(sig=_cross_up(), fill=6.00)
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    assert TRADES, "entry never logged a trade"
    _, kw = TRADES[-1]
    assert kw["fill_price"] == 6.00
    assert kw["signal_price"] == 5.20
    assert kw["slippage"] == 0.80


def test_thresholds_are_armed_off_the_fill_not_the_ask():
    """±50% both move when the basis moves. This is the whole point of the fix."""
    _reset(sig=_cross_up(), fill=12.55)         # ask 5.20 is deliberately far off
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    entry = _stored()["entry_price"]
    assert entry == 12.55
    # A bid at the ASK-derived stop (2.60) must NOT trigger; the real stop is 6.275.
    assert strategy._option_exit_reason(6.30, entry, _EXP) is None
    assert "stop loss" in strategy._option_exit_reason(6.20, entry, _EXP)
    assert "profit target" in strategy._option_exit_reason(18.90, entry, _EXP)


# ── PART A/C: the fallback is loud and counted ────────────────────────────────

def test_unresolved_falls_back_to_ask_with_warning_and_counter():
    _reset(sig=_cross_up(), fill=None)
    with _LogCap() as cap:
        strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    rec = _stored()
    assert rec["entry_price"] == 5.20                  # the ask — old behaviour
    assert rec["entry_fill_resolved"] is False         # but now SAYS so
    assert "OPTION ENTRY UNRESOLVED" in cap.text
    assert "unresolved entries #1" in cap.text
    assert strategy._option_entry_unresolved == 1


def test_unresolved_ledger_row_keeps_fill_null_rather_than_lying():
    """A miss must not write the ask into fill_price — that is the original bug."""
    _reset(sig=_cross_up(), fill=None)
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    _, kw = TRADES[-1]
    assert kw["fill_price"] is None
    assert kw["slippage"] is None


def test_counter_accumulates_across_entries():
    _reset(sig=_cross_up(), fill=None)
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    strategy._save_option_positions({})               # simulate a second entry
    strategy._signaled_buy_today.clear()
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    assert strategy._option_entry_unresolved == 2


def test_resolved_entry_does_not_touch_the_counter():
    _reset(sig=_cross_up(), fill=3.40)
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    assert strategy._option_entry_unresolved == 0


def test_failed_order_stores_nothing():
    """No order, no record — and no counter movement either."""
    _reset(sig=_cross_up(), fill=3.40)
    strategy.tc.place_option_order = lambda *a, **k: None
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    assert _stored() == {}
    assert strategy._option_entry_unresolved == 0


def test_zero_resolved_fill_is_data_not_failure():
    """The caller tests `is not None`, so a 0.0 fill still persists a record.

    Truthiness here would silently drop the position from the store while the
    broker holds the contract — an orphan the exit path can never manage.
    """
    _reset(sig=_cross_up(), fill=0.0)
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    assert _stored().get("occ_symbol")


# ── PART C: the startup reconcile ─────────────────────────────────────────────

def _unresolved_record(entry=18.90, oid="970609541", occ="AMD 260918C520"):
    strategy._save_option_position("AMD_call", strategy._option_record(
        occ, entry, _EXP, "call", 520.0, 521.28,
        entry_fill_resolved=False, entry_order_id=oid))


def test_reconcile_repairs_a_quote_priced_entry_from_broker_history():
    _reset(history=[{"order_id": "970609541", "symbol": "AMD 260918C520",
                     "action": "Buy", "quantity": 1, "price": 12.55,
                     "status": "Filled", "opened": "2026-09-09T14:17:47Z"}])
    _unresolved_record()
    with _LogCap() as cap:
        strategy.reconcile_option_entries("ACCT")
    rec = strategy._load_option_positions()["AMD_call"]
    assert rec["entry_price"] == 12.55
    assert rec["entry_fill_resolved"] is True
    assert "OPTION ENTRY REPAIRED" in cap.text
    assert strategy._option_entries_repaired == 1
    assert strategy._option_entry_unresolved == 0


def test_reconcile_reports_the_threshold_it_moved():
    """The log must name the consequence, not just the price change: 9.45 -> 6.28
    is the number that actually decides when the position exits."""
    _reset(history=[{"order_id": "970609541", "price": 12.55, "status": "Filled"}])
    _unresolved_record()
    with _LogCap() as cap:
        strategy.reconcile_option_entries("ACCT")
    assert "9.45" in cap.text and "6.28" in cap.text


def test_reconcile_leaves_resolved_records_alone():
    _reset(history=[{"order_id": "970609541", "price": 99.99, "status": "Filled"}])
    strategy._save_option_position("AMD_call", strategy._option_record(
        "AMD 260918C520", 12.55, _EXP, "call", 520.0, 521.28,
        entry_fill_resolved=True, entry_order_id="970609541"))
    strategy.reconcile_option_entries("ACCT")
    assert strategy._load_option_positions()["AMD_call"]["entry_price"] == 12.55
    assert strategy._option_entries_repaired == 0


def test_reconcile_warns_when_no_fill_can_be_matched():
    """Unrepairable is still worth saying: those thresholds ARE armed off a quote."""
    _reset(history=[])                       # fetch succeeded, order not in it
    _unresolved_record()
    with _LogCap() as cap:
        strategy.reconcile_option_entries("ACCT")
    assert "OPTION ENTRY UNRESOLVED" in cap.text
    assert strategy._option_entry_unresolved == 1
    assert strategy._load_option_positions()["AMD_call"]["entry_price"] == 18.90


def test_reconcile_skips_and_retries_on_a_failed_fetch():
    """None means the fetch FAILED. Repairing nothing is right; LATCHING would
    spend the process's only pass on an unknown broker state."""
    _reset(history=None)
    _unresolved_record()
    with _LogCap() as cap:
        strategy.reconcile_option_entries("ACCT")
    assert "reconcile skipped" in cap.text
    assert strategy._option_entries_reconciled is False
    assert strategy._option_entry_unresolved == 0


def test_reconcile_ignores_adopted_zero_entry_records():
    """entry_price 0.0 is a HANDLED state (_option_exit_reason refuses to arm on
    it) with no order id to repair from. Warning every startup would be noise."""
    _reset(history=[])
    strategy._save_option_position("AMD_call", strategy._option_record(
        "AMD 260918C520", 0.0, _EXP, "call", 520.0, 521.28,
        entry_fill_resolved=False, entry_order_id=None))
    with _LogCap() as cap:
        strategy.reconcile_option_entries("ACCT")
    assert "UNRESOLVED" not in cap.text
    assert strategy._option_entry_unresolved == 0
    assert strategy._option_entries_reconciled is True


def test_reconcile_does_not_latch_on_an_empty_store():
    """The store is written LATER in the same cycle by the entry path. Latching on
    empty would leave a contract opened minutes from now unchecked until the next
    restart — the same trap reconcile_broker_floors documents."""
    _reset(history=[])
    strategy.reconcile_option_entries("ACCT")
    assert strategy._option_entries_reconciled is False


def test_reconcile_is_one_shot_per_process():
    _reset(history=[{"order_id": "970609541", "price": 12.55, "status": "Filled"}])
    _unresolved_record()
    strategy.reconcile_option_entries("ACCT")
    calls = []
    strategy.tc.get_historical_orders = lambda acct, since: calls.append(since) or []
    strategy.reconcile_option_entries("ACCT")
    assert calls == [], "second pass re-fetched order history"


# ── the incident, end to end ──────────────────────────────────────────────────

def test_amd_incident_would_not_repeat():
    """AMD 260918C520 with the real numbers: ask 18.90, broker fill 12.55.

    Pre-fix the store held 18.90 and the -50% stop sat at 9.45, so a 9.25 bid
    closed the position. Post-fix the basis is 12.55, the stop is 6.275, and that
    same 9.25 bid is correctly NOT an exit.
    """
    _reset(sig=_cross_up(close=521.28, rsi=60.5), underlying=521.28,
           quote={"bid": 18.40, "ask": 18.90, "last": 18.60, "close": 18.6},
           fill=12.55)
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
    entry = _stored()["entry_price"]
    assert entry == 12.55
    assert strategy._option_exit_reason(9.25, entry, _EXP) is None, \
        "the 2026-09-10 premature stop fired again"
    assert strategy._option_exit_reason(18.90, 18.90, _EXP) is None
    assert "stop loss" in strategy._option_exit_reason(9.25, 18.90, _EXP), \
        "sanity: the OLD basis is what made 9.25 a stop"


def test_option_entry_history_is_now_fully_priced():
    """Schema guarantee going forward: an option entry either carries a resolved
    fill or is counted as unresolved. There is no longer a third state where a
    quote is indistinguishable from an execution — which is what let the n=2
    historical option entries (AMD 09-09, and every one before it) sit in the
    ledger with fill_price null and nobody notice for a month.
    """
    for fill in (12.55, None):
        _reset(sig=_cross_up(), fill=fill)
        strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])
        rec = _stored()
        _, kw = TRADES[-1]
        assert rec["entry_fill_resolved"] is (fill is not None)
        assert (kw["fill_price"] is not None) is (fill is not None)
        # Exactly one of the two states is true, and both are observable.
        assert rec["entry_fill_resolved"] or strategy._option_entry_unresolved == 1


if __name__ == "__main__":
    _tmp = tempfile.mkdtemp()
    strategy._OPT_POSITIONS_PATH = os.path.join(
        _testlib.assert_disposable(_tmp), "options_positions.json")
    strategy._STOPS_PATH = os.path.join(_tmp, "stop_prices.json")
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL {name}: {exc}")
    print("FAILED" if failed else "all passed")
    raise SystemExit(1 if failed else 0)
