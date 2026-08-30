"""Indicators, split by what each tool is actually good at.

Recursive indicators (EMA, RSI, ATR, ADX) are computed with pandas `ewm`, which
is Wilder smoothing when alpha = 1/period and adjust=False. Path-dependent
per-symbol state (how long the EMA stack has held, RSI divergence) is computed in
the same pass because it is naturally sequential.

Everything non-recursive - momentum, rolling medians, 52-week highs, volume
baselines - is a DuckDB window function over the whole table at once.

Everything reads from prices_adjusted, never from raw prices, so a corporate
action can never reach an indicator unadjusted.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import Config, IndicatorConfig
from .store.db import Store

log = logging.getLogger(__name__)


def wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing. Deliberately ewm, not a rolling mean - a simple
    rolling average is a different and wrong indicator."""
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    avg_gain = wilder(delta.clip(lower=0.0), period)
    avg_loss = wilder((-delta).clip(lower=0.0), period)

    out = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    # An all-up window has no losses (RSI 100); an all-down window no gains (0);
    # a perfectly flat window is neither, which is 50 by convention.
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0.0)
    out = out.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return out


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    return wilder(true_range(high, low, close), period)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Wilder's ADX: trend strength regardless of direction."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )
    atr_ = wilder(true_range(high, low, close), period)
    plus_di = 100.0 * wilder(plus_dm, period) / atr_
    minus_di = 100.0 * wilder(minus_dm, period) / atr_
    total = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / total.where(total != 0)
    return wilder(dx.fillna(0.0), period)


def _sequential_for_symbol(frame: pd.DataFrame, cfg: IndicatorConfig) -> pd.DataFrame:
    close, high, low = frame["close"], frame["high"], frame["low"]
    # trade_date travels with the computed columns rather than being reattached
    # by position afterwards, so the join key can never drift off its bar.
    out = pd.DataFrame({"trade_date": frame["trade_date"]}, index=frame.index)

    for period in cfg.ema_periods:
        out[f"ema_{period}"] = close.ewm(span=period, adjust=False).mean()

    out["rsi"] = rsi(close, cfg.rsi_period)
    out["atr"] = atr(high, low, close, cfg.atr_period)
    out["adx"] = adx(high, low, close, cfg.adx_period)

    # How many consecutive sessions the EMA stack has held. A young stack is a
    # fresh trend; a very old one is a trend that may be running out of road.
    ordered = sorted(cfg.ema_periods)
    stacked = pd.Series(True, index=frame.index)
    for shorter, longer in zip(ordered, ordered[1:]):
        stacked &= out[f"ema_{shorter}"] > out[f"ema_{longer}"]
    out["trend_age_days"] = stacked.groupby((~stacked).cumsum()).cumsum().astype("int32")

    # Bearish divergence: price prints a new N-day high while RSI does not.
    window = cfg.divergence_window
    price_breakout = close > close.shift(1).rolling(window).max()
    rsi_breakout = out["rsi"] > out["rsi"].shift(1).rolling(window).max()
    out["rsi_divergence"] = (price_breakout & ~rsi_breakout).astype("int8")

    return out


def compute_sequential(store: Store, cfg: Config) -> int:
    """Pass one: per-symbol recursive indicators, in batches to bound memory."""
    ind = cfg.indicators
    symbols = [
        r[0] for r in store.con.execute(
            "SELECT DISTINCT symbol FROM prices ORDER BY symbol"
        ).fetchall()
    ]
    columns = ([f"ema_{p}" for p in ind.ema_periods]
               + ["rsi", "atr", "adx", "trend_age_days", "rsi_divergence"])
    store.con.execute("DROP TABLE IF EXISTS indicators_seq")
    store.con.execute(
        "CREATE TABLE indicators_seq (symbol VARCHAR, trade_date DATE, "
        + ", ".join(
            f"{c} {'INTEGER' if c == 'trend_age_days' else 'TINYINT' if c == 'rsi_divergence' else 'DOUBLE'}"
            for c in columns
        )
        + ")"
    )

    written = 0
    for start in range(0, len(symbols), ind.batch_symbols):
        batch = symbols[start:start + ind.batch_symbols]
        frame = store.con.execute(
            "SELECT symbol, trade_date, high, low, close FROM prices_adjusted "
            "WHERE symbol IN ? ORDER BY symbol, trade_date", [batch]
        ).df()
        if frame.empty:
            continue
        computed = (
            frame.groupby("symbol", group_keys=True)
                 .apply(lambda g: _sequential_for_symbol(g, ind), include_groups=False)
                 .reset_index(level=0)
        )
        payload = computed[["symbol", "trade_date"] + columns]
        store.con.register("_seq", payload)
        store.con.execute("INSERT INTO indicators_seq SELECT * FROM _seq")
        store.con.unregister("_seq")
        written += len(payload)
        log.info("indicators: %d/%d symbols, %d rows", min(start + len(batch), len(symbols)),
                 len(symbols), written)
    return written


