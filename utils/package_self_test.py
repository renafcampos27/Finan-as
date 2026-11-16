"""Functional runtime acceptance using disposable financial profiles only."""

from __future__ import annotations

import json
import hashlib
import math
import os
import platform
import sqlite3
import struct
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import cryptography
import PySide6
from PySide6.QtCore import qVersion

from database.engine import FinanceDB
from utils.version import APP_VERSION


@contextmanager
def _isolated_environment(root: Path):
    for folder in ("tmp", "home", "local", "roaming"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    values = {
        "FINANCAS_PESSOAIS_STATE_DIR": str(root),
        "FINANCAS_PESSOAIS_TESTING": "1",
        "FINANCAS_PESSOAIS_DEMO_DATA": "0",
        "TEMP": str(root / "tmp"),
        "TMP": str(root / "tmp"),
        "TMPDIR": str(root / "tmp"),
        "HOME": str(root / "home"),
        "USERPROFILE": str(root / "home"),
        "LOCALAPPDATA": str(root / "local"),
        "APPDATA": str(root / "roaming"),
    }
    previous = {key: os.environ.get(key) for key in values}
    previous_tempdir = tempfile.tempdir
    tempfile.tempdir = str(root / "tmp")
    os.environ.update(values)
    try:
        yield
    finally:
        tempfile.tempdir = previous_tempdir
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _functional_financial_checks(repo) -> dict[str, Any]:
    from services.financial_intelligence import monte_carlo_projection
    from services.financial_health import FinancialHealthService
    from services.payments import PaymentsService

    projection = monte_carlo_projection(
        100.0,
        annual_return_pct=0,
        annual_volatility_pct=0,
        years=1,
        simulations=100,
        initial_capital=1000.0,
        inflation_pct=0,
        tax_pct=0,
    )
    median = float(projection["rows"][-1]["p50_nominal"])
    if not math.isclose(median, 2200.0, abs_tol=1e-8):
        raise RuntimeError("A simulação NumPy não reconciliou o caso determinístico.")
    health = FinancialHealthService(repo, PaymentsService(repo))
    score = health.financial_score("2026-06")
    risk = health.risk_resilience(
        as_of=date(2026, 7, 15),
        horizon_months=3,
        simulations=200,
        emergency_expense=100,
        income_reduction_pct=10,
    )
    if not score.get("dimensions") or len(risk["rows"]) != 4:
        raise RuntimeError(
            "O score ou o cenário NumPy não produziu os resultados esperados."
        )
    if not all(math.isfinite(float(row["p50"])) for row in risk["rows"][1:]):
        raise RuntimeError("O cenário NumPy produziu valores não finitos.")
    return {
        "numpy_simulation": {
            "passed": True,
            "median": median,
            "simulations": projection["simulations"],
            "rows": len(projection["rows"]),
        },
        "numpy_health": {
            "passed": True,
            "score": risk["calculated_score"],
            "simulations": risk["simulations"],
            "rows": len(risk["rows"]),
        },
    }


def _backup_roundtrip(
    root: Path, archive: Path, password: str, format_name: str, attachment_sha: str
) -> dict[str, Any]:
    from services.backup import BackupService
    from services.backup_crypto import BackupCryptoError
    from database.protection import DataProtectionError

    restored = root / format_name / "restored.sqlite3"
    BackupService.restore_missing_profile(
        archive,
        database_file=restored,
        supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
        password=password,
    )
    reopened = FinanceDB(restored)
    try:
        marker = reopened.conn.execute(
            "SELECT value FROM settings WHERE key='package_self_test_marker'"
        ).fetchone()[0]
        count = int(reopened.scalar("SELECT COUNT(*) FROM transactions"))
        if marker != "synthetic-acceptance" or count != 12:
            raise RuntimeError("O restauro não preservou os registos sintéticos.")
        restored_attachment = restored.parent / "anexos" / "receipt.txt"
        restored_sha = hashlib.sha256(restored_attachment.read_bytes()).hexdigest()
        if restored_sha != attachment_sha:
            raise RuntimeError("O restauro não preservou o anexo sintético.")
    finally:
        reopened.prepare_for_shutdown()
    rejected = {}
    truncated = root / f"truncated-{format_name}.lgbak"
    truncated.write_bytes(archive.read_bytes()[:-17])
    for case, candidate, credential in (
        ("wrong_password", archive, "definitely-wrong-synthetic-password"),
        ("truncated", truncated, password),
    ):
        destination = root / f"rejected-{format_name}-{case}" / "profile.sqlite3"
        try:
            BackupService.restore_missing_profile(
                candidate,
                database_file=destination,
                supported_schema_version=FinanceDB.CURRENT_SCHEMA_VERSION,
                password=credential,
            )
        except ValueError, BackupCryptoError, DataProtectionError:
            if destination.exists():
                raise RuntimeError("Um backup rejeitado deixou um perfil instalado.")
            rejected[case] = True
        else:
            raise RuntimeError("Um backup inválido foi aceite pelo executável.")
    return {
        "passed": True,
        "format": format_name,
        "restored_marker": marker,
        "restored_transactions": count,
        "restored_attachment_sha256": restored_sha,
        "wrong_password_rejected": rejected["wrong_password"],
        "truncated_rejected": rejected["truncated"],
    }


def _window_check(root: Path) -> dict[str, Any]:
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    # A frozen entry point is __main__. Reuse that class rather than importing
    # main.py a second time and re-running its process-wide startup setup.
    window_type = getattr(sys.modules.get("__main__"), "LiquidFinanceApp", None)
    if window_type is None:
        from main import LiquidFinanceApp

        window_type = LiquidFinanceApp

    qapp = QApplication.instance() or QApplication([])
    with _isolated_environment(root / "window-profile"):
        window = window_type()
        try:
            window.repo.set_setting("backup_on_close", "0")
            window.show()
            qapp.processEvents()
            visible = window.isVisible()
            window.close()
            deadline = time.monotonic() + 10
            while window.isVisible() and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.005)
            if not visible or window.isVisible() or not window._database_closed:
                raise RuntimeError(
                    "A janela não concluiu o ciclo de abertura/encerramento."
                )
            return {"passed": True, "shown": visible, "database_closed": True}
        finally:
            window.auto_backup_timer.stop()
            window.subscription_reminder_timer.stop()
            window.background_task_registry.begin_maintenance()
            if not window.background_task_registry.wait_for_idle(5000):
                raise RuntimeError("Ficaram tarefas ativas no auto-teste da janela.")
            if not window._database_closed:
                window.repo.db.conn.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)


