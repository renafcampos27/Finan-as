"""A14 critical-form tasks and A12 bounded GUI refresh work."""

import base64
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QEvent, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtGui import QFont, QFontDatabase
from shiboken6 import delete, isValid
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from database.engine import FinanceDB
from database.repo import Repo
from ui.components.cards import GlassCard
from ui.core.page import BasePage
from ui.core.table_state import TableColumnWidthManager
from ui.core.theme import Theme
from ui.core.widgets import LiquidSheetDialog, OptionalDateEdit
from ui.dialogs import (
    ClosePositionDialog,
    HoldingDialog,
    TransactionDialog,
    TransferDialog,
)
from ui.pages.tools import SmartImportPage


def dispose(*widgets):
    """Dispose only receivers created and owned by this test."""
    for widget in widgets:
        if isValid(widget):
            widget.close()
            widget.deleteLater()
            QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    # Qt's Windows offscreen plugin uses FreeType and does not automatically
    # discover installed system fonts. Missing glyph boxes are not layout QA.
    font_directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("segoeui.ttf", "seguisym.ttf", "seguiemj.ttf"):
        path = font_directory / name
        if path.is_file():
            assert QFontDatabase.addApplicationFont(str(path)) >= 0
    if "Segoe UI" in QFontDatabase.families():
        app.setFont(QFont("Segoe UI", 9))
    return app


@pytest.fixture
def repo(tmp_path, empty_profile):
    instance = Repo(FinanceDB(tmp_path / "accessibility.sqlite3"))
    yield instance
    instance.db.conn.close()


class Settings:
    def __init__(self):
        self.values = {}
        self.reads = []

    def setting(self, key, default=""):
        self.reads.append(key)
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


def import_page(qapp, repo):
    service = MagicMock()
    service.preview.return_value = SimpleNamespace(
        rows=[],
        total=1,
        certain_duplicates=0,
        probable_duplicates=0,
        possible_duplicates=0,
        invalid=0,
        ambiguous_dates=False,
    )
    service.file_history_warning.return_value = ""
    app = SimpleNamespace(
        repo=repo,
        theme=Theme(),
        analytics_service=MagicMock(),
        payments_service=MagicMock(),
        import_service=service,
        notify_data_changed=MagicMock(),
    )
    return SmartImportPage(app)


def test_transaction_all_editors_have_explicit_buddies_and_names(qapp, repo):
    dialog = TransactionDialog(repo)
    try:
        fields = [
            dialog.kind,
            dialog.amount,
            dialog.desc,
            dialog.category,
            dialog.subcategory,
            dialog.purchase_date,
            dialog.payment_date,
            dialog.account,
            dialog.payment_method,
            dialog.merchant,
            dialog.tags,
            dialog.notes,
            dialog.attach_btn,
        ]
        labels = dialog.findChildren(QLabel, "TransactionFieldLabel")
        assert all(any(label.buddy() is field for label in labels) for field in fields)
        assert all(field.accessibleName() for field in fields)
        assert dialog.notes.tabChangesFocus()
    finally:
        dispose(dialog)


@pytest.mark.parametrize("size", [(440, 260), (320, 200), (640, 480)])
def test_sheet_stays_inside_short_narrow_owner(qapp, size):
    parent = QWidget()
    parent.resize(*size)
    dialog = LiquidSheetDialog(parent)
    body = QVBoxLayout()
    for _ in range(12):
        body.addWidget(QLineEdit())
    body.addWidget(QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel))
    dialog.set_content_layout(body)
    try:
        parent.show()
        dialog.show()
        qapp.processEvents()
        assert dialog.rect().contains(dialog.sheet.geometry())
    finally:
        dispose(dialog)
        dispose(parent)


