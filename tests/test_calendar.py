import datetime as dt

from nsemom.calendar import is_conclusively_absent, weekdays


def test_weekends_are_never_candidates():
    days = list(weekdays(dt.date(2026, 8, 24), dt.date(2026, 8, 30)))
    assert days == [dt.date(2026, 8, d) for d in (24, 25, 26, 27, 28)]
    assert all(d.weekday() < 5 for d in days)


def test_single_day_range_is_inclusive():
    assert list(weekdays(dt.date(2026, 8, 28), dt.date(2026, 8, 28))) == [
        dt.date(2026, 8, 28)
    ]


def test_todays_missing_file_is_not_yet_a_holiday():
    # NSE publishes around 18:00-18:30 IST. A run before that must not record
    # today as a holiday, or the date is skipped forever.
    today = dt.date(2026, 8, 28)
    assert not is_conclusively_absent(today, today, confirm_after_days=2)
    assert not is_conclusively_absent(dt.date(2026, 8, 27), today, confirm_after_days=2)


def test_an_old_missing_file_is_conclusively_a_holiday():
    today = dt.date(2026, 8, 28)
    assert is_conclusively_absent(dt.date(2026, 8, 26), today, confirm_after_days=2)
    assert is_conclusively_absent(dt.date(2015, 1, 26), today, confirm_after_days=2)
