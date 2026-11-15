#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for audit batch 11 (parser correctness & atomic mutations):

- BUG-SEC-001 (P1): OFX credit-card statements must be detected from the OFX
  statement container tags (``<CCSTMTRS>``/``<CCSTMTTRNRS>``/``<CCACCTFROM>``)
  and the ``CsvImportData.is_credit_card`` flag must propagate through
  ``ImportService.read_file`` instead of being hardcoded to ``False``.
  Multiline ``<MEMO>`` content must be fully captured (closed SGML tag) instead
  of being truncated to the first line.
- BUG-DB-005 (P1): ``_update_transaction_fields`` must mutate ``transactions``,
  ``transaction_splits`` and ``goals`` inside one SAVEPOINT so a failure after
  the first write leaves no partial state behind.
- BUG-PERS-003 (P1): ``undo_latest`` must reject a ``sqlite_sequence`` collision
  on dependent tables without overwriting live rows. Once the conflicting row
  is explicitly removed, the original snapshot remains available for retry.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB  # noqa: E402
from database.repo import Repo  # noqa: E402
from services.bank_parsers import OFXParser  # noqa: E402
from services.change_journal import ChangeJournalService  # noqa: E402
from services.imports import ImportService  # noqa: E402


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    # The global conftest sets FINANCAS_PESSOAIS_DEMO_DATA=1 for the visual/
    # analytics fixtures; batch 11 needs an empty database with exact row
    # counts, so demo seeding is disabled for this module only.
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch11.sqlite3")
    yield Repo(db)
    try:
        db.conn.close()
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# BUG-SEC-001 (a): OFX credit-card detection
# ---------------------------------------------------------------------------


