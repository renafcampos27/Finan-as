from __future__ import annotations

from typing import Any

from services.imports import ImportService


class ImportRepoStub:
    def suggested_category_for_description(self, _description: str) -> str:
        return ""

    def setting(self, _key: str, default: str = "") -> str:
        return default

    def set_setting(self, _key: str, _value: str) -> None:
        return None

    def is_duplicate_transaction(self, *_args: Any) -> bool:
        return False


def test_invalid_or_missing_import_fields_require_review() -> None:
    service = ImportService(ImportRepoStub())
    mapping = {
        "date": "Data",
        "desc": "Descrição",
        "amount": "Valor",
        "category": "",
        "account": "",
        "method": "",
        "merchant": "",
    }

    invalid_date = service.normalize_row(
        {"Data": "31/02/2026", "Descrição": "Compra", "Valor": "-10"},
        mapping,
        apply_rules=False,
    )
    missing_description = service.normalize_row(
        {"Data": "16/07/2026", "Descrição": "", "Valor": "-10"},
        mapping,
        apply_rules=False,
    )

    assert invalid_date.date == ""
    assert not invalid_date.is_valid
    assert missing_description.description == ""
    assert not missing_description.is_valid
