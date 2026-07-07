import requests
import logging
from typing import Optional
import config

logger = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {config.TRADIER_API_TOKEN}",
    "Accept": "application/json",
}


def _get(endpoint: str, params: dict = None) -> dict:
    url = f"{config.TRADIER_BASE_URL}/{endpoint}"
    resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _post(endpoint: str, data: dict) -> dict:
    url = f"{config.TRADIER_BASE_URL}/{endpoint}"
    resp = requests.post(url, headers=HEADERS, data=data, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Market Data ───────────────────────────────────────────────────────────────

def get_quote(symbol: str) -> Optional[dict]:
    """Return quote dict for a single symbol, or None on error."""
    try:
        data = _get("markets/quotes", {"symbols": symbol, "greeks": "false"})
        quotes = data.get("quotes", {}).get("quote")
        if isinstance(quotes, list):
            return quotes[0]
        return quotes
    except Exception as exc:
        logger.error("Quote fetch failed for %s: %s", symbol, exc)
        return None


def get_historical(symbol: str, interval: str = "daily", days: int = 60) -> list[dict]:
    """Return list of OHLCV dicts sorted oldest→newest."""
    from datetime import date, timedelta
    end   = date.today().isoformat()
    start = (date.today() - timedelta(days=days)).isoformat()
    try:
        data = _get("markets/history", {
            "symbol":   symbol,
            "interval": interval,
            "start":    start,
            "end":      end,
        })
        history = data.get("history", {}).get("day", [])
        if isinstance(history, dict):   # single-day edge case
            history = [history]
        return history
    except Exception as exc:
        logger.error("History fetch failed for %s: %s", symbol, exc)
        return []


def get_option_chain(symbol: str, expiration: str) -> list[dict]:
    """Return list of option contracts for symbol/expiration."""
    try:
        data = _get("markets/options/chains", {
            "symbol":     symbol,
            "expiration": expiration,
            "greeks":     "false",
        })
        # Tradier returns {"options": null} for expired/unknown expirations,
        # so the key exists with a None value — guard against None, don't .get() it.
        options_root = data.get("options")
        options = options_root.get("option", []) if options_root else []
        if isinstance(options, dict):
            options = [options]
        return options
    except Exception as exc:
        logger.error("Option chain fetch failed for %s %s: %s", symbol, expiration, exc)
        return []


def get_option_quote(option_symbol: str) -> Optional[dict]:
    """Return quote for an OCC option symbol."""
    return get_quote(option_symbol)


# ── Account ───────────────────────────────────────────────────────────────────

def get_account_id() -> Optional[str]:
    try:
        data = _get("user/profile")
        accounts = data.get("profile", {}).get("account", [])
        if isinstance(accounts, dict):
            accounts = [accounts]
        return accounts[0].get("account_number") if accounts else None
    except Exception as exc:
        logger.error("Account ID fetch failed: %s", exc)
        return None


def get_positions(account_id: str) -> list[dict]:
    try:
        data = _get(f"accounts/{account_id}/positions")
        raw = data.get("positions")
        if not raw or not isinstance(raw, dict):
            return []
        positions = raw.get("position", [])
        if isinstance(positions, dict):
            positions = [positions]
        return positions if positions else []
    except Exception as exc:
        logger.error("Positions fetch failed: %s", exc)
        return []


def get_account_balance(account_id: str) -> Optional[dict]:
    try:
        data = _get(f"accounts/{account_id}/balances")
        return data.get("balances")
    except Exception as exc:
        logger.error("Balance fetch failed: %s", exc)
        return None


# ── Orders ────────────────────────────────────────────────────────────────────

def place_equity_order(
    account_id: str,
    symbol: str,
    side: str,       # "buy" or "sell"
    quantity: int,
    order_type: str = "market",
    duration: str   = "day",
    limit_price: Optional[float] = None,
) -> Optional[dict]:
    payload = {
        "class":    "equity",
        "symbol":   symbol,
        "side":     side,
        "quantity": str(quantity),
        "type":     order_type,
        "duration": duration,
    }
    if order_type == "limit" and limit_price is not None:
        payload["price"] = str(limit_price)
    try:
        return _post(f"accounts/{account_id}/orders", payload)
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
    payload = {
        "class":          "option",
        "option_symbol":  option_symbol,
        "side":           side,
        "quantity":       str(quantity),
        "type":           order_type,
        "duration":       duration,
    }
    if order_type == "limit" and limit_price is not None:
        payload["price"] = str(limit_price)
    try:
        return _post(f"accounts/{account_id}/orders", payload)
    except Exception as exc:
        logger.error("Option order failed %s %s %s: %s", side, quantity, option_symbol, exc)
        return None


def find_option_symbol(symbol: str, expiration: str, strike: float, option_type: str) -> Optional[str]:
    """Look up the OCC option symbol from the chain."""
    chain = get_option_chain(symbol, expiration)
    opt_type_full = "call" if option_type.lower() == "call" else "put"
    for contract in chain:
        if (
            contract.get("option_type") == opt_type_full
            and abs(float(contract.get("strike", 0)) - strike) < 0.01
        ):
            return contract.get("symbol")
    logger.warning("Option not found: %s %s %.2f %s", symbol, expiration, strike, option_type)
    return None
