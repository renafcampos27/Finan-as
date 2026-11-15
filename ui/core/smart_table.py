"""Typed sorting primitives shared by every user-facing table."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

from PySide6.QtCore import QDate, QDateTime, Qt
from PySide6.QtWidgets import QTableWidgetItem


SORT_VALUE_ROLE = int(Qt.ItemDataRole.UserRole) + 431

_EMPTY_VALUES = {"", "-", "—", "n/d", "n.a.", "não disponível", "sem dados"}
_MONTHS = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}
_STATUS_RANKS = (
    (
        0,
        (
            "critico",
            "critica",
            "erro",
            "falhou",
            "bloqueado",
            "vencido",
            "atrasado",
            "ruptura",
            "ultrapassado",
            "excedido",
            "insuficiente",
            "sem cobertura",
        ),
    ),
    (
        1,
        (
            "risco",
            "em risco",
            "alerta",
            "atencao",
            "rever",
            "provavel",
            "possivel",
        ),
    ),
    (2, ("pendente", "agendado", "em curso", "pausado", "parcial")),
    (
        3,
        (
            "ok",
            "em dia",
            "ativo",
            "ativa",
            "pago",
            "paga",
            "concluido",
            "concluida",
            "saudavel",
            "forte",
            "certo",
            "certa",
            "controlado",
            "atingido",
            "atingida",
            "dentro do orcamento",
            "disponivel",
        ),
    ),
    (4, ("inativo", "inativa", "cancelado", "cancelada", "arquivado", "arquivada")),
)


def _fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).casefold()


def _date_key(text: str) -> tuple[int, int, int, int, int, int] | None:
    candidate = text.strip()
    if re.fullmatch(
        r"\d{4}-\d{2}(?:-\d{2})?(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", candidate
    ):
        try:
            if len(candidate) == 7:
                candidate += "-01"
            value = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return (
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
            )
        except ValueError:
            return None
    european = re.fullmatch(
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:\s+(\d{1,2}):(\d{2}))?",
        candidate,
    )
    if european:
        day, month, year, hour, minute = european.groups()
        try:
            value = datetime(
                int(year), int(month), int(day), int(hour or 0), int(minute or 0)
            )
            return (value.year, value.month, value.day, value.hour, value.minute, 0)
        except ValueError:
            return None
    month_label = re.fullmatch(r"([A-Za-zÀ-ÿ]+)\s+(\d{4})", candidate)
    if month_label:
        month = _MONTHS.get(_fold(month_label.group(1)))
        if month:
            return (int(month_label.group(2)), month, 1, 0, 0, 0)
    return None


def _number_key(text: str) -> float | None:
    candidate = text.strip().replace("\u00a0", " ").replace("−", "-").replace("–", "-")
    candidate = re.sub(r"^[↑↓↗↘▲▼]\s*", "", candidate)
    match = re.fullmatch(
        r"([+-]?\s*\d[\d .]*(?:,\d+)?)\s*"
        r"(€|%|p\.?\s*p\.?|h|horas?|dias?|meses?|anos?)?",
        candidate,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    number, unit = match.groups()
    number = number.replace(" ", "")
    if "," in number:
        number = number.replace(".", "").replace(",", ".")
    try:
        value = float(number)
    except ValueError:
        return None
    unit_key = _fold(unit or "")
    if unit_key.startswith("ano"):
        value *= 12.0
    return value


def smart_sort_key(value: Any) -> tuple[Any, ...]:
    """Return a stable key for dates, amounts, percentages, states and text."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (1, float(value))
    if isinstance(value, QDate):
        return (0, value.year(), value.month(), value.day(), 0, 0, 0)
    if isinstance(value, QDateTime):
        date_value = value.date()
        time_value = value.time()
        return (
            0,
            date_value.year(),
            date_value.month(),
            date_value.day(),
            time_value.hour(),
            time_value.minute(),
            time_value.second(),
        )

    text = str(value or "").strip()
    folded = _fold(text)
    if folded in _EMPTY_VALUES or set(folded) <= {"•", "*"}:
        return (4, "")
    parsed_date = _date_key(text)
    if parsed_date is not None:
        return (0, *parsed_date)
    parsed_number = _number_key(text)
    if parsed_number is not None:
        return (1, parsed_number)
    status_text = re.sub(r"^[^\w]+", "", folded, flags=re.UNICODE)
    for rank, labels in _STATUS_RANKS:
        if any(
            status_text == label or status_text.startswith(label + " ")
            for label in labels
        ):
            return (2, rank, status_text)
    return (3, folded)


class SmartTableWidgetItem(QTableWidgetItem):
    """QTableWidgetItem that compares the semantic value shown to the user."""

    def clone(self) -> "SmartTableWidgetItem":  # type: ignore[override]
        return SmartTableWidgetItem(self)

    def _sorting_value(self) -> Any:
        explicit = self.data(SORT_VALUE_ROLE)
        return self.text() if explicit is None else explicit

    def __lt__(self, other: QTableWidgetItem) -> bool:
        own_key = smart_sort_key(self._sorting_value())
        other_value = (
            other._sorting_value()
            if isinstance(other, SmartTableWidgetItem)
            else other.text()
        )
        return own_key < smart_sort_key(other_value)

    def set_sort_value(self, value: Any) -> None:
        self.setData(SORT_VALUE_ROLE, value)


__all__ = ["SORT_VALUE_ROLE", "SmartTableWidgetItem", "smart_sort_key"]
