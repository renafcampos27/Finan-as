#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import (
    QFont,
    QFontMetrics,
    QHelpEvent,
    QMouseEvent,
    QPalette,
    QWheelEvent,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCalendarWidget,
    QHeaderView,
    QPushButton,
    QSizePolicy,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QWidget,
)

from ui.components.animations import ToastNotification
from ui.components.cards import GlassCard, MetricCard
from ui.components.expense_work_time import ExpenseWorkTimeCard
from ui.components.charts import (
    BalanceChart,
    BudgetForecastChart,
    CashFlowWaterfallChart,
    CashflowTimelineChart,
    DonutChart,
    HeatmapChart,
    HabitImpactChart,
    HabitPlanTrackingChart,
    InvestmentEvolutionChart,
    InvestmentProjectionChart,
    MonteCarloChart,
    NetWorthChart,
    PeriodComparisonChart,
    RadarChart,
    ResilienceScenarioChart,
    SankeyChart,
    ScoreBreakdownChart,
    ScoreTrendChart,
    TreemapChart,
)
from ui.components.charts.utils import (
    _chart_bg,
    _chart_text,
    category_color,
    compact_value_items,
)
from ui.components.charts.bar import StackedExpenseChart
from ui.components.habit_audit import HabitAuditDialog
from ui.core.theme import Theme, apply_calendar_theme, apply_tooltip_theme
from ui.core.tooltips import ReadableToolTipManager
from ui.models import MovementsTableModel
from ui.pages.movements import MovementHoverCard, MovementsTableView
from services.investment_projection import build_investment_projection
from services.financial_intelligence import monte_carlo_projection
from services.habit_simulations import (
    HABIT_PRESETS_BY_KEY,
    HabitSimulationInputs,
    simulate_habit,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _balance_points(count: int = 45) -> list[dict]:
    start = date.today() - timedelta(days=count - 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "balance": 1000 + index * 8 - (index % 7) * 12,
            "income": 50 if index % 15 == 0 else 0,
            "expense": 20 if index % 3 == 0 else 0,
            "delta": 0,
            "is_forecast": index >= count - 7,
        }
        for index in range(count)
    ]


def test_cards_show_a_reusable_compact_context_badge(qapp):
    card = GlassCard("Gráfico financeiro", strong=True)
    card.set_context_badge("Filtro atual", "warning")
    metric = MetricCard(
        "Receitas",
        "2 500,00 €",
        "último mês",
        "#0A84FF",
        "📈",
    )
    metric.set_context_badge("Mês atual", "positive")
    card.show()
    metric.show()
    qapp.processEvents()

    assert card.context_badge is not None
    assert card.context_badge.text() == "Filtro atual"
    assert card.context_badge.property("tone") == "warning"
    assert card.title_label.parentWidget().objectName() == "CardContextHeader"
    assert card.context_badge.isVisibleTo(card)
    assert metric.context_badge is not None
    assert metric.context_badge.text() == "Mês atual"
    assert metric.context_badge.property("tone") == "positive"
    assert metric.v.indexOf(metric.context_badge) == 1
    assert not card.grab().isNull()
    assert not metric.grab().isNull()
    card.close()
    metric.close()


def test_category_comparison_and_focus_render_across_chart_types(qapp):
    theme = Theme(dark=False)
    current = [
        ("Casa", 1_200, "#0A84FF"),
        ("Alimentação", 600, "#FF9F0A"),
        ("Transportes", 300, "#BF5AF2"),
    ]
    previous = [
        ("Casa", 1_000, "#0A84FF"),
        ("Alimentação", 700, "#FF9F0A"),
        ("Transportes", 250, "#BF5AF2"),
    ]
    donut = DonutChart(lambda: theme)
    radar = RadarChart(lambda: theme)
    waterfall = CashFlowWaterfallChart(lambda: theme)
    donut.resize(760, 420)
    radar.resize(760, 520)
    waterfall.resize(980, 390)
    donut.set_data(current)
    donut.set_comparison_data(previous, "período anterior")
    radar.set_data(current)
    radar.set_comparison_data(previous, "período anterior")
    waterfall.set_data(
        4_000,
        2_100,
        current,
        "mês atual",
        previous_income=3_800,
        previous_expenses=1_950,
        previous_categories=previous,
        comparison_label="período anterior",
    )

    for chart in (donut, radar, waterfall):
        chart.set_highlighted_category("Casa")
        chart.show()
    qapp.processEvents()

    assert donut.highlighted_category == "Casa"
    assert donut._comparison_value("Casa") == pytest.approx(1_000)
    assert radar.highlighted_category == "Casa"
    assert radar.comparison_data["Casa"] == pytest.approx(1_000)
    assert waterfall.highlighted_category == "Casa"
    assert waterfall._comparison_amount(waterfall.items[1]) == pytest.approx(1_000)
    assert all(not chart.grab().isNull() for chart in (donut, radar, waterfall))
    assert all(chart._paint_error == "" for chart in (donut, radar, waterfall))
    for chart in (donut, radar, waterfall):
        chart.close()


@pytest.mark.parametrize("dark", [False, True])
def test_movements_tooltip_is_structured_wide_and_high_contrast(dark):
    theme = Theme(dark=dark)
    model = MovementsTableModel(theme)
    model.set_rows(
        [
            {
                "id": 1,
                "date": "2026-07-13",
                "payment_date": "2026-07-14",
                "purchase_date": "2026-07-12",
                "description": "Supermercado <mensal>",
                "amount": -84.25,
                "category": "Supermercado",
                "subcategory": "Alimentação",
                "account": "Conta principal",
                "payment_method": "Cartão",
                "merchant": "Loja Exemplo",
                "tags": "casa",
                "notes": "Compra semanal",
            }
        ]
    )

    tooltip = model.data(model.index(0, 3), Qt.ToolTipRole)
    stylesheet = theme.stylesheet()

    assert "<table width='410'" in tooltip
    assert "<b>Datas</b>" in tooltip
    assert "<b>Categoria</b>" in tooltip
    assert "Supermercado &lt;mensal&gt;" in tooltip
    assert "QToolTip" in stylesheet
    assert f"background-color: {theme.input_popup}" in stylesheet
    assert f"color: {theme.input_text}" in stylesheet


@pytest.mark.parametrize("dark", [False, True])
def test_top_level_qtooltip_gets_opaque_readable_palette(qapp, dark):
    theme = Theme(dark=dark)

    apply_tooltip_theme(theme)

    palette = QToolTip.palette()
    background = palette.color(QPalette.Active, QPalette.ToolTipBase)
    text = palette.color(QPalette.Active, QPalette.ToolTipText)
    assert background.name().upper() == "#FFFFFF"
    assert text.name().upper() == "#0F172A"
    assert "QToolTip" in qapp.styleSheet()
    assert "background-color: #FFFFFF" in qapp.styleSheet()
    assert "color: #0F172A" in qapp.styleSheet()


