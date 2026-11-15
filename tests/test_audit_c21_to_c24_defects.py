"""Comprehensive regression and boundary test suite for defects C21 through C24.

Audit items:
- C21 / S7 [P2 security/recovery]: Recovery reports success before checking persistence,
  and mislabels errors.
  Make recovery completion conditional on verified resulting state. Return typed
  confirmation (CredentialPolicyError), storage (SecurityRecoveryStorageError), and
  incomplete-recovery (SecurityRecoveryIncompleteError) outcomes. Preserve original
  or archived recovery material on failures; support safe retry without moving
  already-archived files again. Clarify UI copy regarding LGBK2 credential wraps.
- C22 / D1 [P2 UI/splits]: Split selection leaves the amount preview at zero.
  Synchronize monetary controls on transaction selection (for both split and unsplit
  transactions); derive exact reconciled previews using the canonical Largest Remainder
  allocator on percentage changes; guard against signal loops.
- C23 / D2 [P2 privacy/splits]: Existing-splits table ignores privacy mode.
  Apply presentation privacy policy (eur(..., privacy)) to all table amount strings;
  mask tooltips under privacy; preserve raw numeric values in Qt.UserRole; support
  immediate transitions without database mutations.
- C24 / D3 [P2 math/splits]: Split table recalculates percentages and loses exact-cent allocation.
  Display the persisted amount as the source of truth in the existing splits table;
  reuse canonical allocator for unsaved previews; verify €0.01, €0.05, €99.99 exact-cent
  reconciliation and ensure re-saving unchanged splits does not redistribute cents.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from database.repo import Repo
from services.startup_security import (
    TAMPER_RECOVERY_CONFIRMATION,
    CredentialPolicyError,
    ProfileSecurityState,
    SecurityRecoveryIncompleteError,
    SecurityRecoveryStorageError,
    StartupSecurityService,
)
from ui.pages.tools import SplitsPage
from ui.startup_security import TamperRecoveryDialog


@pytest.fixture(scope="session")
def qapp():
    """Ensure a QApplication instance exists for GUI widget testing."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ============================================================================
# C21 / S7: Tamper Recovery Persistence, Error Typing & Safe Retry
# ============================================================================


def test_c21_recovery_rejects_wrong_or_empty_confirmation_phrase(tmp_path: Path):
    """Wrong confirmation phrase must raise CredentialPolicyError without mutating residues."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    envelope = config_dir / "security_envelope.json"
    envelope.write_text('{"format_version": 2}', encoding="utf-8")
    marker = config_dir / "security_marker.json"
    marker.write_text('{"format_version": 2}', encoding="utf-8")

    service = StartupSecurityService(
        database_file=tmp_path / "db.sqlite3", configuration_dir=config_dir
    )

    with pytest.raises(CredentialPolicyError, match="Frase de confirmação incorreta"):
        service.acknowledge_tamper_recovery("frase incorreta")

    with pytest.raises(CredentialPolicyError, match="Frase de confirmação incorreta"):
        service.acknowledge_tamper_recovery("")

    # Residues must remain intact
    assert envelope.is_file()
    assert marker.is_file()


def test_c21_recovery_raises_storage_error_on_residue_move_failure(tmp_path: Path):
    """Failure to archive residue must raise SecurityRecoveryStorageError."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    envelope = config_dir / "security_envelope.json"
    envelope.write_text('{"format_version": 2}', encoding="utf-8")

    service = StartupSecurityService(
        database_file=tmp_path / "db.sqlite3", configuration_dir=config_dir
    )

    with patch("shutil.move", side_effect=OSError("Disk write error")):
        with pytest.raises(
            SecurityRecoveryStorageError, match="Falha de armazenamento"
        ):
            service.acknowledge_tamper_recovery(TAMPER_RECOVERY_CONFIRMATION)


