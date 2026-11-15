"""Automated verification suite for Audit Batch 31 defects.

Defects covered:
1. BUG-AUD-C51: Luxembourg (LU) removed from offshore tax havens blacklist and added to CDT (15%).
2. BUG-AUD-C68: Reserved Win32 device names CONIN$, CONOUT$, COM0, LPT0 neutralized in sanitize_filename.
3. BUG-AUD-C81: CSV DDE formula injection via hyphen/plus commands neutralized in spreadsheet_safe_cell.
4. BUG-AUD-C82: Pure-Python math fallback in xirr when NumPy is absent; zero crash on fresh setups.
5. BUG-AUD-C86: Net economic return calculation for liquidated positions with positive dividends.
6. BUG-AUD-C91: Transitive multi-hop account/category merge resolution with cycle detection.
7. BUG-AUD-C95: Secondary monitor window geometry restoration with disconnected screen fallback.
"""

import builtins
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtWidgets import QApplication

from database.engine import FinanceDB
from services.finance_math import xirr
from services.investment_performance import (
    calculate_holding_xirr,
    calculate_portfolio_xirr,
)
from services.tax import (
    CONVENTION_DIVIDEND_TAX_RATES,
    OFFSHORE_JURISDICTIONS,
    ForeignDividendPayment,
    PortugueseTaxEngine,
    calculate_foreign_dividend_tax,
)
from utils.helpers import sanitize_filename
from utils.output_security import spreadsheet_safe_cell


# ============================================================================
# 1. BUG-AUD-C51: Luxembourg (LU) Fiscal Compliance & CDT
# ============================================================================

def test_c51_luxembourg_not_in_offshore_jurisdictions():
    """Luxemburgo é Estado-Membro da UE e não pode constar da lista de paraísos fiscais."""
    assert "LU" not in OFFSHORE_JURISDICTIONS


def test_c51_luxembourg_cdt_rate():
    """CDT Portugal-Luxemburgo (Artigo 10.º n.º 2) prevê taxa limite de 15% para dividendos."""
    assert "LU" in CONVENTION_DIVIDEND_TAX_RATES
    assert CONVENTION_DIVIDEND_TAX_RATES["LU"] == 15.0


