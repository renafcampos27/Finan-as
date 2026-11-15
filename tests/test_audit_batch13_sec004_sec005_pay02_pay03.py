#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for audit batch 13:

- BUG-SEC-04 (P1): the ``security_tombstone`` restored inside a backup
  database binds the *source* profile's envelope identity.  After a
  legitimate restore (new machine / replaced envelope) the C49
  cross-validation would report TAMPERED and lock the user out.  The
  restore flow must re-sync the tombstone with the live envelope (or clear
  it when no envelope exists), while ``refresh_tamper_tombstone`` keeps its
  strict C49 tamper gate.  Credential-rotation rollback must also leave the
  tombstone byte-identical to the pre-rotation value.
- BUG-SEC-05 (P2): ``_derive_scrypt`` must reject a non-power-of-2 ``n``
  with the typed ``CryptoFormatError`` even when parameter validation is
  bypassed, and a missing ``argon2-cffi`` backend must propagate as a typed
  ``BackupCryptoError`` (never silently swallowed).
- BUG-PAY-02 (P2): anchors derived from stored dates that fall on the last
  day of a month mean "end of month" (universal day 31): a Feb 28 / Apr 30
  start must still land on Mar 31 / May 31 without mutating the stored
  anchor.  Explicit ``renewal_day``/``debit_day`` columns stay literal.
- BUG-PAY-03 (P2): scheduled-payment occurrences landing on a Saturday or
  Sunday are stored (and charged) on the next business day; the schedule
  anchor itself is never shifted.  Portuguese holidays are out of scope
  (documented limitation).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import FinanceDB  # noqa: E402
from database.repo import Repo  # noqa: E402
from services.backup import BackupService  # noqa: E402
from services.backup_crypto import (  # noqa: E402
    BackupCryptoError,
    CryptoFormatError,
    KdfParameters,
    _derive_scrypt,
    derive_key,
)
from services.change_journal import ChangeJournalService  # noqa: E402
from services.payments import (  # noqa: E402
    PaymentsService,
    next_business_day,
)
from services.startup_security import (  # noqa: E402
    CredentialRotationError,
    ProfileSecurityState,
    SecurityTamperDetectedError,
    StartupSecurityService,
)


# ---------------------------------------------------------------------------
# BUG-SEC-04: tombstone re-sync after restore
# ---------------------------------------------------------------------------


def _create_settings_table(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.commit()
    finally:
        connection.close()


def _make_database_with_tombstone(database: Path, identity: str) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO settings(key, value) "
            "VALUES('security_tombstone', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                json.dumps(
                    {
                        "protected": True,
                        "envelope_identity": identity,
                        "recorded_at": "2026-09-03T00:00:00",
                    }
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _read_tombstone_identity(database: Path) -> str | None:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT value FROM settings WHERE key='security_tombstone'"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return str(json.loads(str(row[0])).get("envelope_identity"))


def _read_tombstone_raw(database: Path) -> str | None:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT value FROM settings WHERE key='security_tombstone'"
        ).fetchone()
    finally:
        connection.close()
    return str(row[0]) if row else None


