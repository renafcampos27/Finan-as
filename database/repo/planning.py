#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Iterator, Optional


from database.validation import sql_finite_number
from utils.helpers import (
    require_eur_cent_precision,
    require_iso_date,
    require_nonzero_eur,
    row_get,
    safe_float,
)


class PlanningRepoMixin:
    def _capture_planning_state(
        self,
        include_investments: bool = False,
        *,
        commit: bool = True,
    ) -> None:
        """Keep a daily state trail after non-transactional financial edits."""
        if include_investments:
            self.record_investment_snapshot(commit=False)
        self.record_financial_state_snapshot(commit=False)
        if commit:
            self.db.conn.commit()

    @contextmanager
    def _planning_write(
        self,
        savepoint: str,
        *,
        include_investments: bool = False,
        capture_state: bool = True,
    ) -> Iterator[Any]:
        """Commit a planning edit and its historical state as one unit."""

        if not savepoint.replace("_", "").isalnum():
            raise ValueError("Invalid planning savepoint name")
        connection = self.db.conn
        owns_tx = not connection.in_transaction
        connection.execute(f"SAVEPOINT {savepoint}")

        try:
            yield connection
            if capture_state:
                self._capture_planning_state(
                    include_investments=include_investments,
                    commit=False,
                )
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            if owns_tx:
                connection.rollback()
            raise
        if owns_tx:
            try:
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def holdings(self) -> list[Any]:
        return self.q("SELECT * FROM holdings ORDER BY asset_class, name")

    def record_investment_disposal(
        self,
        holding_id: int,
        *,
        sale_date: str,
        sale_value_eur: float,
        sale_fees_eur: float,
        account: str,
        create_transaction: bool = True,
        archive_holding: bool = True,
        acquisition_exchange_rate: float | None = None,
        journal: Any = None,
    ) -> dict[str, Any]:
        """Atomically retain a full-position sale before removing its source.

        Existing settings are included in every database backup. Versioned,
        immutable event keys avoid a schema migration and do not merge lots.
        """
        import json

        from services.tax_reporting import (
            DISPOSAL_PREFIX, build_realized_report, create_disposal_record, load_disposal_records,
        )

        if self.db.conn.in_transaction:
            raise RuntimeError("Conclui a operação pendente antes de fechar a posição.")
        if not archive_holding:
            raise ValueError(
                "Uma venda integral tem de arquivar a posição ativa. "
                "Os dados de aquisição ficam preservados no registo da venda e no histórico de alterações."
            )
        sale_value_eur = require_eur_cent_precision(sale_value_eur, field_name="valor da venda")
        sale_fees_eur = require_eur_cent_precision(sale_fees_eur, field_name="encargos da venda")
        with self._planning_write("investment_disposal", include_investments=True) as connection:
            holding = self.holding_by_id(holding_id)
            if holding is None:
                raise ValueError("O investimento selecionado já não existe.")
            for existing in load_disposal_records(self):
                source = existing.get("holding", {})
                if not source:
                    raise ValueError("Existe um registo de venda danificado; confirma a recuperação antes de continuar.")
                if source.get("stable_id") and source.get("stable_id") == holding["stable_id"]:
                    raise ValueError("Esta posição já tem uma venda integral registada.")
            record = create_disposal_record(
                holding, sale_date=sale_date, sale_value_eur=sale_value_eur,
                sale_fees_eur=sale_fees_eur,
                acquisition_exchange_rate=acquisition_exchange_rate,
            )
            key = DISPOSAL_PREFIX + record["id"]
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
            connection.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, encoded))
            received = require_eur_cent_precision(
                record["sale_value_eur"] - record["sale_fees_eur"],
                field_name="receita líquida",
            )
            if create_transaction and received < 0:
                raise ValueError("Os encargos excedem a receita da venda.")
            transaction_id = None
            if create_transaction and received:
                canonical = build_realized_report([record], int(sale_date[:4]))["rows"][0]
                gain = canonical["net_gain_eur"]
                gain_text = f"{gain:+.2f} €" if gain is not None else "Indisponível (custo ou câmbio desconhecido)"
                transaction_id = self.add_transaction(
                    sale_date, f"Alienação de investimento: {holding['name']}",
                    received, "Investimentos", account,
                    notes=(
                        f"Venda integral registada: {record['id']}. "
                        f"Valor bruto: {record['sale_value_eur']:.2f} €. "
                        f"Encargos de venda: {record['sale_fees_eur']:.2f} €. "
                        f"Mais/Menos-valia líquida: {gain_text}."
                    ), commit=False,
                )
            connection.execute("DELETE FROM holdings WHERE id=?", (int(holding_id),))
            after = None
            if journal is not None:
                journal.record_investment_disposal_undo(
                    dict(holding), after, key, encoded, transaction_id,
                )
        return record

    def financial_accounts(self) -> list[Any]:
        return self.q(
            "SELECT * FROM financial_accounts WHERE is_active=1 "
            "ORDER BY name COLLATE NOCASE"
        )

    def ensure_financial_account(
        self,
        name: str,
        *,
        commit: bool = True,
    ) -> None:
        self.canonical_account_name(name)
        if commit:
            self.db.conn.commit()

    def set_financial_account_reference(
        self,
        name: str,
        reference_balance: float,
        reference_date: str,
        *,
        balance_known: bool,
    ) -> None:
        account = self.canonical_account_name(name)
        day = (
            require_iso_date(reference_date, field_name="data do saldo")
            if balance_known
            else ""
        )
        confirmed_balance = require_eur_cent_precision(
            reference_balance,
            field_name="saldo de referência",
        )
        with self._planning_write("set_account_reference") as connection:
            connection.execute(
                """
                UPDATE financial_accounts
                SET reference_balance=?,reference_date=?,balance_known=?,
                    balance_source=?,updated_at=CURRENT_TIMESTAMP
                WHERE name=?
                """,
                (
                    confirmed_balance,
                    day,
                    1 if balance_known else 0,
                    "manual" if balance_known else "",
                    account,
                ),
            )

    def holding_by_id(self, holding_id: int) -> Optional[Any]:
        rows = self.q("SELECT * FROM holdings WHERE id=?", (int(holding_id),))
        return rows[0] if rows else None

    def add_holding(
        self,
        name: str,
        asset_class: str,
        account: str,
        value: float,
        cost_basis: float,
        notes: str = "",
        fees: float = 0.0,
        cost_basis_known: Optional[bool] = None,
        linked_account: str = "",
        currency: str = "EUR",
        exchange_rate: float = 1.0,
        acquisition_date: str = "",
        jurisdiction: str = "PT",
    ) -> int:
        basis_known = (
            bool(cost_basis_known)
            if cost_basis_known is not None
            else float(cost_basis or 0) > 0
        )
        if basis_known and float(cost_basis or 0) <= 0:
            raise ValueError(
                "O capital investido conhecido tem de ser superior a zero."
            )
        curr = str(currency or "EUR").strip().upper() or "EUR"
        rate = float(exchange_rate or 1.0)
        if rate <= 0:
            rate = 1.0
        acq_date = str(acquisition_date or "").strip()[:10]
        if acq_date:
            try:
                date.fromisoformat(acq_date)
            except ValueError:
                acq_date = ""
        juris = str(jurisdiction or "PT").strip().upper()[:2] or "PT"
        stable_id = self._new_stable_id("ast")
        linked_account_id = ""
        linked_account_name = ""
        if str(linked_account or "").strip():
            linked_account_id, linked_account_name = self.account_identity(
                linked_account
            )
        with self._planning_write(
            "add_holding", include_investments=True
        ) as connection:
            cur = connection.execute(
                """
                INSERT INTO holdings(
                    name, asset_class, account, value, cost_basis,
                    cost_basis_known, fees, linked_account, linked_account_id,
                    stable_id, notes, currency, exchange_rate,
                    acquisition_date, jurisdiction
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    name.strip(),
                    asset_class.strip(),
                    account.strip() or "Manual",
                    float(value or 0),
                    float(cost_basis or 0),
                    1 if basis_known else 0,
                    max(0.0, float(fees or 0)),
                    linked_account_name,
                    linked_account_id,
                    stable_id,
                    notes.strip(),
                    curr,
                    rate,
                    acq_date,
                    juris,
                ),
            )
        return int(cur.lastrowid)

    def update_holding(
        self,
        holding_id: int,
        name: str,
        asset_class: str,
        account: str,
        value: float,
        cost_basis: float,
        notes: str = "",
        fees: float = 0.0,
        cost_basis_known: Optional[bool] = None,
        linked_account: str = "",
        currency: str = "EUR",
        exchange_rate: float = 1.0,
        acquisition_date: str = "",
        jurisdiction: str = "PT",
    ) -> None:
        basis_known = (
            bool(cost_basis_known)
            if cost_basis_known is not None
            else float(cost_basis or 0) > 0
        )
        if basis_known and float(cost_basis or 0) <= 0:
            raise ValueError(
                "O capital investido conhecido tem de ser superior a zero."
            )
        curr = str(currency or "EUR").strip().upper() or "EUR"
        rate = float(exchange_rate or 1.0)
        if rate <= 0:
            rate = 1.0
        acq_date = str(acquisition_date or "").strip()[:10]
        if acq_date:
            try:
                date.fromisoformat(acq_date)
            except ValueError:
                acq_date = ""
        juris = str(jurisdiction or "PT").strip().upper()[:2] or "PT"
        linked_account_id = ""
        linked_account_name = ""
        if str(linked_account or "").strip():
            linked_account_id, linked_account_name = self.account_identity(
                linked_account
            )
        with self._planning_write(
            "update_holding", include_investments=True
        ) as connection:
            cur = connection.execute(
                """
                UPDATE holdings
                SET name=?, asset_class=?, account=?, value=?, cost_basis=?,
                    cost_basis_known=?, fees=?, linked_account=?,
                    linked_account_id=?, notes=?, currency=?, exchange_rate=?,
                    acquisition_date=?, jurisdiction=?
                WHERE id=?
                """,
                (
                    name.strip(),
                    asset_class.strip(),
                    account.strip() or "Manual",
                    float(value or 0),
                    float(cost_basis or 0),
                    1 if basis_known else 0,
                    max(0.0, float(fees or 0)),
                    linked_account_name,
                    linked_account_id,
                    notes.strip(),
                    curr,
                    rate,
                    acq_date,
                    juris,
                    int(holding_id),
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Holding not found: {holding_id}")

    def delete_holding(self, holding_id: int) -> None:
        with self._planning_write(
            "delete_holding", include_investments=True
        ) as connection:
            connection.execute("DELETE FROM holdings WHERE id=?", (int(holding_id),))

    def liabilities(self) -> list[Any]:
        return self.q(
            "SELECT * FROM liabilities ORDER BY balance DESC, liability_type, name"
        )

    def liability_by_id(self, liability_id: int) -> Optional[Any]:
        rows = self.q("SELECT * FROM liabilities WHERE id=?", (int(liability_id),))
        return rows[0] if rows else None

    def add_liability(
        self,
        name: str,
        liability_type: str,
        account: str,
        balance: float,
        interest_rate: float,
        monthly_payment: float,
        notes: str = "",
    ) -> int:
        account_id = ""
        account_name = account.strip()
        if account_name:
            account_id, account_name = self.account_identity(account_name)
        with self._planning_write("add_liability") as connection:
            cur = connection.execute(
                """
                INSERT INTO liabilities(
                    name, liability_type, account, account_id, balance, interest_rate,
                    monthly_payment, notes
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    name.strip(),
                    liability_type.strip() or "Outro",
                    account_name,
                    account_id,
                    max(0.0, float(balance or 0)),
                    max(0.0, float(interest_rate or 0)),
                    max(0.0, float(monthly_payment or 0)),
                    notes.strip(),
                ),
            )
        return int(cur.lastrowid)

    def update_liability(
        self,
        liability_id: int,
        name: str,
        liability_type: str,
        account: str,
        balance: float,
        interest_rate: float,
        monthly_payment: float,
        notes: str = "",
    ) -> None:
        account_id = ""
        account_name = account.strip()
        if account_name:
            account_id, account_name = self.account_identity(account_name)
        with self._planning_write("update_liability") as connection:
            cur = connection.execute(
                """
                UPDATE liabilities
                SET name=?, liability_type=?, account=?, account_id=?, balance=?,
                    interest_rate=?, monthly_payment=?, notes=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    name.strip(),
                    liability_type.strip() or "Outro",
                    account_name,
                    account_id,
                    max(0.0, float(balance or 0)),
                    max(0.0, float(interest_rate or 0)),
                    max(0.0, float(monthly_payment or 0)),
                    notes.strip(),
                    int(liability_id),
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Liability not found: {liability_id}")

    def delete_liability(self, liability_id: int) -> None:
        with self._planning_write("delete_liability") as connection:
            connection.execute(
                "DELETE FROM liabilities WHERE id=?", (int(liability_id),)
            )

    def subscriptions(self, active_only: bool = False) -> list[Any]:
        if active_only:
            return self.q(
                "SELECT * FROM subscriptions WHERE active=1 ORDER BY renewal_day, name"
            )
        return self.q(
            "SELECT * FROM subscriptions ORDER BY active DESC, renewal_day, name"
        )

    def subscription_by_id(self, subscription_id: int) -> Optional[Any]:
        rows = self.q("SELECT * FROM subscriptions WHERE id=?", (int(subscription_id),))
        return rows[0] if rows else None

    def active_subscription_upcoming(self, limit: int = 8) -> list[Any]:
        from services.payments import PaymentsService

        service = PaymentsService(self)
        rows = []
        for source in self.subscriptions(active_only=True):
            row = dict(source)
            row["day"] = int(row_get(source, "renewal_day", 1) or 1)
            row["next_date"] = service.next_subscription_due_date(source).isoformat()
            rows.append(row)
        rows.sort(key=lambda row: (row["next_date"], str(row.get("name") or "")))
        return rows[: max(0, int(limit))]

    def add_subscription(
        self,
        name: str,
        amount: float,
        category: str,
        renewal_day: int,
        period: str,
        notes: str = "",
        active: int = 1,
        last_used_date: str = "",
        next_renewal_date: str = "",
        aliases: str = "",
        account: str = "Conta principal",
        payment_method: str = "",
    ) -> int:
        amount = abs(require_nonzero_eur(amount, field_name="valor da subscrição"))
        category_id, category = self.category_identity(
            category.strip() or "Outros",
            create=True,
            kind="expense",
        )
        account_id, account = self.account_identity(account)
        with self._planning_write("add_subscription") as connection:
            cur = connection.execute(
                "INSERT INTO subscriptions("
                "name,amount,category,renewal_day,period,notes,active,"
                "last_used_date,next_renewal_date,aliases,account,payment_method,"
                "category_id,account_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    name.strip(),
                    amount,
                    category,
                    int(renewal_day or 1),
                    period.strip() or "Mensal",
                    notes.strip(),
                    1 if int(active or 0) else 0,
                    (last_used_date or "")[:10],
                    (next_renewal_date or "")[:10],
                    aliases.strip(),
                    account,
                    payment_method.strip(),
                    category_id,
                    account_id,
                ),
            )
        return int(cur.lastrowid)

    def update_subscription(
        self,
        subscription_id: int,
        name: str,
        amount: float,
        category: str,
        renewal_day: int,
        period: str,
        notes: str = "",
        active: int = 1,
        last_used_date: Optional[str] = None,
        next_renewal_date: Optional[str] = None,
        aliases: Optional[str] = None,
        account: Optional[str] = None,
        payment_method: Optional[str] = None,
    ) -> None:
        existing = self.subscription_by_id(subscription_id)
        if last_used_date is None:
            last_used_date = (
                str(row_get(existing, "last_used_date", "")) if existing else ""
            )
        if next_renewal_date is None:
            next_renewal_date = (
                str(row_get(existing, "next_renewal_date", "")) if existing else ""
            )
        if aliases is None:
            aliases = str(row_get(existing, "aliases", "")) if existing else ""
        if account is None:
            account = (
                str(row_get(existing, "account", "Conta principal"))
                if existing
                else "Conta principal"
            )
        if payment_method is None:
            payment_method = (
                str(row_get(existing, "payment_method", "")) if existing else ""
            )
        amount = abs(require_nonzero_eur(amount, field_name="valor da subscrição"))
        category_id, category = self.category_identity(
            category.strip() or "Outros",
            create=True,
            kind="expense",
        )
        account_id, account = self.account_identity(account)
        with self._planning_write("update_subscription") as connection:
            cur = connection.execute(
                """
                UPDATE subscriptions
                SET name=?, amount=?, category=?, renewal_day=?, period=?, notes=?,
                    active=?, last_used_date=?, next_renewal_date=?, aliases=?,
                    account=?, payment_method=?,category_id=?,account_id=?
                WHERE id=?
                """,
                (
                    name.strip(),
                    amount,
                    category,
                    int(renewal_day or 1),
                    period.strip() or "Mensal",
                    notes.strip(),
                    1 if int(active or 0) else 0,
                    (last_used_date or "")[:10],
                    (next_renewal_date or "")[:10],
                    aliases.strip(),
                    account,
                    payment_method.strip(),
                    category_id,
                    account_id,
                    int(subscription_id),
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Subscription not found: {subscription_id}")

    def delete_subscription(self, subscription_id: int) -> None:
        with self._planning_write("delete_subscription") as connection:
            connection.execute(
                "DELETE FROM subscriptions WHERE id=?", (int(subscription_id),)
            )

    def set_subscription_active(self, subscription_id: int, active: int) -> None:
        with self._planning_write("set_subscription_active") as connection:
            cur = connection.execute(
                "UPDATE subscriptions SET active=? WHERE id=?",
                (1 if int(active or 0) else 0, int(subscription_id)),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Subscription not found: {subscription_id}")

    def toggle_subscription_active(self, subscription_id: int) -> int:
        row = self.subscription_by_id(subscription_id)
        if not row:
            raise ValueError(f"Subscription not found: {subscription_id}")
        new_value = 0 if int(row_get(row, "active", 0) or 0) else 1
        self.set_subscription_active(subscription_id, new_value)
        return new_value

    def process_due_subscriptions(
        self,
        *,
        maximum_new_charges: int = 24,
        commit: bool = True,
        subscriptions: Optional[list[Any]] = None,
        reference_date: Optional[date] = None,
    ) -> int:
        """Create due charges for each active subscription up to reference_date.

        When subscriptions are overdue across multiple billing intervals (e.g. app
        was offline for multiple months), this iterates through all overdue due dates
        up to reference_date and creates ledger occurrences for each interval (BUG-AUD-C88).
        Ancient runaway backlog (>120 intervals) is skipped to protect against typos.
        """

        from services.payments import PaymentsService

        today = reference_date or date.today()
        service = PaymentsService(self)
        subs = (
            list(subscriptions)
            if subscriptions is not None
            else self.subscriptions(active_only=True)
        )
        maximum_new_charges = max(1, min(100, int(maximum_new_charges)))
        count = 0
        skipped = 0
        deferred = 0
        invalid = 0
        processed = 0
        created_ids: list[int] = []
        connection = self.db.conn
        savepoint_name = f"proc_subs_{uuid.uuid4().hex[:8]}"
        connection.execute(f"SAVEPOINT {savepoint_name}")
        try:
            for sub in subs:
                initial_due = service.subscription_scheduled_due(sub, today)
                latest, next_due, older_count = service.subscription_due_window(
                    sub,
                    today,
                    initial_due=initial_due,
                )
                if latest is None:
                    continue
                subscription_id = int(row_get(sub, "id", 0) or 0)
                if subscription_id <= 0:
                    continue

                # Safety guard for runaway historical anomalies (>120 intervals, e.g. year 2000 typo)
                if older_count > 120:
                    charge_date = latest.isoformat()
                    exists = connection.execute(
                        "SELECT 1 FROM subscription_occurrences "
                        "WHERE subscription_id=? AND due_date=?",
                        (subscription_id, charge_date),
                    ).fetchone()
                    if not exists and count >= maximum_new_charges:
                        deferred += 1
                        continue
                    transaction_id: Optional[int] = None
                    if not exists:
                        try:
                            transaction_id = self._create_subscription_charge_uncommitted(
                                subscription_id,
                                charge_date,
                                str(row_get(sub, "name", "") or ""),
                                abs(float(row_get(sub, "amount", 0.0) or 0.0)),
                                str(row_get(sub, "category", "Outros") or "Outros"),
                                str(
                                    row_get(sub, "account", "Conta principal")
                                    or "Conta principal"
                                ),
                                str(row_get(sub, "payment_method", "") or ""),
                            )
                        except ValueError:
                            invalid += 1
                            continue
                        if transaction_id is not None:
                            count += 1
                            created_ids.append(transaction_id)
                    if transaction_id is not None or exists:
                        connection.execute(
                            "UPDATE subscriptions "
                            "SET last_used_date=?, next_renewal_date=? WHERE id=?",
                            (charge_date, next_due.isoformat(), subscription_id),
                        )
                        processed += 1
                        skipped += older_count
                    continue

                # BUG-AUD-C88: Iterate through all overdue due dates up to today,
                # creating ledger occurrences for each overdue interval instead of
                # skipping intermediate months.
                overdue_dates, future_next = service.overdue_subscription_dates(
                    sub,
                    today,
                    initial_due=initial_due,
                )
                if not overdue_dates:
                    continue

                last_charged_date: Optional[str] = None
                deferred_due_date: Optional[date] = None
                sub_processed = False

                for due_date in overdue_dates:
                    charge_date = due_date.isoformat()
                    exists = connection.execute(
                        "SELECT 1 FROM subscription_occurrences "
                        "WHERE subscription_id=? AND due_date=?",
                        (subscription_id, charge_date),
                    ).fetchone()
                    if not exists and count >= maximum_new_charges:
                        deferred += 1
                        if deferred_due_date is None:
                            deferred_due_date = due_date
                        break
                    transaction_id: Optional[int] = None
                    if not exists:
                        try:
                            transaction_id = self._create_subscription_charge_uncommitted(
                                subscription_id,
                                charge_date,
                                str(row_get(sub, "name", "") or ""),
                                abs(float(row_get(sub, "amount", 0.0) or 0.0)),
                                str(row_get(sub, "category", "Outros") or "Outros"),
                                str(
                                    row_get(sub, "account", "Conta principal")
                                    or "Conta principal"
                                ),
                                str(row_get(sub, "payment_method", "") or ""),
                            )
                        except ValueError:
                            invalid += 1
                            continue
                        if transaction_id is not None:
                            count += 1
                            created_ids.append(transaction_id)
                    if transaction_id is not None or exists:
                        last_charged_date = charge_date
                        sub_processed = True

                if sub_processed and last_charged_date is not None:
                    next_renewal = (
                        deferred_due_date.isoformat()
                        if deferred_due_date is not None
                        else future_next.isoformat()
                    )
                    connection.execute(
                        "UPDATE subscriptions "
                        "SET last_used_date=?, next_renewal_date=? WHERE id=?",
                        (last_charged_date, next_renewal, subscription_id),
                    )
                    processed += 1
            self._capture_planning_state(commit=False)
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if commit:
                connection.commit()
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if commit:
                connection.rollback()
            raise

        self.last_subscription_processing_report = {
            "created": count,
            "processed": processed,
            "historical_skipped": skipped,
            "deferred": deferred,
            "invalid": invalid,
            "created_ids": created_ids,
        }
        return count

    def subscription_occurrence_exists(
        self, subscription_id: int, due_date: str
    ) -> bool:
        return bool(
            self.scalar(
                "SELECT COUNT(*) FROM subscription_occurrences "
                "WHERE subscription_id=? AND due_date=?",
                (int(subscription_id), str(due_date or "")[:10]),
            )
        )

    def add_subscription_occurrence(
        self,
        subscription_id: int,
        due_date: str,
        transaction_id: Optional[int],
    ) -> None:
        self.db.conn.execute(
            "INSERT OR IGNORE INTO subscription_occurrences"
            "(subscription_id,due_date,transaction_id) VALUES(?,?,?)",
            (
                int(subscription_id),
                str(due_date or "")[:10],
                int(transaction_id) if transaction_id is not None else None,
            ),
        )
        self.db.conn.commit()

    def create_subscription_charge(
        self,
        subscription_id: int,
        due_date: str,
        name: str,
        amount: float,
        category: str,
        account: str = "Conta principal",
        payment_method: str = "",
    ) -> Optional[int]:
        """Atomically create a subscription movement, metadata and occurrence."""
        connection = self.db.conn
        connection.execute("SAVEPOINT subscription_charge")
        try:
            transaction_id = self._create_subscription_charge_uncommitted(
                subscription_id,
                due_date,
                name,
                amount,
                category,
                account,
                payment_method,
            )
            connection.execute("RELEASE SAVEPOINT subscription_charge")
            connection.commit()
            return transaction_id
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT subscription_charge")
            connection.execute("RELEASE SAVEPOINT subscription_charge")
            raise

    def _create_subscription_charge_uncommitted(
        self,
        subscription_id: int,
        due_date: str,
        name: str,
        amount: float,
        category: str,
        account: str = "Conta principal",
        payment_method: str = "",
    ) -> Optional[int]:
        """Create one charge inside the caller's existing transaction."""

        subscription_id = int(subscription_id)
        due_date = str(due_date or "")[:10]
        connection = self.db.conn
        exists = connection.execute(
            "SELECT 1 FROM subscription_occurrences "
            "WHERE subscription_id=? AND due_date=?",
            (subscription_id, due_date),
        ).fetchone()
        if exists:
            return None
        normalized_amount = abs(
            require_nonzero_eur(amount, field_name="valor da subscrição")
        )
        transaction_id = self._insert_transaction(
            due_date,
            f"{name} (Subscrição)",
            -normalized_amount,
            category or "Outros",
            str(account or "Conta principal").strip() or "Conta principal",
        )
        self._set_transaction_meta_ext(
            transaction_id,
            purchase_date=due_date,
            payment_date=due_date,
            merchant=name,
            payment_method=str(payment_method or "").strip(),
        )
        connection.execute(
            "INSERT INTO subscription_occurrences"
            "(subscription_id,due_date,transaction_id) VALUES(?,?,?)",
            (subscription_id, due_date, transaction_id),
        )
        return transaction_id

    def record_subscription_payment(
        self,
        subscription_id: int,
        payment_date: str,
        next_renewal_date: str,
        *,
        commit: bool = True,
    ) -> Optional[int]:
        """Atomically save a manual payment and advance its subscription."""

        subscription_id = int(subscription_id)
        payment_date = str(payment_date or "")[:10]
        next_renewal_date = str(next_renewal_date or "")[:10]
        subscription = self.subscription_by_id(subscription_id)
        if subscription is None:
            raise ValueError(f"Subscription not found: {subscription_id}")
        connection = self.db.conn
        if commit:
            connection.commit()
            connection.execute("SAVEPOINT manual_subscription_payment")
        try:
            exists = connection.execute(
                "SELECT 1 FROM subscription_occurrences "
                "WHERE subscription_id=? AND due_date=?",
                (subscription_id, payment_date),
            ).fetchone()
            if exists:
                if commit:
                    connection.execute("RELEASE SAVEPOINT manual_subscription_payment")
                    connection.commit()
                return None
            name = str(row_get(subscription, "name", "") or "")
            period = str(row_get(subscription, "period", "Mensal") or "Mensal")
            transaction_id = self._insert_transaction(
                payment_date,
                f"{name} (Subscrição)",
                -abs(
                    require_nonzero_eur(
                        row_get(subscription, "amount", 0),
                        field_name="valor da subscrição",
                    )
                ),
                str(row_get(subscription, "category", "Outros") or "Outros"),
                str(
                    row_get(subscription, "account", "Conta principal")
                    or "Conta principal"
                ),
                f"Pagamento de subscrição ({period})",
            )
            self._set_transaction_meta_ext(
                transaction_id,
                purchase_date=payment_date,
                payment_date=payment_date,
                merchant=name,
                payment_method=str(row_get(subscription, "payment_method", "") or ""),
            )
            connection.execute(
                "INSERT INTO subscription_occurrences"
                "(subscription_id,due_date,transaction_id) VALUES(?,?,?)",
                (subscription_id, payment_date, transaction_id),
            )
            cursor = connection.execute(
                "UPDATE subscriptions "
                "SET last_used_date=?, next_renewal_date=? WHERE id=?",
                (payment_date, next_renewal_date, subscription_id),
            )
            if int(cursor.rowcount or 0) != 1:
                raise ValueError(f"Subscription not found: {subscription_id}")
            self._capture_planning_state(commit=False)
            if commit:
                connection.execute("RELEASE SAVEPOINT manual_subscription_payment")
                connection.commit()
        except Exception:
            if commit:
                connection.execute("ROLLBACK TO SAVEPOINT manual_subscription_payment")
                connection.execute("RELEASE SAVEPOINT manual_subscription_payment")
                connection.rollback()
            raise
        return transaction_id

    def subscription_occurrence_cost(
        self,
        subscription_id: int,
        start: str = "1900-01-01",
        end: str = "2999-12-31",
    ) -> float:
        return float(
            self.scalar(
                f"""
                SELECT COALESCE(SUM(ABS(t.amount)),0)
                FROM subscription_occurrences o
                JOIN transactions t ON t.id=o.transaction_id
                WHERE o.subscription_id=? AND o.due_date>=? AND o.due_date<=?
                  AND {sql_finite_number("t.amount")}
                """,
                (int(subscription_id), start, end),
            )
            or 0
        )

    def scheduled_payments(self) -> list[Any]:
        return self.q(
            "SELECT * FROM scheduled_payments ORDER BY active DESC, start_date DESC, id DESC"
        )

    def scheduled_payment_by_id(self, payment_id: int) -> Optional[Any]:
        rows = self.q("SELECT * FROM scheduled_payments WHERE id=?", (int(payment_id),))
        return rows[0] if rows else None

    def _scheduled_months_total(self, months_total: int) -> int:
        try:
            value = int(months_total)
        except (TypeError, ValueError) as exc:
            raise ValueError("O número de mensalidades tem de ser um inteiro.") from exc
        if value < 0:
            raise ValueError(
                "O número de mensalidades tem de ser 0 (sem fim) ou um inteiro positivo."
            )
        return value

    def add_scheduled_payment(
        self,
        name: str,
        category: str,
        subcategory: str,
        amount: float,
        account: str,
        payment_method: str,
        start_date: str,
        debit_day: int,
        months_total: int,
        notes: str = "",
    ) -> int:
        amount = abs(require_nonzero_eur(amount, field_name="valor do pagamento"))
        category_id, category = self.category_identity(
            category or "Outros",
            create=True,
            kind="expense",
        )
        account_id, account = self.account_identity(account)
        try:
            cur = self.db.conn.execute(
                """INSERT INTO scheduled_payments(
                    name,category,subcategory,amount,account,payment_method,start_date,
                    debit_day,months_total,notes,category_id,account_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    name.strip(),
                    category,
                    subcategory.strip(),
                    amount,
                    account,
                    payment_method.strip(),
                    start_date[:10],
                    max(1, min(31, int(debit_day or 1))),
                    self._scheduled_months_total(months_total),
                    notes.strip(),
                    category_id,
                    account_id,
                ),
            )
            self.db.conn.commit()
            return int(cur.lastrowid)
        except Exception:
            self.db.conn.rollback()
            raise

    def update_scheduled_payment(
        self,
        payment_id: int,
        name: str,
        category: str,
        subcategory: str,
        amount: float,
        account: str,
        payment_method: str,
        start_date: str,
        debit_day: int,
        months_total: int,
        notes: str = "",
        *,
        months_paid: Optional[int] = None,
        active: Optional[int] = None,
    ) -> None:
        existing = self.scheduled_payment_by_id(payment_id)
        if not existing:
            raise ValueError(f"Scheduled payment not found: {payment_id}")
        if months_paid is None:
            months_paid = int(row_get(existing, "months_paid", 0) or 0)
        if active is None:
            active = int(row_get(existing, "active", 1) or 0)
        amount = abs(require_nonzero_eur(amount, field_name="valor do pagamento"))
        category_id, category = self.category_identity(
            category or "Outros",
            create=True,
            kind="expense",
        )
        account_id, account = self.account_identity(account)
        try:
            cur = self.db.conn.execute(
                """
                UPDATE scheduled_payments
                SET name=?, category=?, subcategory=?, amount=?, account=?, payment_method=?,
                    start_date=?, debit_day=?, months_total=?, notes=?, months_paid=?, active=?,
                    category_id=?,account_id=?
                WHERE id=?
                """,
                (
                    name.strip(),
                    category,
                    subcategory.strip(),
                    amount,
                    account,
                    payment_method.strip(),
                    start_date[:10],
                    max(1, min(31, int(debit_day or 1))),
                    self._scheduled_months_total(months_total),
                    notes.strip(),
                    max(0, int(months_paid or 0)),
                    1 if int(active or 0) else 0,
                    category_id,
                    account_id,
                    int(payment_id),
                ),
            )
            self.db.conn.commit()
            if cur.rowcount == 0:
                raise ValueError(f"Scheduled payment not found: {payment_id}")
        except Exception:
            self.db.conn.rollback()
            raise

    def delete_scheduled_payment(self, payment_id: int) -> None:
        connection = self.db.conn
        connection.execute("SAVEPOINT delete_scheduled_payment")
        try:
            connection.execute(
                "DELETE FROM scheduled_payment_occurrences WHERE scheduled_id=?",
                (int(payment_id),),
            )
            cursor = connection.execute(
                "DELETE FROM scheduled_payments WHERE id=?", (int(payment_id),)
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Scheduled payment not found: {payment_id}")
            connection.execute("RELEASE SAVEPOINT delete_scheduled_payment")
            connection.commit()
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT delete_scheduled_payment")
            connection.execute("RELEASE SAVEPOINT delete_scheduled_payment")
            raise

    def scheduled_payment_occurrence_exists(
        self,
        scheduled_id: int,
        due_date: str,
    ) -> bool:
        rows = self.q(
            "SELECT id FROM scheduled_payment_occurrences "
            "WHERE scheduled_id=? AND due_date=?",
            (int(scheduled_id), str(due_date)[:10]),
        )
        return bool(rows)

    def add_scheduled_payment_occurrence(
        self,
        scheduled_id: int,
        due_date: str,
        transaction_id: int,
    ) -> None:
        self.db.conn.execute(
            "INSERT OR IGNORE INTO scheduled_payment_occurrences"
            "(scheduled_id,due_date,transaction_id) VALUES(?,?,?)",
            (int(scheduled_id), str(due_date)[:10], int(transaction_id)),
        )
        self.db.conn.commit()

    def create_scheduled_payment_charge(
        self,
        scheduled_id: int,
        due_date: str,
        description: str,
        amount: float,
        category: str,
        account: str,
        notes: str = "",
        *,
        payment_method: str = "",
        subcategory: str = "",
        purchase_date: str = "",
        merchant: str = "",
    ) -> Optional[int]:
        """Atomically create one scheduled-payment movement and occurrence."""
        connection = self.db.conn
        connection.execute("SAVEPOINT scheduled_payment_charge")
        try:
            transaction_id = self._create_scheduled_payment_charge_uncommitted(
                scheduled_id,
                due_date,
                description,
                amount,
                category,
                account,
                notes,
                payment_method=payment_method,
                subcategory=subcategory,
                purchase_date=purchase_date or due_date,
                merchant=merchant,
            )
            connection.execute("RELEASE SAVEPOINT scheduled_payment_charge")
            connection.commit()
            return transaction_id
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT scheduled_payment_charge")
            connection.execute("RELEASE SAVEPOINT scheduled_payment_charge")
            raise

    def _create_scheduled_payment_charge_uncommitted(
        self,
        scheduled_id: int,
        due_date: str,
        description: str,
        amount: float,
        category: str,
        account: str,
        notes: str = "",
        *,
        payment_method: str = "",
        subcategory: str = "",
        purchase_date: str = "",
        merchant: str = "",
    ) -> Optional[int]:
        """Create one scheduled movement inside an existing transaction."""

        scheduled_id = int(scheduled_id)
        due_date = str(due_date or "")[:10]
        connection = self.db.conn
        # BUG-PAY-01: the occurrence row itself is the concurrency gate.
        # The previous SELECT-then-INSERT was a TOCTOU window: two threads
        # could both observe "no occurrence" and both create a movement for
        # the same (scheduled_id, due_date). UNIQUE(scheduled_id, due_date)
        # is enforced atomically at write time via INSERT OR IGNORE; the
        # loser detects it through changes()==0 and rolls its movement back.
        # transaction_id is NULLABLE precisely so a losing writer can insert
        # the tombstone first and discard it after losing the race.
        placeholder = connection.execute(
            "INSERT OR IGNORE INTO scheduled_payment_occurrences"
            "(scheduled_id,due_date,transaction_id) VALUES(?,?,NULL)",
            (scheduled_id, due_date),
        )
        if placeholder.rowcount == 0:
            # Lost the race: the (scheduled_id, due_date) occurrence already
            # exists, so a movement for this occurrence must not be created.
            return None
        try:
            normalized_amount = abs(
                require_nonzero_eur(amount, field_name="valor do pagamento")
            )
            transaction_id = self._insert_transaction(
                due_date,
                description,
                -normalized_amount,
                category or "Outros",
                account or "Conta principal",
                notes,
                subcategory,
            )
            self._set_transaction_meta_ext(
                transaction_id,
                payment_method=payment_method,
                subcategory=subcategory,
                purchase_date=purchase_date or due_date,
                payment_date=due_date,
                merchant=merchant,
            )
        except Exception:
            # Discard the placeholder occurrence row so a later retry can
            # succeed. No explicit rollback: this helper runs inside the
            # caller's transaction (or SAVEPOINT in the batch generator), and
            # sqlite3.Connection.rollback() would destroy that outer state.
            # Any partial movement writes from the failed attempt remain in
            # the caller's uncommitted transaction and vanish with the
            # caller's own rollback on failure.
            connection.execute(
                "DELETE FROM scheduled_payment_occurrences WHERE id=?",
                (int(placeholder.lastrowid),),
            )
            raise
        connection.execute(
            "UPDATE scheduled_payment_occurrences SET transaction_id=? "
            "WHERE id=?",
            (int(transaction_id), int(placeholder.lastrowid)),
        )
        return transaction_id

    def scheduled_payment_occurrence_count(self, scheduled_id: int) -> int:
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM scheduled_payment_occurrences "
                "WHERE scheduled_id=?",
                (int(scheduled_id),),
            )
        )

    def update_scheduled_payment_progress(
        self,
        scheduled_id: int,
        months_paid: int,
        active: int,
    ) -> None:
        cursor = self._update_scheduled_payment_progress_uncommitted(
            scheduled_id,
            months_paid,
            active,
        )
        self.db.conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(f"Scheduled payment not found: {scheduled_id}")

    def _update_scheduled_payment_progress_uncommitted(
        self,
        scheduled_id: int,
        months_paid: int,
        active: int,
    ) -> Any:
        return self.db.conn.execute(
            "UPDATE scheduled_payments SET months_paid=?, active=? WHERE id=?",
            (
                max(0, int(months_paid)),
                1 if int(active or 0) else 0,
                int(scheduled_id),
            ),
        )

    def goals(self, limit: Optional[int] = None) -> list[Any]:
        sql = """
            SELECT *
            FROM goals
            ORDER BY CASE WHEN target_amount>0 THEN saved_amount/target_amount ELSE 0 END DESC, id DESC
        """
        if limit is not None:
            return self.q(f"{sql} LIMIT ?", (int(limit),))
        return self.q(sql)

    def goal_by_id(self, goal_id: int) -> Optional[Any]:
        rows = self.q("SELECT * FROM goals WHERE id=?", (int(goal_id),))
        return rows[0] if rows else None

    def add_goal(
        self,
        name: str,
        target_amount: float,
        saved_amount: float,
        monthly_contribution: float,
        deadline: str,
        notes: str = "",
        goal_type: str = "standard",
    ) -> int:
        structured_type = (
            "emergency_fund"
            if str(goal_type or "").strip() == "emergency_fund"
            else "standard"
        )
        connection = self.db.conn
        connection.execute("SAVEPOINT add_goal")

        try:
            cur = connection.execute(
                """
                INSERT INTO goals(
                    name,target_amount,saved_amount,monthly_contribution,
                    deadline,notes,goal_type
                )
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    name.strip(),
                    float(target_amount or 0),
                    float(saved_amount or 0),
                    float(monthly_contribution or 0),
                    (deadline or "")[:10],
                    notes.strip(),
                    structured_type,
                ),
            )
            goal_id = int(cur.lastrowid)
            if structured_type == "emergency_fund":
                connection.execute(
                    "UPDATE goals SET goal_type='standard' "
                    "WHERE goal_type='emergency_fund' AND id<>?",
                    (goal_id,),
                )
            self._capture_planning_state(commit=False)
            connection.execute("RELEASE SAVEPOINT add_goal")
            connection.commit()
            return goal_id
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT add_goal")
            connection.execute("RELEASE SAVEPOINT add_goal")
            connection.rollback()
            raise

    def update_goal(
        self,
        goal_id: int,
        name: str,
        target_amount: float,
        saved_amount: float,
        monthly_contribution: float,
        deadline: str,
        notes: str = "",
        goal_type: Optional[str] = None,
    ) -> None:
        values = (
            name.strip(),
            float(target_amount or 0),
            float(saved_amount or 0),
            float(monthly_contribution or 0),
            (deadline or "")[:10],
            notes.strip(),
        )
        connection = self.db.conn
        connection.execute("SAVEPOINT update_goal")

        try:
            if goal_type is None:
                cur = connection.execute(
                    """
                    UPDATE goals
                    SET name=?, target_amount=?, saved_amount=?,
                        monthly_contribution=?, deadline=?, notes=?
                    WHERE id=?
                    """,
                    (*values, int(goal_id)),
                )
            else:
                structured_type = (
                    "emergency_fund"
                    if str(goal_type or "").strip() == "emergency_fund"
                    else "standard"
                )
                cur = connection.execute(
                    """
                    UPDATE goals
                    SET name=?, target_amount=?, saved_amount=?,
                        monthly_contribution=?, deadline=?, notes=?, goal_type=?
                    WHERE id=?
                    """,
                    (*values, structured_type, int(goal_id)),
                )
                if cur.rowcount > 0 and structured_type == "emergency_fund":
                    connection.execute(
                        "UPDATE goals SET goal_type='standard' "
                        "WHERE goal_type='emergency_fund' AND id<>?",
                        (int(goal_id),),
                    )
            if cur.rowcount == 0:
                raise ValueError(f"Goal not found: {goal_id}")
            self._capture_planning_state(commit=False)
            connection.execute("RELEASE SAVEPOINT update_goal")
            connection.commit()
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT update_goal")
            connection.execute("RELEASE SAVEPOINT update_goal")
            connection.rollback()
            raise

    def adjust_goal_with_transaction(
        self,
        goal_id: int,
        amount: float,
        *,
        deposit: bool,
        transaction_date: str,
        account: str = "Conta principal",
        commit: bool = True,
    ) -> int:
        """Adjust a goal and create the matching movement as one operation."""

        goal_id = int(goal_id)
        amount = abs(float(amount or 0))
        if amount <= 0:
            raise ValueError("O valor tem de ser superior a zero.")
        connection = self.db.conn
        savepoint_name = f"adjust_goal_{uuid.uuid4().hex[:8]}"
        connection.execute(f"SAVEPOINT {savepoint_name}")
        try:
            row = connection.execute(
                "SELECT * FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Goal not found: {goal_id}")
            current = float(row["saved_amount"] or 0)
            if not deposit and amount > current:
                raise ValueError("O levantamento excede o valor guardado no objetivo.")
            new_saved = current + amount if deposit else current - amount
            cursor = connection.execute(
                "UPDATE goals SET saved_amount=? WHERE id=?",
                (new_saved, goal_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Goal not found: {goal_id}")
            goal_name = str(row["name"] or "Objetivo")
            description = (
                f"Reforço do pote: {goal_name}"
                if deposit
                else f"Levantamento do pote: {goal_name}"
            )
            notes = (
                f"Depósito no pote {goal_name}"
                if deposit
                else f"Resgate do pote {goal_name}"
            )
            transaction_id = self._insert_transaction(
                str(transaction_date or "")[:10],
                description,
                -amount if deposit else amount,
                "Objetivos e Poupança",
                str(account or "Conta principal").strip() or "Conta principal",
                notes,
                goal_name,
            )
            connection.execute(
                "UPDATE transactions SET goal_id=?,goal_name_snapshot=? WHERE id=?",
                (goal_id, goal_name, transaction_id),
            )
            self._set_transaction_meta_ext(
                transaction_id,
                subcategory=goal_name,
                purchase_date=str(transaction_date or "")[:10],
                payment_date=str(transaction_date or "")[:10],
            )
            self._capture_planning_state(commit=False)
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if commit:
                connection.commit()
            return transaction_id
        except Exception:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            if commit:
                connection.rollback()
            raise

    def delete_goal(self, goal_id: int) -> None:
        """Delete a goal without orphaning its historical transfer context."""

        goal_id = int(goal_id)
        connection = self.db.conn
        connection.execute("SAVEPOINT delete_goal")

        try:
            row = connection.execute(
                "SELECT name FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Goal not found: {goal_id}")
            goal_name = str(row["name"] or "Objetivo")
            connection.execute(
                """
                UPDATE transactions
                SET goal_name_snapshot=COALESCE(
                        NULLIF(TRIM(goal_name_snapshot),''), ?
                    ),
                    goal_id=NULL
                WHERE goal_id=?
                """,
                (goal_name, goal_id),
            )
            cursor = connection.execute("DELETE FROM goals WHERE id=?", (goal_id,))
            if cursor.rowcount != 1:
                raise RuntimeError("O objetivo não foi eliminado por completo.")
            self._capture_planning_state(commit=False)
            connection.execute("RELEASE SAVEPOINT delete_goal")
            connection.commit()
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT delete_goal")
            connection.execute("RELEASE SAVEPOINT delete_goal")
            connection.rollback()
            raise

    def planned_purchases(self) -> list[Any]:
        return self.q(
            "SELECT * FROM planned_purchases ORDER BY CASE priority WHEN 'Alta' THEN 0 WHEN 'Média' THEN 1 ELSE 2 END, desired_date, name"
        )

    def planned_purchase_by_id(self, purchase_id: int) -> Optional[Any]:
        rows = self.q("SELECT * FROM planned_purchases WHERE id=?", (int(purchase_id),))
        return rows[0] if rows else None

    def add_planned_purchase(
        self,
        name: str,
        target_amount: float,
        saved_amount: float,
        desired_date: str,
        priority: str,
        notes: str = "",
    ) -> int:
        try:
            cur = self.db.conn.execute(
                "INSERT INTO planned_purchases(name,target_amount,saved_amount,desired_date,priority,notes) VALUES(?,?,?,?,?,?)",
                (
                    name.strip(),
                    float(target_amount or 0),
                    float(saved_amount or 0),
                    (desired_date or "")[:10],
                    priority.strip() or "Média",
                    notes.strip(),
                ),
            )
            self.db.conn.commit()
            return int(cur.lastrowid)
        except Exception:
            self.db.conn.rollback()
            raise

    def update_planned_purchase(
        self,
        purchase_id: int,
        name: str,
        target_amount: float,
        saved_amount: float,
        desired_date: str,
        priority: str,
        notes: str = "",
    ) -> None:
        try:
            cur = self.db.conn.execute(
                """
                UPDATE planned_purchases
                SET name=?, target_amount=?, saved_amount=?, desired_date=?, priority=?, notes=?
                WHERE id=?
                """,
                (
                    name.strip(),
                    float(target_amount or 0),
                    float(saved_amount or 0),
                    (desired_date or "")[:10],
                    priority.strip() or "Média",
                    notes.strip(),
                    int(purchase_id),
                ),
            )
            self.db.conn.commit()
            if cur.rowcount == 0:
                raise ValueError(f"Planned purchase not found: {purchase_id}")
        except Exception:
            self.db.conn.rollback()
            raise

    def delete_planned_purchase(self, purchase_id: int) -> None:
        try:
            self.db.conn.execute(
                "DELETE FROM planned_purchases WHERE id=?", (int(purchase_id),)
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def monthly_goals(self, ym: str) -> list[Any]:
        return self.q(
            "SELECT * FROM monthly_goals WHERE month=? ORDER BY id DESC", (ym,)
        )

    def add_monthly_goal(
        self,
        ym: str,
        name: str,
        goal_type: str,
        category: str,
        target: float,
        notes: str = "",
    ) -> None:
        category_id = ""
        category_name = category.strip()
        if category_name:
            category_id, category_name = self.category_identity(
                category_name,
                create=True,
                kind="expense",
            )
        with self._planning_write("add_monthly_goal", capture_state=True) as connection:
            connection.execute(
                "INSERT INTO monthly_goals("
                "month,name,goal_type,category,category_id,target_value,notes"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    ym,
                    name.strip(),
                    goal_type,
                    category_name,
                    category_id,
                    float(target or 0),
                    notes.strip(),
                ),
            )

    def delete_monthly_goal(self, goal_id: int) -> None:
        with self._planning_write("delete_monthly_goal", capture_state=True) as connection:
            connection.execute(
                "DELETE FROM monthly_goals WHERE id=?", (int(goal_id),)
            )

    def month_target(self, ym: str) -> dict[str, float]:
        rows = self.q("SELECT * FROM monthly_targets WHERE month=?", (ym,))
        if not rows:
            # BUG-PAN-01: "has_row" distinguishes "no meta configured" from a
            # meta explicitly set to 0 €.
            return {
                "expense_target": 0.0,
                "savings_target": 0.0,
                "investment_target": 0.0,
                "has_row": 0.0,
            }
        return {
            k: safe_float(rows[0][k])
            for k in ["expense_target", "savings_target", "investment_target"]
        } | {"has_row": 1.0}

    def set_month_target(
        self, ym: str, expense: float, savings: float = 0.0, investment: float = 0.0
    ) -> None:
        try:
            self.db.conn.execute(
                """INSERT INTO monthly_targets(month,expense_target,savings_target,investment_target,updated_at)
                VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(month) DO UPDATE SET expense_target=excluded.expense_target, savings_target=excluded.savings_target, investment_target=excluded.investment_target, updated_at=CURRENT_TIMESTAMP
                """,
                (ym, float(expense or 0), float(savings or 0), float(investment or 0)),
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def available_years(self) -> list[int]:
        rows = self.q(
            "SELECT DISTINCT CAST(substr(COALESCE(NULLIF(m.payment_date,''),t.date),1,4) AS INTEGER) AS y "
            "FROM transactions t LEFT JOIN transaction_meta m ON m.transaction_id=t.id "
            "ORDER BY y DESC"
        )
        years = [int(r["y"]) for r in rows if r["y"]]
        current = date.today().year
        if current not in years:
            years.insert(0, current)
        return years or [current]

    def month_period(self, year: int, month: int) -> tuple[str, str, str]:
        start = date(int(year), int(month), 1)
        nxt = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        return (
            start.isoformat(),
            (nxt - timedelta(days=1)).isoformat(),
            start.strftime("%Y-%m"),
        )
