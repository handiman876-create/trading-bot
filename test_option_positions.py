"""
Unit tests for the options position store — NO network.

WHAT THIS PROTECTS: exits used to resolve `held` against an occ_symbol
RECOMPUTED each cycle from _atm_strike(current underlying). A move of more than
half a strike increment renamed the lookup key, _current_position returned 0, and
the exit branch became unreachable — the contract rode to expiration unmanaged.
SPY260717C00540000 (opened 2026-07-01, the only options trade this bot has ever
placed) died exactly that way. The fix persists the contract at entry and drives
every exit off the STORED symbol.

Covers: entry stores the symbol; a price move still resolves the stored contract
rather than a recomputed one; a successful exit clears the store; expiry drops
the record instead of trying to sell something that no longer exists; plus the
legacy-adoption path and ask/bid entry pricing.

All doubles are in-process: tc.get_historical / get_option_quote /
find_option_symbol / place_option_order and log_trade are stubbed, and
_OPT_POSITIONS_PATH is redirected to a temp file, so the suite never touches the
network or live state.

Run:  python3 test_option_positions.py   (or via pytest)
"""

import logging
import os
import tempfile
from datetime import date, timedelta

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


# ── doubles ───────────────────────────────────────────────────────────────────
ORDERS = []          # every place_option_order call lands here
QUOTES = {}          # occ_symbol -> normalized quote dict


def _bars(closes):
    """Minimal daily-bar history; only the closes drive the indicators."""
    return [{"open": c, "high": c, "low": c, "close": c, "volume": 1_000_000}
            for c in closes]


def _reset(monkeypatch=None, sig=None, underlying=100.0, quote=None):
    """Install the doubles and clear all module state. `sig` overrides the
    computed indicator dict outright so each test states its own signal."""
    ORDERS.clear()
    QUOTES.clear()
    strategy._save_option_positions({})
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy._option_expiry_drops = 0
    strategy._option_orphan_drops = 0
    strategy._option_adoptions = 0

    q = quote or {"symbol": "X", "last": 5.00, "bid": 4.90, "ask": 5.20, "close": 5.0}

    # The 30-minute persistence rule is exercised by test_cross_sustain.py. Here it
    # would just mean every entry test asserts on a PENDING clock instead of on the
    # store, so it is switched off to isolate what this module is actually testing.
    strategy.config.CROSS_SUSTAIN_MINUTES = 0
    strategy._cross_first_seen.clear()
    strategy._cross_confirmed.clear()
    strategy._cross_gap_logged.clear()
    strategy._entry_delay_logged.clear()

    strategy.tc.get_historical = lambda s, days=90: _bars([underlying] * 60)
    strategy.ind.compute_indicators = lambda *a, **k: dict(sig or {})
    strategy.tc.get_option_quote = lambda occ: QUOTES.get(occ, q)
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
        """These tests stub module globals on `strategy` in place. Restore them so
        the doubles cannot leak into whatever module pytest collects next."""
        saved = {
            "hist":  strategy.tc.get_historical,
            "ci":    strategy.ind.compute_indicators,
            "oq":    strategy.tc.get_option_quote,
            "fos":   strategy.tc.find_option_symbol,
            "poo":   strategy.tc.place_option_order,
            "log":   strategy.log_trade,
            "xlog":  strategy._log_exit_trade,
            "ea":    strategy.mh.entries_allowed,
            "sus":   getattr(strategy.config, "CROSS_SUSTAIN_MINUTES", 0),
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
        strategy._cross_first_seen.clear()
        strategy._cross_confirmed.clear()
except ImportError:
    pass


def _bullish_sig(close, rsi=50.0):
    """A signal that satisfies the call-entry gate: fresh bullish edge, RSI mid."""
    return {"close": close, "rsi": rsi, "ema_short": close * 1.02,
            "ema_long": close, "bullish_cross": True, "bearish_cross": False}


def _bearish_sig(close, rsi=50.0):
    """Fast EMA below slow — the exit STATE for a long call."""
    return {"close": close, "rsi": rsi, "ema_short": close * 0.98,
            "ema_long": close, "bullish_cross": False, "bearish_cross": True}


_EXP = "2026-08-21"


# ── 1. entry stores the symbol ────────────────────────────────────────────────
def test_entry_stores_symbol():
    _reset(sig=_bullish_sig(540.0))
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])

    assert [o[0] for o in ORDERS] == ["buy_to_open"], "entry should place one order"
    store = strategy._load_option_positions()
    assert "SPY_call" in store, "entry MUST persist the contract"

    rec = store["SPY_call"]
    assert rec["occ_symbol"] == ORDERS[0][1], "stored symbol must be the one transacted"
    assert rec["expiration"] == _EXP
    assert rec["opt_type"] == "call"
    assert rec["strike"] == 540.0
    assert rec["contracts"] == config.OPTIONS_CONTRACTS
    assert rec["underlying_entry"] == 540.0
    assert rec["entry_date"] == date.today().isoformat()


