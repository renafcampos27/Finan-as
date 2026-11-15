#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from database.read_only import CancellableReadOperation
from database.validation import (
    sql_external_financial_flow,
    sql_finite_number,
    sql_iso_date,
)
from services.category_taxonomy import canonical_category
from services.account_identity import clean_account_name
from utils.app_logging import log_exception
from utils.helpers import (
    chunked_iterable,
    parse_month_key,
    require_eur_cent_precision,
    require_iso_date,
    require_nonzero_eur,
    row_get,
    safe_float,
    strict_float,
)


# BUG-AUD-C123: FTS5 MATCH syntax is hostile to user-typed punctuation.
# Quotes, colons, parentheses and neighbours change query semantics and an
# unbalanced quote raises ``sqlite3.OperationalError`` straight into the UI.
# The sanitizer keeps only word-ish tokens so the search phrase is always a
# safe quoted trigram phrase (or empty, which callers must treat as "no FTS
# filter" and fall back to INSTR).
_FTS5_HOSTILE_CHARS = re.compile(r"[\"'\u2018\u2019\u201c\u201d*:^()\[\]{}\\/+~-]")


def sanitize_fts5_query(term: object) -> str:
    """Normalize a user search term into a safe FTS5 quoted-phrase query.

    Strips quote/punctuation characters that alter FTS5 syntax, keeps tokens
    of at least three characters (the trigram tokenizer cannot index shorter
    strings) and returns each token as a quoted prefix phrase (``"tok"*``).
    Returns an empty string when nothing searchable remains.
    """
    cleaned = _FTS5_HOSTILE_CHARS.sub(" ", str(term or ""))
    tokens = [t.strip() for t in cleaned.split() if len(t.strip()) >= 3]
    if not tokens:
        return ""
    return " ".join(f'"{t}"*' for t in tokens)


class TransferLegs(tuple):
    """2-tuple representing transfer (debit_id, credit_id) supporting dict-style item lookup."""

    @property
    def debit_id(self) -> int:
        return self[0]

    @property
    def credit_id(self) -> int:
        return self[1]

    def __getitem__(self, item: Any) -> Any:
        if item == "debit_id":
            return self[0]
        if item == "credit_id":
            return self[1]
        return super().__getitem__(item)


