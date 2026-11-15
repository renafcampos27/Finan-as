"""A reimbursement wallet links evidence; it never manufactures cash income."""

from __future__ import annotations

from datetime import date
from typing import Any


class ReimbursementsService:
    def __init__(self, repo: Any) -> None:
        self.repo = repo

    def create(self, transaction_id: int, amount: Any, **details: Any) -> int:
        return self.repo.create_reimbursement_claim(transaction_id, amount, **details)

    def link(self, claim_id: int, transaction_id: int, amount: Any) -> int:
        return self.repo.link_reimbursement_receipt(claim_id, transaction_id, amount)

    def unlink(self, receipt_id: int) -> None:
        self.repo.unlink_reimbursement_receipt(receipt_id)

    def delete(self, claim_id: int) -> None:
        self.repo.delete_reimbursement_claim(claim_id)

    def list_claims(self, *, as_of: date | str | None = None) -> list[dict[str, Any]]:
        return self.repo.reimbursement_claims(as_of=as_of)

    def pending(self, as_of: date | str | None = None) -> list[dict[str, Any]]:
        """Read-only administrative items; discovering them performs no writes."""
        claims = self.list_claims(as_of=as_of)
        return [
            dict(
                row,
                origin="reimbursements",
                transaction_id=row["source_transaction_id"],
                deadline=row["due_date"],
                target="movements",
                overdue=bool(row["due_date"] and row["due_date"] < row["as_of"]),
                detail="Recebimento parcial"
                if row["state"] == "partial"
                else "A aguardar recebimento",
            )
            for row in claims
            if row["state"] != "received"
        ]

    def source(self, transaction_id: int) -> dict[str, Any]:
        return self.repo.reimbursement_source(transaction_id)

    def receipt_candidates(self, query: str = "") -> list[dict[str, Any]]:
        return self.repo.reimbursement_receipt_candidates(query)
