"""
Tests for futures_performance_analyzer — the futures ledger.

WHY THIS EXISTS: until 2026-09-23 no futures trip reached any ledger (the weekly
analyzer read logs/trades.log only), so every futures P&L number was worked out
by hand. The two ways this module can be silently wrong are both arithmetic:

  * pricing a contract at 1x — NQZ26 09-20 -> 09-23 is +$11,035 at $20/pt and
    +$551.75 at 1x, and both look like plausible dollar figures;
  * computing capture as dollars / points — NQZ26's 56.5% reads as 1,131%.

The fixtures below are the REAL futures_trades.log rows (all 11, 07-21 -> 09-23)
so the expected totals are the ones checked by hand against broker fills.

Standalone-safe: no test reaches tradestation_client._get. Broker data is passed
in directly, and the one run()-level test stubs every broker read itself (a
direct `python3 test_...py` gets no conftest block).
"""

import json
import os
import sys
import tempfile

import config
import futures_performance_analyzer as fpa
import performance_analyzer as pa
import run_test


def _row(ts, action, symbol, price, fill, oid, notes, **attr):
    r = {"timestamp": ts, "action": action, "symbol": symbol, "quantity": 1,
         "price": price, "order_type": "market", "order_id": oid, "notes": notes,
         "signal_price": price if fill is not None else None, "fill_price": fill,
         "slippage": None}
    r.update(attr)
    return r


_WATER = dict(profit_floor_active=False, profit_floor_price=None,
              floor_caused_exit=False, breakeven_lock_held=False,
              lock_caused_exit=False, water_floor_active=True)

# The complete futures history as logged, 2026-07-21 .. 2026-09-23.
REAL_ROWS = [
    _row("2026-07-21 11:31:18 EDT", "BUY",  "RTYU26", 2986.6, 2986.8, "962809412",
         "RTY EMA cross up, RSI=51.7"),
    _row("2026-07-21 11:32:20 EDT", "SELL", "RTYU26", 2985.2, None, "962809910",
         "RTY EMA bearish, RSI=51.5"),
    _row("2026-08-03 18:40:35 EDT", "BUY",  "ESU26", 7634.0, 7635.75, "965284758",
         "ES EMA cross up, RSI=58.8"),
    _row("2026-08-04 15:41:14 EDT", "BUY",  "RTYU26", 3049.2, 3050.7, "965482689",
         "RTY EMA cross up, RSI=60.8"),
    _row("2026-08-06 18:31:29 EDT", "BUY",  "NQU26", 29567.0, 29546.5, "965956189",
         "NQ EMA cross up, RSI=54.5"),
    _row("2026-08-30 18:10:21 EDT", "SELL", "RTYU26", 2972.2, 2971.5, "969342889",
         "RTY EMA bearish, RSI=43.5"),
    _row("2026-09-01 02:13:47 EDT", "SELL", "NQU26", 29549.75, 29544.75, "969523052",
         "trailing stop hit @ 29555.16 (water floor)", **_WATER,
         water_caused_exit=True, water_at_exit=29804.75, water_floor_price=29555.1616,
         atr_trail_at_exit=28307.2193, stop_at_exit=29555.1616),
    _row("2026-09-01 04:28:31 EDT", "SELL", "ESU26", 7674.0, 7666.0, "969526275",
         "trailing stop hit @ 7674.25 (breakeven lock)",
         profit_floor_active=False, floor_caused_exit=False,
         breakeven_lock_held=True, lock_caused_exit=True, water_floor_active=False,
         water_floor_price=None, water_caused_exit=False, water_at_exit=7781.5,
         atr_trail_at_exit=7569.6922, stop_at_exit=7674.25),
    _row("2026-09-20 21:45:05 EDT", "BUY",  "NQZ26", 30118.75, 30114.25, "971896218",
         "NQ EMA cross up, RSI=57.6"),
    _row("2026-09-21 19:28:31 EDT", "BUY",  "ESZ26", 7835.75, 7834.75, "972042788",
         "ES EMA cross up, RSI=58.8"),
    _row("2026-09-23 10:20:08 EDT", "SELL", "NQZ26", 30767.5, 30666.0, "972266671",
         "trailing stop hit @ 30768.89 (water floor)", **_WATER,
         water_caused_exit=True, water_at_exit=31090.25,
         water_floor_price=30768.8904, atr_trail_at_exit=29804.8118,
         stop_at_exit=30768.8904),
]

