#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from utils.qt_runtime import configure_qt_runtime
from utils.version import APP_VERSION_LABEL

configure_qt_runtime()

try:
    from PySide6.QtCore import QDate, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

APP_TITLE = f"Finanças Pessoais — Liquid Glass · {APP_VERSION_LABEL}"


GREEN = "#30D158"
BLUE = "#0A84FF"
PURPLE = "#BF5AF2"
PINK = "#FF375F"
ORANGE = "#FF9F0A"
YELLOW = "#FFD60A"
TEAL = "#64D2FF"
MINT = "#63E6BE"
RED = "#FF453A"
MUTED = "#708093"
APP_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = APP_DIR / "assets"
ARROW_LIGHT = (ASSETS_DIR / "chevron_down_light.svg").as_posix()
ARROW_DARK = (ASSETS_DIR / "chevron_down_dark.svg").as_posix()
ARROW_UP_LIGHT = (ASSETS_DIR / "chevron_up_light.svg").as_posix()
ARROW_UP_DARK = (ASSETS_DIR / "chevron_up_dark.svg").as_posix()
ARROW_DOWN_LIGHT = (ASSETS_DIR / "chevron_down_light.svg").as_posix()
ARROW_DOWN_DARK = (ASSETS_DIR / "chevron_down_dark.svg").as_posix()
CHECKMARK_DARK = (ASSETS_DIR / "checkmark_dark.svg").as_posix()
CHECKMARK_LIGHT = (ASSETS_DIR / "checkmark_light.svg").as_posix()

PT_MONTHS = [
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
]


def format_eur_number(value: Any, *, grouping: bool = True) -> str:
    """Format a euro amount using Portuguese decimal notation.

    Values are rounded to cents with the usual financial half-up rule. Exact
    euro amounts omit the redundant decimal part (``100``); amounts with cents
    always keep both digits (``100,01`` or ``12,90``).
    """

    try:
        number = Decimal(str(value if value not in (None, "") else 0))
        if not number.is_finite():
            number = Decimal(0)
    except (InvalidOperation, TypeError, ValueError):
        number = Decimal(0)
    rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = rounded.copy_abs()
    decimals = 0 if rounded == rounded.to_integral_value() else 2
    raw = format(rounded, f",.{decimals}f" if grouping else f".{decimals}f")
    return raw.replace(",", "X").replace(".", ",").replace("X", " ")


def parse_eur_input(value: Any, *, field_name: str = "valor") -> float:
    """Parse a manually entered euro value and reject decimal points.

    Bank-statement imports deliberately use their own locale-aware parser;
    this stricter helper is only for values typed into the application.
    """

    original = str(value or "").strip()
    text = (
        original.replace("€", "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .strip()
    )
    if not text:
        raise ValueError(f"{field_name.capitalize()} em falta.")
    if "." in text:
        raise ValueError(
            f"{field_name.capitalize()} inválido. Usa vírgula para os cêntimos."
        )
    if re.fullmatch(r"[+-]?\d+(?:,\d{1,2})?", text) is None:
        raise ValueError(
            f"{field_name.capitalize()} inválido. Usa, por exemplo, 3,14 ou 100."
        )
    try:
        number = Decimal(text.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"{field_name.capitalize()} inválido.") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name.capitalize()} inválido.")
    return float(number)


def parse_dot_decimal(value: Any, *, field_name: str = "valor") -> float:
    """Parse a euro amount typed with a decimal point (``250.50``).

    BUG-DLG-09 companion helper: the assistant table can receive values pasted
    with dot decimals; ``parse_eur_input`` deliberately rejects them, so this
    fallback accepts the strict ``1234.56`` form (dot as decimal separator,
    no thousands separators) and still rejects ambiguous inputs like ``1.234``.
    Bare integers are accepted; ``1.234,56`` style remains rejected.
    """

    original = str(value or "").strip()
    text = (
        original.replace("€", "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .strip()
    )
    if not text:
        raise ValueError(f"{field_name.capitalize()} em falta.")
    if re.fullmatch(r"[+-]?\d+(?:\.\d{1,2})?", text) is None:
        raise ValueError(
            f"{field_name.capitalize()} inválido. Usa, por exemplo, 250.50 ou 250,50."
        )
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name.capitalize()} inválido.") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name.capitalize()} inválido.")
    return float(number)


def require_nonzero_eur(value: Any, *, field_name: str = "valor") -> float:
    """Return a finite cent-rounded amount and reject an effective zero.

    Imports and recurring-payment services can bypass the visual editor, so
    this validation deliberately lives at the persistence boundary too.
    Values below half a cent round to zero and are not valid movements.
    """

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name.capitalize()} inválido.") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name.capitalize()} inválido.")
    rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        raise ValueError(f"{field_name.capitalize()} tem de ser diferente de zero.")
    return float(rounded)


