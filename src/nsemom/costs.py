"""Indian delivery-equity transaction costs, vectorised over trades.

Everything here is arithmetic over configured rates. Two details are easy to get
wrong and both are modelled explicitly:

* GST applies to brokerage, exchange charges and the SEBI fee - NOT to STT or
  stamp duty. Taxing STT overstates costs; omitting GST understates them.
* DP charges are flat per scrip per settlement day on the sell side. They do not
  scale with position size, so they dominate small positions and are reported as
  their own line rather than buried in a percentage.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CostConfig:
    enabled: bool
    brokerage_pct: float
    brokerage_flat: float
    stt_buy_pct: float
    stt_sell_pct: float
    exchange_txn_pct: float
    sebi_turnover_pct: float
    stamp_duty_buy_pct: float
    gst_pct: float
    dp_charge_per_scrip: float
    slippage_tiers: tuple[tuple[float, float], ...]

    @classmethod
    def from_raw(cls, raw: dict) -> "CostConfig":
        # Ascending, so that when slippage_bps walks them in order each higher
        # threshold overwrites the looser tier below it and the most liquid tier
        # wins. Descending order silently collapses every name to the widest.
        tiers = tuple(
            (float(threshold), float(bps))
            for threshold, bps in sorted(raw["slippage_tiers"])
        )
        return cls(
            enabled=bool(raw["enabled"]),
            brokerage_pct=float(raw["brokerage_pct"]),
            brokerage_flat=float(raw["brokerage_flat"]),
            stt_buy_pct=float(raw["stt_buy_pct"]),
            stt_sell_pct=float(raw["stt_sell_pct"]),
            exchange_txn_pct=float(raw["exchange_txn_pct"]),
            sebi_turnover_pct=float(raw["sebi_turnover_pct"]),
            stamp_duty_buy_pct=float(raw["stamp_duty_buy_pct"]),
            gst_pct=float(raw["gst_pct"]),
            dp_charge_per_scrip=float(raw["dp_charge_per_scrip"]),
            slippage_tiers=tiers,
        )


def slippage_bps(cfg: CostConfig, median_turnover: np.ndarray) -> np.ndarray:
    """One-way slippage in basis points, by liquidity tier.

    A single 'realistic for midcaps' figure is too optimistic here: a volume
    expansion screen surfaces names whose 20-day median turnover is far below
    what their spike day suggests.
    """
    turnover = np.nan_to_num(np.asarray(median_turnover, dtype=float), nan=0.0)
    out = np.full(turnover.shape, cfg.slippage_tiers[0][1], dtype=float)
    for threshold, bps in cfg.slippage_tiers:
        out = np.where(turnover >= threshold, bps, out)
    return out


def _brokerage(cfg: CostConfig, value: np.ndarray) -> np.ndarray:
    return np.minimum(value * cfg.brokerage_pct, cfg.brokerage_flat) \
        if cfg.brokerage_flat > 0 else value * cfg.brokerage_pct


def buy_costs(cfg: CostConfig, value: np.ndarray) -> np.ndarray:
    """Charges on the buy leg, excluding slippage."""
    if not cfg.enabled:
        return np.zeros_like(np.asarray(value, dtype=float))
    value = np.asarray(value, dtype=float)
    brokerage = _brokerage(cfg, value)
    exchange = value * cfg.exchange_txn_pct
    sebi = value * cfg.sebi_turnover_pct
    gst = (brokerage + exchange + sebi) * cfg.gst_pct
    return (brokerage + exchange + sebi + gst
            + value * cfg.stt_buy_pct
            + value * cfg.stamp_duty_buy_pct)


def sell_costs(cfg: CostConfig, value: np.ndarray,
               dp_units: np.ndarray | float = 1.0) -> np.ndarray:
    """Charges on the sell leg, excluding slippage.

    `dp_units` is the number of scrip-days billed - pass 0 for a leg that shares
    a settlement day with another exit of the same scrip, since the depository
    bills once per scrip per day, not once per tranche.
    """
    if not cfg.enabled:
        return np.zeros_like(np.asarray(value, dtype=float))
    value = np.asarray(value, dtype=float)
    brokerage = _brokerage(cfg, value)
    exchange = value * cfg.exchange_txn_pct
    sebi = value * cfg.sebi_turnover_pct
    gst = (brokerage + exchange + sebi) * cfg.gst_pct
    return (brokerage + exchange + sebi + gst
            + value * cfg.stt_sell_pct
            + np.asarray(dp_units, dtype=float) * cfg.dp_charge_per_scrip)


def apply_slippage(cfg: CostConfig, price: np.ndarray, bps: np.ndarray,
                   side: str) -> np.ndarray:
    """Buys fill above the modelled price, sells below it."""
    if not cfg.enabled:
        return np.asarray(price, dtype=float)
    direction = 1.0 if side == "buy" else -1.0
    return np.asarray(price, dtype=float) * (1.0 + direction * np.asarray(bps) / 10_000.0)
