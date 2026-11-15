import colorsys
import hashlib
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox, QToolButton

from ui.core.theme import Theme, apply_menu_theme, theme_for_widget
from utils.config import parse_date


def compact_value_items(
    items: Any,
    visible_limit: int | None,
    other_label: str = "Outros",
) -> list[tuple[str, float]]:
    """Keep the largest values visible and preserve every remainder in one row."""
    clean: list[tuple[str, float]] = []
    for label, raw_value in items or []:
        try:
            value = float(raw_value or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            clean.append((str(label or "Sem categoria"), value))
    ordered = sorted(clean, key=lambda item: item[1], reverse=True)
    if visible_limit is None:
        return ordered
    limit = max(1, int(visible_limit))
    if len(ordered) <= limit:
        return ordered
    visible = ordered[:limit]
    remainder = ordered[limit:]
    visible.append(
        (
            f"{other_label} ({len(remainder)})",
            sum(value for _label, value in remainder),
        )
    )
    return visible


_CATEGORY_COLOURS = {
    "alimentacao": "#0072B2",
    "supermercado": "#009E73",
    "restaurantes": "#E69F00",
    "casa e contas": "#6F42C1",
    "transporte": "#56B4E9",
    "saude": "#D55E00",
    "educacao": "#CC79A7",
    "lazer": "#F97316",
    "subscricoes": "#0F766E",
    "desporto": "#84CC16",
    "animais": "#8B5CF6",
    "higiene": "#06B6D4",
    "compras grandes": "#DC2626",
    "investimentos": "#2563EB",
    "rendimentos": "#16803A",
    "reembolsos": "#14B8A6",
    "diversos": "#64748B",
    "outros": "#64748B",
}


def _category_key(label: Any) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(label or "Sem categoria"))
    return "".join(char for char in text if not unicodedata.combining(char)).casefold()


def category_color(label: Any) -> str:
    """Return one stable category colour shared by every custom chart."""
    key = _category_key(label)
    known = _CATEGORY_COLOURS.get(key)
    if known:
        return known
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535.0
    saturation = 0.62 + (digest[2] / 255.0) * 0.16
    lightness = 0.42 + (digest[3] / 255.0) * 0.12
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return QColor(round(red * 255), round(green * 255), round(blue * 255)).name()


def _widget_theme(
    widget, theme_provider: Optional[Callable[[], Theme]] = None
) -> Theme:
    provider = theme_provider or getattr(widget, "theme_provider", None)
    if callable(provider):
        try:
            theme = provider()
            if isinstance(theme, Theme):
                return theme
        except (AttributeError, RuntimeError, TypeError):
            pass
    return theme_for_widget(widget)


def enable_export(widget, theme_provider: Optional[Callable[[], Theme]] = None):
    """Attach the standard export/reset menu and its visible action button."""
    if bool(widget.property("chartExportEnabled")):
        return getattr(widget, "_chart_action_button", None)
    widget.setProperty("chartExportEnabled", True)
    widget.setContextMenuPolicy(Qt.CustomContextMenu)

    def on_context(pos):
        menu = QMenu(widget)
        theme = _widget_theme(widget, theme_provider)
        apply_menu_theme(menu, theme)
        detail_action = None
        if hasattr(widget, "open_selected_detail"):
            detail_action = menu.addAction("Ver detalhes do ponto selecionado")
            has_detail = getattr(widget, "has_detail_data", None)
            if callable(has_detail):
                detail_action.setEnabled(bool(has_detail()))
            menu.addSeparator()
        focus_action = None
        if hasattr(widget, "open_focus_view"):
            focus_action = menu.addAction("Abrir em modo de foco")
            menu.addSeparator()
        reset_action = None
        if hasattr(widget, "reset_zoom"):
            reset_action = menu.addAction("Repor zoom")
            if not bool(getattr(widget, "supports_zoom", False)):
                reset_action.setEnabled(False)
            menu.addSeparator()
        export_action = menu.addAction("Guardar gráfico como PNG…")
        action = menu.exec(widget.mapToGlobal(pos))
        if detail_action is not None and action == detail_action:
            widget.open_selected_detail()
        elif focus_action is not None and action == focus_action:
            widget.open_focus_view()
        elif reset_action is not None and action == reset_action:
            widget.reset_zoom()
        elif action == export_action:
            suggested_name = type(widget).__name__.replace("Chart", "").lower()
            path, _ = QFileDialog.getSaveFileName(
                widget,
                "Exportar gráfico",
                str(Path.home() / f"{suggested_name or 'grafico'}.png"),
                "Imagens (*.png)",
            )
            if path:
                if not path.lower().endswith(".png"):
                    path += ".png"
                if widget.grab().save(path, "PNG"):
                    QMessageBox.information(
                        widget, "Exportado", f"Gráfico guardado em:\n{path}"
                    )
                else:
                    QMessageBox.warning(
                        widget,
                        "Exportar gráfico",
                        "Não foi possível guardar a imagem.",
                    )

    widget.customContextMenuRequested.connect(on_context)
    button = QToolButton(widget)
    button.setObjectName("ChartActionButton")
    button.setText("⋯")
    button.setFixedSize(30, 28)
    button.setCursor(Qt.PointingHandCursor)
    button.setAccessibleName("Ações do gráfico")
    button.setToolTip("Ver detalhes, abrir em foco, exportar ou repor zoom")
    button.clicked.connect(lambda: on_context(QPoint(max(0, widget.width() - 34), 30)))
    button.show()
    widget._chart_action_button = button
    return button


def _safe_chart_date(value: Any) -> date:
    return parse_date(str(value)[:10])


def _chart_bg(theme: Theme) -> QColor:
    if theme.dark:
        return QColor(15, 23, 42, 178)
    return QColor(255, 255, 255, 132)


def _chart_text(theme: Theme) -> QColor:
    return QColor(theme.text)


def _chart_muted(theme: Theme) -> QColor:
    return QColor(theme.muted)


def _chart_grid(theme: Theme) -> QColor:
    if theme.dark:
        return QColor(203, 213, 225, 42)
    return QColor(71, 85, 105, 44)


def _chart_accent(theme: Theme) -> QColor:
    return QColor(getattr(theme, "primary_color", "#0A84FF"))


def _chart_secondary(theme: Theme) -> QColor:
    return QColor(getattr(theme, "secondary_color", "#30D158"))


def _chart_positive(theme: Theme) -> QColor:
    return QColor("#30D158" if theme.dark else "#16803A")


def _chart_negative(theme: Theme) -> QColor:
    return QColor("#FF6961" if theme.dark else "#D92D20")


def _chart_tooltip_bg(theme: Theme) -> QColor:
    if theme.dark:
        return QColor(17, 24, 39, 244)
    return QColor(255, 255, 255, 248)
