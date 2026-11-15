"""Unit and regression tests for D4, D5, E1, and E2 audit defects.

D4: Gemini API credential source resolution and isolation (no silent fallback).
D5: Outbound AI payload sanitization for custom labels (categories, goals, payments).
E1: Version-upgrade protection snapshots use the active LGBK2 session.
E2: LGBK2 decryption guards against oversized payloads and disk exhaustion.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from database.protection import (
    DataProtectionError,
    DataProtectionGuard,
)
from services.ai import AIService
from services.backup_crypto import (
    BackupCryptoCodec,
    BackupCryptoError,
    CryptoFormatError,
    InsufficientStorageError,
    LGBK2_MAGIC,
    LGBK2_MAX_HEADER_BYTES,
    LGBK2_MAX_PAYLOAD_SIZE,
    build_envelope,
    write_envelope_atomically,
)
from services.secret_store import SecretStoreError
from services.startup_security import (
    SessionSecurityContext,
    clear_active_session,
)

# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------


class _FakeSecretStore:
    def __init__(self, initial_value: str = ""):
        self.value = initial_value
        self.read_count = 0
        self.write_count = 0

    def read(self) -> str:
        self.read_count += 1
        return self.value

    def write(self, val: str) -> None:
        self.write_count += 1
        self.value = val

    def delete(self) -> None:
        self.value = ""


class _FakeRepo:
    def __init__(self, settings: dict[str, Any] | None = None):
        self._settings = settings or {
            "enabled": True,
            "use_env": False,
            "model": "gemini-2.5-flash",
            "privacy_aggregated": True,
            "anonymize": True,
            "ask_before_details": False,
            "legacy_api_key": "",
        }

    def ai_settings(self) -> dict[str, Any]:
        return dict(self._settings)

    def save_ai_settings(self, **kwargs: Any) -> None:
        self._settings.update(kwargs)

    def mark_ai_secret_migrated(self) -> None:
        pass


@pytest.fixture()
def master_key() -> bytearray:
    return bytearray(range(32))


@pytest.fixture()
def security_session(tmp_path: Path, master_key: bytearray) -> SessionSecurityContext:
    envelope_path = tmp_path / "security_envelope.json"
    envelope = build_envelope(
        profile_id="test-profile-d4d5e1e2",
        default_unlock_method="password",
        master_key=master_key,
        credentials={"password": b"TestPassword123!"},
    )
    write_envelope_atomically(envelope_path, envelope)
    ctx = SessionSecurityContext(
        profile_id="test-profile-d4d5e1e2",
        credential_kind="password",
        default_unlock_method="password",
        backup_master_key=bytearray(master_key),
        envelope_path=envelope_path,
    )
    return ctx


# ---------------------------------------------------------------------------
# D4: Credential source resolution tests
# ---------------------------------------------------------------------------


def test_d4_environment_mode_missing_key_does_not_fallback_to_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If environment mode is selected and GEMINI_API_KEY is empty, fail without using stored key."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    fake_store = _FakeSecretStore(initial_value="AIzaSyStoredKeyThatMustNotBeUsed123")
    repo = _FakeRepo({"enabled": True, "use_env": True})
    service = AIService(repo=repo, secret_store=fake_store)

    prefs = service.preferences()
    assert prefs["has_api_key"] is False
    assert prefs["key_source"] == "none"
    assert prefs["env_key_present"] is False

    with pytest.raises(RuntimeError, match="variável de ambiente GEMINI_API_KEY não está definida"):
        service._client()


def test_d4_environment_mode_both_keys_present_selects_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both env and stored keys exist, environment mode strictly uses the env key."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyEnvironmentKeyActive999")
    fake_store = _FakeSecretStore(initial_value="AIzaSyStoredKeyInactive888")
    repo = _FakeRepo({"enabled": True, "use_env": True})
    service = AIService(repo=repo, secret_store=fake_store)

    prefs = service.preferences()
    assert prefs["has_api_key"] is True
    assert prefs["key_source"] == "environment"
    assert prefs["key_hint"] == "•••• e999"

    client = service._client()
    assert client.api_key == "AIzaSyEnvironmentKeyActive999"


def test_d4_stored_mode_selects_stored_key_ignoring_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stored mode is selected, the stored key is used even if env key exists."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyEnvironmentKeyIgnored")
    fake_store = _FakeSecretStore(initial_value="AIzaSyStoredKeySelected777")
    repo = _FakeRepo({"enabled": True, "use_env": False})
    service = AIService(repo=repo, secret_store=fake_store)

    prefs = service.preferences()
    assert prefs["has_api_key"] is True
    assert prefs["key_source"] == "windows"
    assert prefs["key_hint"] == "•••• d777"

    client = service._client()
    assert client.api_key == "AIzaSyStoredKeySelected777"


def test_d4_stored_mode_missing_key_does_not_fallback_to_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If stored mode is selected and store is empty, fail without using env key."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyEnvironmentKeyDoNotUse")
    fake_store = _FakeSecretStore(initial_value="")
    repo = _FakeRepo({"enabled": True, "use_env": False})
    service = AIService(repo=repo, secret_store=fake_store)

    prefs = service.preferences()
    assert prefs["has_api_key"] is False
    assert prefs["key_source"] == "none"

    with pytest.raises(RuntimeError, match="Configura a chave API Gemini"):
        service._client()


# ---------------------------------------------------------------------------
# D5: Outbound AI payload label sanitization
# ---------------------------------------------------------------------------


def test_d5_sanitize_transaction_rows_redacts_identifiers_in_categories_and_descriptions() -> None:
    """Categories and subcategories with recognized identifiers must be sanitized in rows."""
    raw_rows = [
        {
            "id": 101,
            "date": "2026-09-01",
            "amount": -45.50,
            "category": "Despesas user@empresa.pt",
            "subcategory": "Sub PT50000011112222333344445",
            "description": "Pagamento NIF 123456789 com MB WAY DE JOAO SILVA",
        },
        {
            "id": 102,
            "date": "2026-09-02",
            "amount": -12.00,
            "category": "Alimentação Normal",
            "subcategory": "Supermercado",
            "description": "Compras normais do mês",
        },
    ]

    sanitized = AIService.sanitize_transaction_rows(raw_rows)
    assert len(sanitized) == 2

    row1 = sanitized[0]
    assert "user@empresa.pt" not in row1["categoria"]
    assert "[email oculto]" in row1["categoria"]
    assert "PT50000011112222333344445" not in row1["subcategoria"]
    assert "[IBAN oculto]" in row1["subcategoria"]
    assert "123456789" not in row1["descricao"]
    assert "[NIF oculto]" in row1["descricao"]
    assert "JOAO SILVA" not in row1["descricao"]
    assert row1["valor"] == -45.50
    assert row1["id"] == 101

    row2 = sanitized[1]
    assert row2["categoria"] == "Alimentação Normal"
    assert row2["subcategoria"] == "Supermercado"
    assert row2["descricao"] == "Compras normais do mês"


def test_d5_month_context_redacts_identifiers_when_anonymize_true() -> None:
    """month_context must redact identifiers in goals, categories, payment methods when anonymize=True."""
    repo = MagicMock()
    repo.month_period.return_value = ("2026-09-01", "2026-09-30", "Setembro 2026")
    repo.totals_for_payment_period.return_value = {"rendimentos": 2000.0, "despesas": 1000.0}
    repo.month_target.return_value = {"target": 1200.0}
    repo.top_categories_payment_period.return_value = [
        ("Serviços NIF 500123456", 250.0, "#ff0000"),
        ("Alimentação Normal", 400.0, "#00ff00"),
    ]
    repo.monthly_goals.return_value = [
        {
            "name": "Poupança test@dominio.com",
            "category": "Investimento PT50000011112222333344445",
            "goal_type": "saving",
            "target_value": 500.0,
        }
    ]
    repo.transaction_data_quality_counts.return_value = {
        "uncategorized_or_generic": 0,
        "expenses_without_attachment": 0,
        "invalid_dates": 0,
        "invalid_amounts": 0,
    }
    repo.transaction_context_sample.return_value = [
        {
            "date": "2026-09-03",
            "amount": -30.0,
            "category": "Saúde 912345678",
            "subcategory": "Farmácia",
            "description": "Medicamentos",
        }
    ]

    analytics = MagicMock()
    analytics.payment_method_totals.return_value = [
        ("Cartão Visa **** **** **** 4321", 300.0, "#333333"),
        ("Transferência MB WAY DE PEDRO SANTOS", 150.0, "#444444"),
    ]
    analytics.monthly_goal_progress.return_value = (50.0, 250.0, "on_track")

    payments = MagicMock()
    payments.likely_recurring_expenses.return_value = [
        {
            "category": "Subscrição contacto@servico.com",
            "amount": 15.99,
            "c": 6,
            "first_date": "2026-04-01",
            "last_date": "2026-09-01",
            "description": "Mensalidade NISS 12345678901",
        }
    ]

    service = AIService(
        repo=repo,
        analytics_service=analytics,
        payments_service=payments,
        secret_store=_FakeSecretStore(),
        backup_service=object(),
    )

    context = service.month_context("2026-09", anonymize=True, include_descriptions=True)

    # Check monthly goals
    goal = context["metas_mensais"][0]
    assert "test@dominio.com" not in goal["nome"]
    assert "[email oculto]" in goal["nome"]
    assert "PT50000011112222333344445" not in goal["categoria"]
    assert "[IBAN oculto]" in goal["categoria"]

    # Check top categories
    top_cat = context["categorias_top"][0]["categoria"]
    assert "500123456" not in top_cat
    assert "[NIF oculto]" in top_cat
    assert context["categorias_top"][1]["categoria"] == "Alimentação Normal"

    # Check payment methods
    pm1 = context["formas_de_pagamento"][0]["forma"]
    assert "4321" not in pm1
    assert "[cartão oculto]" in pm1
    pm2 = context["formas_de_pagamento"][1]["forma"]
    assert "PEDRO SANTOS" not in pm2
    assert "[contraparte oculta]" in pm2

    # Check recurring
    rec = context["possiveis_recorrentes"][0]
    assert "contacto@servico.com" not in rec["categoria"]
    assert "[email oculto]" in rec["categoria"]
    assert "12345678901" not in rec["descricao"]
    assert "[NISS oculto]" in rec["descricao"]

    # Check sample movements
    sample_mov = context["movimentos_amostra"][0]
    assert "912345678" not in sample_mov["categoria"]
    assert "[contacto oculto]" in sample_mov["categoria"]


# ---------------------------------------------------------------------------
# E1: Version-upgrade protection snapshots under active LGBK2 session
# ---------------------------------------------------------------------------


def _setup_test_db(db_path: Path) -> sqlite3.Connection:
    # A protection snapshot must contain the real supported financial schema.
    # A two-table mock no longer satisfies restore compatibility validation.
    database = FinanceDB(db_path, app_version="3.49.0")
    repo = Repo(database)
    repo.add_transaction("2026-09-01", "Synthetic snapshot marker", 100.0, "Rendimento sintético")
    database.close()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO settings VALUES ('backup_encryption_enabled', '1');"
    )
    conn.commit()
    return conn


def test_e1_upgrade_snapshot_uses_active_lgbk2_session_without_legacy_password(
    tmp_path: Path,
    security_session: SessionSecurityContext,
) -> None:
    """When an active LGBK2 session exists, upgrade snapshot encrypts with LGBK2 without legacy password."""
    db_path = tmp_path / "data" / "financas_pessoais.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _setup_test_db(db_path)

    guard = DataProtectionGuard(
        db_path,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        required_schema_versions=FinanceDB.REQUIRED_SCHEMA_VERSIONS,
        current_app_version="3.50.0",
        protection_directory=tmp_path / "protection",
        session_context=security_session,
    )

    snapshot_path = guard.create_snapshot(
        conn,
        source_app_version="3.49.0",
        source_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        tables=guard.capture_tables(conn),
        files=[],
        target_path=None,
        purpose="version_upgrade",
    )

    assert snapshot_path.is_file()
    assert snapshot_path.suffix == ".lgbak"

    with snapshot_path.open("rb") as handle:
        magic = handle.read(len(LGBK2_MAGIC))
    assert magic == LGBK2_MAGIC

    # Validating the archive must succeed using the active session
    manifest = guard.validate_protection_archive(snapshot_path)
    assert manifest["source_app_version"] == "3.49.0"

    conn.close()


def test_e1_upgrade_snapshot_restores_cleanly_under_lgbk2(
    tmp_path: Path,
    security_session: SessionSecurityContext,
) -> None:
    """An LGBK2-encrypted upgrade snapshot must restore cleanly under the active session."""
    db_path = tmp_path / "data" / "financas_pessoais.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _setup_test_db(db_path)

    guard = DataProtectionGuard(
        db_path,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        required_schema_versions=FinanceDB.REQUIRED_SCHEMA_VERSIONS,
        current_app_version="3.50.0",
        protection_directory=tmp_path / "protection",
        session_context=security_session,
    )

    snapshot_path = guard.create_snapshot(
        conn,
        source_app_version="3.49.0",
        source_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        tables=guard.capture_tables(conn),
        files=[],
        target_path=None,
        purpose="version_upgrade",
    )
    conn.close()

    # Simulate database deletion
    db_path.unlink()
    assert not db_path.exists()

    # Restore from the snapshot
    guard.restore_archive(snapshot_path)
    assert db_path.is_file()

    # Verify restored data
    restored_conn = sqlite3.connect(str(db_path))
    count = restored_conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE description='Synthetic snapshot marker' AND amount=100.0"
    ).fetchone()[0]
    restored_conn.close()
    assert count == 1


def test_e1_lgbk2_snapshot_fails_closed_without_session(
    tmp_path: Path,
    security_session: SessionSecurityContext,
) -> None:
    """Reading an LGBK2 snapshot without an active session raises DataProtectionError."""
    db_path = tmp_path / "data" / "financas_pessoais.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _setup_test_db(db_path)

    guard = DataProtectionGuard(
        db_path,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        required_schema_versions=FinanceDB.REQUIRED_SCHEMA_VERSIONS,
        current_app_version="3.50.0",
        protection_directory=tmp_path / "protection",
        session_context=security_session,
    )

    snapshot_path = guard.create_snapshot(
        conn,
        source_app_version="3.49.0",
        source_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        tables={},
        files=[],
        target_path=None,
        purpose="version_upgrade",
    )
    conn.close()

    # Create a guard without session_context and clear global session
    clear_active_session()
    unauthenticated_guard = DataProtectionGuard(
        db_path,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        required_schema_versions=FinanceDB.REQUIRED_SCHEMA_VERSIONS,
        current_app_version="3.50.0",
        protection_directory=tmp_path / "protection",
        session_context=None,
    )

    with pytest.raises(DataProtectionError, match="não há sessão de segurança ativa"):
        unauthenticated_guard.validate_protection_archive(snapshot_path)


def test_e1_financedb_forwards_session_context(
    tmp_path: Path,
    security_session: SessionSecurityContext,
) -> None:
    """FinanceDB.__init__ accepts session_context and wires it to DataProtectionGuard."""
    db_path = tmp_path / "data" / "financas_pessoais.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = FinanceDB(
        db_path,
        session_context=security_session,
    )
    try:
        assert db.conn is not None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# E2: LGBK2 Decryption Payload Size and Storage Guards
# ---------------------------------------------------------------------------


def test_e2_insufficient_storage_error_is_backup_crypto_error() -> None:
    """InsufficientStorageError must inherit from BackupCryptoError."""
    assert issubclass(InsufficientStorageError, BackupCryptoError)
    assert LGBK2_MAX_PAYLOAD_SIZE == 5 * 1024**3


def test_e2_payload_size_exceeding_limit_raises_crypto_format_error(
    tmp_path: Path,
    security_session: SessionSecurityContext,
) -> None:
    """If LGBK2 payload size exceeds LGBK2_MAX_PAYLOAD_SIZE (5 GB), reject immediately."""
    payload_file = tmp_path / "small.txt"
    payload_file.write_bytes(b"HELLO WORLD")

    envelope = build_envelope(
        profile_id="p1",
        default_unlock_method="password",
        master_key=security_session.backup_master_key,
        credentials={"password": b"pwd12345"},
    )
    target_archive = tmp_path / "test.lgbak"
    BackupCryptoCodec.encrypt_v2(
        payload_file,
        target_archive,
        master_key=bytes(security_session.backup_master_key),
        envelope=envelope,
        payload_kind="S",
    )

    out_target = tmp_path / "decrypted.txt"
    original_stat = Path.stat

    def mock_stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        real = original_stat(self, *args, **kwargs)
        if self == target_archive:
            fake_size = 6 * 1024**3  # 6 GB
            return os.stat_result(
                (real.st_mode, real.st_ino, real.st_dev, real.st_nlink,
                 real.st_uid, real.st_gid, fake_size, real.st_atime,
                 real.st_mtime, real.st_ctime)
            )
        return real

    with patch.object(Path, "stat", mock_stat):
        with pytest.raises(CryptoFormatError, match="excede o limite seguro de 5 GB"):
            BackupCryptoCodec.decrypt_v2(
                target_archive,
                out_target,
                master_key=bytes(security_session.backup_master_key),
            )

    assert not out_target.exists()


def test_e2_insufficient_disk_space_raises_insufficient_storage_error(
    tmp_path: Path,
    security_session: SessionSecurityContext,
) -> None:
    """When disk space is insufficient, decryption raises InsufficientStorageError before writing."""
    payload_file = tmp_path / "data.txt"
    payload_file.write_bytes(b"Sample payload data for disk space check")

    envelope = build_envelope(
        profile_id="p1",
        default_unlock_method="password",
        master_key=security_session.backup_master_key,
        credentials={"password": b"pwd12345"},
    )
    target_archive = tmp_path / "test.lgbak"
    BackupCryptoCodec.encrypt_v2(
        payload_file,
        target_archive,
        master_key=bytes(security_session.backup_master_key),
        envelope=envelope,
        payload_kind="S",
    )

    out_target = tmp_path / "decrypted.txt"

    # Mock shutil.disk_usage to return 1024 bytes free
    with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(10**9, 10**9, 1024)):
        with pytest.raises(InsufficientStorageError, match="Espaço insuficiente no disco"):
            BackupCryptoCodec.decrypt_v2(
                target_archive,
                out_target,
                master_key=bytes(security_session.backup_master_key),
            )

    assert not out_target.exists()
    assert not (tmp_path / f".{out_target.name}.decrypting").exists()


def test_e2_invalid_tag_cleans_temporary_and_never_publishes_target(
    tmp_path: Path,
    security_session: SessionSecurityContext,
) -> None:
    """Tampered GCM tag raises CryptoFormatError and leaves no staging files."""
    payload_file = tmp_path / "payload.bin"
    payload_file.write_bytes(b"SECRET_DATA" * 100)

    envelope = build_envelope(
        profile_id="p1",
        default_unlock_method="password",
        master_key=security_session.backup_master_key,
        credentials={"password": b"pwd12345"},
    )
    target_archive = tmp_path / "test.lgbak"
    BackupCryptoCodec.encrypt_v2(
        payload_file,
        target_archive,
        master_key=bytes(security_session.backup_master_key),
        envelope=envelope,
        payload_kind="S",
    )

    # Corrupt the last 16 bytes (GCM tag)
    data = bytearray(target_archive.read_bytes())
    data[-5] ^= 0xFF
    target_archive.write_bytes(data)

    out_target = tmp_path / "output.bin"
    staging_file = tmp_path / f".{out_target.name}.decrypting"

    with pytest.raises(CryptoFormatError, match="Credencial incorreta ou backup danificado"):
        BackupCryptoCodec.decrypt_v2(
            target_archive,
            out_target,
            master_key=bytes(security_session.backup_master_key),
        )

    assert not out_target.exists()
    assert not staging_file.exists()


def test_e2_valid_payload_roundtrip_cleans_temporary(
    tmp_path: Path,
    security_session: SessionSecurityContext,
) -> None:
    """Valid encryption and decryption roundtrips payload and removes temporary staging file."""
    payload_file = tmp_path / "valid.bin"
    content = b"Roundtrip validation payload content 12345"
    payload_file.write_bytes(content)

    envelope = build_envelope(
        profile_id="p1",
        default_unlock_method="password",
        master_key=security_session.backup_master_key,
        credentials={"password": b"pwd12345"},
    )
    archive = tmp_path / "roundtrip.lgbak"
    BackupCryptoCodec.encrypt_v2(
        payload_file,
        archive,
        master_key=bytes(security_session.backup_master_key),
        envelope=envelope,
        payload_kind="S",
    )

    decrypted = tmp_path / "decrypted.bin"
    staging_file = tmp_path / f".{decrypted.name}.decrypting"

    kind = BackupCryptoCodec.decrypt_v2(
        archive,
        decrypted,
        master_key=bytes(security_session.backup_master_key),
    )

    assert kind == "S"
    assert decrypted.read_bytes() == content
    assert not staging_file.exists()


def test_d4_whitespace_environment_key_treated_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "   \t\n  ")
    repo = _FakeRepo({"enabled": True, "use_env": True})
    service = AIService(repo=repo, secret_store=_FakeSecretStore("key-in-store"))
    prefs = service.preferences()
    assert prefs["has_api_key"] is False
    assert prefs["key_source"] == "none"
    with pytest.raises(RuntimeError, match="variável de ambiente GEMINI_API_KEY não está definida"):
        service._client()


def test_d5_empty_and_boundary_inputs() -> None:
    assert AIService.sanitize_transaction_rows([]) == []
    single = [{"id": 1, "date": "2026-09-01", "amount": 0.0, "category": "", "subcategory": None, "description": ""}]
    res = AIService.sanitize_transaction_rows(single)
    assert res == [{"id": 1, "data": "2026-09-01", "valor": 0.0}]


def test_e2_empty_file_and_truncated_payload(
    tmp_path: Path,
    security_session: SessionSecurityContext,
) -> None:
    empty_file = tmp_path / "empty.lgbak"
    empty_file.write_bytes(b"")
    target = tmp_path / "out.bin"
    with pytest.raises(CryptoFormatError):
        BackupCryptoCodec.decrypt_v2(
            empty_file,
            target,
            master_key=bytes(security_session.backup_master_key),
        )


def test_d4_save_preferences_requires_gemini_key_when_env_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_preferences with enabled=True and use_env=True raises SecretStoreError if GEMINI_API_KEY is missing."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    service = AIService(repo=_FakeRepo(), secret_store=_FakeSecretStore("key-in-store"))
    with pytest.raises(SecretStoreError, match="A variável GEMINI_API_KEY não está definida"):
        service.save_preferences(
            enabled=True,
            use_env=True,
            api_key="",
            model="gemini-2.5-flash",
            privacy_aggregated=True,
            anonymize=True,
            ask_before_details=False,
        )


def test_d4_save_preferences_allows_env_mode_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """save_preferences allows configuring use_env=True while enabled=False without GEMINI_API_KEY."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    repo = _FakeRepo()
    service = AIService(repo=repo, secret_store=_FakeSecretStore())
    service.save_preferences(
        enabled=False,
        use_env=True,
        api_key="",
        model="gemini-2.5-flash",
        privacy_aggregated=True,
        anonymize=True,
        ask_before_details=False,
    )
    assert repo.ai_settings()["use_env"] is True
    assert repo.ai_settings()["enabled"] is False


