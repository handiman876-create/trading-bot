"""
TradeStation v3 broker client — drop-in replacement for tradier_client.

Every public function preserves the signature the rest of the bot already
calls, and normalizes TradeStation's responses back into the Tradier-shaped
dicts that strategy.py / trade_logger.py read (lowercase keys: close, last,
bid, symbol, quantity, cost_basis, total_cash, total_equity, and order results
wrapped as {"order": {"id": ...}}). That keeps strategy.py, indicators.py,
market_hours.py and trade_logger.py unchanged.

Auth: OAuth2 refresh-token flow. The access token (~20 min lifetime) is
refreshed lazily — checked under a lock before every request and renewed once
it is older than 19 minutes, plus a one-shot retry on a 401. No background
thread.
"""

import logging
import threading
import time
from typing import Optional
from urllib.parse import quote

import requests

import config

logger = logging.getLogger(__name__)

# ── Token management (lazy, lock-guarded) ─────────────────────────────────────
_token_lock = threading.Lock()
_access_token: Optional[str] = None
_token_acquired_at: float = 0.0
# Refresh one minute before the ~20-minute access-token expiry.
_ACCESS_TOKEN_TTL = 19 * 60


def _refresh_access_token() -> str:
    """Exchange the stored refresh token for a fresh access token. Caller holds _token_lock."""
    global _access_token, _token_acquired_at
    resp = requests.post(
        config.TS_TOKEN_URL,
        data={
            "grant_type":    "refresh_token",
            "client_id":     config.TS_CLIENT_ID,
            "client_secret": config.TS_CLIENT_SECRET,
            "refresh_token": config.TS_REFRESH_TOKEN,
        },
        timeout=15,
    )
    resp.raise_for_status()
    _access_token = resp.json()["access_token"]
    _token_acquired_at = time.monotonic()
    logger.info("TradeStation access token refreshed.")
    return _access_token


def _get_access_token() -> str:
    with _token_lock:
        if _access_token is None or (time.monotonic() - _token_acquired_at) >= _ACCESS_TOKEN_TTL:
            return _refresh_access_token()
        return _access_token


def _force_refresh() -> None:
    with _token_lock:
        _refresh_access_token()


# ── HTTP plumbing ─────────────────────────────────────────────────────────────

def _request(method: str, path: str, params: dict = None,
             json_body: dict = None, _retried: bool = False) -> dict:
    url = f"{config.TS_BASE_URL}/{path}"
    headers = {
        "Authorization": f"Bearer {_get_access_token()}",
        "Accept":        "application/json",
    }
    resp = requests.request(method, url, headers=headers, params=params,
                            json=json_body, timeout=15)
    # Access token may have been revoked/expired early — refresh once and retry.
    if resp.status_code == 401 and not _retried:
        _force_refresh()
        return _request(method, path, params, json_body, _retried=True)
    resp.raise_for_status()
    return resp.json()


def _get(path: str, params: dict = None) -> dict:
    return _request("GET", path, params=params)


def _post(path: str, json_body: dict) -> dict:
    return _request("POST", path, json_body=json_body)


