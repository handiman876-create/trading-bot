"""
Tests for the A/B screen experiment: profitability filter, Screen B selection,
realized vol, the tracker's record/measure cycle, IV=None handling, and the
performance-report section.

    pytest test_screen_ab.py

All external I/O (Polygon financials, option IV, grouped-daily collection) is
monkeypatched — the suite makes zero network calls and, via conftest, writes only
to a per-test tmp dir.
"""

import json

import config
import fundamentals
import momentum_screen as ms
import performance_analyzer as pa
import polygon_client as pc
import screen_ab_tracker as tracker


# ── helpers ───────────────────────────────────────────────────────────────────

def _ranked(n):
    """A descending momentum ranking of n synthetic survivors."""
    return [{"symbol": f"S{i}", "return_20d": round(1.0 - i * 0.01, 4),
             "rsi": 60.0, "rel_volume": 1.2} for i in range(n)]


def _by_date(symbols, dates, close_map):
    """{date: {sym: bar}} with every symbol present on every date (so _series_for
    returns a full series). close_map[date][sym] gives the close."""
    return {ds: {s: {"open": close_map[ds][s], "high": close_map[ds][s],
                     "low": close_map[ds][s], "close": close_map[ds][s],
                     "volume": 1_000_000.0}
                 for s in symbols}
            for ds in dates}


# ── fundamentals: profitability rule ──────────────────────────────────────────

def test_profitable_quarters_counts_positive_only():
    q = [{"net_income": 5e9}, {"net_income": -3e9}, {"net_income": None},
         {"net_income": 1e9}, {"net_income": 0}]
    assert fundamentals.profitable_quarters(q) == 2   # 0 and negative and null excluded


def test_is_profitable_pass_and_fail(monkeypatch):
    def five(net_incomes):
        return [{"fiscal_period": "Q", "end_date": "2026-01-01", "net_income": v}
                for v in net_incomes]

    monkeypatch.setattr(pc, "get_quarterly_financials",
                        lambda s, limit=5: five([9, 9, 9, 9, -1]))
    assert fundamentals.is_profitable("AAA", cache={}) is True     # 4/5 positive

    monkeypatch.setattr(pc, "get_quarterly_financials",
                        lambda s, limit=5: five([9, 9, 9, -1, -1]))
    assert fundamentals.is_profitable("BBB", cache={}) is False    # only 3/5


def test_is_profitable_nulls_block_pass(monkeypatch):
    # 3 real profits + 2 nulls: nulls are NOT profits, so 3/5 < 4 → fail.
    rows = [{"net_income": v} for v in [9, 9, 9, None, None]]
    monkeypatch.setattr(pc, "get_quarterly_financials", lambda s, limit=5: rows)
    assert fundamentals.is_profitable("CCC", cache={}) is False


def test_is_profitable_too_few_quarters(monkeypatch):
    rows = [{"net_income": 9}, {"net_income": 9}, {"net_income": 9}]  # only 3 filed
    monkeypatch.setattr(pc, "get_quarterly_financials", lambda s, limit=5: rows)
    assert fundamentals.is_profitable("DDD", cache={}) is False


def test_is_profitable_fetch_failure_returns_none(monkeypatch):
    def boom(s, limit=5):
        raise pc.PolygonError("429")
    monkeypatch.setattr(pc, "get_quarterly_financials", boom)
    assert fundamentals.is_profitable("EEE", cache={}) is None


def test_is_profitable_cache_avoids_refetch(monkeypatch):
    calls = {"n": 0}

    def once(s, limit=5):
        calls["n"] += 1
        return [{"net_income": 9}] * 5
    monkeypatch.setattr(pc, "get_quarterly_financials", once)
    cache = {}
    assert fundamentals.is_profitable("FFF", cache=cache) is True
    assert fundamentals.is_profitable("FFF", cache=cache) is True
    assert calls["n"] == 1   # second call served from the cache dict


# ── Screen B selection ────────────────────────────────────────────────────────

def test_run_screen_b_takes_first_five_profitable(monkeypatch):
    ranked = _ranked(30)
    profitable = {"S0", "S2", "S5", "S9", "S20", "S25"}
    monkeypatch.setattr(fundamentals, "is_profitable",
                        lambda sym, cache=None: sym in profitable)
    monkeypatch.setattr(fundamentals, "_save_cache", lambda c: None)
    picks = ms.run_screen_b(ranked, cache={})
    assert [p["symbol"] for p in picks] == ["S0", "S2", "S5", "S9", "S20"]  # first 5, in rank order


def test_run_screen_b_none_treated_as_not_profitable(monkeypatch):
    ranked = _ranked(30)
    monkeypatch.setattr(fundamentals, "is_profitable",
                        lambda sym, cache=None: True if sym == "S1" else None)
    monkeypatch.setattr(fundamentals, "_save_cache", lambda c: None)
    picks = ms.run_screen_b(ranked, cache={})
    assert [p["symbol"] for p in picks] == ["S1"]   # None -> skipped, not smuggled in


