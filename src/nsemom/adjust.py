"""Corporate action verification and the adjusted-price view.

Two steps, deliberately separate:

  verify()            compare each parsed ratio against the price gap the market
                      actually printed on the ex-date, and record the verdict
  rebuild_intervals() turn verified factors into a step function of cumulative
                      factors, from which prices_adjusted is a cheap view

Raw prices are never rewritten. Adjustment is always derived, so it is
reproducible, auditable and reversible - and re-running it after a correction to
the corporate action data simply produces the right answer.
"""

from __future__ import annotations

import datetime as dt
import logging

from .store.db import Store

log = logging.getLogger(__name__)

FAR_PAST = dt.date(1990, 1, 1)
FAR_FUTURE = dt.date(2099, 12, 31)


def verify(store: Store, tolerance: float, quarantine_before: int = 5,
           quarantine_after: int = 250) -> dict[str, int]:
    """Cross-check parsed ratios against the observed ex-date price gap.

    Actions are grouped by (symbol, ex_date) before comparison: several actions
    can share an ex-date and their factors compound, so only the product is
    comparable to the single gap the market printed. BAJFINANCE on 2025-06-16 is
    the canonical case - a 2:1 face-value split and a 4:1 bonus, together 10x.
    """
    store.con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _verdicts AS
        WITH combined AS (
            SELECT symbol, ex_date, product(adj_factor) AS parsed
            FROM corporate_actions
            WHERE adj_factor IS NOT NULL
            GROUP BY symbol, ex_date
        ),
        gap AS (
            SELECT c.symbol, c.ex_date, c.parsed,
                   p.prev_close / nullif(p.open, 0) AS observed
            FROM combined c
            LEFT JOIN prices p
                   ON p.symbol = c.symbol AND p.trade_date = c.ex_date
        )
        SELECT symbol, ex_date, parsed, observed,
               CASE WHEN observed IS NULL THEN NULL
                    ELSE abs(parsed / observed - 1) END AS deviation,
               CASE
                   WHEN observed IS NULL THEN 'unverified'
                   WHEN abs(parsed / observed - 1) <= ? THEN 'verified'
                   ELSE 'mismatch'
               END AS verdict
            FROM gap
        """,
        [tolerance],
    )
    store.con.execute(
        """
        UPDATE corporate_actions AS ca
           SET observed     = v.observed,
               deviation    = v.deviation,
               verification = v.verdict
          FROM _verdicts AS v
         WHERE ca.symbol = v.symbol AND ca.ex_date = v.ex_date
           AND ca.adj_factor IS NOT NULL
        """
    )
    # Demergers split value across entities with no single derivable ratio, and
    # a mismatch means we do not understand the event. Both are quarantined
    # rather than adjusted with a number we do not trust.
    store.con.execute(
        """
        INSERT OR REPLACE INTO quarantine (symbol, from_date, to_date, reason)
        SELECT symbol,
               ex_date - to_days(CAST(? AS INTEGER)),
               ex_date + to_days(CAST(? AS INTEGER)),
               CASE WHEN action_type = 'demerger' THEN 'demerger'
                    ELSE 'corporate action ratio disagrees with price gap' END
          FROM corporate_actions
         WHERE action_type = 'demerger' OR verification = 'mismatch'
        """,
        [quarantine_before, quarantine_after],
    )
    rows = store.con.execute(
        "SELECT verification, count(*) FROM corporate_actions GROUP BY 1"
    ).fetchall()
    return dict(rows)


def rebuild_intervals(store: Store) -> int:
    """Rebuild the cumulative-factor step function from verified actions.

    For a symbol with ex-dates E1 < E2 and factors f1, f2:
        bars before E1     -> divide by f1 * f2
        bars in [E1, E2)   -> divide by f2
        bars from E2       -> divide by 1

    A bar is adjusted by the product of every factor whose ex-date is still in
    its future, which is what puts old prices on today's share basis.
    """
    store.con.execute("DELETE FROM adj_intervals")
    store.con.execute(
        """
        INSERT INTO adj_intervals (symbol, start_date, end_date, cum_factor)
        WITH events AS (
            SELECT symbol, ex_date, product(adj_factor) AS f
              FROM corporate_actions
             WHERE verification = 'verified' AND adj_factor IS NOT NULL
             GROUP BY symbol, ex_date
        ),
        cumulative AS (
            SELECT symbol, ex_date,
                   -- product of this factor and every later one.
                   -- product() rather than exp(sum(ln)): the log round-trip is
                   -- lossy, and turned an exact 5.0 into 4.999999999999999.
                   product(f) OVER (PARTITION BY symbol ORDER BY ex_date DESC
                                    ROWS UNBOUNDED PRECEDING) AS cum_before,
                   row_number() OVER (PARTITION BY symbol ORDER BY ex_date) AS seq
              FROM events
        ),
        spans AS (
            -- the span ending at each ex-date carries that event's cumulative factor
            SELECT symbol,
                   coalesce(lag(ex_date) OVER (PARTITION BY symbol ORDER BY ex_date),
                            CAST(? AS DATE)) AS start_date,
                   ex_date AS end_date,
                   cum_before AS cum_factor
              FROM cumulative
            UNION ALL
            -- and everything after the final ex-date needs no adjustment
            SELECT symbol, max(ex_date) AS start_date, CAST(? AS DATE) AS end_date, 1.0
              FROM cumulative GROUP BY symbol
        )
        SELECT symbol, start_date, end_date, cum_factor
          FROM spans
         WHERE cum_factor <> 1.0 OR end_date = CAST(? AS DATE)
        """,
        [FAR_PAST, FAR_FUTURE, FAR_FUTURE],
    )
    count = store.con.execute("SELECT count(*) FROM adj_intervals").fetchone()[0]

    store.con.execute(
        """
        CREATE OR REPLACE VIEW prices_adjusted AS
        SELECT p.symbol, p.trade_date, p.isin, p.series,
               p.open       / coalesce(a.cum_factor, 1.0) AS open,
               p.high       / coalesce(a.cum_factor, 1.0) AS high,
               p.low        / coalesce(a.cum_factor, 1.0) AS low,
               p.close      / coalesce(a.cum_factor, 1.0) AS close,
               p.prev_close / coalesce(a.cum_factor, 1.0) AS prev_close,
               CAST(round(p.volume * coalesce(a.cum_factor, 1.0)) AS BIGINT) AS volume,
               p.turnover                                 AS turnover,
               p.trades                                   AS trades,
               coalesce(a.cum_factor, 1.0)                AS cum_factor
          FROM prices p
          LEFT JOIN adj_intervals a
                 ON a.symbol = p.symbol
                AND p.trade_date >= a.start_date
                AND p.trade_date <  a.end_date
        """
    )
    return count
