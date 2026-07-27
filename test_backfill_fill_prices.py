"""
Tests for the broker fill-price backfill.

WHY THIS EXISTS: the bot logged `price` as the signal-bar close and only began
capturing real fills on ENTRIES from 5f26dcd (2026-07-20); exits never captured
one. Every historical round-trip was therefore priced at signal. Re-pricing all
25 closed trips at actual broker fills moved realized P&L from -$36,296.78 to
-$39,229.12 — 8.1% understated, and biased rather than noisy.

This is written as a re-runnable reconcile, not a one-shot migration, so the
tests pin idempotency and the never-overwrite rule alongside the arithmetic.
"""

import backfill_fill_prices as bf


def _event(oid, symbol="AMD", price=100.0, fill=None, role="entry",
           direction="long", ts="2026-07-17 10:00:23 EDT", qty=100):
    return {
        "order_id": oid, "symbol": symbol, "price": price, "fill_price": fill,
        "role": role, "direction": direction, "timestamp": ts, "quantity": qty,
        "action": "BUY" if direction == "long" else "SELL_SHORT",
        "notes": "", "feature": "long_fresh_cross", "estimated_entry": False,
    }


def _ledger(events):
    return {"version": 1, "events": {str(e["order_id"]): e for e in events},
            "closed_trips": []}


def _order(oid, price, symbol="AMD"):
    return {"order_id": str(oid), "symbol": symbol, "price": price,
            "action": "Buy", "quantity": 100, "status": "Filled",
            "opened": "2026-07-17T14:00:23Z"}


def test_applies_fill_price_from_broker():
    led = _ledger([_event("111", price=476.24)])
    stats = bf.backfill(led, [_order("111", 476.00)])
    assert stats["matched"] == 1
    assert led["events"]["111"]["fill_price"] == 476.00


def test_records_signed_slippage_on_a_buy():
    """Positive ALWAYS means a worse fill. Buying above the signal is worse."""
    led = _ledger([_event("111", price=476.24)])
    led["events"]["111"]["action"] = "BUY"
    bf.backfill(led, [_order("111", 476.50)])
    assert led["events"]["111"]["slippage"] == 0.26


def test_slippage_sign_is_inverted_for_selling_actions():
    """The bug this fixes: a raw (fill - signal) reads a SELL filled BELOW the
    signal as favourable, when selling cheaper is worse. Storing it raw made
    exits look advantageous while they were costing money — 20 of 24 exit legs
    filled worse, not better."""
    led = _ledger([_event("111", price=100.0, role="exit", direction="long")])
    led["events"]["111"]["action"] = "SELL"
    bf.backfill(led, [_order("111", 99.50)])
    assert led["events"]["111"]["slippage"] == 0.50, \
        "a sell filled 0.50 below signal is 0.50 WORSE, not better"


def test_cover_and_sell_slippage_signs_are_opposite():
    """Same raw drift, opposite verdict — a BUY_TO_COVER is a purchase."""
    sell = _ledger([_event("1", price=100.0, role="exit")])
    sell["events"]["1"]["action"] = "SELL"
    bf.backfill(sell, [_order("1", 101.0)])

    cover = _ledger([_event("2", price=100.0, role="exit", direction="short")])
    cover["events"]["2"]["action"] = "BUY_TO_COVER"
    bf.backfill(cover, [_order("2", 101.0)])

    assert sell["events"]["1"]["slippage"] == -1.0     # sold higher = better
    assert cover["events"]["2"]["slippage"] == 1.0     # bought higher = worse


def test_slippage_is_rederived_even_when_fill_already_present():
    """Slippage is derived, not observed. An event whose fill we already had must
    still get a corrected sign — otherwise the first run's raw values persist
    forever behind the never-overwrite rule."""
    led = _ledger([_event("111", price=100.0, fill=99.50, role="exit")])
    led["events"]["111"]["action"] = "SELL"
    led["events"]["111"]["slippage"] = -0.50          # the old, inverted value
    stats = bf.backfill(led, [])
    assert stats["already_had"] == 1 and stats["slippage_rederived"] == 1
    assert led["events"]["111"]["slippage"] == 0.50
    assert led["events"]["111"]["fill_price"] == 99.50, "fill itself must not change"