def _f(value) -> Optional[float]:
    """Coerce a TradeStation numeric (often a string) to float, or None if absent."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Symbol construction (Option-chain Decision 1: build, don't fetch) ─────────

def _format_strike(strike: float) -> str:
    """582.5 -> '582.5', 540.0 -> '540'."""
    return ("%f" % strike).rstrip("0").rstrip(".")


def build_option_symbol(symbol: str, expiration: str, strike: float, option_type: str) -> str:
    """Construct a TradeStation option symbol, e.g. 'SPY 250620C540'.

    expiration is 'YYYY-MM-DD'; the TradeStation format uses 'YYMMDD' followed
    by C/P and the strike (trailing zeros stripped).
    """
    yymmdd = expiration.replace("-", "")[2:]          # '2026-06-20' -> '260620'
    cp = "C" if option_type.lower() == "call" else "P"
    return f"{symbol} {yymmdd}{cp}{_format_strike(strike)}"


# ── Market Data ───────────────────────────────────────────────────────────────

def _normalize_quote(q: dict) -> dict:
    return {
        "symbol": q.get("Symbol"),
        "last":   _f(q.get("Last")),
        "bid":    _f(q.get("Bid")),
        "ask":    _f(q.get("Ask")),
        "close":  _f(q.get("Close")),
    }


def get_quote(symbol: str) -> Optional[dict]:
    """Return a normalized quote dict for a single symbol, or None on error."""
    try:
        data = _get(f"marketdata/quotes/{quote(symbol, safe=',')}")
        quotes = data.get("Quotes", [])
        if not quotes:
            logger.warning("No quote returned for %s (errors=%s)", symbol, data.get("Errors"))
            return None
        return _normalize_quote(quotes[0])
    except Exception as exc:
        logger.error("Quote fetch failed for %s: %s", symbol, exc)
        return None


_UNIT_MAP = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "minute": "Minute"}


def get_historical(symbol: str, interval: str = "daily", days: int = 60) -> list[dict]:
    """Return list of OHLCV dicts sorted oldest→newest (keys: date/open/high/low/close/volume).

    `days` is used as the number of bars to request (barsback); for daily data
    that is more than enough trading days to cover the indicator windows.
    """
    unit = _UNIT_MAP.get(interval.lower(), "Daily")
    try:
        data = _get(f"marketdata/barcharts/{quote(symbol, safe='')}", {
            "unit":     unit,
            "interval": 1,
            "barsback": days,
        })
        bars = data.get("Bars", [])
        return [
            {
                "date":   b.get("TimeStamp"),
                "open":   _f(b.get("Open")),
                "high":   _f(b.get("High")),
                "low":    _f(b.get("Low")),
                "close":  _f(b.get("Close")),
                "volume": _f(b.get("TotalVolume")),
            }
            for b in bars
        ]
    except Exception as exc:
        logger.error("History fetch failed for %s: %s", symbol, exc)
        return []


def get_option_quote(option_symbol: str) -> Optional[dict]:
    """Return a normalized quote for a TradeStation option symbol."""
    return get_quote(option_symbol)


def find_option_symbol(symbol: str, expiration: str, strike: float, option_type: str) -> Optional[str]:
    """Build the TradeStation option symbol directly (no chain fetch — Decision 1)."""
    try:
        return build_option_symbol(symbol, expiration, strike, option_type)
    except Exception as exc:
        logger.error("Could not build option symbol %s %s %.2f %s: %s",
                     symbol, expiration, strike, option_type, exc)
        return None


# ── Account ───────────────────────────────────────────────────────────────────

def _is_futures(acct: dict) -> bool:
    return str(acct.get("AccountType", "")).lower() == "futures"


def _find_account_id(match) -> Optional[str]:
    """AccountID of the first account for which match(account) is truthy, else None."""
    try:
        accounts = _get("brokerage/accounts").get("Accounts", [])
    except Exception as exc:
        logger.error("Account lookup failed: %s", exc)
        return None
    for acct in accounts:
        if match(acct):
            return acct.get("AccountID")
    return None


def get_account_id() -> Optional[str]:
    """First non-futures brokerage account id (falls back to first account of any type)."""
    return _find_account_id(lambda a: not _is_futures(a)) or _find_account_id(lambda a: True)


def get_futures_account_id() -> Optional[str]:
    """First futures brokerage account id, or None if this login has none."""
    return _find_account_id(_is_futures)


def get_positions(account_id: str) -> Optional[list[dict]]:
    """Open positions for an account, or None if the fetch FAILED.

    None and [] are NOT interchangeable and callers must not conflate them:
      []   — the request succeeded; the account genuinely holds nothing.
      None — we do not know what the account holds.

    This returned [] on error until 2026-07-16, when a 503 on the positions
    endpoint made every symbol read as held=0 for one cycle. `held == 0` is the
    precondition for BOTH entry paths, so the bot re-entered CRL and LII on top
    of positions it already had (10% of equity each, double the 5% target). The
    same read would have let _enter_short open a SELLSHORT on a name held long,
    had the outage landed on a death-cross bar instead of two minutes earlier.
    "I can't see the account" must never be indistinguishable from "the account
    is flat"."""
    try:
        data = _get(f"brokerage/accounts/{account_id}/positions")
        out = []
        for p in data.get("Positions", []):
            qty = _f(p.get("Quantity")) or 0.0
            if str(p.get("LongShort", "")).lower() == "short":
                qty = -abs(qty)
            out.append({
                "symbol":     p.get("Symbol"),
                "quantity":   int(qty),
                "cost_basis": _f(p.get("TotalCost")),
            })
        return out
    except Exception as exc:
        logger.error("Positions fetch failed: %s", exc)
        return None


def get_account_balance(account_id: str) -> Optional[dict]:
    try:
        data = _get(f"brokerage/accounts/{account_id}/balances")
        balances = data.get("Balances", [])
        if not balances:
            return None
        b = balances[0]
        return {
            "total_cash":   _f(b.get("CashBalance")),
            "total_equity": _f(b.get("Equity")),
        }
    except Exception as exc:
        logger.error("Balance fetch failed: %s", exc)
        return None


# ── Orders ────────────────────────────────────────────────────────────────────

_EQUITY_ACTIONS = {
    "buy":          "BUY",
    "sell":         "SELL",
    "buy_to_cover": "BUYTOCOVER",
    "sell_short":   "SELLSHORT",
}
_OPTION_ACTIONS = {
    "buy_to_open":   "BUYTOOPEN",
    "sell_to_close": "SELLTOCLOSE",
    "buy_to_close":  "BUYTOCLOSE",
    "sell_to_open":  "SELLTOOPEN",
}
_ORDER_TYPES = {"market": "Market", "limit": "Limit", "stop": "StopMarket"}
# Futures use plain BUY/SELL to open long/short — no BUYTOCOVER/SELLSHORT.
_FUTURES_ACTIONS = {"buy": "BUY", "sell": "SELL"}


