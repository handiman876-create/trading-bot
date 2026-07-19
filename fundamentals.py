"""
Fundamentals filter for the A/B screen experiment — Screen B only.

Answers one question per symbol: was the company profitable (positive net
income) in at least SCREEN_B_MIN_PROFITABLE_Q of its last
SCREEN_B_QUARTERS_LOOKBACK reported quarters? Screen B keeps only names that
pass; Screen A ignores this module entirely. Never touches the live trading path.

Rate-limit discipline: quarterly financials change ~once a quarter, but the
screen runs twice a month against a shared free-tier Polygon key (5 calls/min,
also used by autodiscover). So results are cached to FUNDAMENTALS_CACHE_FILE with
a long TTL — a cold Screen B costs ~5-20 financials calls (one per candidate
until MOMENTUM_SLOT_SIZE profitable names are found); a warm one costs ~zero.

Null handling: a quarter whose filing omits net_income counts as NOT profitable
(a data gap is not evidence of profit). A symbol with fewer than
SCREEN_B_MIN_PROFITABLE_Q reported quarters can never pass.
"""

import json
import logging
import os
from datetime import date, datetime, timezone

import config
import polygon_client as pc

logger = logging.getLogger("fundamentals")

# Observability: how many symbols the profitability filter dropped / passed on the
# last run, mirroring the momentum screen's sector-filter counters so we can tell
# whether Screen B is meaningfully diverging from Screen A.
last_pass = 0
last_drop = 0


def _cache_path() -> str:
    return config.FUNDAMENTALS_CACHE_FILE


def _load_cache() -> dict:
    try:
        with open(_cache_path()) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    path = _cache_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _fresh(entry: dict, today: date) -> bool:
    """True if a cache entry is younger than the configured TTL."""
    try:
        fetched = datetime.fromisoformat(entry["fetched"]).date()
    except (KeyError, ValueError, TypeError):
        return False
    return (today - fetched).days < config.FUNDAMENTALS_CACHE_TTL_DAYS


def profitable_quarters(quarters: list[dict]) -> int:
    """Count quarters with strictly positive net income. Pure — no I/O — so the
    profitability rule is unit-tested directly. Null/absent net_income does NOT
    count (a data gap is not a profitable quarter)."""
    n = 0
    for q in quarters:
        ni = q.get("net_income")
        if isinstance(ni, (int, float)) and ni > 0:
            n += 1
    return n


def is_profitable(symbol: str, *, cache: dict | None = None,
                  today: date | None = None) -> bool | None:
    """True if `symbol` cleared the profitability bar, False if not, None if the
    financials couldn't be fetched (caller decides — Screen B treats None as a
    fail so a data outage can't smuggle an unvetted name into the filtered set).

    `cache` lets a caller thread one dict across many symbols so the file is read
    and written once per run rather than per symbol.
    """
    today = today or datetime.now(timezone.utc).date()
    own_cache = cache is None
    cache = _load_cache() if own_cache else cache

    key = symbol.upper()
    entry = cache.get(key)
    if entry and _fresh(entry, today):
        return entry.get("profitable")

    try:
        quarters = pc.get_quarterly_financials(key, limit=config.SCREEN_B_QUARTERS_LOOKBACK)
    except pc.PolygonError as exc:
        logger.warning("financials fetch for %s failed: %s", symbol, exc)
        return None

    n_prof = profitable_quarters(quarters)
    passed = (len(quarters) >= config.SCREEN_B_MIN_PROFITABLE_Q
              and n_prof >= config.SCREEN_B_MIN_PROFITABLE_Q)
    cache[key] = {
        "profitable": passed,
        "n_profitable": n_prof,
        "n_quarters": len(quarters),
        "fetched": today.isoformat(),
    }
    if own_cache:
        _save_cache(cache)
    logger.info("  %-6s profitable=%s (%d/%d quarters positive)",
                symbol, passed, n_prof, len(quarters))
    return passed
