from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QWidget

from ui.core.theme import Theme
from ui.dialogs import BudgetBucketDialog


class _BudgetBucketRepo:
    def category_rows(self, kind: str):
        assert kind == "expense"
        buckets = ("Necessidades", "Desejos", "Poupança")
        return [
            {
                "name": f"Categoria {index + 1}",
                "budget_bucket": buckets[index % len(buckets)],
            }
            for index in range(14)
        ]


@pytest.mark.parametrize("dark", [False, True])
def test_budget_bucket_dialog_is_compact_visible_and_warning_free(dark) -> None:
    qapp = QApplication.instance() or QApplication([])
    host = QWidget()
    host.theme = Theme(dark=dark)
    host.setStyleSheet(host.theme.stylesheet())
    host.resize(960, 640)

    # Reproduce the original call from a tall page inside the main window.
    page = QWidget(host)
    page.resize(900, 2_000)
    host.show()
    page.show()

    qt_messages: list[str] = []

    def message_handler(_mode, _context, message) -> None:
        qt_messages.append(str(message))

    previous_handler = qInstallMessageHandler(message_handler)
    dialog = BudgetBucketDialog(_BudgetBucketRepo(), page)
    try:
        dialog.show()
        QTest.qWait(500)

        assert not any(
            "Attempting to add QLayout" in message for message in qt_messages
        )
        assert dialog.parentWidget() is host
        assert dialog.size() == host.size()
        assert dialog.sheet_layout.count() == 1
        assert dialog.content_layout.count() == 5
        assert 600 <= dialog.sheet.width() <= 720
        assert 480 <= dialog.sheet.height() <= 600
        assert dialog.sheet.width() < dialog.width()
        assert dialog.sheet.height() < dialog.height()
        assert dialog.rect().contains(dialog.sheet.geometry())

        assert dialog.heading_label.isVisibleTo(dialog)
        assert dialog.intro_label.isVisibleTo(dialog)
        assert dialog.bucket_help_label.isVisibleTo(dialog)
        assert dialog.table.isVisibleTo(dialog)
        assert dialog.table.rowCount() == 14
        assert dialog.table.verticalScrollBar().maximum() > 0

        first_selector = dialog._selectors[0][1]
        assert first_selector.isVisibleTo(dialog)
        assert first_selector.height() <= dialog.table.rowHeight(0)
        assert first_selector.accessibleName() == "Grupo 50/30/20 para Categoria 1"
        selector_image = first_selector.grab().toImage()
        assert not selector_image.isNull()
        field_surface = selector_image.pixelColor(
            8,
            max(1, selector_image.height() // 2),
        )
        assert field_surface.lightness() >= 180

        save_button = dialog.buttons.button(QDialogButtonBox.Save)
        cancel_button = dialog.buttons.button(QDialogButtonBox.Cancel)
        assert save_button.text() == "Guardar"
        assert save_button.objectName() == "Primary"
        assert cancel_button.text() == "Cancelar"
        assert cancel_button.objectName() == "Secondary"

        first_selector.setCurrentText("Poupança")
        assert dialog.values()[0] == ("Categoria 1", "Poupança")
        assert not dialog.grab().isNull()
    finally:
        dialog.close()
        host.close()
        qapp.processEvents()
        qInstallMessageHandler(previous_handler)
