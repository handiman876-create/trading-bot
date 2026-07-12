"""
Tests for the momentum screen + effective watchlist.

    python3 test_momentum_screen.py      # runs via pytest
    pytest test_momentum_screen.py

Synthetic price series are tuned so the REAL indicators.rsi() lands the intended
RSI band — no mocking of the indicator, so the screen and live signal stay in
lockstep.
"""

import json
import sys

import config
import momentum_screen as ms
import watchlist


# ── helpers ───────────────────────────────────────────────────────────────────

def _climb(up, dn, base=15, recent=20):
    """Flat base (sets a low close[-21]) then a pullback-laden climb — nets a
    >5% 20-day return with RSI in the 50-70 band for up∈[1.009,1.013]."""
    s = [100.0] * base
    p = 100.0
    for i in range(recent):
        p *= up if (i % 4) != 3 else dn
        s.append(round(p, 2))
    return s


def _vols(n, last_high=True):
    v = [1_000_000.0] * n
    v[-1] = 1_500_000.0 if last_high else 500_000.0
    return v


def _build_by_date(series_map, vol_map):
    n = len(next(iter(series_map.values())))
    dates = [f"2026-04-{d:02d}" for d in range(1, n + 1)]   # ascending, zero-padded
    by_date = {}
    for j, ds in enumerate(dates):
        by_date[ds] = {
            sym: {"open": c[j], "high": c[j], "low": c[j], "close": c[j],
                  "volume": vol_map[sym][j]}
            for sym, c in series_map.items()
        }
    return by_date


# ── evaluate_symbol: the pure criteria ────────────────────────────────────────

def test_evaluate_pass():
    closes = _climb(1.011, 0.985)
    row = ms.evaluate_symbol("WIN", closes, _vols(len(closes)))
    assert row is not None
    assert row["return_20d"] > config.MOM_RETURN_MIN
    assert config.MOM_RSI_MIN <= row["rsi"] <= config.MOM_RSI_MAX
    assert row["rel_volume"] > 1.0


def test_evaluate_fail_return():
    assert ms.evaluate_symbol("FLAT", [100.0] * 35, _vols(35)) is None


def test_evaluate_fail_overbought():
    closes, p = [], 100.0
    for i in range(35):                 # steep up-up-down -> RSI > 70
        p *= 1.012 if i % 3 else 0.994
        closes.append(round(p, 2))
    row = ms.evaluate_symbol("HOT", closes, _vols(35))
    assert row is None                  # return>5% and volume ok, but RSI>70


def test_evaluate_fail_volume():
    closes = _climb(1.011, 0.985)
    assert ms.evaluate_symbol("WIN", closes, _vols(len(closes), last_high=False)) is None


def test_evaluate_fail_short_history():
    assert ms.evaluate_symbol("SHORT", [100.0] * 10, _vols(10)) is None


# ── screen(): core-exclusion, ranking, slot truncation ────────────────────────

def test_screen_excludes_core_ranks_and_truncates(monkeypatch):
    pairs = [(1.013, 0.985), (1.013, 0.984), (1.012, 0.986), (1.013, 0.983),
             (1.012, 0.985), (1.011, 0.987), (1.012, 0.984)]   # 7 distinct winners
    series = {f"W{i}": _climb(up, dn) for i, (up, dn) in enumerate(pairs)}

    core_sym = config.CORE_WATCHLIST[0]          # would pass, but must be excluded
    series[core_sym] = _climb(1.013, 0.985)

    n = len(next(iter(series.values())))
    vols = {s: _vols(n) for s in series}

    monkeypatch.setattr(ms, "_load_universe", lambda: list(series.keys()))
    monkeypatch.setattr(ms, "_collect_grouped_daily", lambda: _build_by_date(series, vols))

    picks = ms.screen()
    syms = [p["symbol"] for p in picks]

    assert core_sym.upper() not in syms                  # core excluded
    assert len(picks) == config.MOMENTUM_SLOT_SIZE       # truncated to slot size
    rets = [p["return_20d"] for p in picks]
    assert rets == sorted(rets, reverse=True)            # ranked best-first


# ── watchlist: effective list assembly ────────────────────────────────────────

def test_effective_watchlist_union_dedup_order(monkeypatch):
    monkeypatch.setattr(watchlist, "_load_momentum_symbols", lambda: ["TSLA", "XYZ"])
    positions = [
        {"symbol": "OLDMO", "quantity": 10},   # held straggler (rotated-out) -> keep
        {"symbol": "NVDA",  "quantity": 5},    # core + held -> dedup
        {"symbol": "ZERO",  "quantity": 0},    # flat -> drop
    ]
    eff = watchlist.effective_stock_watchlist(positions)

    core = [s.upper() for s in config.CORE_WATCHLIST]
    assert eff[:len(core)] == core             # core first, in order
    assert "XYZ" in eff and eff.count("TSLA") == 1   # momentum add + no dup vs core
    assert "OLDMO" in eff and "ZERO" not in eff      # orphan-guard keeps held, drops flat
    assert len(eff) == len(set(eff))                 # fully de-duplicated


# ── watchlist: momentum-file loader degrades gracefully ───────────────────────

def test_load_momentum_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MOMENTUM_WATCHLIST_FILE", str(tmp_path / "nope.json"))
    assert watchlist._load_momentum_symbols() == []


def test_load_momentum_malformed(monkeypatch, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(config, "MOMENTUM_WATCHLIST_FILE", str(p))
    assert watchlist._load_momentum_symbols() == []


def test_load_momentum_missing_key(monkeypatch, tmp_path):
    p = tmp_path / "nokey.json"
    p.write_text(json.dumps({"foo": 1}))
    monkeypatch.setattr(config, "MOMENTUM_WATCHLIST_FILE", str(p))
    assert watchlist._load_momentum_symbols() == []


def test_load_momentum_valid_uppercases(monkeypatch, tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"symbols": ["aaa", "bbb"],
                             "generated": "2026-07-12T00:00:00+00:00"}))
    monkeypatch.setattr(config, "MOMENTUM_WATCHLIST_FILE", str(p))
    assert watchlist._load_momentum_symbols() == ["AAA", "BBB"]


# ── Minimal runner so the file works under plain `python3` (no pytest needed),
#    while the pytest-style signatures still run under `pytest` if available. ──

class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)
        self._undo.clear()


def _run_all():
    import inspect
    import tempfile
    from pathlib import Path

    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    passed = failed = 0
    for name, fn in tests:
        params = inspect.signature(fn).parameters
        mp = _MonkeyPatch()
        tmp = tempfile.TemporaryDirectory() if "tmp_path" in params else None
        try:
            kwargs = {}
            if "monkeypatch" in params:
                kwargs["monkeypatch"] = mp
            if "tmp_path" in params:
                kwargs["tmp_path"] = Path(tmp.name)
            fn(**kwargs)
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            failed += 1
        finally:
            mp.undo()
            if tmp:
                tmp.cleanup()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
