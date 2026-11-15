"""Application-wide visual privacy for monetary input controls."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QLineEdit,
    QWidget,
)


class MoneyInputPrivacyManager(QObject):
    """Mask euro spin boxes unless the user is actively editing them."""

    _REFRESH_EVENTS = {
        QEvent.Type.Show,
        QEvent.Type.FocusIn,
        QEvent.Type.FocusOut,
        QEvent.Type.EnabledChange,
        QEvent.Type.ParentChange,
    }

    def __init__(self, owner: QWidget) -> None:
        super().__init__(owner)
        self.owner = owner
        self._privacy_enabled = False

    @staticmethod
    def _spinbox_for(watched: QObject) -> QDoubleSpinBox | None:
        if isinstance(watched, QDoubleSpinBox):
            return watched
        if isinstance(watched, QLineEdit):
            parent = watched.parentWidget()
            if isinstance(parent, QDoubleSpinBox):
                return parent
        return None

    @staticmethod
    def _is_monetary(spinbox: QDoubleSpinBox) -> bool:
        if bool(spinbox.property("monetaryInput")):
            return True
        affixes = f"{spinbox.prefix()} {spinbox.suffix()}"
        return "€" in affixes

    def _apply_to(self, spinbox: QDoubleSpinBox) -> None:
        try:
            if not self._is_monetary(spinbox):
                return
            editor = spinbox.lineEdit()
            editing = spinbox.hasFocus() or editor.hasFocus()
            masked = self._privacy_enabled and not editing
            editor.setEchoMode(
                QLineEdit.EchoMode.Password if masked else QLineEdit.EchoMode.Normal
            )
            spinbox.setProperty("privacyMasked", masked)
            editor.setProperty("privacyMasked", masked)
            editor.setAccessibleDescription(
                "Valor monetário oculto. Ativa o campo para editar."
                if masked
                else "Valor monetário visível durante a edição."
            )
            editor.update()
        except RuntimeError:
            return

    def _apply_later(self, spinbox: QDoubleSpinBox) -> None:
        QTimer.singleShot(0, lambda target=spinbox: self._apply_to(target))

    def set_privacy(self, enabled: bool) -> None:
        self._privacy_enabled = bool(enabled)
        app = QApplication.instance()
        if app is not None:
            app.setProperty("financePrivacy", self._privacy_enabled)
            for widget in app.allWidgets():
                if isinstance(widget, QDoubleSpinBox):
                    self._apply_to(widget)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        spinbox = self._spinbox_for(watched)
        if spinbox is not None and event.type() in self._REFRESH_EVENTS:
            self._apply_later(spinbox)
        return False
