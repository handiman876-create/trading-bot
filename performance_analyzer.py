"""
Weekly performance analyzer — reads the bot's trade + performance logs, pairs
closed round-trips, and reports realized P&L per entry type against an SPY
buy-and-hold benchmark. Runs Sunday 00:07 ET via performance-analyzer.timer.

Why a cumulative ledger?  The raw logs rotate daily and keep only ~7 rotations
(~1 week), but a strategy needs weeks-to-months of closed trades to be
statistically meaningful. So every run folds the currently-visible trade events
into an append-only, order_id-deduped ledger (data/trade_ledger.json) — the
durable source of truth that outlives log rotation. `closed_trips` is RECOMPUTED
from the ledger's events each run (idempotent), never appended, so re-runs can't
double-count.

MVP scope: realized (closed) round-trips only, priced at signal-time prices
(not broker fills), attributed to the ENTRY's feature. Open positions are counted
but not marked-to-market. See the report header for these caveats.

Run:
  python3 performance_analyzer.py            # update ledger + write both reports
  python3 performance_analyzer.py --dry-run  # compute + print, write nothing
"""

import argparse
import glob
import gzip
import json
import logging
import os
import sys
from datetime import datetime, timedelta

import config

logger = logging.getLogger("performance_analyzer")

_HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH  = os.path.join(_HERE, "data", "trade_ledger.json")
STOPS_PATH   = os.path.join(_HERE, config.STOP_PRICE_FILE)
REPORT_JSON  = os.path.join(_HERE, config.LOG_DIR, "performance_report.json")
REPORT_TXT   = os.path.join(_HERE, config.LOG_DIR, "performance_report.txt")
TRADES_GLOB  = os.path.join(_HERE, config.TRADE_LOG_FILE + "*")   # base + .1 + .2.gz ...
PERF_GLOB    = os.path.join(_HERE, config.PERF_LOG_FILE + "*")

MIN_TRADES_FOR_STATS = 10          # a feature needs this many closed trips to judge
OPTION_MULTIPLIER    = 100         # shares per option contract
LEDGER_VERSION       = 1
STALE_OPEN_DAYS      = 90           # an unpaired entry older than this is pre-analyzer
                                    # noise (its exit rotated out before the ledger
                                    # existed) — excluded from open tracking + pairing

# The four report buckets, in display order.
FEATURES = ["long_fresh_cross", "momentum_alignment", "short", "option"]
FEATURE_LABELS = {
    "long_fresh_cross":   "Long (fresh cross)",
    "momentum_alignment": "Momentum alignment",
    "short":              "Short (death cross)",
    "option":             "Options",
}

# action -> (role, direction). Entry actions open a position; exit actions close.
_ACTION_MAP = {
    "BUY":           ("entry", "long"),
    "SELL":          ("exit",  "long"),
    "SELL_SHORT":    ("entry", "short"),
    "BUY_TO_COVER":  ("exit",  "short"),
    "BUY_TO_OPEN":   ("entry", "option"),
    "SELL_TO_CLOSE": ("exit",  "option"),
}


# ── Classification ────────────────────────────────────────────────────────────

def _feature_for_entry(action: str, notes: str, direction: str) -> str:
    """The entry-type bucket a position is attributed to. Long entries split into
    fresh-cross vs momentum-alignment by their notes string; shorts and options
    are their own bucket."""
    if direction == "short":
        return "short"
    if direction == "option":
        return "option"
    if "momentum alignment" in (notes or "").lower():
        return "momentum_alignment"
    return "long_fresh_cross"


def _classify(action: str, notes: str):
    """(role, direction, feature) for a raw action, or None if the action isn't a
    real trade (TEST artifacts, unknown actions)."""
    if action is None or action.upper().startswith("TEST"):
        return None
    rd = _ACTION_MAP.get(action)
    if rd is None:
        return None
    role, direction = rd
    feature = _feature_for_entry(action, notes, direction) if role == "entry" else None
    return role, direction, feature


# ── Timestamps ────────────────────────────────────────────────────────────────