def require_eur_cent_precision(
    value: Any, *, field_name: str = "valor", allow_zero: bool = False
) -> float:
    """Accept only finite euro values with at most two decimals (non-zero unless allow_zero=True)."""

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name.capitalize()} inválido.") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name.capitalize()} inválido.")
    rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if number != rounded:
        raise ValueError(f"{field_name.capitalize()} tem mais de duas casas decimais.")
    if rounded == 0 and not allow_zero:
        raise ValueError(f"{field_name.capitalize()} tem de ser diferente de zero.")
    return float(rounded)


def eur(value: float, privacy: bool = False) -> str:
    if privacy:
        return "••••• €"
    return f"{format_eur_number(value)} €"


def percent(value: float, privacy: bool = False) -> str:
    if privacy:
        return "•••%"
    return f"{float(value or 0):.1f}".replace(".", ",") + "%"


def format_period_variation(
    current: float,
    previous: float,
    *,
    privacy: bool = False,
    huge_threshold_pct: float = 1_000.0,
) -> dict[str, Any]:
    """Format a period change without exaggerating a tiny comparison base.

    Very large percentages are mathematically valid but visually misleading.
    In those cases the UI presents the multiplier and the previous value, while
    retaining the exact percentage for details and accessibility.
    """

    current_value = safe_float(current)
    previous_value = safe_float(previous)
    delta = current_value - previous_value
    if abs(previous_value) < 0.005:
        display = "•••%" if privacy else "sem base anterior" if abs(current_value) >= 0.005 else "0,0%"
        return {
            "display": display,
            "percentage": None,
            "multiplier": None,
            "huge": abs(current_value) >= 0.005,
            "previous_text": eur(previous_value, privacy),
            "context": f"anterior: {eur(previous_value, privacy)}",
            "explanation": (
                "Não existe uma base anterior diferente de zero; uma percentagem "
                "não seria representativa."
            ),
            "delta": delta,
        }

    change_pct = delta / abs(previous_value) * 100.0
    huge = abs(change_pct) >= max(100.0, float(huge_threshold_pct))
    same_direction = current_value == 0 or current_value * previous_value >= 0
    multiplier = abs(current_value / previous_value) if same_direction else None
    if huge and multiplier is not None:
        prefix = "-" if current_value < 0 < previous_value else ""
        display = prefix + f"{multiplier:.1f}".replace(".", ",") + "×"
    elif huge:
        display = "mudou de sinal"
    else:
        display = f"{change_pct:+.1f}%".replace(".", ",")
    if privacy:
        display = "•••%"
    context = f"anterior: {eur(previous_value, privacy)}"
    explanation = (
        "A base anterior era muito pequena; o multiplicador e o valor anterior "
        "descrevem melhor a variação do que a percentagem isolada."
        if huge
        else "Variação calculada face ao valor do período de referência."
    )
    return {
        "display": display,
        "percentage": change_pct,
        "multiplier": multiplier,
        "huge": huge,
        "previous_text": eur(previous_value, privacy),
        "context": context,
        "explanation": explanation,
        "delta": delta,
    }


def iso(day: date) -> str:
    return day.isoformat()


def format_iso_date(value: Any, fallback: str = "—") -> str:
    """Return an ISO date in the app's Portuguese day/month/year format."""

    try:
        s = str(value or "").strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            y, m, d = int(s[:4]), int(s[5:7]), int(s[8:10])
            date(y, m, d)
            return f"{d:02d}/{m:02d}/{y:04d}"
        return date.fromisoformat(s[:10]).strftime("%d/%m/%Y")
    except (TypeError, ValueError, IndexError):
        return str(fallback)


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert a value to a finite float without propagating invalid input.

    ``NaN`` and infinite values are treated as invalid instead of silently
    entering totals and charts.
    """
    try:
        if value is None or value == "":
            return float(default)
        if isinstance(value, str):
            text = value.strip()
            if "," in text and "." in text:
                if text.rfind(",") > text.rfind("."):
                    text = text.replace(".", "").replace(",", ".")
                else:
                    text = text.replace(",", "")
            elif "," in text:
                text = text.replace(",", ".")
            if text.startswith("(") and text.endswith(")"):
                text = "-" + text[1:-1]
            text = (
                text.replace("€", "")
                .replace("EUR", "")
                .replace("\u00a0", "")
                .replace("\u202f", "")
                .replace(" ", "")
                .strip()
            )
            number = float(text)
        else:
            number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError, OverflowError):
        return float(default)


def strict_float(value: Any, *, field_name: str = "valor") -> float:
    """Return a finite float or raise a user-readable validation error."""
    if value is None or str(value).strip() == "":
        raise ValueError(f"{field_name.capitalize()} em falta.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name.capitalize()} inválido.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name.capitalize()} não pode ser NaN ou infinito.")
    return number


def parse_month_key(value: Any, *, field_name: str = "mês") -> str:
    """Validate and normalize a calendar month in the strict ``AAAA-MM`` form."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match is None:
        raise ValueError(f"{field_name.capitalize()} inválido. Usa o formato AAAA-MM.")
    year, month = (int(part) for part in match.groups())
    try:
        date(year, month, 1)
    except ValueError as exc:
        raise ValueError(
            f"{field_name.capitalize()} inválido. Usa um mês entre 01 e 12."
        ) from exc
    return f"{year:04d}-{month:02d}"


