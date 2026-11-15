#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import re
import sys
from typing import Optional

from utils.app_logging import log_exception
from utils.helpers import format_eur_number
from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import QEasingCurve, QLocale, QPoint, QPropertyAnimation, Qt
    from PySide6.QtGui import QKeyEvent, QValidator
    from PySide6.QtWidgets import (
        QApplication,
        QCalendarWidget,
        QDateEdit,
        QDialog,
        QDoubleSpinBox,
        QFrame,
        QLayout,
        QLineEdit,
        QScrollArea,
        QSizePolicy,
        QStyledItemDelegate,
        QVBoxLayout,
        QWidget,
    )

except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Custom message boxes
# ---------------------------------------------------------------------------

from .theme import Theme, apply_calendar_theme, theme_for_widget
from .form_accessibility import prepare_form_fields


def portuguese_number_locale() -> QLocale:
    """Return the app locale without accepting dots as group separators."""

    locale = QLocale(QLocale.Language.Portuguese, QLocale.Country.Portugal)
    locale.setNumberOptions(
        locale.numberOptions() | QLocale.NumberOption.RejectGroupSeparator
    )
    return locale


def configure_portuguese_number_locale() -> None:
    """Use comma decimals for numeric controls created by the application."""

    QLocale.setDefault(portuguese_number_locale())


# BUG-AUD-C134: strict Portuguese thousands notation (``1.250,50``) — dot
# groups of exactly three digits plus a comma-decimal tail.  Anything else
# containing a dot (e.g. ``3.14``) keeps the old rejection behaviour.
_THOUSANDS_DOT_RE = re.compile(r"[-+]?\d{1,3}(?:\.\d{3})+,\d{1,2}")


class PortugueseMoneyLineEdit(QLineEdit):
    """Editor that keeps manual monetary entry in Portuguese notation."""

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        # Converting a typed decimal key avoids the dangerous ``3.14`` ->
        # ``314`` outcome that would occur if Qt merely ignored the point.
        if event.text() == ".":
            self.insert(",")
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # type: ignore[override]
        text = str(source.text() if source is not None and source.hasText() else "")
        # BUG-AUD-C134: Portuguese/European financial notation uses the dot as
        # a thousands separator (``1.250,50``).  That paste used to be rejected
        # outright; normalize it to the comma-decimal form the editor accepts.
        if _THOUSANDS_DOT_RE.fullmatch(text) is not None:
            self.insert(text.strip().replace(".", ""))
            return
        if "." in text:
            QApplication.beep()
            self.setToolTip("Usa vírgula para os cêntimos, por exemplo 3,14.")
            return
        # PySide6 does not expose QLineEdit.insertFromMimeData on the base
        # wrapper; the clipboard already carries the text, so the documented
        # paste() path performs the same default insertion.
        self.paste()


class PortugueseMoneyValidator(QValidator):
    """Strict validator for editable table cells containing euro amounts."""

    def validate(self, text: str, position: int):  # type: ignore[override]
        candidate = str(text or "").strip().replace("€", "").strip()
        candidate = candidate.replace("\u00a0", "").replace("\u202f", "")
        if "." in candidate or " " in candidate:
            return QValidator.State.Invalid, text, position
        if candidate in {"", "+", "-"}:
            return QValidator.State.Intermediate, text, position
        if re.fullmatch(r"[+-]?\d+(?:,\d{0,2})?", candidate) is None:
            return QValidator.State.Invalid, text, position
        return QValidator.State.Acceptable, text, position


