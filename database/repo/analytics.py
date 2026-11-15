#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Optional

from database.validation import (
    sql_external_financial_flow,
    sql_finite_number,
    sql_iso_date,
)
from utils.output_security import safe_hex_colour
from utils.helpers import row_get, safe_float


_FINANCIAL_DATE_SQL = "COALESCE(NULLIF(m.payment_date,''),t.date)"
_VALID_TRANSACTION_AMOUNT_SQL = sql_finite_number("t.amount", non_zero=True)
_VALID_FINANCIAL_DATE_SQL = sql_iso_date(_FINANCIAL_DATE_SQL)
_EXTERNAL_FLOW_SQL = sql_external_financial_flow("t.category")


class AnalyticsRepoMixin:
    def account_balances(self, as_of: date | str | None = None) -> list[dict[str, Any]]:
        return self.db.account_balances(as_of)

    def cash_balance(self, as_of: date | str | None = None) -> float:
        return self.db.cash_balance(as_of)

    def net_worth(self) -> float:
        return self.db.net_worth()

    def totals_for_payment_period(self, start: str, end: str) -> dict[str, Any]:
        row = self.db.conn.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN t.amount > 0 AND {_EXTERNAL_FLOW_SQL} THEN t.amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN t.amount < 0 AND {_EXTERNAL_FLOW_SQL} THEN ABS(t.amount) ELSE 0 END), 0) AS expenses
            FROM transactions t

            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE {_FINANCIAL_DATE_SQL} >= ? AND {_FINANCIAL_DATE_SQL} <= ?
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            """,
            (start, end),
        ).fetchone()
        income = safe_float(row["income"]) if row else 0.0
        expenses = safe_float(row["expenses"]) if row else 0.0
        savings = income - expenses

        investment_state = self.investment_state_as_of(end)
        invest = float(investment_state.get("total") or 0)
        return {
            "income": income,
            "expenses": expenses,
            "savings": savings,
            "investments": invest,
            "investments_available": bool(investment_state.get("available")),
            "investment_as_of": str(investment_state.get("as_of") or ""),
            "investment_source": str(investment_state.get("source") or "unavailable"),
        }

    def monthly_totals_grouped_for_year(self, year: int) -> dict[str, dict[str, float]]:
        """Fetch all 12 months aggregated income and expenses in a single grouped query."""
        start_bound = f"{year:04d}-01-01"
        end_bound = f"{year:04d}-12-31"
        rows = self.q(
            f"""
            SELECT
                SUBSTR({_FINANCIAL_DATE_SQL}, 1, 7) AS month_key,
                SUM(CASE WHEN t.amount > 0 AND {_EXTERNAL_FLOW_SQL} THEN t.amount ELSE 0 END) AS income,
                SUM(CASE WHEN t.amount < 0 AND {_EXTERNAL_FLOW_SQL} THEN ABS(t.amount) ELSE 0 END) AS expenses
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE {_FINANCIAL_DATE_SQL} >= ? AND {_FINANCIAL_DATE_SQL} <= ?
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {_VALID_FINANCIAL_DATE_SQL}
            GROUP BY month_key
            """,
            (start_bound, end_bound),
        )
        by_month: dict[str, dict[str, float]] = {
            f"{year:04d}-{m:02d}": {"income": 0.0, "expenses": 0.0, "savings": 0.0}
            for m in range(1, 13)
        }
        for r in rows:
            mk = str(row_get(r, "month_key", "") or "")
            if mk in by_month:
                inc = safe_float(row_get(r, "income", 0.0))
                exp = safe_float(row_get(r, "expenses", 0.0))
                by_month[mk] = {
                    "income": inc,
                    "expenses": exp,
                    "savings": inc - exp,
                }
        return by_month

    def top_categories_payment_period(
        self,
        start: str,
        end: str,
        limit: Optional[int] = None,
        *,
        end_exclusive: bool | None = None,
    ) -> list[tuple[str, float, str]]:
        if end_exclusive is True:
            end_bound = str(end)[:10]
        else:
            try:
                end_bound = (
                    date.fromisoformat(str(end)[:10]) + timedelta(days=1)
                ).isoformat()
            except (TypeError, ValueError):
                end_bound = str(end)
        rows = self.category_spend_with_splits(
            start,
            end_bound,
            basis="Pagamento",
            end_exclusive=True,
        )
        if limit is not None:
            rows = rows[: max(0, int(limit))]
        colours = {
            str(row["name"]): safe_hex_colour(row["colour"])
            for row in self.q("SELECT name, colour FROM categories")
        }
        return [
            (
                r["category"] or "Sem categoria",
                float(r["total"] or 0),
                safe_hex_colour(colours.get(str(r["category"] or ""))),
            )
            for r in rows
        ]

    def dynamic_grouped(
        self,
        start: str,
        end: str,
        group_by: str = "Categoria",
        basis: str = "Pagamento",
        kind: str = "Despesas",
        *,
        end_exclusive: bool | None = None,
    ) -> list[dict[str, Any]]:
        if end_exclusive is True:
            end_bound = str(end)[:10]
        else:
            try:
                end_bound = (
                    date.fromisoformat(str(end)[:10]) + timedelta(days=1)
                ).isoformat()
            except (TypeError, ValueError):
                end_bound = str(end)

        if group_by == "Categoria" and kind == "Despesas":
            return [
                {
                    "label": str(row["category"] or "Não definido"),
                    "total": float(row["total"] or 0),
                    "movements": int(row["movements"] or 0),
                }
                for row in self.category_spend_with_splits(
                    start,
                    end_bound,
                    basis=basis,
                    end_exclusive=True,
                )
            ]

        date_expr = (
            "COALESCE(NULLIF(m.purchase_date,''), t.date)"
            if basis == "Compra"
            else "COALESCE(NULLIF(m.payment_date,''), t.date)"
        )
        field_map = {
            "Categoria": "t.category",
            "Subcategoria": "COALESCE(NULLIF(m.subcategory,''),'Sem subcategoria')",
            "Comerciante": "COALESCE(NULLIF(m.merchant,''),'Não definido')",
            "Forma de pagamento": "COALESCE(NULLIF(m.payment_method,''),'Não definido')",
            "Conta": "t.account",
            "Mês": f"substr({date_expr},1,7)",
            "Dia": date_expr,
            "Data": date_expr,
        }
        kind_condition = (
            f"t.amount<0 AND {_EXTERNAL_FLOW_SQL}"
            if kind == "Despesas"
            else f"t.amount>0 AND {_EXTERNAL_FLOW_SQL}"
            if kind == "Receitas"
            else _EXTERNAL_FLOW_SQL
        )
        value_expr = "SUM(ABS(t.amount))" if kind != "Saldo" else "SUM(t.amount)"
        field = field_map.get(group_by, "t.category")
        # TIME-02: when grouping by category, split rows must be counted
        # under their own category (COALESCE), never the parent's.
        join_splits = ""
        if field == "t.category":
            field = "COALESCE(s.category, t.category)"
            join_splits = (
                "LEFT JOIN transaction_splits s ON s.transaction_id=t.id"
            )
            value_expr = (
                "SUM(CASE WHEN s.id IS NOT NULL THEN ABS(s.amount) "
                "ELSE ABS(t.amount) END)"
                if kind != "Saldo"
                # TIME-02: split amounts are stored positive; mirror the PARENT
                # transaction sign so the "Saldo" view keeps expense outflows
                # negative and income inflows positive.  BUG-AUD-C129: the old
                # expression forced -s.amount unconditionally, subtracting
                # split income (e.g. a salary divided into Base/Subsídio) from
                # the balance instead of adding it.
                else "SUM(CASE WHEN s.id IS NOT NULL THEN "
                "(CASE WHEN t.amount < 0 THEN -ABS(s.amount) ELSE ABS(s.amount) END) "
                "ELSE t.amount END)"
            )
        # TIME-04: Temporal groupings (Mês, Dia, Data) must be ordered
        # chronologically (ASC), whereas nominal dimensions order by total DESC.
        order_clause = (
            "ORDER BY label ASC"
            if group_by in ("Mês", "Dia", "Data")
            else "ORDER BY total DESC"
        )
        rows = self.q(
            f"""
            SELECT {field} AS label, {value_expr} AS total, COUNT(DISTINCT t.id) AS movements
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            {join_splits}
            WHERE {date_expr}>=? AND {date_expr}<? AND {kind_condition}
              AND {_VALID_TRANSACTION_AMOUNT_SQL}
              AND {sql_iso_date(date_expr)}
            GROUP BY label
            {order_clause}
            """,
            (start, end_bound),
        )
        return [
            {
                "label": r["label"] or "Não definido",
                "total": float(r["total"] or 0),
                "movements": int(r["movements"] or 0),
            }
            for r in rows
        ]

    def report_months(self) -> list[str]:
        rows = self.q(
            f"SELECT DISTINCT substr({_FINANCIAL_DATE_SQL},1,7) AS ym "
            "FROM transactions t LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
            f"WHERE {_VALID_FINANCIAL_DATE_SQL} ORDER BY ym DESC"
        )
        values = [str(r["ym"] or "") for r in rows if r["ym"]]
        for ym in self.monthly_snapshot_months():
            if ym and ym not in values:
                values.append(ym)
        values = sorted(set(values), reverse=True)
        current = date.today().strftime("%Y-%m")
        if current not in values:
            values.insert(0, current)
        return values

    def monthly_snapshot_months(self) -> list[str]:
        rows = self.q(
            "SELECT DISTINCT month AS ym FROM monthly_snapshots ORDER BY ym DESC"
        )
        return [str(r["ym"] or "") for r in rows if r["ym"]]

    def monthly_snapshot_notes(self, month: str) -> str:
        rows = self.q(
            "SELECT notes FROM monthly_snapshots WHERE month=?", ((month or "")[:7],)
        )
        return str(rows[0]["notes"] or "") if rows else ""

    def _monthly_snapshot_values(
        self, month: str, start: str, end: str, notes: str, include_state: bool
    ) -> tuple[Any, ...]:
        income = self.scalar(
            f"SELECT SUM(t.amount) FROM transactions t "
            f"LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
            f"WHERE {_FINANCIAL_DATE_SQL}>=? "
            f"AND {_FINANCIAL_DATE_SQL}<=? "
            f"AND t.amount>0 AND {_VALID_TRANSACTION_AMOUNT_SQL} "
            f"AND {_VALID_FINANCIAL_DATE_SQL} "
            f"AND {_EXTERNAL_FLOW_SQL}",
            (start, end),
        )
        expenses = self.scalar(
            f"SELECT SUM(ABS(t.amount)) FROM transactions t "
            f"LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
            f"WHERE {_FINANCIAL_DATE_SQL}>=? "
            f"AND {_FINANCIAL_DATE_SQL}<=? "
            f"AND t.amount<0 AND {_VALID_TRANSACTION_AMOUNT_SQL} "
            f"AND {_VALID_FINANCIAL_DATE_SQL} "
            f"AND {_EXTERNAL_FLOW_SQL}",
            (start, end),
        )
        investment_state = self.investment_state_as_of(end)
        investments = float(investment_state.get("total") or 0)
        savings = income - expenses
        rate = savings / income * 100 if income else 0
        goals_json = "[]"
        holdings_json = "[]"
        state = self.financial_state_as_of(end)
        cash_balance = float(self.cash_balance(end))
        liabilities_total = sum(
            float(row.get("balance") or 0) for row in state.get("liabilities", [])
        )
        goal_assets = sum(
            max(0.0, float(row.get("saved_amount") or 0))
            for row in state.get("goals", [])
        )
        active_accounts = self.financial_accounts()
        active_account_ids = {
            str(row["stable_id"] or "").strip()
            for row in active_accounts
            if str(row["stable_id"] or "").strip()
        }
        active_account_names = {
            str(row["name"] or "Conta principal").strip().casefold()
            for row in active_accounts
        }
        state_holdings = list(state.get("holdings") or [])
        additive_investments = investments
        if state_holdings:
            additive_investments = sum(
                max(0.0, float(row.get("value") or 0))
                for row in state_holdings
                if not (
                    str(row.get("linked_account_id") or "").strip()
                    in active_account_ids
                    or str(row.get("linked_account") or "").strip().casefold()
                    in active_account_names
                )
            )
        net_worth = (
            cash_balance + additive_investments + goal_assets - liabilities_total
        )
        if include_state:
            goals_json = json.dumps(
                [dict(r) for r in state.get("goals", [])], ensure_ascii=False
            )
            holdings_json = json.dumps(
                [dict(r) for r in state.get("holdings", [])], ensure_ascii=False
            )
        return (
            (month or "")[:7],
            income,
            expenses,
            investments,
            savings,
            rate,
            cash_balance,
            net_worth,
            goals_json,
            holdings_json,
            notes.strip(),
        )

    def save_monthly_snapshot_notes(
        self, month: str, start: str, end: str, notes: str
    ) -> None:
        month = (month or date.today().strftime("%Y-%m"))[:7]
        existing = self.q("SELECT month FROM monthly_snapshots WHERE month=?", (month,))
        try:
            if existing:
                self.db.conn.execute(
                    "UPDATE monthly_snapshots SET notes=? WHERE month=?",
                    (notes.strip(), month),
                )
            else:
                self.db.conn.execute(
                    """
                    INSERT INTO monthly_snapshots(month,income,expenses,investments,savings,savings_rate,cash_balance,net_worth,goals_json,holdings_json,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    self._monthly_snapshot_values(
                        month, start, end, notes, include_state=False
                    ),
                )
            self.db.conn.commit()
        except (Exception):
            self.db.conn.rollback()
            raise

    def save_monthly_snapshot(
        self,
        month: str,
        start: str,
        end: str,
        notes: str,
        *,
        commit: bool = True,
    ) -> None:
        month = (month or date.today().strftime("%Y-%m"))[:7]
        connection = self.db.conn
        connection.execute("SAVEPOINT save_monthly_snapshot")
        try:
            connection.execute(
                """
                INSERT OR REPLACE INTO monthly_snapshots(month,income,expenses,investments,savings,savings_rate,cash_balance,net_worth,goals_json,holdings_json,notes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                self._monthly_snapshot_values(
                    month, start, end, notes, include_state=True
                ),
            )
            # The monthly snapshot must pin the daily state row to the month's
            # last day; using today() would stamp the state with the save date
            # and break every later ``financial_state_as_of`` lookup.
            self.record_financial_state_snapshot(
                snapshot_day=str(end or "")[:10] or None,
                commit=False,
            )
            connection.execute("RELEASE SAVEPOINT save_monthly_snapshot")
            if commit:
                connection.commit()
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT save_monthly_snapshot")
            connection.execute("RELEASE SAVEPOINT save_monthly_snapshot")
            raise

    @staticmethod
    def _decode_state_list(value: Any) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return (
            [dict(row) for row in parsed if isinstance(row, dict)]
            if isinstance(parsed, list)
            else []
        )

    def record_financial_state_snapshot(
        self,
        snapshot_day: str | None = None,
        *,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Persist the current non-transactional state for future historical views.

        History is immutable (PERS-05): calling this for a past day that
        already has a snapshot returns the stored snapshot unchanged instead
        of overwriting it with today's live state. A past day without a
        snapshot is backfilled from live state (its live values are the best
        reconstruction available). Today remains mutable so the daily
        snapshot can be refreshed as the user edits data.
        """
        day = (snapshot_day or date.today().isoformat())[:10]
        if day < date.today().isoformat():
            stored = self.q(
                """
                SELECT day, holdings_json, liabilities_json, subscriptions_json,
                       goals_json, cash_balance
                FROM financial_state_snapshots
                WHERE day=?
                """,
                (day,),
            )
            if stored:
                row = stored[0]
                return {
                    "day": str(row["day"] or "")[:10],
                    "holdings": self._decode_state_list(row["holdings_json"]),
                    "liabilities": self._decode_state_list(row["liabilities_json"]),
                    "subscriptions": self._decode_state_list(
                        row["subscriptions_json"]
                    ),
                    "goals": self._decode_state_list(row["goals_json"]),
                    "cash_balance": float(row["cash_balance"] or 0),
                }
        holdings = [dict(row) for row in self.holdings()]
        liabilities = [dict(row) for row in self.liabilities()]
        subscriptions = [dict(row) for row in self.subscriptions()]
        goals = [dict(row) for row in self.goals()]
        cash_balance = float(self.cash_balance(day))
        self.db.conn.execute(
            """
            INSERT INTO financial_state_snapshots(
                day, holdings_json, liabilities_json, subscriptions_json,
                goals_json, cash_balance
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(day) DO UPDATE SET
                holdings_json=excluded.holdings_json,
                liabilities_json=excluded.liabilities_json,
                subscriptions_json=excluded.subscriptions_json,
                goals_json=excluded.goals_json,
                cash_balance=excluded.cash_balance,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                day,
                json.dumps(holdings, ensure_ascii=False),
                json.dumps(liabilities, ensure_ascii=False),
                json.dumps(subscriptions, ensure_ascii=False),
                json.dumps(goals, ensure_ascii=False),
                cash_balance,
            ),
        )
        if commit:
            self.db.conn.commit()
        return {
            "day": day,
            "holdings": holdings,
            "liabilities": liabilities,
            "subscriptions": subscriptions,
            "goals": goals,
            "cash_balance": cash_balance,
        }

    def financial_state_as_of(self, end: str) -> dict[str, Any]:
        end_day = str(end or date.today().isoformat())[:10]
        if end_day >= date.today().isoformat():
            return {
                "as_of": date.today().isoformat(),
                "holdings": [dict(row) for row in self.holdings()],
                "liabilities": [dict(row) for row in self.liabilities()],
                "subscriptions": [dict(row) for row in self.subscriptions()],
                "goals": [dict(row) for row in self.goals()],
                "cash_balance": float(self.cash_balance(date.today().isoformat())),
                "source": "current",
                "confidence": 100.0,
                "liabilities_available": True,
                "subscriptions_available": True,
                "goals_available": True,
            }
        rows = self.q(
            """
            SELECT day, holdings_json, liabilities_json, subscriptions_json,
                   goals_json, cash_balance
            FROM financial_state_snapshots
            WHERE day<=?
            ORDER BY day DESC
            LIMIT 1
            """,
            (end_day,),
        )
        if rows:
            row = rows[0]
            return {
                "as_of": str(row["day"] or "")[:10],
                "holdings": self._decode_state_list(row["holdings_json"]),
                "liabilities": self._decode_state_list(row["liabilities_json"]),
                "subscriptions": self._decode_state_list(row["subscriptions_json"]),
                "goals": self._decode_state_list(row["goals_json"]),
                "cash_balance": float(row["cash_balance"] or 0),
                "source": "daily_snapshot",
                "confidence": 100.0,
                "liabilities_available": True,
                "subscriptions_available": True,
                "goals_available": row["goals_json"] is not None,
            }

        legacy = self.q(
            """
            SELECT month, holdings_json, goals_json
            FROM monthly_snapshots
            WHERE month<=? AND holdings_json IS NOT NULL
              AND holdings_json NOT IN ('', '[]')
            ORDER BY month DESC
            LIMIT 1
            """,
            (end_day[:7],),
        )
        if legacy:
            row = legacy[0]
            return {
                "as_of": f"{str(row['month'])[:7]}-01",
                "holdings": self._decode_state_list(row["holdings_json"]),
                "liabilities": [],
                "subscriptions": [],
                "goals": self._decode_state_list(row["goals_json"]),
                "cash_balance": 0.0,
                "source": "legacy_monthly_snapshot",
                "confidence": 55.0,
                "liabilities_available": False,
                "subscriptions_available": False,
                "goals_available": True,
            }
        return {
            "as_of": "",
            "holdings": [],
            "liabilities": [],
            "subscriptions": [],
            "goals": [],
            "cash_balance": 0.0,
            "source": "unavailable",
            "confidence": 35.0,
            "liabilities_available": False,
            "subscriptions_available": False,
            "goals_available": False,
        }

    def investment_state_as_of(self, end: str) -> dict[str, Any]:
        end_day = str(end or date.today().isoformat())[:10]
        state = self.financial_state_as_of(end_day)
        holdings = list(state.get("holdings") or [])
        def _rate(item: Any) -> float:
            if not isinstance(item, dict):
                return 1.0
            try:
                r = float(item.get("exchange_rate") or 1.0)
                return r if r > 0 else 1.0
            except (TypeError, ValueError):
                return 1.0

        if end_day >= date.today().isoformat():
            invested = sum(float(row.get("cost_basis") or 0) * _rate(row) for row in holdings)
            total = sum(float(row.get("value") or 0) * _rate(row) for row in holdings)
            source = "current"
            as_of = date.today().isoformat()
            available = True
        else:
            point = self._investment_point_as_of(end_day)
            if point is not None:
                invested = float(point.get("invested") or 0)
                total = float(point.get("total") or 0)
                source = "investment_snapshot"
                as_of = str(point.get("date") or "")[:10]
                available = True
            elif holdings:
                invested = sum(float(row.get("cost_basis") or 0) * _rate(row) for row in holdings)
                total = sum(float(row.get("value") or 0) * _rate(row) for row in holdings)
                source = str(state.get("source") or "snapshot")
                as_of = str(state.get("as_of") or "")
                available = True
            else:
                invested = 0.0
                total = 0.0
                source = "unavailable"
                as_of = ""
                available = False
        return {
            "available": available,
            "as_of": as_of,
            "source": source,
            "invested": invested,
            "total": total,
            "gain": total - invested,
            "return_pct": (total - invested) / invested * 100 if invested else 0.0,
            "holdings": holdings,
            "holdings_available": bool(holdings),
            "confidence": float(state.get("confidence") or 0),
        }

    def _investment_point_as_of(self, end_day: str) -> dict[str, Any] | None:
        """Find one point, keeping daily tie precedence and legacy fallback.

        A LIMIT 1 on monthly JSON would discard an older valid point when the
        latest payload cannot be decoded. Stream descending candidates until
        a valid monthly point is found or the daily point is already newer.
        """
        daily = self.db.conn.execute(
            "SELECT day,invested,total FROM investment_snapshots "
            "WHERE day<=? ORDER BY day DESC LIMIT 1", (end_day,),
        ).fetchone()
        point = (
            {"date": str(daily["day"])[:10], "invested": float(daily["invested"] or 0), "total": float(daily["total"] or 0)}
            if daily is not None else None
        )
        cursor = self.db.conn.execute(
            "SELECT month,holdings_json FROM monthly_snapshots "
            "WHERE month<=? AND holdings_json IS NOT NULL "
            "AND holdings_json NOT IN ('', '[]') ORDER BY month DESC",
            (end_day[:7],),
        )
        def _rate(item: Any) -> float:
            if not isinstance(item, dict):
                return 1.0
            try:
                rate = float(item.get("exchange_rate") or 1.0)
                return rate if rate > 0 else 1.0
            except (TypeError, ValueError):
                return 1.0

        try:
            for row in cursor:
                day = f"{str(row['month'])[:7]}-01"
                if day > end_day:
                    continue
                if point is not None and day <= str(point["date"]):
                    break
                try:
                    holdings = json.loads(str(row["holdings_json"] or "[]"))
                    if not isinstance(holdings, list) or not holdings:
                        continue
                    # Keep Python's summation semantics identical to the
                    # evolution series, including high-precision unit prices.
                    invested = sum(float(item.get("cost_basis") or 0) * _rate(item) for item in holdings)
                    total = sum(float(item.get("value") or 0) * _rate(item) for item in holdings)
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                point = {"date": day, "invested": invested, "total": total}
                break
        finally:
            cursor.close()
        return point

    def record_investment_snapshot(
        self,
        snapshot_day: str | None = None,
        *,
        commit: bool = True,
    ) -> dict[str, float | str | bool]:
        """Store one real portfolio point per day for the evolution chart."""
        day = (snapshot_day or date.today().isoformat())[:10]
        cols = {str(r["name"]) for r in self.db.query("PRAGMA table_info(holdings)")}
        rate_calc = " * CASE WHEN exchange_rate > 0 THEN exchange_rate ELSE 1.0 END" if "exchange_rate" in cols else ""
        invested = float(
            self.scalar(
                f"SELECT COALESCE(SUM(cost_basis{rate_calc}),0) FROM holdings WHERE "
                + sql_finite_number("cost_basis", non_negative=True)
            )
            or 0
        )
        total = float(
            self.scalar(
                f"SELECT COALESCE(SUM(value{rate_calc}),0) FROM holdings WHERE "
                + sql_finite_number("value", non_negative=True)
            )
            or 0
        )
        cursor = self.db.conn.execute(
            """
            INSERT INTO investment_snapshots(day, invested, total)
            VALUES(?,?,?)
            ON CONFLICT(day) DO UPDATE SET
                invested=excluded.invested,
                total=excluded.total,
                updated_at=CURRENT_TIMESTAMP
            WHERE ABS(investment_snapshots.invested-excluded.invested)>=0.005
               OR ABS(investment_snapshots.total-excluded.total)>=0.005
            """,
            (day, invested, total),
        )
        if commit:
            self.db.conn.commit()
        return {
            "date": day,
            "invested": invested,
            "total": total,
            "changed": cursor.rowcount > 0,
        }

    def investment_evolution(self) -> list[dict[str, float | str]]:
        """Return genuine historical cost and market-value points.

        Monthly snapshots are reused only when their holdings JSON contains both
        values. Daily investment snapshots override a monthly point on the same day.
        """
        points: dict[str, dict[str, float | str]] = {}
        monthly_rows = self.q(
            """
            SELECT month, holdings_json
            FROM monthly_snapshots
            WHERE holdings_json IS NOT NULL AND holdings_json NOT IN ('', '[]')
            ORDER BY month
            """
        )
        def _rate(item: Any) -> float:
            if not isinstance(item, dict):
                return 1.0
            try:
                r = float(item.get("exchange_rate") or 1.0)
                return r if r > 0 else 1.0
            except (TypeError, ValueError):
                return 1.0

        for row in monthly_rows:
            try:
                holdings = json.loads(str(row["holdings_json"] or "[]"))
                if not isinstance(holdings, list) or not holdings:
                    continue
                invested = sum(float(item.get("cost_basis") or 0) * _rate(item) for item in holdings)
                total = sum(float(item.get("value") or 0) * _rate(item) for item in holdings)
                day = f"{str(row['month'])[:7]}-01"
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            points[day] = {"date": day, "invested": invested, "total": total}

        for row in self.q(
            "SELECT day, invested, total FROM investment_snapshots ORDER BY day"
        ):
            day = str(row["day"] or "")[:10]
            if day:
                points[day] = {
                    "date": day,
                    "invested": float(row["invested"] or 0),
                    "total": float(row["total"] or 0),
                }

        result = [points[key] for key in sorted(points)]
        for point in result:
            invested = float(point["invested"] or 0)
            total = float(point["total"] or 0)
            point["gain"] = total - invested
            point["return_pct"] = (
                (total - invested) / invested * 100 if invested else 0.0
            )
        return result

    def ensure_analytics_tables(self) -> None:
        """Extra fields used by the FinanceFlow/Excel inspired analytics layer."""
        self.db.ensure_analytics_tables()

    def chart_movement_details(
        self,
        start: str,
        end: str,
        *,
        category: str = "",
        kind: str = "",
        search_text: str = "",
        subcategory: str = "",
        account: str = "",
        payment_method: str = "",
        basis: str = "Pagamento",
        month_number: int | None = None,
        weekday: int | None = None,
        limit: int = 12,
    ) -> list[Any]:
        """Return the movements that explain a chart point or category.

        Expense splits are expanded into their effective categories so the
        detail always reconciles with the category charts.
        """

        try:
            end_exclusive = (
                date.fromisoformat(str(end or "")[:10]) + timedelta(days=1)
            ).isoformat()
        except (TypeError, ValueError):
            end_exclusive = str(end or "")
        conditions: list[str] = []
        params: list[Any] = [str(start or ""), end_exclusive]
        selected_category = str(category or "").strip()
        if selected_category:
            category_id, category_name = self.category_identity(
                selected_category,
                create=False,
            )
            if category_id:
                conditions.append("effective_category_id=?")
                params.append(category_id)
            else:
                conditions.append("effective_category=?")
                params.append(category_name)
        selected_kind = str(kind or "").strip().casefold()
        if selected_kind in {"expense", "despesas", "despesa"}:
            conditions.append("amount<0")
        elif selected_kind in {"income", "receitas", "receita"}:
            conditions.append("amount>0")
        selected_search = str(search_text or "").strip()
        if selected_search:
            conditions.append(
                "(description LIKE ? ESCAPE '/' COLLATE NOCASE OR merchant LIKE ? ESCAPE '/' COLLATE NOCASE)"
            )
            escaped_search = (
                selected_search.replace("/", "//")
                .replace("%", "/%")
                .replace("_", "/_")
            )
            search_pattern = f"%{escaped_search}%"
            params.extend((search_pattern, search_pattern))
        selected_subcategory = str(subcategory or "").strip()
        if selected_subcategory:
            conditions.append("subcategory=?")
            params.append(selected_subcategory)
        selected_account = str(account or "").strip()
        if selected_account:
            conditions.append("account=?")
            params.append(selected_account)
        selected_payment_method = str(payment_method or "").strip()
        if selected_payment_method:
            conditions.append("payment_method=?")
            params.append(selected_payment_method)
        if month_number is not None:
            selected_month = max(1, min(12, int(month_number)))
            conditions.append("CAST(strftime('%m', financial_date) AS INTEGER)=?")
            params.append(selected_month)
        if weekday is not None:
            selected_weekday = max(0, min(6, int(weekday)))
            conditions.append("CAST(strftime('%w', financial_date) AS INTEGER)=?")
            params.append((selected_weekday + 1) % 7)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, min(50, int(limit or 12))))
        date_expression = (
            "COALESCE(NULLIF(m.purchase_date,''),t.date)"
            if str(basis or "").strip() == "Compra"
            else "COALESCE(NULLIF(m.payment_date,''),t.date)"
        )
        return self.q(
            f"""
            WITH base AS (
                SELECT t.id,
                       {date_expression} AS financial_date,
                       t.description,
                       t.category,
                       t.category_id,
                       COALESCE(NULLIF(m.subcategory,''),'Sem subcategoria') AS subcategory,
                       t.account,
                       t.amount,
                       COALESCE(NULLIF(m.merchant,''),'') AS merchant,
                       COALESCE(NULLIF(m.payment_method,''),'Não definido') AS payment_method,
                       CASE WHEN EXISTS (
                           SELECT 1
                           FROM transaction_splits present
                           WHERE present.transaction_id=t.id
                       ) THEN 1 ELSE 0 END AS has_split
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE {date_expression}>=?
                  AND {date_expression}<?
                  AND {_EXTERNAL_FLOW_SQL}
            ),
            expanded AS (
                SELECT base.id,
                       base.financial_date,
                       base.description,
                       split.category AS effective_category,
                       split.category_id AS effective_category_id,
                       base.subcategory,
                       base.account,
                       (CASE WHEN base.amount < 0 THEN -ABS(split.amount) ELSE ABS(split.amount) END) AS amount,
                       base.merchant,
                       base.payment_method,
                       1 AS split_contribution
                FROM base
                JOIN transaction_splits split ON split.transaction_id=base.id
                UNION ALL
                SELECT base.id,
                       base.financial_date,
                       base.description,
                       base.category AS effective_category,
                       base.category_id AS effective_category_id,
                       base.subcategory,
                       base.account,
                       base.amount,
                       base.merchant,
                       base.payment_method,
                       0 AS split_contribution
                FROM base
                WHERE base.has_split=0
            )
            SELECT *
            FROM expanded
            {where}
            ORDER BY ABS(amount) DESC, financial_date DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        )

    def category_spend_with_splits(
        self,
        start: str,
        end: str,
        basis: str = "Pagamento",
        *,
        end_exclusive: bool | None = None,
    ) -> list[Any]:
        date_expr = (
            "COALESCE(NULLIF(m.purchase_date,''),t.date)"
            if basis == "Compra"
            else "COALESCE(NULLIF(m.payment_date,''),t.date)"
        )
        # C7: One repository contract: [start, end_exclusive).
        # When end_exclusive is True, callers pass an already exclusive boundary
        # (e.g. through_date + 1 day or month start of next month) and no date
        # promotion is performed, preventing penultimate month-end bugs (e.g. Aug 30).
        # When end_exclusive is False, caller passed an inclusive date, so promote by 1 day.
        # When end_exclusive is None (legacy unannotated caller compatibility),
        # only promote if end was the month's last day (TIME-01 behavior).
        if end_exclusive is True:
            pass
        elif end_exclusive is False:
            try:
                end = (date.fromisoformat(str(end)[:10]) + timedelta(days=1)).isoformat()
            except (TypeError, ValueError):
                pass
        else:
            try:
                end_day = date.fromisoformat(str(end)[:10])
                next_day = end_day + timedelta(days=1)
                if next_day.day == 1:  # end_day was the month's last day
                    end = next_day.isoformat()
            except (TypeError, ValueError):
                pass
        return self.q(
            f"""
            WITH base AS (
                SELECT t.id, t.category, ABS(t.amount) AS amount,
                       CASE WHEN EXISTS (SELECT 1 FROM transaction_splits s WHERE s.transaction_id=t.id) THEN 1 ELSE 0 END AS has_split
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE {date_expr} >= ?
                  AND {date_expr} < ?  -- TIME-01: end is EXCLUSIVE (start, end+1d); inclusive callers converted at the repo boundary
                  AND t.amount < 0 AND {_EXTERNAL_FLOW_SQL}
                  AND {_VALID_TRANSACTION_AMOUNT_SQL}
                  AND {sql_iso_date(date_expr)}
            ), split_rows AS (
                SELECT s.category, SUM(ABS(s.amount)) AS total,
                       COUNT(DISTINCT s.transaction_id) AS movements
                FROM transaction_splits s
                JOIN transactions t ON t.id=s.transaction_id
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE {date_expr} >= ?
                  AND {date_expr} < ?  -- TIME-01: same exclusive-end contract
                  AND t.amount < 0 AND {_EXTERNAL_FLOW_SQL}
                  AND {_VALID_TRANSACTION_AMOUNT_SQL}
                  AND {sql_iso_date(date_expr)}
                  AND {sql_finite_number("s.amount", non_negative=True)}
                GROUP BY s.category
            ), unsplit_rows AS (
                SELECT category, SUM(amount) AS total, COUNT(*) AS movements
                FROM base
                WHERE has_split = 0
                GROUP BY category
            )
            SELECT category, SUM(total) AS total, SUM(movements) AS movements
            FROM (
                SELECT * FROM split_rows
                UNION ALL
                SELECT * FROM unsplit_rows
            )
            GROUP BY category
            ORDER BY total DESC
            """,
            (start, end, start, end),
        )
