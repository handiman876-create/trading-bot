#!/usr/bin/env bash
#
# Compact plain-text status report for pasting into a chat (~60 lines).
#
#   deploy/daily-report.sh               # today, America/New_York
#   deploy/daily-report.sh 2026-10-01    # any day still in the rotated logs (7)
#
# READ-ONLY and LOCAL-ONLY: logs, state files and ledgers on disk, plus
# `systemctl show`. It never calls the broker — every cold `python3 -c` re-auths
# against TradeStation and a few in minutes get throttled to 401, so a report
# that polled positions could itself break the bots' next token refresh.
#
# Time zones: trades.log stamps are ET; bot/perf/critical logs are UTC. The day
# is an ET calendar day, converted to its UTC window before any UTC log is read.
#
# Positions, stops and floors come from the LIVE state files, so for a past
# date they still show the current book (labelled as such).
#
# Never prints secrets: every output line passes through a redaction filter
# (webhook URLs, bearer/token/key assignments, long opaque strings), and any
# account id that is not a SIM id is masked to its last 3 chars.
#
set -uo pipefail

REPO="/root/trading-bot"
cd "$REPO" || exit 1

exec /usr/bin/python3 - "${1:-}" <<'PY'
import glob, gzip, json, os, re, subprocess, sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET, UTC = ZoneInfo("America/New_York"), timezone.utc
import config  # FUTURES_SPECS / gap / RSI thresholds; nothing printed from it

arg = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    day = date.fromisoformat(arg) if arg else datetime.now(ET).date()
except ValueError:
    sys.exit(f"usage: daily-report.sh [YYYY-MM-DD]   (got {arg!r})")
w0 = datetime(day.year, day.month, day.day, tzinfo=ET).astimezone(UTC)
w1 = w0 + timedelta(days=1)                       # DST-safe: ET midnight → next
is_today = day == datetime.now(ET).date()
when = "today" if is_today else f"on {day}"
out = []

# ── helpers ───────────────────────────────────────────────────────────────────
_SECRET = [
    (re.compile(r"https?://\S*discord(?:app)?\.com/api/webhooks/\S+"), "<webhook>"),
    (re.compile(r"(?i)\b(bearer)\s+\S+"), r"\1 <redacted>"),
    (re.compile(r"(?i)\b(\w*(?:token|secret|password|api_?key)\w*)\s*[=:]\s*\S+"), r"\1=<redacted>"),
    # Opaque credentials: 32+ chars mixing upper, lower and digits (Discord /
    # TradeStation tokens), or 32+ hex. Lowercase snake_case strategy names are
    # neither, so they survive.
    (re.compile(r"\b(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[a-z])(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{32,}\b"), "<redacted>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<redacted>"),
]
def redact(s):
    for rx, rep in _SECRET:
        s = rx.sub(rep, s)
    return s

def acct(a):
    a = str(a or "?")
    return a if a.startswith("SIM") else "..." + a[-3:]

def lines_of(pattern):
    """All lines of every file matching pattern, oldest rotation first."""
    files = sorted(glob.glob(pattern), key=lambda f: (-int(m.group(1)) if (m := re.search(r"\.(\d+)(?:\.gz)?$", f)) else 0))
    for f in files:
        op = gzip.open if f.endswith(".gz") else open
        try:
            with op(f, "rt", errors="replace") as fh:
                yield from fh
        except OSError:
            continue

def utc_ts(line):
    try:
        return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None

def in_window(line):
    t = utc_ts(line)
    return t is not None and w0 <= t < w1

def trade_rows(pattern):
    for l in lines_of(pattern):
        try:
            yield json.loads(l)
        except ValueError:
            continue

def mult(sym):
    for root, spec in config.FUTURES_SPECS.items():
        if re.fullmatch(root + r"[FGHJKMNQUVXZ]\d{2}", sym):
            return float(spec["multiplier"])
    if " " in sym or re.search(r"\d{6}[CP]\d", sym):
        return 100.0                               # option contract
    return 1.0                                     # equity share

def px(r):
    return float(r.get("fill_price") or r.get("price") or 0.0)

