"""
Pytest-wide safety net: redirect every file the bot WRITES to a per-test temp
directory.

WHY THIS EXISTS: the test modules redirect their state-file paths only inside
their `if __name__ == "__main__"` runners. Under pytest that block never runs, so
the test functions used the REAL paths — a pytest run once overwrote
data/stop_prices.json (destroying live trailing stops) and appended fake trades
to logs/trades.log. This autouse fixture makes every test hermetic regardless of
how it's invoked, so the suite can never mutate live trade/stop/ledger state.
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_bot_state(tmp_path, monkeypatch):
    import config
    import strategy

    monkeypatch.setattr(config, "TRADE_LOG_FILE", str(tmp_path / "trades.log"), raising=False)
    monkeypatch.setattr(config, "PERF_LOG_FILE",  str(tmp_path / "performance.log"), raising=False)
    monkeypatch.setattr(strategy, "_STOPS_PATH",       str(tmp_path / "stop_prices.json"), raising=False)
    monkeypatch.setattr(strategy, "_MOM_ENTRIES_PATH", str(tmp_path / "momentum_entries.json"), raising=False)

    # performance_analyzer, if a test drives its file-writing paths.
    try:
        import performance_analyzer as pa
        monkeypatch.setattr(pa, "LEDGER_PATH", str(tmp_path / "trade_ledger.json"), raising=False)
        monkeypatch.setattr(pa, "REPORT_JSON", str(tmp_path / "performance_report.json"), raising=False)
        monkeypatch.setattr(pa, "REPORT_TXT",  str(tmp_path / "performance_report.txt"), raising=False)
        monkeypatch.setattr(pa, "STOPS_PATH",  str(tmp_path / "stop_prices.json"), raising=False)
    except ImportError:
        pass

    yield
