"""CSV reading, normalization, preview and batch-import orchestration."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from utils.app_logging import log_exception
from utils.helpers import require_eur_cent_precision
from utils.import_dates import detect_import_date_format, parse_import_date
from services.bank_parsers import OFXParser, QIFParser
from services.category_taxonomy import canonical_category
from services.account_identity import account_identity_key, clean_account_name

if TYPE_CHECKING:
    from database.repo import Repo
    from services.change_journal import ChangeJournalService


MAPPING_FIELDS = (
    "date",
    "desc",
    "amount",
    "amount_debit",
    "amount_credit",
    "category",
    "account",
    "method",
    "merchant",
    "reference",
)


AMOUNT_LOCALE_AUTO = "auto"
AMOUNT_LOCALE_PT = "pt"
AMOUNT_LOCALE_INTERNATIONAL = "international"
AMOUNT_LOCALES = {
    AMOUNT_LOCALE_AUTO,
    AMOUNT_LOCALE_PT,
    AMOUNT_LOCALE_INTERNATIONAL,
}
MAX_CSV_BYTES = 64 * 1024 * 1024
MAX_CSV_ROWS = 500_000
MAX_CSV_COLUMNS = 256

_DEFAULT_CATEGORY_TERMS = [
    ('mercadona', 'Supermercado'),
    ('continente', 'Supermercado'),
    ('pingo doce', 'Supermercado'),
    ('pingo', 'Supermercado'),
    ('auchan', 'Supermercado'),
    ('intermarche', 'Supermercado'),
    ('intermarché', 'Supermercado'),
    ('lidl', 'Supermercado'),
    ('aldi', 'Supermercado'),
    ('minipreço', 'Supermercado'),
    ('minipreco', 'Supermercado'),
    ('uber eats', 'Restauração'),
    ('glovo', 'Restauração'),
    ('mcdonald', 'Restauração'),
    ('burger king', 'Restauração'),
    ('restaurante', 'Restauração'),
    ('uber', 'Transporte'),
    ('bolt', 'Transporte'),
    ('galp', 'Transporte'),
    ('cepsa', 'Transporte'),
    ('repsol', 'Transporte'),
    ('bp', 'Transporte'),
    ('cp', 'Transporte'),
    ('metro', 'Transporte'),
    ('carris', 'Transporte'),
    ('via verde', 'Transporte'),
    ('brisa', 'Transporte'),
    ('netflix', 'Subscrições'),
    ('spotify', 'Subscrições'),
    ('apple.com', 'Subscrições'),
    ('google', 'Subscrições'),
    ('prime video', 'Subscrições'),
    ('disney', 'Subscrições'),
    ('vodafone', 'Telecomunicações'),
    ('meo', 'Telecomunicações'),
    ('nos comunic', 'Telecomunicações'),
    ('nowo', 'Telecomunicações'),
    ('edp', 'Habitação'),
    ('endesa', 'Habitação'),
    ('iberdrola', 'Habitação'),
    ('goldenergy', 'Habitação'),
    ('epal', 'Habitação'),
    ('água', 'Habitação'),
    ('farmácia', 'Saúde'),
    ('farmacia', 'Saúde'),
    ('hospital', 'Saúde'),
    ('clínica', 'Saúde'),
    ('clinica', 'Saúde'),
    ('salário', 'Rendimentos'),
    ('vencimento', 'Rendimentos'),
    ('ordenado', 'Rendimentos'),
    ('transferência recebida', 'Rendimentos'),
    ('reembolso', 'Reembolsos'),
]

# BUG-AUD-C108: numeric date column formats disambiguated globally.
DATE_FORMAT_DD_MM = "%d/%m/%Y"
DATE_FORMAT_MM_DD = "%m/%d/%Y"


def detect_date_format_globally(date_strings: Any, column: str | None = None) -> str:
    """Infer a format only when all numeric cells support one ordering."""
    if column is not None and isinstance(date_strings, (list, tuple)):
        values = [str(row.get(column, "")) for row in date_strings if isinstance(row, dict)]
    elif isinstance(date_strings, (list, tuple, set)):
        values = date_strings
    else:
        values = []
    return detect_import_date_format(values)


@dataclass(slots=True, frozen=True)
class CsvImportData:
    headers: list[str]
    rows: list[dict[str, str]]
    encoding: str = "utf-8-sig"
    warnings: tuple[str, ...] = ()
    is_credit_card: bool = False
    invert_signs: bool = False
    source_sha256: str = ""
    # Empty means no column-wide verdict: ambiguous cells need an explicit choice.
    date_format: str = ""


@dataclass(slots=True, frozen=True)
class NormalizedImportRow:
    date: str
    description: str
    amount: float
    category: str = "Outros"
    account: str = "Conta principal"
    payment_method: str = ""
    merchant: str = ""
    external_reference: str = ""
    validation_issues: tuple[str, ...] = ()
    source_row: int = 0
    source_values: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.date, str):
            d_str = self.date.isoformat() if hasattr(self.date, "isoformat") else str(self.date or "")
            object.__setattr__(self, "date", d_str)

    @property
    def signature(self) -> tuple[Any, ...]:
        account = account_identity_key(self.account)
        reference = self.external_reference.strip().casefold()
        if reference:
            return ("reference", account, reference, self.date[:10], round(float(self.amount), 2), self.description.strip().casefold())
        return (
            "movement",
            self.date[:10],
            self.description.strip().casefold(),
            round(float(self.amount), 2),
            account,
        )

    @property
    def is_valid(self) -> bool:
        return not self.validation_issues

    @property
    def validation_message(self) -> str:
        return " · ".join(self.validation_issues)


@dataclass(slots=True, frozen=True)
class ImportPreviewRow:
    transaction: NormalizedImportRow
    status: str
    duplicate_level: str = "none"
    duplicate_reason: str = ""

    @property
    def reason(self) -> str:
        if self.transaction.validation_message:
            return self.transaction.validation_message
        if self.duplicate_reason:
            return self.duplicate_reason
        return "Pronto para importar."


@dataclass(slots=True, frozen=True)
class ImportPreview:
    rows: list[ImportPreviewRow]
    total: int
    duplicates: int
    invalid: int
    certain_duplicates: int = 0
    probable_duplicates: int = 0
    possible_duplicates: int = 0
    ambiguous_dates: int = 0


@dataclass(slots=True, frozen=True)
class ImportReviewRow:
    original: NormalizedImportRow
    proposed: NormalizedImportRow
    reason: str = "Categoria original do ficheiro ou categoria de base."
    source: str = "original"
    rule_keyword: str = ""
    decision: str = "keep"

    @property
    def has_suggestion(self) -> bool:
        return self.original.category != self.proposed.category

    @property
    def effective(self) -> NormalizedImportRow:
        return self.proposed if self.decision == "accept" else self.original


@dataclass(slots=True, frozen=True)
class ImportReviewBatch:
    rows: tuple[ImportReviewRow, ...]
    source_rows_json: str
    mapping_json: str
    options_json: str
    rules_fingerprint: str
    catalogue_fingerprint: str
    snapshot_fingerprint: str

    @property
    def pending_count(self) -> int:
        return sum(row.decision == "pending" for row in self.rows)

    @property
    def excluded_count(self) -> int:
        return sum(row.decision == "exclude" for row in self.rows)

    def choose(self, source_row: int, decision: str) -> ImportReviewBatch:
        if decision not in {"keep", "pending", "accept", "reject", "exclude"}:
            raise ValueError("Decisão de revisão inválida.")
        if isinstance(source_row, bool) or not isinstance(source_row, int):
            raise ValueError("Linha de origem inválida.")
        selected = next((row for row in self.rows if row.original.source_row == source_row), None)
        if selected is None:
            raise ValueError("A linha não pertence ao lote revisto.")
        if decision in {"accept", "pending"} and not selected.has_suggestion:
            raise ValueError("A linha não tem uma sugestão de categoria.")
        return replace(self, rows=tuple(
            replace(row, decision=decision) if row is selected else row for row in self.rows
        ))

    def choose_all(self, decision: str) -> ImportReviewBatch:
        if decision not in {"accept", "reject"}:
            raise ValueError("Escolhe aceitar ou rejeitar as sugestões.")
        return replace(self, rows=tuple(
            replace(row, decision=decision)
            if row.has_suggestion and row.decision != "exclude" else row
            for row in self.rows
        ))


@dataclass(slots=True, frozen=True)
class ImportResult:
    created_ids: list[int]
    skipped_invalid: int
    skipped_duplicates: int

    @property
    def created(self) -> int:
        return len(self.created_ids)

    @property
    def skipped(self) -> int:
        return self.skipped_invalid + self.skipped_duplicates


class ImportService:
    """Prepare CSV rows and persist accepted movements as one atomic batch."""

    detect_date_format_globally = staticmethod(detect_date_format_globally)

    def __init__(
        self,
        repo: "Repo | None" = None,
        change_journal: "ChangeJournalService | None" = None,
    ) -> None:
        self.repo = repo
        self.change_journal = change_journal

    @classmethod
    def read_file(cls, path: Path) -> CsvImportData:
        """Read CSV, OFX, QFX or QIF statement and return normalized tabular preview data."""
        path = Path(path)
        raw = path.read_bytes()
        if len(raw) > MAX_CSV_BYTES:
            raise ValueError("O ficheiro excede o limite seguro de 64 MB.")
        if b"\x00" in raw:
            raise ValueError(
                "O ficheiro contém bytes nulos e não é um ficheiro de texto válido."
            )

        text = ""
        encoding = ""
        for candidate in ("utf-8-sig", "cp1252", "iso-8859-1"):
            try:
                text = raw.decode(candidate, errors="strict")
                encoding = candidate
                break
            except UnicodeDecodeError:
                continue
        if not text:
            text = raw.decode("iso-8859-1", errors="replace")
            encoding = "iso-8859-1"

        suffix = path.suffix.casefold()
        upper_text = text.upper()
        if (
            suffix in {".ofx", ".qfx"}
            or "<OFX>" in upper_text
            or "OFXHEADER" in upper_text
        ):
            parsed = OFXParser.parse(text)
            return CsvImportData(
                headers=parsed.headers,
                rows=parsed.rows,
                encoding=encoding,
                warnings=parsed.warnings,
                is_credit_card=parsed.is_credit_card,
                source_sha256=hashlib.sha256(raw).hexdigest(),
            )
        if suffix == ".qif" or upper_text.startswith("!TYPE:"):
            parsed = QIFParser.parse(text)
            return CsvImportData(
                headers=parsed.headers,
                rows=parsed.rows,
                encoding=encoding,
                warnings=parsed.warnings,
                date_format=detect_date_format_globally(parsed.rows, "Data"),
                source_sha256=hashlib.sha256(raw).hexdigest(),
            )

        return cls.read_csv(path)

    @staticmethod
    def read_csv(path: Path) -> CsvImportData:
        path = Path(path)
        raw = path.read_bytes()
        if len(raw) > MAX_CSV_BYTES:
            raise ValueError("O CSV excede o limite seguro de 64 MB.")
        if b"\x00" in raw:
            raise ValueError(
                "O CSV contém bytes nulos e não é um ficheiro de texto válido."
            )
        text = ""
        encoding = ""
        for candidate in ("utf-8-sig", "cp1252", "iso-8859-1"):
            try:
                text = raw.decode(candidate, errors="strict")
                encoding = candidate
                break
            except UnicodeDecodeError:
                continue
        if not encoding:  # pragma: no cover - latin-1 accepts every byte sequence.
            raise ValueError("Não foi possível determinar a codificação do ficheiro.")
        warnings: list[str] = []
        if encoding != "utf-8-sig":
            label = "Windows-1252" if encoding == "cp1252" else "ISO-8859-1"
            warnings.append(
                f"O ficheiro foi lido como {label}; confirma os acentos na pré-visualização."
            )
        fallback_delimiter = ""
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t,")
        except csv.Error:
            dialect = None
            fallback_delimiter = ";" if text.count(";") > text.count(",") else ","
            warnings.append(
                "O separador não foi identificado com segurança; confirma o mapeamento."
            )
        stream = io.StringIO(text, newline="")
        reader = (
            csv.reader(stream, dialect=dialect, strict=True)
            if dialect is not None
            else csv.reader(stream, delimiter=fallback_delimiter, strict=True)
        )

        all_raw_lines: list[list[str]] = []
        try:
            for row_vals in reader:
                if any(str(v or "").strip() for v in row_vals):
                    all_raw_lines.append([str(v or "").strip() for v in row_vals])
        except csv.Error as exc:
            raise ValueError(
                f"CSV malformado junto à linha {reader.line_num}."
            ) from exc

        if not all_raw_lines:
            raise ValueError("O CSV está vazio ou não contém cabeçalhos.")

        # Bank header scanner: detect table header row even with top metadata
        bank_keywords = (
            "data",
            "date",
            "descr",
            "mov",
            "débito",
            "debito",
            "crédito",
            "credito",
            "valor",
            "amount",
            "montante",
            "saldo",
            "balance",
        )
        header_idx = 0
        best_score = 0
        for idx, line in enumerate(all_raw_lines[:15]):
            score = sum(
                1 for cell in line if any(kw in cell.casefold() for kw in bank_keywords)
            )
            if score >= 2 and len(line) >= 2 and score > best_score:
                best_score = score
                header_idx = idx

        if header_idx > 0:
            warnings.append(
                f"Extrato bancário: detetadas e ignoradas {header_idx} linhas de metadados iniciais."
            )

        header_values = all_raw_lines[header_idx]
        headers = list(header_values)
        if len(headers) > MAX_CSV_COLUMNS:
            raise ValueError(
                f"O CSV tem {len(headers)} colunas; o limite seguro é {MAX_CSV_COLUMNS}."
            )
        empty_headers = [
            index + 1 for index, header in enumerate(headers) if not header
        ]
        if empty_headers:
            positions = ", ".join(str(value) for value in empty_headers[:8])
            raise ValueError(f"Existem cabeçalhos vazios nas colunas: {positions}.")
        normalised_headers: dict[str, list[str]] = {}
        for header in headers:
            normalised_headers.setdefault(header.casefold(), []).append(header)
        duplicates = [
            values[0] for values in normalised_headers.values() if len(values) > 1
        ]
        if duplicates:
            raise ValueError(
                "Existem cabeçalhos CSV repetidos: " + ", ".join(duplicates[:8]) + "."
            )

        rows: list[dict[str, str]] = []
        # BUG-AUD-C109: bank statements (CGD, Millennium BCP, ...) terminate
        # with legal footnotes and consolidated balance lines whose column
        # count differs from the movements table. Those lines are footer
        # noise, not malformed rows: skip them instead of rejecting the
        # whole file. Only strictly-wider data rows are still an error.
        expected_cols = len(headers)
        skipped_footer_lines = 0
        mapping_hint = ImportService.suggested_mapping(headers)
        for line_num, values in enumerate(
            all_raw_lines[header_idx + 1 :], start=header_idx + 2
        ):
            if len(values) != expected_cols:
                if len(values) < expected_cols:
                    skipped_footer_lines += 1
                    continue
                relation = "a mais"
                raise ValueError(
                    f"A linha {line_num} tem colunas {relation}: "
                    f"esperadas {expected_cols}, recebidas {len(values)}."
                )
            rows.append(
                {
                    header: str(value or "")
                    for header, value in zip(headers, values, strict=True)
                }
            )
            if len(rows) > MAX_CSV_ROWS:
                raise ValueError(
                    f"O CSV excede o limite seguro de {MAX_CSV_ROWS} linhas."
                )

        if skipped_footer_lines:
            warnings.append(
                f"Extrato bancário: ignoradas {skipped_footer_lines} linhas de rodapé "
                "com colunas incompletas (avisos legais/saldos consolidados)."
            )

        # BUG-AUD-C108: disambiguate the date column format globally before
        # normalizing rows, so mixed DD/MM vs MM/DD interpretations cannot
        # silently swap month and day across the statement.
        detected_date_format = ""
        date_column = str(mapping_hint.get("date", "") or "")
        if date_column and date_column in headers:
            date_column_index = headers.index(date_column)
            column_dates = [
                values[date_column_index]
                for values in all_raw_lines[header_idx + 1 :]
                if len(values) == expected_cols
                and str(values[date_column_index] or "").strip()
            ]
            detected_date_format = detect_date_format_globally(column_dates)
            if detected_date_format == DATE_FORMAT_MM_DD:
                warnings.append(
                    "Formato de datas detetado como MM/DD/AAAA (internacional); "
                    "as datas foram interpretadas mês-antes-dia."
                )

        is_cc = ImportService.is_likely_credit_card(
            headers,
            " ".join(" ".join(line) for line in all_raw_lines[: header_idx + 1]),
        )
        if is_cc:
            warnings.append(
                "Conta de cartão de crédito detetada. Confirma os débitos/créditos na pré-visualização; inverter sinais é uma decisão separada."
            )

        return CsvImportData(
            headers=headers,
            rows=rows,
            encoding=encoding,
            warnings=tuple(warnings),
            is_credit_card=is_cc,
            date_format=detected_date_format,
            source_sha256=hashlib.sha256(raw).hexdigest(),
        )

    @staticmethod
    def is_likely_credit_card(headers: list[str], sample_text: str = "") -> bool:
        """Identify credit card statements by header/metadata signatures."""
        combined = " ".join(headers).casefold() + " " + sample_text.casefold()
        if (
            ("cartão de débito" in combined or "cartao de debito" in combined)
            and not any(
                w in combined
                for w in (
                    "crédito",
                    "credito",
                    "wizink",
                    "unibanco",
                    "fatura",
                )
            )
        ):
            return False

        keywords = (
            "cartão de crédito",
            "cartao de credito",
            "fatura de cartão",
            "fatura de cartao",
            "fatura cartão",
            "fatura cartao",
            "extrato de cartão de crédito",
            "extrato de cartao de credito",
            "extrato de conta cartão",
            "extrato de conta cartao",
            "conta cartão",
            "conta cartao",
            "cartão wizink",
            "cartao wizink",
            "wizink",
            "unibanco",
        )
        return any(kw in combined for kw in keywords)

    @staticmethod
    def detect_bank_dialect(headers: list[str]) -> str:
        """Identify Portuguese and international banking CSV/statement dialects."""
        clean_headers = [str(h or "").strip().casefold() for h in headers]
        dialect_signatures: dict[str, tuple[str, ...]] = {
            "Caixa Geral de Depósitos (CGD)": (
                "data movimento",
                "saldo contabilístico",
                "saldo contabilistico",
            ),
            "Millennium BCP": ("data mov.", "data mov", "importância", "importancia"),
            "Santander Totta": (
                "data da operação",
                "data da operacao",
                "data operação",
                "data operacao",
            ),
            "ActivoBank": ("saldo autorizado", "activobank"),
            "Revolut": ("started date", "completed date", "product", "fee"),
            "Moey!": ("moey", "moey!"),
            "Banco CTT": ("banco ctt", "débito/crédito", "debito/credito"),
            "Novo Banco": (
                "saldo após movimento",
                "saldo apos movimento",
                "novo banco",
            ),
            "Crédito Agrícola": (
                "crédito agrícola",
                "credito agricola",
                "ca online",
                "data valor",
            ),
            "Bankinter": ("bankinter", "conta à ordem", "conta a ordem"),
            "WiZink": (
                "wizink",
                "data transacção",
                "data transaccao",
                "cartão wizink",
                "cartao wizink",
            ),
            "Unibanco": (
                "unibanco",
                "data compra",
                "movimentos cartão",
                "movimentos cartao",
            ),
            "EuroBic / ABANCA": ("eurobic", "abanca", "saldo valor"),
            "Openbank": ("openbank", "data contable", "concepto"),
            "Trade Republic": (
                "trade republic",
                "isin",
                "shares",
                "transação de ações",
                "transacao de acoes",
            ),
        }
        best_bank = "Extrato Bancário Genérico"
        max_matches = 0
        for bank_name, signature_keywords in dialect_signatures.items():
            matched = sum(
                1 for kw in signature_keywords if any(kw in h for h in clean_headers)
            )
            if matched > max_matches:
                max_matches = matched
                best_bank = bank_name
        return best_bank if max_matches >= 1 else "Extrato Bancário Genérico"

    @staticmethod
    def suggested_mapping(headers: list[str]) -> dict[str, str]:
        aliases = {
            "date": (
                "data da operação",
                "data do movimento",
                "data movimento",
                "data mov.",
                "data mov",
                "started date",
                "completed date",
                "data valor",
                "data",
                "date",
            ),
            "desc": (
                "descrição da operação",
                "descritivo",
                "descrição",
                "descricao",
                "description",
                "detalhe",
                "movimento",
                "texto",
                "opera",
                "entidade",
            ),
            "amount_debit": (
                "valor / débito",
                "débito",
                "debito",
                "debit",
                "saída",
                "saida",
                "despesa",
            ),
            "amount_credit": (
                "valor / crédito",
                "crédito",
                "credito",
                "credit",
                "entrada",
                "receita",
                "rendimento",
            ),
            "amount": ("importância", "montante", "amount", "valor líquido", "valor"),
            "category": ("categoria", "category"),
            "account": ("conta", "iban", "account"),
            "method": ("forma", "método", "metodo", "pagamento", "type"),
            "merchant": (
                "comerc",
                "comerciante",
                "entidade",
                "merchant",
                "beneficiário",
                "destinatário",
            ),
            "reference": (
                "fitid",
                "referência bancária",
                "referência",
                "referencia",
                "reference",
                "transaction id",
                "trans id",
                "txid",
                "id movimento",
            ),
        }
        mapping = {field: "" for field in MAPPING_FIELDS}
        for field, names in aliases.items():
            for header in headers:
                h_clean = header.casefold()
                if field in ("desc", "amount") and "data" in h_clean:
                    continue
                if field == "amount" and any(
                    k in h_clean
                    for k in ("débito", "debito", "crédito", "credito", "saldo")
                ):
                    continue
                if any(name in h_clean for name in names):
                    mapping[field] = header
                    break
        if mapping.get("amount_debit") or mapping.get("amount_credit"):
            mapping["amount"] = ""
        return mapping

    def load_mapping(self) -> dict[str, str]:
        try:
            payload = json.loads(self.repo.setting("smart_import_mapping", "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {field: "" for field in MAPPING_FIELDS}
        if not isinstance(payload, dict):
            return {field: "" for field in MAPPING_FIELDS}
        return {field: str(payload.get(field, "") or "") for field in MAPPING_FIELDS}

    def save_mapping(self, mapping: dict[str, str]) -> None:
        payload = {field: str(mapping.get(field, "") or "") for field in MAPPING_FIELDS}
        try:
            self.repo.set_setting(
                "smart_import_mapping",
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception as exc:
            log_exception("Failed to save smart import mapping", exc)
            raise

    @staticmethod
    def parse_amount(
        value: Any,
        amount_locale: str = AMOUNT_LOCALE_AUTO,
        *,
        allow_zero: bool = False,
    ) -> float:
        amount_locale = str(amount_locale or AMOUNT_LOCALE_AUTO).strip().casefold()
        if amount_locale not in AMOUNT_LOCALES:
            raise ValueError(f"Formato numérico desconhecido: {amount_locale}")
        text = str(value or "").strip()
        # BUG-AUD-C110: Portuguese bank statements (Novo Banco, ActivoBank,
        # CGD) use dedicated debit/credit columns where empty movements are
        # marked "-", "--" or "N/A". Those are legitimate zero cells, not
        # malformed values: treating them as an error discarded entire valid
        # movements.
        if text.replace(" ", "") in ("", "-", "--", "---", "N/A", "NA", "null", "None"):
            return 0.0
        text = re.sub(
            r"(?i)\b(?:EUR|USD|GBP|CHF|CAD|AUD)\b|[€$£¥₹\u20a0-\u20cf]",
            "",
            text,
        )
        text = re.sub(r"[\s\u00a0\u2000-\u200b\u202f\ufeff]+", "", text)
        if text.endswith("-"):
            text = "-" + text[:-1]
        if "(" in text and ")" in text:
            text = "-" + text.replace("(", "").replace(")", "")
        if not text:
            raise ValueError("valor em falta")
        if not re.fullmatch(r"[+-]?[0-9][0-9.,]*", text):
            raise ValueError(f"valor inválido ({str(value).strip()})")

        if amount_locale == AMOUNT_LOCALE_PT:
            if text.count(",") > 1:
                raise ValueError(f"valor inválido ({str(value).strip()})")
            # BUG-R6-IMP-03: if there is no comma, but a dot is present with 1 or 2 trailing digits, e.g. "12.50"
            if "," not in text and text.count(".") == 1:
                parts = text.split(".")
                if 1 <= len(parts[1]) <= 2:
                    pass  # Keep dot as decimal separator
                else:
                    text = text.replace(".", "")
            else:
                text = text.replace(".", "").replace(",", ".")
        elif amount_locale == AMOUNT_LOCALE_INTERNATIONAL:
            if text.count(".") > 1:
                raise ValueError(f"valor inválido ({str(value).strip()})")
            # BUG-R6-IMP-03: if there is no dot, but a comma is present with 1 or 2 trailing digits, e.g. "12,50"
            if "." not in text and text.count(",") == 1:
                parts = text.split(",")
                if 1 <= len(parts[1]) <= 2:
                    text = text.replace(",", ".")
                else:
                    text = text.replace(",", "")
            else:
                text = text.replace(",", "")
        elif "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text or "." in text:
            separator = "," if "," in text else "."
            parts = text.lstrip("+-").split(separator)
            if len(parts) == 2:
                if len(parts[1]) == 3 and 1 <= len(parts[0]) <= 3:
                    raise ValueError(
                        f"valor ambíguo ({str(value).strip()}); escolhe o formato "
                        "Português ou Internacional"
                    )
                text = text.replace(separator, ".")
            elif all(len(group) == 3 for group in parts[1:]):
                text = text.replace(separator, "")
            elif len(parts[-1]) in {1, 2} and all(
                len(group) == 3 for group in parts[1:-1]
            ):
                integer = parts[0] + "".join(parts[1:-1])
                sign = (
                    "-" if text.startswith("-") else "+" if text.startswith("+") else ""
                )
                text = sign + integer + "." + parts[-1]
            else:
                raise ValueError(f"valor inválido ({str(value).strip()})")
        return require_eur_cent_precision(text, field_name="valor", allow_zero=allow_zero)

    @staticmethod
    def _mapped_value(
        raw: dict[str, str],
        mapping: dict[str, str],
        field: str,
    ) -> str:
        column = str(mapping.get(field, "") or "")
        return str(raw.get(column, "") or "") if column else ""

    def suggest_category(self, description: str) -> str:
        suggested = self.repo.suggested_category_for_description(description)
        if suggested:
            return canonical_category(suggested)
        lowered = description.casefold()
        defaults = _DEFAULT_CATEGORY_TERMS
        for keyword, category in defaults:
            if len(keyword) <= 3:
                pattern = rf"(?<!\w){re.escape(keyword)}(?!\w)"
                if re.search(pattern, lowered):
                    return category
            elif keyword in lowered:
                return category
        return "Diversos"

    @staticmethod
    def _parse_row_date(raw_date: str, date_format: str = "") -> date | None:
        return parse_import_date(raw_date, date_format)[0]

    def normalize_row(
        self,
        raw: dict[str, str],
        mapping: dict[str, str],
        *,
        apply_rules: bool,
        amount_locale: str = AMOUNT_LOCALE_AUTO,
        is_credit_card: bool = False,
        date_format: str = "",
        invert_signs: bool | None = None,
        source_row: int = 0,
    ) -> NormalizedImportRow:
        issues: list[str] = []
        raw_date = self._mapped_value(raw, mapping, "date")
        parsed_date, ambiguous_date = parse_import_date(raw_date, date_format)
        if ambiguous_date:
            issues.append(f"Data ambígua ({raw_date.strip()}); escolhe o formato do lote na pré-visualização.")
        elif parsed_date is None:
            issues.append(
                "Data em falta."
                if not raw_date.strip()
                else f"Data inválida ({raw_date.strip()}); usa AAAA-MM-DD ou DD/MM/AAAA."
            )
        description = self._mapped_value(raw, mapping, "desc").strip()
        if not description:
            issues.append("Descrição em falta.")

        raw_amount = self._mapped_value(raw, mapping, "amount").strip()
        raw_debit = self._mapped_value(raw, mapping, "amount_debit").strip()
        raw_credit = self._mapped_value(raw, mapping, "amount_credit").strip()

        has_debit_credit_mapping = bool(
            str(mapping.get("amount_debit", "") or "").strip()
            or str(mapping.get("amount_credit", "") or "").strip()
        )

        if has_debit_credit_mapping and (raw_debit or raw_credit):
            try:
                debit_val = (
                    abs(self.parse_amount(raw_debit, amount_locale, allow_zero=True))
                    if raw_debit
                    else 0.0
                )
                credit_val = (
                    abs(self.parse_amount(raw_credit, amount_locale, allow_zero=True))
                    if raw_credit
                    else 0.0
                )
                if raw_debit and raw_credit:
                    amount = credit_val - debit_val
                elif raw_debit:
                    amount = -debit_val
                else:
                    amount = credit_val
            except ValueError as exc:
                amount = 0.0
                issues.append(str(exc))
        elif raw_amount:
            try:
                amount = self.parse_amount(raw_amount, amount_locale)
            except ValueError as exc:
                amount = 0.0
                issues.append(str(exc))
        else:
            amount = 0.0
            issues.append("Valor em falta.")

        # BNK-PAR-01: Invert sign for credit card statements with single amount column
        # where purchases are positive invoice lines and payments are negative credits.
        # Do not invert if amounts were derived from separate debit/credit columns (already signed correctly).
        # The legacy argument is an explicit inversion request; account nature alone
        # is metadata and never reaches this transform from the import page.
        should_invert = bool(is_credit_card) if invert_signs is None else bool(invert_signs)
        if should_invert and amount != 0 and not (has_debit_credit_mapping and (raw_debit or raw_credit)):
            amount = -amount

        if amount == 0 and not any("em falta" in issue for issue in issues):
            issues.append("O valor não pode ser zero.")

        category = self._mapped_value(raw, mapping, "category").strip()
        if not category:
            category = (
                self.suggest_category(description)
                if apply_rules
                else "Rendimentos"
                if amount > 0
                else "Diversos"
            )
        category = canonical_category(category)
        validator = getattr(self.repo, "validate_transaction_category", None)
        if callable(validator) and amount != 0:
            try:
                category = str(validator(category, amount))
            except ValueError as exc:
                # If validation failed because an expense category has a positive refund amount, accept canonical category
                exc_str = str(exc).lower()
                if ("despesas" in exc_str or "receitas" in exc_str) and amount > 0:
                    pass
                else:
                    issues.append(str(exc))
        account = clean_account_name(self._mapped_value(raw, mapping, "account"))
        return NormalizedImportRow(
            date=parsed_date.isoformat() if parsed_date else "",
            description=description,
            amount=amount,
            category=category,
            account=account,
            payment_method=self._mapped_value(raw, mapping, "method").strip(),
            merchant=self._mapped_value(raw, mapping, "merchant").strip(),
            external_reference=self._mapped_value(raw, mapping, "reference").strip(),
            validation_issues=tuple(issues),
            source_values=tuple((str(key), str(value)) for key, value in raw.items()),
            source_row=source_row,
        )

    def _build_duplicate_index(
        self,
        transactions: list[NormalizedImportRow],
    ) -> dict[str, Any]:
        """Preload candidate database transactions for the entire batch date range."""
        valid_dates = [t.date[:10] for t in transactions if len(t.date) >= 10]
        references = [
            t.external_reference.strip()
            for t in transactions
            if t.external_reference.strip()
        ]

        ref_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        loose_index: dict[tuple[str, float, str], list[dict[str, Any]]] = {}

        if not valid_dates and not references:
            return {"active": False, "ref_index": ref_index, "loose_index": loose_index}

        try:
            if references:
                for i in range(0, len(references), 400):
                    chunk = references[i : i + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    sql = f"""
                        SELECT t.id, LOWER(TRIM(m.external_reference)) AS ref,
                               LOWER(TRIM(t.account)) AS account, t.description,
                               COALESCE(NULLIF(m.payment_date,''),t.date) AS date,
                               ROUND(t.amount,2) AS amount
                        FROM transactions t
                        JOIN transaction_meta m ON m.transaction_id=t.id
                        WHERE LOWER(TRIM(m.external_reference)) IN ({placeholders})
                    """
                    for row in self.repo.q(sql, tuple(r.lower() for r in chunk)):
                        ref_key = (str(row["ref"]).lower(), account_identity_key(row["account"]))
                        ref_index.setdefault(ref_key, []).append(dict(row))

            if valid_dates:
                min_date = min(valid_dates)
                max_date = max(valid_dates)
                sql = """
                    SELECT t.id, t.description, t.date, ROUND(t.amount, 2) AS amount,
                           TRIM(t.account) AS account,
                           IFNULL(m.merchant, '') AS merchant,
                           IFNULL(m.payment_date, '') AS payment_date
                    FROM transactions t
                    LEFT JOIN transaction_meta m ON m.transaction_id=t.id
                    WHERE (t.date >= ? AND t.date <= ?)
                       OR (m.payment_date >= ? AND m.payment_date <= ?)
                """
                for row in self.repo.q(sql, (min_date, max_date, min_date, max_date)):
                    d = str(row["payment_date"] or row["date"])[:10]
                    amt = round(float(row["amount"]), 2)
                    acc = account_identity_key(row["account"])
                    loose_index.setdefault((d, amt, acc), []).append(
                        {
                            "id": int(row["id"]),
                            "description": str(row["description"]),
                            "merchant": str(row["merchant"]),
                        }
                    )
            return {"active": True, "ref_index": ref_index, "loose_index": loose_index}
        except Exception as exc:
            log_exception("Failed to build duplicate pre-index, falling back", exc)
            return {"active": False, "ref_index": {}, "loose_index": {}}

    def _stored_duplicate_evidence_indexed(
        self,
        transaction: NormalizedImportRow,
        index: dict[str, Any],
        claimed_stored_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        if not index.get("active"):
            return self._stored_duplicate_evidence(transaction)

        ref = transaction.external_reference.strip().lower()
        acc = account_identity_key(transaction.account)
        ref_index = index["ref_index"]
        reused_reference = []
        if ref:
            available = [
                row for row in ref_index.get((ref, acc), [])
                if claimed_stored_ids is None or int(row["id"]) not in claimed_stored_ids
            ]
            exact = [int(row["id"]) for row in available
                     if str(row["date"])[:10] == transaction.date[:10]
                     and round(float(row["amount"]), 2) == round(transaction.amount, 2)
                     and str(row["description"]).strip().casefold() == transaction.description.strip().casefold()]
            if exact:
                if claimed_stored_ids is not None:
                    claimed_stored_ids.add(exact[0])
                return {"level": "certain", "label": "certa", "reason": "A mesma referência, data, valor, conta e descrição já existem.", "ids": exact}
            reused_reference = [int(row["id"]) for row in available]

        d = transaction.date[:10]
        amt = round(float(transaction.amount), 2)
        loose_index = index["loose_index"]
        candidate_rows = loose_index.get((d, amt, acc)) or loose_index.get((d, amt, ""))
        if not candidate_rows:
            return {"level": "possible", "reason": "Referência reutilizada com dados diferentes; revê sem descartar automaticamente.", "ids": reused_reference} if reused_reference else {"level": "none", "reason": ""}

        available_candidates = [
            r for r in candidate_rows
            if claimed_stored_ids is None or r["id"] not in claimed_stored_ids
        ]
        if not available_candidates:
            return {"level": "none", "reason": ""}

        desc_norm = transaction.description.strip().lower()
        merchant_norm = transaction.merchant.strip().lower()
        exact_matches = [
            r["id"]
            for r in available_candidates
            if r["description"].strip().lower() == desc_norm
            or (merchant_norm and r["merchant"].strip().lower() == merchant_norm)
            or (
                len(desc_norm) >= 15
                and len(r["description"].strip().lower()) >= 15
                and (
                    r["description"].strip().lower().startswith(desc_norm)
                    or desc_norm.startswith(r["description"].strip().lower())
                )
            )
        ]
        if exact_matches:
            if claimed_stored_ids is not None:
                claimed_stored_ids.add(exact_matches[0])
            return {
                "level": "probable",
                "label": "provável",
                "reason": "Existe uma compra muito semelhante. Confirma antes de decidir.",
                "ids": exact_matches,
            }
        if claimed_stored_ids is not None:
            claimed_stored_ids.add(available_candidates[0]["id"])
        return {
            "level": "possible",
            "label": "possível",
            "reason": "O valor e a data coincidem, mas a descrição é diferente.",
            "ids": [r["id"] for r in available_candidates],
        }

    def _stored_duplicate_evidence(
        self,
        transaction: NormalizedImportRow,
    ) -> dict[str, Any]:
        classifier = getattr(self.repo, "duplicate_classification", None)
        if callable(classifier):
            result = classifier(
                transaction.date,
                transaction.description,
                transaction.amount,
                transaction.account,
                transaction.external_reference,
                transaction.merchant,
            )
            if isinstance(result, dict):
                evidence = dict(result)
                if evidence.get("level") == "certain" and not evidence.get("identity_match"):
                    evidence.update(level="probable", reason="A referência coincide, mas falta confirmação da identidade completa. Revê o movimento.")
                return evidence
        legacy = getattr(self.repo, "is_duplicate_transaction", None)
        if callable(legacy) and legacy(
            transaction.date,
            transaction.description,
            transaction.amount,
            transaction.account,
            transaction.external_reference,
        ):
            return {
                "level": "probable",
                "reason": "Existe uma compra muito semelhante. Confirma antes de decidir.",
            }
        return {"level": "none", "reason": ""}

    @staticmethod
    def _batch_duplicate_evidence(
        transaction: NormalizedImportRow,
        seen_exact: set[tuple[Any, ...]],
        seen_loose: set[tuple[Any, ...]],
    ) -> dict[str, str]:
        exact = transaction.signature
        loose = (
            transaction.date[:10],
            round(float(transaction.amount), 2),
            account_identity_key(transaction.account),
        )
        if exact in seen_exact:
            certain = bool(transaction.external_reference)
            return {
                "level": "certain" if certain else "probable",
                "reason": (
                    "A referência bancária repete-se neste ficheiro."
                    if certain
                    else "A mesma data, valor, conta e descrição repetem-se no ficheiro. Confirma ambas as compras."
                ),
            }
        if loose in seen_loose:
            return {
                "level": "possible",
                "reason": "A data, o valor e a conta repetem-se no ficheiro, mas a descrição é diferente.",
            }
        return {"level": "none", "reason": ""}

    @staticmethod
    def _stronger_duplicate(
        first: dict[str, Any],
        second: dict[str, Any],
    ) -> dict[str, Any]:
        rank = {"none": 0, "possible": 1, "probable": 2, "certain": 3}
        return (
            first
            if rank.get(str(first.get("level")), 0)
            >= rank.get(str(second.get("level")), 0)
            else second
        )

    @staticmethod
    def _review_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def _review_digest(cls, value: Any) -> str:
        return hashlib.sha256(cls._review_json(value).encode("utf-8")).hexdigest()

    @classmethod
    def _review_snapshot(cls, rows: Sequence[ImportReviewRow]) -> str:
        # Stream the seal instead of duplicating the entire normalized batch in
        # a temporary list of dictionaries/JSON for large supported statements.
        digest = hashlib.sha256()
        for row in rows:
            digest.update(cls._review_json({
                "original": asdict(row.original), "proposed": asdict(row.proposed),
                "reason": row.reason, "source": row.source, "keyword": row.rule_keyword,
            }).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    @classmethod
    def _review_rules_digest(cls, rules: Sequence[dict[str, Any]]) -> str:
        return cls._review_digest({"rules": rules, "hints": _DEFAULT_CATEGORY_TERMS, "policy": 1})

    def _review_catalogue(self) -> str:
        # Identity and category changes can alter persistence after a preview.
        # Budget/reference values are irrelevant to the reviewed movement.
        return self._review_digest({
            "categories": [tuple(row) for row in self.repo.q(
                "SELECT stable_id,name,kind,is_active,identity_key FROM categories ORDER BY stable_id,name")],
            "accounts": [tuple(row) for row in self.repo.q(
                "SELECT stable_id,name,account_key,is_active FROM financial_accounts ORDER BY id")],
            "aliases": [tuple(row) for row in self.repo.q(
                "SELECT entity_type,entity_id,alias,identity_key FROM entity_aliases ORDER BY id")],
        })

    @staticmethod
    def _review_rule_match(description: str, rules: Sequence[dict[str, Any]]) -> tuple[str, str, str]:
        lowered = description.casefold()
        for rule in rules:
            keyword = str(rule.get("keyword") or "").strip().casefold()
            target = str(rule.get("category") or "").strip()
            if keyword and target and (
                re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", lowered)
                if len(keyword) <= 3 else keyword in lowered
            ):
                return canonical_category(target), "local_rule", keyword
        for keyword, category in _DEFAULT_CATEGORY_TERMS:
            if (re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", lowered)
                    if len(keyword) <= 3 else keyword in lowered):
                return canonical_category(category), "local_hint", keyword
        return "", "original", ""

    def review_batch(
        self, rows: list[dict[str, str]], mapping: dict[str, str], *,
        apply_rules: bool = True, amount_locale: str = AMOUNT_LOCALE_AUTO,
        is_credit_card: bool = False, date_format: str = "",
        invert_signs: bool | None = None, source_sha256: str = "",
        ignore_duplicates: bool = True,
    ) -> ImportReviewBatch:
        """Snapshot the entire batch without writing identities or learned rules."""
        rules = [dict(rule) for rule in self.repo.category_rules()]
        effective_format = date_format or detect_date_format_globally(rows, str(mapping.get("date", "")))
        reviewed = []
        for index, raw in enumerate(rows, start=1):
            original = self.normalize_row(
                raw, mapping, apply_rules=False, amount_locale=amount_locale,
                is_credit_card=is_credit_card, date_format=effective_format,
                invert_signs=invert_signs, source_row=index,
            )
            proposed, reason, source, keyword = original, "Categoria original do ficheiro ou categoria de base.", "original", ""
            if apply_rules and not self._mapped_value(raw, mapping, "category").strip():
                target, source, keyword = self._review_rule_match(original.description, rules)
                if target and target != original.category:
                    issues = list(original.validation_issues)
                    validator = getattr(self.repo, "validate_transaction_category", None)
                    if callable(validator) and original.amount:
                        try:
                            target = str(validator(target, original.amount))
                        except ValueError as exc:
                            if not (original.amount > 0 and any(word in str(exc).lower() for word in ("despesas", "receitas"))):
                                issues.append(str(exc))
                    proposed = replace(original, category=target, validation_issues=tuple(issues))
                    label = "Regra local" if source == "local_rule" else "Sugestão local por termo"
                    reason = f"{label} «{keyword}»: {original.category} → {target}. Requer uma escolha."
            entry = ImportReviewRow(original, proposed, reason, source, keyword)
            reviewed.append(replace(entry, decision="pending") if entry.has_suggestion else entry)
        options = dict(apply_rules=apply_rules, amount_locale=amount_locale,
                       is_credit_card=is_credit_card, date_format=date_format,
                       invert_signs=invert_signs, source_sha256=source_sha256,
                       ignore_duplicates=ignore_duplicates)
        result = tuple(reviewed)
        return ImportReviewBatch(
            result, json.dumps(rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            self._review_json(mapping), self._review_json(options),
            self._review_rules_digest(rules), self._review_catalogue(), self._review_snapshot(result),
        )

    def review_preview(self, batch: ImportReviewBatch, *, limit: int = 80) -> ImportPreview:
        return self._preview_normalized(
            [row.effective for row in batch.rows if row.decision != "exclude"], limit
        )

    def import_reviewed_batch(
        self, batch: ImportReviewBatch, *, rows: list[dict[str, str]] | None = None,
        mapping: dict[str, str] | None = None, **current_options: Any,
    ) -> ImportResult:
        """Persist frozen choices only after source/rule/identity revalidation."""
        if batch.pending_count:
            raise ValueError("Há sugestões pendentes; aceita ou rejeita cada sugestão antes de importar.")
        if any(row.decision not in {"keep", "accept", "reject", "exclude"} for row in batch.rows):
            raise ValueError("A revisão contém uma decisão inválida.")
        source_rows = json.loads(batch.source_rows_json) if rows is None else rows
        source_mapping = json.loads(batch.mapping_json) if mapping is None else mapping
        options = json.loads(batch.options_json)
        options.update(current_options)
        connection = self.repo.db.conn
        if connection.in_transaction:
            raise RuntimeError("Conclui ou cancela a transação pendente antes de importar o lote revisto.")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if (
                json.dumps(source_rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False) != batch.source_rows_json
                or self._review_json(source_mapping) != batch.mapping_json
                or self._review_json(options) != batch.options_json
                or self._review_rules_digest([dict(rule) for rule in self.repo.category_rules()]) != batch.rules_fingerprint
                or self._review_catalogue() != batch.catalogue_fingerprint
                or self._review_snapshot(batch.rows) != batch.snapshot_fingerprint
            ):
                raise ValueError("O ficheiro, mapeamento, regras ou identidades mudaram. Atualiza a revisão.")
            selected = [row.effective for row in batch.rows if row.decision != "exclude"]
            provenance = {row.original.source_row: {
                "decision": row.decision, "original_category": row.original.category,
                "proposed_category": row.proposed.category, "source": row.source,
                "keyword": row.rule_keyword, "reason": row.reason,
            } for row in batch.rows}
            effective_format = options["date_format"] or detect_date_format_globally(source_rows, str(source_mapping.get("date", "")))
            history_rows: list[NormalizedImportRow] = []
            result = self._import_normalized(
                selected, ignore_duplicates=options["ignore_duplicates"],
                effective_date_format=effective_format, is_credit_card=options["is_credit_card"],
                invert_signs=options["invert_signs"], source_sha256=options["source_sha256"],
                review_provenance=provenance, defer_commit=True,
                accepted_capture=history_rows,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        if result.created_ids:
            if self.change_journal is not None:
                self.change_journal._prune_after_committed_change()
            if options["source_sha256"]:
                self._record_file_history(options["source_sha256"], history_rows)
        return result

    def preview(
        self,
        rows: list[dict[str, str]],
        mapping: dict[str, str],
        *,
        apply_rules: bool = True,
        amount_locale: str = AMOUNT_LOCALE_AUTO,
        limit: int = 80,
        is_credit_card: bool = False,
        date_format: str = "",
        invert_signs: bool | None = None,
    ) -> ImportPreview:

        effective_date_format = date_format or detect_date_format_globally(rows, str(mapping.get("date", "")))
        normalized_list = [
            self.normalize_row(
                raw,
                mapping,
                apply_rules=apply_rules,
                amount_locale=amount_locale,
                is_credit_card=is_credit_card,
                date_format=effective_date_format,
                invert_signs=invert_signs,
                source_row=source_row,
            )
            for source_row, raw in enumerate(rows, start=1)
        ]
        return self._preview_normalized(normalized_list, limit)

    def _preview_normalized(self, normalized_list: list[NormalizedImportRow], limit: int) -> ImportPreview:
        preview_rows: list[ImportPreviewRow] = []
        duplicates = 0
        certain_duplicates = 0
        probable_duplicates = 0
        possible_duplicates = 0
        invalid = 0
        seen_exact: set[tuple[Any, ...]] = set()
        seen_loose: set[tuple[Any, ...]] = set()
        visible_limit = max(0, int(limit))

        duplicate_index = self._build_duplicate_index(
            [t for t in normalized_list if t.is_valid]
        )
        claimed_stored_ids: set[int] = set()

        for transaction in normalized_list:
            if not transaction.is_valid:
                status = "Rever"
                evidence = {"level": "none", "reason": ""}
                invalid += 1
            else:
                batch = self._batch_duplicate_evidence(
                    transaction,
                    seen_exact,
                    seen_loose,
                )
                stored = self._stored_duplicate_evidence_indexed(
                    transaction,
                    duplicate_index,
                    claimed_stored_ids=claimed_stored_ids,
                )
                evidence = self._stronger_duplicate(batch, stored)
                level = str(evidence.get("level") or "none")
                status = {
                    "certain": "Duplicado certo",
                    "probable": "Duplicado provável",
                    "possible": "Possível duplicado",
                }.get(level, "Novo")
                if level != "none":
                    duplicates += 1
                if level == "certain":
                    certain_duplicates += 1
                elif level == "probable":
                    probable_duplicates += 1
                elif level == "possible":
                    possible_duplicates += 1
            seen_exact.add(transaction.signature)
            seen_loose.add(
                (
                    transaction.date[:10],
                    round(float(transaction.amount), 2),
                    account_identity_key(transaction.account),
                )
            )
            if len(preview_rows) < visible_limit:
                preview_rows.append(
                    ImportPreviewRow(
                        transaction=transaction,
                        status=status,
                        duplicate_level=str(evidence.get("level") or "none"),
                        duplicate_reason=str(evidence.get("reason") or ""),
                    )
                )
        return ImportPreview(
            rows=preview_rows,
            total=len(normalized_list),
            duplicates=duplicates,
            invalid=invalid,
            certain_duplicates=certain_duplicates,
            probable_duplicates=probable_duplicates,
            possible_duplicates=possible_duplicates,
            ambiguous_dates=sum(
                any(issue.startswith("Data ambígua") for issue in row.validation_issues)
                for row in normalized_list
            ),
        )

    def import_rows(
        self,
        rows: list[dict[str, str]],
        mapping: dict[str, str],
        *,
        apply_rules: bool,
        ignore_duplicates: bool,
        ignore_probable: bool = False,
        amount_locale: str = AMOUNT_LOCALE_AUTO,
        is_credit_card: bool = False,
        date_format: str = "",
        invert_signs: bool | None = None,
        source_sha256: str = "",
    ) -> ImportResult:
        effective_date_format = date_format or detect_date_format_globally(rows, str(mapping.get("date", "")))
        normalized_list = [
            self.normalize_row(
                raw,
                mapping,
                apply_rules=apply_rules,
                amount_locale=amount_locale,
                is_credit_card=is_credit_card,
                date_format=effective_date_format,
                invert_signs=invert_signs,
                source_row=source_row,
            )
            for source_row, raw in enumerate(rows, start=1)
        ]
        return self._import_normalized(
            normalized_list, ignore_duplicates=ignore_duplicates,
            effective_date_format=effective_date_format, is_credit_card=is_credit_card,
            invert_signs=invert_signs, source_sha256=source_sha256,
        )

    def _import_normalized(
        self, normalized_list: list[NormalizedImportRow], *, ignore_duplicates: bool,
        effective_date_format: str, is_credit_card: bool, invert_signs: bool | None,
        source_sha256: str, review_provenance: dict[int, dict[str, Any]] | None = None,
        defer_commit: bool = False,
        accepted_capture: list[NormalizedImportRow] | None = None,
    ) -> ImportResult:
        accepted: list[NormalizedImportRow] = []
        skipped_invalid = 0
        skipped_duplicates = 0
        seen_exact: set[tuple[Any, ...]] = set()
        seen_loose: set[tuple[Any, ...]] = set()

        if any(any(issue.startswith("Data ambígua") for issue in row.validation_issues) for row in normalized_list):
            raise ValueError("Existem datas ambíguas. Escolhe o formato do lote na pré-visualização antes de importar.")
        duplicate_index = self._build_duplicate_index(
            [t for t in normalized_list if t.is_valid]
        )
        claimed_stored_ids: set[int] = set()

        for transaction in normalized_list:
            if not transaction.is_valid:
                skipped_invalid += 1
                continue
            if ignore_duplicates:
                evidence = self._stronger_duplicate(
                    self._batch_duplicate_evidence(
                        transaction,
                        seen_exact,
                        seen_loose,
                    ),
                    self._stored_duplicate_evidence_indexed(
                        transaction,
                        duplicate_index,
                        claimed_stored_ids=claimed_stored_ids,
                    ),
                )
                level = str(evidence.get("level"))
                if level == "certain":
                    skipped_duplicates += 1
                    seen_exact.add(transaction.signature)
                    seen_loose.add(
                        (
                            transaction.date[:10],
                            round(float(transaction.amount), 2),
                            account_identity_key(transaction.account),
                        )
                    )
                    continue

            seen_exact.add(transaction.signature)
            seen_loose.add(
                (
                    transaction.date[:10],
                    round(float(transaction.amount), 2),
                    account_identity_key(transaction.account),
                )
            )
            accepted.append(transaction)

        if accepted_capture is not None:
            accepted_capture.extend(accepted)

        normalized_rows = [
            {
                **asdict(transaction),
                "notes": "Importado por extrato · " + json.dumps({
                    "source_sha256": source_sha256,
                    "source_row": transaction.source_row,
                    "original": dict(transaction.source_values),
                    "date_format": effective_date_format,
                    "invert_signs": bool(is_credit_card) if invert_signs is None else bool(invert_signs),
                    **({"review": review_provenance[transaction.source_row]} if review_provenance else {}),
                }, ensure_ascii=False),
            }
            for transaction in accepted
        ]
        if defer_commit:
            created_ids = self.repo.import_transactions_batch(normalized_rows, commit=False)
            if self.change_journal is not None and created_ids:
                self.change_journal.record_created_transactions(
                    created_ids, label=f"Importação revista · {len(created_ids)} movimento(s)",
                    action_type="import", commit=False, prune_after=False,
                )
        elif self.change_journal is not None:
            created_ids = self.change_journal.run_with_created_transactions(
                lambda: self.repo.import_transactions_batch(
                    normalized_rows,
                    commit=False,
                ),
                label=lambda ids: f"Importação CSV · {len(ids)} movimento(s)",
                action_type="import",
            )
        else:
            created_ids = self.repo.import_transactions_batch(normalized_rows)
        if created_ids and source_sha256 and not defer_commit:
            self._record_file_history(source_sha256, accepted)
        return ImportResult(
            created_ids=created_ids,
            skipped_invalid=skipped_invalid,
            skipped_duplicates=skipped_duplicates,
        )

    def file_history_warning(self, source_sha256: str, accounts: Sequence[str] = ()) -> str:
        """Profile-local context only: a repeated file never blocks another import."""
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            return ""
        try:
            value = self.repo.setting("smart_import_file_" + source_sha256, "")
            previous = json.loads(value) if value else None
        except (AttributeError, TypeError, ValueError):
            return ""
        if not isinstance(previous, dict):
            return ""
        current = {account_identity_key(account) for account in accounts}
        overlap = bool(current & set(previous.get("accounts", [])))
        context = "nesta conta" if overlap else "noutra conta deste perfil"
        return f"Este ficheiro já foi importado {context}. Revê o contexto e os duplicados; a importação continua disponível."

    def _record_file_history(self, source_sha256: str, rows: Sequence[NormalizedImportRow]) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            return
        try:
            key = "smart_import_file_" + source_sha256
            value = self.repo.setting(key, "")
            prior = json.loads(value) if value else {}
            accounts = set(prior.get("accounts", [])) if isinstance(prior, dict) else set()
            accounts.update(account_identity_key(row.account) for row in rows)
            self.repo.set_setting(key, json.dumps({"accounts": sorted(accounts)}, ensure_ascii=False))
        except Exception as exc:
            # Imported rows are already committed; a warning-cache failure must
            # not present the operation as rolled back or encourage a duplicate retry.
            log_exception("Failed to retain contextual import file warning", exc)

    def assess_duplicates(
        self,
        transactions: Sequence[NormalizedImportRow],
    ) -> list[Any]:
        """Assess duplicate evidence for a sequence of normalized rows without importing."""
        duplicate_index = self._build_duplicate_index(
            [t for t in transactions if getattr(t, "is_valid", True)]
        )
        claimed_stored_ids: set[int] = set()
        seen_exact: set[tuple[Any, ...]] = set()
        seen_loose: set[tuple[Any, ...]] = set()
        results = []
        for t in transactions:
            batch = self._batch_duplicate_evidence(t, seen_exact, seen_loose)
            stored = self._stored_duplicate_evidence_indexed(
                t, duplicate_index, claimed_stored_ids=claimed_stored_ids
            )
            evidence = self._stronger_duplicate(batch, stored)
            seen_exact.add(t.signature)
            seen_loose.add(
                (
                    t.date[:10],
                    round(float(t.amount), 2),
                    account_identity_key(t.account),
                )
            )

            class DuplicateAssessment:
                def __init__(self, ev: dict[str, Any]) -> None:
                    self.duplicate_level = str(ev.get("level") or "none")
                    self.duplicate_reason = str(ev.get("reason") or "")
                    self.duplicate_ids = list(ev.get("ids") or [])

            results.append(DuplicateAssessment(evidence))
        return results
