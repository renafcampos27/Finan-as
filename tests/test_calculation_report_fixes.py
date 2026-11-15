"""Regression guards for the 2026-09-02 calculation and report fixes.

Covers four audit findings:

- AUD-17: ``compare_periods`` must not fabricate ``+100%`` when the reference
  period is zero; it must expose an explicit ``change_basis`` instead.
- AUD-18: ``explain_period_variation`` must reconcile positive and negative
  category drivers with the total expense variation.
- AUD-19: the annual report document totals must count each movement once,
  never once per attachment.
- AUD-20: the portfolio return label must describe the actual formula
  (money-weighted over committed capital) instead of implying TWR/XIRR.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.repo import Repo  # noqa: E402
from services.analytics import AnalyticsService  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Isolated repository that never touches the real user database."""
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "calc_fixes.sqlite3"))
    instance = Repo()
    yield instance
    instance.db.conn.close()


@pytest.fixture
def analytics(repo):
    return AnalyticsService(repo)


def _seed_movements(
    repo: Repo,
    rows: list[tuple[str, str, float, str, str, str]],
) -> None:
    """Bulk-insert transactions for testing."""
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    repo.db.conn.commit()


def test_compare_periods_zero_reference_reports_new_basis_not_plus_100(
    repo: Repo,
    analytics: AnalyticsService,
) -> None:
    """AUD-17: a category absent from the reference must not claim +100%."""
    _seed_movements(
        repo,
        [
            ("2026-06-01", "Salario", 1800.0, "Rendimentos", "Conta", "income"),
            ("2026-06-05", "Mercado", -500.0, "Supermercado", "Conta", "expense"),
            ("2026-07-01", "Salario", 2000.0, "Rendimentos", "Conta", "income"),
            ("2026-07-05", "Mercado", -600.0, "Supermercado", "Conta", "expense"),
            ("2026-07-06", "Cinema", -100.0, "Lazer", "Conta", "expense"),
        ],
    )

    comparison = analytics.compare_periods(
        "2026-07-01", "2026-07-31", "2026-06-01", "2026-06-30"
    )
    categories = {row["category"]: row for row in comparison["categories"]}

    # "Lazer" exists only in the current period: no percentage is defined.
    lazer = categories["Lazer"]
    assert lazer["reference"] == 0
    assert lazer["current"] == pytest.approx(100)
    assert lazer["change_pct"] is None
    assert lazer["change_basis"] == "novo"
    assert lazer["delta"] == pytest.approx(100)

    # A category with a real base keeps a numeric percentage.
    supermercado = categories["Supermercado"]
    assert supermercado["reference"] == pytest.approx(500)
    assert supermercado["change_pct"] == pytest.approx(20.0)
    assert supermercado["change_basis"] == "percentual"

    # Both periods zero must be explicit about the absence of movement.
    configured = categories.get("Animais") or next(
        row
        for row in comparison["categories"]
        if row["current"] == 0 and row["reference"] == 0
    )
    assert configured["change_pct"] is None
    assert configured["change_basis"] == "sem_variacao"


def test_explain_period_variation_reconciles_increases_and_decreases(
    repo: Repo,
    analytics: AnalyticsService,
) -> None:
    """AUD-18: top increases and top decreases must both be listed."""
    _seed_movements(
        repo,
        [
            ("2025-07-01", "Salario", 1800.0, "Rendimentos", "Conta", "income"),
            ("2025-07-05", "Mercado", -400.0, "Supermercado", "Conta", "expense"),
            ("2025-07-06", "Jantar", -200.0, "Restaurantes", "Conta", "expense"),
            ("2026-07-01", "Salario", 2000.0, "Rendimentos", "Conta", "income"),
            ("2026-07-05", "Mercado", -700.0, "Supermercado", "Conta", "expense"),
            ("2026-07-06", "Jantar", -80.0, "Restaurantes", "Conta", "expense"),
        ],
    )
    comparison = analytics.compare_periods(
        "2026-07-01", "2026-07-31", "2025-07-01", "2025-07-31"
    )

    explanations = analytics.explain_period_variation(comparison)
    titles = {row["title"] for row in explanations}

    assert "Mais gasto em Supermercado" in titles
    assert "Poupança em Restaurantes" in titles
    # The highlighted drivers must fully reconcile the expense variation:
    # unexplained remainder is zero in this fixture, so no reconciliation
    # entry is expected. When it exists it must match the total delta.
    reconciliation = [
        row for row in explanations if row.get("kind") == "reconciliation"
    ]
    total_delta = comparison["metrics"]["expenses"]["delta"]
    highlighted_delta = sum(
        row.get("delta", 0.0) for row in explanations if row.get("kind") == "category"
    )
    if reconciliation:
        assert reconciliation[0]["delta"] == pytest.approx(total_delta)
    else:
        assert highlighted_delta == pytest.approx(total_delta)


def test_annual_report_document_total_counts_each_movement_once(
    repo: Repo,
    analytics: AnalyticsService,
) -> None:
    """AUD-19: two attachments of one invoice must not double the total."""
    repo.db.conn.execute("DELETE FROM transaction_attachments")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    transaction_id = repo.add_transaction(
        "2026-01-09", "Computador", -1200, "Compras grandes", "Conta principal"
    )
    for file_name in ("fatura.pdf", "fatura-verso.pdf"):
        repo.add_transaction_attachment(
            transaction_id=transaction_id,
            file_name=file_name,
            stored_name=file_name,
            stored_path=f"C:/tmp/{file_name}",
            original_path=f"C:/tmp/{file_name}",
            file_type="PDF",
            description="Equipamento",
            supplier="Loja Exemplo",
            invoice_date="2026-01-09",
            total_amount=1200,
        )

    report = analytics.annual_report_data(2026)

    assert report["documents"]["document_count"] == 2
    # 1200 once, not 2400.
    assert report["documents"]["documented_total"] == pytest.approx(1200)
    assert report["documents"]["documented_movement_count"] == 1
    suppliers = report["document_suppliers"]
    assert suppliers[0]["supplier"] == "Loja Exemplo"
    assert suppliers[0]["total"] == pytest.approx(1200)


def test_portfolio_method_label_describes_money_weighted_capital_return() -> None:
    """AUD-20: the label must not claim a time-weighted return."""
    from services.investment_performance import portfolio_performance_attribution

    holdings = [
        {
            "id": 1,
            "name": "ETF Global",
            "asset_class": "ETF",
            "account": "Corretora",
            "value": 12_500.0,
            "cost_basis": 10_000.0,
            "cost_basis_known": 1,
            "fees": 125.0,
        }
    ]
    performance: dict[str, Any] = portfolio_performance_attribution(holdings)

    assert performance["net_return_pct"] == pytest.approx(2_375 / 10_125 * 100)
    label = str(performance["method_label"])
    assert "ponderada no tempo" not in label.casefold()
    assert "TWR" not in label
    # It must still explain the real basis: committed capital.
    assert "capital" in label.casefold()
