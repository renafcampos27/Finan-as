from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.protection import (
    DATA_GUARD_BACKUP_KEY,
    DATA_GUARD_VERSION_KEY,
    DataCompatibilityError,
    DataIntegrityError,
    DataPreservationError,
    DataProtectionError,
    DataProtectionGuard,
)
from utils import config


OLD_VERSION = "3.0.9"
NEW_VERSION = "3.1.0"


def _paths(root: Path) -> dict[str, Path]:
    return {
        "database": root / "financas.sqlite3",
        "attachments": root / "anexos",
        "history": root / "historico_alteracoes",
        "preferences": root / "backup_preferences.json",
        "protections": root / "protecoes_de_versao",
    }


def _open_database(paths: dict[str, Path], version: str) -> FinanceDB:
    return FinanceDB(
        paths["database"],
        app_version=version,
        attachments_directory=paths["attachments"],
        history_directory=paths["history"],
        preferences_file=paths["preferences"],
        protection_directory=paths["protections"],
    )


def _create_user_data(root: Path) -> tuple[dict[str, Path], int]:
    paths = _paths(root)
    database = _open_database(paths, OLD_VERSION)
    cursor = database.conn.execute(
        """
        INSERT INTO transactions(
            date, description, amount, category, account, kind, notes
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            "2026-07-26",
            "Movimento pessoal que não pode desaparecer",
            -42.75,
            "Outros",
            "Conta principal",
            "expense",
            "informação fornecida pelo utilizador",
        ),
    )
    transaction_id = int(cursor.lastrowid)
    attachment = paths["attachments"] / str(transaction_id) / "fatura.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("documento pessoal intacto", encoding="utf-8")
    database.conn.execute(
        """
        INSERT INTO transaction_attachments(
            transaction_id, file_name, stored_name, stored_path,
            original_path, file_type, description
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            transaction_id,
            "fatura.txt",
            "fatura.txt",
            str(attachment),
            "",
            "TXT",
            "Documento pessoal",
        ),
    )
    database.conn.commit()
    database.conn.close()

    paths["history"].mkdir(parents=True)
    (paths["history"] / "alteracao.json").write_text(
        '{"acao":"teste"}',
        encoding="utf-8",
    )
    paths["preferences"].write_text(
        '{"interval":"weekly","keep":7}',
        encoding="utf-8",
    )
    return paths, transaction_id


def _setting(database_file: Path, key: str) -> str:
    connection = sqlite3.connect(str(database_file))
    try:
        row = connection.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
        ).fetchone()
        return str(row[0]) if row else ""
    finally:
        connection.close()


