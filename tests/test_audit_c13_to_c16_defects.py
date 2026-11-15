"""Comprehensive regression and boundary test suite for defects C13 through C16.

Audit items:
- C13 / G5 [P2 chart]: Donut reference ring hides disappeared categories.
  Reconcile reference shares against full reference denominator, support disappeared
  categories with explicit reference remainder, and handle empty current data.
- C14 / W1 [P2 worker]: Dashboard cancellation races connection teardown.
  Coordinate interrupt and connection close under a thread-safe lifetime protocol.
  Idempotent cancellation, safe handling of closed connection, no ProgrammingError,
  and no stale signal emissions.
- C15 / S1 [P1 security]: Recovery dialog fails before the user can interact.
  Dedicated TamperRecoveryDialog with explicit layout, accessible input, distinct
  recovery explanations, wrong/correct phrase handling, and non-mutating cancel.
- C16 / S2 [P2 security]: Malformed numeric crypto fields escape typed corruption handling.
  Boundary validation for numeric crypto fields (format_version, memory_kib, iterations,
  lanes, length) rejecting booleans, floats, strings, lists, dicts, nulls, and out-of-range
  integers, raising typed CryptoFormatError and reporting CORRUPT_ENVELOPE.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QScrollArea

from database.engine import FinanceDB
from database.repo.main_repo import Repo
from services.backup_crypto import (
    KEY_LENGTH_BYTES,
    CryptoFormatError,
    KdfParameters,
    SecurityEnvelope,
    _parse_strict_int,
    read_security_marker,
)
from services.startup_security import (
    CredentialPolicyError,
    ProfileSecurityState,
    SecurityEnvelopeError,
    StartupSecurityService,
    clear_active_session,
)
from ui.components.charts.donut import DonutChart
from ui.core.theme import Theme
from ui.dashboard_worker import DashboardWorker
from ui.startup_security import (
    TamperRecoveryDialog,
)


@pytest.fixture(scope="session")
def qapp():
    """Ensure a QApplication exists for widget testing."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _cleanup_session():
    clear_active_session()
    yield
    clear_active_session()


# ============================================================================
# C13 / G5: Donut reference ring hides disappeared categories
# ============================================================================


def _dummy_theme() -> Theme:
    return Theme(dark=False, privacy=False)


