"""
Momentum screen — twice-monthly watchlist rotation (see config "Momentum
Rotation"). Screens the S&P 500 for momentum leaders and writes the dynamic
slot the bot folds into its live list.

Data source: Polygon grouped-daily bars (one call per trading day covers the
whole universe), NOT TradeStation — TradeStation's REST API has no screener.
RSI is computed with the bot's own indicators.rsi() so "RSI 50-70" means exactly
what the live EMA/RSI signal means (single source of truth, no Wilder/simple
drift).

Criteria (all must hold), from config:
  * 20-day price return  > MOM_RETURN_MIN (+5%)
  * latest volume        > trailing 20-day average volume
  * MOM_RSI_MIN <= RSI(14) <= MOM_RSI_MAX  (50..70)
  * market cap > $5B      — satisfied by construction (universe is the S&P 500)

Survivors are ranked by 20-day return, core names are excluded, and the top
MOMENTUM_SLOT_SIZE are written atomically to MOMENTUM_WATCHLIST_FILE.

Run:
  python3 momentum_screen.py            # screen + write the file
  python3 momentum_screen.py --dry-run  # screen + print, write nothing
"""

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import math
import statistics

import config
import fundamentals
import indicators as ind
import polygon_client as pc

logger = logging.getLogger("momentum_screen")

# Observability: how many candidates the sector filter removed on the last screen()
# run, and how many had no sector data (fail-open — counted, not excluded). Every
# filter carries a counter so we can tell whether it's still earning its keep.
_sector_skips = 0
_sector_unknown = 0

# Enough trading days to cover the 20-day return window (needs LOOKBACK+1 closes)
# plus RSI(14) warm-up. ~35 trading days is comfortable headroom.
_TRADING_DAYS_NEEDED = config.MOM_LOOKBACK + 15
# Cap the calendar walk-back so a data outage can't loop forever (~35 trading
# days is ~50 calendar days; 75 leaves slack for holidays).
_MAX_CALENDAR_DAYS = 75


def _load_universe() -> list[str]:
    path = config.MOMENTUM_UNIVERSE_FILE
    with open(path) as f:
        doc = json.load(f)
    syms = doc.get("symbols", [])
    if not syms:
        raise ValueError(f"{path} contains no symbols")
    return syms


def _load_sectors() -> dict:
    """Return the per-symbol GICS map {symbol: {"sector","sub_industry"}} from the
    universe file, or {} if the file predates sector enrichment. Missing data
    fails OPEN (nothing excluded) rather than dropping the whole screen, so an old
    sp500.json degrades to the pre-filter behaviour with a warning."""
    try:
        with open(config.MOMENTUM_UNIVERSE_FILE) as f:
            sectors = json.load(f).get("sectors", {})
    except (OSError, json.JSONDecodeError):
        sectors = {}
    if not sectors:
        logger.warning("No 'sectors' map in %s — sector filter is a no-op. "
                       "Run refresh_sp500.py to enrich it.",
                       config.MOMENTUM_UNIVERSE_FILE)
    return sectors if isinstance(sectors, dict) else {}


def _normalize_symbol(sym: str) -> str:
    """Map a Polygon ticker to the form TradeStation's order path expects.
    S&P 500 momentum leaders are almost all plain tickers; the only shaping
    needed today is upper-casing. Dotted class shares (BRK.B, BF.B) pass through
    unchanged and are extremely unlikely to be momentum leaders."""
    return sym.strip().upper()


def _collect_grouped_daily() -> dict[str, dict]:
    """Walk backwards from today pulling grouped-daily bars until we have enough
    trading days. Returns {date_str: {symbol: bar}}. Non-trading days (empty
    Polygon result) are skipped, not counted."""
    by_date: dict[str, dict] = {}
    d = date.today()
    calendar_walked = 0
    while len(by_date) < _TRADING_DAYS_NEEDED and calendar_walked < _MAX_CALENDAR_DAYS:
        ds = d.isoformat()
        bars = pc.get_grouped_daily(ds)
        if bars:
            by_date[ds] = bars
            logger.info("  %s: %d tickers", ds, len(bars))
        d -= timedelta(days=1)
        calendar_walked += 1
    if len(by_date) < config.MOM_LOOKBACK + 1:
        raise pc.PolygonError(
            f"only {len(by_date)} trading days collected; need >= {config.MOM_LOOKBACK + 1}")
    return by_date


def _series_for(symbol: str, dates_asc: list[str], by_date: dict[str, dict]):
    """Return (closes, volumes) chronological for `symbol`, or (None, None) if the
    symbol is missing on any collected day (incomplete history → skip it)."""
    closes, volumes = [], []
    for ds in dates_asc:
        bar = by_date[ds].get(symbol)
        if not bar or bar.get("close") is None or bar.get("volume") is None:
            return None, None
        closes.append(float(bar["close"]))
        volumes.append(float(bar["volume"]))
    return closes, volumes


