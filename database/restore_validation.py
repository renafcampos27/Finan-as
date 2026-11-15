"""Read-only compatibility checks before installing a restored database."""

from __future__ import annotations

import sqlite3


_BASELINE_TABLES = frozenset({"settings", "categories", "transactions"})
_BASELINE_COLUMNS = {
    "settings": {"key", "value"},
    "categories": {"name", "kind", "colour", "budget"},
    "transactions": {
        "id",
        "date",
        "description",
        "amount",
        "category",
        "account",
        "kind",
        "notes",
        "created_at",
    },
}
# A declaration of a migration is a capability claim. Only check persistent
# fields whose introduction is explicit; older schemas can still migrate.
_CAPABILITIES = (
    (
        24,
        {
            "transactions": {"category_id", "account_id"},
            "categories": {"stable_id", "identity_key"},
            "financial_accounts": {"stable_id", "account_key"},
        },
    ),
    (
        26,
        {
            "transactions": {"goal_id", "goal_name_snapshot"},
            "transaction_attachments": {"stored_path"},
        },
    ),
    (27, {"holdings": {"currency", "exchange_rate"}}),
    (29, {"holdings": {"acquisition_date", "jurisdiction"}}),
    (31, {"subscription_price_reviews": {"id", "subscription_id", "transaction_id", "evidence_key", "evidence_json", "status", "classification", "confirmed_period", "confirmed_currency", "match_confirmed"}}),
    (32, {
        "reimbursement_claims": {"id", "source_transaction_id", "expected_cents", "currency", "due_date"},
        "reimbursement_receipts": {"id", "claim_id", "receipt_transaction_id", "amount_cents"},
    }),
)


def validate_restore_compatibility(
    connection: sqlite3.Connection,
    supported_schema_version: int,
    *,
    required_tables: set[str] | frozenset[str] = _BASELINE_TABLES,
) -> None:
    """Reject unsupported/incomplete schema claims without semantic-version guesses.

    Early schema-less archives remain accepted by their historical column
    contract. Versioned profiles additionally have to meet their declared
    migration/capability contract. A future application label alone does not
    imply an incompatible database.
    """
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(set(required_tables) - tables)
    if missing:
        raise ValueError(
            f"O backup tem uma estrutura incompleta; faltam tabelas: {', '.join(missing)}."
        )
    version = 0
    if "schema_migrations" in tables:
        try:
            raw = [
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            ]
        except sqlite3.DatabaseError as exc:
            raise ValueError("A estrutura do registo de migrações é inválida.") from exc
        if not raw or any(type(item) is not int or item <= 0 for item in raw):
            raise ValueError(
                "O backup tem versões de esquema inválidas ou incompletas."
            )
        versions = set(raw)
        version = max(versions)
        if version > int(supported_schema_version):
            raise ValueError(
                f"O esquema {version} do backup é mais recente que a versão suportada ({supported_schema_version})."
            )
        if versions != set(range(1, version + 1)) or len(raw) != len(versions):
            raise ValueError(
                "A sequência de versões de esquema do backup está incompleta."
            )
    financial = _BASELINE_TABLES.issubset(tables) or bool(
        {"transactions", "categories"}.intersection(required_tables)
    )
    expected = dict(_BASELINE_COLUMNS) if financial else {}
    for introduced, capabilities in _CAPABILITIES:
        if expected and version >= introduced:
            for table, columns in capabilities.items():
                expected[table] = set(expected.get(table, ())) | columns
    for table, columns in expected.items():
        actual = {
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        absent = sorted(columns - actual)
        if absent:
            raise ValueError(
                f"O esquema declarado não tem a capacidade necessária em {table}; faltam colunas: {', '.join(absent)}."
            )
    if financial:
        from database.schema_contract import assert_schema_contract, financial_integrity_report

        assert_schema_contract(connection, allow_legacy=True)
        report = financial_integrity_report(connection)
        if not report["ok"]:
            raise ValueError("O backup tem invariantes financeiras inválidas: " + "; ".join(report["issues"]))
