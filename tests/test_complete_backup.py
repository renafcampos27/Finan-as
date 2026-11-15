from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

import database.protection as protection_module
from database.protection import (
    DataIntegrityError,
    DataProtectionGuard,
    validate_zip_safety,
)
from database.engine import FinanceDB
from services.backup import BackupService


def _database(path: Path, marker: str, amount: float = 12.34) -> None:
    with patch.dict(os.environ, {"FINANCAS_PESSOAIS_DEMO_DATA": "0"}):
        database = FinanceDB(path)
    try:
        database.conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('marker',?)", (marker,)
        )
        database.conn.commit()
        database.add_transaction(
            "2026-01-01",
            "Synthetic fixture",
            amount,
            "Rendimentos" if amount >= 0 else "Diversos",
            "Conta principal",
            "income" if amount >= 0 else "expense",
        )
    finally:
        database.conn.close()


class _Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)

    def backup_to(self, target: Path) -> None:
        self.conn.commit()
        destination = sqlite3.connect(target)
        try:
            self.conn.backup(destination)
            destination.commit()
        finally:
            destination.close()


class _Repo:
    def __init__(self, path: Path):
        self.db = _Database(path)

    def setting(self, key: str, default: str = "") -> str:
        row = self.db.conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
        ).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.db.conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (key, str(value)),
        )
        self.db.conn.commit()


class _SecretStore:
    def __init__(self, value: str = ""):
        self.value = value

    def read(self) -> str:
        return self.value

    def write(self, secret: str) -> None:
        self.value = str(secret)

    def delete(self) -> None:
        self.value = ""


def _service(
    tmp_path: Path,
    *,
    secret_store: _SecretStore | None = None,
) -> tuple[BackupService, _Repo]:
    database = tmp_path / "dados" / "financas.sqlite3"
    database.parent.mkdir()
    _database(database, "original")
    repo = _Repo(database)
    attachments = tmp_path / "dados" / "anexos"
    history = tmp_path / "dados" / "backups" / "historico_alteracoes"
    attachments.mkdir(parents=True)
    history.mkdir(parents=True)
    (attachments / "fatura.pdf").write_bytes(b"fatura-original")
    (history / "desfazer.json").write_text("histórico-original", encoding="utf-8")
    preferences = tmp_path / "dados" / "configuracao" / "backup_preferences.json"
    preferences.parent.mkdir(parents=True)
    preferences.write_text(
        json.dumps(
            {
                "auto_backup_interval_minutes": 1440,
                "backup_keep": 10,
                "backup_on_close": "1",
                "backup_encryption_enabled": "0",
            }
        ),
        encoding="utf-8",
    )
    service = BackupService(
        repo,
        database_file=database,
        backup_directory=tmp_path / "dados" / "backups",
        attachments_directory=attachments,
        history_directory=history,
        preferences_file=preferences,
        secret_store=secret_store,
    )
    return service, repo


