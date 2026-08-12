"""
Unit tests for the options premium-based exits (+50% / −50% / near-expiry) — NO network.

WHAT THIS PROTECTS: before 2026-08-06 the ONLY way out of an option was the
underlying's EMA state flipping. That is a view on the stock, not on the
contract: a call can lose half its premium to theta plus a modest adverse move
while the EMAs stay bullish, and the position then rides to expiration. NVDA
260821C220 was −24.5% on its second day with the EMA state still bullish and
nothing in the code that could ever have cut it.

The rules live in _option_exit_reason so the thresholds are testable without
stubbing a broker, a quote feed and an indicator stack.

THE LANDMINE THESE PIN: the adoption path stores entry_price 0.0 (it cannot know
what was paid). A naive `bid >= entry * 1.50` reads `0 >= 0` as True and closes
every adopted contract on sight. test_adopted_zero_entry_* are the regression.

Priced off the BID, matching _option_fill_price(quote, "exit") — what a sell
actually receives. Options round-trip spreads run ~2.1%, so a mid-based trigger
promises a fill the book will not give.

Run:  python3 test_option_exits.py   (or via pytest)
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


def _bars(closes):
    return [{"open": c, "high": c, "low": c, "close": c, "volume": 1_000_000}
            for c in closes]


def _reset(sig=None, quote=None):
    """Install the doubles and clear module state. Mirrors test_option_positions."""
    ORDERS.clear()
    strategy._save_option_positions({})
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy._option_expiry_drops = 0
    strategy._option_orphan_drops = 0
    strategy._option_adoptions    = 0
    strategy._option_target_exits = 0
    strategy._option_stop_exits   = 0
    strategy._option_expiry_exits = 0

    config.ENABLE_OPTION_EXIT_TARGETS = True
    config.OPTION_PROFIT_TARGET_PCT   = 1.50
    config.OPTION_STOP_LOSS_PCT       = 0.50
    config.OPTION_MIN_DAYS_TO_EXPIRY  = 5

    q = quote or {"symbol": "X", "last": 8.00, "bid": 8.00, "ask": 8.20, "close": 8.0}

    strategy.config.CROSS_SUSTAIN_MINUTES = 0
    strategy._cross_first_seen.clear()
    strategy._cross_confirmed.clear()
    strategy._cross_gap_logged.clear()
    strategy._entry_delay_logged.clear()

    strategy.tc.get_historical = lambda s, days=90: _bars([100.0] * 60)
    strategy.ind.compute_indicators = lambda *a, **k: dict(sig or {})
    strategy.tc.get_option_quote = lambda occ: q
    strategy.tc.find_option_symbol = (
        lambda sym, exp, strike, ot: f"{sym} {exp.replace('-', '')[2:]}"
                                     f"{'C' if ot.lower() == 'call' else 'P'}{int(strike)}")
    strategy.tc.place_option_order = lambda acct, occ, side, qty, **k: (
        ORDERS.append((side, occ, qty)) or {"order": {"id": f"o{len(ORDERS)}"}})
    strategy.log_trade = lambda *a, **k: None
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
        strategy.tc.get_historical      = saved["hist"]
        strategy.ind.compute_indicators = saved["ci"]
        strategy.tc.get_option_quote    = saved["oq"]
        strategy.tc.find_option_symbol  = saved["fos"]
        strategy.tc.place_option_order  = saved["poo"]
        strategy.log_trade              = saved["log"]
        strategy._log_exit_trade        = saved["xlog"]
        strategy.mh.entries_allowed     = saved["ea"]
        strategy.config.CROSS_SUSTAIN_MINUTES = saved["sus"]
        config.ENABLE_OPTION_EXIT_TARGETS = saved["en"]
        config.OPTION_PROFIT_TARGET_PCT   = saved["tgt"]
        config.OPTION_STOP_LOSS_PCT       = saved["stp"]
        config.OPTION_MIN_DAYS_TO_EXPIRY  = saved["dte"]
        strategy._cross_first_seen.clear()
        strategy._cross_confirmed.clear()
except ImportError:
    pass


def _bullish_sig(close=100.0, rsi=50.0):
    """EMA state BULLISH — the state exit is NOT satisfied, so anything that
    closes the position here had to come from the premium rules."""
    return {"close": close, "rsi": rsi, "ema_short": close * 1.02,
            "ema_long": close, "bullish_cross": False, "bearish_cross": False}


def _far()  -> str: return (date.today() + timedelta(days=40)).isoformat()
def _near() -> str: return (date.today() + timedelta(days=3)).isoformat()

def _sessions_out(n: int) -> str:
    """An expiration exactly ``n`` TRADING sessions from today.

    Calendar offsets cannot express this test any more: since the expiry rule
    counts sessions, `today + 6 calendar days` is 4–6 sessions depending on which
    weekday the suite runs on, so a calendar-based boundary test passes on a
    Tuesday and fails on a Friday. Anchor on sessions and it is weekday-proof.
    """
    return mh.shift_trading_days(date.today(), n).isoformat()

OCC = "NVDA 260821C220"


def _hold(entry, exp, occ=OCC):
    strategy._save_option_position("NVDA_call", strategy._option_record(
        occ, entry, exp, "call", 220.0, 220.9))
    return [{"symbol": occ, "quantity": 1}]


# ── 1. the pure predicate ─────────────────────────────────────────────────────
def test_reason_stop_loss():
    assert "stop loss" in strategy._option_exit_reason(4.07, 8.15, _far())


def test_reason_stop_loss_is_inclusive():
    """Exactly −50% must fire; the rule is <=, not <."""
    assert "stop loss" in strategy._option_exit_reason(4.075, 8.15, _far())


def test_reason_profit_target():
    assert "profit target" in strategy._option_exit_reason(12.30, 8.15, _far())


def test_reason_profit_target_is_inclusive():
    assert "profit target" in strategy._option_exit_reason(12.225, 8.15, _far())


def test_reason_none_in_between():
    """NVDA's actual 2026-08-06 state: −24.5%, well inside both thresholds."""
    assert strategy._option_exit_reason(6.15, 8.15, _far()) is None