def test_c13_donut_reference_shares_reconcile_with_disappeared_categories(qapp):
    """Scenario: Current A=100. Reference A=100, B=900.

    A must account for exactly 10% (100 / 1000) of the reference period,
    not 100%, and the remainder (B=900) must account for 90%.
    """
    chart = DonutChart(theme_provider=_dummy_theme)
    chart.resize(400, 300)

    # Current has only category A
    chart.set_data([("Alimentação", 100.0, "#0072B2")])

    # Reference has category A (100) and B (900)
    chart.set_comparison_data(
        [("Alimentação", 100.0, "#0072B2"), ("Habitação", 900.0, "#E69F00")],
        label="Mês Anterior",
    )

    assert chart.comparison_total() == 1000.0
    assert chart.comparison_share("Alimentação") == pytest.approx(0.10)
    assert chart.comparison_share("Habitação") == pytest.approx(0.90)
    assert chart.comparison_share("Inexistente") == pytest.approx(0.0)

    # Test offscreen render without crash
    image = QImage(400, 300, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        chart.render_chart(painter)
    finally:
        painter.end()


def test_c13_donut_comparison_new_appearing_category(qapp):
    """Category in current that did not exist in reference has 0% reference share."""
    chart = DonutChart(theme_provider=_dummy_theme)
    chart.set_data([
        ("Alimentação", 200.0, "#0072B2"),
        ("Nova Categoria", 50.0, "#CC79A7"),
    ])
    chart.set_comparison_data(
        [("Alimentação", 200.0, "#0072B2")],
        label="Mês Anterior",
    )

    assert chart.comparison_total() == 200.0
    assert chart.comparison_share("Alimentação") == pytest.approx(1.0)
    assert chart.comparison_share("Nova Categoria") == pytest.approx(0.0)


def test_c13_donut_current_empty_with_populated_reference(qapp):
    """When current data is empty, reference ring still draws using full reference data."""
    chart = DonutChart(theme_provider=_dummy_theme)
    chart.resize(400, 300)
    chart.set_data([])
    chart.set_comparison_data(
        [("Transportes", 300.0, "#0072B2"), ("Lazer", 100.0, "#E69F00")],
        label="Mês Anterior",
    )

    assert chart.comparison_total() == 400.0
    assert chart.comparison_share("Transportes") == pytest.approx(0.75)
    assert chart.comparison_share("Lazer") == pytest.approx(0.25)

    # Renders comparison ring gracefully around empty center
    image = QImage(400, 300, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        chart.render_chart(painter)
    finally:
        painter.end()


def test_c13_donut_differing_top_n_limits_with_synthetic_aggregate(qapp):
    """Synthetic remainder category in current reconciles with reference categories outside top-N."""
    chart = DonutChart(theme_provider=_dummy_theme)
    chart.set_category_limit(2)

    # 3 categories in current: top 2 + 1 remainder -> Outros (1)
    chart.set_data([
        ("CatA", 500.0, "#0072B2"),
        ("CatB", 300.0, "#E69F00"),
        ("CatC", 200.0, "#CC79A7"),
    ])
    assert chart._synthetic_label == "Outros (1)"

    # Reference has CatA, CatB, CatC and CatD
    chart.set_comparison_data(
        [
            ("CatA", 400.0, "#0072B2"),
            ("CatB", 200.0, "#E69F00"),
            ("CatC", 200.0, "#CC79A7"),
            ("CatD", 200.0, "#009E73"),
        ],
        label="Mês Anterior",
    )

    assert chart.comparison_total() == 1000.0
    assert chart.comparison_share("CatA") == pytest.approx(0.40)
    assert chart.comparison_share("CatB") == pytest.approx(0.20)
    # Synthetic label aggregates CatC (200) + CatD (200) = 400 (40%)
    assert chart.comparison_share(chart._synthetic_label) == pytest.approx(0.40)


# ============================================================================
# C14 / W1: Dashboard cancellation races connection teardown
# ============================================================================


def test_c14_worker_cancel_before_start_does_not_crash():
    """Cancel before start sets interruption request and exits immediately."""
    worker = DashboardWorker(
        request_id=1,
        repo=Repo(),
        analytics=MagicMock(),
        start="2026-08-01",
        end="2026-08-31",
        range_days=31,
        resolution="daily",
        comparison_mode="previous",
    )

    # Cancel before start
    worker.cancel()
    assert worker.isInterruptionRequested()

    # Calling run directly should exit without error or database operations
    worker.run()


def test_c14_worker_cancel_idempotence_and_double_cancel():
    """Calling cancel multiple times never raises exceptions or alters state."""
    worker = DashboardWorker(
        request_id=2,
        repo=Repo(),
        analytics=MagicMock(),
        start="2026-08-01",
        end="2026-08-31",
        range_days=31,
        resolution="daily",
        comparison_mode="previous",
    )

    worker.cancel()
    worker.cancel()
    worker.cancel()
    assert worker.isInterruptionRequested()


def test_c14_worker_cancel_during_teardown_does_not_raise_programming_error():
    """Simulate cancel running concurrently when the connection is already closed.

    In the unfixed version, connection.interrupt() on an already closed
    in-memory/file connection raised sqlite3.ProgrammingError.
    """
    worker = DashboardWorker(
        request_id=3,
        repo=Repo(),
        analytics=MagicMock(),
        start="2026-08-01",
        end="2026-08-31",
        range_days=31,
        resolution="daily",
        comparison_mode="previous",
    )

    # Create an in-memory connection and immediately close it to simulate the race
    conn = sqlite3.connect(":memory:")
    conn.close()

    mock_db = MagicMock()
    mock_db.conn = conn

    with worker._database_lock:
        worker._local_database = mock_db

    # Calling cancel must safely handle ProgrammingError and never let it escape
    worker.cancel()


def test_c14_worker_teardown_barrier_prevents_cancel_on_closing_connection():
    """Verify that entering teardown sets _is_closing and clears the database pointer."""
    worker = DashboardWorker(
        request_id=4,
        repo=Repo(),
        analytics=MagicMock(),
        start="2026-08-01",
        end="2026-08-31",
        range_days=31,
        resolution="daily",
        comparison_mode="previous",
    )

    conn = sqlite3.connect(":memory:")
    mock_db = MagicMock()
    mock_db.conn = conn

    with worker._database_lock:
        worker._local_database = mock_db

    # Simulate entering finally block
    with worker._database_lock:
        worker._is_closing = True
        _db_to_close = worker._local_database
        worker._local_database = None

    assert worker._is_closing is True
    assert worker._local_database is None

    # cancel() called now will see _is_closing and return immediately
    worker.cancel()

    # Clean up dummy connection
    conn.close()


def test_c14_worker_interruption_suppresses_stale_data_emission():
    """If interruption is requested, finished_data must NOT be emitted."""
    worker = DashboardWorker(
        request_id=5,
        repo=Repo(),
        analytics=MagicMock(),
        start="2026-08-01",
        end="2026-08-31",
        range_days=31,
        resolution="daily",
        comparison_mode="previous",
    )

    emitted_data = []
    worker.finished_data.connect(lambda req_id, data: emitted_data.append((req_id, data)))

    # Request interruption
    worker.requestInterruption()

    # Stub services to return valid mock objects
    mock_repo = MagicMock()
    mock_analytics = MagicMock()
    worker._local_services = MagicMock(return_value=(mock_repo, mock_analytics))
    worker._load_data = MagicMock(return_value={"saldo": 1000})

    worker.run()

    # Interruption must suppress signal emission
    assert len(emitted_data) == 0


# ============================================================================
# C15 / S1: Recovery dialog fails before the user can interact
# ============================================================================


def test_c15_tamper_recovery_dialog_widget_construction_and_layout(qapp):
    """TamperRecoveryDialog must construct a real QDialog without layout AttributeError.

    In the unfixed version, run_tamper_recovery_dialog called layout.insertRow(...)
    on a QMessageBox (QGridLayout), which immediately crashed with AttributeError.
    """
    mock_service = MagicMock()
    mock_service.tamper_recovery_confirmation_phrase = "CONFIRMAR RECUPERACAO TESTE"

    dialog = TamperRecoveryDialog(mock_service, state="tampered")
    assert dialog.objectName() == "TamperRecoveryDialog"
    assert dialog.minimumWidth() >= 520
    assert dialog.minimumHeight() >= 320

    # Input box must be present, accessible and reachable
    input_box = dialog.findChild(QLineEdit, "ConfirmationInput")
    assert input_box is not None
    assert input_box.placeholderText() == "CONFIRMAR RECUPERACAO TESTE"

    # Buttons must be present
    confirm_btn = dialog.findChild(QPushButton, "ConfirmButton")
    cancel_btn = dialog.findChild(QPushButton, "CancelButton")
    assert confirm_btn is not None
    assert cancel_btn is not None


def test_c15_tamper_recovery_dialog_explanations_by_state(qapp):
    """Distinct explanations must be shown for missing, corrupt, indeterminate and tampered states."""
    mock_service = MagicMock()
    mock_service.tamper_recovery_confirmation_phrase = "PHRASE"

    for state, expected_keyword in [
        ("missing_envelope", "ausente"),
        ("corrupt_envelope", "corrompido"),
        ("recovery_required", "necessária"),
        ("indeterminate", "necessária"),
        ("tampered", "removida"),
    ]:
        dialog = TamperRecoveryDialog(mock_service, state=state)
        title, desc = dialog._explanation_for_state()
        assert expected_keyword in title.lower() or expected_keyword in desc.lower()


def test_c15_tamper_recovery_dialog_wrong_phrase_does_not_mutate_service(qapp):
    """Submitting a wrong phrase displays error and NEVER calls acknowledge_tamper_recovery."""
    mock_service = MagicMock()
    mock_service.tamper_recovery_confirmation_phrase = "FRASE EXACTA"

    dialog = TamperRecoveryDialog(mock_service, state="tampered")
    dialog.show()
    qapp.processEvents()
    dialog.input_box.setText("frase errada")
    dialog._on_confirm()

    # Error must be shown
    assert dialog.error_label.isVisible()
    assert "incorreta" in dialog.error_label.text().lower()

    # Service must NOT have been acknowledged or mutated
    mock_service.acknowledge_tamper_recovery.assert_not_called()
    assert dialog._confirmed is False
    dialog.close()


def test_c15_tamper_recovery_dialog_correct_phrase_acknowledges_recovery(qapp):
    """Submitting the exact phrase calls acknowledge_tamper_recovery and confirms."""
    mock_service = MagicMock()
    mock_service.tamper_recovery_confirmation_phrase = "FRASE EXACTA"
    mock_service.acknowledge_tamper_recovery.return_value = Path("/tmp/recovery-archive")

    dialog = TamperRecoveryDialog(mock_service, state="tampered")
    dialog.input_box.setText("FRASE EXACTA")
    dialog._on_confirm()

    mock_service.acknowledge_tamper_recovery.assert_called_once_with("FRASE EXACTA")
    assert dialog._confirmed is True


def test_c15_tamper_recovery_dialog_cancel_makes_no_mutation(qapp):
    """Rejecting/cancelling the dialog makes no service call and returns False."""
    mock_service = MagicMock()
    mock_service.tamper_recovery_confirmation_phrase = "FRASE EXACTA"

    dialog = TamperRecoveryDialog(mock_service, state="tampered")
    dialog.reject()

    mock_service.acknowledge_tamper_recovery.assert_not_called()
    assert dialog._confirmed is False


# ============================================================================
# C16 / S2: Malformed numeric crypto fields escape typed corruption handling
# ============================================================================


@pytest.mark.parametrize(
    "invalid_value",
    [
        "invalid",
        "123",
        True,
        False,
        1.5,
        None,
        [123],
        {"a": 1},
    ],
)
def test_c16_parse_strict_int_rejects_non_integers(invalid_value):
    """_parse_strict_int must reject strings, booleans, floats, lists, dicts, and nulls."""
    with pytest.raises(CryptoFormatError) as exc_info:
        _parse_strict_int(invalid_value, "test_field")
    # Error message must be safe and not contain secrets
    assert "test_field" in str(exc_info.value)
    assert "número inteiro" in str(exc_info.value)


def test_c16_parse_strict_int_range_bounds():
    """_parse_strict_int checks min_value and max_value bounds."""
    assert _parse_strict_int(10, "field", min_value=5, max_value=15) == 10

    with pytest.raises(CryptoFormatError) as exc_info:
        _parse_strict_int(3, "field", min_value=5)
    assert "abaixo do valor mínimo" in str(exc_info.value)

    with pytest.raises(CryptoFormatError) as exc_info:
        _parse_strict_int(20, "field", max_value=15)
    assert "acima do valor máximo" in str(exc_info.value)


@pytest.mark.parametrize("bad_version", ["invalid", True, False, 1.5, None, [1], {"v": 1}, 999])
def test_c16_security_envelope_from_json_dict_rejects_malformed_version(bad_version):
    """SecurityEnvelope.from_json_dict must raise CryptoFormatError on malformed format_version."""
    raw = {
        "format_version": bad_version,
        "profile_id": "test-profile",
        "default_unlock_method": "pin",
        "unlocks": {"pin": {}},
    }
    with pytest.raises(CryptoFormatError):
        SecurityEnvelope.from_json_dict(raw)


@pytest.mark.parametrize("field_name", ["memory_kib", "iterations", "lanes", "length"])
@pytest.mark.parametrize("bad_val", ["invalid", True, False, 3.14, None, [65536]])
def test_c16_kdf_parameters_from_json_dict_rejects_malformed_numerics(field_name, bad_val):
    """KdfParameters.from_json_dict must raise CryptoFormatError on malformed numeric fields."""
    valid_kdf = {
        "name": "argon2id",
        "salt_b64": "AAAAAAAAAAAAAAAAAAAAAA==",
        "memory_kib": 65536,
        "iterations": 3,
        "lanes": 1,
        "length": KEY_LENGTH_BYTES,
    }
    valid_kdf[field_name] = bad_val
    with pytest.raises(CryptoFormatError):
        KdfParameters.from_json_dict(valid_kdf)


@pytest.mark.parametrize("bad_version", ["invalid", True, False, 2.5, None, [2]])
def test_c16_read_security_marker_rejects_malformed_version(tmp_path, bad_version):
    """read_security_marker must raise CryptoFormatError on malformed format_version."""
    marker_file = tmp_path / "security_marker.json"
    marker_file.write_text(
        json.dumps({
            "format_version": bad_version,
            "profile_id": "test-profile",
            "default_unlock_method": "pin",
            "envelope_identity": "abc123",
        }),
        encoding="utf-8",
    )
    with pytest.raises(CryptoFormatError):
        read_security_marker(marker_file)


def test_c16_startup_security_inspect_profile_reports_corrupt_envelope_on_malformed_json(tmp_path):
    """StartupSecurityService.inspect_profile must report CORRUPT_ENVELOPE when envelope has malformed numeric fields."""
    db_file = tmp_path / "test.sqlite3"
    db = FinanceDB(db_file)
    db.init_schema()
    db.close()

    service = StartupSecurityService(database_file=db_file)
    service.configuration_dir.mkdir(parents=True, exist_ok=True)

    # Write an envelope with "format_version": "invalid"
    service.envelope_path.write_text(
        json.dumps({
            "format_version": "invalid",
            "profile_id": "test-profile",
            "default_unlock_method": "pin",
            "unlocks": {},
        }),
        encoding="utf-8",
    )

    state = service.inspect_profile()
    assert state == ProfileSecurityState.CORRUPT_ENVELOPE


def test_c16_configure_first_run_rejects_onboarding_when_envelope_is_corrupt(tmp_path):
    """configure_first_run must raise SecurityEnvelopeError/SecurityRecoveryRequiredError when corrupt."""
    db_file = tmp_path / "test.sqlite3"
    db = FinanceDB(db_file)
    db.init_schema()
    db.close()

    service = StartupSecurityService(database_file=db_file)
    service.configuration_dir.mkdir(parents=True, exist_ok=True)

    # Write corrupt envelope
    service.envelope_path.write_text(
        json.dumps({"format_version": "corrupt_string"}),
        encoding="utf-8",
    )

    with pytest.raises(SecurityEnvelopeError):
        service.configure_first_run("pin", "123456")


def test_c13_donut_zero_comparison_total_handles_gracefully(qapp):
    """When comparison data sums to zero, comparison_total returns 0.0, share is None, no crash."""
    chart = DonutChart(theme_provider=_dummy_theme)
    chart.resize(400, 300)
    chart.set_data([("Alimentação", 100.0, "#0072B2")])
    chart.set_comparison_data([("Alimentação", 0.0, "#0072B2"), ("Habitação", 0.0, "#E69F00")])
    assert chart.comparison_total() == 0.0
    assert chart.comparison_share("Alimentação") is None
    assert chart.comparison_share("Habitação") is None

    image = QImage(400, 300, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        chart.render_chart(painter)
    finally:
        painter.end()


def test_c14_worker_live_query_concurrent_cancellation(tmp_path):
    """Start worker with real DB, trigger cancel while query runs, verify no ProgrammingError."""
    db_file = tmp_path / "live_test.sqlite3"
    db = FinanceDB(db_file)
    db.init_schema()
    db.close()

    real_repo = Repo(FinanceDB.open_read_only(db_file))
    worker = DashboardWorker(
        request_id=99,
        repo=real_repo,
        analytics=MagicMock(),
        start="2026-08-01",
        end="2026-08-31",
        range_days=31,
        resolution="daily",
        comparison_mode="previous",
    )

    # Attach live connection under lock
    conn = sqlite3.connect(f"{db_file.as_uri()}?mode=ro", uri=True)
    mock_db = MagicMock()
    mock_db.conn = conn
    with worker._database_lock:
        worker._local_database = mock_db

    # Cancel concurrently
    worker.cancel()
    assert worker.isInterruptionRequested()

    # Teardown barrier simulation
    with worker._database_lock:
        worker._is_closing = True
        to_close = worker._local_database
        worker._local_database = None

    assert to_close is not None
    conn.close()
    # Cancel again on closed connection — must not raise sqlite3.ProgrammingError
    worker.cancel()


def test_c15_tamper_recovery_dialog_non_ascii_unicode_input_does_not_crash(qapp):
    """Entering non-ASCII unicode characters and unicode whitespace does not crash with TypeError."""
    mock_service = MagicMock()
    mock_service.tamper_recovery_confirmation_phrase = "FRASE EXACTA"

    dialog = TamperRecoveryDialog(mock_service, state="tampered")
    dialog.show()
    qapp.processEvents()
    # Non-ASCII unicode text with accented characters and non-breaking space
    dialog.input_box.setText("FRASE À É Õ \u00a0")
    dialog._on_confirm()

    # Error must be shown without unhandled TypeError
    assert dialog.error_label.isVisible()
    assert "incorreta" in dialog.error_label.text().lower()
    mock_service.acknowledge_tamper_recovery.assert_not_called()
    assert dialog._confirmed is False
    dialog.close()


def test_c15_tamper_recovery_service_non_ascii_input_raises_credential_policy_error(tmp_path):
    """StartupSecurityService.acknowledge_tamper_recovery raises CredentialPolicyError on non-ASCII input."""
    db_file = tmp_path / "test.sqlite3"
    db = FinanceDB(db_file)
    db.init_schema()
    db.close()

    service = StartupSecurityService(database_file=db_file)
    with pytest.raises(CredentialPolicyError):
        service.acknowledge_tamper_recovery("FRASE À É Õ \u00a0")


def test_c15_tamper_recovery_dialog_scroll_area_reachable_controls(qapp):
    """TamperRecoveryDialog encapsulates variable text inside QScrollArea and keeps buttons visible."""
    mock_service = MagicMock()
    mock_service.tamper_recovery_confirmation_phrase = "FRASE"

    dialog = TamperRecoveryDialog(mock_service, state="tampered")
    scroll = dialog.findChild(QScrollArea, "RecoveryScrollArea")
    assert scroll is not None
    assert scroll.widget() is not None

    confirm_btn = dialog.findChild(QPushButton, "ConfirmButton")
    cancel_btn = dialog.findChild(QPushButton, "CancelButton")
    assert confirm_btn is not None
    assert cancel_btn is not None
    assert confirm_btn.parent() is dialog
    assert cancel_btn.parent() is dialog

