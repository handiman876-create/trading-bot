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


def _capture_logs():
    """Swap strategy.logger.info for a collector. Mirrors the helper in
    test_breakeven_lock_label.py — the suite deliberately avoids pytest's
    caplog fixture because every test file here is also runnable standalone
    (`python3 test_momentum_entry.py`), where fixtures do not exist."""
    msgs = []
    orig = strategy.logger.info
    strategy.logger.info = lambda fmt, *a: msgs.append(fmt % a if a else fmt)
    return msgs, orig


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
    strategy._regime_short_blocks = 0
    strategy._shorting_disabled_blocks = 0
    strategy._entries_delayed = 0
    strategy._latches_reconstructed = 0
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    # Per-episode counter dedupe. MUST be cleared between tests: it is keyed by
    # (symbol, direction) and survives a _reset otherwise, so a later test
    # reusing the same symbol would see its counter stay at 0 and "pass" for the
    # wrong reason.
    strategy._counted_cross_episodes.clear()
    strategy._cross_first_seen.clear()
    strategy._cross_confirmed.clear()
    # These tests exercise SIGNAL logic, not the clock: pin the entry gate open
    # so they pass regardless of when the suite runs. The gate's own behaviour is
    # covered in test_entry_delay.py.
    strategy.mh.entries_allowed = lambda *a, **k: True
    strategy.config.USE_MOMENTUM_ALIGNMENT = True
    # CROSS_SUSTAIN_MINUTES=0 isolates these cases from cross persistence:
    # they assert on gap/edge/latch behaviour, not on how long a cross has
    # held, and would otherwise all need a 30-minute clock advance.
    strategy.config.CROSS_SUSTAIN_MINUTES = 0
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
    # regime="cautious" is now the ONLY regime a new short can open in
    # (SHORT_MIN_REGIME blocks risk_on; defensive/crisis block all entries), so
    # the armed width is the cautious 2.0x — a short can no longer arm at 2.5x.
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0,
                            is_momentum=False, momentum_generation="",
                            regime="cautious")
    assert _sides("sell_short") == [("AAPL", "sell_short", 50)], _orders
    assert strategy._short_entries == 1, "short-entry counter incremented"
    rec = strategy._load_stops()["AAPL"]
    assert rec["direction"] == "short", rec
    assert abs(rec["stop_price"] - 108.0) < 1e-6, rec        # 100 + 2.0*4, stop ABOVE
    assert abs(rec["low_water"] - 100.0) < 1e-6, rec


def test_momentum_name_shorts_on_death_cross():
    """The short universe is now the full effective watchlist: a momentum-slot
    name (e.g. DDOG) opens a short on a fresh death cross, sized like a long with
    a trailing stop armed ABOVE entry — same as a core name."""
    # A genuine death cross: fast EMA now BELOW slow, so the (earlier) momentum
    # alignment branch cannot fire and the short branch is reached.
    _reset(); _set_sig(bearish_cross=True, close=100.0, ema_short=100.0, ema_long=105.0)
    strategy.evaluate_stock("DDOG", "ACCT", [], 100000.0,
                            is_momentum=True, momentum_generation="G1",
                            regime="cautious")
    assert _sides("sell_short") == [("DDOG", "sell_short", 50)], _orders
    assert strategy._short_entries == 1, "short-entry counter incremented"
    rec = strategy._load_stops()["DDOG"]
    assert rec["direction"] == "short", rec
    assert abs(rec["stop_price"] - 108.0) < 1e-6, rec        # 100 + 2.0*4, stop ABOVE


def test_shorting_disabled_no_short():
    _reset(); _set_sig(bearish_cross=True)
    strategy.config.ENABLE_SHORTING = False
    # cautious, so the assertion proves ENABLE_SHORTING did the blocking rather
    # than SHORT_MIN_REGIME quietly doing it instead.
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0,
                            is_momentum=False, momentum_generation="",
                            regime="cautious")
    assert _sides("sell_short") == [], "ENABLE_SHORTING=False disables shorting"
    assert strategy._regime_short_blocks == 0, "master switch must block first"
    assert strategy._shorting_disabled_blocks == 1, \
        "the master switch must COUNT what it suppressed"


