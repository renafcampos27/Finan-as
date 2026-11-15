"""Safe, cancellable SQLite readers that can never create a database file."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar


T = TypeVar("T")


def read_only_uri(path: Path) -> str:
    """Return a SQLite URI that refuses both writes and implicit file creation."""

    database_file = Path(path).expanduser().resolve()
    if not database_file.is_file():
        raise FileNotFoundError(f"A base de dados não existe: {database_file}")
    return f"{database_file.as_uri()}?mode=ro"


def open_read_only_connection(
    path: Path,
    *,
    timeout: float = 30.0,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """Open an existing SQLite database as an explicitly read-only connection."""

    connection = sqlite3.connect(
        read_only_uri(path),
        uri=True,
        timeout=max(0.1, float(timeout)),
        check_same_thread=check_same_thread,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA cache_size=-64000")
    connection.execute("PRAGMA mmap_size=268435456")
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


class CancellableReadSnapshot:
    """Own one consistent read transaction and expose SQLite hard cancellation."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._connection: sqlite3.Connection | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        """Interrupt an active SQLite statement, including from another thread."""

        self._cancelled.set()
        with self._lock:
            connection = self._connection
        if connection is not None:
            connection.interrupt()

    def __enter__(self) -> sqlite3.Connection:
        if self.cancelled:
            raise sqlite3.OperationalError("interrupted")
        connection = open_read_only_connection(
            self.path,
            check_same_thread=False,
        )
        with self._lock:
            self._connection = connection
        try:
            connection.set_progress_handler(
                lambda: 1 if self.cancelled else 0,
                1_000,
            )
            if self.cancelled:
                connection.interrupt()
                raise sqlite3.OperationalError("interrupted")
            # All statements executed by this reader now share one WAL snapshot.
            connection.execute("BEGIN")
            return connection
        except BaseException:
            try:
                connection.set_progress_handler(None, 0)
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass
            with self._lock:
                self._connection = None
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
        if connection is None:
            return
        try:
            connection.set_progress_handler(None, 0)
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()


class CancellableReadOperation(Generic[T]):
    """Callable accepted by ``AsyncTask`` with a real SQLite cancel hook."""

    def __init__(self, path: Path, reader: Callable[[sqlite3.Connection], T]) -> None:
        self._snapshot = CancellableReadSnapshot(path)
        self._reader = reader

    def cancel(self) -> None:
        self._snapshot.cancel()

    def __call__(self) -> T:
        with self._snapshot as connection:
            if self._snapshot.cancelled:
                raise sqlite3.OperationalError("interrupted")
            return self._reader(connection)


__all__ = [
    "CancellableReadOperation",
    "CancellableReadSnapshot",
    "open_read_only_connection",
    "read_only_uri",
]