def require_iso_date(value: Any, *, field_name: str = "data") -> str:
    """Validate a complete calendar date in the strict ``AAAA-MM-DD`` form."""

    text = str(value or "").strip()
    if (
        len(text) != 10
        or text[4] != "-"
        or text[7] != "-"
        or not (text[:4].isdigit() and text[5:7].isdigit() and text[8:10].isdigit())
    ):
        raise ValueError(
            f"{field_name.capitalize()} inválida. Usa o formato AAAA-MM-DD."
        )
    try:
        y, m, d = int(text[:4]), int(text[5:7]), int(text[8:10])
        date(y, m, d)
    except ValueError as exc:
        raise ValueError(f"{field_name.capitalize()} inválida.") from exc
    return f"{y:04d}-{m:02d}-{d:02d}"


def row_get(row: Any, key: str, default: Any = "") -> Any:
    try:
        return row[key]
    except Exception:
        try:
            return row.get(key, default)
        except Exception:
            return default


def today_iso() -> str:
    return date.today().isoformat()


def backup_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


BACKUP_INTERVAL_OPTIONS = [
    ("Desativado", 0),
    ("15 minutos", 15),
    ("30 minutos", 30),
    ("1 hora", 60),
    ("2 horas", 120),
    ("3 horas", 180),
    ("12 horas", 720),
    ("24 horas", 1440),
    ("2 dias", 2880),
    ("3 dias", 4320),
]


def backup_interval_label(minutes: int) -> str:
    try:
        minutes = int(minutes)
    except Exception:
        minutes = 0
    for label, value in BACKUP_INTERVAL_OPTIONS:
        if value == minutes:
            return label
    if minutes <= 0:
        return "Desativado"
    if minutes < 60:
        return f"{minutes} minutos"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "1 dia" if days == 1 else f"{days} dias"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hora" if hours == 1 else f"{hours} horas"
    return f"{minutes} minutos"


def parse_backup_datetime(value: str) -> Optional[datetime]:
    value = str(value or "").strip()
    if not value:
        return None
    if (
        len(value) >= 19
        and value[4] == "-"
        and value[7] == "-"
        and value[10] in ("T", " ")
        and value[:4].isdigit()
        and value[5:7].isdigit()
        and value[8:10].isdigit()
        and value[11:13].isdigit()
        and value[14:16].isdigit()
        and value[17:19].isdigit()
    ):
        try:
            return datetime(
                int(value[:4]),
                int(value[5:7]),
                int(value[8:10]),
                int(value[11:13]),
                int(value[14:16]),
                int(value[17:19]),
            )
        except (ValueError, IndexError):
            pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(
                value[:19] if "T" in value or " " in value else value, fmt
            )
        except (TypeError, ValueError):
            continue
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


def add_months(base: date, months: int, preferred_day: Optional[int] = None) -> date:
    y = base.year + (base.month - 1 + months) // 12
    m = (base.month - 1 + months) % 12 + 1
    d = preferred_day if preferred_day is not None else base.day
    return date(y, m, min(max(1, int(d)), days_in_month(y, m)))


def rolling_month_window(reference: date, months: int) -> tuple[date, date, int]:
    """Return an inclusive rolling window measured in real calendar months."""
    count = max(1, int(months or 1))
    end = reference
    start = add_months(end, -count) + timedelta(days=1)
    return start, end, (end - start).days + 1


