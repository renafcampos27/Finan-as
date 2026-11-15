#!/usr/bin/env python3
import sys
import sqlite3
import pytest
from datetime import date, timedelta
from pathlib import Path

# Configurar path para permitir imports corretos
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.repo import Repo  # noqa: E402
from services.analytics import AnalyticsService  # noqa: E402
from services.payments import PaymentsService  # noqa: E402
from utils.config import parse_date  # noqa: E402
from utils.helpers import add_months, safe_float, strict_float  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # Keep schema/seed coverage while guaranteeing that tests never open the
    # user's real finance database.
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "analytics.sqlite3"))
    r = Repo()
    yield r
    r.db.conn.close()


@pytest.fixture
def analytics(repo):
    return AnalyticsService(repo)


def test_schema_migrations_are_versioned_and_idempotent(repo):
    migrations = repo.q("SELECT version, name FROM schema_migrations ORDER BY version")

    assert [row["version"] for row in migrations] == list(
        range(1, repo.db.CURRENT_SCHEMA_VERSION + 1)
    )
    assert [row["name"] for row in migrations] == [
        "baseline_schema",
        "transaction_subcategories",
        "liabilities",
        "document_storage",
        "analytics_schema",
        "monthly_category_budgets",
        "subscription_recurrence",
        "financial_state_history",
        "category_budget_buckets",
        "high_volume_query_indexes",
        "habit_change_plans",
        "change_journal",
        "structured_goal_types",
        "holding_fees",
        "scheduled_occurrence_foreign_keys",
        "database_value_constraints",
        "attachment_integrity_metadata",
        "import_traceability_and_subscription_aliases",
        "subscription_payment_routing",
        "financial_integrity_links",
        "canonical_account_identity",
        "literal_transaction_search",
        "legacy_transfer_startup_compatibility",
        "stable_financial_entity_identity",
        "remove_obsolete_transactions_fts",
        "goal_assets_and_portable_attachments",
        "holding_currency_and_exchange_rate",
        "transactions_cascade_transfer_leg_trigger",
        "holding_acquisition_metadata",
        "monthly_category_budgets_update_cascade",
        "subscription_price_reviews",
        "reimbursement_evidence",
    ]
    assert repo.db.schema_version() == repo.db.CURRENT_SCHEMA_VERSION == 32
    holding_columns = {
        str(row["name"]) for row in repo.q("PRAGMA table_info(holdings)")
    }
    assert "fees" in holding_columns
    assert repo.scalar("SELECT COUNT(*) FROM holdings WHERE fees!=0") == 0
    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='liabilities'"
        )
        == 1
    )
    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='change_journal'"
        )
        == 1
    )
    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='monthly_category_budgets'"
        )
        == 1
    )
    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='subscription_occurrences'"
        )
        == 1
    )
    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='financial_state_snapshots'"
        )
        == 1
    )
    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='habit_change_plans'"
        )
        == 1
    )
    goal_columns = {str(row["name"]) for row in repo.q("PRAGMA table_info(goals)")}
    assert "goal_type" in goal_columns

    repo.db._ensure_migrations()

    assert repo.scalar("SELECT COUNT(*) FROM schema_migrations") == 32


def test_only_one_goal_is_explicitly_marked_as_emergency_reserve(repo):
    seeded_house = next(row for row in repo.goals() if row["name"] == "Comprar casa")
    assert seeded_house["goal_type"] == "standard"

    first_id = repo.add_goal(
        "Reserva principal",
        6_000,
        1_500,
        200,
        "",
        "",
        "emergency_fund",
    )
    second_id = repo.add_goal(
        "Reserva nova",
        8_000,
        2_000,
        250,
        "",
        "",
        "emergency_fund",
    )

    assert repo.goal_by_id(first_id)["goal_type"] == "standard"
    assert repo.goal_by_id(second_id)["goal_type"] == "emergency_fund"


def test_goal_type_migration_uses_exact_names_and_never_notes(repo):
    repo.db.conn.execute("DELETE FROM goals")
    repo.db.conn.executemany(
        """
        INSERT INTO goals(
            name,target_amount,saved_amount,monthly_contribution,
            deadline,notes,goal_type
        )
        VALUES(?,?,?,?,?,?,?)
        """,
        [
            (
                "Fundo de emergência",
                6_000,
                1_000,
                200,
                "",
                "",
                "standard",
            ),
            (
                "Reserva de emergência",
                8_000,
                4_000,
                250,
                "",
                "",
                "standard",
            ),
            (
                "Comprar casa",
                160_000,
                42_000,
                900,
                "",
                "Inclui um fundo de emergência",
                "standard",
            ),
        ],
    )
    repo.db.conn.commit()

    repo.db._migration_structured_goal_types()
    rows = {str(row["name"]): str(row["goal_type"]) for row in repo.goals()}

    assert list(rows.values()).count("emergency_fund") == 1
    assert rows["Reserva de emergência"] == "emergency_fund"
    assert rows["Comprar casa"] == "standard"


def test_spending_diagnostics_cover_forecasts_seasonality_and_pareto(
    repo,
    analytics,
):
    repo.db.conn.execute("DELETE FROM transaction_attachments")
    repo.db.conn.execute("DELETE FROM transaction_splits")
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()

    for index in range(18):
        month = add_months(date(2025, 1, 1), index)
        for merchant, amount, day in (
            ("Supermercado Central", 180 + index * 4, 5),
            ("Café Bairro", 35 + index, 12),
            ("Transportes", 60 + index * 2, 19),
        ):
            movement_day = month.replace(day=day).isoformat()
            repo.add_transaction_with_meta(
                movement_day,
                merchant,
                -float(amount),
                "Outros",
                "Conta principal",
                merchant=merchant,
                payment_date=movement_day,
                purchase_date=movement_day,
            )

    accuracy = analytics.forecast_accuracy(
        reference_date=date(2026, 7, 25),
        visible_months=12,
    )
    assert accuracy["sample_months"] == 12
    assert 0 <= accuracy["accuracy_pct"] <= 100
    assert all(
        {"actual", "predicted", "low", "high", "accuracy_pct"} <= set(row)
        for row in accuracy["rows"]
    )

    seasonality = analytics.spending_seasonality(
        "2026-01-01",
        "2026-06-30",
    )
    assert len(seasonality["cells"]) == 84
    assert seasonality["maximum"] > 0
    assert seasonality["peak"]["movements"] > 0

    pareto = analytics.merchant_pareto(
        "2026-01-01",
        "2026-06-30",
        limit=4,
    )
    assert pareto["merchants_count"] == 3
    assert pareto["rows"][-1]["cumulative_pct"] == pytest.approx(100)
    assert pareto["rows"][0]["merchant"] == "Supermercado Central"


def test_category_budgets_are_isolated_by_month(repo, analytics):
    repo.db.conn.execute("UPDATE categories SET budget=0")
    repo.db.conn.execute("DELETE FROM monthly_category_budgets")
    repo.db.conn.commit()
    repo.set_category_budget("Supermercado", 300)
    repo.set_category_budget("Supermercado", 450, "2026-07")
    repo.set_category_budget("Supermercado", 200, "2026-06")

    assert repo.category_budget("Supermercado") == pytest.approx(300)
    assert repo.category_budget("Supermercado", "2026-06") == pytest.approx(200)
    assert repo.category_budget("Supermercado", "2026-07") == pytest.approx(450)
    assert repo.category_budget("Supermercado", "2026-08") == pytest.approx(300)

    july = analytics.monthly_budget_forecast("2026-07", date(2026, 7, 10))
    june = analytics.monthly_budget_forecast("2026-06", date(2026, 6, 30))
    assert july["total_budget"] == pytest.approx(450)
    assert june["total_budget"] == pytest.approx(200)


def test_split_categories_are_consistent_across_charts_and_budgets(repo, analytics):
    repo.db.conn.execute("DELETE FROM transaction_splits")
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("UPDATE categories SET budget=0")
    repo.db.conn.commit()
    repo.set_category_budget("Lazer", 80, "2026-07")
    repo.set_category_budget("Diversos", 80, "2026-07")
    tx_id = repo.add_transaction(
        "2026-07-05", "Compra repartida", -100, "Supermercado", "Conta"
    )
    repo.save_transaction_splits(tx_id, [("Lazer", 50), ("Diversos", 50)])

    chart_rows = {
        category: value
        for category, value, _colour in analytics.category_totals(
            "2026-07-01", "2026-07-31", 20
        )
    }
    budget_rows = {
        row["category"]: row["spent"]
        for row in analytics.monthly_category_budget_rows("2026-07")
    }
    smart_rows = {
        row["category"]: row["spent"]
        for row in analytics.smart_budget_analysis("2026-07", date(2026, 7, 31))["rows"]
        if row["spent"] > 0
    }
    dynamic_rows = {
        row["label"]: row["total"]
        for row in repo.dynamic_grouped(
            "2026-07-01", "2026-07-31", "Categoria", "Pagamento", "Despesas"
        )
    }

    expected = {"Lazer": pytest.approx(50), "Diversos": pytest.approx(50)}
    assert chart_rows == expected
    assert budget_rows == expected
    assert smart_rows == expected
    assert dynamic_rows == expected
    assert "Supermercado" not in chart_rows


