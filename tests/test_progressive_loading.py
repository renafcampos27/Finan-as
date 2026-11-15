from __future__ import annotations

import os
from datetime import date

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


def _wait_for_movements(page, qapp: QApplication) -> None:
    for _ in range(160):
        qapp.processEvents()
        loader = getattr(page, "_loader_task", None)
        if (
            loader is not None
            and not loader.is_running()
            and not getattr(page, "_pending_load", False)
        ):
            qapp.processEvents()
            return
        QTest.qWait(20)
    raise AssertionError("Movements background load did not finish")


def _wait_for_safe_close(window, qapp: QApplication) -> None:
    """Allow cooperative document cancellation to finish before DB teardown."""

    window.close()
    for _ in range(250):
        qapp.processEvents()
        if bool(getattr(window, "_database_closed", False)):
            return
        QTest.qWait(20)
    raise AssertionError("Application background tasks did not close safely")


def test_large_lists_load_progressively_without_losing_totals(tmp_path, monkeypatch):
    import database.repo.base as repo_base
    import services.documents as document_service_module
    import utils.app_logging as app_logging

    database_path = tmp_path / "progressive.sqlite3"
    attachments_path = tmp_path / "anexos"
    monkeypatch.setattr(repo_base, "db_path", lambda: database_path)
    monkeypatch.setattr(
        document_service_module, "attachments_dir", lambda: attachments_path
    )
    monkeypatch.setattr(app_logging, "configure_logging", lambda *_a, **_k: None)

    from main import LiquidFinanceApp

    qapp = QApplication.instance() or QApplication([])
    window = LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "0")
    window.backup_service.run_backup_on_close = lambda silent=True: None
    window.check_subscription_reminders = lambda: None
    today = date.today().isoformat()
    connection = window.repo.db.conn
    connection.execute("DELETE FROM transaction_attachments")
    connection.execute("DELETE FROM transaction_meta")
    connection.execute("DELETE FROM transactions")
    connection.execute("DELETE FROM ai_chat_history")
    connection.executemany(
        """
        INSERT INTO transactions(date,description,amount,category,account,kind)
        VALUES(?,?,-1,'Outros','Conta principal','expense')
        """,
        [(today, f"Movimento de carga {index:04d}") for index in range(620)],
    )
    transaction_id = int(
        connection.execute("SELECT MIN(id) FROM transactions").fetchone()[0]
    )
    connection.executemany(
        """
        INSERT INTO transaction_attachments(
            transaction_id,file_name,stored_name,stored_path,original_path,
            file_type,description,supplier,invoice_date,total_amount
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                transaction_id,
                f"documento_{index:04d}.pdf",
                f"documento_{index:04d}.pdf",
                str(
                    attachments_path / str(transaction_id) / f"missing_{index:04d}.pdf"
                ),
                "",
                "PDF",
                "Carga",
                "Fornecedor",
                today,
                1.0,
            )
            for index in range(430)
        ],
    )
    connection.executemany(
        """
        INSERT INTO ai_chat_history(month,provider,model,action,question,prompt,response)
        VALUES(?,?,?,?,?,?,?)
        """,
        [
            (
                today[:7],
                "Teste",
                "modelo",
                "question",
                f"Pergunta {index}",
                "prompt",
                "resposta",
            )
            for index in range(230)
        ],
    )
    connection.commit()

    try:
        movements = window.pages["movimentos"]
        _wait_for_movements(movements, qapp)
        assert movements.movements_model.rowCount() == 250
        assert movements._total_filtered == 620
        assert "250 de 620" in movements.table_status.text()
        assert not movements.load_more_btn.isHidden()

        movements.load_more_rows()
        _wait_for_movements(movements, qapp)
        assert movements.movements_model.rowCount() == 500
        assert movements._total_filtered == 620

        summary = window.repo.transactions_filtered_summary_fresh(
            today[:8] + "01", today
        )
        assert int(summary["movement_count"]) == 620
        assert float(summary["expenses"]) == 620.0

        documents = window.pages["documentos"]
        assert documents.table.rowCount() == 200
        assert documents._documents_total == 430
        documents.load_more_documents()
        assert documents.table.rowCount() == 400

        ai_page = window.pages["ia"]
        assert ai_page.history_table.rowCount() == 100
        assert ai_page._history_total == 230
        ai_page.load_more_history()
        assert ai_page.history_table.rowCount() == 200
    finally:
        window.auto_backup_timer.stop()
        window.pages["dashboard"].shutdown_workers()
        _wait_for_safe_close(window, qapp)
