"""
Futures performance analyzer — the futures twin of performance_analyzer.py.

WHY A SEPARATE MODULE AND LEDGER. Until 2026-09-23 no futures trip reached any
ledger. The weekly analyzer runs in equities mode, where config.TRADE_LOG_FILE
resolves to logs/trades.log, so logs/futures_trades.log* was never read, and
every futures P&L figure was worked out by hand (the 08-24 "+$6,502.50 reported
vs -$2,482.50 true" table, NQZ26's +$11,035). The two books also have to stay
separable: the equity ledger's history means "equities + options", and merging
futures into it would silently change the meaning of every total quoted from it.

WHAT IS SHARED vs WHAT IS NOT. Parsing, dedup, stop attribution, FIFO pairing and
the broker reconcile all come from performance_analyzer, never re-typed here —
the stop-attribution keys drifted once already when a copy went stale. What is
futures-specific:

  * CONTRACT MULTIPLIER — config.FUTURES_SPECS[root]["multiplier"] (ES $50,
    NQ $20, RTY $50). An unknown root RAISES; there is no 1x fallback, because
    1x is the silent wrong answer (NQZ26 would book +$551.75, not +$11,035).
  * BROKER HISTORY RECOVERY — the futures account's order history (90-day
    broker cap) fills in exit prices the log never captured (RTYU26 07-21,
    fill_price null — it predates the fill-recording fix) and recovers orders
    with no log record at all. Everything it touches is marked; nothing it
    recovers can be mistaken for a logged event.
  * NO PARTITION BY AGE — the equity analyzer drops >90d entries as
    pre-analyzer noise. Futures history is complete from the first contract, so
    there is no such noise and nothing is aged out.
  * NO SPY BENCHMARK, NO OPTIONS, NO PROFIT-TAKING — none apply to futures.

THE SELL AMBIGUITY. Futures TradeAction is BUY/SELL only, so a SELL can OPEN a
short as well as close a long. The shared classifier reads SELL as a long exit,
which is correct today because shorting is disabled in every mode. A SELL with
no open long therefore surfaces as an orphan exit, and the report says why.
Re-enabling futures shorting requires fixing this first.

Run:
  python3 futures_performance_analyzer.py              # update ledger + JSON report
  python3 futures_performance_analyzer.py --dry-run    # compute + print, write nothing
  python3 futures_performance_analyzer.py --extra-trades-glob '/path/futures_trades.log*'
      # fold in an archived copy of rotated-out logs; dedup makes overlap safe

The Sunday run needs none of this: performance_analyzer.run() calls run() here
and appends the === FUTURES PERFORMANCE === section to the weekly report.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

import config
import performance_analyzer as pa

logger = logging.getLogger("futures_performance_analyzer")

_HERE = pa._HERE
LEDGER_PATH = os.path.join(_HERE, config.FUTURES_TRADE_LEDGER_FILE)
# The futures bot writes config.STOP_PRICE_FILE under --mode futures, which is
# this same name in the same STATE_DIR; spelled out because this module runs in
# equities mode, where STOP_PRICE_FILE is the equity file.
STOPS_PATH  = os.path.join(_HERE, config.STATE_DIR, "stop_prices.futures.json")
TRADES_GLOB = os.path.join(_HERE, config.FUTURES_TRADE_LOG_FILE + "*")
REPORT_JSON = os.path.join(_HERE, config.LOG_DIR, "futures_performance_report.json")

FEATURE = "futures"
# The broker caps order history at 90 days; ask for one day less so a UTC/ET
# boundary can never push the request past the cap.
BROKER_HISTORY_DAYS = 89
# A broker order with no order_id match is still the SAME trade as a log row of
# the same symbol/action/qty this close in time (a log row written with a null
# order_id). Market orders fill in seconds; two minutes is generous and still
# far shorter than the 30-minute cross-sustain gap between any two real trades.
BROKER_MATCH_WINDOW_S = 120


class UnknownFuturesRoot(ValueError):
    """A futures symbol whose root has no FUTURES_SPECS multiplier.

    Raised, never defaulted: pricing an unknown contract at 1x produces a P&L
    that is wrong by 20-50x and looks perfectly plausible."""


# ── Contract arithmetic ───────────────────────────────────────────────────────

def futures_root(symbol: str) -> str | None:
    """'NQZ26' -> 'NQ', 'RTYU26' -> 'RTY'; None for anything not a futures
    contract. Delegates to the client's regex — the same one that sets ticks —
    so ESTC/ESS/NQIV can never read as ES/NQ by prefix."""
    import tradestation_client as tc
    return tc._futures_root(symbol)


def point_value(symbol: str) -> float:
    """Dollars per 1.0 point per contract, from config.FUTURES_SPECS."""
    root = futures_root(symbol)
    spec = config.FUTURES_SPECS.get(root) if root else None
    if not spec or not spec.get("multiplier"):
        raise UnknownFuturesRoot(
            f"{symbol!r}: root {root!r} has no FUTURES_SPECS multiplier — refusing "
            f"to price it at 1x")
    return float(spec["multiplier"])


def _is_tracked_future(symbol: str) -> bool:
    root = futures_root(symbol)
    return bool(root) and root in config.FUTURES_SPECS


# ── Ledger ingest ─────────────────────────────────────────────────────────────

def _tag_features(ledger: dict) -> None:
    """Every futures entry is attributed to the one 'futures' bucket. The shared
    classifier would file an ES long under long_fresh_cross — an equity bucket —
    which is meaningless in this ledger and wrong if the two are ever compared."""
    for ev in ledger.get("events", {}).values():
        if ev.get("role") == "entry":
            ev["feature"] = FEATURE


def _ingest_logs(ledger: dict, globs: list[str]) -> dict:
    files, errors, added, backfilled = [], [], 0, 0
    for i, g in enumerate(globs):
        raw, f, e = pa._read_jsonl(g)
        if i:        # an extra glob: say where it came from, or the list reads twice
            f = [f"{os.path.basename(os.path.dirname(g))}/{x}" for x in f]
        a, b = pa._merge_events(ledger, raw)
        files += f
        errors += e
        added += a
        backfilled += b
    _tag_features(ledger)
    return {"files_parsed": files, "parse_errors": errors,
            "new_events_added": added, "events_backfilled": backfilled}


def _broker_ts_to_et(iso: str) -> str | None:
    """Broker OpenedDateTime ('2026-07-21T15:31:18Z') -> the ledger's
    'YYYY-MM-DD HH:MM:SS EDT' so recovered events sort among logged ones."""
    import pytz
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(pytz.timezone(config.MARKET_TZ)).strftime("%Y-%m-%d %H:%M:%S %Z")


def _find_unkeyed_match(events: dict, symbol: str, action: str, qty, ts: str):
    """A logged event that is this broker order under a different key — same
    symbol/action/qty within BROKER_MATCH_WINDOW_S. Guards against booking one
    trade twice when its log row carried no order_id."""
    t0 = pa._parse_ts(ts)
    for ev in events.values():
        if (ev.get("symbol") == symbol and ev.get("action") == action
                and abs(ev.get("quantity") or 0) == abs(qty or 0)
                and abs((pa._parse_ts(ev.get("timestamp")) - t0).total_seconds())
                <= BROKER_MATCH_WINDOW_S):
            return ev
    return None


def _merge_broker_history(ledger: dict, rows: list[dict] | None) -> dict:
    """Fold the futures account's order history into the ledger.

    Two repairs, both marked, never silent:

      fill recovery   a logged event with fill_price null gets the broker's
                      execution price and `fill_broker_recovered: True`.
      event recovery  a filled order with no log record at all becomes a
                      ledger event with `broker_recovered: True`. It carries no
                      stop attribution (the broker does not know why we sold)
                      and its trip is reported with exit_reason
                      "broker_recovered", not dressed up as a signal exit.

    A logged fill that DISAGREES with the broker by more than one tick is
    counted, not overwritten — the log is the record of what the bot saw, and a
    disagreement is worth a human look rather than a quiet correction.

    `rows is None` means the fetch failed: nothing is touched, and the report
    says SKIPPED rather than claiming a clean recovery.
    """
    stats = {"available": rows is not None, "rows": 0, "futures_fills": 0,
             "fills_recovered": [], "events_recovered": [],
             "matched_without_id": 0, "fill_mismatches": [], "unparseable": 0}
    if rows is None:
        return stats
    events = ledger.setdefault("events", {})
    seen = set()
    for r in rows:
        stats["rows"] += 1
        symbol, oid = r.get("symbol"), r.get("order_id")
        qty, price = r.get("quantity") or 0, r.get("price")
        action = (r.get("action") or "").upper()
        if not symbol or not _is_tracked_future(symbol):
            continue
        # Cancelled / replaced GTC broker floors and rejects carry no execution.
        if qty <= 0 or not price or action not in ("BUY", "SELL"):
            continue
        if not oid or oid in seen:
            continue
        seen.add(oid)
        stats["futures_fills"] += 1

        stored = events.get(oid)
        if stored is not None:
            logged = stored.get("fill_price")
            if logged is None:
                stored["fill_price"] = price
                stored["fill_broker_recovered"] = True
                stats["fills_recovered"].append(
                    {"symbol": symbol, "order_id": oid, "action": action,
                     "ts": stored.get("timestamp"), "fill": price})
            else:
                tick = config.FUTURES_SPECS[futures_root(symbol)]["tick"]
                if abs(logged - price) > tick + 1e-9:
                    stats["fill_mismatches"].append(
                        {"symbol": symbol, "order_id": oid,
                         "logged": logged, "broker": price})
            continue

        ts = _broker_ts_to_et(r.get("opened"))
        if ts is None:
            stats["unparseable"] += 1
            continue
        if _find_unkeyed_match(events, symbol, action, qty, ts) is not None:
            stats["matched_without_id"] += 1
            continue
        ev = pa._normalize({
            "timestamp": ts, "action": action, "symbol": symbol,
            "quantity": int(qty), "price": price, "order_type": "broker",
            "order_id": oid, "signal_price": None, "fill_price": price,
            "slippage": None,
            "notes": "broker-recovered: filled order with no trade-log record",
        })
        if ev is None:
            continue
        ev["broker_recovered"] = True
        events[oid] = ev
        stats["events_recovered"].append(
            {"symbol": symbol, "order_id": oid, "action": action, "ts": ts,
             "fill": price})
    _tag_features(ledger)
    return stats


# ── Broker access (each returns None on failure: unknown != empty) ────────────

def _futures_account_id():
    try:
        import tradestation_client as tc
        return tc.get_futures_account_id()
    except Exception as exc:
        logger.warning("FUTURES: account lookup failed (%s)", exc)
        return None


def _broker_positions(account_id) -> list[dict] | None:
    if not account_id:
        return None
    try:
        import tradestation_client as tc
        return tc.get_positions(account_id)
    except Exception as exc:
        logger.warning("FUTURES RECONCILE: positions fetch failed (%s)", exc)
        return None


def _broker_history(account_id) -> list[dict] | None:
    """Historical orders PLUS today's. historicalorders excludes the current
    day, so reading it alone missed the NQZ26 exit on the day it happened.
    Either read failing makes the whole answer None: half a history would
    report the other half's fills as never having happened."""
    if not account_id:
        return None
    since = (datetime.now() - timedelta(days=BROKER_HISTORY_DAYS)).strftime("%Y-%m-%d")
    try:
        import tradestation_client as tc
        hist = tc.get_historical_orders(account_id, since)
        today = tc.get_current_orders(account_id)
    except Exception as exc:
        logger.warning("FUTURES: order history fetch failed (%s)", exc)
        return None
    if hist is None or today is None:
        return None
    return hist + today