def test_editing_split_movement_recalculates_split_amounts(repo):
    repo.db.conn.execute("DELETE FROM transaction_splits")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    tx_id = repo.add_transaction(
        "2026-07-05", "Compra repartida", -100, "Diversos", "Conta"
    )
    repo.save_transaction_splits(tx_id, [("Casa", 25), ("Lazer", 75)])

    repo.update_transaction(tx_id, amount=-240)

    splits = repo.transaction_splits(tx_id)
    assert [row["amount"] for row in splits] == pytest.approx([60, 180])
    assert sum(row["amount"] for row in splits) == pytest.approx(240)


def test_internal_transfer_rolls_back_both_legs_on_failure(repo):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute(
        """
        CREATE TRIGGER reject_transfer_credit
        BEFORE INSERT ON transactions
        WHEN NEW.category='Transferência Interna' AND NEW.amount>0
        BEGIN
            SELECT RAISE(ABORT, 'simulated transfer failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.DatabaseError):
        repo.add_internal_transfer("2026-07-16", 125, "Conta A", "Conta B")

    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM transactions WHERE category='Transferência Interna'"
        )
        == 0
    )
    repo.db.conn.execute("DROP TRIGGER reject_transfer_credit")
    repo.db.conn.commit()

    debit_id, credit_id = repo.add_internal_transfer(
        "2026-07-16", 125, "Conta A", "Conta B"
    )
    rows = repo.q(
        "SELECT amount, account, category, category_id FROM transactions WHERE id IN (?,?) ORDER BY amount",
        (debit_id, credit_id),
    )
    assert [row["amount"] for row in rows] == pytest.approx([-125, 125])
    assert {row["account"] for row in rows} == {"Conta A", "Conta B"}
    assert {row["category"] for row in rows} == {"Transferência Interna"}
    assert len({row["category_id"] for row in rows}) == 1
    totals = AnalyticsService(repo).totals_for_period("2026-07-01", "2026-07-31")
    assert totals["income"] == pytest.approx(0)
    assert totals["expenses"] == pytest.approx(0)


def test_503020_uses_real_split_aware_category_classification(repo, analytics):
    repo.db.conn.execute("DELETE FROM transaction_splits")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    repo.set_category_budget_bucket("Supermercado", "Necessidades")
    repo.set_category_budget_bucket("Lazer", "Desejos")
    repo.set_category_budget_bucket("Investimentos", "Poupança")
    repo.add_transaction("2026-07-01", "Salário", 2_000, "Rendimentos", "Conta")
    split_id = repo.add_transaction(
        "2026-07-05", "Compra mista", -800, "Diversos", "Conta"
    )
    repo.save_transaction_splits(
        split_id,
        [("Supermercado", 75), ("Lazer", 25)],
    )
    repo.add_transaction("2026-07-08", "Reforço ETF", -300, "Investimentos", "Conta")

    result = analytics.budget_rule_503020("2026-07-01", "2026-07-31")
    rows = {row["bucket"]: row for row in result["rows"]}

    assert rows["Necessidades"]["actual"] == pytest.approx(600)
    assert rows["Desejos"]["actual"] == pytest.approx(200)
    assert rows["Poupança"]["actual"] == pytest.approx(1_200)
    assert rows["Poupança"]["actual_pct"] == pytest.approx(60)


def test_smart_budget_rollover_uses_previous_month_budget(repo, analytics):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("UPDATE categories SET budget=0")
    repo.db.conn.execute("DELETE FROM monthly_category_budgets")
    repo.db.conn.commit()
    repo.set_category_budget("Supermercado", 500, "2026-07")
    repo.set_category_budget("Supermercado", 250, "2026-06")
    repo.add_transaction("2026-06-10", "Compras junho", -100, "Supermercado", "Conta")

    row = next(
        row
        for row in analytics.smart_budget_analysis("2026-07", date(2026, 7, 10))["rows"]
        if row["category"] == "Supermercado"
    )

    assert row["previous_budget"] == pytest.approx(250)
    assert row["rollover"] == pytest.approx(150)
    assert row["effective_budget"] == pytest.approx(650)


def test_subscription_recurrence_respects_period_and_short_months(repo):
    service = PaymentsService(repo)
    quarterly = {
        "period": "Trimestral",
        "renewal_day": 31,
        "next_renewal_date": "2026-01-31",
        "last_used_date": "",
    }

    assert service.next_subscription_due_date(quarterly, date(2026, 2, 1)) == date(
        2026, 4, 30
    )
    assert service.advance_subscription_due(
        {"period": "Mensal", "renewal_day": 31}, date(2027, 1, 31)
    ) == date(2027, 2, 28)


def test_monthly_goal_progress_masks_current_value_in_privacy_mode(repo, analytics):
    _progress, current, _status = analytics.monthly_goal_progress(
        {
            "month": "2026-07",
            "goal_type": "Poupar pelo menos",
            "category": "",
            "target_value": 500,
        },
        privacy=True,
    )

    assert current == "••••• €"


def test_due_subscription_creates_one_linked_occurrence(repo):
    repo.db.conn.execute("DELETE FROM subscription_occurrences")
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    today = date.today()
    sid = repo.add_subscription(
        "Seguro anual",
        120,
        "Casa e contas",
        today.day,
        "Anual",
        next_renewal_date=today.isoformat(),
    )

    assert repo.process_due_subscriptions() == 1
    assert repo.process_due_subscriptions() == 0
    subscription = repo.subscription_by_id(sid)
    assert subscription["last_used_date"] == today.isoformat()
    assert (
        subscription["next_renewal_date"]
        == add_months(today, 12, today.day).isoformat()
    )
    assert repo.subscription_occurrence_cost(sid) == pytest.approx(120)
    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM subscription_occurrences WHERE subscription_id=?",
            (sid,),
        )
        == 1
    )


def test_subscription_charge_rolls_back_movement_when_occurrence_fails(repo):
    repo.db.conn.execute("DELETE FROM subscription_occurrences")
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    today = date.today()
    repo.add_subscription(
        "Seguro protegido",
        80,
        "Casa e contas",
        today.day,
        "Mensal",
        next_renewal_date=today.isoformat(),
    )
    repo.db.conn.execute(
        """
        CREATE TRIGGER reject_subscription_occurrence
        BEFORE INSERT ON subscription_occurrences
        BEGIN
            SELECT RAISE(ABORT, 'simulated occurrence failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.DatabaseError):
        repo.process_due_subscriptions()

    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM transactions WHERE description LIKE 'Seguro protegido%'"
        )
        == 0
    )
    assert repo.scalar("SELECT COUNT(*) FROM subscription_occurrences") == 0
    repo.db.conn.execute("DROP TRIGGER reject_subscription_occurrence")
    repo.db.conn.commit()
    assert repo.process_due_subscriptions() == 1


def test_manual_subscription_payment_is_one_atomic_operation(repo):
    repo.db.conn.execute("DELETE FROM subscription_occurrences")
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    payment_date = "2026-07-29"
    next_date = "2026-08-29"
    subscription_id = repo.add_subscription(
        "Serviço protegido",
        21.5,
        "Subscrições",
        29,
        "Mensal",
        next_renewal_date=payment_date,
    )
    repo.db.conn.execute(
        f"""
        CREATE TRIGGER reject_manual_subscription_advance
        BEFORE UPDATE ON subscriptions
        WHEN OLD.id={subscription_id}
        BEGIN
            SELECT RAISE(ABORT, 'simulated advance failure');
        END
        """
    )
    repo.db.conn.commit()

    with pytest.raises(sqlite3.DatabaseError, match="advance failure"):
        repo.record_subscription_payment(
            subscription_id,
            payment_date,
            next_date,
        )

    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM transactions "
            "WHERE description LIKE 'Serviço protegido%'"
        )
        == 0
    )
    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM subscription_occurrences WHERE subscription_id=?",
            (subscription_id,),
        )
        == 0
    )
    subscription = repo.subscription_by_id(subscription_id)
    assert subscription["last_used_date"] == ""
    assert subscription["next_renewal_date"] == payment_date

    repo.db.conn.execute("DROP TRIGGER reject_manual_subscription_advance")
    repo.db.conn.commit()
    transaction_id = repo.record_subscription_payment(
        subscription_id,
        payment_date,
        next_date,
    )
    assert transaction_id is not None
    assert (
        repo.record_subscription_payment(
            subscription_id,
            payment_date,
            next_date,
        )
        is None
    )
    subscription = repo.subscription_by_id(subscription_id)
    assert subscription["last_used_date"] == payment_date
    assert subscription["next_renewal_date"] == next_date
    assert repo.subscription_occurrence_cost(subscription_id) == pytest.approx(21.5)


def test_scheduled_charge_rolls_back_movement_when_occurrence_fails(repo):
    repo.db.conn.execute("DELETE FROM scheduled_payment_occurrences")
    repo.db.conn.execute("DELETE FROM scheduled_payments")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    # Monday keeps failure injection independent of weekend debit deferral.
    today = date(2026, 7, 13)
    repo.add_scheduled_payment(
        "Prestação protegida",
        "Casa e contas",
        "Crédito",
        220,
        "Conta principal",
        "Débito direto",
        today.isoformat(),
        today.day,
        1,
    )
    repo.db.conn.execute(
        """
        CREATE TRIGGER reject_scheduled_occurrence
        BEFORE INSERT ON scheduled_payment_occurrences
        BEGIN
            SELECT RAISE(ABORT, 'simulated scheduled failure');
        END
        """
    )
    repo.db.conn.commit()
    service = PaymentsService(repo)

    with pytest.raises(sqlite3.DatabaseError):
        service.generate_due_scheduled_payments(today)

    assert (
        repo.scalar(
            "SELECT COUNT(*) FROM transactions WHERE description LIKE 'Prestação protegida%'"
        )
        == 0
    )
    assert repo.scalar("SELECT COUNT(*) FROM scheduled_payment_occurrences") == 0
    repo.db.conn.execute("DROP TRIGGER reject_scheduled_occurrence")
    repo.db.conn.commit()
    assert service.generate_due_scheduled_payments(today) == 1


