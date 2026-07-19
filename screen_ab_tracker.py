"""
A/B screen tracker — observation only, NEVER feeds the live bot.

Each rotation (1st/15th, run right after the live momentum screen) this:

  1. Collects the S&P 500 grouped-daily bars ONCE (via momentum_screen).
  2. MEASURES the previous rotation's picks: their 2-week forward return, using
     the entry close recorded last time and the latest close now. Aug 1 picks are
     measured on Aug 15; Aug 15 picks on Sep 1 (the next-rotation cadence).
  3. RECORDS this rotation's two screens:
       Screen A = the live 20-day momentum top 5 (same ranking the bot uses).
       Screen B = the same ranking filtered to 4/5-quarters-profitable names.
     For each pick it stores the entry close, ATM implied vol (None until an
     options-entitled Polygon key exists), and realized vol.
  4. Keeps a running winner tally, but draws NO conclusion before
     SCREEN_AB_MIN_ROTATIONS rotations.

It writes only SCREEN_AB_TRACKING_FILE. It never touches MOMENTUM_WATCHLIST_FILE,
so Screen A's live path is completely independent of this experiment.

Run:
  python3 screen_ab_tracker.py            # measure prior + record this rotation
  python3 screen_ab_tracker.py --dry-run  # compute + print, write nothing
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import fundamentals
import momentum_screen as ms
import polygon_client as pc

logger = logging.getLogger("screen_ab_tracker")


def _today_et() -> str:
    return datetime.now(ZoneInfo(config.MARKET_TZ)).date().isoformat()


def _load_tracking() -> dict:
    try:
        with open(config.SCREEN_AB_TRACKING_FILE) as f:
            doc = json.load(f)
        if isinstance(doc, dict) and "rotations" in doc:
            return doc
    except (OSError, json.JSONDecodeError):
        pass
    return {"rotations": [], "winner_tally": {"screen_a": 0, "screen_b": 0, "tie": 0},
            "updated": None}


def _save_tracking(doc: dict) -> None:
    path = config.SCREEN_AB_TRACKING_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _avg(values: list) -> float | None:
    """Mean of the non-None numbers, rounded, or None if there are none."""
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 1)


def _sector_breakdown(picks: list[dict], sectors: dict) -> dict:
    out: dict[str, int] = {}
    for p in picks:
        info = sectors.get(p["symbol"]) or {}
        sec = info.get("sector") or "Unknown"
        out[sec] = out.get(sec, 0) + 1
    return out


def _build_screen_record(picks: list[dict], by_date: dict, dates_asc: list[str],
                         sectors: dict, *, iv_cache: dict) -> dict:
    """Turn a screen's picks into a recordable block: per-pick entry close / IV /
    realized vol, plus screen-level averages and a sector breakdown."""
    latest = dates_asc[-1] if dates_asc else None
    detail = []
    for p in picks:
        sym = p["symbol"]
        closes, _vols = ms._series_for(sym, dates_asc, by_date)
        if closes:
            entry_close = round(closes[-1], 4)
            rv = ms.realized_vol(closes)
        else:  # pick came from the ranked set, so this is unexpected — fall back
            bar = by_date.get(latest, {}).get(sym) if latest else None
            entry_close = round(bar["close"], 4) if bar and bar.get("close") else None
            rv = None
        # IV once per unique symbol (Screen A and B overlap on profitable names).
        if sym not in iv_cache:
            iv_cache[sym] = pc.get_atm_option_iv(sym, underlying_price=entry_close)
        iv = iv_cache[sym]
        if iv is None:
            logger.warning("%s picked, IV=None (fetch failed / tier not entitled)", sym)
        detail.append({
            "symbol":     sym,
            "return_20d": p.get("return_20d"),
            "rsi":        p.get("rsi"),
            "rel_volume": p.get("rel_volume"),
            "entry_close": entry_close,
            "iv":         iv,
            "rv":         rv,
        })
    return {
        "picks":  [d["symbol"] for d in detail],
        "detail": detail,
        "avg_iv": _avg([d["iv"] for d in detail]),
        "avg_rv": _avg([d["rv"] for d in detail]),
        "sector_breakdown": _sector_breakdown(picks, sectors),
    }


def _measure_returns(screen_block: dict, by_date: dict, latest: str) -> dict:
    """2-week forward return per pick = latest_close / entry_close - 1, plus the
    average. Picks whose exit close is missing are skipped (and noted by absence),
    never counted as zero."""
    returns: dict[str, float] = {}
    for d in screen_block.get("detail", []):
        sym = d["symbol"]
        entry = d.get("entry_close")
        bar = by_date.get(latest, {}).get(sym)
        cur = bar.get("close") if bar else None
        if entry and cur:
            returns[sym] = round(cur / entry - 1.0, 4)
    returns["avg"] = _avg(list(returns.values()))
    return returns


def _decide_winner(a_ret: dict, b_ret: dict, b_had_picks: bool) -> str:
    """Higher average 2-week return wins. An empty Screen B counts as a Screen A
    win by default (config decision): no filtered candidates means the filter
    would have left the bot core-only, which the live screen beats by definition."""
    if not b_had_picks:
        return "screen_a"
    a, b = a_ret.get("avg"), b_ret.get("avg")
    if a is None and b is None:
        return "tie"
    if a is None:
        return "screen_b"
    if b is None:
        return "screen_a"
    if a > b:
        return "screen_a"
    if b > a:
        return "screen_b"
    return "tie"


def run(dry_run: bool = False) -> int:
    doc = _load_tracking()
    today = _today_et()

    if doc["rotations"] and doc["rotations"][-1]["rotation_date"] == today:
        logger.info("Already recorded a rotation for %s — nothing to do "
                    "(idempotent re-run).", today)
        return 0

    # One universe-wide collection, reused for both this rotation's screens and
    # last rotation's return measurement.
    ranked, by_date, dates_asc = ms.collect_and_rank()
    latest = dates_asc[-1]
    sectors = ms._load_sectors()

    # ── 1. Measure the previous rotation, if it's still open ──────────────────
    if doc["rotations"] and doc["rotations"][-1].get("two_week_results") is None:
        prev = doc["rotations"][-1]
        a_ret = _measure_returns(prev["screen_a"], by_date, latest)
        b_ret = _measure_returns(prev["screen_b"], by_date, latest)
        b_had_picks = bool(prev["screen_b"]["picks"])
        winner = _decide_winner(a_ret, b_ret, b_had_picks)
        prev["two_week_results"] = {
            "measured_on":     today,
            "screen_a_returns": a_ret,
            "screen_b_returns": b_ret,
            "winner":          winner,
        }
        doc["winner_tally"][winner] = doc["winner_tally"].get(winner, 0) + 1
        logger.info("Measured %s: A avg=%s  B avg=%s  winner=%s",
                    prev["rotation_date"], a_ret.get("avg"), b_ret.get("avg"), winner)

    # ── 2. Record this rotation's picks ───────────────────────────────────────
    iv_cache: dict = {}
    a_picks = ranked[: config.MOMENTUM_SLOT_SIZE]
    b_cache = fundamentals._load_cache()
    b_picks = ms.run_screen_b(ranked, cache=b_cache)
    if not b_picks:
        logger.warning("Screen B: 0 profitable candidates in top %d",
                       config.SCREEN_B_TOP_N)

    record = {
        "rotation_date": today,
        "screen_a": _build_screen_record(a_picks, by_date, dates_asc, sectors,
                                         iv_cache=iv_cache),
        "screen_b": _build_screen_record(b_picks, by_date, dates_asc, sectors,
                                         iv_cache=iv_cache),
        "two_week_results": None,
    }
    doc["rotations"].append(record)
    doc["updated"] = datetime.now(ZoneInfo(config.MARKET_TZ)).isoformat()

    logger.info("Rotation %s recorded — A picks=%s  B picks=%s  (rotations tracked: %d)",
                today, record["screen_a"]["picks"], record["screen_b"]["picks"],
                len(doc["rotations"]))

    if dry_run:
        logger.info("[dry-run] not writing %s", config.SCREEN_AB_TRACKING_FILE)
        print(json.dumps(record, indent=2))
        return 0

    _save_tracking(doc)
    logger.info("Wrote %s", config.SCREEN_AB_TRACKING_FILE)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="A/B momentum screen tracker (observation only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print without writing the tracking file")
    args = parser.parse_args()
    try:
        return run(dry_run=args.dry_run)
    except Exception as exc:
        logger.error("A/B tracker failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
