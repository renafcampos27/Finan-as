#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Optional

# Package validation always owns a disposable profile, before logging/services
# resolve default paths. It cannot inherit the caller's financial profile.
if "--package-self-test" in sys.argv:
    import tempfile

    _package_test_state = tempfile.TemporaryDirectory(prefix="liquidglass-self-test-")
    os.environ["FINANCAS_PESSOAIS_STATE_DIR"] = _package_test_state.name
    for _variable, _folder in {
        "TEMP": "tmp", "TMP": "tmp", "TMPDIR": "tmp",
        "HOME": "home", "USERPROFILE": "home",
        "LOCALAPPDATA": "local", "APPDATA": "roaming",
    }.items():
        _path = Path(_package_test_state.name) / _folder
        _path.mkdir(parents=True, exist_ok=True)
        os.environ[_variable] = str(_path)
    tempfile.tempdir = str(Path(_package_test_state.name) / "tmp")
    os.environ["FINANCAS_PESSOAIS_TESTING"] = "1"
    os.environ["FINANCAS_PESSOAIS_DEMO_DATA"] = "0"
    os.environ.pop("GEMINI_API_KEY", None)

from utils.qt_runtime import (
    configure_qt_runtime,
    install_global_exception_hook,
    is_qt_valid,
)

configure_qt_runtime()


from utils.app_logging import (
    configure_logging,
    get_log_path,
    install_qt_message_handler,
    log_exception,
    log_info,
    log_warning,
    shutdown_logging,
)

configure_logging()
install_global_exception_hook()

if "--package-self-test" in sys.argv:
    import atexit

    def _cleanup_package_test_state() -> None:
        # A packaged boot hook may import logging before tempfile. Explicit
        # ordering closes the Windows file handle before private tree removal.
        shutdown_logging()
        _package_test_state.cleanup()

    atexit.register(_cleanup_package_test_state)

try:
    from PySide6.QtCore import (
        QEasingCurve,
        QEvent,
        QLockFile,
        QPoint,
        QPropertyAnimation,
        QSize,
        Qt,
        QTimer,
        QUrl,
    )
    from PySide6.QtGui import QColor
    from PySide6.QtMultimedia import QSoundEffect
    from PySide6.QtWidgets import (
        QApplication,
        QCalendarWidget,
        QComboBox,
        QDateEdit,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFrame,
        QGraphicsDropShadowEffect,
        QGraphicsOpacityEffect,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QStackedWidget,
        QStatusBar,
        QVBoxLayout,
        QWidget,
    )
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    log_exception("PySide6 import failed", exc)
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from database.engine import FinanceDB
from database.protection import (
    DataIntegrityError,
    DataProtectionError,
    DataProtectionGuard,
    MissingDatabaseRecoveryError,
)
from database.repo import Repo
from services.ai import AIService
from services.analytics import AnalyticsService
from services.backup import BackupService
from services.change_journal import ChangeJournalService
from services.documents import DocumentService
from services.data_location import ensure_persistent_data_location
from services.imports import ImportService
from services.payments import PaymentsService
from ui.components import Background
from ui.components.page_tools import PageToolsBar
from ui.core import (
    AccessibilityManager,
    BasePage,
    MoneyInputPrivacyManager,
    ReadableToolTipManager,
    TableColumnWidthManager,
    Theme,
    apply_calendar_theme,
    apply_combo_theme,
    apply_tooltip_theme,
    configure_portuguese_number_locale,
    prepare_date_edit,
)
from ui.dialogs import TransactionDialog
from ui.navigation import (
    all_navigation_items,
    contextual_navigation_label,
    navigation_groups,
    navigation_key_for_page,
    workspace_for_page,
)
from ui.search_routing import resolve_route
from ui.pages.dashboard import DashboardPage
from ui.startup import (
    StartupProgressWindow,
    exec_foreground_message,
    prepare_foreground_dialog,
)
from ui.workers import AsyncTask, BackgroundTaskRegistry
from utils.helpers import APP_TITLE
from utils.config import instance_lock_path, paths_for_database
from utils.startup_progress import (
    StartupProgressCallback,
    report_startup_progress,
)
from utils.version import APP_VERSION, APP_VERSION_LABEL, RELEASE_VERSION


SQLITE_CORRUPTION_MARKERS = (
    "database disk image is malformed",
    "database corruption",
    "file is not a database",
)


def is_sqlite_corruption_error(error: BaseException) -> bool:
    """Distinguish damaged storage from transient or operational DB errors."""

    if not isinstance(error, sqlite3.DatabaseError):
        return False
    detail = str(error or "").strip().casefold()
    return any(marker in detail for marker in SQLITE_CORRUPTION_MARKERS)


def acquire_single_instance_lock() -> QLockFile | None:
    """Prevent concurrent writers, migrations and restores for the same profile."""
    lock = QLockFile(str(instance_lock_path()))
    lock.setStaleLockTime(30_000)
    if not lock.tryLock(150):
        return None
    return lock


class LazyPageRegistry(dict[str, BasePage]):
    """Dictionary-compatible page registry that constructs pages on first use."""

    def __init__(
        self,
        app: "LiquidFinanceApp",
        factories: dict[str, type[BasePage] | str],
    ):
        super().__init__()
        self.app = app
        self.factories = factories
        self._creating: set[str] = set()

    def _factory(self, key: str) -> type[BasePage]:
        factory = self.factories[key]
        if isinstance(factory, str):
            module_name, separator, attribute = factory.partition(":")
            if not separator or not module_name or not attribute:
                raise RuntimeError(f"Invalid lazy page factory: {factory!r}")
            resolved = getattr(import_module(module_name), attribute)
            if not isinstance(resolved, type) or not issubclass(resolved, BasePage):
                raise TypeError(f"Invalid page class for {key}: {resolved!r}")
            self.factories[key] = resolved
            return resolved
        return factory

    def __contains__(self, key: object) -> bool:
        return key in self.factories

    def __getitem__(self, key: str) -> BasePage:
        existing = dict.get(self, key)
        if existing is not None:
            return existing
        if key not in self.factories:
            raise KeyError(key)
        if key in self._creating:
            raise RuntimeError(f"Recursive page creation: {key}")
        self._creating.add(key)
        try:
            page = self._factory(key)(self.app)
            page.configure_workspace_navigation(workspace_for_page(key), key)
            dict.__setitem__(self, key, page)
            self.app.stack.addWidget(page)
            for combo in page.findChildren(QComboBox):
                apply_combo_theme(combo, self.app.theme)
            self.app.table_column_manager.register_tree(page)
            self.app.accessibility_manager.audit_tree(page)
            # Page constructors own their initial population. Repeating it here
            # used to rebuild widgets twice and start duplicate Dashboard workers.
            self.app._dirty_page_keys.discard(key)
            refreshed = datetime.now()
            self.app._page_refreshed_at[key] = refreshed.timestamp()
            page.apply_card_preferences()
            page.apply_chart_calculation_contexts()
            if page._refresh_completes_asynchronously:
                page.begin_data_refresh()
            else:
                page.mark_data_updated(refreshed)
            log_info(f"Initialized page on demand: {key} ({page.__class__.__name__})")
            return page
        finally:
            self._creating.discard(key)

    def get(self, key: str, default=None):  # type: ignore[override]
        if key not in self.factories:
            return default
        return self[key]

    def existing(self, key: str, default=None):
        return dict.get(self, key, default)


class CurrentPageStack(QStackedWidget):
    """Expose only the active page's geometry to the outer scroll area."""

    def sizeHint(self) -> QSize:  # type: ignore[override]
        current = self.currentWidget()
        if current is not None:
            return current.sizeHint()
        return super().sizeHint()

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        current = self.currentWidget()
        if current is not None:
            return current.minimumSizeHint()
        return super().minimumSizeHint()


