"""Trading-day candidates.

There is no hardcoded NSE holiday list. Weekdays are candidates; whether one was
a trading day is discovered from the archive itself and cached in ingest_log, so
the calendar builds itself and re-runs cost nothing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

SATURDAY = 5


def weekdays(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    day = start
    while day <= end:
        if day.weekday() < SATURDAY:
            yield day
        day += dt.timedelta(days=1)


def is_conclusively_absent(day: dt.date, today: dt.date, confirm_after_days: int) -> bool:
    """A missing file is only a holiday once it is old enough that NSE would
    certainly have published it. Younger than that it stays 'pending', so a run
    before the ~18:30 IST publication cannot mark today a holiday forever."""
    return (today - day).days >= confirm_after_days
