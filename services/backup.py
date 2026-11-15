"""Backup, restore, retention and backup-preference orchestration."""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Optional

from database.protection import (
    DataIntegrityError,
    DataProtectionGuard,
    MAX_ARCHIVE_UNCOMPRESSED_BYTES,
    MIN_ARCHIVE_FREE_SPACE_BUFFER,
    validate_zip_safety,
)
from database.restore_validation import validate_restore_compatibility
from utils.app_logging import log_exception, log_warning
from utils.config import DEFAULT_DB_NAME, paths_for_database
from utils.helpers import backup_stamp, parse_backup_datetime
from utils.private_temp import (
    cleanup_private_work,
    private_workspace,
    system_private_parent,
)
from utils.version import APP_VERSION
from services.documents import normalise_attachment_storage
from services.backup_crypto import BackupCryptoError, LGBK2_MAGIC
from services.secret_store import (
    BACKUP_CREDENTIAL_TARGET,
    BACKUP_PASSWORD_HISTORY_TARGET,
    SecretStore,
    SecretStoreError,
    default_secret_store,
)

if TYPE_CHECKING:
    from database.repo import Repo


class _ThreadOwnedBackupDatabase:
    """Minimal SQLite wrapper used only by scheduled background backups.

    The main application connection belongs to the GUI thread.  Reusing it
    while a ZIP is created and verified would make unrelated reads/writes wait
    on the same Python/SQLite object.  This adapter owns a separate connection
    for the lifetime of one background task.
    """

    CURRENT_SCHEMA_VERSION = 0

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"A base de dados não existe: {self.path}")
        self.conn = sqlite3.connect(
            f"{self.path.as_uri()}?mode=rw",
            uri=True,
            timeout=30.0,
            check_same_thread=True,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")

    def backup_to(self, target: Path, max_retries: int = 5) -> None:
        """Copy the database into ``target`` via a temporary file.

        BUG-PERS-002: the copy used to be streamed straight into ``target``,
        so a mid-copy failure (disk full, process kill, source lock storm)
        left a truncated database at the final path while the previous good
        backup was already gone.  The snapshot is now built in
        ``<target>.<stamp>.tmp`` and only published with an atomic rename
        after the bytes are fsynced; on any failure the temporary file is
        removed and the previous backup stays untouched.
        """
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        temporary = target.with_name(f"{target.name}.{stamp}.tmp")
        success = False
        try:
            destination = sqlite3.connect(str(temporary), timeout=30.0)
            try:
                destination.execute("PRAGMA busy_timeout=30000")
                for attempt in range(max_retries):
                    try:
                        self.conn.backup(destination, pages=250, sleep=0.01)
                        destination.commit()
                        break
                    except sqlite3.OperationalError as exc:
                        if (
                            "locked" in str(exc).lower()
                            or "busy" in str(exc).lower()
                        ) and attempt < max_retries - 1:
                            time.sleep(0.1 * (2**attempt))
                            continue
                        raise
            finally:
                destination.close()
            # Durable publish: flush the completed snapshot bytes before the
            # atomic rename, mirroring BackupService._durable_replace.
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            temporary.replace(target)
            success = True
        finally:
            if not success:
                temporary.unlink(missing_ok=True)

    def close(self) -> None:
        self.conn.close()


class _ThreadOwnedBackupRepo:
    """Small repository surface required by :class:`BackupService`."""

    def __init__(self, database_file: Path) -> None:
        self.db = _ThreadOwnedBackupDatabase(database_file)

    def setting(self, key: str, default: str = "", max_retries: int = 4) -> str:
        for attempt in range(max_retries):
            try:
                row = self.db.conn.execute(
                    "SELECT value FROM settings WHERE key=?",
                    (str(key),),
                ).fetchone()
                return str(row["value"]) if row is not None else str(default)
            except sqlite3.OperationalError as exc:
                if (
                    "locked" in str(exc).lower() or "busy" in str(exc).lower()
                ) and attempt < max_retries - 1:
                    time.sleep(0.05 * (2**attempt))
                    continue
                raise

    def set_setting(self, key: str, value: str, max_retries: int = 4) -> None:
        for attempt in range(max_retries):
            try:
                self.db.conn.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                    (str(key), str(value)),
                )
                self.db.conn.commit()
                return
            except sqlite3.OperationalError as exc:
                if (
                    "locked" in str(exc).lower() or "busy" in str(exc).lower()
                ) and attempt < max_retries - 1:
                    time.sleep(0.05 * (2**attempt))
                    continue
                raise

    def close(self) -> None:
        self.db.close()


