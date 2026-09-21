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

**No newlines inside the quotes either.** Wrapping a long alternation across
lines for readability splits it into two patterns, and the first one is left
ending in a bare `|` — an empty alternative:

```bash
grep -nE "at most 400|disallows timeframes|
GEN-FAIL|seasonality|^DONE"           # ← BROKEN, two patterns, first ends in `|`
grep -nE "at most 400|disallows timeframes|GEN-FAIL|seasonality|^DONE"  # ← correct
```

What happens next depends on which `grep` is on PATH, and **both outcomes are
silent**: GNU grep treats the empty alternative as matching every line, so you
get the whole file back and every line looks like a hit; `ugrep` (what is
actually installed here) rejects the pattern with `empty (sub)expression` and
prints **zero** matches, which piped into `wc -l` or `head` reads as a clean
all-clear. Same failure family as the missing `-E` — keep the alternation on one
line and let it run long.

### Exit / stop event check — and never filter on `strategy: `

**Correct pattern:**

```bash
grep -E "EXIT|STOP ARMED|WATER FLOOR|PROFIT FLOOR|SIGNAL BUY|SELL_TO_CLOSE" \
  ~/trading-bot/logs/bot.log \
  | grep -v "\] bot: "          # ← drop the startup banner, see below
```

**Drop the `bot: ` lines, but never the `strategy: ` ones.** The startup banner
*describes* these features in `[INFO] bot:` text ("Water floor : ENABLED ...",
"Profit floor: ENABLED ...", "Option exits: ENABLED ..."), so on any day the bot
restarted it matches the pattern without an event having occurred. On
2026-09-10 that was **exactly half the hits — 6 banner, 6 real.** Same
false-positive class as `grep "CRITICAL"` matching the banner (above): a loose
pattern gives a false POSITIVE on a quiet bot, and because you learn to skim
past it, a false negative on the day it matters.

**Wrong pattern — DO NOT USE:**

```bash
... | grep -v "bot: " | grep -v "strategy: "    # ← removes every exit
```

**Exits are logged by the `strategy` logger** (and the settled fill line by
`trade`). The `bot` logger only carries session lifecycle — market open/closed,
`cycle work=`. So the two exclusions above, added to strip per-symbol quote
spam, delete exactly the lines being looked for and return empty. That reads as
"nothing happened today" rather than "I filtered out the answer."

The reason the filter is tempting is that `strategy: ` prefixes **both** the
routine per-symbol poll lines *and* the exit events — it is a logger name, not
an event class, so it cannot separate noise from signal. Seen 2026-09-10: a
five-stage pipeline reported no META and no AMD-call exit on a day both had
exited.

To cut the poll spam, exclude the *symbols* or filter on level instead:

```bash
# per-symbol poll lines are "strategy: SPY | price=..." — the symbol, not the logger
grep -E "EXIT|WATER FLOOR|STOP TRAIL" bot.log | grep -vE "strategy: [A-Z]+ \|"

# highest signal of all: every stop/option exit is WARNING, no banner noise
grep "\[WARNING\]" bot.log
```

The `[WARNING]` filter is the one to reach for first. On 2026-09-10 it returned
**3 lines** — both real exits plus one `SECTOR MAP GAP` warning — against 12 for
the pattern above. Note it will NOT show the `trade:` fill line (that is `[INFO]`),
so use it to find *that* an exit happened and `trades.log` to find at what price.

**Better still, read `logs/trades.log`.** It is the authoritative exit record:
one JSON object per fill carrying `fill_price`, `slippage`, `stop_at_exit`,
`water_at_exit` and the `*_caused_exit` attribution flags. Answering "how did X
exit" from `bot.log` greps is doing it the hard way, and the attribution flags
are not in `bot.log` at all.

**Timestamp trap:** `bot.log` is **UTC**, `trades.log` is **ET** (`EDT`/`EST`
suffixed). The two disagree by 4–5 hours by design — do not align them by eye,
and note a date filter on `bot.log` is usually redundant since it holds one day.

Same silent-empty-grep family as the `-E` trap above and the dated-grep trap
under *Seasonality failure check*.

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

### Futures bootstrap watch

```bash
grep "STOP BOOTSTRAP" ~/trading-bot/logs/futures_bot.log | grep "$(date +%Y-%m-%d)"
```

**Empty output is the healthy result.**

If this appears for a futures symbol, a new **estimated entry** was created
outside the normal entry path. Treat it as an adopted-entry situation: the stop
record anchors on the **live price at adoption, not the true fill**, because
TradeStation reports `cost_basis`/TotalCost as MARGIN for futures and
`_bootstrap_stop` refuses it. Every entry-anchored feature — profit floor,
breakeven lock, water floor, run-length-at-arming, and the exit P&L — then reads
from the wrong basis.