def test_reason_near_expiry():
    assert "near expiry" in strategy._option_exit_reason(8.15, 8.15, _near())


def test_reason_expiry_boundary():
    """<= MIN_DAYS *sessions* fires; one session more does not."""
    at   = _sessions_out(5)
    over = _sessions_out(6)
    assert "near expiry" in strategy._option_exit_reason(8.15, 8.15, at)
    assert strategy._option_exit_reason(8.15, 8.15, over) is None


def test_expiry_counts_sessions_not_calendar_days():
    """The whole point of the 2026-08-12 change: a weekend must not be counted.

    QQQ 260821C715 as it actually stood — expiring Fri 2026-08-21, evaluated from
    Wed 2026-08-12. Calendar arithmetic first goes <= 5 on SUNDAY 08-16 (untradeable,
    so the close defers to Mon 08-17); sessions put it on Fri 08-14.
    """
    exp = date(2026, 8, 21)
    assert (exp - date(2026, 8, 16)).days == 5          # calendar rule: a Sunday
    assert not mh._is_trading_day(date(2026, 8, 16))    # ...which we cannot trade

    assert mh.trading_days_until(exp, date(2026, 8, 12)) == 7   # Wed: no exit
    assert mh.trading_days_until(exp, date(2026, 8, 13)) == 6   # Thu: no exit
    assert mh.trading_days_until(exp, date(2026, 8, 14)) == 5   # Fri: FIRES
    assert mh.trading_days_until(exp, date(2026, 8, 17)) == 4   # Mon: already gone


def test_expiry_skips_holidays_not_just_weekends():
    """Thanksgiving 2026-11-26 (Thu) must not count as a session."""
    exp = date(2026, 12, 4)                              # the Friday after
    assert mh.is_holiday(date(2026, 11, 26))
    # 11-27 Fri, 11-30 Mon, 12-01, 12-02, 12-03, 12-04 = 6 sessions; the Thursday
    # holiday and the 11-28/29 weekend are all skipped.
    assert mh.trading_days_until(exp, date(2026, 11, 25)) == 6
    assert (exp - date(2026, 11, 25)).days == 9          # calendar would say 9


def test_expiry_day_itself_is_tradeable():
    """0 sessions left = expires today, and we can still sell into the close —
    mirrors _option_expired's deliberate `<` rather than `<=`."""
    assert mh.trading_days_until(date(2026, 8, 21), date(2026, 8, 21)) == 0
    assert mh.trading_days_until(date(2026, 8, 20), date(2026, 8, 21)) == 0


def test_reason_stop_wins_over_target():
    """Contradictory thresholds (bad config/data) must resolve to the loss."""
    config.OPTION_PROFIT_TARGET_PCT = 0.10     # target below the stop
    try:
        assert "stop loss" in strategy._option_exit_reason(1.00, 8.15, _far())
    finally:
        config.OPTION_PROFIT_TARGET_PCT = 1.50


def test_reason_disabled_toggle():
    config.ENABLE_OPTION_EXIT_TARGETS = False
    try:
        assert strategy._option_exit_reason(0.01, 8.15, _near()) is None
    finally:
        config.ENABLE_OPTION_EXIT_TARGETS = True


def test_reason_unparseable_expiry_does_not_liquidate():
    """A cosmetic date problem must not force a sale — _option_expired fails open
    for the same reason."""
    assert strategy._option_exit_reason(8.15, 8.15, "not-a-date") is None
    assert strategy._trading_days_to_expiry("not-a-date") is None


# ── 2. THE LANDMINE: adopted contracts store entry_price 0.0 ──────────────────
def test_adopted_zero_entry_skips_target_and_stop():
    """0.0 entry would make `bid >= 0 * 1.50` read 0 >= 0 == True."""
    assert strategy._option_exit_reason(0.0, 0.0, _far()) is None
    assert strategy._option_exit_reason(9.99, 0.0, _far()) is None
    assert strategy._option_exit_reason(0.01, 0.0, _far()) is None


def test_adopted_zero_entry_still_honours_expiry():
    """Expiry needs no entry price, so it must stay armed on adopted contracts."""
    assert "near expiry" in strategy._option_exit_reason(9.99, 0.0, _near())


