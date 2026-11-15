from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCalendarWidget,
    QDialog,
    QHeaderView,
    QTableView,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ui.components import GlassCard
from ui.core import TableColumnWidthManager, Theme, apply_calendar_theme


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _Repo:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}

    def setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value


class _FailingRepo(_Repo):
    def __init__(self) -> None:
        super().__init__()
        self.fail_writes = True

    def set_setting(self, key: str, value: str) -> None:
        if self.fail_writes:
            raise OSError("simulated settings write failure")
        super().set_setting(key, value)


class _Page(QWidget):
    page_title = "Tabela de teste"

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        card = GlassCard("Movimentos")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Data", "Descrição", "Valor"])
        self.table.setProperty(
            "columnPersistenceColumnIds",
            ["date", "description", "amount"],
        )
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        card.v.addWidget(self.table)
        layout.addWidget(card)


def test_column_widths_are_saved_and_restored_for_matching_table(qapp):
    repo = _Repo()
    manager = TableColumnWidthManager(repo)
    first = _Page()
    manager.register_tree(first)
    qapp.processEvents()
    first.table.horizontalHeader().resizeSection(0, 132)
    first.table.horizontalHeader().resizeSection(1, 348)
    first.table.horizontalHeader().resizeSection(2, 176)
    QTest.qWait(520)

    key = str(first.table.property("columnPersistenceKey"))
    assert key == "table_columns:v2:page.page.table"
    assert json.loads(repo.settings[key])["widths"] == [132, 348, 176]
    assert json.loads(repo.settings[key])["version"] == 3
    assert json.loads(repo.settings[key])["table_id"] == "page.page.table"
    assert [column["id"] for column in json.loads(repo.settings[key])["columns"]] == [
        "date",
        "description",
        "amount",
    ]
    assert "hidden" not in json.loads(repo.settings[key])
    assert all(
        first.table.horizontalHeader().sectionResizeMode(column)
        == QHeaderView.ResizeMode.Interactive
        for column in range(3)
    )

    second = _Page()
    manager.register_tree(second)
    qapp.processEvents()
    manager.restore_table(second.table)

    assert [
        second.table.horizontalHeader().sectionSize(column) for column in range(3)
    ] == [132, 348, 176]
    assert all(
        second.table.horizontalHeader().sectionResizeMode(column)
        == QHeaderView.ResizeMode.Interactive
        for column in range(3)
    )

    second.table.horizontalHeader().setSectionResizeMode(
        1, QHeaderView.ResizeMode.Stretch
    )
    second.table.setColumnCount(4)
    qapp.processEvents()
    qapp.processEvents()

    assert all(
        second.table.horizontalHeader().sectionResizeMode(column)
        == QHeaderView.ResizeMode.Interactive
        for column in range(4)
    )


def test_stable_identifier_does_not_depend_on_titles_or_headers(qapp):
    repo = _Repo()
    manager = TableColumnWidthManager(repo)
    first = _Page()
    first.table.setProperty("columnPersistenceId", "movements.primary-list")
    manager.register_tree(first)
    first_key = str(first.table.property("columnPersistenceKey"))

    second = _Page()
    second.table.setProperty("columnPersistenceId", "movements.primary-list")
    second.table.setHorizontalHeaderLabels(["Quando", "Detalhe", "Montante"])
    second.findChild(GlassCard).title_label.setText("Lista traduzida")
    manager.register_tree(second)
    qapp.processEvents()

    assert first_key == "table_columns:v2:movements.primary_list"
    assert str(second.table.property("columnPersistenceKey")) == first_key
    assert str(second.table.property("columnPersistenceId")) == (
        "movements.primary_list"
    )


def test_legacy_widths_are_restored_and_migrated_to_stable_key(qapp):
    repo = _Repo()
    manager = TableColumnWidthManager(repo)
    page = _Page()
    legacy_key = manager._legacy_state_key(page.table)
    repo.settings[legacy_key] = json.dumps({"widths": [141, 329, 187], "hidden": [1]})

    manager.register_tree(page)
    qapp.processEvents()
    manager.restore_table(page.table)

    stable_key = str(page.table.property("columnPersistenceKey"))
    assert stable_key != legacy_key
    assert page.table.horizontalHeader().sectionSize(0) == 141
    assert page.table.horizontalHeader().sectionSize(2) == 187
    assert page.table.isColumnHidden(1)
    assert str(page.table.property("columnPersistenceMigratedFrom")) == legacy_key
    migrated = json.loads(repo.settings[stable_key])
    assert migrated["version"] == 2
    assert migrated["table_id"] == "page.page.table"
    assert migrated["widths"] == [141, 329, 187]


