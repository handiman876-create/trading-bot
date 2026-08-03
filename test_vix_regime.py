"""
Unit tests for the VIX fear-gauge / market-regime filter — NO network.

Covers the pure regime mapping at every boundary, the entry-gate derivation, the
5-minute quote cache, the fail-OPEN path, per-cycle transition logging, the
defensive stop tighten, and the crisis de-risking path (shadow vs armed).

All doubles are in-process: tc.get_vix_level / get_quote / place_equity_order and
log_trade are stubbed, and _STOPS_PATH is redirected to a temp file, so the suite
never touches the network or live state.

Run:  python3 test_vix_regime.py   (or via pytest)
"""

import logging
import os
import tempfile

import _testlib
import config
import strategy


# ── log capture (works under pytest AND the __main__ runner) ──────────────────
class _LogCap:
    def __enter__(self):
        self.records = []
        self._h = logging.Handler()
        self._h.emit = lambda r: self.records.append(r.getMessage())
        self._prev = strategy.logger.level
        strategy.logger.addHandler(self._h)
        strategy.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        strategy.logger.removeHandler(self._h)
        strategy.logger.setLevel(self._prev)

    @property
    def text(self):
        return "\n".join(self.records)


def _reset_regime_state():
    strategy._vix_cache = {"ts": None, "vix": None, "regime": "risk_on"}
    strategy._last_logged_regime = None
    for k in strategy._regime_counts:
        strategy._regime_counts[k] = 0
    strategy._crisis_exits = 0
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()


# ── 1. pure mapping at every boundary ─────────────────────────────────────────
def test_regime_boundaries():
    R = strategy._get_market_regime
    # risk_on below 20
    assert R(0.0)   == "risk_on"
    assert R(19.99) == "risk_on"
    # cautious 20–25
    assert R(20.0)  == "cautious"
    assert R(24.99) == "cautious"
    # defensive 25–30
    assert R(25.0)  == "defensive"
    assert R(29.99) == "defensive"
    # crisis >=30 (incl. the extreme sub-tier)
    assert R(30.0)  == "crisis"
    assert R(34.99) == "crisis"
    assert R(35.0)  == "crisis"
    assert R(100.0) == "crisis"
    # unknown
    assert R(None)  == "unknown"


def test_is_extreme():
    assert strategy._is_extreme(34.99) is False
    assert strategy._is_extreme(35.0) is True
    assert strategy._is_extreme(41.0) is True
    assert strategy._is_extreme(None) is False


# ── 2. entry-gate derivation (the centralized rule table) ─────────────────────
def test_apply_regime_rules():
    """(block_new_entries, block_momentum_align, block_shorts) with the floor
    PINNED to "cautious". The deployed SHORT_MIN_REGIME is a policy that moves —
    it went to "risk_on" on 2026-08-03 — so reading it from live config here would
    make this test change meaning whenever the policy is retuned."""
    orig = config.SHORT_MIN_REGIME
    try:
        config.SHORT_MIN_REGIME = "cautious"
        assert strategy._apply_regime_rules("risk_on")   == (False, False, True)
        assert strategy._apply_regime_rules("cautious")  == (False, True,  False)
        assert strategy._apply_regime_rules("defensive") == (True,  True,  False)
        assert strategy._apply_regime_rules("crisis")    == (True,  True,  False)
        # Longs fail-OPEN on unknown (a VIX glitch never blocks a buy); shorts
        # fail-CLOSED (unknown ranks with risk_on, so it blocks). Asymmetric on
        # purpose — failing open on a short is how you get short into a rally.
        assert strategy._apply_regime_rules("unknown")   == (False, False, True)
    finally:
        config.SHORT_MIN_REGIME = orig


