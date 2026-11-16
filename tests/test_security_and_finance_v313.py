from __future__ import annotations

import math
import re
from datetime import date
from pathlib import Path

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.documents import (
    AttachmentIntegrityError,
    AttachmentValidationError,
    DocumentService,
)
from services.finance_math import effective_annual_to_periodic_rate
from services.financial_health import FinancialHealthService
from services.financial_intelligence import monte_carlo_projection
from services.forecast_validation import measure_interval_coverage
from services.investment_projection import build_investment_projection
from services.payments import PaymentsService
from utils.output_security import html_text, safe_hex_colour, spreadsheet_safe_cell


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import utils.config as config

    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "v313.sqlite3"))
    instance = Repo()
    yield instance
    instance.db.conn.close()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _valid_pdf(label: str = "documento") -> bytes:
    return f"%PDF-1.4\n{label}\n%%EOF\n".encode()


@pytest.mark.parametrize(
    "dangerous",
    ["=2+2", "+SUM(A1:A2)", "-10+20", "@IMPORTXML(A1)", "  =CMD()"],
)
def test_spreadsheet_text_is_exported_as_literal(dangerous: str) -> None:
    assert spreadsheet_safe_cell(dangerous).startswith("'")


def test_output_helpers_preserve_numbers_and_escape_html() -> None:
    assert spreadsheet_safe_cell(-12.34) == -12.34
    assert spreadsheet_safe_cell("texto normal") == "texto normal"
    assert html_text("<img src=x onerror=alert(1)>") == (
        "&lt;img src=x onerror=alert(1)&gt;"
    )
    assert safe_hex_colour("#a1b2c3") == "#A1B2C3"
    assert safe_hex_colour("red; background:url(x)") == "#94A3B8"


def test_attachment_fingerprint_detects_same_size_tampering(
    repo: Repo,
    tmp_path: Path,
) -> None:
    transaction_id = repo.add_transaction(
        "2026-08-01",
        "Documento protegido",
        -20.0,
        "Outros",
        "Conta principal",
    )
    source = tmp_path / "fatura.pdf"
    source.write_bytes(_valid_pdf("original"))
    service = DocumentService(repo, attachments_directory=tmp_path / "anexos")

    stored = service.attach_file(transaction_id, source)
    record = repo.transaction_attachments(transaction_id)[0]
    assert len(str(record["content_sha256"])) == 64
    assert int(record["file_size"]) == stored.stat().st_size
    assert service.verify_attachment_record(record)["verified"] is True

    stored.write_bytes(_valid_pdf("alterado"))
    assert stored.stat().st_size == int(record["file_size"])
    with pytest.raises(AttachmentIntegrityError, match="SHA-256"):
        service.verify_attachment_record(record)
    audit = service.audit_storage(force=True)
    assert int(record["id"]) in audit["altered_record_ids"]
    assert audit["healthy"] is False


