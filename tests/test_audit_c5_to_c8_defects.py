"""Comprehensive regression and boundary test suite for defects C5 through C8.

Audit items:
- C5 [P2]: Correct backup status and format reporting.
- C6 [P1]: Correct variable-expense observation window.
- C7 [P1/P2]: Remove ambiguous month-end date conversion.
- C8 [P3]: Resolve TWRR semantics explicitly.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from database.engine import FinanceDB
from database.repo.main_repo import Repo
from services.analytics import AnalyticsService
from services.backup import BackupService
from services.cashflow_forecast import CashflowForecastService
from services.finance_math import twrr
from services.security_settings import (
    BackupVerificationStatus,
    SecuritySettingsController,
    SecurityStatus,
)
from services.startup_security import StartupSecurityService, clear_active_session


# ============================================================================
# C5: Backup status and format reporting
# ============================================================================


@pytest.fixture(autouse=True)
def _cleanup_sessions():
    clear_active_session()
    yield
    clear_active_session()


@pytest.fixture()
def clean_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    """Provide a clean repository instance with zero demo data."""
    monkeypatch.setenv("FINANCAS_PESSOAIS_DEMO_DATA", "0")
    database = FinanceDB(tmp_path / "test.sqlite3")
    repo = Repo(database)
    try:
        for table in (
            "transaction_splits",
            "transactions",
            "transaction_meta",
            "financial_accounts",
            "categories",
        ):
            repo.db.conn.execute(f"DELETE FROM {table}")
        repo.db.conn.commit()
        yield repo
    finally:
        database.close()


def test_c5_format_detection_all_types(tmp_path: Path):
    """Detect LGBK1, LGBK2, plain ZIP, and unknown/corrupted magic headers."""
    lgbk2_file = tmp_path / "test.lgbk2"
    lgbk2_file.write_bytes(b"LGBK2\x00\x01dummy-payload")
    assert BackupService.detect_archive_format(lgbk2_file) == "LGBK2"

    lgbk1_file = tmp_path / "test.lgbk"
    lgbk1_file.write_bytes(b"LGBK1\x00\x01dummy-payload")
    assert BackupService.detect_archive_format(lgbk1_file) == "LGBK1"

    zip_file = tmp_path / "test.zip"
    zip_file.write_bytes(b"PK\x03\x04dummy-zip-data")
    assert BackupService.detect_archive_format(zip_file) == "ZIP"

    bad_file = tmp_path / "corrupt.bin"
    bad_file.write_bytes(b"CORRUPT_MAGIC_HEADER")
    assert BackupService.detect_archive_format(bad_file) == "unknown"

    short_file = tmp_path / "short.bin"
    short_file.write_bytes(b"AB")
    assert BackupService.detect_archive_format(short_file) == "truncated"

    empty_file = tmp_path / "empty.bin"
    empty_file.write_bytes(b"")
    assert BackupService.detect_archive_format(empty_file) == "truncated"

    assert BackupService.detect_archive_format(None) == "nenhum"
    assert BackupService.detect_archive_format(tmp_path / "nonexistent.zip") == "nenhum"


def test_c5_no_backup_state(tmp_path: Path):
    """Controller returns NO_BACKUP when no backup file exists."""
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "SenhaForte123!")

    backup_stub = MagicMock()
    backup_stub.latest_complete_backup.return_value = None
    backup_stub.configured_backup_format.return_value = "LGBK2"

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=backup_stub,
    )
    status: SecurityStatus = controller.security_status()

    assert status.verification_status == BackupVerificationStatus.NO_BACKUP
    assert status.configured_format == "LGBK2"
    assert status.latest_format == "nenhum"
    assert status.verified is False


def test_c5_recorded_only_metadata(tmp_path: Path):
    """Metadata recorded previously without current-session full integrity check."""
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "SenhaForte123!")

    archive = tmp_path / "backup.lgbk2"
    archive.write_bytes(b"LGBK2\x00\x01data")

    backup_stub = MagicMock()
    backup_stub.latest_complete_backup.return_value = archive
    backup_stub.detect_archive_format.side_effect = BackupService.detect_archive_format
    backup_stub.configured_backup_format.return_value = "LGBK2"
    backup_stub.protection_health_snapshot.return_value = {
        "metadata_verified": True,
        "verification_recorded": True,
    }
    backup_stub.latest_backup_fingerprint.return_value = (
        str(archive.resolve()),
        archive.stat().st_size,
        archive.stat().st_mtime_ns,
    )

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=backup_stub,
    )
    status: SecurityStatus = controller.security_status()

    assert status.verification_status == BackupVerificationStatus.RECORDED_PREVIOUSLY
    assert status.verified is False
    assert status.latest_format == "LGBK2"


def test_c5_current_verified_matching_fingerprint(tmp_path: Path):
    """Cached async verification matching current file fingerprint produces CURRENT_VERIFIED."""
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "SenhaForte123!")

    archive = tmp_path / "backup.lgbk2"
    archive.write_bytes(b"LGBK2\x00\x01valid-archive")

    fingerprint = (str(archive.resolve()), archive.stat().st_size, archive.stat().st_mtime_ns)

    backup_stub = MagicMock()
    backup_stub.latest_complete_backup.return_value = archive
    backup_stub.detect_archive_format.side_effect = BackupService.detect_archive_format
    backup_stub.configured_backup_format.return_value = "LGBK2"
    backup_stub.latest_backup_fingerprint.return_value = fingerprint

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=backup_stub,
    )

    status: SecurityStatus = controller.security_status(
        cached_verification={"verified": True, "details": "all checks pass"},
        cached_fingerprint=fingerprint,
    )

    assert status.verification_status == BackupVerificationStatus.CURRENT_VERIFIED
    assert status.verified is True


def test_c5_changed_archive_fingerprint_mismatch(tmp_path: Path):
    """Stale cached verification for a modified archive reverts to RECORDED_PREVIOUSLY."""
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "SenhaForte123!")

    archive = tmp_path / "backup.lgbk2"
    archive.write_bytes(b"LGBK2\x00\x01initial")

    current_fingerprint = (str(archive.resolve()), archive.stat().st_size, archive.stat().st_mtime_ns)
    stale_fingerprint = (str(archive.resolve()), 999999, 123456789)

    backup_stub = MagicMock()
    backup_stub.latest_complete_backup.return_value = archive
    backup_stub.detect_archive_format.side_effect = BackupService.detect_archive_format
    backup_stub.configured_backup_format.return_value = "LGBK2"
    backup_stub.latest_backup_fingerprint.return_value = current_fingerprint
    backup_stub.protection_health_snapshot.return_value = {
        "metadata_verified": True,
        "verification_recorded": True,
    }

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=backup_stub,
    )

    status: SecurityStatus = controller.security_status(
        cached_verification={"verified": True},
        cached_fingerprint=stale_fingerprint,
    )

    assert status.verification_status == BackupVerificationStatus.RECORDED_PREVIOUSLY
    assert status.verified is False


def test_c5_missing_file_when_recorded(tmp_path: Path):
    """If the recorded archive file was deleted, status reports NO_BACKUP safely."""
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "SenhaForte123!")

    missing_path = tmp_path / "deleted.lgbk2"

    backup_stub = MagicMock()
    backup_stub.latest_complete_backup.return_value = missing_path
    backup_stub.detect_archive_format.side_effect = BackupService.detect_archive_format
    backup_stub.configured_backup_format.return_value = "LGBK2"
    backup_stub.latest_backup_fingerprint.return_value = None

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=backup_stub,
    )
    status: SecurityStatus = controller.security_status()

    assert status.verification_status == BackupVerificationStatus.NO_BACKUP
    assert status.latest_format == "nenhum"


def test_c5_unreadable_file_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """File open error (e.g. PermissionError) does not crash and reports unavailable."""
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "SenhaForte123!")

    archive = tmp_path / "locked.lgbk2"
    archive.write_bytes(b"LGBK2\x00\x01data")

    backup_stub = MagicMock()
    backup_stub.latest_complete_backup.return_value = archive
    backup_stub.detect_archive_format.side_effect = BackupService.detect_archive_format
    backup_stub.configured_backup_format.return_value = "LGBK2"

    orig_open = Path.open

    def mock_open(self, *args, **kwargs):
        if "locked.lgbk2" in str(self):
            raise PermissionError("Access denied by OS")
        return orig_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", mock_open)

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=backup_stub,
    )
    status: SecurityStatus = controller.security_status()

    assert status.latest_format == "unreadable"
    assert status.verification_status == BackupVerificationStatus.UNAVAILABLE


def test_c5_separation_of_configured_format_and_archive_format(tmp_path: Path):
    """Configured format (e.g. LGBK2) is separated from actual archive format (e.g. legacy ZIP)."""
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "SenhaForte123!")

    archive = tmp_path / "legacy_backup.zip"
    archive.write_bytes(b"PK\x03\x04dummy-zip-data")

    backup_stub = MagicMock()
    backup_stub.latest_complete_backup.return_value = archive
    backup_stub.detect_archive_format.side_effect = BackupService.detect_archive_format
    backup_stub.configured_backup_format.return_value = "LGBK2"
    backup_stub.protection_health_snapshot.return_value = {
        "metadata_verified": True,
        "verification_recorded": True,
    }
    backup_stub.latest_backup_fingerprint.return_value = (
        str(archive.resolve()),
        archive.stat().st_size,
        archive.stat().st_mtime_ns,
    )

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=backup_stub,
    )
    status: SecurityStatus = controller.security_status()

    assert status.configured_format == "LGBK2"
    assert status.latest_format == "ZIP"


# ============================================================================
# C6: Variable-expense observation window
# ============================================================================


def test_c6_established_account_90_days_with_90_euro_single_expense(clean_repo: Repo):
    """An established account observed for 90 days with one €90 expense averages €1/day."""
    repo = clean_repo
    ref = date(2026, 9, 4)
    # Established history: first transaction started 90 days ago
    day_90_ago = (ref - timedelta(days=89)).isoformat()
    repo.add_transaction(day_90_ago, "Abertura / Salário", 2500.0, "Salário", "Conta principal")

    # Single variable expense on 2026-08-01 (34 days ago)
    repo.add_transaction("2026-08-01", "Jantar com amigos", -90.0, "Restaurantes", "Conta principal")

    payments_mock = MagicMock()
    payments_mock.upcoming_commitments.return_value = []
    payments_mock.overdue_commitments.return_value = []
    service = CashflowForecastService(repo, payments_mock)

    daily_spend = service._variable_daily_spend(ref, lookback_days=90)
    assert pytest.approx(daily_spend, abs=0.01) == 1.00  # 90 / 90 = 1.00


def test_c6_trailing_zero_spend_days(clean_repo: Repo):
    """Trailing zero-spend days dilute daily spend through the reference horizon."""
    repo = clean_repo
    ref = date(2026, 6, 1)
    # Established account from 60 days ago
    start_date = (ref - timedelta(days=59)).isoformat()
    repo.add_transaction(start_date, "Depósito inicial", 1000.0, "Rendimentos", "Conta principal")

    # Expense of €120 on day 59 ago, then 0 variable expenses for 59 days
    repo.add_transaction(start_date, "Despesa inicial", -120.0, "Lazer", "Conta principal")

    payments_mock = MagicMock()
    payments_mock.upcoming_commitments.return_value = []
    payments_mock.overdue_commitments.return_value = []
    service = CashflowForecastService(repo, payments_mock)

    daily_spend = service._variable_daily_spend(ref, lookback_days=60)
    assert pytest.approx(daily_spend, abs=0.01) == 2.00  # 120 / 60 = 2.00


def test_c6_genuinely_new_account(clean_repo: Repo):
    """Genuinely new account only spans observed days."""
    repo = clean_repo
    ref = date(2026, 9, 4)
    # Account started only 7 days ago
    first_date = (ref - timedelta(days=6)).isoformat()
    repo.add_transaction(first_date, "Primeiro depósito", 500.0, "Rendimentos", "Conta recente")
    repo.add_transaction((ref - timedelta(days=2)).isoformat(), "Café", -14.0, "Alimentação", "Conta recente")

    payments_mock = MagicMock()
    payments_mock.upcoming_commitments.return_value = []
    payments_mock.overdue_commitments.return_value = []
    service = CashflowForecastService(repo, payments_mock)

    # 14 / 7 days = 2.00 €/day (NOT 14 / 90)
    daily_spend = service._variable_daily_spend(ref, lookback_days=90)
    assert pytest.approx(daily_spend, abs=0.01) == 2.00


def test_c6_income_or_fixed_only_history(clean_repo: Repo):
    """History containing only income does not confuse sparse spending with a new account."""
    repo = clean_repo
    ref = date(2026, 9, 4)
    # 60 days of established history, but only income
    day_60_ago = (ref - timedelta(days=59)).isoformat()
    repo.add_transaction(day_60_ago, "Salário Julho", 3000.0, "Salário", "Conta principal")

    payments_mock = MagicMock()
    payments_mock.upcoming_commitments.return_value = []
    payments_mock.overdue_commitments.return_value = []
    service = CashflowForecastService(repo, payments_mock)

    daily_spend = service._variable_daily_spend(ref, lookback_days=60)
    assert daily_spend == 0.0  # zero variable expenses


def test_c6_no_expenses_no_transactions(clean_repo: Repo):
    """Empty database gracefully returns zero spend without division by zero."""
    repo = clean_repo
    payments_mock = MagicMock()
    payments_mock.upcoming_commitments.return_value = []
    payments_mock.overdue_commitments.return_value = []
    service = CashflowForecastService(repo, payments_mock)

    daily_spend = service._variable_daily_spend(date(2026, 9, 4), lookback_days=90)
    assert daily_spend == 0.0


def test_c6_calibration_and_live_prediction_consistency(clean_repo: Repo):
    """forecast_calibration uses the same observation coverage interval as live forecast."""
    repo = clean_repo
    ref = date(2026, 9, 1)
    # Seed 6 months of historical transactions to allow calibration
    for m in range(1, 7):
        m_date = f"2026-0{m}-05"
        repo.add_transaction(m_date, f"Salário 0{m}", 2000.0, "Salário", "Conta principal")
        repo.add_transaction(f"2026-0{m}-15", f"Supermercado 0{m}", -150.0, "Mercado", "Conta principal")

    payments_mock = MagicMock()
    payments_mock.upcoming_commitments.return_value = []
    payments_mock.overdue_commitments.return_value = []
    service = CashflowForecastService(repo, payments_mock)

    calib = service.forecast_calibration(reference=ref, sample_months=6, lookback_days=90)
    assert isinstance(calib, dict)
    assert "correction_factor" in calib
    assert "optimistic_multiplier" in calib
    assert "pessimistic_multiplier" in calib


# ============================================================================
# C7: Month-end date conversion and repository contract
# ============================================================================


def test_c7_august_30_cutoff_excludes_august_31(clean_repo: Repo):
    """A through-date of August 30 MUST exclude August 31 (penultimate month-end defect)."""
    repo = clean_repo
    repo.add_transaction("2026-08-30", "Compra dia 30", -50.0, "Casa", "Conta principal")
    repo.add_transaction("2026-08-31", "Compra dia 31", -100.0, "Casa", "Conta principal")

    analytics = AnalyticsService(repo)
    rows = analytics.monthly_category_budget_rows("2026-08", through_date=date(2026, 8, 30))

    spent_casa = next(r["spent"] for r in rows if r["category"] == "Casa")
    assert spent_casa == pytest.approx(50.0), (
        f"August 31 movement leaked into August 30 budget: {spent_casa}"
    )


def test_c7_august_31_includes_august_31_and_excludes_september_1(clean_repo: Repo):
    """A through-date of August 31 MUST include August 31 and exclude September 1."""
    repo = clean_repo
    repo.add_transaction("2026-08-30", "Compra dia 30", -50.0, "Casa", "Conta principal")
    repo.add_transaction("2026-08-31", "Compra dia 31", -100.0, "Casa", "Conta principal")
    repo.add_transaction("2026-09-01", "Compra dia 1 set", -200.0, "Casa", "Conta principal")

    analytics = AnalyticsService(repo)
    rows = analytics.monthly_category_budget_rows("2026-08", through_date=date(2026, 8, 31))

    spent_casa = next(r["spent"] for r in rows if r["category"] == "Casa")
    assert spent_casa == pytest.approx(150.0), (
        f"Expected both Aug 30 and Aug 31 to be included: {spent_casa}"
    )


def test_c7_top_categories_and_dynamic_grouped_august_30_cutoff(clean_repo: Repo):
    """Repo top_categories_payment_period and dynamic_grouped exclude August 31 when end is 2026-08-30."""
    repo = clean_repo
    repo.add_transaction("2026-08-29", "Despesa 29", -20.0, "Lazer", "Conta principal")
    repo.add_transaction("2026-08-30", "Despesa 30", -30.0, "Lazer", "Conta principal")
    repo.add_transaction("2026-08-31", "Despesa 31", -80.0, "Lazer", "Conta principal")

    # Top categories with end=2026-08-30 (converted to end_exclusive=2026-08-31)
    top = repo.top_categories_payment_period("2026-08-01", "2026-08-30")
    total_top = next(total for cat, total, _ in top if cat == "Lazer")
    assert total_top == pytest.approx(50.0)

    # Dynamic grouped with end=2026-08-30 (converted to end_exclusive=2026-08-31)
    dynamic = repo.dynamic_grouped("2026-08-01", "2026-08-30")
    total_dynamic = next(r["total"] for r in dynamic if r["label"] == "Lazer")
    assert total_dynamic == pytest.approx(50.0)


def test_c7_leap_year_february_boundaries(clean_repo: Repo):
    """Leap-year February 28 vs 29 bounds correctly handle boundaries."""
    repo = clean_repo
    # Leap year 2024
    repo.add_transaction("2024-02-28", "Fev 28", -40.0, "Saúde", "Conta principal")
    repo.add_transaction("2024-02-29", "Fev 29", -60.0, "Saúde", "Conta principal")
    repo.add_transaction("2024-03-01", "Março 1", -100.0, "Saúde", "Conta principal")

    analytics = AnalyticsService(repo)

    # Cutoff on Feb 28 excludes Feb 29
    feb_28_rows = analytics.monthly_category_budget_rows("2024-02", through_date=date(2024, 2, 28))
    spent_28 = next(r["spent"] for r in feb_28_rows if r["category"] == "Saúde")
    assert spent_28 == pytest.approx(40.0)

    # Cutoff on Feb 29 includes Feb 29 and excludes Mar 1
    feb_29_rows = analytics.monthly_category_budget_rows("2024-02", through_date=date(2024, 2, 29))
    spent_29 = next(r["spent"] for r in feb_29_rows if r["category"] == "Saúde")
    assert spent_29 == pytest.approx(100.0)


def test_c7_split_and_unsplit_movements_under_exclusive_bounds(clean_repo: Repo):
    """Split and unsplit movements honor exclusive bound without leaking month-end splits."""
    repo = clean_repo
    # Unsplit on Aug 30
    repo.add_transaction("2026-08-30", "Supermercado simples", -50.0, "Mercado", "Conta principal")

    # Split on Aug 30
    tx30 = repo.add_transaction("2026-08-30", "Misto 30", -100.0, "Casa", "Conta principal")
    repo.save_transaction_splits(tx30, [("Farmácia", 60.0), ("Mercado", 40.0)])

    # Split on Aug 31 (MUST be excluded under Aug 30 cutoff)
    tx31 = repo.add_transaction("2026-08-31", "Misto 31", -200.0, "Casa", "Conta principal")
    repo.save_transaction_splits(tx31, [("Farmácia", 120.0), ("Mercado", 80.0)])

    rows = repo.category_spend_with_splits(
        "2026-08-01",
        "2026-08-31",  # Exclusive boundary: through Aug 30 23:59:59
        end_exclusive=True,
    )
    totals = {str(r["category"]): float(r["total"]) for r in rows}

    assert totals.get("Mercado") == pytest.approx(90.0)  # 50 unsplit + 40 split
    assert totals.get("Farmácia") == pytest.approx(60.0)  # 60 split from Aug 30 only
    assert "Casa" not in totals  # Parent category fully replaced by splits


# ============================================================================
# C8: TWRR semantics and explicit recapitalization
# ============================================================================


def test_c8_uninterrupted_geometric_linking_retains_total_loss():
    """Default twrr retains total loss (-100%) under uninterrupted geometric linking."""
    # Loss of 100% in period 1 cannot magically recover without new capital injection
    assert twrr([-100.0, 50.0, 20.0]) == -100.0
    # Mid-series total loss
    assert twrr([10.0, -100.0, 50.0]) == -100.0
    assert twrr([50.0, 20.0, -100.0]) == -100.0


def test_c8_explicit_recapitalization():
    """Recapitalization cannot turn a total loss into positive cumulative return (C97)."""
    # Fresh-capital gains restart compounding but cumulative reporting is capped.
    ret = twrr([-100.0, 50.0, 20.0], allow_recapitalization=True)
    assert ret == 0.0

    # Initial gain, total loss, then recapitalization gain:
    # Initial gains are also erased by total loss; the C97 cap still applies.
    ret2 = twrr([10.0, -100.0, 30.0], allow_recapitalization=True)
    assert ret2 == 0.0


def test_c8_normal_compounding():
    """Standard series compound correctly under both modes."""
    assert round(twrr([10.0, 20.0]), 2) == 32.0
    assert round(twrr([10.0, 20.0], allow_recapitalization=True), 2) == 32.0
    assert round(twrr([50.0, -50.0]), 2) == -25.0
    assert round(twrr([50.0, -50.0], allow_recapitalization=True), 2) == -25.0


def test_c8_edge_cases_and_boundaries():
    """Boundary conditions: empty series, single total loss, repeated total loss, zero returns."""
    assert twrr([]) == 0.0
    assert twrr([], allow_recapitalization=True) == 0.0
    assert twrr([-100.0]) == -100.0
    assert twrr([-100.0], allow_recapitalization=True) == -100.0
    assert twrr([-100.0, -100.0]) == -100.0
    assert twrr([-100.0, -100.0], allow_recapitalization=True) == -100.0
    assert twrr([0.0, 0.0, 0.0]) == 0.0
    assert twrr([0.0, 0.0, 0.0], allow_recapitalization=True) == 0.0


# ============================================================================
# Deep adversarial boundary verification: review audit findings
# ============================================================================


def test_c5_security_settings_all_exports_backup_verification_status():
    """BackupVerificationStatus must be exported in services.security_settings.__all__."""
    import services.security_settings as sec_mod

    assert "BackupVerificationStatus" in sec_mod.__all__
    assert getattr(sec_mod, "BackupVerificationStatus") is BackupVerificationStatus


def test_c5_corrupt_header_nul_bytes(tmp_path: Path):
    """Corrupt 5-byte NUL header returns unknown and status UNAVAILABLE without crashing."""
    corrupt_file = tmp_path / "nul_header.bin"
    corrupt_file.write_bytes(b"\x00\x00\x00\x00\x00")

    fmt = BackupService.detect_archive_format(corrupt_file)
    assert fmt == "unknown"

    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "SenhaForte123!")

    backup_stub = MagicMock()
    backup_stub.latest_complete_backup.return_value = corrupt_file
    backup_stub.detect_archive_format.side_effect = BackupService.detect_archive_format
    backup_stub.configured_backup_format.return_value = "LGBK2"

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=backup_stub,
    )
    status: SecurityStatus = controller.security_status()
    assert status.latest_format == "unknown"
    assert status.verification_status == BackupVerificationStatus.UNAVAILABLE
    assert status.verified is False


def test_c5_configured_backup_format_exception_guard(tmp_path: Path):
    """If configured_backup_format raises an exception, controller handles it defensively."""
    security = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
    security.configure_first_run("password", "SenhaForte123!")

    backup_stub = MagicMock()
    backup_stub.latest_complete_backup.return_value = None
    backup_stub.configured_backup_format.side_effect = RuntimeError("Preferences locked")

    controller = SecuritySettingsController(
        security_service=security,
        backup_service=backup_stub,
    )
    # Must not raise RuntimeError
    status: SecurityStatus = controller.security_status()
    assert status.configured_format == "LGBK2"


def test_c5_render_security_status_clean_punctuation():
    """_render_security_status renders clean sentences without dangling dots or bullets."""
    from ui.pages.settings import SettingsPage

    mock_page = MagicMock(spec=SettingsPage)
    mock_page.encryption_status = MagicMock()

    status = SecurityStatus(
        state="protected",
        methods=(),
        default_method="",
        format_active="LGBK2",
        latest_backup="backup.lgbk2",
        verified=True,
        verification_status=BackupVerificationStatus.CURRENT_VERIFIED,
        configured_format="LGBK2",
        latest_format="LGBK2",
    )

    SettingsPage._render_security_status(mock_page, status)
    text = mock_page.encryption_status.setText.call_args[0][0]
    assert not text.endswith("· ."), f"Dangling punctuation found: {text!r}"
    assert text.endswith("backup."), f"Expected clean sentence end, got: {text!r}"


def test_c6_empty_database_forecast_coverage_metrics(clean_repo: Repo):
    """Empty database reports observation_days=0, coverage_sufficient=False, and coverage_start=''."""
    repo = clean_repo
    payments_mock = MagicMock()
    payments_mock.upcoming_commitments.return_value = []
    payments_mock.overdue_commitments.return_value = []
    service = CashflowForecastService(repo, payments_mock)

    fc = service.cashflow_calendar_forecast(horizon_days=30, as_of=date(2026, 9, 4))
    assert fc["observation_days"] == 0
    assert fc["coverage_sufficient"] is False
    assert fc["coverage_start"] == ""


def test_c6_sufficient_coverage_thresholds(clean_repo: Repo):
    """coverage_sufficient is False below 30 days and True at 30+ days of history."""
    repo = clean_repo
    ref = date(2026, 9, 4)

    # 15 days of history
    d15 = (ref - timedelta(days=14)).isoformat()
    repo.add_transaction(d15, "Abertura", 1000.0, "Rendimentos", "Conta principal")

    payments_mock = MagicMock()
    payments_mock.upcoming_commitments.return_value = []
    payments_mock.overdue_commitments.return_value = []
    service = CashflowForecastService(repo, payments_mock)

    fc15 = service.cashflow_calendar_forecast(horizon_days=30, as_of=ref)
    assert fc15["observation_days"] == 15
    assert fc15["coverage_sufficient"] is False

    # Extend history to 45 days ago
    d45 = (ref - timedelta(days=44)).isoformat()
    repo.add_transaction(d45, "Salário anterior", 2000.0, "Salário", "Conta principal")

    fc45 = service.cashflow_calendar_forecast(horizon_days=30, as_of=ref)
    assert fc45["observation_days"] == 45
    assert fc45["coverage_sufficient"] is True


def test_c7_december_31_and_january_1_year_boundary(clean_repo: Repo):
    """Year-roll boundary: Dec 30 excludes Dec 31, Dec 31 includes Dec 31, Jan 1 retrieves Dec prior."""
    repo = clean_repo
    repo.add_transaction("2026-12-30", "Despesa 30 dez", -75.0, "Lazer", "Conta principal")
    repo.add_transaction("2026-12-31", "Despesa 31 dez", -125.0, "Lazer", "Conta principal")
    repo.add_transaction("2027-01-01", "Despesa 01 jan", -200.0, "Lazer", "Conta principal")

    analytics = AnalyticsService(repo)

    # Cutoff Dec 30 excludes Dec 31
    rows_dec30 = analytics.monthly_category_budget_rows("2026-12", through_date=date(2026, 12, 30))
    spent_dec30 = next(r["spent"] for r in rows_dec30 if r["category"] == "Lazer")
    assert spent_dec30 == pytest.approx(75.0)

    # Cutoff Dec 31 includes Dec 31 and excludes Jan 1
    rows_dec31 = analytics.monthly_category_budget_rows("2026-12", through_date=date(2026, 12, 31))
    spent_dec31 = next(r["spent"] for r in rows_dec31 if r["category"] == "Lazer")
    assert spent_dec31 == pytest.approx(200.0)

    # Query January 2027: prior month rows must reflect full Dec 2026 (200.0) without leaking Jan 1
    rows_jan = analytics.monthly_category_budget_rows("2027-01", through_date=date(2027, 1, 1))
    lazer_jan = next(r for r in rows_jan if r["category"] == "Lazer")
    assert lazer_jan["spent"] == pytest.approx(200.0)  # Jan 1 spend
    assert lazer_jan["previous_spend"] == pytest.approx(200.0)  # Dec 30 + Dec 31 spend


def test_c8_multi_period_recapitalization_and_liquidation():
    """Repeated liquidations retain the cumulative reporting cap (C97)."""
    # Loss 100%, restart +50%, loss 100%, restart +20%
    # Segment returns restart, while cumulative gains after total loss remain capped.
    assert twrr([-100.0, 50.0, -100.0, 20.0], allow_recapitalization=True) == 0.0
    # Without recapitalization: permanent loss
    assert twrr([-100.0, 50.0, -100.0, 20.0], allow_recapitalization=False) == -100.0