def test_write_failure_is_logged_and_exposed_on_the_table(qapp, monkeypatch):
    repo = _FailingRepo()
    manager = TableColumnWidthManager(repo)
    page = _Page()
    page.table.setProperty("columnPersistenceId", "tests.write_failure")
    reported: list[tuple[str, BaseException | None]] = []
    monkeypatch.setattr(
        "ui.core.table_state.log_exception",
        lambda context, exc=None: reported.append((context, exc)),
    )

    manager.register_tree(page)
    qapp.processEvents()
    assert not manager.save_table(page.table)
    assert bool(page.table.property("columnPersistenceSaveFailed"))
    assert "simulated settings write failure" in str(
        page.table.property("columnPersistenceSaveError")
    )
    assert len(reported) == 1
    assert "table=tests.write_failure" in reported[0][0]
    assert "operation=save" in reported[0][0]

    # Repeated identical timer failures do not flood the log.
    assert not manager.save_table(page.table)
    assert len(reported) == 1

    repo.fail_writes = False
    assert manager.save_table(page.table)
    assert not bool(page.table.property("columnPersistenceSaveFailed"))
    assert page.table.property("columnPersistenceSaveError") == ""


def test_future_dialog_tables_are_registered_resizable_and_saved(qapp):
    repo = _Repo()
    manager = TableColumnWidthManager(repo)
    qapp.installEventFilter(manager)
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    table = QTableWidget(0, 2)
    table.setHorizontalHeaderLabels(["Nome", "Estado"])
    table.horizontalHeader().setSectionResizeMode(
        0, QHeaderView.ResizeMode.ResizeToContents
    )
    table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    layout.addWidget(table)

    try:
        dialog.show()
        qapp.processEvents()
        qapp.processEvents()

        assert bool(table.property("columnPersistenceRegistered"))
        assert all(
            table.horizontalHeader().sectionResizeMode(column)
            == QHeaderView.ResizeMode.Interactive
            for column in range(2)
        )
        table.horizontalHeader().resizeSection(0, 244)
        table.horizontalHeader().resizeSection(1, 188)
        QTest.qWait(520)
        key = str(table.property("columnPersistenceKey"))
        assert json.loads(repo.settings[key])["widths"] == [244, 188]
    finally:
        dialog.close()
        qapp.removeEventFilter(manager)


def test_header_menu_hides_restores_and_persists_columns(qapp):
    repo = _Repo()
    manager = TableColumnWidthManager(repo)
    first = _Page()
    manager.register_tree(first)
    qapp.processEvents()
    header = first.table.horizontalHeader()
    header.resizeSection(0, 132)
    header.resizeSection(1, 348)
    header.resizeSection(2, 176)

    assert header.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    menu = manager._create_column_menu(first.table, 1)
    action_texts = [action.text() for action in menu.actions()]
    assert "Ocultar «Descrição»" in action_texts
    assert "Ajustar «Descrição» ao conteúdo" in action_texts
    assert "Colunas visíveis" in action_texts
    assert "Mostrar todas as colunas" in action_texts
    assert "Ajustar larguras ao conteúdo" in action_texts
    assert "Repor larguras padrão" in action_texts
    assert "Repor organização da tabela" in action_texts

    assert manager.set_column_visible(first.table, 1, False)
    assert first.table.isColumnHidden(1)
    key = str(first.table.property("columnPersistenceKey"))
    saved = json.loads(repo.settings[key])
    assert saved["hidden"] == [1]
    assert saved["widths"] == [132, 348, 176]

    second = _Page()
    manager.register_tree(second)
    qapp.processEvents()
    manager.restore_table(second.table)
    assert second.table.isColumnHidden(1)

    assert manager.set_column_visible(second.table, 1, True)
    assert not second.table.isColumnHidden(1)
    assert second.table.horizontalHeader().sectionSize(1) == 348
    assert json.loads(repo.settings[key])["hidden"] == []

    assert manager.set_column_visible(second.table, 0, False)
    assert manager.set_column_visible(second.table, 1, False)
    assert not manager.set_column_visible(second.table, 2, False)
    assert not second.table.isColumnHidden(2)

    manager.show_all_columns(second.table)
    assert all(not second.table.isColumnHidden(column) for column in range(3))
    assert json.loads(repo.settings[key])["hidden"] == []