class TestBugSEC001OFXCreditCard:
    def test_ccstmtrs_sets_is_credit_card_true(self) -> None:
        content = (
            "OFXHEADER:100\n"
            "DATA:OFXSGML\n"
            "VERSION:102\n"
            "\n"
            "<OFX>\n"
            "<CREDITCARDMSGSRSV1>\n"
            "<CCSTMTTRNRS>\n"
            "<CCSTMTRS>\n"
            "<CURDEF>EUR\n"
            "<CCACCTFROM>\n"
            "<CCACCTID>1234567890123456\n"
            "</CCACCTFROM>\n"
            "<BANKTRANLIST>\n"
            "<STMTTRN>\n"
            "<TRNTYPE>POS\n"
            "<DTPOSTED>20260105120000\n"
            "<TRNAMT>-42.50\n"
            "<FITID>CC-001\n"
            "<NAME>MERCADONA\n"
            "<MEMO>COMPRA CARTAO\n"
            "</STMTTRN>\n"
            "</BANKTRANLIST>\n"
            "</CCSTMTRS>\n"
            "</CCSTMTTRNRS>\n"
            "</CREDITCARDMSGSRSV1>\n"
            "</OFX>\n"
        )
        parsed = OFXParser.parse(content)
        assert parsed.is_credit_card is True

    def test_ccacctfrom_without_ccstmtrs_sets_is_credit_card_true(self) -> None:
        # Non-standard exports that carry <CCACCTFROM> inside a <STMTRS>
        # container still describe a credit card account.
        content = (
            "<OFX>\n"
            "<BANKMSGSRSV1>\n"
            "<STMTTRNRS>\n"
            "<STMTRS>\n"
            "<CCACCTFROM>\n"
            "<CCACCTID>9999\n"
            "</CCACCTFROM>\n"
            "<BANKTRANLIST>\n"
            "<STMTTRN>\n"
            "<TRNTYPE>DEBIT\n"
            "<DTPOSTED>20260106120000\n"
            "<TRNAMT>-10.00\n"
            "<FITID>CC-002\n"
            "<NAME>LOJA\n"
            "</STMTTRN>\n"
            "</BANKTRANLIST>\n"
            "</STMTRS>\n"
            "</STMTTRNRS>\n"
            "</BANKMSGSRSV1>\n"
            "</OFX>\n"
        )
        parsed = OFXParser.parse(content)
        assert parsed.is_credit_card is True

    def test_plain_bank_statement_keeps_is_credit_card_false(self) -> None:
        content = (
            "<OFX>\n"
            "<BANKMSGSRSV1>\n"
            "<STMTTRNRS>\n"
            "<STMTRS>\n"
            "<BANKACCTFROM>\n"
            "<BANKID>0035\n"
            "<ACCTID>003501234567\n"
            "<ACCTTYPE>CHECKING\n"
            "</BANKACCTFROM>\n"
            "<BANKTRANLIST>\n"
            "<STMTTRN>\n"
            "<TRNTYPE>DEBIT\n"
            "<DTPOSTED>20260105120000\n"
            "<TRNAMT>-42.50\n"
            "<FITID>TX-001\n"
            "<NAME>MERCADONA\n"
            "</STMTTRN>\n"
            "</BANKTRANLIST>\n"
            "</STMTRS>\n"
            "</STMTTRNRS>\n"
            "</BANKMSGSRSV1>\n"
            "</OFX>\n"
        )
        parsed = OFXParser.parse(content)
        assert parsed.is_credit_card is False

    def test_read_file_propagates_credit_card_flag(self, tmp_path: Path) -> None:
        ofx_path = tmp_path / "cartao.ofx"
        ofx_path.write_text(
            "<OFX>\n"
            "<CREDITCARDMSGSRSV1>\n"
            "<CCSTMTTRNRS>\n"
            "<CCSTMTRS>\n"
            "<CCACCTFROM>\n"
            "<CCACCTID>1234\n"
            "</CCACCTFROM>\n"
            "<BANKTRANLIST>\n"
            "<STMTTRN>\n"
            "<TRNTYPE>POS\n"
            "<DTPOSTED>20260105120000\n"
            "<TRNAMT>-42.50\n"
            "<FITID>CC-003\n"
            "<NAME>MERCADONA\n"
            "</STMTTRN>\n"
            "</BANKTRANLIST>\n"
            "</CCSTMTRS>\n"
            "</CCSTMTTRNRS>\n"
            "</CREDITCARDMSGSRSV1>\n"
            "</OFX>\n",
            encoding="ascii",
        )
        data = ImportService.read_file(ofx_path)
        # A07 separates account metadata from the deliberate sign transform.
        assert data.is_credit_card is True
        assert data.invert_signs is False

    def test_read_file_keeps_bank_flag_false(self, tmp_path: Path) -> None:
        ofx_path = tmp_path / "banco.ofx"
        ofx_path.write_text(
            "<OFX>\n"
            "<BANKMSGSRSV1>\n"
            "<STMTTRNRS>\n"
            "<STMTRS>\n"
            "<BANKACCTFROM>\n"
            "<ACCTID>003501234567\n"
            "</BANKACCTFROM>\n"
            "<BANKTRANLIST>\n"
            "<STMTTRN>\n"
            "<TRNTYPE>DEBIT\n"
            "<DTPOSTED>20260105120000\n"
            "<TRNAMT>-42.50\n"
            "<FITID>TX-002\n"
            "<NAME>MERCADONA\n"
            "</STMTTRN>\n"
            "</BANKTRANLIST>\n"
            "</STMTRS>\n"
            "</STMTTRNRS>\n"
            "</BANKMSGSRSV1>\n"
            "</OFX>\n",
            encoding="ascii",
        )
        data = ImportService.read_file(ofx_path)
        assert data.is_credit_card is False


# ---------------------------------------------------------------------------
# BUG-SEC-001 (b): multiline <MEMO> truncation
# ---------------------------------------------------------------------------