def test_sec004_full_restore_flow_resyncs_tombstone(tmp_path: Path) -> None:
    """End-to-end: a complete-archive restore re-binds the tombstone.

    Simulates the production sequence: profile A creates a backup (its
    tombstone identity is baked into the backup DB), the live envelope is
    later replaced (profile B), and profile A's backup is restored - the
    restore-flow re-sync must leave the gate CONFIGURED, not TAMPERED.
    """

    from services.backup_crypto import envelope_identity
    from services.startup_security import TAMPER_RECOVERY_CONFIRMATION

    data_root = tmp_path / "dados"
    database = data_root / "financas_pessoais.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)

    # --- Profile A: onboard and produce a complete backup ZIP. ---
    service_a = StartupSecurityService(database_file=database)
    service_a.configure_first_run("pin", "1357902468")
    identity_a = envelope_identity(service_a.load_envelope())

    repo = Repo(FinanceDB(database))
    repo.db.seed_if_empty()
    repo.db.conn.commit()
    attachments = data_root / "anexos"
    history = data_root / "backups" / "historico_alteracoes"
    attachments.mkdir(parents=True, exist_ok=True)
    history.mkdir(parents=True, exist_ok=True)
    preferences = data_root / "configuracao" / "backup_preferences.json"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_text(
        json.dumps({"backup_keep": 10, "backup_encryption_enabled": "0"}),
        encoding="utf-8",
    )
    backup_service = BackupService(
        repo,
        database_file=database,
        backup_directory=data_root / "backups",
        attachments_directory=attachments,
        history_directory=history,
        preferences_file=preferences,
    )
    backup_service._create_complete_zip(
        tmp_path / "backup_perfil_A.zip",
        purpose="manual_backup",
    )
    repo.db.close()

    # --- Profile B: replace the envelope (new machine / new identity). ---
    service_b = StartupSecurityService(database_file=database)
    service_b.acknowledge_tamper_recovery(TAMPER_RECOVERY_CONFIRMATION)
    service_b.configure_first_run("pin", "2468013579")
    assert envelope_identity(service_b.load_envelope()) != identity_a

    # --- Restore profile A's backup over profile B's database. ---
    # A raw DB replacement brings the source profile's tombstone back;
    # exactly what restore_backup publishes before calling the re-sync.
    _make_database_with_tombstone(database, identity_a)
    restored_service = StartupSecurityService(database_file=database)
    assert restored_service.inspect_profile() == ProfileSecurityState.TAMPERED

    BackupService._resync_security_tombstone(database)

    # The gate is unblocked and the tombstone is now bound to the *live*
    # envelope (profile B), not to the backup's source identity.
    assert restored_service.inspect_profile() == ProfileSecurityState.CONFIGURED
    assert _read_tombstone_identity(database) == (
        envelope_identity(service_b.load_envelope())
    )


def test_sec004_resync_rebinds_restored_tombstone_to_live_envelope(
    tmp_path: Path,
) -> None:
    """A restored DB with a foreign tombstone identity must be re-bound."""

    database = tmp_path / "financas_pessoais.sqlite3"
    service = StartupSecurityService(database_file=database)
    # Onboard FIRST (writes the live tombstone), then simulate the restore
    # by overwriting the tombstone with the source profile's identity.
    service.configure_first_run("pin", "1357902468")
    _make_database_with_tombstone(database, "ident-source-machine")

    # Pre-condition (the bug): the restored tombstone points at the old
    # identity, so the startup gate reports TAMPERED and locks the user out.
    assert _read_tombstone_identity(database) == "ident-source-machine"
    assert service.inspect_profile() == ProfileSecurityState.TAMPERED

    # Restore-flow re-sync: re-grave the tombstone with the live envelope.
    service.resync_tamper_tombstone_after_restore()

    live_identity = _read_tombstone_identity(database)
    assert live_identity is not None
    assert live_identity != "ident-source-machine"
    assert service.inspect_profile() == ProfileSecurityState.CONFIGURED


def test_sec004_resync_clears_stale_tombstone_without_envelope(
    tmp_path: Path,
) -> None:
    """No live envelope -> the stale restored tombstone is cleared."""

    database = tmp_path / "financas_pessoais.sqlite3"
    _make_database_with_tombstone(database, "ident-source-machine")

    service = StartupSecurityService(database_file=database)
    assert service.envelope_path.is_file() is False

    service.resync_tamper_tombstone_after_restore()

    assert _read_tombstone_identity(database) is None
    assert service.inspect_profile() == ProfileSecurityState.LEGACY_UNPROTECTED


def test_sec004_refresh_tamper_gate_stays_strict(tmp_path: Path) -> None:
    """C49 semantics are preserved: refresh must still raise on mismatch."""

    database = tmp_path / "financas_pessoais.sqlite3"
    service = StartupSecurityService(database_file=database)
    service.configure_first_run("pin", "1357902468")
    _make_database_with_tombstone(database, "ident-source-machine")

    with pytest.raises(SecurityTamperDetectedError):
        service.refresh_tamper_tombstone()
    # And inspect_profile keeps reporting TAMPERED (no silent overwrite).
    assert service.inspect_profile() == ProfileSecurityState.TAMPERED


def test_sec004_resync_noop_without_database(tmp_path: Path) -> None:
    """No database file -> the re-sync is a no-op (NEW profile startup)."""

    service = StartupSecurityService(
        database_file=tmp_path / "missing" / "db.sqlite3"
    )
    service.resync_tamper_tombstone_after_restore()  # must not raise


