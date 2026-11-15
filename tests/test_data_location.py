from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from services.data_location import (
    DataLocationError,
    MIGRATION_MANIFEST_NAME,
    ensure_persistent_data_location,
)
from utils import config


def _legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE transactions "
            "(id INTEGER PRIMARY KEY, description TEXT, amount REAL)"
        )
        connection.execute("INSERT INTO settings(key,value) VALUES('owner','renato')")
        connection.execute(
            "INSERT INTO transactions(description,amount) VALUES(?,?)",
            ("Valor decimal", 12.34),
        )
        connection.commit()
    finally:
        connection.close()


def test_legacy_data_is_copied_verified_and_source_is_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "programa"
    legacy.mkdir()
    _legacy_database(legacy / config.DEFAULT_DB_NAME)
    (legacy / "anexos" / "1").mkdir(parents=True)
    (legacy / "anexos" / "1" / "fatura.pdf").write_bytes(b"documento")
    (legacy / "backups").mkdir()
    (legacy / "backups" / "backup_antigo.sqlite3").write_bytes(b"backup")
    (legacy / "utils").mkdir()
    (legacy / "utils" / "backup_preferences.json").write_text(
        '{"backup_keep": 30}',
        encoding="utf-8",
    )
    (legacy / "logs").mkdir()
    (legacy / "logs" / "app.log").write_text("registo antigo", encoding="utf-8")

    state = tmp_path / "perfil"
    monkeypatch.setenv("FINANCAS_PESSOAIS_STATE_DIR", str(state))
    monkeypatch.setattr(config, "DB_NAME", config.DEFAULT_DB_NAME)
    paths = config.paths_for_database()

    result = ensure_persistent_data_location(
        "3.2.0",
        legacy_root=legacy,
        paths=paths,
    )

    assert result.state == "migrated"
    assert (legacy / config.DEFAULT_DB_NAME).is_file()
    assert (legacy / "anexos" / "1" / "fatura.pdf").read_bytes() == b"documento"
    assert (paths.attachments / "1" / "fatura.pdf").read_bytes() == b"documento"
    assert (paths.backups / "backup_antigo.sqlite3").read_bytes() == b"backup"
    assert json.loads(paths.backup_preferences.read_text(encoding="utf-8")) == {
        "backup_keep": 30
    }
    assert (paths.logs / "legado" / "app.log").read_text(
        encoding="utf-8"
    ) == "registo antigo"
    manifest = json.loads(
        (paths.configuration / MIGRATION_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["legacy_kept_intact"] is True
    assert manifest["database"]["tables"]["transactions"] == 1

    connection = sqlite3.connect(paths.database)
    try:
        amount = connection.execute("SELECT amount FROM transactions").fetchone()[0]
    finally:
        connection.close()
    assert amount == pytest.approx(12.34)

    repeated = ensure_persistent_data_location(
        "3.2.0",
        legacy_root=legacy,
        paths=paths,
    )
    assert repeated.state == "ready"


def test_partial_target_blocks_startup_without_touching_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "programa"
    legacy.mkdir()
    source = legacy / config.DEFAULT_DB_NAME
    _legacy_database(source)
    original = source.read_bytes()

    state = tmp_path / "perfil"
    monkeypatch.setenv("FINANCAS_PESSOAIS_STATE_DIR", str(state))
    monkeypatch.setattr(config, "DB_NAME", config.DEFAULT_DB_NAME)
    paths = config.paths_for_database()
    paths.data.mkdir(parents=True)
    (paths.data / "ficheiro_incompleto.txt").write_text("não apagar")

    with pytest.raises(DataLocationError, match="migração parcial"):
        ensure_persistent_data_location(
            "3.2.0",
            legacy_root=legacy,
            paths=paths,
        )

    assert source.read_bytes() == original
    assert (paths.data / "ficheiro_incompleto.txt").read_text() == "não apagar"


def test_fresh_empty_directories_are_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "programa_sem_dados"
    legacy.mkdir()
    monkeypatch.setenv(
        "FINANCAS_PESSOAIS_STATE_DIR",
        str(tmp_path / "perfil_novo"),
    )
    monkeypatch.setattr(config, "DB_NAME", config.DEFAULT_DB_NAME)
    paths = config.paths_for_database()

    first = ensure_persistent_data_location(
        "3.2.0",
        legacy_root=legacy,
        paths=paths,
    )
    second = ensure_persistent_data_location(
        "3.2.0",
        legacy_root=legacy,
        paths=paths,
    )

    assert first.state == "fresh"
    assert second.state == "fresh"
    assert paths.attachments.is_dir()
    assert paths.history.is_dir()


def test_valid_interrupted_migration_is_recovered_without_legacy_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "programa"
    legacy.mkdir()
    _legacy_database(legacy / config.DEFAULT_DB_NAME)
    (legacy / "anexos" / "1").mkdir(parents=True)
    (legacy / "anexos" / "1" / "fatura.txt").write_text(
        "documento recuperável",
        encoding="utf-8",
    )
    state = tmp_path / "perfil"
    monkeypatch.setenv("FINANCAS_PESSOAIS_STATE_DIR", str(state))
    monkeypatch.setattr(config, "DB_NAME", config.DEFAULT_DB_NAME)
    paths = config.paths_for_database()

    first = ensure_persistent_data_location(
        "3.11.0",
        legacy_root=legacy,
        paths=paths,
    )
    assert first.state == "migrated"
    staging = paths.root / ".dados_migracao_20260810_120000_000000"
    paths.data.replace(staging)
    (legacy / config.DEFAULT_DB_NAME).unlink()

    recovered = ensure_persistent_data_location(
        "3.11.0",
        legacy_root=legacy,
        paths=paths,
    )

    assert recovered.state == "migration_recovered"
    assert paths.database.is_file()
    assert (paths.attachments / "1" / "fatura.txt").read_text(
        encoding="utf-8"
    ) == "documento recuperável"
    assert not staging.exists()


def test_incomplete_staging_is_preserved_and_blocks_fresh_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "programa_sem_base"
    legacy.mkdir()
    state = tmp_path / "perfil"
    monkeypatch.setenv("FINANCAS_PESSOAIS_STATE_DIR", str(state))
    monkeypatch.setattr(config, "DB_NAME", config.DEFAULT_DB_NAME)
    paths = config.paths_for_database()
    paths.root.mkdir(parents=True)
    staging = paths.root / ".dados_migracao_incompleta"
    staging.mkdir()
    (staging / "nao_apagar.txt").write_text("dados", encoding="utf-8")

    with pytest.raises(DataLocationError, match="Nenhum ficheiro foi apagado"):
        ensure_persistent_data_location(
            "3.11.0",
            legacy_root=legacy,
            paths=paths,
        )

    assert (staging / "nao_apagar.txt").read_text(encoding="utf-8") == "dados"
    assert not paths.database.exists()
