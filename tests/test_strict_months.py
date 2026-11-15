from __future__ import annotations

import pytest

from services.financial_health import _month_key
from utils.helpers import parse_month_key


@pytest.mark.parametrize("value", ["2026-07", "0001-01", "9999-12"])
def test_parse_month_key_accepts_only_real_calendar_months(value):
    assert parse_month_key(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "2026-7",
        "26-07",
        "2026-00",
        "2026-13",
        "2026-07-01",
        "texto",
    ],
)
def test_parse_month_key_rejects_missing_or_invalid_values(value):
    with pytest.raises(ValueError, match="inválido"):
        parse_month_key(value)


def test_financial_health_never_clamps_or_replaces_an_invalid_month():
    assert _month_key("2026-07") == "2026-07"
    with pytest.raises(ValueError):
        _month_key("2026-99")