def test_c21_recovery_raises_incomplete_error_on_tombstone_clear_failure(tmp_path: Path):
    """Failure during _clear_tamper_tombstone must raise SecurityRecoveryIncompleteError."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    service = StartupSecurityService(
        database_file=tmp_path / "db.sqlite3", configuration_dir=config_dir
    )

    with patch.object(
        service,
        "_clear_tamper_tombstone",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        with pytest.raises(
            SecurityRecoveryIncompleteError, match="Falha ao limpar o registo"
        ):
            service.acknowledge_tamper_recovery(TAMPER_RECOVERY_CONFIRMATION)


def test_c21_recovery_raises_incomplete_error_if_tombstone_remains_active(
    tmp_path: Path,
):
    """If tombstone remains present after clearing, raise SecurityRecoveryIncompleteError."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    service = StartupSecurityService(
        database_file=tmp_path / "db.sqlite3", configuration_dir=config_dir
    )

    with patch.object(service, "_clear_tamper_tombstone", return_value=None):
        with patch.object(service, "_tamper_tombstone_present", return_value=True):
            with pytest.raises(
                SecurityRecoveryIncompleteError,
                match="registo de proteção .* permanece ativo",
            ):
                service.acknowledge_tamper_recovery(TAMPER_RECOVERY_CONFIRMATION)


def test_c21_recovery_raises_incomplete_error_if_resulting_state_not_clean(
    tmp_path: Path,
):
    """If inspect_profile does not return NEW or LEGACY_UNPROTECTED, raise SecurityRecoveryIncompleteError."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)

    service = StartupSecurityService(
        database_file=tmp_path / "db.sqlite3", configuration_dir=config_dir
    )

    with patch.object(service, "_clear_tamper_tombstone", return_value=None):
        with patch.object(service, "_tamper_tombstone_present", return_value=False):
            with patch.object(
                service,
                "inspect_profile",
                return_value=ProfileSecurityState.CORRUPT_ENVELOPE,
            ):
                with pytest.raises(
                    SecurityRecoveryIncompleteError,
                    match="Recuperação incompleta; perfil permanece",
                ):
                    service.acknowledge_tamper_recovery(TAMPER_RECOVERY_CONFIRMATION)


def test_c21_recovery_safe_retry_after_transient_failure(tmp_path: Path):
    """After a transient tombstone failure where files were already moved, retry must succeed."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    envelope = config_dir / "security_envelope.json"
    envelope.write_text('{"format_version": 2}', encoding="utf-8")

    db_file = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO settings VALUES ('security_tombstone', '{\"protected\": true}')"
    )
    conn.commit()
    conn.close()

    service = StartupSecurityService(
        database_file=db_file, configuration_dir=config_dir
    )

    # First attempt: simulate transient OperationalError during tombstone clear
    with patch.object(
        service,
        "_clear_tamper_tombstone",
        side_effect=sqlite3.OperationalError("database is locked"),
    ):
        with pytest.raises(SecurityRecoveryIncompleteError):
            service.acknowledge_tamper_recovery(TAMPER_RECOVERY_CONFIRMATION)

    # Residue was moved to recovery-archive-* and is preserved
    archives = list(config_dir.glob("recovery-archive-*"))
    assert len(archives) == 1
    assert (archives[0] / "security_envelope.json").is_file()
    assert not envelope.is_file()

    # Second attempt (retry): database is unlocked, operation succeeds
    archive_returned = service.acknowledge_tamper_recovery(
        TAMPER_RECOVERY_CONFIRMATION
    )
    assert archive_returned.is_dir()
    # Tombstone is cleared
    assert not service._tamper_tombstone_present()
    # Profile inspects as LEGACY_UNPROTECTED (orphan database)
    assert service.inspect_profile() == ProfileSecurityState.LEGACY_UNPROTECTED


