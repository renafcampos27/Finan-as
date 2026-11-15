"""SEC-TOMBSTONE: deleting envelope+marker must not allow silent re-onboarding.

Attack (verified manually before this fix): delete both
``security_envelope.json`` and ``security_marker.json``; the app reported a
fresh profile, accepted a NEW attacker-chosen credential and exposed the
(live, unencrypted) database.  The fix records a tombstone row inside the
database itself — an attacker must delete the DB to remove it, which
destroys the very data they are trying to read.

Semantics (ADR-010-aligned):
- tombstone present + no envelope/marker  -> ProfileSecurityState.TAMPERED
- tombstone absent                        -> legacy/NEW as before
- explicit typed acknowledgment archives residue and clears the tombstone
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from services.startup_security import (
    CredentialPolicyError,
    ProfileSecurityState,
    SecurityEnvelopeError,
    StartupSecurityService,
    TAMPER_RECOVERY_CONFIRMATION,
)


@pytest.fixture()
def profile(tmp_path: Path) -> StartupSecurityService:
    """Service over a profile whose database already exists (like production:
    the tombstone needs a database to live in; main.py records it right
    after LiquidFinanceApp creates/migrates the database)."""
    database = tmp_path / "db.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.commit()
    finally:
        connection.close()
    return StartupSecurityService(database_file=database)


def _create_database_with_tombstone(
    database: Path,
    *,
    identity: str = "ident-1",
) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO settings(key, value) VALUES('security_tombstone', ?)",
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


def test_tamper_tombstone_is_written_on_onboarding(profile) -> None:
    """First-run configuration records the tombstone inside the database."""

    profile.configure_first_run("pin", "1357902468")
    assert profile._tamper_tombstone_present() is True
    assert profile.inspect_profile() == ProfileSecurityState.CONFIGURED

    connection = sqlite3.connect(profile._database_file())
    try:
        row = connection.execute(
            "SELECT value FROM settings WHERE key='security_tombstone'"
        ).fetchone()
    finally:
        connection.close()
    payload = json.loads(str(row[0]))
    assert payload["protected"] is True
    assert isinstance(payload["envelope_identity"], str)
    assert payload["envelope_identity"]


def test_tamper_tombstone_refresh_binds_new_envelope_identity(profile) -> None:
    """refresh_tamper_tombstone() re-binds after a new envelope exists."""

    profile.configure_first_run("pin", "1357902468")
    old_identity = json.loads(
        sqlite3.connect(profile._database_file())
        .execute(
            "SELECT value FROM settings WHERE key='security_tombstone'"
        )
        .fetchone()[0]
    )["envelope_identity"]

    # Simulate the recovery path: residue archived, tombstone cleared,
    # then a brand-new onboarding with a fresh envelope.
    profile.acknowledge_tamper_recovery(TAMPER_RECOVERY_CONFIRMATION)
    assert profile._tamper_tombstone_present() is False
    profile.configure_first_run("pin", "2468013579")
    profile.refresh_tamper_tombstone()

    new_identity = json.loads(
        sqlite3.connect(profile._database_file())
        .execute(
            "SELECT value FROM settings WHERE key='security_tombstone'"
        )
        .fetchone()[0]
    )["envelope_identity"]
    assert new_identity != old_identity
    assert profile.inspect_profile() == ProfileSecurityState.CONFIGURED


def test_deleted_envelope_and_marker_with_tombstone_reports_tampered(
    profile,
) -> None:
    """The exact reported attack now lands in TAMPERED, not NEW/LEGACY."""

    profile.configure_first_run("pin", "1357902468")
    # Simulate the attacker deleting both security files.
    profile.envelope_path.unlink()
    profile.marker_path.unlink()

    assert profile.inspect_profile() == ProfileSecurityState.TAMPERED


def test_tampered_profile_cannot_onboard_or_unlock(profile) -> None:
    """TAMPERED refuses onboarding and unlock (no bypass via the dialog)."""

    profile.configure_first_run("pin", "1357902468")
    profile.envelope_path.unlink()
    profile.marker_path.unlink()
    assert profile.inspect_profile() == ProfileSecurityState.TAMPERED

    with pytest.raises(SecurityEnvelopeError):
        profile.configure_first_run("pin", "9876543210")
    with pytest.raises(SecurityEnvelopeError):
        profile.unlock("pin", "1357902468")


def test_tamper_recovery_requires_exact_phrase(profile) -> None:
    """Wrong/empty confirmation is rejected; exact phrase archives residue."""

    profile.configure_first_run("pin", "1357902468")
    profile.envelope_path.unlink()
    profile.marker_path.unlink()

    with pytest.raises(CredentialPolicyError):
        profile.acknowledge_tamper_recovery("frase errada")
    with pytest.raises(CredentialPolicyError):
        profile.acknowledge_tamper_recovery("")
    assert profile.inspect_profile() == ProfileSecurityState.TAMPERED

    archive = profile.acknowledge_tamper_recovery(
        f"  {TAMPER_RECOVERY_CONFIRMATION}  "
    )
    assert archive.is_dir()
    # Leftover residue (none in this scenario) would be moved there; the
    # tombstone inside the DB is cleared and the profile can onboard again
    # (the orphan database without a tombstone reports LEGACY_UNPROTECTED).
    assert profile._tamper_tombstone_present() is False
    assert profile.inspect_profile() == ProfileSecurityState.LEGACY_UNPROTECTED


def test_tamper_recovery_archives_leftover_security_files(tmp_path: Path) -> None:
    """Partial residue (marker left behind) is preserved in the archive."""

    database = tmp_path / "db.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.commit()
    finally:
        connection.close()
    profile = StartupSecurityService(database_file=database)
    profile.configure_first_run("password", "SenhaForte123")
    profile.envelope_path.unlink()  # attacker deleted only the envelope
    # Tombstone alone must NOT report TAMPERED while the marker still
    # exists (MISSING_ENVELOPE already covers that case and keeps evidence).

    assert profile.inspect_profile() == ProfileSecurityState.MISSING_ENVELOPE

    # Full attack: now the marker goes too.
    profile.marker_path.unlink()
    assert profile.inspect_profile() == ProfileSecurityState.TAMPERED

    profile.acknowledge_tamper_recovery(TAMPER_RECOVERY_CONFIRMATION)
    archives = list(
        profile.configuration_dir.glob("recovery-archive-*")
    )
    assert archives, "expected a recovery archive directory"
    # No residue files existed in this scenario, but the archive exists and
    # the profile can onboard again (orphan DB -> LEGACY_UNPROTECTED).
    assert profile.inspect_profile() == ProfileSecurityState.LEGACY_UNPROTECTED


def test_database_without_tombstone_is_untouched(profile) -> None:
    """A database without a tombstone reports the legacy state, not NEW."""

    assert profile._tamper_tombstone_present() is False
    assert profile.inspect_profile() == ProfileSecurityState.LEGACY_UNPROTECTED


def test_onboarding_without_database_does_not_fail(tmp_path) -> None:
    """NEW profile: no DB yet — onboarding must not raise (regression:
    the live error `sqlite3.OperationalError: no such table: settings`
    surfaced in the app log when the tombstone write ran pre-DB)."""

    profile = StartupSecurityService(
        database_file=tmp_path / "fresh" / "db.sqlite3"
    )
    assert not profile._database_file().is_file()
    # Must not raise even though the database does not exist.
    profile.configure_first_run("pin", "1357902468")
    assert profile.envelope_path.is_file()
    assert profile._tamper_tombstone_present() is False
    # Once main creates the database and calls refresh, the tombstone lands.
    connection = sqlite3.connect(profile._database_file())
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.commit()
    finally:
        connection.close()
    profile.refresh_tamper_tombstone()
    assert profile._tamper_tombstone_present() is True
    assert profile.inspect_profile() == ProfileSecurityState.CONFIGURED


def test_tombstone_survives_database_reopen(profile) -> None:
    """The tombstone is durable state, not an in-memory flag."""

    profile.configure_first_run("pin", "1357902468")
    database = profile._database_file()
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE settings SET value=? WHERE key='security_tombstone'",
            (json.dumps({"protected": True, "envelope_identity": "x"}),),
        )
        connection.commit()
    finally:
        connection.close()
    profile.envelope_path.unlink()
    profile.marker_path.unlink()
    # New service instance = fresh process simulation.
    reopened = StartupSecurityService(database_file=database)
    assert reopened.inspect_profile() == ProfileSecurityState.TAMPERED
