"""Dense rendering fixtures must retain distinct stable category identities."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from database.engine import FinanceDB
from database.repo import Repo
from scripts.validate_visual_rendering import _seed_stress_data


def test_dense_visual_fixture_keeps_every_category_in_real_analytics(tmp_path):
    database = FinanceDB(tmp_path / "synthetic-visual.sqlite3")
    repo = Repo(database)
    try:
        repo.account_identity("Conta principal", create=True)
        categories = _seed_stress_data(SimpleNamespace(repo=repo))
        assert len(categories) == 30
        identities = [repo.category_identity(name)[0] for name in categories]
        assert all(identities)
        assert len(set(identities)) == len(categories)
        grouped = repo.top_categories_payment_period(
            (date.today() - timedelta(days=364)).isoformat(), date.today().isoformat()
        )
        assert set(categories).issubset({row[0] for row in grouped})
        assert all(row[1] > 0 for row in grouped)
    finally:
        repo.close()
