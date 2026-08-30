"""Storage tests, focused on the guarantee backfill depends on: re-running must
never duplicate a row."""

import datetime as dt

import pytest

from nsemom.ingest.bhavcopy import Bar
from nsemom.store.db import Store

DAY = dt.date(2024, 3, 1)


def _bars(day=DAY, close=10.5):
    return [
        Bar("AAA", day, "INE001A01001", "EQ", 10, 11, 9, close, 10, 100, 1050, 7),
        Bar("BBB", day, "INE002A01002", "EQ", 20, 21, 19, 20.5, 20, 200, 4100, 9),
    ]


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.duckdb") as s:
        yield s


def test_rewriting_a_session_replaces_rather_than_appends(store):
    store.write_day(DAY, "udiff", _bars(), "sha-1")
    store.write_day(DAY, "udiff", _bars(), "sha-1")
    store.write_day(DAY, "udiff", _bars(), "sha-1")

    assert store.con.execute("SELECT count(*) FROM prices").fetchone()[0] == 2
    assert store.duplicate_row_count() == 0


def test_a_restated_session_overwrites_the_old_values(store):
    store.write_day(DAY, "udiff", _bars(close=10.5), "sha-1")
    store.write_day(DAY, "udiff", _bars(close=99.0), "sha-2")

    close = store.con.execute(
        "SELECT close FROM prices WHERE symbol = 'AAA'"
    ).fetchone()[0]
    assert close == 99.0
    sha = store.con.execute(
        "SELECT content_sha FROM ingest_log WHERE trade_date = ?", [DAY]
    ).fetchone()[0]
    assert sha == "sha-2"


def test_rewriting_one_session_leaves_neighbours_untouched(store):
    other = dt.date(2024, 3, 4)
    store.write_day(DAY, "udiff", _bars(), "sha-1")
    store.write_day(other, "udiff", _bars(day=other), "sha-2")
    store.write_day(DAY, "udiff", _bars(), "sha-1")

    assert store.con.execute("SELECT count(*) FROM prices").fetchone()[0] == 4
    assert store.duplicate_row_count() == 0


def test_only_ok_and_holiday_count_as_settled(store):
    store.write_day(DAY, "udiff", _bars(), "sha-1")
    store.mark(dt.date(2024, 3, 5), "holiday")
    store.mark(dt.date(2024, 3, 6), "pending")
    store.mark(dt.date(2024, 3, 7), "failed", note="boom")

    # pending and failed must be retried on the next run, not skipped
    assert store.settled_dates() == {DAY, dt.date(2024, 3, 5)}


def test_a_pending_day_settles_once_it_arrives(store):
    day = dt.date(2024, 3, 6)
    store.mark(day, "pending")
    assert store.settled_dates() == set()
    store.write_day(day, "udiff", _bars(day=day), "sha-9")
    assert store.settled_dates() == {day}
