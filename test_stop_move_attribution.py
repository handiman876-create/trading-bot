"""
Unit tests for STOP-MOVE ATTRIBUTION — the log line and the trail counter. NO network.

Same isolation contract as test_profit_floor.py / test_breakeven_lock_label.py:
strategy._STOPS_PATH is redirected to a throwaway temp file and the TradeStation
client is stubbed, so nothing here can touch data/stop_prices.json or place a
real order.

WHAT THIS FILE EXISTS TO PIN
----------------------------
Every stop move was logged as a trail move. The line was titled "STOP TRAIL",
printed the TRAIL's parameters, and bumped `trails #N` — even when a profit-floor
rung had set the level and the trail had nothing to do with it. Observed live on
GOOGL short, 2026-08-31 14:10 UTC:

    STOP TRAIL GOOGL short 367.17 → 342.30 (low_water=338.30, trail=2.50x11.39) — trails #1
    PROFIT FLOOR GOOGL short: stop 367.17 → 342.30 — +1% of entry 345.76 ... (trail would be 366.78)

The first line is internally inconsistent: 338.30 + 2.50*11.39 = 366.78, not
342.30. The rung set the stop; the line credited the trail. Two consequences,
both pinned below:
  * `trails #N` overstated real trail work by exactly the number of binding
    floor moves, so it could not be used to measure the trail.
  * A reader who recomputed the trail from the printed parameters got a number
    that did not match the printed stop, with nothing on the line to explain it.

test_googl_floor_move_is_not_counted_as_a_trail is the direct regression — it
fails on the pre-fix code.

THE TWO QUESTIONS ARE DIFFERENT, AND THAT IS THE WHOLE DESIGN
-------------------------------------------------------------
  "who HOLDS this level?"  -> _stop_source, by level identity. Drives the LABEL.
  "who MOVED the stop?"    -> counterfactual vs the trail alone. Drives the COUNTER.

They disagree in BOTH directions, so neither one alone is sufficient:

  * GOOGL: floor holds the level AND moved it past the trail -> label "profit
    floor", do not count. (Both agree.)
  * TIE case: the trail and the breakeven lock land on the SAME level. Level
    identity says "breakeven lock", but the trail alone would have produced
    exactly this stop, so the trail DOES earn the count. Using level identity
    for the counter would wrongly zero it — test_tie_counts_as_trail_move pins
    this, and it is also what keeps the pre-existing test_stops.py trail tests
    passing (a long at +10% with atr 4 on a 100 entry sits exactly here).

CONFTEST INTERACTION — READ BEFORE ADDING A CASE
------------------------------------------------
conftest.py's autouse fixture sets config.ENABLE_PROFIT_FLOOR = False for every
test, because the ladder is a third stop source that silently overrides the trail
and changes the answer in tests that are not about it. Any case here that needs a
rung MUST set it True in the test body (monkeypatch still undoes it). A case that
forgets will silently measure a pure trail and pass for the wrong reason.
"""

import os
import tempfile

import config
import strategy


# ── Harness ───────────────────────────────────────────────────────────────────

def _capture_logs():
    """Collect strategy.logger.info messages, fully formatted."""
    msgs = []
    orig = strategy.logger.info
    strategy.logger.info = lambda fmt, *a: msgs.append(fmt % a if a else fmt)
    return msgs, orig


def _reset(quote_price):
    strategy._stop_exits = 0
    strategy._stops_trailed = 0
    strategy._breakeven_locks = 0
    strategy._profit_floors_long = 0
    strategy._profit_floors_short = 0
    strategy.tc.get_quote = lambda s: {"Last": quote_price}
    strategy.tc.place_equity_order = lambda *a, **k: {"OrderID": "TEST"}
    if os.path.exists(strategy._STOPS_PATH):
        os.remove(strategy._STOPS_PATH)


def _run(symbol, held, price, rec):
    """Drive one _check_and_trail_stop cycle; return (trail_lines, all_msgs)."""
    strategy._save_stops({symbol: rec})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop(symbol, held, {"close": price, "atr": rec["atr_at_entry"]},
                                       "ACCT", [])
    finally:
        strategy.logger.info = orig
    return [m for m in msgs if "STOP TRAIL" in m], msgs


# ── 1. The GOOGL regression ───────────────────────────────────────────────────

