"""I2: reimbursement evidence is independent of review status and cash creation."""

from __future__ import annotations

import pytest
import sqlite3
from datetime import date
from decimal import Decimal

from services.reimbursements import ReimbursementsService

from database.engine import FinanceDB
from database.repo import Repo


@pytest.fixture
def repo(tmp_path, empty_profile):
    instance = Repo(FinanceDB(tmp_path / "reimbursements.sqlite3"))
    yield instance
    instance.db.conn.close()


def test_reimbursement_workflow_has_a_distinct_public_contract(repo):
    assert callable(getattr(repo, "create_reimbursement_claim", None))


def test_reimbursement_evidence_has_its_own_persistence(repo):
    tables = {
        row[0] for row in repo.q("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"reimbursement_claims", "reimbursement_receipts"} <= tables


def movement(repo, amount=-100, description="Synthetic expense"):
    category = repo.category_rows("expense")[0]["name"]
    return repo.import_transactions_batch(
        [
            {
                "date": "2026-01-02",
                "description": description,
                "amount": amount,
                "category": category,
                "account": "Synthetic account",
            }
        ]
    )[0]


def test_claim_partial_complete_and_unlink_do_not_manufacture_income(repo):
    source = movement(repo)
    income = movement(repo, 60, "Actual receipt")
    other_income = movement(repo, 40, "Second actual receipt")
    service = ReimbursementsService(repo)
    before_count = repo.movement_count()
    before_cash = repo.cash_balance("2026-01-31")
    claim = service.create(
        source, 100, counterpart="Synthetic employer", due_date="2026-02-01"
    )
    assert repo.reimbursement_claim(claim)["state"] == "expected"
    first = service.link(claim, income, 60)
    assert repo.reimbursement_claim(claim)["state"] == "partial"
    assert repo.reimbursement_claim(claim)["outstanding_cents"] == 4000
    second = service.link(claim, other_income, 40)
    assert repo.reimbursement_claim(claim)["state"] == "received"
    assert service.pending() == []
    service.unlink(second)
    assert repo.reimbursement_claim(claim)["state"] == "partial"
    service.unlink(first)
    assert repo.reimbursement_claim(claim)["state"] == "expected"
    assert repo.movement_count() == before_count
    assert repo.cash_balance("2026-01-31") == before_cash


@pytest.mark.parametrize(
    "amount", [0, -1, float("nan"), float("inf"), Decimal("1.001"), True]
)
def test_expected_amount_rejects_invalid_or_fractional_eur_without_rounding(
    repo, amount
):
    source = movement(repo)
    with pytest.raises(ValueError):
        repo.create_reimbursement_claim(source, amount)
    assert repo.reimbursement_claims() == []


def test_claim_and_receipt_caps_cover_multiple_claims_and_raw_sql(repo):
    first_source, second_source = movement(repo), movement(repo, -70)
    receipt = movement(repo, 100)
    first = repo.create_reimbursement_claim(first_source, 80)
    second = repo.create_reimbursement_claim(second_source, 70)
    repo.link_reimbursement_receipt(first, receipt, 80)
    with pytest.raises(ValueError, match="atrib"):
        repo.link_reimbursement_receipt(second, receipt, 21)
    repo.link_reimbursement_receipt(second, receipt, 20)
    with pytest.raises(sqlite3.IntegrityError):
        repo.db.conn.execute(
            "INSERT INTO reimbursement_receipts(claim_id,receipt_transaction_id,amount_cents) VALUES(?,?,?)",
            (second, receipt, 1),
        )
    repo.db.conn.rollback()
    assert repo.reimbursement_claim(second)["received_cents"] == 2000
    with pytest.raises(ValueError):
        repo.create_reimbursement_claim(movement(repo, -10), 10.01)


def test_general_review_flag_is_independent_and_legacy_reimbursable_does_not_infer_claim(
    repo,
):
    source = movement(repo)
    repo.set_transaction_meta_ext(source, status="Revisto", is_reimbursable=1)
    assert repo.reimbursement_claims() == []
    claim = repo.create_reimbursement_claim(source, 50)
    assert repo.reimbursement_claim(claim)["state"] == "expected"
    meta = repo.transaction_meta_ext(source)
    assert meta["status"] == "Revisto" and meta["is_reimbursable"] == 1


def test_sources_and_receipts_must_be_external_cash_with_supported_precision(repo):
    positive = movement(repo, 100)
    negative = movement(repo)
    with pytest.raises(ValueError):
        repo.create_reimbursement_claim(positive, 10)
    claim = repo.create_reimbursement_claim(negative, 10)
    with pytest.raises(ValueError):
        repo.link_reimbursement_receipt(claim, negative, 10)
    transfer = repo.add_internal_transfer(
        "2026-01-03", 20, "Synthetic account", "Other account"
    )
    for identity in transfer:
        with pytest.raises(ValueError):
            repo.link_reimbursement_receipt(claim, identity, 1)
    # Legacy fractional evidence is preserved and refused, never rounded for the wallet.
    legacy = movement(repo, -10)
    repo.db.conn.execute("UPDATE transactions SET amount=-10.001 WHERE id=?", (legacy,))
    repo.db.conn.commit()
    with pytest.raises(ValueError, match="casas"):
        repo.create_reimbursement_claim(legacy, 5)
    assert repo.transaction_by_id(legacy)["amount"] == -10.001


def test_identity_mismatch_is_unavailable_instead_of_silently_relinked(repo):
    source = movement(repo)
    trigger = repo.q(
        "SELECT sql FROM sqlite_master WHERE name='trg_transactions_identity_update'"
    )[0][0]
    repo.db.conn.execute("DROP TRIGGER trg_transactions_identity_update")
    repo.db.conn.execute(
        "UPDATE transactions SET account='Stale identity name' WHERE id=?", (source,)
    )
    repo.db.conn.execute(trigger)
    repo.db.conn.commit()
    with pytest.raises(ValueError, match="identidade"):
        repo.create_reimbursement_claim(source, 10)


def test_future_financial_date_does_not_settle_claim_early(repo):
    source = movement(repo)
    receipt = movement(repo, 100)
    repo.set_transaction_meta_ext(receipt, payment_date="2099-01-04")
    claim = repo.create_reimbursement_claim(source, 100)
    repo.link_reimbursement_receipt(claim, receipt, 100)
    before = repo.reimbursement_claim(claim, as_of="2099-01-03")
    assert before["state"] == "expected" and before["allocated_cents"] == 10000
    assert repo.reimbursement_claim(claim, as_of="2099-01-04")["state"] == "received"


@pytest.mark.parametrize("operation", ["create", "link", "unlink", "delete"])
def test_caller_transaction_owns_every_metadata_operation(repo, operation):
    source = movement(repo)
    receipt = movement(repo, 50)
    claim = repo.create_reimbursement_claim(source, 50)
    link = repo.link_reimbursement_receipt(claim, receipt, 50)
    if operation == "delete":
        repo.unlink_reimbursement_receipt(link)
    repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('caller-marker','pending')"
    )
    before = repo.reimbursement_claims()
    if operation == "create":
        # The additional financial source is created under the caller-owned transaction.
        extra = repo.add_transaction(
            "2026-01-02",
            "Second source",
            -10,
            "Diversos",
            "Synthetic account",
            commit=False,
        )
        repo.create_reimbursement_claim(extra, 10)
    elif operation == "link":
        repo.unlink_reimbursement_receipt(link)
        repo.link_reimbursement_receipt(claim, receipt, 20)
    elif operation == "unlink":
        repo.unlink_reimbursement_receipt(link)
    else:
        repo.delete_reimbursement_claim(claim)
    assert repo.db.conn.in_transaction
    repo.db.conn.rollback()
    assert repo.reimbursement_claims() == before
    assert repo.setting("caller-marker", "") == ""


def test_failure_rolls_back_only_its_savepoint_and_pending_discovery_is_readonly(repo):
    source, receipt = movement(repo), movement(repo, 10)
    claim = repo.create_reimbursement_claim(source, 20)
    repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('caller-evidence','pending')"
    )
    with pytest.raises(ValueError):
        repo.link_reimbursement_receipt(claim, receipt, 11)
    assert repo.db.conn.in_transaction
    assert repo.setting("caller-evidence") == "pending"
    before_changes = repo.db.conn.total_changes
    before_pending = repo.db.conn.in_transaction
    result = ReimbursementsService(repo).pending(date(2026, 2, 2))
    assert result[0]["origin"] == "reimbursements"
    assert repo.db.conn.total_changes == before_changes
    assert repo.db.conn.in_transaction == before_pending
    repo.db.conn.rollback()