def _pdf_preview_check(root: Path) -> dict[str, Any]:
    """Open and paint a generated PDF through both the PDF engine and viewer."""
    from PySide6.QtCore import QCoreApplication, QEvent, QSize
    from PySide6.QtGui import QColor, QPageSize, QPainter, QPdfWriter
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtPdfWidgets import QPdfView
    from PySide6.QtWidgets import QApplication

    qapp = QApplication.instance() or QApplication([])
    pdf = root / "synthetic-preview.pdf"
    writer = QPdfWriter(str(pdf))
    writer.setResolution(72)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A6))
    painter = QPainter(writer)
    if not painter.isActive():
        raise RuntimeError("Não foi possível criar o PDF sintético do auto-teste.")
    try:
        painter.fillRect(20, 20, 80, 60, QColor("#d12a3a"))
        painter.drawText(20, 110, "Synthetic PDF package acceptance")
    finally:
        painter.end()
    del writer  # Finalize the PDF cross-reference/trailer before opening it.

    def marker_pixels(image) -> int:
        return sum(
            1
            for y in range(0, image.height(), 4)
            for x in range(0, image.width(), 4)
            if (color := image.pixelColor(x, y)).red() > 150
            and color.green() < 100 and color.blue() < 120
        )

    document = QPdfDocument(qapp)
    view = QPdfView()
    try:
        error = document.load(str(pdf))
        if error != QPdfDocument.Error.None_ or document.pageCount() != 1:
            raise RuntimeError("O motor PDF não abriu a página sintética.")
        image = document.render(0, QSize(240, 320))
        rendered_marker_pixels = marker_pixels(image)
        if image.isNull() or rendered_marker_pixels < 20:
            raise RuntimeError("O motor PDF não desenhou o conteúdo sintético.")

        view.resize(320, 440)
        view.setDocument(document)
        view.setPageMode(QPdfView.PageMode.MultiPage)
        view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        view.show()
        deadline = time.monotonic() + 5
        viewer_marker_pixels = 0
        while time.monotonic() < deadline:
            qapp.processEvents()
            viewer_marker_pixels = marker_pixels(view.grab().toImage())
            if viewer_marker_pixels >= 20:
                break
            time.sleep(0.01)
        if not view.isVisible() or viewer_marker_pixels < 20:
            raise RuntimeError("O visualizador PDF não apresentou o conteúdo sintético.")
        return {
            "passed": True,
            "document_pages": document.pageCount(),
            "render_width": image.width(),
            "render_height": image.height(),
            "rendered_marker_pixels": rendered_marker_pixels,
            "viewer_marker_pixels": viewer_marker_pixels,
            "viewer_shown": view.isVisible(),
            "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        }
    finally:
        view.setDocument(None)
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(view, QEvent.Type.DeferredDelete)
        document.close()
        document.deleteLater()
        QCoreApplication.sendPostedEvents(document, QEvent.Type.DeferredDelete)


def create_package_self_test_report() -> dict[str, Any]:
    """Exercise the delivered runtime, UI and both encrypted recovery formats."""

    import numpy
    import argon2
    import ui.pages as pages
    from database.repo import Repo
    from services.backup import BackupService
    from services.backup_crypto import read_envelope
    from services.startup_security import (
        StartupSecurityService,
        get_active_session,
        set_active_session,
        clear_active_session,
    )

    if get_active_session() is not None:
        raise RuntimeError("O auto-teste requer um processo sem sessão pessoal ativa.")

    loaded_exports: list[str] = []
    for export_name in pages.__all__:
        getattr(pages, export_name)
        loaded_exports.append(export_name)

    with tempfile.TemporaryDirectory(prefix="liquidglass-package-test-") as folder:
        root = Path(folder)
        with _isolated_environment(root / "profile"):
            database_path = root / "source" / "self_test.sqlite3"
            database = FinanceDB(database_path)
            try:
                repo = Repo(database)
                for month in range(1, 7):
                    database.add_transaction(
                        f"2026-{month:02d}-15",
                        "Synthetic income",
                        1000,
                        "Rendimentos",
                        "Synthetic",
                        "income",
                    )
                    database.add_transaction(
                        f"2026-{month:02d}-16",
                        "Synthetic expense",
                        -400,
                        "Supermercado",
                        "Synthetic",
                        "expense",
                    )
                repo.set_setting("package_self_test_marker", "synthetic-acceptance")
                functional = _functional_financial_checks(repo)
                attachments = database_path.parent / "anexos"
                attachments.mkdir(parents=True, exist_ok=True)
                receipt = attachments / "receipt.txt"
                receipt.write_bytes(b"Synthetic package acceptance receipt\n")
                attachment_sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
                backup = BackupService(repo, attachments_directory=attachments)
                legacy = root / "legacy.lgbak"
                legacy_password = "synthetic legacy backup password"
                backup.create_lgbk1_legacy_backup(
                    legacy, password=legacy_password, purpose="package_self_test"
                )
                functional["lgbk1_roundtrip"] = _backup_roundtrip(
                    root,
                    legacy,
                    legacy_password,
                    "LGBK1",
                    attachment_sha,
                )
                password = "synthetic strong LGBK2 password"
                session = StartupSecurityService(
                    database_file=database_path
                ).configure_first_run("password", password)
                set_active_session(session)
                modern = root / "modern.lgbak"
                try:
                    kdf_algorithm = (
                        read_envelope(session.envelope_path)
                        .unlocks["password"]
                        .kdf.name
                    )
                    if kdf_algorithm != "argon2id":
                        raise RuntimeError(
                            "O pacote não executou o KDF Argon2id obrigatório."
                        )
                    backup.create_lgbk2_complete_backup(
                        modern, purpose="package_self_test"
                    )
                    functional["lgbk2_roundtrip"] = _backup_roundtrip(
                        root,
                        modern,
                        password,
                        "LGBK2",
                        attachment_sha,
                    )
                    functional["lgbk2_roundtrip"]["kdf_algorithm"] = kdf_algorithm
                finally:
                    clear_active_session()
                quick_check = str(
                    database.conn.execute("PRAGMA quick_check(1)").fetchone()[0]
                )
                schema_version = int(database.CURRENT_SCHEMA_VERSION)
            finally:
                database.prepare_for_shutdown()

            reopened = sqlite3.connect(database_path)
            try:
                reopened_check = str(
                    reopened.execute("PRAGMA quick_check(1)").fetchone()[0]
                )
            finally:
                reopened.close()
            functional["pdf_preview"] = _pdf_preview_check(root)
            functional["window_open_close"] = _window_check(root)

    return {
        "ok": quick_check == reopened_check == "ok"
        and all(check["passed"] for check in functional.values()),
        "application_version": APP_VERSION,
        "python": platform.python_version(),
        "python_bits": struct.calcsize("P") * 8,
        "architecture": platform.machine(),
        "pyside6": PySide6.__version__,
        "qt": qVersion(),
        "cryptography": cryptography.__version__,
        "numpy": numpy.__version__,
        "bundle_root": str(Path(sys._MEIPASS).resolve())
        if getattr(sys, "frozen", False)
        else None,
        "bundle_module_paths": {
            "numpy": str(Path(numpy.__file__).resolve()),
            "argon2": str(Path(argon2.__file__).resolve()),
            "QtPdf": str(Path(sys.modules["PySide6.QtPdf"].__file__).resolve()),
            "QtPdfWidgets": str(Path(sys.modules["PySide6.QtPdfWidgets"].__file__).resolve()),
        },
        "functional_checks": functional,
        "frozen": bool(getattr(sys, "frozen", False)),
        "database_schema": schema_version,
        "database_quick_check": reopened_check,
        "page_exports_loaded": len(loaded_exports),
        "page_exports": loaded_exports,
    }


def write_package_self_test_report(target: str | Path) -> dict[str, Any]:
    """Write the result atomically so a build never accepts a partial report."""

    destination = Path(target).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = create_package_self_test_report()
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return report


__all__ = ["create_package_self_test_report", "write_package_self_test_report"]
