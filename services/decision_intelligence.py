"""Explainable, local and actionable financial decision briefing.

The module deliberately returns structured facts instead of pre-formatted
currency strings.  The UI can therefore apply the privacy mask without trying
to redact prose after the calculations have already exposed a value.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from utils.helpers import safe_float


_CONFIDENCE_LABELS = {
    "high": "Confiança elevada",
    "medium": "Confiança média",
    "initial": "Confiança inicial",
}


def _value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _metric(label: str, value: Any, kind: str = "money") -> dict[str, Any]:
    return {"label": label, "value": value, "kind": kind}


def _category_map(rows: Iterable[Any]) -> dict[str, float]:
    grouped: dict[str, float] = {}
    for row in rows or ():
        if isinstance(row, Mapping):
            name = str(
                row.get("category") or row.get("name") or "Sem categoria"
            ).strip()
            amount = safe_float(row.get("amount", row.get("value", row.get("total"))))
        else:
            try:
                name = str(row[0] or "Sem categoria").strip()
                amount = safe_float(row[1])
            except (IndexError, TypeError):
                continue
        if name and amount > 0:
            grouped[name] = grouped.get(name, 0.0) + amount
    return grouped


def _confidence(level: str) -> tuple[str, str]:
    safe_level = level if level in _CONFIDENCE_LABELS else "initial"
    return safe_level, _CONFIDENCE_LABELS[safe_level]


def _priority(
    *,
    key: str,
    icon: str,
    tone: str,
    urgency: str,
    title: str,
    message: str,
    action: str,
    page: str,
    score: float,
    confidence: str,
    scope_label: str,
    metrics: list[dict[str, Any]] | None = None,
    evidence: str = "",
    impact_amount: float = 0.0,
    impact_label: str = "",
    category: str = "",
    search: str = "",
) -> dict[str, Any]:
    confidence_key, confidence_label = _confidence(confidence)
    return {
        "key": key,
        "icon": icon,
        "tone": tone
        if tone in {"positive", "warning", "negative", "neutral"}
        else "neutral",
        "urgency": urgency,
        "title": title,
        "message": message,
        "action": action,
        "page": page,
        "priority_score": safe_float(score),
        "confidence": confidence_key,
        "confidence_label": confidence_label,
        "scope_label": str(scope_label or "Período não identificado").strip(),
        "metrics": list(metrics or []),
        "evidence": evidence,
        "impact_amount": max(0.0, safe_float(impact_amount)),
        "impact_label": impact_label,
        "category": category,
        "search": search,
    }


def _period_days(start: str, end: str) -> int:
    try:
        start_day = date.fromisoformat(str(start)[:10])
        end_day = date.fromisoformat(str(end)[:10])
    except (TypeError, ValueError):
        return 0
    return max(0, (end_day - start_day).days + 1)


def _scope_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value or "")[:10]).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return ""


def _selected_scope(start: str, end: str, period_label: str) -> str:
    friendly = str(period_label or "").strip()
    if friendly:
        return f"Filtro · {friendly}"
    start_label = _scope_date(start)
    end_label = _scope_date(end)
    if start_label and end_label:
        return f"Filtro · {start_label} a {end_label}"
    return "Filtro · período selecionado"


def _recent_monthly_margin(rows: Iterable[Any]) -> tuple[float, int]:
    usable: list[float] = []
    for row in rows or ():
        income = safe_float(_value(row, "income"))
        expenses = safe_float(_value(row, "expenses"))
        if income <= 0 and expenses <= 0:
            continue
        if bool(_value(row, "partial", False)):
            continue
        saving = safe_float(_value(row, "savings"), income - expenses)
        usable.append(saving)
    if not usable:
        return 0.0, 0
    recent = usable[-6:]
    return sum(recent) / len(recent), len(recent)


def _cashflow_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    summaries = payload.get("summaries") or {}
    if not isinstance(summaries, Mapping) or not summaries:
        return {}
    numeric_keys = sorted(
        (
            (int(str(key)), value)
            for key, value in summaries.items()
            if str(key).isdigit() and isinstance(value, Mapping)
        ),
        key=lambda item: item[0],
    )
    if not numeric_keys:
        return {}
    return dict(
        next(
            (value for days, value in numeric_keys if days >= 60),
            numeric_keys[-1][1],
        )
    )


def _coverage(
    *,
    active_months: int,
    has_current: bool,
    has_comparison: bool,
    has_budget: bool,
    has_cashflow: bool,
) -> tuple[int, str, str]:
    score = round(
        min(6, max(0, active_months)) / 6.0 * 50.0
        + (20.0 if has_current else 0.0)
        + (10.0 if has_comparison else 0.0)
        + (10.0 if has_budget else 0.0)
        + (10.0 if has_cashflow else 0.0)
    )
    level = "high" if score >= 75 else "medium" if score >= 50 else "initial"
    return score, level, _CONFIDENCE_LABELS[level]


def build_decision_briefing(
    *,
    totals: Mapping[str, Any] | None,
    previous_totals: Mapping[str, Any] | None = None,
    categories: Iterable[Any] = (),
    previous_categories: Iterable[Any] = (),
    monthly_summary: Iterable[Any] = (),
    budget_forecast: Mapping[str, Any] | None = None,
    cashflow_forecast: Mapping[str, Any] | None = None,
    emergency_reserve: Mapping[str, Any] | None = None,
    subscription_total: float = 0.0,
    subscription_suspects: Iterable[Any] = (),
    anomalies: Iterable[Any] = (),
    work_value: Mapping[str, Any] | None = None,
    data_quality: Mapping[str, Any] | None = None,
    foreign_tax_summary: Mapping[str, Any] | None = None,
    foreign_dividends: Iterable[Any] = (),
    start: str = "",
    end: str = "",
    period_label: str = "",
    comparison_label: str = "período anterior",
    as_of: date | None = None,
    limit: int = 4,
) -> dict[str, Any]:
    """Build a ranked briefing without external AI or invented transactions."""

    current = dict(totals or {})
    previous = dict(previous_totals or {})
    budget = dict(budget_forecast or {})
    cashflow = dict(cashflow_forecast or {})
    reserve = dict(emergency_reserve or {})
    work = dict(work_value or {})
    quality = dict(data_quality or {})
    current_categories = _category_map(categories)
    reference_categories = _category_map(previous_categories)
    history_rows = list(monthly_summary or ())
    suspects = list(subscription_suspects or ())
    anomaly_rows = list(anomalies or ())
    reference_day = as_of or date.today()
    period_length = _period_days(start, end)
    selected_scope = _selected_scope(start, end, period_label)

    income = max(0.0, safe_float(current.get("income")))
    expenses = max(0.0, safe_float(current.get("expenses")))
    savings = safe_float(current.get("savings"), income - expenses)
    savings_rate = savings / income * 100.0 if income > 0 else 0.0
    monthly_margin, active_months = _recent_monthly_margin(history_rows)
    history_scope = (
        f"Histórico · últimos {active_months} meses completos"
        if active_months
        else "Histórico · ainda sem meses completos"
    )
    budget_scope = f"Orçamento · mês atual ({reference_day.strftime('%m/%Y')})"
    reserve_expense_months = max(
        0,
        int(safe_float(reserve.get("expense_history_months"))),
    )
    reserve_lookup_months = max(
        reserve_expense_months,
        int(safe_float(reserve.get("expense_lookup_months"))),
    )
    reserve_expense_scope = (
        f"{reserve_expense_months} meses com despesas"
        if reserve_expense_months
        else f"janela de até {reserve_lookup_months or 13} meses, ainda sem despesas úteis"
    )
    income_context_months = max(
        0,
        int(safe_float(work.get("income_context_months"))),
    )
    monthly_income = max(0.0, safe_float(work.get("monthly_income")))
    if monthly_income <= 0:
        historical_incomes = [
            safe_float(_value(row, "income"))
            for row in history_rows
            if safe_float(_value(row, "income")) > 0
            and not bool(_value(row, "partial", False))
        ]
        if historical_incomes:
            monthly_income = sum(historical_incomes[-6:]) / len(historical_incomes[-6:])

    budget_rows = [
        dict(row) for row in budget.get("rows", []) if isinstance(row, Mapping)
    ]
    cash_summary = _cashflow_summary(cashflow)
    has_current = income > 0 or expenses > 0 or bool(current_categories)
    has_comparison = (
        safe_float(previous.get("income")) > 0
        or safe_float(previous.get("expenses")) > 0
        or bool(reference_categories)
    )
    has_budget = bool(budget_rows)
    has_cashflow = bool(cashflow.get("points")) and (
        abs(safe_float(cashflow.get("starting_balance"))) > 0.01
        or bool(cashflow.get("events"))
    )
    has_data = any(
        (
            has_current,
            active_months > 0,
            max(0.0, safe_float(subscription_total)) > 0,
            has_budget,
            max(0.0, safe_float(reserve.get("current"))) > 0,
            bool(anomaly_rows),
        )
    )

    candidates: list[dict[str, Any]] = []

    invalid_dates = max(0, int(safe_float(quality.get("invalid_dates"))))
    invalid_amounts = max(0, int(safe_float(quality.get("invalid_amounts"))))
    invalid_total = invalid_dates + invalid_amounts
    if invalid_total:
        candidates.append(
            _priority(
                key="data_quality:invalid",
                icon="🧹",
                tone="negative",
                urgency="Corrigir dados",
                title="Rever dados inválidos excluídos dos cálculos",
                message=(
                    "Estes registos continuam visíveis para correção, mas não entram "
                    "nos totais, previsões ou scores financeiros."
                ),
                action="Abrir movimentos",
                page="movimentos",
                score=1_000,
                confidence="high",
                scope_label=selected_scope,
                metrics=[
                    _metric("Datas inválidas", invalid_dates, "count"),
                    _metric("Valores inválidos", invalid_amounts, "count"),
                ],
                evidence="Validação estrutural local da base de dados.",
            )
        )

    if anomaly_rows:
        alert = anomaly_rows[0]
        severity = str(_value(alert, "severity") or "warning")
        kind = str(_value(alert, "kind") or "anomaly")
        amount = max(0.0, safe_float(_value(alert, "amount")))
        category = str(_value(alert, "category") or "Sem categoria")
        messages = {
            "duplicate": "Há movimentos iguais que podem representar uma cobrança repetida.",
            "spike": "Um movimento ficou muito acima do padrão recente da categoria.",
            "increase": "Um custo recorrente aumentou face ao registo anterior.",
        }
        candidates.append(
            _priority(
                key=f"anomaly:{_value(alert, 'key', kind)}",
                icon="🔎",
                tone="negative" if severity == "critical" else "warning",
                urgency="Confirmar agora" if severity == "critical" else "Confirmar",
                title=str(_value(alert, "title") or "Movimento fora do padrão"),
                message=messages.get(
                    kind,
                    "Foi encontrado um movimento que merece confirmação manual.",
                ),
                action="Rever movimento",
                page=str(_value(alert, "action_page") or "movimentos"),
                score=1_200 if severity == "critical" else 980,
                confidence="medium",
                scope_label="Anomalias · últimos 45 dias (base de 180 dias)",
                metrics=[
                    _metric("Valor", amount),
                    _metric("Data", str(_value(alert, "date")), "date"),
                    _metric("Categoria", category, "text"),
                ],
                evidence="Regra local baseada no histórico; confirma antes de corrigir.",
                impact_amount=amount,
                impact_label="valor a confirmar",
                category=category,
                search=str(_value(alert, "search") or ""),
            )
        )

    if cash_summary:
        cash_tone = str(cash_summary.get("tone") or "positive")
        minimum_balance = safe_float(cash_summary.get("minimum_balance"))
        starting_balance = safe_float(cashflow.get("starting_balance"))
        horizon = int(safe_float(cash_summary.get("days"), 60))
        risk_date = str(
            cash_summary.get("risk_date") or cash_summary.get("minimum_date") or ""
        )
        if cash_tone == "negative":
            candidates.append(
                _priority(
                    key="cashflow:negative",
                    icon="🚦",
                    tone="negative",
                    urgency="Agir agora",
                    title="Evitar saldo negativo previsto",
                    message=(
                        "O cenário base atravessa zero dentro do horizonte de "
                        "tesouraria."
                    ),
                    action="Abrir tesouraria",
                    page="centro",
                    score=1_100 + min(99.0, abs(minimum_balance) / 10.0),
                    confidence="medium",
                    scope_label=f"Previsão · próximos {horizon} dias",
                    metrics=[
                        _metric("Saldo atual", starting_balance),
                        _metric("Mínimo previsto", minimum_balance),
                        _metric("Data de risco", risk_date, "date"),
                    ],
                    evidence=(
                        f"Previsão de {horizon} dias com eventos conhecidos e "
                        "ritmo variável recente."
                    ),
                    impact_amount=max(0.0, -minimum_balance),
                    impact_label="falta para não ficar negativo",
                )
            )
        elif cash_tone == "warning":
            candidates.append(
                _priority(
                    key="cashflow:warning",
                    icon="🛟",
                    tone="warning",
                    urgency="Esta semana",
                    title="Proteger a margem de tesouraria",
                    message=(
                        "O saldo permanece positivo, mas a margem de segurança "
                        "fica reduzida."
                    ),
                    action="Ver próximos eventos",
                    page="centro",
                    score=790,
                    confidence="medium",
                    scope_label=f"Previsão · próximos {horizon} dias",
                    metrics=[
                        _metric("Saldo atual", starting_balance),
                        _metric("Mínimo previsto", minimum_balance),
                        _metric("Horizonte", horizon, "days"),
                    ],
                    evidence="Cenário base; a área de tesouraria mostra também as faixas.",
                )
            )

    if income > 0 or expenses > 0:
        if savings < -0.01:
            top_category = max(
                current_categories.items(),
                key=lambda item: item[1],
                default=("", 0.0),
            )
            candidates.append(
                _priority(
                    key="margin:deficit",
                    icon="🧯",
                    tone="negative",
                    urgency="Agir agora",
                    title="Fechar o défice do período",
                    message=(
                        "As saídas superaram as entradas; começa pela maior "
                        "categoria e confirma o que pode ser adiado ou reduzido."
                    ),
                    action="Rever maiores despesas",
                    page="movimentos",
                    score=1_020 + min(79.0, abs(savings) / 10.0),
                    confidence="high" if period_length >= 28 else "medium",
                    scope_label=selected_scope,
                    metrics=[
                        _metric("Receitas", income),
                        _metric("Despesas", expenses),
                        _metric("Diferença", savings),
                    ],
                    evidence=(
                        f"Valores registados em {period_label or 'no período selecionado'}."
                    ),
                    impact_amount=abs(savings),
                    impact_label="défice a recuperar",
                    category=top_category[0],
                )
            )
        elif savings_rate < 10.0:
            target_margin = income * 0.10
            missing_margin = max(0.0, target_margin - savings)
            candidates.append(
                _priority(
                    key="margin:low",
                    icon="🌱",
                    tone="warning",
                    urgency="Próximo passo",
                    title="Criar uma margem mínima",
                    message=(
                        "A poupança ficou abaixo da referência simples de 10% "
                        "das receitas."
                    ),
                    action="Abrir orçamentos",
                    page="orcamentos",
                    score=760 + min(49.0, missing_margin / 10.0),
                    confidence="high" if period_length >= 28 else "medium",
                    scope_label=selected_scope,
                    metrics=[
                        _metric("Taxa atual", savings_rate, "percent"),
                        _metric("Poupança", savings),
                        _metric("Referência", target_margin),
                    ],
                    evidence="A referência de 10% é um ponto de partida, não uma regra pessoal.",
                    impact_amount=missing_margin,
                    impact_label="margem até 10%",
                )
            )

    risky_budget_rows = [
        row
        for row in budget_rows
        if str(row.get("forecast_status")) in {"Ultrapassado", "Em risco", "Atenção"}
        and safe_float(row.get("budget")) > 0
    ]
    if risky_budget_rows:
        exceeded_rows = [
            row
            for row in risky_budget_rows
            if safe_float(row.get("projected")) > safe_float(row.get("budget"))
        ]
        if not exceeded_rows:
            # BUG-DEC-05: when no category actually exceeds its limit, never
            # pick an arbitrary row as the "worst"; report that none exceeded
            # and surface the highest utilisation as neutral context.
            context_row = max(
                risky_budget_rows,
                key=lambda row: (
                    safe_float(row.get("projected"))
                    / safe_float(row.get("budget"))
                    if safe_float(row.get("budget")) > 0
                    else 0.0
                ),
            )
            context_category = str(
                context_row.get("category") or "Sem categoria"
            )
            context_budget = max(0.0, safe_float(context_row.get("budget")))
            context_projected = max(0.0, safe_float(context_row.get("projected")))
            utilisation = (
                context_projected / context_budget * 100.0
                if context_budget > 0
                else 0.0
            )
            candidates.append(
                _priority(
                    key="budget:none_exceeded",
                    icon="🛡️",
                    tone="neutral",
                    urgency="Vigiar",
                    title="Nenhum orçamento ultrapassado",
                    message=(
                        "Nenhuma categoria ultrapassa o limite neste mês; "
                        f"{context_category} é a mais próxima, com cerca de "
                        f"{utilisation:.0f}% do limite consumido na projeção."
                    ),
                    action="Abrir orçamentos",
                    page="orcamentos",
                    score=520,
                    confidence=(
                        "high"
                        if int(safe_float(budget.get("days_elapsed"))) >= 10
                        else "medium"
                    ),
                    scope_label=budget_scope,
                    metrics=[
                        _metric("Gasto", max(0.0, safe_float(context_row.get("spent")))),
                        _metric("Projeção", context_projected),
                        _metric("Limite", context_budget),
                    ],
                    evidence=(
                        "Projeção do ritmo mensal; nenhum limite foi excedido."
                    ),
                    impact_amount=0.0,
                    impact_label="",
                    category=context_category,
                )
            )
        else:
            budget_row = max(
                exceeded_rows,
                key=lambda row: (
                    safe_float(row.get("projected")) - safe_float(row.get("budget")),
                    safe_float(row.get("projected"))
                    / safe_float(row.get("budget"))
                    if safe_float(row.get("budget")) > 0
                    else 0.0,
                ),
            )
        if exceeded_rows:
            status = str(budget_row.get("forecast_status") or "Atenção")
            category = str(budget_row.get("category") or "Sem categoria")
            spent = max(0.0, safe_float(budget_row.get("spent")))
            projected = max(spent, safe_float(budget_row.get("projected")))
            limit_amount = max(0.0, safe_float(budget_row.get("budget")))
            excess = max(0.0, projected - limit_amount)
            candidates.append(
                _priority(
                    key=f"budget:{category}",
                    icon="🎯",
                    tone="negative" if status == "Ultrapassado" else "warning",
                    urgency=(
                        "Agir agora" if status == "Ultrapassado" else "Esta semana"
                    ),
                    title=f"Conter {category} antes do fecho",
                    message=(
                        "A projeção mensal ultrapassa o limite definido."
                        if excess > 0
                        else "O ritmo atual está demasiado próximo do limite definido."
                    ),
                    action="Abrir orçamento",
                    page="orcamentos",
                    score=930 if status == "Ultrapassado" else 840,
                    confidence=(
                        "high"
                        if int(safe_float(budget.get("days_elapsed"))) >= 10
                        else "medium"
                    ),
                    scope_label=budget_scope,
                    metrics=[
                        _metric("Gasto", spent),
                        _metric("Projeção", projected),
                        _metric("Limite", limit_amount),
                    ],
                    evidence=str(
                        budget_row.get("projection_basis")
                        or budget.get("method_label")
                        or "Projeção do ritmo mensal."
                    ).capitalize(),
                    impact_amount=excess,
                    impact_label="excesso previsto" if excess > 0 else "",
                    category=category,
                )
            )
    else:
        unbudgeted = max(0.0, safe_float(budget.get("unbudgeted_spend")))
        total_spent = max(0.0, safe_float(budget.get("total_spent")))
        coverage = safe_float(budget.get("coverage_pct"), 100.0)
        if (
            total_spent > 0
            and unbudgeted >= max(50.0, total_spent * 0.20)
            and coverage < 80.0
        ):
            candidates.append(
                _priority(
                    key="budget:coverage",
                    icon="🧩",
                    tone="warning",
                    urgency="Organizar",
                    title="Dar limite às despesas sem orçamento",
                    message=(
                        "Uma parte relevante dos gastos ainda não tem um limite "
                        "mensal associado."
                    ),
                    action="Definir orçamentos",
                    page="orcamentos",
                    score=640,
                    confidence="high",
                    scope_label=budget_scope,
                    metrics=[
                        _metric("Sem limite", unbudgeted),
                        _metric("Cobertura", coverage, "percent"),
                        _metric("Gasto total", total_spent),
                    ],
                    evidence="Cobertura calculada apenas sobre despesas já registadas.",
                    impact_amount=unbudgeted,
                    impact_label="gasto ainda sem limite",
                )
            )

    if bool(reserve.get("available")):
        missing = max(0.0, safe_float(reserve.get("missing")))
        coverage_months = max(0.0, safe_float(reserve.get("coverage_months")))
        if missing > 0.01 and coverage_months < 3.0:
            monthly_pace = min(missing, max(0.0, monthly_margin))
            # BUG-DEC-03: a near-zero pace must not project astronomical months;
            # require a viable pace and cap the horizon at 10 years.
            MIN_VIABLE_PACE = 5.0
            MAX_RESERVE_HORIZON_MONTHS = 120
            pace_viable = monthly_pace >= MIN_VIABLE_PACE
            target_months = (
                math.ceil(missing / monthly_pace) if pace_viable else 0
            )
            horizon_capped = target_months > MAX_RESERVE_HORIZON_MONTHS
            if horizon_capped:
                target_months = MAX_RESERVE_HORIZON_MONTHS
            reserve_message = (
                f"Ao manter a margem média recente, a referência seria atingida "
                f"em cerca de {target_months} meses."
                if target_months
                else (
                    "A margem média recente ainda não permite calendarizar o "
                    "reforço; primeiro é preciso libertar saldo mensal."
                )
            )
            if horizon_capped:
                reserve_message = (
                    "Ao ritmo atual, a referência levaria mais de 10 anos a "
                    "atingir; aumenta a margem mensal para acelerar o reforço."
                )
            candidates.append(
                _priority(
                    key="reserve:gap",
                    icon="🛡️",
                    tone="negative" if coverage_months < 1.0 else "warning",
                    urgency="Prioridade alta" if coverage_months < 1.0 else "Construir",
                    title="Reforçar a reserva de emergência",
                    message=reserve_message,
                    action=(
                        "Criar margem primeiro"
                        if monthly_pace <= 0
                        else "Abrir objetivo"
                    ),
                    page="orcamentos" if monthly_pace <= 0 else "objetivos",
                    score=870 if coverage_months < 1.0 else 730,
                    confidence="high" if reserve.get("source") == "goal" else "medium",
                    scope_label=(
                        f"Reserva atual · despesas: {reserve_expense_scope}; "
                        f"ritmo: {active_months} meses completos"
                    ),
                    metrics=[
                        _metric("Atual", safe_float(reserve.get("current"))),
                        _metric("Objetivo", safe_float(reserve.get("target"))),
                        _metric("Cobertura", coverage_months, "months"),
                    ],
                    evidence=(
                        "Objetivo definido pelo utilizador."
                        if reserve.get("source") == "goal"
                        else "Referência automática baseada nas despesas médias."
                    ),
                    impact_amount=missing,
                    impact_label="falta na reserva",
                )
            )

    # BUG-DEC-02: _category_map drops zero-amount rows, so a category that
    # was 0 € in the reference period disappears from reference_categories.
    # Fall back to the raw previous_categories so the comparison still runs.
    if current_categories and (reference_categories or previous_categories):
        increases = []
        new_expenses = []
        for category, amount in current_categories.items():
            old_amount = max(0.0, safe_float(reference_categories.get(category)))
            if old_amount <= 0:
                # BUG-DEC-02: a category that did not exist in the reference
                # period is still a relevant new expense when its absolute
                # amount is material for the month.
                if amount >= max(10.0, expenses * 0.05):
                    new_expenses.append((amount, category))
                continue
            delta = amount - old_amount
            change_pct = delta / old_amount * 100.0
            if delta >= max(10.0, old_amount * 0.15) and amount >= max(
                10.0, expenses * 0.05
            ):
                increases.append((delta, change_pct, category, amount, old_amount))
        if new_expenses:
            amount, category = max(new_expenses)
            candidates.append(
                _priority(
                    key=f"comparison:new:{category}",
                    icon="📈",
                    tone="warning",
                    urgency="Confirmar",
                    title=f"Confirmar despesa nova em {category}",
                    message=(
                        f"Não havia gasto registado em {category} no "
                        f"{comparison_label or 'período de referência'} e agora "
                        "pesa no orçamento do mês."
                    ),
                    action="Ver movimentos",
                    page="movimentos",
                    score=660 + min(59.0, amount / max(1.0, expenses) * 100.0),
                    confidence="high" if period_length >= 28 else "medium",
                    scope_label=(
                        f"Comparação · {selected_scope.removeprefix('Filtro · ')} vs. "
                        f"{comparison_label or 'período de referência'}"
                    ),
                    metrics=[
                        _metric("Agora", amount),
                        _metric("Referência", 0.0),
                        _metric(
                            "Peso nas despesas",
                            amount / expenses * 100.0 if expenses > 0 else 0.0,
                            "percent",
                        ),
                    ],
                    evidence="Comparações entre períodos com a mesma duração.",
                    impact_amount=amount,
                    impact_label="despesa nova no período",
                    category=category,
                )
            )
        if increases:
            delta, change_pct, category, amount, old_amount = max(increases)
            candidates.append(
                _priority(
                    key=f"comparison:{category}",
                    icon="📈",
                    tone="warning",
                    urgency="Confirmar",
                    title=f"Confirmar aumento em {category}",
                    message=(
                        f"Foi a maior subida relevante face ao "
                        f"{comparison_label or 'período de referência'}."
                    ),
                    action="Ver movimentos",
                    page="movimentos",
                    score=670 + min(59.0, change_pct / 5.0),
                    confidence="high" if period_length >= 28 else "medium",
                    scope_label=(
                        f"Comparação · {selected_scope.removeprefix('Filtro · ')} vs. "
                        f"{comparison_label or 'período de referência'}"
                    ),
                    metrics=[
                        _metric("Agora", amount),
                        _metric("Referência", old_amount),
                        _metric("Variação", change_pct, "percent"),
                    ],
                    evidence="Comparação entre períodos com a mesma duração.",
                    impact_amount=delta,
                    impact_label="aumento no período",
                    category=category,
                )
            )

    monthly_subscriptions = max(0.0, safe_float(subscription_total))
    if suspects:
        suspect_amount = sum(
            max(0.0, safe_float(_value(row, "amount"))) for row in suspects
        )
        suspect_names = [
            str(_value(row, "name") or "").strip()
            for row in suspects[:2]
            if str(_value(row, "name") or "").strip()
        ]
        candidates.append(
            _priority(
                key="subscriptions:suspects",
                icon="🕵️",
                tone="warning",
                urgency="Confirmar",
                title="Validar cobranças recorrentes detetadas",
                message=(
                    "Confirma se estes serviços continuam ativos: "
                    + ", ".join(suspect_names)
                    if suspect_names
                    else "Foram encontrados padrões recorrentes ainda por confirmar."
                ),
                action="Rever subscrições",
                page="subscricoes",
                score=650,
                confidence="medium",
                scope_label="Recorrência · últimos 90 dias",
                metrics=[
                    _metric("Sob revisão", suspect_amount),
                    _metric("Impacto anual", suspect_amount * 12.0),
                    _metric("Sinais", len(suspects), "count"),
                ],
                evidence="Deteção por repetição mensal; não assume que o serviço é inútil.",
                impact_amount=suspect_amount,
                impact_label="custo mensal sob revisão",
            )
        )
    elif monthly_subscriptions > 0:
        subscription_ratio = (
            monthly_subscriptions / monthly_income * 100.0
            if monthly_income > 0
            else 0.0
        )
        material = (
            subscription_ratio >= 5.0
            if monthly_income > 0
            else monthly_subscriptions >= 100.0
        )
        if material:
            candidates.append(
                _priority(
                    key="subscriptions:weight",
                    icon="🧾",
                    tone="warning",
                    urgency="Rever",
                    title="Rever o peso das subscrições",
                    message=(
                        "O custo recorrente já representa uma parte relevante "
                        "do rendimento mensal."
                    ),
                    action="Abrir subscrições",
                    page="subscricoes",
                    score=570,
                    confidence="high",
                    scope_label=(
                        "Subscrições ativas · valor mensal atual"
                        + (
                            f" vs. rendimento médio de {income_context_months} meses"
                            if income_context_months
                            else ""
                        )
                    ),
                    metrics=[
                        _metric("Mensal", monthly_subscriptions),
                        _metric("Anual", monthly_subscriptions * 12.0),
                        _metric("Do rendimento", subscription_ratio, "percent"),
                    ],
                    evidence="Total das subscrições explicitamente marcadas como ativas.",
                    impact_amount=monthly_subscriptions,
                    impact_label="custo mensal recorrente",
                )
            )

    # AUD-TAX-01: Foreign dividend tax credit & uncreditable leakage alert (Art. 81.º CIRS / W-8BEN)
    tax_summary_dict = dict(foreign_tax_summary or {})
    uncreditable_loss = safe_float(
        tax_summary_dict.get(
            "total_uncreditable_loss",
            tax_summary_dict.get("total_uncreditable_foreign_tax_loss", 0.0),
        )
    )
    if not uncreditable_loss and foreign_dividends:
        from services.tax import summarize_foreign_dividends

        try:
            div_summary = summarize_foreign_dividends(list(foreign_dividends))
            uncreditable_loss = safe_float(
                div_summary.get("total_uncreditable_loss", 0.0)
            )
        except Exception:
            uncreditable_loss = 0.0

    if uncreditable_loss > 0:
        has_data = True
        candidates.append(
            _priority(
                key="tax:foreign_dividend_leak",
                icon="💸",
                tone="warning",
                urgency="Otimizar",
                title="Evitar perda fiscal em dividendos estrangeiros",
                message=(
                    f"Foram detetados {uncreditable_loss:.2f} € de retenção na fonte no estrangeiro acima "
                    "do teto da Convenção (CDT / Art. 81.º CIRS). Verifica a submissão do formulário "
                    "W-8BEN (EUA a 15%) ou equivalente na tua corretora para eliminar esta perda definitiva."
                ),
                action="Ver fiscalidade",
                page="investimentos",
                score=610,
                confidence="high",
                scope_label="Fiscalidade internacional · Categoria E / Anexo J",
                metrics=[
                    _metric("Perda fiscal não dedutível", uncreditable_loss),
                ],
                evidence="Retenção no estrangeiro acima do crédito admitido pela AT no Anexo J Quadro 8.",
                impact_amount=uncreditable_loss,
                impact_label="imposto retido a mais",
            )
        )

    if not has_data:
        candidates = [
            _priority(
                key="data:start",
                icon="🧭",
                tone="neutral",
                urgency="Começar",
                title="Registar a base financeira",
                message=(
                    "Ainda não existem dados suficientes; a app não vai inventar "
                    "recomendações."
                ),
                action="Adicionar movimento",
                page="movimentos",
                score=100,
                confidence="initial",
                scope_label=f"Dados disponíveis · até {reference_day.strftime('%d/%m/%Y')}",
                metrics=[_metric("Meses úteis", active_months, "count")],
                evidence="Adiciona receitas e despesas para desbloquear prioridades reais.",
            )
        ]
    elif not candidates:
        if monthly_margin > 0:
            candidates.append(
                _priority(
                    key="healthy:allocate",
                    icon="✨",
                    tone="positive",
                    urgency="Manter",
                    title="Dar destino à margem que já crias",
                    message=(
                        "A média recente é positiva; associá-la a um objetivo "
                        "evita que a folga se dilua."
                    ),
                    action="Abrir objetivos",
                    page="objetivos",
                    score=100,
                    confidence="high" if active_months >= 4 else "medium",
                    scope_label=history_scope,
                    metrics=[
                        _metric("Margem média", monthly_margin),
                        _metric("Potencial anual", monthly_margin * 12.0),
                        _metric("Meses analisados", active_months, "count"),
                    ],
                    evidence="Média dos últimos meses completos com movimentos.",
                    impact_amount=monthly_margin,
                    impact_label="margem média mensal",
                )
            )
        elif monthly_margin < 0:
            # BUG-DEC-01: a negative structural margin is never a green card.
            candidates.append(
                _priority(
                    key="margin:structural",
                    icon="⚠️",
                    tone="negative",
                    urgency="Agir agora",
                    title="Margem estrutural deficitária",
                    message=(
                        "A média dos últimos meses completos fecha em negativo; "
                        "sem cortes ou rendimento extra, o desequilíbrio "
                        "continua a repetir-se todos os meses."
                    ),
                    action="Rever orçamentos",
                    page="orcamentos",
                    score=980 + min(69.0, abs(monthly_margin) / 10.0),
                    confidence="high" if active_months >= 3 else "medium",
                    scope_label=history_scope,
                    metrics=[
                        _metric("Margem média", monthly_margin),
                        _metric("Meses analisados", active_months, "count"),
                    ],
                    evidence="Média dos últimos meses completos com movimentos.",
                    impact_amount=abs(monthly_margin),
                    impact_label="défice médio mensal",
                )
            )
        else:
            candidates.append(
                _priority(
                    key="healthy:maintain",
                    icon="✅",
                    tone="positive",
                    urgency="Manter",
                    title="Estrutura sem riscos imediatos detetados",
                    message=(
                        "Não foi encontrado um problema prioritário com os dados "
                        "disponíveis."
                    ),
                    action="Ver saúde financeira",
                    page="saude",
                    score=100,
                    confidence="medium",
                    scope_label="Síntese · várias janelas identificadas abaixo",
                    metrics=[_metric("Meses analisados", active_months, "count")],
                    evidence="Continua a registar movimentos para manter a leitura atualizada.",
                )
            )

    ordered = sorted(
        candidates,
        key=lambda row: (
            -safe_float(row.get("priority_score")),
            -safe_float(row.get("impact_amount")),
            str(row.get("title") or "").casefold(),
        ),
    )[: max(1, min(6, int(limit or 4)))]
    for rank, row in enumerate(ordered, start=1):
        row["rank"] = rank

    coverage_score, coverage_level, coverage_label = _coverage(
        active_months=active_months,
        has_current=has_current,
        has_comparison=has_comparison,
        has_budget=has_budget,
        has_cashflow=has_cashflow,
    )
    largest = max(
        ordered,
        key=lambda row: safe_float(row.get("impact_amount")),
        default={},
    )
    urgent_count = sum(1 for row in ordered if row.get("tone") == "negative")
    warning_count = sum(1 for row in ordered if row.get("tone") == "warning")
    return {
        "as_of": reference_day.isoformat(),
        "period_label": period_label,
        "headline": str(ordered[0].get("title") or "") if ordered else "",
        "priorities": ordered,
        "priority_count": len(ordered),
        "urgent_count": urgent_count,
        "warning_count": warning_count,
        "largest_impact_amount": safe_float(largest.get("impact_amount")),
        "largest_impact_label": str(largest.get("impact_label") or ""),
        "coverage_score": coverage_score,
        "coverage_level": coverage_level,
        "coverage_label": coverage_label,
        "active_months": active_months,
        "scope_summary": (
            f"{selected_scope}. Margem recente: até 6 meses completos dentro de "
            f"uma consulta de 8 meses. Reserva: {reserve_expense_scope} (até "
            f"{reserve_lookup_months or 13} consultados). Orçamento: mês atual "
            f"({reference_day.strftime('%m/%Y')}). "
            "Anomalias: últimos 45 dias, comparadas com uma base de 180 dias. "
            "Tesouraria: previsão de 90 dias; as prioridades usam a janela de "
            "60 dias. Subscrições detetadas: últimos 90 dias."
        ),
        "method_label": (
            "Ranking local por urgência, impacto monetário e solidez da evidência. "
            "Cada prioridade mostra o período que realmente utiliza. A cobertura "
            "combina várias fontes e não é uma probabilidade estatística."
        ),
    }


__all__ = ["build_decision_briefing"]
