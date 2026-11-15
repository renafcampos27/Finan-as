from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from database.engine import FinanceDB
from ui.startup import StartupProgressWindow
from ui.workers import AsyncTask, BackgroundTaskRegistry, _ACTIVE_JOBS
from utils.startup_progress import StartupProgress


def _wait_until(predicate, timeout_ms: int = 3_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        QTest.qWait(10)
    return bool(predicate())


def test_registry_cancels_every_task_and_rejects_new_work_during_shutdown() -> None:
    qapp = QApplication.instance() or QApplication([])
    registry = BackgroundTaskRegistry()
    first = AsyncTask(
        registry=registry,
        label="Pesquisa de movimentos",
    )
    second = AsyncTask(
        registry=registry,
        label="Pedido ao assistente de IA",
    )
    finished: list[bool] = []
    first.finished.connect(finished.append)
    second.finished.connect(finished.append)

    class CancellableCall:
        def __init__(self) -> None:
            self.cancelled = threading.Event()

        def __call__(self) -> str:
            self.cancelled.wait(1.0)
            return "ia"

        def cancel(self) -> None:
            self.cancelled.set()

    ai_call = CancellableCall()
    assert first.start(lambda: (time.sleep(0.12), "movimentos")[1])
    assert second.start(ai_call)
    assert set(registry.active_labels()) == {
        "Pesquisa de movimentos",
        "Pedido ao assistente de IA",
    }

    assert registry.begin_shutdown() == 2
    assert ai_call.cancelled.is_set()
    refused = AsyncTask(registry=registry, label="Tarefa tardia")
    assert not refused.start(lambda: "não deve arrancar")
    assert _wait_until(lambda: not registry.has_active_tasks())
    assert _wait_until(lambda: len(finished) == 2)
    assert sorted(finished) == [True, True]
    qapp.processEvents()


def test_maintenance_drains_even_tasks_allowed_during_shutdown() -> None:
    qapp = QApplication.instance() or QApplication([])
    registry = BackgroundTaskRegistry()
    task = AsyncTask(
        registry=registry,
        label="Backup automático",
        allow_during_shutdown=True,
    )

    class CancellableBackup:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.cancelled = threading.Event()

        def __call__(self) -> str:
            self.started.set()
            self.cancelled.wait(1.0)
            return "backup"

        def cancel(self) -> None:
            self.cancelled.set()

    callback = CancellableBackup()
    assert task.start(callback)
    assert callback.started.wait(1.0)
    assert registry.begin_maintenance() == 1
    assert callback.cancelled.is_set()
    assert not registry.can_start(allow_during_shutdown=True)
    assert registry.wait_for_idle(2_000)
    registry.end_maintenance()
    assert registry.can_start()
    qapp.processEvents()


def test_repeated_task_shutdown_releases_qthreads_on_the_gui_thread() -> None:
    qapp = QApplication.instance() or QApplication([])
    for _iteration in range(60):
        registry = BackgroundTaskRegistry()
        task = AsyncTask(registry=registry, label="Tarefa repetida")
        finished: list[bool] = []
        task.finished.connect(finished.append)

        class RepeatedCall:
            def __init__(self) -> None:
                self.cancelled = threading.Event()

            def __call__(self) -> bool:
                self.cancelled.wait(0.5)
                return True

            def cancel(self) -> None:
                self.cancelled.set()

        callback = RepeatedCall()

        assert task.start(callback)
        registry.begin_shutdown()
        assert _wait_until(lambda registry=registry: not registry.has_active_tasks())
        assert _wait_until(lambda finished=finished: finished == [True])
        assert not _ACTIVE_JOBS

        task.deleteLater()
        registry.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()


def test_finished_task_cannot_be_replaced_before_queued_cleanup() -> None:
    qapp = QApplication.instance() or QApplication([])
    registry = BackgroundTaskRegistry()
    task = AsyncTask(registry=registry, label="Tarefa com reinÃ­cio imediato")
    callback_returned = threading.Event()

    def first_callback() -> str:
        callback_returned.set()
        return "primeira"

    assert task.start(first_callback)
    first_thread = task._thread
    assert first_thread is not None
    assert callback_returned.wait(1.0)

    # Finish the native thread without processing the queued cleanup slots in
    # the GUI.  This recreates the narrow race seen when a refresh was requested
    # immediately after a movement query completed.
    first_thread.quit()
    assert first_thread.wait(3_000)
    assert first_thread.isFinished()
    assert task.is_running()
    assert not task.start(lambda: "substituiÃ§Ã£o prematura")
    assert task._thread is first_thread

    assert _wait_until(lambda: task._thread is None)
    assert not registry.has_active_tasks()
    assert not _ACTIVE_JOBS

    task.deleteLater()
    registry.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_shutdown_only_allows_the_explicit_final_backup_task() -> None:
    qapp = QApplication.instance() or QApplication([])
    registry = BackgroundTaskRegistry()
    registry.begin_shutdown()
    ordinary = AsyncTask(registry=registry, label="Tarefa normal")
    final_backup = AsyncTask(
        registry=registry,
        label="Backup ao fechar",
        allow_during_shutdown=True,
    )

    assert not ordinary.start(lambda: True)
    assert final_backup.start(lambda: "backup concluído")
    assert registry.active_labels() == ("Backup ao fechar",)
    assert _wait_until(lambda: not registry.has_active_tasks())
    qapp.processEvents()


def test_protected_database_startup_reports_real_monotonic_progress(
    tmp_path: Path,
) -> None:
    database = tmp_path / "financas.sqlite3"
    attachments = tmp_path / "anexos"
    history = tmp_path / "historico"
    preferences = tmp_path / "configuracao" / "backup_preferences.json"
    protections = tmp_path / "protecoes"

    first = FinanceDB(
        database,
        app_version="3.8.0",
        attachments_directory=attachments,
        history_directory=history,
        preferences_file=preferences,
        protection_directory=protections,
    )
    first.conn.execute(
        """
        INSERT INTO transactions(date,description,amount,category,kind)
        VALUES('2026-07-01','Teste protegido',-12.5,'Outros','expense')
        """
    )
    first.conn.commit()
    expected_transactions = int(first.scalar("SELECT COUNT(*) FROM transactions"))
    first.conn.close()
    attachments.mkdir(parents=True)
    history.mkdir(parents=True)
    (attachments / "fatura.pdf").write_bytes(b"documento protegido")
    (history / "alteracao.json").write_text("{}", encoding="utf-8")

    events: list[StartupProgress] = []
    upgraded = FinanceDB(
        database,
        app_version="3.9.0",
        attachments_directory=attachments,
        history_directory=history,
        preferences_file=preferences,
        protection_directory=protections,
        progress_callback=events.append,
    )
    try:
        assert events
        percentages = [event.percent for event in events]
        assert percentages == sorted(percentages)
        stages = {event.stage for event in events}
        assert "Inventariar documentos" in stages
        assert "Criar backup de proteção" in stages
        assert "Atualizar a estrutura" in stages or "Estrutura atualizada" in stages
        assert "Dados preservados" in stages
        assert events[-1].percent == 92
        assert (
            int(upgraded.scalar("SELECT COUNT(*) FROM transactions"))
            == expected_transactions
        )
        assert list(protections.glob("antes_atualizacao_*.zip"))
    finally:
        upgraded.conn.close()


def test_startup_window_is_compact_readable_and_uses_monotonic_progress() -> None:
    qapp = QApplication.instance() or QApplication([])
    original_palette = qapp.palette()
    palette = QPalette(original_palette)
    palette.setColor(QPalette.Window, QColor("#F4F7FF"))
    qapp.setPalette(palette)
    window = StartupProgressWindow()
    try:
        window.show()
        window.update_progress(
            StartupProgress(
                42,
                "Inventariar documentos",
                "fatura julho.pdf",
                current=7,
                total=18,
            )
        )
        window.update_progress(
            StartupProgress(35, "Etapa atrasada", "não reduz o progresso")
        )
        QTest.qWait(30)

        assert window.size().width() == 520
        assert window.size().height() == 270
        assert window.progress_bar.value() == 42
        assert window.percent_label.text() == "42%"
        assert "não reduz" in window.detail_label.text()
        assert window.detail_label.isVisible()
        assert window.stage_label.isVisible()

        window.close()
        qapp.processEvents()
        assert window.isVisible()
        window.dismiss()
        qapp.processEvents()
        assert not window.isVisible()
    finally:
        window.dismiss()
        qapp.setPalette(original_palette)


def test_document_heavy_upgrade_keeps_repainting_real_progress(
    tmp_path: Path,
) -> None:
    qapp = QApplication.instance() or QApplication([])
    database = tmp_path / "muitos_documentos.sqlite3"
    attachments = tmp_path / "anexos"
    protections = tmp_path / "protecoes"
    first = FinanceDB(
        database,
        app_version="3.8.0",
        attachments_directory=attachments,
        protection_directory=protections,
    )
    first.conn.close()
    attachments.mkdir(parents=True)
    for index in range(20):
        (attachments / f"documento_{index:02d}.pdf").write_bytes(
            (f"documento-{index}".encode("utf-8") + b"-") * 1_024
        )

    startup = StartupProgressWindow()
    startup.show()
    pulses = 0
    events: list[StartupProgress] = []
    heartbeat = QTimer()
    heartbeat.setInterval(1)

    def pulse() -> None:
        nonlocal pulses
        pulses += 1

    def update(progress: StartupProgress) -> None:
        events.append(progress)
        startup.update_progress(progress)

    heartbeat.timeout.connect(pulse)
    heartbeat.start()
    upgraded = FinanceDB(
        database,
        app_version="3.9.0",
        attachments_directory=attachments,
        protection_directory=protections,
        progress_callback=update,
    )
    heartbeat.stop()
    try:
        assert pulses >= 5
        document_events = [
            event
            for event in events
            if event.stage
            in {
                "Inventariar documentos",
                "Guardar documentos protegidos",
                "Verificar backup de proteção",
            }
            and event.total >= 20
        ]
        assert document_events
        assert max(event.current for event in document_events) >= 20
        assert startup.progress_bar.value() == 92
    finally:
        upgraded.conn.close()
        startup.dismiss()
        qapp.processEvents()


def test_database_shutdown_commits_validates_checkpoints_and_closes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "durable_shutdown.sqlite3"
    db = FinanceDB(database)
    db.conn.execute(
        """
        INSERT INTO transactions(date,description,amount,category,kind)
        VALUES('2026-07-29','Ainda por confirmar',-19.95,'Outros','expense')
        """
    )

    assert int(db.conn.execute("PRAGMA synchronous").fetchone()[0]) in (1, 2)
    assert int(db.conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 30_000

    db.prepare_for_shutdown()
    db.prepare_for_shutdown()
    with pytest.raises(sqlite3.ProgrammingError):
        db.conn.execute("SELECT 1")

    reopened = sqlite3.connect(database)
    try:
        assert (
            reopened.execute(
                "SELECT COUNT(*) FROM transactions "
                "WHERE description='Ainda por confirmar'"
            ).fetchone()[0]
            == 1
        )
        assert reopened.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
    finally:
        reopened.close()
    wal = database.with_name(database.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_main_window_waits_for_registered_work_before_closing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import database.repo.base as repo_base
    import main as main_module
    from main import LiquidFinanceApp
    from services.backup import BackupService

    database = tmp_path / "encerramento.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database)
    shutdown_errors: list[str] = []
    monkeypatch.setattr(
        main_module.QMessageBox,
        "critical",
        lambda _parent, _title, message: shutdown_errors.append(str(message)),
    )
    qapp = QApplication.instance() or QApplication([])
    window = LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "1")
    main_thread = threading.get_ident()
    timeline: list[tuple[str, int]] = []

    def final_backup(_database: Path, *, silent: bool = True) -> Path:
        assert not silent
        timeline.append(("backup", threading.get_ident()))
        time.sleep(0.04)
        return tmp_path / "backup_final.zip"

    monkeypatch.setattr(
        BackupService,
        "run_close_backup_for_database",
        staticmethod(final_backup),
    )
    delayed = AsyncTask(
        window,
        registry=window.background_task_registry,
        label="Pesquisa de movimentos",
    )
    states: list[bool] = []
    delayed.finished.connect(states.append)

    def delayed_search() -> str:
        time.sleep(0.16)
        timeline.append(("pesquisa", threading.get_ident()))
        return "resultado tardio"

    assert delayed.start(delayed_search)
    window.show()
    qapp.processEvents()

    window.close()
    assert window._closing
    assert window.background_task_registry.is_shutting_down
    assert window.isVisible()
    assert not window.isEnabled()
    assert _wait_until(lambda: not window.isVisible(), timeout_ms=5_000)
    assert states == [True]
    assert not window.background_task_registry.has_active_tasks()
    assert [step for step, _thread in timeline] == ["pesquisa", "backup"]
    assert all(thread_id != main_thread for _step, thread_id in timeline)
    assert shutdown_errors == []
    window.repo.db.conn.close()


def test_main_window_stays_open_when_final_commit_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import database.repo.base as repo_base
    import main as main_module
    from main import LiquidFinanceApp

    database = tmp_path / "commit_failure.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database)
    messages: list[str] = []
    monkeypatch.setattr(
        main_module.QMessageBox,
        "critical",
        lambda _parent, _title, message: messages.append(str(message)),
    )
    qapp = QApplication.instance() or QApplication([])
    window = LiquidFinanceApp()
    # The modal is stubbed above, so this test opts out of the global
    # testing-mode suppression to exercise the real abort dialog contract.
    window._suppress_error_modals_in_testing = False
    window.repo.set_setting("backup_on_close", "0")
    original_commit = window.repo.commit

    def fail_commit() -> None:
        raise OSError("disco sem espaço")

    monkeypatch.setattr(window.repo, "commit", fail_commit)
    window.show()
    qapp.processEvents()
    window.close()

    assert _wait_until(lambda: not window._closing)
    assert window.isVisible()
    assert window.isEnabled()
    assert not window.background_task_registry.is_shutting_down
    assert not window._database_closed
    assert messages and "disco sem espaço" in messages[-1]

    monkeypatch.setattr(window.repo, "commit", original_commit)
    window.close()
    assert _wait_until(lambda: not window.isVisible())
    assert window._database_closed


def test_main_window_stays_open_when_requested_close_backup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import database.repo.base as repo_base
    from main import LiquidFinanceApp
    from services.backup import BackupService

    database = tmp_path / "backup_failure.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database)
    messages: list[str] = []

    def fail_backup(_database: Path, *, silent: bool = True) -> Path:
        assert not silent
        raise OSError("destino de backup indisponível")

    monkeypatch.setattr(
        BackupService,
        "run_close_backup_for_database",
        staticmethod(fail_backup),
    )
    qapp = QApplication.instance() or QApplication([])
    window = LiquidFinanceApp()
    window.repo.set_setting("backup_on_close", "1")
    # A13: the failure decision itself presents the error; cancel must not
    # open a second modal or silently disable the saved backup preference.
    monkeypatch.setattr(
        window, "_choose_close_backup_failure",
        lambda detail: messages.append(str(detail)) or "cancel",
    )
    window.show()
    qapp.processEvents()
    window.close()

    assert _wait_until(lambda: not window._closing, timeout_ms=5_000)
    assert window.isVisible()
    assert window.isEnabled()
    assert not window.background_task_registry.is_shutting_down
    assert len(messages) == 1
    assert "destino de backup indisponível" in messages[-1]

    window.repo.set_setting("backup_on_close", "0")
    window.close()
    assert _wait_until(lambda: not window.isVisible())
    assert window._database_closed


def test_loaded_pages_flush_debounced_preferences_before_close(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import database.repo.base as repo_base
    from main import LiquidFinanceApp

    database = tmp_path / "pending_preferences.sqlite3"
    monkeypatch.setattr(repo_base, "db_path", lambda: database)
    qapp = QApplication.instance() or QApplication([])
    window = LiquidFinanceApp()
    settings = window.pages["definicoes"]
    simulations = window.pages["simulacoes"]
    calls: list[str] = []
    monkeypatch.setattr(
        settings,
        "save_backup_settings",
        lambda silent=False: calls.append("backup") is None or True,
    )
    monkeypatch.setattr(
        settings,
        "save_ai_settings",
        lambda silent=False: calls.append("ai") is None or True,
    )
    monkeypatch.setattr(
        simulations,
        "_persist_state_now",
        lambda: calls.append("simulações"),
    )
    settings.backup_save_timer.start(60_000)
    settings.ai_save_timer.start(60_000)
    simulations._persist_timer.start(60_000)

    assert window._save_shutdown_state() == (True, "")
    assert calls == ["backup", "ai", "simulações"]
    assert not settings.backup_save_timer.isActive()
    assert not settings.ai_save_timer.isActive()
    assert not simulations._persist_timer.isActive()

    window.repo.set_setting("backup_on_close", "0")
    window.close()
    assert _wait_until(lambda: not window.isVisible())
    qapp.processEvents()
