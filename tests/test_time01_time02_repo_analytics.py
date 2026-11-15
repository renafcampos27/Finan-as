"""TIME-01 + TIME-02: month-boundary and split-category analytics regressions.

- TIME-01: transactions on the LAST day of a month must be included in
  monthly aggregates regardless of caller bound style (exclusive enforced
  once at the repo boundary in ``category_spend_with_splits``).
- TIME-02: split rows must be attributed to their OWN category
  (``COALESCE(s.category, t.category)``), never the parent's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo.main_repo import Repo


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
    database = FinanceDB(tmp_path / "time_fixes.sqlite3")
    r = Repo(database)
    try:
        for table in (
            "transaction_splits",
            "transactions",
            "financial_accounts",
        ):
            r.db.conn.execute(f"DELETE FROM {table}")
        r.db.conn.commit()
        yield r
    finally:
        database.close()


def test_time01_last_day_of_month_included_in_category_spend(repo: Repo) -> None:
    """A transaction ON the month's last day must appear (previously lost)."""

    tx_id = repo.add_transaction("2026-02-28", "Jantar fim de mês", -80.0, "Casa", "Conta principal")
    assert tx_id

    rows = repo.category_spend_with_splits("2026-02-01", "2026-02-28")

    totals = {str(r["category"]): float(r["total"]) for r in rows}
    assert totals.get("Casa") == pytest.approx(80.0), (
        f"last-day movement lost: {totals}"
    )


def test_time01_inclusive_bounds_and_full_year_grouping(repo: Repo) -> None:
    """Year grouping via <= keeps working (no double counting at 12-31)."""

    repo.add_transaction("2026-12-31", "Compra natalícia", -120.0, "Desejos", "Conta principal")

    by_month = repo.monthly_totals_grouped_for_year(2026)
    assert by_month["2026-12"]["expenses"] == pytest.approx(120.0)


def test_time02_split_counts_under_own_category(repo: Repo) -> None:
    """Parent 'Casa' with split 'Saude': grouping must show Saude, not Casa."""

    tx_id = repo.add_transaction("2026-03-10", "Farmácia e supermercado", -100.0, "Casa", "Conta principal")
    repo.save_transaction_splits(tx_id, [("Saúde", 60.0), ("Casa", 40.0)])

    rows = repo.category_spend_with_splits("2026-03-01", "2026-03-31")

    totals = {str(r["category"]): float(r["total"]) for r in rows}
    assert totals.get("Saúde") == pytest.approx(60.0)
    assert totals.get("Casa") == pytest.approx(40.0)


def test_time02_dynamic_grouped_by_category_uses_split_category(repo: Repo) -> None:
    """dynamic_grouped(group_by='Categoria') must honour split categories."""

    tx_id = repo.add_transaction("2026-03-15", "Recibo misto", -200.0, "Casa", "Conta principal")
    repo.save_transaction_splits(tx_id, [("Saúde", 75.0), ("Casa", 25.0)])
    # NOTE: category identity may remap by keyword rules (e.g. "Mercado" ->
    # "Supermercado"); read back what was persisted instead of assuming.
    plain_id = repo.add_transaction("2026-03-20", "Compra livre de regras", -90.0, "Livre", "Conta principal")
    assert plain_id
    persisted = str(repo.transaction_by_id(plain_id)["category"])

    rows = repo.dynamic_grouped("2026-03-01", "2026-03-31", group_by="Categoria", kind="Despesas")

    by_label = {str(r["label"]): float(r["total"]) for r in rows}
    assert by_label.get("Saúde") == pytest.approx(150.0), by_label
    assert by_label.get("Casa") == pytest.approx(50.0), by_label
    assert by_label.get(persisted) == pytest.approx(90.0), (by_label, persisted)


def test_time01_leap_year_last_day_counts(repo: Repo) -> None:
    """2024-02-29 (leap year) passed as month end must be counted."""

    repo.add_transaction("2024-02-29", "Despesa 29 fev", -30.0, "Saúde", "Conta principal")

    rows = repo.category_spend_with_splits("2024-02-01", "2024-02-29")

    totals = {str(r["category"]): float(r["total"]) for r in rows}
    assert totals.get("Saúde") == pytest.approx(30.0), totals


def test_time01_exclusive_mid_month_bound_is_not_promoted(repo: Repo) -> None:
    """A bound that is NOT the month's last day keeps half-open semantics:
    services pass ``through_date + 1 day`` as an exclusive cutoff, so the
    movement falling ON that day must stay excluded (no +1 day double count)."""

    repo.add_transaction("2026-02-15", "Dentro da janela", -10.0, "Casa", "Conta principal")
    repo.add_transaction("2026-02-16", "No dia de corte", -99.0, "Casa", "Conta principal")

    rows = repo.category_spend_with_splits("2026-02-01", "2026-02-16")

    totals = {str(r["category"]): float(r["total"]) for r in rows}
    assert totals == {"Casa": pytest.approx(10.0)}, totals


def test_time01_month_period_bounds_count_last_day(repo: Repo) -> None:
    """repo.month_period returns inclusive bounds; the last day counts and the
    first day of the next month never leaks in."""

    period_start, period_end, _month_key = repo.month_period(2026, 2)
    assert period_start == "2026-02-01"
    assert period_end == "2026-02-28"

    repo.add_transaction("2026-02-28", "Luz de fevereiro", -80.0, "Casa", "Conta principal")
    rows = repo.category_spend_with_splits(period_start, period_end)
    totals = {str(r["category"]): float(r["total"]) for r in rows}
    assert totals.get("Casa") == pytest.approx(80.0), totals

    repo.add_transaction("2026-03-01", "Água de março", -20.0, "Casa", "Conta principal")
    rows_after = repo.category_spend_with_splits(period_start, period_end)
    totals_after = {str(r["category"]): float(r["total"]) for r in rows_after}
    assert totals_after.get("Casa") == pytest.approx(80.0), totals_after


def test_time02_full_split_filters_by_parcel_category_not_parent(repo: Repo) -> None:
    """Parent 'Casa' fully split into 'Saúde': filtering 'Saúde' shows the
    split row and filtering 'Casa' shows nothing (COALESCE semantics)."""

    tx_id = repo.add_transaction("2026-03-10", "Mãe Casa, parcela Saúde", -100.0, "Casa", "Conta principal")
    repo.save_transaction_splits(tx_id, [("Saúde", 100.0)])

    rows = repo.category_spend_with_splits("2026-03-01", "2026-03-31")
    totals = {str(r["category"]): float(r["total"]) for r in rows}
    assert totals == {"Saúde": pytest.approx(100.0)}, totals

    saude = repo.chart_movement_details(
        "2026-03-01", "2026-03-31", category="Saúde", kind="Despesas"
    )
    assert len(saude) == 1
    assert float(saude[0]["amount"]) == pytest.approx(-100.0)
    assert str(saude[0]["effective_category"]) == "Saúde"
    assert int(saude[0]["split_contribution"]) == 1

    casa = repo.chart_movement_details(
        "2026-03-01", "2026-03-31", category="Casa", kind="Despesas"
    )
    assert casa == [], casa
