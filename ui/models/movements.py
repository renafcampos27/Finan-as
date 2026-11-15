from __future__ import annotations

import html
from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QComboBox, QStyledItemDelegate, QWidget

from ui.core.theme import Theme
from utils.app_logging import log_exception, log_warning
from utils.helpers import GREEN, RED, eur, parse_eur_input, row_get, safe_float


class MovementsTableModel(QAbstractTableModel):
    """Table model for movements with a real model reset after every save.

    QTableWidget can keep stale painted cell text on some Windows/PySide6
    combinations. A proper QAbstractTableModel lets us call beginResetModel /
    endResetModel, so the view is forced to ask the model for fresh values.
    """

    HEADERS = [
        "ID",
        "Pagamento",
        "Compra",
        "Descrição",
        "Categoria",
        "Subcategoria",
        "Método",
        "Conta",
        "Valor",
        "Etiquetas",
        "Anexos",
    ]

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.theme = theme
        self.page = parent  # Reference to MovementsPage for db access
        self._rows: list[Any] = []
        self._sort_column: Optional[int] = None
        self._sort_order = Qt.DescendingOrder
        self._sort_warning_columns: set[int] = set()

    def set_rows(self, rows: list[Any], theme: Optional[Theme] = None) -> None:
        if theme is not None:
            self.theme = theme
        new_rows = list(rows)
        if self._sort_column is not None:
            new_rows = self._sorted_rows(new_rows, self._sort_column, self._sort_order)
        self.beginResetModel()
        self._rows = new_rows
        self.endResetModel()

    def rowCount(self, parent: Optional[QModelIndex] = None) -> int:  # type: ignore[override]
        return 0 if parent is not None and parent.isValid() else len(self._rows)

    def columnCount(self, parent: Optional[QModelIndex] = None) -> int:  # type: ignore[override]
        return 0 if parent is not None and parent.isValid() else len(self.HEADERS)


    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole
    ):  # type: ignore[override]
        if (
            role == Qt.DisplayRole
            and orientation == Qt.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def row_object(self, row: int) -> Optional[Any]:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def transaction_id(self, row: int) -> Optional[int]:
        r = self.row_object(row)
        if r is None:
            return None
        try:
            return int(r["id"])
        except Exception:
            return None

    def _sort_key(self, r: Any, col: int) -> Any:
        if col == 0:
            return int(row_get(r, "id", 0) or 0)
        if col in (1, 2):
            return str(
                row_get(r, "payment_date" if col == 1 else "purchase_date")
                or row_get(r, "date")
            )
        if col == 4:
            cat = str(row_get(r, "category") or "")
            if cat.startswith("⚠️"):
                cat = cat.lstrip("⚠️").strip()
            return cat.casefold()
        if col == 8:
            return safe_float(row_get(r, "amount"))
        if col == 10:
            return int(row_get(r, "attachment_count", 0) or 0)
        return str(self._display_value(r, col)).casefold()

    def _sorted_rows(self, rows: list[Any], col: int, order: Qt.SortOrder) -> list[Any]:
        reverse = order == Qt.DescendingOrder
        try:
            return sorted(rows, key=lambda r: self._sort_key(r, col), reverse=reverse)
        except Exception as exc:
            if col not in self._sort_warning_columns:
                self._sort_warning_columns.add(col)
                log_warning(
                    "A ordenação da coluna %s falhou; foi mantida a ordem atual: %s",
                    col,
                    exc,
                )
            return rows

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:  # type: ignore[override]
        self._sort_column = int(column)
        self._sort_order = order
        self.beginResetModel()
        self._rows = self._sorted_rows(self._rows, self._sort_column, self._sort_order)
        self.endResetModel()

    def _display_value(self, r: Any, col: int) -> str:
        attachment_count = int(row_get(r, "attachment_count", 0) or 0)
        category = str(row_get(r, "category") or "")
        if col == 4 and int(row_get(r, "category_inconsistent", 0) or 0):
            category = f"⚠️ {category}"
        vals = [
            row_get(r, "id"),
            row_get(r, "payment_date") or row_get(r, "date"),
            row_get(r, "purchase_date") or row_get(r, "date"),
            row_get(r, "description"),
            category,
            row_get(r, "subcategory"),
            row_get(r, "payment_method"),
            row_get(r, "account"),
            eur(safe_float(row_get(r, "amount")), self.theme.privacy),
            row_get(r, "tags"),
            str(attachment_count) if attachment_count else "—",
        ]
        return str(vals[col]) if 0 <= col < len(vals) else ""

    def _row_tooltip(self, r: Any) -> str:
        amount = eur(safe_float(row_get(r, "amount")), self.theme.privacy)
        description = html.escape(str(row_get(r, "description") or "Movimento"))
        category = str(row_get(r, "category") or "Sem categoria")
        if row_get(r, "subcategory"):
            category += f" / {row_get(r, 'subcategory')}"
        fields = [
            (
                "Datas",
                f"Pagamento: {row_get(r, 'payment_date') or row_get(r, 'date')}  ·  "
                f"Compra: {row_get(r, 'purchase_date') or row_get(r, 'date')}",
            ),
            ("Categoria", category),
            ("Valor", f"{amount}  ·  Conta: {row_get(r, 'account') or '—'}"),
        ]
        if int(row_get(r, "category_inconsistent", 0) or 0):
            fields.insert(
                2,
                (
                    "Aviso",
                    "A categoria não corresponde ao tipo receita/despesa. Edita o movimento para corrigir.",
                ),
            )
        if row_get(r, "payment_method"):
            fields.append(("Método", str(row_get(r, "payment_method"))))
        if row_get(r, "merchant"):
            fields.append(("Comerciante", str(row_get(r, "merchant"))))
        if row_get(r, "tags"):
            fields.append(("Etiquetas", str(row_get(r, "tags"))))
        if row_get(r, "notes"):
            fields.append(("Notas", str(row_get(r, "notes"))[:260]))
        rows = "".join(
            "<tr>"
            f"<td width='92' valign='top'><b>{html.escape(label)}</b></td>"
            f"<td valign='top'>{html.escape(value)}</td>"
            "</tr>"
            for label, value in fields
            if value.strip()
        )
        return (
            "<div style='white-space:normal'>"
            f"<p style='font-size:14pt;font-weight:700;margin:0 0 8px 0'>{description}</p>"
            f"<table width='410' cellspacing='0' cellpadding='4'>{rows}</table>"
            "</div>"
        )

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # type: ignore[override]
        if not index.isValid():
            return None
        r = self.row_object(index.row())
        if r is None:
            return None
        col = index.column()
        if role == Qt.DisplayRole:
            return self._display_value(r, col)
        if role == Qt.EditRole:
            if col == 4:
                return str(row_get(r, "category") or "")
            if col == 8:
                return str(row_get(r, "amount") or "")
            return self._display_value(r, col)
        if role == Qt.ToolTipRole:
            return self._row_tooltip(r)
        if role == Qt.TextAlignmentRole and col in (8, 10):
            return Qt.AlignRight | Qt.AlignVCenter
        if role == Qt.ForegroundRole and col == 8:
            return QColor(GREEN if safe_float(r["amount"]) >= 0 else RED)
        if (
            role == Qt.ForegroundRole
            and col == 4
            and int(row_get(r, "category_inconsistent", 0) or 0)
        ):
            return QColor("#C56A00")
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if not index.isValid():
            return flags
        # Allow editing for Description (3), Category (4), Value (8)
        if index.column() in (3, 4, 8):
            flags |= Qt.ItemIsEditable
        return flags

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:
        if role == Qt.EditRole and index.isValid():
            row_idx = index.row()
            col_idx = index.column()
            r = self.row_object(row_idx)
            if r is None or not hasattr(self, "page") or self.page is None:
                return False

            tx_id = self.transaction_id(row_idx)
            if not tx_id:
                return False

            new_val = str(value).strip()
            # Depending on column, update different fields
            updates = {}
            if col_idx == 3:  # Description
                updates["description"] = new_val
            elif col_idx == 4:  # Category
                updates["category"] = new_val
            elif col_idx == 8:  # Value
                try:
                    updates["amount"] = parse_eur_input(new_val, field_name="valor")
                except ValueError:
                    return False

            if updates:
                try:
                    updated = self.page.apply_inline_edit(tx_id, updates)
                    if not updated:
                        return False
                    # Fresh database reads return immutable sqlite3.Row objects.
                    # Replace the cached row instead of mutating it after the
                    # database commit has already succeeded.
                    updated_row = dict(r)
                    updated_row.update(dict(updated))
                    self._rows[row_idx] = updated_row
                    self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])

                    # Schedule a full DB refresh to update summary cards and re-sort if needed
                    QTimer.singleShot(
                        0,
                        lambda: self.page.force_refresh_after_save(
                            tx_id,
                            notify_views=False,
                        ),
                    )
                    return True
                except Exception as exc:
                    log_exception("Inline edit failed", exc)
        return False


class MovementCategoryDelegate(QStyledItemDelegate):
    """Offer only categories compatible with the selected movement's sign."""

    def createEditor(self, parent, option, index):  # type: ignore[override]
        combo = QComboBox(parent)
        model = index.model()
        row = model.row_object(index.row()) if hasattr(model, "row_object") else None
        page = getattr(model, "page", None)
        amount = safe_float(row_get(row, "amount"))
        kind = "income" if amount > 0 else "expense"
        values = page.repo.categories(kind) if page is not None else []
        combo.addItems(list(values))
        combo.setEditable(False)
        return combo

    def setEditorData(self, editor, index):  # type: ignore[override]
        value = str(index.data(Qt.EditRole) or "")
        found = editor.findText(value)
        if found >= 0:
            editor.setCurrentIndex(found)

    def setModelData(self, editor, model, index):  # type: ignore[override]
        model.setData(index, editor.currentText(), Qt.EditRole)
