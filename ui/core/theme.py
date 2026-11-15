#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import sys
import textwrap
from typing import Optional

from utils.app_logging import log_exception
from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import QEvent, QObject, Qt
    from PySide6.QtGui import QBrush, QColor, QFont, QPalette, QTextCharFormat
    from PySide6.QtWidgets import (
        QApplication,
        QCalendarWidget,
        QComboBox,
        QHeaderView,
        QLabel,
        QMenu,
        QMessageBox,
        QTableView,
        QToolTip,
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

from utils.helpers import (
    ARROW_DARK,
    ARROW_DOWN_DARK,
    ARROW_DOWN_LIGHT,
    ARROW_LIGHT,
    ARROW_UP_DARK,
    ARROW_UP_LIGHT,
    CHECKMARK_LIGHT,
)


class Theme:
    def __init__(
        self,
        dark: bool = False,
        privacy: bool = False,
        primary_color_name: str = "Azul",
    ) -> None:
        self.dark = dark
        self.privacy = privacy

        # Primary colors mapping
        primary_map = {
            "azul": ("#0A84FF", "#30D158"),  # Blue / Green (default)
            "verde": ("#30D158", "#0A84FF"),  # Green / Blue
            "roxo": ("#BF5AF2", "#FF375F"),  # Purple / Pink
            "rosa": ("#FF375F", "#FF9F0A"),  # Pink / Orange
            "laranja": ("#FF9F0A", "#FFD60A"),  # Orange / Yellow
        }
        name_lower = primary_color_name.lower().strip()
        if name_lower not in primary_map:
            name_lower = "azul"

        self.primary_color, self.secondary_color = primary_map[name_lower]
        if dark:
            self.bg1 = QColor("#0B1020")
            self.bg2 = QColor("#111827")
            self.card = "rgba(32, 39, 56, 0.88)"
            self.card_strong = "rgba(45, 55, 76, 0.94)"
            self.text = "#F8FAFC"
            self.muted = "#A7B2C4"
            self.border = "rgba(255,255,255,0.13)"
            # Milky input surfaces: more readable than fully transparent glass.
            self.input = "rgba(255,255,255,0.66)"
            self.input_text = "#0F172A"
            self.input_popup = "#FFFFFF"
            self.input_border = "rgba(15,23,42,0.24)"
            self.arrow = ARROW_DARK
            self.arrow_up = ARROW_UP_DARK
            self.arrow_down = ARROW_DOWN_DARK
            self.page_surface = "rgba(11,16,32,0.01)"
            self.sidebar = "rgba(15,23,42,0.78)"
            self.nav_active = "rgba(10,132,255,0.18)"
            self.nav_active_border = "rgba(125,211,252,0.26)"
            self.header_bg = "rgba(15,23,42,0.82)"
            self.table_alt = "rgba(255,255,255,0.04)"
            self.table_item_selected = "rgba(10,132,255,0.28)"
            self.scroll_handle = "rgba(148,163,184,0.34)"
            self.calendar_bg = "rgba(15,23,42,0.88)"
            self.calendar_nav = "rgba(30,41,59,0.92)"
            self.calendar_grid = "rgba(148,163,184,0.22)"
            self.calendar_selection = "rgba(10,132,255,0.48)"
            self.combo_surface = "#172033"
            self.combo_text = "#F8FAFC"
            self.combo_popup = "#111827"
            self.combo_border = "rgba(148,163,184,0.42)"
            self.combo_arrow = ARROW_LIGHT
            self.topbar_surface = "#111827"
            self.topbar_text = "#F8FAFC"
            self.topbar_placeholder = "#A7B2C4"
            self.status_surface = "#111827"
            self.status_text = "#F8FAFC"
            self.check_surface = "#172033"
            self.check_border = "#94A3B8"
            self.checkmark = CHECKMARK_LIGHT
            self.message_surface = "#111827"
            self.message_button = "#172033"
            self.message_text = "#F8FAFC"
            self.message_muted = "#CBD5E1"
            self.message_border = "rgba(148,163,184,0.42)"
            self.message_hover = "rgba(10,132,255,0.28)"
        else:
            self.bg1 = QColor("#F4F8FF")
            self.bg2 = QColor("#EAFBF3")
            self.card = "rgba(255,255,255,204)"
            self.card_strong = "rgba(255,255,255,217)"
            self.text = "#142035"
            self.muted = "#64748B"
            self.border = "rgba(255,255,255,230)"
            # Milky input surfaces: consistent with the Liquid Glass look.
            self.input = "rgba(255,255,255,184)"
            self.input_text = "#142035"
            self.input_popup = "#FFFFFF"
            self.input_border = "rgba(20,32,53,0.22)"
            self.arrow = ARROW_LIGHT
            self.arrow_up = ARROW_UP_LIGHT
            self.arrow_down = ARROW_DOWN_LIGHT
            self.page_surface = "transparent"
            self.sidebar = "rgba(255,255,255,43)"
            self.nav_active = "rgba(255,255,255,217)"
            self.nav_active_border = "rgba(255,255,255,230)"
            self.header_bg = "rgba(255,255,255,51)"
            self.table_alt = "rgba(255,255,255,41)"
            self.table_item_selected = "rgba(10,132,255,0.18)"
            self.scroll_handle = "rgba(100,116,139,0.28)"
            # Light mode calendar must look like the other cards, not like a dark/native widget.
            self.calendar_bg = "rgba(255,255,255,217)"
            self.calendar_nav = "rgba(255,255,255,230)"
            self.calendar_grid = "rgba(100,116,139,0.18)"
            self.calendar_selection = "rgba(10,132,255,0.26)"
            self.combo_surface = "#FFFFFF"
            self.combo_text = "#142035"
            self.combo_popup = "#FFFFFF"
            self.combo_border = "rgba(20,32,53,0.24)"
            self.combo_arrow = ARROW_DARK
            self.topbar_surface = "#FFFFFF"
            self.topbar_text = "#142035"
            self.topbar_placeholder = "#64748B"
            self.status_surface = "#F8FAFC"
            self.status_text = "#142035"
            self.check_surface = "#FFFFFF"
            self.check_border = "#64748B"
            self.checkmark = CHECKMARK_LIGHT
            self.message_surface = "#F8FAFF"
            self.message_button = "#FFFFFF"
            self.message_text = "#142035"
            self.message_muted = "#52647D"
            self.message_border = "rgba(20,32,53,0.22)"
            self.message_hover = "rgba(10,132,255,0.14)"

    def stylesheet(self) -> str:
        from utils.config import resource_path

        with open(resource_path("assets/style.qss"), "r", encoding="utf-8") as f:
            css = f.read()
        return (
            css.replace("@text@", self.text)
            .replace("@page_surface@", self.page_surface)
            .replace("@muted@", self.muted)
            .replace("@card@", self.card)
            .replace("@border@", self.border)
            .replace("@card_strong@", self.card_strong)
            .replace("@sidebar@", self.sidebar)
            .replace("@input@", self.input)
            .replace("@input_popup@", self.input_popup)
            .replace("@input_text@", self.input_text)
            .replace("@input_border@", self.input_border)
            .replace("@combo_surface@", self.combo_surface)
            .replace("@combo_text@", self.combo_text)
            .replace("@combo_popup@", self.combo_popup)
            .replace("@combo_border@", self.combo_border)
            .replace("@combo_arrow@", self.combo_arrow)
            .replace("@topbar_surface@", self.topbar_surface)
            .replace("@topbar_text@", self.topbar_text)
            .replace("@topbar_placeholder@", self.topbar_placeholder)
            .replace("@status_surface@", self.status_surface)
            .replace("@status_text@", self.status_text)
            .replace("@check_surface@", self.check_surface)
            .replace("@check_border@", self.check_border)
            .replace("@checkmark@", self.checkmark)
            .replace("@message_surface@", self.message_surface)
            .replace("@message_button@", self.message_button)
            .replace("@message_text@", self.message_text)
            .replace("@message_muted@", self.message_muted)
            .replace("@message_border@", self.message_border)
            .replace("@message_hover@", self.message_hover)
            .replace("@nav_active_border@", self.nav_active_border)
            .replace("@hover@", self.nav_active)
            .replace("@scroll_thumb@", self.scroll_handle)
            .replace("@primary@", self.primary_color)
            .replace("@secondary@", self.secondary_color)
            .replace("@nav_active@", self.nav_active)
            .replace("@nav_active_border@", self.nav_active_border)
            .replace("@arrow@", self.arrow)
            .replace("@arrow_up@", self.arrow_up)
            .replace("@arrow_down@", self.arrow_down)
            .replace("@table_alt@", self.table_alt)
            .replace("@table_item_selected@", self.table_item_selected)
            .replace("@header_bg@", self.header_bg)
            .replace("@calendar_bg@", self.calendar_bg)
            .replace("@calendar_nav@", self.calendar_nav)
            .replace("@calendar_selection@", self.calendar_selection)
            .replace("@calendar_grid@", self.calendar_grid)
        )


def _popup_fallback_palette(theme: Theme, source: QPalette) -> QPalette:
    """Return a complete palette for dialogs that bypass Qt stylesheets."""
    palette = QPalette(source)
    surface = QColor(theme.message_surface)
    button = QColor(theme.message_button)
    text = QColor(theme.message_text)
    muted = QColor(theme.message_muted)
    accent = QColor(theme.primary_color)
    tooltip_surface = QColor("#FFFFFF")
    tooltip_text = QColor("#0F172A")

    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    ):
        for role in (
            QPalette.ColorRole.Window,
            QPalette.ColorRole.Base,
            QPalette.ColorRole.AlternateBase,
        ):
            palette.setColor(group, role, surface)
        palette.setColor(group, QPalette.ColorRole.Button, button)
        palette.setColor(group, QPalette.ColorRole.Highlight, accent)
        palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
        palette.setColor(group, QPalette.ColorRole.Link, accent)
        palette.setColor(group, QPalette.ColorRole.LinkVisited, accent)
        palette.setColor(group, QPalette.ColorRole.ToolTipBase, tooltip_surface)
        palette.setColor(group, QPalette.ColorRole.ToolTipText, tooltip_text)
        foreground = muted if group == QPalette.ColorGroup.Disabled else text
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
            QPalette.ColorRole.PlaceholderText,
        ):
            palette.setColor(group, role, foreground)
    return palette


