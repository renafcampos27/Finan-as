"""A07: deterministic dates, deliberate signs and non-destructive deduplication."""

from datetime import date
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from database.engine import FinanceDB
from database.repo import Repo
from services.bank_parsers import QIFParser
from services.imports import ImportService


MAPPING = {
    "date": "Data",
    "desc": "Descrição",
    "amount": "Valor",
    "account": "Conta",
    "reference": "Ref",
}


@pytest.fixture
def service(tmp_path, empty_profile):
    database = FinanceDB(tmp_path / "synthetic.sqlite3")
    result = ImportService(Repo(database))
    yield result
    database.close()


def row(day="2026-01-15", description="Compra", amount="-10", reference=""):
    return {
        "Data": day,
        "Descrição": description,
        "Valor": amount,
        "Conta": "Conta teste",
        "Ref": reference,
    }


def test_compact_ambiguous_date_requires_preview_decision(service):
    result = service.normalize_row(row("010226"), MAPPING, apply_rules=False)
    assert not result.is_valid
    assert "ambígua" in result.validation_message


def test_explicit_compact_order_is_used(service):
    result = service.normalize_row(
        row("010226"), MAPPING, apply_rules=False, date_format="%d%m%y"
    )
    assert result.is_valid
    assert result.date == "2026-02-01"


def test_separated_ambiguous_dates_are_not_assigned_a_default(tmp_path):
    file = tmp_path / "statement.csv"
    file.write_text("Data;Descrição;Valor\n01/02/2026;Compra;-10\n", encoding="utf-8")
    data = ImportService.read_csv(file)
    assert data.date_format == ""


def test_qif_retains_original_date_until_batch_decision():
    parsed = QIFParser.parse("!Type:Bank\nD01/02/2026\nT-10\nPCompra\n^\n")
    assert parsed.rows[0]["Data"] == "01/02/2026"


def test_reused_reference_does_not_discard_different_movements(service):
    rows = [
        row("2026-01-15", reference="shared"),
        row("2026-02-15", reference="shared"),
    ]
    preview = service.preview(rows, MAPPING, apply_rules=False)
    assert preview.certain_duplicates == 0
    result = service.import_rows(
        rows, MAPPING, apply_rules=False, ignore_duplicates=True
    )
    assert result.created == 2


def test_reused_stored_reference_is_reviewed_without_dropping(service):
    service.import_rows(
        [row(reference="shared")], MAPPING, apply_rules=False, ignore_duplicates=True
    )
    result = service.import_rows(
        [row("2026-02-15", reference="shared")],
        MAPPING,
        apply_rules=False,
        ignore_duplicates=True,
    )
    assert result.created == 1


def test_prefix_similarity_is_never_automatically_discarded(service):
    service.import_rows(
        [row(description="Supermercado Avenida operação 1")],
        MAPPING,
        apply_rules=False,
        ignore_duplicates=True,
    )
    result = service.import_rows(
        [row(description="Supermercado Avenida operação 12")],
        MAPPING,
        apply_rules=False,
        ignore_duplicates=True,
        ignore_probable=True,
    )
    assert result.created == 1


@pytest.mark.parametrize(
    "text,fmt,expected",
    [
        ("010226", "%d%m%y", "2026-02-01"),
        ("010226", "%y%m%d", "2001-02-26"),
        ("010226", "%m%d%y", "2026-01-02"),
        ("01022026", "%d%m%Y", "2026-02-01"),
        ("01/02/26", "%m/%d/%y", "2026-01-02"),
    ],
)
def test_same_configuration_is_independent_of_import_day(
    service, monkeypatch, text, fmt, expected
):
    import utils.helpers as helpers

    class FutureDate(date):
        @classmethod
        def today(cls):
            return cls(2099, 12, 31)

    before = service.normalize_row(
        row(text), MAPPING, apply_rules=False, date_format=fmt
    )
    monkeypatch.setattr(helpers, "date", FutureDate)
    after = service.normalize_row(
        row(text), MAPPING, apply_rules=False, date_format=fmt
    )
    assert before.date == after.date == expected


