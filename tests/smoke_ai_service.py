#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline smoke checks for the AI service boundary."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.repo import Repo  # noqa: E402
from services.ai import (
    GEMINI_RECOMMENDED_TEXT_MODELS,  # noqa: E402
    AIService,
    GeminiClient,
)


def test_ai_transaction_payload_is_minimal_and_redacted() -> None:
    rows = [
        {
            "id": 17,
            "date": "2026-07-01",
            "description": "Transferência PT50 0002 0123 1234 5678 9015 4 para a@b.pt",
            "amount": -42.0,
            "category": "Outros",
            "account": "Conta privada",
            "payment_method": "Cartão",
        }
    ]

    sanitized = AIService.sanitize_transaction_rows(rows)

    assert sanitized[0]["id"] == 17
    assert sanitized[0]["data"] == "2026-07-01"
    assert "IBAN oculto" in sanitized[0]["descricao"]
    assert "email oculto" in sanitized[0]["descricao"]
    assert "account" not in sanitized[0]
    assert "payment_method" not in sanitized[0]
    assert AIService.detailed_payload_fields(sanitized) == [
        "identificador",
        "data",
        "descrição",
        "valor",
        "categoria",
    ]


def main() -> int:
    repo = Repo()
    service = AIService(repo)
    current_month = date.today().strftime("%Y-%m")

    preferences = service.preferences()
    expected_keys = {
        "enabled",
        "use_env",
        "has_api_key",
        "key_source",
        "key_hint",
        "env_key_present",
        "model",
        "privacy_aggregated",
        "anonymize",
        "ask_before_details",
    }
    missing = expected_keys.difference(preferences)
    if missing:
        raise AssertionError(f"Missing AI preference keys: {sorted(missing)}")

    context = service.month_context(current_month, include_descriptions=True)
    aggregate_context = service.month_context(current_month, include_descriptions=False)
    if context.get("mes") != current_month:
        raise AssertionError("AI month context returned the wrong month")
    if not isinstance(context.get("qualidade_dados"), dict):
        raise AssertionError("AI month context has no data-quality summary")
    if not isinstance(context.get("movimentos_amostra"), list):
        raise AssertionError("AI detailed context has no transaction sample")
    if "movimentos_amostra" in aggregate_context:
        raise AssertionError("Aggregate AI context exposed transaction descriptions")
    if any(
        "descricao" in recurring or "description" in recurring
        for recurring in aggregate_context.get("possiveis_recorrentes", [])
    ):
        raise AssertionError("Aggregate recurring data exposed descriptions")
    if not isinstance(service.uncategorized_context(5), list):
        raise AssertionError("AI uncategorized context should be a list")
    if not isinstance(service.history_months(), list):
        raise AssertionError("AI history months should be a list")
    if not isinstance(service.history(current_month, 5), list):
        raise AssertionError("AI history should be a list")

    if hasattr(repo, "gemini_client") or hasattr(repo, "ai_month_context"):
        raise AssertionError("Repo still exposes AI service responsibilities")
    if not GEMINI_RECOMMENDED_TEXT_MODELS:
        raise AssertionError("Gemini model recommendations are empty")

    try:
        GeminiClient("", "gemini-2.5-flash").generate("test")
    except RuntimeError:
        pass
    else:
        raise AssertionError("GeminiClient accepted an empty API key")

    print("AI service smoke OK")
    print("model", preferences.get("model"))
    print("context_categories", len(context.get("categorias_top", [])))
    print("context_transactions", len(context.get("movimentos_amostra", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