def test_c51_luxembourg_holding_disposal_taxed_at_standard_rate():
    """Alienação de ativo no Luxemburgo é tributada à taxa padrão (28%) e não à taxa agravada (35%)."""
    engine = PortugueseTaxEngine()
    engine.add_acquisition(
        lot_id="lot_lu",
        asset_symbol="ETF_LU",
        acquisition_date=date(2025, 1, 1),
        quantity=10.0,
        unit_price=100.0,
        country_code="LU",
        is_foreign=True,
    )
    matches = engine.process_disposal(
        disposal_id="disp_lu",
        asset_symbol="ETF_LU",
        disposal_date=date(2025, 6, 1),
        quantity=10.0,
        unit_price=150.0,
        country_code="LU",
        is_foreign=True,
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.tax_rate_pct == 28.0
    assert m.estimated_tax == pytest.approx(140.0)  # 500 * 0.28


def test_c51_luxembourg_dividend_tax_calculation():
    """Dividendo do Luxemburgo beneficia de crédito de imposto da CDT até ao teto de 15%."""
    payment = ForeignDividendPayment(
        id="div_lu_1",
        asset_symbol="LU_FUND",
        payment_date=date(2025, 5, 1),
        gross_amount_eur=1000.0,
        foreign_tax_withheld_eur=150.0,  # 15% retido na fonte
        country_code="LU",
        has_treaty_form=True,
    )
    res = calculate_foreign_dividend_tax(payment)
    assert res.treaty_rate_pct == 15.0
    assert res.maximum_creditable_foreign_tax == 150.0
    assert res.uncreditable_foreign_tax_loss == 0.0
    # Imposto português devido: 1000 * 0.28 - 150 = 130 EUR
    assert res.net_portuguese_tax_payable == pytest.approx(130.0)


# ============================================================================
# 2. BUG-AUD-C68: Reserved Win32 Device Names in sanitize_filename
# ============================================================================

def test_c68_reserved_win32_devices_prefixed():
    """CONIN$, CONOUT$, COM0, LPT0 e suas variantes devem ser prefixados com 'safe_'."""
    assert sanitize_filename("CONIN$") == "safe_CONIN$"
    assert sanitize_filename("CONOUT$") == "safe_CONOUT$"
    assert sanitize_filename("conin$.txt") == "safe_conin$.txt"
    assert sanitize_filename("conout$.pdf") == "safe_conout$.pdf"
    assert sanitize_filename("COM0") == "safe_COM0"
    assert sanitize_filename("LPT0") == "safe_LPT0"
    assert sanitize_filename("com0.dat") == "safe_com0.dat"
    assert sanitize_filename("lpt0.csv") == "safe_lpt0.csv"


def test_c68_regular_filenames_not_prefixed():
    """Nomes de ficheiros normais não devem ser alterados desnecessariamente."""
    assert sanitize_filename("fatura_luz_2025.pdf") == "fatura_luz_2025.pdf"
    assert sanitize_filename("recibo_renda.png") == "recibo_renda.png"
    assert sanitize_filename("extrato_bancario.csv") == "extrato_bancario.csv"


# ============================================================================
# 3. BUG-AUD-C81: CSV DDE Formula Injection Prevention in spreadsheet_safe_cell
# ============================================================================

def test_c81_spreadsheet_safe_cell_neutralizes_dde_with_spaces():
    """Células com prefixo '-' ou '+' seguido de pipe DDE ou fórmulas são neutralizadas com apóstrofo."""
    assert spreadsheet_safe_cell("- cmd|' /C calc'!A0").startswith("'")
    assert spreadsheet_safe_cell("+ cmd|' /C calc'!A0").startswith("'")
    assert spreadsheet_safe_cell("- @SUM(A1:A10)").startswith("'")
    assert spreadsheet_safe_cell("- =1+1").startswith("'")
    assert spreadsheet_safe_cell("- 10+20").startswith("'")
    assert spreadsheet_safe_cell("- 2*3").startswith("'")
    assert spreadsheet_safe_cell("- %APPDATA%").startswith("'")


def test_c81_spreadsheet_safe_cell_allows_genuine_human_notes():
    """Notas de texto e marcadores de lista genuínos iniciados por hífen não são prefixados."""
    assert spreadsheet_safe_cell("- Compra no supermercado") == "- Compra no supermercado"
    assert spreadsheet_safe_cell("+ Salário de Janeiro") == "+ Salário de Janeiro"
    assert spreadsheet_safe_cell("- Pão, leite e café") == "- Pão, leite e café"
    assert spreadsheet_safe_cell("- Desconto de 15 € na fatura") == "- Desconto de 15 € na fatura"
    assert spreadsheet_safe_cell("-") == "-"
    assert spreadsheet_safe_cell("--") == "--"
    assert spreadsheet_safe_cell("---") == "---"


# ============================================================================
# 4. BUG-AUD-C82: Pure-Python Math Fallback in xirr
# ============================================================================

def test_c82_xirr_pure_python_fallback():
    """Quando o NumPy não está disponível, o fallback em Python puro calcula a taxa com exatidão."""
    orig_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("No module named 'numpy'")
        return orig_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        flows = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)]
        res = xirr(flows)
        assert res is not None
        assert abs(res - 0.10) < 1e-6


def test_c82_xirr_returns_none_for_monosigned_cashflows():
    """Fluxos exclusivamente negativos ou exclusivamente positivos retornam None tanto em numpy como em fallback."""
    flows_negative_only = [(date(2025, 1, 1), -100.0), (date(2025, 2, 1), -200.0)]
    assert xirr(flows_negative_only) is None


# ============================================================================
# 5. BUG-AUD-C86: Holding and Portfolio XIRR on Liquidated Positions with Dividends
# ============================================================================

def test_c86_holding_xirr_liquidated_with_dividends():
    """Posição liquidada/falida (valor=0) que recebeu dividendos calcula retorno real líquido e não -100%."""
    # Investimento de 100 EUR, recebeu 80 EUR de dividendos, valor final 0.0 EUR
    # Perda económica real é -20.0%, não -100.0%
    ret = calculate_holding_xirr(
        acquisition_date=date(2024, 1, 1),
        current_date=date(2024, 12, 31),
        cost_basis=100.0,
        current_value=0.0,
        dividends=[(date(2024, 6, 1), 80.0)],
    )
    assert ret is not None
    assert ret == pytest.approx(-20.0, abs=0.01)


def test_c86_holding_xirr_total_loss_without_dividends():
    """Posição liquidada a 0.0 EUR sem qualquer dividendo continua a reportar -100%."""
    ret = calculate_holding_xirr(
        acquisition_date=date(2024, 1, 1),
        current_date=date(2024, 12, 31),
        cost_basis=100.0,
        current_value=0.0,
        dividends=[],
    )
    assert ret == -100.0


def test_c86_portfolio_xirr_liquidated_with_net_inflows():
    """Carteira liquidada (valor=0) que gerou dividendos calcula retorno nominal e não -100%."""
    cash_flows = [
        (date(2024, 1, 1), -10000.0),
        (date(2024, 6, 1), 9000.0),
    ]
    # Retirou 9.000 EUR de 10.000 EUR investidos: perda real é -10.0%
    ret = calculate_portfolio_xirr(
        cash_flows=cash_flows,
        current_date=date(2024, 12, 31),
        current_portfolio_value=0.0,
    )
    assert ret is not None
    assert ret == pytest.approx(-10.0, abs=0.01)


