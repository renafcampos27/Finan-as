"""Evidence-preserving disposal worksheets; no inferred sales or tax-law changes."""

from __future__ import annotations

import csv
import html
import io
import json
import math
import uuid
from datetime import date
from typing import Any, Iterable

from utils.helpers import row_get
from utils.output_security import spreadsheet_safe_row


DISPOSAL_PREFIX = "investment_disposal:v1:"
REPORT_LIMITATION = (
    "Mapa de apoio de vendas registadas; não constitui uma declaração fiscal. "
    "Confirma os comprovativos, lotes e regras do ano fiscal antes de declarar. "
    "Posições abertas e notas livres não são tratadas como vendas."
)


def _number(value: Any, *, minimum: float = 0) -> float:
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError("O valor deve ser finito e não negativo.")
    return result


def _historical_rate(holding: Any, supplied: float | None) -> float | None:
    currency = str(row_get(holding, "currency", "EUR") or "EUR").upper()
    if currency == "EUR":
        return 1.0
    value = supplied
    if value is None:
        for key in ("historical_exchange_rate", "acquisition_exchange_rate"):
            candidate = row_get(holding, key, None)
            if candidate is not None:
                value = candidate
                break
    if value is None:
        return None
    result = _number(value)
    if result <= 0:
        raise ValueError("O câmbio histórico deve ser superior a zero.")
    return result


def create_disposal_record(
    holding: Any,
    *,
    sale_date: str,
    sale_value_eur: float,
    sale_fees_eur: float,
    acquisition_exchange_rate: float | None = None,
) -> dict[str, Any]:
    """Freeze a full-position sale with original acquisition data and EUR proceeds.

    The holding is one manually maintained position, not a reconstructed FIFO
    history. Separate positions remain separate records even with equal names.
    """
    day = date.fromisoformat(sale_date).isoformat()
    snapshot = dict(holding)
    record = {
        "schema": 1,
        "id": uuid.uuid4().hex,
        "kind": "realized_full_position",
        "source": "explicit_position_close",
        "holding": snapshot,
        "sale_date": day,
        "sale_value_eur": _number(sale_value_eur),
        "sale_fees_eur": _number(sale_fees_eur),
        "sale_currency": "EUR",
        "sale_exchange_rate": 1.0,
        "acquisition_exchange_rate": _historical_rate(
            holding, acquisition_exchange_rate
        ),
        "provenance": "Posição integral confirmada pelo utilizador; sem inferência de notas.",
    }
    acquisition = str(row_get(holding, "acquisition_date", "") or "")
    if acquisition:
        try:
            acquired = date.fromisoformat(acquisition)
        except ValueError:
            pass  # Preserve invalid original evidence; report it as incomplete.
        else:
            if acquired.isoformat() > day:
                raise ValueError("A venda não pode anteceder a aquisição registada.")
    return record


def _evaluate_record(record: dict[str, Any]) -> dict[str, Any]:
    if (
        record.get("schema") != 1
        or record.get("kind") != "realized_full_position"
        or record.get("source") != "explicit_position_close"
        or not isinstance(record.get("holding"), dict)
        or not isinstance(record.get("id"), str)
        or len(record["id"]) != 32
        or any(c not in "0123456789abcdef" for c in record["id"])
        or record.get("sale_currency") != "EUR"
        or record.get("sale_exchange_rate") != 1.0
    ):
        raise ValueError("Registo de venda sem proveniência suportada.")
    holding = record["holding"]
    sold = date.fromisoformat(record["sale_date"])
    proceeds = _number(record["sale_value_eur"])
    sale_fees = _number(record["sale_fees_eur"])
    problems: list[str] = []
    known_flag = row_get(holding, "cost_basis_known", 0)
    if known_flag not in (0, 1) or isinstance(known_flag, str):
        raise ValueError("Invalid cost provenance flag")
    cost_known = bool(known_flag)
    cost = _number(row_get(holding, "cost_basis", 0)) if cost_known else None
    if not cost_known:
        problems.append("Custo de aquisição desconhecido")
    acquisition = str(row_get(holding, "acquisition_date", "") or "")
    try:
        acquired = date.fromisoformat(acquisition)
        if acquired > sold:
            raise ValueError("Acquisition after disposal")
    except TypeError, ValueError:
        problems.append("Data de aquisição sem comprovativo válido")
    currency = str(row_get(holding, "currency", "EUR") or "EUR").upper()
    rate = record.get("acquisition_exchange_rate")
    if currency == "EUR":
        rate = 1.0
    elif rate is None:
        problems.append("Câmbio histórico de aquisição desconhecido")
    if rate is not None:
        rate = _number(rate)
        if rate <= 0:
            raise ValueError("Invalid historical exchange rate")
    acquisition_fees = _number(row_get(holding, "fees", 0))
    cost_eur = cost * rate if cost is not None and rate is not None else None
    acquisition_fees_eur = acquisition_fees * rate if rate is not None else None
    gain = (
        proceeds - sale_fees - cost_eur - acquisition_fees_eur
        if cost_eur is not None and acquisition_fees_eur is not None
        else None
    )
    return {
        "id": record["id"],
        "holding_id": row_get(holding, "id"),
        "name": str(row_get(holding, "name", "Ativo")),
        "account": str(row_get(holding, "account", "")),
        "acquisition_date": acquisition,
        "sale_date": sold.isoformat(),
        "currency": currency,
        "acquisition_exchange_rate": rate,
        "cost_basis_eur": cost_eur,
        "acquisition_fees_eur": acquisition_fees_eur,
        "sale_value_eur": proceeds,
        "sale_fees_eur": sale_fees,
        "net_gain_eur": gain,
        "complete": not problems,
        "issues": problems,
        "provenance": str(record.get("provenance") or "Confirmação manual"),
    }