def evaluate_symbol(symbol: str, closes: list[float], volumes: list[float]) -> dict | None:
    """Apply the momentum criteria to one symbol's chronological OHLCV series.
    Returns a survivor dict (symbol/return_20d/rsi/rel_volume) or None if any
    criterion fails or history is too short. Pure — no I/O — so it's the unit the
    tests exercise directly."""
    lb = config.MOM_LOOKBACK
    if closes is None or len(closes) < lb + 1:
        return None

    ret_20d = closes[-1] / closes[-1 - lb] - 1.0
    avg_vol = sum(volumes[-lb:]) / lb
    latest_vol = volumes[-1]
    rsi_val = float(ind.rsi(pd.Series(closes), 14).iloc[-1])

    if (ret_20d > config.MOM_RETURN_MIN
            and latest_vol > avg_vol
            and config.MOM_RSI_MIN <= rsi_val <= config.MOM_RSI_MAX):
        return {
            "symbol":     symbol,
            "return_20d": round(ret_20d, 4),
            "rsi":        round(rsi_val, 1),
            "rel_volume": round(latest_vol / avg_vol, 2) if avg_vol else None,
        }
    return None


def realized_vol(closes: list[float], window: int = None) -> float | None:
    """Annualized realized volatility (%) over the last `window` daily returns.

    Supplementary premium proxy recorded next to the (paid, often-unavailable)
    implied vol: high-realized-vol names are high-IV names, so this still lets the
    A/B compare "how volatile are Screen A's picks vs Screen B's" while avg_iv is
    None. Pure — no I/O — so it's unit-tested directly. Returns None if there
    aren't enough closes for a 2+ point sample."""
    window = window or config.SCREEN_AB_RV_WINDOW
    if not closes or len(closes) < 3:
        return None
    tail = closes[-(window + 1):]
    rets = [math.log(tail[i] / tail[i - 1])
            for i in range(1, len(tail)) if tail[i - 1] > 0 and tail[i] > 0]
    if len(rets) < 2:
        return None
    return round(statistics.stdev(rets) * math.sqrt(252) * 100.0, 1)


def run_screen_b(ranked: list[dict], *, cache: dict | None = None) -> list[dict]:
    """Screen B: from the top SCREEN_B_TOP_N of the SAME momentum ranking, keep
    only names that clear the profitability filter, and take the first
    MOMENTUM_SLOT_SIZE that survive. Returns the same row shape as `screen()`.

    Reaches deeper than Screen A only to backfill slots vacated by unprofitable
    names — the momentum criteria and ordering are identical, so profitability is
    the only difference between A and B. A symbol whose financials can't be
    fetched (is_profitable -> None) is treated as NOT profitable, so a Polygon
    outage can't smuggle an unvetted name into the filtered set. May return fewer
    than MOMENTUM_SLOT_SIZE (even zero) if the top-N has too few profitable names;
    the caller records that rather than backfilling with unvetted picks."""
    if cache is None:
        cache = fundamentals._load_cache()
    picks: list[dict] = []
    considered = 0
    for row in ranked[: config.SCREEN_B_TOP_N]:
        considered += 1
        if fundamentals.is_profitable(row["symbol"], cache=cache) is True:
            picks.append(row)
            if len(picks) >= config.MOMENTUM_SLOT_SIZE:
                break
    fundamentals._save_cache(cache)
    logger.info("Screen B: %d profitable of %d considered (top-%d)",
                len(picks), considered, config.SCREEN_B_TOP_N)
    return picks


def _excluded_set() -> set[str]:
    """config.EXCLUDED_SECTORS as a casefolded set for case-insensitive matching."""
    return {s.casefold() for s in getattr(config, "EXCLUDED_SECTORS", [])}


def _is_excluded_sector(info: dict | None, excluded: set[str]) -> bool:
    """True if a symbol's GICS classification lands in an excluded bucket. Matches
    the excluded names against BOTH the sector and sub-industry fields (a sector
    name and a sub-industry name never collide). Pure — no I/O — so the tests
    exercise it directly. Missing info (symbol absent from the map) fails OPEN:
    returns False so a data gap can't silently drop a candidate."""
    if not info:
        return False
    fields = (info.get("sector", ""), info.get("sub_industry", ""))
    return any(f.casefold() in excluded for f in fields if f)


