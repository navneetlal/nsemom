"""Vectorised backtest with an explicit exit plan.

Look-ahead prevention is structural, not a convention:

  * the signal is read from bar T
  * the entry fills at the open of bar T+1
  * every exit rule is parameterised by ATR as at bar T, never later
  * a stop fills at min(stop level, that bar's open), so a gap through the stop
    fills at the gap, not at the stop
  * a structural exit is a close-based signal, so it fills at the NEXT open

Each candidate's outcome is computed over a forward window as one array
operation across all candidates at once - no Python loop over dates. Slot
allocation is a light sequential pass, because whether a slot is free is
genuinely path-dependent; the expensive per-bar work is already vectorised.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .costs import CostConfig, apply_slippage, buy_costs, sell_costs, slippage_bps
from .screen import Preset
from .store.db import Store

log = logging.getLogger(__name__)

NEVER = 1 << 30


@dataclass(frozen=True)
class ExitRules:
    initial_stop_atr: float
    trailing_stop_atr: float
    trailing_arms_at_r: float
    structural_exit_days: int
    max_hold_days: int
    min_hold_days: int


@dataclass
class Panel:
    """Every bar, flattened and sorted by (symbol, date), plus each bar's
    symbol boundaries so a forward window can never run into another symbol."""

    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    ema_mid: np.ndarray
    atr: np.ndarray
    median_turnover: np.ndarray
    last_index_of_symbol: np.ndarray
    dates: np.ndarray


def load_panel(store: Store, ema_mid_period: int) -> Panel:
    store.con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _panel AS
        -- gidx is assigned first and the per-symbol boundary derived from it in
        -- a second pass: DuckDB cannot nest one window function inside another.
        WITH numbered AS (
            SELECT row_number() OVER (ORDER BY symbol, trade_date) - 1 AS gidx,
                   symbol, trade_date, open, high, low, close,
                   ema_{ema_mid_period} AS ema_mid, atr, median_turnover
              FROM indicators
        )
        SELECT *, max(gidx) OVER (PARTITION BY symbol) AS last_gidx
          FROM numbered
        """
    )
    frame = store.con.execute("SELECT * FROM _panel ORDER BY gidx").df()
    return Panel(
        open=frame["open"].to_numpy(float),
        high=frame["high"].to_numpy(float),
        low=frame["low"].to_numpy(float),
        close=frame["close"].to_numpy(float),
        ema_mid=frame["ema_mid"].to_numpy(float),
        atr=frame["atr"].to_numpy(float),
        median_turnover=frame["median_turnover"].to_numpy(float),
        last_index_of_symbol=frame["last_gidx"].to_numpy(np.int64),
        dates=frame["trade_date"].to_numpy(),
    )


def _first_true(mask: np.ndarray) -> np.ndarray:
    """Index of the first True per row, or NEVER where there is none."""
    return np.where(mask.any(axis=1), mask.argmax(axis=1), NEVER)


def _shift_right(mask: np.ndarray, by: int) -> np.ndarray:
    out = np.zeros_like(mask)
    if by < mask.shape[1]:
        out[:, by:] = mask[:, : mask.shape[1] - by]
    return out