# ── Trip enrichment ───────────────────────────────────────────────────────────

def _floor_label(t: dict) -> str | None:
    """Which floor caused a stop exit, if any. Checked in the same order the
    labels are assigned at exit time; only one is ever true for a trip."""
    if t.get("water_caused_exit"):
        return "water floor"
    if t.get("lock_caused_exit"):
        return "breakeven lock"
    if t.get("floor_caused_exit"):
        return "profit floor"
    return None


def _enrich(trip: dict, events_by_oid: dict) -> dict:
    """Add points, root, floor label and — where a floor fired — capture ratio.

    capture = realized / peak, peak = |water - entry| × qty × point_value: the
    share of the best excursion actually banked, ON THE FILL. It is the K knob
    (see pa._water_floor_stats), and computing it off the signal price would
    credit K with slippage it did not cause (NQZ26: 67.1% signal vs 56.5% fill).
    """
    sign = -1 if trip["direction"] == "short" else 1
    t = dict(trip)
    t["root"] = futures_root(t["symbol"])
    t["points"] = round((t["exit_price"] - t["entry_price"]) * sign, 4)
    entry_ev = events_by_oid.get(t.get("entry_order_id")) or {}
    exit_ev = events_by_oid.get(t.get("exit_order_id")) or {}
    t["broker_recovered"] = bool(entry_ev.get("broker_recovered")
                                 or exit_ev.get("broker_recovered"))
    t["fill_broker_recovered"] = bool(entry_ev.get("fill_broker_recovered")
                                      or exit_ev.get("fill_broker_recovered"))
    if exit_ev.get("broker_recovered"):
        t["exit_reason"] = "broker_recovered"     # we do not know why it sold
    t["floor"] = _floor_label(t)
    t["capture"] = None
    wa, en = t.get("water_at_exit"), t.get("entry_price")
    if t["floor"] and wa is not None and en is not None:
        peak = abs(wa - en) * pa._dollar_qty(t)
        if peak > 0:
            t["capture"] = round(t["pnl"] / peak, 4)
    return t