def test_historical_investments_do_not_use_current_portfolio(repo, analytics):
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM investment_snapshots")
    repo.db.conn.execute("DELETE FROM financial_state_snapshots")
    repo.db.conn.commit()
    holding_id = repo.add_holding("ETF", "ETF", "Conta", 1_200, 1_000)
    repo.record_investment_snapshot("2025-12-31")
    repo.record_financial_state_snapshot("2025-12-31")
    repo.update_holding(holding_id, "ETF", "ETF", "Conta", 9_000, 1_000)

    historical = analytics.totals_for_period("2025-12-01", "2025-12-31")
    current = analytics.totals_for_period(
        date.today().replace(day=1).isoformat(), date.today().isoformat()
    )

    assert historical["investments"] == pytest.approx(1_200)
    assert historical["investment_cost"] == pytest.approx(1_000)
    assert historical["investment_as_of"] == "2025-12-31"
    assert current["investments"] == pytest.approx(9_000)


def test_historical_net_worth_uses_state_snapshot_not_current_values(repo, analytics):
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM liabilities")
    repo.db.conn.execute("DELETE FROM goals")
    repo.db.conn.execute("DELETE FROM investment_snapshots")
    repo.db.conn.execute("DELETE FROM financial_state_snapshots")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    repo.add_transaction("2025-12-01", "Saldo", 5_000, "Rendimentos", "Conta")
    holding_id = repo.add_holding("ETF", "ETF", "Conta", 1_200, 1_000)
    liability_id = repo.add_liability("Crédito", "Crédito", "Banco", 500, 5, 50)
    repo.record_investment_snapshot("2025-12-31")
    repo.record_financial_state_snapshot("2025-12-31")
    repo.update_holding(holding_id, "ETF", "ETF", "Conta", 9_000, 1_000)
    repo.update_liability(liability_id, "Crédito", "Crédito", "Banco", 4_000, 5, 400)

    snapshot = analytics.net_worth_snapshot(date(2025, 12, 31))

    assert snapshot["investment_assets"] == pytest.approx(1_200)
    assert snapshot["total_liabilities"] == pytest.approx(500)
    assert snapshot["monthly_debt_payment"] == pytest.approx(50)
    assert snapshot["net_worth"] == pytest.approx(5_700)
    assert snapshot["state_source"] == "daily_snapshot"


def test_historical_score_is_not_rewritten_by_current_state(repo, analytics):
    repo.db.conn.execute("DELETE FROM financial_state_snapshots")
    repo.db.conn.execute("DELETE FROM investment_snapshots")
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.execute("DELETE FROM liabilities")
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM goals")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    for month in range(7, 13):
        repo.add_transaction(
            f"2025-{month:02d}-01",
            "Salário",
            2_000,
            "Rendimentos",
            "Conta",
        )
        repo.add_transaction(
            f"2025-{month:02d}-10",
            "Despesas",
            -1_200,
            "Casa e contas",
            "Conta",
        )

    before = analytics.detailed_financial_score("2025-12")
    repo.add_holding("Carteira atual", "Cripto", "Conta", 50_000, 10_000)
    repo.add_liability("Dívida atual", "Cartão", "Banco", 30_000, 20, 1_500)
    repo.add_subscription(
        "Subscrição atual",
        600,
        "Subscrições",
        15,
        "Mensal",
    )
    after = analytics.detailed_financial_score("2025-12")

    assert after["score"] == before["score"]
    assert after["state_source"] == "unavailable"
    assert after["historical_state_confidence"] == pytest.approx(35)


def test_financial_periods_use_payment_date_and_clip_monthly_buckets(repo, analytics):
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    moved_id = repo.add_transaction(
        "2026-06-30", "Compra paga em julho", -120, "Lazer", "Conta"
    )
    repo.set_transaction_meta_ext(
        moved_id,
        purchase_date="2026-06-30",
        payment_date="2026-07-02",
    )
    excluded_id = repo.add_transaction(
        "2026-07-01", "Fora da janela", -80, "Lazer", "Conta"
    )
    repo.set_transaction_meta_ext(excluded_id, payment_date="2026-07-01")
    included_id = repo.add_transaction(
        "2026-07-10", "Dentro da janela", 500, "Rendimentos", "Conta"
    )
    repo.set_transaction_meta_ext(included_id, payment_date="2026-07-10")

    june = analytics.totals_for_period("2026-06-01", "2026-06-30")
    july = analytics.totals_for_period("2026-07-01", "2026-07-31")
    clipped = analytics.monthly_summary_for_period("2026-07-02", "2026-07-10")

    assert june["expenses"] == 0
    assert july["expenses"] == pytest.approx(200)
    assert july["income"] == pytest.approx(500)
    assert analytics.category_totals("2026-07-02", "2026-07-10", 10)[0][
        1
    ] == pytest.approx(120)
    assert len(clipped) == 1
    assert clipped[0]["period_start"] == "2026-07-02"
    assert clipped[0]["period_end"] == "2026-07-10"
    assert clipped[0]["expenses"] == pytest.approx(120)
    assert clipped[0]["income"] == pytest.approx(500)
    assert clipped[0]["partial"] is True


def test_cashflow_series_groups_real_movements_without_internal_transfers(
    repo,
    analytics,
):
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    repo.add_transaction("2026-07-01", "Salário", 1_500, "Rendimentos", "Conta")
    repo.add_transaction("2026-07-02", "Supermercado", -120, "Alimentação", "Conta")
    repo.add_transaction("2026-07-09", "Renda", -600, "Casa e contas", "Conta")
    repo.add_transaction(
        "2026-07-03",
        "Transferência entre contas",
        -300,
        "Transferência Interna",
        "Conta",
    )

    daily = analytics.cashflow_series("2026-07-01", "2026-07-09", "daily")
    weekly = analytics.cashflow_series("2026-07-01", "2026-07-09", "weekly")
    monthly = analytics.cashflow_series("2026-07-01", "2026-07-09", "monthly")

    assert len(daily) == 9
    assert sum(row["income"] for row in daily) == pytest.approx(1_500)
    assert sum(row["expenses"] for row in daily) == pytest.approx(720)
    assert daily[2]["income"] == daily[2]["expenses"] == 0
    assert len(weekly) == 2
    assert weekly[0]["income"] == pytest.approx(1_500)
    assert weekly[0]["expenses"] == pytest.approx(120)
    assert weekly[1]["expenses"] == pytest.approx(600)
    assert len(monthly) == 1
    assert monthly[0]["net"] == pytest.approx(780)
    assert analytics.cashflow_series("data inválida", "2026-07-09") == []


def test_filtered_pulse_series_keeps_empty_prefix_and_every_month(
    repo,
    analytics,
):
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    repo.add_transaction(
        "2026-03-15",
        "Primeiro movimento",
        1_000,
        "Rendimentos",
        "Conta",
    )
    repo.add_transaction(
        "2026-07-03",
        "Movimento recente",
        -200,
        "Casa e contas",
        "Conta",
    )

    daily = analytics.cashflow_series("2026-07-01", "2026-07-03", "daily")
    monthly = analytics.cashflow_series("2026-01-01", "2026-07-31", "monthly")
    full_history = analytics.cashflow_series(
        "1900-01-01",
        "2026-07-31",
        "monthly",
    )
    full_savings = analytics.monthly_summary_for_period(
        "1900-01-01",
        "2026-07-31",
    )

    assert [row["date"] for row in daily] == [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
    ]
    assert daily[0]["income"] == daily[0]["expenses"] == 0
    assert len(monthly) == 7
    assert monthly[0]["date"] == "2026-01-01"
    assert monthly[1]["net"] == 0
    assert monthly[-1]["expenses"] == pytest.approx(200)
    assert len(full_history) == 5
    assert full_history[0]["date"] == "2026-03-01"
    assert full_history[1]["net"] == 0
    assert [row["month"] for row in full_savings] == [
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
    ]
    assert full_savings[1]["movement_count"] == 0
    assert analytics.monthly_summary_for_period("2025-01-01", "2025-12-31") == []


def test_dashboard_annual_pulse_cards_use_the_exact_selected_window(
    repo,
    analytics,
):
    from ui.dashboard_worker import DashboardWorker

    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    repo.add_transaction(
        "2025-12-01",
        "Receita dentro do ano",
        2_000,
        "Rendimentos",
        "Conta",
    )
    repo.add_transaction(
        "2026-07-01",
        "Despesa dentro do ano",
        -500,
        "Casa e contas",
        "Conta",
    )
    worker = DashboardWorker(
        1,
        repo,
        analytics,
        "2025-07-29",
        "2026-07-28",
        365,
        "monthly",
        "none",
        enabled_modules={"cashflow_pulse", "savings_efficiency"},
    )

    data = worker._load_data(repo, analytics)

    assert data is not None
    assert len(data["cashflow_pulse"]) == 13
    assert data["cashflow_pulse"][0]["date"] == "2025-07-01"
    assert data["cashflow_pulse"][0]["income"] == 0
    assert len(data["savings_efficiency"]) == 13
    assert data["savings_efficiency"][0]["period_start"] == "2025-07-29"
    assert data["savings_efficiency"][-1]["period_end"] == "2026-07-28"
    assert len(data["metric_trends"]["income"]) == 13


