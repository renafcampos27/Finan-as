"""Explicit reimbursement metadata writes retain caller transaction ownership."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

from database.validation import INTERNAL_FLOW_CATEGORIES, MAX_ABSOLUTE_FINANCIAL_VALUE
from utils.helpers import require_iso_date


def reimbursement_cents(value: Any, *, field: str = "Valor") -> int:
    try:
        number = Decimal(str(value))
        if (
            not number.is_finite()
            or number <= 0
            or number > Decimal(str(MAX_ABSOLUTE_FINANCIAL_VALUE))
        ):
            raise ValueError
        cents = number * 100
        if cents != cents.to_integral_value():
            raise ValueError
        return int(cents)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"{field}: usa um valor positivo em euros com até duas casas decimais."
        ) from exc


def _identity(value: Any) -> int:
    try:
        if isinstance(value, bool) or int(str(value)) <= 0:
            raise ValueError
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("Identificador de reembolso ou movimento inválido.") from exc


class ReimbursementsRepoMixin:
    @contextmanager
    def _reimbursement_write(
        self, *, commit: bool = True
    ) -> Iterator[sqlite3.Connection]:
        connection = self.db.conn
        owns = not connection.in_transaction
        if owns:
            connection.execute("BEGIN IMMEDIATE")
        name = "reimbursement_" + uuid.uuid4().hex
        connection.execute(f"SAVEPOINT {name}")
        try:
            yield connection
            connection.execute(f"RELEASE SAVEPOINT {name}")
            if owns and commit:
                connection.commit()
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
            connection.execute(f"RELEASE SAVEPOINT {name}")
            if owns:
                connection.rollback()
            raise

    def _reimbursement_movement(
        self, transaction_id: int, *, receipt: bool = False
    ) -> dict[str, Any]:
        identity = _identity(transaction_id)
        rows = self.q(
            "SELECT t.*,COALESCE(NULLIF(m.payment_date,''),t.date) AS financial_date "
            "FROM transactions t LEFT JOIN transaction_meta m ON m.transaction_id=t.id WHERE t.id=?",
            (identity,),
        )
        if not rows:
            raise ValueError("O movimento já não existe. Atualiza a carteira.")
        row = dict(rows[0])
        require_iso_date(
            row["financial_date"], field_name="data financeira do movimento"
        )
        try:
            amount = Decimal(str(row["amount"]))
        except InvalidOperation as exc:
            raise ValueError("O movimento não tem um valor monetário válido.") from exc
        if (
            not amount.is_finite()
            or amount == 0
            or (amount > 0) != receipt
            or row["kind"] != ("income" if receipt else "expense")
            or row.get("transfer_group_id")
            or row.get("category") in INTERNAL_FLOW_CATEGORIES
        ):
            raise ValueError(
                "Escolhe uma entrada externa recebida."
                if receipt
                else "Escolhe uma despesa externa."
            )
        reimbursement_cents(abs(amount), field="Valor do movimento")
        for kind, table in (
            ("account", "financial_accounts"),
            ("category", "categories"),
        ):
            stable = row.get(kind + "_id")
            if stable:
                target = self.q(
                    f"SELECT name FROM {table} WHERE stable_id=?", (stable,)
                )
                if not target or str(target[0]["name"]) != str(row.get(kind) or ""):
                    raise ValueError(
                        "A identidade da conta/categoria precisa de revisão antes da ligação."
                    )
        return row

    def create_reimbursement_claim(
        self,
        source_transaction_id: int,
        expected_amount: Any,
        *,
        label: str = "",
        counterpart: str = "",
        due_date: str = "",
        notes: str = "",
        commit: bool = True,
    ) -> int:
        cents = reimbursement_cents(expected_amount, field="Valor esperado")
        due = (
            require_iso_date(due_date, field_name="prazo do reembolso")
            if due_date
            else ""
        )
        with self._reimbursement_write(commit=commit) as connection:
            source = self._reimbursement_movement(source_transaction_id)
            if cents > reimbursement_cents(
                abs(Decimal(str(source["amount"]))), field="Despesa"
            ):
                raise ValueError(
                    "O valor esperado não pode exceder a despesa de origem."
                )
            title = str(label or source["description"] or "Reembolso").strip()
            if not title:
                raise ValueError("Indica uma descrição para o reembolso.")
            if connection.execute(
                "SELECT 1 FROM reimbursement_claims WHERE source_transaction_id=?",
                (source["id"],),
            ).fetchone():
                raise ValueError("Esta despesa já tem um pedido de reembolso.")
            cursor = connection.execute(
                "INSERT INTO reimbursement_claims(source_transaction_id,label,expected_cents,counterpart,due_date,notes) VALUES(?,?,?,?,?,?)",
                (source["id"], title, cents, str(counterpart).strip(), due, str(notes)),
            )
            return int(cursor.lastrowid)

    def link_reimbursement_receipt(
        self,
        claim_id: int,
        receipt_transaction_id: int,
        amount: Any,
        *,
        commit: bool = True,
    ) -> int:
        claim_id, transaction_id = (
            _identity(claim_id),
            _identity(receipt_transaction_id),
        )
        cents = reimbursement_cents(amount, field="Valor a ligar")
        with self._reimbursement_write(commit=commit) as connection:
            if not connection.execute(
                "SELECT 1 FROM reimbursement_claims WHERE id=?", (claim_id,)
            ).fetchone():
                raise ValueError("O pedido de reembolso já não existe.")
            self._reimbursement_movement(transaction_id, receipt=True)
            if connection.execute(
                "SELECT 1 FROM reimbursement_receipts WHERE claim_id=? AND receipt_transaction_id=?",
                (claim_id, transaction_id),
            ).fetchone():
                raise ValueError(
                    "Este movimento já está ligado ao pedido; remove a ligação antes de corrigir o valor."
                )
            try:
                cursor = connection.execute(
                    "INSERT INTO reimbursement_receipts(claim_id,receipt_transaction_id,amount_cents) VALUES(?,?,?)",
                    (claim_id, transaction_id, cents),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(str(exc)) from exc
            return int(cursor.lastrowid)

    def unlink_reimbursement_receipt(
        self, receipt_id: int, *, commit: bool = True
    ) -> None:
        with self._reimbursement_write(commit=commit) as connection:
            if (
                connection.execute(
                    "DELETE FROM reimbursement_receipts WHERE id=?",
                    (_identity(receipt_id),),
                ).rowcount
                != 1
            ):
                raise ValueError("A ligação de recebimento já não existe.")

    def delete_reimbursement_claim(self, claim_id: int, *, commit: bool = True) -> None:
        with self._reimbursement_write(commit=commit) as connection:
            identity = _identity(claim_id)
            if connection.execute(
                "SELECT 1 FROM reimbursement_receipts WHERE claim_id=?", (identity,)
            ).fetchone():
                raise ValueError(
                    "Remove primeiro as ligações a recebimentos. Os movimentos permanecem na conta."
                )
            if (
                connection.execute(
                    "DELETE FROM reimbursement_claims WHERE id=?", (identity,)
                ).rowcount
                != 1
            ):
                raise ValueError("O pedido de reembolso já não existe.")

    def reimbursement_claims(
        self, *, as_of: date | str | None = None
    ) -> list[dict[str, Any]]:
        day = (
            as_of.isoformat()
            if isinstance(as_of, date)
            else str(as_of or date.today().isoformat())
        )
        day = require_iso_date(day, field_name="data da carteira")
        rows = self.q(
            """
            SELECT c.*,t.description AS source_description,t.account AS source_account,
                t.category AS source_category,t.account_id AS source_account_id,
                t.category_id AS source_category_id,
                (SELECT COUNT(*) FROM transaction_attachments a WHERE a.transaction_id=t.id) AS document_count,
                COALESCE((SELECT SUM(r.amount_cents) FROM reimbursement_receipts r WHERE r.claim_id=c.id),0) AS allocated_cents,
                COALESCE((SELECT SUM(r.amount_cents) FROM reimbursement_receipts r
                    JOIN transactions received ON received.id=r.receipt_transaction_id
                    LEFT JOIN transaction_meta m ON m.transaction_id=received.id
                    WHERE r.claim_id=c.id AND COALESCE(NULLIF(m.payment_date,''),received.date)<=?),0) AS received_cents
            FROM reimbursement_claims c JOIN transactions t ON t.id=c.source_transaction_id
            ORDER BY c.id DESC
            """,
            (day,),
        )
        result = []
        for raw in rows:
            row = dict(raw)
            received = int(row["received_cents"])
            row.update(
                state="received"
                if received >= row["expected_cents"]
                else "partial"
                if received
                else "expected",
                outstanding_cents=int(row["expected_cents"]) - received,
                expected_amount=int(row["expected_cents"]) / 100,
                received_amount=received / 100,
                outstanding_amount=(int(row["expected_cents"]) - received) / 100,
                as_of=day,
            )
            result.append(row)
        return result

    def reimbursement_claim(
        self, claim_id: int, *, as_of: date | str | None = None
    ) -> dict[str, Any] | None:
        identity = _identity(claim_id)
        return next(
            (
                row
                for row in self.reimbursement_claims(as_of=as_of)
                if row["id"] == identity
            ),
            None,
        )

    def reimbursement_receipts(self, claim_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.q(
                "SELECT r.*,t.description,t.account,COALESCE(NULLIF(m.payment_date,''),t.date) AS financial_date "
                "FROM reimbursement_receipts r JOIN transactions t ON t.id=r.receipt_transaction_id "
                "LEFT JOIN transaction_meta m ON m.transaction_id=t.id WHERE r.claim_id=? ORDER BY r.id",
                (_identity(claim_id),),
            )
        ]

    def reimbursement_source(self, transaction_id: int) -> dict[str, Any]:
        return self._reimbursement_movement(transaction_id)

    def reimbursement_receipt_candidates(
        self, query: str = "", *, limit: int = 200
    ) -> list[dict[str, Any]]:
        text = str(query).strip()
        predicate, params = "", []
        if text.startswith("#") and text[1:].isdigit():
            predicate, params = " AND t.id=?", [int(text[1:])]
        elif text:
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            predicate, params = (
                " AND t.description LIKE ? ESCAPE '\\'",
                ["%" + escaped + "%"],
            )
        params.append(max(1, min(int(limit), 500)))
        rows = self.q(
            """SELECT t.*,COALESCE(NULLIF(m.payment_date,''),t.date) AS financial_date,
                COALESCE((SELECT SUM(r.amount_cents) FROM reimbursement_receipts r WHERE r.receipt_transaction_id=t.id),0) AS allocated_cents
                FROM transactions t LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE t.amount>0 AND t.kind='income' AND COALESCE(t.transfer_group_id,'')=''
                  AND t.category NOT IN ('Transferência Interna','Objetivos e Poupança')
                  AND (COALESCE(t.account_id,'')='' OR EXISTS(
                      SELECT 1 FROM financial_accounts a WHERE a.stable_id=t.account_id AND a.name=t.account))
                  AND (COALESCE(t.category_id,'')='' OR EXISTS(
                      SELECT 1 FROM categories c WHERE c.stable_id=t.category_id AND c.name=t.category))
                """
            + predicate
            + " ORDER BY t.date DESC,t.id DESC LIMIT ?",
            tuple(params),
        )
        result = []
        for raw in rows:
            row = dict(raw)
            try:
                available = reimbursement_cents(row["amount"]) - int(
                    row["allocated_cents"]
                )
                require_iso_date(row["financial_date"], field_name="data financeira")
            except ValueError:
                continue
            if available > 0:
                row["available_cents"] = available
                result.append(row)
        return result
