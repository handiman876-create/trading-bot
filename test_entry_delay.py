"""
Unit tests for the post-open entry delay on both clocks — NO network.

Signals run on DAILY bars whose last bar is today's still-forming bar. At the
bell that bar holds seconds of data, so its EMAs are noise: on 2026-07-15 QQQ
fired a "bullish cross" at 9:30:05 with the EMAs 0.017% apart and was back below
within 44 minutes; HCA was bought at 9:35 on a 5-minute-old bar at RSI 35.

The equity and futures clocks anchor to DIFFERENT session opens, and getting
that wrong inverts the filter — see test_futures_uses_session_open_not_the_bell.

Run:  python3 test_entry_delay.py
"""

from datetime import datetime

import pytz

import config
import market_hours as mh
import futures_market_hours as fmh

_ET = pytz.timezone(config.MARKET_TZ)


def _et(y, m, d, hh, mm):
    return _ET.localize(datetime(y, m, d, hh, mm))


# Wed 2026-07-15 — a normal trading day (the day the defect was found).
# Sun 2026-07-19 / Mon 2026-07-20 — used for the CME weekly open.


# ── Equities: NYSE 9:30 bell ──────────────────────────────────────────────────

def test_equity_entries_blocked_at_the_bell():
    assert mh.entries_allowed(_et(2026, 7, 15, 9, 30)) is False, \
        "9:30:00 — the daily bar has no data yet"


def test_equity_entries_blocked_at_qqq_entry_time():
    """QQQ's actual fill was 9:30:05 ET. This is the entry the delay prevents."""
    assert mh.entries_allowed(_et(2026, 7, 15, 9, 30)) is False


def test_equity_entries_blocked_at_hca_entry_time():
    """HCA's actual fill was 9:35:33 ET — a momentum alignment entry, which is
    why the delay gates the momentum path too."""
    assert mh.entries_allowed(_et(2026, 7, 15, 9, 35)) is False


def test_equity_entries_blocked_one_minute_before_cutoff():
    assert mh.entries_allowed(_et(2026, 7, 15, 9, 59)) is False


def test_equity_entries_allowed_at_cutoff():
    assert mh.entries_allowed(_et(2026, 7, 15, 10, 0)) is True, \
        "10:00 = 9:30 + CROSS_ENTRY_DELAY_MINUTES"


def test_equity_entries_allowed_midday():
    """GOOGL entered at 11:15 ET — the delay does NOT catch it. Its 0.000% gap
    needs the separate gap filter, deliberately deferred."""
    assert mh.entries_allowed(_et(2026, 7, 15, 11, 15)) is True


def test_equity_entries_blocked_after_close():
    assert mh.entries_allowed(_et(2026, 7, 15, 16, 30)) is False


def test_equity_entries_blocked_premarket():
    assert mh.entries_allowed(_et(2026, 7, 15, 8, 0)) is False


def test_equity_entries_blocked_on_weekend():
    assert mh.entries_allowed(_et(2026, 7, 18, 11, 0)) is False, "Saturday"


def test_equity_entries_blocked_on_holiday():
    assert mh.entries_allowed(_et(2026, 7, 3, 11, 0)) is False, \
        "Independence Day (observed) — a full NYSE closure"


def test_equity_delay_tracks_config():
    """The cutoff is derived, not hard-coded at 10:00."""
    orig = config.CROSS_ENTRY_DELAY_MINUTES
    try:
        config.CROSS_ENTRY_DELAY_MINUTES = 60
        assert mh.entries_allowed(_et(2026, 7, 15, 10, 0)) is False
        assert mh.entries_allowed(_et(2026, 7, 15, 10, 30)) is True
    finally:
        config.CROSS_ENTRY_DELAY_MINUTES = orig


# ── Futures: CME 18:00 ET session open ────────────────────────────────────────

def test_futures_entries_blocked_at_session_open():
    assert fmh.entries_allowed(_et(2026, 7, 15, 18, 0)) is False, \
        "18:00 — a fresh ES daily bar with no data"


def test_futures_entries_blocked_inside_delay():
    assert fmh.entries_allowed(_et(2026, 7, 15, 18, 29)) is False


def test_futures_entries_allowed_after_delay():
    assert fmh.entries_allowed(_et(2026, 7, 15, 18, 30)) is True


def test_futures_uses_session_open_not_the_bell():
    """The anchor matters. At 9:30 ET the ES daily bar is ~15.5h old and fully
    formed, so entries must be ALLOWED — using the equity anchor here would block
    a formed bar and wave through the 18:00 stub, exactly backwards."""
    assert fmh.entries_allowed(_et(2026, 7, 15, 9, 30)) is True
    assert mh.entries_allowed(_et(2026, 7, 15, 9, 30)) is False


def test_futures_entries_allowed_overnight():
    """03:00 ET is deep inside the session that opened at 18:00 yesterday."""
    assert fmh.entries_allowed(_et(2026, 7, 15, 3, 0)) is True


def test_futures_entries_blocked_during_maintenance_halt():
    assert fmh.entries_allowed(_et(2026, 7, 15, 17, 30)) is False


def test_futures_entries_blocked_after_friday_close():
    assert fmh.entries_allowed(_et(2026, 7, 17, 18, 0)) is False, \
        "Friday 17:00 closes the week — no Friday evening reopen"


def test_futures_sunday_open_applies_delay():
    assert fmh.entries_allowed(_et(2026, 7, 19, 18, 0)) is False, "Sunday 18:00 stub"
    assert fmh.entries_allowed(_et(2026, 7, 19, 18, 30)) is True


def test_futures_entries_blocked_on_cme_holiday():
    assert fmh.entries_allowed(_et(2026, 9, 7, 12, 0)) is False, "Labor Day"


# ── Both clocks expose the same surface ───────────────────────────────────────

def test_clock_modules_are_interchangeable():
    """main.py treats either module as the same clock; entries_allowed joins
    is_market_open / seconds_until_open / seconds_until_close on that surface."""
    for mod in (mh, fmh):
        assert callable(getattr(mod, "entries_allowed", None)), mod.__name__


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
        passed += 1
    print(f"All {passed} assertions passed.")
