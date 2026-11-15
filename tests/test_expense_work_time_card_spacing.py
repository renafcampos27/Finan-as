from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget

from ui.components.expense_work_time import ExpenseWorkTimeCard
from ui.core.theme import Theme


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_expense_work_time_card_compact_spacing_and_joined_sentences(qapp) -> None:
    theme = Theme(dark=False)
    win = QWidget()
    win.resize(1100, 650)
    card = ExpenseWorkTimeCard(lambda: theme, win)
    win.show()
    qapp.processEvents()

    # 1. Base intro text
    assert "Quanto tempo do teu trabalho líquido" in card.intro.text()
    assert "·" not in card.intro.text()
    assert card.v.spacing() <= 4

    # 2. Join comparison sentence
    comparison_text = "↔ Comparação com período anterior: sem despesas de referência."
    card.comparison_hint.setText(comparison_text)
    card.comparison_hint.setVisible(True)
    qapp.processEvents()

    assert card.comparison_hint.isVisible() is True
    assert card.comparison_hint.text() == comparison_text
    assert comparison_text in card.intro.text()
    assert " · " in card.intro.text()
    assert card.intro.text().startswith("Quanto tempo do teu trabalho líquido")

    # 3. Clearing / hiding comparison
    card.comparison_hint.setVisible(False)
    qapp.processEvents()
    assert card.comparison_hint.isVisible() is False
    assert comparison_text not in card.intro.text()
    assert " · " not in card.intro.text()

    # 4. Vertical layout compactness under expansion
    card.comparison_hint.setVisible(True)
    win.resize(1100, 750)
    card.resize(1100, 700)
    qapp.processEvents()

    intro_geom = card.intro.geometry()
    title_geom = card.title_label.geometry()
    metrics_geom = card.metrics.geometry()

    # Gap between title bottom and intro top must be <= 10px
    title_bottom = title_geom.y() + title_geom.height()
    gap_title_intro = intro_geom.y() - title_bottom
    assert 0 <= gap_title_intro <= 10

    # Gap between intro bottom and metrics top must be <= 15px (no 80px dead space)
    intro_bottom = intro_geom.y() + intro_geom.height()
    gap_intro_metrics = metrics_geom.y() - intro_bottom
    assert 0 <= gap_intro_metrics <= 15

    # 5. Metric frames have Fixed vertical size policy to prevent stretching
    for key in ("amount", "hours", "days"):
        frame, _val, _det = card.metric_values[key]
        assert frame.sizePolicy().verticalPolicy() == QSizePolicy.Fixed

    # 6. Bottom spacer absorbs extra space
    last_item = card.v.itemAt(card.v.count() - 1)
    assert last_item.spacerItem() is not None

    win.close()
