# Profit floor — observed behavior

Running log of what the floor mechanisms actually did in production, as distinct
from what they were designed to do. The weekly report's
`=== PROFIT FLOOR ANALYSIS ===` and `=== WATER FLOOR ANALYSIS ===` sections carry
the running counters; this file carries the per-trip stories the counters can't
hold.

**Two different features live here.** The **profit-floor ladder** (entry-anchored
static rungs, 2026-08-13) and the **water floor** (water-anchored trailing floor
at K·ATR, 2026-08-31). They are separate sources in `floor_srcs` with separate
counters, separate ledger keys and separate report sections, and conflating their
exit counts is the easiest mistake to make when reading this file. Each report
section withholds a verdict below 3 caused exits of **its own** kind.

Scoreboard as of 2026-09-02 — armings and caused exits counted separately,
because only the second number moves a verdict:

| mechanism | armed | caused an exit | caused exits, net |
|---|---|---|---|
| profit-floor ladder | 3 (AVGO, CRWV, GOOGL) | **1** (AVGO) | +$437.57 |
| water floor, equities | 2 (TSLA, GOOGL) | **2** (TSLA, GOOGL) | +$797.65 |
| water floor, futures | 1 (NQU26; ESU26 was guard-blocked, never armed) | **1** (NQU26) | **−$35.00** |

Neither feature has reached `MIN_*_TRIPS_FOR_VERDICT` = 3 in the ledger the
report reads, so **nothing below is a validated result.** The futures leg is not
in that ledger at all (see the note in Trip 2).

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

---

# Water floor — observed behavior

Separate feature, separate counters, separate verdict gate. Shipped `72c1aa2`
2026-08-31 at K=0.50, raised to **K=0.75** in `36e02a8` on 2026-09-01.

**Everything recorded below is K=0.50 evidence.** Both 09-01 restarts were after
the close, so the K raise first took effect at the 2026-09-02 open — and because
raising K can never loosen a floor that is already armed (the monotonic ratchet
at `strategy.py:1819` clamps it), GOOGL carried its K=0.50-era level to the exit.
**K=0.75 has still never armed anything.**

## Trip 2 — GOOGL short, 2026-08-13 → 2026-09-02

| | |
|---|---|
| Entry | 2026-08-13 10:53:51 EDT, `SELL_SHORT` 140 @ **345.76** (fill), order 967054314 |
| Entry ATR | 11.3921 = 3.30% of entry, trail multiple 2.50x |
| Best price | 333.04 → **+3.68%**, peak excursion **$1,780.80** (12.72 × 140) |
| Floor armed | 2026-09-01, four ratchet steps: 342.30 → 340.35 → 340.09 → 339.94 → **339.16** (`short water floors #1–4`) |
| Run at final arm | **1.08 ATR** behind water 333.46, K=0.50 |
| Exit | 2026-09-02 10:14:20 EDT, `BUY_TO_COVER` @ **339.35** (fill; signal 339.31, slippage +0.04), order 969759768 |
| Realized | **+$897.40** (+1.854%) |
| Captured | **50.4%** of peak excursion |
| ATR trail at exit | **361.52** — 22.17 away, would NOT have fired |
| Broker GTC | cancelled 969547362 (`cancels #1`) |

Ledger record carries `water_caused_exit: true`, `water_floor_active: true`,
`water_floor_price: 339.161`, `stop_at_exit: 339.161`, `water_at_exit: 333.04`,
and `floor_caused_exit: false` — the ladder's +2% rung (342.3024) existed but was
not the binding level. That handoff is `floor_srcs` working as designed: at
deploy the rung was tighter than the water floor (342.3024 vs 342.8738) and won;
as water improved the water floor overtook it and held the stop to the exit.

### Same shape as AVGO: the floor was the only mechanism near price