def evaluate_exits(panel: Panel, signal_idx: np.ndarray, rules: ExitRules,
                   costs: CostConfig) -> pd.DataFrame:
    """Resolve every candidate's trade outcome in one vectorised pass."""
    horizon = rules.max_hold_days
    entry_idx = signal_idx + 1
    last_idx = panel.last_index_of_symbol[signal_idx]

    tradeable = entry_idx <= last_idx
    signal_idx, entry_idx, last_idx = (signal_idx[tradeable], entry_idx[tradeable],
                                       last_idx[tradeable])

    offsets = np.arange(horizon)
    positions = entry_idx[:, None] + offsets[None, :]
    valid = positions <= last_idx[:, None]
    positions = np.minimum(positions, last_idx[:, None])

    open_m = panel.open[positions]
    low_m = panel.low[positions]
    close_m = panel.close[positions]
    ema_m = panel.ema_mid[positions]

    # ATR as at the SIGNAL bar. Using a later ATR would leak the future into the
    # stop that was supposedly placed on entry.
    atr_signal = panel.atr[signal_idx]
    liquidity = panel.median_turnover[signal_idx]
    bps = slippage_bps(costs, liquidity)

    entry_price = apply_slippage(costs, panel.open[entry_idx], bps, "buy")
    risk = rules.initial_stop_atr * atr_signal
    initial_stop = entry_price - risk

    # Trailing stop follows the highest close so far, but may only use bars
    # strictly BEFORE the one being tested.
    running_max = np.maximum.accumulate(close_m, axis=1)
    prior_max = np.concatenate([entry_price[:, None], running_max[:, :-1]], axis=1)
    trailing_stop = prior_max - rules.trailing_stop_atr * atr_signal[:, None]
    armed = prior_max >= (entry_price + rules.trailing_arms_at_r * risk)[:, None]
    stop_level = np.where(armed, np.maximum(initial_stop[:, None], trailing_stop),
                          initial_stop[:, None])

    stop_hit = (low_m <= stop_level) & valid
    stop_k = _first_true(stop_hit)

    below = (close_m < ema_m) & valid
    persistent = below.copy()
    for lag in range(1, rules.structural_exit_days):
        persistent &= _shift_right(below, lag)
    persistent[:, : rules.min_hold_days] = False   # do not get shaken out early
    structural_k = _first_true(persistent)

    time_k = np.minimum(horizon - 1, last_idx - entry_idx)

    # A stop fills intraday on its own bar; a close-based signal fills next open.
    exec_stop = stop_k
    exec_structural = np.where(structural_k == NEVER, NEVER, structural_k + 1)
    exec_structural = np.where(exec_structural > time_k, NEVER, exec_structural)
    exec_time = time_k

    stacked = np.vstack([exec_stop, exec_structural, exec_time])
    choice = stacked.argmin(axis=0)
    exec_k = stacked.min(axis=0)

    rows = np.arange(len(signal_idx))
    stop_fill = np.minimum(stop_level[rows, np.minimum(stop_k, horizon - 1)],
                           open_m[rows, np.minimum(stop_k, horizon - 1)])
    exit_raw = np.where(
        choice == 0, stop_fill, open_m[rows, np.minimum(exec_k, horizon - 1)]
    )
    exit_price = apply_slippage(costs, exit_raw, bps, "sell")

    reason = np.array(["stop", "structural", "time"])[choice]
    # A window truncated by the symbol disappearing is a delisting, not a
    # time stop, and must be reported separately rather than flattering results.
    truncated = (choice == 2) & ((last_idx - entry_idx) < horizon - 1)
    reason = np.where(truncated, "data_end", reason)

    return pd.DataFrame({
        "signal_idx": signal_idx,
        "entry_idx": entry_idx,
        "exit_idx": entry_idx + exec_k,
        "entry_date": panel.dates[entry_idx],
        "exit_date": panel.dates[entry_idx + exec_k],
        "entry_price": entry_price,
        "exit_price": exit_price,
        "gross_return": exit_price / entry_price - 1.0,
        "hold_days": exec_k + 1,
        "exit_reason": reason,
        "median_turnover": liquidity,
        "slippage_bps": bps,
    })


@dataclass
class Result:
    trades: pd.DataFrame
    equity: pd.DataFrame
    stats: dict[str, float]
    preset: str
    stats_gross: dict[str, float] | None = None

    @property
    def cost_drag_on_return(self) -> float:
        """How much of the gross return costs actually ate."""
        if not self.stats_gross:
            return float("nan")
        return (self.stats_gross.get("total_return_net", float("nan"))
                - self.stats.get("total_return_net", float("nan")))


