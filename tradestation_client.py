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


def _delete(path: str) -> dict:
    return _request("DELETE", path)


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


def get_vix_level() -> Optional[float]:
    """Latest CBOE VIX index level via config.VIX_SYMBOL ("$VIX.X"), or None.

    The cash index carries no bid/ask book — only Last/Close — so read Last with a
    Close fallback (get_quote already normalizes both). Returns None on any failure
    so callers can fail open rather than block trading on a data glitch."""
    q = get_quote(config.VIX_SYMBOL)
    if not q:
        return None
    val = q.get("last") or q.get("close")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
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


def get_historical_orders(account_id: str, since: str) -> Optional[list[dict]]:
    """Filled/cancelled order history since `since` (YYYY-MM-DD), or None on error.

    READ-ONLY. `since` is inclusive and the broker caps history at 90 days —
    anything older returns nothing, which is why the backfill reports unmatched
    order IDs rather than treating them as missing trades.

    Returns one row per LEG (a single-leg equity order yields one row), carrying
    the execution price. Same None-vs-[] contract as get_positions: None means
    the fetch failed, [] means there genuinely were no orders."""
    try:
        data = _get(f"brokerage/accounts/{account_id}/historicalorders",
                    {"since": since})
        out = []
        for o in data.get("Orders", []):
            for leg in (o.get("Legs") or []):
                out.append({
                    "order_id":  str(o.get("OrderID")),
                    "symbol":    leg.get("Symbol"),
                    "action":    leg.get("BuyOrSell"),
                    "quantity":  _f(leg.get("ExecQuantity")),
                    # ExecutionPrice is the leg's average; FilledPrice is the
                    # whole-order average. Equal for single-leg equity orders.
                    "price":     _f(leg.get("ExecutionPrice")) or _f(o.get("FilledPrice")),
                    "status":    o.get("StatusDescription") or o.get("Status"),
                    "opened":    o.get("OpenedDateTime"),
                })
        return out
    except Exception as exc:
        logger.error("Historical orders fetch failed: %s", exc)
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
    stop_price:  Optional[float] = None,
) -> dict:
    """Assemble the request body shared by place (orders) and confirm
    (orderconfirm) so the two paths can never diverge. Route "Intelligent" is
    accepted (and is the default) for equities, options AND futures.

    An unknown order_type RAISES rather than defaulting. It used to fall back to
    "Market", which is a quiet catastrophe for a stop: a typo'd type would send a
    MARKET order that fills instantly at any price instead of resting at a
    trigger. The wrappers below catch the raise and return None, so a bad call
    now places nothing at all — the only safe failure for an order.

    Trigger/limit prices are rounded to a 0.01 tick, correct for equities and
    options. FUTURES TICK DIFFERENTLY (ES/NQ 0.25, RTY 0.10) and would be
    rejected or silently re-ticked by the broker, so place_futures_order refuses
    the stop path until per-root tick rounding exists. Nothing arms futures stops
    today (strategy._arm_stop_on_entry is called only from the equity paths).
    """
    kind = order_type.lower()
    ts_type = _ORDER_TYPES.get(kind)
    if ts_type is None:
        raise ValueError(f"Unsupported order_type {order_type!r} — expected one "
                         f"of {sorted(_ORDER_TYPES)}")
    if kind == "limit" and limit_price is None:
        raise ValueError("limit order requires limit_price")
    if kind == "stop" and stop_price is None:
        raise ValueError("stop order requires stop_price")

    body = {
        "AccountID":   account_id,
        "Symbol":      symbol,
        "Quantity":    str(quantity),
        "OrderType":   ts_type,
        "TradeAction": trade_action,
        "TimeInForce": {"Duration": duration.upper()},
        "Route":       "Intelligent",
    }
    if kind == "limit":
        body["LimitPrice"] = f"{round(limit_price, 2):.2f}"
    if kind == "stop":
        body["StopPrice"] = f"{round(stop_price, 2):.2f}"
    return body