def _message_box_stylesheet(theme: Theme) -> str:
    return f"""
        QMessageBox {{
            background-color: {theme.message_surface};
            color: {theme.message_text};
            border: 1px solid {theme.message_border};
        }}
        QMessageBox QWidget,
        QMessageBox QDialogButtonBox {{
            background-color: {theme.message_surface};
            color: {theme.message_text};
        }}
        QMessageBox QLabel {{
            background-color: transparent;
            color: {theme.message_text};
            font-size: 14px;
            font-weight: 600;
            padding: 4px 6px;
        }}
        QMessageBox QLabel#qt_msgbox_label {{
            color: {theme.message_text};
            min-width: 360px;
            max-width: 460px;
        }}
        QMessageBox QLabel#qt_msgbox_informativelabel {{
            color: {theme.message_muted};
            min-width: 360px;
            max-width: 460px;
            font-size: 13px;
            font-weight: 500;
        }}
        QMessageBox QPushButton {{
            min-width: 92px;
            min-height: 34px;
            padding: 8px 18px;
            border-radius: 17px;
            background-color: {theme.message_button};
            color: {theme.message_text};
            border: 1px solid {theme.message_border};
            font-weight: 750;
        }}
        QMessageBox QPushButton:hover {{
            background-color: {theme.message_hover};
            border-color: {theme.primary_color};
        }}
        QMessageBox QPushButton:default {{
            background-color: {theme.message_hover};
            border: 2px solid {theme.primary_color};
        }}
    """