def test_apply_regime_rules_at_the_deployed_risk_on_floor():
    """The 2026-08-03 policy: floor "risk_on" makes the short filter a no-op, and
    with it the fail-CLOSED-on-unknown property above. Documented here so the
    trade-off is visible rather than implicit in a config edit."""
    orig = config.SHORT_MIN_REGIME
    try:
        config.SHORT_MIN_REGIME = "risk_on"
        assert strategy._apply_regime_rules("risk_on")[2] is False
        # unknown now PERMITS shorts — a VIX outage no longer blocks them.
        assert strategy._apply_regime_rules("unknown")[2] is False
    finally:
        config.SHORT_MIN_REGIME = orig


def test_short_min_regime_gate_is_configurable():
    """SHORT_MIN_REGIME moves the floor; 'risk_on' restores pre-gate behaviour."""
    orig = config.SHORT_MIN_REGIME
    try:
        config.SHORT_MIN_REGIME = "risk_on"
        for r in ("risk_on", "cautious", "defensive", "crisis", "unknown"):
            assert strategy._apply_regime_rules(r)[2] is False, r

        config.SHORT_MIN_REGIME = "defensive"
        assert strategy._apply_regime_rules("cautious")[2] is True
        assert strategy._apply_regime_rules("defensive")[2] is False
    finally:
        config.SHORT_MIN_REGIME = orig


def test_regime_short_filter_master_switch_off():
    """ENABLE_REGIME_SHORT_FILTER=False restores pre-filter behaviour exactly:
    block_shorts is False in EVERY regime, and the other two gates are untouched."""
    orig = config.ENABLE_REGIME_SHORT_FILTER
    try:
        config.ENABLE_REGIME_SHORT_FILTER = False
        assert strategy._apply_regime_rules("risk_on")   == (False, False, False)
        assert strategy._apply_regime_rules("cautious")  == (False, True,  False)
        assert strategy._apply_regime_rules("defensive") == (True,  True,  False)
        assert strategy._apply_regime_rules("crisis")    == (True,  True,  False)
        assert strategy._apply_regime_rules("unknown")   == (False, False, False)
    finally:
        config.ENABLE_REGIME_SHORT_FILTER = orig


def test_crisis_still_blocks_every_entry_including_shorts():
    """The pre-existing crisis rule is unchanged: block_new_entries closes crisis
    to ALL entries, so a crisis short is blocked by the OLD gate, not the new one.
    block_shorts reads False there — that is correct and not a regression."""
    block_new, _, block_shorts = strategy._apply_regime_rules("crisis")
    assert block_new is True, "crisis must still block all new entries"
    assert block_shorts is False, "crisis shorts are stopped by block_new_entries"
    block_new, _, block_shorts = strategy._apply_regime_rules("defensive")
    assert block_new is True, "defensive must still block all new entries"
    assert block_shorts is False


def _short_openable_regimes():
    return [r for r in ("risk_on", "cautious", "defensive", "crisis", "unknown")
            if not strategy._apply_regime_rules(r)[0]      # entries allowed
            and not strategy._apply_regime_rules(r)[2]]    # shorts allowed


def test_defensive_and_crisis_already_block_all_entries():
    """The documented consequence at SHORT_MIN_REGIME='cautious': cautious is the
    ONLY regime where a new short can fire, because defensive/crisis are already
    closed by block_new_entries. If this fails, the config comment claiming
    'shorts fire only while 20 <= VIX < 25' has gone stale.

    Pinned, not read from live config — the deployed floor moved to 'risk_on' on
    2026-08-03 and this assertion is about the cautious-floor semantics."""
    orig = config.SHORT_MIN_REGIME
    try:
        config.SHORT_MIN_REGIME = "cautious"
        assert _short_openable_regimes() == ["cautious"], _short_openable_regimes()
    finally:
        config.SHORT_MIN_REGIME = orig