def _parse_ts(s: str) -> datetime:
    """Parse the leading 'YYYY-MM-DD HH:MM:SS' of a log timestamp (the trailing
    tz abbrev like 'EDT' isn't reliably parseable and all rows are ET anyway).
    Returns datetime.min on failure so a malformed row sorts first, harmlessly."""
    try:
        return datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.min


def _reference_now() -> datetime:
    """Naive 'now' for age comparisons against _parse_ts. Isolated so tests can
    monkeypatch it deterministically."""
    return datetime.now()


def _partition_stale(events: list, cutoff: datetime):
    """Split events into (recent, stale_entries). An ENTRY older than `cutoff` is
    pre-analyzer noise — its exit rotated out before the ledger existed, so it
    would otherwise sit forever as a phantom 'open' position and could mispair
    with a recent exit. Such entries are pulled out of the pairing pool; stale
    NON-entry events are simply dropped."""
    recent, stale_entries = [], []
    for e in events:
        if _parse_ts(e["timestamp"]) >= cutoff:
            recent.append(e)
        elif e.get("role") == "entry":
            stale_entries.append(e)
    return recent, stale_entries


# ── Log reading ───────────────────────────────────────────────────────────────

def _open_maybe_gz(path: str):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path)


def _read_jsonl(path_glob: str):
    """Yield (record, source_file) for every JSONL line across all rotations, and
    return parse bookkeeping. Returns (records, files_parsed, parse_errors)."""
    records, files_parsed, parse_errors = [], [], []
    for path in sorted(glob.glob(path_glob)):
        try:
            with _open_maybe_gz(path) as f:
                n = 0
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append((json.loads(line), path))
                        n += 1
                    except json.JSONDecodeError as exc:
                        parse_errors.append(f"{os.path.basename(path)}:{lineno}: {exc}")
            files_parsed.append(f"{os.path.basename(path)} ({n})")
        except OSError as exc:
            parse_errors.append(f"{os.path.basename(path)}: {exc}")
    return records, files_parsed, parse_errors


# ── Event normalization + ledger merge ────────────────────────────────────────

def _event_key(raw: dict) -> str:
    """Dedup key: order_id when present, else a composite of the immutable fields.
    The composite covers rows logged with a null order_id and still dedups the
    same event seen across overlapping rotations (a real copytruncate hazard)."""
    oid = raw.get("order_id")
    if oid:
        return str(oid)
    return "|".join(str(raw.get(k)) for k in
                    ("timestamp", "action", "symbol", "quantity", "price"))


def _normalize(raw: dict) -> dict | None:
    """Raw trade record -> normalized ledger event, or None if not a real trade."""
    cls = _classify(raw.get("action"), raw.get("notes", ""))
    if cls is None:
        return None
    role, direction, feature = cls
    return {
        "timestamp":       raw.get("timestamp"),
        "action":          raw.get("action"),
        "symbol":          raw.get("symbol"),
        "quantity":        raw.get("quantity"),
        "price":           raw.get("price"),
        "order_type":      raw.get("order_type"),
        "order_id":        raw.get("order_id"),
        "notes":           raw.get("notes"),
        "role":            role,
        "direction":       direction,
        "feature":         feature,
        "estimated_entry": False,
    }


def _merge_events(ledger: dict, raw_records) -> int:
    """Insert normalized trade events into ledger['events'] (dedup by key).
    Returns the number of NEW events added."""
    events = ledger.setdefault("events", {})
    added = 0
    for raw, _src in raw_records:
        ev = _normalize(raw)
        if ev is None:
            continue
        key = _event_key(raw)
        if key not in events:
            events[key] = ev
            added += 1
    return added


