#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for audit batch 15:

- BUG-PERS-004 (P1): ``Repo.clear_all_transactions`` deletes the whole
  movement ledger but never compensated ``goals.saved_amount``, leaving
  orphan goal balances after a bulk clear.  The compensation must follow
  the exact sign convention of the (batch 10 fixed)
  ``trg_transactions_cascade_transfer_leg`` trigger and
  ``delete_transactions``: ``saved_amount + tx_amount`` (a goal deposit
  movement has a negative amount), floored at ``MAX(0.0, ...)``.  Transfer
  legs are excluded — they never contributed to ``saved_amount`` and are
  compensated exclusively by the cascade trigger (BUG-DB-003 contract);
  compensating them here too would double-count.  The change_journal is
  intentionally NOT compensated: ``clear_all_transactions`` marks every
  available journal entry as ``undone`` in the same atomic savepoint, so no
  later undo can resurrect a movement whose contribution was already
  reverted.
- BUG-SEC-02 (P1): ``run_tamper_recovery_dialog`` crashed the startup
  recovery flow with ``AttributeError``.  Root cause chain:
  ``_active_theme_or_default`` referenced the nonexistent
  ``SecuritySettingsService`` class and its nonexistent ``current_theme``
  attribute, so it always returned ``None``; the caller then passed that
  ``None`` into ``apply_message_box_theme``, which raised
  ``AttributeError: 'NoneType' object has no attribute 'message_surface'``
  — masked by a bare ``except: pass`` (violating the zero except:pass
  rule).  The fix resolves a real ``Theme`` from the live application
  window (falling back to a plain ``Theme``), only applies it when
  non-None, logs any styling failure with ``log_exception`` and returns a
  strict ``bool`` from ``dialog._confirmed``.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os_environ_qt_offscreen = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from database.engine import FinanceDB  # noqa: E402
from database.repo import Repo  # noqa: E402
from services.startup_security import (  # noqa: E402
    ProfileSecurityState,
    StartupSecurityService,
)
import ui.startup_security as ui_sec  # noqa: E402
from ui.startup_security import (  # noqa: E402
    TamperRecoveryDialog,
    run_tamper_recovery_dialog,
)


