#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test suite for Phase 5: Security, PII / GDPR masking, Gemini Guardrails and Filename Sanitization."""

from __future__ import annotations


from services.ai import AIService, GeminiClient
from utils.app_logging import redact_log_text
from utils.helpers import sanitize_filename


def test_sanitize_filename_alternate_data_streams():
    """Verify NTFS Alternate Data Streams and invalid characters are replaced."""
    assert (
        sanitize_filename("fatura.pdf:hidden_stream.exe")
        == "fatura.pdf_hidden_stream.exe"
    )
    assert sanitize_filename("document<1>?*.pdf") == "document_1___.pdf"

    assert sanitize_filename("../../../etc/passwd") == "______etc_passwd"
    assert sanitize_filename("   ") == "ficheiro"


def test_sanitize_filename_reserved_win32_names():
    """Verify DOS device names are safely prefixed."""
    assert sanitize_filename("CON.txt") == "safe_CON.txt"
    assert sanitize_filename("nul.pdf") == "safe_nul.pdf"
    assert sanitize_filename("AUX.csv") == "safe_AUX.csv"
    assert sanitize_filename("com1.dat") == "safe_com1.dat"
    assert sanitize_filename("lpt2.bin") == "safe_lpt2.bin"
    assert sanitize_filename("normal_file.pdf") == "normal_file.pdf"


def test_sanitize_filename_length_bounding():
    """Verify long filenames preserve their extension and are truncated safely."""
    long_name = "a" * 150 + ".pdf"
    sanitized = sanitize_filename(long_name, max_length=50)
    assert len(sanitized) <= 50
    assert sanitized.endswith(".pdf")


def test_ai_service_pii_sanitization_portuguese_data():
    """Verify AIService.sanitize_text masks Portuguese NIF, IBAN, Postal Code, Phone and Counterparties."""
    text_with_nif = (
        "Pagamento da fatura com NIF 123456789 e NIF: PT234567890 no balcão."
    )
    sanitized_nif = AIService.sanitize_text(text_with_nif)
    assert "123456789" not in sanitized_nif
    assert "234567890" not in sanitized_nif
    assert "[NIF oculto]" in sanitized_nif

    text_with_postal = (
        "Entrega na Rua Augusta 123, 1100-050 Lisboa e código 4200 123 Porto."
    )
    sanitized_postal = AIService.sanitize_text(text_with_postal)
    assert "1100-050" not in sanitized_postal
    assert "[código postal oculto]" in sanitized_postal

    text_with_phone = (
        "Contacto do fornecedor: 912345678 ou +351 961 234 567 ou 213 456 789."
    )
    sanitized_phone = AIService.sanitize_text(text_with_phone)
    assert "912345678" not in sanitized_phone
    assert "[contacto oculto]" in sanitized_phone

    text_with_counterparty = (
        "Recebido via MB WAY DE MARIA SILVA SANTOS e TRANSF DE JOAO PEREIRA."
    )
    sanitized_counterparty = AIService.sanitize_text(text_with_counterparty)
    assert "MARIA SILVA SANTOS" not in sanitized_counterparty
    assert "JOAO PEREIRA" not in sanitized_counterparty
    assert "[contraparte oculta]" in sanitized_counterparty


def test_ai_service_sanitize_transaction_rows():
    """Verify transaction rows are pruned to minimal fields and descriptions are redacted."""
    raw_rows = [
        {
            "id": 42,
            "date": "2026-08-15",
            "description": "MB WAY DE RUI SANTOS nif 501234567 tel 919999999",
            "amount": 25.50,
            "category": "Refeições",
            "subcategory": "Almoço",
            "account": "Conta Principal",
            "notes": "Segredo pessoal",
        }
    ]
    sanitized = AIService.sanitize_transaction_rows(raw_rows, keep_description=True)
    assert len(sanitized) == 1
    row = sanitized[0]
    assert row["id"] == 42
    assert row["data"] == "2026-08-15"
    assert row["valor"] == 25.50
    assert row["categoria"] == "Refeições"
    assert "account" not in row
    assert "notes" not in row
    assert "RUI SANTOS" not in row["descricao"]
    assert "501234567" not in row["descricao"]
    assert "919999999" not in row["descricao"]


def test_log_redactions_comprehensive():
    """Verify logging redactions for Portuguese fiscal identifiers and amounts."""
    log_sample = (
        "Erro na transacao com NIF: 123456789 e NIB 0033 0000 12345678901 05. "
        "Fatura com ATCUD:CS123456-12345678 no montante de 142.50 € e 500,00 EUR. "
        "Chave secreta AIzaSyD12345678901234567890."
    )
    redacted = redact_log_text(log_sample)
    assert "123456789" not in redacted
    assert "[NIF]" in redacted
    assert "[NIB]" in redacted
    assert "[ATCUD]" in redacted
    assert "[AMOUNT]" in redacted
    assert "[REDACTED_API_KEY]" in redacted


def test_gemini_client_system_instruction_payload():
    """Verify GeminiClient.generate sends system_instruction as a top-level payload object."""
    client = GeminiClient(api_key="test_api_key_12345", model="gemini-2.5-flash")
    captured_payload = {}

    def mock_request_json(method, path, *, payload, total_timeout):
        nonlocal captured_payload
        captured_payload = payload
        return {
            "candidates": [{"content": {"parts": [{"text": "Resposta de teste OK"}]}}]
        }

    client._request_json = mock_request_json  # type: ignore

    res = client.generate(
        "Qual o meu saldo?",
        system_instruction="Tu és um assistente financeiro seguro.",
        response_mime_type="application/json",
    )
    assert res == "Resposta de teste OK"
    assert "system_instruction" in captured_payload
    assert (
        captured_payload["system_instruction"]["parts"][0]["text"]
        == "Tu és um assistente financeiro seguro."
    )
    assert (
        captured_payload["generationConfig"]["responseMimeType"] == "application/json"
    )


def test_ai_service_pii_extended_identifiers():
    """Verify AIService.sanitize_text masks NISS, CC/BI, cards, API keys and counterparty variants."""
    text_sample = (
        "Beneficiário: PEDRO MIGUEL SANTOS com NISS 12012345678 e CC 12345678 9ZZ0. "
        "Pagamento de Cartao **** 1234 com chave AIzaSyD1234567890123456789012345678901. "
        "Destinatário: MARIA INES COSTA ordenante: JOAO ALVES."
    )
    sanitized = AIService.sanitize_text(text_sample)
    assert "PEDRO MIGUEL SANTOS" not in sanitized
    assert "12012345678" not in sanitized
    assert "[NISS oculto]" in sanitized
    assert "12345678 9ZZ0" not in sanitized
    assert "[documento oculto]" in sanitized
    assert "[cartão oculto]" in sanitized
    assert "AIzaSyD1234567890123456789012345678901" not in sanitized
    assert "[chave de api oculta]" in sanitized
    assert "MARIA INES COSTA" not in sanitized
    assert "JOAO ALVES" not in sanitized
    assert "[contraparte oculta]" in sanitized
