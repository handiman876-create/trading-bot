"""
Unit tests for the momentum alignment entry + one-shot latch — NO network.

Monkeypatches strategy's data deps (ind.compute_indicators, tc.get_historical /
get_quote / place_equity_order) and points the stop + latch files at throwaway
temp files, so we can drive evaluate_stock's entry branches without the API,
without placing orders, and without touching live JSON state.

Run:  python3 test_momentum_entry.py
"""

import os
import tempfile

import _testlib
import strategy

_orders = []          # (symbol, side, qty) captured from place_equity_order


def _fake_place(account_id, symbol, side, qty):
    _orders.append((symbol, side, qty))
    return {"order": {"id": "T1"}}


def _buys():
    return [o for o in _orders if o[1] == "buy"]


def _sides(side):
    return [o for o in _orders if o[1] == side]


def _reset():
    _orders.clear()
    strategy._stop_exits = 0
    strategy._momentum_align_entries = 0
    strategy._short_entries = 0
    strategy._short_covers = 0
    strategy._entries_delayed = 0
    strategy._latches_reconstructed = 0
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    # These tests exercise SIGNAL logic, not the clock: pin the entry gate open
    # so they pass regardless of when the suite runs. The gate's own behaviour is
    # covered in test_entry_delay.py.
    strategy.mh.entries_allowed = lambda *a, **k: True
    strategy.config.USE_MOMENTUM_ALIGNMENT = True
    strategy.config.USE_TRAILING_STOP = True
    strategy.config.ENABLE_SHORTING = True
    strategy.tc.place_equity_order = _fake_place
    strategy.tc.get_historical = lambda *a, **k: [{"bar": 1}]     # truthy
    strategy.tc.get_quote = lambda s: {"last": 10_000.0}          # high -> no stop breach
    for path in (strategy._STOPS_PATH, strategy._MOM_ENTRIES_PATH):
        _testlib.safe_remove(path)


def _set_sig(**kw):
    """Default sig = alignment (EMA9>EMA21), RSI 55, no fresh cross."""
    sig = {"close": 100.0, "ema_short": 105.0, "ema_long": 100.0, "rsi": 55.0,
           "bullish_cross": False, "bearish_cross": False, "atr": 4.0}
    sig.update(kw)
    strategy.ind.compute_indicators = lambda *a, **k: sig
    return sig


# ── Alignment entry fires ─────────────────────────────────────────────────────

def test_alignment_fires_for_momentum():
    _reset(); _set_sig()
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _buys() == [("DAL", "buy", 50)], _orders     # 100000*0.05/100 = 50
    assert strategy._momentum_entry_taken("DAL", "G1"), "latch recorded for G1"
    assert "DAL" in strategy._load_stops(), "stop armed on alignment entry"


def test_fresh_cross_enters_core():
    """Regression: the fresh-cross path still works after the _enter_long refactor."""
    _reset(); _set_sig(bullish_cross=True)
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0,
                            is_momentum=False, momentum_generation="")
    assert _buys() == [("AAPL", "buy", 50)], _orders
    assert "AAPL" in strategy._load_stops(), "stop armed on fresh-cross entry"


# ── Latch: one-shot per rotation ──────────────────────────────────────────────

def test_latch_blocks_same_generation():
    _reset(); _set_sig()
    strategy._record_momentum_entry("DAL", "G1")        # already entered this rotation
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _buys() == [], "latch should block a second entry in the same rotation"


def test_new_generation_rearms():
    _reset(); _set_sig()
    strategy._record_momentum_entry("DAL", "G1")
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G2")
    assert _buys() == [("DAL", "buy", 50)], "new rotation id re-arms the shot"
    assert strategy._momentum_entry_taken("DAL", "G2")


def test_fresh_cross_ignores_alignment_latch():
    """A genuine fresh cross re-enters even when the alignment latch is set —
    the latch only gates the level-based alignment path, not the edge signal."""
    _reset(); _set_sig(bullish_cross=True)
    strategy._record_momentum_entry("DAL", "G1")
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _buys() == [("DAL", "buy", 50)], "fresh cross must bypass the latch"


# ── Alignment gating ──────────────────────────────────────────────────────────