def test_entry_price_uses_ask_not_last():
    """A buy lifts the ASK. `last` can be a stale print outside the spread."""
    _reset(sig=_bullish_sig(540.0),
           quote={"last": 8.93, "bid": 9.25, "ask": 9.45, "close": 8.9})
    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])

    rec = strategy._load_option_positions()["SPY_call"]
    assert rec["entry_price"] == 9.45, "entry must record the ask, not last (8.93)"


def test_exit_price_uses_bid():
    """A sell hits the BID — the other side of the same correction."""
    occ = "SPY 260821C540"
    _reset(sig=_bearish_sig(540.0),
           quote={"last": 8.93, "bid": 9.25, "ask": 9.45, "close": 8.9})
    strategy._save_option_position("SPY_call", strategy._option_record(
        occ, 9.45, _EXP, "call", 540.0, 540.0))

    with _LogCap() as cap:
        strategy.evaluate_option("SPY", _EXP, "call", "ACCT",
                                 [{"symbol": occ, "quantity": 1}])
    assert "bid=9.25" in cap.text and "ask=9.45" in cap.text, \
        f"both sides of the book should be logged; got:\n{cap.text}"


# ── 2. price move: uses stored symbol, not a recompute ───────────────────────
def test_price_move_uses_stored_symbol():
    """THE REGRESSION TEST. Entry at 540, underlying runs to 600. A recompute
    would look up the 600 strike and find nothing; the stored symbol must win."""
    occ = "SPY 260821C540"
    _reset(sig=_bearish_sig(600.0))          # bearish STATE -> should exit
    strategy._save_option_position("SPY_call", strategy._option_record(
        occ, 8.50, _EXP, "call", 540.0, 541.20))

    # Broker reports the ORIGINAL contract only — nothing at the 600 strike.
    positions = [{"symbol": occ, "quantity": 1}]

    with _LogCap() as cap:
        strategy.evaluate_option("SPY", _EXP, "call", "ACCT", positions)

    assert [o[0] for o in ORDERS] == ["sell_to_close"], \
        "the stored contract must still be exitable after a 60-point move"
    assert ORDERS[0][1] == occ, "the sell must target the STORED symbol"
    # The STALE OPTION RECOVERED counter that used to be asserted here was retired
    # on 2026-08-06 — it was observability over a path that no longer runs, and it
    # fired on every poll of a healthy position. What it was protecting is exactly
    # the two assertions above, which is why they are the ones that stayed.
    assert "STALE OPTION RECOVERED" not in cap.text, \
        "the retired recompute must not log anything"


def test_pre_fix_behaviour_would_have_missed_it():
    """Pins the actual defect: with the store empty, the recomputed symbol does
    not match the held contract, held reads 0, and no exit is attempted."""
    occ = "SPY 260821C540"
    _reset(sig=_bearish_sig(600.0))          # store deliberately left empty
    positions = [{"symbol": occ, "quantity": 1}]

    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", positions)

    assert not [o for o in ORDERS if o[0] == "sell_to_close"], \
        "without a stored symbol the recompute cannot find the contract"


# ── 3. exit clears the stored position ────────────────────────────────────────
def test_exit_clears_stored_position():
    occ = "SPY 260821C540"
    _reset(sig=_bearish_sig(540.0))
    strategy._save_option_position("SPY_call", strategy._option_record(
        occ, 8.50, _EXP, "call", 540.0, 540.0))

    strategy.evaluate_option("SPY", _EXP, "call", "ACCT",
                             [{"symbol": occ, "quantity": 1}])

    assert [o[0] for o in ORDERS] == ["sell_to_close"]
    assert "SPY_call" not in strategy._load_option_positions(), \
        "a filled exit MUST clear the store or the pair is pinned forever"


def test_failed_exit_keeps_stored_position():
    """A rejected sell must NOT clear the store — we still hold the contract."""
    occ = "SPY 260821C540"
    _reset(sig=_bearish_sig(540.0))
    strategy.tc.place_option_order = lambda *a, **k: None     # broker rejects
    strategy._save_option_position("SPY_call", strategy._option_record(
        occ, 8.50, _EXP, "call", 540.0, 540.0))

    strategy.evaluate_option("SPY", _EXP, "call", "ACCT",
                             [{"symbol": occ, "quantity": 1}])

    assert "SPY_call" in strategy._load_option_positions(), \
        "a failed close must leave the position under management"


