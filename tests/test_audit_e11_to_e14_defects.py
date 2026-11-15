from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FINANCAS_PESSOAIS_TESTING"] = "1"

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QPainter
from PySide6.QtWidgets import QApplication

from ui.components.charts.bar import BarChart, StackedBarChart
from ui.components.charts.base import StableChart
from ui.components.charts.budget import BudgetForecastChart
from ui.components.charts.heatmap import HeatmapChart
from ui.components.charts.utils import _chart_tooltip_bg
from ui.core import Theme


def _get_app() -> QApplication:
    return QApplication.instance() or QApplication([])


# ============================================================================
# E11 (C37): Keyboard-selected chart points open details and category fallback
# ============================================================================

class _SampleDetailChart(StableChart):
    def __init__(self):
        super().__init__(lambda: Theme(dark=False))
        self.hover_index: int | None = None
        self.rows = [
            {"date": "2026-07-01", "balance": 1500.0},
            {"date": "2026-07-02", "balance": 1650.0},
            {"date": "2026-07-03", "balance": 1800.0},
        ]
        self.points = list(self.rows)


def test_e11_keyboard_navigation_enter_and_space_open_detail():
    _get_app()
    chart = _SampleDetailChart()
    chart.resize(400, 300)

    # 1. Left/Right/Home/End navigation
    press_right = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Right, Qt.NoModifier)
    chart.keyPressEvent(press_right)
    assert chart.hover_index == 0

    chart.keyPressEvent(press_right)
    assert chart.hover_index == 1

    # 2. Return opens detail
    chart._last_detail_payload = None
    press_enter = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)
    chart.keyPressEvent(press_enter)
    assert chart._last_detail_payload is not None
    assert chart._last_detail_payload["subtitle"] == "2026-07-02"

    # 3. Space also opens detail
    press_right = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Right, Qt.NoModifier)
    chart.keyPressEvent(press_right)
    assert chart.hover_index == 2

    chart._last_detail_payload = None
    press_space = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Space, Qt.NoModifier)
    chart.keyPressEvent(press_space)
    assert chart._last_detail_payload is not None
    assert chart._last_detail_payload["subtitle"] == "2026-07-03"

    # 4. Escape clears selection and pinned items consistently
    chart.pin_detail_item({"date": "2026-07-03", "balance": 1800.0})
    assert chart._pinned_detail_item is not None
    assert chart.hover_index == 2

    press_esc = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    chart.keyPressEvent(press_esc)
    assert chart._pinned_detail_item is None
    assert chart.hover_index is None
    chart.close()


def test_e11_category_chart_preserves_category_clicked_signal():
    _get_app()
    chart = BudgetForecastChart(lambda: Theme(dark=False))
    chart.resize(500, 300)
    chart.set_rows(
        [
            {"category": "Alimentação", "spent": 120.0, "budget": 200.0, "projected": 180.0},
            {"category": "Transportes", "spent": 50.0, "budget": 80.0, "projected": 75.0},
        ],
        month_label="Julho 2026",
    )

    emitted: list[str] = []
    chart.category_clicked.connect(emitted.append)

    # Rows are prioritized by budget risk
    first_cat = chart.rows[0]["category"]
    second_cat = chart.rows[1]["category"]

    # Select second item (index 1)
    press_right = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Right, Qt.NoModifier)
    chart.keyPressEvent(press_right)
    chart.keyPressEvent(press_right)
    assert chart.hover_index == 1

    # Enter emits category_clicked for the selected item
    press_enter = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Enter, Qt.NoModifier)
    chart.keyPressEvent(press_enter)
    assert emitted == [second_cat]

    # Space also emits category_clicked for the selected item
    press_left = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Left, Qt.NoModifier)
    chart.keyPressEvent(press_left)
    assert chart.hover_index == 0

    press_space = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Space, Qt.NoModifier)
    chart.keyPressEvent(press_space)
    assert emitted == [second_cat, first_cat]
    chart.close()


# ============================================================================
# E12 (C38): Chart details render numeric zero as distinct from missing
# ============================================================================