def test_readable_in_app_tooltip_replaces_native_black_popup(qapp):
    host = QWidget()
    host.resize(680, 260)
    button = QPushButton("🩺 Score Financeiro 2.0", host)
    button.setGeometry(110, 70, 250, 42)
    button.setToolTip("Abrir 🩺 Score Financeiro 2.0")
    manager = ReadableToolTipManager(host)
    qapp.installEventFilter(manager)
    host.show()
    qapp.processEvents()

    try:
        local_position = QPoint(120, 21)
        help_event = QHelpEvent(
            QEvent.Type.ToolTip,
            local_position,
            button.mapToGlobal(local_position),
        )
        QApplication.sendEvent(button, help_event)
        qapp.processEvents()

        assert manager.popup.isVisible()
        assert not manager.popup.isWindow()
        assert manager.popup.text_label.text() == "Abrir 🩺 Score Financeiro 2.0"
        tooltip_image = manager.popup.grab().toImage()
        background = tooltip_image.pixelColor(
            manager.popup.width() - 16,
            manager.popup.height() - 16,
        )
        assert background.lightness() > 235

        leave_event = QEvent(QEvent.Type.Leave)
        QApplication.sendEvent(button, leave_event)
        assert not manager.popup.isVisible()
    finally:
        qapp.removeEventFilter(manager)
        host.close()


def test_item_tooltip_role_uses_the_same_readable_light_popup(qapp):
    host = QWidget()
    host.resize(720, 320)
    table = QTableWidget(1, 1, host)
    table.setGeometry(40, 40, 520, 180)
    item = QTableWidgetItem("Movimento")
    item.setToolTip("Detalhe legível do movimento")
    table.setItem(0, 0, item)
    manager = ReadableToolTipManager(host)
    qapp.installEventFilter(manager)
    host.show()
    qapp.processEvents()

    try:
        position = table.visualItemRect(item).center()
        help_event = QHelpEvent(
            QEvent.Type.ToolTip,
            position,
            table.viewport().mapToGlobal(position),
        )
        QApplication.sendEvent(table.viewport(), help_event)
        qapp.processEvents()

        assert manager.popup.isVisible()
        assert manager.popup.text_label.text() == "Detalhe legível do movimento"
        image = manager.popup.grab().toImage()
        assert (
            image.pixelColor(
                manager.popup.width() - 16, manager.popup.height() - 16
            ).lightness()
            > 235
        )
    finally:
        qapp.removeEventFilter(manager)
        host.close()


def test_category_colours_limits_and_keyboard_are_consistent(qapp):
    theme = Theme()
    rows = [(f"Categoria {index}", 100.0 - index, "#10B981") for index in range(12)]
    rows[0] = ("Supermercado", 150.0, "#10B981")
    donut = DonutChart(lambda: theme)
    treemap = TreemapChart(lambda: theme)
    sankey = SankeyChart(lambda: theme)
    for chart in (donut, treemap, sankey):
        chart.resize(760, 700)
        chart.show()
    donut.set_data(rows)
    treemap.set_data(rows)
    sankey.set_data(rows)
    qapp.processEvents()

    assert len(donut.data) == 9
    assert len(sankey.items) == 9
    assert donut.data[0][2] == category_color("Supermercado")
    # Treemap preserves explicit category colours supplied by its caller.
    assert treemap.category_colors["Supermercado"] == "#10B981"
    donut.set_category_limit(None)
    sankey.set_category_limit(None)
    assert len(donut.data) == 12
    assert len(sankey.items) == 12
    donut.set_category_limit(8)
    sankey.set_category_limit(8)
    assert len(donut.data) == 9
    assert donut.data[-1][0].startswith("Outros")
    assert len(sankey.items) == 9

    donut.setFocus()
    QTest.keyClick(donut, Qt.Key_Right)
    assert donut.hover_index == 0
    assert donut._chart_action_button.isVisible()

    for chart in (donut, treemap, sankey):
        chart.close()


def test_stacked_chart_dark_tooltip_has_dark_surface_and_light_text(qapp):
    theme = Theme(dark=True)
    chart = StackedExpenseChart(lambda: theme)
    chart.resize(760, 300)
    chart.set_data(
        [{"label": "Jul", "Casa": 620.0, "Lazer": 180.0}],
        [("Casa", "#0A84FF"), ("Lazer", "#BF5AF2")],
    )
    chart.hover_index = 0
    chart.show()
    qapp.processEvents()

    image = chart.grab().toImage()
    left, top, right, bottom = chart.plot_rect()
    gap = 8
    bar_width = max(10, min(42, (right - left - gap * 0) / 1))
    centre_x = left + bar_width / 2
    tooltip_width = 285
    tooltip_height = 28 + 2 * 19
    tooltip_x = min(max(centre_x + 14, left), right - tooltip_width)
    tooltip_y = max(top + 4, top + 20 - tooltip_height - 12)
    background = image.pixelColor(
        int(tooltip_x + tooltip_width - 18),
        int(tooltip_y + tooltip_height - 12),
    )
    brightest = max(
        image.pixelColor(x, y).lightness()
        for x in range(int(tooltip_x + 8), int(tooltip_x + tooltip_width - 8))
        for y in range(int(tooltip_y + 8), int(tooltip_y + tooltip_height - 8))
    )

    assert background.lightness() < 90
    assert background.alpha() == 255
    assert brightest > 200
    assert chart._paint_error == ""
    chart.close()


@pytest.mark.parametrize("dark", [False, True])
def test_finance_calendar_keeps_all_weekday_and_day_columns_readable(qapp, dark):
    calendar = QCalendarWidget()
    calendar.setObjectName("FinanceCalendar")
    calendar.setHorizontalHeaderFormat(QCalendarWidget.ShortDayNames)
    calendar.setSelectedDate(QDate(2026, 5, 1))
    calendar.setFixedHeight(320)
    apply_calendar_theme(calendar, Theme(dark=dark))
    calendar.resize(520, 320)
    calendar.show()
    qapp.processEvents()

    calendar_view = calendar.findChild(QTableView, "qt_calendar_calendarview")
    assert calendar_view is not None
    widths = [
        calendar_view.columnWidth(column)
        for column in range(calendar_view.model().columnCount())
    ]
    weekday_labels = [
        str(
            calendar_view.model().data(
                calendar_view.model().index(0, column), Qt.DisplayRole
            )
            or ""
        )
        for column in range(calendar_view.model().columnCount())
    ]

    assert len(widths) == 7
    assert min(widths) >= 60
    assert sum(widths) >= calendar_view.viewport().width() - 7
    assert all(label and label != "..." for label in weekday_labels)
    assert calendar_view.textElideMode() == Qt.TextElideMode.ElideNone
    assert all(
        calendar_view.horizontalHeader().sectionResizeMode(column)
        == QHeaderView.ResizeMode.Stretch
        for column in range(7)
    )
    assert not calendar.grab().isNull()
    calendar.close()


def test_custom_movement_hover_card_is_an_in_app_opaque_light_surface(qapp):
    parent = QWidget()
    parent.resize(720, 420)
    parent.show()
    card = MovementHoverCard(parent)
    card.set_movement(
        {
            "id": 8,
            "date": "2026-07-13",
            "description": "Streaming TV (Subscrição)",
            "amount": -12.99,
            "category": "Subscrições",
            "account": "Conta principal",
        }
    )
    card.resize(500, 180)
    card.show()
    qapp.processEvents()

    image = card.grab().toImage()
    background = image.pixelColor(card.width() - 18, card.height() - 18)

    assert not card.isWindow()
    assert background.lightness() > 235
    assert "color:#0F172A" in card.title.styleSheet()
    assert "color:#0F172A" in card.details.styleSheet()
    assert "Streaming TV" in card.title.text()
    assert not image.isNull()


