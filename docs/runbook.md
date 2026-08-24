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

### Durable sink check (`critical_alerts.log`)

`config.CRITICAL_ALERT_FILE` is a second, rotation-proof copy of every
`[CRITICAL]` record, written by the handler at `trade_logger.py:22`. It lives in
the repo root, NOT `logs/`, precisely so logrotate's `logs/*.log` glob cannot
truncate it. Check it as well as `bot.log` — it is the only copy that survives a
weekend rotation.

```bash
f=~/trading-bot/critical_alerts.log
if   [ ! -e "$f" ]; then echo "MISSING — sink not writing, investigate"
elif [ -s "$f" ];   then cat "$f"
else                     echo "empty - good!"
fi
```

**Wrong pattern — DO NOT USE:**

```bash
cat ~/trading-bot/critical_alerts.log || echo "empty - good!"   # ← never fires
[ -s file ] && cat file || echo "empty - good!"                 # ← lies when MISSING
```

The first is the `cat`-succeeds-on-empty trap already documented under Options
Positions: an existing empty file makes `cat` exit 0, so the fallback is dead
code and you learn nothing. The second fixes that but introduces a worse bug —
`-s` is false for *missing* as well as *empty*, so a sink that was never created
(or got deleted) reports **"empty - good!"**. That is the failure mode this file
exists to prevent, reported as health. Test `-e` before `-s`, always.

**Wrong pattern — DO NOT USE:**

```bash
grep "CRITICAL" bot.log        # ← matches the startup BANNER
```

The banner is an `[INFO]` line that *describes* the CRITICAL feature
("Exit alerts : CRITICAL on rejection ..."). All 8 lifetime hits of the
unbracketed pattern are banner text; zero are real events. So the loose pattern
gives a false POSITIVE on a clean bot, and — because you learn to ignore it —
a false negative when it matters.

### Alternation needs `-E`

```bash
grep    "STOP|EXIT|TRAIL" bot.log     # ← matches the LITERAL string "STOP|EXIT|TRAIL"
grep -E "STOP|EXIT|TRAIL" bot.log     # ← matches STOP or EXIT or TRAIL
```

Without `-E` the pipes are literal, so the pattern can only match if the file
contains that exact text with the bars in it. It never does, so you get a silent
empty result that is indistinguishable from a genuine all-clear.

**A clean grep result from a pattern without `-E` is meaningless — not
reassuring.** Re-run with `-E` before reporting any alternation grep as a
negative. `grep -e a -e b` and BRE's escaped `grep "a\|b"` are equally correct;
the bare `|` is the only broken form. This has produced false all-clears on stop
exits, futures activity and sentiment checks, so it is promoted out of the
CRITICAL note above to its own heading.

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

**Nothing pages you.** As of 2026-08-17 the durable sink above closes the
*retention* half of this gap — a CRITICAL now survives rotation and reboot — but
it is a sink, not an alert channel. No outbound notification exists, so these
manual checks remain the entire detection mechanism. Wiring an outbound channel
is still open work — see `backlog.md`.

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

### Sentiment Overlay Check

The overlay can fail SILENTLY as far as the bots are concerned, so check it
directly. `sentiment-analysis.timer` fires Mon 08:00 ET; there is no push
channel, so nothing pages you.

```bash
python3 -c "import json; d=json.load(open('data/sentiment_report.json')); \
print({k: d.get(k) for k in ('fallback','fear_score','regime','headlines_analyzed','generated_at')})"
systemctl status sentiment-analysis --no-pager | tail -3
tail -5 logs/sentiment.log
```

**`fallback: true` means every other field is synthetic.** The neutral fallback
writes `fear_score: 1` (the most bullish score possible), `regime: risk_on` and
all sectors `low`. Do not read any of it as a market signal. `headlines_analyzed:
0` is the corroborating tell.

**The failure is NOT visible in `bot.log` or `futures_bot.log`** — zero matches
for the error, by design. The bots never call the API; they only read the JSON.
The exception is written by `sentiment_analyzer.py` into `logs/sentiment.log` and
`journalctl -u sentiment-analysis`. Grepping the bot logs for a credit or API
error will always come back clean and always be meaningless here.

Fields that do NOT exist in the report, and so silently return your `.get()`
default: `override_active`, `sectors_blocked`, `cost`. The real keys are
`generated_at`, `fear_score`, `regime`, `top_risks`, `sector_risks`, `summary`,
`headlines_analyzed`, `fallback`. Override state is `config.ENABLE_SENTIMENT_OVERRIDE`
+ `SENTIMENT_OVERRIDE_MIN_FEAR`, not a report field.

A fallback is SAFE but not inert. `sentiment_participates()` gates on
`fear >= SENTIMENT_OVERRIDE_MIN_FEAR` and `effective_regime` combines with
`_more_fearful` (strict max), so a `risk_on` fallback can only lose the max —
it can never loosen a VIX-derived regime. **But the sector gate is independent of
the fear threshold**: a `high` sector blocks new long entries regardless of
`fear_score`. A wrong sector read still bites during a fallback.

Known cause as of 2026-08-24: Anthropic API credit exhausted (400
`invalid_request_error`, "credit balance is too low"). Recurs every Monday until
topped up. The run correctly exits 1, so the timer shows FAILED — that, or this
check, are the only ways to notice.

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