def test_sec004_backup_service_resync_helper_is_best_effort(
    tmp_path: Path,
) -> None:
    """The backup-layer helper never raises, even with a broken profile."""

    database = tmp_path / "financas_pessoais.sqlite3"
    _make_database_with_tombstone(database, "ident-source-machine")
    # Corrupt the settings table so the tombstone write fails inside the
    # service; the helper must swallow and log, never propagate.
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE settings")
        connection.commit()
    finally:
        connection.close()

    BackupService._resync_security_tombstone(database)  # must not raise


def test_sec004_rotation_rollback_restores_prior_tombstone(
    tmp_path: Path,
) -> None:
    """A failed rotation restores the tombstone byte-for-byte."""

    database = tmp_path / "financas_pessoais.sqlite3"
    service = StartupSecurityService(database_file=database)
    _create_settings_table(database)
    service.configure_first_run("pin", "1357902468")
    before_identity = _read_tombstone_identity(database)
    assert before_identity
    before_raw = _read_tombstone_raw(database)
    assert before_raw

    # Force a failure after the envelope has been mutated: the step-5
    # master-key confirmation is sabotaged (the unlocked session reports a
    # wrong master key) so the rotation must roll back, restoring the
    # pre-rotation tombstone byte-for-byte.  The rollback path itself uses
    # _master_key_of() and must remain untouched.
    # BUG-AUD-C120: step 5 now reopens the candidate through a fresh
    # ``session_probe`` instance bound to the transition directory, so
    # instance-level patches on ``service`` are dead.  Patch the class-level
    # ``unlock`` for the duration of the rotation instead; the final
    # post-rollback unlock below runs after the patch context exits.
    real_unlock = StartupSecurityService.unlock

    def _wrong_unlock(self: StartupSecurityService, kind: str, secret: str) -> object:
        context = real_unlock(self, kind, secret)
        context.backup_master_key = bytearray(b"\x00" * 32)
        return context

    with pytest.raises(CredentialRotationError):
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(StartupSecurityService, "unlock", _wrong_unlock)
            service.change_credential("1357902468", "password", "nova senha forte")

    after_identity = _read_tombstone_identity(database)
    assert after_identity == before_identity
    assert _read_tombstone_raw(database) == before_raw
    assert service.inspect_profile() == ProfileSecurityState.CONFIGURED
    # The old credential still unlocks the rolled-back envelope.
    session = service.unlock("pin", "1357902468")
    session.close()


# ---------------------------------------------------------------------------
# BUG-SEC-05: scrypt power-of-2 validation + argon2id error propagation
# ---------------------------------------------------------------------------


def _scrypt_params(memory_kib: int) -> KdfParameters:
    return KdfParameters(
        name="scrypt",
        salt=b"\x01" * 16,
        memory_kib=memory_kib,
        iterations=1,
        lanes=1,
        length=32,
    )


def test_sec005_derive_scrypt_rejects_non_power_of_two_directly() -> None:
    """_derive_scrypt itself must raise CryptoFormatError for bad ``n``."""

    # 16 MiB + 1 KiB -> n = 32770, not a power of two.
    with pytest.raises(CryptoFormatError, match="pot"):
        _derive_scrypt(_scrypt_params(16 * 1024 + 1), b"secret")


def test_sec005_derive_key_scrypt_rejects_non_power_of_two() -> None:
    """Full path: derive_key must reject before touching the backend."""

    with pytest.raises(CryptoFormatError, match="pot"):
        derive_key(_scrypt_params(16 * 1024 + 1), b"secret")


def test_sec005_derive_scrypt_valid_power_of_two_still_works() -> None:
    """The smallest valid scrypt configuration keeps deriving."""

    derived = _derive_scrypt(_scrypt_params(16 * 1024), b"secret")
    assert len(derived) == 32
    assert any(derived)


