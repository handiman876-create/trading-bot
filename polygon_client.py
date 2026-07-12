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
