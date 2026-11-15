"""Read-only product follow-up and reproducible explanations of existing totals."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any, Iterable

from database.validation import (
    sql_external_financial_flow,
    sql_finite_number,
    sql_iso_date,
)
from utils.helpers import add_months


_FINANCIAL_DATE = "COALESCE(NULLIF(m.payment_date,''),t.date)"


def _day(value: date | str | None) -> date:
    if value is None:
        return date.today()
    raw = value.isoformat() if isinstance(value, date) else str(value)
    parsed = date.fromisoformat(raw)
    if parsed.isoformat() != raw:
        raise ValueError("Indica uma data no formato AAAA-MM-DD.")
    return parsed


class ProductReviewService:
    def __init__(self, repo: Any) -> None:
        self.repo = repo

    def period_context(
        self, start: str, end: str, *, as_of: date | str | None = None
    ) -> dict[str, Any]:
        first, last, reference = _day(start), _day(end), _day(as_of)
        if first > last:
            raise ValueError("O início do período não pode ser posterior ao fim.")
        row = self.repo.q(
            f"""
            SELECT COUNT(*) AS recorded,
                   COALESCE(SUM(CASE WHEN {_FINANCIAL_DATE}>? THEN 1 ELSE 0 END),0) AS future,
                   MIN({_FINANCIAL_DATE}) AS first_record,
                   MAX({_FINANCIAL_DATE}) AS last_record
            FROM transactions t LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE {_FINANCIAL_DATE}>=? AND {_FINANCIAL_DATE}<=?
              AND {sql_finite_number("t.amount", non_zero=True)}
              AND {sql_iso_date(_FINANCIAL_DATE)}
            """,
            (reference.isoformat(), first.isoformat(), last.isoformat()),
        )[0]
        state = (
            "closed"
            if last < reference
            else "future"
            if first > reference
            else "in_progress"
        )
        return {
            "start": first.isoformat(),
            "end": last.isoformat(),
            "as_of": reference.isoformat(),
            "calendar_state": state,
            "recorded": int(row["recorded"]),
            "future_records": int(row["future"]),
            "first_record": row["first_record"],
            "last_record": row["last_record"],
            "financial_date_basis": _FINANCIAL_DATE,
            "formula": "Receitas externas positivas − despesas externas negativas em valor absoluto.",
        }

    def period_summary_text(
        self,
        start: str,
        end: str,
        *,
        as_of: date | str | None = None,
        privacy: bool = False,
    ) -> str:
        context = self.period_context(start, end, as_of=as_of)
        labels = {
            "closed": "Calendário encerrado",
            "future": "Período futuro",
            "in_progress": "Período em curso",
        }
        count = "•••" if privacy else str(context["recorded"])
        future = "•••" if privacy else str(context["future_records"])
        return (
            f"Âmbito e origem\n{labels[context['calendar_state']]} · {start} a {end} · "
            f"consulta em {context['as_of']}.\n"
            "A data financeira é a data de pagamento; na ausência, a data do movimento.\n"
            "Receitas: movimentos externos positivos, incluindo reembolsos recebidos. "
            "Despesas: valor absoluto dos movimentos externos negativos. "
            "Poupança = receitas − despesas. Transferências internas ficam excluídas.\n"
            f"Registos válidos no período: {count}; com data posterior à consulta: {future}. "
            "Os totais incluem os registos já existentes no período selecionado, sem projeção.\n"
            "Um calendário encerrado não prova histórico completo. Comparações com períodos "
            "em curso podem ter coberturas diferentes; não extrapolam um ano completo."
        )

    def quality_checklist(
        self, *, as_of: date | str | None = None
    ) -> list[dict[str, Any]]:
        reference = _day(as_of).isoformat()
        missing_references = int(
            self.repo.scalar(
                "SELECT COUNT(*) FROM financial_accounts WHERE is_active=1 "
                "AND (balance_known=0 OR reference_date='')"
            )
        )
        identity_problems = int(
            self.repo.scalar(
                "SELECT COUNT(*) FROM transactions t "
                "LEFT JOIN financial_accounts a ON a.stable_id=t.account_id "
                "LEFT JOIN categories c ON c.stable_id=t.category_id "
                "WHERE COALESCE(t.account_id,'')='' OR COALESCE(t.category_id,'')='' "
                "OR a.stable_id IS NULL OR c.stable_id IS NULL "
                "OR a.name<>t.account OR c.name<>t.category"
            )
        )
        unknown_costs = int(
            self.repo.scalar("SELECT COUNT(*) FROM holdings WHERE cost_basis_known=0")
        )
        unknown_fx = int(
            self.repo.scalar(
                "SELECT COUNT(*) FROM holdings WHERE currency<>'EUR' AND NOT ("
                + sql_finite_number("exchange_rate")
                + " AND exchange_rate>0)"
            )
        )
        months = int(
            self.repo.scalar(
                f"SELECT COUNT(DISTINCT SUBSTR({_FINANCIAL_DATE},1,7)) "
                "FROM transactions t LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
                f"WHERE {_FINANCIAL_DATE}<=? AND {sql_iso_date(_FINANCIAL_DATE)} "
                f"AND {sql_finite_number('t.amount', non_zero=True)}",
                (reference,),
            )
        )
        return [
            {
                "key": "references",
                "label": "Saldos de referência",
                "count": missing_references,
                "needs_review": missing_references > 0,
                "action_page": "carteira",
                "explanation": "Sem saldo confirmado, a conta mostra apenas o histórico registado; confirma saldo e data na carteira.",
            },
            {
                "key": "identities",
                "label": "Identidades dos movimentos",
                "count": identity_problems,
                "needs_review": identity_problems > 0,
                "action_page": "gestao_dados",
                "explanation": "Rever ligações e nomes de contas/categorias evita separar o mesmo histórico; não elimina movimentos.",
            },
            {
                "key": "costs",
                "label": "Custo dos investimentos",
                "count": unknown_costs,
                "needs_review": unknown_costs > 0,
                "action_page": "carteira",
                "explanation": "Custo desconhecido impede afirmar ganho/rentabilidade; completa a origem sem inferir vendas de notas.",
            },
            {
                "key": "fx",
                "label": "Câmbio declarado",
                "count": unknown_fx,
                "needs_review": unknown_fx > 0,
                "action_page": "carteira",
                "explanation": "Sem taxa válida não se pode converter o valor estrangeiro para euros; não se assume câmbio de hoje.",
            },
            {
                "key": "history",
                "label": "Meses com movimentos observados",
                "count": months,
                "needs_review": months == 0,
                "action_page": "importacao",
                "explanation": "Meses observados não garantem extrato completo. A checklist é opcional e não atribui uma penalização por ausência da aplicação.",
            },
        ]

    def administrative_items(
        self, *, as_of: date | str | None = None
    ) -> list[dict[str, Any]]:
        from services.reimbursements import ReimbursementsService
        from services.subscription_radar import SubscriptionRadarService

        reference = _day(as_of)
        items = []
        for claim in ReimbursementsService(self.repo).pending(reference):
            state_key = (
                f"{claim['id']}:{claim['outstanding_cents']}:{claim['deadline']}"
            )
            items.append(
                {
                    "key": "reimbursement:"
                    + hashlib.sha256(state_key.encode()).hexdigest(),
                    "severity": "warning" if claim["overdue"] else "info",
                    "kind": "reimbursement",
                    "origin": "Carteira de reembolsos",
                    "deadline": claim["deadline"],
                    "date": claim["deadline"] or reference.isoformat(),
                    "title": claim["label"],
                    "message": claim["detail"],
                    "amount": claim["outstanding_amount"],
                    "category": "Reembolsos",
                    "action_page": "movimentos",
                    "action": "reimbursements",
                }
            )
        for candidate in SubscriptionRadarService(self.repo).pending(reference):
            items.append(
                {
                    "key": "subscription-review:" + candidate.evidence_key,
                    "severity": "info",
                    "kind": "subscription_review",
                    "origin": "Radar de subscrições",
                    "deadline": "",
                    "date": candidate.financial_date,
                    "title": "Cobrança a rever",
                    "message": candidate.subscription_name + ": " + candidate.reason,
                    "amount": candidate.observed_amount,
                    "category": "Subscrições",
                    "action_page": "subscricoes",
                    "action": "subscription_radar",
                }
            )
        start = add_months(reference, -12).isoformat()
        documents = self.repo.q(
            f"SELECT t.id,t.description,t.amount,t.account_id,t.category_id,{_FINANCIAL_DATE} AS financial_date "
            "FROM transactions t LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
            f"WHERE t.amount<=-100 AND {sql_external_financial_flow('t.category')} "
            f"AND {sql_finite_number('t.amount', non_zero=True)} AND {sql_iso_date(_FINANCIAL_DATE)} "
            f"AND {_FINANCIAL_DATE}>=? AND {_FINANCIAL_DATE}<=? "
            "AND NOT EXISTS(SELECT 1 FROM transaction_attachments a WHERE a.transaction_id=t.id) ORDER BY t.id",
            (start, reference.isoformat()),
        )
        if documents:
            # Preferences follow the actual evidence, including replacement rows
            # that reuse an old numeric ID, rather than an aggregate row count.
            evidence = [dict(row) for row in documents]
            digest = hashlib.sha256(
                json.dumps(
                    evidence,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            items.append(
                {
                    "key": "documents-missing:" + digest,
                    "severity": "info",
                    "kind": "missing_documents",
                    "origin": "Movimentos e documentos",
                    "deadline": "",
                    "date": reference.isoformat(),
                    "title": "Despesas relevantes sem anexo",
                    "message": f"{len(documents)} despesa(s) de pelo menos 100 € entre {start} e {reference}.",
                    "amount": math.fsum(abs(row["amount"]) for row in documents),
                    "category": "Documentação",
                    "action_page": "movimentos",
                    "action": "missing_documents",
                    "start": start,
                    "end": reference.isoformat(),
                }
            )
        return items

    def _snoozes(self) -> dict[str, str]:
        try:
            values = json.loads(self.repo.setting("administrative_snoozes_v1", "{}"))
        except TypeError, ValueError:
            return {}
        if not isinstance(values, dict):
            return {}
        result = {}
        for key, value in values.items():
            try:
                result[str(key)] = _day(value).isoformat()
            except ValueError, TypeError:
                continue
        return result

    def visible_alerts(
        self, alerts: Iterable[dict[str, Any]], *, as_of: date | str | None = None
    ) -> list[dict[str, Any]]:
        reference = _day(as_of).isoformat()
        snoozes = self._snoozes()
        result = []
        for alert in alerts:
            item = dict(alert)
            item.setdefault(
                "origin",
                "Centro de alertas · " + str(item.get("kind") or "análise local"),
            )
            item.setdefault(
                "deadline",
                item.get("date", "") if item.get("kind") == "subscription" else "",
            )
            if snoozes.get(str(item.get("key")), "") > reference:
                continue
            result.append(item)
        return result

    def snooze(
        self, key: str, until: date | str, *, as_of: date | str | None = None
    ) -> None:
        if not isinstance(key, str) or not key or len(key) > 1024:
            raise ValueError("Seleciona uma pendência válida.")
        if _day(until) <= _day(as_of):
            raise ValueError("O adiamento tem de terminar depois da data de consulta.")
        values = self._snoozes()
        values[key] = _day(until).isoformat()
        self.repo.save_administrative_snoozes(values)

    def clear_snoozes(self) -> None:
        self.repo.save_administrative_snoozes({})

    def dismiss(self, key: str) -> None:
        if not isinstance(key, str) or not key or len(key) > 1024:
            raise ValueError("Seleciona uma pendência válida.")
        try:
            values = json.loads(self.repo.setting("dismissed_financial_alerts", "[]"))
        except TypeError, ValueError:
            values = []
        dismissed = (
            {str(value) for value in values} if isinstance(values, list) else set()
        )
        dismissed.add(key)
        self.repo.save_dismissed_financial_alerts(sorted(dismissed))

    def reset_choices(self) -> None:
        self.repo.reset_administrative_choices()