def test_risk_on_floor_opens_shorts_in_risk_on_and_unknown():
    """The deployed 2026-08-03 policy, stated explicitly: dropping the floor to
    risk_on widens the short window from {cautious} to {risk_on, cautious,
    unknown} — including 'unknown', i.e. shorts now fire during a VIX outage."""
    orig = config.SHORT_MIN_REGIME
    try:
        config.SHORT_MIN_REGIME = "risk_on"
        assert _short_openable_regimes() == ["risk_on", "cautious", "unknown"], \
            _short_openable_regimes()
    finally:
        config.SHORT_MIN_REGIME = orig


# ── 3. cache: one fetch per VIX_CACHE_SECONDS window ──────────────────────────
def test_current_regime_caches_five_minutes():
    _reset_regime_state()
    calls = {"n": 0}

    def _fake_vix():
        calls["n"] += 1
        return 18.7

    strategy.tc.get_vix_level = _fake_vix
    try:
        vix, regime = strategy.current_regime(now=1000.0)
        assert (vix, regime) == (18.7, "risk_on")
        assert calls["n"] == 1
        # within the 300s window → served from cache, no refetch
        strategy.current_regime(now=1000.0 + 299)
        assert calls["n"] == 1, "cache hit should not refetch"
        # just past the window → refetch
        strategy.current_regime(now=1000.0 + 301)
        assert calls["n"] == 2, "stale cache should refetch"
    finally:
        _reset_regime_state()


# ── 4. fail-OPEN when the quote is unavailable ────────────────────────────────
def test_current_regime_fails_open_on_none():
    _reset_regime_state()
    strategy.tc.get_vix_level = lambda: None
    try:
        vix, regime = strategy.current_regime(now=2000.0)
        assert vix is None
        assert regime == "unknown"
        # 'unknown' gates like risk_on for LONGS (no blocks) but blocks shorts —
        # only while the floor sits above risk_on, so pin it (deployed floor is
        # "risk_on" as of 2026-08-03, under which unknown permits shorts; that
        # case is covered by test_risk_on_floor_opens_shorts_in_risk_on_and_unknown).
        orig = config.SHORT_MIN_REGIME
        try:
            config.SHORT_MIN_REGIME = "cautious"
            assert strategy._apply_regime_rules(regime) == (False, False, True)
        finally:
            config.SHORT_MIN_REGIME = orig
    finally:
        _reset_regime_state()


# ── 5. master switch off ⇒ always risk_on, no fetch ───────────────────────────
def test_filter_disabled_forces_risk_on_without_fetching():
    _reset_regime_state()
    calls = {"n": 0}
    strategy.tc.get_vix_level = lambda: calls.__setitem__("n", calls["n"] + 1) or 99.0
    prev = config.ENABLE_VIX_FILTER
    config.ENABLE_VIX_FILTER = False
    try:
        vix, regime = strategy.current_regime(now=3000.0)
        assert (vix, regime) == (None, "risk_on")
        assert calls["n"] == 0, "disabled filter must not call the quote API"
    finally:
        config.ENABLE_VIX_FILTER = prev
        _reset_regime_state()


# ── 6. per-cycle logging: level line + mode line ──────────────────────────────
def test_note_regime_logs_level_and_mode_lines():
    _reset_regime_state()
    with _LogCap() as cap:
        strategy.note_regime(22.3, "cautious")
    assert "VIX=22.3 regime=cautious" in cap.text
    assert "CAUTIOUS MODE - skipping momentum alignment (VIX=22.3)" in cap.text

    _reset_regime_state()
    with _LogCap() as cap:
        strategy.note_regime(27.0, "defensive")
    assert "DEFENSIVE MODE - no new entries (VIX=27.0)" in cap.text

    # extreme tag on the level line
    _reset_regime_state()
    with _LogCap() as cap:
        strategy.note_regime(38.0, "crisis")
    assert "regime=crisis EXTREME" in cap.text
    assert "CRISIS MODE EXTREME" in cap.text
    assert "de-risking momentum slot" in cap.text


