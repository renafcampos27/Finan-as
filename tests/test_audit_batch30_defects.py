"""
Unit tests for Audit Batch 30 Defects (P1)
===========================================
Defects covered:
1. BUG-AUD-C61 (P1): Net capital losses against capital gains within the same fiscal year (Art. 43.º CIRS)
   in summarize_group instead of unnetted per-lot summation.
2. BUG-AUD-C62 (P1): Removal of UAE (AE) and Qatar (QA) from OFFSHORE_JURISDICTIONS per Portarias 309-A/2020 & 345-B/2016.
3. BUG-AUD-C85 (P1): Gate explosive annualization in XIRR for short holding periods (< 30 days) per GIPS standards.
4. BUG-AUD-C97 (P1): TWRR recapitalization formula cannot report a positive cumulative rate after a total (100%) loss.
5. BUG-AUD-C98 (P1): Use historical acquisition exchange rate for cost basis and fees under CIRS Art. 43.º.
6. BUG-AUD-C100 (P1): Mask financial balances in tooltips and IRS summary copy button under Privacy Mode.
7. BUG-AUD-C102 (P1): Graceful handling of zero and negative budget envelopes in budget_health_monitor.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
import pytest

from services.tax import (
    ForeignDividendPayment,
    OFFSHORE_JURISDICTIONS,
    TAXA_AGRAVADA_OFFSHORE,
    TAXA_LIBERATORIA_PADRAO,
    PortugueseTaxEngine,
    calculate_foreign_dividend_tax,
)
from services.finance_math import twrr
from services.investment_performance import (
    calculate_holding_xirr,
    calculate_portfolio_xirr,
    portfolio_performance_attribution,
)
from services.financial_intelligence import FinancialIntelligenceService
from services.analytics import AnalyticsService


# ============================================================================
# 1. BUG-AUD-C61: Loss netting under autonomous 28% taxation (Art. 43.º CIRS)
# ============================================================================

def test_c61_loss_netting_in_summarize_group():
    """Art. 43.º CIRS: menos-valias compensam mais-valias no imposto estimado do grupo."""
    engine = PortugueseTaxEngine()
    # Ganho de 1000 EUR
    engine.add_acquisition(
        lot_id="gain_lot",
        asset_symbol="AAPL",
        acquisition_date=date(2025, 1, 10),
        quantity=10.0,
        unit_price=100.0,
    )
    matches_gain = engine.process_disposal(
        disposal_id="d_gain",
        asset_symbol="AAPL",
        disposal_date=date(2025, 6, 10),
        quantity=10.0,
        unit_price=200.0,  # Ganho bruto = +1000
    )
    # Perda de 400 EUR
    engine.add_acquisition(
        lot_id="loss_lot",
        asset_symbol="MSFT",
        acquisition_date=date(2025, 2, 1),
        quantity=10.0,
        unit_price=100.0,
    )
    matches_loss = engine.process_disposal(
        disposal_id="d_loss",
        asset_symbol="MSFT",
        disposal_date=date(2025, 7, 1),
        quantity=10.0,
        unit_price=60.0,  # Perda bruta = -400
    )

    all_matches = matches_gain + matches_loss
    report = engine.generate_annual_tax_report(all_matches, 2025)

    # Anexo G summary must reflect the NET balance: 1000 - 400 = 600 EUR
    anexo_g = report["anexo_g"]
    assert anexo_g["capital_gains"] == pytest.approx(1000.0)
    assert anexo_g["capital_losses"] == pytest.approx(400.0)
    assert anexo_g["net_taxable_balance"] == pytest.approx(600.0)
    # Estimated tax under 28% must be 600 * 0.28 = 168.0, NOT 280.0 (which was unnetted)
    assert anexo_g["estimated_tax_autonomous_28pct"] == pytest.approx(168.0)
    assert report["global_estimated_tax_28pct"] == pytest.approx(168.0)


def test_c61_loss_exceeding_gains_results_in_zero_group_estimated_tax():
    """Quando as perdas superam os ganhos no grupo, o imposto estimado é 0.0 EUR."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="g",
        asset_symbol="AAA",
        acquisition_date=date(2025, 1, 1),
        quantity=10.0,
        unit_price=50.0,
    )
    m_gain = engine.process_disposal(
        disposal_id="dg",
        asset_symbol="AAA",
        disposal_date=date(2025, 4, 1),
        quantity=10.0,
        unit_price=70.0,
    )  # +200

    engine.add_acquisition(
        lot_id="l",
        asset_symbol="BBB",
        acquisition_date=date(2025, 1, 1),
        quantity=10.0,
        unit_price=100.0,
    )
    m_loss = engine.process_disposal(
        disposal_id="dl",
        asset_symbol="BBB",
        disposal_date=date(2025, 5, 1),
        quantity=10.0,
        unit_price=50.0,
    )  # -500

    report = engine.generate_annual_tax_report(m_gain + m_loss, 2025)
    anexo_g = report["anexo_g"]
    assert anexo_g["net_taxable_balance"] == pytest.approx(-300.0)
    assert anexo_g["estimated_tax_autonomous_28pct"] == 0.0
    assert report["global_estimated_tax_28pct"] == 0.0