# What the futures account's order history returned on 2026-09-23 for the
# RTYU26 07-21 exit — the one fill the log never captured.
RTY_EXIT_BROKER = {"order_id": "962809910", "symbol": "RTYU26", "action": "Sell",
                   "quantity": 1.0, "price": 2986.2, "status": "Filled",
                   "opened": "2026-07-21T15:32:20Z"}


def _ledger_from(rows):
    led = {"version": 1, "events": {}, "closed_trips": []}
    pa._merge_events(led, [(r, "fixture") for r in rows])
    fpa._tag_features(led)
    return led


def _report(led, positions=None, broker=None):
    return fpa.build_report(led, {}, {"files_parsed": [], "parse_errors": []},
                            positions=positions, broker=broker, mark=False)


def _trip(report, symbol):
    return next(t for t in report["trips"] if t["symbol"] == symbol)


# ── Contract multipliers ──────────────────────────────────────────────────────

def test_multipliers_from_futures_specs():
    assert fpa.point_value("ESZ26") == 50.0
    assert fpa.point_value("NQZ26") == 20.0
    assert fpa.point_value("RTYU26") == 50.0     # 3-char root must be reachable
    assert fpa.point_value("YMZ26") == 5.0


def test_unknown_root_raises_never_prices_at_1x():
    for sym in ("CLZ26", "ESTC", "AAPL"):
        try:
            fpa.point_value(sym)
        except fpa.UnknownFuturesRoot:
            continue
        raise AssertionError(f"{sym} priced instead of raising")


# ── Round trips on the real history ───────────────────────────────────────────

def test_nqz26_round_trip_is_11035_on_fills():
    led = _ledger_from([REAL_ROWS[8], REAL_ROWS[10]])
    t = _trip(_report(led, positions=[]), "NQZ26")
    assert t["pnl"] == 11035.0, t["pnl"]          # +551.75 pts x $20
    assert t["points"] == 551.75
    assert t["price_basis"] == "fill"
    assert t["point_value"] == 20.0
    assert t["floor"] == "water floor"


def test_capture_is_on_the_fill_not_the_signal():
    """56.5% on the 30666.00 fill; 67.1% if the signal price were used."""
    led = _ledger_from([REAL_ROWS[8], REAL_ROWS[10]])
    t = _trip(_report(led, positions=[]), "NQZ26")
    assert abs(t["capture"] - 0.5653) < 1e-4, t["capture"]


def test_full_history_reproduces_hand_checked_totals():
    """Log only: RTY 07-21 exit has no fill, so it is priced mixed at -$80."""
    rep = _report(_ledger_from(REAL_ROWS), positions=[{"symbol": "ESZ26", "quantity": 1}])
    assert rep["totals"]["trips"] == 5
    assert rep["totals"]["realized"] == 8472.50, rep["totals"]["realized"]
    by = {t["symbol"] + t["entry_ts"][:10]: t["pnl"] for t in rep["trips"]}
    assert by == {"RTYU262026-07-21": -80.0, "RTYU262026-08-04": -3960.0,
                  "NQU262026-08-06": -35.0, "ESU262026-08-03": 1512.5,
                  "NQZ262026-09-20": 11035.0}, by
    assert rep["data_quality"]["priced_mixed"] == 1


def test_broker_recovers_the_rty_0721_exit_fill():
    led = _ledger_from(REAL_ROWS)
    broker = fpa._merge_broker_history(led, [RTY_EXIT_BROKER])
    assert len(broker["fills_recovered"]) == 1
    ev = led["events"]["962809910"]
    assert ev["fill_price"] == 2986.2 and ev["fill_broker_recovered"] is True
    rep = _report(led, positions=[{"symbol": "ESZ26", "quantity": 1}], broker=broker)
    t = next(t for t in rep["trips"] if t["entry_ts"].startswith("2026-07-21"))
    assert t["pnl"] == -30.0 and t["price_basis"] == "fill"
    assert t["fill_broker_recovered"] is True
    assert rep["totals"]["realized"] == 8522.50


