"""Automatic accessibility metadata for lazily-created Qt controls."""

from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtCore import QChildEvent, QEvent, QObject, QTimer
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSlider,
    QAbstractSpinBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QTextEdit,
    QWidget,
)

try:
    from shiboken6 import isValid as _shiboken_is_valid
except ImportError:  # pragma: no cover - shipped with PySide6
    _shiboken_is_valid = None


def plain_accessible_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "<" in text and ">" in text:
        document = QTextDocument()
        document.setHtml(text)
        text = document.toPlainText()
    text = re.sub(r"\s+", " ", text).strip()
    return text


@dataclass(frozen=True)
class AccessibilityAudit:
    inspected: int
    named: int
    descriptions_added: int
    unnamed: tuple[str, ...]


class AccessibilityManager(QObject):
    """Fill missing names/descriptions without overriding authored metadata."""

    INTERACTIVE_TYPES = (
        QAbstractButton,
        QAbstractItemView,
        QAbstractSlider,
        QAbstractSpinBox,
        QComboBox,
        QLineEdit,
        QTextEdit,
    )

    def __init__(self, owner: QWidget) -> None:
        super().__init__(owner)
        self.owner = owner
        self._deferred_apply: set[int] = set()

    def _deferred_apply_for(self, widget: QWidget) -> None:
        """Run a deferred accessibility application outside polish cycles."""
        self._deferred_apply.discard(id(widget))
        if _shiboken_is_valid is not None and not _shiboken_is_valid(widget):
            return
        try:
            self.apply_to_widget(widget)
        except RuntimeError:
            return

    @staticmethod
    def _buddy_label(widget: QWidget) -> str:
        parent = widget.parentWidget()
        if parent is None:
            return ""
        for label in parent.findChildren(QLabel):
            if label.buddy() is widget:
                return plain_accessible_text(label.text())
        return ""

    @classmethod
    def _derived_name(cls, widget: QWidget) -> str:
        candidates: list[object] = [cls._buddy_label(widget)]
        if isinstance(widget, QAbstractButton):
            candidates.append(widget.text())
        if isinstance(widget, QLineEdit):
            candidates.append(widget.placeholderText())
        # QComboBox deliberately never derives its name from currentText:
        # the accessible name must describe the field ("Tipo", "Conta"),
        # not the selected value ("Despesa").  A value-derived name goes
        # stale the moment the user picks another item and then reads the
        # wrong field to screen readers.
        candidates.extend((widget.toolTip(), widget.objectName()))
        for candidate in candidates:
            text = plain_accessible_text(candidate)
            if text:
                return text
        fallbacks = {
            QAbstractItemView: "Tabela ou lista",
            QAbstractSlider: "Controlo deslizante",
            QAbstractSpinBox: "Campo numérico",
            QComboBox: "Lista de opções",
            QLineEdit: "Campo de texto",
            QTextEdit: "Área de texto",
            QAbstractButton: "Botão",
        }
        for widget_type, label in fallbacks.items():
            if isinstance(widget, widget_type):
                return label
        return "Controlo"

    _DERIVED_SOURCE_PROPERTY = "_accessibleNameDerivedFrom"

    @staticmethod
    def _derivation_source(widget: QWidget) -> tuple[str, str]:
        """Record which volatile value an auto-derived name came from."""
        # QComboBox is intentionally absent: currentText is the selected
        # value, never a stable field label, so it must not be recorded
        # (nor trusted later) as a name source.
        if isinstance(widget, QLineEdit):
            return (
                "placeholderText",
                plain_accessible_text(widget.placeholderText()),
            )
        if isinstance(widget, QAbstractButton):
            return "text", plain_accessible_text(widget.text())
        return "", ""

    @classmethod
    def _stale_derived_name(cls, widget: QWidget) -> bool:
        """Detect a name auto-derived from a value the user has changed.

        Names derived from ``currentText`` follow the combo selection at
        derivation time and become wrong the moment the user picks another
        item; the same applies to placeholders and button captions.  When the
        recorded source no longer matches, the name must be re-derived from a
        stable anchor (buddy label, tooltip, object name).
        """
        source = widget.property(cls._DERIVED_SOURCE_PROPERTY)
        if not isinstance(source, tuple) or len(source) != 2:
            return False
        kind, captured = source
        # No "currentText" branch: combos no longer capture the selected
        # value as their name source, so nothing value-derived can go stale.
        if kind == "currentText":
            return True
        if kind == "placeholderText" and isinstance(widget, QLineEdit):
            return plain_accessible_text(widget.placeholderText()) != str(captured)
        if kind == "text" and isinstance(widget, QAbstractButton):
            return plain_accessible_text(widget.text()) != str(captured)
        return False

    @classmethod
    def apply_to_widget(cls, widget: QWidget) -> tuple[bool, bool]:
        if not isinstance(widget, cls.INTERACTIVE_TYPES):
            return False, False
        named = False
        described = False
        if not plain_accessible_text(widget.accessibleName()):
            widget.setAccessibleName(cls._derived_name(widget))
            widget.setProperty(
                cls._DERIVED_SOURCE_PROPERTY, cls._derivation_source(widget)
            )
            named = True
        elif cls._stale_derived_name(widget):
            refreshed = cls._derived_name(widget)
            if (
                plain_accessible_text(refreshed)
                and refreshed != widget.accessibleName()
            ):
                widget.setAccessibleName(refreshed)
                widget.setProperty(
                    cls._DERIVED_SOURCE_PROPERTY, cls._derivation_source(widget)
                )
                named = True
        tooltip = plain_accessible_text(widget.toolTip())
        if tooltip and not plain_accessible_text(widget.accessibleDescription()):
            widget.setAccessibleDescription(tooltip)
            described = True
        return named, described

    def audit_tree(self, root: QWidget) -> AccessibilityAudit:
        widgets = [
            widget
            for widget in root.findChildren(QWidget)
            if isinstance(widget, self.INTERACTIVE_TYPES)
        ]
        if isinstance(root, self.INTERACTIVE_TYPES):
            widgets.insert(0, root)
        named = descriptions = 0
        for widget in widgets:
            name_added, description_added = self.apply_to_widget(widget)
            named += int(name_added)
            descriptions += int(description_added)
        unnamed = tuple(
            f"{widget.__class__.__name__}:{widget.objectName() or '<sem id>'}"
            for widget in widgets
            if not plain_accessible_text(widget.accessibleName())
        )
        return AccessibilityAudit(
            inspected=len(widgets),
            named=named,
            descriptions_added=descriptions,
            unnamed=unnamed,
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Never mutate accessible properties synchronously inside Show /
        # ChildAdded delivery: those events fire while Qt is polishing the
        # widget tree (e.g. from a global ``app.setStyleSheet`` during theme
        # changes) and each accessibleName/property mutation schedules
        # another polish pass, re-delivering Show and re-entering this
        # filter -> unbounded native recursion / hang.  Defer via a queued
        # single-shot timer so metadata is applied after the current event
        # (and any polish cascade) settles, with a guard against stacking.
        if isinstance(watched, QWidget) and event.type() in {
            QEvent.Type.Show,
            QEvent.Type.FocusIn,
        }:
            self._deferred_apply.add(id(watched))
            QTimer.singleShot(0, lambda: self._deferred_apply_for(watched))
        elif isinstance(event, QChildEvent) and event.type() == QEvent.Type.ChildAdded:
            # Intentionally inert.  Scheduling work (or retaining any event-
            # derived reference) while Qt delivers ChildAdded corrupts
            # shiboken's wrapper typing for the child being added on this
            # stack (PySide6 6.11.1 + Python 3.14): lazily-created widgets
            # such as QMainWindow.statusBar() afterwards come back as plain
            # QWidget, and later event deliveries raise SystemError.
            # Children are instead covered when they are shown (branch
            # above) or by explicit ``audit_tree`` passes.
            pass
        return False