# ── 4. expiry handling ────────────────────────────────────────────────────────
def test_expired_position_is_dropped_not_sold():
    occ = "SPY 260717C540"
    past = (date.today() - timedelta(days=1)).isoformat()
    _reset(sig=_bearish_sig(540.0))
    strategy._save_option_position("SPY_call", strategy._option_record(
        occ, 8.50, past, "call", 540.0, 540.0))

    with _LogCap() as cap:
        strategy.evaluate_option("SPY", _EXP, "call", "ACCT",
                                 [{"symbol": occ, "quantity": 1}])

    assert ORDERS == [], "an expired contract must not be sold — it no longer exists"
    assert "SPY_call" not in strategy._load_option_positions()
    assert strategy._option_expiry_drops == 1
    assert "OPTION POSITION CLEARED" in cap.text


def test_expiration_day_is_still_tradeable():
    """Equality is not expiry — a contract trades through the close on its
    expiration date. Dropping it that morning would abandon a sellable position."""
    occ = "SPY 260821C540"
    today = date.today().isoformat()
    _reset(sig=_bearish_sig(540.0))
    strategy._save_option_position("SPY_call", strategy._option_record(
        occ, 8.50, today, "call", 540.0, 540.0))

    strategy.evaluate_option("SPY", _EXP, "call", "ACCT",
                             [{"symbol": occ, "quantity": 1}])

    assert [o[0] for o in ORDERS] == ["sell_to_close"], \
        "expiration DAY must still allow an exit"
    assert strategy._option_expiry_drops == 0


def test_unparseable_expiration_fails_open():
    """A cosmetic date problem must not strand a real position."""
    assert strategy._option_expired("not-a-date") is False
    assert strategy._option_expired(None) is False


# ── 5. broker orphan + legacy adoption ────────────────────────────────────────
def test_orphan_is_dropped_when_broker_has_nothing():
    occ = "SPY 260821C540"
    _reset(sig=_bearish_sig(540.0))
    strategy._save_option_position("SPY_call", strategy._option_record(
        occ, 8.50, _EXP, "call", 540.0, 540.0))

    strategy.evaluate_option("SPY", _EXP, "call", "ACCT", [])   # broker flat

    assert ORDERS == []
    assert "SPY_call" not in strategy._load_option_positions()
    assert strategy._option_orphan_drops == 1


def test_legacy_position_is_adopted():
    """A contract opened before the store existed gets folded in, so the exit
    path can manage it instead of losing it at the next strike move."""
    _reset(sig=_bullish_sig(540.0))
    occ = strategy.tc.find_option_symbol("SPY", _EXP, 540.0, "call")

    with _LogCap() as cap:
        strategy.evaluate_option("SPY", _EXP, "call", "ACCT",
                                 [{"symbol": occ, "quantity": 1}])

    store = strategy._load_option_positions()
    assert store.get("SPY_call", {}).get("occ_symbol") == occ
    assert strategy._option_adoptions == 1
    assert "OPTION POSITION ADOPTED" in cap.text
    assert not [o for o in ORDERS if o[0] == "buy_to_open"], \
        "adoption must not double-buy a contract we already hold"


# ── 6. store mechanics ────────────────────────────────────────────────────────
def test_store_isolates_watchlist_pairs():
    _reset()
    strategy._save_option_position("SPY_call", {"occ_symbol": "A"})
    strategy._save_option_position("AAPL_put", {"occ_symbol": "B"})
    strategy._close_option_position("SPY_call")

    store = strategy._load_option_positions()
    assert "SPY_call" not in store
    assert store["AAPL_put"]["occ_symbol"] == "B", \
        "clearing one pair must not disturb the other"


def test_close_missing_key_is_noop():
    _reset()
    strategy._save_option_position("AAPL_put", {"occ_symbol": "B"})
    strategy._close_option_position("SPY_call")      # never stored
    assert "AAPL_put" in strategy._load_option_positions()


def test_option_key_normalizes_type_case():
    assert strategy._option_key("SPY", "CALL") == "SPY_call"
    assert strategy._option_key("SPY", "call") == "SPY_call"


def test_fill_price_falls_back_when_side_missing():
    """A one-sided book degrades to last, then to the other side — never to 0."""
    assert strategy._option_fill_price({"ask": None, "last": 7.0, "bid": 6.5}, "entry") == 7.0
    assert strategy._option_fill_price({"ask": None, "last": None, "bid": 6.5}, "entry") == 6.5
    assert strategy._option_fill_price({"bid": None, "last": 7.0, "ask": 7.5}, "exit") == 7.0
    assert strategy._option_fill_price(None, "entry") == 0.0


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
