import html
import re
from dataclasses import dataclass
from datetime import date

from utils.helpers import parse_flexible_date, safe_float


@dataclass(slots=True, frozen=True)
class ParsedBankStatement:
    headers: list[str]
    rows: list[dict[str, str]]
    account_id: str = ""
    bank_id: str = ""
    statement_type: str = "OFX"
    warnings: tuple[str, ...] = ()
    is_credit_card: bool = False


class OFXParser:
    """Robust parser for both OFX SGML 1.x (open leaf tags) and XML 2.x formats."""

    @classmethod
    def parse(cls, content: str) -> ParsedBankStatement:
        text = str(content or "")
        warnings: list[str] = []

        headers = [
            "Data",
            "Descrição",
            "Valor",
            "Referência bancária",
            "Comerciante",
            "Conta",
        ]

        def _extract_tag(tag_name: str, src: str) -> str:
            # BNK-PAR-03: Robust extraction supporting multiline XML tags (re.DOTALL)
            # and SGML open leaf tags without closing tags
            match_closed = re.search(
                rf"<{tag_name}(?:\s+[^>]*)?>(.*?)</{tag_name}>",
                src,
                re.DOTALL | re.IGNORECASE,
            )
            if match_closed:
                return match_closed.group(1).strip()
            match_open = re.search(
                rf"<{tag_name}(?:\s+[^>]*)?>([^<\r\n]+)",
                src,
                re.IGNORECASE,
            )
            return match_open.group(1).strip() if match_open else ""

        def _extract_multiline_tag(tag_name: str, src: str) -> str:
            # BUG-SEC-001: closed tags already span newlines via re.DOTALL;
            # SGML open leaf tags must also swallow the following raw lines
            # until the next tag (or the end of the block) instead of
            # truncating the value to its first line.
            match_closed = re.search(
                rf"<{tag_name}(?:\s+[^>]*)?>(.*?)</{tag_name}>",
                src,
                re.DOTALL | re.IGNORECASE,
            )
            if match_closed:
                return match_closed.group(1).strip()
            match_open = re.search(
                rf"<{tag_name}(?:\s+[^>]*)?>(.*?)(?=<[^>]*>|\Z)",
                src,
                re.DOTALL | re.IGNORECASE,
            )
            return match_open.group(1).strip() if match_open else ""

        # BUG-AUD-C111: credit-card statements (<CCSTMTTRNRS>/<CCSTMTRS>) use
        # inverted sign semantics: a <TRNTYPE>PAYMENT is the client settling
        # the card balance (an inflow on the card ledger), not an expense.
        # Detection must happen before per-transaction parsing so every
        # <STMTTRN> block of the statement shares the same context.
        is_credit_card_stmt = bool(
            re.search(
                r"<(?:CCSTMTTRNRS|CCSTMTRS|CCACCTFROM)>",
                text,
                re.IGNORECASE,
            )
        )

        def _parse_trn_block(
            block: str, acct_name: str, card_account: bool = False
        ) -> dict[str, str]:
            dt_posted = _extract_tag("DTPOSTED", block)
            trntype = _extract_tag("TRNTYPE", block).strip().upper()
            raw_amount = _extract_tag("TRNAMT", block).replace(",", ".")
            amount = safe_float(raw_amount, 0.0)

            # BNK-PAR-02: Enforce correct sign matching OFX TRNTYPE specification
            debit_types = (
                "DEBIT",
                "CHECK",
                "PAYMENT",
                "FEE",
                "POS",
                "ATM",
                "SRVCHG",
                "CASH",
                "DIRECTDEBIT",
                "REPEATPMT",
            )
            credit_types = ("CREDIT", "DEP", "INT", "DIV", "DIRECTDEP")
            if trntype == "PAYMENT" and card_account:
                # On a credit card, PAYMENT is balance settlement (credit/inflow).
                amount = abs(amount)
            elif trntype in debit_types and amount > 0:
                amount = -amount
            elif trntype in credit_types and amount < 0:
                amount = abs(amount)

            fitid = (
                _extract_tag("FITID", block)
                or _extract_tag("CHECKNUM", block)
                or _extract_tag("REFNUM", block)
            )
            name = html.unescape(_extract_tag("NAME", block))
            memo = html.unescape(_extract_multiline_tag("MEMO", block))

            if name and memo and name.casefold() != memo.casefold():
                description = f"{name} · {memo}"
            else:
                description = name or memo or "Movimento bancário"

            parsed_date: date | None = None
            if dt_posted:
                date_clean = dt_posted[:8]
                if len(date_clean) == 8 and date_clean.isdigit():
                    try:
                        parsed_date = date(
                            int(date_clean[:4]),
                            int(date_clean[4:6]),
                            int(date_clean[6:8]),
                        )
                    except ValueError:
                        parsed_date = None
                if parsed_date is None:
                    parsed_date = parse_flexible_date(dt_posted)

            date_str = parsed_date.isoformat() if parsed_date else dt_posted[:10]

            return {
                "Data": date_str,
                "Descrição": description,
                "Valor": f"{amount:.2f}",
                "Referência bancária": fitid,
                "Comerciante": name,
                "Conta": acct_name or "Conta principal",
                "Data original": dt_posted,
                "Valor original": _extract_tag("TRNAMT", block),
                "Tipo OFX": trntype,
                "Natureza da conta": "credit_card" if card_account else "bank",
            }

        # Check for statement response containers (<STMTTRNRS>, <CCSTMTTRNRS>, <STMTRS>, <CCSTMTRS>)
        stmt_sections = re.findall(
            r"<(STMTTRNRS|CCSTMTTRNRS|STMTRS|CCSTMTRS|INVSTMTTRNRS)>(.*?)(?:</\1>|(?=<(?:STMTTRNRS|CCSTMTTRNRS|STMTRS|CCSTMTRS|INVSTMTTRNRS)>)|$)",
            text,
            re.DOTALL | re.IGNORECASE,
        )

        rows: list[dict[str, str]] = []
        global_account_id = ""
        global_bank_id = ""

        if stmt_sections:
            for _tag_name, section_text in stmt_sections:
                sec_acct = _extract_tag("ACCTID", section_text)
                sec_bank = _extract_tag("BANKID", section_text)
                if sec_acct and not global_account_id:
                    global_account_id = sec_acct
                if sec_bank and not global_bank_id:
                    global_bank_id = sec_bank

                sec_trn_blocks = re.findall(
                    r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>)|$)",
                    section_text,
                    re.DOTALL | re.IGNORECASE,
                )
                for trn_block in sec_trn_blocks:
                    rows.append(
                        _parse_trn_block(
                            trn_block,
                            sec_acct or "Conta principal",
                            _tag_name.upper().startswith("CC")
                            or bool(re.search(r"<CCACCTFROM>", section_text, re.IGNORECASE)),
                        )
                    )
        else:
            # Fallback for flat or non-standard OFX files
            global_account_id = _extract_tag("ACCTID", text)
            global_bank_id = _extract_tag("BANKID", text)
            trn_blocks = re.findall(
                r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>)|$)",
                text,
                re.DOTALL | re.IGNORECASE,
            )
            for trn_block in trn_blocks:
                rows.append(
                    _parse_trn_block(
                        trn_block, global_account_id or "Conta principal", is_credit_card_stmt
                    )
                )

        if not rows and ("<OFX>" in text.upper() or "<BANKTRANLIST>" in text.upper()):
            warnings.append(
                "O ficheiro OFX foi reconhecido, mas não continha transações <STMTTRN>."
            )

        # BUG-SEC-001: credit-card statements are identified by their OFX
        # statement container tags (<CCSTMTTRNRS>/<CCSTMTRS>) and by the
        # credit-card account block (<CCACCTFROM>) that some exports emit
        # inside a plain <STMTRS> container.
        is_cc = bool(
            re.search(
                r"<(?:CCSTMTTRNRS|CCSTMTRS|CCACCTFROM)>",
                text,
                re.IGNORECASE,
            )
        )

        return ParsedBankStatement(
            headers=headers,
            rows=rows,
            account_id=global_account_id,
            bank_id=global_bank_id,
            statement_type="OFX",
            warnings=tuple(warnings),
            is_credit_card=is_cc,
        )


