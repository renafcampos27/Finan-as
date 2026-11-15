"""Safe one-time migration from the program folder to stable user storage."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from database.protection import (
    DataIntegrityError,
    DataProtectionGuard,
    MissingDatabaseRecoveryError,
)
from services.documents import normalise_attachment_storage
from utils.app_logging import log_info, log_warning
from utils.config import (
    DEFAULT_DB_NAME,
    AppPaths,
    app_dir,
    paths_for_database,
)
from utils.startup_progress import (
    StartupProgressCallback,
    report_startup_progress,
    scaled_percent,
)


MIGRATION_MANIFEST_NAME = "migracao_localizacao_dados.json"


class DataLocationError(DataIntegrityError):
    """Raised when changing the data location cannot be proven safe."""


@dataclass(slots=True, frozen=True)
class DataLocationResult:
    state: str
    source: Path
    target: Path
    copied_files: int = 0


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    return (path for path in sorted(root.rglob("*")) if path.is_file())


def _copy_tree_verified(
    source: Path,
    target: Path,
    *,
    progress_callback: StartupProgressCallback | None = None,
    progress_start: int = 0,
    progress_end: int = 0,
    progress_stage: str = "Copiar ficheiros",
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    if not source.is_dir():
        return records
    source_root = source.resolve()
    items = list(_files(source))
    total = len(items)
    for index, item in enumerate(items, start=1):
        relative = item.resolve().relative_to(source_root)
        report_startup_progress(
            progress_callback,
            scaled_percent(
                progress_start,
                progress_end,
                index - 1,
                total,
            ),
            progress_stage,
            relative.as_posix(),
            current=index,
            total=total,
        )
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        source_hash = _digest(item)
        if destination.stat().st_size != item.stat().st_size:
            raise DataLocationError(
                f"A cópia de {relative} ficou com um tamanho diferente."
            )
        if _digest(destination) != source_hash:
            raise DataLocationError(
                f"A cópia de {relative} não passou a verificação SHA-256."
            )
        records.append(
            {
                "path": relative.as_posix(),
                "size": int(item.stat().st_size),
                "sha256": source_hash,
            }
        )
        report_startup_progress(
            progress_callback,
            scaled_percent(progress_start, progress_end, index, total),
            progress_stage,
            relative.as_posix(),
            current=index,
            total=total,
        )
    return records


def _clone_database_verified(
    source: Path,
    target: Path,
    *,
    progress_callback: StartupProgressCallback | None = None,
) -> dict[str, int]:
    source_connection = None
    target_connection = None
    try:
        report_startup_progress(
            progress_callback,
            6,
            "Migrar a base de dados",
            "A validar a base original.",
        )
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        source_connection = sqlite3.connect(source_uri, uri=True)
        DataProtectionGuard.validate_connection(
            source_connection,
            full=True,
            progress_callback=progress_callback,
            progress_percent=7,
            progress_stage="Migrar a base de dados",
        )
        source_tables = DataProtectionGuard.capture_tables(source_connection)

        target.parent.mkdir(parents=True, exist_ok=True)
        target_connection = sqlite3.connect(str(target))

        def backup_progress(_status: int, remaining: int, total: int) -> None:
            copied = max(0, int(total) - int(remaining))
            report_startup_progress(
                progress_callback,
                scaled_percent(8, 11, copied, int(total)),
                "Migrar a base de dados",
                "A copiar páginas SQLite com verificação.",
                current=copied,
                total=int(total),
            )

        source_connection.backup(
            target_connection,
            pages=256,
            progress=backup_progress,
        )
        target_connection.commit()
        DataProtectionGuard.validate_connection(
            target_connection,
            full=True,
            progress_callback=progress_callback,
            progress_percent=12,
            progress_stage="Migrar a base de dados",
        )
        target_tables = DataProtectionGuard.capture_tables(target_connection)
        if source_tables != target_tables:
            raise DataLocationError(
                "A base de dados copiada não contém exatamente os mesmos registos."
            )
        return {
            table: int(sum(fingerprint.rows.values()))
            for table, fingerprint in source_tables.items()
        }
    except sqlite3.DatabaseError as exc:
        raise DataLocationError(
            f"A base de dados antiga não pôde ser validada ou copiada: {exc}"
        ) from exc
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()


def _validate_existing_database(
    path: Path,
    *,
    progress_callback: StartupProgressCallback | None = None,
    progress_percent: int = 11,
) -> None:
    connection = None
    try:
        report_startup_progress(
            progress_callback,
            progress_percent,
            "Verificar a base de dados",
            "A confirmar a integridade da localização permanente.",
        )
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        DataProtectionGuard.validate_connection(
            connection,
            full=True,
            progress_callback=progress_callback,
            progress_percent=progress_percent,
            progress_stage="Verificar a base de dados",
        )
    except sqlite3.DatabaseError as exc:
        raise DataLocationError(
            "A base de dados da localização permanente está danificada; "
            "a aplicação não vai escrever por cima dela."
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _validate_and_normalise_staged_attachments(
    database_file: Path,
    attachments_directory: Path,
    *,
    rewrite: bool,
) -> dict[str, object]:
    """Prove that every attachment can be resolved below a staged root."""

    connection = None
    try:
        connection = sqlite3.connect(str(database_file))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        report = normalise_attachment_storage(
            connection,
            attachments_directory,
            require_all=True,
            rewrite=rewrite,
        )
        if rewrite:
            connection.commit()
        else:
            connection.rollback()
        return report
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        raise DataLocationError(
            "Os documentos associados aos movimentos não puderam ser "
            "validados na nova localização."
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _directory_has_content(path: Path) -> bool:
    if not path.exists():
        return False
    return any(item.is_file() or item.is_symlink() for item in path.rglob("*"))


def _missing_profile_error(
    target_paths: AppPaths,
    app_version: str,
) -> MissingDatabaseRecoveryError | None:
    guard = DataProtectionGuard(
        target_paths.database,
        supported_schema_version=0,
        current_app_version=app_version,
        attachments_directory=target_paths.attachments,
        history_directory=target_paths.history,
        preferences_file=target_paths.backup_preferences,
        protection_directory=target_paths.protections,
    )
    if guard.empty_profile_authorized():
        return None
    evidence = guard.existing_profile_evidence()
    if not evidence:
        return None
    return MissingDatabaseRecoveryError(
        target_paths.database,
        evidence=evidence,
        candidates=guard.recovery_candidates(),
    )


def _remove_empty_data_tree(path: Path, root: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    if (
        resolved.parent != root.resolve()
        or resolved.name != "dados"
        or _directory_has_content(path)
    ):
        raise DataLocationError(
            "A pasta de dados que seria preparada não está vazia ou não "
            "corresponde ao destino esperado."
        )
    shutil.rmtree(path)


def _remove_controlled_staging(staging: Path, root: Path) -> None:
    resolved = staging.resolve()
    expected_parent = root.resolve()
    if resolved.parent != expected_parent or not resolved.name.startswith(
        ".dados_migracao_"
    ):
        raise DataLocationError(
            "A pasta temporária de migração não pertence à localização esperada."
        )
    if staging.exists():
        shutil.rmtree(staging)


def _safe_staged_file(staging: Path, relative_value: object) -> Path:
    relative = Path(str(relative_value or ""))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise DataLocationError("A migração interrompida contém um caminho inválido.")
    candidate = (staging / relative).resolve()
    candidate.relative_to(staging.resolve())
    return candidate


def _validate_staged_migration(
    staging: Path,
    target_paths: AppPaths,
    *,
    progress_callback: StartupProgressCallback | None = None,
) -> tuple[dict[str, object], int]:
    """Prove that a completed staging tree can safely become the live data tree."""

    manifest_path = staging / "configuracao" / MIGRATION_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataLocationError(
            f"A migração interrompida {staging.name} não tem um manifesto válido."
        ) from exc
    if not isinstance(manifest, dict) or int(manifest.get("format_version") or 0) != 1:
        raise DataLocationError(
            f"A migração interrompida {staging.name} usa um formato desconhecido."
        )
    try:
        declared_target = Path(str(manifest.get("target") or "")).resolve()
    except OSError as exc:
        raise DataLocationError(
            "O destino declarado pela migração é inválido."
        ) from exc
    if declared_target != target_paths.data.resolve():
        raise DataLocationError(
            f"A migração interrompida {staging.name} pertence a outro destino."
        )

    database_record = manifest.get("database")
    if not isinstance(database_record, dict):
        raise DataLocationError("O manifesto não descreve a base de dados migrada.")
    staged_database = staging / DEFAULT_DB_NAME
    if not staged_database.is_file():
        raise DataLocationError("A migração interrompida não contém a base de dados.")
    if staged_database.stat().st_size != int(database_record.get("size") or -1):
        raise DataLocationError(
            "O tamanho da base migrada não corresponde ao manifesto."
        )
    if _digest(staged_database) != str(database_record.get("sha256") or ""):
        raise DataLocationError("A base migrada não passou a verificação SHA-256.")
    _validate_existing_database(
        staged_database,
        progress_callback=progress_callback,
        progress_percent=8,
    )

    copied_files = 0
    for scope, root_name in (("attachments", "anexos"), ("backups", "backups")):
        records = manifest.get(scope) or []
        if not isinstance(records, list):
            raise DataLocationError(f"A lista {scope} da migração é inválida.")
        for record in records:
            if not isinstance(record, dict):
                raise DataLocationError(
                    "A migração contém um registo de ficheiro inválido."
                )
            candidate = _safe_staged_file(
                staging / root_name,
                record.get("path"),
            )
            if not candidate.is_file():
                raise DataLocationError(
                    f"Falta um ficheiro declarado pela migração: {record.get('path')}."
                )
            if candidate.stat().st_size != int(record.get("size") or -1):
                raise DataLocationError(
                    f"O tamanho de {record.get('path')} não corresponde ao manifesto."
                )
            if _digest(candidate) != str(record.get("sha256") or ""):
                raise DataLocationError(
                    f"O ficheiro {record.get('path')} não passou a verificação SHA-256."
                )
            copied_files += 1

    _validate_and_normalise_staged_attachments(
        staged_database,
        staging / "anexos",
        rewrite=False,
    )

    preference_record = manifest.get("preferences")
    if preference_record is not None:
        if not isinstance(preference_record, dict):
            raise DataLocationError("O registo das preferências migradas é inválido.")
        preference = staging / "configuracao" / "backup_preferences.json"
        if not preference.is_file():
            raise DataLocationError("Faltam as preferências declaradas pela migração.")
        if preference.stat().st_size != int(preference_record.get("size") or -1):
            raise DataLocationError("O tamanho das preferências migradas é inválido.")
        if _digest(preference) != str(preference_record.get("sha256") or ""):
            raise DataLocationError("As preferências migradas falharam a verificação.")
        copied_files += 1
    return manifest, copied_files


def _recover_staged_migration(
    target_paths: AppPaths,
    *,
    progress_callback: StartupProgressCallback | None = None,
) -> DataLocationResult | None:
    candidates = sorted(target_paths.root.glob(".dados_migracao_*"))
    if not candidates:
        return None
    valid: list[tuple[Path, dict[str, object], int]] = []
    errors: list[str] = []
    for candidate in candidates:
        try:
            manifest, copied_files = _validate_staged_migration(
                candidate,
                target_paths,
                progress_callback=progress_callback,
            )
            valid.append((candidate, manifest, copied_files))
        except (DataLocationError, OSError, ValueError) as exc:
            errors.append(f"{candidate.name}: {exc}")
    if len(valid) != 1 or errors:
        detail = "; ".join(errors[:3]) or "foram encontradas várias cópias válidas"
        raise DataLocationError(
            "Foi encontrada uma migração interrompida que não pode ser escolhida "
            f"automaticamente. Nenhum ficheiro foi apagado. Detalhe: {detail}"
        )
    if _directory_has_content(target_paths.data):
        raise DataLocationError(
            "A migração interrompida é válida, mas o destino também contém ficheiros. "
            "O arranque foi bloqueado para não substituir nenhum deles."
        )
    _remove_empty_data_tree(target_paths.data, target_paths.root)
    staging, manifest, copied_files = valid[0]
    staging.replace(target_paths.data)
    target_paths.create_runtime_directories()
    source = Path(str(manifest.get("source") or target_paths.root))
    report_startup_progress(
        progress_callback,
        18,
        "Migração recuperada",
        "A cópia interrompida foi validada e ativada sem repetir a migração.",
        current=copied_files,
        total=copied_files,
    )
    log_info("Migração interrompida recuperada a partir de %s", staging)
    return DataLocationResult(
        state="migration_recovered",
        source=source,
        target=target_paths.data,
        copied_files=copied_files,
    )


def _copy_legacy_logs(legacy_root: Path, paths: AppPaths) -> None:
    source = legacy_root / "logs"
    if not source.is_dir() or source.resolve() == paths.logs.resolve():
        return
    target = paths.logs / "legado"
    try:
        _copy_tree_verified(source, target)
    except Exception as exc:
        # Logs are diagnostic data, never a reason to endanger the financial DB.
        log_warning("Não foi possível copiar todos os registos antigos: %s", exc)


def _ensure_persistent_data_location(
    app_version: str,
    *,
    legacy_root: Path | None = None,
    paths: AppPaths | None = None,
    progress_callback: StartupProgressCallback | None = None,
) -> DataLocationResult:
    """Prepare stable data storage, copying legacy data once without deleting it."""
    report_startup_progress(
        progress_callback,
        2,
        "Localizar os teus dados",
        "A escolher a pasta permanente desta instalação.",
    )
    target_paths = paths or paths_for_database()
    source_root = Path(legacy_root or app_dir()).resolve()
    if not target_paths.uses_stable_profile:
        target_paths.create_runtime_directories()
        report_startup_progress(
            progress_callback,
            18,
            "Localização confirmada",
            str(target_paths.data),
        )
        return DataLocationResult(
            state="explicit",
            source=source_root,
            target=target_paths.data,
        )

    target_paths.root.mkdir(parents=True, exist_ok=True)
    legacy_database = source_root / DEFAULT_DB_NAME
    target_database = target_paths.database
    if target_paths.data.exists() and not target_paths.data.is_dir():
        raise DataLocationError(
            "A localização permanente dos dados existe, mas não é uma pasta. "
            "O arranque foi bloqueado sem alterar os ficheiros."
        )

    if target_database.is_file() and target_database.stat().st_size > 0:
        _validate_existing_database(
            target_database,
            progress_callback=progress_callback,
        )
        target_paths.create_runtime_directories()
        _copy_legacy_logs(source_root, target_paths)
        report_startup_progress(
            progress_callback,
            18,
            "Localização confirmada",
            str(target_paths.data),
        )
        return DataLocationResult(
            state="ready",
            source=source_root,
            target=target_paths.data,
        )
    if target_database.is_file() and target_database.stat().st_size == 0:
        missing_error = _missing_profile_error(target_paths, app_version)
        if missing_error is not None:
            raise missing_error
        target_database.unlink()
    if target_database.exists():
        raise DataLocationError(
            "A localização permanente da base de dados existe, mas não é um "
            "ficheiro normal. O arranque foi bloqueado."
        )

    recovered = _recover_staged_migration(
        target_paths,
        progress_callback=progress_callback,
    )
    if recovered is not None:
        _copy_legacy_logs(source_root, target_paths)
        return recovered

    if not legacy_database.is_file():
        if _directory_has_content(target_paths.data):
            missing_error = _missing_profile_error(target_paths, app_version)
            if missing_error is not None:
                raise missing_error
            authorization_guard = DataProtectionGuard(
                target_paths.database,
                supported_schema_version=0,
                current_app_version=app_version,
                attachments_directory=target_paths.attachments,
                history_directory=target_paths.history,
                preferences_file=target_paths.backup_preferences,
                protection_directory=target_paths.protections,
            )
            if not authorization_guard.empty_profile_authorized():
                raise DataLocationError(
                    "A pasta permanente contém dados incompletos, mas não contém a "
                    "base de dados. O arranque foi bloqueado para não os ocultar."
                )
        target_paths.create_runtime_directories()
        report_startup_progress(
            progress_callback,
            18,
            "Preparar a primeira utilização",
            "A base de dados será criada na localização permanente.",
        )
        return DataLocationResult(
            state="fresh",
            source=source_root,
            target=target_paths.data,
        )

    if _directory_has_content(target_paths.data):
        raise DataLocationError(
            "Foi encontrada uma migração parcial na localização permanente. "
            "A aplicação manteve os dados antigos intactos e bloqueou o arranque."
        )
    _remove_empty_data_tree(target_paths.data, target_paths.root)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    staging = target_paths.root / f".dados_migracao_{stamp}"
    records: dict[str, object] = {}
    copied_files = 0
    try:
        staging.mkdir(parents=False, exist_ok=False)
        staged_database = staging / DEFAULT_DB_NAME
        table_counts = _clone_database_verified(
            legacy_database,
            staged_database,
            progress_callback=progress_callback,
        )

        attachments = _copy_tree_verified(
            source_root / "anexos",
            staging / "anexos",
            progress_callback=progress_callback,
            progress_start=12,
            progress_end=15,
            progress_stage="Migrar documentos",
        )
        backups = _copy_tree_verified(
            source_root / "backups",
            staging / "backups",
            progress_callback=progress_callback,
            progress_start=15,
            progress_end=17,
            progress_stage="Migrar backups anteriores",
        )
        copied_files += len(attachments) + len(backups)

        _validate_and_normalise_staged_attachments(
            staged_database,
            staging / "anexos",
            rewrite=True,
        )

        legacy_preferences = source_root / "utils" / "backup_preferences.json"
        preference_record: dict[str, object] | None = None
        if legacy_preferences.is_file():
            report_startup_progress(
                progress_callback,
                17,
                "Migrar preferências",
                "A copiar e verificar as definições de backup.",
            )
            destination = staging / "configuracao" / "backup_preferences.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_preferences, destination)
            source_hash = _digest(legacy_preferences)
            if _digest(destination) != source_hash:
                raise DataLocationError(
                    "As preferências de backup copiadas não passaram a verificação."
                )
            preference_record = {
                "size": int(legacy_preferences.stat().st_size),
                "sha256": source_hash,
            }
            copied_files += 1

        records = {
            "format_version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "application_version": str(app_version),
            "source": str(source_root),
            "target": str(target_paths.data),
            "legacy_kept_intact": True,
            "database": {
                "source": str(legacy_database),
                "size": int(staged_database.stat().st_size),
                "sha256": _digest(staged_database),
                "tables": table_counts,
            },
            "attachments": attachments,
            "backups": backups,
            "preferences": preference_record,
        }
        manifest = staging / "configuracao" / MIGRATION_MANIFEST_NAME
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        _validate_existing_database(
            staged_database,
            progress_callback=progress_callback,
            progress_percent=18,
        )
        staging.replace(target_paths.data)
    except Exception:
        _remove_controlled_staging(staging, target_paths.root)
        raise

    target_paths.create_runtime_directories()
    _copy_legacy_logs(source_root, target_paths)
    log_info(
        "Dados migrados com verificação para %s; origem mantida em %s",
        target_paths.data,
        source_root,
    )
    report_startup_progress(
        progress_callback,
        18,
        "Migração concluída",
        f"{copied_files} ficheiro(s) copiados e verificados.",
        current=copied_files,
        total=copied_files,
    )
    return DataLocationResult(
        state="migrated",
        source=source_root,
        target=target_paths.data,
        copied_files=copied_files,
    )


def ensure_persistent_data_location(
    app_version: str,
    *,
    legacy_root: Path | None = None,
    paths: AppPaths | None = None,
    progress_callback: StartupProgressCallback | None = None,
) -> DataLocationResult:
    """Expose migration failures as one startup-safe error family."""
    try:
        return _ensure_persistent_data_location(
            app_version,
            legacy_root=legacy_root,
            paths=paths,
            progress_callback=progress_callback,
        )
    except (DataLocationError, MissingDatabaseRecoveryError):
        raise
    except (DataIntegrityError, OSError, ValueError) as exc:
        raise DataLocationError(
            "Não foi possível preparar a localização permanente com segurança. "
            "A origem não foi apagada nem substituída."
        ) from exc
