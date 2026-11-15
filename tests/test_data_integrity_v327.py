from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from database.engine import FinanceDB
from database.protection import DataIntegrityError
from database.repo import Repo
from services.backup import BackupService
from services.category_taxonomy import canonical_category
from services.change_journal import ChangeJournalService
from services.documents import DocumentService
from services.financial_intelligence import FinancialIntelligenceService
from services.imports import ImportService
from services.investment_performance import portfolio_performance_attribution
from services.secret_store import SecretStoreError
from utils.version import APP_VERSION


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "integrity-v327.sqlite3"))
    instance = Repo()
    yield instance
    instance.db.conn.close()


class _Pages(dict):
    def existing(self, key: str):
        return self.get(key)


def test_movement_save_invalidates_every_derived_page_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ui.pages.movements as movements_module
    from main import LiquidFinanceApp
    from ui.pages.movements import MovementsPage

    invalidations: list[int | None] = []
    replaced: list[tuple[list[object], int | None, dict[str, object]]] = []
    page = SimpleNamespace(
        repo=SimpleNamespace(safe_commit=lambda _message: True),
        _loader_task=None,
        _loaded_limit=200,
        _query_payload=lambda _limit: {"rows": ["fresh"], "summary": {"total": 1}},
        _replace_model_rows=lambda rows, selected, summary: replaced.append(
            (rows, selected, summary)
        ),
        refresh=lambda **_kwargs: None,
        app=SimpleNamespace(
            invalidate_after_movement_change=lambda **kwargs: invalidations.append(
                kwargs.get("selected_tx_id")
            )
        ),
    )
    monkeypatch.setattr(movements_module.QTimer, "singleShot", lambda *_args: None)

    assert MovementsPage.force_refresh_after_save(page, 41) is True
    assert replaced == [(["fresh"], 41, {"total": 1})]
    assert invalidations == [41]

    selected: list[int] = []
    messages: list[str] = []
    snapshots: list[bool] = []
    app = SimpleNamespace(
        _dirty_page_keys=set(),
        page_factories={
            "dashboard": object(),
            "movimentos": object(),
            "orcamentos": object(),
            "relatorios": object(),
        },
        _page_refreshed_at={"dashboard": 999.0, "movimentos": 0.0},
        repo=SimpleNamespace(
            record_financial_state_snapshot=lambda: snapshots.append(True)
        ),
        refresh_undo_actions=lambda: None,
        pages=_Pages(
            movimentos=SimpleNamespace(
                _select_transaction_row=lambda value: selected.append(value)
            )
        ),
        statusBar=lambda: SimpleNamespace(
            showMessage=lambda message, _timeout: messages.append(message)
        ),
    )

    LiquidFinanceApp.invalidate_after_movement_change(app, selected_tx_id=41)

    assert app._dirty_page_keys == {"dashboard", "orcamentos", "relatorios"}
    assert app._page_refreshed_at["movimentos"] > 0
    assert selected == [41]
    assert snapshots == [True]
    assert messages and "análises" in messages[-1]


def test_internal_transfer_is_linked_atomic_and_reversible(
    repo: Repo, tmp_path: Path
) -> None:
    debit_id, credit_id = repo.add_internal_transfer(
        "2026-08-01", 125.5, "Conta A", "Conta B"
    )
    rows = repo.q(
        "SELECT * FROM transactions WHERE id IN (?,?) ORDER BY amount",
        (debit_id, credit_id),
    )
    group_id = str(rows[0]["transfer_group_id"] or "")
    assert group_id
    assert {str(row["transfer_group_id"]) for row in rows} == {group_id}
    assert sum(float(row["amount"]) for row in rows) == pytest.approx(0)

    repo.db.conn.execute(
        f"""
        CREATE TRIGGER reject_transfer_credit_update
        BEFORE UPDATE ON transactions
        WHEN OLD.id={credit_id}
        BEGIN
            SELECT RAISE(ABORT, 'simulated linked transfer failure');
        END
        """
    )
    repo.db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="linked transfer failure"):
        repo.update_internal_transfer(group_id, "2026-08-02", 200, "Conta C", "Conta D")
    unchanged = repo.q(
        "SELECT date,amount,account FROM transactions "
        "WHERE transfer_group_id=? ORDER BY amount",
        (group_id,),
    )
    assert [str(row["date"]) for row in unchanged] == ["2026-08-01"] * 2
    assert [str(row["account"]) for row in unchanged] == ["Conta A", "Conta B"]

    repo.db.conn.execute("DROP TRIGGER reject_transfer_credit_update")
    repo.db.conn.commit()
    repo.update_internal_transfer(group_id, "2026-08-02", 200, "Conta C", "Conta D")
    updated = repo.q(
        "SELECT amount,account FROM transactions "
        "WHERE transfer_group_id=? ORDER BY amount",
        (group_id,),
    )
    assert [float(row["amount"]) for row in updated] == [-200, 200]
    assert [str(row["account"]) for row in updated] == ["Conta C", "Conta D"]
    with pytest.raises(ValueError, match="transferência ligada"):
        repo.update_transaction(debit_id, amount=-99)

    journal = ChangeJournalService(
        repo,
        attachments_directory=tmp_path / "anexos",
        journal_storage=tmp_path / "historico",
    )
    removed, _preserved = journal.delete_transactions(
        [debit_id], label="Eliminar transferência"
    )
    assert removed == 2
    assert (
        repo.q("SELECT id FROM transactions WHERE transfer_group_id=?", (group_id,))
        == []
    )
    assert journal.undo_latest() is not None
    restored = repo.q(
        "SELECT transfer_group_id,amount FROM transactions "
        "WHERE transfer_group_id=? ORDER BY amount",
        (group_id,),
    )
    assert len(restored) == 2
    assert sum(float(row["amount"]) for row in restored) == pytest.approx(0)


