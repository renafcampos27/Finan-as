from __future__ import annotations

from datetime import date, datetime

import pytest

from utils.helpers import (
    format_iso_date,
    parse_backup_datetime,
    parse_flexible_date,
    require_iso_date,
)


def test_format_iso_date_fast_path() -> None:
    assert format_iso_date("2026-08-23") == "23/08/2026"
    assert format_iso_date("2024-02-29") == "29/02/2024"  # Leap year
    assert format_iso_date("invalid-date", fallback="—") == "—"
    assert format_iso_date("", fallback="N/A") == "N/A"
    assert format_iso_date(None, fallback="N/A") == "N/A"


def test_require_iso_date_validation() -> None:
    assert require_iso_date("2026-08-23") == "2026-08-23"
    assert require_iso_date("2024-02-29") == "2024-02-29"

    with pytest.raises(ValueError, match="inválida"):
        require_iso_date("2026-02-29")  # 2026 is not a leap year

    with pytest.raises(ValueError, match="inválida"):
        require_iso_date("23/08/2026")

    with pytest.raises(ValueError, match="inválida"):
        require_iso_date("")


def test_parse_flexible_date_all_supported_formats() -> None:
    expected = date(2026, 8, 23)

    # 1. ISO YYYY-MM-DD
    assert parse_flexible_date("2026-08-23") == expected
    # 2. Portuguese DD/MM/YYYY
    assert parse_flexible_date("23/08/2026") == expected
    # 3. Dash DD-MM-YYYY
    assert parse_flexible_date("23-08-2026") == expected
    # 4. Slash YYYY/MM/DD
    assert parse_flexible_date("2026/08/23") == expected
    # 5. Dot DD.MM.YYYY
    assert parse_flexible_date("23.08.2026") == expected

    # Edge cases
    assert parse_flexible_date("") is None
    assert parse_flexible_date(None) is None
    assert parse_flexible_date("invalid-date-string") is None
    assert parse_flexible_date("31/02/2026") is None  # Invalid calendar day


def test_parse_backup_datetime_timestamp_formats() -> None:
    expected = datetime(2026, 8, 23, 15, 30, 45)

    assert parse_backup_datetime("2026-08-23T15:30:45") == expected
    assert parse_backup_datetime("2026-08-23 15:30:45") == expected
    assert parse_backup_datetime("2026-08-23") == datetime(2026, 8, 23, 0, 0, 0)
    assert parse_backup_datetime("") is None
    assert parse_backup_datetime(None) is None