def test_c21_tamper_recovery_dialog_error_label_mapping(qapp):
    """TamperRecoveryDialog maps typed exceptions to distinct descriptive error labels."""
    service_mock = MagicMock()
    dialog = TamperRecoveryDialog(
        service_mock, state=ProfileSecurityState.TAMPERED
    )
    dialog.show()
    qapp.processEvents()
    dialog.input_box.setText(dialog._phrase)

    # CredentialPolicyError
    service_mock.acknowledge_tamper_recovery.side_effect = CredentialPolicyError(
        "frase recusada"
    )
    dialog._on_confirm()
    assert "Frase de confirmação incorreta" in dialog.error_label.text()
    assert dialog.error_label.isVisible()

    # SecurityRecoveryStorageError
    service_mock.acknowledge_tamper_recovery.side_effect = (
        SecurityRecoveryStorageError("espaço em disco insuficiente")
    )
    dialog._on_confirm()
    assert "Falha de armazenamento" in dialog.error_label.text()

    # SecurityRecoveryIncompleteError
    service_mock.acknowledge_tamper_recovery.side_effect = (
        SecurityRecoveryIncompleteError("registo não apagado")
    )
    dialog._on_confirm()
    assert "Recuperação incompleta" in dialog.error_label.text()
    dialog.close()


def test_c21_tamper_recovery_dialog_ui_copy_explains_lgbk2_credential_wraps(qapp):
    """Dialog UI copy distinguishes original backup credentials from newly configured credentials."""
    service_mock = MagicMock()
    dialog = TamperRecoveryDialog(
        service_mock, state=ProfileSecurityState.TAMPERED
    )
    instruction = dialog.instruction_label.text()
    assert "credencial original" in instruction
    assert "a nova credencial a configurar não os abrirá" in instruction


# ============================================================================
# C22 / D1: Split Selection Preview Synchronization & Canonical Allocator
# ============================================================================


class _FakeTheme:
    privacy: bool = False
    dark: bool = False


class _FakeApp:
    def __init__(self, repo=None):
        self.theme = _FakeTheme()
        self.change_journal_service = MagicMock()
        if repo is None:
            repo = _FakeRepo([], {})
        self.repo = repo
        self.analytics_service = MagicMock()
        self.payments_service = MagicMock()
        self.import_service = MagicMock()

    def notify_data_changed(self, msg: str, **kwargs):
        pass


class _FakeRepo:
    def __init__(self, txs: list[dict], splits_map: dict[int, list[dict]]):
        self._txs = txs
        self._splits_map = splits_map
        self._real_repo = Repo(MagicMock())

    def setting(self, key: str, default: str = "") -> str:
        return default

    def categories(self, kind: str = "expense") -> list[str]:
        return ["Alimentação", "Transportes", "Casa", "Lazer", "Saúde", "Outros"]

    def transactions_filtered(self, kind: str = "Despesas", limit=None) -> list[dict]:
        return self._txs

    def transaction_by_id(self, tx_id: int) -> dict | None:
        for t in self._txs:
            if t["id"] == tx_id:
                return t
        return None

    def transaction_splits(self, tx_id: int) -> list[dict]:
        return self._splits_map.get(tx_id, [])

    def transactions_with_splits(self) -> list[dict]:
        return [t for t in self._txs if t["id"] in self._splits_map and self._splits_map[t["id"]]]

    def _allocate_split_amounts(self, amount: float, normalized: list[tuple[str, float]]) -> list[tuple[str, float, float]]:
        return self._real_repo._allocate_split_amounts(amount, normalized)


def test_c22_splits_page_loads_existing_50_50_split_reconciled(qapp):
    """Loading a stored 100€ transaction with 50/50 splits synchronizes amounts and reconciles diff to 0."""
    txs = [{"id": 1, "kind": "expense", "amount": 100.0, "description": "Supermercado", "date": "2026-09-01", "category": "Alimentação"}]
    splits = {
        1: [
            {"category": "Alimentação", "percent": 50.0, "amount": 50.0},
            {"category": "Casa", "percent": 50.0, "amount": 50.0},
        ]
    }
    repo = _FakeRepo(txs, splits)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.reload_transactions()

    # Select transaction 1
    page.tx.setCurrentIndex(1)

    # Amounts must be synchronized immediately (not 0.0)
    assert page._split_rows[0]["value"].value() == 50.0
    assert page._split_rows[1]["value"].value() == 50.0
    assert page._split_rows[0]["percent"].value() == 50.0
    assert page._split_rows[1]["percent"].value() == 50.0

    # Totals label must show Por atribuir: 0,00 €
    assert "0,00" in page.total_diff_label.text()
    # Donut chart must have 2 slices of 50.0 and no "Por atribuir" remainder
    slice_labels = [s[0] for s in page.donut.data]
    assert "Alimentação" in slice_labels
    assert "Casa" in slice_labels
    assert "Por atribuir" not in slice_labels


