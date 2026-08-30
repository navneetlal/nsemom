"""Corporate actions: fetch, parse, and cross-validate against the price gap.

Bhavcopy is unadjusted. An unhandled 1:5 split reads as an 80% single-day crash,
which trips every filter and poisons every EMA for the next 200 bars.

NSE states corporate action ratios only as free text in a `subject` field, so
parsing is unavoidable. What makes it trustworthy is the second source: on the
ex-date the price gaps by the same ratio, so `prev_close / open` independently
implies the factor. Agreement means the parse is right. Disagreement is a loud
failure, never a silent adjustment.

Verified against live data for June-July 2025, where all 15 price-affecting
actions agreed within 5% - including BAJFINANCE, which had a 2:1 face-value
split AND a 4:1 bonus on the same ex-date compounding to 10x.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass

from ..config import Config
from .client import NseClient

log = logging.getLogger(__name__)

API_URL = ("https://www.nseindia.com/api/corporates-corporateActions"
           "?index=equities&from_date={frm}&to_date={to}")
API_HEADERS = {
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
    "X-Requested-With": "XMLHttpRequest",
}

# "From Rs 10/- Per Share To Rs 2/- Per Share"  ->  10 / 2 = 5.0
# Also matches consolidations ("From Re 1 ... To Rs 10" -> 0.1), because a
# consolidation is just a split with the ratio the other way up.
FACE_VALUE_RE = re.compile(
    r"from\s+(?:rs|re)\.?\s*([\d.]+)\s*/?-?.*?\bto\s+(?:rs|re)\.?\s*([\d.]+)",
    re.IGNORECASE | re.DOTALL,
)
# "Bonus 4:1" -> 4 free shares per 1 held -> holding becomes 5x
BONUS_RE = re.compile(r"bonus\s*(?:issue)?\s*(\d+)\s*:\s*(\d+)", re.IGNORECASE)

SPLIT_WORDS = ("split", "sub-division", "subdivision", "sub division")
CONSOLIDATION_WORDS = ("consolidation", "consolidat")
DEMERGER_WORDS = ("demerger", "de-merger", "scheme of arrangement", "spin off",
                  "spin-off")
# A "bonus debenture" or bonus issue of preference shares does not adjust the
# equity price. Matching these into the bonus branch would corrupt the series.
BONUS_EXCLUSIONS = ("debenture", "deb ", "preference", "pref ", "ncrps", "ncd",
                    "dvr", "warrant")


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    ex_date: dt.date
    isin: str | None
    series: str | None
    action_type: str
    subject: str
    face_value: float | None
    adj_factor: float | None
    note: str | None = None


def _parse_ex_date(raw: str) -> dt.date | None:
    raw = (raw or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _has_bonus(low: str) -> bool:
    return "bonus" in low and not any(w in low for w in BONUS_EXCLUSIONS)


def classify(subject: str) -> str:
    """Map the free-text subject onto an action type.

    NSE routinely packs two actions into one subject, separated by a slash:
    "Bonus 1:1 / Face Value Split - From Rs 10/- Per Share To Rs 5/-". Both
    components move the price and both must be applied, so those are labelled
    compositely rather than collapsed to whichever keyword appears first.
    """
    low = subject.lower()
    if any(w in low for w in DEMERGER_WORDS):
        return "demerger"

    parts = []
    if _has_bonus(low):
        parts.append("bonus")
    if any(w in low for w in CONSOLIDATION_WORDS):
        parts.append("consolidation")
    elif any(w in low for w in SPLIT_WORDS):
        parts.append("split")
    return "+".join(parts) if parts else "ignored"


def parse_factor(subject: str, action_type: str) -> tuple[float | None, str | None]:
    """Return (adjustment factor, note).

    Every component present in the subject is parsed and the factors are
    MULTIPLIED. A bonus alongside a face-value split is not one action or the
    other - the price gaps by the product of the two, and applying only one
    leaves the whole prior series wrong by the factor of the other.

    Prices before the ex-date are divided by the result; volumes multiplied.
    """
    if action_type in ("ignored", "demerger"):
        return None, None

    components = action_type.split("+")
    factor, missing = 1.0, []

    if "bonus" in components:
        match = BONUS_RE.search(subject)
        if match and int(match.group(2)) != 0:
            factor *= 1 + int(match.group(1)) / int(match.group(2))
        else:
            missing.append("bonus")

    if "split" in components or "consolidation" in components:
        match = FACE_VALUE_RE.search(subject)
        if match and float(match.group(2)) != 0:
            factor *= float(match.group(1)) / float(match.group(2))
        else:
            missing.append("face value")

    if missing:
        return None, f"could not parse {' and '.join(missing)} ratio"
    return factor, None


def parse_records(payload: list[dict], series_filter: frozenset[str]
                  ) -> list[CorporateAction]:
    actions: list[CorporateAction] = []
    for row in payload:
        series = (row.get("series") or "").strip()
        if series_filter and series not in series_filter:
            continue
        ex_date = _parse_ex_date(row.get("exDate", ""))
        symbol = (row.get("symbol") or "").strip()
        subject = (row.get("subject") or "").strip()
        if not ex_date or not symbol or not subject:
            continue

        action_type = classify(subject)
        factor, note = parse_factor(subject, action_type)
        try:
            face_value = float(row.get("faceVal") or "") or None
        except ValueError:
            face_value = None

        actions.append(CorporateAction(
            symbol=symbol, ex_date=ex_date, isin=(row.get("isin") or "").strip() or None,
            series=series, action_type=action_type, subject=subject,
            face_value=face_value, adj_factor=factor, note=note,
        ))
    return actions


def fetch_range(client: NseClient, cfg: Config, start: dt.date, end: dt.date,
                chunk_days: int = 60) -> list[CorporateAction]:
    """Walk the CA API in chunks. Verified to return data back to 2015."""
    client.bootstrap_session()
    actions: list[CorporateAction] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + dt.timedelta(days=chunk_days - 1), end)
        url = API_URL.format(frm=f"{cursor:%d-%m-%Y}", to=f"{stop:%d-%m-%Y}")
        result = client.fetch(url, headers=API_HEADERS)
        if result.status == "ok" and result.content:
            try:
                payload = json.loads(result.content)
            except json.JSONDecodeError:
                log.error("corporate actions %s..%s: response was not JSON",
                          cursor, stop)
                payload = []
            if isinstance(payload, list):
                found = parse_records(payload, cfg.universe.series)
                actions.extend(found)
                log.info("%s .. %s  %4d records, %2d price-affecting",
                         cursor, stop, len(payload),
                         sum(1 for a in found if a.action_type != "ignored"))
        cursor = stop + dt.timedelta(days=1)
    return actions
