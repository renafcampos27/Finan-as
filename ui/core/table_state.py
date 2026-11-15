from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import weakref
from typing import Any

from PySide6.QtCore import QAbstractItemModel, QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import (
    QCalendarWidget,
    QDialog,
    QHeaderView,
    QMenu,
    QTableView,
    QTableWidget,
    QWidget,
)

from ui.core.smart_table import SmartTableWidgetItem
from utils.app_logging import log_exception, log_warning

try:
    from shiboken6 import isValid as _shiboken_is_valid
except ImportError:  # pragma: no cover - shipped with PySide6
    _shiboken_is_valid = None


class TableColumnWidthManager(QObject):
    """Persist widths and visible columns for every table in the app."""

    STATE_PREFIX = "table_columns:v2"
    STATE_VERSION = 3
    COLUMN_IDS_BY_TABLE: dict[str, tuple[str, ...]] = {
        "ai.conversation_history": ("date", "month", "type", "model", "summary"),
        "budgets.assistant_categories": ("category", "suggested_budget"),
        "budgets.bucket_allocation": ("category", "bucket"),
        "budgets.list": (
            "category",
            "spent",
            "budget",
            "month_projection",
            "projected_margin",
            "used_percent",
            "previous_month",
            "status",
        ),
        "budgets.smart_forecast": (
            "category",
            "carried_balance",
            "effective_limit",
            "projection",
            "suggestion",
            "break_date",
            "status",
        ),
        "customization.page_cards": ("card", "visible", "disabled"),
        "dashboard.expenses_work_time": (
            "category",
            "amount",
            "work_hours",
            "work_days",
        ),
        "documents.list": (
            "id",
            "date",
            "movement",
            "file",
            "type",
            "description",
            "status",
            "path",
        ),
        "dynamic_analysis.results": ("group", "total", "movements", "weight"),
        "finance_center.alerts": (
            "priority",
            "date",
            "alert",
            "category",
            "amount",
        ),
        "goals.list": (
            "id",
            "name",
            "progress",
            "current_target",
            "remaining",
            "monthly",
            "deadline",
            "forecast",
            "status",
            "notes",
        ),
        "health.metrics": ("metric", "value", "reading"),
        "history.changes": (
            "category",
            "spent",
            "budget",
            "used_percent",
            "difference",
        ),
        "monthly_goals.list": (
            "id",
            "name",
            "type",
            "category",
            "target",
            "current",
            "status",
        ),
        "movements.list": (
            "id",
            "payment_date",
            "purchase_date",
            "description",
            "category",
            "subcategory",
            "payment_method",
            "account",
            "amount",
            "tags",
            "attachments",
        ),
        "movements.smart_split_preview": (
            "date",
            "description",
            "amount",
            "category",
            "count",
            "ids",
        ),
        "planned_purchases.list": (
            "id",
            "name",
            "target",
            "saved",
            "progress",
            "target_date",
            "priority",
        ),
        "portfolio.holdings": (
            "id",
            "name",
            "asset_class",
            "account",
            "current_value",
            "invested_value",
            "fees",
            "net_gain_loss",
            "net_return",
            "contribution_pp",
        ),
        "portfolio.liabilities": (
            "id",
            "name",
            "type",
            "entity",
            "principal",
            "interest_rate",
            "payment",
        ),
        "reports.anomalies": (
            "priority",
            "date",
            "description",
            "category",
            "amount",
        ),
        "reports.period_comparison": (
            "month",
            "income",
            "expenses",
            "savings",
            "savings_rate",
        ),
        "scheduled_payments.list": (
            "id",
            "name",
            "amount",
            "category",
            "payment_method",
            "start_date",
            "day",
            "duration",
            "paid",
            "next_debit",
        ),
        "score.components": (
            "dimension",
            "status",
            "score",
            "points",
            "maximum",
            "reading",
        ),
        "simulations.habit_audit": (
            "selected",
            "date",
            "description",
            "category",
            "amount",
            "origin",
        ),
        "simulations.habit_plans": (
            "month",
            "planned",
            "actual",
            "difference",
            "expected_savings",
            "actual_savings",
        ),
        "simulations.results": (
            "horizon",
            "current",
            "reduced_25",
            "half",
            "alternative",
            "real_savings",
            "invested_savings",
        ),
        "smart_import.preview": (
            "date",
            "description",
            "amount",
            "category",
            "account",
            "status",
            "reason",
        ),
        "splits.list": (
            "movement_id",
            "description",
            "category",
            "percent",
            "amount",
        ),
        "subscriptions.list": (
            "id",
            "name",
            "amount",
            "category",
            "account",
            "payment_method",
            "day",
            "next_renewal",
            "period",
            "effort_percent",
            "status",
            "notes",
            "statement_aliases",
        ),
    }

    def __init__(self, repo: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.repo = repo
        self._active = True
        self._reported_load_failures: dict[str, str] = {}
        self._reported_save_failures: dict[str, str] = {}
        self._defaults: weakref.WeakKeyDictionary[QTableView, dict[str, Any]] = (
            weakref.WeakKeyDictionary()
        )
        self._tables: weakref.WeakSet[QTableView] = weakref.WeakSet()
        self._timers: weakref.WeakKeyDictionary[QTableView, QTimer] = (
            weakref.WeakKeyDictionary()
        )
        self._models: weakref.WeakKeyDictionary[QTableView, QAbstractItemModel] = (
            weakref.WeakKeyDictionary()
        )
        self._reapply_timers: weakref.WeakKeyDictionary[QTableView, QTimer] = (
            weakref.WeakKeyDictionary()
        )
        self._headers: weakref.WeakKeyDictionary[QHeaderView, QTableView] = (
            weakref.WeakKeyDictionary()
        )
        self._suspended_sorts: weakref.WeakKeyDictionary[
            QTableView, tuple[int, Qt.SortOrder]
        ] = weakref.WeakKeyDictionary()

    @staticmethod
    def _is_valid_qt_object(obj: QObject | None) -> bool:
        if obj is None:
            return False
        try:
            return _shiboken_is_valid(obj) if _shiboken_is_valid else True
        except RuntimeError:
            return False

    def _restore_weak(self, reference: weakref.ReferenceType[QTableView]) -> None:
        table = reference()
        if self._is_valid_qt_object(table):
            self.restore_table(table)

    @staticmethod
    def _slug(value: str) -> str:
        normalised = unicodedata.normalize("NFKD", str(value or ""))
        ascii_text = normalised.encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-z0-9]+", "_", ascii_text.casefold()).strip("_")

    @classmethod
    def _normalise_stable_id(cls, value: str) -> str:
        parts = [
            cls._slug(part)
            for part in re.split(r"[.:/\\]+", str(value or ""))
            if cls._slug(part)
        ]
        return ".".join(parts)

    @staticmethod
    def _persistence_boundary(table: QTableView) -> QWidget:
        """Return the page/dialog that gives the table a durable UI scope."""
        current: QWidget | None = table
        fallback: QWidget = table
        while current is not None:
            fallback = current
            if isinstance(current, QDialog) or hasattr(current, "page_title"):
                return current
            current = current.parentWidget()
        return fallback

    @staticmethod
    def _attribute_name(
        table: QTableView, boundary: QWidget
    ) -> tuple[str, QWidget] | None:
        """Find a stable Python attribute that owns this table."""
        current: QWidget | None = table.parentWidget()
        while current is not None:
            try:
                for name, value in vars(current).items():
                    if value is table and not str(name).startswith("_"):
                        return str(name), current
            except (RuntimeError, TypeError):
                pass
            if current is boundary:
                break
            current = current.parentWidget()
        return None

    def _stable_table_id(self, table: QTableView) -> str:
        configured = self._normalise_stable_id(
            str(table.property("columnPersistenceId") or "")
        )
        if configured:
            return configured

        boundary = self._persistence_boundary(table)
        scope = self._slug(type(boundary).__name__) or "window"
        explicit = self._slug(str(table.objectName() or ""))
        if explicit:
            return f"{scope}.{explicit}"

        owner_attribute = self._attribute_name(table, boundary)
        if owner_attribute is not None:
            attribute, owner = owner_attribute
            owner_scope = self._slug(type(owner).__name__) or scope
            return f"{scope}.{owner_scope}.{self._slug(attribute) or 'table'}"

        try:
            candidates = [
                candidate
                for candidate in boundary.findChildren(QTableView)
                if self._should_manage_table(candidate)
            ]
            ordinal = candidates.index(table) + 1
        except (RuntimeError, ValueError):
            ordinal = 1
        return f"{scope}.table_{ordinal}"

    def _state_key(self, table: QTableView) -> str:
        stable_id = self._stable_table_id(table)
        return f"{self.STATE_PREFIX}:{stable_id}"

    def _column_ids(self, table: QTableView) -> list[str]:
        model = table.model()
        count = model.columnCount() if model is not None else 0
        configured = table.property("columnPersistenceColumnIds")
        values: list[str] = []
        if isinstance(configured, (list, tuple)):
            values = [self._slug(str(value)) for value in configured]
        if len(values) != count:
            stable_id = self._stable_table_id(table)
            registered = list(self.COLUMN_IDS_BY_TABLE.get(stable_id, ()))
            if len(registered) == count:
                values = registered
                table.setProperty("columnPersistenceUsesFallbackIds", False)
            else:
                values = []
                seen: dict[str, int] = {}
                for column in range(count):
                    title = self._column_title(table, column)
                    base = self._slug(title) or f"column_{column + 1}"
                    seen[base] = seen.get(base, 0) + 1
                    values.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
                table.setProperty("columnPersistenceUsesFallbackIds", True)
        table.setProperty("columnPersistenceColumnIds", values)
        return values

    @staticmethod
    def _column_entries(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw = state.get("columns")
        if isinstance(raw, dict):
            return {
                str(column_id): dict(details)
                for column_id, details in raw.items()
                if isinstance(details, dict)
            }
        if not isinstance(raw, list):
            return {}
        return {
            str(details.get("id")): dict(details)
            for details in raw
            if isinstance(details, dict) and str(details.get("id") or "").strip()
        }

    def _saved_width(
        self, table: QTableView, state: dict[str, Any], logical: int
    ) -> int | None:
        column_ids = self._column_ids(table)
        entries = self._column_entries(state)
        if 0 <= logical < len(column_ids):
            width = entries.get(column_ids[logical], {}).get("width")
            if width is not None:
                return max(24, min(2_000, int(width)))
        widths = list(state.get("widths", []))
        if 0 <= logical < len(widths):
            return max(24, min(2_000, int(widths[logical])))
        return None

    def _current_order(self, table: QTableView, column_ids: list[str]) -> list[str]:
        header = table.horizontalHeader()
        logical_columns = range(len(column_ids))
        return [
            column_ids[column]
            for column in sorted(
                logical_columns,
                key=lambda logical: header.visualIndex(logical),
            )
        ]

    @staticmethod
    def _sort_payload(
        table: QTableView, column_ids: list[str]
    ) -> dict[str, str] | None:
        if not table.isSortingEnabled():
            return None
        header = table.horizontalHeader()
        logical = int(header.sortIndicatorSection())
        if not (0 <= logical < len(column_ids)):
            return None
        order = (
            "descending"
            if header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
            else "ascending"
        )
        return {"column_id": column_ids[logical], "order": order}

    def _snapshot_state(
        self,
        table: QTableView,
        previous_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        model = table.model()
        if model is None:
            return {}
        header = table.horizontalHeader()
        column_ids = self._column_ids(table)
        widths: list[int] = []
        hidden: list[int] = []
        columns: list[dict[str, Any]] = []
        for logical, column_id in enumerate(column_ids):
            width = int(header.sectionSize(logical))
            if width < 24:
                saved = self._saved_width(table, previous_state or {}, logical)
                width = (
                    saved
                    if saved is not None
                    else max(
                        24,
                        int(header.sectionSizeHint(logical)),
                        int(header.defaultSectionSize()),
                    )
                )
            width = max(24, min(2_000, width))
            is_hidden = bool(table.isColumnHidden(logical))
            widths.append(width)
            if is_hidden:
                hidden.append(logical)
            columns.append(
                {
                    "id": column_id,
                    "width": width,
                    "hidden": is_hidden,
                    "visual_index": int(header.visualIndex(logical)),
                }
            )
        payload: dict[str, Any] = {
            "version": self.STATE_VERSION,
            "table_id": self._table_label(table),
            "columns": columns,
            "order": self._current_order(table, column_ids),
            "widths": widths,
            "widths_customized": bool(table.property("columnWidthsCustomized")),
            "visibility_customized": bool(table.property("columnVisibilityCustomized")),
            "order_customized": bool(table.property("columnOrderCustomized")),
            "sort_customized": bool(table.property("columnSortCustomized")),
        }
        if payload["visibility_customized"]:
            payload["hidden"] = hidden
        sort = self._sort_payload(table, column_ids)
        if sort is not None:
            payload["sort"] = sort
        return payload

    def _capture_defaults(self, table: QTableView) -> None:
        if table in self._defaults:
            return
        self._defaults[table] = self._snapshot_state(table)

    def _apply_column_order(
        self, table: QTableView, order: list[Any], column_ids: list[str]
    ) -> None:
        header = table.horizontalHeader()
        logical_by_id = {
            column_id: logical for logical, column_id in enumerate(column_ids)
        }
        selected = [
            str(column_id) for column_id in order if str(column_id) in logical_by_id
        ]
        for target_visual, column_id in enumerate(selected):
            logical = logical_by_id[column_id]
            current_visual = header.visualIndex(logical)
            if current_visual != target_visual:
                header.moveSection(current_visual, target_visual)

    def _apply_sort_state(
        self,
        table: QTableView,
        sort: Any,
        column_ids: list[str],
    ) -> None:
        if not table.isSortingEnabled() or not isinstance(sort, dict):
            return
        column_id = str(sort.get("column_id") or "")
        if column_id not in column_ids:
            return
        order = (
            Qt.SortOrder.DescendingOrder
            if str(sort.get("order") or "") == "descending"
            else Qt.SortOrder.AscendingOrder
        )
        table.sortByColumn(column_ids.index(column_id), order)

    def _apply_state(
        self,
        table: QTableView,
        state: dict[str, Any],
        *,
        force: bool = False,
    ) -> None:
        model = table.model()
        if model is None:
            return
        header = table.horizontalHeader()
        column_ids = self._column_ids(table)
        entries = self._column_entries(state)
        version = int(state.get("version") or 1)

        apply_widths = (
            force
            or version < self.STATE_VERSION
            or bool(state.get("widths_customized"))
        )
        if apply_widths:
            if entries:
                for logical, column_id in enumerate(column_ids):
                    details = entries.get(column_id, {})
                    if "width" in details:
                        header.resizeSection(
                            logical,
                            max(24, min(2_000, int(details["width"]))),
                        )
            else:
                for logical, width in enumerate(
                    list(state.get("widths", []))[: model.columnCount()]
                ):
                    header.resizeSection(logical, max(24, min(2_000, int(width))))

        apply_order = force or bool(state.get("order_customized"))
        if apply_order:
            order = state.get("order")
            if not isinstance(order, list) and entries:
                order = [
                    column_id
                    for column_id, _details in sorted(
                        entries.items(),
                        key=lambda item: int(
                            item[1].get("visual_index", len(column_ids))
                        ),
                    )
                ]
            if isinstance(order, list):
                self._apply_column_order(table, order, column_ids)

        has_legacy_hidden = isinstance(state.get("hidden"), list)
        apply_visibility = (
            force
            or bool(state.get("visibility_customized"))
            or (version < self.STATE_VERSION and has_legacy_hidden)
        )
        if apply_visibility:
            if entries:
                hidden = {
                    logical
                    for logical, column_id in enumerate(column_ids)
                    if bool(entries.get(column_id, {}).get("hidden"))
                }
            else:
                hidden = {
                    int(column)
                    for column in state.get("hidden", [])
                    if str(column).lstrip("-").isdigit()
                    and 0 <= int(column) < model.columnCount()
                }
            if len(hidden) >= model.columnCount():
                hidden.discard(0)
            for logical in range(model.columnCount()):
                table.setColumnHidden(logical, logical in hidden)

        if force and not isinstance(state.get("sort"), dict):
            table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        elif force or bool(state.get("sort_customized")):
            self._apply_sort_state(table, state.get("sort"), column_ids)

    def _legacy_state_key(self, table: QTableView) -> str:
        """Reproduce the pre-v2 key so existing preferences can be migrated."""
        explicit = str(table.objectName() or "").strip()
        page_name = "global"
        card_name = ""
        current: QWidget | None = table
        while current is not None:
            if hasattr(current, "page_title"):
                page_name = type(current).__name__
                break
            if not card_name:
                card_title_fn = getattr(current, "card_title", None)
                if callable(card_title_fn):
                    try:
                        card_name = str(card_title_fn() or "")
                    except RuntimeError:
                        card_name = ""
                else:
                    title_label = getattr(current, "title_label", None)
                    if title_label is not None and self._is_valid_qt_object(title_label):
                        try:
                            card_name = str(title_label.text() or "")
                        except RuntimeError:
                            card_name = ""
            current = current.parentWidget()

        model = table.model()
        headers: list[str] = []
        if model is not None:
            for column in range(model.columnCount()):
                value = model.headerData(column, Qt.Horizontal, Qt.DisplayRole)
                headers.append(str(value or column))
        signature_source = "|".join(headers) or type(table).__name__
        digest = hashlib.sha1(signature_source.encode("utf-8")).hexdigest()[:10]
        identity = self._slug(explicit or card_name) or digest
        return f"table_columns:{page_name}:{identity}:{digest}"

    def _table_label(self, table: QTableView) -> str:
        return str(
            table.property("columnPersistenceId")
            or self._stable_table_id(table)
            or type(table).__name__
        )

    def _report_load_failure(
        self, table: QTableView, key: str, exc: BaseException
    ) -> None:
        signature = f"{type(exc).__name__}:{exc}"
        if self._reported_load_failures.get(key) == signature:
            return
        self._reported_load_failures[key] = signature
        log_exception(
            "Failed to read table column preferences "
            f"[table={self._table_label(table)}, key={key}]",
            exc,
        )

    def _report_save_failure(
        self,
        table: QTableView,
        key: str,
        operation: str,
        exc: BaseException,
    ) -> None:
        signature = f"{operation}:{type(exc).__name__}:{exc}"
        if self._reported_save_failures.get(key) != signature:
            self._reported_save_failures[key] = signature
            log_exception(
                "Failed to persist table column preferences "
                f"[table={self._table_label(table)}, key={key}, "
                f"operation={operation}]",
                exc,
            )
        if self._is_valid_qt_object(table):
            table.setProperty("columnPersistenceSaveFailed", True)
            table.setProperty("columnPersistenceSaveError", str(exc))

    def _read_state_key(
        self, table: QTableView, key: str
    ) -> tuple[dict[str, Any] | None, bool]:
        try:
            raw = self.repo.setting(key, "")
        except Exception as exc:
            self._report_load_failure(table, key, exc)
            return None, True
        if raw is None or not str(raw).strip():
            return None, False
        try:
            state = json.loads(str(raw))
            if not isinstance(state, dict):
                raise ValueError("saved table column state is not an object")
        except Exception as exc:
            self._report_load_failure(table, key, exc)
            return None, True
        self._reported_load_failures.pop(key, None)
        return state, True

    def _write_state(
        self,
        table: QTableView,
        key: str,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> bool:
        try:
            self.repo.set_setting(
                key,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        except Exception as exc:
            self._report_save_failure(table, key, operation, exc)
            return False
        self._reported_save_failures.pop(key, None)
        if self._is_valid_qt_object(table):
            table.setProperty("columnPersistenceSaveFailed", False)
            table.setProperty("columnPersistenceSaveError", "")
        return True

    def register_tree(self, root: QWidget) -> None:
        if not self._active:
            return
        if isinstance(root, QTableView):
            self.register_table(root)
        for table in root.findChildren(QTableView):
            self.register_table(table)

    @staticmethod
    def _should_manage_table(table: QTableView) -> bool:
        """Ignore Qt's private grids and popup views, not user data tables."""
        if bool(table.property("disableColumnPersistence")):
            return False
        if str(table.objectName() or "") == "qt_calendar_calendarview":
            return False
        if str(table.property("columnPersistenceId") or "").strip():
            return True
        has_user_scope = False
        current: QWidget | None = table.parentWidget()
        while current is not None:
            if isinstance(current, QCalendarWidget):
                return False
            class_name = type(current).__name__
            if "ComboBoxPrivateContainer" in class_name or "Completer" in class_name:
                return False
            if isinstance(current, QDialog) or hasattr(current, "page_title"):
                has_user_scope = True
            current = current.parentWidget()
        return has_user_scope

    def register_table(self, table: QTableView) -> None:
        if not self._active:
            return
        if not self._should_manage_table(table):
            table.setProperty("columnPersistenceExcluded", True)
            return
        header = table.horizontalHeader()
        if header is None:
            return
        if bool(table.property("columnPersistenceRegistered")):
            self._headers[header] = table
            header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._connect_model(table)
            self.ensure_interactive(table)
            table_ref = weakref.ref(table)
            QTimer.singleShot(0, lambda ref=table_ref: self._restore_weak(ref))
            return
        stable_id = self._stable_table_id(table)
        table.setProperty("columnPersistenceRegistered", True)
        table.setProperty("columnPersistenceId", stable_id)
        table.setProperty("columnPersistenceKey", self._state_key(table))
        table.setProperty("columnPersistenceLegacyKey", self._legacy_state_key(table))
        table.setProperty("columnWidthsCustomized", False)
        table.setProperty("columnVisibilityCustomized", False)
        table.setProperty("columnOrderCustomized", False)
        table.setProperty("columnSortCustomized", False)
        self._column_ids(table)
        self._capture_defaults(table)
        timer = QTimer(table)
        timer.setSingleShot(True)
        timer.setInterval(450)
        timer.timeout.connect(lambda target=table: self.save_table(target))
        self._timers[table] = timer
        self._headers[header] = table
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.setAccessibleDescription(
            "Arrasta para reorganizar. Usa o botão direito para ajustar, "
            "ocultar ou repor colunas."
        )
        header.customContextMenuRequested.connect(
            lambda position, target=table: self._show_column_menu(target, position)
        )
        header.sectionResized.connect(
            lambda *_args, target=table: self._column_resized(target)
        )
        header.sectionMoved.connect(
            lambda *_args, target=table: self._column_moved(target)
        )
        header.sortIndicatorChanged.connect(
            lambda *_args, target=table: self._sort_changed(target)
        )
        self._connect_model(table)
        self._tables.add(table)
        self.ensure_interactive(table)
        table_ref = weakref.ref(table)
        QTimer.singleShot(0, lambda ref=table_ref: self._restore_weak(ref))

    @staticmethod
    def _column_title(table: QTableView, column: int) -> str:
        model = table.model()
        if model is None:
            return f"Coluna {column + 1}"
        value = model.headerData(column, Qt.Horizontal, Qt.DisplayRole)
        title = str(value or "").strip()
        return title or f"Coluna {column + 1}"

    @staticmethod
    def _visible_column_count(table: QTableView) -> int:
        model = table.model()
        if model is None:
            return 0
        return sum(
            1
            for column in range(model.columnCount())
            if not table.isColumnHidden(column)
        )

    def _load_state(self, table: QTableView) -> dict[str, Any]:
        if not self._active or self.repo is None:
            return {}
        key = str(table.property("columnPersistenceKey") or self._state_key(table))
        state, new_key_exists = self._read_state_key(table, key)
        if new_key_exists:
            return state or {}

        legacy_key = str(
            table.property("columnPersistenceLegacyKey")
            or self._legacy_state_key(table)
        )
        if legacy_key == key:
            return {}
        legacy_state, legacy_key_exists = self._read_state_key(table, legacy_key)
        if not legacy_key_exists or legacy_state is None:
            return {}

        migrated = dict(legacy_state)
        migrated["version"] = 2
        migrated["table_id"] = self._table_label(table)
        if self._write_state(
            table,
            key,
            migrated,
            operation=f"migrate from {legacy_key}",
        ):
            table.setProperty("columnPersistenceMigratedFrom", legacy_key)
        return migrated

    def _create_column_menu(self, table: QTableView, clicked_column: int = -1) -> QMenu:
        menu = QMenu(table)
        menu.setAccessibleName("Escolher colunas visíveis")
        try:
            from ui.core.theme import apply_menu_theme, theme_for_widget

            apply_menu_theme(menu, theme_for_widget(table))
        except (ImportError, RuntimeError):
            pass

        model = table.model()
        if model is None or model.columnCount() <= 0:
            unavailable = menu.addAction("Sem colunas disponíveis")
            unavailable.setEnabled(False)
            return menu

        visible_count = self._visible_column_count(table)
        if 0 <= clicked_column < model.columnCount() and not table.isColumnHidden(
            clicked_column
        ):
            fit_clicked = menu.addAction(
                f"Ajustar «{self._column_title(table, clicked_column)}» ao conteúdo"
            )
            fit_clicked.triggered.connect(
                lambda _checked=False, column=clicked_column, target=table: (
                    self.resize_columns_to_contents(target, [column])
                )
            )
            hide_clicked = menu.addAction(
                f"Ocultar «{self._column_title(table, clicked_column)}»"
            )
            hide_clicked.setEnabled(visible_count > 1)
            hide_clicked.triggered.connect(
                lambda _checked=False, column=clicked_column, target=table: (
                    self.set_column_visible(target, column, False)
                )
            )
            menu.addSeparator()

        heading = menu.addAction("Colunas visíveis")
        heading.setEnabled(False)
        for column in range(model.columnCount()):
            visible = not table.isColumnHidden(column)
            action = menu.addAction(self._column_title(table, column))
            action.setCheckable(True)
            action.setChecked(visible)
            if visible and visible_count <= 1:
                action.setEnabled(False)
            action.toggled.connect(
                lambda checked, current=column, target=table: self.set_column_visible(
                    target, current, checked
                )
            )

        menu.addSeparator()
        show_all = menu.addAction("Mostrar todas as colunas")
        show_all.setEnabled(visible_count < model.columnCount())
        show_all.triggered.connect(
            lambda _checked=False, target=table: self.show_all_columns(target)
        )
        fit_all = menu.addAction("Ajustar larguras ao conteúdo")
        fit_all.triggered.connect(
            lambda _checked=False, target=table: self.resize_columns_to_contents(target)
        )
        menu.addSeparator()
        reset_widths = menu.addAction("Repor larguras padrão")
        reset_widths.triggered.connect(
            lambda _checked=False, target=table: self.reset_column_widths(target)
        )
        reset_all = menu.addAction("Repor organização da tabela")
        reset_all.triggered.connect(
            lambda _checked=False, target=table: self.reset_table_layout(target)
        )
        return menu

    def _show_column_menu(self, table: QTableView, position) -> None:
        if not self._active:
            return
        try:
            header = table.horizontalHeader()
            clicked_column = header.logicalIndexAt(position)
            menu = self._create_column_menu(table, clicked_column)
            menu.exec(header.mapToGlobal(position))
            menu.deleteLater()
        except RuntimeError:
            return

    def set_column_visible(self, table: QTableView, column: int, visible: bool) -> bool:
        """Change one column without ever allowing a completely empty table."""
        try:
            model = table.model()
            if model is None or not (0 <= column < model.columnCount()):
                return False
            currently_visible = not table.isColumnHidden(column)
            visible = bool(visible)
            if currently_visible == visible:
                return True
            if not visible and self._visible_column_count(table) <= 1:
                return False

            # Capture the current width before Qt reports zero for a hidden section.
            state = self._load_state(table)
            if not visible:
                self.save_table(table)
                state = self._load_state(table)

            table.setProperty("columnVisibilityCustomized", True)
            table.setProperty("columnPersistenceRestoring", True)
            try:
                table.setColumnHidden(column, not visible)
                if visible:
                    width = self._saved_width(table, state, column)
                    if width is not None:
                        table.horizontalHeader().resizeSection(column, width)
            finally:
                table.setProperty("columnPersistenceRestoring", False)
            self.save_table(table)
            table.viewport().update()
            return True
        except (RuntimeError, TypeError, ValueError):
            return False

    def show_all_columns(self, table: QTableView) -> None:
        try:
            model = table.model()
            if model is None:
                return
            state = self._load_state(table)
            table.setProperty("columnVisibilityCustomized", True)
            table.setProperty("columnPersistenceRestoring", True)
            try:
                for column in range(model.columnCount()):
                    table.setColumnHidden(column, False)
                    width = self._saved_width(table, state, column)
                    if width is not None:
                        table.horizontalHeader().resizeSection(column, width)
            finally:
                table.setProperty("columnPersistenceRestoring", False)
            self.save_table(table)
            table.viewport().update()
        except (RuntimeError, TypeError, ValueError):
            return

    def resize_columns_to_contents(
        self, table: QTableView, columns: list[int] | None = None
    ) -> None:
        try:
            model = table.model()
            if model is None:
                return
            selected = (
                list(range(model.columnCount()))
                if columns is None
                else [
                    int(column)
                    for column in columns
                    if 0 <= int(column) < model.columnCount()
                ]
            )
            restoring = bool(table.property("columnPersistenceRestoring"))
            table.setProperty("columnPersistenceRestoring", True)
            try:
                header = table.horizontalHeader()
                for column in selected:
                    if table.isColumnHidden(column):
                        continue
                    table.resizeColumnToContents(column)
                    header.resizeSection(
                        column,
                        max(24, min(2_000, int(header.sectionSize(column)))),
                    )
            finally:
                table.setProperty("columnPersistenceRestoring", restoring)
            table.setProperty("columnWidthsCustomized", True)
            self.save_table(table)
            table.viewport().update()
        except (RuntimeError, TypeError, ValueError):
            return

    def reset_column_widths(self, table: QTableView) -> None:
        try:
            defaults = self._defaults.get(table)
            if not defaults:
                self._capture_defaults(table)
                defaults = self._defaults.get(table, {})
            entries = self._column_entries(defaults)
            widths = list(defaults.get("widths", []))
            column_ids = self._column_ids(table)
            restoring = bool(table.property("columnPersistenceRestoring"))
            table.setProperty("columnPersistenceRestoring", True)
            try:
                header = table.horizontalHeader()
                for logical, column_id in enumerate(column_ids):
                    details = entries.get(column_id, {})
                    width = details.get(
                        "width",
                        widths[logical] if logical < len(widths) else None,
                    )
                    if width is not None:
                        header.resizeSection(logical, max(24, min(2_000, int(width))))
            finally:
                table.setProperty("columnPersistenceRestoring", restoring)
            table.setProperty("columnWidthsCustomized", False)
            self.save_table(table)
            table.viewport().update()
        except (RuntimeError, TypeError, ValueError):
            return

    def reset_table_layout(self, table: QTableView) -> None:
        try:
            defaults = self._defaults.get(table)
            if not defaults:
                self._capture_defaults(table)
                defaults = self._defaults.get(table, {})
            restoring = bool(table.property("columnPersistenceRestoring"))
            table.setProperty("columnPersistenceRestoring", True)
            try:
                self._apply_state(table, defaults, force=True)
            finally:
                table.setProperty("columnPersistenceRestoring", restoring)
            table.setProperty("columnWidthsCustomized", False)
            table.setProperty("columnVisibilityCustomized", False)
            table.setProperty("columnOrderCustomized", False)
            table.setProperty("columnSortCustomized", False)
            self.save_table(table)
            table.viewport().update()
        except (RuntimeError, TypeError, ValueError):
            return

    def _connect_model(self, table: QTableView) -> None:
        model = table.model()
        if model is None or self._models.get(table) is model:
            return
        self._models[table] = model
        table_ref = weakref.ref(table)
        timer = self._reapply_timers.get(table)
        if timer is None:
            timer = QTimer(table)
            timer.setSingleShot(True)
            timer.setInterval(0)
            timer.timeout.connect(lambda ref=table_ref: self._reapply_weak(ref))
            self._reapply_timers[table] = timer

        def reapply(*_args) -> None:
            target = table_ref()
            if self._active and self._is_valid_qt_object(target):
                timer.start()

        model.modelReset.connect(reapply)
        model.columnsInserted.connect(reapply)
        model.columnsRemoved.connect(reapply)
        model.headerDataChanged.connect(reapply)
        model.layoutChanged.connect(
            lambda *_args, target=table: self.ensure_interactive(target)
        )
        model.modelAboutToBeReset.connect(
            lambda target=table: self._suspend_sorting_for_population(target)
        )
        model.rowsAboutToBeInserted.connect(
            lambda *_args, target=table: self._suspend_sorting_for_population(target)
        )
        model.rowsAboutToBeRemoved.connect(
            lambda *_args, target=table: self._suspend_sorting_for_population(target)
        )

    def _suspend_sorting_for_population(self, table: QTableView) -> None:
        """Keep QTableWidget rows intact while a page repopulates its cells."""

        if (
            not self._is_valid_qt_object(table)
            or not isinstance(table, QTableWidget)
            or bool(table.property("tablePopulationSortSuspended"))
            or not table.isSortingEnabled()
        ):
            return
        header = table.horizontalHeader()
        self._suspended_sorts[table] = (
            int(header.sortIndicatorSection()),
            header.sortIndicatorOrder(),
        )
        table.setProperty("tablePopulationSortSuspended", True)
        restoring = bool(table.property("columnPersistenceRestoring"))
        table.setProperty("columnPersistenceRestoring", True)
        try:
            table.setSortingEnabled(False)
        finally:
            table.setProperty("columnPersistenceRestoring", restoring)
        table_ref = weakref.ref(table)
        QTimer.singleShot(
            0,
            lambda reference=table_ref: self._resume_sorting_after_population(
                reference
            ),
        )

    def _resume_sorting_after_population(
        self, reference: weakref.ReferenceType[QTableView]
    ) -> None:
        table = reference()
        if not self._is_valid_qt_object(table):
            return
        saved = self._suspended_sorts.pop(table, (-1, Qt.SortOrder.AscendingOrder))
        restoring = bool(table.property("columnPersistenceRestoring"))
        table.setProperty("columnPersistenceRestoring", True)
        try:
            table.setSortingEnabled(True)
            logical, order = saved
            table.sortByColumn(logical, order)
        finally:
            table.setProperty("tablePopulationSortSuspended", False)
            table.setProperty("columnPersistenceRestoring", restoring)
        table.viewport().update()

    def _reapply_weak(self, reference: weakref.ReferenceType[QTableView]) -> None:
        table = reference()
        if self._active and self._is_valid_qt_object(table):
            self._reapply(table)

    def _reapply(self, table: QTableView) -> None:
        try:
            timer = self._timers.get(table)
            if timer is not None and timer.isActive():
                timer.stop()
                self.save_table(table)
            self.ensure_interactive(table)
            self.restore_table(table)
        except RuntimeError:
            return

    def ensure_interactive(self, table: QTableView) -> None:
        """Make every column draggable and every data table sortable."""
        restoring = False
        try:
            model = table.model()
            if model is None:
                return
            restoring = bool(table.property("columnPersistenceRestoring"))
            table.setProperty("columnPersistenceRestoring", True)
            header = table.horizontalHeader()
            header.setStretchLastSection(False)
            header.setSectionsMovable(True)
            if hasattr(header, "setFirstSectionMovable"):
                header.setFirstSectionMovable(True)
            header.setMinimumSectionSize(max(24, header.minimumSectionSize()))
            if isinstance(table, QTableWidget):
                table.setItemPrototype(SmartTableWidgetItem())
            if (
                not bool(table.property("disableTableSorting"))
                and not bool(table.property("tablePopulationSortSuspended"))
                and not bool(table.property("tableSortingInitialization"))
                and not table.isSortingEnabled()
            ):
                table.setProperty("tableSortingInitialization", True)
                try:
                    header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
                    table.setSortingEnabled(True)
                finally:
                    table.setProperty("tableSortingInitialization", False)
            for column in range(model.columnCount()):
                if (
                    header.sectionResizeMode(column)
                    != QHeaderView.ResizeMode.Interactive
                ):
                    header.setSectionResizeMode(
                        column, QHeaderView.ResizeMode.Interactive
                    )
        except RuntimeError:
            return
        finally:
            try:
                table.setProperty("columnPersistenceRestoring", restoring)
            except RuntimeError:
                pass

    def _schedule_save(self, table: QTableView) -> None:
        if not self._is_valid_qt_object(table):
            return
        if bool(table.property("columnPersistenceRestoring")):
            return
        timer = self._timers.get(table)
        if timer is not None:
            timer.start()

    def _column_resized(self, table: QTableView) -> None:
        if not self._is_valid_qt_object(table) or bool(
            table.property("columnPersistenceRestoring")
        ):
            return
        table.setProperty("columnWidthsCustomized", True)
        self._schedule_save(table)

    def _column_moved(self, table: QTableView) -> None:
        if not self._is_valid_qt_object(table) or bool(
            table.property("columnPersistenceRestoring")
        ):
            return
        table.setProperty("columnOrderCustomized", True)
        self._schedule_save(table)

    def _sort_changed(self, table: QTableView) -> None:
        if not self._is_valid_qt_object(table) or bool(
            table.property("columnPersistenceRestoring")
        ):
            return
        table.setProperty("columnSortCustomized", True)
        self._schedule_save(table)

    def restore_table(self, table: QTableView) -> None:
        if not self._active or self.repo is None or not self._is_valid_qt_object(table):
            return
        try:
            model = table.model()
        except RuntimeError:
            return
        if model is None or model.columnCount() <= 0:
            return
        self.ensure_interactive(table)
        state = self._load_state(table)
        if not any(
            key in state for key in ("widths", "hidden", "columns", "order", "sort")
        ):
            return
        version = int(state.get("version") or 1)
        widths_customized = bool(
            state.get(
                "widths_customized",
                version < self.STATE_VERSION and bool(state.get("widths")),
            )
        )
        visibility_customized = bool(
            state.get(
                "visibility_customized",
                version < self.STATE_VERSION and isinstance(state.get("hidden"), list),
            )
        )
        order_customized = bool(state.get("order_customized"))
        sort_customized = bool(state.get("sort_customized"))
        restoring = bool(table.property("columnPersistenceRestoring"))
        table.setProperty("columnPersistenceRestoring", True)
        try:
            self._apply_state(table, state)
            table.setProperty("columnWidthsCustomized", widths_customized)
            table.setProperty("columnVisibilityCustomized", visibility_customized)
            table.setProperty("columnOrderCustomized", order_customized)
            table.setProperty("columnSortCustomized", sort_customized)
        except Exception as exc:
            key = str(table.property("columnPersistenceKey") or self._state_key(table))
            self._report_load_failure(table, key, exc)
            return
        finally:
            if self._is_valid_qt_object(table):
                table.setProperty("columnPersistenceRestoring", restoring)

    def save_table(self, table: QTableView) -> bool:
        if not self._active or self.repo is None or not self._is_valid_qt_object(table):
            return True
        key = str(table.property("columnPersistenceKey") or self._state_key(table))
        try:
            model = table.model()
            if model is None or model.columnCount() <= 0:
                return True
            self.ensure_interactive(table)
            previous_state = self._load_state(table)
            payload = self._snapshot_state(table, previous_state)
            return self._write_state(table, key, payload, operation="save")
        except Exception as exc:
            if self._is_valid_qt_object(table):
                self._report_save_failure(table, key, "collect state", exc)
                return False
            return True

    def flush(self) -> bool:
        if not self._active:
            return True
        failed: list[str] = []
        for table in tuple(self._tables):
            if not self.save_table(table):
                failed.append(self._table_label(table))
        if failed:
            log_warning(
                "Failed to flush table column preferences for %d table(s): %s",
                len(failed),
                ", ".join(sorted(set(failed))),
            )
        return not failed

    def deactivate(self) -> None:
        """Stop callbacks before the owning window or repository is closed."""
        self._active = False
        for timer in tuple(self._timers.values()):
            try:
                timer.stop()
            except RuntimeError:
                pass
        for timer in tuple(self._reapply_timers.values()):
            try:
                timer.stop()
            except RuntimeError:
                pass
        self.repo = None

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if not self._active:
            return super().eventFilter(watched, event)
        if (
            isinstance(watched, QTableView)
            and self._should_manage_table(watched)
            and event.type() == QEvent.Type.Show
        ):
            QTimer.singleShot(0, lambda table=watched: self.register_table(table))
        elif (
            isinstance(watched, QHeaderView) and watched.orientation() == Qt.Horizontal
        ):
            table = self._headers.get(watched)
            if table is not None and event.type() == QEvent.Type.MouseButtonPress:
                self.ensure_interactive(table)
        return super().eventFilter(watched, event)