def test_global_month_first_evidence_reaches_preview_and_import(service):
    rows = [row("01/02/2026"), row("12/25/2026")]
    preview = service.preview(rows, MAPPING, apply_rules=False)
    assert [r.transaction.date for r in preview.rows] == ["2026-01-02", "2026-12-25"]
    service.import_rows(rows, MAPPING, apply_rules=False, ignore_duplicates=True)
    assert [
        r["date"] for r in service.repo.q("SELECT date FROM transactions ORDER BY id")
    ] == ["2026-01-02", "2026-12-25"]


def test_ambiguity_after_preview_limit_blocks_whole_batch(service):
    rows = [row() for _ in range(81)] + [row("010226")]
    preview = service.preview(rows, MAPPING, apply_rules=False, limit=80)
    assert len(preview.rows) == 80
    assert preview.ambiguous_dates == 1
    with pytest.raises(ValueError, match="ambíguas"):
        service.import_rows(rows, MAPPING, apply_rules=False, ignore_duplicates=True)
    assert service.repo.movement_count() == 0


def test_declared_format_does_not_fall_back_to_another_compact_order(service):
    result = service.normalize_row(
        row("310126"), MAPPING, apply_rules=False, date_format="%m%d%y"
    )
    assert not result.is_valid


def test_exact_structured_reference_reimport_is_still_certain(service):
    rows = [row(reference="unique-bank-id")]
    first = service.import_rows(
        rows, MAPPING, apply_rules=False, ignore_duplicates=True
    )
    preview = service.preview(rows, MAPPING, apply_rules=False)
    second = service.import_rows(
        rows, MAPPING, apply_rules=False, ignore_duplicates=True
    )
    assert first.created == 1
    assert preview.certain_duplicates == 1
    assert second.created == 0 and second.skipped_duplicates == 1


def test_reference_identity_includes_description_and_amount(service):
    rows = [
        row(reference="same"),
        row(description="Outra compra", reference="same"),
        row(amount="-12", reference="same"),
    ]
    assert service.preview(rows, MAPPING, apply_rules=False).certain_duplicates == 0
    assert (
        service.import_rows(
            rows, MAPPING, apply_rules=False, ignore_duplicates=True
        ).created
        == 3
    )


def test_repository_reference_fallback_agrees_with_index(service):
    service.import_rows(
        [row(reference="reused")], MAPPING, apply_rules=False, ignore_duplicates=True
    )
    classification = service.repo.duplicate_classification(
        "2026-02-15", "Compra", -10, "Conta teste", "reused"
    )
    assert classification["level"] != "certain"


def test_raw_values_and_original_line_survive_normalization_and_persistence(service):
    original = row("010226", amount="-12,50")
    fingerprint = "a" * 64
    result = service.import_rows(
        [row("not-a-date"), original],
        MAPPING,
        apply_rules=False,
        ignore_duplicates=True,
        date_format="%d%m%y",
        amount_locale="pt",
        source_sha256=fingerprint,
    )
    assert result.created == 1 and result.skipped_invalid == 1
    stored = service.repo.q("SELECT date,amount,notes FROM transactions")[0]
    evidence = json.loads(stored["notes"].split(" · ", 1)[1])
    assert stored["date"] == "2026-02-01" and stored["amount"] == -12.5
    assert evidence["original"] == original
    assert evidence["source_row"] == 2
    assert evidence["source_sha256"] == fingerprint
    assert evidence["date_format"] == "%d%m%y"


