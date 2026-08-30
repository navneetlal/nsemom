"""The screen: deterministic filters over the indicator table.

Two properties matter more than the filters themselves.

Point-in-time universe: the screen runs against every symbol that actually
traded on the date being screened, with the liquidity floor evaluated from that
date's trailing window. Nothing is filtered by a present-day index membership
list, so names later delisted or dropped from an index are still screened on the
dates they were tradeable - which is what keeps survivorship bias out.

No look-ahead: every column referenced here is computed from data at or before
the bar. Entry happens at the next bar's open, which the backtest enforces.
"""

from __future__ import annotations

from dataclasses import dataclass

from .store.db import Store


@dataclass(frozen=True)
class Preset:
    name: str
    require_ema_stack: bool
    require_close_above: bool
    rsi_min: float
    rsi_max: float
    adx_min: float
    min_close: float
    liquidity_mode: str
    min_turnover: float
    volume_mode: str
    volume_metric: str
    volume_min_ratio: float
    rank_by: str
    basket_size: int

    @classmethod
    def from_raw(cls, name: str, raw: dict) -> "Preset":
        return cls(name=name, **{k: raw[k] for k in (
            "require_ema_stack", "require_close_above", "rsi_min", "rsi_max",
            "adx_min", "min_close", "liquidity_mode", "min_turnover",
            "volume_mode", "volume_metric", "volume_min_ratio", "rank_by",
            "basket_size")})

    @property
    def volume_column(self) -> str:
        return "vol_sustained" if self.volume_metric == "sustained" else "vol_ratio"


def conditions(preset: Preset, min_warmup_bars: int) -> list[str]:
    """The screen as a list of SQL predicates, one per rule."""
    clauses = [
        f"bar_index >= {min_warmup_bars}",
        f"rsi BETWEEN {preset.rsi_min} AND {preset.rsi_max}",
    ]
    if preset.require_ema_stack:
        clauses.append("ema_20 > ema_50 AND ema_50 > ema_100 AND ema_100 > ema_200")
    if preset.require_close_above:
        clauses.append("close > ema_20 AND close > ema_50 AND close > ema_200")
    if preset.adx_min > 0:
        clauses.append(f"adx > {preset.adx_min}")
    if preset.min_close > 0:
        clauses.append(f"close > {preset.min_close}")

    if preset.liquidity_mode == "median":
        clauses.append(f"median_turnover >= {preset.min_turnover}")
    else:
        # Chartink's same-day filter. Measured on real data, 44% of bars passing
        # this have a 20-day median turnover below the same threshold, because
        # the volume spike the screen selects on also inflates today's turnover.
        clauses.append(f"close * volume >= {preset.min_turnover}")

    if preset.volume_mode == "gate":
        clauses.append(f"{preset.volume_column} >= {preset.volume_min_ratio}")

    clauses.append(f"{preset.rank_by} IS NOT NULL")
    return clauses


def build_query(preset: Preset, min_warmup_bars: int,
                date_filter: str = "") -> str:
    where = " AND ".join(f"({c})" for c in conditions(preset, min_warmup_bars))
    date_clause = f"AND i.trade_date {date_filter}" if date_filter else ""
    return f"""
        SELECT i.*,
               row_number() OVER (PARTITION BY i.trade_date
                                  ORDER BY i.{preset.rank_by} DESC) AS rank
          FROM indicators i
          LEFT JOIN quarantine q
                 ON q.symbol = i.symbol
                AND i.trade_date BETWEEN q.from_date AND q.to_date
         WHERE q.symbol IS NULL {date_clause}
           AND {where}
    """


def run(store: Store, preset: Preset, min_warmup_bars: int,
        date_filter: str = "", top_n: int | None = None):
    """Return ranked candidates as a DataFrame."""
    query = build_query(preset, min_warmup_bars, date_filter)
    if top_n is not None:
        query = f"SELECT * FROM ({query}) WHERE rank <= {top_n}"
    return store.con.execute(query + " ORDER BY trade_date, rank").df()
