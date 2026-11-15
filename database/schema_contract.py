"""Read-only schema trust and financial diagnostics for protected profiles.

Expected objects come from application-owned DDL executed in SQLite memory.
No object from an inspected profile is executed to construct its own allowlist.
"""

from __future__ import annotations

import math
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any


_TOKENS = re.compile(
    r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
    r"`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|"
    r"\d+(?:\.\d+)?|<>|!=|<=|>=|==|\|\||\S",
    re.S,
)


def normalized_sql(sql: str | None) -> tuple[str, ...]:
    """Remove comments/formatting, preserving every quoted literal and operator."""
    return tuple(
        token.casefold() if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", token) else token
        for token in _TOKENS.findall(str(sql or ""))
        if not token.startswith(("--", "/*"))
    )


def _quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _affinity(value: str) -> str:
    upper = value.upper()
    if "INT" in upper:
        return "INTEGER"
    if any(part in upper for part in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in upper or not upper:
        return "BLOB"
    if any(part in upper for part in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _checks(sql: str) -> tuple[tuple[str, ...], ...]:
    tokens = normalized_sql(sql)
    result = []
    for index, token in enumerate(tokens):
        if token != "check" or index + 1 >= len(tokens) or tokens[index + 1] != "(":
            continue
        depth = 0
        for end in range(index + 1, len(tokens)):
            depth += tokens[end] == "("
            depth -= tokens[end] == ")"
            if depth == 0:
                result.append(tokens[index + 1 : end + 1])
                break
    return tuple(sorted(result))


def _shape(connection: sqlite3.Connection, table: str, sql: str) -> dict[str, Any]:
    columns = {
        str(row[1]): (
            _affinity(str(row[2])),
            int(row[3]),
            normalized_sql(row[4]),
            int(row[5]),
            int(row[6]),
        )
        for row in connection.execute(f"PRAGMA table_xinfo({_quoted(table)})")
    }
    foreign_key_groups: dict[int, list[tuple[Any, ...]]] = defaultdict(list)
    for row in connection.execute(f"PRAGMA foreign_key_list({_quoted(table)})"):
        foreign_key_groups[int(row[0])].append(tuple(row)[1:])
    foreign_keys = sorted(
        (
            tuple(sorted(group, key=lambda item: item[0]))
            for group in foreign_key_groups.values()
        ),
        key=str,
    )
    automatic_unique = []
    for row in connection.execute(f"PRAGMA index_list({_quoted(table)})"):
        if str(row[3]) in {"u", "pk"}:
            automatic_unique.append(
                tuple(
                    tuple(item)[2:]
                    for item in connection.execute(
                        f"PRAGMA index_xinfo({_quoted(str(row[1]))})"
                    )
                )
            )
    collations = {}
    tokens = normalized_sql(sql)
    depth = 0
    definition: list[str] = []
    definitions = []
    for token in tokens:
        if token == "(":
            depth += 1
            if depth == 1:
                continue
        elif token == ")":
            depth -= 1
            if depth == 0:
                definitions.append(definition)
                break
        if token == "," and depth == 1:
            definitions.append(definition)
            definition = []
        elif depth:
            definition.append(token)
    for definition in definitions:
        if not definition:
            continue
        column = definition[0].strip('"`[]')
        if column in columns:
            collations[column] = tuple(
                definition[i + 1]
                for i, token in enumerate(definition[:-1])
                if token == "collate"
            )
    # PRAGMA metadata omits conflict-resolution and deferred-FK timing. Keep
    # their full owning declarations so policy changes cannot hide behind an
    # otherwise identical column/PK/FK shape.
    constraint_policies = tuple(
        sorted(
            tuple(definition)
            for definition in definitions
            if "deferrable" in definition
            or any(
                definition[index : index + 2] == ["on", "conflict"]
                for index in range(len(definition) - 1)
            )
        )
    )
    return {
        "columns": columns,
        "foreign_keys": foreign_keys,
        "automatic_unique": sorted(automatic_unique, key=str),
        "checks": _checks(sql),
        "collations": collations,
        "constraint_policies": constraint_policies,
        "virtual_sql": normalized_sql(sql)
        if "virtual" in normalized_sql(sql)[:3]
        else (),
        "options": tuple(
            t for t in normalized_sql(sql)[-3:] if t in {"strict", "without", "rowid"}
        ),
    }


def _capture(connection: sqlite3.Connection) -> dict[str, Any]:
    objects = {}
    for kind, name, table, sql in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ):
        name = str(name)
        if name.startswith("sqlite_"):
            continue
        item = {"kind": str(kind), "table": str(table), "sql": normalized_sql(sql)}
        if kind == "table":
            item["shape"] = _shape(connection, name, str(sql or ""))
        objects[name] = item
    return objects


def _build_schema_contracts(
    *, historical_baseline: bool = False
) -> dict[int, dict[str, Any]]:
    """Replay trusted migration DDL in memory; never execute inspected SQL."""
    from database.engine import FinanceDB

    reference = FinanceDB.__new__(FinanceDB)
    reference.conn = sqlite3.connect(":memory:")
    reference.conn.row_factory = sqlite3.Row
    reference.path = Path(":memory:")
    reference.attachments_directory = Path("__empty_schema_reference_attachments__")
    reference._closed = False
    reference._read_only = False
    reference._startup_progress = None
    contracts: dict[int, dict[str, Any]] = {}
    try:
        if historical_baseline:
            # Exact pre-ledger baseline retained by the supported A03 restore
            # contract. These declarations persist after additive migrations.
            reference.conn.executescript(
                "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);"
                "CREATE TABLE categories(name TEXT PRIMARY KEY,kind TEXT NOT NULL,colour TEXT NOT NULL,budget REAL NOT NULL DEFAULT 0);"
                "CREATE TABLE transactions(id INTEGER PRIMARY KEY,date TEXT NOT NULL,description TEXT NOT NULL,amount REAL NOT NULL,category TEXT NOT NULL,account TEXT NOT NULL,kind TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            )
        reference.init_schema()
        contracts[0] = _capture(reference.conn)

        def observe(version: int) -> None:
            if version >= 16:
                reference._defer_validation_trigger_rebuild = False
                try:
                    reference._migration_value_constraints()
                finally:
                    reference._defer_validation_trigger_rebuild = True
            contracts[version] = _capture(reference.conn)

        reference._schema_contract_observer = observe
        reference._ensure_migrations()
        reference.ensure_analytics_tables()
        reference.seed_if_empty()
        reference._ensure_stable_financial_entity_identity()
        reference.ensure_reference_data()
        reference._ensure_stable_financial_entity_identity()
        contracts[FinanceDB.CURRENT_SCHEMA_VERSION] = _capture(reference.conn)
    finally:
        reference.conn.close()
    return contracts


@lru_cache(maxsize=1)
def trusted_schema_contracts() -> dict[int, dict[str, Any]]:
    """Canonical fresh-profile reference versions, built once per process."""
    return _build_schema_contracts()


@lru_cache(maxsize=1)
def _historical_schema_contracts() -> dict[int, dict[str, Any]]:
    return _build_schema_contracts(historical_baseline=True)


@lru_cache(maxsize=1)
def _retained_profile_contract() -> dict[str, Any]:
    """Build exact reviewed declarations independently of the inspected profile."""
    from database.retained_schema import (
        COVERING_INDEX_SQL,
        LEGACY_SEARCH_SQL,
        RETAINED_TABLE_SQL,
    )

    connection = sqlite3.connect(":memory:")
    try:
        # Only stubs needed to compile the fixed index/trigger declarations.
        connection.execute("CREATE TABLE transaction_meta(transaction_id INTEGER)")
        for sql in (*RETAINED_TABLE_SQL.values(), *LEGACY_SEARCH_SQL.values(), COVERING_INDEX_SQL):
            connection.execute(sql)
        captured = _capture(connection)
        return {
            name: captured[name]
            for name in (*RETAINED_TABLE_SQL, *LEGACY_SEARCH_SQL, "idx_transactions_date_amount_cat")
        }
    finally:
        connection.close()


def inspect_schema_contract(
    connection: sqlite3.Connection, *, allow_legacy: bool = True
) -> dict[str, Any]:
    """Inspect a financial profile without migrations, repairs, writes or commits."""
    from database.engine import FinanceDB

    contracts = trusted_schema_contracts()
    current = contracts[FinanceDB.CURRENT_SCHEMA_VERSION]
    historical = _historical_schema_contracts()
    actual = _capture(connection)
    versions = []
    problems: list[str] = []
    if "schema_migrations" in actual:
        try:
            versions = [
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            ]
        except sqlite3.DatabaseError as exc:
            problems.append(f"Registo de migrações inválido: {exc}")
    if any(type(version) is not int or version < 1 for version in versions):
        problems.append("Versões de migração inválidas")
        version = 0
    else:
        version = max(versions, default=0)
        if versions and set(versions) != set(range(1, version + 1)):
            problems.append("Sequência de migrações incompleta")
    if version > FinanceDB.CURRENT_SCHEMA_VERSION:
        problems.append("Versão de esquema não suportada")
    legacy = allow_legacy and version < FinanceDB.CURRENT_SCHEMA_VERSION
    expected = contracts.get(version, current)
    # A migration ledger is a capability claim, including on legacy sources.
    # Share the historical minimum contract with restore preflight without
    # calling its validator recursively.
    from database.restore_validation import _BASELINE_COLUMNS, _CAPABILITIES

    mandatory = (
        {table: set(columns) for table, columns in _BASELINE_COLUMNS.items()}
        if actual
        else {}
    )
    for introduced, capabilities in _CAPABILITIES:
        if version >= introduced:
            for table, columns in capabilities.items():
                mandatory.setdefault(table, set()).update(columns)
    missing_capabilities = []
    for table, columns in mandatory.items():
        actual_columns = actual.get(table, {}).get("shape", {}).get("columns", {})
        missing_capabilities.extend(
            f"{table}.{column}" for column in sorted(columns - set(actual_columns))
        )
    if missing_capabilities:
        problems.append(
            "Capacidades declaradas em falta: " + ", ".join(missing_capabilities)
        )
    # Historical helpers may have installed an already-known later capability.
    allowed = (
        [
            family[v]
            for family in (contracts, historical)
            for v in family
            if v >= version
        ]
        if legacy
        else [current, historical[FinanceDB.CURRENT_SCHEMA_VERSION]]
    )
    retained = {}
    if 30 <= version <= FinanceDB.CURRENT_SCHEMA_VERSION:
        # Retained data declarations survive additive migrations. Obsolete
        # executable search bodies are accepted only before the upgrade that
        # replaces them; they are never trusted as current-version triggers.
        retained = {
            name: item for name, item in _retained_profile_contract().items()
            if item["kind"] != "trigger" or (legacy and allow_legacy)
        }
    unexpected = []
    drift = []
    for name, item in actual.items():
        candidates = [contract[name] for contract in allowed if name in contract]
        if name in retained:
            candidates.append(retained[name])
        if not candidates:
            unexpected.append(name)
            continue
        if item["kind"] != "table":
            if not any(item == candidate for candidate in candidates):
                drift.append(name)
            continue
        if not any(candidate["kind"] == "table" for candidate in candidates):
            drift.append(name)
            continue
        actual_shape = item["shape"]
        candidates = [
            candidate["shape"]
            for candidate in candidates
            if candidate["kind"] == "table"
        ]
        # Supported old columns are compared by declaration, not physical order.
        matching = any(actual_shape == candidate for candidate in candidates)
        if legacy and not matching:
            matching = any(
                all(
                    column in candidate["columns"]
                    and signature == candidate["columns"][column]
                    for column, signature in actual_shape["columns"].items()
                )
                and actual_shape["checks"] == candidate["checks"]
                and actual_shape["options"] == candidate["options"]
                and actual_shape["constraint_policies"]
                == candidate["constraint_policies"]
                and actual_shape["virtual_sql"] == candidate["virtual_sql"]
                and all(
                    candidate["collations"].get(column) == collation
                    for column, collation in actual_shape["collations"].items()
                )
                and all(
                    fk in candidate["foreign_keys"]
                    for fk in actual_shape["foreign_keys"]
                )
                and actual_shape["automatic_unique"] == candidate["automatic_unique"]
                # Reviewed retained declarations require an exact shape; they
                # do not expand the older additive-column subset policy.
                for candidate in (
                    contract[name]["shape"] for contract in allowed
                    if name in contract and contract[name]["kind"] == "table"
                )
            )
        if not matching:
            drift.append(name)
    missing = (
        sorted(name for name in expected if name not in actual) if not legacy else []
    )
    if unexpected:
        problems.append("Objetos SQL inesperados: " + ", ".join(sorted(unexpected)))
    if drift:
        problems.append(
            "Definições SQL/estrutura alteradas: " + ", ".join(sorted(drift))
        )
    if missing:
        problems.append("Capacidades/índices em falta: " + ", ".join(missing))
    return {
        "ok": not problems,
        "schema_version": version,
        "legacy": legacy,
        "unexpected_objects": sorted(unexpected),
        "drifted_objects": sorted(drift),
        "missing_objects": missing,
        "missing_capabilities": missing_capabilities,
        "issues": problems,
    }


def assert_schema_contract(
    connection: sqlite3.Connection, *, allow_legacy: bool = True
) -> dict[str, Any]:
    report = inspect_schema_contract(connection, allow_legacy=allow_legacy)
    if not report["ok"]:
        raise ValueError(
            "A estrutura SQL exige revisão antes de gravar. "
            + "; ".join(report["issues"])
        )
    return report


def is_trusted_derived_table(connection: sqlite3.Connection, table: str) -> bool:
    """Only the exact source-owned search root and its exact shadows are derived."""
    from database.engine import FinanceDB

    root = "transaction_search_fts"
    trusted = trusted_schema_contracts()[FinanceDB.CURRENT_SCHEMA_VERSION]
    names = {name for name in trusted if name == root or name.startswith(root + "_")}
    if table not in names:
        return False
    for name in {root, table}:
        row = connection.execute(
            "SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()
        expected = trusted[name]
        if (
            row is None
            or row[0] != expected["kind"]
            or row[1] != expected["table"]
            or normalized_sql(row[2]) != expected["sql"]
        ):
            return False
        if _shape(connection, name, str(row[2] or "")) != expected["shape"]:
            return False
    return True


def financial_integrity_report(connection: sqlite3.Connection) -> dict[str, Any]:
    """Check ledger semantics without changing precision or deleting evidence."""
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    issues: list[str] = []
    totals: dict[str, dict[str, Decimal]] = {
        "account": defaultdict(Decimal),
        "category": defaultdict(Decimal),
    }
    transfers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    transactions: dict[int, dict[str, Any]] = {}
    identities: dict[str, dict[str, str]] = {}
    for kind, table in (("category", "categories"), ("account", "financial_accounts")):
        if table not in tables:
            continue
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({_quoted(table)})")
        }
        if {"stable_id", "name"}.issubset(columns):
            identities[kind] = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    f"SELECT stable_id,name FROM {_quoted(table)}"
                )
                if row[0]
            }

    def inspect_identity(row: dict[str, Any], kind: str, label: str) -> None:
        stable_id = str(row.get(kind + "_id") or "")
        if not stable_id or kind not in identities:
            return  # Blank legacy links can be filled; declared links are evidence.
        canonical = identities[kind].get(stable_id)
        if canonical is None or str(row.get(kind) or "") != canonical:
            issues.append(f"{label}: identidade de {kind} sem correspondência canónica")

    if "transactions" in tables:
        cursor = connection.execute("SELECT * FROM transactions")
        names = [column[0] for column in cursor.description]
        for raw in cursor:
            row = dict(zip(names, raw))
            identity = int(row["id"])
            transactions[identity] = row
            inspect_identity(row, "category", f"Movimento {identity}")
            inspect_identity(row, "account", f"Movimento {identity}")
            value = row["amount"]
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value == 0
            ):
                issues.append(f"Movimento {identity}: valor não finito ou nulo")
                continue
            if row.get("kind") != ("income" if value > 0 else "expense"):
                issues.append(
                    f"Movimento {identity}: natureza incompatível com o sinal"
                )
            decimal = Decimal(str(value))
            for key in totals:
                totals[key][str(row.get(key + "_id") or row.get(key) or "")] += decimal
            if row.get("transfer_group_id"):
                transfers[str(row["transfer_group_id"])].append(row)
        for group, rows in transfers.items():
            if (
                len(rows) != 2
                or abs(sum(Decimal(str(row["amount"])) for row in rows))
                > Decimal("0.005")
                or len({row.get("account_id") or row.get("account") for row in rows})
                != 2
            ):
                issues.append(
                    f"Transferência {group}: par financeiro incompleto ou desequilibrado"
                )
    if "transaction_splits" in tables:
        cursor = connection.execute("SELECT * FROM transaction_splits")
        names = [column[0] for column in cursor.description]
        for raw in cursor:
            split = dict(zip(names, raw))
            inspect_identity(split, "category", f"Repartição {split.get('id')}")
        for txid, allocated in connection.execute(
            "SELECT transaction_id,SUM(ABS(amount)) FROM transaction_splits GROUP BY transaction_id"
        ):
            source = transactions.get(int(txid))
            if (
                source is not None
                and float(allocated or 0) > abs(float(source["amount"])) + 0.005
            ):
                issues.append(f"Movimento {txid}: repartições excedem o valor")
    if "subscription_price_reviews" in tables:
        accepted_sources: dict[int, set[int]] = defaultdict(set)
        subscription_ids = {row[0] for row in connection.execute("SELECT id FROM subscriptions")}
        cursor = connection.execute("SELECT * FROM subscription_price_reviews")
        names = [column[0] for column in cursor.description]
        for raw in cursor:
            review = dict(zip(names, raw))
            try:
                evidence = json.loads(review["evidence_json"])
                canonical = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != review["evidence_key"]:
                    raise ValueError("digest")
                for field, evidence_name in (("subscription_id", "subscription"), ("transaction_id", "transaction")):
                    if review[field] is not None and review[field] != evidence[evidence_name]["id"]:
                        raise ValueError("source identity")
                if review["subscription_id"] is not None and review["subscription_id"] not in subscription_ids:
                    raise ValueError("missing subscription")
                if review["transaction_id"] is not None and review["transaction_id"] not in transactions:
                    raise ValueError("missing transaction")
                if review["status"] == "accepted" and review["classification"] == "unclassified":
                    raise ValueError("classification")
                if review["status"] == "accepted" and review["classification"] == "price":
                    if not review["match_confirmed"] or review["confirmed_currency"] != "EUR" or review["confirmed_period"] != evidence["subscription"]["period"]:
                        raise ValueError("comparison basis")
                if review["status"] == "accepted" and review["classification"] in {"price", "plan"} and review["transaction_id"] is not None and review["subscription_id"] is not None:
                    accepted_sources[int(review["transaction_id"])].add(int(review["subscription_id"]))
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
                issues.append(f"Revisão de subscrição {review.get('id')}: evidência ou confirmação incoerente")
        for transaction_id, subscriptions in accepted_sources.items():
            if len(subscriptions) > 1:
                issues.append(f"Movimento {transaction_id}: cobrança aceite para várias subscrições")

    def settled_cents(transaction_id: int, *, receipt: bool) -> int | None:
        from database.validation import INTERNAL_FLOW_CATEGORIES

        movement = transactions.get(transaction_id)
        if movement is None or movement.get("category") in INTERNAL_FLOW_CATEGORIES or movement.get("transfer_group_id"):
            return None
        try:
            value = Decimal(str(movement["amount"]))
            if not value.is_finite() or value == 0 or (value > 0) != receipt:
                return None
            if movement.get("kind") != ("income" if receipt else "expense"):
                return None
            cents = abs(value) * 100
            return int(cents) if cents == cents.to_integral_value() else None
        except (ValueError, TypeError, ArithmeticError):
            return None

    if "reimbursement_claims" in tables:
        claims = {}
        claim_allocations: dict[int, int] = defaultdict(int)
        receipt_allocations: dict[int, int] = defaultdict(int)
        for identity, source, expected in connection.execute(
            "SELECT id,source_transaction_id,expected_cents FROM reimbursement_claims"
        ):
            claims[int(identity)] = expected
            source_cents = settled_cents(int(source), receipt=False)
            if type(expected) is not int or expected <= 0 or source_cents is None or expected > source_cents:
                issues.append(f"Reembolso {identity}: expectativa incompatível com a despesa de origem")
        if "reimbursement_receipts" in tables:
            for identity, claim, source, amount in connection.execute(
                "SELECT id,claim_id,receipt_transaction_id,amount_cents FROM reimbursement_receipts"
            ):
                source_cents = settled_cents(int(source), receipt=True)
                if int(claim) not in claims or type(amount) is not int or amount <= 0 or source_cents is None:
                    issues.append(f"Recebimento {identity}: ligação ou valor incompatível com o movimento")
                    continue
                claim_allocations[int(claim)] += amount
                receipt_allocations[int(source)] += amount
            for identity, amount in claim_allocations.items():
                expected = claims[identity]
                if type(expected) is not int or amount > expected:
                    issues.append(f"Reembolso {identity}: recebimentos excedem a expectativa")
            for identity, amount in receipt_allocations.items():
                if amount > (settled_cents(identity, receipt=True) or 0):
                    issues.append(f"Movimento {identity}: recebimentos atribuídos mais de uma vez")
    return {
        "ok": not issues,
        "issues": issues,
        "transaction_count": len(transactions),
        "totals": {
            kind: {key: str(value) for key, value in sorted(values.items())}
            for kind, values in totals.items()
        },
    }


