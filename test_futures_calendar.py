"""
Unit tests for futures_market_hours — pure functions, NO network.

Covers the CME session clock (open/halt/weekend/holiday boundaries),
seconds-until-open/close, and the quarterly front-month roll (incl. the
5-day-before-expiry boundary and Dec->Mar year rollover).

Run:  python3 test_futures_calendar.py      (also importable by pytest)
"""

from datetime import date, datetime

import pytz

import futures_market_hours as fmh

ET = pytz.timezone("America/New_York")


def _dt(y, mo, d, h=0, mi=0):
    """Build a tz-aware ET datetime."""
    return ET.localize(datetime(y, mo, d, h, mi))


# ── Symbology ─────────────────────────────────────────────────────────────────

def test_build_futures_symbol():
    assert fmh.build_futures_symbol("ES", 9, 2026) == "ESU26"
    assert fmh.build_futures_symbol("NQ", 12, 2026) == "NQZ26"
    assert fmh.build_futures_symbol("RTY", 3, 2027) == "RTYH27"
    assert fmh.build_futures_symbol("ES", 6, 2026) == "ESM26"


def test_month_codes():
    assert fmh._MONTH_CODES[3] == "H"
    assert fmh._MONTH_CODES[6] == "M"
    assert fmh._MONTH_CODES[9] == "U"
    assert fmh._MONTH_CODES[12] == "Z"


def test_signal_symbol():
    assert fmh.signal_symbol("ES") == "@ES"
    assert fmh.signal_symbol("NQ") == "@NQ"


# ── Front-month roll ──────────────────────────────────────────────────────────

def test_front_month_basic():
    # 2026-07-08: Sep 2026 is front (Jun already past its roll)
    assert fmh.front_month_contract("ES", date(2026, 7, 8)) == "ESU26"
    assert fmh.front_month_contract("NQ", date(2026, 7, 8)) == "NQU26"


def test_front_month_roll_boundary():
    # Sep 2026 expiry (3rd Fri) = 2026-09-18; roll date = 09-13.
    assert fmh.front_month_contract("ES", date(2026, 9, 12)) == "ESU26"  # before roll
    assert fmh.front_month_contract("ES", date(2026, 9, 13)) == "ESZ26"  # on roll -> next qtr
    assert fmh.front_month_contract("ES", date(2026, 9, 14)) == "ESZ26"  # after roll


def test_front_month_year_rollover():
    # Dec 2026 expiry = 2026-12-18; roll = 12-13. After that -> Mar 2027.
    assert fmh.front_month_contract("ES", date(2026, 12, 20)) == "ESH27"
    assert fmh.front_month_contract("ES", date(2027, 1, 5))  == "ESH27"


# ── Session clock: is_market_open ─────────────────────────────────────────────

def test_open_sunday_boundary():
    # 2026-07-12 is a Sunday
    assert fmh.is_market_open(_dt(2026, 7, 12, 17, 59)) is False
    assert fmh.is_market_open(_dt(2026, 7, 12, 18, 0)) is True
    assert fmh.is_market_open(_dt(2026, 7, 12, 18, 1)) is True


def test_open_weekday_and_halt():
    # 2026-07-13 Monday
    assert fmh.is_market_open(_dt(2026, 7, 13, 9, 30)) is True
    assert fmh.is_market_open(_dt(2026, 7, 13, 16, 59)) is True
    assert fmh.is_market_open(_dt(2026, 7, 13, 17, 0)) is False   # halt begins
    assert fmh.is_market_open(_dt(2026, 7, 13, 17, 30)) is False
    assert fmh.is_market_open(_dt(2026, 7, 13, 18, 0)) is True    # resumes


def test_open_overnight():
    # 2026-07-15 Wednesday 03:00 — overnight session is open
    assert fmh.is_market_open(_dt(2026, 7, 15, 3, 0)) is True


def test_close_friday_and_weekend():
    # 2026-07-17 Friday
    assert fmh.is_market_open(_dt(2026, 7, 17, 16, 59)) is True
    assert fmh.is_market_open(_dt(2026, 7, 17, 17, 0)) is False   # weekly close
    assert fmh.is_market_open(_dt(2026, 7, 17, 20, 0)) is False
    # 2026-07-18 Saturday — closed all day
    assert fmh.is_market_open(_dt(2026, 7, 18, 12, 0)) is False


def test_closed_on_holiday():
    # Christmas 2026-12-25 (a Friday) — full closure even mid-day
    assert fmh.is_market_open(_dt(2026, 12, 25, 10, 0)) is False


# ── seconds_until_open / close ────────────────────────────────────────────────

def test_seconds_until_open_when_open_is_zero():
    assert fmh.seconds_until_open(_dt(2026, 7, 13, 10, 0)) == 0.0


def test_seconds_until_open_from_halt():
    # Monday 17:30 -> resumes 18:00 = 30 min
    assert fmh.seconds_until_open(_dt(2026, 7, 13, 17, 30)) == 1800.0


def test_seconds_until_open_over_weekend():
    # Friday 17:30 (closed) -> Sunday 18:00. 2 days + 30 min = 174600 s
    assert fmh.seconds_until_open(_dt(2026, 7, 17, 17, 30)) == 174600.0


def test_seconds_until_close_when_closed_is_zero():
    assert fmh.seconds_until_close(_dt(2026, 7, 18, 12, 0)) == 0.0


def test_seconds_until_close_intraday():
    # Monday 16:30 -> halt at 17:00 = 30 min
    assert fmh.seconds_until_close(_dt(2026, 7, 13, 16, 30)) == 1800.0
    # Wednesday 03:00 -> close at 17:00 = 14 h
    assert fmh.seconds_until_close(_dt(2026, 7, 15, 3, 0)) == 50400.0


def test_describe_next_open():
    assert fmh.describe_next_open(_dt(2026, 7, 17, 17, 30)) == "2026-07-19 18:00 ET"


# ── Script runner (mirrors test_indicators.py ergonomics) ─────────────────────

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"All {passed} assertions passed.")