def _place_order(
    account_id:  str,
    symbol:      str,
    trade_action: str,         # TradeStation enum, e.g. "BUY" / "BUYTOOPEN"
    quantity:    int,
    order_type:  str,
    duration:    str,
    limit_price: Optional[float],
    stop_price:  Optional[float] = None,
) -> Optional[dict]:
    """Single dispatch point for equity, option and futures orders.

    Returns a Tradier-shaped {"order": {"id": <OrderID>}} on success, or None.
    """
    body = _build_order_body(account_id, symbol, trade_action, quantity,
                             order_type, duration, limit_price, stop_price)

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
    stop_price: Optional[float] = None,
) -> Optional[dict]:
    action = _EQUITY_ACTIONS.get(side.lower())
    if action is None:
        logger.error("Unknown equity side: %s", side)
        return None
    try:
        return _place_order(account_id, symbol, action, quantity,
                            order_type, duration, limit_price, stop_price)
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
    stop_price:    Optional[float] = None,
) -> Optional[dict]:
    action = _OPTION_ACTIONS.get(side.lower())
    if action is None:
        logger.error("Unknown option side: %s", side)
        return None
    try:
        return _place_order(account_id, option_symbol, action, quantity,
                            order_type, duration, limit_price, stop_price)
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
    stop_price: Optional[float] = None,
) -> Optional[dict]:
    action = _FUTURES_ACTIONS.get(side.lower())
    if action is None:
        logger.error("Unknown futures side: %s", side)
        return None
    # Refused, not silently mis-ticked: _build_order_body rounds to 0.01, but ES
    # and NQ tick at 0.25 and RTY at 0.10, so a rounded stop is an invalid price.
    # Nothing arms futures stops today; lift this once per-root tick rounding
    # lands, together in one change so the two can never disagree.
    if order_type.lower() == "stop" or stop_price is not None:
        logger.error("Futures stop orders are not supported yet (%s): needs "
                     "per-root tick rounding (ES/NQ 0.25, RTY 0.10)", symbol)
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
    stop_price:   Optional[float] = None,
) -> Optional[dict]:
    """Validate an order WITHOUT placing it, via orderexecution/orderconfirm.

    Returns the first Confirmation dict (for futures this includes
    InitialMarginDisplay / EstimatedCost / EstimatedPrice), or None on error.
    Useful as a pre-trade margin check and in the read-only smoke test.

    The body build sits INSIDE the try so the validation raises added to
    _build_order_body return None here, exactly as they do in the place_*
    wrappers — a dry run must not be the one path that explodes.
    """
    try:
        body = _build_order_body(account_id, symbol, trade_action, quantity,
                                 order_type, duration, limit_price, stop_price)
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


# Terminal states — an order in any of these is finished and is NOT resting.
#
# Matched on BOTH the human StatusDescription and the short Status code, because
# TradeStation reports the same outcome two ways and neither is a superset of
# the other. "UROut" (User Requested Out — what a cancelled order BECOMES) was
# missing from this set until 2026-08-11, so a dead order read as still-resting
# to every caller that asked: get_working_orders listed it as live, and the
# floor reconcile would have skipped re-arming a position it believed protected.
_DONE_STATUSES = {"filled", "canceled", "cancelled", "rejected", "expired",
                  "replaced", "out", "urout", "too late to cancel"}
_DONE_STATUS_CODES = {"FLL", "CAN", "REJ", "EXP", "OUT", "UROUT", "RPL", "TLC"}

# Terminal WITHOUT having executed anything. The distinction from _DONE_STATUSES
# is "filled": a dead order moved ZERO shares, so a caller must never price a
# trade off it, never log it as a completed trade, and never tear down the
# protection behind the position it failed to close.
_FAILED_STATUSES = _DONE_STATUSES - {"filled"}
_FAILED_STATUS_CODES = _DONE_STATUS_CODES - {"FLL"}


def _order_is_done(o: dict) -> bool:
    """True if an order reached a terminal state and is no longer working."""
    desc = str(o.get("StatusDescription", "")).strip().lower()
    code = str(o.get("Status", "")).strip().upper()
    return desc in _DONE_STATUSES or code in _DONE_STATUS_CODES