def orphan_inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    """Classify unresolved relations as evidence; never call all orphans derived."""
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    links = {
        "transaction_meta": ("transaction_id", "transactions"),
        "transaction_splits": ("transaction_id", "transactions"),
        "transaction_attachments": ("transaction_id", "transactions"),
        "subscription_occurrences": ("subscription_id", "subscriptions"),
        "scheduled_payment_occurrences": ("scheduled_id", "scheduled_payments"),
    }
    result = {}
    for table, (column, parent) in links.items():
        if table not in tables or parent not in tables:
            continue
        rows = [
            tuple(row)
            for row in connection.execute(
                f"SELECT * FROM {_quoted(table)} WHERE {_quoted(column)} NOT IN (SELECT id FROM {_quoted(parent)})"
            )
        ]
        result[table] = {
            "count": len(rows),
            "classification": "financial_or_document_evidence",
            "automatic_delete_allowed": False,
        }
    product_links = {
        "subscription_price_reviews": (("subscription_id", "subscriptions"), ("transaction_id", "transactions")),
        "reimbursement_claims": (("source_transaction_id", "transactions"),),
        "reimbursement_receipts": (("claim_id", "reimbursement_claims"), ("receipt_transaction_id", "transactions")),
    }
    for table, relations in product_links.items():
        if table not in tables or any(parent not in tables for _, parent in relations):
            continue
        clauses = [f"({_quoted(column)} IS NOT NULL AND {_quoted(column)} NOT IN (SELECT id FROM {_quoted(parent)}))" for column, parent in relations]
        count = connection.execute(f"SELECT COUNT(*) FROM {_quoted(table)} WHERE " + " OR ".join(clauses)).fetchone()[0]
        result[table] = {"count": count, "classification": "financial_or_document_evidence", "automatic_delete_allowed": False}
        if table == "subscription_price_reviews":
            result[table]["retained_detached_reviews"] = connection.execute(
                "SELECT COUNT(*) FROM subscription_price_reviews WHERE subscription_id IS NULL OR transaction_id IS NULL"
            ).fetchone()[0]
    return {
        "dry_run": True,
        "orphans": result,
        "counts": {table: item["count"] for table, item in result.items()},
        "snapshot_blobs_preserved": True,
    }


