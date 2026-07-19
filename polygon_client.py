"""
Polygon.io REST client — momentum-screen data source ONLY.

Used exclusively by momentum_screen.py for the twice-monthly watchlist rotation.
Deliberately tiny and self-contained: the live trading path never touches
Polygon (that stays on tradestation_client). The grouped-daily endpoint returns
OHLCV for every US stock in a single call, so a whole-universe screen costs ~35
calls; this client self-throttles to POLYGON_MAX_CALLS_PER_MIN so a biweekly EOD
run fits comfortably inside the free tier's 5-calls/minute limit.
"""

import logging
import time
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


class PolygonError(RuntimeError):
    """A Polygon API failure the screen should treat as fatal for this run."""


# ── Rate limiting (free tier: 5 calls/min) ────────────────────────────────────
_MIN_INTERVAL = 60.0 / max(1, config.POLYGON_MAX_CALLS_PER_MIN)
_last_call_at = 0.0


def _throttle() -> None:
    """Block just long enough to stay under the per-minute call budget."""
    global _last_call_at
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def _get(path: str, params: Optional[dict] = None) -> dict:
    if not config.POLYGON_API_KEY:
        raise PolygonError("POLYGON_API_KEY is not set (add it to .env)")
    _throttle()
    url = f"{config.POLYGON_BASE_URL}/{path}"
    q = dict(params or {})
    q["apiKey"] = config.POLYGON_API_KEY
    try:
        resp = requests.get(url, params=q, timeout=30)
    except requests.RequestException as exc:
        raise PolygonError(f"request to {path} failed: {exc}") from exc
    if resp.status_code == 429:
        raise PolygonError(
            "Polygon rate limit hit (429) — lower POLYGON_MAX_CALLS_PER_MIN")
    if resp.status_code != 200:
        raise PolygonError(f"{path} -> HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def get_grouped_daily(date_str: str, adjusted: bool = True) -> dict:
    """OHLCV for every US stock on `date_str` ('YYYY-MM-DD').

    Returns {symbol: {"open","high","low","close","volume"}}. An empty dict means
    a non-trading day (weekend/holiday): Polygon returns an OK status with
    results=[], which the caller simply skips.
    """
    data = _get(
        f"v2/aggs/grouped/locale/us/market/stocks/{date_str}",
        {"adjusted": "true" if adjusted else "false"},
    )
    results = data.get("results") or []
    out: dict[str, dict] = {}
    for r in results:
        sym = r.get("T")
        if not sym:
            continue
        out[sym] = {
            "open":   r.get("o"),
            "high":   r.get("h"),
            "low":    r.get("l"),
            "close":  r.get("c"),
            "volume": r.get("v"),
        }
    return out


def get_quarterly_financials(symbol: str, limit: int = 5) -> list[dict]:
    """Most recent `limit` quarterly filings for `symbol`, newest first.

    Returns a list of {"fiscal_period","fiscal_year","end_date","net_income"};
    `net_income` is None when the filing omits the field (a real data gap — the
    caller must treat null as "not known to be profitable", never as zero). Used
    only by fundamentals.py for Screen B's profitability filter — never by the
    live trading path. This is the SEC-derived /vX/reference/financials endpoint,
    which is available on the free tier (unlike the options snapshot below)."""
    data = _get(
        "vX/reference/financials",
        {
            "ticker": symbol.upper(),
            "timeframe": "quarterly",
            "limit": limit,
            "order": "desc",
            "sort": "period_of_report_date",
        },
    )
    out: list[dict] = []
    for f in data.get("results") or []:
        inc = (f.get("financials") or {}).get("income_statement") or {}
        ni = inc.get("net_income_loss") or {}
        out.append({
            "fiscal_period": f.get("fiscal_period"),
            "fiscal_year":   f.get("fiscal_year"),
            "end_date":      f.get("end_date"),
            "net_income":    ni.get("value"),
        })
    return out


def get_atm_option_iv(symbol: str, underlying_price: float | None = None) -> float | None:
    """Implied volatility (annualized %) of the ~ATM, nearest-expiry option for
    `symbol`, or None if it can't be fetched.

    Reads the unified options snapshot (/v3/snapshot/options/<sym>), keeps the
    soonest non-expired expiry, and within it the strike closest to
    `underlying_price` (falling back to the snapshot's own underlying quote), then
    returns that contract's implied_volatility as a percentage (Polygon reports it
    as a decimal, e.g. 0.652 -> 65.2).

    IV is SUPPLEMENTARY, never a gate: any failure — most notably a tier that is
    not entitled to options data (Polygon returns NOT_AUTHORIZED) — returns None
    so the caller records the pick with iv=None and moves on. Confirmed
    2026-07-19: the free/shared stock key is NOT entitled, so this returns None
    until an options-entitled key is configured; the code then works unchanged."""
    try:
        data = _get(f"v3/snapshot/options/{symbol.upper()}", {"limit": 250})
    except PolygonError as exc:
        logger.warning("IV fetch for %s failed: %s", symbol, exc)
        return None
    if data.get("status") == "NOT_AUTHORIZED":
        logger.warning("IV fetch for %s: NOT_AUTHORIZED (tier lacks options data)", symbol)
        return None
    results = data.get("results") or []
    if not results:
        return None

    def _details(c: dict) -> dict:
        return c.get("details") or {}

    dated = [c for c in results if _details(c).get("expiration_date")]
    if not dated:
        return None
    nearest_exp = min(_details(c)["expiration_date"] for c in dated)
    chain = [c for c in dated if _details(c)["expiration_date"] == nearest_exp]

    px = underlying_price
    if px is None:
        ua = (chain[0].get("underlying_asset") or {})
        px = ua.get("price") or ua.get("value")
    if px is None:
        return None

    def _strike_dist(c: dict) -> float:
        return abs((_details(c).get("strike_price") or 0.0) - px)

    atm = min(chain, key=_strike_dist)
    iv = atm.get("implied_volatility")
    if iv is None:
        return None
    return round(float(iv) * 100.0, 1)