def test_movements_table_emits_hover_after_native_event_processing(qapp):
    model = MovementsTableModel(Theme())
    model.set_rows(
        [
            {
                "id": 1,
                "date": "2026-07-13",
                "description": "Teste",
                "amount": -10,
                "category": "Diversos",
            }
        ]
    )
    table = MovementsTableView()
    table.setModel(model)
    table.setMouseTracking(True)
    table.viewport().setMouseTracking(True)
    table.resize(640, 240)
    table.show()
    qapp.processEvents()
    hovered = []
    table.movement_hovered.connect(
        lambda index, position: hovered.append((index.row(), position))
    )
    position = table.visualRect(model.index(0, 3)).center()
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(position),
        QPointF(table.viewport().mapToGlobal(position)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )

    QApplication.sendEvent(table.viewport(), event)

    assert hovered and hovered[-1][0] == 0


def test_inline_movement_edit_replaces_immutable_sqlite_row(qapp):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute("""
        SELECT 7 AS id, '2026-07-16' AS date, 'Original' AS description,
               -10.0 AS amount, 'Diversos' AS category, 'Conta principal' AS account
        """).fetchone()
    connection.close()

    class RepoStub:
        def __init__(self):
            self.calls = []

        def update_transaction(self, tx_id, **updates):
            self.calls.append((tx_id, updates))

    class PageStub:
        def __init__(self):
            self.repo = RepoStub()
            self.refreshed = []

        def apply_inline_edit(self, tx_id, updates):
            self.repo.calls.append((tx_id, updates))
            return dict(updates)

        def force_refresh_after_save(self, tx_id, *, notify_views=True):
            self.refreshed.append(tx_id)

    page = PageStub()
    model = MovementsTableModel(Theme())
    model.page = page
    model.set_rows([row])

    assert not model.setData(model.index(0, 8), "-25.50 €")
    assert page.repo.calls == []
    assert model.setData(model.index(0, 3), "Descrição atualizada")
    assert model.setData(model.index(0, 8), "-25,50 €")
    assert page.repo.calls == [
        (7, {"description": "Descrição atualizada"}),
        (7, {"amount": -25.5}),
    ]
    assert isinstance(model.row_object(0), dict)
    assert model.row_object(0)["description"] == "Descrição atualizada"
    assert model.row_object(0)["amount"] == -25.5


def test_metric_card_keeps_old_api_and_adds_semantic_status(qapp):
    legacy = MetricCard("Saldo", "€ 1.000,00", "Disponível", "#30D158", "💰")
    assert legacy.value_label.fullText() == "€ 1.000,00"

    card = MetricCard(
        "Poupança",
        "€ 300,00",
        "Taxa 30%",
        "#30D158",
        "🏆",
        status="Bom ritmo",
        tone="positive",
        progress=0.75,
    )
    assert card.status_label.text() == "Bom ritmo"
    assert card.status_label.property("tone") == "positive"
    assert card.status_label.wordWrap()
    assert (
        card.status_label.sizePolicy().horizontalPolicy()
        == QSizePolicy.Policy.Expanding
    )
    card.set_data(status="Reserva atingida · faltam 118 000 € para o objetivo")
    assert card.status_label.toolTip() == card.status_label.text()
    assert card.progress_bar.value() == 75
    assert "Poupança" in card.accessibleName()


def test_metric_card_adds_sparkline_and_keyboard_mouse_action(qapp):
    card = MetricCard(
        "Receitas",
        "2 400,00 €",
        "Últimos 3 meses · ↑ 12%",
        "#0A84FF",
        "📈",
        status="Acima do período anterior",
        tone="positive",
        trend=[1200, 1500, 1420, 1900, 2400],
        trend_label="Tendência dos últimos 5 meses",
        clickable=True,
        action_hint="Abrir receitas filtradas",
    )
    card.resize(320, 210)
    card.show()
    qapp.processEvents()

    activations = []
    card.clicked.connect(lambda: activations.append(True))
    QTest.mouseClick(card, Qt.LeftButton, pos=card.rect().center())

    assert card.sparkline.values[-1] == 2400
    assert card.sparkline.isVisible()
    assert "últimos 5 meses" in card.accessibleDescription()
    assert activations == [True]
    assert not card.grab().isNull()


def test_chart_palette_respects_light_and_dark_theme():
    light = Theme(dark=False)
    dark = Theme(dark=True)
    assert _chart_bg(light) != _chart_bg(dark)
    assert _chart_text(light) == _chart_text(Theme(dark=False))
    assert _chart_text(dark) == _chart_text(Theme(dark=True))


def test_balance_chart_scroll_propagation_and_real_zoom(qapp):
    theme = Theme(dark=False)
    chart = BalanceChart(lambda: theme)
    chart.resize(680, 320)
    chart.set_points(_balance_points())

    normal_wheel = QWheelEvent(
        QPointF(240, 120),
        QPointF(240, 120),
        QPoint(),
        QPoint(0, 120),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )
    chart.wheelEvent(normal_wheel)
    assert not normal_wheel.isAccepted()

    zoom_wheel = QWheelEvent(
        QPointF(240, 120),
        QPointF(240, 120),
        QPoint(),
        QPoint(0, 120),
        Qt.NoButton,
        Qt.ControlModifier,
        Qt.ScrollUpdate,
        False,
    )
    chart.wheelEvent(zoom_wheel)
    assert zoom_wheel.isAccepted()
    assert chart.zoom_factor > 1.0
    assert chart._view_state()["end_position"] < len(chart.points) - 1


def test_donut_is_responsive_compact_and_slice_clickable(qapp):
    theme = Theme(dark=True, privacy=True)
    chart = DonutChart(lambda: theme)
    chart.resize(220, 220)
    chart.set_category_limit(8)
    chart.set_data(
        [(f"Categoria {index}", (12 - index) * 10, "#0A84FF") for index in range(12)]
    )
    assert len(chart.data) == 9
    assert chart.data[-1][0] == "Outros (4)"
    assert len({color for _label, _value, color in chart.data}) == len(chart.data)

    arc, _legend, _stroke, inner, outer = chart._geometry()
    hit = chart._hit_test(
        QPointF(arc.center().x(), arc.center().y() - (inner + outer) / 2)
    )
    assert hit == 0

    chart.show()
    qapp.processEvents()
    assert not chart.grab().isNull()
    assert chart._paint_error == ""
    assert chart._center_label_rect.bottom() + 3 <= chart._center_amount_rect.top()


def test_donut_category_colours_are_distinct_and_stable():
    chart = DonutChart(lambda: Theme(dark=False))
    chart.set_data(
        [
            ("Casa", 800, "#22C55E"),
            ("Supermercado", 500, "#20C65B"),
            ("Transportes", 200, "#24C45F"),
            ("Lazer", 100, "#21C55D"),
        ]
    )
    first_mapping = {label: color for label, _value, color in chart.data}
    assert len(set(first_mapping.values())) == len(first_mapping)

    chart.set_data(
        [
            ("Lazer", 900, "#22C55E"),
            ("Transportes", 600, "#20C65B"),
            ("Supermercado", 300, "#24C45F"),
            ("Casa", 50, "#21C55D"),
        ]
    )
    second_mapping = {label: color for label, _value, color in chart.data}
    assert second_mapping == first_mapping


