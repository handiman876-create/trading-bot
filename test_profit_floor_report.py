"""
Unit tests for the PROFIT FLOOR ANALYSIS report section — NO network, NO ledger.

Exercises _profit_floor_stats / _profit_floor_lines on synthetic closed trips.
Nothing here reads or writes data/trade_ledger.json.

Run:  python3 test_profit_floor_report.py
"""

import performance_analyzer as pa


def _trip(pnl=100.0, reason="stop", active=True, caused=True, floor=125.0,
          trail=105.0, qty=10):
    return {"exit_reason": reason, "pnl": pnl, "win": pnl > 0, "qty": qty,
            "profit_floor_active": active, "floor_caused_exit": caused,
            "profit_floor_price": floor, "atr_trail_at_exit": trail}


def test_pre_ladder_exits_are_not_counted_as_inactive():
    """An exit predating the ladder is NO evidence, not evidence of inactivity."""
    trips = [_trip(active=None, caused=None, floor=None, trail=None)
             for _ in range(5)]
    st = pa._profit_floor_stats(trips)
    assert st["attributed"] == 0, st
    assert st["unattributed"] == 5, st
    assert st["floor_active"] == 0, st
    lines = "\n".join(pa._profit_floor_lines(st))
    assert "no attributed stop exits yet" in lines, lines
    assert "5 stop exit(s) predate the ladder" in lines, lines


def test_non_stop_exits_are_excluded():
    """Signal exits never consulted a stop and must not dilute the denominator."""
    st = pa._profit_floor_stats([_trip(reason="signal"), _trip(reason="stop")])
    assert st["stop_exits"] == 1 and st["attributed"] == 1, st


def test_counts_split_caused_vs_trail_would_fire():
    trips = [_trip(caused=True), _trip(caused=True), _trip(caused=False),
             _trip(active=False, caused=False)]
    st = pa._profit_floor_stats(trips)
    assert st["attributed"] == 4, st
    assert st["floor_active"] == 3, st
    assert st["floor_caused"] == 2, st
    assert st["trail_would_fire"] == 1, st


def test_room_given_up_sums_floor_minus_trail_times_qty():
    st = pa._profit_floor_stats([_trip(floor=125.0, trail=105.0, qty=10)])
    assert abs(st["room_given_up"] - 200.0) < 0.01, st    # (125-105) * 10


def test_verdict_withheld_below_threshold():
    st = pa._profit_floor_stats([_trip(pnl=50.0), _trip(pnl=50.0)])
    assert st["verdict"] == "INSUFFICIENT DATA", st
    lines = "\n".join(pa._profit_floor_lines(st))
    assert "need 3+ before this means anything" in lines, lines


def test_verdict_helping_on_profitable_caused_exits():
    st = pa._profit_floor_stats([_trip(pnl=100.0) for _ in range(3)])
    assert st["verdict"] == "HELPING", st
    assert st["realized_on_caused"] == 300.0, st
    assert st["winners_on_caused"] == 3, st


def test_verdict_hurting_on_net_negative_caused_exits():
    st = pa._profit_floor_stats([_trip(pnl=-100.0) for _ in range(3)])
    assert st["verdict"] == "HURTING", st
    lines = "\n".join(pa._profit_floor_lines(st))
    assert "firing on trades the trail would have held" in lines, lines


def test_verdict_never_claims_a_counterfactual():
    """The section must not present realized P&L as the ladder's impact."""
    lines = "\n".join(pa._profit_floor_lines(
        pa._profit_floor_stats([_trip(pnl=100.0) for _ in range(3)])))
    assert "not a counterfactual" in lines, lines
    assert "NOT a realized loss" in lines, lines


def test_stats_land_in_the_json_report_not_only_the_text():
    """Regression: the section once read report['closed_trips'], which is a COUNT
    in data_quality, so it silently rendered empty forever."""
    st = pa._profit_floor_stats([_trip(pnl=100.0)])
    rendered = pa._profit_floor_lines(st)
    assert st["floor_caused"] == 1, st
    assert any("floor caused exit:" in ln for ln in rendered), rendered
    # and the renderer must survive a report that has no profit_floor key at all
    assert pa._profit_floor_lines(None)[0].startswith("=== PROFIT FLOOR")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"All {len(tests)} assertions passed.")
