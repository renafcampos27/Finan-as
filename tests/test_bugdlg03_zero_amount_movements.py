"""BUG-DLG-03 (P1) — caller-side zero-amount guard for TransactionDialog.

MovementsPage forwarded ``data["amount"]`` straight to the repo as soon as
the dialog returned ``QDialog.Accepted``.  A 0.00 amount is rejected by the
repo, but by then the dialog was already closed — silently discarding the
notes, subcategory, tags and attachments the user had typed.

Contract under test (ui/pages/movements.py only; ui/dialogs.py belongs to
another workstream):
* Accepted with 0.00  -> repo NOT called; ``QMessageBox.warning`` shown; the
  SAME dialog object is re-exec'd so the user can fix the amount without
  losing the form (loop exits on a valid amount or on cancel).
* Cancel/Rejected     -> repo NOT called, no warning.
* Valid amount at first exec -> repo called exactly once with the exact form
  payload and journal metadata.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from ui.pages import movements as movements_module
from ui.pages.movements import MovementsPage

ZERO_AMOUNT: float = 0.0
VALID_AMOUNT: float = -12.34


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _dispose_owned_pages(qapp):
    """Delete this test's Qt owners while QApplication is still alive."""
    existing = set(qapp.topLevelWidgets())
    yield
    for widget in qapp.topLevelWidgets():
        if widget not in existing and isinstance(widget, MovementsPage):
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _payload(amount: float) -> dict[str, Any]:
    """Form payload shaped exactly like TransactionDialog.extended_data()."""
    return {
        "date": "2026-09-05",
        "description": "Mercado",
        "amount": amount,
        "category": "Alimentação",
        "account": "Conta principal",
        "notes": "Compra teste",
        "subcategory": "Mercearia",
        "merchant": "Pingo Doce",
        "payment_method": "Cartão de débito",
        "tags": "mercado",
        "purchase_date": "2026-09-04",
        "payment_date": "2026-09-05",
    }


class _RecordingRepo:
    """Repo stub registering add/update calls instead of touching SQLite."""

    def __init__(self) -> None:
        self.add_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.update_calls: list[tuple[Any, ...]] = []

    def add_transaction_with_meta(self, *args: Any, **kwargs: Any) -> int:
        self.add_calls.append((args, kwargs))
        return 999

    def transaction_by_id(self, tx_id: int) -> dict[str, Any]:
        return {"id": tx_id, "category": "Alimentação"}

    def internal_transfer_by_id(self, tx_id: int) -> None:
        return None

    def update_transaction_with_meta(self, *args: Any, **kwargs: Any) -> None:
        self.update_calls.append(args)


class _FakeTransactionDialog:
    """Scripted TransactionDialog double (exec results + extended payloads)."""

    def __init__(
        self,
        results: list[QDialog.DialogCode],
        payloads: list[dict[str, Any]],
    ) -> None:
        self.repo: Any = None
        self.parent: Any = None
        self.row: Any = None
        self._results = list(results)
        self._payloads = list(payloads)
        self.exec_calls = 0

    def exec(self) -> QDialog.DialogCode:
        self.exec_calls += 1
        return self._results.pop(0)

    def extended_data(self) -> dict[str, Any]:
        return self._payloads.pop(0)

    def attachment_paths(self) -> list[Path]:
        return []


def _dialog_factory(
    fake: _FakeTransactionDialog,
) -> Callable[[Any, Any, Any], _FakeTransactionDialog]:
    def _factory(
        repo: Any, parent: Any = None, row: Any = None
    ) -> _FakeTransactionDialog:
        fake.repo = repo
        fake.parent = parent
        fake.row = row
        return fake

    return _factory


def make_mock_app(tmp_path: Any) -> MagicMock:
    """Replicates tests/test_phase6_v349_aud31_32.py::make_mock_app."""
    mock_app = MagicMock()
    # Dialog guard tests do not exercise background searches. A permissive
    # MagicMock registry otherwise starts real QThreads without owning them.
    mock_app.background_task_registry.can_start.return_value = False
    mock_app.theme.privacy = False
    mock_app.theme.dark = False
    mock_app.content_scroll.viewport().width.return_value = 1280
    mock_app.content_scroll.verticalScrollBar().value.return_value = 0
    mock_app.page_density = "normal"
    db_file = tmp_path / "financas.db"
    db_file.touch()
    mock_app.repo.db.path = db_file
    mock_app.repo.db.conn = MagicMock()
    mock_app.repo.setting.side_effect = lambda k, default="": str(default)
    mock_app.repo.saved_filters.return_value = []
    mock_app.repo.monthly_snapshot_notes.return_value = ""
    mock_app.repo.month_period.return_value = ("2026-08-01", "2026-08-31", "2026-08")
    mock_app.repo.month_target.return_value = {"expense_target": 0.0}
    mock_app.repo.totals_for_payment_period.return_value = {
        "income": 1000.0,
        "expenses": 500.0,
        "savings": 500.0,
    }
    mock_app.repo.top_categories_payment_period.return_value = []
    mock_app.repo.monthly_evolution_payment_period.return_value = []
    mock_app.repo.savings_rate_milestones.return_value = []
    mock_app.repo.list_transactions.return_value = []
    mock_app.repo.filter_tags.return_value = []
    mock_app.repo.account_names.return_value = []
    mock_app.repo.category_names.return_value = []
    mock_app.repo.active_holding_classes.return_value = []
    mock_app.repo.holdings_summary.return_value = {"total_value": 0.0, "holdings": []}
    mock_app.analytics_service.payment_method_totals.return_value = []
    mock_app.analytics_service.totals_for_period.return_value = {
        "income": 1000.0,
        "expenses": 500.0,
        "savings": 500.0,
        "investments": 0.0,
    }
    mock_app.analytics_service.category_totals.return_value = []
    mock_app.analytics_service.monthly_summary_for_period.return_value = []
    return mock_app