class BackupService:
    """Coordinate database copies without adding file policy to ``Repo``."""

    REQUIRED_RESTORE_TABLES = frozenset({"settings", "categories", "transactions"})
    ENCRYPTED_MAGIC = b"LGBK1"
    ENCRYPTED_SALT_SIZE = 16
    ENCRYPTED_NONCE_SIZE = 12
    ENCRYPTED_TAG_SIZE = 16
    ENCRYPTED_CHUNK_SIZE = 1024 * 1024
    _LOCK_REGISTRY_GUARD = threading.Lock()
    _OPERATION_LOCKS: dict[str, threading.RLock] = {}

    def __init__(
        self,
        repo: "Repo",
        *,
        backup_directory: Optional[Path] = None,
        database_file: Optional[Path] = None,
        preferences_file: Optional[Path] = None,
        attachments_directory: Optional[Path] = None,
        history_directory: Optional[Path] = None,
        secret_store: Optional[SecretStore] = None,
        password_history_store: Optional[SecretStore] = None,
    ) -> None:
        self.repo = repo
        default_database = Path(
            database_file
            or getattr(getattr(repo, "db", None), "path", "")
            or paths_for_database().database
        )
        resolved_paths = paths_for_database(default_database)
        self.database_file = default_database
        self.preferences_file = Path(
            preferences_file or resolved_paths.backup_preferences
        )
        self.configuration_directory = Path(resolved_paths.configuration)
        self.default_backup_directory = Path(resolved_paths.backups)
        self.primary_directory_error = ""
        stored_preferences = self._read_preferences_path(self.preferences_file)
        configured_primary = str(
            stored_preferences.get("primary_backup_directory") or ""
        ).strip()
        self._configured_primary_directory = configured_primary
        requested_backup_directory = Path(
            backup_directory or configured_primary or self.default_backup_directory
        ).expanduser()
        try:
            requested_backup_directory = requested_backup_directory.resolve()
            requested_backup_directory.mkdir(parents=True, exist_ok=True)
            self.backup_directory = requested_backup_directory
        except OSError as exc:
            if backup_directory is not None:
                raise
            self.primary_directory_error = (
                "A pasta principal escolhida não está disponível; os backups "
                "estão temporariamente a usar a localização segura predefinida."
            )
            log_warning(
                "Configured primary backup directory %s is unavailable: %s",
                requested_backup_directory,
                exc,
            )
            self.backup_directory = self.default_backup_directory.resolve()
            self.backup_directory.mkdir(parents=True, exist_ok=True)
        self.attachments_directory = Path(
            attachments_directory or resolved_paths.attachments
        )
        self.history_directory = Path(history_directory or resolved_paths.history)
        self.protection_directory = resolved_paths.protections
        self.data_root = resolved_paths.data
        lock_key = str(self.database_file.expanduser().resolve()).casefold()
        with self._LOCK_REGISTRY_GUARD:
            self._operation_lock = self._OPERATION_LOCKS.setdefault(
                lock_key,
                threading.RLock(),
            )
        self.secret_store = secret_store
        self.password_history_store = password_history_store
        if self.secret_store is None:
            try:
                self.secret_store = default_secret_store(BACKUP_CREDENTIAL_TARGET)
            except SecretStoreError as exc:
                log_warning("Secure backup password store is unavailable: %s", exc)
        if self.password_history_store is None:
            try:
                self.password_history_store = default_secret_store(
                    BACKUP_PASSWORD_HISTORY_TARGET
                )
            except SecretStoreError as exc:
                log_warning("Secure backup password history is unavailable: %s", exc)
        self.backup_directory.mkdir(parents=True, exist_ok=True)
        self.history_directory.mkdir(parents=True, exist_ok=True)
        with self._operation_lock:
            cleanup_private_work(
                self.backup_directory,
                legacy_patterns=(
                    ".encrypted_export_*.zip",
                    ".encrypted_complete_*.zip",
                    "restore_temp_*",
                    "financas-restore-inspect-*",
                    "financas-startup-recovery-*",
                    ".*.encrypting",
                    ".*.decrypting",
                    ".*.creating",
                    ".*.source.sqlite3",
                    ".*.copying",
                    ".*.restore.tmp",
                    ".*.startup-restore.tmp",
                    ".*.startup-restore.previous",
                    ".*.rollback.tmp",
                ),
            )

            cleanup_private_work(system_private_parent())
            cleanup_private_work(
                Path(tempfile.gettempdir()),
                legacy_patterns=(
                    "financas-backup-check-*",
                    "financas-encrypted-check-*",
                    "financas-recovery-drill-*",
                ),
            )

    def _guard(
        self,
        database_file: Optional[Path] = None,
        *,
        protection_password: str = "",
    ) -> DataProtectionGuard:
        from database.engine import FinanceDB

        # Supported capabilities belong to this executable, not to whatever
        # version the current (possibly legacy/corrupt) profile declares.
        schema = FinanceDB.CURRENT_SCHEMA_VERSION
        return DataProtectionGuard(
            database_file or self.database_file,
            supported_schema_version=max(0, schema),
            current_app_version=APP_VERSION,
            attachments_directory=self.attachments_directory,
            history_directory=self.history_directory,
            preferences_file=self.preferences_file,
            protection_directory=self.protection_directory,
            protection_password=str(protection_password or ""),
        )

    @staticmethod
    def _encryption_primitives():
        try:
            from cryptography.exceptions import InvalidTag
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        except ImportError as exc:
            raise RuntimeError(
                "A cifragem de backups requer a biblioteca cryptography."
            ) from exc
        return InvalidTag, hashes, Cipher, algorithms, modes, PBKDF2HMAC

    @classmethod
    def encryption_available(cls) -> bool:
        try:
            cls._encryption_primitives()
            return True
        except (RuntimeError):
            return False

    def stored_backup_password(self) -> str:
        if self.secret_store is None:
            return ""
        try:
            return str(self.secret_store.read() or "")
        except SecretStoreError as exc:
            raise RuntimeError(
                "Não foi possível ler a palavra-passe de backup do Gestor de Credenciais."
            ) from exc

    def has_backup_password(self) -> bool:
        return bool(self.stored_backup_password())

    def save_backup_password(self, password: str) -> None:
        value = str(password or "")
        if len(value) < 8:
            raise ValueError("A palavra-passe deve ter pelo menos 8 caracteres.")
        if self.secret_store is None:
            raise RuntimeError(
                "O armazenamento seguro de palavras-passe não está disponível."
            )
        existing = self.stored_backup_password()
        if existing and not hmac.compare_digest(existing, value):
            raise RuntimeError(
                "Usa a alteração protegida da palavra-passe para manter acesso "
                "aos backups anteriores."
            )
        try:
            self.secret_store.write(value)
        except SecretStoreError as exc:
            raise RuntimeError(
                "Não foi possível guardar a palavra-passe no Gestor de Credenciais."
            ) from exc

    MAX_LEGACY_PASSWORD_HISTORY = 10
    MAX_LEGACY_PASSWORD_HISTORY_BYTES = 2048

    def _stored_previous_backup_passwords(self) -> list[str]:
        if self.password_history_store is None:
            return []
        try:
            raw = str(self.password_history_store.read() or "")
        except SecretStoreError as exc:
            raise RuntimeError(
                "Não foi possível ler o histórico seguro das palavras-passe."
            ) from exc
        if not raw:
            return []
        try:
            values = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            values = [raw]
        if not isinstance(values, list):
            return []
        return list(
            dict.fromkeys(
                str(value)
                for value in values
                if isinstance(value, str) and len(value) >= 8
            )
        )[:self.MAX_LEGACY_PASSWORD_HISTORY]

    def backup_password_candidates(self) -> list[str]:
        current = self.stored_backup_password()
        return list(
            dict.fromkeys(
                [
                    value
                    for value in [current, *self._stored_previous_backup_passwords()]
                    if value
                ]
            )
        )[: 1 + self.MAX_LEGACY_PASSWORD_HISTORY]

    def rotate_backup_password(
        self,
        old_password: str,
        new_password: str,
        *,
        preference_values: Optional[
            tuple[int, int, bool, bool, bool, str]
            | tuple[int, int, bool, bool, bool, str, str]
        ] = None,
    ) -> Path:
        """Validate, protect and commit a password change in the safe order.

        .. deprecated:: v3.50
            Legacy LGBK1 path. When a startup-security session owns the
            envelope, use :meth:`StartupSecurityService.change_credential`
            instead; calling this method in that state raises.
        """

        from services.startup_security import get_active_session

        if get_active_session() is not None:
            raise BackupCryptoError(
                "A alteração de palavra-passe é gerida pela segurança de "
                "arranque (envelope). Usa a alteração de credencial nas "
                "Definições."
            )
        new_value = str(new_password or "")
        if len(new_value) < 8:
            raise ValueError("A nova palavra-passe deve ter pelo menos 8 caracteres.")
        with self._operation_lock:
            current = self.stored_backup_password()
            if current and not hmac.compare_digest(current, str(old_password or "")):
                raise ValueError("A palavra-passe atual não está correta.")
            if current and hmac.compare_digest(current, new_value):
                raise ValueError("A nova palavra-passe tem de ser diferente da atual.")
            if current:
                encrypted_backups = sorted(
                    self.backup_directory.glob("financas_backup_*.lgbak"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                if encrypted_backups:
                    self.validate_backup_file(
                        encrypted_backups[0],
                        password=current,
                    )
                if self.password_history_store is None:
                    raise RuntimeError(
                        "O cofre seguro para manter acesso aos backups antigos não "
                        "está disponível; a palavra-passe não foi alterada."
                    )

            old_preferences = self.preferences()
            old_history = self._stored_previous_backup_passwords()
            if current:
                prospective_history = list(dict.fromkeys([current, *old_history]))[
                    : self.MAX_LEGACY_PASSWORD_HISTORY
                ]
                serialized_history = json.dumps(
                    prospective_history, ensure_ascii=False
                )
                if (
                    len(serialized_history.encode("utf-8"))
                    > self.MAX_LEGACY_PASSWORD_HISTORY_BYTES
                ):
                    raise ValueError(
                        "O histórico de palavras-passe de backup excedeu o limite máximo seguro de retenção."
                    )

            stamp = backup_stamp()
            target = self.backup_directory / (
                f"financas_backup_rotacao_password_{stamp}.lgbak"
            )
            sequence = 1
            while target.exists():
                target = self.backup_directory / (
                    f"financas_backup_rotacao_password_{stamp}_{sequence}.lgbak"
                )
                sequence += 1
            self._create_encrypted_complete_backup(
                target,
                password=new_value,
                purpose="password_rotation",
            )
            self.validate_backup_file(target, password=new_value)
            try:
                if preference_values is not None:
                    self.save_preferences(*preference_values)
                if current:
                    history = list(dict.fromkeys([current, *old_history]))[
                        : self.MAX_LEGACY_PASSWORD_HISTORY
                    ]
                    assert self.password_history_store is not None
                    self.password_history_store.write(
                        json.dumps(history, ensure_ascii=False)
                    )
                if self.secret_store is None:
                    raise RuntimeError(
                        "O armazenamento seguro de palavras-passe não está disponível."
                    )
                self.secret_store.write(new_value)
            except (Exception):
                if preference_values is not None:
                    try:
                        self.save_preferences(
                            int(
                                old_preferences.get(
                                    "auto_backup_interval_minutes", 1440
                                )
                            ),
                            int(old_preferences.get("backup_keep", 15)),
                            bool(old_preferences.get("backup_on_close", False)),
                            bool(
                                old_preferences.get("backup_encryption_enabled", False)
                            ),
                            bool(
                                old_preferences.get("secondary_backup_enabled", False)
                            ),
                            str(
                                old_preferences.get("secondary_backup_directory") or ""
                            ),
                            str(old_preferences.get("primary_backup_directory") or ""),
                        )
                    except Exception as rollback_error:
                        log_exception(
                            "Failed to roll back backup preferences after password rotation",
                            rollback_error,
                        )
                if current and self.secret_store is not None:
                    try:
                        self.secret_store.write(current)
                    except (SecretStoreError):
                        pass
                if self.password_history_store is not None:
                    try:
                        if old_history:
                            self.password_history_store.write(
                                json.dumps(old_history, ensure_ascii=False)
                            )
                        else:
                            self.password_history_store.delete()
                    except (SecretStoreError):
                        pass
                try:
                    target.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    log_exception(
                        "Failed to remove rejected password-rotation backup",
                        cleanup_error,
                    )
                raise
            try:
                self._record_verified_backup(target)
            except Exception as exc:
                # The password, preferences and validated recovery point are already
                # committed. A secondary status write must not turn that successful
                # rotation into a misleading error that invites the user to retry it.
                log_exception(
                    "Password rotated but backup verification metadata was not updated",
                    exc,
                )
            return target

    @staticmethod
    def _sync_file(path: Path) -> None:
        """Force file contents from the operating-system cache to durable storage."""

        # Windows' FlushFileBuffers requires a handle opened with write access;
        # ``rb`` produces EBADF on current CPython/Windows even for os.fsync().
        with Path(path).open("r+b") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _sync_directory(path: Path) -> None:
        """Persist a replaced directory entry where the platform supports it."""

        if os.name == "nt":
            # Python cannot open a Windows directory with the semantics required
            # by FlushFileBuffers. The file itself is still fsynced before replace.
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(Path(path), flags)
            os.fsync(descriptor)
        except OSError as exc:
            # The content was already synced and atomically published. Some file
            # systems do not expose directory fsync, so record rather than report
            # a false backup failure after the replacement has succeeded.
            log_warning("Could not sync backup directory %s: %s", path, exc)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @classmethod
    def _ensure_writable(cls, path: Path) -> None:
        try:
            if path.exists():
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass

    @classmethod
    def _durable_replace(cls, source: Path, target: Path) -> None:
        """Publish a completed file only after its bytes are physically flushed."""

        source = Path(source)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        cls._ensure_writable(target)
        cls._ensure_writable(source)
        cls._sync_file(source)
        try:
            os.replace(source, target)
        except OSError as exc:
            cls._ensure_writable(target)
            cls._ensure_writable(source)
            if getattr(exc, "winerror", 0) in (5, 17) or getattr(exc, "errno", 0) in (13, 18, 22):
                if getattr(exc, "winerror", 0) == 5 or getattr(exc, "errno", 0) == 13:
                    try:
                        os.replace(source, target)
                        cls._sync_directory(target.parent)
                        return
                    except OSError:
                        pass
                same_volume_temp = target.with_name(f".{target.name}.xdev-stage.{os.getpid()}")
                try:
                    shutil.copyfile(source, same_volume_temp)
                    cls._sync_file(same_volume_temp)
                    cls._ensure_writable(target)
                    os.replace(same_volume_temp, target)
                finally:
                    cls._ensure_writable(same_volume_temp)
                    cls._ensure_writable(source)
                    same_volume_temp.unlink(missing_ok=True)
                    source.unlink(missing_ok=True)
            else:
                raise
        cls._sync_directory(target.parent)

    @classmethod
    def _derive_encryption_key(cls, password: str, salt: bytes) -> bytearray:
        _invalid_tag, hashes, _cipher, _algorithms, _modes, pbkdf2 = (
            cls._encryption_primitives()
        )
        pwd_bytes = bytearray(str(password).encode("utf-8"))
        try:
            derivation = pbkdf2(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=390_000,
            )
            return bytearray(derivation.derive(bytes(pwd_bytes)))
        finally:
            from utils.output_security import secure_zero

            secure_zero(pwd_bytes)

    @classmethod
    def is_encrypted_backup(cls, path: Path) -> bool:
        try:
            with Path(path).open("rb") as handle:
                return handle.read(len(cls.ENCRYPTED_MAGIC)) in {
                    cls.ENCRYPTED_MAGIC,
                    LGBK2_MAGIC,
                }
        except (OSError):
            return False

    @classmethod
    def encrypted_payload_kind(cls, path: Path) -> str:
        from services.backup_crypto import BackupCryptoCodec

        path = Path(path)
        with path.open("rb") as handle:
            magic = handle.read(len(cls.ENCRYPTED_MAGIC))
            if magic not in {cls.ENCRYPTED_MAGIC, LGBK2_MAGIC}:
                raise ValueError("O ficheiro não é um backup cifrado desta aplicação.")
            kind = handle.read(1)
        if kind not in {b"S", b"Z"}:
            raise ValueError("O tipo de conteúdo do backup cifrado é inválido.")
        if magic == LGBK2_MAGIC:
            # Cross-check against the portable LGBK2 header.
            header = BackupCryptoCodec.inspect_header(path)
            if header.payload_kind != kind.decode("ascii"):
                raise ValueError("O tipo de conteúdo do backup cifrado é inválido.")
        return kind.decode("ascii")

    @classmethod
    def encrypt_file(
        cls,
        source: Path,
        target: Path,
        password: str,
        *,
        payload_kind: str,
    ) -> None:
        from utils.output_security import secure_zero

        value = str(password or "")
        if len(value) < 8:
            raise ValueError("A palavra-passe deve ter pelo menos 8 caracteres.")
        kind = str(payload_kind or "").upper().encode("ascii", errors="ignore")[:1]
        if kind not in {b"S", b"Z"}:
            raise ValueError("Tipo de backup cifrado inválido.")
        _invalid_tag, _hashes, cipher_type, algorithms, modes, _pbkdf2 = (
            cls._encryption_primitives()
        )
        salt = os.urandom(cls.ENCRYPTED_SALT_SIZE)
        nonce = os.urandom(cls.ENCRYPTED_NONCE_SIZE)
        key = cls._derive_encryption_key(value, salt)
        aad = cls.ENCRYPTED_MAGIC + kind
        try:
            encryptor = cipher_type(
                algorithms.AES(bytes(key)), modes.GCM(nonce)
            ).encryptor()
            encryptor.authenticate_additional_data(aad)
            target = Path(target)
            temporary = target.with_name(f".{target.name}.encrypting")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with (
                    Path(source).open("rb") as source_handle,
                    temporary.open("wb") as target_handle,
                ):
                    target_handle.write(aad + salt + nonce)
                    while chunk := source_handle.read(cls.ENCRYPTED_CHUNK_SIZE):
                        target_handle.write(encryptor.update(chunk))
                    target_handle.write(encryptor.finalize())
                    target_handle.write(encryptor.tag)
                cls._durable_replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            secure_zero(key)

    @classmethod
    def decrypt_file(cls, source: Path, target: Path, password: str) -> str:
        from utils.output_security import secure_zero

        invalid_tag, _hashes, cipher_type, algorithms, modes, _pbkdf2 = (
            cls._encryption_primitives()
        )
        source = Path(source)
        header_size = (
            len(cls.ENCRYPTED_MAGIC)
            + 1
            + cls.ENCRYPTED_SALT_SIZE
            + cls.ENCRYPTED_NONCE_SIZE
        )
        source_size = source.stat().st_size
        if source_size <= header_size + cls.ENCRYPTED_TAG_SIZE:
            raise ValueError("O backup cifrado está vazio ou incompleto.")
        if (
            source_size
            > MAX_ARCHIVE_UNCOMPRESSED_BYTES + header_size + cls.ENCRYPTED_TAG_SIZE
        ):
            raise ValueError("O backup cifrado excede o limite seguro de 5 GB.")
        target_parent = Path(target).parent
        target_parent.mkdir(parents=True, exist_ok=True)
        if (
            shutil.disk_usage(target_parent).free
            < source_size + MIN_ARCHIVE_FREE_SPACE_BUFFER
        ):
            raise ValueError(
                "Não existe espaço livre suficiente para decifrar o backup."
            )
        temporary = Path(target).with_name(f".{Path(target).name}.decrypting")
        key: bytearray | None = None
        try:
            with source.open("rb") as source_handle:
                magic = source_handle.read(len(cls.ENCRYPTED_MAGIC))
                kind = source_handle.read(1)
                if magic != cls.ENCRYPTED_MAGIC or kind not in {b"S", b"Z"}:
                    raise ValueError(
                        "O ficheiro não é um backup cifrado desta aplicação."
                    )
                salt = source_handle.read(cls.ENCRYPTED_SALT_SIZE)
                nonce = source_handle.read(cls.ENCRYPTED_NONCE_SIZE)
                source_handle.seek(-cls.ENCRYPTED_TAG_SIZE, os.SEEK_END)
                tag = source_handle.read(cls.ENCRYPTED_TAG_SIZE)
                ciphertext_end = source.stat().st_size - cls.ENCRYPTED_TAG_SIZE
                source_handle.seek(header_size)
                key = cls._derive_encryption_key(str(password or ""), salt)
                decryptor = cipher_type(
                    algorithms.AES(bytes(key)),
                    modes.GCM(nonce, tag),
                ).decryptor()
                decryptor.authenticate_additional_data(magic + kind)
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                with temporary.open("wb") as target_handle:
                    remaining = ciphertext_end - header_size
                    while remaining > 0:
                        chunk = source_handle.read(
                            min(cls.ENCRYPTED_CHUNK_SIZE, remaining)
                        )
                        if not chunk:
                            raise ValueError("O backup cifrado está incompleto.")
                        remaining -= len(chunk)
                        target_handle.write(decryptor.update(chunk))
                    target_handle.write(decryptor.finalize())
            cls._durable_replace(temporary, target)
            return kind.decode("ascii")
        except invalid_tag as exc:
            raise ValueError(
                "Palavra-passe incorreta ou backup cifrado danificado."
            ) from exc
        finally:
            if key is not None:
                secure_zero(key)
            temporary.unlink(missing_ok=True)

    def _decrypt_with_available_passwords(
        self,
        source: Path,
        target: Path,
        password: Optional[str] = None,
    ) -> str:
        """Open old encrypted backups without exposing archived credentials."""

        from services.backup_crypto import BackupCryptoCodec, LGBK2_MAGIC

        if BackupCryptoCodec.detect_magic(Path(source)) == LGBK2_MAGIC:
            return self._decrypt_lgbk2_with_fallbacks(source, target, password)
        explicit = str(password or "")
        candidates = [explicit] if explicit else self.backup_password_candidates()
        if not candidates:
            candidates = [self._required_backup_password()]
        last_error: Optional[ValueError] = None
        for candidate in candidates:
            try:
                return self.decrypt_file(source, target, candidate)
            except ValueError as exc:
                last_error = exc
                Path(target).unlink(missing_ok=True)
        if explicit and last_error is not None:
            raise last_error
        raise ValueError(
            "Nenhuma das palavras-passe guardadas consegue abrir este backup."
        ) from last_error

    def _decrypt_lgbk2_with_fallbacks(
        self,
        source: Path,
        target: Path,
        password: Optional[str] = None,
    ) -> str:
        """Decrypt LGBK2 via the unlocked session, then via credentials."""

        from services.backup_crypto import BackupCryptoCodec
        from services.startup_security import get_active_session

        codec = BackupCryptoCodec
        explicit = str(password or "")
        last_error: Optional[ValueError] = None
        session = get_active_session()
        if session is not None and not explicit:
            try:
                return codec.decrypt(
                    source,
                    target,
                    session_master_key=session.backup_master_key,
                )
            except ValueError as exc:
                last_error = exc
                Path(target).unlink(missing_ok=True)
        candidates = [explicit] if explicit else self.backup_password_candidates()
        for candidate in candidates:
            try:
                return codec.decrypt(
                    source,
                    target,
                    credential=candidate.encode("utf-8"),
                )
            except ValueError as exc:
                last_error = exc
                Path(target).unlink(missing_ok=True)
        if explicit and last_error is not None:
            raise last_error
        raise ValueError(
            "Nenhuma das credenciais guardadas consegue abrir este backup."
        ) from last_error

    @staticmethod
    def scrub_ai_secret_from_sqlite_file(path: Path) -> bool:
        """Remove the legacy plaintext Gemini key from a SQLite copy."""
        path = Path(path)
        connection = sqlite3.connect(str(path))
        try:
            settings_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
            ).fetchone()
            if not settings_exists:
                return False
            exists = connection.execute(
                "SELECT 1 FROM settings WHERE key='ai_gemini_api_key' AND value<>''"
            ).fetchone()
            if not exists:
                return False
            connection.execute("DELETE FROM settings WHERE key='ai_gemini_api_key'")
            connection.commit()
            return True
        finally:
            connection.close()

    @classmethod
    def scrub_ai_secret_from_zip_file(cls, path: Path) -> bool:
        """Remove the legacy key from SQLite and JSON entries inside a ZIP."""
        path = Path(path)
        if not path.is_file():
            return False
        with private_workspace(path.parent, prefix="sanitize-zip") as workspace:
            temporary_zip = workspace / "sanitized.zip"
            temporary_db = workspace / "sanitized.sqlite3"
            changed = False
            try:
                with zipfile.ZipFile(path, "r") as source:
                    from database.protection import (
                        DataIntegrityError,
                        validate_zip_safety,
                    )

                    try:
                        validate_zip_safety(source, extraction_root=workspace)
                    except DataIntegrityError as exc:
                        log_warning(
                            "Ignorado arquivo ZIP inseguro %s: %s", path.name, exc
                        )
                        return False

                    members = source.namelist()
                    is_app_archive = any(
                        name.lower().endswith(
                            (".sqlite3", ".sqlite", ".db", "metadata.json")
                        )
                        for name in members
                    )
                    if not is_app_archive:
                        return False

                    with zipfile.ZipFile(
                        temporary_zip,
                        "w",
                        compression=zipfile.ZIP_DEFLATED,
                        compresslevel=9,
                    ) as target:
                        for info in source.infolist():
                            if info.is_dir():
                                target.writestr(info, b"")
                                continue
                            data = source.read(info.filename)
                            lower_name = info.filename.lower()
                            if lower_name.endswith((".sqlite3", ".sqlite", ".db")):
                                temporary_db.write_bytes(data)
                                try:
                                    if cls.scrub_ai_secret_from_sqlite_file(temporary_db):
                                        data = temporary_db.read_bytes()
                                        changed = True
                                finally:
                                    temporary_db.unlink(missing_ok=True)
                            elif lower_name.endswith(".json"):
                                try:
                                    payload = json.loads(data.decode("utf-8"))
                                    tables = (
                                        payload.get("tables")
                                        if isinstance(payload, dict)
                                        else None
                                    )
                                    settings = (
                                        tables.get("settings")
                                        if isinstance(tables, dict)
                                        else None
                                    )
                                    if isinstance(settings, list):
                                        filtered = [
                                            row
                                            for row in settings
                                            if not (
                                                isinstance(row, dict)
                                                and row.get("key") == "ai_gemini_api_key"
                                            )
                                        ]
                                        if len(filtered) != len(settings):
                                            tables["settings"] = filtered
                                            data = json.dumps(
                                                payload,
                                                ensure_ascii=False,
                                                indent=2,
                                            ).encode("utf-8")
                                            changed = True
                                    elif (
                                        isinstance(settings, dict)
                                        and "ai_gemini_api_key" in settings
                                    ):
                                        settings.pop("ai_gemini_api_key", None)
                                        data = json.dumps(
                                            payload,
                                            ensure_ascii=False,
                                            indent=2,
                                        ).encode("utf-8")
                                        changed = True
                                except (UnicodeDecodeError, json.JSONDecodeError):
                                    pass
                            target.writestr(info, data)
                if changed:
                    cls._durable_replace(temporary_zip, path)
                return changed
            except zipfile.BadZipFile:
                log_warning("Ficheiro ZIP corrompido: %s", path.name)
                return False

    def scrub_ai_secret_from_backups(
        self,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict[str, Any]:
        """Sanitize existing local backups without exposing their contents."""
        scrubbed = 0
        failed = 0
        sqlite_files = list(self.backup_directory.glob("*.sqlite3"))
        zip_files = list(self.backup_directory.glob("*.zip"))
        total = len(sqlite_files) + len(zip_files)
        current = 0
        for path in sqlite_files:
            if cancel_check and cancel_check():
                return {
                    "scrubbed": scrubbed,
                    "failed": failed,
                    "cancelled": True,
                    "total": total,
                    "processed": current,
                }
            try:
                scrubbed += int(self.scrub_ai_secret_from_sqlite_file(path))
            except Exception as exc:
                failed += 1
                log_warning("Failed to sanitize SQLite backup %s: %s", path.name, exc)
            current += 1
            if progress_callback:
                progress_callback(current, total, path.name)
        for path in zip_files:
            if cancel_check and cancel_check():
                return {
                    "scrubbed": scrubbed,
                    "failed": failed,
                    "cancelled": True,
                    "total": total,
                    "processed": current,
                }
            try:
                scrubbed += int(self.scrub_ai_secret_from_zip_file(path))
            except Exception as exc:
                failed += 1
                log_warning("Failed to sanitize ZIP backup %s: %s", path.name, exc)
            current += 1
            if progress_callback:
                progress_callback(current, total, path.name)
        return {
            "scrubbed": scrubbed,
            "failed": failed,
            "cancelled": False,
            "total": total,
            "processed": current,
        }

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _create_complete_zip(self, target: Path, *, purpose: str) -> Path:
        """Create one verified bundle with DB, documents, undo and preferences."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        db_path = getattr(getattr(self.repo, "db", None), "path", None)
        db_size = (
            Path(db_path).stat().st_size
            if db_path and Path(db_path).is_file()
            else 1024 * 1024
        )
        scope_size = sum(
            item.stat().st_size
            for folder in (self.attachments_directory, self.history_directory)
            if folder.exists()
            for item in folder.rglob("*")
            if item.is_file()
        )
        if self.preferences_file.is_file():
            scope_size += self.preferences_file.stat().st_size
        wal = Path(f"{self.database_file}-wal")
        if wal.is_file():
            db_size += wal.stat().st_size
        # Uncompressed upper bound covers the DB snapshot, archive staging and
        # an encrypted output coexisting on this destination volume.
        required_free = MIN_ARCHIVE_FREE_SPACE_BUFFER + 3 * (db_size + scope_size)
        if shutil.disk_usage(target.parent).free < required_free:
            raise DataIntegrityError(
                "Espaço em disco insuficiente no destino para criar o backup em segurança."
            )
        temporary_database = target.parent / (
            f".{target.stem}.{backup_stamp()}.source.sqlite3"
        )
        staged_archive = target.with_name(
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.creating"
        )
        try:
            self.repo.db.backup_to(temporary_database)

            self.scrub_ai_secret_from_sqlite_file(temporary_database)
            connection = sqlite3.connect(str(temporary_database))
            try:
                DataProtectionGuard.validate_connection(connection, full=True)
                guard = self._guard()
                tables = guard.capture_tables(connection)
                schema = guard.schema_version(connection)
                source_version = guard.setting_value(
                    connection,
                    "data_guard_app_version",
                )
                guard.create_snapshot(
                    connection,
                    source_app_version=source_version or APP_VERSION,
                    source_schema_version=schema,
                    tables=tables,
                    files=guard.capture_files(),
                    target_path=staged_archive,
                    purpose=purpose,
                )
            finally:
                connection.close()
            self.validate_complete_archive(staged_archive)
            self._durable_replace(staged_archive, target)
            self.validate_complete_archive(target)
            return target
        finally:
            temporary_database.unlink(missing_ok=True)
            staged_archive.unlink(missing_ok=True)

    @classmethod
    def validate_complete_archive(cls, path: Path) -> dict[str, Any]:
        """Verify hashes and run a full SQLite recovery check without restoring."""
        path = Path(path)
        manifest = DataProtectionGuard.validate_archive(path)
        scopes = set(manifest.get("included_scopes") or ())
        required_scopes = {"database", "attachments", "history", "preferences"}
        # v3.1 protection archives already contained these four scopes but did
        # not yet name them explicitly in the manifest.
        if not scopes and int(manifest.get("format_version") or 0) == 1:
            scopes = set(required_scopes)
        if not required_scopes.issubset(scopes):
            raise DataIntegrityError(
                "O backup não declara todos os âmbitos obrigatórios: base de "
                "dados, documentos, histórico e preferências."
            )
        with private_workspace(
            system_private_parent(),
            prefix="backup-check",
        ) as temporary:
            database = manifest.get("database") or {}
            archive_name = str(
                database.get("archive_name") or "financas_pessoais.sqlite3"
            )
            extracted = temporary / "financas_pessoais.sqlite3"
            with zipfile.ZipFile(path, "r") as archive:
                validate_zip_safety(archive, extraction_root=temporary)
                with (
                    archive.open(archive_name) as source,
                    extracted.open("wb") as destination,
                ):
                    shutil.copyfileobj(source, destination)
            cls.validate_restore_database(extracted)
        return manifest

    @staticmethod
    def calculate_backup_efficiency(
        source_db_size: int,
        backup_file_size: int,
    ) -> dict[str, Any]:
        """Calculate compression efficiency, byte reduction and savings ratio."""
        source = max(0, int(source_db_size))
        backup = max(0, int(backup_file_size))
        freed = max(0, source - backup)
        reduction_pct = round((freed / source) * 100.0, 2) if source > 0 else 0.0
        ratio = round(source / backup, 2) if backup > 0 else 1.0
        return {
            "source_bytes": source,
            "backup_bytes": backup,
            "freed_bytes": freed,
            "reduction_pct": reduction_pct,
            "compression_ratio": ratio,
        }

    def validate_backup_file(
        self,
        path: Path,
        *,
        password: Optional[str] = None,
    ) -> dict[str, Any]:
        source = Path(path)
        if self.is_encrypted_backup(source):
            with private_workspace(
                self.backup_directory,
                prefix="encrypted-check",
            ) as temporary:
                decrypted = temporary / "backup.zip"
                kind = self._decrypt_with_available_passwords(
                    source,
                    decrypted,
                    password,
                )
                if kind != "Z":
                    self.validate_restore_database(decrypted)
                    return {"format_version": 0, "kind": "S"}
                return self.validate_backup_file(decrypted)
        if source.suffix.lower() == ".zip":
            if self._has_protection_manifest(source):
                return self.validate_complete_archive(source)
            with private_workspace(
                self.backup_directory,
                prefix="legacy-check",
            ) as temporary:
                db_extracted, _att = self._extract_restore_zip(source, temporary)
                self.validate_restore_database(db_extracted)
                return {"format_version": 0, "kind": "legacy_zip"}
        elif source.suffix.lower() in {".sqlite3", ".db"}:
            self.validate_restore_database(source)
            return {"format_version": 0, "kind": "sqlite3"}
        return self.validate_complete_archive(source)

    def export_backup_zip(self, target: Path) -> Path:
        with self._operation_lock:
            return self._create_complete_zip(Path(target), purpose="manual_export")

    def export_encrypted_backup(
        self,
        target: Path,
        password: Optional[str] = None,
    ) -> Path:
        target = Path(target)
        with self._operation_lock:
            return self._create_encrypted_complete_backup(
                target,
                password=password,
                purpose="manual_encrypted_export",
            )

    def backup_sqlite(self, target: Path) -> None:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        db_path = getattr(getattr(self.repo, "db", None), "path", None)
        db_size = (
            Path(db_path).stat().st_size
            if db_path and Path(db_path).is_file()
            else 1024 * 1024
        )
        if shutil.disk_usage(target.parent).free < db_size + 10 * 1024 * 1024:
            raise DataIntegrityError(
                "Espaço em disco insuficiente no destino para criar o backup SQLite."
            )
        temporary = target.with_name(f".{target.name}.creating")
        try:
            self.repo.db.backup_to(temporary)
            self.scrub_ai_secret_from_sqlite_file(temporary)
            self._durable_replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _required_backup_password(self) -> str:
        password = self.stored_backup_password()
        if not password:
            raise RuntimeError(
                "Define primeiro uma palavra-passe de backup nas Definições."
            )
        return password

    def _encrypted_backups_enabled(self) -> bool:
        return bool(self.preferences().get("backup_encryption_enabled", False))

    def _encrypt_as_lgbk2(
        self,
        source_zip: Path,
        target: Path,
        payload_kind: str = "Z",
    ) -> bool:
        """Encrypt via the unlocked session's master key (LGBK2).

        Returns False only when no security session is available (the caller
        may then use the explicitly named LGBK1 legacy operation).  With a
        live session, any failure propagates: fail closed, never silently
        downgrade.  Never rewrites LGBK1 backups.
        """

        from services.backup_crypto import BackupCryptoCodec
        from services.startup_security import get_active_session

        session = get_active_session()
        if session is None:
            return False
        envelope = self._load_security_envelope_for_session(session)
        if envelope is None:
            return False
        BackupCryptoCodec.encrypt_v2(
            source_zip,
            target,
            master_key=session.backup_master_key,
            envelope=envelope,
            payload_kind=payload_kind,
        )
        return True

    def _load_security_envelope_for_session(self, session: Any) -> Any:
        from services.backup_crypto import read_envelope

        try:
            return read_envelope(session.envelope_path)
        except (OSError, ValueError) as exc:
            log_warning("Security envelope unavailable for LGBK2 encryption: %s", exc)
            return None

    def create_lgbk2_complete_backup(
        self,
        target: Path,
        *,
        purpose: str,
    ) -> Path:
        """Explicit LGBK2-session operation: encrypt with the live envelope.

        Fails closed: stages output in private workspace, validates it, and publishes
        atomically via _durable_replace. Pre-existing target files are never deleted or
        overwritten on failure. No automatic LGBK1 downgrade.
        """

        from services.backup_crypto import BackupCryptoError
        from services.backup_crypto import BackupCryptoCodec as _Codec
        from services.startup_security import get_active_session

        session = get_active_session()
        if session is None:
            raise BackupCryptoError(
                "Não há sessão desbloqueada para cifrar o backup com LGBK2."
            )
        envelope = self._load_security_envelope_for_session(session)
        if envelope is None:
            raise BackupCryptoError(
                "O envelope de segurança não está disponível para cifrar o backup."
            )
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        workspace_parent = (
            target.parent if target.parent.is_dir() else self.backup_directory
        )
        with private_workspace(
            workspace_parent,
            prefix="encrypted-complete",
        ) as temporary:
            temporary_zip = temporary / "payload.zip"
            staged_archive = temporary / f".{target.name}.staged.lgbak"
            try:
                self._create_complete_zip(temporary_zip, purpose=purpose)
                _Codec.encrypt_v2(
                    temporary_zip,
                    staged_archive,
                    master_key=session.backup_master_key,
                    envelope=envelope,
                    payload_kind="Z",
                )
                self.validate_backup_file(staged_archive)
                self._durable_replace(staged_archive, target)
                return target
            except Exception as exc:
                log_exception("LGBK2 backup failed closed", exc)
                raise BackupCryptoError(
                    "Não foi possível criar o backup cifrado. Nenhum ficheiro "
                    "foi publicado."
                ) from exc
            finally:
                temporary_zip.unlink(missing_ok=True)
                staged_archive.unlink(missing_ok=True)

    def create_lgbk1_legacy_backup(
        self,
        target: Path,
        *,
        password: str,
        purpose: str,
    ) -> Path:
        """Explicitly named LGBK1 legacy compatibility operation.

        Stages output in private workspace, validates it, and publishes
        atomically via _durable_replace.
        """

        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        workspace_parent = (
            target.parent if target.parent.is_dir() else self.backup_directory
        )
        with private_workspace(
            workspace_parent,
            prefix="encrypted-complete",
        ) as temporary:
            temporary_zip = temporary / "payload.zip"
            staged_archive = temporary / f".{target.name}.staged.lgbak"
            try:
                self._create_complete_zip(temporary_zip, purpose=purpose)
                self.encrypt_file(
                    temporary_zip,
                    staged_archive,
                    password,
                    payload_kind="Z",
                )
                self.validate_backup_file(staged_archive, password=password)
                self._durable_replace(staged_archive, target)
                return target
            finally:
                temporary_zip.unlink(missing_ok=True)
                staged_archive.unlink(missing_ok=True)

    def _create_encrypted_complete_backup(
        self,
        target: Path,
        *,
        password: Optional[str] = None,
        purpose: str,
    ) -> Path:
        """Dispatch encrypted backups; an explicit password is never ignored.

        With an active session: LGBK2, fail-closed, and any explicit password
        must be a live credential of the envelope or the operation is
        rejected.  Without a session: the explicitly named LGBK1 legacy path.
        """

        from services.startup_security import get_active_session

        session = get_active_session()
        if session is not None:
            if password is not None:
                from services.startup_security import StartupSecurityService

                probe = StartupSecurityService(database_file=Path(self.database_file))
                try:
                    probe.verify_credential_matches(password)
                except Exception as exc:
                    from services.backup_crypto import BackupCryptoError

                    raise BackupCryptoError(
                        "A palavra-passe indicada não corresponde à credencial "
                        "de segurança ativa. Usa a alteração de credencial para "
                        "a mudar."
                    ) from exc
            return self.create_lgbk2_complete_backup(target, purpose=purpose)
        value = password or self._required_backup_password()
        return self.create_lgbk1_legacy_backup(
            target,
            password=value,
            purpose=purpose,
        )

    def create_manual_backup(self) -> Path:
        with self._operation_lock:
            if self._encrypted_backups_enabled():
                target = (
                    self.backup_directory
                    / f"financas_backup_manual_{backup_stamp()}.lgbak"
                )
                self._create_encrypted_complete_backup(
                    target,
                    purpose="manual_backup",
                )
            else:
                target = (
                    self.backup_directory
                    / f"financas_backup_manual_{backup_stamp()}.zip"
                )
                self._create_complete_zip(target, purpose="manual_backup")
            self._record_verified_backup(target)
            self._mirror_to_secondary(target)
            return target

    def default_export_path(self) -> Path:
        suffix = ".lgbak" if self._encrypted_backups_enabled() else ".zip"
        return self.backup_directory / f"backup_liquid_{backup_stamp()}{suffix}"

    @classmethod
    def validate_restore_database(
        cls, source: Path, *, supported_schema_version: Optional[int] = None
    ) -> None:
        """Reject corrupt, empty or unrelated SQLite files before replacement."""
        source = Path(source)
        if not source.is_file() or source.stat().st_size < 100:
            raise ValueError("O backup não contém uma base de dados válida.")
        with source.open("rb") as handle:
            header = handle.read(16)
        if header != b"SQLite format 3\x00":
            raise ValueError("O ficheiro não é uma base de dados SQLite válida.")
        connection: Optional[sqlite3.Connection] = None
        try:
            uri = f"{source.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).casefold() != "ok":
                detail = str(integrity[0]) if integrity else "resultado desconhecido"
                raise ValueError(f"A base de dados do backup está corrompida: {detail}")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = sorted(cls.REQUIRED_RESTORE_TABLES - tables)
            if missing:
                raise ValueError(
                    "O ficheiro não parece ser um backup desta aplicação; "
                    f"faltam as tabelas: {', '.join(missing)}."
                )
            if supported_schema_version is None:
                from database.engine import FinanceDB

                supported_schema_version = FinanceDB.CURRENT_SCHEMA_VERSION
            validate_restore_compatibility(
                connection,
                supported_schema_version,
                required_tables=cls.REQUIRED_RESTORE_TABLES,
            )
            foreign_key_error = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchone()
            if foreign_key_error:
                raise ValueError("O backup contém referências de dados inválidas.")
        except sqlite3.DatabaseError as exc:
            raise ValueError(
                f"Não foi possível validar a base de dados: {exc}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @classmethod
    def _materialise_restore_database(
        cls,
        source: Path,
        target: Path,
        *,
        supported_schema_version: Optional[int] = None,
    ) -> Path:
        """Take a coherent committed snapshot, including an input's live WAL.

        The caller owns target's private staging directory. Opening the source
        read-only avoids checkpointing or deleting the user's source files;
        SQLite's backup protocol supplies one consistent committed view.
        """
        source, target = Path(source), Path(target)
        if source.resolve() == target.resolve():
            raise ValueError(
                "A cópia de preparação tem de usar um ficheiro separado da origem."
            )
        if not source.is_file() or source.stat().st_size < 100:
            raise ValueError("O backup não contém uma base de dados válida.")
        with source.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise ValueError("O ficheiro não é uma base de dados SQLite válida.")
        target.parent.mkdir(parents=True, exist_ok=True)
        wal = Path(f"{source}-wal")
        required = source.stat().st_size + (wal.stat().st_size if wal.is_file() else 0)
        if (
            shutil.disk_usage(target.parent).free
            < required + MIN_ARCHIVE_FREE_SPACE_BUFFER
        ):
            raise DataIntegrityError(
                "Espaço insuficiente para preparar uma cópia coerente do backup."
            )
        reader: Optional[sqlite3.Connection] = None
        writer: Optional[sqlite3.Connection] = None
        busy_since: Optional[float] = None

        def progress(status: int, _remaining: int, _total: int) -> None:
            nonlocal busy_since
            if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                busy_since = busy_since or time.monotonic()
                if time.monotonic() - busy_since >= 30.0:
                    raise DataIntegrityError(
                        "A origem continua ocupada; fecha a aplicação de origem e tenta novamente."
                    )
            else:
                busy_since = None

        try:
            reader = sqlite3.connect(
                f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0
            )
            writer = sqlite3.connect(target)
            reader.backup(writer, pages=256, progress=progress)
            writer.commit()
            # Publish a self-contained stage; no source sidecar is copied or
            # ever associated with the new profile's database identity.
            writer.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.DatabaseError as exc:
            raise ValueError(
                f"Não foi possível preparar a base de dados do backup: {exc}"
            ) from exc
        finally:
            if writer is not None:
                writer.close()
            if reader is not None:
                reader.close()
        cls.validate_restore_database(
            target, supported_schema_version=supported_schema_version
        )
        return target

    @classmethod
    def restore_missing_profile(
        cls,
        source: Path,
        *,
        database_file: Path,
        supported_schema_version: int,
        password: Optional[str] = None,
    ) -> Optional[Path]:
        """Recover a missing/empty primary without discarding residual evidence."""
        from services.backup_crypto import BackupCryptoCodec

        source = Path(source).expanduser().resolve()
        database_file = Path(database_file).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if database_file.is_file() and database_file.stat().st_size > 0:
            raise RuntimeError(
                "A recuperação de arranque só é permitida quando a base principal está ausente."
            )
        paths = paths_for_database(database_file)
        paths.create_runtime_directories()
        cleanup_private_work(
            paths.backups, legacy_patterns=("financas-startup-recovery-*",)
        )
        guard = DataProtectionGuard(
            database_file,
            supported_schema_version=int(supported_schema_version),
            current_app_version=APP_VERSION,
            attachments_directory=paths.attachments,
            history_directory=paths.history,
            preferences_file=paths.backup_preferences,
            protection_directory=paths.protections,
        )
        preserved_root = (
            paths.backups / f"perfil_sem_base_{backup_stamp()}_{time.monotonic_ns()}"
        )
        preserved_any = False

        with private_workspace(
            paths.backups, prefix="startup-recovery"
        ) as temporary_root:
            restore_source = source
            if cls.is_encrypted_backup(source):
                if password is None or password == "":
                    raise ValueError(
                        "Indica uma credencial não vazia para abrir o backup."
                    )
                restore_source = temporary_root / "decrypted.payload"
                kind = BackupCryptoCodec.decrypt(
                    source, restore_source, credential=str(password).encode("utf-8")
                )
                typed_source = temporary_root / (
                    "payload.zip" if kind == "Z" else "payload.sqlite3"
                )
                restore_source.replace(typed_source)
                restore_source = typed_source

            complete = (
                restore_source.suffix.casefold() == ".zip"
                and cls._has_protection_manifest(restore_source)
            )
            staged_attachments = None
            if complete:
                cls.validate_complete_archive(restore_source)
            else:
                if restore_source.suffix.casefold() == ".zip":
                    restore_source, staged_attachments = cls._extract_restore_zip(
                        restore_source, temporary_root
                    )
                restore_source = cls._materialise_restore_database(
                    restore_source,
                    temporary_root / "coherent.sqlite3",
                    supported_schema_version=supported_schema_version,
                )

            # Preserve zero-byte primary and all sidecars as bytes, without
            # opening them through SQLite (which could consume recovery WAL).
            for item in (
                database_file,
                Path(f"{database_file}-wal"),
                Path(f"{database_file}-shm"),
            ):
                if item.is_file():
                    destination = preserved_root / "database" / item.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
                    preserved_any = True
            for label, item in (
                ("anexos", paths.attachments),
                ("historico_alteracoes", paths.history),
            ):
                if item.exists() and any(child.is_file() for child in item.rglob("*")):
                    shutil.copytree(item, preserved_root / label)
                    preserved_any = True
            if paths.backup_preferences.is_file():
                destination = (
                    preserved_root / "configuracao" / paths.backup_preferences.name
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(paths.backup_preferences, destination)
                preserved_any = True

            if complete:
                guard.restore_archive(restore_source, interrupted_upgrade=False)
            else:
                attachment_stage = temporary_root / "installed-attachments"
                history_stage = temporary_root / "installed-history"
                cls._stage_preserved_directory(
                    staged_attachments or paths.attachments, attachment_stage
                )
                cls._stage_preserved_directory(paths.history, history_stage)
                preferences_stage = temporary_root / "installed-preferences.json"
                preferences_present = paths.backup_preferences.is_file()
                if preferences_present:
                    shutil.copy2(paths.backup_preferences, preferences_stage)

                def finalize() -> None:
                    cls._normalise_installed_attachment_paths(
                        database_file, paths.attachments
                    )
                    cls._resync_security_tombstone(database_file, strict=True)
                    cls.validate_restore_database(
                        database_file, supported_schema_version=supported_schema_version
                    )

                guard._install_staged_restore(
                    restore_source,
                    attachment_stage,
                    history_stage,
                    preferences_stage if preferences_present else None,
                    preferences_present=preferences_present,
                    finalize_callback=finalize,
                )
        return preserved_root if preserved_any else None

    @staticmethod
    def _stage_preserved_directory(source: Path, target: Path) -> None:
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _resync_security_tombstone(
        database_file: Path, *, strict: bool = False
    ) -> None:
        """BUG-SEC-04: re-bind the restored tombstone to the live envelope.

        A restored database carries the ``security_tombstone`` of the
        profile that produced the backup.  If that identity no longer
        matches the current security envelope (restore on a new machine, or
        after an envelope replacement), the startup gate would report
        TAMPERED and lock the user out of their own data.  Best-effort by
        design: a missing envelope simply clears the stale tombstone.
        """

        try:
            from services.startup_security import StartupSecurityService

            StartupSecurityService(
                database_file=Path(database_file)
            ).resync_tamper_tombstone_after_restore()
        except Exception:
            log_exception("Failed to resync the security tombstone after restore")
            if strict:
                raise

    @classmethod
    def _remove_database_sidecars(cls, database_file: Path) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database_file}{suffix}")
            cls._ensure_writable(sidecar)
            sidecar.unlink(missing_ok=True)

    @staticmethod
    def _normalise_installed_attachment_paths(
        database_file: Path,
        attachments_directory: Path,
    ) -> dict[str, Any]:
        """Validate every restored attachment before accepting the profile."""

        connection = sqlite3.connect(str(Path(database_file)))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            report = normalise_attachment_storage(
                connection,
                Path(attachments_directory),
                require_all=True,
                rewrite=True,
            )
            DataProtectionGuard.validate_connection(connection, full=True)
            connection.commit()
            return report
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _extract_restore_zip(
        source: Path,
        temporary_root: Path,
    ) -> tuple[Path, Optional[Path]]:
        database_target = temporary_root / "restore.sqlite3"
        attachment_target = temporary_root / "anexos"
        with zipfile.ZipFile(source, "r") as archive:
            infos, _total = validate_zip_safety(
                archive,
                extraction_root=temporary_root,
            )
            database_info = next(
                (
                    info
                    for info in infos
                    if PurePosixPath(info.filename).name == "financas_pessoais.sqlite3"
                ),
                next(
                    (
                        info
                        for info in infos
                        if info.filename.lower().endswith((".sqlite3", ".db"))
                    ),
                    None,
                ),
            )
            if database_info is None:
                raise ValueError("O ZIP não contém uma base de dados SQLite.")
            with (
                archive.open(database_info) as source_file,
                database_target.open("wb") as target_file,
            ):
                shutil.copyfileobj(source_file, target_file)

            restored_attachments = False
            for info in infos:
                if info.is_dir():
                    continue
                parts = PurePosixPath(info.filename.replace("\\", "/")).parts
                if not parts or parts[0].casefold() != "anexos":
                    continue
                relative_parts = parts[1:]
                if not relative_parts or any(
                    part in {"", ".", ".."} for part in relative_parts
                ):
                    raise ValueError("O ZIP contém um caminho de anexo inválido.")
                destination = attachment_target.joinpath(*relative_parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with (
                    archive.open(info) as source_file,
                    destination.open("wb") as target_file,
                ):
                    shutil.copyfileobj(source_file, target_file)
                restored_attachments = True
        return database_target, attachment_target if restored_attachments else None

    def _restore_attachments(self, staged_directory: Path) -> None:
        """Replace the legacy attachment scope exactly instead of overlaying it."""

        target = self.attachments_directory
        target.parent.mkdir(parents=True, exist_ok=True)
        identity = f"{os.getpid()}.{time.monotonic_ns()}"
        replacement = target.with_name(f".{target.name}.restore.tmp.{identity}")
        previous = target.with_name(f".{target.name}.restore.previous.{identity}")
        shutil.copytree(staged_directory, replacement)
        try:
            if target.exists():
                target.replace(previous)
            replacement.replace(target)
        except Exception as install_exc:
            if previous.exists() and not target.exists():
                try:
                    previous.replace(target)
                except Exception as recovery_exc:
                    raise DataIntegrityError(
                        "A reposição dos anexos falhou. Os anexos anteriores "
                        f"foram preservados em {previous}."
                    ) from recovery_exc
            raise install_exc
        else:
            DataProtectionGuard._remove_path(previous)
        finally:
            # Never remove previous here: it is the only surviving original
            # directory when both installation and its rollback fail.
            DataProtectionGuard._remove_path(replacement)

    @staticmethod
    def _has_protection_manifest(path: Path) -> bool:
        try:
            with zipfile.ZipFile(path, "r") as archive:
                validate_zip_safety(archive)
                return "manifesto_protecao.json" in archive.namelist()
        except OSError, zipfile.BadZipFile:
            return False

    def _create_restore_safety_backup(self, *, password: Optional[str] = None) -> Path:
        if self._encrypted_backups_enabled():
            recovery_password = self._restore_protection_password(password)
            target = self._restore_artifact_path(
                self.backup_directory, "antes_de_repor", ".lgbak"
            )
            return self._create_encrypted_complete_backup(
                target,
                password=recovery_password,
                purpose="before_restore",
            )
        target = self._restore_artifact_path(
            self.backup_directory, "antes_de_repor", ".zip"
        )
        return self._create_complete_zip(target, purpose="before_restore")

    @staticmethod
    def _restore_artifact_path(directory: Path, prefix: str, suffix: str) -> Path:
        return (
            Path(directory) / f"{prefix}_{backup_stamp()}_{time.monotonic_ns()}{suffix}"
        )

    def _restore_protection_password(self, password: Optional[str]) -> Optional[str]:
        from services.startup_security import get_active_session

        if get_active_session() is not None:
            # The unlocked live envelope protects the old profile, even when
            # the imported archive belongs to another credential/profile.
            return None
        try:
            return self._required_backup_password()
        except Exception as exc:
            explicit = str(password or "")
            if not explicit:
                raise RuntimeError(
                    "Não foi possível obter uma credencial para proteger o "
                    "perfil anterior. O restauro foi cancelado antes da substituição."
                ) from exc
            return explicit

    def _require_committed_restore_state(self) -> None:
        connection = getattr(getattr(self.repo, "db", None), "conn", None)
        if (
            connection is not None
            and getattr(connection, "in_transaction", False) is True
        ):
            raise DataIntegrityError(
                "Existem alterações pendentes. Guarda ou reverte essas alterações "
                "antes de iniciar o restauro."
            )

    def _begin_durable_restore(
        self,
        *,
        password: Optional[str] = None,
    ) -> tuple[DataProtectionGuard, Path]:
        """Create a crash-recovery bundle and publish its durable marker."""

        encrypted_recovery = self._encrypted_backups_enabled()
        recovery_password = (
            self._restore_protection_password(password) if encrypted_recovery else None
        )
        guard = self._guard(protection_password=recovery_password or "")
        if guard.restore_marker.exists():
            raise DataIntegrityError(
                "Existe um restauro pendente. Conclui a recuperação antes de iniciar outro restauro."
            )
        if encrypted_recovery:
            recovery = self._restore_artifact_path(
                self.protection_directory, "restauro_rollback", ".lgbak"
            )
            self._create_encrypted_complete_backup(
                recovery,
                password=recovery_password,
                purpose="restore_crash_recovery",
            )
        else:
            recovery = self._restore_artifact_path(
                self.protection_directory, "restauro_rollback", ".zip"
            )
            self._create_complete_zip(
                recovery,
                purpose="restore_crash_recovery",
            )
        guard.write_restore_marker(recovery)
        return guard, recovery

    @staticmethod
    def _validate_installed_database(guard: DataProtectionGuard) -> None:
        connection = sqlite3.connect(str(guard.database_file))
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            guard.validate_connection(connection, full=True)
            connection.commit()
        finally:
            connection.close()

    def _complete_durable_restore(
        self,
        guard: DataProtectionGuard,
        recovery: Path,
    ) -> None:
        self._normalise_installed_attachment_paths(
            guard.database_file,
            self.attachments_directory,
        )
        self._resync_security_tombstone(guard.database_file, strict=True)
        self._validate_installed_database(guard)
        # Attachment path/integrity normalization above can change the exact
        # database fingerprint after the scope installer authorized it.
        guard._authorize_compatible_restore()
        guard.clear_restore_marker()
        self._discard_completed_recovery(recovery)

    @classmethod
    def _discard_completed_recovery(cls, recovery: Path) -> None:
        try:
            cls._ensure_writable(recovery)
            recovery.unlink(missing_ok=True)
        except OSError as exc:
            # The committed profile is valid. Retain a detectable encrypted/
            # clear recovery artefact according to its original policy rather
            # than turn a cleanup failure into another financial rollback.
            log_warning(
                "Validated restore retained recovery archive %s: %s", recovery, exc
            )

    def _rollback_durable_restore(
        self,
        guard: DataProtectionGuard,
        recovery: Path,
    ) -> None:
        """Put every data scope back; retain the marker if recovery fails."""

        guard.restore_archive(recovery, interrupted_upgrade=True)
        self._resync_security_tombstone(guard.database_file, strict=True)
        self._validate_installed_database(guard)
        guard._authorize_compatible_restore()
        guard.clear_restore_marker()
        self._discard_completed_recovery(recovery)

    def restore_backup(
        self,
        source: Path,
        *,
        before_replace: Optional[Callable[[], None]] = None,
        password: Optional[str] = None,
    ) -> Path:
        """Restore a complete bundle atomically, retaining legacy compatibility."""
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)
        with self._operation_lock:
            if self._guard().restore_marker.exists():
                raise DataIntegrityError(
                    "Existe um restauro pendente. Conclui a recuperação antes de iniciar outro restauro."
                )
            self._require_committed_restore_state()
            with private_workspace(
                self.backup_directory,
                prefix="restore-inspect",
            ) as temporary:
                restore_source = source
                if self.is_encrypted_backup(source):
                    kind = self.encrypted_payload_kind(source)
                    decrypted = temporary / (
                        "payload.zip" if kind == "Z" else "payload.sqlite3"
                    )
                    self._decrypt_with_available_passwords(
                        source,
                        decrypted,
                        password,
                    )
                    restore_source = decrypted

                if (
                    restore_source.suffix.lower() == ".zip"
                    and self._has_protection_manifest(restore_source)
                ):
                    self.validate_complete_archive(restore_source)
                    safety = self._create_restore_safety_backup(password=password)
                    guard, recovery = self._begin_durable_restore(password=password)

                    replacement_started = False
                    try:
                        self._require_committed_restore_state()
                        try:
                            if hasattr(getattr(self.repo, "db", None), "conn"):
                                conn = self.repo.db.conn
                                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        except Exception as exc:
                            log_warning("Pre-restore WAL checkpoint: %s", exc)
                        if before_replace is not None:
                            before_replace()
                        elif hasattr(getattr(self.repo, "db", None), "close"):
                            self.repo.db.close()
                        elif hasattr(getattr(self.repo, "db", None), "conn"):
                            self.repo.db.conn.close()
                        replacement_started = True
                        guard.restore_archive(
                            restore_source,
                            interrupted_upgrade=False,
                        )
                        self._complete_durable_restore(guard, recovery)
                    except Exception as exc:
                        if replacement_started:
                            try:
                                self._rollback_durable_restore(guard, recovery)
                            except Exception as recovery_exc:
                                raise DataIntegrityError(
                                    "O restauro falhou e a recuperação automática também não foi concluída. "
                                    "O marcador de segurança foi preservado para o próximo arranque."
                                ) from recovery_exc
                        else:
                            guard.clear_restore_marker()
                            recovery.unlink(missing_ok=True)
                        raise exc
                    return safety

            return self._restore_legacy_backup(
                source,
                before_replace=before_replace,
                password=password,
            )

    def _restore_legacy_backup(
        self,
        source: Path,
        *,
        before_replace: Optional[Callable[[], None]] = None,
        password: Optional[str] = None,
    ) -> Path:
        """Restore a legacy payload using the common transactional scope installer."""
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)
        replacement_started = False
        durable_guard: Optional[DataProtectionGuard] = None
        durable_recovery: Optional[Path] = None
        with private_workspace(
            self.backup_directory, prefix="legacy-restore"
        ) as temporary_root:
            try:
                restore_source = source
                if self.is_encrypted_backup(source):
                    kind = self.encrypted_payload_kind(source)
                    restore_source = temporary_root / (
                        "payload.zip" if kind == "Z" else "payload.sqlite3"
                    )
                    self._decrypt_with_available_passwords(
                        source, restore_source, password
                    )
                staged_attachments = None
                if restore_source.suffix.casefold() == ".zip":
                    restore_source, staged_attachments = self._extract_restore_zip(
                        restore_source, temporary_root
                    )
                # Reject unsupported data before making safety copies, closing
                # the repository or touching any of the live profile scopes.
                restore_source = self._materialise_restore_database(
                    restore_source, temporary_root / "coherent.sqlite3"
                )
                if self._encrypted_backups_enabled():
                    safety = self._create_restore_safety_backup(password=password)
                else:
                    safety = self._restore_artifact_path(
                        self.backup_directory, "antes_de_repor", ".sqlite3"
                    )
                    self.backup_sqlite(safety)
                durable_guard, durable_recovery = self._begin_durable_restore(
                    password=password
                )

                attachments_stage = temporary_root / "installed-attachments"
                history_stage = temporary_root / "installed-history"
                self._stage_preserved_directory(
                    staged_attachments or self.attachments_directory, attachments_stage
                )
                self._stage_preserved_directory(self.history_directory, history_stage)
                preferences_stage = temporary_root / "installed-preferences.json"
                preferences_present = self.preferences_file.is_file()
                if preferences_present:
                    shutil.copy2(self.preferences_file, preferences_stage)
                self._require_committed_restore_state()
                try:
                    connection = getattr(getattr(self.repo, "db", None), "conn", None)
                    if connection is not None:
                        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception as exc:
                    log_warning("Pre-restore WAL checkpoint: %s", exc)
                if before_replace is not None:
                    before_replace()
                elif hasattr(getattr(self.repo, "db", None), "close"):
                    self.repo.db.close()
                elif hasattr(getattr(self.repo, "db", None), "conn"):
                    self.repo.db.conn.close()
                replacement_started = True

                def finalize() -> None:
                    self._normalise_installed_attachment_paths(
                        self.database_file, self.attachments_directory
                    )
                    self._resync_security_tombstone(self.database_file, strict=True)
                    self._validate_installed_database(durable_guard)

                durable_guard._install_staged_restore(
                    restore_source,
                    attachments_stage,
                    history_stage,
                    preferences_stage if preferences_present else None,
                    preferences_present=preferences_present,
                    finalize_callback=finalize,
                )
                self._complete_durable_restore(durable_guard, durable_recovery)
                durable_guard = None
                durable_recovery = None
                return safety
            except Exception:
                if durable_guard is not None and durable_recovery is not None:
                    if replacement_started:
                        try:
                            self._rollback_durable_restore(
                                durable_guard, durable_recovery
                            )
                        except Exception as recovery_exc:
                            raise DataIntegrityError(
                                "O restauro falhou e a recuperação automática também não foi concluída. "
                                "O marcador de segurança foi preservado para o próximo arranque."
                            ) from recovery_exc
                    else:
                        durable_guard.clear_restore_marker()
                        self._discard_completed_recovery(durable_recovery)
                raise

    @staticmethod
    def _read_preferences_path(path: Path) -> dict[str, Any]:
        try:
            candidate = Path(path)
            if candidate.exists():
                data = json.loads(candidate.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            log_exception("Failed to read backup preferences file", exc)
        return {}

    def _read_preferences_file(self) -> dict[str, Any]:
        return self._read_preferences_path(self.preferences_file)

    def _write_preferences_file(self, data: dict[str, Any]) -> None:
        self.preferences_file.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        temporary = self.preferences_file.with_suffix(
            self.preferences_file.suffix + ".tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            self._durable_replace(temporary, self.preferences_file)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _preference_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        return str(value).strip().casefold() in {"1", "true", "sim", "yes", "on"}

    def preferences(self) -> dict[str, Any]:
        """Read backup preferences from their JSON mirror and SQLite settings."""
        preferences = self._read_preferences_file()

        def read_int(name: str, setting_key: str, default: int) -> int:
            if name in preferences and str(preferences.get(name, "")).strip() != "":
                try:
                    return int(float(preferences.get(name)))
                except Exception as exc:
                    log_warning(
                        "Invalid backup preference %s=%r in preference file: %s",
                        name,
                        preferences.get(name),
                        exc,
                    )
            raw = self.repo.setting(setting_key, "")
            if str(raw).strip() != "":
                try:
                    return int(float(raw))
                except Exception as exc:
                    log_warning(
                        "Invalid backup setting %s=%r in database: %s",
                        setting_key,
                        raw,
                        exc,
                    )
            return default

        def metadata_int(name: str) -> int:
            try:
                return max(0, int(preferences.get(name) or 0))
            except (TypeError, ValueError):
                return 0

        interval = read_int(
            "auto_backup_interval_minutes",
            "auto_backup_interval_minutes",
            -1,
        )
        if interval < 0:
            try:
                interval = max(
                    0,
                    int(float(self.repo.setting("auto_backup_days", "1"))) * 1440,
                )
            except Exception as exc:
                log_warning("Invalid legacy backup interval in database: %s", exc)
                interval = 1440
        keep = max(1, min(250, read_int("backup_keep", "backup_keep", 15)))

        if "backup_on_close" in preferences:
            backup_on_close = self._preference_bool(preferences.get("backup_on_close"))
        else:
            backup_on_close = self.repo.setting("backup_on_close", "0") == "1"
        if "backup_encryption_enabled" in preferences:
            encryption_enabled = self._preference_bool(
                preferences.get("backup_encryption_enabled")
            )
        else:
            encryption_enabled = (
                self.repo.setting("backup_encryption_enabled", "0") == "1"
            )
        secondary_enabled = self._preference_bool(
            preferences.get("secondary_backup_enabled"),
            False,
        )
        secondary_directory = str(
            preferences.get("secondary_backup_directory") or ""
        ).strip()
        primary_directory = str(
            preferences.get("primary_backup_directory") or ""
        ).strip()

        return {
            "auto_backup_interval_minutes": max(0, interval),
            "backup_keep": keep,
            "backup_on_close": backup_on_close,
            "backup_encryption_enabled": encryption_enabled,
            "secondary_backup_enabled": secondary_enabled,
            "secondary_backup_directory": secondary_directory,
            "primary_backup_directory": primary_directory,
            "effective_backup_directory": str(self.backup_directory),
            "primary_backup_directory_error": self.primary_directory_error,
            "last_verified_backup_file": str(
                preferences.get("last_verified_backup_file") or ""
            ),
            "last_verified_backup_sha256": str(
                preferences.get("last_verified_backup_sha256") or ""
            ),
            "last_verified_backup_at": str(
                preferences.get("last_verified_backup_at") or ""
            ),
            "last_verified_backup_size": metadata_int("last_verified_backup_size"),
            "last_verified_backup_encrypted": self._preference_bool(
                preferences.get("last_verified_backup_encrypted"),
                False,
            ),
            "last_secondary_backup_file": str(
                preferences.get("last_secondary_backup_file") or ""
            ),
            "last_secondary_backup_sha256": str(
                preferences.get("last_secondary_backup_sha256") or ""
            ),
            "last_secondary_backup_at": str(
                preferences.get("last_secondary_backup_at") or ""
            ),
            "last_secondary_backup_size": metadata_int("last_secondary_backup_size"),
            "last_secondary_backup_error": str(
                preferences.get("last_secondary_backup_error") or ""
            ),
            "last_recovery_drill_at": str(
                preferences.get("last_recovery_drill_at") or ""
            ),
            "last_recovery_drill_file": str(
                preferences.get("last_recovery_drill_file") or ""
            ),
        }

    def save_preferences(
        self,
        interval_minutes: int,
        keep: int,
        backup_on_close: bool,
        encryption_enabled: Optional[bool] = None,
        secondary_enabled: Optional[bool] = None,
        secondary_directory: Optional[str] = None,
        primary_directory: Optional[str] = None,
    ) -> None:
        interval_minutes = max(0, int(interval_minutes))
        keep = max(1, min(250, int(keep)))
        close_value = "1" if backup_on_close else "0"
        if encryption_enabled is None:
            encryption_enabled = self._encrypted_backups_enabled()
        encryption_value = "1" if encryption_enabled else "0"
        current = self._read_preferences_file()
        if secondary_enabled is None:
            secondary_enabled = self._preference_bool(
                current.get("secondary_backup_enabled"),
                False,
            )
        if secondary_directory is None:
            secondary_directory = str(
                current.get("secondary_backup_directory") or ""
            ).strip()
        if primary_directory is None:
            primary_directory = str(
                current.get("primary_backup_directory") or ""
            ).strip()
        primary_text = str(primary_directory or "").strip()
        validated_primary = self.validate_primary_directory(primary_text)
        validated_secondary = self.validate_secondary_directory(
            secondary_directory,
            enabled=bool(secondary_enabled),
            primary_directory=validated_primary,
        )

        current.update(
            {
                "auto_backup_interval_minutes": interval_minutes,
                "backup_keep": keep,
                "backup_on_close": close_value,
                "backup_encryption_enabled": encryption_value,
                "secondary_backup_enabled": "1" if secondary_enabled else "0",
                "secondary_backup_directory": (
                    str(validated_secondary) if validated_secondary else ""
                ),
                "primary_backup_directory": primary_text,
            }
        )
        connection = self.repo.db.conn
        previous_preferences = (
            self.preferences_file.read_bytes()
            if self.preferences_file.is_file()
            else None
        )
        connection.commit()
        connection.execute("SAVEPOINT save_backup_preferences")
        try:
            for key, value in (
                ("auto_backup_interval_minutes", str(interval_minutes)),
                ("backup_on_close", close_value),
                ("backup_keep", str(keep)),
                ("backup_encryption_enabled", encryption_value),
            ):
                connection.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                    (key, value),
                )
            self._write_preferences_file(current)
            connection.execute("RELEASE SAVEPOINT save_backup_preferences")
            connection.commit()
            self.backup_directory = validated_primary
            self._configured_primary_directory = primary_text
            self.primary_directory_error = ""
        except (Exception):
            connection.execute("ROLLBACK TO SAVEPOINT save_backup_preferences")
            connection.execute("RELEASE SAVEPOINT save_backup_preferences")
            connection.rollback()
            try:
                if previous_preferences is None:
                    self.preferences_file.unlink(missing_ok=True)
                else:
                    temporary = self.preferences_file.with_suffix(
                        self.preferences_file.suffix + ".rollback.tmp"
                    )
                    with temporary.open("wb") as handle:
                        handle.write(previous_preferences)
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._durable_replace(temporary, self.preferences_file)
            except OSError as restore_exc:
                log_exception(
                    "Failed to restore backup preferences after save failure",
                    restore_exc,
                )
            raise

    def validate_primary_directory(
        self,
        directory: str | Path | None,
    ) -> Path:
        """Validate and write-probe the folder used by every automatic backup."""

        text = str(directory or "").strip()
        candidate = Path(text).expanduser() if text else self.default_backup_directory
        candidate = candidate.resolve()
        forbidden_roots = (
            self.attachments_directory.resolve(),
            self.history_directory.resolve(),
            self.protection_directory.resolve(),
            self.configuration_directory.resolve(),
        )
        if any(
            candidate == root or candidate.is_relative_to(root)
            for root in forbidden_roots
        ):
            raise ValueError(
                "Escolhe uma pasta de backups separada dos documentos, histórico, "
                "proteções e configuração da aplicação."
            )
        probe: Optional[Path] = None
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".financas_backup_write_probe_{os.getpid()}"
            descriptor = os.open(
                probe,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, b"backup-folder-check")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            probe.unlink(missing_ok=True)
        except OSError as exc:
            try:
                if probe is not None:
                    probe.unlink(missing_ok=True)
            except (OSError):
                pass
            raise ValueError(
                "A pasta principal de backups não está disponível para escrita."
            ) from exc
        return candidate

    def validate_secondary_directory(
        self,
        directory: str | Path | None,
        *,
        enabled: bool,
        primary_directory: Optional[Path] = None,
    ) -> Optional[Path]:
        text = str(directory or "").strip()
        if not text:
            if enabled:
                raise ValueError(
                    "Escolhe uma segunda pasta antes de ativar a cópia adicional."
                )
            return None
        candidate = Path(text).expanduser().resolve()
        if not enabled:
            return candidate
        primary = Path(primary_directory or self.backup_directory).resolve()
        data_root = self.data_root.resolve()
        if candidate == primary:
            raise ValueError(
                "A segunda localização tem de ser diferente da pasta principal."
            )
        if candidate == data_root or candidate.is_relative_to(data_root):
            raise ValueError(
                "Escolhe uma segunda localização fora da pasta principal de dados."
            )
        return candidate

    def _update_backup_metadata(self, **values: Any) -> None:
        current = self._read_preferences_file()
        current.update(values)
        self._write_preferences_file(current)

    def _record_verified_backup(self, path: Path, digest: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        digest = str(digest or "") or self._file_digest(path)
        try:
            connection = getattr(getattr(self.repo, "db", None), "conn", None)
            if connection is not None and getattr(connection, "in_transaction", False):
                connection.execute("SAVEPOINT record_verified_backup_setting")
                try:
                    connection.execute(
                        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                        ("last_verified_backup_file", Path(path).name),
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                        ("last_verified_backup_at", now),
                    )
                    connection.execute(
                        "RELEASE SAVEPOINT record_verified_backup_setting"
                    )
                except (Exception):
                    connection.execute(
                        "ROLLBACK TO SAVEPOINT record_verified_backup_setting"
                    )
                    connection.execute(
                        "RELEASE SAVEPOINT record_verified_backup_setting"
                    )
            else:
                self.repo.set_setting("last_verified_backup_file", Path(path).name)
                self.repo.set_setting("last_verified_backup_at", now)
        except Exception as exc:
            log_exception("Failed to record verified backup setting", exc)
        self._update_backup_metadata(
            last_verified_backup_file=Path(path).name,
            last_verified_backup_sha256=digest,
            last_verified_backup_at=now,
            last_verified_backup_size=int(Path(path).stat().st_size),
            last_verified_backup_encrypted=self.is_encrypted_backup(path),
        )

    def _mirror_to_secondary(self, source: Path) -> Optional[Path]:
        preferences = self.preferences()
        if not bool(preferences.get("secondary_backup_enabled")):
            return None
        try:
            target_root = self.validate_secondary_directory(
                preferences.get("secondary_backup_directory"),
                enabled=True,
            )
            assert target_root is not None
            target_root.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(target_root).free < (
                Path(source).stat().st_size + MIN_ARCHIVE_FREE_SPACE_BUFFER
            ):
                raise DataIntegrityError(
                    "Espaço insuficiente na segunda localização para guardar a cópia."
                )
            target = target_root / Path(source).name
            # BUG-SEC-003: the temporary name must be unique per operation.
            # A shared ".<name>.copying" name made two concurrent mirrors
            # fight over the same file (FileNotFoundError from shutil/os on
            # the losing side). Stamp + PID disambiguates processes; the
            # monotonic counter disambiguates threads within this process.
            temporary = target.with_name(
                f".{target.name}.copying.{time.monotonic_ns()}_{os.getpid()}"
            )
            try:
                shutil.copy2(source, temporary)
                source_hash = self._file_digest(source)
                if self._file_digest(temporary) != source_hash:
                    raise DataIntegrityError(
                        "A cópia secundária não corresponde ao backup principal."
                    )
                self._durable_replace(temporary, target)
            finally:
                # Cleanup only THIS operation's temporary file; concurrent
                # operations own their own uniquely-named temporaries.
                temporary.unlink(missing_ok=True)
            if self._file_digest(target) != source_hash:
                raise DataIntegrityError(
                    "A cópia secundária falhou a verificação SHA-256."
                )
            now = datetime.now().isoformat(timespec="seconds")
            self._update_backup_metadata(
                last_secondary_backup_file=target.name,
                last_secondary_backup_sha256=source_hash,
                last_secondary_backup_at=now,
                last_secondary_backup_size=int(target.stat().st_size),
                last_secondary_backup_error="",
            )
            return target
        except Exception as exc:
            try:
                self._update_backup_metadata(
                    last_secondary_backup_error=str(exc),
                )
            except Exception as metadata_exc:
                log_warning(
                    "Não foi possível registar a falha da segunda cópia: %s",
                    metadata_exc,
                )
            log_warning("A cópia para a segunda localização falhou: %s", exc)
            return None

    def prune_invalid_and_corrupt_backups(self) -> list[str]:
        """Audit and remove 0-byte, corrupt, or unfinished backup files."""
        pruned: list[str] = []
        if not self.backup_directory.is_dir():
            return pruned
        for candidate in self.backup_directory.iterdir():
            try:
                if candidate.is_file():
                    size = candidate.stat().st_size
                    is_backup = candidate.name.startswith(
                        "financas_backup_"
                    ) and candidate.suffix in {".zip", ".lgbak"}
                    is_temp = candidate.name.startswith(".") and (
                        ".copying" in candidate.name or ".tmp" in candidate.name
                    )
                    # BUG-SEC-003: uniquely-suffixed temporaries created by
                    # _mirror_to_secondary still match ".copying" here, so a
                    # crashed operation's orphan is cleaned on the next audit
                    # while concurrent live operations remain untouched.
                    if (is_backup and size == 0) or is_temp:
                        candidate.unlink(missing_ok=True)
                        pruned.append(candidate.name)
            except OSError as exc:
                log_warning(
                    "Não foi possível auditar/remover ficheiro de backup inválido %s: %s",
                    candidate,
                    exc,
                )
        return pruned

    def latest_complete_backup(self) -> Optional[Path]:

        files = [
            *self.backup_directory.glob("financas_backup_*.zip"),
            *self.backup_directory.glob("financas_backup_*.lgbak"),
        ]
        try:
            return max(files, key=lambda item: item.stat().st_mtime) if files else None
        except OSError as exc:
            log_warning("Não foi possível consultar o backup mais recente: %s", exc)
            return None

    def latest_backup_fingerprint(self) -> tuple[str, int, int] | None:
        """Return a cheap identity used only to schedule background verification."""

        latest = self.latest_complete_backup()
        if latest is None:
            return None
        try:
            stat = latest.stat()
            return (str(latest.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
        except OSError as exc:
            log_warning("Não foi possível identificar o backup mais recente: %s", exc)
            return None

    @classmethod
    def detect_archive_format(cls, path: Optional[Path]) -> str:
        """Detect archive format: LGBK2, LGBK1, ZIP, truncated, unknown, unreadable, or missing."""
        if path is None:
            return "nenhum"
        try:
            p = Path(path)
            if not p.is_file():
                return "nenhum"
            with p.open("rb") as handle:
                header = handle.read(5)
        except OSError as exc:
            log_warning("Não foi possível ler o cabeçalho do backup %s: %s", path, exc)
            return "unreadable"
        if len(header) == 0:
            return "truncated"
        if header == LGBK2_MAGIC:
            return "LGBK2"
        if header == cls.ENCRYPTED_MAGIC:
            return "LGBK1"
        if header.startswith(b"PK\x03\x04") or header.startswith(b"PK\x05\x06") or header.startswith(b"PK\x07\x08"):
            return "ZIP"
        if len(header) < 5:
            return "truncated"
        return "unknown"

    def configured_backup_format(self) -> str:
        """Return the configured target format for new complete backups."""
        if self._encrypted_backups_enabled():
            from services.startup_security import get_active_session

            session = get_active_session()
            envelope_file = paths_for_database(self.database_file).security_envelope
            if session is not None or envelope_file.is_file():
                return "LGBK2"
            return "LGBK1 (compatibilidade)"
        return "ZIP (não cifrado)"

    def protection_health_snapshot(self) -> dict[str, Any]:
        """Return a fast, explicitly metadata-based protection summary.

        This path is suitable for dashboards: it never hashes an archive. The
        complete verification remains available through :meth:`protection_health`.
        """

        preferences = self.preferences()
        latest = self.latest_complete_backup()
        metadata_verified = False
        recent = False
        mirrored = False
        if latest is not None:
            try:
                stat = latest.stat()
                metadata_verified = (
                    latest.name
                    == str(preferences.get("last_verified_backup_file") or "")
                    and bool(preferences.get("last_verified_backup_sha256"))
                    and stat.st_size
                    == int(preferences.get("last_verified_backup_size") or 0)
                )
                recent = datetime.now() - datetime.fromtimestamp(
                    stat.st_mtime
                ) <= timedelta(hours=72)
                secondary_directory = str(
                    preferences.get("secondary_backup_directory") or ""
                ).strip()
                if preferences.get("secondary_backup_enabled") and secondary_directory:
                    secondary = Path(secondary_directory) / latest.name
                    mirrored = (
                        secondary.is_file()
                        and secondary.stat().st_size == stat.st_size
                        and latest.name
                        == str(preferences.get("last_secondary_backup_file") or "")
                        and bool(preferences.get("last_secondary_backup_sha256"))
                    )
            except OSError as exc:
                log_warning("Não foi possível ler o resumo rápido dos backups: %s", exc)
        encryption_requested = bool(preferences.get("backup_encryption_enabled"))
        encrypted = bool(latest and self.is_encrypted_backup(latest))
        drill_at = str(preferences.get("last_recovery_drill_at") or "")
        drill_recent = False
        if drill_at:
            try:
                drill_recent = datetime.now() - datetime.fromisoformat(
                    drill_at
                ) <= timedelta(days=90)
            except (ValueError):
                drill_recent = False
        score = (
            (35 if metadata_verified else 0)
            + (20 if recent else 0)
            + (15 if mirrored else 0)
            + (15 if encrypted else 0)
            + (15 if drill_recent else 0)
        )
        registered_at = str(preferences.get("last_verified_backup_at") or "")
        if latest is None:
            message = "Ainda não existe um backup completo."
        elif not metadata_verified:
            message = "O backup mais recente ainda não tem verificação registada."
        elif encryption_requested and not encrypted:
            message = "A cifragem está ativa, mas o backup mais recente ainda não está cifrado."
        elif not recent:
            message = "O último backup verificado tem mais de 72 horas."
        elif not drill_recent:
            message = "Backup recente; falta testar a recuperação nos últimos 90 dias."
        else:
            message = (
                f"Última verificação registada em {registered_at}; "
                "a confirmação atual decorre em segundo plano."
            )
        return {
            "score": score,
            "metadata_verified": metadata_verified,
            "verification_recorded": metadata_verified,
            "recent": recent,
            "mirrored": mirrored,
            "encrypted": encrypted,
            "encryption_requested": encryption_requested,
            "drill_recent": drill_recent,
            "latest": latest,
            "last_verified_at": registered_at,
            "last_recovery_drill_at": drill_at,
            "message": message,
            "summary_kind": "metadata",
        }

    def protection_health(self) -> dict[str, Any]:
        """Revalidate the latest backup instead of trusting stale metadata."""
        preferences = self.preferences()
        latest = self.latest_complete_backup()
        verified = False
        recent = False
        mirrored = False
        validation_error = ""
        current_hash = ""
        if latest is not None:
            expected_name = str(preferences.get("last_verified_backup_file") or "")
            expected_hash = str(preferences.get("last_verified_backup_sha256") or "")
            try:
                latest_stat = latest.stat()
                current_hash = self._file_digest(latest)
                metadata_matches = (
                    latest.name == expected_name
                    and bool(expected_hash)
                    and current_hash == expected_hash
                    and latest_stat.st_size
                    == int(preferences.get("last_verified_backup_size") or 0)
                )
                previously_verified_changed = (
                    latest.name == expected_name
                    and bool(expected_hash)
                    and not metadata_matches
                )
                if previously_verified_changed:
                    validation_error = (
                        "O conteúdo do backup mudou desde a última verificação."
                    )
                else:
                    try:
                        self.validate_backup_file(latest)
                        verified = True
                    except Exception as exc:
                        validation_error = str(exc)
                        verified = False
                        log_warning(
                            "O backup mais recente falhou a validação completa: %s",
                            exc,
                        )
                recent = datetime.now() - datetime.fromtimestamp(
                    latest_stat.st_mtime
                ) <= timedelta(hours=72)
            except OSError as exc:
                log_warning("Não foi possível validar o estado do backup: %s", exc)
                latest_stat = None
            secondary_text = str(preferences.get("secondary_backup_directory") or "")
            if (
                preferences.get("secondary_backup_enabled")
                and secondary_text
                and preferences.get("last_secondary_backup_file") == latest.name
            ):
                try:
                    secondary = Path(secondary_text) / latest.name
                    mirrored = (
                        latest_stat is not None
                        and secondary.is_file()
                        and secondary.stat().st_size
                        == int(preferences.get("last_secondary_backup_size") or 0)
                        == latest_stat.st_size
                        and str(preferences.get("last_secondary_backup_sha256") or "")
                        == expected_hash
                        and self._file_digest(secondary) == current_hash
                        and bool(current_hash)
                    )
                except OSError as exc:
                    log_warning(
                        "Não foi possível consultar a segunda localização: %s",
                        exc,
                    )
                    mirrored = False
        encryption_requested = bool(preferences.get("backup_encryption_enabled"))
        encrypted = bool(latest and self.is_encrypted_backup(latest))
        score = (
            (40 if verified else 0)
            + (25 if recent else 0)
            + (20 if mirrored else 0)
            + (15 if encrypted else 0)
        )
        if latest is None:
            message = "Ainda não existe um backup completo."
        elif not verified:
            message = "O backup mais recente falhou a verificação atual."
        elif encryption_requested and not encrypted:
            message = (
                "O backup é válido, mas ainda não está cifrado apesar da opção ativa."
            )
        elif preferences.get("secondary_backup_enabled") and not mirrored:
            message = "Backup principal válido; falta confirmar a segunda cópia."
        elif not preferences.get("secondary_backup_enabled"):
            message = "Backup principal válido; a segunda localização está desligada."
        else:
            message = "Backup completo, recente e duplicado noutra localização."
        recommendations: list[str] = []
        if latest is None:
            recommendations.append("Cria um backup completo agora.")
        elif not verified:
            recommendations.append("Cria um novo backup; o mais recente não é fiável.")
        if not encrypted:
            if encryption_requested:
                recommendations.append(
                    "Cria agora um novo backup para aplicar a cifragem ativa."
                )
            else:
                recommendations.append(
                    "Ativa a cifragem para proteger valores e documentos."
                )
        if not preferences.get("secondary_backup_enabled"):
            recommendations.append("Escolhe uma segunda localização fora deste disco.")
        elif not mirrored:
            recommendations.append(
                "Confirma que a segunda localização está disponível."
            )
        return {
            "score": score,
            "verified": verified,
            "recent": recent,
            "mirrored": mirrored,
            "encrypted": encrypted,
            "encryption_requested": encryption_requested,
            "latest": latest,
            "message": message,
            "validation_error": validation_error,
            "recommendations": recommendations,
            "current_hash": current_hash,
            "verified_at": (
                datetime.now().isoformat(timespec="seconds") if verified else ""
            ),
        }

    def verify_latest_backup_and_record(self) -> dict[str, Any]:
        """Perform a real validation in a worker and persist its exact timestamp."""

        with self._operation_lock:
            health = self.protection_health()
            latest = health.get("latest")
            if health.get("verified") and isinstance(latest, Path):
                self._record_verified_backup(
                    latest,
                    str(health.get("current_hash") or ""),
                )
                health["verified_at"] = datetime.now().isoformat(timespec="seconds")
            return health

    def recovery_drill(self, source: Optional[Path] = None) -> dict[str, Any]:
        """Restore the latest bundle into an isolated folder as a real drill."""
        candidate = Path(source) if source else self.latest_complete_backup()
        if candidate is None or not candidate.is_file():
            raise FileNotFoundError("Ainda não existe um backup completo para testar.")
        with (
            self._operation_lock,
            private_workspace(
                self.backup_directory,
                prefix="recovery-drill",
            ) as root,
        ):
            archive = candidate
            if self.is_encrypted_backup(candidate):
                archive = root / "backup.zip"
                kind = self._decrypt_with_available_passwords(
                    candidate,
                    archive,
                )
                if kind != "Z":
                    raise DataIntegrityError(
                        "O backup selecionado é antigo e não contém todos os dados."
                    )
            manifest = self.validate_complete_archive(archive)
            drill_guard = DataProtectionGuard(
                root / "dados" / DEFAULT_DB_NAME,
                supported_schema_version=int(
                    manifest.get("target_schema_version") or 0
                ),
                current_app_version=APP_VERSION,
                attachments_directory=root / "dados" / "anexos",
                history_directory=root / "dados" / "historico_alteracoes",
                preferences_file=root / "dados" / "backup_preferences.json",
                protection_directory=root / "protecoes",
            )
            drill_guard.restore_archive(
                archive,
                interrupted_upgrade=False,
            )
            self.validate_restore_database(drill_guard.database_file)
            now = datetime.now().isoformat(timespec="seconds")
            self._update_backup_metadata(
                last_recovery_drill_at=now,
                last_recovery_drill_file=candidate.name,
            )
            return {
                "file": candidate,
                "verified_at": now,
                "tables": dict(manifest.get("tables") or {}),
                "files": len(manifest.get("files") or []),
            }

    def create_auto_backup(self, prefix: str = "financas_backup_auto") -> Path:
        with self._operation_lock:
            if self._encrypted_backups_enabled():
                target = self.backup_directory / f"{prefix}_{backup_stamp()}.lgbak"
                self._create_encrypted_complete_backup(
                    target,
                    purpose=prefix,
                )
            else:
                target = self.backup_directory / f"{prefix}_{backup_stamp()}.zip"
                self._create_complete_zip(target, purpose=prefix)
            self._record_verified_backup(target)
            self._mirror_to_secondary(target)
            now_text = datetime.now().isoformat(timespec="seconds")
            self.repo.set_setting("last_auto_backup_dt", now_text)
            self.repo.set_setting("last_auto_backup", date.today().isoformat())
            self.repo.set_setting("last_auto_backup_file", target.name)
            self.prune_backups()
            return target

    @classmethod
    def run_scheduled_backup_for_database(
        cls,
        database_file: Path,
        *,
        silent: bool = True,
    ) -> Optional[Path]:
        """Run a scheduled backup with a connection owned by the worker thread."""

        background_repo = _ThreadOwnedBackupRepo(Path(database_file))
        try:
            service = cls(
                background_repo,
                database_file=Path(database_file),
            )
            return service.run_auto_backup_if_due(silent=silent)
        finally:
            background_repo.close()

    @classmethod
    def run_manual_backup_for_database(cls, database_file: Path) -> Path:
        """Create and verify a manual backup on a worker-owned connection."""

        background_repo = _ThreadOwnedBackupRepo(Path(database_file))
        try:
            return cls(
                background_repo,
                database_file=Path(database_file),
            ).create_manual_backup()
        finally:
            background_repo.close()

    @classmethod
    def run_recovery_drill_for_database(cls, database_file: Path) -> dict[str, Any]:
        """Validate a restore drill without borrowing the GUI SQLite connection."""

        background_repo = _ThreadOwnedBackupRepo(Path(database_file))
        try:
            return cls(
                background_repo,
                database_file=Path(database_file),
            ).recovery_drill()
        finally:
            background_repo.close()

    @classmethod
    def run_close_backup_for_database(
        cls,
        database_file: Path,
        *,
        silent: bool = True,
    ) -> Optional[Path]:
        """Create the optional close backup on a worker-owned connection."""

        background_repo = _ThreadOwnedBackupRepo(Path(database_file))
        try:
            service = cls(
                background_repo,
                database_file=Path(database_file),
            )
            return service.run_backup_on_close(silent=silent)
        finally:
            background_repo.close()

    @classmethod
    def run_verify_latest_backup_for_database(
        cls,
        database_file: Path,
    ) -> dict[str, Any]:
        """Verify and record backup health on an isolated worker-owned connection."""
        background_repo = _ThreadOwnedBackupRepo(Path(database_file))
        try:
            service = cls(
                background_repo,
                database_file=Path(database_file),
            )
            return service.verify_latest_backup_and_record()
        finally:
            background_repo.close()

    def run_auto_backup_if_due(self, silent: bool = True) -> Optional[Path]:

        interval_minutes = int(self.preferences()["auto_backup_interval_minutes"])
        if interval_minutes <= 0:
            return None
        last = self.repo.setting("last_auto_backup_dt", "") or self.repo.setting(
            "last_auto_backup",
            "",
        )
        last_datetime = parse_backup_datetime(last)
        now = datetime.now()
        if last_datetime is not None and last_datetime > now + timedelta(minutes=5):
            log_warning(
                "O último backup automático está no futuro; será criado um novo backup seguro."
            )
            last_datetime = None
        due = (
            last_datetime is None
            or (now - last_datetime).total_seconds() >= interval_minutes * 60
        )
        if not due:
            return None
        try:
            return self.create_auto_backup("financas_backup_auto")
        except Exception as exc:
            if not silent:
                raise
            log_exception("Automatic backup failed", exc)
            return None

    def run_backup_on_close(self, silent: bool = True) -> Optional[Path]:
        if not bool(self.preferences().get("backup_on_close", False)):
            return None
        try:
            return self.create_auto_backup("financas_backup_ao_fechar")
        except Exception as exc:
            if not silent:
                raise
            log_exception("Backup on close failed", exc)
            return None

    def prune_backups(self) -> None:
        try:
            keep = max(1, int(self.preferences().get("backup_keep", 15)))
        except Exception as exc:
            log_exception(
                "Failed to read backup retention preference; using default",
                exc,
            )
            keep = 15
        automatic_families = {
            "automatico": (
                "financas_backup_auto_*.sqlite3",
                "financas_backup_auto_*.zip",
                "financas_backup_auto_*.lgbak",
            ),
            "ao_fechar": (
                "financas_backup_ao_fechar_*.sqlite3",
                "financas_backup_ao_fechar_*.zip",
                "financas_backup_ao_fechar_*.lgbak",
            ),
        }

        def family_files(folder: Path, patterns: tuple[str, ...]) -> list[Path]:
            unique = {
                item.resolve(): item
                for pattern in patterns
                for item in folder.glob(pattern)
                if item.is_file()
            }
            return sorted(
                unique.values(),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )

        def prune_folder(folder: Path) -> None:
            for family, patterns in automatic_families.items():
                for old in family_files(folder, patterns)[keep:]:
                    try:
                        old.unlink()
                    except OSError as exc:
                        log_warning(
                            "Failed to delete old %s backup %s: %s",
                            family,
                            old,
                            exc,
                        )

        # Manual, exported and pre-restore backups are intentionally excluded.
        prune_folder(self.backup_directory)
        preferences = self.preferences()
        if preferences.get("secondary_backup_enabled"):
            try:
                secondary = self.validate_secondary_directory(
                    preferences.get("secondary_backup_directory"),
                    enabled=True,
                )
                assert secondary is not None
                if secondary.is_dir():
                    prune_folder(secondary)
            except Exception as exc:
                log_warning(
                    "Não foi possível aplicar a retenção na segunda localização: %s",
                    exc,
                )