def test_multiplier_reaches_the_water_floor_capture_ratio():
    """pa._water_floor_stats must read dollars/dollars. Without _dollar_qty the
    NQZ26-only ratio is 11035 / 976 = 11.3 (1,131%)."""
    led = _ledger_from([REAL_ROWS[8], REAL_ROWS[10]])
    _report(led, positions=[])
    st = pa._water_floor_stats(led["closed_trips"])
    assert abs(st["capture_ratio"] - 0.5653) < 1e-4, st["capture_ratio"]


def test_equity_trips_are_unchanged_by_the_point_value_hook():
    e = {"timestamp": "2026-09-01 10:00:00 EDT", "action": "BUY", "symbol": "AAPL",
         "quantity": 10, "price": 100.0, "fill_price": 100.0, "role": "entry",
         "direction": "long", "feature": "long_fresh_cross", "order_id": "a"}
    x = dict(e, timestamp="2026-09-02 10:00:00 EDT", action="SELL", price=105.0,
             fill_price=105.0, role="exit", feature=None, order_id="b", notes="")
    closed, _o, _open = pa._pair_round_trips([e, x])
    assert closed[0]["pnl"] == 50.0
    assert "point_value" not in closed[0]
    assert pa._dollar_qty(closed[0]) == 10


# ── Open positions + reconcile against the FUTURES account ────────────────────

def test_esz26_stays_open_when_futures_account_holds_it():
    rep = _report(_ledger_from(REAL_ROWS), positions=[{"symbol": "ESZ26", "quantity": 1}])
    assert rep["data_quality"]["open_entries"] == 1
    assert rep["data_quality"]["reconciled_entries"] == []
    assert not any(t["symbol"] == "ESZ26" for t in rep["trips"])


def test_unreadable_futures_account_retires_nothing():
    """None is 'unknown', never 'flat' — ESZ26 must survive a failed fetch."""
    led = _ledger_from(REAL_ROWS)
    rep = _report(led, positions=None)
    assert rep["data_quality"]["open_entries"] == 1
    assert rep["data_quality"]["reconciled_entries"] is None
    assert not led["events"]["972042788"].get("reconciled")
    assert any("NOT reconciled" in w for w in rep["warnings"])


# ── Broker recovery: marking + the double-booking guards ──────────────────────

def test_unlogged_broker_order_is_recovered_and_marked():
    led = _ledger_from([REAL_ROWS[9]])                    # ESZ26 entry only
    broker = fpa._merge_broker_history(led, [
        {"order_id": "999", "symbol": "ESZ26", "action": "Sell", "quantity": 1.0,
         "price": 7900.0, "status": "Filled", "opened": "2026-09-24T14:00:00Z"}])
    assert len(broker["events_recovered"]) == 1
    ev = led["events"]["999"]
    assert ev["broker_recovered"] is True and ev["timestamp"].startswith("2026-09-24 10:00:00")
    assert ev["water_floor_active"] is None, "the broker cannot know why we sold"
    t = _trip(_report(led, positions=[]), "ESZ26")
    assert t["exit_reason"] == "broker_recovered" and t["broker_recovered"] is True
    assert t["pnl"] == (7900.0 - 7834.75) * 50


def test_cancelled_rejected_and_equity_rows_are_ignored():
    led = _ledger_from([])
    broker = fpa._merge_broker_history(led, [
        {"order_id": "1", "symbol": "ESZ26", "action": "Sell", "quantity": 0.0,
         "price": None, "status": "Canceled", "opened": "2026-09-22T00:00:00Z"},
        {"order_id": "2", "symbol": "AAPL", "action": "Buy", "quantity": 5.0,
         "price": 200.0, "status": "Filled", "opened": "2026-09-22T00:00:00Z"},
        {"order_id": "3", "symbol": "ESTC", "action": "Buy", "quantity": 5.0,
         "price": 90.0, "status": "Filled", "opened": "2026-09-22T00:00:00Z"},
    ])
    assert broker["futures_fills"] == 0 and led["events"] == {}


def test_log_row_without_order_id_is_not_booked_twice():
    row = dict(REAL_ROWS[9], order_id=None)
    led = _ledger_from([row])
    broker = fpa._merge_broker_history(led, [
        {"order_id": "972042788", "symbol": "ESZ26", "action": "Buy", "quantity": 1.0,
         "price": 7834.75, "status": "Filled", "opened": "2026-09-21T23:29:01Z"}])
    assert broker["matched_without_id"] == 1
    assert len(led["events"]) == 1


