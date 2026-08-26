# Profit floor — observed behavior

Running log of what the profit-floor ladder actually did in production, as
distinct from what it was designed to do. The weekly report's
`=== PROFIT FLOOR ANALYSIS ===` section carries the running counters; this file
carries the per-trip stories the counters can't hold.

The report withholds a verdict below `MIN_FLOOR_TRIPS_FOR_VERDICT` = 3
floor-caused exits. **We are at 1.** Nothing below is a validated result.

---

## Trip 1 — AVGO short, 2026-08-19 → 2026-08-26

**The first exit ever CAUSED by the profit floor.**

| | |
|---|---|
| Entry | 2026-08-19 10:30:38 EDT, `SELL_SHORT` 133 @ **359.42** (fill; signal 359.61, slippage +0.19) |
| Entry ATR | 16.42 = 4.57% of entry, trail multiple 2.50x |
| Best price | 350.2999 → **+2.538%**, MFE **$1,212.97** (9.1201 × 133) |
| Floor armed | 2026-08-26 10:15:45 EDT — +2% rung, stop 393.28 → **355.83** (`floors #1`) |
| Broker GTC | cancel 967927132 → place 968838289 @ 364.03, same second (`cancels #1` / `raises #1`) |
| Exit | 2026-08-26 15:52:38 EDT, `BUY_TO_COVER` @ **356.13** (fill; signal 356.095, slippage +0.035) |
| Realized | **+$437.57** (+0.92%), order 968940051 |
| Captured | **36% of MFE** |
| ATR trail at exit | **391.34** — 35.21 away, would NOT have fired |
| Breakeven lock | never armed (needs 342.99 = entry − 1 ATR; best was 350.30) |

Ledger record carries `floor_caused_exit: true`, `profit_floor_active: true`,
`stop_at_exit: 355.8258`, `water_at_exit: 350.2999`. `exit_reason` buckets as
`"stop"` as designed.

### Why this trip is the load-bearing one

The floor was not merely *tighter* than the other two mechanisms — it was the
**only** mechanism anywhere near price. The ATR trail sat 35 points away on a
4.57%-of-entry ATR at 2.5x; on the observed tape it would not plausibly have
fired this month, and the position would still be open. The breakeven lock could
not arm at any point in the trade's life. Remove the ladder and this trade has no
exit.

So the +2% micro-rung is the entire reason this closed green. That is the
strongest single piece of evidence the ladder has produced, and it arrived on the
**shorts** side — the side with no demonstrated edge and the more aggressive
geometry.

### The micro-rung was predicted to do exactly this

`af8c960` (2026-08-21) added the two micro-rungs `+2%→1%` and `+5%→3%`, five
days before this fired. The config comment above `PROFIT_FLOOR_STEPS_SHORT`
already stated the case: the pre-existing `+8%→5%` first rung "sits too far out
to see" the give-back, and the micro-rungs "catch the brief profitable short
moves before reversal." AVGO's best gain was **2.538%** — it never came within
reach of the +8% rung. Without `af8c960` this trade has no floor.

`cd1ba66` also called the interaction correctly: on names with ATR/price > 2%
(AAPL, AVGO, GOOGL all qualify) the micro-rung **supersedes the breakeven lock**.
Observed here exactly — lock unreachable, micro-rung binding.

### Where the prediction was wrong

The config comment warns that at ~0.33 ATR of room "once the +2% rung arms the
position exits on the next small retrace." It did not. Timeline:

- 10:15 ET — rung arms at water 352.17
- price runs a further **1.87 in favor**, water 352.17 → 350.30
- 15:52 ET — exits on a retrace of **1.66% off the low** (350.30 → 356.10)

It held **5.5 hours** and let the position improve after arming. One trip proves
nothing about the distribution, but the "instant scratch" failure mode did not
appear on its first live test. Worth watching whether that holds: the geometry
argument for it is still sound, and the *reason* it survived is that AVGO's ATR
is 4.57% of entry, so 1pp of lock is 0.22 ATR — even tighter than the 0.33 ATR
the comment modelled on AAPL.

### The trigger armed by 6 cents

The +2% rung needs price ≤ 352.2316 (359.42 × 0.98). Low_water at the arming
poll was **352.17** — it cleared the trigger by **$0.0616**. A 6-cent-different
tape produces no floor, no exit, and an open position. Treat the entire finding
as resting on one poll.

---

## Open question: 36% MFE capture

The ladder locked +1% out of a +2.54% best move and the exit landed just above
the lock. `$4,723.64` of "room given up vs trail" is reported, but that is *not*
a loss — the trail sat there because it is loose, not because it was right.

The real observation is that the reachable ladder was one rung deep. The
`+5%→3%` rung never engaged because the trade never reached +5%. So the shape
that governed this trip was:

- trigger +2%, lock +1% → a **1pp gap**
- on this name, 1pp = **0.22 ATR**

Which is, in effect, *close now*. The argument that this is correct for shorts:
equities drift up, short gains reverse faster than long gains, and an overnight
gap against a short is unbounded. A 1pp gap accepts a small win in exchange for
never carrying a reversed short. The argument against: 36% MFE capture, repeated
across many trips, is a ceiling on the whole short book.

**Do not tune this on n=1.** What would make the question answerable:

1. **Split the `PROFIT FLOOR` counter long vs short.** Still unsplit as of
   2026-08-26 (`_profit_floors`, `strategy.py:472`), so the counter cannot tell
   you whether the short ladder specifically earns its keep — which is the only
   question here.
2. Get to 3+ floor-caused exits so the report stops withholding a verdict.
3. Then compare realized-on-caused against MFE-at-arm-time, per direction. The
   report deliberately does not compute dollar impact (true impact is what price
   did after the exit, which the ledger does not carry) — this comparison is the
   honest substitute.

Until then: **the short ladder worked.** It is the only mechanism that has ever
closed a trade green on its own.
