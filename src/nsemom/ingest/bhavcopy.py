"""Fetch and normalise NSE equity bhavcopy in both published formats.

NSE replaced the legacy bhavcopy with UDiFF on 2024-07-08, but the legacy
archive is still served for historical dates. Neither format alone spans
2015-today, so both parsers are needed and they normalise onto one Bar record.

Verified against live endpoints: UDiFF is available from 2024-01-02, legacy
through 2024-07-08. The overlap is what tests/test_bhavcopy.py uses to assert
the two parsers produce identical output for the same session.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import zipfile
from dataclasses import dataclass, fields
from typing import Literal

from ..config import Config, Source, UniverseConfig
from .client import NseClient

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Bar:
    """One symbol's session. Field order matches the prices table."""

    symbol: str
    trade_date: dt.date
    isin: str | None
    series: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    prev_close: float | None
    volume: int | None
    turnover: float | None
    trades: int | None


BAR_COLUMNS = tuple(f.name for f in fields(Bar))


@dataclass(frozen=True)
class DayFetch:
    status: Literal["ok", "not_found"]
    source: str
    bars: tuple[Bar, ...] = ()
    sha256: str | None = None
    raw: bytes | None = None


class ParseError(ValueError):
    """The file downloaded but did not look like the bhavcopy we expected."""


def _to_float(raw: str) -> float | None:
    raw = raw.strip()
    if not raw or raw == "-":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _to_int(raw: str) -> int | None:
    value = _to_float(raw)
    return None if value is None else int(value)


# NSE is not perfectly consistent here. Almost every legacy file uses
# "02-JAN-2015", but at least 2020-07-13 was published as "13-Jul-20" - a
# two-digit year in mixed case. Both are accepted; anything else is an error.
LEGACY_DATE_FORMATS = ("%d-%b-%Y", "%d-%b-%y")


def _parse_legacy_date(raw: str) -> dt.date:
    raw = raw.strip()
    for fmt in LEGACY_DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ParseError(f"unrecognised TIMESTAMP format: {raw!r}")


def unzip_single_csv(content: bytes) -> str:
    """Bhavcopy zips contain exactly one CSV."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        if len(names) != 1:
            raise ParseError(f"expected 1 file in archive, found {names}")
        return archive.read(names[0]).decode("utf-8", errors="replace")


def _read_table(text: str) -> tuple[dict[str, int], list[list[str]]]:
    """Return a header index and rows, tolerating stray whitespace and the
    trailing empty column the legacy format carries."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ParseError("empty file") from None
    index = {name.strip(): i for i, name in enumerate(header) if name.strip()}
    return index, [row for row in reader if row]


def _parse_udiff(text: str, universe: UniverseConfig) -> list[Bar]:
    idx, rows = _read_table(text)
    required = {"TradDt", "TckrSymb", "SctySrs", "FinInstrmTp", "ISIN",
                "OpnPric", "HghPric", "LwPric", "ClsPric", "PrvsClsgPric",
                "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd"}
    missing = required - idx.keys()
    if missing:
        raise ParseError(f"UDiFF file missing columns: {sorted(missing)}")

    bars: list[Bar] = []
    for row in rows:
        if len(row) <= idx["ClsPric"]:
            continue
        series = row[idx["SctySrs"]].strip()
        if series not in universe.series:
            continue
        if row[idx["FinInstrmTp"]].strip() not in universe.instrument_types:
            continue
        symbol = row[idx["TckrSymb"]].strip()
        if universe.is_excluded(symbol):
            continue
        bars.append(Bar(
            symbol=symbol,
            trade_date=dt.date.fromisoformat(row[idx["TradDt"]].strip()),
            isin=row[idx["ISIN"]].strip() or None,
            series=series,
            open=_to_float(row[idx["OpnPric"]]),
            high=_to_float(row[idx["HghPric"]]),
            low=_to_float(row[idx["LwPric"]]),
            close=_to_float(row[idx["ClsPric"]]),
            prev_close=_to_float(row[idx["PrvsClsgPric"]]),
            volume=_to_int(row[idx["TtlTradgVol"]]),
            turnover=_to_float(row[idx["TtlTrfVal"]]),
            trades=_to_int(row[idx["TtlNbOfTxsExctd"]]),
        ))
    return bars