@pytest.fixture(scope="module")
def qapp():
    """Ensure a QApplication exists for widget testing."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # The global conftest sets FINANCAS_PESSOAIS_DEMO_DATA=1 for the visual/
    # analytics fixtures; batch 15 needs an empty database with exact row
    # counts, so demo seeding is disabled for this module only.
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch15.sqlite3")
    yield Repo(db)
    try:
        db.conn.close()
    except sqlite3.Error:
        pass


def _goal_saved_amount(repo: Repo, goal_id: int) -> float:
    row = repo.q("SELECT saved_amount FROM goals WHERE id=?", (int(goal_id),))
    assert row, f"goal {goal_id} missing"
    return float(row[0]["saved_amount"])


def _link_goal_movement(
    repo: Repo, tx_id: int, goal_id: int, amount: float
) -> None:
    """Link an existing movement to a goal applying the exact codebase
    convention (adjust_goal_with_transaction): a deposit tx has a negative
    amount and RAISES saved_amount (``saved_amount - tx_amount``), a
    withdrawal tx has a positive amount and LOWERS saved_amount.  Deleting
    the movement must invert it (``saved_amount + tx_amount``)."""
    repo.db.conn.execute(
        "UPDATE transactions SET goal_id=? WHERE id=?",
        (int(goal_id), int(tx_id)),
    )
    repo.db.conn.execute(
        "UPDATE goals SET saved_amount = MAX(0.0, saved_amount - ?) WHERE id=?",
        (float(amount), int(goal_id)),
    )
    repo.db.conn.commit()


# ---------------------------------------------------------------------------
# BUG-PERS-004
# ---------------------------------------------------------------------------


class TestBugPERS004ClearAllCompensatesGoals:
    def test_clear_all_resets_goal_deposit_contribution(
        self, repo: Repo
    ) -> None:
        """A goal deposit movement (-250) raised saved_amount to 250; the
        bulk clear must remove its contribution (back to 0.0)."""
        goal_id = repo.add_goal("Fundo Viagem", 1000.0, 0.0, 0.0, "2026-12-31")
        repo.adjust_goal_with_transaction(
            goal_id, 250.0, deposit=True, transaction_date="2026-02-01"
        )
        assert _goal_saved_amount(repo, goal_id) == 250.0

        removed = repo.clear_all_transactions()

        assert removed == 1
        assert repo.scalar("SELECT COUNT(*) FROM transactions") == 0
        assert _goal_saved_amount(repo, goal_id) == 0.0

    def test_clear_all_compensates_multiple_goals_with_exact_values(
        self, repo: Repo
    ) -> None:
        """Several goals with several linked movements each: the compensation
        is the exact arithmetic sum, not a blanket ``SET 0``.  Goal B starts
        with a manual 120.0; its movements (a +80 deposit and a -30
        withdrawal) net out to zero compensation, so 120.0 must survive."""
        goal_a = repo.add_goal("Meta A", 500.0, 0.0, 0.0, "2026-12-31")
        goal_b = repo.add_goal("Meta B", 800.0, 120.0, 0.0, "2026-12-31")
        repo.adjust_goal_with_transaction(
            goal_a, 200.0, deposit=True, transaction_date="2026-02-01"
        )
        repo.adjust_goal_with_transaction(
            goal_a, 50.0, deposit=True, transaction_date="2026-02-02"
        )
        repo.adjust_goal_with_transaction(
            goal_b, 80.0, deposit=True, transaction_date="2026-02-03"
        )
        repo.adjust_goal_with_transaction(
            goal_b, 30.0, deposit=False, transaction_date="2026-02-04"
        )
        assert _goal_saved_amount(repo, goal_a) == 250.0
        assert _goal_saved_amount(repo, goal_b) == 120.0 + 80.0 - 30.0

        repo.clear_all_transactions()

        assert _goal_saved_amount(repo, goal_a) == 0.0
        # The goal B movements net to zero (80 deposit - 30 withdrawal), so
        # only the manual 120.0 seed remains — a blanket SET 0 would destroy
        # it (exact-value assertion, per the TDD house rules).
        assert _goal_saved_amount(repo, goal_b) == 120.0

    def test_clear_all_never_touches_unlinked_goals(self, repo: Repo) -> None:
        """A goal without linked movements keeps its manual saved_amount."""
        linked = repo.add_goal("Com movimentos", 500.0, 0.0, 0.0, "2026-12-31")
        manual = repo.add_goal("Manual", 900.0, 415.5, 0.0, "2026-12-31")
        repo.adjust_goal_with_transaction(
            linked, 125.0, deposit=True, transaction_date="2026-02-01"
        )

        repo.clear_all_transactions()

        assert _goal_saved_amount(repo, linked) == 0.0
        assert _goal_saved_amount(repo, manual) == 415.5

    def test_clear_all_excludes_transfer_legs_from_compensation(
        self, repo: Repo
    ) -> None:
        """A transfer leg linked to a goal must NOT be compensated by the
        bulk clear: the cascade trigger compensates transfer legs
        exclusively (BUG-DB-003 contract).  Compensating here too would
        double-count the same contribution."""
        goal_id = repo.add_goal("Meta transfer", 500.0, 100.0, 0.0, "2026-12-31")
        debit_id, _credit_id = repo.add_internal_transfer(
            "2026-02-05", 25.0, "Conta A", "Conta B"
        )
        _link_goal_movement(repo, debit_id, goal_id, -25.0)
        assert _goal_saved_amount(repo, goal_id) == 125.0

        repo.clear_all_transactions()

        # The transfer leg contributed +25 on linking; it is compensated by
        # the cascade trigger during the bulk delete, so the final value is
        # exactly the pre-link balance 100.0 — not 100+25 (double count).
        assert _goal_saved_amount(repo, goal_id) == 100.0

    def test_clear_all_marks_available_journal_entries_undone(
        self, repo: Repo
    ) -> None:
        """The undo journal cannot resurrect cleared movements: every
        available entry is marked undone inside the same savepoint, so no
        extra goal compensation is needed for the journal path."""
        goal_id = repo.add_goal("Meta journal", 500.0, 0.0, 0.0, "2026-12-31")
        repo.adjust_goal_with_transaction(
            goal_id, 60.0, deposit=True, transaction_date="2026-02-06"
        )
        repo.db.conn.execute(
            "INSERT INTO change_journal(action_type,label,payload_json) "
            "VALUES(?,?,?)",
            ("edit", "Alteração anterior", '{"undo_kind": "delete_created"}'),
        )
        repo.db.conn.commit()

        repo.clear_all_transactions()

        assert (
            repo.scalar(
                "SELECT COUNT(*) FROM change_journal WHERE status='available'"
            )
            == 0
        )
        assert (
            repo.scalar(
                "SELECT COUNT(*) FROM change_journal WHERE status='undone'"
            )
            == 1
        )
        assert _goal_saved_amount(repo, goal_id) == 0.0

    def test_clear_all_failure_rolls_back_goal_compensation(
        self, repo: Repo
    ) -> None:
        """A failed bulk clear must restore both the movements and the goal
        balances (single atomic savepoint)."""
        goal_id = repo.add_goal("Meta rollback", 500.0, 0.0, 0.0, "2026-12-31")
        repo.adjust_goal_with_transaction(
            goal_id, 90.0, deposit=True, transaction_date="2026-02-07"
        )
        assert _goal_saved_amount(repo, goal_id) == 90.0
        repo.db.conn.executescript(
            """
            CREATE TRIGGER reject_complete_ledger_clear
            BEFORE DELETE ON transactions
            BEGIN
                SELECT RAISE(ABORT, 'simulated clear failure');
            END;
            """
        )
        repo.db.conn.commit()

        with pytest.raises(Exception, match="simulated clear failure"):
            repo.clear_all_transactions()

        assert repo.scalar("SELECT COUNT(*) FROM transactions") == 1
        assert _goal_saved_amount(repo, goal_id) == 90.0


# ---------------------------------------------------------------------------
# BUG-SEC-02
# ---------------------------------------------------------------------------


class TestBugSEC02TamperRecoveryDialog:
    def test_active_theme_or_default_never_returns_none(self, qapp) -> None:
        """With no themed top-level window available the helper must fall
        back to a real Theme instance — never None (the crash source)."""
        theme = ui_sec._active_theme_or_default()
        from ui.core.theme import Theme

        assert isinstance(theme, Theme)

    def test_active_theme_or_default_prefers_visible_themed_window(
        self, qapp
    ) -> None:
        """A visible top-level window carrying a ``theme`` attribute wins."""
        from PySide6.QtWidgets import QWidget

        from ui.core.theme import Theme

        window = QWidget()
        window.theme = Theme(dark=True)  # type: ignore[attr-defined]
        window.show()
        try:
            theme = ui_sec._active_theme_or_default()
            assert isinstance(theme, Theme)
            assert theme.dark is True
        finally:
            window.close()

    def test_run_tamper_recovery_dialog_applies_theme_without_crash(
        self, qapp
    ) -> None:
        """The real recovery flow must reach ``dialog.exec()`` with a valid
        theme applied (no AttributeError from apply_message_box_theme)."""
        applied: list[object] = []

        def _fake_apply(dialog, theme, *, forced=False):
            applied.append(theme)

        with patch(
            "ui.core.theme.apply_message_box_theme", _fake_apply
        ):
            with patch.object(QDialog, "exec", lambda self: None):
                result = run_tamper_recovery_dialog(MagicMock())
        assert result is False
        assert len(applied) == 1
        from ui.core.theme import Theme

        assert isinstance(applied[0], Theme)

    def test_run_tamper_recovery_dialog_full_recovery_flow_succeeds(
        self, qapp, tmp_path: Path
    ) -> None:
        """End-to-end main.py sequence: TAMPERED profile -> recovery dialog
        confirms -> acknowledge clears the tombstone -> NEW state.  The
        dialog must never crash before the user can type the phrase."""
        service = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
        service.configure_first_run("pin", "1357902468")
        connection = sqlite3.connect(str(service._database_file()))
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS settings "
                "(key TEXT PRIMARY KEY, value TEXT)"
            )
        finally:
            connection.close()
        service.refresh_tamper_tombstone()
        service.envelope_path.unlink(missing_ok=True)
        service.marker_path.unlink(missing_ok=True)
        assert service.inspect_profile() == ProfileSecurityState.TAMPERED

        def _user_confirms(self: QDialog) -> None:
            self.input_box.setText("ASSUMO A PERDA DE ACESSO AOS BACKUPS")
            self._on_confirm()

        with patch.object(QDialog, "exec", _user_confirms):
            confirmed = run_tamper_recovery_dialog(service)

        assert confirmed is True
        assert service.inspect_profile() in {
            ProfileSecurityState.NEW,
            ProfileSecurityState.LEGACY_UNPROTECTED,
        }

    def test_run_tamper_recovery_dialog_declined_returns_strict_false(
        self, qapp, tmp_path: Path
    ) -> None:
        """Cancelling must return a strict ``False`` (not a MagicMock)."""
        service = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
        with patch.object(QDialog, "exec", lambda self: None):
            result = run_tamper_recovery_dialog(service)
        assert result is False

    def test_run_tamper_recovery_dialog_survives_theme_failure(
        self, qapp
    ) -> None:
        """If styling still fails for any reason the dialog must run and the
        failure must be logged (never silently swallowed)."""
        logged: list[tuple[str, BaseException]] = []

        def _boom(dialog, theme, *, forced=False):
            raise AttributeError("simulated styling failure")

        def _fake_log(context, exc=None):
            logged.append(
                (
                    context,
                    exc if isinstance(exc, BaseException) else Exception(str(exc)),
                )
            )

        with patch(
            "ui.core.theme.apply_message_box_theme", _boom
        ):
            with patch(
                "utils.app_logging.log_exception", _fake_log
            ):
                with patch.object(QDialog, "exec", lambda self: None):
                    result = run_tamper_recovery_dialog(MagicMock())
        assert result is False
        assert len(logged) == 1
        assert "theme" in logged[0][0].lower()

    def test_dialog_no_longer_calls_nonexistent_theme_source(self, qapp) -> None:
        """Source guard: the phantom ``SecuritySettingsService`` import and
        its nonexistent ``current_theme`` attribute are gone."""
        source = (ROOT / "ui" / "startup_security.py").read_text(
            encoding="utf-8"
        )
        # The phantom class import and its nonexistent attribute must be
        # gone from executable code (mentions inside BUG-SEC-02 docstrings
        # are documentation, not references).
        executable_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        code_lines = [
            line
            for line in executable_lines
            if not (
                line.startswith('"""')
                or line.startswith('"')
                or ("SecuritySettingsService" in line and "nonexistent" in line)
                or ("current_theme" in line and "nonexistent" in line)
            )
        ]
        offenders = [
            line
            for line in code_lines
            if "SecuritySettingsService" in line or "current_theme" in line
        ]
        assert offenders == [], f"phantom theme references remain: {offenders}"

    def test_dialog_construction_with_real_service_and_state_none(
        self, qapp, tmp_path: Path
    ) -> None:
        """Constructing the dialog with state=None (the production path:
        inspect_profile result is only known to main.py) must not crash and
        must resolve the explanation from the live service."""
        service = StartupSecurityService(database_file=tmp_path / "db.sqlite3")
        service.configure_first_run("pin", "1357902468")
        dialog = TamperRecoveryDialog(service, state=None)
        assert dialog._phrase == "ASSUMO A PERDA DE ACESSO AOS BACKUPS"
        title, desc = dialog._explanation_for_state()
        assert title
        assert desc
        dialog.close()
