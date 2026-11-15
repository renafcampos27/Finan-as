#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reusable Liquid Glass backgrounds, cards and visual status components."""

from __future__ import annotations

from ui.core.read_scope import read_setting, invalidate_setting_read

import json
import sys
from typing import Any, Optional

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import (
        QAbstractAnimation,
        QEvent,
        QPointF,
        QPropertyAnimation,
        QRect,
        QRectF,
        QSize,
        QTimer,
        Property,
        Signal,
        Qt,
    )

    from PySide6.QtGui import (
        QBrush,
        QColor,
        QFont,
        QFontMetrics,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
    )
    from PySide6.QtWidgets import (
        QFrame,
        QHBoxLayout,
        QLabel,
        QLayout,
        QMenu,
        QProgressBar,
        QSizePolicy,
        QVBoxLayout,
        QWidget,
    )
except Exception as exc:  # pragma: no cover - only used when PySide6 is missing.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from utils.qt_runtime import is_qt_valid as _is_qt_valid


from ui.core.theme import Theme, apply_menu_theme, theme_for_widget  # noqa: E402


_UNSET = object()
_TONE_COLORS = {
    "positive": "#30D158",
    "warning": "#FF9F0A",
    "negative": "#FF453A",
    "neutral": "#0A84FF",
}


def _normalise_tone(tone: str) -> str:
    value = str(tone or "neutral").strip().lower()
    return value if value in _TONE_COLORS else "neutral"


def _safe_color(value: str, fallback: str = "#0A84FF") -> QColor:
    color = QColor(str(value or ""))
    return color if color.isValid() else QColor(fallback)


def _rgba_css(value: str | QColor, alpha: int) -> str:
    """Return Qt-safe rgba; eight-digit hex in Qt is #AARRGGBB, not CSS RGBA."""
    color = QColor(value) if isinstance(value, QColor) else _safe_color(value)
    return (
        f"rgba({color.red()},{color.green()},{color.blue()},{max(0, min(255, alpha))})"
    )


def _mix(left: QColor, right: QColor, amount: float) -> QColor:
    amount = max(0.0, min(1.0, float(amount)))
    keep = 1.0 - amount
    return QColor(
        round(left.red() * keep + right.red() * amount),
        round(left.green() * keep + right.green() * amount),
        round(left.blue() * keep + right.blue() * amount),
        left.alpha(),
    )