def test_run_screen_b_respects_top_n(monkeypatch):
    ranked = _ranked(40)
    monkeypatch.setattr(fundamentals, "is_profitable",
                        lambda sym, cache=None: sym == "S35")   # profitable but past top-30
    monkeypatch.setattr(fundamentals, "_save_cache", lambda c: None)
    assert ms.run_screen_b(ranked, cache={}) == []


def test_run_screen_b_empty_when_none_profitable(monkeypatch):
    monkeypatch.setattr(fundamentals, "is_profitable", lambda sym, cache=None: False)
    monkeypatch.setattr(fundamentals, "_save_cache", lambda c: None)
    assert ms.run_screen_b(_ranked(30), cache={}) == []


# ── realized vol ──────────────────────────────────────────────────────────────

def test_realized_vol_constant_growth_is_zero():
    closes = [100 * (1.01 ** i) for i in range(25)]   # identical log returns → stdev 0
    assert ms.realized_vol(closes) == 0.0


def test_realized_vol_varied_is_positive():
    closes = [100, 108, 101, 110, 103, 112, 104, 115, 106, 118, 108, 120]
    rv = ms.realized_vol(closes)
    assert rv is not None and rv > 0


def test_realized_vol_too_short_is_none():
    assert ms.realized_vol([100, 101]) is None
    assert ms.realized_vol([]) is None


# ── return measurement + winner logic ─────────────────────────────────────────

def test_measure_returns_and_avg():
    block = {"detail": [{"symbol": "AAA", "entry_close": 100.0},
                        {"symbol": "BBB", "entry_close": 200.0}]}
    by_date = {"2026-08-15": {"AAA": {"close": 110.0}, "BBB": {"close": 190.0}}}
    res = tracker._measure_returns(block, by_date, "2026-08-15")
    assert res["AAA"] == 0.1 and res["BBB"] == -0.05
    assert res["avg"] == 0.0   # mean of +0.10 and -0.05 = 0.025 → rounds to 0.0 at 1dp


def test_measure_returns_skips_missing_exit():
    block = {"detail": [{"symbol": "AAA", "entry_close": 100.0},
                        {"symbol": "GONE", "entry_close": 50.0}]}
    by_date = {"d": {"AAA": {"close": 120.0}}}   # GONE absent
    res = tracker._measure_returns(block, by_date, "d")
    assert res["AAA"] == 0.2 and "GONE" not in res


def test_decide_winner():
    assert tracker._decide_winner({"avg": 0.05}, {"avg": 0.02}, True) == "screen_a"
    assert tracker._decide_winner({"avg": 0.01}, {"avg": 0.04}, True) == "screen_b"
    assert tracker._decide_winner({"avg": 0.03}, {"avg": 0.03}, True) == "tie"
    assert tracker._decide_winner({"avg": -0.9}, {"avg": None}, False) == "screen_a"  # B empty → A


# ── tracker record + measure cycle (end to end, mocked I/O) ────────────────────

def _wire_tracker(monkeypatch, ranked, by_date, dates, profitable):
    monkeypatch.setattr(ms, "collect_and_rank", lambda: (ranked, by_date, dates))
    monkeypatch.setattr(ms, "_load_sectors", lambda: {})
    monkeypatch.setattr(pc, "get_atm_option_iv",
                        lambda sym, underlying_price=None: None)   # tier not entitled
    monkeypatch.setattr(fundamentals, "is_profitable",
                        lambda sym, cache=None: sym in profitable)
    monkeypatch.setattr(fundamentals, "_save_cache", lambda c: None)
    monkeypatch.setattr(fundamentals, "_load_cache", lambda: {})


