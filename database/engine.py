from __future__ import annotations

import json
import os
import random
import re
import shutil
import sqlite3
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional


from database.protection import (
    DataIntegrityError,
    DataProtectionError,
    DataProtectionGuard,
)
from database.read_only import open_read_only_connection
from database.validation import (
    sql_finite_number,
    sql_iso_date,
    sql_month_key,
)
from utils.app_logging import log_exception, log_warning
from utils.config import (
    APP_NAME,
    BLUE,
    GREEN,
    GREEN_2,
    paths_for_database,
    today_iso,
)
from utils.version import APP_VERSION
from utils.startup_progress import (
    StartupProgressCallback,
    report_startup_progress,
    scaled_percent,
)


@dataclass(slots=True, frozen=True)
class Category:
    name: str
    kind: str
    colour: str
    budget: float


class _LockedConnectionHandle:
    """Context manager holding the shared-connection gate for one with-block.

    BUG-DB-001 companion: ``FinanceDB._locked_connection`` must release
    ``_SQLITE_SHARED_CONNECTION_LOCK`` when the caller's ``with`` block ends;
    returning the raw connection would keep the gate acquired forever and
    freeze every background worker.
    """

    __slots__ = ("_lock", "_conn")

    def __init__(
        self,
        lock: Any,
        conn: sqlite3.Connection,
    ) -> None:
        self._lock = lock
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        return self._conn

    def __exit__(
        self,
        exc_type: Any,
        exc: Any,
        tb: Any,
    ) -> bool:
        self._lock.release()
        return False


