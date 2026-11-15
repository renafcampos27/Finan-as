"""Read-only charge proposals and explicit subscription review decisions."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from services.account_identity import account_identity_key
from services.subscription_matching import (
    normalized_subscription_text,
    subscription_match_terms,
)


PERIODS = ("Mensal", "Trimestral", "Semestral", "Anual")


@dataclass(slots=True, frozen=True)
class RadarCandidate:
    evidence_key: str
    evidence_json: str
    subscription_id: int
    transaction_id: int
    subscription_name: str
    description: str
    financial_date: str
    expected_amount: float
    observed_amount: float
    expected_period: str
    match_quality: str
    reason: str
    status: str = "pending"
    classification: str = "unclassified"

    @property
    def increase_pct(self) -> float:
        return (self.observed_amount / self.expected_amount - 1) * 100


class SubscriptionRadarService:
    """A larger settled debit is a proposal, not proof of a price increase.

    The cash ledger is denominated in EUR. Its rows do not establish the
    original charge's currency or billing interval; users confirm both before
    classifying a price increase. Reviews never mutate a subscription amount.
    """

    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self._observed_revisions: dict[str, tuple[int, int]] = {}

    def _revision(self) -> tuple[int, int]:
        connection = self.repo.db.conn
        return (
            connection.total_changes,
            int(connection.execute("PRAGMA data_version").fetchone()[0]),
        )

    def discover(self, as_of: date | None = None) -> list[RadarCandidate]:
        before = self._revision()
        candidates = self._candidates(as_of or date.today(), latest_only=True)
        after = self._revision()
        if before != after:
            raise ValueError("Os dados mudaram durante a leitura; atualiza o radar.")
        self._observed_revisions = {item.evidence_key: after for item in candidates}
        return candidates

    def pending(self, as_of: date | None = None) -> list[RadarCandidate]:
        return [item for item in self.discover(as_of) if item.status == "pending"]

    def _candidates(self, as_of: date, *, latest_only: bool) -> list[RadarCandidate]:
        if not isinstance(as_of, date):
            raise ValueError("Indica uma data válida para consultar o radar.")
        subscriptions = {
            int(row["id"]): dict(row)
            for row in self.repo.subscriptions(active_only=True)
            if row["period"] in PERIODS
        }
        if not subscriptions:
            return []
        terms: dict[str, set[int]] = {}
        for identity, row in subscriptions.items():
            for term in subscription_match_terms(row):
                terms.setdefault(term, set()).add(identity)
        grouped: dict[int, tuple[dict[str, Any], set[int]]] = {}
        for row in self.repo.subscription_radar_transactions(as_of.isoformat()):
            tx_id = int(row["id"])
            if tx_id not in grouped:
                grouped[tx_id] = (row, set())
            if row["linked_subscription_id"] is not None:
                grouped[tx_id][1].add(int(row["linked_subscription_id"]))
        decisions = {
            row["evidence_key"]: row for row in self.repo.subscription_price_reviews()
        }
        result = []
        seen = set()
        for transaction, linked_ids in grouped.values():
            description = normalized_subscription_text(transaction["description"])
            merchant = normalized_subscription_text(transaction["merchant"])
            matched: set[int] = set()
            if linked_ids:
                matched = linked_ids.intersection(subscriptions)
                quality = "occurrence" if len(linked_ids) == 1 else "ambiguous"
            else:
                matched.update(terms.get(description, set()))
                matched.update(terms.get(merchant, set()))
                quality = "exact" if len(matched) == 1 else "ambiguous"
                if not matched:
                    matched = {
                        identity
                        for identity, row in subscriptions.items()
                        if len(normalized_subscription_text(row["name"])) >= 4
                        and (
                            normalized_subscription_text(row["name"]) in description
                            or normalized_subscription_text(row["name"]) in merchant
                        )
                    }
                    quality = "possible" if len(matched) == 1 else "ambiguous"
            for subscription_id in sorted(matched):
                if latest_only and subscription_id in seen:
                    continue
                seen.add(subscription_id)
                subscription = subscriptions[subscription_id]
                expected = float(subscription["amount"])
                observed = abs(float(transaction["amount"]))
                if (
                    not math.isfinite(expected)
                    or not math.isfinite(observed)
                    or expected <= 0
                ):
                    continue
                if observed - expected < 0.005:
                    continue
                candidate_quality = quality
                account_id = str(subscription.get("account_id") or "")
                transaction_account_id = str(transaction.get("account_id") or "")
                same_account = (
                    account_id == transaction_account_id
                    if account_id and transaction_account_id
                    else account_identity_key(subscription.get("account"))
                    == account_identity_key(transaction["account"])
                )
                if not same_account and candidate_quality in {"occurrence", "exact"}:
                    candidate_quality = "account_mismatch"
                reason = {
                    "occurrence": "Cobrança ligada por ocorrência registada.",
                    "exact": "Nome ou alias confirmado corresponde exatamente ao extrato.",
                    "possible": "Texto semelhante; a ligação ainda não está confirmada.",
                    "ambiguous": "A cobrança pode corresponder a mais de uma subscrição.",
                    "account_mismatch": "A conta da cobrança difere da conta configurada; confirme a ligação.",
                }[candidate_quality]
                reason += " Valor superior ao configurado; confirme preço, plano, período e moeda original."
                evidence = {
                    "version": 1,
                    "subscription": {
                        key: subscription.get(key)
                        for key in (
                            "id",
                            "name",
                            "amount",
                            "period",
                            "active",
                            "aliases",
                            "account",
                            "account_id",
                        )
                    },
                    "transaction": {
                        key: transaction.get(key)
                        for key in (
                            "id",
                            "date",
                            "financial_date",
                            "description",
                            "amount",
                            "category",
                            "account",
                            "account_id",
                            "kind",
                            "merchant",
                            "created_at",
                        )
                    },
                    "linked_subscription_ids": sorted(linked_ids),
                    "matched_subscription_ids": sorted(matched),
                    "match_quality": candidate_quality,
                    "ledger_currency": "EUR",
                    "original_charge_currency": "unknown",
                    "observed_billing_period": "unknown",
                }
                evidence_json = json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                key = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
                stored = decisions.get(key, {})
                if stored and (
                    stored["subscription_id"] != subscription_id
                    or stored["transaction_id"] != transaction["id"]
                ):
                    # SET NULL marks deleted originals even if someone manually
                    # inserts identical data under the same numeric identifiers.
                    continue
                result.append(
                    RadarCandidate(
                        evidence_key=key,
                        evidence_json=evidence_json,
                        subscription_id=subscription_id,
                        transaction_id=int(transaction["id"]),
                        subscription_name=str(subscription["name"]),
                        description=str(transaction["description"]),
                        financial_date=str(transaction["financial_date"]),
                        expected_amount=expected,
                        observed_amount=observed,
                        expected_period=str(subscription["period"]),
                        match_quality=candidate_quality,
                        reason=reason,
                        status=str(stored.get("status", "pending")),
                        classification=str(
                            stored.get("classification", "unclassified")
                        ),
                    )
                )
        return result

    def decide(
        self,
        evidence_key: str,
        *,
        status: str,
        classification: str = "unclassified",
        confirmed_period: str = "",
        confirmed_currency: str = "",
        match_confirmed: bool = False,
        note: str = "",
        as_of: date | None = None,
    ) -> int:
        if status not in {"pending", "accepted", "rejected"}:
            raise ValueError("Estado de revisão inválido.")
        if classification not in {"unclassified", "price", "plan", "matching"}:
            raise ValueError("Classificação de revisão inválida.")
        if confirmed_period not in ("", *PERIODS):
            raise ValueError("Período de cobrança inválido.")
        currency = str(confirmed_currency).strip().upper()
        if currency and (
            len(currency) != 3 or not currency.isascii() or not currency.isalpha()
        ):
            raise ValueError("Indica o código da moeda original com três letras.")
        if status == "accepted" and classification == "unclassified":
            raise ValueError("Distingue preço, plano ou ligação antes de aceitar.")
        if (
            status == "accepted"
            and classification in {"plan", "matching"}
            and not str(note).strip()
        ):
            raise ValueError("Explica a mudança de plano ou o problema de ligação.")
        connection = self.repo.db.conn
        observed_revision = self._observed_revisions.get(evidence_key)
        if observed_revision is None:
            raise ValueError(
                "Atualiza o radar e revê a proposta antes de guardar uma decisão."
            )
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        savepoint = "radar_decision_" + uuid.uuid4().hex
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            if self._revision() != observed_revision:
                raise ValueError(
                    "A origem mudou depois do preview. Atualiza o radar antes de decidir."
                )
            candidate = next(
                (
                    item
                    for item in self.discover(as_of)
                    if item.evidence_key == evidence_key
                ),
                None,
            )
            if candidate is None:
                raise ValueError(
                    "A origem mudou, foi eliminada ou existe uma cobrança mais recente. Atualiza o radar."
                )
            if (
                status == "accepted"
                and classification == "price"
                and (
                    not match_confirmed
                    or confirmed_period != candidate.expected_period
                    or currency != "EUR"
                )
            ):
                raise ValueError(
                    "Para comparar preço, confirma a ligação, o mesmo período e moeda original EUR; não é inferido câmbio."
                )
            review_id = self.repo.save_subscription_price_review(
                subscription_id=candidate.subscription_id,
                transaction_id=candidate.transaction_id,
                evidence_key=candidate.evidence_key,
                evidence_json=candidate.evidence_json,
                status=status,
                classification=classification,
                confirmed_period=confirmed_period,
                confirmed_currency=currency,
                match_confirmed=match_confirmed,
                note=note,
            )
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            if owns_transaction:
                connection.commit()
            return review_id
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            if owns_transaction:
                connection.rollback()
            raise

    def history(self, as_of: date | None = None) -> list[dict[str, Any]]:
        all_keys = {
            item.evidence_key
            for item in self._candidates(as_of or date.today(), latest_only=False)
        }
        latest_keys = {item.evidence_key for item in self.discover(as_of)}
        return [
            dict(
                row,
                stale=row["evidence_key"] not in all_keys,
                latest=row["evidence_key"] in latest_keys,
            )
            for row in self.repo.subscription_price_reviews()
        ]