def test_legacy_transfer_migration_does_not_guess_ambiguous_pairs(repo: Repo) -> None:
    ids: list[int] = []
    for _index in range(2):
        ids.append(
            repo.add_transaction(
                "2026-01-10",
                "Transferência para Conta B",
                -50,
                "Transferência Interna",
                "Conta A",
            )
        )
        ids.append(
            repo.add_transaction(
                "2026-01-10",
                "Transferência de Conta A",
                50,
                "Transferência Interna",
                "Conta B",
            )
        )
    repo.db._migration_financial_integrity_links()
    placeholders = ",".join("?" for _ in ids)
    linked = repo.q(
        f"SELECT transfer_group_id FROM transactions WHERE id IN ({placeholders})",
        tuple(ids),
    )
    assert all(not str(row["transfer_group_id"] or "") for row in linked)


def test_net_worth_uses_reference_balance_and_avoids_double_counting(
    repo: Repo,
) -> None:
    for table in (
        "transactions",
        "holdings",
        "liabilities",
        "subscriptions",
        "goals",
        "financial_accounts",
        "investment_snapshots",
        "financial_state_snapshots",
    ):
        repo.db.conn.execute(f"DELETE FROM {table}")
    repo.db.conn.commit()

    repo.set_financial_account_reference(
        "Conta principal",
        1_000,
        "2026-01-31",
        balance_known=True,
    )
    repo.add_transaction(
        "2026-01-15", "Histórico anterior", 500, "Rendimentos", "Conta principal"
    )
    repo.add_transaction(
        "2026-02-03", "Entrada posterior", 200, "Rendimentos", "Conta principal"
    )
    repo.add_transaction(
        "2026-02-04", "Saída posterior", -50, "Diversos", "Conta principal"
    )
    repo.add_holding(
        "Saldo bancário",
        "Depósito",
        "Banco",
        1_150,
        1_150,
        cost_basis_known=True,
        linked_account="Conta principal",
    )
    repo.add_holding(
        "ETF global",
        "ETF",
        "Corretora",
        2_000,
        1_500,
        cost_basis_known=True,
    )

    snapshot = FinancialIntelligenceService(repo).net_worth_snapshot()

    assert snapshot["liquid_assets"] == pytest.approx(1_150)
    assert snapshot["investment_assets"] == pytest.approx(2_000)
    assert snapshot["total_assets"] == pytest.approx(3_150)
    quality = snapshot["data_quality"]
    assert quality["account_coverage_pct"] == pytest.approx(100)
    assert quality["label"] == "Completa"
    assert quality["linked_holdings"] == ["Saldo bancário"]
    account = next(
        row for row in snapshot["account_rows"] if row["account"] == "Conta principal"
    )
    assert account["source"] == "reference_balance"
    assert account["balance"] == pytest.approx(1_150)


