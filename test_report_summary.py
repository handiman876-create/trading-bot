"""
Tests for the TOTAL P&L summary block and the disabled-feature markers.

WHY THIS EXISTS: the report printed per-feature totals that summed to
-$34,587.94 while the account-level realized figure was -$36,296.78, and
nothing on the page reconciled the two — the difference is 2 hand-placed
correction trips that _aggregate deliberately excludes. Readers had to
reconstruct the real number by hand.

Separately, momentum_alignment was disabled in 40a34a3 but the report still
rendered it as "INSUFFICIENT DATA (<10 trades)". Its book can never reach 10,
so that read as "verdict pending" when the verdict was in and the feature was
already off.
"""

import config
import performance_analyzer as pa


def _trip(feature, pnl, symbol="AAA", reason="signal", win=None):
    return {
        "symbol": symbol, "direction": "long", "feature": feature,
        "qty": 1, "entry_price": 100.0, "exit_price": 100.0 + pnl,
        "entry_ts": "2026-07-01 09:30:00 EDT", "exit_ts": "2026-07-02 09:30:00 EDT",
        "exit_reason": reason, "pnl": pnl,
        "win": (pnl > 0) if win is None else win,
        "estimated_entry": False,
    }


# ── TOTAL P&L block ───────────────────────────────────────────────────────────

def test_realized_is_all_in_including_corrections():
    """The headline realized number is account-level: every closed trip."""
    trips = [_trip("long_fresh_cross", -100.0),
             _trip("momentum_alignment", -50.0, reason="correction")]
    t = pa._totals(trips, {"pnl": 0.0}, {})
    assert t["realized"] == -150.0
    assert t["realized_strategy"] == -100.0
    assert t["correction_pnl"] == -50.0
    assert t["correction_trips"] == 1


def test_total_is_realized_plus_open():
    t = pa._totals([_trip("long_fresh_cross", -100.0)], {"pnl": 25.5}, {})
    assert t["open_estimate"] == 25.5
    assert t["total"] == -74.5


def test_total_is_none_when_open_leg_unavailable():
    """A missing open estimate must not silently render as 0 — that would make
    the Total line read better than reality."""
    t = pa._totals([_trip("long_fresh_cross", -100.0)], {"pnl": None}, {})
    assert t["open_estimate"] is None
    assert t["total"] is None


def test_vs_spy_is_carried_from_the_spy_block():
    t = pa._totals([], {"pnl": 0.0}, {"delta_vs_spy": -0.0225})
    assert t["vs_spy"] == -0.0225


# ── Open mark-to-market ───────────────────────────────────────────────────────

def _entry(symbol, direction, qty, price):
    return {"symbol": symbol, "direction": direction, "quantity": qty,
            "price": price, "timestamp": "2026-07-13 09:32:11 EDT",
            "role": "entry", "feature": "long_fresh_cross"}


def test_mark_prices_long_and_short_correctly(monkeypatch):
    import tradestation_client as tc
    monkeypatch.setattr(tc, "get_quote",
                        lambda s: {"NVDA": {"last": 200.0},
                                   "PLTR": {"last": 120.0}}[s], raising=False)
    m = pa._mark_open_entries([_entry("NVDA", "long", 10, 210.0),
                               _entry("PLTR", "short", 10, 123.0)])
    # long: (200-210)*10 = -100 ; short: (123-120)*10 = +30
    assert m["pnl"] == -70.0
    assert m["priced"] == 2 and m["unpriced"] == []


def test_missing_quote_is_excluded_not_zeroed(monkeypatch):
    """An unpriced position must be named, not silently contribute $0."""
    import tradestation_client as tc
    monkeypatch.setattr(tc, "get_quote",
                        lambda s: {"last": 200.0} if s == "NVDA" else None,
                        raising=False)
    m = pa._mark_open_entries([_entry("NVDA", "long", 10, 210.0),
                               _entry("ZZZZ", "long", 10, 50.0)])
    assert m["pnl"] == -100.0          # ZZZZ contributes nothing at all
    assert m["priced"] == 1
    assert m["unpriced"] == ["ZZZZ"]


def test_mark_falls_back_to_close_when_last_is_absent(monkeypatch):
    """The analyzer runs Sunday 00:07 ET — `last` may be absent out of hours."""
    import tradestation_client as tc
    monkeypatch.setattr(tc, "get_quote", lambda s: {"close": 205.0}, raising=False)
    m = pa._mark_open_entries([_entry("NVDA", "long", 10, 210.0)])
    assert m["pnl"] == -50.0


def test_no_open_entries_is_zero_not_none():
    m = pa._mark_open_entries([])
    assert m["pnl"] == 0.0 and m["positions"] == []


# ── Disabled-feature markers ──────────────────────────────────────────────────

def test_disabled_marker_reads_the_live_flag(monkeypatch):
    """State comes from the flag, not a hardcoded name list — re-enabling the
    feature in config must clear the marker with no other edit."""
    monkeypatch.setattr(config, "USE_MOMENTUM_ALIGNMENT", True, raising=False)
    assert "momentum_alignment" not in pa._disabled_features([])

    monkeypatch.setattr(config, "USE_MOMENTUM_ALIGNMENT", False, raising=False)
    assert "momentum_alignment" in pa._disabled_features([])