def test_c22_splits_page_loads_99_99_at_33_3_reconciled(qapp):
    """Loading €99.99 with 33.3/33.3/33.4 splits synchronizes to 33.30/33.30/33.39 with diff 0."""
    txs = [{"id": 2, "kind": "expense", "amount": 99.99, "description": "Despesa 99.99", "date": "2026-09-02", "category": "Outros"}]
    splits = {
        2: [
            {"category": "Alimentação", "percent": 33.3, "amount": 33.30},
            {"category": "Casa", "percent": 33.3, "amount": 33.30},
            {"category": "Lazer", "percent": 33.4, "amount": 33.39},
        ]
    }
    repo = _FakeRepo(txs, splits)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.reload_transactions()

    page.tx.setCurrentIndex(1)

    assert page._split_rows[0]["value"].value() == 33.30
    assert page._split_rows[1]["value"].value() == 33.30
    assert page._split_rows[2]["value"].value() == 33.39

    assert "0,00" in page.total_diff_label.text()
    slice_labels = [s[0] for s in page.donut.data]
    assert "Por atribuir" not in slice_labels


def test_c22_splits_page_unsplit_transaction_initializes_100_percent_and_base_amount(qapp):
    """Selecting an unsplit transaction defaults row 0 to 100% and base amount with diff 0."""
    txs = [{"id": 3, "kind": "expense", "amount": 75.50, "description": "Restaurante", "date": "2026-09-03", "category": "Alimentação"}]
    repo = _FakeRepo(txs, {})
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.reload_transactions()

    page.tx.setCurrentIndex(1)

    # Row 0 has 100% and full 75.50€
    assert page._split_rows[0]["percent"].value() == 100.0
    assert page._split_rows[0]["value"].value() == 75.50
    assert page._split_rows[0]["category"].currentText() == "Alimentação"

    # Fully reconciled
    assert "0,00" in page.total_diff_label.text()
    assert "Por atribuir" not in [s[0] for s in page.donut.data]


def test_c22_splits_page_switching_between_split_unsplit_and_empty(qapp):
    """Switching split -> unsplit -> empty resets and synchronizes cleanly without residue."""
    txs = [
        {"id": 1, "kind": "expense", "amount": 100.0, "description": "Dividido", "date": "2026-09-01", "category": "Alimentação"},
        {"id": 2, "kind": "expense", "amount": 50.0, "description": "Não dividido", "date": "2026-09-02", "category": "Transportes"},
    ]
    splits = {
        1: [
            {"category": "Alimentação", "percent": 60.0, "amount": 60.0},
            {"category": "Transportes", "percent": 40.0, "amount": 40.0},
        ]
    }
    repo = _FakeRepo(txs, splits)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.reload_transactions()

    # 1. Select split
    page.tx.setCurrentIndex(1)
    assert page._split_rows[0]["value"].value() == 60.0
    assert page._split_rows[1]["value"].value() == 40.0
    assert "0,00" in page.total_diff_label.text()

    # 2. Select unsplit
    page.tx.setCurrentIndex(2)
    assert page._split_rows[0]["value"].value() == 50.0
    assert page._split_rows[0]["percent"].value() == 100.0
    assert page._split_rows[1]["value"].value() == 0.0
    assert page._split_rows[1]["percent"].value() == 0.0
    assert "0,00" in page.total_diff_label.text()

    # 3. Select empty (index 0)
    page.tx.setCurrentIndex(0)
    for r in page._split_rows:
        assert r["value"].value() == 0.0
        assert r["percent"].value() == 0.0
    assert "0,00" in page.total_diff_label.text()


