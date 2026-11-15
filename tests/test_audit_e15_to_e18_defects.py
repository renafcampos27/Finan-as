from __future__ import annotations

import json
import os
from pathlib import Path
import struct
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FINANCAS_PESSOAIS_TESTING"] = "1"

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from services.backup_crypto import (
    BackupCryptoCodec,
    CredentialKind,
    CryptoFormatError,
    SecurityEnvelope,
    build_envelope,
)
from services.financial_intelligence import monte_carlo_projection
from ui.components.chart_details import ChartDetailDialog
from ui.components.charts.base import StableChart
from ui.core import Theme


def _get_app() -> QApplication:
    return QApplication.instance() or QApplication([])


# ============================================================================
# E15 (C41): Generic chart details fail-closed under privacy mode & PlainText
# ============================================================================

class _DummyChart(StableChart):
    def __init__(self, privacy: bool):
        super().__init__(lambda: Theme(dark=False, privacy=privacy))


def test_e15_unknown_numeric_fields_and_zero_fail_closed_under_privacy():
    _get_app()
    chart_priv = _DummyChart(privacy=True)
    chart_pub = _DummyChart(privacy=False)

    # 1. Unknown numeric fields must fail-closed to "••••• €" under privacy mode
    assert chart_priv._format_detail_value("fees_paid", 150.0) == "••••• €"
    assert chart_priv._format_detail_value("taxes_paid", 42.5) == "••••• €"
    assert chart_priv._format_detail_value("gross_gain", 300.0) == "••••• €"
    assert chart_priv._format_detail_value("dividend", 12.0) == "••••• €"
    assert chart_priv._format_detail_value("completely_unrecognized_metric", 999.99) == "••••• €"

    # Zero values must also be masked under privacy mode
    assert chart_priv._format_detail_value("fees_paid", 0.0) == "••••• €"
    assert chart_priv._format_detail_value("completely_unrecognized_metric", 0) == "••••• €"

    # 2. In public (non-privacy) mode, values are formatted properly
    assert "150" in chart_pub._format_detail_value("fees_paid", 150.0)
    assert "0" in chart_pub._format_detail_value("fees_paid", 0.0)
    assert "999,99" in chart_pub._format_detail_value("completely_unrecognized_metric", 999.99)

    # A05 hides activity counts; non-sensitive scores remain available.
    assert chart_priv._format_detail_value("count", 5) == "•••"
    assert chart_priv._format_detail_value("score", 85) == "85"


def test_e15_chart_details_redaction_and_plain_text():
    _get_app()
    theme_priv = Theme(dark=False, privacy=True)
    theme_pub = Theme(dark=False, privacy=False)

    payload = {
        "title": "<b>Relatório</b> de Gastos",
        "subtitle": "Conta de user@empresa.pt",
        "explanation": "Pagamento para o IBAN PT50000012345678901234567.",
        "metrics": [
            {"label": "Comissão admin@banco.pt", "value": 45.0, "tone": "negative"},
            {"label": "Taxa", "value": "12.5%", "tone": "neutral"},
            {"label": "Lucro não mapeado", "value": "350.00", "tone": "positive"},
        ],
        "rows": [
            {
                "date": "2026-08-01",
                "title": "Transferência contacto@loja.com",
                "subtitle": "PT50999988887777666655554",
                "value": 125.50,
            }
        ],
    }

    # Under privacy mode:
    dialog_priv = ChartDetailDialog(payload, theme_priv)

    # PlainText format enforced on untrusted labels
    assert dialog_priv.title_label.textFormat() == Qt.PlainText
    assert dialog_priv.subtitle_label.textFormat() == Qt.PlainText
    assert dialog_priv.explanation_label.textFormat() == Qt.PlainText
    assert dialog_priv.rows_title.textFormat() == Qt.PlainText

    # Redactions
    assert dialog_priv.subtitle_label.text() == "Dados ocultos"
    assert "user@empresa.pt" not in dialog_priv.subtitle_label.text()

    assert "[IBAN]" in dialog_priv.explanation_label.text()
    assert "PT50000012345678901234567" not in dialog_priv.explanation_label.text()

    # Metric labels and values masked
    assert "[EMAIL]" in dialog_priv.metric_labels[0].text()
    assert "••••• €" in dialog_priv.metric_labels[0].text()
    assert "12.5%" in dialog_priv.metric_labels[1].text()  # Percentages retained
    assert "••••• €" in dialog_priv.metric_labels[2].text()  # Unknown unmasked string masked

    # Movement row masked
    row_frame = dialog_priv.rows_layout.itemAt(0).widget()
    row_date = row_frame.findChild(object, "ChartDetailRowDate")
    row_title = row_frame.findChild(object, "ChartDetailRowTitle")
    row_sub = row_frame.findChild(object, "ChartDetailRowSubtitle")
    row_val = row_frame.findChild(object, "ChartDetailRowValue")

    assert row_date.textFormat() == Qt.PlainText
    assert row_title.textFormat() == Qt.PlainText
    assert row_val.textFormat() == Qt.PlainText

    assert row_title.text() == "Dados ocultos"
    assert "contacto@loja.com" not in row_title.text()
    assert row_sub.text() == "Dados ocultos"
    assert "PT50999988887777666655554" not in row_sub.text()
    assert row_val.text() == "••••• €"

    # Under public mode:
    dialog_pub = ChartDetailDialog(payload, theme_pub)
    row_frame_pub = dialog_pub.rows_layout.itemAt(0).widget()
    row_val_pub = row_frame_pub.findChild(object, "ChartDetailRowValue")
    assert "125,50" in row_val_pub.text()


# ============================================================================
# E16 (C42): AI-secret migration decoupled from preferences getter
# ============================================================================

