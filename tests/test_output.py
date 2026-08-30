"""Daily output: health gating and deterministic exit reporting.

The health check exists so the scheduled job fails loudly instead of writing a
confident-looking shortlist from a half-ingested session.
"""

import datetime as dt

import pytest

from nsemom.adjust import rebuild_intervals, verify
from nsemom.backtest import ExitRules
from nsemom.config import Config
from nsemom.indicators import rebuild
from nsemom.ingest.bhavcopy import Bar
from nsemom.output import (HealthError, check_health, latest_date,
                           log_shortlist, open_position_status, shortlist)
from nsemom.screen import Preset
from nsemom.store.db import Store

START = dt.date(2024, 1, 1)
RULES = ExitRules(2.5, 3.0, 1.0, 2, max_hold_days=90, min_hold_days=30)

PERMISSIVE = dict(
    require_ema_stack=False, require_close_above=False, rsi_min=0.0, rsi_max=100.0,
    adx_min=0.0, min_close=0.0, liquidity_mode="median", min_turnover=0.0,
    volume_mode="off", volume_metric="spike", volume_min_ratio=0.0,
    rank_by="mom_short", basket_size=10,
)


def _bars(symbol, n, base=100.0, drift=1.0, volume=100_000):
    return [
        Bar(symbol, START + dt.timedelta(days=i), "INE1", "EQ",
            base + drift * i, (base + drift * i) * 1.02, (base + drift * i) * 0.98,
            base + drift * i, base + drift * i, volume,
            (base + drift * i) * volume, 500)
        for i in range(n)
    ]


@pytest.fixture
def built(tmp_path):
    cfg = Config.load()
    store = Store(tmp_path / "out.duckdb")
    bars = _bars("ALPHA", 120) + _bars("BETA", 120, base=200.0, drift=0.5)
    for day in sorted({b.trade_date for b in bars}):
        store.write_day(day, "udiff", [b for b in bars if b.trade_date == day], None)
    verify(store, 0.1)
    rebuild_intervals(store)
    rebuild(store, cfg)
    yield store, cfg
    store.close()


def test_health_rejects_a_date_with_no_rows(built):
    store, _ = built
    with pytest.raises(HealthError, match="no price rows"):
        check_health(store, dt.date(2030, 1, 1))


def test_health_rejects_a_suspiciously_thin_session(built):
    store, _ = built
    thin_day = START + dt.timedelta(days=120)   # the session right after the data
    lone = Bar("ALPHA", thin_day, "INE1", "EQ", 100, 101, 99, 100, 100,
               1000, 100_000, 10)
    store.write_day(thin_day, "udiff", [lone], None)
    # one row against a 40-day median of two is below the 0.8 ratio
    with pytest.raises(HealthError, match="partial session"):
        check_health(store, thin_day)


def test_health_refuses_to_screen_while_an_ingest_date_is_failed(built):
    store, _ = built
    store.mark(dt.date(2024, 3, 1), "failed", note="boom")
    with pytest.raises(HealthError, match="failed to ingest"):
        check_health(store, latest_date(store))


def test_health_passes_on_a_normal_session(built):
    store, _ = built
    check_health(store, latest_date(store))     # must not raise


def test_shortlist_carries_the_evidence_columns_and_a_stop(built):
    store, cfg = built
    frame = shortlist(store, Preset.from_raw("p", PERMISSIVE), RULES, 20,
                      latest_date(store))
    assert not frame.empty
    for column in ("ext_zscore", "pct_from_high", "trend_age_days",
                   "median_turnover", "suggested_stop"):
        assert column in frame.columns
    row = frame.iloc[0]
    assert row["suggested_stop"] == pytest.approx(row["close"] - 2.5 * row["atr"])
    assert row["suggested_stop"] < row["close"]


def test_a_healthy_position_reads_hold(built):
    store, cfg = built
    entry = START + dt.timedelta(days=40)
    store.con.execute("INSERT INTO positions VALUES (1,'ALPHA',?,?,10,NULL,NULL,NULL,NULL)",
                      [entry, 140.0])
    status = open_position_status(store, RULES, 50, latest_date(store))
    assert status["action"].iloc[0] == "HOLD"
    assert status["unrealised_return"].iloc[0] > 0


def test_a_position_below_its_initial_stop_reads_exit(built):
    store, cfg = built
    # entered far above the eventual price, so the initial stop is breached
    entry = START + dt.timedelta(days=40)
    store.con.execute("INSERT INTO positions VALUES (1,'ALPHA',?,?,10,NULL,NULL,NULL,NULL)",
                      [entry, 100_000.0])
    status = open_position_status(store, RULES, 50, latest_date(store))
    assert status["action"].iloc[0].startswith("EXIT")


def test_a_position_past_max_hold_reads_a_time_stop(built):
    store, cfg = built
    rules = ExitRules(2.5, 3.0, 1.0, 2, max_hold_days=10, min_hold_days=5)
    entry = START + dt.timedelta(days=40)
    store.con.execute("INSERT INTO positions VALUES (1,'ALPHA',?,?,10,NULL,NULL,NULL,NULL)",
                      [entry, 140.0])
    status = open_position_status(store, rules, 50, latest_date(store))
    assert status["action"].iloc[0] == "EXIT: time stop"


def test_closed_positions_are_not_reported(built):
    store, cfg = built
    entry = START + dt.timedelta(days=40)
    store.con.execute(
        "INSERT INTO positions VALUES (1,'ALPHA',?,?,10,?,160.0,'manual',NULL)",
        [entry, 140.0, START + dt.timedelta(days=80)])
    assert open_position_status(store, RULES, 50, latest_date(store)).empty


def test_every_shortlist_row_is_logged_for_later_comparison(built):
    store, cfg = built
    as_of = latest_date(store)
    frame = shortlist(store, Preset.from_raw("p", PERMISSIVE), RULES, 20, as_of)
    log_shortlist(store, frame, "p", as_of)

    logged = store.con.execute("SELECT count(*) FROM shortlist_log").fetchone()[0]
    assert logged == len(frame)
    # re-logging the same day must not duplicate
    log_shortlist(store, frame, "p", as_of)
    assert store.con.execute("SELECT count(*) FROM shortlist_log").fetchone()[0] == logged