def _inject_bootstrap_entries(ledger: dict, stops: dict, open_keys: set) -> int:
    """Spec #9: adopted (bootstrapped) positions have no logged entry. For each
    held name in stop_prices.json that has no OPEN (unpaired) entry of its
    direction — either never logged, or whose only logged entries already closed —
    inject a synthetic ESTIMATED entry (deduped by a bootstrap key) so its
    eventual exit can be paired. `open_keys` is the set of (symbol, direction)
    that currently have an open entry after FIFO pairing. quantity is unknown here
    (stop records don't carry it) and is taken from the exit at pairing time.
    Returns the number injected."""
    events = ledger.setdefault("events", {})
    injected = 0
    for symbol, rec in stops.items():
        direction = rec.get("direction", "long")
        if (symbol, direction) in open_keys:      # already represented by an open entry
            continue
        opened = rec.get("opened") or "1970-01-01"
        key = f"bootstrap|{symbol}|{opened}"
        if key in events:
            continue
        events[key] = {
            "timestamp":       f"{opened} 00:00:00 EDT",
            "action":          "BUY" if direction == "long" else "SELL_SHORT",
            "symbol":          symbol,
            "quantity":        None,                       # filled from the exit
            "price":           rec.get("entry_price"),
            "order_type":      "bootstrap",
            "order_id":        None,
            "notes":           "estimated entry (adopted position, no logged entry)",
            "role":            "entry",
            "direction":       direction,
            "feature":         "long_fresh_cross" if direction == "long" else "short",
            "estimated_entry": True,
        }
        injected += 1
    return injected


# ── Round-trip pairing + P&L ──────────────────────────────────────────────────

def _pnl(direction: str, entry_price: float, exit_price: float, qty: float) -> float:
    """Realized P&L in dollars. Longs profit when price rises, shorts when it
    falls; options are per-contract × 100 shares."""
    qty = abs(qty or 0)
    if direction == "short":
        gross = (entry_price - exit_price) * qty
    else:                                   # long or option
        gross = (exit_price - entry_price) * qty
    if direction == "option":
        gross *= OPTION_MULTIPLIER
    return gross


def _pnl_pct(direction: str, entry_price: float, exit_price: float) -> float | None:
    """Percent return on the entry price (multiplier-independent). None if entry
    price is unusable."""
    if not entry_price:
        return None
    if direction == "short":
        return (entry_price - exit_price) / entry_price
    return (exit_price - entry_price) / entry_price


def _exit_reason(notes: str) -> str:
    """Why a position was closed, from the exit's notes.

      correction — a hand-placed repair of a bug's damage; the strategy never
                   signalled it. Checked FIRST: a correction is a correction
                   regardless of what else the note says.
      stop       — the trailing stop fired.
      signal     — the strategy's own exit logic.
    """
    n = (notes or "").lower()
    if config.CORRECTION_NOTE_MARKER.lower() in n:
        return "correction"
    if "trailing stop" in n:
        return "stop"
    return "signal"


def _pair_round_trips(events: list):
    """FIFO-pair entries and exits per (symbol, direction). An exit closes the
    OLDEST open entry of the same symbol+direction. Returns
    (closed_trips, orphan_exits) — orphan_exits are exits with no open entry
    (missing/unlogged entry; surfaced in Data Quality)."""
    from collections import defaultdict
    open_q = defaultdict(list)              # (symbol, direction) -> [entry events]
    closed, orphans = [], []
    for ev in sorted(events, key=lambda e: _parse_ts(e["timestamp"])):
        key = (ev["symbol"], ev["direction"])
        if ev["role"] == "entry":
            open_q[key].append(ev)
        else:                               # exit
            if not open_q[key]:
                orphans.append(ev)
                continue
            entry = open_q[key].pop(0)
            direction = ev["direction"]
            # qty: the entry's, or the exit's when the entry is a synthetic
            # bootstrap (quantity unknown at injection).
            qty = entry.get("quantity") or ev.get("quantity")
            entry_price = entry.get("price")
            exit_price  = ev.get("price")
            pnl = _pnl(direction, entry_price, exit_price, qty)
            closed.append({
                "symbol":          ev["symbol"],
                "direction":       direction,
                "feature":         entry.get("feature"),
                "qty":             qty,
                "entry_order_id":  entry.get("order_id"),
                "entry_ts":        entry.get("timestamp"),
                "entry_price":     entry_price,
                "estimated_entry": entry.get("estimated_entry", False),
                "exit_order_id":   ev.get("order_id"),
                "exit_ts":         ev.get("timestamp"),
                "exit_price":      exit_price,
                "exit_reason":     _exit_reason(ev.get("notes")),
                "pnl":             round(pnl, 2),
                "pnl_pct":         _pnl_pct(direction, entry_price, exit_price),
                "win":             pnl > 0,
            })
    open_entries = [e for q in open_q.values() for e in q]
    return closed, orphans, open_entries