def test_c22_on_percent_changed_derives_exact_canonical_allocation(qapp):
    """Editing percentages in UI uses canonical allocator to derive exact integer cents."""
    txs = [{"id": 1, "kind": "expense", "amount": 0.05, "description": "Mini", "date": "2026-09-01", "category": "Outros"}]
    repo = _FakeRepo(txs, {})
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.reload_transactions()

    page.tx.setCurrentIndex(1)

    # Set 3 categories at 33.34%, 33.33%, 33.33%
    page._split_rows[0]["category"].setCurrentText("Alimentação")
    page._split_rows[1]["category"].setCurrentText("Casa")
    page._split_rows[2]["category"].setCurrentText("Lazer")

    page._updating = True
    page._split_rows[0]["percent"].setValue(33.34)
    page._split_rows[1]["percent"].setValue(33.33)
    page._split_rows[2]["percent"].setValue(33.33)
    page._updating = False

    page.on_percent_changed()

    # Values must be 0.02, 0.02, 0.01 (sum 0.05), NOT 0.02, 0.02, 0.02 (sum 0.06)
    assert page._split_rows[0]["value"].value() == 0.02
    assert page._split_rows[1]["value"].value() == 0.02
    assert page._split_rows[2]["value"].value() == 0.01

    assert "0,00" in page.total_diff_label.text()


def test_c22_partial_percentage_reconciles_unassigned_difference(qapp):
    """Partial percentage (e.g. 40% on 100€) allocates 40.00€ and shows 60.00€ unassigned."""
    txs = [{"id": 1, "kind": "expense", "amount": 100.0, "description": "Parcial", "date": "2026-09-01", "category": "Alimentação"}]
    repo = _FakeRepo(txs, {})
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.reload_transactions()

    page.tx.setCurrentIndex(1)

    page._updating = True
    page._split_rows[0]["percent"].setValue(40.0)
    page._split_rows[1]["percent"].setValue(0.0)
    page._updating = False

    page.on_percent_changed()

    assert page._split_rows[0]["value"].value() == 40.0
    assert "60,00" in page.total_diff_label.text()


# ============================================================================
# C23 / D2: Existing-Splits Table Presentation Privacy Mode Masking
# ============================================================================


def test_c23_existing_splits_table_masks_amounts_in_privacy_mode(qapp):
    """Table amount strings must be masked under privacy mode without leaking values."""
    txs = [{"id": 1, "kind": "expense", "amount": 100.0, "description": "Mercado", "date": "2026-09-01", "category": "Alimentação"}]
    splits = {
        1: [
            {"category": "Alimentação", "percent": 50.0, "amount": 50.0},
            {"category": "Casa", "percent": 50.0, "amount": 50.0},
        ]
    }
    repo = _FakeRepo(txs, splits)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo

    # 1. Privacy OFF
    page.set_privacy(False)
    assert page.table.rowCount() == 1
    val_item = page.table.item(0, 4)
    assert "50,00" in val_item.text()
    assert "•••••" not in val_item.text()
    assert "50,00" in val_item.toolTip()

    # 2. Privacy ON
    page.set_privacy(True)
    val_item = page.table.item(0, 4)
    assert "••••• €" in val_item.text()
    assert "50,00" not in val_item.text()
    assert "50.00" not in val_item.text()
    # Tooltip must also be masked
    assert "50,00" not in val_item.toolTip()
    assert "Modo de privacidade ativo" in val_item.toolTip()


def test_c23_existing_splits_table_preserves_numeric_values_in_user_role(qapp):
    """Numeric allocations remain accessible in Qt.UserRole even when visual presentation is masked."""
    txs = [{"id": 1, "kind": "expense", "amount": 100.0, "description": "Mercado", "date": "2026-09-01", "category": "Alimentação"}]
    splits = {
        1: [
            {"category": "Alimentação", "percent": 70.0, "amount": 70.0},
            {"category": "Casa", "percent": 30.0, "amount": 30.0},
        ]
    }
    repo = _FakeRepo(txs, splits)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.set_privacy(True)

    val_item = page.table.item(0, 4)
    stored_role_data = val_item.data(Qt.UserRole)
    assert stored_role_data == [70.0, 30.0]