def test_e12_format_detail_value_distinguishes_zero_from_missing():
    _get_app()
    chart = StableChart(lambda: Theme(dark=False, privacy=False))

    # Missing / None / empty
    assert chart._format_detail_value("score", None) == "—"
    assert chart._format_detail_value("notes", "") == "—"
    assert chart._format_detail_value("event_count", None) == "—"

    # Numeric zero in various declared and undeclared roles
    assert chart._format_detail_value("score", 0) == "0"
    assert chart._format_detail_value("score", 0.0) == "0,00"
    assert chart._format_detail_value("event_count", 0) == "0"
    assert chart._format_detail_value("movements", 0) == "0"
    assert chart._format_detail_value("count", 0) == "0"
    assert chart._format_detail_value("accuracy_pct", 0.0) == "0,0%"
    assert chart._format_detail_value("accuracy_pct", 0) == "0,0%"
    assert chart._format_detail_value("balance", 0.0) == "0 €"
    assert chart._format_detail_value("balance", 0) == "0 €"
    assert chart._format_detail_value("unknown_metric", 0) == "0"

    # Non-zero positive and negative values
    assert chart._format_detail_value("score", 85) == "85"
    assert chart._format_detail_value("event_count", 12) == "12"
    assert chart._format_detail_value("movements", -3) == "-3"
    assert chart._format_detail_value("accuracy_pct", 94.5) == "94,5%"
    assert chart._format_detail_value("balance", -50.0) == "-50 €"

    # Privacy masking applies to money fields but not count/number/zero
    chart_priv = StableChart(lambda: Theme(dark=False, privacy=True))
    assert chart_priv._format_detail_value("balance", 0.0) == "••••• €"
    assert chart_priv._format_detail_value("balance", 120.0) == "••••• €"
    # A05 privacy policy also hides movement/event counts, including zero.
    assert chart_priv._format_detail_value("event_count", 0) == "•••"
    assert chart_priv._format_detail_value("score", 0) == "0"
    chart.close()
    chart_priv.close()


def test_e12_default_field_types_schema():
    types = StableChart._DEFAULT_DETAIL_FIELD_TYPES
    assert types.get("event_count") == "count"
    assert types.get("movements") == "count"
    assert types.get("count") == "count"
    assert types.get("score") == "number"
    assert types.get("p10") == "money"
    assert types.get("p50") == "money"
    assert types.get("p90") == "money"
    assert types.get("probability_target") == "percent"


# ============================================================================
# E13 (C39): Tooltips respect active theme tokens (light and dark)
# ============================================================================

def test_e13_bar_chart_tooltip_uses_theme_tokens():
    _get_app()
    light_theme = Theme(dark=False)
    dark_theme = Theme(dark=True)

    # Verify helper values
    bg_light = _chart_tooltip_bg(light_theme)
    bg_dark = _chart_tooltip_bg(dark_theme)
    assert bg_light == QColor(255, 255, 255, 248)
    assert bg_dark == QColor(17, 24, 39, 244)

    # BarChart rendering with tooltip active
    bar_light = BarChart(lambda: light_theme)
    bar_light.resize(400, 300)
    bar_light.set_items([("Alimentação", 250.0, "#0A84FF")])
    bar_light.hover_index = 0
    bar_light.show()
    pix_light = bar_light.grab()
    assert not pix_light.isNull()

    bar_dark = BarChart(lambda: dark_theme)
    bar_dark.resize(400, 300)
    bar_dark.set_items([("Alimentação", 250.0, "#0A84FF")])
    bar_dark.hover_index = 0
    bar_dark.show()
    pix_dark = bar_dark.grab()
    assert not pix_dark.isNull()

    bar_light.close()
    bar_dark.close()


def test_e13_stacked_bar_chart_tooltip_uses_theme_tokens():
    _get_app()
    light_theme = Theme(dark=False)
    dark_theme = Theme(dark=True)

    stacked_light = StackedBarChart(lambda: light_theme)
    stacked_light.resize(450, 300)
    stacked_light.set_data(
        [{"month": "2026-06", "Habitação": 500.0, "Alimentação": 200.0}],
        [("Habitação", "#0A84FF"), ("Alimentação", "#30D158")],
    )
    stacked_light.hover_index = 0
    stacked_light.show()
    assert not stacked_light.grab().isNull()

    stacked_dark = StackedBarChart(lambda: dark_theme)
    stacked_dark.resize(450, 300)
    stacked_dark.set_data(
        [{"month": "2026-06", "Habitação": 500.0, "Alimentação": 200.0}],
        [("Habitação", "#0A84FF"), ("Alimentação", "#30D158")],
    )
    stacked_dark.hover_index = 0
    stacked_dark.show()
    assert not stacked_dark.grab().isNull()

    stacked_light.close()
    stacked_dark.close()


