#!/usr/bin/env python3
"""Offline smoke checks for scheduled-payment orchestration."""

from __future__ import annotations

import sys
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.payments import PaymentsService


class FakeRepo:
    def __init__(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE scheduled_payment_occurrences "
            "(scheduled_id INTEGER, due_date TEXT, transaction_id INTEGER, "
            "UNIQUE(scheduled_id, due_date))"
        )
        self.db = SimpleNamespace(conn=connection)
        self.subscription_rows = [
            {"amount": 120.0, "period": "Anual"},
            {"amount": 90.0, "period": "Trimestral"},
            {"amount": 60.0, "period": "Semestral"},
            {"amount": 10.0, "period": "Mensal"},
        ]
        self.payment_rows = [
            {
                "id": 1,
                "name": "Prestação",
                "amount": 100.0,
                "category": "Casa",
                "account": "Conta",
                "notes": "",
                "payment_method": "Débito direto",
                "subcategory": "Crédito",
                "start_date": "2026-01-31",
                "debit_day": 31,
                "months_total": 3,
                "months_paid": 0,
                "active": 1,
            },
            {
                "id": 2,
                "name": "Serviço contínuo",
                "amount": 25.0,
                "category": "Serviços",
                "account": "Conta",
                "notes": "",
                "payment_method": "Cartão",
                "subcategory": "",
                "start_date": "2026-01-15",
                "debit_day": 15,
                "months_total": 0,
                "months_paid": 0,
                "active": 1,
            },
            {
                "id": 3,
                "name": "Inativo",
                "amount": 50.0,
                "category": "Outros",
                "account": "Conta",
                "notes": "",
                "payment_method": "",
                "subcategory": "",
                "start_date": "2026-01-01",
                "debit_day": 1,
                "months_total": 2,
                "months_paid": 0,
                "active": 0,
            },
        ]
        self.occurrences: set[tuple[int, str]] = set()
        self.transactions: list[dict[str, Any]] = []
        self.metadata: list[tuple[int, dict[str, Any]]] = []
        self.progress: dict[int, tuple[int, int]] = {}

    def subscriptions(self, active_only: bool = False) -> list[dict[str, Any]]:
        return list(self.subscription_rows)

    def scheduled_payments(self) -> list[dict[str, Any]]:
        return self.payment_rows

    def scheduled_payment_occurrence_exists(self, scheduled_id: int, due: str) -> bool:
        return (scheduled_id, due) in self.occurrences

    def add_transaction(
        self,
        due: str,
        description: str,
        amount: float,
        category: str,
        account: str,
        notes: str,
    ) -> int:
        transaction_id = len(self.transactions) + 1
        self.transactions.append(
            {
                "id": transaction_id,
                "date": due,
                "description": description,
                "amount": amount,
                "category": category,
                "account": account,
                "notes": notes,
            }
        )
        return transaction_id

    def set_transaction_meta_ext(self, transaction_id: int, **values: Any) -> None:
        self.metadata.append((transaction_id, values))

    def add_scheduled_payment_occurrence(
        self,
        scheduled_id: int,
        due: str,
        transaction_id: int,
    ) -> None:
        self.occurrences.add((scheduled_id, due))
        self.db.conn.execute(
            "INSERT OR IGNORE INTO scheduled_payment_occurrences"
            "(scheduled_id,due_date,transaction_id) VALUES(?,?,?)",
            (scheduled_id, due, transaction_id),
        )

    def create_scheduled_payment_charge(
        self,
        scheduled_id: int,
        due: str,
        description: str,
        amount: float,
        category: str,
        account: str,
        notes: str,
        **metadata: Any,
    ) -> int | None:
        if self.scheduled_payment_occurrence_exists(scheduled_id, due):
            return None
        transaction_id = self.add_transaction(
            due,
            description,
            -abs(amount),
            category,
            account,
            notes,
        )
        self.set_transaction_meta_ext(transaction_id, **metadata)
        self.add_scheduled_payment_occurrence(scheduled_id, due, transaction_id)
        return transaction_id

    def scheduled_payment_occurrence_count(self, scheduled_id: int) -> int:
        return sum(1 for item in self.occurrences if item[0] == scheduled_id)

    def _create_scheduled_payment_charge_uncommitted(
        self,
        scheduled_id: int,
        due: str,
        description: str,
        amount: float,
        category: str,
        account: str,
        notes: str,
        **metadata: Any,
    ) -> int | None:
        return self.create_scheduled_payment_charge(
            scheduled_id,
            due,
            description,
            amount,
            category,
            account,
            notes,
            **metadata,
        )

    def update_scheduled_payment_progress(
        self,
        scheduled_id: int,
        months_paid: int,
        active: int,
    ) -> None:
        self.progress[scheduled_id] = (months_paid, active)
        for row in self.payment_rows:
            if row["id"] == scheduled_id:
                row["months_paid"] = months_paid
                row["active"] = active

    def _update_scheduled_payment_progress_uncommitted(
        self,
        scheduled_id: int,
        months_paid: int,
        active: int,
    ) -> Any:
        self.update_scheduled_payment_progress(scheduled_id, months_paid, active)
        return SimpleNamespace(rowcount=1)

    def _capture_planning_state(self, *, commit: bool = True) -> None:
        del commit

    def q(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [
            {
                "key": "energia",
                "description": "Energia",
                "category": "Casa",
                "amount": 45.0,
                "c": 3,
                "first_date": "2026-01-01",
                "last_date": "2026-03-01",
            }
        ][: int(params[0])]


def main() -> int:
    repo = FakeRepo()
    service = PaymentsService(repo)

    assert service.active_subscription_monthly_total() == 60.0
    assert (
        service.due_date_for_payment(repo.payment_rows[0], 1).isoformat()
        == "2026-02-28"
    )
    assert service.next_due_for_payment(repo.payment_rows[1]) == "2026-01-15"

    created = service.generate_due_scheduled_payments(date(2026, 3, 31))
    assert created == 6
    assert len(repo.transactions) == 6
    assert len(repo.metadata) == 6
    assert repo.progress[1] == (3, 0)
    assert repo.progress[2] == (3, 1)
    assert 3 not in repo.progress
    assert repo.transactions[0]["description"] == "Prestação (1/3)"
    assert service.next_due_for_payment(repo.payment_rows[0]) == "Concluído"
    assert service.next_due_for_payment(repo.payment_rows[1]) == "2026-04-15"

    assert service.generate_due_scheduled_payments(date(2026, 3, 31)) == 0
    recurring = service.likely_recurring_expenses(10)
    assert len(recurring) == 1 and recurring[0]["c"] == 3

    print("Payments service smoke OK")
    print("monthly_subscription_total", 60.0)
    print("generated_transactions", created)
    print("unlimited_payment_active", True)
    print("idempotent_generation", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