class FinanceDB:
    """Small SQLite wrapper for local personal finance data."""

    CURRENT_SCHEMA_VERSION = 32
    REQUIRED_SCHEMA_VERSIONS = tuple(range(1, CURRENT_SCHEMA_VERSION + 1))

    @classmethod
    def open_read_only(cls, path: Path) -> "FinanceDB":
        """Open an existing database without migrations, writes or file creation."""

        database_file = Path(path).expanduser().resolve()
        instance = cls.__new__(cls)
        instance.path = database_file
        resolved_paths = paths_for_database(database_file)
        instance.attachments_directory = resolved_paths.attachments
        instance.history_directory = resolved_paths.history
        instance.preferences_file = resolved_paths.backup_preferences
        instance.conn = open_read_only_connection(
            database_file,
            check_same_thread=False,
        )
        instance._closed = False
        instance._read_only = True
        instance._startup_progress = None
        return instance

    def __init__(
        self,
        path: Path,
        *,
        app_version: str = APP_VERSION,
        attachments_directory: Optional[Path] = None,
        history_directory: Optional[Path] = None,
        preferences_file: Optional[Path] = None,
        protection_directory: Optional[Path] = None,
        progress_callback: StartupProgressCallback | None = None,
        session_context: Optional[Any] = None,
    ):
        self.path = Path(path)
        self._startup_progress = progress_callback
        report_startup_progress(
            progress_callback,
            19,
            "Abrir os dados financeiros",
            self.path.name,
        )
        resolved_paths = paths_for_database(self.path)
        self.attachments_directory = Path(
            attachments_directory or resolved_paths.attachments
        )
        self.history_directory = Path(history_directory or resolved_paths.history)
        self.preferences_file = Path(
            preferences_file or resolved_paths.backup_preferences
        )
        guard = DataProtectionGuard(
            self.path,
            supported_schema_version=self.CURRENT_SCHEMA_VERSION,
            required_schema_versions=self.REQUIRED_SCHEMA_VERSIONS,
            current_app_version=app_version,
            attachments_directory=self.attachments_directory,
            history_directory=self.history_directory,
            preferences_file=self.preferences_file,
            protection_directory=protection_directory,
            progress_callback=progress_callback,
            session_context=session_context,
            financial_profile=True,
        )
        session = guard.prepare()
        connection: Optional[sqlite3.Connection] = None
        try:
            report_startup_progress(
                progress_callback,
                72,
                "Abrir a base protegida",
                "A criar uma ligação SQLite isolada.",
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.path),
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            self.conn = connection
            self._closed = False
            self._read_only = False
            report_startup_progress(
                progress_callback,
                73,
                "Preparar a estrutura",
                "A confirmar tabelas e índices essenciais.",
            )
            self.init_schema()
            self._ensure_migrations(progress_callback=progress_callback)
            report_startup_progress(
                progress_callback,
                78,
                "Preparar análises",
                "A confirmar o histórico e os indicadores.",
            )
            self.ensure_analytics_tables()
            report_startup_progress(
                progress_callback,
                79,
                "Confirmar dados base",
                "A preservar categorias e configurações existentes.",
            )
            self.seed_if_empty()
            # Reference categories are inserted after structural migrations.
            # Give them their durable IDs before any optional demo movement is
            # created; otherwise several blank IDs are indistinguishable to
            # SQLite triggers and a display name can be resolved incorrectly.
            if self._stable_financial_identity_needs_repair():
                self._ensure_stable_financial_entity_identity()
            if self._demo_data_requested():
                self.seed_demo_data()
            report_startup_progress(
                progress_callback,
                80,
                "Atualizar referências",
                "A completar apenas os dados auxiliares em falta.",
            )
            self.ensure_reference_data()
            # Reference rows may be inserted after migration 24 on a brand-new
            # profile.  Run the complete repair only when a missing link or
            # trigger is detected; established profiles avoid a full scan of
            # every movement on each startup.
            if self._stable_financial_identity_needs_repair():
                self._ensure_stable_financial_entity_identity()
            self.conn.commit()
            actual_schema = self.schema_version()
            session.verify_after_upgrade(self.conn, actual_schema)
            session.complete(self.conn, actual_schema)
            report_startup_progress(
                progress_callback,
                92,
                "Base de dados pronta",
                "Integridade e preservação confirmadas.",
            )
        except Exception as exc:
            if connection is not None:
                try:
                    connection.close()
                except Exception as close_error:
                    log_exception(
                        "Failed to close database during protected rollback",
                        close_error,
                    )
            try:
                session.rollback()
            except Exception as rollback_error:
                recovery_hint = (
                    f"usa a proteção guardada em {session.backup_path}."
                    if session.backup_path is not None
                    else "a base nova e incompleta não pôde ser removida."
                )
                raise DataProtectionError(
                    "A atualização falhou e a reposição automática também não "
                    "foi concluída. Não voltes a abrir esta cópia da aplicação; "
                    f"{recovery_hint}"
                ) from rollback_error
            if isinstance(exc, DataProtectionError):
                raise
            raise DataProtectionError(
                "A preparação desta versão falhou. O arranque foi cancelado e "
                "o estado anterior foi reposto sem alterar os teus dados. "
                f"Detalhe técnico: {exc}"
            ) from exc

    def init_schema(self) -> None:
        cur = self.conn.cursor()
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
            self.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        cur.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA cache_size=-64000;
            PRAGMA temp_store=MEMORY;
            PRAGMA mmap_size=268435456;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=30000;

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS categories (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                kind TEXT NOT NULL,
                colour TEXT NOT NULL,
                budget REAL NOT NULL DEFAULT 0,
                budget_bucket TEXT NOT NULL DEFAULT '',
                stable_id TEXT NOT NULL DEFAULT '',
                identity_key TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                merged_into_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT 'Conta principal',
                kind TEXT NOT NULL DEFAULT 'expense',
                notes TEXT NOT NULL DEFAULT '',
                transfer_group_id TEXT NOT NULL DEFAULT '',
                category_id TEXT NOT NULL DEFAULT '',
                account_id TEXT NOT NULL DEFAULT '',
                goal_id INTEGER,
                goal_name_snapshot TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE SET NULL
            );
            DROP TRIGGER IF EXISTS trg_transactions_cascade_transfer_leg;
            CREATE TRIGGER IF NOT EXISTS trg_transactions_cascade_transfer_leg
            AFTER DELETE ON transactions
            FOR EACH ROW
            WHEN OLD.transfer_group_id IS NOT NULL AND OLD.transfer_group_id <> ''
            BEGIN
                -- BUG-DB-003: cascading transfer-leg deletes must keep
                -- goals.saved_amount synchronised.  Sign convention matches
                -- adjust_goal_with_transaction/_delete_transactions:
                -- ``saved_amount + amount`` (deposit leg is negative),
                -- floored at zero.  (1) compensates the explicitly deleted
                -- leg itself; (2) compensates goal-linked sibling legs that
                -- statement (3) is about to cascade-delete -- they can never
                -- fire triggers themselves (recursive_triggers=OFF).
                UPDATE goals
                SET saved_amount = MAX(0.0, saved_amount + OLD.amount)
                WHERE OLD.goal_id IS NOT NULL AND id = OLD.goal_id;
                UPDATE goals
                SET saved_amount = MAX(0.0, saved_amount + COALESCE((
                        SELECT SUM(t.amount) FROM transactions t
                        WHERE t.transfer_group_id = OLD.transfer_group_id
                          AND t.id <> OLD.id AND t.goal_id = goals.id
                    ), 0.0))
                WHERE id IN (SELECT goal_id FROM transactions
                             WHERE transfer_group_id = OLD.transfer_group_id
                               AND id <> OLD.id AND goal_id IS NOT NULL);
                DELETE FROM transaction_splits
                WHERE transaction_id IN (
                    SELECT id FROM transactions
                    WHERE transfer_group_id = OLD.transfer_group_id AND id <> OLD.id
                );
                DELETE FROM transaction_attachments
                WHERE transaction_id IN (
                    SELECT id FROM transactions
                    WHERE transfer_group_id = OLD.transfer_group_id AND id <> OLD.id
                );
                DELETE FROM transaction_meta
                WHERE transaction_id IN (
                    SELECT id FROM transactions
                    WHERE transfer_group_id = OLD.transfer_group_id AND id <> OLD.id
                );
                DELETE FROM transactions
                WHERE transfer_group_id = OLD.transfer_group_id
                  AND id <> OLD.id;
            END;
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                renewal_day INTEGER NOT NULL DEFAULT 1,
                period TEXT NOT NULL DEFAULT 'Mensal',
                notes TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                last_used_date TEXT NOT NULL DEFAULT '',
                next_renewal_date TEXT NOT NULL DEFAULT '',
                aliases TEXT NOT NULL DEFAULT '',
                account TEXT NOT NULL DEFAULT 'Conta principal',
                payment_method TEXT NOT NULL DEFAULT '',
                category_id TEXT NOT NULL DEFAULT '',
                account_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS subscription_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                transaction_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(subscription_id, due_date),
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT 'Manual',
                value REAL NOT NULL,
                cost_basis REAL NOT NULL DEFAULT 0,
                cost_basis_known INTEGER NOT NULL DEFAULT 0,
                fees REAL NOT NULL DEFAULT 0,
                linked_account TEXT NOT NULL DEFAULT '',
                stable_id TEXT NOT NULL DEFAULT '',
                linked_account_id TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT 'EUR',
                exchange_rate REAL NOT NULL DEFAULT 1.0
            );
            CREATE TABLE IF NOT EXISTS financial_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                reference_balance REAL NOT NULL DEFAULT 0,
                reference_date TEXT NOT NULL DEFAULT '',
                balance_known INTEGER NOT NULL DEFAULT 0,
                balance_source TEXT NOT NULL DEFAULT '',
                account_key TEXT NOT NULL DEFAULT '',
                stable_id TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                merged_into_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_amount REAL NOT NULL,
                saved_amount REAL NOT NULL DEFAULT 0,
                monthly_contribution REAL NOT NULL DEFAULT 0,
                deadline TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                goal_type TEXT NOT NULL DEFAULT 'standard'
            );
            CREATE TABLE IF NOT EXISTS transaction_splits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                category_id TEXT NOT NULL DEFAULT '',
                percent REAL NOT NULL,
                amount REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS category_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                category_id TEXT NOT NULL DEFAULT '',
                split_json TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS planned_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_amount REAL NOT NULL,
                saved_amount REAL NOT NULL DEFAULT 0,
                desired_date TEXT NOT NULL DEFAULT '',
                priority TEXT NOT NULL DEFAULT 'Média',
                notes TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS monthly_snapshots (
                month TEXT PRIMARY KEY,
                income REAL NOT NULL DEFAULT 0,
                expenses REAL NOT NULL DEFAULT 0,
                investments REAL NOT NULL DEFAULT 0,
                savings REAL NOT NULL DEFAULT 0,
                savings_rate REAL NOT NULL DEFAULT 0,
                cash_balance REAL NOT NULL DEFAULT 0,
                net_worth REAL NOT NULL DEFAULT 0,
                goals_json TEXT NOT NULL DEFAULT '[]',
                holdings_json TEXT NOT NULL DEFAULT '[]',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS financial_state_snapshots (
                day TEXT PRIMARY KEY,
                holdings_json TEXT NOT NULL DEFAULT '[]',
                liabilities_json TEXT NOT NULL DEFAULT '[]',
                subscriptions_json TEXT NOT NULL DEFAULT '[]',
                goals_json TEXT NOT NULL DEFAULT '[]',
                cash_balance REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category);
            CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account);
            CREATE INDEX IF NOT EXISTS idx_transactions_account_date ON transactions(account, date);
            CREATE INDEX IF NOT EXISTS idx_transactions_category_date ON transactions(category, date);
            CREATE INDEX IF NOT EXISTS idx_holdings_asset_class_name ON holdings(asset_class, name);
            """)

        self.conn.commit()

    def _ensure_migrations(
        self,
        *,
        progress_callback: StartupProgressCallback | None = None,
    ) -> None:
        """Apply pending schema changes and version records as one atomic unit.

        A nested savepoint preserves caller-owned transactions; an outermost
        savepoint commits only after the migrations and deferred constraints
        succeed. Schema helpers must never commit this transaction themselves.
        """
        savepoint = f"migrations_{uuid.uuid4().hex}"
        previous_dirty = bool(getattr(self, "_validation_triggers_dirty", False))
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            self._apply_pending_migrations(progress_callback=progress_callback)
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            self._validation_triggers_dirty = previous_dirty
            raise

    def _migration_steps(self) -> list[tuple[int, str, Any]]:
        """Source-owned migration registry, shared by execution and dry-run planning."""
        return [
            (1, "baseline_schema", self._migration_baseline),
            (2, "transaction_subcategories", self._migration_subcategories),
            (3, "liabilities", self._migration_liabilities),
            (4, "document_storage", self._migration_document_storage),
            (5, "analytics_schema", self._migration_analytics_schema),
            (6, "monthly_category_budgets", self._migration_monthly_category_budgets),
            (7, "subscription_recurrence", self._migration_subscription_recurrence),
            (8, "financial_state_history", self._migration_financial_state_history),
            (9, "category_budget_buckets", self._migration_category_budget_buckets),
            (
                10,
                "high_volume_query_indexes",
                self._migration_high_volume_query_indexes,
            ),
            (11, "habit_change_plans", self._migration_habit_change_plans),
            (12, "change_journal", self._migration_change_journal),
            (13, "structured_goal_types", self._migration_structured_goal_types),
            (14, "holding_fees", self._migration_holding_fees),
            (
                15,
                "scheduled_occurrence_foreign_keys",
                self._migration_scheduled_occurrence_foreign_keys,
            ),
            (16, "database_value_constraints", self._migration_value_constraints),
            (17, "attachment_integrity_metadata", self._migration_attachment_integrity),
            (
                18,
                "import_traceability_and_subscription_aliases",
                self._migration_import_traceability_and_subscription_aliases,
            ),
            (
                19,
                "subscription_payment_routing",
                self._migration_subscription_payment_routing,
            ),
            (
                20,
                "financial_integrity_links",
                self._migration_financial_integrity_links,
            ),
            (
                21,
                "canonical_account_identity",
                self._migration_canonical_account_identity,
            ),
            (
                22,
                "literal_transaction_search",
                self._migration_literal_transaction_search,
            ),
            (
                23,
                "legacy_transfer_startup_compatibility",
                self._migration_legacy_transfer_startup_compatibility,
            ),
            (
                24,
                "stable_financial_entity_identity",
                self._migration_stable_financial_entity_identity,
            ),
            (
                25,
                "remove_obsolete_transactions_fts",
                self._migration_remove_obsolete_transactions_fts,
            ),
            (
                26,
                "goal_assets_and_portable_attachments",
                self._migration_goal_assets_and_portable_attachments,
            ),
            (
                27,
                "holding_currency_and_exchange_rate",
                self._migration_holding_currency_and_exchange_rate,
            ),
            (
                28,
                "transactions_cascade_transfer_leg_trigger",
                self._migration_transactions_cascade_transfer_leg_trigger,
            ),
            (
                29,
                "holding_acquisition_metadata",
                self._migration_holding_acquisition_metadata,
            ),
            (
                30,
                "monthly_category_budgets_update_cascade",
                self._migration_monthly_category_budgets_update_cascade,
            ),
            (31, "subscription_price_reviews", self._migration_subscription_price_reviews),
            (32, "reimbursement_evidence", self._migration_reimbursement_evidence),
        ]

    def _migration_subscription_price_reviews(self) -> None:
        from database.subscription_radar_schema import install_subscription_radar_schema

        install_subscription_radar_schema(self)

    def _migration_reimbursement_evidence(self) -> None:
        from database.reimbursement_schema import create_reimbursement_schema

        create_reimbursement_schema(self)

    def _apply_pending_migrations(
        self,
        *,
        progress_callback: StartupProgressCallback | None = None,
    ) -> None:
        """Apply migrations inside the transaction owned by _ensure_migrations."""
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        migration_cols = {
            str(row["name"])
            for row in self.query("PRAGMA table_info(schema_migrations)")
        }
        if "name" not in migration_cols:
            self.conn.execute(
                "ALTER TABLE schema_migrations ADD COLUMN name TEXT NOT NULL DEFAULT ''"
            )
        applied = {
            int(row["version"])
            for row in self.query("SELECT version FROM schema_migrations")
        }

        migrations = self._migration_steps()

        pending = [
            (version, name, migration)
            for version, name, migration in migrations
            if version not in applied
        ]
        if not pending:
            report_startup_progress(
                progress_callback,
                77,
                "Estrutura atualizada",
                "Não existem migrações pendentes.",
            )
            return
        total = len(pending)
        previous_defer = bool(getattr(self, "_defer_validation_trigger_rebuild", False))
        self._defer_validation_trigger_rebuild = True
        try:
            # The migration ledger records structural capabilities, so old
            # schema-30 profiles may still carry earlier search bodies. The
            # read-only guard has already checked their exact declarations.
            # Refresh only this derived index inside the owned transaction,
            # before any later migration or reference-data write can fire it.
            if 30 in applied:
                from database.schema_contract import normalized_sql, trusted_schema_contracts

                current_contract = trusted_schema_contracts()[self.CURRENT_SCHEMA_VERSION]
                search_names = (
                    "trg_transaction_search_insert",
                    "trg_transaction_search_update",
                    "trg_transaction_meta_search_insert",
                    "trg_transaction_meta_search_update",
                )
                if any(
                    (row := self.conn.execute(
                        "SELECT sql FROM sqlite_master WHERE name=?", (trigger,)
                    ).fetchone()) is not None
                    and normalized_sql(row[0]) != current_contract[trigger]["sql"]
                    for trigger in search_names
                ):
                    self._migration_literal_transaction_search()
            for index, (version, name, migration) in enumerate(pending, start=1):
                report_startup_progress(
                    progress_callback,
                    scaled_percent(74, 77, index - 1, total),
                    "Atualizar a estrutura",
                    name.replace("_", " "),
                    current=index,
                    total=total,
                )
                migration()
                self.conn.execute(
                    "INSERT INTO schema_migrations(version,name) VALUES(?,?)",
                    (version, name),
                )
                observer = getattr(self, "_schema_contract_observer", None)
                if observer is not None:
                    observer(version)
                report_startup_progress(
                    progress_callback,
                    scaled_percent(74, 77, index, total),
                    "Atualizar a estrutura",
                    name.replace("_", " "),
                    current=index,
                    total=total,
                )
        finally:
            self._defer_validation_trigger_rebuild = previous_defer
        if not previous_defer and bool(
            getattr(self, "_validation_triggers_dirty", False) or 30 in applied
        ):
            self._validation_triggers_dirty = False
            self._migration_value_constraints()
        self._migration_high_throughput_covering_indexes()
        # Earlier additive releases could mark their structural migration
        # before installing every later source-owned derived guard/index.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transaction_splits_category_tx "
            "ON transaction_splits(category, transaction_id)"
        )

    def schema_version(self) -> int:
        try:
            return int(
                self.scalar("SELECT MAX(version) FROM schema_migrations", default=0)
            )
        except sqlite3.OperationalError:
            return 0

    def schema_versions(self) -> set[int]:
        """Return every applied migration, preserving gaps for integrity checks."""

        try:
            return {
                int(row["version"])
                for row in self.query("SELECT version FROM schema_migrations")
            }
        except sqlite3.OperationalError:
            return set()

    def _migration_baseline(self) -> None:
        """Version marker for databases created before migration tracking."""

    def _migration_subcategories(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS subcategories(category TEXT, name TEXT, PRIMARY KEY(category, name))"
        )
        try:
            self.conn.execute(
                "ALTER TABLE transactions ADD COLUMN subcategory TEXT DEFAULT ''"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).casefold():
                raise

    def _migration_liabilities(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS liabilities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                liability_type TEXT NOT NULL DEFAULT 'Outro',
                account TEXT NOT NULL DEFAULT '',
                account_id TEXT NOT NULL DEFAULT '',
                balance REAL NOT NULL DEFAULT 0,
                interest_rate REAL NOT NULL DEFAULT 0,
                monthly_payment REAL NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    def _migration_document_storage(self) -> None:
        self.ensure_document_tables()

    def _migration_analytics_schema(self) -> None:
        self.ensure_analytics_tables()

    def _migration_monthly_category_budgets(self) -> None:
        self._ensure_monthly_category_budget_table()

    def _migration_subscription_recurrence(self) -> None:
        try:
            self.conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN next_renewal_date TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).casefold():
                raise
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                transaction_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(subscription_id, due_date),
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscription_occurrences_due "
            "ON subscription_occurrences(due_date)"
        )

    def _migration_financial_state_history(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS financial_state_snapshots (
                day TEXT PRIMARY KEY,
                holdings_json TEXT NOT NULL DEFAULT '[]',
                liabilities_json TEXT NOT NULL DEFAULT '[]',
                subscriptions_json TEXT NOT NULL DEFAULT '[]',
                cash_balance REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    def _migration_category_budget_buckets(self) -> None:
        columns = {str(row["name"]) for row in self.query("PRAGMA table_info(categories)")}
        if "budget_bucket" in columns:
            # Existing blanks are user evidence, not permission to reclassify.
            return
        try:
            self.conn.execute(
                "ALTER TABLE categories ADD COLUMN budget_bucket TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).casefold():
                raise
        needs = (
            "Casa e contas",
            "Supermercado",
            "Saúde",
            "Transporte",
            "Higiene",
            "Educação",
            "Animais",
        )
        savings = ("Investimentos",)
        self.conn.executemany(
            "UPDATE categories SET budget_bucket='Necessidades' "
            "WHERE name=? AND TRIM(budget_bucket)=''",
            [(name,) for name in needs],
        )
        self.conn.executemany(
            "UPDATE categories SET budget_bucket='Poupança' "
            "WHERE name=? AND TRIM(budget_bucket)=''",
            [(name,) for name in savings],
        )
        self.conn.execute(
            "UPDATE categories SET budget_bucket='Desejos' "
            "WHERE kind='expense' AND TRIM(budget_bucket)=''"
        )

    def _migration_structured_goal_types(self) -> None:
        """Classify emergency funds explicitly without interpreting free-form notes."""

        columns = {str(row["name"]) for row in self.query("PRAGMA table_info(goals)")}
        if "goal_type" not in columns:
            self.conn.execute(
                "ALTER TABLE goals ADD COLUMN goal_type "
                "TEXT NOT NULL DEFAULT 'standard'"
            )
        self.conn.execute(
            """
            UPDATE goals
            SET goal_type='emergency_fund'
            WHERE goal_type='standard'
              AND LOWER(TRIM(name)) IN (
                  'fundo de emergência',
                  'fundo de emergencia',
                  'reserva de emergência',
                  'reserva de emergencia',
                  'colchão financeiro',
                  'colchao financeiro',
                  'emergency fund'
              )
            """
        )
        self.conn.execute(
            """
            UPDATE goals
            SET goal_type='standard'
            WHERE goal_type='emergency_fund'
              AND id<>(
                  SELECT id
                  FROM goals
                  WHERE goal_type='emergency_fund'
                  ORDER BY
                      CASE
                          WHEN target_amount>0
                          THEN saved_amount/target_amount
                          ELSE 0
                      END DESC,
                      id DESC
                  LIMIT 1
              )
            """
        )

    def _migration_holding_fees(self) -> None:
        """Add cumulative fees without changing any existing holding value."""

        try:
            self.conn.execute(
                "ALTER TABLE holdings ADD COLUMN fees REAL NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).casefold():
                raise

    def _migration_scheduled_occurrence_foreign_keys(self) -> None:
        """Rebuild scheduled occurrences with the same referential safety as subscriptions."""

        self.ensure_analytics_tables()
        foreign_keys = {
            (
                str(row["from"]),
                str(row["table"]),
                str(row["to"]),
                str(row["on_delete"]).upper(),
            )
            for row in self.query(
                "PRAGMA foreign_key_list(scheduled_payment_occurrences)"
            )
        }
        expected = {
            ("scheduled_id", "scheduled_payments", "id", "CASCADE"),
            ("transaction_id", "transactions", "id", "SET NULL"),
        }
        if expected.issubset(foreign_keys):
            return

        orphan_schedule = self.conn.execute(
            """
            SELECT o.id
            FROM scheduled_payment_occurrences o
            LEFT JOIN scheduled_payments p ON p.id=o.scheduled_id
            WHERE p.id IS NULL
            LIMIT 1
            """
        ).fetchone()
        orphan_transaction = self.conn.execute(
            """
            SELECT o.id
            FROM scheduled_payment_occurrences o
            LEFT JOIN transactions t ON t.id=o.transaction_id
            WHERE o.transaction_id IS NOT NULL AND t.id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if orphan_schedule is not None or orphan_transaction is not None:
            raise sqlite3.IntegrityError(
                "scheduled_payment_occurrences contains orphan references"
            )

        self.conn.execute("SAVEPOINT scheduled_occurrence_fk_migration")
        try:
            self.conn.execute(
                "ALTER TABLE scheduled_payment_occurrences "
                "RENAME TO scheduled_payment_occurrences_legacy"
            )
            self.conn.execute(
                """
                CREATE TABLE scheduled_payment_occurrences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scheduled_id INTEGER NOT NULL,
                    due_date TEXT NOT NULL,
                    transaction_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(scheduled_id, due_date),
                    FOREIGN KEY(scheduled_id) REFERENCES scheduled_payments(id) ON DELETE CASCADE,
                    FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
                )
                """
            )
            self.conn.execute(
                """
                INSERT INTO scheduled_payment_occurrences(
                    id, scheduled_id, due_date, transaction_id, created_at
                )
                SELECT id, scheduled_id, due_date, transaction_id, created_at
                FROM scheduled_payment_occurrences_legacy
                """
            )
            self.conn.execute("DROP TABLE scheduled_payment_occurrences_legacy")
            self.conn.execute(
                "CREATE INDEX idx_scheduled_payment_occurrences_due_id "
                "ON scheduled_payment_occurrences(due_date, id)"
            )
            self.conn.execute("RELEASE SAVEPOINT scheduled_occurrence_fk_migration")
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT scheduled_occurrence_fk_migration")
            self.conn.execute("RELEASE SAVEPOINT scheduled_occurrence_fk_migration")
            raise

    def _install_validation_triggers(
        self,
        table: str,
        checks: list[tuple[str, str]],
    ) -> None:
        """Install matching INSERT/UPDATE guards without rewriting legacy rows."""

        for operation in ("INSERT", "UPDATE"):
            suffix = operation.casefold()
            trigger = f"validate_{table}_{suffix}"
            self.conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            statements = "\n".join(
                "SELECT CASE WHEN NOT ("
                + predicate
                + ") THEN RAISE(ABORT, '"
                + message.replace("'", "")
                + "') END;"
                for predicate, message in checks
            )
            self.conn.execute(
                f"""
                CREATE TRIGGER {trigger}
                BEFORE {operation} ON {table}
                BEGIN
                    {statements}
                END
                """
            )

    def _migration_value_constraints(self) -> None:
        """Reject structurally invalid dates, numbers, enums and JSON at SQLite level."""

        if bool(getattr(self, "_defer_validation_trigger_rebuild", False)):
            self._validation_triggers_dirty = True
            return
        finite = sql_finite_number
        iso_date = sql_iso_date
        month_key = sql_month_key

        def text_required(column: str) -> str:
            return f"TYPEOF({column})='text' AND LENGTH(TRIM({column}))>0"

        validations: dict[str, list[tuple[str, str]]] = {
            "categories": [
                (text_required("NEW.name"), "categories.name is required"),
                ("NEW.kind IN ('income','expense')", "categories.kind is invalid"),
                (
                    finite("NEW.budget", non_negative=True),
                    "categories.budget is invalid",
                ),
            ],
            "transactions": [
                (iso_date("NEW.date"), "transactions.date is invalid"),
                (
                    text_required("NEW.description"),
                    "transactions.description is required",
                ),
                (text_required("NEW.category"), "transactions.category is required"),
                (text_required("NEW.account"), "transactions.account is required"),
                (finite("NEW.amount"), "transactions.amount is invalid"),
                ("ABS(NEW.amount)>=0.005", "transactions.amount cannot be zero"),
                ("NEW.kind IN ('income','expense')", "transactions.kind is invalid"),
                (
                    "(NEW.amount>0 AND NEW.kind='income') OR "
                    "(NEW.amount<0 AND NEW.kind='expense')",
                    "transactions.kind does not match amount",
                ),
            ],
            "subscriptions": [
                (text_required("NEW.name"), "subscriptions.name is required"),
                (
                    finite("NEW.amount", non_negative=True),
                    "subscriptions.amount is invalid",
                ),
                ("NEW.amount>=0.005", "subscriptions.amount cannot be zero"),
                (
                    "NEW.renewal_day BETWEEN 1 AND 31",
                    "subscriptions.renewal_day is invalid",
                ),
                (
                    "NEW.period IN ('Mensal','Trimestral','Semestral','Anual')",
                    "subscriptions.period is invalid",
                ),
                ("NEW.active IN (0,1)", "subscriptions.active is invalid"),
                (
                    iso_date("NEW.last_used_date", allow_empty=True),
                    "subscriptions.last_used_date is invalid",
                ),
                (
                    iso_date("NEW.next_renewal_date", allow_empty=True),
                    "subscriptions.next_renewal_date is invalid",
                ),
            ],
            "subscription_occurrences": [
                (
                    iso_date("NEW.due_date"),
                    "subscription_occurrences.due_date is invalid",
                ),
            ],
            "holdings": (
                [
                    (text_required("NEW.name"), "holdings.name is required"),
                    (finite("NEW.value", non_negative=True), "holdings.value is invalid"),
                    (
                        finite("NEW.cost_basis", non_negative=True),
                        "holdings.cost_basis is invalid",
                    ),
                    (finite("NEW.fees", non_negative=True), "holdings.fees is invalid"),
                ]
                + (
                    [(text_required("NEW.currency"), "holdings.currency is required")]
                    if "currency"
                    in {str(r["name"]) for r in self.query("PRAGMA table_info(holdings)")}
                    else []
                )
                + (
                    [
                        (
                            finite("NEW.exchange_rate", non_negative=True),
                            "holdings.exchange_rate is invalid",
                        ),
                        ("NEW.exchange_rate>0", "holdings.exchange_rate must be positive"),
                    ]
                    if "exchange_rate"
                    in {str(r["name"]) for r in self.query("PRAGMA table_info(holdings)")}
                    else []
                )
            ),
            "financial_accounts": [
                (text_required("NEW.name"), "financial_accounts.name is required"),
                (
                    finite("NEW.reference_balance"),
                    "financial_accounts.balance is invalid",
                ),
                ("NEW.balance_known IN (0,1)", "financial_accounts.quality is invalid"),
                (
                    iso_date("NEW.reference_date", allow_empty=True),
                    "financial_accounts.date is invalid",
                ),
                (
                    "(NEW.balance_known=0 AND NEW.reference_date='') OR "
                    "(NEW.balance_known=1 AND NEW.reference_date<>'')",
                    "financial_accounts.reference is incomplete",
                ),
            ],
            "liabilities": [
                (text_required("NEW.name"), "liabilities.name is required"),
                (
                    finite("NEW.balance", non_negative=True),
                    "liabilities.balance is invalid",
                ),
                (
                    finite("NEW.interest_rate", non_negative=True),
                    "liabilities.interest_rate is invalid",
                ),
                (
                    finite("NEW.monthly_payment", non_negative=True),
                    "liabilities.monthly_payment is invalid",
                ),
            ],
            "goals": [
                (text_required("NEW.name"), "goals.name is required"),
                (
                    finite("NEW.target_amount", non_negative=True),
                    "goals.target_amount is invalid",
                ),
                (
                    finite("NEW.saved_amount", non_negative=True),
                    "goals.saved_amount is invalid",
                ),
                (
                    finite("NEW.monthly_contribution", non_negative=True),
                    "goals.monthly_contribution is invalid",
                ),
                (
                    iso_date("NEW.deadline", allow_empty=True),
                    "goals.deadline is invalid",
                ),
                (
                    "NEW.goal_type IN ('standard','emergency_fund')",
                    "goals.goal_type is invalid",
                ),
            ],
            "planned_purchases": [
                (text_required("NEW.name"), "planned_purchases.name is required"),
                (
                    finite("NEW.target_amount", non_negative=True),
                    "planned_purchases.target_amount is invalid",
                ),
                (
                    finite("NEW.saved_amount", non_negative=True),
                    "planned_purchases.saved_amount is invalid",
                ),
                (
                    iso_date("NEW.desired_date", allow_empty=True),
                    "planned_purchases.desired_date is invalid",
                ),
                (
                    "NEW.priority IN ('Alta','Média','Baixa')",
                    "planned_purchases.priority is invalid",
                ),
            ],
            "transaction_splits": [
                (
                    text_required("NEW.category"),
                    "transaction_splits.category is required",
                ),
                (
                    finite("NEW.percent", non_negative=True),
                    "transaction_splits.percent is invalid",
                ),
                (
                    "NEW.percent>0 AND NEW.percent<=100",
                    "transaction_splits.percent is outside range",
                ),
                (
                    finite("NEW.amount", non_negative=True),
                    "transaction_splits.amount is invalid",
                ),
            ],
            "category_rules": [
                (text_required("NEW.keyword"), "category_rules.keyword is required"),
                (text_required("NEW.category"), "category_rules.category is required"),
                (
                    "NEW.split_json='' OR JSON_VALID(NEW.split_json)",
                    "category_rules.split_json is invalid",
                ),
            ],
            "monthly_category_budgets": [
                (month_key("NEW.month"), "monthly_category_budgets.month is invalid"),
                (
                    finite("NEW.budget", non_negative=True),
                    "monthly_category_budgets.budget is invalid",
                ),
            ],
            "monthly_targets": [
                (month_key("NEW.month"), "monthly_targets.month is invalid"),
                (
                    finite("NEW.expense_target", non_negative=True),
                    "monthly_targets.expense_target is invalid",
                ),
                (
                    finite("NEW.savings_target", non_negative=True),
                    "monthly_targets.savings_target is invalid",
                ),
                (
                    finite("NEW.investment_target", non_negative=True),
                    "monthly_targets.investment_target is invalid",
                ),
            ],
            "scheduled_payments": [
                (text_required("NEW.name"), "scheduled_payments.name is required"),
                (
                    finite("NEW.amount", non_negative=True),
                    "scheduled_payments.amount is invalid",
                ),
                ("NEW.amount>=0.005", "scheduled_payments.amount cannot be zero"),
                (
                    iso_date("NEW.start_date"),
                    "scheduled_payments.start_date is invalid",
                ),
                (
                    "NEW.debit_day BETWEEN 1 AND 31",
                    "scheduled_payments.debit_day is invalid",
                ),
                ("NEW.months_total>=0", "scheduled_payments.months_total is invalid"),
                (
                    "NEW.months_paid>=0 AND "
                    "(NEW.months_total=0 OR NEW.months_paid<=NEW.months_total)",
                    "scheduled_payments.months_paid is invalid",
                ),
                ("NEW.active IN (0,1)", "scheduled_payments.active is invalid"),
            ],
            "scheduled_payment_occurrences": [
                (
                    iso_date("NEW.due_date"),
                    "scheduled_payment_occurrences.due_date is invalid",
                ),
            ],
            "monthly_goals": [
                (month_key("NEW.month"), "monthly_goals.month is invalid"),
                (text_required("NEW.name"), "monthly_goals.name is required"),
                (
                    "NEW.goal_type IN ('Poupar pelo menos','Investir pelo menos','Gastar no máximo')",
                    "monthly_goals.goal_type is invalid",
                ),
                (
                    finite("NEW.target_value", non_negative=True),
                    "monthly_goals.target_value is invalid",
                ),
            ],
            "investment_snapshots": [
                (iso_date("NEW.day"), "investment_snapshots.day is invalid"),
                (
                    finite("NEW.invested", non_negative=True),
                    "investment_snapshots.invested is invalid",
                ),
                (
                    finite("NEW.total", non_negative=True),
                    "investment_snapshots.total is invalid",
                ),
            ],
            "monthly_snapshots": [
                (month_key("NEW.month"), "monthly_snapshots.month is invalid"),
                (
                    finite("NEW.income", non_negative=True),
                    "monthly_snapshots.income is invalid",
                ),
                (
                    finite("NEW.expenses", non_negative=True),
                    "monthly_snapshots.expenses is invalid",
                ),
                (
                    finite("NEW.investments", non_negative=True),
                    "monthly_snapshots.investments is invalid",
                ),
                (finite("NEW.savings"), "monthly_snapshots.savings is invalid"),
                (
                    finite("NEW.savings_rate"),
                    "monthly_snapshots.savings_rate is invalid",
                ),
                (
                    finite("NEW.cash_balance"),
                    "monthly_snapshots.cash_balance is invalid",
                ),
                (finite("NEW.net_worth"), "monthly_snapshots.net_worth is invalid"),
                (
                    "JSON_VALID(NEW.goals_json)",
                    "monthly_snapshots.goals_json is invalid",
                ),
                (
                    "JSON_VALID(NEW.holdings_json)",
                    "monthly_snapshots.holdings_json is invalid",
                ),
            ],
            "financial_state_snapshots": [
                (iso_date("NEW.day"), "financial_state_snapshots.day is invalid"),
                (
                    finite("NEW.cash_balance"),
                    "financial_state_snapshots.cash_balance is invalid",
                ),
                (
                    "JSON_VALID(NEW.holdings_json)",
                    "financial_state_snapshots.holdings_json is invalid",
                ),
                (
                    "JSON_VALID(NEW.liabilities_json)",
                    "financial_state_snapshots.liabilities_json is invalid",
                ),
                (
                    "JSON_VALID(NEW.subscriptions_json)",
                    "financial_state_snapshots.subscriptions_json is invalid",
                ),
            ],
            "habit_change_plans": [
                (text_required("NEW.name"), "habit_change_plans.name is required"),
                (
                    finite("NEW.baseline_unit_cost", non_negative=True),
                    "habit_change_plans.baseline_unit_cost is invalid",
                ),
                (
                    finite("NEW.baseline_frequency", non_negative=True),
                    "habit_change_plans.baseline_frequency is invalid",
                ),
                (
                    finite("NEW.alternative_unit_cost", non_negative=True),
                    "habit_change_plans.alternative_unit_cost is invalid",
                ),
                (
                    finite("NEW.target_frequency", non_negative=True),
                    "habit_change_plans.target_frequency is invalid",
                ),
                (
                    finite("NEW.baseline_monthly_cost", non_negative=True),
                    "habit_change_plans.baseline_monthly_cost is invalid",
                ),
                (
                    finite("NEW.planned_monthly_cost", non_negative=True),
                    "habit_change_plans.planned_monthly_cost is invalid",
                ),
                (
                    finite("NEW.planned_monthly_savings", non_negative=True),
                    "habit_change_plans.planned_monthly_savings is invalid",
                ),
                (
                    "NEW.frequency_unit IN ('day','week','month')",
                    "habit_change_plans.frequency_unit is invalid",
                ),
                (
                    iso_date("NEW.start_date"),
                    "habit_change_plans.start_date is invalid",
                ),
                (
                    "NEW.status IN ('active','paused','completed')",
                    "habit_change_plans.status is invalid",
                ),
                (
                    "JSON_VALID(NEW.audit_rules_json)",
                    "habit_change_plans.audit_rules_json is invalid",
                ),
            ],
            "transaction_meta": [
                (
                    "NEW.is_reimbursable IN (0,1)",
                    "transaction_meta.is_reimbursable is invalid",
                ),
                (
                    iso_date("NEW.purchase_date", allow_empty=True),
                    "transaction_meta.purchase_date is invalid",
                ),
                (
                    iso_date("NEW.payment_date", allow_empty=True),
                    "transaction_meta.payment_date is invalid",
                ),
                (
                    month_key("NEW.payment_month", allow_empty=True),
                    "transaction_meta.payment_month is invalid",
                ),
            ],
            "transaction_attachments": [
                (
                    iso_date("NEW.invoice_date", allow_empty=True),
                    "transaction_attachments.invoice_date is invalid",
                ),
                (
                    "NEW.total_amount IS NULL OR "
                    + finite("NEW.total_amount", non_negative=True),
                    "transaction_attachments.total_amount is invalid",
                ),
            ],
            "saved_filters": [
                (text_required("NEW.name"), "saved_filters.name is required"),
                ("JSON_VALID(NEW.filter_json)", "saved_filters.filter_json is invalid"),
            ],
            "change_journal": [
                (
                    text_required("NEW.action_type"),
                    "change_journal.action_type is required",
                ),
                (
                    "(TYPEOF(NEW.payload_json)='blob' OR JSON_VALID(NEW.payload_json) OR SUBSTR(CAST(NEW.payload_json AS TEXT),1,5) IN ('ZSTD:','ZLIB:','RAW:'))",
                    "change_journal.payload_json is invalid",
                ),
                (
                    "NEW.status IN ('available','undone','failed')",
                    "change_journal.status is invalid",
                ),
            ],
        }
        subscription_columns = {
            str(row["name"]) for row in self.query("PRAGMA table_info(subscriptions)")
        }
        if "account" in subscription_columns:
            validations["subscriptions"].insert(
                1,
                (
                    text_required("NEW.account"),
                    "subscriptions.account is required",
                ),
            )
        holding_columns = {
            str(row["name"]) for row in self.query("PRAGMA table_info(holdings)")
        }
        if "cost_basis_known" in holding_columns:
            validations["holdings"].append(
                (
                    "NEW.cost_basis_known IN (0,1)",
                    "holdings.cost_basis_known is invalid",
                )
            )
        attachment_columns = {
            str(row["name"])
            for row in self.query("PRAGMA table_info(transaction_attachments)")
        }
        if "content_sha256" in attachment_columns:
            validations["transaction_attachments"].append(
                (
                    "NEW.content_sha256='' OR (LENGTH(NEW.content_sha256)=64 "
                    "AND NEW.content_sha256 NOT GLOB '*[^0-9A-Fa-f]*')",
                    "transaction_attachments.content_sha256 is invalid",
                )
            )
        if "file_size" in attachment_columns:
            validations["transaction_attachments"].append(
                (
                    "TYPEOF(NEW.file_size)='integer' AND NEW.file_size>=0",
                    "transaction_attachments.file_size is invalid",
                )
            )
        if "file_mtime_ns" in attachment_columns:
            validations["transaction_attachments"].append(
                (
                    "TYPEOF(NEW.file_mtime_ns)='integer' AND NEW.file_mtime_ns>=0",
                    "transaction_attachments.file_mtime_ns is invalid",
                )
            )
        financial_state_columns = {
            str(row["name"])
            for row in self.query("PRAGMA table_info(financial_state_snapshots)")
        }
        if "goals_json" in financial_state_columns:
            validations["financial_state_snapshots"].append(
                (
                    "NEW.goals_json IS NULL OR JSON_VALID(NEW.goals_json)",
                    "financial_state_snapshots.goals_json is invalid",
                )
            )
        for table, checks in validations.items():
            if (
                self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                is not None
            ):
                self._install_validation_triggers(table, checks)

        for operation in ("INSERT", "UPDATE"):
            trigger = f"validate_transaction_splits_total_{operation.casefold()}"
            self.conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self.conn.execute(
                f"""
                CREATE TRIGGER {trigger}
                AFTER {operation} ON transaction_splits
                WHEN (
                    SELECT COALESCE(SUM(percent),0)
                    FROM transaction_splits
                    WHERE transaction_id=NEW.transaction_id
                ) > 100.05
                OR (
                    SELECT COALESCE(SUM(amount),0)
                    FROM transaction_splits
                    WHERE transaction_id=NEW.transaction_id
                ) > (
                    SELECT ROUND(ABS(amount) + 0.015, 2)
                    FROM transactions
                    WHERE id=NEW.transaction_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'transaction_splits total exceeds transaction amount or 100 percent');
                END
                """
            )

    def _migration_attachment_integrity(self) -> None:
        """Add non-destructive hashes and verification metadata to attachments."""

        columns = {
            str(row["name"])
            for row in self.query("PRAGMA table_info(transaction_attachments)")
        }
        additions = (
            ("content_sha256", "TEXT NOT NULL DEFAULT ''"),
            ("file_size", "INTEGER NOT NULL DEFAULT 0"),
            ("last_verified_at", "TEXT NOT NULL DEFAULT ''"),
        )
        for name, declaration in additions:
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE transaction_attachments "
                    f"ADD COLUMN {name} {declaration}"
                )
        # Migration 16 may already have installed the old attachment trigger.
        # Reinstall all guards so the new columns are protected as well.
        self._migration_value_constraints()

    def _migration_import_traceability_and_subscription_aliases(self) -> None:
        """Keep bank references and user-confirmed subscription names."""

        subscription_columns = {
            str(row["name"]) for row in self.query("PRAGMA table_info(subscriptions)")
        }
        if "aliases" not in subscription_columns:
            self.conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN aliases TEXT NOT NULL DEFAULT ''"
            )

        meta_columns = {
            str(row["name"])
            for row in self.query("PRAGMA table_info(transaction_meta)")
        }
        if "external_reference" not in meta_columns:
            self.conn.execute(
                "ALTER TABLE transaction_meta "
                "ADD COLUMN external_reference TEXT NOT NULL DEFAULT ''"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transaction_meta_external_reference "
            "ON transaction_meta(external_reference)"
        )

    def _migration_subscription_payment_routing(self) -> None:
        """Associate recurring subscription movements with their real account."""

        columns = {
            str(row["name"]) for row in self.query("PRAGMA table_info(subscriptions)")
        }
        additions = (
            ("account", "TEXT NOT NULL DEFAULT 'Conta principal'"),
            ("payment_method", "TEXT NOT NULL DEFAULT ''"),
        )
        for name, declaration in additions:
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE subscriptions ADD COLUMN {name} {declaration}"
                )
        self._migration_value_constraints()

    def _migration_financial_integrity_links(self) -> None:
        """Add non-destructive links and provenance needed by financial totals."""

        def add_column(table: str, name: str, declaration: str) -> None:
            columns = {
                str(row["name"]) for row in self.query(f"PRAGMA table_info({table})")
            }
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                )

        add_column(
            "transactions",
            "transfer_group_id",
            "TEXT NOT NULL DEFAULT ''",
        )
        add_column(
            "holdings",
            "cost_basis_known",
            "INTEGER NOT NULL DEFAULT 0",
        )
        add_column(
            "holdings",
            "linked_account",
            "TEXT NOT NULL DEFAULT ''",
        )
        add_column(
            "transaction_attachments",
            "file_mtime_ns",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self.conn.execute(
            "UPDATE holdings SET cost_basis_known=1 "
            "WHERE cost_basis_known=0 AND cost_basis>0"
        )
        self.run_schema_script(
            """
            CREATE TABLE IF NOT EXISTS financial_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                reference_balance REAL NOT NULL DEFAULT 0,
                reference_date TEXT NOT NULL DEFAULT '',
                balance_known INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_transfer_group
                ON transactions(transfer_group_id)
                WHERE transfer_group_id<>'';
            """
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO financial_accounts(name)
            SELECT DISTINCT COALESCE(NULLIF(TRIM(account),''),'Conta principal')
            FROM transactions
            """
        )

        # Older CSV imports used a small set of labels that are semantic
        # aliases of the application's reference categories.  Restrict the
        # repair to rows carrying the import marker so a user-created custom
        # category with the same name is never silently renamed.
        imported_category_aliases = {
            "Alimentação": "Supermercado",
            "Alimentacao": "Supermercado",
            "Alimentação e supermercado": "Supermercado",
            "Alimentacao e supermercado": "Supermercado",
            "Mercearia": "Supermercado",
            "Supermercados": "Supermercado",
            "Salário": "Rendimentos",
            "Salario": "Rendimentos",
            "Salários": "Rendimentos",
            "Salarios": "Rendimentos",
            "Ordenado": "Rendimentos",
            "Vencimento": "Rendimentos",
            "Receita": "Rendimentos",
            "Receitas": "Rendimentos",
            "Outro": "Diversos",
            "Outros": "Diversos",
            "Outras": "Diversos",
            "Misc": "Diversos",
            "Miscellaneous": "Diversos",
            "Transportes": "Transporte",
            "Subscrição": "Subscrições",
            "Subscricao": "Subscrições",
            "Subscricoes": "Subscrições",
        }
        for old_category, canonical_category in imported_category_aliases.items():
            self.conn.execute(
                """
                UPDATE transaction_splits
                SET category=?
                WHERE category=?
                  AND transaction_id IN (
                      SELECT id FROM transactions
                      WHERE INSTR(COALESCE(notes,''), 'Importado por CSV') > 0
                  )
                """,
                (canonical_category, old_category),
            )
            self.conn.execute(
                """
                UPDATE transactions
                SET category=?
                WHERE category=?
                  AND INSTR(COALESCE(notes,''), 'Importado por CSV') > 0
                """,
                (canonical_category, old_category),
            )

        # Link only legacy pairs that match unambiguously. No financial field is
        # rewritten: this adds a relationship to two rows that already cancel.
        legacy = self.query(
            """
            SELECT id,date,description,amount,account
            FROM transactions
            WHERE transfer_group_id=''
              AND category='Transferência Interna'
            ORDER BY id
            """
        )
        credits = [row for row in legacy if float(row["amount"] or 0) > 0]
        debit_candidates: dict[int, list[Any]] = {}
        debit_rows = [row for row in legacy if float(row["amount"] or 0) < 0]
        for debit in debit_rows:
            source = str(debit["account"] or "").strip()
            debit_description = str(debit["description"] or "")
            prefix = "Transferência para "
            if not debit_description.startswith(prefix):
                continue
            destination = debit_description[len(prefix) :].strip()
            debit_candidates[int(debit["id"])] = [
                credit
                for credit in credits
                if str(credit["date"] or "") == str(debit["date"] or "")
                and abs(float(credit["amount"] or 0) + float(debit["amount"] or 0))
                < 0.000001
                and str(credit["account"] or "").strip() == destination
                and str(credit["description"] or "").strip()
                == f"Transferência de {source}"
            ]
        credit_match_counts: dict[int, int] = {}
        for matches in debit_candidates.values():
            for credit in matches:
                credit_id = int(credit["id"])
                credit_match_counts[credit_id] = (
                    credit_match_counts.get(credit_id, 0) + 1
                )

        for debit in debit_rows:
            matches = debit_candidates.get(int(debit["id"]), [])
            if len(matches) != 1:
                continue
            credit = matches[0]
            if credit_match_counts.get(int(credit["id"]), 0) != 1:
                continue
            group_id = f"legacy-{uuid.uuid4().hex}"
            self.conn.execute(
                "UPDATE transactions SET transfer_group_id=? WHERE id IN (?,?)",
                (group_id, int(debit["id"]), int(credit["id"])),
            )
        self._migration_value_constraints()

    def _migration_legacy_transfer_startup_compatibility(self) -> None:
        """Repair transfer linkage even when an old migration ledger drifted.

        ``init_schema`` must never reference a column that can be absent from an
        existing database: SQLite's ``CREATE TABLE IF NOT EXISTS`` does not add
        new columns to an old table.  Migration 20 normally creates this field;
        this later idempotent repair also covers databases whose ledger says 20
        was applied while the physical column is missing.
        """

        columns = {
            str(row["name"]) for row in self.query("PRAGMA table_info(transactions)")
        }
        if "transfer_group_id" not in columns:
            self.conn.execute(
                "ALTER TABLE transactions ADD COLUMN "
                "transfer_group_id TEXT NOT NULL DEFAULT ''"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_transfer_group "
            "ON transactions(transfer_group_id) WHERE transfer_group_id<>''"
        )

    @staticmethod
    def _stable_entity_id(entity_type: str, *parts: object) -> str:
        """Build a deterministic opaque identity for one existing entity."""

        source = "|".join(str(part or "").strip() for part in parts)
        token = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"financas-pessoais/{entity_type}/{source}",
        ).hex
        prefixes = {
            "account": "acc",
            "category": "cat",
            "asset": "ast",
        }
        return f"{prefixes.get(entity_type, 'ent')}_{token}"

    def _ensure_stable_financial_entity_identity(self) -> None:
        """Repair stable entity links without changing financial amounts.

        Human-readable names remain as compatibility/display snapshots.  The
        opaque IDs added here are the durable relationship used by new writes,
        merges and detail views.
        """

        from services.account_identity import account_identity_key, clean_account_name
        from services.category_taxonomy import category_identity_key

        self._migration_liabilities()
        self._migration_monthly_category_budgets()
        self.ensure_document_tables()
        self.ensure_analytics_tables()

        def columns(table: str) -> set[str]:

            return {
                str(row["name"]) for row in self.query(f"PRAGMA table_info({table})")
            }

        def add_column(table: str, name: str, declaration: str) -> None:
            table_cols = columns(table)
            if table_cols and name not in table_cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                )

        for name, declaration in (
            ("stable_id", "TEXT NOT NULL DEFAULT ''"),
            ("identity_key", "TEXT NOT NULL DEFAULT ''"),
            ("is_active", "INTEGER NOT NULL DEFAULT 1"),
            ("merged_into_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            add_column("categories", name, declaration)
        for name, declaration in (
            ("account_key", "TEXT NOT NULL DEFAULT ''"),
            ("balance_source", "TEXT NOT NULL DEFAULT ''"),
            ("stable_id", "TEXT NOT NULL DEFAULT ''"),
            ("is_active", "INTEGER NOT NULL DEFAULT 1"),
            ("merged_into_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            add_column("financial_accounts", name, declaration)
        try:
            self.conn.execute(
                "UPDATE financial_accounts SET balance_source='manual' "
                "WHERE balance_known=1 AND TRIM(balance_source)=''"
            )
        except Exception:
            pass
        for name, declaration in (
            ("stable_id", "TEXT NOT NULL DEFAULT ''"),
            ("linked_account_id", "TEXT NOT NULL DEFAULT ''"),
        ):
            add_column("holdings", name, declaration)
        add_column("liabilities", "account_id", "TEXT NOT NULL DEFAULT ''")

        linked_columns = {
            "transactions": ("category_id", "account_id"),
            "subscriptions": ("category_id", "account_id"),
            "scheduled_payments": ("category_id", "account_id"),
            "transaction_splits": ("category_id",),
            "category_rules": ("category_id",),
            "monthly_category_budgets": ("category_id",),
            "monthly_goals": ("category_id",),
            "subcategories": ("category_id",),
        }
        for table, names in linked_columns.items():
            table_columns = columns(table)
            if not table_columns:
                continue
            for name in names:
                if name not in table_columns:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} "
                        "TEXT NOT NULL DEFAULT ''"
                    )

        self.run_schema_script(
            """
            CREATE TABLE IF NOT EXISTS entity_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                identity_key TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(entity_type, entity_id, identity_key)
            );
            CREATE TABLE IF NOT EXISTS entity_merges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                target_name TEXT NOT NULL,
                affected_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_entity_alias_lookup
                ON entity_aliases(entity_type, identity_key);
            CREATE INDEX IF NOT EXISTS idx_entity_merges_created
                ON entity_merges(created_at DESC, id DESC);
            """
        )

        category_reference_tables = (
            ("transactions", "category"),
            ("subscriptions", "category"),
            ("scheduled_payments", "category"),
            ("transaction_splits", "category"),
            ("category_rules", "category"),
            ("monthly_category_budgets", "category"),
            ("monthly_goals", "category"),
            ("subcategories", "category"),
        )
        referenced_categories: set[str] = set()
        for table, column in category_reference_tables:
            if not columns(table):
                continue
            referenced_categories.update(
                str(row["value"] or "").strip()
                for row in self.query(
                    f"SELECT DISTINCT {column} AS value FROM {table} "
                    f"WHERE TRIM(COALESCE({column},''))<>''"
                )
            )
        registered_category_keys = {
            str(row["identity_key"] or "") or category_identity_key(row["name"])
            for row in self.query("SELECT name,identity_key FROM categories")
        }
        for name in sorted(referenced_categories, key=str.casefold):
            key = category_identity_key(name)
            if key in registered_category_keys:
                continue
            signs = self.conn.execute(
                "SELECT SUM(CASE WHEN amount>0 THEN 1 ELSE 0 END) AS income_rows,"
                "SUM(CASE WHEN amount<0 THEN 1 ELSE 0 END) AS expense_rows "
                "FROM transactions WHERE category=?",
                (name,),
            ).fetchone()
            income_rows = int(signs["income_rows"] or 0) if signs else 0
            expense_rows = int(signs["expense_rows"] or 0) if signs else 0
            kind = "income" if income_rows and not expense_rows else "expense"
            self.conn.execute(
                "INSERT OR IGNORE INTO categories("
                "name,kind,colour,budget,identity_key) VALUES(?,?,?,0,?)",
                (name, kind, "#94A3B8", key),
            )
            registered_category_keys.add(key)

        account_reference_tables = (
            ("transactions", "account"),
            ("subscriptions", "account"),
            ("scheduled_payments", "account"),
            ("holdings", "linked_account"),
            ("liabilities", "account"),
        )
        referenced_accounts: set[str] = set()
        for table, column in account_reference_tables:
            if not columns(table):
                continue
            referenced_accounts.update(
                clean_account_name(row["value"])
                for row in self.query(
                    f"SELECT DISTINCT {column} AS value FROM {table} "
                    f"WHERE TRIM(COALESCE({column},''))<>''"
                )
            )

        registered_account_keys = {
            str(row["account_key"] or "") or account_identity_key(row["name"])
            for row in self.query("SELECT name,account_key FROM financial_accounts")
        }
        for name in sorted(referenced_accounts, key=str.casefold):
            key = account_identity_key(name)
            if key in registered_account_keys:
                continue
            self.conn.execute(
                "INSERT OR IGNORE INTO financial_accounts(name,account_key) "
                "VALUES(?,?)",
                (name, key),
            )
            registered_account_keys.add(key)
        for table in ("subscriptions", "scheduled_payments"):
            if not columns(table):
                continue
            self.run_schema_script(
                f"""
                DROP TRIGGER IF EXISTS trg_{table}_identity_insert;
                DROP TRIGGER IF EXISTS trg_{table}_identity_update;
                CREATE TRIGGER trg_{table}_identity_insert
                AFTER INSERT ON {table}
                BEGIN
                    UPDATE {table}
                    SET category_id=COALESCE(
                            (SELECT stable_id FROM categories
                             WHERE stable_id=NEW.category_id AND is_active=1 LIMIT 1),
                            (SELECT stable_id FROM categories
                             WHERE name=NEW.category AND is_active=1 LIMIT 1),''),
                        account_id=COALESCE(
                            (SELECT stable_id FROM financial_accounts
                             WHERE stable_id=NEW.account_id AND is_active=1 LIMIT 1),
                            (SELECT stable_id FROM financial_accounts
                             WHERE name=NEW.account COLLATE NOCASE AND is_active=1 LIMIT 1),'')
                    WHERE id=NEW.id;
                    UPDATE {table}
                    SET category=COALESCE(
                            (SELECT name FROM categories WHERE stable_id=category_id),category),
                        account=COALESCE(
                            (SELECT name FROM financial_accounts WHERE stable_id=account_id),account)
                    WHERE id=NEW.id;
                END;
                CREATE TRIGGER trg_{table}_identity_update
                AFTER UPDATE OF category,category_id,account,account_id ON {table}
                BEGIN
                    UPDATE {table}
                    SET category_id=COALESCE(
                            (SELECT stable_id FROM categories
                             WHERE stable_id=NEW.category_id AND is_active=1 LIMIT 1),
                            (SELECT stable_id FROM categories
                             WHERE name=NEW.category AND is_active=1 LIMIT 1),''),
                        account_id=COALESCE(
                            (SELECT stable_id FROM financial_accounts
                             WHERE stable_id=NEW.account_id AND is_active=1 LIMIT 1),
                            (SELECT stable_id FROM financial_accounts
                             WHERE name=NEW.account COLLATE NOCASE AND is_active=1 LIMIT 1),'')
                    WHERE id=NEW.id;
                    UPDATE {table}
                    SET category=COALESCE(
                            (SELECT name FROM categories WHERE stable_id=category_id),category),
                        account=COALESCE(
                            (SELECT name FROM financial_accounts WHERE stable_id=account_id),account)
                    WHERE id=NEW.id;
                END;
                """
            )

        for table in ("category_rules", "monthly_goals"):
            self.run_schema_script(
                f"""
                DROP TRIGGER IF EXISTS trg_{table}_category_identity_insert;
                DROP TRIGGER IF EXISTS trg_{table}_category_identity_update;
                CREATE TRIGGER trg_{table}_category_identity_insert
                AFTER INSERT ON {table}
                BEGIN
                    UPDATE {table}
                    SET category_id=COALESCE(
                        (SELECT stable_id FROM categories
                         WHERE stable_id=NEW.category_id AND is_active=1 LIMIT 1),
                        (SELECT stable_id FROM categories
                         WHERE name=NEW.category AND is_active=1 LIMIT 1),'')
                    WHERE id=NEW.id;
                    UPDATE {table}
                    SET category=COALESCE(
                        (SELECT name FROM categories WHERE stable_id=category_id),category)
                    WHERE id=NEW.id;
                END;
                CREATE TRIGGER trg_{table}_category_identity_update
                AFTER UPDATE OF category,category_id ON {table}
                BEGIN
                    UPDATE {table}
                    SET category_id=COALESCE(
                        (SELECT stable_id FROM categories
                         WHERE stable_id=NEW.category_id AND is_active=1 LIMIT 1),
                        (SELECT stable_id FROM categories
                         WHERE name=NEW.category AND is_active=1 LIMIT 1),'')
                    WHERE id=NEW.id;
                    UPDATE {table}
                    SET category=COALESCE(
                        (SELECT name FROM categories WHERE stable_id=category_id),category)
                    WHERE id=NEW.id;
                END;
                """
            )

        self.run_schema_script(
            """
            DROP TRIGGER IF EXISTS trg_monthly_budgets_category_identity_insert;
            DROP TRIGGER IF EXISTS trg_monthly_budgets_category_identity_update;
            CREATE TRIGGER trg_monthly_budgets_category_identity_insert
            AFTER INSERT ON monthly_category_budgets
            BEGIN
                UPDATE monthly_category_budgets
                SET category_id=COALESCE(
                    (SELECT stable_id FROM categories
                     WHERE stable_id=NEW.category_id AND is_active=1 LIMIT 1),
                    (SELECT stable_id FROM categories
                     WHERE name=NEW.category AND is_active=1 LIMIT 1),'')
                WHERE rowid=NEW.rowid;
                UPDATE monthly_category_budgets
                SET category=COALESCE(
                    (SELECT name FROM categories WHERE stable_id=category_id),category)
                WHERE rowid=NEW.rowid;
            END;
            CREATE TRIGGER trg_monthly_budgets_category_identity_update
            AFTER UPDATE OF category,category_id ON monthly_category_budgets
            BEGIN
                UPDATE monthly_category_budgets
                SET category_id=COALESCE(
                    (SELECT stable_id FROM categories
                     WHERE stable_id=NEW.category_id AND is_active=1 LIMIT 1),
                    (SELECT stable_id FROM categories
                     WHERE name=NEW.category AND is_active=1 LIMIT 1),'')
                WHERE rowid=NEW.rowid;
                UPDATE monthly_category_budgets
                SET category=COALESCE(
                    (SELECT name FROM categories WHERE stable_id=category_id),category)
                WHERE rowid=NEW.rowid;
            END;

            DROP TRIGGER IF EXISTS trg_holdings_linked_account_identity_insert;
            DROP TRIGGER IF EXISTS trg_holdings_linked_account_identity_update;
            CREATE TRIGGER trg_holdings_linked_account_identity_insert
            AFTER INSERT ON holdings
            BEGIN
                UPDATE holdings
                SET linked_account_id=CASE WHEN TRIM(NEW.linked_account)='' THEN ''
                    ELSE COALESCE(
                        (SELECT stable_id FROM financial_accounts
                         WHERE stable_id=NEW.linked_account_id AND is_active=1 LIMIT 1),
                        (SELECT stable_id FROM financial_accounts
                         WHERE name=NEW.linked_account COLLATE NOCASE AND is_active=1 LIMIT 1),'')
                    END
                WHERE id=NEW.id;
                UPDATE holdings
                SET linked_account=COALESCE(
                    (SELECT name FROM financial_accounts
                     WHERE stable_id=linked_account_id),linked_account)
                WHERE id=NEW.id;
            END;
            CREATE TRIGGER trg_holdings_linked_account_identity_update
            AFTER UPDATE OF linked_account,linked_account_id ON holdings
            BEGIN
                UPDATE holdings
                SET linked_account_id=CASE WHEN TRIM(NEW.linked_account)='' THEN ''
                    ELSE COALESCE(
                        (SELECT stable_id FROM financial_accounts
                         WHERE stable_id=NEW.linked_account_id AND is_active=1 LIMIT 1),
                        (SELECT stable_id FROM financial_accounts
                         WHERE name=NEW.linked_account COLLATE NOCASE AND is_active=1 LIMIT 1),'')
                    END
                WHERE id=NEW.id;
                UPDATE holdings
                SET linked_account=COALESCE(
                    (SELECT name FROM financial_accounts
                     WHERE stable_id=linked_account_id),linked_account)
                WHERE id=NEW.id;
            END;

            DROP TRIGGER IF EXISTS trg_liabilities_account_identity_insert;
            DROP TRIGGER IF EXISTS trg_liabilities_account_identity_update;
            CREATE TRIGGER trg_liabilities_account_identity_insert
            AFTER INSERT ON liabilities
            BEGIN
                UPDATE liabilities
                SET account_id=CASE WHEN TRIM(NEW.account)='' THEN ''
                    ELSE COALESCE(
                        (SELECT stable_id FROM financial_accounts
                         WHERE stable_id=NEW.account_id AND is_active=1 LIMIT 1),
                        (SELECT stable_id FROM financial_accounts
                         WHERE name=NEW.account COLLATE NOCASE AND is_active=1 LIMIT 1),'')
                    END
                WHERE id=NEW.id;
                UPDATE liabilities
                SET account=COALESCE(
                    (SELECT name FROM financial_accounts
                     WHERE stable_id=account_id),account)
                WHERE id=NEW.id;
            END;
            CREATE TRIGGER trg_liabilities_account_identity_update
            AFTER UPDATE OF account,account_id ON liabilities
            BEGIN
                UPDATE liabilities
                SET account_id=CASE WHEN TRIM(NEW.account)='' THEN ''
                    ELSE COALESCE(
                        (SELECT stable_id FROM financial_accounts
                         WHERE stable_id=NEW.account_id AND is_active=1 LIMIT 1),
                        (SELECT stable_id FROM financial_accounts
                         WHERE name=NEW.account COLLATE NOCASE AND is_active=1 LIMIT 1),'')
                    END
                WHERE id=NEW.id;
                UPDATE liabilities
                SET account=COALESCE(
                    (SELECT name FROM financial_accounts
                     WHERE stable_id=account_id),account)
                WHERE id=NEW.id;
            END;
            """
        )

        used_ids: set[str] = set()
        category_rows = self.query(
            "SELECT rowid AS legacy_rowid,* FROM categories ORDER BY rowid"
        )
        categories_by_name: dict[str, tuple[str, str]] = {}
        categories_by_key: dict[str, tuple[str, str]] = {}
        for row in category_rows:
            name = str(row["name"] or "").strip()
            kind = str(row["kind"] or "expense").strip()
            key = str(row["identity_key"] or "").strip() or category_identity_key(name)
            stable_id = str(row["stable_id"] or "").strip()
            if not stable_id or stable_id in used_ids:
                stable_id = self._stable_entity_id(
                    "category",
                    kind,
                    name,
                    int(row["legacy_rowid"] or 0),
                )
            used_ids.add(stable_id)
            self.conn.execute(
                "UPDATE categories SET stable_id=?,identity_key=? WHERE name=?",
                (stable_id, key, name),
            )
            categories_by_name[name] = (stable_id, name)
            categories_by_key.setdefault(key, (stable_id, name))
            self.conn.execute(
                "INSERT OR IGNORE INTO entity_aliases("
                "entity_type,entity_id,alias,identity_key) VALUES('category',?,?,?)",
                (stable_id, name, key),
            )

        # Rebuild the lookup after assigning IDs.  A merged/inactive label must
        # always resolve to its active destination when this repair is rerun at
        # startup; otherwise a stale display name could revive an archived ID.
        categories_by_name.clear()
        categories_by_key.clear()
        resolved_categories = {
            str(row["stable_id"]): row
            for row in self.query(
                "SELECT stable_id,name,identity_key,is_active,merged_into_id "
                "FROM categories ORDER BY is_active DESC,rowid"
            )
        }
        for row in resolved_categories.values():
            target = row
            # BUG-AUD-C91: Transitive multi-hop merge resolution with cycle detection
            visited_cat = {str(row["stable_id"])}
            while not int(target["is_active"] or 0) and str(target["merged_into_id"] or ""):
                mid = str(target["merged_into_id"] or "")
                if not mid or mid in visited_cat or mid not in resolved_categories:
                    break
                visited_cat.add(mid)
                target = resolved_categories[mid]
            name = str(row["name"] or "").strip()
            identity = (
                str(target["stable_id"] or ""),
                str(target["name"] or name),
            )
            categories_by_name[name] = identity
            categories_by_key.setdefault(
                str(row["identity_key"] or category_identity_key(name)),
                identity,
            )

        used_ids.clear()
        account_rows = self.query(
            "SELECT id,name,account_key,stable_id,is_active,merged_into_id "
            "FROM financial_accounts ORDER BY id"
        )
        accounts_by_name: dict[str, tuple[str, str]] = {}
        accounts_by_key: dict[str, tuple[str, str]] = {}
        for row in account_rows:
            name = clean_account_name(row["name"])
            key = str(row["account_key"] or "").strip() or account_identity_key(name)
            stable_id = str(row["stable_id"] or "").strip()
            if not stable_id or stable_id in used_ids:
                stable_id = self._stable_entity_id(
                    "account",
                    key,
                    int(row["id"] or 0),
                )
            used_ids.add(stable_id)
            self.conn.execute(
                "UPDATE financial_accounts SET name=?,account_key=?,stable_id=? "
                "WHERE id=?",
                (name, key, stable_id, int(row["id"])),
            )
            accounts_by_name[name.casefold()] = (stable_id, name)
            accounts_by_key.setdefault(key, (stable_id, name))
            self.conn.execute(
                "INSERT OR IGNORE INTO entity_aliases("
                "entity_type,entity_id,alias,identity_key) VALUES('account',?,?,?)",
                (stable_id, name, key),
            )

        accounts_by_name.clear()
        accounts_by_key.clear()
        resolved_accounts = {
            str(row["stable_id"]): row
            for row in self.query(
                "SELECT stable_id,name,account_key,is_active,merged_into_id "
                "FROM financial_accounts ORDER BY is_active DESC,id"
            )
        }
        for row in resolved_accounts.values():
            target = row
            # BUG-AUD-C91: Transitive multi-hop merge resolution with cycle detection
            visited_acc = {str(row["stable_id"])}
            while not int(target["is_active"] or 0) and str(target["merged_into_id"] or ""):
                mid = str(target["merged_into_id"] or "")
                if not mid or mid in visited_acc or mid not in resolved_accounts:
                    break
                visited_acc.add(mid)
                target = resolved_accounts[mid]
            name = clean_account_name(row["name"])
            identity = (
                str(target["stable_id"] or ""),
                str(target["name"] or name),
            )
            accounts_by_name[name.casefold()] = identity
            accounts_by_key.setdefault(
                str(row["account_key"] or account_identity_key(name)),
                identity,
            )

        used_ids.clear()
        for row in self.query("SELECT id,name,stable_id FROM holdings ORDER BY id"):
            stable_id = str(row["stable_id"] or "").strip()
            if not stable_id or stable_id in used_ids:
                stable_id = self._stable_entity_id(
                    "asset",
                    int(row["id"] or 0),
                    str(row["name"] or ""),
                )
            used_ids.add(stable_id)
            self.conn.execute(
                "UPDATE holdings SET stable_id=? WHERE id=?",
                (stable_id, int(row["id"])),
            )

        def category_identity(value: object) -> tuple[str, str]:
            display = str(value or "").strip()
            return categories_by_name.get(display) or categories_by_key.get(
                category_identity_key(display),
                ("", display),
            )

        def account_identity(value: object) -> tuple[str, str]:
            display = clean_account_name(value)
            return accounts_by_name.get(display.casefold()) or accounts_by_key.get(
                account_identity_key(display),
                ("", display),
            )

        for table in (
            "transactions",
            "subscriptions",
            "scheduled_payments",
        ):
            if not columns(table):
                continue
            for row in self.query(
                f"SELECT id,category,account,category_id,account_id FROM {table}"
            ):
                category_id, _category_name = category_identity(row["category"])
                account_id, _account_name = account_identity(row["account"])
                self.conn.execute(
                    f"UPDATE {table} SET category_id=?,account_id=? WHERE id=?",
                    (category_id, account_id, int(row["id"])),
                )

        for table in (
            "transaction_splits",
            "category_rules",
            "monthly_goals",
        ):
            if not columns(table):
                continue
            for row in self.query(f"SELECT id,category FROM {table}"):
                category_id, _category_name = category_identity(row["category"])
                self.conn.execute(
                    f"UPDATE {table} SET category_id=? WHERE id=?",
                    (category_id, int(row["id"])),
                )
        if columns("monthly_category_budgets"):
            for row in self.query(
                "SELECT rowid AS legacy_rowid,month,category "
                "FROM monthly_category_budgets"
            ):
                category_id, _category_name = category_identity(row["category"])
                self.conn.execute(
                    "UPDATE monthly_category_budgets SET category_id=? WHERE rowid=?",
                    (category_id, int(row["legacy_rowid"])),
                )
        if columns("subcategories"):
            for row in self.query(
                "SELECT rowid AS legacy_rowid,category FROM subcategories"
            ):
                category_id, _category_name = category_identity(row["category"])
                self.conn.execute(
                    "UPDATE subcategories SET category_id=? WHERE rowid=?",
                    (category_id, int(row["legacy_rowid"])),
                )
        if columns("holdings"):
            for row in self.query("SELECT id,linked_account FROM holdings"):
                linked_name = str(row["linked_account"] or "").strip()
                linked_id = account_identity(linked_name)[0] if linked_name else ""
                self.conn.execute(
                    "UPDATE holdings SET linked_account_id=? WHERE id=?",
                    (linked_id, int(row["id"])),
                )
        if columns("liabilities"):
            for row in self.query("SELECT id,account FROM liabilities"):
                account_name = str(row["account"] or "").strip()
                account_id = account_identity(account_name)[0] if account_name else ""
                self.conn.execute(
                    "UPDATE liabilities SET account_id=? WHERE id=?",
                    (account_id, int(row["id"])),
                )

        self.run_schema_script(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_stable_id
                ON categories(stable_id) WHERE stable_id<>'';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name_nocase
                ON categories(name COLLATE NOCASE);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_identity_active_unique
                ON categories(identity_key) WHERE is_active = 1 AND identity_key<>'';
            CREATE INDEX IF NOT EXISTS idx_categories_identity_active
                ON categories(identity_key,is_active);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_stable_id
                ON financial_accounts(stable_id) WHERE stable_id<>'';
            CREATE INDEX IF NOT EXISTS idx_accounts_active_name
                ON financial_accounts(is_active,name COLLATE NOCASE);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_holdings_stable_id
                ON holdings(stable_id) WHERE stable_id<>'';
            CREATE INDEX IF NOT EXISTS idx_transactions_category_id
                ON transactions(category_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_account_id
                ON transactions(account_id);
            CREATE INDEX IF NOT EXISTS idx_transaction_splits_category_id
                ON transaction_splits(category_id);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_category_account_ids
                ON subscriptions(category_id,account_id);
            CREATE INDEX IF NOT EXISTS idx_scheduled_category_account_ids
                ON scheduled_payments(category_id,account_id);
            CREATE INDEX IF NOT EXISTS idx_liabilities_account_id
                ON liabilities(account_id);
            """
        )
        self._recreate_financial_identity_triggers()

    def _stable_financial_identity_needs_repair(self) -> bool:
        """Use cheap checks to avoid scanning the full ledger at every startup."""

        if self.schema_version() < 24:
            return True
        missing_checks = (
            "SELECT 1 FROM categories "
            "WHERE TRIM(stable_id)='' OR TRIM(identity_key)='' LIMIT 1",
            "SELECT 1 FROM financial_accounts "
            "WHERE TRIM(stable_id)='' OR TRIM(account_key)='' LIMIT 1",
            "SELECT 1 FROM holdings WHERE TRIM(stable_id)='' LIMIT 1",
            "SELECT 1 FROM transactions "
            "WHERE TRIM(category_id)='' OR TRIM(account_id)='' LIMIT 1",
            "SELECT 1 FROM subscriptions "
            "WHERE TRIM(category_id)='' OR TRIM(account_id)='' LIMIT 1",
            "SELECT 1 FROM scheduled_payments "
            "WHERE TRIM(category_id)='' OR TRIM(account_id)='' LIMIT 1",
            "SELECT 1 FROM transaction_splits WHERE TRIM(category_id)='' LIMIT 1",
            "SELECT 1 FROM category_rules WHERE TRIM(category_id)='' LIMIT 1",
            "SELECT 1 FROM monthly_category_budgets WHERE TRIM(category_id)='' LIMIT 1",
            "SELECT 1 FROM monthly_goals WHERE TRIM(category)<>'' "
            "AND TRIM(category_id)='' LIMIT 1",
            "SELECT 1 FROM subcategories WHERE TRIM(category_id)='' LIMIT 1",
            "SELECT 1 FROM holdings WHERE TRIM(linked_account)<>'' "
            "AND TRIM(linked_account_id)='' LIMIT 1",
            "SELECT 1 FROM liabilities WHERE TRIM(account)<>'' "
            "AND TRIM(account_id)='' LIMIT 1",
        )
        if any(self.conn.execute(sql).fetchone() is not None for sql in missing_checks):
            return True
        expected_triggers = {
            "trg_transactions_identity_insert",
            "trg_transactions_identity_update",
            "trg_splits_identity_insert",
            "trg_splits_identity_update",
            "trg_subscriptions_identity_insert",
            "trg_subscriptions_identity_update",
            "trg_scheduled_payments_identity_insert",
            "trg_scheduled_payments_identity_update",
            "trg_category_rules_category_identity_insert",
            "trg_category_rules_category_identity_update",
            "trg_monthly_goals_category_identity_insert",
            "trg_monthly_goals_category_identity_update",
            "trg_monthly_budgets_category_identity_insert",
            "trg_monthly_budgets_category_identity_update",
            "trg_holdings_linked_account_identity_insert",
            "trg_holdings_linked_account_identity_update",
            "trg_liabilities_account_identity_insert",
            "trg_liabilities_account_identity_update",
            "trg_categories_display_name",
            "trg_accounts_display_name",
            "trg_categories_cleanup_before_delete",
            "trg_financial_accounts_prevent_delete_with_txs",
            "trg_transactions_cascade_transfer_leg",
        }
        installed = {
            str(row["name"])
            for row in self.query("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        return not expected_triggers.issubset(installed)

    def _recreate_financial_identity_triggers(self) -> None:
        """Keep stable links and legacy display labels synchronised."""

        self.run_schema_script(
            """
            DROP TRIGGER IF EXISTS trg_transactions_identity_insert;
            DROP TRIGGER IF EXISTS trg_transactions_identity_update;
            CREATE TRIGGER trg_transactions_identity_insert
            AFTER INSERT ON transactions
            WHEN TYPEOF(NEW.date)='text'
             AND LENGTH(NEW.date)=10
             AND DATE(NEW.date,'+0 days')=NEW.date
             AND TYPEOF(NEW.amount) IN ('integer','real')
             AND ABS(NEW.amount)>=0.005
            BEGIN
                UPDATE transactions
                SET category_id=COALESCE(
                        (SELECT stable_id FROM categories
                         WHERE stable_id=NEW.category_id AND is_active=1 LIMIT 1),
                        (SELECT stable_id FROM categories
                         WHERE name=NEW.category AND is_active=1 LIMIT 1),''),
                    account_id=COALESCE(
                        (SELECT stable_id FROM financial_accounts
                         WHERE stable_id=NEW.account_id AND is_active=1 LIMIT 1),
                        (SELECT stable_id FROM financial_accounts
                         WHERE name=NEW.account COLLATE NOCASE AND is_active=1 LIMIT 1),'')
                WHERE id=NEW.id;
                UPDATE transactions
                SET category=COALESCE(
                        (SELECT name FROM categories WHERE stable_id=category_id),category),
                    account=COALESCE(
                        (SELECT name FROM financial_accounts WHERE stable_id=account_id),account)
                WHERE id=NEW.id;
            END;
            CREATE TRIGGER trg_transactions_identity_update
            AFTER UPDATE OF category,category_id,account,account_id ON transactions
            BEGIN
                UPDATE transactions
                SET category_id=COALESCE(
                        (SELECT stable_id FROM categories
                         WHERE stable_id=NEW.category_id AND is_active=1 LIMIT 1),
                        (SELECT stable_id FROM categories
                         WHERE name=NEW.category AND is_active=1 LIMIT 1),''),
                    account_id=COALESCE(
                        (SELECT stable_id FROM financial_accounts
                         WHERE stable_id=NEW.account_id AND is_active=1 LIMIT 1),
                        (SELECT stable_id FROM financial_accounts
                         WHERE name=NEW.account COLLATE NOCASE AND is_active=1 LIMIT 1),'')
                WHERE id=NEW.id;
                UPDATE transactions
                SET category=COALESCE(
                        (SELECT name FROM categories WHERE stable_id=category_id),category),
                    account=COALESCE(
                        (SELECT name FROM financial_accounts WHERE stable_id=account_id),account)
                WHERE id=NEW.id;
            END;

            DROP TRIGGER IF EXISTS trg_splits_identity_insert;
            DROP TRIGGER IF EXISTS trg_splits_identity_update;
            CREATE TRIGGER trg_splits_identity_insert
            AFTER INSERT ON transaction_splits
            BEGIN
                UPDATE transaction_splits
                SET category_id=COALESCE(
                    (SELECT stable_id FROM categories
                     WHERE stable_id=NEW.category_id AND is_active=1 LIMIT 1),
                    (SELECT stable_id FROM categories
                     WHERE name=NEW.category AND is_active=1 LIMIT 1),'')
                WHERE id=NEW.id;
                UPDATE transaction_splits
                SET category=COALESCE(
                    (SELECT name FROM categories WHERE stable_id=category_id),category)
                WHERE id=NEW.id;
            END;
            CREATE TRIGGER trg_splits_identity_update
            AFTER UPDATE OF category,category_id ON transaction_splits
            BEGIN
                UPDATE transaction_splits
                SET category_id=COALESCE(
                    (SELECT stable_id FROM categories
                     WHERE stable_id=NEW.category_id AND is_active=1 LIMIT 1),
                    (SELECT stable_id FROM categories
                     WHERE name=NEW.category AND is_active=1 LIMIT 1),'')
                WHERE id=NEW.id;
                UPDATE transaction_splits
                SET category=COALESCE(
                    (SELECT name FROM categories WHERE stable_id=category_id),category)
                WHERE id=NEW.id;
            END;

            DROP TRIGGER IF EXISTS trg_categories_display_name;
            CREATE TRIGGER trg_categories_display_name
            AFTER UPDATE OF name ON categories
            WHEN OLD.name<>NEW.name
            BEGIN
                UPDATE transactions SET category=NEW.name WHERE category_id=NEW.stable_id;
                UPDATE transaction_splits SET category=NEW.name WHERE category_id=NEW.stable_id;
                UPDATE subscriptions SET category=NEW.name WHERE category_id=NEW.stable_id;
                UPDATE scheduled_payments SET category=NEW.name WHERE category_id=NEW.stable_id;
                UPDATE category_rules SET category=NEW.name WHERE category_id=NEW.stable_id;
                UPDATE monthly_category_budgets SET category=NEW.name
                    WHERE category_id=NEW.stable_id;
                UPDATE monthly_goals SET category=NEW.name WHERE category_id=NEW.stable_id;
                UPDATE subcategories SET category=NEW.name WHERE category_id=NEW.stable_id;
            END;

            DROP TRIGGER IF EXISTS trg_accounts_display_name;
            CREATE TRIGGER trg_accounts_display_name
            AFTER UPDATE OF name ON financial_accounts
            WHEN OLD.name<>NEW.name
            BEGIN
                UPDATE transactions SET account=NEW.name WHERE account_id=NEW.stable_id;
                UPDATE subscriptions SET account=NEW.name WHERE account_id=NEW.stable_id;
                UPDATE scheduled_payments SET account=NEW.name WHERE account_id=NEW.stable_id;
                UPDATE holdings SET linked_account=NEW.name
                    WHERE linked_account_id=NEW.stable_id;
                UPDATE liabilities SET account=NEW.name
                    WHERE account_id=NEW.stable_id;
            END;

            DROP TRIGGER IF EXISTS trg_categories_cleanup_before_delete;
            CREATE TRIGGER trg_categories_cleanup_before_delete
            BEFORE DELETE ON categories
            BEGIN
                SELECT CASE
                    WHEN EXISTS (
                        SELECT 1 FROM transactions
                        WHERE (category_id <> '' AND category_id = OLD.stable_id)
                           OR category = OLD.name
                        LIMIT 1
                    )
                    OR EXISTS (
                        SELECT 1 FROM transaction_splits
                        WHERE (category_id <> '' AND category_id = OLD.stable_id)
                           OR category = OLD.name
                        LIMIT 1
                    )
                    THEN RAISE(ABORT, 'Não é possível eliminar uma categoria com transações associadas. Utiliza a fusão de categorias.')
                END;
                DELETE FROM category_rules
                WHERE (category_id <> '' AND category_id = OLD.stable_id)
                   OR category = OLD.name;
                UPDATE monthly_goals
                SET category='Diversos', category_id=''
                WHERE (category_id <> '' AND category_id = OLD.stable_id)
                   OR category = OLD.name;
            END;

            DROP TRIGGER IF EXISTS trg_financial_accounts_prevent_delete_with_txs;
            CREATE TRIGGER trg_financial_accounts_prevent_delete_with_txs
            BEFORE DELETE ON financial_accounts
            BEGIN
                SELECT CASE
                    WHEN EXISTS (
                        SELECT 1 FROM transactions
                        WHERE (account_id <> '' AND account_id = OLD.stable_id)
                           OR account = OLD.name COLLATE NOCASE
                        LIMIT 1
                    )
                    THEN RAISE(ABORT, 'Não é possível eliminar uma conta com transações associadas. Usa a fusão de contas.')
                END;
                -- BUG-AUD-C131: a conta sem movimentos mas com posições
                -- (holdings) ou dívidas (liabilities) ligadas não pode ser
                -- eliminada; ficariam órfãos sem conta dona.
                SELECT CASE
                    WHEN EXISTS (
                        SELECT 1 FROM holdings
                        WHERE (linked_account_id <> '' AND linked_account_id = OLD.stable_id)
                           OR linked_account = OLD.name COLLATE NOCASE
                        LIMIT 1
                    )
                    THEN RAISE(ABORT, 'Não é possível eliminar uma conta com ativos associados. Usa a fusão de contas.')
                END;
                SELECT CASE
                    WHEN EXISTS (
                        SELECT 1 FROM liabilities
                        WHERE (account_id <> '' AND account_id = OLD.stable_id)
                           OR account = OLD.name COLLATE NOCASE
                        LIMIT 1
                    )
                    THEN RAISE(ABORT, 'Não é possível eliminar uma conta com dívidas associadas. Usa a fusão de contas.')
                END;
            END;

            DROP TRIGGER IF EXISTS trg_transactions_cascade_transfer_leg;
            CREATE TRIGGER trg_transactions_cascade_transfer_leg
            AFTER DELETE ON transactions
            FOR EACH ROW
            WHEN OLD.transfer_group_id IS NOT NULL AND OLD.transfer_group_id <> ''
            BEGIN
                -- BUG-DB-003: cascading transfer-leg deletes must keep
                -- goals.saved_amount synchronised.  Sign convention matches
                -- adjust_goal_with_transaction/_delete_transactions:
                -- ``saved_amount + amount`` (deposit leg is negative),
                -- floored at zero.  (1) compensates the explicitly deleted
                -- leg itself; (2) compensates goal-linked sibling legs that
                -- statement (3) is about to cascade-delete -- they can never
                -- fire triggers themselves (recursive_triggers=OFF).
                UPDATE goals
                SET saved_amount = MAX(0.0, saved_amount + OLD.amount)
                WHERE OLD.goal_id IS NOT NULL AND id = OLD.goal_id;
                UPDATE goals
                SET saved_amount = MAX(0.0, saved_amount + COALESCE((
                        SELECT SUM(t.amount) FROM transactions t
                        WHERE t.transfer_group_id = OLD.transfer_group_id
                          AND t.id <> OLD.id AND t.goal_id = goals.id
                    ), 0.0))
                WHERE id IN (SELECT goal_id FROM transactions
                             WHERE transfer_group_id = OLD.transfer_group_id
                               AND id <> OLD.id AND goal_id IS NOT NULL);
                DELETE FROM transaction_splits
                WHERE transaction_id IN (
                    SELECT id FROM transactions
                    WHERE transfer_group_id = OLD.transfer_group_id AND id <> OLD.id
                );
                DELETE FROM transaction_attachments
                WHERE transaction_id IN (
                    SELECT id FROM transactions
                    WHERE transfer_group_id = OLD.transfer_group_id AND id <> OLD.id
                );
                DELETE FROM transaction_meta
                WHERE transaction_id IN (
                    SELECT id FROM transactions
                    WHERE transfer_group_id = OLD.transfer_group_id AND id <> OLD.id
                );
                DELETE FROM transactions
                WHERE transfer_group_id = OLD.transfer_group_id
                  AND id <> OLD.id;
            END;
            """
        )

    def _migration_stable_financial_entity_identity(self) -> None:
        """Introduce stable IDs and a non-destructive merge audit trail."""

        self._ensure_stable_financial_entity_identity()

    def _migration_canonical_account_identity(self) -> None:
        """Merge cosmetic account-name variants and keep one stable identity."""

        from services.account_identity import account_identity_key, clean_account_name

        columns = {
            str(row["name"])
            for row in self.query("PRAGMA table_info(financial_accounts)")
        }
        if "account_key" not in columns:
            self.conn.execute(
                "ALTER TABLE financial_accounts "
                "ADD COLUMN account_key TEXT NOT NULL DEFAULT ''"
            )

        registry_rows = list(
            self.conn.execute(
                "SELECT id,name,reference_balance,reference_date,balance_known,updated_at "
                "FROM financial_accounts ORDER BY id"
            ).fetchall()
        )
        names: list[str] = [str(row["name"] or "") for row in registry_rows]
        for table, column in (
            ("transactions", "account"),
            ("subscriptions", "account"),
            ("scheduled_payments", "account"),
            ("holdings", "linked_account"),
        ):
            table_names = {
                str(row["name"]) for row in self.query(f"PRAGMA table_info({table})")
            }
            if column not in table_names:
                continue
            names.extend(
                str(row[0] or "")
                for row in self.conn.execute(
                    f"SELECT DISTINCT {column} FROM {table}"
                ).fetchall()
                if str(row[0] or "").strip()
            )

        canonical_by_key: dict[str, str] = {}
        for name in names:
            cleaned = clean_account_name(name)
            canonical_by_key.setdefault(account_identity_key(cleaned), cleaned)

        # Keep the oldest registry spelling, except for the built-in account,
        # and retain the newest known reference balance among merged variants.
        grouped: dict[str, list[object]] = {}
        for row in registry_rows:
            grouped.setdefault(account_identity_key(row["name"]), []).append(row)
        for key, rows in grouped.items():
            canonical = clean_account_name(rows[0]["name"])
            canonical_by_key[key] = canonical
            known = [row for row in rows if int(row["balance_known"] or 0)]
            source = max(
                known or rows,
                key=lambda row: (str(row["updated_at"] or ""), int(row["id"])),
            )
            keep_id = int(rows[0]["id"])
            duplicate_ids = [int(row["id"]) for row in rows[1:]]
            if duplicate_ids:
                marks = ",".join("?" for _ in duplicate_ids)
                self.conn.execute(
                    f"DELETE FROM financial_accounts WHERE id IN ({marks})",
                    tuple(duplicate_ids),
                )
            self.conn.execute(
                "UPDATE financial_accounts SET name=?,account_key=?,"
                "reference_balance=?,reference_date=?,balance_known=?,updated_at=? "
                "WHERE id=?",
                (
                    canonical,
                    key,
                    float(source["reference_balance"] or 0),
                    str(source["reference_date"] or ""),
                    int(source["balance_known"] or 0),
                    str(source["updated_at"] or ""),
                    keep_id,
                ),
            )

        for key, canonical in canonical_by_key.items():
            existing = self.conn.execute(
                "SELECT id FROM financial_accounts WHERE account_key=?",
                (key,),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO financial_accounts(name,account_key) VALUES(?,?)",
                    (canonical, key),
                )

        for table, column in (
            ("transactions", "account"),
            ("subscriptions", "account"),
            ("scheduled_payments", "account"),
            ("holdings", "linked_account"),
        ):
            table_names = {
                str(row["name"]) for row in self.query(f"PRAGMA table_info({table})")
            }
            if column not in table_names:
                continue
            for old in names:
                if not str(old or "").strip() and column == "linked_account":
                    continue
                key = account_identity_key(old)
                canonical = canonical_by_key.get(key, clean_account_name(old))
                self.conn.execute(
                    f"UPDATE {table} SET {column}=? "
                    f"WHERE LOWER(TRIM({column}))=LOWER(TRIM(?))",
                    (canonical, str(old)),
                )

        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_accounts_account_key "
            "ON financial_accounts(account_key)"
        )

    def _migration_literal_transaction_search(self) -> None:
        """Build a trigram FTS index kept in sync with movements and metadata.

        Trigrams preserve substring search while avoiding the full-table scan of
        eight ``LIKE '%term%'`` expressions.  The UI still uses a literal
        ``INSTR`` fallback for one- and two-character searches, which FTS5
        trigrams cannot index.
        """

        self._migration_subcategories()
        self.run_schema_script(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS transaction_search_fts USING fts5(

                search_text,
                tokenize='trigram'
            );

            DELETE FROM transaction_search_fts;
            INSERT INTO transaction_search_fts(rowid, search_text)
            SELECT t.id,
                   TRIM(
                       COALESCE(t.description,'') || ' ' ||
                       COALESCE(t.category,'') || ' ' ||
                       COALESCE(t.subcategory,'') || ' ' ||
                       COALESCE(t.account,'') || ' ' ||
                       COALESCE(t.notes,'') || ' ' ||
                       COALESCE(m.external_reference,'') || ' ' ||
                       COALESCE(m.tags,'') || ' ' ||
                       COALESCE(m.merchant,'') || ' ' ||
                       COALESCE(m.subcategory,'') || ' ' ||
                       COALESCE(m.payment_method,'')
                   )
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id;

            DROP TRIGGER IF EXISTS trg_transaction_search_insert;
            DROP TRIGGER IF EXISTS trg_transaction_search_update;
            DROP TRIGGER IF EXISTS trg_transaction_search_delete;
            DROP TRIGGER IF EXISTS trg_transaction_meta_search_insert;
            DROP TRIGGER IF EXISTS trg_transaction_meta_search_update;
            DROP TRIGGER IF EXISTS trg_transaction_meta_search_delete;

            CREATE TRIGGER trg_transaction_search_insert
            AFTER INSERT ON transactions
            BEGIN
                -- BUG-AUD-C124: never INSERT OR REPLACE.  REPLACE would
                -- silently overwrite a pre-existing rowid without removing
                -- its old terms, leaving ghost words searchable forever.
                -- The defensive pre-DELETE also keeps the trigger safe when
                -- sibling AFTER INSERT triggers (e.g. the identity trigger)
                -- fire UPDATE statements that re-enter trg_transaction_search
                -- _update within the same outer INSERT statement: SQLite
                -- fires same-statement triggers in creation order, so the
                -- update-trigger FTS insert can run before this insert
                -- trigger and a bare INSERT would raise a UNIQUE constraint
                -- failure on the shared rowid.
                DELETE FROM transaction_search_fts WHERE rowid=NEW.id;
                INSERT INTO transaction_search_fts(rowid, search_text)
                VALUES(
                    NEW.id,
                    TRIM(
                        COALESCE(NEW.description,'') || ' ' ||
                        COALESCE(NEW.category,'') || ' ' ||
                        COALESCE(NEW.subcategory,'') || ' ' ||
                        COALESCE(NEW.account,'') || ' ' ||
                        COALESCE(NEW.notes,'')
                    )
                );
            END;

            CREATE TRIGGER trg_transaction_search_update
            AFTER UPDATE OF description, category, subcategory, account, notes
            ON transactions
            BEGIN
                -- BUG-AUD-C124: DELETE-then-INSERT (never OR REPLACE) keeps
                -- the inverted index honest and makes the trigger idempotent
                -- even when re-entered from a sibling trigger within the same
                -- outer INSERT statement.
                DELETE FROM transaction_search_fts WHERE rowid=NEW.id;
                INSERT INTO transaction_search_fts(rowid, search_text)
                SELECT NEW.id,
                       TRIM(
                           COALESCE(NEW.description,'') || ' ' ||
                           COALESCE(NEW.category,'') || ' ' ||
                           COALESCE(NEW.subcategory,'') || ' ' ||
                           COALESCE(NEW.account,'') || ' ' ||
                           COALESCE(NEW.notes,'') || ' ' ||
                           COALESCE(m.external_reference,'') || ' ' ||
                           COALESCE(m.tags,'') || ' ' ||
                           COALESCE(m.merchant,'') || ' ' ||
                           COALESCE(m.subcategory,'') || ' ' ||
                           COALESCE(m.payment_method,'')
                       )
                FROM (SELECT 1) AS seed
                LEFT JOIN transaction_meta m ON m.transaction_id=NEW.id;
            END;

            CREATE TRIGGER trg_transaction_search_delete
            AFTER DELETE ON transactions
            BEGIN
                DELETE FROM transaction_search_fts WHERE rowid=OLD.id;
            END;

            CREATE TRIGGER trg_transaction_meta_search_insert
            AFTER INSERT ON transaction_meta
            BEGIN
                DELETE FROM transaction_search_fts WHERE rowid=NEW.transaction_id;
                INSERT INTO transaction_search_fts(rowid, search_text)
                SELECT t.id,
                       TRIM(
                           COALESCE(t.description,'') || ' ' ||
                           COALESCE(t.category,'') || ' ' ||
                           COALESCE(t.subcategory,'') || ' ' ||
                           COALESCE(t.account,'') || ' ' ||
                           COALESCE(t.notes,'') || ' ' ||
                           COALESCE(NEW.external_reference,'') || ' ' ||
                           COALESCE(NEW.tags,'') || ' ' ||
                           COALESCE(NEW.merchant,'') || ' ' ||
                           COALESCE(NEW.subcategory,'') || ' ' ||
                           COALESCE(NEW.payment_method,'')
                       )
                FROM transactions t WHERE t.id=NEW.transaction_id;
            END;

            CREATE TRIGGER trg_transaction_meta_search_update
            AFTER UPDATE OF tags, merchant, subcategory, payment_method, external_reference
            ON transaction_meta
            BEGIN
                DELETE FROM transaction_search_fts WHERE rowid=NEW.transaction_id;
                INSERT INTO transaction_search_fts(rowid, search_text)
                SELECT t.id,
                       TRIM(
                           COALESCE(t.description,'') || ' ' ||
                           COALESCE(t.category,'') || ' ' ||
                           COALESCE(t.subcategory,'') || ' ' ||
                           COALESCE(t.account,'') || ' ' ||
                           COALESCE(t.notes,'') || ' ' ||
                           COALESCE(NEW.external_reference,'') || ' ' ||
                           COALESCE(NEW.tags,'') || ' ' ||
                           COALESCE(NEW.merchant,'') || ' ' ||
                           COALESCE(NEW.subcategory,'') || ' ' ||
                           COALESCE(NEW.payment_method,'')
                       )
                FROM transactions t WHERE t.id=NEW.transaction_id;
            END;

            CREATE TRIGGER trg_transaction_meta_search_delete
            AFTER DELETE ON transaction_meta
            BEGIN
                DELETE FROM transaction_search_fts WHERE rowid=OLD.transaction_id;
                INSERT INTO transaction_search_fts(rowid, search_text)
                SELECT t.id,
                       TRIM(
                           COALESCE(t.description,'') || ' ' ||
                           COALESCE(t.category,'') || ' ' ||
                           COALESCE(t.subcategory,'') || ' ' ||
                           COALESCE(t.account,'') || ' ' ||
                           COALESCE(t.notes,'')
                       )
                FROM transactions t WHERE t.id=OLD.transaction_id;
            END;
            """
        )

    def _migration_high_throughput_covering_indexes(self) -> None:
        """Ensure covering and performance indexes exist on core financial tables."""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_transactions_date_amount_cat ON transactions(date, amount, category)",
            "CREATE INDEX IF NOT EXISTS idx_transactions_date_account_amt ON transactions(date, account, amount)",
            "CREATE INDEX IF NOT EXISTS idx_transactions_category_date ON transactions(category, date)",
            "CREATE INDEX IF NOT EXISTS idx_transaction_meta_payment_date ON transaction_meta(payment_date)",
            "CREATE INDEX IF NOT EXISTS idx_transaction_splits_tx_cat ON transaction_splits(transaction_id, category)",
            "CREATE INDEX IF NOT EXISTS idx_subscription_occurrences_due ON subscription_occurrences(subscription_id, due_date)",
        ]
        for sql in indexes:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                if self.schema_version() >= self.CURRENT_SCHEMA_VERSION:
                    raise

    def _migration_remove_obsolete_transactions_fts(self) -> None:
        """Remove the superseded external-content FTS index safely.

        Early versions kept ``transactions_fts`` synchronized through three
        generic trigger names.  The application has used the independent
        ``transaction_search_fts`` index since migration 22, so the old index
        is derived, unused data.  A damaged legacy index can nevertheless make
        every insert, update or delete on ``transactions`` fail because its
        triggers still run.  Drop the triggers first and then the obsolete
        virtual table in the same transaction; no financial row is rewritten.
        """

        for trigger_name in (
            "transactions_ai",
            "transactions_ad",
            "transactions_au",
        ):
            self.conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
        self.conn.execute('DROP TABLE IF EXISTS "transactions_fts"')

    def _migration_goal_assets_and_portable_attachments(self) -> None:
        """Link goal transfers and make managed document paths portable."""

        transaction_columns = {
            str(row["name"]) for row in self.query("PRAGMA table_info(transactions)")
        }
        if "goal_id" not in transaction_columns:
            self.conn.execute("ALTER TABLE transactions ADD COLUMN goal_id INTEGER")
        if "goal_name_snapshot" not in transaction_columns:
            self.conn.execute(
                "ALTER TABLE transactions ADD COLUMN "
                "goal_name_snapshot TEXT NOT NULL DEFAULT ''"
            )

        self.conn.execute(
            """
            UPDATE transactions
            SET goal_name_snapshot=TRIM(COALESCE(
                NULLIF(subcategory,''),
                (SELECT NULLIF(TRIM(m.subcategory),'')
                 FROM transaction_meta m WHERE m.transaction_id=transactions.id),
                CASE
                    WHEN description LIKE 'Reforço do pote: %'
                    THEN SUBSTR(description, LENGTH('Reforço do pote: ') + 1)
                    WHEN description LIKE 'Levantamento do pote: %'
                    THEN SUBSTR(description, LENGTH('Levantamento do pote: ') + 1)
                    ELSE ''
                END
            ))
            WHERE category='Objetivos e Poupança'
              AND TRIM(COALESCE(goal_name_snapshot,''))=''
            """
        )
        # Historical movements are linked only when the visible goal name has
        # one unambiguous match.  Ambiguous/deleted goals retain their snapshot
        # without inventing a relationship.
        self.conn.execute(
            """
            UPDATE transactions
            SET goal_id=(
                SELECT MIN(g.id) FROM goals g
                WHERE LOWER(TRIM(g.name))=LOWER(TRIM(transactions.goal_name_snapshot))
            )
            WHERE category='Objetivos e Poupança'
              AND goal_id IS NULL
              AND TRIM(COALESCE(goal_name_snapshot,''))<>''
              AND (
                  SELECT COUNT(*) FROM goals g
                  WHERE LOWER(TRIM(g.name))=LOWER(TRIM(transactions.goal_name_snapshot))
              )=1
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_goal_id "
            "ON transactions(goal_id) WHERE goal_id IS NOT NULL"
        )

        snapshot_columns = {
            str(row["name"])
            for row in self.query("PRAGMA table_info(financial_state_snapshots)")
        }
        if "goals_json" not in snapshot_columns:
            # NULL deliberately means that an older snapshot has no trustworthy
            # historical goal state; new snapshots always write a JSON list.
            self.conn.execute(
                "ALTER TABLE financial_state_snapshots ADD COLUMN goals_json TEXT"
            )

        from services.documents import normalise_attachment_storage

        report = normalise_attachment_storage(
            self.conn,
            self.attachments_directory,
            require_all=False,
            rewrite=True,
        )
        if report["missing_ids"] or report["outside_ids"] or report["altered_ids"]:
            log_warning(
                "Portable attachment migration found unavailable records: "
                "missing=%s outside=%s altered=%s",
                len(report["missing_ids"]),
                len(report["outside_ids"]),
                len(report["altered_ids"]),
            )
        self._migration_value_constraints()

    def _migration_holding_currency_and_exchange_rate(self) -> None:
        """Add currency and exchange_rate to holdings table with safe defaults."""

        columns = {str(row["name"]) for row in self.query("PRAGMA table_info(holdings)")}
        if "currency" not in columns:
            try:
                self.conn.execute(
                    "ALTER TABLE holdings ADD COLUMN currency TEXT NOT NULL DEFAULT 'EUR'"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).casefold():
                    raise
        if "exchange_rate" not in columns:
            try:
                self.conn.execute(
                    "ALTER TABLE holdings ADD COLUMN exchange_rate REAL NOT NULL DEFAULT 1.0"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).casefold():
                    raise
        self._migration_value_constraints()

    def _migration_transactions_cascade_transfer_leg_trigger(self) -> None:
        """Add trigger ensuring bilateral cascade deletion and integrity on transfer_group_id."""
        self._recreate_financial_identity_triggers()

    def _migration_holding_acquisition_metadata(self) -> None:
        """BUG-DLG-06: add acquisition_date and jurisdiction to holdings (crypto >365d exemption)."""
        columns = {str(row["name"]) for row in self.query("PRAGMA table_info(holdings)")}
        if "acquisition_date" not in columns:
            try:
                self.conn.execute(
                    "ALTER TABLE holdings ADD COLUMN acquisition_date TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).casefold():
                    raise
        if "jurisdiction" not in columns:
            try:
                self.conn.execute(
                    "ALTER TABLE holdings ADD COLUMN jurisdiction TEXT NOT NULL DEFAULT 'PT'"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).casefold():
                    raise

    def _migration_monthly_category_budgets_update_cascade(self) -> None:
        """BUG-AUD-C126: recreate ``monthly_category_budgets`` with the FK on
        ``ON UPDATE CASCADE``.

        The original table declared
        ``FOREIGN KEY(category) REFERENCES categories(name) ON DELETE CASCADE``
        without ``ON UPDATE CASCADE``.  Renaming a category in Definições
        therefore failed with ``sqlite3.IntegrityError: FOREIGN KEY constraint
        failed``.  SQLite cannot alter a foreign key in place; the canonical
        zero-loss recipe is a full recreate inside the caller's migration
        transaction: build the replacement table with the corrected clause,
        copy every row, drop the legacy table and rename the replacement.
        """

        schema_row = self.conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='monthly_category_budgets'"
        ).fetchone()
        current_sql = str(schema_row["sql"] or "") if schema_row is not None else ""
        if "ON UPDATE CASCADE" in current_sql.upper():
            return  # already migrated; idempotent re-run

        # The table must exist at all before it can be recreated.
        self._ensure_monthly_category_budget_table()
        schema_row = self.conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='monthly_category_budgets'"
        ).fetchone()
        current_sql = str(schema_row["sql"] or "") if schema_row is not None else ""
        if "ON UPDATE CASCADE" in current_sql.upper():
            return  # _ensure_ created it with the corrected clause

        # Drop every trigger/index referencing the table before the swap.
        # trg_categories_display_name rewrites the display label on rename and
        # would fail with "no such table" against the mid-swap absence.
        self.conn.execute(
            "DROP TRIGGER IF EXISTS trg_monthly_budgets_category_identity_insert"
        )
        self.conn.execute(
            "DROP TRIGGER IF EXISTS trg_monthly_budgets_category_identity_update"
        )
        self.conn.execute(
            "DROP TRIGGER IF EXISTS trg_categories_display_name"
        )
        self.conn.execute(
            "DROP INDEX IF EXISTS idx_monthly_category_budgets_month"
        )
        self.conn.execute(
            "DROP INDEX IF EXISTS idx_monthly_category_budgets_cat_id_month"
        )

        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute(
                """
                CREATE TABLE monthly_category_budgets_new (
                    month TEXT NOT NULL,
                    category TEXT NOT NULL,
                    budget REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    category_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(month, category),
                    FOREIGN KEY(category) REFERENCES categories(name)
                        ON DELETE CASCADE ON UPDATE CASCADE
                )
                """
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO monthly_category_budgets_new(
                    month,category,budget,updated_at,category_id
                )
                SELECT month,category,budget,updated_at,category_id
                FROM monthly_category_budgets
                """
            )
            self.conn.execute("DROP TABLE monthly_category_budgets")
            self.conn.execute(
                "ALTER TABLE monthly_category_budgets_new "
                "RENAME TO monthly_category_budgets"
            )
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_monthly_category_budgets_month "
            "ON monthly_category_budgets(month)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_monthly_category_budgets_cat_id_month "
            "ON monthly_category_budgets(category_id, month)"
        )
        # Restore the identity triggers that were dropped for the swap.
        self._recreate_financial_identity_triggers()
        self._migration_value_constraints()


    def ensure_performance_indexes(self) -> None:
        """Create high-throughput composite covering and foreign key indexes."""
        self.run_schema_script(
            """
            CREATE INDEX IF NOT EXISTS idx_transactions_date_amount_cat
                ON transactions(date, amount, category_id, category);
            CREATE INDEX IF NOT EXISTS idx_transactions_date_kind_cat_amt
                ON transactions(date, kind, category, amount);
            CREATE INDEX IF NOT EXISTS idx_transactions_date_account_amt
                ON transactions(date, account, amount);
            CREATE INDEX IF NOT EXISTS idx_holdings_asset_class_name
                ON holdings(asset_class, name);
            CREATE INDEX IF NOT EXISTS idx_holdings_asset_class_name_covering
                ON holdings(asset_class, name, id, value, cost_basis);
            CREATE INDEX IF NOT EXISTS idx_subscription_occurrences_tx_id
                ON subscription_occurrences(transaction_id) WHERE transaction_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_scheduled_payment_occurrences_tx_id
                ON scheduled_payment_occurrences(transaction_id) WHERE transaction_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_transaction_splits_tx_id
                ON transaction_splits(transaction_id);
            CREATE INDEX IF NOT EXISTS idx_transaction_splits_tx_amount
                ON transaction_splits(transaction_id, amount);
            CREATE INDEX IF NOT EXISTS idx_transaction_splits_covering
                ON transaction_splits(transaction_id, category_id, amount, category);
            CREATE INDEX IF NOT EXISTS idx_transaction_splits_cat_amount_tx
                ON transaction_splits(category, amount, transaction_id);
            CREATE INDEX IF NOT EXISTS idx_transaction_splits_cat_id_amount_tx
                ON transaction_splits(category_id, amount, transaction_id);
            CREATE INDEX IF NOT EXISTS idx_transaction_meta_payment_date
                ON transaction_meta(payment_date, external_reference);
            CREATE INDEX IF NOT EXISTS idx_categories_kind_active_name
                ON categories(kind, is_active, name);
            CREATE INDEX IF NOT EXISTS idx_transactions_account_id_date
                ON transactions(account_id, date);
            CREATE INDEX IF NOT EXISTS idx_transactions_category_id_date
                ON transactions(category_id, date);
            CREATE INDEX IF NOT EXISTS idx_transactions_account_id_date_amount
                ON transactions(account_id, date, amount);
            CREATE INDEX IF NOT EXISTS idx_monthly_category_budgets_cat_id_month
                ON monthly_category_budgets(category_id, month);
            CREATE INDEX IF NOT EXISTS idx_transactions_meta_tx_paydate
                ON transaction_meta(transaction_id, payment_date);
            CREATE INDEX IF NOT EXISTS idx_transactions_date_id_desc
                ON transactions(date DESC, id DESC);
            """
        )

    def _migration_high_volume_query_indexes(self) -> None:
        """Index the tables that grow continuously as the app is used."""
        self.ensure_document_tables()
        self.ensure_analytics_tables()
        self.run_schema_script(
            """
            CREATE INDEX IF NOT EXISTS idx_transaction_attachments_created_id
                ON transaction_attachments(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_chat_history_created_id
                ON ai_chat_history(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_chat_history_month_created_id
                ON ai_chat_history(month, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_scheduled_payment_occurrences_due_id
                ON scheduled_payment_occurrences(due_date, id);
            CREATE INDEX IF NOT EXISTS idx_transactions_account_date
                ON transactions(account, date);
            CREATE INDEX IF NOT EXISTS idx_transactions_category_date
                ON transactions(category, date);
            CREATE INDEX IF NOT EXISTS idx_transaction_splits_category_tx
                ON transaction_splits(category, transaction_id);
            """
        )

    def _migration_habit_change_plans(self) -> None:
        """Persist non-destructive habit plans and their matching assumptions."""
        self.run_schema_script(
            """
            CREATE TABLE IF NOT EXISTS habit_change_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                preset_key TEXT NOT NULL,
                habit_name TEXT NOT NULL,
                scenario_key TEXT NOT NULL,
                baseline_unit_cost REAL NOT NULL DEFAULT 0,
                baseline_frequency REAL NOT NULL DEFAULT 0,
                frequency_unit TEXT NOT NULL DEFAULT 'month',
                alternative_unit_cost REAL NOT NULL DEFAULT 0,
                target_frequency REAL NOT NULL DEFAULT 0,
                baseline_monthly_cost REAL NOT NULL DEFAULT 0,
                planned_monthly_cost REAL NOT NULL DEFAULT 0,
                planned_monthly_savings REAL NOT NULL DEFAULT 0,
                goal_id INTEGER,
                start_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                audit_rules_json TEXT NOT NULL DEFAULT '{}',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_habit_change_plans_status_start
                ON habit_change_plans(status, start_date, id);
            CREATE INDEX IF NOT EXISTS idx_habit_change_plans_preset
                ON habit_change_plans(preset_key, id);
            """
        )

    def _migration_change_journal(self) -> None:
        """Store reversible user actions without changing financial records."""

        self.run_schema_script(
            """
            CREATE TABLE IF NOT EXISTS change_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                action_type TEXT NOT NULL,
                label TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'available',
                undone_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_change_journal_status_id
                ON change_journal(status, id DESC);
            """
        )

    def _ensure_monthly_category_budget_table(self) -> None:
        """Create month-specific category limits without rewriting legacy limits."""
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monthly_category_budgets (
                month TEXT NOT NULL,
                category TEXT NOT NULL,
                budget REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(month, category),
                FOREIGN KEY(category) REFERENCES categories(name) ON DELETE CASCADE
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_monthly_category_budgets_month "
            "ON monthly_category_budgets(month)"
        )

    def ensure_reference_data(self) -> None:
        """Add new categories and rules without changing existing user data."""
        from services.category_taxonomy import category_identity_key
        extra_categories = [
            Category("Higiene", "expense", "#86C7A0", 60),
            Category("Desporto", "expense", "#4CB870", 80),
            Category("Educação", "expense", "#88C7FF", 80),
            Category("Compras grandes", "expense", "#D9E2D7", 250),
            Category("Animais", "expense", "#B7DDF8", 60),
            Category("Investimentos", "expense", "#4F46E5", 0),
        ]
        for c in extra_categories:
            exists = self.conn.execute(
                "SELECT 1 FROM categories WHERE name=?", (c.name,)
            ).fetchone()
            if not exists:
                key = category_identity_key(c.name)
                stable_id = f"cat_{uuid.uuid4().hex}"
                self.conn.execute(
                    "INSERT INTO categories(name,kind,colour,budget,stable_id,identity_key) VALUES(?,?,?,?,?,?)",
                    (c.name, c.kind, c.colour, c.budget, stable_id, key),
                )

        rules = [
            (
                "continente",
                "Supermercado",
                json.dumps(
                    [["Supermercado", 80], ["Higiene", 15], ["Casa e contas", 5]],
                    ensure_ascii=False,
                ),
            ),
            (
                "pingo doce",
                "Supermercado",
                json.dumps(
                    [["Supermercado", 85], ["Higiene", 10], ["Diversos", 5]],
                    ensure_ascii=False,
                ),
            ),
            (
                "lidl",
                "Supermercado",
                json.dumps([["Supermercado", 90], ["Higiene", 10]], ensure_ascii=False),
            ),
            (
                "mercadona",
                "Supermercado",
                json.dumps(
                    [["Supermercado", 80], ["Higiene", 15], ["Diversos", 5]],
                    ensure_ascii=False,
                ),
            ),
            (
                "prozis",
                "Saúde",
                json.dumps([["Saúde", 60], ["Desporto", 40]], ensure_ascii=False),
            ),
            (
                "myprotein",
                "Saúde",
                json.dumps([["Saúde", 50], ["Desporto", 50]], ensure_ascii=False),
            ),
            ("farmácia", "Saúde", json.dumps([["Saúde", 100]], ensure_ascii=False)),
            (
                "uber eats",
                "Restaurantes",
                json.dumps([["Restaurantes", 100]], ensure_ascii=False),
            ),
            (
                "glovo",
                "Restaurantes",
                json.dumps(
                    [["Restaurantes", 85], ["Diversos", 15]], ensure_ascii=False
                ),
            ),
            (
                "galp",
                "Transporte",
                json.dumps([["Transporte", 100]], ensure_ascii=False),
            ),
            ("bp", "Transporte", json.dumps([["Transporte", 100]], ensure_ascii=False)),
        ]
        self.conn.executemany(
            "INSERT OR IGNORE INTO category_rules(keyword, category, split_json) VALUES(?,?,?)",
            rules,
        )
        self.ensure_document_tables()
        self.ensure_performance_indexes()
        self.conn.commit()

    @staticmethod
    def _demo_data_requested() -> bool:
        """Keep realistic sample finances behind an explicit opt-in switch."""

        return os.environ.get("FINANCAS_PESSOAIS_DEMO_DATA", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "sim",
        }

    def seed_if_empty(self) -> None:
        """Create reference categories only; never invent financial records."""
        from services.category_taxonomy import category_identity_key

        count = self.conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()[
            "c"
        ]
        if count:
            return
        categories = [
            Category("Casa e contas", "expense", GREEN, 1200),
            Category("Supermercado", "expense", "#208A43", 550),
            Category("Subscrições", "expense", "#65C780", 95),
            Category("Saúde", "expense", "#9CE2AC", 120),
            Category("Transporte", "expense", BLUE, 180),
            Category("Lazer", "expense", "#B7DDF8", 220),
            Category("Restaurantes", "expense", "#D9E2D7", 180),
            Category("Diversos", "expense", "#AEB8A8", 150),
            Category("Rendimentos", "income", GREEN, 0),
            Category("Reembolsos", "income", GREEN_2, 0),
        ]
        for c in categories:
            key = category_identity_key(c.name)
            stable_id = f"cat_{uuid.uuid4().hex}"
            self.conn.execute(
                "INSERT INTO categories(name,kind,colour,budget,stable_id,identity_key) VALUES(?,?,?,?,?,?)",
                (c.name, c.kind, c.colour, c.budget, stable_id, key),
            )
        self.conn.commit()

    def seed_demo_data(self, *, seed: int = 20260701) -> bool:
        """Populate an explicitly requested, empty demo profile once.

        Production startup never calls this method unless the dedicated demo
        environment switch is present.  Checking every financial table makes
        the operation idempotent and prevents sample records from being mixed
        into a partially restored or existing user profile.
        """

        marker = self.conn.execute(
            "SELECT value FROM settings WHERE key='demo_data_seeded'"
        ).fetchone()
        if marker is not None and str(marker["value"] or "") == "1":
            return False
        financial_tables = ("transactions", "subscriptions", "holdings", "goals")
        counts = {
            table: int(
                self.conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()[
                    "c"
                ]
            )
            for table in financial_tables
        }
        if any(counts.values()):
            return False
        rng = random.Random(int(seed))
        self._seed_transactions(rng=rng)
        self._seed_subscriptions()
        self._seed_holdings()
        self._seed_goals()
        self.conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('demo_data_seeded','1')"
        )
        self.conn.commit()
        return True

    def _seed_transactions(self, *, rng: random.Random | None = None) -> None:
        base = date.today().replace(day=1)
        monthly_income = 2750
        generator = rng or random.Random(20260701)
        # Seed five months of deterministic demo transactions.
        for m_offset in range(4, -1, -1):
            month_start = (base - timedelta(days=31 * m_offset)).replace(day=1)
            pay_date = month_start + timedelta(days=1)
            self.add_transaction(
                pay_date.isoformat(),
                "Salário",
                monthly_income,
                "Rendimentos",
                "Conta principal",
                "income",
                commit=False,
            )
            samples = [
                ("Renda", -980, "Casa e contas", 3),
                ("Eletricidade e gás", -82, "Casa e contas", 8),
                ("Internet e telemóvel", -48, "Casa e contas", 10),
                ("Passe / combustível", -95, "Transporte", 11),
                ("Farmácia", -28, "Saúde", 16),
                ("Jantar fora", -42, "Restaurantes", 18),
                ("Cinema", -19, "Lazer", 22),
            ]
            for desc, amount, cat, day_num in samples:
                d = month_start + timedelta(days=min(day_num, 25) - 1)
                self.add_transaction(
                    d.isoformat(),
                    desc,
                    amount,
                    cat,
                    "Conta principal",
                    "expense",
                    commit=False,
                )
            for i in range(8):
                d = month_start + timedelta(days=generator.randint(2, 27))
                amount = -round(generator.uniform(19, 95), 2)
                self.add_transaction(
                    d.isoformat(),
                    "Supermercado",
                    amount,
                    "Supermercado",
                    "Conta principal",
                    "expense",
                    commit=False,
                )
            for i in range(2):
                d = month_start + timedelta(days=generator.randint(5, 25))
                amount = -round(generator.uniform(22, 75), 2)
                self.add_transaction(
                    d.isoformat(),
                    "Compra ocasional",
                    amount,
                    "Diversos",
                    "Conta principal",
                    "expense",
                    commit=False,
                )

    def _seed_subscriptions(self) -> None:
        data = [
            (
                "Streaming TV",
                12.99,
                "Subscrições",
                4,
                "Mensal",
                "Plano familiar",
                1,
                "",
            ),
            ("Ginásio", 33.00, "Subscrições", 10, "Mensal", "", 1, ""),
            (
                "Armazenamento cloud",
                2.99,
                "Subscrições",
                16,
                "Mensal",
                "",
                1,
                today_iso(),
            ),
            ("Música", 7.99, "Subscrições", 21, "Mensal", "", 1, today_iso()),
            ("Software criativo", 19.99, "Subscrições", 27, "Mensal", "", 1, ""),
        ]
        self.conn.executemany(
            "INSERT INTO subscriptions(name,amount,category,renewal_day,period,notes,active,last_used_date) VALUES(?,?,?,?,?,?,?,?)",
            data,
        )

    def _seed_holdings(self) -> None:
        data = [
            (
                "Conta à ordem",
                "Dinheiro",
                "Manual",
                6200.87,
                6200.87,
                "Sem sincronização",
            ),
            ("ETF global", "ETF", "Manual", 19131.14, 16200.00, "Registo manual"),
            (
                "Ações individuais",
                "Ações",
                "Manual",
                48512.84,
                41000.00,
                "Registo manual",
            ),
            (
                "Obrigações",
                "Obrigações",
                "Manual",
                18354.60,
                17500.00,
                "Registo manual",
            ),
            ("Cripto", "Cripto", "Manual", 10736.38, 8900.00, "Registo manual"),
        ]
        self.conn.executemany(
            "INSERT INTO holdings(name,asset_class,account,value,cost_basis,"
            "cost_basis_known,notes) VALUES(?,?,?,?,?,1,?)",
            data,
        )

    def _seed_goals(self) -> None:
        target = 160000
        self.conn.execute(
            "INSERT INTO goals(name,target_amount,saved_amount,monthly_contribution,deadline,notes) VALUES(?,?,?,?,?,?)",
            (
                "Comprar casa",
                target,
                42000,
                900,
                "",
                "Entrada, custos, pequenas obras e fundo de emergência",
            ),
        )

    def check_physical_integrity(self) -> dict[str, Any]:
        """Perform comprehensive physical and referential integrity checks on SQLite."""
        results: dict[str, Any] = {
            "integrity_ok": False,
            "quick_check_ok": False,
            "foreign_keys_ok": False,
            "errors": [],
        }
        try:
            quick_rows = [
                r[0] for r in self.conn.execute("PRAGMA quick_check").fetchall()
            ]
            results["quick_check_ok"] = quick_rows == ["ok"]
            if not results["quick_check_ok"]:
                results["errors"].extend(quick_rows)

            integrity_rows = [
                r[0] for r in self.conn.execute("PRAGMA integrity_check").fetchall()
            ]
            results["integrity_ok"] = integrity_rows == ["ok"]
            if not results["integrity_ok"]:
                results["errors"].extend(integrity_rows)

            fk_rows = self.conn.execute("PRAGMA foreign_key_check").fetchall()
            results["foreign_keys_ok"] = len(fk_rows) == 0
            if not results["foreign_keys_ok"]:
                results["errors"].append(
                    f"Foreign key violations found: {len(fk_rows)}"
                )
        except Exception as exc:
            results["errors"].append(f"Integrity check failed: {exc}")
        return results

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        start = time.perf_counter()
        rows = list(self.conn.execute(sql, params).fetchall())
        duration_ms = (time.perf_counter() - start) * 1000.0
        if duration_ms >= 250.0:
            log_warning(
                "Slow database query (%.2f ms): %s",
                duration_ms,
                " ".join(sql.split())[:180],
            )
        return rows

    def _locked_connection(self) -> "_LockedConnectionHandle":
        """BUG-DB-001: serialized access to the shared connection.

        The connection is opened with ``check_same_thread=False`` and is
        shared between the GUI thread and background workers.  Workers take
        ``_SQLITE_SHARED_CONNECTION_LOCK`` around every statement; GUI-thread
        callers that touch ``self.conn`` directly must go through this
        accessor so both sides honour the same gate.  Usage::

            with db._locked_connection() as conn:
                conn.execute(...)

        The lock is an ``RLock``, so service/repo methods that already hold
        it (or nested locked accessors) re-enter safely without deadlocking.
        """

        from ui.workers import _SQLITE_SHARED_CONNECTION_LOCK

        return _LockedConnectionHandle(_SQLITE_SHARED_CONNECTION_LOCK, self.conn)

    def query_iter(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> Iterator[sqlite3.Row]:
        """Stream query results row-by-row without buffering full result sets in RAM."""
        cursor = self.conn.execute(sql, params)
        try:
            yield from cursor
        finally:
            cursor.close()

    def query_chunks(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        chunk_size: int = 1000,
    ) -> Iterator[list[sqlite3.Row]]:
        """Yield bounded chunks to control peak memory consumption on large queries."""
        cursor = self.conn.execute(sql, params)
        try:
            while True:
                rows = cursor.fetchmany(max(1, int(chunk_size)))
                if not rows:
                    break
                yield rows
        finally:
            cursor.close()

    def scalar(
        self, sql: str, params: tuple[Any, ...] = (), default: float = 0
    ) -> float:
        row = self.conn.execute(sql, params).fetchone()
        if not row:
            return default
        val = row[0]
        return default if val is None else float(val)

    def categories(self, kind: Optional[str] = None) -> list[str]:
        if kind:
            rows = self.query(
                "SELECT name FROM categories WHERE kind=? AND is_active=1 ORDER BY name",
                (kind,),
            )
        else:
            rows = self.query(
                "SELECT name FROM categories WHERE is_active=1 ORDER BY kind, name"
            )
        return [r["name"] for r in rows]

    def add_transaction(
        self,
        tx_date: str,
        description: str,
        amount: float,
        category: str,
        account: str,
        kind: str,
        notes: str = "",
        subcategory: str = "",
        commit: bool = True,
    ) -> None:
        from services.account_identity import account_identity_key, clean_account_name

        account = clean_account_name(account)
        account_key = account_identity_key(account)
        self.conn.execute(
            "INSERT OR IGNORE INTO financial_accounts(name,account_key) VALUES(?,?)",
            (account, account_key),
        )
        account_row = self.conn.execute(
            "SELECT id,stable_id,name FROM financial_accounts "
            "WHERE name=? COLLATE NOCASE LIMIT 1",
            (account,),
        ).fetchone()
        account_id = str(account_row["stable_id"] or "") if account_row else ""
        if account_row is not None and not account_id:
            account_id = self._stable_entity_id(
                "account",
                account_key,
                int(account_row["id"] or 0),
            )
            self.conn.execute(
                "UPDATE financial_accounts SET stable_id=?,account_key=? WHERE id=?",
                (account_id, account_key, int(account_row["id"])),
            )
        category_row = self.conn.execute(
            "SELECT stable_id,name FROM categories "
            "WHERE name=? AND is_active=1 LIMIT 1",
            (str(category or "").strip(),),
        ).fetchone()
        category_id = str(category_row["stable_id"] or "") if category_row else ""
        category = str(category_row["name"] or category) if category_row else category
        self.conn.execute(
            "INSERT INTO transactions("
            "date,description,amount,category,subcategory,account,kind,notes,"
            "category_id,account_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                tx_date,
                description,
                amount,
                category,
                subcategory,
                account,
                kind,
                notes,
                category_id,
                account_id,
            ),
        )
        if commit:
            self.conn.commit()

    def account_balances(self, as_of: date | str | None = None) -> list[dict[str, Any]]:
        """Return the canonical per-account balance decomposition at ``as_of``.

        Each active account uses its confirmed reference balance plus only
        movements after the reference date.  Accounts without a usable
        reference, and movements whose account identity is missing, retain
        the full ledger history so imported legacy money never disappears.
        """
        financial_date = "COALESCE(NULLIF(m.payment_date,''),t.date)"
        reference_day = (
            as_of.isoformat() if isinstance(as_of, date) else str(as_of or "")[:10]
        )
        if not reference_day:
            reference_day = date.today().isoformat()
        try:
            reference_day = date.fromisoformat(reference_day).isoformat()
        except ValueError as exc:
            raise ValueError(
                "A data do saldo tem de estar no formato AAAA-MM-DD."
            ) from exc

        # Scan the ledger once, retaining the original ID-first/name-fallback
        # matching and the forward/backward reference-date decomposition.
        active_accounts = self.query(
            "SELECT id,stable_id,name,reference_balance,reference_date,balance_known "
            "FROM financial_accounts WHERE is_active=1"
        )
        ledger = self.query(
            f"""
            WITH resolved AS (
              SELECT COALESCE(by_id.id,by_name.id) AS matched_id,
                     COALESCE(by_id.reference_date,by_name.reference_date,'') AS reference_date,
                     COALESCE(by_id.balance_known,by_name.balance_known,0) AS balance_known,
                     COALESCE(t.account_id,'') AS account_id,
                     COALESCE(NULLIF(TRIM(t.account),''),'Conta principal') AS account,
                     t.amount AS amount,{financial_date} AS financial_day
              FROM transactions t
              LEFT JOIN transaction_meta m ON m.transaction_id=t.id
              LEFT JOIN financial_accounts by_id ON by_id.is_active=1
                AND by_id.stable_id=t.account_id AND by_id.stable_id<>''
              LEFT JOIN financial_accounts by_name ON by_name.is_active=1
                AND by_name.name=CASE WHEN COALESCE(t.account_id,'')=''
                  THEN COALESCE(NULLIF(TRIM(t.account),''),'Conta principal') END COLLATE NOCASE
            )
            SELECT matched_id,
                   CASE WHEN matched_id IS NULL THEN account_id ELSE '' END AS orphan_id,
                   CASE WHEN matched_id IS NULL THEN account ELSE '' END AS orphan_name,
                   SUM(CASE WHEN balance_known<>0 AND SUBSTR(reference_date,1,10)>:day
                     THEN -amount ELSE amount END) AS movements
            FROM resolved
            WHERE (
              (balance_known<>0 AND SUBSTR(reference_date,1,10)<>'' AND (
                (SUBSTR(reference_date,1,10)<=:day AND financial_day>SUBSTR(reference_date,1,10) AND financial_day<=:day)
                OR (SUBSTR(reference_date,1,10)>:day AND financial_day>:day AND financial_day<=SUBSTR(reference_date,1,10))
              ))
              OR ((balance_known=0 OR SUBSTR(reference_date,1,10)='') AND financial_day<=:day)
            ) AND {sql_finite_number("amount")} AND {sql_iso_date("financial_day")}
            GROUP BY matched_id,orphan_id,orphan_name
            """,
            {"day": reference_day},
        )
        movements = {row["matched_id"]: float(row["movements"] or 0) for row in ledger if row["matched_id"] is not None}
        balances: list[dict[str, Any]] = []
        for row in active_accounts:
            configured = str(row["reference_date"] or "")[:10]
            usable = bool(int(row["balance_known"] or 0)) and bool(configured)
            balance = movements.get(row["id"], 0.0)
            if usable:
                balance += float(row["reference_balance"] or 0)
            balances.append({
                "account": str(row["name"] or "Conta principal"),
                "account_id": str(row["stable_id"] or ""),
                "balance": balance,
                "balance_known": usable,
                "reference_date": configured if usable else "",
                "source": "reference_balance" if usable else "partial_history",
                "orphan": False,
            })
        balances.extend({
            "account": str(row["orphan_name"] or "Conta principal"),
            "account_id": str(row["orphan_id"] or ""),
            "balance": float(row["movements"] or 0),
            "balance_known": False,
            "reference_date": "",
            "source": "orphan_history",
            "orphan": True,
        } for row in ledger if row["matched_id"] is None)
        return balances

    def cash_balance(self, as_of: date | str | None = None) -> float:
        """Return the canonical total liquid balance at ``as_of``."""
        return sum(float(row["balance"] or 0) for row in self.account_balances(as_of))

    def net_worth(self) -> float:
        """Single canonical net worth definition for the whole app.

        Cash comes from the per-account ledger (confirmed reference balance
        plus movements after it), so profiles with pre-history opening
        balances stop being understated.  Investment rows bound to a bank
        account (``linked_account``) represent the same money as that
        account's ledger balance and would be counted twice; only manual
        holdings stay additive.  Dashboard, reports and the patrimonial
        bridge must all read patrimony through this method.
        """
        account_total = self.cash_balance()
        active_accounts = self.query(
            "SELECT stable_id,name FROM financial_accounts WHERE is_active=1"
        )
        active_account_ids = {
            str(row["stable_id"] or "") for row in active_accounts if row["stable_id"]
        }
        active_account_names = {
            str(row["name"] or "Conta principal").strip().casefold()
            for row in active_accounts
        }
        holdings_total = 0.0
        columns = {str(r["name"]) for r in self.query("PRAGMA table_info(holdings)")}
        rate_col = ",exchange_rate" if "exchange_rate" in columns else ""
        for row in self.query(
            f"SELECT value,linked_account,linked_account_id{rate_col} FROM holdings "
            f"WHERE {sql_finite_number('value', non_negative=True)}"
        ):
            linked = str(row["linked_account"] or "").strip().casefold()
            linked_id = str(row["linked_account_id"] or "").strip()
            if linked_id in active_account_ids or linked in active_account_names:
                continue
            try:
                rate_val = float(row["exchange_rate"] if "exchange_rate" in row.keys() else 1.0)
                rate = rate_val if rate_val > 0 else 1.0
            except (TypeError, ValueError, IndexError, KeyError):
                rate = 1.0
            holdings_total += float(row["value"] or 0) * rate
        liabilities_total = self.scalar(
            "SELECT COALESCE(SUM(balance),0) FROM liabilities WHERE "
            + sql_finite_number("balance")
        )
        return account_total + holdings_total - liabilities_total

    def suggest_split(
        self, description: str, amount: float = 0
    ) -> list[tuple[str, float]]:
        desc = (description or "").lower()
        for r in self.query(
            "SELECT * FROM category_rules ORDER BY LENGTH(keyword) DESC"
        ):
            kw = (r["keyword"] or "").lower().strip()
            if not kw:
                continue
            matched = (
                bool(re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", desc, re.IGNORECASE))
                if len(kw) <= 3
                else kw in desc
            )
            if matched:
                try:
                    data = json.loads(r["split_json"] or "[]")
                    if data:
                        return [
                            (str(cat), float(percent_value))
                            for cat, percent_value in data
                        ]
                except Exception:
                    return [(r["category"], 100.0)]
        # Learn from previous manual split for the same description.
        prev = self.query(
            """
            SELECT s.category, AVG(s.percent) AS percent
            FROM transaction_splits s
            JOIN transactions t ON t.id=s.transaction_id
            WHERE LOWER(TRIM(t.description)) = LOWER(TRIM(?))
            GROUP BY s.category
            ORDER BY percent DESC
            """,
            (description,),
        )
        if prev:
            total = sum(float(r["percent"]) for r in prev) or 100
            return [
                (r["category"], round(float(r["percent"]) / total * 100, 1))
                for r in prev
            ]
        cat = "Rendimentos" if amount > 0 else "Diversos"
        # Reuse simple keyword category suggestion where possible.
        desc_rules = [
            (
                [
                    "continente",
                    "pingo doce",
                    "lidl",
                    "mercadona",
                    "auchan",
                    "supermercado",
                ],
                "Supermercado",
            ),
            (
                [
                    "renda",
                    "condomínio",
                    "condominio",
                    "eletricidade",
                    "edp",
                    "água",
                    "agua",
                    "internet",
                    "meo",
                    "nos",
                    "vodafone",
                ],
                "Casa e contas",
            ),
            (
                [
                    "netflix",
                    "spotify",
                    "icloud",
                    "cloud",
                    "ginásio",
                    "ginasio",
                    "prime",
                    "hbo",
                    "disney",
                ],
                "Subscrições",
            ),
            (
                ["farmácia", "farmacia", "hospital", "clínica", "clinica", "dentista"],
                "Saúde",
            ),
            (
                [
                    "uber",
                    "bolt",
                    "cp",
                    "metro",
                    "comboio",
                    "combustível",
                    "combustivel",
                    "gasolina",
                    "gasóleo",
                    "gasoleo",
                ],
                "Transporte",
            ),
            (
                [
                    "restaurante",
                    "café",
                    "cafe",
                    "takeaway",
                    "glovo",
                    "jantar",
                    "almoço",
                    "almoco",
                ],
                "Restaurantes",
            ),
            (["cinema", "livro", "jogo", "viagem", "hotel", "lazer"], "Lazer"),
        ]
        for keywords, category in desc_rules:
            matched_any = False
            for k in keywords:
                kw = k.lower().strip()
                if not kw:
                    continue
                matched = (
                    bool(re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", desc, re.IGNORECASE))
                    if len(kw) <= 3
                    else kw in desc
                )
                if matched:
                    cat = category
                    matched_any = True
                    break
            if matched_any:
                break
        return [(cat, 100.0)]

    @staticmethod
    def _split_schema_statements(script: str) -> list[str]:
        """Split a DDL script into single statements, keeping trigger bodies.

        A naive ``;`` split would break ``CREATE TRIGGER`` because SQLite
        requires semicolons between statements inside a trigger body.  A
        character scanner tracks quoted literals and BEGIN/CASE nesting so a
        semicolon only ends a statement outside any open trigger block; the
        scanner also splits statements that share one physical line, which
        line-based scanning would miss.
        """

        statements: list[str] = []
        buffer: list[str] = []
        depth = 0
        in_string = False
        length = len(script)
        index = 0

        def token_at(position: int, word: str) -> bool:
            end = position + len(word)
            if end > length or script[position:end].upper() != word:
                return False
            before = script[position - 1] if position else " "
            after = script[end] if end < length else " "
            return not (
                before.isalnum() or before == "_" or after.isalnum() or after == "_"
            )

        while index < length:
            char = script[index]
            if in_string:
                buffer.append(char)
                if char == "'":
                    if index + 1 < length and script[index + 1] == "'":
                        buffer.append("'")
                        index += 1
                    else:
                        in_string = False
                index += 1
                continue
            if char == "'":
                in_string = True
                buffer.append(char)
                index += 1
                continue
            if depth > 0 and token_at(index, "CASE"):
                depth += 1
                buffer.append(script[index : index + 4])
                index += 4
                continue
            if depth > 0 and token_at(index, "END"):
                depth -= 1
                buffer.append(script[index : index + 3])
                index += 3
                continue
            if depth == 0 and token_at(index, "BEGIN"):
                depth += 1
                buffer.append(script[index : index + 5])
                index += 5
                continue
            if char == ";":
                # A semicolon only terminates a statement outside trigger
                # bodies; inside BEGIN...END it separates body statements.
                if depth == 0:
                    statement = "".join(buffer).strip()
                    if statement:
                        statements.append(statement + ";")
                    buffer = []
                else:
                    buffer.append(char)
                index += 1
                continue
            buffer.append(char)
            index += 1
        tail = "".join(buffer).strip()
        if tail:
            statements.append(tail + ";")
        return [s for s in statements if s and s != ";"]

    def run_schema_script(self, script: str) -> None:
        """Execute a DDL script with all-or-nothing semantics.

        ``executescript`` implicitly COMMITs any pending transaction before
        running, which allowed a partially applied schema when a later
        statement failed.  Statements run one by one inside an explicit
        transaction instead; while a migration is already running the DDL
        joins the open transaction, so the migration and its
        ``schema_migrations`` marker commit together.
        """
        statements = self._split_schema_statements(script)
        if not statements:
            return
        savepoint = f"schema_script_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            for statement in statements:
                self.conn.execute(statement)
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def backup_to(self, target: Path, max_retries: int = 5) -> None:
        """Copy the committed database state without altering this connection.

        ``Connection.backup()`` cannot stream a source connection that has an
        open write transaction (it blocks forever), and committing here would
        silently promote user changes that may still be rolled back.  The
        snapshot is therefore produced by ``VACUUM INTO`` on a separate
        read-only connection, which WAL allows to read the last committed
        state concurrently while pending changes stay pending.
        """
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target.with_suffix(f".tmp_{os.getpid()}_{uuid.uuid4().hex[:6]}")
        success = False
        try:
            for attempt in range(max_retries):
                try:
                    if tmp_target.exists():
                        tmp_target.unlink()
                    # A side read-only connection streams the last committed
                    # WAL state; using this connection as the source would
                    # deadlock while a write transaction is open, and
                    # ``VACUUM INTO`` is refused on query-only connections.
                    dst = sqlite3.connect(str(tmp_target), timeout=30.0)
                    reader = open_read_only_connection(self.path)
                    try:
                        reader.backup(dst, pages=250, sleep=0.01)
                        dst.commit()
                    finally:
                        reader.close()
                        dst.close()
                    success = True
                    break
                except sqlite3.OperationalError as exc:
                    if (
                        "locked" in str(exc).lower() or "busy" in str(exc).lower()
                    ) and attempt < max_retries - 1:
                        time.sleep(0.1 * (2**attempt))
                        continue
                    raise
        finally:
            if success and tmp_target.exists():
                tmp_target.replace(target)
            elif tmp_target.exists():
                try:
                    tmp_target.unlink()
                except OSError:
                    pass

    def integrity_inventory(self) -> dict[str, Any]:
        """Describe schema, financial and orphan issues without repairing them."""
        from database.schema_contract import (
            financial_integrity_report, inspect_schema_contract, orphan_inventory, migration_dry_run,
        )

        return {"schema": inspect_schema_contract(self.conn),
                "financial": financial_integrity_report(self.conn),
                "orphans": orphan_inventory(self.conn), "migration_plan": migration_dry_run(self.conn), "dry_run": True}

    def cleanup_orphaned_records(self, *, dry_run: bool = True) -> dict[str, int]:
        """Inventory unresolved relationships; preserve user/document evidence."""
        from database.schema_contract import orphan_inventory

        if not dry_run:
            raise ValueError(
                "A remoção automática de órfãos está desativada. "
                "É necessário rever o inventário e preparar um plano de recuperação."
            )
        return orphan_inventory(self.conn)["counts"]

    def _graceful_shutdown_checkpoint(self) -> tuple[int, int, int] | None:
        """Run the final WAL checkpoint with a graceful wait and PASSIVE fallback.

        BUG-AUD-C128: a bare ``PRAGMA wal_checkpoint(TRUNCATE)`` fails with
        ``database is locked`` whenever any other connection holds a read
        mark.  The shutdown path retries TRUNCATE within a short grace
        window (concurrent readers usually release within milliseconds) and
        finally degrades to PASSIVE, which never blocks: the -wal file may
        survive shutdown but the application still closes cleanly.
        """

        # A busy timeout would make each blocked TRUNCATE wait out its own
        # timeout (5s driver default x retries), so checkpointing runs with
        # busy_timeout=0 and the grace loop below governs the total wait.
        previous_timeout = int(self.conn.execute("PRAGMA busy_timeout").fetchone()[0])
        self.conn.execute("PRAGMA busy_timeout=0")
        try:
            deadline = time.monotonic() + 1.5
            while True:
                try:
                    row = self.conn.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                except sqlite3.OperationalError:
                    row = None
                if row is not None and int(row[0] or 0) == 0:
                    return row
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        finally:
            self.conn.execute(f"PRAGMA busy_timeout={previous_timeout}")
        # Grace window exhausted: degrade to PASSIVE so shutdown proceeds.
        try:
            return self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        except sqlite3.OperationalError:
            return None

    def prepare_for_shutdown(self) -> None:
        """Durably commit, validate, checkpoint and close the active database.

        This is deliberately stricter than relying on Python process teardown.
        A caller must treat any raised exception as a failed shutdown and keep
        the application open so the user is never told that unsaved data was
        safely stored.
        """

        if getattr(self, "_closed", False):
            return
        if getattr(self, "_read_only", False):
            if self.conn.in_transaction:
                self.conn.rollback()
            self.conn.close()
            self._closed = True
            return
        self.conn.commit()
        if self.conn.in_transaction:
            raise RuntimeError(
                "A base de dados manteve uma transação aberta depois do commit final."
            )
        # BUG-AUD-C128: wal_checkpoint(TRUNCATE) aborts with
        # "database is locked" when another connection still holds a read
        # mark (background worker, open backup snapshot, read-only viewer).
        # Retry briefly, then degrade to PASSIVE: a truncate that cannot
        # complete must never block the orderly shutdown or leave the -wal
        # file inflated.
        checkpoint = self._graceful_shutdown_checkpoint()
        if checkpoint is not None and int(checkpoint[0] or 0) != 0:
            raise RuntimeError(
                "A sincronização final da base de dados continua ocupada; "
                "o encerramento foi cancelado para proteger os dados."
            )
        try:
            check = self.conn.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.DatabaseError as exc:
            raise DataIntegrityError(
                "Não foi possível confirmar a integridade final da base de dados."
            ) from exc
        if check is None or str(check[0]).strip().casefold() != "ok":
            detail = str(check[0] if check is not None else "sem resultado")
            raise DataIntegrityError(
                f"A verificação final da base de dados não foi aprovada: {detail}"
            )
        self.conn.close()
        self._closed = True

    def close(self) -> None:
        """Public idempotent close using the protected shutdown sequence."""

        self.prepare_for_shutdown()

    def ensure_document_tables(self) -> None:
        self.run_schema_script("""
            CREATE TABLE IF NOT EXISTS transaction_meta (
                transaction_id INTEGER PRIMARY KEY,
                tags TEXT NOT NULL DEFAULT '',
                merchant TEXT NOT NULL DEFAULT '',
                payment_method TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                is_reimbursable INTEGER NOT NULL DEFAULT 0,
                external_reference TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS transaction_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL,
                file_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                original_path TEXT NOT NULL DEFAULT '',
                file_type TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                invoice_date TEXT NOT NULL DEFAULT '',
                total_amount REAL,
                content_sha256 TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                file_mtime_ns INTEGER NOT NULL DEFAULT 0,
                last_verified_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS saved_filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                filter_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_transaction_attachments_tx ON transaction_attachments(transaction_id);
            CREATE INDEX IF NOT EXISTS idx_transaction_attachments_date ON transaction_attachments(invoice_date);
            CREATE INDEX IF NOT EXISTS idx_transaction_attachments_created_id
                ON transaction_attachments(created_at DESC, id DESC);

            """)

    def ensure_analytics_tables(self) -> None:
        """Create the reporting/planning schema used by repository analytics."""
        self.ensure_document_tables()
        self.run_schema_script("""

            CREATE TABLE IF NOT EXISTS monthly_targets (
                month TEXT PRIMARY KEY,
                expense_target REAL NOT NULL DEFAULT 0,
                savings_target REAL NOT NULL DEFAULT 0,
                investment_target REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS scheduled_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Outros',
                subcategory TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL DEFAULT 0,
                account TEXT NOT NULL DEFAULT 'Conta principal',
                payment_method TEXT NOT NULL DEFAULT '',
                start_date TEXT NOT NULL,
                debit_day INTEGER NOT NULL DEFAULT 1,
                months_total INTEGER NOT NULL DEFAULT 1,
                months_paid INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS scheduled_payment_occurrences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scheduled_id INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                transaction_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scheduled_id, due_date),
                FOREIGN KEY(scheduled_id) REFERENCES scheduled_payments(id) ON DELETE CASCADE,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS monthly_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,
                name TEXT NOT NULL,
                goal_type TEXT NOT NULL DEFAULT 'Poupar',
                category TEXT NOT NULL DEFAULT '',
                target_value REAL NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ai_chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                month TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'Google Gemini',
                model TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                response TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS investment_snapshots (
                day TEXT PRIMARY KEY,
                invested REAL NOT NULL DEFAULT 0,
                total REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_ai_chat_history_created_id
                ON ai_chat_history(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_chat_history_month_created_id
                ON ai_chat_history(month, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_scheduled_payment_occurrences_due_id
                ON scheduled_payment_occurrences(due_date, id);
            """)
        self._ensure_monthly_category_budget_table()
        columns = {
            str(row["name"])
            for row in self.query("PRAGMA table_info(transaction_meta)")
        }
        additions = {
            "subcategory": "TEXT NOT NULL DEFAULT ''",
            "purchase_date": "TEXT NOT NULL DEFAULT ''",
            "payment_date": "TEXT NOT NULL DEFAULT ''",
            "payment_month": "TEXT NOT NULL DEFAULT ''",
            "external_reference": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE transaction_meta ADD COLUMN {name} {definition}"
                )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transaction_meta_payment_date "
            "ON transaction_meta(payment_date)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transaction_meta_purchase_date "
            "ON transaction_meta(purchase_date)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transaction_meta_external_reference "
            "ON transaction_meta(external_reference)"
        )

    def save_filter(self, name: str, data: dict[str, Any]) -> None:
        self.ensure_document_tables()
        self.conn.execute(
            "INSERT INTO saved_filters(name,filter_json) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET filter_json=excluded.filter_json",
            (name.strip(), json.dumps(data, ensure_ascii=False)),
        )
        self.conn.commit()

    def saved_filters(self) -> list[sqlite3.Row]:
        self.ensure_document_tables()
        return self.query("SELECT * FROM saved_filters ORDER BY name")

    def delete_saved_filter(self, name: str) -> None:
        self.ensure_document_tables()
        self.conn.execute("DELETE FROM saved_filters WHERE name=?", (name,))
        self.conn.commit()

    def export_full_zip(self, target: Path) -> None:
        target = Path(target)
        temp_db = target.parent / (
            f".temp_export_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.sqlite3"
        )
        self.backup_to(temp_db)
        data = {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "app": APP_NAME,
            "tables": {},
        }
        tables = [
            str(row["name"])
            for row in self.query(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' "
                "ORDER BY name"
            )
        ]
        for table in tables:
            try:
                data["tables"][table] = [
                    dict(r) for r in self.query(f'SELECT * FROM "{table}"')
                ]
            except sqlite3.OperationalError:
                # Optional tables may not exist in backups from older schemas.
                continue

        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(
            target,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as zf:
            zf.write(temp_db, "financas_pessoais.sqlite3")

            zf.writestr(
                "dados_exportados.json", json.dumps(data, ensure_ascii=False, indent=2)
            )
            base = self.attachments_directory
            if base.exists():
                for path in base.rglob("*"):
                    if path.is_file():
                        zf.write(path, "anexos/" + str(path.relative_to(base)))
        try:
            temp_db.unlink()
        except OSError:
            pass

    def database_storage_stats(self) -> dict[str, Any]:
        """Return storage, file sizes, and record metrics for diagnostic reporting."""
        db_path = self.path
        wal_path = Path(str(self.path) + "-wal")
        shm_path = Path(str(self.path) + "-shm")

        db_size = db_path.stat().st_size if db_path.exists() else 0
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
        shm_size = shm_path.stat().st_size if shm_path.exists() else 0

        def _count(table: str) -> int:
            try:
                return int(self.scalar(f'SELECT COUNT(*) FROM "{table}"') or 0)
            except Exception:
                return 0

        return {
            "db_size_bytes": db_size,
            "wal_size_bytes": wal_size,
            "shm_size_bytes": shm_size,
            "total_size_bytes": db_size + wal_size + shm_size,
            "freelist_count": int(self.scalar("PRAGMA freelist_count") or 0),
            "auto_vacuum_mode": int(self.scalar("PRAGMA auto_vacuum") or 0),
            "incremental_vacuum_available": int(self.scalar("PRAGMA auto_vacuum") or 0) == 2,
            "total_transactions": _count("transactions"),
            "total_splits": _count("transaction_splits"),
            "total_attachments": _count("transaction_attachments"),
            "total_snapshots": _count("financial_state_snapshots")
            + _count("monthly_snapshots"),
            "total_categories": _count("categories"),
            "total_accounts": _count("financial_accounts"),
            "schema_version": self.schema_version(),
        }

    def optimize_database(self) -> dict[str, Any]:
        """Safely compact, reindex, analyze and verify SQLite storage with zero data loss."""
        if getattr(self, "_read_only", False):
            raise PermissionError(
                "Não é possível otimizar uma base de dados aberta em modo de leitura."
            )

        start_time = time.perf_counter()
        stats_before = self.database_storage_stats()
        before_bytes = int(stats_before["total_size_bytes"])

        if self.conn.in_transaction:
            raise RuntimeError("Conclui ou cancela a transação pendente antes da manutenção.")

        # 1. Truncate WAL to write all pending pages into the main file
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

        # 2. Rebuild query optimizer statistics
        self.conn.execute("PRAGMA optimize")
        self.conn.execute("ANALYZE")

        # 3. Rebuild all indexes for optimal B-Tree performance
        self.conn.execute("REINDEX")

        # 4. Compact unused pages from disk
        try:
            self.conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            # PERS-07: In WAL mode, full VACUUM requires an EXCLUSIVE lock.
            # If concurrent readers are active, fall back gracefully to incremental vacuum.
            log_warning(f"optimize_database: VACUUM exclusivo falhou ({exc}); a recorrer a incremental_vacuum")
            try:
                self.incremental_vacuum()
            except Exception:
                pass

        # 5. Verify integrity after vacuuming
        integrity = list(self.query("PRAGMA quick_check(1)"))
        integrity_status = str(integrity[0][0]) if integrity else "ok"

        # 6. Re-ensure composite performance indexes
        self.ensure_performance_indexes()
        self.conn.commit()

        duration_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        stats_after = self.database_storage_stats()
        after_bytes = int(stats_after["total_size_bytes"])
        freed_bytes = max(0, before_bytes - after_bytes)

        return {
            "status": "ok"
            if integrity_status.lower() in ("ok", "true")
            else integrity_status,
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "freed_bytes": freed_bytes,
            "duration_ms": duration_ms,
            "stats": stats_after,
        }

    def incremental_vacuum(self, pages: int = 0) -> int:
        """Return actual freelist pages reclaimed, never committing caller work."""
        if getattr(self, "_read_only", False):
            return 0
        if self.conn.in_transaction:
            raise RuntimeError("Conclui ou cancela a transação pendente antes da manutenção.")
        if int(self.conn.execute("PRAGMA auto_vacuum").fetchone()[0]) != 2:
            return 0
        count = int(pages or 0)
        before = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        command = f"PRAGMA incremental_vacuum({count})" if count > 0 else "PRAGMA incremental_vacuum"
        # Stepping/finalizing the entire pragma is required; result tuples are
        # not a count of removed pages (and can be empty).
        cursor = self.conn.execute(command)
        try:
            cursor.fetchall()
        finally:
            cursor.close()
        after = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        return max(0, before - after)

    def enable_incremental_vacuum(self, backup_path: Path) -> dict[str, Any]:
        """Explicit legacy maintenance, with a retained backup and space gate.

        Startup never calls this conversion. The caller chooses a new backup
        destination; no existing recovery file is replaced or removed.
        """
        if getattr(self, "_read_only", False):
            raise PermissionError("A manutenção exige uma base de dados com escrita.")
        if self.conn.in_transaction:
            raise RuntimeError("Conclui ou cancela a transação pendente antes da manutenção.")
        mode = int(self.conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        if mode == 2:
            return {"changed": False, "auto_vacuum_mode": 2, "backup_path": ""}
        destination = Path(backup_path).resolve()
        source = self.path.resolve()
        reserved = {source, *(Path(str(source) + suffix) for suffix in ("-wal", "-shm", "-journal"))}
        if destination in reserved or destination.exists():
            raise FileExistsError("Escolhe um novo ficheiro para a cópia de segurança.")
        if not destination.parent.is_dir():
            raise FileNotFoundError("A pasta da cópia de segurança não existe.")
        page_count = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(self.conn.execute("PRAGMA page_size").fetchone()[0])
        # SQLite VACUUM can need 2x the original in free space; reserve a
        # further original-size allowance for the independent recovery copy.
        required = max(page_count * page_size, self.path.stat().st_size) * 3
        if min(shutil.disk_usage(self.path.parent).free, shutil.disk_usage(destination.parent).free) < required:
            raise OSError("Espaço livre insuficiente para a cópia e a manutenção segura.")
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        backup = sqlite3.connect(destination)
        try:
            self.conn.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise DataIntegrityError("A cópia de segurança não passou a verificação de integridade.")
        finally:
            backup.close()
        # PRAGMA then VACUUM is the required legacy conversion order. Failure
        # leaves the independent backup intact; never silently delete evidence.
        self.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.conn.execute("VACUUM").fetchall()
        actual = int(self.conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        if actual != 2:
            raise RuntimeError("A manutenção não ativou o modo incremental; a cópia foi preservada.")
        return {"changed": True, "auto_vacuum_mode": actual, "backup_path": str(destination)}

    def optimize_storage(self) -> dict[str, Any]:
        """Safely compact and optimize database storage with incremental vacuum and WAL checkpointing."""
        if getattr(self, "_read_only", False):
            raise PermissionError(
                "Não é possível otimizar uma base de dados aberta em modo de leitura."
            )

        start_time = time.perf_counter()
        stats_before = self.database_storage_stats()
        before_bytes = int(stats_before["total_size_bytes"])

        if self.conn.in_transaction:
            raise RuntimeError("Conclui ou cancela a transação pendente antes da manutenção.")

        # 1. Truncate WAL / checkpoint
        checkpoint_status = "ok"
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:
            checkpoint_status = str(exc)

        # 2. Rebuild query optimizer statistics
        try:
            self.conn.execute("PRAGMA optimize")
            self.conn.execute("ANALYZE")
        except Exception:
            pass

        # 3. Rebuild indexes for optimal performance
        try:
            self.conn.execute("REINDEX")
        except Exception:
            pass

        # 4. Incremental vacuum to reclaim free pages
        self.incremental_vacuum(pages=0)

        # 5. Verify integrity
        integrity = list(self.query("PRAGMA quick_check(1)"))
        integrity_status = str(integrity[0][0]) if integrity else "ok"

        # 6. Re-ensure composite performance indexes
        try:
            self.ensure_performance_indexes()
        except Exception:
            pass
        self.conn.commit()

        duration_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        stats_after = self.database_storage_stats()
        after_bytes = int(stats_after["total_size_bytes"])
        freed_bytes = max(0, before_bytes - after_bytes)

        return {
            "status": "ok"
            if integrity_status.lower() in ("ok", "true")
            else integrity_status,
            "checkpoint_status": checkpoint_status,
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "freed_bytes": freed_bytes,
            "duration_ms": duration_ms,
            "stats": stats_after,
        }

    def defragment_storage(self) -> dict[str, Any]:
        """Perform non-blocking WAL truncate and incremental vacuum."""
        return self.optimize_storage()

    def wal_checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        """Execute a cooperative WAL checkpoint (PASSIVE, FULL, RESTART, TRUNCATE).

        Returns tuple of (busy_status, log_pages, checkpointed_pages).
        """
        valid_modes = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
        target = str(mode or "PASSIVE").upper().strip()
        if target not in valid_modes:
            target = "PASSIVE"
        try:
            row = self.conn.execute(f"PRAGMA wal_checkpoint({target})").fetchone()
            if row:
                return (int(row[0]), int(row[1]), int(row[2]))
        except Exception:
            pass
        return (0, 0, 0)