def test_disabled_stats_are_all_in(monkeypatch):
    """The retirement figure is the full bucket — the number the disabling commit
    quotes — while _aggregate stays correction-free for live features."""
    monkeypatch.setattr(config, "USE_MOMENTUM_ALIGNMENT", False, raising=False)
    trips = [_trip("momentum_alignment", -1000.0),
             _trip("momentum_alignment", 200.0),
             _trip("momentum_alignment", -500.0, reason="correction")]
    d = pa._disabled_features(trips)["momentum_alignment"]
    assert d["trips"] == 3 and d["wins"] == 1
    assert d["total_pnl"] == -1300.0        # all-in
    assert d["correction_trips"] == 1
    # _aggregate over the same trips excludes the correction trip
    assert pa._aggregate(trips)["momentum_alignment"]["total_pnl"] == -800.0


def test_disabled_note_carries_when_and_why(monkeypatch):
    monkeypatch.setattr(config, "USE_MOMENTUM_ALIGNMENT", False, raising=False)
    d = pa._disabled_features([])["momentum_alignment"]
    assert d["since"] == "2026-07-24"
    assert d["commit"] == "40a34a3"
    assert d["reason"]


def test_flag_off_without_a_note_still_marks(monkeypatch):
    """A missing note must not suppress the DISABLED marker itself."""
    monkeypatch.setattr(config, "FEATURE_FLAGS", {"short": "USE_SHORTS_X"}, raising=False)
    monkeypatch.setattr(config, "FEATURE_DISABLED_NOTES", {}, raising=False)
    monkeypatch.setattr(config, "USE_SHORTS_X", False, raising=False)
    d = pa._disabled_features([])
    assert "short" in d and d["short"]["trips"] == 0


def test_unknown_flag_defaults_to_enabled(monkeypatch):
    """A note naming a flag that does not exist must not mark a live feature
    disabled — default True, so absence of evidence is not evidence of absence."""
    monkeypatch.setattr(config, "FEATURE_FLAGS", {"short": "NO_SUCH_FLAG"}, raising=False)
    assert pa._disabled_features([]) == {}


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render(monkeypatch):
    monkeypatch.setattr(config, "USE_MOMENTUM_ALIGNMENT", False, raising=False)
    trips = [_trip("long_fresh_cross", -100.0)] * 10 + \
            [_trip("momentum_alignment", -1000.0)] * 10 + \
            [_trip("momentum_alignment", -500.0, reason="correction")]
    report = {
        "generated": "2026-07-26 20:00:00 EDT",
        "scope": "test", "ledger_span": ["2026-06-09", "2026-07-24"],
        "per_feature": pa._aggregate(trips),
        "disabled": pa._disabled_features(trips),
        "totals": pa._totals(trips, {"pnl": -227.56}, {"delta_vs_spy": -0.0225}),
        "open_mark": {"pnl": -227.56, "priced": 5, "unpriced": [], "positions": []},
        "spy": {"available": False, "reason": "n/a"},
        "warnings": [],
        "data_quality": {"closed_trips": len(trips), "open_entries": 5,
                         "files_parsed": [], "parse_errors": [],
                         "estimated_entry_trips_closed": 0, "estimated_entry_open": 0,
                         "orphan_exits_missing_entry": [], "reconciled_entries": [],
                         "correction_trips_excluded": 1, "stale_pre_analyzer_entries": 0,
                         "bootstrap_injected": 0, "new_events_added": 0},
    }
    return pa.render_txt(report)


def test_render_includes_total_block(monkeypatch):
    out = _render(monkeypatch)
    assert "=== TOTAL P&L ===" in out
    assert "Realized:" in out and "Open (est.):" in out
    assert "Total:" in out and "vs SPY:" in out
    # The block precedes the feature breakdown.
    assert out.index("=== TOTAL P&L ===") < out.index("PER-FEATURE")


def test_render_marks_disabled_feature(monkeypatch):
    out = _render(monkeypatch)
    assert "MOMENTUM ALIGNMENT: DISABLED" in out
    assert "disabled 2026-07-24, commit 40a34a3" in out
    assert "10-for-11" not in out          # 0 wins here, not a copy of live data
    assert "0-for-11" in out
    # and it must NOT still claim insufficient data
    disabled_line = [l for l in out.splitlines() if "MOMENTUM ALIGNMENT" in l][0]
    assert "INSUFFICIENT DATA" not in disabled_line


def test_render_flags_partial_open_estimate(monkeypatch):
    monkeypatch.setattr(config, "USE_MOMENTUM_ALIGNMENT", False, raising=False)
    out = _render(monkeypatch)
    assert "PARTIAL" not in out            # nothing unpriced in this fixture


if __name__ == "__main__":
    print("run under pytest (uses monkeypatch)")