def _order_failed(o: dict) -> bool:
    """True if an order is terminal AND executed nothing."""
    desc = str(o.get("StatusDescription", "")).strip().lower()
    code = str(o.get("Status", "")).strip().upper()
    return desc in _FAILED_STATUSES or code in _FAILED_STATUS_CODES


def get_order_outcome(account_id: str, order_id: str) -> dict:
    """What actually happened to an order.

    Returns {"state", "fill_price", "reason", "status"} where `state` is exactly
    one of — and these are NOT interchangeable:

      "filled"  — executed; fill_price is a real average execution price
      "working" — still live at the broker, no fill yet; may still fill
      "dead"    — terminal and executed NOTHING (rejected/cancelled/expired)
      "unknown" — the lookup itself failed; we genuinely do not know

    WHY THE SPLIT: this was a single Optional[float] that returned None for all
    of working/dead/unknown, and decided pending-vs-done purely on "is there a
    FilledPrice?" — a test a rejected order fails for the same reason a slow one
    does. On 2026-08-11 a GOOGL stop exit was REJECTED by the broker ("You are
    long 127 shares with 127 remaining on sell orders!"). get_order saw no fill
    price, called it pending, and the caller read that None as "it filled but we
    could not read the price": it logged a completed SELL of 127 shares that
    never happened at a price 2.55 better than reality, released the trailing
    stop, and left the position open and UNPROTECTED for three hours. A
    rejection and a slow fill are opposite outcomes; they must never share a
    return value.

    The broker's RejectReason is logged here. It reached no log at all before
    this — the only trace of the GOOGL failure was two lines calling a rejected
    order "still pending", and the reason had to be pulled from the API by hand.
    """
    status = None
    for attempt in (1, 2):
        try:
            data = _get(f"brokerage/accounts/{account_id}/orders/{order_id}")
        except Exception as exc:
            logger.warning("get_order_outcome failed for %s: %s", order_id, exc)
            return {"state": "unknown", "fill_price": None,
                    "reason": str(exc), "status": None}
        orders = data.get("Orders", [])
        if not orders:
            logger.warning("get_order_outcome returned no order for %s: %s",
                           order_id, data.get("Errors") or data)
            return {"state": "unknown", "fill_price": None,
                    "reason": "no order in response", "status": None}
        o = orders[0]
        status = o.get("StatusDescription") or o.get("Status")
        # FilledPrice is the whole-order average; fall back to the first leg's
        # ExecutionPrice (equal for a single-leg equity market order).
        price = _f(o.get("FilledPrice"))
        if price is None:
            legs = o.get("Legs") or []
            if legs:
                price = _f(legs[0].get("ExecutionPrice"))
        if price is not None and price > 0:
            if str(o.get("StatusDescription", "")).lower() == "partial fill":
                logger.warning("Order %s only PARTIALLY filled — using partial "
                               "average fill %.4f", order_id, price)
            return {"state": "filled", "fill_price": price,
                    "reason": None, "status": status}
        # Terminal with nothing executed. Checked BEFORE the retry: waiting
        # cannot resurrect a rejected order, and the old code's second poll was
        # two wasted seconds on the way to the wrong answer.
        if _order_failed(o):
            reason = o.get("RejectReason") or None
            logger.error("Order %s DEAD (status=%s) — executed NOTHING%s",
                         order_id, status, f": {reason}" if reason else "")
            return {"state": "dead", "fill_price": None,
                    "reason": reason, "status": status}
        # Genuinely still working (Received/Sent, no price yet). Retry once.
        if attempt == 1:
            logger.info("Order %s still working (status=%s) — retrying in 2s",
                        order_id, status)
            time.sleep(2)
        else:
            logger.warning("Order %s still working after 2s (status=%s)",
                           order_id, status)
    return {"state": "working", "fill_price": None,
            "reason": None, "status": status}


