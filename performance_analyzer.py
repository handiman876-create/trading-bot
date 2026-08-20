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
        if e.get("reconciled"):        # already settled against the broker
            continue
        if _parse_ts(e["timestamp"]) >= cutoff:
            recent.append(e)
        elif e.get("role") == "entry":
            stale_entries.append(e)
    return recent, stale_entries


# ── Broker reconciliation ─────────────────────────────────────────────────────

def _broker_positions() -> list[dict] | None:
    """Current broker positions, or None if we could not find out.

    None and [] are NOT interchangeable — see tradestation_client.get_positions.
    Every caller here must treat None as "do not touch the ledger"."""
    try:
        import tradestation_client as tc
        account_id = tc.get_account_id()
        if not account_id:
            logger.warning("RECONCILE: no account id — skipping reconciliation")
            return None
        return tc.get_positions(account_id)
    except Exception as exc:
        logger.warning("RECONCILE: positions fetch failed (%s) — skipping", exc)
        return None


# Ledger `direction` -> the sign the broker reports for that exposure. Options
# are opened with BUY_TO_OPEN, so they are long exposure like a long stock.
_DIRECTION_SIGN = {"long": 1, "option": 1, "short": -1}


def _broker_qty_by_key(positions: list[dict]) -> dict:
    """(symbol, direction) -> absolute quantity the broker actually holds.

    Broker rows carry a SIGNED quantity (negative = short). A symbol can only be
    long or short at once, so each row contributes to exactly one direction key —
    except options, which share the long sign and are keyed separately by the
    caller matching on the ledger's own direction."""
    out = {}
    for p in positions:
        symbol = p.get("symbol")
        qty = p.get("quantity") or 0
        if not symbol or qty == 0:
            continue
        direction = "long" if qty > 0 else "short"
        out[(symbol, direction)] = out.get((symbol, direction), 0) + abs(qty)
        if qty > 0:                    # an option row is long-signed; key it both ways
            out[(symbol, "option")] = out.get((symbol, "option"), 0) + abs(qty)
    return out


