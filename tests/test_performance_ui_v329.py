from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidget

from database.repo import Repo
from ui.core import SmartTableWidgetItem, TableColumnWidthManager, Theme


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "v329.sqlite3"))
    instance = Repo()
    instance.db.conn.executescript(
        """
        DELETE FROM transaction_attachments;
        DELETE FROM transaction_meta;
        DELETE FROM transaction_splits;
        DELETE FROM transactions;
        """
    )
    instance.db.conn.commit()
    yield instance
    instance.db.conn.close()


def test_literal_transaction_search_uses_fts_and_keeps_symbols_literal(repo: Repo):
    exact_id = repo.add_transaction(
        "2026-08-13",
        "Loja 100%_real",
        -12.34,
        "Diversos",
        "Conta principal",
        notes="referência literal",
    )
    repo.add_transaction(
        "2026-08-13",
        "Loja 1000 real",
        -12.34,
        "Diversos",
        "Conta principal",
    )

    assert [row["id"] for row in repo.transactions_filtered(search="100%_")] == [
        exact_id
    ]
    assert [row["id"] for row in repo.transactions_filtered(search="%")] == [exact_id]
    assert [row["id"] for row in repo.transactions_filtered(search="_")] == [exact_id]

    sql, params = repo._filtered_transactions_statement(search="100%_", limit=20)
    plan = "\n".join(
        str(row["detail"]) for row in repo.q("EXPLAIN QUERY PLAN " + sql, params)
    )
    assert "transaction_search_fts" in plan
    assert "VIRTUAL TABLE INDEX" in plan


def test_transaction_search_index_tracks_movement_and_metadata_changes(repo: Repo):
    transaction_id = repo.add_transaction(
        "2026-08-13",
        "Descrição inicial",
        -9.0,
        "Diversos",
        "Conta principal",
    )
    repo.set_transaction_meta_ext(
        transaction_id,
        merchant="Café Central",
        tags="fim_de_semana",
    )
    assert len(repo.transactions_filtered(search="Café Central")) == 1
    assert len(repo.transactions_filtered(search="fim_de_semana")) == 1

    repo.update_transaction_fields(
        transaction_id, {"description": "Descrição atualizada"}
    )
    assert repo.transactions_filtered(search="Descrição inicial") == []
    assert len(repo.transactions_filtered(search="Descrição atualizada")) == 1

    repo.db.conn.execute(
        "DELETE FROM transaction_meta WHERE transaction_id=?", (transaction_id,)
    )
    repo.db.conn.commit()
    assert repo.transactions_filtered(search="Café Central") == []
    assert len(repo.transactions_filtered(search="Descrição atualizada")) == 1


class _SettingsRepo:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}

    def setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value


def _smart_table() -> QTableWidget:
    table = QTableWidget(4, 4)
    table.setProperty("columnPersistenceId", "tests.typed_sorting")
    table.setProperty(
        "columnPersistenceColumnIds", ["date", "amount", "percent", "status"]
    )
    table.setHorizontalHeaderLabels(["Data", "Valor", "%", "Estado"])
    rows = [
        ("02/01/2026", "100,00 €", "9,5%", "Concluído"),
        ("31/12/2025", "9,00 €", "100,0%", "Crítico"),
        ("2026-01-15", "20,00 €", "12,0%", "Pendente"),
        ("janeiro 2025", "1 200,00 €", "2,0%", "Alerta"),
    ]
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            table.setItem(row, column, SmartTableWidgetItem(value))
    return table


def test_all_managed_tables_sort_typed_values_and_persist_choice(qapp):
    repo = _SettingsRepo()
    manager = TableColumnWidthManager(repo)
    table = _smart_table()
    manager.register_table(table)
    qapp.processEvents()

    assert table.isSortingEnabled()
    table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
    assert [table.item(row, 1).text() for row in range(4)] == [
        "9,00 €",
        "20,00 €",
        "100,00 €",
        "1 200,00 €",
    ]
    table.sortByColumn(2, Qt.SortOrder.AscendingOrder)
    assert [table.item(row, 2).text() for row in range(4)] == [
        "2,0%",
        "9,5%",
        "12,0%",
        "100,0%",
    ]
    table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    assert [table.item(row, 0).text() for row in range(4)] == [
        "janeiro 2025",
        "31/12/2025",
        "02/01/2026",
        "2026-01-15",
    ]
    table.sortByColumn(3, Qt.SortOrder.AscendingOrder)
    assert [table.item(row, 3).text() for row in range(4)] == [
        "Crítico",
        "Alerta",
        "Pendente",
        "Concluído",
    ]
    manager.save_table(table)
    state = json.loads(repo.settings[str(table.property("columnPersistenceKey"))])
    assert state["sort"] == {"column_id": "status", "order": "ascending"}
    assert state["sort_customized"]


def test_sorting_is_safely_suspended_while_table_is_repopulated(qapp):
    repo = _SettingsRepo()
    manager = TableColumnWidthManager(repo)
    table = _smart_table()
    manager.register_table(table)
    table.sortByColumn(1, Qt.SortOrder.AscendingOrder)

    table.setRowCount(3)
    replacement = [
        ("A", "30,00 €"),
        ("B", "10,00 €"),
        ("C", "20,00 €"),
    ]
    for row, (name, amount) in enumerate(replacement):
        table.setItem(row, 0, SmartTableWidgetItem(name))
        table.setItem(row, 1, SmartTableWidgetItem(amount))
    QTest.qWait(20)

    assert table.isSortingEnabled()
    assert [table.item(row, 1).text() for row in range(3)] == [
        "10,00 €",
        "20,00 €",
        "30,00 €",
    ]
    assert [table.item(row, 0).text() for row in range(3)] == ["B", "C", "A"]


class _AssistantRepo(_SettingsRepo):
    def simple_duplicate_groups(self, *, limit: int = 5):
        return []

    def generic_category_count(self) -> int:
        return 0


def test_local_assistant_is_compact_expandable_and_responsive(qapp):
    from ui.pages.ai import AssistantPage

    repo = _AssistantRepo()
    app = SimpleNamespace(
        repo=repo,
        analytics_service=SimpleNamespace(
            totals_for_period=lambda *_args: {
                "income": 2000.0,
                "expenses": 1400.0,
                "savings": 600.0,
            },
            category_totals=lambda *_args: [("Supermercado", 350.0, "#0A84FF")],
        ),
        payments_service=SimpleNamespace(
            active_subscription_monthly_total=lambda: 29.99
        ),
        import_service=SimpleNamespace(),
        theme=Theme(dark=False, privacy=False),
    )
    page = AssistantPage(app)
    page.resize(640, 560)
    page.show()
    qapp.processEvents()

    assert page.details_card.isHidden()
    assert page.text.minimumHeight() == 180
    assert page.text.maximumHeight() == 340
    assert page.alerts_section.content.isVisibleTo(page)
    assert not page.categories_section.content.isVisibleTo(page)
    assert "600" in page.savings_value.text()

    page.categories_section.toggle.click()
    assert page.categories_section.content.isVisibleTo(page)
    page.explanation_btn.click()
    assert page.details_card.isVisibleTo(page)
    assert page.explanation_btn.text() == "Ocultar explicação completa"
    assert "ANÁLISE DO MÊS" in page.text.toPlainText()
    assert page.minimumHeight() < 800
    page.close()