# ── Aggregation ───────────────────────────────────────────────────────────────

def _aggregate(closed_trips: list) -> dict:
    """Per-feature stats: count, win_rate, avg_pnl, total_pnl, best, worst.

    Correction exits are EXCLUDED. `feature` is attributed from the ENTRY, so a
    hand-placed repair would otherwise be scored against whichever strategy
    feature opened the position — crediting or blaming it for a trade it never
    chose. The samples here are small enough that one artificial round trip
    visibly moves a feature's win rate. The excluded count is surfaced in Data
    Quality rather than dropped silently."""
    agg = {}
    for feat in FEATURES:
        trips = [t for t in closed_trips
                 if t["feature"] == feat and t.get("exit_reason") != "correction"]
        if not trips:
            agg[feat] = {"count": 0}
            continue
        pnls = [t["pnl"] for t in trips]
        wins = sum(1 for t in trips if t["win"])
        best = max(trips, key=lambda t: t["pnl"])
        worst = min(trips, key=lambda t: t["pnl"])
        agg[feat] = {
            "count":     len(trips),
            "wins":      wins,
            "win_rate":  round(wins / len(trips), 4),
            "avg_pnl":   round(sum(pnls) / len(trips), 2),
            "total_pnl": round(sum(pnls), 2),
            "best":      {"symbol": best["symbol"],  "pnl": best["pnl"]},
            "worst":     {"symbol": worst["symbol"], "pnl": worst["pnl"]},
        }
    return agg


def _build_warnings(agg: dict) -> list:
    """WARN only when a feature has >= MIN_TRADES_FOR_STATS closed trips AND
    negative total P&L — enough data to be meaningful."""
    warns = []
    for feat in FEATURES:
        a = agg[feat]
        if a["count"] >= MIN_TRADES_FOR_STATS and a["total_pnl"] < 0:
            warns.append(f"{FEATURE_LABELS[feat]}: NEGATIVE P&L "
                         f"${a['total_pnl']:+,.2f} over {a['count']} trades")
    return warns


# ── SPY buy-and-hold benchmark ────────────────────────────────────────────────

def _equity_snapshots():
    """All {ts, equity} snapshots from performance.log rotations, sorted oldest
    first (empty list if none)."""
    records, _f, _e = _read_jsonl(PERF_GLOB)
    snaps = [{"ts": r["timestamp"], "equity": r["total_equity"]}
             for (r, _s) in records if r.get("total_equity") is not None]
    snaps.sort(key=lambda s: _parse_ts(s["ts"]))
    return snaps


def _spy_close_on_or_before(bars: list, date_str: str):
    """SPY close on date_str, or the nearest prior trading day, from get_historical
    bars (each has 'date' ISO + 'close'). None if no bar is on/before the date."""
    by_date = {}
    for b in bars:
        d = (b.get("date") or "")[:10]
        if d and b.get("close") is not None:
            by_date[d] = b["close"]
    candidates = sorted(d for d in by_date if d <= date_str)
    return by_date[candidates[-1]] if candidates else None


# A single-step equity jump beyond this ratio is almost certainly a
# deposit/withdrawal (the sandbox was refunded 89k -> 1M), not trading. We start
# the SPY comparison AFTER the last such jump so a funding event can't masquerade
# as return.
_FUNDING_RATIO = 3.0


def _trim_after_funding(snaps: list) -> list:
    """Return the snapshots from just after the LAST large single-step equity jump
    (a deposit/withdrawal). If there's no such jump, returns the list unchanged, so
    the comparison always covers only the period since the account was last
    funded."""
    start = 0
    for i in range(1, len(snaps)):
        prev, cur = snaps[i - 1]["equity"], snaps[i]["equity"]
        if prev and (cur / prev > _FUNDING_RATIO or cur / prev < 1.0 / _FUNDING_RATIO):
            start = i
    return snaps[start:]


