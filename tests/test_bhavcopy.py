"""Parser tests.

The headline test is parser equivalence. NSE published both the legacy and UDiFF
bhavcopy between 2024-01-02 and 2024-07-08, so the same session exists in two
independent encodings - if our two parsers disagree on any field, one of them is
wrong, and the overlap is the only place that can be proven rather than assumed.
"""

import datetime as dt

import pytest

from nsemom.config import UniverseConfig
from nsemom.ingest.bhavcopy import ParseError, parse, unzip_single_csv

from .conftest import FIXTURES, OVERLAP_DAY

COMPARED_FIELDS = ("open", "high", "low", "close", "prev_close",
                   "volume", "turnover", "trades", "isin", "series")


def _load(name, universe, day=OVERLAP_DAY):
    source = name.split("_")[0]
    text = unzip_single_csv((FIXTURES / name).read_bytes())
    return parse(text, source, universe, expected_date=day)


def test_parsers_agree_on_every_field_of_an_overlap_session(universe):
    udiff = {b.symbol: b for b in _load("udiff_20240301.csv.zip", universe)}
    legacy = {b.symbol: b for b in _load("legacy_20240301.csv.zip", universe)}

    assert udiff, "fixture parsed to nothing"
    assert set(udiff) == set(legacy), (
        f"symbol sets differ: only-udiff={sorted(set(udiff) - set(legacy))[:5]} "
        f"only-legacy={sorted(set(legacy) - set(udiff))[:5]}"
    )
    mismatches = [
        (symbol, field, getattr(udiff[symbol], field), getattr(legacy[symbol], field))
        for symbol in udiff
        for field in COMPARED_FIELDS
        if getattr(udiff[symbol], field) != getattr(legacy[symbol], field)
    ]
    assert not mismatches, f"{len(mismatches)} field mismatches, first: {mismatches[:3]}"


def test_overlap_session_has_a_plausible_symbol_count(universe):
    bars = _load("udiff_20240301.csv.zip", universe)
    assert 1000 < len(bars) < 4000


UDIFF_HEADER = ("TradDt,TckrSymb,SctySrs,FinInstrmTp,ISIN,OpnPric,HghPric,LwPric,"
                "ClsPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd")


def _udiff_csv(*rows):
    return "\n".join((UDIFF_HEADER, *rows)) + "\n"


def test_only_configured_series_and_instrument_types_survive(universe):
    text = _udiff_csv(
        "2024-03-01,GOODCO,EQ,STK,INE001A01001,10,11,9,10.5,10,100,1050,7",
        "2024-03-01,SMECO,SM,STK,INE002A01002,10,11,9,10.5,10,100,1050,7",
        "2024-03-01,GOLDBOND,GB,STK,IN0020200104,10,11,9,10.5,10,100,1050,7",
        "2024-03-01,SOMEREIT,EQ,INVIT,INE003A01003,10,11,9,10.5,10,100,1050,7",
    )
    assert [b.symbol for b in parse(text, "udiff", universe)] == ["GOODCO"]


def test_rights_entitlements_are_excluded(universe):
    text = _udiff_csv(
        "2024-03-01,REALCO,EQ,STK,INE001A01001,10,11,9,10.5,10,100,1050,7",
        "2024-03-01,ASTEC-RE,EQ,STK,INE002A01002,10,11,9,10.5,10,100,1050,7",
        "2024-03-01,SEPC-RE3,EQ,STK,INE003A01003,10,11,9,10.5,10,100,1050,7",
    )
    assert [b.symbol for b in parse(text, "udiff", universe)] == ["REALCO"]


def test_blank_numeric_fields_become_none_not_zero(universe):
    # A zero would silently poison an average; None is excluded from one.
    text = _udiff_csv("2024-03-01,THINCO,EQ,STK,INE001A01001,,,,10.5,10,0,,0")
    bar = parse(text, "udiff", universe)[0]
    assert bar.open is None and bar.high is None and bar.turnover is None
    assert bar.close == 10.5


def test_serving_the_wrong_session_is_rejected(universe):
    text = _udiff_csv("2024-03-04,GOODCO,EQ,STK,INE001A01001,10,11,9,10.5,10,100,1050,7")
    with pytest.raises(ParseError, match="wrong session"):
        parse(text, "udiff", universe, expected_date=dt.date(2024, 3, 1))


def test_a_dropped_column_is_a_diagnosable_error(universe):
    text = "TradDt,TckrSymb,SctySrs,FinInstrmTp,ClsPric\n2024-03-01,X,EQ,STK,10\n"
    with pytest.raises(ParseError, match="missing columns"):
        parse(text, "udiff", universe)


def test_empty_result_is_an_error_not_a_silent_zero(universe):
    # An all-filtered file means the format changed under us. Failing loudly here
    # is what stops a silently empty session reaching the screen.
    text = _udiff_csv("2024-03-01,SMECO,SM,STK,INE002A01002,10,11,9,10.5,10,100,1050,7")
    with pytest.raises(ParseError, match="no rows survived"):
        parse(text, "udiff", universe)


def test_legacy_uppercase_month_timestamp_parses(universe):
    text = ("SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,"
            "TIMESTAMP,TOTALTRADES,ISIN,\n"
            "GOODCO,EQ,30.4,30.95,30.35,30.75,30.9,30.4,76443,2349239.85,"
            "02-JAN-2015,367,INE144J01027,\n")
    bar = parse(text, "legacy", universe, expected_date=dt.date(2015, 1, 2))[0]
    assert bar.trade_date == dt.date(2015, 1, 2)
    assert bar.close == 30.75 and bar.volume == 76443


def test_legacy_two_digit_year_timestamp_parses(universe):
    # NSE published 2020-07-13 as "13-Jul-20" while every neighbouring session
    # used "13-JUL-2020". This crashed a full backfill mid-run.
    text = ("SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,"
            "TIMESTAMP,TOTALTRADES,ISIN,\n"
            "GOODCO,EQ,30.4,30.95,30.35,30.75,30.9,30.4,76443,2349239.85,"
            "13-Jul-20,367,INE144J01027,\n")
    bar = parse(text, "legacy", universe, expected_date=dt.date(2020, 7, 13))[0]
    assert bar.trade_date == dt.date(2020, 7, 13)


def test_an_unrecognised_date_format_is_a_parse_error_not_a_crash(universe):
    text = ("SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,"
            "TIMESTAMP,TOTALTRADES,ISIN,\n"
            "GOODCO,EQ,30.4,30.95,30.35,30.75,30.9,30.4,76443,2349239.85,"
            "2020/07/13,367,INE144J01027,\n")
    with pytest.raises(ParseError, match="TIMESTAMP"):
        parse(text, "legacy", universe)
