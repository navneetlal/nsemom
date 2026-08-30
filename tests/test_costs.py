"""Cost model tests. Two things are easy to get wrong and both bias results."""

import tomllib
from pathlib import Path

import numpy as np
import pytest

from nsemom.costs import (CostConfig, apply_slippage, buy_costs, sell_costs,
                          slippage_bps)

RAW = tomllib.load(open(Path(__file__).parents[1] / "config" / "config.toml", "rb"))


@pytest.fixture
def cfg():
    return CostConfig.from_raw(RAW["costs"])


def test_slippage_tiers_widen_as_liquidity_falls(cfg):
    # Regression: sorting the tiers the wrong way silently collapsed every name
    # onto the widest tier, applying 80bps to the most liquid stocks on NSE.
    turnover = np.array([1e9, 2e8, 5e7, 1e7, 4e6])
    bps = slippage_bps(cfg, turnover)
    assert list(bps) == [10.0, 20.0, 40.0, 80.0, 80.0]
    assert (np.diff(bps) >= 0).all(), "slippage must not improve as liquidity falls"


def test_missing_liquidity_is_treated_as_illiquid(cfg):
    assert slippage_bps(cfg, np.array([np.nan]))[0] == 80.0


def test_gst_applies_to_fees_but_not_to_stt(cfg):
    value = np.array([100_000.0])
    charged = float(buy_costs(cfg, value)[0])

    exchange = 100_000 * cfg.exchange_txn_pct
    sebi = 100_000 * cfg.sebi_turnover_pct
    expected = (exchange + sebi) * (1 + cfg.gst_pct) \
        + 100_000 * cfg.stt_buy_pct + 100_000 * cfg.stamp_duty_buy_pct
    assert charged == pytest.approx(expected)

    # Taxing STT as well would overstate every buy by ~18% of the STT line.
    over_taxed = (exchange + sebi + 100_000 * cfg.stt_buy_pct) * (1 + cfg.gst_pct) \
        + 100_000 * cfg.stamp_duty_buy_pct
    assert charged < over_taxed


def test_stamp_duty_is_charged_on_the_buy_leg_only(cfg):
    value = np.array([100_000.0])
    assert float(buy_costs(cfg, value)[0]) - float(sell_costs(cfg, value, 0.0)[0]) \
        == pytest.approx(100_000 * cfg.stamp_duty_buy_pct)


def test_dp_charge_is_flat_and_dominates_small_positions(cfg):
    small = float(sell_costs(cfg, np.array([1_667.0]), 1.0)[0]) / 1_667.0
    large = float(sell_costs(cfg, np.array([500_000.0]), 1.0)[0]) / 500_000.0
    assert small > 0.009, "a ~1% DP drag on tiny positions must show up"
    assert large < 0.0015
    assert small > 6 * large


def test_dp_units_let_one_scrip_day_be_billed_once(cfg):
    value = np.array([50_000.0])
    billed = float(sell_costs(cfg, value, 1.0)[0])
    unbilled = float(sell_costs(cfg, value, 0.0)[0])
    assert billed - unbilled == pytest.approx(cfg.dp_charge_per_scrip)


def test_slippage_moves_against_you_on_both_legs(cfg):
    price = np.array([100.0])
    assert apply_slippage(cfg, price, np.array([50.0]), "buy")[0] == pytest.approx(100.5)
    assert apply_slippage(cfg, price, np.array([50.0]), "sell")[0] == pytest.approx(99.5)


def test_disabling_costs_zeroes_everything(cfg):
    off = CostConfig(**{**cfg.__dict__, "enabled": False})
    value = np.array([100_000.0])
    assert float(buy_costs(off, value)[0]) == 0.0
    assert float(sell_costs(off, value, 1.0)[0]) == 0.0
    assert apply_slippage(off, np.array([100.0]), np.array([50.0]), "buy")[0] == 100.0


def test_round_trip_cost_falls_as_position_size_rises(cfg):
    def round_trip(notional):
        v = np.array([notional])
        return (float(buy_costs(cfg, v)[0]) + float(sell_costs(cfg, v, 1.0)[0])) / notional

    assert round_trip(1_667) > 0.01      # the 600-slot case: over 1%
    assert round_trip(67_000) < 0.003    # the 15-slot case: a rounding error
    assert round_trip(1_667) > round_trip(16_667) > round_trip(67_000)