def _marker(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(
            connection.execute(
                "SELECT value FROM settings WHERE key='marker'"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_complete_backup_and_atomic_restore_include_every_scope(
    tmp_path: Path,
) -> None:
    service, repo = _service(tmp_path)
    original_preferences = service.preferences_file.read_bytes()

    backup = service.create_manual_backup()

    assert backup.suffix == ".zip"
    manifest = DataProtectionGuard.validate_archive(backup)
    assert manifest["purpose"] == "manual_backup"
    assert set(manifest["included_scopes"]) == {
        "database",
        "attachments",
        "history",
        "preferences",
    }
    with zipfile.ZipFile(backup) as archive:
        names = set(archive.namelist())
    assert "anexos/fatura.pdf" in names
    assert "historico_alteracoes/desfazer.json" in names
    assert "configuracao/backup_preferences.json" in names

    repo.db.conn.execute("UPDATE settings SET value='alterado' WHERE key='marker'")
    repo.db.conn.commit()
    (service.attachments_directory / "fatura.pdf").write_bytes(b"alterada")
    (service.attachments_directory / "extra.txt").write_text("remover")
    (service.history_directory / "desfazer.json").write_text("alterado")
    service.preferences_file.write_text('{"alterado": true}', encoding="utf-8")

    safety = service.restore_backup(backup)

    assert safety.suffix == ".zip"
    assert _marker(service.database_file) == "original"
    assert (service.attachments_directory / "fatura.pdf").read_bytes() == (
        b"fatura-original"
    )
    assert not (service.attachments_directory / "extra.txt").exists()
    assert (service.history_directory / "desfazer.json").read_text(
        encoding="utf-8"
    ) == "histórico-original"
    assert service.preferences_file.read_bytes() == original_preferences


def test_secondary_copy_health_and_recovery_drill(tmp_path: Path) -> None:
    service, repo = _service(tmp_path)
    secondary = tmp_path / "disco_externo"
    service.save_preferences(
        1440,
        10,
        True,
        False,
        True,
        str(secondary),
    )

    backup = service.create_manual_backup()
    mirrored = secondary / backup.name

    assert mirrored.is_file()
    assert (
        hashlib.sha256(mirrored.read_bytes()).digest()
        == hashlib.sha256(backup.read_bytes()).digest()
    )
    health = service.protection_health()
    assert health["verified"] is True
    assert health["mirrored"] is True
    assert health["score"] == 85

    result = service.recovery_drill()
    assert Path(result["file"]) == backup
    assert result["tables"]["transactions"] == 1
    assert int(result["files"]) >= 3
    repo.db.conn.close()


def test_encrypted_backup_contains_and_restores_the_complete_bundle(
    tmp_path: Path,
) -> None:
    password = "palavra-passe-forte"
    service, repo = _service(
        tmp_path,
        secret_store=_SecretStore(password),
    )
    service.save_preferences(1440, 10, True, True)

    backup = service.create_manual_backup()

    assert backup.suffix == ".lgbak"
    assert service.encrypted_payload_kind(backup) == "Z"
    manifest = service.validate_backup_file(backup, password=password)
    assert manifest["purpose"] == "manual_backup"

    repo.db.conn.execute("UPDATE settings SET value='alterado' WHERE key='marker'")
    repo.db.conn.commit()
    safety = service.restore_backup(backup, password=password)

    assert safety.suffix == ".lgbak"
    assert service.encrypted_payload_kind(safety) == "Z"
    assert _marker(service.database_file) == "original"


def test_unavailable_secondary_location_keeps_primary_backup(
    tmp_path: Path,
) -> None:
    service, repo = _service(tmp_path)
    unavailable = tmp_path / "destino_indisponivel"
    unavailable.write_text("não é uma pasta", encoding="utf-8")
    service.save_preferences(
        1440,
        10,
        True,
        False,
        True,
        str(unavailable),
    )

    primary = service.create_manual_backup()

    assert primary.is_file()
    assert service.validate_complete_archive(primary)["purpose"] == "manual_backup"
    assert service.preferences()["last_secondary_backup_error"]
    repo.db.conn.close()


def test_v31_protection_archive_without_named_scopes_remains_restorable(
    tmp_path: Path,
) -> None:
    service, repo = _service(tmp_path)
    current = service.create_manual_backup()
    legacy = tmp_path / "protecao_v31.zip"
    with (
        zipfile.ZipFile(current, "r") as source,
        zipfile.ZipFile(
            legacy,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as target,
    ):
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "manifesto_protecao.json":
                manifest = json.loads(payload.decode("utf-8"))
                manifest.pop("included_scopes", None)
                manifest.pop("purpose", None)
                payload = json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
            target.writestr(info, payload)

    assert service.validate_complete_archive(legacy)["format_version"] == 1
    repo.db.conn.execute("UPDATE settings SET value='alterado' WHERE key='marker'")
    repo.db.conn.commit()
    service.restore_backup(legacy)
    assert _marker(service.database_file) == "original"


def test_health_recomputes_hash_and_rejects_same_size_tampering(
    tmp_path: Path,
) -> None:
    service, repo = _service(tmp_path)
    backup = service.create_manual_backup()
    assert service.protection_health()["verified"] is True

    payload = bytearray(backup.read_bytes())
    payload[len(payload) // 2] ^= 0x01
    backup.write_bytes(payload)

    health = service.protection_health()
    assert health["verified"] is False
    assert health["score"] < 40
    assert any("novo backup" in item.casefold() for item in health["recommendations"])
    repo.db.conn.close()


def test_retention_never_deletes_manual_or_pre_restore_backups(
    tmp_path: Path,
) -> None:
    service, repo = _service(tmp_path)
    service.save_preferences(1440, 1, True, False)
    names = [
        "financas_backup_manual_20260101_010101.zip",
        "financas_backup_manual_20260102_010101.zip",
        "antes_de_repor_20260101_010101.zip",
        "financas_backup_auto_20260101_010101.zip",
        "financas_backup_auto_20260102_010101.zip",
        "financas_backup_ao_fechar_20260101_010101.zip",
        "financas_backup_ao_fechar_20260102_010101.zip",
    ]
    for index, name in enumerate(names):
        path = service.backup_directory / name
        path.write_bytes(name.encode("utf-8"))
        os.utime(path, (index + 1, index + 1))

    service.prune_backups()

    remaining = {
        path.name for path in service.backup_directory.iterdir() if path.is_file()
    }
    assert "financas_backup_manual_20260101_010101.zip" in remaining
    assert "financas_backup_manual_20260102_010101.zip" in remaining
    assert "antes_de_repor_20260101_010101.zip" in remaining
    assert "financas_backup_auto_20260102_010101.zip" in remaining
    assert "financas_backup_auto_20260101_010101.zip" not in remaining
    assert "financas_backup_ao_fechar_20260102_010101.zip" in remaining
    assert "financas_backup_ao_fechar_20260101_010101.zip" not in remaining
    repo.db.conn.close()


def test_backup_preferences_roll_back_database_and_json_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo = _service(tmp_path)
    service.save_preferences(1440, 10, True, False)
    before_file = service.preferences_file.read_bytes()
    before_interval = repo.setting("auto_backup_interval_minutes")

    def fail_preferences(_data: dict[str, object]) -> None:
        raise OSError("simulated preferences failure")

    monkeypatch.setattr(service, "_write_preferences_file", fail_preferences)
    with pytest.raises(OSError, match="preferences failure"):
        service.save_preferences(15, 2, False, False)

    assert repo.setting("auto_backup_interval_minutes") == before_interval
    assert service.preferences_file.read_bytes() == before_file
    repo.db.conn.close()


def test_zip_safety_rejects_member_count_and_extreme_compression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    many = tmp_path / "many.zip"
    with zipfile.ZipFile(many, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("one.txt", "1")
        archive.writestr("two.txt", "2")
    monkeypatch.setattr(protection_module, "MAX_ARCHIVE_MEMBERS", 1)
    with (
        zipfile.ZipFile(many, "r") as archive,
        pytest.raises(DataIntegrityError, match="demasiados ficheiros"),
    ):
        validate_zip_safety(archive)

    compressed = tmp_path / "compressed.zip"
    with zipfile.ZipFile(compressed, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.txt", b"0" * (2 * 1024 * 1024))
    monkeypatch.setattr(protection_module, "MAX_ARCHIVE_MEMBERS", 10_000)
    monkeypatch.setattr(protection_module, "MAX_ARCHIVE_COMPRESSION_RATIO", 2)
    with (
        zipfile.ZipFile(compressed, "r") as archive,
        pytest.raises(DataIntegrityError, match="rácio de compressão"),
    ):
        validate_zip_safety(archive)


def test_pending_restore_marker_recovers_every_scope_on_next_start(
    tmp_path: Path,
) -> None:
    service, repo = _service(tmp_path)
    guard, recovery = service._begin_durable_restore()
    marker = guard.restore_marker
    assert marker.is_file()
    assert recovery.is_file()
    repo.db.conn.close()

    connection = sqlite3.connect(service.database_file)
    connection.execute("UPDATE settings SET value='interrompido' WHERE key='marker'")
    connection.commit()
    connection.close()
    (service.attachments_directory / "fatura.pdf").write_bytes(b"interrompida")
    service.preferences_file.write_text('{"interrompido": true}', encoding="utf-8")

    next_start = DataProtectionGuard(
        service.database_file,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        attachments_directory=service.attachments_directory,
        history_directory=service.history_directory,
        preferences_file=service.preferences_file,
        protection_directory=service.protection_directory,
    )
    next_start._recover_incomplete_restore()

    assert _marker(service.database_file) == "original"
    assert (
        service.attachments_directory / "fatura.pdf"
    ).read_bytes() == b"fatura-original"
    assert (
        json.loads(service.preferences_file.read_text(encoding="utf-8"))["backup_keep"]
        == 10
    )
    assert not next_start.restore_marker.exists()
    assert not recovery.exists()
