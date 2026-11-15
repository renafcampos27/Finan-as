from __future__ import annotations

from services.export_documents import build_movements_pdf_html
from utils.helpers import eur


def test_movement_pdf_escapes_user_content() -> None:
    report = build_movements_pdf_html(
        [
            {
                "date": "2026-07-25",
                "description": "<script>alert('x')</script> & café",
                "category": "Casa < manutenção",
                "account": 'Conta "principal"',
                "amount": -12.5,
            }
        ]
    )

    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert "&amp; café" in report
    assert "Casa &lt; manutenção" in report
    assert "Conta &quot;principal&quot;" in report


def test_movement_pdf_respects_privacy_mode() -> None:
    report = build_movements_pdf_html(
        [{"date": "2026-07-25", "description": "Teste", "amount": -1234.56}],
        privacy=True,
    )

    assert eur(-1234.56, False) not in report
    assert eur(-1234.56, True) in report
    assert "color:#334155" in report