def _spy_comparison():
    """Compare bot equity growth to a same-period SPY buy-and-hold. Returns a dict
    (never raises) — degrades to {'available': False, 'reason': ...} on any missing
    data / API failure so the report still writes.

    The window is CLAMPED to the SPY history we can fetch (get_historical returns a
    bounded number of daily bars), and the baseline is the first equity snapshot
    inside that window — so a months-old equity curve (with account-funding events)
    can't blow up the comparison."""
    all_snaps = _equity_snapshots()
    if not all_snaps:
        return {"available": False, "reason": "no performance.log equity snapshots"}
    snaps = _trim_after_funding(all_snaps)
    funded_after = len(snaps) < len(all_snaps)     # a funding event was trimmed off
    try:
        import tradestation_client as tc
        bars = tc.get_historical("SPY", days=60)
    except Exception as exc:                       # network/creds/import — degrade
        return {"available": False, "reason": f"SPY history unavailable ({exc})"}
    spy_dates = sorted((b.get("date") or "")[:10] for b in bars
                       if b.get("date") and b.get("close") is not None)
    if not spy_dates:
        return {"available": False, "reason": "SPY history returned no usable bars"}
    spy_first = spy_dates[0]
    # Baseline = earliest equity snapshot on/after the SPY window start.
    in_window = [s for s in snaps if s["ts"][:10] >= spy_first]
    if not in_window:
        return {"available": False,
                "reason": f"no equity snapshots within SPY window (>= {spy_first})"}
    base, end = in_window[0], in_window[-1]
    d0, d1 = base["ts"][:10], end["ts"][:10]
    spy0 = _spy_close_on_or_before(bars, d0)
    spy1 = _spy_close_on_or_before(bars, d1)
    if not spy0 or not spy1:
        return {"available": False, "reason": "SPY closes not found for clamped period"}
    bot_ret = (end["equity"] / base["equity"] - 1.0) if base["equity"] else None
    spy_ret = spy1 / spy0 - 1.0
    ratio = (end["equity"] / base["equity"]) if base["equity"] else 1.0
    funding = ratio > _FUNDING_RATIO or ratio < 1.0 / _FUNDING_RATIO
    return {
        "available":     True,
        "period":        [d0, d1],
        "clamped":       d0 != snaps[0]["ts"][:10],
        "bot_start_eq":  round(base["equity"], 2),
        "bot_end_eq":    round(end["equity"], 2),
        "bot_return":    round(bot_ret, 6) if bot_ret is not None else None,
        "spy_start":     spy0,
        "spy_end":       spy1,
        "spy_return":    round(spy_ret, 6),
        "spy_bh_equity": round(base["equity"] * (spy1 / spy0), 2),
        "delta_vs_spy":  round((bot_ret - spy_ret), 6) if bot_ret is not None else None,
        "funding_suspected": funding,
        "funding_trimmed":   funded_after,
    }


# ── Ledger persistence ────────────────────────────────────────────────────────

def _load_ledger() -> dict:
    try:
        with open(LEDGER_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("events"), dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"version": LEDGER_VERSION, "events": {}, "closed_trips": []}