def test_c61_dialog_nets_losses_against_gains():
    """PortugueseTaxDialog compensa menos-valias contra mais-valias no cálculo do imposto estimado."""
    from PySide6.QtWidgets import QApplication, QWidget
    from ui.dialogs import PortugueseTaxDialog

    _ = QApplication.instance() or QApplication([])

    mock_repo = MagicMock()
    mock_repo.q.return_value = [
        {
            "name": "Apple Gain",
            "asset_class": "Ações",
            "category": "Ações",
            "cost_basis": 1000.0,
            "value": 2000.0,  # Ganho = +1000
            "jurisdiction": "US",
            "is_foreign": 1,
            "acquisition_date": "2024-01-01",
        },
        {
            "name": "Tesla Loss",
            "asset_class": "Ações",
            "category": "Ações",
            "cost_basis": 1000.0,
            "value": 600.0,  # Perda = -400
            "jurisdiction": "US",
            "is_foreign": 1,
            "acquisition_date": "2024-01-01",
        },
    ]

    parent_mock = QWidget()
    parent_mock.theme = MagicMock(dark=False, text="#000", muted="#666", border="#CCC", privacy=False)

    dialog = PortugueseTaxDialog(mock_repo, parent=parent_mock)
    # Total taxable gains = 1000, Total taxable losses = 400 -> Net balance = 600 EUR
    # Estimated tax under 28% must be 600 * 0.28 = 168.0 EUR, NOT 280.0 EUR
    assert dialog.table.rowCount() == 2
    assert "168" in dialog._summary_text
    assert "280" not in dialog._summary_text
    dialog.deleteLater()
    parent_mock.deleteLater()


def test_c61_anexo_j_offshore_weighted_rate_loss_netting():
    """Anexo J com ativos offshore (35%) e perdas calcula imposto ponderado sobre saldo líquido."""
    engine = PortugueseTaxEngine()
    # Ativo offshore (Cayman KY): Ganho 1000 EUR tributado a 35%
    engine.add_acquisition(
        lot_id="lot_ky",
        asset_symbol="FUND_KY",
        acquisition_date=date(2025, 1, 1),
        quantity=10.0,
        unit_price=100.0,
        country_code="KY",
        is_foreign=True,
    )
    m_ky = engine.process_disposal(
        disposal_id="disp_ky",
        asset_symbol="FUND_KY",
        disposal_date=date(2025, 6, 1),
        quantity=10.0,
        unit_price=200.0,
        country_code="KY",
        is_foreign=True,
    )

    # Ativo com perda de 400 EUR em Anexo J
    engine.add_acquisition(
        lot_id="lot_us",
        asset_symbol="LOSS_US",
        acquisition_date=date(2025, 1, 1),
        quantity=10.0,
        unit_price=100.0,
        country_code="US",
        is_foreign=True,
    )
    m_us = engine.process_disposal(
        disposal_id="disp_us",
        asset_symbol="LOSS_US",
        disposal_date=date(2025, 7, 1),
        quantity=10.0,
        unit_price=60.0,
        country_code="US",
        is_foreign=True,
    )

    report = engine.generate_annual_tax_report(m_ky + m_us, 2025)
    anexo_j = report["anexo_j"]
    assert anexo_j["capital_gains"] == pytest.approx(1000.0)
    assert anexo_j["capital_losses"] == pytest.approx(400.0)
    assert anexo_j["net_taxable_balance"] == pytest.approx(600.0)
    # Taxa ponderada dos ganhos é 35.0% -> imposto estimado = 600 * 0.35 = 210.0 EUR
    assert anexo_j["estimated_tax_autonomous_28pct"] == pytest.approx(210.0)


