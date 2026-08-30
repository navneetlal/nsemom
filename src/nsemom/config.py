"""Typed access to config/config.toml.

Every threshold, endpoint and cost in this project is a config value. Nothing
numeric should be hardcoded in a module.
"""

from __future__ import annotations

import datetime as dt
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .costs import CostConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "config.toml"

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


@dataclass(frozen=True)
class Source:
    """One bhavcopy endpoint, valid over a closed date range."""

    name: str
    url_template: str
    start: dt.date
    end: dt.date

    def covers(self, day: dt.date) -> bool:
        return self.start <= day <= self.end

    def url_for(self, day: dt.date) -> str:
        return self.url_template.format(
            yyyymmdd=f"{day:%Y%m%d}",
            yyyy=f"{day:%Y}",
            mm=f"{day:%m}",
            dd=f"{day:%d}",
            MON=_MONTHS[day.month - 1],
        )


@dataclass(frozen=True)
class IngestConfig:
    user_agent: str
    referer: str
    history_start: dt.date
    request_timeout_seconds: float
    min_request_interval_seconds: float
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    keep_raw: bool
    holiday_confirm_after_days: int
    backfill_checkpoint_threshold: str
    sources: tuple[Source, ...]

    def source_for(self, day: dt.date) -> Source | None:
        """First source covering `day`. Order in config is priority order."""
        for source in self.sources:
            if source.covers(day):
                return source
        return None


@dataclass(frozen=True)
class BacktestConfig:
    portfolio_mode: str
    slots: int
    capital: float
    min_warmup_bars: int
    initial_stop_atr: float
    trailing_stop_atr: float
    trailing_arms_at_r: float
    structural_exit_days: int
    max_hold_days: int
    min_hold_days: int


@dataclass(frozen=True)
class IndicatorConfig:
    ema_periods: tuple[int, ...]
    rsi_period: int
    atr_period: int
    adx_period: int
    momentum_short_days: int
    momentum_long_days: int
    momentum_skip_days: int
    volume_avg_days: int
    volume_fast_days: int
    volume_slow_days: int
    turnover_median_days: int
    high_lookback_days: int
    extension_zscore_days: int
    up_day_window: int
    divergence_window: int
    batch_symbols: int


@dataclass(frozen=True)
class CorporateActionConfig:
    verification_tolerance: float
    quarantine_days_before: int
    quarantine_days_after: int


@dataclass(frozen=True)
class UniverseConfig:
    series: frozenset[str]
    instrument_types: frozenset[str]
    exclude_symbol_patterns: tuple[re.Pattern[str], ...]

    def is_excluded(self, symbol: str) -> bool:
        return any(p.search(symbol) for p in self.exclude_symbol_patterns)


@dataclass(frozen=True)
class Config:
    db_path: Path
    output_dir: Path
    ingest: IngestConfig
    universe: UniverseConfig
    corporate_actions: CorporateActionConfig
    indicators: IndicatorConfig
    backtest: BacktestConfig
    costs: CostConfig
    # Screen presets stay as raw dicts here: config.py cannot import screen.py
    # without a cycle (screen -> store -> ingest -> config).
    screen_raw: dict
    default_preset: str

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        path = Path(path) if path else DEFAULT_CONFIG
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)

        ing = raw["ingest"]
        sources = tuple(
            Source(
                name=s["name"],
                url_template=s["url"],
                start=s["start"],
                end=s["end"],
            )
            for s in ing["sources"]
        )
        uni = raw["universe"]
        ca = raw["corporate_actions"]
        ind = raw["indicators"]
        bt = raw["backtest"]
        scr = raw["screen"]

        def resolve(p: str) -> Path:
            candidate = Path(p)
            return candidate if candidate.is_absolute() else REPO_ROOT / candidate

        return cls(
            db_path=resolve(raw["paths"]["db"]),
            output_dir=resolve(raw["paths"]["output"]),
            ingest=IngestConfig(
                user_agent=ing["user_agent"],
                referer=ing["referer"],
                history_start=ing["history_start"],
                request_timeout_seconds=float(ing["request_timeout_seconds"]),
                min_request_interval_seconds=float(ing["min_request_interval_seconds"]),
                max_retries=int(ing["max_retries"]),
                backoff_base_seconds=float(ing["backoff_base_seconds"]),
                backoff_max_seconds=float(ing["backoff_max_seconds"]),
                keep_raw=bool(ing["keep_raw"]),
                holiday_confirm_after_days=int(ing["holiday_confirm_after_days"]),
                backfill_checkpoint_threshold=ing["backfill_checkpoint_threshold"],
                sources=sources,
            ),
            backtest=BacktestConfig(
                portfolio_mode=bt["portfolio_mode"], slots=int(bt["slots"]),
                capital=float(bt["capital"]),
                min_warmup_bars=int(bt["min_warmup_bars"]),
                initial_stop_atr=float(bt["initial_stop_atr"]),
                trailing_stop_atr=float(bt["trailing_stop_atr"]),
                trailing_arms_at_r=float(bt["trailing_arms_at_r"]),
                structural_exit_days=int(bt["structural_exit_days"]),
                max_hold_days=int(bt["max_hold_days"]),
                min_hold_days=int(bt["min_hold_days"]),
            ),
            costs=CostConfig.from_raw(raw["costs"]),
            screen_raw=scr["presets"],
            default_preset=scr["default_preset"],
            indicators=IndicatorConfig(
                ema_periods=tuple(int(x) for x in ind["ema_periods"]),
                rsi_period=int(ind["rsi_period"]),
                atr_period=int(ind["atr_period"]),
                adx_period=int(ind["adx_period"]),
                momentum_short_days=int(ind["momentum_short_days"]),
                momentum_long_days=int(ind["momentum_long_days"]),
                momentum_skip_days=int(ind["momentum_skip_days"]),
                volume_avg_days=int(ind["volume_avg_days"]),
                volume_fast_days=int(ind["volume_fast_days"]),
                volume_slow_days=int(ind["volume_slow_days"]),
                turnover_median_days=int(ind["turnover_median_days"]),
                high_lookback_days=int(ind["high_lookback_days"]),
                extension_zscore_days=int(ind["extension_zscore_days"]),
                up_day_window=int(ind["up_day_window"]),
                divergence_window=int(ind["divergence_window"]),
                batch_symbols=int(ind["batch_symbols"]),
            ),
            corporate_actions=CorporateActionConfig(
                verification_tolerance=float(ca["verification_tolerance"]),
                quarantine_days_before=int(ca["quarantine_days_before"]),
                quarantine_days_after=int(ca["quarantine_days_after"]),
            ),
            universe=UniverseConfig(
                series=frozenset(uni["series"]),
                instrument_types=frozenset(uni["instrument_types"]),
                exclude_symbol_patterns=tuple(
                    re.compile(p) for p in uni["exclude_symbol_patterns"]
                ),
            ),
        )