class QIFParser:
    """Parser for QIF (Quicken Interchange Format) financial files."""

    @classmethod
    def parse(cls, content: str) -> ParsedBankStatement:
        text = str(content or "")
        lines = text.splitlines()
        rows: list[dict[str, str]] = []
        headers = [
            "Data",
            "Descrição",
            "Valor",
            "Referência bancária",
            "Comerciante",
            "Conta",
            "Categoria",
        ]
        warnings: list[str] = []

        current_record: dict[str, str] = {}
        in_account_header = False
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("!"):
                # Header line like !Type:Bank or !Account
                if line.casefold().startswith("!account"):
                    in_account_header = True
                    current_record.clear()
                elif line.casefold().startswith("!type"):
                    in_account_header = False
                    current_record.clear()
                continue
            if in_account_header:
                if line == "^":
                    current_record.clear()
                continue
            if line == "^":
                # End of record
                if current_record:
                    date_val = current_record.get("D", "")
                    date_str = date_val

                    raw_amount = current_record.get("T", "0").strip()
                    payee = html.unescape(current_record.get("P", "").strip())
                    memo = html.unescape(current_record.get("M", "").strip())
                    category = html.unescape(current_record.get("L", "").strip())
                    ref = current_record.get("N", "").strip()

                    if payee and memo and payee.casefold() != memo.casefold():
                        description = f"{payee} · {memo}"
                    else:
                        description = payee or memo or "Movimento bancário"

                    rows.append(
                        {
                            "Data": date_str,
                            "Descrição": description,
                            "Valor": raw_amount,
                            "Referência bancária": ref,
                            "Comerciante": payee,
                            "Conta": "Conta principal",
                            "Categoria": category,
                        }
                    )
                    current_record = {}
                continue

            field_type = line[0]
            field_value = line[1:].strip()
            current_record[field_type] = field_value

        # Process trailing record without final ^
        if current_record and not in_account_header:
            date_val = current_record.get("D", "")
            date_str = date_val
            raw_amount = current_record.get("T", "0").strip()

            payee = html.unescape(current_record.get("P", "").strip())
            memo = html.unescape(current_record.get("M", "").strip())
            category = html.unescape(current_record.get("L", "").strip())
            ref = current_record.get("N", "").strip()
            description = (
                f"{payee} · {memo}"
                if payee and memo and payee.casefold() != memo.casefold()
                else payee or memo or "Movimento bancário"
            )
            rows.append(
                {
                    "Data": date_str,
                    "Descrição": description,
                    "Valor": raw_amount,
                    "Referência bancária": ref,
                    "Comerciante": payee,
                    "Conta": "Conta principal",
                    "Categoria": category,
                }
            )

        return ParsedBankStatement(
            headers=headers,
            rows=rows,
            statement_type="QIF",
            warnings=tuple(warnings),
        )