def count_excluded_universe() -> tuple[int, int]:
    """(excluded, total) — how many universe names the sector filter removes.
    Pure file read (no Polygon), for the bot's startup banner. Core names aren't
    in any excluded sector, so leaving them in doesn't change the count."""
    universe = _load_universe()
    sectors = _load_sectors()
    excluded = _excluded_set()
    n = sum(1 for s in universe if _is_excluded_sector(sectors.get(s), excluded))
    return n, len(universe)


def collect_and_rank() -> tuple[list[dict], dict, list[str]]:
    """Collect grouped-daily bars once and return the FULL ranked survivor list
    (best 20-day return first), plus the raw (by_date, dates_asc) so a caller can
    reuse the same universe-wide bars — e.g. to look up any symbol's latest close
    without spending more Polygon calls.

    This is `screen()` minus the final slice: extracted so both the live screen
    (top MOMENTUM_SLOT_SIZE) and the A/B tracker (which needs the deeper ranking
    for Screen B and the bars for return measurement) draw from ONE computation."""
    global _sector_skips, _sector_unknown
    _sector_skips = _sector_unknown = 0

    universe = _load_universe()
    sectors = _load_sectors()
    excluded = _excluded_set()
    core = {s.upper() for s in config.CORE_WATCHLIST}
    logger.info("Universe: %d symbols; collecting grouped-daily bars...", len(universe))

    by_date = _collect_grouped_daily()
    dates_asc = sorted(by_date)
    logger.info("Collected %d trading days (%s .. %s)",
                len(dates_asc), dates_asc[0], dates_asc[-1])

    survivors: list[dict] = []
    for raw in universe:
        sym = _normalize_symbol(raw)
        if sym in core:
            continue
        # Sector filter — skip excluded GICS sectors/sub-industries (config.
        # EXCLUDED_SECTORS). Keyed by the raw sp500.json symbol form (the sectors
        # map uses the same form). Fails open on missing data.
        info = sectors.get(raw)
        if excluded:
            if info is None:
                _sector_unknown += 1
            elif _is_excluded_sector(info, excluded):
                _sector_skips += 1
                logger.info("SECTOR SKIP %-6s — %s / %s", sym,
                            info.get("sector"), info.get("sub_industry"))
                continue
        closes, volumes = _series_for(raw, dates_asc, by_date)
        row = evaluate_symbol(sym, closes, volumes)
        if row:
            survivors.append(row)

    if excluded:
        logger.info("Sector filter: %d skipped, %d had no sector data (kept)",
                    _sector_skips, _sector_unknown)
    survivors.sort(key=lambda r: r["return_20d"], reverse=True)
    logger.info("%d symbols passed all criteria", len(survivors))
    return survivors, by_date, dates_asc


def screen() -> list[dict]:
    """Run the screen. Returns the top MOMENTUM_SLOT_SIZE ranked survivors
    (excluding core) as dicts with symbol/return_20d/rsi/rel_volume, best return
    first. This is the LIVE Screen A output — its behavior is unchanged."""
    survivors, _by_date, _dates_asc = collect_and_rank()
    return survivors[: config.MOMENTUM_SLOT_SIZE]


def _atomic_write(picks: list[dict], universe_size: int) -> None:
    doc = {
        "symbols":   [p["symbol"] for p in picks],
        "detail":    picks,
        "generated": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "lookback_days": config.MOM_LOOKBACK,
            "return_min":    config.MOM_RETURN_MIN,
            "rsi_range":     [config.MOM_RSI_MIN, config.MOM_RSI_MAX],
            "volume":        "latest > trailing 20-day average",
            "excluded_sectors": config.EXCLUDED_SECTORS,
        },
        "slot_size":     config.MOMENTUM_SLOT_SIZE,
        "universe_size": universe_size,
    }
    path = config.MOMENTUM_WATCHLIST_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)   # atomic on POSIX — the bot never sees a partial file


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="S&P 500 momentum screen")
    parser.add_argument("--dry-run", action="store_true",
                        help="screen and print results without writing the file")
    args = parser.parse_args()

    try:
        picks = screen()
    except Exception as exc:
        logger.error("Screen failed: %s", exc)
        return 1

    if picks:
        for p in picks:
            logger.info("  PICK %-6s  ret20d=%+.1f%%  rsi=%.1f  relvol=%.2f",
                        p["symbol"], p["return_20d"] * 100, p["rsi"], p["rel_volume"])
    else:
        logger.warning("No symbols passed the screen — bot will trade core-only.")

    if args.dry_run:
        logger.info("[dry-run] not writing %s", config.MOMENTUM_WATCHLIST_FILE)
        print(json.dumps([p["symbol"] for p in picks]))
        return 0

    _atomic_write(picks, universe_size=len(_load_universe()))
    logger.info("Wrote %d symbols to %s", len(picks), config.MOMENTUM_WATCHLIST_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
