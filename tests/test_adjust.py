"""Corporate action tests.

Bhavcopy is unadjusted, so an unhandled 1:5 split reads as an 80% single-day
crash that trips every filter and poisons every EMA for the next 200 bars. These
are modelled on real NSE events, including the compound case that a naive
implementation gets wrong.
"""

import datetime as dt

import pytest

from nsemom.adjust import rebuild_intervals, verify
from nsemom.ingest.bhavcopy import Bar
from nsemom.ingest.corpactions import CorporateAction
from nsemom.store.db import Store

TOLERANCE = 0.10
EX = dt.date(2025, 6, 16)


def _price_series(symbol, pre_close, post_close, ex_date=EX, days=6):
    """Bars either side of an ex-date, with the split-day gap NSE actually
    prints: prev_close on the old basis, open on the new one."""
    bars = []
    for i in range(days):
        day = ex_date - dt.timedelta(days=days - i)
        bars.append(Bar(symbol, day, "INE001A01001", "EQ", pre_close, pre_close,
                        pre_close, pre_close, pre_close, 1000, pre_close * 1000, 10))
    bars.append(Bar(symbol, ex_date, "INE001A01001", "EQ", post_close, post_close,
                    post_close, post_close, pre_close,  # prev_close stays unadjusted
                    1000, post_close * 1000, 10))
    for i in range(1, days):
        day = ex_date + dt.timedelta(days=i)
        bars.append(Bar(symbol, day, "INE001A01001", "EQ", post_close, post_close,
                        post_close, post_close, post_close, 1000,
                        post_close * 1000, 10))
    return bars


def _action(symbol, subject, action_type, factor, ex_date=EX):
    return CorporateAction(symbol, ex_date, "INE001A01001", "EQ", action_type,
                           subject, None, factor)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "adj.duckdb") as s:
        yield s


def _load(store, bars, actions):
    for day in sorted({b.trade_date for b in bars}):
        store.write_day(day, "udiff", [b for b in bars if b.trade_date == day], None)
    store.upsert_corporate_actions(actions)
    verdicts = verify(store, TOLERANCE)
    rebuild_intervals(store)
    return verdicts


def _adjusted(store, symbol):
    return {
        row[0]: (row[1], row[2], row[3])
        for row in store.con.execute(
            "SELECT trade_date, close, volume, turnover FROM prices_adjusted "
            "WHERE symbol = ? ORDER BY trade_date", [symbol]
        ).fetchall()
    }


def test_a_one_for_five_split_makes_history_continuous(store):
    # COFORGE, 2025-06-04: face value split Rs 10 -> Rs 2, price 8499 -> 1719
    _load(store, _price_series("SPLITCO", 1000.0, 200.0),
          [_action("SPLITCO", "Face Value Split - From Rs 10/- To Rs 2/-", "split", 5.0)])

    series = _adjusted(store, "SPLITCO")
    closes = {round(c, 6) for c, _, _ in series.values()}
    assert closes == {200.0}, f"split left a discontinuity: {sorted(closes)}"


def test_compound_actions_sharing_an_ex_date_multiply(store):
    # BAJFINANCE, 2025-06-16: a 2:1 face-value split AND a 4:1 bonus on the same
    # ex-date. They compound to 10x - applying either alone leaves the series
    # wrong by 5x or 2x, and the price gap agrees only with the product.
    verdicts = _load(
        store, _price_series("BAJTEST", 9331.0, 933.1),
        [_action("BAJTEST", "Face Value Split - From Rs 2/- To Re 1/-", "split", 2.0),
         _action("BAJTEST", "Bonus 4:1", "bonus", 5.0)],
    )
    assert verdicts.get("verified") == 2

    series = _adjusted(store, "BAJTEST")
    closes = {round(c, 4) for c, _, _ in series.values()}
    assert closes == {933.1}, f"compound adjustment wrong: {sorted(closes)}"


def test_applying_only_one_of_two_same_day_actions_would_be_caught(store):
    # The same event with only the bonus recorded: parsed 5x against an observed
    # 10x gap is a 50% disagreement, so it must be rejected, not applied.
    verdicts = _load(store, _price_series("BAJPART", 9331.0, 933.1),
                     [_action("BAJPART", "Bonus 4:1", "bonus", 5.0)])
    assert verdicts.get("mismatch") == 1
    assert verdicts.get("verified") is None

    closes = {round(c, 4) for c, _, _ in _adjusted(store, "BAJPART").values()}
    assert closes == {9331.0, 933.1}, "a mismatched ratio must not be applied"


def test_a_consolidation_adjusts_history_upward(store):
    # VERTOZ, 2025-06-25: consolidation Re 1 -> Rs 10, price 9.81 -> 96.28
    _load(store, _price_series("VERTEST", 9.81, 98.1),
          [_action("VERTEST", "Consolidation From Re 1 To Rs 10", "consolidation", 0.1)])

    closes = {round(c, 4) for c, _, _ in _adjusted(store, "VERTEST").values()}
    assert closes == {98.1}


