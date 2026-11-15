"""Persistence of reviewed subscription evidence, separate from the cash ledger."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from database.validation import (
    sql_external_financial_flow,
    sql_finite_number,
    sql_iso_date,
)


class SubscriptionRadarRepoMixin:
    def subscription_radar_transactions(self, end_day: str) -> list[dict[str, Any]]:
        financial_date = "COALESCE(NULLIF(m.payment_date,''),t.date)"
        return [
            dict(row)
            for row in self.q(
                f"""
                SELECT t.id,t.date,t.description,t.amount,t.category,t.account,
                       t.account_id,t.kind,t.created_at,COALESCE(m.merchant,'') AS merchant,
                       {financial_date} AS financial_date,
                       o.subscription_id AS linked_subscription_id,o.due_date
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                LEFT JOIN subscription_occurrences o ON o.transaction_id=t.id
                WHERE t.amount<0 AND t.kind='expense'
                  AND {sql_finite_number("t.amount", non_zero=True)}
                  AND {sql_iso_date(financial_date)}
                  AND {sql_external_financial_flow("t.category")}
                  AND {financial_date}<=?
                ORDER BY financial_date DESC,t.id DESC,o.subscription_id
                """,
                (end_day,),
            )
        ]

    def subscription_price_reviews(
        self, subscription_id: int | None = None
    ) -> list[dict[str, Any]]:
        where = "WHERE subscription_id=?" if subscription_id is not None else ""
        params = (int(subscription_id),) if subscription_id is not None else ()
        return [
            dict(row)
            for row in self.q(
                "SELECT * FROM subscription_price_reviews "
                + where
                + " ORDER BY id DESC",
                params,
            )
        ]

    def save_subscription_price_review(
        self,
        *,
        subscription_id: int,
        transaction_id: int,
        evidence_key: str,
        evidence_json: str,
        classification: str,
        status: str,
        confirmed_period: str = "",
        confirmed_currency: str = "",
        match_confirmed: bool = False,
        note: str = "",
    ) -> int:
        """Save only a decision, never a movement or subscription price.

        The immutable evidence digest is revalidated at the persistence boundary;
        the service additionally verifies freshness and comparison semantics.
        """
        decoded = json.loads(evidence_json)
        if not isinstance(decoded, dict):
            raise ValueError("A evidência da cobrança tem de ser um objeto.")
        canonical = json.dumps(
            decoded,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != evidence_key:
            raise ValueError(
                "A evidência da cobrança não corresponde ao identificador."
            )
        if int(decoded.get("subscription", {}).get("id", 0)) != int(
            subscription_id
        ) or int(decoded.get("transaction", {}).get("id", 0)) != int(transaction_id):
            raise ValueError("A evidência refere outra subscrição ou movimento.")
        if classification not in {"unclassified", "price", "plan", "matching"}:
            raise ValueError("Classificação de revisão inválida.")
        if status not in {"pending", "accepted", "rejected"}:
            raise ValueError("Estado de revisão inválido.")
        if status == "accepted" and classification == "unclassified":
            raise ValueError("Classifica a alteração antes de aceitar a revisão.")
        if (
            status == "accepted"
            and classification == "price"
            and (
                not match_confirmed
                or confirmed_currency != "EUR"
                or confirmed_period != decoded.get("subscription", {}).get("period")
            )
        ):
            raise ValueError(
                "Confirma a ligação, o mesmo período e a moeda EUR antes de aceitar um aumento."
            )
        connection = self.db.conn
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        savepoint = "subscription_review_" + uuid.uuid4().hex
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            existing = connection.execute(
                "SELECT subscription_id,transaction_id FROM subscription_price_reviews WHERE evidence_key=?",
                (evidence_key,),
            ).fetchone()
            if existing is not None and tuple(existing) != (
                int(subscription_id),
                int(transaction_id),
            ):
                raise ValueError(
                    "A origem desta revisão foi eliminada; não pode ser substituída por outro registo."
                )
            if status == "accepted" and classification in {"price", "plan"}:
                conflict = connection.execute(
                    "SELECT 1 FROM subscription_price_reviews WHERE transaction_id=? "
                    "AND subscription_id<>? AND status='accepted' "
                    "AND classification IN ('price','plan') LIMIT 1",
                    (int(transaction_id), int(subscription_id)),
                ).fetchone()
                if conflict is not None:
                    raise ValueError(
                        "Esta cobrança já foi confirmada para outra subscrição."
                    )
            connection.execute(
                """
                INSERT INTO subscription_price_reviews(
                    subscription_id,transaction_id,evidence_key,evidence_json,
                    classification,status,confirmed_period,confirmed_currency,match_confirmed,note
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(evidence_key) DO UPDATE SET
                    classification=excluded.classification,status=excluded.status,
                    confirmed_period=excluded.confirmed_period,
                    confirmed_currency=excluded.confirmed_currency,note=excluded.note,
                    match_confirmed=excluded.match_confirmed,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    int(subscription_id),
                    int(transaction_id),
                    evidence_key,
                    canonical,
                    classification,
                    status,
                    confirmed_period,
                    confirmed_currency,
                    int(bool(match_confirmed)),
                    str(note).strip(),
                ),
            )
            review_id = connection.execute(
                "SELECT id FROM subscription_price_reviews WHERE evidence_key=?",
                (evidence_key,),
            ).fetchone()[0]
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            if owns_transaction:
                connection.commit()
            return int(review_id)
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            if owns_transaction:
                connection.rollback()
            raise
