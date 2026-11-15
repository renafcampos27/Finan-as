"""Reversible change history for movement imports, edits and deletions."""

from __future__ import annotations

import json
import shutil
import time
import zlib
from datetime import date
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, TypeVar

try:
    import zstandard as zstd
except ImportError:
    zstd = None

from utils.app_logging import log_exception, log_warning
from utils.config import paths_for_database
from services.backup_crypto import SNAPSHOT_MIN_RETENTION_SECONDS
from utils.helpers import chunked_iterable
from services.documents import (
    attachment_storage_key,
    resolve_managed_attachment_path,
)

if TYPE_CHECKING:
    from database.repo import Repo


T = TypeVar("T")


def compress_payload(data: str | bytes, threshold: int = 1024) -> bytes:
    """Compress a payload losslessly using zstandard or zlib if size >= threshold.

    Returns bytes prefixed with 'ZSTD:', 'ZLIB:', or 'RAW:'.
    """
    if isinstance(data, str):
        raw_bytes = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(data)
    else:
        raw_bytes = str(data or "").encode("utf-8")

    if len(raw_bytes) < max(0, int(threshold)):
        return b"RAW:" + raw_bytes

    if zstd is not None:
        try:
            cctx = zstd.ZstdCompressor(level=3)
            return b"ZSTD:" + cctx.compress(raw_bytes)
        except Exception:
            pass

    compressed = zlib.compress(raw_bytes, level=9)
    return b"ZLIB:" + compressed


def decompress_payload(data: bytes | str) -> str:
    """Decompress a payload compressed with compress_payload, or decode raw strings.

    Returns the exact original string with 100% fidelity.
    """
    if isinstance(data, str):
        if data.startswith("RAW:"):
            return data[4:]
        if data.startswith("ZLIB:"):
            try:
                return zlib.decompress(data[5:].encode("latin1")).decode("utf-8")
            except Exception:
                pass
        return data

    if not isinstance(data, (bytes, bytearray, memoryview)):
        return str(data or "")

    data_bytes = bytes(data)
    if data_bytes.startswith(b"RAW:"):
        return data_bytes[4:].decode("utf-8")
    if data_bytes.startswith(b"ZSTD:"):
        if zstd is not None:
            dctx = zstd.ZstdDecompressor()
            return dctx.decompress(data_bytes[5:]).decode("utf-8")
        else:
            raise RuntimeError("zstandard is required to decompress this payload")
    if data_bytes.startswith(b"ZLIB:"):
        return zlib.decompress(data_bytes[5:]).decode("utf-8")
    if data_bytes.startswith(b"\x28\xb5\x2f\xfd"):  # Standard Zstandard frame magic
        if zstd is not None:
            dctx = zstd.ZstdDecompressor()
            return dctx.decompress(data_bytes).decode("utf-8")
    # Try zlib decompress in case it is a raw zlib stream
    try:
        return zlib.decompress(data_bytes).decode("utf-8")
    except Exception:
        pass
    return data_bytes.decode("utf-8")


class AttachmentRestoreTransientError(RuntimeError):
    """Raised when attachment restore fails but partial work was cleanly compensated."""


class AttachmentCompensationError(RuntimeError):
    """Raised when attachment restore failed and rolling back previously restored files failed."""

    def __init__(
        self, message: str, *, uncompensated: list[dict[str, str]] | None = None
    ) -> None:
        super().__init__(message)
        self.uncompensated = uncompensated or []


__all__ = [
    "AttachmentCompensationError",
    "AttachmentRestoreTransientError",
    "ChangeJournalService",
    "compress_payload",
    "decompress_payload",
]