def test_e16_preferences_getter_does_not_scrub_backups_or_mutate_disk(tmp_path: Path):
    from services.ai import AIService

    repo = MagicMock()
    repo.ai_settings.return_value = {
        "enabled": False,
        "use_env": False,
        "legacy_api_key": "sk-legacy-test-key",
        "secret_migration_done": False,
    }
    stored_state = {"key": ""}
    secret_store = MagicMock()
    secret_store.read.side_effect = lambda: stored_state["key"]
    secret_store.write.side_effect = lambda k: stored_state.update({"key": k})

    backup_service = MagicMock()
    backup_service.backup_directory = tmp_path
    backup_service.scrub_ai_secret_from_backups = MagicMock()

    service = AIService(
        repo=repo,
        backup_service=backup_service,
        payments_service=MagicMock(),
        secret_store=secret_store,
    )

    # 1. Calling preferences() must NOT invoke scrub_ai_secret_from_backups()
    prefs = service.preferences()
    assert backup_service.scrub_ai_secret_from_backups.call_count == 0
    assert prefs["migration_pending"] is True
    assert prefs["legacy_key_pending"] is True
    assert repo.clear_legacy_ai_api_key.call_count == 0
    assert repo.mark_ai_secret_migrated.call_count == 0

    # 2. Calling migrate_ai_secrets() explicitly executes the migration
    backup_service.scrub_ai_secret_from_backups.return_value = {
        "scrubbed": 2,
        "failed": 0,
        "cancelled": False,
    }
    result = service.migrate_ai_secrets()
    assert backup_service.scrub_ai_secret_from_backups.call_count == 1
    assert result["done"] is True
    assert repo.mark_ai_secret_migrated.call_count == 1
    assert repo.clear_legacy_ai_api_key.call_count == 1

    # 3. Cancellation support in migrate_ai_secrets
    backup_service.scrub_ai_secret_from_backups.reset_mock()
    cancel_result = service.migrate_ai_secrets(cancel_check=lambda: True)
    assert cancel_result["cancelled"] is True
    assert cancel_result["done"] is False


# ============================================================================
# E17 (C43): Monte Carlo correlation discloses 1-factor model & metadata
# ============================================================================

def test_e17_monte_carlo_discloses_one_factor_approximation():
    result = monte_carlo_projection(
        contribution=200.0,
        years=3,
        simulations=150,
        asset_weights=[0.6, 0.4],
        correlation_pct=50.0,
        seed=101,
    )

    # 1. Model type and version explicitly state one-factor approximation
    assert result["model_type"] == "one_factor_volatility_adjustment"
    assert result["model_version"] == "1.1-scalar-approx"
    assert result["is_multi_asset_covariance"] is False
    assert "aproximação" in result["model_description"].casefold()

    # 2. Method label discloses the one-factor nature and lack of multi-asset covariance
    assert "modelo de 1 fator" in result["method_label"]
    assert "sem covariância multi-ativo" in result["method_label"]

    # 3. Assumptions dictionary contains verified parameters
    assumptions = result["assumptions"]
    assert assumptions["asset_count"] == 2
    assert pytest.approx(sum(assumptions["weights"])) == 1.0
    assert "effective_volatility_pct" in assumptions
    assert "concentration_hhi" in assumptions


# ============================================================================
# E18 (C44): LGBK2 default_unlock_method authenticated in payload AAD
# ============================================================================

def _make_dummy_envelope(tmp_path: Path) -> tuple[SecurityEnvelope, bytearray]:
    master_key = bytearray(os.urandom(32))
    envelope = build_envelope(
        profile_id="test-profile-e18",
        default_unlock_method=str(CredentialKind.PASSWORD),
        master_key=master_key,
        credentials={
            str(CredentialKind.PASSWORD): b"TestPassword123!",
            str(CredentialKind.PIN): b"1234",
        },
    )
    return envelope, master_key


def test_e18_tampered_default_unlock_method_rejected_by_aad(tmp_path: Path):
    envelope, master_key = _make_dummy_envelope(tmp_path)

    # 1. Create a dummy source zip file
    source_file = tmp_path / "data.zip"
    source_file.write_bytes(bytes([0x50, 0x4B, 0x05, 0x06]) + bytes(18))

    target_lgbk2 = tmp_path / "backup.lgbk2"
    BackupCryptoCodec.encrypt_v2(
        source_file,
        target_lgbk2,
        master_key=bytes(master_key),
        envelope=envelope,
        payload_kind="Z",
    )

    # 2. Verify normal decryption with session key succeeds
    out_file = tmp_path / "decrypted.zip"
    BackupCryptoCodec.decrypt(
        target_lgbk2,
        out_file,
        session_master_key=bytes(master_key),
    )
    assert out_file.exists()
    out_file.unlink()

    # 3. Read raw bytes and tamper with default_unlock_method in the JSON header
    raw = bytearray(target_lgbk2.read_bytes())
    header_length = struct.unpack(">I", raw[6:10])[0]
    header_json = json.loads(bytes(raw[10 : 10 + header_length]).decode("utf-8"))

    # Tamper with default_unlock_method: switch from "password" to "pin"
    assert header_json["default_unlock_method"] == "password"
    header_json["default_unlock_method"] = "pin"

    forged_header = json.dumps(
        header_json, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    tampered_target = tmp_path / "tampered.lgbk2"
    tampered_target.write_bytes(
        bytes(raw[:6])
        + struct.pack(">I", len(forged_header))
        + forged_header
        + bytes(raw[10 + header_length :])
    )

    # 4. Decrypting tampered file must fail authentication tag verification (CryptoFormatError)
    with pytest.raises(CryptoFormatError):
        BackupCryptoCodec.decrypt(
            tampered_target,
            out_file,
            session_master_key=bytes(master_key),
        )