def test_attachment_rejects_executable_disguised_and_oversized_files(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "relatorio.exe"
    executable.write_bytes(b"MZ")
    disguised = tmp_path / "relatorio.pdf"
    disguised.write_bytes(b"isto nao e um pdf")
    oversized = tmp_path / "demasiado-grande.txt"
    with oversized.open("wb") as handle:
        handle.seek(DocumentService.MAX_ATTACHMENT_BYTES)
        handle.write(b"x")

    with pytest.raises(AttachmentValidationError, match="Formato"):
        DocumentService.validate_source(executable)
    with pytest.raises(AttachmentValidationError, match="conteúdo"):
        DocumentService.validate_source(disguised)
    with pytest.raises(AttachmentValidationError, match="limite"):
        DocumentService.validate_source(oversized)


def test_attachment_integrity_columns_are_migrated(repo: Repo) -> None:
    columns = {
        str(row["name"]) for row in repo.q("PRAGMA table_info(transaction_attachments)")
    }
    assert {"content_sha256", "file_size", "last_verified_at"} <= columns
    assert repo.db.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION


def test_schema_14_attachment_rows_survive_integrity_migration(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    common = {
        "attachments_directory": tmp_path / "anexos",
        "history_directory": tmp_path / "historico",
        "preferences_file": tmp_path / "preferencias.json",
        "protection_directory": tmp_path / "protecoes",
    }
    database = FinanceDB(database_path, **common)
    transaction_id = int(
        database.conn.execute(
            """
            INSERT INTO transactions(date,description,amount,category,account,kind)
            VALUES('2026-08-01','Legado',-10,'Outros','Conta principal','expense')
            """
        ).lastrowid
    )
    database.conn.execute(
        """
        INSERT INTO transaction_attachments(
            transaction_id,file_name,stored_name,stored_path,original_path,
            file_type,description,supplier,invoice_date,total_amount
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            transaction_id,
            "fatura.pdf",
            "fatura.pdf",
            str(tmp_path / "anexos" / "1" / "fatura.pdf"),
            "",
            "PDF",
            "Documento legado",
            "Fornecedor",
            "2026-08-01",
            10.0,
        ),
    )
    database.conn.commit()
    cascade_sql = database.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_transactions_cascade_transfer_leg'"
    ).fetchone()[0]
    # This fixture downgrades only attachment capabilities. Do not let SQLite's
    # table rename rewrite a different table's trusted trigger to the temp name.
    database.conn.execute("DROP TRIGGER trg_transactions_cascade_transfer_leg")
    for row in database.query(
        "SELECT name FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='transaction_attachments'"
    ):
        database.conn.execute(f'DROP TRIGGER IF EXISTS "{row["name"]}"')
    database.conn.executescript(
        """
        ALTER TABLE transaction_attachments RENAME TO transaction_attachments_v17;
        CREATE TABLE transaction_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            original_path TEXT NOT NULL DEFAULT '',
            file_type TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            supplier TEXT NOT NULL DEFAULT '',
            invoice_date TEXT NOT NULL DEFAULT '',
            total_amount REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
        );
        INSERT INTO transaction_attachments(
            id,transaction_id,file_name,stored_name,stored_path,original_path,
            file_type,description,supplier,invoice_date,total_amount,created_at
        )
        SELECT id,transaction_id,file_name,stored_name,stored_path,original_path,
               file_type,description,supplier,invoice_date,total_amount,created_at
        FROM transaction_attachments_v17;
        DROP TABLE transaction_attachments_v17;
        DELETE FROM schema_migrations WHERE version>=15;
        """
    )
    database.conn.execute(cascade_sql)
    database.conn.commit()
    database.close()

    upgraded = FinanceDB(database_path, **common)
    try:
        row = upgraded.query(
            "SELECT * FROM transaction_attachments WHERE transaction_id=?",
            (transaction_id,),
        )[0]
        assert upgraded.schema_version() == FinanceDB.CURRENT_SCHEMA_VERSION
        assert row["file_name"] == "fatura.pdf"
        assert row["description"] == "Documento legado"
        assert row["content_sha256"] == ""
        assert row["file_size"] == 0
    finally:
        upgraded.close()


def test_effective_annual_rate_compounds_exactly() -> None:
    monthly = effective_annual_to_periodic_rate(6.0)
    assert (1.0 + monthly) ** 12 - 1.0 == pytest.approx(0.06)
    assert monthly != pytest.approx(0.06 / 12.0)


def test_projection_keeps_nominal_and_today_euros_separate() -> None:
    projection = build_investment_projection(
        100.0,
        0.0,
        1,
        inflation_pct=2.0,
        tax_pct=0.0,
        fee_pct=0.0,
    )
    row = projection["rows"][0]
    assert row["invested_nominal"] == pytest.approx(1_200.0)
    assert row["invested_real"] < row["invested_nominal"]
    assert row["projected_nominal"] == pytest.approx(1_200.0)
    assert row["projected_real"] < row["projected_nominal"]
    assert row["gain_real"] < 0


def test_monte_carlo_uses_effective_rate_and_portfolio_diversification() -> None:
    deterministic = monte_carlo_projection(
        100.0,
        6.0,
        0.0,
        1,
        mode="single",
        simulations=100,
        inflation_pct=0.0,
        tax_pct=0.0,
        fee_pct=0.0,
        seed=9,
    )
    concentrated = monte_carlo_projection(
        100.0,
        6.0,
        12.0,
        1,
        mode="single",
        simulations=100,
        asset_weights=[1.0],
        correlation_pct=0.0,
        seed=9,
    )
    diversified = monte_carlo_projection(
        100.0,
        6.0,
        12.0,
        1,
        mode="single",
        simulations=100,
        asset_weights=[1.0, 1.0],
        correlation_pct=0.0,
        seed=9,
    )

    assert deterministic["rows"][0]["p50_nominal"] == pytest.approx(106.0)
    assert deterministic["annual_return_convention"] == "effective_annual"
    assert diversified["effective_volatility_pct"] == pytest.approx(
        12.0 / math.sqrt(2.0)
    )
    assert (
        diversified["effective_volatility_pct"]
        < concentrated["effective_volatility_pct"]
    )


def test_resilience_withholds_score_until_history_is_sufficient(
    repo: Repo,
) -> None:
    repo.db.conn.execute("DELETE FROM transactions")
    repo.db.conn.commit()
    service = FinancialHealthService(repo, PaymentsService(repo))

    insufficient = service.risk_resilience(as_of=date(2026, 7, 25))
    assert insufficient["score_available"] is False
    assert insufficient["score"] is None
    assert insufficient["confidence"] == "insufficient"

    for offset in range(12):
        month = 8 + offset
        year = 2025 + (month - 1) // 12
        month = (month - 1) % 12 + 1
        repo.add_transaction(
            f"{year:04d}-{month:02d}-02",
            "Salário",
            2_500.0 + offset * 10,
            "Rendimentos",
            "Conta principal",
        )
        repo.add_transaction(
            f"{year:04d}-{month:02d}-12",
            "Despesas",
            -(1_600.0 + offset * 5),
            "Casa",
            "Conta principal",
        )

    # August is the first query month after all twelve seeded months completed.
    complete = service.risk_resilience(as_of=date(2026, 8, 25))
    assert complete["score_available"] is True
    assert complete["score"] is not None
    assert complete["data_months"] == 12
    assert complete["confidence"] == "high"
    assert complete["confidence_score"] == 100
    assert len(complete["sensitivity"]) == 3


def test_forecast_quality_penalises_unnecessarily_wide_intervals() -> None:
    narrow = measure_interval_coverage(
        [
            {
                "actual": 100.0,
                "predicted": 100.0,
                "low": 90.0,
                "high": 110.0,
                "is_validation": True,
            }
            for _ in range(6)
        ]
    )
    wide = measure_interval_coverage(
        [
            {
                "actual": 100.0,
                "predicted": 100.0,
                "low": 0.0,
                "high": 200.0,
                "is_validation": True,
            }
            for _ in range(6)
        ]
    )

    assert narrow["interval_coverage_pct"] == wide["interval_coverage_pct"] == 100
    assert narrow["accuracy_pct"] > wide["accuracy_pct"]
    assert narrow["mean_band_width_pct"] < wide["mean_band_width_pct"]
    assert wide["accuracy_method"] == "composite_interval_quality"


def test_security_dependency_is_pinned_to_fixed_release() -> None:
    requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(
        encoding="utf-8"
    )
    # Exact pins must retain the validated release floors while permitting
    # later patched releases; installed-version consistency is checked separately.
    for package, floor in (("cryptography", (50, 0, 1)), ("argon2-cffi", (25, 1, 0))):
        match = re.search(rf"^{re.escape(package)}==(\d+)\.(\d+)\.(\d+)$", requirements, re.MULTILINE)
        assert match is not None, f"Missing exact stable pin for {package}"
        assert tuple(map(int, match.groups())) >= floor


@pytest.mark.parametrize("close_method", ["close", "reject"])
def test_internal_image_and_pdf_previews_render_offscreen(tmp_path: Path, qapp, close_method) -> None:
    from PySide6.QtCore import QCoreApplication, QEvent, QRect
    from PySide6.QtGui import QColor, QImage, QPainter, QPdfWriter
    from shiboken6 import isValid

    from ui.document_preview import AttachmentPreviewDialog

    app = qapp
    image_path = tmp_path / "comprovativo.png"
    image = QImage(320, 180, QImage.Format_ARGB32)
    image.fill(QColor("#DCEBFF"))
    assert image.save(str(image_path))

    pdf_path = tmp_path / "fatura.pdf"
    writer = QPdfWriter(str(pdf_path))
    painter = QPainter(writer)
    painter.drawText(QRect(100, 100, 800, 200), "Fatura verificada")
    painter.end()
    del painter
    del writer  # Finalize the PDF cross-reference before opening it for preview.

    image_dialog = AttachmentPreviewDialog(image_path)
    pdf_dialog = AttachmentPreviewDialog(pdf_path)
    document = pdf_dialog._pdf_document
    view = pdf_dialog._pdf_view
    assert document is not None and document.pageCount() == 1
    assert view is not None and view.document() is document
    try:
        for dialog in (image_dialog, pdf_dialog):
            dialog.show()
            app.processEvents()
            assert not dialog.grab().isNull()
            assert dialog.width() >= 620
            assert dialog.height() >= 480
            getattr(dialog, close_method)()
        assert view.document() is None
        assert document.pageCount() == 0
    finally:
        for dialog in (image_dialog, pdf_dialog):
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)
        app.processEvents()
