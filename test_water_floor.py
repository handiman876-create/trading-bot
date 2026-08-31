"""
Unit tests for the WATER FLOOR — floor = max(entry, water -/+ k*ATR). NO network.

Isolation contract matches test_profit_floor.py / test_stop_move_attribution.py:
strategy._STOPS_PATH is redirected to a throwaway temp file and the TradeStation
client is stubbed, so nothing here can touch data/stop_prices*.json or place a
real order.

CONFTEST INTERACTION — READ FIRST
---------------------------------
conftest.py's autouse fixture sets `config.ENABLE_WATER_FLOOR = False` for every
test in the suite, because the water floor is a FOURTH stop source that arms on
any position whose run cleared k*ATR — which is most fixtures that move price far
enough to trail at all. Turning it on live took the suite from 3 known failures
to 40 across test_stops.py, the breakeven-lock modules and the futures stops,
none of which reference it; every one was the feature working, not a break. So
EVERY test here must set the flag True in its own body (monkeypatch still undoes
it). A test that forgets silently measures a world with no water floor and passes
for the wrong reason.

WHAT THIS FILE EXISTS TO PIN
---------------------------
The breakeven lock floors a winner at `entry`, which protects $0 by construction,
and it provably dominates the ATR trail for every excursion in
[BREAKEVEN_LOCK_ATR*ATR, atr_mult*ATR). Inside that window the best stop
available was breakeven, so a large open gain round-tripped to a scratch: QQQ
08-13→08-18 gave back $1,131.90 of peak to realise -$7.92 with the trail 21
points away. The water floor trails the excursion instead of sitting at entry.

THE TWO GUARDS ARE THE INTERESTING PART, AND BOTH ARE LOAD-BEARING
------------------------------------------------------------------
`_water_floor` returns None rather than implementing `max(entry, ...)` literally:

  GUARD 1 (past entry). Returning `entry` on a short run would persist
  `water_floor_price == entry`, and since _stop_source tests the water floor
  before the `stop == entry` breakeven-lock branch, every lock-held stop would
  relabel as "water floor" and lock attribution would go to zero. The `max()`
  semantics are not lost — the caller contributes `entry` from the lock and takes
  the most protective source. test_rty_short_run_stays_at_entry_and_keeps_lock_label
  is the regression.

  GUARD 2 (never through the market). A floor on the wrong side of the current
  price is an instant market exit. Confirmed against live state on 2026-08-31:
  the unguarded formula put the floor ABOVE last price on BOTH open futures legs
  (ESU26 7746.20 vs 7701.50, NQU26 29555.16 vs 29511.75) because each had run
  past 1.3 ATR then retraced — shipping without it would have closed both
  positions. test_es_live_leg_retraced_does_not_arm_through_market and its NQ
  twin are the regression, and they use the real persisted records.

  The clamp in GUARD 2 must never reach attribution: on the cycle price breaches
  the floor it returns None, so anything asking "was the water floor active?"
  through this function answers "no" exactly when the floor fired — the QQQ
  2026-08-18 bug verbatim. test_attribution_survives_the_arming_clamp pins that
  the persisted level, not the live return, drives the label.
"""

import os
import tempfile

import config
import strategy


# ── Harness ───────────────────────────────────────────────────────────────────

def _capture_logs():
    msgs = []
    orig = strategy.logger.info
    strategy.logger.info = lambda fmt, *a: msgs.append(fmt % a if a else fmt)
    return msgs, orig


def _reset(quote_price, k=0.5):
    config.ENABLE_WATER_FLOOR = True          # conftest pins this False
    config.WATER_FLOOR_K = k
    strategy._stop_exits = 0
    strategy._stops_trailed = 0
    strategy._breakeven_locks = 0
    strategy._water_floors_long = 0
    strategy._water_floors_short = 0
    strategy.tc.get_quote = lambda s: {"Last": quote_price}
    strategy.tc.place_equity_order = lambda *a, **k: {"OrderID": "TEST"}
    if os.path.exists(strategy._STOPS_PATH):
        os.remove(strategy._STOPS_PATH)


def _run(symbol, held, price, rec):
    strategy._save_stops({symbol: rec})
    msgs, orig = _capture_logs()
    try:
        strategy._check_and_trail_stop(
            symbol, held, {"close": price, "atr": rec["atr_at_entry"]}, "ACCT", [])
    finally:
        strategy.logger.info = orig
    return strategy._load_stops()[symbol], msgs


# ── 1. QQQ — the equities regression with real numbers ────────────────────────