def test_shorting_disabled_block_is_observable():
    """The master-switch suppression must be visible, not inferred.

    Until 2026-09-14 `ENABLE_SHORTING` sat in the short branch's elif condition,
    so a suppressed short produced no log line and no counter. The 2026-09-14
    CRWV death cross sustained 30.2 min and fired with every other gate open,
    and the suppression had to be reconstructed from the ABSENCE of a SHORT
    ENTRY line. This pins that a fired-and-suppressed short now says so.
    """
    _reset(); _set_sig(bearish_cross=True)
    strategy.config.ENABLE_SHORTING = False
    msgs, orig_log = _capture_logs()
    try:
        strategy.evaluate_stock("CRWV", "ACCT", [], 100000.0,
                                is_momentum=False, momentum_generation="",
                                regime="risk_on")
    finally:
        strategy.logger.info = orig_log
    assert _sides("sell_short") == []
    line = next((m for m in msgs if "SHORTING DISABLED" in m), None)
    assert line is not None, f"suppression must be logged, not silent: {msgs}"
    assert "CRWV" in line and "#1" in line, line


def _poll_short_suppression(symbol, polls, regime="cautious"):
    """Drive N cycles of evaluate_stock on a live death cross for `symbol`."""
    for _ in range(polls):
        strategy.evaluate_stock(symbol, "ACCT", [], 100000.0,
                                is_momentum=False, momentum_generation="",
                                regime=regime)


def test_shorting_disabled_counts_one_per_cross_not_per_poll():
    """The counter's unit is a SIGNAL, not a poll.

    2026-09-15: one NVDA death cross held from 11:17 ET to the close and read
    264 — the 60s poll count, not the signal count. The "edge" it sits behind is
    a BAR-level edge (indicators.py: prev bar on the far side, current bar
    across), and on the daily timeframe the prior bar stays on the far side all
    session, so the edge is true on every poll of the day it fired. 264 reads as
    264 forgone shorts when the honest number is 1, which would badly overstate
    the case for reopening the ENABLE_SHORTING gate.
    """
    _reset(); _set_sig(bearish_cross=True)
    strategy.config.ENABLE_SHORTING = False
    msgs, orig_log = _capture_logs()
    try:
        _poll_short_suppression("NVDA", 264)       # the real 2026-09-15 poll count
    finally:
        strategy.logger.info = orig_log
    assert _sides("sell_short") == [], "still no short, obviously"
    assert strategy._shorting_disabled_blocks == 1, \
        f"one cross = one increment, got {strategy._shorting_disabled_blocks}"
    # The LOG stays per-poll on purpose — that is how you see the signal is
    # still live right now — so the line count is the poll rate and the #N in it
    # is the signal count.
    lines = [m for m in msgs if "SHORTING DISABLED" in m]
    assert len(lines) == 264, f"log line should still fire every poll: {len(lines)}"
    assert lines[-1].endswith("#1"), lines[-1]


def test_shorting_disabled_counts_each_symbol_separately():
    """A second name suppressed is a second forgone short."""
    _reset(); _set_sig(bearish_cross=True)
    strategy.config.ENABLE_SHORTING = False
    _poll_short_suppression("NVDA", 30)
    _poll_short_suppression("AAPL", 30)
    assert strategy._shorting_disabled_blocks == 2, \
        f"two distinct crosses = 2, got {strategy._shorting_disabled_blocks}"


def test_shorting_disabled_dedupe_survives_interleaved_symbols():
    """Regression against a single-slot "last counted symbol" dedupe.

    The cycle loop walks the whole watchlist every poll, so two names with live
    death crosses alternate NVDA, AAPL, NVDA, AAPL... A "did the symbol change
    since last time?" check returns True on every one of those and counts polls
    again, just twice as fast. Only a per-(symbol, direction) key is correct.
    """
    _reset(); _set_sig(bearish_cross=True)
    strategy.config.ENABLE_SHORTING = False
    for _ in range(20):
        _poll_short_suppression("NVDA", 1)
        _poll_short_suppression("AAPL", 1)
    assert strategy._shorting_disabled_blocks == 2, \
        f"still two crosses, not 40 polls, got {strategy._shorting_disabled_blocks}"