class TransactionsRepoMixin:
    def categories(self, kind: Optional[str] = None) -> list[str]:
        return self.db.categories(kind)

    def subcategories(self, category: str) -> list[str]:
        rows = self.q(
            "SELECT name FROM subcategories WHERE category=? ORDER BY name", (category,)
        )
        return [r["name"] for r in rows]

    def category_rows(self, kind: Optional[str] = None) -> list[Any]:
        if kind:
            return self.q(
                "SELECT * FROM categories WHERE kind=? AND is_active=1 "
                "ORDER BY kind, name",
                (kind,),
            )
        return self.q("SELECT * FROM categories WHERE is_active=1 ORDER BY kind, name")

    def category_kind(self, category: object) -> str:
        """Return the registered movement kind for a category, if known."""

        category_id, resolved_name = self.category_identity(category)
        rows = self.q(
            "SELECT kind FROM categories "
            "WHERE stable_id=? AND name=? AND is_active=1 LIMIT 1",
            (category_id, resolved_name),
        )
        return str(rows[0]["kind"] or "") if rows else ""

    def validate_transaction_category(
        self,
        category: object,
        amount: object,
        *,
        allow_refunds: bool = True,
    ) -> str:
        """Reject unknown or income/expense-incompatible categories."""

        name = canonical_category(category)
        if not name:
            raise ValueError("Seleciona uma categoria.")
        if name in {"Transferência Interna", "Objetivos e Poupança"}:
            return name
        registered_kind = self.category_kind(name)
        amt = float(amount or 0)
        expected = "income" if amt > 0 else "expense"
        if not registered_kind:
            raise ValueError(
                f"A categoria «{name}» não existe. Escolhe uma categoria da lista."
            )
        if registered_kind != expected:
            if allow_refunds and registered_kind == "expense" and amt > 0:
                return name
            label = "receitas" if expected == "income" else "despesas"
            raise ValueError(
                f"A categoria «{name}» não pertence às categorias de {label}."
            )
        return name

    def category_budget_buckets(self) -> dict[str, str]:
        return {
            str(row["name"]): str(row["budget_bucket"] or "Desejos")
            for row in self.category_rows("expense")
        }

    def set_category_budget_bucket(self, category: str, bucket: str) -> None:
        allowed = {"Necessidades", "Desejos", "Poupança"}
        selected = str(bucket or "").strip()
        if selected not in allowed:
            raise ValueError(f"Invalid budget bucket: {selected}")
        try:
            cur = self.db.conn.execute(
                "UPDATE categories SET budget_bucket=? WHERE name=? AND kind='expense'",
                (selected, str(category or "").strip()),
            )
            self.db.conn.commit()
            if cur.rowcount == 0:
                raise ValueError(f"Category not found: {category}")
        except Exception:
            self.db.conn.rollback()
            raise

    def category_budgets(
        self, kind: Optional[str] = "expense", month: Optional[str] = None
    ) -> dict[str, float]:
        """Return category limits, optionally applying overrides for one month.

        The value stored on ``categories`` remains the legacy/default limit.
        Month-specific edits live in ``monthly_category_budgets`` so changing
        July can no longer rewrite the historical reading for June.
        """
        budgets = {
            str(r["name"]): safe_float(r["budget"]) for r in self.category_rows(kind)
        }
        month_text = str(month or "").strip()
        if not month_text:
            return budgets
        month_key = parse_month_key(month_text)
        rows = self.q(
            "SELECT category, budget FROM monthly_category_budgets WHERE month=?",
            (month_key,),
        )
        for row in rows:
            budgets[str(row["category"])] = safe_float(row["budget"])
        return budgets

    def category_budget(self, category: str, month: Optional[str] = None) -> float:
        category_name = str(category or "").strip()
        _category_id, category_name = self.category_identity(category_name)
        month_text = str(month or "").strip()
        if month_text:
            month_key = parse_month_key(month_text)
            rows = self.q(
                "SELECT budget FROM monthly_category_budgets "
                "WHERE month=? AND category=?",
                (month_key, category_name),
            )
            if rows:
                return safe_float(rows[0]["budget"])
        return self.scalar(
            "SELECT budget FROM categories WHERE name=?",
            (category_name,),
            default=0,
        )

    def set_category_budget(
        self, category: str, budget: float, month: Optional[str] = None
    ) -> None:
        category_name = str(category or "").strip()
        category_id, category_name = self.category_identity(category_name)
        amount = strict_float(budget, field_name="orçamento")
        month_text = str(month or "").strip()
        try:
            if month_text:
                month_key = parse_month_key(month_text)
                exists = self.scalar(
                    "SELECT COUNT(*) FROM categories WHERE name=?",
                    (category_name,),
                    default=0,
                )
                if not exists:
                    raise ValueError(f"Category not found: {category_name}")
                self.db.conn.execute(
                    """
                    INSERT INTO monthly_category_budgets(
                        month,category,budget,updated_at,category_id
                    ) VALUES(?,?,?,CURRENT_TIMESTAMP,?)
                    ON CONFLICT(month,category) DO UPDATE SET
                        budget=excluded.budget,
                        updated_at=CURRENT_TIMESTAMP,
                        category_id=excluded.category_id
                    """,
                    (month_key, category_name, amount, category_id),
                )
                self.db.conn.commit()
                return
            cur = self.db.conn.execute(
                "UPDATE categories SET budget=? WHERE name=?",
                (amount, category_name),
            )
            self.db.conn.commit()
            if cur.rowcount == 0:
                raise ValueError(f"Category not found: {category_name}")
        except Exception:
            self.db.conn.rollback()
            raise

    def month_bounds_from_key(self, month_key: str) -> tuple[str, str]:
        key = parse_month_key(month_key)
        start = date.fromisoformat(f"{key}-01")
        end = (
            date(start.year + 1, 1, 1)
            if start.month == 12
            else date(start.year, start.month + 1, 1)
        )
        return start.isoformat(), end.isoformat()

    def previous_month_key(self, month_key: str) -> str:
        key = parse_month_key(month_key)
        start = date.fromisoformat(f"{key}-01")
        return (start - timedelta(days=1)).replace(day=1).strftime("%Y-%m")

    def saved_filters(self) -> list[Any]:
        try:
            return self.db.saved_filters()
        except Exception as exc:
            log_exception("FinanceDB.saved_filters failed; using fallback query", exc)
            return self.q("SELECT * FROM saved_filters ORDER BY name")

    def save_filter(self, name: str, state: dict[str, Any]) -> None:
        name = str(name or "").strip()
        if not name:
            return
        try:
            self.db.save_filter(name, state)
        except Exception as exc:
            log_exception("FinanceDB.save_filter failed; using fallback query", exc)
            payload = json.dumps(state, ensure_ascii=False)
            try:
                self.db.conn.execute(
                    "INSERT INTO saved_filters(name,filter_json) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET filter_json=excluded.filter_json",
                    (name, payload),
                )
                self.db.conn.commit()
            except Exception:
                self.db.conn.rollback()
                raise

    def delete_saved_filter(self, name: str) -> None:
        try:
            self.db.delete_saved_filter(name)
        except Exception as exc:
            log_exception(
                "FinanceDB.delete_saved_filter failed; using fallback query", exc
            )
            try:
                self.db.conn.execute(
                    "DELETE FROM saved_filters WHERE name=?", (str(name or "").strip(),)
                )
                self.db.conn.commit()
            except Exception:
                self.db.conn.rollback()
                raise

    @staticmethod
    def _append_literal_instr_filter(
        where: list[str], params: list[Any], literal_search: str
    ) -> None:
        """Append the case-insensitive INSTR fallback used by short/raw terms."""
        fields = (
            "t.description",
            "t.category",
            "t.account",
            "t.notes",
            "m.external_reference",
            "m.tags",
            "m.merchant",
            "m.subcategory",
            "m.payment_method",
        )
        where.append(
            "("
            + " OR ".join(
                f"INSTR(LOWER(COALESCE({field},'')), LOWER(?)) > 0"
                for field in fields
            )
            + ")"
        )
        params.extend([literal_search] * len(fields))

    def _transaction_filter_parts(
        self,
        start: str = "",
        end: str = "",
        search: str = "",
        category: str = "Todas",
        kind: str = "Todos",
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        attachments: str = "Todos",
    ) -> tuple[list[str], list[Any], str]:
        where: list[str] = []
        params: list[Any] = []
        date_expr = "COALESCE(NULLIF(m.payment_date,''), t.date)"
        date_col = f"SUBSTR({date_expr}, 1, 10)"
        if start or end:
            where.append(sql_iso_date(date_col))
        if start:
            where.append(f"{date_col} >= ?")
            params.append(start[:10])
        if end:
            where.append(f"{date_col} <= ?")
            params.append(end[:10])
        literal_search = str(search or "").strip()
        if literal_search:
            # BUG-AUD-C123: user terms go through the FTS5 sanitizer first so
            # quotes/punctuation can never change MATCH semantics.  When the
            # sanitizer yields tokens the FTS path runs; a too-short term (the
            # trigram tokenizer cannot index it) degrades to the literal INSTR
            # fallback which stays correct for one- and two-character searches.
            fts_query = sanitize_fts5_query(literal_search)
            if fts_query:
                where.append(
                    "t.id IN (SELECT rowid FROM transaction_search_fts "
                    "WHERE transaction_search_fts MATCH ?)"
                )
                params.append(fts_query)
            else:
                self._append_literal_instr_filter(where, params, literal_search)
        if category and category != "Todas":
            where.append("t.category=?")
            params.append(category)
        if kind == "Receitas":
            where.append("t.amount>0")
        elif kind == "Despesas":
            where.append("t.amount<0")
        if min_value is not None:
            where.append("ABS(t.amount)>=?")
            params.append(float(min_value))
        if max_value is not None:
            where.append("ABS(t.amount)<=?")
            params.append(float(max_value))
        # Alias-independent on purpose: these filter parts are shared by the
        # rows statement and the summary statement, which build different joins.
        if attachments == "Com anexos":
            where.append(
                "EXISTS (SELECT 1 FROM transaction_attachments a "
                "WHERE a.transaction_id=t.id)"
            )
        elif attachments == "Sem anexos":
            where.append(
                "NOT EXISTS (SELECT 1 FROM transaction_attachments a "
                "WHERE a.transaction_id=t.id)"
            )
        return where, params, date_expr

    def _filtered_transactions_statement(
        self,
        start: str = "",
        end: str = "",
        search: str = "",
        category: str = "Todas",
        kind: str = "Todos",
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        attachments: str = "Todos",
        limit: Optional[int] = 2000,
        offset: int = 0,
    ) -> tuple[str, tuple[Any, ...]]:
        where, params, date_expr = self._transaction_filter_parts(
            start,
            end,
            search,
            category,
            kind,
            min_value,
            max_value,
            attachments,
        )
        sql = """
        SELECT t.*, IFNULL(m.tags,'') AS tags, IFNULL(m.merchant,'') AS merchant, IFNULL(m.payment_method,'') AS payment_method,
               IFNULL(m.subcategory,'') AS subcategory, IFNULL(m.purchase_date,'') AS purchase_date, IFNULL(m.payment_date,'') AS payment_date,
               (SELECT COUNT(*) FROM transaction_attachments a WHERE a.transaction_id=t.id) AS attachment_count,
               IFNULL(c.kind,'') AS category_kind,
               CASE WHEN t.category IN ('Transferência Interna','Objetivos e Poupança') THEN 0
                    WHEN c.kind IS NULL THEN 1
                    WHEN c.kind<>CASE WHEN t.amount>0 THEN 'income' ELSE 'expense' END THEN 1
                    ELSE 0 END AS category_inconsistent
        FROM transactions t
        LEFT JOIN transaction_meta m ON m.transaction_id=t.id
        LEFT JOIN categories c ON c.name=t.category
        """

        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {date_expr} DESC, t.id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
            if int(offset) > 0:
                sql += " OFFSET ?"
                params.append(max(0, int(offset)))
        elif int(offset) > 0:
            sql += " LIMIT -1 OFFSET ?"
            params.append(max(0, int(offset)))
        return sql, tuple(params)

    def _filtered_transactions_summary_statement(
        self,
        start: str = "",
        end: str = "",
        search: str = "",
        category: str = "Todas",
        kind: str = "Todos",
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        attachments: str = "Todos",
    ) -> tuple[str, tuple[Any, ...]]:
        where, params, date_expr = self._transaction_filter_parts(
            start,
            end,
            search,
            category,
            kind,
            min_value,
            max_value,
            attachments,
        )
        valid_amount = sql_finite_number("t.amount", non_zero=True)
        valid_date = sql_iso_date(f"SUBSTR({date_expr}, 1, 10)")
        valid_row = f"({valid_amount} AND {valid_date})"
        external_flow = sql_external_financial_flow("t.category")
        sql = f"""
            SELECT COUNT(*) AS movement_count,
                   COALESCE(SUM(CASE WHEN {valid_row} AND {external_flow}
                                               AND t.amount>0
                                     THEN t.amount ELSE 0 END),0) AS income,
                   COALESCE(SUM(CASE WHEN {valid_row} AND {external_flow}
                                               AND t.amount<0
                                     THEN ABS(t.amount) ELSE 0 END),0) AS expenses,
                   SUM(CASE WHEN {valid_row} THEN 1 ELSE 0 END) AS valid_movement_count,
                   SUM(CASE WHEN NOT {valid_amount} THEN 1 ELSE 0 END) AS invalid_amounts,
                   SUM(CASE WHEN NOT {valid_date} THEN 1 ELSE 0 END) AS invalid_dates,
                   SUM(CASE WHEN NOT {valid_row} THEN 1 ELSE 0 END) AS excluded_invalid_count,
                   MIN(CASE WHEN {valid_date} THEN {date_expr} END) AS first_date,
                   MAX(CASE WHEN {valid_date} THEN {date_expr} END) AS last_date
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql, tuple(params)

    def transactions_filtered_payload_operation(
        self,
        start: str = "",
        end: str = "",
        search: str = "",
        category: str = "Todas",
        kind: str = "Todos",
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        attachments: str = "Todos",
        limit: Optional[int] = 2000,
        offset: int = 0,
        *,
        context: Optional[dict[str, Any]] = None,
        include_rows: bool = True,
        include_summary: bool = True,
    ) -> CancellableReadOperation[dict[str, Any]]:
        """Build one cancellable snapshot for both visible rows and complete totals."""

        rows_sql, rows_params = self._filtered_transactions_statement(
            start,
            end,
            search,
            category,
            kind,
            min_value,
            max_value,
            attachments,
            limit,
            offset,
        )
        summary_sql, summary_params = self._filtered_transactions_summary_statement(
            start,
            end,
            search,
            category,
            kind,
            min_value,
            max_value,
            attachments,
        )
        payload_context = dict(context or {})

        def read_payload(connection) -> dict[str, Any]:
            rows = (
                list(connection.execute(rows_sql, rows_params).fetchall())
                if include_rows
                else []
            )
            summary_row = (
                connection.execute(summary_sql, summary_params).fetchone()
                if include_summary
                else None
            )
            return {
                **payload_context,
                "rows": rows,
                "summary": dict(summary_row) if summary_row is not None else {},
            }

        return CancellableReadOperation(self.db.path, read_payload)

    def transactions_filtered(
        self,
        start: str = "",
        end: str = "",
        search: str = "",
        category: str = "Todas",
        kind: str = "Todos",
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        attachments: str = "Todos",
        limit: Optional[int] = 2000,
        offset: int = 0,
    ) -> list[Any]:
        sql, params = self._filtered_transactions_statement(
            start,
            end,
            search,
            category,
            kind,
            min_value,
            max_value,
            attachments,
            limit,
            offset,
        )
        return self.q(sql, params)

    def transactions_filtered_fresh(
        self,
        start: str = "",
        end: str = "",
        search: str = "",
        category: str = "Todas",
        kind: str = "Todos",
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        attachments: str = "Todos",
        limit: Optional[int] = 2000,
        offset: int = 0,
    ) -> list[Any]:
        """Read movements through a short-lived SQLite connection.

        This bypasses any Qt/model/connection cache and is used by the
        Movimentos tab, where the user must see the current value immediately
        after editing and saving a movement.
        """
        payload = self.transactions_filtered_payload_operation(
            start,
            end,
            search,
            category,
            kind,
            min_value,
            max_value,
            attachments,
            limit,
            offset,
            include_summary=False,
        )()
        return list(payload["rows"])

    def transactions_filtered_summary_fresh(
        self,
        start: str = "",
        end: str = "",
        search: str = "",
        category: str = "Todas",
        kind: str = "Todos",
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
        attachments: str = "Todos",
    ) -> dict[str, Any]:
        """Return complete totals while the visible table loads only one chunk."""
        payload = self.transactions_filtered_payload_operation(
            start,
            end,
            search,
            category,
            kind,
            min_value,
            max_value,
            attachments,
            limit=0,
            include_rows=False,
        )()
        return dict(payload["summary"])

    def _insert_transaction(
        self,
        d: str,
        desc: str,
        amount: float,
        category: str,
        account: str,
        notes: str = "",
        subcategory: str = "",
        transfer_group_id: str = "",
        *,
        goal_id: Optional[int] = None,
    ) -> int:
        amount = require_nonzero_eur(amount, field_name="valor do movimento")
        category = str(category or "").strip() or "Diversos"
        kind = "income" if amount > 0 else "expense"
        category_id, category = self.category_identity(
            category,
            create=True,
            kind=kind,
        )
        account_id, account = self.account_identity(account)
        if not category_id:
            raise ValueError(f"A categoria «{category}» não tem uma identidade válida.")
        if not account_id:
            raise ValueError(f"A conta «{account}» não tem uma identidade válida.")
        cur = self.db.conn.execute(
            "INSERT INTO transactions("
            "date,description,amount,category,subcategory,account,kind,notes,"
            "transfer_group_id,category_id,account_id,goal_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d,
                desc,
                amount,
                category,
                subcategory,
                account,
                kind,
                notes,
                str(transfer_group_id or "").strip(),
                category_id,
                account_id,
                goal_id,
            ),
        )
        return int(cur.lastrowid)

    def add_transaction(
        self,
        d: str = "",
        desc: str = "",
        amount: float = 0.0,
        category: str = "Diversos",
        account: str = "Conta principal",
        notes: str = "",
        subcategory: str = "",
        transfer_group_id: str = "",
        *,
        goal_id: Optional[int] = None,
        external_reference: str = "",
        commit: bool = True,
        **kwargs: Any,
    ) -> int:
        """Insert one movement as a single atomic operation.

        ``_insert_transaction`` also writes identity rows (category/account +
        aliases).  Those inserts must never survive a failed movement, so the
        whole step runs inside a savepoint and is rolled back on any error
        instead of being consolidated by a later unrelated commit.
        """
        if not d and "date_str" in kwargs:
            d = kwargs.pop("date_str")
        if not desc and "description" in kwargs:
            desc = kwargs.pop("description")
        if not d:
            from datetime import date as _dt_date
            d = _dt_date.today().isoformat()
        if not desc:
            desc = "Movimento"
        if "amount" in kwargs:
            amount = kwargs.pop("amount")
        if "category" in kwargs:
            category = kwargs.pop("category")
        if "account" in kwargs:
            account = kwargs.pop("account")
        if "notes" in kwargs:
            notes = kwargs.pop("notes")
        if "subcategory" in kwargs:
            subcategory = kwargs.pop("subcategory")
        if "transfer_group_id" in kwargs:
            transfer_group_id = kwargs.pop("transfer_group_id")
        if goal_id is None and "goal_id" in kwargs:
            goal_id = kwargs.pop("goal_id")
        if not external_reference and "external_reference" in kwargs:
            external_reference = kwargs.pop("external_reference")

        connection = self.db.conn
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        savepoint_name = f"add_transaction_{uuid.uuid4().hex[:8]}"
        connection.execute(f"SAVEPOINT {savepoint_name}")
        try:
            transaction_id = self._insert_transaction(
                d,
                desc,
                amount,
                category,
                account,
                notes,
                subcategory,
                transfer_group_id,
                goal_id=goal_id,
            )
            if external_reference:
                self._set_transaction_meta_ext(
                    transaction_id,
                    external_reference=external_reference,
                )
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if owns_transaction:
                connection.rollback()
            raise
        if commit and owns_transaction:
            try:
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return transaction_id

    def add_transaction_with_meta(
        self,
        d: str,
        desc: str,
        amount: float,
        category: str,
        account: str,
        notes: str = "",
        *,
        tags: str = "",
        merchant: str = "",
        payment_method: str = "",
        status: str = "",
        is_reimbursable: int = 0,
        subcategory: str = "",
        purchase_date: str = "",
        payment_date: str = "",
        external_reference: str = "",
        commit: bool = True,
    ) -> int:
        """Persist a movement and its extended metadata as one operation.

        BUG-AUD-C125 & BUG-R6-TXN-02: the savepoint is always opened, even when ``commit`` is
        true.  When owns_transaction is true, an outer BEGIN IMMEDIATE is opened
        so releasing the savepoint never auto-commits when commit=False.
        """
        connection = self.db.conn
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        savepoint_name = f"movement_with_meta_{uuid.uuid4().hex[:8]}"
        connection.execute(f"SAVEPOINT {savepoint_name}")
        try:
            transaction_id = self._insert_transaction(
                d,
                desc,
                amount,
                category,
                account,
                notes,
                subcategory,
            )
            self._set_transaction_meta_ext(
                transaction_id,
                tags=tags,
                merchant=merchant,
                payment_method=payment_method,
                status=status,
                is_reimbursable=is_reimbursable,
                subcategory=subcategory,
                purchase_date=purchase_date,
                payment_date=payment_date,
                external_reference=external_reference,
            )
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if owns_transaction:
                connection.rollback()
            raise
        if commit and owns_transaction:
            try:
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return transaction_id

    def add_internal_transfer(
        self,
        d: str = "",
        amount: float = 0.0,
        source_account: str = "",
        destination_account: str = "",
        *,
        commit: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Create both legs of an internal transfer as one atomic operation.

        Validation happens before any write and account identity rows are
        created inside the same savepoint as the movement legs.  Creating
        identities first leaked orphan accounts that a later unrelated
        commit silently made permanent.

        Caller-owned transactions are never committed or rolled back here.
        With no existing transaction, commit=False leaves a new transaction
        open for the caller; commit=True persists both legs together.
        """
        if not d and "date_str" in kwargs:
            d = kwargs.pop("date_str")
        if not d:
            from datetime import date as _dt_date
            d = _dt_date.today().isoformat()
        if "amount" in kwargs:
            amount = kwargs.pop("amount")
        if "source_account" in kwargs:
            source_account = kwargs.pop("source_account")
        if "destination_account" in kwargs:
            destination_account = kwargs.pop("destination_account")
        if "description" in kwargs:
            kwargs.pop("description")

        amount = abs(float(amount or 0))
        if amount <= 0:
            raise ValueError(
                "O valor da transferência tem de ser superior a zero."
            )
        clean_source = clean_account_name(source_account)
        clean_destination = clean_account_name(destination_account)
        if not clean_source or not clean_destination:
            raise ValueError("Seleciona as contas de origem e destino.")
        if clean_source.casefold() == clean_destination.casefold():
            raise ValueError(
                "As contas de origem e destino têm de ser diferentes."
            )
        transfer_group_id = uuid.uuid4().hex
        connection = self.db.conn
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            # Releasing an outermost savepoint would implicitly commit even
            # when commit=False. Keep a real transaction outside it.
            connection.execute("BEGIN IMMEDIATE")
        savepoint_name = f"internal_transfer_{uuid.uuid4().hex[:8]}"
        connection.execute(f"SAVEPOINT {savepoint_name}")
        try:
            source_account_id, source_account = self.account_identity(clean_source)
            destination_account_id, destination_account = self.account_identity(
                clean_destination
            )
            if not source_account or not destination_account:
                raise ValueError("Seleciona as contas de origem e destino.")
            if source_account == destination_account:
                raise ValueError(
                    "As contas de origem e destino têm de ser diferentes."
                )
            debit_id = self._insert_transaction(
                d,
                f"Transferência para {destination_account}",
                -amount,
                "Transferência Interna",
                source_account,
                "Automático",
                "",
                transfer_group_id,
            )
            credit_id = self._insert_transaction(
                d,
                f"Transferência de {source_account}",
                amount,
                "Transferência Interna",
                destination_account,
                "Automático",
                "",
                transfer_group_id,
            )
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if owns_transaction:
                connection.rollback()
            raise
        if commit and owns_transaction:
            try:
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return TransferLegs((debit_id, credit_id))

    def expand_transfer_linked_ids(self, tx_ids: list[int]) -> list[int]:
        """Expand a selection so one transfer leg can never be mutated alone."""

        ids = list(dict.fromkeys(int(value) for value in tx_ids if int(value) > 0))
        if not ids:
            return []
        groups: list[str] = []
        for chunk in chunked_iterable(ids, chunk_size=400):
            placeholders = ",".join("?" for _ in chunk)
            groups.extend(
                str(row["transfer_group_id"] or "")
                for row in self.q(
                    f"SELECT DISTINCT transfer_group_id FROM transactions "
                    f"WHERE id IN ({placeholders}) AND transfer_group_id<>''",
                    tuple(chunk),
                )
            )
        groups = list(dict.fromkeys(g for g in groups if g))
        if groups:
            for chunk in chunked_iterable(groups, chunk_size=400):
                group_placeholders = ",".join("?" for _ in chunk)
                ids.extend(
                    int(row["id"])
                    for row in self.q(
                        f"SELECT id FROM transactions WHERE transfer_group_id "
                        f"IN ({group_placeholders})",
                        tuple(chunk),
                    )
                )
        return list(dict.fromkeys(ids))

    def linked_transfer_ids(self, tx_ids: list[int]) -> list[int]:
        ids = list(dict.fromkeys(int(value) for value in tx_ids if int(value) > 0))
        if not ids:
            return []
        result: list[int] = []
        for chunk in chunked_iterable(ids, chunk_size=400):
            placeholders = ",".join("?" for _ in chunk)
            result.extend(
                int(row["id"])
                for row in self.q(
                    f"SELECT id FROM transactions WHERE id IN ({placeholders}) "
                    "AND transfer_group_id<>''",
                    tuple(chunk),
                )
            )
        return list(dict.fromkeys(result))

    def internal_transfer_by_id(self, tx_id: int) -> Optional[dict[str, Any]]:
        row = self.transaction_by_id(int(tx_id))
        group_id = str(row_get(row, "transfer_group_id", "") or "")
        if not group_id:
            return None
        rows = self.q(
            "SELECT * FROM transactions WHERE transfer_group_id=? ORDER BY amount,id",
            (group_id,),
        )
        if len(rows) != 2:
            raise RuntimeError(
                "A transferência ligada está incompleta. Usa a verificação de dados "
                "antes de a alterar."
            )
        debit = next((item for item in rows if float(item["amount"] or 0) < 0), None)
        credit = next((item for item in rows if float(item["amount"] or 0) > 0), None)
        if debit is None or credit is None:
            raise RuntimeError("As duas partes da transferência não se anulam.")
        amount = abs(float(debit["amount"] or 0))
        if abs(float(credit["amount"] or 0) - amount) > 0.000001:
            raise RuntimeError(
                "As duas partes da transferência têm valores diferentes."
            )
        return {
            "group_id": group_id,
            "ids": [int(debit["id"]), int(credit["id"])],
            "date": str(debit["date"] or ""),
            "amount": amount,
            "source": str(debit["account"] or "Conta principal"),
            "destination": str(credit["account"] or "Conta principal"),
        }

    def update_internal_transfer(
        self,
        transfer_group_id: str = "",
        d: str = "",
        amount: float = 0.0,
        source_account: str = "",
        destination_account: str = "",
        *,
        tx_id: Optional[int] = None,
        commit: bool = True,
        **kwargs: Any,
    ) -> tuple[int, int]:
        """Update the two transfer legs together under one savepoint."""
        if not transfer_group_id and tx_id is not None:
            t_info = self.internal_transfer_by_id(tx_id)
            if t_info is not None:
                transfer_group_id = t_info["group_id"]
        if not transfer_group_id and "tx_id" in kwargs:
            t_info = self.internal_transfer_by_id(kwargs.pop("tx_id"))
            if t_info is not None:
                transfer_group_id = t_info["group_id"]
        if not d and "date_str" in kwargs:
            d = kwargs.pop("date_str")
        if not d:
            from datetime import date as _dt_date
            d = _dt_date.today().isoformat()
        if "amount" in kwargs:
            amount = kwargs.pop("amount")
        if "source_account" in kwargs:
            source_account = kwargs.pop("source_account")
        if "destination_account" in kwargs:
            destination_account = kwargs.pop("destination_account")

        group_id = str(transfer_group_id or "").strip()
        amount = abs(require_nonzero_eur(amount, field_name="valor da transferência"))
        # BUG-DB-002: the ownership check must run BEFORE any write, including
        # ``canonical_account_name``/``category_identity``/``account_identity``:
        # each of them opens an implicit transaction whenever it creates a
        # missing account/category.  With the check placed after them,
        # ``owns_transaction`` saw False, the savepoint became the transaction
        # boundary, ``RELEASE`` ended the caller-owned transaction and
        # ``commit=True`` skipped its own commit (dangling open transaction).
        owns_transaction = not self.db.conn.in_transaction
        connection = self.db.conn
        if owns_transaction:
            # Releasing an outermost savepoint would implicitly commit even
            # when commit=False.  Mirror ``add_internal_transfer``: keep a
            # real transaction outside the savepoint so the caller-owned
            # transaction contract holds (release never ends the outer
            # transaction; only ``commit=True`` + ownership commits here).
            connection.execute("BEGIN IMMEDIATE")
        source_account = self.canonical_account_name(source_account)
        destination_account = self.canonical_account_name(destination_account)
        if not group_id:
            raise ValueError("A transferência não tem uma ligação válida.")
        if not source_account or not destination_account:
            raise ValueError("Seleciona as contas de origem e destino.")
        if source_account == destination_account:
            raise ValueError("As contas de origem e destino têm de ser diferentes.")
        transfer_category_id, transfer_category = self.category_identity(
            "Transferência Interna",
            create=True,
            kind="expense",
        )
        source_account_id, source_account = self.account_identity(source_account)
        destination_account_id, destination_account = self.account_identity(
            destination_account
        )
        rows = self.q(
            "SELECT id,amount,goal_id FROM transactions WHERE transfer_group_id=? ORDER BY id",
            (group_id,),
        )
        if len(rows) != 2:
            raise RuntimeError("A transferência ligada está incompleta.")
        debit_row = next(
            (row for row in rows if float(row["amount"] or 0) < 0),
            None,
        )
        credit_row = next(
            (row for row in rows if float(row["amount"] or 0) > 0),
            None,
        )
        if not debit_row or not credit_row:
            raise RuntimeError("A transferência não contém uma saída e uma entrada.")
        debit_id = int(debit_row["id"])
        credit_id = int(credit_row["id"])
        savepoint_name = f"update_transfer_{uuid.uuid4().hex[:8]}"
        connection.execute(f"SAVEPOINT {savepoint_name}")
        try:
            debit = connection.execute(
                """
                UPDATE transactions
                SET date=?,description=?,amount=?,category=?,category_id=?,
                    subcategory='',account=?,account_id=?,kind='expense',
                    notes='Automático'
                WHERE id=? AND transfer_group_id=?
                """,
                (
                    d,
                    f"Transferência para {destination_account}",
                    -amount,
                    transfer_category,
                    transfer_category_id,
                    source_account,
                    source_account_id,
                    debit_id,
                    group_id,
                ),
            )
            credit = connection.execute(
                """
                UPDATE transactions
                SET date=?,description=?,amount=?,category=?,category_id=?,
                    subcategory='',account=?,account_id=?,kind='income',
                    notes='Automático'
                WHERE id=? AND transfer_group_id=?
                """,
                (
                    d,
                    f"Transferência de {source_account}",
                    amount,
                    transfer_category,
                    transfer_category_id,
                    destination_account,
                    destination_account_id,
                    credit_id,
                    group_id,
                ),
            )
            if debit.rowcount != 1 or credit.rowcount != 1:
                raise RuntimeError("A atualização das duas partes não ficou completa.")
            # BUG-R6-TXN-04: synchronize goals if any leg was linked to a goal
            for leg_row, new_leg_amount in ((debit_row, -amount), (credit_row, amount)):
                goal_id = row_get(leg_row, "goal_id")
                if goal_id is not None:
                    old_leg_amount = float(leg_row["amount"] or 0)
                    if abs(new_leg_amount - old_leg_amount) > 1e-6:
                        delta_saved = old_leg_amount - new_leg_amount
                        connection.execute(
                            "UPDATE goals SET saved_amount = MAX(0.0, saved_amount + ?) WHERE id = ?",
                            (delta_saved, int(goal_id)),
                        )
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if owns_transaction:
                connection.rollback()
            raise
        if commit and owns_transaction:
            try:
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return debit_id, credit_id

    def duplicate_internal_transfer(
        self, tx_id: int, *, commit: bool = True
    ) -> tuple[int, int]:
        transfer = self.internal_transfer_by_id(tx_id)
        if transfer is None:
            raise ValueError("O movimento não pertence a uma transferência ligada.")
        return self.add_internal_transfer(
            transfer["date"],
            transfer["amount"],
            transfer["source"],
            transfer["destination"],
            commit=commit,
        )

    def duplicate_transaction(
        self,
        tx_id: int,
        *,
        description_suffix: str = " (cópia)",
        commit: bool = True,
    ) -> int:
        """Duplicate one movement, including metadata and category splits."""

        connection = self.db.conn
        connection.execute("SAVEPOINT duplicate_transaction")
        try:
            source = connection.execute(
                "SELECT * FROM transactions WHERE id=?",
                (int(tx_id),),
            ).fetchone()
            if source is None:
                raise ValueError(f"Transaction not found: {tx_id}")
            if str(row_get(source, "transfer_group_id", "") or ""):
                raise ValueError(
                    "Uma transferência ligada tem de ser duplicada pelas duas partes."
                )
            meta = connection.execute(
                "SELECT * FROM transaction_meta WHERE transaction_id=?",
                (int(tx_id),),
            ).fetchone()
            transaction_id = self._insert_transaction(
                str(source["date"] or ""),
                str(source["description"] or "") + str(description_suffix or ""),
                float(source["amount"]),
                str(source["category"] or "Outros"),
                str(source["account"] or "Conta principal"),
                str(source["notes"] or ""),
                str(row_get(source, "subcategory", "") or ""),
            )
            if meta is not None:
                self._set_transaction_meta_ext(
                    transaction_id,
                    tags=str(row_get(meta, "tags", "") or ""),
                    merchant=str(row_get(meta, "merchant", "") or ""),
                    payment_method=str(row_get(meta, "payment_method", "") or ""),
                    status=str(row_get(meta, "status", "") or ""),
                    is_reimbursable=int(row_get(meta, "is_reimbursable", 0) or 0),
                    subcategory=str(row_get(meta, "subcategory", "") or ""),
                    purchase_date=str(row_get(meta, "purchase_date", "") or ""),
                    payment_date=str(row_get(meta, "payment_date", "") or ""),
                )
            split_rows = connection.execute(
                "SELECT category,category_id,percent,amount FROM transaction_splits "
                "WHERE transaction_id=? ORDER BY id",
                (int(tx_id),),
            ).fetchall()
            for split in split_rows:
                connection.execute(
                    "INSERT INTO transaction_splits("
                    "transaction_id,category,category_id,percent,amount) "
                    "VALUES(?,?,?,?,?)",
                    (
                        transaction_id,
                        str(split["category"] or ""),
                        str(split["category_id"] or ""),
                        float(split["percent"]),
                        float(split["amount"]),
                    ),
                )
            connection.execute("RELEASE SAVEPOINT duplicate_transaction")
            if commit:
                connection.commit()
            return transaction_id
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT duplicate_transaction")
            connection.execute("RELEASE SAVEPOINT duplicate_transaction")
            if commit:
                connection.rollback()
            raise

    def import_transactions_batch(
        self,
        rows: list[dict[str, Any]],
        *,
        commit: bool = True,
        chunk_size: int = 500,
    ) -> list[int]:
        """Persist one normalized import batch atomically or in cooperative chunks."""
        if not rows:
            return []
        connection = self.db.conn
        created_ids: list[int] = []
        # Only this synchronous operation owns these entries. A direct caller
        # rollback, savepoint rollback or later alias edit cannot leave stale
        # positive/negative identities on the Repo instance.
        account_cache: dict[str, tuple[str, str]] = {}
        category_cache: dict[tuple[str, bool], tuple[str, str]] = {}

        def _persist_single_row(row: dict[str, Any]) -> int:
            amount = require_eur_cent_precision(
                row.get("amount"),
                field_name="valor do movimento importado",
            )
            transaction_date = require_iso_date(
                row.get("date"),
                field_name="data do movimento importado",
            )
            account_key = clean_account_name(row.get("account"))
            if account_key not in account_cache:
                account_cache[account_key] = self.account_identity(account_key)
            account_id, account = account_cache[account_key]
            category_key = (canonical_category(row.get("category") or "Diversos"), amount > 0)
            # The ledger trigger defines kind from the signed amount. A refund
            # may retain an expense category without becoming a negative inflow.
            resolved_kind = "income" if amount > 0 else "expense"
            if category_key not in category_cache:
                category = self.validate_transaction_category(
                    category_key[0], amount, allow_refunds=True,
                )
                existing_kind = self.category_kind(category)
                category_cache[category_key] = self.category_identity(
                    category, create=True, kind=existing_kind or resolved_kind,
                )
            category_id, category = category_cache[category_key]
            cursor = connection.execute(
                """
                INSERT INTO transactions(
                    date,description,amount,category,account,kind,notes,
                    category_id,account_id
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    transaction_date,
                    str(row.get("description") or "").strip(),
                    amount,
                    category,
                    account,
                    resolved_kind,
                    str(row.get("notes") or ""),
                    category_id,
                    account_id,
                ),
            )
            tx_id = int(cursor.lastrowid)
            payment_date = str(row.get("date") or "")[:10]
            connection.execute(
                """
                INSERT INTO transaction_meta(
                    transaction_id,merchant,payment_method,purchase_date,
                    payment_date,payment_month,external_reference,updated_at
                ) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(transaction_id) DO UPDATE SET
                    merchant=excluded.merchant,
                    payment_method=excluded.payment_method,
                    purchase_date=excluded.purchase_date,
                    payment_date=excluded.payment_date,
                    payment_month=excluded.payment_month,
                    external_reference=excluded.external_reference,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    tx_id,
                    str(row.get("merchant") or "").strip(),
                    str(row.get("payment_method") or "").strip(),
                    payment_date,
                    payment_date,
                    payment_date[:7],
                    str(row.get("external_reference") or "").strip(),
                ),
            )
            return tx_id

        if not commit:
            for row in rows:
                created_ids.append(_persist_single_row(row))
            return created_ids

        # AUD-DB-07: In massive imports, batch into chunks of `chunk_size` (default 500)
        # with cooperative WAL checkpoints between chunks to prevent write-lock starvation.
        # BUG-DB-004: a failure mid-way through the chunks must not leave
        # partially imported movements behind, so every chunk runs inside one
        # outer savepoint.  The inner savepoint keeps per-chunk WAL checkpoints
        # possible, while any failure rolls the whole batch back to the empty
        # pre-import state (nothing is persisted for a failed file).
        effective_chunk_size = max(1, int(chunk_size))
        connection.execute("SAVEPOINT smart_import_batch")
        try:
            for chunk in chunked_iterable(rows, chunk_size=effective_chunk_size):
                connection.execute("SAVEPOINT smart_import_chunk")
                try:
                    for row in chunk:
                        created_ids.append(_persist_single_row(row))
                    connection.execute("RELEASE SAVEPOINT smart_import_chunk")
                except Exception:
                    connection.execute("ROLLBACK TO SAVEPOINT smart_import_chunk")
                    connection.execute("RELEASE SAVEPOINT smart_import_chunk")
                    raise
            connection.execute("RELEASE SAVEPOINT smart_import_batch")
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT smart_import_batch")
            connection.execute("RELEASE SAVEPOINT smart_import_batch")
            connection.rollback()
            raise
        connection.commit()
        # Cooperative WAL checkpoint to let concurrent readers execute without lock timeouts
        wal_fn = getattr(self.db, "wal_checkpoint", None)
        if callable(wal_fn):
            wal_fn("PASSIVE")

        return created_ids

    def _update_transaction_fields(self, tx_id: int, *, _commit: bool = True, **kwargs: Any) -> None:
        if not kwargs:
            return

        kwargs = dict(kwargs)
        if "d" in kwargs:
            kwargs["date"] = kwargs.pop("d")
        if "desc" in kwargs:
            kwargs["description"] = kwargs.pop("desc")

        allowed_fields = {
            "date",
            "description",
            "amount",
            "category",
            "subcategory",
            "account",
            "kind",
            "notes",
            "goal_id",
        }
        unsupported = sorted(set(kwargs) - allowed_fields)
        if unsupported:
            raise ValueError(
                "Campos de movimento não suportados: " + ", ".join(unsupported)
            )

        if "amount" in kwargs:
            kwargs["amount"] = require_nonzero_eur(
                kwargs["amount"], field_name="valor do movimento"
            )
            kwargs["kind"] = "income" if float(kwargs["amount"]) > 0 else "expense"

        # BUG-DB-005: the transactions row, the transaction_splits maintenance
        # and the goals compensation must mutate atomically.  Follow the
        # repo-wide caller-owned contract (see add_internal_transfer): when
        # the caller already owns a transaction we only open a nested
        # savepoint and never commit/rollback for them.
        connection = self.db.conn
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            # Releasing an outermost savepoint would implicitly commit even
            # when the caller asked for no commit.  Keep a real transaction
            # outside the savepoint so the contract holds.
            connection.execute("BEGIN IMMEDIATE")
        savepoint_name = f"update_transaction_fields_{uuid.uuid4().hex[:8]}"
        connection.execute(f"SAVEPOINT {savepoint_name}")
        try:
            self._update_transaction_fields_mutable(tx_id, **kwargs)
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if owns_transaction:
                connection.rollback()
            raise
        if _commit and owns_transaction:
            try:
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _update_transaction_fields_mutable(
        self, tx_id: int, **kwargs: Any
    ) -> None:
        """Perform the validated field mutation (runs inside a savepoint)."""
        current = self.db.conn.execute(
            "SELECT category,amount,account,transfer_group_id,goal_id "
            "FROM transactions WHERE id=?",
            (int(tx_id),),
        ).fetchone()
        if current is None:
            raise RuntimeError(
                f"Movimento {tx_id} não foi encontrado para actualização."
            )
        if str(row_get(current, "transfer_group_id", "") or ""):
            raise ValueError(
                "Esta linha pertence a uma transferência ligada. "
                "Edita a transferência completa."
            )
        final_amount = float(kwargs.get("amount", current["amount"]) or 0)
        final_category = kwargs.get("category", current["category"])
        validated_category = self.validate_transaction_category(
            final_category,
            final_amount,
        )
        category_id, category_name = self.category_identity(validated_category)
        kwargs["category"] = category_name
        kwargs["category_id"] = category_id
        if "account" in kwargs:
            account_id, account_name = self.account_identity(kwargs["account"])
            kwargs["account"] = account_name
            kwargs["account_id"] = account_id
        category_changed = "category" in kwargs and str(
            kwargs["category"] or ""
        ) != str(current["category"] or "")

        fields = list(kwargs.keys())
        values = list(kwargs.values())

        set_clause = ", ".join(f"{f}=?" for f in fields)
        query = f"UPDATE transactions SET {set_clause} WHERE id=?"

        cur = self.db.conn.execute(query, tuple(values + [int(tx_id)]))
        if cur.rowcount == 0:
            raise RuntimeError(
                f"Movimento {tx_id} não foi encontrado para actualização."
            )
        if category_changed:
            self.db.conn.execute(
                "DELETE FROM transaction_splits WHERE transaction_id=?",
                (int(tx_id),),
            )
        elif "amount" in kwargs:
            split_rows = self.db.conn.execute(
                "SELECT id, category, category_id, percent, created_at "
                "FROM transaction_splits WHERE transaction_id=? ORDER BY id ASC",
                (int(tx_id),),
            ).fetchall()
            if split_rows:
                normalized = [
                    (str(r["category"]), float(r["percent"])) for r in split_rows
                ]
                allocated = self._allocate_split_amounts(
                    float(kwargs["amount"]), normalized
                )
                self.db.conn.execute(
                    "DELETE FROM transaction_splits WHERE transaction_id=?",
                    (int(tx_id),),
                )
                for r, (_cat, _pct, split_amt) in zip(split_rows, allocated):
                    self.db.conn.execute(
                        "INSERT INTO transaction_splits("
                        "id, transaction_id, category, category_id, percent, amount, created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            r["id"],
                            int(tx_id),
                            r["category"],
                            r["category_id"],
                            r["percent"],
                            split_amt,
                            r["created_at"],
                        ),
                    )

        # PERS-04: Sincronização automática entre edição de transações e saldo de metas
        old_goal_id = row_get(current, "goal_id")
        new_goal_id = kwargs.get("goal_id", old_goal_id)
        old_amount = float(current["amount"] or 0)
        new_amount = float(kwargs.get("amount", old_amount) or 0)

        if old_goal_id is not None or new_goal_id is not None:
            if old_goal_id is not None and old_goal_id == new_goal_id and abs(new_amount - old_amount) > 1e-6:
                # Transação negativa representa depósito (aumenta meta); positiva representa levantamento (reduz meta)
                delta_saved = old_amount - new_amount
                self.db.conn.execute(
                    "UPDATE goals SET saved_amount = MAX(0.0, saved_amount + ?) WHERE id = ?",
                    (delta_saved, int(old_goal_id)),
                )
            elif old_goal_id != new_goal_id:
                if old_goal_id is not None:
                    # Reverter o efeito do movimento antigo na meta anterior
                    self.db.conn.execute(
                        "UPDATE goals SET saved_amount = MAX(0.0, saved_amount + ?) WHERE id = ?",
                        (old_amount, int(old_goal_id)),
                    )
                if new_goal_id is not None:
                    # Aplicar o novo movimento na nova meta
                    self.db.conn.execute(
                        "UPDATE goals SET saved_amount = MAX(0.0, saved_amount - ?) WHERE id = ?",
                        (new_amount, int(new_goal_id)),
                    )

    def update_transaction_fields(
        self,
        tx_id: int,
        updates: dict[str, Any],
        *,
        commit: bool = True,
    ) -> None:
        """Atomically update fields without flushing a caller-owned transaction."""
        self._update_transaction_fields(int(tx_id), _commit=commit, **dict(updates or {}))

    def update_transaction(self, tx_id: int, **kwargs: Any) -> None:
        """Update a movement using the same owner-aware atomic field helper."""
        if kwargs:
            self.update_transaction_fields(tx_id, kwargs)

    def update_transaction_with_meta(
        self,
        tx_id: int,
        transaction_updates: dict[str, Any],
        *,
        tags: str = "",
        merchant: str = "",
        payment_method: str = "",
        status: str = "",
        is_reimbursable: int = 0,
        subcategory: str = "",
        purchase_date: str = "",
        payment_date: str = "",
        external_reference: str = "",
        commit: bool = True,
    ) -> None:
        """Update the movement and metadata without allowing a partial save."""
        connection = self.db.conn
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        savepoint_name = f"update_movement_with_meta_{uuid.uuid4().hex[:8]}"
        connection.execute(f"SAVEPOINT {savepoint_name}")
        try:
            self._update_transaction_fields(tx_id, **dict(transaction_updates))
            self._set_transaction_meta_ext(
                tx_id,
                tags=tags,
                merchant=merchant,
                payment_method=payment_method,
                status=status,
                is_reimbursable=is_reimbursable,
                subcategory=subcategory,
                purchase_date=purchase_date,
                payment_date=payment_date,
                external_reference=external_reference,
            )
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if owns_transaction:
                connection.rollback()
            raise
        if commit and owns_transaction:
            try:
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def transaction_full_by_id(self, tx_id: int) -> Optional[Any]:
        rows = self.q(
            """
            SELECT t.*, IFNULL(m.tags,'') AS tags, IFNULL(m.merchant,'') AS merchant, IFNULL(m.payment_method,'') AS payment_method,
                   IFNULL(m.status,'') AS status, IFNULL(m.is_reimbursable,0) AS is_reimbursable,
                   IFNULL(m.subcategory,'') AS subcategory, IFNULL(m.purchase_date,'') AS purchase_date, IFNULL(m.payment_date,'') AS payment_date,
                   IFNULL(a.cnt,0) AS attachment_count,
                   IFNULL(c.kind,'') AS category_kind,
                   CASE WHEN t.category IN ('Transferência Interna','Objetivos e Poupança') THEN 0
                        WHEN c.kind IS NULL THEN 1
                        WHEN c.kind<>CASE WHEN t.amount>0 THEN 'income' ELSE 'expense' END THEN 1
                        ELSE 0 END AS category_inconsistent
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            LEFT JOIN (SELECT transaction_id, COUNT(*) AS cnt FROM transaction_attachments GROUP BY transaction_id) a ON a.transaction_id=t.id
            LEFT JOIN categories c ON c.name=t.category
            WHERE t.id=?
            """,
            (int(tx_id),),
        )
        return rows[0] if rows else None

    def delete_transaction(self, tx_id: int) -> None:
        self.delete_transactions([tx_id])

    def delete_transactions(self, tx_ids: list[int]) -> int:
        """Delete a selection as one database operation.

        File cleanup remains in ``DocumentService``.  If any selected movement
        cannot be deleted, every database change in the batch is rolled back.
        """
        ids = self.expand_transfer_linked_ids(tx_ids)
        if not ids:
            return 0
        connection = self.db.conn
        connection.execute("SAVEPOINT delete_movements")
        try:
            found = 0
            for chunk in chunked_iterable(ids, chunk_size=400):
                placeholders = ",".join("?" for _ in chunk)
                found += int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM transactions WHERE id IN ({placeholders})",
                        tuple(chunk),
                    ).fetchone()[0]
                )
            if found != len(ids):
                raise RuntimeError(
                    "Um ou mais movimentos selecionados já não existem. "
                    "Atualiza a lista e tenta novamente."
                )
            # Keep the database consistent even when cascading is unavailable.
            deleted_count = 0
            for chunk in chunked_iterable(ids, chunk_size=400):
                placeholders = ",".join("?" for _ in chunk)
                params = tuple(chunk)
                for table in (
                    "transaction_attachments",
                    "transaction_splits",
                    "transaction_meta",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE transaction_id IN ({placeholders})",
                        params,
                    )
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
                cursor = connection.execute(
                    f"DELETE FROM transactions WHERE id IN ({placeholders})", params
                )
                deleted_count += cursor.rowcount

            remaining = 0
            for chunk in chunked_iterable(ids, chunk_size=400):
                placeholders = ",".join("?" for _ in chunk)
                remaining += int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM transactions WHERE id IN ({placeholders})",
                        tuple(chunk),
                    ).fetchone()[0]
                )
            if remaining != 0:
                raise RuntimeError("A eliminação em lote não ficou completa.")
            connection.execute("RELEASE SAVEPOINT delete_movements")
            connection.commit()
            return len(ids)
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT delete_movements")
            connection.execute("RELEASE SAVEPOINT delete_movements")
            connection.rollback()
            raise

    def clear_all_transactions(self) -> int:
        """Remove the complete movement ledger as one atomic maintenance action.

        Planning rules, categories, accounts, investments, goals and documents
        not linked to a movement are deliberately preserved.  Existing
        subscription/payment occurrences remain as processed occurrences, but
        their deleted movement link is cleared so an old charge is not silently
        regenerated at the next startup.
        """

        connection = self.db.conn
        connection.execute("SAVEPOINT clear_all_movements")
        try:
            total = int(
                connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            )
            for table in (
                "subscription_occurrences",
                "scheduled_payment_occurrences",
            ):
                try:
                    connection.execute(
                        f"UPDATE {table} SET transaction_id=NULL "
                        "WHERE transaction_id IS NOT NULL"
                    )
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc).casefold():
                        raise
            # BUG-PERS-004: goal-linked movements are being deleted here, so
            # their contributions to goals.saved_amount must be compensated
            # first, using the same sign convention as the (batch 10 fixed)
            # trg_transactions_cascade_transfer_leg trigger and
            # delete_transactions: ``saved_amount + amount`` (a deposit leg is
            # negative), floored at zero.  Transfer legs are excluded — they
            # never contributed to saved_amount and the cascade trigger
            # compensates them exclusively (BUG-DB-003).  A transfer leg with
            # a goal link would otherwise be double-counted here.
            goal_txs = connection.execute(
                "SELECT goal_id, amount FROM transactions "
                "WHERE goal_id IS NOT NULL "
                "AND (transfer_group_id IS NULL OR transfer_group_id = '')"
            ).fetchall()
            for gtx in goal_txs:
                gid = int(gtx["goal_id"])
                amt = float(gtx["amount"] or 0)
                connection.execute(
                    "UPDATE goals SET saved_amount = MAX(0.0, saved_amount + ?) WHERE id = ?",
                    (amt, gid),
                )
            for table in (
                "transaction_attachments",
                "transaction_splits",
                "transaction_meta",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.execute("DELETE FROM transactions")
            try:
                connection.execute("DELETE FROM transaction_search_fts")
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).casefold():
                    raise
            try:
                connection.execute(
                    "UPDATE change_journal "
                    "SET status='undone', undone_at=CURRENT_TIMESTAMP "
                    "WHERE status='available'"
                )
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).casefold():
                    raise
            connection.execute("DELETE FROM sqlite_sequence WHERE name='transactions'")
            remaining = int(
                connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            )
            if remaining != 0:
                raise RuntimeError(
                    "A limpeza do histórico de movimentos ficou incompleta."
                )
            connection.execute("RELEASE SAVEPOINT clear_all_movements")
            connection.commit()
            return total
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT clear_all_movements")
            connection.execute("RELEASE SAVEPOINT clear_all_movements")
            connection.rollback()
            raise

    def transaction_by_id(self, tx_id: int) -> Optional[Any]:
        rows = self.q("SELECT * FROM transactions WHERE id=?", (tx_id,))
        return rows[0] if rows else None

    def transaction_splits(self, tx_id: int) -> list[Any]:
        return self.q(
            "SELECT category,category_id,percent,amount "
            "FROM transaction_splits WHERE transaction_id=? ORDER BY id",
            (int(tx_id),),
        )

    def transaction_splits_by_ids(self, tx_ids: Iterable[int]) -> dict[int, list[Any]]:
        """Return category splits mapped by transaction ID in a single query."""
        ids = [int(i) for i in tx_ids if int(i or 0) > 0]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.q(
            f"SELECT transaction_id,category,category_id,percent,amount "
            f"FROM transaction_splits WHERE transaction_id IN ({placeholders}) "
            f"ORDER BY transaction_id, id",
            tuple(ids),
        )
        splits_by_tx: dict[int, list[Any]] = {tx_id: [] for tx_id in ids}
        for row in rows:
            splits_by_tx[int(row["transaction_id"])].append(row)
        return splits_by_tx

    def transactions_with_splits(self) -> list[Any]:
        """Return every movement that currently has an applied category split."""
        return self.q(
            """
            SELECT t.*
            FROM transactions t
            WHERE EXISTS (
                SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id
            )
            ORDER BY t.date DESC, t.id DESC
            """
        )

    def _normalized_splits(
        self, splits: list[tuple[str, float]]
    ) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for category, pct in splits:
            category = str(category or "").strip()
            pct = safe_float(pct)
            if category and pct > 0:
                out.append((category, pct))
        total = sum(pct for _, pct in out)
        if total > 0 and abs(total - 100) > 0.05:
            out = [(category, pct / total * 100) for category, pct in out]
        return out

    def _allocate_split_amounts(
        self, amount: float, normalized: list[tuple[str, float]]
    ) -> list[tuple[str, float, float]]:
        """Allocate exact integer cents across splits using Largest Remainder (Hare-Niemeyer).

        Guarantees that sum(split_amounts) == round(amount, 2) without losing residual cents.
        """
        if not normalized:
            return []
        total_cents = int(round(abs(safe_float(amount)) * 100))
        if total_cents == 0:
            return [(cat, pct, 0.0) for cat, pct in normalized]

        total_pct = sum(pct for _, pct in normalized)
        if total_pct <= 0:
            total_pct = 100.0

        items: list[dict[str, Any]] = []
        for idx, (cat, pct) in enumerate(normalized):
            share = total_cents * (pct / total_pct)
            base = int(share)
            rem = share - base
            items.append({
                "idx": idx,
                "category": cat,
                "pct": pct,
                "base_cents": base,
                "rem": rem,
            })

        allocated = sum(item["base_cents"] for item in items)
        missing_cents = total_cents - allocated

        # Distribute missing cents to splits with highest remainder
        sorted_by_rem = sorted(
            items,
            key=lambda item: (item["rem"], item["pct"], -item["idx"]),
            reverse=True,
        )
        for i in range(min(missing_cents, len(sorted_by_rem))):
            sorted_by_rem[i]["base_cents"] += 1

        items.sort(key=lambda item: item["idx"])

        return [
            (
                item["category"],
                item["pct"],
                round(item["base_cents"] / 100.0, 2),
            )
            for item in items
        ]

    allocate_split_amounts = _allocate_split_amounts

    def save_transaction_splits(
        self,
        tx_id: int,
        splits: list[tuple[str, float]],
        *,
        commit: bool = True,
    ) -> None:
        row = self.transaction_by_id(tx_id)
        if row is None:
            raise ValueError(f"Transaction not found: {tx_id}")
        if str(row_get(row, "transfer_group_id", "") or ""):
            raise ValueError(
                "Uma transferência ligada não pode ser dividida por categorias."
            )
        amount = abs(safe_float(row_get(row, "amount")))
        normalized = self._normalized_splits(splits)
        if not normalized:
            # BUG-TOOL-02b: never persist zero-weight splits; when every split
            # is zero the stored split set must be cleared, not rewritten with
            # dead rows.
            connection = self.db.conn
            if commit:
                connection.commit()
                connection.execute("SAVEPOINT save_transaction_splits")
            try:
                connection.execute(
                    "DELETE FROM transaction_splits WHERE transaction_id=?",
                    (int(tx_id),),
                )
                if commit:
                    connection.execute("RELEASE SAVEPOINT save_transaction_splits")
                    connection.commit()
            except Exception:
                if commit:
                    connection.execute("ROLLBACK TO SAVEPOINT save_transaction_splits")
                    connection.execute("RELEASE SAVEPOINT save_transaction_splits")
                    connection.rollback()
                raise
            return
        if amount <= 0.0:
            raise ValueError("No valid splits to save.")
        allocated_splits = self._allocate_split_amounts(amount, normalized)
        connection = self.db.conn
        if commit:
            connection.commit()
            connection.execute("SAVEPOINT save_transaction_splits")
        try:
            connection.execute(
                "DELETE FROM transaction_splits WHERE transaction_id=?", (int(tx_id),)
            )
            for category, pct, split_amount in allocated_splits:
                category_id, category = self.category_identity(
                    category,
                    create=True,
                    kind="expense",
                )
                if not category_id:
                    raise ValueError(
                        f"A categoria «{category}» não tem uma identidade válida."
                    )
                connection.execute(
                    "INSERT INTO transaction_splits("
                    "transaction_id,category,category_id,percent,amount) "
                    "VALUES(?,?,?,?,?)",
                    (int(tx_id), category, category_id, pct, split_amount),
                )
            if commit:
                connection.execute("RELEASE SAVEPOINT save_transaction_splits")
                connection.commit()
        except Exception:
            if commit:
                connection.execute("ROLLBACK TO SAVEPOINT save_transaction_splits")
                connection.execute("RELEASE SAVEPOINT save_transaction_splits")
                connection.rollback()
            raise

    def update_transactions_category(
        self, tx_ids: list[int], category: str, *, commit: bool = True
    ) -> int:
        ids = [int(x) for x in tx_ids if x]
        category = str(category or "").strip()
        if not ids or not category:
            return 0
        if self.linked_transfer_ids(ids):
            raise ValueError("As transferências ligadas não podem mudar de categoria.")
        placeholders = ",".join("?" for _ in ids)
        expected_rows = self.q(
            f"SELECT DISTINCT CASE WHEN amount>0 THEN 'income' ELSE 'expense' END AS kind "
            f"FROM transactions WHERE id IN ({placeholders})",
            tuple(ids),
        )
        expected_kinds = {str(row["kind"]) for row in expected_rows}
        if len(expected_kinds) != 1:
            raise ValueError(
                "A seleção mistura receitas e despesas; altera cada tipo separadamente."
            )
        category = self.validate_transaction_category(
            category,
            1.0 if "income" in expected_kinds else -1.0,
        )
        category_id, category = self.category_identity(category)
        connection = self.db.conn
        if commit:
            connection.commit()
            connection.execute("SAVEPOINT update_transactions_category")
        try:
            changed_rows = connection.execute(
                f"SELECT id FROM transactions "
                f"WHERE id IN ({placeholders}) AND category<>?",
                (*ids, category),
            ).fetchall()
            changed_ids = [int(row["id"]) for row in changed_rows]
            if changed_ids:
                changed_placeholders = ",".join("?" for _ in changed_ids)
                connection.execute(
                    f"DELETE FROM transaction_splits "
                    f"WHERE transaction_id IN ({changed_placeholders})",
                    tuple(changed_ids),
                )
                connection.execute(
                    f"UPDATE transactions SET category=?,category_id=? "
                    f"WHERE id IN ({changed_placeholders})",
                    (category, category_id, *changed_ids),
                )
            if commit:
                connection.execute("RELEASE SAVEPOINT update_transactions_category")
                connection.commit()
            return len(changed_ids)
        except Exception:
            if commit:
                connection.execute("ROLLBACK TO SAVEPOINT update_transactions_category")
                connection.execute("RELEASE SAVEPOINT update_transactions_category")
                connection.rollback()
            raise

    def update_transactions_account(
        self, tx_ids: list[int], account: str, *, commit: bool = True
    ) -> int:
        ids = [int(x) for x in tx_ids if x]
        account_id, account = self.account_identity(account)
        if not ids:
            return 0
        if self.linked_transfer_ids(ids):
            raise ValueError(
                "Altera as contas através da edição da transferência completa."
            )
        placeholders = ",".join("?" for _ in ids)
        cur = self.db.conn.execute(
            f"UPDATE transactions SET account=?,account_id=? "
            f"WHERE id IN ({placeholders})",
            (account, account_id, *ids),
        )
        if commit:
            self.db.conn.commit()
        return int(cur.rowcount or 0)

    def add_tags_to_transactions(
        self, tx_ids: list[int], tags: str, *, commit: bool = True
    ) -> int:
        ids = [int(x) for x in tx_ids if x]
        new_tags = [
            t.strip() for t in str(tags or "").replace(";", ",").split(",") if t.strip()
        ]
        if not ids or not new_tags:
            return 0
        connection = self.db.conn
        if commit:
            connection.commit()
            connection.execute("SAVEPOINT add_tags_batch")
        changed = 0
        try:
            for tx_id in ids:
                meta = self.transaction_meta_ext(tx_id)
                current = [
                    value.strip()
                    for value in str(meta.get("tags", "")).split(",")
                    if value.strip()
                ]
                merged = list(dict.fromkeys([*current, *new_tags]))
                self._set_transaction_meta_ext(
                    tx_id,
                    tags=", ".join(merged),
                    merchant=str(meta.get("merchant", "")),
                    payment_method=str(meta.get("payment_method", "")),
                    status=str(meta.get("status", "")),
                    is_reimbursable=int(meta.get("is_reimbursable", 0) or 0),
                    subcategory=str(meta.get("subcategory", "")),
                    purchase_date=str(meta.get("purchase_date", "")),
                    payment_date=str(meta.get("payment_date", "")),
                )
                changed += 1
            if commit:
                connection.execute("RELEASE SAVEPOINT add_tags_batch")
                connection.commit()
            return changed
        except Exception:
            if commit:
                connection.execute("ROLLBACK TO SAVEPOINT add_tags_batch")
                connection.execute("RELEASE SAVEPOINT add_tags_batch")
                connection.rollback()
            raise

    def mark_transactions_reviewed(
        self, tx_ids: list[int], *, commit: bool = True
    ) -> int:
        ids = [int(x) for x in tx_ids if x]
        if not ids:
            return 0
        connection = self.db.conn
        if commit:
            connection.commit()
            connection.execute("SAVEPOINT mark_reviewed_batch")
        changed = 0
        try:
            for tx_id in ids:
                meta = self.transaction_meta_ext(tx_id)
                self._set_transaction_meta_ext(
                    tx_id,
                    tags=str(meta.get("tags", "")),
                    merchant=str(meta.get("merchant", "")),
                    payment_method=str(meta.get("payment_method", "")),
                    status="Revisto",
                    is_reimbursable=int(meta.get("is_reimbursable", 0) or 0),
                    subcategory=str(meta.get("subcategory", "")),
                    purchase_date=str(meta.get("purchase_date", "")),
                    payment_date=str(meta.get("payment_date", "")),
                )
                changed += 1
            if commit:
                connection.execute("RELEASE SAVEPOINT mark_reviewed_batch")
                connection.commit()
            return changed
        except Exception:
            if commit:
                connection.execute("ROLLBACK TO SAVEPOINT mark_reviewed_batch")
                connection.execute("RELEASE SAVEPOINT mark_reviewed_batch")
                connection.rollback()
            raise

    def category_rules(self, order_by: str = "match") -> list[Any]:
        if order_by == "keyword":
            return self.q("SELECT * FROM category_rules ORDER BY LOWER(keyword) ASC")
        return self.q(
            "SELECT * FROM category_rules ORDER BY LENGTH(keyword) DESC, keyword ASC"
        )

    def add_category_rule(
        self,
        keyword: str,
        category: str,
        splits: list[tuple[str, float]],
        *,
        commit: bool = True,
    ) -> None:
        keyword = str(keyword or "").strip().lower()
        category = str(category or "").strip()
        normalized = self._normalized_splits(splits)
        if not keyword:
            raise ValueError("Keyword is required.")
        if not category and normalized:
            category = normalized[0][0]
        category_id, category = self.category_identity(category or "Diversos")
        try:
            self.db.conn.execute(
                """
                INSERT INTO category_rules(keyword, category, category_id, split_json)
                VALUES(?,?,?,?)
                ON CONFLICT(keyword) DO UPDATE SET
                    category=excluded.category,
                    category_id=excluded.category_id,
                    split_json=excluded.split_json
                """,
                (
                    keyword,
                    category,
                    category_id,
                    json.dumps(normalized, ensure_ascii=False),
                ),
            )
            if commit:
                self.db.conn.commit()
        except Exception:
            if commit:
                self.db.conn.rollback()
            raise

    def apply_category_rules_to_transactions(
        self, tx_ids: list[int], *, commit: bool = True
    ) -> int:
        ids = [int(x) for x in tx_ids if x]
        if not ids:
            return 0
        connection = self.db.conn
        savepoint_name = f"apply_cat_rules_{uuid.uuid4().hex[:8]}"
        connection.execute(f"SAVEPOINT {savepoint_name}")
        changed = 0
        try:
            for tx_id in ids:
                row = self.transaction_by_id(tx_id)
                if row is None:
                    continue
                category = self.suggested_category_for_description(
                    str(row["description"] or "")
                )
                if category and category != str(row["category"] or ""):
                    connection.execute(
                        "DELETE FROM transaction_splits WHERE transaction_id=?",
                        (tx_id,),
                    )
                    category_id, category = self.category_identity(category)
                    connection.execute(
                        "UPDATE transactions SET category=?,category_id=? WHERE id=?",
                        (category, category_id, tx_id),
                    )
                    changed += 1
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if commit:
                connection.commit()
            return changed
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if commit:
                connection.rollback()
            raise

    def possible_duplicates(self, limit: Optional[int] = None) -> list[Any]:
        sql = """
            WITH candidates AS (
                SELECT COALESCE(NULLIF(m.payment_date,''), t.date) AS date_key,
                       LOWER(TRIM(t.description)) AS description_key,
                       ROUND(t.amount, 2) AS amount_key,
                       LOWER(TRIM(t.account)) AS account_key,
                       COALESCE(NULLIF(t.category,''), 'Outros') AS category_key,
                       COUNT(*) AS count,
                       GROUP_CONCAT(t.id) AS ids,
                       MIN(t.description) AS description,
                       MIN(t.category) AS category,
                       CASE
                         WHEN COUNT(DISTINCT NULLIF(LOWER(TRIM(m.external_reference)),''))=1
                          AND SUM(CASE WHEN TRIM(IFNULL(m.external_reference,''))<>'' THEN 1 ELSE 0 END)=COUNT(*)
                         THEN 'certa'
                         ELSE 'provável'
                       END AS confidence
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                GROUP BY date_key, description_key, amount_key, account_key
                HAVING COUNT(*) > 1
                UNION ALL
                SELECT COALESCE(NULLIF(m.payment_date,''), t.date) AS date_key,
                       '' AS description_key,
                       ROUND(t.amount, 2) AS amount_key,
                       LOWER(TRIM(t.account)) AS account_key,
                       COALESCE(NULLIF(MIN(t.category),''), 'Outros') AS category_key,
                       COUNT(*) AS count,
                       GROUP_CONCAT(t.id) AS ids,
                       GROUP_CONCAT(DISTINCT t.description) AS description,
                       MIN(t.category) AS category,
                       'possível' AS confidence
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                GROUP BY date_key, amount_key, account_key
                HAVING COUNT(*) > 1
                   AND COUNT(DISTINCT LOWER(TRIM(t.description))) > 1
            )
            SELECT * FROM candidates
            ORDER BY date_key DESC
            """
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, int(limit)),)
        return self.q(sql, params)

    def duplicate_classification(
        self,
        tx_date: str,
        description: str,
        amount: float,
        account: str = "",
        external_reference: str = "",
        merchant: str = "",
    ) -> dict[str, Any]:
        """Classify duplicate evidence without deleting or hiding uncertain rows."""

        account_name = (
            self.canonical_account_name(account, create=False)
            if str(account or "").strip()
            else ""
        )
        reference = str(external_reference or "").strip()
        base_params: list[Any] = []
        if reference:
            sql = """
                SELECT t.id,t.description,t.amount,
                       COALESCE(NULLIF(m.payment_date,''),t.date) AS date
                FROM transactions t
                JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE LOWER(TRIM(m.external_reference))=LOWER(TRIM(?))
            """
            base_params.append(reference)
            if account_name:
                sql += " AND t.account=? COLLATE NOCASE"
                base_params.append(account_name)
            sql += " ORDER BY t.id LIMIT 20"
            rows = self.q(sql, tuple(base_params))
            exact = [row for row in rows
                     if str(row["date"])[:10] == str(tx_date)[:10]
                     and round(float(row["amount"]), 2) == round(float(amount), 2)
                     and str(row["description"]).strip().casefold() == str(description).strip().casefold()]
            if exact:
                return {
                    "level": "certain",
                    "identity_match": True,
                    "label": "certa",
                    "reason": "A mesma referência bancária já existe nesta conta.",
                    "ids": [int(row["id"]) for row in exact],
                }

        date_key = str(tx_date or "")[:10]
        amount_value = round(float(amount or 0), 2)
        sql = """
            SELECT t.id,t.description,IFNULL(m.merchant,'') AS merchant
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)=?
              AND ROUND(t.amount,2)=ROUND(?,2)
        """
        params: list[Any] = [date_key, amount_value]
        if account_name:
            sql += " AND t.account=? COLLATE NOCASE"
            params.append(account_name)
        sql += " ORDER BY t.id LIMIT 50"
        candidates = self.q(sql, tuple(params))
        if not candidates:
            if reference and rows:
                return {"level": "possible", "label": "possível", "reason": "Referência reutilizada com dados diferentes; confirma antes de decidir.", "ids": [int(row["id"]) for row in rows]}
            return {"level": "none", "label": "", "reason": "", "ids": []}

        description_key = " ".join(str(description or "").split()).casefold()
        merchant_key = " ".join(str(merchant or "").split()).casefold()
        probable = [
            row
            for row in candidates
            if (
                description_key
                and " ".join(str(row["description"] or "").split()).casefold()
                == description_key
            )
            or (
                merchant_key
                and " ".join(str(row["merchant"] or "").split()).casefold()
                == merchant_key
            )
            or (
                description_key
                and len(description_key) >= 15
                and len(" ".join(str(row["description"] or "").split()).casefold()) >= 15
                and (
                    " ".join(str(row["description"] or "").split()).casefold().startswith(description_key)
                    or description_key.startswith(" ".join(str(row["description"] or "").split()).casefold())
                )
            )
        ]
        if probable:
            return {
                "level": "probable",
                "label": "provável",
                "reason": (
                    "Existe uma compra com a mesma data, valor, conta e descrição "
                    "ou comerciante. Confirma antes de decidir."
                ),
                "ids": [int(row["id"]) for row in probable],
            }
        return {
            "level": "possible",
            "label": "possível",
            "reason": (
                "Existe um movimento com a mesma data, valor e conta, mas texto "
                "diferente. Pode ser uma compra legítima."
            ),
            "ids": [int(row["id"]) for row in candidates],
        }

    def simple_duplicate_groups(self, limit: int = 5) -> list[Any]:
        return self.q(
            """
            SELECT COALESCE(NULLIF(m.payment_date,''),t.date) AS date,
                   t.description, t.amount, t.account, COUNT(*) AS c
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            GROUP BY COALESCE(NULLIF(m.payment_date,''),t.date),
                     t.description, t.amount, LOWER(TRIM(t.account))
            HAVING c>1
            ORDER BY date DESC
            LIMIT ?
            """,
            (int(limit),),
        )

    def is_duplicate_transaction(
        self,
        tx_date: str,
        description: str,
        amount: float,
        account: str = "",
        external_reference: str = "",
    ) -> bool:
        return (
            self.duplicate_classification(
                tx_date,
                description,
                amount,
                account,
                external_reference,
            )["level"]
            != "none"
        )

    def generic_category_count(self) -> int:
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM transactions WHERE category IN ('Diversos','Outros')",
                default=0,
            )
        )

    def transaction_data_quality_counts(
        self,
        start: str,
        end: str,
        attachment_threshold: float = 100.0,
    ) -> dict[str, int]:
        uncategorized = int(
            self.scalar(
                """
                SELECT COUNT(*)
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
                  AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
                  AND (t.category='' OR t.category IN ('Outros','Diversos'))
                """,
                (start, end),
            )
        )
        without_attachment = int(
            self.scalar(
                f"""
                SELECT COUNT(*)
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                LEFT JOIN (
                    SELECT transaction_id, COUNT(*) AS count
                    FROM transaction_attachments
                    GROUP BY transaction_id
                ) attachments ON attachments.transaction_id=t.id
                WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
                  AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
                  AND t.amount<0
                  AND {sql_external_financial_flow("t.category")}
                  AND ABS(t.amount)>=?
                  AND IFNULL(attachments.count, 0)=0
                """,
                (start, end, float(attachment_threshold)),
            )
        )
        invalid_dates = int(
            self.scalar(
                f"""
                SELECT COUNT(*)
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE NOT {sql_iso_date("t.date")}
                   OR NOT {sql_iso_date("m.payment_date", allow_empty=True)}
                   OR NOT {sql_iso_date("m.purchase_date", allow_empty=True)}
                """,
                default=0,
            )
        )
        invalid_amounts = int(
            self.scalar(
                f"""
                SELECT COUNT(*)
                FROM transactions t
                WHERE NOT {sql_finite_number("t.amount", non_zero=True)}
                """,
                default=0,
            )
        )
        category_inconsistencies = int(
            self.scalar(
                """
                SELECT COUNT(*)
                FROM transactions t
                LEFT JOIN categories c ON c.name=t.category
                WHERE t.category NOT IN ('Transferência Interna','Objetivos e Poupança')
                  AND (c.kind IS NULL OR c.kind<>CASE WHEN t.amount>0 THEN 'income' ELSE 'expense' END)
                """,
                default=0,
            )
        )
        return {
            "uncategorized_or_generic": uncategorized,
            "expenses_without_attachment": without_attachment,
            "invalid_dates": invalid_dates,
            "invalid_amounts": invalid_amounts,
            "category_inconsistencies": category_inconsistencies,
        }

    def transaction_context_sample(
        self,
        start: str,
        end: str,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        rows = self.q(
            """
            SELECT COALESCE(NULLIF(m.payment_date,''),t.date) AS date,
                   t.description, t.amount, t.category, t.account,
                   IFNULL(m.merchant,'') AS merchant,
                   IFNULL(m.payment_method,'') AS payment_method,
                   IFNULL(m.subcategory,'') AS subcategory
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE COALESCE(NULLIF(m.payment_date,''),t.date)>=?
              AND COALESCE(NULLIF(m.payment_date,''),t.date)<=?
            ORDER BY date DESC, t.id DESC
            LIMIT ?
            """,
            (start, end, max(1, int(limit))),
        )
        return [dict(row) for row in rows]

    def uncategorized_transactions(self, limit: int = 40) -> list[dict[str, Any]]:
        rows = self.q(
            """
            SELECT t.id, t.date, t.description, t.amount, t.category, t.account,
                   IFNULL(m.merchant,'') AS merchant,
                   IFNULL(m.payment_method,'') AS payment_method
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE t.category='' OR t.category IN ('Outros','Diversos')
            ORDER BY t.date DESC, t.id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        )
        return [dict(row) for row in rows]

    def _set_transaction_meta_ext(
        self,
        tx_id: int,
        *,
        tags: str = "",
        merchant: str = "",
        payment_method: str = "",
        status: str = "",
        is_reimbursable: int = 0,
        subcategory: str = "",
        purchase_date: str = "",
        payment_date: str = "",
        external_reference: str = "",
    ) -> None:
        if purchase_date:
            purchase_date = require_iso_date(purchase_date, field_name="data de compra")
        if payment_date:
            payment_date = require_iso_date(payment_date, field_name="data de pagamento")
        payment_month = (payment_date or "")[:7]
        self.db.conn.execute(
            """
            INSERT INTO transaction_meta(transaction_id,tags,merchant,payment_method,status,is_reimbursable,subcategory,purchase_date,payment_date,payment_month,external_reference,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(transaction_id) DO UPDATE SET
                tags=excluded.tags, merchant=excluded.merchant, payment_method=excluded.payment_method,
                status=excluded.status, is_reimbursable=excluded.is_reimbursable, subcategory=excluded.subcategory,
                purchase_date=excluded.purchase_date, payment_date=excluded.payment_date, payment_month=excluded.payment_month,
                external_reference=CASE WHEN excluded.external_reference <> '' THEN excluded.external_reference ELSE transaction_meta.external_reference END,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                tx_id,
                tags.strip(),
                merchant.strip(),
                payment_method.strip(),
                status.strip(),
                int(is_reimbursable or 0),
                subcategory.strip(),
                purchase_date[:10],
                payment_date[:10],
                payment_month,
                external_reference.strip(),
            ),
        )
        if subcategory.strip():
            row = self.db.conn.execute(
                "SELECT category,category_id FROM transactions WHERE id=?",
                (int(tx_id),),
            ).fetchone()
            if row is not None:
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO subcategories("
                    "category,name,category_id) VALUES(?,?,?)",
                    (
                        str(row["category"] or ""),
                        subcategory.strip(),
                        str(row["category_id"] or ""),
                    ),
                )

    def set_transaction_meta_ext(
        self,
        tx_id: int,
        *,
        tags: str = "",
        merchant: str = "",
        payment_method: str = "",
        status: str = "",
        is_reimbursable: int = 0,
        subcategory: str = "",
        purchase_date: str = "",
        payment_date: str = "",
    ) -> None:
        try:
            self._set_transaction_meta_ext(
                tx_id,
                tags=tags,
                merchant=merchant,
                payment_method=payment_method,
                status=status,
                is_reimbursable=is_reimbursable,
                subcategory=subcategory,
                purchase_date=purchase_date,
                payment_date=payment_date,
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def transaction_meta_ext(self, tx_id: int) -> dict[str, Any]:
        rows = self.q("SELECT * FROM transaction_meta WHERE transaction_id=?", (tx_id,))
        return dict(rows[0]) if rows else {}
