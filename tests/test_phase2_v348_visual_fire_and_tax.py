from __future__ import annotations

import pytest
from pathlib import Path
from PySide6.QtWidgets import QApplication

from database.repo.main_repo import Repo
from services.fire import FirePlanningEngine
from ui.dialogs import PortugueseTaxDialog


def _get_qapp():
    return QApplication.instance() or QApplication([])


def test_fire_milestones_and_guardrails_engine():
    milestones = FirePlanningEngine.calculate_fire_milestones(
        current_invested=100000.0,
        annual_expenses=20000.0,
        annual_savings=10000.0,
        real_growth_rate_pct=5.0,
        swr_pct=4.0,
    )
    assert "standard" in milestones
    assert milestones["standard"].target_capital == 500000.0
    assert milestones["standard"].progress_pct == 20.0
    assert milestones["standard"].years_to_goal is not None

    assert "lean" in milestones
    assert milestones["lean"].target_capital == 375000.0

    srr_stable = FirePlanningEngine.simulate_sequence_of_returns_risk(
        initial_capital=500000.0,
        initial_annual_withdrawal=20000.0,
        duration_years=30,
        average_real_return_pct=5.0,
        early_crash_scenario="baseline_steady",
    )
    assert srr_stable.portfolio_survived is True
    assert srr_stable.final_capital_real > 0


def test_portuguese_tax_dialog_flow(tmp_path: Path, monkeypatch: pytest.Monkeypatch):
    _ = _get_qapp()
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    monkeypatch.setenv("FINANCAS_PESSOAIS_STATE_DIR", str(tmp_path / "profile"))

    repo = Repo()
    try:
        repo.db.conn.execute("""
            INSERT INTO holdings (name, asset_class, value, cost_basis, notes)
            VALUES
            ('VWCE ETF', 'Ações / ETFs', 15000.0, 10000.0, 'Vanguard FTSE All-World'),
            ('Bitcoin', 'Criptoativos', 8000.0, 3000.0, 'Cold storage >365d')
        """)
        repo.db.conn.commit()

        dialog = PortugueseTaxDialog(repo)
        try:
            assert dialog.table.rowCount() == 2
            assert "VWCE ETF" in dialog._summary_text
            assert "Bitcoin" in dialog._summary_text
            assert "Isenções Cripto" in dialog._summary_text
        finally:
            dialog.close()
    finally:
        repo.close()