class LiquidFinanceApp(QMainWindow):
    COMPACT_CHROME_BREAKPOINT = 1120
    SHUTDOWN_WARNING_SECONDS = 10.0

    def __init__(
        self,
        *,
        startup_progress: StartupProgressCallback | None = None,
    ):
        super().__init__()
        self._closing = False
        self._shutdown_started = False
        self._shutdown_state_saved = False
        self._shutdown_backup_started = False
        self._shutdown_backup_finished = False
        self._shutdown_backup_error = ""
        self._shutdown_close_scheduled = False
        self._shutdown_started_at = 0.0
        self._shutdown_warning_shown = False
        self._shutdown_wait_dialog = None
        self._shutdown_cancel_requested = False
        self._database_closed = False
        self._database_restore_prepared = False
        self._database_maintenance_active = False
        self._shutdown_integrity_error = False
        self.background_task_registry = BackgroundTaskRegistry(self)
        self.background_task_registry.all_finished.connect(
            self._continue_shutdown_when_ready
        )
        self._shutdown_poll_timer = QTimer(self)
        self._shutdown_poll_timer.setSingleShot(True)
        self._shutdown_poll_timer.setInterval(80)
        self._shutdown_poll_timer.timeout.connect(self._continue_shutdown_when_ready)
        QApplication.instance().setApplicationVersion(APP_VERSION)
        self._sidebar_compact = False
        self._sidebar_contextual = False
        self._sidebar_manual_override = False
        self._topbar_compact = False
        self._chrome_reflow_timer = QTimer(self)
        self._chrome_reflow_timer.setSingleShot(True)
        self._chrome_reflow_timer.timeout.connect(self._apply_responsive_chrome)
        self._content_sync_timer = QTimer(self)
        self._content_sync_timer.setSingleShot(True)
        self._content_sync_timer.setInterval(32)
        self._content_sync_timer.timeout.connect(self.sync_content_size)
        self.repo = Repo(progress_callback=startup_progress)
        report_startup_progress(
            startup_progress,
            93,
            "Preparar rotinas financeiras",
            "A atualizar subscrições e o estado diário.",
        )
        self._sidebar_user_compact = self.repo.setting("sidebar_compact", "0") == "1"
        self.table_column_manager = TableColumnWidthManager(self.repo, self)
        QApplication.instance().installEventFilter(self.table_column_manager)
        self.change_journal_service = ChangeJournalService(self.repo)
        try:
            active_subscriptions = self.repo.subscriptions(active_only=True)
            active_subscription_ids = [
                int(subscription["id"]) for subscription in active_subscriptions
            ]

            def process_subscriptions() -> list[int]:
                self.repo.process_due_subscriptions(
                    commit=False,
                    subscriptions=active_subscriptions,
                )
                report = dict(
                    getattr(
                        self.repo,
                        "last_subscription_processing_report",
                        {},
                    )
                    or {}
                )
                return [int(value) for value in report.get("created_ids", [])]

            charged_ids = self.change_journal_service.run_with_created_transactions(
                process_subscriptions,
                label=lambda ids: f"Subscrições automáticas · {len(ids)} movimento(s)",
                action_type="automatic_subscription",
                state_targets={
                    "subscriptions": {
                        "ids": active_subscription_ids,
                        "columns": ("last_used_date", "next_renewal_date"),
                    }
                },
                delete_linked_occurrences=True,
            )
            charged_subs = len(charged_ids)
            if charged_subs > 0:
                log_info(f"Auto-charged {charged_subs} subscriptions on startup")
            subscription_report = dict(
                getattr(self.repo, "last_subscription_processing_report", {}) or {}
            )
            if int(subscription_report.get("historical_skipped", 0) or 0) > 0:
                log_warning(
                    "Skipped %s stale subscription renewals; only the current due "
                    "renewal was considered",
                    subscription_report["historical_skipped"],
                )
            if int(subscription_report.get("deferred", 0) or 0) > 0:
                log_warning(
                    "Deferred %s subscription charges because the safe startup "
                    "batch limit was reached",
                    subscription_report["deferred"],
                )
            if int(subscription_report.get("invalid", 0) or 0) > 0:
                log_warning(
                    "Ignored %s subscription charges with an invalid or zero value",
                    subscription_report["invalid"],
                )
        except sqlite3.DatabaseError as exc:
            log_exception("Failed to auto-charge subscriptions", exc)
            try:
                self.repo.db.conn.rollback()
            except sqlite3.DatabaseError as rollback_error:
                log_exception(
                    "Failed to roll back automatic subscription charges",
                    rollback_error,
                )
            if is_sqlite_corruption_error(exc):
                raise DataProtectionError(
                    "O SQLite detetou uma estrutura interna danificada ao "
                    "processar as subscrições. A operação foi anulada e o "
                    "arranque foi bloqueado para evitar novas gravações."
                ) from exc
        except Exception as exc:
            log_exception("Failed to auto-charge subscriptions", exc)
        try:
            # Create one controlled daily checkpoint. Reopening or refreshing
            # without changes does not rewrite its timestamp.
            self.repo.record_investment_snapshot()
            self.repo.record_financial_state_snapshot()
        except Exception as exc:
            log_exception("Failed to capture startup financial state", exc)
        report_startup_progress(
            startup_progress,
            95,
            "Carregar serviços",
            "Backups, documentos, análises e pagamentos.",
        )

        # Audio Setup
        from utils.config import resource_path

        self.click_sound = QSoundEffect()
        self.click_sound.setSource(
            QUrl.fromLocalFile(str(resource_path("assets/click.wav")))
        )
        self.click_sound.setVolume(0.15)

        self.payments_service = PaymentsService(
            self.repo,
            self.change_journal_service,
        )
        self.analytics_service = AnalyticsService(
            self.repo,
            payments_service=self.payments_service,
        )
        self.backup_service = BackupService(self.repo)
        self.auto_backup_task = AsyncTask(
            self,
            registry=self.background_task_registry,
            label="Backup automático",
        )
        self.auto_backup_task.result.connect(self._auto_backup_completed)
        self.auto_backup_task.error.connect(self._auto_backup_failed)
        self.close_backup_task = AsyncTask(
            self,
            registry=self.background_task_registry,
            label="Backup ao fechar",
            allow_during_shutdown=True,
        )
        self.close_backup_task.result.connect(self._close_backup_completed)
        self.close_backup_task.error.connect(self._close_backup_failed)
        self.close_backup_task.finished.connect(self._close_backup_finished)
        self.document_service = DocumentService(self.repo)
        self.import_service = ImportService(self.repo, self.change_journal_service)
        self.ai_service = AIService(
            self.repo,
            backup_service=self.backup_service,
            analytics_service=self.analytics_service,
            payments_service=self.payments_service,
        )
        self.theme = Theme(
            dark=self.repo.setting("liquid_dark", "0") == "1",
            privacy=self.repo.setting("liquid_privacy", "0") == "1",
            primary_color_name=self.repo.setting("liquid_primary", "Azul"),
        )
        # Install top-level popup selectors before the widget tree exists.
        # Applying QApplication QSS after hundreds of widgets were created
        # forced a second complete style pass during a cold launch.
        apply_tooltip_theme(self.theme)
        report_startup_progress(
            startup_progress,
            96,
            "Construir a interface",
            "A aplicar o tema e os controlos de privacidade.",
        )
        self.money_input_privacy = MoneyInputPrivacyManager(self)
        QApplication.instance().installEventFilter(self.money_input_privacy)
        self.money_input_privacy.set_privacy(self.theme.privacy)
        self.tooltip_manager = ReadableToolTipManager(
            self,
            accent=self.theme.primary_color,
        )
        QApplication.instance().installEventFilter(self.tooltip_manager)
        self.accessibility_manager = AccessibilityManager(self)
        QApplication.instance().installEventFilter(self.accessibility_manager)
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(960, 640)
        from ui.components.animations import AuroraBackgroundWidget, ConfettiWidget

        self.root = Background(self.theme)
        self.setCentralWidget(self.root)
        # Defensive: PySide6 6.11.x on Python 3.14 can return a bare QWidget
        # from ``statusBar()`` when the layout was mutated during __init__
        # (style re-polish from apply_tooltip_theme).  Build the bar
        # explicitly so the type contract is guaranteed on every Qt build.
        status = QStatusBar(self)
        self.setStatusBar(status)
        status.setObjectName("AppStatusBar")
        status.setSizeGripEnabled(False)

        self.confetti = ConfettiWidget(self.root)
        self.confetti.hide()

        self.aurora_bg = AuroraBackgroundWidget(self.root, is_dark=self.theme.dark)
        self.aurora_bg.lower()
        self.root.installEventFilter(self)
        outer = QHBoxLayout(self.root)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(12)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("Sidebar")
        # Compact responsive sidebar: uses less horizontal screen space while keeping labels readable.
        # Long labels keep full text as tooltip and the nav area scrolls vertically when needed.
        self.sidebar.setMinimumWidth(184)
        self.sidebar.setMaximumWidth(198)
        self.sidebar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.sidebar.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(self.sidebar)
        self.side_layout = QVBoxLayout(self.sidebar)
        self.side_layout.setContentsMargins(8, 12, 8, 12)
        self.side_layout.setSpacing(6)
        self.build_sidebar()
        content_wrap = QVBoxLayout()
        content_wrap.setSpacing(8)
        outer.addLayout(content_wrap, 1)
        self.build_topbar(content_wrap)
        self.dashboard_sticky_host = QFrame()
        self.dashboard_sticky_host.setObjectName("DashboardStickyFilters")
        self.dashboard_sticky_host.setAttribute(Qt.WA_StyledBackground, True)
        self.dashboard_sticky_layout = QVBoxLayout(self.dashboard_sticky_host)
        self.dashboard_sticky_layout.setContentsMargins(0, 0, 0, 0)
        self.dashboard_sticky_layout.setSpacing(0)
        self.dashboard_sticky_summary = QLabel()
        self.dashboard_sticky_summary.setObjectName("DashboardStickySummary")
        self.dashboard_sticky_summary.setWordWrap(True)
        self.dashboard_sticky_summary.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Maximum,
        )
        self.dashboard_sticky_summary.setAccessibleName(
            "Resumo dos filtros atuais do Dashboard"
        )
        self.dashboard_sticky_layout.addWidget(self.dashboard_sticky_summary)
        self.dashboard_sticky_host.hide()
        content_wrap.addWidget(self.dashboard_sticky_host, 0)
        self.stack = CurrentPageStack()
        self.stack.setObjectName("MainStack")
        self.stack.setAutoFillBackground(False)
        self.stack.setAttribute(Qt.WA_StyledBackground, True)
        self.stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self.content_scroll = QScrollArea()
        self.content_scroll.setObjectName("ContentScroll")
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.NoFrame)
        self.content_scroll.setAutoFillBackground(False)
        self.content_scroll.viewport().setAutoFillBackground(False)
        self.content_scroll.viewport().setStyleSheet("background: transparent;")
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.content_scroll.verticalScrollBar().setSingleStep(24)
        self.content_scroll.setWidget(self.stack)
        self.content_scroll.viewport().installEventFilter(self)
        self.page_tools = PageToolsBar(
            self.content_scroll.viewport(),
            on_top=self._page_tools_top,
            on_filters=self._page_tools_filters,
            on_reset=self._page_tools_reset,
        )
        self.content_scroll.verticalScrollBar().valueChanged.connect(
            self._update_dashboard_sticky_summary
        )
        self.content_scroll.verticalScrollBar().valueChanged.connect(
            lambda _value: self._update_page_tools()
        )
        self.content_scroll.verticalScrollBar().rangeChanged.connect(
            lambda _minimum, _maximum: self._update_page_tools()
        )
        content_wrap.addWidget(self.content_scroll, 1)
        self.page_factories: dict[str, type[BasePage] | str] = {
            "dashboard": DashboardPage,
            "panorama": "ui.pages.panorama:PanoramaPage",
            "movimentos": "ui.pages.movements:MovementsPage",
            "orcamentos": "ui.pages.planning:BudgetsPage",
            "subscricoes": "ui.pages.subscriptions:SubscriptionsPage",
            "carteira": "ui.pages.investments:PortfolioPage",
            "objetivos": "ui.pages.planning:GoalsPage",
            "planeador": "ui.pages.planned_purchases:PlanningPage",
            "analises": "ui.pages.reports:ReportsPage",
            "dinamica": "ui.pages.dynamic_analysis:DynamicAnalysisPage",
            "importacao": "ui.pages.tools:SmartImportPage",
            "revisao": "ui.pages.reports:MonthlyReviewPage",
            "pagamentos": "ui.pages.planning:ScheduledPaymentsPage",
            "metas_mensais": "ui.pages.planning:MonthlyGoalsPage",
            "simulacoes": "ui.pages.simulations:HabitSimulationsPage",
            "simulador": "ui.pages.tools:SimulatorPage",
            "juros": "ui.pages.tools:CompoundInterestPage",
            "score": "ui.pages.reports:ScoreDetailsPage",
            "risco": "ui.pages.risk_resilience:RiskResiliencePage",
            "relatorios_pdf": "ui.pages.reports:PdfReportsPage",
            "historico": "ui.pages.history:HistoryPage",
            "documentos": "ui.pages.core_pages:DocumentsPage",
            "divisao": "ui.pages.tools:SplitsPage",
            "saude": "ui.pages.reports:HealthPage",
            "centro": "ui.pages.core_pages:FinanceCenterPage",
            "assistente": "ui.pages.ai:AssistantPage",
            "ia": "ui.pages.ai:AIAssistantPage",
            "definicoes": "ui.pages.settings:SettingsPage",
            "gestao_dados": "ui.pages.data_management:DataManagementPage",
        }
        self._dirty_page_keys: set[str] = set(self.page_factories)
        self._page_refreshed_at: dict[str, float] = {}
        self._page_scroll_positions: dict[str, int] = {}
        self._current_page_key = ""
        # Tests that monkeypatch QMessageBox may opt out of the global
        # testing-mode modal suppression so the abort path is still exercised.
        self._suppress_error_modals_in_testing = True
        self._scroll_restore_generation = 0
        self.pages = LazyPageRegistry(self, self.page_factories)
        self.pages["dashboard"]
        report_startup_progress(
            startup_progress,
            98,
            "Preparar o Dashboard",
            "A organizar os cartões e filtros guardados.",
        )
        self._current_page_key = "dashboard"
        self.nav_buttons: dict[str, QPushButton] = {}
        self.populate_nav_buttons()
        self.set_sidebar_compact(self._sidebar_user_compact, persist=False)
        self.apply_theme(first=True)
        self.apply_font_scale()
        self.goto("dashboard")
        self.auto_backup_timer = QTimer(self)
        self.auto_backup_timer.timeout.connect(self._start_auto_backup_check)
        # Check frequently so short intervals such as 15/30 minutes work reliably.
        self.auto_backup_timer.start(60 * 1000)

        # Theme and font were already applied above. Reapplying both after the
        # window appeared caused a second full style/layout pass during the
        # most sensitive part of startup.
        QTimer.singleShot(100, self.sync_content_size)
        self._setup_shortcuts()
        self.accessibility_manager.audit_tree(self)
        self._apply_startup_geometry()
        self._chrome_reflow_timer.start(0)

        self.subscription_reminder_timer = QTimer(self)
        self.subscription_reminder_timer.setSingleShot(True)
        self.subscription_reminder_timer.timeout.connect(
            self.check_subscription_reminders
        )
        self.subscription_reminder_timer.start(2500)

        self._prewarm_queue: list[str] = [
            "movimentos",
            "carteira",
            "panorama",
            "analises",
            "planeador",
            "orcamentos",
            "subscricoes",
        ]
        self._prewarm_timer = QTimer(self)
        self._prewarm_timer.setSingleShot(True)
        self._prewarm_timer.timeout.connect(self._prewarm_next_page)
        self._prewarm_timer.start(1200)

        from ui.app_updates import AppUpdateController

        self.app_updates = AppUpdateController(self)
        self.app_updates.schedule_startup_check()

        report_startup_progress(
            startup_progress,
            99,
            "Interface pronta",
            "A apresentar a janela principal.",
        )

    def _prewarm_next_page(self) -> None:
        """Silently instantiate common pages during idle time after startup."""
        if self._closing or not self._prewarm_queue:
            return
        key = self._prewarm_queue.pop(0)
        if key in self.pages.factories and key not in self.pages.keys():
            try:
                _ = self.pages[key]
            except Exception as exc:
                log_warning("Ignorando pré-aquecimento da página '%s': %s", key, exc)
        if self._prewarm_queue and not self._closing:
            self._prewarm_timer.start(300)

    def _start_auto_backup_check(self) -> None:
        """Check/create scheduled backups without occupying the GUI thread."""

        if self._closing or self.auto_backup_task.is_running():
            return
        if not self.repo.safe_commit("Failed to commit before scheduled backup"):
            self.statusBar().showMessage(
                "Backup automático adiado: os dados ainda não foram confirmados.",
                8000,
            )
            return
        database_file = self.repo.db.path
        self.auto_backup_task.start(
            lambda: BackupService.run_scheduled_backup_for_database(
                database_file,
                silent=True,
            )
        )

    def _auto_backup_completed(self, result: object) -> None:
        if result:
            log_info("Scheduled backup completed: %s", result)

    def _auto_backup_failed(self, message: str) -> None:
        log_warning("Scheduled background backup failed: %s", message)

    def _close_backup_completed(self, result: object) -> None:
        if result:
            log_info("Backup on close completed: %s", result)
            self._shutdown_backup_error = ""
            return
        self._shutdown_backup_error = (
            "O backup configurado para o encerramento não foi criado."
        )

    def _close_backup_failed(self, message: str) -> None:
        log_warning("Background backup on close failed: %s", message)
        self._shutdown_backup_error = str(message or "Falha desconhecida no backup.")

    def _close_backup_finished(self, _cancelled: bool = False) -> None:
        self._shutdown_backup_finished = True
        self._continue_shutdown_when_ready()

    def check_subscription_reminders(self) -> None:
        """Show renewals due in the next three real calendar days."""
        if self._closing:
            return
        try:
            due_subs = self.payments_service.upcoming_subscriptions(within_days=3)
            if due_subs:

                def due_label(item: dict) -> str:
                    days = int(item.get("days", 0) or 0)
                    when = (
                        "hoje"
                        if days == 0
                        else "amanhã"
                        if days == 1
                        else f"em {days} dias"
                    )
                    return f"{item.get('name', 'Subscrição')} ({when})"

                names = ", ".join(due_label(item) for item in due_subs[:2])
                if len(due_subs) > 2:
                    names += f" e +{len(due_subs) - 2}"
                self.show_dashboard_notice(
                    "Renovações próximas",
                    f"{names}. Confirma os valores e as datas na área de Subscrições.",
                    "📅",
                    tone="warning",
                    duration_ms=24000,
                )
        except Exception as e:
            from utils.app_logging import log_exception

            log_exception("Failed to check subscription reminders", e)

    def _setup_shortcuts(self) -> None:
        """Register global keyboard shortcuts for common actions."""
        from PySide6.QtGui import QKeySequence, QShortcut

        shortcuts = [
            (QKeySequence("Ctrl+N"), self.quick_add),
            (QKeySequence("Ctrl+R"), self.refresh_all),
            (
                QKeySequence("Ctrl+F"),
                lambda: (self.search.setFocus(), self.search.selectAll()),
            ),
            (
                QKeySequence("Ctrl+K"),
                lambda: (self.search.setFocus(), self.search.selectAll()),
            ),
            (QKeySequence("Ctrl+P"), self.open_command_palette),
            (QKeySequence("F1"), lambda: self.goto("dashboard")),
            (QKeySequence("F2"), lambda: self.goto("movimentos")),
            (QKeySequence("F3"), lambda: self.goto("objetivos")),
            (QKeySequence("F4"), lambda: self.goto("orcamentos")),
            (QKeySequence("F5"), self.refresh_all),
            (QKeySequence("F6"), lambda: self.goto("carteira")),
            (QKeySequence("F7"), lambda: self.goto("historico")),
            (QKeySequence("F8"), lambda: self.goto("ia")),
            (QKeySequence("F9"), lambda: self.goto("definicoes")),
            (QKeySequence("Ctrl+D"), lambda: self.goto("dashboard")),
            (QKeySequence("Ctrl+M"), lambda: self.goto("movimentos")),
            (QKeySequence("Ctrl+O"), lambda: self.goto("objetivos")),
            (QKeySequence("Ctrl+F6"), lambda: self.cycle_focus_zone(1)),
            (QKeySequence("Ctrl+Shift+F6"), lambda: self.cycle_focus_zone(-1)),
        ]
        for key_seq, slot in shortcuts:
            sc = QShortcut(key_seq, self)
            sc.activated.connect(slot)

    @staticmethod
    def _is_descendant(widget: Optional[QWidget], ancestor: QWidget) -> bool:
        current = widget
        while current is not None:
            if current is ancestor:
                return True
            current = current.parentWidget()
        return False

    @staticmethod
    def _first_focusable(root: QWidget) -> Optional[QWidget]:
        for widget in root.findChildren(QWidget):
            if (
                widget.isVisibleTo(root)
                and widget.isEnabled()
                and widget.focusPolicy() != Qt.NoFocus
            ):
                return widget
        return None

    def cycle_focus_zone(self, direction: int = 1) -> Optional[QWidget]:
        """Move focus between navigation, top bar and current page."""

        page = self.stack.currentWidget()
        if page is None:
            return None
        active_navigation_key = navigation_key_for_page(self._current_page_key)
        nav_target = self.nav_buttons.get(active_navigation_key)
        if nav_target is None or not nav_target.isVisibleTo(self):
            nav_target = next(
                (
                    button
                    for button in self.nav_buttons.values()
                    if button.isVisibleTo(self)
                ),
                None,
            )
        page_target = self._first_focusable(page)
        zones = [target for target in (nav_target, self.search, page_target) if target]
        if not zones:
            return None
        focused = QApplication.focusWidget()
        focused_index = next(
            (
                index
                for index, target in enumerate(zones)
                if focused is target or self._is_descendant(focused, target)
            ),
            None,
        )
        if focused_index is None:
            current_index = int(
                getattr(self, "_focus_zone_index", -1 if direction >= 0 else 0)
            )
        else:
            current_index = focused_index
        step = 1 if direction >= 0 else -1
        target_index = (current_index + step) % len(zones)
        target = zones[target_index]
        self._focus_zone_index = target_index
        target.setFocus(Qt.ShortcutFocusReason)
        if isinstance(target, QLineEdit):
            target.selectAll()
        self.statusBar().showMessage(
            f"Foco: {target.accessibleName() or target.objectName() or 'conteúdo'}",
            1800,
        )
        return target

    def _apply_startup_geometry(self) -> None:
        """Restore window geometry and state, or start in a centered/maximized window."""
        was_maximized = self.repo.setting("window_maximized", "1") == "1"
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None

        saved_geom = self.repo.setting("window_geometry", "")
        geometry_restored = False
        if saved_geom and not was_maximized:
            try:
                parts = [int(p.strip()) for p in saved_geom.split(",")]
                if len(parts) == 4:
                    x, y, w, h = parts
                    # BUG-AUD-C95: Detect the target screen containing the saved coordinates
                    # (including secondary monitors); if disconnected or off-screen, fall back
                    # to primary screen.
                    from PySide6.QtCore import QPoint
                    from PySide6.QtGui import QGuiApplication

                    center_pt = QPoint(x + w // 2, y + h // 2)
                    target_screen = QGuiApplication.screenAt(center_pt)
                    if target_screen is None:
                        target_screen = QGuiApplication.screenAt(QPoint(x + 50, y + 50))
                    if target_screen is not None:
                        target_avail = target_screen.availableGeometry()
                        if (
                            w >= self.minimumWidth()
                            and h >= self.minimumHeight()
                            and x + w > target_avail.x() + 50
                            and x < target_avail.x() + target_avail.width() - 50
                            and y + h > target_avail.y() + 50
                            and y < target_avail.y() + target_avail.height() - 50
                        ):
                            self.setGeometry(x, y, w, h)
                            geometry_restored = True
            except Exception as exc:
                log_warning(
                    "Não foi possível restaurar a geometria guardada da janela: %s", exc
                )

        if not geometry_restored:
            if available is None:
                self.resize(1280, 820)
            else:
                margin = 48
                max_w = max(self.minimumWidth(), available.width() - margin)
                max_h = max(self.minimumHeight(), available.height() - margin)
                width = min(
                    1360, max(self.minimumWidth(), int(available.width() * 0.86)), max_w
                )
                height = min(
                    860,
                    max(self.minimumHeight(), int(available.height() * 0.86)),
                    max_h,
                )
                x = available.x() + max(0, (available.width() - width) // 2)
                y = available.y() + max(0, (available.height() - height) // 2)
                self.setGeometry(x, y, width, height)

        if was_maximized:
            self.setWindowState(Qt.WindowMaximized)
        else:
            self.setWindowState(Qt.WindowNoState)

    def _page_tools_top(self) -> None:
        self.content_scroll.verticalScrollBar().setValue(0)

    def _page_tools_filters(self) -> None:
        page = self.stack.currentWidget()
        focus_filters = getattr(page, "focus_primary_filters", None)
        panel = focus_filters() if callable(focus_filters) else None
        if panel is not None:
            self.content_scroll.ensureWidgetVisible(panel, 24, 24)
            QTimer.singleShot(
                0,
                lambda target=panel: self.content_scroll.ensureWidgetVisible(
                    target, 24, 24
                ),
            )

    def _page_tools_reset(self) -> None:
        page = self.stack.currentWidget()
        reset = getattr(page, "reset_page_view", None)
        if callable(reset):
            reset()
            QTimer.singleShot(0, self._update_page_tools)

    def _update_page_tools(self) -> None:
        if not hasattr(self, "page_tools"):
            return
        page = self.stack.currentWidget()
        filter_available = bool(
            page is not None
            and getattr(page, "_primary_filter_panel", None) is not None
        )
        scroll_available = self.content_scroll.verticalScrollBar().maximum() > 0
        self.page_tools.set_context(
            filter_available=filter_available,
            scroll_available=scroll_available,
        )

    def eventFilter(self, obj, event):
        if (
            hasattr(self, "root")
            and obj == self.root
            and event.type() == event.Type.Resize
        ):
            if hasattr(self, "aurora_bg") and self.aurora_bg:
                self.aurora_bg.resize(event.size())
            if hasattr(self, "confetti") and self.confetti:
                self.confetti.resize(event.size())
        if (
            hasattr(self, "content_scroll")
            and obj == self.content_scroll.viewport()
            and event.type() == event.Type.Resize
        ):
            if hasattr(self, "page_tools"):
                self.page_tools.reposition()
            if hasattr(self, "_content_sync_timer"):
                self._content_sync_timer.start(16)
        return super().eventFilter(obj, event)

    def show_toast(
        self,
        message: str,
        icon: str = "🎉",
        duration_ms: int = 4000,
        *,
        title: str = "",
        tone: str = "info",
        anchor_widget=None,
    ) -> None:
        from ui.components.animations import ToastNotification

        toast = ToastNotification(
            self.root,
            message,
            icon,
            duration_ms,
            title=title,
            tone=tone,
            anchor_widget=anchor_widget,
            theme=self.theme,
        )
        toast.show_toast()

    def show_dashboard_notice(
        self,
        title: str,
        message: str,
        icon: str,
        *,
        tone: str = "info",
        duration_ms: int = 7000,
    ) -> None:
        dashboard = getattr(self, "pages", {}).get("dashboard")
        anchor = getattr(dashboard, "config_btn", None)
        self.show_toast(
            message,
            icon,
            duration_ms,
            title=title,
            tone=tone,
            anchor_widget=anchor,
        )

    def play_click(self):
        self.click_sound.play()

    def show_confetti(self) -> None:
        self.confetti.resize(self.root.size())
        self.confetti.burst()

    def _shutdown_task_message(self) -> str:
        labels = self.background_task_registry.active_labels()
        if not labels:
            return "A concluir o encerramento seguro…"
        visible = ", ".join(labels[:3])
        if len(labels) > 3:
            visible += f" e mais {len(labels) - 3}"
        return f"A terminar em segurança: {visible}…"

    def _continue_shutdown_when_ready(self) -> None:
        if not self._closing:
            return
        if self.background_task_registry.has_active_tasks():
            self.statusBar().showMessage(self._shutdown_task_message())
            LiquidFinanceApp._update_shutdown_wait(self)
            if not self._shutdown_poll_timer.isActive():
                self._shutdown_poll_timer.start()
            return
        self._shutdown_poll_timer.stop()
        LiquidFinanceApp._dismiss_shutdown_wait(self)
        if getattr(self, "_shutdown_cancel_requested", False):
            self._abort_shutdown(
                "Encerramento cancelado pelo utilizador.", user_cancelled=True
            )
            return
        if not self._shutdown_close_scheduled:
            self._shutdown_close_scheduled = True
            QTimer.singleShot(0, self, self._resume_close_attempt)

    def _update_shutdown_wait(self) -> None:
        elapsed = time.monotonic() - getattr(self, "_shutdown_started_at", time.monotonic())
        dialog = getattr(self, "_shutdown_wait_dialog", None)
        if elapsed >= LiquidFinanceApp.SHUTDOWN_WARNING_SECONDS and not getattr(
            self, "_shutdown_warning_shown", False
        ):
            from ui.shutdown import ShutdownWaitDialog

            self._shutdown_warning_shown = True
            log_warning(
                "Shutdown warning deadline exceeded; pending work: %s",
                ", ".join(self.background_task_registry.active_labels()),
            )
            dialog = ShutdownWaitDialog()
            self._shutdown_wait_dialog = dialog
            dialog.cancel_shutdown.connect(self._cancel_shutdown_when_idle)
            dialog.request_task_cancellation.connect(self._request_shutdown_task_cancellation)
            dialog.show()
        if dialog is not None:
            cancelled = getattr(self, "_shutdown_cancel_requested", False)
            prefix = "Encerramento cancelado; a aguardar trabalhos: " if cancelled else ""
            dialog.update_pending(
                prefix + self._shutdown_task_message(), cancellation_pending=cancelled
            )

    def _request_shutdown_task_cancellation(self) -> None:
        # Close backups may write attachments as well as SQLite; never interrupt
        # them merely because their callable cannot observe cancellation safely.
        self.background_task_registry.cancel_all(include_shutdown_tasks=False)
        self.statusBar().showMessage("Cancelamento cooperativo pedido às tarefas.")

    def _cancel_shutdown_when_idle(self) -> None:
        self._shutdown_cancel_requested = True
        log_info("User cancelled shutdown; waiting for owned jobs to drain safely.")
        self._continue_shutdown_when_ready()

    def _dismiss_shutdown_wait(self) -> None:
        dialog = getattr(self, "_shutdown_wait_dialog", None)
        if dialog is not None:
            self._shutdown_wait_dialog = None
            dialog.hide()
            dialog.deleteLater()

    def _choose_close_backup_failure(self, detail: str) -> str:
        """The choice affects this close attempt, never the saved preference."""
        if os.environ.get("FINANCAS_PESSOAIS_TESTING") == "1":
            return "cancel"
        message = QMessageBox()
        message.setIcon(QMessageBox.Warning)
        message.setWindowTitle("Backup ao fechar — Finanças Pessoais")
        message.setTextFormat(Qt.PlainText)
        message.setText("Não foi possível criar o backup deste encerramento.")
        message.setInformativeText(
            "Os dados ainda serão confirmados antes de fechar. A preferência "
            "de backup mantém-se para os próximos encerramentos.\n\n" + detail
        )
        retry = message.addButton("Tentar novamente", QMessageBox.AcceptRole)
        skip = message.addButton("Fechar sem este backup", QMessageBox.DestructiveRole)
        cancel = message.addButton("Cancelar", QMessageBox.RejectRole)
        message.setDefaultButton(cancel)
        message.setEscapeButton(cancel)
        # The main data UI stays disabled, but this independent decision window
        # must remain interactive while the shutdown gate is active.
        message.exec()
        if message.clickedButton() is retry:
            return "retry"
        return "skip" if message.clickedButton() is skip else "cancel"

    def _resolve_close_backup_failure(self, detail: str) -> None:
        choice = self._choose_close_backup_failure(detail)
        log_warning("Close backup failure; user decision=%s: %s", choice, detail)
        if choice == "cancel":
            self._abort_shutdown(
                "O backup pedido para o encerramento falhou: " + detail,
                user_cancelled=True,
            )
            return
        self._shutdown_backup_error = ""
        self._shutdown_backup_started = choice == "skip"
        self._shutdown_backup_finished = choice == "skip"
        self._continue_shutdown_when_ready()

    def _resume_close_attempt(self) -> None:
        """Coalesce duplicate task-finished signals into one close event."""

        self._shutdown_close_scheduled = False
        if self._closing:
            self.close()

    def _save_shutdown_state(self) -> tuple[bool, str]:
        if self._shutdown_state_saved:
            return True, ""
        try:
            if not self.table_column_manager.flush():
                raise RuntimeError(
                    "não foi possível guardar a personalização de uma ou mais tabelas"
                )
            if hasattr(self, "pages"):
                for page in tuple(self.pages.values()):
                    persist_view = getattr(page, "persist_page_view_state", None)
                    if callable(persist_view) and not bool(persist_view()):
                        page_name = str(
                            getattr(page, "page_title", page.__class__.__name__)
                        )
                        raise RuntimeError(
                            f"não foi possível guardar a vista de {page_name}"
                        )
                    flush_pending = getattr(page, "flush_pending_state", None)
                    if callable(flush_pending) and not bool(flush_pending()):
                        page_name = str(
                            getattr(page, "page_title", page.__class__.__name__)
                        )
                        raise RuntimeError(
                            f"não foi possível guardar as preferências de {page_name}"
                        )
            is_max = bool(
                self.isMaximized() or (self.windowState() & Qt.WindowMaximized)
            )
            self.repo.set_setting("window_maximized", "1" if is_max else "0")
            if not is_max:
                self.repo.set_setting(
                    "window_geometry",
                    f"{self.x()},{self.y()},{self.width()},{self.height()}",
                )
            self.repo.commit()
        except Exception as exc:
            log_exception("Failed to persist all pending state on close", exc)
            return False, str(exc)
        self._shutdown_state_saved = True
        return True, ""

    def _finalize_database_shutdown(self) -> tuple[bool, str]:
        if self._database_closed:
            return True, ""
        try:
            self.statusBar().showMessage(
                "A confirmar e sincronizar os dados antes de fechar…"
            )
            self.repo.close()
        except Exception as exc:
            log_exception("Failed to finalize database shutdown", exc)
            if isinstance(exc, DataIntegrityError) or (
                isinstance(exc, sqlite3.DatabaseError)
                and getattr(exc, "sqlite_errorcode", 0) & 0xFF
                in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
            ):
                self._shutdown_integrity_error = True
            return False, str(exc)
        self._database_closed = True
        try:
            qapp = QApplication.instance()
            if qapp is not None:
                qapp.removeEventFilter(self.table_column_manager)
                qapp.removeEventFilter(self.money_input_privacy)
                qapp.removeEventFilter(self.tooltip_manager)
                qapp.removeEventFilter(self.accessibility_manager)
            self.table_column_manager.deactivate()
        except Exception as exc:
            # The database is already durably closed.  A stale UI filter must
            # not turn a successful persistence operation into a false failure.
            log_exception("Failed to detach global UI filters on close", exc)
        return True, ""

    def _enter_integrity_maintenance(self, detail: str) -> None:
        """Keep verified-corrupt data available for inspection, never new writes."""

        self._shutdown_integrity_error = True
        self.background_task_registry.begin_maintenance()
        self._database_maintenance_active = True
        try:
            self.repo.db.conn.execute("PRAGMA query_only=ON")
        except Exception as exc:
            log_exception("Failed to enforce read-only integrity maintenance", exc)
            # UI access also stays disabled if SQLite cannot enforce the gate.
            self.setEnabled(False)
        else:
            self.setEnabled(True)
        self.statusBar().showMessage(
            "Modo de manutenção: integridade por confirmar; gravações bloqueadas."
        )
        log_warning("Database remains in integrity maintenance: %s", detail)

    def _abort_shutdown(self, detail: str, *, user_cancelled: bool = False) -> None:
        """Keep the app open when persistence or the requested backup failed."""

        updater = getattr(self, "app_updates", None)
        if updater is not None:
            updater.cancel_pending()
        self._shutdown_poll_timer.stop()
        self._closing = False
        self._shutdown_started = False
        self._shutdown_state_saved = False
        self._shutdown_backup_started = False
        self._shutdown_backup_finished = False
        self._shutdown_backup_error = ""
        self._shutdown_close_scheduled = False
        self._shutdown_started_at = 0.0
        self._shutdown_warning_shown = False
        self._shutdown_cancel_requested = False
        LiquidFinanceApp._dismiss_shutdown_wait(self)
        if getattr(self, "_shutdown_integrity_error", False):
            self._enter_integrity_maintenance(detail)
            if os.environ.get("FINANCAS_PESSOAIS_TESTING") != "1":
                QMessageBox.critical(
                    self, APP_TITLE,
                    "A integridade da base de dados não foi confirmada. "
                    "As gravações e tarefas automáticas permanecem bloqueadas.\n\n"
                    "Preserva o perfil atual e utiliza um backup verificado para recuperação.\n\n"
                    f"Detalhe: {detail}",
                )
            return
        try:
            self.background_task_registry.resume_after_failed_shutdown()
        except Exception as exc:
            log_exception("Failed to resume tasks after aborted shutdown", exc)
        try:
            dashboard = (
                self.pages.existing("dashboard") if hasattr(self, "pages") else None
            )
            if dashboard is not None and hasattr(dashboard, "resume_data_access"):
                dashboard.resume_data_access()
        except Exception as exc:
            log_exception("Failed to resume dashboard after aborted shutdown", exc)
        self.setEnabled(True)
        if hasattr(self, "auto_backup_timer"):
            self.auto_backup_timer.start(60 * 1000)
        self.statusBar().showMessage(
            "Encerramento cancelado pelo utilizador." if user_cancelled else
            "Encerramento cancelado: os dados não foram confirmados.",
            8000,
        )
        if user_cancelled:
            return
        # FINANCAS_PESSOAIS_TESTING is exported by tests/conftest.py for the
        # whole pytest session.  A modal here blocks the GUI event loop and,
        # under session state pollution, no test can ever dismiss it -> the
        # run hangs forever.  Log the detail instead and keep the window open
        # (the protective behaviour is fully preserved).
        if os.environ.get("FINANCAS_PESSOAIS_TESTING") != "1" or not getattr(
            self, "_suppress_error_modals_in_testing", True
        ):
            QMessageBox.critical(
                self,
                APP_TITLE,
                "A aplicação não foi fechada para proteger os teus dados.\n\n"
                "Corrige a causa indicada e tenta fechar novamente.\n\n"
                f"Detalhe: {detail}",
            )
        else:
            log_warning(
                "Shutdown aborted (testing mode, modal suppressed): %s",
                detail,
            )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        history_page = self.pages.existing("historico") if hasattr(self, "pages") else None
        prepare_to_leave = getattr(history_page, "prepare_to_leave", None)
        if callable(prepare_to_leave) and not prepare_to_leave("close"):
            updater = getattr(self, "app_updates", None)
            if updater is not None:
                updater.cancel_pending()
            event.ignore()
            return
        self._closing = True
        LiquidFinanceApp._finish_page_transition(self)
        for timer_name in ("auto_backup_timer", "subscription_reminder_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None and timer.isActive():
                timer.stop()

        if not self._shutdown_started:
            self._shutdown_started = True
            self._shutdown_started_at = time.monotonic()
            self._shutdown_warning_shown = False
            self._shutdown_cancel_requested = False
            self.setEnabled(False)
            try:
                dashboard = (
                    self.pages.existing("dashboard") if hasattr(self, "pages") else None
                )
                if dashboard is not None and hasattr(dashboard, "shutdown_workers"):
                    dashboard.shutdown_workers(timeout_ms=1000)

            except Exception as exc:
                log_exception("Failed to stop dashboard workers on close", exc)
            self.background_task_registry.begin_shutdown()
            # BUG-AUD-C142: begin_shutdown() only requests cooperative
            # cancellation.  Before the database connection is finalized the
            # registered QThreads must actually return, otherwise a running
            # Monte Carlo/import job keeps issuing queries against a closed
            # SQLite connection.  wait_for_idle() pumps non-user-input events
            # while waiting so the window stays responsive.
            try:
                self.background_task_registry.wait_for_idle(timeout_ms=1000)
            except Exception as exc:
                log_exception("Failed to wait for background workers on close", exc)

        if self.background_task_registry.has_active_tasks():
            self.statusBar().showMessage(self._shutdown_task_message())
            LiquidFinanceApp._update_shutdown_wait(self)
            if not self._shutdown_poll_timer.isActive():
                self._shutdown_poll_timer.start()
            event.ignore()
            return

        if getattr(self, "_shutdown_cancel_requested", False):
            event.ignore()
            self._abort_shutdown(
                "Encerramento cancelado pelo utilizador.", user_cancelled=True
            )
            return

        state_saved, state_error = self._save_shutdown_state()
        if not state_saved:
            event.ignore()
            self._abort_shutdown(state_error)
            return

        if not self._shutdown_backup_started:
            self._shutdown_backup_started = True
            try:
                backup_enabled = bool(
                    self.backup_service.preferences().get(
                        "backup_on_close",
                        False,
                    )
                )
            except Exception as exc:
                log_exception("Failed to read backup-on-close preference", exc)
                event.ignore()
                self._resolve_close_backup_failure(
                    "Não foi possível confirmar a configuração do backup ao fechar: "
                    f"{exc}"
                )
                return
            if backup_enabled:
                database_file = self.repo.db.path
                self._shutdown_backup_error = ""
                started = self.close_backup_task.start(
                    lambda: BackupService.run_close_backup_for_database(
                        database_file,
                        silent=False,
                    )
                )
                if started:
                    self.statusBar().showMessage(
                        "A criar e verificar o backup antes de fechar…"
                    )
                    event.ignore()
                    return
                log_warning(
                    "Close backup could not start in the background; "
                    "the application will remain open."
                )
                event.ignore()
                self._resolve_close_backup_failure(
                    "O backup pedido para o encerramento não conseguiu iniciar."
                )
                return
            self._shutdown_backup_finished = True

        if self.close_backup_task.is_running() or not self._shutdown_backup_finished:
            self.statusBar().showMessage(
                "A criar e verificar o backup antes de fechar…"
            )
            if not self._shutdown_poll_timer.isActive():
                self._shutdown_poll_timer.start()
            event.ignore()
            return

        if self._shutdown_backup_error:
            event.ignore()
            self._resolve_close_backup_failure(self._shutdown_backup_error)
            return

        finalized, finalization_error = self._finalize_database_shutdown()
        if not finalized:
            event.ignore()
            self._abort_shutdown(finalization_error)
            return

        self._shutdown_poll_timer.stop()
        LiquidFinanceApp._dismiss_shutdown_wait(self)
        super().closeEvent(event)

    def reload_data_services(self) -> None:
        """Recreate data-bound services after replacing the SQLite database."""
        if not self._database_restore_prepared:
            self._finish_database_maintenance()
            return
        dashboard = self.pages.get("dashboard") if hasattr(self, "pages") else None
        if dashboard is not None and hasattr(dashboard, "shutdown_workers"):
            dashboard.shutdown_workers()
        try:
            self.repo.db.conn.close()
        except Exception as exc:
            log_exception("Failed to close database before reloading services", exc)
        self.repo = Repo()
        self._shutdown_integrity_error = False
        self._database_closed = False
        try:
            self.repo.record_financial_state_snapshot()
        except Exception as exc:
            log_exception("Failed to capture restored financial state", exc)
        self.table_column_manager.repo = self.repo
        self.change_journal_service = ChangeJournalService(self.repo)
        self.payments_service = PaymentsService(
            self.repo,
            self.change_journal_service,
        )
        self.analytics_service = AnalyticsService(
            self.repo,
            payments_service=self.payments_service,
        )
        self.backup_service = BackupService(self.repo)
        self.document_service = DocumentService(self.repo)
        self.import_service = ImportService(self.repo, self.change_journal_service)
        self.ai_service = AIService(
            self.repo,
            backup_service=self.backup_service,
            analytics_service=self.analytics_service,
            payments_service=self.payments_service,
        )
        # A restored legacy database may still contain the old plaintext key.
        self.ai_service.preferences()
        self._database_restore_prepared = False
        self._finish_database_maintenance(resume_pages=False)
        for page in self.pages.values():
            page.repo = self.repo
            if hasattr(page, "payments_service"):
                page.payments_service = self.payments_service
            if hasattr(page, "import_service"):
                page.import_service = self.import_service
            if hasattr(page, "analytics_service"):
                page.analytics_service = self.analytics_service
            if hasattr(page, "ai_service"):
                page.ai_service = self.ai_service
            if hasattr(page, "backup_service"):
                page.backup_service = self.backup_service
            if hasattr(page, "change_journal_service"):
                page.change_journal_service = self.change_journal_service
            if hasattr(page, "resume_data_access"):
                page.resume_data_access()
        self._dirty_page_keys.update(self.page_factories)
        self.refresh_undo_actions()

    def _finish_database_maintenance(self, *, resume_pages: bool = True) -> None:
        if getattr(self, "_shutdown_integrity_error", False):
            self.background_task_registry.begin_maintenance()
            self._database_maintenance_active = True
            return
        if self._database_maintenance_active:
            self.background_task_registry.end_maintenance()
            self._database_maintenance_active = False
        if not resume_pages or not hasattr(self, "pages"):
            return
        for page in self.pages.values():
            if hasattr(page, "resume_data_access"):
                page.resume_data_access()

    def prepare_database_restore(self) -> None:
        """Drain every background task and close SQLite before a restore."""

        self._database_restore_prepared = False
        self.background_task_registry.begin_maintenance()
        self._database_maintenance_active = True
        try:
            dashboard = (
                self.pages.existing("dashboard") if hasattr(self, "pages") else None
            )
            if dashboard is not None and hasattr(dashboard, "shutdown_workers"):
                dashboard.shutdown_workers(timeout_ms=0)
            if not self.background_task_registry.wait_for_idle(15_000):
                labels = ", ".join(self.background_task_registry.active_labels())
                raise RuntimeError(
                    "Ainda existem tarefas a utilizar os dados"
                    + (f": {labels}." if labels else ".")
                    + " Aguarda alguns segundos e tenta novamente."
                )
            self.repo.db.conn.commit()
            self.repo.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.repo.db.conn.close()
            self._database_restore_prepared = True
        except Exception:
            self._finish_database_maintenance()
            raise

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        if hasattr(self, "_chrome_reflow_timer"):
            self._chrome_reflow_timer.start(0)
        if hasattr(self, "_content_sync_timer"):
            self._content_sync_timer.start(0)
        QTimer.singleShot(50, self.sync_content_size)
        QTimer.singleShot(150, self.sync_content_size)

    def changeEvent(self, event):  # type: ignore[override]
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if hasattr(self, "_chrome_reflow_timer"):
                self._chrome_reflow_timer.start(0)
            if hasattr(self, "_content_sync_timer"):
                self._content_sync_timer.start(0)
            QTimer.singleShot(50, self.sync_content_size)
            QTimer.singleShot(150, self.sync_content_size)

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        if hasattr(self, "_chrome_reflow_timer"):
            self._chrome_reflow_timer.start(32)
        if hasattr(self, "_content_sync_timer"):
            self._content_sync_timer.start()

    def sync_content_size(self) -> None:
        # Deferred resize callbacks can outlive the native window/children when
        # another nested event loop (for example PDF rendering) runs after close.
        if getattr(self, "_closing", False) or not is_qt_valid(self):
            return
        stack = getattr(self, "stack", None)
        scroll = getattr(self, "content_scroll", None)
        if not is_qt_valid(stack) or not is_qt_valid(scroll):
            return
        try:
            page = stack.currentWidget()
            viewport = scroll.viewport()
            if not is_qt_valid(page) or not is_qt_valid(viewport):
                return

            viewport_h = viewport.height()
            viewport_w = max(1, viewport.width())

            page.setMinimumWidth(0)
            page.setMaximumWidth(viewport_w)
            self.stack.setMinimumWidth(0)
            self.stack.setMaximumWidth(viewport_w)

            # Trigger responsive reflow on the active page if available
            if hasattr(page, "_schedule_responsive_reflow"):
                page._schedule_responsive_reflow()
            elif hasattr(page, "reflow_dashboard"):
                page.reflow_dashboard()

            # Force the stack to match the current page's content sizeHint
            hint_h = max(
                page.sizeHint().height(),
                page.minimumSizeHint().height(),
                page.minimumHeight(),
            )
            final_h = max(hint_h, viewport_h)

            # Keep the active page at its calculated content height. Leaving the
            # maximum unbounded lets vertically-expanding chart cards absorb
            # surplus space and become disproportionately tall.
            self.stack.setMinimumHeight(final_h)
            self.stack.setMaximumHeight(final_h)

            # Update bounds
            self.stack.updateGeometry()
            self.content_scroll.updateGeometry()
        except RuntimeError as exc:
            log_exception("Failed to synchronize content geometry", exc)

    def build_sidebar(self) -> None:

        self.side_layout.addSpacing(6)
        brand = QWidget()
        brand.setObjectName("SidebarBrand")
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(7, 4, 7, 8)
        brand_layout.setSpacing(4)
        self.brand_label = QLabel("💶  Finanças")
        self.brand_label.setObjectName("SidebarBrandName")
        self.version_badge = QLabel(APP_VERSION_LABEL)
        self.version_badge.setObjectName("AppVersion")
        self.version_badge.setAlignment(Qt.AlignCenter)
        self.version_badge.setMinimumWidth(64)
        self.version_badge.setMaximumWidth(76)
        self.version_badge.setToolTip(
            f"Liquid Glass {APP_VERSION_LABEL} · lançamento {RELEASE_VERSION}"
        )
        brand_layout.addWidget(self.brand_label)
        brand_layout.addWidget(self.version_badge, 0, Qt.AlignLeft)
        self.side_layout.addWidget(brand)

        self.nav_container = QWidget()
        self.nav_container.setObjectName("NavContainer")
        self.nav_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.nav_placeholder = QVBoxLayout(self.nav_container)
        self.nav_placeholder.setContentsMargins(0, 0, 0, 0)
        self.nav_placeholder.setSpacing(4)

        self.nav_scroll = QScrollArea()
        self.nav_scroll.setObjectName("SidebarScroll")
        self.nav_scroll.setFrameShape(QFrame.NoFrame)
        self.nav_scroll.setWidgetResizable(True)
        self.nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.nav_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.nav_scroll.setWidget(self.nav_container)
        self.side_layout.addWidget(self.nav_scroll, 1)

    def set_sidebar_compact(
        self,
        compact: bool,
        persist: bool = True,
        *,
        contextual: bool = False,
    ) -> None:
        self._sidebar_compact = bool(compact)
        self._sidebar_contextual = bool(compact and contextual)
        if persist:
            self._sidebar_user_compact = self._sidebar_compact
            self.repo.set_setting(
                "sidebar_compact", "1" if self._sidebar_compact else "0"
            )
        if self._sidebar_compact:
            width = 142 if self._sidebar_contextual else 64
            self.sidebar.setMinimumWidth(width)
            self.sidebar.setMaximumWidth(width)
            if hasattr(self, "side_layout"):
                self.side_layout.setContentsMargins(
                    4 if self._sidebar_contextual else 4,
                    8,
                    4 if self._sidebar_contextual else 4,
                    8,
                )
            self.brand_label.setVisible(self._sidebar_contextual)
            self.version_badge.setText(
                APP_VERSION_LABEL if self._sidebar_contextual else "v3"
            )
            self.version_badge.setMinimumWidth(64 if self._sidebar_contextual else 38)
            self.version_badge.setMaximumWidth(76 if self._sidebar_contextual else 38)
            for btn in self.nav_buttons.values():
                btn.setText(
                    btn.property("context_text")
                    if self._sidebar_contextual
                    else btn.property("icon_text")
                )
            for header in getattr(self, "nav_group_headers", {}).values():
                header.hide()
            for group in getattr(self, "nav_group_widgets", {}).values():
                group.show()
                if group.layout():
                    group.layout().setContentsMargins(0, 0, 0, 2)
        else:
            # Full labels include entries such as "Importação inteligente";
            # reserve enough room for them instead of clipping the last word.
            self.sidebar.setMinimumWidth(224)
            self.sidebar.setMaximumWidth(236)
            if hasattr(self, "side_layout"):
                self.side_layout.setContentsMargins(8, 12, 8, 12)
            self.brand_label.show()
            self.version_badge.setText(APP_VERSION_LABEL)
            self.version_badge.setMinimumWidth(64)
            self.version_badge.setMaximumWidth(76)
            for btn in self.nav_buttons.values():
                btn.setText(btn.property("full_text"))
            for name, header in getattr(self, "nav_group_headers", {}).items():
                header.show()
                group = getattr(self, "nav_group_widgets", {}).get(name)
                if group is not None:
                    group.setVisible(name not in self._collapsed_nav_groups())
                    if group.layout():
                        group.layout().setContentsMargins(4, 0, 0, 2)

    def toggle_sidebar(self) -> None:
        self._sidebar_manual_override = True
        self.set_sidebar_compact(not self._sidebar_compact, contextual=False)

    def _apply_responsive_chrome(self) -> None:
        """Protect useful content width while preserving explicit user choices."""

        if not hasattr(self, "sidebar"):
            return
        compact_topbar = self.width() < self.COMPACT_CHROME_BREAKPOINT
        if not self._sidebar_manual_override:
            desired_sidebar = True if compact_topbar else self._sidebar_user_compact
            contextual = bool(desired_sidebar and compact_topbar)
            if (
                desired_sidebar != self._sidebar_compact
                or contextual != self._sidebar_contextual
            ):
                self.set_sidebar_compact(
                    desired_sidebar,
                    persist=False,
                    contextual=contextual,
                )
        self._apply_topbar_density(compact_topbar)

    def _apply_topbar_density(self, compact: bool) -> None:
        if not hasattr(self, "quick_btn"):
            return
        self._topbar_compact = bool(compact)
        privacy_icon = "🙈" if self.theme.privacy else "👁"
        if self._topbar_compact:
            actions = (
                (self.refresh_btn, "🔄", "Atualizar"),
                (self.privacy_btn, privacy_icon, "Privacidade"),
                (self.quick_btn, "＋", "Novo movimento"),
            )
            for button, text, accessible_name in actions:
                button.setText(text)
                button.setAccessibleName(accessible_name)
                button.setFixedWidth(44)
            self.search.setPlaceholderText("🔍  Pesquisar... (Ctrl+K)")
            return

        for button in (self.refresh_btn, self.privacy_btn, self.quick_btn):
            button.setMinimumWidth(0)
            button.setMaximumWidth(16777215)
        self.refresh_btn.setText("🔄  Atualizar")
        self.privacy_btn.setText(
            "🙈  Valores ocultos" if self.theme.privacy else "👁  Privacidade"
        )
        self.quick_btn.setText("＋  Novo movimento")
        self.search.setPlaceholderText(
            "🔍  Pesquisar movimentos, categorias, páginas... (Ctrl+K)"
        )

    def _setting_list(self, key: str, default: list[str]) -> list[str]:
        try:
            value = json.loads(str(self.repo.setting(key, "") or ""))
        except TypeError, ValueError, json.JSONDecodeError:
            value = default
        return (
            [str(item) for item in value] if isinstance(value, list) else list(default)
        )

    def _collapsed_nav_groups(self) -> set[str]:
        return set(
            self._setting_list(
                "sidebar_collapsed_groups",
                [],
            )
        )

    def _toggle_nav_group(self, name: str) -> None:
        collapsed = self._collapsed_nav_groups()
        if name in collapsed:
            collapsed.remove(name)
        else:
            collapsed.add(name)
        self.repo.set_setting(
            "sidebar_collapsed_groups",
            json.dumps(sorted(collapsed), ensure_ascii=False),
        )
        group = self.nav_group_widgets.get(name)
        if group is not None:
            group.setVisible(name not in collapsed)
        header = self.nav_group_headers.get(name)
        if header is not None:
            header.setText(f"{'▸' if name in collapsed else '▾'}  {name}")

    def _toggle_nav_favorite(self, key: str) -> None:
        favorites = self._setting_list("sidebar_favorites", [])
        if key in favorites:
            favorites.remove(key)
        else:
            favorites.append(key)
        self.repo.set_setting(
            "sidebar_favorites",
            json.dumps(favorites, ensure_ascii=False),
        )
        self.populate_nav_buttons()
        self.set_sidebar_compact(
            self._sidebar_compact,
            persist=False,
            contextual=self._sidebar_contextual,
        )

    def populate_nav_buttons(self) -> None:
        while self.nav_placeholder.count():
            item = self.nav_placeholder.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.nav_buttons.clear()
        self.nav_group_headers: dict[str, QPushButton] = {}
        self.nav_group_widgets: dict[str, QWidget] = {}

        groups = navigation_groups()
        labels = {key: label for _name, items in groups for key, label in items}
        favorites = [
            key for key in self._setting_list("sidebar_favorites", []) if key in labels
        ]
        collapsed = self._collapsed_nav_groups()
        current_widget = self.stack.currentWidget()
        current_key = next(
            (key for key, page in self.pages.items() if page is current_widget),
            "",
        )
        current_navigation_key = navigation_key_for_page(current_key)

        def create_button(key: str, label: str, favorite: bool) -> QPushButton:
            btn = QPushButton(label)
            btn.setObjectName("Nav")
            btn.setCursor(Qt.PointingHandCursor)
            full_text = f"★ {label}" if favorite else label
            btn.setProperty("full_text", full_text)
            # Preserve the complete emoji (including variation selectors) in compact mode.
            btn.setProperty("icon_text", label.split("  ", 1)[0])
            btn.setProperty(
                "context_text",
                contextual_navigation_label(key) or label.replace("  ", " "),
            )
            btn.setText(full_text)
            btn.setMinimumHeight(34)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setToolTip(
                label.replace("  ", " ")
                + " · botão direito para adicionar/remover dos favoritos"
            )
            btn.setProperty(
                "active",
                "true" if key == current_navigation_key else "false",
            )

            # Ambient Glow Effect
            shadow = QGraphicsDropShadowEffect(btn)
            shadow.setBlurRadius(0)
            shadow.setColor(QColor("#00E5FF"))
            shadow.setOffset(0, 0)
            btn.setGraphicsEffect(shadow)
            btn.setProperty("glow", shadow)

            btn.clicked.connect(lambda _=False, k=key: self.goto(k))
            btn.setContextMenuPolicy(Qt.CustomContextMenu)

            def open_menu(position, page_key=key, is_favorite=favorite, button=btn):
                menu = QMenu(button)
                action = menu.addAction(
                    "★ Remover dos favoritos"
                    if is_favorite
                    else "☆ Adicionar aos favoritos"
                )
                if menu.exec(button.mapToGlobal(position)) == action:
                    self._toggle_nav_favorite(page_key)

            btn.customContextMenuRequested.connect(open_menu)
            self.nav_buttons[key] = btn
            return btn

        ordered_groups = (
            [("★ Favoritos", [(key, labels[key]) for key in favorites])]
            if favorites
            else []
        ) + groups
        favorite_set = set(favorites)
        for group_name, items in ordered_groups:
            filtered = [
                (key, label)
                for key, label in items
                if group_name == "★ Favoritos" or key not in favorite_set
            ]
            if not filtered:
                continue
            is_collapsed = group_name in collapsed
            header = QPushButton(f"{'▸' if is_collapsed else '▾'}  {group_name}")
            header.setObjectName("NavGroup")
            header.setCursor(Qt.PointingHandCursor)
            header.clicked.connect(
                lambda _checked=False, name=group_name: self._toggle_nav_group(name)
            )
            self.nav_group_headers[group_name] = header
            self.nav_placeholder.addWidget(header)
            container = QWidget()
            container.setObjectName("NavGroupItems")
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0 if getattr(self, "_sidebar_compact", False) else 4, 0, 0, 2)
            layout.setSpacing(3)
            for key, label in filtered:
                layout.addWidget(create_button(key, label, key in favorite_set))
            container.setVisible(not is_collapsed)
            self.nav_group_widgets[group_name] = container
            self.nav_placeholder.addWidget(container)
        self.nav_placeholder.addStretch(1)

    def build_topbar(self, content: QVBoxLayout) -> None:
        top = QHBoxLayout()
        top.setSpacing(12)

        self.toggle_btn = QPushButton("☰")
        self.toggle_btn.setFixedSize(36, 36)
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_btn.setStyleSheet(
            "font-size: 18px; border-radius: 18px; background: transparent;"
        )
        self.toggle_btn.clicked.connect(self.toggle_sidebar)
        top.addWidget(self.toggle_btn)

        self.search = QLineEdit()
        self.search.setObjectName("GlobalSearch")
        self.search.setPlaceholderText(
            "🔍  Pesquisar movimentos, categorias, páginas... (Ctrl+K)"
        )
        self.search.setToolTip(
            "Omnibar: pesquisa global e navegação rápida.\nAtalhos: Ctrl+K ou Ctrl+F"
        )
        self.search.returnPressed.connect(self.global_search)

        self.quick_btn = QPushButton("＋  Novo movimento")
        self.quick_btn.setObjectName("Primary")
        self.quick_btn.setAccessibleName("Novo movimento")
        self.quick_btn.setToolTip("Adicionar novo movimento  [Ctrl+N]")
        self.quick_btn.clicked.connect(self.quick_add)

        self.refresh_btn = QPushButton("🔄  Atualizar")
        self.refresh_btn.setAccessibleName("Atualizar")
        self.refresh_btn.setToolTip("Atualizar página atual  [F5 / Ctrl+R]")
        self.refresh_btn.clicked.connect(self.refresh_all)

        self.privacy_btn = QPushButton("👁  Privacidade")
        self.privacy_btn.setAccessibleName("Privacidade")
        self.privacy_btn.setToolTip("Esconder/mostrar todos os valores monetários")
        self.privacy_btn.clicked.connect(self.toggle_privacy)

        self.undo_btn = QPushButton("↶")
        self.undo_btn.setObjectName("Secondary")
        self.undo_btn.setFixedSize(40, 40)
        self.undo_btn.setAccessibleName("Desfazer alteração mais recente")
        self.undo_btn.clicked.connect(self.undo_latest_change)
        self.history_btn = QPushButton("🕘")
        self.history_btn.setObjectName("Secondary")
        self.history_btn.setFixedSize(40, 40)
        self.history_btn.setAccessibleName("Abrir histórico de alterações")
        self.history_btn.setToolTip("Abrir histórico de alterações")
        self.history_btn.clicked.connect(self.open_change_history)

        top.addWidget(self.search, 1)

        top.addWidget(self.undo_btn)
        top.addWidget(self.history_btn)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.privacy_btn)
        top.addWidget(self.quick_btn)
        content.addLayout(top)
        self.refresh_undo_actions()

    def refresh_undo_actions(self) -> None:
        if not hasattr(self, "undo_btn"):
            return
        latest = self.change_journal_service.latest_available()
        self.undo_btn.setEnabled(latest is not None)
        self.undo_btn.setToolTip(
            f"Desfazer: {latest.get('label')}"
            if latest is not None
            else "Não existem alterações por desfazer"
        )

    def open_change_history(self) -> None:
        from ui.change_history import ChangeHistoryDialog

        ChangeHistoryDialog(self).exec()
        self.refresh_undo_actions()

    def undo_latest_change(self) -> bool:
        latest = self.change_journal_service.latest_available()
        if latest is None:
            QMessageBox.information(
                self,
                APP_TITLE,
                "Não existem alterações por desfazer.",
            )
            self.refresh_undo_actions()
            return False
        label = str(latest.get("label") or "alteração")
        answer = QMessageBox.question(
            self,
            APP_TITLE,
            f"Desfazer a alteração mais recente?\n\n{label}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return False
        try:
            entry = self.change_journal_service.undo_latest()
        except Exception as exc:
            log_exception("Failed to undo latest change", exc)
            QMessageBox.critical(
                self,
                APP_TITLE,
                f"Não foi possível desfazer a alteração:\n{exc}",
            )
            return False
        if entry is None:
            return False
        self.document_service.invalidate_audit_cache()
        self.notify_data_changed(f"Desfeito: {label}")
        self.refresh_undo_actions()
        self.statusBar().showMessage(f"Desfeito: {label}", 5000)
        return True

    def apply_theme(self, first: bool = False) -> None:
        previous_popup_theme = (
            bool(getattr(self.theme, "dark", False)),
            str(getattr(self.theme, "primary_color", "")),
        )
        self.theme = Theme(
            dark=self.repo.setting("liquid_dark", "0") == "1",
            privacy=self.repo.setting("liquid_privacy", "0") == "1",
            primary_color_name=self.repo.setting("liquid_primary", "Azul"),
        )
        popup_theme_changed = previous_popup_theme != (
            self.theme.dark,
            self.theme.primary_color,
        )
        self.root.set_theme(self.theme)
        self.money_input_privacy.set_privacy(self.theme.privacy)
        self.tooltip_manager.set_accent(self.theme.primary_color)
        if hasattr(self, "aurora_bg"):
            self.aurora_bg.set_dark_mode(self.theme.dark)
        self.setStyleSheet(self.theme.stylesheet())
        if not first or popup_theme_changed:
            apply_tooltip_theme(self.theme)
        if hasattr(self, "privacy_btn"):
            if self.theme.privacy:
                self.privacy_btn.setText("🙈  Valores ocultos")
                self.privacy_btn.setToolTip("Mostrar todos os valores monetários")
            else:
                self.privacy_btn.setText("👁  Privacidade")
                self.privacy_btn.setToolTip("Esconder todos os valores monetários")
            self._apply_topbar_density(self.width() < self.COMPACT_CHROME_BREAKPOINT)
        try:
            self.content_scroll.viewport().setStyleSheet("background: transparent;")
            self.stack.setStyleSheet("background: transparent;")
        except Exception as exc:
            log_exception("Failed to apply transparent content styles", exc)
        for date_edit in self.findChildren(QDateEdit):
            try:
                prepare_date_edit(date_edit, self.theme)
            except Exception as exc:
                log_exception("Failed to apply date field theme", exc)
        for combo in self.findChildren(QComboBox):
            try:
                apply_combo_theme(combo, self.theme)
            except Exception as exc:
                log_exception("Failed to apply combo theme", exc)
        for calendar in self.findChildren(QCalendarWidget):
            try:
                apply_calendar_theme(calendar, self.theme)
            except Exception as exc:
                log_exception("Failed to apply calendar theme", exc)
        for page in self.pages.values():
            update_export_labels = getattr(page, "update_export_privacy_labels", None)
            if callable(update_export_labels):
                update_export_labels()

        if not first:
            self.refresh_all()

    def apply_font_scale(self) -> None:
        val = self.repo.setting("global_font_scale", "Normal (100%)")
        scale_map = {
            "Pequeno (90%)": 9,
            "Normal (100%)": 10,
            "Grande (110%)": 11,
            "Muito Grande (125%)": 12,
        }
        pt_size = scale_map.get(str(val), 10)
        font = QApplication.instance().font()
        font.setPointSize(pt_size)
        QApplication.instance().setFont(font)
        self.setFont(font)

        for page in self.pages.values():
            if hasattr(page, "reflow_responsive_grids"):
                page.reflow_responsive_grids()
        self.update()
        QTimer.singleShot(0, self.sync_content_size)
        QTimer.singleShot(120, self.sync_content_size)

    def open_command_palette(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Ir para")
        dialog.setMinimumSize(460, 520)
        dialog.setStyleSheet(self.styleSheet())
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("Pesquisar página...")
        results = QListWidget()
        layout.addWidget(search)
        layout.addWidget(results, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Close)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def populate() -> None:
            query = search.text().strip().casefold()
            results.clear()
            for route in all_navigation_items():
                key = route.key
                label = route.label.replace("  ", " ")
                if (
                    query
                    and query not in label.casefold()
                    and query not in key.casefold()
                ):
                    continue
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, key)
                results.addItem(item)
            if results.count():
                results.setCurrentRow(0)

        results.itemDoubleClicked.connect(lambda *_: dialog.accept())
        search.returnPressed.connect(dialog.accept)
        search.textChanged.connect(populate)
        populate()
        search.setFocus()
        if dialog.exec() != QDialog.Accepted:
            return
        item = results.currentItem()
        if item is not None:
            self.goto(str(item.data(Qt.UserRole)))

    def toggle_privacy(self) -> None:
        self.repo.set_setting("liquid_privacy", "0" if self.theme.privacy else "1")
        self.apply_theme()

    def _finish_page_transition(
        self, expected_animation: QPropertyAnimation | None = None
    ) -> None:
        """Release only the effect owned by the current navigation transition."""
        animation = getattr(self, "_transition_anim", None)
        if expected_animation is not None and animation is not expected_animation:
            return
        widget = getattr(self, "_transition_widget", None)
        effect = getattr(self, "_transition_effect", None)
        self._transition_anim = None
        self._transition_widget = None
        self._transition_effect = None
        if animation is not None:
            try:
                animation.stop()
                # Break the finished callback's references before deferred
                # destruction; the callback captures this window and animation.
                animation.finished.disconnect()
                animation.setTargetObject(None)
                animation.deleteLater()
            except RuntimeError:
                # A page can be destroyed before its transition completes.
                pass
        if widget is not None and effect is not None:
            try:
                if widget.graphicsEffect() is effect:
                    widget.setGraphicsEffect(None)
            except RuntimeError:
                pass

    def goto(self, key: str) -> None:
        if key not in self.pages:
            return

        previous_key = self._current_page_key
        if previous_key and previous_key != key:
            previous_page = self.pages.existing(previous_key)
            prepare_to_leave = getattr(previous_page, "prepare_to_leave", None)
            if callable(prepare_to_leave) and not prepare_to_leave("navigation"):
                return
            self._page_scroll_positions[previous_key] = (
                self.content_scroll.verticalScrollBar().value()
            )
            previous_page = self.pages.existing(previous_key)
            persist_view = getattr(previous_page, "persist_page_view_state", None)
            if callable(persist_view):
                persist_view()
        new_widget = self.pages[key]
        self._sync_dashboard_sticky_filters(key)
        if self.stack.currentWidget() == new_widget:
            self._current_page_key = key
            # Re-selecting the active page is also used after a window resize.
            # Refresh its bounds on the next event-loop turn instead of
            # retaining a wider geometry inherited from the previous viewport.
            QTimer.singleShot(0, self.sync_content_size)
            QTimer.singleShot(0, self._update_dashboard_sticky_summary)
            QTimer.singleShot(0, self._update_page_tools)
            return

        self.stack.setCurrentWidget(new_widget)
        self._current_page_key = key
        ws = workspace_for_page(key)
        if ws is not None and hasattr(new_widget, "configure_workspace_navigation"):
            new_widget.configure_workspace_navigation(ws, key)

        # stop() does not emit finished: explicitly release the previous
        # page's effect before starting another transition.
        self._finish_page_transition()
        eff = QGraphicsOpacityEffect(new_widget)
        new_widget.setGraphicsEffect(eff)
        fade = QPropertyAnimation(eff, b"opacity", new_widget)
        fade.setDuration(160)
        fade.setStartValue(0.15)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.OutQuad)

        self._transition_anim = fade
        self._transition_widget = new_widget
        self._transition_effect = eff
        fade.finished.connect(lambda: self._finish_page_transition(fade))
        fade.start()

        active_navigation_key = navigation_key_for_page(key)
        for k, b in self.nav_buttons.items():
            is_active = k == active_navigation_key
            b.setProperty("active", "true" if is_active else "false")
            b.style().unpolish(b)
            b.style().polish(b)

            shadow = b.property("glow")
            if shadow:
                shadow.setBlurRadius(20 if is_active else 0)
                shadow.setColor(QColor("#00E5FF" if self.theme.dark else "#00B0FF"))
        last_refresh = self._page_refreshed_at.get(key, 0.0)
        needs_refresh = (
            key in self._dirty_page_keys
            or datetime.now().timestamp() - last_refresh > 60.0
        )
        if needs_refresh:
            new_widget.begin_data_refresh()
            new_widget.refresh()
            self._dirty_page_keys.discard(key)
            refreshed = datetime.now()
            self._page_refreshed_at[key] = refreshed.timestamp()
        if hasattr(new_widget, "apply_card_preferences"):
            new_widget.apply_card_preferences()

        for i in range(self.stack.count()):
            w = self.stack.widget(i)
            if w == self.pages[key]:
                w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            else:
                w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.stack.updateGeometry()
        target_scroll = max(0, int(self._page_scroll_positions.get(key, 0)))
        self.content_scroll.verticalScrollBar().setValue(target_scroll)
        self._scroll_restore_generation += 1
        generation = self._scroll_restore_generation

        def restore_scroll_once() -> None:
            if generation != self._scroll_restore_generation:
                return
            self.sync_content_size()
            if self._current_page_key == key:
                self.content_scroll.verticalScrollBar().setValue(target_scroll)

        QTimer.singleShot(0, restore_scroll_once)
        QTimer.singleShot(0, self._update_dashboard_sticky_summary)
        QTimer.singleShot(0, self._update_page_tools)

    def _sync_dashboard_sticky_filters(self, key: str) -> None:
        dashboard = dict.get(self.pages, "dashboard")
        filters = getattr(dashboard, "filters_card", None)
        if dashboard is None or filters is None:
            self.dashboard_sticky_host.hide()
            return
        # The full control panel always remains inside the page.  Only its
        # compact textual summary becomes sticky after the controls scroll out.
        if dashboard.body.indexOf(filters) < 0:
            self.dashboard_sticky_layout.removeWidget(filters)
            dashboard.body.insertWidget(0, filters)
        if key != "dashboard":
            self.dashboard_sticky_host.hide()
            return
        self.refresh_dashboard_sticky_summary()

    def refresh_dashboard_sticky_summary(self) -> None:
        dashboard = dict.get(self.pages, "dashboard")
        summary = getattr(dashboard, "filters_summary", None)
        if dashboard is None or summary is None:
            self.dashboard_sticky_host.hide()
            return
        self.dashboard_sticky_summary.setText(summary.text())
        self.dashboard_sticky_summary.setAccessibleDescription(summary.text())
        QTimer.singleShot(0, self._update_dashboard_sticky_summary)

    def _update_dashboard_sticky_summary(
        self,
        value: int | None = None,
    ) -> None:
        if getattr(self, "_current_page_key", "") != "dashboard":
            self.dashboard_sticky_host.hide()
            return
        dashboard = dict.get(self.pages, "dashboard")
        filters = getattr(dashboard, "filters_card", None)
        if dashboard is None or filters is None:
            self.dashboard_sticky_host.hide()
            return
        try:
            scroll_value = (
                self.content_scroll.verticalScrollBar().value()
                if value is None
                else int(value)
            )
            filter_bottom = filters.mapTo(
                self.stack,
                QPoint(0, filters.height()),
            ).y()
            should_show = (
                self.content_scroll.verticalScrollBar().maximum() > 0
                and scroll_value >= max(1, filter_bottom - 10)
            )
        except RuntimeError:
            should_show = False
        self.dashboard_sticky_host.setVisible(should_show)

    def refresh_all(self) -> None:
        """Refresh the visible page and invalidate every other page lazily."""
        if hasattr(self, "analytics_service") and hasattr(
            self.analytics_service, "invalidate_caches"
        ):
            self.analytics_service.invalidate_caches()
        self._dirty_page_keys.update(self.page_factories)

        changed_at = datetime.now()
        for page in self.pages.values():
            page.mark_data_stale(changed_at)
        current = self.stack.currentWidget()
        current_key = next(
            (key for key, page in self.pages.items() if page is current),
            "",
        )
        if current_key and current is not None:
            try:
                current.begin_data_refresh()
                current.refresh()
                current.apply_card_preferences()
                self._dirty_page_keys.discard(current_key)
                refreshed = datetime.now()
                self._page_refreshed_at[current_key] = refreshed.timestamp()
            except Exception as exc:
                log_exception(f"Failed to refresh {current.__class__.__name__}", exc)
        self.statusBar().showMessage(
            f"Atualizado às {datetime.now().strftime('%H:%M:%S')}", 3500
        )

    def notify_data_changed(
        self, message: str = "Dados atualizados.", selected_tx_id: Optional[int] = None
    ) -> bool:
        """Commit pending DB work and refresh visible/derived data after a mutation."""
        if not self.repo.safe_commit("Failed to commit before data-change refresh"):
            error_message = (
                "Não foi possível confirmar a gravação. A operação foi revertida e "
                "os cartões não foram atualizados. Tenta novamente."
            )
            self.statusBar().showMessage(error_message, 8000)
            QMessageBox.critical(self, APP_TITLE, error_message)
            return False
        try:
            self.repo.record_financial_state_snapshot()
        except Exception as exc:
            log_exception("Failed to capture state after data change", exc)
        self.play_click()
        self.refresh_all()
        self.refresh_undo_actions()
        if selected_tx_id is not None:
            try:
                page = self.pages.existing("movimentos")
                if page is not None:
                    from ui.pages.movements import MovementsPage

                if page is not None and isinstance(page, MovementsPage):
                    page._select_transaction_row(selected_tx_id)
            except Exception as exc:
                log_exception("Failed to select changed movement after refresh", exc)

        if message:
            self.statusBar().showMessage(message, 3500)
        return True

    def invalidate_after_movement_change(
        self,
        *,
        selected_tx_id: Optional[int] = None,
        message: str = "Movimento guardado. Todas as análises foram atualizadas.",
    ) -> None:
        """Invalidate every derived page immediately after a movement mutation."""
        if hasattr(self, "analytics_service") and hasattr(
            self.analytics_service, "invalidate_caches"
        ):
            self.analytics_service.invalidate_caches()
        self._dirty_page_keys.update(self.page_factories)

        self._dirty_page_keys.discard("movimentos")
        self._page_refreshed_at["movimentos"] = datetime.now().timestamp()
        try:
            self.repo.record_financial_state_snapshot()
        except Exception as exc:
            log_exception("Failed to capture state after movement change", exc)
        self.refresh_undo_actions()
        if selected_tx_id is not None:
            page = self.pages.existing("movimentos")
            select_row = getattr(page, "_select_transaction_row", None)
            if callable(select_row):
                select_row(int(selected_tx_id))
        if message:
            self.statusBar().showMessage(message, 4500)

    def quick_add(self) -> None:
        dialog = TransactionDialog(self.repo, self)
        if dialog.exec() == QDialog.Accepted:
            data = dialog.extended_data()
            if not data["description"]:
                QMessageBox.warning(self, APP_TITLE, "A descrição é obrigatória.")
                return
            try:
                created_ids = self.change_journal_service.run_with_created_transactions(
                    lambda: [
                        self.repo.add_transaction_with_meta(
                            data["date"],
                            data["description"],
                            data["amount"],
                            data["category"],
                            data["account"],
                            data["notes"],
                            tags=data["tags"],
                            merchant=data["merchant"],
                            payment_method=data["payment_method"],
                            subcategory=data["subcategory"],
                            purchase_date=data["purchase_date"],
                            payment_date=data["payment_date"],
                            commit=False,
                        )
                    ],
                    label=f"Novo movimento · {data['description']}",
                    action_type="create",
                )
                tx_id = created_ids[0]
            except Exception as exc:
                log_exception("Failed to create quick movement atomically", exc)
                QMessageBox.critical(
                    self,
                    APP_TITLE,
                    "Não foi possível guardar o movimento. Nenhum dado foi aplicado.",
                )
                return
            failed_attachments: list[str] = []
            for path in dialog.attachment_paths():
                try:
                    self.document_service.attach_file(
                        tx_id,
                        path,
                        "Anexo adicionado no atalho + Novo",
                    )
                except Exception as exc:
                    failed_attachments.append(path.name)
                    log_exception(
                        f"Failed to attach quick movement document: {path}", exc
                    )
            if failed_attachments:
                QMessageBox.warning(
                    self,
                    APP_TITLE,
                    "O movimento foi guardado, mas os seguintes anexos não foram "
                    "adicionados: " + ", ".join(failed_attachments[:5]),
                )
            self.notify_data_changed("Movimento guardado.", selected_tx_id=tx_id)

    def global_search(self) -> None:
        text = self.search.text().strip()
        if not text:
            return

        target = resolve_route(text)
        if target:
            self.goto(target)
            self.search.clear()
            return

        # Fallback: pesquisar nos movimentos
        self.goto("movimentos")
        from ui.pages.movements import MovementsPage

        page = self.pages.get("movimentos")
        if isinstance(page, MovementsPage):
            page.search.setText(self.search.text())
            page.refresh()


def recover_missing_database_interactively(
    error: MissingDatabaseRecoveryError,
    startup: StartupProgressWindow,
) -> bool:
    """Let the user recover an established profile before SQLite is reopened."""

    paths = paths_for_database(error.database_file)
    while True:
        dialog = QMessageBox(startup)
        dialog.setIcon(QMessageBox.Warning)
        dialog.setWindowTitle("Recuperação dos dados")
        dialog.setText("A base financeira principal não foi encontrada")
        dialog.setInformativeText(
            "A aplicação encontrou backups, documentos ou preferências deste perfil. "
            "Não será criada uma base nova sem a tua confirmação."
        )

        recover_button = dialog.addButton(
            "Recuperar backup…",
            QMessageBox.AcceptRole,
        )
        empty_button = dialog.addButton(
            "Criar perfil vazio",
            QMessageBox.DestructiveRole,
        )
        cancel_button = dialog.addButton("Sair", QMessageBox.RejectRole)
        dialog.setDefaultButton(recover_button)
        prepare_foreground_dialog(dialog, startup)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is cancel_button or clicked is None:
            return False
        if clicked is empty_button:
            answer = exec_foreground_message(
                startup,
                QMessageBox.Warning,
                APP_TITLE,
                "Criar um perfil vazio não recupera os movimentos anteriores.",
                informative_text=(
                    "Os backups e documentos existentes não serão apagados. "
                    "Confirma que pretendes continuar com uma base nova e vazia?"
                ),
                buttons=QMessageBox.Yes | QMessageBox.No,
                default_button=QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                continue
            guard = DataProtectionGuard(
                error.database_file,
                supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
                current_app_version=APP_VERSION,
                attachments_directory=paths.attachments,
                history_directory=paths.history,
                preferences_file=paths.backup_preferences,
                protection_directory=paths.protections,
            )
            guard.authorize_empty_profile_once()
            return True

        initial = error.candidates[0].parent if error.candidates else paths.backups
        selected, _ = QFileDialog.getOpenFileName(
            startup,
            "Escolher backup para recuperar",
            str(initial),
            "Backups (*.lgbak *.zip *.sqlite3 *.db);;Todos (*.*)",
        )
        if not selected:
            continue
        source = Path(selected)
        password: Optional[str] = None
        if BackupService.is_encrypted_backup(source):
            password, accepted = QInputDialog.getText(
                startup,
                "Palavra-passe do backup",
                "Introduz a palavra-passe para decifrar o backup:",
                QLineEdit.Password,
            )
            if not accepted:
                continue
        try:
            report_startup_progress(
                startup.update_progress,
                28,
                "Recuperar os dados",
                source.name,
            )
            preserved = BackupService.restore_missing_profile(
                source,
                database_file=error.database_file,
                supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
                password=password,
            )
            detail = (
                f"\n\nOs ficheiros que ainda existiam foram preservados em:\n{preserved}"
                if preserved is not None
                else ""
            )
            exec_foreground_message(
                startup,
                QMessageBox.Information,
                APP_TITLE,
                f"Backup validado e recuperado com sucesso.{detail}",
            )
            return True
        except Exception as exc:
            log_exception("Failed to recover a missing database at startup", exc)
            exec_foreground_message(
                startup,
                QMessageBox.Critical,
                APP_TITLE,
                "O backup não foi aplicado. Nenhum perfil vazio foi criado.",
                informative_text=str(exc),
            )


def main() -> None:
    configure_qt_runtime()
    install_qt_message_handler()
    try:
        QApplication.setAttribute(Qt.AA_UseSoftwareOpenGL)
    except RuntimeError as exc:
        log_exception("Failed to enable software OpenGL", exc)
    app = QApplication(sys.argv)
    configure_portuguese_number_locale()
    app.setApplicationName(APP_TITLE)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    # Startup/recovery and second-instance warnings can appear before the main
    # window loads the saved theme.  Give those top-level popups a readable
    # light baseline; ``LiquidFinanceApp.apply_theme`` replaces it with the
    # user's active light/dark popup palette immediately afterwards.
    apply_tooltip_theme(Theme(dark=False))
    log_info("Application starting. Log file: %s", get_log_path())
    if "--package-self-test" in sys.argv:
        report_path = os.environ.get("LIQUIDGLASS_SELF_TEST_REPORT", "").strip()
        if not report_path:
            log_warning("Package self-test requested without a report path.")
            raise SystemExit(2)
        try:
            from utils.package_self_test import write_package_self_test_report

            report = write_package_self_test_report(report_path)
            if not bool(report.get("ok")):
                raise RuntimeError("A verificacao interna do pacote falhou.")
        except Exception as exc:
            log_exception("Package self-test failed", exc)
            Path(report_path).write_text(
                json.dumps(
                    {"ok": False, "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            raise SystemExit(3) from exc
        raise SystemExit(0)
    instance_lock = acquire_single_instance_lock()
    if instance_lock is None:
        from utils.single_instance import request_instance_activation

        if request_instance_activation(instance_lock_path()):
            raise SystemExit(0)
        exec_foreground_message(
            None,
            QMessageBox.Information,
            APP_TITLE,
            "A aplicação já está aberta.",
            informative_text=(
                "Para proteger os movimentos, documentos e backups, só pode existir "
                "uma instância em execução de cada vez."
            ),
        )
        raise SystemExit(0)
    app._financas_instance_lock = instance_lock  # type: ignore[attr-defined]
    from utils.single_instance import InstanceActivationServer

    activation_server = InstanceActivationServer(instance_lock_path(), app)
    app._financas_activation_server = activation_server  # type: ignore[attr-defined]
    # --- Startup security gate (PIN / passphrase) ---------------------------
    # No FinanceDB, Repo, BackupService or LiquidFinanceApp may exist before
    # a successful unlock.  For an already-configured profile the unlock
    # happens immediately; for a NEW/LEGACY profile the onboarding dialog is
    # deferred until the data location is prepared (the legacy migration
    # refuses a non-empty data folder, so the envelope cannot be written yet).
    from services.startup_security import (
        ProfileSecurityState,
        SecurityRecoveryRequiredError,
        StartupSecurityService,
        clear_active_session,
        set_active_session,
    )
    from ui.startup_security import run_onboarding_gate, run_startup_security_gate

    security_service = StartupSecurityService()
    try:
        security_state = security_service.inspect_profile()
        if security_state in {
            ProfileSecurityState.MISSING_ENVELOPE,
            ProfileSecurityState.CORRUPT_ENVELOPE,
            ProfileSecurityState.TAMPERED,
            ProfileSecurityState.RECOVERY_REQUIRED,
        }:
            raise SecurityRecoveryRequiredError(
                "A proteção deste perfil está danificada ou foi removida."
            )
        security_context = None
        if security_state == ProfileSecurityState.CONFIGURED:
            # Unlock happens before the loading window exists; its dialog is
            # application-modal and topmost on its own.
            security_context = run_startup_security_gate(security_service)
            if security_context is None:
                log_info("Startup cancelled at the security gate.")
                raise SystemExit(0)
    except SecurityRecoveryRequiredError as exc:
        log_warning("Startup security requires recovery: %s", exc)
        from ui.startup_security import run_tamper_recovery_dialog

        if not run_tamper_recovery_dialog(security_service):
            log_warning("Tamper recovery declined or failed; exiting.")
            raise SystemExit(2) from exc
        security_state = security_service.inspect_profile()
        if security_state not in {
            ProfileSecurityState.NEW,
            ProfileSecurityState.LEGACY_UNPROTECTED,
        }:
            log_warning(
                "Tamper recovery incomplete; state=%s", security_state
            )
            raise SystemExit(2)
        security_context = None
    if security_context is not None:
        set_active_session(security_context)
    startup = StartupProgressWindow()
    startup.show()
    report_startup_progress(
        startup.update_progress,
        1,
        "Iniciar a aplicação",
        "A preparar um arranque protegido.",
    )
    try:
        while True:
            try:
                ensure_persistent_data_location(
                    APP_VERSION,
                    progress_callback=startup.update_progress,
                )
                # First-run onboarding happens only after the data location
                # is prepared, so the envelope is written to its final place.
                # The live startup window is passed so the dialog is stacked
                # strictly ABOVE the loading screen (never covered by it).
                if security_context is None:
                    security_context = run_onboarding_gate(
                        security_service,
                        startup=startup,
                    )
                    if security_context is None:
                        startup.dismiss()
                        log_info("Startup cancelled at the onboarding gate.")
                        clear_active_session()
                        raise SystemExit(0)
                    set_active_session(security_context)
                win = LiquidFinanceApp(startup_progress=startup.update_progress)
                activation_server.set_window(win)
                # The database now exists (created or migrated above): bind
                # the anti-sabotage tombstone to the live envelope identity.
                security_service.refresh_tamper_tombstone()
                break
            except MissingDatabaseRecoveryError as exc:
                log_warning("Startup requires database recovery: %s", exc)
                if recover_missing_database_interactively(exc, startup):
                    continue
                startup.dismiss()
                clear_active_session()
                raise SystemExit(2)
    except DataProtectionError as exc:
        log_exception("Startup blocked by the data-protection guard", exc)
        startup.show_error(str(exc))
        exec_foreground_message(
            startup,
            QMessageBox.Critical,
            APP_TITLE,
            "Arranque bloqueado para proteger os teus dados.",
            informative_text=(
                "A aplicação não continuará enquanto a verificação e a eventual "
                "reposição não estiverem concluídas.\n\n"
                f"{exc}\n\n"
                f"Mais detalhes no registo da aplicação:\n{get_log_path()}"
            ),
        )
        startup.dismiss()
        clear_active_session()
        raise SystemExit(2)
    if bool(win.windowState() & Qt.WindowMaximized) or win.isMaximized():
        win.showMaximized()
    else:
        win.show()
    app.processEvents()
    startup.finish()
    exit_code = app.exec()
    # Zeroize the session master key during shutdown.
    clear_active_session()
    try:
        win.app_updates.launch_pending_after_exit()
    except Exception as exc:
        log_exception("Unable to launch the verified application update", exc)
        QMessageBox.critical(
            None, APP_TITLE,
            "Não foi possível iniciar a instalação da atualização. "
            "A versão anterior continua instalada. Volta a abrir a aplicação e tenta novamente.",
        )
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