# ── 7. transition logging across all four regimes ─────────────────────────────
def test_all_four_transitions_log_once_each():
    _reset_regime_state()
    seq = [(18.0, "risk_on"),   # first call: no transition (no prior)
           (22.0, "cautious"),  # risk_on -> cautious
           (22.5, "cautious"),  # no change: no transition line
           (27.0, "defensive"), # cautious -> defensive
           (33.0, "crisis"),    # defensive -> crisis
           (17.0, "risk_on")]   # crisis -> risk_on
    with _LogCap() as cap:
        for vix, regime in seq:
            strategy.note_regime(vix, regime)
    text = cap.text
    assert "REGIME TRANSITION risk_on -> cautious" in text
    assert "REGIME TRANSITION cautious -> defensive" in text
    assert "REGIME TRANSITION defensive -> crisis" in text
    assert "REGIME TRANSITION crisis -> risk_on" in text
    # exactly four transitions (the repeat cautious->cautious must NOT log one)
    assert text.count("REGIME TRANSITION") == 4
    # counters ticked once per note_regime call
    assert strategy._regime_counts["cautious"] == 2
    assert strategy._regime_counts["crisis"] == 1
    _reset_regime_state()


# ── 8. defensive stop tighten (1.5x vs 2.5x ATR on a >3% loser) ───────────────
def _fake_quote(price):
    return lambda symbol: {"last": price, "close": price}


def test_defensive_tightens_stop_on_a_loser():
    strategy.tc.get_quote = _fake_quote(96.0)           # down 4% from entry 100
    strategy.tc.place_equity_order = lambda *a, **k: None
    _testlib.safe_remove(strategy._STOPS_PATH)
    rec = {"entry_price": 100.0, "atr_at_entry": 4.0, "high_water": 100.0,
           "stop_price": 90.0, "opened": "2026-07-17", "bootstrapped": False,
           "direction": "long"}
    sig = {"close": 96.0, "atr": 4.0}

    # risk_on: normal 2.5x ATR → stop = high_water(100) - 2.5*4 = 90.0
    strategy._save_stops({"AAA": dict(rec)})
    strategy._check_and_trail_stop("AAA", 10, sig, "ACCT", [], regime="risk_on")
    normal_stop = strategy._load_stops()["AAA"]["stop_price"]

    # defensive + down >3%: 1.5x ATR → stop = 100 - 1.5*4 = 94.0 (tighter/higher)
    strategy._save_stops({"AAA": dict(rec)})
    strategy._check_and_trail_stop("AAA", 10, sig, "ACCT", [], regime="defensive")
    tight_stop = strategy._load_stops()["AAA"]["stop_price"]

    assert normal_stop == 90.0, normal_stop
    assert tight_stop == 94.0, tight_stop
    assert tight_stop > normal_stop, "defensive stop must be tighter (higher)"


# ── 9. crisis momentum exit — routed through evaluate_stock's SELL path ───────
def _drive_crisis_stock(is_momentum, shadow, held=221, price=230.0):
    """Run evaluate_stock in crisis with a BULLISH state and a price ABOVE the stop
    (so neither the trailing stop nor the normal bearish SELL fires — any exit is
    therefore the crisis momentum branch). Returns the captured order list."""
    orders = []
    strategy.tc.get_historical = lambda *a, **k: [{"bar": 1}]
    strategy.ind.compute_indicators = lambda *a, **k: {
        "close": price, "ema_short": 105.0, "ema_long": 100.0, "rsi": 55.0,
        "bullish_cross": False, "bearish_cross": False, "atr": 4.0}
    strategy.tc.place_equity_order = lambda acct, sym, side, qty: \
        orders.append((sym, side, qty)) or {"order": {"id": "X"}}
    strategy.tc.get_quote = _fake_quote(price)
    strategy.log_trade = lambda *a, **k: None
    _testlib.safe_remove(strategy._STOPS_PATH)
    strategy._save_stops({"CRL": {"entry_price": 225.0, "stop_price": 207.0,
                                  "direction": "long", "atr_at_entry": 8.0,
                                  "high_water": 229.0}})
    strategy._signaled_buy_today.clear()
    strategy._signaled_sell_today.clear()
    strategy._crisis_exits = 0
    prev = config.VIX_CRISIS_SHADOW
    config.VIX_CRISIS_SHADOW = shadow
    try:
        strategy.evaluate_stock("CRL", "ACCT", [{"symbol": "CRL", "quantity": held}],
                                100000.0, is_momentum=is_momentum, regime="crisis")
    finally:
        config.VIX_CRISIS_SHADOW = prev
    return orders