def test_shorting_disabled_recounts_a_genuinely_new_cross():
    """Dedupe must not swallow a real second signal.

    The cross lapsing is what ends an episode, so a cross that clears and later
    re-forms is a NEW forgone short and counts again. Without this the counter
    would undercount to 1 forever per symbol per process.
    """
    _reset(); _set_sig(bearish_cross=True)
    strategy.config.ENABLE_SHORTING = False
    _poll_short_suppression("NVDA", 10)
    assert strategy._shorting_disabled_blocks == 1
    _set_sig(bearish_cross=False)                  # cross lapses -> episode over
    _poll_short_suppression("NVDA", 3)
    assert strategy._shorting_disabled_blocks == 1, "no cross, no new count"
    _set_sig(bearish_cross=True)                   # a genuinely new cross
    _poll_short_suppression("NVDA", 10)
    assert strategy._shorting_disabled_blocks == 2, \
        f"re-formed cross is a new signal, got {strategy._shorting_disabled_blocks}"


def test_shorting_disabled_not_counted_when_regime_blocks_anyway():
    """The counter measures what the FLAG alone suppressed.

    In defensive/crisis `block_new_entries` stops the entry whatever the master
    switch says, so counting those would credit ENABLE_SHORTING with
    suppressions it did not cause and overstate the case for keeping the gate.
    """
    _reset(); _set_sig(bearish_cross=True)
    strategy.config.ENABLE_SHORTING = False
    strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0,
                            is_momentum=False, momentum_generation="",
                            regime="crisis")
    assert _sides("sell_short") == []
    assert strategy._shorting_disabled_blocks == 0, \
        "regime already blocked it — not attributable to the master switch"


def test_shorting_disabled_still_covers_an_open_short():
    """ENABLE_SHORTING is an ENTRY gate, never a management gate.

    When it was flipped to False on 2026-08-03 there was a live AVGO short
    (held=-125). If the master switch had also sat on the cover branch, that
    position would have been stranded — no exit signal, riding indefinitely on a
    trailing stop nobody could close out. Same class of bug as the options
    occ_symbol regression that killed the only options trade ever placed.

    Bullish state + RSI below overbought = cover, with shorting disabled.
    """
    _reset(); _set_sig(rsi=55.0)                    # ema_short > ema_long => bullish
    strategy.config.ENABLE_SHORTING = False
    # _reset pins get_quote at 10_000 so a LONG stop never breaches; for a SHORT
    # the stop sits ABOVE entry, so that same price trips it and the stop path
    # covers before the signal path is reached. Turn the stop off to isolate the
    # branch under test — that stops are also ungated is covered in test_stops.py.
    strategy.config.USE_TRAILING_STOP = False
    positions = [{"symbol": "AVGO", "quantity": -125, "cost_basis": 390.0}]
    strategy.evaluate_stock("AVGO", "ACCT", positions, 100000.0,
                            is_momentum=False, momentum_generation="",
                            regime="risk_on")
    assert _sides("buy_to_cover") == [("AVGO", "buy_to_cover", 125)], _orders
    assert strategy._short_covers == 1, "cover counter must still increment"


def test_short_respects_max_positions():
    _reset(); _set_sig(bearish_cross=True)
    full = [{"symbol": f"S{i}", "quantity": 1, "cost_basis": 100.0}
            for i in range(strategy.config.MAX_POSITIONS)]
    strategy.evaluate_stock("AAPL", "ACCT", full, 100000.0,
                            is_momentum=False, momentum_generation="",
                            regime="cautious")
    assert _sides("sell_short") == [], "max positions blocks a new short"


