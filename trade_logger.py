import logging
import os
import json
from datetime import datetime
import pytz
import config

os.makedirs(config.LOG_DIR, exist_ok=True)

# ── Root app logger ───────────────────────────────────────────────────────────

# CRITICAL-only sink, on a path logrotate does not touch, so the record of an
# exit rejection or a stuck floor outlives daily rotation and the weekend gap
# (bot.log holds ONE day, and Sat/Sun produce no rotated file at all). Same
# formatter as the app log so the lines are directly comparable.
#
# A FileHandler is used rather than a journal/syslog handler on purpose: conftest
# Layer 3 re-points FileHandlers into a tmpdir, so the test suite cannot write
# here. A journal handler would slip past that check entirely and let fixture
# output — including the fabricated "STOP-LOSS EXIT NVDA ... sell order failed"
# that once reached the production log — land in a persistent alert sink.
_critical_handler = logging.FileHandler(config.CRITICAL_ALERT_FILE)
_critical_handler.setLevel(logging.CRITICAL)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.APP_LOG_FILE),
        logging.StreamHandler(),
        _critical_handler,
    ],
)

_ET = pytz.timezone(config.MARKET_TZ)


def _now_str() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d %H:%M:%S %Z")


# Stop-attribution keys. Declared here (not built ad hoc at the call site) so
# every record carries the same shape whether or not the caller supplies them —
# the same uniformity rule the fill_price/signal_price/slippage trio follows.
# An exit written before this existed simply has them absent, which reads as
# "unknown", NOT as "the floor was inactive": the analyzer must not count a
# pre-2026-08-13 exit as evidence either way.
_STOP_ATTR_KEYS = ("profit_floor_active", "profit_floor_price",
                   "atr_trail_at_exit", "floor_caused_exit")


def log_trade(action: str, symbol: str, quantity: int, price: float,
              order_type: str, order_id=None, notes: str = "",
              fill_price=None, signal_price=None, slippage=None,
              stop_attr: dict | None = None) -> None:
    """Append one trade record to the trade log.

    `price` stays the signal-bar close for backward compatibility (the ledger
    prices round-trips off it). Entry paths also pass the resolved broker
    `fill_price`, the `signal_price`, and the signed `slippage` (positive = worse
    fill than signalled); these are None for exits and on a fill-lookup miss.
    The three keys are ALWAYS written on new records (null when absent) so the
    ledger schema is uniform going forward — existing records are never rewritten.

    `stop_attr` carries which floor was holding the stop when a stop-exit fired
    (see _STOP_ATTR_KEYS). Only the trailing-stop exit path supplies it; every
    other record writes the keys as null. Without it a stop-out at a profit-floor
    rung, at a breakeven lock and at a plain ATR trail are indistinguishable in
    the ledger — all three write the same "trailing stop hit @ X" note — so the
    ladder could never be evaluated against the trail it overrides.
    """
    record = {
        "timestamp":    _now_str(),
        "action":       action,
        "symbol":       symbol,
        "quantity":     quantity,
        "price":        price,
        "order_type":   order_type,
        "order_id":     order_id,
        "notes":        notes,
        "signal_price": signal_price,
        "fill_price":   fill_price,
        "slippage":     slippage,
    }
    record.update({k: (stop_attr or {}).get(k) for k in _STOP_ATTR_KEYS})
    with open(config.TRADE_LOG_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    if fill_price is not None:
        fill_str = f" (fill={fill_price:.4f}, slippage={slippage:+.4f})"
    elif signal_price is not None:
        fill_str = " (fill=UNAVAILABLE, using signal)"
    else:
        fill_str = ""
    logging.getLogger("trade").info(
        "%s  %s x%d @ %.4f  [%s]  %s%s",
        action, symbol, quantity, price, order_type, notes, fill_str,
    )


def log_performance(account_id: str, balance: dict, positions: list) -> None:
    """Append account snapshot to the performance log."""
    record = {
        "timestamp":   _now_str(),
        "account_id":  account_id,
        # Use the top-level total_cash, which is present for both cash and margin
        # accounts. The nested cash.cash_available path only exists for cash
        # accounts, so it returned None on the (margin) sandbox account.
        "cash":        balance.get("total_cash") if balance else None,
        "total_equity": balance.get("total_equity") if balance else None,
        "positions":   [
            {
                "symbol":   p.get("symbol"),
                "quantity": p.get("quantity"),
                "cost":     p.get("cost_basis"),
            }
            for p in positions
        ],
    }
    with open(config.PERF_LOG_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    logging.getLogger("performance").info(
        "Equity: %s | Cash: %s | Open positions: %d",
        record["total_equity"], record["cash"], len(positions),
    )