def test_qqq_water_floor_beats_breakeven_lock():
    """QQQ 2026-08-13→08-18: the case the feature exists for.

    Real figures from the incident: entry 717.26, high_water 734.41, raw trail
    696.00 → mult*atr = 38.41, so at mult 2.5 the entry ATR is 15.364. The run is
    734.41 - 717.26 = 17.15 = 1.12 ATR — inside [1*ATR, 2.5*ATR), the window where
    the lock dominates the trail and therefore caps protection at breakeven.

    Water floor = 734.41 - 0.5*15.364 = 726.728. The trade realised -$7.92 on 66
    shares with the stop at entry; the floor would have held 726.73, i.e.
    9.47/share above entry.
    """
    atr = 38.41 / 2.5
    _reset(quote_price=730.00)
    rec = {"entry_price": 717.26, "atr_at_entry": atr, "atr_mult": 2.5,
           "high_water": 734.41, "stop_price": 717.26, "opened": "2026-08-13",
           "bootstrapped": False}
    out, msgs = _run("QQQ", 66, 730.00, rec)

    assert abs(out["stop_price"] - 726.728) < 0.01, out["stop_price"]
    assert out["water_floor_active"] is True, out
    assert abs(out["water_floor_price"] - 726.728) < 0.01, out
    # Strictly better than the mechanism it replaces, and strictly behind price.
    assert out["stop_price"] > rec["entry_price"], "must beat breakeven"
    assert out["stop_price"] < 730.00, "must stay behind the market"
    # The trail is nowhere near — this is the dominance window.
    assert 734.41 - 2.5 * atr < rec["entry_price"]
    assert strategy._water_floors_long == 1, strategy._water_floors_long
    assert any("WATER FLOOR QQQ" in m for m in msgs), msgs


def test_qqq_exit_is_attributed_to_water_floor_not_trail():
    """A breach of the water floor must label as `water floor`, not `atr trail`.

    This is the line-563 failure mode one source further out: the floor sits
    ABOVE entry, so the `stop == entry` tell cannot see it and it would otherwise
    fall through to the trail.
    """
    atr = 38.41 / 2.5
    _reset(quote_price=726.00)                       # through the 726.728 floor
    rec = {"entry_price": 717.26, "atr_at_entry": atr, "atr_mult": 2.5,
           "high_water": 734.41, "stop_price": 726.728,
           "water_floor_price": 726.728, "opened": "2026-08-13",
           "bootstrapped": False}
    strategy._save_stops({"QQQ": rec})
    src = strategy._stop_source(strategy._load_stops()["QQQ"], 717.26, False, True)
    assert src == "water floor", src


# ── 2. Live futures legs — GUARD 2, from the real persisted records ───────────

def test_es_live_leg_retraced_does_not_arm_through_market():
    """ESU26 as persisted on 2026-08-31: floor would be ABOVE last price.

    entry 7674.25, atr 70.6026, high_water 7781.50 → floor 7746.20, but last
    price was 7701.50. The position ran 1.52 ATR and retraced; the floor never
    existed while it ran, so there is no ratcheted stop to inherit. Arming here
    would place a long's stop above the market = instant exit.

    Correct behaviour: the water floor does NOT arm, and the stop stays where the
    breakeven lock put it (entry).
    """
    _reset(quote_price=7701.50)
    rec = {"entry_price": 7674.25, "atr_at_entry": 70.6026, "atr_mult": 3.0,
           "high_water": 7781.50, "stop_price": 7674.25, "opened": "2026-08-17",
           "bootstrapped": False}
    assert strategy._water_floor(rec, 7701.50) is None, \
        "must not arm through the market"
    out, msgs = _run("ESU26", 1, 7701.50, rec)
    assert abs(out["stop_price"] - 7674.25) < 0.01, \
        f"stop must stay at breakeven, got {out['stop_price']}"
    assert out["water_floor_active"] is False, out
    assert "water_floor_price" not in out, \
        "an unarmed floor must not persist a level"
    assert strategy._water_floors_long == 0
    assert not any("WATER FLOOR" in m for m in msgs), msgs


def test_nq_live_leg_retraced_does_not_arm_through_market():
    """NQU26 twin of the ES case: floor 29555.16 vs last price 29511.75."""
    _reset(quote_price=29511.75)
    rec = {"entry_price": 29118.75, "atr_at_entry": 499.1769, "atr_mult": 3.0,
           "high_water": 29804.75, "stop_price": 29118.75, "opened": "2026-08-17",
           "bootstrapped": False}
    assert strategy._water_floor(rec, 29511.75) is None
    out, _ = _run("NQU26", 1, 29511.75, rec)
    assert abs(out["stop_price"] - 29118.75) < 0.01, out["stop_price"]
    assert out["water_floor_active"] is False


