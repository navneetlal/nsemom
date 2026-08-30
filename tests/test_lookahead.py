"""Look-ahead prevention.

A backtest that peeks at the future fails in the flattering direction, so these
tests try to make it peek and assert that it cannot. The central one poisons
every bar after a decision point and requires the decision to come out identical.
"""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from nsemom.backtest import ExitRules, Panel, evaluate_exits
from nsemom.costs import CostConfig

FREE = CostConfig(
    enabled=False, brokerage_pct=0, brokerage_flat=0, stt_buy_pct=0,
    stt_sell_pct=0, exchange_txn_pct=0, sebi_turnover_pct=0,
    stamp_duty_buy_pct=0, gst_pct=0, dp_charge_per_scrip=0,
    slippage_tiers=((0.0, 0.0),),
)
RULES = ExitRules(initial_stop_atr=2.5, trailing_stop_atr=3.0,
                  trailing_arms_at_r=1.0, structural_exit_days=2,
                  max_hold_days=90, min_hold_days=0)


def make_panel(series: dict[str, dict], atr=4.0, ema_gap=-50.0) -> Panel:
    """Build a Panel from {symbol: {"close": [...], "open": [...]}}."""
    opens, highs, lows, closes, emas, atrs, turns, lasts, dates = ([] for _ in range(9))
    cursor = 0
    for bars in series.values():
        n = len(bars["close"])
        close = np.asarray(bars["close"], dtype=float)
        open_ = np.asarray(bars.get("open", close), dtype=float)
        closes.append(close)
        opens.append(open_)
        highs.append(np.asarray(bars.get("high", close + 1), dtype=float))
        lows.append(np.asarray(bars.get("low", close - 1), dtype=float))
        # ema_mid far below price unless a test overrides it, so the structural
        # rule stays dormant by default
        emas.append(np.asarray(bars.get("ema_mid", close + ema_gap), dtype=float))
        atrs.append(np.full(n, atr))
        turns.append(np.full(n, 1e9))
        lasts.append(np.full(n, cursor + n - 1, dtype=np.int64))
        dates.append(np.array([dt.date(2020, 1, 1) + dt.timedelta(days=i)
                               for i in range(cursor, cursor + n)]))
        cursor += n
    return Panel(
        open=np.concatenate(opens), high=np.concatenate(highs),
        low=np.concatenate(lows), close=np.concatenate(closes),
        ema_mid=np.concatenate(emas), atr=np.concatenate(atrs),
        median_turnover=np.concatenate(turns),
        last_index_of_symbol=np.concatenate(lasts), dates=np.concatenate(dates),
    )


def test_entry_is_always_the_bar_after_the_signal():
    panel = make_panel({"A": {"close": list(np.linspace(100, 200, 300))}})
    signals = np.array([10, 50, 120], dtype=np.int64)
    out = evaluate_exits(panel, signals, RULES, FREE)
    assert (out["entry_idx"] == out["signal_idx"] + 1).all()


def test_entry_price_is_the_next_open_not_the_signal_close():
    closes = [100.0] * 50
    opens = [100.0] * 50
    opens[11] = 137.0                       # the bar we should actually fill on
    panel = make_panel({"A": {"close": closes, "open": opens}})
    out = evaluate_exits(panel, np.array([10], dtype=np.int64), RULES, FREE)
    assert out["entry_price"].iloc[0] == pytest.approx(137.0)


def test_poisoning_every_bar_after_the_exit_changes_nothing():
    # The strongest form of the check: rewrite the future beyond the trade and
    # require the trade to be bit-identical.
    base = list(np.linspace(100, 160, 200))
    clean = make_panel({"A": {"close": base}})
    before = evaluate_exits(clean, np.array([10], dtype=np.int64), RULES, FREE)

    exit_idx = int(before["exit_idx"].iloc[0])
    poisoned_closes = list(base)
    for i in range(exit_idx + 1, len(poisoned_closes)):
        poisoned_closes[i] = 1e6            # absurd, and entirely in the future
    poisoned = make_panel({"A": {"close": poisoned_closes}})
    after = evaluate_exits(poisoned, np.array([10], dtype=np.int64), RULES, FREE)

    for column in ("entry_price", "exit_price", "gross_return", "hold_days",
                   "exit_reason", "exit_idx"):
        assert before[column].iloc[0] == after[column].iloc[0], column


def test_a_crash_after_the_exit_cannot_retroactively_stop_the_trade():
    rising = list(np.linspace(100, 300, 150))
    panel_a = make_panel({"A": {"close": rising}})
    normal = evaluate_exits(panel_a, np.array([5], dtype=np.int64), RULES, FREE)

    crashed = list(rising)
    exit_idx = int(normal["exit_idx"].iloc[0])
    for i in range(exit_idx + 1, len(crashed)):
        crashed[i] = 1.0
    panel_b = make_panel({"A": {"close": crashed}})
    after = evaluate_exits(panel_b, np.array([5], dtype=np.int64), RULES, FREE)

    assert normal["gross_return"].iloc[0] == pytest.approx(after["gross_return"].iloc[0])