def test_donut_compact_wide_legend_preserves_full_category_names(qapp):
    chart = DonutChart(lambda: Theme(dark=False))
    chart.resize(546, 398)
    chart.set_data(
        [
            ("Casa e contas", 2_220, "#0A84FF"),
            ("Supermercado", 1_021.78, "#FF9F0A"),
            ("Transportes", 190, "#BF5AF2"),
            ("Diversos", 156.76, "#30D158"),
            ("Restaurantes", 84, "#FF375F"),
            ("Saúde", 56, "#64D2FF"),
            ("Subscrições", 45.99, "#FFD60A"),
            ("Lazer", 38, "#5E5CE6"),
        ]
    )
    chart.show()
    qapp.processEvents()
    assert not chart.grab().isNull()

    legend = chart._geometry()[1]
    assert chart._legend_uses_two_lines(legend)
    assert len(chart._legend_label_rects) == 8
    metrics = QFontMetrics(QFont("Segoe UI", 9))
    assert all(
        metrics.horizontalAdvance(label) <= rect.width()
        for label, rect in chart._legend_label_rects
    )
    assert all(rect.height() >= 17 for _label, rect in chart._legend_label_rects)
    chart.close()


def test_donut_exposes_permanent_amount_and_percentage_in_wide_legend(qapp):
    theme = Theme(dark=False)
    chart = DonutChart(lambda: theme)
    chart.resize(410, 280)
    chart.set_data(
        [
            ("Habitação", 1_200, "#0A84FF"),
            ("Alimentação", 300, "#FF9F0A"),
        ]
    )
    chart.set_comparison_data(
        [
            ("Habitação", 1_000, "#0A84FF"),
            ("Alimentação", 400, "#FF9F0A"),
        ],
        "período anterior",
    )
    chart.show()
    qapp.processEvents()

    assert "Habitação: 1 200 €, 80,0%" in chart.accessibleDescription()
    assert chart._comparison_value("Habitação") == pytest.approx(1_000)
    assert chart._geometry()[1] is not None
    assert not chart.grab().isNull()
    assert chart._paint_error == ""
    chart.close()


def test_donut_numeric_columns_remain_aligned_when_chart_grows(qapp):
    chart = DonutChart(lambda: Theme(dark=False))
    chart.set_data(
        [
            ("Casa e contas", 2_220, "#0A84FF"),
            ("Supermercado", 1_021.78, "#FF9F0A"),
            ("Transportes", 190, "#BF5AF2"),
            ("Diversos", 56.76, "#30D158"),
        ]
    )
    chart.show()

    for width in (546, 800):
        chart.resize(width, 398)
        qapp.processEvents()
        assert not chart.grab().isNull()
        assert len(chart._legend_amount_rects) == len(chart.data)
        assert len(chart._legend_percent_rects) == len(chart.data)

        amount_lefts = {round(rect.left(), 2) for rect in chart._legend_amount_rects}
        amount_rights = {round(rect.right(), 2) for rect in chart._legend_amount_rects}
        percent_lefts = {round(rect.left(), 2) for rect in chart._legend_percent_rects}
        percent_rights = {
            round(rect.right(), 2) for rect in chart._legend_percent_rects
        }
        assert len(amount_lefts) == len(amount_rights) == 1
        assert len(percent_lefts) == len(percent_rights) == 1
        assert all(
            amount_rect.right() + 8.0 == pytest.approx(percent_rect.left())
            for amount_rect, percent_rect in zip(
                chart._legend_amount_rects,
                chart._legend_percent_rects,
            )
        )

        legend = chart._geometry()[1]
        if chart._legend_uses_two_lines(legend):
            assert all(
                label_rect.left() == pytest.approx(amount_rect.left())
                for (_label, label_rect), amount_rect in zip(
                    chart._legend_label_rects,
                    chart._legend_amount_rects,
                )
            )
        else:
            assert all(
                label_rect.center().y() == pytest.approx(amount_rect.center().y())
                for (_label, label_rect), amount_rect in zip(
                    chart._legend_label_rects,
                    chart._legend_amount_rects,
                )
            )

    assert chart._paint_error == ""
    chart.close()


@pytest.mark.parametrize("privacy", [False, True])
def test_expense_work_time_card_shows_every_category_without_inner_scroll(
    qapp,
    privacy,
):
    theme = Theme(dark=False, privacy=privacy)
    card = ExpenseWorkTimeCard(lambda: theme)
    card.resize(960, 500)
    rows = [
        {
            "category": f"Categoria {index}",
            "amount": 100 + index,
            "hours": 10 + index,
            "days": (10 + index) / 8,
            "colour": "#0A84FF",
        }
        for index in range(11)
    ]
    card.set_data(
        {
            "available": True,
            "hourly_value": 10,
            "daily_hours": 8,
            "rows": rows,
            "total_amount": sum(row["amount"] for row in rows),
            "total_hours": sum(row["hours"] for row in rows),
            "total_days": sum(row["days"] for row in rows),
        },
        period_label="últimos 3 meses",
        income_reference="salário",
        context_months=3,
        weekly_hours=40,
    )
    card.show()
    qapp.processEvents()

    assert card.table.rowCount() == len(rows)
    assert card.table.verticalScrollBar().maximum() == 0
    assert card.table.minimumHeight() == 48 + len(rows) * 40 + 4
    expected_amount = "••••• €" if privacy else "100 €"
    assert card.table.item(0, 1).text() == expected_amount
    card.set_highlighted_category("Categoria 3")
    assert card.table.currentRow() == 3
    card.set_highlighted_category("")
    assert not card.table.selectedItems()
    assert not card.grab().isNull()
    card.close()


@pytest.mark.parametrize("dark", [False, True])
def test_sankey_grows_to_keep_dense_month_categories_inside_card(qapp, dark):
    chart = SankeyChart(lambda: Theme(dark=dark))
    chart.set_category_limit(None)
    # Reproduces the old Panorama constraint that caused the bottom rows to clip.
    chart.setMinimumHeight(350)
    chart.set_data(
        [("Habitação", 1_000.0)]
        + [(f"Categoria {index}", 10.0) for index in range(1, 12)]
    )

    assert len(chart.items) == 12
    assert all(not label.startswith("Outros") for label, _value in chart.items)
    assert chart.minimumHeight() >= 580
    assert chart.minimumHeight() == chart.required_height_for_items(chart.items)

    chart.resize(960, chart.minimumHeight())
    chart.show()
    qapp.processEvents()

    assert chart.height() >= chart.minimumHeight()
    assert not chart.grab().isNull()
    assert chart._paint_error == ""
    chart.close()


