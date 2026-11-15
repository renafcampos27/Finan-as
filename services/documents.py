"""Filesystem orchestration for transaction documents and invoices."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import threading
import time
import zipfile
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Optional

from utils.app_logging import log_exception, log_warning
from utils.config import attachments_dir, paths_for_database
from utils.helpers import safe_float, sanitize_filename

if TYPE_CHECKING:
    from database.repo import Repo


class AttachmentValidationError(ValueError):
    """The selected file is not an allowed, recognisable document."""


class AttachmentIntegrityError(RuntimeError):
    """A managed attachment no longer matches its recorded fingerprint."""


def attachment_storage_key(transaction_id: int, stored_name: str) -> str:
    """Return the portable path persisted for one managed attachment."""

    transaction_id = int(transaction_id)
    normalised_name = str(stored_name or "").replace("\\", "/").strip()
    name = PurePosixPath(normalised_name).name
    if (
        transaction_id <= 0
        or not name
        or name in {".", ".."}
        or normalised_name != name
    ):
        raise AttachmentIntegrityError(
            "O identificador ou o nome interno do anexo é inválido."
        )
    return (PurePosixPath(str(transaction_id)) / name).as_posix()


def resolve_managed_attachment_path(
    attachments_directory: Path,
    stored_path: str,
    *,
    transaction_id: int = 0,
    stored_name: str = "",
) -> Optional[Path]:
    """Resolve new relative paths and safe legacy absolute paths.

    When an old absolute path belongs to another installation, the canonical
    ``transaction/name`` location is accepted only if the copied file really
    exists below the current managed root.
    """

    try:
        root = Path(attachments_directory).expanduser().resolve()
        raw = str(stored_path or "").strip()
        if raw:
            raw_path = Path(raw).expanduser()
            is_absolute_path = (
                raw_path.is_absolute()
                or raw.startswith(("/", "\\"))
                or bool(re.match(r"^[a-zA-Z]:", raw))
            )
            if is_absolute_path:
                try:
                    candidate = raw_path.resolve()
                    candidate.relative_to(root)
                    return candidate
                except (ValueError, OSError):
                    candidate = None
            else:
                clean_rel = raw.replace("\\", "/").lstrip("/")
                relative = PurePosixPath(clean_rel)
                if relative.parts and not any(
                    part in {"", ".", ".."} for part in relative.parts
                ):
                    try:
                        candidate = root.joinpath(*relative.parts).resolve()
                        candidate.relative_to(root)
                        return candidate
                    except (ValueError, OSError):
                        pass
        if int(transaction_id or 0) > 0 and str(stored_name or "").strip():
            fallback = root.joinpath(
                *PurePosixPath(
                    attachment_storage_key(transaction_id, stored_name)
                ).parts
            ).resolve()
            fallback.relative_to(root)
            if fallback.is_file():
                return fallback
    except (AttachmentIntegrityError, OSError, TypeError, ValueError):
        return None
    return None


def normalise_attachment_storage(
    connection: sqlite3.Connection,
    attachments_directory: Path,
    *,
    require_all: bool,
    rewrite: bool = True,
) -> dict[str, Any]:
    """Validate attachment records and migrate managed paths to relative keys.

    The caller owns the surrounding transaction.  With ``require_all=True`` no
    row is rewritten unless every referenced document is present and matches
    its recorded size/hash, which makes the function suitable for restores and
    data-location changes.
    """

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='transaction_attachments'"
    ).fetchone()
    if exists is None:
        return {
            "total": 0,
            "validated": 0,
            "normalised": 0,
            "missing_ids": [],
            "outside_ids": [],
            "altered_ids": [],
        }

    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM transaction_attachments ORDER BY id"
        ).fetchall()
    finally:
        connection.row_factory = previous_factory
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(transaction_attachments)"
        ).fetchall()
    }
    root = Path(attachments_directory).expanduser().resolve()
    missing_ids: list[int] = []
    outside_ids: list[int] = []
    altered_ids: list[int] = []
    updates: list[tuple[dict[str, Any], int]] = []
    validated = 0
    normalised = 0

    for row in rows:
        attachment_id = int(row["id"])
        transaction_id = int(row["transaction_id"] or 0)
        stored_name = str(row["stored_name"] or "")
        raw_path = str(row["stored_path"] or "")
        try:
            storage_key = attachment_storage_key(transaction_id, stored_name)
        except AttachmentIntegrityError:
            outside_ids.append(attachment_id)
            continue

        candidate = resolve_managed_attachment_path(
            root,
            raw_path,
            transaction_id=transaction_id,
            stored_name=stored_name,
        )
        raw_is_managed = False
        try:
            raw_candidate = Path(raw_path).expanduser()
            if raw_candidate.is_absolute():
                raw_candidate.resolve().relative_to(root)
                raw_is_managed = True
            elif raw_path:
                relative = PurePosixPath(raw_path.replace("\\", "/"))
                if relative.parts and not any(
                    part in {"", ".", ".."} for part in relative.parts
                ):
                    root.joinpath(*relative.parts).resolve().relative_to(root)
                    raw_is_managed = True
        except (OSError, ValueError):
            raw_is_managed = False

        if candidate is None:
            if raw_is_managed:
                missing_ids.append(attachment_id)
                if rewrite and raw_path != storage_key:
                    updates.append(({"stored_path": storage_key}, attachment_id))
                    normalised += 1
            else:
                outside_ids.append(attachment_id)
            continue
        if not candidate.is_file():
            missing_ids.append(attachment_id)
            if rewrite and raw_path != storage_key:
                updates.append(({"stored_path": storage_key}, attachment_id))
                normalised += 1
            continue

        stat = candidate.stat()
        current_size = int(stat.st_size)
        if current_size == 0:
            altered_ids.append(attachment_id)
            continue
        current_hash = DocumentService._file_digest(candidate)
        stored_size = int(row["file_size"] or 0) if "file_size" in columns else 0
        stored_hash = (
            str(row["content_sha256"] or "").casefold()
            if "content_sha256" in columns
            else ""
        )
        if (stored_size > 0 and stored_size != current_size) or (
            stored_hash and stored_hash != current_hash
        ):
            altered_ids.append(attachment_id)
            continue

        values: dict[str, Any] = {}
        if raw_path != storage_key:
            values["stored_path"] = storage_key
            normalised += 1
        if "content_sha256" in columns:
            values["content_sha256"] = current_hash
        if "file_size" in columns:
            values["file_size"] = current_size
        if "file_mtime_ns" in columns:
            values["file_mtime_ns"] = int(stat.st_mtime_ns)
        if "last_verified_at" in columns:
            values["last_verified_at"] = datetime.now().isoformat(timespec="seconds")
        if rewrite and values:
            updates.append((values, attachment_id))
        validated += 1

    failures = missing_ids + outside_ids + altered_ids
    if require_all and failures:
        raise AttachmentIntegrityError(
            "A localização contém referências de anexos em falta, fora da pasta "
            "gerida ou com conteúdo alterado. Nenhum caminho foi atualizado."
        )

    if rewrite:
        for values, attachment_id in updates:
            assignments = ",".join(f"{column}=?" for column in values)
            connection.execute(
                f"UPDATE transaction_attachments SET {assignments} WHERE id=?",
                (*values.values(), attachment_id),
            )
    return {
        "total": len(rows),
        "validated": validated,
        "normalised": normalised,
        "missing_ids": missing_ids,
        "outside_ids": outside_ids,
        "altered_ids": altered_ids,
    }


class DocumentAuditOperation:
    """Cancellable audit using a SQLite connection owned by the worker thread."""

    def __init__(
        self,
        service: "DocumentService",
        *,
        force: bool,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> None:
        self.service = service
        self.force = bool(force)
        self.progress_callback = progress_callback
        self.generation = service.audit_generation()
        self._cancelled = threading.Event()
        self._connection_lock = threading.Lock()
        self._connection: Optional[sqlite3.Connection] = None

    def cancel(self) -> None:
        self._cancelled.set()
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            try:
                connection.interrupt()
            except sqlite3.Error:
                pass

    def __call__(self) -> dict[str, Any]:
        if self._cancelled.is_set():
            raise RuntimeError("Verificação de documentos cancelada.")
        database_path = Path(self.service.repo.db.path)
        connection = sqlite3.connect(str(database_path), check_same_thread=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")

        with self._connection_lock:
            self._connection = connection
        try:
            records = connection.execute(
                """
                SELECT a.*, t.date AS tx_date, t.description AS tx_desc
                FROM transaction_attachments a
                LEFT JOIN transactions t ON t.id=a.transaction_id
                ORDER BY a.id
                """
            ).fetchall()
            return self.service._audit_storage_records(
                records,
                connection=connection,
                force=self.force,
                cancelled=self._cancelled,
                progress_callback=self.progress_callback,
                expected_generation=self.generation,
            )
        finally:
            with self._connection_lock:
                self._connection = None
            connection.close()


class DocumentService:
    """Copy and remove attachment files while ``Repo`` persists their records."""

    MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
    MAX_OFFICE_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
    MAX_OFFICE_MEMBERS = 2_000
    ALLOWED_EXTENSIONS = frozenset(
        {
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
            ".csv",
            ".xlsx",
            ".xls",
            ".docx",
            ".txt",
        }
    )
    INTERNAL_PREVIEW_EXTENSIONS = frozenset(
        {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
    )
    ACTIVE_WARNING_EXTENSIONS = frozenset({".csv", ".xlsx", ".xls", ".docx"})

    def __init__(
        self,
        repo: "Repo",
        *,
        attachments_directory: Optional[Path] = None,
    ) -> None:
        self.repo = repo
        database_path = getattr(getattr(repo, "db", None), "path", None)
        paths = paths_for_database(Path(database_path) if database_path else None)
        self.attachments_directory = Path(
            attachments_directory
            or (paths.attachments if database_path else attachments_dir())
        )
        self.attachments_directory.mkdir(parents=True, exist_ok=True)
        self._audit_cache: Optional[dict[str, Any]] = None
        self._audit_cached_at = 0.0
        self._audit_cache_lock = threading.Lock()
        self._audit_generation = 0

    def invalidate_audit_cache(self) -> None:
        with self._audit_cache_lock:
            self._audit_generation += 1
            self._audit_cache = None
            self._audit_cached_at = 0.0

    def audit_generation(self) -> int:
        with self._audit_cache_lock:
            return self._audit_generation

    def cached_storage_audit(self) -> Optional[dict[str, Any]]:
        with self._audit_cache_lock:
            if self._audit_cache is None:
                return None
            return self._copy_audit(self._audit_cache)

    @classmethod
    def file_dialog_filter(cls) -> str:
        return (
            "Documentos seguros (*.pdf *.png *.jpg *.jpeg *.webp *.gif "
            "*.csv *.xlsx *.xls *.docx *.txt)"
        )

    @classmethod
    def requires_external_open_warning(cls, path: Path) -> bool:
        return Path(path).suffix.casefold() in cls.ACTIVE_WARNING_EXTENSIONS

    @classmethod
    def supports_internal_preview(cls, path: Path) -> bool:
        return Path(path).suffix.casefold() in cls.INTERNAL_PREVIEW_EXTENSIONS

    @classmethod
    def _validate_office_container(cls, path: Path, expected_root: str) -> None:
        try:
            with zipfile.ZipFile(path, "r") as archive:
                members = archive.infolist()
                if len(members) > cls.MAX_OFFICE_MEMBERS:
                    raise AttachmentValidationError(
                        "O documento contém demasiados elementos internos."
                    )
                total = sum(max(0, int(item.file_size)) for item in members)
                compressed = sum(max(1, int(item.compress_size)) for item in members)
                if total > cls.MAX_OFFICE_UNCOMPRESSED_BYTES:
                    raise AttachmentValidationError(
                        "O documento expande para um tamanho interno excessivo."
                    )
                if total / max(1, compressed) > 200:
                    raise AttachmentValidationError(
                        "O documento tem uma compressão interna anormal."
                    )
                names = {item.filename.replace("\\", "/") for item in members}
                if "[Content_Types].xml" not in names or not any(
                    name.startswith(expected_root) for name in names
                ):
                    raise AttachmentValidationError(
                        "A estrutura interna não corresponde ao formato indicado."
                    )
                damaged = archive.testzip()
                if damaged:
                    raise AttachmentValidationError(
                        "O documento comprimido está danificado."
                    )
        except zipfile.BadZipFile as exc:
            raise AttachmentValidationError(
                "O conteúdo não corresponde a um documento Office válido."
            ) from exc

    @classmethod
    def _validate_content_signature(cls, path: Path) -> None:
        suffix = path.suffix.casefold()
        with path.open("rb") as handle:
            prefix = handle.read(4_096)
        valid = True
        if suffix == ".pdf":
            valid = b"%PDF-" in prefix[:1_024]
        elif suffix == ".png":
            valid = prefix.startswith(b"\x89PNG\r\n\x1a\n")
        elif suffix in {".jpg", ".jpeg"}:
            valid = prefix.startswith(b"\xff\xd8\xff")
        elif suffix == ".gif":
            valid = prefix.startswith((b"GIF87a", b"GIF89a"))
        elif suffix == ".webp":
            valid = (
                len(prefix) >= 12
                and prefix.startswith(b"RIFF")
                and prefix[8:12] == b"WEBP"
            )
        elif suffix == ".xls":
            valid = prefix.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        elif suffix in {".csv", ".txt"}:
            valid = b"\x00" not in prefix
        elif suffix == ".docx":
            cls._validate_office_container(path, "word/")
            return
        elif suffix == ".xlsx":
            cls._validate_office_container(path, "xl/")
            return
        if not valid:
            raise AttachmentValidationError(
                "O conteúdo do ficheiro não corresponde à respetiva extensão."
            )

    @classmethod
    def validate_source(cls, original: Path) -> dict[str, Any]:
        """Validate type, size and basic file structure before copying it."""

        path = Path(original)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(path)
        suffix = path.suffix.casefold()
        if suffix not in cls.ALLOWED_EXTENSIONS:
            allowed = ", ".join(sorted(cls.ALLOWED_EXTENSIONS))
            raise AttachmentValidationError(
                f"Formato não permitido ({suffix or 'sem extensão'}). "
                f"Formatos aceites: {allowed}."
            )
        size = int(path.stat().st_size)
        if size <= 0:
            raise AttachmentValidationError("O ficheiro está vazio.")
        if size > cls.MAX_ATTACHMENT_BYTES:
            limit_mb = cls.MAX_ATTACHMENT_BYTES // (1024 * 1024)
            raise AttachmentValidationError(
                f"O ficheiro excede o limite de {limit_mb} MB."
            )
        cls._validate_content_signature(path)
        return {
            "path": path,
            "extension": suffix,
            "size": size,
            "active_warning": suffix in cls.ACTIVE_WARNING_EXTENSIONS,
            "internal_preview": suffix in cls.INTERNAL_PREVIEW_EXTENSIONS,
        }

    @staticmethod
    def attachment_type(original: Path) -> str:
        extension = Path(original).suffix.lower().lstrip(".")
        if extension in {"jpg", "jpeg", "png", "webp", "gif"}:
            return "Imagem"
        if extension == "pdf":
            return "PDF"
        if extension in {"xlsx", "xls", "csv"}:
            return "Folha de cálculo"
        return extension.upper() or "Ficheiro"

    @staticmethod
    def infer_metadata(original: Path) -> tuple[str, str, Optional[float]]:
        """Infer supplier, invoice date and amount from common filename patterns."""
        stem = Path(original).stem
        date_match = re.search(
            r"(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})|"
            r"(\d{1,2})[-_.](\d{1,2})[-_.](20\d{2})",
            stem,
        )
        invoice_date = ""
        if date_match:
            if date_match.group(1):
                year, month, day = (
                    int(date_match.group(1)),
                    int(date_match.group(2)),
                    int(date_match.group(3)),
                )
            else:
                day, month, year = (
                    int(date_match.group(4)),
                    int(date_match.group(5)),
                    int(date_match.group(6)),
                )
            try:
                invoice_date = date(year, month, day).isoformat()
            except ValueError as exc:
                log_warning(
                    "Ignored invalid invoice date in attachment filename %s: %s",
                    Path(original).name,
                    exc,
                )
            amount_text = (
                stem[: date_match.start()] + " " + stem[date_match.end() :]
            )
        else:
            amount_text = stem

        amount_match = re.search(r"(?<!\d)(\d{1,5}[,.]\d{2})(?!\d)", amount_text)
        total_amount = (
            safe_float(amount_match.group(1).replace(",", "."), None)
            if amount_match
            else None
        )
        supplier = re.sub(
            r"20\d{2}[-_.]\d{1,2}[-_.]\d{1,2}|"
            r"\d{1,2}[-_.]\d{1,2}[-_.]20\d{2}|"
            r"\d{1,5}[,.]\d{2}",
            " ",
            stem,
        )
        supplier = re.sub(r"[_\\-]+", " ", supplier).strip()
        return supplier[:120], invoice_date, total_amount

    def _target_for(self, transaction_id: int, original: Path) -> Path:
        folder = self.attachments_directory / str(int(transaction_id))
        folder.mkdir(parents=True, exist_ok=True)
        folder_str = str(folder.resolve())
        safe_name = sanitize_filename(original.name)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # BUG-DOC-01: Prevent MAX_PATH (260 chars) overflow on Windows
        # Budget for folder + sep + '.' + timestamp + '_' + safe_name + '.copying'
        max_allowed = 200
        avail = max(10, max_allowed - len(folder_str) - 36)
        if len(safe_name) > avail:
            orig_p = Path(safe_name)
            ext = orig_p.suffix
            stem = orig_p.stem
            max_stem = max(1, avail - len(ext))
            safe_name = f"{stem[:max_stem]}{ext}"
        target = folder / f"{timestamp}_{safe_name}"
        counter = 1
        while target.exists():
            target = folder / f"{timestamp}_{counter}_{safe_name}"
            counter += 1
        return target

    @staticmethod
    def _file_digest(
        path: Path,
        cancelled: Optional[threading.Event] = None,
    ) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                if cancelled is not None and cancelled.is_set():
                    raise RuntimeError("Verificação de documentos cancelada.")
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _flush_file(path: Path) -> None:
        with Path(path).open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())

    def attach_file(
        self,
        transaction_id: int,
        original: Path,
        description: str = "",
    ) -> Path:
        original = Path(original)
        validation = self.validate_source(original)
        original_size = int(validation["size"])
        original_digest = self._file_digest(original)

        target = self._target_for(transaction_id, original)
        temporary = target.with_name(f".{target.name}.copying")
        try:
            shutil.copy2(original, temporary)
            self._flush_file(temporary)
            if (
                original_size != temporary.stat().st_size
                or original_digest != self._file_digest(temporary)
            ):
                raise OSError(
                    "A cópia do documento não corresponde ao ficheiro original."
                )
            temporary.replace(target)
            self._flush_file(target)
            target_validation = self.validate_source(target)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
            except OSError as cleanup_error:
                log_exception(
                    "Failed to remove incomplete attachment copy", cleanup_error
                )
            raise
        supplier, invoice_date, total_amount = self.infer_metadata(original)
        content_sha256 = self._file_digest(target)
        try:
            self.repo.add_transaction_attachment(
                transaction_id=int(transaction_id),
                file_name=original.name,
                stored_name=target.name,
                stored_path=attachment_storage_key(transaction_id, target.name),
                original_path=str(original),
                file_type=self.attachment_type(original),
                description=str(description or ""),
                supplier=supplier,
                invoice_date=invoice_date,
                total_amount=total_amount,
                content_sha256=content_sha256,
                file_size=int(target_validation["size"]),
                file_mtime_ns=int(target.stat().st_mtime_ns),
            )
        except Exception:
            try:
                target.unlink(missing_ok=True)
            except OSError as cleanup_error:
                log_exception(
                    "Failed to remove orphaned attachment copy", cleanup_error
                )
            raise
        self.invalidate_audit_cache()
        return target

    def _managed_path(
        self,
        stored_path: str,
        *,
        transaction_id: int = 0,
        stored_name: str = "",
    ) -> Optional[Path]:
        candidate = resolve_managed_attachment_path(
            self.attachments_directory,
            stored_path,
            transaction_id=transaction_id,
            stored_name=stored_name,
        )
        if candidate is None:
            log_warning(
                "Skipped attachment path outside managed folder: %s", stored_path
            )
        return candidate

    @staticmethod
    def _row_value(row: Any, key: str, default: Any = "") -> Any:
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default

    def verify_attachment_record(
        self,
        row: Any,
        *,
        update_verification: bool = True,
    ) -> dict[str, Any]:
        """Verify one managed attachment immediately before it is opened."""

        attachment_id = int(self._row_value(row, "id", 0) or 0)
        candidate = self._managed_path(
            str(self._row_value(row, "stored_path", "") or ""),
            transaction_id=int(self._row_value(row, "transaction_id", 0) or 0),
            stored_name=str(self._row_value(row, "stored_name", "") or ""),
        )
        if candidate is None:
            raise AttachmentIntegrityError(
                "O anexo aponta para fora da pasta protegida da aplicação."
            )
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        validation = self.validate_source(candidate)
        current_size = int(validation["size"])
        current_hash = self._file_digest(candidate)
        stored_size = int(self._row_value(row, "file_size", 0) or 0)
        stored_hash = str(self._row_value(row, "content_sha256", "") or "").casefold()
        if stored_size > 0 and current_size != stored_size:
            raise AttachmentIntegrityError(
                "O tamanho do anexo mudou desde que foi guardado."
            )
        if stored_hash and current_hash != stored_hash:
            raise AttachmentIntegrityError(
                "A assinatura SHA-256 do anexo já não corresponde ao original."
            )
        if update_verification and attachment_id > 0:
            self.repo.update_transaction_attachment_integrity(
                attachment_id,
                content_sha256=current_hash,
                file_size=current_size,
                file_mtime_ns=int(candidate.stat().st_mtime_ns),
            )
            self.invalidate_audit_cache()
        return {
            **validation,
            "id": attachment_id,
            "sha256": current_hash,
            "stored_hash": stored_hash,
            "stored_size": stored_size,
            "verified": True,
        }

    def _remove_managed_paths(
        self, transaction_id: int, stored_paths: list[str]
    ) -> int:
        removed = 0
        for stored_path in stored_paths:
            candidate = self._managed_path(stored_path)
            if candidate is None:
                continue
            try:
                if candidate.exists():
                    candidate.unlink()
                    removed += 1
            except PermissionError as exc:
                log_warning(
                    "Attachment file is locked by another process on Windows: %s (%s)",
                    candidate,
                    exc,
                )
            except OSError as exc:
                log_exception("Failed to remove transaction attachment file", exc)

        transaction_folder = self.attachments_directory / str(int(transaction_id))
        try:
            if transaction_folder.exists() and not any(transaction_folder.iterdir()):
                transaction_folder.rmdir()
        except PermissionError as exc:
            log_warning(
                "Attachment folder is locked by another process: %s (%s)",
                transaction_folder,
                exc,
            )
        except OSError as exc:
            log_exception("Failed to remove empty transaction attachment folder", exc)
        return removed

    def remove_transaction_files(self, transaction_id: int) -> int:
        stored_paths = [
            str(row["stored_path"] or "")
            for row in self.repo.transaction_attachments(int(transaction_id))
        ]
        removed = self._remove_managed_paths(int(transaction_id), stored_paths)
        self.invalidate_audit_cache()
        return removed

    def delete_transaction(self, transaction_id: int) -> int:
        return self.delete_transactions([transaction_id])

    def delete_transactions(self, transaction_ids: list[int]) -> int:
        """Delete a database selection first, then clean its managed files.

        A failed database operation therefore never leaves attachment rows pointing
        at files that were already removed.  A later filesystem cleanup failure is
        safer: it can only leave an orphan file, which is logged and can be retried.
        """
        ids = list(
            dict.fromkeys(int(value) for value in transaction_ids if int(value) > 0)
        )
        expand = getattr(self.repo, "expand_transfer_linked_ids", None)
        if callable(expand):
            ids = list(dict.fromkeys(int(value) for value in expand(ids)))
        if not ids:
            return 0
        stored_paths = {
            transaction_id: [
                str(row["stored_path"] or "")
                for row in self.repo.transaction_attachments(transaction_id)
            ]
            for transaction_id in ids
        }
        self.repo.delete_transactions(ids)
        self.invalidate_audit_cache()
        return sum(
            self._remove_managed_paths(transaction_id, stored_paths[transaction_id])
            for transaction_id in ids
        )

    def clear_all_transactions(self) -> dict[str, int]:
        """Clear the complete ledger after its backup has been created by the UI.

        Database removal is committed before managed attachment files are
        deleted.  A database failure therefore leaves every file untouched; a
        later filesystem failure can only leave a harmless orphan that the
        document audit can detect and clean.
        """

        rows = self.repo.q(
            "SELECT transaction_id,stored_path FROM transaction_attachments "
            "ORDER BY transaction_id,id"
        )
        stored_paths: dict[int, list[str]] = {}
        for row in rows:
            transaction_id = int(row["transaction_id"])
            stored_paths.setdefault(transaction_id, []).append(
                str(row["stored_path"] or "")
            )
        removed_movements = int(self.repo.clear_all_transactions())
        removed_files = sum(
            self._remove_managed_paths(transaction_id, paths)
            for transaction_id, paths in stored_paths.items()
        )
        self.invalidate_audit_cache()
        return {
            "movements": removed_movements,
            "attachment_records": len(rows),
            "attachment_files": removed_files,
        }

    @staticmethod
    def _copy_audit(audit: dict[str, Any]) -> dict[str, Any]:
        return {
            key: list(value) if isinstance(value, list) else value
            for key, value in audit.items()
        }

    def audit_storage_operation(
        self,
        *,
        force: bool = False,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> DocumentAuditOperation:
        return DocumentAuditOperation(
            self,
            force=force,
            progress_callback=progress_callback,
        )

    def audit_storage(self, *, force: bool = False) -> dict[str, Any]:
        """Compare records and files; callers in the UI should use the operation."""

        with self._audit_cache_lock:
            if (
                not force
                and self._audit_cache is not None
                and monotonic() - self._audit_cached_at < 30.0
            ):
                return self._copy_audit(self._audit_cache)
        return self.audit_storage_operation(force=force)()

    def _audit_storage_records(
        self,
        records: list[Any],
        *,
        connection: sqlite3.Connection,
        force: bool,
        cancelled: threading.Event,
        progress_callback: Optional[Callable[[int, int, str], None]],
        expected_generation: int,
    ) -> dict[str, Any]:
        """Audit incrementally and persist every verified fingerprint in one batch."""

        referenced: set[str] = set()
        available_record_ids: list[int] = []
        missing_record_ids: list[int] = []
        outside_record_ids: list[int] = []
        altered_record_ids: list[int] = []
        unverified_record_ids: list[int] = []
        verified_record_ids: list[int] = []
        integrity_updates: list[tuple[str, int, int, int]] = []
        total_records = len(records)
        for index, row in enumerate(records, start=1):
            if cancelled.is_set():
                raise RuntimeError("Verificação de documentos cancelada.")
            if progress_callback is not None:
                progress_callback(
                    index - 1,
                    total_records,
                    str(self._row_value(row, "file_name", "Documento")),
                )
            attachment_id = int(row["id"])
            candidate = self._managed_path(
                str(row["stored_path"] or ""),
                transaction_id=int(row["transaction_id"] or 0),
                stored_name=str(row["stored_name"] or ""),
            )
            if candidate is None:
                outside_record_ids.append(attachment_id)
                continue
            key = os.path.normcase(str(candidate.resolve()))
            referenced.add(key)
            if candidate.is_file():
                available_record_ids.append(attachment_id)
                try:
                    current_size = int(candidate.stat().st_size)
                    current_mtime_ns = int(candidate.stat().st_mtime_ns)
                    stored_size = int(self._row_value(row, "file_size", 0) or 0)
                    stored_mtime_ns = int(self._row_value(row, "file_mtime_ns", 0) or 0)
                    stored_hash = str(
                        self._row_value(row, "content_sha256", "") or ""
                    ).casefold()
                    if stored_size > 0 and current_size != stored_size:
                        altered_record_ids.append(attachment_id)
                        continue
                    unchanged = (
                        not force
                        and bool(stored_hash)
                        and stored_size == current_size
                        and stored_mtime_ns == current_mtime_ns
                    )
                    if unchanged:
                        current_hash = stored_hash
                    else:
                        self.validate_source(candidate)
                        current_hash = self._file_digest(candidate, cancelled)
                        final_stat = candidate.stat()
                        if (
                            int(final_stat.st_size) != current_size
                            or int(final_stat.st_mtime_ns) != current_mtime_ns
                        ):
                            altered_record_ids.append(attachment_id)
                            continue
                    if stored_hash and current_hash != stored_hash:
                        altered_record_ids.append(attachment_id)
                        continue
                    integrity_updates.append(
                        (
                            current_hash,
                            current_size,
                            current_mtime_ns,
                            attachment_id,
                        )
                    )
                    verified_record_ids.append(attachment_id)
                except (AttachmentValidationError, OSError) as exc:
                    altered_record_ids.append(attachment_id)
                    log_warning(
                        "Attachment %s failed its integrity audit: %s",
                        attachment_id,
                        exc,
                    )
            else:
                missing_record_ids.append(attachment_id)

        managed_files: dict[str, Path] = {}
        if progress_callback is not None:
            progress_callback(
                total_records, total_records, "A procurar ficheiros órfãos"
            )
        now = time.time()
        try:
            for candidate in self.attachments_directory.rglob("*"):
                if cancelled.is_set():
                    raise RuntimeError("Verificação de documentos cancelada.")
                if not candidate.is_file():
                    continue
                if candidate.name.startswith(".") or candidate.name.endswith(
                    (".copying", ".tmp")
                ):
                    continue
                try:
                    if now - candidate.stat().st_mtime < 120.0:
                        continue
                except OSError:
                    continue
                managed = self._managed_path(str(candidate))
                if managed is not None:
                    managed_files[os.path.normcase(str(managed.resolve()))] = managed
        except OSError as exc:
            log_exception("Failed to scan attachment storage", exc)

        orphan_files = [
            str(path) for key, path in managed_files.items() if key not in referenced
        ]
        managed_bytes = 0
        for path in managed_files.values():
            try:
                managed_bytes += int(path.stat().st_size)
            except OSError as exc:
                log_exception("Failed to read attachment file size", exc)

        if cancelled.is_set():
            raise RuntimeError("Verificação de documentos cancelada.")
        if integrity_updates:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    """
                    UPDATE transaction_attachments
                    SET content_sha256=?,file_size=?,file_mtime_ns=?,
                        last_verified_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    integrity_updates,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                unverified_record_ids.extend(verified_record_ids)
                verified_record_ids.clear()
                raise

        audit: dict[str, Any] = {
            "total_records": len(records),
            "available_records": len(available_record_ids),
            "available_record_ids": available_record_ids,
            "missing_records": len(missing_record_ids),
            "missing_record_ids": missing_record_ids,
            "outside_records": len(outside_record_ids),
            "outside_record_ids": outside_record_ids,
            "altered_records": len(altered_record_ids),
            "altered_record_ids": altered_record_ids,
            "unverified_records": len(unverified_record_ids),
            "unverified_record_ids": unverified_record_ids,
            "verified_records": len(verified_record_ids),
            "verified_record_ids": verified_record_ids,
            "orphan_files": orphan_files,
            "orphan_count": len(orphan_files),
            "managed_bytes": managed_bytes,
            "healthy": not (
                missing_record_ids
                or outside_record_ids
                or altered_record_ids
                or unverified_record_ids
                or orphan_files
            ),
        }
        with self._audit_cache_lock:
            stale = self._audit_generation != int(expected_generation)
            audit["stale"] = stale
            if not stale:
                self._audit_cache = self._copy_audit(audit)
                self._audit_cached_at = monotonic()
        if progress_callback is not None:
            progress_callback(total_records, total_records, "Verificação concluída")
        return self._copy_audit(audit)

    def _prune_empty_managed_directories(self) -> None:
        try:
            directories = sorted(
                (
                    path
                    for path in self.attachments_directory.rglob("*")
                    if path.is_dir()
                ),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for directory in directories:
                managed = self._managed_path(str(directory))
                if managed is not None and not any(managed.iterdir()):
                    managed.rmdir()
        except OSError as exc:
            log_exception("Failed to remove empty attachment folder", exc)

    def remove_orphan_files(self) -> int:
        audit = self.audit_storage(force=True)
        removed = 0
        rows = self.repo.q(
            "SELECT stored_path, transaction_id, stored_name FROM transaction_attachments"
        )
        referenced_norm = set()
        for r in rows:
            p = self._managed_path(
                str(r["stored_path"] or ""),
                transaction_id=int(r["transaction_id"] or 0),
                stored_name=str(r["stored_name"] or ""),
            )
            if p is not None:
                referenced_norm.add(os.path.normcase(str(p.resolve())))

        now = time.time()
        for stored_path in audit["orphan_files"]:
            candidate = self._managed_path(str(stored_path))
            if candidate is None:
                continue
            cand_norm = os.path.normcase(str(candidate.resolve()))
            if cand_norm in referenced_norm:
                log_warning(
                    "Skipped deletion of referenced attachment file: %s",
                    candidate,
                )
                continue
            try:
                if candidate.is_file():
                    try:
                        if now - candidate.stat().st_mtime < 120.0:
                            continue
                    except OSError:
                        pass
                    candidate.unlink()
                    removed += 1
            except PermissionError as exc:
                log_warning(
                    "Orphan attachment file is locked by another process on Windows: %s (%s)",
                    candidate,
                    exc,
                )
            except OSError as exc:
                log_exception("Failed to remove orphan attachment file", exc)
        self._prune_empty_managed_directories()
        self.invalidate_audit_cache()
        return removed

    prune_orphans = remove_orphan_files

    def remove_missing_references(self) -> int:
        audit = self.audit_storage(force=True)
        removed = self.repo.delete_transaction_attachments(
            list(audit["missing_record_ids"])
        )
        self.invalidate_audit_cache()
        return removed
