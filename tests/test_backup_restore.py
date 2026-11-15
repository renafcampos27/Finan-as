from __future__ import annotations

import os

import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from database.engine import FinanceDB
from services.backup import BackupService


def create_database(path: Path, marker: str) -> None:
    # A legacy envelope still needs an application database that can reopen.
    with patch.dict(os.environ, {"FINANCAS_PESSOAIS_DEMO_DATA": "0"}):
        database = FinanceDB(path)
    try:
        database.conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('marker', ?)",
            (marker,),
        )
        database.conn.commit()
    finally:
        database.conn.close()


def setting_value(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM settings WHERE key='marker'"
        ).fetchone()
        return str(row[0]) if row else ""
    finally:
        connection.close()


class DatabaseStub:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)

    def backup_to(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(target)
        try:
            self.conn.backup(destination)
            destination.commit()
        finally:
            destination.close()


class RepoStub:
    def __init__(self, path: Path):
        self.db = DatabaseStub(path)

    def setting(self, _key: str, default: str = "") -> str:
        return default

    def set_setting(self, _key: str, _value: str) -> None:
        return None


def backup_service(tmp_path: Path, active: Path) -> BackupService:
    return BackupService(
        RepoStub(active),
        backup_directory=tmp_path / "backups",
        database_file=active,
        preferences_file=tmp_path / "preferences.json",
        attachments_directory=tmp_path / "anexos",
    )


def test_restore_rejects_unrelated_sqlite_before_replacement(tmp_path: Path) -> None:
    active = tmp_path / "active.sqlite3"
    invalid = tmp_path / "invalid.sqlite3"
    create_database(active, "original")
    connection = sqlite3.connect(invalid)
    connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    service = backup_service(tmp_path, active)

    with pytest.raises(ValueError, match="faltam as tabelas"):
        service.restore_backup(invalid)

    assert setting_value(active) == "original"
    assert service.repo.db.conn.execute("SELECT 1").fetchone()[0] == 1
    service.repo.db.conn.close()


def test_zip_restore_recovers_database_and_attachments(tmp_path: Path) -> None:
    active = tmp_path / "active.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    archive_path = tmp_path / "replacement.zip"
    create_database(active, "original")
    create_database(replacement, "replacement")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(replacement, "financas_pessoais.sqlite3")
        archive.writestr("anexos/2026/fatura.txt", "documento restaurado")
    orphan = tmp_path / "anexos" / "antigo" / "orfao.txt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("não pertence ao backup", encoding="utf-8")
    service = backup_service(tmp_path, active)

    safety = service.restore_backup(archive_path)

    assert safety.exists()
    assert setting_value(active) == "replacement"
    assert (tmp_path / "anexos" / "2026" / "fatura.txt").read_text(
        encoding="utf-8"
    ) == "documento restaurado"
    assert not orphan.exists()


def test_encrypted_backup_restores_sqlite_and_rejects_wrong_password(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    encrypted = tmp_path / "replacement.lgbak"
    create_database(active, "original")
    create_database(replacement, "replacement")
    BackupService.encrypt_file(
        replacement,
        encrypted,
        "palavra-passe-segura",
        payload_kind="S",
    )
    service = backup_service(tmp_path, active)

    with pytest.raises(ValueError, match="Palavra-passe incorreta"):
        service.restore_backup(
            encrypted,
            password="palavra-passe-errada",
        )
    assert setting_value(active) == "original"

    safety = service.restore_backup(
        encrypted,
        password="palavra-passe-segura",
    )
    assert safety.exists()
    assert setting_value(active) == "replacement"


def test_restore_rolls_back_database_when_attachment_copy_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    archive_path = tmp_path / "replacement.zip"
    create_database(active, "original")
    create_database(replacement, "replacement")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(replacement, "financas_pessoais.sqlite3")
        archive.writestr("anexos/falha.txt", "conteúdo")
    service = backup_service(tmp_path, active)

    original_replace = Path.replace
    failed = False

    def fail_attachment_install(path: Path, target: Path):
        nonlocal failed
        if (
            not failed
            and Path(target) == service.attachments_directory
            and path.name.endswith(".candidate")
        ):
            # The shared installer has already replaced the DB at this point;
            # exercising its real filesystem boundary must restore that DB.
            assert setting_value(active) == "replacement"
            failed = True
            raise OSError("simulated attachment failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_attachment_install)
    with pytest.raises(OSError, match="simulated attachment failure"):
        service.restore_backup(archive_path)

    assert setting_value(active) == "original"
    assert failed
