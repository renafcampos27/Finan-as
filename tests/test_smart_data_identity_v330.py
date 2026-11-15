from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from database.repo import Repo
from services.analytics import AnalyticsService
from services.payments import PaymentsService
from ui.components.charts.financial_pulse import CumulativeCashflowChart
from ui.components.chart_details import ChartDetailDialog
from ui.core.theme import Theme
from ui.pages.data_management import DataManagementPage


@pytest.fixture
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "smart-identity.sqlite3"))
    instance = Repo()
    yield instance
    instance.db.conn.close()


def test_stable_ids_link_new_movements_accounts_categories_and_assets(
    repo: Repo,
) -> None:
    transaction_id = repo.add_transaction(
        "2026-08-18",
        "Compra identificada",
        -19.95,
        "Diversos",
        "Conta de teste",
    )
    holding_id = repo.add_holding(
        "ETF Global",
        "ETF",
        "Corretora",
        1_250,
        1_000,
        cost_basis_known=True,
        linked_account="Conta de teste",
    )

    movement = repo.transaction_by_id(transaction_id)
    holding = repo.holding_by_id(holding_id)
    assert str(movement["category_id"]).startswith("cat_")
    assert str(movement["account_id"]).startswith("acc_")
    assert str(holding["stable_id"]).startswith("ast_")
    assert holding["linked_account_id"] == movement["account_id"]


def test_category_merge_is_atomic_audited_and_keeps_every_link(repo: Repo) -> None:
    connection = repo.db.conn
    connection.execute("DELETE FROM transaction_splits")
    connection.execute("DELETE FROM transaction_meta")
    connection.execute("DELETE FROM transactions")
    connection.execute("DELETE FROM monthly_category_budgets")
    connection.commit()

    source_id, source_name = repo.category_identity("Lazer")
    target_id, target_name = repo.category_identity("Diversos")
    source_tx = repo.add_transaction(
        "2026-08-01", "Cinema", -30, source_name, "Conta principal"
    )
    split_tx = repo.add_transaction(
        "2026-08-02", "Compra repartida", -100, target_name, "Conta principal"
    )
    repo.save_transaction_splits(split_tx, [(source_name, 40), (target_name, 60)])
    repo.set_category_budget(source_name, 40, month="2026-08")
    repo.set_category_budget(target_name, 60, month="2026-08")

    preview = repo.identity_merge_preview("category", source_id, target_id)
    assert preview["affected"]["transactions"] == 1
    assert preview["affected"]["transaction_splits"] == 1
    repo.merge_financial_identities("category", source_id, target_id)

    assert repo.transaction_by_id(source_tx)["category_id"] == target_id
    assert repo.transaction_by_id(source_tx)["category"] == target_name
    splits = repo.transaction_splits(split_tx)
    assert len(splits) == 1
    assert {str(row["category_id"]) for row in splits} == {target_id}
    assert float(splits[0]["percent"]) == pytest.approx(100)
    assert float(splits[0]["amount"]) == pytest.approx(100)
    assert repo.category_budget(target_name, "2026-08") == pytest.approx(100)
    archived = connection.execute(
        "SELECT is_active,merged_into_id FROM categories WHERE stable_id=?",
        (source_id,),
    ).fetchone()
    assert tuple(archived) == (0, target_id)
    assert repo.category_identity(source_name) == (target_id, target_name)
    assert repo.identity_merge_history()[0]["source_id"] == source_id

    # The startup repair must not revive the archived identity.
    repo.db._ensure_stable_financial_entity_identity()
    connection.commit()
    assert repo.category_identity(source_name) == (target_id, target_name)
    assert not repo.scalar(
        "SELECT COUNT(*) FROM transactions WHERE category_id=?", (source_id,)
    )


def test_account_merge_consolidates_balance_and_preserves_linked_asset(
    repo: Repo,
) -> None:
    source_id, source_name = repo.account_identity("Carteira diária")
    target_id, target_name = repo.account_identity("Banco principal")
    repo.set_financial_account_reference(
        source_name, 100, "2026-08-01", balance_known=True
    )
    repo.set_financial_account_reference(
        target_name, 200, "2026-08-01", balance_known=True
    )
    transaction_id = repo.add_transaction(
        "2026-08-10", "Compra", -10, "Diversos", source_name
    )
    holding_id = repo.add_holding(
        "Fundo monetário",
        "Fundo",
        "Manual",
        500,
        450,
        cost_basis_known=True,
        linked_account=source_name,
    )
    liability_id = repo.add_liability(
        "Crédito pessoal", "Crédito", source_name, 2_000, 5.0, 100
    )

    repo.merge_financial_identities("account", source_id, target_id)

    movement = repo.transaction_by_id(transaction_id)
    holding = repo.holding_by_id(holding_id)
    liability = repo.liability_by_id(liability_id)
    target = repo.db.conn.execute(
        "SELECT * FROM financial_accounts WHERE stable_id=?", (target_id,)
    ).fetchone()
    source = repo.db.conn.execute(
        "SELECT * FROM financial_accounts WHERE stable_id=?", (source_id,)
    ).fetchone()
    assert movement["account_id"] == target_id
    assert movement["account"] == target_name
    assert holding["linked_account_id"] == target_id
    assert liability["account_id"] == target_id
    assert liability["account"] == target_name
    assert float(target["reference_balance"]) == pytest.approx(290)
    assert target["balance_source"] == "merged_confirmed"
    assert tuple((source["is_active"], source["merged_into_id"])) == (0, target_id)
    assert repo.account_identity(source_name) == (target_id, target_name)