def _mark_open(open_entries: list) -> dict:
    """Open entries marked to the latest quote, at the contract multiplier and
    the ENTRY FILL where known. Same honesty rule as the equity mark: an
    unquoted entry is listed as unpriced, never counted as $0."""
    if not open_entries:
        return {"pnl": 0.0, "priced": 0, "unpriced": [], "positions": []}
    try:
        import tradestation_client as tc
    except Exception as exc:
        logger.warning("FUTURES: open mark unavailable (%s)", exc)
        return {"pnl": None, "priced": 0,
                "unpriced": [e["symbol"] for e in open_entries], "positions": []}
    total, priced, unpriced, rows = 0.0, 0, [], []
    for e in open_entries:
        q = tc.get_quote(e["symbol"])
        mark = (q or {}).get("last") or (q or {}).get("close")
        entry = e.get("fill_price") if e.get("fill_price") is not None else e.get("price")
        if mark is None or entry is None:
            unpriced.append(e["symbol"])
            continue
        pv = point_value(e["symbol"])
        pnl = pa._pnl(e["direction"], entry, mark, e.get("quantity"), point_value=pv)
        total += pnl
        priced += 1
        rows.append({"symbol": e["symbol"], "direction": e["direction"],
                     "qty": e.get("quantity"), "entry": entry, "mark": mark,
                     "opened": (e.get("timestamp") or "")[:10],
                     "pnl": round(pnl, 2)})
    return {"pnl": round(total, 2) if priced else None, "priced": priced,
            "unpriced": unpriced, "positions": rows}