def _wrap_plain_popup_text(value: str, *, width: int = 48) -> str:
    """Insert stable line breaks because QMessageBox can ignore QLabel wrapping."""
    text = str(value or "")
    if not text or ("<" in text and ">" in text):
        return text
    wrapped_lines: list[str] = []
    for source_line in text.splitlines():
        if not source_line.strip():
            wrapped_lines.append("")
            continue
        wrapped_lines.extend(
            textwrap.wrap(
                source_line,
                width=width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [""]
        )
    return "\n".join(wrapped_lines)


def apply_message_box_theme(
    message_box: QMessageBox,
    theme: Theme,
    *,
    forced: bool = False,
) -> None:
    """Force one real message box to use the selected app theme.

    A local stylesheet and palette are both intentional. On Windows, static
    QMessageBox helpers can keep a native/system palette even while the main
    window is using a light stylesheet.

    The function is idempotent by design: the event filter invokes it while
    Polish/Show events are being delivered, and every ``setStyleSheet`` or
    ``setPalette`` call re-queues another PolishRequest. Re-applying an
    already-correct state therefore livelocks the repolish cycle (LGBK3,
    full-suite timeout). A marker property records the stylesheet that was
    last applied so repeated deliveries short-circuit before touching the
    widget.
    """
    expected_stylesheet = _message_box_stylesheet(theme)
    if (
        not forced
        and message_box.styleSheet() == expected_stylesheet
        and message_box.property(_THEME_MARKER_PROPERTY) == expected_stylesheet
    ):
        return
    try:
        message_box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    except (AttributeError, RuntimeError) as exc:
        log_exception("Failed to disable native QMessageBox rendering", exc)
    message_box.setProperty(_THEME_MARKER_PROPERTY, expected_stylesheet)
    message_box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    message_box.setAutoFillBackground(True)
    palette = _popup_fallback_palette(theme, message_box.palette())
    message_box.setPalette(palette)
    message_box.setStyleSheet(expected_stylesheet)
    wrapped_text = _wrap_plain_popup_text(message_box.text())
    if wrapped_text != message_box.text():
        message_box.setText(wrapped_text)
    wrapped_detail = _wrap_plain_popup_text(
        message_box.informativeText(),
        width=48,
    )
    if wrapped_detail != message_box.informativeText():
        message_box.setInformativeText(wrapped_detail)
    for child in message_box.findChildren(QWidget):
        child.setPalette(palette)
    for label in message_box.findChildren(QLabel):
        if label.objectName() == "qt_msgboxex_icon_label":
            continue
        label.setWordWrap(True)
        label.setMaximumWidth(460)


_THEME_MARKER_PROPERTY = "_finance_theme_stylesheet"


class _MessageBoxThemeFilter(QObject):
    """Apply the active palette to static QMessageBox convenience dialogs."""

    _THEME_EVENTS = {
        QEvent.Type.Polish,
        QEvent.Type.PolishRequest,
        QEvent.Type.Show,
        QEvent.Type.ShowToParent,
    }

    def __init__(self, app: QApplication, theme: Theme) -> None:
        super().__init__(app)
        self.theme = theme
        self._applying = False

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, QMessageBox) and event.type() in self._THEME_EVENTS:
            # Re-entrancy guard: apply_message_box_theme() mutates stylesheet
            # and palette, which emits a nested PolishRequest. Without this
            # flag the filter recurses until the test/process hangs (LGBK3).
            # The marker check inside apply_message_box_theme() is what
            # actually breaks the asynchronous repolish cycle; this flag
            # only blocks synchronous nesting.
            if self._applying:
                return False
            self._applying = True
            try:
                apply_message_box_theme(watched, self.theme)
            except Exception as exc:
                log_exception("Failed to enforce QMessageBox theme", exc)
            finally:
                self._applying = False
        return False