def test_holding_fee_migration_preserves_existing_portfolio_values() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT 'Manual',
            value REAL NOT NULL,
            cost_basis REAL NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        INSERT INTO holdings(
            name, asset_class, account, value, cost_basis, notes
        ) VALUES(?,?,?,?,?,?)
        """,
        ("ETF pessoal", "ETF", "Corretora", 12_345.67, 10_000.25, "intacto"),
    )
    database = FinanceDB.__new__(FinanceDB)
    database.conn = connection

    database._migration_holding_fees()
    database._migration_holding_fees()
    row = connection.execute("SELECT * FROM holdings").fetchone()

    assert row["name"] == "ETF pessoal"
    assert row["value"] == pytest.approx(12_345.67)
    assert row["cost_basis"] == pytest.approx(10_000.25)
    assert row["notes"] == "intacto"
    assert row["fees"] == 0
    connection.close()


def test_upgrade_creates_verified_snapshot_before_preserving_all_user_data(
    tmp_path: Path,
) -> None:
    paths, transaction_id = _create_user_data(tmp_path)

    upgraded = _open_database(paths, NEW_VERSION)
    row = upgraded.conn.execute(
        "SELECT description, amount, notes FROM transactions WHERE id=?",
        (transaction_id,),
    ).fetchone()
    upgraded.conn.close()

    assert tuple(row) == (
        "Movimento pessoal que não pode desaparecer",
        -42.75,
        "informação fornecida pelo utilizador",
    )
    assert (paths["attachments"] / str(transaction_id) / "fatura.txt").read_text(
        encoding="utf-8"
    ) == "documento pessoal intacto"
    archives = sorted(paths["protections"].glob("antes_atualizacao_*.zip"))
    assert len(archives) == 1

    manifest = DataProtectionGuard.validate_archive(archives[0])
    assert manifest["source_app_version"] == OLD_VERSION
    assert manifest["target_app_version"] == NEW_VERSION
    assert manifest["tables"]["transactions"] >= 1
    assert {entry["scope"] for entry in manifest["files"]} == {
        "attachments",
        "history",
        "preferences",
    }
    with zipfile.ZipFile(archives[0], "r") as archive:
        assert (
            archive.read(f"anexos/{transaction_id}/fatura.txt").decode("utf-8")
            == "documento pessoal intacto"
        )
        protected_db = tmp_path / "protected.sqlite3"
        protected_db.write_bytes(archive.read("financas_pessoais.sqlite3"))
    protected_connection = sqlite3.connect(str(protected_db))
    try:
        protected_row = protected_connection.execute(
            "SELECT description FROM transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()
    finally:
        protected_connection.close()
    assert protected_row == ("Movimento pessoal que não pode desaparecer",)
    assert _setting(paths["database"], DATA_GUARD_VERSION_KEY) == NEW_VERSION
    assert Path(_setting(paths["database"], DATA_GUARD_BACKUP_KEY)) == archives[0]

    reopened = _open_database(paths, NEW_VERSION)
    reopened.conn.close()
    assert len(list(paths["protections"].glob("antes_atualizacao_*.zip"))) == 1


def test_database_from_before_the_guard_gets_a_legacy_snapshot(
    tmp_path: Path,
) -> None:
    paths, transaction_id = _create_user_data(tmp_path)
    connection = sqlite3.connect(str(paths["database"]))
    try:
        connection.execute("DELETE FROM settings WHERE key LIKE 'data_guard_%'")
        connection.commit()
    finally:
        connection.close()

    upgraded = _open_database(paths, NEW_VERSION)
    description = upgraded.conn.execute(
        "SELECT description FROM transactions WHERE id=?",
        (transaction_id,),
    ).fetchone()[0]
    upgraded.conn.close()

    archives = list(
        paths["protections"].glob("antes_atualizacao_vlegado_para_v3.1.0_*.zip")
    )
    assert len(archives) == 1
    assert description == "Movimento pessoal que não pode desaparecer"
    assert _setting(paths["database"], DATA_GUARD_VERSION_KEY) == NEW_VERSION


def test_failed_upgrade_automatically_restores_rows_documents_and_preferences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, transaction_id = _create_user_data(tmp_path)

    def mutate_existing_user_data(database: FinanceDB) -> None:
        database.conn.execute(
            "UPDATE transactions SET description='alterado pela atualização' "
            "WHERE id=?",
            (transaction_id,),
        )
        database.conn.commit()
        (paths["attachments"] / str(transaction_id) / "fatura.txt").write_text(
            "alterado", encoding="utf-8"
        )
        paths["preferences"].write_text('{"keep":0}', encoding="utf-8")

    monkeypatch.setattr(
        FinanceDB,
        "ensure_reference_data",
        mutate_existing_user_data,
    )
    with pytest.raises(DataPreservationError):
        _open_database(paths, NEW_VERSION)

    connection = sqlite3.connect(str(paths["database"]))
    try:
        row = connection.execute(
            "SELECT description, notes FROM transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row == (
        "Movimento pessoal que não pode desaparecer",
        "informação fornecida pelo utilizador",
    )
    assert (paths["attachments"] / str(transaction_id) / "fatura.txt").read_text(
        encoding="utf-8"
    ) == "documento pessoal intacto"
    assert json.loads(paths["preferences"].read_text(encoding="utf-8"))["keep"] == 7
    assert not list(paths["protections"].glob("atualizacao_em_curso_*.json"))


def test_unexpected_initialisation_error_is_reported_after_safe_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, transaction_id = _create_user_data(tmp_path)

    def fail_initialisation(_database: FinanceDB) -> None:
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(
        FinanceDB,
        "ensure_reference_data",
        fail_initialisation,
    )
    with pytest.raises(
        DataProtectionError,
        match="estado anterior foi reposto",
    ):
        _open_database(paths, NEW_VERSION)

    connection = sqlite3.connect(str(paths["database"]))
    try:
        description = connection.execute(
            "SELECT description FROM transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert description == "Movimento pessoal que não pode desaparecer"


def test_failed_rollback_keeps_the_recovery_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _transaction_id = _create_user_data(tmp_path)
    guard = DataProtectionGuard(
        paths["database"],
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        current_app_version=NEW_VERSION,
        attachments_directory=paths["attachments"],
        history_directory=paths["history"],
        preferences_file=paths["preferences"],
        protection_directory=paths["protections"],
    )
    session = guard.prepare()
    assert guard.incomplete_marker.is_file()

    def fail_restore(_path: Path) -> None:
        raise DataIntegrityError("falha simulada ao repor")

    monkeypatch.setattr(guard, "restore_archive", fail_restore)
    with pytest.raises(DataIntegrityError):
        session.rollback()

    assert guard.incomplete_marker.is_file()


def test_interrupted_upgrade_is_recovered_before_the_next_write(
    tmp_path: Path,
) -> None:
    paths, transaction_id = _create_user_data(tmp_path)
    guard = DataProtectionGuard(
        paths["database"],
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        current_app_version=NEW_VERSION,
        attachments_directory=paths["attachments"],
        history_directory=paths["history"],
        preferences_file=paths["preferences"],
        protection_directory=paths["protections"],
    )
    interrupted_session = guard.prepare()
    assert interrupted_session.backup_path is not None
    assert guard.incomplete_marker.is_file()

    connection = sqlite3.connect(str(paths["database"]))
    try:
        connection.execute(
            "UPDATE transactions SET description='escrita interrompida' WHERE id=?",
            (transaction_id,),
        )
        connection.commit()
    finally:
        connection.close()
    (paths["attachments"] / str(transaction_id) / "fatura.txt").write_text(
        "ficheiro interrompido", encoding="utf-8"
    )

    recovered = _open_database(paths, NEW_VERSION)
    row = recovered.conn.execute(
        "SELECT description FROM transactions WHERE id=?",
        (transaction_id,),
    ).fetchone()
    recovered.conn.close()

    assert tuple(row) == ("Movimento pessoal que não pode desaparecer",)
    assert (paths["attachments"] / str(transaction_id) / "fatura.txt").read_text(
        encoding="utf-8"
    ) == "documento pessoal intacto"
    assert not guard.incomplete_marker.exists()


def test_older_app_and_corrupt_database_are_refused_without_writes(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path / "newer")
    newer = _open_database(paths, "9.0.0")
    newer.conn.close()
    before = paths["database"].read_bytes()

    with pytest.raises(DataCompatibilityError):
        _open_database(paths, NEW_VERSION)
    assert paths["database"].read_bytes() == before
    assert not list(paths["protections"].glob("antes_atualizacao_*.zip"))

    schema_paths = _paths(tmp_path / "newer_schema")
    current = _open_database(schema_paths, NEW_VERSION)
    current.conn.execute(
        "INSERT INTO schema_migrations(version,name) VALUES(999,'future')"
    )
    current.conn.commit()
    current.conn.close()
    schema_before = schema_paths["database"].read_bytes()
    with pytest.raises(DataCompatibilityError):
        _open_database(schema_paths, NEW_VERSION)
    assert schema_paths["database"].read_bytes() == schema_before

    corrupt_paths = _paths(tmp_path / "corrupt")
    corrupt_paths["database"].parent.mkdir(parents=True)
    corrupt_paths["database"].write_bytes(b"not a sqlite database")
    corrupt_before = corrupt_paths["database"].read_bytes()
    with pytest.raises(DataIntegrityError):
        _open_database(corrupt_paths, NEW_VERSION)
    assert corrupt_paths["database"].read_bytes() == corrupt_before

    blocked_paths, _transaction_id = _create_user_data(tmp_path / "blocked")
    blocked_paths["protections"].parent.mkdir(parents=True, exist_ok=True)
    blocked_paths["protections"].write_text("not a directory", encoding="utf-8")
    blocked_before = blocked_paths["database"].read_bytes()
    with pytest.raises(DataProtectionError):
        _open_database(blocked_paths, NEW_VERSION)
    assert blocked_paths["database"].read_bytes() == blocked_before


def test_default_protection_folder_is_outside_the_program_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = tmp_path / "programa"
    local_data = tmp_path / "dados_locais"
    monkeypatch.setattr(config, "app_dir", lambda: program)
    monkeypatch.delenv("FINANCAS_PESSOAIS_STATE_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_data))

    target = config.version_protection_dir(
        program / config.DEFAULT_DB_NAME,
    )

    assert target == local_data / "FinancasPessoais" / "protecoes_de_versao"
    assert not target.is_relative_to(program)