def test_short_blocked_in_risk_on_regime():
    """The SHORT_MIN_REGIME gate, at the evaluate_stock level: an otherwise
    perfect death-cross short does not fire in risk_on, increments the counter,
    and arms no stop record.

    Pins SHORT_MIN_REGIME="cautious" rather than reading the live config. This
    test describes the MECHANISM of a floor above risk_on; the deployed floor is
    a policy that moves (it went to "risk_on" on 2026-08-03, which made this fail).
    A test that silently changes meaning when a config value is retuned is not
    testing anything.
    """
    orig = strategy.config.SHORT_MIN_REGIME
    try:
        strategy.config.SHORT_MIN_REGIME = "cautious"
        _reset(); _set_sig(bearish_cross=True, close=100.0)
        strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0,
                                is_momentum=False, momentum_generation="",
                                regime="risk_on")
        assert _sides("sell_short") == [], f"risk_on must block the short, got {_orders}"
        assert strategy._short_entries == 0, "blocked short must not count as an entry"
        assert strategy._regime_short_blocks == 1, "regime block must be counted"
        assert "AAPL" not in strategy._load_stops(), "no stop armed for a blocked short"
    finally:
        strategy.config.SHORT_MIN_REGIME = orig


def test_short_fires_in_risk_on_when_floor_is_risk_on():
    """The deployed policy as of 2026-08-03: floor "risk_on" makes the filter a
    no-op, so the same death cross that is blocked above now fires."""
    orig = strategy.config.SHORT_MIN_REGIME
    try:
        strategy.config.SHORT_MIN_REGIME = "risk_on"
        _reset(); _set_sig(bearish_cross=True, close=100.0)
        strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0,
                                is_momentum=False, momentum_generation="",
                                regime="risk_on")
        assert _sides("sell_short") == [("AAPL", "sell_short", 50)], _orders
        assert strategy._short_entries == 1
        assert strategy._regime_short_blocks == 0, "no block at a risk_on floor"
    finally:
        strategy.config.SHORT_MIN_REGIME = orig


def test_regime_filter_off_allows_risk_on_short():
    """Master switch OFF ⇒ the same risk_on death cross that is blocked above
    fires normally, and the block counter stays at zero."""
    _reset(); _set_sig(bearish_cross=True, close=100.0)
    orig = strategy.config.ENABLE_REGIME_SHORT_FILTER
    try:
        strategy.config.ENABLE_REGIME_SHORT_FILTER = False
        strategy.evaluate_stock("AAPL", "ACCT", [], 100000.0,
                                is_momentum=False, momentum_generation="",
                                regime="risk_on")
        assert _sides("sell_short") == [("AAPL", "sell_short", 50)], _orders
        assert strategy._short_entries == 1
        assert strategy._regime_short_blocks == 0, "filter off ⇒ no blocks counted"
    finally:
        strategy.config.ENABLE_REGIME_SHORT_FILTER = orig


def test_existing_short_not_an_entry_attempt_under_regime_gate():
    """The gate is ENTRY-only — it never reaches a held position. With a short
    already open in risk_on, no NEW short is opened and the block counter stays
    at zero (held != flat, so the entry branch is not evaluated at all).

    Deliberately does NOT assert on buy_to_cover: this module's mock quote is
    10000 against a cost basis of 100, so the bootstrapped trailing stop fires
    immediately. That is the stop machinery behaving correctly and is covered in
    test_stops.py — asserting it here would test the fixture, not the gate."""
    _reset(); _set_sig(bearish_cross=True, close=100.0)
    positions = [{"symbol": "AAPL", "quantity": -50, "cost_basis": 5000.0}]
    strategy.evaluate_stock("AAPL", "ACCT", positions, 100000.0,
                            is_momentum=False, momentum_generation="",
                            regime="risk_on")
    assert _sides("sell_short") == [], "held short must not be added to"
    assert strategy._short_entries == 0
    assert strategy._regime_short_blocks == 0, "held short is not an entry attempt"


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
