"""Automated verification suite for Council Audit Round 8 - Batch 41 defects.

Defects covered:
 1. validate_transaction_category allow_refunds & preserve expense kind on refunds.
 2. duplicate_classification prefix matching (>= 15 chars).
 3. ImportService duplicate index normalized account_identity_key & prefix matching.
 4. BackupService SQLite header 16-byte check (b"SQLite format 3\\x00") and min size.
 5. BackupService _durable_replace and _ensure_writable handling Windows read-only files.
 6. BackupService _restore_legacy_backup moves sidecars to .rollback and restores on failure.
 7. DataProtectionGuard _install_staged_restore _ensure_writable for read-only files.
 8. DocumentService _target_for max_allowed 200 chars to avoid MAX_PATH overflow.
 9. DocumentService audit_storage and remove_orphan_files ignore files modified < 120s ago.
10. normalise_attachment_storage flags 0-byte files as altered_ids.
11. ChartFocusDialog accept() and done() call _restore_chart() before super().
12. StableChart clear_pinned_detail() and _apply_pinned_comparison privacy masking ("•••%").
13. account_identity & category_taxonomy invisible chars removal and IBAN space stripping.
14. category_identity existing category check: reactivate inactive or disambiguate different kind.
15. suggest_split word boundary matching for short keywords (<= 3 chars).
16. CIRS Art. 43.º n.º 3: exclude offshore capital losses from global loss netting & math exports.
17. crisis_backtester clamp peak_to_trough_drop <= 0 and _safe_mdd non-positive series handling.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB
from database.protection import DataProtectionGuard
from database.repo import Repo
from motores_matematicos_simulacao.crisis_backtester_and_architecture_engine import (
    CrisisScenario,
    _safe_mdd,
    generate_market_path,
)
from motores_matematicos_simulacao.portfolio_stress_and_tax_optimizer import (
    TAXA_AGRAVADA_OFFSHORE,
    TaxLot,
    cornish_fisher_var,
    expected_shortfall_cvar,
    process_fifo_sale,
)
from services.account_identity import (
    DEFAULT_ACCOUNT_NAME,
    account_identity_key,
    clean_account_name,
)
from services.backup import BackupService
from services.category_taxonomy import category_identity_key
from services.documents import (
    DocumentService,
    normalise_attachment_storage,
)
from services.imports import NormalizedImportRow, ImportService
from ui.components.charts.base import ChartFocusDialog, StableChart


@dataclass
class SimpleTheme:
    dark: bool = False
    privacy: bool = False


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch41_test.sqlite3")
    r = Repo(db)
    yield r
    try:
        db.conn.close()
    except Exception:
        pass


# ==============================================================================
# 1. Defect 1: validate_transaction_category allow_refunds & refund expense kind
# ==============================================================================

def test_defect_01_validate_category_refunds_and_import_batch(repo):
    """Refunds on expense categories should be allowed when allow_refunds=True,

    and import_transactions_batch should preserve expense classification.
    """
    # Supermercado is an expense category
    assert repo.category_kind("Supermercado") == "expense"

    # Positive amount with allow_refunds=True should succeed
    val = repo.validate_transaction_category("Supermercado", 25.50, allow_refunds=True)
    assert val == "Supermercado"

    # Positive amount with allow_refunds=False should fail
    with pytest.raises(ValueError, match="receitas"):
        repo.validate_transaction_category("Supermercado", 25.50, allow_refunds=False)

    # In import_transactions_batch, a refund (positive amount on expense category)
    # should be imported without error and retain its category and kind
    rows = [
        {
            "date": "2026-03-01",
            "description": "Reembolso Compra Continente",
            "amount": 25.50,
            "category": "Supermercado",
            "account": "Conta principal",
        }
    ]
    ids = repo.import_transactions_batch(rows)
    assert len(ids) == 1
    imported = repo.transaction_by_id(ids[0])
    assert imported is not None
    assert imported["category"] == "Supermercado"
    assert imported["amount"] == 25.50
    assert imported["kind"] == "income"
    assert repo.category_kind(imported["category"]) == "expense"


# ==============================================================================
# 2. Defect 2: duplicate_classification prefix matching (>= 15 chars)
# ==============================================================================

def test_defect_02_duplicate_classification_prefix_matching(repo):
    """duplicate_classification should find probable duplicates when description

    prefix matches for lengths >= 15 chars.
    """
    # Insert existing transaction using valid add_transaction
    tx_id = repo.add_transaction(
        d="2026-03-05",
        desc="RESTAURANTE MARISQUEIRA DO PORTO REF 001",
        amount=-45.00,
        category="Restaurantes",
        account="Conta principal",
    )
    assert tx_id > 0

    # New incoming transaction has prefix >= 15 chars matching
    classification = repo.duplicate_classification(
        account="Conta principal",
        tx_date="2026-03-05",
        amount=-45.00,
        description="RESTAURANTE MARISQUEIRA DO PORTO",
    )
    assert classification["level"] == "probable"
    assert tx_id in classification["ids"]


# ==============================================================================
# 3. Defect 3: ImportService duplicate index normalized account key & prefix
# ==============================================================================

def test_defect_03_import_service_duplicate_index_normalized_account_and_prefix(repo, tmp_path):
    """ImportService index uses account_identity_key for robust account matching

    and prefix matching for descriptions >= 15 chars.
    """
    svc = ImportService(repo)
    # Seed a transaction with spacing differences in account
    tx_id = repo.add_transaction(
        d="2026-03-10",
        desc="SERVICOS DE TELECOMUNICACOES NOS FIBRA",
        amount=-35.90,
        category="Casa e contas",
        account="  CONTA   PRINCIPAL  ",
        external_reference="REF-NOS-12345",
    )
    assert tx_id > 0

    # 1. Incoming row with identical external_reference and normalized account
    incoming_row = NormalizedImportRow(
        date="2026-03-10",
        description="SERVICOS DE TELECOMUNICACOES NOS FIBRA",
        amount=-35.90,
        account="Conta Principal",
        category="Casa e contas",
        external_reference="REF-NOS-12345",
    )

    index = svc._build_duplicate_index([incoming_row])
    assert index["active"] is True

    dup_ref = svc._stored_duplicate_evidence_indexed(incoming_row, index)
    assert dup_ref["level"] == "certain"
    assert tx_id in dup_ref["ids"]

    # 2. Incoming row without external reference, matching on prefix (>= 15 chars)
    incoming_loose = NormalizedImportRow(
        date="2026-03-10",
        description="SERVICOS DE TELECOMUNICACOES NOS",
        amount=-35.90,
        account="Conta Principal",
        category="Casa e contas",
        external_reference="",
    )
    dup_loose = svc._stored_duplicate_evidence_indexed(incoming_loose, index)
    assert dup_loose["level"] == "probable"
    assert tx_id in dup_loose["ids"]


# ==============================================================================
# 4. Defect 4: BackupService SQLite header 16-byte check & minimum size
# ==============================================================================

def test_defect_04_backup_validate_restore_database_sqlite_header(tmp_path, repo):
    """validate_restore_database rejects files smaller than 100 bytes or lacking

    the standard SQLite 16-byte magic header.
    """
    # 1. File too small (<100 bytes)
    small_file = tmp_path / "tiny.db"
    small_file.write_bytes(b"short content")
    with pytest.raises(ValueError, match="O backup não contém uma base de dados válida"):
        BackupService.validate_restore_database(small_file)

    # 2. File >= 100 bytes but invalid header
    bad_header = tmp_path / "bad_header.db"
    bad_header.write_bytes(b"NOT A SQLITE FILE" + b"\x00" * 150)
    with pytest.raises(ValueError, match="O ficheiro não é uma base de dados SQLite válida"):
        BackupService.validate_restore_database(bad_header)

    # 3. Valid database passes
    live_db = Path(repo.db.path)
    BackupService.validate_restore_database(live_db)


# ==============================================================================
# 5. Defect 5: BackupService _durable_replace and _ensure_writable on read-only
# ==============================================================================

def test_defect_05_backup_durable_replace_readonly_file(tmp_path):
    """_durable_replace handles targets marked Read-Only on Windows."""
    src = tmp_path / "source.txt"
    dst = tmp_path / "target.txt"

    src.write_text("new content", encoding="utf-8")
    dst.write_text("old content", encoding="utf-8")

    # Mark destination read-only
    os.chmod(dst, stat.S_IREAD)

    BackupService._durable_replace(src, dst)

    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "new content"


# ==============================================================================
# 6. Defect 6: BackupService _restore_legacy_backup sidecars rollback & cleanup
# ==============================================================================

def test_defect_06_restore_legacy_backup_wal_sidecars_rollback_and_success(tmp_path, repo):
    """_restore_legacy_backup backs up WAL/SHM sidecars to .rollback and restores them on failure."""
    db_file = tmp_path / "live.sqlite3"
    wal_file = Path(f"{db_file}-wal")
    shm_file = Path(f"{db_file}-shm")

    backup_svc = BackupService(
        repo=repo,
        backup_directory=tmp_path / "backups",
        preferences_file=tmp_path / "prefs.json",
        database_file=db_file,
    )

    valid_restore_source = tmp_path / "source.sqlite3"
    backup_svc.backup_sqlite(valid_restore_source)
    shutil.copyfile(valid_restore_source, db_file)
    wal_file.write_bytes(b"live wal content")
    shm_file.write_bytes(b"live shm content")

    # 1. Failure simulation during replacement: patch replacement.replace to fail
    original_replace = Path.replace

    failed_once = False

    def mock_replace(self_path, target_path):
        nonlocal failed_once
        if not failed_once and Path(target_path) == db_file and ".restore" in str(self_path):
            failed_once = True
            raise OSError("Simulated replace failure")
        return original_replace(self_path, target_path)

    with patch.object(Path, "replace", mock_replace):
        with pytest.raises(Exception, match="Simulated replace failure"):
            backup_svc._restore_legacy_backup(valid_restore_source)

    # Sidecars should have been restored from .rollback
    assert wal_file.exists(), "WAL sidecar should have been restored on failure"
    assert shm_file.exists(), "SHM sidecar should have been restored on failure"
    assert len(shm_file.read_bytes()) > 0

    # 2. Success case: sidecars and rollback files properly finalized
    fresh_repo = Repo(FinanceDB(valid_restore_source))
    backup_svc_ok = BackupService(
        repo=fresh_repo,
        backup_directory=tmp_path / "backups",
        preferences_file=tmp_path / "prefs.json",
        database_file=db_file,
    )
    backup_svc_ok._restore_legacy_backup(valid_restore_source)
    # Rollback files must be cleaned up
    rollback_files = list(tmp_path.glob("*.rollback"))
    assert len(rollback_files) == 0


# ==============================================================================
# 7. Defect 7: DataProtectionGuard _install_staged_restore _ensure_writable
# ==============================================================================

def test_defect_07_data_protection_install_staged_restore_readonly_files(tmp_path, repo):
    """_install_staged_restore succeeds even if existing database/sidecars are read-only."""
    guard = DataProtectionGuard(
        database_file=tmp_path / "data.sqlite3",
        preferences_file=tmp_path / "prefs.json",
        protection_directory=tmp_path / "protection",
    )

    real_db = Path(repo.db.path)
    live_db = tmp_path / "data.sqlite3"
    shutil.copyfile(real_db, live_db)
    live_wal = Path(f"{live_db}-wal")
    live_wal.write_bytes(b"wal content")

    # Mark files read-only
    os.chmod(live_db, stat.S_IREAD)
    os.chmod(live_wal, stat.S_IREAD)

    staged_db = tmp_path / "staged.sqlite3"
    shutil.copyfile(real_db, staged_db)
    staged_att = tmp_path / "staged_att"
    staged_att.mkdir()
    staged_hist = tmp_path / "staged_hist"
    staged_hist.mkdir()

    # Installation should make them writable and replace them successfully
    guard._install_staged_restore(
        staged_database=staged_db,
        staged_attachments=staged_att,
        staged_history=staged_hist,
        staged_preferences=None,
        preferences_present=False,
    )

    assert live_db.exists()
    assert live_db.stat().st_mode & stat.S_IWRITE


# ==============================================================================
# 8. Defect 8: DocumentService _target_for max_allowed 200 chars
# ==============================================================================

def test_defect_08_document_service_target_for_max_allowed_200(repo, tmp_path):
    """DocumentService._target_for limits safe_name length budgeting for max_allowed=200."""
    doc_svc = DocumentService(repo, attachments_directory=tmp_path / "attachments")
    very_long_name = "A" * 300 + ".pdf"

    target = doc_svc._target_for(12345, Path(very_long_name))
    assert len(str(target.resolve())) < 260
    assert target.suffix == ".pdf"


# ==============================================================================
# 9. Defect 9: DocumentService audit_storage & remove_orphans ignore < 120s files
# ==============================================================================

def test_defect_09_document_service_audit_storage_and_remove_orphans_ignores_recent_files(repo, tmp_path):
    """audit_storage and remove_orphan_files ignore files created/modified in the last 120 seconds."""
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir(parents=True, exist_ok=True)
    doc_svc = DocumentService(repo, attachments_directory=attach_dir)

    # Create a fresh file in attachments (modified right now)
    fresh_file = attach_dir / "1" / "recent_invoice.pdf"
    fresh_file.parent.mkdir(parents=True, exist_ok=True)
    fresh_file.write_bytes(b"%PDF-1.4 recent invoice")

    # Audit storage: fresh unreferenced file should be ignored from orphan list
    audit = doc_svc.audit_storage(force=True)
    assert str(fresh_file) not in audit["orphan_files"]

    # remove_orphan_files should not delete fresh files
    removed = doc_svc.remove_orphan_files()
    assert removed == 0
    assert fresh_file.exists()


# ==============================================================================
# 10. Defect 10: normalise_attachment_storage flags 0-byte files as altered_ids
# ==============================================================================

def test_defect_10_normalise_attachment_storage_flags_zero_byte_files(repo, tmp_path):
    """normalise_attachment_storage flags 0-byte files as altered_ids."""
    conn = repo.db.conn
    attach_dir = tmp_path / "attachments"
    attach_dir.mkdir(parents=True, exist_ok=True)

    # Insert parent transaction first to satisfy FK constraint
    tx_id = repo.add_transaction(
        d="2026-03-01",
        desc="Teste Attachment",
        amount=-10.0,
        category="Geral",
        account="Conta principal",
    )

    # Insert attachment row
    cursor = conn.execute(
        """
        INSERT INTO transaction_attachments(
            transaction_id, stored_name, stored_path, file_name, file_size
        ) VALUES(?, 'doc.pdf', '1/doc.pdf', 'doc.pdf', 0)
        """,
        (tx_id,),
    )
    att_id = cursor.lastrowid
    conn.commit()

    # Create zero-byte file on disk
    target_file = attach_dir / "1" / "doc.pdf"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_bytes(b"")

    res = normalise_attachment_storage(conn, attach_dir, require_all=False)
    assert att_id in res["altered_ids"]


# ==============================================================================
# 11. Defect 11: ChartFocusDialog accept() and done() call _restore_chart()
# ==============================================================================

def test_defect_11_chart_focus_dialog_accept_and_done_restores_chart(qapp):
    """ChartFocusDialog accept() and done() must call _restore_chart() before closing."""
    parent_widget = QWidget()
    chart = StableChart(lambda: SimpleTheme(), parent_widget)
    chart.setAccessibleName("Teste Grafico")

    dialog = ChartFocusDialog(chart)
    assert chart.parentWidget() == dialog

    # Call accept()
    dialog.accept()
    assert chart.parentWidget() == parent_widget
    assert dialog._restored is True

    # Test done()
    dialog2 = ChartFocusDialog(chart)
    assert chart.parentWidget() == dialog2
    dialog2.done(1)
    assert chart.parentWidget() == parent_widget
    assert dialog2._restored is True


# ==============================================================================
# 12. Defect 12: StableChart clear_pinned_detail & privacy percentage masking
# ==============================================================================

def test_defect_12_stable_chart_pinned_comparison_privacy_masking_and_clear(qapp):
    """StableChart clear_pinned_detail clears pinned item, and privacy mode masks delta percentage."""
    theme_state = SimpleTheme(privacy=False)
    chart = StableChart(lambda: theme_state)

    chart.pin_detail_item({"label": "Jan", "amount": 1000.0})
    assert chart._pinned_detail_item is not None

    chart.clear_pinned_detail()
    assert chart._pinned_detail_item is None

    # Test privacy masking in _apply_pinned_comparison
    chart.pin_detail_item({"label": "Base", "amount": 1000.0})
    target_item = {"label": "Atual", "amount": 1500.0}

    # Non-privacy mode
    theme_state.privacy = False
    payload_public = chart._apply_pinned_comparison(target_item, {"subtitle": "", "metrics": []})
    metric_public = next(
        m for m in payload_public["metrics"]
        if "amount" in m["label"].lower() or "montante" in m["label"].lower() or "valor" in m["label"].lower() or "Δ" in m["label"]
    )
    assert "+50,0%" in metric_public["value"]

    # Privacy mode
    theme_state.privacy = True
    payload_private = chart._apply_pinned_comparison(target_item, {"subtitle": "", "metrics": []})
    metric_private = next(m for m in payload_private["metrics"] if "Δ" in m["label"])
    assert "•••%" in metric_private["value"]
    assert "+50,0%" not in metric_private["value"]


# ==============================================================================
# 13. Defect 13: account_identity & category_taxonomy invisible chars & IBAN spaces
# ==============================================================================

def test_defect_13_account_and_category_identity_invisible_chars_and_iban():
    """clean_account_name and identity keys strip invisible chars and normalize IBANs."""
    # Zero-width spaces in account name
    dirty_account = "Conta\u200B principal\uFEFF"
    assert clean_account_name(dirty_account) == DEFAULT_ACCOUNT_NAME
    assert account_identity_key(dirty_account) == account_identity_key("Conta principal")

    # IBAN with spaces and zero-width chars
    iban_with_spaces = " PT50 1234 5678 \u200C9012 3456 7890 1 "
    expected_iban = "PT50123456789012345678901"
    assert clean_account_name(iban_with_spaces) == expected_iban
    assert account_identity_key(iban_with_spaces) == expected_iban.lower()

    # category_identity_key zero-width chars
    dirty_cat = "Alimentação\u200B e\u2060 Mercado"
    clean_key = category_identity_key(dirty_cat)
    assert "\u200B" not in clean_key
    assert "\u2060" not in clean_key
    assert clean_key == category_identity_key("Alimentação e Mercado")


# ==============================================================================
# 14. Defect 14: category_identity reactivation of inactive & disambiguation
# ==============================================================================

def test_defect_14_category_identity_existing_inactive_reactivation_and_disambiguation(repo):
    """category_identity reactivates existing inactive category and disambiguates different kind."""
    conn = repo.db.conn
    # Insert an inactive category
    conn.execute(
        """
        INSERT INTO categories(name, kind, is_active, stable_id, identity_key, colour)
        VALUES('Categoria Inativa', 'expense', 0, 'cat_inact_01', 'categoria inativa', '#94A3B8')
        """
    )
    conn.commit()

    # category_identity with create=True on inactive category should reactivate it
    stable_id, name = repo.category_identity("Categoria Inativa", create=True, kind="expense")
    assert stable_id == "cat_inact_01"
    assert name == "Categoria Inativa"
    row = conn.execute("SELECT is_active FROM categories WHERE stable_id='cat_inact_01'").fetchone()
    assert row["is_active"] == 1

    # Now create category with same name but different kind (income)
    stable_id_inc, name_inc = repo.category_identity("Categoria Inativa", create=True, kind="income")
    assert stable_id_inc != "cat_inact_01"
    assert "Receita" in name_inc
    assert name_inc == "Categoria Inativa (Receita)"


# ==============================================================================
# 15. Defect 15: suggest_split word boundaries for short keywords
# ==============================================================================

def test_defect_15_suggest_split_word_boundaries_short_keywords(repo):
    """suggest_split avoids false positive substring matches for short keywords like 'nos'."""
    db = repo.db
    # "diagnostico" contains "nos" as a substring, but must NOT match "Casa e contas"
    split_diag = db.suggest_split("Exame e Diagnostico Medico", -50.0)
    assert split_diag[0][0] != "Casa e contas"

    # "Fatura NOS" contains "nos" as a distinct word, and SHOULD match "Casa e contas"
    split_nos = db.suggest_split("Fatura NOS Comunicacoes", -50.0)
    assert split_nos[0][0] == "Casa e contas"


# ==============================================================================
# 16. Defect 16: CIRS Art. 43.º n.º 3 offshore capital losses excluded from netting
# ==============================================================================

def test_defect_16_portfolio_stress_cirs_art43_offshore_losses_excluded():
    """CIRS Art. 43.º n.º 3: Capital losses from offshore entities cannot offset capital gains."""
    # Check that quant functions are exported
    assert callable(cornish_fisher_var)
    assert callable(expected_shortfall_cvar)

    # 1. Normal gain (+1000 EUR) and offshore loss (-500 EUR)
    # The offshore loss must NOT reduce the taxable gain to 500 EUR.
    # The normal gain must be taxed on the full 1000 EUR at standard 28%.
    lots = [
        # Normal US stock with gain: acq 1000, sell 2000 -> profit +1000
        TaxLot(id=1, asset_symbol="AAPL", buy_date=date(2025, 1, 1), quantity=10, buy_price=100.0, buy_fees=0.0, is_foreign=True, country_code="US"),
        # Offshore Cayman stock with loss: acq 1000, sell 500 -> profit -500
        TaxLot(id=2, asset_symbol="OFFSHORE_CORP", buy_date=date(2025, 1, 1), quantity=10, buy_price=100.0, buy_fees=0.0, is_foreign=True, country_code="KY"),
    ]

    # Sell both lots
    _, realized_aapl = process_fifo_sale(lots[:1], date(2025, 6, 1), 10, 200.0, 0.0)
    _, realized_offshore = process_fifo_sale(lots[1:], date(2025, 6, 1), 10, 50.0, 0.0)

    all_realized = realized_aapl + realized_offshore
    assert all_realized[1].tax_rate_pct == TAXA_AGRAVADA_OFFSHORE
    assert all_realized[1].taxable_profit == -500.0

    # Netting calculation with combined realized events:
    net_profit = sum(
        ev.taxable_profit
        for ev in all_realized
        if not ev.is_exempt
        and not (ev.tax_rate_pct == TAXA_AGRAVADA_OFFSHORE and ev.taxable_profit < 0)
    )
    assert net_profit == 1000.0

    # 2. Within a single process_fifo_sale call with multiple lots:
    fund_lots = [
        # Lot 1: Offshore Cayman with loss (-500 EUR)
        TaxLot(id=10, asset_symbol="GLOBAL_FUND", buy_date=date(2025, 1, 1), quantity=10, buy_price=100.0, buy_fees=0.0, is_foreign=True, country_code="KY"),
        # Lot 2: US lot with gain (+400 EUR)
        TaxLot(id=11, asset_symbol="GLOBAL_FUND", buy_date=date(2025, 2, 1), quantity=10, buy_price=10.0, buy_fees=0.0, is_foreign=True, country_code="US"),
    ]
    _, realized_fund = process_fifo_sale(fund_lots, date(2025, 6, 1), 20, 50.0, 0.0)
    assert len(realized_fund) == 2
    assert realized_fund[0].taxable_profit == -500.0
    assert realized_fund[0].estimated_tax == 0.0
    assert realized_fund[1].taxable_profit == 400.0
    assert realized_fund[1].estimated_tax == pytest.approx(112.0, abs=0.01)


# ==============================================================================
# 17. Defect 17: crisis_backtester clamped drop & _safe_mdd non-positive series
# ==============================================================================

def test_defect_17_crisis_backtester_clamped_drop_and_safe_mdd_non_positive():
    """generate_market_path clamps positive peak_to_trough_drop, and _safe_mdd handles non-positive values."""
    # Positive drop scenario must be clamped to 0.0 drop (no inverted crash curve)
    positive_drop_scenario = CrisisScenario(
        "Fictitious Boom",
        duration_months=12,
        trough_month=3,
        peak_to_trough_drop=0.50,  # Positive value!
        recovery_months=6,
    )
    prices = generate_market_path(positive_drop_scenario)
    assert prices[3] <= 100.0, "Trough price with clamped drop must not exceed 100.0"

    # _safe_mdd with empty, single, or all non-positive values
    assert _safe_mdd([]) == 0.0
    assert _safe_mdd([100.0]) == 0.0
    assert _safe_mdd([0.0, 0.0]) == 0.0
    assert _safe_mdd([-10.0, -20.0, -5.0]) == 0.0

    # _safe_mdd series starting positive then dropping to zero or negative
    assert _safe_mdd([100.0, 0.0]) == -1.0
    assert _safe_mdd([100.0, -50.0]) == -1.0
    assert _safe_mdd([-10.0, 100.0, 60.0]) == pytest.approx(-0.40, abs=1e-4)
