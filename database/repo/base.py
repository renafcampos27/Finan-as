#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from utils.app_logging import log_exception
from utils.config import db_path, paths_for_database
from utils.startup_progress import StartupProgressCallback
from database.engine import FinanceDB
from services.account_identity import account_identity_key, clean_account_name


class BaseRepo:
    def __init__(
        self,
        db: FinanceDB | None = None,
        *,
        progress_callback: StartupProgressCallback | None = None,
    ) -> None:

        if db is not None:
            self.db = db
            # FinanceDB exposes its real file as ``path``; the legacy
            # ``db_path`` attribute never existed, so injected test
            # databases silently resolved paths against the project
            # app.sqlite3 store.
            database_file = Path(
                getattr(db, "path", getattr(db, "db_path", "app.sqlite3"))
            )
            self.paths = paths_for_database(database_file)
        else:
            database_file = Path(db_path())
            self.paths = paths_for_database(database_file)
            self.db = FinanceDB(
                database_file,
                attachments_directory=self.paths.attachments,
                history_directory=self.paths.history,
                preferences_file=self.paths.backup_preferences,
                protection_directory=self.paths.protections,
                progress_callback=progress_callback,
            )

    def q(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        return self.db.query(sql, params)

    def q_iter(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        return self.db.query_iter(sql, params)

    def q_chunks(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        chunk_size: int = 1000,
    ) -> Any:
        return self.db.query_chunks(sql, params, chunk_size=chunk_size)

    def scalar(
        self, sql: str, params: tuple[Any, ...] = (), default: float = 0.0
    ) -> float:
        return self.db.scalar(sql, params, default)

    def movement_count(self) -> int:
        """Number of stored movements (keeps SQL out of the UI layer)."""

        return int(self.db.scalar("SELECT COUNT(*) FROM transactions", (), 0) or 0)

    def commit(self) -> None:
        self.db.conn.commit()

    def integrity_inventory(self) -> dict[str, Any]:
        return self.db.integrity_inventory()

    def cleanup_orphaned_records(self, *, dry_run: bool = True) -> dict[str, int]:
        return self.db.cleanup_orphaned_records(dry_run=dry_run)

    def database_storage_stats(self) -> dict[str, Any]:
        return self.db.database_storage_stats()

    def optimize_database(self) -> dict[str, Any]:
        return self.db.optimize_database()

    def optimize_storage(self) -> dict[str, Any]:
        return self.db.optimize_storage()

    def incremental_vacuum(self, pages: int = 0) -> int:
        return self.db.incremental_vacuum(pages)

    def enable_incremental_vacuum(self, backup_path: Path) -> dict[str, Any]:
        return self.db.enable_incremental_vacuum(backup_path)

    def close(self) -> None:
        self.db.prepare_for_shutdown()

    def safe_commit(self, context: str = "Database commit failed") -> bool:
        try:
            self.commit()
            return True
        except Exception as exc:
            try:
                self.db.conn.rollback()
            except Exception as rollback_exc:
                log_exception(f"{context}; rollback also failed", rollback_exc)
            log_exception(context, exc)
            return False

    def setting(self, key: str, default: str = "") -> str:
        rows = self.q("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        try:
            self.db.conn.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value)
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def canonical_account_name(
        self,
        name: object,
        *,
        create: bool = True,
    ) -> str:
        """Resolve an account to its stable registry name without committing."""

        display = clean_account_name(name)
        identity = account_identity_key(display)
        try:
            row = self.db.conn.execute(
                "SELECT name FROM financial_accounts WHERE account_key=? LIMIT 1",
                (identity,),
            ).fetchone()
        except sqlite3.OperationalError:
            # Compatibility with a database being inspected before migration 21.
            row = self.db.conn.execute(
                "SELECT name FROM financial_accounts "
                "WHERE LOWER(TRIM(name))=LOWER(TRIM(?)) LIMIT 1",
                (display,),
            ).fetchone()
        if row is not None:
            return str(row["name"] or display)
        if create:
            columns = {
                str(item["name"])
                for item in self.db.conn.execute(
                    "PRAGMA table_info(financial_accounts)"
                ).fetchall()
            }
            if "account_key" in columns:
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO financial_accounts(name,account_key) "
                    "VALUES(?,?)",
                    (display, identity),
                )
            else:
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO financial_accounts(name) VALUES(?)",
                    (display,),
                )
        return display