This is the second time a floor has closed a trade that nothing else would have
closed. The trail sat 22.17 points away on a 2.5x multiple and never came near;
the breakeven lock was superseded (the water floor arms at 0.75 ATR — 0.50 at the
time — against the lock's 1.00, and is strictly more protective once armed).
Remove the water floor and this position is **still open**, not closed at a loss.

**That distinction is the one number to be careful with.** It is tempting to
write "without the floor this would have been −$2,206" (the loss if price ran to
the 361.52 trail). It would be wrong in the same way the doc already refuses for
the ladder: `room given up vs trail` is **$3,130.30** here and that is not a
realized gain, because the trail sat there for being loose, not for being right.
Nothing in the ledger knows what price did after 10:14, so −$2,206 is a **bound
on exposure, not an estimated outcome.** The honest claim is the narrow one: on
the observed tape the floor was the only mechanism in play, and it banked
+$897.40.

### Capture ratio — the number that actually tunes K

Realized over peak excursion, and the direct descendant of the ladder's "36% of
MFE" question. Now computed by the report:

| trip | peak excursion | realized | captured |
|---|---|---|---|
| GOOGL 09-02 | $1,780.80 | +$897.40 | **50.4%** |
| TSLA 09-01 | $1,147.79 | −$99.75 | **−8.7%** |
| both | $2,928.59 | +$797.65 | **27.2%** |

Read the same caveat as AVGO's 36%: water is **poll-sampled**, so it is a lower
bound on the true excursion and every capture figure here is optimistic. One
significant figure at most.

The split is what motivated the K raise, and it is run length at arming, not
direction or instrument: TSLA armed at **0.64 ATR** and was stopped 67 seconds
later; GOOGL armed at **1.08 ATR** and held for a day. At K=0.75 TSLA never arms.

### Corrections to the obvious readings of this trip

Three claims that sound right and are not:

1. **It is not the first water-floor-caused exit.** TSLA (equities, 09-01,
   −$99.75) and NQU26 (futures, 09-01, −$35.00) both preceded it. GOOGL is the
   **third** water-floor-caused exit overall and the **second** on equities. What
   is genuinely first: it is the first water-floor-caused exit that was a
   **winner on equities**, and the first where the floor was demonstrably the
   only mechanism in play.
2. **It is not the third floor-caused exit overall.** Within the equities ledger
   it is the third of any kind (AVGO ladder, TSLA water, GOOGL water); counting
   the futures leg it is the fourth.
3. **The positive case is not "proven".** `_water_floor_stats` reports
   `INSUFFICIENT DATA` at 2 equity fires, and those two are split +$897.40 /
   −$99.75. Two fires, one of them a shakeout, is exactly the sample the verdict
   gate exists to refuse.

### The measurement gap this trip closed — and the one it exposed

The water floor shipped 2026-08-31 with counters in the log but **no section in
the performance report and no attribution surviving into the ledger.**
`performance_analyzer.py` kept two hand-copied lists of the stop-attribution keys
(one in `_normalize`, one in `_pair_round_trips`) and neither was updated when
`trade_logger._STOP_ATTR_KEYS` gained `water_floor_active` / `water_floor_price`
/ `water_caused_exit`. So `trades.log` recorded the attribution in full while
`closed_trips` dropped all three, and **both 09-01 fires plus this one landed in
the ledger looking like plain trail stops.**

Fixed 2026-09-02, three parts:

- both copies replaced by `**{k: raw.get(k) for k in _STOP_ATTR_KEYS}` — the key
  list is owned by `trade_logger` and never re-typed;
- a **reconcile** in `_merge_events`, not a one-shot backfill: stored events are
  derived values frozen against the derivation of their day, and re-running could
  not repair them because dedup skipped them as already-seen. It fills only keys
  that are *absent*, never a present-but-`None` (which means "known
  unattributed"), so it is idempotent. It repaired **12 events** on first run and
  reports the count in the report and the run log.
- a `=== WATER FLOOR ANALYSIS ===` section, so the feature is arguable from the
  report instead of only from this file.

The test that should have caught it asserted on **source text** —
`src.count('raw.get("breakeven_lock_held")') == 1` over four hand-listed keys —
so it passed throughout, because whoever forgot to add the water keys to the
analyzer also forgot to add them to the test's literal. Rewritten to drive
`_normalize` and `_pair_round_trips` and iterate `_STOP_ATTR_KEYS` itself; a
fifth stop source is now covered by adding it to the tuple.

**Still open:** the futures ledger. `TRADES_GLOB` reads `trades.log*` only, so
NQU26's water-floor exit is in `futures_trades.log` and in **no ledger and no
report**, which is why the scoreboard above has a futures row the report cannot
see. Backlogged as "performance_analyzer.py reads `trades.log*` only".

**And a warning about what that invisibility cost.** This section originally
recorded NQU26 as **+$8,520** — the figure carried in the backlog, in
`config.py`'s WATER_FLOOR_K commentary, and in the reasoning that set K=0.75.
It is wrong. The true realized P&L is **−$35.00**: the entry used throughout was
29118.75, which was the position's *pre-arming stop*, while the actual fill was
29546.50. +$8,520 was `exit − old stop`, a counterfactual booked as P&L, and the
same bad entry inflated the run at arming from **0.52 ATR to 1.37 ATR**.

The consequence for this file's argument: including futures does **not** unlock a
positive verdict. Measured, `_water_floor_stats` returns **NEUTRAL** at 3 caused
exits (+$762.65 net, 1 winner of 3, 23.9% capture) — where the wrong figure would
have returned *HELPING* on a 292% capture ratio, which is the tell that a trade
cannot bank three times its own best excursion. A number no ledger ever checked
survived into three documents and a config decision. That is the real cost of the
glob, more than the missing count.
