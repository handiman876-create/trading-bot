# Trading Bot Runbook

Operational procedures. For planned work see `backlog.md`.

## Daily Health Check

### CRITICAL Alert Check

```bash
# Correct pattern (bracket-escaped — matches the LEVEL, not the word):
grep "\[CRITICAL\]" ~/trading-bot/logs/bot.log

# Check archives too (bot.log holds ONE day):
zgrep "\[CRITICAL\]" ~/trading-bot/logs/bot.log.*.gz
grep "\[CRITICAL\]" ~/trading-bot/logs/bot.log.1 2>/dev/null
```

**Wrong pattern — DO NOT USE:**

```bash
grep "CRITICAL" bot.log        # ← matches the startup BANNER
```

The banner is an `[INFO]` line that *describes* the CRITICAL feature
("Exit alerts : CRITICAL on rejection ..."). All 8 lifetime hits of the
unbracketed pattern are banner text; zero are real events. So the loose pattern
gives a false POSITIVE on a clean bot, and — because you learn to ignore it —
a false negative when it matters.

Alternation needs `-E`. `grep "a|b"` matches nothing and looks like a clean
result; always `grep -E "a|b"`.

### Rotation and the weekend gap

Rotation is `logrotate.timer` → `OnCalendar=daily`, system TZ is `Etc/UTC`, so
**00:00 UTC**. `maxsize 10M` can also rotate mid-session. `copytruncate` is used
because both the Python `FileHandler` and systemd's `append:` hold the fd open.

A Saturday CRITICAL rotates out of `bot.log` before a Monday check, so
**on Mondays you must also check `bot.log.1`.**

Expect *missing* weekend files, and do not read that as lost data: the bot writes
its "Sleeping until Monday" line on Friday and nothing after, so `bot.log` is
empty at Sat/Sun midnight and `notifempty` skips rotation. Verified coverage as of
2026-08-17:

| file | dates |
|---|---|
| `bot.log` | 08-17 |
| `bot.log.1` | 08-14 |
| `.2.gz` … `.7.gz` | 08-13, 12, 11, 10, 07, 06 |

08-15/16 and 08-08/09 are absent — both weekends. A file-per-day sweep silently
skips them.

### Interpretation

* **Empty result = genuinely clean.**
* **Any `[CRITICAL]` line = act immediately:**
  * `EXIT ORDER REJECTED` → the broker refused the exit; the position may still
    be open.
  * `BROKER FLOOR stuck` → a floor cancel would not confirm; the position may be
    open AND unprotected.

Both retry next cycle, and **a repeating one never self-clears.** Counters:
`EXIT ORDER REJECTED`, `BROKER FLOOR stuck` — both 0 lifetime as of 2026-08-17.

**Nothing pages you.** These go to `bot.log` only; there is no alert channel yet.
This manual check is currently the entire detection mechanism. Wiring
`[CRITICAL]` to journald (persistent, rotation- and reboot-proof) plus an
outbound channel is open work — see `backlog.md`.

### Stop File Check

```bash
python3 -m json.tool < ~/trading-bot/data/stop_prices.json
```

Read the numbers off this file, never from recall or from a previous message.
Cross-check `stop_price` against the last `STOP TRAIL` line for the same symbol;
they must agree to the cent.

`profit_floor_active` tracks the **ladder only** — a `stop_price` exactly equal to
`entry_price` is the breakeven lock, which leaves that flag `false`. Do not read
`false` as "unprotected".

### Options Positions Check

```bash
python3 -c "
import json
d = json.load(open('/root/trading-bot/data/options_positions.json'))
print(f'{len(d)} open option position(s)')
for k, v in d.items():
    print(' ', k, v)
"
```

Do **not** use `cat file || echo 'No open options'` — the file is `{}` when flat,
so `cat` succeeds and the fallback never fires.

### Broker Floors Check

```bash
cd ~/trading-bot && python3 -c "
import sys; sys.path.insert(0, '.')
import tradestation_client as tc

acct = tc.get_account_id()
if not acct:
    sys.exit('FAIL: no account id (auth) — do NOT read as zero floors')

orders = tc.get_working_orders(acct)
if orders is None:
    sys.exit('FAIL: fetch returned None — do NOT read as zero floors')

floors = [o for o in orders if o.get('order_type') == 'StopMarket']
print(f'{len(floors)} GTC floors resting')
for o in floors:
    print(' ', o.get('symbol'), o.get('stop_price'),
          o.get('duration'), o.get('status'))
"
```

Three traps, all of which make a broken check look like a clean one:

1. **Keys are normalized lowercase.** `get_working_orders` returns `order_id`,
   `symbol`, `action`, `quantity`, `order_type`, `stop_price`, `duration`,
   `status`. Using the raw TradeStation spellings (`OrderType`, `Symbol`,
   `StopPrice`) yields `None` for every field and prints `0 GTC floors resting`
   unconditionally.
2. **`None` ≠ `[]`.** `None` means the fetch FAILED; `[]` means genuinely nothing
   resting. Never collapse them.
3. **Do not append `2>/dev/null`.** It hides auth failures and the `TypeError`
   from iterating `None`.

Each bare `python3 -c` re-authenticates (the token cache is per-process). Several
in quick succession can 401 the token endpoint, and a swallowed 401 surfaces as a
silent empty result — so run this once, not in a loop.

Broker floors legitimately sit BELOW the bot stop: the GTC is raised only by
profit-floor rungs, never by the ATR trail. A position below the first rung still
rests at its entry-time disaster floor. That gap is the real overnight/gap
exposure.

### Discovery Health Check

```bash
python3 -c "
import json
d = json.load(open('/root/strategy-discovery/logs/autodiscover_summary.json'))
spent = d.get('spent_usd', 0)
hits  = d.get('hits', [])
print(f'spent: \${spent:.4f}   hits: {len(hits)}')
if spent == 0:
    print('WARNING: spent=0 — likely an API failure, not a cheap run')
"
```

**`hits: 0` is healthy and expected.** The generator promotes on
`ci_lower > 1.0`, not on PF or score, so zero hits is the normal state until the
generator improves. Do not treat it as breakage; `spent == 0` is the real alarm.

Spend landing at or just over the `$0.50` nightly ceiling is normal (the cap is
checked between candidates, so the last one can cross it).

Nightly run is 03:00 ET (`--fast-only`). The momentum screen shares ONE free
Polygon key with it — keep their schedules non-overlapping.