def _parse_legacy(text: str, universe: UniverseConfig) -> list[Bar]:
    idx, rows = _read_table(text)
    # ISIN and TOTALTRADES are read defensively below - the oldest legacy
    # files predate them - so they are not required here.
    required = {"SYMBOL", "SERIES", "TIMESTAMP", "OPEN", "HIGH", "LOW",
                "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TOTTRDVAL"}
    missing = required - idx.keys()
    if missing:
        raise ParseError(f"legacy file missing columns: {sorted(missing)}")

    bars: list[Bar] = []
    for row in rows:
        if len(row) <= idx["CLOSE"]:
            continue
        series = row[idx["SERIES"]].strip()
        if series not in universe.series:
            continue
        symbol = row[idx["SYMBOL"]].strip()
        if universe.is_excluded(symbol):
            continue
        # Legacy carries no instrument-type column; the EQ series is equities
        # by definition, so the series filter alone is sufficient here.
        bars.append(Bar(
            symbol=symbol,
            trade_date=_parse_legacy_date(row[idx["TIMESTAMP"]]),
            isin=row[idx["ISIN"]].strip() or None if "ISIN" in idx else None,
            series=series,
            open=_to_float(row[idx["OPEN"]]),
            high=_to_float(row[idx["HIGH"]]),
            low=_to_float(row[idx["LOW"]]),
            close=_to_float(row[idx["CLOSE"]]),
            prev_close=_to_float(row[idx["PREVCLOSE"]]),
            volume=_to_int(row[idx["TOTTRDQTY"]]),
            turnover=_to_float(row[idx["TOTTRDVAL"]]),
            trades=_to_int(row[idx["TOTALTRADES"]]) if "TOTALTRADES" in idx else None,
        ))
    return bars


_PARSERS = {"udiff": _parse_udiff, "legacy": _parse_legacy}


def parse(text: str, source_name: str, universe: UniverseConfig,
          expected_date: dt.date | None = None) -> list[Bar]:
    try:
        parser = _PARSERS[source_name]
    except KeyError:
        raise ParseError(f"no parser for source {source_name!r}") from None

    try:
        bars = parser(text, universe)
    except ParseError:
        raise
    except (ValueError, KeyError, IndexError) as exc:
        raise ParseError(f"{source_name}: malformed file - {type(exc).__name__}: {exc}") from exc
    if not bars:
        raise ParseError(f"{source_name}: no rows survived the universe filter")

    if expected_date is not None:
        mismatched = {b.trade_date for b in bars} - {expected_date}
        if mismatched:
            raise ParseError(
                f"{source_name}: file for {expected_date} contains dates "
                f"{sorted(mismatched)} - NSE may have served the wrong session"
            )
    return bars


def fetch_day(client: NseClient, cfg: Config, day: dt.date) -> DayFetch:
    """Download and normalise one session. A 404 means 'not a trading day'."""
    source: Source | None = cfg.ingest.source_for(day)
    if source is None:
        raise ParseError(f"no configured bhavcopy source covers {day}")

    result = client.fetch(source.url_for(day))
    if result.status == "not_found":
        return DayFetch(status="not_found", source=source.name)

    assert result.content is not None
    bars = parse(
        unzip_single_csv(result.content), source.name, cfg.universe, expected_date=day
    )
    return DayFetch(
        status="ok",
        source=source.name,
        bars=tuple(bars),
        sha256=result.sha256,
        raw=result.content if cfg.ingest.keep_raw else None,
    )
