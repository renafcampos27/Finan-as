from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from database.repo import Repo
from services.ai import AIService
from services.change_journal import ChangeJournalService
from services.imports import ImportService
from ui.components.charts.donut import DonutChart
from ui.core.theme import Theme
from ui.dialogs import TransactionDialog


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "integrity-v328.sqlite3"))
    instance = Repo()
    yield instance
    instance.db.conn.close()


def test_account_identity_collapses_case_and_whitespace(repo: Repo) -> None:
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("DELETE FROM financial_accounts")
    repo.db.conn.commit()

    repo.add_transaction(
        "2026-08-01",
        "Primeiro",
        -10,
        "Diversos",
        "  Conta   principal ",
    )
    repo.add_transaction(
        "2026-08-02",
        "Segundo",
        -12,
        "Diversos",
        "conta principal",
    )
    repo.add_transaction(
        "2026-08-03",
        "Terceiro",
        -8,
        "Diversos",
        "  Poupança   Família ",
    )
    repo.add_transaction(
        "2026-08-04",
        "Quarto",
        -7,
        "Diversos",
        "poupança família",
    )

    accounts = [
        str(row["account"])
        for row in repo.q("SELECT account FROM transactions ORDER BY id")
    ]
    assert accounts == [
        "Conta principal",
        "Conta principal",
        "Poupança Família",
        "Poupança Família",
    ]
    assert [str(row["name"]) for row in repo.financial_accounts()] == [
        "Conta principal",
        "Poupança Família",
    ]


def test_account_migration_merges_variants_and_preserves_known_balance(
    repo: Repo,
) -> None:
    connection = repo.db.conn
    connection.execute("DROP INDEX IF EXISTS idx_financial_accounts_account_key")
    connection.execute("DELETE FROM transactions")
    connection.execute("DELETE FROM financial_accounts")
    connection.execute(
        "INSERT INTO financial_accounts(name,account_key,reference_balance,reference_date,balance_known,updated_at) "
        "VALUES('Banco X','banco x',100,'2026-01-01',1,'2026-01-01 10:00:00')"
    )
    connection.execute(
        "INSERT INTO financial_accounts(name,account_key,reference_balance,reference_date,balance_known,updated_at) "
        "VALUES(' banco   x ','',250,'2026-07-31',1,'2026-08-01 10:00:00')"
    )
    connection.execute(
        "INSERT INTO transactions(date,description,amount,category,account,kind) "
        "VALUES('2026-08-01','Teste',-1,'Diversos',' banco   x ','expense')"
    )

    repo.db._migration_canonical_account_identity()
    connection.commit()

    rows = repo.q("SELECT * FROM financial_accounts WHERE account_key='banco x'")
    assert len(rows) == 1
    assert rows[0]["name"] == "Banco X"
    assert float(rows[0]["reference_balance"]) == 250
    assert repo.q("SELECT account FROM transactions")[0]["account"] == "Banco X"


def test_duplicate_evidence_has_three_levels_and_import_skips_only_certain(
    repo: Repo,
) -> None:
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    existing = repo.add_transaction(
        "2026-08-02",
        "Café Central",
        -4.5,
        "Restaurantes",
        "Conta principal",
    )
    repo.db.conn.execute(
        "INSERT INTO transaction_meta(transaction_id,payment_date,external_reference) "
        "VALUES(?,?,?)",
        (existing, "2026-08-02", "REF-001"),
    )
    repo.db.conn.commit()

    assert (
        repo.duplicate_classification(
            "2026-08-02", "Café Central", -4.5, "conta principal", "REF-001"
        )["level"]
        == "certain"
    )
    assert (
        repo.duplicate_classification(
            "2026-08-02", "  café central ", -4.5, "Conta principal"
        )["level"]
        == "probable"
    )
    assert (
        repo.duplicate_classification(
            "2026-08-02", "Pastelaria diferente", -4.5, "Conta principal"
        )["level"]
        == "possible"
    )

    service = ImportService(repo)
    mapping = {
        "date": "Data",
        "desc": "Descrição",
        "amount": "Valor",
        "category": "",
        "account": "Conta",
        "method": "",
        "merchant": "",
        "reference": "Referência",
    }
    rows = [
        {
            "Data": "2026-08-10",
            "Descrição": "A",
            "Valor": "-10,00",
            "Conta": "Conta principal",
            "Referência": "BATCH-1",
        },
        {
            "Data": "2026-08-10",
            "Descrição": "A",
            "Valor": "-10,00",
            "Conta": "conta principal",
            "Referência": "BATCH-1",
        },
        {
            "Data": "2026-08-11",
            "Descrição": "Compra igual",
            "Valor": "-12,00",
            "Conta": "Conta principal",
            "Referência": "",
        },
        {
            "Data": "2026-08-11",
            "Descrição": "Compra igual",
            "Valor": "-12,00",
            "Conta": "Conta principal",
            "Referência": "",
        },
        {
            "Data": "2026-08-11",
            "Descrição": "Compra diferente",
            "Valor": "-12,00",
            "Conta": "Conta principal",
            "Referência": "",
        },
    ]
    preview = service.preview(rows, mapping, apply_rules=False)
    assert preview.certain_duplicates == 1
    assert preview.probable_duplicates == 1
    assert preview.possible_duplicates == 1

    result = service.import_rows(
        rows,
        mapping,
        apply_rules=False,
        ignore_duplicates=True,
    )
    assert result.created == 4
    assert result.skipped_duplicates == 1