def test_a_stop_gap_fills_at_the_open_not_at_the_stop_level():
    # Stop sits at 100 - 2.5*4 = 90. The bar gaps to 80 and never trades at 90,
    # so filling at 90 would be a fiction that flatters every stopped trade.
    closes = [100.0] * 40
    opens = [100.0] * 40
    lows = [99.0] * 40
    opens[12], lows[12], closes[12] = 80.0, 75.0, 78.0
    panel = make_panel({"A": {"close": closes, "open": opens, "low": lows}})

    out = evaluate_exits(panel, np.array([10], dtype=np.int64), RULES, FREE)
    assert out["exit_reason"].iloc[0] == "stop"
    assert out["exit_price"].iloc[0] == pytest.approx(80.0)


def test_a_clean_stop_breach_fills_at_the_stop():
    closes = [100.0] * 40
    opens = [100.0] * 40
    lows = [99.0] * 40
    lows[12], closes[12] = 88.0, 92.0       # trades through 90 intraday
    panel = make_panel({"A": {"close": closes, "open": opens, "low": lows}})

    out = evaluate_exits(panel, np.array([10], dtype=np.int64), RULES, FREE)
    assert out["exit_reason"].iloc[0] == "stop"
    assert out["exit_price"].iloc[0] == pytest.approx(90.0)


def test_structural_exit_is_a_close_signal_so_it_fills_next_open():
    closes = [100.0] * 40
    opens = [100.0] * 40
    emas = [50.0] * 40
    emas[14] = emas[15] = 150.0             # two consecutive closes below the EMA
    opens[16] = 123.0                       # the bar the exit must fill on
    panel = make_panel({"A": {"close": closes, "open": opens, "ema_mid": emas}})

    out = evaluate_exits(panel, np.array([10], dtype=np.int64), RULES, FREE)
    assert out["exit_reason"].iloc[0] == "structural"
    assert out["exit_idx"].iloc[0] == 16
    assert out["exit_price"].iloc[0] == pytest.approx(123.0)


def test_one_close_below_the_ema_is_not_enough():
    closes = [100.0] * 40
    emas = [50.0] * 40
    emas[14] = 150.0                        # a single breach, not two
    panel = make_panel({"A": {"close": closes, "ema_mid": emas}})
    out = evaluate_exits(panel, np.array([10], dtype=np.int64), RULES, FREE)
    assert out["exit_reason"].iloc[0] != "structural"


def test_time_stop_fires_at_max_hold():
    panel = make_panel({"A": {"close": [100.0] * 300}})
    rules = ExitRules(2.5, 3.0, 1.0, 2, max_hold_days=30, min_hold_days=0)
    out = evaluate_exits(panel, np.array([10], dtype=np.int64), rules, FREE)
    assert out["exit_reason"].iloc[0] == "time"
    assert out["hold_days"].iloc[0] == 30


def test_a_forward_window_never_reads_the_next_symbol():
    # Symbol A ends at index 29. A signal at 25 must not see symbol B's bars,
    # which are a different company at a wildly different price.
    panel = make_panel({
        "A": {"close": [100.0] * 30},
        "B": {"close": [9999.0] * 30},
    })
    out = evaluate_exits(panel, np.array([25], dtype=np.int64), RULES, FREE)

    assert out["exit_idx"].iloc[0] <= 29, "window crossed into the next symbol"
    assert out["exit_price"].iloc[0] == pytest.approx(100.0)
    assert out["exit_reason"].iloc[0] == "data_end"


def test_a_signal_on_the_final_bar_produces_no_trade():
    panel = make_panel({"A": {"close": [100.0] * 30}})
    out = evaluate_exits(panel, np.array([29], dtype=np.int64), RULES, FREE)
    assert out.empty, "there is no next open to enter on"


def test_trailing_stop_uses_only_bars_before_the_one_it_is_tested_on():
    # Price runs up then collapses within a single bar. The trailing stop for
    # that bar must be set from the PREVIOUS high-water mark; using the same
    # bar's close would let the exit price depend on the bar it reacts to.
    closes = [100.0] * 10 + [140.0] * 5 + [100.0] * 25
    lows = [99.0] * 10 + [139.0] * 5 + [99.0] * 25
    panel = make_panel({"A": {"close": closes, "low": lows}})
    out = evaluate_exits(panel, np.array([2], dtype=np.int64), RULES, FREE)

    # high-water 140, trail 3*ATR = 12 -> stop at 128, breached by the drop to 99
    assert out["exit_reason"].iloc[0] == "stop"
    assert out["exit_price"].iloc[0] == pytest.approx(100.0)
