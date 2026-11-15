#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

from typing import Any, Optional


class DocumentsRepoMixin:
    def add_transaction_attachment(
        self,
        *,
        transaction_id: int,
        file_name: str,
        stored_name: str,
        stored_path: str,
        original_path: str,
        file_type: str,
        description: str,
        supplier: str,
        invoice_date: str,
        total_amount: Optional[float],
        content_sha256: str = "",
        file_size: int = 0,
        file_mtime_ns: int = 0,
    ) -> int:
        try:
            cursor = self.db.conn.execute(
                """
                INSERT INTO transaction_attachments(
                    transaction_id,file_name,stored_name,stored_path,original_path,
                    file_type,description,supplier,invoice_date,total_amount,
                    content_sha256,file_size,file_mtime_ns,last_verified_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                """,
                (
                    int(transaction_id),
                    str(file_name),
                    str(stored_name),
                    str(stored_path),
                    str(original_path),
                    str(file_type),
                    str(description or ""),
                    str(supplier or ""),
                    str(invoice_date or ""),
                    total_amount,
                    str(content_sha256 or "").casefold(),
                    max(0, int(file_size or 0)),
                    max(0, int(file_mtime_ns or 0)),
                ),
            )
            self.db.conn.commit()
            return int(cursor.lastrowid)
        except Exception:
            self.db.conn.rollback()
            raise

    def transaction_attachment(self, attachment_id: int) -> Optional[Any]:
        rows = self.q(
            "SELECT * FROM transaction_attachments WHERE id=?",
            (int(attachment_id),),
        )
        return rows[0] if rows else None

    def update_transaction_attachment_integrity(
        self,
        attachment_id: int,
        *,
        content_sha256: str,
        file_size: int,
        file_mtime_ns: int = 0,
    ) -> None:
        try:
            self.db.conn.execute(
                """
                UPDATE transaction_attachments
                SET content_sha256=?, file_size=?, file_mtime_ns=?,
                    last_verified_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    str(content_sha256 or "").casefold(),
                    max(0, int(file_size or 0)),
                    max(0, int(file_mtime_ns or 0)),
                    int(attachment_id),
                ),
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def transaction_attachments(self, transaction_id: int) -> list[Any]:
        return self.q(
            "SELECT * FROM transaction_attachments WHERE transaction_id=? ORDER BY id DESC",
            (int(transaction_id),),
        )

    def transaction_attachment_names(
        self, transaction_id: int, limit: Optional[int] = None
    ) -> list[str]:
        rows = self.transaction_attachments(transaction_id)
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        return [
            str(r["file_name"] or "") for r in rows if str(r["file_name"] or "").strip()
        ]

    def delete_transaction_attachments(self, attachment_ids: list[int]) -> int:
        ids = list(
            dict.fromkeys(int(value) for value in attachment_ids if int(value) > 0)
        )
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        try:
            cursor = self.db.conn.execute(
                f"DELETE FROM transaction_attachments WHERE id IN ({placeholders})",
                tuple(ids),
            )
            self.db.conn.commit()
            return max(0, int(cursor.rowcount))
        except Exception:
            self.db.conn.rollback()
            raise

    def document_attachments(
        self,
        search: str = "",
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[Any]:
        search = str(search or "").strip()
        sql = """
            SELECT a.*, t.date AS tx_date, t.description AS tx_desc
            FROM transaction_attachments a
            LEFT JOIN transactions t ON t.id=a.transaction_id
            """
        params: list[Any] = []
        if search:
            term = f"%{search}%"
            sql += """
                WHERE a.file_name LIKE ? OR a.description LIKE ?
                   OR a.supplier LIKE ? OR t.description LIKE ?
                """
            params.extend([term, term, term, term])
        sql += " ORDER BY a.created_at DESC, a.id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
            if int(offset) > 0:
                sql += " OFFSET ?"
                params.append(max(0, int(offset)))
        elif int(offset) > 0:
            sql += " LIMIT -1 OFFSET ?"
            params.append(max(0, int(offset)))
        return self.q(sql, tuple(params))

    def document_attachment_count(self, search: str = "") -> int:
        search = str(search or "").strip()
        if not search:
            return int(
                self.scalar(
                    "SELECT COUNT(*) FROM transaction_attachments",
                    default=0,
                )
            )
        term = f"%{search}%"
        return int(
            self.scalar(
                """
                SELECT COUNT(*)
                FROM transaction_attachments a
                LEFT JOIN transactions t ON t.id=a.transaction_id
                WHERE a.file_name LIKE ? OR a.description LIKE ?
                   OR a.supplier LIKE ? OR t.description LIKE ?
                """,
                (term, term, term, term),
                default=0,
            )
        )