def migration_dry_run(connection: sqlite3.Connection) -> dict[str, Any]:
    """Plan pending transformations without executing target SQL or committing.

    This is a deterministic source-registry plan, not a claim that every future
    row rewrite has been simulated. The protected migration verifies preservation
    against its actual snapshot before accepting an upgraded profile.
    """
    from database.engine import FinanceDB

    schema = inspect_schema_contract(connection)
    financial = (
        financial_integrity_report(connection)
        if schema["ok"]
        else {
            "ok": False,
            "issues": ["Totais indisponíveis até revisão da estrutura SQL."],
            "totals": {},
        }
    )
    actual = _capture(connection)
    applied = set()
    if "schema_migrations" in actual:
        applied = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
    registry = FinanceDB.__new__(FinanceDB)._migration_steps()
    pending = [
        {"version": version, "name": name}
        for version, name, _ in registry
        if version not in applied
    ]
    target = trusted_schema_contracts()[FinanceDB.CURRENT_SCHEMA_VERSION]
    data_effects = {
        31: "Criar evidência de revisões de subscrições vazia; não alterar preços nem movimentos existentes.",
        32: "Criar pedidos e ligações de reembolsos vazios; não criar receitas nem alterar movimentos existentes.",
        20: "Completar ligações e aliases legados conhecidos; preservar montantes, datas e conteúdo original.",
        21: "Normalizar identidades cosméticas de contas; conflitos exigem revisão e não autorizam apagar dados.",
        24: "Completar identidades estáveis apenas quando vazias; identidades existentes ficam protegidas.",
        26: "Converter caminhos de anexos para chaves portáteis; preservar bytes e histórico de reconstituição.",
    }
    column_changes = {}
    for name, item in target.items():
        if (
            item["kind"] != "table"
            or name not in actual
            or actual[name]["kind"] != "table"
        ):
            continue
        old_columns = actual[name]["shape"]["columns"]
        new_columns = item["shape"]["columns"]
        added = sorted(set(new_columns) - set(old_columns))
        changed = sorted(
            column
            for column in set(old_columns) & set(new_columns)
            if old_columns[column] != new_columns[column]
        )
        if added or changed:
            column_changes[name] = {
                "added_columns": added,
                "changed_declarations": changed,
            }
    return {
        "dry_run": True,
        "mode": "source_registry_plan_only",
        "source_schema_version": schema["schema_version"],
        "target_schema_version": FinanceDB.CURRENT_SCHEMA_VERSION,
        "pending_migrations": pending,
        "would_block": not schema["ok"] or not financial["ok"],
        "issues": schema["issues"] + financial["issues"],
        "before_account_category_totals": financial["totals"],
        "structural_plan": {
            "missing_target_objects": sorted(set(target) - set(actual)),
            "legacy_objects_absent_from_target": sorted(set(actual) - set(target)),
            "column_changes": column_changes,
        },
        "data_transformations": [
            {"version": item["version"], "description": data_effects[item["version"]]}
            for item in pending
            if item["version"] in data_effects
        ],
        "preservation_policy": "Sem eliminação automática; snapshot verificado, invariantes e comparação de dados antes da aceitação.",
        "limitation": "Plano de operações; não prevê a contagem exata de linhas alteradas nem executa reescritas.",
    }
