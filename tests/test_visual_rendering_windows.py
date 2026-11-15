from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="requires the real Windows Qt platform")
@pytest.mark.timeout(750)
def test_real_windows_visual_probe_has_fonts_and_readable_capture(tmp_path) -> None:
    # This end-to-end probe validates a full 16-state matrix.
    # Keep the suite watchdog outside that boundary so it can report/clean up.
    project_root = Path(__file__).resolve().parents[1]
    output_dir = tmp_path / "visual"
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "windows"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["FINANCAS_PESSOAIS_STATE_DIR"] = str(tmp_path / "profile")

    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "validate_visual_rendering.py"),
            "--output-dir",
            str(output_dir),
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=700,
        check=False,
    )

    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
    )
    manifest = json.loads(
        (output_dir / "visual_validation.json").read_text(encoding="utf-8")
    )
    assert manifest["platform"].casefold() == "windows"
    assert manifest["font_families_available"] >= 10
    assert manifest["font_family"]
    startup = manifest["startup_window"]
    assert startup["size"] == [520, 270]
    assert startup["percent"] == 64
    assert startup["stage"] == "Guardar documentos protegidos"
    assert startup["detail"].startswith("38/57")
    assert startup["sampled_colours"] >= 24
    assert (output_dir / startup["file"]).is_file()
    message_boxes = {capture["theme"]: capture for capture in manifest["message_boxes"]}
    assert set(message_boxes) == {"light", "dark"}
    assert message_boxes["light"]["median_lightness"] > 205
    assert message_boxes["dark"]["median_lightness"] < 90
    assert message_boxes["light"]["palette_surface"] == "#f8faff"
    assert message_boxes["dark"]["palette_surface"] == "#111827"
    for capture in message_boxes.values():
        assert capture["size"][0] >= 420
        assert capture["size"][1] >= 180
        assert capture["sampled_colours"] >= 20
        assert capture["local_stylesheet"] is True
        assert capture["native_dialog_disabled"] is True
        assert capture["message_wrapped"] is True
        assert (output_dir / capture["file"]).is_file()
    assert len(manifest["captures"]) == 16
    assert {tuple(capture["viewport"]) for capture in manifest["captures"]} == {
        (960, 900),
        (1280, 900),
        (1600, 900),
        (1920, 1080),
    }
    assert {
        (capture["theme"], capture["privacy"]) for capture in manifest["captures"]
    } == {
        ("light", False),
        ("light", True),
        ("dark", False),
        ("dark", True),
    }
    for capture in manifest["captures"]:
        assert capture["sampled_colours"] >= 32
        assert len(capture["perceptual_hash"]) == 64
        assert set(capture["perceptual_hash"]) <= set("0123456789abcdef")
        assert capture["metric_values"] >= 3
        width = capture["viewport"][0]
        assert capture["expanded_filter_card_height"] <= 270
        assert capture["filter_card_height"] < capture["expanded_filter_card_height"]
        assert capture["filter_controls_visible"] is False
        assert capture["filter_group_rows"] == (1 if width >= 1600 else 2)
        assert capture["filter_row_sizes"] == ([5] if width >= 1600 else [2, 3])
        assert capture["filter_layout_mode"] == ("wide" if width >= 1600 else "medium")
        assert "3 meses" in capture["filter_summary"]
        assert capture["sticky_summary_visible_on_scroll"] is True
        assert capture["sticky_summary_text"] == capture["filter_summary"]
        assert all(capture["dashboard_card_contexts"].values())
        assert len(capture["dashboard_card_contexts"]) == 15
        assert all(capture["metric_contexts"])
        assert "Dados até" in capture["chart_data_context"]
        assert "confiança" in capture["chart_data_context"]
        assert "Dados até" in capture["budget_data_context"]
        assert capture["category_comparison_visible"] is True
        assert "Vs." in capture["category_comparison_text"]
        assert capture["wrapped_metric_statuses"] == capture["metric_values"]
        assert capture["status_overflow"] == 0
        assert capture["wrapped_label_overflow"] == 0
        assert capture["nav_label_overflow"] == 0
        if width < 1120:
            assert capture["sidebar_width"] == 142
            assert capture["sidebar_contextual"] is True
            assert any(
                len(label.split(maxsplit=1)) > 1
                for label in capture["visible_nav_labels"]
            )
            assert capture["topbar_compact"] is True
        else:
            assert capture["sidebar_width"] >= 184
            assert capture["sidebar_contextual"] is False
            assert capture["topbar_compact"] is False
        if capture["privacy"]:
            assert capture["masked_metric_values"] >= 3
        else:
            assert capture["masked_metric_values"] == 0
        assert (output_dir / capture["file"]).is_file()
    editor = manifest["transaction_editor"]
    assert editor["viewport"] == [1920, 1080]
    assert 620 <= editor["size"][0] <= 780
    assert 520 <= editor["size"][1] <= 720
    assert editor["horizontal_scroll"] == 0
    assert editor["parent_blurred"] is False
    assert editor["field_labels"] == 13
    assert editor["sampled_colours"] >= 28
    assert (output_dir / editor["file"]).is_file()
    assert {chart["key"] for chart in manifest["feature_charts"]} == {
        "cashflow_pulse",
        "cash_runway_probability",
        "budget_variance_heatmap",
        "data_quality_trend",
        "fixed_commitments",
        "net_worth_bridge",
        "portfolio_contribution",
        "recurring_commitment_timeline",
        "savings_efficiency",
    }
    for chart in manifest["feature_charts"]:
        assert chart["rows"] > 0
        assert chart["comparison_rows"] > 0
        assert chart["size"][0] >= 300
        assert chart["size"][1] >= 340
        assert chart["sampled_colours"] >= 24
        assert (output_dir / chart["file"]).is_file()
    assert {card["key"] for card in manifest["feature_cards"]} == {
        "category_comparison",
        "decision_intelligence",
        "score_context",
    }
    for card in manifest["feature_cards"]:
        assert card["sampled_colours"] >= 24
        assert (output_dir / card["file"]).is_file()
    category_card = next(
        card
        for card in manifest["feature_cards"]
        if card["key"] == "category_comparison"
    )
    assert "Vs." in category_card["comparison_text"]
    assert category_card["comparison_height"] <= 72
    assert category_card["title_gap"] <= 20
    score_card = next(
        card for card in manifest["feature_cards"] if card["key"] == "score_context"
    )
    assert "Dados até" in score_card["data_context"]
    assert {card["key"] for card in manifest["period_filter_cards"]} == {
        "cashflow_pulse_annual",
        "savings_efficiency_annual",
    }
    for card in manifest["period_filter_cards"]:
        assert card["period_label"] == "último ano"
        assert card["rows"] >= 12
        assert card["comparison_rows"] >= 12
        assert "último ano" in card["title"]
        assert card["sampled_colours"] >= 24
        assert (output_dir / card["file"]).is_file()
