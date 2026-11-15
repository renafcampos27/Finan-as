from __future__ import annotations

import ast
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo.main_repo import Repo


def test_p0_settings_backup_health_task_uses_isolated_worker_connection() -> None:
    """Validate that SettingsPage uses run_verify_latest_backup_for_database."""
    settings_file = (
        Path(__file__).resolve().parents[1] / "ui" / "pages" / "settings.py"
    )
    source = settings_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(settings_file))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "start":
                if (
                    isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "backup_health_task"
                ):
                    for arg in node.args:
                        if isinstance(arg, ast.Attribute) and arg.attr == "verify_latest_backup_and_record":
                            pytest.fail(
                                "SettingsPage still passes self.backup_service.verify_latest_backup_and_record "
                                "directly to backup_health_task.start, violating SQLite thread-safety."
                            )

    assert "run_verify_latest_backup_for_database" in source


def test_p0_cash_balance_bidirectional_retroactive_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate that cash_balance anchors bidirectional reconciliation."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "p0_retro_cash.sqlite3")
    try:
        db.conn.execute(
            "INSERT INTO financial_accounts(name,reference_balance,reference_date,balance_known) "
            "VALUES('Millennium', 10000.0, '2026-03-01', 1)"
        )
        db.add_transaction(
            "2026-02-15", "Jantar", -200.0, "Alimentação", "Millennium", "expense"
        )
        db.add_transaction(
            "2026-02-20", "Freelance", 1000.0, "Rendimentos", "Millennium", "income"
        )
        db.add_transaction(
            "2026-03-05", "Renda", -800.0, "Habitação", "Millennium", "expense"
        )

        assert db.cash_balance("2026-03-05") == pytest.approx(9200.0)
        assert db.cash_balance("2026-03-01") == pytest.approx(10000.0)
        assert db.cash_balance("2026-02-28") == pytest.approx(10000.0)
        assert db.cash_balance("2026-02-18") == pytest.approx(9000.0)
        assert db.cash_balance("2026-02-01") == pytest.approx(9200.0)
        assert db.cash_balance("2026-01-01") == pytest.approx(9200.0)
    finally:
        db.prepare_for_shutdown()


def test_p0_repo_identity_account_balance_as_of_retroactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate that IdentityRepo._account_balance_as_of and account_balances match identically."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "p0_identity_repo.sqlite3")
    try:
        repo = Repo(db)
        repo.set_financial_account_reference(
            "ActivoBank", 5000.0, "2026-03-01", balance_known=True
        )
        db.add_transaction(
            "2026-02-10", "Subscrição", -50.0, "Tecnologia", "ActivoBank", "expense"
        )

        rows = repo.account_balances("2026-02-05")
        activo = next(r for r in rows if r["account"] == "ActivoBank")
        assert float(activo["balance"]) == pytest.approx(5050.0)
        assert activo["balance_known"] is True
        assert activo["source"] == "reference_balance"
        assert repo.cash_balance("2026-02-05") == pytest.approx(5050.0)
    finally:
        db.prepare_for_shutdown()


def test_p0_unknown_account_retains_partial_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate that an account without known balance aggregates movements from day zero."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "p0_unknown_account.sqlite3")
    try:
        repo = Repo(db)
        db.conn.execute(
            "INSERT INTO financial_accounts(name,reference_balance,reference_date,balance_known) "
            "VALUES('Conta Desconhecida', 0, '', 0)"
        )
        db.add_transaction(
            "2026-01-10", "Entrada", 300.0, "Outros", "Conta Desconhecida", "income"
        )
        db.add_transaction(
            "2026-02-10", "Saida", -100.0, "Outros", "Conta Desconhecida", "expense"
        )

        rows = repo.account_balances("2026-01-31")
        acc = next(r for r in rows if r["account"] == "Conta Desconhecida")
        assert float(acc["balance"]) == pytest.approx(300.0)
        assert acc["balance_known"] is False
        assert acc["source"] == "partial_history"

        rows_feb = repo.account_balances("2026-02-28")
        acc_feb = next(r for r in rows_feb if r["account"] == "Conta Desconhecida")
        assert float(acc_feb["balance"]) == pytest.approx(200.0)
        assert acc_feb["balance_known"] is False
    finally:
        db.prepare_for_shutdown()
