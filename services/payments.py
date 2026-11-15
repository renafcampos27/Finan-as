"""Scheduled-payment and recurring-expense orchestration."""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Optional

from database.validation import sql_external_financial_flow
from utils.helpers import (
    add_months,
    parse_flexible_date,
    row_get,
    safe_float,
)

if TYPE_CHECKING:
    from database.repo import Repo
    from services.change_journal import ChangeJournalService


def _anchor_from_date_value(value: Any) -> Optional[int]:
    """Anchor day derived from a stored date, with end-of-month intent.

    BUG-PAY-02: a schedule born on the last day of a month (Jan 31, Feb 28,
    Apr 30) means "end of month", not a literal day 28/30.  Returning the
    universal day-31 anchor keeps March at 31 instead of permanently
    drifting to the shorter month's day.  The stored anchor is never
    mutated — ``_clamped_anchor_date`` still clamps to each month's length.
    """

    parsed = parse_flexible_date(value)
    if parsed is None:
        return None
    if parsed.day == calendar.monthrange(parsed.year, parsed.month)[1]:
        return 31
    return parsed.day


def next_business_day(day: date) -> date:
    """BUG-PAY-03: push a due date falling on a weekend to the next Monday.

    Documented limitation: Portuguese banking holidays (TARGET2) are not
    modelled yet — no holiday calendar exists in the repo — so only
    Saturdays and Sundays are adjusted for now.
    """

    while day.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        day = day + timedelta(days=1)
    return day


def _subscription_anchor_day(row: Any, fallback: int = 1) -> int:
    """Resolve the stable renewal/debit day of a schedule row.

    RECUR-01: the anchor must come from an explicit column (renewal_day),
    from debit_day, or from a stored date (start_date / last_used_date) —
    never from the last generated (possibly clamped) due date alone,
    otherwise a 31st anchored schedule drifts to 28 for good after the first
    short month.

    BUG-PAY-02: when the anchor is derived from a stored date that is itself
    the last day of its month, the intent is "end of month" and the
    universal day-31 anchor is used (a Feb 28 / Apr 30 start still lands on
    Mar 31 / May 31).  Explicit renewal_day/debit_day columns are honoured
    literally.
    """

    anchor = row_get(row, "renewal_day", None)
    if anchor in (None, "", 0):
        anchor = row_get(row, "debit_day", None)
    if anchor in (None, "", 0):
        start_anchor = _anchor_from_date_value(row_get(row, "start_date"))
        if start_anchor is not None:
            anchor = start_anchor
    if anchor in (None, "", 0):
        last_anchor = _anchor_from_date_value(row_get(row, "last_used_date"))
        if last_anchor is not None:
            anchor = last_anchor
    if anchor in (None, "", 0):
        return max(1, min(31, int(fallback)))
    try:
        return max(1, min(31, int(anchor)))
    except (TypeError, ValueError):
        return max(1, min(31, int(fallback)))


def _clamped_anchor_date(base: date, anchor_day: int) -> date:
    """Place ``anchor_day`` in ``base``'s month, clamped to month length.

    RECUR-01: min(anchor_day, days_in_month) keeps February at 28/29 while
    March returns to 31 — the anchor itself never changes.
    """

    day = min(max(1, int(anchor_day)), calendar.monthrange(base.year, base.month)[1])
    return base.replace(day=day)