def test_c23_existing_splits_table_repeated_privacy_toggling(qapp):
    """Toggling privacy mode repeatedly reveals and masks table consistently without DB mutation."""
    txs = [{"id": 1, "kind": "expense", "amount": 80.0, "description": "Comércio", "date": "2026-09-01", "category": "Alimentação"}]
    splits = {
        1: [
            {"category": "Alimentação", "percent": 50.0, "amount": 40.0},
            {"category": "Lazer", "percent": 50.0, "amount": 40.0},
        ]
    }
    repo = _FakeRepo(txs, splits)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo

    for _ in range(3):
        page.set_privacy(True)
        assert "••••• €" in page.table.item(0, 4).text()
        assert "40,00" not in page.table.item(0, 4).text()

        page.set_privacy(False)
        assert "40,00" in page.table.item(0, 4).text()
        assert "•••••" not in page.table.item(0, 4).text()


# ============================================================================
# C24 / D3: Split Table Exact-Cent Allocation & Persisted Amount Fidelity
# ============================================================================


def test_c24_table_displays_persisted_amount_source_of_truth(qapp):
    """Table displays persisted cents (0.02, 0.02, 0.01) instead of recalculating 0.02, 0.02, 0.02."""
    txs = [{"id": 1, "kind": "expense", "amount": 0.05, "description": "Probe 0.05", "date": "2026-09-01", "category": "Outros"}]
    # Stored splits with exact cents from largest remainder
    splits = {
        1: [
            {"category": "CatA", "percent": 33.34, "amount": 0.02},
            {"category": "CatB", "percent": 33.33, "amount": 0.02},
            {"category": "CatC", "percent": 33.33, "amount": 0.01},
        ]
    }
    repo = _FakeRepo(txs, splits)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.set_privacy(False)

    val_item = page.table.item(0, 4)
    lines = val_item.text().split("\n")
    assert len(lines) == 3
    assert "0,02" in lines[0]
    assert "0,02" in lines[1]
    assert "0,01" in lines[2]

    # Stored role data must match exact cents
    assert val_item.data(Qt.UserRole) == [0.02, 0.02, 0.01]


def test_c24_canonical_allocator_exact_cent_reconciliation_fixtures():
    """Verify largest-remainder exact cent reconciliation across boundary fixtures."""
    repo = Repo(MagicMock())

    # 1. €0.01 at 50/50
    alloc_001 = repo._allocate_split_amounts(0.01, [("A", 50.0), ("B", 50.0)])
    amounts_001 = [item[2] for item in alloc_001]
    assert amounts_001 == [0.01, 0.0]
    assert round(sum(amounts_001), 2) == 0.01

    # 2. €0.05 at 33.34 / 33.33 / 33.33
    alloc_005 = repo._allocate_split_amounts(
        0.05, [("A", 33.34), ("B", 33.33), ("C", 33.33)]
    )
    amounts_005 = [item[2] for item in alloc_005]
    assert amounts_005 == [0.02, 0.02, 0.01]
    assert round(sum(amounts_005), 2) == 0.05

    # 3. €99.99 at 33.3 / 33.3 / 33.4
    alloc_9999 = repo._allocate_split_amounts(
        99.99, [("A", 33.3), ("B", 33.3), ("C", 33.4)]
    )
    amounts_9999 = [item[2] for item in alloc_9999]
    assert amounts_9999 == [33.30, 33.30, 33.39]
    assert round(sum(amounts_9999), 2) == 99.99

    # 4. €100.00 at 50 / 50
    alloc_100 = repo._allocate_split_amounts(100.00, [("A", 50.0), ("B", 50.0)])
    amounts_100 = [item[2] for item in alloc_100]
    assert amounts_100 == [50.0, 50.0]
    assert round(sum(amounts_100), 2) == 100.00