def _install_message_box_theme_filter(app: QApplication, theme: Theme) -> None:
    manager = getattr(app, "_finance_message_box_theme_filter", None)
    if not isinstance(manager, _MessageBoxThemeFilter):
        manager = _MessageBoxThemeFilter(app, theme)
        app.installEventFilter(manager)
        app._finance_message_box_theme_filter = manager
    else:
        manager.set_theme(theme)
    for widget in app.topLevelWidgets():
        if isinstance(widget, QMessageBox):
            apply_message_box_theme(widget, theme)


def apply_tooltip_theme(theme: Theme) -> None:
    """Style Qt's top-level popup windows with guaranteed high contrast.

    QToolTip and QMessageBox may not remain children of the main window on
    Windows, so its window-level stylesheet does not reliably reach them.
    Apply global selectors for these popup types while keeping the rest of the
    application under the main-window stylesheet.
    """
    background = QColor("#FFFFFF")
    text = QColor("#0F172A")
    palette = QToolTip.palette()
    for group in (
        QPalette.Active,
        QPalette.Inactive,
        QPalette.Disabled,
    ):
        for role in (QPalette.ToolTipBase, QPalette.Window, QPalette.Base):
            palette.setColor(group, role, background)
        for role in (QPalette.ToolTipText, QPalette.WindowText, QPalette.Text):
            palette.setColor(group, role, text)
    QToolTip.setPalette(palette)
    font = QToolTip.font()
    font.setPointSize(max(11, font.pointSize()))
    font.setWeight(QFont.Weight.Medium)
    QToolTip.setFont(font)

    app = QApplication.instance()
    if app is not None:
        # The main window is painted by QSS, but native/fallback dialogs may
        # still use QApplication's OS palette. Keep that fallback coherent
        # with the selected app theme before any popup is constructed.
        app.setPalette(_popup_fallback_palette(theme, app.palette()))
        _install_message_box_theme_filter(app, theme)
        app.setStyleSheet(f"""
            QToolTip {{
                background-color: #FFFFFF;
                color: #0F172A;
                border: 2px solid {theme.primary_color};
                border-radius: 10px;
                padding: 12px 14px;
                font-size: 13px;
                font-weight: 520;
                opacity: 255;
            }}
            {_message_box_stylesheet(theme)}
        """)


