#!/usr/bin/env python3
"""Offline smoke checks for transaction document orchestration."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.documents import DocumentService


class FakeRepo:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.deleted_transactions: list[int] = []
        self.fail_next_insert = False
        self.fail_next_delete = False

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
        if self.fail_next_insert:
            self.fail_next_insert = False
            raise RuntimeError("simulated database failure")
        record = {
            "id": len(self.records) + 1,
            "transaction_id": transaction_id,
            "file_name": file_name,
            "stored_name": stored_name,
            "stored_path": stored_path,
            "original_path": original_path,
            "file_type": file_type,
            "description": description,
            "supplier": supplier,
            "invoice_date": invoice_date,
            "total_amount": total_amount,
            "content_sha256": content_sha256,
            "file_size": file_size,
            "file_mtime_ns": file_mtime_ns,
            "last_verified_at": "now",
        }
        self.records.append(record)
        return int(record["id"])

    def transaction_attachment(self, attachment_id: int) -> Optional[dict[str, Any]]:
        return next(
            (
                record
                for record in self.records
                if int(record["id"]) == int(attachment_id)
            ),
            None,
        )

    def update_transaction_attachment_integrity(
        self,
        attachment_id: int,
        *,
        content_sha256: str,
        file_size: int,
        file_mtime_ns: int = 0,
    ) -> None:
        record = self.transaction_attachment(attachment_id)
        if record is not None:
            record["content_sha256"] = content_sha256
            record["file_size"] = file_size
            record["file_mtime_ns"] = file_mtime_ns
            record["last_verified_at"] = "now"

    def transaction_attachments(self, transaction_id: int) -> list[dict[str, Any]]:
        return [
            record
            for record in self.records
            if int(record["transaction_id"]) == int(transaction_id)
        ]

    def delete_transaction(self, transaction_id: int) -> None:
        self.delete_transactions([transaction_id])

    def delete_transactions(self, transaction_ids: list[int]) -> int:
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise RuntimeError("simulated delete failure")
        ids = [int(value) for value in transaction_ids]
        self.deleted_transactions.extend(ids)
        self.records = [
            record
            for record in self.records
            if int(record["transaction_id"]) not in ids
        ]
        return len(ids)


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source_directory = root / "source"
        managed_directory = root / "managed"
        source_directory.mkdir()
        source = source_directory / "2026-06-20_Loja_Central_123,45.pdf"
        source.write_bytes(b"%PDF-1.4\ninvoice\n%%EOF\n")

        repo = FakeRepo()
        service = DocumentService(
            repo,
            attachments_directory=managed_directory,
        )

        first = service.attach_file(42, source, "Fatura de teste")
        second = service.attach_file(42, source, "Segunda cópia")
        assert first.exists() and second.exists()
        assert first != second
        assert len(repo.records) == 2
        assert repo.records[0]["file_type"] == "PDF"
        assert repo.records[0]["invoice_date"] == "2026-06-20"
        assert repo.records[0]["total_amount"] == 123.45
        assert "Loja Central" in repo.records[0]["supplier"]

        before_failure = set(managed_directory.rglob("*"))
        repo.fail_next_insert = True
        try:
            service.attach_file(42, source, "Falha simulada")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Database failure did not propagate")
        after_failure = set(managed_directory.rglob("*"))
        assert before_failure == after_failure

        outside = root / "outside.txt"
        outside.write_text("must not be deleted", encoding="utf-8")
        repo.records.append(
            {
                "id": 99,
                "transaction_id": 42,
                "stored_path": str(outside),
            }
        )
        repo.fail_next_delete = True
        try:
            service.delete_transaction(42)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Database delete failure did not propagate")
        assert first.exists() and second.exists()

        removed = service.delete_transaction(42)
        assert removed == 2
        assert outside.exists()
        assert not first.exists() and not second.exists()
        assert repo.deleted_transactions == [42]

        invalid = source_directory / "2026-99-99_Fornecedor_10,00.png"
        invalid.write_bytes(b"image")
        supplier, invoice_date, amount = service.infer_metadata(invalid)
        assert supplier == "Fornecedor"
        assert invoice_date == ""
        assert amount == 10.0
        assert service.attachment_type(invalid) == "Imagem"

        try:
            service.attach_file(7, source_directory / "missing.pdf")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("Missing attachment source was accepted")

    print("Document service smoke OK")
    print("unique_names", True)
    print("metadata_inference", True)
    print("copy_rollback", True)
    print("managed_delete_guard", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