def test_validated_inline_update_is_journalled_and_rejects_bad_values(
    repo: Repo,
) -> None:
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("DELETE FROM change_journal")
    repo.db.conn.commit()
    transaction_id = repo.add_transaction(
        "2026-08-03", "Original", -15, "Diversos", "Conta principal"
    )
    journal = ChangeJournalService(repo)
    journal.run_with_snapshot(
        [transaction_id],
        lambda: repo.update_transaction_fields(
            transaction_id,
            {"description": "Atualizado", "category": "Supermercado"},
            commit=False,
        ),
        label="Edição direta",
        action_type="inline_edit",
    )
    assert repo.transaction_by_id(transaction_id)["description"] == "Atualizado"
    assert journal.latest_available()["action_type"] == "inline_edit"

    with pytest.raises(ValueError, match="zero"):
        repo.update_transaction_fields(transaction_id, {"amount": 0})
    with pytest.raises(ValueError, match="categorias de despesas"):
        repo.update_transaction_fields(
            transaction_id,
            {"category": "Rendimentos"},
        )
    with pytest.raises(ValueError, match="não existe"):
        repo.update_transaction_fields(transaction_id, {"category": "Livre"})


def test_category_selector_filters_kind_and_flags_legacy_rows(
    repo: Repo,
    qapp: QApplication,
) -> None:
    dialog = TransactionDialog(repo)
    expense_categories = {
        str(dialog.category.itemData(index) or dialog.category.itemText(index))
        for index in range(dialog.category.count())
    }
    assert "Diversos" in expense_categories
    assert "Rendimentos" not in expense_categories
    dialog.kind.setCurrentText("Receita")
    income_categories = {
        str(dialog.category.itemData(index) or dialog.category.itemText(index))
        for index in range(dialog.category.count())
    }
    assert "Rendimentos" in income_categories
    assert "Diversos" not in income_categories
    assert "Receita" not in income_categories
    assert dialog.category_warning.isHidden()
    dialog.close()

    transaction_id = repo.add_transaction(
        "2026-08-04", "Antigo", 20, "Supermercado", "Conta principal"
    )
    # Modern writes disambiguate categories by kind. Inject a historical row
    # at this UI boundary to exercise its incompatible-category warning.
    legacy_row = dict(repo.transaction_by_id(transaction_id))
    legacy_row["category"] = "Supermercado"
    legacy = TransactionDialog(repo, row=legacy_row)
    assert legacy._selected_category() == "Supermercado"
    assert not legacy.category_warning.isHidden()
    legacy.close()


def test_same_name_donut_items_retain_internal_holding_id(qapp: QApplication) -> None:
    chart = DonutChart(lambda: Theme())
    chart.set_category_limit(None)
    chart.set_data(
        [
            {"id": 11, "label": "ETF", "value": 100, "color": "#0072B2"},
            {"id": 22, "label": "ETF", "value": 80, "color": "#E69F00"},
        ]
    )
    assert len(chart.data) == 2
    assert [item["id"] for item in chart._detail_items] == [11, 22]


class _MemorySecretStore:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def read(self) -> str:
        return self.value

    def write(self, secret: str) -> None:
        self.value = str(secret)

    def delete(self) -> None:
        self.value = ""


def test_ai_database_settings_are_atomic(repo: Repo) -> None:
    before = repo.ai_settings()
    repo.db.conn.execute(
        """
        CREATE TRIGGER fail_ai_preferences
        BEFORE INSERT ON settings
        WHEN NEW.key='ai_gemini_model'
        BEGIN SELECT RAISE(ABORT, 'simulated AI settings failure'); END
        """
    )
    with pytest.raises(Exception, match="simulated AI settings failure"):
        repo.save_ai_settings(
            enabled=not before["enabled"],
            use_env=not before["use_env"],
            model="gemini-test",
            privacy_aggregated=not before["privacy_aggregated"],
            anonymize=not before["anonymize"],
            ask_before_details=not before["ask_before_details"],
        )
    after = repo.ai_settings()
    for key in (
        "enabled",
        "use_env",
        "model",
        "privacy_aggregated",
        "anonymize",
        "ask_before_details",
    ):
        assert after[key] == before[key]


def test_ai_credential_is_restored_when_atomic_settings_fail(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemorySecretStore("old-secret")
    service = AIService(
        repo,
        secret_store=store,
        backup_service=SimpleNamespace(
            scrub_ai_secret_from_backups=lambda: {"failed": 0}
        ),
        analytics_service=SimpleNamespace(),
        payments_service=SimpleNamespace(),
    )
    service._migration_checked = True
    service._stored_key_cache = "old-secret"

    def fail_save(**_values):
        raise RuntimeError("simulated settings failure")

    monkeypatch.setattr(repo, "save_ai_settings", fail_save)
    with pytest.raises(RuntimeError, match="simulated settings failure"):
        service.save_preferences(
            enabled=True,
            use_env=False,
            api_key="new-secret",
            model="gemini-test",
            privacy_aggregated=True,
            anonymize=True,
            ask_before_details=True,
        )
    assert store.read() == "old-secret"
    assert service._stored_key_cache == "old-secret"
