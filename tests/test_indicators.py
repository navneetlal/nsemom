"""Indicator correctness.

RSI is verified against an explicit recursive implementation of Wilder's
definition written independently in this file. Comparing pandas `ewm` against a
second pandas call would prove nothing; comparing it against a hand-rolled loop
proves the smoothing is actually Wilder's and not something that merely looks
like it.
"""

import math

import numpy as np
import pandas as pd
import pytest

from nsemom.indicators import adx, atr, rsi, true_range, wilder

PERIOD = 14

# Wilder's own 14-period worked example from "New Concepts in Technical Trading
# Systems" - the closes most reference implementations are checked against.
WILDER_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
    46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57,
    43.42, 42.66, 43.13,
]


def reference_rsi(closes, period=PERIOD):
    """Wilder's RSI, written as an explicit recursive loop.

    Mirrors ewm(alpha=1/period, adjust=False): seeded on the first delta, then
    each step moves the average 1/period of the way toward the new value.
    """
    alpha = 1.0 / period
    out = [math.nan] * len(closes)
    avg_gain = avg_loss = None
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain, loss = max(delta, 0.0), max(-delta, 0.0)
        if avg_gain is None:
            avg_gain, avg_loss = gain, loss
        else:
            avg_gain = (1 - alpha) * avg_gain + alpha * gain
            avg_loss = (1 - alpha) * avg_loss + alpha * loss
        if avg_loss == 0 and avg_gain == 0:
            out[i] = 50.0
        elif avg_loss == 0:
            out[i] = 100.0
        elif avg_gain == 0:
            out[i] = 0.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def reference_textbook_rsi(closes, period=PERIOD):
    """The other common convention: seed with a simple mean of the first
    `period` deltas, then smooth. Used only to show the two converge."""
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [math.nan] * len(closes)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = (100.0 if avg_loss == 0
                      else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return out


def test_rsi_matches_an_independent_recursive_implementation():
    closes = pd.Series(WILDER_CLOSES)
    expected = reference_rsi(WILDER_CLOSES)
    actual = rsi(closes, PERIOD)

    for i in range(1, len(WILDER_CLOSES)):
        assert actual.iloc[i] == pytest.approx(expected[i], abs=1e-9), f"bar {i}"


def test_rsi_is_wilder_smoothing_not_a_rolling_mean():
    # The failure this guards: ewm(alpha=1/14) replaced by rolling(14).mean().
    # Both are "an average of 14 gains", and both produce a plausible-looking
    # series, so nothing downstream would reveal the substitution.
    closes = pd.Series(WILDER_CLOSES)
    delta = closes.diff()
    naive = 100 - 100 / (
        1 + delta.clip(lower=0).rolling(PERIOD).mean()
        / (-delta.clip(upper=0)).rolling(PERIOD).mean()
    )
    wilder_rsi = rsi(closes, PERIOD)

    tail = slice(PERIOD, None)
    difference = (wilder_rsi[tail] - naive[tail]).abs().max()
    assert difference > 1.0, "Wilder and rolling-mean RSI are indistinguishable here"


def test_both_wilder_seedings_converge_on_a_long_series():
    # The ewm seeding differs from the textbook seeding only in its first bars.
    # Over a 200-bar warmup, which every screened symbol has, they agree.
    rng = np.random.default_rng(7)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 400)))
    ewm_rsi = rsi(pd.Series(closes), PERIOD)
    textbook = reference_textbook_rsi(closes, PERIOD)

    late = [(ewm_rsi.iloc[i], textbook[i]) for i in range(300, 400)]
    assert max(abs(a - b) for a, b in late) < 0.01


def test_rsi_saturates_correctly():
    rising = pd.Series([100 + i for i in range(40)])
    falling = pd.Series([100 - i for i in range(40)])
    flat = pd.Series([100.0] * 40)

    assert rsi(rising, PERIOD).iloc[-1] == pytest.approx(100.0)
    assert rsi(falling, PERIOD).iloc[-1] == pytest.approx(0.0)
    assert rsi(flat, PERIOD).iloc[-1] == pytest.approx(50.0)


def test_rsi_stays_within_bounds_on_noisy_input():
    rng = np.random.default_rng(11)
    closes = pd.Series(100 + np.cumsum(rng.normal(0, 2, 500)))
    values = rsi(closes, PERIOD).dropna()
    assert values.between(0, 100).all()


def test_wilder_smoothing_matches_its_definition():
    values = pd.Series([1.0, 2.0, 3.0, 10.0, 4.0])
    smoothed = wilder(values, 4)
    alpha, expected = 0.25, 1.0
    for value in values.iloc[1:]:
        expected = (1 - alpha) * expected + alpha * value
    assert smoothed.iloc[-1] == pytest.approx(expected, abs=1e-12)


def test_true_range_takes_the_widest_of_the_three_measures():
    high = pd.Series([10.0, 12.0, 11.0])
    low = pd.Series([9.0, 11.0, 8.0])
    close = pd.Series([9.5, 11.5, 8.5])
    tr = true_range(high, low, close)

    assert tr.iloc[0] == pytest.approx(1.0)            # no prior close: high-low
    assert tr.iloc[1] == pytest.approx(2.5)            # high - prev close
    assert tr.iloc[2] == pytest.approx(3.5)            # prev close - low


def test_atr_matches_an_independent_loop():
    rng = np.random.default_rng(3)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 120)))
    high, low = close + 1.5, close - 1.5
    actual = atr(high, low, close, PERIOD)

    tr = true_range(high, low, close).tolist()
    alpha, expected = 1.0 / PERIOD, tr[0]
    for value in tr[1:]:
        expected = (1 - alpha) * expected + alpha * value
    assert actual.iloc[-1] == pytest.approx(expected, abs=1e-9)


def test_adx_is_high_in_a_trend_and_low_in_chop():
    trend_close = pd.Series([100 + i * 1.5 for i in range(120)])
    trending = adx(trend_close + 1, trend_close - 1, trend_close, PERIOD)

    chop_close = pd.Series([100 + (2 if i % 2 else -2) for i in range(120)])
    choppy = adx(chop_close + 1, chop_close - 1, chop_close, PERIOD)

    assert trending.iloc[-1] > 40, f"trend ADX only {trending.iloc[-1]:.1f}"
    assert choppy.iloc[-1] < 25, f"chop ADX {choppy.iloc[-1]:.1f} is too high"


def test_adx_is_bounded():
    rng = np.random.default_rng(5)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1.5, 400)))
    values = adx(close + 2, close - 2, close, PERIOD).dropna()
    assert values.between(0, 100).all()
