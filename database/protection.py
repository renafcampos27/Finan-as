"""Non-destructive version upgrades for the local finance data store."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import sys
import zipfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Optional

from utils.app_logging import log_info, log_warning
from utils.config import version_protection_dir
from utils.private_temp import cleanup_private_work, private_workspace
from utils.startup_progress import (
    StartupProgressCallback,
    report_startup_progress,
    scaled_percent,
)
from utils.version import APP_VERSION


DATA_GUARD_VERSION_KEY = "data_guard_app_version"
DATA_GUARD_SCHEMA_KEY = "data_guard_schema_version"
DATA_GUARD_BACKUP_KEY = "data_guard_last_backup"
DATA_GUARD_VERIFIED_KEY = "data_guard_last_verified_at"
PROTECTION_FORMAT_VERSION = 1
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 5 * 1024 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 500
MAX_ARCHIVE_PATH_DEPTH = 20
MIN_ARCHIVE_FREE_SPACE_BUFFER = 64 * 1024 * 1024
ENCRYPTED_BACKUP_MAGIC = b"LGBK1"
VERSION_SNAPSHOT_KEEP = 3
VERSION_SNAPSHOT_MAX_BYTES = 5 * 1024 * 1024 * 1024

# These fields are derived links, never amounts, dates, descriptions or user
# documents.  During an upgrade an empty legacy link may be completed, but an
# existing non-empty identity remains protected just like every other value.
_REPAIRABLE_IDENTITY_COLUMNS: dict[str, frozenset[str]] = {
    "categories": frozenset({"stable_id", "identity_key"}),
    "financial_accounts": frozenset({"stable_id", "account_key", "balance_source"}),
    "holdings": frozenset({"stable_id", "linked_account_id"}),
    "liabilities": frozenset({"account_id"}),
    "transactions": frozenset({"category_id", "account_id"}),
    "subscriptions": frozenset({"category_id", "account_id"}),
    "scheduled_payments": frozenset({"category_id", "account_id"}),
    "transaction_splits": frozenset({"category_id"}),
    "category_rules": frozenset({"category_id"}),
    "monthly_category_budgets": frozenset({"category_id"}),
    "monthly_goals": frozenset({"category_id"}),
    "subcategories": frozenset({"category_id"}),
    "transaction_attachments": frozenset({"content_sha256"}),
}

# A managed attachment path is derived from its transaction and stored name.
# Rewriting an absolute legacy path to that portable key is allowed during an
# upgrade; the attachment bytes themselves remain protected independently by
# their size and SHA-256 fingerprint.
_REWRITABLE_DERIVED_COLUMNS: dict[str, frozenset[str]] = {
    "transaction_attachments": frozenset(
        {"stored_path", "file_size", "file_mtime_ns", "last_verified_at"}
    ),
}


class DataProtectionError(RuntimeError):
    """Base error for startup operations that could endanger stored data."""


class DataCompatibilityError(DataProtectionError):
    """Raised before an older application can write to newer stored data."""


class DataIntegrityError(DataProtectionError):
    """Raised when SQLite or a mandatory recovery archive is damaged."""


class MissingDatabaseRecoveryError(DataIntegrityError):
    """Raised when an established profile has lost its primary database."""

    def __init__(
        self,
        database_file: Path,
        *,
        evidence: Iterable[Path] = (),
        candidates: Iterable[Path] = (),
    ) -> None:
        self.database_file = Path(database_file)
        self.evidence = tuple(Path(path) for path in evidence)
        self.candidates = tuple(Path(path) for path in candidates)
        candidate_hint = (
            f" Foram encontrados {len(self.candidates)} ficheiros de recuperação."
            if self.candidates
            else ""
        )
        super().__init__(
            "A base de dados principal desapareceu ou está vazia, mas este perfil "
            "já contém sinais de utilização. Para evitar substituir dados reais por "
            "um perfil novo, o arranque foi interrompido. Recupera um backup ou "
            f"confirma explicitamente a criação de um perfil vazio.{candidate_hint}"
        )


class DataPreservationError(DataProtectionError):
    """Raised when existing user rows or managed documents have changed."""


@dataclass(slots=True, frozen=True)
class TableFingerprint:
    columns: tuple[str, ...]
    rows: Counter[str]
    preserved_columns: tuple[str, ...] = ()
    preserved_rows: Counter[str] = field(default_factory=Counter)
    locked_identity_rows: dict[str, Counter[str]] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ProtectedFile:
    scope: str
    relative_path: str
    archive_name: str
    source: Path
    size: int
    sha256: str

    def manifest_record(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "relative_path": self.relative_path,
            "archive_name": self.archive_name,
            "size": self.size,
            "sha256": self.sha256,
        }


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _semantic_version(value: str) -> Optional[tuple[int, ...]]:
    text = str(value or "").strip().lower().removeprefix("v")
    parts = text.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    numbers = tuple(int(part) for part in parts)
    return numbers + (0,) * max(0, 3 - len(numbers))


def _normalise_sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float):
        if math.isnan(value):
            return {"float": "nan"}
        if math.isinf(value):
            return {"float": "inf" if value > 0 else "-inf"}
    if value is None or isinstance(value, (str, int, float)):
        return value
    return str(value)


def _row_digest(row: Iterable[Any]) -> str:
    payload = json.dumps(
        [_normalise_sql_value(value) for value in row],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_digest(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_parts(value: str) -> tuple[str, ...]:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if path.is_absolute() or not path.parts:
        raise DataIntegrityError("A proteção contém um caminho de ficheiro inválido.")
    if any(
        part in {"", ".", ".."} or ":" in part or "\x00" in part for part in path.parts
    ):
        raise DataIntegrityError("A proteção contém um caminho de ficheiro inseguro.")
    return tuple(path.parts)


def validate_zip_safety(
    archive: zipfile.ZipFile,
    *,
    extraction_root: Optional[Path] = None,
) -> tuple[list[zipfile.ZipInfo], int]:
    """Reject unsafe or unreasonably large archives before reading contents."""

    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise DataIntegrityError(
            f"O backup contém demasiados ficheiros ({len(infos)})."
        )
    seen: set[str] = set()
    total_size = 0
    for info in infos:
        parts = _safe_relative_parts(info.filename)
        if len(parts) > MAX_ARCHIVE_PATH_DEPTH:
            raise DataIntegrityError("O backup contém uma pasta demasiado profunda.")
        normalized = "/".join(parts).casefold()
        if normalized in seen:
            raise DataIntegrityError(
                f"O backup contém caminhos duplicados: {info.filename}."
            )
        seen.add(normalized)
        mode = (int(info.external_attr) >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            raise DataIntegrityError("O backup contém uma ligação simbólica insegura.")
        if info.flag_bits & 0x1:
            raise DataIntegrityError(
                "O ZIP contém membros cifrados não suportados. Usa o formato .lgbak."
            )
        if info.is_dir():
            continue
        size = int(info.file_size)
        compressed = int(info.compress_size)
        if size < 0 or size > MAX_ARCHIVE_MEMBER_BYTES:
            raise DataIntegrityError(
                f"O ficheiro {info.filename} excede o limite seguro do backup."
            )
        if (
            size > 1024 * 1024
            and size / max(1, compressed) > MAX_ARCHIVE_COMPRESSION_RATIO
        ):
            raise DataIntegrityError(
                f"O ficheiro {info.filename} tem um rácio de compressão inseguro."
            )
        total_size += size
        if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise DataIntegrityError(
                "O conteúdo extraído do backup excede o limite seguro de 5 GB."
            )

    if extraction_root is not None:
        probe = Path(extraction_root).resolve()
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        buffer = max(MIN_ARCHIVE_FREE_SPACE_BUFFER, total_size // 10)
        if free < total_size + buffer:
            raise DataIntegrityError(
                "Não existe espaço livre suficiente para extrair e validar o backup."
            )
    return infos, total_size


class DataProtectionSession:
    """One guarded startup, from pre-upgrade snapshot to final verification."""

    def __init__(
        self,
        guard: "DataProtectionGuard",
        *,
        original_database_existed: bool,
        record_state: bool,
        source_app_version: str = "",
        source_schema_version: int = 0,
        backup_path: Optional[Path] = None,
        tables: Optional[dict[str, TableFingerprint]] = None,
        files: Optional[list[ProtectedFile]] = None,
    ) -> None:
        self.guard = guard
        self.original_database_existed = bool(original_database_existed)
        self.record_state = bool(record_state)
        self.source_app_version = str(source_app_version or "")
        self.source_schema_version = int(source_schema_version)
        self.backup_path = Path(backup_path) if backup_path else None
        self.tables = dict(tables or {})
        self.files = list(files or [])
        self._completed = False

    def verify_after_upgrade(
        self,
        connection: sqlite3.Connection,
        actual_schema_version: int,
    ) -> None:
        self.guard._progress(
            82,
            "Verificar a atualização",
            "A validar a estrutura e a integridade final.",
        )
        self.guard.validate_connection(
            connection,
            full=True,
            progress_callback=self.guard.progress_callback,
            progress_percent=83,
            progress_stage="Verificar a atualização",
        )
        self.guard.validate_current_search_index(connection)
        if int(actual_schema_version) != self.guard.supported_schema_version:
            raise DataCompatibilityError(
                "A atualização da estrutura de dados não terminou. "
                f"Esperado: {self.guard.supported_schema_version}; "
                f"obtido: {int(actual_schema_version)}."
            )
        missing_versions = self.guard.missing_schema_versions(connection)
        if missing_versions:
            values = ", ".join(str(value) for value in sorted(missing_versions))
            raise DataCompatibilityError(
                "A sequência de migrações ficou incompleta. "
                f"Versões em falta: {values}."
            )
        if self.guard.financial_profile:
            from database.schema_contract import assert_schema_contract, financial_integrity_report

            try:
                assert_schema_contract(connection, allow_legacy=False)
                financial = financial_integrity_report(connection)
                if not financial["ok"]:
                    raise ValueError("Invariantes financeiras inválidas: " + "; ".join(financial["issues"]))
            except ValueError as exc:
                raise DataIntegrityError(str(exc)) from exc

        total_items = len(self.tables) + len(self.files)
        checked = 0
        for table, original in self.tables.items():
            self.guard._progress(
                scaled_percent(84, 89, checked, total_items),
                "Confirmar dados preservados",
                table,
                current=checked + 1,
                total=total_items,
            )
            current = self.guard.row_fingerprints(
                connection,
                table,
                original.columns,
            )
            missing = original.rows - current
            if missing:
                # A migration may fill a previously blank durable relationship.
                # Compare every non-derived field and then lock every identity
                # that already had a value.  This permits only blank -> filled;
                # deleting or replacing an existing ID still blocks startup.
                preserved = self.guard.row_fingerprints(
                    connection,
                    table,
                    original.preserved_columns,
                )
                preserved_missing = original.preserved_rows - preserved
                locked_now = self.guard.locked_identity_fingerprints(
                    connection,
                    table,
                    original.preserved_columns,
                    tuple(original.locked_identity_rows),
                )
                locked_missing = any(
                    values - locked_now.get(column, Counter())
                    for column, values in original.locked_identity_rows.items()
                )
                if (
                    not original.preserved_columns
                    or preserved_missing
                    or locked_missing
                ):
                    raise DataPreservationError(
                        "A atualização tentou alterar ou remover informação existente "
                        f"na tabela {table!r}. A base original será reposta."
                    )
            checked += 1

        for protected in self.files:
            self.guard._progress(
                scaled_percent(84, 89, checked, total_items),
                "Confirmar documentos preservados",
                protected.relative_path,
                current=checked + 1,
                total=total_items,
            )
            if not protected.source.is_file():
                raise DataPreservationError(
                    "A atualização removeu um ficheiro protegido: "
                    f"{protected.relative_path}."
                )
            if protected.source.stat().st_size != protected.size:
                raise DataPreservationError(
                    "A atualização alterou o tamanho de um ficheiro protegido: "
                    f"{protected.relative_path}."
                )
            if _file_digest(protected.source) != protected.sha256:
                raise DataPreservationError(
                    "A atualização alterou um ficheiro protegido: "
                    f"{protected.relative_path}."
                )
            checked += 1
        self.guard._progress(
            89,
            "Dados preservados",
            "Todos os registos e documentos anteriores foram confirmados.",
            current=total_items,
            total=total_items,
        )

    def complete(
        self,
        connection: sqlite3.Connection,
        actual_schema_version: int,
    ) -> None:
        self.guard._progress(
            90,
            "Finalizar a proteção",
            "A registar esta versão como verificada.",
        )
        if self.record_state:
            now = datetime.now().isoformat(timespec="seconds")
            values = {
                DATA_GUARD_VERSION_KEY: self.guard.current_app_version,
                DATA_GUARD_SCHEMA_KEY: str(int(actual_schema_version)),
                DATA_GUARD_VERIFIED_KEY: now,
            }
            if self.backup_path is not None:
                values[DATA_GUARD_BACKUP_KEY] = str(self.backup_path)
            connection.executemany(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                list(values.items()),
            )
            connection.commit()
        self.guard._clear_compatible_restore_authorization()
        self.guard.clear_incomplete_marker()
        self._completed = True
        try:
            removed = self.guard.prune_version_snapshots(
                protected_paths=([self.backup_path] if self.backup_path else []),
            )
            if removed:
                log_info(
                    "Removed %s obsolete version protection snapshot(s)",
                    len(removed),
                )
        except Exception as exc:
            # Retention happens only after a verified upgrade and must never
            # turn a successful migration into a false startup failure.
            log_warning("Could not prune old version protections: %s", exc)
        if self.backup_path is not None:
            log_info(
                "Data preserved before version upgrade: %s",
                self.backup_path,
            )
        self.guard._progress(
            91,
            "Proteção concluída",
            "A base está pronta para abrir.",
        )

    def rollback(self) -> None:
        if self._completed:
            return
        if self.backup_path is not None:
            self.guard.restore_archive(self.backup_path)
        elif not self.original_database_existed:
            self.guard.remove_new_database()
        self.guard.clear_incomplete_marker()


class DataProtectionGuard:
    """Create and verify a recovery point before an application upgrade."""

    def __init__(
        self,
        database_file: Path,
        *,
        supported_schema_version: int = 1,
        required_schema_versions: Optional[Iterable[int]] = None,
        current_app_version: str = APP_VERSION,
        attachments_directory: Optional[Path] = None,
        history_directory: Optional[Path] = None,
        preferences_file: Optional[Path] = None,
        protection_directory: Optional[Path] = None,
        progress_callback: StartupProgressCallback | None = None,
        protection_password: Optional[str] = None,
        session_context: Optional[Any] = None,
        financial_profile: bool = False,
    ) -> None:
        self.database_file = Path(database_file)
        self.supported_schema_version = int(supported_schema_version)
        self.session_context = session_context
        self.financial_profile = bool(financial_profile)
        self.required_schema_versions = frozenset(
            int(version)
            for version in (
                required_schema_versions
                if required_schema_versions is not None
                else range(1, self.supported_schema_version + 1)
            )
        )
        self.current_app_version = str(current_app_version)
        self.attachments_directory = Path(
            attachments_directory or self.database_file.parent / "anexos"
        )
        self.history_directory = Path(
            history_directory
            or self.database_file.parent / "backups" / "historico_alteracoes"
        )
        self.preferences_file = Path(
            preferences_file
            or self.database_file.parent / "utils" / "backup_preferences.json"
        )
        self.protection_directory = Path(
            protection_directory or version_protection_dir(self.database_file)
        )
        self.progress_callback = progress_callback
        self.protection_password = str(protection_password or "")
        identity = hashlib.sha256(
            str(self.database_file.resolve()).casefold().encode("utf-8")
        ).hexdigest()[:16]
        self.incomplete_marker = (
            self.protection_directory / f"atualizacao_em_curso_{identity}.json"
        )
        self.restore_marker = (
            self.protection_directory / f"restauro_em_curso_{identity}.json"
        )
        self.compatible_restore_marker = (
            self.protection_directory / f"restauro_compativel_{identity}.json"
        )
        self.new_profile_marker = (
            self.protection_directory / f"novo_perfil_autorizado_{identity}.json"
        )
        cleanup_private_work(
            self.protection_directory,
            legacy_patterns=(".antes_atualizacao_*.tmp",),
        )
        cleanup_private_work(
            self.database_file.parent,
            legacy_patterns=(".protecao-versao-*",),
        )

    def _active_lgbk2_session(self) -> Any:
        session = self.session_context
        if session is None:
            try:
                from services.startup_security import get_active_session

                session = get_active_session()
            except Exception:
                return None
        if session is not None:
            if getattr(session, "_closed", False):
                return None
            if not getattr(session, "backup_master_key", None):
                return None
            return session
        return None

    def _load_envelope_for_session(self, session: Any) -> Any:
        from services.backup_crypto import read_envelope

        try:
            return read_envelope(session.envelope_path)
        except Exception as exc:
            log_warning("Security envelope unavailable for LGBK2 encryption: %s", exc)
            return None

    @staticmethod
    def _preference_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().casefold() in {
            "1",
            "true",
            "sim",
            "yes",
            "on",
        }

    def _version_snapshot_encryption_requested(
        self,
        connection: sqlite3.Connection,
    ) -> bool:
        if self._active_lgbk2_session() is not None:
            return True
        try:
            if self.preferences_file.is_file():
                payload = json.loads(self.preferences_file.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and "backup_encryption_enabled" in payload:
                    return self._preference_bool(
                        payload.get("backup_encryption_enabled")
                    )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log_warning("Could not read backup encryption preference: %s", exc)
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key='backup_encryption_enabled'"
            ).fetchone()
            return self._preference_bool(row[0] if row is not None else "0")
        except sqlite3.DatabaseError:
            return False

    def _required_protection_password(self) -> str:
        if self.protection_password:
            return self.protection_password
        try:
            from services.secret_store import (
                BACKUP_CREDENTIAL_TARGET,
                SecretStoreError,
                WindowsCredentialStore,
            )

            store = WindowsCredentialStore(BACKUP_CREDENTIAL_TARGET)
            password = str(store.read() or "")
        except (ImportError, SecretStoreError, OSError) as exc:
            raise DataProtectionError(
                "A cifragem dos backups está ativa, mas a palavra-passe segura "
                "não pôde ser lida. Os dados não foram atualizados."
            ) from exc
        if len(password) < 8:
            raise DataProtectionError(
                "A cifragem dos backups está ativa, mas não existe uma "
                "palavra-passe válida. Os dados não foram atualizados."
            )
        return password

    @staticmethod
    def _is_encrypted_archive(path: Path) -> bool:
        try:
            with Path(path).open("rb") as handle:
                magic = handle.read(5)
                from services.backup_crypto import LGBK1_MAGIC, LGBK2_MAGIC

                return magic in {LGBK1_MAGIC, LGBK2_MAGIC}
        except OSError:
            return False

    @contextmanager
    def _readable_archive(self, path: Path) -> Iterator[Path]:
        """Yield a ZIP, decrypting only inside the managed private workspace."""

        source = Path(path)
        if not self._is_encrypted_archive(source):
            yield source
            return
        with private_workspace(
            self.protection_directory,
            prefix="version-restore",
        ) as workspace:
            decrypted = workspace / "protection.zip"
            from services.backup_crypto import LGBK1_MAGIC, LGBK2_MAGIC

            with source.open("rb") as handle:
                magic = handle.read(5)

            try:
                if magic == LGBK2_MAGIC:
                    session = self._active_lgbk2_session()
                    if session is None:
                        raise DataProtectionError(
                            "O backup de proteção está cifrado com LGBK2, mas não há sessão de segurança ativa."
                        )
                    from services.backup_crypto import (
                        BackupCryptoCodec as _Codec,
                        BackupCryptoError,
                        CryptoFormatError,
                    )

                    try:
                        kind = _Codec.decrypt(
                            source,
                            decrypted,
                            session_master_key=session.backup_master_key,
                        )
                    except (BackupCryptoError, CryptoFormatError) as exc:
                        raise DataProtectionError(
                            f"Falha ao desencriptar o backup de proteção: {exc}"
                        ) from exc
                elif magic == LGBK1_MAGIC:
                    from services.backup import BackupService

                    kind = BackupService.decrypt_file(
                        source,
                        decrypted,
                        self._required_protection_password(),
                    )
                else:
                    raise DataProtectionError(
                        "Formato de arquivo cifrado desconhecido."
                    )
            except OSError as exc:
                raise DataProtectionError(
                    f"Erro de E/S ao aceder ao arquivo cifrado: {exc}"
                ) from exc

            if kind != "Z":
                raise DataIntegrityError(
                    "A proteção cifrada não contém um arquivo completo."
                )
            yield decrypted

    def validate_protection_archive(self, path: Path) -> dict[str, Any]:
        with self._readable_archive(path) as readable:
            return self.validate_archive(readable)

    @staticmethod
    def _files_below(root: Path) -> list[Path]:
        if not root.exists():
            return []
        try:
            return [path for path in root.rglob("*") if path.is_file()]
        except OSError:
            return []

    def _configured_backup_directories(self) -> tuple[Path, ...]:
        """Return user-selected recovery folders without opening the database."""

        if not self.preferences_file.is_file():
            return ()
        try:
            payload = json.loads(self.preferences_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return ()
            values = (
                payload.get("primary_backup_directory"),
                payload.get("secondary_backup_directory"),
            )
            return tuple(
                dict.fromkeys(
                    Path(str(value)).expanduser().resolve()
                    for value in values
                    if str(value or "").strip()
                )
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log_warning(
                "Could not read configured backup recovery directories: %s",
                exc,
            )
            return ()

    @staticmethod
    def _configured_backup_files(root: Path) -> tuple[Path, ...]:
        """Inspect only direct backup files in an externally selected folder.

        A user may choose a folder on a large drive.  Recursing through that
        complete drive during recovery would make startup appear frozen and
        could treat unrelated files as profile evidence.  Application backups
        are always written directly into the selected folder.
        """

        supported = {".lgbak", ".zip", ".sqlite3", ".db"}
        try:
            return tuple(
                path
                for path in root.iterdir()
                if path.is_file() and path.suffix.casefold() in supported
            )
        except OSError:
            return ()

    def existing_profile_evidence(self) -> tuple[Path, ...]:
        """Return durable artefacts proving that this is not a fresh profile."""

        evidence: list[Path] = []
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.database_file}{suffix}")
            if sidecar.is_file():
                evidence.append(sidecar)
        if self.preferences_file.is_file() and self.preferences_file.stat().st_size > 0:
            evidence.append(self.preferences_file)
        for root in (
            self.attachments_directory,
            self.history_directory,
            self.protection_directory,
        ):
            for path in self._files_below(root):
                if path == self.new_profile_marker:
                    continue
                evidence.append(path)
        backup_directory = self.database_file.parent / "backups"
        roots_and_files = [
            (backup_directory, self._files_below(backup_directory)),
            *(
                (root, self._configured_backup_files(root))
                for root in self._configured_backup_directories()
                if root != backup_directory
            ),
        ]
        for _root, files in roots_and_files:
            for path in files:
                if path == self.new_profile_marker or path in evidence:
                    continue
                evidence.append(path)
        return tuple(sorted(set(evidence), key=lambda path: str(path).casefold()))

    def recovery_candidates(self) -> tuple[Path, ...]:
        """List plausible backups newest-first without opening user data."""

        supported = {".lgbak", ".zip", ".sqlite3", ".db"}
        candidates = [
            path
            for path in self.existing_profile_evidence()
            if path.suffix.casefold() in supported
            and path.resolve() != self.database_file.resolve()
        ]
        return tuple(
            sorted(
                candidates,
                key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
                reverse=True,
            )
        )

    def authorize_empty_profile_once(self) -> None:
        """Persist a one-shot, explicit choice to start a new empty profile."""

        self.protection_directory.mkdir(parents=True, exist_ok=True)
        temporary = self.new_profile_marker.with_suffix(".tmp")
        payload = {
            "database": str(self.database_file.resolve()),
            "authorized_at": datetime.now().isoformat(timespec="seconds"),
        }
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.new_profile_marker)

    def empty_profile_authorized(self) -> bool:
        """Check the one-shot choice without consuming it before DB startup."""

        if not self.new_profile_marker.is_file():
            return False
        try:
            payload = json.loads(self.new_profile_marker.read_text(encoding="utf-8"))
            return str(payload.get("database") or "") == str(
                self.database_file.resolve()
            )
        except OSError, ValueError, json.JSONDecodeError:
            return False

    def _consume_empty_profile_authorization(self) -> bool:
        allowed = self.empty_profile_authorized()
        self.new_profile_marker.unlink(missing_ok=True)
        return allowed

    def _progress(
        self,
        percent: int,
        stage: str,
        detail: str = "",
        *,
        current: int = 0,
        total: int = 0,
    ) -> None:
        report_startup_progress(
            self.progress_callback,
            percent,
            stage,
            detail,
            current=current,
            total=total,
        )

    @staticmethod
    def _readonly_connection(path: Path) -> sqlite3.Connection:
        uri = f"{Path(path).resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000;")
        connection.execute("PRAGMA query_only=ON;")
        return connection

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            is not None
        )

    @classmethod
    def setting_value(
        cls,
        connection: sqlite3.Connection,
        key: str,
    ) -> str:
        if not cls._table_exists(connection, "settings"):
            return ""
        row = connection.execute(
            "SELECT value FROM settings WHERE key=?",
            (str(key),),
        ).fetchone()
        return str(row[0]) if row else ""

    @classmethod
    def schema_version(cls, connection: sqlite3.Connection) -> int:
        if not cls._table_exists(connection, "schema_migrations"):
            return 0
        row = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
        ).fetchone()
        return int(row[0] or 0) if row else 0

    @classmethod
    def schema_versions(cls, connection: sqlite3.Connection) -> set[int]:
        if not cls._table_exists(connection, "schema_migrations"):
            return set()
        return {
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }

    def missing_schema_versions(self, connection: sqlite3.Connection) -> set[int]:
        return set(self.required_schema_versions) - self.schema_versions(connection)

    @staticmethod
    def validate_connection(
        connection: sqlite3.Connection,
        *,
        full: bool,
        progress_callback: StartupProgressCallback | None = None,
        progress_percent: int = 0,
        progress_stage: str = "Verificar a base de dados",
    ) -> None:
        pragma = "integrity_check" if full else "quick_check"
        pulse = 0

        def report_activity() -> int:
            nonlocal pulse
            pulse += 1
            report_startup_progress(
                progress_callback,
                progress_percent,
                progress_stage,
                "A analisar páginas e relações SQLite.",
                current=pulse,
            )
            return 0

        if progress_callback is not None:
            report_startup_progress(
                progress_callback,
                progress_percent,
                progress_stage,
                "A iniciar a verificação SQLite.",
            )
            connection.set_progress_handler(report_activity, 20_000)
        try:
            result = connection.execute(f"PRAGMA {pragma}").fetchone()
            foreign_key_error = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchone()
        finally:
            if progress_callback is not None:
                connection.set_progress_handler(None, 0)
        if not result or str(result[0]).casefold() != "ok":
            detail = str(result[0]) if result else "resultado desconhecido"
            raise DataIntegrityError(
                f"A base de dados não passou a validação SQLite: {detail}"
            )
        if foreign_key_error:
            raise DataIntegrityError(
                "A base de dados contém referências relacionais inválidas. "
                "Nenhuma atualização foi aplicada."
            )
        report_startup_progress(
            progress_callback,
            progress_percent,
            progress_stage,
            "Integridade SQLite confirmada.",
        )

    @staticmethod
    def validate_current_search_index(connection: sqlite3.Connection) -> None:
        """Run the FTS5 check that SQLite's general PRAGMAs do not cover.

        ``integrity_check`` can return ``ok`` even when an FTS5 shadow table is
        damaged.  The active movement-search index is derived data, but its
        synchronization triggers participate in every movement mutation, so a
        hidden failure must block startup before normal writes begin.
        """

        exists = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='transaction_search_fts'"
        ).fetchone()
        if exists is None:
            return
        savepoint = "validate_transaction_search_fts"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            connection.execute(
                "INSERT INTO transaction_search_fts(transaction_search_fts) "
                "VALUES('integrity-check')"
            )
        except sqlite3.DatabaseError as exc:
            try:
                connection.execute(f"ROLLBACK TO {savepoint}")
                connection.execute(f"RELEASE {savepoint}")
            except sqlite3.DatabaseError:
                # Preserve the original FTS error, which is the useful cause
                # shown by the protected-startup dialog.
                pass
            raise DataIntegrityError(
                "O índice de pesquisa dos movimentos não passou a validação "
                "FTS5. Nenhuma gravação normal foi iniciada."
            ) from exc
        else:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")

    @classmethod
    def protected_tables(
        cls,
        connection: sqlite3.Connection,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT name, COALESCE(sql,'')
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        protected: list[str] = []
        for name, sql in rows:
            table = str(name)
            if table == "schema_migrations":
                continue
            from database.schema_contract import is_trusted_derived_table

            if is_trusted_derived_table(connection, table):
                continue
            protected.append(table)
        return protected

    @classmethod
    def table_columns(
        cls,
        connection: sqlite3.Connection,
        table: str,
    ) -> tuple[str, ...]:
        rows = connection.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        ).fetchall()
        return tuple(str(row[1]) for row in rows)

    @classmethod
    def row_fingerprints(
        cls,
        connection: sqlite3.Connection,
        table: str,
        columns: tuple[str, ...],
    ) -> Counter[str]:
        if not cls._table_exists(connection, table):
            raise DataPreservationError(
                f"A atualização removeu a tabela protegida {table!r}."
            )
        if not columns:
            return Counter()
        current_columns = set(cls.table_columns(connection, table))
        missing_columns = [
            column for column in columns if column not in current_columns
        ]
        if missing_columns:
            raise DataPreservationError(
                "A atualização removeu campos existentes de "
                f"{table!r}: {', '.join(missing_columns)}."
            )
        selected = ", ".join(_quote_identifier(column) for column in columns)
        cursor = connection.execute(
            f"SELECT {selected} FROM {_quote_identifier(table)}"
        )
        return Counter(_row_digest(tuple(row)) for row in cursor)

    @classmethod
    def locked_identity_fingerprints(
        cls,
        connection: sqlite3.Connection,
        table: str,
        preserved_columns: tuple[str, ...],
        identity_columns: tuple[str, ...],
    ) -> dict[str, Counter[str]]:
        """Fingerprint only identities already populated in the source row."""

        current_columns = set(cls.table_columns(connection, table))
        result: dict[str, Counter[str]] = {}
        selected_core = ", ".join(
            _quote_identifier(column) for column in preserved_columns
        )
        for column in identity_columns:
            if column not in current_columns:
                result[column] = Counter()
                continue
            selected = (
                f"{selected_core}, {_quote_identifier(column)}"
                if selected_core
                else _quote_identifier(column)
            )
            cursor = connection.execute(
                f"SELECT {selected} FROM {_quote_identifier(table)} "
                f"WHERE TRIM(COALESCE(CAST({_quote_identifier(column)} AS TEXT),''))<>''"
            )
            result[column] = Counter(_row_digest(tuple(row)) for row in cursor)
        return result

    @classmethod
    def capture_tables(
        cls,
        connection: sqlite3.Connection,
        *,
        progress_callback: StartupProgressCallback | None = None,
        progress_start: int = 0,
        progress_end: int = 0,
        progress_stage: str = "Inventariar dados protegidos",
    ) -> dict[str, TableFingerprint]:
        captured: dict[str, TableFingerprint] = {}
        tables = cls.protected_tables(connection)
        total = len(tables)
        for index, table in enumerate(tables, start=1):
            report_startup_progress(
                progress_callback,
                scaled_percent(
                    progress_start,
                    progress_end,
                    index - 1,
                    total,
                ),
                progress_stage,
                table,
                current=index,
                total=total,
            )
            columns = cls.table_columns(connection, table)
            repairable = tuple(
                column
                for column in columns
                if column in _REPAIRABLE_IDENTITY_COLUMNS.get(table, frozenset())
            )
            rewriteable = _REWRITABLE_DERIVED_COLUMNS.get(table, frozenset())
            preserved_columns = tuple(
                column
                for column in columns
                if column not in repairable and column not in rewriteable
            )
            captured[table] = TableFingerprint(
                columns=columns,
                rows=cls.row_fingerprints(connection, table, columns),
                preserved_columns=preserved_columns,
                preserved_rows=cls.row_fingerprints(
                    connection,
                    table,
                    preserved_columns,
                ),
                locked_identity_rows=cls.locked_identity_fingerprints(
                    connection,
                    table,
                    preserved_columns,
                    repairable,
                ),
            )
            report_startup_progress(
                progress_callback,
                scaled_percent(
                    progress_start,
                    progress_end,
                    index,
                    total,
                ),
                progress_stage,
                table,
                current=index,
                total=total,
            )
        return captured

    @staticmethod
    def _files_under(
        root: Path,
        *,
        scope: str,
        archive_root: str,
        progress_callback: StartupProgressCallback | None = None,
        progress_start: int = 0,
        progress_end: int = 0,
        progress_stage: str = "Inventariar documentos protegidos",
    ) -> list[ProtectedFile]:
        if not root.exists():
            return []
        resolved_root = root.resolve()
        protected: list[ProtectedFile] = []
        sources = [
            source
            for source in sorted(root.rglob("*"))
            if source.is_file()
            and not source.name.startswith(".")
            and not source.name.endswith((".copying", ".tmp"))
        ]
        total = len(sources)
        for index, source in enumerate(sources, start=1):
            resolved = source.resolve()
            try:
                relative = resolved.relative_to(resolved_root)
            except ValueError as exc:
                raise DataProtectionError(
                    f"Ficheiro gerido fora da pasta protegida: {source}"
                ) from exc
            relative_text = PurePosixPath(*relative.parts).as_posix()
            archive_name = (
                PurePosixPath(archive_root) / PurePosixPath(relative_text)
            ).as_posix()
            report_startup_progress(
                progress_callback,
                scaled_percent(
                    progress_start,
                    progress_end,
                    index - 1,
                    total,
                ),
                progress_stage,
                relative_text,
                current=index,
                total=total,
            )
            protected.append(
                ProtectedFile(
                    scope=scope,
                    relative_path=relative_text,
                    archive_name=archive_name,
                    source=source,
                    size=int(source.stat().st_size),
                    sha256=_file_digest(source),
                )
            )
            report_startup_progress(
                progress_callback,
                scaled_percent(
                    progress_start,
                    progress_end,
                    index,
                    total,
                ),
                progress_stage,
                relative_text,
                current=index,
                total=total,
            )
        return protected

    def capture_files(self) -> list[ProtectedFile]:
        protected = self._files_under(
            self.attachments_directory,
            scope="attachments",
            archive_root="anexos",
            progress_callback=self.progress_callback,
            progress_start=40,
            progress_end=46,
            progress_stage="Inventariar documentos",
        )
        protected.extend(
            self._files_under(
                self.history_directory,
                scope="history",
                archive_root="historico_alteracoes",
                progress_callback=self.progress_callback,
                progress_start=46,
                progress_end=49,
                progress_stage="Inventariar histórico",
            )
        )
        if self.preferences_file.is_file():
            self._progress(
                49,
                "Inventariar preferências",
                self.preferences_file.name,
                current=1,
                total=1,
            )
            protected.append(
                ProtectedFile(
                    scope="preferences",
                    relative_path=self.preferences_file.name,
                    archive_name="configuracao/backup_preferences.json",
                    source=self.preferences_file,
                    size=int(self.preferences_file.stat().st_size),
                    sha256=_file_digest(self.preferences_file),
                )
            )
        return protected

    def _archive_name(self, source_app_version: str) -> Path:
        def safe_version_token(value: str, fallback: str) -> str:
            text = str(value or fallback).strip().lower().removeprefix("v")
            safe = "".join(
                character
                if character.isalnum() or character in {".", "-", "_"}
                else "_"
                for character in text
            )
            return safe[:48] or fallback

        source = safe_version_token(source_app_version, "legado")
        target = safe_version_token(self.current_app_version, "desconhecida")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return self.protection_directory / (
            f"antes_atualizacao_v{source}_para_v{target}_{stamp}.zip"
        )

    @staticmethod
    def _table_counts(
        tables: dict[str, TableFingerprint],
    ) -> dict[str, int]:
        return {
            table: int(sum(fingerprint.rows.values()))
            for table, fingerprint in tables.items()
        }

    def create_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        source_app_version: str,
        source_schema_version: int,
        tables: dict[str, TableFingerprint],
        files: list[ProtectedFile],
        target_path: Optional[Path] = None,
        purpose: str = "version_upgrade",
    ) -> Path:
        automatic_version_snapshot = (
            target_path is None
            and str(purpose or "version_upgrade") == "version_upgrade"
        )
        encrypt_snapshot = automatic_version_snapshot and (
            self._version_snapshot_encryption_requested(connection)
        )
        active_session = self._active_lgbk2_session() if encrypt_snapshot else None
        encryption_password = (
            self._required_protection_password()
            if encrypt_snapshot and active_session is None
            else ""
        )
        target = (
            Path(target_path)
            if target_path is not None
            else self._archive_name(source_app_version)
        )
        if encrypt_snapshot:
            target = target.with_suffix(".lgbak")
        target.parent.mkdir(parents=True, exist_ok=True)
        workspace_context = private_workspace(
            target.parent,
            prefix="version-snapshot",
        )
        workspace = workspace_context.__enter__()
        temporary_archive = workspace / "protection.zip"
        temporary_database = workspace / "protected.sqlite3"
        try:
            self._progress(
                50,
                "Criar backup de proteção",
                "A copiar a base de dados.",
            )
            destination = sqlite3.connect(str(temporary_database))
            try:

                def backup_progress(
                    _status: int,
                    remaining: int,
                    total: int,
                ) -> None:
                    copied = max(0, int(total) - int(remaining))
                    self._progress(
                        scaled_percent(50, 56, copied, int(total)),
                        "Criar backup de proteção",
                        "A copiar páginas SQLite.",
                        current=copied,
                        total=int(total),
                    )

                connection.backup(
                    destination,
                    pages=256,
                    progress=backup_progress,
                )
                destination.commit()
                settings_exists = destination.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
                ).fetchone()
                if settings_exists:
                    destination.execute(
                        "DELETE FROM settings WHERE key='ai_gemini_api_key'"
                    )
                    destination.commit()
                self.validate_connection(
                    destination,
                    full=True,
                    progress_callback=self.progress_callback,
                    progress_percent=57,
                    progress_stage="Verificar backup de proteção",
                )
            finally:
                destination.close()
            self._progress(
                58,
                "Assinar backup de proteção",
                "A calcular a assinatura da cópia da base de dados.",
            )
            database_hash = _file_digest(temporary_database)
            manifest = {
                "format_version": PROTECTION_FORMAT_VERSION,
                "purpose": str(purpose or "version_upgrade"),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "database_path": str(self.database_file.resolve()),
                "source_app_version": str(source_app_version or ""),
                "target_app_version": self.current_app_version,
                "source_schema_version": int(source_schema_version),
                "target_schema_version": self.supported_schema_version,
                "database": {
                    "archive_name": "financas_pessoais.sqlite3",
                    "size": int(temporary_database.stat().st_size),
                    "sha256": database_hash,
                },
                "tables": self._table_counts(tables),
                "files": [item.manifest_record() for item in files],
                "preferences_present": self.preferences_file.is_file(),
                "included_scopes": [
                    "database",
                    "attachments",
                    "history",
                    "preferences",
                ],
            }
            with zipfile.ZipFile(
                temporary_archive,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                archive.write(
                    temporary_database,
                    "financas_pessoais.sqlite3",
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
                total_files = len(files)
                for index, item in enumerate(files, start=1):
                    self._progress(
                        scaled_percent(59, 65, index - 1, total_files),
                        "Guardar documentos protegidos",
                        item.relative_path,
                        current=index,
                        total=total_files,
                    )
                    ext = item.source.suffix.lower()
                    if ext in {".pdf", ".jpg", ".jpeg", ".png", ".zip", ".gz", ".webp"}:
                        archive.write(
                            item.source,
                            item.archive_name,
                            compress_type=zipfile.ZIP_STORED,
                        )
                    else:
                        archive.write(
                            item.source,
                            item.archive_name,
                            compress_type=zipfile.ZIP_DEFLATED,
                            compresslevel=9,
                        )
                    self._progress(
                        scaled_percent(59, 65, index, total_files),
                        "Guardar documentos protegidos",
                        item.relative_path,
                        current=index,
                        total=total_files,
                    )

                archive.writestr(
                    "manifesto_protecao.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
            self._progress(
                66,
                "Verificar backup de proteção",
                "A validar a base e os documentos guardados.",
            )
            self.validate_archive(
                temporary_archive,
                progress_callback=self.progress_callback,
                progress_start=66,
                progress_end=70,
            )
            if encrypt_snapshot:
                if active_session is not None:
                    from services.backup_crypto import BackupCryptoCodec as _Codec

                    envelope = self._load_envelope_for_session(active_session)
                    if envelope is None:
                        raise DataProtectionError(
                            "Envelope de segurança indisponível para cifrar o backup com LGBK2."
                        )
                    encrypted_candidate = workspace / "protection.lgbak"
                    _Codec.encrypt_v2(
                        temporary_archive,
                        encrypted_candidate,
                        master_key=active_session.backup_master_key,
                        envelope=envelope,
                        payload_kind="Z",
                    )
                else:
                    from services.backup import BackupService

                    encrypted_candidate = workspace / "protection.lgbak"
                    BackupService.encrypt_file(
                        temporary_archive,
                        encrypted_candidate,
                        encryption_password,
                        payload_kind="Z",
                    )
                self.validate_protection_archive(encrypted_candidate)
                self._ensure_writable(target)
                encrypted_candidate.replace(target)
            else:
                temporary_archive.replace(target)
            self._progress(
                70,
                "Backup de proteção concluído",
                target.name,
            )
            return target
        finally:
            temporary_database.unlink(missing_ok=True)
            temporary_archive.unlink(missing_ok=True)
            workspace_context.__exit__(*sys.exc_info())

    @staticmethod
    def read_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
        try:
            payload = json.loads(
                archive.read("manifesto_protecao.json").decode("utf-8")
            )
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataIntegrityError(
                "A proteção de versão não contém um manifesto válido."
            ) from exc
        if not isinstance(payload, dict):
            raise DataIntegrityError(
                "A proteção de versão usa um formato incompatível."
            )
        try:
            format_version = int(payload.get("format_version") or 0)
        except (TypeError, ValueError) as exc:
            raise DataIntegrityError(
                "A proteção de versão usa um formato incompatível."
            ) from exc
        if format_version != PROTECTION_FORMAT_VERSION:
            raise DataIntegrityError(
                "A proteção de versão usa um formato incompatível."
            )
        return payload

    @classmethod
    def validate_archive(
        cls,
        path: Path,
        *,
        progress_callback: StartupProgressCallback | None = None,
        progress_start: int = 0,
        progress_end: int = 0,
    ) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(path, "r") as archive:
                validate_zip_safety(archive)
                manifest = cls.read_manifest(archive)
                database = manifest.get("database")
                if not isinstance(database, dict):
                    raise DataIntegrityError(
                        "O manifesto não descreve a base de dados protegida."
                    )
                database_name = str(
                    database.get("archive_name") or "financas_pessoais.sqlite3"
                )
                _safe_relative_parts(database_name)
                database_info = archive.getinfo(database_name)
                if database_info.file_size != int(database.get("size") or -1):
                    raise DataIntegrityError(
                        "O tamanho da base de dados protegida é inválido."
                    )
                records = list(manifest.get("files", []))
                total = len(records) + 1
                report_startup_progress(
                    progress_callback,
                    progress_start,
                    "Verificar backup de proteção",
                    database_name,
                    current=1,
                    total=total,
                )
                with archive.open(database_name) as handle:
                    if _stream_digest(handle) != str(database.get("sha256") or ""):
                        raise DataIntegrityError(
                            "A cópia protegida da base de dados não passou a "
                            "verificação criptográfica."
                        )
                for index, record in enumerate(records, start=2):
                    if not isinstance(record, dict):
                        raise DataIntegrityError(
                            "O manifesto contém um ficheiro inválido."
                        )
                    archive_name = str(record.get("archive_name") or "")
                    _safe_relative_parts(archive_name)
                    file_info = archive.getinfo(archive_name)
                    if file_info.file_size != int(record.get("size") or -1):
                        raise DataIntegrityError(
                            "O tamanho de um documento protegido é inválido: "
                            f"{archive_name}."
                        )
                    report_startup_progress(
                        progress_callback,
                        scaled_percent(
                            progress_start,
                            progress_end,
                            index - 1,
                            total,
                        ),
                        "Verificar backup de proteção",
                        archive_name,
                        current=index,
                        total=total,
                    )
                    with archive.open(archive_name) as handle:
                        if _stream_digest(handle) != str(record.get("sha256") or ""):
                            raise DataIntegrityError(
                                "Um documento da proteção não passou a "
                                f"verificação: {archive_name}."
                            )
                    report_startup_progress(
                        progress_callback,
                        scaled_percent(
                            progress_start,
                            progress_end,
                            index,
                            total,
                        ),
                        "Verificar backup de proteção",
                        archive_name,
                        current=index,
                        total=total,
                    )
                return manifest
        except (
            OSError,
            TypeError,
            ValueError,
            zipfile.BadZipFile,
            KeyError,
        ) as exc:
            raise DataIntegrityError(
                f"Não foi possível validar a proteção de versão: {exc}"
            ) from exc

    def write_incomplete_marker(self, backup_path: Path) -> None:
        self.protection_directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "database": str(self.database_file.resolve()),
            "backup": str(Path(backup_path).resolve()),
            "target_app_version": self.current_app_version,
            "target_schema_version": self.supported_schema_version,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        temporary = self.incomplete_marker.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.incomplete_marker)

    def clear_incomplete_marker(self) -> None:
        self.incomplete_marker.unlink(missing_ok=True)

    def _marker_referenced_archives(self) -> set[Path]:
        """Return protection files referenced by any durable recovery marker."""

        root = self.protection_directory.resolve()
        referenced: set[Path] = set()

        def inspect(value: Any) -> None:
            if isinstance(value, dict):
                for nested in value.values():
                    inspect(nested)
                return
            if isinstance(value, list):
                for nested in value:
                    inspect(nested)
                return
            if not isinstance(value, str) or not value.strip():
                return
            try:
                candidate = Path(value).resolve()
                candidate.relative_to(root)
            except OSError, ValueError:
                return
            if candidate.suffix.casefold() in {".zip", ".lgbak"}:
                referenced.add(candidate)

        if not self.protection_directory.is_dir():
            return referenced
        for marker in self.protection_directory.glob("*.json"):
            try:
                inspect(json.loads(marker.read_text(encoding="utf-8")))
            except OSError, ValueError, json.JSONDecodeError:
                # An unreadable marker is handled by the recovery flow. It must
                # not make retention guess which archive is safe to delete.
                log_warning(
                    "Skipped version retention because marker is unreadable: %s",
                    marker,
                )
                return set(
                    path.resolve()
                    for path in self.protection_directory.iterdir()
                    if path.is_file() and path.suffix.casefold() in {".zip", ".lgbak"}
                )
        return referenced

    def prune_version_snapshots(
        self,
        *,
        keep: int = VERSION_SNAPSHOT_KEEP,
        max_total_bytes: int = VERSION_SNAPSHOT_MAX_BYTES,
        protected_paths: Iterable[Path] = (),
    ) -> list[Path]:
        """Bound automatic upgrade snapshots without touching other backups."""

        if not self.protection_directory.is_dir():
            return []
        keep = max(1, int(keep))
        max_total_bytes = max(1, int(max_total_bytes))
        root = self.protection_directory.resolve()
        protected = self._marker_referenced_archives()
        for value in protected_paths:
            if value is None:
                continue
            try:
                path = Path(value).resolve()
                path.relative_to(root)
            except OSError, ValueError:
                continue
            protected.add(path)

        candidates: list[tuple[Path, int, int]] = []
        for path in self.protection_directory.iterdir():
            if (
                not path.is_file()
                or not path.name.casefold().startswith("antes_atualizacao_v")
                or path.suffix.casefold() not in {".zip", ".lgbak"}
            ):
                continue
            try:
                stat_result = path.stat()
                candidates.append(
                    (
                        path.resolve(),
                        int(stat_result.st_mtime_ns),
                        int(stat_result.st_size),
                    )
                )
            except OSError as exc:
                log_warning("Could not inspect version protection %s: %s", path, exc)
        candidates.sort(key=lambda item: (item[1], item[0].name), reverse=True)

        selected: set[Path] = set(protected)
        selected_size = sum(
            size for path, _mtime, size in candidates if path in selected
        )
        for index, (path, _mtime, size) in enumerate(candidates):
            if path in selected:
                continue
            should_keep = (
                len(selected.intersection(item[0] for item in candidates)) < keep
            )
            fits_limit = selected_size + size <= max_total_bytes
            if index == 0 or (should_keep and fits_limit):
                selected.add(path)
                selected_size += size

        removed: list[Path] = []
        for path, _mtime, _size in candidates:
            if path in selected:
                continue
            try:
                path.relative_to(root)
                path.unlink()
                removed.append(path)
            except OSError as exc:
                log_warning("Could not remove old version protection %s: %s", path, exc)
        return removed

    def write_restore_marker(self, recovery_archive: Path) -> None:
        """Durably record how to recover if a restore is interrupted."""

        archive = Path(recovery_archive).resolve()
        archive.relative_to(self.protection_directory.resolve())
        self.validate_protection_archive(archive)
        self.protection_directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "database": str(self.database_file.resolve()),
            "recovery_archive": str(archive),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._write_restore_marker_payload(payload)

    def _write_restore_marker_payload(self, payload: dict[str, Any]) -> None:
        temporary = self.restore_marker.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        self._ensure_writable(self.restore_marker)
        temporary.replace(self.restore_marker)

    def clear_restore_marker(self, *, remove_archive: bool = False) -> None:
        archive: Optional[Path] = None
        if remove_archive and self.restore_marker.is_file():
            try:
                payload = json.loads(self.restore_marker.read_text(encoding="utf-8"))
                if payload.get("database") != str(self.database_file.resolve()):
                    raise ValueError("restore marker belongs to another database")
                archive = Path(str(payload.get("recovery_archive") or "")).resolve()
                archive.relative_to(self.protection_directory.resolve())
                if archive == self.protection_directory.resolve():
                    raise ValueError("restore archive path is empty")
                if (
                    archive.is_symlink()
                    or getattr(archive, "is_junction", lambda: False)()
                ):
                    raise ValueError("restore archive is a link")
                if archive.exists() and not archive.is_file():
                    raise ValueError("restore archive is not a regular file")
                if not archive.is_file() and payload.get("completed") is not True:
                    raise ValueError("pending restore archive is missing")
                self._validate_restored_database(self.database_file)
                payload["completed"] = True
                payload["completed_fingerprint"] = self._restored_database_fingerprint()
                self._write_restore_marker_payload(payload)
            except (
                OSError,
                ValueError,
                TypeError,
                AttributeError,
                json.JSONDecodeError,
            ) as exc:
                raise DataIntegrityError(
                    "Não foi possível finalizar o marcador de recuperação."
                ) from exc
        if archive is not None:
            self._remove_owned_file(archive)
        self._remove_owned_file(self.restore_marker)

    @classmethod
    def _remove_sidecars(cls, database_file: Path) -> None:
        for suffix in ("-wal", "-shm"):
            cls._remove_owned_file(Path(f"{database_file}{suffix}"))

    @staticmethod
    def _checkpoint_live_wal(database_file: Path) -> None:
        """BUG-PERS-001: fold un-checkpointed WAL frames into the live database.

        The restore path later moves ``-wal``/``-shm`` sidecars aside.  Any
        transaction still sitting in the WAL at that point would be destroyed
        with them, so a best-effort TRUNCATE checkpoint runs first.  Failure
        is logged and tolerated: the sidecar backup below still preserves the
        frames, so data is never silently dropped.

        A second keeper connection is opened first and closed last: closing
        the LAST SQLite connection deletes the ``-wal``/``-shm`` sidecars,
        which would defeat the sidecar preservation below (and would
        silently drop the frames whenever the checkpoint itself failed, e.g.
        on a corrupt main file).  The sidecars must stay in place so the
        caller can move them aside explicitly.

        Additionally, the checkpoint is only attempted when the file carries
        a valid SQLite header: opening a *corrupt/invalid* database makes
        sqlite's recovery delete the sidecars on close (verified
        experimentally), so in that case the whole call is skipped and the
        sidecar preservation below is the sole safety net.
        """

        header = b""
        try:
            with database_file.open("rb") as handle:
                header = handle.read(16)
        except OSError as exc:
            log_warning("WAL checkpoint before restore could not read header: %s", exc)
            return
        if header != b"SQLite format 3\x00":
            log_warning(
                "WAL checkpoint skipped: %s is not a valid SQLite database; "
                "sidecar preservation remains responsible for the WAL frames.",
                database_file.name,
            )
            return

        try:
            connection = sqlite3.connect(str(database_file))
        except sqlite3.Error as exc:
            log_warning("WAL checkpoint before restore could not connect: %s", exc)
            return
        keeper: Optional[sqlite3.Connection] = None
        try:
            # Hold the WAL open with a keeper connection so the checkpoint
            # connection's close() cannot delete the sidecar files.
            keeper = sqlite3.connect(str(database_file))
            keeper.execute("SELECT 1")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            log_warning("WAL checkpoint before restore failed: %s", exc)
        finally:
            for handle in (connection, keeper):
                if handle is None:
                    continue
                try:
                    handle.close()
                except sqlite3.Error:
                    pass

    def remove_new_database(self) -> None:
        self._remove_sidecars(self.database_file)
        self._remove_owned_file(self.database_file)

    @staticmethod
    def _copy_archive_member(
        archive: zipfile.ZipFile,
        archive_name: str,
        target: Path,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(archive_name) as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink():
            path.unlink(missing_ok=True)
            return
        if getattr(path, "is_junction", lambda: False)():
            path.rmdir()
            return
        if path.is_dir():

            def _handle_remove_readonly(func, p, _excinfo):
                candidate = Path(p)
                if candidate.is_symlink():
                    candidate.unlink(missing_ok=True)
                    return
                if getattr(candidate, "is_junction", lambda: False)():
                    candidate.rmdir()
                    return
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
                func(p)

            shutil.rmtree(path, onerror=_handle_remove_readonly)
        else:
            try:
                path.unlink(missing_ok=True)
            except PermissionError:
                try:
                    os.chmod(path, stat.S_IWRITE)
                    path.unlink(missing_ok=True)
                except Exception:
                    raise

    @staticmethod
    def _ensure_writable(path: Path) -> None:
        try:
            p = Path(path)
            if p.is_symlink() or getattr(p, "is_junction", lambda: False)():
                return
            if p.exists():
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass

    @classmethod
    def _remove_owned_file(cls, path: Path) -> None:
        """Keep unlink's refusal to recurse into an unexpected directory."""
        if path.is_dir() and not (
            path.is_symlink() or getattr(path, "is_junction", lambda: False)()
        ):
            raise IsADirectoryError(str(path))
        cls._remove_path(path)

    def _validate_restored_database(self, database: Path) -> None:
        from database.restore_validation import validate_restore_compatibility

        connection = self._readonly_connection(database)
        try:
            self.validate_connection(connection, full=True)
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = (
                frozenset({"settings", "categories", "transactions"})
                if tables.intersection({"transactions", "categories"})
                else frozenset()
            )
            try:
                validate_restore_compatibility(
                    connection, self.supported_schema_version, required_tables=required
                )
            except ValueError as exc:
                raise DataCompatibilityError(str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            raise DataIntegrityError(
                "A base de dados de restauro não é uma cópia SQLite válida."
            ) from exc
        finally:
            connection.close()

    def _restored_database_fingerprint(self) -> str:
        digest = hashlib.sha256()
        for suffix in ("", "-wal"):
            candidate = Path(f"{self.database_file}{suffix}")
            digest.update(suffix.encode("ascii"))
            digest.update(
                _file_digest(candidate).encode("ascii")
                if candidate.is_file()
                else b"absent"
            )
        return digest.hexdigest()

    def _clear_compatible_restore_authorization(self) -> None:
        self._remove_owned_file(self.compatible_restore_marker)

    def _authorize_compatible_restore(self) -> None:
        """Allow one verified restore, not an arbitrary application downgrade."""
        connection = self._readonly_connection(self.database_file)
        try:
            source_version = self.setting_value(connection, DATA_GUARD_VERSION_KEY)
            source_schema = self.schema_version(connection)
        finally:
            connection.close()
        source_semantic = _semantic_version(source_version)
        current_semantic = _semantic_version(self.current_app_version)
        if not (
            source_semantic and current_semantic and source_semantic > current_semantic
        ):
            self._clear_compatible_restore_authorization()
            return
        payload = {
            "database": str(self.database_file.resolve()),
            "source_app_version": source_version,
            "source_schema_version": source_schema,
            "fingerprint": self._restored_database_fingerprint(),
        }
        self.protection_directory.mkdir(parents=True, exist_ok=True)
        temporary = self.compatible_restore_marker.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self._ensure_writable(self.compatible_restore_marker)
            temporary.replace(self.compatible_restore_marker)
        finally:
            self._remove_path(temporary)

    def _compatible_restore_authorized(self, connection: sqlite3.Connection) -> bool:
        try:
            payload = json.loads(
                self.compatible_restore_marker.read_text(encoding="utf-8")
            )
            return (
                payload.get("database") == str(self.database_file.resolve())
                and payload.get("source_app_version")
                == self.setting_value(connection, DATA_GUARD_VERSION_KEY)
                and payload.get("source_schema_version")
                == self.schema_version(connection)
                and int(payload["source_schema_version"])
                <= self.supported_schema_version
                and payload.get("fingerprint") == self._restored_database_fingerprint()
            )
        except OSError, ValueError, TypeError, KeyError, json.JSONDecodeError:
            return False

    def _install_staged_restore(
        self,
        staged_database: Path,
        staged_attachments: Path,
        staged_history: Path,
        staged_preferences: Optional[Path],
        *,
        preferences_present: bool,
        finalize_callback: Callable[[], None] | None = None,
    ) -> None:
        self._validate_restored_database(staged_database)
        prior_authorization = (
            self.compatible_restore_marker.read_bytes()
            if self.compatible_restore_marker.is_file()
            else None
        )
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        database_candidate = self.database_file.with_name(
            f".{self.database_file.name}.{stamp}.restore"
        )
        database_rollback = self.database_file.with_name(
            f".{self.database_file.name}.{stamp}.rollback"
        )
        directory_pairs = (
            (
                self.attachments_directory,
                staged_attachments,
                self.attachments_directory.with_name(
                    f".{self.attachments_directory.name}.{stamp}.rollback"
                ),
            ),
            (
                self.history_directory,
                staged_history,
                self.history_directory.with_name(
                    f".{self.history_directory.name}.{stamp}.rollback"
                ),
            ),
        )
        preference_candidate = self.preferences_file.with_name(
            f".{self.preferences_file.name}.{stamp}.restore"
        )
        preference_rollback = self.preferences_file.with_name(
            f".{self.preferences_file.name}.{stamp}.rollback"
        )
        moved_directories: list[tuple[Path, Path]] = []
        installed_directories: list[Path] = []
        staged_candidates: list[Path] = []
        preference_moved = False
        database_moved = False
        installed_database = False
        installed_preferences = False
        sidecar_backups: dict[str, Path] = {}
        try:
            self.database_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged_database, database_candidate)
            with database_candidate.open("r+b") as handle:
                os.fsync(handle.fileno())
            # BUG-PERS-001: never destroy un-checkpointed WAL frames.  Fold
            # them into the live database first, then move the sidecars aside
            # instead of deleting them; on any later failure they are restored
            # so no recent transaction can be lost mid-restore.
            self._checkpoint_live_wal(self.database_file)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.database_file}{suffix}")
                if sidecar.exists():
                    sidecar_backup = self.database_file.with_name(
                        f".{self.database_file.name}.{stamp}{suffix}.rollback"
                    )
                    self._remove_path(sidecar_backup)
                    self._ensure_writable(sidecar)
                    self._ensure_writable(sidecar_backup)
                    sidecar.replace(sidecar_backup)
                    sidecar_backups[suffix] = sidecar_backup
            if self.database_file.exists():
                self._ensure_writable(self.database_file)
                self._ensure_writable(database_rollback)
                self.database_file.replace(database_rollback)
                database_moved = True
            self._ensure_writable(self.database_file)
            self._ensure_writable(database_candidate)
            database_candidate.replace(self.database_file)
            installed_database = True
            with self.database_file.open("r+b") as handle:
                os.fsync(handle.fileno())

            for target, staged, rollback in directory_pairs:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._remove_path(rollback)
                candidate = target.with_name(f".{target.name}.{stamp}.candidate")
                self._remove_path(candidate)
                # Stage candidate fully before mutating live target (R2)
                staged_candidates.append(candidate)
                shutil.copytree(staged, candidate)
                if target.exists():
                    self._ensure_writable(target)
                    self._ensure_writable(rollback)
                    target.replace(rollback)
                    moved_directories.append((target, rollback))
                self._ensure_writable(target)
                self._ensure_writable(candidate)
                candidate.replace(target)
                installed_directories.append(target)
                staged_candidates.remove(candidate)

            self.preferences_file.parent.mkdir(parents=True, exist_ok=True)
            self._remove_path(preference_rollback)
            self._remove_path(preference_candidate)
            if preferences_present and staged_preferences is not None:
                shutil.copy2(staged_preferences, preference_candidate)
                with preference_candidate.open("r+b") as handle:
                    os.fsync(handle.fileno())
                if self.preferences_file.exists():
                    self._ensure_writable(self.preferences_file)
                    self._ensure_writable(preference_rollback)
                    self.preferences_file.replace(preference_rollback)
                    preference_moved = True
                self._ensure_writable(self.preferences_file)
                self._ensure_writable(preference_candidate)
                preference_candidate.replace(self.preferences_file)
                installed_preferences = True
            if finalize_callback is not None:
                finalize_callback()
            self._validate_restored_database(self.database_file)
            self._authorize_compatible_restore()
        except Exception as install_exc:
            rollback_errors: list[str] = []
            try:
                if prior_authorization is None:
                    self._clear_compatible_restore_authorization()
                else:
                    self._ensure_writable(self.compatible_restore_marker)
                    self.compatible_restore_marker.write_bytes(prior_authorization)
            except OSError as exc:
                rollback_errors.append(f"restore authorization rollback failed: {exc}")

            # 1. Rollback preferences independently without deleting untouched files (R1)
            try:
                if preference_moved and preference_rollback.exists():
                    self._ensure_writable(self.preferences_file)
                    self._ensure_writable(preference_rollback)
                    try:
                        preference_rollback.replace(self.preferences_file)
                    except OSError:
                        if self.preferences_file.exists():
                            self._ensure_writable(self.preferences_file)
                            self.preferences_file.unlink(missing_ok=True)
                        self._ensure_writable(preference_rollback)
                        preference_rollback.replace(self.preferences_file)
                elif installed_preferences and self.preferences_file.exists():
                    self._remove_path(self.preferences_file)
            except Exception as exc:
                rollback_errors.append(f"preferences rollback failed: {exc}")

            # 2. Rollback directories independently with clear target removal before rename (R2)
            for target in reversed(installed_directories):
                try:
                    self._remove_path(target)
                except Exception as exc:
                    rollback_errors.append(
                        f"failed removing installed dir {target.name}: {exc}"
                    )

            for target, rollback in reversed(moved_directories):
                try:
                    if target.exists():
                        self._remove_path(target)
                    if rollback.exists():
                        self._ensure_writable(target)
                        self._ensure_writable(rollback)
                        rollback.replace(target)
                except Exception as exc:
                    rollback_errors.append(
                        f"failed restoring dir {target.name} from rollback: {exc}"
                    )

            # 3. Rollback database independently
            try:
                if database_moved and database_rollback.exists():
                    self._ensure_writable(self.database_file)
                    self._ensure_writable(database_rollback)
                    try:
                        database_rollback.replace(self.database_file)
                    except OSError:
                        import time

                        time.sleep(0.05)
                        self._ensure_writable(self.database_file)
                        self._ensure_writable(database_rollback)
                        database_rollback.replace(self.database_file)
                elif installed_database and self.database_file.exists():
                    self._remove_path(self.database_file)
            except Exception as exc:
                rollback_errors.append(f"database rollback failed: {exc}")

            # 3b. BUG-PERS-001 / BUG-R6-RESTORE-01: restore the preserved WAL sidecars so any
            # frames the checkpoint could not fold back in survive the failed
            # restore instead of being destroyed with the live sidecars.
            # Restore all sidecars moved into sidecar_backups even if subsequent sidecar backup failed.
            if sidecar_backups:
                for suffix, sidecar_backup in sidecar_backups.items():
                    try:
                        if database_moved and database_rollback.exists():
                            # The old main file could not be restored; its WAL
                            # must remain beside the old rollback evidence.
                            continue
                        sidecar = Path(f"{self.database_file}{suffix}")
                        if sidecar.exists():
                            self._remove_path(sidecar)
                        if sidecar_backup.exists():
                            self._ensure_writable(sidecar)
                            self._ensure_writable(sidecar_backup)
                            sidecar_backup.replace(sidecar)
                    except Exception as exc:
                        rollback_errors.append(
                            f"sidecar rollback failed for {suffix}: {exc}"
                        )

            # 4. Clean up any leftover staging candidates
            for cand in staged_candidates:
                try:
                    self._remove_path(cand)
                except Exception as exc:
                    log_warning(
                        f"Could not remove staged candidate during rollback: {exc}"
                    )
            for item in (preference_candidate, database_candidate):
                try:
                    item.unlink(missing_ok=True)
                except Exception as exc:
                    log_warning(f"Could not remove candidate during rollback: {exc}")

            if rollback_errors:
                log_warning(
                    f"Restore rollback encountered error(s); recovery required: {'; '.join(rollback_errors)}"
                )
                raise DataPreservationError(
                    f"Falha no restauro e reversão incompleta ({'; '.join(rollback_errors)}). "
                    "Os dados originais foram preservados no armazenamento de rollback."
                ) from install_exc
            raise
        else:
            self._remove_path(database_rollback)
            for _target, _staged, rollback in directory_pairs:
                self._remove_path(rollback)
            self._remove_path(preference_rollback)
            # BUG-PERS-001: only drop the preserved sidecars once the staged
            # database is fully installed and every rollback artefact is gone;
            # the staged database carries the checkpointed state, so the old
            # WAL/SHM must never survive into the new database lifetime.
            for sidecar_backup in sidecar_backups.values():
                try:
                    self._remove_path(sidecar_backup)
                except Exception as exc:
                    log_warning(
                        f"Could not remove preserved sidecar backup {sidecar_backup}: {exc}"
                    )
        finally:
            for item in (database_candidate, preference_candidate):
                try:
                    item.unlink(missing_ok=True)
                except Exception as exc:
                    log_warning(f"Could not remove restore candidate {item}: {exc}")
            for cand in staged_candidates:
                try:
                    self._remove_path(cand)
                except Exception as exc:
                    log_warning(f"Could not remove staged candidate {cand}: {exc}")

    def restore_archive(
        self,
        path: Path,
        *,
        interrupted_upgrade: bool = True,
    ) -> None:
        source = Path(path)
        with self._readable_archive(source) as readable:
            self._restore_zip_archive(
                readable,
                interrupted_upgrade=interrupted_upgrade,
                source_path=source,
            )

    def _restore_zip_archive(
        self,
        path: Path,
        *,
        interrupted_upgrade: bool,
        source_path: Optional[Path] = None,
    ) -> None:
        path = Path(path)
        display_path = Path(source_path or path)
        manifest = self.validate_archive(path)
        self.database_file.parent.mkdir(parents=True, exist_ok=True)
        with private_workspace(
            self.database_file.parent,
            prefix="version-restore-stage",
        ) as root:
            staged_database = root / "financas_pessoais.sqlite3"
            staged_attachments = root / "anexos"
            staged_history = root / "historico_alteracoes"
            staged_preferences = root / "backup_preferences.json"
            staged_attachments.mkdir(parents=True, exist_ok=True)
            staged_history.mkdir(parents=True, exist_ok=True)
            preferences_present = bool(manifest.get("preferences_present"))
            restored_preferences = False
            with zipfile.ZipFile(path, "r") as archive:
                validate_zip_safety(archive, extraction_root=root)
                database = manifest["database"]
                self._copy_archive_member(
                    archive,
                    str(database["archive_name"]),
                    staged_database,
                )
                for record in manifest.get("files", []):
                    scope = str(record.get("scope") or "")
                    relative_parts = _safe_relative_parts(
                        str(record.get("relative_path") or "")
                    )
                    if scope == "attachments":
                        target = staged_attachments.joinpath(*relative_parts)
                    elif scope == "history":
                        target = staged_history.joinpath(*relative_parts)
                    elif scope == "preferences":
                        target = staged_preferences
                        restored_preferences = True
                    else:
                        raise DataIntegrityError(
                            f"Âmbito desconhecido na proteção: {scope!r}."
                        )
                    self._copy_archive_member(
                        archive,
                        str(record.get("archive_name") or ""),
                        target,
                    )
                    if target.stat().st_size != int(record.get("size") or -1):
                        raise DataIntegrityError(
                            f"Tamanho inválido ao repor {target.name}."
                        )
                    if _file_digest(target) != str(record.get("sha256") or ""):
                        raise DataIntegrityError(
                            f"Conteúdo inválido ao repor {target.name}."
                        )
            if preferences_present and not restored_preferences:
                raise DataIntegrityError(
                    "A proteção indica preferências guardadas, mas o respetivo "
                    "ficheiro não está presente."
                )
            restored_connection = sqlite3.connect(str(staged_database))
            try:
                self.validate_connection(restored_connection, full=True)
                self.validate_current_search_index(restored_connection)
            finally:
                restored_connection.close()

            def finalize_restore() -> None:
                from services.startup_security import StartupSecurityService

                StartupSecurityService(
                    database_file=self.database_file
                ).resync_tamper_tombstone_after_restore()

            self._install_staged_restore(
                staged_database,
                staged_attachments,
                staged_history,
                staged_preferences if staged_preferences.exists() else None,
                preferences_present=preferences_present,
                finalize_callback=finalize_restore,
            )
        if interrupted_upgrade:
            log_warning(
                "Restored data after an incomplete or failed upgrade: %s",
                display_path,
            )
        else:
            log_info("Verified data archive restored: %s", display_path)

    def _recover_incomplete_restore(self) -> None:
        """Restore the pre-operation snapshot after an interrupted restore."""

        if not self.restore_marker.is_file():
            return
        try:
            payload = json.loads(self.restore_marker.read_text(encoding="utf-8"))
            if str(payload.get("database") or "") != str(self.database_file.resolve()):
                raise DataIntegrityError(
                    "O marcador de restauro pertence a outra base de dados."
                )
            recovery_archive = Path(
                str(payload.get("recovery_archive") or "")
            ).resolve()
            recovery_archive.relative_to(self.protection_directory.resolve())
            if payload.get("completed") is True:
                if (
                    payload.get("completed_fingerprint")
                    != self._restored_database_fingerprint()
                ):
                    raise DataIntegrityError(
                        "O perfil mudou desde a conclusão do restauro; a limpeza exige recuperação manual."
                    )
                self._validate_restored_database(self.database_file)
                self.clear_restore_marker(remove_archive=True)
                return
            if not recovery_archive.is_file():
                raise DataIntegrityError(
                    "A cópia necessária para recuperar o restauro interrompido não existe."
                )
            self.validate_protection_archive(recovery_archive)
            self.restore_archive(recovery_archive, interrupted_upgrade=True)
            connection = self._readonly_connection(self.database_file)
            try:
                self.validate_connection(connection, full=True)
            finally:
                connection.close()
            self.clear_restore_marker(remove_archive=True)
            log_warning("Recovered the state before an interrupted backup restore.")
        except DataProtectionError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise DataIntegrityError(
                "Não foi possível recuperar um restauro interrompido. A base não será aberta para escrita."
            ) from exc

    def _recover_incomplete_upgrade(self) -> None:
        if not self.incomplete_marker.is_file():
            return
        try:
            payload = json.loads(self.incomplete_marker.read_text(encoding="utf-8"))
            expected_database = str(self.database_file.resolve())
            if str(payload.get("database") or "") != expected_database:
                raise DataIntegrityError(
                    "O marcador de atualização pertence a outra base de dados."
                )
            target_version = str(payload.get("target_app_version") or "")
            if self.database_file.is_file():
                try:
                    connection = self._readonly_connection(self.database_file)
                    try:
                        self.validate_connection(connection, full=False)
                        completed_version = self.setting_value(
                            connection,
                            DATA_GUARD_VERSION_KEY,
                        )
                    finally:
                        connection.close()
                    if completed_version == target_version:
                        self.clear_incomplete_marker()
                        return
                except sqlite3.DatabaseError, DataProtectionError:
                    pass
            backup = Path(str(payload.get("backup") or "")).resolve()
            backup.relative_to(self.protection_directory.resolve())
            if not backup.is_file():
                raise DataIntegrityError(
                    "A proteção necessária para recuperar a atualização não existe."
                )
            self.restore_archive(backup)
            self.clear_incomplete_marker()
        except DataProtectionError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise DataIntegrityError(
                "Não foi possível recuperar uma atualização interrompida. "
                "A base de dados não será aberta para escrita."
            ) from exc

    def prepare(self) -> DataProtectionSession:
        connection: Optional[sqlite3.Connection] = None
        try:
            self._progress(
                20,
                "Preparar a proteção dos dados",
                "A procurar restauros ou atualizações interrompidos.",
            )
            self._recover_incomplete_restore()
            self._recover_incomplete_upgrade()
            if (
                not self.database_file.is_file()
                or self.database_file.stat().st_size == 0
            ):
                evidence = self.existing_profile_evidence()
                explicitly_authorized = self._consume_empty_profile_authorization()
                if evidence and not explicitly_authorized:
                    raise MissingDatabaseRecoveryError(
                        self.database_file,
                        evidence=evidence,
                        candidates=self.recovery_candidates(),
                    )
                self._progress(
                    49,
                    "Preparar uma base nova",
                    "Perfil vazio confirmado; ainda não existem dados financeiros.",
                )
                return DataProtectionSession(
                    self,
                    original_database_existed=False,
                    record_state=True,
                )

            connection = self._readonly_connection(self.database_file)
            # Validate source-owned SQL before init_schema, seed/reference writes
            # or migration DDL can execute a trigger from the inspected profile.
            source_schema = self.schema_version(connection)
            if source_schema > self.supported_schema_version:
                raise DataCompatibilityError(
                    "Esta base de dados foi criada por uma versão mais recente "
                    f"(estrutura {source_schema}). A versão instalada suporta "
                    f"apenas a estrutura {self.supported_schema_version}. "
                    "Os dados não foram alterados."
                )
            if self.financial_profile:
                from database.schema_contract import assert_schema_contract, financial_integrity_report

                try:
                    assert_schema_contract(connection, allow_legacy=True)
                    financial = financial_integrity_report(connection)
                    if not financial["ok"]:
                        raise ValueError("Invariantes financeiras inválidas: " + "; ".join(financial["issues"]))
                except ValueError as exc:
                    raise DataIntegrityError(str(exc)) from exc
            self.validate_connection(
                connection,
                full=False,
                progress_callback=self.progress_callback,
                progress_percent=24,
                progress_stage="Validar a base atual",
            )
            source_schema = self.schema_version(connection)
            missing_schema_versions = self.missing_schema_versions(connection)
            source_version = self.setting_value(
                connection,
                DATA_GUARD_VERSION_KEY,
            )
            if source_schema > self.supported_schema_version:
                raise DataCompatibilityError(
                    "Esta base de dados foi criada por uma versão mais recente "
                    f"(estrutura {source_schema}). A versão instalada suporta "
                    f"apenas a estrutura {self.supported_schema_version}. "
                    "Os dados não foram alterados."
                )
            stored_semantic = _semantic_version(source_version)
            current_semantic = _semantic_version(self.current_app_version)
            if (
                stored_semantic is not None
                and current_semantic is not None
                and stored_semantic > current_semantic
                and not self._compatible_restore_authorized(connection)
            ):
                raise DataCompatibilityError(
                    "Os dados já foram abertos por uma versão mais recente "
                    f"({source_version}). Instala essa versão ou uma superior. "
                    "Nenhum dado foi alterado."
                )

            record_state = (
                source_version != self.current_app_version
                or source_schema != self.supported_schema_version
                or bool(missing_schema_versions)
            )
            if not record_state:
                self._progress(
                    49,
                    "Proteção já atualizada",
                    "A base já corresponde a esta versão da aplicação.",
                )
                return DataProtectionSession(
                    self,
                    original_database_existed=True,
                    record_state=False,
                    source_app_version=source_version,
                    source_schema_version=source_schema,
                )
            self.validate_connection(
                connection,
                full=True,
                progress_callback=self.progress_callback,
                progress_percent=30,
                progress_stage="Validar antes da atualização",
            )
            tables = self.capture_tables(
                connection,
                progress_callback=self.progress_callback,
                progress_start=32,
                progress_end=40,
                progress_stage="Inventariar dados protegidos",
            )
            files = self.capture_files()
            self._progress(
                49,
                "Inventário protegido concluído",
                f"{len(tables)} tabela(s) e {len(files)} ficheiro(s).",
            )
            backup_path = self.create_snapshot(
                connection,
                source_app_version=source_version,
                source_schema_version=source_schema,
                tables=tables,
                files=files,
            )
            self.write_incomplete_marker(backup_path)
            self._progress(
                71,
                "Proteção pronta",
                "A atualização pode avançar com reposição automática.",
            )
            return DataProtectionSession(
                self,
                original_database_existed=True,
                record_state=True,
                source_app_version=source_version,
                source_schema_version=source_schema,
                backup_path=backup_path,
                tables=tables,
                files=files,
            )
        except sqlite3.DatabaseError as exc:
            raise DataIntegrityError(
                "Não foi possível validar a base de dados antes da atualização. "
                "Nenhum dado foi alterado."
            ) from exc
        except OSError as exc:
            raise DataProtectionError(
                "Não foi possível criar ou validar a proteção obrigatória desta "
                "versão. A base de dados não foi aberta para escrita e nenhum "
                "dado foi alterado."
            ) from exc
        finally:
            if connection is not None:
                connection.close()


FinanceDatabaseProtection = DataProtectionGuard
