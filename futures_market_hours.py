"""
Futures (CME Globex) market-hours + contract-calendar awareness.

Mirrors the public surface of ``market_hours`` (now_et / is_market_open /
seconds_until_open / seconds_until_close) so ``main.py`` can treat either module
as an interchangeable "clock", and adds the futures-specific calendar:
symbol construction, the continuous "signal" symbol, and front-month resolution
with the agreed 5-day-before-expiry quarterly roll.

Session model (equity-index futures, e.g. ES/NQ/RTY):
    Sunday 18:00 ET  ->  Friday 17:00 ET,
    with a daily maintenance halt 17:00-18:00 ET (Mon-Thu).
On a CME full-closure holiday the whole calendar date is treated as closed.

MVP simplifications (see TODOs):
  * CME_HOLIDAYS holds full-closure dates only; partial / early-close sessions
    (e.g. day before Thanksgiving) are treated as fully open, and full holidays
    as fully closed — conservative either way for an auto-trader.
"""

from datetime import date, datetime, time, timedelta

import pytz

import config
from market_hours import _third_friday   # reuse: 3rd-Friday expiry math

_ET = pytz.timezone(config.MARKET_TZ)

# ── Session boundaries (ET) ───────────────────────────────────────────────────
_HALT_START     = time(17, 0)   # daily maintenance halt begins (also Fri weekly close)
_SESSION_START  = time(18, 0)   # session resumes (also Sunday weekly open)

# ── Quarterly cycle + month codes ─────────────────────────────────────────────
_MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J",  5: "K",  6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}
_QUARTERLY_MONTHS = (3, 6, 9, 12)   # H, M, U, Z — the equity-index cycle
_DEFAULT_ROLL_DAYS = 5              # roll this many days before expiry

# ── CME full-closure holidays (seed set — TODO: extend + handle partials) ──────
CME_HOLIDAYS = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving Day
    date(2026, 12, 25),  # Christmas Day
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed)
    date(2027, 7, 5),    # Independence Day (observed)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving Day
    date(2027, 12, 24),  # Christmas Day (observed)
})


def now_et() -> datetime:
    return datetime.now(_ET)


# ── Session clock ─────────────────────────────────────────────────────────────

def is_market_open(now: datetime = None) -> bool:
    """True if the CME Globex equity-index session is trading at ``now`` (ET)."""
    now = now or now_et()
    d, t, wd = now.date(), now.time(), now.weekday()   # weekday: Mon=0 … Sun=6

    if d in CME_HOLIDAYS:
        return False
    if wd == 5:                      # Saturday — closed all day
        return False
    if wd == 6:                      # Sunday — opens 18:00
        return t >= _SESSION_START
    if wd == 4:                      # Friday — closes 17:00 for the week
        return t < _HALT_START
    # Monday–Thursday — open except the 17:00–18:00 maintenance halt
    return t < _HALT_START or t >= _SESSION_START


def _scan_minutes(now: datetime, want_open: bool) -> float:
    """Seconds from ``now`` until is_market_open first equals ``want_open``.

    All session transitions fall on minute boundaries, so a minute-resolution
    scan is exact. Bounded to 8 days to comfortably clear weekends + holidays.
    """
    probe = now.replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 60):
        probe += timedelta(minutes=1)
        if is_market_open(probe) == want_open:
            return max(0.0, (probe - now).total_seconds())
    return 0.0   # unreachable in practice


def seconds_until_open(now: datetime = None) -> float:
    """Seconds until the next session open (0.0 if currently open)."""
    now = now or now_et()
    if is_market_open(now):
        return 0.0
    return _scan_minutes(now, want_open=True)


def seconds_until_close(now: datetime = None) -> float:
    """Seconds until the next halt/close (0.0 if currently closed)."""
    now = now or now_et()
    if not is_market_open(now):
        return 0.0
    return _scan_minutes(now, want_open=False)


def entries_allowed(now: datetime = None) -> bool:
    """True once the CME session has been open CROSS_ENTRY_DELAY_MINUTES.

    Mirrors ``market_hours.entries_allowed`` but anchors to the GLOBEX session
    open (18:00 ET), NOT the 9:30 equity bell — the ES daily bar runs 18:00 ->
    17:00 ET, so its unformed stub window is the evening reopen. Using the equity
    anchor here would be exactly backwards: it would block 9:30-10:00 (~15h into
    a fully formed bar) and wave through 18:00:05 (bar five seconds old).

    Before the 17:00 halt we are inside the session that opened the previous
    evening, so the bar is many hours old and entries are always allowed.
    """
    now = now or now_et()
    if not is_market_open(now):
        return False
    if now.time() < _SESSION_START:
        return True          # session opened last evening — bar long since formed
    session_open = now.replace(hour=_SESSION_START.hour,
                               minute=_SESSION_START.minute,
                               second=0, microsecond=0)
    return now >= session_open + timedelta(minutes=config.CROSS_ENTRY_DELAY_MINUTES)


def describe_next_open(now: datetime = None) -> str:
    """Human-readable next-open timestamp, e.g. '2026-07-19 18:00 ET'."""
    now = now or now_et()
    nxt = now + timedelta(seconds=seconds_until_open(now))
    return nxt.strftime("%Y-%m-%d %H:%M ET")


# ── Contract symbology + front-month roll ─────────────────────────────────────

def build_futures_symbol(root: str, month: int, year: int) -> str:
    """Assemble a dated futures symbol, e.g. ('ES', 9, 2026) -> 'ESU26'."""
    return f"{root}{_MONTH_CODES[month]}{year % 100:02d}"


def signal_symbol(root: str) -> str:
    """Continuous symbol used for indicator/bar history, e.g. 'ES' -> '@ES'."""
    return f"@{root}"


def _quarterly_slots(start_year: int, years: int = 3):
    """Yield (year, month) for each quarterly contract, oldest first."""
    for y in range(start_year, start_year + years):
        for m in _QUARTERLY_MONTHS:
            yield y, m


def front_month_contract(root: str, from_date: date = None,
                         roll_days: int = _DEFAULT_ROLL_DAYS) -> str:
    """Return the dated front-month contract to trade for ``root``.

    Picks the nearest quarterly contract whose roll date (expiry − ``roll_days``)
    is still in the future, so we roll to the next quarter ``roll_days`` days
    before the current front-month expires.
    """
    d = from_date or now_et().date()
    for y, m in _quarterly_slots(d.year):
        expiry = _third_friday(y, m)
        if d < expiry - timedelta(days=roll_days):
            return build_futures_symbol(root, m, y)
    # Unreachable given the 3-year horizon; fall back to the last slot.
    y, m = list(_quarterly_slots(d.year))[-1]
    return build_futures_symbol(root, m, y)
