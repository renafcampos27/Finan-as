"""I3: proposals require deliberate decisions and commit the reviewed snapshot."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.imports import ImportService

MAPPING = {"date": "Data", "desc": "Descrição", "amount": "Valor", "account": "Conta"}


@pytest.fixture
def service(tmp_path, empty_profile):
    db = FinanceDB(tmp_path / "review.sqlite3")
    repo = Repo(db)
    repo.add_category_rule("synthetic shop", "Supermercado", [])
    yield ImportService(repo)
    db.conn.close()


def rows(count=1):
    return [
        {
            "Data": "2026-01-15",
            "Descrição": f"Synthetic shop {n}",
            "Valor": "-10,50",
            "Conta": "Synthetic account",
        }
        for n in range(count)
    ]


def test_review_is_read_only_and_explains_local_rule(service):
    before = service.repo.db.conn.total_changes
    review = service.review_batch(rows(), MAPPING)
    assert service.repo.db.conn.total_changes == before
    entry = review.rows[0]
    assert entry.original.category == "Diversos"
    assert entry.proposed.category == "Supermercado"
    assert entry.rule_keyword == "synthetic shop"
    assert "synthetic shop" in entry.reason
    assert review.pending_count == 1


def test_pending_suggestion_cannot_be_persisted(service):
    review = service.review_batch(rows(), MAPPING)
    with pytest.raises(ValueError, match="pendente"):
        service.import_reviewed_batch(review)
    assert service.repo.movement_count() == 0


def test_rejection_retains_movement_and_original_category(service):
    review = service.review_batch(rows(), MAPPING).choose(1, "reject")
    result = service.import_reviewed_batch(review)
    assert result.created == 1
    movement = service.repo.transaction_by_id(result.created_ids[0])
    assert movement["category"] == "Diversos"
    evidence = json.loads(movement["notes"].split(" · ", 1)[1])
    assert evidence["review"]["decision"] == "reject"


def test_acceptance_persists_frozen_category_and_provenance_without_learning(service):
    before_rules = [tuple(row) for row in service.repo.category_rules()]
    review = service.review_batch(rows(), MAPPING, source_sha256="a" * 64).choose(
        1, "accept"
    )
    result = service.import_reviewed_batch(review)
    movement = service.repo.transaction_by_id(result.created_ids[0])
    assert movement["category"] == "Supermercado" and movement["amount"] == -10.5
    evidence = json.loads(movement["notes"].split(" · ", 1)[1])
    assert evidence["original"] == rows()[0]
    assert evidence["source_sha256"] == "a" * 64
    assert evidence["review"]["keyword"] == "synthetic shop"
    assert evidence["review"]["original_category"] == "Diversos"
    assert [tuple(row) for row in service.repo.category_rules()] == before_rules


def test_tail_beyond_main_preview_limit_remains_pending(service):
    review = service.review_batch(rows(121), MAPPING)
    for index in range(1, 81):
        review = review.choose(index, "accept")
    assert len(service.review_preview(review).rows) == 80
    assert review.pending_count == 41
    with pytest.raises(ValueError, match="pendente"):
        service.import_reviewed_batch(review)
    assert service.repo.movement_count() == 0
    result = service.import_reviewed_batch(review.choose_all("reject"))
    assert result.created == 121
    assert {
        row["category"] for row in service.repo.q("SELECT category FROM transactions")
    } == {"Diversos"}


def test_exclusion_is_explicit_and_bulk_choice_does_not_reinclude_it(service):
    review = (
        service.review_batch(rows(3), MAPPING).choose(2, "exclude").choose_all("accept")
    )
    assert review.excluded_count == 1 and review.pending_count == 0
    result = service.import_reviewed_batch(review)
    assert result.created == 2
    assert [
        row["description"]
        for row in service.repo.q("SELECT description FROM transactions ORDER BY id")
    ] == ["Synthetic shop 0", "Synthetic shop 2"]


@pytest.mark.parametrize("change", ["source", "mapping", "sign", "date_format", "hash"])
def test_changed_source_or_normalization_options_refuse_stale_review(service, change):
    original = rows()
    review = service.review_batch(original, MAPPING).choose_all("accept")
    options = {}
    if change == "source":
        original[0]["Valor"] = "-25"
        options["rows"] = original
    elif change == "mapping":
        options["mapping"] = {**MAPPING, "account": "Descrição"}
    elif change == "sign":
        options["invert_signs"] = True
    elif change == "date_format":
        options["date_format"] = "%d/%m/%Y"
    else:
        options["source_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="mudaram"):
        service.import_reviewed_batch(review, **options)
    assert service.repo.movement_count() == 0


def test_changed_rule_cannot_silently_replace_reviewed_value(service):
    review = service.review_batch(rows(), MAPPING).choose_all("accept")
    service.repo.add_category_rule("synthetic shop", "Saúde", [])
    with pytest.raises(ValueError, match="mudaram"):
        service.import_reviewed_batch(review)
    assert service.repo.movement_count() == 0


def test_changed_identity_catalogue_requires_refresh(service):
    service.repo.ensure_financial_account("Known bank")
    review = service.review_batch(rows(), MAPPING).choose_all("reject")
    service.repo.db.conn.execute(
        "UPDATE financial_accounts SET name='Renamed bank' WHERE name='Known bank'"
    )
    service.repo.db.conn.commit()
    with pytest.raises(ValueError, match="mudaram"):
        service.import_reviewed_batch(review)


def test_mutated_snapshot_and_unsupported_decisions_are_refused(service):
    review = service.review_batch(rows(), MAPPING).choose_all("accept")
    forged = replace(
        review.rows[0], proposed=replace(review.rows[0].proposed, amount=-999)
    )
    with pytest.raises(ValueError, match="mudaram"):
        service.import_reviewed_batch(replace(review, rows=(forged,)))
    with pytest.raises(ValueError, match="inválida"):
        service.import_reviewed_batch(
            replace(review, rows=(replace(review.rows[0], decision="learn"),))
        )
    assert service.repo.movement_count() == 0


def test_explicit_refund_category_is_never_overridden_or_duplicated(service):
    data = [
        {**rows()[0], "Valor": "5,25", "Categoria": "Supermercado", "Ref": "refund-001"}
    ]
    mapping = {**MAPPING, "category": "Categoria", "reference": "Ref"}
    review = service.review_batch(data, mapping)
    assert review.pending_count == 0
    result = service.import_reviewed_batch(review)
    movement = service.repo.transaction_by_id(result.created_ids[0])
    assert (movement["amount"], movement["kind"], movement["category"]) == (
        5.25,
        "income",
        "Supermercado",
    )
    fresh = service.review_batch(data, mapping)
    assert service.import_reviewed_batch(fresh).created == 0  # Certain duplicate only.


def test_caller_transaction_is_preserved_without_commit_or_rollback(service):
    review = service.review_batch(rows(), MAPPING).choose_all("accept")
    service.repo.db.conn.execute(
        "INSERT INTO settings(key,value) VALUES('pending-review','marker')"
    )
    with pytest.raises(RuntimeError, match="pendente"):
        service.import_reviewed_batch(review)
    assert service.repo.db.conn.in_transaction
    assert service.repo.setting("pending-review", "") == "marker"
    service.repo.db.conn.rollback()
    assert service.repo.movement_count() == 0


def test_reviewed_import_failure_is_atomic(service):
    review = service.review_batch(rows(2), MAPPING).choose_all("accept")
    service.repo.db.conn.execute("""
        CREATE TRIGGER synthetic_review_failure BEFORE INSERT ON transactions
        WHEN NEW.description='Synthetic shop 1'
        BEGIN SELECT RAISE(ABORT,'synthetic failure'); END
    """)
    service.repo.db.conn.commit()
    with pytest.raises(Exception, match="synthetic failure"):
        service.import_reviewed_batch(review)
    assert service.repo.movement_count() == 0
    assert not service.repo.q("SELECT * FROM transaction_meta")
    assert not service.repo.q(
        "SELECT * FROM financial_accounts WHERE name='Synthetic account'"
    )


def test_unreviewed_ambiguous_dates_remain_blocked(service):
    data = [{**rows()[0], "Data": "010226"}]
    review = service.review_batch(data, MAPPING).choose_all("reject")
    with pytest.raises(ValueError, match="ambíguas"):
        service.import_reviewed_batch(review)
    assert service.repo.movement_count() == 0


def test_default_hint_is_distinguished_from_confirmed_local_rule(service):
    service.repo.db.conn.execute("DELETE FROM category_rules")
    service.repo.db.conn.commit()
    data = [{**rows()[0], "Descrição": "Mercadona synthetic"}]
    review = service.review_batch(data, MAPPING)
    assert review.rows[0].source == "local_hint"
    assert review.rows[0].rule_keyword == "mercadona"
    assert "Sugestão local por termo" in review.rows[0].reason
    mapped = service.review_batch(
        [{**data[0], "Categoria": "Saúde"}], {**MAPPING, "category": "Categoria"}
    )
    assert mapped.pending_count == 0 and mapped.rows[0].proposed.category == "Saúde"


def test_short_rule_terms_retain_word_boundaries(service):
    service.repo.add_category_rule("bp", "Transporte", [])
    review = service.review_batch(
        [{**rows()[0], "Descrição": "ABPORT synthetic"}], MAPPING
    )
    assert not review.rows[0].has_suggestion


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
        QCoreApplication.sendPostedEvents(widget, QEvent.DeferredDelete)


def test_review_dialog_reaches_tail_and_private_cells_hide_values(qapp, service):
    from ui.import_review import ImportReviewDialog
    from ui.core.theme import Theme
    from PySide6.QtWidgets import QWidget

    review = service.review_batch(rows(121), MAPPING)
    owner = QWidget()
    owner.app = SimpleNamespace(theme=Theme(privacy=True))
    dialog = ImportReviewDialog(review, owner, privacy=True)
    try:
        dialog.show()
        dialog.page.setValue(2)
        qapp.processEvents()
        assert dialog.table.rowCount() == 21
        text = " ".join(
            dialog.table.item(row, column).text()
            + dialog.table.item(row, column).toolTip()
            for row in range(21)
            for column in range(7)
        )
        assert (
            "Synthetic" not in text
            and "Supermercado" not in text
            and "10,50" not in text
        )
        choice = dialog.table.cellWidget(20, 7)
        choice.setCurrentIndex(choice.findData("reject"))
        assert not choice.isEnabled() and dialog.review.rows[120].decision == "pending"
        owner.app.theme = Theme(privacy=False)
        dialog.sync_privacy()
        choice.setCurrentIndex(choice.findData("reject"))
        assert dialog.review.rows[120].decision == "reject"
        assert (
            review.rows[120].decision == "pending"
        )  # Cancel cannot mutate caller state.
    finally:
        dispose(dialog)
        dispose(owner)


def test_bulk_decisions_require_explicit_confirmation(qapp, service, monkeypatch):
    from ui import import_review

    review = service.review_batch(rows(121), MAPPING)
    dialog = import_review.ImportReviewDialog(review)
    try:
        monkeypatch.setattr(
            import_review.QMessageBox,
            "question",
            lambda *args: import_review.QMessageBox.No,
        )
        dialog._choose_all("accept")
        assert dialog.review.pending_count == 121
        monkeypatch.setattr(
            import_review.QMessageBox,
            "question",
            lambda *args: import_review.QMessageBox.Yes,
        )
        dialog._choose_all("reject")
        assert dialog.review.pending_count == 0
        assert {entry.decision for entry in dialog.review.rows} == {"reject"}
    finally:
        dispose(dialog)


def test_main_page_blocks_pending_then_commits_exact_rejection(
    qapp, service, monkeypatch
):
    from ui.pages import tools

    app = SimpleNamespace(
        repo=service.repo,
        import_service=service,
        analytics_service=None,
        payments_service=None,
        theme=SimpleNamespace(privacy=False),
        notify_data_changed=lambda: None,
    )
    page = tools.SmartImportPage(app)
    try:
        page.rows = rows()
        for combo, header in (
            (page.map_date, "Data"),
            (page.map_desc, "Descrição"),
            (page.map_amount, "Valor"),
            (page.map_account, "Conta"),
        ):
            combo.blockSignals(True)
            combo.addItem(header)
            combo.setCurrentText(header)
            combo.blockSignals(False)
        page.populate_preview()
        assert page.review_btn.isEnabled() and not page.import_btn.isEnabled()
        page._review_batch = page._review_batch.choose_all("reject")
        page.populate_preview()
        assert page.import_btn.isEnabled()
        assert page.preview.item(0, 3).text() == "Diversos"
        monkeypatch.setattr(tools.QMessageBox, "information", lambda *args: None)
        monkeypatch.setattr(
            tools.QMessageBox, "critical", lambda *args: pytest.fail(str(args[-1]))
        )
        page.import_rows()
        assert (
            service.repo.q("SELECT category FROM transactions")[0]["category"]
            == "Diversos"
        )
    finally:
        dispose(page)


def test_review_tooltips_escape_external_markup(qapp, service):
    from ui.import_review import ImportReviewDialog

    data = [{**rows()[0], "Descrição": "<b>Synthetic shop</b>"}]
    dialog = ImportReviewDialog(service.review_batch(data, MAPPING))
    try:
        assert dialog.table.item(0, 2).text() == "<b>Synthetic shop</b>"
        tooltip = dialog.table.item(0, 2).toolTip()
        assert "&lt;b&gt;" in tooltip and "<b>" not in tooltip
    finally:
        dispose(dialog)


def test_changed_file_on_disk_is_refused_before_any_import(
    qapp, service, tmp_path, monkeypatch
):
    from ui.pages import tools

    app = SimpleNamespace(
        repo=service.repo,
        import_service=service,
        analytics_service=None,
        payments_service=None,
        theme=SimpleNamespace(privacy=False),
        notify_data_changed=lambda: None,
    )
    page = tools.SmartImportPage(app)
    source = tmp_path / "statement.csv"
    source.write_text(
        "Data;Descrição;Valor;Conta\n2026-01-15;Synthetic shop 0;-10,50;Synthetic account\n",
        encoding="utf-8",
    )
    messages = []
    monkeypatch.setattr(
        tools.QFileDialog, "getOpenFileName", lambda *args: (str(source), "")
    )
    monkeypatch.setattr(
        tools.QMessageBox, "critical", lambda *args: messages.append(str(args[-1]))
    )
    try:
        page.open_csv()
        page._review_batch = page._review_batch.choose_all("reject")
        page.populate_preview()
        source.write_text("changed after preview", encoding="utf-8")
        page.import_rows()
        assert len(messages) == 1 and "ficheiro mudou" in messages[0]
        assert service.repo.movement_count() == 0
    finally:
        dispose(page)


def test_reviewed_import_supports_existing_undo_without_creating_rules(
    service, tmp_path
):
    from services.change_journal import ChangeJournalService

    service.change_journal = ChangeJournalService(
        service.repo,
        attachments_directory=tmp_path / "attachments",
        journal_storage=tmp_path / "journal",
    )
    rules_before = [tuple(row) for row in service.repo.category_rules()]
    result = service.import_reviewed_batch(
        service.review_batch(rows(2), MAPPING).choose_all("accept")
    )
    assert result.created == 2
    service.change_journal.undo_latest()
    assert service.repo.movement_count() == 0
    assert not service.repo.q("SELECT * FROM transaction_meta")
    assert [tuple(row) for row in service.repo.category_rules()] == rules_before


def test_catalogue_revalidation_and_write_share_one_owned_write_lock(
    service, monkeypatch
):
    review = service.review_batch(rows(), MAPPING).choose_all("accept")
    original = service._review_catalogue
    blocked = []

    def concurrent_change():
        assert service.repo.db.conn.in_transaction
        with sqlite3.connect(service.repo.db.path, timeout=0.01) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("UPDATE financial_accounts SET name='Concurrent rename'")
            blocked.append(True)
        return original()

    monkeypatch.setattr(service, "_review_catalogue", concurrent_change)
    assert service.import_reviewed_batch(review).created == 1
    assert blocked == [True]
    assert not service.repo.db.conn.in_transaction


def test_journal_failure_rolls_back_reviewed_rows_and_new_identity(
    service, tmp_path, monkeypatch
):
    from services.change_journal import ChangeJournalService

    service.change_journal = ChangeJournalService(
        service.repo,
        attachments_directory=tmp_path / "attachments",
        journal_storage=tmp_path / "journal",
    )
    review = service.review_batch(rows(2), MAPPING).choose_all("accept")

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic journal failure")

    monkeypatch.setattr(service.change_journal, "record_created_transactions", fail)
    with pytest.raises(RuntimeError, match="synthetic journal failure"):
        service.import_reviewed_batch(review)
    assert service.repo.movement_count() == 0
    assert not service.repo.q("SELECT * FROM transaction_meta")
    assert not service.repo.q(
        "SELECT * FROM financial_accounts WHERE name='Synthetic account'"
    )
    assert not service.repo.q("SELECT * FROM change_journal")
    assert not service.repo.db.conn.in_transaction


def test_file_history_tracks_only_rows_actually_created(service, monkeypatch):
    data = [{**rows()[0], "Ref": "stored-001"}]
    mapping = {**MAPPING, "reference": "Ref"}
    service.import_reviewed_batch(
        service.review_batch(data, mapping).choose_all("reject")
    )
    other = {
        **rows()[0],
        "Descrição": "Other purchase",
        "Conta": "Other account",
        "Ref": "new-002",
    }
    review = service.review_batch(
        data + [other], mapping, source_sha256="c" * 64
    ).choose_all("reject")
    recorded = []
    monkeypatch.setattr(
        service,
        "_record_file_history",
        lambda key, accepted: recorded.extend(row.account for row in accepted),
    )
    result = service.import_reviewed_batch(review)
    assert result.created == 1 and result.skipped_duplicates == 1
    assert recorded == ["Other account"]


def test_review_confirmation_stays_inside_short_enlarged_form(qapp, service):
    import re
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QDialogButtonBox, QWidget
    from ui.core.theme import Theme
    from ui.import_review import ImportReviewDialog

    parent = QWidget()
    css = re.sub(
        r"font-size:\s*(\d+(?:\.\d+)?)px",
        lambda match: f"font-size: {float(match.group(1)) * 1.5:g}px",
        Theme(dark=True).stylesheet(),
    )
    parent.setStyleSheet(css)
    dialog = ImportReviewDialog(service.review_batch(rows(121), MAPPING), parent)
    try:
        dialog.resize(640, 360)
        dialog.show()
        qapp.processEvents()
        assert dialog.width() <= 640 and dialog.height() <= 360
        buttons = dialog.findChild(QDialogButtonBox)
        save = buttons.button(QDialogButtonBox.Save)
        assert save.isVisible() and save.isEnabled()
        assert dialog.rect().contains(QRect(save.mapTo(dialog, QPoint()), save.size()))
        save.setFocus()
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        QTest.keyClick(save, Qt.Key_Space)
        assert not dialog.isVisible()
    finally:
        dispose(dialog)
        dispose(parent)


def test_replaced_theme_is_polled_and_blocks_row_and_accept_actions(qapp, service):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QWidget, QDialog
    from ui.core.theme import Theme
    from ui.import_review import ImportReviewDialog

    owner = QWidget()
    original_theme = Theme(dark=False, privacy=False)
    owner.app = SimpleNamespace(theme=original_theme)
    dialog = ImportReviewDialog(service.review_batch(rows(), MAPPING), owner)
    try:
        dialog.show()
        qapp.processEvents()
        assert "Synthetic shop" in dialog.table.item(0, 2).text()
        owner.app.theme = Theme(dark=True, privacy=True)
        QTest.qWait(180)
        assert not original_theme.privacy
        assert dialog.privacy and not dialog.save_button.isEnabled()
        assert "Synthetic shop" not in dialog.table.item(0, 2).text()
        assert dialog.table.item(0, 2).toolTip() == "Dados ocultos"
        assert dialog.styleSheet() == owner.app.theme.stylesheet()
        dialog._choose(1, "reject")
        dialog.accept()
        dialog.done(QDialog.DialogCode.Accepted)
        assert dialog.review.pending_count == 1 and dialog.isVisible()
        owner.app.theme = Theme(privacy=False)
        dialog.sync_privacy()
        dialog._choose(1, "reject")
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert service.repo.movement_count() == 0
    finally:
        dispose(dialog)
        dispose(owner)


def test_bulk_confirmation_rechecks_privacy_after_modal_choice(
    qapp, service, monkeypatch
):
    from PySide6.QtWidgets import QWidget
    from ui.core.theme import Theme
    from ui import import_review

    owner = QWidget()
    owner.app = SimpleNamespace(theme=Theme(privacy=False))
    dialog = import_review.ImportReviewDialog(
        service.review_batch(rows(), MAPPING), owner
    )

    def confirm_then_private(*args):
        owner.app.theme = Theme(privacy=True)
        return import_review.QMessageBox.Yes

    monkeypatch.setattr(import_review.QMessageBox, "question", confirm_then_private)
    try:
        dialog._choose_all("accept")
        assert dialog.review.pending_count == 1
        assert not dialog.accept_all.isEnabled()
        assert dialog.table.item(0, 2).toolTip() == "Dados ocultos"
    finally:
        dispose(dialog)
        dispose(owner)


def test_page_forwards_privacy_immediately_and_refuses_private_commit(
    qapp, service, monkeypatch
):
    from ui.core.theme import Theme
    from ui.pages import tools
    from ui.import_review import ImportReviewDialog

    app = SimpleNamespace(
        repo=service.repo,
        import_service=service,
        analytics_service=None,
        payments_service=None,
        theme=Theme(privacy=False),
        notify_data_changed=lambda: None,
    )
    page = tools.SmartImportPage(app)
    dialog = None
    try:
        page.rows = rows()
        for combo, header in (
            (page.map_date, "Data"),
            (page.map_desc, "Descrição"),
            (page.map_amount, "Valor"),
            (page.map_account, "Conta"),
        ):
            combo.blockSignals(True)
            combo.addItem(header)
            combo.setCurrentText(header)
            combo.blockSignals(False)
        page.populate_preview()
        page._review_batch = page._review_batch.choose_all("reject")
        page.populate_preview()
        assert page.import_btn.isEnabled()
        dialog = ImportReviewDialog(page._review_batch, page)
        page._review_dialog = dialog
        dialog.show()
        qapp.processEvents()
        app.theme = Theme(dark=True, privacy=True)
        monkeypatch.setattr(
            service,
            "review_batch",
            lambda *args, **kwargs: pytest.fail(
                "masking must be immediate without rebuilding"
            ),
        )
        page.update_export_privacy_labels()
        assert dialog.privacy and not dialog.save_button.isEnabled()
        assert "Synthetic" not in page.preview.item(0, 1).text()
        assert "Synthetic" not in page.preview.item(0, 1).toolTip()
        assert not page.import_btn.isEnabled()
        page.import_rows()
        assert service.repo.movement_count() == 0
    finally:
        page._review_dialog = None
        if dialog is not None:
            dispose(dialog)
        dispose(page)