class ChangeJournalService:
    """Persist small, explainable undo records beside the finance data."""

    compress_payload = staticmethod(compress_payload)
    decompress_payload = staticmethod(decompress_payload)

    _SNAPSHOT_TABLES = frozenset(
        {
            "transactions",
            "transaction_meta",
            "transaction_splits",
            "transaction_attachments",
            "subscription_occurrences",
            "scheduled_payment_occurrences",
        }
    )
    _STATE_COLUMNS = {
        "subscriptions": frozenset({"last_used_date", "next_renewal_date"}),
        "scheduled_payments": frozenset({"months_paid", "active"}),
        "goals": frozenset({"saved_amount"}),
    }
    _STATE_TABLES = frozenset(_STATE_COLUMNS)
    _ALLOWED_JOURNAL_TABLES = _SNAPSHOT_TABLES | _STATE_TABLES | {"holdings"}
    _UNDO_KINDS = frozenset(
        {
            "delete_created",
            "restore_existing",
            "restore_deleted",
            "restore_deleted_holding",
            "restore_investment_disposal",
        }
    )
    _MAX_UNDO_ROWS = 100_000
    _MAX_UNDO_FILES = 10_000

    def __init__(
        self,
        repo: "Repo",
        *,
        attachments_directory: Optional[Path] = None,
        journal_storage: Optional[Path] = None,
    ) -> None:
        self.repo = repo
        database_path = getattr(getattr(repo, "db", None), "path", None)
        paths = paths_for_database(Path(database_path) if database_path else None)
        self.attachments_directory = Path(attachments_directory or paths.attachments)
        self.journal_storage = Path(journal_storage or paths.history)
        self.attachments_directory.mkdir(parents=True, exist_ok=True)
        self.journal_storage.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _normalise_ids(values: Iterable[int]) -> list[int]:
        return list(
            dict.fromkeys(int(value) for value in values if int(value or 0) > 0)
        )

    @staticmethod
    def _quote_identifier(value: str) -> str:
        return '"' + str(value).replace('"', '""') + '"'

    @classmethod
    def _validated_row_columns(
        cls,
        connection,
        table: str,
        row: dict[str, Any],
    ) -> list[str]:
        if table not in cls._ALLOWED_JOURNAL_TABLES:
            raise RuntimeError(
                "O histórico de alterações contém uma tabela não permitida."
            )
        available = {
            str(info[1])
            for info in connection.execute(
                f"PRAGMA table_info({cls._quote_identifier(table)})"
            ).fetchall()
        }
        columns = list(row)
        if not columns or any(column not in available for column in columns):
            raise RuntimeError(
                f"O histórico de alterações contém campos inválidos em {table}."
            )
        if any(isinstance(value, (dict, list, tuple)) for value in row.values()):
            raise RuntimeError("O histórico de alterações contém valores inválidos.")
        return columns

    def _table_exists(self, table: str) -> bool:
        row = self.repo.db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    def _rows_for_ids(
        self,
        table: str,
        column: str,
        ids: list[int],
    ) -> list[dict[str, Any]]:
        if not ids or not self._table_exists(table):
            return []
        all_rows: list[dict[str, Any]] = []
        for chunk in chunked_iterable(ids, chunk_size=400):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.repo.db.conn.execute(
                f"SELECT * FROM {table} WHERE {column} IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            all_rows.extend(dict(row) for row in rows)
        return all_rows

    def capture_transactions(
        self,
        transaction_ids: Iterable[int],
        *,
        include_attachments: bool = False,
    ) -> dict[str, Any]:
        """Capture the relational state needed to restore selected movements."""

        ids = self._normalise_ids(transaction_ids)
        expand = getattr(self.repo, "expand_transfer_linked_ids", None)
        if callable(expand):
            ids = self._normalise_ids(expand(ids))
        return {
            "transaction_ids": ids,
            "transactions": self._rows_for_ids("transactions", "id", ids),
            "transaction_meta": self._rows_for_ids(
                "transaction_meta", "transaction_id", ids
            ),
            "transaction_splits": self._rows_for_ids(
                "transaction_splits", "transaction_id", ids
            ),
            "transaction_attachments": (
                self._rows_for_ids("transaction_attachments", "transaction_id", ids)
                if include_attachments
                else []
            ),
            "subscription_occurrences": self._rows_for_ids(
                "subscription_occurrences", "transaction_id", ids
            ),
            "scheduled_payment_occurrences": self._rows_for_ids(
                "scheduled_payment_occurrences", "transaction_id", ids
            ),
        }

    def capture_state(
        self,
        targets: dict[str, dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Capture selected fields from related rows changed with new movements."""

        snapshot: dict[str, list[dict[str, Any]]] = {}
        total_rows = 0
        for table, specification in dict(targets or {}).items():
            if table not in self._STATE_TABLES or not isinstance(specification, dict):
                raise ValueError("O estado associado ao histórico não é permitido.")
            ids = self._normalise_ids(specification.get("ids", []))
            requested_columns = tuple(
                dict.fromkeys(
                    str(column or "").strip()
                    for column in specification.get("columns", ())
                    if str(column or "").strip() and str(column or "").strip() != "id"
                )
            )
            if not ids or not requested_columns:
                snapshot[table] = []
                continue
            if any(
                column not in self._STATE_COLUMNS[table] for column in requested_columns
            ):
                raise ValueError("O estado associado usa campos não permitidos.")
            available = {
                str(info[1])
                for info in self.repo.db.conn.execute(
                    f"PRAGMA table_info({self._quote_identifier(table)})"
                ).fetchall()
            }
            if "id" not in available or any(
                column not in available for column in requested_columns
            ):
                raise ValueError("O estado associado usa campos desconhecidos.")
            placeholders = ",".join("?" for _ in ids)
            columns_sql = ",".join(
                self._quote_identifier(column) for column in ("id", *requested_columns)
            )
            rows = self.repo.db.conn.execute(
                f"SELECT {columns_sql} FROM {self._quote_identifier(table)} "
                f"WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
            if len(rows) != len(ids):
                raise RuntimeError(
                    "Um registo associado ao movimento já não existe. "
                    "Atualiza a página e tenta novamente."
                )
            snapshot[table] = [dict(row) for row in rows]
            total_rows += len(rows)
            if total_rows > self._MAX_UNDO_ROWS:
                raise RuntimeError(
                    "O estado a guardar no histórico é demasiado grande."
                )
        return snapshot

    def _insert_journal(
        self,
        action_type: str,
        label: str,
        payload: dict[str, Any],
    ) -> int:
        raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        compressed = compress_payload(raw_json)
        cursor = self.repo.db.conn.execute(
            """
            INSERT INTO change_journal(action_type,label,payload_json,status)
            VALUES(?,?,?,'available')
            """,
            (
                str(action_type or "alteracao"),
                str(label or "Alteração"),
                compressed if compressed.startswith((b"ZSTD:", b"ZLIB:")) else raw_json,
            ),
        )
        return int(cursor.lastrowid)

    def record_created_transactions(
        self,
        transaction_ids: Iterable[int],
        *,
        label: str,
        action_type: str = "import",
        commit: bool = True,
        prune_after: Optional[bool] = None,
        restore_state: Optional[dict[str, list[dict[str, Any]]]] = None,
        delete_linked_occurrences: bool = False,
    ) -> Optional[int]:
        ids = self._normalise_ids(transaction_ids)
        expand = getattr(self.repo, "expand_transfer_linked_ids", None)
        if callable(expand):
            ids = self._normalise_ids(expand(ids))
        if not ids:
            return None
        payload: dict[str, Any] = {
            "undo_kind": "delete_created",
            "transaction_ids": ids,
        }

        if restore_state:
            payload["restore_state"] = self._validated_state_snapshot(restore_state)
        if delete_linked_occurrences:
            payload["delete_linked_occurrences"] = True
        journal_id = self._insert_journal(
            action_type,
            label,
            payload,
        )
        if commit:
            self.repo.db.conn.commit()
        if prune_after is None:
            prune_after = commit
        if prune_after:
            self.prune()
        return journal_id

    def record_transaction_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        label: str,
        action_type: str,
        commit: bool = True,
        prune_after: Optional[bool] = None,
    ) -> Optional[int]:
        if not snapshot.get("transactions"):
            return None
        payload = {
            "undo_kind": "restore_existing",
            "snapshot": snapshot,
        }
        journal_id = self._insert_journal(action_type, label, payload)
        if commit:
            self.repo.db.conn.commit()
        if prune_after is None:
            prune_after = commit
        if prune_after:
            self.prune()
        return journal_id

    def run_with_created_transactions(
        self,
        mutation: Callable[[], list[int]],
        *,
        label: str | Callable[[list[int]], str],
        action_type: str = "import",
        state_targets: Optional[dict[str, dict[str, Any]]] = None,
        delete_linked_occurrences: bool = False,
    ) -> list[int]:
        """Commit created movements and their undo entry as one unit."""

        connection = self.repo.db.conn
        connection.commit()
        restore_state = self.capture_state(state_targets or {})
        connection.execute("SAVEPOINT mutation_with_created_journal")
        try:
            created_ids = self._normalise_ids(mutation())
            if created_ids:
                resolved_label = label(created_ids) if callable(label) else label
                self.record_created_transactions(
                    created_ids,
                    label=resolved_label,
                    action_type=action_type,
                    commit=False,
                    prune_after=False,
                    restore_state=restore_state,
                    delete_linked_occurrences=delete_linked_occurrences,
                )
            connection.execute("RELEASE SAVEPOINT mutation_with_created_journal")
            connection.commit()
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT mutation_with_created_journal")
            connection.execute("RELEASE SAVEPOINT mutation_with_created_journal")
            connection.rollback()
            raise
        self._prune_after_committed_change()
        return created_ids

    def run_with_snapshot(
        self,
        transaction_ids: Iterable[int],
        mutation: Callable[[], T],
        *,
        label: str | Callable[[T], str],
        action_type: str,
        journal_when: Optional[Callable[[T], bool]] = None,
    ) -> T:
        """Commit a mutation and its pre-change undo snapshot together."""

        ids = self._normalise_ids(transaction_ids)
        expand = getattr(self.repo, "expand_transfer_linked_ids", None)
        if callable(expand):
            ids = self._normalise_ids(expand(ids))
        if not ids:
            raise ValueError("É necessário indicar pelo menos um movimento.")
        connection = self.repo.db.conn
        connection.commit()
        snapshot = self.capture_transactions(ids)

        if len(snapshot.get("transactions", [])) != len(ids):
            raise RuntimeError(
                "Um ou mais movimentos já não existem. Atualiza a lista e tenta novamente."
            )
        connection.execute("SAVEPOINT mutation_with_snapshot_journal")
        try:
            result = mutation()
            should_journal = journal_when(result) if journal_when else True
            if should_journal:
                resolved_label = label(result) if callable(label) else label
                self.record_transaction_snapshot(
                    snapshot,
                    label=resolved_label,
                    action_type=action_type,
                    commit=False,
                    prune_after=False,
                )
            connection.execute("RELEASE SAVEPOINT mutation_with_snapshot_journal")
            connection.commit()
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT mutation_with_snapshot_journal")
            connection.execute("RELEASE SAVEPOINT mutation_with_snapshot_journal")
            connection.rollback()
            raise
        self._prune_after_committed_change()
        return result

    def _prune_after_committed_change(self) -> None:
        """Retention cleanup must never turn a committed action into a false failure."""

        try:
            self.prune()
        except Exception as exc:
            log_exception("Failed to prune committed change history", exc)

    def _safe_attachment_path(
        self,
        value: str,
        *,
        transaction_id: int = 0,
        stored_name: str = "",
    ) -> Optional[Path]:
        candidate = resolve_managed_attachment_path(
            self.attachments_directory,
            value,
            transaction_id=transaction_id,
            stored_name=stored_name,
        )
        if candidate is not None:
            return candidate
        if int(transaction_id or 0) > 0 and str(stored_name or "").strip():
            try:
                key = attachment_storage_key(transaction_id, stored_name)
                root = self.attachments_directory.resolve()
                candidate = root.joinpath(*PurePosixPath(key).parts).resolve()
                candidate.relative_to(root)
                return candidate
            except (OSError, ValueError):
                pass
        log_warning("Ignored undo attachment outside managed folder: %s", value)
        return None

    def _file_records(
        self,
        journal_id: int,
        snapshot: dict[str, Any],
    ) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        root = self.attachments_directory.resolve()
        for row in snapshot.get("transaction_attachments", []):
            source = self._safe_attachment_path(
                str(row.get("stored_path") or ""),
                transaction_id=int(row.get("transaction_id") or 0),
                stored_name=str(row.get("stored_name") or ""),
            )
            if source is None:
                continue
            relative = source.relative_to(root)
            relative_key = PurePosixPath(*relative.parts).as_posix()
            records.append(
                {
                    "source": relative_key,
                    "stash": relative_key,
                }
            )
        return records

    def _validated_file_records(
        self,
        records: Any,
        journal_id: int,
    ) -> list[dict[str, str]]:
        if not isinstance(records, list) or len(records) > self._MAX_UNDO_FILES:
            raise RuntimeError("O histórico de alterações contém anexos inválidos.")
        attachment_root = self.attachments_directory.resolve()
        stash_root = (self.journal_storage / str(int(journal_id))).resolve()
        validated: list[dict[str, str]] = []

        def relative_record_path(value: Any, root: Path) -> Path:
            raw = str(value or "").strip()
            candidate = Path(raw).expanduser()
            if candidate.is_absolute():
                try:
                    return candidate.resolve().relative_to(root)
                except (OSError, ValueError):
                    # Legacy undo payloads stored absolute paths.  After a data
                    # move, recover only their canonical transaction/name tail.
                    parts = PurePosixPath(raw.replace("\\", "/")).parts
                    if len(parts) >= 2 and parts[-2].isdigit():
                        return Path(parts[-2], parts[-1])
                    raise
            relative = PurePosixPath(raw.replace("\\", "/"))
            if not relative.parts or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                raise ValueError("invalid relative attachment path")
            return Path(*relative.parts)

        for record in records:
            if not isinstance(record, dict) or set(record) != {"source", "stash"}:
                raise RuntimeError(
                    "O histórico de alterações contém um anexo inválido."
                )
            try:
                source_relative = relative_record_path(
                    record["source"], attachment_root
                )
                stash_relative = relative_record_path(record["stash"], stash_root)
                source = (attachment_root / source_relative).resolve()
                stash = (stash_root / stash_relative).resolve()
                source.relative_to(attachment_root)
                stash.relative_to(stash_root)
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError(
                    "O histórico de alterações tentou aceder fora das pastas autorizadas."
                ) from exc
            if source_relative != stash_relative:
                raise RuntimeError(
                    "O histórico de alterações contém um destino de anexo incoerente."
                )
            validated.append({"source": str(source), "stash": str(stash)})
        return validated

    def _validated_snapshot(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RuntimeError("O estado guardado no histórico está danificado.")
        allowed_keys = {"transaction_ids", *self._SNAPSHOT_TABLES}
        if any(key not in allowed_keys for key in value):
            raise RuntimeError("O estado guardado contém campos não permitidos.")
        try:
            transaction_ids = self._normalise_ids(value.get("transaction_ids", []))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("O histórico contém identificadores inválidos.") from exc
        if len(transaction_ids) > self._MAX_UNDO_ROWS:
            raise RuntimeError("O histórico contém demasiados movimentos.")
        result: dict[str, Any] = {"transaction_ids": transaction_ids}
        total_rows = 0
        transaction_id_set = set(transaction_ids)
        for table in self._SNAPSHOT_TABLES:
            rows = value.get(table, [])
            if not isinstance(rows, list):
                raise RuntimeError(f"O histórico contém linhas inválidas em {table}.")
            total_rows += len(rows)
            if total_rows > self._MAX_UNDO_ROWS:
                raise RuntimeError("O histórico contém demasiadas linhas.")
            validated_rows: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise RuntimeError(
                        f"O histórico contém uma linha inválida em {table}."
                    )
                self._validated_row_columns(self.repo.db.conn, table, row)
                if table == "transactions":
                    try:
                        row_id = int(row.get("id") or 0)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(
                            "O histórico contém um movimento inválido."
                        ) from exc
                    if row_id not in transaction_id_set:
                        raise RuntimeError(
                            "O histórico contém um movimento fora da operação original."
                        )
                elif "transaction_id" in row and row.get("transaction_id") is not None:
                    try:
                        linked_id = int(row["transaction_id"])
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(
                            "O histórico contém uma ligação de movimento inválida."
                        ) from exc
                    if linked_id not in transaction_id_set:
                        raise RuntimeError(
                            "O histórico contém uma ligação fora da operação original."
                        )
                if table == "transaction_attachments":
                    stored_path = self._safe_attachment_path(
                        str(row.get("stored_path") or ""),
                        transaction_id=int(row.get("transaction_id") or 0),
                        stored_name=str(row.get("stored_name") or ""),
                    )
                    if stored_path is None:
                        raise RuntimeError(
                            "O histórico contém um anexo fora da pasta autorizada."
                        )
                    row = dict(row)
                    row["stored_path"] = attachment_storage_key(
                        int(row.get("transaction_id") or 0),
                        str(row.get("stored_name") or ""),
                    )
                validated_rows.append(dict(row))
            result[table] = validated_rows
        return result

    def _validated_payload(
        self,
        payload: Any,
        journal_id: int,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError("O registo de alterações está danificado.")
        kind = str(payload.get("undo_kind") or "")
        if kind not in self._UNDO_KINDS:
            raise RuntimeError("Este tipo de alteração não pode ser desfeito.")
        allowed = {
            "delete_created": {
                "undo_kind",
                "transaction_ids",
                "files",
                "restore_state",
                "delete_linked_occurrences",
            },
            "restore_existing": {"undo_kind", "snapshot"},
            "restore_deleted": {"undo_kind", "snapshot", "files"},
            "restore_deleted_holding": {"undo_kind", "holding"},
            "restore_investment_disposal": {
                "undo_kind", "holding", "after_holding", "event_key", "event_value", "snapshot",
            },
        }[kind]
        if any(key not in allowed for key in payload):
            raise RuntimeError("O registo de alterações contém campos não permitidos.")
        validated: dict[str, Any] = {"undo_kind": kind}
        if kind == "delete_created":
            try:
                ids = self._normalise_ids(payload.get("transaction_ids", []))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "O histórico contém identificadores inválidos."
                ) from exc
            if len(ids) > self._MAX_UNDO_ROWS:
                raise RuntimeError("O histórico contém demasiados movimentos.")
            validated["transaction_ids"] = ids
            validated["restore_state"] = self._validated_state_snapshot(
                payload.get("restore_state", {})
            )
            delete_linked = payload.get("delete_linked_occurrences", False)
            if not isinstance(delete_linked, bool):
                raise RuntimeError(
                    "O histórico contém uma opção de ocorrências inválida."
                )
            validated["delete_linked_occurrences"] = delete_linked
        elif kind in {"restore_deleted_holding", "restore_investment_disposal"}:
            holding = payload.get("holding")
            if not isinstance(holding, dict) or not holding.get("name"):
                raise RuntimeError("O registo de alterações do ativo está danificado.")
            self._validated_row_columns(self.repo.db.conn, "holdings", holding)
            validated["holding"] = dict(holding)
            if kind == "restore_investment_disposal":
                from services.tax_reporting import DISPOSAL_PREFIX, build_realized_report

                after = payload.get("after_holding")
                if after is not None:
                    if not isinstance(after, dict) or after.get("id") != holding.get("id"):
                        raise RuntimeError("O estado do investimento está danificado.")
                    self._validated_row_columns(self.repo.db.conn, "holdings", after)
                try:
                    event = json.loads(payload["event_value"])
                    report = build_realized_report([event], date.fromisoformat(event["sale_date"]).year)
                    if report["issues"] or event["holding"] != holding or payload["event_key"] != DISPOSAL_PREFIX + event["id"]:
                        raise ValueError("Invalid disposal provenance")
                except (KeyError, ValueError, TypeError) as exc:
                    raise RuntimeError("O registo fiscal do histórico está danificado.") from exc
                validated.update({
                    "after_holding": after,
                    "event_key": payload["event_key"],
                    "event_value": payload["event_value"],
                    "snapshot": self._validated_snapshot(payload.get("snapshot")),
                })
        else:
            validated["snapshot"] = self._validated_snapshot(payload.get("snapshot"))
        if "files" in payload or kind == "restore_deleted":
            validated["files"] = self._validated_file_records(
                payload.get("files", []),
                journal_id,
            )
        return validated

    def _validated_state_snapshot(
        self,
        value: Any,
    ) -> dict[str, list[dict[str, Any]]]:
        if not isinstance(value, dict):
            raise RuntimeError("O estado associado ao histórico está danificado.")
        if any(table not in self._STATE_TABLES for table in value):
            raise RuntimeError("O estado associado contém uma tabela não permitida.")
        total_rows = 0
        validated: dict[str, list[dict[str, Any]]] = {}
        for table, rows in value.items():
            if not isinstance(rows, list):
                raise RuntimeError(
                    f"O estado associado contém linhas inválidas em {table}."
                )
            total_rows += len(rows)
            if total_rows > self._MAX_UNDO_ROWS:
                raise RuntimeError("O estado associado contém demasiadas linhas.")
            validated_rows: list[dict[str, Any]] = []
            row_ids: set[int] = set()
            for row in rows:
                if not isinstance(row, dict) or "id" not in row or len(row) < 2:
                    raise RuntimeError(
                        f"O estado associado contém uma linha inválida em {table}."
                    )
                self._validated_row_columns(self.repo.db.conn, table, row)
                if any(
                    column != "id" and column not in self._STATE_COLUMNS[table]
                    for column in row
                ):
                    raise RuntimeError(
                        f"O estado associado contém campos não permitidos em {table}."
                    )
                try:
                    row_id = int(row.get("id") or 0)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "O estado associado contém um identificador inválido."
                    ) from exc
                if row_id <= 0:
                    raise RuntimeError(
                        "O estado associado contém um identificador inválido."
                    )
                if row_id in row_ids:
                    raise RuntimeError(
                        "O estado associado repete o mesmo identificador."
                    )
                row_ids.add(row_id)
                validated_rows.append(dict(row))
            validated[table] = validated_rows
        return validated

    @staticmethod
    def _delete_db_transactions(
        connection,
        ids: list[int],
        *,
        delete_linked_occurrences: bool = False,
    ) -> None:
        if not ids:
            return
        for chunk in chunked_iterable(ids, chunk_size=400):
            placeholders = ",".join("?" for _ in chunk)
            params = tuple(chunk)
            for table in (
                "subscription_occurrences",
                "scheduled_payment_occurrences",
            ):
                try:
                    if delete_linked_occurrences:
                        connection.execute(
                            f"DELETE FROM {table} WHERE transaction_id IN ({placeholders})",
                            params,
                        )
                    else:
                        connection.execute(
                            f"UPDATE {table} SET transaction_id=NULL "
                            f"WHERE transaction_id IN ({placeholders})",
                            params,
                        )
                except Exception as exc:
                    if "no such table" not in str(exc).casefold():
                        raise
            for table in (
                "transaction_attachments",
                "transaction_splits",
                "transaction_meta",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE transaction_id IN ({placeholders})",
                    params,
                )
            try:
                # BUG-DB-003: transfer legs are compensated exclusively by
                # the trg_transactions_cascade_transfer_leg trigger (it sees
                # both the deleted leg and the sibling legs it cascades);
                # compensating here too would double-count.
                goal_txs = connection.execute(
                    f"SELECT goal_id, amount FROM transactions "
                    f"WHERE id IN ({placeholders}) AND goal_id IS NOT NULL "
                    f"AND (transfer_group_id IS NULL OR transfer_group_id = '')",
                    params,
                ).fetchall()
                for gtx in goal_txs:
                    gid = int(gtx["goal_id"])
                    amt = float(gtx["amount"] or 0)
                    connection.execute(
                        "UPDATE goals SET saved_amount = MAX(0.0, saved_amount + ?) WHERE id = ?",
                        (amt, gid),
                    )
            except Exception as exc:
                if "no such table" not in str(exc).casefold():
                    raise
            connection.execute(
                f"DELETE FROM transactions WHERE id IN ({placeholders})",
                params,
            )

    def _restore_related_state(
        self,
        snapshot: dict[str, list[dict[str, Any]]],
    ) -> None:
        connection = self.repo.db.conn
        for table, rows in snapshot.items():
            for row in rows:
                columns = self._validated_row_columns(connection, table, row)
                row_id = int(row["id"])
                state_columns = [column for column in columns if column != "id"]
                if not state_columns:
                    raise RuntimeError(
                        "O estado associado não contém campos para repor."
                    )
                cursor = connection.execute(
                    f"UPDATE {self._quote_identifier(table)} SET "
                    + ",".join(
                        f"{self._quote_identifier(column)}=?"
                        for column in state_columns
                    )
                    + " WHERE id=?",
                    tuple(row[column] for column in state_columns) + (row_id,),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise RuntimeError(
                        "Não foi possível repor o estado associado ao movimento."
                    )

    def _stash_files(
        self,
        records: list[dict[str, str]],
        journal_id: int,
    ) -> list[dict[str, str]]:
        records = self._validated_file_records(records, journal_id)
        moved: list[dict[str, str]] = []
        for record in records:
            source = Path(record["source"])
            stash = Path(record["stash"])
            if not source.is_file():
                continue
            try:
                stash.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(stash))
                moved.append(record)
            except OSError as exc:
                log_exception("Failed to preserve an attachment for undo", exc)
        return moved

    def _pending_stash_path(self, stash: Path) -> Path:
        """BUG-AUD-C119: staging path next to the final stash destination."""

        return stash.with_name(stash.name + ".pending_stash")

    def _preflight_stash_files(
        self,
        records: list[dict[str, str]],
        journal_id: int,
    ) -> tuple[list[dict[str, str]], int]:
        """BUG-AUD-C119 phase 1: stage files to ``.pending_stash`` paths.

        Returns ``(pending_records, restored_count)``.  If any move fails,
        every already-staged file is restored to its original location so the
        filesystem stays consistent with the still-live DB rows.
        """

        records = self._validated_file_records(records, journal_id)
        pending: list[dict[str, str]] = []
        for record in records:
            source = Path(record["source"])
            stash = Path(record["stash"])
            if not source.is_file():
                continue
            staging = self._pending_stash_path(stash)
            try:
                stash.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(staging))
                pending.append(record)
            except OSError as exc:
                log_exception(
                    "Failed to stage an attachment for the undo stash", exc
                )
                uncompensated: list[dict[str, str]] = []
                for done in reversed(pending):
                    done_source = Path(done["source"])
                    done_staging = self._pending_stash_path(Path(done["stash"]))
                    try:
                        done_source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(done_staging), str(done_source))
                    except OSError as restore_exc:
                        log_exception(
                            "Failed to roll back a staged attachment",
                            restore_exc,
                        )
                        uncompensated.append(done)
                if uncompensated:
                    raise AttachmentCompensationError(
                        "Não foi possível preparar os anexos para o histórico "
                        "e a reversão parcial falhou.",
                        uncompensated=uncompensated,
                    ) from exc
                raise AttachmentRestoreTransientError(
                    "Não foi possível preparar os anexos para o histórico; "
                    "os ficheiros foram mantidos no local original e a "
                    "operação pode ser repetida."
                ) from exc
        return pending, 0

    def _publish_stash_files(
        self,
        records: list[dict[str, str]],
        journal_id: int,
    ) -> int:
        """BUG-AUD-C119 phase 2: publish staged files into the final stash."""

        records = self._validated_file_records(records, journal_id)
        published = 0
        for record in records:
            stash = Path(record["stash"])
            staging = self._pending_stash_path(stash)
            if not staging.is_file():
                continue
            try:
                stash.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staging), str(stash))
                published += 1
            except OSError as exc:
                log_exception("Failed to publish a staged attachment", exc)
        return published

    def _restore_files(
        self,
        records: list[dict[str, str]],
        journal_id: int,
    ) -> list[dict[str, str]]:
        records = self._validated_file_records(records, journal_id)
        moved: list[dict[str, str]] = []
        for record in records:
            source = Path(record["source"])
            stash = Path(record["stash"])
            if source.exists() or not stash.is_file():
                continue
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(stash), str(source))
                moved.append(record)
            except OSError as exc:
                log_exception("Failed to restore an attachment from undo", exc)
                # R3: Compensate previously moved files within this operation back to stash
                uncompensated: list[dict[str, str]] = []
                for comp_record in reversed(moved):
                    c_src = Path(comp_record["source"])
                    c_stash = Path(comp_record["stash"])
                    if c_src.is_file():
                        try:
                            c_stash.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(c_src), str(c_stash))
                        except OSError as comp_exc:
                            log_exception(
                                "Failed to roll back an attachment restore during compensation",
                                comp_exc,
                            )
                            uncompensated.append(comp_record)
                if uncompensated:
                    raise AttachmentCompensationError(
                        "Não foi possível repor um anexo e a reversão parcial dos ficheiros falhou.",
                        uncompensated=uncompensated,
                    ) from exc
                raise AttachmentRestoreTransientError(
                    "Não foi possível repor um anexo; a reposição parcial foi totalmente revertida "
                    "e a operação pode ser repetida."
                ) from exc
        return moved

    def _restash_files(
        self,
        records: list[dict[str, str]],
        journal_id: int,
    ) -> list[dict[str, str]]:
        """Restash files back to stash folder. Returns list of uncompensated records (if any)."""
        records = self._validated_file_records(records, journal_id)
        uncompensated: list[dict[str, str]] = []
        for record in reversed(records):
            source = Path(record["source"])
            stash = Path(record["stash"])
            if not source.is_file():
                continue
            try:
                stash.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(stash))
            except OSError as exc:
                log_exception("Failed to roll back an attachment restore", exc)
                uncompensated.append(record)
        return uncompensated

    def delete_transactions(
        self,
        transaction_ids: Iterable[int],
        *,
        label: str,
    ) -> tuple[int, int]:
        """Delete movements while keeping their records and files recoverable."""

        ids = self._normalise_ids(transaction_ids)
        expand = getattr(self.repo, "expand_transfer_linked_ids", None)
        if callable(expand):
            ids = self._normalise_ids(expand(ids))
        if not ids:
            return 0, 0
        snapshot = self.capture_transactions(ids, include_attachments=True)
        if len(snapshot["transactions"]) != len(ids):
            raise RuntimeError(
                "Um ou mais movimentos já não existem. Atualiza a lista e tenta novamente."
            )

        connection = self.repo.db.conn
        connection.commit()
        connection.execute("SAVEPOINT reversible_delete")
        try:
            payload = {
                "undo_kind": "restore_deleted",
                "snapshot": snapshot,
                "files": [],
            }
            journal_id = self._insert_journal("delete", label, payload)
            payload["files"] = self._file_records(journal_id, snapshot)
            raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            compressed = compress_payload(raw_json)
            connection.execute(
                "UPDATE change_journal SET payload_json=? WHERE id=?",
                (
                    compressed if compressed.startswith((b"ZSTD:", b"ZLIB:")) else raw_json,
                    journal_id,
                ),
            )
            self._delete_db_transactions(connection, ids)
            connection.execute("RELEASE SAVEPOINT reversible_delete")
            connection.commit()
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT reversible_delete")
            connection.execute("RELEASE SAVEPOINT reversible_delete")
            connection.rollback()
            raise

        moved = self._stash_files(payload["files"], journal_id)
        self.prune()
        return len(ids), len(moved)

    def delete_holding(
        self,
        holding_id: int,
        *,
        label: str,
    ) -> None:
        """Delete an investment holding while keeping it recoverable via undo."""
        hid = int(holding_id)
        holding_getter = getattr(self.repo, "holding_by_id", None)
        if callable(holding_getter):
            current = holding_getter(hid)
        else:
            row = self.repo.db.conn.execute("SELECT * FROM holdings WHERE id=?", (hid,)).fetchone()
            current = dict(row) if row else None
        if not current:
            raise RuntimeError("O investimento selecionado já não existe.")

        snapshot = dict(current)
        connection = self.repo.db.conn
        connection.commit()
        connection.execute("SAVEPOINT reversible_delete_holding")
        try:
            payload = {
                "undo_kind": "restore_deleted_holding",
                "holding": snapshot,
            }
            self._insert_journal("delete_holding", label, payload)
            connection.execute("DELETE FROM holdings WHERE id=?", (hid,))
            connection.execute("RELEASE SAVEPOINT reversible_delete_holding")
            connection.commit()
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT reversible_delete_holding")
            connection.execute("RELEASE SAVEPOINT reversible_delete_holding")
            connection.rollback()
            raise

        self.prune()

    def record_investment_disposal_undo(
        self, holding: dict[str, Any], after_holding: dict[str, Any] | None,
        event_key: str, event_value: str, transaction_id: int | None,
    ) -> None:
        """Join the caller-owned sale transaction without committing it."""
        payload = {
            "undo_kind": "restore_investment_disposal",
            "holding": holding,
            "after_holding": after_holding,
            "event_key": event_key,
            "event_value": event_value,
            "snapshot": self.capture_transactions(
                [transaction_id] if transaction_id else [], include_attachments=True,
            ),
        }
        self._validated_payload(payload, 0)
        self._insert_journal("investment_disposal", "Fechar posição · " + str(holding["name"]), payload)

    def _restore_investment_disposal(self, payload: dict[str, Any]) -> None:
        """Undo cash, position and immutable receipt together, refusing later edits."""
        connection = self.repo.db.conn
        event = connection.execute("SELECT value FROM settings WHERE key=?", (payload["event_key"],)).fetchone()
        current = self.repo.holding_by_id(payload["holding"]["id"])
        current = dict(current) if current is not None else None
        if event is None or event[0] != payload["event_value"] or current != payload["after_holding"]:
            raise RuntimeError("A venda ou o investimento mudou; não é seguro desfazer.")
        stable_id = str(payload["holding"].get("stable_id") or "")
        if stable_id and connection.execute(
            "SELECT id FROM holdings WHERE stable_id=? AND id<>?",
            (stable_id, payload["holding"]["id"]),
        ).fetchone() is not None:
            raise RuntimeError("A identidade do investimento já está em uso; não é seguro desfazer.")
        snapshot = payload["snapshot"]
        ids = snapshot["transaction_ids"]
        current_snapshot = self._validated_snapshot(self.capture_transactions(ids, include_attachments=True))
        if current_snapshot != snapshot:
            raise RuntimeError("O movimento da venda mudou; não é seguro desfazer.")
        if ids:
            self._delete_db_transactions(connection, ids)
        if current is not None:
            connection.execute("DELETE FROM holdings WHERE id=?", (payload["holding"]["id"],))
        self._restore_deleted_holding(payload["holding"])
        connection.execute("DELETE FROM settings WHERE key=?", (payload["event_key"],))
        self.repo._capture_planning_state(include_investments=True, commit=False)

    @classmethod
    def _insert_rows(cls, connection, table: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if not row:
                continue
            columns = cls._validated_row_columns(connection, table, row)
            placeholders = ",".join("?" for _ in columns)
            # BUG-PERS-003: snapshot rows carry their original primary keys.
            # If a table's AUTOINCREMENT sequence was reset (legacy migration
            # or backup restore) a new row may already occupy that id, and a
            # plain INSERT would abort the whole undo with IntegrityError.
            # INSERT OR REPLACE restores the snapshotted state idempotently:
            # the colliding placeholder row is overwritten by the original
            # row, keeping referential integrity (child rows keep pointing at
            # the restored parent ids).
            connection.execute(
                f"INSERT OR REPLACE INTO {cls._quote_identifier(table)}("
                + ",".join(cls._quote_identifier(column) for column in columns)
                + f") VALUES({placeholders})",
                tuple(row[column] for column in columns),
            )

    @classmethod
    def _update_transaction_row(cls, connection, row: dict[str, Any]) -> None:
        cls._validated_row_columns(connection, "transactions", row)
        if "id" not in row:
            raise RuntimeError("O histórico não identifica o movimento a repor.")
        transaction_id = int(row["id"])
        exists = connection.execute(
            "SELECT 1 FROM transactions WHERE id=?",
            (transaction_id,),
        ).fetchone()
        if exists is None:
            cls._insert_rows(connection, "transactions", [row])
            return
        columns = [column for column in row if column != "id"]
        connection.execute(
            "UPDATE transactions SET "
            + ",".join(f"{cls._quote_identifier(column)}=?" for column in columns)
            + " WHERE id=?",
            tuple(row[column] for column in columns) + (transaction_id,),
        )

    def _restore_existing_snapshot(self, snapshot: dict[str, Any]) -> None:
        connection = self.repo.db.conn
        ids = self._normalise_ids(snapshot.get("transaction_ids", []))
        for row in snapshot.get("transactions", []):
            # BUG-R6-JRN-01: compensate goals.saved_amount when reverting (mirroring database/repo/transactions.py:1416-1437)
            tx_id = row.get("id")
            if tx_id is not None:
                current = connection.execute(
                    "SELECT amount, goal_id FROM transactions WHERE id=?",
                    (int(tx_id),),
                ).fetchone()
                if current is not None:
                    current_goal_id = current["goal_id"]
                    restored_goal_id = row["goal_id"] if "goal_id" in row else current_goal_id
                    current_amount = float(current["amount"] or 0)
                    restored_amount = (
                        float(row["amount"])
                        if "amount" in row and row["amount"] is not None
                        else current_amount
                    )
                    if current_goal_id is not None or restored_goal_id is not None:
                        try:
                            if (
                                current_goal_id is not None
                                and current_goal_id == restored_goal_id
                                and abs(restored_amount - current_amount) > 1e-6
                            ):
                                delta_saved = current_amount - restored_amount
                                connection.execute(
                                    "UPDATE goals SET saved_amount = MAX(0.0, saved_amount + ?) WHERE id = ?",
                                    (delta_saved, int(current_goal_id)),
                                )
                            elif current_goal_id != restored_goal_id:
                                if current_goal_id is not None:
                                    connection.execute(
                                        "UPDATE goals SET saved_amount = MAX(0.0, saved_amount + ?) WHERE id = ?",
                                        (current_amount, int(current_goal_id)),
                                    )
                                if restored_goal_id is not None:
                                    connection.execute(
                                        "UPDATE goals SET saved_amount = MAX(0.0, saved_amount - ?) WHERE id = ?",
                                        (restored_amount, int(restored_goal_id)),
                                    )
                        except Exception as exc:
                            if "no such table" not in str(exc).casefold():
                                raise
            self._update_transaction_row(connection, row)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        connection.execute(
            f"DELETE FROM transaction_splits WHERE transaction_id IN ({placeholders})",
            tuple(ids),
        )
        connection.execute(
            f"DELETE FROM transaction_meta WHERE transaction_id IN ({placeholders})",
            tuple(ids),
        )
        self._insert_rows(
            connection,
            "transaction_meta",
            list(snapshot.get("transaction_meta", [])),
        )
        self._insert_rows(
            connection,
            "transaction_splits",
            list(snapshot.get("transaction_splits", [])),
        )

    def _restore_deleted_snapshot(self, snapshot: dict[str, Any]) -> None:
        connection = self.repo.db.conn
        ids = self._normalise_ids(snapshot.get("transaction_ids", []))
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conflict = connection.execute(
                f"SELECT id FROM transactions WHERE id IN ({placeholders}) LIMIT 1",
                tuple(ids),
            ).fetchone()
            if conflict is not None:
                raise RuntimeError(
                    "Não é seguro repor esta eliminação porque um identificador já está em uso."
                )

        # BUG-R6-JRN-03: check ID collisions for transaction_splits and transaction_attachments before inserting
        split_ids = self._normalise_ids(
            row.get("id")
            for row in snapshot.get("transaction_splits", [])
            if row.get("id") is not None
        )
        if split_ids:
            for chunk in chunked_iterable(split_ids, chunk_size=400):
                placeholders = ",".join("?" for _ in chunk)
                conflict = connection.execute(
                    f"SELECT id FROM transaction_splits WHERE id IN ({placeholders}) LIMIT 1",
                    tuple(chunk),
                ).fetchone()
                if conflict is not None:
                    raise RuntimeError(
                        "Não é seguro repor esta eliminação porque uma partilha com o mesmo identificador já está em uso."
                    )

        attachment_ids = self._normalise_ids(
            row.get("id")
            for row in snapshot.get("transaction_attachments", [])
            if row.get("id") is not None
        )
        if attachment_ids:
            for chunk in chunked_iterable(attachment_ids, chunk_size=400):
                placeholders = ",".join("?" for _ in chunk)
                conflict = connection.execute(
                    f"SELECT id FROM transaction_attachments WHERE id IN ({placeholders}) LIMIT 1",
                    tuple(chunk),
                ).fetchone()
                if conflict is not None:
                    raise RuntimeError(
                        "Não é seguro repor esta eliminação porque um anexo com o mesmo identificador já está em uso."
                    )

        for table in (
            "transactions",
            "transaction_meta",
            "transaction_splits",
            "transaction_attachments",
        ):
            self._insert_rows(connection, table, list(snapshot.get(table, [])))
        for row in snapshot.get("transactions", []):
            gid = row.get("goal_id")
            if gid is not None:
                amt = float(row.get("amount") or 0)
                try:
                    connection.execute(
                        "UPDATE goals SET saved_amount = MAX(0.0, saved_amount - ?) WHERE id = ?",
                        (amt, int(gid)),
                    )
                except Exception as exc:
                    if "no such table" not in str(exc).casefold():
                        raise
        for table in (
            "subscription_occurrences",
            "scheduled_payment_occurrences",
        ):
            for row in snapshot.get(table, []):
                # BUG-R6-JRN-02: if UPDATE has rowcount == 0 (row was deleted), re-insert the row
                try:
                    cursor = connection.execute(
                        f"UPDATE {table} SET transaction_id=? WHERE id=?",
                        (row.get("transaction_id"), row.get("id")),
                    )
                    if cursor.rowcount == 0:
                        self._insert_rows(connection, table, [row])
                except Exception as exc:
                    if "no such table" not in str(exc).casefold():
                        raise

    def _restore_deleted_holding(self, holding: dict[str, Any]) -> None:
        connection = self.repo.db.conn
        hid = holding.get("id")
        if hid is not None:
            conflict = connection.execute(
                "SELECT id FROM holdings WHERE id=?", (int(hid),)
            ).fetchone()
            if conflict is not None:
                raise RuntimeError(
                    "Não é seguro repor este investimento porque o identificador já está em uso."
                )
        stable_id = str(holding.get("stable_id") or "")
        if stable_id and connection.execute(
            "SELECT id FROM holdings WHERE stable_id=?", (stable_id,),
        ).fetchone() is not None:
            raise RuntimeError(
                "Não é seguro repor este investimento porque a identidade já está em uso."
            )
        self._insert_rows(connection, "holdings", [holding])

    def latest_available(self) -> Optional[dict[str, Any]]:
        row = self.repo.db.conn.execute(
            """
            SELECT id,created_at,action_type,label,status
            FROM change_journal
            WHERE status='available'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def history(self, limit: int = 40) -> list[dict[str, Any]]:
        rows = self.repo.db.conn.execute(
            """
            SELECT id,created_at,action_type,label,status,undone_at
            FROM change_journal
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(250, int(limit))),),
        ).fetchall()
        return [dict(row) for row in rows]

    def undo(self, entry_id: Optional[int] = None) -> Optional[dict[str, Any]]:
        """Undo a change journal entry (delegates to undo_latest)."""
        return self.undo_latest()

    def undo_latest(self) -> Optional[dict[str, Any]]:
        row = self.repo.db.conn.execute(
            """
            SELECT * FROM change_journal
            WHERE status='available'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        entry = dict(row)
        entry_id = int(entry["id"])
        try:
            payload_raw = entry.get("payload_json")
            if isinstance(payload_raw, (bytes, bytearray, memoryview)):
                payload_str = decompress_payload(payload_raw)
            else:
                payload_str = decompress_payload(str(payload_raw or "{}"))
            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError as exc:
                raise RuntimeError("O registo de alterações está danificado.") from exc
            payload = self._validated_payload(payload, entry_id)
            kind = str(payload.get("undo_kind") or "")
            file_records = list(payload.get("files") or [])
            moved_back: list[dict[str, str]] = []

            if kind == "restore_deleted":
                moved_back = self._restore_files(file_records, entry_id)

            connection = self.repo.db.conn
            connection.commit()
            connection.execute("SAVEPOINT undo_change")
            try:
                if kind == "delete_created":
                    ids = self._normalise_ids(payload.get("transaction_ids", []))
                    current = self.capture_transactions(ids, include_attachments=True)
                    if len(current.get("transactions", [])) != len(ids):
                        raise RuntimeError(
                            "Não é seguro desfazer: um movimento criado pela operação "
                            "já não existe. Atualiza o histórico e confirma os dados."
                        )
                    current_files = self._file_records(entry_id, current)
                    payload["files"] = current_files
                    self._delete_db_transactions(
                        connection,
                        ids,
                        delete_linked_occurrences=bool(
                            payload.get("delete_linked_occurrences", False)
                        ),
                    )
                    self._restore_related_state(
                        dict(payload.get("restore_state") or {})
                    )
                elif kind == "restore_existing":
                    self._restore_existing_snapshot(dict(payload.get("snapshot") or {}))
                elif kind == "restore_deleted":
                    self._restore_deleted_snapshot(dict(payload.get("snapshot") or {}))
                elif kind == "restore_deleted_holding":
                    self._restore_deleted_holding(dict(payload.get("holding") or {}))
                elif kind == "restore_investment_disposal":
                    self._restore_investment_disposal(payload)
                else:
                    raise RuntimeError("Este tipo de alteração não pode ser desfeito.")
                raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                compressed = compress_payload(raw_json)
                connection.execute(
                    """
                    UPDATE change_journal
                    SET status='undone', undone_at=CURRENT_TIMESTAMP, payload_json=?
                    WHERE id=?
                    """,
                    (
                        compressed if compressed.startswith((b"ZSTD:", b"ZLIB:")) else raw_json,
                        entry_id,
                    ),
                )
                connection.execute("RELEASE SAVEPOINT undo_change")
                connection.commit()
            except Exception:
                connection.execute("ROLLBACK TO SAVEPOINT undo_change")
                connection.execute("RELEASE SAVEPOINT undo_change")
                connection.rollback()
                if moved_back:
                    uncomp = self._restash_files(moved_back, entry_id)
                    if uncomp:
                        raise AttachmentCompensationError(
                            "Falha na base de dados durante o undo e a reversão dos ficheiros falhou parcialmente.",
                            uncompensated=uncomp,
                        )
                raise

            if kind == "delete_created":
                # BUG-AUD-C119: two-phase stash.  The DB commit already
                # succeeded; stage attachments to a pending location first and
                # only publish them into the stash after every move succeeded.
                # If staging fails the exception propagates before the entry
                # is marked undone, so the LIFO top stays intact (C118) and
                # the physical files were restored to their original paths.
                records = list(payload.get("files") or [])
                pending, _restored = self._preflight_stash_files(records, entry_id)
                self._publish_stash_files(pending, entry_id)
            entry["status"] = "undone"
            return entry
        except AttachmentRestoreTransientError as exc:
            # R3 + BUG-AUD-C118: transient filesystem failure was fully
            # compensated to the original state; no database transaction was
            # executed.  BUG-AUD-C119: the stash phase runs *after* the DB
            # commit, so a failure here must revert the already-committed
            # status flip and keep the entry 'available' (LIFO top intact).
            try:
                self.repo.db.conn.execute(
                    "UPDATE change_journal SET status='available' "
                    "WHERE id=? AND status='undone'",
                    (entry_id,),
                )
                self.repo.db.conn.commit()
            except Exception as revert_exc:  # pragma: no cover - defensive
                log_exception(
                    f"Failed to revert journal status after stash failure for entry #{entry_id}",
                    revert_exc,
                )
            log_exception(
                f"Transient attachment restore failure for journal entry #{entry_id}; status remains retryable",
                exc,
            )
            raise
        except Exception as exc:
            # BUG-AUD-C118: never mark the entry 'failed' on undo.  Marking it
            # would advance the LIFO pointer past a step that was not undone,
            # breaking referential integrity.  The entry stays 'available' and
            # blocks subsequent undos until the failure is resolved.
            log_exception(f"Undo operation failed for journal entry #{entry_id}", exc)
            raise

    def prune(self, keep: int = 100) -> None:
        keep_limit = max(10, int(keep))
        # Preserve the newest entries regardless of status and every available
        # undo. Materialize only obsolete IDs, not the full retained history.
        rows = self.repo.db.conn.execute(
            """
            SELECT id FROM change_journal
            WHERE id < (
                SELECT id FROM change_journal ORDER BY id DESC LIMIT 1 OFFSET ?
            ) AND LOWER(COALESCE(status,'')) <> 'available'
            ORDER BY id DESC
            """,
            (keep_limit - 1,),
        ).fetchall()
        obsolete = [int(row["id"]) for row in rows]
        if not obsolete:
            return
        owns_tx = not self.repo.db.conn.in_transaction
        for chunk in chunked_iterable(obsolete, chunk_size=400):
            placeholders = ",".join("?" for _ in chunk)
            self.repo.db.conn.execute(
                f"DELETE FROM change_journal WHERE id IN ({placeholders})",
                tuple(chunk),
            )
        if owns_tx:
            self.repo.db.conn.commit()
            for journal_id in obsolete:
                folder = self.journal_storage / str(journal_id)
                if folder.exists():
                    try:
                        shutil.rmtree(folder)
                    except OSError as exc:
                        log_warning(
                            "Failed to prune undo attachment folder %s: %s",
                            folder,
                            exc,
                        )

    def compact_snapshots(self) -> dict[str, Any]:
        """Compact change journal records: prune obsolete rows, purge orphaned stash files and recompress large payloads."""
        owns_tx = not self.repo.db.conn.in_transaction
        self.prune(keep=50)
        active_rows = self.repo.db.conn.execute(
            "SELECT id, payload_json FROM change_journal"
        ).fetchall()
        recompressed_count = 0
        for row in active_rows:
            entry_id = int(row["id"])
            raw_payload = row["payload_json"]
            if isinstance(raw_payload, str) and len(raw_payload.encode("utf-8")) >= 1024:
                compressed = compress_payload(raw_payload)
                if compressed.startswith((b"ZSTD:", b"ZLIB:")):
                    self.repo.db.conn.execute(
                        "UPDATE change_journal SET payload_json=? WHERE id=?",
                        (compressed, entry_id),
                    )
                    recompressed_count += 1
        if recompressed_count and owns_tx:
            self.repo.db.conn.commit()

        active_ids = {int(row["id"]) for row in active_rows}
        purged_folders = 0
        if self.journal_storage.exists():
            # BUG-AUD-C121: only purge folders that are both orphaned and
            # older than the minimum retention window.  A folder whose entry
            # no longer exists may still be owned by an in-flight operation;
            # the age gate prevents compaction from destroying active work.
            now_ts = time.time()
            for item in self.journal_storage.iterdir():
                if item.is_dir() and item.name.isdigit():
                    if int(item.name) in active_ids:
                        continue
                    try:
                        age_seconds = max(0.0, now_ts - item.stat().st_mtime)
                    except OSError:
                        continue
                    if age_seconds < SNAPSHOT_MIN_RETENTION_SECONDS:
                        continue
                    try:
                        shutil.rmtree(item)
                        purged_folders += 1
                    except OSError:
                        pass
        return {
            "active_journal_entries": len(active_ids),
            "recompressed_entries": recompressed_count,
            "purged_orphaned_folders": purged_folders,
        }


__all__ = [
    "ChangeJournalService",
    "compress_payload",
    "decompress_payload",
]