def test_column_ids_preserve_width_visibility_order_and_sort(qapp):
    repo = _Repo()
    manager = TableColumnWidthManager(repo)
    first = _Page()
    first.table.setProperty("columnPersistenceId", "tests.column_schema")
    first.table.setSortingEnabled(True)
    manager.register_tree(first)
    qapp.processEvents()
    header = first.table.horizontalHeader()
    header.resizeSection(0, 131)
    header.resizeSection(1, 347)
    header.resizeSection(2, 179)
    header.moveSection(header.visualIndex(2), 0)
    first.table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert manager.set_column_visible(first.table, 1, False)
    manager.save_table(first.table)

    key = str(first.table.property("columnPersistenceKey"))
    saved = json.loads(repo.settings[key])
    assert saved["version"] == 3
    assert saved["order"] == ["amount", "date", "description"]
    assert saved["order_customized"]
    assert saved["sort"] == {
        "column_id": "description",
        "order": "descending",
    }

    second = _Page()
    second.table.setProperty("columnPersistenceId", "tests.column_schema")
    second.table.setHorizontalHeaderLabels(["Valor", "Data", "Descrição"])
    second.table.setProperty(
        "columnPersistenceColumnIds",
        ["amount", "date", "description"],
    )
    second.table.setSortingEnabled(True)
    manager.register_tree(second)
    qapp.processEvents()
    manager.restore_table(second.table)
    second_header = second.table.horizontalHeader()

    assert [second_header.sectionSize(column) for column in range(2)] == [179, 131]
    assert second.table.isColumnHidden(2)
    assert not second.table.isColumnHidden(0)
    assert manager.set_column_visible(second.table, 2, True)
    assert second_header.sectionSize(2) == 347
    assert second_header.sortIndicatorSection() == 2
    assert second_header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    assert second_header.sectionsMovable()


def test_reset_actions_restore_original_table_layout(qapp):
    repo = _Repo()
    manager = TableColumnWidthManager(repo)
    page = _Page()
    page.table.setProperty("columnPersistenceId", "tests.reset_layout")
    page.table.setSortingEnabled(True)
    manager.register_tree(page)
    qapp.processEvents()
    header = page.table.horizontalHeader()
    default_widths = [header.sectionSize(column) for column in range(3)]

    header.resizeSection(0, 240)
    manager.resize_columns_to_contents(page.table, [1])
    assert bool(page.table.property("columnWidthsCustomized"))
    manager.reset_column_widths(page.table)
    assert not bool(page.table.property("columnWidthsCustomized"))
    assert [header.sectionSize(column) for column in range(3)] == default_widths

    header.moveSection(header.visualIndex(2), 0)
    assert manager.set_column_visible(page.table, 1, False)
    page.table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
    manager.reset_table_layout(page.table)

    assert [header.logicalIndex(visual) for visual in range(3)] == [0, 1, 2]
    assert all(not page.table.isColumnHidden(column) for column in range(3))
    assert not bool(page.table.property("columnOrderCustomized"))
    assert not bool(page.table.property("columnVisibilityCustomized"))
    assert not bool(page.table.property("columnSortCustomized"))
    saved = json.loads(repo.settings[str(page.table.property("columnPersistenceKey"))])
    assert saved["version"] == 3
    assert not saved["widths_customized"]
    assert not saved["visibility_customized"]
    assert not saved["order_customized"]
    assert not saved["sort_customized"]


def test_calendar_private_grid_is_never_treated_as_a_user_table(qapp):
    repo = _Repo()
    manager = TableColumnWidthManager(repo)
    root = QWidget()
    layout = QVBoxLayout(root)
    calendar = QCalendarWidget()
    calendar.setObjectName("FinanceCalendar")
    apply_calendar_theme(calendar, Theme(dark=False))
    layout.addWidget(calendar)
    root.resize(520, 360)
    root.show()
    qapp.processEvents()

    calendar_view = calendar.findChild(QTableView, "qt_calendar_calendarview")
    assert calendar_view is not None
    manager.register_tree(root)
    qapp.processEvents()

    assert bool(calendar_view.property("columnPersistenceExcluded"))
    assert not bool(calendar_view.property("columnPersistenceRegistered"))
    assert all(
        calendar_view.horizontalHeader().sectionResizeMode(column)
        == QHeaderView.ResizeMode.Stretch
        for column in range(calendar_view.model().columnCount())
    )
    root.close()