The three legacy corrections (ES **+$1,925**, NQ **−$8,555**, RTY **−$2,355**)
are **resolved** — all three legs exited 2026-08-30/09-01 and reconciled exactly;
true book −$2,482.50, not the +$6,502.50 reported. See `docs/backlog.md`, "The
whole futures book at true fills". **Any new STOP BOOTSTRAP starts fresh** — those
corrections do not apply to it, and it needs its own row computed from
`futures_trades.log*` before that log rotates.

Two gotchas:

- **`date +%Y-%m-%d` only lines up because this host and `futures_bot.log` are
  both UTC.** If either changes, the second grep silently matches nothing and the
  check passes for the wrong reason. Note the trade logs are **EDT**, so the same
  substitution is wrong there.
- **`futures_bot.log` holds one day.** This is the daily check; for "has it *ever*
  fired", the live log alone will lie —

  ```bash
  zcat -f ~/trading-bot/logs/futures_bot.log* | grep "STOP BOOTSTRAP"
  ```

  `zcat -f` passes plain files through, so one glob covers the live log, `.1`, and
  every `.gz`. Do **not** reach for `futures_bot.log*[!z]` to mean "the
  uncompressed ones" — it requires a character after `.log` and therefore skips
  the live log, which is the file most likely to hold today's hit.

  As of 2026-09-02 this returns **nothing**, and that is not evidence the
  bootstrap never ran: the 2026-08-24 ESU26/NQU26/RTYU26 bootstrap has **already
  aged out of all 7 rotations.** The durable record is the table in
  `docs/backlog.md`, not the logs.

The durable state check, independent of log rotation — a bootstrapped record
carries the flag for as long as the position is open:

```bash
python3 -c "
import json
d = json.load(open('/root/trading-bot/data/stop_prices.futures.json'))
bad = {k: v for k, v in d.items() if v.get('bootstrapped')}
print(f'{len(d)} futures stop record(s), {len(bad)} with an ESTIMATED entry')
for k, v in bad.items():
    print(' ', k, 'entry_price', v.get('entry_price'), '<- live price at adoption, NOT the fill')
"
```

`{}` means flat, which is also why `cat` is the wrong tool here — same trap as the
options file below.

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

Spend landing at or just over the `$0.60` nightly ceiling is normal (the cap is
checked between candidates, so the last one can cross it). The ceiling is read
off the run header — `autodiscover START (n=20, cost-ceiling=$0.60, fast-only)`
— not from this doc, which has been stale before.

Nightly run is 03:00 ET (`--fast-only`). The momentum screen shares ONE free
Polygon key with it — keep their schedules non-overlapping.

#### Seasonality failure check

Run after 07:30 UTC **any day** — `OnCalendar=*-*-* 03:00:00 America/New_York`
is every day, not weekdays; the timer demonstrably fired Sat 08-29 and Sun 08-30.

```bash
# Keep the whole alternation on ONE line — no newlines inside quotes!
awk '/autodiscover START/{buf=""} {buf=buf $0 ORS} END{printf "%s", buf}' \
  ~/strategy-discovery/logs/autodiscover.log |
  grep -nE "at most 400|disallows timeframes|GEN-FAIL|seasonality|^DONE"
```

A clean run has **no** `at most 400` and **no** `disallows timeframes` lines, and
always ends with the `DONE` line.

**Do NOT add `| grep <date>` to this.** Only three lines in the whole log carry a
date (`START`, the summary-saved line, `END`). The `CAND N` lines and their
`attempt N:` errors are undated, so a dated grep returns **zero lines** and reads
as a clean pass while the failures sit right there in the file. That is why the
`awk` block is here: it isolates the LAST run without needing a date at all. Same
silent-empty-grep family as the `-E` alternation trap above.

Interpreting the `DONE` line:

- `usable=20` is the target. Anything less means candidates failed *generation* —
  look for `GEN-FAIL all 3 attempts failed` above it.
- `hits=0` is still expected and healthy; `usable` counts generation, `hits`
  counts `ci_lower > 1.0` promotions. They are different gates.
- `reason=batch_exhausted` is the normal ending. `reason=cost_ceiling` means the
  run stopped early on spend, which looks like a generation failure but is not —
  check `spent=` against the header's ceiling.

Background: two of 20 slots were lost to this on 2026-09-04 (`CAND 9`, `CAND 19`,
`usable=18`). Both burned all three attempts — three failures on the 400-char
thesis ceiling (`spec.py`, `max_length=400`) and three on `timeframes ['5m']`
rejected by the translator. Prompt constraints added in `strategy-discovery`
`ed38090`.