def test_missing_entry_price_skips_target_and_stop():
    assert strategy._option_exit_reason(4.00, None, _far()) is None


def test_zero_bid_skips_target_and_stop():
    """A missing quote degrades _option_fill_price to 0.0 — that is 'no data',
    not 'worthless', and must not be read as a −100% stop."""
    assert strategy._option_exit_reason(0.0, 8.15, _far()) is None


# ── 3. end-to-end through evaluate_option ─────────────────────────────────────
def test_stop_loss_closes_position():
    _reset(sig=_bullish_sig(), quote={"bid": 4.00, "ask": 4.20, "last": 4.10})
    positions = _hold(8.15, _far())

    with _LogCap() as cap:
        strategy.evaluate_option("NVDA", _far(), "call", "ACCT", positions)

    assert [o[0] for o in ORDERS] == ["sell_to_close"], \
        "a −50% contract must be closed even with the EMA state still bullish"
    assert ORDERS[0][1] == OCC, "the sell must target the STORED symbol"
    assert strategy._option_stop_exits == 1
    assert "OPTION TARGET EXIT" in cap.text and "stop loss" in cap.text
    assert "NVDA_call" not in strategy._load_option_positions(), \
        "a closed contract must be cleared from the store"


def test_profit_target_closes_position():
    _reset(sig=_bullish_sig(), quote={"bid": 12.50, "ask": 12.70, "last": 12.60})
    positions = _hold(8.15, _far())

    strategy.evaluate_option("NVDA", _far(), "call", "ACCT", positions)

    assert [o[0] for o in ORDERS] == ["sell_to_close"]
    assert strategy._option_target_exits == 1


def test_near_expiry_closes_position():
    _reset(sig=_bullish_sig(), quote={"bid": 8.00, "ask": 8.20, "last": 8.10})
    positions = _hold(8.15, _near())

    strategy.evaluate_option("NVDA", _near(), "call", "ACCT", positions)

    assert [o[0] for o in ORDERS] == ["sell_to_close"]
    assert strategy._option_expiry_exits == 1


def test_healthy_position_is_left_alone():
    """NVDA as it actually stood on 2026-08-06: −24.5%, 15 days out, EMAs bullish."""
    _reset(sig=_bullish_sig(), quote={"bid": 6.15, "ask": 6.20, "last": 6.17})
    positions = _hold(8.15, _far())

    strategy.evaluate_option("NVDA", _far(), "call", "ACCT", positions)

    assert not ORDERS, "nothing should close a position inside every threshold"
    assert "NVDA_call" in strategy._load_option_positions()
    assert (strategy._option_stop_exits, strategy._option_target_exits,
            strategy._option_expiry_exits) == (0, 0, 0)


def test_premium_exit_precedes_state_exit():
    """The premium rules run FIRST. With a bearish state AND a stop breach, the
    close must be attributed to the stop, not to the state exit."""
    _reset(sig={"close": 100.0, "rsi": 50.0, "ema_short": 98.0, "ema_long": 100.0,
                "bullish_cross": False, "bearish_cross": True},
           quote={"bid": 4.00, "ask": 4.20, "last": 4.10})
    positions = _hold(8.15, _far())

    with _LogCap() as cap:
        strategy.evaluate_option("NVDA", _far(), "call", "ACCT", positions)

    assert len(ORDERS) == 1, "the position must be sold exactly once"
    assert strategy._option_stop_exits == 1
    assert "STATE-ONLY EXIT" not in cap.text, \
        "the state path must not also claim this exit"


def test_disabled_toggle_end_to_end():
    _reset(sig=_bullish_sig(), quote={"bid": 0.50, "ask": 0.60, "last": 0.55})
    config.ENABLE_OPTION_EXIT_TARGETS = False
    try:
        positions = _hold(8.15, _far())
        strategy.evaluate_option("NVDA", _far(), "call", "ACCT", positions)
        assert not ORDERS, "the toggle must switch the whole block off"
    finally:
        config.ENABLE_OPTION_EXIT_TARGETS = True


def test_adopted_contract_not_closed_end_to_end():
    """The 0.0-entry landmine, driven through the real code path."""
    _reset(sig=_bullish_sig(), quote={"bid": 6.15, "ask": 6.20, "last": 6.17})
    positions = _hold(0.0, _far())

    strategy.evaluate_option("NVDA", _far(), "call", "ACCT", positions)

    assert not ORDERS, "an adopted contract (entry 0.0) must not self-liquidate"


# ── standalone runner (mirrors the other test modules) ────────────────────────
if __name__ == "__main__":
    _tmp = tempfile.mkdtemp()
    strategy._OPT_POSITIONS_PATH = _testlib.assert_disposable(
        os.path.join(_tmp, "options_positions.json"))
    strategy._STOPS_PATH = _testlib.assert_disposable(
        os.path.join(_tmp, "stop_prices.json"))

    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"  PASS  {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"  FAIL  {_name}: {_e}")
    print("OK" if not _failed else f"{_failed} FAILED")
    raise SystemExit(1 if _failed else 0)