def test_dashboard_all_period_loads_balance_forecast(repo, analytics):
    from ui.dashboard_worker import DashboardWorker

    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    today = date.today()
    repo.add_transaction(
        (today - timedelta(days=45)).isoformat(),
        "Saldo inicial observado",
        2_000,
        "Rendimentos",
        "Conta",
    )
    repo.add_transaction(
        (today - timedelta(days=5)).isoformat(),
        "Despesa recente",
        -125,
        "Casa e contas",
        "Conta",
    )
    worker = DashboardWorker(
        1,
        repo,
        analytics,
        "1900-01-01",
        today.isoformat(),
        None,
        "monthly",
        "none",
        enabled_modules={"chart"},
        period_label="todo o histórico",
    )

    data = worker._load_data(repo, analytics)

    assert data is not None
    assert data["forecast"]
    assert (
        data["forecast"][0]["date"]
        == (today - timedelta(days=45)).replace(day=1).isoformat()
    )
    assert any(row.get("is_forecast") for row in data["forecast"])
    assert data["comparison_start"] == ""
    assert data["comparison_end"] == ""


def test_dashboard_comparison_populates_every_comparable_chart(
    repo,
    analytics,
):
    from ui.dashboard_worker import DashboardWorker

    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    repo.add_transaction(
        "2026-06-04",
        "Salário anterior",
        1_800,
        "Rendimentos",
        "Conta",
    )
    repo.add_transaction(
        "2026-06-11",
        "Supermercado anterior",
        -320,
        "Alimentação",
        "Conta",
    )
    repo.add_transaction(
        "2026-07-04",
        "Salário atual",
        2_000,
        "Rendimentos",
        "Conta",
    )
    repo.add_transaction(
        "2026-07-11",
        "Supermercado atual",
        -260,
        "Alimentação",
        "Conta",
    )
    worker = DashboardWorker(
        1,
        repo,
        analytics,
        "2026-07-01",
        "2026-07-31",
        31,
        "daily",
        "previous",
        enabled_modules={
            "cashflow_pulse",
            "savings_efficiency",
            "donut",
        },
    )

    data = worker._load_data(repo, analytics)

    assert data is not None
    assert data["comparison_start"] == "2026-05-31"
    assert data["comparison_end"] == "2026-06-30"
    assert data["cashflow_pulse_previous"]
    assert data["savings_efficiency_previous"]
    assert data["previous_categories"][0][0] == "Supermercado"
    assert data["previous_categories"][0][1] == pytest.approx(320)
    assert len(data["metric_trends"]["income"]) <= 24
    assert sum(data["metric_trends"]["income"]) == pytest.approx(2_000)
    assert sum(data["metric_trends"]["expenses"]) == pytest.approx(260)
    assert data["data_context"]["data_as_of"] == "2026-07-11"
    assert "coverage_pct" in data["forecast_data_context"]


def test_data_coverage_snapshot_reports_date_span_and_confidence(
    repo,
    analytics,
):
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    for month in (4, 5, 6):
        for day in (2, 8, 15, 22):
            repo.add_transaction(
                f"2026-{month:02d}-{day:02d}",
                f"Movimento {month}-{day}",
                -25,
                "Casa e contas",
                "Conta",
            )

    snapshot = analytics.data_coverage_snapshot(
        "2026-04-01",
        "2026-06-30",
    )
    empty = analytics.data_coverage_snapshot(
        "2025-01-01",
        "2025-03-31",
    )
    invalid = analytics.data_coverage_snapshot("data inválida", "")

    assert {
        key: snapshot[key]
        for key in (
            "data_as_of",
            "first_data_date",
            "movement_count",
            "active_months",
            "expected_months",
            "coverage_pct",
            "confidence",
            "confidence_label",
        )
    } == {
        "data_as_of": "2026-06-22",
        "first_data_date": "2026-04-02",
        "movement_count": 12,
        "active_months": 3,
        "expected_months": 3,
        "coverage_pct": pytest.approx(100),
        "confidence": "high",
        "confidence_label": "Confiança alta",
    }
    assert snapshot["initial_balances_known"] <= snapshot["initial_balances_total"]
    assert snapshot["investment_cost_known"] <= snapshot["investment_cost_total"]
    assert snapshot["invalid_transactions_excluded"] == 0
    assert empty["movement_count"] == 0
    assert empty["data_as_of"] == ""
    assert empty["confidence"] == "initial"
    assert invalid["coverage_pct"] == 0
    assert invalid["confidence_label"] == "Confiança inicial"


def test_inclusive_month_period_excludes_first_day_of_next_month(repo):
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    repo.add_transaction("2026-07-31", "Receita de julho", 1000, "Rendimentos", "Conta")
    repo.add_transaction(
        "2026-08-01", "Receita de agosto", 2500, "Rendimentos", "Conta"
    )

    period_start, period_end, _ = repo.month_period(2026, 7)
    totals = repo.totals_for_payment_period(period_start, period_end)

    assert period_start == "2026-07-01"
    assert period_end == "2026-07-31"
    assert totals["income"] == pytest.approx(1000)


def test_dashboard_worker_skips_expensive_disabled_modules(repo):
    from services.payments import PaymentsService
    from ui.pages.dashboard import DashboardWorker

    class GuardAnalytics(AnalyticsService):
        def balance_forecast_series(self, *_args, **_kwargs):
            raise AssertionError("disabled balance chart was calculated")

        def category_totals(self, *_args, **_kwargs):
            raise AssertionError("disabled category charts were calculated")

        def balance_series(self, *_args, **_kwargs):
            raise AssertionError("disabled heatmap was calculated")

        def detect_recurring_subscriptions(self, *_args, **_kwargs):
            raise AssertionError("disabled insights were calculated")

        def monthly_budget_forecast(self, *_args, **_kwargs):
            raise AssertionError("disabled budget forecast was calculated")

        def cashflow_calendar_forecast(self, *_args, **_kwargs):
            raise AssertionError("disabled cash-flow forecast was calculated")

    analytics = GuardAnalytics(repo, payments_service=PaymentsService(repo))
    result: list[dict] = []
    errors: list[str] = []
    worker = DashboardWorker(
        1,
        repo,
        analytics,
        "2026-01-01",
        "2026-07-31",
        90,
        "daily",
        "none",
        enabled_modules=set(),
    )
    worker.finished_data.connect(lambda _request_id, data: result.append(data))
    worker.failed.connect(lambda _request_id, error: errors.append(error))

    worker.run()

    assert not errors
    assert result
    assert result[0]["forecast"] == []
    assert result[0]["cashflow_pulse"] == []
    assert set(result[0]["metric_trends"]) == {"income", "expenses", "savings"}
    assert result[0]["savings_efficiency"] == []
    assert result[0]["categories"] == []
    assert result[0]["heatmap"] == []
    assert result[0]["decision_briefing"] == {}
    assert result[0]["budget_forecast"] == {}
    assert result[0]["cashflow_forecast"] == {}


def test_totals_for_period(analytics):
    today = date.today()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()

    totals = analytics.totals_for_period(start, end)

    assert "income" in totals
    assert "expenses" in totals
    assert "savings" in totals
    assert totals["savings"] == totals["income"] - totals["expenses"]


def test_category_totals(analytics):
    today = date.today()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()

    cats = analytics.category_totals(start, end, limit=5)

    assert isinstance(cats, list)
    for row in cats:
        assert len(row) == 3  # category, value, color


def test_complete_category_and_dynamic_results_are_not_silently_truncated(
    repo, analytics
):
    repo.db.conn.execute("DELETE FROM transaction_splits")
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES('2026-07-10', ?, -10, ?, 'Conta', 'expense')
        """,
        [(f"Movimento {index}", f"Categoria {index:02d}") for index in range(85)],
    )
    repo.db.conn.commit()

    categories = analytics.category_totals("2026-07-01", "2026-07-31")
    grouped = repo.dynamic_grouped(
        "2026-07-01", "2026-07-31", "Categoria", "Pagamento", "Despesas"
    )

    assert len(categories) == 85
    assert len(grouped) == 85
    assert len(analytics.category_totals("2026-07-01", "2026-07-31", 8)) == 8
    assert len(repo.transactions_filtered(limit=None)) == 85
    assert len(repo.transactions_filtered(limit=10)) == 10
    first_page = repo.transactions_filtered(limit=10, offset=0)
    second_page = repo.transactions_filtered(limit=10, offset=10)
    assert {int(row["id"]) for row in first_page}.isdisjoint(
        {int(row["id"]) for row in second_page}
    )
    assert (
        len(repo.transactions_filtered_fresh("2026-07-01", "2026-07-31", limit=None))
        == 85
    )
    summary = repo.transactions_filtered_summary_fresh("2026-07-01", "2026-07-31")
    assert int(summary["movement_count"]) == 85
    assert summary["income"] == pytest.approx(0)
    assert summary["expenses"] == pytest.approx(850)


def test_monthly_review_counts_every_duplicate_group(repo, analytics):
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    rows = []
    for index in range(9):
        rows.extend(
            [
                (
                    f"2026-07-{index + 1:02d}",
                    f"Duplicado {index}",
                    -10.0 - index,
                    "Diversos",
                    "Conta",
                    "expense",
                ),
                (
                    f"2026-07-{index + 1:02d}",
                    f"Duplicado {index}",
                    -10.0 - index,
                    "Diversos",
                    "Conta",
                    "expense",
                ),
            ]
        )
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?,?,?,?,?,?)
        """,
        rows,
    )
    repo.db.conn.commit()

    assert len(analytics.monthly_review_data("2026-07")["duplicates"]) == 9
    assert len(repo.possible_duplicates()) == 9


