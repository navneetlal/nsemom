"""Portfolio simulation and metrics."""

import datetime as dt
import tomllib
from pathlib import Path

import pandas as pd
import pytest

from nsemom.backtest import compute_stats, simulate_slots
from nsemom.costs import CostConfig

RAW = tomllib.load(open(Path(__file__).parents[1] / "config" / "config.toml", "rb"))
COSTS = CostConfig.from_raw(RAW["costs"])
FREE = CostConfig(**{**COSTS.__dict__, "enabled": False})
D = dt.date(2024, 1, 1)


def candidates(rows):
    """rows: (symbol, entry_offset, exit_offset, entry_price, exit_price, rank)"""
    return pd.DataFrame([
        {"symbol": s, "entry_date": D + dt.timedelta(days=eo),
         "exit_date": D + dt.timedelta(days=xo), "entry_price": ep,
         "exit_price": xp, "rank": rk, "gross_return": xp / ep - 1,
         "exit_reason": "time", "hold_days": xo - eo}
        for s, eo, xo, ep, xp, rk in rows
    ])


def test_never_holds_more_than_the_slot_count():
    frame = candidates([(f"S{i}", 0, 60, 100.0, 110.0, i + 1) for i in range(20)])
    trades, curve = simulate_slots(frame, slots=5, capital=1_000_000, costs=FREE)

    assert len(trades) == 5, "more positions opened than there are slots"
    assert curve["open_positions"].max() == 5


def test_slots_are_filled_in_rank_order():
    frame = candidates([("WORST", 0, 60, 100.0, 110.0, 3),
                        ("BEST", 0, 60, 100.0, 110.0, 1),
                        ("MID", 0, 60, 100.0, 110.0, 2)])
    trades, _ = simulate_slots(frame, slots=2, capital=1_000_000, costs=FREE)
    assert set(trades["symbol"]) == {"BEST", "MID"}


def test_a_freed_slot_is_reused_by_a_later_candidate():
    frame = candidates([("EARLY", 0, 10, 100.0, 110.0, 1),
                        ("LATE", 20, 40, 100.0, 120.0, 1)])
    trades, _ = simulate_slots(frame, slots=1, capital=1_000_000, costs=FREE)
    assert set(trades["symbol"]) == {"EARLY", "LATE"}


def test_a_candidate_arriving_with_no_free_slot_is_dropped():
    frame = candidates([("HOLDER", 0, 90, 100.0, 110.0, 1),
                        ("BLOCKED", 5, 40, 100.0, 200.0, 1)])
    trades, _ = simulate_slots(frame, slots=1, capital=1_000_000, costs=FREE)
    assert set(trades["symbol"]) == {"HOLDER"}


def test_the_same_symbol_is_never_held_twice_at_once():
    frame = candidates([("DUP", 0, 60, 100.0, 110.0, 1),
                        ("DUP", 5, 65, 100.0, 110.0, 1),
                        ("OTHER", 5, 65, 100.0, 110.0, 2)])
    trades, _ = simulate_slots(frame, slots=5, capital=1_000_000, costs=FREE)
    overlapping = trades[trades["symbol"] == "DUP"]
    assert len(overlapping) == 1


def test_costs_always_reduce_the_result():
    frame = candidates([(f"S{i}", i, i + 30, 100.0, 110.0, 1) for i in range(20)])
    gross, gross_curve = simulate_slots(frame, 5, 1_000_000, FREE)
    net, net_curve = simulate_slots(frame, 5, 1_000_000, COSTS)

    gross_stats = compute_stats(gross, gross_curve, 1_000_000)
    net_stats = compute_stats(net, net_curve, 1_000_000)
    assert net_stats["total_return_net"] < gross_stats["total_return_net"]
    assert net_stats["total_costs"] > 0


def test_equity_compounds_across_sequential_winners():
    frame = candidates([("A", 0, 10, 100.0, 110.0, 1),
                        ("B", 10, 20, 100.0, 110.0, 1),
                        ("C", 20, 30, 100.0, 110.0, 1)])
    trades, curve = simulate_slots(frame, slots=1, capital=1_000_000, costs=FREE)

    # one slot, so each trade deploys the whole book: 1.1^3
    assert curve["equity"].iloc[-1] == pytest.approx(1_000_000 * 1.1 ** 3, rel=1e-9)


def test_stats_report_hit_rate_and_win_loss_correctly():
    frame = candidates([("W1", 0, 10, 100.0, 120.0, 1),
                        ("W2", 10, 20, 100.0, 120.0, 1),
                        ("L1", 20, 30, 100.0, 90.0, 1),
                        ("L2", 30, 40, 100.0, 90.0, 1)])
    trades, curve = simulate_slots(frame, slots=1, capital=1_000_000, costs=FREE)
    stats = compute_stats(trades, curve, 1_000_000)

    assert stats["trades"] == 4
    assert stats["hit_rate"] == pytest.approx(0.5)
    assert stats["avg_win"] == pytest.approx(0.20)
    assert stats["avg_loss"] == pytest.approx(-0.10)
    assert stats["win_loss_ratio"] == pytest.approx(2.0)


def test_max_drawdown_is_measured_from_the_peak():
    frame = candidates([("UP", 0, 10, 100.0, 150.0, 1),
                        ("DOWN", 10, 20, 100.0, 75.0, 1)])
    trades, curve = simulate_slots(frame, slots=1, capital=1_000_000, costs=FREE)
    stats = compute_stats(trades, curve, 1_000_000)
    assert stats["max_drawdown"] == pytest.approx(-0.25, abs=1e-9)


def test_an_empty_candidate_set_is_not_an_error():
    # what a filtered-to-nothing screen actually produces: columns, no rows
    empty = candidates([("X", 0, 1, 1.0, 1.0, 1)]).iloc[0:0]
    trades, curve = simulate_slots(empty, slots=5, capital=1_000_000, costs=FREE)
    assert trades.empty
    assert compute_stats(trades, curve, 1_000_000) == {"trades": 0}


def test_a_position_exiting_on_its_entry_bar_frees_its_slot():
    # A trade stopped out intraday on day one exits on the date it opened. If
    # the ledger only pops exits before processing entries, that position is
    # never closed and holds its slot forever - and once as many of those
    # accumulate as there are slots, the portfolio stops trading entirely.
    frame = candidates([("SAMEDAY", 0, 0, 100.0, 90.0, 1),
                        ("LATER", 1, 30, 100.0, 110.0, 1)])
    trades, curve = simulate_slots(frame, slots=1, capital=1_000_000, costs=FREE)

    assert set(trades["symbol"]) == {"SAMEDAY", "LATER"}, \
        "the same-day exit did not release its slot"
    assert curve["open_positions"].iloc[0] == 0


def test_many_same_day_exits_do_not_freeze_the_book():
    rows = [(f"DEAD{i}", i, i, 100.0, 90.0, 1) for i in range(5)]
    rows += [(f"LIVE{i}", 10 + i, 40 + i, 100.0, 120.0, 1) for i in range(5)]
    trades, _ = simulate_slots(candidates(rows), slots=3, capital=1_000_000, costs=FREE)

    # all five same-day losers must close rather than accumulate
    assert sum(s.startswith("DEAD") for s in trades["symbol"]) == 5
    # and the book must still be trading afterwards: three slots, overlapping
    # holds, so three of the five later names get in. Before the fix: zero.
    assert sum(s.startswith("LIVE") for s in trades["symbol"]) == 3
