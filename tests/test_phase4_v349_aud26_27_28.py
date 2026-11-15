"""Unit and integration tests for AUD-26, AUD-27, and AUD-28 fixes in v3.49.0."""

from datetime import date
from utils.helpers import parse_flexible_date
from ui.models.movements import MovementsTableModel
from ui.core.theme import Theme
from PySide6.QtCore import QModelIndex
from database.engine import FinanceDB
from database.repo.main_repo import Repo
from services.payments import PaymentsService
from services.financial_health import FinancialHealthService


def test_aud26_financial_health_empty_shares_no_crash(tmp_path, monkeypatch):
    """Verify FinancialHealthService handles empty positive asset class shares without ZeroDivisionError or ValueError."""
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "health.sqlite3")
    try:
        repo = Repo(db)
        # Create a holding with 0.0 value
        db.conn.execute(
            "INSERT INTO holdings(name, asset_class, value, cost_basis) "
            "VALUES('Ativo Zerado', 'A??es', 0.0, 100.0)"
        )
        db.conn.commit()

        service = FinancialHealthService(repo, PaymentsService(repo))
        # Call financial_score
        result = service.financial_score("2026-08")
        assert isinstance(result, dict)
        assert "score" in result
        assert "dimensions" in result
        dim_by_key = {d["key"]: d for d in result["dimensions"]}
        assert "diversification" in dim_by_key
        assert dim_by_key["diversification"]["score"] >= 0.0

        # Now test the edge case where holding_rows has holdings with 0.0 value and detail available
        mock_snapshot = {
            "holding_rows": [{"asset_class": "A??es", "value": 0.0}],
            "holdings_detail_available": True,
            "liquid_assets": 1000.0,
        }
        monkeypatch.setattr(service, "_net_worth", lambda *args, **kwargs: mock_snapshot)
        service.clear_cache()
        res_mock = service.financial_score("2026-08")
        mock_dim = {d["key"]: d for d in res_mock["dimensions"]}
        assert mock_dim["diversification"]["score"] == 45.0
    finally:
        db.prepare_for_shutdown()


def test_aud27_parse_flexible_date_two_digit_years():
    """Verify parse_flexible_date recognizes Portuguese/European bank dates with 2-digit years."""
    # Standard Portuguese formats: DD/MM/YY, DD-MM-YY, DD.MM.YY
    assert parse_flexible_date("24/08/26") == date(2026, 8, 24)
    assert parse_flexible_date("24-08-26") == date(2026, 8, 24)
    assert parse_flexible_date("24.08.26") == date(2026, 8, 24)
    assert parse_flexible_date("15/01/25") == date(2025, 1, 15)
    assert parse_flexible_date("01-02-23") == date(2023, 2, 1)
    # ISO short format: YY-MM-DD
    assert parse_flexible_date("26-08-24") == date(2024, 8, 26)


def test_aud28_movements_table_model_parent_none_handling():
    """Verify MovementsTableModel rowCount and columnCount accept None and default cleanly without C++ import instantiation."""
    theme = Theme()
    model = MovementsTableModel(theme)
    model.set_rows([{"id": 1, "description": "Teste", "amount": -10.0}])

    # With None parent
    assert model.rowCount(None) == 1
    assert model.columnCount(None) == len(model.HEADERS)

    # With default (omitted argument)
    assert model.rowCount() == 1
    assert model.columnCount() == len(model.HEADERS)

    # With invalid QModelIndex
    invalid_index = QModelIndex()
    assert model.rowCount(invalid_index) == 1
    assert model.columnCount(invalid_index) == len(model.HEADERS)