def test_e13_budget_and_heatmap_tooltips_use_theme_tokens():
    _get_app()
    # BudgetForecastChart
    b_light = BudgetForecastChart(lambda: Theme(dark=False))
    b_light.resize(500, 300)
    b_light.set_rows(
        [{"category": "Lazer", "spent": 60.0, "budget": 100.0, "projected": 90.0}],
        month_label="Julho 2026",
    )
    b_light.hover_index = 0
    b_light._hover_pos = QPointF(100.0, 100.0)
    b_light.show()
    assert not b_light.grab().isNull()

    b_dark = BudgetForecastChart(lambda: Theme(dark=True))
    b_dark.resize(500, 300)
    b_dark.set_rows(
        [{"category": "Lazer", "spent": 60.0, "budget": 100.0, "projected": 90.0}],
        month_label="Julho 2026",
    )
    b_dark.hover_index = 0
    b_dark._hover_pos = QPointF(100.0, 100.0)
    b_dark.show()
    assert not b_dark.grab().isNull()

    # HeatmapChart
    h_light = HeatmapChart(lambda: Theme(dark=False))
    h_light.resize(500, 250)
    h_light.set_data([{"date": "2026-07-01", "expense": 45.0}])
    h_light.hover_index = 0
    h_light.show()
    assert not h_light.grab().isNull()

    h_dark = HeatmapChart(lambda: Theme(dark=True))
    h_dark.resize(500, 250)
    h_dark.set_data([{"date": "2026-07-01", "expense": 45.0}])
    h_dark.hover_index = 0
    h_dark.show()
    assert not h_dark.grab().isNull()

    b_light.close()
    b_dark.close()
    h_light.close()
    h_dark.close()


# ============================================================================
# E14 (C40): Chart rendering displays sanitized error, never raw exception text
# ============================================================================

class _SecretLeakingFailingChart(StableChart):
    def __init__(self, exc: Exception):
        super().__init__(lambda: Theme(dark=False))
        self.exc = exc

    def render_chart(self, painter):
        raise self.exc


def test_e14_chart_error_does_not_render_raw_exception_text(monkeypatch):
    _get_app()
    sensitive_path = "C:\\Users\\renat\\SuperSecretDir\\financas.sqlite"
    sensitive_email = "user.sensitive@example.com"
    sensitive_account = "PT50123499990000"
    raw_error_message = (
        f"Database access violation at {sensitive_path} for {sensitive_email} "
        f"on account {sensitive_account}"
    )

    reported_logs: list[tuple[str, BaseException | None]] = []
    monkeypatch.setattr(
        "ui.components.charts.base.log_exception",
        lambda context, exc=None: reported_logs.append((context, exc)),
    )

    chart = _SecretLeakingFailingChart(RuntimeError(raw_error_message))
    chart.resize(480, 280)
    chart.show()

    # Capture drawn text via mock QPainter to inspect the exact drawn strings
    captured_texts: list[str] = []
    original_draw_text = QPainter.drawText

    def _intercept_draw_text(painter_self, *args, **kwargs):
        for arg in args:
            if isinstance(arg, str):
                captured_texts.append(arg)
        return original_draw_text(painter_self, *args, **kwargs)

    monkeypatch.setattr(QPainter, "drawText", _intercept_draw_text)

    # Force paint
    chart.grab()

    # Verify no raw sensitive strings were painted to the visual chart canvas
    assert sensitive_path not in captured_texts
    assert sensitive_email not in captured_texts
    assert sensitive_account not in captured_texts
    assert raw_error_message not in captured_texts

    # Verify sanitized localized text was painted
    assert "Não foi possível apresentar o gráfico" in captured_texts
    assert any("erro interno" in text for text in captured_texts)

    # Verify internal error log still recorded the failure for diagnostics
    assert chart._paint_error == raw_error_message
    assert len(reported_logs) == 1
    assert "chart=_SecretLeakingFailingChart" in reported_logs[0][0]

    chart.close()