def test_phrase_reads_are_bounded_by_refresh_operation(qapp):
    class CardPage(BasePage):
        def __init__(self):
            QWidget.__init__(self)
            self.repo = Settings()
            self.card = GlassCard("Savings", self)

        def begin_data_refresh(self):
            pass

        def mark_data_updated(self):
            self.card.phrase_preferences()

        def refresh(self):
            return [self.card.phrase_preferences() for _ in range(12)]

    page = CardPage()
    try:
        first = page.refresh()
        assert len(page.repo.reads) == 1
        page.repo.values[page.card.phrase_setting_key()] = '{"show_footer":false}'
        second = page.refresh()
        assert len(page.repo.reads) == 2
        assert first[0]["show_footer"] and not second[0]["show_footer"]
    finally:
        dispose(page)


def test_repeated_model_signals_coalesce_into_one_restore(qapp, monkeypatch):
    manager = TableColumnWidthManager(Settings())
    table = QTableWidget(0, 2)
    restores = []
    monkeypatch.setattr(manager, "_reapply", lambda current: restores.append(current))
    manager._connect_model(table)
    model = table.model()
    model.modelReset.emit()
    model.headerDataChanged.emit(Qt.Horizontal, 0, 1)
    model.modelReset.emit()
    qapp.processEvents()
    assert restores == [table]
    manager.deactivate()
    dispose(table)


def test_import_empty_state_disables_commit(qapp, repo):
    page = import_page(qapp, repo)
    try:
        assert not page.import_btn.isEnabled()
        page.populate_preview()
        assert not page.import_btn.isEnabled()
        assert "Abre um extrato" in page.import_status.text()
    finally:
        dispose(page)


def test_import_rich_status_escapes_external_labels(qapp, repo):
    page = import_page(qapp, repo)
    page.rows = [{"Data": "2026-10-01"}]
    page.detected_bank = '<img src="file:///external">'
    page.csv_warnings = ("<b>untrusted</b>",)
    try:
        page.populate_preview()
        assert "<img" not in page.import_status.text()
        assert "&lt;img" in page.import_status.text()
        assert "<b>untrusted</b>" not in page.import_status.text()
        assert page.import_status.textFormat() == Qt.RichText
    finally:
        dispose(page)


def test_transaction_keyboard_entry_can_reach_and_activate_save(qapp, repo):
    dialog = TransactionDialog(repo)
    try:
        dialog.show()
        qapp.processEvents()
        dialog.desc.setFocus()
        QTest.keyClicks(dialog.desc, "Compra por teclado")
        dialog.amount.setFocus()
        dialog.amount.selectAll()
        QTest.keyClicks(dialog.amount, "23,45")
        dialog.notes.setFocus()
        QTest.keyClicks(dialog.notes, "Nota por teclado")
        QTest.keyClick(dialog.notes, Qt.Key_Tab)
        assert dialog.focusWidget() is dialog.attach_btn
        QTest.keyClick(dialog.attach_btn, Qt.Key_Tab)
        save = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Save)
        assert dialog.focusWidget() is save
        QTest.keyClick(save, Qt.Key_Space)
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.extended_data()["description"] == "Compra por teclado"
        assert dialog.extended_data()["amount"] == pytest.approx(-23.45)
    finally:
        dispose(dialog)


def test_transfer_keyboard_fields_and_confirmation(qapp, repo):
    parent = QWidget()
    parent.resize(760, 480)
    dialog = TransferDialog(repo, parent)
    try:
        parent.show()
        dialog.show()
        qapp.processEvents()
        dialog.dest.lineEdit().selectAll()
        QTest.keyClicks(dialog.dest.lineEdit(), "Conta destino sintetica")
        dialog.amount.setFocus()
        dialog.amount.selectAll()
        QTest.keyClicks(dialog.amount, "10,50")
        for field in (dialog.amount, dialog.date_edit, dialog.source, dialog.dest):
            assert field.accessibleName()
        buttons = dialog.findChild(QDialogButtonBox)
        save = buttons.button(QDialogButtonBox.Save)
        save.setFocus()
        QTest.keyClick(save, Qt.Key_Space)
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.extended_data()["amount"] == pytest.approx(10.5)
        assert dialog.extended_data()["dest"] == "Conta destino sintetica"
    finally:
        dispose(dialog)
        dispose(parent)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("font_scale", [1.0, 1.5], ids=["normal_text", "large_text"])
