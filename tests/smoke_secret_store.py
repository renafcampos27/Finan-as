from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

STAGING_ROOT = Path(__file__).resolve().parents[1]
if str(STAGING_ROOT) not in sys.path:
    sys.path.insert(0, str(STAGING_ROOT))

from services.ai import AIService
from services.backup import BackupService
from services.secret_store import SecretStoreError


class MemorySecretStore:
    def __init__(self, value: str = ""):
        self.value = value

    def read(self) -> str:
        return self.value

    def write(self, secret: str) -> None:
        self.value = str(secret or "")

    def delete(self) -> None:
        self.value = ""


class FailingSecretStore(MemorySecretStore):
    def read(self) -> str:
        raise SecretStoreError("cofre indisponivel")

    def write(self, secret: str) -> None:
        raise SecretStoreError("cofre indisponivel")


class FakeRepo:
    def __init__(self, legacy_key: str = ""):
        self.legacy_key = legacy_key
        self.migration_done = False
        self.saved: dict[str, object] = {}

    def ai_settings(self) -> dict[str, object]:
        return {
            "enabled": True,
            "use_env": False,
            "legacy_api_key": self.legacy_key,
            "secret_migration_done": self.migration_done,
            "model": "gemini-test",
            "privacy_aggregated": True,
            "anonymize": True,
            "ask_before_details": True,
        }

    def clear_legacy_ai_api_key(self) -> None:
        self.legacy_key = ""

    def mark_ai_secret_migrated(self) -> None:
        self.migration_done = True

    def save_ai_settings(self, **values: object) -> None:
        self.saved = values


class FakeBackupService:
    def __init__(self) -> None:
        self.scrub_calls = 0

    def scrub_ai_secret_from_backups(
        self, *, cancel_check=None, progress_callback=None
    ) -> dict[str, int]:
        self.scrub_calls += 1
        return {"scrubbed": 1, "failed": 0}


def create_sqlite(path: Path, secret: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            ("ai_gemini_api_key", secret),
        )
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            ("theme", "light"),
        )
        connection.commit()
    finally:
        connection.close()


def sqlite_has_secret(path: Path) -> bool:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT 1 FROM settings WHERE key='ai_gemini_api_key' AND value<>''"
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def test_service_migration() -> None:
    repo = FakeRepo("legacy-secret")
    store = MemorySecretStore()
    backup_service = FakeBackupService()
    service = AIService(
        repo,
        secret_store=store,
        backup_service=backup_service,
    )

    # Reading preferences must not migrate or rewrite backups on the GUI path.
    preferences = service.preferences()
    assert store.value == ""
    assert repo.legacy_key == "legacy-secret"
    assert backup_service.scrub_calls == 0
    assert preferences["migration_pending"] is True
    result = service.migrate_ai_secrets()
    assert result["done"] is True
    assert result["key_migrated"] is True
    preferences = service.preferences()
    assert store.value == "legacy-secret"
    assert repo.legacy_key == ""
    assert repo.migration_done is True
    assert backup_service.scrub_calls == 1
    assert preferences["has_api_key"] is True
    assert preferences["key_hint"] == "•••• cret"
    assert "stored_key" not in preferences
    assert "legacy_api_key" not in preferences

    service.save_preferences(
        enabled=True,
        use_env=False,
        api_key="new-secret",
        model="gemini-new",
        privacy_aggregated=True,
        anonymize=False,
        ask_before_details=True,
    )
    assert store.value == "new-secret"
    assert "api_key" not in repo.saved
    assert repo.saved["model"] == "gemini-new"


def test_conflicting_secure_key_keeps_legacy_key() -> None:
    repo = FakeRepo("legacy-secret")
    store = MemorySecretStore("different-secure-secret")
    backup_service = FakeBackupService()
    service = AIService(
        repo,
        secret_store=store,
        backup_service=backup_service,
    )

    result = service.migrate_ai_secrets()
    assert result["done"] is False
    assert result["failed"] == 1
    preferences = service.preferences()
    assert repo.legacy_key == "legacy-secret"
    assert store.value == "different-secure-secret"
    assert preferences["has_api_key"] is True
    assert "stored_key" not in preferences
    assert preferences["legacy_key_pending"] is True
    assert backup_service.scrub_calls == 0


def test_failed_migration_keeps_legacy_key() -> None:
    repo = FakeRepo("legacy-secret")
    backup_service = FakeBackupService()
    service = AIService(
        repo,
        secret_store=FailingSecretStore(),
        backup_service=backup_service,
    )

    result = service.migrate_ai_secrets()
    assert result["done"] is False
    assert result["failed"] == 1
    preferences = service.preferences()
    assert repo.legacy_key == "legacy-secret"
    assert preferences["has_api_key"] is True
    assert "stored_key" not in preferences
    assert preferences["legacy_key_pending"] is True
    assert backup_service.scrub_calls == 0


def test_backup_scrubbing() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sqlite_path = root / "backup.sqlite3"
        create_sqlite(sqlite_path, "sqlite-secret")
        assert BackupService.scrub_ai_secret_from_sqlite_file(sqlite_path) is True
        assert sqlite_has_secret(sqlite_path) is False

        embedded = root / "embedded.sqlite3"
        create_sqlite(embedded, "zip-secret")
        zip_path = root / "backup.zip"
        payload = {
            "tables": {
                "settings": [
                    {"key": "ai_gemini_api_key", "value": "json-secret"},
                    {"key": "theme", "value": "light"},
                ]
            }
        }
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(embedded, "financas_pessoais.sqlite3")
            archive.writestr("dados.json", json.dumps(payload))

        assert BackupService.scrub_ai_secret_from_zip_file(zip_path) is True
        with zipfile.ZipFile(zip_path, "r") as archive:
            extracted = root / "extracted.sqlite3"
            extracted.write_bytes(archive.read("financas_pessoais.sqlite3"))
            assert sqlite_has_secret(extracted) is False
            cleaned = json.loads(archive.read("dados.json").decode("utf-8"))
            keys = {row["key"] for row in cleaned["tables"]["settings"]}
            assert "ai_gemini_api_key" not in keys
            assert "theme" in keys


if __name__ == "__main__":
    test_service_migration()
    test_conflicting_secure_key_keeps_legacy_key()
    test_failed_migration_keeps_legacy_key()
    test_backup_scrubbing()
    print("smoke_secret_store: OK")