def test_d4_save_preferences_requires_key_when_stored_enabled_and_no_store_key() -> None:
    """save_preferences with enabled=True and use_env=False raises SecretStoreError if store and api_key are empty."""
    service = AIService(repo=_FakeRepo(), secret_store=_FakeSecretStore(""))
    with pytest.raises(SecretStoreError, match="Indica uma chave Gemini antes de ativar"):
        service.save_preferences(
            enabled=True,
            use_env=False,
            api_key="",
            model="gemini-2.5-flash",
            privacy_aggregated=True,
            anonymize=True,
            ask_before_details=False,
        )


def test_d5_sanitize_transaction_rows_handles_sqlite3_row_and_portuguese_keys() -> None:
    """sanitize_transaction_rows must handle sqlite3.Row and Portuguese column keys without crashing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE txs (id INT, data TEXT, valor REAL, categoria TEXT, subcategoria TEXT, descricao TEXT)")
    conn.execute("INSERT INTO txs VALUES (1, '2026-09-01', -50.0, 'Email user@test.com', 'NIF 123456789', 'Pagamento')")
    row = conn.execute("SELECT * FROM txs").fetchone()

    res = AIService.sanitize_transaction_rows([row])
    assert len(res) == 1
    assert res[0]["id"] == 1
    assert res[0]["data"] == "2026-09-01"
    assert res[0]["valor"] == -50.0
    assert "[email oculto]" in res[0]["categoria"]
    assert "[NIF oculto]" in res[0]["subcategoria"]
    assert res[0]["descricao"] == "Pagamento"
    conn.close()


def test_d5_month_context_preserves_descriptions_when_anonymize_false() -> None:
    """When anonymize=False, month_context must preserve raw descriptions and labels without redaction."""
    repo = MagicMock()
    repo.month_period.return_value = ("2026-09-01", "2026-09-30", "Setembro 2026")
    repo.totals_for_payment_period.return_value = {"rendimentos": 1000.0, "despesas": 500.0}
    repo.month_target.return_value = {"target": 600.0}
    repo.top_categories_payment_period.return_value = [("Alimentação NIF 123456789", 100.0, "#f00")]
    repo.monthly_goals.return_value = [
        {"name": "Meta user@test.com", "category": "Poupança", "goal_type": "saving", "target_value": 200.0}
    ]
    repo.transaction_data_quality_counts.return_value = {
        "uncategorized_or_generic": 0,
        "expenses_without_attachment": 0,
        "invalid_dates": 0,
        "invalid_amounts": 0,
    }
    repo.transaction_context_sample.return_value = [
        {"date": "2026-09-01", "description": "Raw description with NIF 123456789", "amount": 10.0, "category": "Geral", "subcategory": "Diversos"}
    ]

    analytics = MagicMock()
    analytics.payment_method_totals.return_value = [("Cartão 1234", 100.0, "#00f")]
    analytics.monthly_goal_progress.return_value = (50.0, 100.0, "on_track")

    payments = MagicMock()
    payments.likely_recurring_expenses.return_value = [
        {
            "category": "Subscrição user@test.com",
            "amount": 15.0,
            "c": 3,
            "first_date": "2026-06-01",
            "last_date": "2026-09-01",
            "description": "Recorrente NIF 123456789",
        }
    ]

    service = AIService(repo=repo, analytics_service=analytics, payments_service=payments, secret_store=_FakeSecretStore(), backup_service=object())
    context = service.month_context("2026-09", include_descriptions=True, anonymize=False)

    assert "123456789" in context["categorias_top"][0]["categoria"]
    assert "user@test.com" in context["metas_mensais"][0]["nome"]
    assert "123456789" in context["movimentos_amostra"][0]["descricao"]
    assert "123456789" in context["possiveis_recorrentes"][0]["descricao"]


def test_e1_active_lgbk2_session_ignores_closed_session(tmp_path: Path, security_session: SessionSecurityContext) -> None:
    """_active_lgbk2_session must return None if the session is closed / zeroized."""
    guard = DataProtectionGuard(tmp_path / "db.sqlite3", session_context=security_session)
    assert guard._active_lgbk2_session() is security_session
    security_session.close()
    assert guard._active_lgbk2_session() is None


def test_e1_create_snapshot_fails_closed_when_envelope_missing(tmp_path: Path, security_session: SessionSecurityContext) -> None:
    """create_snapshot raises DataProtectionError if the active session envelope file is missing."""
    db_path = tmp_path / "data" / "financas_pessoais.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _setup_test_db(db_path)

    # Delete envelope file
    security_session.envelope_path.unlink()

    guard = DataProtectionGuard(
        db_path,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        required_schema_versions=FinanceDB.REQUIRED_SCHEMA_VERSIONS,
        current_app_version="3.50.0",
        protection_directory=tmp_path / "protection",
        session_context=security_session,
    )

    with pytest.raises(DataProtectionError, match="Envelope de segurança indisponível"):
        guard.create_snapshot(
            conn,
            source_app_version="3.49.0",
            source_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
            tables={},
            files=[],
            purpose="version_upgrade",
        )
    conn.close()


def test_e1_readable_archive_translates_crypto_errors(tmp_path: Path, security_session: SessionSecurityContext) -> None:
    """_readable_archive must translate BackupCryptoError from corrupted archives into DataProtectionError."""
    db_path = tmp_path / "data" / "financas_pessoais.sqlite3"
    guard = DataProtectionGuard(db_path, session_context=security_session)

    corrupt_archive = tmp_path / "corrupt.lgbak"
    corrupt_archive.write_bytes(LGBK2_MAGIC + b"Z" + b"\x00\x00\x00\x05bad!!")

    with pytest.raises(DataProtectionError, match="Falha ao desencriptar"):
        with guard._readable_archive(corrupt_archive):
            pass


def test_e2_read_header_length_rejects_oversized_header() -> None:
    """_read_header_length raises CryptoFormatError when header length exceeds LGBK2_MAX_HEADER_BYTES."""
    import io
    import struct

    oversized = LGBK2_MAX_HEADER_BYTES + 100
    stream = io.BytesIO(LGBK2_MAGIC + b"S" + struct.pack(">I", oversized))
    with pytest.raises(CryptoFormatError, match="excede o limite seguro"):
        BackupCryptoCodec._read_header_length(stream)


def test_e2_write_failure_cleans_temporary_and_raises_insufficient_storage(
    tmp_path: Path,
    security_session: SessionSecurityContext,
) -> None:
    """If out.write fails with OSError (e.g. disk full), temporary staging is cleaned and InsufficientStorageError raised."""
    payload_file = tmp_path / "data.bin"
    payload_file.write_bytes(b"PAYLOAD" * 500)
    archive = tmp_path / "test.lgbak"
    envelope = build_envelope(
        profile_id="p1",
        default_unlock_method="password",
        master_key=security_session.backup_master_key,
        credentials={"password": b"pwd12345"},
    )
    BackupCryptoCodec.encrypt_v2(
        payload_file,
        archive,
        master_key=bytes(security_session.backup_master_key),
        envelope=envelope,
        payload_kind="S",
    )

    out_target = tmp_path / "failed_out.bin"
    staging_file = tmp_path / f".{out_target.name}.decrypting"

    orig_open = Path.open

    def mock_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == staging_file.name:
            m = MagicMock()
            m.__enter__.return_value = m
            m.__exit__.return_value = None
            m.write.side_effect = OSError("No space left on device")
            return m
        return orig_open(self, *args, **kwargs)

    with patch.object(Path, "open", mock_open):
        with pytest.raises(InsufficientStorageError, match="Erro de E/S ou disco cheio"):
            BackupCryptoCodec.decrypt_v2(
                archive,
                out_target,
                master_key=bytes(security_session.backup_master_key),
            )

    assert not out_target.exists()
    assert not staging_file.exists()


def test_syntax_and_lint_modified_files(tmp_path: Path) -> None:
    """Verify that all modified files compile and pass ruff check without errors."""
    import py_compile
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    files = [
        root / "services" / "ai.py",
        root / "database" / "protection.py",
        root / "database" / "engine.py",
        root / "services" / "backup_crypto.py",
        root / "tests" / "test_audit_d4_d5_e1_e2_defects.py",
    ]

    for f in files:
        assert f.is_file(), f"File missing: {f}"
        py_compile.compile(str(f), cfile=str(tmp_path / (f.stem + ".pyc")), doraise=True)

    # If ruff is installed in python env, invoke it
    try:
        res = subprocess.run(
            [sys.executable, "-m", "ruff", "check", *(str(f) for f in files)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, f"Ruff check failed:\n{res.stdout}\n{res.stderr}"
    except FileNotFoundError:
        pass


