"""Date interpretation for bank batches without clock-dependent heuristics."""

from __future__ import annotations

import re
from datetime import date, datetime

from utils.helpers import parse_flexible_date


DATE_FORMATS = (
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d/%m/%y",
    "%m/%d/%y",
    "%Y%m%d",
    "%d%m%Y",
    "%m%d%Y",
    "%y%m%d",
    "%d%m%y",
    "%m%d%y",
)


def _date_text(value: str) -> str:
    text = str(value or "").strip()
    return re.split(r"[ T](?=\d{1,2}:\d{2})", text, maxsplit=1)[0]


def _explicit(text: str, fmt: str) -> date | None:
    if fmt not in DATE_FORMATS:
        return None
    # Two-digit years are a fixed 2000-2099 bank-file convention.
    text = _date_text(text)
    normalized = re.sub(r"[-.' ]", "/", text) if "/" in fmt else text
    pattern = re.escape(fmt).replace("%Y", r"\d{4}").replace("%y", r"\d{2}")
    pattern = pattern.replace("%m", r"\d{1,2}" if "/" in fmt else r"\d{2}")
    pattern = pattern.replace("%d", r"\d{1,2}" if "/" in fmt else r"\d{2}")
    if not re.fullmatch(pattern, normalized):
        return None
    try:
        parsed = datetime.strptime(normalized, fmt).date()
        return parsed.replace(year=2000 + parsed.year % 100) if "%y" in fmt else parsed
    except ValueError:
        return None


def import_date_candidates(value: str) -> dict[str, date]:
    text = _date_text(value)
    if re.fullmatch(r"\d{6}", text):
        formats = ("%y%m%d", "%d%m%y", "%m%d%y")
    elif re.fullmatch(r"\d{8}", text):
        formats = ("%Y%m%d", "%d%m%Y", "%m%d%Y")
    elif re.fullmatch(r"\d{1,2}[-/.' ]\d{1,2}[-/.' ]\d{2,4}", text):
        formats = ("%d/%m/%Y", "%m/%d/%Y", "%d/%m/%y", "%m/%d/%y")
    else:
        return {}
    return {
        fmt: result for fmt in formats if (result := _explicit(text, fmt)) is not None
    }


def parse_import_date(value: str, date_format: str = "") -> tuple[date | None, bool]:
    """Return (date, ambiguity); explicit numeric formats fail rather than guess."""
    text = _date_text(value)
    # ISO dates/timestamps have an intrinsic order even in a mixed batch.
    if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:$|[ T])", text):
        return parse_flexible_date(text), False
    candidates = import_date_candidates(text)
    if date_format:
        # A declared numeric format controls all numeric cells in that batch.
        if candidates or re.fullmatch(r"[\d/.'-]+", text):
            return _explicit(text, date_format), False
    if candidates:
        unique = set(candidates.values())
        return (next(iter(unique)), False) if len(unique) == 1 else (None, True)
    return parse_flexible_date(text), False


def detect_import_date_format(values) -> str:
    """Infer only when the whole numeric column has one possible ordering."""
    compatible: set[str] | None = None
    found = False
    for value in values:
        candidates = import_date_candidates(str(value or ""))
        if not candidates:
            continue
        found = True
        compatible = (
            set(candidates) if compatible is None else compatible & set(candidates)
        )
    return (
        next(iter(compatible)) if found and compatible and len(compatible) == 1 else ""
    )