def apply_menu_theme(menu: QMenu, theme: Theme) -> None:
    """Force context-menu colours per instance.

    Some Windows/Qt styles can keep native dark menus even when the rest of
    the application is in the light theme. Applying a palette and stylesheet
    directly to the QMenu prevents black unreadable context menus in tables.
    """
    menu.setObjectName("FinanceMenu")
    if theme.dark:
        bg = "#111827"
        text = "#F8FAFC"
        muted = "#94A3B8"
        border = "rgba(255,255,255,0.16)"
        hover = "rgba(10,132,255,0.28)"
    else:
        bg = "#FFFFFF"
        text = "#142035"
        muted = "#64748B"
        border = "rgba(20,32,53,0.18)"
        hover = "rgba(10,132,255,0.18)"
    palette = menu.palette()
    palette.setColor(QPalette.Window, QColor(bg))
    palette.setColor(QPalette.Base, QColor(bg))
    palette.setColor(QPalette.Text, QColor(text))
    palette.setColor(QPalette.WindowText, QColor(text))
    palette.setColor(QPalette.ButtonText, QColor(text))
    palette.setColor(
        QPalette.Highlight, QColor(10, 132, 255, 46 if not theme.dark else 82)
    )
    palette.setColor(QPalette.HighlightedText, QColor(text))
    menu.setPalette(palette)
    menu.setAutoFillBackground(True)
    menu.setStyleSheet(f"""
        QMenu {{
            background-color: {bg};
            color: {text};
            border: 1.2px solid {border};
            border-radius: 14px;
            padding: 6px;
        }}
        QMenu::item {{
            background: transparent;
            color: {text};
            padding: 9px 18px;
            min-height: 28px;
            border-radius: 9px;
        }}
        QMenu::item:selected {{
            background: {hover};
            color: {text};
        }}
        QMenu::item:disabled {{
            color: {muted};
        }}
        QMenu::separator {{
            height: 1px;
            background: {border};
            margin: 6px 8px;
        }}
    """)


def apply_combo_theme(combo: QComboBox, theme: Theme) -> None:
    """Force a solid readable palette on the control and its popup view."""

    surface = QColor(theme.combo_surface)
    popup = QColor(theme.combo_popup)
    text = QColor(theme.combo_text)
    highlight = QColor(theme.primary_color)
    palette = combo.palette()
    for role in (QPalette.Base, QPalette.Button, QPalette.Window):
        palette.setColor(role, surface)
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        palette.setColor(role, text)
    palette.setColor(QPalette.Highlight, highlight)
    palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    combo.setPalette(palette)
    try:
        view = combo.view()
        view_palette = view.palette()
        for role in (QPalette.Base, QPalette.Button, QPalette.Window):
            view_palette.setColor(role, popup)
        for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
            view_palette.setColor(role, text)
        view_palette.setColor(QPalette.Highlight, highlight)
        view_palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        view.setPalette(view_palette)
        view.setAutoFillBackground(True)
    except RuntimeError as exc:
        log_exception("Failed to apply combo popup palette", exc)


