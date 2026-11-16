"""Closed tax windows release native widgets before application teardown."""

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from database.engine import FinanceDB
from database.repo import Repo
from ui.dialogs import PortugueseTaxDialog
from ui.pages.investments import InvestmentsPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("outcome", ["accept", "reject", "error"])
def test_tax_action_releases_every_closed_dialog(qapp, tmp_path, empty_profile, monkeypatch, outcome):
    repo = Repo(FinanceDB(tmp_path / "tax-dialog.sqlite3"))
    owner = QWidget()
    owner.repo = repo
    dialogs = []
    original_init = PortugueseTaxDialog.__init__

    def initialize(dialog, *args, **kwargs):
        original_init(dialog, *args, **kwargs)
        dialogs.append(dialog)
        if outcome != "error":
            QTimer.singleShot(0, dialog, getattr(dialog, outcome))

    monkeypatch.setattr(PortugueseTaxDialog, "__init__", initialize)
    if outcome == "error":
        def fail_exec(_dialog):
            raise RuntimeError("Synthetic native-loop failure")
        monkeypatch.setattr(PortugueseTaxDialog, "exec", fail_exec)
    try:
        for _ in range(3):
            if outcome == "error":
                with pytest.raises(RuntimeError, match="Synthetic native-loop"):
                    InvestmentsPage._open_tax_dialog(owner)
            else:
                InvestmentsPage._open_tax_dialog(owner)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            assert all(not isValid(dialog) for dialog in dialogs)
            assert not owner.findChildren(PortugueseTaxDialog)
    finally:
        owner.close()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        repo.close()