# ── Report ────────────────────────────────────────────────────────────────────

def build_report(ledger: dict, stops: dict, data_quality: dict,
                 positions: list[dict] | None = None,
                 broker: dict | None = None, mark: bool = True) -> dict:
    events = [e for e in ledger["events"].values() if not e.get("reconciled")]
    _c0, _o0, open0 = pa._pair_round_trips(events, point_value)
    open_keys = {(e["symbol"], e["direction"]) for e in open0}
    injected = pa._inject_bootstrap_entries(ledger, stops, open_keys)
    if injected:
        _tag_features(ledger)
        events = [e for e in ledger["events"].values() if not e.get("reconciled")]
    closed, orphans, open_entries = pa._pair_round_trips(events, point_value)
    open_entries, reconciled = pa._reconcile_open_entries(ledger, open_entries, positions)

    ledger["closed_trips"] = closed            # recomputed view, never appended
    by_oid = {e.get("order_id"): e for e in ledger["events"].values() if e.get("order_id")}
    trips = [_enrich(t, by_oid) for t in closed]

    realized = round(sum(t["pnl"] for t in trips), 2)
    by_root = {}
    for t in trips:
        r = by_root.setdefault(t["root"], {"trips": 0, "wins": 0, "pnl": 0.0})
        r["trips"] += 1
        r["wins"] += 1 if t["win"] else 0
        r["pnl"] = round(r["pnl"] + t["pnl"], 2)
    open_mark = _mark_open(open_entries) if mark else {
        "pnl": None, "priced": 0, "unpriced": [e["symbol"] for e in open_entries],
        "positions": []}

    broker = broker or {"available": False}
    dq = dict(data_quality)
    dq.update({
        "bootstrap_injected":         injected,
        "reconciled_entries":         reconciled,
        "closed_trips":               len(trips),
        "open_entries":               len(open_entries),
        "orphan_exits_missing_entry": [
            {"symbol": o["symbol"], "action": o["action"], "ts": o["timestamp"]}
            for o in orphans],
        "priced_at_fill":   sum(1 for t in trips if t.get("price_basis") == "fill"),
        "priced_at_signal": sum(1 for t in trips if t.get("price_basis") == "signal"),
        "priced_mixed":     sum(1 for t in trips if t.get("price_basis") == "mixed"),
        "broker_history":   broker,
    })

    warnings = []
    if not broker.get("available"):
        warnings.append("broker order history unavailable — missing fills were "
                        "NOT recovered this run")
    if broker.get("fill_mismatches"):
        warnings.append(f"{len(broker['fill_mismatches'])} logged fill(s) disagree "
                        f"with the broker by more than a tick — review before "
                        f"quoting these trips")
    if orphans:
        warnings.append(f"{len(orphans)} futures exit(s) with no open entry — a "
                        f"futures SELL can also OPEN a short (TradeAction is "
                        f"BUY/SELL only); shorting is disabled, so each needs review")
    if reconciled is None:
        warnings.append("futures positions unavailable — open entries NOT "
                        "reconciled against the broker")

    return {
        "generated":    pa._now_ts(),
        "scope":        "futures round trips at contract multiplier, priced at "
                        "real fills where known; separate ledger from equities",
        "ledger_span":  ([min(events, key=lambda e: pa._parse_ts(e["timestamp"]))["timestamp"][:10],
                          max(events, key=lambda e: pa._parse_ts(e["timestamp"]))["timestamp"][:10]]
                         if events else [None, None]),
        "trips":        trips,
        "by_root":      by_root,
        "totals": {
            "realized":      realized,
            "trips":         len(trips),
            "wins":          sum(1 for t in trips if t["win"]),
            "open_estimate": open_mark.get("pnl"),
            "total":         (None if open_mark.get("pnl") is None
                              else round(realized + open_mark["pnl"], 2)),
        },
        "open_mark":      open_mark,
        "water_floor":    pa._water_floor_stats(closed),
        "breakeven_lock": pa._breakeven_lock_stats(closed),
        "warnings":       warnings,
        "data_quality":   dq,
    }


