"""Conservative matching between subscriptions and recorded transactions."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from database.validation import sql_external_financial_flow
from utils.helpers import row_get, safe_float


def normalized_subscription_text(value: Any) -> str:
    """Normalize spacing/case without inventing a fuzzy merchant identity."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return " ".join(text.split())


def subscription_aliases(row: Any) -> tuple[str, ...]:
    raw = str(row_get(row, "aliases", "") or "")
    values = [normalized_subscription_text(value) for value in re.split(r"[;\n]", raw)]
    return tuple(dict.fromkeys(value for value in values if value))


def subscription_match_terms(row: Any) -> tuple[str, ...]:
    name = normalized_subscription_text(row_get(row, "name", ""))
    values = [name]
    if name:
        values.append(f"{name} (subscrição)")
    values.extend(subscription_aliases(row))
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(slots=True, frozen=True)
class SubscriptionLinkageSummary:
    year_to_date: float = 0.0
    all_time: float = 0.0
    linked: int = 0
    probable: int = 0
    unlinked: int = 0
    ambiguous: int = 0


def subscription_history_linkage(
    repo: Any,
    subscriptions: Iterable[Any],
    *,
    reference_date: date | None = None,
) -> SubscriptionLinkageSummary:
    """Classify history without ever auto-linking approximate matches.

    Occurrences, the exact subscription name and user-confirmed aliases are
    considered linked. Similar text is reported as probable but never included
    in the historical cost until the user confirms it as an alias.
    """

    subscription_rows = list(subscriptions)
    subscription_ids = {int(row_get(row, "id", 0) or 0) for row in subscription_rows}
    exact_terms: dict[str, set[int]] = {}
    names: dict[int, str] = {}
    for subscription in subscription_rows:
        subscription_id = int(row_get(subscription, "id", 0) or 0)
        if not subscription_id:
            continue
        name = normalized_subscription_text(row_get(subscription, "name", ""))
        names[subscription_id] = name
        for term in subscription_match_terms(subscription):
            exact_terms.setdefault(term, set()).add(subscription_id)

    external_flow = sql_external_financial_flow("t.category")
    transactions = repo.q(
        f"""
        SELECT t.id, t.amount, t.description, t.category,
               COALESCE(m.merchant,'') AS merchant,
               COALESCE(NULLIF(m.payment_date,''),t.date) AS financial_date,
               o.subscription_id
        FROM transactions t
        LEFT JOIN transaction_meta m ON m.transaction_id=t.id
        LEFT JOIN subscription_occurrences o ON o.transaction_id=t.id
        WHERE t.kind='expense' AND {external_flow}
        """
    )
    today = reference_date or date.today()
    year_prefix = str(today.year)
    year_to_date = 0.0
    all_time = 0.0
    linked = probable = unlinked = ambiguous = 0

    for transaction in transactions:
        occurrence_id = int(row_get(transaction, "subscription_id", 0) or 0)
        matched_ids: set[int] = set()
        if occurrence_id in subscription_ids:
            matched_ids = {occurrence_id}
        else:
            description = normalized_subscription_text(
                row_get(transaction, "description", "")
            )
            merchant = normalized_subscription_text(
                row_get(transaction, "merchant", "")
            )
            matched_ids.update(exact_terms.get(description, set()))
            if merchant:
                matched_ids.update(exact_terms.get(merchant, set()))

        if len(matched_ids) == 1:
            amount = abs(safe_float(row_get(transaction, "amount", 0)))
            all_time += amount
            if str(row_get(transaction, "financial_date", "")).startswith(year_prefix):
                year_to_date += amount
            linked += 1
            continue
        if len(matched_ids) > 1:
            ambiguous += 1
            continue

        category = normalized_subscription_text(row_get(transaction, "category", ""))
        description = normalized_subscription_text(
            row_get(transaction, "description", "")
        )
        possible = any(
            len(name) >= 4 and name in description for name in names.values() if name
        )
        if possible:
            probable += 1
        elif category in {"subscrições", "subscricoes"}:
            unlinked += 1

    return SubscriptionLinkageSummary(
        year_to_date=year_to_date,
        all_time=all_time,
        linked=linked,
        probable=probable,
        unlinked=unlinked,
        ambiguous=ambiguous,
    )
