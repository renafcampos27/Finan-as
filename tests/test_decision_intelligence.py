from __future__ import annotations

from datetime import date

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from services.decision_intelligence import build_decision_briefing
from ui.components.decision_briefing import DecisionBriefingCard
from ui.core.theme import Theme
from utils.helpers import eur


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _healthy_context() -> dict:
    return {
        "totals": {"income": 2_500.0, "expenses": 1_800.0, "savings": 700.0},
        "monthly_summary": [
            {
                "income": 2_500.0,
                "expenses": 1_800.0,
                "savings": 700.0,
                "partial": False,
            }
            for _ in range(6)
        ],
        "emergency_reserve": {
            "available": True,
            "source": "goal",
            "current": 6_000.0,
            "target": 6_000.0,
            "missing": 0.0,
            "coverage_months": 4.0,
        },
        "start": "2026-07-01",
        "end": "2026-07-31",
        "period_label": "julho",
        "as_of": date(2026, 7, 27),
    }


def test_decision_briefing_ranks_critical_anomaly_before_other_risks() -> None:
    context = _healthy_context()
    context.update(
        {
            "totals": {
                "income": 2_000.0,
                "expenses": 2_250.0,
                "savings": -250.0,
            },
            "categories": [
                ("Casa", 1_100.0, "#0A84FF"),
                ("Alimentação", 650.0, "#30D158"),
            ],
            "cashflow_forecast": {
                "starting_balance": 300.0,
                "points": [{"balance": 300.0}, {"balance": -120.0}],
                "events": [{"name": "Renda"}],
                "summaries": {
                    "60": {
                        "days": 60,
                        "tone": "negative",
                        "minimum_balance": -120.0,
                        "risk_date": "2026-08-12",
                    }
                },
            },
            "anomalies": [
                {
                    "key": "duplicate:1",
                    "severity": "critical",
                    "kind": "duplicate",
                    "title": "Possível cobrança duplicada",
                    "amount": 85.0,
                    "date": "2026-07-24",
                    "category": "Casa",
                    "search": "Energia",
                    "action_page": "movimentos",
                }
            ],
        }
    )

    result = build_decision_briefing(**context)

    assert result["priorities"][0]["key"] == "anomaly:duplicate:1"
    assert result["priorities"][0]["rank"] == 1
    assert {row["key"] for row in result["priorities"]} >= {
        "anomaly:duplicate:1",
        "cashflow:negative",
        "margin:deficit",
    }
    assert result["urgent_count"] >= 3
    assert result["headline"] == "Possível cobrança duplicada"
    assert all(row["scope_label"] for row in result["priorities"])
    scopes = {row["key"]: row["scope_label"] for row in result["priorities"]}
    assert scopes["anomaly:duplicate:1"] == (
        "Anomalias · últimos 45 dias (base de 180 dias)"
    )
    assert scopes["cashflow:negative"] == "Previsão · próximos 60 dias"
    assert scopes["margin:deficit"] == "Filtro · julho"


def test_budget_priority_quantifies_the_excess_without_hiding_its_basis() -> None:
    context = _healthy_context()
    context["budget_forecast"] = {
        "days_elapsed": 18,
        "rows": [
            {
                "category": "Restaurantes",
                "spent": 520.0,
                "budget": 600.0,
                "projected": 900.0,
                "forecast_status": "Em risco",
                "projection_basis": "mês anterior",
            }
        ],
    }

    result = build_decision_briefing(**context)
    priority = next(
        row for row in result["priorities"] if row["key"] == "budget:Restaurantes"
    )

    assert priority["impact_amount"] == pytest.approx(300.0)
    assert priority["impact_label"] == "excesso previsto"
    assert priority["page"] == "orcamentos"
    assert priority["confidence"] == "high"
    assert [metric["value"] for metric in priority["metrics"]] == [
        520.0,
        900.0,
        600.0,
    ]
    assert "mês anterior" in priority["evidence"].casefold()
    assert priority["scope_label"] == "Orçamento · mês atual (07/2026)"