## Monthly: S&P 500 Constituent Refresh

`data/sp500.json` is the momentum-screen **universe** — the 503 names
`momentum_screen.py` ranks. It is a **vendored file**, not a live API lookup, so
it only changes when something rewrites it.

**This is now automated.** `sp500-refresh.timer` runs monthly on the 1st at
05:30 ET, 30 minutes before `momentum-rotation.timer` (06:00 ET on the 1st and
15th), so the 1st-of-month rotation always screens the current index. Both are
pre-market, so the swap never lands mid-session. Source is
`constituents.csv` on GitHub — **not Polygon**, so it spends none of the shared
5-calls/min free-tier budget.

To run it by hand (safe any time — it shares `momentum.lock` with the screen, so
it cannot race a rotation):

```bash
python3 refresh_sp500.py --dry-run   # print the symbol diff, write nothing
python3 refresh_sp500.py             # rewrite data/sp500.json
```

### Why this needed automating

Before 2026-09-03 there was **no schedule at all**: the file had sat unchanged
since **2026-07-14**, and every index change in those ~7 weeks was invisible to
the momentum screen. Nothing warned about it — `MOMENTUM_MAX_AGE_DAYS` guards
`momentum_watchlist.json` (the generated slot), **not** the universe file, which
has no staleness check whatsoever. The refresh on 2026-09-03 found the drift was
`+3 / -3`:

| | Symbols | Screenable? |
|---|---|---|
| added | `FERG`, `RDDT`, `VMRK` | FERG (Industrials) + RDDT (Communication Services) yes; VMRK is a Real Estate REIT, so `EXCLUDED_SECTORS` blocks it |
| removed | `AVB`, `EA`, `EQR` | AVB/EQR were Real Estate, already excluded — only `EA` was a real loss of a screenable name |

So the practical effect of 7 weeks of staleness was **two** newly screenable
names missed and one stale one carried. That's the expected magnitude — index
changes are rare, which is why monthly is sufficient.

Note the staleness bound moved on 2026-09-16: it used to be ≤2 weeks, set by the
15th being the last rotation in a month. The momentum rotation is now **weekly**,
so the last Monday of a month screens a universe up to **~4 weeks** old. That is
still well inside the measured tolerance above, so the monthly refresh stands —
but if constituent churn ever picks up, this is the number that moved.

### Checks

```bash
systemctl list-timers sp500-refresh.timer --all   # NEXT should be the 1st, 05:30 ET
grep sp500-refresh logs/momentum.log | tail -4    # START/END + exit code
python3 -c "
import json; d = json.load(open('/root/trading-bot/data/sp500.json'))
print('count :', d['count']); print('source:', d['source'])
"
```

A failed fetch **exits non-zero and leaves the old file in place** — the stale
file is the graceful degradation, but the unit must go FAILED so it is visible.
A timer showing green with a rotting universe file is the failure mode this
whole section exists to prevent. `count` is expected to be **503**, not 500: three
companies carry dual share classes (`GOOGL`/`GOOG`, `FOXA`/`FOX`, `NWSA`/`NWS`).

## Test Suite Health

### Full suite (authoritative)

```bash
cd ~/trading-bot && .venv/bin/python -m pytest -q 2>/dev/null | tail -3
```

Expected: **712 passed, 3 failed**. The three are `test_futures_orders.py`
(403 Forbidden from the sim futures endpoint) and are the standing baseline —
treat any *fourth* failure as real. Note `pytest` is not on the system python;
it only exists in `.venv`.

### `__main__` ordering check — the bug pytest structurally cannot see

Every test file ends with an `if __name__ == "__main__":` runner that builds its
list from `globals()` **at call time**. Anything defined *below* that block does
not exist yet when it runs. pytest imports the whole module before running
anything, so it never sees this — the file stays green while the direct runner
is wrong.

```bash
cd ~/trading-bot && for f in test_*.py; do
  m=$(grep -n '^if __name__ == "__main__":' "$f" | head -1 | cut -d: -f1)
  [ -z "$m" ] && continue
  n=$(awk -v m="$m" 'NR>m && /^(def |class |@)/' "$f" | wc -l)
  [ "$n" -gt 0 ] && echo "$f: $n definition(s) AFTER the __main__ block"
done; echo "(no output = clean)"
```

Two failure modes, and the quiet one is the dangerous one:

* **Loud** — the runner calls a helper defined below it and dies with
  `NameError`. This was `test_sentiment.py` (`_override`), fixed ba7a5c8.