def test_cashflow_waterfall_explains_result_and_filters_category(qapp):
    theme = Theme(dark=False)
    chart = CashFlowWaterfallChart(lambda: theme)
    chart.resize(980, 390)
    chart.set_data(
        5000,
        3200,
        [
            ("Casa", 1400, "#0A84FF"),
            ("Alimentação", 800, "#FF9F0A"),
            ("Transportes", 450, "#BF5AF2"),
            ("Lazer", 300, "#FF375F"),
            ("Saúde", 150, "#30D158"),
            ("Outros", 100, "#94A3B8"),
        ],
        "últimos 3 meses",
        previous_income=4_700,
        previous_expenses=3_400,
        previous_categories=[
            ("Casa", 1_600, "#0A84FF"),
            ("Alimentação", 750, "#FF9F0A"),
        ],
        comparison_label="período anterior",
    )
    chart.show()
    qapp.processEvents()
    assert not chart.grab().isNull()
    assert chart._paint_error == ""
    assert chart.items[0]["role"] == "income"
    assert chart.items[-1]["role"] == "result"
    assert chart.items[-1]["end"] == 1800
    assert len(chart._bar_hit_rects) == len(chart.items)
    assert chart._comparison_amount(chart.items[0]) == pytest.approx(4_700)
    assert chart._comparison_amount(chart.items[1]) == pytest.approx(1_600)
    assert chart._comparison_amount(chart.items[-1]) == pytest.approx(1_300)

    selected = []
    chart.filter_clicked.connect(
        lambda kind, category: selected.append((kind, category))
    )
    category_rect = chart._bar_hit_rects[1][1]
    QTest.mouseClick(chart, Qt.LeftButton, pos=category_rect.center().toPoint())
    assert selected == [("Despesas", "Casa")]
    assert "taxa de poupança" in chart.accessibleDescription().lower()


@pytest.mark.parametrize("dark", [False, True])
def test_cashflow_waterfall_hover_uses_opaque_light_in_app_card(qapp, dark):
    theme = Theme(dark=dark)
    chart = CashFlowWaterfallChart(lambda: theme)
    chart.resize(980, 390)
    chart.set_data(
        5_000,
        3_200,
        [
            ("Casa", 1_400, "#0A84FF"),
            ("Alimentação", 800, "#FF9F0A"),
            ("Transportes", 450, "#BF5AF2"),
        ],
        "últimos 3 meses",
    )
    chart.show()
    qapp.processEvents()
    chart.grab()
    category_rect = chart._bar_hit_rects[1][1]
    hover = category_rect.center()
    chart.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            hover,
            hover,
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
    )
    qapp.processEvents()

    image = chart.grab().toImage()
    tooltip = chart._tooltip_rect()
    background = image.pixelColor(int(tooltip.right() - 18), int(tooltip.bottom() - 12))

    assert chart.hover_index == 1
    assert chart.toolTip() == ""
    assert background.lightness() > 235
    assert chart._paint_error == ""
    chart.close()


def test_budget_forecast_chart_shows_actual_projection_and_click(qapp):
    theme = Theme(dark=False)
    chart = BudgetForecastChart(lambda: theme)
    chart.resize(980, 420)
    chart.set_rows(
        [
            {
                "category": "Casa e contas",
                "spent": 700,
                "budget": 1200,
                "projected": 1050,
                "previous_spend": 980,
                "forecast_status": "Controlado",
            },
            {
                "category": "Lazer",
                "spent": 180,
                "budget": 220,
                "projected": 285,
                "previous_spend": 240,
                "forecast_status": "Em risco",
            },
            {
                "category": "Restaurantes",
                "spent": 260,
                "budget": 200,
                "projected": 310,
                "previous_spend": 190,
                "forecast_status": "Ultrapassado",
            },
        ],
        "Julho 2026",
    )
    chart.show()
    qapp.processEvents()

    assert not chart.grab().isNull()
    assert chart._paint_error == ""
    assert chart.rows[0]["category"] == "Restaurantes"
    assert len(chart._hit_rects) == 3
    assert "2 em risco" in chart.accessibleDescription()

    selected = []
    chart.category_clicked.connect(selected.append)
    QTest.mouseClick(
        chart, Qt.LeftButton, pos=chart._hit_rects[0][1].center().toPoint()
    )
    assert selected == ["Restaurantes"]


def test_budget_forecast_row_labels_stop_before_100_percent_marker(qapp):
    theme = Theme(dark=False)
    chart = BudgetForecastChart(lambda: theme)
    chart.resize(576, 260)
    chart.set_rows(
        [
            {
                "category": "Restaurantes",
                "spent": 180,
                "budget": 200,
                "projected": 430,
                "previous_spend": 160,
                "forecast_status": "Ultrapassado",
            }
        ],
        "Julho 2026",
    )
    chart.show()
    qapp.processEvents()

    assert not chart.grab().isNull()
    assert chart._summary_rects
    plot = chart._plot_rect()
    limit_x = plot.left() + plot.width() * 100.0 / chart._scale_max()
    assert all(rect.right() <= limit_x - 10.0 for rect in chart._summary_rects)
    assert chart._paint_error == ""
    chart.close()


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("privacy", [False, True])
def test_habit_impact_chart_is_interactive_and_theme_safe(qapp, dark, privacy):
    theme = Theme(dark=dark, privacy=privacy)
    chart = HabitImpactChart(lambda: theme)
    chart.resize(960, 480)
    chart.set_data(
        simulate_habit(
            HABIT_PRESETS_BY_KEY["casino"],
            HabitSimulationInputs(
                unit_cost=30.0,
                frequency=1.0,
                frequency_unit="week",
                annual_inflation_pct=2.0,
                annual_investment_rate_pct=5.0,
                gambling=True,
                win_probability_pct=30.0,
                payout_multiplier=1.5,
            ),
        )
    )
    chart.show()
    qapp.processEvents()

    assert not chart.grab().isNull()
    assert chart._paint_error == ""
    assert len(chart.rows) == 7
    assert len(chart.series) == 360
    assert len(chart._point_rects) == 7
    assert "Casino" in chart.accessibleDescription()
    if privacy:
        assert "••••• €" in chart.accessibleDescription()

    target = chart._point_rects[4].center()
    chart.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            target,
            target,
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
    )
    qapp.processEvents()
    image = chart.grab().toImage()
    tooltip = chart._tooltip_rect()
    background = image.pixelColor(int(tooltip.right() - 18), int(tooltip.bottom() - 12))
    assert chart.hover_index == 59
    assert chart.toolTip() == ""
    assert background.lightness() < 90 if dark else background.lightness() > 235
    month_1_x = chart._point_rects[0].center().x()
    month_120_x = chart._point_rects[5].center().x()
    month_360_x = chart._point_rects[6].center().x()
    assert (month_120_x - month_1_x) / (month_360_x - month_1_x) == pytest.approx(
        119 / 359,
        abs=0.01,
    )

    selected = []
    chart.horizon_selected.connect(selected.append)
    QTest.mouseClick(chart, Qt.LeftButton, pos=target.toPoint())
    assert selected == [60]
    chart.close()


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("privacy", [False, True])
def test_habit_plan_chart_and_auditor_are_clear_and_interactive(qapp, dark, privacy):
    theme = Theme(dark=dark, privacy=privacy)
    chart = HabitPlanTrackingChart(lambda: theme)
    chart.resize(960, 420)
    chart.set_data(
        {
            "plan_name": "Reduzir cafés",
            "rows": [
                {
                    "month": f"2026-{month:02d}",
                    "label": f"{month:02d}/2026",
                    "planned_cost": 20.0,
                    "actual_cost": actual,
                    "cumulative_planned": month * 20.0,
                    "cumulative_actual": cumulative,
                    "partial": month == 4,
                }
                for month, actual, cumulative in (
                    (1, 18.0, 18.0),
                    (2, 24.0, 42.0),
                    (3, 14.0, 56.0),
                    (4, 8.0, 64.0),
                )
            ],
        }
    )
    chart.show()
    qapp.processEvents()
    target = QPointF(chart._plot().center().x(), chart._plot().center().y())
    chart.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            target,
            target,
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
    )
    qapp.processEvents()
    assert chart.hover_index is not None
    assert chart.toolTip() == ""
    assert not chart.grab().isNull()
    assert chart._paint_error == ""
    assert "Reduzir cafés" in chart.accessibleDescription()
    if privacy:
        assert "€" in chart.accessibleDescription()

    dialog = HabitAuditDialog(
        habit_name="Cafés",
        rows=[
            {
                "id": 1,
                "financial_date": "2026-07-10",
                "description": "Café habitual",
                "amount": 1.2,
                "category": "Restauração",
                "subcategory": "",
                "merchant": "Pastelaria",
                "built_in_match": True,
                "selected": True,
                "match_kind": "keyword",
            },
            {
                "id": 2,
                "financial_date": "2026-07-11",
                "description": "Máquina do escritório",
                "amount": 0.8,
                "category": "Diversos",
                "subcategory": "",
                "merchant": "",
                "built_in_match": False,
                "selected": False,
                "match_kind": "",
            },
        ],
        audit_rules={},
        privacy=privacy,
    )
    dialog.show()
    qapp.processEvents()
    assert dialog.width() <= 920
    assert dialog.height() <= 620
    dialog.keywords.setText("máquina do escritório")
    qapp.processEvents()
    rules = dialog.rules()
    assert rules["keywords"] == ["máquina do escritório"]
    assert rules["included_transaction_ids"] == []
    dialog.table.item(0, 0).setCheckState(Qt.Unchecked)
    rules = dialog.rules()
    assert rules["excluded_transaction_ids"] == [1]
    dialog.close()
    chart.close()