def _reconcile_open_entries(ledger: dict, open_entries: list, positions: list[dict] | None):
    """Settle ledger open entries against what the broker actually holds.

    WHY THIS EXISTS: the ledger tracked 10 open entries while the account held 4
    positions. Entries are PER-FILL and broker positions are PER-SYMBOL
    aggregates, so the two are not directly comparable: AAPL alone had three open
    entries totalling 9 shares against a real position of 6. A naive "symbol
    missing from broker -> close it" would have spared all three AAPL rows and
    still reported the wrong share count, so this compares AGGREGATE quantity per
    (symbol, direction) and trims the excess FIFO — oldest entries first, which
    matches how _pair_round_trips consumes them.

    Reconciled entries are marked, NOT closed: we do not know the exit price, so
    inventing a round-trip would fabricate P&L. They are excluded from open
    tracking and never become closed trips.

    Returns (kept_open, reconciled_records), where reconciled_records is None —
    NOT [] — when the fetch failed, so the report can say "skipped" instead of
    claiming a clean reconcile. On that path this is a no-op: an unreadable
    account must never be mistaken for a flat one. That conflation is exactly
    what caused the 2026-07-16 CRL/LII double-entry, and it is far worse here —
    a 503 would silently retire every genuinely open entry in the ledger."""
    if positions is None:
        logger.warning("RECONCILE: broker positions unavailable — ledger left untouched "
                       "(unknown != flat)")
        return list(open_entries), None

    broker = _broker_qty_by_key(positions)
    by_key = {}
    for e in open_entries:
        by_key.setdefault((e["symbol"], e["direction"]), []).append(e)

    kept, reconciled = [], []
    stamp = _now_ts()
    for key, entries in by_key.items():
        entries.sort(key=lambda e: _parse_ts(e["timestamp"]))    # FIFO: oldest first
        held = broker.get(key, 0)
        symbol, direction = key
        ledger_qty = sum(abs(e.get("quantity") or 0) for e in entries)
        excess = ledger_qty - held

        # Retire from the OLDEST end. An entry is only unpaired because its exit
        # was never logged, and _pair_round_trips pops the oldest open entry on an
        # exit — so the entry a missing exit would have consumed is the oldest one.
        # Trimming the newest instead would retire a position we still hold and
        # leave the already-closed one on the books.
        for entry in entries:
            qty = abs(entry.get("quantity") or 0)
            if excess <= 0:
                kept.append(entry)
                continue
            if qty > excess:
                # Partial overlap: this entry is bigger than the leftover excess,
                # so no whole entry explains the gap. Keep it — over-reporting an
                # open position is recoverable, retiring a held one is not.
                logger.warning(
                    "RECONCILE: %s %s has a %d-share gap that no whole entry "
                    "explains (next entry is %d) — leaving it open for review",
                    symbol, direction, excess, qty)
                excess = 0
                kept.append(entry)
                continue
            reason = ("not in broker positions" if held == 0 else
                      f"quantity exceeds broker position "
                      f"({held} held, ledger had {ledger_qty})")
            entry["reconciled"] = {
                "at":         stamp,
                "reason":     reason,
                "broker_qty": held,
            }
            reconciled.append({
                "symbol":    symbol,
                "direction": direction,
                "qty":       entry.get("quantity"),
                "entry_ts":  entry.get("timestamp"),
                "reason":    reason,
            })
            logger.info("RECONCILE: closed stale ledger entry for %s (%s x%s @ %s) — %s",
                        symbol, direction, entry.get("quantity"),
                        entry.get("timestamp"), reason)
            excess -= qty

    # Persist the marks: `entry` objects are the same dicts held in ledger["events"],
    # so mutating them above already updated the ledger in place. Assert that
    # invariant rather than trusting it — a copy here would silently re-reconcile
    # the same entries on every run.
    marked = sum(1 for e in ledger.get("events", {}).values() if e.get("reconciled"))
    if reconciled and marked == 0:
        logger.error("RECONCILE: marks did not reach the ledger — entries were copies")
    return kept, reconciled


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
        "signal_price":    raw.get("signal_price"),
        "fill_price":      raw.get("fill_price"),
        "slippage":        raw.get("slippage"),
        "role":            role,
        "direction":       direction,
        "feature":         feature,
        "estimated_entry": False,
        # Stop attribution (exits written on/after 2026-08-13). ABSENT on older
        # records, and absent must stay distinguishable from False — an exit that
        # predates the ladder is not evidence the ladder was inactive, it is no
        # evidence at all. Hence None, never a `or False` default.
        "profit_floor_active": raw.get("profit_floor_active"),
        "profit_floor_price":  raw.get("profit_floor_price"),
        "atr_trail_at_exit":   raw.get("atr_trail_at_exit"),
        "floor_caused_exit":   raw.get("floor_caused_exit"),
        # Breakeven-lock attribution (exits on/after 2026-08-19). Same
        # absent-is-unknown contract: before that date the label was
        # unreachable, so a missing key is not "the lock was inactive".
        "breakeven_lock_held": raw.get("breakeven_lock_held"),
        "lock_caused_exit":    raw.get("lock_caused_exit"),
        "stop_at_exit":        raw.get("stop_at_exit"),
        "water_at_exit":       raw.get("water_at_exit"),
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

      correction     — a hand-placed repair of a bug's damage; the strategy never
                       signalled it. Checked FIRST: a correction is a correction
                       regardless of what else the note says.
      stop           — the trailing stop fired (ATR trail / breakeven lock /
                       profit-floor rung; the note names which).
      option_stop    — an option's -50% PREMIUM stop.
      option_target  — an option's +50% premium target.
      option_expiry  — an option force-closed on days-to-expiry.
      signal         — the strategy's own exit logic.

    WHY THE OPTION BUCKETS EXIST: all three premium rules used to land in
    "signal", because the note was built from the UNDERLYING's rationale and read
    "{symbol} reversal" no matter which rule fired. That made them mutually
    indistinguishable in the ledger, and filed the premium STOP as a non-stop.

    They are deliberately NOT folded into "stop". Options are not stop-managed —
    they carry no profit_floor_active / atr_trail_at_exit — so putting them in
    "stop" would add rows that every consumer of that bucket has to filter back
    out. Separate buckets also let each premium rule be judged on its own, which
    is the point: one has fired once, one twice, one never.
    """
    n = (notes or "").lower()
    if config.CORRECTION_NOTE_MARKER.lower() in n:
        return "correction"
    if "trailing stop" in n:
        return "stop"
    # Keyed off the "option <reason>" prefix that strategy._close_option writes.
    # Order matches _option_exit_reason's own worst-news-first precedence.
    if "option stop loss" in n:
        return "option_stop"
    if "option profit target" in n:
        return "option_target"
    if "option near expiry" in n:
        return "option_expiry"
    return "signal"


def _leg_price(event: dict):
    """(price, source) for one leg — the REAL fill when we have it, else the
    signal-bar close.

    WHY THIS MATTERS: pricing a round-trip at the signal close is not a rounding
    detail. A broker audit on 2026-07-27 re-priced all 25 closed trips at their
    actual fills and moved total realized P&L from -$36,296.78 to -$39,229.12 —
    the ledger understated the loss by $2,932.34 (8.1%). 54 of 55 matched orders
    filled at a price different from the one logged, and the drift is
    DIRECTIONALLY BIASED rather than random: entries fill worse and exits fill
    worse, so it accumulates against the account. One LII buy slipped $8.10.

    Falls back to the signal price rather than dropping the trip, because most
    historical events predate fill capture (5f26dcd, 2026-07-20) — a trip priced
    at signal is worth reporting with a caveat, not discarding."""
    fill = event.get("fill_price")
    if fill is not None:
        return fill, "fill"
    return event.get("price"), "signal"


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
            entry_price, entry_src = _leg_price(entry)
            exit_price,  exit_src  = _leg_price(ev)
            # A trip counts as fill-priced only when BOTH legs are real fills;
            # one real leg against one signal leg is a mixed basis and is
            # reported separately rather than being rounded up to "at fill".
            if entry_src == exit_src:
                basis = entry_src
            else:
                basis = "mixed"
            pnl = _pnl(direction, entry_price, exit_price, qty)
            closed.append({
                "symbol":          ev["symbol"],
                "direction":       direction,
                "feature":         entry.get("feature"),
                "qty":             qty,
                "entry_order_id":  entry.get("order_id"),
                "entry_ts":        entry.get("timestamp"),
                "entry_price":     entry_price,
                "entry_price_src": entry_src,
                "estimated_entry": entry.get("estimated_entry", False),
                "exit_order_id":   ev.get("order_id"),
                "exit_ts":         ev.get("timestamp"),
                "exit_price":      exit_price,
                "exit_price_src":  exit_src,
                "price_basis":     basis,
                "exit_reason":     _exit_reason(ev.get("notes")),
                "profit_floor_active": ev.get("profit_floor_active"),
                "profit_floor_price":  ev.get("profit_floor_price"),
                "atr_trail_at_exit":   ev.get("atr_trail_at_exit"),
                "floor_caused_exit":   ev.get("floor_caused_exit"),
                "breakeven_lock_held": ev.get("breakeven_lock_held"),
                "lock_caused_exit":    ev.get("lock_caused_exit"),
                "stop_at_exit":        ev.get("stop_at_exit"),
                "water_at_exit":       ev.get("water_at_exit"),
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


# ── Open-position mark-to-market ──────────────────────────────────────────────

def _mark_open_entries(open_entries: list) -> dict:
    """Mark open entries to the latest quote for an unrealized-P&L estimate.

    ESTIMATE, not a booking: it uses last/close (the analyzer runs Sunday
    00:07 ET, so this is Friday's close), ignores commissions, and prices the
    signal-time entry rather than the broker fill on older records.

    Partial data is reported as partial. If a quote is missing, that entry is
    listed under `unpriced` and excluded from the total rather than silently
    contributing $0 — a quiet zero would understate a loss and make the Total
    line read better than reality."""
    if not open_entries:
        return {"pnl": 0.0, "priced": 0, "unpriced": [], "positions": []}

    try:
        import tradestation_client as tc
    except Exception as exc:
        logger.warning("Open mark-to-market unavailable (%s)", exc)
        return {"pnl": None, "priced": 0,
                "unpriced": [e["symbol"] for e in open_entries], "positions": []}

    quotes, total, priced, unpriced, rows = {}, 0.0, 0, [], []
    for e in open_entries:
        symbol = e["symbol"]
        if symbol not in quotes:
            quotes[symbol] = tc.get_quote(symbol)
        q = quotes[symbol]
        price = (q or {}).get("last") or (q or {}).get("close")
        if price is None:
            unpriced.append(symbol)
            continue
        pnl = _pnl(e["direction"], e.get("price"), price, e.get("quantity"))
        total += pnl
        priced += 1
        rows.append({
            "symbol":    symbol,
            "direction": e["direction"],
            "qty":       e.get("quantity"),
            "entry":     e.get("price"),
            "mark":      price,
            "pnl":       round(pnl, 2),
        })
    return {"pnl": round(total, 2) if priced else None,
            "priced": priced, "unpriced": unpriced, "positions": rows}


def _disabled_features(closed_trips: list) -> dict:
    """Feature bucket -> retirement note + ALL-IN historical stats, for buckets
    whose flag is currently OFF.

    Reads the LIVE flag rather than a hardcoded list, so a feature re-enabled in
    config stops being marked disabled without anyone remembering to edit this.
    A note with no matching flag is ignored (the flag is the source of truth); a
    flag that is off with no note still gets marked, just without the detail.

    The stats here are ALL-IN — correction trips included — unlike _aggregate,
    which excludes them so a hand-placed repair can't distort a live feature's
    win rate. For a retired bucket the question is different: "what did this
    cost us in total", which is also the number the disabling commit quotes
    (40a34a3: 1-for-11, -$23,735.16). Both figures are rendered so neither can
    be mistaken for the other."""
    out = {}
    for feature, flag_name in getattr(config, "FEATURE_FLAGS", {}).items():
        if getattr(config, flag_name, True):
            continue
        note = dict(getattr(config, "FEATURE_DISABLED_NOTES", {}).get(feature, {}))
        trips = [t for t in closed_trips if t["feature"] == feature]
        corrections = [t for t in trips if t.get("exit_reason") == "correction"]
        note.update({
            "trips":            len(trips),
            "wins":             sum(1 for t in trips if t["win"]),
            "total_pnl":        round(sum(t["pnl"] for t in trips), 2),
            "correction_trips": len(corrections),
        })
        out[feature] = note
    return out


def _totals(closed_trips: list, open_mark: dict, spy: dict) -> dict:
    """The headline block: realized + estimated-open + combined, vs SPY.

    Realized is EVERY closed trip, correction trips included — this is the
    account-level number, unlike the per-feature block below it, which excludes
    hand-placed repairs because they are not strategy decisions. The two
    therefore differ on purpose, and the rendered block says so."""
    realized = sum(t["pnl"] for t in closed_trips)
    corrections = [t for t in closed_trips if t.get("exit_reason") == "correction"]
    open_pnl = open_mark.get("pnl")
    return {
        "realized":            round(realized, 2),
        "realized_strategy":   round(realized - sum(t["pnl"] for t in corrections), 2),
        "correction_pnl":      round(sum(t["pnl"] for t in corrections), 2),
        "correction_trips":    len(corrections),
        "open_estimate":       open_pnl,
        "total":               None if open_pnl is None else round(realized + open_pnl, 2),
        "vs_spy":              (spy or {}).get("delta_vs_spy"),
    }


# ── Report assembly + rendering ───────────────────────────────────────────────

def _now_ts() -> str:
    import pytz
    return datetime.now(pytz.timezone(config.MARKET_TZ)).strftime("%Y-%m-%d %H:%M:%S %Z")


def build_report(ledger: dict, stops: dict, data_quality: dict,
                 positions: list[dict] | None = None) -> dict:
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

    # Settle what is left open against the broker BEFORE the open-side numbers are
    # reported, so "open entries" means "positions we actually hold". Closed trips
    # are untouched — reconciliation only ever retires unpaired entries.
    open_entries, reconciled = _reconcile_open_entries(ledger, open_entries, positions)

    ledger["closed_trips"] = closed        # recomputed view, not appended

    data_quality = dict(data_quality)
    data_quality["bootstrap_injected"] = injected
    data_quality["reconciled_entries"] = reconciled
    data_quality["stale_pre_analyzer_entries"] = len(stale_entries)
    agg = _aggregate(closed)
    est_closed = sum(1 for t in closed if t["estimated_entry"])
    est_open   = sum(1 for e in open_entries if e.get("estimated_entry"))
    spy = _spy_comparison()
    open_mark = _mark_open_entries(open_entries)

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
        "priced_at_fill":               sum(1 for t in closed if t.get("price_basis") == "fill"),
        "priced_at_signal":             sum(1 for t in closed if t.get("price_basis") == "signal"),
        "priced_mixed":                 sum(1 for t in closed if t.get("price_basis") == "mixed"),
    })

    return {
        "generated":    _now_ts(),
        "scope":        "MVP: realized closed round-trips, priced at real broker "
                        "fills where known (signal-bar close otherwise — see Data "
                        "Quality), attributed to entry feature",
        "ledger_span":  [events and min(events, key=lambda e: _parse_ts(e["timestamp"]))["timestamp"][:10],
                         events and max(events, key=lambda e: _parse_ts(e["timestamp"]))["timestamp"][:10]]
                        if events else [None, None],
        "per_feature":  agg,
        "disabled":     _disabled_features(closed),
        "totals":       _totals(closed, open_mark, spy),
        "open_mark":    open_mark,
        "spy":          spy,
        "warnings":     _build_warnings(agg),
        "profit_floor": _profit_floor_stats(closed),
        "breakeven_lock": _breakeven_lock_stats(closed),
        "data_quality": data_quality,
    }


def _fmt_money(v) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "n/a"


def _fmt_pct(v) -> str:
    return f"{v*100:+.2f}%" if isinstance(v, (int, float)) else "n/a"


def _fmt_iv(v) -> str:
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "n/a"


MIN_FLOOR_TRIPS_FOR_VERDICT = 3


def _profit_floor_stats(closed_trips: list) -> dict:
    """Is the ladder earning its keep, or cutting winners short?

    Reads only stop-reason trips. Three populations, kept strictly separate:

      unknown  — profit_floor_active is None: the exit predates the ladder
                 (< 2026-08-13) or came from a non-stop path. NOT evidence the
                 floor was inactive; excluded from every denominator.
      active   — the ladder was holding the stop when it fired.
      caused   — active AND the raw ATR trail would NOT have fired at that price,
                 i.e. the trade would still be open without the ladder.

    HONEST LIMIT: the true dollar impact of a floor-caused exit is what price did
    AFTERWARDS, and nothing here knows that — the analyzer reads the ledger, not
    forward bars. So this reports (a) realized P&L on floor-caused exits and (b)
    'room given up': how far the trail sat beyond the floor at exit, which bounds
    how much earlier the ladder fired. It does NOT claim a counterfactual P&L.
    Summing realized P&L and calling it "floor impact" would be wrong in the
    common case — a floor-caused exit on a WINNER still books a profit, which
    reads as the ladder helping even if price then doubled.
    """
    stops = [t for t in closed_trips if t.get("exit_reason") == "stop"]
    known = [t for t in stops if t.get("profit_floor_active") is not None]
    active = [t for t in known if t.get("profit_floor_active")]
    caused = [t for t in active if t.get("floor_caused_exit")]
    realized = round(sum(t.get("pnl") or 0 for t in caused), 2)
    wins = sum(1 for t in caused if t.get("win"))
    room = 0.0
    for t in caused:
        fl, tr, qty = (t.get("profit_floor_price"),
                       t.get("atr_trail_at_exit"), t.get("qty") or 0)
        if fl is not None and tr is not None:
            room += abs(fl - tr) * qty

    # The verdict refuses to judge on a thin sample. A ladder that has fired
    # twice tells you nothing, and a confident HELPING/HURTING at n=2 is exactly
    # how a safety net gets pulled for the wrong reason.
    if len(caused) < MIN_FLOOR_TRIPS_FOR_VERDICT:
        verdict = "INSUFFICIENT DATA"
    elif realized > 0 and wins / len(caused) >= 0.5:
        verdict = "HELPING"
    elif realized < 0:
        verdict = "HURTING"
    else:
        verdict = "NEUTRAL"
    return {
        "stop_exits":       len(stops),
        "attributed":       len(known),
        "unattributed":     len(stops) - len(known),
        "floor_active":     len(active),
        "floor_caused":     len(caused),
        "trail_would_fire": len(active) - len(caused),
        "realized_on_caused": realized,
        "winners_on_caused":  wins,
        "room_given_up":    round(room, 2),
        "verdict":          verdict,
    }


def _profit_floor_lines(st: dict | None) -> list[str]:
    """Render _profit_floor_stats. Separate from the computation so the numbers
    land in the JSON report too, rather than existing only as formatted text."""
    L = ["=== PROFIT FLOOR ANALYSIS ==="]
    st = st or {}
    if not st.get("attributed"):
        n = st.get("unattributed", 0)
        L.append(f"  no attributed stop exits yet"
                 + (f" ({n} stop exit(s) predate the ladder, not counted)"
                    if n else ""))
        L.append("  the ladder only leaves a trace when a floor-held stop FIRES; "
                 "rungs that stay inert never appear here")
        return L

    L.append(f"  attributed stop exits:     {st['attributed']}"
             + (f"   ({st['unattributed']} older/unattributed, excluded)"
                if st["unattributed"] else ""))
    L.append(f"  trades with floor active:  {st['floor_active']} of {st['attributed']}")
    L.append(f"  floor caused exit:         {st['floor_caused']} of {st['floor_active']}")
    L.append(f"  trail would have fired:    {st['trail_would_fire']} of {st['floor_active']}")
    if st["floor_caused"]:
        L.append(f"  realized on floor-caused:  "
                 f"{_fmt_money(st['realized_on_caused'])} "
                 f"({st['winners_on_caused']}/{st['floor_caused']} winners)")
        L.append(f"  room given up vs trail:    "
                 f"{_fmt_money(st['room_given_up'])} "
                 f"(how much further the trail sat; NOT a realized loss)")
    if st["verdict"] == "INSUFFICIENT DATA":
        L.append(f"  verdict: INSUFFICIENT DATA — {st['floor_caused']} "
                 f"floor-caused exit(s), need "
                 f"{MIN_FLOOR_TRIPS_FOR_VERDICT}+ before this means anything")
    else:
        detail = {
            "HELPING": "floor-caused exits are net profitable",
            "HURTING": ("floor-caused exits are net negative; the ladder is "
                        "firing on trades the trail would have held"),
            "NEUTRAL": "no clear signal either way",
        }[st["verdict"]]
        L.append(f"  verdict: {st['verdict']} — {detail}")
        L.append("  (still not a counterfactual — confirming this needs "
                 "post-exit price paths the ledger does not carry)")
    return L


MIN_LOCK_TRIPS_FOR_VERDICT = 3

# What counts as "booked ~$0", as a fraction of position notional rather than an
# absolute dollar band. QQQ 2026-08-18 is why: -$7.92 across 66 shares is -$0.12
# a share — unambiguously a scratch — but any fixed dollar threshold small enough
# to be meaningful on a 10-share position calls it a real loss. Sized to cover
# stop slippage and commission without swallowing a genuine one.
SCRATCH_BAND_PCT = 0.002


def _breakeven_lock_stats(closed_trips: list) -> dict:
    """Is the breakeven lock earning its keep, or scratching out live trades?

    Same three-population shape as _profit_floor_stats, and the same refusal to
    count an unattributed exit as evidence:

      unknown  — breakeven_lock_held is None: the exit predates the attribution
                 fix (< 2026-08-19) or came from a non-stop path. EVERY lock
                 exit before that date is unknown, because the label was
                 unreachable (see strategy._stop_source). Do not read the old
                 ledger's zero lock exits as "the lock never fired".
      held     — the entry floor, not the trail, was holding the stop.
      caused   — held AND the raw ATR trail would NOT have fired at that price.

    THE VERDICT DOES NOT KEY ON REALIZED P&L. A lock exit fires at entry, so it
    books ~$0 by construction and `realized > 0` is unreachable — judging it the
    ladder's way marks every success as neutral-at-best. The real trade-off is:

      protected  = |entry - trail| * qty ... the loss avoided, had the trail fired
      given_back = |water - entry| * qty ... the peak excursion surrendered

    A lock that repeatedly scratches positions out of 1+ ATR of profit is HURTING
    even though it never books a loss, and that only shows up in given_back.

    HONEST LIMITS. `protected` assumes the trail would eventually have fired;
    price could equally have recovered past entry, in which case the lock cost
    the position rather than saving it. `given_back` is peak-to-entry, not
    peak-to-what-price-did-next. The analyzer reads the ledger, not forward bars,
    and cannot separate those. water_at_exit did not exist before 2026-08-19, so
    pre-fix trips are EXCLUDED from given_back rather than counted as zero —
    zero would read as "gave nothing back", the most favourable possible reading
    of a trip we know nothing about.
    """
    stops = [t for t in closed_trips if t.get("exit_reason") == "stop"]
    known = [t for t in stops if t.get("breakeven_lock_held") is not None]
    held = [t for t in known if t.get("breakeven_lock_held")]
    caused = [t for t in held if t.get("lock_caused_exit")]
    realized = round(sum(t.get("pnl") or 0 for t in caused), 2)
    scratches = sum(1 for t in caused
                    if abs(t.get("pnl") or 0)
                    < SCRATCH_BAND_PCT * abs((t.get("entry_price") or 0)
                                             * (t.get("qty") or 0)))

    protected = given_back = 0.0
    measurable = 0
    for t in caused:
        en, tr, qty = (t.get("entry_price"), t.get("atr_trail_at_exit"),
                       t.get("qty") or 0)
        if en is not None and tr is not None:
            protected += abs(en - tr) * qty
        wa = t.get("water_at_exit")
        if en is not None and wa is not None:
            given_back += abs(wa - en) * qty
            measurable += 1

    if len(caused) < MIN_LOCK_TRIPS_FOR_VERDICT:
        verdict = "INSUFFICIENT DATA"
    elif not measurable:
        verdict = "INSUFFICIENT DATA"
    elif protected >= given_back:
        verdict = "HELPING"
    else:
        verdict = "HURTING"
    return {
        "stop_exits":       len(stops),
        "attributed":       len(known),
        "unattributed":     len(stops) - len(known),
        "lock_held":        len(held),
        "lock_caused":      len(caused),
        "trail_would_fire": len(held) - len(caused),
        "realized_on_caused":  realized,
        "scratches_on_caused": scratches,
        "principal_protected": round(protected, 2),
        "peak_given_back":     round(given_back, 2),
        "given_back_measured": measurable,
        "given_back_excluded": len(caused) - measurable,
        "verdict":          verdict,
    }


def _breakeven_lock_lines(st: dict | None) -> list[str]:
    """Render _breakeven_lock_stats. Split from the computation for the same
    reason as the profit floor: the numbers belong in the JSON report too."""
    L = ["=== BREAKEVEN LOCK ANALYSIS ==="]
    st = st or {}
    if not st.get("attributed"):
        n = st.get("unattributed", 0)
        L.append("  no attributed stop exits yet"
                 + (f" ({n} stop exit(s) predate the attribution fix, "
                    "not counted)" if n else ""))
        L.append("  before 2026-08-19 the 'breakeven lock' label was "
                 "unreachable, so an absence here is not evidence the lock "
                 "never held a stop")
        return L

    L.append(f"  attributed stop exits:     {st['attributed']}"
             + (f"   ({st['unattributed']} older/unattributed, excluded)"
                if st["unattributed"] else ""))
    L.append(f"  trades with lock holding:  {st['lock_held']} of {st['attributed']}")
    L.append(f"  lock caused exit:          {st['lock_caused']} of {st['lock_held']}")
    L.append(f"  trail would have fired:    {st['trail_would_fire']} of {st['lock_held']}")
    if st["lock_caused"]:
        L.append(f"  realized on lock-caused:   "
                 f"{_fmt_money(st['realized_on_caused'])} "
                 f"({st['scratches_on_caused']}/{st['lock_caused']} scratches "
                 f"within {SCRATCH_BAND_PCT:.1%} of notional — near-zero is "
                 f"the DESIGN, not a result)")
        L.append(f"  principal protected:       "
                 f"{_fmt_money(st['principal_protected'])} "
                 f"(what the raw trail sat below entry)")
        L.append(f"  peak given back:           "
                 f"{_fmt_money(st['peak_given_back'])} "
                 f"(excursion surrendered; {st['given_back_measured']} of "
                 f"{st['lock_caused']} measurable"
                 + (f", {st['given_back_excluded']} pre-2026-08-19 with no "
                    "water_at_exit" if st["given_back_excluded"] else "")
                 + ")")
        L.append("  neither figure is a counterfactual — both assume price did "
                 "what it did; the ledger carries no post-exit path")
    if st["verdict"] == "INSUFFICIENT DATA":
        if st["lock_caused"] >= MIN_LOCK_TRIPS_FOR_VERDICT:
            L.append("  verdict: INSUFFICIENT DATA — no lock-caused exit carries "
                     "water_at_exit, so peak given back cannot be measured")
        else:
            L.append(f"  verdict: INSUFFICIENT DATA — {st['lock_caused']} "
                     f"lock-caused exit(s), need "
                     f"{MIN_LOCK_TRIPS_FOR_VERDICT}+ before this means anything")
    else:
        detail = {
            "HELPING": ("protected more principal than it surrendered in peak "
                        "— the floor is converting round-trips into scratches"),
            "HURTING": ("surrendered more peak excursion than it protected in "
                        "principal — the lock is scratching out of positions "
                        "that were meaningfully in profit"),
        }[st["verdict"]]
        L.append(f"  verdict: {st['verdict']} — {detail}")
    return L


def _ab_screen_lines(tracking: dict | None = None) -> list[str]:
    """Render the A/B SCREEN TRACKER section from SCREEN_AB_TRACKING_FILE.

    Read-only: summarizes screen_ab_tracker.py's output (which is produced by a
    separate timer). `tracking` can be passed in for tests; otherwise it's loaded
    from disk, and a missing/empty file degrades to a one-line 'no data yet'
    note rather than an error. No recommendation is shown before
    SCREEN_AB_MIN_ROTATIONS completed rotations."""
    if tracking is None:
        try:
            with open(os.path.join(_HERE, config.SCREEN_AB_TRACKING_FILE)) as f:
                tracking = json.load(f)
        except (OSError, json.JSONDecodeError):
            tracking = None

    L = ["=== A/B Screen Comparison ==="]
    rotations = (tracking or {}).get("rotations") or []
    completed = [r for r in rotations if r.get("two_week_results")]
    min_rot = config.SCREEN_AB_MIN_ROTATIONS
    L.append(f"Rotations completed: {len(completed)} (min {min_rot} needed)")
    if not rotations:
        L.append("  (no A/B rotations recorded yet — screen_ab_tracker.py has not run)")
        return L

    def _summarize(screen_key: str, label: str) -> tuple[list[str], float | None]:
        lines = [f"{label} picks so far:"]
        all_rets, all_ivs = [], []
        for r in completed:
            res = r["two_week_results"]
            rets = res[f"{screen_key}_returns"]
            picks = [f"{s} {_fmt_pct(v)}" for s, v in rets.items() if s != "avg"]
            lines.append(f"  {r['rotation_date']}: "
                         + (", ".join(picks) if picks else "(no picks)")
                         + f"   [avg {_fmt_pct(rets.get('avg'))}]")
            all_rets += [v for s, v in rets.items() if s != "avg" and isinstance(v, (int, float))]
            all_ivs += [d.get("iv") for d in r[screen_key].get("detail", [])
                        if isinstance(d.get("iv"), (int, float))]
        avg_ret = round(sum(all_rets) / len(all_rets), 4) if all_rets else None
        avg_iv = round(sum(all_ivs) / len(all_ivs), 1) if all_ivs else None
        lines.append(f"  Avg 2-week return: {_fmt_pct(avg_ret)}")
        lines.append(f"  Avg IV: {_fmt_iv(avg_iv)}")
        return lines, avg_ret

    a_lines, a_avg = _summarize("screen_a", "Screen A (current)")
    b_lines, b_avg = _summarize("screen_b", "Screen B (profitable filter)")
    L += a_lines + b_lines

    tally = (tracking or {}).get("winner_tally") or {}
    a_wins, b_wins, ties = tally.get("screen_a", 0), tally.get("screen_b", 0), tally.get("tie", 0)
    if a_wins > b_wins:
        leader = "Screen A"
    elif b_wins > a_wins:
        leader = "Screen B"
    else:
        leader = "TIE"
    L.append(f"Current leader: {leader}  (A:{a_wins} B:{b_wins} tie:{ties})")

    if len(completed) >= min_rot:
        if a_avg is not None and b_avg is not None and b_avg > a_avg and b_wins > a_wins:
            rec = "adopt B (Screen B leads on both avg return and rotations won)"
        elif a_avg is not None and b_avg is not None and a_avg >= b_avg:
            rec = "keep A (profitability filter did not improve returns)"
        else:
            rec = "wait (signal is mixed — leader and avg-return disagree)"
        L.append(f"Recommendation: {rec}")
    else:
        L.append(f"Recommendation: wait — need {min_rot - len(completed)} more rotation(s) "
                 f"before drawing a conclusion")
    return L


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

    t = report.get("totals") or {}
    mark = report.get("open_mark") or {}
    L.append("=== TOTAL P&L ===")
    L.append(f"  Realized:     {_fmt_money(t.get('realized'))}   "
             f"({dq['closed_trips']} closed trips, all-in)")
    if t.get("correction_trips"):
        L.append(f"                {_fmt_money(t.get('realized_strategy'))} strategy-only "
                 f"— excludes {t['correction_trips']} correction trips "
                 f"({_fmt_money(t.get('correction_pnl'))}); this is what the "
                 f"per-feature totals below sum to")
    if t.get("open_estimate") is None:
        L.append(f"  Open (est.):  n/a — no quotes for "
                 f"{', '.join(mark.get('unpriced') or ['open positions'])}")
    else:
        note = ""
        if mark.get("unpriced"):
            note = f"  [PARTIAL — no quote for {', '.join(mark['unpriced'])}]"
        L.append(f"  Open (est.):  {_fmt_money(t['open_estimate'])}   "
                 f"({mark.get('priced', 0)} of {dq['open_entries']} entries "
                 f"marked to last close){note}")
    L.append(f"  Total:        {_fmt_money(t.get('total'))}")
    L.append(f"  vs SPY:       {_fmt_pct(t.get('vs_spy'))}")
    L.append("  (open leg is an ESTIMATE — last/close, no commissions, signal-time "
             "entry prices; nothing here is booked until the position closes)")
    L.append("")

    L.append("PER-FEATURE (realized, closed round-trips)")
    disabled = report.get("disabled") or {}
    for feat in FEATURES:
        a = report["per_feature"][feat]
        label = FEATURE_LABELS[feat]
        if feat in disabled:
            note = disabled[feat]
            L.append(f"  {label.upper()}: DISABLED")
            L.append(f"      ({note['wins']}-for-{note['trips']}, "
                     f"{_fmt_money(note.get('total_pnl'))} historical)")
            if note.get("correction_trips"):
                L.append(f"      (of which {note['correction_trips']} correction trips; "
                         f"strategy-only: {a['count']} trips, "
                         f"{_fmt_money(a.get('total_pnl'))})")
            since = note.get("since")
            commit = note.get("commit")
            if since or commit:
                L.append(f"      (disabled {since or 'date unknown'}"
                         + (f", commit {commit}" if commit else "") + ")")
            if note.get("reason"):
                L.append(f"      ({note['reason']})")
            continue
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
    n_trips = dq["closed_trips"]
    at_fill, at_signal = dq.get("priced_at_fill", 0), dq.get("priced_at_signal", 0)
    mixed = dq.get("priced_mixed", 0)
    L.append(f"  trips priced at fill:   {at_fill}/{n_trips}")
    L.append(f"  trips priced at signal: {at_signal}/{n_trips}"
             + ("  ⚠️  signal-priced P&L is understated — real fills are worse "
                "on both legs" if at_signal else ""))
    if mixed:
        L.append(f"  trips priced mixed:     {mixed}/{n_trips} "
                 f"(one leg filled, one leg signal-only)")
    L.append(f"  correction trips excluded from per-feature stats: "
             f"{dq.get('correction_trips_excluded', 0)} "
             f"(hand-placed repairs; not strategy decisions)")
    L.append(f"  pre-analyzer entries excluded (>{STALE_OPEN_DAYS}d): "
             f"{dq.get('stale_pre_analyzer_entries', 0)}")
    L.append(f"  exits missing an entry (orphans): {len(dq['orphan_exits_missing_entry'])}")
    for o in dq["orphan_exits_missing_entry"][:5]:
        L.append(f"      - {o['symbol']} {o['action']} @ {o['ts']}")
    rec = dq.get("reconciled_entries")
    if rec is None:
        L.append("  broker reconcile: SKIPPED — positions unavailable; open count "
                 "is ledger-only and may overstate what is held")
    else:
        L.append(f"  stale open entries reconciled against broker this run: {len(rec)} "
                 f"(retired, not closed — no exit price is known, so no P&L is booked)")
        for r in rec[:5]:
            L.append(f"      - {r['symbol']} {r['direction']} x{r['qty']} "
                     f"@ {r['entry_ts']} — {r['reason']}")
    L.append(f"  new events added to ledger this run: {dq.get('new_events_added', 0)}")
    L.append("")
    L.extend(_profit_floor_lines(report.get("profit_floor")))
    L.append("")
    L.extend(_breakeven_lock_lines(report.get("breakeven_lock")))
    L.append("")
    L.append("A/B SCREEN TRACKER")
    L.extend(_ab_screen_lines())
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


def run(dry_run: bool = False, reconcile: bool = True) -> dict:
    ledger = _load_ledger()
    raw_records, files_parsed, parse_errors = _read_jsonl(TRADES_GLOB)
    new_events = _merge_events(ledger, raw_records)

    # A --dry-run still fetches positions: the reconcile result is part of what
    # the operator is previewing. Nothing is persisted unless dry_run is False.
    positions = _broker_positions() if reconcile else None

    report = build_report(ledger, _load_stops(), {
        "files_parsed":     files_parsed,
        "parse_errors":     parse_errors,
        "new_events_added": new_events,
    }, positions=positions)

    if not dry_run:
        _save_ledger(ledger)
        _write_reports(report)
        logger.info("Wrote %s and %s", REPORT_JSON, REPORT_TXT)
    dq = report["data_quality"]
    reconciled = dq.get("reconciled_entries")
    logger.info("Ledger: %d events (+%d new, +%d bootstrap), %d closed trips, "
                "%d open, reconciled %s",
                len(ledger["events"]), new_events,
                dq["bootstrap_injected"], dq["closed_trips"], dq["open_entries"],
                "SKIPPED" if reconciled is None else len(reconciled))
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
    parser.add_argument("--no-reconcile", action="store_true",
                        help="skip the broker position reconcile (offline runs); "
                             "the open-side count is then ledger-only")
    args = parser.parse_args()
    try:
        report = run(dry_run=args.dry_run, reconcile=not args.no_reconcile)
    except Exception as exc:
        logger.error("Performance analysis failed: %s", exc)
        return 1
    if args.dry_run:
        print(render_txt(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