class _ImportRepo:
    def suggested_category_for_description(self, _description: str) -> str:
        return ""

    def setting(self, _key: str, default: str = "") -> str:
        return default

    def set_setting(self, _key: str, _value: str) -> None:
        return None

    def is_duplicate_transaction(self, *_args) -> bool:
        return False


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Alimentação", "Supermercado"),
        ("Salário", "Rendimentos"),
        ("Outros", "Diversos"),
        ("Transportes", "Transporte"),
        ("Subscricao", "Subscrições"),
        ("Categoria personalizada", "Categoria personalizada"),
    ],
)
def test_import_categories_use_the_application_taxonomy(
    source: str, expected: str
) -> None:
    assert canonical_category(source) == expected
    service = ImportService(_ImportRepo())
    normalized = service.normalize_row(
        {
            "Data": "2026-08-01",
            "Descrição": "Movimento",
            "Valor": "-10,50",
            "Categoria": source,
        },
        {
            "date": "Data",
            "desc": "Descrição",
            "amount": "Valor",
            "category": "Categoria",
            "account": "",
            "method": "",
            "merchant": "",
            "reference": "",
        },
        apply_rules=False,
    )
    assert normalized.is_valid
    assert normalized.category == expected


def test_integrity_migration_repairs_only_identifiable_imported_categories(
    repo: Repo,
) -> None:
    imported_id = repo.add_transaction(
        "2026-08-01",
        "Importado",
        -20,
        "Outros",
        "Conta principal",
        notes="Importado por CSV",
    )
    manual_id = repo.add_transaction(
        "2026-08-02",
        "Manual",
        -15,
        "Outros",
        "Conta principal",
    )
    repo.save_transaction_splits(imported_id, [("Alimentação", 100.0)])

    repo.db._migration_financial_integrity_links()

    assert repo.transaction_by_id(imported_id)["category"] == "Diversos"
    # New writes are canonicalised immediately; the migration therefore has
    # nothing left to repair on this manually added row.
    assert repo.transaction_by_id(manual_id)["category"] == "Diversos"
    split = repo.db.conn.execute(
        "SELECT category FROM transaction_splits WHERE transaction_id=?",
        (imported_id,),
    ).fetchone()
    assert split["category"] == "Supermercado"


