"""Shared SQLite predicates for trustworthy financial calculations."""

from __future__ import annotations


MAX_ABSOLUTE_FINANCIAL_VALUE = 1.0e15

# These movements only relocate money already owned by the user.  They must
# remain in the account ledger, but must never be presented as income or
# expenses in financial analytics.
INTERNAL_FLOW_CATEGORIES = (
    "Transferência Interna",
    "Objetivos e Poupança",
)


def sql_finite_number(
    column: str,
    *,
    non_negative: bool = False,
    non_zero: bool = False,
) -> str:
    """Build a SQLite predicate that rejects text, NULL, NaN and infinities."""

    expression = str(column).strip()
    clauses = [
        f"{expression} IS NOT NULL",
        f"TYPEOF({expression}) IN ('integer','real')",
        f"{expression} = {expression}",
        f"ABS({expression}) <= {MAX_ABSOLUTE_FINANCIAL_VALUE:.1e}",
    ]
    if non_negative:
        clauses.append(f"{expression} >= 0")
    if non_zero:
        clauses.append(f"ABS({expression}) >= 0.005")
    return "(" + " AND ".join(clauses) + ")"


def sql_iso_date(column: str, *, allow_empty: bool = False) -> str:
    """Build a strict ``YYYY-MM-DD`` calendar-date predicate for SQLite."""

    expression = str(column).strip()
    valid = (
        f"TYPEOF({expression})='text' "
        f"AND LENGTH({expression})=10 "
        f"AND {expression} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
        f"AND DATE({expression}, '+0 days')={expression}"
    )
    if allow_empty:
        return f"({expression}='' OR ({valid}))"
    return f"({valid})"


def sql_month_key(column: str, *, allow_empty: bool = False) -> str:
    """Build a strict ``YYYY-MM`` predicate by validating its first day."""

    expression = str(column).strip()
    valid = (
        f"TYPEOF({expression})='text' "
        f"AND LENGTH({expression})=7 "
        f"AND {expression} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
        f"AND STRFTIME('%Y-%m', {expression} || '-01')={expression}"
    )
    if allow_empty:
        return f"({expression}='' OR ({valid}))"
    return f"({valid})"


def sql_valid_transaction_date(alias: str = "t", meta_alias: str = "m") -> str:
    expression = f"COALESCE(NULLIF({meta_alias}.payment_date,''),{alias}.date)"
    return sql_iso_date(expression)


def sql_external_financial_flow(column: str = "t.category") -> str:
    """Exclude ledger-only transfers from income and expense calculations."""

    expression = str(column).strip()
    values = ",".join(
        "'" + category.replace("'", "''") + "'" for category in INTERNAL_FLOW_CATEGORIES
    )
    return f"COALESCE({expression},'') NOT IN ({values})"


VALID_TRANSACTION_AMOUNT_SQL = sql_finite_number("t.amount", non_zero=True)
VALID_TRANSACTION_DATE_SQL = sql_valid_transaction_date()


__all__ = [
    "MAX_ABSOLUTE_FINANCIAL_VALUE",
    "INTERNAL_FLOW_CATEGORIES",
    "VALID_TRANSACTION_AMOUNT_SQL",
    "VALID_TRANSACTION_DATE_SQL",
    "sql_finite_number",
    "sql_external_financial_flow",
    "sql_iso_date",
    "sql_month_key",
    "sql_valid_transaction_date",
]