def test_invalid_stored_dates_and_numbers_are_reported_not_hidden(repo, analytics):
    # Simulate a damaged pre-V3.12 database. New writes are rejected by the
    # database triggers, but legacy invalid rows must still be visible for review.
    repo.db.conn.execute("DROP TRIGGER validate_transactions_insert")
    repo.db.conn.execute(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES('2026-02-31', 'Linha corrompida', 'valor-inválido', 'Outros', 'Conta', 'expense')
        """
    )
    repo.db.conn.commit()

    quality = repo.transaction_data_quality_counts("2026-07-01", "2026-07-31")
    review = analytics.monthly_review_data("2026-07")

    assert quality["invalid_dates"] >= 1
    assert quality["invalid_amounts"] >= 1
    assert review["invalid_dates"] >= 1
    assert review["invalid_amounts"] >= 1
    totals = analytics.totals_for_period("2026-01-01", "2026-12-31")
    summary = repo.transactions_filtered_summary_fresh()
    assert totals["income"] >= 0
    assert totals["expenses"] >= 0
    assert int(summary["excluded_invalid_count"]) >= 1
    assert int(summary["invalid_amounts"]) >= 1


def test_invalid_date_and_non_finite_number_helpers_do_not_create_fake_values():
    with pytest.raises(ValueError, match="Data inválida"):
        parse_date("31/02/2026")
    with pytest.raises(ValueError, match="NaN ou infinito"):
        strict_float("NaN")
    assert safe_float("NaN", 7.0) == 7.0
    assert safe_float("inf", 9.0) == 9.0


def test_dashboard_aggregates_exclude_internal_transfers(repo, analytics):
    today = date.today()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    totals_before = analytics.totals_for_period(start, end)
    summary_before = analytics.monthly_summary(1)[0]

    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?, ?, ?, 'Transferência Interna', 'Conta principal', ?)
        """,
        [
            (end, "Transferência recebida", 2500.0, "income"),
            (end, "Transferência enviada", -2500.0, "expense"),
        ],
    )
    repo.db.conn.commit()

    assert analytics.totals_for_period(start, end) == totals_before
    summary_after = analytics.monthly_summary(1)[0]
    assert summary_after["income"] == summary_before["income"]
    assert summary_after["expenses"] == summary_before["expenses"]
    assert all(
        category != "Transferência Interna"
        for category, _value, _color in analytics.category_totals(start, end, 50)
    )


def test_monthly_budget_forecast_combines_current_and_previous_month(repo, analytics):
    repo.db.conn.execute("DELETE FROM transaction_splits")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("UPDATE categories SET budget=0")
    repo.db.conn.commit()
    repo.set_category_budget("Supermercado", 1000)
    repo.set_category_budget("Lazer", 200)
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?, ?, ?, ?, 'Conta principal', 'expense')
        """,
        [
            ("2026-07-05", "Compras julho", -200.0, "Supermercado"),
            ("2026-07-08", "Lazer julho", -180.0, "Lazer"),
            ("2026-07-09", "Sem limite", -100.0, "Diversos"),
            ("2026-06-15", "Compras junho", -310.0, "Supermercado"),
            ("2026-06-18", "Lazer junho", -200.0, "Lazer"),
            ("2026-06-20", "Diversos junho", -90.0, "Diversos"),
        ],
    )
    repo.db.conn.commit()

    forecast = analytics.monthly_budget_forecast("2026-07", date(2026, 7, 10))
    rows = {row["category"]: row for row in forecast["rows"]}

    assert forecast["days_elapsed"] == 10
    assert forecast["days_remaining"] == 21
    assert forecast["total_budget"] == pytest.approx(1200.0)
    assert forecast["coverage_pct"] == pytest.approx(380 / 480 * 100)
    assert rows["Supermercado"]["projected"] == pytest.approx(200 + 310 * 21 / 31)
    assert rows["Lazer"]["forecast_status"] == "Em risco"
    assert rows["Diversos"]["forecast_status"] == "Sem orçamento"
    assert forecast["risk_count"] == 1
    assert forecast["overall_tone"] == "warning"
    assert "mês anterior" in forecast["method_label"]


def test_cashflow_calendar_combines_income_commitments_and_baseline(repo, analytics):
    repo.db.conn.execute("DELETE FROM scheduled_payment_occurrences")
    repo.db.conn.execute("DELETE FROM scheduled_payments")
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.execute("DELETE FROM transaction_splits")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?, ?, ?, ?, 'Conta principal', ?)
        """,
        [
            ("2026-05-02", "Salário", 2000.0, "Rendimentos", "income"),
            ("2026-06-02", "Salário", 2000.0, "Rendimentos", "income"),
            ("2026-04-14", "Mercado", -300.0, "Supermercado", "expense"),
            ("2026-07-12", "Mercado", -300.0, "Supermercado", "expense"),
        ],
    )
    repo.db.conn.commit()
    repo.add_subscription(
        "Streaming",
        30.0,
        "Subscrições",
        20,
        "Mensal",
        last_used_date="2026-06-20",
    )
    repo.add_scheduled_payment(
        "Prestação",
        "Casa e contas",
        "",
        500.0,
        "Conta principal",
        "Débito direto",
        "2026-07-01",
        15,
        3,
    )
    repo.add_scheduled_payment(
        "Pagamento atrasado",
        "Diversos",
        "",
        100.0,
        "Conta principal",
        "Débito direto",
        "2026-06-01",
        10,
        2,
    )

    forecast = analytics.cashflow_calendar_forecast(90, date(2026, 7, 12))
    summary = forecast["summaries"]["30"]
    event_sources = {event["source"] for event in forecast["events"]}

    assert forecast["starting_balance"] == pytest.approx(3400.0)
    assert forecast["variable_daily_expense"] == pytest.approx(600 / 90)
    assert len(forecast["points"]) == 91
    assert {"recurring_income", "subscription", "scheduled"} <= event_sources
    assert summary["expected_income"] == pytest.approx(2000.0)
    assert summary["known_commitments"] == pytest.approx(730.0)
    assert summary["baseline_expenses"] == pytest.approx(200.0)
    assert summary["ending_balance"] == pytest.approx(4470.0)
    assert summary["ending_balance_pessimistic"] < summary["ending_balance_base"]
    assert summary["ending_balance_base"] < summary["ending_balance_optimistic"]
    assert summary["overdue_count"] == 2
    assert summary["tone"] == "positive"
    assert all(
        {
            "balance_pessimistic",
            "balance_base",
            "balance_optimistic",
        }
        <= point.keys()
        for point in forecast["points"]
    )


def test_investment_projection_shows_compounding_for_each_of_five_years(analytics):
    projection = analytics.investment_projection(250.0, 6.0, 5)
    rows = projection["rows"]

    assert [row["year"] for row in rows] == [1, 2, 3, 4, 5]
    assert rows[0]["invested"] == pytest.approx(3_000.0)
    assert rows[-1]["invested"] == pytest.approx(15_000.0)
    monthly_rate = 1.06 ** (1 / 12) - 1
    expected_value = 250.0 * (((1 + monthly_rate) ** 60 - 1) / monthly_rate)
    assert rows[-1]["projected"] == pytest.approx(expected_value)
    assert rows[-1]["gain"] == pytest.approx(expected_value - 15_000.0)
    assert rows[-1]["return_pct"] > rows[0]["return_pct"] > 0
    assert "impostos" in projection["method_label"]

    no_return = analytics.investment_projection(250.0, 0.0, 5)
    assert all(
        row["projected"] == pytest.approx(row["invested"])
        and row["return_pct"] == pytest.approx(0.0)
        for row in no_return["rows"]
    )


def test_investment_projection_supports_single_investment_costs_and_scenarios(
    analytics,
):
    projection = analytics.investment_projection(
        10_000.0,
        6.0,
        5,
        mode="single",
        initial_capital=2_000.0,
        inflation_pct=2.0,
        tax_pct=28.0,
        fee_pct=1.0,
    )

    assert projection["mode"] == "single"
    assert projection["monthly_contribution"] == 0.0
    assert all(row["invested"] == pytest.approx(12_000.0) for row in projection["rows"])
    assert all(row["fees_paid"] == pytest.approx(100.0) for row in projection["rows"])
    assert projection["rows"][-1]["taxes_paid"] > 0
    assert (
        projection["rows"][-1]["projected_base"]
        < projection["rows"][-1]["nominal_base"]
    )
    assert all(
        row["projected_pessimistic"]
        < row["projected_base"]
        < row["projected_optimistic"]
        for row in projection["rows"]
    )


def test_balance_series(analytics):
    series = analytics.balance_series(30)

    assert len(series) <= 30
    if len(series) > 0:
        assert "date" in series[0]
        assert "balance" in series[0]


def test_monthly_evolution(analytics):
    year = date.today().year
    evolution = analytics.monthly_evolution(year)

    assert len(evolution) == 12
    for m in evolution:
        assert "month" in m
        assert "income" in m
        assert "expenses" in m