def test_googl_floor_move_is_not_counted_as_a_trail():
    """The live 2026-08-31 case, with the real numbers.

    GOOGL short 140 @ 345.76, atr 11.3921, mult 2.5, stop 367.17. Price 338.30
    is a new low, so low_water becomes 338.30 on this cycle and the raw trail is
    338.30 + 2.5*11.3921 = 366.78 — matching the live line exactly. The gain is
    (345.76-338.30)/345.76 = 2.16%, which clears the +2% micro-rung, locking 1%
    at 345.76*0.99 = 342.3024. The rung is 24.48 points tighter, so it sets the
    level and the trail must NOT be credited.

    Price matters to the cent here: at 339.00 the gain is 1.955% and the rung
    does NOT arm, which makes this a pure trail move and the test vacuous.
    """
    _reset(quote_price=338.30)
    config.ENABLE_PROFIT_FLOOR = True
    rec = {"direction": "short", "entry_price": 345.76, "atr_at_entry": 11.3921,
           "atr_mult": 2.5, "low_water": 339.00, "stop_price": 367.17,
           "opened": "2026-08-13", "bootstrapped": False}
    trail, msgs = _run("GOOGL", -140, 338.30, rec)

    assert len(trail) == 1, f"expected one stop-move line, got {msgs}"
    line = trail[0]

    # The stop landed on the rung, not the trail.
    assert "367.17 → 342.30" in line, line
    # THE FIX, part 1: the raw trail is printed, so the line reconciles. Before,
    # a reader computed 366.78 from the parameters and saw 342.30 printed.
    assert "trail=2.50x11.39→366.78" in line, line
    # THE FIX, part 2: the line names the real source.
    assert "held by profit floor" in line, line
    assert "NOT a trail move, set by profit floor" in line, line
    assert "trail alone would be 366.78" in line, line

    # THE FIX, part 3: the counter did not move.
    assert strategy._stops_trailed == 0, \
        f"floor-set level must not bump the trail counter, got {strategy._stops_trailed}"
    # The ladder still logs and counts its own fire — this fix must not mute it.
    assert any("PROFIT FLOOR GOOGL" in m for m in msgs), msgs
    assert strategy._profit_floors_short == 1, strategy._profit_floors_short


def test_googl_line_is_internally_consistent():
    """The defect was arithmetic, so assert the arithmetic directly.

    Whatever the line prints as the trail level must equal water +/- mult*atr,
    and when it says the stop was NOT a trail move the two MUST differ. A line
    that satisfies both can no longer contradict itself the way the live one did.
    """
    _reset(quote_price=338.30)
    config.ENABLE_PROFIT_FLOOR = True
    rec = {"direction": "short", "entry_price": 345.76, "atr_at_entry": 11.3921,
           "atr_mult": 2.5, "low_water": 339.00, "stop_price": 367.17,
           "opened": "2026-08-13", "bootstrapped": False}
    trail, _ = _run("GOOGL", -140, 338.30, rec)
    line = trail[0]

    # Reconstruct the trail from the printed parameters, the way a human reads it.
    # Two "→" on the line: [1] is the new stop, [2] is the printed raw trail.
    water = float(line.split("low_water=")[1].split(",")[0])
    printed_trail = float(line.split("→")[2].split(",")[0])
    stop = float(line.split("→")[1].strip().split(" ")[0])
    assert abs((water + 2.5 * 11.3921) - printed_trail) < 0.01, \
        f"printed trail {printed_trail} != {water} + 2.5*11.3921"
    assert abs(stop - printed_trail) > 0.01, \
        "this case is a floor move: stop and trail must differ"
    assert "NOT a trail move" in line, line


# ── 2. Genuine trail moves still count ────────────────────────────────────────

def test_genuine_trail_move_counts_and_reads_clean():
    """No floor anywhere near: the trail owns the move and says so."""
    _reset(quote_price=110.0)
    config.ENABLE_PROFIT_FLOOR = False       # explicit: this case is trail-only
    rec = {"entry_price": 100.0, "atr_at_entry": 4.0, "atr_mult": 2.5,
           "high_water": 100.0, "stop_price": 80.0, "opened": "2026-07-13",
           "bootstrapped": False}
    trail, msgs = _run("AAA", 10, 110.0, rec)

    assert len(trail) == 1, msgs
    line = trail[0]
    assert "80.00 → 100.00" in line, line          # 110 - 2.5*4
    assert "trail=2.50x4.00→100.00" in line, line
    assert "held by atr trail" in line, line
    assert "trails #1" in line, line
    assert "NOT a trail move" not in line, line
    assert strategy._stops_trailed == 1, strategy._stops_trailed


def test_tie_counts_as_trail_move():
    """Trail and breakeven lock land on the SAME level -> the trail earns it.

    Long entry 100, atr 4, mult 2.5. STORED high_water must already be 110 so
    that raw_trail = 110 - 10 = 100 = entry AND the lock trigger (stored water
    >= entry + 1 ATR = 104) is already satisfied — _breakeven_reached reads the
    PRE-ratchet water, so a record whose water only reaches 110 on this cycle
    locks one cycle later and is a plain trail move, not a tie.

    With the tie set up, _stop_source labels the level "breakeven lock" by level
    identity, but the trail alone would have produced exactly 100.00, so the
    counter MUST still fire. Attributing this to the lock would zero the counter
    on a shape that occurs throughout the suite — this is the case that makes
    level-identity-for-the-counter wrong.
    """
    _reset(quote_price=110.0)
    config.ENABLE_PROFIT_FLOOR = False
    rec = {"entry_price": 100.0, "atr_at_entry": 4.0, "atr_mult": 2.5,
           "high_water": 110.0, "stop_price": 90.0, "opened": "2026-07-13",
           "bootstrapped": False}
    trail, msgs = _run("AAA", 10, 110.0, rec)

    line = trail[0]
    assert "90.00 → 100.00" in line, line
    assert "held by breakeven lock" in line, line      # label: level identity
    assert "trails #1" in line, line                   # counter: counterfactual
    assert strategy._stops_trailed == 1, \
        "a tie is still a trail move — the trail alone reached this level"