def _money(v) -> str:
    return pa._fmt_money(v)


def _sub_section(lines: list[str], title: str) -> list[str]:
    """Re-head a shared equity renderer's block so it cannot be mistaken for the
    equity section of the same name further up the report."""
    return [f"  --- {title} ---"] + [f"  {l}" for l in lines[1:]]


def render_lines(report: dict | None) -> list[str]:
    """The === FUTURES PERFORMANCE === section of the weekly report."""
    L = ["=== FUTURES PERFORMANCE ==="]
    if not report:
        L.append("  not run")
        return L
    if report.get("error"):
        L.append(f"  ⚠️  FAILED — {report['error']}")
        L.append("  futures P&L is NOT in this report; the equity sections above "
                 "are unaffected")
        return L

    t = report["totals"]
    dq = report["data_quality"]
    span = report["ledger_span"]
    L.append(f"  separate ledger ({os.path.basename(LEDGER_PATH)}) — NOT included "
             f"in the equity totals above")
    L.append(f"  ledger span: {span[0]} .. {span[1]}   |   closed trips: "
             f"{t['trips']} ({t['wins']} wins)   |   open: {dq['open_entries']}")
    L.append(f"  Realized:     {_money(t['realized'])}   (gross, before commissions)")
    om = report["open_mark"]
    if t["open_estimate"] is None:
        L.append(f"  Open (est.):  n/a — no quote for "
                 f"{', '.join(om.get('unpriced') or ['open positions'])}")
    else:
        L.append(f"  Open (est.):  {_money(t['open_estimate'])}   "
                 f"({om['priced']} of {dq['open_entries']} marked to last)")
    L.append(f"  Total:        {_money(t['total'])}")
    for root, r in sorted(report["by_root"].items()):
        L.append(f"    {root:4} {r['trips']} trip(s), {r['wins']} win(s), "
                 f"{_money(r['pnl'])}")
    L.append("")
    L.append("  ROUND TRIPS (entry fill -> exit fill, x contract multiplier)")
    for tr in report["trips"]:
        tags = []
        if tr.get("price_basis") != "fill":
            tags.append(f"priced {tr.get('price_basis')}")
        if tr.get("fill_broker_recovered"):
            tags.append("fill broker-recovered")
        if tr.get("broker_recovered"):
            tags.append("broker-recovered")
        why = tr["exit_reason"]
        if tr["floor"]:
            why += f" ({tr['floor']}"
            why += (f", capture {tr['capture']:.1%}" if tr["capture"] is not None
                    else ", capture n/a") + ")"
        L.append(f"    {tr['symbol']:7} {tr['direction']} x{tr['qty']}  "
                 f"{(tr['entry_ts'] or '')[:10]} -> {(tr['exit_ts'] or '')[:10]}  "
                 f"{tr['entry_price']:.2f} -> {tr['exit_price']:.2f}  "
                 f"{tr['points']:+.2f} pts x ${tr['point_value']:.0f}  "
                 f"{_money(tr['pnl'])}  {why}"
                 + (f"  [{'; '.join(tags)}]" if tags else ""))
    for p in om.get("positions") or []:
        L.append(f"    {p['symbol']:7} {p['direction']} x{p['qty']}  "
                 f"{p['opened']} -> OPEN        {p['entry']:.2f} -> {p['mark']:.2f} "
                 f"(mark)  {_money(p['pnl'])} est.")
    L.append("")
    L.extend(_sub_section(pa._water_floor_lines(report.get("water_floor")),
                          "water floor (futures trips only)"))
    L.append("")
    L.extend(_sub_section(pa._breakeven_lock_lines(report.get("breakeven_lock")),
                          "breakeven lock (futures trips only)"))
    L.append("")
    L.append("  --- futures data quality ---")
    L.append(f"  log files parsed: {', '.join(dq.get('files_parsed') or []) or 'none'}")
    L.append(f"  parse errors: {len(dq.get('parse_errors') or [])}")
    L.append(f"  trips priced at fill: {dq['priced_at_fill']}/{t['trips']}   "
             f"signal: {dq['priced_at_signal']}   mixed: {dq['priced_mixed']}")
    b = dq.get("broker_history") or {}
    if not b.get("available"):
        L.append("  broker order history: SKIPPED — unavailable this run")
    else:
        L.append(f"  broker order history: {b['futures_fills']} futures fill(s) in "
                 f"the last {BROKER_HISTORY_DAYS}d; {len(b['fills_recovered'])} missing "
                 f"fill(s) recovered, {len(b['events_recovered'])} unlogged order(s) "
                 f"recovered, {len(b['fill_mismatches'])} mismatch(es)")
        for f in b["fills_recovered"][:5]:
            L.append(f"      - fill recovered: {f['symbol']} {f['action']} @ {f['ts']} "
                     f"-> {f['fill']}")
        for f in b["events_recovered"][:5]:
            L.append(f"      - order recovered: {f['symbol']} {f['action']} @ {f['ts']} "
                     f"-> {f['fill']}")
        for f in b["fill_mismatches"][:5]:
            L.append(f"      - MISMATCH {f['symbol']} order {f['order_id']}: log "
                     f"{f['logged']} vs broker {f['broker']}")
    rec = dq.get("reconciled_entries")
    L.append("  broker reconcile: SKIPPED — futures positions unavailable"
             if rec is None else
             f"  open entries reconciled against the futures account: {len(rec)}")
    L.append(f"  exits missing an entry (orphans): "
             f"{len(dq['orphan_exits_missing_entry'])}")
    for o in dq["orphan_exits_missing_entry"][:5]:
        L.append(f"      - {o['symbol']} {o['action']} @ {o['ts']}")
    for w in report.get("warnings") or []:
        L.append(f"  ⚠️  {w}")
    return L


