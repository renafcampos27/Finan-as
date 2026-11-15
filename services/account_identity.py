"""Stable identity rules for user-facing financial account names."""

from __future__ import annotations

import re
import unicodedata

DEFAULT_ACCOUNT_NAME = "Conta principal"

_INVISIBLE_CHARS = re.compile(r"[\u200B-\u200D\u2060\uFEFF]")
_IBAN_COMPACT_RE = re.compile(r"^[A-Za-z]{2}\d{2}[A-Za-z0-9]{11,30}$")


def _is_iban(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    return bool(_IBAN_COMPACT_RE.match(compact))


def clean_account_name(value: object, *, default: str = DEFAULT_ACCOUNT_NAME) -> str:
    """Return a readable, whitespace-normalised account name.

    The display casing chosen by the user is preserved.  The built-in main
    account is the exception: every casing/spacing variant uses one stable
    Portuguese label.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _INVISIBLE_CHARS.sub("", text)
    text = " ".join(text.split()).strip()
    if not text:
        text = str(default or DEFAULT_ACCOUNT_NAME).strip() or DEFAULT_ACCOUNT_NAME
    if _is_iban(text):
        text = "".join(text.split()).upper()
    if account_identity_key(text) == account_identity_key(DEFAULT_ACCOUNT_NAME):
        return DEFAULT_ACCOUNT_NAME
    return text


def account_identity_key(value: object) -> str:
    """Return the comparison/storage key shared by every account workflow."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _INVISIBLE_CHARS.sub("", text)
    text = " ".join(text.split()).strip()
    if _is_iban(text):
        text = "".join(text.split())
    return text.casefold()


__all__ = ["DEFAULT_ACCOUNT_NAME", "account_identity_key", "clean_account_name"]