def simulate_slots(candidates: pd.DataFrame, slots: int, capital: float,
                   costs: CostConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fill a fixed number of slots from the ranked shortlist.

    A slot frees only when its position exits, so on most days the screen offers
    more candidates than there is room for - which is the realistic constraint
    and the reason a hard volume gate costs so little to relax.
    """
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame(columns=["date", "equity", "open_positions"])
    candidates = candidates.sort_values(["entry_date", "rank"]).reset_index(drop=True)
    by_entry = {d: g for d, g in candidates.groupby("entry_date", sort=True)}
    exits_due: dict[object, list[dict]] = {}

    equity = capital
    open_symbols: set[str] = set()
    ledger: list[dict] = []
    curve: list[dict] = []

    def close_due(date) -> None:
        """Realise every position whose exit falls on this date."""
        nonlocal equity
        for position in exits_due.pop(date, []):
            value = position["quantity"] * position["exit_price"]
            charge = float(sell_costs(costs, np.array([value]), 1.0)[0])
            proceeds = value - charge
            profit = proceeds - position["cost_basis"]
            equity += profit
            open_symbols.discard(position["symbol"])
            ledger.append({**position, "exit_value": value, "sell_cost": charge,
                           "net_profit": profit,
                           "net_return": profit / position["cost_basis"]})

    all_dates = sorted(set(candidates["entry_date"]) | set(candidates["exit_date"]))
    for date in all_dates:
        close_due(date)

        free = slots - len(open_symbols)
        if free > 0 and date in by_entry:
            for _, row in by_entry[date].iterrows():
                if free == 0:
                    break
                if row["symbol"] in open_symbols:
                    continue
                notional = equity / slots
                quantity = notional / row["entry_price"]
                if quantity <= 0:
                    continue
                charge = float(buy_costs(costs, np.array([notional]))[0])
                cost_basis = notional + charge
                equity -= charge
                open_symbols.add(row["symbol"])
                exits_due.setdefault(row["exit_date"], []).append({
                    "symbol": row["symbol"], "entry_date": date,
                    "exit_date": row["exit_date"], "entry_price": row["entry_price"],
                    "exit_price": row["exit_price"], "quantity": quantity,
                    "cost_basis": cost_basis, "buy_cost": charge,
                    "gross_return": row["gross_return"],
                    "exit_reason": row["exit_reason"], "hold_days": row["hold_days"],
                })
                free -= 1

        # A position stopped out intraday on its own entry bar exits on the date
        # it opened. Without this second pass it is never popped, holds its slot
        # forever, and ~15 such trades silently freeze the whole portfolio.
        close_due(date)
        curve.append({"date": date, "equity": equity, "open_positions": len(open_symbols)})

    return pd.DataFrame(ledger), pd.DataFrame(curve)


def compute_stats(trades: pd.DataFrame, curve: pd.DataFrame,
                  capital: float) -> dict[str, float]:
    if trades.empty:
        return {"trades": 0}

    curve = curve.sort_values("date")
    final = float(curve["equity"].iloc[-1])
    span_days = (curve["date"].iloc[-1] - curve["date"].iloc[0]).days or 1
    years = span_days / 365.25

    peak = curve["equity"].cummax()
    drawdown = curve["equity"] / peak - 1.0

    net = trades["net_return"]
    wins, losses = net[net > 0], net[net <= 0]
    traded_value = (trades["cost_basis"].sum() + trades["exit_value"].sum())
    total_costs = trades["buy_cost"].sum() + trades["sell_cost"].sum()

    return {
        "trades": int(len(trades)),
        "total_return_net": final / capital - 1.0,
        "cagr_net": (final / capital) ** (1 / years) - 1.0 if years > 0 else float("nan"),
        "mean_gross_return": float(trades["gross_return"].mean()),
        "median_net_return": float(net.median()),
        "mean_net_return": float(net.mean()),
        "max_drawdown": float(drawdown.min()),
        "hit_rate": float((net > 0).mean()),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "win_loss_ratio": float(wins.mean() / abs(losses.mean())) if len(losses) and losses.mean() != 0 else float("nan"),
        "avg_hold_days": float(trades["hold_days"].mean()),
        "turnover_x_per_year": float(traded_value / capital / years) if years > 0 else float("nan"),
        "total_costs": float(total_costs),
        "cost_drag_pct_of_capital": float(total_costs / capital),
        "cost_drag_per_trade": float(total_costs / len(trades)),
        "years": years,
    }


def exit_reason_breakdown(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    return (trades.groupby("exit_reason")
            .agg(trades=("net_return", "size"),
                 mean_net=("net_return", "mean"),
                 hit_rate=("net_return", lambda s: (s > 0).mean()),
                 mean_hold=("hold_days", "mean"))
            .sort_values("trades", ascending=False))


def run(store: Store, preset: Preset, rules: ExitRules, costs: CostConfig,
        slots: int, capital: float, min_warmup_bars: int, ema_mid_period: int,
        start: dt.date | None = None, end: dt.date | None = None) -> Result:
    from .screen import build_query

    panel = load_panel(store, ema_mid_period)

    date_clause = []
    if start:
        date_clause.append(f"r.trade_date >= DATE '{start}'")
    if end:
        date_clause.append(f"r.trade_date <= DATE '{end}'")
    where_dates = (" AND " + " AND ".join(date_clause)) if date_clause else ""

    query = build_query(preset, min_warmup_bars)
    candidates = store.con.execute(
        f"""
        WITH screened AS ({query}),
             ranked AS (SELECT * FROM screened WHERE rank <= {preset.basket_size})
        SELECT p.gidx AS signal_idx, r.symbol, r.trade_date AS signal_date, r.rank
          FROM ranked r
          JOIN _panel p ON p.symbol = r.symbol AND p.trade_date = r.trade_date
         WHERE TRUE {where_dates}
         ORDER BY r.trade_date, r.rank
        """
    ).df()

    if candidates.empty:
        return Result(pd.DataFrame(), pd.DataFrame(), {"trades": 0}, preset.name)

    signal_idx = candidates["signal_idx"].to_numpy(np.int64)

    def simulate(cost_model: CostConfig):
        outcomes = evaluate_exits(panel, signal_idx, rules, cost_model)
        merged = candidates.merge(outcomes, on="signal_idx", how="inner")
        trades, curve = simulate_slots(merged, slots, capital, cost_model)
        return trades, curve, compute_stats(trades, curve, capital)

    trades, curve, stats = simulate(costs)
    # Identical run with every charge and slippage switched off. Slippage moves
    # fills, which moves which stops trigger, so this cannot be approximated by
    # subtracting a fee total afterwards.
    free = CostConfig(**{**costs.__dict__, "enabled": False})
    _, _, gross_stats = simulate(free)
    return Result(trades, curve, stats, preset.name, gross_stats)


def walk_forward(store: Store, preset: Preset, rules: ExitRules, costs: CostConfig,
                 slots: int, capital: float, min_warmup_bars: int,
                 ema_mid_period: int, split: dt.date) -> dict[str, Result]:
    """Thresholds are chosen on the in-sample period; the out-of-sample period
    is measured once and never tuned against."""
    return {
        "in_sample": run(store, preset, rules, costs, slots, capital,
                         min_warmup_bars, ema_mid_period, end=split),
        "out_of_sample": run(store, preset, rules, costs, slots, capital,
                             min_warmup_bars, ema_mid_period, start=split),
    }


def yearly_returns(curve: pd.DataFrame) -> pd.DataFrame:
    """Calendar-year returns off the equity curve.

    One headline CAGR hides everything that matters about whether a strategy is
    holdable, so the year-by-year path is reported alongside it.
    """
    if curve.empty:
        return pd.DataFrame()
    frame = curve.sort_values("date").copy()
    frame["year"] = pd.to_datetime(frame["date"]).dt.year
    rows = []
    for year, group in frame.groupby("year"):
        opening = group["equity"].iloc[0]
        closing = group["equity"].iloc[-1]
        peak = group["equity"].cummax()
        rows.append({
            "year": int(year),
            "return": closing / opening - 1.0,
            "max_drawdown": float((group["equity"] / peak - 1.0).min()),
            "trading_days": len(group),
        })
    return pd.DataFrame(rows)


def rolling_window_returns(curve: pd.DataFrame, window_days: int = 365) -> pd.Series:
    """Return from every possible start date, over a fixed holding window.

    This is the honest version of "what would I have made?" - a single
    start-to-finish number is one draw from this distribution, and picking the
    flattering one is how backtests mislead.
    """
    if curve.empty:
        return pd.Series(dtype=float)
    frame = curve.sort_values("date").copy()
    frame["date"] = pd.to_datetime(frame["date"])
    series = frame.set_index("date")["equity"]

    out = {}
    for start, opening in series.items():
        target = start + pd.Timedelta(days=window_days)
        forward = series[series.index >= target]
        if forward.empty:
            break
        out[start.date()] = forward.iloc[0] / opening - 1.0
    return pd.Series(out, dtype=float)


def summarise_distribution(returns: pd.Series, label: str) -> dict[str, float]:
    if returns.empty:
        return {"label": label, "windows": 0}
    return {
        "label": label,
        "windows": int(len(returns)),
        "worst": float(returns.min()),
        "p10": float(returns.quantile(0.10)),
        "median": float(returns.median()),
        "mean": float(returns.mean()),
        "p90": float(returns.quantile(0.90)),
        "best": float(returns.max()),
        "share_positive": float((returns > 0).mean()),
    }