class EuroSpinBox(QDoubleSpinBox):
    """Money input with comma-only decimals and compact cent presentation."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setLineEdit(PortugueseMoneyLineEdit(self))
        self.setLocale(portuguese_number_locale())
        self.setDecimals(2)
        self.setProperty("monetaryInput", True)
        self.setAccessibleDescription(
            "Valor em euros. Usa vírgula para os cêntimos, por exemplo 3,14."
        )

    def textFromValue(self, value: float) -> str:  # type: ignore[override]
        return format_eur_number(value, grouping=False)


class PortugueseMoneyDelegate(QStyledItemDelegate):
    """Comma-decimal editor for monetary columns in item views."""

    def createEditor(self, parent, option, index):  # type: ignore[override]
        editor = PortugueseMoneyLineEdit(parent)
        editor.setValidator(PortugueseMoneyValidator(editor))
        editor.setAccessibleDescription(
            "Valor em euros. Usa vírgula para os cêntimos, por exemplo 3,14."
        )
        return editor

    def setEditorData(self, editor, index) -> None:  # type: ignore[override]
        text = str(index.data(Qt.ItemDataRole.EditRole) or "")
        editor.setText(text.replace("€", "").strip())
        editor.selectAll()

    def setModelData(self, editor, model, index) -> None:  # type: ignore[override]
        model.setData(index, editor.text().strip(), Qt.ItemDataRole.EditRole)


def prepare_date_edit(date_edit: QDateEdit, theme: Optional[Theme] = None) -> None:
    """Attach a fully styled calendar popup to a QDateEdit.

    QDateEdit creates its popup calendar lazily and, on Windows/PySide6, that
    popup may inherit the native dark palette even in the light theme. By
    assigning our own QCalendarWidget and styling it directly, every date field
    across the app uses the same light/dark calendar style.
    """
    try:
        if theme is None:
            theme = theme_for_widget(date_edit)
        date_edit.setCalendarPopup(True)
        cal = date_edit.calendarWidget()
        if cal is None:
            cal = QCalendarWidget(date_edit)
            date_edit.setCalendarWidget(cal)
        cal.setObjectName("FinanceCalendar")
        apply_calendar_theme(cal, theme)
    except Exception as exc:
        log_exception("Failed to prepare date edit calendar", exc)


class OptionalDateEdit(QDateEdit):
    """An unset optional date must not trap Tab in an absent date section."""

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if (
            self.specialValueText()
            and self.date() == self.minimumDate()
            and event.key() in (Qt.Key_Tab, Qt.Key_Backtab)
            and event.modifiers() in (Qt.NoModifier, Qt.ShiftModifier)
        ):
            QWidget.focusNextPrevChild(self, event.key() == Qt.Key_Tab)
            event.accept()
            return
        super().keyPressEvent(event)


class LiquidSheetDialog(QDialog):
    def __init__(self, parent=None, theme_provider=None):
        super().__init__(parent)
        self.theme_provider = theme_provider
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)

        self.overlay = QFrame(self)
        self.overlay.setObjectName("LiquidSheetOverlay")
        self.overlay.setStyleSheet(
            "QFrame#LiquidSheetOverlay { "
            "background: rgba(0, 0, 0, 110); border: none; "
            "}"
        )

        self.sheet = QFrame(self.overlay)
        self.sheet.setObjectName("LiquidSheetSurface")
        dark = False
        if theme_provider:
            theme = theme_provider()
            if theme:
                dark = theme.dark
        elif parent:
            theme = theme_for_widget(parent)
            if theme:
                dark = theme.dark

        bg = "rgba(32, 39, 56, 1.0)" if dark else "rgba(255, 255, 255, 1.0)"
        border = "rgba(255, 255, 255, 0.15)" if dark else "rgba(0, 0, 0, 0.1)"
        self.sheet.setStyleSheet(
            "QFrame#LiquidSheetSurface { "
            f"background: {bg}; border: 1px solid {border}; border-radius: 20px;"
            "}"
        )

        self.sheet_layout = QVBoxLayout(self.sheet)
        self.sheet_layout.setContentsMargins(24, 24, 24, 24)
        self.sheet_layout.setSpacing(12)
        self.sheet_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

        self.main_layout.addWidget(self.overlay)

    def set_content_layout(self, layout):
        # BUG-AUD-C133: wrap the sheet body in a transparent QScrollArea so
        # tall forms keep the action buttons reachable on 768p/HiDPI screens.
        scroll = QScrollArea(self.sheet)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setMinimumSize(0, 0)
        scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; } "
            "QWidget#LiquidSheetScrollContent { background: transparent; }"
        )
        content = QWidget(scroll)
        content.setObjectName("LiquidSheetScrollContent")
        content.setLayout(layout)
        scroll.setWidget(content)
        self._content_scroll = scroll
        self.sheet_layout.addWidget(scroll, 1)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        # BUG-AUD-C136: a fast close during the 450ms slide-in must stop the
        # running QPropertyAnimation or it keeps ticking against a sheet that
        # is being torn down (RuntimeError: wrapped C/C++ object deleted).
        anim = getattr(self, "anim", None)
        if anim is not None:
            anim.stop()
        super().closeEvent(event)

    def reject(self) -> None:  # type: ignore[override]
        anim = getattr(self, "anim", None)
        if anim is not None:
            anim.stop()
        super().reject()

    def done(self, r) -> None:  # type: ignore[override]
        anim = getattr(self, "anim", None)
        if anim is not None:
            anim.stop()
        super().done(r)

    def showEvent(self, event):
        super().showEvent(event)
        prepare_form_fields(self.sheet)
        if self.parent():
            # The sheet covers its parent widget.  When the parent is a plain
            # child page (e.g. an inner QStackedWidget page) its geometry() is
            # expressed in the parent widget's own coordinates, so assigning
            # it directly would anchor the dialog to (0, 0) of the window and
            # shift the sheet.  mapToGlobal() yields true top-level
            # coordinates in both cases, including offsets from sticky bars
            # and window frames.
            parent = self.parent()
            top_left = parent.mapToGlobal(QPoint(0, 0))
            self.setMinimumSize(
                min(self.minimumWidth(), max(1, parent.width())),
                min(self.minimumHeight(), max(1, parent.height())),
            )
            self.setGeometry(
                top_left.x(),
                top_left.y(),
                max(1, parent.width()),
                max(1, parent.height()),
            )

        # Position sheet
        # BUG-AUD-C133: cap the sheet height so tall scrollable forms never
        # push the action buttons off-screen on 768p/HiDPI displays.
        self.sheet.adjustSize()
        self.sheet.setMinimumSize(0, 0)
        w = min(max(480, self.sheet.width()), max(1, self.width() - 24))
        scroll = getattr(self, "_content_scroll", None)
        preferred_h = self.sheet.height()
        if scroll is not None:
            scroll.setMinimumHeight(0)
            margins = self.sheet_layout.contentsMargins()
            preferred_h = max(
                preferred_h,
                scroll.widget().sizeHint().height() + margins.top() + margins.bottom(),
            )
        # A minimum of 320 would overflow a short owner; the body already scrolls.
        max_h = max(1, self.height() - 60)
        h = min(preferred_h, max_h)
        self.sheet.setFixedHeight(h)

        end_y = max(0, (self.height() - h) // 2)
        end_x = max(0, (self.width() - w) // 2)
        # Keep the complete sheet inside the dialog from the first frame.  A
        # fully off-screen starting point could remain visible under heavy UI
        # load if the animation timer was temporarily starved.
        lowest_visible_y = max(0, self.height() - h)
        start_y = min(lowest_visible_y, end_y + 48)

        self.sheet.setGeometry(end_x, start_y, w, h)

        self.anim = QPropertyAnimation(self.sheet, b"pos")
        self.anim.setDuration(450)
        self.anim.setStartValue(QPoint(end_x, start_y))
        self.anim.setEndValue(QPoint(end_x, end_y))
        self.anim.setEasingCurve(QEasingCurve.OutBack)
        self.anim.start()

    def hideEvent(self, event):
        if self.parent() and self.parent().graphicsEffect() is not None:
            self.parent().setGraphicsEffect(None)
        super().hideEvent(event)
