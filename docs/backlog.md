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