def compute_windows(store: Store, cfg: Config) -> int:
    """Pass two: everything non-recursive, as DuckDB window functions."""
    ind = cfg.indicators
    ema_short, ema_mid = sorted(ind.ema_periods)[0], sorted(ind.ema_periods)[1]
    skip_total = ind.momentum_long_days + ind.momentum_skip_days

    store.con.execute("DROP TABLE IF EXISTS indicators")
    store.con.execute(
        f"""
        CREATE TABLE indicators AS
        WITH base AS (
            SELECT p.symbol, p.trade_date, p.open, p.high, p.low, p.close,
                   p.volume, p.turnover,
                   s.* EXCLUDE (symbol, trade_date)
              FROM prices_adjusted p
              JOIN indicators_seq s
                ON s.symbol = p.symbol AND s.trade_date = p.trade_date
        ),
        -- lag() is pulled into its own level because DuckDB cannot nest one
        -- window function inside another's argument.
        with_prior AS (
            SELECT *, lag(close) OVER (PARTITION BY symbol ORDER BY trade_date)
                          AS prior_close
              FROM base
        ),
        windowed AS (
            SELECT *,
                   close / nullif(lag(close, {ind.momentum_short_days})
                       OVER w, 0) - 1 AS mom_short,
                   close / nullif(lag(close, {ind.momentum_long_days})
                       OVER w, 0) - 1 AS mom_long,
                   -- formation window skipping the most recent month, the
                   -- convention that sidesteps short-term reversal
                   lag(close, {ind.momentum_skip_days}) OVER w
                       / nullif(lag(close, {skip_total}) OVER w, 0) - 1 AS mom_long_skip,

                   -- current bar EXCLUDED from its own baseline
                   volume / nullif(avg(volume) OVER (
                       PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN {ind.volume_avg_days} PRECEDING AND 1 PRECEDING
                   ), 0) AS vol_ratio,

                   avg(volume) OVER (PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN {ind.volume_fast_days - 1} PRECEDING AND CURRENT ROW)
                   / nullif(avg(volume) OVER (PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN {ind.volume_slow_days - 1} PRECEDING AND CURRENT ROW),
                     0) AS vol_sustained,

                   -- liquidity measured EXCLUDING today, so the volume spike the
                   -- screen selects on cannot also manufacture the liquidity
                   median(turnover) OVER (
                       PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN {ind.turnover_median_days} PRECEDING AND 1 PRECEDING
                   ) AS median_turnover,

                   max(high) OVER (PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN {ind.high_lookback_days - 1} PRECEDING AND CURRENT ROW
                   ) AS high_252,

                   avg(CASE WHEN close > prior_close THEN 1.0 ELSE 0.0 END) OVER (
                       PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN {ind.up_day_window - 1} PRECEDING AND CURRENT ROW
                   ) AS up_day_ratio,

                   count(*) OVER (PARTITION BY symbol ORDER BY trade_date
                       ROWS UNBOUNDED PRECEDING) AS bar_index
              FROM with_prior
            WINDOW w AS (PARTITION BY symbol ORDER BY trade_date)
        ),
        extended AS (
            SELECT *,
                   (close - ema_{ema_mid}) / nullif(atr, 0) AS ext_atr,
                   close / nullif(high_252, 0) - 1          AS pct_from_high
              FROM windowed
        )
        SELECT * EXCLUDE (rsi, adx, atr, ext_atr, pct_from_high, mom_short,
                          mom_long, mom_long_skip, vol_ratio, vol_sustained,
                          up_day_ratio, high_252),
               -- Derived indicators are stored as 32-bit floats. At ~7
               -- significant digits that is far more precision than an RSI or a
               -- volume ratio carries, and it halves the largest table in the
               -- database - which matters on an SD card. OHLC, turnover and
               -- median_turnover stay DOUBLE: they become fill prices and a
               -- liquidity threshold.
               CAST(rsi AS FLOAT) AS rsi, CAST(adx AS FLOAT) AS adx,
               CAST(atr AS FLOAT) AS atr, CAST(ext_atr AS FLOAT) AS ext_atr,
               CAST(pct_from_high AS FLOAT) AS pct_from_high,
               CAST(mom_short AS FLOAT) AS mom_short,
               CAST(mom_long AS FLOAT) AS mom_long,
               CAST(mom_long_skip AS FLOAT) AS mom_long_skip,
               CAST(vol_ratio AS FLOAT) AS vol_ratio,
               CAST(vol_sustained AS FLOAT) AS vol_sustained,
               CAST(up_day_ratio AS FLOAT) AS up_day_ratio,
               CAST(high_252 AS FLOAT) AS high_252,
               CAST((ext_atr - avg(ext_atr) OVER z)
                   / nullif(stddev_samp(ext_atr) OVER z, 0) AS FLOAT) AS ext_zscore
          FROM extended
        WINDOW z AS (PARTITION BY symbol ORDER BY trade_date
                     ROWS BETWEEN {ind.extension_zscore_days - 1} PRECEDING AND CURRENT ROW)
        """
    )
    return store.con.execute("SELECT count(*) FROM indicators").fetchone()[0]


def rebuild(store: Store, cfg: Config) -> dict[str, int]:
    seq = compute_sequential(store, cfg)
    total = compute_windows(store, cfg)
    # Scratch table: everything in it has been folded into `indicators`.
    store.con.execute("DROP TABLE IF EXISTS indicators_seq")
    store.con.execute("CHECKPOINT")
    return {"sequential_rows": seq, "indicator_rows": total}
