"""Automated unit and integration tests for Phase 14 Batch 6.

Defects covered:
1. BNK-PAR-05: Currency codes (EUR, USD, GBP, etc.) and Unicode spaces in ImportService.parse_amount.
2. CASH-02: Cashflow forecast safety buffer with immediate negative starting balance.
3. AUD-37: Unified release gate and provenance verification (scripts/release_gate.py).
4. BUG-TRANSACTION-EMPTY-DESC-001: TransactionDialog description validation rejecting empty descriptions.
"""

from __future__ import annotations

import os
import hashlib
import json
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from scripts.release_gate import verify_release_gate
from services.cashflow_forecast import CashflowForecastService
from services.imports import AMOUNT_LOCALE_INTERNATIONAL, AMOUNT_LOCALE_PT, ImportService
from ui.dialogs import TransactionDialog
from utils.version import APP_VERSION, RELEASE_VERSION


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


# ============================================================================
# 1. BNK-PAR-05: parse_amount with currency suffixes & unicode whitespace
# ============================================================================


def test_bnk_par_05_currency_suffixes_and_unicode_spaces() -> None:
    # Portuguese format with currency suffixes and thin spaces
    assert ImportService.parse_amount("1 250,50 EUR", AMOUNT_LOCALE_PT) == 1250.50
    assert ImportService.parse_amount("1\u202f250,50 €", AMOUNT_LOCALE_PT) == 1250.50
    assert ImportService.parse_amount("1\u2009250,50 CHF", AMOUNT_LOCALE_PT) == 1250.50
    assert ImportService.parse_amount("1\ufeff250,50 €", AMOUNT_LOCALE_PT) == 1250.50
    assert ImportService.parse_amount("1\u00a0250,50 EUR", AMOUNT_LOCALE_PT) == 1250.50

    # International format with USD and GBP
    assert (
        ImportService.parse_amount("1,250.50 USD", AMOUNT_LOCALE_INTERNATIONAL)
        == 1250.50
    )
    assert (
        ImportService.parse_amount("£ 45.20", AMOUNT_LOCALE_INTERNATIONAL) == 45.20
    )
    assert (
        ImportService.parse_amount("$ 1,500.00", AMOUNT_LOCALE_INTERNATIONAL) == 1500.00
    )

    # Negative numbers with currency
    assert ImportService.parse_amount("- 500,00 EUR", AMOUNT_LOCALE_PT) == -500.00
    assert ImportService.parse_amount("(1 250,50 EUR)", AMOUNT_LOCALE_PT) == -1250.50
    assert ImportService.parse_amount("1 250,50 EUR-", AMOUNT_LOCALE_PT) == -1250.50
    assert (
        ImportService.parse_amount("- 1,250.50 USD", AMOUNT_LOCALE_INTERNATIONAL)
        == -1250.50
    )

    # Auto locale detection with currency codes
    assert ImportService.parse_amount("1 250,50 EUR") == 1250.50
    assert ImportService.parse_amount("1 250.50 USD") == 1250.50


# ============================================================================
# 2. CASH-02: Negative starting balance immediate risk date & zero buffer
# ============================================================================


def test_cash_02_negative_starting_balance_immediate_risk() -> None:
    today = date(2026, 9, 4)
    mock_repo = MagicMock()
    mock_repo.all_transactions.return_value = []
    mock_repo.financial_accounts.return_value = []
    mock_repo.active_subscriptions.return_value = []
    mock_repo.active_scheduled_payments.return_value = []
    mock_repo.list_planned_purchases.return_value = []
    mock_repo.cash_balance.return_value = -350.0

    mock_payments = MagicMock()
    mock_payments.active_recurring_payments.return_value = []
    service = CashflowForecastService(mock_repo, mock_payments)

    with (
        patch.object(service, "_recorded_future_events", return_value=[]),
        patch.object(service, "_recurring_income_events", return_value=[]),
        patch.object(service, "_subscription_forecast_events", return_value=[]),
        patch.object(service, "_scheduled_forecast_events", return_value=[]),
        patch.object(service, "_variable_daily_spend", return_value=20.0),
        patch.object(
            service,
            "forecast_calibration",
            return_value={
                "correction_factor": 1.0,
                "optimistic_multiplier": 0.8,
                "pessimistic_multiplier": 1.2,
            },
        ),
    ):
        result = service.cashflow_calendar_forecast(horizon_days=30, as_of=today)

    summary_30 = result["summaries"]["30"]
    assert summary_30["buffer_days"] == 0, f"Expected 0 buffer days, got {summary_30['buffer_days']}"
    assert summary_30["risk_date"] == today.isoformat(), f"Expected {today.isoformat()}, got {summary_30['risk_date']}"
    assert summary_30["status"] == "Saldo negativo previsto"
    assert summary_30["tone"] == "negative"


def test_cash_02_positive_starting_balance_normal_buffer() -> None:
    today = date(2026, 9, 4)
    mock_repo = MagicMock()
    mock_repo.all_transactions.return_value = []
    mock_repo.financial_accounts.return_value = []
    mock_repo.active_subscriptions.return_value = []
    mock_repo.active_scheduled_payments.return_value = []
    mock_repo.list_planned_purchases.return_value = []
    mock_repo.cash_balance.return_value = 5000.0

    mock_payments = MagicMock()
    mock_payments.active_recurring_payments.return_value = []
    service = CashflowForecastService(mock_repo, mock_payments)

    with (
        patch.object(service, "_recorded_future_events", return_value=[]),
        patch.object(service, "_recurring_income_events", return_value=[]),
        patch.object(service, "_subscription_forecast_events", return_value=[]),
        patch.object(service, "_scheduled_forecast_events", return_value=[]),
        patch.object(service, "_variable_daily_spend", return_value=10.0),
        patch.object(
            service,
            "forecast_calibration",
            return_value={
                "correction_factor": 1.0,
                "optimistic_multiplier": 0.8,
                "pessimistic_multiplier": 1.2,
            },
        ),
    ):
        result = service.cashflow_calendar_forecast(horizon_days=30, as_of=today)

    summary_30 = result["summaries"]["30"]
    assert summary_30["buffer_days"] == 30
    assert summary_30["risk_date"] == ""
    assert summary_30["status"] == "Tesouraria estável"
    assert summary_30["tone"] == "positive"


