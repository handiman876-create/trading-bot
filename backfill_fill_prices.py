#!/usr/bin/env python3
"""
Backfill real broker fill prices onto ledger events, matched by order_id.

WHY THIS EXISTS: the bot logged `price` as the signal-bar close and only started
capturing the real `fill_price` on ENTRIES from 5f26dcd (2026-07-20); exits never
captured one. The analyzer therefore priced every historical round-trip at signal
prices. A read-only broker audit on 2026-07-27 re-priced all 25 closed trips at
their actual fills: total realized P&L moved from -$36,296.78 to -$39,229.12 —
an 8.1% understatement, and biased, not noisy (entries fill worse AND exits fill
worse, so the error accumulates against the account).

This is a RECONCILE, not a one-shot repair: it re-derives fill prices from the
broker every run and is safe to re-run. Deliberately not a migration script that
stamps values once and rots — the same lesson as the ledger open-position
reconcile. It only ever WIDENS knowledge (fills in a missing fill_price); it
never overwrites a fill the bot itself recorded at execution time, since that one
was read closer to the event.

Read-only against the broker (GET historicalorders). Writes only data/trade_ledger.json,
and only with --apply.

    python3 backfill_fill_prices.py              # dry run, shows the P&L impact
    python3 backfill_fill_prices.py --apply      # persist fill prices to the ledger
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta

import performance_analyzer as pa
import tradestation_client as tc

logger = logging.getLogger("backfill_fill_prices")

# The broker caps historicalorders at 90 days; ask for the whole window.
HISTORY_DAYS = 90


def _since_date() -> str:
    return (pa._reference_now() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")


def backfill(ledger: dict, orders: list[dict]) -> dict:
    """Attach broker fill prices to ledger events. Returns a stats dict.

    Never overwrites an existing fill_price: the bot read that one seconds after
    the fill via get_order, which is at least as authoritative as history and
    avoids churning the ledger on every run."""
    by_id = {o["order_id"]: o for o in orders if o.get("price") is not None}
    stats = {"matched": 0, "already_had": 0, "unmatched": [], "no_order_id": 0}

    for event in ledger.get("events", {}).values():
        oid = event.get("order_id")
        if not oid:
            stats["no_order_id"] += 1          # bootstrap/synthetic entries
            continue
        if event.get("fill_price") is not None:
            stats["already_had"] += 1
            continue
        order = by_id.get(str(oid))
        if order is None:
            stats["unmatched"].append((str(oid), event.get("symbol"),
                                       event.get("timestamp")))
            continue
        event["fill_price"] = order["price"]
        signal = event.get("price")
        if signal is not None:
            event["slippage"] = round(order["price"] - signal, 4)
        stats["matched"] += 1
    return stats


def _realized(ledger: dict) -> tuple[float, int]:
    """Total realized P&L over the ledger's pairable events, and the trip count."""
    cutoff = pa._reference_now() - timedelta(days=pa.STALE_OPEN_DAYS)
    events, _ = pa._partition_stale(list(ledger["events"].values()), cutoff)
    closed, _, _ = pa._pair_round_trips(events)
    return sum(t["pnl"] for t in closed), len(closed)


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the backfilled fill prices to the ledger "
                         "(default is a dry run)")
    args = ap.parse_args()

    account_id = tc.get_account_id()
    if not account_id:
        logger.error("no account id — aborting")
        return 1

    since = _since_date()
    orders = tc.get_historical_orders(account_id, since)
    if orders is None:
        logger.error("historical orders fetch FAILED — aborting without changes "
                     "(an empty result must never be mistaken for 'no fills')")
        return 1
    logger.info("broker returned %d order legs since %s", len(orders), since)

    ledger = pa._load_ledger()
    before_pnl, before_n = _realized(ledger)

    stats = backfill(ledger, orders)
    after_pnl, after_n = _realized(ledger)

    print()
    print("=== BACKFILL ===")
    print(f"  fill prices retrieved and applied : {stats['matched']}")
    print(f"  events that already had a fill    : {stats['already_had']}")
    print(f"  events with no order_id (synthetic): {stats['no_order_id']}")
    print(f"  order_ids not found at broker      : {len(stats['unmatched'])}")
    for oid, sym, ts in stats["unmatched"][:10]:
        print(f"      - {oid}  {sym:<6} {ts}")
    if len(stats["unmatched"]) > 10:
        print(f"      ... and {len(stats['unmatched']) - 10} more")
    print()
    print("=== REALIZED P&L IMPACT ===")
    print(f"  before backfill (signal prices) : ${before_pnl:,.2f}  ({before_n} trips)")
    print(f"  after  backfill (real fills)    : ${after_pnl:,.2f}  ({after_n} trips)")
    print(f"  change                          : ${after_pnl - before_pnl:,.2f}")
    print()

    if args.apply:
        pa._save_ledger(ledger)
        print(f"WROTE {pa.LEDGER_PATH}")
        print("Re-run performance_analyzer.py to regenerate the report.")
    else:
        print("DRY RUN — nothing written. Re-run with --apply to persist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