def parse_flexible_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None

    # Strip time component if timestamp contains ':'
    if " " in text and ":" in text:
        text = text.split(" ")[0].strip()
    elif "T" in text and ":" in text:
        text = text.split("T")[0].strip()

    # Remove Portuguese connectors like "15 de Janeiro de 2026"
    if re.search(r"\bde\b", text, re.IGNORECASE):
        text = re.sub(r"\bde\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()

    # Handle Portuguese month names (e.g. 24-Ago-2026, 24/Jan/2026)
    pt_months = {
        "janeiro": "01",
        "jan": "01",
        "fevereiro": "02",
        "fev": "02",
        "março": "03",
        "marco": "03",
        "mar": "03",
        "abril": "04",
        "abr": "04",
        "maio": "05",
        "mai": "05",
        "junho": "06",
        "jun": "06",
        "julho": "07",
        "jul": "07",
        "agosto": "08",
        "ago": "08",
        "setembro": "09",
        "set": "09",
        "outubro": "10",
        "out": "10",
        "novembro": "11",
        "nov": "11",
        "dezembro": "12",
        "dez": "12",
    }
    lowered = text.casefold()
    for month_name, month_num in pt_months.items():
        if month_name in lowered:
            pattern = re.compile(rf"\b{re.escape(month_name)}\b", re.IGNORECASE)
            text = pattern.sub(month_num, text)
            break

    # Compact 8-digit date YYYYMMDD
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        except ValueError:
            pass

    # BNK-PAR-04: Compact 6-digit date YYMMDD or DDMMYY
    if len(text) == 6 and text.isdigit():
        yy, mm, dd = int(text[:2]), int(text[2:4]), int(text[4:])
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            try:
                return date(2000 + yy, mm, dd)
            except ValueError:
                pass
        dd_alt, mm_alt, yy_alt = int(text[:2]), int(text[2:4]), int(text[4:])
        if 1 <= mm_alt <= 12 and 1 <= dd_alt <= 31:
            try:
                return date(2000 + yy_alt, mm_alt, dd_alt)
            except ValueError:
                pass

    if len(text) == 10:
        c4, c7 = text[4], text[7]
        c2, c5 = text[2], text[5]
        if (
            c4 == "-"
            and c7 == "-"
            and text[:4].isdigit()
            and text[5:7].isdigit()
            and text[8:].isdigit()
        ):
            try:
                return date(int(text[:4]), int(text[5:7]), int(text[8:]))
            except ValueError:
                return None
        if (
            c2 == "/"
            and c5 == "/"
            and text[:2].isdigit()
            and text[3:5].isdigit()
            and text[6:].isdigit()
        ):
            try:
                return date(int(text[6:]), int(text[3:5]), int(text[:2]))
            except ValueError:
                return None
        if (
            c2 == "-"
            and c5 == "-"
            and text[:2].isdigit()
            and text[3:5].isdigit()
            and text[6:].isdigit()
        ):
            try:
                return date(int(text[6:]), int(text[3:5]), int(text[:2]))
            except ValueError:
                return None
        if (
            c4 == "/"
            and c7 == "/"
            and text[:4].isdigit()
            and text[5:7].isdigit()
            and text[8:].isdigit()
        ):
            try:
                return date(int(text[:4]), int(text[5:7]), int(text[8:]))
            except ValueError:
                return None
        if (
            c2 == "."
            and c5 == "."
            and text[:2].isdigit()
            and text[3:5].isdigit()
            and text[6:].isdigit()
        ):
            try:
                return date(int(text[6:]), int(text[3:5]), int(text[:2]))
            except ValueError:
                return None

    if len(text) == 8:
        c2, c5 = text[2], text[5]
        if (
            c2 in ("/", "-", ".")
            and c5 == c2
            and text[:2].isdigit()
            and text[3:5].isdigit()
            and text[6:].isdigit()
        ):
            sep = c2
            fmt = f"%d{sep}%m{sep}%y"
            try:
                res = datetime.strptime(text, fmt).date()
                if res.year < 2000:
                    res = res.replace(year=res.year + 100)
                return res
            except (TypeError, ValueError):
                pass

    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%d.%m.%Y",
        "%d %m %Y",
        "%d %b %Y",
        "%d-%b-%Y",
        "%d/%b/%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%d.%m.%y",
        "%y-%m-%d",
        "%y/%m/%d",
        "%d %m %y",
        "%d %b %y",
        "%d-%b-%y",
        "%d/%b/%y",
    ):

        try:
            res = datetime.strptime(text, fmt).date()
            if "%y" in fmt and res.year < 2000:
                res = res.replace(year=res.year + 100)
            return res
        except (TypeError, ValueError):
            continue
    try:
        return datetime.fromisoformat(text).date()
    except (TypeError, ValueError):
        return None


# BNK-PAR-04: Backwards-compatible alias for universal date parsing
parse_date = parse_flexible_date