def test_linked_evidence_blocks_deletion_and_financial_invalidation(repo):
    source, receipt = movement(repo), movement(repo, 100)
    claim = repo.create_reimbursement_claim(source, 100)
    link = repo.link_reimbursement_receipt(claim, receipt, 100)
    for identity in (source, receipt):
        with pytest.raises(sqlite3.IntegrityError):
            repo.db.conn.execute("DELETE FROM transactions WHERE id=?", (identity,))
        repo.db.conn.rollback()
    for identity, amount in ((source, -90), (receipt, 90), (receipt, -100)):
        with pytest.raises(sqlite3.IntegrityError):
            repo.db.conn.execute(
                "UPDATE transactions SET amount=? WHERE id=?", (amount, identity)
            )
        repo.db.conn.rollback()
    with pytest.raises(ValueError):
        repo.delete_reimbursement_claim(claim)
    repo.unlink_reimbursement_receipt(link)
    repo.delete_reimbursement_claim(claim)
    assert repo.transaction_by_id(source) is not None
    assert repo.transaction_by_id(receipt) is not None


def test_source_identity_is_immutable_but_normal_account_rename_is_preserved(repo):
    from services.account_identity import account_identity_key

    source, other = movement(repo), movement(repo)
    claim = repo.create_reimbursement_claim(source, 20)
    with pytest.raises(sqlite3.IntegrityError):
        repo.db.conn.execute(
            "UPDATE reimbursement_claims SET source_transaction_id=? WHERE id=?",
            (other, claim),
        )
    repo.db.conn.rollback()
    source_row = repo.transaction_by_id(source)
    repo.db.conn.execute(
        "UPDATE financial_accounts SET name=?,account_key=? WHERE stable_id=?",
        (
            "Renamed account",
            account_identity_key("Renamed account"),
            source_row["account_id"],
        ),
    )
    repo.db.conn.commit()
    assert repo.reimbursement_claim(claim)["source_transaction_id"] == source
    assert repo.reimbursement_claim(claim)["source_account"] == "Renamed account"
    assert repo.reimbursement_source(source)["account"] == "Renamed account"


