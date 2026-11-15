"""Source-owned I2 metadata; receipts reference cash already in the ledger."""

from __future__ import annotations

from typing import Any

from database.validation import sql_finite_number, sql_iso_date


def create_reimbursement_schema(db: Any) -> None:
    """Create evidence tables without rewriting any existing financial rows."""
    db.run_schema_script(
        f"""
        CREATE TABLE IF NOT EXISTS reimbursement_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_transaction_id INTEGER NOT NULL UNIQUE
                REFERENCES transactions(id) ON DELETE RESTRICT,
            label TEXT NOT NULL CHECK(LENGTH(TRIM(label))>0),
            expected_cents INTEGER NOT NULL
                CHECK(TYPEOF(expected_cents)='integer' AND expected_cents>0
                      AND expected_cents<=100000000000000000),
            currency TEXT NOT NULL DEFAULT 'EUR' CHECK(currency='EUR'),
            counterpart TEXT NOT NULL DEFAULT '',
            due_date TEXT NOT NULL DEFAULT '' CHECK({sql_iso_date("due_date", allow_empty=True)}),
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reimbursement_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL REFERENCES reimbursement_claims(id) ON DELETE RESTRICT,
            receipt_transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE RESTRICT,
            amount_cents INTEGER NOT NULL
                CHECK(TYPEOF(amount_cents)='integer' AND amount_cents>0
                      AND amount_cents<=100000000000000000),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(claim_id,receipt_transaction_id)
        );
        CREATE INDEX IF NOT EXISTS idx_reimbursement_receipts_transaction
            ON reimbursement_receipts(receipt_transaction_id);
        """
    )
    external = (
        "COALESCE(t.transfer_group_id,'')='' AND "
        "COALESCE(t.category,'') NOT IN ('Transferência Interna','Objetivos e Poupança')"
    )
    settled = (
        sql_finite_number("t.amount", non_zero=True)
        + " AND ABS(t.amount*100-ROUND(t.amount*100))<=0.000001"
    )
    for operation in ("INSERT", "UPDATE"):
        exclusion = "AND id<>OLD.id" if operation == "UPDATE" else ""
        source_guard = (
            "SELECT CASE WHEN NEW.source_transaction_id<>OLD.source_transaction_id "
            "THEN RAISE(ABORT,'A despesa de origem do pedido é imutável') END;"
            if operation == "UPDATE"
            else ""
        )
        db.run_schema_script(
            f"""
            CREATE TRIGGER IF NOT EXISTS validate_reimbursement_claim_{operation.lower()}
            BEFORE {operation} ON reimbursement_claims BEGIN
                {source_guard}
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM transactions t
                    WHERE t.id=NEW.source_transaction_id AND t.amount<0
                      AND t.kind='expense' AND {external} AND {settled}
                      AND NEW.expected_cents<=CAST(ROUND(-t.amount*100) AS INTEGER)
                ) THEN RAISE(ABORT,'Despesa de origem ou valor esperado incompatível') END;
                SELECT CASE WHEN NEW.expected_cents < (
                    SELECT COALESCE(SUM(amount_cents),0) FROM reimbursement_receipts
                    WHERE claim_id=NEW.id
                ) THEN RAISE(ABORT,'Valor esperado inferior aos recebimentos ligados') END;
            END;
            CREATE TRIGGER IF NOT EXISTS validate_reimbursement_receipt_{operation.lower()}
            BEFORE {operation} ON reimbursement_receipts BEGIN
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM transactions t
                    WHERE t.id=NEW.receipt_transaction_id AND t.amount>0
                      AND t.kind='income' AND {external} AND {settled}
                ) THEN RAISE(ABORT,'Recebimento exige entrada externa válida em euros') END;
                SELECT CASE WHEN NEW.amount_cents + (
                    SELECT COALESCE(SUM(amount_cents),0) FROM reimbursement_receipts
                    WHERE claim_id=NEW.claim_id {exclusion}
                ) > (SELECT expected_cents FROM reimbursement_claims WHERE id=NEW.claim_id)
                THEN RAISE(ABORT,'Recebimentos excedem o valor esperado') END;
                SELECT CASE WHEN NEW.amount_cents + (
                    SELECT COALESCE(SUM(amount_cents),0) FROM reimbursement_receipts
                    WHERE receipt_transaction_id=NEW.receipt_transaction_id {exclusion}
                ) > (SELECT CAST(ROUND(amount*100) AS INTEGER) FROM transactions
                     WHERE id=NEW.receipt_transaction_id)
                THEN RAISE(ABORT,'Movimento recebido já está integralmente atribuído') END;
            END;
            """
        )
    new_external = external.replace("t.", "NEW.")
    new_settled = settled.replace("t.", "NEW.")
    db.run_schema_script(
        f"""
        CREATE TRIGGER IF NOT EXISTS protect_reimbursement_ledger_delete
        BEFORE DELETE ON transactions
        WHEN EXISTS(SELECT 1 FROM reimbursement_claims WHERE source_transaction_id=OLD.id)
          OR EXISTS(SELECT 1 FROM reimbursement_receipts WHERE receipt_transaction_id=OLD.id)
        BEGIN
            SELECT RAISE(ABORT,'Movimento ligado a reembolso: remove primeiro as ligações e o pedido na carteira de reembolsos');
        END;
        CREATE TRIGGER IF NOT EXISTS protect_reimbursement_ledger_update
        BEFORE UPDATE ON transactions BEGIN
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM reimbursement_claims c WHERE c.source_transaction_id=OLD.id
                  AND NOT (NEW.id=OLD.id AND NEW.amount<0 AND NEW.kind='expense'
                    AND {new_external} AND {new_settled}
                    AND c.expected_cents<=CAST(ROUND(-NEW.amount*100) AS INTEGER))
            ) THEN RAISE(ABORT,'Desliga o pedido de reembolso antes de alterar esta despesa') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM reimbursement_receipts r WHERE r.receipt_transaction_id=OLD.id
                GROUP BY r.receipt_transaction_id
                HAVING NOT (NEW.id=OLD.id AND NEW.amount>0 AND NEW.kind='income'
                    AND {new_external} AND {new_settled}
                    AND SUM(r.amount_cents)<=CAST(ROUND(NEW.amount*100) AS INTEGER))
            ) THEN RAISE(ABORT,'Desliga os recebimentos antes de alterar este movimento') END;
        END;
        """
    )
