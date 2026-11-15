"""Shared evidence and navigation helpers for chart detail panels."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from utils.helpers import eur, format_iso_date, safe_float, to_qdate

if TYPE_CHECKING:
    from database.repo import Repo
    from ui.core import Theme


def period_for_chart_item(
    item: dict[str, Any],
    *,
    fallback_start: str,
    fallback_end: str,
    clip_to_fallback: bool = True,
) -> tuple[str, str]:
    """Resolve a daily, weekly or monthly visual point to inclusive dates."""

    raw = str(item.get("date") or item.get("month") or "")[:10]
    resolution = str(item.get("resolution") or "").casefold()
    try:
        selected = date.fromisoformat(raw)
    except (TypeError, ValueError):
        if len(raw) != 7:
            return str(fallback_start), str(fallback_end)
        try:
            selected = date.fromisoformat(f"{raw}-01")
        except ValueError:
            return str(fallback_start), str(fallback_end)
        resolution = "monthly"

    if resolution == "monthly" or len(str(item.get("month") or "")) == 7:
        next_month = (
            date(selected.year + 1, 1, 1)
            if selected.month == 12
            else date(selected.year, selected.month + 1, 1)
        )
        start = selected.replace(day=1)
        end = next_month - timedelta(days=1)
    elif resolution == "weekly":
        start = selected
        end = selected + timedelta(days=6)
    else:
        start = selected
        end = selected

    if clip_to_fallback:
        try:
            clipped_start = max(start, date.fromisoformat(str(fallback_start)[:10]))
            clipped_end = min(end, date.fromisoformat(str(fallback_end)[:10]))
        except (TypeError, ValueError):
            clipped_start, clipped_end = start, end
        if clipped_start <= clipped_end:
            start, end = clipped_start, clipped_end
    return start.isoformat(), end.isoformat()


def movement_evidence_rows(
    repo: "Repo",
    theme: "Theme",
    start: str,
    end: str,
    *,
    category: str = "",
    kind: str = "",
    search_text: str = "",
    subcategory: str = "",
    account: str = "",
    payment_method: str = "",
    basis: str = "Pagamento",
    month_number: int | None = None,
    weekday: int | None = None,
    limit: int = 10,
) -> list[dict[str, str]]:
    rows = repo.chart_movement_details(
        start,
        end,
        category=category,
        kind=kind,
        search_text=search_text,
        subcategory=subcategory,
        account=account,
        payment_method=payment_method,
        basis=basis,
        month_number=month_number,
        weekday=weekday,
        limit=limit,
    )
    result: list[dict[str, str]] = []
    for row in rows:
        amount = safe_float(row["amount"])
        effective_category = str(row["effective_category"] or "Sem categoria")
        account = str(row["account"] or "")
        split_note = (
            " · parte de uma divisão" if bool(row["split_contribution"]) else ""
        )
        result.append(
            {
                "date": format_iso_date(str(row["financial_date"] or "")[:10]),
                "title": str(row["description"] or "Movimento"),
                "subtitle": (
                    effective_category
                    + (f" · {account}" if account else "")
                    + split_note
                ),
                "value": eur(amount, theme.privacy),
                "tone": "positive" if amount > 0 else "negative",
            }
        )
    return result


def movement_detail_payload(
    repo: "Repo",
    theme: "Theme",
    *,
    title: str,
    item: dict[str, Any],
    start: str,
    end: str,
    category: str = "",
    kind: str = "",
    search_text: str = "",
    subcategory: str = "",
    account: str = "",
    payment_method: str = "",
    basis: str = "Pagamento",
    month_number: int | None = None,
    weekday: int | None = None,
    subtitle: str = "",
    explanation: str = "",
    rows_title: str = "Maiores movimentos que explicam o ponto",
    limit: int = 10,
) -> dict[str, Any]:
    selected = str(
        subtitle
        or item.get("category")
        or item.get("label")
        or item.get("name")
        or item.get("date")
        or item.get("month")
        or ""
    ).strip()
    return {
        "icon": "🔎",
        "title": str(title or "Detalhes do gráfico"),
        "subtitle": selected or f"{format_iso_date(start)} a {format_iso_date(end)}",
        "explanation": explanation
        or (
            "Os movimentos usam a mesma data financeira e as mesmas divisões "
            "por categoria do gráfico, permitindo confirmar a origem do valor."
        ),
        "rows_title": rows_title,
        "rows": movement_evidence_rows(
            repo,
            theme,
            start,
            end,
            category=category,
            kind=kind,
            search_text=search_text,
            subcategory=subcategory,
            account=account,
            payment_method=payment_method,
            basis=basis,
            month_number=month_number,
            weekday=weekday,
            limit=limit,
        ),
        "show_empty_rows": True,
        "empty_rows_text": (
            "Não existem movimentos individuais para esta seleção ou o ponto "
            "representa apenas uma previsão."
        ),
    }


def open_movements_view(
    app: Any,
    *,
    start: str,
    end: str,
    kind: str = "Todos",
    category: str = "",
    search_text: str = "",
) -> None:
    """Open Movimentos with the exact evidence scope used by a chart."""

    page = app.pages.get("movimentos")
    if page is None:
        return
    period_blocked = page.period.blockSignals(True)
    try:
        custom_index = page.period.findText("Personalizado")
        if custom_index >= 0:
            page.period.setCurrentIndex(custom_index)
        start_day = datetime.strptime(str(start)[:10], "%Y-%m-%d").date()
        end_day = datetime.strptime(str(end)[:10], "%Y-%m-%d").date()
        page.start.setDate(to_qdate(start_day))
        page.end.setDate(to_qdate(end_day))
    finally:
        page.period.blockSignals(period_blocked)

    page.search.setText(str(search_text or ""))
    attachment_index = page.attach_filter.findText("Todos")
    if attachment_index >= 0:
        page.attach_filter.setCurrentIndex(attachment_index)
    selected_category = str(category or "").strip()
    category_index = (
        page.category.findText(selected_category) if selected_category else 0
    )
    page.category.setCurrentIndex(category_index if category_index >= 0 else 0)
    kind_name = kind if kind in {"Receitas", "Despesas"} else "Todos"
    kind_index = page.kind.findText(kind_name)
    page.kind.setCurrentIndex(kind_index if kind_index >= 0 else 0)
    app.goto("movimentos")
    page.refresh()


__all__ = [
    "movement_detail_payload",
    "movement_evidence_rows",
    "open_movements_view",
    "period_for_chart_item",
]