def test_sequential_actions_compound_across_time(store):
    # Two 1:2 splits on different dates: bars before the first must be divided
    # by 4, bars between them by 2, bars after by 1.
    first, second = dt.date(2025, 3, 10), dt.date(2025, 6, 16)
    bars = [Bar("SEQCO", d, "INE001A01001", "EQ", p, p, p, p, p, 1000, p * 1000, 10)
            for d, p in [(dt.date(2025, 3, 3), 400.0), (first, 200.0),
                         (dt.date(2025, 4, 1), 200.0), (second, 100.0),
                         (dt.date(2025, 7, 1), 100.0)]]
    # give each ex-date the gap that implies its own factor
    bars[1] = bars[1]._replace() if hasattr(bars[1], "_replace") else bars[1]
    bars = [b if b.trade_date != first else
            Bar("SEQCO", first, "INE001A01001", "EQ", 200.0, 200.0, 200.0, 200.0,
                400.0, 1000, 200000.0, 10) for b in bars]
    bars = [b if b.trade_date != second else
            Bar("SEQCO", second, "INE001A01001", "EQ", 100.0, 100.0, 100.0, 100.0,
                200.0, 1000, 100000.0, 10) for b in bars]

    _load(store, bars,
          [_action("SEQCO", "Bonus 1:1", "bonus", 2.0, ex_date=first),
           _action("SEQCO", "Bonus 1:1", "bonus", 2.0, ex_date=second)])

    series = _adjusted(store, "SEQCO")
    assert round(series[dt.date(2025, 3, 3)][0], 6) == 100.0   # divided by 4
    assert round(series[dt.date(2025, 4, 1)][0], 6) == 100.0   # divided by 2
    assert round(series[dt.date(2025, 7, 1)][0], 6) == 100.0   # unadjusted


def test_volume_is_scaled_inversely_and_turnover_is_invariant(store):
    _load(store, _price_series("VOLCO", 1000.0, 200.0),
          [_action("VOLCO", "Face Value Split - From Rs 10/- To Rs 2/-", "split", 5.0)])

    series = _adjusted(store, "VOLCO")
    pre = series[EX - dt.timedelta(days=1)]
    post = series[EX + dt.timedelta(days=1)]
    assert pre[1] == 5000 and post[1] == 1000           # volume multiplied by 5
    # Turnover is price x quantity, so a split cannot change it. If adjustment
    # touched it, the liquidity filter would read 5x on pre-split history.
    assert pre[2] == 1_000_000 and post[2] == 200_000


def test_a_demerger_is_quarantined_rather_than_adjusted(store):
    # Quess Corp -> BLUSPRING / DIGITIDE, 2025-06-11. Value splits across two
    # entities with no single derivable ratio, so there is nothing correct to do
    # except refuse to trade it.
    _load(store, _price_series("DEMCO", 160.45, 89.0),
          [_action("DEMCO", "Scheme Of Arrangement - Demerger", "demerger", None)])

    quarantined = store.con.execute(
        "SELECT symbol, reason FROM quarantine").fetchall()
    assert quarantined and quarantined[0][0] == "DEMCO"
    assert "demerger" in quarantined[0][1]


def test_symbols_without_actions_are_untouched(store):
    _load(store, _price_series("PLAINCO", 500.0, 500.0), [])
    closes = {c for c, _, _ in _adjusted(store, "PLAINCO").values()}
    assert closes == {500.0}
    assert store.con.execute("SELECT count(*) FROM adj_intervals").fetchone()[0] == 0


def test_two_actions_packed_into_one_subject_are_multiplied():
    # NSE routinely writes both actions into a single subject separated by a
    # slash. Classifying on the first keyword found parses one component and
    # leaves the entire prior series wrong by the factor of the other.
    # Real examples: TECHM 2015-03-19 (observed gap 4.03), BAJFINANCE
    # 2016-09-08 (9.87), RAMASTEEL 2016-03-14 (9.69).
    from nsemom.ingest.corpactions import classify, parse_factor

    combined = [
        ("Bonus 1:1 / Face Value Split - From Rs 10/- Per Share To Rs 5/- Per Share", 4.0),
        ("Bonus 4:1/Face Value Split (Sub-Division) - From Rs 10/- To Rs 5/-", 10.0),
        ("Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- To Rs 2/-", 10.0),
        ("Bonus 1:2/Face Value Split (Sub-Division) From Rs 10/- To Rs 5/-", 3.0),
    ]
    for subject, expected in combined:
        action_type = classify(subject)
        assert action_type == "bonus+split", f"{subject!r} -> {action_type}"
        factor, note = parse_factor(subject, action_type)
        assert note is None
        assert factor == pytest.approx(expected), subject


def test_a_partially_parseable_combined_subject_is_refused():
    from nsemom.ingest.corpactions import parse_factor
    # bonus keyword present but no ratio: better to refuse than to apply the
    # split half alone and silently mis-scale everything before the ex-date
    factor, note = parse_factor("Bonus issue / Face Value Split From Rs 10 To Rs 5",
                                "bonus+split")
    assert factor is None and "bonus" in note
