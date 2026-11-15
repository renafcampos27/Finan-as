from __future__ import annotations

from ui.pages.subscriptions import _effort_color, _monthly_subscription_amount
from utils.helpers import BLUE, GREEN, ORANGE, PURPLE, RED, TEAL, YELLOW


def test_subscription_effort_color_thresholds() -> None:
    assert _effort_color(50) == RED
    assert _effort_color(35) == ORANGE
    assert _effort_color(25) == YELLOW
    assert _effort_color(15) == PURPLE
    assert _effort_color(10) == BLUE
    assert _effort_color(5) == TEAL
    assert _effort_color(1) == GREEN
    assert _effort_color(3) is None


def test_subscription_period_is_normalised_to_monthly_cost() -> None:
    assert _monthly_subscription_amount({"amount": 120, "period": "Anual"}) == 10
    assert _monthly_subscription_amount({"amount": 60, "period": "Semestral"}) == 10
    assert _monthly_subscription_amount({"amount": 30, "period": "Trimestral"}) == 10
    assert _monthly_subscription_amount({"amount": 10, "period": "Mensal"}) == 10
