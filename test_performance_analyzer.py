"""
Unit tests for performance_analyzer — NO network, NO real files.

The pure functions (classification, P&L, FIFO pairing, aggregation, warnings,
ledger merge) are exercised directly with synthetic events. P&L SIGN correctness
and FIFO pairing are the focus — a sign flip here would misreport real money.

Run:  python3 test_performance_analyzer.py
"""

import sys

import performance_analyzer as pa


# ── helpers ───────────────────────────────────────────────────────────────────

def _ev(ts, action, symbol, qty, price, notes="", order_id=None, estimated=False):
    """Build a normalized ledger event the way _normalize would."""
    cls = pa._classify(action, notes)
    assert cls is not None, f"{action} should classify"
    role, direction, feature = cls
    return {
        "timestamp": ts, "action": action, "symbol": symbol, "quantity": qty,
        "price": price, "order_type": "market", "order_id": order_id, "notes": notes,
        "role": role, "direction": direction, "feature": feature,
        "estimated_entry": estimated,
    }


def _closed(feature, pnl, symbol="X", win=None):
    return {"feature": feature, "pnl": pnl, "symbol": symbol,
            "win": (pnl > 0) if win is None else win}


# ── Classification ────────────────────────────────────────────────────────────

def test_classify_actions():
    assert pa._classify("BUY", "EMA cross up, RSI=55")[:2] == ("entry", "long")
    assert pa._classify("BUY", "EMA cross up")[2] == "long_fresh_cross"
    assert pa._classify("BUY", "momentum alignment entry, RSI=52")[2] == "momentum_alignment"
    assert pa._classify("SELL_SHORT", "EMA cross down (short)")[:2] == ("entry", "short")
    assert pa._classify("SELL_SHORT", "x")[2] == "short"
    assert pa._classify("BUY_TO_COVER", "cover")[:2] == ("exit", "short")
    assert pa._classify("SELL", "EMA cross down")[:2] == ("exit", "long")
    assert pa._classify("BUY_TO_OPEN", "call")[:2] == ("entry", "option")
    assert pa._classify("BUY_TO_OPEN", "call")[2] == "option"
    assert pa._classify("SELL_TO_CLOSE", "x")[:2] == ("exit", "option")


def test_classify_rejects_test_and_unknown():
    assert pa._classify("TEST_BUY", "") is None
    assert pa._classify("WITHDRAW", "") is None
    assert pa._classify(None, "") is None


# ── P&L sign correctness (the money-critical part) ────────────────────────────

def test_pnl_long_sign():
    assert pa._pnl("long", 100.0, 110.0, 10) == 100.0     # up = profit
    assert pa._pnl("long", 100.0,  90.0, 10) == -100.0    # down = loss


def test_pnl_short_sign():
    assert pa._pnl("short", 100.0,  90.0, 10) == 100.0    # down = profit
    assert pa._pnl("short", 100.0, 110.0, 10) == -100.0   # up = loss


def test_pnl_option_multiplier():
    assert pa._pnl("option", 2.00, 3.00, 1) == 100.0      # (3-2)*1*100
    assert pa._pnl("option", 2.00, 1.50, 2) == -100.0     # (1.5-2)*2*100


def test_pnl_qty_abs():
    # short positions carry negative held qty upstream; _pnl must use magnitude
    assert pa._pnl("short", 100.0, 90.0, -10) == 100.0


def test_pnl_pct_signs():
    assert abs(pa._pnl_pct("long", 100.0, 110.0) - 0.10) < 1e-9
    assert abs(pa._pnl_pct("short", 100.0, 90.0) - 0.10) < 1e-9   # short gain positive
    assert pa._pnl_pct("long", 0, 10) is None                     # unusable entry


# ── FIFO pairing ──────────────────────────────────────────────────────────────

def test_pair_long_roundtrip():
    events = [
        _ev("2026-07-10 09:30:00 EDT", "BUY",  "AAPL", 10, 100.0, "EMA cross up"),
        _ev("2026-07-12 09:30:00 EDT", "SELL", "AAPL", 10, 110.0, "EMA cross down"),
    ]
    closed, orphans, opens = pa._pair_round_trips(events)
    assert orphans == [] and opens == []
    assert len(closed) == 1
    t = closed[0]
    assert t["feature"] == "long_fresh_cross"
    assert t["pnl"] == 100.0 and t["win"] is True
    assert t["exit_reason"] == "signal"


