from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from database.engine import FinanceDB
from services.ai import AIService, GeminiClient
from services.backup import BackupService
from utils.app_logging import redact_log_text


class _Response:
    status = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")
        self._read = False

    def read(self, _size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._payload

    def getheader(self, _name: str) -> None:
        return None


class _Connection:
    sock = None

    def __init__(self) -> None:
        self.request_data: tuple[Any, ...] | None = None

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.request_data = (method, path, body, dict(headers or {}))

    def getresponse(self) -> _Response:
        return _Response({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    def close(self) -> None:
        return None


class _SecretStore:
    def __init__(self, value: str) -> None:
        self.value = value

    def read(self) -> str:
        return self.value

    def write(self, value: str) -> None:
        self.value = value


class _AIRepo:
    def __init__(self) -> None:
        self.saved: dict[str, Any] = {}

    def ai_settings(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "use_env": False,
            "model": "gemini-test",
            "privacy_aggregated": True,
            "anonymize": True,
            "ask_before_details": True,
            "legacy_api_key": "",
            "secret_migration_done": True,
        }

    def clear_legacy_ai_api_key(self) -> None:
        return None

    def mark_ai_secret_migrated(self) -> None:
        return None

    def save_ai_settings(self, **values: Any) -> None:
        self.saved = values


class _BackupStub:
    def scrub_ai_secret_from_backups(self) -> dict[str, int]:
        return {"failed": 0}


def test_gemini_secret_is_sent_only_in_the_required_header() -> None:
    connection = _Connection()
    client = GeminiClient(
        "secret-api-key",
        "gemini-test",
        connection_factory=lambda _host, _timeout: connection,
        max_attempts=1,
    )

    assert client.generate("teste") == "ok"
    assert connection.request_data is not None
    _method, path, _body, headers = connection.request_data
    assert "secret-api-key" not in path
    assert "?key=" not in path
    assert headers["x-goog-api-key"] == "secret-api-key"


def test_public_ai_preferences_never_return_the_plaintext_secret() -> None:
    secret = "gemini-super-secret-value"
    repo = _AIRepo()
    store = _SecretStore(secret)
    service = AIService(
        repo,  # type: ignore[arg-type]
        secret_store=store,  # type: ignore[arg-type]
        backup_service=_BackupStub(),  # type: ignore[arg-type]
    )

    preferences = service.preferences()
    assert preferences["has_api_key"] is True
    assert preferences["key_hint"] == "•••• alue"
    assert secret not in json.dumps(preferences, ensure_ascii=False)
    service.save_preferences(
        enabled=True,
        use_env=False,
        api_key="",
        model="gemini-test",
        privacy_aggregated=True,
        anonymize=True,
        ask_before_details=True,
    )
    assert store.value == secret
    assert service._client().api_key == secret


def test_logs_redact_keys_email_and_iban() -> None:
    message = (
        "GET /v1?key=segredo123&x=1 x-goog-api-key: outra-chave "
        "renato@example.com PT50000201231234567890154"
    )
    redacted = redact_log_text(message)
    assert "segredo123" not in redacted
    assert "outra-chave" not in redacted
    assert "renato@example.com" not in redacted
    assert "PT50000201231234567890154" not in redacted
    assert "[REDACTED]" in redacted
    assert "[EMAIL]" in redacted
    assert "[IBAN]" in redacted


def test_manual_backup_worker_uses_an_independent_database_connection(
    tmp_path: Path,
) -> None:
    database = FinanceDB(tmp_path / "worker.sqlite3")
    try:
        target = BackupService.run_manual_backup_for_database(database.path)
        assert target.is_file()
        manifest = BackupService.validate_complete_archive(target)
        assert set(manifest.get("included_scopes") or ()) >= {
            "database",
            "attachments",
            "history",
            "preferences",
        }
    finally:
        database.conn.close()


def test_release_build_embeds_version_and_creates_checksums() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = (root / "LiquidGlass.spec").read_text(encoding="utf-8")
    build = (root / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    assert "version='build/version_info.txt'" in spec
    assert "RELEASE_MANIFEST.json" in build
    assert "sha256.txt" in build
    assert "LIQUIDGLASS_SIGN_CERT_THUMBPRINT" in build
