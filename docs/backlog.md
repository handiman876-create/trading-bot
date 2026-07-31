# Backlog

Deferred work and known limitations that are observed but not actively
prioritized. Each entry: what was seen, where, and the proposed direction.

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
