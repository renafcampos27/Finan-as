from __future__ import annotations

from pathlib import Path

import pytest
from shiboken6 import isValid

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QHelpEvent, QImage, QColor, QFocusEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from scripts.validate_visual_rendering import _perceptual_hash
from ui.core.accessibility import AccessibilityManager
from ui.core.tooltips import ReadableToolTipManager
from database.repo import Repo
from ui.dialogs import TransactionDialog


@pytest.mark.parametrize("editing", [False, True], ids=["new", "edit"])
def test_transaction_dialog_combo_names_and_buddies_survive_selection_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    editing: bool,
) -> None:
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "accessibility.sqlite3"))
    app = QApplication.instance() or QApplication([])
    repo = Repo()
    row = None
    if editing:
        transaction_id = repo.add_transaction(
            "2026-08-04", "Teste acessibilidade", -20, "Diversos", "Conta principal"
        )
        row = repo.transaction_by_id(transaction_id)
    dialog = TransactionDialog(repo, row=row)
    manager = AccessibilityManager(dialog)
    fields = (
        (dialog.kind, "Tipo", "Tipo"),
        (dialog.category, "Categoria", "Categoria"),
        (dialog.account, "Conta", "Conta"),
        (dialog.payment_method, "Método", "Método de pagamento"),
    )
    watched_fields = [combo for combo, _, _ in fields]
    watched_fields.extend(
        combo.lineEdit() for combo, _, _ in fields if combo.isEditable()
    )
    for field in watched_fields:
        field.installEventFilter(manager)

    def assert_metadata() -> None:
        labels = dialog.findChildren(QLabel, "TransactionFieldLabel")
        for combo, caption, name in fields:
            assert combo.accessibleName() == name
            matching_labels = [label for label in labels if label.text() == caption]
            assert len(matching_labels) == 1
            assert matching_labels[0].buddy() is combo
            if combo.isEditable():
                assert combo.lineEdit().accessibleName() == name
        assert len({combo.accessibleName() for combo, _, _ in fields}) == 4

    def reapply_manager() -> None:
        # Exercise queued focus handling before the explicit tree audit.
        for field in watched_fields:
            QCoreApplication.sendEvent(field, QFocusEvent(QEvent.Type.FocusIn))
        app.processEvents()
        assert_metadata()
        manager.audit_tree(dialog)
        assert_metadata()

    try:
        assert_metadata()
        dialog.show()
        app.processEvents()
        reapply_manager()
        for kind in ("Receita", "Despesa"):
            dialog.kind.setCurrentText(kind)
            assert dialog.category.count() > 1
            for index in (1, 0):
                dialog.category.setCurrentIndex(index)
                dialog.account.addItem(f"Conta de teste {kind} {index}")
                dialog.account.setCurrentIndex(dialog.account.count() - 1)
                dialog.payment_method.setCurrentIndex(index + 1)
                app.processEvents()
                reapply_manager()
        for combo in (dialog.account, dialog.payment_method):
            combo.setEditText("Valor personalizado")
            app.processEvents()
            reapply_manager()
    finally:
        for field in watched_fields:
            field.removeEventFilter(manager)
        app.processEvents()
        dialog.close()
        dialog.deleteLater()
        # Dispose only this test's dialog, not deferred Qt objects owned by
        # unrelated tests (for example stopped navigation animations).
        QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        assert not isValid(dialog)
        repo.db.conn.close()


def test_accessibility_manager_names_every_interactive_control() -> None:
    app = QApplication.instance() or QApplication([])
    host = QWidget()
    layout = QVBoxLayout(host)
    button = QPushButton("Guardar alterações")
    field = QLineEdit()
    field.setPlaceholderText("Pesquisar movimentos")
    combo = QComboBox()
    combo.addItems(["Mensal", "Anual"])
    table = QTableWidget(1, 1)
    for widget in (button, field, combo, table):
        layout.addWidget(widget)

    manager = AccessibilityManager(host)
    audit = manager.audit_tree(host)

    assert app is not None
    assert audit.inspected >= 4
    assert audit.unnamed == ()
    assert button.accessibleName() == "Guardar alterações"
    assert field.accessibleName() == "Pesquisar movimentos"
    # A combo's accessible name must describe the field, never echo the
    # currently selected value: value-derived names go stale as soon as the
    # user picks another item and then read the wrong field to screen readers.
    assert combo.accessibleName() == "Lista de opções"
    combo.setCurrentIndex(1)
    manager.audit_tree(host)
    assert combo.accessibleName() == "Lista de opções"
    assert table.accessibleName() == "Tabela ou lista"


def test_tooltip_manager_removes_html_and_consumes_native_fallback() -> None:
    app = QApplication.instance() or QApplication([])
    host = QWidget()
    host.resize(480, 260)
    button = QPushButton("Estado", host)
    button.setToolTip("<b>Saúde financeira</b><br>Detalhe legível")
    button.move(30, 30)
    host.show()
    app.processEvents()
    manager = ReadableToolTipManager(host)
    event = QHelpEvent(
        QEvent.Type.ToolTip,
        QPoint(5, 5),
        button.mapToGlobal(QPoint(5, 5)),
    )

    assert manager.eventFilter(button, event) is True
    assert manager.popup.text_label.text() == "Saúde financeira Detalhe legível"
    assert "<b>" not in manager.popup.text_label.text()

    empty = QWidget(host)
    empty_event = QHelpEvent(
        QEvent.Type.ToolTip,
        QPoint(1, 1),
        empty.mapToGlobal(QPoint(1, 1)),
    )
    assert manager.eventFilter(empty, empty_event) is True
    assert manager.popup.isHidden()


def test_tooltip_popup_recovers_after_a_transient_host_is_deleted() -> None:
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner.show()
    manager = ReadableToolTipManager(owner)
    dialog = QWidget(owner, Qt.Window)
    button = QPushButton("Ajuda", dialog)
    button.setToolTip("Primeiro detalhe")
    dialog.show()
    app.processEvents()
    first_event = QHelpEvent(
        QEvent.Type.ToolTip,
        QPoint(2, 2),
        button.mapToGlobal(QPoint(2, 2)),
    )
    assert manager.eventFilter(button, first_event) is True
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()

    next_button = QPushButton("Ajuda seguinte", owner)
    next_button.setToolTip("Segundo detalhe")
    next_event = QHelpEvent(
        QEvent.Type.ToolTip,
        QPoint(2, 2),
        next_button.mapToGlobal(QPoint(2, 2)),
    )
    assert manager.eventFilter(next_button, next_event) is True
    assert manager.popup.text_label.text() == "Segundo detalhe"


def test_visual_probe_has_a_stable_perceptual_signature_and_200_percent_matrix() -> (
    None
):
    image = QImage(160, 100, QImage.Format_ARGB32)
    image.fill(QColor("#F8FAFC"))
    for x in range(80, 160):
        for y in range(100):
            image.setPixelColor(x, y, QColor("#0F172A"))

    signature = _perceptual_hash(image)
    project = Path(__file__).resolve().parents[1]
    stress_script = (project / "scripts" / "validate_visual_stress.py").read_text(
        encoding="utf-8"
    )

    assert len(signature) == 64
    assert signature == _perceptual_hash(image)
    assert "(1.0, 1.25, 1.5, 2.0)" in stress_script