def test_es_arms_once_price_is_back_above_the_floor():
    """Same ES record, price recovered: now the floor is legitimately behind the
    market and must arm. Pairs with the negative above so the guard is shown to
    be a clamp on POSITION, not a blanket refusal."""
    _reset(quote_price=7760.00)
    rec = {"entry_price": 7674.25, "atr_at_entry": 70.6026, "atr_mult": 3.0,
           "high_water": 7781.50, "stop_price": 7674.25, "opened": "2026-08-17",
           "bootstrapped": False}
    out, msgs = _run("ESU26", 1, 7760.00, rec)
    assert abs(out["stop_price"] - 7746.1987) < 0.01, out["stop_price"]
    assert out["water_floor_active"] is True, out
    assert strategy._water_floors_long == 1


# ── 3. RTY — GUARD 1, the short-run negative case ─────────────────────────────

def test_rty_short_run_stays_at_entry_and_keeps_lock_label():
    """RTYU26: 0.55 ATR run, so water - 0.5*ATR lands barely past entry... and
    the documented negative case is that it must NOT arm.

    Constructed to the documented 0.55 ATR run rather than a live record (RTY is
    flat). With k=0.5 a 0.55 ATR run clears entry by only 0.05 ATR, which is why
    the backlog's own table shows k=0.5 capturing $90 on this leg — it is the
    boundary. Set k=0.75 to put it unambiguously below entry, which is the regime
    the negative case is about: a position that never really ran keeps the
    breakeven lock, and the lock KEEPS ITS LABEL.
    """
    _reset(quote_price=2300.0, k=0.75)
    entry, atr = 2300.0, 100.0
    rec = {"entry_price": entry, "atr_at_entry": atr, "atr_mult": 3.0,
           "high_water": entry + 0.55 * atr, "stop_price": entry,
           "opened": "2026-08-17", "bootstrapped": False}
    assert strategy._water_floor(rec, 2300.0) is None, \
        "0.55 ATR run must not clear entry at k=0.75"

    out, msgs = _run("RTYU26", 1, 2300.0, rec)
    assert abs(out["stop_price"] - entry) < 0.01, out["stop_price"]
    assert "water_floor_price" not in out, out
    assert strategy._water_floors_long == 0
    # THE LABEL TRAP: entry is still owned by the breakeven lock.
    src = strategy._stop_source(out, entry, False, True)
    assert src == "breakeven lock", \
        f"water floor must not steal the lock's label at entry, got {src}"


def test_floor_exactly_at_entry_is_rejected():
    """The equality case Guard 1 exists for: a run of exactly k*ATR puts the
    floor precisely ON entry, where it is indistinguishable from the lock."""
    _reset(quote_price=100.0)
    rec = {"entry_price": 100.0, "atr_at_entry": 10.0, "atr_mult": 2.5,
           "high_water": 105.0, "stop_price": 100.0, "opened": "2026-08-17",
           "bootstrapped": False}
    # water 105 - 0.5*10 = 100.0 == entry exactly.
    assert strategy._water_floor(rec, 100.0) is None
    out = _run("AAA", 10, 100.0, rec)[0]
    src = strategy._stop_source(out, 100.0, False, True)
    assert src == "breakeven lock", src


# ── 4. Shorts — direction awareness ───────────────────────────────────────────

def test_short_water_floor_mirrors_below_entry():
    """GOOGL-shaped short: the floor sits BELOW entry and ABOVE price.

    entry 345.76, atr 11.3921, low_water 330.00 → floor 330.00 + 5.696 = 335.696,
    which is below entry (protective for a short) and above price (behind the
    market). Run = 15.76 = 1.38 ATR.
    """
    _reset(quote_price=332.00)
    rec = {"direction": "short", "entry_price": 345.76, "atr_at_entry": 11.3921,
           "atr_mult": 2.5, "low_water": 330.00, "stop_price": 345.76,
           "opened": "2026-08-13", "bootstrapped": False}
    out, msgs = _run("GOOGL", -140, 332.00, rec)

    assert abs(out["stop_price"] - 335.6961) < 0.01, out["stop_price"]
    assert out["stop_price"] < rec["entry_price"], "short floor sits below entry"
    assert out["stop_price"] > 332.00, "short floor stays above the market"
    assert out["water_floor_active"] is True, out
    assert strategy._water_floors_short == 1, "must count on the SHORT counter"
    assert strategy._water_floors_long == 0, "long counter must stay clean"
    assert any("WATER FLOOR GOOGL short" in m for m in msgs), msgs