def test_sec005_argon2id_missing_dependency_propagates_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing argon2-cffi backend must surface as BackupCryptoError."""

    import builtins

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: object, **kwargs: object):
        if name == "argon2.low_level" or name == "argon2":
            raise ImportError("No module named 'argon2'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    params = KdfParameters(
        name="argon2id",
        salt=b"\x02" * 16,
        memory_kib=16 * 1024,
        iterations=1,
        lanes=1,
        length=32,
    )
    with pytest.raises(BackupCryptoError, match="argon2-cffi"):
        derive_key(params, b"secret")


# ---------------------------------------------------------------------------
# BUG-PAY-02: end-of-month anchor intent
# ---------------------------------------------------------------------------


def test_pay02_anchor_from_february_28_start_recovers_day31() -> None:
    """A Feb 28 start means end-of-month: March must land on 31."""

    row = {
        "id": 1,
        "name": "Renda",
        "start_date": "2026-02-28",
        "debit_day": 0,  # absent -> anchor derived from start_date
        "active": 1,
        "months_paid": 0,
    }
    assert PaymentsService.due_date_for_payment(row, 0) == date(2026, 2, 28)
    assert PaymentsService.due_date_for_payment(row, 1) == date(2026, 3, 31)
    assert PaymentsService.due_date_for_payment(row, 2) == date(2026, 4, 30)


def test_pay02_anchor_from_april_30_start_recovers_day31() -> None:
    """An Apr 30 start means end-of-month: May must land on 31."""

    row = {
        "id": 2,
        "name": "Seguro",
        "start_date": "2026-04-30",
        "active": 1,
        "months_paid": 0,
    }
    assert PaymentsService.due_date_for_payment(row, 0) == date(2026, 4, 30)
    assert PaymentsService.due_date_for_payment(row, 1) == date(2026, 5, 31)
    assert PaymentsService.due_date_for_payment(row, 2) == date(2026, 6, 30)


def test_pay02_explicit_debit_day_stays_literal() -> None:
    """An explicit debit_day=15 must never be re-interpreted."""

    row = {
        "id": 3,
        "name": "Streaming",
        "start_date": "2026-04-30",
        "debit_day": 15,
        "active": 1,
        "months_paid": 0,
    }
    assert PaymentsService.due_date_for_payment(row, 0) == date(2026, 4, 15)
    assert PaymentsService.due_date_for_payment(row, 1) == date(2026, 5, 15)


def test_pay02_explicit_renewal_day_31_stays_literal() -> None:
    """RECUR-01 behaviour is preserved for explicit renewal_day anchors."""

    row = {
        "id": 4,
        "name": "Renda 31",
        "start_date": "2026-01-31",
        "debit_day": 31,
        "active": 1,
        "months_paid": 0,
    }
    dues = [PaymentsService.due_date_for_payment(row, i) for i in range(4)]
    assert dues == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]


def test_pay02_stored_anchor_never_mutated() -> None:
    """The clamp is per-occurrence: the stored start date stays day 28."""

    row = {
        "id": 5,
        "name": "Net",
        "start_date": "2026-02-28",
        "active": 1,
        "months_paid": 0,
    }
    for index in range(6):
        PaymentsService.due_date_for_payment(row, index)
    assert str(row["start_date"]) == "2026-02-28"


def test_pay02_subscription_scheduled_due_uses_eom_intent() -> None:
    """subscription_scheduled_due derives the EOM anchor from last_used."""

    row = {
        "id": 6,
        "name": "Net fibra",
        "amount": 45.0,
        "period": "Mensal",
        "start_date": "2026-02-28",
        "last_used_date": "2026-02-28",
        "next_renewal_date": "",
    }
    reference = date(2026, 3, 15)
    assert PaymentsService.subscription_scheduled_due(row, reference) == date(
        2026, 3, 31
    )


# ---------------------------------------------------------------------------
# BUG-PAY-03: weekend adjustment at charge creation
# ---------------------------------------------------------------------------


def test_pay03_next_business_day_skips_weekend() -> None:
    assert next_business_day(date(2026, 3, 7)) == date(2026, 3, 9)  # Sat -> Mon
    assert next_business_day(date(2026, 3, 8)) == date(2026, 3, 9)  # Sun -> Mon
    assert next_business_day(date(2026, 3, 6)) == date(2026, 3, 6)  # Fri stays


@pytest.fixture
def payments_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FINANCAS_PESSOAIS_DEMO_DATA", raising=False)
    db = FinanceDB(tmp_path / "batch13_pay03.sqlite3")
    repo = Repo(db)
    repo.db.seed_if_empty()
    for table in ("transaction_splits", "transactions", "scheduled_payments"):
        repo.db.conn.execute(f"DELETE FROM {table}")
    repo.db.conn.commit()
    journal = ChangeJournalService(repo)
    payments = PaymentsService(repo, journal)
    try:
        yield repo, payments, db
    finally:
        try:
            db.conn.close()
        except Exception:
            pass


def test_pay03_weekend_due_dates_shift_to_next_business_day(
    payments_env,
) -> None:
    """Occurrences on Sat/Sun are stored and charged on the next Monday."""

    repo, payments, db = payments_env
    # 2026-02-28 is a Saturday; 2026-03-01 is a Sunday.
    scheduled_id = repo.add_scheduled_payment(
        "Renda fim de semana",
        "Casa e contas",
        "",
        50.0,
        "Conta principal",
        "Débito direto",
        "2026-02-28",
        28,
        2,
    )
    created = payments.generate_due_scheduled_payments(date(2026, 4, 30))
    assert created == 2

    rows = db.conn.execute(
        "SELECT due_date, transaction_id FROM scheduled_payment_occurrences "
        "WHERE scheduled_id=? ORDER BY due_date",
        (scheduled_id,),
    ).fetchall()
    assert [(str(r[0]), r[1] is not None) for r in rows] == [
        ("2026-03-02", True),  # Feb 28 (Sat) -> Mon Mar 2
        ("2026-03-30", True),  # Mar 28 (Sat) -> Mon Mar 30
    ]
    # No movement is ever dated on a weekend.
    weekend = db.conn.execute(
        "SELECT COUNT(*) FROM transactions t "
        "JOIN scheduled_payment_occurrences o ON o.transaction_id=t.id "
        "WHERE o.scheduled_id=? "
        "AND (strftime('%w', t.date) IN ('0','6'))",
        (scheduled_id,),
    ).fetchone()[0]
    assert weekend == 0


def test_pay03_weekday_due_dates_unchanged(payments_env) -> None:
    """Weekday schedules keep their exact due dates (no off-by-one)."""

    repo, payments, db = payments_env
    # 2026-04-15 (Wed) and 2026-05-15 (Fri) are both business days.
    scheduled_id = repo.add_scheduled_payment(
        "Streaming",
        "Assinaturas",
        "",
        15.0,
        "Conta principal",
        "Cartão",
        "2026-04-15",
        15,
        2,
    )
    created = payments.generate_due_scheduled_payments(date(2026, 6, 30))
    assert created == 2
    rows = db.conn.execute(
        "SELECT due_date FROM scheduled_payment_occurrences "
        "WHERE scheduled_id=? ORDER BY due_date",
        (scheduled_id,),
    ).fetchall()
    assert [str(r[0]) for r in rows] == ["2026-04-15", "2026-05-15"]


def test_pay03_generation_is_idempotent_after_weekend_shift(
    payments_env,
) -> None:
    """Re-running generation must not duplicate shifted occurrences."""

    repo, payments, _db = payments_env
    repo.add_scheduled_payment(
        "Renda fim de semana",
        "Casa e contas",
        "",
        50.0,
        "Conta principal",
        "Débito direto",
        "2026-02-28",
        28,
        2,
    )
    first = payments.generate_due_scheduled_payments(date(2026, 4, 30))
    assert first == 2
    second = payments.generate_due_scheduled_payments(date(2026, 4, 30))
    assert second == 0


def test_pay03_anchor_preserved_for_future_occurrences(payments_env) -> None:
    """The weekend shift never feeds back into the schedule anchor."""

    repo, payments, db = payments_env
    # Anchor day 28; Feb 2026 occurrence shifts Sat 28 -> Mon Mar 2, but the
    # March occurrence must still be computed from the anchor (Sat Mar 28 ->
    # Mon Mar 30), not from the shifted previous occurrence.
    repo.add_scheduled_payment(
        "Renda dia 28",
        "Casa e contas",
        "",
        50.0,
        "Conta principal",
        "Débito direto",
        "2026-02-28",
        28,
        3,
    )
    payments.generate_due_scheduled_payments(date(2026, 5, 31))
    rows = db.conn.execute(
        "SELECT due_date FROM scheduled_payment_occurrences ORDER BY due_date"
    ).fetchall()
    assert [str(r[0]) for r in rows] == [
        "2026-03-02",
        "2026-03-30",
        "2026-04-28",  # Tuesday: no shift
    ]