def _run_created_transactions(mutation: Callable[[], Any], **_: Any) -> list[int]:
    """Execute the journaled mutation so the repo stub records the call."""
    mutation()
    return [999]


def _make_page(tmp_path: Any) -> tuple[MovementsPage, MagicMock]:
    app = make_mock_app(tmp_path)
    app.change_journal_service.run_with_created_transactions = MagicMock(
        side_effect=_run_created_transactions
    )
    page = MovementsPage(app)
    page.repo = _RecordingRepo()
    page.force_refresh_after_save = MagicMock()
    return page, app


def test_bugdlg03_zero_amount_reopens_dialog_and_skips_repo(qapp, tmp_path):
    """Accepted 0.00 -> warning + dialog re-shown; repo untouched until valid."""
    page, _app = _make_page(tmp_path)
    repo = page.repo
    fake = _FakeTransactionDialog(
        results=[QDialog.Accepted, QDialog.Accepted],
        payloads=[_payload(ZERO_AMOUNT), _payload(VALID_AMOUNT)],
    )
    with patch.object(
        movements_module, "TransactionDialog", side_effect=_dialog_factory(fake)
    ):
        with patch.object(movements_module.QMessageBox, "warning") as mock_warning:
            page.add_transaction()
    assert fake.exec_calls == 2
    mock_warning.assert_called_once()
    assert "não pode ser zero" in mock_warning.call_args[0][2]
    # The 0.00 attempt never reaches the repo; the corrected amount is saved.
    assert len(repo.add_calls) == 1
    assert repo.add_calls[0][0][2] == VALID_AMOUNT
    assert all(args[2] != ZERO_AMOUNT for args, _kw in repo.add_calls)
    page.force_refresh_after_save.assert_called_once_with(999)


def test_bugdlg03_zero_amount_then_cancel_preserves_repo(qapp, tmp_path):
    """Accepted 0.00 followed by Cancel -> warning once, repo untouched."""
    page, _app = _make_page(tmp_path)
    repo = page.repo
    fake = _FakeTransactionDialog(
        results=[QDialog.Accepted, QDialog.Rejected],
        payloads=[_payload(ZERO_AMOUNT)],
    )
    with patch.object(
        movements_module, "TransactionDialog", side_effect=_dialog_factory(fake)
    ):
        with patch.object(movements_module.QMessageBox, "warning") as mock_warning:
            page.add_transaction()
    assert fake.exec_calls == 2
    mock_warning.assert_called_once()
    assert repo.add_calls == []
    page.force_refresh_after_save.assert_not_called()


def test_bugdlg03_empty_description_warns_once_and_stops(qapp, tmp_path):
    """Existing empty-description guard must keep its exact behavior."""
    page, _app = _make_page(tmp_path)
    repo = page.repo
    payload = _payload(VALID_AMOUNT)
    payload["description"] = ""
    fake = _FakeTransactionDialog([QDialog.Accepted], [payload])
    with patch.object(
        movements_module, "TransactionDialog", side_effect=_dialog_factory(fake)
    ):
        with patch.object(movements_module.QMessageBox, "warning") as mock_warning:
            page.add_transaction()
    assert fake.exec_calls == 1
    mock_warning.assert_called_once_with(
        page, movements_module.APP_TITLE, "A descrição é obrigatória."
    )
    assert repo.add_calls == []


def test_bugdlg03_valid_amount_first_try_calls_repo_once(qapp, tmp_path):
    """Valid amount on first exec -> repo called exactly once, form intact."""
    page, _app = _make_page(tmp_path)
    repo = page.repo
    fake = _FakeTransactionDialog([QDialog.Accepted], [_payload(VALID_AMOUNT)])
    with patch.object(
        movements_module, "TransactionDialog", side_effect=_dialog_factory(fake)
    ):
        with patch.object(movements_module.QMessageBox, "warning") as mock_warning:
            page.add_transaction()
    assert fake.exec_calls == 1
    mock_warning.assert_not_called()
    assert len(repo.add_calls) == 1
    args, kwargs = repo.add_calls[0]
    assert args == (
        "2026-09-05",
        "Mercado",
        VALID_AMOUNT,
        "Alimentação",
        "Conta principal",
        "Compra teste",
    )
    assert kwargs == {
        "tags": "mercado",
        "merchant": "Pingo Doce",
        "payment_method": "Cartão de débito",
        "subcategory": "Mercearia",
        "purchase_date": "2026-09-04",
        "payment_date": "2026-09-05",
        "commit": False,
    }
    page.force_refresh_after_save.assert_called_once_with(999)


def test_bugdlg03_zero_amount_edit_path_reopens_dialog(qapp, tmp_path):
    """Edit path (row-based dialog) gets the same zero-amount reopen guard."""
    page, _app = _make_page(tmp_path)
    repo = page.repo
    page.selected_ids = MagicMock(return_value=[7])
    page._run_journaled_change = MagicMock(return_value=(True, None))
    fake = _FakeTransactionDialog(
        results=[QDialog.Accepted, QDialog.Accepted],
        payloads=[_payload(ZERO_AMOUNT), _payload(VALID_AMOUNT)],
    )
    with patch.object(
        movements_module, "TransactionDialog", side_effect=_dialog_factory(fake)
    ):
        with patch.object(movements_module.QMessageBox, "warning") as mock_warning:
            with patch.object(movements_module.QTimer, "singleShot"):
                page.edit_selected()
    assert fake.exec_calls == 2
    mock_warning.assert_called_once()
    assert repo.update_calls == []
    page._run_journaled_change.assert_called_once()
    page.force_refresh_after_save.assert_called_once_with(7)