* **Silent** — a *test* is defined below it, so it is simply never collected.
  No error, and the runner still prints `All N assertions passed`. This was
  `test_broker_floor.py`: pytest ran 21, the direct run ran 20 and reported
  success. The uncollected test was the one asserting a rejected broker floor
  does not latch `_floors_reconciled`. A runner reporting success without doing
  the work — same shape as the `"materials"` dead-key trap in
  `sentiment_analyzer.py`.

When a mismatch is suspected, compare the two counts directly rather than
trusting either summary line:

```bash
f=test_broker_floor.py
.venv/bin/python -m pytest $f -q 2>/dev/null | tail -1   # pytest's count
.venv/bin/python $f 2>/dev/null | grep -c "PASS  "       # direct run's count
```

**The counts must match.** `All N passed` with the wrong N is the silent case.

### Standalone convention — a guarantee; any failure IS a regression

**Use the wrapper.** It spawns one fresh process per file (which is the whole
point of the convention) and isolates every state file to a throwaway tmpdir
first:

```bash
cd ~/trading-bot && .venv/bin/python run_test.py            # all files
cd ~/trading-bot && .venv/bin/python run_test.py test_stops.py test_water_floor.py
```

**Expected: `44/44 passed`, exit 0.** Treat any failure here as a real
regression, not as known drift. This was 27/13 until the 13 were fixed
(2026-09-16), and 40/40 before `run_test.py` and `test_state_isolation.py`
were added.

The bare form still works and is still safe:

```bash
.venv/bin/python test_stops.py
```

`config._detect_test_run()` fires on an argv[0] starting with `test_`, so a
direct run writes state under `data/test/` and logs as `logs/test_bot.log` —
never the live files. The wrapper only upgrades that to a tmpdir that cleans
itself up and leaves no `data/test/` behind.

**Why this exists (2026-09-21):** conftest.py is a *pytest* hook, so for a long
time the direct form had no redirects at all. A standalone run wrote a synthetic
SPY position — `entry_price 100.0`, `atr_at_entry 4.0`, `broker_order_id "X"` —
into the live `data/stop_prices.json`, and the equities bot loaded it on the
next restart. The deploy box is also the dev box, so this was live state, not a
sandbox. `config._state_path()` is now the single floor for both entry points;
`test_state_isolation.py` asserts it, including that a stray
`export TB_TEST_TMPDIR=...` can never redirect a *production* process.

If you ever see `REDIRECT FAILED — production state at risk`, the wrapper and
`config.py` have drifted: a new state file was added to config without being
added to `run_test._GUARDED`. Fix that before running tests on this box.

One exception before you go hunting: `test_smoke.py` and `test_smoke_futures.py`
are LIVE read-only checks against TradeStation, not hermetic unit tests. Each
invocation re-authenticates, and the token endpoint throttles rapid cold
refreshes (~4 in a few minutes → 401), so a back-to-back sweep can flake one of
them. **Re-run that single file before believing it.** Every other file in the
list is offline and a failure there is real.

What made those 13 fail, and what to do when a new one appears: `conftest.py`
pins three stop sources OFF before every pytest test —
`ENABLE_WATER_FLOOR`, `ENABLE_PROFIT_FLOOR`, `ENABLE_BROKER_STOP_FLOOR` — because
each one silently overrides the ATR trail whenever it is more protective, so a
test that never mentions them still measures them. The standalone runner has no
conftest and gets the production values. **Restate those pins in the file's
`_reset`**; the module that owns a feature re-enables its own flag per test
(`test_water_floor.py`, `test_profit_floor.py`, `test_broker_floor.py`).

Two other shapes showed up in the same sweep:

* **Doubles with fixed arity.** `_place_broker_floor` runs on every entry and is
  NOT behind `USE_TRAILING_STOP`, passing `order_type=`/`stop_price=`. A
  `def _fake_place(account_id, symbol, side, qty)` stub dies on `TypeError`.
  Give every order double `**kw`.
* **Assertions on a leaked flag.** `test_vix_regime` asserted the `CAUTIOUS MODE`
  line, which is gated on `USE_MOMENTUM_ALIGNMENT` — `False` in production since
  2026-07-24. It only ever passed on a `True` leaked from another module. If a
  case needs a non-production flag, it must pin it itself and say why.

A crashing direct run **does** exit non-zero, so the check above is trustworthy;
the summary line is not, because the format varies per file (`All N tests
passed.` / `All N assertions passed.` / `RESULTS: N passed`) and an aborted run
prints no summary at all. Never conclude from `tail -1` alone.