def test_file_hash_warning_is_contextual_without_blocking_other_accounts_or_profiles(
    service, tmp_path
):
    fingerprint = "b" * 64
    original = row(reference="id")
    assert not service.file_history_warning(fingerprint, ["Conta teste"])
    service.import_rows(
        [original],
        MAPPING,
        apply_rules=False,
        ignore_duplicates=True,
        source_sha256=fingerprint,
    )
    assert "nesta conta" in service.file_history_warning(fingerprint, ["Conta teste"])
    other = {**original, "Conta": "Outra conta"}
    assert "noutra conta" in service.file_history_warning(fingerprint, ["Outra conta"])
    assert (
        service.import_rows(
            [other],
            MAPPING,
            apply_rules=False,
            ignore_duplicates=True,
            source_sha256=fingerprint,
        ).created
        == 1
    )
    database = FinanceDB(tmp_path / "other-profile.sqlite3")
    try:
        assert not ImportService(Repo(database)).file_history_warning(
            fingerprint, ["Conta teste"]
        )
    finally:
        database.close()


def ofx_section(card=False, account="bank"):
    tag = "CCSTMTTRNRS" if card else "STMTTRNRS"
    account_tag = "CCACCTFROM" if card else "BANKACCTFROM"
    return f"<{tag}><{account_tag}><ACCTID>{account}</ACCTID></{account_tag}><BANKTRANLIST><STMTTRN><TRNTYPE>PAYMENT</TRNTYPE><DTPOSTED>20260115120000</DTPOSTED><TRNAMT>-25.00</TRNAMT><NAME>Pagamento</NAME></STMTTRN></BANKTRANLIST></{tag}>"


def test_ofx_mixed_bank_card_context_preserves_each_payment_nature(tmp_path, service):
    path = tmp_path / "mixed.ofx"
    path.write_text(
        "<OFX>" + ofx_section() + ofx_section(True, "card") + "</OFX>", encoding="utf-8"
    )
    data = service.read_file(path)
    assert data.is_credit_card and not data.invert_signs
    assert data.source_sha256 == sha256(path.read_bytes()).hexdigest()
    preview = service.preview(
        data.rows,
        service.suggested_mapping(data.headers),
        apply_rules=False,
        invert_signs=False,
    )
    assert [entry.transaction.amount for entry in preview.rows] == [-25, 25]
    assert dict(preview.rows[1].transaction.source_values)["Valor original"] == "-25.00"


def test_debit_credit_columns_are_not_inverted_twice(service):
    original = {**row(), "Débito": "25", "Crédito": ""}
    mapping = {
        **MAPPING,
        "amount": "",
        "amount_debit": "Débito",
        "amount_credit": "Crédito",
    }
    normalized = service.normalize_row(
        original, mapping, apply_rules=False, invert_signs=True
    )
    assert normalized.amount == -25


