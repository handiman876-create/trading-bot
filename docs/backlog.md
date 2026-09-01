# Backlog

Deferred work and known limitations that are observed but not actively
prioritized. Each entry: what was seen, where, and the proposed direction.

## CRITICAL log lines reach no one — REQUIRED BEFORE GOING LIVE

As of 2026-08-11 the bot logs `CRITICAL` on the two states where it wanted out
of a position and could not get out:

- **Exit rejected by the broker** — the position is still open, carrying the
  broker's own `RejectReason`.
- **Floor cancel will not confirm** — the exit is withheld rather than risk a
  refusal, so the position may be open *and unprotected*.

Both retry on the next cycle, so a transient failure self-clears. A **repeating**
one never does: an entitlement problem, a bad symbol, or a stuck share
reservation will refuse the exit identically every cycle while the position sits
there.

**The gap: CRITICAL goes to `bot.log` and nowhere else.** There is no alert
channel in this repo. Nothing pages, emails, or pushes. The line only "catches"
a failure when a human happens to read the log — which is exactly how the
2026-08-11 GOOGL incident ran for three hours before anyone noticed, and that
was with the failure logged at INFO/WARNING as "still pending". CRITICAL makes
it *findable*; it does not make it *noticed*.

Proposed directions, cheapest first:

- **Scan in an existing timer.** `performance-analyzer.timer` already runs and
  already writes a report a human reads. A `grep -c CRITICAL` over `bot.log*`
  (plus the `.gz` archives — `bot.log` holds one day only) costs nothing and
  needs no new infrastructure. Weakness: weekly cadence is far too slow for an
  open unprotected position.
- **systemd `OnFailure=`.** Clean and native, but it fires on *process* failure,
  and a CRITICAL here does **not** crash the bot — the whole design is to keep
  trading and retry. Would need the bot to exit non-zero on a repeat count, which
  conflicts with "degrade, don't die". Probably wrong for this.
- **A dedicated cron scan + webhook/email**, every 5–15 min over the tail of
  `bot.log`, alerting on new CRITICAL lines since the last watermark. Most likely
  the right answer. Needs a watermark file so a single event does not re-alert
  forever, and must fail non-zero itself if the scan cannot run (see the
  fail-safe-is-not-exit-0 rule).

Counters already exist for the underlying conditions (`_exit_rejections`,
`_floor_clear_stuck`, `_floor_rearms`), so an alert can key off those rather than
scraping text — but they are per-process and reset on restart, so a scan needs
the log either way.

**This is paper trading today. Do not go live without an alert channel** — the
whole failure class is "the bot believes it closed a position it did not", and
in a live account that is real money left exposed with no one watching.

## A/B tracker: option IV needs an entitled Polygon key

**Observed (2026-07-19):** `screen_ab_tracker.py` records `avg_iv` per screen via
`polygon_client.get_atm_option_iv()`, which reads `/v3/snapshot/options/{symbol}`.
On the current free/shared Polygon key that endpoint returns **HTTP 403
`NOT_AUTHORIZED`** ("You are not entitled to this data"). The fetch catches this,
logs a WARNING, and returns `None`, so every pick is still recorded and the
2-week return comparison is unaffected — but `avg_iv` will read `None` for the
whole experiment. Realized volatility (`avg_rv`) is recorded alongside as the
premium proxy and answers the volatility question meaningfully in the meantime.

**Direction:** No code change needed — the IV path is already correct. When an
options-entitled Polygon key is available, set `POLYGON_API_KEY` to it (or point
the tracker at a separate entitled key) and `avg_iv` populates automatically.
Polygon's Options Starter (~$29/mo) is the cheapest tier that returns the
snapshot's `implied_volatility`/greeks. Revisit if/when the A/B result argues that
true IV (not just realized vol) is decision-relevant.

## reconcile_stops(): re-arm existing positions on a significant regime change

**Observed (2026-07-23):** Pre-2026-07-18 positions carry no `atr_mult` and fall
back to 2.5x (risk_on width) for their whole life; by design, an existing stop is
never re-widened/re-tightened when the regime shifts — only NEW entries feel it
(config.py:139-146). Idea: on a material regime change, run a reconcile pass that
re-derives `atr_mult` for open positions from the current regime x ATR band.

**IMPORTANT — vet these two traps before building (both surfaced while scoping
DDOG on 2026-07-23):**
1. **Which price feeds the band?** The shipped rule bands on **ATR/price AT
   ENTRY** (`_get_atr_mult`, strategy.py:396). Re-deriving on *current* price is a
   different, unbuilt rule. Example: DDOG is 4.98% (normal, 2.5x) at entry but
   5.19% (high, 1.5x) at today's lower price. Current-price banding **tightens the
   stop precisely because the position fell** — the more a long loses, the harder
   it yanks the stop toward market, stopping out near local lows. Backwards for a
   trailing stop. If we reconcile, band on entry price, not spot.
2. **Never arm a stop through the market.** A re-tightened long stop can land
   ABOVE current price (DDOG's 1.5x figure was $254.37 vs $244.40 spot = instant
   sell). Any reconcile MUST clamp: never place a stop above spot (long) / below
   spot (short) — else "better protection" becomes an immediate realized loss.

