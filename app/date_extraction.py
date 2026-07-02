"""Best-effort extraction of promotion validity dates from a HighCo Nifty
email's body text. The admin still reviews/corrects every value on the
pending-validation screen — this only saves typing in the common case.

Patterns observed in real HighCo Nifty mailings (2026-07):
- "cette offre sera valable du 15 octobre au 30 novembre 2025"
- "Valable jusqu'au 31/07/26"
- "jusqu'au 20 octobre 2025"
- "à partir du 01/07/2025"
"""

import re
from datetime import date
from typing import Optional, Tuple

_MONTHS_FR = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}
_MONTH_PATTERN = "|".join(_MONTHS_FR.keys())

_NUMERIC_DATE = r"\d{1,2}[/.]\d{1,2}[/.]\d{2,4}"

_TEXTUAL_RANGE = re.compile(
    r"du\s+(\d{1,2})(?:er)?\s*(?:(" + _MONTH_PATTERN + r")\s+)?(?:jusqu.au|au)\s+"
    r"(\d{1,2})(?:er)?\s+(" + _MONTH_PATTERN + r")\s+(\d{4})",
    re.IGNORECASE,
)
_NUMERIC_RANGE = re.compile(rf"du\s+({_NUMERIC_DATE})\s+(?:jusqu.au|au)\s+({_NUMERIC_DATE})", re.IGNORECASE)

_TEXTUAL_START = re.compile(
    r"(?:à|a)\s+partir\s+du\s+(\d{1,2})(?:er)?\s+(" + _MONTH_PATTERN + r")\s+(\d{4})", re.IGNORECASE
)
_NUMERIC_START = re.compile(rf"(?:à|a)\s+partir\s+du\s+({_NUMERIC_DATE})", re.IGNORECASE)

_TEXTUAL_END = re.compile(r"jusqu.au\s+(\d{1,2})(?:er)?\s+(" + _MONTH_PATTERN + r")\s+(\d{4})", re.IGNORECASE)
_NUMERIC_END = re.compile(rf"jusqu.au\s+({_NUMERIC_DATE})", re.IGNORECASE)


def _parse_numeric(raw: str) -> Optional[date]:
    parts = re.split(r"[/.]", raw)
    if len(parts) != 3:
        return None
    day, month, year = parts
    year_int = int(year)
    if year_int < 100:
        year_int += 2000
    try:
        return date(year_int, int(month), int(day))
    except ValueError:
        return None


def _parse_textual(day: str, month_word: str, year: str) -> Optional[date]:
    month = _MONTHS_FR.get(month_word.lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def extract_validity_dates(text: str) -> Tuple[Optional[date], Optional[date]]:
    if not text:
        return None, None

    match = _TEXTUAL_RANGE.search(text)
    if match:
        start_day, start_month_word, end_day, end_month_word, year = match.groups()
        end_date = _parse_textual(end_day, end_month_word, year)
        start_date = _parse_textual(start_day, start_month_word or end_month_word, year)
        if start_date or end_date:
            return start_date, end_date

    match = _NUMERIC_RANGE.search(text)
    if match:
        return _parse_numeric(match.group(1)), _parse_numeric(match.group(2))

    start_date = None
    match = _TEXTUAL_START.search(text)
    if match:
        start_date = _parse_textual(*match.groups())
    else:
        match = _NUMERIC_START.search(text)
        if match:
            start_date = _parse_numeric(match.group(1))

    end_date = None
    match = _TEXTUAL_END.search(text)
    if match:
        end_date = _parse_textual(*match.groups())
    else:
        match = _NUMERIC_END.search(text)
        if match:
            end_date = _parse_numeric(match.group(1))

    return start_date, end_date
