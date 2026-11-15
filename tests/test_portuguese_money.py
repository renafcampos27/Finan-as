from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QValidator
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui.core.widgets import EuroSpinBox, PortugueseMoneyValidator
from utils.helpers import eur, format_eur_number, parse_eur_input


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 €"),
        (100, "100 €"),
        (100.01, "100,01 €"),
        (12.9, "12,90 €"),
        (-33, "-33 €"),
        (-12.9, "-12,90 €"),
        (1_234.56, "1 234,56 €"),
    ],
)
def test_euro_display_uses_comma_and_omits_zero_cents(value, expected) -> None:
    assert eur(value) == expected


def test_euro_number_uses_financial_rounding() -> None:
    assert format_eur_number("1.005") == "1,01"
    assert format_eur_number("2.004") == "2"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("3,14", 3.14), ("100", 100.0), ("100,01 €", 100.01)],
)
def test_manual_euro_parser_accepts_only_portuguese_decimals(text, expected) -> None:
    assert parse_eur_input(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["3.14", "100.01", "1.234,56"])
def test_manual_euro_parser_rejects_decimal_points(text) -> None:
    with pytest.raises(ValueError, match="Usa vírgula"):
        parse_eur_input(text)


def test_euro_spinbox_displays_compact_cents_and_rejects_dot(qapp) -> None:
    spinbox = EuroSpinBox()
    spinbox.setRange(0, 1_000_000)
    spinbox.setSuffix(" €")

    spinbox.setValue(100)
    assert spinbox.cleanText() == "100"
    spinbox.setValue(100.01)
    assert spinbox.cleanText() == "100,01"
    assert spinbox.validate("3.14 €", 4)[0] == QValidator.State.Invalid
    assert spinbox.validate("3,14 €", 4)[0] == QValidator.State.Acceptable

    spinbox.lineEdit().selectAll()
    QTest.keyClicks(spinbox.lineEdit(), "3.14")
    spinbox.interpretText()
    assert spinbox.cleanText() == "3,14"
    assert spinbox.value() == pytest.approx(3.14)
    spinbox.close()


def test_money_table_validator_never_accepts_dot() -> None:
    validator = PortugueseMoneyValidator()
    assert validator.validate("3,14", 4)[0] == QValidator.State.Acceptable
    assert validator.validate("100", 3)[0] == QValidator.State.Acceptable
    assert validator.validate("3.14", 4)[0] == QValidator.State.Invalid
    assert validator.validate("1 000,00", 8)[0] == QValidator.State.Invalid


def test_pasted_dot_amount_is_not_inserted(qapp) -> None:
    spinbox = EuroSpinBox()
    spinbox.setRange(0, 1_000)
    spinbox.setValue(7)
    spinbox.lineEdit().selectAll()
    QApplication.clipboard().setText("3.14")
    QTest.keyClick(
        spinbox.lineEdit(), Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier
    )
    assert spinbox.cleanText() == "7"
    spinbox.close()