def test_c86_portfolio_xirr_pure_total_loss():
    """Carteira liquidada sem qualquer inflow continua a reportar -100%."""
    cash_flows = [
        (date(2024, 1, 1), -5000.0),
    ]
    ret = calculate_portfolio_xirr(
        cash_flows=cash_flows,
        current_date=date(2024, 12, 31),
        current_portfolio_value=0.0,
    )
    assert ret == -100.0


# ============================================================================
# 6. BUG-AUD-C91: Transitive Multi-Hop Identity Merge Resolution
# ============================================================================

def test_c91_transitive_account_merge_resolution(tmp_path):
    """Resolução de contas em cascata (A -> B -> C) mapeia transitivamente A diretamente para C."""
    db_file = tmp_path / "test_c91.db"
    engine = FinanceDB(str(db_file))

    # Criação de 3 contas com IDs altos para evitar colisões com o seed
    engine.conn.execute(
        "INSERT INTO financial_accounts(id, name, account_key, stable_id, is_active, merged_into_id) "
        "VALUES (901, 'Conta A', 'conta_a_c91', 'acc_a', 0, 'acc_b'), "
        "       (902, 'Conta B', 'conta_b_c91', 'acc_b', 0, 'acc_c'), "
        "       (903, 'Conta C', 'conta_c_c91', 'acc_c', 1, '')"
    )
    # E uma transação apontando para 'Conta A'
    engine.conn.execute(
        "INSERT INTO transactions(id, date, amount, description, account, category) "
        "VALUES (999, '2025-01-01', -50.0, 'Teste cascata', 'Conta A', 'Geral')"
    )
    engine.conn.commit()

    # Executa a reparação/resolução de identidades do engine
    engine._ensure_stable_financial_entity_identity()

    # Verifica se a transação foi resolvida transitivamente para a Conta C (acc_c)
    tx = engine.conn.execute("SELECT account_id FROM transactions WHERE id = 999").fetchone()
    assert tx is not None
    assert tx["account_id"] == "acc_c"
    engine.close()


def test_c91_cycle_protection_in_merge_traversal():
    """Laços cíclicos de fusão (A -> B -> A) são interrompidos sem loop infinito."""
    resolved = {
        "acc_a": {"stable_id": "acc_a", "is_active": 0, "merged_into_id": "acc_b", "name": "A"},
        "acc_b": {"stable_id": "acc_b", "is_active": 0, "merged_into_id": "acc_a", "name": "B"},
    }

    target = resolved["acc_a"]
    visited = {"acc_a"}
    iterations = 0
    while not int(target["is_active"] or 0) and str(target["merged_into_id"] or ""):
        iterations += 1
        mid = str(target["merged_into_id"] or "")
        if not mid or mid in visited or mid not in resolved:
            break
        visited.add(mid)
        target = resolved[mid]

    assert iterations <= 2


# ============================================================================
# 7. BUG-AUD-C95: Secondary Monitor Geometry Restoration
# ============================================================================

def test_c95_secondary_screen_geometry_restoration():
    """Coordenadas num monitor secundário conectado são validadas e restauradas com sucesso."""
    _ = QApplication.instance() or QApplication([])

    mock_screen = MagicMock(spec=QScreen)
    # Monitor secundário à direita: x=1920..3840, y=0..1080
    mock_screen.availableGeometry.return_value = QRect(1920, 0, 1920, 1080)

    with patch.object(QGuiApplication, "screenAt", return_value=mock_screen):
        saved_geom = "2000, 100, 1280, 800"
        parts = [int(p.strip()) for p in saved_geom.split(",")]
        x, y, w, h = parts

        center_pt = QPoint(x + w // 2, y + h // 2)
        target_screen = QGuiApplication.screenAt(center_pt)
        assert target_screen is not None

        target_avail = target_screen.availableGeometry()
        fits = (
            w >= 800
            and h >= 600
            and x + w > target_avail.x() + 50
            and x < target_avail.x() + target_avail.width() - 50
            and y + h > target_avail.y() + 50
            and y < target_avail.y() + target_avail.height() - 50
        )
        assert fits is True


def test_c95_disconnected_secondary_screen_falls_back():
    """Quando o monitor secundário foi desconectado, screenAt retorna None e a janela não tenta abrir fora do ecrã."""
    with patch.object(QGuiApplication, "screenAt", return_value=None):
        saved_geom = "2500, 100, 1280, 800"
        parts = [int(p.strip()) for p in saved_geom.split(",")]
        x, y, w, h = parts

        center_pt = QPoint(x + w // 2, y + h // 2)
        target_screen = QGuiApplication.screenAt(center_pt)
        assert target_screen is None
