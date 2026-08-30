"""Command line entry points.

    nsemom backfill [--from D] [--to D] [--limit N]   walk history, resumable
    nsemom update                                     fill every gap up to today
    nsemom status                                     what is in the database
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from dataclasses import dataclass

from . import adjust as adjust_mod
from . import indicators as indicators_mod
from . import output as output_mod
from .backtest import ExitRules, exit_reason_breakdown, run as run_backtest, walk_forward
from .calendar import is_conclusively_absent, weekdays
from .config import Config
from .ingest.bhavcopy import ParseError, fetch_day
from .ingest.client import FetchError, NseClient
from .ingest.corpactions import fetch_range
from .screen import Preset
from .store.db import Store, compact

log = logging.getLogger("nsemom")


@dataclass
class Tally:
    stored: int = 0
    rows: int = 0
    holidays: int = 0
    pending: int = 0
    failed: int = 0
    skipped: int = 0


def _date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def run_ingest(cfg: Config, start: dt.date, end: dt.date, limit: int | None,
               max_consecutive_failures: int) -> Tally:
    today = dt.date.today()
    tally = Tally()
    consecutive_failures = 0

    with Store(cfg.db_path) as store, NseClient(cfg.ingest) as client:
        store.defer_checkpoints(cfg.ingest.backfill_checkpoint_threshold)
        settled = store.settled_dates()

        try:
            for day in weekdays(start, end):
                if day in settled:
                    tally.skipped += 1
                    continue
                if limit is not None and (tally.stored + tally.holidays) >= limit:
                    log.info("limit of %d days reached", limit)
                    break

                try:
                    result = fetch_day(client, cfg, day)
                except Exception as exc:
                    # Deliberately broad: a backfill is a multi-hour unattended
                    # run over a decade of third-party files, and one malformed
                    # session must cost that session, not the whole run. The
                    # consecutive-failure breaker below still stops a real
                    # outage, and `status` surfaces every failed date.
                    store.mark(day, "failed",
                               note=f"{type(exc).__name__}: {exc}"[:500])
                    tally.failed += 1
                    consecutive_failures += 1
                    log.error("%s FAILED  %s", day, exc)
                    if consecutive_failures >= max_consecutive_failures:
                        log.error(
                            "aborting: %d consecutive failures - NSE may be "
                            "blocking or the URL template may have changed",
                            consecutive_failures,
                        )
                        break
                    continue

                consecutive_failures = 0

                if result.status == "not_found":
                    if is_conclusively_absent(
                        day, today, cfg.ingest.holiday_confirm_after_days
                    ):
                        store.mark(day, "holiday", source=result.source)
                        tally.holidays += 1
                        log.info("%s holiday", day)
                    else:
                        store.mark(day, "pending", source=result.source,
                                   note="not published yet")
                        tally.pending += 1
                        log.info("%s pending (not published yet)", day)
                    continue

                count = store.write_day(day, result.source, result.bars, result.sha256)
                tally.stored += 1
                tally.rows += count
                log.info("%s %-6s %5d rows", day, result.source, count)
        finally:
            # Fold the WAL back into the database file once, rather than on
            # every commit. Also runs on Ctrl-C so a stopped backfill stays tidy.
            store.checkpoint()

    return tally


def cmd_ingest(args: argparse.Namespace, cfg: Config) -> int:
    start = args.date_from or cfg.ingest.history_start
    end = args.date_to or dt.date.today()
    log.info("ingesting %s .. %s", start, end)

    tally = run_ingest(cfg, start, end, args.limit, args.max_consecutive_failures)

    log.info(
        "done: %d sessions (%d rows), %d holidays, %d pending, %d failed, %d skipped",
        tally.stored, tally.rows, tally.holidays, tally.pending,
        tally.failed, tally.skipped,
    )
    # The scheduled job must fail loudly rather than leave a stale shortlist.
    return 1 if tally.failed else 0


def cmd_compact(args: argparse.Namespace, cfg: Config) -> int:
    if not cfg.db_path.exists():
        log.error("no database at %s", cfg.db_path)
        return 1
    before, after = compact(cfg.db_path)
    log.info("compacted %.0f MB -> %.0f MB (reclaimed %.0f MB)",
             before, after, before - after)
    return 0


def _compact_if_bloated(cfg: Config, threshold_mb: float) -> None:
    """Rebuilding the indicator table leaves its predecessor as dead space."""
    if cfg.db_path.exists() and cfg.db_path.stat().st_size / 1e6 > threshold_mb:
        before, after = compact(cfg.db_path)
        log.info("compacted %.0f MB -> %.0f MB", before, after)


def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    if not cfg.db_path.exists():
        print(f"no database at {cfg.db_path}")
        return 1
    with Store(cfg.db_path, read_only=True) as store:
        s = store.summary()
        print(f"database   {cfg.db_path}  ({s['db_mb']:.1f} MB)")
        print(f"price rows {s['price_rows']:,}  across {s['symbols']:,} symbols")
        print(f"date range {s['first_date']} .. {s['last_date']}")
        print(f"sessions   {s['sessions']:,} stored, {s['holidays']:,} holidays")
        if s["pending"]:
            print(f"pending    {s['pending']:,} (not published when last attempted)")
        if s["failed"]:
            print(f"FAILED     {s['failed']:,} dates")
            for day, _, note in store.recent_failures():
                print(f"   {day}  {note}")
        if s["duplicates"]:
            print(f"DUPLICATES {s['duplicates']:,} - this should never be non-zero")
            return 1
    return 0


def _preset(cfg: Config, name: str | None) -> Preset:
    name = name or cfg.default_preset
    if name not in cfg.screen_raw:
        raise SystemExit(f"unknown preset {name!r}; have {sorted(cfg.screen_raw)}")
    return Preset.from_raw(name, cfg.screen_raw[name])


def _rules(cfg: Config) -> ExitRules:
    bt = cfg.backtest
    return ExitRules(bt.initial_stop_atr, bt.trailing_stop_atr,
                     bt.trailing_arms_at_r, bt.structural_exit_days,
                     bt.max_hold_days, bt.min_hold_days)


def _ema_mid(cfg: Config) -> int:
    return sorted(cfg.indicators.ema_periods)[1]


def cmd_corpactions(args: argparse.Namespace, cfg: Config) -> int:
    start = args.date_from or cfg.ingest.history_start
    end = args.date_to or dt.date.today()
    log.info("corporate actions %s .. %s", start, end)
    with Store(cfg.db_path) as store, NseClient(cfg.ingest) as client:
        actions = fetch_range(client, cfg, start, end)
        stored = store.upsert_corporate_actions(actions)
        log.info("stored %d records", stored)
        for action_type, verification, count in store.corporate_action_summary():
            log.info("  %-14s %-11s %d", action_type, verification, count)
    return 0


def cmd_adjust(args: argparse.Namespace, cfg: Config) -> int:
    ca = cfg.corporate_actions
    with Store(cfg.db_path) as store:
        verdicts = adjust_mod.verify(store, ca.verification_tolerance,
                                     ca.quarantine_days_before, ca.quarantine_days_after)
        intervals = adjust_mod.rebuild_intervals(store)
        for verdict, count in sorted(verdicts.items()):
            log.info("  %-11s %d", verdict, count)
        log.info("%d adjustment intervals built", intervals)
        quarantined = store.con.execute(
            "SELECT count(DISTINCT symbol) FROM quarantine").fetchone()[0]
        if quarantined:
            log.warning("%d symbols quarantined (demerger or ratio mismatch)",
                        quarantined)
        store.checkpoint()
    return 0


def cmd_indicators(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg.db_path) as store:
        counts = indicators_mod.rebuild(store, cfg)
        log.info("indicators rebuilt: %s", counts)
        store.checkpoint()
    return 0


def cmd_screen(args: argparse.Namespace, cfg: Config) -> int:
    preset = _preset(cfg, args.preset)
    with Store(cfg.db_path, read_only=True) as store:
        as_of = args.date or output_mod.latest_date(store)
        if as_of is None:
            log.error("no indicators; run `nsemom indicators` first")
            return 1
        frame = output_mod.shortlist(store, preset, _rules(cfg),
                                     cfg.backtest.min_warmup_bars, as_of)
        print(f"\n=== {preset.name} shortlist for {as_of} "
              f"({len(frame)} names, ranked by {preset.rank_by}) ===")
        print(output_mod.render(frame, {"close": 2, "rsi": 1, "adx": 1,
                                        "median_turnover": 0, "atr": 2,
                                        "suggested_stop": 2}))
    return 0


def cmd_backtest(args: argparse.Namespace, cfg: Config) -> int:
    rules, costs = _rules(cfg), cfg.costs
    names = args.presets or [cfg.default_preset]
    with Store(cfg.db_path, read_only=False) as store:
        if args.split:
            preset = _preset(cfg, names[0])
            halves = walk_forward(store, preset, rules, costs, cfg.backtest.slots,
                                  cfg.backtest.capital, cfg.backtest.min_warmup_bars,
                                  _ema_mid(cfg), args.split)
            print(f"\n=== walk-forward, split at {args.split}, preset {preset.name} ===")
            print(_stats_table({k: v.stats for k, v in halves.items()}))
            return 0

        results = {}
        for name in names:
            preset = _preset(cfg, name)
            log.info("backtesting %s ...", name)
            result = run_backtest(store, preset, rules, costs, cfg.backtest.slots,
                                  cfg.backtest.capital, cfg.backtest.min_warmup_bars,
                                  _ema_mid(cfg), args.start, args.end)
            results[name] = result
            if not result.trades.empty and args.verbose:
                print(f"\n--- {name}: exits ---")
                print(exit_reason_breakdown(result.trades).to_string())
        print("\n=== backtest: GROSS (no costs, no slippage) ===")
        print(_stats_table({k: (v.stats_gross or {}) for k, v in results.items()}))
        print("\n=== backtest: NET of all costs ===")
        print(_stats_table({k: v.stats for k, v in results.items()}))
        print("\n=== what costs ate ===")
        for name, result in results.items():
            print(f"  {name:<10} {result.cost_drag_on_return:+.2%} of total return")

        if getattr(args, "by_year", False):
            from .backtest import (rolling_window_returns, summarise_distribution,
                                   yearly_returns)
            import pandas as pd
            for name, result in results.items():
                if result.equity.empty:
                    continue
                print(f"\n=== {name}: calendar years (net) ===")
                yearly = yearly_returns(result.equity)
                print(yearly.to_string(index=False,
                                       float_format=lambda v: f"{v:,.2%}"))
                print(f"\n=== {name}: outcome from EVERY possible start date (net) ===")
                rows = [
                    summarise_distribution(
                        rolling_window_returns(result.equity, days), f"{label} hold")
                    for days, label in ((30, "1 month"), (90, "3 month"),
                                        (365, "1 year"), (1095, "3 year"))
                ]
                table = pd.DataFrame([r for r in rows if r.get("windows")])
                if not table.empty:
                    print(table.to_string(
                        index=False,
                        float_format=lambda v: f"{v:,.2%}"))
    return 0


def _stats_table(stats_by_name: dict[str, dict]) -> str:
    import pandas as pd
    rows = [
        "trades", "years", "total_return_net", "cagr_net", "max_drawdown",
        "hit_rate", "avg_win", "avg_loss", "win_loss_ratio", "mean_gross_return",
        "mean_net_return", "avg_hold_days", "turnover_x_per_year",
        "cost_drag_pct_of_capital", "cost_drag_per_trade",
    ]
    frame = pd.DataFrame({
        name: {r: stats.get(r) for r in rows} for name, stats in stats_by_name.items()
    })
    return frame.to_string(float_format=lambda v: f"{v:,.4f}")


def cmd_daily(args: argparse.Namespace, cfg: Config) -> int:
    """The scheduled job: ingest, adjust, recompute, then report."""
    failed = cmd_ingest(args, cfg)

    lookback = dt.date.today() - dt.timedelta(days=args.ca_lookback_days)
    args.date_from, args.date_to = lookback, dt.date.today()
    cmd_corpactions(args, cfg)
    cmd_adjust(args, cfg)
    cmd_indicators(args, cfg)
    _compact_if_bloated(cfg, args.compact_above_mb)

    preset = _preset(cfg, args.preset)
    with Store(cfg.db_path) as store:
        as_of = output_mod.latest_date(store)
        if as_of is None:
            log.error("no indicators available")
            return 1
        try:
            output_mod.check_health(store, as_of)
        except output_mod.HealthError as exc:
            log.error("HEALTH CHECK FAILED: %s", exc)
            return 1

        held = output_mod.open_position_status(store, _rules(cfg), _ema_mid(cfg), as_of)
        print(f"\n=== open positions as of {as_of} ===")
        print(output_mod.render(held, {"entry_price": 2, "last_close": 2,
                                       "initial_stop": 2, "trailing_stop": 2,
                                       "unrealised_return": 4}))

        frame = output_mod.shortlist(store, preset, _rules(cfg),
                                     cfg.backtest.min_warmup_bars, as_of)
        print(f"\n=== {preset.name} shortlist for {as_of} ({len(frame)} names) ===")
        print(output_mod.render(frame, {"close": 2, "rsi": 1, "adx": 1,
                                        "median_turnover": 0, "atr": 2,
                                        "suggested_stop": 2}))
        path = output_mod.write_outputs(frame, cfg.output_dir, "shortlist", as_of)
        output_mod.log_shortlist(store, frame, preset.name, as_of)
        store.checkpoint()
        log.info("wrote %s", path)
    return failed


def cmd_position(args: argparse.Namespace, cfg: Config) -> int:
    with Store(cfg.db_path) as store:
        if args.action == "add":
            next_id = (store.con.execute(
                "SELECT coalesce(max(position_id), 0) + 1 FROM positions").fetchone()[0])
            store.con.execute(
                "INSERT INTO positions (position_id, symbol, entry_date, "
                "entry_price, quantity) VALUES (?, ?, ?, ?, ?)",
                [next_id, args.symbol.upper(), args.date or dt.date.today(),
                 args.price, args.quantity])
            log.info("added position %d: %s", next_id, args.symbol.upper())
        elif args.action == "close":
            store.con.execute(
                "UPDATE positions SET exit_date = ?, exit_price = ?, exit_reason = ? "
                "WHERE position_id = ?",
                [args.date or dt.date.today(), args.price, args.reason, args.id])
            log.info("closed position %d", args.id)
        frame = store.con.execute(
            "SELECT * FROM positions ORDER BY exit_date NULLS FIRST, entry_date"
        ).df()
        print(output_mod.render(frame))
        store.checkpoint()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nsemom")
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("backfill", "walk history and store every session"),
        ("update", "fill every remaining gap up to today"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--from", dest="date_from", type=_date, default=None)
        p.add_argument("--to", dest="date_to", type=_date, default=None)
        p.add_argument("--limit", type=int, default=None,
                       help="stop after this many days (for smoke tests)")
        p.add_argument("--max-consecutive-failures", type=int, default=5)
        p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("status", help="summarise the database")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("corpactions", help="fetch and parse corporate actions")
    p.add_argument("--from", dest="date_from", type=_date, default=None)
    p.add_argument("--to", dest="date_to", type=_date, default=None)
    p.set_defaults(func=cmd_corpactions)

    p = sub.add_parser("adjust", help="verify ratios and rebuild the adjusted view")
    p.set_defaults(func=cmd_adjust)

    p = sub.add_parser("indicators", help="recompute all indicators")
    p.set_defaults(func=cmd_indicators)

    p = sub.add_parser("compact", help="reclaim dead space from rebuilt tables")
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("screen", help="today's shortlist")
    p.add_argument("--preset", default=None)
    p.add_argument("--date", type=_date, default=None)
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("backtest", help="measure a preset, net and gross of costs")
    p.add_argument("--preset", dest="presets", action="append", default=None,
                   help="repeatable, to compare presets head to head")
    p.add_argument("--start", type=_date, default=None)
    p.add_argument("--end", type=_date, default=None)
    p.add_argument("--split", type=_date, default=None,
                   help="walk-forward: tune before this date, measure after")
    p.add_argument("--by-year", action="store_true",
                   help="also report calendar-year returns and the distribution "
                        "of outcomes across every possible start date")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("daily", help="the scheduled job: ingest, recompute, report")
    p.add_argument("--from", dest="date_from", type=_date, default=None)
    p.add_argument("--to", dest="date_to", type=_date, default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--max-consecutive-failures", type=int, default=5)
    p.add_argument("--preset", default=None)
    p.add_argument("--ca-lookback-days", type=int, default=45)
    p.add_argument("--compact-above-mb", type=float, default=900.0,
                   help="reclaim dead space once the database exceeds this size")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("position", help="record what you actually bought")
    p.add_argument("action", choices=["add", "close", "list"])
    p.add_argument("--symbol", default="")
    p.add_argument("--price", type=float, default=None)
    p.add_argument("--quantity", type=int, default=0)
    p.add_argument("--date", type=_date, default=None)
    p.add_argument("--id", type=int, default=None)
    p.add_argument("--reason", default="manual")
    p.set_defaults(func=cmd_position)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config.load(args.config)
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        log.warning("interrupted - progress is committed, re-run to resume")
        return 130


if __name__ == "__main__":
    sys.exit(main())