def _build_order_body(
    account_id:  str,
    symbol:      str,
    trade_action: str,
    quantity:    int,
    order_type:  str,
    duration:    str,
    limit_price: Optional[float],
) -> dict:
    """Assemble the request body shared by place (orders) and confirm
    (orderconfirm) so the two paths can never diverge. Route "Intelligent" is
    accepted (and is the default) for equities, options AND futures."""
    body = {
        "AccountID":   account_id,
        "Symbol":      symbol,
        "Quantity":    str(quantity),
        "OrderType":   _ORDER_TYPES.get(order_type.lower(), "Market"),
        "TradeAction": trade_action,
        "TimeInForce": {"Duration": duration.upper()},
        "Route":       "Intelligent",
    }
    if order_type.lower() == "limit" and limit_price is not None:
        body["LimitPrice"] = str(limit_price)
    return body


def _place_order(
    account_id:  str,
    symbol:      str,
    trade_action: str,         # TradeStation enum, e.g. "BUY" / "BUYTOOPEN"
    quantity:    int,
    order_type:  str,
    duration:    str,
    limit_price: Optional[float],
) -> Optional[dict]:
    """Single dispatch point for equity, option and futures orders.

    Returns a Tradier-shaped {"order": {"id": <OrderID>}} on success, or None.
    """
    body = _build_order_body(account_id, symbol, trade_action, quantity,
                             order_type, duration, limit_price)

    data = _post("orderexecution/orders", body)
    orders = data.get("Orders", [])
    order_id = orders[0].get("OrderID") if orders else None
    if not order_id:
        logger.error("Order rejected for %s %s x%d: %s",
                     trade_action, symbol, quantity, data.get("Errors") or data)
        return None
    return {"order": {"id": order_id}}


def place_equity_order(
    account_id: str,
    symbol: str,
    side: str,       # "buy" or "sell"
    quantity: int,
    order_type: str = "market",
    duration: str   = "day",
    limit_price: Optional[float] = None,
) -> Optional[dict]:
    action = _EQUITY_ACTIONS.get(side.lower())
    if action is None:
        logger.error("Unknown equity side: %s", side)
        return None
    try:
        return _place_order(account_id, symbol, action, quantity,
                            order_type, duration, limit_price)
    except Exception as exc:
        logger.error("Equity order failed %s %s %s: %s", side, quantity, symbol, exc)
        return None


def place_option_order(
    account_id:    str,
    option_symbol: str,
    side:          str,    # "buy_to_open" | "sell_to_close" etc.
    quantity:      int,
    order_type:    str = "market",
    duration:      str = "day",
    limit_price:   Optional[float] = None,
) -> Optional[dict]:
    action = _OPTION_ACTIONS.get(side.lower())
    if action is None:
        logger.error("Unknown option side: %s", side)
        return None
    try:
        return _place_order(account_id, option_symbol, action, quantity,
                            order_type, duration, limit_price)
    except Exception as exc:
        logger.error("Option order failed %s %s %s: %s", side, quantity, option_symbol, exc)
        return None


def place_futures_order(
    account_id:  str,
    symbol:      str,       # dated futures contract, e.g. "ESU26"
    side:        str,       # "buy" | "sell"
    quantity:    int,
    order_type:  str = "market",
    duration:    str = "day",
    limit_price: Optional[float] = None,
) -> Optional[dict]:
    action = _FUTURES_ACTIONS.get(side.lower())
    if action is None:
        logger.error("Unknown futures side: %s", side)
        return None
    try:
        return _place_order(account_id, symbol, action, quantity,
                            order_type, duration, limit_price)
    except Exception as exc:
        logger.error("Futures order failed %s %s %s: %s", side, quantity, symbol, exc)
        return None


def confirm_order(
    account_id:   str,
    symbol:       str,
    trade_action: str,       # TradeStation enum, e.g. "BUY" / "SELL"
    quantity:     int,
    order_type:   str = "market",
    duration:     str = "day",
    limit_price:  Optional[float] = None,
) -> Optional[dict]:
    """Validate an order WITHOUT placing it, via orderexecution/orderconfirm.

    Returns the first Confirmation dict (for futures this includes
    InitialMarginDisplay / EstimatedCost / EstimatedPrice), or None on error.
    Useful as a pre-trade margin check and in the read-only smoke test.
    """
    body = _build_order_body(account_id, symbol, trade_action, quantity,
                             order_type, duration, limit_price)
    try:
        data = _post("orderexecution/orderconfirm", body)
    except Exception as exc:
        logger.error("Order confirm failed %s %s x%d: %s",
                     trade_action, symbol, quantity, exc)
        return None
    confirmations = data.get("Confirmations", [])
    if not confirmations:
        logger.error("Order confirm returned nothing for %s %s: %s",
                     trade_action, symbol, data.get("Errors") or data)
        return None
    return confirmations[0]