def test_short_does_not_arm_through_market():
    """Mirror of Guard 2 for shorts: price has rallied back above the floor."""
    _reset(quote_price=340.00)
    rec = {"direction": "short", "entry_price": 345.76, "atr_at_entry": 11.3921,
           "atr_mult": 2.5, "low_water": 330.00, "stop_price": 345.76,
           "opened": "2026-08-13", "bootstrapped": False}
    # floor 335.70 is BELOW price 340.00 -> for a short that is through the market
    assert strategy._water_floor(rec, 340.00) is None
    out, _ = _run("GOOGL", -140, 340.00, rec)
    assert abs(out["stop_price"] - 345.76) < 0.01, out["stop_price"]


# ── 5. Composition with the other floors ──────────────────────────────────────

def test_armed_water_floor_ALWAYS_dominates_the_atr_trail():
    """Structural, and the biggest behavioural consequence of shipping this.

    Both levels are anchored to the SAME water mark:

        trail = water - atr_mult * atr
        floor = water - k        * atr

    so the floor is tighter than the trail iff `k < atr_mult` — and config.py
    VALIDATES `0 < WATER_FLOOR_K < STOP_LOSS_ATR_MULT` at import. Therefore an
    armed water floor can NEVER lose to the trail; it wins by
    `(atr_mult - k) * atr` at every excursion, forever.

    This is NOT the ladder's contract. A profit rung is entry-anchored and static,
    so the trail eventually overtakes it and the rung goes inert. The water floor
    trails the same mark as the trail itself, so it simply REPLACES it: once a
    position is more than k*ATR past entry, its effective trailing stop is
    k*ATR wide, not atr_mult*ATR.

    Pinned as a test because it is easy to read the feature as "a floor that
    complements the trail" (which the ladder is) rather than "a much tighter trail
    that supersedes it" (which this is). If a future change wants the trail to win
    back at large excursions, it needs an explicit cap — the arithmetic alone will
    never do it.
    """
    _reset(quote_price=150.0)
    rec = {"entry_price": 100.0, "atr_at_entry": 4.0, "atr_mult": 2.5,
           "high_water": 150.0, "stop_price": 139.0, "opened": "2026-08-17",
           "bootstrapped": False}
    out, _ = _run("AAA", 10, 150.0, rec)
    trail = 150.0 - 2.5 * 4.0                      # 140.0
    assert abs(out["stop_price"] - 148.0) < 0.01, out["stop_price"]
    assert out["stop_price"] > trail, "floor must beat the trail"
    assert abs(out["stop_price"] - trail - (2.5 - 0.5) * 4.0) < 0.01, \
        "gap over the trail is exactly (atr_mult - k) * atr"

    # The dominance does not decay at a large excursion — it is constant.
    _reset(quote_price=500.0)
    rec = {"entry_price": 100.0, "atr_at_entry": 4.0, "atr_mult": 2.5,
           "high_water": 500.0, "stop_price": 400.0, "opened": "2026-08-17",
           "bootstrapped": False}
    out, _ = _run("AAA", 10, 500.0, rec)
    assert abs(out["stop_price"] - 498.0) < 0.01, \
        f"10x the gain, still 0.5 ATR behind water: {out['stop_price']}"
    assert abs(out["stop_price"] - (500.0 - 2.5 * 4.0) - 8.0) < 0.01

    # And config forbids the only setting that would let the trail win.
    assert config.WATER_FLOOR_K < config.STOP_LOSS_ATR_MULT


def test_ratchet_never_loosens_when_floor_unarms():
    """Once the floor has moved the stop, a later cycle where Guard 2 blocks it
    must NOT give the level back. The stop's own monotonic ratchet is what holds
    it — this is why un-arming is safe."""
    _reset(quote_price=150.0)
    rec = {"entry_price": 100.0, "atr_at_entry": 4.0, "atr_mult": 2.5,
           "high_water": 150.0, "stop_price": 139.0, "opened": "2026-08-17",
           "bootstrapped": False}
    out, _ = _run("AAA", 10, 150.0, rec)
    assert abs(out["stop_price"] - 148.0) < 0.01, out["stop_price"]

    # Price collapses below the floor. _water_floor now returns None (through the
    # market), but the persisted stop must stay at 148.
    assert strategy._water_floor(out, 145.0) is None
    strategy.tc.get_quote = lambda s: {"Last": 145.0}
    out2, _ = _run("AAA", 10, 145.0, out)
    assert abs(out2["stop_price"] - 148.0) < 0.01, \
        f"ratchet must hold the armed level, got {out2['stop_price']}"