@pytest.mark.parametrize("dark", [False, True])
def test_budget_forecast_hover_uses_opaque_themed_in_app_card(qapp, dark):
    theme = Theme(dark=dark)
    chart = BudgetForecastChart(lambda: theme)
    chart.resize(980, 260)
    chart.set_rows(
        [
            {
                "category": "Subscrições",
                "spent": 45.99,
                "budget": 95,
                "projected": 52.01,
                "previous_spend": 10.98,
                "forecast_status": "Controlado",
            }
        ],
        "Julho 2026",
    )
    chart.show()
    qapp.processEvents()
    chart.grab()

    hover = chart._hit_rects[0][1].center()
    chart.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            hover,
            hover,
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
    )
    qapp.processEvents()

    image = chart.grab().toImage()
    tooltip = chart._tooltip_rect()
    background = image.pixelColor(int(tooltip.right() - 18), int(tooltip.bottom() - 12))

    assert chart.hover_index == 0
    assert chart.toolTip() == ""
    assert background.lightness() < 90 if dark else background.lightness() > 235
    assert background.alpha() == 255
    assert chart._paint_error == ""
    chart.close()


def test_budget_forecast_chart_keeps_every_active_budget_category(qapp):
    theme = Theme(dark=False)
    chart = BudgetForecastChart(lambda: theme)
    rows = [
        {
            "category": f"Categoria {index}",
            "spent": 50 + index,
            "budget": 100,
            "projected": 70 + index,
            "previous_spend": 40 + index,
            "forecast_status": "Controlado",
        }
        for index in range(11)
    ]

    chart.set_rows(rows, "Julho 2026")

    assert len(chart.rows) == 11
    assert "11 categorias" in chart.accessibleDescription()
    assert chart.minimumHeight() == 108 + 11 * 36


def test_budget_forecast_chart_keeps_budgeted_categories_without_spending(qapp):
    theme = Theme(dark=False)
    chart = BudgetForecastChart(lambda: theme)

    chart.set_rows(
        [
            {
                "category": "Subscrições",
                "spent": 45.99,
                "budget": 95,
                "projected": 52.01,
                "previous_spend": 10.98,
                "forecast_status": "Controlado",
            },
            {
                "category": "Supermercado",
                "spent": 0,
                "budget": 550,
                "projected": 0,
                "previous_spend": 0,
                "forecast_status": "Controlado",
            },
            {
                "category": "Transporte",
                "spent": 0,
                "budget": 180,
                "projected": 0,
                "previous_spend": 0,
                "forecast_status": "Controlado",
            },
            {
                "category": "Farmácia",
                "spent": 18.5,
                "budget": 0,
                "projected": 21,
                "previous_spend": 0,
                "forecast_status": "Sem orçamento",
            },
            {
                "category": "Sem dados",
                "spent": 0,
                "budget": 0,
                "projected": 0,
                "previous_spend": 0,
                "forecast_status": "Sem orçamento",
            },
        ],
        "Julho 2026",
    )

    assert {row["category"] for row in chart.rows} == {
        "Subscrições",
        "Supermercado",
        "Transporte",
        "Farmácia",
    }
    assert "4 categorias" in chart.accessibleDescription()
    assert "3 com orçamento" in chart.accessibleDescription()
    assert "1 sem orçamento" in chart.accessibleDescription()
    assert "2 com valor realizado" in chart.accessibleDescription()

    chart.resize(980, chart.minimumHeight())
    chart.show()
    qapp.processEvents()
    assert not chart.grab().isNull()
    assert chart._paint_error == ""


def test_compact_value_items_preserves_the_full_total():
    source = [(f"Categoria {index}", float(index + 1)) for index in range(11)]

    compacted = compact_value_items(source, visible_limit=8)

    assert len(compacted) == 9
    assert compacted[-1][0] == "Outros (3)"
    assert sum(value for _label, value in compacted) == pytest.approx(
        sum(value for _label, value in source)
    )


def test_cashflow_timeline_switches_horizon_and_opens_event(qapp):
    theme = Theme(dark=False)
    start = date(2026, 7, 12)
    events = [
        {
            "date": (start + timedelta(days=3)).isoformat(),
            "name": "Prestação",
            "amount": -500,
            "source": "scheduled",
            "kind": "expense",
            "category": "Casa e contas",
        },
        {
            "date": (start + timedelta(days=8)).isoformat(),
            "name": "Streaming",
            "amount": -20,
            "source": "subscription",
            "kind": "expense",
            "category": "Subscrições",
        },
        {
            "date": (start + timedelta(days=21)).isoformat(),
            "name": "Salário",
            "amount": 2000,
            "source": "recurring_income",
            "kind": "income",
            "category": "Rendimentos",
        },
        {
            "date": (start + timedelta(days=25)).isoformat(),
            "name": "Compra futura",
            "amount": -80,
            "source": "recorded",
            "kind": "expense",
            "category": "Diversos",
        },
    ]
    points = []
    for offset in range(91):
        balance = 3000 - offset * 20
        if offset >= 3:
            balance -= 500
        if offset >= 8:
            balance -= 20
        if offset >= 21:
            balance += 2000
        if offset >= 25:
            balance -= 80
        point_date = (start + timedelta(days=offset)).isoformat()
        points.append(
            {
                "date": point_date,
                "balance": balance,
                "balance_pessimistic": balance - offset * 4,
                "balance_base": balance,
                "balance_optimistic": balance + offset * 4,
                "income": 2000 if offset == 21 else 0,
                "expense": 20 + (500 if offset == 3 else 0),
                "baseline_expense": 20,
                "event_count": sum(
                    1 for event in events if event["date"] == point_date
                ),
            }
        )

    chart = CashflowTimelineChart(lambda: theme)
    chart.resize(1080, 410)
    chart.set_data({"points": points, "events": events}, 30)
    chart.show()
    qapp.processEvents()

    assert not chart.grab().isNull()
    assert chart._paint_error == ""
    assert len(chart.points) == 31
    assert len(chart._event_hits) == 4
    assert "30 dias" in chart.accessibleDescription()

    selected = []
    chart.event_clicked.connect(selected.append)
    QTest.mouseClick(
        chart, Qt.LeftButton, pos=chart._event_hits[0][1].center().toPoint()
    )
    assert selected[0]["name"] == "Prestação"

    chart.set_horizon(60)
    assert len(chart.points) == 61
    assert chart.horizon_days == 60


