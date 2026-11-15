#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import json
import inspect
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any

from utils.app_logging import log_exception
from utils.qt_runtime import configure_qt_runtime, is_qt_valid
from .responsive import ResponsivePolicy
from .read_scope import settings_read_scope, read_setting, invalidate_setting_read

configure_qt_runtime()

if TYPE_CHECKING:
    from main import LiquidFinanceApp

try:
    from PySide6.QtCore import QDate, Qt, QTimer
    from PySide6.QtWidgets import (
        QAbstractButton,
        QButtonGroup,
        QComboBox,
        QDateEdit,
        QDoubleSpinBox,
        QHBoxLayout,
        QGridLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QSizePolicy,
        QSlider,
        QSpinBox,
        QTableView,
        QVBoxLayout,
        QWidget,
    )
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

_is_qt_object_valid = is_qt_valid


class BasePage(QWidget):
    _refresh_completes_asynchronously = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Track freshness for every synchronous page refresh automatically."""

        super().__init_subclass__(**kwargs)
        refresh = cls.__dict__.get("refresh")
        if not callable(refresh) or getattr(refresh, "_freshness_tracked", False):
            return
        parameters = list(inspect.signature(refresh).parameters.values())[1:]
        accepts_extra_args = any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters
        )
        positional_limit = sum(
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            for parameter in parameters
        )

        @wraps(refresh)
        def tracked_refresh(self, *args: Any, **refresh_kwargs: Any):
            with settings_read_scope():
                self.begin_data_refresh()
                try:
                    forwarded_args = args if accepts_extra_args else args[:positional_limit]
                    result = refresh(self, *forwarded_args, **refresh_kwargs)
                except Exception:
                    self.mark_data_refresh_failed()
                    raise
                if not bool(getattr(self, "_refresh_completes_asynchronously", False)):
                    self.mark_data_updated()
                return result

        tracked_refresh._freshness_tracked = True  # type: ignore[attr-defined]
        setattr(cls, "refresh", tracked_refresh)

    def __init__(self, app: "LiquidFinanceApp", title: str, subtitle: str = ""):
        super().__init__()
        self.page_title = str(title or self.__class__.__name__)
        self.setObjectName("Page")
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.app = app
        self.repo = app.repo
        self.analytics_service = app.analytics_service
        self.payments_service = app.payments_service
        self.import_service = app.import_service
        self._responsive_grids: list[tuple[QGridLayout, int, int]] = []
        self._responsive_reflow_timer = QTimer(self)
        self._responsive_reflow_timer.setSingleShot(True)
        self._responsive_reflow_timer.setInterval(24)
        self._responsive_reflow_timer.timeout.connect(self.reflow_responsive_grids)
        self._primary_filter_panel: QWidget | None = None
        self._filter_panel_expander = None
        self._view_state_restored = False
        self.page_density = str(
            self.repo.setting(self._density_setting_key(), "comfortable")
            or "comfortable"
        )
        if self.page_density not in {"compact", "comfortable"}:
            self.page_density = "comfortable"
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        root.setAlignment(Qt.AlignTop)
        self.header = QHBoxLayout()
        self.header.setContentsMargins(0, 0, 0, 0)
        names = QVBoxLayout()
        names.setContentsMargins(0, 0, 0, 0)
        names.setSpacing(1)
        t = QLabel(title)
        t.setObjectName("Title")
        t.setWordWrap(True)
        t.setMinimumWidth(0)
        t.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        t.setToolTip(str(title or ""))
        s = QLabel(subtitle)
        s.setObjectName("Subtle")
        s.setWordWrap(True)
        s.setMinimumWidth(0)
        s.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        s.setToolTip(str(subtitle or ""))
        s.setVisible(bool(subtitle))
        self.page_title_label = t
        self.page_subtitle_label = s
        names.addWidget(t)
        names.addWidget(s)
        self.data_updated_label = QLabel("Dados ainda não atualizados nesta página")
        self.data_updated_label.setObjectName("PageDataFreshness")
        self.data_updated_label.setProperty("state", "stale")
        self.data_updated_label.setAccessibleName("Atualidade dos dados da página")
        self.data_updated_label.setWordWrap(True)
        self.data_updated_label.setMinimumWidth(0)
        self.data_updated_label.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        names.addWidget(self.data_updated_label)
        self._data_updated_at: datetime | None = None
        self.header.addLayout(names, 1)
        self.density_btn = QPushButton()
        self.density_btn.setObjectName("Secondary")
        self.density_btn.setAccessibleName("Densidade visual da página")
        self.density_btn.setToolTip(
            "Alternar entre espaçamento confortável e compacto nesta página"
        )
        self.density_btn.clicked.connect(self.toggle_page_density)
        self.header.addWidget(self.density_btn)
        self._update_density_button()
        self.reset_view_btn = QPushButton("↺ Repor")
        self.reset_view_btn.setObjectName("Secondary")
        self.reset_view_btn.setToolTip(
            "Repor zoom, filtros e comparações fixadas desta página"
        )
        self.reset_view_btn.clicked.connect(self.reset_page_view)
        self.header.addWidget(self.reset_view_btn)
        self.customize_cards_btn = QPushButton("🧩 Cards")
        self.customize_cards_btn.setObjectName("Secondary")
        self.customize_cards_btn.setToolTip(
            "Mostrar, esconder ou desativar cards desta página"
        )
        self.customize_cards_btn.clicked.connect(self.open_card_customization)
        self.header.addWidget(self.customize_cards_btn)
        root.addLayout(self.header, 0)
        self.workspace_host = QWidget()
        self.workspace_host.setObjectName("WorkspaceHost")
        self.workspace_host_layout = QVBoxLayout(self.workspace_host)
        self.workspace_host_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_host_layout.setSpacing(0)
        self.workspace_host.setVisible(False)
        self._workspace_switcher = None
        root.addWidget(self.workspace_host, 0)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        self.body.setAlignment(Qt.AlignTop)
        root.addLayout(self.body, 0)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        if widget is None or not _is_qt_object_valid(widget):
            return
        try:
            style = widget.style()
            if style is not None:
                style.unpolish(widget)
                style.polish(widget)
            widget.update()
        except RuntimeError:
            pass

    def begin_data_refresh(self) -> None:
        label = getattr(self, "data_updated_label", None)
        if label is not None and _is_qt_object_valid(label):
            try:
                label.setText("⏳ A atualizar os dados…")
                label.setProperty("state", "loading")
                self._repolish(label)
            except RuntimeError:
                pass

    def mark_data_updated(self, moment: datetime | None = None) -> None:
        """Expose the last completed page read instead of a transient status bar."""

        updated = moment or datetime.now()
        self._data_updated_at = updated
        label = getattr(self, "data_updated_label", None)
        if label is not None and _is_qt_object_valid(label):
            try:
                label.setText(
                    f"✓ Dados atualizados às {updated.strftime('%H:%M:%S')}"
                )
                label.setToolTip(
                    "Última leitura concluída nesta página: "
                    + updated.strftime("%d/%m/%Y às %H:%M:%S")
                )
                label.setProperty("state", "fresh")
                self._repolish(label)
            except RuntimeError:
                pass
        self.apply_chart_calculation_contexts()
        self.apply_default_forecast_coverage()

    def mark_data_stale(self, moment: datetime | None = None) -> None:
        changed = moment or datetime.now()
        if self._data_updated_at is None:
            text = "● Dados ainda não atualizados nesta página"
        else:
            text = (
                f"● Existem alterações posteriores às "
                f"{self._data_updated_at.strftime('%H:%M:%S')}"
            )
        label = getattr(self, "data_updated_label", None)
        if label is not None and _is_qt_object_valid(label):
            try:
                label.setText(text)
                label.setToolTip(
                    "Foram alterados dados financeiros às "
                    + changed.strftime("%d/%m/%Y às %H:%M:%S")
                    + "; esta página será atualizada ao abrir."
                )
                label.setProperty("state", "stale")
                self._repolish(label)
            except RuntimeError:
                pass

    def mark_data_refresh_failed(self) -> None:
        previous = (
            self._data_updated_at.strftime("%H:%M:%S")
            if self._data_updated_at is not None
            else "indisponível"
        )
        label = getattr(self, "data_updated_label", None)
        if label is not None and _is_qt_object_valid(label):
            try:
                label.setText(
                    f"⚠ Atualização falhou · última leitura: {previous}"
                )
                label.setToolTip(
                    "A página manteve os últimos dados apresentados; tenta atualizar novamente."
                )
                label.setProperty("state", "stale")
                self._repolish(label)
            except RuntimeError:
                pass

    def apply_default_forecast_coverage(self) -> None:
        """Give every predictive card the same honest minimum data disclosure."""

        from ui.components.cards import GlassCard

        keywords = (
            "previs",
            "proje",
            "monte carlo",
            "rutura",
            "tesouraria",
            "runway",
            "independência financeira",
            "futuro",
        )
        cards: list[GlassCard] = []
        for card in self.findChildren(GlassCard):
            if not _is_qt_object_valid(card):
                continue
            try:
                card_title_fn = getattr(card, "card_title", None)
                if callable(card_title_fn):
                    title = str(card_title_fn() or "").casefold()
                else:
                    title_label = getattr(card, "title_label", None)
                    if title_label is not None and _is_qt_object_valid(title_label):
                        title = str(title_label.text() or "").casefold()
                    else:
                        title = ""
                key = str(card.property("pageCardKey") or "").casefold()
                if any(keyword in f"{title} {key}" for keyword in keywords):
                    cards.append(card)
            except RuntimeError:
                continue
        if not cards:
            return
        snapshot_provider = getattr(
            self.analytics_service,
            "data_coverage_snapshot",
            None,
        )
        if not callable(snapshot_provider):
            return
        today = date.today()
        snapshot = snapshot_provider(
            (today - timedelta(days=730)).isoformat(),
            today.isoformat(),
        )
        text = self.data_coverage_text(snapshot)
        active_months = int(snapshot.get("active_months", 0) or 0)
        invalid = int(snapshot.get("invalid_transactions_excluded", 0) or 0)
        tone = "positive" if active_months >= 6 and not invalid else "warning"
        for card in cards:
            if not _is_qt_object_valid(card):
                continue
            try:
                # The Dashboard already owns a context badge in the same visual slot.
                # Its text is enriched separately to avoid presenting two badges.
                existing = [
                    badge
                    for badge in card.findChildren(QLabel, "DataCoverageBadge")
                    if _is_qt_object_valid(badge)
                ]
                coverage_label = getattr(card, "data_coverage_label", None)
                if existing and (
                    coverage_label is None or not _is_qt_object_valid(coverage_label)
                ):
                    continue
                card.set_data_coverage(text, tone)
            except RuntimeError:
                continue

    def data_coverage_text(self, snapshot: dict[str, Any] | None) -> str:
        """Format forecast coverage, including in lightweight test integrations."""

        values = dict(snapshot or {})
        formatter = getattr(
            self.analytics_service,
            "data_coverage_label",
            None,
        )
        if callable(formatter):
            return str(formatter(values) or "").strip()
        active_months = int(values.get("active_months", 0) or 0)
        expected_months = int(values.get("expected_months", 0) or 0)
        invalid = int(values.get("invalid_transactions_excluded", 0) or 0)
        if active_months or expected_months or invalid:
            months = (
                f"{active_months}/{expected_months} meses"
                if expected_months
                else f"{active_months} meses"
            )
            return f"Cobertura real · {months} · inválidos excluídos {invalid}"
        return "Cobertura real · detalhe indisponível nesta leitura"

    def apply_chart_calculation_contexts(self) -> None:
        """Attach one calculation-evidence provider to every reusable chart."""

        from ui.components.charts.base import StableChart

        for chart in self.findChildren(StableChart):
            if not _is_qt_object_valid(chart):
                continue
            try:
                chart.configure_calculation_context(self._chart_calculation_context)
            except RuntimeError:
                continue

    @staticmethod
    def _item_period(item: dict[str, Any]) -> tuple[str, str, str]:
        raw_date = str(item.get("date") or "")[:10]
        if len(raw_date) == 10:
            return raw_date, raw_date, raw_date
        month = str(item.get("month") or "")[:7]
        if len(month) == 7 and month[4:5] == "-":
            try:
                start = date.fromisoformat(f"{month}-01")
                next_month = (
                    date(start.year + 1, 1, 1)
                    if start.month == 12
                    else date(start.year, start.month + 1, 1)
                )
                end = next_month - timedelta(days=1)
                return start.isoformat(), end.isoformat(), month
            except ValueError:
                pass
        year = str(item.get("year") or "")[:4]
        if year.isdigit():
            return f"{year}-01-01", f"{year}-12-31", year
        return "", "", ""

    def _page_period(self, item: dict[str, Any]) -> tuple[str, str, str]:
        selected = getattr(self, "selected_period", None)
        if callable(selected):
            try:
                value = selected()
                if isinstance(value, (tuple, list)) and len(value) >= 2:
                    return (
                        str(value[0])[:10],
                        str(value[1])[:10],
                        str(value[2] if len(value) > 2 else "período selecionado"),
                    )
            except Exception as exc:
                log_exception("Failed to read page period for chart detail", exc)
        exact = self._item_period(item)
        if exact[0]:
            return exact
        edits = [
            edit
            for edit in self.findChildren(QDateEdit)
            if _is_qt_object_valid(edit)
        ]
        if edits:
            values = []
            for edit in edits:
                try:
                    if edit.isVisible():
                        values.append(edit.date().toString("yyyy-MM-dd"))
                except RuntimeError:
                    continue
            if values:
                values.sort()
                return values[0], values[-1], f"{values[0]} a {values[-1]}"
        return "", "", "Período atualmente visível"

    def _visible_filter_text(self, item: dict[str, Any]) -> str:
        summary = getattr(self, "filters_summary", None)
        if isinstance(summary, QLabel) and _is_qt_object_valid(summary):
            try:
                if summary.text().strip():
                    return " ".join(summary.text().split())
            except RuntimeError:
                pass
        values: list[str] = []
        for combo in self.findChildren(QComboBox):
            if not _is_qt_object_valid(combo):
                continue
            try:
                if not combo.isVisible():
                    continue
                value = combo.currentText().strip()
                if not value or value.casefold() in {"todos", "todas", "—"}:
                    continue
                label = combo.accessibleName().strip()
                values.append(f"{label}: {value}" if label else value)
                if len(values) >= 5:
                    break
            except RuntimeError:
                continue
        category = str(item.get("category") or item.get("label") or "").strip()
        identity_provider = getattr(self.repo, "category_identity", None)
        category_id, category_name = (
            identity_provider(category, create=False)
            if callable(identity_provider)
            else ("", category)
        )
        if category_id:
            values.insert(0, f"Categoria: {category_name}")
        return " · ".join(dict.fromkeys(values)) or "Sem filtros adicionais"

    def _chart_calculation_context(
        self,
        item: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        start, end, period_label = self._page_period(item)
        conditions = [
            "t.amount IS NOT NULL",
            "TYPEOF(t.amount) IN ('integer','real')",
            "ABS(t.amount)>=0.005",
            "LENGTH(COALESCE(NULLIF(m.payment_date,''),t.date))=10",
            "DATE(COALESCE(NULLIF(m.payment_date,''),t.date),'+0 days')="
            "COALESCE(NULLIF(m.payment_date,''),t.date)",
        ]
        params: list[Any] = []
        if start and end:
            conditions.extend(
                [
                    "COALESCE(NULLIF(m.payment_date,''),t.date)>=?",
                    "COALESCE(NULLIF(m.payment_date,''),t.date)<=?",
                ]
            )
            params.extend((start, end))
        category = str(item.get("category") or item.get("label") or "").strip()
        identity_provider = getattr(self.repo, "category_identity", None)
        category_id, _category_name = (
            identity_provider(category, create=False)
            if callable(identity_provider)
            else ("", category)
        )
        split_join = ""
        join_params: list[Any] = []
        if category_id:
            split_join = "LEFT JOIN transaction_splits s ON s.transaction_id=t.id AND s.category_id=?"
            join_params.append(category_id)
            conditions.append("(t.category_id=? OR s.id IS NOT NULL)")
            params.append(category_id)
        scalar_provider = getattr(self.repo, "scalar", None)
        movement_count = (
            int(
                scalar_provider(
                    f"SELECT COUNT(DISTINCT t.id) FROM transactions t "
                    f"LEFT JOIN transaction_meta m ON m.transaction_id=t.id {split_join} WHERE "
                    + " AND ".join(conditions),
                    tuple(join_params + params),
                    default=0,
                )
            )
            if callable(scalar_provider)
            else 0
        )
        coverage_start = start or (date.today() - timedelta(days=730)).isoformat()
        coverage_end = end or date.today().isoformat()
        snapshot_provider = getattr(
            self.analytics_service,
            "data_coverage_snapshot",
            None,
        )
        coverage = (
            snapshot_provider(coverage_start, coverage_end)
            if callable(snapshot_provider)
            else {}
        )
        invalid = int(coverage.get("invalid_transactions_excluded", 0) or 0)
        exclusions = [f"{invalid} transação(ões) inválida(s)"]
        explicit = payload.get("exclusions")
        if isinstance(explicit, (list, tuple)):
            exclusions.extend(str(value) for value in explicit if str(value).strip())
        elif str(explicit or "").strip():
            exclusions.append(str(explicit))
        return {
            "period": period_label,
            "filters": self._visible_filter_text(item),
            "movements": movement_count,
            "exclusions": " · ".join(dict.fromkeys(exclusions)),
            "data_coverage": self.data_coverage_text(coverage),
            "updated_at": (
                self._data_updated_at.strftime("%H:%M:%S")
                if self._data_updated_at is not None
                else "a atualizar"
            ),
        }

    def configure_workspace_navigation(self, workspace, active_key: str) -> None:
        """Show a compact page switcher when this page belongs to a workspace."""

        if workspace is None:
            self.workspace_host.setVisible(False)
            return
        from ui.components.workspace import WorkspaceSwitcher

        if self._workspace_switcher is None:
            self._workspace_switcher = WorkspaceSwitcher(
                self.app,
                workspace,
                active_key,
                self.workspace_host,
            )
            self.workspace_host_layout.addWidget(self._workspace_switcher)
        else:
            self._workspace_switcher.set_active(active_key)
        self.workspace_host.setVisible(True)

    def register_responsive_grid(
        self,
        layout: QGridLayout,
        max_columns: int = 4,
        min_column_width: int = 290,
    ) -> None:
        if not any(
            existing is layout for existing, _columns, _width in self._responsive_grids
        ):
            self._responsive_grids.append(
                (
                    layout,
                    max(1, int(max_columns)),
                    max(180, int(min_column_width)),
                )
            )
        self._schedule_responsive_reflow()

    def _schedule_responsive_reflow(self) -> None:
        """Coalesce repeated resize requests into one event-loop callback."""

        self._responsive_reflow_timer.start()

    def _responsive_column_count(
        self,
        max_columns: int,
        min_column_width: int,
        spacing: int = 0,
    ) -> int:
        effective_minimum = int(
            round(min_column_width * (0.90 if self.page_density == "compact" else 1.0))
        )
        try:
            viewport_w = (
                self.app.content_scroll.viewport().width()
                if hasattr(self.app, "content_scroll")
                else 0
            )
        except (AttributeError, RuntimeError):
            viewport_w = 0
        effective_width = max(self.width(), viewport_w)
        return ResponsivePolicy.grid_columns(
            effective_width,
            max_columns,
            effective_minimum,
            spacing,
        )

    def reflow_responsive_grid(
        self,
        layout: QGridLayout,
        max_columns: int = 4,
        min_column_width: int = 290,
    ) -> None:
        positioned: list[tuple[int, int, QWidget, Qt.Alignment]] = []
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is None or not self._qt_object_is_valid(widget):
                continue
            row, column, _row_span, _column_span = layout.getItemPosition(index)
            positioned.append((row, column, widget, item.alignment()))
        positioned.sort(key=lambda entry: (entry[0], entry[1]))
        spacing = layout.horizontalSpacing()
        if spacing < 0:
            spacing = layout.spacing()
        columns = self._responsive_column_count(
            max_columns,
            min_column_width,
            max(0, spacing),
        )
        for _row, _column, widget, _alignment in positioned:
            layout.removeWidget(widget)
        for index, (_row, _column, widget, alignment) in enumerate(positioned):
            layout.addWidget(widget, index // columns, index % columns, alignment)
        for column in range(max_columns):
            layout.setColumnStretch(column, 1 if column < columns else 0)
        layout.invalidate()

    def reflow_responsive_grids(self) -> None:
        for layout, max_columns, min_column_width in self._responsive_grids:
            self.reflow_responsive_grid(
                layout,
                max_columns,
                min_column_width,
            )
        layout_cards = (
            getattr(self, "_layout_content_cards", None)
            or getattr(self, "_reflow_cards", None)
            or getattr(self, "reflow_dashboard", None)
        )
        if callable(layout_cards):
            try:
                layout_cards()
            except Exception:
                pass

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        if self._responsive_grids:
            self._schedule_responsive_reflow()

    def refresh(self) -> None:
        pass

    def flush_pending_state(self) -> bool:
        """Persist page-owned debounced state before the application closes."""

        return self.persist_page_view_state()

    def _density_setting_key(self) -> str:
        return f"page_density:{self.__class__.__name__}"

    def _view_state_setting_key(self) -> str:
        return f"page_view_state:{self.__class__.__name__}"

    def _update_density_button(self) -> None:
        compact = self.page_density == "compact"
        self.density_btn.setText("\u25a6 Compacta" if compact else "\u25a6 Confortável")
        self.density_btn.setToolTip(
            "Mudar para vista confortável" if compact else "Mudar para vista compacta"
        )

    def toggle_page_density(self) -> None:
        self.set_page_density(
            "comfortable" if self.page_density == "compact" else "compact"
        )

    def set_page_density(self, mode: str, *, persist: bool = True) -> None:
        selected = "compact" if str(mode) == "compact" else "comfortable"
        self.page_density = selected
        if persist:
            self.repo.set_setting(self._density_setting_key(), selected)
        self._update_density_button()
        self.apply_page_density()
        self._schedule_responsive_reflow()
        sync = getattr(self.app, "sync_content_size", None)
        if callable(sync):
            QTimer.singleShot(0, sync)

    def apply_page_density(self) -> None:
        """Apply one density language to cards and tables on this page."""

        from ui.components.cards import GlassCard

        compact = self.page_density == "compact"
        self.body.setSpacing(5 if compact else 8)
        for card in self.findChildren(GlassCard):
            if _is_qt_object_valid(card):
                try:
                    card.apply_density(self.page_density)
                except RuntimeError:
                    continue
        for table in self.findChildren(QTableView):
            if not _is_qt_object_valid(table):
                continue
            try:
                if bool(table.property("disableDensity")):
                    continue
                header = table.verticalHeader()
                if header is None or not _is_qt_object_valid(header):
                    continue
                baseline = table.property("comfortableRowHeight")
                if baseline is None:
                    baseline = max(26, int(header.defaultSectionSize()))
                    table.setProperty("comfortableRowHeight", baseline)
                header.setDefaultSectionSize(
                    max(25, int(baseline) - 5) if compact else int(baseline)
                )
            except RuntimeError:
                continue

    def update_export_privacy_labels(self) -> None:
        """Tell the user when privacy mode makes the safer export the default."""

        privacy = bool(getattr(getattr(self.app, "theme", None), "privacy", False))
        for button in self.findChildren(QPushButton):
            if not _is_qt_object_valid(button):
                continue
            try:
                if bool(button.property("privacyExportExempt")):
                    continue
                text = str(button.text() or "")
                original = button.property("privacyExportOriginalText")
                if original is None:
                    if not text.casefold().startswith("exportar"):
                        continue
                    original = text.replace(" mascarado", "")
                    button.setProperty("privacyExportOriginalText", original)
                base = str(original)
                button.setText(f"{base} mascarado" if privacy else base)
                if privacy:
                    button.setToolTip(
                        "Privacidade ativa: valores e campos sensíveis serão mascarados."
                    )
            except RuntimeError:
                continue

    def register_filter_panel(self, panel: QWidget, expander=None) -> None:
        """Register the page's main filter area for focus and persistence."""

        self._primary_filter_panel = panel
        self._filter_panel_expander = expander
        if panel.property("filtersExpanded") is None:
            panel.setProperty("filtersExpanded", True)

    def set_filter_panel_expanded(self, expanded: bool) -> None:
        if self._primary_filter_panel is None:
            return
        self._primary_filter_panel.setProperty("filtersExpanded", bool(expanded))
        if callable(self._filter_panel_expander):
            self._filter_panel_expander(bool(expanded))

    def focus_primary_filters(self) -> QWidget | None:
        """Expand and return the filter panel used by the floating shortcut."""

        panel = self._primary_filter_panel
        if panel is None:
            return None
        self.set_filter_panel_expanded(True)
        panel.show()
        panel.setFocus(Qt.OtherFocusReason)
        return panel

    def _is_inside_filter_panel(self, widget: QWidget) -> bool:
        panel = self._primary_filter_panel
        if not is_qt_valid(panel):
            return False
        current: QWidget | None = widget
        while is_qt_valid(current):
            if current is panel:
                return True
            current = current.parentWidget()
        return False

    @staticmethod
    def _json_scalar(value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def _persistent_filter_bindings(self) -> list[tuple[str, object]]:
        bindings: list[tuple[str, object]] = []
        seen: set[int] = set()
        supported = (
            QButtonGroup,
            QComboBox,
            QDateEdit,
            QDoubleSpinBox,
            QSpinBox,
            QSlider,
            QAbstractButton,
            QLineEdit,
        )
        for name, value in vars(self).items():
            if name.startswith("_") or not isinstance(value, supported) or not is_qt_valid(value):
                continue
            if id(value) in seen:
                continue
            if isinstance(value, QButtonGroup):
                if not any(
                    self._is_inside_filter_panel(button) for button in value.buttons()
                ):
                    continue
            elif isinstance(value, QWidget):
                if not self._is_inside_filter_panel(value):
                    continue
                if isinstance(value, QLineEdit) and not bool(
                    value.property("persistPageFilter")
                ):
                    continue
            seen.add(id(value))
            bindings.append((name, value))
        return bindings

    def _capture_page_filter_state(self) -> dict[str, object]:
        state: dict[str, object] = {}
        for name, widget in self._persistent_filter_bindings():
            if isinstance(widget, QButtonGroup):
                checked = widget.checkedButton()
                if checked is not None:
                    state[name] = {
                        "type": "group",
                        "value": self._json_scalar(checked.property("filterValue")),
                        "text": checked.text(),
                    }
            elif isinstance(widget, QComboBox):
                state[name] = {
                    "type": "combo",
                    "data": self._json_scalar(widget.currentData()),
                    "text": widget.currentText(),
                }
            elif isinstance(widget, QDateEdit):
                state[name] = {
                    "type": "date",
                    "value": widget.date().toString("yyyy-MM-dd"),
                }
            elif isinstance(widget, (QDoubleSpinBox, QSpinBox, QSlider)):
                state[name] = {"type": "number", "value": widget.value()}
            elif isinstance(widget, QAbstractButton) and widget.isCheckable():
                state[name] = {"type": "checked", "value": widget.isChecked()}
            elif isinstance(widget, QLineEdit):
                state[name] = {"type": "text", "value": widget.text()}
        return state

    def _restore_page_filter_state(self, state: dict[str, object]) -> None:
        bindings = dict(self._persistent_filter_bindings())
        for name, raw in dict(state or {}).items():
            widget = bindings.get(str(name))
            entry = raw if isinstance(raw, dict) else {}
            if widget is None or not entry:
                continue
            kind = str(entry.get("type") or "")
            try:
                if isinstance(widget, QButtonGroup) and kind == "group":
                    target = entry.get("value")
                    button = next(
                        (
                            candidate
                            for candidate in widget.buttons()
                            if candidate.property("filterValue") == target
                        ),
                        None,
                    )
                    if button is None:
                        button = next(
                            (
                                candidate
                                for candidate in widget.buttons()
                                if candidate.text() == str(entry.get("text") or "")
                            ),
                            None,
                        )
                    if button is not None and not button.isChecked():
                        button.click()
                elif isinstance(widget, QComboBox) and kind == "combo":
                    index = widget.findData(entry.get("data"))
                    if index < 0:
                        index = widget.findText(str(entry.get("text") or ""))
                    if index >= 0 and index != widget.currentIndex():
                        widget.setCurrentIndex(index)
                elif isinstance(widget, QDateEdit) and kind == "date":
                    parsed = QDate.fromString(
                        str(entry.get("value") or ""), "yyyy-MM-dd"
                    )
                    if parsed.isValid() and parsed != widget.date():
                        widget.setDate(parsed)
                elif isinstance(widget, QDoubleSpinBox) and kind == "number":
                    widget.setValue(float(entry.get("value", widget.value())))
                elif isinstance(widget, (QSpinBox, QSlider)) and kind == "number":
                    widget.setValue(int(entry.get("value", widget.value())))
                elif isinstance(widget, QAbstractButton) and kind == "checked":
                    widget.setChecked(bool(entry.get("value")))
                elif isinstance(widget, QLineEdit) and kind == "text":
                    widget.setText(str(entry.get("value") or ""))
            except (RuntimeError, TypeError, ValueError) as exc:
                log_exception(f"Failed to restore page filter {name}", exc)

    def _chart_bindings(self) -> dict[str, object]:
        try:
            from ui.components.charts.base import StableChart
        except ImportError:
            return {}
        result: dict[str, object] = {}
        seen: set[int] = set()
        for name, value in vars(self).items():
            if isinstance(value, StableChart) and id(value) not in seen:
                result[name] = value
                seen.add(id(value))
        return result

    def persist_page_view_state(self) -> bool:
        """Durably keep density, filters, collapse state and chart view."""

        # ``hideEvent`` can be delivered while the top-level window is being
        # torn down.  The explicit shutdown transaction has already persisted
        # every page before closing SQLite, so a late UI event must be a no-op.
        if bool(getattr(self.app, "_database_closed", False)):
            return True
        try:
            charts = {
                name: {
                    "zoom": round(float(getattr(chart, "zoom_factor", 1.0)), 6),
                    "pan": round(float(getattr(chart, "pan_offset", 0.0)), 3),
                }
                for name, chart in self._chart_bindings().items()
            }
            state = {
                "version": 1,
                "density": self.page_density,
                "filters": self._capture_page_filter_state(),
                "filters_expanded": (
                    bool(self._primary_filter_panel.property("filtersExpanded"))
                    if self._primary_filter_panel is not None
                    else True
                ),
                "charts": charts,
            }
            self.repo.set_setting(
                self._view_state_setting_key(),
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            )
            return True
        except Exception as exc:
            log_exception(
                f"Failed to persist page view [{self.__class__.__name__}]",
                exc,
            )
            return False

    def restore_page_view_state(self) -> None:
        if self._view_state_restored:
            return
        if bool(getattr(self.app, "_database_closed", False)):
            return
        self._view_state_restored = True
        try:
            raw = self.repo.setting(self._view_state_setting_key(), "{}")
            state = json.loads(str(raw or "{}"))
            if not isinstance(state, dict):
                state = {}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            log_exception(
                f"Failed to read page view [{self.__class__.__name__}]",
                exc,
            )
            state = {}
        density = str(state.get("density") or self.page_density)
        self.set_page_density(density, persist=False)
        default_expanded = (
            bool(self._primary_filter_panel.property("filtersExpanded"))
            if self._primary_filter_panel is not None
            else True
        )
        self.set_filter_panel_expanded(
            bool(state.get("filters_expanded", default_expanded))
        )
        filters = state.get("filters")
        if isinstance(filters, dict):
            self._restore_page_filter_state(filters)
        charts = state.get("charts")
        if isinstance(charts, dict):
            for name, chart in self._chart_bindings().items():
                entry = charts.get(name)
                if not isinstance(entry, dict) or not bool(
                    getattr(chart, "supports_zoom", False)
                ):
                    continue
                try:
                    chart.zoom_factor = max(
                        float(chart.minimum_zoom),
                        min(
                            float(chart.maximum_zoom),
                            float(entry.get("zoom", 1.0)),
                        ),
                    )
                    chart.pan_offset = max(0.0, float(entry.get("pan", 0.0)))
                    chart._clamp_pan()
                    chart.invalidate_cache()
                except (AttributeError, TypeError, ValueError):
                    continue
        after_restore = getattr(self, "_after_restore_page_view", None)
        if callable(after_restore):
            after_restore()

    def _card_setting_key(self) -> str:
        return f"page_cards:{self.__class__.__name__}"

    def _card_preferences(self) -> dict[str, list[str]]:
        try:
            parsed = json.loads(read_setting(self.repo, self._card_setting_key(), "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return {
            "hidden": [str(value) for value in parsed.get("hidden", [])],
            "permanent": [str(value) for value in parsed.get("permanent", [])],
        }

    @staticmethod
    def _card_slug(value: str) -> str:
        normalised = unicodedata.normalize("NFKD", str(value or ""))
        ascii_text = normalised.encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "_", ascii_text.casefold()).strip("_")
        return slug or "card"

    @staticmethod
    def _qt_object_is_valid(obj: object) -> bool:
        """Return false for Python wrappers whose Qt object was deleted.

        Dynamic cards use ``deleteLater`` during refresh.  Until Qt processes
        every deferred deletion, ``findChildren`` can briefly return a card or
        title wrapper whose underlying C++ object no longer exists.
        """
        return _is_qt_object_valid(obj)

    def page_cards(self) -> list[tuple[str, str, QWidget]]:
        from ui.components.cards import GlassCard

        cards = self.findChildren(GlassCard)
        seen: dict[str, int] = {}
        result: list[tuple[str, str, QWidget]] = []
        for card in cards:
            if not _is_qt_object_valid(card):
                continue
            try:
                card_title_fn = getattr(card, "card_title", None)
                if callable(card_title_fn):
                    title = str(card_title_fn() or "").strip()
                else:
                    title_label = getattr(card, "title_label", None)
                    if title_label is not None and not _is_qt_object_valid(
                        title_label
                    ):
                        continue
                    title = str(
                        title_label.text() if title_label is not None else ""
                    ).strip()
                if not title:
                    continue
                existing_key = str(card.property("pageCardKey") or "")
                if existing_key:
                    key = existing_key
                else:
                    base = self._card_slug(title)
                    seen[base] = seen.get(base, 0) + 1
                    key = base if seen[base] == 1 else f"{base}_{seen[base]}"
                    card.setProperty("pageCardKey", key)
                result.append((key, title, card))
            except RuntimeError:
                # A deferred Qt deletion may finish between the validity check
                # and reading the label/property.  Ignore only that stale card.
                continue
        return result

    def card_should_show(self, card: QWidget) -> bool:
        preferences = self._card_preferences()
        key = str(card.property("pageCardKey") or "")
        if not key:
            self.page_cards()
            key = str(card.property("pageCardKey") or "")
        return key not in preferences["hidden"] and key not in preferences["permanent"]

    def card_is_permanently_disabled(self, card_or_key: QWidget | str) -> bool:
        if isinstance(card_or_key, str):
            key = card_or_key
        else:
            key = str(card_or_key.property("pageCardKey") or "")
            if not key:
                self.page_cards()
                key = str(card_or_key.property("pageCardKey") or "")
        return key in set(self._card_preferences()["permanent"])

    def should_compute_card(self, card: QWidget) -> bool:
        """Return false when the user permanently disabled this card."""
        return not self.card_is_permanently_disabled(card)

    def apply_card_preferences(self) -> None:
        preferences = self._card_preferences()
        hidden = set(preferences["hidden"])
        permanent = set(preferences["permanent"])
        for key, _title, card in self.page_cards():
            if not self._qt_object_is_valid(card):
                continue
            try:
                is_permanent = key in permanent
                card.setProperty("permanentlyDisabled", is_permanent)
                card.setUpdatesEnabled(not is_permanent)
                card.setVisible(key not in hidden and not is_permanent)
                if hasattr(card, "apply_phrase_preferences") and callable(
                    card.apply_phrase_preferences
                ):
                    card.apply_phrase_preferences()
            except RuntimeError:
                continue
        self.updateGeometry()

    def open_card_customization(self) -> None:
        from ui.card_customization import PageCardCustomizationDialog

        preferences = self._card_preferences()
        hidden = set(preferences["hidden"])
        permanent = set(preferences["permanent"])
        cards = self.page_cards()
        if not cards:
            QMessageBox.information(
                self,
                "Personalizar cards",
                "Esta página não tem cards configuráveis.",
            )
            return
        entries = [
            {
                "key": key,
                "title": title,
                "visible": key not in hidden and key not in permanent,
                "permanent": key in permanent,
                "card": card,
            }
            for key, title, card in cards
        ]
        dialog = PageCardCustomizationDialog(self.page_title, entries, self)
        if not dialog.exec():
            return
        self.repo.set_setting(
            self._card_setting_key(),
            json.dumps(dialog.state(), ensure_ascii=False),
        )
        invalidate_setting_read(self.repo, self._card_setting_key())
        self.apply_card_preferences()
        preferences_changed = getattr(self, "card_preferences_changed", None)
        if callable(preferences_changed):
            preferences_changed()
        app_sync = getattr(self.app, "sync_content_size", None)
        if callable(app_sync):
            QTimer.singleShot(0, app_sync)

    def reset_page_view(self) -> None:
        reset_filters = getattr(self, "_reset_page_filters", None)
        if callable(reset_filters):
            reset_filters()
        try:
            from ui.components.charts.base import StableChart

            for chart in self.findChildren(StableChart):
                if not _is_qt_object_valid(chart):
                    continue
                try:
                    if bool(getattr(chart, "supports_zoom", False)):
                        chart.reset_zoom()
                    chart.clear_pinned_detail()
                except RuntimeError:
                    continue
        except (ImportError, RuntimeError):
            pass
        self.persist_page_view_state()
        status_bar = getattr(self.app, "statusBar", None)
        if callable(status_bar):
            status_bar().showMessage(
                "Zoom, filtros e comparações repostos.",
                2500,
            )

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        # Restoring the page view already applies its density and schedules the
        # responsive reflow.  Running ``apply_page_density`` again on the same
        # event-loop turn caused a second full card/table layout pass during a
        # cold start.
        QTimer.singleShot(0, self.restore_page_view_state)
        QTimer.singleShot(0, self.update_export_privacy_labels)
        QTimer.singleShot(0, self.apply_card_preferences)

    def hideEvent(self, event):  # type: ignore[override]
        if self._view_state_restored:
            self.persist_page_view_state()
        super().hideEvent(event)
