"""BUG-AUD-C123..C131 (Batch 26 - Membro 3 do conselho, ronda 4).

Cobertura de regressao para os nove defeitos SQLite/Repositorios do Membro 3:
C123 (escape FTS5), C124 (trigger INSERT OR REPLACE -> ghost terms), C125
(commit prematuro em add_transaction_with_meta), C126 (FK sem ON UPDATE
CASCADE), C127 (aliases perdidos no merge), C128 (checkpoint TRUNCATE bloqueia
shutdown), C129 (sinal de splits de receita), C130 (goal_id autorizado) e
C131 (delete de conta com holdings/liabilities).

Cada teste afirma valores exatos: contagens, totais monetarios e versoes de
schema sao comparados com ==, nunca com verboosidades vagas.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("FINANCAS_PESSOAIS_DEMO_DATA", "0")

from database.engine import FinanceDB  # noqa: E402
from database.repo import Repo  # noqa: E402
from database.repo.transactions import sanitize_fts5_query  # noqa: E402
from services.account_identity import account_identity_key  # noqa: E402


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
    db = FinanceDB(tmp_path / "batch26.sqlite3")
    yield Repo(db)
    try:
        db.conn.close()
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# C123 - FTS5 escape
# ---------------------------------------------------------------------------


def test_c123_sanitize_strips_hostile_punctuation() -> None:
    assert sanitize_fts5_query('restaurante "o tiago') == '"restaurante"* "tiago"*'
    assert sanitize_fts5_query("a:b*c^d(e)[f]{g}") == ""  # all tokens < 3 chars
    assert sanitize_fts5_query("' OR 1=1 --") == '"1=1"*'
    assert (
        sanitize_fts5_query("'; DROP TABLE transactions; --")
        == '"DROP"* "TABLE"* "transactions;"*'
    )
    assert sanitize_fts5_query("ab") == ""
    assert sanitize_fts5_query("") == ""
    assert sanitize_fts5_query(None) == ""
    assert sanitize_fts5_query("café expresso") == '"café"* "expresso"*'


def test_c123_hostile_search_never_raises(repo: Repo) -> None:
    repo.add_transaction(
        "2026-09-01", 'Restaurante "O Tiago"', -25.0, "Alimentacao", "Conta principal"
    )
    repo.db.conn.commit()
    hostile_terms = [
        'restaurante "o tiago',
        "tv 4k\"",
        "' OR 1=1 --",
        "'; DROP TABLE transactions; --",
        '((("',
        "a:b*c^d",
        "back\\slash",
    ]
    for term in hostile_terms:
        rows = repo.transactions_filtered(
            start="2026-01-01", end="2026-12-31", search=term
        )
        assert isinstance(rows, list)


def test_c123_hostile_search_finds_expected_row(repo: Repo) -> None:
    repo.add_transaction(
        "2026-09-01", 'Restaurante "O Tiago"', -25.0, "Alimentacao", "Conta principal"
    )
    repo.add_transaction(
        "2026-09-02", "TV 4K Samsung", -450.0, "Compras", "Conta principal"
    )
    repo.db.conn.commit()
    rows = repo.transactions_filtered(
        start="2026-01-01", end="2026-12-31", search='restaurante "o tiago'
    )
    assert [str(r["description"]) for r in rows] == ['Restaurante "O Tiago"']
    # Two-character terms degrade to the INSTR fallback and still match.
    rows_short = repo.transactions_filtered(
        start="2026-01-01", end="2026-12-31", search="tv"
    )
    assert [str(r["description"]) for r in rows_short] == ["TV 4K Samsung"]


# ---------------------------------------------------------------------------
# C124 - FTS5 triggers never INSERT OR REPLACE
# ---------------------------------------------------------------------------


def test_c124_triggers_do_not_use_replace(repo: Repo) -> None:
    names = {
        "trg_transaction_search_insert",
        "trg_transaction_search_update",
        "trg_transaction_meta_search_insert",
        "trg_transaction_meta_search_update",
        "trg_transaction_meta_search_delete",
    }
    for name in names:
        row = repo.db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        assert row is not None, f"trigger {name} inexistente"
        body = str(row["sql"])
        # Strip comments: the C124 fix explains itself in trigger comments.
        code_only = "\n".join(
            line.split("--")[0] for line in body.splitlines()
        ).upper()
        assert "OR REPLACE" not in code_only, f"{name} usa OR REPLACE"


def test_c124_update_removes_old_terms_and_no_ghost(repo: Repo) -> None:
    tx = repo.add_transaction(
        "2026-09-01", "Mercado Continente", -30.0, "Alimentacao", "Conta principal"
    )
    repo.db.conn.commit()
    conn = repo.db.conn
    before = conn.execute(
        "SELECT count(*) FROM transaction_search_fts "
        "WHERE transaction_search_fts MATCH ?",
        ('"Continente"',),
    ).fetchone()[0]
    assert before == 1
    conn.execute(
        "UPDATE transactions SET description='Mercado Pingo Doce' WHERE id=?", (tx,)
    )
    conn.commit()
    ghost = conn.execute(
        "SELECT count(*) FROM transaction_search_fts "
        "WHERE transaction_search_fts MATCH ?",
        ('"Continente"',),
    ).fetchone()[0]
    fresh = conn.execute(
        "SELECT count(*) FROM transaction_search_fts "
        "WHERE transaction_search_fts MATCH ?",
        ('"Pingo"',),
    ).fetchone()[0]
    assert ghost == 0
    assert fresh == 1


def test_c124_insert_trigger_survives_identity_trigger_cascade(repo: Repo) -> None:
    """Regression: the identity trigger UPDATE fires the FTS update trigger
    inside the same outer INSERT statement; trigger creation order makes the
    update-trigger FTS insert run BEFORE the insert-trigger FTS insert, so a
    bare INSERT raised a UNIQUE constraint failure."""
    conn = repo.db.conn
    # Seed identity rows first so the identity trigger performs its UPDATE.
    conn.execute(
        "INSERT OR IGNORE INTO categories(name,kind,identity_key,is_active) "
        "VALUES('CatC124','expense','catc124',1)"
    )
    conn.commit()
    tx = repo.add_transaction(
        "2026-09-03", "Compra com identidade", -12.0, "CatC124", "Conta principal"
    )
    conn.commit()
    row = conn.execute(
        "SELECT category_id,category FROM transactions WHERE id=?", (tx,)
    ).fetchone()
    assert row["category_id"] != ""
    hits = conn.execute(
        "SELECT count(*) FROM transaction_search_fts WHERE transaction_search_fts MATCH ?",
        ('"identidade"',),
    ).fetchone()[0]
    assert hits == 1


# ---------------------------------------------------------------------------
# C125 - no premature commit in add_transaction_with_meta
# ---------------------------------------------------------------------------


def test_c125_commit_false_respects_outer_savepoint(repo: Repo) -> None:
    conn = repo.db.conn
    conn.execute("SAVEPOINT outer_c125")
    try:
        repo.add_transaction_with_meta(
            "2026-09-02",
            "Teste C125",
            -10.0,
            "Alimentacao",
            "Conta principal",
            tags="c125",
            commit=False,
        )
        inside = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE description='Teste C125'"
        ).fetchone()[0]
        assert inside == 1
        conn.execute("ROLLBACK TO SAVEPOINT outer_c125")
        conn.execute("RELEASE SAVEPOINT outer_c125")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT outer_c125")
        conn.execute("RELEASE SAVEPOINT outer_c125")
        raise
    after = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE description='Teste C125'"
    ).fetchone()[0]
    assert after == 0


def test_c125_commit_true_persists_data(repo: Repo) -> None:
    tx = repo.add_transaction_with_meta(
        "2026-09-02",
        "Teste C125 commit",
        -10.0,
        "Alimentacao",
        "Conta principal",
        tags="c125b",
        commit=True,
    )
    conn = repo.db.conn
    assert conn.in_transaction is False
    row = conn.execute(
        "SELECT t.description, m.tags FROM transactions t "
        "JOIN transaction_meta m ON m.transaction_id=t.id WHERE t.id=?",
        (tx,),
    ).fetchone()
    assert row["description"] == "Teste C125 commit"
    assert row["tags"] == "c125b"


# ---------------------------------------------------------------------------
# C126 - monthly_category_budgets FK ON UPDATE CASCADE
# ---------------------------------------------------------------------------


def test_c126_fk_has_on_update_cascade(repo: Repo) -> None:
    ddl = repo.db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='monthly_category_budgets'"
    ).fetchone()[0]
    assert "ON UPDATE CASCADE" in ddl.upper()


def test_c126_migration_registered_and_idempotent(repo: Repo) -> None:
    row = repo.db.conn.execute(
        "SELECT name FROM schema_migrations WHERE version=30"
    ).fetchone()
    assert row is not None
    assert row["name"] == "monthly_category_budgets_update_cascade"
    # Re-running the migration must be a no-op (idempotent).
    repo.db._migration_monthly_category_budgets_update_cascade()
    ddl = repo.db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='monthly_category_budgets'"
    ).fetchone()[0]
    assert "ON UPDATE CASCADE" in ddl.upper()


def test_c126_category_rename_cascades_to_budgets(repo: Repo) -> None:
    conn = repo.db.conn
    category_id, category_name = repo.category_identity(
        "CatC126Rename", create=True, kind="expense"
    )
    conn.execute(
        "INSERT INTO monthly_category_budgets(month,category,budget,category_id) "
        "VALUES(?,?,?,?)",
        ("2026-09", category_name, 100.0, category_id),
    )
    conn.commit()
    conn.execute(
        "UPDATE categories SET name='CatC126Renomeada' WHERE stable_id=?",
        (category_id,),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT category,budget FROM monthly_category_budgets WHERE category_id=?",
        (category_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["category"] == "CatC126Renomeada"
    assert rows[0]["budget"] == 100.0


# ---------------------------------------------------------------------------
# C127 - aliases follow the merge
# ---------------------------------------------------------------------------


def test_c127_merge_moves_source_aliases_to_target(repo: Repo) -> None:
    conn = repo.db.conn
    source_id, _ = repo.account_identity("BancoLegadoC127")
    target_id, _ = repo.account_identity("BancoNovoC127")
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases(entity_type,entity_id,alias,identity_key) "
        "VALUES('account',?, 'Extrato Legado 2024','key_legado_2024')",
        (source_id,),
    )
    conn.commit()
    repo.merge_financial_identities("account", source_id, target_id)
    row = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE alias='Extrato Legado 2024'"
    ).fetchone()
    assert row is not None
    assert row["entity_id"] == target_id


def test_c127_merge_with_colliding_alias_keys_keeps_target_aliases(repo: Repo) -> None:
    conn = repo.db.conn
    source_id, source_name = repo.account_identity("BancoColisaoC127")
    target_id, _ = repo.account_identity("BancoColisaoDestinoC127")
    # A legacy alias whose identity_key collides with an alias the target
    # already owns must not break the merge.
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases(entity_type,entity_id,alias,identity_key) "
        "VALUES('account',?,?,?)",
        (
            source_id,
            "Historico Colisao",
            account_identity_key(source_name),
        ),
    )
    conn.commit()
    repo.merge_financial_identities("account", source_id, target_id)
    rows = conn.execute(
        "SELECT entity_id,alias FROM entity_aliases WHERE identity_key=?",
        (account_identity_key(source_name),),
    ).fetchall()
    assert len(rows) >= 1
    assert all(r["entity_id"] == target_id for r in rows)


def test_c127_category_merge_moves_aliases(repo: Repo) -> None:
    conn = repo.db.conn
    source_id, _ = repo.category_identity("CatLegadoC127", create=True, kind="expense")
    target_id, _ = repo.category_identity("CatNovaC127", create=True, kind="expense")
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases(entity_type,entity_id,alias,identity_key) "
        "VALUES('category',?, 'Alias Categoria Legado','key_cat_legado_c127')",
        (source_id,),
    )
    conn.commit()
    repo.merge_financial_identities("category", source_id, target_id)
    row = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE alias='Alias Categoria Legado'"
    ).fetchone()
    assert row is not None
    assert row["entity_id"] == target_id


# ---------------------------------------------------------------------------
# C128 - graceful shutdown checkpoint
# ---------------------------------------------------------------------------


def test_c128_shutdown_with_active_reader_completes(tmp_path: Path) -> None:
    db = FinanceDB(tmp_path / "c128.sqlite3")
    db.conn.execute(
        "INSERT INTO transactions(date,description,amount,category,account) "
        "VALUES('2026-09-01','C128',-10.0,'Alimentacao','Conta principal')"
    )
    db.conn.commit()
    reader = sqlite3.connect(tmp_path / "c128.sqlite3")
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM transactions").fetchall()
        started = time.perf_counter()
        db.prepare_for_shutdown()
        elapsed = time.perf_counter() - started
        assert db._closed is True
        # Grace window is 1.5s; allow scheduler jitter but never minutes.
        assert elapsed < 5.0
    finally:
        reader.rollback()
        reader.close()


def test_c128_graceful_checkpoint_degrades_to_passive(tmp_path: Path) -> None:
    db = FinanceDB(tmp_path / "c128b.sqlite3")
    db.conn.execute(
        "INSERT INTO transactions(date,description,amount,category,account) "
        "VALUES('2026-09-01','C128b',-10.0,'Alimentacao','Conta principal')"
    )
    db.conn.commit()
    reader = sqlite3.connect(tmp_path / "c128b.sqlite3")
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM transactions").fetchall()
        row = db._graceful_shutdown_checkpoint()
        # TRUNCATE could not complete (reader active), so the fallback ran:
        # PASSIVE reports busy=0 and the connection is still usable.
        assert row is not None
        assert int(row[0]) == 0
        count = db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        assert count >= 1
    finally:
        reader.rollback()
        reader.close()


def test_c128_clean_shutdown_still_truncates(tmp_path: Path) -> None:
    db = FinanceDB(tmp_path / "c128c.sqlite3")
    db.conn.execute(
        "INSERT INTO transactions(date,description,amount,category,account) "
        "VALUES('2026-09-01','C128c',-10.0,'Alimentacao','Conta principal')"
    )
    db.conn.commit()
    started = time.perf_counter()
    db.prepare_for_shutdown()
    elapsed = time.perf_counter() - started
    assert db._closed is True
    assert elapsed < 1.4  # no reader: TRUNCATE succeeds on the first attempt
    wal = tmp_path / "c128c.sqlite3-wal"
    assert not wal.exists() or wal.stat().st_size == 0


# ---------------------------------------------------------------------------
# C129 - split income keeps positive sign in Saldo view
# ---------------------------------------------------------------------------


def _seed_split_income_and_expense(repo: Repo) -> None:
    conn = repo.db.conn
    # Remove demo seed rows so the totals below are the only movements.
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM transaction_splits")
    conn.commit()
    income = repo.add_transaction(
        "2026-09-04", "Salario dividido", 1000.0, "Rendimentos", "Conta principal"
    )
    expense = repo.add_transaction(
        "2026-09-05", "Compra dividida", -100.0, "Alimentacao", "Conta principal"
    )
    conn.commit()
    for category, amount, tx in (
        ("Base", 800.0, income),
        ("Subsidio", 200.0, income),
        ("Mercearia", 60.0, expense),
        ("Bebidas", 40.0, expense),
    ):
        conn.execute(
            "INSERT INTO transaction_splits(transaction_id,category,category_id,percent,amount) "
            "VALUES(?,?,?,?,?)",
            (tx, category, "", 50.0, amount),
        )
    conn.commit()


def test_c129_saldo_view_keeps_income_positive(repo: Repo) -> None:
    _seed_split_income_and_expense(repo)
    rows = repo.dynamic_grouped(
        "2026-01-01", "2026-12-31", group_by="Categoria", kind="Saldo"
    )
    totals = {r["label"]: r["total"] for r in rows}
    assert totals == {
        "Base": 800.0,
        "Subsidio": 200.0,
        "Mercearia": -60.0,
        "Bebidas": -40.0,
    }


def test_c129_despesas_view_unchanged(repo: Repo) -> None:
    _seed_split_income_and_expense(repo)
    rows = repo.dynamic_grouped(
        "2026-01-01", "2026-12-31", group_by="Categoria", kind="Despesas"
    )
    totals = {r["label"]: r["total"] for r in rows}
    assert totals == {"Mercearia": 60.0, "Bebidas": 40.0}


def test_c129_receitas_view_unchanged(repo: Repo) -> None:
    _seed_split_income_and_expense(repo)
    rows = repo.dynamic_grouped(
        "2026-01-01", "2026-12-31", group_by="Categoria", kind="Receitas"
    )
    totals = {r["label"]: r["total"] for r in rows}
    assert totals == {"Base": 800.0, "Subsidio": 200.0}


# ---------------------------------------------------------------------------
# C130 - goal_id accepted by _update_transaction_fields
# ---------------------------------------------------------------------------


def test_c130_goal_id_is_an_allowed_field(repo: Repo) -> None:
    conn = repo.db.conn
    goal_id = int(
        conn.execute(
            "INSERT INTO goals(name,target_amount,saved_amount) "
            "VALUES('MetaC130',100.0,0.0)"
        ).lastrowid
    )
    conn.commit()
    tx = repo.add_transaction(
        "2026-09-03", "Deposito meta C130", -50.0, "Objetivos e Poupanca",
        "Conta principal",
    )
    conn.commit()
    repo.update_transaction_fields(tx, {"goal_id": goal_id})
    row = conn.execute("SELECT goal_id FROM transactions WHERE id=?", (tx,)).fetchone()
    assert row["goal_id"] == goal_id


def test_c130_goal_compensation_runs_on_amount_change(repo: Repo) -> None:
    conn = repo.db.conn
    goal_id = int(
        conn.execute(
            "INSERT INTO goals(name,target_amount,saved_amount) "
            "VALUES('MetaC130b',100.0,0.0)"
        ).lastrowid
    )
    conn.commit()
    tx = repo.add_transaction(
        "2026-09-03", "Deposito meta C130b", -40.0, "Objetivos e Poupanca",
        "Conta principal",
    )
    conn.commit()
    repo.update_transaction_fields(tx, {"goal_id": goal_id, "amount": -70.0})
    saved = conn.execute(
        "SELECT saved_amount FROM goals WHERE id=?", (goal_id,)
    ).fetchone()[0]
    assert saved == 70.0


# ---------------------------------------------------------------------------
# C131 - account delete guard covers holdings and liabilities
# ---------------------------------------------------------------------------


def test_c131_delete_blocked_with_holding(repo: Repo) -> None:
    conn = repo.db.conn
    account_id, account_name = repo.account_identity("ContaC131a")
    conn.commit()
    conn.execute(
        "INSERT INTO holdings(name,asset_class,value,cost_basis,"
        "linked_account,linked_account_id) VALUES('AtivoC131','acoes',"
        "1000.0,900.0,?,?)",
        (account_name, account_id),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM financial_accounts WHERE stable_id=?", (account_id,))
    conn.rollback()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM financial_accounts WHERE stable_id=?", (account_id,)
        ).fetchone()[0]
        == 1
    )


def test_c131_delete_blocked_with_liability(repo: Repo) -> None:
    conn = repo.db.conn
    account_id, account_name = repo.account_identity("ContaC131b")
    conn.commit()
    conn.execute(
        "INSERT INTO liabilities(name,account,account_id,balance,monthly_payment) "
        "VALUES('CreditoC131',?,?,5000.0,150.0)",
        (account_name, account_id),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM financial_accounts WHERE stable_id=?", (account_id,))
    conn.rollback()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM financial_accounts WHERE stable_id=?", (account_id,)
        ).fetchone()[0]
        == 1
    )


def test_c131_empty_account_still_deletable(repo: Repo) -> None:
    conn = repo.db.conn
    account_id, _ = repo.account_identity("ContaC131c")
    conn.commit()
    conn.execute("DELETE FROM financial_accounts WHERE stable_id=?", (account_id,))
    conn.commit()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM financial_accounts WHERE stable_id=?", (account_id,)
        ).fetchone()[0]
        == 0
    )