def test_merge_failure_rolls_back_every_relationship(repo: Repo) -> None:
    source_id, source_name = repo.category_identity("Lazer")
    target_id, _target_name = repo.category_identity("Diversos")
    transaction_id = repo.add_transaction(
        "2026-08-19", "Teste de reversão", -12, source_name, "Conta principal"
    )
    repo.db.conn.execute(
        """
        CREATE TRIGGER reject_merge_audit
        BEFORE INSERT ON entity_merges
        BEGIN SELECT RAISE(ABORT, 'simulated merge failure'); END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(Exception, match="simulated merge failure"):
        repo.merge_financial_identities("category", source_id, target_id)

    movement = repo.transaction_by_id(transaction_id)
    source = repo.db.conn.execute(
        "SELECT is_active,merged_into_id FROM categories WHERE stable_id=?",
        (source_id,),
    ).fetchone()
    assert movement["category_id"] == source_id
    assert movement["category"] == source_name
    assert tuple(source) == (1, "")
    assert repo.scalar("SELECT COUNT(*) FROM entity_merges") == 0


def test_real_data_coverage_discloses_quality_and_forecasts_include_it(
    repo: Repo,
) -> None:
    connection = repo.db.conn
    connection.execute("DELETE FROM transaction_meta")
    connection.execute("DELETE FROM transactions")
    connection.execute("DELETE FROM holdings")
    connection.execute("DELETE FROM financial_accounts")
    connection.commit()

    repo.ensure_financial_account("Conta conhecida")
    repo.ensure_financial_account("Conta estimada")
    repo.set_financial_account_reference(
        "Conta conhecida", 500, "2026-06-01", balance_known=True
    )
    repo.add_holding("Com custo", "ETF", "Manual", 1_100, 1_000, cost_basis_known=True)
    repo.add_holding("Sem custo", "Cripto", "Manual", 250, 0, cost_basis_known=False)
    repo.add_transaction(
        "2026-06-10", "Receita", 1_000, "Rendimentos", "Conta conhecida"
    )
    repo.add_transaction("2026-07-10", "Despesa", -250, "Diversos", "Conta estimada")
    connection.execute("DROP TRIGGER IF EXISTS validate_transactions_insert")
    connection.execute(
        "INSERT INTO transactions(date,description,amount,category,account,kind) "
        "VALUES('2026-07-10XYZ','Inválido',0,'Diversos','Conta estimada','expense')"
    )
    connection.commit()

    analytics = AnalyticsService(repo, PaymentsService(repo))
    coverage = analytics.data_coverage_snapshot("2026-06-01", "2026-07-31")
    assert coverage["active_months"] == 2
    assert coverage["initial_balances_known"] == 1
    assert coverage["initial_balances_total"] == 2
    assert coverage["investment_cost_known"] == 1
    assert coverage["investment_cost_total"] == 2
    assert coverage["invalid_transactions_excluded"] == 1
    assert coverage["invalid_dates_excluded"] == 1
    assert coverage["invalid_amounts_excluded"] == 1
    assert "saldos iniciais 1/2" in analytics.data_coverage_label(coverage)

    forecast = analytics.monthly_budget_forecast(
        "2026-07",
        as_of=date(2026, 7, 20),
    )
    assert forecast["data_coverage"]["invalid_transactions_excluded"] == 1


def test_chart_detail_receives_calculation_evidence(qapp: QApplication) -> None:
    chart = CumulativeCashflowChart(lambda: Theme(dark=False))
    chart.set_rows(
        [
            {
                "date": "2026-08-18",
                "income": 1_000,
                "expenses": 300,
                "net": 700,
            }
        ]
    )
    chart.configure_calculation_context(
        lambda _item, _payload: {
            "period": "18/08/2026",
            "filters": "Categoria: Diversos",
            "movements": 3,
            "exclusions": "1 transação inválida",
            "data_coverage": "2 meses · saldos 1/2",
            "updated_at": "19:05:00",
        }
    )

    payload = chart.detail_payload(chart.rows[0])
    assert payload["calculation"]["movements"] == 3
    assert payload["calculation"]["data_coverage"] == "2 meses · saldos 1/2"
    dialog = ChartDetailDialog(payload, Theme(dark=False))
    assert dialog.calculation_host.isHidden() is False
    calculation_text = " ".join(label.text() for label in dialog.calculation_labels)
    assert "Movimentos considerados" in calculation_text
    assert "Categoria: Diversos" in calculation_text
    dialog.close()


def test_every_base_page_exposes_freshness_and_management_page_is_responsive(
    repo: Repo,
    qapp: QApplication,
) -> None:
    analytics = AnalyticsService(repo, PaymentsService(repo))
    app = SimpleNamespace(
        repo=repo,
        analytics_service=analytics,
        payments_service=PaymentsService(repo),
        import_service=object(),
        notify_data_changed=lambda _message: None,
    )
    page = DataManagementPage(app)
    page.resize(640, 760)
    page.show()
    qapp.processEvents()
    page.reflow_responsive_grids()
    assert "Dados atualizados às" in page.data_updated_label.text()
    first_update = page._data_updated_at
    page.mark_data_stale()
    page.refresh()
    assert page._data_updated_at is not None
    assert page._data_updated_at >= first_update
    assert (
        any(
            "Como este valor foi calculado" in label.text()
            for label in page.findChildren(QLabel)
        )
        is False
    )
    assert any(
        layout is page.summary_grid and columns == 5 and minimum == 180
        for layout, columns, minimum in page._responsive_grids
    )
    assert page.source.count() > 1
    page.close()
