"""DuckDB persistence. Single file, no server process."""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

import duckdb
import pandas as pd

from ..ingest.bhavcopy import BAR_COLUMNS, Bar

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

log = logging.getLogger(__name__)


def compact(path: Path) -> tuple[float, float]:
    """Rewrite the database into a fresh file, reclaiming dead space.

    DuckDB does not return space from dropped or replaced tables to the
    filesystem, and the indicator table is dropped and rebuilt on every run. Left
    alone the file grows without bound, which on an SD card is the one failure
    mode worth engineering against. Returns (before_mb, after_mb).
    """
    before = path.stat().st_size / 1e6
    scratch = path.with_suffix(".compacting")
    scratch.unlink(missing_ok=True)

    con = duckdb.connect(str(path))
    try:
        current = con.execute("SELECT current_database()").fetchone()[0]
        con.execute(f"ATTACH '{scratch}' AS _fresh")
        con.execute(f'COPY FROM DATABASE "{current}" TO _fresh')
        con.execute("DETACH _fresh")
    finally:
        con.close()

    os.replace(scratch, path)
    return before, path.stat().st_size / 1e6


class Store:
    def __init__(self, path: Path, read_only: bool = False) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path), read_only=read_only)
        if not read_only:
            self.con.execute(SCHEMA_PATH.read_text())

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()

    # -- backfill tuning ---------------------------------------------------
    def defer_checkpoints(self, threshold: str) -> None:
        """Let the WAL grow during backfill instead of rewriting the main
        database file on every commit. This is the SD-card wear guard."""
        self.con.execute(f"SET checkpoint_threshold = '{threshold}'")

    def checkpoint(self) -> None:
        self.con.execute("CHECKPOINT")

    # -- writes ------------------------------------------------------------
    def write_day(self, day: dt.date, source: str, bars: Sequence[Bar],
                  sha: str | None) -> int:
        """Replace one session atomically. Re-running is a no-op on row counts."""
        frame = pd.DataFrame(
            [
                (b.symbol, b.trade_date, b.isin, b.series, b.open, b.high, b.low,
                 b.close, b.prev_close, b.volume, b.turnover, b.trades)
                for b in bars
            ],
            columns=list(BAR_COLUMNS),
        )
        self.con.execute("BEGIN TRANSACTION")
        try:
            self.con.execute("DELETE FROM prices WHERE trade_date = ?", [day])
            self.con.register("_incoming", frame)
            self.con.execute(
                f"INSERT INTO prices SELECT {', '.join(BAR_COLUMNS)} FROM _incoming"
            )
            self.con.unregister("_incoming")
            self._log(day, source, "ok", len(bars), sha, None)
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return len(bars)

    def mark(self, day: dt.date, status: str, source: str | None = None,
             note: str | None = None) -> None:
        self._log(day, source, status, None, None, note)

    def _log(self, day: dt.date, source: str | None, status: str,
             rows: int | None, sha: str | None, note: str | None) -> None:
        self.con.execute(
            """INSERT OR REPLACE INTO ingest_log
               (trade_date, source, status, row_count, content_sha, fetched_at, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [day, source, status, rows, sha, dt.datetime.now(), note],
        )

    def upsert_corporate_actions(self, actions) -> int:
        rows = [
            (a.symbol, a.ex_date, a.isin, a.series, a.action_type, a.subject,
             a.face_value, a.adj_factor,
             "ignored" if a.action_type == "ignored" else "pending", a.note)
            for a in actions
        ]
        if not rows:
            return 0
        self.con.executemany(
            """INSERT OR REPLACE INTO corporate_actions
               (symbol, ex_date, isin, series, action_type, subject, face_value,
                adj_factor, verification, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        return len(rows)

    def corporate_action_summary(self) -> list[tuple]:
        return self.con.execute(
            """SELECT action_type, verification, count(*)
                 FROM corporate_actions GROUP BY 1, 2 ORDER BY 1, 2"""
        ).fetchall()

    # -- reads -------------------------------------------------------------
    def settled_dates(self) -> set[dt.date]:
        """Dates needing no further work: stored, or confirmed non-trading."""
        rows = self.con.execute(
            "SELECT trade_date FROM ingest_log WHERE status IN ('ok', 'holiday')"
        ).fetchall()
        return {r[0] for r in rows}

    def last_stored_date(self) -> dt.date | None:
        row = self.con.execute("SELECT max(trade_date) FROM prices").fetchone()
        return row[0] if row and row[0] else None

    def duplicate_row_count(self) -> int:
        row = self.con.execute(
            """SELECT coalesce(sum(n - 1), 0) FROM (
                   SELECT count(*) AS n FROM prices
                   GROUP BY symbol, trade_date HAVING count(*) > 1
               )"""
        ).fetchone()
        return int(row[0]) if row else 0

    def summary(self) -> dict[str, object]:
        price_rows, symbols, first, last = self.con.execute(
            "SELECT count(*), count(DISTINCT symbol), min(trade_date), max(trade_date) "
            "FROM prices"
        ).fetchone()
        by_status = dict(self.con.execute(
            "SELECT status, count(*) FROM ingest_log GROUP BY status ORDER BY status"
        ).fetchall())
        return {
            "price_rows": price_rows,
            "symbols": symbols,
            "first_date": first,
            "last_date": last,
            "sessions": by_status.get("ok", 0),
            "holidays": by_status.get("holiday", 0),
            "pending": by_status.get("pending", 0),
            "failed": by_status.get("failed", 0),
            "duplicates": self.duplicate_row_count(),
            "db_mb": self.path.stat().st_size / 1e6 if self.path.exists() else 0.0,
        }

    def recent_failures(self, limit: int = 10) -> list[tuple]:
        return self.con.execute(
            "SELECT trade_date, status, note FROM ingest_log "
            "WHERE status = 'failed' ORDER BY trade_date DESC LIMIT ?", [limit]
        ).fetchall()
