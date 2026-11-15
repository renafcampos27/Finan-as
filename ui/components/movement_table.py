"""Movement-table widgets shared by the movements page."""

from __future__ import annotations

import html
from typing import Any

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QLabel, QTableView, QVBoxLayout, QWidget

from utils.helpers import eur, row_get, safe_float


class MovementsTableView(QTableView):
    """Table view with a reliable in-app hover signal and no native tooltip."""

    movement_hovered = Signal(QModelIndex, QPoint)
    movement_hover_left = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # This table owns a richer movement preview, so the global readable
        # item-tooltip manager must not open a second card over it.
        self.setProperty("readableItemToolTipDisabled", True)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        position = event.position().toPoint()
        index = self.indexAt(position)
        super().mouseMoveEvent(event)
        self.movement_hovered.emit(index, position)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.movement_hover_left.emit()
        super().leaveEvent(event)

    def viewportEvent(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.ToolTip:
            return True
        return super().viewportEvent(event)


class MovementHoverCard(QFrame):
    """In-app movement preview that never uses the native Windows tooltip."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("MovementHoverCard")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setFixedWidth(500)
        self.setMinimumHeight(140)
        self.setMaximumHeight(280)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.setSpacing(8)
        self.title = QLabel()
        self.title.setTextFormat(Qt.PlainText)
        self.title.setWordWrap(True)
        self.title.setStyleSheet(
            "color:#0F172A;background:transparent;font-size:16px;font-weight:800;"
        )
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setStyleSheet(
            "color:#0F172A;background:transparent;font-size:13px;font-weight:500;"
        )
        layout.addWidget(self.title)
        layout.addWidget(self.details)
        self.hide()

    def set_movement(self, row: Any, privacy: bool = False) -> None:
        self.title.setText(str(row_get(row, "description") or "Movimento"))
        category = str(row_get(row, "category") or "Sem categoria")
        if row_get(row, "subcategory"):
            category += f" / {row_get(row, 'subcategory')}"
        amount = eur(safe_float(row_get(row, "amount")), privacy)
        fields = [
            (
                "Datas",
                f"Pagamento: {row_get(row, 'payment_date') or row_get(row, 'date')}  ·  "
                f"Compra: {row_get(row, 'purchase_date') or row_get(row, 'date')}",
            ),
            ("Categoria", category),
            ("Valor", f"{amount}  ·  Conta: {row_get(row, 'account') or '—'}"),
        ]
        for label, key in (
            ("Método", "payment_method"),
            ("Comerciante", "merchant"),
            ("Etiquetas", "tags"),
        ):
            if row_get(row, key):
                fields.append((label, str(row_get(row, key))))
        if row_get(row, "notes"):
            fields.append(("Notas", str(row_get(row, "notes"))[:260]))
        rows = "".join(
            "<tr>"
            f"<td width='100' valign='top'><span style='color:#475569;font-weight:700'>{html.escape(label)}</span></td>"
            f"<td valign='top'><span style='color:#0F172A'>{html.escape(value)}</span></td>"
            "</tr>"
            for label, value in fields
        )
        self.details.setText(
            f"<table width='100%' cellspacing='0' cellpadding='4'>{rows}</table>"
        )

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor("#0A84FF"), 2))
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5),
            14,
            14,
        )