class TestBugSEC001MultilineMemo:
    def test_closed_multiline_memo_is_fully_captured(self) -> None:
        content = (
            "<OFX>\n"
            "<BANKTRANLIST>\n"
            "<STMTTRN>\n"
            "<TRNTYPE>DEBIT\n"
            "<DTPOSTED>20260105120000\n"
            "<TRNAMT>-42.50\n"
            "<FITID>TX-ML-1\n"
            "<NAME>MERCADONA\n"
            "<MEMO>Compra no supermercado\n"
            "linha dois da nota\n"
            "linha tres da nota\n"
            "</MEMO>\n"
            "</STMTTRN>\n"
            "</BANKTRANLIST>\n"
            "</OFX>\n"
        )
        parsed = OFXParser.parse(content)
        assert len(parsed.rows) == 1
        memo_full = "Compra no supermercado\nlinha dois da nota\nlinha tres da nota"
        assert parsed.rows[0]["Descrição"].endswith(memo_full)

    def test_open_multiline_memo_keeps_first_line_semantics(self) -> None:
        # SGML open-leaf tags (no closing tag) keep reading the first line;
        # the next tag must terminate the value exactly as before.
        content = (
            "<OFX>\n"
            "<BANKTRANLIST>\n"
            "<STMTTRN>\n"
            "<TRNTYPE>DEBIT\n"
            "<DTPOSTED>20260105120000\n"
            "<TRNAMT>-42.50\n"
            "<FITID>TX-ML-2\n"
            "<NAME>MERCADONA\n"
            "<MEMO>Primeira linha\n"
            "<CATEGORY>Alimentação\n"
            "</STMTTRN>\n"
            "</BANKTRANLIST>\n"
            "</OFX>\n"
        )
        parsed = OFXParser.parse(content)
        assert len(parsed.rows) == 1
        assert parsed.rows[0]["Descrição"].endswith("Primeira linha")

    def test_single_line_memo_unchanged(self) -> None:
        content = (
            "<OFX>\n"
            "<BANKTRANLIST>\n"
            "<STMTTRN>\n"
            "<TRNTYPE>DEBIT\n"
            "<DTPOSTED>20260105120000\n"
            "<TRNAMT>-42.50\n"
            "<FITID>TX-ML-3\n"
            "<NAME>MERCADONA\n"
            "<MEMO>COMPRA CARTAO\n"
            "</STMTTRN>\n"
            "</BANKTRANLIST>\n"
            "</OFX>\n"
        )
        parsed = OFXParser.parse(content)
        assert parsed.rows[0]["Descrição"] == "MERCADONA · COMPRA CARTAO"


# ---------------------------------------------------------------------------
# BUG-DB-005: atomic SAVEPOINT in _update_transaction_fields
# ---------------------------------------------------------------------------