def test_crisis_shadow_places_no_order():
    orders = _drive_crisis_stock(is_momentum=True, shadow=True)
    assert orders == [], "shadow crisis must place NO order"
    assert "CRL" in strategy._load_stops(), "shadow leaves the position/stop intact"
    assert strategy._crisis_exits == 0


def test_crisis_armed_exits_momentum_via_sell_path():
    orders = _drive_crisis_stock(is_momentum=True, shadow=False)
    assert ("CRL", "sell", 221) in orders, "armed crisis sells held momentum via SELL path"
    assert "CRL" not in strategy._load_stops(), "stop cleared on exit"
    assert strategy._crisis_exits == 1, "crisis-exit counter incremented"


def test_crisis_keeps_core_names():
    # a CORE (non-momentum) name is NOT force-exited by crisis (bullish state, armed)
    orders = _drive_crisis_stock(is_momentum=False, shadow=False)
    assert orders == [], "crisis must keep core positions (no forced exit)"


# ── 10. crisis breakeven floor — in the stop trailer, armed-only ──────────────
def test_crisis_breakeven_floor_in_stop_trailer():
    strategy.tc.get_quote = _fake_quote(215.0)           # underwater vs entry 225
    strategy.tc.place_equity_order = lambda *a, **k: {"order": {"id": "X"}}
    strategy.log_trade = lambda *a, **k: None
    sig = {"close": 215.0, "atr": 8.0}
    base = {"entry_price": 225.0, "atr_at_entry": 8.0, "high_water": 229.0,
            "stop_price": 207.0, "opened": "2026-07-17", "bootstrapped": False,
            "direction": "long"}

    # shadow: no floor → stop trails normally (well below entry), position survives
    prev = config.VIX_CRISIS_SHADOW
    config.VIX_CRISIS_SHADOW = True
    _testlib.safe_remove(strategy._STOPS_PATH)
    strategy._save_stops({"CRL": dict(base)})
    exited = strategy._check_and_trail_stop("CRL", 221, sig, "ACCT", [], regime="crisis")
    assert not exited, "shadow crisis does not floor the stop"
    assert strategy._load_stops()["CRL"]["stop_price"] < 225.0

    # armed: stop floored to breakeven (entry 225) → 215 < 225 breaches → exit
    config.VIX_CRISIS_SHADOW = False
    _testlib.safe_remove(strategy._STOPS_PATH)
    strategy._save_stops({"CRL": dict(base)})
    exited = strategy._check_and_trail_stop("CRL", 221, sig, "ACCT", [], regime="crisis")
    config.VIX_CRISIS_SHADOW = prev
    assert exited, "armed crisis floors stop to breakeven → underwater long exits"


if __name__ == "__main__":
    _tmpdir = tempfile.mkdtemp(prefix="vix_test_")
    strategy._STOPS_PATH = os.path.join(_tmpdir, "stop_prices.json")
    _orig = {
        "vix": getattr(strategy.tc, "get_vix_level", None),
        "quote": strategy.tc.get_quote,
        "place": strategy.tc.place_equity_order,
        "logtrade": strategy.log_trade,
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
        print(f"All {passed} tests passed.")
    finally:
        if _orig["vix"] is not None:
            strategy.tc.get_vix_level = _orig["vix"]
        strategy.tc.get_quote = _orig["quote"]
        strategy.tc.place_equity_order = _orig["place"]
        strategy.log_trade = _orig["logtrade"]