def test_no_line_and_no_count_when_stop_unchanged():
    """A pullback moves nothing. Unchanged behaviour, re-pinned here because the
    fix touches this exact branch."""
    _reset(quote_price=104.0)
    config.ENABLE_PROFIT_FLOOR = False
    rec = {"entry_price": 100.0, "atr_at_entry": 4.0, "atr_mult": 2.5,
           "high_water": 110.0, "stop_price": 100.0, "opened": "2026-07-13",
           "bootstrapped": False}
    trail, _ = _run("AAA", 10, 104.0, rec)
    assert trail == [], trail
    assert strategy._stops_trailed == 0


# ── 3. Breakeven lock that genuinely BEATS the trail ──────────────────────────

def test_lock_beating_trail_is_not_counted():
    """Lock strictly tighter than the trail -> lock owns the move.

    Long entry 100, atr 4 but a WIDE 2.5x on a low high_water: high_water 105
    -> raw trail 95, below entry. The lock floors at 100, which is 5 points
    tighter, so the lock moved the stop and the trail did not.
    """
    _reset(quote_price=105.0)
    config.ENABLE_PROFIT_FLOOR = False
    rec = {"entry_price": 100.0, "atr_at_entry": 4.0, "atr_mult": 2.5,
           "high_water": 105.0, "stop_price": 90.0, "opened": "2026-07-13",
           "bootstrapped": False}
    trail, msgs = _run("AAA", 10, 105.0, rec)

    line = trail[0]
    assert "90.00 → 100.00" in line, line
    assert "trail=2.50x4.00→95.00" in line, line
    assert "held by breakeven lock" in line, line
    assert "NOT a trail move, set by breakeven lock" in line, line
    assert "trail alone would be 95.00" in line, line
    assert strategy._stops_trailed == 0, strategy._stops_trailed
    assert strategy._breakeven_locks == 1, "the lock's own line must still fire"


# ── 4. _stop_source covers all sources ────────────────────────────────────────

def test_stop_source_all_labels():
    """Every branch of the helper, including the reserved water-floor slot.

    Called directly with hand-written records: the point is the label mapping,
    not the trail machinery, and driving each through a full cycle would couple
    these assertions to rung geometry that changes when config changes.
    """
    base = {"stop_price": 100.0}

    # atr trail — nothing else claims the level.
    assert strategy._stop_source(dict(base), 90.0, False, False) == "atr trail"

    # profit floor — the flag the caller recomputes every cycle.
    rec = dict(base, profit_floor_active=True)
    assert strategy._stop_source(rec, 90.0, False, False) == "profit floor"

    # breakeven lock — stop sits at entry and the excursion was reached.
    assert strategy._stop_source(dict(base), 100.0, False, True) == "breakeven lock"

    # crisis floor — same level as the lock, so ORDER is what separates them.
    assert strategy._stop_source(dict(base), 100.0, True, True) == "crisis floor"

    # water floor — reserved slot, inert until the feature writes the key.
    assert strategy._stop_source(dict(base), 90.0, False, False) == "atr trail", \
        "absent water_floor_price must not change today's behaviour"
    rec = dict(base, water_floor_price=100.0)
    assert strategy._stop_source(rec, 90.0, False, False) == "water floor"
    # A water floor the stop has moved off no longer holds the level.
    rec = dict(base, water_floor_price=97.0)
    assert strategy._stop_source(rec, 90.0, False, False) == "atr trail"
    # Ladder wins a tie on the same level (more specific claim).
    rec = dict(base, water_floor_price=100.0, profit_floor_active=True)
    assert strategy._stop_source(rec, 90.0, False, False) == "profit floor"


def test_stop_source_tolerates_float_dust():
    """stop_price is round(_, 4) of a float floor; equality would miss by dust."""
    rec = {"stop_price": 100.0002}
    assert strategy._stop_source(rec, 100.0, False, True) == "breakeven lock"
    rec = {"stop_price": 100.0002, "water_floor_price": 100.0}
    assert strategy._stop_source(rec, 90.0, False, False) == "water floor"


if __name__ == "__main__":
    _tmp = tempfile.mkdtemp(prefix="stop-attr-")
    strategy._STOPS_PATH = os.path.join(_tmp, "stop_prices.json")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