def apply_calendar_theme(calendar: QCalendarWidget, theme: Theme) -> None:
    """Force QCalendarWidget colours per instance.

    On Windows, QCalendarWidget can keep native dark palette colours even when the
    rest of the PySide6 app is in the light theme. A normal global stylesheet is not
    always enough because the internal calendar view/navigation widgets are created
    by Qt and may inherit the operating-system palette. This function applies both
    an opaque palette and a per-widget stylesheet to the calendar and its internal
    children so the Centro Financeiro calendar stays coherent with the other cards.
    """
    if theme.dark:
        bg = QColor(15, 23, 42, 235)
        nav = QColor(30, 41, 59, 242)
        text = QColor(248, 250, 252)
        selected = QColor(10, 132, 255, 120)
        border = "rgba(255,255,255,0.13)"
        bg_css = "#0F172A"
        nav_css = "#1E293B"
        text_css = "#F8FAFC"
        muted_css = "#A7B2C4"
        selected_css = "rgba(10,132,255,0.46)"
    else:
        # Use solid/opaque colours here, not rgba, to avoid the black native calendar
        # background that some Windows/Qt builds show with transparent calendar views.
        bg = QColor(248, 251, 255, 255)
        nav = QColor(255, 255, 255, 255)
        text = QColor(20, 32, 53)
        selected = QColor(10, 132, 255, 58)
        border = "rgba(20,32,53,0.16)"
        bg_css = "#F8FBFF"
        nav_css = "#FFFFFF"
        text_css = "#142035"
        muted_css = "#64748B"
        selected_css = "rgba(10,132,255,0.22)"

    pal = calendar.palette()
    for role in (
        QPalette.Window,
        QPalette.Base,
        QPalette.AlternateBase,
        QPalette.Button,
    ):
        pal.setColor(role, bg if role != QPalette.Button else nav)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(role, text)
    pal.setColor(QPalette.ToolTipBase, nav)
    pal.setColor(QPalette.ToolTipText, text)
    pal.setColor(QPalette.Highlight, selected)
    pal.setColor(QPalette.HighlightedText, text)
    calendar.setPalette(pal)
    calendar.setAutoFillBackground(True)
    calendar.setAttribute(Qt.WA_StyledBackground, True)
    calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)

    fmt = QTextCharFormat()
    fmt.setForeground(QBrush(text))
    fmt.setBackground(QBrush(bg))
    for weekday in (
        Qt.Monday,
        Qt.Tuesday,
        Qt.Wednesday,
        Qt.Thursday,
        Qt.Friday,
        Qt.Saturday,
        Qt.Sunday,
    ):
        try:
            calendar.setWeekdayTextFormat(weekday, fmt)
        except Exception as exc:
            log_exception("Failed to apply weekday calendar text format", exc)

    from utils.config import resource_path

    with open(resource_path("assets/calendar.qss"), "r", encoding="utf-8") as f:
        cal_css = f.read()

    calendar.setStyleSheet(
        cal_css.replace("@bg_css@", bg_css)
        .replace("@text_css@", text_css)
        .replace("@border@", border)
        .replace("@nav_css@", nav_css)
        .replace("@selected_css@", selected_css)
        .replace("@muted_css@", muted_css)
    )

    # We apply the palette to children, but DO NOT force auto-fill on viewports,
    # as that breaks the internal grid rendering of the calendar days!
    for child in calendar.findChildren(QWidget):
        try:
            child.setPalette(pal)
            name = child.objectName()
            if name == "qt_calendar_navigationbar":
                child.setAutoFillBackground(True)
        except Exception as exc:
            log_exception("Failed to force calendar child theme", exc)

    # QCalendarWidget uses a private QTableView for its seven day columns.
    # Keep those sections stretched when the card/window changes width; treating
    # them like normal resizable app columns makes Qt elide day numbers to "...".
    calendar_view = calendar.findChild(QTableView, "qt_calendar_calendarview")
    if calendar_view is not None:
        calendar_view.setProperty("disableColumnPersistence", True)
        calendar_view.setTextElideMode(Qt.TextElideMode.ElideNone)
        calendar_view.setWordWrap(False)
        calendar_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        calendar_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = calendar_view.horizontalHeader()
        header.setMinimumSectionSize(24)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)


def theme_for_widget(widget: Optional[QWidget]) -> Theme:
    """Find the current app theme from a widget/page/dialog."""
    current = widget
    while current is not None:
        app_obj = getattr(current, "app", None)
        if app_obj is not None and hasattr(app_obj, "theme"):
            try:
                return app_obj.theme
            except Exception as exc:
                log_exception("Failed to read theme from widget app", exc)
        theme_obj = getattr(current, "theme", None)
        if isinstance(theme_obj, Theme):
            return theme_obj
        current = current.parentWidget()
    inst = QApplication.instance()
    if inst is not None and inst.activeWindow() is not None:
        active = inst.activeWindow()
        app_obj = getattr(active, "app", None)
        if app_obj is not None and hasattr(app_obj, "theme"):
            return app_obj.theme
        theme_obj = getattr(active, "theme", None)
        if isinstance(theme_obj, Theme):
            return theme_obj
    return Theme(dark=False)
