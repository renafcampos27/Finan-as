from __future__ import annotations

from pathlib import Path
from database.engine import FinanceDB
from database.repo import Repo
from services.change_journal import ChangeJournalService
from services.financial_health import FinancialHealthService
from services.payments import PaymentsService


def test_financial_health_score_memoization_and_mathematical_consistency(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "test_score.sqlite3"
    database = FinanceDB(db_file)
    try:
        repo = Repo(database)
        assert repo.paths.database == db_file
        repo.db.seed_if_empty()
        journal = ChangeJournalService(repo)
        payments = PaymentsService(repo, journal)
        service = FinancialHealthService(repo, payments)

        # First computation (populates caches)
        result_1 = service.financial_score("2026-08")
        assert "score" in result_1
        assert "trend" in result_1
        assert "dimensions" in result_1

        # Second computation (should hit caches instantly)
        result_2 = service.financial_score("2026-08")
        assert result_1["score"] == result_2["score"]
        assert result_1["grade"] == result_2["grade"]
        assert result_1["confidence"] == result_2["confidence"]
        assert len(result_1["trend"]) == len(result_2["trend"])

        # Verify cache dictionaries are populated
        assert len(service._snapshot_cache) > 0
        assert len(service._totals_cache) > 0
        assert len(service._quality_cache) > 0
    finally:
        database.close()