class _ForecastRepo:
    """Minimal read-only repository used by the pure forecast tests."""

    def q(self, *_args, **_kwargs):
        return []


class _SubscriptionForecastRepo:
    def __init__(self, renewal_date: date):
        self.renewal_date = renewal_date

    def q(self, sql, *_args, **_kwargs):
        if "subscription_occurrences" in str(sql):
            return [{"total": 300.0}]
        return []

    def subscriptions(self, active_only=True):
        return [
            {
                "name": "Seguro",
                "amount": 300.0,
                "period": "Mensal",
                "renewal_day": self.renewal_date.day,
                "next_renewal_date": self.renewal_date.isoformat(),
                "last_used_date": "",
                "active": 1,
            }
        ]


def test_forecast_burn_uses_calendar_days(monkeypatch):
    service = AnalyticsService(_ForecastRepo(), payments_service=object())
    start = date.today() - timedelta(days=29)
    points = []
    for offset in range(30):
        points.append(
            {
                "date": (start + timedelta(days=offset)).isoformat(),
                "balance": 1000.0,
                "delta": 0.0,
                "income": 0.0,
                "expense": 300.0 if offset == 29 else 0.0,
            }
        )
    monkeypatch.setattr(service, "balance_series", lambda days: list(points))

    result = service.balance_forecast_series(
        historical_days=30, forecast_days=1, resolution="daily"
    )

    assert result[-1]["is_forecast"] is True
    assert result[-1]["balance"] == pytest.approx(990.0)
    assert result[-1]["balance_pessimistic"] == pytest.approx(988.0)
    assert result[-1]["balance_base"] == pytest.approx(990.0)
    assert result[-1]["balance_optimistic"] == pytest.approx(992.0)


def test_forecast_does_not_count_linked_subscription_twice(monkeypatch):
    tomorrow = date.today() + timedelta(days=1)
    repo = _SubscriptionForecastRepo(tomorrow)
    service = AnalyticsService(repo, payments_service=PaymentsService(repo))
    start = date.today() - timedelta(days=29)
    points = [
        {
            "date": (start + timedelta(days=offset)).isoformat(),
            "balance": 1000.0,
            "delta": 0.0,
            "income": 0.0,
            "expense": 300.0 if offset == 29 else 0.0,
        }
        for offset in range(30)
    ]
    monkeypatch.setattr(service, "balance_series", lambda days: list(points))

    result = service.balance_forecast_series(
        historical_days=30, forecast_days=1, resolution="daily"
    )

    forecast = result[-1]
    assert forecast["daily_baseline"] == pytest.approx(0.0)
    assert forecast["scheduled_expense"] == pytest.approx(300.0)
    assert forecast["balance"] == pytest.approx(700.0)


def test_upcoming_subscriptions_respects_month_boundaries_and_real_due_dates():
    service = PaymentsService(object())
    reference = date(2026, 1, 31)
    rows = [
        {
            "name": "Mensal dia 1",
            "amount": 10,
            "period": "Mensal",
            "renewal_day": 1,
            "next_renewal_date": "",
            "last_used_date": "",
        },
        {
            "name": "Anual distante",
            "amount": 120,
            "period": "Anual",
            "renewal_day": 31,
            "next_renewal_date": "2026-07-31",
            "last_used_date": "",
        },
    ]

    upcoming = service.upcoming_subscriptions(rows, reference=reference, within_days=3)

    assert upcoming == [{"name": "Mensal dia 1", "date": "2026-02-01", "days": 1}]


def test_duplicate_detection_uses_payment_date_and_normalized_description(repo):
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    transaction_id = repo.add_transaction(
        "2026-07-01", "Loja XPTO", -42.5, "Diversos", "Conta"
    )
    repo.set_transaction_meta_ext(transaction_id, payment_date="2026-07-05")

    assert repo.is_duplicate_transaction("2026-07-05", "  loja xpto  ", -42.5)
    assert not repo.is_duplicate_transaction("2026-07-01", "Loja XPTO", -42.5)


def test_grouped_forecast_uses_closing_balance(monkeypatch):
    service = AnalyticsService(_ForecastRepo(), payments_service=object())
    monday = date.today() - timedelta(days=date.today().weekday() + 7)
    points = [
        {
            "date": monday.isoformat(),
            "balance": 100.0,
            "delta": 10.0,
            "income": 10.0,
            "expense": 0.0,
        },
        {
            "date": (monday + timedelta(days=1)).isoformat(),
            "balance": 125.0,
            "delta": 25.0,
            "income": 25.0,
            "expense": 0.0,
        },
    ]
    monkeypatch.setattr(service, "balance_series", lambda days: list(points))

    result = service.balance_forecast_series(
        historical_days=2, forecast_days=0, resolution="weekly"
    )

    assert len(result) == 1
    assert result[0]["balance"] == pytest.approx(125.0)
    assert result[0]["balance_base"] == pytest.approx(125.0)
    assert result[0]["delta"] == pytest.approx(35.0)


def test_balance_forecast_accepts_full_history_window(monkeypatch):
    service = AnalyticsService(_ForecastRepo(), payments_service=object())
    today = date.today()
    points = [
        {
            "date": (today - timedelta(days=2)).isoformat(),
            "balance": 100.0,
            "delta": 0.0,
            "income": 0.0,
            "expense": 0.0,
        },
        {
            "date": (today - timedelta(days=1)).isoformat(),
            "balance": 125.0,
            "delta": 25.0,
            "income": 25.0,
            "expense": 0.0,
        },
        {
            "date": today.isoformat(),
            "balance": 120.0,
            "delta": -5.0,
            "income": 0.0,
            "expense": 5.0,
        },
    ]
    requested_windows: list[int | None] = []
    coverage_periods: list[tuple[str, str]] = []

    def balance_series(days):
        requested_windows.append(days)
        return list(points)

    monkeypatch.setattr(service, "balance_series", balance_series)
    monkeypatch.setattr(
        service,
        "data_coverage_snapshot",
        lambda start, end: coverage_periods.append((start, end)) or {},
    )

    result = service.balance_forecast_series(
        historical_days=None,
        forecast_days=1,
        resolution="daily",
    )

    assert requested_windows == [None]
    assert coverage_periods == [(points[0]["date"], today.isoformat())]
    assert result[-1]["is_forecast"] is True


def test_monte_carlo_projection_is_deterministic_and_orders_percentiles(analytics):
    first = analytics.monte_carlo_projection(
        300,
        6,
        12,
        10,
        simulations=500,
        target=50_000,
        initial_capital=5_000,
        inflation_pct=2,
        tax_pct=28,
        fee_pct=0.5,
        seed=17,
    )
    second = analytics.monte_carlo_projection(
        300,
        6,
        12,
        10,
        simulations=500,
        target=50_000,
        initial_capital=5_000,
        inflation_pct=2,
        tax_pct=28,
        fee_pct=0.5,
        seed=17,
    )

    assert first == second
    assert len(first["rows"]) == 10
    assert all(row["p10"] <= row["p50"] <= row["p90"] for row in first["rows"])
    assert 0 <= first["probability_target"] <= 100
    assert first["rows"][-1]["invested_nominal"] == pytest.approx(41_000)
    assert first["rows"][-1]["invested"] == pytest.approx(
        first["rows"][-1]["invested_real"]
    )
    assert first["rows"][-1]["invested_real"] < 41_000


