"""Utilities for NYSE market-hours awareness."""

from datetime import date, datetime, time, timedelta
import pytz
import config

_ET = pytz.timezone(config.MARKET_TZ)
_OPEN  = time(config.MARKET_OPEN_HOUR,  config.MARKET_OPEN_MIN)
_CLOSE = time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MIN)

# ── NYSE full-day market holidays (observed dates) ────────────────────────────
# These are the dates the exchange is fully CLOSED. Observance rules already
# applied: a holiday on Saturday is observed the preceding Friday, on Sunday the
# following Monday (e.g. Independence Day 2026 = Sat Jul 4 -> observed Fri Jul 3;
# note Mon Jul 6 is a NORMAL trading day). Early-close half-days (e.g. day after
# Thanksgiving) are NOT full closures and are intentionally omitted.
# Extend this set each year — verify against the official NYSE calendar.
MARKET_HOLIDAYS = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Washington's Birthday (Presidents' Day)
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed; Jul 4 is a Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving Day
    date(2026, 12, 25),  # Christmas Day
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # Martin Luther King Jr. Day
    date(2027, 2, 15),   # Washington's Birthday (Presidents' Day)
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed; Jun 19 is a Saturday)
    date(2027, 7, 5),    # Independence Day (observed; Jul 4 is a Sunday)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving Day
    date(2027, 12, 24),  # Christmas Day (observed; Dec 25 is a Saturday)
    date(2027, 12, 31),  # New Year's Day 2028 (observed; Jan 1 2028 is a Saturday)
    # 2028
    date(2028, 1, 17),   # Martin Luther King Jr. Day
    date(2028, 2, 21),   # Washington's Birthday (Presidents' Day)
    date(2028, 4, 14),   # Good Friday
    date(2028, 5, 29),   # Memorial Day
    date(2028, 6, 19),   # Juneteenth
    date(2028, 7, 4),    # Independence Day
    date(2028, 9, 4),    # Labor Day
    date(2028, 11, 23),  # Thanksgiving Day
    date(2028, 12, 25),  # Christmas Day
    # 2029
    date(2029, 1, 1),    # New Year's Day
    date(2029, 1, 15),   # Martin Luther King Jr. Day
    date(2029, 2, 19),   # Washington's Birthday (Presidents' Day)
    date(2029, 3, 30),   # Good Friday
    date(2029, 5, 28),   # Memorial Day
    date(2029, 6, 19),   # Juneteenth
    date(2029, 7, 4),    # Independence Day
    date(2029, 9, 3),    # Labor Day
    date(2029, 11, 22),  # Thanksgiving Day
    date(2029, 12, 25),  # Christmas Day
    # 2030
    date(2030, 1, 1),    # New Year's Day
    date(2030, 1, 21),   # Martin Luther King Jr. Day
    date(2030, 2, 18),   # Washington's Birthday (Presidents' Day)
    date(2030, 4, 19),   # Good Friday
    date(2030, 5, 27),   # Memorial Day
    date(2030, 6, 19),   # Juneteenth
    date(2030, 7, 4),    # Independence Day
    date(2030, 9, 2),    # Labor Day
    date(2030, 11, 28),  # Thanksgiving Day
    date(2030, 12, 25),  # Christmas Day
})


def now_et() -> datetime:
    return datetime.now(_ET)


def is_holiday(d: date) -> bool:
    """Return True if `d` is a full-day NYSE market holiday."""
    return d in MARKET_HOLIDAYS


def _is_trading_day(d: date) -> bool:
    """A weekday (Mon–Fri) that is not a market holiday."""
    return d.weekday() < 5 and d not in MARKET_HOLIDAYS


def _third_friday(year: int, month: int) -> date:
    """Return the date of the 3rd Friday of the given month."""
    first = date(year, month, 1)
    # weekday(): Mon=0 … Fri=4. Days until the first Friday, then +14 for the 3rd.
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def next_monthly_expiration(from_date: date = None) -> str:
    """Next standard monthly option expiration (3rd Friday) as 'YYYY-MM-DD'.

    If this month's 3rd Friday has already passed — or is today — roll forward
    to next month's 3rd Friday so we never select an expired/same-day contract.
    """
    d = from_date or now_et().date()
    third = _third_friday(d.year, d.month)
    if d >= third:
        year  = d.year + 1 if d.month == 12 else d.year
        month = 1 if d.month == 12 else d.month + 1
        third = _third_friday(year, month)
    return third.isoformat()


def is_market_open() -> bool:
    """Return True if current ET time is within the NYSE regular session on a
    trading day (Mon–Fri, excluding market holidays)."""
    now = now_et()
    if not _is_trading_day(now.date()):
        return False
    current_time = now.time()
    return _OPEN <= current_time < _CLOSE


def entries_allowed(now: datetime = None) -> bool:
    """True once the regular session has been open CROSS_ENTRY_DELAY_MINUTES.

    Gates ENTRIES only — exits and stops stay live from the bell. The daily bar
    the EMAs are computed from is still forming; at 9:30:05 it holds seconds of
    data. See config.CROSS_ENTRY_DELAY_MINUTES.

    `futures_market_hours.entries_allowed` mirrors this, anchored to the CME
    session open instead, so main.py can treat either module as the same clock.
    """
    now = now or now_et()
    if not _is_trading_day(now.date()):
        return False
    if not (_OPEN <= now.time() < _CLOSE):
        return False
    open_dt = now.replace(hour=config.MARKET_OPEN_HOUR,
                          minute=config.MARKET_OPEN_MIN,
                          second=0, microsecond=0)
    return now >= open_dt + timedelta(minutes=config.CROSS_ENTRY_DELAY_MINUTES)


def seconds_until_open() -> float:
    """Seconds until the next market open (always positive).

    Rolls forward over weekends AND market holidays so the bot never wakes to
    trade on a day the exchange is closed.
    """
    now = now_et()
    candidate = now.replace(
        hour=_OPEN.hour, minute=_OPEN.minute, second=0, microsecond=0
    )
    # If we're already past today's open, or today isn't a trading day, advance
    # to the next day, then skip any further weekend/holiday days.
    if now >= candidate or not _is_trading_day(now.date()):
        candidate += timedelta(days=1)
    while not _is_trading_day(candidate.date()):
        candidate += timedelta(days=1)
    delta = (candidate - now).total_seconds()
    return max(0.0, delta)


def describe_next_open(now: datetime = None) -> str:
    """Human-readable next-open timestamp, e.g. '2026-07-13 09:30 ET'.

    Mirrors futures_market_hours.describe_next_open so main.py can treat either
    module as an interchangeable clock.
    """
    base = now or now_et()
    nxt = base + timedelta(seconds=seconds_until_open())
    return nxt.strftime("%Y-%m-%d %H:%M ET")


def seconds_until_close() -> float:
    """Seconds until today's close (assumes market is currently open)."""
    now = now_et()
    close_dt = now.replace(
        hour=_CLOSE.hour, minute=_CLOSE.minute, second=0, microsecond=0
    )
    delta = (close_dt - now).total_seconds()
    return max(0.0, delta)
