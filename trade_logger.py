import logging
import os
import json
from datetime import datetime
import pytz
import config

os.makedirs(config.LOG_DIR, exist_ok=True)

# ── Root app logger ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.APP_LOG_FILE),
        logging.StreamHandler(),
    ],
)

_ET = pytz.timezone(config.MARKET_TZ)


def _now_str() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d %H:%M:%S %Z")


def log_trade(action: str, symbol: str, quantity: int, price: float,
              order_type: str, order_id=None, notes: str = "") -> None:
    """Append one trade record to the trade log."""
    record = {
        "timestamp":  _now_str(),
        "action":     action,
        "symbol":     symbol,
        "quantity":   quantity,
        "price":      price,
        "order_type": order_type,
        "order_id":   order_id,
        "notes":      notes,
    }
    with open(config.TRADE_LOG_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    logging.getLogger("trade").info(
        "%s  %s x%d @ %.4f  [%s]  %s",
        action, symbol, quantity, price, order_type, notes,
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