def test_c24_reopening_and_resaving_unchanged_split_does_not_redistribute_cents():
    """Determinism test: re-allocating an unchanged split yields identical exact cent distributions."""
    repo = Repo(MagicMock())

    specs = [
        (0.05, [("A", 33.34), ("B", 33.33), ("C", 33.33)]),
        (99.99, [("A", 33.3), ("B", 33.3), ("C", 33.4)]),
        (0.01, [("A", 50.0), ("B", 50.0)]),
        (123.45, [("A", 15.0), ("B", 35.0), ("C", 50.0)]),
    ]

    for amt, splits in specs:
        alloc1 = repo._allocate_split_amounts(amt, splits)
        alloc2 = repo._allocate_split_amounts(amt, splits)
        assert alloc1 == alloc2
        assert round(sum(item[2] for item in alloc1), 2) == round(amt, 2)


def test_c24_allocate_split_amounts_public_alias():
    """Repo exposes allocate_split_amounts as public alias for _allocate_split_amounts."""
    repo = Repo(MagicMock())
    assert hasattr(repo, "allocate_split_amounts")
    assert repo.allocate_split_amounts == repo._allocate_split_amounts


def test_c21_recovery_raises_storage_error_on_mkdir_failure(tmp_path: Path):
    """Failure to create archive directory with mkdir must raise SecurityRecoveryStorageError."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    envelope = config_dir / "security_envelope.json"
    envelope.write_text('{"format_version": 2}', encoding="utf-8")

    service = StartupSecurityService(
        database_file=tmp_path / "db.sqlite3", configuration_dir=config_dir
    )

    with patch.object(Path, "mkdir", side_effect=OSError("Read-only filesystem")):
        with pytest.raises(
            SecurityRecoveryStorageError, match="Falha de armazenamento"
        ):
            service.acknowledge_tamper_recovery(TAMPER_RECOVERY_CONFIRMATION)


def test_c22_splits_page_with_real_sqlite_rows_no_attribute_error(qapp):
    """SplitsPage must safely access sqlite3.Row fields without crashing on s.get()."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE transactions (id INTEGER PRIMARY KEY, kind TEXT, amount REAL, description TEXT, date TEXT, category TEXT)"
    )
    conn.execute(
        "CREATE TABLE transaction_splits (id INTEGER PRIMARY KEY, transaction_id INTEGER, category TEXT, category_id TEXT, percent REAL, amount REAL)"
    )
    conn.execute(
        "INSERT INTO transactions VALUES (1, 'expense', 100.0, 'Mercado Real', '2026-09-01', 'Alimentação')"
    )
    conn.execute(
        "INSERT INTO transaction_splits VALUES (1, 1, 'Alimentação', 'c1', 60.0, 60.0)"
    )
    conn.execute(
        "INSERT INTO transaction_splits VALUES (2, 1, 'Casa', 'c2', 40.0, 40.0)"
    )
    conn.commit()

    class _RealRowRepo:
        def __init__(self, connection: sqlite3.Connection):
            self.conn = connection

        def setting(self, key: str, default: str = "") -> str:
            return default

        def categories(self, kind: str = "expense") -> list[str]:
            return ["Alimentação", "Casa", "Outros"]

        def transactions_filtered(
            self, kind: str = "Despesas", limit=None
        ) -> list[sqlite3.Row]:
            return self.conn.execute("SELECT * FROM transactions").fetchall()

        def transaction_by_id(self, tx_id: int) -> sqlite3.Row | None:
            return self.conn.execute(
                "SELECT * FROM transactions WHERE id=?", (tx_id,)
            ).fetchone()

        def transaction_splits(self, tx_id: int) -> list[sqlite3.Row]:
            return self.conn.execute(
                "SELECT * FROM transaction_splits WHERE transaction_id=? ORDER BY id",
                (tx_id,),
            ).fetchall()

        def transactions_with_splits(self) -> list[sqlite3.Row]:
            return self.conn.execute("SELECT * FROM transactions").fetchall()

        def _allocate_split_amounts(
            self, amount: float, normalized: list[tuple[str, float]]
        ) -> list[tuple[str, float, float]]:
            return Repo(MagicMock())._allocate_split_amounts(amount, normalized)

    repo = _RealRowRepo(conn)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.refresh()

    # Selecting the row must not raise AttributeError: 'sqlite3.Row' object has no attribute 'get'
    page.tx.setCurrentIndex(1)
    assert page._split_rows[0]["value"].value() == 60.0
    assert page._split_rows[1]["value"].value() == 40.0
    assert "0,00" in page.total_diff_label.text()

    # Table must also format real sqlite3.Row rows cleanly
    assert page.table.rowCount() == 1
    assert "60,00" in page.table.item(0, 4).text()


