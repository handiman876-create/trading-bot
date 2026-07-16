#!/usr/bin/env python3
"""
One-shot repair: unwind the 2026-07-16 double entries in CRL and LII.
=====================================================================
A 503 on the positions endpoint made get_positions() return [] (since fixed to
return None), so `held == 0` read as "flat" for every symbol on that cycle and
the momentum-alignment path re-entered two names it already owned:

    CRL  219 @ 227.29 (07-15)  +  221 @ 225.68 (07-16)  = 440  (~10% of equity)
    LII   89 @ 559.87 (07-15)  +   90 @ 551.38 (07-16)  = 179  (~10% of equity)

Both should be one 5% lot. This sells the FIRST lot of each, exactly.

WHY THE ODD QUANTITIES (219/89, not 220/89.5):
performance_analyzer._pair_round_trips is FIFO by EVENT, not by quantity — one
exit pops one entry and computes P&L from the ENTRY's quantity, ignoring the
exit's. Selling exactly lot #1's size makes the resulting round trip true
share-for-share and leaves lot #2 open against the shares we actually still
hold. Any other quantity leaves the ledger describing a position that does not
exist.

WHY THIS LOGS THROUGH log_trade AND NOT THE TRADESTATION UI:
an unlogged sale leaves the ledger with two open entries and 221 real shares.
The eventual real exit would then pop lot #1 at the wrong basis and strand lot
#2 as a phantom "open" position until the 90-day stale sweep. The fill is real;
recording it is the honest option.

The note carries config.CORRECTION_NOTE_MARKER, so the analyzer classifies these
as exit_reason="correction" and excludes them from per-feature stats — the
strategy never signalled this trade and must not be scored on it.

KNOWN IMPRECISION: the order response carries no fill price, so the logged price
is the last trade observed just before placing a market order, not the fill.
Same limitation as the bot's own trade logging and the stop-arming TODO. At
these sizes the drift is cents; it lands in the round-trip P&L, not the account.

USAGE:
    python3 trim_duplicate_entries.py            # dry run — prints the plan
    python3 trim_duplicate_entries.py --execute  # places the orders

Run at/after the 09:30 ET open on 2026-07-17. Market orders need a live book.
"""

import argparse
import logging
import os
import sys

# Mirror main.py: BOT_MODE is read by config/trade_logger at import time to pick
# the log filenames. These fills must land in the equities trade log the analyzer
# reads, so pin it before importing either.
os.environ.setdefault("BOT_MODE", "equities")

import trade_logger  # noqa: F401 — configures logging as an import side-effect
import config
import tradestation_client as tc
import strategy
from trade_logger import log_trade

logger = logging.getLogger("trim")

# (symbol, expected_held_now, qty_to_sell). expected_held is asserted, not
# assumed: if the bot exited or stopped out overnight, the premise of this whole
# script is void for that name and we must not place a blind order. The exact
# match also makes a second run a no-op rather than a second sale.
TRIMS = [
    ("CRL", 440, 219),
    ("LII", 179,  89),
]

NOTE = f"trim — {config.CORRECTION_NOTE_MARKER} (503 re-entry)"


def _plan_line(symbol: str, held: int, qty: int, price) -> str:
    px = f"${price:.2f}" if price else "price unavailable"
    return (f"  {symbol}: hold {held} -> sell {qty} -> leave {held - qty} "
            f"(~{px}, ~${qty * price:,.0f})" if price else
            f"  {symbol}: hold {held} -> sell {qty} -> leave {held - qty} ({px})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--execute", action="store_true",
                    help="actually place the orders (default: dry run)")
    args = ap.parse_args()

    account_id = tc.get_account_id()
    if not account_id:
        logger.error("No equities account id — check credentials.")
        return 1

    positions = tc.get_positions(account_id)
    if positions is None:
        # The exact failure this whole exercise is about. Do not guess.
        logger.error("Positions fetch FAILED — holdings unknown, not flat. "
                     "Refusing to trim blind. Retry.")
        return 1

    held_by_symbol = {p["symbol"]: int(p["quantity"]) for p in positions}
    plan, problems = [], []

    for symbol, expected, qty in TRIMS:
        held = held_by_symbol.get(symbol, 0)
        if held != expected:
            problems.append(
                f"  {symbol}: expected {expected} shares, found {held}. "
                f"Position changed since diagnosis — skipping (re-check by hand).")
            continue
        price = strategy._live_price(symbol)
        plan.append((symbol, held, qty, price))

    for p in problems:
        logger.warning("%s", p)
    if not plan:
        logger.error("Nothing to trim — no symbol matched its expected holding.")
        return 1

    logger.info("Trim plan (%s):", "EXECUTE" if args.execute else "DRY RUN")
    for symbol, held, qty, price in plan:
        logger.info("%s", _plan_line(symbol, held, qty, price))
    logger.info('Note on each fill: "%s"', NOTE)

    if not args.execute:
        logger.info("Dry run — no orders placed. Re-run with --execute.")
        return 0

    failed = 0
    for symbol, held, qty, price in plan:
        if price is None:
            logger.error("%s: no live price — skipping (the ledger record needs "
                         "a price to pair a round trip).", symbol)
            failed += 1
            continue
        result = tc.place_equity_order(account_id, symbol, "sell", qty)
        if not result:
            logger.error("%s: sell order FAILED — not logged. Re-run.", symbol)
            failed += 1
            continue
        order_id = result.get("order", {}).get("id")
        log_trade("SELL", symbol, qty, price, "market", order_id, NOTE)
        logger.info("%s: sold %d, %d remain. Stop record untouched — the bot "
                    "keeps trailing the remainder.", symbol, qty, held - qty)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
