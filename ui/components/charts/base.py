import math
import os
import re
from functools import wraps
from typing import Any, Callable, Optional

from PySide6.QtCore import (
    Property,
    QEvent,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Signal,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QCloseEvent, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QBoxLayout,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.components.chart_details import ChartDetailDialog
from ui.components.charts.utils import (
    _chart_bg,
    _chart_grid,
    _chart_muted,
    _chart_text,
    enable_export,
)
from ui.core.theme import Theme
from utils.app_logging import log_exception
from utils.helpers import eur, format_period_variation, safe_float
from utils.qt_runtime import is_qt_valid

RED = "#FF3B30"
_EMAIL_REDACTION_REGEX = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_IBAN_REDACTION_REGEX = re.compile(
    r"(?i)\b(?:PT\d{2}|[A-Z]{2}\d{2})(?:[\s-]?[A-Z0-9]){11,30}\b"
)


class ChartFocusDialog(QDialog):
    """Temporarily enlarge a chart without duplicating its data or state."""

    def __init__(self, chart: "StableChart"):
        owner = chart.window() if chart.parentWidget() is not None else None
        super().__init__(owner)
        self.chart = chart
        self._restored = False
        self._original_parent = chart.parentWidget()
        self._original_layout = (
            self._original_parent.layout()
            if self._original_parent is not None
            else None
        )
        self._original_index = (
            self._original_layout.indexOf(chart)
            if self._original_layout is not None
            else -1
        )
        self._original_alignment = Qt.Alignment()
        self._original_stretch = 0
        self._original_grid_position: tuple[int, int, int, int] | None = None
        if self._original_layout is not None and self._original_index >= 0:
            item = self._original_layout.itemAt(self._original_index)
            if item is not None:
                self._original_alignment = item.alignment()
            if isinstance(self._original_layout, QBoxLayout):
                self._original_stretch = self._original_layout.stretch(
                    self._original_index
                )
            elif isinstance(self._original_layout, QGridLayout):
                self._original_grid_position = self._original_layout.getItemPosition(
                    self._original_index
                )

        self._original_minimum = chart.minimumSize()
        self._original_maximum = chart.maximumSize()
        self._original_size_policy = chart.sizePolicy()
        self._placeholder: QWidget | None = None
        if self._original_parent is not None:
            self._original_parent.installEventFilter(self)
            self._original_parent.destroyed.connect(self._owner_destroyed)

        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setModal(False)
        title = chart.accessibleName().strip() or type(chart).__name__.replace(
            "Chart", ""
        )
        if chart.theme_provider().privacy:
            title = "Gráfico"
        self.setWindowTitle(f"Modo de foco · {title}")
        self.setObjectName("ChartFocusDialog")

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(12)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        heading = QLabel(f"🔎 {title}")
        self._heading_label = heading
        heading.setObjectName("ChartFocusTitle")
        hint = QLabel("Esc para fechar")
        hint.setObjectName("ChartFocusHint")
        close_button = QPushButton("Fechar")
        close_button.setObjectName("Secondary")
        close_button.clicked.connect(self.close)
        header.addWidget(heading)
        header.addStretch(1)
        header.addWidget(hint)
        header.addWidget(close_button)
        root.addLayout(header)

        if self._original_parent is not None:
            self._placeholder = QWidget(self._original_parent)
            self._placeholder.setObjectName("ChartFocusPlaceholder")
            self._placeholder.setMinimumSize(self._original_minimum)
            self._placeholder.setSizePolicy(self._original_size_policy)
            self._placeholder.setFixedHeight(max(chart.height(), chart.minimumHeight()))
            if self._original_layout is not None:
                self._original_layout.removeWidget(chart)
                self._insert_widget(self._placeholder)

        chart.setParent(self)
        chart.setMinimumSize(0, 0)
        chart.setMaximumSize(16_777_215, 16_777_215)
        chart.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(chart, 1)
        chart.show()

        theme = chart.theme_provider()
        surface = "#111827" if theme.dark else "#F8FAFC"
        text = "#F8FAFC" if theme.dark else "#0F172A"
        muted = "#CBD5E1" if theme.dark else "#475569"
        border = "rgba(148, 163, 184, 105)"
        self.setStyleSheet(
            "QDialog#ChartFocusDialog {"
            f"background: {surface}; color: {text};"
            f"border: 1px solid {border};"
            "}"
            "QLabel#ChartFocusTitle {"
            f"color: {text}; font-size: 18px; font-weight: 800;"
            "}"
            "QLabel#ChartFocusHint {"
            f"color: {muted}; font-size: 12px; font-weight: 600;"
            "}"
        )

        screen = chart.screen()
        available = (
            screen.availableGeometry()
            if screen is not None
            else (owner.geometry() if owner is not None else chart.geometry())
        )
        width = min(max(720, round(available.width() * 0.88)), available.width())
        height = min(max(500, round(available.height() * 0.84)), available.height())
        self.resize(width, height)
        self.move(
            available.x() + max(0, (available.width() - width) // 2),
            available.y() + max(0, (available.height() - height) // 2),
        )

    def _insert_widget(self, widget: QWidget) -> None:
        layout = self._original_layout
        if layout is None:
            return
        if isinstance(layout, QGridLayout) and self._original_grid_position is not None:
            row, column, row_span, column_span = self._original_grid_position
            layout.addWidget(
                widget,
                row,
                column,
                row_span,
                column_span,
                self._original_alignment,
            )
        elif isinstance(layout, QBoxLayout):
            layout.insertWidget(
                max(0, self._original_index),
                widget,
                self._original_stretch,
                self._original_alignment,
            )
        else:
            layout.addWidget(widget)

    def refresh_title(self) -> None:
        title = "Gráfico" if self.chart.theme_provider().privacy else (
            self.chart.accessibleName().strip() or type(self.chart).__name__.replace("Chart", "")
        )
        self.setWindowTitle(f"Modo de foco · {title}")
        self._heading_label.setText(f"🔎 {title}")

    def _restore_chart(self) -> None:
        if self._restored:
            return
        self._restored = True
        if not is_qt_valid(self.chart):
            return
        if self._original_parent is not None and not is_qt_valid(self._original_parent):
            # The focus dialog retains ownership until its own deletion.
            self.chart._focus_dialog = None
            return
        try:
            focus_layout = self.layout()
            if focus_layout is not None:
                focus_layout.removeWidget(self.chart)
            self.chart.setParent(self._original_parent)
            self.chart.setMinimumSize(self._original_minimum)
            self.chart.setMaximumSize(self._original_maximum)
            self.chart.setSizePolicy(self._original_size_policy)
            if is_qt_valid(self._placeholder) and is_qt_valid(self._original_layout):
                self._original_layout.removeWidget(self._placeholder)
            self._insert_widget(self.chart)
            self.chart._focus_dialog = None
            self.chart.show()
            self.chart.invalidate_cache()
        except RuntimeError:
            # The page may have been destroyed while the focus window was open.
            pass
        finally:
            if is_qt_valid(self._placeholder):
                self._placeholder.deleteLater()
            self._placeholder = None
            if is_qt_valid(self._original_parent):
                self._original_parent.removeEventFilter(self)
                self._original_parent.destroyed.disconnect(self._owner_destroyed)

    def eventFilter(self, watched, event):
        if watched is self._original_parent and event.type() in (
            QEvent.DeferredDelete, QEvent.Close,
        ):
            # Restore while the original container and its layout still exist.
            self.close()
        return super().eventFilter(watched, event)

    def _owner_destroyed(self, *_args) -> None:
        if not is_qt_valid(self):
            return
        self._restored = True
        if is_qt_valid(self.chart):
            self.chart._focus_dialog = None
        self._placeholder = None
        # Never re-enter QWidget.close while an owning ancestor is destructing.
        # QObject removes this queued deletion if its parent deletes us first.
        self.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self._restore_chart()
        super().closeEvent(event)

    def reject(self) -> None:  # type: ignore[override]
        self._restore_chart()
        super().reject()

    def accept(self) -> None:  # type: ignore[override]
        self._restore_chart()
        super().accept()

    def done(self, r: int) -> None:  # type: ignore[override]
        self._restore_chart()
        super().done(r)


class Background(QWidget):
    def __init__(self, theme: Theme, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.zoom_factor = 1.0
        self.pan_offset = 0.0
        self.theme = theme


class StableChart(QWidget):
    """Cached, animated base widget shared by the custom finance charts."""

    _DEFAULT_DETAIL_FIELD_TYPES = {
        "amount": "money",
        "value": "money",
        "total": "money",
        "actual": "money",
        "current": "money",
        "reference": "money",
        "predicted": "money",
        "delta": "money",
        "projected": "money",
        "projected_optimistic": "money",
        "projected_pessimistic": "money",
        "budget": "money",
        "income": "money",
        "expenses": "money",
        "expense": "money",
        "savings": "money",
        "net": "money",
        "balance": "money",
        "balance_base": "money",
        "balance_optimistic": "money",
        "balance_pessimistic": "money",
        "invested": "money",
        "gain": "money",
        "cost_base": "money",
        "cost_low": "money",
        "cost_high": "money",
        "subscriptions": "money",
        "scheduled": "money",
        "before": "money",
        "after": "money",
        "fees_paid": "money",
        "fee": "money",
        "fees": "money",
        "taxes_paid": "money",
        "tax": "money",
        "taxes": "money",
        "gross_gain": "money",
        "net_gain": "money",
        "gross_amount": "money",
        "net_amount": "money",
        "profit": "money",
        "loss": "money",
        "dividend": "money",
        "dividends": "money",
        "withholding_tax": "money",
        "capital": "money",
        "total_invested": "money",
        "market_value": "money",
        "current_value": "money",
        "initial_value": "money",
        "final_value": "money",
        "contributions": "money",
        "withdrawals": "money",
        "yield_amount": "money",
        "interest": "money",
        "principal": "money",
        "target": "money",
        "target_amount": "money",
        "gap": "money",
        "rate": "percent",
        "savings_rate": "percent",
        "return_pct": "percent",
        "accuracy_pct": "percent",
        "wape_pct": "percent",
        "regularity_pct": "percent",
        "error_pct": "percent",
        "share_pct": "percent",
        "change_pct": "percent",
        "percent": "percent",
        "pct": "percent",
        "coverage_pct": "percent",
        "inflation_rate": "percent",
        "dividend_yield": "percent",
        "fee_pct": "percent",
        "tax_rate": "percent",
        "volatility": "percent",
        "cagr": "percent",
        "allocation_pct": "percent",
        "weight_pct": "percent",
        "event_count": "count",
        "movements": "count",
        "count": "count",
        "score": "number",
        "p10": "money",
        "p50": "money",
        "p90": "money",
        "probability_target": "percent",
    }

    category_hovered = Signal(str)
    period_hovered = Signal(str)
    view_state_changed = Signal(float, float)
    supports_zoom = False
    minimum_zoom = 1.0
    maximum_zoom = 10.0

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name in ("set_data", "set_rows", "set_points", "set_items"):
            setter = cls.__dict__.get(name)
            if setter is None:
                continue

            def wrap_setter(method):
                @wraps(method)
                def replace_data(self, *args, **method_kwargs):
                    result = method(self, *args, **method_kwargs)
                    self._data_replaced()
                    return result
                return replace_data

            setattr(cls, name, wrap_setter(setter))

    def _data_replaced(self) -> None:
        """Discard geometry/selection derived from the previous series."""
        self.clear_period_hover()
        self.highlighted_category = ""
        for name in ("tooltip_rects", "_row_rects", "weekday_label_rects"):
            if hasattr(self, name):
                setattr(self, name, [])
        self._last_detail_payload = {}
        if self._pinned_detail_item:
            candidates = []
            for name in ("items", "points", "rows", "data"):
                values = getattr(self, name, None)
                if isinstance(values, (list, tuple)):
                    candidates.extend(self._normalise_detail_item(value) for value in values)
                elif isinstance(values, dict):
                    candidates.append(self._normalise_detail_item(values))
            if not any(
                all(item.get(key) == value for key, value in self._pinned_detail_item.items())
                for item in candidates
            ):
                self.clear_pinned_detail()
        self._update_pinned_badge()
        if is_qt_valid(self._focus_dialog):
            self._focus_dialog.refresh_title()
        self.invalidate_cache()

    def __init__(
        self, theme_provider: Callable[[], Theme], parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.zoom_factor = 1.0
        self.pan_offset = 0.0
        self.theme_provider = (
            theme_provider
            if callable(theme_provider)
            else (lambda theme=theme_provider: theme)
        )
        self._paint_error = ""
        self._reported_paint_error = ""
        self.highlighted_category = ""
        self.highlighted_period = ""
        self._emitted_period = ""
        self._focus_dialog: ChartFocusDialog | None = None
        self._detail_provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self._calculation_context_provider: (
            Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None
        ) = None
        self._detail_action_handler: Callable[[dict[str, Any]], None] | None = None
        self._detail_action_label = ""
        self._detail_field_types = dict(self._DEFAULT_DETAIL_FIELD_TYPES)
        self._last_detail_payload: dict[str, Any] = {}
        self._pinned_detail_item: dict[str, Any] | None = None
        self._pinned_badge = QLabel(self)
        self._pinned_badge.setObjectName("ChartPinnedBadge")
        self._pinned_badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._pinned_badge.setVisible(False)
        self._pinned_badge.setMaximumWidth(230)
        self._pinned_badge.setFixedHeight(27)
        self._pinned_badge.setAlignment(Qt.AlignCenter)

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setAttribute(Qt.WA_NoSystemBackground, False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self._cached_pixmap: Optional[QPixmap] = None
        self._needs_repaint = True
        self._anim_progress = 1.0
        self.anim: Optional[QPropertyAnimation] = None
        self._chart_action_button = enable_export(self, self.theme_provider)

    @staticmethod
    def _normalised_category(value: object) -> str:
        text = " ".join(str(value or "").casefold().strip().split())
        return text.split(" (", 1)[0] if text.startswith("outros (") else text

    def category_is_highlighted(self, label: object) -> bool:
        selected = self._normalised_category(self.highlighted_category)
        return bool(selected) and self._normalised_category(label) == selected

    def set_highlighted_category(self, category: str) -> None:
        selected = str(category or "").strip()
        if selected == self.highlighted_category:
            return
        self.highlighted_category = selected
        self.invalidate_cache()

    def _period_rows(self) -> list:
        for attribute in ("points", "rows", "data"):
            values = getattr(self, attribute, None)
            if isinstance(values, list):
                return values
        return []

    @staticmethod
    def _period_key_for_row(row: object) -> str:
        if not isinstance(row, dict):
            return ""
        month = str(row.get("month") or "").strip()
        if len(month) >= 7 and month[4:5] == "-":
            return month[:7]
        raw_date = str(row.get("date") or "").strip()
        if len(raw_date) < 7 or raw_date[4:5] != "-":
            return ""
        resolution = str(row.get("resolution") or "").strip().casefold()
        return raw_date[:7] if resolution == "monthly" else raw_date[:10]

    def period_key_for_index(self, index: int | None) -> str:
        rows = self._period_rows()
        if index is None or not 0 <= int(index) < len(rows):
            return ""
        return self._period_key_for_row(rows[int(index)])

    def emit_period_for_hover(self, index: int | None) -> None:
        selected = self.period_key_for_index(index)
        if selected == self._emitted_period:
            return
        self._emitted_period = selected
        self.period_hovered.emit(selected)

    def set_highlighted_period(self, period: str) -> None:
        selected = str(period or "").strip()[:10]
        rows = self._period_rows()
        index: int | None = None
        if selected:
            keys = [self._period_key_for_row(row) for row in rows]
            try:
                index = keys.index(selected)
            except ValueError:
                month = selected[:7]
                matches = [
                    candidate_index
                    for candidate_index, key in enumerate(keys)
                    if key and key[:7] == month
                ]
                if matches:
                    # Monthly selections map to the last available point of that
                    # month in daily/weekly charts; exact dates remain exact.
                    index = matches[-1] if len(selected) == 7 else matches[0]
        if (
            selected == self.highlighted_period
            and getattr(self, "hover_index", None) == index
        ):
            return
        self.highlighted_period = selected
        if hasattr(self, "hover_index"):
            self.hover_index = index
        self.invalidate_cache()

    def clear_period_hover(self) -> None:
        self.highlighted_period = ""
        self._emitted_period = ""
        if hasattr(self, "hover_index"):
            self.hover_index = None

    def open_focus_view(self) -> ChartFocusDialog:
        dialog = self._focus_dialog
        if dialog is not None:
            try:
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                return dialog
            except RuntimeError:
                self._focus_dialog = None
        dialog = ChartFocusDialog(self)
        self._focus_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def configure_details(
        self,
        provider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        *,
        action_label: str = "",
        action_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Configure optional evidence enrichment and a related action."""

        self._detail_provider = provider
        self._detail_action_label = str(action_label or "").strip()
        self._detail_action_handler = action_handler

    def configure_calculation_context(
        self,
        provider: (Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None),
    ) -> None:
        """Attach period, filters, evidence and exclusions to every detail."""

        self._calculation_context_provider = provider

    def set_detail_field_types(self, field_types: dict[str, str]) -> None:
        """Declare how raw detail fields must be rendered and protected."""

        valid = {"money", "percent", "number", "count", "text"}
        for key, field_type in dict(field_types or {}).items():
            normalised = str(field_type or "").strip().casefold()
            if normalised in valid:
                self._detail_field_types[str(key)] = normalised

    def _detail_field_type(self, key: str) -> str:
        return str(self._detail_field_types.get(str(key), "") or "")

    @staticmethod
    def _detail_identity(item: dict[str, Any]) -> tuple[str, ...]:
        keys = (
            "entity_id",
            "id",
            "key",
            "role",
            "source",
            "label",
            "category",
            "name",
            "date",
            "month",
            "year",
            "value",
            "amount",
            "total",
        )
        return tuple(str(item.get(key) or "") for key in keys)

    def _same_detail_item(
        self,
        first: dict[str, Any] | None,
        second: dict[str, Any] | None,
    ) -> bool:
        if not first or not second:
            return False
        return self._detail_identity(first) == self._detail_identity(second)

    @staticmethod
    def _detail_item_label(item: dict[str, Any]) -> str:
        return str(
            item.get("label")
            or item.get("category")
            or item.get("name")
            or item.get("date")
            or item.get("month")
            or item.get("year")
            or "ponto"
        ).strip()

    def _update_pinned_badge(self) -> None:
        pinned = self._pinned_detail_item
        if not pinned:
            self._pinned_badge.hide()
            return
        label = (
            "Dados ocultos" if self.theme_provider().privacy
            else self._detail_item_label(pinned)
        )
        short = label if len(label) <= 28 else f"{label[:25]}…"
        theme = self.theme_provider()
        surface = "#273449" if theme.dark else "#EEF6FF"
        text = "#F8FAFC" if theme.dark else "#0F3E6D"
        border = "#5CAEFF" if theme.dark else "#79B9F5"
        self._pinned_badge.setText(f"📌 Comparar com {short}")
        self._pinned_badge.setStyleSheet(
            f"background:{surface};color:{text};border:1px solid {border};"
            "border-radius:11px;padding:2px 9px;font-weight:700;"
        )
        self._pinned_badge.adjustSize()
        self._pinned_badge.setFixedHeight(27)
        self._pinned_badge.show()
        self._position_pinned_badge()
        self._pinned_badge.raise_()

    def _position_pinned_badge(self) -> None:
        if not self._pinned_badge.isVisible():
            return
        self._pinned_badge.move(
            max(6, self.width() - self._pinned_badge.width() - 8),
            max(6, self.height() - self._pinned_badge.height() - 7),
        )

    def pin_detail_item(self, item: object, *, label: str = "") -> bool:
        normalised = self._normalise_detail_item(item, label)
        if not normalised:
            return False
        self._pinned_detail_item = dict(normalised)
        self._update_pinned_badge()
        self.update()
        return True

    def clear_pinned_detail(self) -> None:
        self._pinned_detail_item = None
        self._update_pinned_badge()
        self.update()

    def _apply_pinned_comparison(
        self,
        item: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        pinned = self._pinned_detail_item
        if not pinned:
            return payload
        is_privacy = bool(getattr(self.theme_provider(), "privacy", False))
        pinned_label = "Dados ocultos" if is_privacy else self._detail_item_label(pinned)
        if self._same_detail_item(item, pinned):
            result = dict(payload)
            result["explanation"] = (
                str(result.get("explanation") or "")
                + " Este é o ponto atualmente fixado para comparação."
            ).strip()
            result["comparison"] = {
                "pinned_label": pinned_label,
                "same_item": True,
                "metrics": [],
            }
            return result

        preferred = (
            "value",
            "amount",
            "total",
            "actual",
            "predicted",
            "projected",
            "budget",
            "income",
            "expenses",
            "savings",
            "net",
            "balance",
            "invested",
            "gain",
            "p50",
            "probability_target",
            "subscriptions",
            "scheduled",
            "accuracy_pct",
            "return_pct",
            "score",
            "event_count",
            "movements",
            "count",
        )
        common = [
            key
            for key in preferred
            if isinstance(item.get(key), (int, float))
            and isinstance(pinned.get(key), (int, float))
        ]
        comparison_metrics: list[dict[str, Any]] = []
        raw_metrics: list[dict[str, Any]] = []
        is_privacy = bool(getattr(self.theme_provider(), "privacy", False))
        for key in common[:4]:
            current = safe_float(item.get(key))
            baseline = safe_float(pinned.get(key))
            difference = current - baseline
            field_type = self._detail_field_type(key)
            if is_privacy and field_type == "percent":
                difference_text = "••• p.p."
            elif is_privacy and field_type != "money":
                difference_text = "•••"
            elif field_type == "money":
                difference_text = self._format_detail_value(key, difference)
            elif field_type == "percent":
                difference_text = f"{difference:+.1f} p.p.".replace(".", ",")
            else:
                difference_text = f"{difference:+.2f}".replace(".", ",")
            relative = difference / abs(baseline) * 100.0 if baseline else None
            display = difference_text
            if relative is not None and field_type != "percent":
                if is_privacy:
                    display += " · •••%"
                else:
                    display += f" · {relative:+.1f}%".replace(".", ",")
            comparison_metrics.append(
                {
                    "label": f"Δ {self._detail_field_label(key)}",
                    "value": display,
                    "tone": "info",
                    "sensitive": True,
                }
            )
            raw_metrics.append(
                {
                    "key": key,
                    "pinned": baseline,
                    "current": current,
                    "delta": difference,
                    "change_pct": relative,
                }
            )

        result = dict(payload)
        subtitle = str(result.get("subtitle") or "").strip()
        comparison_subtitle = f"comparado com {pinned_label}"
        result["subtitle"] = (
            f"{subtitle} · {comparison_subtitle}" if subtitle else comparison_subtitle
        )
        result["metrics"] = list(result.get("metrics") or []) + comparison_metrics
        result["comparison"] = {
            "pinned_label": pinned_label,
            "same_item": False,
            "metrics": raw_metrics,
        }
        return result

    @staticmethod
    def _normalise_detail_item(item: object, label: str = "") -> dict[str, Any]:
        if isinstance(item, dict):
            result = dict(item)
        elif isinstance(item, (tuple, list)):
            result = {}
            if item:
                result["label"] = item[0]
            if len(item) > 1:
                result["value"] = item[1]
            if len(item) > 2:
                result["extra"] = item[2]
        elif item is None:
            result = {}
        else:
            result = {"value": item}
        if label and not result.get("label"):
            result["label"] = label
        return result

    @staticmethod
    def _detail_field_label(key: str) -> str:
        labels = {
            "label": "Seleção",
            "category": "Categoria",
            "date": "Data",
            "month": "Mês",
            "year": "Ano",
            "amount": "Valor",
            "value": "Valor",
            "total": "Total",
            "actual": "Realizado",
            "current": "Período atual",
            "reference": "Período anterior",
            "predicted": "Previsto",
            "projected": "Projeção",
            "projected_optimistic": "Projeção otimista",
            "projected_pessimistic": "Projeção pessimista",
            "budget": "Orçamento",
            "income": "Entradas",
            "expenses": "Saídas",
            "expense": "Saídas",
            "savings": "Poupança",
            "net": "Saldo",
            "balance": "Saldo",
            "balance_base": "Cenário base",
            "balance_optimistic": "Otimista",
            "balance_pessimistic": "Pessimista",
            "invested": "Investido",
            "gain": "Ganho",
            "rate": "Taxa",
            "savings_rate": "Taxa de poupança",
            "return_pct": "Rentabilidade",
            "accuracy_pct": "Cobertura da faixa",
            "change_pct": "Variação",
            "error_pct": "Erro",
            "wape_pct": "Erro ponderado",
            "regularity_pct": "Regularidade",
            "subscriptions": "Subscrições",
            "scheduled": "Pagamentos agendados",
            "before": "Antes",
            "after": "Depois",
            "delta": "Diferença",
            "event_count": "Eventos",
            "movements": "Movimentos",
            "p10": "Percentil 10",
            "p50": "Mediana",
            "p90": "Percentil 90",
            "probability_target": "Probabilidade do objetivo",
            "base": "Cenário base",
            "optimistic": "Cenário otimista",
            "stressed": "Cenário de stress",
            "status": "Estado",
            "fees_paid": "Comissões pagas",
            "taxes_paid": "Impostos pagos",
            "gross_gain": "Mais-valia bruta",
            "net_gain": "Mais-valia líquida",
            "profit": "Lucro",
            "loss": "Prejuízo",
            "dividend": "Dividendo",
            "dividends": "Dividendos",
            "withholding_tax": "Retenção na fonte",
            "capital": "Capital",
            "total_invested": "Total investido",
            "market_value": "Valor de mercado",
            "initial_value": "Valor inicial",
            "final_value": "Valor final",
            "contributions": "Reforços",
            "withdrawals": "Levantamentos",
            "yield_amount": "Rendimento",
            "interest": "Juros",
            "target": "Objetivo",
            "gap": "Diferença para objetivo",
        }
        return labels.get(key, key.replace("_", " ").strip().capitalize())

    def _format_detail_value(self, key: str, value: object) -> str:
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "Sim" if value else "Não"
        is_privacy = bool(self.theme_provider().privacy)
        field_type = self._detail_field_type(key)
        if field_type == "money" and isinstance(value, (int, float)):
            return eur(float(value), is_privacy)
        if field_type == "percent" and isinstance(value, (int, float)):
            if is_privacy and key in {
                "savings_rate", "return_pct", "accuracy_pct", "wape_pct",
                "regularity_pct", "error_pct", "share_pct", "change_pct",
                "coverage_pct", "cagr", "allocation_pct", "weight_pct",
            }:
                return "•••%"
            return f"{safe_float(value):.1f}%".replace(".", ",")
        if field_type == "count" and isinstance(value, (int, float)):
            return "•••" if is_privacy else str(int(round(safe_float(value))))
        if field_type == "number" and isinstance(value, (int, float)):
            if isinstance(value, int):
                return str(value)
            return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
        # Fail-closed for unknown numeric values under privacy mode
        if isinstance(value, (int, float)):
            if is_privacy:
                return eur(float(value), True)
            if isinstance(value, int):
                return str(value)
            return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
        text = str(value).strip()
        if not text:
            return "—"
        if is_privacy:
            text = _EMAIL_REDACTION_REGEX.sub("[EMAIL]", text)
            text = _IBAN_REDACTION_REGEX.sub("[IBAN]", text)
        return text

    def _default_detail_payload(self, item: dict[str, Any]) -> dict[str, Any]:
        title = self.accessibleName().strip() or type(self).__name__.replace(
            "Chart", ""
        )
        subtitle = str(
            item.get("label")
            or item.get("category")
            or item.get("date")
            or item.get("month")
            or item.get("year")
            or ""
        )
        ignored = {
            "color",
            "colour",
            "extra",
            "source_id",
            "recorded",
            "overdue",
            "is_forecast",
            "partial",
            "resolution",
            "kind",
            "source",
            "name",
            "description",
            "label",
            "category",
            "date",
            "date_str",
            "month",
            "year",
        }
        preferred_order = (
            "amount",
            "value",
            "actual",
            "current",
            "reference",
            "predicted",
            "projected",
            "projected_optimistic",
            "projected_pessimistic",
            "budget",
            "income",
            "expenses",
            "subscriptions",
            "scheduled",
            "savings",
            "net",
            "balance",
            "balance_base",
            "balance_optimistic",
            "balance_pessimistic",
            "p10",
            "p50",
            "p90",
            "probability_target",
            "base",
            "optimistic",
            "stressed",
            "savings_rate",
            "return_pct",
            "accuracy_pct",
            "event_count",
            "movements",
            "status",
        )
        keys = [key for key in preferred_order if key in item]
        keys.extend(
            key
            for key, value in item.items()
            if key not in ignored
            and key not in keys
            and not isinstance(value, (dict, list, tuple, set))
        )
        variation = None
        if "current" in item and "reference" in item:
            variation = format_period_variation(
                safe_float(item.get("current")),
                safe_float(item.get("reference")),
                privacy=self.theme_provider().privacy,
            )

        def display_value(key: str) -> str:
            if key == "change_pct" and variation is not None:
                text = str(variation["display"])
                return f"{text} · {variation['context']}" if variation["huge"] else text
            return self._format_detail_value(key, item.get(key))

        metrics = [
            {
                "label": self._detail_field_label(key),
                "value": display_value(key),
                "tone": (
                    "positive"
                    if key in {"income", "savings", "gain"}
                    and safe_float(item.get(key)) >= 0
                    else "negative"
                    if key in {"expenses", "expense", "subscriptions", "scheduled"}
                    else "neutral"
                ),
            }
            for key in keys[:8]
        ]
        return {
            "icon": "🔎",
            "title": title,
            "subtitle": subtitle,
            "explanation": (
                "Valores exatos do ponto selecionado. Quando existem movimentos "
                "associados, são apresentados abaixo como evidência. "
                + (
                    str(variation["explanation"])
                    if variation is not None and variation["huge"]
                    else ""
                )
            ),
            "metrics": metrics,
        }

    def _current_detail_item(self) -> dict[str, Any] | None:
        index = getattr(self, "hover_index", None)
        if index is not None:
            for attribute in ("rows", "points", "items", "series", "data"):
                source = getattr(self, attribute, None)
                if isinstance(source, (list, tuple)) and 0 <= int(index) < len(source):
                    return self._normalise_detail_item(source[int(index)])
        selected_label = str(getattr(self, "hovered_label", "") or "").strip()
        if selected_label:
            source = getattr(self, "data", None)
            if isinstance(source, dict) and selected_label in source:
                return self._normalise_detail_item(
                    {"label": selected_label, "value": source[selected_label]}
                )
            if isinstance(source, (list, tuple)):
                for value in source:
                    normalised = self._normalise_detail_item(value)
                    candidate = str(
                        normalised.get("label") or normalised.get("category") or ""
                    )
                    if candidate == selected_label:
                        return normalised
            return {"label": selected_label}
        return None

    def _representative_detail_item(self) -> dict[str, Any] | None:
        """Return a useful fallback when the user clicks outside a painted item."""

        source = getattr(self, "data", None)
        if isinstance(source, dict) and source:
            numeric_items = [
                entry for entry in source.items() if isinstance(entry[1], (int, float))
            ]
            if numeric_items:
                label, value = max(
                    numeric_items,
                    key=lambda entry: abs(safe_float(entry[1])),
                )
                return self._normalise_detail_item({"label": label, "value": value})
            return self._normalise_detail_item(source)
        for attribute in ("rows", "points", "items", "series", "data"):
            values = getattr(self, attribute, None)
            if not isinstance(values, (list, tuple)) or not values:
                continue
            normalised = [self._normalise_detail_item(value) for value in values]
            normalised = [value for value in normalised if value]
            if not normalised:
                continue
            # Time-series charts are most useful at their latest point.  For
            # categorical charts, open the largest visible contribution.
            if any(
                value.get("date") or value.get("month") or value.get("year")
                for value in normalised
            ):
                return normalised[-1]
            return max(
                normalised,
                key=lambda value: abs(
                    safe_float(
                        value.get("value", value.get("amount", value.get("total")))
                    )
                ),
            )
        return None

    def has_detail_data(self) -> bool:
        return (
            self._current_detail_item() is not None
            or self._representative_detail_item() is not None
        )

    def open_selected_detail(self) -> bool:
        """Open detail for the hovered point or a representative visible point."""

        item = self._current_detail_item() or self._representative_detail_item()
        return bool(item and self.open_detail(item))

    def detail_payload(self, item: object, *, label: str = "") -> dict[str, Any]:
        normalised = self._normalise_detail_item(item, label)
        payload = self._default_detail_payload(normalised)
        if self._detail_provider is not None:
            try:
                enriched = self._detail_provider(dict(normalised))
                if enriched:
                    payload.update(dict(enriched))
            except Exception as exc:
                log_exception(
                    f"Chart detail enrichment failed [{type(self).__name__}]",
                    exc,
                )
        if self._calculation_context_provider is not None:
            try:
                calculation = self._calculation_context_provider(
                    dict(normalised),
                    dict(payload),
                )
                if calculation:
                    existing = dict(payload.get("calculation") or {})
                    existing.update(dict(calculation))
                    payload["calculation"] = existing
            except Exception as exc:
                log_exception(
                    f"Chart calculation context failed [{type(self).__name__}]",
                    exc,
                )
        return self._apply_pinned_comparison(normalised, payload)

    def open_detail(
        self,
        item: object,
        *,
        label: str = "",
        action_label: str = "",
    ) -> bool:
        normalised = self._normalise_detail_item(item, label)
        if not normalised:
            return False
        payload = self.detail_payload(normalised)
        self._last_detail_payload = dict(payload)
        selected_action = str(
            action_label
            or self._detail_action_label
            or payload.get("action_label")
            or ""
        ).strip()
        if os.environ.get("FINANCAS_PESSOAIS_TESTING", "").strip() == "1":
            if self._detail_action_handler is not None:
                self._detail_action_handler(dict(normalised))
            return True
        dialog = ChartDetailDialog(
            payload,
            self.theme_provider(),
            action_label=selected_action,
            pin_label=(
                "Desafixar comparação"
                if self._same_detail_item(
                    normalised,
                    self._pinned_detail_item,
                )
                else "Fixar para comparar"
            ),
            parent=self.window(),
        )
        accepted = dialog.exec() == QDialog.Accepted
        if dialog.pin_requested:
            if self._same_detail_item(normalised, self._pinned_detail_item):
                self.clear_pinned_detail()
            else:
                self.pin_detail_item(normalised)
            return False
        if accepted and self._detail_action_handler is not None:
            try:
                self._detail_action_handler(dict(normalised))
            except Exception as exc:
                log_exception(
                    f"Chart detail action failed [{type(self).__name__}]",
                    exc,
                )
        return accepted

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            if self.open_selected_detail():
                event.accept()
                return
        super().mousePressEvent(event)

    def _keyboard_labels(self) -> list[str]:
        source = getattr(self, "data", None)
        if isinstance(source, dict):
            return [str(label) for label in source]
        for attribute in ("data", "items", "rows", "points"):
            values = getattr(self, attribute, None)
            if not isinstance(values, list) or not values:
                continue
            labels: list[str] = []
            for index, value in enumerate(values):
                if isinstance(value, (tuple, list)) and value:
                    labels.append(str(value[0]))
                elif isinstance(value, dict):
                    labels.append(
                        str(
                            value.get("label")
                            or value.get("category")
                            or value.get("date")
                            or value.get("year")
                            or index + 1
                        )
                    )
                else:
                    labels.append(str(index + 1))
            return labels
        return []

    def _set_keyboard_selection(self, index: int | None) -> None:
        labels = self._keyboard_labels()
        if index is not None and labels:
            index = max(0, min(len(labels) - 1, int(index)))
        if hasattr(self, "hover_index"):
            self.hover_index = index
        if hasattr(self, "hovered_label"):
            self.hovered_label = labels[index] if index is not None and labels else None
        self.emit_period_for_hover(index)
        self.invalidate_cache()

    def keyPressEvent(self, event):  # type: ignore[override]
        labels = self._keyboard_labels()
        if (
            event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Home, Qt.Key_End)
            and labels
        ):
            current = getattr(self, "hover_index", None)
            if current is None and hasattr(self, "hovered_label"):
                try:
                    current = labels.index(str(self.hovered_label))
                except (ValueError, TypeError):
                    current = None
            if event.key() == Qt.Key_Home:
                selected = 0
            elif event.key() == Qt.Key_End:
                selected = len(labels) - 1
            elif event.key() == Qt.Key_Left:
                selected = 0 if current is None else max(0, int(current) - 1)
            else:
                selected = (
                    0 if current is None else min(len(labels) - 1, int(current) + 1)
                )
            self._set_keyboard_selection(selected)
            event.accept()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            selected = getattr(self, "hover_index", None)
            if selected is None and hasattr(self, "hovered_label") and labels:
                try:
                    selected = labels.index(str(self.hovered_label))
                except (ValueError, TypeError):
                    selected = None

            # Category charts preserve existing signal behavior
            if selected is not None and hasattr(self, "category_clicked") and labels:
                self.category_clicked.emit(labels[int(selected)])
                event.accept()
                return

            # Period/detail charts (Balance, Cashflow, Heatmap, Savings, etc.) open detail dialog
            if self.has_detail_data():
                self.open_selected_detail()
                event.accept()
                return

            if hasattr(self, "category_clicked") and labels and selected is not None:
                self.category_clicked.emit(labels[int(selected)])
                event.accept()
                return

        if event.key() == Qt.Key_Escape:
            if self._pinned_detail_item:
                self.clear_pinned_detail()
            self._set_keyboard_selection(None)
            event.accept()
            return
        super().keyPressEvent(event)

    @Property(float)
    def anim_progress(self):
        return self._anim_progress

    @anim_progress.setter
    def anim_progress(self, value):
        self._anim_progress = max(0.0, min(1.0, float(value)))
        self.invalidate_cache()

    def start_entrance_animation(self, duration_ms: int = 800):
        if self.anim is not None:
            self.anim.stop()
            self.anim.deleteLater()
        self._anim_progress = 0.0
        self.anim = QPropertyAnimation(self, b"anim_progress", self)
        self.anim.setDuration(max(0, int(duration_ms)))
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.start()

    def zoom_viewport_width(self) -> float:
        return max(1.0, float(self.width()))

    def zoom_anchor_x(self, event) -> float:
        return max(0.0, min(self.zoom_viewport_width(), float(event.position().x())))

    def _max_pan_offset(self, zoom: Optional[float] = None) -> float:
        factor = self.zoom_factor if zoom is None else float(zoom)
        return max(0.0, self.zoom_viewport_width() * (factor - 1.0))

    def _clamp_pan(self) -> None:
        self.pan_offset = max(0.0, min(float(self.pan_offset), self._max_pan_offset()))

    def _clear_hover_after_view_change(self) -> None:
        if hasattr(self, "hover_index"):
            self.hover_index = None
            self.highlighted_period = ""
            self.emit_period_for_hover(None)

    @staticmethod
    def _wheel_delta(event, horizontal: bool = False) -> float:
        angle = event.angleDelta()
        pixel = event.pixelDelta()
        if horizontal:
            return float(angle.x() or pixel.x() or angle.y() or pixel.y())
        return float(angle.y() or pixel.y() or angle.x() or pixel.x())

    def wheelEvent(self, event):  # type: ignore[override]
        """Zoom only on Ctrl; pan only horizontally/with Shift.

        A regular vertical wheel is deliberately ignored so the containing
        QScrollArea continues to scroll naturally.
        """
        if not bool(getattr(self, "supports_zoom", False)):
            event.ignore()
            return

        modifiers = event.modifiers()
        if modifiers & Qt.ControlModifier:
            delta = self._wheel_delta(event)
            if math.isclose(delta, 0.0):
                event.ignore()
                return
            old_zoom = float(self.zoom_factor)
            step = math.pow(1.16, delta / 120.0)
            new_zoom = max(
                float(self.minimum_zoom),
                min(float(self.maximum_zoom), old_zoom * step),
            )
            anchor = self.zoom_anchor_x(event)
            if not math.isclose(old_zoom, new_zoom):
                content_at_anchor = self.pan_offset + anchor
                self.zoom_factor = new_zoom
                self.pan_offset = content_at_anchor * (new_zoom / old_zoom) - anchor
                self._clamp_pan()
                self._clear_hover_after_view_change()
                self.invalidate_cache()
                self.view_state_changed.emit(self.zoom_factor, self.pan_offset)
            event.accept()
            return

        horizontal_delta = float(event.angleDelta().x() or event.pixelDelta().x())
        wants_pan = bool(modifiers & Qt.ShiftModifier) or not math.isclose(
            horizontal_delta, 0.0
        )
        if wants_pan and self.zoom_factor > self.minimum_zoom:
            delta = self._wheel_delta(event, horizontal=True)
            self.pan_offset -= delta
            self._clamp_pan()
            self._clear_hover_after_view_change()
            self.invalidate_cache()
            self.view_state_changed.emit(self.zoom_factor, self.pan_offset)
            event.accept()
            return

        event.ignore()

    def reset_zoom(self) -> None:
        if math.isclose(self.zoom_factor, self.minimum_zoom) and math.isclose(
            self.pan_offset, 0.0
        ):
            return
        self.zoom_factor = float(self.minimum_zoom)
        self.pan_offset = 0.0
        self._clear_hover_after_view_change()
        self.invalidate_cache()
        self.view_state_changed.emit(self.zoom_factor, self.pan_offset)

    def mouseDoubleClickEvent(self, event):  # type: ignore[override]
        if (
            bool(getattr(self, "supports_zoom", False))
            and event.button() == Qt.LeftButton
        ):
            self.reset_zoom()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def invalidate_cache(self):
        self._needs_repaint = True
        self.update()

    def _has_animation_data(self) -> bool:
        return any(
            bool(getattr(self, attribute, None))
            for attribute in ("data", "points", "items", "rows")
        )

    def _data_item_count(self) -> int:
        for attribute in ("data", "points", "items", "rows"):
            value = getattr(self, attribute, None)
            if isinstance(value, (list, tuple, dict)):
                return len(value)
        return 0

    def _animate_if_visible(self) -> None:
        if self.isVisible() and self._has_animation_data():
            self.start_entrance_animation(700)

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        self._cached_pixmap = None
        self.invalidate_cache()
        if self._has_animation_data() and not getattr(
            self, "_has_animated_once", False
        ):
            self._has_animated_once = True
            QTimer.singleShot(50, self._animate_if_visible)

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        if self._chart_action_button is not None:
            self._chart_action_button.move(max(4, self.width() - 36), 6)
            self._chart_action_button.raise_()
        self._position_pinned_badge()
        self._cached_pixmap = None
        self._clamp_pan()
        self.invalidate_cache()

    def paintEvent(self, event):  # type: ignore[override]
        if self.width() <= 0 or self.height() <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.TextAntialiasing)
        ratio = self.devicePixelRatioF()
        pixel_width = int(math.ceil(self.width() * ratio))
        pixel_height = int(math.ceil(self.height() * ratio))
        target_pixel_size = QSize(pixel_width, pixel_height)

        cache_valid = (
            not self._needs_repaint
            and self._cached_pixmap is not None
            and not self._cached_pixmap.isNull()
            and self._cached_pixmap.size() == target_pixel_size
            and math.isclose(
                self._cached_pixmap.devicePixelRatio(), ratio, abs_tol=1e-3
            )
        )

        if not cache_valid:
            if pixel_width < 50 or pixel_height < 50:
                painter.end()
                return
            if (
                self._cached_pixmap is None
                or self._cached_pixmap.size() != target_pixel_size
                or not math.isclose(
                    self._cached_pixmap.devicePixelRatio(), ratio, abs_tol=1e-3
                )
            ):
                self._cached_pixmap = QPixmap(pixel_width, pixel_height)
                self._cached_pixmap.setDevicePixelRatio(ratio)

            self._cached_pixmap.fill(Qt.transparent)

            cache_painter = QPainter(self._cached_pixmap)
            cache_painter.setRenderHint(QPainter.Antialiasing)
            cache_painter.setRenderHint(QPainter.SmoothPixmapTransform)
            cache_painter.setRenderHint(QPainter.TextAntialiasing)
            try:
                self._paint_error = ""
                self.render_chart(cache_painter)
                self._reported_paint_error = ""
            except Exception as exc:
                self._paint_error = str(exc)
                signature = f"{type(exc).__name__}:{exc}"
                if signature != self._reported_paint_error:
                    self._reported_paint_error = signature
                    log_exception(
                        "Chart rendering failed "
                        f"[chart={type(self).__name__}, "
                        f"size={self.width()}x{self.height()}, "
                        f"items={self._data_item_count()}]",
                        exc,
                    )
                self.draw_error(cache_painter, exc)
            finally:
                if cache_painter.isActive():
                    cache_painter.end()
            self._needs_repaint = False

        if self._cached_pixmap is not None and not self._cached_pixmap.isNull():
            painter.drawPixmap(0, 0, self._cached_pixmap)
        painter.end()

    def render_chart(self, painter: QPainter):
        """Subclasses override this method to perform their drawing."""

    def draw_empty(self, painter: QPainter, message: str) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        center = self.rect().center()
        badge = QRectF(center.x() - 18, center.y() - 46, 36, 36)
        painter.setPen(Qt.NoPen)
        painter.setBrush(_chart_grid(theme))
        painter.drawEllipse(badge)
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 17, QFont.Bold))
        painter.drawText(badge, Qt.AlignCenter, "—")

        title_rect = QRectF(12, center.y() - 3, max(1, self.width() - 24), 24)
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 10, QFont.Bold))
        painter.drawText(title_rect, Qt.AlignHCenter | Qt.AlignTop, message)

        detail_rect = QRectF(18, center.y() + 23, max(1, self.width() - 36), 34)
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(
            detail_rect,
            Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
            "Os dados aparecerão aqui assim que estiverem disponíveis.",
        )

    def draw_error(self, painter: QPainter, exc: Exception) -> None:
        theme = self.theme_provider()
        painter.fillRect(self.rect(), _chart_bg(theme))
        center = self.rect().center()
        badge = QRectF(center.x() - 18, center.y() - 46, 36, 36)
        painter.setPen(Qt.NoPen)
        error_color = QColor("#FF6961" if theme.dark else RED)
        badge_bg = QColor(error_color)
        badge_bg.setAlpha(40)
        painter.setBrush(badge_bg)
        painter.drawEllipse(badge)
        painter.setPen(error_color)
        painter.setFont(QFont("Segoe UI", 16, QFont.Bold))
        painter.drawText(badge, Qt.AlignCenter, "!")

        title_rect = QRectF(12, center.y() - 3, max(1, self.width() - 24), 24)
        painter.setPen(_chart_text(theme))
        painter.setFont(QFont("Segoe UI", 10, QFont.Bold))
        painter.drawText(
            title_rect, Qt.AlignHCenter | Qt.AlignTop, "Não foi possível apresentar o gráfico"
        )

        detail_rect = QRectF(18, center.y() + 23, max(1, self.width() - 36), 34)
        painter.setPen(_chart_muted(theme))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(
            detail_rect,
            Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
            "Ocorreu um erro interno de processamento. Os detalhes foram registados no log.",
        )
