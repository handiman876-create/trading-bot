# Profit floor — observed behavior

Running log of what the profit-floor ladder actually did in production, as
distinct from what it was designed to do. The weekly report's
`=== PROFIT FLOOR ANALYSIS ===` section carries the running counters; this file
carries the per-trip stories the counters can't hold.

The report withholds a verdict below `MIN_FLOOR_TRIPS_FOR_VERDICT` = 3
floor-caused exits. **We are at 1.** Nothing below is a validated result.

Count armings and exits separately. The ladder has **armed twice** (AVGO
2026-08-26, CRWV 2026-08-28) and **caused one exit** (AVGO). Only the second
number moves the verdict, and only the second number is in the report — see
"The arming that no counter sees" below for why that is a measurement gap.

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

## Arming 2 — CRWV short, 2026-08-28 (intraday)

**The second micro-rung arming, and explicitly NOT a floor-caused exit.** The
floor tightened the stop by 9.37 points and then a different mechanism — the
Friday weekend-gap close — fired first. The ladder's trip count stays at **1**.

| | |
|---|---|
| Entry | 2026-08-28 10:30:59 EDT, `SELL_SHORT` 556 @ **86.01** (fill; signal 86.07, slippage +0.06) |
| Entry ATR | 6.82 = **7.93%** of entry, trail multiple **1.50x** (regime `risk_on` × high vol band) |
| Best price | low_water **84.24** → +2.058%, polled MFE **$984.12** (1.77 × 556) |
| Floor armed | 11:52:45 EDT — +2% rung, stop 94.52 → **85.15** (`short floors #1`) |
| Broker GTC | cancel 969230209 → raise 98.28 → **87.19** (2.04 behind the rung), order 969263437 (`cancels #2` / `raises #1`) |
| Exit | 15:45:25 EDT, `BUY_TO_COVER` @ **84.17** (fill; signal 84.14, slippage +0.03) |
| Exit cause | **`friday_short_close`** — gain 2.17% > 0.50% threshold, NOT the floor |
| Realized | **+$1,023.04** (+2.139%), order 969317271 |
| ATR trail at exit | **94.52** — 10.35 away, would NOT have fired |
| Floor at exit | 85.15 — sat **0.98 above** the exit price, never touched |
| Breakeven lock | never armed (needs 79.19 = entry − 1 ATR; best was 84.24) |

Ledger record carries `exit_reason: "friday_short_close"` and
`floor_caused_exit: null` — see the gap section below; the null is not a "no".

### Three things this trip confirms

1. **The 5-cent arming margin is not a one-off.** The +2% rung needs price
   ≤ 84.2898 (86.01 × 0.98). Best low_water was **84.24** — it cleared by
   **$0.0498**. AVGO cleared its trigger by $0.0616. Both armings in the ladder's
   history have hung on under 7 cents. Whatever else is true, this ladder's
   arming rate is dominated by tape noise at the trigger, not by trade quality.
2. **The "instant scratch" failure mode still has not appeared.** The rung armed
   at 11:52 and the position was not closed by it 3h 53m later, having improved
   from 84.24 to a 84.17 fill. Here 1pp of lock is only **0.126 ATR** (0.8601 /
   6.82) — *tighter* than AVGO's 0.22 ATR, so this was the more hostile test of
   the geometry, and it still held. Two for two.
3. **The trail remains inert at 1.5x on a 7.9%-of-entry ATR.** It sat 10.35 away
   at exit and never came within 9 points of the floor. Consistent with the
   futures finding: above ~1.5x the trail is not a live mechanism.

### Where this trip differs from AVGO: MFE capture

AVGO captured 36% of MFE and that drove the whole "is the 1pp gap too tight"
question. CRWV captured **104%** — realized $1,023.04 against a polled MFE of
$984.12, because the cover filled at 84.17, *below* the best price the 60-second
poll ever recorded (84.24).

That over-100% number is not the ladder outperforming. It is a measurement
artifact worth naming: **poll-sampled MFE is a lower bound on true MFE, not a
ceiling.** The 60s poll can miss the actual low entirely. So AVGO's "36% of MFE"
is itself an overestimate of capture — true MFE was at least as good as polled,
probably better, making the real capture fraction *lower* than 36%. This
strengthens rather than weakens the open question below, and it means MFE-based
capture ratios should never be quoted to more than one significant figure.

Note also that the exit here was not the ladder's decision, so this trip says
nothing about what the 1pp gap does when left to run. It is not a data point on
the capture question — only on arming and on holding after arm.

### The arming that no counter sees

`_profit_floor_stats` reads `closed_trips` filtered to
`exit_reason == "stop"` (`performance_analyzer.py:935`). CRWV exits as
`friday_short_close`, so it is excluded from **every** population in that
report — `floor_active` included. The weekly report therefore still says
"trades with floor active: 1 of 4" after a session in which the floor armed,
moved a stop 9.37 points, and raised a broker GTC order.

Compounding it: `strategy.py:2427`'s friday-close `_log_exit_trade` call does not
pass the floor fields at all (unlike the stop path), so the event carries
`profit_floor_active: null`. **A `null` here is indistinguishable from "no floor
existed"** — which is why this doc, not the ledger, is currently the only record
that the floor was armed on CRWV.

The consequence is directional and it is the bad direction: armings that end in
a non-stop exit are invisible, so the report systematically **understates how
often the ladder engages** while correctly counting when it exits. Anyone reading
`floor_active: 1 of 4` would conclude the ladder rarely engages. It has engaged
on 2 of the last 2 profitable shorts.

This is a reporting gap, not a trading bug — no money moved wrongly. It is
folded into the breakeven-lock attribution fix already scoped in `backlog.md`,
which is the prerequisite gate for the water-based floor; fixing exit attribution
piecemeal here would collide with it.

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

1. ~~**Split the `PROFIT FLOOR` counter long vs short.**~~ **DONE** — `41290a5`.
   First live read of the split counter is CRWV 2026-08-28 (`short floors #1`).
2. Get to 3+ floor-caused exits so the report stops withholding a verdict.
   **Still at 1.** CRWV armed but did not exit on the floor, so it does not
   count — and note that armings ending in a non-stop exit are not merely
   uncounted, they are unrecorded (see "The arming that no counter sees").
   At 2 armings per 2 profitable shorts and 1 exit in 2 armings, the binding
   constraint on reaching n=3 is how often a *floor* rather than some other
   mechanism gets to close the trade.
3. Then compare realized-on-caused against MFE-at-arm-time, per direction. The
   report deliberately does not compute dollar impact (true impact is what price
   did after the exit, which the ledger does not carry) — this comparison is the
   honest substitute. Quote the ratio to **one significant figure only**: CRWV
   showed polled MFE can be beaten by the fill, so these ratios carry poll-
   sampling error in the optimistic direction.

Until then: **the short ladder worked, once.** AVGO remains the only trade the
floor has ever closed green on its own. CRWV is not a second instance of that —
it is a second instance of the ladder *arming*, which is a weaker claim and the
one the counters were built to keep separate.