class TestBugDB005AtomicSavepoint:
    def test_failed_goal_update_leaves_no_partial_state(
        self, repo: Repo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tx_id = repo.add_transaction(
            "2026-02-01",
            "Compra com meta",
            -50.0,
            "Alimentação",
            "Conta principal",
        )
        goal_id = repo.db.conn.execute(
            "INSERT INTO goals(name, target_amount, saved_amount) "
            "VALUES(?,?,?)",
            ("Fundo de emergência", 1000.0, 0.0),
        ).lastrowid
        repo.db.conn.commit()

        # Attach the movement to the goal via a partial update.
        repo.update_transaction_fields(tx_id, {"goal_id": goal_id})
        saved_before = float(
            repo.db.conn.execute(
                "SELECT saved_amount FROM goals WHERE id=?", (goal_id,)
            ).fetchone()[0]
        )
        assert saved_before == 50.0

        # Force the goal UPDATE inside _update_transaction_fields to fail by
        # intercepting connection.execute (sqlite3.Connection attributes are
        # read-only, so a subclass proxy is used instead of monkeypatch).
        class _FailingGoalUpdateConnection:
            def __init__(self, inner: sqlite3.Connection) -> None:
                self._inner = inner

            def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
                stripped = " ".join(str(sql).split())
                if stripped.startswith("UPDATE goals"):
                    raise sqlite3.OperationalError(
                        "goals table unavailable (simulated)"
                    )
                return self._inner.execute(sql, *args, **kwargs)

            def __getattr__(self, item: str) -> Any:
                return getattr(self._inner, item)

        real_conn = repo.db.conn
        repo.db.conn = _FailingGoalUpdateConnection(real_conn)  # type: ignore[assignment]
        try:
            with pytest.raises(sqlite3.OperationalError):
                repo.update_transaction_fields(
                    tx_id, {"amount": -80.0, "goal_id": goal_id}
                )
        finally:
            repo.db.conn = real_conn  # type: ignore[assignment]

        # No partial state may survive: the movement keeps its original
        # amount and the goal keeps its original saved_amount.
        row = real_conn.execute(
            "SELECT amount, kind FROM transactions WHERE id=?", (tx_id,)
        ).fetchone()
        assert row is not None
        assert float(row["amount"]) == -50.0
        assert str(row["kind"]) == "expense"
        saved_after = float(
            real_conn.execute(
                "SELECT saved_amount FROM goals WHERE id=?", (goal_id,)
            ).fetchone()[0]
        )
        assert saved_after == 50.0
        # The split maintenance writes must also be rolled back (there were
        # none before the failed call).
        assert (
            real_conn.execute(
                "SELECT COUNT(*) FROM transaction_splits WHERE transaction_id=?",
                (tx_id,),
            ).fetchone()[0]
            == 0
        )

    @staticmethod
    def _execute_dropping_goal_update(
        conn: sqlite3.Connection, sql: str, *args: Any, **kwargs: Any
    ) -> sqlite3.Cursor:
        """Legacy helper kept for reference; the proxy class above is used."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# BUG-PERS-003: undo must preserve live rows after dependent-table ID reuse
# ---------------------------------------------------------------------------


class TestBugPERS003UndoIdCollision:
    def test_undo_after_sequence_reset_rejects_collision_without_data_loss(
        self, tmp_path: Path
    ) -> None:
        db = FinanceDB(tmp_path / "pers003.sqlite3")
        try:
            repo = Repo(db)
            journal = ChangeJournalService(
                repo,
                attachments_directory=tmp_path / "anexos",
                journal_storage=tmp_path / "historico",
            )

            tx_id = repo.add_transaction(
                "2026-02-01",
                "Movimento com anexo",
                -12.5,
                "Alimentação",
                "Conta principal",
            )
            attachment = tmp_path / "anexos" / str(tx_id) / "fatura.txt"
            attachment.parent.mkdir(parents=True)
            attachment.write_text("documento", encoding="utf-8")
            db.conn.execute(
                "INSERT INTO transaction_attachments("
                "transaction_id,file_name,stored_name,stored_path) "
                "VALUES(?,?,?,?)",
                (tx_id, "fatura.txt", "fatura.txt", str(attachment)),
            )
            db.conn.commit()

            deleted, preserved = journal.delete_transactions(
                [tx_id], label="Eliminar movimento"
            )
            assert (deleted, preserved) == (1, 1)
            assert not attachment.exists()

            # Simulate the production failure mode: the AUTOINCREMENT sequence
            # of a dependent table was reset (e.g. legacy migration or backup
            # restore), so a new attachment row re-occupies the deleted id.
            # A second live transaction keeps the FK (transaction_id ->
            # transactions.id) satisfiable while creating the colliding row.
            other_tx_id = repo.add_transaction(
                "2026-02-02",
                "Movimento de conflito",
                -5.0,
                "Alimentação",
                "Conta principal",
            )
            db.conn.execute(
                "DELETE FROM sqlite_sequence WHERE name='transaction_attachments'"
            )
            db.conn.execute(
                "INSERT INTO transaction_attachments("
                "transaction_id,file_name,stored_name,stored_path) "
                "VALUES(?,?,?,?)",
                (other_tx_id, "novo.txt", "novo.txt", str(attachment)),
            )
            db.conn.commit()

            before = [dict(row) for row in db.conn.execute(
                "SELECT * FROM transaction_attachments ORDER BY id"
            )]
            with pytest.raises(RuntimeError, match="anexo.*identificador"):
                journal.undo_latest()
            after = [dict(row) for row in db.conn.execute(
                "SELECT * FROM transaction_attachments ORDER BY id"
            )]
            assert after == before
            assert not attachment.exists()
            assert repo.transaction_by_id(tx_id) is None
            assert repo.transaction_by_id(other_tx_id) is not None

            # Failure must not consume the undo entry. Resolve only the
            # conflicting metadata row, then retry the preserved snapshot.
            db.conn.execute(
                "DELETE FROM transaction_attachments WHERE transaction_id=?",
                (other_tx_id,),
            )
            db.conn.commit()
            assert journal.undo_latest() is not None
            restored = db.conn.execute(
                "SELECT file_name FROM transaction_attachments WHERE transaction_id=?",
                (tx_id,),
            ).fetchall()
            assert len(restored) == 1
            assert str(restored[0]["file_name"]) == "fatura.txt"
            assert attachment.read_text(encoding="utf-8") == "documento"
            assert repo.transaction_by_id(other_tx_id) is not None
        finally:
            try:
                db.conn.close()
            except sqlite3.Error:
                pass
