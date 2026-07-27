"""
Tests for ledger/broker open-position reconciliation.

WHY THIS EXISTS: the weekly report claimed 10 open entries while the account
held 4 positions. Ledger entries are PER-FILL and broker positions are
PER-SYMBOL aggregates, so the counts are not directly comparable — AAPL alone
had three open entries totalling 9 shares against a real position of 6. The
stale rows (a closed SPY long, an expired SPY call whose exit was never logged,
META, TSLA, and one duplicate AAPL fill) sat as phantom opens forever, and the
open side of the report could not be trusted.

The pairing/closed-trip side must be unaffected: reconciliation only ever
retires UNPAIRED entries, and it marks them "reconciled" rather than closing
them, because no exit price is known and inventing one would fabricate P&L.
"""

import performance_analyzer as pa


def _entry(symbol, direction, qty, ts, price=100.0):
    return {
        "timestamp": ts, "action": "BUY" if direction == "long" else "SELL_SHORT",
        "symbol": symbol, "quantity": qty, "price": price, "order_type": "market",
        "order_id": f"{symbol}-{ts}", "notes": "", "role": "entry",
        "direction": direction, "feature": "long_fresh_cross",
        "estimated_entry": False,
    }


def _ledger(entries):
    return {"version": 1, "events": {e["order_id"]: e for e in entries},
            "closed_trips": []}


def test_symbol_absent_from_broker_is_reconciled():
    e = _entry("TSLA", "long", 2, "2026-07-09 14:14:59 EDT")
    led = _ledger([e])
    kept, rec = pa._reconcile_open_entries(led, [e], positions=[])
    assert kept == []
    assert len(rec) == 1
    assert rec[0]["symbol"] == "TSLA"
    assert "not in broker positions" in rec[0]["reason"]


def test_quantity_mismatch_trims_oldest_first():
    """AAPL: three open fills of 3 = 9 shares, broker holds 6. The OLDEST fill is
    retired and the two newer ones survive — FIFO, matching how _pair_round_trips
    consumes the queue."""
    old = _entry("AAPL", "long", 3, "2026-06-17 09:57:57 EDT")
    mid = _entry("AAPL", "long", 3, "2026-07-06 09:30:09 EDT")
    new = _entry("AAPL", "long", 3, "2026-07-06 09:30:10 EDT")
    led = _ledger([old, mid, new])
    kept, rec = pa._reconcile_open_entries(
        led, [old, mid, new], positions=[{"symbol": "AAPL", "quantity": 6}])
    assert len(kept) == 2 and len(rec) == 1
    assert rec[0]["entry_ts"] == "2026-06-17 09:57:57 EDT"
    assert sum(abs(k["quantity"]) for k in kept) == 6


def test_partial_overlap_keeps_the_entry():
    """When no WHOLE entry explains the gap (ledger 5, broker 3, single entry of
    5), the entry is kept. Over-reporting an open position is recoverable;
    retiring one we actually hold is not."""
    e = _entry("AMD", "long", 5, "2026-07-20 09:31:00 EDT")
    led = _ledger([e])
    kept, rec = pa._reconcile_open_entries(
        led, [e], positions=[{"symbol": "AMD", "quantity": 3}])
    assert rec == [], "a partial gap must not retire a held entry"
    assert len(kept) == 1
    assert not e.get("reconciled")


def test_matching_positions_are_left_alone():
    """The real open positions must survive untouched."""
    nvda = _entry("NVDA", "long", 238, "2026-07-13 09:32:11 EDT")
    pltr = _entry("PLTR", "short", 392, "2026-07-23 10:33:29 EDT")
    avgo = _entry("AVGO", "short", 125, "2026-07-24 10:00:33 EDT")
    led = _ledger([nvda, pltr, avgo])
    kept, rec = pa._reconcile_open_entries(led, [nvda, pltr, avgo], positions=[
        {"symbol": "NVDA", "quantity": 238},
        {"symbol": "PLTR", "quantity": -392},
        {"symbol": "AVGO", "quantity": -125},
    ])
    assert rec == []
    assert len(kept) == 3
    assert not any(e.get("reconciled") for e in led["events"].values())


def test_short_sign_is_respected():
    """A short entry matches a NEGATIVE broker quantity. If the sign convention
    were dropped, every open short would look unheld and be wrongly retired."""
    pltr = _entry("PLTR", "short", 392, "2026-07-23 10:33:29 EDT")
    led = _ledger([pltr])
    kept, rec = pa._reconcile_open_entries(
        led, [pltr], positions=[{"symbol": "PLTR", "quantity": -392}])
    assert rec == [] and len(kept) == 1


def test_none_positions_is_a_no_op():
    """A FAILED fetch must never retire anything: unknown != flat. This is the
    2026-07-16 CRL/LII lesson — there a 503 read as 'flat' and double-entered;
    here it would retire every genuinely open entry in the ledger."""
    e = _entry("NVDA", "long", 238, "2026-07-13 09:32:11 EDT")
    led = _ledger([e])
    kept, rec = pa._reconcile_open_entries(led, [e], positions=None)
    assert rec is None, "a failed fetch must report SKIPPED, not a clean reconcile"
    assert len(kept) == 1
    assert not e.get("reconciled")


def test_marks_persist_in_the_ledger():
    """The retired entry must be marked in ledger['events'] itself, or the next
    run would re-reconcile and re-report the same entries forever."""
    e = _entry("META", "long", 1, "2026-07-07 09:30:21 EDT")
    led = _ledger([e])
    pa._reconcile_open_entries(led, [e], positions=[])
    stored = led["events"][e["order_id"]]
    assert stored.get("reconciled"), "mark did not reach the ledger"
    assert stored["reconciled"]["broker_qty"] == 0


def test_reconciled_entries_drop_out_of_pairing():
    """A reconciled entry is excluded from the pairing pool on subsequent runs,
    so it can neither show as open nor mispair with a later exit."""
    e = _entry("SPY", "long", 1, "2026-07-01 09:45:50 EDT")
    led = _ledger([e])
    pa._reconcile_open_entries(led, [e], positions=[])
    recent, stale = pa._partition_stale(list(led["events"].values()),
                                        pa._reference_now().replace(year=2000))
    assert recent == [] and stale == []


def test_reconcile_never_creates_closed_trips():
    """Retired != closed. No P&L may be booked for an entry with no known exit."""
    e = _entry("SPY260717C00540000", "option", 1, "2026-07-01 09:45:55 EDT", price=195.37)
    e["direction"] = "option"
    led = _ledger([e])
    kept, rec = pa._reconcile_open_entries(led, [e], positions=[])
    assert len(rec) == 1 and kept == []
    assert led["closed_trips"] == []
    assert "pnl" not in led["events"][e["order_id"]]


def test_option_long_sign_matches_broker():
    """A held option is long-signed at the broker but carries direction 'option'
    in the ledger; it must reconcile as held, not be retired."""
    e = _entry("SPY260815C00560000", "option", 2, "2026-08-01 09:45:55 EDT")
    e["direction"] = "option"
    led = _ledger([e])
    kept, rec = pa._reconcile_open_entries(
        led, [e], positions=[{"symbol": "SPY260815C00560000", "quantity": 2}])
    assert rec == [] and len(kept) == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("OK")