@pytest.mark.parametrize("kind", ["transaction", "transfer", "holding", "sale"])
def test_critical_form_actions_remain_reachable(
    qapp, repo, tmp_path, dark, font_scale, kind
):
    original_font = qapp.font()
    parent = QWidget()
    parent.repo = repo
    parent.theme = Theme(dark=dark)
    # Stress the shipped theme, including its explicit pixel-sized fonts.
    # This is simulated text enlargement, separate from Qt's screen scale.
    stylesheet = re.sub(
        r"font-size:\s*(\d+(?:\.\d+)?)px",
        lambda match: f"font-size: {float(match.group(1)) * font_scale:g}px",
        parent.theme.stylesheet(),
    )
    parent.setStyleSheet(stylesheet)
    font = parent.font()
    font.setPointSizeF(font.pointSizeF() * font_scale)
    qapp.setFont(font)
    parent.setFont(font)
    available = qapp.primaryScreen().availableGeometry()
    parent.resize(min(960, available.width()), min(540, available.height()))
    if kind == "transaction":
        dialog = TransactionDialog(repo, parent)
    elif kind == "transfer":
        dialog = TransferDialog(repo, parent)
    elif kind == "holding":
        dialog = HoldingDialog(parent)
    else:
        dialog = ClosePositionDialog(
            parent=parent,
            holding={
                "id": 1,
                "name": "Synthetic",
                "value": 100,
                "cost_basis": 90,
                "cost_basis_known": 1,
                "fees": 0,
                "currency": "EUR",
            },
        )
    dialog.setFont(font)
    try:
        parent.show()
        dialog.show()
        qapp.processEvents()
        editor = (
            dialog.desc
            if kind == "transaction"
            else (
                dialog.amount
                if kind == "transfer"
                else (dialog.name if kind == "holding" else dialog.sale_value)
            )
        )
        assert editor.font().pixelSize() >= 14 * font_scale
        if isinstance(dialog, LiquidSheetDialog):
            dialog.anim.stop()
            assert dialog.rect().contains(dialog.sheet.geometry())
            scroll = dialog._content_scroll
        else:
            assert dialog.width() <= available.width()
            assert dialog.height() <= available.height()
            scroll = dialog._dialog_scroll
        buttons = dialog.findChild(QDialogButtonBox)
        save = buttons.button(QDialogButtonBox.Save) or buttons.button(
            QDialogButtonBox.Ok
        )
        editor.setFocus()
        traversed = []
        for _ in range(80):
            if dialog.focusWidget() is save:
                break
            current = dialog.focusWidget()
            traversed.append((type(current).__name__, current.accessibleName()))
            QTest.keyClick(dialog.focusWidget(), Qt.Key_Tab)
        qapp.processEvents()
        assert save.isVisible() and save.hasFocus(), traversed
        if scroll.isAncestorOf(save):
            geometry = QRect(save.mapTo(scroll.viewport(), QPoint()), save.size())
            assert scroll.viewport().rect().contains(geometry)
        else:
            assert dialog.rect().contains(
                QRect(save.mapTo(dialog, QPoint()), save.size())
            )
        if os.environ.get("FINANCAS_PESSOAIS_PHASE5_CAPTURE") == "1":
            screenshot = (
                tmp_path / f"{kind}_{'dark' if dark else 'light'}_{font_scale}.png"
            )
            pixmap = dialog.grab()
            assert pixmap.save(str(screenshot), "PNG")
            print(
                "PHASE5_SCREENSHOT_JSON="
                + json.dumps(
                    {
                        "path": str(screenshot),
                        "kind": kind,
                        "dark": dark,
                        "font_scale": font_scale,
                        "actual_dpr": dialog.devicePixelRatioF(),
                        "editor_font_points": editor.font().pointSizeF(),
                        "editor_font_pixels": editor.font().pixelSize(),
                        "editor_font_height": editor.fontMetrics().height(),
                        "theme_stylesheet_applied": True,
                        "font_family": editor.fontInfo().family(),
                        "screen_geometry": [available.width(), available.height()],
                        "dialog_size": [dialog.width(), dialog.height()],
                        "viewport": [
                            scroll.viewport().width(),
                            scroll.viewport().height(),
                        ],
                        "image_size": [pixmap.width(), pixmap.height()],
                        "png_base64": base64.b64encode(screenshot.read_bytes()).decode(
                            "ascii"
                        ),
                    }
                )
            )
        cancel = buttons.button(QDialogButtonBox.Cancel)
        QTest.keyClick(save, Qt.Key_Tab)
        assert dialog.focusWidget() is cancel
        QTest.keyClick(cancel, Qt.Key_Space)
        assert not dialog.isVisible()
    finally:
        dispose(dialog)
        dispose(parent)
        qapp.setFont(original_font)


