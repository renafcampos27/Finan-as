"""Canonical category names shared by imports and financial analytics."""

from __future__ import annotations

import re
import unicodedata

_INVISIBLE_CHARS = re.compile(r"[\u200B-\u200D\u2060\uFEFF]")


def category_identity_key(value: object) -> str:
    """Return a stable comparison key while keeping the display name separate."""

    text = unicodedata.normalize("NFKD", str(value or "").strip())
    text = _INVISIBLE_CHARS.sub("", text)
    text = "".join(
        character for character in text if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", text).strip().casefold()


_ALIASES = {
    "alimentacao": "Supermercado",
    "alimentacao e supermercado": "Supermercado",
    "mercearia": "Supermercado",
    "supermercados": "Supermercado",
    "salario": "Rendimentos",
    "salarios": "Rendimentos",
    "ordenado": "Rendimentos",
    "vencimento": "Rendimentos",
    "receita": "Rendimentos",
    "receitas": "Rendimentos",
    "outro": "Diversos",
    "outros": "Diversos",
    "outras": "Diversos",
    "misc": "Diversos",
    "miscellaneous": "Diversos",
    "transportes": "Transporte",
    "subscricao": "Subscrições",
    "subscricoes": "Subscrições",
}


def canonical_category(value: object, *, fallback: str = "Diversos") -> str:
    """Return one stable application category without altering custom names."""

    original = str(value or "").strip()
    if not original:
        return str(fallback or "Diversos")
    return _ALIASES.get(category_identity_key(original), original)


__all__ = ["canonical_category", "category_identity_key"]
