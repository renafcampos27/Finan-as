"""Core domain entities using strict type hints and slot-optimized data classes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class Transaction:
    """Core movement entity in the personal finance ledger."""

    id: Optional[int]
    date: str
    description: str
    amount: float
    category: str
    account: str
    kind: str = "expense"  # 'expense', 'income', 'transfer'
    notes: str = ""
    merchant: str = ""
    payment_date: str = ""
    payment_month: str = ""
    stable_id: str = ""
    category_id: str = ""
    account_id: str = ""
    is_reimbursable: bool = False
    splits: list[TransactionSplit] = field(default_factory=list)

    @property
    def is_expense(self) -> bool:
        return self.kind == "expense" or self.amount < 0

    @property
    def is_income(self) -> bool:
        return self.kind == "income" or self.amount > 0

    @property
    def is_transfer(self) -> bool:
        return self.kind == "transfer"

    def absolute_amount(self) -> float:
        return abs(float(self.amount))

    @classmethod
    def from_row(cls, row: Any) -> Transaction:
        """Instantiate Transaction directly from sqlite3.Row or mapping."""

        def _get(k: str, default: Any = "") -> Any:
            if hasattr(row, "keys"):
                return row[k] if k in row.keys() else default
            if isinstance(row, dict):
                return row.get(k, default)
            return getattr(row, k, default)

        raw_id = _get("id", None)
        return cls(
            id=int(raw_id) if raw_id is not None else None,
            date=str(_get("date", "")),
            description=str(_get("description", "")),
            amount=float(_get("amount", 0.0)),
            category=str(_get("category", "")),
            account=str(_get("account", "")),
            kind=str(_get("kind", "expense")),
            notes=str(_get("notes", "")),
            merchant=str(_get("merchant", "")),
            payment_date=str(_get("payment_date", "")),
            payment_month=str(_get("payment_month", "")),
            stable_id=str(_get("stable_id", "")),
            category_id=str(_get("category_id", "")),
            account_id=str(_get("account_id", "")),
            is_reimbursable=bool(_get("is_reimbursable", False)),
        )


@dataclass(slots=True)
class TransactionSplit:
    """Split category allocation for a single movement."""

    id: Optional[int]
    transaction_id: int
    category: str
    percent: float
    amount: float
    category_id: str = ""
    created_at: str = ""


@dataclass(slots=True)
class Category:
    """Budget category definition."""

    name: str
    kind: str  # 'expense', 'income'
    colour: str = "#4A90E2"
    budget: float = 0.0
    budget_bucket: str = ""  # 'Necessidades', 'Desejos', 'Poupança'
    stable_id: str = ""
    is_active: bool = True


@dataclass(slots=True)
class FinancialAccount:
    """Liquid, savings, or investment account."""

    name: str
    account_type: str = "checking"
    currency: str = "EUR"
    reference_balance: float = 0.0
    balance_known: bool = True
    reference_date: str = ""
    account_key: str = ""
    stable_id: str = ""
    is_active: bool = True


@dataclass(slots=True)
class Subscription:
    """Recurring service or contract."""

    id: Optional[int]
    name: str
    amount: float
    category: str
    frequency: str = "Mensal"
    period: str = "Mensal"
    active: bool = True
    next_renewal_date: str = ""
    account: str = ""
    last_used_date: str = ""


@dataclass(slots=True)
class Holding:
    """Investment holding asset."""

    id: Optional[int]
    name: str
    asset_class: str
    symbol: str = ""
    quantity: float = 0.0
    unit_price: float = 0.0
    value: float = 0.0
    cost_basis: float = 0.0
    fees: float = 0.0
    currency: str = "EUR"
    cost_basis_known: bool = True

    @property
    def gross_gain(self) -> Optional[float]:
        if not self.cost_basis_known:
            return None
        return max(0.0, self.value) - max(0.0, self.cost_basis)

    @property
    def net_gain(self) -> Optional[float]:
        if not self.cost_basis_known:
            return None
        return max(0.0, self.value) - (max(0.0, self.cost_basis) + max(0.0, self.fees))

    @property
    def return_pct(self) -> Optional[float]:
        if not self.cost_basis_known:
            return None
        cost = max(0.0, self.cost_basis) + max(0.0, self.fees)
        return (self.net_gain / cost * 100.0) if cost > 0 else None


@dataclass(slots=True)
class Goal:
    """Financial savings or emergency fund milestone."""

    id: Optional[int]
    name: str
    target_amount: float
    saved_amount: float = 0.0
    monthly_contribution: float = 0.0
    deadline: str = ""
    notes: str = ""
    goal_type: str = "standard"  # 'standard', 'emergency_fund'

    @property
    def progress_pct(self) -> float:
        return (
            (self.saved_amount / self.target_amount * 100.0)
            if self.target_amount > 0
            else 100.0
        )

    @property
    def remaining_amount(self) -> float:
        return max(0.0, self.target_amount - self.saved_amount)


@dataclass(slots=True)
class PlannedPurchase:
    """Planned future expense with priority ranking."""

    id: Optional[int]
    name: str
    target_amount: float
    saved_amount: float = 0.0
    desired_date: str = ""
    priority: str = "Média"  # 'Alta', 'Média', 'Baixa'
    notes: str = ""


@dataclass(slots=True)
class CategoryRule:
    """Automated categorization rule matching keywords."""

    id: Optional[int]
    keyword: str
    category: str
    category_id: str = ""
    split_json: str = ""


__all__ = [
    "Category",
    "CategoryRule",
    "FinancialAccount",
    "Goal",
    "Holding",
    "PlannedPurchase",
    "Subscription",
    "Transaction",
    "TransactionSplit",
]
