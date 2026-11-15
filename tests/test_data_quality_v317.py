from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from database.repo import Repo
from services.imports import ImportService
from services.payments import PaymentsService
from services.subscription_matching import subscription_history_linkage
from ui.components.charts.balance import BalanceChart
from ui.core.theme import Theme


@pytest.fixture
def repo(tmp_path: Path, monkeypatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "quality.sqlite3"))
    repository = Repo()
    yield repository
    repository.db.conn.close()


def _mapping() -> dict[str, str]:
    return {
        "date": "Data",
        "desc": "Descrição",
        "amount": "Valor",
        "category": "Categoria",
        "account": "Conta",
        "method": "",
        "merchant": "",
        "reference": "Referência",
    }


def _clear_financial_rows(repo) -> None:
    repo.db.conn.execute("DELETE FROM subscription_occurrences")
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()


def test_csv_encoding_is_detected_without_replacement(repo, tmp_path: Path) -> None:
    path = tmp_path / "extrato.csv"
    path.write_bytes("Data;Descrição;Valor\n01/08/2026;Café;-1,20 €\n".encode("cp1252"))

    data = ImportService(repo).read_csv(path)

    assert data.encoding == "cp1252"
    assert data.rows[0]["Descrição"] == "Café"
    assert data.warnings


def test_ambiguous_amount_requires_an_explicit_locale(repo) -> None:
    service = ImportService(repo)

    with pytest.raises(ValueError, match="ambíguo"):
        service.parse_amount("1.234")

    assert service.parse_amount("1.234", "pt") == 1234.0
    with pytest.raises(ValueError, match="mais de duas casas decimais"):
        service.parse_amount("1.234", "international")
    assert service.parse_amount("1.23", "international") == 1.23


def test_import_duplicate_identity_uses_account_and_bank_reference(repo) -> None:
    _clear_financial_rows(repo)
    service = ImportService(repo)
    rows = [
        {
            "Data": "01/08/2026",
            "Descrição": "Compra",
            "Valor": "-12,50",
            "Categoria": "Outros",
            "Conta": "Conta A",
            "Referência": "BANK-001",
        },
        {
            "Data": "01/08/2026",
            "Descrição": "Compra",
            "Valor": "-12,50",
            "Categoria": "Outros",
            "Conta": "Conta B",
            "Referência": "BANK-001",
        },
    ]

    result = service.import_rows(
        rows,
        _mapping(),
        apply_rules=False,
        ignore_duplicates=True,
        date_format="%d/%m/%Y",
    )

    assert result.created == 2
    assert repo.is_duplicate_transaction(
        "2026-08-15", "Nome diferente", -99, "Conta A", "BANK-001"
    )
    assert not repo.is_duplicate_transaction(
        "2026-08-15", "Nome diferente", -99, "Conta C", "BANK-001"
    )
    references = repo.q(
        "SELECT external_reference FROM transaction_meta ORDER BY transaction_id"
    )
    assert [row["external_reference"] for row in references] == ["BANK-001", "BANK-001"]


def test_subscription_aliases_link_exactly_and_leave_probable_unconfirmed(repo) -> None:
    _clear_financial_rows(repo)
    subscription_id = repo.add_subscription(
        "Serviço TV",
        12.0,
        "Subscrições",
        1,
        "Mensal",
        aliases="STREAMING LISBOA; STREAMING.PT",
    )
    repo.add_transaction(
        "2026-08-01", "STREAMING.PT", -12.0, "Subscrições", "Conta principal"
    )
    repo.add_transaction(
        "2026-08-02",
        "Serviço TV Portugal",
        -12.0,
        "Subscrições",
        "Conta principal",
    )

    summary = subscription_history_linkage(
        repo,
        [repo.subscription_by_id(subscription_id)],
    )

    assert summary.linked == 1
    assert summary.probable == 1
    assert summary.all_time == 12.0


def test_recurring_detection_accepts_small_price_variation(repo) -> None:
    _clear_financial_rows(repo)
    for month, amount in (("05", -10.0), ("06", -10.4), ("07", -10.2)):
        repo.add_transaction(
            f"2026-{month}-05",
            "Serviço variável",
            amount,
            "Subscrições",
            "Conta principal",
        )

    rows = PaymentsService(repo).likely_recurring_expenses(10)

    match = next(row for row in rows if row["key"] == "serviço variável")
    assert match["c"] == 3
    assert match["confidence"] == "Média"
    assert match["requires_confirmation"] is True


def test_invalid_installment_count_is_never_silently_replaced(repo) -> None:
    with pytest.raises(ValueError, match="inteiro"):
        repo._scheduled_months_total("não é um número")
    assert repo._scheduled_months_total(0) == 0
    with pytest.raises(ValueError, match="sem fim"):
        repo._scheduled_months_total(-1)


def test_balance_chart_reports_excluded_invalid_points() -> None:
    app = QApplication.instance() or QApplication([])
    theme = Theme()
    chart = BalanceChart(lambda: theme)

    chart.set_points(
        [
            {"date": "2026-08-01", "balance": 100.0},
            {"date": "data inválida", "balance": 90.0},
            {"date": "2026-08-03", "balance": float("nan")},
        ]
    )

    assert app is not None
    assert chart.invalid_points == 2
    assert len(chart.points) == 1
    assert "excluídos" in chart.accessibleDescription()