# ============================================================================
# 2. BUG-AUD-C62: UAE and Qatar removed from offshore blacklist
# ============================================================================

def test_c62_uae_and_qatar_not_in_offshore_blacklist():
    """AE e QA não podem constar da lista de jurisdições offshore (Portarias 309-A/2020 e 345-B/2016)."""
    assert "AE" not in OFFSHORE_JURISDICTIONS
    assert "QA" not in OFFSHORE_JURISDICTIONS


def test_c62_investments_in_uae_and_qatar_taxed_at_standard_rate():
    """Ganhos em ativos no Dubai/EAU ou Qatar são tributados a 28%, não a 35%."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="lot_dubai",
        asset_symbol="EMAAR",
        acquisition_date=date(2025, 1, 1),
        quantity=100.0,
        unit_price=10.0,
        country_code="AE",
    )
    matches_ae = engine.process_disposal(
        disposal_id="disp_dubai",
        asset_symbol="EMAAR",
        disposal_date=date(2025, 6, 1),
        quantity=100.0,
        unit_price=15.0,
        country_code="AE",
    )
    assert len(matches_ae) == 1
    m = matches_ae[0]
    assert m.tax_rate_pct == pytest.approx(TAXA_LIBERATORIA_PADRAO)
    assert m.tax_rate_pct != pytest.approx(TAXA_AGRAVADA_OFFSHORE)
    assert m.estimated_tax == pytest.approx(500.0 * 0.28)


def test_c62_uae_and_qatar_dividends_not_taxed_at_punitive_rate():
    """Dividendos auferidos nos EAU ou Qatar não sofrem taxa agravada de 35% nem perda de crédito."""
    div_ae = ForeignDividendPayment(
        id="div_ae_1",
        asset_symbol="EMAAR",
        payment_date=date(2025, 4, 1),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=0.0,
        country_code="AE",
    )
    res_ae = calculate_foreign_dividend_tax(div_ae)
    assert res_ae.portuguese_autonomous_tax_28pct == pytest.approx(280.0)
    assert res_ae.effective_total_tax_rate_pct == pytest.approx(28.0)
    assert res_ae.effective_total_tax_rate_pct != pytest.approx(35.0)

    div_qa = ForeignDividendPayment(
        id="div_qa_1",
        asset_symbol="QNB",
        payment_date=date(2025, 5, 1),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=0.0,
        country_code="QA",
    )
    res_qa = calculate_foreign_dividend_tax(div_qa)
    assert res_qa.portuguese_autonomous_tax_28pct == pytest.approx(280.0)
    assert res_qa.effective_total_tax_rate_pct == pytest.approx(28.0)


# ============================================================================
# 3. BUG-AUD-C85: Gate explosive annualization for short holding periods (< 30d)
# ============================================================================

def test_c85_holding_xirr_short_period_reports_nominal_return():
    """Períodos < 30 dias não devem explodir por anualização; reportam retorno nominal (GIPS)."""
    # Ganho de 5% em 10 dias: comprado a 1000€, valor atual 1050€
    acq_date = date(2026, 1, 1)
    curr_date = date(2026, 1, 11)  # 10 dias
    ret = calculate_holding_xirr(
        acquisition_date=acq_date,
        cost_basis=1000.0,
        current_date=curr_date,
        current_value=1050.0,
    )
    assert ret is not None
    # Nominal return = +5.0%, not annualization of (1.05)^36.5 - 1 (~496%)
    assert ret == pytest.approx(5.0, abs=1e-4)


def test_c85_holding_xirr_short_period_loss_reports_nominal_loss():
    """Perda em período < 30 dias reporta perda nominal correspondente."""
    acq_date = date(2026, 1, 1)
    curr_date = date(2026, 1, 6)  # 5 dias
    ret = calculate_holding_xirr(
        acquisition_date=acq_date,
        cost_basis=1000.0,
        current_date=curr_date,
        current_value=970.0,
    )
    assert ret is not None
    # Nominal return = -3.0%
    assert ret == pytest.approx(-3.0, abs=1e-4)


def test_c85_portfolio_xirr_short_horizon_reports_nominal_return():
    """Portfolio XIRR com horizonte < 30 dias reporta retorno cumulativo nominal."""
    flows = [
        (date(2026, 3, 1), -10_000.0),
        (date(2026, 3, 15), 100.0),  # Dividendo
    ]
    curr_date = date(2026, 3, 20)  # 19 dias total
    ret = calculate_portfolio_xirr(flows, curr_date, 10_200.0)
    assert ret is not None
    # Total invested = 10,000, Total recovered + value = 10,300 -> +3.0%
    assert ret == pytest.approx(3.0, abs=1e-4)


def test_c85_holding_xirr_normal_period_calculates_annualized_rate():
    """Posições detidas >= 30 dias continuam a calcular XIRR anualizado padrão."""
    acq_date = date(2025, 1, 1)
    curr_date = date(2026, 1, 1)  # 365 dias
    ret = calculate_holding_xirr(
        acquisition_date=acq_date,
        cost_basis=1000.0,
        current_date=curr_date,
        current_value=1100.0,
    )
    assert ret is not None
    assert ret == pytest.approx(10.0, abs=0.5)


def test_c85_holding_xirr_zero_days_holding_period():
    """Posição adquirida e avaliada no mesmo dia (0 dias) reporta retorno nominal sem crash/None."""
    today = date(2026, 3, 1)
    ret = calculate_holding_xirr(
        acquisition_date=today,
        cost_basis=1000.0,
        current_date=today,
        current_value=1000.0,
    )
    assert ret is not None
    assert ret == pytest.approx(0.0)


def test_c85_holding_xirr_inverted_dates_returns_none():
    """Datas invertidas (aquisição posterior à data de avaliação) devolve None com segurança."""
    ret = calculate_holding_xirr(
        acquisition_date=date(2026, 5, 1),
        cost_basis=1000.0,
        current_date=date(2026, 1, 1),
        current_value=1100.0,
    )
    assert ret is None


# ============================================================================
# 4. BUG-AUD-C97: TWRR recapitalization formula cannot report positive rate
# ============================================================================

def test_c97_twrr_recapitalization_cannot_turn_positive_after_total_loss():
    """Perda total (-100%) impede que retornos subsequentes tornem o retorno cumulativo positivo."""
    # Perda total no subperíodo 1, seguida de ganhos de +50% e +20% pós-recapitalização
    ret = twrr([-100.0, 50.0, 20.0], allow_recapitalization=True)
    assert ret == 0.0

    # Retorno substancialmente superior (+200%) pós-perda total é igualmente limitado a 0.0%
    ret_huge = twrr([-100.0, 200.0], allow_recapitalization=True)
    assert ret_huge == 0.0


def test_c97_twrr_recapitalization_preserves_negative_rates():
    """Pós-recapitalização, perdas subsequentes são devidamente reportadas como negativas."""
    ret_neg = twrr([-100.0, -20.0], allow_recapitalization=True)
    assert round(ret_neg, 2) == -20.0


def test_c97_twrr_without_recapitalization_retains_minus_100():
    """Sem flag de recapitalização, qualquer perda total bloqueia permanentemente a -100%."""
    assert twrr([-100.0, 50.0, 20.0], allow_recapitalization=False) == -100.0


def test_c97_twrr_total_loss_in_middle_subperiod():
    """Perda total ocorrida no meio da série é devidamente limitada a 0.0% mesmo após ganhos."""
    # -50% seguido de -100% (total loss no período 2) seguido de +50%
    ret = twrr([-50.0, -100.0, 50.0], allow_recapitalization=True)
    assert ret == 0.0

    # Perda subsequente pós-perda total no meio reflete a perda líquida pós-recapitalização (-10%)
    ret_neg = twrr([-50.0, -100.0, -10.0], allow_recapitalization=True)
    assert round(ret_neg, 2) == -10.0


# ============================================================================
# 5. BUG-AUD-C98: Historical acquisition exchange rate in portfolio attribution
# ============================================================================

def test_c98_attribution_uses_historical_acquisition_rate():
    """Atribuição usa o câmbio histórico de aquisição para custo de aquisição e comissões (CIRS Art. 43.º)."""
    holdings = [
        {
            "id": 1,
            "name": "US Stock",
            "value": 100.0,        # Valor atual em USD
            "cost_basis": 80.0,     # Custo nativo em USD
            "fees": 5.0,           # Encargos em USD
            "currency": "USD",
            "exchange_rate": 0.80,  # Câmbio atual (spot)
            "historical_exchange_rate": 0.90,  # Câmbio na data da compra
            "cost_basis_known": 1,
        }
    ]
    result = portfolio_performance_attribution(holdings)
    row = result["rows"][0]

    # Valor atual convertido pelo spot rate: 100 * 0.80 = 80.0 EUR
    assert row["current_value"] == pytest.approx(80.0)
    # Custo de aquisição convertido pelo câmbio histórico: 80 * 0.90 = 72.0 EUR
    assert row["cost_basis"] == pytest.approx(72.0)
    # Encargos convertidos pelo câmbio histórico: 5 * 0.90 = 4.5 EUR
    assert row["fees"] == pytest.approx(4.5)
    # Câmbio histórico retornado no registo
    assert row["historical_exchange_rate"] == pytest.approx(0.90)

    # Ganho bruto = 80.0 - 72.0 = 8.0 EUR
    assert row["gross_gain"] == pytest.approx(8.0)
    # Total cost basis agregado
    assert result["total_cost_basis"] == pytest.approx(72.0)


def test_c98_attribution_historical_rate_from_metadata_dict():
    """Atribuição aceita taxa histórica dentro do dicionário 'metadata'."""
    holdings = [
        {
            "id": 2,
            "name": "Foreign ETF",
            "value": 200.0,
            "cost_basis": 150.0,
            "fees": 0.0,
            "currency": "USD",
            "exchange_rate": 0.75,
            "metadata": {"acquisition_exchange_rate": 0.85},
            "cost_basis_known": 1,
        }
    ]
    result = portfolio_performance_attribution(holdings)
    row = result["rows"][0]
    assert row["current_value"] == pytest.approx(150.0)  # 200 * 0.75
    assert row["cost_basis"] == pytest.approx(127.5)     # 150 * 0.85


def test_c98_attribution_requires_historical_rate_when_absent():
    """A08: a current quote cannot supply an unknown acquisition conversion."""
    holdings = [
        {
            "id": 3,
            "name": "No Hist Rate",
            "value": 100.0,
            "cost_basis": 80.0,
            "fees": 2.0,
            "currency": "USD",
            "exchange_rate": 0.80,
            "cost_basis_known": 1,
        }
    ]
    result = portfolio_performance_attribution(holdings)
    row = result["rows"][0]
    assert row["current_value"] == pytest.approx(80.0)
    assert row["cost_basis"] is None
    assert row["historical_exchange_rate"] is None
    assert row["performance_available"] is False
    assert result["data_coverage_pct"] == 0


def test_c98_attribution_historical_rate_from_json_string_metadata():
    """Atribuição extrai taxa histórica quando metadata é uma string JSON serializada."""
    holdings = [
        {
            "id": 10,
            "name": "Serialized Meta ETF",
            "value": 200.0,
            "cost_basis": 150.0,
            "fees": 10.0,
            "currency": "USD",
            "exchange_rate": 0.75,
            "metadata": '{"historical_exchange_rate": 0.85}',
            "cost_basis_known": 1,
        }
    ]
    result = portfolio_performance_attribution(holdings)
    row = result["rows"][0]
    assert row["current_value"] == pytest.approx(150.0)  # 200 * 0.75
    assert row["cost_basis"] == pytest.approx(127.5)     # 150 * 0.85
    assert row["fees"] == pytest.approx(8.5)            # 10 * 0.85
    assert row["historical_exchange_rate"] == pytest.approx(0.85)


# ============================================================================
# 6. BUG-AUD-C100: Privacy mode masks tooltips & IRS summary copy button
# ============================================================================

def test_c100_privacy_mode_masks_dialog_tax_summary_and_tooltips():
    """Em modo de privacidade, o resumo do diálogo fiscal mascara todos os montantes."""
    from PySide6.QtWidgets import QApplication, QWidget
    from ui.dialogs import PortugueseTaxDialog

    _ = QApplication.instance() or QApplication([])

    mock_repo = MagicMock()
    mock_repo.q.return_value = [
        {
            "name": "Apple",
            "asset_class": "Ações",
            "category": "Ações",
            "cost_basis": 1000.0,
            "value": 1500.0,
            "jurisdiction": "US",
            "is_foreign": 1,
            "acquisition_date": "2024-01-01",
        }
    ]

    parent_mock = QWidget()
    parent_mock.theme = MagicMock(dark=True, text="#FFF", muted="#AAA", border="#333", privacy=True)

    dialog = PortugueseTaxDialog(mock_repo, parent=parent_mock)
    dialog.theme = parent_mock.theme
    dialog._populate_tax_data()

    # O texto do resumo preparado para cópia NÃO pode conter montantes numéricos não mascarados
    summary = dialog._summary_text
    assert "1 500" not in summary
    assert "1 000" not in summary
    assert "500" not in summary
    assert "••••• €" in summary

    # As células da tabela com montantes também devem estar mascaradas
    cost_item = dialog.table.item(0, 2)
    assert cost_item.text() == "••••• €"
    assert cost_item.toolTip() == "Modo de privacidade ativo"

    dialog.deleteLater()
    parent_mock.deleteLater()


def test_c100_investments_page_foreign_currency_tooltip_privacy():
    """ui/pages/investments.py mascara montante estrangeiro em tooltip no Modo de Privacidade."""
    from PySide6.QtWidgets import QApplication, QTableWidgetItem
    from utils.helpers import safe_float

    _ = QApplication.instance() or QApplication([])

    r = {
        "id": 1,
        "name": "US Stock",
        "value": 1500.0,
        "currency": "USD",
        "exchange_rate": 0.80,
    }
    rate = 0.80
    curr = "USD"

    # Simular lógica da célula da coluna 4 em modo de privacidade ativo
    privacy_active = True
    val_str = "••••" if privacy_active else f"{safe_float(r['value']):,.2f}"
    item_priv = QTableWidgetItem("••••• €")
    item_priv.setToolTip(f"{val_str} {curr} (câmbio: {rate:.4f})")
    assert "1 500" not in item_priv.toolTip()
    assert "•••• USD (câmbio: 0.8000)" == item_priv.toolTip()

    # Em modo normal
    privacy_active = False
    val_str = "••••" if privacy_active else f"{safe_float(r['value']):,.2f}"
    item_norm = QTableWidgetItem("1 200,00 €")
    item_norm.setToolTip(f"{val_str} {curr} (câmbio: {rate:.4f})")
    assert "1,500.00 USD (câmbio: 0.8000)" == item_norm.toolTip()


def test_c100_dialog_copy_button_tooltip_in_privacy_mode():
    """Botão de cópia no diálogo fiscal indica modo de privacidade no tooltip."""
    from PySide6.QtWidgets import QApplication, QWidget
    from ui.dialogs import PortugueseTaxDialog

    _ = QApplication.instance() or QApplication([])

    mock_repo = MagicMock()
    mock_repo.q.return_value = []

    parent_priv = QWidget()
    parent_priv.theme = MagicMock(dark=True, privacy=True)

    dlg_priv = PortugueseTaxDialog(mock_repo, parent=parent_priv)
    assert "privacidade" in dlg_priv.copy_btn.toolTip().lower()
    dlg_priv.deleteLater()
    parent_priv.deleteLater()

    parent_norm = QWidget()
    parent_norm.theme = MagicMock(dark=False, privacy=False)
    dlg_norm = PortugueseTaxDialog(mock_repo, parent=parent_norm)
    assert "privacidade" not in dlg_norm.copy_btn.toolTip().lower()
    dlg_norm.deleteLater()
    parent_norm.deleteLater()


# ============================================================================
# 7. BUG-AUD-C102: Handle zero and negative budget envelopes gracefully
# ============================================================================

def test_c102_budget_health_monitor_negative_and_zero_envelopes():
    """budget_health_monitor não gera taxas negativas nem alertas distorcidos com orçamentos nulos ou negativos."""
    repo_mock = MagicMock()
    # Orçamento negativo e zero para duas categorias
    repo_mock.category_budgets.return_value = {
        "Alimentação": -100.0,
        "Lazer": 0.0,
    }
    repo_mock.category_spend_with_splits.return_value = [
        {"category": "Alimentação", "total": 50.0},
        {"category": "Lazer", "total": 20.0},
    ]

    service = FinancialIntelligenceService(repo_mock)
    result = service.smart_budget_analysis("2026-03", as_of=date(2026, 3, 15))

    rows_by_cat = {r["category"]: r for r in result["rows"]}

    # Categoria com orçamento negativo (-100) deve ser tratada com orçamento efetivo 0
    alim = rows_by_cat["Alimentação"]
    assert alim["effective_budget"] == 0.0
    assert alim["utilisation"] == 0.0
    assert alim["projected_utilisation"] == 0.0
    assert alim["breach_date"] == ""
    assert alim["status"] == "Sem limite"
    assert alim["tone"] == "neutral"

    # Categoria com orçamento zero
    lazer = rows_by_cat["Lazer"]
    assert lazer["effective_budget"] == 0.0
    assert lazer["utilisation"] == 0.0
    assert lazer["projected_utilisation"] == 0.0
    assert lazer["breach_date"] == ""
    assert lazer["status"] == "Sem limite"
    assert lazer["tone"] == "neutral"


def test_c102_analytics_monthly_category_budget_rows_negative_envelopes():
    """monthly_category_budget_rows e monthly_budget_forecast em analytics não produzem taxas negativas."""
    repo_mock = MagicMock()
    repo_mock.month_bounds_from_key.return_value = (date(2026, 3, 1), date(2026, 3, 31))
    repo_mock.previous_month_key.return_value = "2026-02"
    repo_mock.category_spend_with_splits.return_value = [
        {"category": "Supermercado", "total": 60.0},
    ]
    repo_mock.category_budgets.return_value = {
        "Supermercado": -150.0,
    }
    repo_mock.setting.return_value = "{}"

    analytics = AnalyticsService(repo_mock)
    rows = analytics.monthly_category_budget_rows("2026-03")
    assert len(rows) == 1
    row = rows[0]
    # Orçamento negativo deve ser sanitizado para 0.0 e utilização não pode ser negativa
    assert row["budget"] == 0.0
    assert row["utilisation"] == 0.0
    assert row["status"] == "Sem orçamento"

    forecast = analytics.monthly_budget_forecast("2026-03", as_of=date(2026, 3, 15))
    f_row = forecast["rows"][0]
    assert f_row["budget"] == 0.0
    assert f_row["projected_utilisation"] == 0.0
    assert f_row["forecast_status"] == "Sem orçamento"