def test_import_error_state_distinguishes_failure_from_empty_and_hides_private_detail(
    qapp, repo
):
    page = import_page(qapp, repo)
    try:
        page.rows = [{"Data": "bad"}]
        page.import_service.preview.side_effect = ValueError("<secret>")
        page.populate_preview()
        assert "Não foi possível calcular" in page.import_status.text()
        assert "&lt;secret&gt;" in page.import_status.text()
        assert not page.import_btn.isEnabled()
        page.app.theme.privacy = True
        page.populate_preview()
        assert "secret" not in page.import_status.text()
        assert "ocultos" in page.import_status.text()
    finally:
        dispose(page)


def test_import_mapping_fields_have_stable_buddies(qapp, repo):
    page = import_page(qapp, repo)
    try:
        fields = [
            page.map_date,
            page.map_desc,
            page.map_amount,
            page.map_amount_debit,
            page.map_amount_credit,
            page.map_category,
            page.map_account,
            page.map_method,
            page.map_merchant,
            page.map_reference,
            page.amount_locale,
            page.date_format,
        ]
        assert all(field.accessibleName() for field in fields)
        assert all(
            any(label.buddy() is field for label in page.findChildren(QLabel))
            for field in fields
        )
        assert len({field.accessibleName() for field in fields}) == len(fields)
    finally:
        dispose(page)


def test_phrase_operation_separates_repositories_and_invalidates_own_writes(qapp):
    from ui.core.read_scope import settings_read_scope

    first, second = GlassCard("Same"), GlassCard("Same")
    first._repo, second._repo = Settings(), Settings()
    try:
        with settings_read_scope():
            assert first.phrase_preferences()["show_footer"]
            second.save_phrase_preferences({"show_footer": False})
            assert not second.phrase_preferences()["show_footer"]
            assert first.phrase_preferences()["show_footer"]
            first.save_phrase_preferences({"show_footer": False})
            assert not first.phrase_preferences()["show_footer"]
            first.reset_phrase_preferences()
            assert first.phrase_preferences()["show_footer"]
        assert len(second._repo.reads) == 2
        assert len(first._repo.reads) == 3
    finally:
        dispose(first)
        dispose(second)


def test_failed_refresh_does_not_leave_setting_snapshot_live(qapp):
    from ui.core.read_scope import read_setting, settings_read_scope

    repo = Settings()
    with pytest.raises(ValueError):
        with settings_read_scope():
            read_setting(repo, "x")
            with settings_read_scope():
                read_setting(repo, "x")
            raise ValueError("stop")
    repo.values["x"] = "new"
    assert read_setting(repo, "x") == "new"
    assert len(repo.reads) == 2


