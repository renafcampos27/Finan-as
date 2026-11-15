"""Explicit administrative preferences never commit caller-owned ledger work."""

from __future__ import annotations

import json
import uuid
from typing import Any


class ProductReviewRepoMixin:
    def save_administrative_snoozes(self, values: dict[str, Any]) -> None:
        self._save_administrative_preferences({"administrative_snoozes_v1": values})

    def save_dismissed_financial_alerts(self, values: list[str]) -> None:
        self._save_administrative_preferences({"dismissed_financial_alerts": values})

    def reset_administrative_choices(self) -> None:
        self._save_administrative_preferences(
            {"administrative_snoozes_v1": {}, "dismissed_financial_alerts": []}
        )

    def _save_administrative_preferences(self, values: dict[str, Any]) -> None:
        connection = self.db.conn
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        savepoint = "administrative_snooze_" + uuid.uuid4().hex
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            for key, value in values.items():
                connection.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
                )
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            if owns_transaction:
                connection.commit()
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            if owns_transaction:
                connection.rollback()
            raise