def test_toast_notifications_stack_without_overlap(qapp):
    parent = QWidget()
    parent.resize(900, 600)
    parent.show()
    first = ToastNotification(parent, "Primeiro aviso", "✅", duration_ms=9999)
    second = ToastNotification(
        parent,
        "Segundo aviso com texto longo para validar a quebra de linha.",
        "⚠️",
        duration_ms=9999,
    )
    first.show_toast()
    second.show_toast()
    qapp.processEvents()

    assert second.y() >= first.y() + first.height()
    assert first.x() + first.width() == second.x() + second.width()
    assert first.x() >= 0
    assert second.x() >= 0


def test_dashboard_notice_is_readable_and_anchored(qapp):
    parent = QWidget()
    parent.resize(1000, 700)
    parent.show()
    anchor = QWidget(parent)
    anchor.setGeometry(810, 32, 150, 40)
    anchor.show()
    notice = ToastNotification(
        parent,
        "Confirma os valores e as datas na área de Subscrições.",
        "📅",
        duration_ms=9999,
        title="Renovações próximas",
        tone="warning",
        anchor_widget=anchor,
        theme=Theme(dark=False),
    )
    notice.show_toast()
    qapp.processEvents()

    assert notice.title_lbl.text() == "Renovações próximas"
    assert notice.msg_lbl.wordWrap()
    assert notice.progress.value() > 0
    final_position = notice.slide_anim.endValue()
    assert final_position.x() + notice.width() == anchor.x() + anchor.width()
    assert notice.x() + notice.width() == anchor.x() + anchor.width()
    assert final_position.y() == anchor.y() + anchor.height() + 10
    assert notice.accessibleDescription().startswith("Confirma")


def test_dashboard_notices_realign_after_anchor_moves(qapp):
    parent = QWidget()
    parent.resize(1000, 700)
    parent.show()
    anchor = QWidget(parent)
    anchor.setGeometry(540, 32, 150, 40)
    anchor.show()
    first = ToastNotification(
        parent,
        "A tua taxa chegou aos 31%.",
        "🏆",
        duration_ms=9999,
        title="Excelente ritmo de poupança",
        tone="success",
        anchor_widget=anchor,
        theme=Theme(dark=False),
    )
    first.show_toast()
    qapp.processEvents()

    anchor.move(810, 32)
    second = ToastNotification(
        parent,
        "Confirma a próxima renovação.",
        "📅",
        duration_ms=9999,
        title="Renovações próximas",
        tone="warning",
        anchor_widget=anchor,
        theme=Theme(dark=False),
    )
    second.show_toast()
    qapp.processEvents()

    expected_right = anchor.x() + anchor.width()
    assert first.x() + first.width() == expected_right
    assert second.x() + second.width() == expected_right
    assert second.y() >= first.y() + first.height()


def test_treemap_keeps_every_category_in_tiles_without_footer_legend(qapp):
    theme = Theme(dark=False)
    chart = TreemapChart(lambda: theme)
    chart.resize(1080, 730)
    categories = [
        (f"Categoria {index + 1}", 1000 / (index + 1), "#0A84FF") for index in range(18)
    ]
    chart.set_data(categories)
    chart.show()
    qapp.processEvents()
    assert not chart.grab().isNull()
    assert chart._paint_error == ""
    assert set(chart.data) == {row[0] for row in categories}
    assert {label for _rect, label in chart.hit_boxes} == set(chart.data)
    assert max(rect.bottom() for rect, _label in chart.hit_boxes) > chart.height() * 0.9
    assert chart.minimumHeight() >= 540


def test_investment_evolution_renders_two_real_series(qapp):
    theme = Theme(dark=False)
    chart = InvestmentEvolutionChart(lambda: theme)
    chart.resize(1080, 380)
    rows = [
        {
            "date": f"2026-0{month}-01",
            "invested": 10000 + month * 900,
            "total": 10200 + month * 1120,
        }
        for month in range(1, 7)
    ]
    chart.set_data(rows, portfolio_return_pct=8.4)
    chart.show()
    qapp.processEvents()

    assert len(chart.rows) == 6
    assert chart.rows[-1]["total"] > chart.rows[-1]["invested"]
    assert "Rentabilidade ponderada" in chart.accessibleDescription()
    assert not chart.grab().isNull()
    assert chart._paint_error == ""


def test_investment_projection_updates_five_years_from_slider_data(qapp):
    theme = Theme(dark=True)
    chart = InvestmentProjectionChart(lambda: theme)
    chart.resize(1080, 390)

    def projection(monthly: float) -> dict:
        rows = []
        monthly_rate = 0.05 / 12
        for year in range(1, 6):
            months = year * 12
            invested = monthly * months
            projected = monthly * (((1 + monthly_rate) ** months - 1) / monthly_rate)
            rows.append(
                {
                    "year": year,
                    "months": months,
                    "invested": invested,
                    "projected": projected,
                    "projected_base": projected,
                    "projected_pessimistic": projected * 0.94,
                    "projected_optimistic": projected * 1.06,
                }
            )
        return {
            "monthly_contribution": monthly,
            "annual_rate_pct": 5.0,
            "rows": rows,
        }

    chart.set_projection(projection(250.0))
    chart.show()
    qapp.processEvents()
    assert [row["year"] for row in chart.rows] == [1, 2, 3, 4, 5]
    assert chart.rows[-1]["projected"] > chart.rows[-1]["invested"]
    assert (
        chart.rows[-1]["projected_pessimistic"]
        < chart.rows[-1]["projected_base"]
        < chart.rows[-1]["projected_optimistic"]
    )
    assert "rentabilidade" in chart.accessibleDescription()
    assert not chart.grab().isNull()
    assert chart._paint_error == ""

    first_value = chart.rows[-1]["projected"]
    chart.set_projection(projection(500.0))
    QTest.qWait(320)
    assert chart.rows[-1]["projected"] == pytest.approx(first_value * 2)
    hover_position = QPointF(chart.width() / 2, chart.height() / 2)
    chart.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            hover_position,
            hover_position,
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
    )
    qapp.processEvents()
    assert chart.hover_index is not None
    assert not chart.grab().isNull()
    assert chart._paint_error == ""