def _normalise_progress(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        progress = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= progress <= 1.0:
        progress *= 100.0
    return max(0, min(100, round(progress)))


def _apply_tone(widget: Optional[QWidget], tone: str) -> None:
    if widget is None or not _is_qt_valid(widget):
        return
    try:
        widget.setProperty("tone", _normalise_tone(tone))
        style = widget.style()
        if style is not None:
            style.unpolish(widget)
            style.polish(widget)
        widget.update()
    except RuntimeError:
        pass


class Background(QWidget):
    def __init__(self, theme: Theme, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.theme = theme

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.update()

    def paintEvent(self, event):  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        grad = QLinearGradient(0, 0, rect.width(), rect.height())
        grad.setColorAt(0.0, self.theme.bg1)
        grad.setColorAt(
            0.50, QColor("#ECFDF5") if not self.theme.dark else QColor("#142033")
        )
        grad.setColorAt(1.0, self.theme.bg2)
        painter.fillRect(rect, grad)
        blobs = [
            (
                QColor(48, 209, 88, 92),
                QPointF(rect.width() * 0.18, rect.height() * 0.18),
                rect.width() * 0.24,
            ),
            (
                QColor(10, 132, 255, 80),
                QPointF(rect.width() * 0.78, rect.height() * 0.15),
                rect.width() * 0.22,
            ),
            (
                QColor(191, 90, 242, 58),
                QPointF(rect.width() * 0.55, rect.height() * 0.88),
                rect.width() * 0.28,
            ),
            (
                QColor(255, 159, 10, 48),
                QPointF(rect.width() * 0.86, rect.height() * 0.75),
                rect.width() * 0.18,
            ),
        ]
        for color, center, radius in blobs:
            path = QPainterPath()
            path.addEllipse(center, radius, radius)
            glow = QLinearGradient(
                center.x() - radius,
                center.y() - radius,
                center.x() + radius,
                center.y() + radius,
            )
            glow.setColorAt(0, color)
            transparent = QColor(color)
            transparent.setAlpha(0)
            glow.setColorAt(1, transparent)
            painter.fillPath(path, glow)


class GlassCard(QFrame):
    def __init__(
        self,
        title: str = "",
        parent: Optional[QWidget] = None,
        strong: bool = False,
        *,
        accent: Optional[str] = None,
    ):
        super().__init__(parent)
        self._glass_strong = strong
        self._accent = _safe_color(accent or "#0A84FF")
        self._uses_theme_accent = not bool(accent)
        self._hover_progress = 0.0
        self._mouse_offset_x = 0.0
        self._mouse_offset_y = 0.0
        self._compact_height_labels: list[tuple[QLabel, bool]] = []
        self._compact_height_widgets: list[QWidget] = []
        self._compact_height_sync_pending = False
        self._fit_compact_context_header = False
        self.context_badge: Optional[QLabel] = None
        self._context_header: Optional[QWidget] = None
        self.description_label: Optional[QLabel] = None
        self.legend_label: Optional[QLabel] = None
        self.footer_label: Optional[QLabel] = None
        self._original_description: str = ""
        self._original_footer: str = ""
        self.data_coverage_label: Optional[QLabel] = None
        self.confidence_badge: Optional[QLabel] = None
        self._original_data_coverage: str = ""
        self._original_confidence_badge: str = ""
        self._original_context_badge: str = ""
        self._original_legend: str = ""
        self._density_baseline: tuple[int, int, int, int, int] | None = None
        # Cards are painted manually because translucent QSS frames are inconsistent
        # across Windows/PySide6 render backends.
        self.setObjectName("GlassPaintedStrong" if strong else "GlassPainted")
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet(
            "QFrame#GlassPainted, QFrame#GlassPaintedStrong { "
            "background: transparent; border: none; }"
        )
        self.setMouseTracking(True)

        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(15, 13, 15, 13)
        self.v.setSpacing(8)
        self._original_title: str = str(title or "").strip()
        self.title_label: Optional[QLabel] = None
        if title:
            self.title_label = QLabel(title)
            self.title_label.setObjectName("SectionTitle")
            self.v.addWidget(self.title_label)
            # A title must follow the width assigned by the responsive grid.
            # Relying on QLabel's one-line size hint made longer titles appear
            # truncated on common 1920x1080/125% Windows configurations.
            self.fit_label_height(
                self.title_label,
                word_wrap=True,
                stable_measurement=True,
            )

        self._anim = QPropertyAnimation(self, b"hoverProgress", self)
        self._anim.setDuration(170)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        try:
            if getattr(self, "v", None) is not None and _is_qt_valid(self.v):
                return self.v.minimumSize()
        except RuntimeError:
            pass
        return super().minimumSizeHint()

    def sizeHint(self) -> QSize:  # type: ignore[override]
        try:
            if getattr(self, "v", None) is not None and _is_qt_valid(self.v):
                return self.v.sizeHint()
        except RuntimeError:
            pass
        return super().sizeHint()

    def card_title(self) -> str:
        """Safely return card title text if title_label is valid and intact."""
        label = getattr(self, "title_label", None)
        if label is None or not _is_qt_valid(label):
            return ""
        try:
            return str(label.text() or "").strip()
        except RuntimeError:
            return ""

    def set_card_title(self, title: str) -> None:
        """Safely update the card title and synchronize compact heights."""
        value = str(title or "").strip()
        self._original_title = value
        if self.title_label is not None and _is_qt_valid(self.title_label):
            try:
                self.title_label.setText(value)
                self.refresh_compact_heights()
                return
            except RuntimeError:
                pass

    def _semantic_label(
        self,
        attribute: str,
        object_name: str,
        text: str,
        *,
        insert_index: int | None = None,
    ) -> QLabel | None:
        try:
            if getattr(self, "v", None) is None or not _is_qt_valid(self.v):
                return None
            label = getattr(self, attribute, None)
            if label is None or not _is_qt_valid(label):
                label = QLabel(self)
                label.setObjectName(object_name)
                label.setWordWrap(True)
                label.setMinimumWidth(0)
                label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                if insert_index is None:
                    self.v.addWidget(label)
                else:
                    self.v.insertWidget(max(0, min(insert_index, self.v.count())), label)
                setattr(self, attribute, label)
            value = str(text or "").strip()
            label.setText(value)
            label.setVisible(bool(value))
            label.setAccessibleDescription(value)
            self.fit_label_height(
                label,
                word_wrap=True,
                stable_measurement=True,
            )
            self.updateGeometry()
            return label
        except RuntimeError:
            return None

    def set_description(self, text: str) -> None:
        """Set the standard explanatory line immediately below the header."""
        self._original_description = str(text or "").strip()
        try:
            if getattr(self, "v", None) is None or not _is_qt_valid(self.v):
                return
            prefs = self.phrase_preferences()
            show = prefs.get("show_description", True)
            custom = prefs.get("custom_description", "")
            display = (custom if custom else self._original_description) if show else ""
            header = (
                self._context_header
                if _is_qt_valid(self._context_header)
                else (self.title_label if _is_qt_valid(self.title_label) else None)
            )
            index = self.v.indexOf(header) + 1 if header is not None else 0
            self._semantic_label(
                "description_label",
                "CardDescription",
                display,
                insert_index=index,
            )
            desc = getattr(self, "description_label", None)
            if desc is not None and _is_qt_valid(desc):
                desc.setAccessibleName("Descrição explicativa do cartão")
        except RuntimeError:
            pass

    def set_legend(self, text: str) -> None:
        """Set a compact textual legend before the card footer."""
        self._original_legend = str(text or "").strip()
        try:
            if getattr(self, "v", None) is None or not _is_qt_valid(self.v):
                return
            footer = self.footer_label if _is_qt_valid(self.footer_label) else None
            footer_index = self.v.indexOf(footer) if footer is not None else -1
            label = self._semantic_label(
                "legend_label",
                "CardLegend",
                self._original_legend,
                insert_index=footer_index if footer_index >= 0 else None,
            )
            prefs = self.phrase_preferences()
            show = bool(prefs.get("show_legend", True))
            if label is not None and _is_qt_valid(label):
                label.setVisible(bool(self._original_legend) and show)
        except RuntimeError:
            pass

    def set_footer(self, text: str) -> None:
        """Set the standard methodology/source line at the end of a card."""
        self._original_footer = str(text or "").strip()
        try:
            if getattr(self, "v", None) is None or not _is_qt_valid(self.v):
                return
            prefs = self.phrase_preferences()
            show = prefs.get("show_footer", True)
            custom = prefs.get("custom_footer", "")
            display = (custom if custom else self._original_footer) if show else ""
            self._semantic_label("footer_label", "CardFooter", display)
        except RuntimeError:
            pass

    def set_data_coverage(self, text: str, tone: str = "neutral") -> None:
        """Show the real data basis used by a predictive card."""
        self._original_data_coverage = str(text or "").strip()
        try:
            if getattr(self, "v", None) is None or not _is_qt_valid(self.v):
                return
            footer = self.footer_label if _is_qt_valid(self.footer_label) else None
            footer_index = self.v.indexOf(footer) if footer is not None else -1
            label = self._semantic_label(
                "data_coverage_label",
                "DataCoverageBadge",
                self._original_data_coverage,
                insert_index=footer_index if footer_index >= 0 else None,
            )
            if label is not None and _is_qt_valid(label):
                label.setAccessibleName("Cobertura real dos dados da previsão")
                label.setToolTip(self._original_data_coverage)
                # Coverage sentences are intentionally complete, but their one-line
                # size hint must not widen a dashboard column.  They wrap inside the
                # width assigned to the card instead.
                self.fit_label_height(
                    label,
                    word_wrap=True,
                    stable_measurement=True,
                )
                _apply_tone(label, tone)
                prefs = self.phrase_preferences()
                show = bool(prefs.get("show_data_coverage", True))
                label.setVisible(bool(self._original_data_coverage) and show)
        except RuntimeError:
            pass

    def set_confidence_badge(self, text: str, tone: str = "neutral") -> None:
        """Show a consistent confidence badge on predictive cards."""

        value = str(text or "").strip()
        self._original_confidence_badge = value
        try:
            if getattr(self, "v", None) is None or not _is_qt_valid(self.v):
                return
            if self.confidence_badge is None or not _is_qt_valid(self.confidence_badge):
                badge = QLabel(self)
                badge.setObjectName("CardConfidenceBadge")
                badge.setAlignment(Qt.AlignCenter)
                badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
                badge.setAccessibleName("Confiança da previsão")
                header = (
                    self._context_header
                    if _is_qt_valid(self._context_header)
                    else (self.title_label if _is_qt_valid(self.title_label) else None)
                )
                index = self.v.indexOf(header) + 1 if header is not None else 0
                self.v.insertWidget(index, badge, 0, Qt.AlignLeft)
                self.confidence_badge = badge
            self.confidence_badge.setText(value)
            self.confidence_badge.setToolTip(
                value
                + (
                    "\nA confiança depende da quantidade e estabilidade dos dados."
                    if value
                    else ""
                )
            )
            _apply_tone(self.confidence_badge, tone)
            prefs = self.phrase_preferences()
            show = bool(prefs.get("show_confidence_badge", True))
            self.confidence_badge.setVisible(bool(value) and show)
            self.updateGeometry()
        except RuntimeError:
            pass

    def apply_density(self, mode: str) -> None:
        """Apply page density without overriding a card's bespoke geometry."""
        try:
            if getattr(self, "v", None) is None or not _is_qt_valid(self.v):
                return
            if self._density_baseline is None:
                margins = self.v.contentsMargins()
                self._density_baseline = (
                    margins.left(),
                    margins.top(),
                    margins.right(),
                    margins.bottom(),
                    self.v.spacing(),
                )
            left, top, right, bottom, spacing = self._density_baseline
            compact = str(mode or "comfortable") == "compact"
            if compact:
                self.v.setContentsMargins(
                    max(8, left - 3),
                    max(5, top - 3),
                    max(8, right - 3),
                    max(6, bottom - 3),
                )
                self.v.setSpacing(max(3, spacing - 2))
            else:
                self.v.setContentsMargins(left, top, right, bottom)
                self.v.setSpacing(spacing)
            self.setProperty("pageDensity", "compact" if compact else "comfortable")
            self._queue_compact_height_sync()
            self.updateGeometry()
        except RuntimeError:
            pass

    def set_context_badge(self, text: str, tone: str = "neutral") -> None:
        """Show a compact, reusable time/data context without rewriting the title."""

        value = str(text or "").strip()
        if self.context_badge is None or not _is_qt_valid(self.context_badge):
            badge = QLabel(self)
            badge.setObjectName("CardContextBadge")
            badge.setAlignment(Qt.AlignCenter)
            badge.setMinimumWidth(0)
            badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            badge.setAccessibleName("Contexto do cartão")
            self.context_badge = badge

            title_lbl = self.title_label if _is_qt_valid(self.title_label) else None
            title_index = (
                self.v.indexOf(title_lbl) if title_lbl is not None else -1
            )
            if title_index >= 0 and title_lbl is not None:
                self.v.removeWidget(title_lbl)
                header = QWidget(self)
                header.setObjectName("CardContextHeader")
                header.setMinimumWidth(0)
                header.setSizePolicy(
                    QSizePolicy.Ignored,
                    QSizePolicy.Maximum,
                )
                header_layout = QHBoxLayout(header)
                header_layout.setSizeConstraint(QLayout.SetNoConstraint)
                header_layout.setContentsMargins(0, 0, 0, 0)
                header_layout.setSpacing(8)
                title_lbl.setMinimumWidth(0)
                title_lbl.setParent(header)
                badge.setParent(header)
                header_layout.addWidget(
                    title_lbl,
                    1,
                    (
                        Qt.AlignTop
                        if self._fit_compact_context_header
                        else Qt.Alignment()
                    ),
                )
                header_layout.addWidget(badge, 0, Qt.AlignTop)
                self.v.insertWidget(title_index, header)
                self._context_header = header
                if self._fit_compact_context_header:
                    self.fit_widget_height(header)
            else:
                # MetricCard builds its own title row after GlassCard. Keep the
                # badge immediately below that row without altering its layout.
                self.v.insertWidget(
                    min(1, self.v.count()),
                    badge,
                    0,
                    Qt.AlignLeft,
                )

        try:
            self.context_badge.setText(value)
            self.context_badge.setToolTip(value)
            self.context_badge.setAccessibleDescription(value)
            _apply_tone(self.context_badge, tone)
            self._original_context_badge = value
            prefs = self.phrase_preferences()
            show = bool(prefs.get("show_context_badge", True))
            self.context_badge.setVisible(bool(value) and show)
            if self._context_header is not None and _is_qt_valid(self._context_header):
                self._context_header.setVisible(
                    (bool(value) and show)
                    or (self.title_label is not None and _is_qt_valid(self.title_label))
                )
                self._context_header.updateGeometry()
            self.updateGeometry()
        except RuntimeError:
            pass

    def set_compact_header(
        self,
        *,
        word_wrap: bool = False,
        top_margin: int = 7,
        bottom_margin: int = 11,
        spacing: int = 4,
        fit_context_header: bool = False,
    ) -> None:
        """Keep a card title at its real responsive text height."""
        self._fit_compact_context_header = bool(fit_context_header)
        try:
            if getattr(self, "v", None) is None or not _is_qt_valid(self.v):
                return
            margins = self.v.contentsMargins()
            self.v.setContentsMargins(
                margins.left(),
                max(0, int(top_margin)),
                margins.right(),
                max(0, int(bottom_margin)),
            )
            self.v.setSpacing(max(0, int(spacing)))
            self.v.setAlignment(Qt.AlignTop)
            if self.title_label is not None and _is_qt_valid(self.title_label):
                self.title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.fit_label_height(
                    self.title_label,
                    word_wrap=word_wrap,
                    stable_measurement=self._fit_compact_context_header,
                )
                if (
                    self._fit_compact_context_header
                    and self._context_header is not None
                    and _is_qt_valid(self._context_header)
                ):
                    header_layout = self._context_header.layout()
                    if header_layout is not None:
                        header_layout.setAlignment(self.title_label, Qt.AlignTop)
                    self.fit_widget_height(self._context_header)
        except RuntimeError:
            pass

    def fit_label_height(
        self,
        label: QLabel,
        *,
        word_wrap: Optional[bool] = None,
        stable_measurement: bool = False,
    ) -> None:
        """Prevent a direct card label from absorbing spare vertical space."""
        if label is None or not _is_qt_valid(label):
            return
        try:
            wraps = label.wordWrap() if word_wrap is None else bool(word_wrap)
            label.setWordWrap(wraps)
            label.setProperty("stableCompactHeight", bool(stable_measurement))
            label.setMinimumWidth(0)
            label.setSizePolicy(
                QSizePolicy.Ignored if wraps else QSizePolicy.Expanding,
                QSizePolicy.Fixed,
            )
            if all(
                existing is not label for existing, _wraps in self._compact_height_labels
            ):
                self._compact_height_labels.append((label, wraps))
            else:
                self._compact_height_labels = [
                    (existing, wraps if existing is label else existing_wraps)
                    for existing, existing_wraps in self._compact_height_labels
                ]
            self._queue_compact_height_sync()
        except RuntimeError:
            pass

    def fit_widget_height(self, widget: QWidget) -> None:
        """Keep a responsive wrapper at its intrinsic height inside a card."""
        if widget is None or not _is_qt_valid(widget):
            return
        try:
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if all(existing is not widget for existing in self._compact_height_widgets):
                self._compact_height_widgets.append(widget)
            self._queue_compact_height_sync()
        except RuntimeError:
            pass

    @staticmethod
    def _fitted_label_height(label: QLabel, wraps: bool) -> int:
        metrics = label.fontMetrics()
        margins = label.contentsMargins()
        horizontal_padding = margins.left() + margins.right() + label.margin() * 2
        vertical_padding = margins.top() + margins.bottom() + label.margin() * 2
        if bool(label.property("stableCompactHeight")):
            if wraps:
                available_width = max(1, label.width() - horizontal_padding)
                fitted = metrics.boundingRect(
                    QRect(0, 0, available_width, 1_000_000),
                    Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop,
                    label.text(),
                ).height()
                if fitted > 0:
                    return max(
                        metrics.height() + vertical_padding,
                        int(fitted) + vertical_padding,
                    )
            return max(1, metrics.height() + vertical_padding)
        if wraps:
            fitted = label.heightForWidth(max(1, label.width()))
            if fitted > 0:
                return max(metrics.height(), int(fitted))
        return max(1, metrics.height())

    def _queue_compact_height_sync(self) -> None:
        if self._compact_height_sync_pending:
            return
        self._compact_height_sync_pending = True
        QTimer.singleShot(0, self._sync_compact_label_heights)

    def _sync_compact_label_heights(self) -> None:
        if not _is_qt_valid(self):
            return
        self._compact_height_sync_pending = False
        changed = False
        live_labels: list[tuple[QLabel, bool]] = []
        for label, wraps in self._compact_height_labels:
            try:
                if not _is_qt_valid(label):
                    continue
                target = self._fitted_label_height(label, wraps)
                live_labels.append((label, wraps))
                if label.minimumHeight() == target and label.maximumHeight() == target:
                    continue
                label.setMinimumHeight(target)
                label.setMaximumHeight(target)
                label.updateGeometry()
                changed = True
            except RuntimeError:
                continue
        self._compact_height_labels = live_labels
        live_widgets: list[QWidget] = []
        for widget in self._compact_height_widgets:
            try:
                if not _is_qt_valid(widget):
                    continue
                if widget is self._context_header and self._fit_compact_context_header:
                    layout = widget.layout()
                    layout_margins = (
                        layout.contentsMargins() if layout is not None else None
                    )
                    vertical_margins = (
                        layout_margins.top() + layout_margins.bottom()
                        if layout_margins is not None
                        else 0
                    )
                    title_height = (
                        self.title_label.height()
                        if self.title_label is not None and _is_qt_valid(self.title_label)
                        else 0
                    )
                    badge_height = (
                        self.context_badge.sizeHint().height()
                        if self.context_badge is not None and _is_qt_valid(self.context_badge)
                        else 0
                    )
                    target = max(title_height, badge_height) + vertical_margins
                else:
                    target = (
                        widget.heightForWidth(max(1, widget.width()))
                        if widget.hasHeightForWidth()
                        else widget.sizeHint().height()
                    )
                target = max(1, int(target))
                live_widgets.append(widget)
                if (
                    widget.minimumHeight() == target
                    and widget.maximumHeight() == target
                ):
                    continue
                widget.setMinimumHeight(target)
                widget.setMaximumHeight(target)
                widget.updateGeometry()
                changed = True
            except RuntimeError:
                continue
        self._compact_height_widgets = live_widgets
        if changed:
            try:
                if getattr(self, "v", None) is not None and _is_qt_valid(self.v):
                    self.v.invalidate()
                self.updateGeometry()
            except RuntimeError:
                pass

    def refresh_compact_heights(self) -> None:
        """Recalculate fitted labels after their text changes."""
        self._queue_compact_height_sync()

    def event(self, event):  # type: ignore[override]
        result = super().event(event)
        if (
            getattr(self, "_compact_height_labels", ())
            or getattr(self, "_compact_height_widgets", ())
        ) and event.type() in {
            QEvent.LayoutRequest,
            QEvent.FontChange,
            QEvent.StyleChange,
        }:
            self._queue_compact_height_sync()
        return result

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        try:
            if getattr(self, "v", None) is not None and _is_qt_valid(self.v):
                min_h = self.v.minimumSize().height()
                if min_h > 0 and self.minimumHeight() < min_h:
                    self.setMinimumHeight(min_h)
        except RuntimeError:
            pass
        if getattr(self, "_compact_height_labels", ()) or getattr(
            self, "_compact_height_widgets", ()
        ):
            self._queue_compact_height_sync()

    @Property(float)
    def hoverProgress(self) -> float:
        return self._hover_progress

    @hoverProgress.setter
    def hoverProgress(self, value: float) -> None:
        self._hover_progress = max(0.0, min(1.0, float(value)))
        self.update()

    def _current_theme(self) -> Theme:
        return theme_for_widget(self)

    def _current_accent(self, theme: Theme) -> QColor:
        if self._uses_theme_accent:
            return _safe_color(getattr(theme, "primary_color", "#0A84FF"))
        return QColor(self._accent)

    def paintEvent(self, event):  # type: ignore[override]
        if self.width() <= 0 or self.height() <= 0:
            return
        theme = self._current_theme()
        dark = bool(theme.dark)
        accent = self._current_accent(theme)
        hover = self._hover_progress
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = self.rect().adjusted(2, 2, -2, -3)
        radius = min(24.0, max(12.0, min(rect.width(), rect.height()) / 3.0))

        # A restrained internal shadow avoids QGraphicsEffect conflicts and stays
        # stable with translucent child widgets.
        shadow = QColor(2, 8, 23, int((16 if dark else 24) + hover * 18))
        painter.setPen(Qt.NoPen)
        painter.setBrush(shadow)
        painter.drawRoundedRect(rect.translated(0, 2), radius, radius)

        if dark:
            base = (
                QColor(45, 55, 76, 241)
                if self._glass_strong
                else QColor(32, 39, 56, 227)
            )
            neutral_border = QColor(255, 255, 255, 38)
            highlight = QColor(255, 255, 255, int(18 + hover * 22))
        else:
            base = (
                QColor(255, 255, 255, 234)
                if self._glass_strong
                else QColor(255, 255, 255, 220)
            )
            neutral_border = QColor(255, 255, 255, 242)
            highlight = QColor(255, 255, 255, int(115 + hover * 90))

        fill_gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        fill_gradient.setColorAt(0.0, _mix(base, accent, 0.018 + hover * 0.035))
        fill_gradient.setColorAt(0.58, base)
        fill_gradient.setColorAt(1.0, _mix(base, accent, 0.035 + hover * 0.060))

        accent_border = QColor(accent)
        accent_border.setAlpha(int(22 + hover * (66 if dark else 48)))
        border_gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        border_gradient.setColorAt(0.0, highlight)
        border_gradient.setColorAt(0.48, neutral_border)
        border_gradient.setColorAt(1.0, accent_border)
        painter.setPen(QPen(QBrush(border_gradient), 1.0 + hover * 0.35))
        painter.setBrush(QBrush(fill_gradient))
        painter.drawRoundedRect(rect, radius, radius)

        # A small, cursor-aware accent glint gives feedback without moving content.
        glint_center = max(0.18, min(0.82, 0.50 + self._mouse_offset_x * 0.20))
        glint = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.top())
        transparent = QColor(accent)
        transparent.setAlpha(0)
        glint_color = QColor(accent)
        glint_color.setAlpha(int(18 + hover * 50))
        glint.setColorAt(0.0, transparent)
        glint.setColorAt(glint_center, glint_color)
        glint.setColorAt(1.0, transparent)
        painter.setPen(QPen(QBrush(glint), 1.1 + hover * 0.7))
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(
            QPointF(rect.left() + radius, rect.top() + 1),
            QPointF(rect.right() - radius, rect.top() + 1),
        )
        painter.end()

    def _animate_hover(self, target: float) -> None:
        self._anim.stop()
        self._anim.setDirection(QAbstractAnimation.Forward)
        self._anim.setStartValue(self._hover_progress)
        self._anim.setEndValue(float(target))
        self._anim.start()

    def enterEvent(self, event):  # type: ignore[override]
        super().enterEvent(event)
        self._animate_hover(1.0)

    def leaveEvent(self, event):  # type: ignore[override]
        super().leaveEvent(event)
        self._mouse_offset_x = 0.0
        self._mouse_offset_y = 0.0
        self._animate_hover(0.0)

    def mouseMoveEvent(self, event):  # type: ignore[override]
        super().mouseMoveEvent(event)
        half_width, half_height = self.width() / 2, self.height() / 2
        if half_width > 0 and half_height > 0:
            self._mouse_offset_x = (event.position().x() - half_width) / half_width
            self._mouse_offset_y = (event.position().y() - half_height) / half_height
        if self._hover_progress > 0:
            self.update()

    def repo(self) -> Any:
        """Find the application database repository if connected."""
        card_repo = getattr(self, "_repo", None)
        if card_repo is not None:
            return card_repo
        parent = self.parentWidget()
        while parent is not None:
            parent_repo = getattr(parent, "repo", None)
            if parent_repo is not None:
                return parent_repo
            app_repo = getattr(getattr(parent, "app", None), "repo", None)
            if app_repo is not None:
                return app_repo
            parent = parent.parentWidget()
        win = self.window()
        if win is not None and win is not self:
            win_repo = getattr(win, "repo", None)
            if win_repo is not None:
                return win_repo
            app_repo = getattr(getattr(win, "app", None), "repo", None)
            if app_repo is not None:
                return app_repo
        return None

    def _resolve_repo(self) -> Any:
        repo_attr = getattr(self, "repo", None)
        if callable(repo_attr):
            return repo_attr()
        return repo_attr

    def card_key(self) -> str:
        """Return the unique configuration key for this card."""
        try:
            key = str(self.property("pageCardKey") or "").strip()
            if key:
                return key
        except RuntimeError:
            return ""
        title = self.card_title()
        if title:
            import re
            import unicodedata

            norm = unicodedata.normalize("NFKD", title)
            ascii_txt = norm.encode("ascii", "ignore").decode("ascii")
            slug = re.sub(r"[^a-z0-9]+", "_", ascii_txt.casefold()).strip("_")
            return slug or "card"
        return ""

    def phrase_setting_key(self) -> str:
        key = self.card_key()
        return f"card_phrases:{key}" if key else ""

    def phrase_preferences(self) -> dict[str, Any]:
        default = {
            "show_description": True,
            "custom_description": "",
            "show_comparison": True,
            "show_footer": True,
            "custom_footer": "",
            "show_data_coverage": True,
            "show_confidence_badge": True,
            "show_context_badge": True,
            "show_legend": True,
            "show_delta": True,
            "show_sparkline": True,
            "show_status": True,
            "show_work_metrics": True,
        }
        repo = self._resolve_repo()
        setting_key = self.phrase_setting_key()
        if repo is None or not setting_key:
            return dict(default)
        try:
            raw = read_setting(repo, setting_key)
            if not raw:
                return dict(default)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                res = dict(default)
                for k, v in parsed.items():
                    if k.startswith("custom_"):
                        res[k] = str(v or "").strip()
                    elif k.startswith("show_"):
                        res[k] = bool(v)
                    else:
                        res[k] = v
                return res
        except Exception:
            pass
        return dict(default)

    def save_phrase_preferences(
        self, prefs: dict[str, Any], *, overwrite: bool = False
    ) -> None:
        repo = self._resolve_repo()
        setting_key = self.phrase_setting_key()
        if repo is None or not setting_key:
            return
        try:
            if overwrite or not prefs:
                to_save = dict(prefs)
            else:
                current = self.phrase_preferences()
                current.update(prefs)
                to_save = current
            repo.set_setting(setting_key, json.dumps(to_save, ensure_ascii=False))
            invalidate_setting_read(repo, setting_key)
        except Exception:
            pass

    def reset_phrase_preferences(self) -> None:
        """Restore all phrase and element preferences for this card to default."""
        self.save_phrase_preferences({}, overwrite=True)
        self.apply_phrase_preferences()

    @staticmethod
    def _safe_widget_text(widget: object, default: str = "") -> str:
        if widget is None or not _is_qt_valid(widget):
            return default
        try:
            return str(getattr(widget, "text", lambda: default)() or default)
        except (AttributeError, RuntimeError):
            return default

    def available_customization_elements(self) -> dict[str, dict[str, Any]]:
        """Intelligently inspect and return all customizable elements currently present on this card."""
        elements: dict[str, dict[str, Any]] = {}
        prefs = self.phrase_preferences()

        # 1. Base Real / Data Coverage
        coverage_text = getattr(self, "_original_data_coverage", "")
        if not coverage_text:
            coverage_text = self._safe_widget_text(self.data_coverage_label)
        if coverage_text or (
            self.data_coverage_label is not None and _is_qt_valid(self.data_coverage_label)
        ):
            elements["data_coverage"] = {
                "key": "show_data_coverage",
                "title": "Base real dos dados",
                "preview": coverage_text,
                "icon": "📐",
                "enabled": bool(prefs.get("show_data_coverage", True)),
                "supports_text": False,
            }

        # 2. Main Explanatory Phrase / Subtitle
        desc_text = getattr(self, "_original_description", "")
        if not desc_text:
            desc_text = self._safe_widget_text(self.description_label)
        intro_widget = getattr(self, "intro", None)
        if not desc_text and intro_widget is not None:
            desc_text = self._safe_widget_text(intro_widget)
        has_desc = (
            desc_text
            or (self.description_label is not None and _is_qt_valid(self.description_label))
            or (intro_widget is not None and _is_qt_valid(intro_widget))
        )
        if has_desc:
            custom_desc = str(prefs.get("custom_description", "") or "").strip()
            elements["description"] = {
                "key": "show_description",
                "title": "Frase explicativa",
                "preview": desc_text,
                "custom_key": "custom_description",
                "custom_text": custom_desc,
                "icon": "💬",
                "enabled": bool(prefs.get("show_description", True)),
                "supports_text": True,
            }

        # 3. Confidence Badge
        conf_text = getattr(self, "_original_confidence_badge", "")
        if not conf_text:
            conf_text = self._safe_widget_text(self.confidence_badge)
        if conf_text or (
            self.confidence_badge is not None and _is_qt_valid(self.confidence_badge)
        ):
            elements["confidence_badge"] = {
                "key": "show_confidence_badge",
                "title": "Confiança da previsão",
                "preview": conf_text,
                "icon": "🎯",
                "enabled": bool(prefs.get("show_confidence_badge", True)),
                "supports_text": False,
            }

        # 4. Context Badge
        ctx_text = getattr(self, "_original_context_badge", "")
        if not ctx_text:
            ctx_text = self._safe_widget_text(self.context_badge)
        if ctx_text or (
            self.context_badge is not None and _is_qt_valid(self.context_badge)
        ):
            elements["context_badge"] = {
                "key": "show_context_badge",
                "title": "Contexto temporal",
                "preview": ctx_text,
                "icon": "🗓️",
                "enabled": bool(prefs.get("show_context_badge", True)),
                "supports_text": False,
            }

        # 5. Comparison note
        has_comparison = hasattr(self, "comparison_hint") or hasattr(
            self, "_comparison_text"
        )
        comp_text = getattr(self, "_comparison_text", "")
        if not comp_text and hasattr(self, "comparison_hint"):
            comp_text = self._safe_widget_text(getattr(self, "comparison_hint"))
        if has_comparison or comp_text:
            elements["comparison"] = {
                "key": "show_comparison",
                "title": "Nota de comparação",
                "preview": comp_text,
                "icon": "📊",
                "enabled": bool(prefs.get("show_comparison", True)),
                "supports_text": False,
            }

        # 6. Footer / Methodology
        footer_text = getattr(self, "_original_footer", "")
        if not footer_text:
            footer_text = self._safe_widget_text(self.footer_label)
        method_widget = getattr(self, "method", None)
        if not footer_text and method_widget is not None:
            footer_text = self._safe_widget_text(method_widget)
        has_footer_widget = (
            footer_text
            or (self.footer_label is not None and _is_qt_valid(self.footer_label))
            or (method_widget is not None and _is_qt_valid(method_widget))
        )
        if has_footer_widget:
            custom_foot = str(prefs.get("custom_footer", "") or "").strip()
            elements["footer"] = {
                "key": "show_footer",
                "title": "Rodapé / metodologia",
                "preview": footer_text,
                "custom_key": "custom_footer",
                "custom_text": custom_foot,
                "icon": "📌",
                "enabled": bool(prefs.get("show_footer", True)),
                "supports_text": True,
            }

        # 7. Legend
        leg_text = getattr(self, "_original_legend", "")
        if not leg_text:
            leg_text = self._safe_widget_text(self.legend_label)
        if leg_text or (
            self.legend_label is not None and _is_qt_valid(self.legend_label)
        ):
            elements["legend"] = {
                "key": "show_legend",
                "title": "Legenda explicativa",
                "preview": leg_text,
                "icon": "🏷️",
                "enabled": bool(prefs.get("show_legend", True)),
                "supports_text": False,
            }

        return elements

    def apply_phrase_preferences(self) -> None:
        prefs = self.phrase_preferences()
        show_desc = bool(prefs.get("show_description", True))
        custom_desc = str(prefs.get("custom_description", "") or "").strip()
        show_footer = bool(prefs.get("show_footer", True))
        custom_footer = str(prefs.get("custom_footer", "") or "").strip()

        if self.description_label is not None and _is_qt_valid(self.description_label):
            try:
                if not show_desc:
                    self.description_label.setVisible(False)
                else:
                    text = custom_desc if custom_desc else self._original_description
                    if text:
                        self.description_label.setText(text)
                        self.description_label.setVisible(True)
                        self.fit_label_height(
                            self.description_label,
                            word_wrap=True,
                            stable_measurement=True,
                        )
                    else:
                        self.description_label.setVisible(False)
            except RuntimeError:
                pass

        if self.footer_label is not None and _is_qt_valid(self.footer_label):
            try:
                if not show_footer:
                    self.footer_label.setVisible(False)
                else:
                    text = custom_footer if custom_footer else self._original_footer
                    if text:
                        self.footer_label.setText(text)
                        self.footer_label.setVisible(True)
                        self.fit_label_height(
                            self.footer_label,
                            word_wrap=True,
                            stable_measurement=True,
                        )
                    else:
                        self.footer_label.setVisible(False)
            except RuntimeError:
                pass

        if self.data_coverage_label is not None and _is_qt_valid(self.data_coverage_label):
            try:
                show_coverage = bool(prefs.get("show_data_coverage", True))
                coverage_text = (
                    getattr(self, "_original_data_coverage", "")
                    or self.data_coverage_label.text()
                )
                self.data_coverage_label.setVisible(
                    show_coverage and bool(coverage_text.strip())
                )
            except RuntimeError:
                pass

        if self.confidence_badge is not None and _is_qt_valid(self.confidence_badge):
            try:
                show_conf = bool(prefs.get("show_confidence_badge", True))
                conf_text = (
                    getattr(self, "_original_confidence_badge", "")
                    or self.confidence_badge.text()
                )
                self.confidence_badge.setVisible(show_conf and bool(conf_text.strip()))
            except RuntimeError:
                pass

        if self.context_badge is not None and _is_qt_valid(self.context_badge):
            try:
                show_ctx = bool(prefs.get("show_context_badge", True))
                ctx_text = (
                    getattr(self, "_original_context_badge", "")
                    or self.context_badge.text()
                )
                self.context_badge.setVisible(show_ctx and bool(ctx_text.strip()))
                if self._context_header is not None and _is_qt_valid(self._context_header):
                    self._context_header.setVisible(
                        (show_ctx and bool(ctx_text.strip()))
                        or (self.title_label is not None and _is_qt_valid(self.title_label))
                    )
                    self._context_header.updateGeometry()
            except RuntimeError:
                pass

        if self.legend_label is not None and _is_qt_valid(self.legend_label):
            try:
                show_leg = bool(prefs.get("show_legend", True))
                leg_text = (
                    getattr(self, "_original_legend", "") or self.legend_label.text()
                )
                self.legend_label.setVisible(show_leg and bool(leg_text.strip()))
            except RuntimeError:
                pass

        try:
            self._apply_custom_phrase_elements(prefs)
            self.updateGeometry()
        except RuntimeError:
            pass

    def _apply_custom_phrase_elements(self, prefs: dict[str, Any]) -> None:
        """Hook for subclasses to handle bespoke phrase widgets."""
        pass

    def show_phrase_dialog(self) -> None:
        from ui.card_phrase_dialog import CardPhraseCustomizationDialog

        title = self.card_title() or self.card_key() or "Card"
        prefs = self.phrase_preferences()
        elements = self.available_customization_elements()
        dialog = CardPhraseCustomizationDialog(
            card_title=title,
            preferences=prefs,
            card=self,
            elements=elements,
            parent=self,
        )
        if dialog.exec():
            new_state = dialog.state()
            self.save_phrase_preferences(new_state)
            self.apply_phrase_preferences()

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        self.apply_phrase_preferences()

    def create_context_menu(self) -> QMenu:
        menu = QMenu(self)
        apply_menu_theme(menu, self._current_theme())

        title = self.card_title() or "Card"
        prefs = self.phrase_preferences()

        title_action = menu.addAction(f"💬 Frases · {title}")
        title_action.setEnabled(False)
        menu.addSeparator()

        elements = self.available_customization_elements()
        for _elem_id, info in elements.items():
            key = info["key"]
            elem_title = info["title"]
            icon = info.get("icon", "👁️")
            action = menu.addAction(f"{icon} Mostrar {elem_title.lower()}")
            action.setCheckable(True)
            action.setChecked(bool(prefs.get(key, True)))

            def make_toggle(pref_key: str, act) -> Any:
                def toggle() -> None:
                    updated = dict(self.phrase_preferences())
                    updated[pref_key] = act.isChecked()
                    self.save_phrase_preferences(updated)
                    self.apply_phrase_preferences()
                return toggle

            action.triggered.connect(make_toggle(key, action))

        menu.addSeparator()

        edit_action = menu.addAction("✏️ Personalizar texto da frase…")
        edit_action.triggered.connect(self.show_phrase_dialog)

        reset_action = menu.addAction("🔄 Restaurar frase original")

        def reset_phrases() -> None:
            self.reset_phrase_preferences()

        reset_action.triggered.connect(reset_phrases)

        menu.addSeparator()
        page_action = menu.addAction("🧩 Personalizar cards desta página…")

        def open_page_cards() -> None:
            p = self.parentWidget()
            while p is not None:
                if hasattr(p, "open_card_customization") and callable(
                    p.open_card_customization
                ):
                    p.open_card_customization()
                    return
                p = p.parentWidget()

        page_action.triggered.connect(open_page_cards)
        return menu

    def contextMenuEvent(self, event) -> None:
        menu = self.create_context_menu()
        menu.exec(event.globalPos())


class _ResponsiveMetricLabel(QLabel):
    """Metric value that scales down safely and only elides as a last resort."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._full_text = ""
        self._fitting = False
        self._base_pixel_size = 22
        self._minimum_pixel_size = 13
        self.setObjectName("Metric")
        self.setMinimumWidth(0)
        self.setMinimumHeight(30)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        font = QFont("Segoe UI")
        font.setPixelSize(self._base_pixel_size)
        font.setWeight(QFont.Weight.ExtraBold)
        self.setFont(font)
        self.setText(text)

    def fullText(self) -> str:
        return self._full_text

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full_text = str(text or "")
        super().setText(self._full_text)
        self._fit_text()

    def _fit_text(self) -> None:
        if self._fitting or self.width() <= 8:
            return
        self._fitting = True
        try:
            available = max(12, self.contentsRect().width() - 2)
            font = QFont(self.font())
            display = self._full_text
            for size in range(self._base_pixel_size, self._minimum_pixel_size - 1, -1):
                font.setPixelSize(size)
                if QFontMetrics(font).horizontalAdvance(self._full_text) <= available:
                    break
            metrics = QFontMetrics(font)
            if metrics.horizontalAdvance(display) > available:
                display = metrics.elidedText(display, Qt.ElideRight, available)
            self.setFont(font)
            super().setText(display)
            self.setToolTip(self._full_text if display != self._full_text else "")
        finally:
            self._fitting = False

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        self._fit_text()

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        self._fit_text()


class _MetricSparkline(QWidget):
    """Small theme-aware trend line used inside metric cards."""

    def __init__(self, accent: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.accent = _safe_color(accent)
        self.values: list[float] = []
        self.trend_label = ""
        self.setFixedHeight(38)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("Tendência do indicador")

    def set_values(self, values: object, trend_label: str = "") -> None:
        clean: list[float] = []
        if isinstance(values, (list, tuple)):
            for value in values:
                try:
                    clean.append(float(value))
                except (TypeError, ValueError):
                    continue
        self.values = clean
        self.trend_label = str(trend_label or "")
        self.setVisible(len(clean) >= 2)
        self.setAccessibleDescription(
            self.trend_label
            or (
                f"Evolução visual com {len(clean)} pontos" if clean else "Sem tendência"
            )
        )
        self.update()

    def paintEvent(self, event):  # type: ignore[override]
        if len(self.values) < 2 or self.width() < 20:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        theme = theme_for_widget(self)
        plot = QRectF(self.rect()).adjusted(2.0, 4.0, -2.0, -4.0)
        low = min(self.values)
        high = max(self.values)
        if abs(high - low) < 1e-9:
            padding = max(1.0, abs(high) * 0.04)
            low -= padding
            high += padding
        else:
            padding = (high - low) * 0.12
            low -= padding
            high += padding

        def point(index: int, value: float) -> QPointF:
            x = plot.left() + plot.width() * index / max(1, len(self.values) - 1)
            y = plot.bottom() - plot.height() * (value - low) / max(1e-9, high - low)
            return QPointF(x, y)

        points = [point(index, value) for index, value in enumerate(self.values)]

        guide = QColor(theme.muted)
        guide.setAlpha(34 if theme.dark else 28)
        painter.setPen(QPen(guide, 1))
        painter.drawLine(
            plot.left(), plot.center().y(), plot.right(), plot.center().y()
        )
        if low < 0 < high:
            zero_y = point(0, 0.0).y()
            zero = QColor(theme.muted)
            zero.setAlpha(76 if theme.dark else 62)
            painter.setPen(QPen(zero, 1, Qt.DashLine))
            painter.drawLine(plot.left(), zero_y, plot.right(), zero_y)

        line = QPainterPath(points[0])
        for previous, current in zip(points, points[1:]):
            middle_x = (previous.x() + current.x()) / 2.0
            line.cubicTo(
                QPointF(middle_x, previous.y()),
                QPointF(middle_x, current.y()),
                current,
            )

        area = QPainterPath(line)
        area.lineTo(points[-1].x(), plot.bottom())
        area.lineTo(points[0].x(), plot.bottom())
        area.closeSubpath()
        fill = QLinearGradient(0, plot.top(), 0, plot.bottom())
        fill_top = QColor(self.accent)
        fill_top.setAlpha(92 if theme.dark else 72)
        fill_bottom = QColor(self.accent)
        fill_bottom.setAlpha(4)
        fill.setColorAt(0.0, fill_top)
        fill.setColorAt(1.0, fill_bottom)
        painter.fillPath(area, fill)

        line_color = QColor(self.accent)
        line_color.setAlpha(238)
        painter.setPen(QPen(line_color, 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(line)

        halo = QColor(self.accent)
        halo.setAlpha(48)
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(points[-1], 5.5, 5.5)
        painter.setBrush(line_color)
        painter.drawEllipse(points[-1], 2.7, 2.7)
        painter.end()


class MetricCard(GlassCard):
    clicked = Signal()

    def __init__(
        self,
        title: str,
        value: str,
        delta: str,
        accent: str,
        icon: str,
        *,
        status: str = "",
        tone: str = "neutral",
        progress: Optional[float] = None,
        trend: Optional[list[float]] = None,
        trend_label: str = "",
        clickable: bool = False,
        action_hint: str = "",
        accessible_text: str = "",
        variant: str = "primary",
        parent: Optional[QWidget] = None,
    ):
        safe_accent = _safe_color(accent)
        super().__init__(strong=False, parent=parent, accent=safe_accent.name())
        self.setProperty("metricCard", True)
        self.variant = str(variant or "primary")
        self.setProperty("metricVariant", self.variant)
        self.accent = safe_accent.name()
        self._original_title = str(title or "").strip()
        self.tone = _normalise_tone(tone)
        self._explicit_accessible_text = str(accessible_text or "")
        self._clickable = False
        self._action_hint = str(action_hint or "")

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        self.title_label = QLabel(str(title or ""))
        self.title_label.setObjectName("MetricTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.icon_badge = QLabel(str(icon or ""))
        self.icon_badge.setObjectName("MetricIconBadge")
        self.icon_badge.setAlignment(Qt.AlignCenter)
        self.icon_badge.setFixedSize(42, 42)
        emoji_font = QFont("Segoe UI Emoji")
        emoji_font.setPixelSize(19)
        self.icon_badge.setFont(emoji_font)
        self.icon_badge.setAccessibleName(f"Ícone de {title}")
        self.icon_badge.setStyleSheet(
            "background-color: "
            f"{_rgba_css(safe_accent, 34)}; color: {safe_accent.name()}; "
            f"border: 1px solid {_rgba_css(safe_accent, 60)}; "
            "border-radius: 14px;"
        )
        header.addWidget(self.title_label, 1)
        header.addWidget(self.icon_badge, 0, Qt.AlignTop)
        self.v.addLayout(header)

        self.value_label = _ResponsiveMetricLabel(str(value or ""))
        self.v.addWidget(self.value_label)

        self.delta_label = QLabel(str(delta or ""))
        self.delta_label.setObjectName("MetricDelta")
        self.delta_label.setWordWrap(True)
        self.delta_label.setStyleSheet(
            f"color: {safe_accent.name()}; font-weight: 750; background: transparent;"
        )
        self.v.addWidget(self.delta_label)

        self.sparkline = _MetricSparkline(safe_accent.name(), self)
        self.sparkline.set_values(trend or [], trend_label)
        self.v.addWidget(self.sparkline)

        self.status_label = QLabel()
        self.status_label.setObjectName("MetricStatus")
        self.status_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Preferred,
        )
        self.v.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("MetricProgress")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(7)
        self.v.addWidget(self.progress_bar)

        self.set_data(
            value=str(value or ""),
            delta=str(delta or ""),
            status=str(status or ""),
            tone=self.tone,
            progress=progress,
            trend=trend,
            trend_label=trend_label,
            accessible_text=accessible_text,
        )
        self.set_clickable(clickable, action_hint)

    def set_data(
        self,
        *,
        value: Optional[str] = None,
        delta: Optional[str] = None,
        status: Optional[str] = None,
        tone: Optional[str] = None,
        progress: object = _UNSET,
        trend: object = _UNSET,
        trend_label: Optional[str] = None,
        accessible_text: Optional[str] = None,
    ) -> None:
        if value is not None:
            val_w = getattr(self, "value_label", None)
            if val_w is not None and _is_qt_valid(val_w):
                try:
                    val_w.setText(str(value))
                except RuntimeError:
                    pass
        if delta is not None:
            delta_w = getattr(self, "delta_label", None)
            if delta_w is not None and _is_qt_valid(delta_w):
                try:
                    delta_w.setText(str(delta))
                except RuntimeError:
                    pass
        if status is not None:
            stat_w = getattr(self, "status_label", None)
            if stat_w is not None and _is_qt_valid(stat_w):
                try:
                    stat_w.setText(str(status))
                    stat_w.setToolTip(str(status) if str(status).strip() else "")
                    stat_w.setVisible(bool(str(status).strip()))
                except RuntimeError:
                    pass
        if tone is not None:
            self.tone = _normalise_tone(tone)
        _apply_tone(getattr(self, "status_label", None), self.tone)
        _apply_tone(getattr(self, "progress_bar", None), self.tone)

        if progress is not _UNSET:
            normalised = _normalise_progress(progress)
            prog_w = getattr(self, "progress_bar", None)
            if prog_w is not None and _is_qt_valid(prog_w):
                try:
                    prog_w.setVisible(normalised is not None)
                    if normalised is not None:
                        prog_w.setValue(normalised)
                except RuntimeError:
                    pass

        spark_w = getattr(self, "sparkline", None)
        if trend is not _UNSET:
            if spark_w is not None and _is_qt_valid(spark_w):
                try:
                    spark_w.set_values(
                        trend,
                        spark_w.trend_label if trend_label is None else trend_label,
                    )
                except RuntimeError:
                    pass
        elif trend_label is not None:
            if spark_w is not None and _is_qt_valid(spark_w):
                try:
                    spark_w.set_values(spark_w.values, trend_label)
                except RuntimeError:
                    pass

        if accessible_text is not None:
            self._explicit_accessible_text = str(accessible_text or "")
        title_text = self._safe_widget_text(getattr(self, "title_label", None))
        val_text = ""
        val_lbl = getattr(self, "value_label", None)
        if val_lbl is not None and _is_qt_valid(val_lbl):
            try:
                val_text = val_lbl.fullText()
            except (AttributeError, RuntimeError):
                val_text = ""
        delta_text = self._safe_widget_text(getattr(self, "delta_label", None))
        status_text = ""
        stat_lbl = getattr(self, "status_label", None)
        if stat_lbl is not None and _is_qt_valid(stat_lbl):
            try:
                status_text = stat_lbl.text() if stat_lbl.isVisible() else ""
            except RuntimeError:
                status_text = ""
        generated = ". ".join(
            part
            for part in (
                title_text,
                val_text,
                delta_text,
                status_text,
            )
            if part
        )
        try:
            self.setAccessibleName(self._explicit_accessible_text or generated)
        except RuntimeError:
            pass
        trend_text = ""
        spark = getattr(self, "sparkline", None)
        if spark is not None and _is_qt_valid(spark):
            try:
                trend_text = spark.trend_label if not spark.isHidden() else ""
            except RuntimeError:
                trend_text = ""
        description = ". ".join(
            part
            for part in (
                delta_text,
                trend_text,
                self._action_hint if getattr(self, "_clickable", False) else "",
            )
            if part
        )
        try:
            self.setAccessibleDescription(description)
        except RuntimeError:
            pass
        self._apply_custom_phrase_elements(self.phrase_preferences())

    def available_customization_elements(self) -> dict[str, dict[str, Any]]:
        elements = super().available_customization_elements()
        prefs = self.phrase_preferences()

        delta_lbl = getattr(self, "delta_label", None)
        if delta_lbl is not None and _is_qt_valid(delta_lbl):
            elements["delta"] = {
                "key": "show_delta",
                "title": "Variação / delta percentual",
                "preview": self._safe_widget_text(delta_lbl),
                "icon": "🔺",
                "enabled": bool(prefs.get("show_delta", True)),
                "supports_text": False,
            }

        spark = getattr(self, "sparkline", None)
        if spark is not None and _is_qt_valid(spark):
            preview = ""
            try:
                preview = getattr(spark, "trend_label", "") or "Evolução temporal"
            except RuntimeError:
                preview = "Evolução temporal"
            elements["sparkline"] = {
                "key": "show_sparkline",
                "title": "Mini-gráfico de tendência (sparkline)",
                "preview": preview,
                "icon": "📈",
                "enabled": bool(prefs.get("show_sparkline", True)),
                "supports_text": False,
            }

        stat_lbl = getattr(self, "status_label", None)
        if stat_lbl is not None and _is_qt_valid(stat_lbl):
            elements["metric_status"] = {
                "key": "show_status",
                "title": "Frase de estado da métrica",
                "preview": self._safe_widget_text(stat_lbl),
                "icon": "💬",
                "enabled": bool(prefs.get("show_status", True)),
                "supports_text": False,
            }

        return elements

    def _apply_custom_phrase_elements(self, prefs: dict[str, Any]) -> None:
        super()._apply_custom_phrase_elements(prefs)
        delta_lbl = getattr(self, "delta_label", None)
        if delta_lbl is not None and _is_qt_valid(delta_lbl):
            try:
                show_delta = bool(prefs.get("show_delta", True))
                text = self._safe_widget_text(delta_lbl).strip()
                delta_lbl.setVisible(show_delta and bool(text))
            except RuntimeError:
                pass
        spark = getattr(self, "sparkline", None)
        if spark is not None and _is_qt_valid(spark):
            try:
                show_sparkline = bool(prefs.get("show_sparkline", True))
                if not show_sparkline:
                    spark.setVisible(False)
                else:
                    spark.setVisible(
                        len(getattr(spark, "values", [])) >= 2
                    )
            except RuntimeError:
                pass
        stat_lbl = getattr(self, "status_label", None)
        if stat_lbl is not None and _is_qt_valid(stat_lbl):
            try:
                show_status = bool(prefs.get("show_status", True))
                text = self._safe_widget_text(stat_lbl).strip()
                stat_lbl.setVisible(show_status and bool(text))
            except RuntimeError:
                pass

    def set_clickable(self, clickable: bool, action_hint: str = "") -> None:
        self._clickable = bool(clickable)
        if action_hint:
            self._action_hint = str(action_hint)
        try:
            self.setCursor(Qt.PointingHandCursor if self._clickable else Qt.ArrowCursor)
            self.setFocusPolicy(Qt.StrongFocus if self._clickable else Qt.NoFocus)
            for child in self.findChildren(QWidget):
                if _is_qt_valid(child):
                    try:
                        child.setAttribute(Qt.WA_TransparentForMouseEvents, self._clickable)
                    except RuntimeError:
                        pass
            self.setToolTip(self._action_hint if self._clickable else "")
            self.setProperty("clickable", self._clickable)
        except RuntimeError:
            pass
        self.set_data()

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        if (
            self._clickable
            and event.button() == Qt.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):  # type: ignore[override]
        if self._clickable and event.key() in (
            Qt.Key_Return,
            Qt.Key_Enter,
            Qt.Key_Space,
        ):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)