def test_tracker_full_cycle(monkeypatch):
    ranked = _ranked(30)
    syms = [r["symbol"] for r in ranked]
    d1 = ["2026-07-30", "2026-07-31", "2026-08-01"]
    closes1 = {ds: {s: 100.0 + i for i, s in enumerate(syms)} for ds in d1}
    _wire_tracker(monkeypatch, ranked, _by_date(syms, d1, closes1), d1,
                  profitable={"S1", "S3", "S6", "S8", "S11"})

    # Rotation 1: record only, nothing to measure yet.
    monkeypatch.setattr(tracker, "_today_et", lambda: "2026-08-01")
    assert tracker.run(dry_run=False) == 0
    doc = json.load(open(config.SCREEN_AB_TRACKING_FILE))
    assert len(doc["rotations"]) == 1
    r0 = doc["rotations"][0]
    assert r0["screen_a"]["picks"] == ["S0", "S1", "S2", "S3", "S4"]      # top 5
    assert r0["screen_b"]["picks"] == ["S1", "S3", "S6", "S8", "S11"]     # top 5 profitable
    assert r0["screen_a"]["avg_iv"] is None                              # IV unavailable
    assert r0["screen_a"]["detail"][0]["rv"] is not None                 # RV still recorded
    assert r0["two_week_results"] is None

    # Idempotent same-day re-run: no second rotation appended.
    assert tracker.run(dry_run=False) == 0
    assert len(json.load(open(config.SCREEN_AB_TRACKING_FILE))["rotations"]) == 1

    # Rotation 2 two weeks later: Screen A names each +10, Screen B names each -5%.
    d2 = ["2026-08-13", "2026-08-14", "2026-08-15"]
    base = {s: 100.0 + i for i, s in enumerate(syms)}
    later = {}
    for s in syms:
        if s in ["S0", "S2", "S4"]:            # A-only names up big
            later[s] = base[s] * 1.10
        elif s in ["S6", "S8", "S11"]:         # B-only names down
            later[s] = base[s] * 0.95
        else:                                   # shared names flat
            later[s] = base[s]
    closes2 = {ds: later for ds in d2}
    _wire_tracker(monkeypatch, ranked, _by_date(syms, d2, closes2), d2,
                  profitable={"S1", "S3", "S6", "S8", "S11"})
    monkeypatch.setattr(tracker, "_today_et", lambda: "2026-08-15")
    assert tracker.run(dry_run=False) == 0

    doc = json.load(open(config.SCREEN_AB_TRACKING_FILE))
    assert len(doc["rotations"]) == 2
    res = doc["rotations"][0]["two_week_results"]          # rotation 1 now measured
    assert res["measured_on"] == "2026-08-15"
    assert res["screen_a_returns"]["avg"] > res["screen_b_returns"]["avg"]
    assert res["winner"] == "screen_a"
    assert doc["winner_tally"]["screen_a"] == 1


def test_tracker_records_iv_none_without_dropping_pick(monkeypatch):
    ranked = _ranked(10)
    syms = [r["symbol"] for r in ranked]
    dates = ["2026-09-01", "2026-09-02"]
    closes = {ds: {s: 50.0 + i for i, s in enumerate(syms)} for ds in dates}
    _wire_tracker(monkeypatch, ranked, _by_date(syms, dates, closes), dates,
                  profitable=set(syms))
    monkeypatch.setattr(tracker, "_today_et", lambda: "2026-09-02")
    tracker.run(dry_run=False)
    r = json.load(open(config.SCREEN_AB_TRACKING_FILE))["rotations"][0]
    assert len(r["screen_a"]["picks"]) == 5
    assert all(d["iv"] is None for d in r["screen_a"]["detail"])   # None, but still present


# ── performance-report section ────────────────────────────────────────────────

def _completed_rotation(date, a_rets, b_rets, winner):
    return {
        "rotation_date": date,
        "screen_a": {"picks": list(a_rets), "detail": [{"symbol": s, "iv": 25.0} for s in a_rets],
                     "avg_iv": 25.0, "avg_rv": 30.0, "sector_breakdown": {}},
        "screen_b": {"picks": list(b_rets), "detail": [{"symbol": s, "iv": 20.0} for s in b_rets],
                     "avg_iv": 20.0, "avg_rv": 22.0, "sector_breakdown": {}},
        "two_week_results": {
            "measured_on": date,
            "screen_a_returns": {**a_rets, "avg": round(sum(a_rets.values()) / len(a_rets), 4)},
            "screen_b_returns": {**b_rets, "avg": round(sum(b_rets.values()) / len(b_rets), 4)},
            "winner": winner,
        },
    }


def test_report_section_empty():
    lines = pa._ab_screen_lines({"rotations": [], "winner_tally": {}})
    assert any("no A/B rotations recorded yet" in l for l in lines)


def test_report_section_waits_before_min_rotations():
    doc = {"rotations": [_completed_rotation("2026-08-01", {"AAA": 0.05}, {"BBB": 0.02}, "screen_a")],
           "winner_tally": {"screen_a": 1, "screen_b": 0, "tie": 0}}
    lines = pa._ab_screen_lines(doc)
    text = "\n".join(lines)
    assert "Rotations completed: 1" in text
    assert "need 3 more rotation" in text
    assert "Current leader: Screen A" in text


def test_report_section_recommends_after_min_rotations():
    rots = [_completed_rotation(f"2026-0{m}-01", {"AAA": 0.01}, {"BBB": 0.06}, "screen_b")
            for m in range(1, 5)]
    doc = {"rotations": rots, "winner_tally": {"screen_a": 0, "screen_b": 4, "tie": 0}}
    lines = pa._ab_screen_lines(doc)
    text = "\n".join(lines)
    assert "Rotations completed: 4" in text
    assert "adopt B" in text