def _save_ledger(ledger: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    tmp = f"{LEDGER_PATH}.tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2)
        f.write("\n")
    os.replace(tmp, LEDGER_PATH)


# ── Report assembly + rendering ───────────────────────────────────────────────

def _now_ts() -> str:
    import pytz
    return datetime.now(pytz.timezone(config.MARKET_TZ)).strftime("%Y-%m-%d %H:%M:%S %Z")


def build_report(ledger: dict, stops: dict, data_quality: dict) -> dict:
    all_events = list(ledger["events"].values())
    cutoff = _reference_now() - timedelta(days=STALE_OPEN_DAYS)
    # Drop pre-analyzer entries (>90d) so they can't sit as phantom opens or
    # mispair with recent exits; keep them only as a count for Data Quality.
    events, stale_entries = _partition_stale(all_events, cutoff)

    # Pair once to see which held positions lack an OPEN entry, inject synthetic
    # bootstrap entries for those, then re-pair so their exits can match.
    _c0, _o0, open0 = _pair_round_trips(events)
    open_keys = {(e["symbol"], e["direction"]) for e in open0}
    injected = _inject_bootstrap_entries(ledger, stops, open_keys)
    if injected:
        events, stale_entries = _partition_stale(list(ledger["events"].values()), cutoff)
    closed, orphans, open_entries = _pair_round_trips(events)
    ledger["closed_trips"] = closed        # recomputed view, not appended

    data_quality = dict(data_quality)
    data_quality["bootstrap_injected"] = injected
    data_quality["stale_pre_analyzer_entries"] = len(stale_entries)
    agg = _aggregate(closed)
    est_closed = sum(1 for t in closed if t["estimated_entry"])
    est_open   = sum(1 for e in open_entries if e.get("estimated_entry"))

    data_quality.update({
        "correction_trips_excluded":    sum(1 for t in closed
                                            if t.get("exit_reason") == "correction"),
        "estimated_entry_trips_closed": est_closed,
        "estimated_entry_open":         est_open,
        "orphan_exits_missing_entry":   [
            {"symbol": o["symbol"], "action": o["action"], "ts": o["timestamp"]}
            for o in orphans],
        "closed_trips":                 len(closed),
        "open_entries":                 len(open_entries),
    })

    return {
        "generated":    _now_ts(),
        "scope":        "MVP: realized closed round-trips, signal-time prices, "
                        "attributed to entry feature",
        "ledger_span":  [events and min(events, key=lambda e: _parse_ts(e["timestamp"]))["timestamp"][:10],
                         events and max(events, key=lambda e: _parse_ts(e["timestamp"]))["timestamp"][:10]]
                        if events else [None, None],
        "per_feature":  agg,
        "spy":          _spy_comparison(),
        "warnings":     _build_warnings(agg),
        "data_quality": data_quality,
    }


def _fmt_money(v) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "n/a"


def _fmt_pct(v) -> str:
    return f"{v*100:+.2f}%" if isinstance(v, (int, float)) else "n/a"


def render_txt(report: dict) -> str:
    L = []
    L.append("TradeStation Bot — Weekly Performance Report")
    L.append(f"generated {report['generated']}")
    L.append(f"scope: {report['scope']}")
    span = report["ledger_span"]
    dq = report["data_quality"]
    L.append(f"ledger span: {span[0]} .. {span[1]}   |   "
             f"closed trips: {dq['closed_trips']}   |   open: {dq['open_entries']}")
    L.append("")
    L.append("PER-FEATURE (realized, closed round-trips)")
    for feat in FEATURES:
        a = report["per_feature"][feat]
        label = FEATURE_LABELS[feat]
        if a["count"] < MIN_TRADES_FOR_STATS:
            L.append(f"  {label:22} trades={a['count']:<3} "
                     f"INSUFFICIENT DATA (<{MIN_TRADES_FOR_STATS} trades)"
                     + (f"  [so far: total={_fmt_money(a.get('total_pnl'))}, "
                        f"win%={a['win_rate']*100:.0f}]" if a["count"] else ""))
        else:
            L.append(f"  {label:22} trades={a['count']:<3} "
                     f"win%={a['win_rate']*100:>5.1f}  avg={_fmt_money(a['avg_pnl'])}  "
                     f"total={_fmt_money(a['total_pnl'])}  "
                     f"best={a['best']['symbol']} {_fmt_money(a['best']['pnl'])}  "
                     f"worst={a['worst']['symbol']} {_fmt_money(a['worst']['pnl'])}")
    L.append("")
    L.append("OVERALL vs SPY BUY & HOLD")
    spy = report["spy"]
    if spy.get("available"):
        notes = []
        if spy.get("funding_trimmed"):
            notes.append("since last deposit/withdrawal")
        if spy.get("clamped"):
            notes.append("clamped to SPY history")
        suffix = f"  [{'; '.join(notes)}]" if notes else ""
        L.append(f"  period:     {spy['period'][0]} .. {spy['period'][1]}{suffix}")
        L.append(f"  bot equity: {_fmt_money(spy['bot_start_eq'])} -> "
                 f"{_fmt_money(spy['bot_end_eq'])}  ({_fmt_pct(spy['bot_return'])})")
        L.append(f"  SPY B&H:    {_fmt_money(spy['bot_start_eq'])} -> "
                 f"{_fmt_money(spy['spy_bh_equity'])}  ({_fmt_pct(spy['spy_return'])})")
        L.append(f"  delta vs SPY: {_fmt_pct(spy['delta_vs_spy'])}   "
                 f"(note: bot isn't 100% invested — rough benchmark)")
        if spy.get("funding_suspected"):
            L.append("  ⚠️  equity swing this large implies a deposit/withdrawal — "
                     "'bot return' is NOT pure trading P&L over this window")
    else:
        L.append(f"  unavailable — {spy.get('reason')}")
    L.append("")
    L.append("WARNINGS")
    if report["warnings"]:
        for w in report["warnings"]:
            L.append(f"  ⚠️  {w}")
    else:
        L.append(f"  (none — no feature has {MIN_TRADES_FOR_STATS}+ closed trades "
                 f"with negative P&L)")
    L.append("")
    L.append("DATA QUALITY")
    L.append(f"  log files parsed: {', '.join(dq.get('files_parsed') or []) or 'none'}")
    L.append(f"  parse errors: {len(dq.get('parse_errors') or [])}")
    for e in (dq.get("parse_errors") or [])[:5]:
        L.append(f"      - {e}")
    L.append(f"  estimated (bootstrapped) entries: {dq['estimated_entry_trips_closed']} closed, "
             f"{dq['estimated_entry_open']} open")
    L.append(f"  correction trips excluded from per-feature stats: "
             f"{dq.get('correction_trips_excluded', 0)} "
             f"(hand-placed repairs; not strategy decisions)")
    L.append(f"  pre-analyzer entries excluded (>{STALE_OPEN_DAYS}d): "
             f"{dq.get('stale_pre_analyzer_entries', 0)}")
    L.append(f"  exits missing an entry (orphans): {len(dq['orphan_exits_missing_entry'])}")
    for o in dq["orphan_exits_missing_entry"][:5]:
        L.append(f"      - {o['symbol']} {o['action']} @ {o['ts']}")
    L.append(f"  new events added to ledger this run: {dq.get('new_events_added', 0)}")
    return "\n".join(L) + "\n"


def _write_reports(report: dict) -> None:
    os.makedirs(os.path.dirname(REPORT_JSON), exist_ok=True)
    for path, payload in ((REPORT_JSON, json.dumps(report, indent=2) + "\n"),
                          (REPORT_TXT, render_txt(report))):
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            f.write(payload)
        os.replace(tmp, path)


# ── Orchestration ─────────────────────────────────────────────────────────────

def _load_stops() -> dict:
    try:
        with open(STOPS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(dry_run: bool = False) -> dict:
    ledger = _load_ledger()
    raw_records, files_parsed, parse_errors = _read_jsonl(TRADES_GLOB)
    new_events = _merge_events(ledger, raw_records)

    report = build_report(ledger, _load_stops(), {
        "files_parsed":     files_parsed,
        "parse_errors":     parse_errors,
        "new_events_added": new_events,
    })

    if not dry_run:
        _save_ledger(ledger)
        _write_reports(report)
        logger.info("Wrote %s and %s", REPORT_JSON, REPORT_TXT)
    dq = report["data_quality"]
    logger.info("Ledger: %d events (+%d new, +%d bootstrap), %d closed trips",
                len(ledger["events"]), new_events,
                dq["bootstrap_injected"], dq["closed_trips"])
    for w in report["warnings"]:
        logger.warning("PERF WARNING: %s", w)
    return report


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Weekly performance analyzer")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print the report without writing files")
    args = parser.parse_args()
    try:
        report = run(dry_run=args.dry_run)
    except Exception as exc:
        logger.error("Performance analysis failed: %s", exc)
        return 1
    if args.dry_run:
        print(render_txt(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
