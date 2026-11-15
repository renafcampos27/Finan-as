#!/usr/bin/env python3
"""Offline smoke checks for CSV import and atomic batch persistence."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB
from database.repo import Repo
from services.imports import ImportService


class FakeRepo:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.existing = {("2026-06-01", "já existe", -10.0, "principal", "")}
        self.imported: list[dict[str, Any]] = []

    def setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = str(value)

    def suggested_category_for_description(self, description: str) -> str:
        return "Saúde" if "farmácia" in description.casefold() else ""

    def is_duplicate_transaction(
        self,
        transaction_date: str,
        description: str,
        amount: float,
        account: str = "",
        external_reference: str = "",
    ) -> bool:
        signature = (
            transaction_date[:10],
            description.strip().casefold(),
            round(float(amount), 2),
            account.strip().casefold(),
            external_reference.strip().casefold(),
        )
        return signature in self.existing

    def import_transactions_batch(self, rows: list[dict[str, Any]]) -> list[int]:
        self.imported.extend(rows)
        return list(range(1, len(rows) + 1))


class ConnectionHolder(Repo):
    """Use real identity resolution and migrations, without demo transactions."""

    def __init__(self, path: Path) -> None:
        with patch.dict(os.environ, {"FINANCAS_PESSOAIS_DEMO_DATA": "0"}):
            super().__init__(FinanceDB(path))


def create_import_failure_trigger(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TRIGGER reject_failed_import
        BEFORE INSERT ON transactions
        WHEN NEW.description='FAIL'
        BEGIN
            SELECT RAISE(ABORT, 'simulated import failure');
        END;
        """)
    connection.commit()


def normalized_batch(description: str) -> dict[str, Any]:
    return {
        "date": "2026-06-10",
        "description": description,
        "amount": -15.5,
        "category": "Outros",
        "account": "Conta",
        "payment_method": "Cartão",
        "merchant": "Loja",
        "external_reference": "",
        "notes": "Importado por CSV",
    }


def test_invalid_or_missing_import_fields_require_review() -> None:
    service = ImportService(FakeRepo())
    mapping = {
        "date": "Data",
        "desc": "Descrição",
        "amount": "Valor",
        "category": "",
        "account": "",
        "method": "",
        "merchant": "",
    }

    invalid_date = service.normalize_row(
        {"Data": "31/02/2026", "Descrição": "Compra", "Valor": "-10"},
        mapping,
        apply_rules=False,
    )
    missing_description = service.normalize_row(
        {"Data": "16/07/2026", "Descrição": "", "Valor": "-10"},
        mapping,
        apply_rules=False,
    )

    assert invalid_date.date == ""
    assert not invalid_date.is_valid
    assert missing_description.description == ""
    assert not missing_description.is_valid
    assert "Data inválida" in invalid_date.validation_message
    assert "Descrição em falta" in missing_description.validation_message


def test_non_finite_and_malformed_amounts_are_visible_validation_errors() -> None:
    service = ImportService(FakeRepo())
    mapping = {
        "date": "Data",
        "desc": "Descrição",
        "amount": "Valor",
        "category": "",
        "account": "",
        "method": "",
        "merchant": "",
    }

    preview = service.preview(
        [
            {"Data": "2026-07-01", "Descrição": "NaN", "Valor": "NaN"},
            {"Data": "2026-07-02", "Descrição": "Infinito", "Valor": "inf"},
            {"Data": "2026-07-03", "Descrição": "Texto", "Valor": "abc"},
        ],
        mapping,
        apply_rules=False,
    )

    assert preview.invalid == 3
    assert all(row.status == "Rever" for row in preview.rows)
    assert all(
        row.reason and row.reason != "Pronto para importar." for row in preview.rows
    )


def assert_atomic_repo_batch(root: Path) -> None:
    database_path = root / "batch.sqlite3"
    holder = ConnectionHolder(database_path)
    connection = holder.db.conn

    try:
        create_import_failure_trigger(connection)
        try:
            Repo.import_transactions_batch(
                holder,
                [normalized_batch("Primeiro"), normalized_batch("FAIL")],
            )
        except sqlite3.IntegrityError as exc:
            assert "simulated import failure" in str(exc)
        else:
            raise AssertionError("Failing batch was committed")
        assert (
            connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM transaction_meta").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM financial_accounts WHERE name='Conta'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM entity_aliases WHERE entity_type='account' AND alias='Conta'"
            ).fetchone()[0]
            == 0
        )

        created = Repo.import_transactions_batch(
            holder,
            [normalized_batch("Primeiro"), normalized_batch("Segundo")],
        )
        assert len(created) == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM transaction_meta").fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM transactions t "
                "JOIN financial_accounts a ON a.stable_id=t.account_id "
                "JOIN categories c ON c.stable_id=t.category_id"
            ).fetchone()[0]
            == 2
        ), "Reimport after rollback must not reuse deleted identity IDs from cache"
    finally:
        connection.close()


