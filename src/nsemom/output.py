"""Daily output: the shortlist, open-position exit status, and health checks.

The shortlist is deliberately not a buy list. It is an evidence pack - the
deterministic layer's answer to "what is worth looking at, and how much of the
move has already happened" - sized to be pasted into the research prompt in the
README.

Exits are the opposite: fully deterministic, evaluated here every day against
whatever is actually held, and reported before the new candidates.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pandas as pd

from .backtest import ExitRules
from .screen import Preset, build_query
from .store.db import Store

log = logging.getLogger(__name__)

SHORTLIST_COLUMNS = [
    "rank", "symbol", "close", "rsi", "adx", "mom_short", "mom_long_skip",
    "ext_atr", "ext_zscore", "pct_from_high", "trend_age_days", "up_day_ratio",
    "rsi_divergence", "vol_ratio", "vol_sustained", "median_turnover", "atr",
    "suggested_stop",
]


class HealthError(RuntimeError):
    """Raised when the day's data is not trustworthy enough to act on."""


def latest_date(store: Store) -> dt.date | None:
    row = store.con.execute("SELECT max(trade_date) FROM indicators").fetchone()
    return row[0] if row and row[0] else None


def check_health(store: Store, as_of: dt.date, min_rows_ratio: float = 0.8) -> None:
    """Fail loudly rather than emit a stale or suspiciously thin shortlist."""
    rows = store.con.execute(
        "SELECT count(*) FROM prices WHERE trade_date = ?", [as_of]
    ).fetchone()[0]
    if rows == 0:
        raise HealthError(f"no price rows stored for {as_of}")

    median_rows = store.con.execute(
        """SELECT median(n) FROM (
               SELECT count(*) AS n FROM prices
                WHERE trade_date < ? AND trade_date >= ? - to_days(40)
                GROUP BY trade_date)""",
        [as_of, as_of],
    ).fetchone()[0]
    if median_rows and rows < median_rows * min_rows_ratio:
        raise HealthError(
            f"{as_of}: only {rows} rows against a 40-day median of "
            f"{median_rows:.0f} - refusing to screen on a partial session"
        )

    failed = store.con.execute(
        "SELECT count(*) FROM ingest_log WHERE status = 'failed'"
    ).fetchone()[0]
    if failed:
        raise HealthError(f"{failed} dates failed to ingest; fix before screening")

    mismatched = store.con.execute(
        "SELECT count(*) FROM corporate_actions WHERE verification = 'mismatch'"
    ).fetchone()[0]
    if mismatched:
        log.warning("%d corporate actions disagree with their price gap; "
                    "those symbols are quarantined", mismatched)


def shortlist(store: Store, preset: Preset, rules: ExitRules,
              min_warmup_bars: int, as_of: dt.date) -> pd.DataFrame:
    query = build_query(preset, min_warmup_bars)
    frame = store.con.execute(
        f"""
        SELECT * FROM ({query}) WHERE trade_date = ? AND rank <= ?
        ORDER BY rank
        """,
        [as_of, preset.basket_size],
    ).df()
    # Deliberately no early return on an empty frame: a day where nothing passes
    # must still report the same columns as any other day, or every consumer has
    # to special-case it.
    frame["suggested_stop"] = frame["close"] - rules.initial_stop_atr * frame["atr"]
    return frame[[c for c in SHORTLIST_COLUMNS if c in frame.columns]]


def open_position_status(store: Store, rules: ExitRules, ema_mid_period: int,
                         as_of: dt.date) -> pd.DataFrame:
    """Evaluate every deterministic exit rule against what is actually held."""
    return store.con.execute(
        f"""
        WITH held AS (
            SELECT position_id, symbol, entry_date, entry_price, quantity
              FROM positions WHERE exit_date IS NULL
        ),
        entry_state AS (
            SELECT h.*, i.atr AS atr_at_entry
              FROM held h
              JOIN indicators i
                ON i.symbol = h.symbol AND i.trade_date = h.entry_date
        ),
        path AS (
            SELECT e.position_id,
                   max(i.close)                                     AS peak_close,
                   count(*)                                         AS bars_held,
                   max(CASE WHEN i.trade_date = ? THEN i.close END) AS last_close,
                   max(CASE WHEN i.trade_date = ? THEN i.ema_{ema_mid_period} END)
                                                                    AS last_ema,
                   min(i.low)                                       AS lowest_low
              FROM entry_state e
              JOIN indicators i
                -- The entry bar is included, so a position bought on the most
                -- recent session still reports (as HOLD, 1 bar held) instead of
                -- being dropped by the join. This also makes bars_held agree
                -- with the backtest, where the entry bar counts as day one.
                ON i.symbol = e.symbol AND i.trade_date >= e.entry_date
                                       AND i.trade_date <= ?
             GROUP BY e.position_id
        )
        SELECT e.position_id, e.symbol, e.entry_date, e.entry_price, e.quantity,
               p.last_close, p.peak_close, p.bars_held,
               e.entry_price - {rules.initial_stop_atr} * e.atr_at_entry AS initial_stop,
               CASE WHEN p.peak_close >= e.entry_price
                         + {rules.trailing_arms_at_r * rules.initial_stop_atr} * e.atr_at_entry
                    THEN p.peak_close - {rules.trailing_stop_atr} * e.atr_at_entry
               END AS trailing_stop,
               p.last_close / e.entry_price - 1 AS unrealised_return,
               CASE
                 WHEN p.last_close <= e.entry_price
                      - {rules.initial_stop_atr} * e.atr_at_entry THEN 'EXIT: initial stop'
                 WHEN p.peak_close >= e.entry_price
                      + {rules.trailing_arms_at_r * rules.initial_stop_atr} * e.atr_at_entry
                      AND p.last_close <= p.peak_close
                          - {rules.trailing_stop_atr} * e.atr_at_entry
                      THEN 'EXIT: trailing stop'
                 WHEN p.bars_held >= {rules.max_hold_days} THEN 'EXIT: time stop'
                 WHEN p.last_close < p.last_ema AND p.bars_held >= {rules.min_hold_days}
                      THEN 'WATCH: below trend EMA'
                 ELSE 'HOLD'
               END AS action
          FROM entry_state e JOIN path p USING (position_id)
         ORDER BY action DESC, e.entry_date
        """,
        [as_of, as_of, as_of],
    ).df()


def render(frame: pd.DataFrame, float_cols: dict[str, int] | None = None) -> str:
    if frame.empty:
        return "  (none)"
    display = frame.copy()
    for column, places in (float_cols or {}).items():
        if column in display.columns:
            display[column] = display[column].map(lambda v: f"{v:,.{places}f}"
                                                  if pd.notna(v) else "-")
    return display.to_string(index=False)


def write_outputs(frame: pd.DataFrame, output_dir: Path, name: str,
                  as_of: dt.date) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}_{as_of}.csv"
    frame.to_csv(path, index=False)
    return path


def log_shortlist(store: Store, frame: pd.DataFrame, preset: str,
                  as_of: dt.date) -> None:
    """Keep every shortlist row, so the discretionary layer can be measured
    later against the full set it chose from."""
    if frame.empty:
        return
    rows = [
        (as_of, r["symbol"], int(r["rank"]), preset,
         json.dumps({k: (None if pd.isna(v) else v) for k, v in r.items()}, default=str))
        for _, r in frame.iterrows()
    ]
    store.con.executemany(
        "INSERT OR REPLACE INTO shortlist_log VALUES (?, ?, ?, ?, ?)", rows
    )