def test_reserve_priority_uses_the_real_recent_margin_for_its_timeline() -> None:
    context = _healthy_context()
    context["monthly_summary"] = [
        {
            "income": 2_000.0,
            "expenses": 1_700.0,
            "savings": 300.0,
            "partial": False,
        }
        for _ in range(6)
    ]
    context["emergency_reserve"] = {
        "available": True,
        "source": "goal",
        "current": 200.0,
        "target": 2_000.0,
        "missing": 1_800.0,
        "coverage_months": 0.4,
    }

    result = build_decision_briefing(**context)
    priority = next(row for row in result["priorities"] if row["key"] == "reserve:gap")

    assert "6 meses" in priority["message"]
    assert priority["impact_amount"] == pytest.approx(1_800.0)
    assert priority["action"] == "Abrir objetivo"
    assert priority["confidence"] == "high"


def test_decision_briefing_refuses_to_invent_advice_without_data() -> None:
    result = build_decision_briefing(
        totals={"income": 0.0, "expenses": 0.0, "savings": 0.0},
        as_of=date(2026, 7, 27),
    )

    assert result["priority_count"] == 1
    assert result["coverage_score"] == 0
    assert result["priorities"][0]["key"] == "data:start"
    assert "não vai inventar" in result["priorities"][0]["message"]


def test_invalid_data_is_the_first_action_and_is_declared_excluded() -> None:
    context = _healthy_context()
    context["data_quality"] = {"invalid_dates": 2, "invalid_amounts": 1}

    result = build_decision_briefing(**context)
    priority = result["priorities"][0]

    assert priority["key"] == "data_quality:invalid"
    assert priority["page"] == "movimentos"
    assert "não entram nos totais" in priority["message"]
    assert [metric["value"] for metric in priority["metrics"]] == [2, 1]


def test_decision_card_masks_every_structured_money_value_and_emits_action(
    qapp,
) -> None:
    theme = Theme(dark=False, privacy=True)
    previous_stylesheet = qapp.styleSheet()
    card = DecisionBriefingCard()
    data = {
        "as_of": "2026-07-27",
        "headline": "Conter Restaurantes antes do fecho",
        "priority_count": 1,
        "urgent_count": 0,
        "warning_count": 1,
        "coverage_score": 82,
        "coverage_level": "high",
        "coverage_label": "Confiança elevada",
        "method_label": "Cálculo local e explicável.",
        "scope_summary": "Filtro: julho. Orçamento: mês atual.",
        "priorities": [
            {
                "rank": 1,
                "key": "budget:Restaurantes",
                "icon": "🎯",
                "tone": "warning",
                "urgency": "Esta semana",
                "title": "Conter Restaurantes antes do fecho",
                "message": "A projeção ultrapassa o limite definido.",
                "action": "Abrir orçamento",
                "page": "orcamentos",
                "category": "Restaurantes",
                "search": "",
                "confidence": "high",
                "confidence_label": "Confiança elevada",
                "scope_label": "Orçamento · mês atual (07/2026)",
                "evidence": "Projeção baseada no mês anterior.",
                "impact_amount": 300.0,
                "impact_label": "excesso previsto",
                "metrics": [
                    {"label": "Gasto", "value": 520.0, "kind": "money"},
                    {"label": "Projeção", "value": 900.0, "kind": "money"},
                ],
            }
        ],
    }
    events: list[tuple[str, str, str]] = []
    card.action_requested.connect(
        lambda page, category, search: events.append((page, category, search))
    )

    try:
        qapp.setStyleSheet(theme.stylesheet())
        card.set_data(data, privacy=True)
        card.resize(720, card.sizeHint().height())
        card.show()
        qapp.processEvents()

        texts = " ".join(label.text() for label in card.findChildren(QLabel))
        assert eur(0.0, True) in texts
        assert "520,00" not in texts
        assert "900,00" not in texts
        assert "300,00" not in texts
        assert card.priority_rows[0].objectName() == "DecisionPriorityRow"
        assert card.priority_rows[0].scope_label.text() == (
            "🗓️  Orçamento · mês atual (07/2026)"
        )
        assert "cada prioridade" in card.scope_summary.text().casefold()
        assert "Filtro: julho" in card.method_label.text()
        assert not card.grab().isNull()

        card.priority_rows[0].action_button.click()
        assert events == [("orcamentos", "Restaurantes", "")]
    finally:
        card.close()
        card.deleteLater()
        qapp.setStyleSheet(previous_stylesheet)
        qapp.processEvents()