def test_atomic_repo_batch(tmp_path: Path) -> None:
    assert_atomic_repo_batch(tmp_path)


def test_import_persists_real_account_and_category_identities(tmp_path: Path) -> None:
    holder = ConnectionHolder(tmp_path / "identities.sqlite3")
    try:
        account_id, account = holder.account_identity("  Conta  ")
        category_id, category = holder.category_identity(
            "Outros", create=True, kind="expense"
        )
        assert account_id.startswith("acc_")
        assert category_id.startswith("cat_")
        assert holder.account_identity("conta", create=False) == (account_id, account)
        assert holder.account_identity("Missing", create=False) == ("", "Missing")
        created = holder.import_transactions_batch(
            [normalized_batch("Primeiro"), normalized_batch("Segundo")]
        )
        rows = holder.db.conn.execute(
            "SELECT id,account_id,account,category_id,category FROM transactions ORDER BY id"
        ).fetchall()
        assert [row["id"] for row in rows] == created
        assert len(rows) == 2
        for row in rows:
            assert (row["account_id"], row["account"]) == (account_id, account)
            assert (row["category_id"], row["category"]) == (category_id, category)
        assert (
            holder.db.conn.execute(
                "SELECT COUNT(*) FROM financial_accounts WHERE stable_id=?",
                (account_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            holder.db.conn.execute(
                "SELECT COUNT(*) FROM categories WHERE stable_id=?", (category_id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        holder.db.conn.close()


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        csv_path = root / "movimentos.csv"
        csv_path.write_text(
            "Data;Descrição;Valor;Conta;Forma;Comerciante\n"
            "01/06/2026;Já existe;-10,00;Principal;Cartão;Loja A\n"
            "02/06/2026;Farmácia Central;(25,50);Principal;Cartão;Farmácia\n"
            "02/06/2026;Farmácia Central;(25,50);Principal;Cartão;Farmácia\n"
            "03/06/2026;Valor inválido;0,00;Principal;Numerário;Loja B\n",
            encoding="utf-8-sig",
        )

        repo = FakeRepo()
        service = ImportService(repo)
        data = service.read_csv(csv_path)
        assert len(data.rows) == 4
        mapping = service.suggested_mapping(data.headers)
        assert mapping["date"] == "Data"
        assert mapping["desc"] == "Descrição"
        assert mapping["amount"] == "Valor"

        service.save_mapping(mapping)
        assert service.load_mapping() == mapping
        assert service.parse_amount("1.234,56 €") == 1234.56
        assert service.parse_amount("1,234.56") == 1234.56
        assert service.parse_amount("1.234", "pt") == 1234.0
        assert service.parse_amount("(12,50)") == -12.5
        assert service.parse_amount("12,50-") == -12.5

        preview = service.preview(
            data.rows,
            mapping,
            apply_rules=True,
            limit=80,
            date_format="%d/%m/%Y",
        )
        assert preview.total == 4
        assert preview.duplicates == 2
        assert preview.certain_duplicates == 0
        assert preview.probable_duplicates == 2
        assert preview.possible_duplicates == 0
        assert preview.invalid == 1
        assert preview.rows[1].transaction.category == "Saúde"

        result = service.import_rows(
            data.rows,
            mapping,
            apply_rules=True,
            ignore_duplicates=True,
            date_format="%d/%m/%Y",
        )
        assert result.created == 3
        assert result.skipped_duplicates == 0
        assert result.skipped_invalid == 1
        assert repo.imported[1]["merchant"] == "Farmácia"
        assert repo.imported[1]["payment_method"] == "Cartão"

        assert_atomic_repo_batch(root)

    print("Import service smoke OK")
    print("csv_rows", 4)
    print("duplicates_detected", 2)
    print("atomic_rollback", True)
    print("created_rows", 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