def test_disagreeing_fill_is_counted_not_overwritten():
    led = _ledger_from([REAL_ROWS[10]])
    broker = fpa._merge_broker_history(led, [
        {"order_id": "972266671", "symbol": "NQZ26", "action": "Sell", "quantity": 1.0,
         "price": 30700.0, "status": "Filled", "opened": "2026-09-23T14:20:07Z"}])
    assert led["events"]["972266671"]["fill_price"] == 30666.0
    assert broker["fill_mismatches"][0]["broker"] == 30700.0
    rep = _report(led, positions=[], broker=broker)
    assert any("disagree" in w for w in rep["warnings"])


def test_broker_unavailable_touches_nothing_and_says_so():
    led = _ledger_from(REAL_ROWS)
    before = json.dumps(led, sort_keys=True)
    broker = fpa._merge_broker_history(led, None)
    assert broker["available"] is False
    assert json.dumps(led, sort_keys=True) == before
    rep = _report(led, positions=[], broker=broker)
    assert any("order history unavailable" in w for w in rep["warnings"])


def test_history_needs_both_reads_or_none():
    """historicalorders excludes today; half a history must not pass as whole."""
    import tradestation_client as tc
    saved = tc.get_historical_orders, tc.get_current_orders
    try:
        tc.get_historical_orders = lambda a, s: [RTY_EXIT_BROKER]
        tc.get_current_orders = lambda a: None
        assert fpa._broker_history("SIM") is None
        tc.get_current_orders = lambda a: []
        assert fpa._broker_history("SIM") == [RTY_EXIT_BROKER]
    finally:
        tc.get_historical_orders, tc.get_current_orders = saved


def test_current_orders_parse_like_historical():
    import tradestation_client as tc
    saved = tc._get
    try:
        tc._get = lambda path, params=None: {"Orders": [{
            "OrderID": 972266671, "StatusDescription": "Filled",
            "OpenedDateTime": "2026-09-23T14:20:07Z", "FilledPrice": "30666",
            "Legs": [{"Symbol": "NQZ26", "BuyOrSell": "Sell",
                      "ExecQuantity": "1", "ExecutionPrice": "30666"}]}]}
        rows = tc.get_current_orders("SIM")
    finally:
        tc._get = saved
    assert rows == [{"order_id": "972266671", "symbol": "NQZ26", "action": "Sell",
                     "quantity": 1.0, "price": 30666.0, "status": "Filled",
                     "opened": "2026-09-23T14:20:07Z"}]


# ── Ledger hygiene ────────────────────────────────────────────────────────────

def test_orphan_sell_is_flagged_as_possible_short():
    rep = _report(_ledger_from([REAL_ROWS[10]]), positions=[])
    assert len(rep["data_quality"]["orphan_exits_missing_entry"]) == 1
    assert any("OPEN a short" in w for w in rep["warnings"])


def test_reingesting_the_same_logs_is_idempotent():
    led = _ledger_from(REAL_ROWS)
    pa._merge_events(led, [(r, "again") for r in REAL_ROWS])
    fpa._merge_broker_history(led, [RTY_EXIT_BROKER])
    fpa._merge_broker_history(led, [RTY_EXIT_BROKER])
    assert len(led["events"]) == 11
    assert _report(led, positions=[{"symbol": "ESZ26", "quantity": 1}])["totals"]["realized"] == 8522.50


def test_every_futures_entry_is_in_the_futures_bucket():
    led = _ledger_from(REAL_ROWS)
    feats = {e["feature"] for e in led["events"].values() if e["role"] == "entry"}
    assert feats == {"futures"}


# ── Paths + isolation ─────────────────────────────────────────────────────────

def test_ledger_path_is_guarded_and_state_routed():
    assert "FUTURES_TRADE_LEDGER_FILE" in run_test._GUARDED
    assert config.FUTURES_TRADE_LEDGER_FILE in config.STATE_FILES
    assert config.FUTURES_TRADE_LEDGER_FILE.startswith(config.STATE_DIR + "/")
    assert config.FUTURES_TRADE_LEDGER_FILE != config.TRADE_LEDGER_FILE
    if config._IS_TEST:
        assert "data/test" in config.FUTURES_TRADE_LEDGER_FILE or \
            config.STATE_DIR == os.environ.get("TB_TEST_TMPDIR")


