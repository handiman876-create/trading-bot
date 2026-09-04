"""One-off: entry-time distribution + entry price vs same-day EOD close.

Single process on purpose: the TS token endpoint throttles cold refreshes, so
every symbol must reuse one auth.
"""
import json
import time
from collections import Counter, defaultdict

import tradestation_client as ts

LEDGER = "data/trade_ledger.json"

trips = json.load(open(LEDGER))["closed_trips"]

# Options carry a premium basis, not a share price — excluded from price stats.
equity = [t for t in trips if t.get("feature") != "option"]

# ── 1. Entry-time distribution ───────────────────────────────────────────────
print("=" * 72)
print("ENTRY TIMES (ET) — all %d closed trips" % len(trips))
print("=" * 72)

by_hhmm = Counter()
real, estimated = [], []
for t in trips:
    ts_str = t.get("entry_ts")
    if not ts_str:
        continue
    (estimated if t.get("estimated_entry") else real).append(t)
    by_hhmm[ts_str.split()[1][:5]] += 1

print("\nExact clock times (real entries only):")
rc = Counter(t["entry_ts"].split()[1][:5] for t in real)
for hhmm in sorted(rc):
    print(f"  {hhmm}  {rc[hhmm]:2d}  " + "#" * rc[hhmm])

print(f"\nreal entries={len(real)}  estimated/adopted={len(estimated)}")
for t in estimated:
    print(f"  ADOPTED (synthetic ts): {t['symbol']} {t['entry_ts']}")

times = sorted(t["entry_ts"].split()[1] for t in real)
print(f"\nearliest={times[0]}   latest={times[-1]}")

for gate in ("12:00:00", "13:00:00", "14:00:00"):
    n = sum(1 for x in times if x >= gate)
    print(f"  entries at/after {gate}: {n} of {len(times)}")

# ── 2. Entry price vs same-day EOD close ─────────────────────────────────────
print()
print("=" * 72)
print("ENTRY PRICE vs SAME-DAY CLOSE")
print("=" * 72)

symbols = sorted({t["symbol"] for t in equity})
closes = defaultdict(dict)
for i, sym in enumerate(symbols):
    bars = ts.get_historical(sym, "daily", 120)
    if not bars:
        print(f"  !! no bars for {sym}")
    for b in bars:
        if b["date"] and b["close"]:
            closes[sym][b["date"][:10]] = b["close"]
    time.sleep(0.4)

print(f"\nfetched daily bars for {sum(1 for s in symbols if closes[s])}/{len(symbols)} symbols\n")

rows = []
for t in equity:
    if not t.get("entry_ts"):
        continue
    sym, day = t["symbol"], t["entry_ts"].split()[0]
    close = closes[sym].get(day)
    entry = t.get("entry_price")
    if close is None or not entry:
        rows.append((t, None, None))
        continue
    # Signed in the trade's favour: long wants close > entry, short wants below.
    sign = 1.0 if t["direction"] == "long" else -1.0
    drift = sign * (close - entry) / entry * 100.0
    rows.append((t, close, drift))

print(f"{'entry_ts':22s} {'sym':6s} {'dir':5s} {'entry':>9s} {'close':>9s} "
      f"{'drift%':>8s} {'held_pnl':>10s}")
for t, close, drift in sorted(rows, key=lambda r: r[0]["entry_ts"]):
    if close is None:
        print(f"{t['entry_ts'][:19]:22s} {t['symbol']:6s} {t['direction']:5s} "
              f"{t.get('entry_price'):>9} {'--':>9} {'--':>8}")
        continue
    print(f"{t['entry_ts'][:19]:22s} {t['symbol']:6s} {t['direction']:5s} "
          f"{t['entry_price']:>9.2f} {close:>9.2f} {drift:>+8.2f} "
          f"{t.get('pnl', 0):>+10.2f}")

ok = [(t, c, d) for t, c, d in rows if d is not None]


def summarize(label, subset):
    if not subset:
        print(f"\n{label}: n=0")
        return
    ds = [d for _, _, d in subset]
    favour = sum(1 for d in ds if d > 0)
    print(f"\n{label}: n={len(ds)}")
    print(f"  mean drift to close : {sum(ds)/len(ds):+.3f}%")
    print(f"  median              : {sorted(ds)[len(ds)//2]:+.3f}%")
    print(f"  closed in favour    : {favour}/{len(ds)} ({favour/len(ds)*100:.0f}%)")
    print(f"  min / max           : {min(ds):+.2f}% / {max(ds):+.2f}%")


summarize("ALL entries", ok)
summarize("LONGS", [r for r in ok if r[0]["direction"] == "long"])
summarize("SHORTS", [r for r in ok if r[0]["direction"] == "short"])
summarize("09:xx entries", [r for r in ok if r[0]["entry_ts"].split()[1][:2] == "09"])
summarize("10:xx entries", [r for r in ok if r[0]["entry_ts"].split()[1][:2] == "10"])
summarize("11:xx+ entries", [r for r in ok if r[0]["entry_ts"].split()[1][:2] >= "11"])