def test_core_symbol_no_alignment():
    _reset(); _set_sig()
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0,
                            is_momentum=False, momentum_generation="G1")
    assert _buys() == [], "core names never take the alignment entry"


def test_rsi_too_high_blocks_alignment():
    _reset(); _set_sig(rsi=70.0)                         # > MOMENTUM_ALIGN_RSI_MAX (65)
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _buys() == [], "RSI above the ceiling (65) blocks the alignment entry"


def test_rsi_too_low_blocks_alignment():
    _reset(); _set_sig(rsi=35.1)                         # < MOMENTUM_ALIGN_RSI_MIN (45), e.g. HCA
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _buys() == [], "RSI below the floor (45) blocks the alignment entry (breakdown)"


def test_rsi_band_edges_allow_alignment():
    """Both inclusive bounds (45 and 65) permit the alignment entry."""
    for edge in (45.0, 65.0):
        _reset(); _set_sig(rsi=edge)
        strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                                is_momentum=True, momentum_generation="G1")
        assert _buys() == [("DAL", "buy", 50)], f"RSI {edge} (inclusive edge) should enter"


def test_held_blocks_alignment():
    _reset(); _set_sig()
    positions = [{"symbol": "DAL", "quantity": 50, "cost_basis": 5000.0}]
    strategy.evaluate_stock("DAL", "ACCT", positions, 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _buys() == [], "already holding -> no alignment entry"


def test_master_switch_off_disables_alignment():
    _reset(); _set_sig()
    strategy.config.USE_MOMENTUM_ALIGNMENT = False
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _buys() == [], "USE_MOMENTUM_ALIGNMENT=False disables the branch"


def test_max_positions_blocks_alignment_latch_not_consumed():
    """MAX_POSITIONS reached -> alignment blocked -> latch NOT consumed -> retries
    next cycle once a slot frees."""
    _reset(); _set_sig()
    full = [{"symbol": f"S{i}", "quantity": 1, "cost_basis": 100.0}
            for i in range(strategy.config.MAX_POSITIONS)]      # 20 open, DAL not among them
    strategy.evaluate_stock("DAL", "ACCT", full, 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _buys() == [], "max positions blocks the entry"
    assert not strategy._momentum_entry_taken("DAL", "G1"), "latch NOT consumed when blocked"

    _orders.clear()                                             # a slot frees up
    strategy.evaluate_stock("DAL", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _buys() == [("DAL", "buy", 50)], "retries and enters when slot frees"
    assert strategy._momentum_entry_taken("DAL", "G1"), "latch consumed after a real entry"


# ── Short selling: entry / cover / guards ─────────────────────────────────────

def test_short_enters_core_on_death_cross():
    """Core name, fresh death cross, flat -> SELLSHORT, sized like a long, with a
    trailing stop armed ABOVE entry."""
    _reset(); _set_sig(bearish_cross=True, close=100.0)
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0,
                            is_momentum=False, momentum_generation="")
    assert _sides("sell_short") == [("AAPL", "sell_short", 50)], _orders
    assert strategy._short_entries == 1, "short-entry counter incremented"
    rec = strategy._load_stops()["AAPL"]
    assert rec["direction"] == "short", rec
    assert abs(rec["stop_price"] - 110.0) < 1e-6, rec        # 100 + 2.5*4, stop ABOVE
    assert abs(rec["low_water"] - 100.0) < 1e-6, rec


def test_momentum_name_does_not_short():
    """The momentum slot is long-only: a death cross must NOT open a short."""
    _reset(); _set_sig(bearish_cross=True)
    strategy.evaluate_stock("DDOG", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1")
    assert _sides("sell_short") == [], "momentum names never short"


def test_shorting_disabled_no_short():
    _reset(); _set_sig(bearish_cross=True)
    strategy.config.ENABLE_SHORTING = False
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0,
                            is_momentum=False, momentum_generation="")
    assert _sides("sell_short") == [], "ENABLE_SHORTING=False disables shorting"


def test_short_respects_max_positions():
    _reset(); _set_sig(bearish_cross=True)
    full = [{"symbol": f"S{i}", "quantity": 1, "cost_basis": 100.0}
            for i in range(strategy.config.MAX_POSITIONS)]
    strategy.evaluate_stock("AAPL", "ACCT", full, 100000.0,
                            is_momentum=False, momentum_generation="")
    assert _sides("sell_short") == [], "max positions blocks a new short"


def test_cover_on_bullish_cross():
    """A held short is bought to cover on a bullish cross; the stop record clears.
    Quote is below the ABOVE stop so the trailing stop does NOT fire first."""
    _reset(); _set_sig(bullish_cross=True, close=100.0)
    strategy.tc.get_quote = lambda s: {"last": 100.0}       # below short stop (110) -> no breach
    strategy._arm_stop_on_entry("AAPL", 100.0, 4.0, direction="short")   # stop 110
    positions = [{"symbol": "AAPL", "quantity": -50, "cost_basis": 5000.0}]
    strategy.evaluate_stock("AAPL", "ACCT", positions, 100000.0,
                            is_momentum=False, momentum_generation="")
    assert _sides("buy_to_cover") == [("AAPL", "buy_to_cover", 50)], _orders
    assert strategy._short_covers == 1, "cover counter incremented"
    assert "AAPL" not in strategy._load_stops(), "stop cleared on cover"


def test_short_stops_out_when_price_rises_into_stop():
    """Trailing stop fires (buy_to_cover) BEFORE the signal when price rises into
    the ABOVE stop — even on the same cycle."""
    _reset(); _set_sig(bullish_cross=False, bearish_cross=False, close=115.0)
    strategy.tc.get_quote = lambda s: {"last": 115.0}       # above short stop (110) -> breach
    strategy._arm_stop_on_entry("AAPL", 100.0, 4.0, direction="short")   # stop 110
    positions = [{"symbol": "AAPL", "quantity": -50, "cost_basis": 5000.0}]
    strategy.evaluate_stock("AAPL", "ACCT", positions, 100000.0,
                            is_momentum=False, momentum_generation="")
    assert _sides("buy_to_cover") == [("AAPL", "buy_to_cover", 50)], _orders
    assert strategy._stop_exits == 1, "short stop-out counted"
    assert "AAPL" not in strategy._load_stops(), "record cleared after stop-out"


# ── Reconcile ─────────────────────────────────────────────────────────────────

def test_reconcile_momentum_prunes_unlisted():
    _reset()
    strategy._save_json(strategy._MOM_ENTRIES_PATH, {
        "DAL": {"generation": "G1", "entered": "d"},
        "OLD": {"generation": "G1", "entered": "d"}})
    strategy.reconcile_momentum_entries(["DAL", "DDOG"], [], "G1")
    entries = strategy._load_json(strategy._MOM_ENTRIES_PATH)
    assert "DAL" in entries, entries
    assert "OLD" not in entries, "name no longer in slot pruned"


def test_reconcile_empty_slot_guard():
    _reset()
    strategy._save_json(strategy._MOM_ENTRIES_PATH,
                        {"DAL": {"generation": "G1", "entered": "d"}})
    strategy.reconcile_momentum_entries([], [], "G1")           # screen failed -> []
    assert "DAL" in strategy._load_json(strategy._MOM_ENTRIES_PATH), \
        "empty slot must not prune latches"


# ── Latch reconstruction (the 2026-07-16 CRL/LII doubling) ───────────────────
# A held momentum name with no latch record means the record was LOST (the test
# wipe of 07-15), not never written. Broker positions are the authority — the
# stop file is not, because the same _reset() deletes both.

def _held(symbol, qty=440):
    return [{"symbol": symbol, "quantity": qty, "cost_basis": 1000.0}]


def test_reconstruct_latch_for_held_name_with_no_record():
    _reset()
    strategy._save_json(strategy._MOM_ENTRIES_PATH, {})          # wiped
    strategy.reconcile_momentum_entries(["CRL"], _held("CRL"), "G1")
    rec = strategy._load_json(strategy._MOM_ENTRIES_PATH).get("CRL")
    assert rec, "held momentum name with no latch must be reconstructed"
    assert rec["generation"] == "G1", rec
    assert rec.get("reconstructed") is True, "reconstructed records are marked"
    assert strategy._latches_reconstructed == 1, "counter incremented"


def test_reconstructed_latch_blocks_the_re_entry():
    """End-to-end: the exact 07-16 scenario. Wiped latch + a positions fetch that
    wrongly reads flat = the double entry. With reconcile run first, the latch is
    back and the entry is blocked."""
    _reset(); _set_sig(rsi=63.1)                                 # CRL's RSI that day
    strategy._save_json(strategy._MOM_ENTRIES_PATH, {})          # latch wiped 07-15
    strategy.reconcile_momentum_entries(["CRL"], _held("CRL", 219), "G1")
    # The 503: positions read as [] -> held == 0 -> "flat".
    strategy.evaluate_stock("CRL", "ACCT", [], 998905.0,
                            is_momentum=True, momentum_generation="G1")
    assert _buys() == [], "reconstructed latch must block the 503 re-entry"


def test_reconstruct_does_not_overwrite_older_generation():
    """A name held from rotation G1 into G2 keeps its G1 latch: the shot for G2 is
    legitimately unused (the latch re-arms per rotation), and stamping it G2 would
    silently consume a re-entry the strategy is entitled to after a stop-out."""
    _reset()
    strategy._save_json(strategy._MOM_ENTRIES_PATH,
                        {"CRL": {"generation": "G1", "entered": "d"}})
    strategy.reconcile_momentum_entries(["CRL"], _held("CRL"), "G2")
    rec = strategy._load_json(strategy._MOM_ENTRIES_PATH)["CRL"]
    assert rec["generation"] == "G1", "existing record must not be overwritten"
    assert "reconstructed" not in rec, rec
    assert strategy._latches_reconstructed == 0, "nothing was reconstructed"


def test_no_reconstruct_when_not_held():
    _reset()
    strategy._save_json(strategy._MOM_ENTRIES_PATH, {})
    strategy.reconcile_momentum_entries(["CRL"], [], "G1")       # in slot, not held
    assert strategy._load_json(strategy._MOM_ENTRIES_PATH) == {}, \
        "a name we don't hold has no entry to latch"
    assert strategy._latches_reconstructed == 0


def test_no_reconstruct_for_zero_quantity_position():
    _reset()
    strategy._save_json(strategy._MOM_ENTRIES_PATH, {})
    strategy.reconcile_momentum_entries(["CRL"], _held("CRL", 0), "G1")
    assert strategy._load_json(strategy._MOM_ENTRIES_PATH) == {}, \
        "a closed (qty=0) position is not held"


def test_no_reconstruct_for_held_name_outside_slot():
    """Held but not in the momentum slot (e.g. a core name): the alignment path
    never applies to it, so it has no latch to rebuild."""
    _reset()
    strategy._save_json(strategy._MOM_ENTRIES_PATH, {})
    strategy.reconcile_momentum_entries(["CRL"], _held("AAPL"), "G1")
    assert strategy._load_json(strategy._MOM_ENTRIES_PATH) == {}, \
        "only momentum-slot names get latches"


def test_reconstruct_and_prune_in_one_pass():
    """Both directions at once, single write."""
    _reset()
    strategy._save_json(strategy._MOM_ENTRIES_PATH,
                        {"OLD": {"generation": "G1", "entered": "d"}})
    strategy.reconcile_momentum_entries(["CRL"], _held("CRL"), "G1")
    entries = strategy._load_json(strategy._MOM_ENTRIES_PATH)
    assert "OLD" not in entries, "pruned"
    assert entries["CRL"]["reconstructed"] is True, "reconstructed"


if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="mom_test_")
    strategy._STOPS_PATH = os.path.join(_tmpdir, "stop_prices.json")
    strategy._MOM_ENTRIES_PATH = os.path.join(_tmpdir, "momentum_entries.json")
    _orig = {
        "place": strategy.tc.place_equity_order,
        "hist":  strategy.tc.get_historical,
        "quote": strategy.tc.get_quote,
        "ci":    strategy.ind.compute_indicators,
        "log":   strategy.log_trade,
    }
    strategy.log_trade = lambda *a, **k: None
    try:
        tests = [v for k, v in sorted(globals().items())
                 if k.startswith("test_") and callable(v)]
        passed = 0
        for t in tests:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        print(f"All {passed} assertions passed.")
    finally:
        strategy.tc.place_equity_order = _orig["place"]
        strategy.tc.get_historical    = _orig["hist"]
        strategy.tc.get_quote         = _orig["quote"]
        strategy.ind.compute_indicators = _orig["ci"]
        strategy.log_trade            = _orig["log"]