@pytest.mark.parametrize("width", [960, 1280, 1600])
@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("privacy", [False, True])
def test_forecast_charts_render_responsive_theme_and_privacy_states(
    qapp, width, dark, privacy
):
    theme = Theme(dark=dark, privacy=privacy)
    investment = InvestmentProjectionChart(lambda: theme)
    investment.resize(width, 420)
    investment.set_projection(
        build_investment_projection(
            350.0,
            6.0,
            5,
            initial_capital=4_000.0,
            inflation_pct=2.0,
            tax_pct=28.0,
            fee_pct=0.5,
        )
    )

    start = date(2026, 7, 12)
    points = []
    for offset in range(31):
        base = 8_000.0 - offset * 72.0
        points.append(
            {
                "date": (start + timedelta(days=offset)).isoformat(),
                "balance": base,
                "balance_base": base,
                "balance_pessimistic": base - offset * 18.0,
                "balance_optimistic": base + offset * 18.0,
                "income": 0.0,
                "expense": 72.0,
                "baseline_expense": 72.0,
                "event_count": 0,
            }
        )
    cashflow = CashflowTimelineChart(lambda: theme)
    cashflow.resize(width, 420)
    cashflow.set_data({"points": points, "events": []}, 30)

    for chart in (investment, cashflow):
        chart.show()
        qapp.processEvents()
        assert chart.width() == width
        assert not chart.grab().isNull()
        assert chart._paint_error == ""

    descriptions = (
        investment.accessibleDescription(),
        cashflow.accessibleDescription(),
    )
    if privacy:
        assert all("••••• €" in description for description in descriptions)
    else:
        assert all(
            "€" in description and "•••••" not in description
            for description in descriptions
        )


def test_heatmap_always_draws_all_compact_weekday_labels(qapp):
    theme = Theme(dark=False)
    chart = HeatmapChart(lambda: theme)
    chart.resize(720, 210)
    start = date(2026, 1, 1)
    chart.set_data(
        [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "expense": float(index % 9),
            }
            for index in range(365)
        ]
    )
    chart.show()
    qapp.processEvents()

    assert [label for label, _rect in chart.weekday_label_rects] == [
        "Seg",
        "Ter",
        "Qua",
        "Qui",
        "Sex",
        "Sáb",
        "Dom",
    ]
    assert all(rect.height() >= 13 for _label, rect in chart.weekday_label_rects)
    assert not chart.grab().isNull()
    assert chart._paint_error == ""


@pytest.mark.parametrize("width", [960, 1280, 1600])
@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("privacy", [False, True])
def test_score_and_resilience_charts_render_responsively(qapp, width, dark, privacy):
    theme = Theme(dark=dark, privacy=privacy)
    dimensions = [
        {
            "key": f"dimension-{index}",
            "name": name,
            "icon": icon,
            "score": score,
            "tone": tone,
            "status": status,
            "marker": marker,
        }
        for index, (name, icon, score, tone, status, marker) in enumerate(
            (
                ("Liquidez e reserva", "🛟", 76, "neutral", "Estável", "🔵"),
                ("Poupança consistente", "🌱", 88, "positive", "Forte", "🟢"),
                ("Disciplina orçamental", "🎯", 57, "warning", "Atenção", "🟡"),
                ("Endividamento", "⚖️", 92, "positive", "Forte", "🟢"),
                ("Custos fixos", "🔁", 38, "negative", "Crítico", "🔴"),
            )
        )
    ]
    breakdown = ScoreBreakdownChart(lambda: theme)
    breakdown.resize(width, 360)
    breakdown.set_data(dimensions, "dimension-2")

    trend_rows = [
        {
            "month": f"2026-{month:02d}",
            "score": 48 + month * 3,
            "grade": "B",
            "status": "Estável",
            "tone": "neutral" if month < 8 else "positive",
        }
        for month in range(1, 13)
    ]
    trend = ScoreTrendChart(lambda: theme)
    trend.resize(width, 330)
    trend.set_data(trend_rows, "2026-12")

    scenario_rows = []
    for month in range(13):
        base = 8_000 + month * 250
        scenario_rows.append(
            {
                "month": month,
                "label": "Hoje" if month == 0 else f"M{month}",
                "base": base,
                "optimistic": base + month * 150,
                "stressed": base - 3_000 - month * 180 if month else base,
                "p10": base - month * 280,
                "p50": base,
                "p90": base + month * 320,
            }
        )
    scenario = ResilienceScenarioChart(lambda: theme)
    scenario.resize(width, 430)
    scenario.set_data(scenario_rows)

    for chart in (breakdown, trend, scenario):
        chart.show()
        qapp.processEvents()
        assert not chart.grab().isNull()
        assert chart._paint_error == ""

    selected: list[str] = []
    breakdown.dimension_clicked.connect(selected.append)
    target = breakdown._row_rects[2].center().toPoint()
    target_f = QPointF(target)
    breakdown.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            target_f,
            target_f,
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
    )
    breakdown.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            target_f,
            target_f,
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
    )
    assert selected == ["dimension-2"]

    selected_months: list[str] = []
    trend.month_clicked.connect(selected_months.append)
    trend_plot = trend._plot()
    trend_target = QPointF(
        trend_plot.left() + trend_plot.width() * 5 / 11,
        trend_plot.center().y(),
    )
    trend.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            trend_target,
            trend_target,
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
    )
    trend.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            trend_target,
            trend_target,
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
    )
    assert selected_months == ["2026-06"]

    selected_points: list[dict] = []
    scenario.point_clicked.connect(selected_points.append)
    scenario_plot = scenario._plot()
    scenario_target = QPointF(
        scenario_plot.left() + scenario_plot.width() / 2,
        scenario_plot.center().y(),
    )
    scenario.mouseMoveEvent(
        QMouseEvent(
            QEvent.Type.MouseMove,
            scenario_target,
            scenario_target,
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
    )
    scenario.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            scenario_target,
            scenario_target,
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
    )
    assert selected_points and selected_points[0]["month"] == 6
    for chart in (breakdown, trend, scenario):
        chart.close()


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("privacy", [False, True])
def test_financial_intelligence_charts_render_in_all_visual_states(qapp, dark, privacy):
    theme = Theme(dark=dark, privacy=privacy)
    monte = MonteCarloChart(lambda: theme)
    monte.resize(960, 420)
    monte.set_data(
        monte_carlo_projection(
            300,
            6,
            12,
            10,
            simulations=300,
            target=60_000,
            initial_capital=5_000,
        )
    )

    net_worth = NetWorthChart(lambda: theme)
    net_worth.resize(960, 280)
    net_worth.set_data(
        {
            "total_assets": 125_000,
            "total_liabilities": 42_000,
            "net_worth": 83_000,
            "debt_ratio_pct": 33.6,
            "monthly_debt_payment": 650,
        }
    )

    comparison = PeriodComparisonChart(lambda: theme)
    comparison.resize(960, 420)
    comparison.set_data(
        {
            "categories": [
                {
                    "category": f"Categoria {index}",
                    "current": 0 if index == 14 else 900 / index,
                    "reference": 0 if index == 14 else 720 / index,
                    "change_pct": 0 if index == 14 else 25,
                }
                for index in range(1, 15)
            ]
        }
    )
    assert len(comparison.rows) == 14
    assert comparison.minimumHeight() >= 52 + 14 * 46
    assert "14 categorias comparadas" in comparison.accessibleDescription()

    for chart in (monte, net_worth, comparison):
        chart.show()
        qapp.processEvents()
        assert not chart.grab().isNull()
        assert chart._paint_error == ""

    if privacy:
        assert "••••• €" in monte.accessibleDescription()
        assert "••••• €" in net_worth.accessibleDescription()
    else:
        assert "€" in monte.accessibleDescription()
        assert "€" in net_worth.accessibleDescription()