def test_standalone_no_commit_creation_can_be_rolled_back(repo):
    source = movement(repo)
    repo.create_reimbursement_claim(source, 10, commit=False)
    assert repo.db.conn.in_transaction
    repo.db.conn.rollback()
    assert repo.reimbursement_claims() == []


def test_candidates_have_literal_search_and_explicit_id_lookup(repo):
    literal = movement(repo, 10, "Refund 100%_literal")
    movement(repo, 20, "Refund ordinary")
    service = ReimbursementsService(repo)
    assert [r["id"] for r in service.receipt_candidates("%_literal")] == [literal]
    assert [r["id"] for r in service.receipt_candidates("#" + str(literal))] == [
        literal
    ]


def test_delete_source_with_attachment_fails_without_losing_document(repo, tmp_path):
    from services.change_journal import ChangeJournalService

    source = movement(repo)
    repo.create_reimbursement_claim(source, 10)
    attachments = tmp_path / "attachments"
    file = attachments / str(source) / "receipt.txt"
    file.parent.mkdir(parents=True)
    file.write_text("Synthetic document", encoding="utf-8")
    repo.db.conn.execute(
        "INSERT INTO transaction_attachments(transaction_id,file_name,stored_name,stored_path) VALUES(?,?,?,?)",
        (source, "receipt.txt", "receipt.txt", f"{source}/receipt.txt"),
    )
    repo.db.conn.commit()
    journal = ChangeJournalService(
        repo, attachments_directory=attachments, journal_storage=tmp_path / "history"
    )
    with pytest.raises(sqlite3.IntegrityError):
        journal.delete_transactions([source], label="Delete linked expense")
    assert file.read_text(encoding="utf-8") == "Synthetic document"
    assert repo.transaction_by_id(source) is not None
    assert repo.reimbursement_claims()[0]["document_count"] == 1