class PaymentsService:
    """Apply payment timing rules while ``Repo`` persists rows and occurrences."""

    def __init__(
        self,
        repo: "Repo",
        change_journal_service: Optional["ChangeJournalService"] = None,
    ) -> None:
        self.repo = repo
        self.change_journal_service = change_journal_service

    @staticmethod
    def _months_total(row: Any) -> int:
        value = row_get(row, "months_total", 1)
        if value is None or value == "":
            return 1
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def subscription_monthly_amount(row: Any) -> float:
        amount = safe_float(row_get(row, "amount"))
        period = str(row_get(row, "period", "Mensal") or "Mensal").strip().lower()
        if period.startswith("anual"):
            return amount / 12
        if period.startswith("trimes"):
            return amount / 3
        if period.startswith("semes"):
            return amount / 6
        return amount

    def active_subscription_monthly_total(self) -> float:
        return sum(
            self.subscription_monthly_amount(row)
            for row in self.repo.subscriptions(active_only=True)
        )

    @staticmethod
    def subscription_interval_months(row: Any) -> int:
        period = str(row_get(row, "period", "Mensal") or "Mensal").strip().casefold()
        if period.startswith("anual"):
            return 12
        if period.startswith("semes"):
            return 6
        if period.startswith("trimes"):
            return 3
        return 1

    @classmethod
    def advance_subscription_due(cls, row: Any, due: date) -> date:
        # RECUR-01: resolve the anchor from the row (renewal_day/debit_day/
        # start_date) instead of trusting the current due date's day, which
        # may already be a short-month clamp (28/29) of the original anchor.
        start_d = parse_flexible_date(row_get(row, "start_date"))
        anchor_day = _subscription_anchor_day(
            row, start_d.day if start_d else due.day
        )
        return _clamped_anchor_date(
            add_months(due, cls.subscription_interval_months(row)),
            anchor_day,
        )

    @classmethod
    def subscription_scheduled_due(
        cls,
        row: Any,
        reference: Optional[date] = None,
    ) -> date:
        """Return the stored/anchored due date without skipping an overdue charge."""
        reference = reference or date.today()
        # RECUR-01: the fallback anchor comes from the row itself (start_date/
        # last chain), never from ``reference.day`` which changes every run.
        renewal_day = _subscription_anchor_day(row, 1)
        explicit = parse_flexible_date(row_get(row, "next_renewal_date"))
        if explicit is not None:
            return explicit
        last_used = parse_flexible_date(row_get(row, "last_used_date"))
        if last_used is not None:
            # RECUR-01: clamp against the stable anchor, not last_used's day.
            return _clamped_anchor_date(
                add_months(last_used, cls.subscription_interval_months(row)),
                renewal_day,
            )
        return _clamped_anchor_date(reference.replace(day=1), renewal_day)

    @classmethod
    def next_subscription_due_date(
        cls,
        row: Any,
        reference: Optional[date] = None,
    ) -> date:
        reference = reference or date.today()
        candidate = cls.subscription_scheduled_due(row, reference)
        if candidate >= reference:
            return candidate
        _latest, next_due, _skipped = cls.subscription_due_window(
            row,
            reference,
            initial_due=candidate,
        )
        return next_due

    @classmethod
    def subscription_due_window(
        cls,
        row: Any,
        reference: date,
        *,
        initial_due: Optional[date] = None,
    ) -> tuple[Optional[date], date, int]:
        """Return the latest due date, next date and omitted older renewals.

        The calculation jumps arithmetically across stale dates instead of
        iterating through years of historical renewals.  This prevents an old
        or mistyped date from turning into hundreds of movements.
        """

        due = initial_due or cls.subscription_scheduled_due(row, reference)
        if due > reference:
            return None, due, 0
        interval = cls.subscription_interval_months(row)
        month_distance = max(
            0,
            (reference.year - due.year) * 12 + reference.month - due.month,
        )
        steps = month_distance // interval
        # RECUR-01: anchor day resolved from the row (renewal_day/debit_day/
        # start_date); ``due.day`` is only a last-resort fallback. Each jump is
        # computed against the immutable anchor, not accumulated month to
        # month, so Feb 28/29 does not overwrite a day-31 anchor.
        renewal_day = _subscription_anchor_day(row, due.day)
        latest = (
            _clamped_anchor_date(add_months(due, steps * interval), renewal_day)
            if steps
            else due
        )
        while latest > reference and steps > 0:
            steps -= 1
            latest = (
                _clamped_anchor_date(add_months(due, steps * interval), renewal_day)
                if steps
                else due
            )
        next_due = cls.advance_subscription_due(row, latest)
        if next_due <= reference:
            latest = next_due
            steps += 1
            next_due = cls.advance_subscription_due(row, latest)
        return latest, next_due, max(0, steps)

    @classmethod
    def overdue_subscription_dates(
        cls,
        row: Any,
        reference: date,
        *,
        initial_due: Optional[date] = None,
        max_occurrences: int = 120,
    ) -> tuple[list[date], date]:
        """Return all overdue dates up to reference (inclusive) and the next future due date.

        When subscriptions are overdue across multiple billing intervals (e.g. app was offline
        for multiple months), this iterates through every overdue interval up to reference,
        ensuring no intermediate billing months are dropped (BUG-AUD-C88).
        """
        curr = initial_due or cls.subscription_scheduled_due(row, reference)
        if curr > reference:
            return [], curr
        overdue: list[date] = []
        while curr <= reference and len(overdue) < max_occurrences:
            overdue.append(curr)
            curr = cls.advance_subscription_due(row, curr)
        return overdue, curr

    @classmethod
    def subscription_days_until(
        cls,
        row: Any,
        reference: Optional[date] = None,
    ) -> int:
        reference = reference or date.today()
        return (cls.next_subscription_due_date(row, reference) - reference).days

    def upcoming_subscriptions(
        self,
        rows: Optional[list[Any]] = None,
        *,
        reference: Optional[date] = None,
        within_days: int = 3,
    ) -> list[dict[str, Any]]:
        """Return active renewals in the next inclusive calendar-day window."""
        reference = reference or date.today()
        if rows is None:
            rows = list(self.repo.subscriptions(active_only=True))
        upcoming: list[dict[str, Any]] = []
        for row in rows:
            due = self.next_subscription_due_date(row, reference)
            days = (due - reference).days
            if 0 <= days <= max(0, int(within_days)):
                upcoming.append(
                    {
                        "name": str(row_get(row, "name", "Subscrição") or "Subscrição"),
                        "date": due.isoformat(),
                        "days": days,
                    }
                )
        upcoming.sort(
            key=lambda item: (int(item["days"]), str(item["name"]).casefold())
        )
        return upcoming

    @staticmethod
    def due_date_for_payment(row: Any, index: int) -> date:
        # RECUR-01: every occurrence is computed against the immutable anchor
        # (debit_day / start_date.day), so February's clamp to 28/29 never
        # overwrites a day-31 schedule — March returns to 31.
        start = parse_flexible_date(row_get(row, "start_date")) or date.today()
        debit_day = int(row_get(row, "debit_day", 0) or 0)
        if debit_day <= 0:
            # BUG-PAY-02: no explicit debit_day column — derive the anchor
            # from start_date with end-of-month intent (a Feb 28 / Apr 30
            # start anchors at the universal day 31 instead of freezing 28/30).
            debit_day = (
                _anchor_from_date_value(row_get(row, "start_date")) or start.day
            )
        return add_months(start, int(index), debit_day)

    def next_due_for_payment(self, row: Any) -> str:
        total = self._months_total(row)
        paid = int(row_get(row, "months_paid", 0) or 0)
        if total > 0 and paid >= total:
            return "Concluído"
        return self.effective_due_date_for_payment(row, paid).isoformat()

    @classmethod
    def effective_due_date_for_payment(cls, row: Any, index: int) -> date:
        """One effective date for preview, occurrence identity and posting.

        The immutable calendar anchor remains available via due_date_for_payment.
        Only weekends are shifted; banking holidays require a separate calendar.
        """
        return next_business_day(cls.due_date_for_payment(row, index))

    def generate_due_scheduled_payments(
        self,
        until: Optional[date] = None,
        *,
        maximum_new_charges: int = 24,
        maximum_per_payment: int = 12,
    ) -> int:
        """Create a reviewable, atomic batch of due scheduled payments."""

        until = until or date.today()
        if self.change_journal_service is not None:
            active_ids = [
                int(row["id"])
                for row in self.repo.scheduled_payments()
                if int(row_get(row, "active", 1) or 0) == 1
            ]
            created_ids = self.change_journal_service.run_with_created_transactions(
                lambda: self._generate_due_scheduled_payment_ids(
                    until,
                    maximum_new_charges=maximum_new_charges,
                    maximum_per_payment=maximum_per_payment,
                    commit=False,
                ),
                label=lambda ids: f"Pagamentos automáticos · {len(ids)} movimento(s)",
                action_type="automatic_payment",
                state_targets={
                    "scheduled_payments": {
                        "ids": active_ids,
                        "columns": ("months_paid", "active"),
                    }
                },
                delete_linked_occurrences=True,
            )
            return len(created_ids)
        return len(
            self._generate_due_scheduled_payment_ids(
                until,
                maximum_new_charges=maximum_new_charges,
                maximum_per_payment=maximum_per_payment,
                commit=True,
            )
        )

    def _generate_due_scheduled_payment_ids(
        self,
        until: date,
        *,
        maximum_new_charges: int,
        maximum_per_payment: int,
        commit: bool,
    ) -> list[int]:
        """Generate due movements inside either an owned or caller transaction."""

        maximum_new_charges = max(1, min(100, int(maximum_new_charges)))
        maximum_per_payment = max(1, min(24, int(maximum_per_payment)))
        created = 0
        created_ids: list[int] = []
        remaining_due = 0
        limited_payments = 0
        invalid_payments = 0
        connection = self.repo.db.conn
        if commit:
            connection.commit()
            connection.execute("SAVEPOINT generate_due_scheduled_payments")
        try:
            for row in self.repo.scheduled_payments():
                if int(row_get(row, "active", 1) or 0) != 1:
                    continue
                total = self._months_total(row)
                first_due = self.due_date_for_payment(row, 0)
                elapsed_months = max(
                    0,
                    (until.year - first_due.year) * 12
                    + until.month
                    - first_due.month
                    + 1,
                )
                maximum_months = (
                    min(total, elapsed_months) if total > 0 else elapsed_months
                )
                while (
                    maximum_months > 0
                    and self.effective_due_date_for_payment(row, maximum_months - 1) > until
                ):
                    maximum_months -= 1
                scheduled_id = int(row["id"])
                created_for_payment = 0
                stopped_by_limit = False
                invalid_row = False
                for index in range(maximum_months):
                    due = self.due_date_for_payment(row, index)
                    effective_due = self.effective_due_date_for_payment(row, index)
                    if effective_due > until:
                        break
                    # BUG-PAY-03: banks do not debit on weekends; the stored
                    # occurrence and its movement land on the next business
                    # day (Portuguese holidays are not modelled yet).  The
                    # schedule anchor itself is never shifted.
                    due_text = effective_due.isoformat()
                    exists = connection.execute(
                        "SELECT 1 FROM scheduled_payment_occurrences "
                        "WHERE scheduled_id=? AND due_date IN (?,?)",
                        (scheduled_id, due_text, due.isoformat()),
                    ).fetchone()
                    if exists:
                        continue
                    if (
                        created >= maximum_new_charges
                        or created_for_payment >= maximum_per_payment
                    ):
                        stopped_by_limit = True
                        break
                    description = str(row["name"])
                    if total > 1:
                        description = f"{description} ({index + 1}/{total})"
                    try:
                        transaction_id = (
                            self.repo._create_scheduled_payment_charge_uncommitted(
                                scheduled_id,
                                due_text,
                                description,
                                abs(float(row["amount"] or 0)),
                                row["category"] or "Outros",
                                row["account"] or "Conta principal",
                                row["notes"] or "",
                                payment_method=row["payment_method"] or "",
                                subcategory=row["subcategory"] or "",
                                purchase_date=row["start_date"] or due_text,
                                merchant=row["name"] or "",
                            )
                        )
                    except ValueError:
                        invalid_payments += 1
                        invalid_row = True
                        break
                    if transaction_id is not None:
                        created += 1
                        created_for_payment += 1
                        created_ids.append(transaction_id)
                paid_count = self.repo.scheduled_payment_occurrence_count(scheduled_id)
                due_total = maximum_months
                outstanding = max(0, due_total - paid_count)
                remaining_due += outstanding
                if invalid_row:
                    continue
                if stopped_by_limit and outstanding:
                    limited_payments += 1
                active = 0 if total > 0 and paid_count >= total else 1
                if (
                    created_for_payment
                    or paid_count != int(row_get(row, "months_paid", 0) or 0)
                    or active != int(row_get(row, "active", 1) or 0)
                ):
                    cursor = self.repo._update_scheduled_payment_progress_uncommitted(
                        scheduled_id,
                        paid_count,
                        active,
                    )
                    if cursor.rowcount == 0:
                        raise ValueError(f"Scheduled payment not found: {scheduled_id}")
            self.repo._capture_planning_state(commit=False)
            if commit:
                connection.execute("RELEASE SAVEPOINT generate_due_scheduled_payments")
                connection.commit()
        except Exception:
            if commit:
                connection.execute(
                    "ROLLBACK TO SAVEPOINT generate_due_scheduled_payments"
                )
                connection.execute("RELEASE SAVEPOINT generate_due_scheduled_payments")
                connection.rollback()
            raise
        self.last_generation_report = {
            "created": created,
            "remaining_due": remaining_due,
            "limited_payments": limited_payments,
            "invalid_payments": invalid_payments,
            "maximum_new_charges": maximum_new_charges,
            "maximum_per_payment": maximum_per_payment,
            "created_ids": created_ids,
        }
        return created_ids

    def likely_recurring_expenses(self, limit: int = 20) -> list[dict[str, Any]]:
        external_flow = sql_external_financial_flow("category")
        rows = self.repo.q(
            f"""
            SELECT LOWER(TRIM(description)) AS key,
                   MIN(description) AS description,
                   MIN(category) AS category,
                   MIN(account) AS account,
                   ROUND(AVG(ABS(amount)),2) AS amount,
                   ROUND(MIN(ABS(amount)),2) AS min_amount,
                   ROUND(MAX(ABS(amount)),2) AS max_amount,
                   COUNT(*) AS c,
                   COUNT(DISTINCT SUBSTR(date,1,7)) AS month_count,
                   MIN(date) AS first_date, MAX(date) AS last_date
            FROM transactions
            WHERE amount<0 AND {external_flow}
              AND LENGTH(TRIM(description))>0
            GROUP BY key, LOWER(TRIM(account))
            HAVING c>=2
               AND (MAX(ABS(amount))-MIN(ABS(amount)))
                   <= MAX(1.0, AVG(ABS(amount))*0.12)
            ORDER BY c DESC, last_date DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        result: list[dict[str, Any]] = []
        for source in rows:
            row = dict(source)
            count = int(row_get(source, "c", 0) or 0)
            month_count = int(row_get(source, "month_count", count) or count)
            spread = max(
                0.0,
                safe_float(row_get(source, "max_amount", row_get(source, "amount", 0)))
                - safe_float(
                    row_get(source, "min_amount", row_get(source, "amount", 0))
                ),
            )
            average = max(0.0, safe_float(row_get(source, "amount", 0)))
            variation = spread / average if average else 0.0
            if count >= 6 and month_count >= 5 and variation <= 0.05:
                confidence = "Alta"
            elif count >= 3 and month_count >= 3:
                confidence = "Média"
            else:
                confidence = "Baixa"
            row["confidence"] = confidence
            row["amount_variation_pct"] = variation * 100
            row["requires_confirmation"] = True
            result.append(row)
        return result
