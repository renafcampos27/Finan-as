from __future__ import annotations

from pathlib import Path
from typing import Any

from services.bank_parsers import OFXParser, QIFParser
from services.imports import (
    AMOUNT_LOCALE_PT,
    ImportService,
)
from utils.helpers import parse_flexible_date


class MockRepo:
    def __init__(self) -> None:
        self.rules = [
            {"keyword": "bp", "category": "Transporte"},
            {"keyword": "cp", "category": "Transporte"},
            {"keyword": "bpi", "category": "Finanças"},
            {"keyword": "salário", "category": "Rendimentos"},
        ]
        self.settings: dict[str, str] = {}
        self.existing_transactions: list[dict[str, Any]] = []

    def category_rules(self) -> list[dict[str, Any]]:
        return self.rules

    def suggested_category_for_description(self, description: str) -> str:
        import re

        text = str(description or "").casefold()
        for rule in self.rules:
            keyword = str(rule["keyword"] or "").strip().casefold()
            if keyword and str(rule["category"] or "").strip():
                if len(keyword) <= 3:
                    pattern = rf"(?<!\w){re.escape(keyword)}(?!\w)"
                    if re.search(pattern, text):
                        return str(rule["category"]).strip()
                elif keyword in text:
                    return str(rule["category"]).strip()
        return ""

    def setting(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = str(value)

    def duplicate_classification(
        self,
        date_str: str,
        description: str,
        amount: float,
        account: str = "",
        external_reference: str = "",
        merchant: str = "",
    ) -> dict[str, Any]:
        ref = external_reference.strip().lower()
        d = date_str[:10]
        amt = round(float(amount), 2)
        desc = description.strip().lower()

        for tx in self.existing_transactions:
            tx_ref = str(tx.get("external_reference", "")).strip().lower()
            if ref and tx_ref and ref == tx_ref:
                return {
                    "level": "certain",
                    "reason": "A mesma referência bancária já existe nesta conta.",
                }
            if tx.get("date") == d and round(float(tx.get("amount", 0)), 2) == amt:
                if tx.get("description", "").strip().lower() == desc:
                    return {
                        "level": "probable",
                        "reason": "Existe uma compra muito semelhante.",
                    }
                return {
                    "level": "possible",
                    "reason": "O valor e a data coincidem, mas a descrição é diferente.",
                }
        return {"level": "none", "reason": ""}

    def import_transactions_batch(
        self,
        rows: list[dict[str, Any]],
        *,
        commit: bool = True,
    ) -> list[int]:
        ids: list[int] = []
        for i, r in enumerate(rows, start=len(self.existing_transactions) + 1):
            self.existing_transactions.append(
                {
                    "id": i,
                    "date": r["date"],
                    "description": r["description"],
                    "amount": r["amount"],
                    "account": r["account"],
                    "external_reference": r.get("external_reference", ""),
                }
            )
            ids.append(i)
        return ids


def test_portuguese_dates_with_timestamps_and_months():
    # Timestamps
    d1 = parse_flexible_date("2026-08-24 14:32:10")
    assert d1 is not None and d1.isoformat() == "2026-08-24"

    d2 = parse_flexible_date("24/08/2026 14:32:10")
    assert d2 is not None and d2.isoformat() == "2026-08-24"

    d3 = parse_flexible_date("2026-08-24T18:00:00Z")
    assert d3 is not None and d3.isoformat() == "2026-08-24"

    # Portuguese month names
    d4 = parse_flexible_date("24-Ago-2026")
    assert d4 is not None and d4.isoformat() == "2026-08-24"

    d5 = parse_flexible_date("15 de Janeiro de 2026")
    assert d5 is not None and d5.isoformat() == "2026-01-15"

    # Compact format YYYYMMDD
    d6 = parse_flexible_date("20260625")
    assert d6 is not None and d6.isoformat() == "2026-06-25"


def test_category_rule_word_boundaries_prevent_false_positives():
    repo = MockRepo()
    service = ImportService(repo)

    # "bp" rule should match "Abastecimento BP" but NOT "Transferência BPI"
    assert service.suggest_category("Abastecimento BP Oeiras") == "Transporte"
    assert service.suggest_category("Transferência Banco BPI") != "Transporte"

    # "cp" rule should match "Bilhete CP Lisboa" but NOT "Copo de agua"
    assert service.suggest_category("Bilhete CP Lisboa") == "Transporte"
    assert service.suggest_category("Copo de café") != "Transporte"


def test_cgd_statement_with_metadata_headers_and_split_debit_credit(tmp_path: Path):
    cgd_content = """Extrato de Conta à Ordem
Conta: 0035 0123 4567890 01
Titular: JOAO MANUEL SILVA
Periodo: 01-01-2026 a 31-01-2026
Saldo Inicial: 1.250,50 EUR

Data Mov.;Data Valor;Descritivo;Valor / Débito;Valor / Crédito;Saldo Contabilístico
05-01-2026;05-01-2026;CONTINENTE BOM DIA;54,20;;1.196,30
10-01-2026;10-01-2026;VENCIMENTO EMPRESA;;2.100,00;3.296,30
15-01-2026;15-01-2026;GALP ENERGIA;35,00;;3.261,30
"""
    file_path = tmp_path / "cgd_extrato.csv"
    file_path.write_text(cgd_content, encoding="utf-8-sig")

    service = ImportService(MockRepo())
    data = service.read_file(file_path)

    assert len(data.rows) == 3
    assert "Valor / Débito" in data.headers
    assert "Valor / Crédito" in data.headers
    assert len(data.warnings) > 0  # Warned about skipped metadata header lines

    suggested = service.suggested_mapping(data.headers)
    assert suggested["date"] == "Data Mov."
    assert suggested["desc"] == "Descritivo"
    assert suggested["amount_debit"] == "Valor / Débito"
    assert suggested["amount_credit"] == "Valor / Crédito"

    preview = service.preview(
        data.rows,
        suggested,
        apply_rules=True,
        amount_locale=AMOUNT_LOCALE_PT,
    )
    assert preview.total == 3
    assert preview.invalid == 0
    # Debit row is negative expense
    assert preview.rows[0].transaction.amount == -54.20
    assert preview.rows[0].transaction.category == "Supermercado"
    # Credit row is positive income
    assert preview.rows[1].transaction.amount == 2100.00
    assert preview.rows[1].transaction.category == "Rendimentos"


def test_ofx_sgml_parser_and_import(tmp_path: Path):
    ofx_content = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>EUR
<BANKACCTFROM>
<BANKID>0035
<ACCTID>003501234567
<ACCTTYPE>CHECKING
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260101
<DTEND>20260131
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260105120000
<TRNAMT>-42.50
<FITID>TX-20260105-001
<NAME>MERCADONA MATOSINHOS
<MEMO>COMPRA CARTAO
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260128090000
<TRNAMT>1850.00
<FITID>TX-20260128-002
<NAME>TRANSFERENCIA SALARIO
<MEMO>ORDENADO JANEIRO
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""
    file_path = tmp_path / "statement.ofx"
    file_path.write_text(ofx_content, encoding="utf-8")

    parsed = OFXParser.parse(ofx_content)
    assert len(parsed.rows) == 2
    assert parsed.rows[0]["Data"] == "2026-01-05"
    assert parsed.rows[0]["Valor"] == "-42.50"
    assert parsed.rows[0]["Referência bancária"] == "TX-20260105-001"
    assert "MERCADONA" in parsed.rows[0]["Descrição"]

    # Test via ImportService.read_file
    service = ImportService(MockRepo())
    data = service.read_file(file_path)
    assert len(data.rows) == 2
    suggested = service.suggested_mapping(data.headers)
    preview = service.preview(data.rows, suggested, apply_rules=True)
    assert preview.total == 2
    assert preview.invalid == 0
    assert preview.rows[0].transaction.amount == -42.50
    assert preview.rows[0].transaction.category == "Supermercado"
    assert preview.rows[1].transaction.amount == 1850.00
    assert preview.rows[1].transaction.category == "Rendimentos"


def test_qif_parser_and_import(tmp_path: Path):
    qif_content = """!Type:Bank
D12/01/2026
T-18.90
PNetflix International
MSubscricao Mensal
NREF-NETFLIX-01
^
D20/01/2026
T-32.40
PUber Trip
MViagem Lisboa
^
"""
    file_path = tmp_path / "statement.qif"
    file_path.write_text(qif_content, encoding="utf-8")

    parsed = QIFParser.parse(qif_content)
    assert len(parsed.rows) == 2
    # A07: retain the original cell until the complete batch resolves its order.
    assert parsed.rows[0]["Data"] == "12/01/2026"
    assert parsed.rows[0]["Valor"] == "-18.90"
    assert "Netflix" in parsed.rows[0]["Descrição"]

    service = ImportService(MockRepo())
    data = service.read_file(file_path)
    assert len(data.rows) == 2
    suggested = service.suggested_mapping(data.headers)
    preview = service.preview(data.rows, suggested, apply_rules=True)
    assert preview.total == 2
    assert preview.rows[0].transaction.date == "2026-01-12"
    assert preview.rows[0].transaction.category == "Subscrições"
    assert preview.rows[1].transaction.category == "Transporte"


def test_overlapping_statement_reimport_keeps_probable_rows_for_review(tmp_path: Path):
    repo = MockRepo()
    service = ImportService(repo)

    # First statement: Jan + Feb movements
    batch1 = [
        {
            "Data": "2026-01-15",
            "Descrição": "Mercadona",
            "Valor": "-50.00",
            "Ref": "REF1",
        },
        {
            "Data": "2026-02-10",
            "Descrição": "Pingo Doce",
            "Valor": "-30.00",
            "Ref": "",
        },  # No external reference
    ]
    mapping = {
        "date": "Data",
        "desc": "Descrição",
        "amount": "Valor",
        "reference": "Ref",
    }
    result1 = service.import_rows(
        batch1, mapping, apply_rules=True, ignore_duplicates=True
    )
    assert result1.created == 2
    assert result1.skipped_duplicates == 0

    # Overlapping second statement: Feb + Mar movements
    batch2 = [
        {
            "Data": "2026-02-10",
            "Descrição": "Pingo Doce",
            "Valor": "-30.00",
            "Ref": "",
        },  # Overlap! (Probable duplicate)
        {"Data": "2026-03-01", "Descrição": "Galp", "Valor": "-40.00", "Ref": "REF3"},
    ]
    result2 = service.import_rows(
        batch2,
        mapping,
        apply_rules=True,
        ignore_duplicates=True,
        ignore_probable=True,
    )
    # Without a corroborated bank reference, identical text may be a real purchase.
    # The legacy ignore_probable flag cannot discard uncertain rows automatically.
    assert result2.created == 2
    assert result2.skipped_duplicates == 0
