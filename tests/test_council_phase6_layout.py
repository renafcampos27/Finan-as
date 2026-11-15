"""Synthetic Phase 6 dialog tasks across theme, text size and process DPI."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QComboBox,
    QWidget,
)
from shiboken6 import isValid

from database.engine import FinanceDB
from database.repo import Repo
from services.imports import ImportService
from services.reimbursements import ReimbursementsService
from ui.core.theme import Theme, apply_tooltip_theme
from ui.import_review import ImportReviewDialog
from ui.product_review import QualityChecklistDialog
from ui.reimbursements import ReimbursementsDialog
from ui.subscription_radar import SubscriptionRadarDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("segoeui.ttf", "seguisym.ttf", "seguiemj.ttf"):
        source = fonts / name
        if source.is_file():
            assert QFontDatabase.addApplicationFont(str(source)) >= 0
    assert "Segoe UI" in QFontDatabase.families()
    return app


@pytest.fixture
def repo(tmp_path, empty_profile):
    instance = Repo(FinanceDB(tmp_path / "layout.sqlite3"))
    yield instance
    instance.db.conn.close()


def _scaled(stylesheet: str, factor: float) -> str:
    return re.sub(
        r"font-size:\s*(\d+(?:\.\d+)?)px",
        lambda match: f"font-size: {float(match.group(1)) * factor:g}px",
        stylesheet,
    )


def _make(kind, parent, app, repo):
    if kind == "subscriptions":
        repo.add_subscription("Layout media", 10, "Diversos", 1, "Mensal")
        repo.add_transaction(
            "2026-01-02", "Layout media", -15, "Diversos", "Conta principal"
        )
        repo.commit()
        dialog = SubscriptionRadarDialog(repo, parent)
        dialog.classification.setCurrentIndex(dialog.classification.findData("price"))
        dialog.period.setCurrentIndex(dialog.period.findData("Mensal"))
        dialog.currency.setText("EUR")
        dialog.match.setChecked(True)
        return dialog, dialog.table, dialog.accept_button, "accepted_review"
    if kind == "reimbursements":
        transaction_id = repo.add_transaction(
            "2026-01-02", "Layout expense", -100, "Diversos", "Conta principal"
        )
        repo.commit()
        service = ReimbursementsService(repo)
        service.create(transaction_id, 80, label="Synthetic reimbursement")
        dialog = ReimbursementsDialog(
            service, parent, source_transaction_id=transaction_id
        )
        dialog.claims.selectRow(0)
        return dialog, dialog.claims, dialog.new_button, "reachable_then_close"
    if kind == "import":
        repo.add_category_rule("layout shop", "Supermercado", [])
        repo.commit()
        batch = (
            ImportService(repo)
            .review_batch(
                [
                    {
                        "Data": "2026-01-15",
                        "Descrição": "Layout shop A",
                        "Valor": "-10,50",
                    },
                    {
                        "Data": "2026-01-16",
                        "Descrição": "Layout shop B",
                        "Valor": "-11,50",
                    },
                ],
                {"date": "Data", "desc": "Descrição", "amount": "Valor"},
            )
            .choose_all("reject")
        )
        dialog = ImportReviewDialog(batch, parent)
        return dialog, dialog.table, dialog.save_button, "accepted_choices"
    dialog = QualityChecklistDialog(app, parent)
    target = dialog.table.cellWidget(0, 4)
    return dialog, dialog.table, target, "opened_origin"


@pytest.mark.parametrize(
    "kind", ["subscriptions", "reimbursements", "import", "quality"]
)
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("text_scale", [1.0, 1.5], ids=["normal_text", "large_text"])
def test_phase6_dialog_action_geometry(qapp, repo, tmp_path, kind, dark, text_scale):
    original_font = qapp.font()
    original_palette = qapp.palette()
    original_stylesheet = qapp.styleSheet()
    qapp.setFont(QFont("Segoe UI", 10))
    font = qapp.font()
    font.setPointSizeF(font.pointSizeF() * text_scale)
    qapp.setFont(font)
    theme = Theme(dark=dark)
    # MainWindow.apply_theme invokes this public hook before opening dialogs.
    # It supplies the real app's fallback palette as well as its popup QSS.
    apply_tooltip_theme(theme)
    navigated = []
    app = SimpleNamespace(repo=repo, theme=theme, goto=navigated.append)
    parent = QWidget()
    parent.app = app
    parent.theme = theme
    available = qapp.primaryScreen().availableGeometry()
    parent.resize(min(960, available.width() - 32), min(540, available.height() - 32))
    parent.setStyleSheet(_scaled(theme.stylesheet(), text_scale))
    dialog = None
    try:
        dialog, first, action, outcome = _make(kind, parent, app, repo)
        dialog.setStyleSheet(_scaled(theme.stylesheet(), text_scale))
        parent.show()
        dialog.show()
        qapp.processEvents()
        QTest.qWait(20)
        first.setFocus()
        visited = []
        row_editors = {}
        for _ in range(100):
            current = dialog.focusWidget()
            visited.append(type(current).__name__)
            if kind == "import" and isinstance(current, QComboBox):
                qapp.processEvents()
                row = current.property("import_review_row")
                rect = QRect(
                    current.mapTo(dialog.table.viewport(), QPoint()), current.size()
                )
                editor = {
                    "row": row,
                    "size": [current.width(), current.height()],
                    "hint": [current.sizeHint().width(), current.sizeHint().height()],
                    "contained": dialog.table.viewport().rect().contains(rect),
                }
                assert current.width() >= current.sizeHint().width(), editor
                assert current.height() >= current.sizeHint().height(), editor
                assert editor["contained"], editor
                row_editors[row] = editor
            if current is action:
                break
            QTest.keyClick(current or dialog, Qt.Key_Tab)
        qapp.processEvents()
        bounds = []
        owner = action.parentWidget()
        while owner is not None and owner is not dialog:
            if isinstance(owner, QAbstractScrollArea):
                mapped = QRect(action.mapTo(owner.viewport(), QPoint()), action.size())
                bounds.append(
                    {
                        "type": type(owner).__name__,
                        "viewport": [
                            owner.viewport().width(),
                            owner.viewport().height(),
                        ],
                        "action": [
                            mapped.x(),
                            mapped.y(),
                            mapped.width(),
                            mapped.height(),
                        ],
                        "contained": owner.viewport().rect().contains(mapped),
                    }
                )
            owner = owner.parentWidget()
        frame = dialog.frameGeometry()
        metadata = {
            "kind": kind,
            "dark": dark,
            "text_scale": text_scale,
            "actual_dpr": dialog.devicePixelRatioF(),
            "screen": [available.width(), available.height()],
            "frame": [frame.x(), frame.y(), frame.width(), frame.height()],
            "font_family": action.fontInfo().family(),
            "action_font_pixels": action.font().pixelSize(),
            "action_font_points": action.font().pointSizeF(),
            "action_font_height": action.fontMetrics().height(),
            "action_size": [action.width(), action.height()],
            "action_size_hint": [action.sizeHint().width(), action.sizeHint().height()],
            "focused": action.hasFocus(),
            "enabled": action.isEnabled(),
            "action_visible": action.isVisible(),
            "scroll_bounds": bounds,
            "visited": visited,
            "row_editors": list(row_editors.values()),
        }
        if os.environ.get("FINANCAS_PESSOAIS_PHASE6_CAPTURE") == "1":
            path = tmp_path / "dialog.png"
            assert dialog.grab().save(str(path), "PNG")
            metadata["png_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        print("PHASE6_LAYOUT_JSON=" + json.dumps(metadata))
        assert available.contains(frame), metadata
        assert dialog.palette().color(QPalette.Window) == QColor(theme.message_surface)
        assert action.isVisible() and action.isEnabled() and action.hasFocus(), metadata
        assert action.fontMetrics().height() > 0, metadata
        assert action.height() >= action.sizeHint().height(), metadata
        assert action.width() >= action.sizeHint().width(), metadata
        assert all(bound["contained"] for bound in bounds), metadata
        if kind == "import":
            assert len(row_editors) == dialog.table.rowCount(), metadata
        expected = os.environ.get("FINANCAS_PESSOAIS_PHASE6_EXPECT_DPR")
        if expected:
            assert dialog.devicePixelRatioF() == pytest.approx(float(expected))
        if outcome == "reachable_then_close":
            close = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close)
            for _ in range(100):
                if close.hasFocus():
                    break
                QTest.keyClick(dialog.focusWidget() or dialog, Qt.Key_Tab)
            assert close.hasFocus()
            QTest.keyClick(close, Qt.Key_Space)
        else:
            QTest.keyClick(action, Qt.Key_Space)
        qapp.processEvents()
        if outcome == "accepted_review":
            assert repo.subscription_price_reviews()[0]["status"] == "accepted"
            assert repo.movement_count() == 1
        elif outcome == "accepted_choices":
            assert dialog.result() == QDialog.DialogCode.Accepted
            assert repo.movement_count() == 0
        elif outcome == "opened_origin":
            assert dialog.result() == QDialog.DialogCode.Accepted and navigated
        else:
            assert not dialog.isVisible()
    finally:
        for widget in (dialog, parent):
            if widget is not None and isValid(widget):
                widget.close()
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        qapp.processEvents()
        qapp.setFont(original_font)
        qapp.setPalette(original_palette)
        qapp.setStyleSheet(original_stylesheet)
