"""When a metered feature's period starts and ends. Pure functions, no database."""

import calendar
from datetime import datetime, timedelta, timezone

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)  # the single period of features that never reset


def add_months(start: datetime, months: int) -> datetime:
    """Same day `months` later, clamped to the end of shorter months (Jan 31 -> Feb 28 -> Mar 31)."""
    month = start.month - 1 + months
    year, month = start.year + month // 12, month % 12 + 1
    return start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))


def _step(start: datetime, period: str, n: int) -> datetime:
    if period == "day":
        return start + timedelta(days=n)
    if period == "week":
        return start + timedelta(weeks=n)
    return add_months(start, n * (12 if period == "year" else 1))


def bounds(period: str, now: datetime, anchor: datetime) -> tuple[datetime, datetime | None]:
    """(start, end) of the period containing `now`. Periods are counted from `anchor`, always from the anchor itself,
    so a month that starts on the 31st comes back to the 31st. `end` is None for features that never reset.

    For calendar periods, pass an anchor on a calendar boundary in the right time zone (see `calendar_anchor`)."""
    if period == "never":
        return EPOCH, None
    if period in ("day", "week"):
        size = timedelta(days=1 if period == "day" else 7)
        n = (now - anchor) // size
    else:
        n = (now.year - anchor.year) * 12 + now.month - anchor.month
        if period == "year":
            n //= 12
        if _step(anchor, period, n) > now:
            n -= 1
    return _step(anchor, period, n), _step(anchor, period, n + 1)


def calendar_anchor(period: str, now: datetime) -> datetime:
    """A calendar boundary to count from: midnight, a Monday, the 1st of the month or Jan 1, in now's time zone."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return midnight - timedelta(days=midnight.weekday())
    if period == "month":
        return midnight.replace(day=1)
    if period == "year":
        return midnight.replace(month=1, day=1)
    return midnight