def test_pair_short_roundtrip_win():
    events = [
        _ev("2026-07-10 09:30:00 EDT", "SELL_SHORT",   "TSLA", 5, 200.0, "EMA cross down (short)"),
        _ev("2026-07-11 09:30:00 EDT", "BUY_TO_COVER", "TSLA", 5, 180.0, "cover"),
    ]
    closed, orphans, opens = pa._pair_round_trips(events)
    assert len(closed) == 1 and not orphans and not opens
    t = closed[0]
    assert t["feature"] == "short"
    assert t["pnl"] == 100.0 and t["win"] is True       # 200->180 short = +100


def test_pair_stop_exit_reason():
    events = [
        _ev("2026-07-10 09:30:00 EDT", "BUY",  "NVDA", 2, 200.0, "EMA cross up"),
        _ev("2026-07-11 09:30:00 EDT", "SELL", "NVDA", 2, 190.0, "trailing stop hit @ 190.00"),
    ]
    closed, _o, _p = pa._pair_round_trips(events)
    assert closed[0]["exit_reason"] == "stop"


def test_pair_fifo_order():
    """Two open longs, one exit closes the OLDEST (FIFO)."""
    events = [
        _ev("2026-07-10 09:30:00 EDT", "BUY",  "MSFT", 10, 100.0, "EMA cross up", order_id="e1"),
        _ev("2026-07-11 09:30:00 EDT", "BUY",  "MSFT", 10, 200.0, "EMA cross up", order_id="e2"),
        _ev("2026-07-12 09:30:00 EDT", "SELL", "MSFT", 10, 150.0, "EMA cross down"),
    ]
    closed, _o, opens = pa._pair_round_trips(events)
    assert len(closed) == 1 and len(opens) == 1
    assert closed[0]["entry_order_id"] == "e1"          # oldest closed
    assert closed[0]["pnl"] == 500.0                    # (150-100)*10
    assert opens[0]["order_id"] == "e2"                 # newest still open


def test_pair_orphan_exit():
    """An exit with no open entry is surfaced, not silently dropped or mispriced."""
    events = [_ev("2026-07-10 09:30:00 EDT", "SELL", "GOOG", 3, 150.0, "EMA cross down")]
    closed, orphans, opens = pa._pair_round_trips(events)
    assert closed == [] and opens == []
    assert len(orphans) == 1 and orphans[0]["symbol"] == "GOOG"


def test_pair_bootstrap_entry_qty_from_exit():
    """A synthetic bootstrap entry has qty=None; pairing takes qty from the exit."""
    entry = _ev("2026-07-13 00:00:00 EDT", "BUY", "DAL", None, 87.0,
                "estimated entry", estimated=True)
    exit_ = _ev("2026-07-14 09:30:00 EDT", "SELL", "DAL", 10, 90.0, "EMA cross down")
    closed, orphans, opens = pa._pair_round_trips([entry, exit_])
    assert len(closed) == 1 and not orphans and not opens
    t = closed[0]
    assert t["qty"] == 10 and t["estimated_entry"] is True
    assert t["pnl"] == 30.0                              # (90-87)*10


# ── Aggregation ───────────────────────────────────────────────────────────────

def test_aggregate_stats():
    trips = [_closed("long_fresh_cross", 100.0, "A"),
             _closed("long_fresh_cross", -40.0, "B"),
             _closed("long_fresh_cross", 60.0, "C"),
             _closed("short", 20.0, "D")]
    agg = pa._aggregate(trips)
    lf = agg["long_fresh_cross"]
    assert lf["count"] == 3 and lf["wins"] == 2
    assert lf["win_rate"] == round(2/3, 4)
    assert lf["total_pnl"] == 120.0 and lf["avg_pnl"] == 40.0
    assert lf["best"]["symbol"] == "A" and lf["worst"]["symbol"] == "B"
    assert agg["option"]["count"] == 0                  # empty bucket
    assert agg["momentum_alignment"]["count"] == 0


# ── Warnings (only >=10 trades AND negative) ─────────────────────────────────

def test_warn_negative_over_threshold():
    trips = [_closed("short", -5.0, f"S{i}") for i in range(10)]
    agg = pa._aggregate(trips)
    warns = pa._build_warnings(agg)
    assert len(warns) == 1 and "Short" in warns[0]


def test_no_warn_below_threshold():
    trips = [_closed("short", -5.0, f"S{i}") for i in range(9)]   # <10
    assert pa._build_warnings(pa._aggregate(trips)) == []


def test_no_warn_when_positive():
    trips = [_closed("short", 5.0, f"S{i}") for i in range(12)]   # >=10 but positive
    assert pa._build_warnings(pa._aggregate(trips)) == []


# ── Ledger merge / dedup ──────────────────────────────────────────────────────