def test_raw_allocation_insert_and_update_cannot_reuse_cash_across_claims(repo):
    claims = [repo.create_reimbursement_claim(movement(repo), 100) for _ in range(3)]
    receipt = movement(repo, 100)
    first = repo.link_reimbursement_receipt(claims[0], receipt, 60)
    repo.link_reimbursement_receipt(claims[1], receipt, 40)
    for sql, params in [
        (
            "INSERT INTO reimbursement_receipts(claim_id,receipt_transaction_id,amount_cents) VALUES(?,?,?)",
            (claims[2], receipt, 1),
        ),
        ("UPDATE reimbursement_receipts SET amount_cents=? WHERE id=?", (6001, first)),
    ]:
        with pytest.raises(sqlite3.IntegrityError, match="atribuído"):
            repo.db.conn.execute(sql, params)
        repo.db.conn.rollback()
    assert repo.reimbursement_claim(claims[0])["received_cents"] == 6000
    assert repo.reimbursement_claim(claims[1])["received_cents"] == 4000
    assert repo.reimbursement_claim(claims[2])["received_cents"] == 0


def test_raw_claim_and_allocation_updates_preserve_integer_cents_and_caps(repo):
    source, receipt = movement(repo), movement(repo, 60)
    claim = repo.create_reimbursement_claim(source, 100)
    allocation = repo.link_reimbursement_receipt(claim, receipt, 60)
    for sql, params in [
        ("UPDATE reimbursement_claims SET expected_cents=5999 WHERE id=?", (claim,)),
        ("UPDATE reimbursement_claims SET expected_cents=10001 WHERE id=?", (claim,)),
        (
            "UPDATE reimbursement_receipts SET amount_cents=1.5 WHERE id=?",
            (allocation,),
        ),
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            repo.db.conn.execute(sql, params)
        repo.db.conn.rollback()
    assert repo.reimbursement_claim(claim)["expected_cents"] == 10000
    assert repo.reimbursement_claim(claim)["received_cents"] == 6000


def test_reopen_preserves_wallet_links_cash_and_general_review_evidence(repo):
    source, receipt = movement(repo), movement(repo, 40)
    repo.set_transaction_meta_ext(source, status="Revisto", is_reimbursable=1)
    claim = repo.create_reimbursement_claim(
        source, 75, label="Persistent evidence", due_date="2026-12-01"
    )
    repo.link_reimbursement_receipt(claim, receipt, 40)
    before = repo.reimbursement_claims()
    ledger = [tuple(row) for row in repo.q("SELECT * FROM transactions ORDER BY id")]
    path = repo.db.path
    repo.db.conn.close()
    reopened = Repo(FinanceDB(path))
    try:
        assert reopened.reimbursement_claims() == before
        assert [
            tuple(row) for row in reopened.q("SELECT * FROM transactions ORDER BY id")
        ] == ledger
        assert reopened.transaction_meta_ext(source)["status"] == "Revisto"
        assert (
            reopened.reimbursement_receipts(claim)[0]["receipt_transaction_id"]
            == receipt
        )
        assert reopened.cash_balance("2026-12-31") == -60
    finally:
        reopened.db.conn.close()


def test_complete_backup_restore_preserves_partial_wallet_without_new_income(
    repo, tmp_path
):
    from services.backup import BackupService
    from services.startup_security import clear_active_session

    clear_active_session()
    source, receipt = movement(repo), movement(repo, 30)
    claim = repo.create_reimbursement_claim(source, 60)
    repo.link_reimbursement_receipt(claim, receipt, 30)
    before = repo.reimbursement_claims()
    archive = BackupService(repo, database_file=repo.db.path).export_backup_zip(
        tmp_path / "wallet.zip"
    )
    target = Repo(FinanceDB(tmp_path / "target" / "profile.sqlite3"))
    restored = None
    try:
        BackupService(target, database_file=target.db.path).restore_backup(archive)
        restored = Repo(FinanceDB(target.db.path))
        assert restored.reimbursement_claims() == before
        assert restored.movement_count() == 2
        assert restored.reimbursement_claim(claim)["state"] == "partial"
        assert restored.cash_balance("2026-12-31") == -70
    finally:
        if restored is not None:
            restored.db.conn.close()
        target.db.conn.close()
        clear_active_session()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def dispose(widget):
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid

    if isValid(widget):
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)