def money(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"

def ago(t):
    m = int((datetime.now(UTC) - t).total_seconds() // 60)
    return f"{m}m ago" if m < 120 else f"{m // 60}h ago"

# ── 1. services ───────────────────────────────────────────────────────────────
out.append(f"=== Trading status {day} (ET){'' if is_today else '  [historical day; positions = CURRENT book]'} ===")
def unit(name, *props):
    r = subprocess.run(["systemctl", "show", name, "-p", ",".join(props)], capture_output=True, text=True)
    return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
for name, short in (("trading-bot-equities", "equities"), ("trading-bot-futures", "futures")):
    u = unit(name, "ActiveState", "SubState", "ActiveEnterTimestamp", "NRestarts")
    out.append(f"{short:9s}: {u.get('ActiveState')}/{u.get('SubState')} since {u.get('ActiveEnterTimestamp','?').replace(' UTC','')} UTC, restarts={u.get('NRestarts','?')}")
# systemd keeps no exit timestamp for a manually started oneshot, so the time
# comes from the script's own log line.
mb = unit("memory-backup.service", "Result", "ExecMainStatus")
last_mb = [l.strip() for l in lines_of("logs/memory-backup.log") if "memory-backup:" in l and re.search(r"pushed|CRITICAL|nothing to commit", l)]
if last_mb:
    t = datetime.fromisoformat(last_mb[-1].split(" ", 1)[0]).astimezone(ET)
    out.append(f"mem-backup: result={mb.get('Result')} exit={mb.get('ExecMainStatus')} | {t:%m-%d %H:%M} ET {last_mb[-1].split('memory-backup: ', 1)[1][:58]}")
else:
    out.append(f"mem-backup: result={mb.get('Result')} | no run logged yet")

# ── 2. accounts (latest perf line at or before end of day) ────────────────────
out.append("--- Accounts")
for label, pat in (("equities", "logs/performance.log*"), ("futures", "logs/futures_performance.log*")):
    best = None
    for l in lines_of(pat):
        try:
            d = json.loads(l)
            t = datetime.strptime(d["timestamp"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
            rec = (t, acct(d.get("account_id")), d.get("total_equity"), d.get("cash"))
        except (ValueError, KeyError):
            m = re.search(r"Equity: ([\d.]+) \| Cash: ([\d.]+)", l)
            t = utc_ts(l)
            if not (m and t):
                continue
            rec = (t, "?", float(m.group(1)), float(m.group(2)))
        if rec[0] < w1 and (best is None or rec[0] >= best[0]):
            best = rec
    if best:
        out.append(f"{label:9s}: {best[1]}  equity ${best[2]:,.2f}  cash ${best[3]:,.2f}  (as of {best[0].astimezone(ET):%m-%d %H:%M} ET)")
    else:
        out.append(f"{label:9s}: no performance line on/before {day}")

# ── latest poll per symbol: price + EMAs (UTC bot logs, whole history) ────────
poll = {}
rx_eq = re.compile(r"strategy: (\S+) \| price=([\d.]+)\s+EMA9=([\d.]+)\s+EMA21=([\d.]+)\s+RSI=([\d.]+)\s+held=(-?\d+)")
rx_fu = re.compile(r"strategy: FUT \S+ \| signal=\S+ trade=(\S+)\s+close=([\d.]+)\s+EMA9=([\d.]+)\s+EMA21=([\d.]+)\s+RSI=([\d.]+)\s+held=(-?\d+)")
for pat in ("logs/bot.log*", "logs/futures_bot.log*"):
    for l in lines_of(pat):
        m = rx_eq.search(l) or rx_fu.search(l)
        if m and (t := utc_ts(l)) and t < w1:
            poll[m.group(1)] = dict(t=t, price=float(m.group(2)), e9=float(m.group(3)), e21=float(m.group(4)), rsi=float(m.group(5)), held=int(m.group(6)))

# ── 3/4. open positions + exit gap ────────────────────────────────────────────
out.append("--- Open positions (stop state files; last = latest bot poll)")
book = {}
for f in ("data/stop_prices.json", "data/stop_prices.futures.json"):
    try:
        book.update(json.load(open(f)))
    except (OSError, ValueError):
        pass
try:
    opts = json.load(open("data/options_positions.json"))
except (OSError, ValueError):
    opts = {}
gap_pct = config.EMA_CROSS_MIN_GAP_PCT
if not book and not opts:
    out.append("none")
for sym, s in sorted(book.items()):
    p, long_ = poll.get(sym), s.get("direction", "long") == "long"
    entry, mlt = float(s["entry_price"]), mult(sym)
    floors = []
    for k, nm in (("water_floor_active", "water"), ("profit_floor_active", "profit")):
        pr = s.get(k.replace("_active", "_price"))
        floors.append(f"{nm}:{'ARMED' + (f'@{pr:.2f}' if pr else '') if s.get(k) else 'off'}")
    bf = s.get("broker_floor_price")
    if p:
        qty = abs(p["held"])
        opnl = (p["price"] - entry) * qty * mlt * (1 if long_ else -1)
        out.append(f"{sym} {'L' if long_ else 'S'}{qty} entry {entry:.2f} last {p['price']:.2f} ({ago(p['t'])})  open {money(opnl)}")
    else:
        out.append(f"{sym} {'L' if long_ else 'S'}? entry {entry:.2f} last ?  (no bot poll line found)")
    out.append(f"   stop {s['stop_price']:.2f}  broker floor {f'{bf:.2f}' if bf else 'none'}  {' '.join(floors)}  opened {s.get('opened','?')}")
    if p:
        gap, need = p["e9"] - p["e21"], gap_pct * p["price"]
        rsi_ok = p["rsi"] > config.RSI_OVERSOLD if long_ else p["rsi"] < config.RSI_OVERBOUGHT
        # long exits on EMA9 <= EMA21 - need; short covers on EMA9 >= EMA21 + need
        to_go = (gap + need) if long_ else (need - gap)
        verdict = "EXIT SIGNAL LIVE" if to_go <= 0 and rsi_ok else (f"{to_go:.2f} pts to go" if to_go > 0 else "gap met, RSI gate holds")
        out.append(f"   EMA9-EMA21 {gap:+.2f} | exit needs {'<= -' if long_ else '>= +'}{need:.2f} ({gap_pct*100:.2f}% of {p['price']:.2f}) | {verdict}  RSI {p['rsi']:.1f}")
for k, o in sorted(opts.items()):
    out.append(f"{o.get('occ_symbol', k)} option entry {o.get('entry_price','?')} — premium exits only, no stop")

# ── 5. today's fills + realized P&L (FIFO over ledger events + trade logs) ────
out.append(f"--- Fills {when} (fill prices; realized = FIFO vs earlier entries)")
events = {}
for f in ("data/trade_ledger.json", "data/futures_trade_ledger.json"):
    try:
        events.update(json.load(open(f)).get("events", {}))
    except (OSError, ValueError, AttributeError):
        pass
for pat in ("logs/trades.log*", "logs/futures_trades.log*"):
    for r in trade_rows(pat):
        events[r.get("order_id") or f"{r['timestamp']}|{r['symbol']}|{r['action']}"] = r
def et_of(r):
    return datetime.strptime(r["timestamp"][:19], "%Y-%m-%d %H:%M:%S").date()
# Role comes from the ledger when it has one; trade-log rows carry none, so
# SELL / SELL_TO_CLOSE / BUY_TO_COVER are exits and everything else an entry
# (shorting is disabled in BOTH modes, so a bare BUY is never a cover). An exit
# only ever consumes lots — it never opens one — so an exit whose entry predates
# every source prints with no realized figure instead of inventing a short.
# Ledger "bootstrap|..." rows (adopted positions, estimated price, NO quantity)
# are skipped: they can't be sized, and the ledger's closed_trips owns them.
EXIT_ACTS = ("SELL", "SELL_TO_CLOSE", "BUY_TO_COVER")
lots, fills, realized_day = defaultdict(list), [], 0.0
for r in sorted(events.values(), key=lambda r: r["timestamp"][:19]):
    if r.get("quantity") in (None, ""):
        continue
    sym, act, qty, price = r["symbol"], r["action"], float(r["quantity"]), px(r)
    is_exit = r.get("role") == "exit" if r.get("role") else act in EXIT_ACTS
    short_side = act in ("SELL_SHORT", "BUY_TO_COVER") or r.get("direction") == "short"
    mlt, book_ = mult(sym), lots[(sym, short_side)]       # long and short lots never mix
    pnl, matched = 0.0, 0.0
    if is_exit:
        left = qty
        while left > 1e-9 and book_:
            lq, lp = book_[0]
            take = min(left, lq)
            pnl += (lp - price if short_side else price - lp) * take * mlt
            matched += take; left -= take
            book_[0] = (lq - take, lp)
            if book_[0][0] < 1e-9:
                book_.pop(0)
    else:
        book_.append((qty, price))
    if et_of(r) == day:
        fills.append((r, pnl if matched else None, matched))
        if matched:
            realized_day += pnl
if not fills:
    out.append("none")
for r, pnl, matched in fills:
    tag = "" if pnl is None else f"  realized {money(pnl)}" + ("" if matched == float(r["quantity"]) else f" on {matched:g}")
    out.append(f"{r['timestamp'][11:16]} {r['action']} {r['symbol']} x{float(r['quantity']):g} @ {px(r):.2f}  {(r.get('notes') or '')[:38]}{tag}")
if any(p is not None for _, p, _ in fills):
    out.append(f"realized {when}: {money(realized_day)}")

# ── 6. gap + sustain blocks; 7. CRITICAL lines (UTC logs, ET-day window) ──────
gap, sus = Counter(), Counter()
for pat in ("logs/bot.log*", "logs/futures_bot.log*"):
    for l in lines_of(pat):
        if "BLOCK" in l and in_window(l):
            if m := re.search(r"CROSS GAP BLOCK (\S+)", l): gap[m.group(1)] += 1
            elif m := re.search(r"SUSTAIN BLOCK (\S+)", l): sus[m.group(1)] += 1
# Gap blocks: the bot logs once per symbol-day, but a restart forgets that
# latch and logs again, so the honest unit is distinct symbols. Sustain blocks
# are one line per cross that died young, so lines ARE the unit there.
out.append(f"--- Blocks: gap {len(gap)} symbol(s){' (' + ', '.join(sorted(gap)) + ')' if gap else ''}"
           f" | sustain {sum(sus.values())}{' (' + ', '.join(f'{k}x{v}' if v > 1 else k for k, v in sorted(sus.items())) + ')' if sus else ''}")
crit = [l.strip() for pat in config.CRITICAL_ALERT_SINKS for l in lines_of(pat) if "[CRITICAL]" in l and in_window(l)]
out.append(f"--- CRITICAL {when}: {len(crit) or 'none'}")
for l in crit[-6:]:
    out.append("  " + l[11:19] + " UTC " + l.split("] ", 1)[-1][:110])
if len(crit) > 6:
    out.append(f"  (+{len(crit) - 6} earlier — see critical_alerts.log)")

# ── 8. last autodiscover on/before the day ────────────────────────────────────
summ = sorted(f for f in glob.glob("../strategy-discovery/logs/autodiscover_summary_*.json")
              if re.search(r"_(\d{8})\.json$", f).group(1) <= day.strftime("%Y%m%d"))
if summ:
    d = json.load(open(summ[-1]))
    cands = d.get("candidates", [])
    top = max(cands, key=lambda c: c.get("fast_score") or 0, default=None)
    real = [c for c in cands if (c.get("fast_trades") or 0) >= 50]   # n<50 ci_lower is a sentinel
    bci = max(real, key=lambda c: c.get("fast_ci_lower") or 0, default=None)
    out.append(f"--- Autodiscover {re.search(r'_(\d{8})', summ[-1]).group(1)}: spent ${d.get('spent_usd', 0):.2f}, hits {len(d.get('hits', []))}/{d.get('usable_candidates', len(cands))}")
    if top:
        out.append(f"  best score {top['fast_score']:.3f} {top['name']} (pf {top.get('fast_pf')}, ci_lo {top.get('fast_ci_lower')}, n={top.get('fast_trades')})")
    if bci and bci is not top:
        out.append(f"  best ci_lo (n>=50) {bci['fast_ci_lower']} {bci['name']} (pf {bci.get('fast_pf')}, n={bci.get('fast_trades')}); gate is ci_lo > 1.0")
else:
    out.append("--- Autodiscover: no summary on/before this day")

print("\n".join(redact(l) for l in out))
PY