def test_unrecognised_action_leaves_slippage_unset():
    led = _ledger([_event("111", price=100.0)])
    led["events"]["111"]["action"] = "HODL"
    led["events"]["111"].pop("slippage", None)
    bf.backfill(led, [_order("111", 101.0)])
    assert led["events"]["111"].get("slippage") is None
    assert led["events"]["111"]["fill_price"] == 101.0, "fill still applies"


def test_never_overwrites_an_existing_fill():
    """The bot read that price seconds after execution via get_order — at least
    as authoritative as history, and leaving it alone keeps the ledger stable
    across re-runs."""
    led = _ledger([_event("111", price=476.24, fill=475.00)])
    stats = bf.backfill(led, [_order("111", 999.99)])
    assert stats["already_had"] == 1 and stats["matched"] == 0
    assert led["events"]["111"]["fill_price"] == 475.00


def test_is_idempotent():
    """Second run must be a no-op — this is a reconcile, not a migration."""
    led = _ledger([_event("111", price=476.24)])
    first = bf.backfill(led, [_order("111", 476.00)])
    second = bf.backfill(led, [_order("111", 476.00)])
    assert first["matched"] == 1
    assert second["matched"] == 0 and second["already_had"] == 1
    assert led["events"]["111"]["fill_price"] == 476.00


def test_unmatched_order_ids_are_reported_not_dropped():
    """Orders older than the broker's 90-day window cannot be matched. They must
    surface as unmatched, NOT be silently treated as having no fill."""
    led = _ledger([_event("28388699", symbol="TSLA")])
    stats = bf.backfill(led, [_order("111", 476.00)])
    assert stats["matched"] == 0
    assert [u[0] for u in stats["unmatched"]] == ["28388699"]
    assert led["events"]["28388699"]["fill_price"] is None


def test_events_without_an_order_id_are_counted_separately():
    """Synthetic bootstrap entries carry no order_id and are not a failure."""
    e = _event(None)
    led = {"version": 1, "events": {"bootstrap|AAPL|2026-07-14": e},
           "closed_trips": []}
    stats = bf.backfill(led, [])
    assert stats["no_order_id"] == 1 and stats["unmatched"] == []


def test_orders_without_a_price_are_ignored():
    """A cancelled/unfilled order has no execution price; it must not stamp None
    over a good signal price."""
    led = _ledger([_event("111", price=476.24)])
    stats = bf.backfill(led, [{"order_id": "111", "price": None,
                               "symbol": "AMD", "status": "Cancelled"}])
    assert stats["matched"] == 0
    assert led["events"]["111"]["fill_price"] is None


def test_backfill_changes_realized_pnl_in_the_loss_direction():
    """End-to-end on the real AMD short: signal-priced -$1,502 becomes -$1,573
    at true fills. Fills make a losing short worse on BOTH legs."""
    entry = _event("111", symbol="AMD", price=476.24, role="entry",
                   direction="short", ts="2026-07-17 10:00:23 EDT")
    exit_ = _event("222", symbol="AMD", price=491.26, role="exit",
                   direction="short", ts="2026-07-17 10:11:33 EDT")
    exit_["action"] = "BUY_TO_COVER"
    led = _ledger([entry, exit_])

    before, n_before = bf._realized(led)
    bf.backfill(led, [_order("111", 476.00, "AMD"), _order("222", 491.73, "AMD")])
    after, n_after = bf._realized(led)

    assert n_before == n_after == 1
    assert before == -1502.00
    assert after == -1573.00
    assert after < before


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("OK")
