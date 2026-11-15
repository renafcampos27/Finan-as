"""Stable financial identities and non-destructive duplicate merging."""

from __future__ import annotations

import json
import uuid
from datetime import date
from difflib import SequenceMatcher
from typing import Any

from database.validation import sql_finite_number, sql_iso_date
from services.account_identity import account_identity_key, clean_account_name
from services.category_taxonomy import canonical_category, category_identity_key
from utils.helpers import row_get, safe_float


_FINANCIAL_DATE_SQL = "COALESCE(NULLIF(m.payment_date,''),t.date)"
_VALID_AMOUNT_SQL = sql_finite_number("t.amount", non_zero=True)
_VALID_DATE_SQL = sql_iso_date(_FINANCIAL_DATE_SQL)


class IdentityRepoMixin:
    """Resolve entities by opaque ID while retaining names for presentation."""

    def invalidate_identity_cache(self) -> None:
        """Compatibility hook for callers and any legacy instance-local entries.

        Identity results are no longer memoized: callers can roll back SQLite
        directly, including to a savepoint while in_transaction stays True.
        SQLite total_changes does not change on rollback either. Neither value
        is a safe cache version; positive IDs and negative results must always
        be resolved against the current connection's visible rows.
        """
        if hasattr(self, "_inst_account_identity_cache"):
            self._inst_account_identity_cache.clear()
        if hasattr(self, "_inst_category_identity_cache"):
            self._inst_category_identity_cache.clear()

    @staticmethod
    def _new_stable_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def canonical_account_name(
        self,
        name: object,
        *,
        create: bool = True,
    ) -> str:
        """Resolve current account display name, including historical aliases."""

        display = clean_account_name(name)
        key = account_identity_key(display)
        row = self.db.conn.execute(
            """
            SELECT name
            FROM financial_accounts
            WHERE is_active=1
              AND (account_key=? OR name=? COLLATE NOCASE)
            ORDER BY CASE WHEN name=? COLLATE NOCASE THEN 0 ELSE 1 END,id
            LIMIT 1
            """,
            (key, display, display),
        ).fetchone()
        if row is None:
            row = self.db.conn.execute(
                """
                SELECT target.name
                FROM entity_aliases alias
                JOIN financial_accounts target
                  ON target.stable_id=alias.entity_id
                 AND target.is_active=1
                WHERE alias.entity_type='account' AND alias.identity_key=?
                ORDER BY alias.id DESC
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if row is not None:
            return str(row["name"] or display)
        if not create:
            return display

        stable_id = self._new_stable_id("acc")
        self.db.conn.execute(
            """
            INSERT INTO financial_accounts(
                name,account_key,stable_id,is_active,balance_source
            ) VALUES(?,?,?,1,'')
            """,
            (display, key, stable_id),
        )
        self.db.conn.execute(
            """
            INSERT OR IGNORE INTO entity_aliases(
                entity_type,entity_id,alias,identity_key
            ) VALUES('account',?,?,?)
            """,
            (stable_id, display, key),
        )
        self.invalidate_identity_cache()
        return display

    def account_identity(
        self,
        name: object,
        *,
        create: bool = True,
    ) -> tuple[str, str]:
        """Resolve against current SQLite state without committing caller writes."""

        display = self.canonical_account_name(name, create=create)
        row = self.db.conn.execute(
            "SELECT stable_id,name FROM financial_accounts "
            "WHERE is_active=1 AND name=? COLLATE NOCASE LIMIT 1",
            (display,),
        ).fetchone()
        if row is None:
            res = ("", display)
        else:
            res = (str(row["stable_id"] or ""), str(row["name"] or display))
        return res

    def category_identity(
        self,
        name: object,
        *,
        active_only: bool = True,
        create: bool = False,
        kind: str = "expense",
    ) -> tuple[str, str]:
        """Resolve current rows/aliases; never retain a transaction-local result."""

        display = canonical_category(name)
        key = category_identity_key(display)
        # Internal bookkeeping categories carry both signs. Their shared
        # identity must not become an external income/expense category.
        internal_category = display in {"Transferência Interna", "Objetivos e Poupança"}
        selected_kind = "income" if str(kind).casefold() == "income" else "expense"
        active_sql = "AND is_active=1" if active_only else ""
        row = self.db.conn.execute(
            f"""
            SELECT stable_id,name,kind
            FROM categories
            WHERE (name=? COLLATE NOCASE OR identity_key=?) {active_sql}
            ORDER BY CASE WHEN name=? COLLATE NOCASE THEN 0 ELSE 1 END,rowid
            LIMIT 1
            """,
            (display, key, display),
        ).fetchone()
        if row is not None and create and not internal_category and str(row["kind"]).casefold() != selected_kind:
            row = None
        if row is None:
            row = self.db.conn.execute(
                f"""
                SELECT target.stable_id,target.name,target.kind
                FROM entity_aliases alias
                JOIN categories target ON target.stable_id=alias.entity_id
                WHERE alias.entity_type='category'
                  AND alias.identity_key=? {active_sql.replace("is_active", "target.is_active")}
                ORDER BY alias.id DESC
                LIMIT 1
                """,
                (key,),
            ).fetchone()
            if row is not None and create and not internal_category and str(row["kind"]).casefold() != selected_kind:
                row = None
        if row is None:
            if not create:
                return "", display
            selected_kind = "income" if str(kind).casefold() == "income" else "expense"
            existing = self.db.conn.execute(
                """
                SELECT stable_id, name, kind, is_active
                FROM categories
                WHERE name=? COLLATE NOCASE
                LIMIT 1
                """,
                (display,),
            ).fetchone()
            if existing is not None:
                if not internal_category and str(existing["kind"]).casefold() != selected_kind:
                    suffix = "Receita" if selected_kind == "income" else "Despesa"
                    disambiguated = f"{display} ({suffix})"
                    counter = 2
                    while self.db.conn.execute(
                        "SELECT 1 FROM categories WHERE name=? COLLATE NOCASE",
                        (disambiguated,),
                    ).fetchone():
                        disambiguated = f"{display} ({suffix} {counter})"
                        counter += 1
                    display = disambiguated
                    key = category_identity_key(display)
                elif not existing["is_active"]:
                    self.db.conn.execute(
                        "UPDATE categories SET is_active=1 WHERE stable_id=?",
                        (existing["stable_id"],),
                    )
                    self.invalidate_identity_cache()
                    return str(existing["stable_id"]), str(existing["name"])
                else:
                    return str(existing["stable_id"]), str(existing["name"])

            stable_id = self._new_stable_id("cat")
            self.db.conn.execute(
                """
                INSERT INTO categories(
                    name,kind,colour,budget,budget_bucket,stable_id,
                    identity_key,is_active
                ) VALUES(?,?,?,0,'',?,?,1)
                """,
                (display, selected_kind, "#94A3B8", stable_id, key),
            )
            self.db.conn.execute(
                """
                INSERT OR IGNORE INTO entity_aliases(
                    entity_type,entity_id,alias,identity_key
                ) VALUES('category',?,?,?)
                """,
                (stable_id, display, key),
            )
            self.invalidate_identity_cache()
            return stable_id, display
        res = (str(row["stable_id"] or ""), str(row["name"] or display))
        return res

    def entity_identity_rows(
        self,
        entity_type: str,
        *,
        include_merged: bool = False,
    ) -> list[dict[str, Any]]:
        selected = str(entity_type or "").strip().casefold()
        active_clause = "" if include_merged else "WHERE is_active=1"
        if selected == "account":
            rows = self.q(
                f"""
                SELECT stable_id,name,is_active,merged_into_id,balance_known,
                       balance_source,reference_balance,reference_date
                FROM financial_accounts
                {active_clause}
                ORDER BY is_active DESC,name COLLATE NOCASE
                """
            )
            tx_usage = {
                str(r["account_id"]): int(r["c"])
                for r in self.q(
                    "SELECT account_id, COUNT(*) AS c FROM transactions WHERE account_id<>'' GROUP BY account_id"
                )
            }
            liab_usage = {
                str(r["account_id"]): int(r["c"])
                for r in self.q(
                    "SELECT account_id, COUNT(*) AS c FROM liabilities WHERE account_id<>'' GROUP BY account_id"
                )
            }
            result: list[dict[str, Any]] = []
            for row in rows:
                stable_id = str(row["stable_id"] or "")
                usage = tx_usage.get(stable_id, 0) + liab_usage.get(stable_id, 0)
                result.append({**dict(row), "usage_count": int(usage)})
            return result
        if selected == "category":
            rows = self.q(
                f"""
                SELECT stable_id,name,kind,colour,budget,budget_bucket,
                       identity_key,is_active,merged_into_id
                FROM categories
                {active_clause}
                ORDER BY kind,name COLLATE NOCASE
                """
            )
            tx_cat_usage = {
                str(r["category_id"]): int(r["c"])
                for r in self.q(
                    "SELECT category_id, COUNT(*) AS c FROM transactions WHERE category_id<>'' GROUP BY category_id"
                )
            }
            split_cat_usage = {
                str(r["category_id"]): int(r["c"])
                for r in self.q(
                    "SELECT category_id, COUNT(*) AS c FROM transaction_splits WHERE category_id<>'' GROUP BY category_id"
                )
            }
            result = []
            for row in rows:
                stable_id = str(row["stable_id"] or "")
                usage = tx_cat_usage.get(stable_id, 0) + split_cat_usage.get(
                    stable_id, 0
                )
                result.append({**dict(row), "usage_count": int(usage)})
            return result

        if selected == "asset":
            return [dict(row) for row in self.q("SELECT * FROM holdings ORDER BY name")]
        raise ValueError("Tipo de identidade inválido.")

    def identity_integrity_summary(self) -> dict[str, Any]:
        """Return visible counts and any missing/orphaned stable relationships."""

        checks = {
            "transactions_without_category_id": self.scalar(
                "SELECT COUNT(*) FROM transactions WHERE TRIM(category_id)=''",
                default=0,
            ),
            "transactions_without_account_id": self.scalar(
                "SELECT COUNT(*) FROM transactions WHERE TRIM(account_id)=''",
                default=0,
            ),
            "orphan_category_links": self.scalar(
                """
                SELECT COUNT(*) FROM transactions t
                LEFT JOIN categories c ON c.stable_id=t.category_id
                WHERE t.category_id<>'' AND c.stable_id IS NULL
                """,
                default=0,
            ),
            "orphan_account_links": self.scalar(
                """
                SELECT COUNT(*) FROM transactions t
                LEFT JOIN financial_accounts a ON a.stable_id=t.account_id
                WHERE t.account_id<>'' AND a.stable_id IS NULL
                """,
                default=0,
            ),
            "liabilities_without_account_id": self.scalar(
                "SELECT COUNT(*) FROM liabilities "
                "WHERE TRIM(account)<>'' AND TRIM(account_id)=''",
                default=0,
            ),
            "orphan_liability_account_links": self.scalar(
                """
                SELECT COUNT(*) FROM liabilities l
                LEFT JOIN financial_accounts a ON a.stable_id=l.account_id
                WHERE l.account_id<>'' AND a.stable_id IS NULL
                """,
                default=0,
            ),
        }
        problems = sum(int(value or 0) for value in checks.values())
        return {
            "active_accounts": len(self.entity_identity_rows("account")),
            "active_categories": len(self.entity_identity_rows("category")),
            "assets": len(self.entity_identity_rows("asset")),
            "linked_transfers": int(
                self.scalar(
                    "SELECT COUNT(DISTINCT transfer_group_id) FROM transactions "
                    "WHERE transfer_group_id<>''",
                    default=0,
                )
            ),
            "problems": problems,
            "checks": checks,
        }

    def identity_merge_suggestions(self, entity_type: str) -> list[dict[str, Any]]:
        rows = self.entity_identity_rows(entity_type)
        suggestions: list[dict[str, Any]] = []
        for index, source in enumerate(rows):
            source_key = (
                account_identity_key(source["name"])
                if entity_type == "account"
                else category_identity_key(source["name"])
            )
            for target in rows[index + 1 :]:
                if entity_type == "category" and source.get("kind") != target.get(
                    "kind"
                ):
                    continue
                target_key = (
                    account_identity_key(target["name"])
                    if entity_type == "account"
                    else category_identity_key(target["name"])
                )
                score = SequenceMatcher(None, source_key, target_key).ratio()
                if source_key == target_key:
                    score = 1.0
                if score < 0.78:
                    continue
                suggestions.append(
                    {
                        "source_id": source["stable_id"],
                        "source_name": source["name"],
                        "target_id": target["stable_id"],
                        "target_name": target["name"],
                        "similarity_pct": round(score * 100),
                    }
                )
        return sorted(
            suggestions,
            key=lambda item: (-int(item["similarity_pct"]), item["source_name"]),
        )

    def _identity_row(self, entity_type: str, stable_id: str) -> Any:
        table = "financial_accounts" if entity_type == "account" else "categories"
        row = self.db.conn.execute(
            f"SELECT * FROM {table} WHERE stable_id=? AND is_active=1",
            (str(stable_id or ""),),
        ).fetchone()
        if row is None:
            raise ValueError("A origem ou o destino já não está disponível.")
        return row

    def identity_merge_preview(
        self,
        entity_type: str,
        source_id: str,
        target_id: str,
    ) -> dict[str, Any]:
        selected = str(entity_type or "").strip().casefold()
        if selected not in {"account", "category"}:
            raise ValueError("Só é possível fundir contas ou categorias.")
        if not source_id or source_id == target_id:
            raise ValueError("Seleciona uma origem e um destino diferentes.")
        source = self._identity_row(selected, source_id)
        target = self._identity_row(selected, target_id)
        if selected == "category" and str(source["kind"]) != str(target["kind"]):
            raise ValueError("Só podes fundir categorias do mesmo tipo financeiro.")

        if selected == "account":
            specifications = (
                ("transactions", "account_id"),
                ("subscriptions", "account_id"),
                ("scheduled_payments", "account_id"),
                ("holdings", "linked_account_id"),
                ("liabilities", "account_id"),
            )
        else:
            specifications = (
                ("transactions", "category_id"),
                ("transaction_splits", "category_id"),
                ("subscriptions", "category_id"),
                ("scheduled_payments", "category_id"),
                ("category_rules", "category_id"),
                ("monthly_category_budgets", "category_id"),
                ("monthly_goals", "category_id"),
                ("subcategories", "category_id"),
            )
        affected = {
            table: int(
                self.scalar(
                    f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
                    (source_id,),
                    default=0,
                )
            )
            for table, column in specifications
        }
        warnings: list[str] = []
        if selected == "account":
            source_known = bool(int(row_get(source, "balance_known", 0) or 0))
            target_known = bool(int(row_get(target, "balance_known", 0) or 0))
            if not source_known or not target_known:
                warnings.append(
                    "Pelo menos um saldo inicial não está confirmado; o saldo "
                    "consolidado ficará identificado como estimado a partir do histórico."
                )
            elif str(source["reference_date"] or "") != str(
                target["reference_date"] or ""
            ):
                warnings.append(
                    "Os saldos de referência têm datas diferentes; será criado um "
                    "saldo consolidado com a data de hoje."
                )
        return {
            "entity_type": selected,
            "source": dict(source),
            "target": dict(target),
            "affected": affected,
            "affected_total": sum(affected.values()),
            "warnings": warnings,
        }

    def _account_balance_at(self, row: Any, as_of: str) -> tuple[float, bool]:
        stable_id = str(row["stable_id"] or "")
        name = str(row["name"] or "Conta principal")
        reference_date = str(row["reference_date"] or "")[:10]
        known = bool(int(row["balance_known"] or 0)) and bool(reference_date)
        identity_where = (
            "(t.account_id=? OR (t.account_id='' AND t.account=? COLLATE NOCASE))"
        )
        if known:
            if reference_date <= as_of:
                movements = self.scalar(
                    f"""
                    SELECT COALESCE(SUM(t.amount),0)
                    FROM transactions t
                    LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                    WHERE {identity_where} AND {_FINANCIAL_DATE_SQL}>?
                      AND {_FINANCIAL_DATE_SQL}<=? AND {_VALID_AMOUNT_SQL}
                      AND {_VALID_DATE_SQL}
                    """,
                    (stable_id, name, reference_date, as_of),
                    default=0,
                )
                return safe_float(row["reference_balance"]) + movements, True
            else:
                movements = self.scalar(
                    f"""
                    SELECT COALESCE(SUM(t.amount),0)
                    FROM transactions t
                    LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                    WHERE {identity_where} AND {_FINANCIAL_DATE_SQL}>?
                      AND {_FINANCIAL_DATE_SQL}<=? AND {_VALID_AMOUNT_SQL}
                      AND {_VALID_DATE_SQL}
                    """,
                    (stable_id, name, as_of, reference_date),
                    default=0,
                )
                return safe_float(row["reference_balance"]) - movements, True
        movements = self.scalar(
            f"""
            SELECT COALESCE(SUM(t.amount),0)
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE {identity_where} AND {_FINANCIAL_DATE_SQL}<=?
              AND {_VALID_AMOUNT_SQL} AND {_VALID_DATE_SQL}
            """,
            (stable_id, name, as_of),
            default=0,
        )
        return movements, False

    @staticmethod
    def _replace_category_in_json(value: object, source: str, target: str) -> str:
        try:
            payload = json.loads(str(value or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return str(value or "")

        def replace(item: Any) -> Any:
            if isinstance(item, str):
                return target if item == source else item
            if isinstance(item, list):
                return [replace(child) for child in item]
            if isinstance(item, dict):
                return {key: replace(child) for key, child in item.items()}
            return item

        return json.dumps(replace(payload), ensure_ascii=False, separators=(",", ":"))

    def merge_financial_identities(
        self,
        entity_type: str,
        source_id: str,
        target_id: str,
    ) -> dict[str, Any]:
        """Atomically merge duplicates and retain the source identity as history."""

        preview = self.identity_merge_preview(entity_type, source_id, target_id)
        selected = str(preview["entity_type"])
        source = preview["source"]
        target = preview["target"]
        source_name = str(source["name"])
        target_name = str(target["name"])
        connection = self.db.conn
        connection.execute("SAVEPOINT merge_financial_identity")

        try:
            if selected == "account":
                today = date.today().isoformat()
                source_balance, source_known = self._account_balance_at(source, today)
                target_balance, target_known = self._account_balance_at(target, today)
                for table, column, display_column in (
                    ("transactions", "account_id", "account"),
                    ("subscriptions", "account_id", "account"),
                    ("scheduled_payments", "account_id", "account"),
                    ("holdings", "linked_account_id", "linked_account"),
                    ("liabilities", "account_id", "account"),
                ):
                    connection.execute(
                        f"UPDATE {table} SET {column}=?,{display_column}=? "
                        f"WHERE {column}=? OR ({column}='' AND {display_column}=? COLLATE NOCASE)",
                        (target_id, target_name, source_id, source_name),
                    )
                connection.execute(
                    """
                    UPDATE financial_accounts
                    SET reference_balance=?,reference_date=?,balance_known=1,
                        balance_source=?,updated_at=CURRENT_TIMESTAMP
                    WHERE stable_id=?
                    """,
                    (
                        source_balance + target_balance,
                        today,
                        "merged_confirmed"
                        if source_known and target_known
                        else "merged_derived",
                        target_id,
                    ),
                )
                alias_key = account_identity_key(source_name)
                table = "financial_accounts"
            else:
                for table_name, column, display_column in (
                    ("transactions", "category_id", "category"),
                    ("transaction_splits", "category_id", "category"),
                    ("subscriptions", "category_id", "category"),
                    ("scheduled_payments", "category_id", "category"),
                    ("category_rules", "category_id", "category"),
                    ("monthly_goals", "category_id", "category"),
                ):
                    connection.execute(
                        f"UPDATE {table_name} SET {column}=?,{display_column}=? "
                        f"WHERE {column}=? OR ({column}='' AND {display_column}=?)",
                        (target_id, target_name, source_id, source_name),
                    )

                consolidated_splits = 0
                duplicate_split_groups = connection.execute(
                    """
                    SELECT transaction_id,MIN(id) AS keep_id,
                           SUM(percent) AS total_percent,SUM(amount) AS total_amount,
                           COUNT(*) AS row_count
                    FROM transaction_splits
                    WHERE category_id=?
                    GROUP BY transaction_id,category_id
                    HAVING COUNT(*)>1
                    """,
                    (target_id,),
                ).fetchall()
                for group in duplicate_split_groups:
                    keep_id = int(group["keep_id"])
                    transaction_id = int(group["transaction_id"])
                    row_count = int(group["row_count"] or 0)
                    connection.execute(
                        "DELETE FROM transaction_splits "
                        "WHERE transaction_id=? AND category_id=? AND id<>?",
                        (transaction_id, target_id, keep_id),
                    )
                    connection.execute(
                        "UPDATE transaction_splits "
                        "SET category=?,category_id=?,percent=?,amount=? WHERE id=?",
                        (
                            target_name,
                            target_id,
                            safe_float(group["total_percent"]),
                            safe_float(group["total_amount"]),
                            keep_id,
                        ),
                    )
                    consolidated_splits += max(0, row_count - 1)

                source_budgets = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM monthly_category_budgets "
                        "WHERE category_id=? OR (category_id='' AND category=?)",
                        (source_id, source_name),
                    ).fetchall()
                ]
                for budget in source_budgets:
                    connection.execute(
                        """
                        INSERT INTO monthly_category_budgets(
                            month,category,budget,updated_at,category_id
                        ) VALUES(?,?,?,CURRENT_TIMESTAMP,?)
                        ON CONFLICT(month,category) DO UPDATE SET
                            budget=monthly_category_budgets.budget+excluded.budget,
                            updated_at=CURRENT_TIMESTAMP,
                            category_id=excluded.category_id
                        """,
                        (
                            budget["month"],
                            target_name,
                            safe_float(budget["budget"]),
                            target_id,
                        ),
                    )
                connection.execute(
                    "DELETE FROM monthly_category_budgets "
                    "WHERE category_id=? OR category=?",
                    (source_id, source_name),
                )

                source_subcategories = [
                    str(row["name"])
                    for row in connection.execute(
                        "SELECT name FROM subcategories "
                        "WHERE category_id=? OR category=?",
                        (source_id, source_name),
                    ).fetchall()
                ]
                for subcategory in source_subcategories:
                    connection.execute(
                        "INSERT OR IGNORE INTO subcategories(category,name,category_id) "
                        "VALUES(?,?,?)",
                        (target_name, subcategory, target_id),
                    )
                connection.execute(
                    "DELETE FROM subcategories WHERE category_id=? OR category=?",
                    (source_id, source_name),
                )
                for rule in connection.execute(
                    "SELECT id,split_json FROM category_rules"
                ).fetchall():
                    updated = self._replace_category_in_json(
                        rule["split_json"], source_name, target_name
                    )
                    if updated != str(rule["split_json"] or ""):
                        connection.execute(
                            "UPDATE category_rules SET split_json=? WHERE id=?",
                            (updated, int(rule["id"])),
                        )
                connection.execute(
                    "UPDATE categories SET budget=budget+? WHERE stable_id=?",
                    (safe_float(source["budget"]), target_id),
                )
                preview["source_monthly_budgets"] = source_budgets
                preview["source_subcategories"] = source_subcategories
                preview["consolidated_split_rows"] = consolidated_splits
                alias_key = category_identity_key(source_name)
                table = "categories"

            connection.execute(
                """
                INSERT OR IGNORE INTO entity_aliases(
                    entity_type,entity_id,alias,identity_key
                ) VALUES(?,?,?,?)
                """,
                (selected, target_id, source_name, alias_key),
            )
            # BUG-AUD-C127: historical aliases registered on the absorbed
            # entity must follow the merge.  ``canonical_account_name``/
            # ``category_identity`` only join ``entity_aliases`` against
            # ACTIVE targets, so an alias left pointing at the deactivated
            # source id is invisible and the next import re-creates the
            # duplicate entity it was supposed to prevent.  Rows whose
            # (entity_type, entity_id, identity_key) would collide with an
            # alias the target already owns are dropped first: the target's
            # own alias already carries that lookup key, so the historical
            # label loses nothing that is not still resolvable.
            connection.execute(
                """
                DELETE FROM entity_aliases
                WHERE entity_type=? AND entity_id=?
                  AND identity_key IN (
                      SELECT identity_key FROM entity_aliases
                      WHERE entity_type=? AND entity_id=?
                  )
                """,
                (selected, source_id, selected, target_id),
            )
            connection.execute(
                """
                UPDATE entity_aliases
                SET entity_id=?
                WHERE entity_type=? AND entity_id=?
                """,
                (target_id, selected, source_id),
            )
            connection.execute(
                f"UPDATE {table} SET is_active=0,merged_into_id=? WHERE stable_id=?",
                (target_id, source_id),
            )
            connection.execute(
                """
                INSERT INTO entity_merges(
                    entity_type,source_id,target_id,source_name,target_name,affected_json
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    selected,
                    source_id,
                    target_id,
                    source_name,
                    target_name,
                    json.dumps(preview, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            connection.execute("RELEASE SAVEPOINT merge_financial_identity")
            connection.commit()
            self.invalidate_identity_cache()
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT merge_financial_identity")
            connection.execute("RELEASE SAVEPOINT merge_financial_identity")
            connection.rollback()
            raise
        return preview

    def identity_merge_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.q(
                "SELECT id,entity_type,source_id,target_id,source_name,target_name,"
                "affected_json,created_at FROM entity_merges "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(500, int(limit or 50))),),
            )
        ]


__all__ = ["IdentityRepoMixin"]
