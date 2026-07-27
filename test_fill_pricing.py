"""
Tests for fill-price preference when pricing round-trips.

WHY THIS EXISTS: the analyzer priced both legs of every round-trip at the
signal-bar close. A read-only broker audit on 2026-07-27 re-priced all 25 closed
trips at their ACTUAL fills and moved total realized P&L from -$36,296.78 to
-$39,229.12 — the ledger understated the loss by $2,932.34 (8.1%).

54 of 55 matched orders filled away from the logged price, and the drift is
DIRECTIONALLY BIASED, not random noise: entries fill worse and exits fill worse,
so the error accumulates against the account rather than averaging out. One LII
buy slipped $8.10 (559.87 logged, 567.97 filled), which alone moved that trip by
-$802.78. The only winning trade in the ledger, HCA, shrank from +$2,071.28 to
+$1,808.80.

The report's `scope:` line did say "signal-time prices", so this was documented
rather than hidden — but every per-feature verdict drawn from those numbers was
more negative in reality than reported.
"""

import performance_analyzer as pa


def _ev(role, direction, price, fill=None, ts="2026-07-20 10:00:00 EDT",
        symbol="AMD", qty=100):
    return {
        "timestamp": ts, "symbol": symbol, "quantity": qty, "price": price,
        "fill_price": fill, "role": role, "direction": direction,
        "action": "SELL_SHORT" if direction == "short" else "BUY",
        "order_id": f"{symbol}-{role}-{ts}", "notes": "",
        "feature": "short" if direction == "short" else "long_fresh_cross",
        "estimated_entry": False,
    }


# ── _leg_price ────────────────────────────────────────────────────────────────

def test_prefers_fill_price_when_present():
    price, src = pa._leg_price({"price": 476.24, "fill_price": 476.00})
    assert price == 476.00 and src == "fill"


def test_falls_back_to_signal_when_fill_absent():
    price, src = pa._leg_price({"price": 476.24, "fill_price": None})
    assert price == 476.24 and src == "signal"


def test_falls_back_when_key_missing_entirely():
    """Pre-5f26dcd events have no fill_price key at all."""
    price, src = pa._leg_price({"price": 476.24})
    assert price == 476.24 and src == "signal"


def test_zero_fill_price_is_not_treated_as_absent():
    """0.0 is falsy — an `or` fallback would silently discard it. Only None
    means 'we do not have a fill'."""
    price, src = pa._leg_price({"price": 10.0, "fill_price": 0.0})
    assert price == 0.0 and src == "fill"


# ── Pairing uses the resolved prices ──────────────────────────────────────────

def test_trip_priced_at_fill_on_both_legs():
    entry = _ev("entry", "short", 476.24, fill=476.00, ts="2026-07-17 10:00:23 EDT")
    exit_ = _ev("exit", "short", 491.26, fill=491.73, ts="2026-07-17 10:11:33 EDT")
    exit_["action"] = "BUY_TO_COVER"
    closed, _, _ = pa._pair_round_trips([entry, exit_])
    t = closed[0]
    assert t["price_basis"] == "fill"
    assert t["entry_price"] == 476.00 and t["exit_price"] == 491.73
    # real: (476.00 - 491.73) * 100 = -1573.00, vs signal-priced -1502.00
    assert t["pnl"] == -1573.00


def test_trip_priced_at_signal_when_neither_leg_filled():
    entry = _ev("entry", "short", 476.24, ts="2026-07-17 10:00:23 EDT")
    exit_ = _ev("exit", "short", 491.26, ts="2026-07-17 10:11:33 EDT")
    closed, _, _ = pa._pair_round_trips([entry, exit_])
    t = closed[0]
    assert t["price_basis"] == "signal"
    assert t["pnl"] == -1502.00


def test_mixed_basis_is_reported_separately():
    """Entry filled, exit signal-only — the current state of every post-07-20
    trip, since exits never capture a fill. Must NOT round up to 'at fill'."""
    entry = _ev("entry", "short", 476.24, fill=476.00, ts="2026-07-17 10:00:23 EDT")
    exit_ = _ev("exit", "short", 491.26, ts="2026-07-17 10:11:33 EDT")
    closed, _, _ = pa._pair_round_trips([entry, exit_])
    t = closed[0]
    assert t["price_basis"] == "mixed"
    assert t["entry_price_src"] == "fill" and t["exit_price_src"] == "signal"


def test_fill_pricing_makes_a_losing_short_worse():
    """The directional-bias property: on a short, a lower entry fill and a higher
    cover fill both hurt. This is why the error accumulates."""
    sig_entry = _ev("entry", "short", 476.24, ts="2026-07-17 10:00:23 EDT")
    sig_exit = _ev("exit", "short", 491.26, ts="2026-07-17 10:11:33 EDT")
    signal_pnl = pa._pair_round_trips([sig_entry, sig_exit])[0][0]["pnl"]

    fill_entry = _ev("entry", "short", 476.24, fill=476.00, ts="2026-07-17 10:00:23 EDT")
    fill_exit = _ev("exit", "short", 491.26, fill=491.73, ts="2026-07-17 10:11:33 EDT")
    fill_pnl = pa._pair_round_trips([fill_entry, fill_exit])[0][0]["pnl"]

    assert fill_pnl < signal_pnl


def test_long_direction_also_resolves_fills():
    entry = _ev("entry", "long", 559.87, fill=567.97, ts="2026-07-14 09:30:00 EDT",
                symbol="LII", qty=89)
    exit_ = _ev("exit", "long", 545.96, fill=545.04, ts="2026-07-17 12:54:00 EDT",
                symbol="LII", qty=89)
    closed, _, _ = pa._pair_round_trips([entry, exit_])
    t = closed[0]
    assert t["price_basis"] == "fill"
    # (545.04 - 567.97) * 89 = -2040.77 — the real LII loss
    assert round(t["pnl"], 2) == -2040.77


def test_bootstrap_entry_without_fill_still_pairs():
    """Synthetic bootstrap entries carry no fill_price and must not break."""
    entry = _ev("entry", "long", 300.0, ts="2026-07-01 09:30:00 EDT", symbol="AAPL")
    entry["estimated_entry"] = True
    entry["quantity"] = None
    exit_ = _ev("exit", "long", 310.0, ts="2026-07-02 09:30:00 EDT", symbol="AAPL")
    closed, _, _ = pa._pair_round_trips([entry, exit_])
    assert closed[0]["pnl"] == 1000.0        # qty taken from the exit
    assert closed[0]["price_basis"] == "signal"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("OK")
