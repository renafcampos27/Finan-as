#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test suite for Phase 8: Clean Code, Typed Domain Models & Clean Architecture."""

from __future__ import annotations

import pytest
from pathlib import Path

from domain.models import (
    Goal,
    Holding,
    Transaction,
)


def test_domain_transaction_properties_and_slots():
    """Verify Transaction slots-optimization and business properties."""
    tx = Transaction(
        id=101,
        date="2026-08-01",
        description="Supermercado Continente",
        amount=-85.50,
        category="Supermercado",
        account="Conta Ordem",
        kind="expense",
    )
    assert tx.is_expense is True
    assert tx.is_income is False
    assert tx.is_transfer is False
    assert tx.absolute_amount() == 85.50

    # Ensure __slots__ is enforced (cannot assign arbitrary undeclared attributes)
    with pytest.raises(AttributeError):
        tx.arbitrary_unknown_field = "invalid"  # type: ignore[attr-defined]


def test_domain_holding_gain_and_return_calculations():
    """Verify Holding calculations for net gains, fees, and percentage returns."""
    holding = Holding(
        id=1,
        name="Vanguard S&P 500 ETF",
        asset_class="Ações",
        symbol="VUSA.AS",
        quantity=50.0,
        unit_price=90.0,
        value=4500.0,
        cost_basis=4000.0,
        fees=20.0,
    )
    assert holding.gross_gain == 500.0
    assert holding.net_gain == 480.0
    # Return = 480 / (4000 + 20) * 100 = 11.9402985%
    assert pytest.approx(holding.return_pct, abs=0.01) == 11.94


def test_domain_goal_progress_and_remaining():
    """Verify Goal progress percentage and remaining targets."""
    goal = Goal(
        id=10,
        name="Fundo de Emergência 6 Meses",
        target_amount=12000.0,
        saved_amount=6000.0,
        goal_type="emergency_fund",
    )
    assert goal.progress_pct == 50.0
    assert goal.remaining_amount == 6000.0


def test_all_python_files_are_valid_utf8_without_bom():
    """Verify zero files have unexpected byte order marks (BOM) or unparsed syntax."""
    import ast

    root = Path(__file__).resolve().parents[1]
    ignored_dirs = {
        ".pytest_cache",
        "__pycache__",
        ".venv",
        "venv",
        "release-venv",
        "artifacts",
        "build",
        "dist",
        ".git",
        ".ruff_cache",
    }
    for py_file in root.rglob("*.py"):
        if any(part in ignored_dirs for part in py_file.parts):
            continue
        raw_bytes = py_file.read_bytes()
        assert not raw_bytes.startswith(b"\xef\xbb\xbf"), (
            f"File {py_file} contains UTF-8 BOM"
        )
        tree = ast.parse(raw_bytes.decode("utf-8"), filename=str(py_file))
        assert tree is not None
