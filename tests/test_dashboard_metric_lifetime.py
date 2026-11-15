"""Fast refreshes cannot reuse a metrics host awaiting deferred deletion."""

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QGridLayout, QWidget
from shiboken6 import isValid

from ui.components.cards import MetricCard
from ui.pages.dashboard import DashboardPage


def test_fast_result_creates_new_host_before_deferred_delete():
    qapp = QApplication.instance() or QApplication([])
    root = QWidget()
    page = SimpleNamespace(
        cards_grid=QGridLayout(root),
        _secondary_metrics_host=None,
        secondary_metrics_grid=None,
        _secondary_metrics_expanded=True,
        primary_metric_column_count=lambda: 2,
        toggle_secondary_metrics=lambda: None,
        _qt_object_is_valid=isValid,
    )

    def metrics():
        return [
            MetricCard("Saldo", "10 €", "Contexto", "#009E73", "", variant="primary"),
            MetricCard("Detalhe", "5 €", "Contexto", "#009E73", "", variant="context"),
        ]

    page.metric_widgets = metrics()
    DashboardPage.layout_metric_cards(page)
    previous = page._secondary_metrics_host
    assert isValid(previous)
    DashboardPage.clear_layout(page, page.cards_grid)
    assert isValid(previous), "Reproduce the deferred-delete window before processing events"
    assert page._secondary_metrics_host is None
    assert page.secondary_metrics_grid is None
    page.metric_widgets = metrics()
    DashboardPage.layout_metric_cards(page)
    current = page._secondary_metrics_host
    assert current is not previous
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    assert not isValid(previous)
    assert isValid(current)
    assert all(isValid(card.status_label) for card in page.metric_widgets)
    root.close()
    root.deleteLater()
    QCoreApplication.sendPostedEvents(root, QEvent.Type.DeferredDelete)