@pytest.fixture
def import_page(service, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from ui.pages.tools import SmartImportPage
    import shiboken6

    application = QApplication.instance() or QApplication([])
    host = SimpleNamespace(
        repo=service.repo,
        analytics_service=None,
        payments_service=None,
        import_service=service,
        theme=SimpleNamespace(privacy=False),
        notify_data_changed=lambda: None,
    )
    page = SmartImportPage(host)

    def reject_dialog(*args):
        raise AssertionError(str(args[-1]))

    from ui.pages import tools

    monkeypatch.setattr(tools.QMessageBox, "critical", reject_dialog)
    monkeypatch.setattr(tools.QMessageBox, "warning", reject_dialog)
    yield page
    page.close()
    shiboken6.delete(page)
    application.processEvents()


def test_real_preview_requires_date_choice_then_imports_matching_result(
    import_page, service, tmp_path, monkeypatch
):
    from ui.pages import tools

    path = tmp_path / "ambiguous.csv"
    path.write_text("Data;Descrição;Valor\n010226;Compra;-10\n", encoding="utf-8")
    monkeypatch.setattr(
        tools.QFileDialog, "getOpenFileName", lambda *a: (str(path), "")
    )
    monkeypatch.setattr(tools.QMessageBox, "information", lambda *a: None)
    import_page.open_csv()
    assert not import_page.import_btn.isEnabled()
    assert "ambígua" in import_page.preview.item(0, 6).text()
    import_page.date_format.setCurrentIndex(import_page.date_format.findData("%d%m%y"))
    assert import_page.import_btn.isEnabled()
    assert import_page.preview.item(0, 0).text() == "2026-02-01"
    import_page.import_rows()
    assert service.repo.q("SELECT date FROM transactions")[0]["date"] == "2026-02-01"


def test_real_card_file_selection_does_not_enable_sign_inversion(
    import_page, service, tmp_path, monkeypatch
):
    from ui.pages import tools

    path = tmp_path / "card.ofx"
    path.write_text("<OFX>" + ofx_section(True, "card") + "</OFX>", encoding="utf-8")
    monkeypatch.setattr(
        tools.QFileDialog, "getOpenFileName", lambda *a: (str(path), "")
    )
    monkeypatch.setattr(tools.QMessageBox, "information", lambda *a: None)
    import_page.open_csv()
    assert import_page.account_nature == "credit_card"
    assert not import_page.is_credit_card.isChecked()
    assert import_page.preview.item(0, 2).text().replace("\u00a0", " ").startswith("25")
    import_page.import_rows()
    assert service.repo.q("SELECT amount FROM transactions")[0]["amount"] == 25


def test_imported_refund_preserves_expense_category_but_kind_follows_positive_amount(
    service,
):
    category_id, category = service.repo.category_identity(
        "Alimentação", create=True, kind="expense"
    )
    original = {**row(amount="25"), "Categoria": category}
    mapping = {**MAPPING, "category": "Categoria"}
    result = service.import_rows(
        [original], mapping, apply_rules=False, ignore_duplicates=True
    )
    assert result.created == 1
    stored = service.repo.q(
        "SELECT amount,category_id,category,kind FROM transactions"
    )[0]
    assert stored["amount"] == 25 and stored["kind"] == "income"
    assert stored["category_id"] == category_id and stored["category"] == category


def test_ambiguous_numeric_timestamp_also_requires_a_format(service):
    ambiguous = service.normalize_row(
        row("01/02/2026 12:34:56"), MAPPING, apply_rules=False
    )
    explicit = service.normalize_row(
        row("01/02/2026 12:34:56"), MAPPING, apply_rules=False, date_format="%m/%d/%Y"
    )
    assert not ambiguous.is_valid and "ambígua" in ambiguous.validation_message
    assert explicit.date == "2026-01-02"


def test_legacy_classifier_without_full_identity_cannot_discard_rows(
    service, monkeypatch
):
    monkeypatch.setattr(service, "_build_duplicate_index", lambda *a: {"active": False})
    monkeypatch.setattr(
        service.repo,
        "duplicate_classification",
        lambda *a: {"level": "certain", "reason": "Same reference"},
    )
    assert (
        service.import_rows(
            [row(reference="reused")],
            MAPPING,
            apply_rules=False,
            ignore_duplicates=True,
        ).created
        == 1
    )


def test_private_import_preview_hides_original_descriptions_accounts_and_tooltips(
    import_page, tmp_path, monkeypatch
):
    from ui.pages import tools

    path = tmp_path / "private.csv"
    path.write_text(
        "Data;Descrição;Valor;Conta\n2026-01-15;PRIVATE-DESCRIPTION;-10;PRIVATE-ACCOUNT\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tools.QFileDialog, "getOpenFileName", lambda *a: (str(path), "")
    )
    import_page.app.theme.privacy = True
    import_page.open_csv()
    text = " ".join(
        import_page.preview.item(0, column).text()
        + import_page.preview.item(0, column).toolTip()
        for column in range(7)
    )
    assert "PRIVATE-DESCRIPTION" not in text and "PRIVATE-ACCOUNT" not in text
    assert "modo privado" in import_page.import_status.text()
