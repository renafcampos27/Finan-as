# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
import pytest
from services.imports import ImportService


def test_detect_portuguese_bank_dialects():
    cgd = [
        "Data movimento",
        "Data valor",
        "Descricao",
        "Montante",
        "Saldo contabilistico",
    ]
    assert "CGD" in ImportService.detect_bank_dialect(cgd)

    bcp = ["Data Mov.", "Data Valor", "Movimento", "Importancia", "Saldo"]
    assert "BCP" in ImportService.detect_bank_dialect(bcp)

    santander = ["Data da Operacao", "Data Valor", "Descricao", "Montante", "Saldo"]
    assert "Santander" in ImportService.detect_bank_dialect(santander)

    revolut = [
        "Type",
        "Product",
        "Started Date",
        "Completed Date",
        "Description",
        "Amount",
    ]
    assert "Revolut" in ImportService.detect_bank_dialect(revolut)

    generic = ["Col1_X", "Col2_Y"]
    assert "Genérico" in ImportService.detect_bank_dialect(generic)


def test_pt_banking_suggested_mapping():
    headers = [
        "Data movimento",
        "Descrição da operação",
        "Montante",
        "Referência bancária",
    ]
    mapping = ImportService.suggested_mapping(headers)
    assert mapping["date"] == "Data movimento"
    assert mapping["desc"] == "Descrição da operação"
    assert mapping["amount"] == "Montante"
    assert mapping["reference"] == "Referência bancária"


def test_csv_reading_and_preview_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from database.repo.main_repo import Repo

    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    monkeypatch.setenv("FINANCAS_PESSOAIS_STATE_DIR", str(tmp_path / "profile"))

    repo = Repo()
    try:
        service = ImportService(repo)
        csv_file = tmp_path / "extrato_cgd.csv"
        csv_lines = [
            "Data movimento,Descrição,Montante,Saldo contabilístico",
            "2026-08-20,Compras Pingo Doce,-45.50,1200.00",
            "2026-08-21,Vencimento Mensal,+1500.00,2700.00",
        ]
        csv_file.write_text("\n".join(csv_lines) + "\n", encoding="utf-8")
        data = service.read_file(csv_file)
        assert len(data.rows) == 2
        bank = service.detect_bank_dialect(data.headers)
        assert "CGD" in bank

        mapping = service.suggested_mapping(data.headers)
        preview = service.preview(data.rows, mapping, amount_locale="international")
        assert preview.total == 2
        assert preview.certain_duplicates == 0
    finally:
        repo.close()
