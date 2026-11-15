"""Read-only diagnostics for financial data quality and legacy demo records."""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from database.engine import FinanceDB
from database.validation import sql_finite_number, sql_iso_date
from utils.helpers import add_months, row_get, safe_float

if TYPE_CHECKING:
    from database.repo import Repo
    from services.backup import BackupService


_FINANCIAL_DATE = "COALESCE(NULLIF(m.payment_date,''),t.date)"


@dataclass(slots=True, frozen=True)
class DemoDataReview:
    """Conservative evidence used only to offer a manual review."""

    marker_present: bool
    complete_demo_profile: bool
    safe_to_offer_removal: bool
    transaction_candidates: int
    subscription_candidates: int
    holding_candidates: int
    goal_candidates: int
    extra_financial_records: int
    message: str


class DataHealthService:
    """Build fast diagnostics without modifying the user's financial records."""

    _DEMO_FIXED_TRANSACTIONS = {
        ("Salário", 2750.0, "Rendimentos", "income"): 5,
        ("Renda", -980.0, "Casa e contas", "expense"): 5,
        ("Eletricidade e gás", -82.0, "Casa e contas", "expense"): 5,
        ("Internet e telemóvel", -48.0, "Casa e contas", "expense"): 5,
        ("Passe / combustível", -95.0, "Transporte", "expense"): 5,
        ("Farmácia", -28.0, "Saúde", "expense"): 5,
        ("Jantar fora", -42.0, "Restaurantes", "expense"): 5,
        ("Cinema", -19.0, "Lazer", "expense"): 5,
    }
    _DEMO_SUBSCRIPTIONS = {
        ("Streaming TV", 12.99, 4),
        ("Ginásio", 33.0, 10),
        ("Armazenamento cloud", 2.99, 16),
        ("Música", 7.99, 21),
        ("Software criativo", 19.99, 27),
    }
    _DEMO_HOLDINGS = {
        ("Conta à ordem", 6200.87, 6200.87),
        ("ETF global", 19131.14, 16200.0),
        ("Ações individuais", 48512.84, 41000.0),
        ("Obrigações", 18354.60, 17500.0),
        ("Cripto", 10736.38, 8900.0),
    }

    def __init__(
        self,
        repo: "Repo",
        *,
        backup_service: "BackupService | None" = None,
    ) -> None:
        self.repo = repo
        self.backup_service = backup_service

    @staticmethod
    def _demo_variable_amounts() -> tuple[Counter[float], Counter[float]]:
        generator = random.Random(20260701)
        supermarket: Counter[float] = Counter()
        occasional: Counter[float] = Counter()
        for _month in range(5):
            for _index in range(8):
                generator.randint(2, 27)
                supermarket[-round(generator.uniform(19, 95), 2)] += 1
            for _index in range(2):
                generator.randint(5, 25)
                occasional[-round(generator.uniform(22, 75), 2)] += 1
        return supermarket, occasional

    @staticmethod
    def _status(score: float) -> tuple[str, str, str]:
        if score >= 90:
            return "positive", "Excelente", "✅"
        if score >= 72:
            return "positive", "Saudável", "🟢"
        if score >= 50:
            return "warning", "A rever", "🟠"
        return "negative", "Atenção necessária", "🔴"

    def monthly_quality_trend(
        self,
        months: int = 12,
        reference_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Score active months only; missing history is never invented as zero."""

        reference = reference_date or date.today()
        visible_months = max(3, min(36, int(months or 12)))
        start = add_months(reference.replace(day=1), -(visible_months - 1), 1)
        end = reference.isoformat()
        valid_amount = sql_finite_number("t.amount", non_zero=True)
        valid_date = sql_iso_date(_FINANCIAL_DATE)
        rows = self.repo.q(
            f"""
            SELECT SUBSTR({_FINANCIAL_DATE},1,7) AS month,
                   COUNT(*) AS total,
                   SUM(CASE WHEN TRIM(t.category)='' OR
                                      t.category IN ('Outros','Diversos')
                            THEN 1 ELSE 0 END) AS generic_count,
                   SUM(CASE WHEN LENGTH(TRIM(t.description))<2
                            THEN 1 ELSE 0 END) AS weak_description_count
            FROM transactions t
            LEFT JOIN transaction_meta m ON m.transaction_id=t.id
            WHERE {_FINANCIAL_DATE}>=? AND {_FINANCIAL_DATE}<=?
              AND {valid_amount} AND {valid_date}
            GROUP BY SUBSTR({_FINANCIAL_DATE},1,7)
            ORDER BY month
            """,
            (start.isoformat(), end),
        )
        duplicate_rows = self.repo.q(
            f"""
            SELECT month, SUM(c - 1) AS duplicate_count
            FROM (
                SELECT SUBSTR({_FINANCIAL_DATE},1,7) AS month,
                       LOWER(TRIM(t.account)) AS account_key,
                       {_FINANCIAL_DATE} AS financial_date,
                       LOWER(TRIM(t.description)) AS description_key,
                       ROUND(t.amount,2) AS amount_key,
                       COUNT(*) AS c
                FROM transactions t
                LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                WHERE {_FINANCIAL_DATE}>=? AND {_FINANCIAL_DATE}<=?
                  AND {valid_amount} AND {valid_date}
                GROUP BY month, account_key, financial_date,
                         description_key, amount_key
                HAVING COUNT(*)>1
            ) candidates
            GROUP BY month
            """,
            (start.isoformat(), end),
        )
        duplicates = {
            str(row["month"]): max(0, int(row["duplicate_count"] or 0))
            for row in duplicate_rows
        }
        result: list[dict[str, Any]] = []
        for source in rows:
            total = max(0, int(source["total"] or 0))
            if total <= 0:
                continue
            generic = max(0, int(source["generic_count"] or 0))
            weak = max(0, int(source["weak_description_count"] or 0))
            duplicate = duplicates.get(str(source["month"]), 0)
            generic_rate = generic / total
            duplicate_rate = duplicate / total
            weak_rate = weak / total
            score = max(
                0.0,
                min(
                    100.0,
                    100.0
                    - min(45.0, generic_rate * 100.0 * 0.45)
                    - min(35.0, duplicate_rate * 100.0 * 0.35)
                    - min(20.0, weak_rate * 100.0 * 0.20),
                ),
            )
            tone, status, marker = self._status(score)
            month_key = str(source["month"] or "")[:7]
            result.append(
                {
                    "month": month_key,
                    "label": month_key[5:7] + "/" + month_key[2:4],
                    "score": round(score, 1),
                    "total": total,
                    "generic": generic,
                    "duplicates": duplicate,
                    "weak_descriptions": weak,
                    "issues": generic + duplicate + weak,
                    "tone": tone,
                    "status": status,
                    "marker": marker,
                }
            )
        return result

    def demo_data_review(self) -> DemoDataReview:
        """Recognise the complete built-in demo suite; never delete anything."""

        marker_present = self.repo.setting("demo_data_seeded", "0") == "1"
        transaction_rows = self.repo.q(
            "SELECT description, amount, category, kind FROM transactions"
        )
        fixed_found = 0
        for signature, expected_count in self._DEMO_FIXED_TRANSACTIONS.items():
            description, amount, category, kind = signature
            matches = sum(
                1
                for row in transaction_rows
                if str(row_get(row, "description", "")) == description
                and round(safe_float(row_get(row, "amount", 0)), 2) == round(amount, 2)
                and str(row_get(row, "category", "")) == category
                and str(row_get(row, "kind", "")) == kind
            )
            fixed_found += min(expected_count, matches)
        supermarket_values = Counter(
            round(safe_float(row_get(row, "amount", 0)), 2)
            for row in transaction_rows
            if str(row_get(row, "description", "")) == "Supermercado"
            and str(row_get(row, "category", "")) == "Supermercado"
            and safe_float(row_get(row, "amount", 0)) < 0
        )
        occasional_values = Counter(
            round(safe_float(row_get(row, "amount", 0)), 2)
            for row in transaction_rows
            if str(row_get(row, "description", "")) == "Compra ocasional"
            and str(row_get(row, "category", "")) == "Diversos"
            and safe_float(row_get(row, "amount", 0)) < 0
        )
        expected_supermarket, expected_occasional = self._demo_variable_amounts()
        supermarket_matches = sum((supermarket_values & expected_supermarket).values())
        occasional_matches = sum((occasional_values & expected_occasional).values())
        transaction_candidates = fixed_found + supermarket_matches + occasional_matches

        subscription_rows = self.repo.q(
            "SELECT name, amount, renewal_day FROM subscriptions"
        )
        subscription_candidates = sum(
            1
            for row in subscription_rows
            if (
                str(row_get(row, "name", "")),
                round(safe_float(row_get(row, "amount", 0)), 2),
                int(row_get(row, "renewal_day", 0) or 0),
            )
            in self._DEMO_SUBSCRIPTIONS
        )
        holding_rows = self.repo.q("SELECT name, value, cost_basis FROM holdings")
        holding_candidates = sum(
            1
            for row in holding_rows
            if (
                str(row_get(row, "name", "")),
                round(safe_float(row_get(row, "value", 0)), 2),
                round(safe_float(row_get(row, "cost_basis", 0)), 2),
            )
            in self._DEMO_HOLDINGS
        )
        goal_rows = self.repo.q(
            "SELECT name, target_amount, saved_amount, monthly_contribution FROM goals"
        )
        goal_candidates = sum(
            1
            for row in goal_rows
            if str(row_get(row, "name", "")) == "Comprar casa"
            and round(safe_float(row_get(row, "target_amount", 0)), 2) == 160000.0
            and round(safe_float(row_get(row, "saved_amount", 0)), 2) == 42000.0
            and round(safe_float(row_get(row, "monthly_contribution", 0)), 2) == 900.0
        )
        expected_total = 90 + 5 + 5 + 1
        actual_total = (
            len(transaction_rows)
            + len(subscription_rows)
            + len(holding_rows)
            + len(goal_rows)
        )
        complete = (
            transaction_candidates == 90
            and subscription_candidates == 5
            and holding_candidates == 5
            and goal_candidates == 1
        )
        extra = max(0, actual_total - expected_total)
        safe = (
            marker_present
            and complete
            and extra == 0
            and actual_total == expected_total
        )
        if safe:
            message = (
                "Foi identificado o perfil de demonstração completo e sem registos "
                "financeiros adicionais. A remoção continua a exigir backup e confirmação."
            )
        elif marker_present or complete:
            message = (
                "Existem indícios de dados de demonstração misturados com outros dados. "
                "A aplicação não permite remoção automática deste perfil."
            )
        else:
            message = "Não foi identificado um perfil completo de demonstração."
        return DemoDataReview(
            marker_present=marker_present,
            complete_demo_profile=complete,
            safe_to_offer_removal=safe,
            transaction_candidates=transaction_candidates,
            subscription_candidates=subscription_candidates,
            holding_candidates=holding_candidates,
            goal_candidates=goal_candidates,
            extra_financial_records=extra,
            message=message,
        )

    def snapshot(
        self,
        *,
        reference_date: date | None = None,
        months: int = 12,
    ) -> dict[str, Any]:
        reference = reference_date or date.today()
        total_transactions = max(
            0,
            int(self.repo.scalar("SELECT COUNT(*) FROM transactions", default=0)),
        )
        quality = self.repo.transaction_data_quality_counts(
            "1900-01-01",
            reference.isoformat(),
        )
        trend = self.monthly_quality_trend(months, reference)
        latest_score = safe_float(trend[-1].get("score")) if trend else 0.0
        invalid = (
            int(quality.get("invalid_dates", 0))
            + int(quality.get("invalid_amounts", 0))
            + int(quality.get("category_inconsistencies", 0))
        )
        generic = int(quality.get("uncategorized_or_generic", 0))
        duplicate = sum(int(row.get("duplicates", 0) or 0) for row in trend)
        attachment_rows = self.repo.q(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN TRIM(content_sha256)='' OR file_size<=0
                            THEN 1 ELSE 0 END) AS unverified
            FROM transaction_attachments
            """
        )
        attachment = attachment_rows[0] if attachment_rows else {}
        attachment_total = int(row_get(attachment, "total", 0) or 0)
        attachment_unverified = int(row_get(attachment, "unverified", 0) or 0)
        verified_at = self.repo.setting("data_guard_last_verified_at", "")
        stored_schema = int(
            safe_float(
                self.repo.setting(
                    "data_guard_schema_version",
                    str(self.repo.db.schema_version()),
                )
            )
        )
        database_verified = (
            bool(verified_at) and stored_schema == FinanceDB.CURRENT_SCHEMA_VERSION
        )
        quality_score = (
            latest_score if trend else (100.0 if total_transactions == 0 else 0.0)
        )
        integrity_score = (
            100.0
            if database_verified and invalid == 0
            else 55.0
            if invalid == 0
            else 20.0
        )
        document_score = (
            100.0
            if attachment_total == 0 or attachment_unverified == 0
            else max(
                0.0,
                100.0 * (attachment_total - attachment_unverified) / attachment_total,
            )
        )
        overall = round(
            quality_score * 0.55 + integrity_score * 0.30 + document_score * 0.15
        )
        tone, status, marker = self._status(overall)
        protection = (
            self.backup_service.protection_health_snapshot()
            if self.backup_service is not None
            else {}
        )
        review = self.demo_data_review()
        return {
            "score": overall,
            "tone": tone,
            "status": status,
            "marker": marker,
            "transactions": total_transactions,
            "invalid": invalid,
            "generic": generic,
            "category_inconsistencies": int(quality.get("category_inconsistencies", 0)),
            "duplicate_candidates": duplicate,
            "attachments": attachment_total,
            "attachments_unverified": attachment_unverified,
            "database_verified": database_verified,
            "database_verified_at": verified_at,
            "schema_version": stored_schema,
            "supported_schema_version": FinanceDB.CURRENT_SCHEMA_VERSION,
            "trend": trend,
            "protection": protection,
            "demo_review": asdict(review),
        }


__all__ = ["DataHealthService", "DemoDataReview"]
