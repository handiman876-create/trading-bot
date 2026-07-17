"""
Unit tests for the Claude sentiment overlay — NO network, NO Claude/Polygon calls.

Covers fear_score→regime, report validation/coercion, the NEUTRAL fallback, the
staleness window (including the Monday-morning weekend-gap scenario), per-sector
entry gating, the VIX-vs-sentiment "more fearful" combination, and main()'s
fail-safe paths (Polygon/Claude failure → neutral report, cost-cap alert).

Run:  python3 test_sentiment.py   (or via pytest)
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta

import config
import sentiment_analyzer as sa
import strategy

UTC = timezone.utc


class _LogCap:
    def __enter__(self):
        self.records = []
        self._h = logging.Handler()
        self._h.emit = lambda r: self.records.append(r.getMessage())
        logging.getLogger("sentiment").addHandler(self._h)
        logging.getLogger("sentiment").setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        logging.getLogger("sentiment").removeHandler(self._h)

    @property
    def text(self):
        return "\n".join(self.records)


def _tmp_report_path():
    d = tempfile.mkdtemp(prefix="sentiment_test_")
    path = os.path.join(d, "sentiment_report.json")
    config.SENTIMENT_REPORT_FILE = path
    return path


def _write(path, report):
    with open(path, "w") as f:
        json.dump(report, f)


def _report(gen, regime="defensive", fear=7, fallback=False, sectors=None):
    return {"generated_at": gen.isoformat(), "fear_score": fear, "regime": regime,
            "top_risks": ["oil", "rates", "ai"], "sector_risks": sectors or
            {s: "low" for s in sa._SECTORS}, "summary": "x",
            "headlines_analyzed": 20, "fallback": fallback}


# ── 1. fear_score → regime ────────────────────────────────────────────────────
def test_regime_from_score():
    R = sa._regime_from_score
    assert [R(s) for s in (1, 2, 3)] == ["risk_on"] * 3
    assert [R(s) for s in (4, 5, 6)] == ["cautious"] * 3
    assert [R(s) for s in (7, 8)] == ["defensive"] * 2
    assert [R(s) for s in (9, 10)] == ["crisis"] * 2


# ── 2. validation / coercion ──────────────────────────────────────────────────
def test_validate_coerces_and_derives():
    out = sa._validate({"fear_score": 99, "top_risks": ["a", "b", "c", "d"],
                        "sector_risks": {"tech": "HIGH", "energy": "bogus"},
                        "summary": "s"}, 20)
    assert out["fear_score"] == 10                 # clamped
    assert out["regime"] == "crisis"               # derived from clamped score
    assert out["top_risks"] == ["a", "b", "c"]     # capped at 3
    assert out["sector_risks"]["tech"] == "high"   # lowercased
    assert out["sector_risks"]["energy"] == "low"  # invalid → low
    assert out["sector_risks"]["financials"] == "low"   # missing → low
    assert out["fallback"] is False
    assert set(out["sector_risks"]) == set(sa._SECTORS)


def test_validate_survives_garbage():
    out = sa._validate({"fear_score": "not a number"}, 0)
    assert out["fear_score"] == 1 and out["regime"] == "risk_on"


# ── 3. neutral fallback ───────────────────────────────────────────────────────
def test_neutral_report_is_risk_on_and_flagged():
    r = sa._neutral_report("because")
    assert r["fallback"] is True
    assert r["regime"] == "risk_on"
    assert all(v == "low" for v in r["sector_risks"].values())
    assert "because" in r["summary"]


# ── 4. staleness window ───────────────────────────────────────────────────────
def test_fresh_report_is_used():
    path = _tmp_report_path()
    gen = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    _write(path, _report(gen, regime="defensive", fear=7))
    rep = sa.current_sentiment(now=gen + timedelta(hours=10))    # 10h < 26h
    assert rep["fallback"] is False and rep["regime"] == "defensive"


def test_report_stale_after_window_is_neutral():
    path = _tmp_report_path()
    gen = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    _write(path, _report(gen, regime="defensive", fear=7))
    over = config.SENTIMENT_MAX_AGE_HOURS + 1                    # just past the window
    rep = sa.current_sentiment(now=gen + timedelta(hours=over))
    assert rep["fallback"] is True and rep["regime"] == "risk_on"


def test_missing_and_corrupt_reports_are_neutral():
    path = _tmp_report_path()
    assert sa.current_sentiment()["fallback"] is True            # file absent
    with open(path, "w") as f:
        f.write("{not json")
    assert sa.current_sentiment()["fallback"] is True            # corrupt


def test_disabled_is_neutral_without_reading():
    _tmp_report_path()
    prev = config.ENABLE_SENTIMENT
    config.ENABLE_SENTIMENT = False
    try:
        assert sa.current_sentiment()["fallback"] is True
    finally:
        config.ENABLE_SENTIMENT = prev


# ── 5. THE Monday-morning weekend-gap scenario ────────────────────────────────
def test_monday_morning_uses_neutral_not_stale_friday():
    """Friday 08:00 ET report, no weekend runs, "now" = Monday 07:59 ET (~72h old).
    Must resolve to NEUTRAL — NOT the stale Friday 'defensive' report."""
    path = _tmp_report_path()
    fri = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)     # Fri 08:00 ET
    mon = datetime(2026, 7, 20, 11, 59, tzinfo=UTC)    # Mon 07:59 ET
    age_h = (mon - fri).total_seconds() / 3600
    assert age_h > 71                                   # ~71.98h, not the "47h" myth
    _write(path, _report(fri, regime="defensive", fear=7))
    rep = sa.current_sentiment(now=mon)
    assert rep["fallback"] is True, "Monday must fall back to NEUTRAL"
    assert rep["regime"] == "risk_on", "must NOT reuse Friday's stale 'defensive'"


# ── 6. per-sector entry gating ────────────────────────────────────────────────
def test_sectors_blocked_maps_high_to_symbols():
    rep = _report(datetime(2026, 7, 17, tzinfo=UTC),
                  sectors={"tech": "high", "financials": "medium", "energy": "high",
                           "healthcare": "low", "consumer": "low", "industrials": "low"})
    blocked = sa.sectors_blocked(rep)
    assert "NVDA" in blocked and "AMD" in blocked and "CRWD" in blocked  # tech high
    assert "JPM" not in blocked                       # financials only medium
    assert not (blocked & {"KO", "COST", "TGT"})      # consumer low
    # energy "high" is a harmless no-op (already universe-excluded → empty list)
    assert all(sym for sym in blocked)


def test_sectors_blocked_empty_when_no_high():
    rep = _report(datetime(2026, 7, 17, tzinfo=UTC))  # all low
    assert sa.sectors_blocked(rep) == set()


# ── 7. VIX vs sentiment: take the MORE fearful ────────────────────────────────
def test_more_fearful_combination():
    F = strategy._more_fearful
    assert F("risk_on", "defensive") == "defensive"   # sentiment escalates
    assert F("crisis", "cautious") == "crisis"        # VIX escalates
    assert F("cautious", "cautious") == "cautious"
    assert F("unknown", "cautious") == "cautious"     # unknown ranks as calm
    assert F("defensive", "risk_on") == "defensive"


# ── 7b. headline merge/dedup (multi-ticker) ───────────────────────────────────
def test_dedup_recent_dedups_sorts_caps():
    raw = [
        {"title": "A", "article_url": "u1", "published_utc": "2026-07-17T10:00:00Z"},
        {"title": "A again", "article_url": "u1", "published_utc": "2026-07-17T11:00:00Z"},  # dup URL
        {"title": "B", "article_url": "u2", "published_utc": "2026-07-17T12:00:00Z"},
        {"title": "", "article_url": "u3", "published_utc": "2026-07-17T13:00:00Z"},          # titleless
        {"title": "C", "id": "idC", "published_utc": "2026-07-16T09:00:00Z"},                 # no url → id key
    ]
    out = sa._dedup_recent(raw)
    assert [h["title"] for h in out] == ["B", "A", "C"]   # newest-first, u1 kept once, titleless gone
    assert len(out) == 3


def test_dedup_recent_caps_at_limit():
    raw = [{"title": f"h{i}", "article_url": f"u{i}",
            "published_utc": f"2026-07-17T{i:02d}:00:00Z"} for i in range(24)]
    out = sa._dedup_recent(raw)
    assert len(out) == config.SENTIMENT_NEWS_LIMIT      # capped at 20
    assert out[0]["title"] == "h23"                     # most recent first


# ── 7c. PR/legal spam filter ──────────────────────────────────────────────────
def test_is_spam_filters_pr_and_legal():
    S = sa._is_spam
    assert S({"publisher": {"name": "GlobeNewswire Inc."}, "title": "X reports earnings"})
    assert S({"publisher": {"name": "The Motley Fool"}, "title": "PENTAIR INVESTOR ALERT: ..."})
    assert S({"publisher": {"name": "Reuters"}, "title": "ROSEN Law Firm encourages Acme investors to act"})
    assert S({"publisher": {"name": "PRNewswire"}, "title": "anything at all"})
    assert S({"publisher": {}, "title": "SEC opens INVESTIGATION into fraud"})
    # genuine market news survives
    assert not S({"publisher": {"name": "The Motley Fool"},
                  "title": "Stocks slide as semiconductor rout deepens"})
    assert not S({"publisher": {"name": "Reuters"}, "title": "Fed holds rates steady"})


def test_dedup_recent_drops_spam():
    raw = [
        {"title": "Real market news", "article_url": "u1",
         "published_utc": "2026-07-17T12:00:00Z", "publisher": {"name": "Reuters"}},
        {"title": "ACME SHAREHOLDER ALERT: class action DEADLINE", "article_url": "u2",
         "published_utc": "2026-07-17T13:00:00Z", "publisher": {"name": "GlobeNewswire"}},
    ]
    assert [h["title"] for h in sa._dedup_recent(raw)] == ["Real market news"]


# ── 8. main() fail-safe paths (no network) ────────────────────────────────────
def test_main_writes_neutral_when_polygon_fails():
    path = _tmp_report_path()
    sa._fetch_headlines = lambda: None
    sa.main()
    rep = json.load(open(path))
    assert rep["fallback"] is True and rep["regime"] == "risk_on"


def test_main_writes_neutral_when_claude_fails():
    path = _tmp_report_path()
    sa._fetch_headlines = lambda: [{"title": "t", "published_utc": "z"}]
    sa._call_claude = lambda system, user: (None, 0.0)
    sa.main()
    rep = json.load(open(path))
    assert rep["fallback"] is True


def test_main_happy_path_writes_validated_report():
    path = _tmp_report_path()
    sa._fetch_headlines = lambda: [{"title": "t", "published_utc": "z"}] * 20
    sa._call_claude = lambda system, user: (
        {"fear_score": 8, "top_risks": ["oil"], "summary": "tense",
         "sector_risks": {"tech": "high"}}, 0.01)
    sa.main()
    rep = json.load(open(path))
    assert rep["fallback"] is False
    assert rep["fear_score"] == 8 and rep["regime"] == "defensive"
    assert rep["sector_risks"]["tech"] == "high"


def test_main_cost_alert_over_cap():
    _tmp_report_path()
    sa._fetch_headlines = lambda: [{"title": "t", "published_utc": "z"}]
    sa._call_claude = lambda system, user: (
        {"fear_score": 3, "top_risks": [], "summary": "calm", "sector_risks": {}}, 0.50)
    with _LogCap() as cap:
        sa.main()
    assert "SENTIMENT COST ALERT" in cap.text and "0.50" in cap.text


# ── 9. evaluate_stock respects blocked_symbols (integration) ──────────────────
def _drive_entry(symbol, blocked):
    """Drive evaluate_stock with a fresh bullish cross; return placed orders.
    Saves/restores every strategy double so nothing leaks to later test files."""
    saved = (strategy.tc.get_historical, strategy.ind.compute_indicators,
             strategy.mh.entries_allowed, strategy.tc.place_equity_order,
             strategy.tc.get_quote, strategy.log_trade)
    orders = []
    try:
        strategy.tc.get_historical = lambda *a, **k: [{"bar": 1}]
        strategy.ind.compute_indicators = lambda *a, **k: {
            "close": 100.0, "ema_short": 105.0, "ema_long": 100.0, "rsi": 55.0,
            "bullish_cross": True, "bearish_cross": False, "atr": 4.0}
        strategy.tc.place_equity_order = lambda acct, sym, side, qty: \
            orders.append((sym, side, qty)) or {"order": {"id": "X"}}
        strategy.tc.get_quote = lambda s: {"last": 100.0, "close": 100.0}
        strategy.mh.entries_allowed = lambda *a, **k: True
        strategy.log_trade = lambda *a, **k: None
        strategy._signaled_buy_today.clear()
        strategy._signaled_sell_today.clear()
        strategy._sentiment_sector_blocks = 0
        strategy.evaluate_stock(symbol, "ACCT", [], 100000.0, regime="risk_on",
                                blocked_symbols=blocked)
    finally:
        (strategy.tc.get_historical, strategy.ind.compute_indicators,
         strategy.mh.entries_allowed, strategy.tc.place_equity_order,
         strategy.tc.get_quote, strategy.log_trade) = saved
    return orders


def test_evaluate_stock_blocks_sector_high_symbol():
    tech = frozenset(sa.SECTOR_TO_SYMBOLS["tech"])
    orders = _drive_entry("NVDA", blocked=tech)          # tech high → blocked
    assert orders == [], "sector-high symbol must not enter on a fresh cross"
    assert strategy._sentiment_sector_blocks == 1


def test_evaluate_stock_allows_unblocked_symbol():
    orders = _drive_entry("SPY", blocked=frozenset(["NVDA", "AMD"]))  # SPY not blocked
    assert any(o[0] == "SPY" and o[1] == "buy" for o in orders), orders


if __name__ == "__main__":
    _orig = {"fetch": sa._fetch_headlines, "call": sa._call_claude,
             "report_file": config.SENTIMENT_REPORT_FILE}
    try:
        tests = [v for k, v in sorted(globals().items())
                 if k.startswith("test_") and callable(v)]
        passed = 0
        for t in tests:
            # each file-writing test resets the report path itself; restore doubles
            sa._fetch_headlines = _orig["fetch"]
            sa._call_claude = _orig["call"]
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        print(f"All {passed} tests passed.")
    finally:
        sa._fetch_headlines = _orig["fetch"]
        sa._call_claude = _orig["call"]
        config.SENTIMENT_REPORT_FILE = _orig["report_file"]