@pytest.fixture
def wallet(qapp, repo):
    from PySide6.QtWidgets import QWidget
    from ui.core.theme import Theme
    from ui.reimbursements import ReimbursementsDialog

    source = movement(repo, -50, "Private claim <b>literal</b>")
    owner = QWidget()
    owner.theme = Theme()
    dialog = ReimbursementsDialog(
        ReimbursementsService(repo), owner, source_transaction_id=source
    )
    yield dialog, owner, source
    dispose(dialog)
    dispose(owner)
    qapp.processEvents()


def test_keyboard_claim_entry_is_explicit_and_reaches_confirmation(wallet, qapp, repo):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog, QDialogButtonBox
    from ui.reimbursements import ReimbursementClaimDialog

    parent, _, source = wallet
    before = repo.movement_count()
    dialog = ReimbursementClaimDialog(parent.service, source, parent)
    try:
        dialog.show()
        qapp.processEvents()
        dialog.label.setFocus()
        dialog.label.selectAll()
        QTest.keyClicks(dialog.label, "Synthetic employer refund")
        save = dialog.buttons.button(QDialogButtonBox.Save)
        for _ in range(40):
            if dialog.focusWidget() is save:
                break
            QTest.keyClick(dialog.focusWidget(), Qt.Key_Tab)
        assert dialog.focusWidget() is save
        QTest.keyClick(save, Qt.Key_Space)
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert repo.reimbursement_claim(dialog.created_id)["state"] == "expected"
        assert repo.movement_count() == before
    finally:
        dispose(dialog)


def test_receipt_confirmation_links_existing_movement_without_duplicate_cash(
    wallet, qapp, repo
):
    from PySide6.QtWidgets import QDialog
    from ui.reimbursements import ReimbursementReceiptDialog

    parent, _, source = wallet
    receipt = movement(repo, 20, "Actual employer receipt")
    claim = parent.service.create(source, 50)
    count = repo.movement_count()
    dialog = ReimbursementReceiptDialog(parent.service, claim, parent)
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.receipt.currentData()[0] == receipt
        dialog.amount.setValue(20)
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert repo.reimbursement_claim(claim)["state"] == "partial"
        assert repo.movement_count() == count
    finally:
        dispose(dialog)


def test_wallet_private_mode_masks_fields_and_rejects_writes(wallet, qapp, repo):
    dialog, owner, source = wallet
    dialog.service.create(source, 50)
    owner.theme.privacy = True
    dialog.refresh()
    assert not dialog.new_button.isEnabled()
    assert not dialog.link_button.isEnabled()
    for row in range(dialog.claims.rowCount()):
        for column in range(dialog.claims.columnCount()):
            item = dialog.claims.item(row, column)
            assert item.text() == "•••••" and not item.toolTip()
    before = repo.db.conn.total_changes
    dialog.new_claim()
    dialog.link_receipt()
    dialog.unlink_receipt()
    dialog.delete_claim()
    assert repo.db.conn.total_changes == before


def test_live_privacy_change_closes_detail_editor_and_masks_wallet(wallet, qapp, repo):
    from PySide6.QtTest import QTest
    from ui.reimbursements import ReimbursementReceiptDialog

    dialog, owner, source = wallet
    movement(repo, 20, "Private existing receipt")
    claim = dialog.service.create(source, 50)
    dialog.refresh()
    child = ReimbursementReceiptDialog(dialog.service, claim, dialog)
    try:
        dialog.show()
        child.show()
        qapp.processEvents()
        owner.theme.privacy = True
        QTest.qWait(150)
        assert not child.isVisible()
        assert child.receipt.count() == 0 and child.amount.value() == 0
        assert child.search.text() == ""
        assert dialog.claims.item(0, 0).text() == "•••••"
        assert not dialog.link_button.isEnabled()
    finally:
        dispose(child)


