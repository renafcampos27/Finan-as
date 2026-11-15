"""Safe presentation helpers for exported user-controlled data."""

from __future__ import annotations

import ctypes
import html
import re
from typing import Any, Iterable


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "|", "%")
_HEX_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
_LEADING_STRIP_REGEX = re.compile(
    r"^[\s\x00-\x1f\x7f-\x9f\ufeff\u00a0\u2000-\u200f\u2028\u2029\u202f\u2060\u3000]+"
)
_NUMERIC_LITERAL_REGEX = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
# BUG-AUD-C81 / BUG-DYN-01: Only allow plain text notes that do not contain formula/DDE execution operators (=, @, |, %) or function invocation tokens ((), quotes, brackets, colons)
_SAFE_HYPHEN_TEXT_REGEX = re.compile(
    r"^[+-]\s+[^\d=@+\-|%()\"':;\[\]][^=@|%()\"':;\[\]]*$"
)
_PRIVATE_EXPORT_FIELDS = {
    "account",
    "conta",
    "description",
    "descrição",
    "descricao",
    "merchant",
    "comerciante",
    "notes",
    "notas",
    "tags",
    "etiquetas",
    "document",
    "documento",
    "file",
    "ficheiro",
    "attachments",
    "anexos",
    "beneficiary",
    "beneficiário",
    "beneficiario",
    "entity",
    "entidade",
    "iban",
    "nif",
    "card",
    "cartão",
    "cartao",
    "counterparty",
    "contraparte",
}
_CATEGORY_EXPORT_FIELDS = {
    "category",
    "categoria",
    "subcategory",
    "subcategoria",
    "group",
    "grupo",
}
_PERCENT_EXPORT_FIELDS = {
    "percent",
    "percentagem",
    "percentage",
    "taxa",
    "ratio",
    "utilisation",
    "utilização",
}
_MONEY_EXPORT_FIELDS = {
    "amount",
    "valor",
    "total",
    "budget",
    "orçamento",
    "spent",
    "gasto",
    "remaining",
    "disponível",
    "saldo",
    "forecast",
    "previsão",
}


def secure_zero(target: Any) -> None:
    """Explicitly wipe memory buffers containing secret key bytes or passwords."""
    if isinstance(target, (bytearray, memoryview)):
        if len(target) > 0:
            target_addr = (ctypes.c_char * len(target)).from_buffer(target)
            ctypes.memset(ctypes.byref(target_addr), 0, len(target))
    elif isinstance(target, ctypes.Array):
        ctypes.memset(ctypes.byref(target), 0, ctypes.sizeof(target))


def spreadsheet_safe_cell(value: Any) -> Any:
    """Neutralise spreadsheet formulas without changing the stored value.

    Numeric Python values remain numeric. Only exported text that a spreadsheet
    could execute is prefixed with an apostrophe, the conventional literal-text
    marker understood by Excel and compatible applications.
    """

    if not isinstance(value, str):
        return value
    text = value.replace("\x00", "")
    candidate = _LEADING_STRIP_REGEX.sub("", text)
    if not candidate:
        return text
    if _NUMERIC_LITERAL_REGEX.fullmatch(candidate):
        return text
    # SEC-EXP-02: Allow legitimate bullet points or text notes starting with '-' or '+'
    if (
        (candidate.startswith(("-", "+")) and _SAFE_HYPHEN_TEXT_REGEX.match(candidate))
        or candidate.strip() in ("-", "+", "--", "---")
    ) and not text.startswith(("\t", "\r", "\n")):
        return text
    # SEC-EXP-01: Check both raw text prefix (control chars like \t, \r, \n) and stripped candidate prefix
    if (
        text.startswith(_FORMULA_PREFIXES)
        or candidate.startswith(_FORMULA_PREFIXES)
        or candidate.startswith("'=")
    ):
        return "'" + text
    return text


def spreadsheet_safe_row(values: Iterable[Any]) -> list[Any]:
    """Return one CSV row with every textual cell made non-executable."""

    return [spreadsheet_safe_cell(value) for value in values]


def privacy_export_cell(
    value: Any,
    field: str,
    privacy: bool,
    *,
    mask_categories: bool = False,
    mask_percentages: bool = True,
) -> Any:
    """Mask sensitive CSV fields when the application's privacy mode is active."""

    if not privacy:
        return spreadsheet_safe_cell(value)
    normalised = str(field or "").strip().casefold()
    if normalised in _MONEY_EXPORT_FIELDS:
        return "••••• €"
    if normalised in _PRIVATE_EXPORT_FIELDS:
        return "Dados ocultos"
    if mask_percentages and normalised in _PERCENT_EXPORT_FIELDS:
        return "••• %"
    if mask_categories and normalised in _CATEGORY_EXPORT_FIELDS:
        return "Dados ocultos"
    return spreadsheet_safe_cell(value)


def privacy_export_row(
    values: Iterable[Any],
    fields: Iterable[str],
    privacy: bool,
    *,
    mask_categories: bool = False,
    mask_percentages: bool = True,
) -> list[Any]:
    """Prepare a row whose masking policy follows the visible privacy mode."""

    return [
        privacy_export_cell(
            value,
            field,
            privacy,
            mask_categories=mask_categories,
            mask_percentages=mask_percentages,
        )
        for value, field in zip(values, fields)
    ]


def export_label(label: str, privacy: bool) -> str:
    """Make the safer privacy-mode default explicit before a file is created."""

    value = str(label or "Exportar")
    return (
        f"{value} mascarado"
        if privacy and "mascarado" not in value.casefold()
        else value
    )


def html_text(value: Any) -> str:
    """Escape a dynamic value before inserting it in rich text or HTML."""

    return html.escape(str(value if value is not None else ""), quote=True)


def safe_hex_colour(value: Any, fallback: str = "#94A3B8") -> str:
    """Allow only six-digit hexadecimal colours from persisted/user data."""

    candidate = str(value or "").strip()
    safe_fallback = str(fallback or "#94A3B8").strip()
    if not _HEX_COLOUR.fullmatch(safe_fallback):
        safe_fallback = "#94A3B8"
    return (
        candidate.upper() if _HEX_COLOUR.fullmatch(candidate) else safe_fallback.upper()
    )


__all__ = [
    "html_text",
    "export_label",
    "privacy_export_cell",
    "privacy_export_row",
    "safe_hex_colour",
    "spreadsheet_safe_cell",
    "spreadsheet_safe_row",
]