_RESERVED_WIN32_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    "COM0",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT0",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WIN32_PATTERN = re.compile(
    r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9]|LPT[0-9])(\..*|\s+.*)?$", re.IGNORECASE
)


def sanitize_filename(name: str, max_length: int = 120) -> str:
    """Sanitize filename against NTFS Alternate Data Streams, reserved DOS device names, and traversal."""
    cleaned = str(name or "").strip()
    cleaned = _INVALID_FILENAME_CHARS.sub("_", cleaned)
    cleaned = re.sub(r"\.\.+", "_", cleaned)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        cleaned = "ficheiro"

    # DOC-SEC-02: Check reserved Win32 device names with regex and whitespace tolerance
    base = cleaned.split(".")[0].strip(" .").upper()
    if base in _RESERVED_WIN32_NAMES or _RESERVED_WIN32_PATTERN.match(cleaned):
        cleaned = f"safe_{cleaned}"

    # DOC-SEC-01: Truncation must not reintroduce trailing spaces or dots on Windows
    if len(cleaned) > max_length:
        parts = cleaned.rsplit(".", 1)
        if len(parts) == 2 and len(parts[1]) <= 10:
            ext = f".{parts[1]}"
            stem = parts[0][: max_length - len(ext)].rstrip(". ")
            if not stem:
                stem = "ficheiro"
            cleaned = f"{stem}{ext}"
        else:
            cleaned = cleaned[:max_length].rstrip(". ")
            if not cleaned:
                cleaned = "ficheiro"
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        cleaned = "ficheiro"
    return cleaned


def safe_year_from_combo(combo: Any, default: Optional[int] = None) -> int:
    """BUG-COMBOBOX-CRASH-001: Safely extract a 4-digit year from a QComboBox or string."""
    fallback = default if default is not None else date.today().year
    if combo is None:
        return fallback
    try:
        raw = (
            combo.currentText()
            if hasattr(combo, "currentText") and callable(combo.currentText)
            else str(combo or "")
        ).strip()
    except Exception:
        return fallback
    match = re.search(r"\b(19\d\d|20\d\d)\b", raw) or re.search(r"\d{4}", raw)
    if match:
        try:
            return int(match.group(0))
        except (ValueError, TypeError):
            pass
    return fallback


def mask_secret(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * max(4, len(value) - 8) + value[-4:]


def safe_json_dumps(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(data)


def strip_json_fences(raw: str) -> str:
    """Strip markdown code fences from a model reply (BUG-AI-06).

    Gemini sometimes wraps JSON in ```json ... ``` even when
    responseMimeType=application/json is requested, which breaks json.loads.
    """
    text = str(raw or "").strip()
    fence = re.compile(
        r"^```[a-zA-Z0-9_-]*\s*\n(?P<body>.*?)\n?```\s*$", re.DOTALL
    )
    match = fence.match(text)
    if match:
        return match.group("body").strip()
    return text


def to_qdate(day: date) -> QDate:
    return QDate(day.year, day.month, day.day)


def open_path(path: Path) -> bool:
    """Open a local path through Qt without constructing a shell command."""

    try:
        candidate = Path(path).expanduser().resolve()
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(candidate)))
        if not opened:
            raise OSError("o sistema não aceitou o pedido de abertura")
        return True
    except Exception as exc:
        QMessageBox.warning(
            None, APP_TITLE, f"Não foi possível abrir:\n{path}\n\n{exc}"
        )
        return False


def start_end_for_period(period: str) -> tuple[str, str]:
    today = date.today()
    if period == "Hoje":
        return iso(today), iso(today)
    if period == "Ontem":
        d = today - timedelta(days=1)
        return iso(d), iso(d)
    if period == "Semana atual":
        start = today - timedelta(days=today.weekday())
        return iso(start), iso(today)
    if period == "Últimos 7 dias":
        return iso(today - timedelta(days=6)), iso(today)
    if period == "Últimos 30 dias":
        return iso(today - timedelta(days=29)), iso(today)
    if period == "Mês atual":
        start = today.replace(day=1)
        return iso(start), iso(today)
    if period == "Mês anterior":
        first = today.replace(day=1)
        end = first - timedelta(days=1)
        start = end.replace(day=1)
        return iso(start), iso(end)
    if period == "Ano atual":
        return iso(today.replace(month=1, day=1)), iso(today)
    return "", ""


def chunked_iterable(iterable: Any, chunk_size: int = 400) -> Any:
    """Yield successive chunks of size chunk_size from an iterable, protecting against SQLite parameter limits."""
    chunk = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= max(1, int(chunk_size)):
            yield chunk
            chunk = []
    if chunk:
        yield chunk

