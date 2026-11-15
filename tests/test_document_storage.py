from __future__ import annotations

import sqlite3
import os
import time
from pathlib import Path

import pytest

from database.repo import Repo
from services.documents import DocumentService


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "documents.sqlite3"))
    instance = Repo()
    yield instance
    instance.db.conn.close()


def _movement(repo: Repo, description: str) -> int:
    return repo.add_transaction_with_meta(
        "2026-07-17",
        description,
        -25.0,
        "Outros",
        "Conta principal",
        tags="original",
        status="Pendente",
        payment_date="2026-07-17",
    )


def _attachment_record(
    repo: Repo, transaction_id: int, stored_path: Path, name: str
) -> int:
    return repo.add_transaction_attachment(
        transaction_id=transaction_id,
        file_name=name,
        stored_name=stored_path.name,
        stored_path=str(stored_path),
        original_path=str(stored_path),
        file_type="PDF",
        description="Teste de integridade",
        supplier="Fornecedor",
        invoice_date="2026-07-17",
        total_amount=25.0,
    )


def test_storage_audit_and_safe_cleanup(repo: Repo, tmp_path: Path) -> None:
    transaction_id = _movement(repo, "Movimento documentado")
    managed = tmp_path / "anexos"
    service = DocumentService(repo, attachments_directory=managed)
    source = tmp_path / "fatura.pdf"
    source.write_bytes(b"%PDF-1.4\navailable\n%%EOF\n")
    available_path = service.attach_file(transaction_id, source, "Disponível")

    missing_path = managed / str(transaction_id) / "missing.pdf"
    missing_id = _attachment_record(repo, transaction_id, missing_path, "missing.pdf")
    outside_path = tmp_path / "outside.pdf"
    outside_path.write_bytes(b"outside")
    outside_id = _attachment_record(repo, transaction_id, outside_path, "outside.pdf")
    orphan_path = managed / "orphan" / "unlinked.pdf"
    orphan_path.parent.mkdir(parents=True)
    orphan_path.write_bytes(b"orphan")
    old_timestamp = time.time() - 180
    os.utime(orphan_path, (old_timestamp, old_timestamp))

    audit = service.audit_storage()

    assert audit["total_records"] == 3
    assert repo.document_attachment_count() == 3
    first_page = repo.document_attachments(limit=2)
    second_page = repo.document_attachments(limit=2, offset=2)
    assert len(first_page) == 2 and len(second_page) == 1
    assert {int(row["id"]) for row in first_page}.isdisjoint(
        {int(row["id"]) for row in second_page}
    )
    assert audit["available_records"] == 1
    assert audit["missing_record_ids"] == [missing_id]
    assert audit["outside_record_ids"] == [outside_id]
    assert audit["orphan_files"] == [str(orphan_path.resolve())]
    assert audit["healthy"] is False

    assert service.remove_orphan_files() == 1
    assert not orphan_path.exists()
    assert available_path.exists()
    assert outside_path.exists()

    assert service.remove_missing_references() == 1
    remaining_ids = {
        int(row["id"]) for row in repo.transaction_attachments(transaction_id)
    }
    assert missing_id not in remaining_ids
    assert outside_id in remaining_ids


def test_attachment_is_verified_before_its_record_is_committed(
    repo: Repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.documents as documents_module

    transaction_id = _movement(repo, "Documento verificado")
    managed = tmp_path / "anexos"
    service = DocumentService(repo, attachments_directory=managed)
    source = tmp_path / "origem.pdf"
    source.write_bytes(b"%PDF-1.4\nconteudo financeiro correto\n%%EOF\n")

    def corrupt_copy(_source: Path, target: Path) -> Path:
        Path(target).write_bytes(b"conteudo truncado")
        return Path(target)

    monkeypatch.setattr(documents_module.shutil, "copy2", corrupt_copy)
    with pytest.raises(OSError, match="não corresponde"):
        service.attach_file(transaction_id, source)

    assert repo.transaction_attachments(transaction_id) == []
    assert list(managed.rglob("*.*")) == []


def test_bulk_delete_rolls_back_every_movement_and_file(
    repo: Repo, tmp_path: Path
) -> None:
    first_id = _movement(repo, "Primeiro movimento")
    second_id = _movement(repo, "Segundo movimento")
    service = DocumentService(repo, attachments_directory=tmp_path / "anexos")
    first_source = tmp_path / "first.pdf"
    second_source = tmp_path / "second.pdf"
    first_source.write_bytes(b"%PDF-1.4\nfirst\n%%EOF\n")
    second_source.write_bytes(b"%PDF-1.4\nsecond\n%%EOF\n")
    first_stored = service.attach_file(first_id, first_source)
    second_stored = service.attach_file(second_id, second_source)
    repo.db.conn.execute(
        f"""
        CREATE TRIGGER reject_second_meta_delete
        BEFORE DELETE ON transaction_meta
        WHEN OLD.transaction_id={second_id}
        BEGIN
            SELECT RAISE(ABORT, 'simulated bulk delete failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="bulk delete failure"):
        service.delete_transactions([first_id, second_id])

    assert repo.transaction_by_id(first_id) is not None
    assert repo.transaction_by_id(second_id) is not None
    assert first_stored.exists() and second_stored.exists()

    repo.db.conn.execute("DROP TRIGGER reject_second_meta_delete")
    repo.db.conn.commit()
    removed = service.delete_transactions([first_id, second_id])

    assert removed == 2
    assert repo.transaction_by_id(first_id) is None
    assert repo.transaction_by_id(second_id) is None
    assert not first_stored.exists() and not second_stored.exists()


@pytest.mark.parametrize("operation", ["tags", "reviewed"])
def test_metadata_bulk_actions_roll_back_as_a_group(repo: Repo, operation: str) -> None:
    first_id = _movement(repo, "Primeiro lote")
    second_id = _movement(repo, "Segundo lote")
    before_first = repo.transaction_meta_ext(first_id)
    before_second = repo.transaction_meta_ext(second_id)
    repo.db.conn.execute(
        f"""
        CREATE TRIGGER reject_second_meta_update
        BEFORE UPDATE ON transaction_meta
        WHEN OLD.transaction_id={second_id}
        BEGIN
            SELECT RAISE(ABORT, 'simulated batch metadata failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="batch metadata failure"):
        if operation == "tags":
            repo.add_tags_to_transactions([first_id, second_id], "novo")
        else:
            repo.mark_transactions_reviewed([first_id, second_id])

    assert repo.transaction_meta_ext(first_id) == before_first
    assert repo.transaction_meta_ext(second_id) == before_second
