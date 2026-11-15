"""Phase 3 navigation, dialog and active analytical-path regressions."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QScrollArea,
    QStackedWidget,
    QWidget,
)

from main import LiquidFinanceApp
from ui.core.widgets import LiquidSheetDialog


class _Pages(dict):
    def existing(self, key):
        return self.get(key)


class _Navigation(SimpleNamespace):
    goto = LiquidFinanceApp.goto
    _finish_page_transition = LiquidFinanceApp._finish_page_transition


@pytest.fixture
def navigation(monkeypatch):
    import main

    app = QApplication.instance() or QApplication([])
    # Layout callbacks are unrelated to this regression; Qt still advances
    # the real QPropertyAnimation through its event loop.
    monkeypatch.setattr(main.QTimer, "singleShot", lambda *_args: None)
    stack = QStackedWidget()
    pages = _Pages((key, QWidget()) for key in ("a", "b", "c"))
    for page in pages.values():
        stack.addWidget(page)
    nav = _Navigation(
        pages=pages,
        stack=stack,
        content_scroll=QScrollArea(),
        _current_page_key="a",
        _page_scroll_positions={},
        _scroll_restore_generation=0,
        _page_refreshed_at={key: datetime.now().timestamp() for key in pages},
        _dirty_page_keys=set(),
        nav_buttons={},
        _sync_dashboard_sticky_filters=lambda _key: None,
        sync_content_size=lambda: None,
        _update_dashboard_sticky_summary=lambda: None,
        _update_page_tools=lambda: None,
    )
    yield nav
    nav._finish_page_transition()
    # Complete this fixture's Qt deletions while its widget tree is still owned.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    stack.close()
    nav.content_scroll.close()
    app.processEvents()


def test_rapid_navigation_removes_previous_page_opacity(navigation):
    navigation.goto("b")
    old_animation = navigation._transition_anim
    assert navigation.pages["b"].graphicsEffect() is not None
    navigation.goto("c")
    assert navigation.pages["b"].graphicsEffect() is None
    assert old_animation.targetObject() is None
    current_effect = navigation.pages["c"].graphicsEffect()
    old_animation.finished.emit()
    assert navigation.pages["c"].graphicsEffect() is current_effect
    # A stale completion must never clear the new page's transition.
    navigation._finish_page_transition(old_animation)
    assert navigation.pages["c"].graphicsEffect() is current_effect
    QTest.qWait(250)
    assert navigation.pages["c"].graphicsEffect() is None
    assert navigation._transition_anim is None
    assert navigation._transition_widget is None
    assert navigation._transition_effect is None


def test_repeated_navigation_and_reselect_leave_no_dimmed_pages(navigation):
    for key in ("b", "a", "c", "b", "b", "a"):
        navigation.goto(key)
        assert all(
            page.graphicsEffect() is None
            for name, page in navigation.pages.items()
            if name != key
        )
    QTest.qWait(250)
    assert all(page.graphicsEffect() is None for page in navigation.pages.values())


def test_transition_cleanup_tolerates_destroyed_page(navigation):
    from shiboken6 import delete

    navigation.goto("b")
    delete(navigation.pages["b"])
    navigation._finish_page_transition()
    assert navigation._transition_anim is None
    assert navigation._transition_widget is None
    navigation.goto("c")
    QTest.qWait(250)
    assert navigation.pages["c"].graphicsEffect() is None


def test_transition_cleanup_preserves_an_unrelated_replacement_effect(navigation):
    navigation.goto("b")
    page = navigation.pages["b"]
    replacement = QGraphicsOpacityEffect(page)
    replacement.setOpacity(1.0)
    page.setGraphicsEffect(replacement)
    navigation._finish_page_transition()
    assert page.graphicsEffect() is replacement
    page.setGraphicsEffect(None)


def test_transition_cleanup_is_idempotent(navigation):
    navigation.goto("b")
    navigation._finish_page_transition()
    navigation._finish_page_transition()
    assert navigation.pages["b"].graphicsEffect() is None
    assert navigation._transition_anim is None
    assert navigation._transition_widget is None
    assert navigation._transition_effect is None


def test_shutdown_releases_transition_even_while_waiting_for_workers(navigation):
    from PySide6.QtGui import QCloseEvent

    navigation.goto("b")
    polls = []
    navigation._shutdown_started = True
    navigation.background_task_registry = SimpleNamespace(has_active_tasks=lambda: True)
    navigation.statusBar = lambda: SimpleNamespace(showMessage=lambda _message: None)
    navigation._shutdown_task_message = lambda: "A aguardar tarefas"
    navigation._shutdown_poll_timer = SimpleNamespace(
        isActive=lambda: False, start=lambda: polls.append(True)
    )
    event = QCloseEvent()
    LiquidFinanceApp.closeEvent(navigation, event)
    assert not event.isAccepted()
    assert polls == [True]
    assert navigation.pages["b"].graphicsEffect() is None
    assert navigation._transition_anim is None
    assert navigation._transition_widget is None
    assert navigation._transition_effect is None


def test_liquid_sheet_covers_nested_parent_in_global_coordinates():
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.setGeometry(100, 100, 900, 700)
    page = QWidget(window)
    page.setGeometry(120, 80, 600, 500)
    window.show()
    app.processEvents()
    dialog = LiquidSheetDialog(page)
    try:
        dialog.show()
        app.processEvents()
        assert dialog.pos() == page.mapToGlobal(QPoint(0, 0))
        assert dialog.size() == page.size()
    finally:
        dialog.hide()
        window.close()


def test_refresh_all_invalidates_the_active_analytics_service():
    invalidations = []
    app = SimpleNamespace(
        analytics_service=SimpleNamespace(
            invalidate_caches=lambda: invalidations.append(True)
        ),
        _dirty_page_keys=set(),
        page_factories={"dashboard": object()},
        pages={},
        stack=SimpleNamespace(currentWidget=lambda: None),
        statusBar=lambda: SimpleNamespace(showMessage=lambda *_args: None),
    )
    LiquidFinanceApp.refresh_all(app)
    assert invalidations == [True]
    assert app._dirty_page_keys == {"dashboard"}


def test_portfolio_monte_carlo_captures_coverage_before_worker(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import get_ident

    import services.financial_intelligence as intelligence
    from ui.pages.investments import PortfolioPage

    caller = get_ident()
    coverage_threads = []
    calculation_threads = []
    pending = []

    def coverage(*_args):
        coverage_threads.append(get_ident())
        return {"active_months": 12}

    def calculation(*_args, **_kwargs):
        calculation_threads.append(get_ident())
        return {"rows": []}

    monkeypatch.setattr(intelligence, "monte_carlo_projection", calculation)

    def field(value):
        return SimpleNamespace(value=lambda: value)

    page = SimpleNamespace(
        should_compute_card=lambda _card: True,
        monte_carlo_card=object(),
        projection_slider=field(200),
        projection_rate=field(7),
        monte_carlo_volatility=field(15),
        monte_carlo_years=SimpleNamespace(currentData=lambda: 10),
        monte_carlo_simulations=SimpleNamespace(currentData=lambda: 100),
        monte_carlo_target=field(50_000),
        _projection_mode=lambda: "monthly",
        projection_initial=field(1000),
        projection_inflation=field(2),
        projection_tax=field(28),
        projection_fee=field(0.2),
        monte_carlo_correlation=field(25),
        _monte_carlo_asset_weights=[],
        _monte_carlo_history_points=[],
        _monte_carlo_task=SimpleNamespace(
            is_running=lambda: False, start=pending.append
        ),
        analytics_service=SimpleNamespace(data_coverage_snapshot=coverage),
    )
    PortfolioPage._update_monte_carlo(page)
    assert coverage_threads == [caller]
    assert len(pending) == 1
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(pending[0]).result(timeout=2)
    assert calculation_threads[0] != caller
    assert coverage_threads == [caller]
    assert result["data_coverage"] == {"active_months": 12}