def test_merge_dedups_by_order_id_and_filters_test():
    ledger = {"version": 1, "events": {}, "closed_trips": []}
    raw = [
        ({"timestamp": "2026-07-10 09:30:00 EDT", "action": "BUY", "symbol": "AAPL",
          "quantity": 10, "price": 100.0, "order_type": "market", "order_id": "o1",
          "notes": "EMA cross up"}, "trades.log"),
        # same order_id again (rotation overlap) -> no new event
        ({"timestamp": "2026-07-10 09:30:00 EDT", "action": "BUY", "symbol": "AAPL",
          "quantity": 10, "price": 100.0, "order_type": "market", "order_id": "o1",
          "notes": "EMA cross up"}, "trades.log.1"),
        # TEST artifact -> filtered
        ({"timestamp": "2026-07-10 09:31:00 EDT", "action": "TEST_BUY", "symbol": "AAPL",
          "quantity": 1, "price": 100.0, "order_type": "market", "order_id": "t1",
          "notes": ""}, "trades.log"),
    ]
    added = pa._merge_events(ledger, raw)
    assert added == 1, "dedup + TEST filter -> one event"
    assert len(ledger["events"]) == 1


def test_event_key_composite_when_no_order_id():
    a = {"timestamp": "t", "action": "BUY", "symbol": "X", "quantity": 1,
         "price": 2.0, "order_id": None}
    b = dict(a, price=3.0)
    assert pa._event_key(a) != pa._event_key(b)         # different price -> different key
    assert pa._event_key(dict(a, order_id="z")) == "z"  # order_id wins


def test_inject_bootstrap_when_no_open_entry():
    # HELD has no OPEN entry -> inject; LOGGED still has an open entry -> skip.
    ledger = {"version": 1, "events": {}, "closed_trips": []}
    stops = {
        "HELD":   {"entry_price": 50.0,  "opened": "2026-07-13", "direction": "long"},
        "LOGGED": {"entry_price": 100.0, "opened": "2026-07-13", "direction": "long"},
    }
    open_keys = {("LOGGED", "long")}          # LOGGED is represented by an open entry
    n = pa._inject_bootstrap_entries(ledger, stops, open_keys)
    assert n == 1, "only HELD injected"
    boot = [e for e in ledger["events"].values() if e["estimated_entry"]]
    assert len(boot) == 1 and boot[0]["symbol"] == "HELD" and boot[0]["price"] == 50.0
    # idempotent: a second run injects nothing new
    assert pa._inject_bootstrap_entries(ledger, stops, open_keys) == 0


# ── Stale (pre-analyzer) entry exclusion ──────────────────────────────────────

def test_partition_stale_excludes_old_entries():
    from datetime import datetime
    cutoff = datetime(2026, 7, 1)      # entries before this are pre-analyzer
    events = [
        _ev("2026-04-17 09:30:00 EDT", "BUY",  "OLD",  10, 100.0, "EMA cross up"),   # stale entry
        _ev("2026-07-10 09:30:00 EDT", "BUY",  "NEW",  10, 100.0, "EMA cross up"),   # recent entry
        _ev("2026-04-18 09:30:00 EDT", "SELL", "OLD",  10, 110.0, "EMA cross down"), # stale exit -> dropped
    ]
    recent, stale = pa._partition_stale(events, cutoff)
    assert [e["symbol"] for e in stale] == ["OLD"]           # only the old ENTRY is stale
    assert [e["symbol"] for e in recent] == ["NEW"]          # recent kept; stale exit dropped


def test_stale_old_entry_not_paired_with_recent_exit():
    """An ancient open entry must NOT pair with a recent exit (wrong-era P&L)."""
    from datetime import datetime
    cutoff = datetime(2026, 7, 1)
    events = [
        _ev("2026-04-17 09:30:00 EDT", "BUY",  "AAPL", 10, 100.0, "EMA cross up"),   # stale
        _ev("2026-07-10 09:30:00 EDT", "SELL", "AAPL", 10, 999.0, "EMA cross down"), # recent exit
    ]
    recent, stale = pa._partition_stale(events, cutoff)
    closed, orphans, opens = pa._pair_round_trips(recent)
    assert closed == [], "recent exit must not pair with a pre-analyzer entry"
    assert len(orphans) == 1 and orphans[0]["symbol"] == "AAPL"   # surfaced, not mispriced
    assert len(stale) == 1


# ── SPY close lookup ──────────────────────────────────────────────────────────

def test_spy_close_on_or_before():
    bars = [{"date": "2026-07-10T00:00:00Z", "close": 500.0},
            {"date": "2026-07-13T00:00:00Z", "close": 510.0}]
    assert pa._spy_close_on_or_before(bars, "2026-07-14") == 510.0   # nearest prior
    assert pa._spy_close_on_or_before(bars, "2026-07-10") == 500.0   # exact
    assert pa._spy_close_on_or_before(bars, "2026-07-01") is None    # before all


# ── runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
