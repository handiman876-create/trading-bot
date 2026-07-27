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