def test_wallet_discovery_and_selection_do_not_write_or_commit(wallet, qapp, repo):
    dialog, _, source = wallet
    claim = dialog.service.create(source, 50)
    repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('ui-caller','pending')"
    )
    before = repo.db.conn.total_changes
    dialog.refresh()
    dialog.claims.selectRow(0)
    qapp.processEvents()
    assert dialog.selected_claim() == claim
    assert repo.db.conn.total_changes == before
    assert repo.db.conn.in_transaction
    repo.db.conn.rollback()


def test_page_privacy_callback_masks_replaced_theme_without_timer_delay(qapp, repo):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QPushButton, QWidget
    from ui.core.theme import Theme
    from ui.pages.movements import MovementsPage
    from ui.reimbursements import ReimbursementsDialog

    page = MovementsPage.__new__(MovementsPage)
    QWidget.__init__(page)
    page.app = SimpleNamespace(theme=Theme())
    page.repo = repo
    page._view_state_restored = False
    export = QPushButton("Exportar CSV", page)
    source = movement(repo, -50, "Immediate-private claimant")
    dialog = ReimbursementsDialog(
        ReimbursementsService(repo), page, source_transaction_id=source
    )
    page._reimbursements_dialog = dialog
    try:
        dialog.service.create(source, 50)
        dialog.refresh()
        assert dialog.claims.item(0, 0).text() == "Immediate-private claimant"
        before = repo.db.conn.total_changes
        page.app.theme = Theme(privacy=True)
        page.update_export_privacy_labels()
        # Deliberately no processEvents/qWait: the application callback owns
        # immediate masking, while the timer remains only a fallback.
        assert dialog.claims.item(0, 0).text() == "•••••"
        assert export.text() == "Exportar CSV mascarado"
        assert not dialog.new_button.isEnabled()
        assert repo.db.conn.total_changes == before
    finally:
        page._reimbursements_dialog = None
        dispose(dialog)
        dispose(page)


@pytest.mark.parametrize("action", ["reject", "close"])
def test_wallet_closure_stops_owned_privacy_timer(wallet, action):
    dialog, _, _ = wallet
    assert dialog._privacy_timer.isActive()
    getattr(dialog, action)()
    assert not dialog._privacy_timer.isActive()


@pytest.mark.parametrize("raises", [False, True])
def test_page_wallet_ownership_clears_on_return_and_failure(
    qapp, repo, monkeypatch, raises
):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QWidget
    from ui.core.theme import Theme
    from ui.pages.movements import MovementsPage
    from ui.reimbursements import ReimbursementsDialog

    page = MovementsPage.__new__(MovementsPage)
    QWidget.__init__(page)
    notifications = []
    page.app = SimpleNamespace(
        theme=Theme(), notify_data_changed=lambda: notifications.append(True)
    )
    page.repo = repo
    page._view_state_restored = False
    monkeypatch.setattr(page, "selected_id", lambda: None)
    observed = []

    def exec_wallet(dialog):
        observed.append(dialog)
        assert page._reimbursements_dialog is dialog
        dialog.changed.emit()
        if raises:
            raise RuntimeError("Synthetic modal failure")
        dialog.reject()
        assert not dialog._privacy_timer.isActive()
        return dialog.result()

    monkeypatch.setattr(ReimbursementsDialog, "exec", exec_wallet)
    try:
        if raises:
            with pytest.raises(RuntimeError, match="Synthetic modal failure"):
                page.open_reimbursements()
        else:
            page.open_reimbursements()
        assert len(observed) == 1 and notifications == [True]
        assert page._reimbursements_dialog is None
        assert not observed[0]._privacy_timer.isActive()
        page.update_export_privacy_labels()
    finally:
        for dialog in observed:
            dispose(dialog)
        dispose(page)