def test_futures_trade_log_path_is_mode_independent():
    assert config.FUTURES_TRADE_LOG_FILE.endswith("futures_trades.log")
    assert config.FUTURES_TRADE_LOG_FILE != config.TRADE_LOG_FILE or config._IS_FUTURES
    if config._IS_TEST:
        assert os.path.basename(config.FUTURES_TRADE_LOG_FILE) == "test_futures_trades.log"


def test_run_writes_only_the_futures_ledger():
    """End to end with every broker read stubbed: the futures ledger and JSON
    land at their paths, the equity ledger is never touched."""
    import tradestation_client as tc
    tmp = tempfile.mkdtemp()
    log = os.path.join(tmp, "futures_trades.log")
    with open(log, "w") as f:
        for r in REAL_ROWS:
            f.write(json.dumps(r) + "\n")
    names = ("LEDGER_PATH", "TRADES_GLOB", "STOPS_PATH", "REPORT_JSON")
    saved = {n: getattr(fpa, n) for n in names}
    saved_fns = (fpa._futures_account_id, fpa._broker_history,
                 fpa._broker_positions, tc.get_quote)
    equity_mtime = os.path.getmtime(pa.LEDGER_PATH) if os.path.exists(pa.LEDGER_PATH) else None
    try:
        fpa.LEDGER_PATH = os.path.join(tmp, "futures_trade_ledger.json")
        fpa.TRADES_GLOB = log + "*"
        fpa.STOPS_PATH = os.path.join(tmp, "none.json")
        fpa.REPORT_JSON = os.path.join(tmp, "futures_performance_report.json")
        fpa._futures_account_id = lambda: "SIM3297102F"
        fpa._broker_history = lambda a: [RTY_EXIT_BROKER]
        fpa._broker_positions = lambda a: [{"symbol": "ESZ26", "quantity": 1}]
        tc.get_quote = lambda s: {"last": 7772.75}
        rep = fpa.run()
    finally:
        for n, v in saved.items():
            setattr(fpa, n, v)
        (fpa._futures_account_id, fpa._broker_history,
         fpa._broker_positions, tc.get_quote) = saved_fns
    assert rep["totals"]["realized"] == 8522.50
    assert rep["totals"]["open_estimate"] == (7772.75 - 7834.75) * 50
    with open(os.path.join(tmp, "futures_trade_ledger.json")) as f:
        assert len(json.load(f)["events"]) == 11
    assert os.path.exists(os.path.join(tmp, "futures_performance_report.json"))
    after = os.path.getmtime(pa.LEDGER_PATH) if os.path.exists(pa.LEDGER_PATH) else None
    assert after == equity_mtime, "the equity ledger must not be written"


# ── Weekly report integration ─────────────────────────────────────────────────

def test_section_renders_trips_and_capture():
    led = _ledger_from(REAL_ROWS)
    broker = fpa._merge_broker_history(led, [RTY_EXIT_BROKER])
    text = "\n".join(fpa.render_lines(
        _report(led, positions=[{"symbol": "ESZ26", "quantity": 1}], broker=broker)))
    assert text.startswith("=== FUTURES PERFORMANCE ===")
    assert "$8,522.50" in text
    assert "capture 56.5%" in text
    assert "fill broker-recovered" in text
    assert "NOT included in the equity totals" in text


def test_futures_failure_is_reported_and_exits_nonzero():
    text = "\n".join(fpa.render_lines({"error": "UnknownFuturesRoot: 'CLZ26'"}))
    assert "FAILED" in text and "CLZ26" in text
    saved_run, saved_argv = pa.run, sys.argv
    try:
        sys.argv = ["performance_analyzer.py", "--no-reconcile"]
        pa.run = lambda **k: {"futures": {"error": "boom"}}
        assert pa.main() == 1
        pa.run = lambda **k: {"futures": {"totals": {}}}
        assert pa.main() == 0
    finally:
        pa.run, sys.argv = saved_run, saved_argv


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("OK")