def test_document_audit_reuses_fingerprints_and_cancel_writes_no_partial_batch(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction_id = repo.add_transaction(
        "2026-08-01", "Documento", -20, "Diversos", "Conta principal"
    )
    service = DocumentService(repo, attachments_directory=tmp_path / "anexos")
    source = tmp_path / "fatura.txt"
    source.write_text("documento financeiro", encoding="utf-8")
    service.attach_file(transaction_id, source)
    row = repo.transaction_attachments(transaction_id)[0]
    attachment_id = int(row["id"])

    original_digest = service._file_digest
    digest_calls: list[Path] = []

    def tracking_digest(path: Path, cancelled=None) -> str:
        digest_calls.append(Path(path))
        return original_digest(path, cancelled)

    monkeypatch.setattr(service, "_file_digest", tracking_digest)
    progress: list[tuple[int, int, str]] = []
    audit = service.audit_storage_operation(
        progress_callback=lambda current, total, detail: progress.append(
            (current, total, detail)
        )
    )()
    assert audit["healthy"] is True
    assert digest_calls == []
    assert progress and progress[-1][0] == progress[-1][1]

    repo.db.conn.execute(
        "UPDATE transaction_attachments SET content_sha256='',file_size=0,"
        "file_mtime_ns=0,last_verified_at='' WHERE id=?",
        (attachment_id,),
    )
    repo.db.conn.commit()
    service.invalidate_audit_cache()
    operation = None

    def cancel_immediately(_current: int, _total: int, _detail: str) -> None:
        assert operation is not None
        operation.cancel()

    operation = service.audit_storage_operation(
        force=True,
        progress_callback=cancel_immediately,
    )
    with pytest.raises(RuntimeError, match="cancelada"):
        operation()
    unchanged = repo.transaction_attachment(attachment_id)
    assert str(unchanged["content_sha256"] or "") == ""
    assert int(unchanged["file_size"] or 0) == 0


class _SecretStore:
    def __init__(self, value: str = "", *, fail_on: str = "") -> None:
        self.value = value
        self.fail_on = fail_on

    def read(self) -> str:
        return self.value

    def write(self, secret: str) -> None:
        if self.fail_on and str(secret) == self.fail_on:
            raise SecretStoreError("simulated credential failure")
        self.value = str(secret)

    def delete(self) -> None:
        self.value = ""


def _backup_service(
    repo: Repo,
    tmp_path: Path,
    current_store: _SecretStore,
    history_store: _SecretStore,
) -> BackupService:
    return BackupService(
        repo,
        database_file=repo.db.path,
        backup_directory=tmp_path / "backups",
        attachments_directory=tmp_path / "anexos",
        history_directory=tmp_path / "historico",
        preferences_file=tmp_path / "backup_preferences.json",
        secret_store=current_store,
        password_history_store=history_store,
    )


def test_backup_password_rotation_preserves_access_to_old_backups(
    repo: Repo, tmp_path: Path
) -> None:
    current = _SecretStore()
    history = _SecretStore()
    service = _backup_service(repo, tmp_path, current, history)
    old_password = "palavra-passe-antiga"
    new_password = "palavra-passe-nova"
    service.save_backup_password(old_password)
    service.save_preferences(0, 5, False, True, False, "")
    old_backup = service.create_manual_backup()

    rotation_backup = service.rotate_backup_password(
        old_password,
        new_password,
        preference_values=(0, 5, False, True, False, ""),
    )

    assert current.value == new_password
    assert old_password in json.loads(history.value)
    assert service.validate_backup_file(rotation_backup, password=new_password)
    assert service.validate_backup_file(old_backup)
    with pytest.raises(ValueError, match="atual não está correta"):
        service.rotate_backup_password("errada", "outra-palavra-passe")
    assert current.value == new_password


def test_backup_password_rotation_rolls_back_before_reporting_failure(
    repo: Repo, tmp_path: Path
) -> None:
    old_password = "palavra-passe-antiga"
    new_password = "palavra-passe-nova"
    current = _SecretStore(old_password, fail_on=new_password)
    history = _SecretStore()
    service = _backup_service(repo, tmp_path, current, history)
    service.save_preferences(60, 7, True, False, False, "")
    before = service.preferences()

    with pytest.raises(SecretStoreError, match="credential failure"):
        service.rotate_backup_password(
            old_password,
            new_password,
            preference_values=(120, 9, False, True, False, ""),
        )

    assert current.value == old_password
    assert history.value == ""
    assert list((tmp_path / "backups").glob("*rotacao_password*.lgbak")) == []
    after = service.preferences()
    for key in (
        "auto_backup_interval_minutes",
        "backup_keep",
        "backup_on_close",
        "backup_encryption_enabled",
        "secondary_backup_enabled",
        "secondary_backup_directory",
    ):
        assert after[key] == before[key]


def test_missing_migration_below_maximum_is_blocked_without_mutation(
    tmp_path: Path,
) -> None:
    database_file = tmp_path / "dados" / "financas.sqlite3"
    kwargs = {
        "app_version": APP_VERSION,
        "attachments_directory": tmp_path / "dados" / "anexos",
        "history_directory": tmp_path / "dados" / "historico",
        "preferences_file": tmp_path / "dados" / "backup_preferences.json",
        "protection_directory": tmp_path / "dados" / "protecoes",
    }
    database = FinanceDB(database_file, **kwargs)
    database.conn.execute("DELETE FROM schema_migrations WHERE version=17")
    database.conn.commit()
    assert database.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION
    assert 17 not in database.schema_versions()
    database.conn.close()

    # A gap is evidence of unreviewed schema drift, not permission to rerun DDL.
    before = database_file.read_bytes()
    with pytest.raises(DataIntegrityError, match="migrações incompleta"):
        FinanceDB(database_file, **kwargs)
    assert database_file.read_bytes() == before
    connection = sqlite3.connect(database_file)
    try:
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        assert 17 not in versions
        assert max(versions) == FinanceDB.CURRENT_SCHEMA_VERSION
    finally:
        connection.close()


def test_unknown_investment_cost_is_excluded_and_coverage_is_visible() -> None:
    result = portfolio_performance_attribution(
        [
            {
                "id": 1,
                "name": "ETF conhecido",
                "value": 1_200,
                "cost_basis": 1_000,
                "cost_basis_known": 1,
                "fees": 0,
            },
            {
                "id": 2,
                "name": "Ativo sem custo",
                "value": 500,
                "cost_basis": 0,
                "cost_basis_known": 0,
                "fees": 0,
            },
            {
                "id": 3,
                "name": "Custo desconhecido",
                "value": 300,
                "cost_basis": 0,
                "cost_basis_known": 0,
                "fees": 15,
            },
        ]
    )

    assert result["total_value"] == pytest.approx(2_000)
    assert result["known_value"] == pytest.approx(1_200)
    assert result["data_coverage_pct"] == pytest.approx(60)
    assert result["net_return_pct"] == pytest.approx(20)
    assert result["holding_count_known"] == 1
    unavailable = [row for row in result["rows"] if not row["performance_available"]]
    assert len(unavailable) == 2
    assert all(row["net_gain"] is None for row in unavailable)
    assert all(row["net_return_pct"] is None for row in unavailable)
    assert all(row["contribution_pp"] is None for row in unavailable)
