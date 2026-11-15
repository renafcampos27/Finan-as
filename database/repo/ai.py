#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from utils.app_logging import log_exception
from utils.helpers import safe_float


class AiRepoMixin:
    def save_ai_chat_history(
        self, *, month: str, action: str, question: str, prompt: str, response: str
    ) -> int:
        """Persist a Gemini interaction so it can be reviewed later by month."""
        settings = self.ai_settings()
        try:
            cur = self.db.conn.execute(
                """
                INSERT INTO ai_chat_history(month, provider, model, action, question, prompt, response)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    str(month or ""),
                    "Google Gemini",
                    str(settings.get("model", "")),
                    str(action or ""),
                    str(question or ""),
                    str(prompt or ""),
                    str(response or ""),
                ),
            )
            self.db.conn.commit()
            return int(cur.lastrowid)
        except Exception:
            self.db.conn.rollback()
            raise

    def ai_chat_history_months(self) -> list[str]:
        rows = self.q(
            "SELECT DISTINCT month FROM ai_chat_history WHERE month<>'' ORDER BY month DESC"
        )
        months = [str(r["month"]) for r in rows if str(r["month"] or "").strip()]
        current = f"{date.today().year}-{date.today().month:02d}"
        if current not in months:
            months.insert(0, current)
        return months

    def ai_chat_history(
        self, month: str = "", limit: Optional[int] = None, offset: int = 0
    ) -> list[Any]:
        where: list[str] = []
        params: list[Any] = []
        if month and month != "Todos":
            where.append("month=?")
            params.append(str(month))
        sql = """
            SELECT id, created_at, month, provider, model, action, question, response
            FROM ai_chat_history
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
            if int(offset) > 0:
                sql += " OFFSET ?"
                params.append(max(0, int(offset)))
        elif int(offset) > 0:
            sql += " LIMIT -1 OFFSET ?"
            params.append(max(0, int(offset)))
        return self.q(sql, tuple(params))

    def ai_chat_history_count(self, month: str = "") -> int:
        if month and month != "Todos":
            return int(
                self.scalar(
                    "SELECT COUNT(*) FROM ai_chat_history WHERE month=?",
                    (str(month),),
                    default=0,
                )
            )
        return int(self.scalar("SELECT COUNT(*) FROM ai_chat_history", default=0))

    def ai_chat_history_entry(self, entry_id: int) -> Optional[Any]:
        rows = self.q("SELECT * FROM ai_chat_history WHERE id=?", (int(entry_id),))
        return rows[0] if rows else None

    def delete_ai_chat_history_entry(self, entry_id: int) -> None:
        try:
            self.db.conn.execute(
                "DELETE FROM ai_chat_history WHERE id=?", (int(entry_id),)
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def clear_ai_chat_history_month(self, month: str) -> None:
        if not month or month == "Todos":
            return
        try:
            self.db.conn.execute(
                "DELETE FROM ai_chat_history WHERE month=?", (str(month),)
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def ai_settings(self) -> dict[str, Any]:
        """Read persisted non-secret AI settings and the legacy migration state."""
        return {
            "enabled": self.setting("ai_gemini_enabled", "0") == "1",
            "use_env": self.setting("ai_gemini_use_env", "1") == "1",
            "legacy_api_key": self.setting("ai_gemini_api_key", ""),
            "secret_migration_done": self.setting("ai_secret_migration_v1", "0") == "1",
            "model": self.setting("ai_gemini_model", "gemini-2.5-flash"),
            "privacy_aggregated": self.setting("ai_privacy_aggregated", "1") == "1",
            "anonymize": self.setting("ai_anonymize", "1") == "1",
            "ask_before_details": self.setting("ai_ask_before_details", "1") == "1",
        }

    def clear_legacy_ai_api_key(self) -> None:
        self.set_setting("ai_gemini_api_key", "")

    def mark_ai_secret_migrated(self) -> None:
        self.set_setting("ai_secret_migration_v1", "1")

    def save_ai_settings(
        self,
        *,
        enabled: bool,
        use_env: bool,
        model: str,
        privacy_aggregated: bool,
        anonymize: bool,
        ask_before_details: bool,
        clear_legacy_api_key: bool = False,
        secret_migration_done: Optional[bool] = None,
    ) -> None:
        values = [
            ("ai_gemini_enabled", "1" if enabled else "0"),
            ("ai_gemini_use_env", "1" if use_env else "0"),
            ("ai_gemini_model", str(model or "gemini-2.5-flash").strip()),
            ("ai_privacy_aggregated", "1" if privacy_aggregated else "0"),
            ("ai_anonymize", "1" if anonymize else "0"),
            ("ai_ask_before_details", "1" if ask_before_details else "0"),
        ]
        if clear_legacy_api_key:
            values.append(("ai_gemini_api_key", ""))
        if secret_migration_done is not None:
            values.append(
                (
                    "ai_secret_migration_v1",
                    "1" if secret_migration_done else "0",
                )
            )
        connection = self.db.conn
        connection.commit()
        connection.execute("SAVEPOINT save_ai_preferences")
        try:
            connection.executemany(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                values,
            )
            connection.execute("RELEASE SAVEPOINT save_ai_preferences")
            connection.commit()
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT save_ai_preferences")
            connection.execute("RELEASE SAVEPOINT save_ai_preferences")
            connection.rollback()
            raise

    def suggested_category_for_description(self, description: str) -> str:
        text = str(description or "").casefold()
        for rule in self.category_rules():
            keyword = str(rule["keyword"] or "").strip().casefold()
            if keyword and str(rule["category"] or "").strip():
                if len(keyword) <= 3:
                    pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
                    if re.search(pattern, text):
                        return str(rule["category"]).strip()
                elif keyword in text:
                    return str(rule["category"]).strip()
        return ""

    def suggest_split(
        self, description: str, amount: float = 0
    ) -> list[tuple[str, float]]:
        try:
            return self.db.suggest_split(description, amount)
        except Exception as exc:
            log_exception("FinanceDB.suggest_split failed; using Repo fallback", exc)
        category = self.suggested_category_for_description(description)
        if not category:
            category = "Rendimentos" if safe_float(amount) > 0 else "Diversos"
        return [(category, 100.0)]
