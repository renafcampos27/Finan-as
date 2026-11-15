"""Fixed, reviewed compatibility declarations for retained version-30 profiles.

These literals are application policy, never loaded from an inspected database.
The six retired feature tables are retained as opaque user evidence: no writes,
cleanup, automatic conversion or feature activation is authorised by this list.
Historical additive columns/defaults are preserved rather than rebuilt away.
See STARTUP_SCHEMA_COMPATIBILITY_2026-11-01.md for provenance and validation.
"""

RETAINED_TABLE_SQL = {
    'transactions': """CREATE TABLE transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL,
        category TEXT NOT NULL, account TEXT NOT NULL DEFAULT 'Conta principal',
        kind TEXT NOT NULL DEFAULT 'expense', notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        subcategory TEXT DEFAULT '', transfer_group_id TEXT NOT NULL DEFAULT '',
        category_id TEXT NOT NULL DEFAULT '', account_id TEXT NOT NULL DEFAULT '',
        goal_id INTEGER, goal_name_snapshot TEXT NOT NULL DEFAULT ''
    )""",
    'annual_goals': """CREATE TABLE annual_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            name TEXT NOT NULL,
            target_amount REAL NOT NULL DEFAULT 0,
            current_amount REAL NOT NULL DEFAULT 0,
            goal_type TEXT NOT NULL DEFAULT 'Poupança',
            notes TEXT NOT NULL DEFAULT ''
        )""",
    'monthly_split_plans': """CREATE TABLE monthly_split_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,
                base_type TEXT NOT NULL DEFAULT '',
                base_amount REAL NOT NULL DEFAULT 0,
                category TEXT NOT NULL,
                percent REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
    'personal_debts': """CREATE TABLE personal_debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'Emprestei',
            total_amount REAL NOT NULL DEFAULT 0,
            paid_amount REAL NOT NULL DEFAULT 0,
            due_date TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Aberto',
            notes TEXT NOT NULL DEFAULT ''
        )""",
    'query_cache': """CREATE TABLE query_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )""",
    'recurring_items': """CREATE TABLE recurring_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL DEFAULT 'Casa e contas',
            account TEXT NOT NULL DEFAULT 'Conta principal',
            day INTEGER NOT NULL DEFAULT 1,
            frequency TEXT NOT NULL DEFAULT 'Mensal',
            next_date TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            notes TEXT NOT NULL DEFAULT ''
        )""",
    'warranties': """CREATE TABLE warranties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            supplier TEXT NOT NULL DEFAULT '',
            purchase_date TEXT NOT NULL DEFAULT '',
            end_date TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0,
            attachment_id INTEGER,
            status TEXT NOT NULL DEFAULT 'Ativa',
            notes TEXT NOT NULL DEFAULT ''
        )""",
    'categories': """CREATE TABLE categories (
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                colour TEXT NOT NULL,
                budget REAL NOT NULL DEFAULT 0
            , parent_name TEXT DEFAULT '', budget_bucket TEXT NOT NULL DEFAULT '', stable_id TEXT NOT NULL DEFAULT '', identity_key TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1, merged_into_id TEXT NOT NULL DEFAULT '')""",
    'financial_state_snapshots': """CREATE TABLE financial_state_snapshots (
                day TEXT PRIMARY KEY,
                holdings_json TEXT NOT NULL DEFAULT '[]',
                liabilities_json TEXT NOT NULL DEFAULT '[]',
                subscriptions_json TEXT NOT NULL DEFAULT '[]',
                cash_balance REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            , goals_json TEXT)""",
    'monthly_targets': """CREATE TABLE monthly_targets (
                month TEXT PRIMARY KEY,
                expense_target REAL NOT NULL DEFAULT 0,
                savings_target REAL NOT NULL DEFAULT 0,
                investment_target REAL NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
    'schema_migrations': """CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
    'subcategories': """CREATE TABLE subcategories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                name TEXT NOT NULL, budget REAL DEFAULT 0, category_id TEXT NOT NULL DEFAULT '',
                UNIQUE(category, name)
            )""",
    'transaction_attachments': """CREATE TABLE transaction_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            original_path TEXT NOT NULL DEFAULT '',
            file_type TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '',
            supplier TEXT NOT NULL DEFAULT '',
            invoice_number TEXT NOT NULL DEFAULT '',
            invoice_date TEXT NOT NULL DEFAULT '',
            total_amount REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, content_sha256 TEXT NOT NULL DEFAULT '', file_size INTEGER NOT NULL DEFAULT 0, last_verified_at TEXT NOT NULL DEFAULT '', file_mtime_ns INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
        )""",
}

LEGACY_SEARCH_SQL = {
    'trg_transaction_meta_search_insert': """CREATE TRIGGER trg_transaction_meta_search_insert
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
                           COALESCE(NEW.tags,'') || ' ' ||
                           COALESCE(NEW.merchant,'') || ' ' ||
                           COALESCE(NEW.subcategory,'') || ' ' ||
                           COALESCE(NEW.payment_method,'')
                       )
                FROM transactions t WHERE t.id=NEW.transaction_id;
            END""",
    'trg_transaction_meta_search_update': """CREATE TRIGGER trg_transaction_meta_search_update
            AFTER UPDATE OF tags, merchant, subcategory, payment_method
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
                           COALESCE(NEW.tags,'') || ' ' ||
                           COALESCE(NEW.merchant,'') || ' ' ||
                           COALESCE(NEW.subcategory,'') || ' ' ||
                           COALESCE(NEW.payment_method,'')
                       )
                FROM transactions t WHERE t.id=NEW.transaction_id;
            END""",
    'trg_transaction_search_insert': """CREATE TRIGGER trg_transaction_search_insert
            AFTER INSERT ON transactions
            BEGIN
                INSERT OR REPLACE INTO transaction_search_fts(rowid, search_text)
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
            END""",
    'trg_transaction_search_update': """CREATE TRIGGER trg_transaction_search_update
            AFTER UPDATE OF description, category, subcategory, account, notes
            ON transactions
            BEGIN
                DELETE FROM transaction_search_fts WHERE rowid=NEW.id;
                INSERT INTO transaction_search_fts(rowid, search_text)
                SELECT NEW.id,
                       TRIM(
                           COALESCE(NEW.description,'') || ' ' ||
                           COALESCE(NEW.category,'') || ' ' ||
                           COALESCE(NEW.subcategory,'') || ' ' ||
                           COALESCE(NEW.account,'') || ' ' ||
                           COALESCE(NEW.notes,'') || ' ' ||
                           COALESCE(m.tags,'') || ' ' ||
                           COALESCE(m.merchant,'') || ' ' ||
                           COALESCE(m.subcategory,'') || ' ' ||
                           COALESCE(m.payment_method,'')
                       )
                FROM (SELECT 1) AS seed
                LEFT JOIN transaction_meta m ON m.transaction_id=NEW.id;
            END""",
}

COVERING_INDEX_SQL = "CREATE INDEX idx_transactions_date_amount_cat ON transactions(date, amount, category_id, category)"