def test_complete_net_worth_includes_accounts_investments_and_liabilities(
    repo, analytics
):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM liabilities")
    repo.db.conn.execute("DELETE FROM goals")
    repo.db.conn.execute(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES('2026-07-01', 'Saldo', 5000, 'Rendimentos', 'Conta A', 'income')
        """
    )
    repo.db.conn.commit()
    repo.add_holding("ETF Global", "ETF", "Corretora", 10_000, 8_000)
    repo.add_liability(
        "Crédito automóvel", "Crédito automóvel", "Banco", 4_000, 7.5, 220
    )

    snapshot = analytics.net_worth_snapshot()

    assert snapshot["total_assets"] == pytest.approx(15_000)
    assert snapshot["total_liabilities"] == pytest.approx(4_000)
    assert snapshot["net_worth"] == pytest.approx(11_000)
    assert snapshot["liquid_assets"] == pytest.approx(5_000)
    assert snapshot["monthly_debt_payment"] == pytest.approx(220)
    assert snapshot["debt_ratio_pct"] == pytest.approx(4_000 / 15_000 * 100)
    assert repo.net_worth() == pytest.approx(11_000)


def test_net_worth_bridge_reconciles_cash_portfolio_and_liabilities(
    repo,
    analytics,
):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM liabilities")
    repo.db.conn.execute("DELETE FROM goals")
    repo.db.conn.execute("DELETE FROM investment_snapshots")
    repo.db.conn.execute("DELETE FROM financial_state_snapshots")
    repo.db.conn.commit()

    repo.add_transaction("2026-05-01", "Saldo inicial", 5_000, "Rendimentos", "Conta")
    holding_id = repo.add_holding("ETF", "ETF", "Corretora", 1_200, 1_000)
    liability_id = repo.add_liability(
        "Crédito",
        "Crédito pessoal",
        "Banco",
        500,
        5,
        50,
    )
    repo.record_investment_snapshot("2026-05-31")
    repo.record_financial_state_snapshot("2026-05-31")

    repo.add_transaction("2026-06-02", "Salário", 2_000, "Rendimentos", "Conta")
    repo.add_transaction("2026-06-10", "Despesas", -800, "Casa", "Conta")
    repo.add_transaction(
        "2026-06-20",
        "Reforço ETF",
        -500,
        "Investimentos",
        "Conta",
    )
    repo.update_holding(holding_id, "ETF", "ETF", "Corretora", 1_900, 1_500)
    repo.update_liability(
        liability_id,
        "Crédito",
        "Crédito pessoal",
        "Banco",
        300,
        5,
        50,
    )
    repo.record_investment_snapshot("2026-06-30")
    repo.record_financial_state_snapshot("2026-06-30")

    bridge = analytics.net_worth_bridge("2026-06-01", "2026-06-30")

    assert bridge["opening_net_worth"] == pytest.approx(5_700)
    assert bridge["closing_net_worth"] == pytest.approx(7_300)
    assert bridge["income"] == pytest.approx(2_000)
    assert bridge["expenses"] == pytest.approx(1_300)
    assert bridge["invested_capital_change"] == pytest.approx(500)
    assert bridge["market_result"] == pytest.approx(200)
    assert bridge["liability_reduction"] == pytest.approx(200)
    assert bridge["change"] == pytest.approx(1_600)
    assert bridge["coverage_pct"] == pytest.approx(100)
    assert bridge["explained_change_pct"] == pytest.approx(100)
    assert bridge["history_complete"] is True
    assert bridge["unknown_change_magnitude"] == pytest.approx(0)
    assert bridge["complete"] is True
    assert bridge["steps"][0]["key"] == "opening"
    assert bridge["steps"][-1]["key"] == "closing"
    assert not any(row["key"] == "reconciliation" for row in bridge["steps"])


def test_net_worth_bridge_does_not_invent_market_gain_without_history(
    repo,
    analytics,
):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM liabilities")
    repo.db.conn.execute("DELETE FROM goals")
    repo.db.conn.execute("DELETE FROM investment_snapshots")
    repo.db.conn.execute("DELETE FROM financial_state_snapshots")
    repo.db.conn.commit()

    today = date.today()
    start = today - timedelta(days=30)
    repo.add_holding("ETF atual", "ETF", "Corretora", 12_000, 10_000)

    bridge = analytics.net_worth_bridge(start.isoformat(), today.isoformat())
    unknown = next(
        row for row in bridge["steps"] if row["key"] == "unclassified_investments"
    )

    assert bridge["complete"] is False
    assert bridge["invested_capital_change"] == pytest.approx(0)
    assert bridge["market_result"] == pytest.approx(0)
    assert bridge["unclassified_investment_change"] == pytest.approx(12_000)
    assert bridge["explained_change_pct"] == pytest.approx(0)
    assert bridge["history_complete"] is False
    assert bridge["unknown_change_magnitude"] == pytest.approx(12_000)
    assert unknown["value"] == pytest.approx(12_000)
    assert "sem fingir rentabilidade" in unknown["explanation"]


def test_fixed_commitments_evolution_uses_explicit_events_without_duplicates(
    repo,
    analytics,
):
    repo.db.conn.execute("DELETE FROM subscription_occurrences")
    repo.db.conn.execute("DELETE FROM scheduled_payment_occurrences")
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.execute("DELETE FROM scheduled_payments")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()

    subscription_id = repo.add_subscription(
        "Streaming",
        30,
        "Subscrições",
        20,
        "Mensal",
        last_used_date="2026-06-20",
    )
    repo.add_scheduled_payment(
        "Prestação",
        "Casa e contas",
        "",
        100,
        "Conta",
        "Débito direto",
        "2026-07-01",
        15,
        3,
    )
    transaction_id = repo.add_transaction(
        "2026-08-18",
        "Streaming (Subscrição)",
        -30,
        "Subscrições",
        "Conta",
    )
    repo.add_subscription_occurrence(
        subscription_id,
        "2026-08-20",
        transaction_id,
    )

    result = analytics.fixed_commitments_evolution(
        3,
        date(2026, 7, 12),
    )

    assert [row["month"] for row in result["rows"]] == [
        "2026-07",
        "2026-08",
        "2026-09",
    ]
    assert [row["total"] for row in result["rows"]] == pytest.approx([130, 130, 130])
    assert result["subscription_total"] == pytest.approx(90)
    assert result["scheduled_total"] == pytest.approx(300)
    assert result["monthly_average"] == pytest.approx(130)
    assert result["trend_pct"] == pytest.approx(0)
    assert result["rows"][1]["event_count"] == 2
    assert (
        sum(
            1
            for event in result["events"]
            if event.get("source") == "subscription"
            and event.get("linked_due_date") == "2026-08-20"
        )
        == 1
    )
    cashflow = analytics.cashflow_calendar_forecast(
        80,
        date(2026, 7, 12),
    )
    streaming_events = [
        event
        for event in cashflow["events"]
        if "streaming" in str(event.get("name") or "").casefold()
    ]
    assert [event["date"] for event in streaming_events] == [
        "2026-07-20",
        "2026-08-18",
        "2026-09-20",
    ]


def test_fixed_commitment_average_excludes_current_partial_month(repo, analytics):
    repo.db.conn.execute("DELETE FROM subscription_occurrences")
    repo.db.conn.execute("DELETE FROM scheduled_payment_occurrences")
    repo.db.conn.execute("DELETE FROM subscriptions")
    repo.db.conn.execute("DELETE FROM scheduled_payments")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()

    repo.add_subscription(
        "Streaming",
        30,
        "Subscrições",
        20,
        "Mensal",
        last_used_date="2026-06-20",
    )
    repo.add_scheduled_payment(
        "Despesa única",
        "Casa e contas",
        "",
        100,
        "Conta",
        "Débito direto",
        "2026-07-01",
        15,
        1,
    )

    result = analytics.fixed_commitments_evolution(
        3,
        date(2026, 7, 12),
    )

    assert [row["total"] for row in result["rows"]] == pytest.approx([130, 30, 30])
    assert result["rows"][0]["partial"] is True
    assert result["partial_month_total"] == pytest.approx(130)
    assert result["complete_months"] == 2
    assert result["monthly_average"] == pytest.approx(30)
    assert result["summaries"]["3"]["monthly_average"] == pytest.approx(30)


def test_smart_budget_calculates_rollover_suggestion_and_breach(repo, analytics):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("UPDATE categories SET budget=0")
    repo.db.conn.commit()
    repo.set_category_budget("Supermercado", 300)
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?, 'Compras', ?, 'Supermercado', 'Conta', 'expense')
        """,
        [
            ("2026-04-10", -100),
            ("2026-05-10", -100),
            ("2026-06-10", -100),
            ("2026-07-05", -200),
        ],
    )
    repo.db.conn.commit()

    analysis = analytics.smart_budget_analysis("2026-07", date(2026, 7, 10))
    row = next(row for row in analysis["rows"] if row["category"] == "Supermercado")

    assert row["rollover"] == pytest.approx(200)
    assert row["effective_budget"] == pytest.approx(500)
    assert row["projected"] == pytest.approx(620)
    assert row["suggested_budget"] == pytest.approx(270)
    assert row["breach_date"] == "2026-07-25"
    assert row["status"] == "Rutura prevista"


def test_anomaly_detection_finds_spikes_duplicates_and_price_increases(repo, analytics):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?, ?, ?, ?, 'Conta', 'expense')
        """,
        [
            ("2026-05-01", "Mercado", -20, "Supermercado"),
            ("2026-05-15", "Mercado", -22, "Supermercado"),
            ("2026-06-01", "Mercado", -21, "Supermercado"),
            ("2026-06-15", "Mercado", -19, "Supermercado"),
            ("2026-07-10", "Compra excecional", -200, "Supermercado"),
            ("2026-07-11", "Cobrança repetida", -50, "Diversos"),
            ("2026-07-11", "Cobrança repetida", -50, "Diversos"),
            ("2026-06-12", "Streaming", -10, "Subscrições"),
            ("2026-07-12", "Streaming", -12, "Subscrições"),
        ],
    )
    repo.db.conn.commit()

    anomalies = analytics.detect_financial_anomalies(date(2026, 7, 12), 180)
    kinds = {alert["kind"] for alert in anomalies}

    assert {"spike", "duplicate", "increase"} <= kinds
    assert all(alert["action_page"] == "movimentos" for alert in anomalies)


def test_anomaly_detection_respects_the_visible_reporting_period(repo, analytics):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?, ?, -25, 'Diversos', 'Conta', 'expense')
        """,
        [
            ("2026-04-01", "Duplicado antigo"),
            ("2026-04-01", "Duplicado antigo"),
            ("2026-07-10", "Duplicado recente"),
            ("2026-07-10", "Duplicado recente"),
        ],
    )
    repo.db.conn.commit()

    recent = analytics.detect_financial_anomalies(
        date(2026, 7, 12), lookback_days=180, reporting_days=30
    )
    extended = analytics.detect_financial_anomalies(
        date(2026, 7, 12), lookback_days=180, reporting_days=120
    )

    assert {alert["search"] for alert in recent} == {"Duplicado recente"}
    assert {alert["search"] for alert in extended} == {
        "Duplicado antigo",
        "Duplicado recente",
    }