def test_c22_reload_transactions_preserves_active_selection_and_editor_state(qapp):
    """Reloading transactions preserves the selected transaction and avoids orphan negative diffs."""
    txs = [
        {
            "id": 10,
            "kind": "expense",
            "amount": 80.0,
            "description": "Conta Luz",
            "date": "2026-09-01",
            "category": "Casa",
        },
        {
            "id": 20,
            "kind": "expense",
            "amount": 40.0,
            "description": "Farmácia",
            "date": "2026-09-02",
            "category": "Saúde",
        },
    ]
    splits = {
        10: [
            {"category": "Casa", "percent": 50.0, "amount": 40.0},
            {"category": "Outros", "percent": 50.0, "amount": 40.0},
        ]
    }
    repo = _FakeRepo(txs, splits)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.refresh()

    # Select transaction 10 (index 1)
    page.tx.setCurrentIndex(1)
    assert page.current_transaction_amount() == 80.0
    assert page._split_rows[0]["value"].value() == 40.0
    assert "0,00" in page.total_diff_label.text()

    # Toggle privacy mode -> triggers set_privacy -> refresh -> reload_transactions
    page.set_privacy(True)

    # Selection must still be transaction 10, NOT reset to 0 with orphan negative diff
    assert page.tx.currentIndex() == 1
    assert page.current_transaction_amount() == 80.0
    assert page._split_rows[0]["value"].value() == 40.0
    assert "0,00" in page.total_diff_label.text()


def test_c24_table_displays_two_decimal_percentages(qapp):
    """Existing splits table displays up to 2 decimal places in % column (e.g. 33.34%, 33.33%)."""
    txs = [
        {
            "id": 1,
            "kind": "expense",
            "amount": 100.0,
            "description": "Preciso",
            "date": "2026-09-01",
            "category": "Outros",
        }
    ]
    splits = {
        1: [
            {"category": "CatA", "percent": 33.34, "amount": 33.34},
            {"category": "CatB", "percent": 33.33, "amount": 33.33},
            {"category": "CatC", "percent": 33.33, "amount": 33.33},
        ]
    }
    repo = _FakeRepo(txs, splits)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.refresh()

    pct_item = page.table.item(0, 3)
    lines = pct_item.text().split("\n")
    assert len(lines) == 3
    assert lines[0] == "33.34%"
    assert lines[1] == "33.33%"
    assert lines[2] == "33.33%"


def test_c22_on_transaction_changed_partial_split_unassigned_fallback(qapp):
    """When a split lacks stored amounts and sum of percentages < 100%, allocate only its partial share."""
    txs = [
        {
            "id": 5,
            "kind": "expense",
            "amount": 100.0,
            "description": "Parcial",
            "date": "2026-09-01",
            "category": "Casa",
        }
    ]
    # Stored splits without 'amount' field, only 40%
    splits = {
        5: [
            {"category": "Casa", "percent": 40.0, "amount": None},
        ]
    }
    repo = _FakeRepo(txs, splits)
    app = _FakeApp()

    page = SplitsPage(app)
    page.repo = repo
    page.reload_transactions()

    page.tx.setCurrentIndex(1)
    # 40% of 100€ must be 40.00€, NOT 100.00€
    assert page._split_rows[0]["value"].value() == 40.0
    assert "60,00" in page.total_diff_label.text()