def test_table_coalescing_preserves_pending_user_layout_save(qapp, monkeypatch):
    owner = QWidget()
    owner.page_title = "Synthetic"
    table = QTableWidget(0, 3, owner)
    table.setProperty("columnPersistenceId", "phase5.synthetic")
    table.setHorizontalHeaderLabels(["Date", "Value", "Notes"])
    manager = TableColumnWidthManager(Settings())
    manager.register_table(table)
    qapp.processEvents()
    saved = []
    original_save = manager.save_table

    def counted_save(current):
        saved.append(current)
        return original_save(current)

    monkeypatch.setattr(manager, "save_table", counted_save)
    try:
        table.horizontalHeader().resizeSection(1, 241)
        table.horizontalHeader().moveSection(2, 0)
        assert manager._timers[table].isActive()
        table.model().modelReset.emit()
        table.model().headerDataChanged.emit(Qt.Horizontal, 0, 2)
        table.model().modelReset.emit()
        qapp.processEvents()
        assert saved == [table]
        assert table.horizontalHeader().sectionSize(1) == 241
        assert table.horizontalHeader().visualIndex(2) == 0
        assert not manager._timers[table].isActive()
    finally:
        manager.deactivate()
        dispose(owner)


def test_table_reapply_timer_is_cancelled_with_native_receiver(qapp, monkeypatch):
    manager = TableColumnWidthManager(Settings())
    table = QTableWidget(0, 2)
    calls = []
    monkeypatch.setattr(manager, "_reapply", lambda current: calls.append(current))
    manager._connect_model(table)
    table.model().modelReset.emit()
    delete(table)
    qapp.processEvents()
    assert calls == []
    manager.deactivate()


def test_table_deactivate_cancels_queued_reapply(qapp, monkeypatch):
    manager = TableColumnWidthManager(Settings())
    table = QTableWidget(0, 2)
    calls = []
    monkeypatch.setattr(manager, "_reapply", lambda current: calls.append(current))
    manager._connect_model(table)
    table.model().modelReset.emit()
    manager.deactivate()
    qapp.processEvents()
    assert calls == []
    dispose(table)


def test_optional_date_tabs_escape_unset_without_changing_date(qapp):
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    before, after = QLineEdit(), QLineEdit()
    field = OptionalDateEdit()
    field.setMinimumDate(QDate(2000, 1, 1))
    field.setSpecialValueText("Não indicada")
    field.setDate(field.minimumDate())
    for widget in (before, field, after):
        layout.addWidget(widget)
    try:
        dialog.show()
        qapp.processEvents()
        field.setFocus()
        QTest.keyClick(field, Qt.Key_Tab)
        assert dialog.focusWidget() is after
        field.setFocus()
        QTest.keyClick(field, Qt.Key_Backtab)
        assert dialog.focusWidget() is before
        assert field.date() == field.minimumDate()
        field.setDate(QDate(2026, 10, 1))
        field.setFocus()
        original = field.date()
        QTest.keyClick(field, Qt.Key_Up)
        assert field.date().isValid() and field.date() != original
    finally:
        dispose(dialog)


def test_page_customization_write_is_visible_inside_current_read_operation(
    qapp, monkeypatch
):
    from ui.core.read_scope import settings_read_scope
    import ui.card_customization as customization

    class Page(BasePage):
        def __init__(self):
            QWidget.__init__(self)
            self.repo = Settings()
            self.app = SimpleNamespace()
            self.page_title = "Synthetic"
            self.card = QWidget(self)
            self.applied = None

        def page_cards(self):
            return [("one", "One", self.card)]

        def apply_card_preferences(self):
            self.applied = self._card_preferences()

    page = Page()
    monkeypatch.setattr(
        customization,
        "PageCardCustomizationDialog",
        lambda *args: SimpleNamespace(
            exec=lambda: 1, state=lambda: {"hidden": ["one"], "permanent": []}
        ),
    )
    try:
        with settings_read_scope():
            assert page._card_preferences()["hidden"] == []
            page.open_card_customization()
            assert page.applied["hidden"] == ["one"]
        assert len(page.repo.reads) == 2
    finally:
        dispose(page)