# ── Orchestration ─────────────────────────────────────────────────────────────

def _load_stops() -> dict:
    try:
        with open(STOPS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(dry_run: bool = False, reconcile: bool = True, broker_history: bool = True,
        extra_globs: list[str] | None = None) -> dict:
    ledger = pa._load_ledger(LEDGER_PATH)
    dq = _ingest_logs(ledger, [TRADES_GLOB] + list(extra_globs or []))

    account_id = _futures_account_id() if (reconcile or broker_history) else None
    broker = _merge_broker_history(
        ledger, _broker_history(account_id) if broker_history else None)
    positions = _broker_positions(account_id) if reconcile else None

    # --no-reconcile is the offline switch, as in the equity analyzer: it also
    # skips the open mark, which is the only other broker read.
    report = build_report(ledger, _load_stops(), dq, positions=positions,
                          broker=broker, mark=reconcile)
    if not dry_run:
        pa._save_ledger(ledger, LEDGER_PATH)
        os.makedirs(os.path.dirname(REPORT_JSON), exist_ok=True)
        tmp = f"{REPORT_JSON}.tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(report, indent=2) + "\n")
        os.replace(tmp, REPORT_JSON)
    t = report["totals"]
    logger.info("Futures ledger: %d events (+%d new), %d closed trips, realized %s, "
                "%d open; broker history %s",
                len(ledger["events"]), dq["new_events_added"], t["trips"],
                pa._fmt_money(t["realized"]), report["data_quality"]["open_entries"],
                "SKIPPED" if not broker.get("available") else
                f"{len(broker['fills_recovered'])} fill(s) + "
                f"{len(broker['events_recovered'])} order(s) recovered")
    for w in report["warnings"]:
        logger.warning("FUTURES PERF WARNING: %s", w)
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Futures performance analyzer")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print the section without writing files")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="skip the futures-account position reconcile and the "
                             "open mark (offline runs)")
    parser.add_argument("--no-broker-history", action="store_true",
                        help="skip the order-history fill/order recovery")
    parser.add_argument("--extra-trades-glob", action="append", default=[],
                        help="also read these trade logs (e.g. an archive of "
                             "rotated-out files); repeatable")
    args = parser.parse_args()
    try:
        report = run(dry_run=args.dry_run, reconcile=not args.no_reconcile,
                     broker_history=not args.no_broker_history,
                     extra_globs=args.extra_trades_glob)
    except Exception as exc:
        logger.error("Futures performance analysis failed: %s", exc)
        return 1
    if args.dry_run:
        print("\n".join(render_lines(report)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
