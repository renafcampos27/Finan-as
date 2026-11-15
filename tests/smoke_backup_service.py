#!/usr/bin/env python3
"""Offline smoke checks for backup orchestration."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB
from services.backup import BackupService


def create_database(path: Path, marker: str, secret: str = "") -> None:
    # A legacy envelope still needs an application database that can reopen.
    with patch.dict(os.environ, {"FINANCAS_PESSOAIS_DEMO_DATA": "0"}):
        database = FinanceDB(path)
    try:
        database.conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('marker', ?)",
            (marker,),
        )
        if secret:
            database.conn.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('ai_gemini_api_key',?)",
                (secret,),
            )
        database.conn.commit()
    finally:
        database.conn.close()


def setting_value(path: Path, key: str) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
        ).fetchone()
        return str(row[0]) if row else ""
    finally:
        connection.close()


def bundle_setting_value(path: Path, key: str) -> str:
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "financas_pessoais.sqlite3"
        with zipfile.ZipFile(path, "r") as archive:
            database.write_bytes(archive.read("financas_pessoais.sqlite3"))
        return setting_value(database, key)


class FakeDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)

    def backup_to(self, target: Path) -> None:
        self.conn.commit()
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(target)
        try:
            self.conn.backup(destination)
            destination.commit()
        finally:
            destination.close()

    def export_full_zip(self, target: Path) -> None:
        temporary = target.with_suffix(".source.sqlite3")
        self.backup_to(temporary)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            settings = [
                dict(row) for row in connection.execute("SELECT * FROM settings")
            ]
        finally:
            connection.close()
        payload = {"tables": {"settings": settings}}
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(temporary, "financas_pessoais.sqlite3")
            archive.writestr("dados_exportados.json", json.dumps(payload))
        temporary.unlink()


class FakeRepo:
    def __init__(self, database_path: Path):
        self.db = FakeDatabase(database_path)
        self.settings: dict[str, str] = {}

    def setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = str(value)


def test_restore_rejects_unrelated_sqlite_before_replacing_active_database(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active.sqlite3"
    invalid = tmp_path / "invalid.sqlite3"
    create_database(active, "original")
    connection = sqlite3.connect(invalid)
    connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    repo = FakeRepo(active)
    service = BackupService(
        repo,
        backup_directory=tmp_path / "backups",
        database_file=active,
        preferences_file=tmp_path / "preferences.json",
        attachments_directory=tmp_path / "anexos",
    )

    with pytest.raises(ValueError, match="faltam as tabelas"):
        service.restore_backup(invalid)

    assert setting_value(active, "marker") == "original"
    assert repo.db.conn.execute("SELECT 1").fetchone()[0] == 1
    repo.db.conn.close()


def test_zip_restore_recovers_database_and_attachments(tmp_path: Path) -> None:
    active = tmp_path / "active.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    archive_path = tmp_path / "replacement.zip"
    attachments = tmp_path / "anexos"
    create_database(active, "original")
    create_database(replacement, "replacement")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(replacement, "financas_pessoais.sqlite3")
        archive.writestr("anexos/2026/fatura.txt", "documento restaurado")
    repo = FakeRepo(active)
    service = BackupService(
        repo,
        backup_directory=tmp_path / "backups",
        database_file=active,
        preferences_file=tmp_path / "preferences.json",
        attachments_directory=attachments,
    )

    safety = service.restore_backup(archive_path)

    assert safety.exists()
    assert setting_value(active, "marker") == "replacement"
    assert (attachments / "2026" / "fatura.txt").read_text(encoding="utf-8") == (
        "documento restaurado"
    )


def test_restore_rolls_back_database_when_attachment_restore_fails(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    archive_path = tmp_path / "replacement.zip"
    create_database(active, "original")
    create_database(replacement, "replacement")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(replacement, "financas_pessoais.sqlite3")
        archive.writestr("anexos/falha.txt", "conteúdo")
    repo = FakeRepo(active)
    service = BackupService(
        repo,
        backup_directory=tmp_path / "backups",
        database_file=active,
        preferences_file=tmp_path / "preferences.json",
        attachments_directory=tmp_path / "anexos",
    )

    original_replace = Path.replace
    failed = False

    def fail_attachment_install(path: Path, target: Path):
        nonlocal failed
        if (
            not failed
            and Path(target) == service.attachments_directory
            and path.name.endswith(".candidate")
        ):
            assert setting_value(active, "marker") == "replacement"
            failed = True
            raise OSError("simulated attachment failure")
        return original_replace(path, target)

    with patch.object(Path, "replace", fail_attachment_install):
        with pytest.raises(OSError, match="simulated attachment failure"):
            service.restore_backup(archive_path)

    assert setting_value(active, "marker") == "original"
    assert failed


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        active = root / "active.sqlite3"
        backup_directory = root / "backups"
        preferences_file = root / "backup_preferences.json"
        create_database(active, "original", "plaintext-secret")
        repo = FakeRepo(active)
        service = BackupService(
            repo,
            backup_directory=backup_directory,
            database_file=active,
            preferences_file=preferences_file,
            attachments_directory=root / "anexos",
        )

        manual = service.create_manual_backup()
        assert manual.exists()
        assert manual.suffix == ".zip"
        assert bundle_setting_value(manual, "marker") == "original"
        assert bundle_setting_value(manual, "ai_gemini_api_key") == ""

        exported = root / "export.zip"
        service.export_backup_zip(exported)
        assert exported.exists()
        assert BackupService.scrub_ai_secret_from_zip_file(exported) is False

        service.save_preferences(15, 2, True)
        preferences = service.preferences()
        assert {
            key: preferences[key]
            for key in (
                "auto_backup_interval_minutes",
                "backup_keep",
                "backup_on_close",
                "backup_encryption_enabled",
            )
        } == {
            "auto_backup_interval_minutes": 15,
            "backup_keep": 2,
            "backup_on_close": True,
            "backup_encryption_enabled": False,
        }
        assert "backup_interval_minutes" not in repo.settings
        assert "auto_backup_days" not in repo.settings
        assert "last_manual_backup_file" not in repo.settings
        assert preferences_file.exists()

        repo.set_setting(
            "last_auto_backup_dt",
            (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),
        )
        automatic = service.run_auto_backup_if_due(silent=False)
        assert automatic is not None and automatic.exists()

        for family in ("auto", "ao_fechar"):
            for index in range(4):
                candidate = (
                    backup_directory
                    / f"financas_backup_{family}_2026010{index + 1}_010101.zip"
                )
                shutil.copy2(manual, candidate)
                timestamp = 1_700_000_000 + index
                os.utime(candidate, (timestamp, timestamp))
        service.prune_backups()
        assert manual.exists()
        assert len(list(backup_directory.glob("financas_backup_auto_*.zip"))) == 2
        assert len(list(backup_directory.glob("financas_backup_ao_fechar_*.zip"))) == 2

        # A second synthetic profile owns a separate data directory; the
        # active profile already has backup/recovery evidence by this point.
        replacement = root / "replacement" / "replacement.sqlite3"
        create_database(replacement, "replacement")
        safety = service.restore_backup(replacement)
        assert safety.exists()
        assert setting_value(active, "marker") == "replacement"
        assert setting_value(safety, "ai_gemini_api_key") == ""
        repo.db.conn.close()

    print("Backup service smoke OK")
    print("manual_backup_sanitized", True)
    print("zip_backup_sanitized", True)
    print("preferences_roundtrip", True)
    print("retention_per_automatic_family", 2)
    print("manual_backup_retained", True)
    print("restore_completed", True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