**Note:** the motivating "$2,473 better on DDOG" figure was a mirage — it combined
both traps (current-price banding + a stop above market). Entry-band reconcile on
DDOG today yields 2.5x = no change. The genuine goal behind it ("winners stop
giving back gains") is better served by a regime-independent **breakeven-lock**
(floor stop at entry once high_water >= entry), which cannot place a stop through
market. Discuss before implementing.

## SHORT_MAX_ATR_PCT: exclude high-ATR names from short entries

**Observed (2026-07-27):** every short entry in the retained window (7, spanning
2026-07-17..07-27) lost money, total -$10,775. The high-ATR ones lost most:
blocking `ATR/price >= 5%` at entry would have stopped AMD x3 and PLTR
(-$8,523 of the -$10,775, 79%). AMD entered at **7.82%** ATR/price, giving a
1.5x stop **11.73% wide** — so the breakeven lock (which needs +1.0 ATR = a
7.82% favorable move) is effectively unreachable on that name. AMD's actual
favorable excursion was 0.04 ATR.

Current watchlist spread (2026-07-27 close, Wilder ATR14): 6 of 20 names sit at
>= 5% — PLTR 5.04, CRWD 5.35, TSLA 5.96, AMD 7.57, ARM 9.64, CRWV 9.87. So the
rule is genuinely selective (leaves 10 of 15 core shortable), NOT an off-switch.

**Well-formed but currently unmeasurable — do NOT implement yet. Four reasons:**

1. **The regime filter already blocks all of it.** `SHORT_MIN_REGIME` (shipped
   `dfcfcca`, 2026-07-27) blocks new shorts below cautious, and all 7 historical
   shorts were armed in risk_on. Both rules would block the same 4 trades, so
   shipping this one now makes neither attributable to any outcome.
2. **The threshold is knife-edge on the trade that motivates it.** PLTR reads
   **5.04%** — four hundredths above the line. At a 7% cutoff PLTR is waved
   through and the -$3,120 is not caught; at 5% it is. Picking between those
   from 7 trades is fitting, not discovery. Compare the DDOG-at-4.98% note in
   the reconcile_stops entry above: same cliff, already documented once.
3. **The dollar case is mostly position sizing.** Blocked trades averaged
   -$2,131 vs -$751 allowed, but every position is 5% of equity, so high-ATR
   names mechanically swing more in dollars. Normalized by ATR the gap narrows
   to 0.67 vs 0.39 ATR — and the worst risk-adjusted trade (PLTR, 1.21 ATR) is
   in the *blocked* group only by that 0.04% margin.
4. **Zero cautious-regime shorts exist.** risk_on is the only regime observed,
   so there is no control group and no evidence risk_on (or high ATR%) is
   *causal* rather than merely coincident. 0-for-7 on one regime is not a
   comparison.

**Direction:** run death-cross-short through the discovery pipeline with ATR% as
a **conditioning variable**, and let the existing `ci_lower > 1.0` promotion gate
choose the cutoff — instead of hand-picking 5% or 7% from live losses. If the
archetype clears ci_lower only when conditioned on ATR%, that is a real finding
and worth building; if it fails unconditionally, no entry filter rescues it and
the right answer is to stop shorting on this signal. Note the survivors under a
5% filter were still 0-for-3: the filter reduces trade count, it has not been
shown to find a *profitable* short.

**If it is ever built:** do NOT add a new `SHORT_MAX_ATR_PCT` constant.
`ATR_PCT_HIGH_THRESHOLD` is already 0.05 and the band is already computed and
persisted at entry (`atr_mult` / `atr_at_entry`), so the rule states as "do not
short anything in the high-vol band" and reuses that machinery rather than
adding a third ATR%-of-price constant beside the two that exist.

**Prerequisite:** let `SHORT_MIN_REGIME` run 2-4 weeks first and gather actual
cautious-regime short data. Revisit only with pipeline evidence.

## SPY trend confirmation: demote the effective regime when SPY disagrees

**Observed (2026-07-30):** the whole session ran `cautious` on a SENTIMENT
OVERRIDE (`fear=6`) while VIX itself read `risk_on` at 18.7-18.8 — the combined
regime was driven entirely by Claude's read, not the fear gauge. Hypothesis: use
SPY's own EMA trend as a third opinion, demoting back toward `risk_on` when the
index disagrees with a sentiment-driven demotion.

**Today argues it is NOT needed.** SPY was bearish for the entire session —
EMA9 740.45 < EMA21 742.76 at the close, spread -0.31%, bearish across all 360
SPY indicator prints. A confirmation rule would have *agreed* with `cautious`
and changed no decision. The system was already right without it.

**Direction:** insert at `main.py:141`, after `strategy._more_fearful(...)` and
before `note_regime(...)`. It must be a **separate demotion step, not another
argument to `_more_fearful`** — that function is a strict max over
`_REGIME_RANK` (strategy.py:1145/1148), so it can only ever ratchet fear *up*.
Anything that reduces fear has to be an explicit second stage, and per the
project convention it needs its own counter (e.g. `SPY CONFIRM DEMOTE`) so we
can tell whether it ever earns its keep.

**Prerequisite:** enough cautious-regime sessions to measure the improvement
against. Same control-group problem as the `SHORT_MAX_ATR_PCT` entry above:
cautious-regime trading is very new (first cautious-regime shorts were
2026-07-28), so there is no sample to attribute an improvement to yet. Implement
only once cautious sessions are numerous enough that "SPY disagreed with a
sentiment demotion" has actually occurred more than once — today it did not
occur at all.

## Earnings blackout for short entries

**Observed (2026-07-30):** MSFT gapped **+12.1% overnight on earnings** (390.54
close 07-29 → 437.90 open 07-30, volume 109.4M vs ~30M baseline). It was a long,
so the gap ran in our favour — but the same mechanism on a short is an uncapped
loss, because a bot-managed stop cannot fill inside a gap. The bot currently has
**no earnings awareness of any kind**: no calendar source, no blackout, no
per-symbol event flag.

**Risk quantified (CRWD, open tonight):** 272 shares short from 175.59, stop
187.5351, no broker-native order. A stop-out fills around **-$3,249**; a gap
straight through it does not. From tonight's 185.28:

| overnight gap | fill | realized |
|---|---|---|
| +10% | ~203.81 | **-$7,676** |
| +15% | ~213.07 | **-$10,195** |

So the -$3,249 "cap" is a floor, not a ceiling — 2.4-3.1x worse in a routine
earnings gap. (This is larger than the -$5,000..-$8,000 first estimated; the
figures above are computed from the live stop record.) CRWD's own Q2 FY2027
quarter ends 2026-07-31 with a release expected ~2026-08-26, so this specific
position is **not** exposed tomorrow — but it will be inside ~4 weeks if held.

**Direction:** block NEW short entries within N days of a known earnings date.
For positions already open, either cover before the release or require a
broker-native stop first.

**Prerequisites — both are real blockers:**

1. **A data source, which the project does not have.** Polygon's
   `/vX/reference/financials` (already wired as
   `polygon_client.get_quarterly_financials`) returns *fiscal periods and filing
   lag*, not forward release dates — it can only infer a date from historical
   cadence, which is how the ~08-26 estimate above was reached. A real calendar
   needs a dedicated endpoint: Alpha Vantage `EARNINGS`, Financial Modeling Prep's
   earnings calendar, or a Polygon tier that exposes one. (Polygon's legacy
   `/v1/meta/symbols/{ticker}/company` is deprecated — do not build on it.) Note
   the shared-key constraint: Polygon is one free key split across the momentum
   screen and autodiscover, so a new poller needs a non-overlapping schedule.
2. **Broker-native stops should land first.** They protect against gaps
   regardless of cause — earnings, macro, halts, anything — whereas an earnings
   blackout only covers the subset of gaps we can predict. Building the narrow
   fix before the general one gets the ordering backwards. See the deferred
   broker-native-stop item (bot-managed stops are paper-only and give no
   overnight-gap protection).

## Short profit taking: formula fix required before the long-only guard is relaxed

**Observed (2026-07-31):** `_maybe_take_profit` (strategy.py:815) sizes the gain
with a single direction-blind formula:

```python
gain = (price - entry) / entry
```

This **fails for shorts, and it fails dangerously** — a *losing* short produces a
*positive* gain, so the rule reads it as a winner:

- NVDA, live at the time of writing: entry $194.34, price $201.78
- `(201.78 - 194.34) / 194.34` = **+3.83%** — on a position that is **down $1,830**
- At the 12% threshold this fires once a short has moved **12% against** you
- Line 848 then places a plain `"sell"`, which **adds to the short** rather than
  covering it

It does not fail safe. The formula never goes negative for shorts; it goes
positive precisely when the position is losing, so nothing downstream catches it.

**Current protection (both must be maintained):**

1. `if not config.ENABLE_PROFIT_TAKING or held <= 0` — strategy.py:828
2. `if held > 0 and _maybe_take_profit(...)` — the call site, strategy.py:1276

A short carries `held < 0`, so both reject it before any gain or RSI check runs.
The long-only docstring is accurate, not stale. Enabling the feature for longs
(2026-07-31) does not change this — shorts remain excluded by both guards.

**Direction — all three fixes land together before either guard is relaxed:**

1. **Direction-aware gain:**
   ```python
   if direction == 'short':
       gain = (entry - price) / entry
   else:
       gain = (price - entry) / entry
   ```
2. **Direction-aware order side:**
   ```python
   if direction == 'short':
       action = 'buytocover'
   else:
       action = 'sell'
   ```
3. **Direction-aware RSI bound.** RSI >= 60 means *extended*, which is the
   scale-out signal for a long. A short that is up 12% has driven the underlying
   down, so its RSI will be *low* — requiring RSI >= 60 on a profitable short is
   close to self-contradictory and would almost never fire. The mirror is:
   ```python
   if direction == 'short':
       rsi_ok = rsi <= RSI_MAX   # e.g. 40
   else:
       rsi_ok = rsi >= RSI_MIN   # e.g. 60
   ```
   Use a separate config constant rather than deriving `100 - RSI_MIN`; the two
   bounds need not stay mirror images.

`direction` is already persisted per position in `stop_prices.json`, so no new
state is needed. Note this is the same dispatched-logic shape seen three times
before in this project: put `(gain, rsi_ok, action)` in one direction-aware
helper on day one rather than duplicating a long path and a short path.

## Shadow options tracker

**Purpose:** parallel-track hypothetical options trades alongside real stock
trades, on identical signals, to compare performance without risking capital.
The equities book is 2-for-30 and the short leg is 0-for-7; buying a put on a
death cross has the same entry timing as shorting it, so a shadow run measures
whether the instrument change helps before any capital moves.

**Infrastructure confirmed working (2026-07-31, live API):**

| Piece | Where | Verified |
|---|---|---|
| `build_option_symbol()` | tradestation_client.py:113 | `NVDA 260821P200` accepted |
| `get_option_quote()` | tradestation_client.py:199 | returns last/bid/ask |
| `next_monthly_expiration()` | market_hours.py | → `2026-08-21` |
| `_atm_strike()` | strategy.py | nearest $5, already used live |

Real quotes pulled during the check:

```
NVDA 260821P200          last=6.79 bid=6.7  ask=6.8
NVDA 260821P205          last=8.93 bid=9.25 ask=9.45
NVDA 260814P200          last=5.5  bid=5.4  ask=5.6
NVDA 260814P205          last=7.56 bid=8.05 ask=8.2
```

Weeklies (260814) resolve as well as monthlies — not limited to the third Friday.

**Design — read-only, places no orders:**

- Hook the death cross: open a shadow put
- Hook the cover signal: close the shadow
- Persist to `data/options_shadow.json`
- Add a report section in `performance_analyzer.py`

**Critical implementation notes:**

1. **Use the ASK for entry, not `last`.** `last` can be stale and printed
   outside the spread — in the sample above `NVDA 260821P205` shows `last=8.93`
   against `bid=9.25`, a stale print from a closed market. The ask is what you
   would actually pay. Note `evaluate_option` (strategy.py:1433) currently reads
   `last` first and falls back to `bid`; the shadow tracker should NOT copy that
   ordering, and the live path arguably needs the same correction before it ever
   places a real order.

2. **The bid/ask spread is the real cost, and it is large.** `NVDA 260821P205`
   quotes bid 9.25 / ask 9.45 — a $0.20 spread on a ~$9.35 mid, ≈**2.1%
   round-trip**. Measured equity slippage for comparison: CRWD's cover cost
   $0.08 on $188.98, ≈**0.04%**. Options cost roughly **50× more to trade**.
   That ratio, measured empirically across real signals, is the number that
   decides whether options are viable here — it is the single most valuable
   output of the tracker.

3. **Use `next_monthly_expiration()`, never a hand-written calendar date.** The
   check initially failed with `FAILED, INVALID SYMBOL` on `2026-08-15` because
   that is a **Saturday**. Options expire Friday; the monthly is the third
   Friday, `2026-08-21`.

4. **ATM strike comes from `_atm_strike()`** — already implemented and in use.

5. **None of the live-trading gaps apply to a shadow run.** No stop arming, no
   regime gates, no fill resolution, no expiry management. Those four are real
   blockers for *placing* option orders (see the options scoping discussion) but
   are irrelevant to a read-only tracker. This is what makes the build small.

**Build estimate:** 1–2 hours when ready.
**Files:** `shadow_options.py` (new), `strategy.py` (2 hook lines),
`performance_analyzer.py` (report section).

**Prerequisite:** enough shadow data to compare — at least 5–10 signal events.
Build when the next cautious regime produces death crosses.

**Open question on that prerequisite:** short *entries* are gated to cautious
(`SHORT_MIN_REGIME`), but death *crosses* fire in any regime — and a shadow
tracker places no orders, so it need not inherit the regime gate at all. Letting
it record every death cross regardless of regime would reach 5–10 events far
faster, and would also capture the risk_on crosses the live book is currently
forbidden from taking. Decide this before building; it changes how long the data
takes to accumulate.

## 5-minute scalping bot

**Motivation (2026-08-01):** the daily EMA9/EMA21 crossover strategy has a
**6.7% win rate** — 2 wins in 30 closed trips, −$49,110.33 cumulative
(`data/trade_ledger.json`). The concept may not be wrong so much as sampled too
coarsely: a daily bar resolves one signal per session, so an intraday move that
runs and reverses inside a single candle is invisible to it. Same concept on
5-minute bars would see those moves.

**The AMD case that prompted this.** The live daily bot's AMD trip: 99 shares
SHORT, entered 2026-07-27 10:44:23 EDT @ $479.50, stopped out 2026-07-30
09:55:05 EDT @ $480.00 — **−$49.50**, a scratch after three days of exposure.
A hypothetical 5-minute version of the same logic over that window is estimated
at **+$3,069**.

> **The +$3,069 is an estimate, not a backtest.** It has not been produced by a
> replay against real 5-minute bars, and it is a single hand-picked episode on a
> strategy whose measured win rate is 6.7%. Cherry-picking one favourable window
> is exactly the error that put profit-taking on hold (see "Short profit taking"
> above, and the DDOG episode). Treat it as the reason to *measure*, never as
> evidence the edge exists. The shadow phase below is what would make it real.

**Design:**

- New service `trading-bot-scalping.service`, separate process/lock/logs
  alongside equities and futures (same pattern as `--mode futures`).
- Same EMA9/EMA21 concept, 5-minute bars.
- Tight stops — 5-min ATR runs ~$0.50–2.00 on these names.
- **Mandatory flat at 15:55 ET.** No overnight exposure, so no gap risk.
- Watchlist: SPY, QQQ, AMD, NVDA, TSLA, META.
- **Shadow mode first** — logs what it would do, places nothing.
- `CROSS_SUSTAIN_MINUTES=30` almost certainly does not transfer. On daily bars it
  spans a fraction of one candle; on 5-min bars it is 6 candles. The equivalent
  is likely 3–5 candles (15–25 min), but it should be re-fit on 5-min data, not
  converted arithmetically — the 30-min value is itself flagged PROVISIONAL
  (fit in-sample on 25 trips).

**Data availability:** TradeStation almost certainly serves 5-min bars —
`tradestation_client._UNIT_MAP` already carries a `"minute"` unit, so
`get_historical` likely needs only an interval argument rather than new
plumbing. **Verify before scoping further**, along with the rate limits: a
6-symbol watchlist on 5-min bars is a much higher call rate than the current
daily fetch, and the free Polygon key is already shared between the momentum
screen and autodiscover (non-overlapping schedules).

**Build prerequisites, in order:**

1. Verify the 5-min bar API and its rate limits.
2. Shadow test 2–4 weeks.
3. Compare against the daily bot on the same names over the same window.
4. Only then consider options scalping (0–3 DTE puts/calls).

**Why this connects to the options work.** The options position store shipped
2026-08-01 (`e5e18b9`) makes options exits reachable at all — before it, a
contract was orphaned as soon as the underlying moved half a strike, which is
routine intraday. 5-minute signals on 0–3 DTE contracts is gamma scalping, the
stated end goal. Note the ordering: 0–3 DTE options decay fastest and carry the
widest spreads (~2.1% round-trip vs ~0.04% on equities), so they punish a weak
signal far harder than shares do. The equity version has to prove an edge first;
options scalping cannot rescue a signal that does not work on stock.

---

## Options exit thresholds are arbitrary (not hardcoded — unvalidated)

Shipped 2026-08-06 in `ec48e56`. **These are already config constants**, so the
work is not plumbing:

```python
config.OPTION_PROFIT_TARGET_PCT   = 1.50   # +50%
config.OPTION_STOP_LOSS_PCT       = 0.50   # -50%
config.OPTION_MIN_DAYS_TO_EXPIRY  = 5
ENABLE_OPTION_EXIT_TARGETS        = True
```

Change any of them by editing config.py and restarting — no code change, and the
startup banner reprints the live values ("Option exits:" line).

**The open question is whether the NUMBERS are right, and there is currently no
evidence either way.** They were chosen as round symmetric figures, not fit to
anything. This bot has placed exactly three options trades ever
(SPY260717C00540000, which died orphaned; NVDA 260821C220; QQQ 260821C715), so
there is no distribution to fit against — the same problem that kept
ENABLE_PROFIT_TAKING disabled for two weeks, and the reason the equities
profit-take is still a single data point (MSFT 2026-08-03).

Specific doubts worth testing once there are closed trades:

- **-50% may be far too wide for a 2-3 week contract.** An ATM call routinely
  gives up half its premium on an ordinary underlying pullback and recovers. The
  stop may fire mostly on noise, or never fire before the expiry rule does.
- **The rules are symmetric; option payoffs are not.** A long call's loss is
  capped at 100% while its gain is unbounded, so +50%/-50% is not a 1:1 risk
  ratio in any meaningful sense.
- **5 days may be too late for 0-3 DTE work** (see the 5-minute scalping section
  above) and too early for a 45-day contract. It is one constant serving two very
  different regimes.
- **No trailing analogue.** The equities side trails its stop; options take a
  fixed threshold off entry and never ratchet, so a contract can go +140%, fall
  back to +5%, and exit on EMA state with nothing locked. Same structural gap as
  the breakeven lock flooring at entry rather than at profit.

**Acceptable as-is for now** — something is strictly better than the previous
state (EMA state only, i.e. no premium-based exit at all). Revisit once 5+
options round trips have closed, and fit rather than guess.

---

## Split profit floor measurement by direction

**Observed (2026-08-14, when `79589ca` made the ladders asymmetric):** the profit
floor is now two ladders — `PROFIT_FLOOR_STEPS_LONG` (first rung +15%, 5pp gaps
early) and `PROFIT_FLOOR_STEPS_SHORT` (first rung +8%, 3pp gaps early) — but
every measurement of the feature is direction-blind. Nothing can currently tell
whether the short ladder is helping or hurting on its own.

**Why it matters more on the short side.** At +8% with a 3pp gap the short floor
sits roughly **1 ATR behind price** on a typical name (AAPL's entry ATR is 3.0%
of entry), so it binds over the ATR trail almost immediately and exits on about
one ATR of retrace. That is a much more aggressive posture than the long ladder,
applied to a direction with **no demonstrated edge** — shorts are 0-for-lifetime
on win rate, `SHORT_MIN_REGIME` is effectively a no-op, and the sentiment-driven
short session on 2026-07-28 went −$2,389. Shipping an aggressive rule onto an
unproven direction is exactly when measurement has to come first.

**Direction — two changes, and the second is the one that matters:**

1. **The counter.** `_profit_floors` (strategy.py:460, incremented at :1448) →
   `_profit_floors_long` / `_profit_floors_short`, split in `_profit_floor()`'s
   caller where `direction` is already in scope. Reported in the trail log line
   at strategy.py:1454.

2. **The weekly report — this is the real prerequisite.** The counter is
   **per-process, resets on every restart, and the weekly report does not read
   it.** `=== PROFIT FLOOR ANALYSIS ===` is built by `_profit_floor_stats()`
   (performance_analyzer.py:879) from **ledger trip attribution**, so splitting
   the counter alone changes nothing the report can see. Partition
   `_profit_floor_stats` and `_profit_floor_lines` by direction instead. Trips
   already carry `direction` (performance_analyzer.py:331/390/494), so this needs
   no new plumbing — but note it splits an already-small sample, and the section
   already withholds a verdict below 3 floor-caused exits
   (`MIN_FLOOR_TRIPS_FOR_VERDICT`). Decide whether that threshold applies per
   direction or to the combined set before splitting, or the report will go
   silent on both halves.

**Prerequisite for the prerequisite:** there are currently **zero** attributed
floor exits in the ledger (16 stop exits predate the ladder). Splitting a
statistic that has no observations yet buys nothing today — build this when the
first floor-caused exits start landing, and before anyone argues from short-side
floor numbers. Same control-group problem as the `SHORT_MAX_ATR_PCT` and SPY
trend confirmation entries above.

---

## MOMENTUM LATCH FALSE RECONSTRUCTION

**Observed (2026-08-17):** MOMENTUM LATCH
RECONSTRUCTED fires for SNDK despite
USE_MOMENTUM_ALIGNMENT = False. No latch
is ever written when alignment is off,
so reconstruction is always spurious.

**Direction:** Gate the latch check on
USE_MOMENTUM_ALIGNMENT. If False, skip
the reconstruction path entirely.

**Risk:** May block valid re-entries on
the "Blocking re-entry this rotation"
path even when alignment is disabled.

**Prerequisite:** Verify the re-entry
block actually fires before fixing;
if it's log-only, lower priority.

**Resolution (2026-08-17):** Log-only.
_momentum_entry_taken call site at
strategy.py:2134 is already gated by
USE_MOMENTUM_ALIGNMENT at line 2131 —
Python short-circuits, so the latch is
never read when alignment is off. No
re-entries are blocked. The real cost
is _latches_reconstructed incrementing
on routine cross entries, masking the
CRL/LII doubling defense. Fix when the
doubling defense needs a reliable signal.
Priority: LOW.

---

## Breakeven lock exit mislabeled as atr trail

**Observed (2026-08-18):** QQQ stopped out with the stop sitting exactly at
entry and the ATR trail 21 points below, but the log credited the trail:

```
2026-08-18 14:54:43  STOP-LOSS EXIT QQQ long x66 @ 717.12
  (stop=717.26 entry=717.26 water=734.41, held by atr trail, trail=696.00)
```

The lock armed 2026-08-13 (`BREAKEVEN LOCK QQQ long: floor raised to entry
717.26 (trail would be 694.55) — locks #1`) and held the stop for five
sessions. Realized 66 × (717.14 − 717.26) = **−$7.92**, after giving back
66 × (734.41 − 717.26) = **$1,131.90** of peak excursion.

**Root cause — structural, not an edge case.** `src` at strategy.py:1487
requires `apply_floor`, which is recomputed live from `breakeven_lock`
(strategy.py:1347-1353). That test needs `price > entry` for a long
(`price < entry` for a short) — the exact condition a breach of an
entry-anchored stop violates. The two are mutually exclusive, so a
breakeven-lock stop-out can **never** print "breakeven lock", in either
direction. Every one is credited to the trail, in both the strategy line and
the `trade:` line.

**Approach is NOT settled — do not implement a persisted
`breakeven_lock_active` flag without re-reading this.** A latched flag is the
thing strategy.py:1396-1404 explicitly rejects for the profit floor ("a
sticky `floor_active = True` would still be set long after the trail overtook
the rung, and every later stop-out would be mis-credited"). It fails on real
data: MSFT exited 2026-08-17 for **+$5,499.60** with its stop 92 points above
entry (`stop=489.94 entry=397.86 trail=489.94`) while the lock condition was
long since satisfied — a condition-set flag labels that winner "breakeven
lock". "When the lock first fires" is itself ambiguous between the condition
going true and the stop actually snapping to entry, and the two readings
mislabel differently.

**Direction — level identity for the label, recomputed every cycle:**

1. `stop_price == round(entry, 4)` → lock held. This is the same shape as
   `profit_floor_active` (strategy.py:1405-1408), not a new pattern. MSFT is
   the negative control: 489.94 ≠ 397.86, so it correctly stays "atr trail".
2. Guard with `and not crisis_floor`, as the arming site already does at
   strategy.py:1435. `floor_srcs` (strategy.py:1363-1368) collapses crisis
   floor and breakeven lock to the *same* level (`entry`), so the label cannot
   tell them apart without it. Latent only while `VIX_CRISIS_SHADOW = True`
   (config.py:550) — a silent regression the day that flips. A profit-floor
   rung can never collide, being strictly past entry (strategy.py:1366).
3. **Centralize.** This is the third site naming a stop's holding source
   (:1405, :1435, :1487). Extract one
   `_stop_source(rec, entry_r, crisis_floor, rung)` returning the label, and
   have both the log line and the `stop_attr` dict consume it, so the
   human-readable label and the machine attribution cannot disagree. They
   agree today only because both are wrong.
4. Counter `_breakeven_lock_exits`, separate from `_stop_exits` (:443) and
   `_breakeven_locks` (:459, which counts armings). Count *caused*, reusing
   the counterfactual at :1479-1483. Note `held ⇒ caused` almost always here:
   if the trail had overtaken entry, `stop_price != entry_r` and the label is
   already "atr trail". Unlike the ladder, this counter cannot inflate itself.
5. Ledger plumbing mirrors the ladder: `_STOP_ATTR_KEYS` (trade_logger.py:49)
   plus both ingest sites (performance_analyzer.py:341 and :534). New keys
   `breakeven_lock_held`, `lock_caused_exit`, `stop_at_exit`, `water_at_exit`.
   `absent ≠ False` applies (trade_logger.py:44-47) — pre-fix exits read
   unknown, never "lock was inactive".
6. **Non-stop exit paths must carry the stop attribution too.** Surfaced by CRWV
   2026-08-28 (docs/profit-floor-analysis.md, "Arming 2"): the profit floor
   armed, moved the stop 9.37 points and raised a broker GTC, then the Friday
   weekend-gap rule closed the position. The friday-close `_log_exit_trade` call
   (strategy.py:2427) passes no floor/lock fields, so the event stores
   `profit_floor_active: null` — and by `absent ≠ False` that is *unknown*, not
   *inactive*, yet it reads identically to a trade that never had a floor.
   Then `_profit_floor_stats` filters `exit_reason == "stop"`
   (performance_analyzer.py:935), dropping the trip from `floor_active` as well
   as from `floor_caused`. Net effect: **an arming that ends in a non-stop exit
   is invisible**, so engagement is understated while causation stays correct —
   the report said "floor active: 1 of 4" after a session with an arming in it.
   Every non-stop exit site that can close a stop-managed position has this hole
   (friday close :2427, EMA signal exits :2372/:2399/:2442, profit take :1860,
   stale-symbol :2969). Do NOT fix by widening the `== "stop"` filter — that
   would credit the ladder for exits it did not cause, which is the error this
   whole section exists to remove. Fix by attaching attribution at every exit
   site (the `_stop_source` helper in point 3 is the natural home, per
   "centralize dispatched logic on day one") and by splitting the report's
   populations into *armed* vs *caused*, denominated over all exits rather than
   stop exits only.

**Report section**, mirroring `_profit_floor_stats`/`_profit_floor_lines`
(performance_analyzer.py:899/963) so the numbers land in the JSON too. Three
corrections to the obvious spec:

- "Times lock armed" **cannot come from `_breakeven_locks`** — it is
  per-process and resets on restart. It read 0 all day on 2026-08-18 despite
  the lock demonstrably holding QQQ since 08-13. Aggregate `BREAKEVEN LOCK`
  lines across `bot.log*` plus the `.gz` archives instead. (Same
  counter-vs-ledger trap as "Split profit floor measurement by direction".)
- "Avg gain locked" is **≈ $0 by construction** — the lock floors at *entry*,
  so a lock-caused exit books roughly zero minus slippage. Reporting its mean
  is noise dressed as a metric.
- The number that decides the feature is **peak given back** (`water − entry`
  at exit). `high_water`/`low_water` appear **nowhere** in the ledger today,
  which is why `water_at_exit` is in the key list above. Not recoverable for
  past trips — those must show as excluded, not as $0.
- Verdict logic must **invert** vs the ladder's: `realized > 0` is
  unreachable for this feature. Key on peak-given-back against realized — a
  lock that repeatedly scratches out of positions 1+ ATR in profit is HURTING
  even though it never books a loss. Reuse `MIN_FLOOR_TRIPS_FOR_VERDICT = 3`.

**"Eligible but never bound" is the statistic worth having, and it is free.**
Back ATR out of `water − trail` and test the arm threshold. Of the 4 stop
exits in the retained logs (Aug 8–18), 3 had the lock eligible — CRWV
(ATR 4.60, threshold 91.59, water 117.22) and MSFT (ATR 9.456, threshold
407.32, water 513.58) both went to the trail because the floor never bound;
only QQQ ever bound, and it scratched. *Bound then overtaken* has **zero**
instances. So spec this on eligibility, derivable at breach from `water`,
`entry`, `atr_at_entry` with no new persisted state.

**Persisted arming marker: decided against, for now.** It buys exactly one
report line, and that line's case has no observations while the case that
does have observations is free. Revisit if a second position ever binds. If
it is ever added, name it `breakeven_lock_armed_at` (ISO timestamp, set once,
never cleared) and use it **only** as report metadata, never for the label —
`_active` naming is what invites the sticky misuse.

**Reconcile, not backfill.** Historical exits carry the wrong label, so
re-derive rather than one-shot patch (stored values age relative to their
derivation logic). Feasible for post-08-13 exits: `atr_trail_at_exit` is
recorded, entry price is on the trip, and the stop level is parseable from
the notes (`trailing stop hit @ 717.26`). `water_at_exit` is unrecoverable
for past trips.

**Tests** — homes are test_critical_sink_and_attribution.py and
test_profit_floor.py: (a) the QQQ regression, entry 717.26 / stop 717.26 /
trail 696.00 / exit 717.12 → "breakeven lock"; (b) trail above entry
(`high_water > 755.67` on QQQ's 38.41 `mult_atr`) → "atr trail", the
latched-flag guard; (c) crisis floor with shadow off → "crisis floor";
(d) rung armed → "profit floor" wins; (e) absent keys stay unknown through
both ingest sites; (f) short-side symmetry for (a)-(c).

**Trading impact: none.** Attribution/observability only — no stop level, no
entry, and no exit changes. The lock behaves identically before and after.
That is why this was deferred on 2026-08-18 rather than shipped same-night.

Priority: MEDIUM. Blocks any honest verdict on whether the breakeven lock
earns its keep — it has 4 armings lifetime and its exits are currently
invisible, credited to the trail.

### STATUS 2026-08-19 — mostly SHIPPED, two items deliberately left

**Built.** `strategy._breakeven_reached` (water-based, no price clamp) and
`strategy._stop_source` (level identity, crisis-before-lock ordering), both
consumed by the log line and `stop_attr` so the two cannot disagree.
`_breakeven_lock_exits` counts *caused*, on confirmed exits only. Ledger keys
`breakeven_lock_held` / `lock_caused_exit` / `stop_at_exit` / `water_at_exit`
through `_STOP_ATTR_KEYS` and BOTH analyzer ingest sites. BREAKEVEN LOCK
ANALYSIS section with the inverted verdict (protected vs peak-given-back, never
realized P&L). 26 tests in `test_breakeven_lock_label.py`, including the QQQ
regression — verified to FAIL against the pre-fix `src` expression, not merely
to pass against the new one.

Two deviations from the direction above, both deliberate:

* Scratch detection is `SCRATCH_BAND_PCT` of notional, not an absolute dollar
  band. QQQ's −$7.92 across 66 shares is −$0.12 a share; any fixed band tight
  enough to mean something on a 10-share position misclassifies it.
* The verdict compares `principal_protected` against `peak_given_back` rather
  than peak-given-back against realized. Realized is ≈$0 by construction — this
  document says so itself — so it cannot be one side of a ratio. Protected-vs-
  surrendered is the same question with a non-degenerate denominator.

**NOT built — still open.**

1. **"Eligible but never bound".** The free statistic identified above, and on
   the retained logs the one with actual observations (3 of 4 stop exits
   eligible, only QQQ ever bound, *bound then overtaken* still zero). Needs a
   log-aggregation pass over `bot.log*` + `.gz`, not a strategy change, so it
   did not belong in this commit.
2. **Reconcile for pre-fix exits.** Every stop exit before 2026-08-19 carries
   the wrong label and is excluded from the new section rather than corrected.
   Re-derive from `atr_trail_at_exit` + entry + the stop level parsed out of the
   notes; `water_at_exit` stays unrecoverable, so reconciled trips can populate
   held/caused but never `peak_given_back` — they must remain excluded from the
   verdict rather than counted as zero given back.

Until (1) and (2) land the section reports on post-2026-08-19 exits only, and
will read "no attributed stop exits yet" until the next lock-held stop-out.

---

## FUTURES STOP PROTECTION

**Observed (2026-08-20):** futures positions have **no stop protection of any
kind**. No ATR trail, no profit floor, no broker-native GTC order. The only two
ways out are the EMA state exit and the quarterly roll. Confirmed in code:
`strategy._arm_stop_on_entry` is called only from the equity paths, and
`place_futures_order` explicitly *refuses* the stop path
(tradestation_client.py:485-488), so nothing arms a futures stop today.

**Evidence of need:** NQU26 peaked at **+$12,860 (2026-08-13)** and is now
giving it back. No mechanism locked any of it — the same "winners stop giving
back gains" failure the breakeven lock and profit floor ladder exist to catch on
the equity side.

### Four blockers, in priority order

**1. Tick rounding — unblocks (3) and (4).** `_build_order_body` formats both
trigger and limit prices as `f"{round(p, 2):.2f}"`
(tradestation_client.py:394-396). That is correct for equities and options and
**wrong for futures**: ES and NQ tick at 0.25, RTY at 0.10, so a 0.01-rounded
stop is an invalid price the broker either rejects or silently re-ticks. Fix:
per-root tick tables in `tradestation_client.py`, and lift the
`place_futures_order` refusal in the same change so the two can never disagree.

> **Correction to the original framing:** this unblocks futures *only*, not
> "equities and futures simultaneously". Broker-native stops for equities are
> already shipped and live — `ENABLE_BROKER_STOP_FLOOR = True` since
> 2026-08-10 (config.py:706), with confirmed GTC raises/cancels on 08-14 — and
> they are correctly ticked at 0.01. Nothing on the equity side is waiting on
> this.

**2. Stop file namespacing.** `STOP_PRICE_FILE = "data/stop_prices.json"` is a
single hardcoded path (config.py:282) shared by the equities and futures
processes, which are separate processes with separate locks. There is no file
locking, so concurrent writes are last-writer-wins. Fix: either split per
process (`stop_prices_equities.json` / `stop_prices_futures.json`) or add file
locking.

> **Latent, not active.** Because nothing arms futures stops today, the futures
> process never writes this file, so no records are being lost right now. This
> is a **prerequisite of (3)**, not an independent live bug — it must land
> *before* the first futures stop is armed, not after.

**3. ATR instrument decision.** Signals come off `@ES` (the continuous
contract); the position is in `ESU26` (a dated contract). ATR and entry price
therefore sit on different price bases, and the gap between them jumps at every
roll. Because the equity machinery persists `atr_at_entry` and `atr_mult` at
entry and trails at that fixed width for the position's whole life, a
basis-mismatched ATR is baked in permanently rather than self-correcting.
**Decide the basis before implementing** — continuous for ATR with dated for
price is the tempting default and is exactly the mismatch.

**4. Rung calibration.** The equity percentage rungs are not transferable. 1%
on ES is ~$3,200 of notional against a much smaller figure on a 5%-of-equity
stock position, so the long/short ladders (`PROFIT_FLOOR_STEPS_LONG` first rung
+15%, `PROFIT_FLOOR_STEPS_SHORT` +8%) mean something entirely different per
contract. A futures-specific ladder is needed — and per the asymmetric-ladder
precedent, do not derive it arithmetically from the equity values.

### Suggested sequence

1. Tick rounding (unblocks broker GTC for futures).
2. Stop file namespacing.
3. Pilot the ATR trail on **one root only** (ES).
4. Broker-native GTC for futures.
5. Profit floor ladder, futures-specific percentages.

**Priority: HIGH** — futures currently carry no gap protection at all, and
unlike equities they trade nearly around the clock, so "gap" here includes the
Sunday open and every session break.

**Prerequisite: NONE for tick rounding.** It is self-contained in
`tradestation_client.py` and testable without arming anything (see
`test_futures_orders.py`).

---

## Direction-aware tick rounding

**Observed (2026-08-21, 5135c33):** `_round_to_tick` uses ROUND_HALF_UP
(nearest tick), but protective stops should round AWAY from the market:

  * Long stop (rests below market)  -> round DOWN
  * Short stop (rests above market) -> round UP

Nearest-tick can move a stop half a tick toward the market ($6.25/contract on
ES, $6.25 on NQ, $5.00 on RTY), tightening protection slightly. The error is
bounded at half a tick and is as likely to loosen as tighten, so it is a
correctness tidy-up rather than a live risk.

**Direction:** Thread position direction into `_build_order_body` and select
ROUND_FLOOR (long) or ROUND_CEILING (short). ~10 lines.

Note the direction is NOT the same as `trade_action`, which `_build_order_body`
already receives. A protective stop for a LONG is a SELL, and for a SHORT is a
BUY — so the mapping inverts relative to the obvious reading, and getting it
backwards would tighten every stop by up to a tick instead of loosening it. For
equities the actions disambiguate (SELL vs BUYTOCOVER); for futures they do not,
because _FUTURES_ACTIONS collapses both to plain BUY/SELL. So futures need the
position side passed explicitly, not inferred from the action.

**Prerequisite:** None (standalone fix).
**Priority:** LOW (half-tick = $6.25 on ES), but do it before futures stops are
actually armed — `strategy._arm_stop_on_entry` is still equity-only, so no
futures stop rests at a rounded price yet and the bug is currently latent.

**Also outstanding from 5135c33:** the tick-rounding code itself has no
committed tests. It was verified interactively (ES/NQ/RTY/equity/unknown-root,
body build, stop_price forwarding, UnknownFuturesTick) but none of that landed
in `test_futures_orders.py`. Worth adding, especially the ESTC-style
prefix-collision case, which is the regression most likely to be reintroduced by
someone "simplifying" the regex back to a slice.

---

## Futures profit floor: % rungs unreachable

**Status: PARTLY DONE (2026-08-31).** The water-based floor specced in "Build
sequence" below SHIPPED as `72c1aa2` and supersedes the *need* for dollar rungs —
Options A and B are therefore **obsolete, not done**. The % rungs themselves are
still structurally unreachable on futures, which is what keeps this item open.
Follow-up: "Monitor K=0.5 effect on TSLA" at the end of this section — RESOLVED
2026-09-01, K raised to 0.75.

**Observed (2026-08-24):** The profit-floor ladder thresholds are percentages of
ENTRY PRICE, and `PROFIT_FLOOR_STEPS_LONG`'s first rung is +15%. NQU26's entire
peak run was **+2.66% of price = +$15,695** on ~$44k of margin. No rung can ever
arm on an index future: leverage produces large dollar P&L at tiny percentage
moves, so a ladder keyed on percent-of-entry is structurally dead here. Same trap
applies to anything else keyed on percent-of-entry — `PROFIT_TAKE_PCT` (+12%) is
equally unreachable.

This is why futures currently scratch at entry instead of banking a gain: with the
ladder inert, the breakeven lock is the only floor, and it locks at exactly entry.
Measured against the Aug 16-24 NQU26 tape, every ATR multiple from 1.5x to 3.0x
produced the identical $0 outcome for that reason.

**Direction:** dollar-based rungs, or per-root percentages ~30x tighter.

  Option A — global +0.5% trigger -> lock 0.3%. Per contract that is:
      NQ   trigger 147.7 pts = $2,955   lock  88.6 pts = $1,773
      ES   trigger  38.2 pts = $1,909   lock  22.9 pts = $1,145
      RTY  trigger  15.3 pts = $  763   lock   9.2 pts = $  458

  Option B — dollar thresholds per root:
      NQ   +$5,000 -> lock $3,000
      ES   +$2,000 -> lock $1,200
      RTY  +$1,000 -> lock   $600

  These two converge, which is the argument for B: expressed as percentages,
  B is 0.85%/0.51% on NQ, 0.52%/0.31% on ES, 0.66%/0.39% on RTY — i.e. Option A's
  shape, but calibrated per root instead of one number that means a different
  dollar amount on each contract. Prefer B.

  On the actual NQU26 run, Option B's single rung would have locked ~+$3,000
  instead of the $0 the breakeven lock produced.

**Prerequisite:** point value per root. That is `config.FUTURES_SPECS[root]
["multiplier"]` (ES 50, NQ 20, RTY 50) — NOT `_arm_stop_on_entry`, which never
touches multipliers; it works purely in price space and is unaware a contract
multiplier exists. Any dollar-denominated rung needs the multiplier threaded into
`_profit_floor`, which today also works purely in price space and takes only
`rec` and `price`. That plumbing is the actual work here, not the thresholds.

**Priority:** MEDIUM. Gap risk is already covered by the resting GTC floors as of
99c117a; this is about profit capture on trending futures moves, which is where
the $27,485 NQU26 give-back actually went.

### The lock arithmetic proves the fix is a trailing floor, not a tuned multiplier

Added 2026-08-27, after the first two futures breakeven locks armed live (NQU26
08-26, override 991.03 pts; ESU26 08-27, override 140.56 pts).

The lock arithmetic proves the fix. `lock = entry` protects **$0** by
construction, and it *always* dominates the trail over the whole 1–3 ATR range:

    lock  = entry
    trail = entry + run − mult·ATR                      (long; mirror for short)
    lock binds ⟺ run < mult·ATR        lock arms ⟺ run ≥ BREAKEVEN_LOCK_ATR·ATR

With `BREAKEVEN_LOCK_ATR = 1.0` and a 3.0x futures trail, the lock strictly
dominates for every excursion in **[1·ATR, 3·ATR)** — a 3x-wide window that a
position must cross *before* the trail can bind at all. So the fix is **NOT
multiplier tuning** but a water-based trailing floor:

    floor = max(entry, water − k·ATR)                   (long)
    floor = min(entry, water + k·ATR)                   (short)

i.e. the futures profit floor expressed as a **trailing** mechanism rather than a
ladder. The `max(entry, …)` keeps the breakeven lock as the hard lower bound, so
this is strictly an improvement on the current behaviour, never a loosening.

**The dollar-based rung proposal above (Option B) is a step-function
approximation of this** — same shape, quantised to one or two fixed thresholds.
That is an argument for building the trailing form directly: it needs no
per-root threshold table, and it inherits the ATR normalisation that Option A was
reaching for with its per-root percentages.

**k is the only knob, and it is bounded by the arming window.** The floor clears
entry only when `run > k·ATR`, so `k` must sit below the run being captured —
which is exactly the same 1-ATR neighbourhood the lock arms in. Measured against
the live 08-27 state (`data/stop_prices.futures.json`, multipliers ES/NQ/RTY =
50/20/50):

| leg | run/ATR | k=0.25 | k=0.5 | k=0.75 | k=1.0 |
|---|---|---|---|---|---|
| ESU26 | 1.14 | $3,155 | $2,272 | $1,390 | $507 |
| NQU26 | 1.16 | $9,124 | $6,628 | $4,132 | $1,636 |
| RTYU26 | 0.55 | $567 | $90 | below entry | below entry |

k≈0.5 lands in Option B's intended range on ES and NQ ($2,272 vs $1,200 target;
$6,628 vs $3,000) while capturing almost nothing on RTY, whose run never reached
1 ATR — the same reason RTY is the one leg that has not locked. Note this table
is the *current* excursion on three open legs in one direction and one regime;
it sizes the mechanism, it does not fit k. Do not tune k on it.

**Not futures-specific.** The identical window is why AMD (07-29), PLTR (08-04)
and QQQ (08-13→hit 08-18) all scratched on equities with $1.1k–$5.4k of open gain
unprotected. If this is built, build it for both asset classes; the futures
`multiplier` plumbing described above is only needed for the *dollar-denominated*
variant, and a water-based floor in ATR space does not need it at all — which
removes this item's stated prerequisite.

Cross-refs: "Breakeven lock exit mislabeled as atr trail" (line 563) — a
water-based floor makes that attribution bug worse, because the floor would sit
above entry and the `stop == entry` tell used to detect lock-caused exits stops
working. Fix attribution first, or at least in the same change.

#### Build sequence (specced 2026-08-27, build target: week of 2026-08-31)

Spec is settled: `floor = max(entry, water − k·atr_at_entry)`, `k ≈ 0.5`.
**Blocked on the attribution fix at line 563 — build that first.** All line
numbers below verified against `strategy.py` on 2026-08-27.

1. **`_stop_source()` (strategy.py:1477)** — teach it a floor type that sits
   *above* entry, not at it. Today it returns `"breakeven lock"` from
   `at_entry and breakeven_reached`; a water floor is neither `at_entry` nor the
   ladder, so it would fall through and be credited to `"atr trail"` — the exact
   line-563 bug, one level worse. This is why attribution lands first.

2. **New `_check_water_floor(rec, price)`** — returns the candidate floor;
   apply when `floor > current_stop` (long; `<` for short), then raise the
   resting broker GTC. **Reuse, do not reimplement:** `_floor_price(entry, atr,
   mult, direction)` (:838) already does entry ± mult·ATR in price space and
   should be generalised to take an anchor (entry *or* water) rather than
   copied; `_maybe_raise_broker_floor()` (:1369) already owns the GTC-raise path
   including the cancel/replace ordering. Signature should mirror
   `_profit_floor(rec, price)` (:1333) so both floors compose the same way.
   (Two call sites for the same logic is the pattern that has bitten this repo
   3× — put the anchor-generalised helper in place on day one.)

3. **Config:** `ENABLE_WATER_FLOOR = True`, `WATER_FLOOR_K = 0.75` (shipped at
   0.5; raised 2026-09-01, see the resolved section below), alongside
   `ENABLE_BREAKEVEN_LOCK` / `BREAKEVEN_LOCK_ATR` (config.py:573-574).

4. **Wire into `_check_and_trail_stop()` (strategy.py:1511) — equities AND
   futures.** Single code path, no `_IS_FUTURES` branch: the mechanism is pure
   price space, so it needs no per-root multiplier and no split.

5. **Counter + report line.** A new safety net needs its own counter or we
   cannot tell later whether it earned its keep — follow `_bump_profit_floor()`
   (:487) and the `_profit_floors_long/short` split already specced at line 482,
   i.e. direction-split from the start rather than retrofitted.

6. **Tests:** the three equities regressions — AMD 07-29 (short, $5,445 open at
   the low, protected $0), PLTR 08-04 (long, $3,525 open, realised −$6),
   QQQ 08-13→08-18 (long, $1,131.90 peak, realised −$7.92) — plus the two
   futures cases ESU26 and NQU26. Each asserts the floor binds *above* entry and
   that `_stop_source` attributes the exit to the water floor, not the trail.
   Add RTYU26 as the negative case: 0.55 ATR run, floor must stay at entry and
   the water floor must NOT arm. Every positive case needs its paired negative.

7. **Then update this item's status to PARTLY DONE** — the % rungs are still
   unreachable; the water floor supersedes the *need* for dollar rungs (Option A
   and B above become obsolete, not done).

**Caveat on the $2,272 / $6,628 figures.** Those are open unrealised on three
live legs, and both ES and NQ carry **estimated** adopted entries (true fills
7,635.75 and 29,546.50 — see the futures-stops note). Since the floor is anchored
on `entry`, a wrong entry puts the floor at the wrong price and those dollar
figures inherit the error. They size the mechanism; they are not a forecast, and
they are not a k-fit.

---

## Monitor K=0.5 effect on TSLA — first live test of the water floor

**Status: RESOLVED 2026-09-01. K=0.50 was wrong. Raised to K=0.75.** Water floor
shipped 2026-08-31 (`72c1aa2`); the answer arrived on day one, three days before
the 09-04 deadline. Build sequence above is DONE.

### Resolution — the shaken-out branch was met exactly

**TSLA long x133 @ entry 359.92, `atr_at_entry` 13.51:**

| fact | value |
|---|---|
| Run at arming | **0.64 ATR** — barely past the 0.50 threshold |
| Floor armed | 13:30:08, stop 334.77 → 361.79 (0.50 ATR behind water 368.55) |
| Exited | 13:31:15 — **67 seconds later**, one poll cycle |
| Exit print / fill | 359.10 / **359.17** (slippage −0.0750) |
| Realized | **−$99.75** |
| Peak unrealized | **+$1,147.79** (water 368.55, 8.63/share) |
| Trail at exit | **334.77** — 26.42 away, never in play |
| Gap through stop | **2.62** — stop 361.79, filled 359.17 |

The decision rule below asked for the exit *reason*, not the P&L sign. The reason
is: a floor armed 0.14 ATR above the market on a run that had barely established
itself, and the next minute took it out. The trail was irrelevant — this exit
exists *only* because the floor armed. A +$1.1k open position was converted into a
realized loss. That is the failure case as written.

**K=0.75 verdict on this case: would NOT have armed.** Arming needs `run > K`, and
the run was 0.64 ATR. TSLA stays on the trail and stays open. ✅

### The opposite case, same day — NQU26 says K=0.50 was RIGHT there

**NQU26 long x1 @ entry 29118.75:**

| fact | value |
|---|---|
| Run at arming | **1.37 ATR** — well established |
| Floor armed | 06:06:37, stop 29118.75 → 29555.16 |
| Exited | 06:13:46, fill **29544.75** (slippage +5.00) |
| Realized | **+$8,520** (426.00 pts × $20/pt) |
| Trail | **28,307.22** — could never have fired |

The floor was the **only** path to that realization. Keeping K=0.50 for its own
sake was never the question; the question was whether 0.50 arms too early.

**At K=0.75 NQU26 still arms** (1.37 > 0.75) and still banks the run. The knob
separates the two cases cleanly — which is the whole argument for moving it.

### The verdict

The split is **run length at arming**, not the instrument and not the direction:

| | |
|---|---|
| K=0.50 | Arms on runs that have barely established → shaken out by normal volatility |
| K=0.75 | Arms only on more established runs → fewer noise exits, still captures what the trail structurally cannot |

Cost, stated plainly: this raises the give-back allowance on **every** winner by
0.25·ATR. K is the trailing-stop width for every profitable position (see the
REPLACES-THE-TRAIL arithmetic below), so this is not a free fix — it is buying
arming discipline with give-back. n=2. Re-examine on the next 2–3 armings.

Note K=0.75 < `BREAKEVEN_LOCK_ATR` = 1.0 still holds, so the floor continues to
arm before the lock and continues to supersede it.

### ESU26 note — CORRECTION to the obvious reading

ESU26 exited the same day at entry as `lock exits #1`, the **first
breakeven-lock-caused exit ever**: 107.25 points of peak given back to a scratch.
The tempting explanation is "the water floor never armed because the run never
reached 0.50 ATR." **That is wrong, and the log disproves it:**

* `STOP BOOTSTRAP ESU26` (2026-08-24) records `atr=70.60 mult=3.0x`. The trail
  7569.69 confirms it: 7781.50 − 3.0 × 70.60 = 7569.69. ✓
* Run from entry 7674.25 to water 7781.50 = 107.25 pts = **1.52 ATR** — past the
  0.50 floor threshold *and* past the 1.0 lock threshold. The lock arming at all
  is itself proof the run exceeded 0.50 ATR.

The floor was blocked by **Guard 2 (never arm through the market)**, not by the
threshold. Its level would have been 7781.50 − 0.50 × 70.60 = **7746.20**, already
above the market when the floor shipped retroactively on 08-31, and the water never
advanced again to rescue it. This is exactly the banner's "the lock now only binds
on positions where the floor is blocked."

**K=0.75 does not fix ESU26**: the floor would sit at 7728.55, still above the
market, still guard-blocked. A general side effect worth tracking, though — raising
K lands the floor *lower*, so Guard 2 blocks it less often on retroactive applies.

Two further caveats on this leg:

* ESU26 carries an **ESTIMATED adopted entry** — bootstrap anchored on live price
  7674.25 and explicitly refused the margin `cost_basis`. The log states outright
  that "profit floor, breakeven lock and exit P&L are all off the true fill for
  this record." The −$412.50 implied by fill 7666.00 is therefore soft. Per-leg
  ledger corrections are signed and per-leg; do not aggregate this naively.
* This is the **fourth consecutive scratch-on-a-winner** with the same signature
  (AMD 07-29, PLTR 08-04, QQQ 08-13, now ESU26). **That structural problem is
  still open** — the water floor was supposed to close it and could not here,
  because a guard-blocked floor falls back to exactly the lock behavior the floor
  was built to replace. Raising K does not address it.

---

**The original decision rule, set before seeing any outcome (kept for the record):**

* If TSLA is **shaken out on normal volatility** in the first week — stopped at
  the floor while the trend is still intact (EMA9 > EMA21 at the exit, and price
  recovers above the exit within a few sessions) — **raise K to 0.75**.
* If the floor **captures a significant gain** it would otherwise have given back
  — exits near the high, or holds while price chops and then resumes — **keep
  K=0.5**.
* Judge on the *reason* for the exit, not the P&L sign. A small profit banked by
  an exit that killed a live trend is the failure case, not a success.

**Why TSLA is the test.** It is the only position the floor moved at deploy:
stop **334.773 → 361.7946** (+27.02/share, $3,593.87 of stop movement across 133
shares), from 25.15 points BELOW entry 359.92 (protecting nothing) to 1.87 points
above it. Room to price 368.19 is **6.40 points = 0.47 ATR**, down from ~9%. A
normal day's range can take it out. GOOGL is unaffected (its +2% rung at 342.3024
is already tighter than the water floor at 342.8738 — the composition picking the
most protective source, working as designed), and ESU26/NQU26 are guard-blocked.

**K=0.5 IS NOT A MINOR KNOB — it is the trailing-stop width for every profitable
position.** Floor and trail share the same water anchor and config validates
`0 < K < STOP_LOSS_ATR_MULT`, so an armed floor never loses to the trail. Once a
position is more than K·ATR past entry its effective trail is K·ATR wide, at every
excursion. A position up 10 ATR exits on a 0.5 ATR retrace. It also supersedes the
breakeven lock (floor arms at 0.5 ATR, lock at 1.0, floor strictly more protective
once armed), so **expect `locks #N` → 0 on new positions**; a non-zero lock count
now means the floor was guard-blocked.

The trade-off, stated plainly:

| | |
|---|---|
| ✅ | Captures gains faster — closes the give-back window the lock could not |
| ❌ | More exits on normal volatility |
| ❌ | May shake out strong trends |

And the thing to keep in view: **the same K=0.5 that looked good sizing ES/NQ
($2,272/$6,628 on legs that had run 1.1-1.5 ATR) now applies to EVERY profitable
position at EVERY excursion.** Those two legs were selected by having run far;
they are not a sample of the population K governs.

**The K ladder** (measured, `entry=100, atr=10`; floor clears entry at `run > K`):

| K | character | floor at 1.5 ATR run | notes |
|---|---|---|---|
| 0.25 | very tight | 112.50 | exits on noise |
| 0.5 | tight, captures fast | 110.00 | 2026-08-31 → 2026-09-01; shook out TSLA |
| **0.75** | **looser, more room** | **107.50** | **current** — the fallback, taken 2026-09-01 |
| 1.0 | most conservative useful | 105.00 | see correction below |

**Correction to a plausible-sounding claim: K=1.0 is NOT "redundant with the
lock".** It *ties* the lock at exactly a 1.0 ATR run (both land on entry) and
strictly dominates it beyond — at a 1.5 ATR run the floor is 105.00 against the
lock's 100.00. What K=1.0 actually does is align the floor's arming threshold with
`BREAKEVEN_LOCK_ATR`, so the floor never arms earlier than the lock would have.
That makes it the most conservative setting that still improves on the lock, not a
no-op. K ≥ `STOP_LOSS_ATR_MULT` is the setting that makes the feature inert, and
the config validator rejects it.

Cross-refs: `config.py` WATER_FLOOR_K (the same trade-off is documented at the
knob), `test_water_floor.py::test_armed_water_floor_ALWAYS_dominates_the_atr_trail`
(pins the dominance), "Breakeven lock exit mislabeled as atr trail" (line 563)
point 6 — still OPEN, so a floor armed on a position closed by the friday rule or
an EMA exit stays invisible to the weekly report; engagement understated,
causation correct.