def get_order(account_id: str, order_id: str) -> Optional[float]:
    """An order's average fill price, or None if it did not (yet) fill.

    Thin wrapper over get_order_outcome for callers that only need a price and
    treat "no price" the same however it arose (entry-side stop arming, which
    falls back to the signal bar either way).

    Any caller that acts on the DIFFERENCE between "still working", "rejected"
    and "lookup failed" — above all an exit deciding whether to log a trade or
    tear down a position's protection — must call get_order_outcome instead.
    None here still collapses all three, by design.
    """
    return get_order_outcome(account_id, order_id).get("fill_price")


def get_working_orders(account_id: str) -> Optional[list[dict]]:
    """Orders still WORKING at the broker right now, or None on error.

    Distinct from get_historical_orders, which reports only FINISHED orders.
    This is the live view a reconcile needs: to answer "is a stop actually
    resting behind this position" you have to ask the broker. A stored order id
    cannot answer it — the broker can fill, cancel or expire an order without
    telling us, so the stored value ages out of agreement with reality exactly
    the way any other derived field does.

    Same None-vs-[] contract as get_positions, and for the same reason: None
    means the fetch FAILED and the caller must not read it as "nothing is
    resting" (that would cancel nothing and re-arm duplicates); [] means there
    genuinely are no working orders.
    """
    try:
        data = _get(f"brokerage/accounts/{account_id}/orders")
    except Exception as exc:
        logger.error("Working-orders fetch failed: %s", exc)
        return None
    out = []
    for o in data.get("Orders", []):
        if _order_is_done(o):
            continue
        legs = o.get("Legs") or []
        leg = legs[0] if legs else {}
        out.append({
            "order_id":   str(o.get("OrderID")),
            "symbol":     leg.get("Symbol"),
            "action":     leg.get("BuyOrSell"),
            "quantity":   _f(leg.get("QuantityOrdered")),
            "order_type": o.get("OrderType"),
            "stop_price": _f(o.get("StopPrice")),
            "duration":   (o.get("Duration") or
                           (o.get("TimeInForce") or {}).get("Duration")),
            "status":     o.get("StatusDescription"),
        })
    return out


def cancel_order(account_id: str, order_id: str) -> bool:
    """Cancel a working order. Returns True iff it is gone (or already was).

    Built for tearing down a resting broker-native stop when the position leaves
    by some OTHER route — a state exit, a crisis de-risk, a profit take. Skip
    that teardown and the stop outlives the position it protected: a GTC sell
    stop with no shares behind it can fill later and open an unintended SHORT.
    That is why this returns a bool the caller must actually check rather than
    firing and forgetting.

    An order that is already filled or already cancelled reports True: the
    caller's goal is "no live stop for this symbol", and that goal is met. Only
    a real failure — network, auth, an order still working after a rejected
    cancel — returns False, which means an orphan may be live and a human needs
    to look.

    NOTE this is a cancel REQUEST. An order can still fill in the race between
    the request and the broker acting on it, so a True here means "no longer
    working", not "never filled". Callers reconciling positions should re-read
    state rather than assume the cancel won.
    """
    try:
        data = _delete(f"orderexecution/orders/{order_id}")
    except Exception as exc:
        # 404 and "order not found" mean the order is already gone, which is the
        # outcome we wanted; anything else is a genuine failure.
        msg = str(exc).lower()
        if "404" in msg or "not found" in msg:
            logger.info("Cancel %s: order already gone (%s)", order_id, exc)
            return True
        logger.error("Cancel FAILED for order %s: %s — a resting stop may still "
                     "be live at the broker", order_id, exc)
        return False

    errors = data.get("Errors") or []
    if errors:
        text = str(errors).lower()
        if "not found" in text or "already" in text:
            logger.info("Cancel %s: nothing to cancel (%s)", order_id, errors)
            return True
        logger.error("Cancel REJECTED for order %s: %s — a resting stop may "
                     "still be live at the broker", order_id, errors)
        return False

    logger.info("Cancel request accepted for order %s", order_id)
    return True