# ============================================================================
# 3. AUD-37: Release gate verification and metadata consistency
# ============================================================================


def test_aud_37_release_gate_verification(tmp_path: Path) -> None:
    # Source checkouts contain no committed release binaries. A synthetic
    # distribution makes this gate regression independent of local ZIPs.
    version_file = tmp_path / "utils/version.py"
    version_file.parent.mkdir()
    version_file.write_text(
        f'APP_VERSION="{APP_VERSION}"\nAPP_VERSION_LABEL="v{APP_VERSION}"\nRELEASE_VERSION="{RELEASE_VERSION}"\n',
        encoding="utf-8",
    )
    for name in ("RELEASE_NOTES.md", "RELEASE_NOTES_RESUME.md"):
        (tmp_path / name).write_text(
            f"## {RELEASE_VERSION} — v{APP_VERSION} — Synthetic release\n",
            encoding="utf-8",
        )
    release = tmp_path / "release"
    release.mkdir()
    package = release / f"LiquidGlass_App_v{APP_VERSION}.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("RELEASE_MANIFEST.json", json.dumps({
            "version": APP_VERSION,
            "installation": {"install_test": {"passed": True}},
        }))
    checksum = hashlib.sha256(package.read_bytes()).hexdigest()
    package.with_suffix(".sha256.txt").write_text(
        f"{checksum}  {package.name}\n", encoding="ascii"
    )
    report = verify_release_gate(project_root=tmp_path)
    assert report["ok"] is True, f"Release gate failed with errors: {report['errors']}"
    assert report["version"] == APP_VERSION
    assert len(report["checks_passed"]) >= 5
    assert len(report["errors"]) == 0

    # Ensure packages in release/ were checked and matched their sidecars
    assert len(report["packages"]) >= 1
    for pkg in report["packages"]:
        assert pkg["sidecar_matched"] is True, f"Sidecar mismatch for {pkg['file']}"
        assert pkg["manifest_found"] is True, f"Manifest not found in {pkg['file']}"
        assert pkg["manifest_version"] == APP_VERSION


def test_aud_37_release_gate_detects_tampered_sidecar(tmp_path: Path) -> None:
    # Setup dummy project root with corrupted sidecar
    utils_dir = tmp_path / "utils"
    utils_dir.mkdir()
    (utils_dir / "__init__.py").write_text("", encoding="utf-8")
    (utils_dir / "version.py").write_text(
        'APP_VERSION = "9.9.9"\n'
        'APP_VERSION_LABEL = "v9.9.9"\n'
        'RELEASE_VERSION = "V9.9"\n'
        'RELEASE_DATE = "2026-09-04"\n',
        encoding="utf-8",
    )
    (tmp_path / "RELEASE_NOTES.md").write_text("## V9.9 — v9.9.9 — Test\n", encoding="utf-8")
    (tmp_path / "RELEASE_NOTES_RESUME.md").write_text("## V9.9 — v9.9.9 — Test\n", encoding="utf-8")

    release_dir = tmp_path / "release"
    release_dir.mkdir()
    dummy_zip = release_dir / "LiquidGlass_App_v9.9.9.zip"
    dummy_zip.write_bytes(b"dummy zip content")
    dummy_sidecar = release_dir / "LiquidGlass_App_v9.9.9.sha256.txt"
    dummy_sidecar.write_text("0000000000000000000000000000000000000000000000000000000000000000  LiquidGlass_App_v9.9.9.zip\n", encoding="ascii")

    report = verify_release_gate(project_root=tmp_path)
    assert report["ok"] is False
    assert any("Checksum mismatch" in err for err in report["errors"])


# ============================================================================
# 4. BUG-TRANSACTION-EMPTY-DESC-001: TransactionDialog empty description rejected
# ============================================================================


def test_bug_transaction_empty_desc_rejected(qapp) -> None:
    mock_repo = MagicMock()
    mock_repo.categories.return_value = ["Alimentação", "Transporte"]
    mock_repo.financial_accounts.return_value = [
        {"id": 1, "name": "Conta principal", "balance_known": 1, "reference_balance": 1000.0, "reference_date": "2026-09-01"}
    ]
    mock_repo.validate_transaction_category.return_value = True

    dialog = TransactionDialog(mock_repo)
    dialog.amount.setValue(25.50)
    dialog.desc.setText("")  # Empty description

    with patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warning:
        dialog.accept()
        mock_warning.assert_called_once()
        args, _ = mock_warning.call_args
        assert "Descrição em falta" in args[1]
        assert dialog.result() != QDialog.Accepted

    # Now provide whitespace only
    dialog.desc.setText("   \t   ")
    with patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warning:
        dialog.accept()
        mock_warning.assert_called_once()
        assert dialog.result() != QDialog.Accepted

    # Now provide valid description
    dialog.desc.setText("Almoço de trabalho")
    with patch("PySide6.QtWidgets.QMessageBox.warning") as mock_warning:
        dialog.accept()
        mock_warning.assert_not_called()
        assert dialog.result() == QDialog.Accepted
        data = dialog.extended_data()
        assert data["description"] == "Almoço de trabalho"
        assert data["amount"] == -25.50

    dialog.close()