def test_alert_center_aggregates_high_interest_debt(repo, analytics):
    repo.db.conn.execute("DELETE FROM liabilities")
    repo.db.conn.commit()
    repo.add_liability("Cartão", "Cartão de crédito", "Banco", 1_500, 19.9, 90)

    alerts = analytics.financial_alert_center(date(2026, 7, 12))

    debt_alert = next(alert for alert in alerts if alert["kind"] == "debt")
    assert debt_alert["severity"] == "critical"
    assert debt_alert["action_page"] == "carteira"


def test_period_comparator_returns_totals_and_category_deltas(repo, analytics):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?, ?, ?, ?, 'Conta', ?)
        """,
        [
            ("2026-06-01", "Salário", 1800, "Rendimentos", "income"),
            ("2026-06-05", "Mercado", -500, "Supermercado", "expense"),
            ("2026-07-01", "Salário", 2000, "Rendimentos", "income"),
            ("2026-07-05", "Mercado", -600, "Supermercado", "expense"),
            ("2026-07-06", "Cinema", -100, "Lazer", "expense"),
        ],
    )
    repo.db.conn.commit()

    comparison = analytics.compare_periods(
        "2026-07-01", "2026-07-31", "2026-06-01", "2026-06-30"
    )
    categories = {row["category"]: row for row in comparison["categories"]}

    assert set(repo.categories("expense")) <= set(categories)
    assert comparison["metrics"]["income"]["delta"] == pytest.approx(200)
    assert comparison["metrics"]["expenses"]["delta"] == pytest.approx(200)
    assert comparison["metrics"]["savings"]["delta"] == pytest.approx(0)
    assert categories["Supermercado"]["delta"] == pytest.approx(100)
    assert categories["Lazer"]["reference"] == 0
    assert categories["Animais"]["current"] == 0
    assert categories["Animais"]["reference"] == 0


def test_period_comparator_uses_split_categories_without_hiding_parent_rows(
    repo, analytics
):
    repo.db.conn.execute("DELETE FROM transaction_splits")
    repo.db.conn.execute("DELETE FROM transaction_meta")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    reference_id = repo.add_transaction(
        "2026-06-08", "Compra repartida anterior", -80, "Supermercado", "Conta"
    )
    repo.save_transaction_splits(
        reference_id,
        [("Lazer", 50), ("Transporte", 50)],
    )
    current_id = repo.add_transaction(
        "2026-07-08", "Compra repartida atual", -100, "Supermercado", "Conta"
    )
    repo.save_transaction_splits(
        current_id,
        [("Lazer", 25), ("Diversos", 75)],
    )

    comparison = analytics.compare_periods(
        "2026-07-01", "2026-07-31", "2026-06-01", "2026-06-30"
    )
    categories = {row["category"]: row for row in comparison["categories"]}

    assert categories["Lazer"]["current"] == pytest.approx(25)
    assert categories["Lazer"]["reference"] == pytest.approx(40)
    assert categories["Diversos"]["current"] == pytest.approx(75)
    assert categories["Transporte"]["reference"] == pytest.approx(40)
    assert categories["Supermercado"]["current"] == 0
    assert categories["Supermercado"]["reference"] == 0


def test_period_variation_explanation_identifies_main_causes(repo, analytics):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.executemany(
        """
        INSERT INTO transactions(date, description, amount, category, account, kind)
        VALUES(?, ?, ?, ?, 'Conta', ?)
        """,
        [
            ("2025-07-01", "Salário", 1800, "Rendimentos", "income"),
            ("2025-07-05", "Mercado", -300, "Supermercado", "expense"),
            ("2026-07-01", "Salário", 2000, "Rendimentos", "income"),
            ("2026-07-05", "Mercado", -550, "Supermercado", "expense"),
            ("2026-07-06", "Cinema", -100, "Lazer", "expense"),
        ],
    )
    repo.db.conn.commit()
    comparison = analytics.compare_periods(
        "2026-07-01", "2026-07-31", "2025-07-01", "2025-07-31"
    )

    explanations = analytics.explain_period_variation(comparison)
    titles = {row["title"] for row in explanations}

    assert explanations[0]["delta"] == pytest.approx(350)
    assert "Mais gasto em Supermercado" in titles
    assert "Receitas cresceram" in titles
    assert "Poupança recuou" in titles
    assert all(row["message"] for row in explanations)


def test_annual_report_combines_cashflow_investments_and_documents(repo, analytics):
    repo.db.conn.execute("DELETE FROM transaction_attachments")
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM investment_snapshots")
    repo.db.conn.commit()
    repo.add_transaction(
        "2026-01-03", "Salário", 2400, "Rendimentos", "Conta principal"
    )
    transaction_id = repo.add_transaction(
        "2026-01-09", "Computador", -1200, "Compras grandes", "Conta principal"
    )
    repo.add_transaction(
        "2025-01-09", "Computador anterior", -800, "Compras grandes", "Conta principal"
    )
    repo.add_holding(
        "ETF Global",
        "ETF",
        "Corretora",
        12_500,
        10_000,
        fees=125,
    )
    repo.record_investment_snapshot("2026-06-30")
    repo.add_transaction_attachment(
        transaction_id=transaction_id,
        file_name="fatura.pdf",
        stored_name="fatura.pdf",
        stored_path="C:/tmp/fatura.pdf",
        original_path="C:/tmp/fatura.pdf",
        file_type="PDF",
        description="Equipamento",
        supplier="Loja Exemplo",
        invoice_date="2026-01-09",
        total_amount=1200,
    )

    report = analytics.annual_report_data(2026)

    assert len(report["monthly"]) == 12
    assert report["monthly"][0]["income"] == pytest.approx(2400)
    assert report["monthly"][0]["expenses"] == pytest.approx(1200)
    assert report["investment_total"] == pytest.approx(12_500)
    assert report["investment_cost"] == pytest.approx(10_000)
    assert report["investment_gain"] == pytest.approx(2_500)
    assert report["investment_fees"] == pytest.approx(125)
    assert report["investment_net_gain"] == pytest.approx(2_375)
    assert report["investment_net_return_pct"] == pytest.approx(2_375 / 10_125 * 100)
    assert report["investment_attribution"][0]["contribution_pp"] == (
        pytest.approx(report["investment_net_return_pct"])
    )
    assert any(row["date"] == "2026-06-30" for row in report["investment_evolution"])
    assert report["documents"]["document_count"] == 1
    assert report["documents"]["documented_total"] == pytest.approx(1200)
    assert report["document_suppliers"][0]["supplier"] == "Loja Exemplo"
    assert report["explanations"]


def test_financial_score_2_is_explainable_and_has_one_hundred_point_weight(
    repo, analytics
):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM liabilities")
    repo.db.conn.execute("UPDATE categories SET budget=0")
    repo.db.conn.commit()
    repo.set_category_budget("Supermercado", 500)
    for month in range(1, 7):
        repo.add_transaction(
            f"2026-{month:02d}-02", "Salário", 2_500, "Rendimentos", "Conta"
        )
        repo.add_transaction(
            f"2026-{month:02d}-10", "Compras", -800, "Supermercado", "Conta"
        )
        repo.add_transaction(
            f"2026-{month:02d}-16", "Casa", -700, "Casa e contas", "Conta"
        )
    repo.add_holding("ETF global", "ETF", "Corretora", 5_000, 4_500)

    score = analytics.detailed_financial_score("2026-06")

    assert 0 <= score["score"] <= 100
    assert score["grade"] in {"A+", "A", "B+", "B", "C+", "C", "D", "E"}
    assert len(score["dimensions"]) == len(score["criteria"]) == 7
    assert sum(row["max_points"] for row in score["dimensions"]) == pytest.approx(100)
    assert len(score["trend"]) == 12
    assert 0 <= score["confidence"] <= 100
    assert score["data_as_of"] == "2026-06-16"
    assert score["actions"]
    assert score["causes"]
    assert all(
        {"key", "score", "points", "status", "marker", "action"} <= row.keys()
        for row in score["dimensions"]
    )


def test_risk_resilience_stress_controls_change_the_projection(repo, analytics):
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.execute("DELETE FROM holdings")
    repo.db.conn.execute("DELETE FROM liabilities")
    repo.db.conn.commit()
    for month in range(1, 7):
        repo.add_transaction(
            f"2026-{month:02d}-02", "Salário", 2_400, "Rendimentos", "Conta"
        )
        repo.add_transaction(
            f"2026-{month:02d}-12", "Despesas", -1_750, "Casa e contas", "Conta"
        )
    repo.add_holding("ETF global", "ETF", "Corretora", 8_000, 7_000)

    mild = analytics.risk_resilience_analysis(
        as_of=date(2026, 7, 25),
        horizon_months=12,
        income_reduction_pct=10,
        income_reduction_months=3,
        emergency_expense=500,
        market_drawdown_pct=5,
    )
    severe = analytics.risk_resilience_analysis(
        as_of=date(2026, 7, 25),
        horizon_months=12,
        income_reduction_pct=60,
        income_reduction_months=12,
        emergency_expense=8_000,
        market_drawdown_pct=45,
    )

    assert len(severe["rows"]) == 13
    assert len(severe["components"]) == 5
    assert 0 <= severe["score"] <= 100
    assert 0 <= severe["probability_negative"] <= 100
    assert severe["rows"][-1]["stressed"] < mild["rows"][-1]["stressed"]
    assert severe["stressed_final_net_worth"] < mild["stressed_final_net_worth"]
    assert severe["probability_negative"] >= mild["probability_negative"]
    assert all(row["p10"] <= row["p50"] <= row["p90"] for row in severe["rows"])
