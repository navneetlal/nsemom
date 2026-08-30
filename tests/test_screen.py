"""Screen mechanics, end to end over a synthetic database.

The property under test is the one that silently inflates returns when it is
wrong: the universe must be rebuilt from whatever actually traded on each date,
never from a present-day membership list. A symbol that later delists has to
appear on the dates it was tradeable.

Thresholds are deliberately permissive here - this exercises universe
construction and the SQL, not whether RSI 60-75 is a good band.
"""

import datetime as dt

import pandas as pd
import pytest

from nsemom.adjust import rebuild_intervals, verify
from nsemom.config import Config
from nsemom.indicators import rebuild
from nsemom.ingest.bhavcopy import Bar
from nsemom.screen import Preset, run
from nsemom.store.db import Store

WARMUP = 20
START = dt.date(2020, 1, 1)

PERMISSIVE = dict(
    require_ema_stack=False, require_close_above=False, rsi_min=0.0, rsi_max=100.0,
    adx_min=0.0, min_close=0.0, liquidity_mode="median", min_turnover=0.0,
    volume_mode="off", volume_metric="spike", volume_min_ratio=0.0,
    rank_by="mom_short", basket_size=50,
)


def _bars(symbol, n_bars, first_bar=0, base=100.0, drift=0.6):
    out = []
    for i in range(n_bars):
        price = base + drift * i
        day = START + dt.timedelta(days=first_bar + i)
        out.append(Bar(symbol, day, f"INE{symbol[:3]}01001", "EQ", price, price * 1.01,
                       price * 0.99, price, price, 100_000, price * 100_000, 500))
    return out


@pytest.fixture
def store(tmp_path):
    cfg = Config.load()
    with Store(tmp_path / "screen.duckdb") as s:
        yield s, cfg


def _build(store_and_cfg, all_bars):
    store, cfg = store_and_cfg
    for day in sorted({b.trade_date for b in all_bars}):
        store.write_day(day, "udiff", [b for b in all_bars if b.trade_date == day], None)
    verify(store, cfg.corporate_actions.verification_tolerance)
    rebuild_intervals(store)
    rebuild(store, cfg)
    return store


def test_a_symbol_that_later_delists_is_still_screened_while_it_traded(store):
    # GONE trades for 120 sessions then vanishes. STAYS trades throughout.
    # Screening from a present-day symbol list would silently drop GONE from
    # every historical date - the classic survivorship inflation.
    built = _build(store, _bars("GONE", 120) + _bars("STAYS", 240))
    preset = Preset.from_raw("permissive", PERMISSIVE)

    mid = START + dt.timedelta(days=100)
    early = run(built, preset, WARMUP, date_filter=f"= DATE '{mid}'")
    assert set(early["symbol"]) == {"GONE", "STAYS"}

    late = START + dt.timedelta(days=200)
    later = run(built, preset, WARMUP, date_filter=f"= DATE '{late}'")
    assert set(later["symbol"]) == {"STAYS"}, "a delisted name cannot trade later"


def test_a_symbol_listed_late_is_absent_before_it_listed(store):
    built = _build(store, _bars("OLD", 240) + _bars("NEW", 120, first_bar=120))
    preset = Preset.from_raw("permissive", PERMISSIVE)

    before = run(built, preset, WARMUP,
                 date_filter=f"= DATE '{START + dt.timedelta(days=100)}'")
    assert set(before["symbol"]) == {"OLD"}


def test_warmup_excludes_bars_without_enough_history(store):
    built = _build(store, _bars("SLOW", 240))
    preset = Preset.from_raw("permissive", PERMISSIVE)

    frame = run(built, preset, min_warmup_bars=100)
    first_screened = pd.Timestamp(frame["trade_date"].min()).date()
    assert first_screened >= START + dt.timedelta(days=99)


def test_quarantined_symbols_are_excluded_for_the_quarantine_window(store):
    built = _build(store, _bars("CLEAN", 240) + _bars("MESSY", 240))
    built.con.execute(
        "INSERT INTO quarantine VALUES ('MESSY', ?, ?, 'demerger')",
        [START, START + dt.timedelta(days=150)],
    )
    preset = Preset.from_raw("permissive", PERMISSIVE)

    during = run(built, preset, WARMUP,
                 date_filter=f"= DATE '{START + dt.timedelta(days=100)}'")
    assert set(during["symbol"]) == {"CLEAN"}

    after = run(built, preset, WARMUP,
                date_filter=f"= DATE '{START + dt.timedelta(days=200)}'")
    assert set(after["symbol"]) == {"CLEAN", "MESSY"}


def test_the_liquidity_floor_uses_the_median_not_the_current_bar(store):
    # THIN is illiquid every day except one spike. A same-day turnover filter
    # admits it on the spike; a 20-day median floor does not. This is the
    # circularity that was measured at 44% of passing bars on real data.
    thin = _bars("THIN", 240, base=100.0, drift=0.0)
    spike_day = 200
    thin[spike_day] = thin[spike_day]._replace() if hasattr(thin[spike_day], "_replace") \
        else thin[spike_day]
    thin = [
        Bar(b.symbol, b.trade_date, b.isin, b.series, b.open, b.high, b.low, b.close,
            b.prev_close, 50_000_000 if i == spike_day else 1_000,
            b.close * (50_000_000 if i == spike_day else 1_000), b.trades)
        for i, b in enumerate(thin)
    ]
    built = _build(store, thin)
    spike_date = thin[spike_day].trade_date

    median_preset = Preset.from_raw("median", {**PERMISSIVE,
                                               "liquidity_mode": "median",
                                               "min_turnover": 1e8})
    same_day_preset = Preset.from_raw("same_day", {**PERMISSIVE,
                                                   "liquidity_mode": "same_day",
                                                   "min_turnover": 1e8})

    by_median = run(built, median_preset, WARMUP, date_filter=f"= DATE '{spike_date}'")
    by_same_day = run(built, same_day_preset, WARMUP, date_filter=f"= DATE '{spike_date}'")

    assert by_median.empty, "a one-day spike must not satisfy a median floor"
    assert not by_same_day.empty, "the same-day filter is expected to admit it"


def test_ranking_is_descending_on_the_configured_column(store):
    built = _build(store, _bars("FAST", 240, drift=1.5) + _bars("SLOW", 240, drift=0.1))
    preset = Preset.from_raw("permissive", PERMISSIVE)

    frame = run(built, preset, WARMUP,
                date_filter=f"= DATE '{START + dt.timedelta(days=200)}'")
    ordered = frame.sort_values("rank")["symbol"].tolist()
    assert ordered[0] == "FAST", f"ranking is not by momentum: {ordered}"
    assert frame["mom_short"].is_monotonic_decreasing


def test_indicators_stay_aligned_to_their_own_symbol_and_bar(store):
    # Batched per-symbol computation is reattached to (symbol, trade_date). If
    # that join key ever drifted, every indicator would silently belong to the
    # wrong bar - and the numbers would still look entirely plausible.
    built = _build(store, _bars("AAA", 60, base=100.0, drift=1.0)
                   + _bars("MMM", 60, base=500.0, drift=1.0)
                   + _bars("ZZZ", 60, base=900.0, drift=1.0))

    rows = built.con.execute(
        """SELECT symbol, close, ema_20 FROM indicators
            WHERE trade_date = ? ORDER BY symbol""",
        [START + dt.timedelta(days=55)],
    ).fetchall()

    assert len(rows) == 3
    for symbol, close, ema in rows:
        # each EMA must track its own symbol's price level, not a neighbour's
        assert abs(close - ema) < 20, f"{symbol}: ema {ema} is not near close {close}"