def test_attribution_survives_the_arming_clamp():
    """The QQQ-2026-08-18 trap, one source out: on the breach cycle Guard 2
    returns None, so attribution must come from the PERSISTED level."""
    _reset(quote_price=147.0)
    rec = {"entry_price": 100.0, "atr_at_entry": 4.0, "atr_mult": 2.5,
           "high_water": 150.0, "stop_price": 148.0,
           "water_floor_price": 148.0, "opened": "2026-08-17",
           "bootstrapped": False}
    # Live recompute is blocked by the clamp...
    assert strategy._water_floor(rec, 147.0) is None
    # ...but the label still resolves, because it reads the persisted level.
    assert strategy._stop_source(rec, 100.0, False, True) == "water floor"


# ── 6. Helper units ───────────────────────────────────────────────────────────

def test_floor_price_anchor_generalisation():
    """The default call must be byte-identical to the pre-anchor behaviour, and
    an explicit anchor must move the result by exactly the anchor delta."""
    entry, atr, mult = 100.0, 4.0, 2.5
    base_long = strategy._floor_price(entry, atr, mult, "long")
    assert abs(base_long - (entry - atr * mult * config.BROKER_STOP_FLOOR_BUFFER)) < 1e-9
    base_short = strategy._floor_price(entry, atr, mult, "short")
    assert abs(base_short - (entry + atr * mult * config.BROKER_STOP_FLOOR_BUFFER)) < 1e-9

    # anchor shifts the result one-for-one
    shifted = strategy._floor_price(entry, atr, mult, "long", anchor=120.0)
    assert abs(shifted - (base_long + 20.0)) < 1e-9

    # buffer=1.0 removes the widening — the water floor's call shape
    wf = strategy._floor_price(entry, atr, 0.5, "long", anchor=150.0, buffer=1.0)
    assert abs(wf - (150.0 - 2.0)) < 1e-9
    wf_s = strategy._floor_price(entry, atr, 0.5, "short", anchor=50.0, buffer=1.0)
    assert abs(wf_s - (50.0 + 2.0)) < 1e-9


def test_broker_floor_target_picks_most_protective():
    """The GTC must track the same source the bot stop does."""
    rec = {"entry_price": 100.0}
    # long: water floor higher than the rung -> water wins
    got = strategy._broker_floor_target(rec, 100.0, "long", (110.0, 0.15, 0.10), 120.0)
    assert got[0] == 120.0 and got[2] == "water floor", got
    assert abs(got[1] - 0.20) < 1e-9, "implied lock = (120-100)/100"
    # long: rung higher -> rung wins, and keeps its declared lock
    got = strategy._broker_floor_target(rec, 100.0, "long", (130.0, 0.35, 0.30), 120.0)
    assert got[0] == 130.0 and got[2] == "profit floor" and got[1] == 0.30, got
    # short: most protective is the LOWEST
    got = strategy._broker_floor_target(rec, 100.0, "short", (90.0, 0.15, 0.10), 80.0)
    assert got[0] == 80.0 and got[2] == "water floor", got
    assert abs(got[1] - 0.20) < 1e-9, "implied lock = (100-80)/100"
    # neither armed
    assert strategy._broker_floor_target(rec, 100.0, "long", None, None) is None


def test_disabled_flag_makes_it_inert():
    config.ENABLE_WATER_FLOOR = False
    rec = {"entry_price": 100.0, "atr_at_entry": 4.0, "high_water": 150.0}
    assert strategy._water_floor(rec, 150.0) is None


def test_missing_basis_returns_none():
    """No entry or no ATR -> no floor, never a crash."""
    config.ENABLE_WATER_FLOOR = True
    assert strategy._water_floor({"entry_price": 0, "atr_at_entry": 4.0,
                                  "high_water": 150.0}, 150.0) is None
    assert strategy._water_floor({"entry_price": 100.0, "atr_at_entry": 0,
                                  "high_water": 150.0}, 150.0) is None
    assert strategy._water_floor({"high_water": 150.0}, 150.0) is None


def test_counter_split_is_direction_keyed():
    strategy._water_floors_long = 0
    strategy._water_floors_short = 0
    assert strategy._bump_water_floor("long") == 1
    assert strategy._bump_water_floor("long") == 2
    assert strategy._bump_water_floor("short") == 1
    assert strategy._water_floors_long == 2, "short fire must not touch long"
    assert strategy._water_floors_short == 1
    # legacy records with no direction key count long, matching the .get default
    assert strategy._bump_water_floor("banana") == 3


if __name__ == "__main__":
    _tmp = tempfile.mkdtemp(prefix="water-floor-")
    strategy._STOPS_PATH = os.path.join(_tmp, "stop_prices.json")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all passed")