def build_realized_report(
    records: Iterable[dict[str, Any]], fiscal_year: int
) -> dict[str, Any]:
    """Build one canonical report for the table, clipboard, CSV and PDF."""
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    identities: set[str] = set()
    for record in records:
        try:
            row = _evaluate_record(record)
            if row["id"] in identities:
                raise ValueError("Identificador de venda repetido")
            identities.add(row["id"])
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            problems.append(f"Registo fiscal inválido: {exc}")
            continue
        if date.fromisoformat(row["sale_date"]).year == int(fiscal_year):
            rows.append(row)
    rows.sort(key=lambda row: (row["sale_date"], row["acquisition_date"], row["id"]))
    return {
        "fiscal_year": int(fiscal_year),
        "rows": rows,
        "issues": problems,
        "complete": not problems and all(row["complete"] for row in rows),
        "export_allowed": bool(rows)
        and not problems
        and all(row["complete"] for row in rows),
        "net_gain_eur": sum(row["net_gain_eur"] for row in rows)
        if rows and all(row["complete"] for row in rows)
        else None,
        "limitation": REPORT_LIMITATION,
    }


REPORT_COLUMNS = (
    "Registo",
    "Ativo",
    "Conta",
    "Aquisição",
    "Venda",
    "Moeda de aquisição",
    "Câmbio histórico EUR",
    "Custo EUR",
    "Encargos aquisição EUR",
    "Realização EUR",
    "Encargos venda EUR",
    "Ganho líquido EUR",
    "Proveniência",
)


def realized_report_rows(
    report: dict[str, Any], *, privacy: bool = False
) -> list[list[Any]]:
    if not report.get("export_allowed"):
        raise ValueError(
            "O mapa não tem vendas completas para exportar. Confirma os dados em falta."
        )
    if privacy:
        raise ValueError(
            "Desativa o modo de privacidade para exportar o mapa de apoio fiscal."
        )
    return [
        [
            row[key]
            for key in (
                "id",
                "name",
                "account",
                "acquisition_date",
                "sale_date",
                "currency",
                "acquisition_exchange_rate",
                "cost_basis_eur",
                "acquisition_fees_eur",
                "sale_value_eur",
                "sale_fees_eur",
                "net_gain_eur",
                "provenance",
            )
        ]
        for row in report["rows"]
    ]


def realized_report_csv(report: dict[str, Any], *, privacy: bool = False) -> str:
    rows = realized_report_rows(report, privacy=privacy)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";")
    writer.writerow([report["limitation"]])
    writer.writerow(REPORT_COLUMNS)
    writer.writerows(spreadsheet_safe_row(row) for row in rows)
    return stream.getvalue()


def realized_report_html(report: dict[str, Any], *, privacy: bool = False) -> str:
    rows = realized_report_rows(report, privacy=privacy)

    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    return (
        "<!doctype html><html lang='pt'><meta charset='utf-8'><body>"
        f"<h2>Mapa de apoio de vendas — {report['fiscal_year']}</h2>"
        f"<p>{esc(report['limitation'])}</p><table border='1'><tr>"
        + "".join(f"<th>{esc(value)}</th>" for value in REPORT_COLUMNS)
        + "</tr>"
        + "".join(
            "<tr>" + "".join(f"<td>{esc(value)}</td>" for value in row) + "</tr>"
            for row in rows
        )
        + "</table></body></html>"
    )


def load_disposal_records(repo: Any) -> list[dict[str, Any]]:
    """Read versioned records from existing backed-up settings; never parse notes."""
    values = repo.q(
        "SELECT key,value FROM settings WHERE key LIKE ? ORDER BY key",
        (DISPOSAL_PREFIX + "%",),
    )
    records = []
    for value in values:
        try:
            record = json.loads(value["value"])
            if not isinstance(record, dict) or value["key"] != DISPOSAL_PREFIX + str(
                record.get("id")
            ):
                raise ValueError("Invalid event key")
        except KeyError, TypeError, ValueError:
            record = {}  # Invalid evidence blocks export; it is never silently dropped.
        records.append(record)
    return records
