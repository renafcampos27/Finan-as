"""Startup security service: PIN/passphrase gate around the whole app.

Responsibilities:
- inspect the profile security state (new / legacy / configured / broken);
- first-run onboarding (configure PIN and/or passphrase);
- unlock (verify a credential, unwrap the backup master key);
- credential rotation (rewrap, never regenerate, the master key);
- failed-attempt throttling data for the UI.

The UI layer never compares secrets, derives keys, opens SQLite or writes
security files — it only calls this service.

Threat-model note (must be communicated verbatim in the UI):
- The startup credential controls *application* access.
- The wrapped master key protects *portable ``.lgbak`` backups*.
- The active SQLite file is NOT encrypted by this mechanism; on Windows it
  is protected by account permissions / BitLocker.
"""

from __future__ import annotations

import hmac
import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from typing import Any

from utils.app_logging import log_exception

from services.backup_crypto import (
    BackupCryptoError,
    CredentialKind,
    CryptoFormatError,
    ENVELOPE_FILE_NAME,
    MASTER_KEY_BYTES,
    SECURITY_MARKER_FILE_NAME,
    SecurityEnvelope,
    build_envelope,
    new_kdf_parameters,
    read_envelope,
    replace_credential_unlock,
    unlock_with_credential,
    write_envelope_atomically,
    write_security_marker_atomically,
)
from utils.config import paths_for_database
from utils.output_security import secure_zero

LOCAL_PIN_MIN_DIGITS = 6
PORTABLE_BACKUP_PIN_MIN_DIGITS = 10
PASSWORD_MIN_CHARACTERS = 8

MAX_THROTTLE_SECONDS = 300

# Phrase the user must type exactly to acknowledge tamper recovery.
TAMPER_RECOVERY_CONFIRMATION = "ASSUMO A PERDA DE ACESSO AOS BACKUPS"


class StartupSecurityError(RuntimeError):
    """Base class for startup-security failures."""


class InvalidCredentialError(StartupSecurityError):
    """The provided credential does not unlock the profile."""


class CredentialPolicyError(StartupSecurityError):
    """The provided credential violates the documented policy."""


class SecurityEnvelopeError(StartupSecurityError):
    """The security envelope cannot be used (missing/corrupt/unsupported)."""


class UnsupportedSecurityFormatError(SecurityEnvelopeError):
    """The stored security format requires an application upgrade."""


class CredentialRotationError(StartupSecurityError):
    """Credential rotation failed and was rolled back."""


class SecurityRecoveryRequiredError(StartupSecurityError):
    """Security state requires manual recovery; startup must not proceed."""


class SecurityRecoveryStorageError(StartupSecurityError):
    """Leftover security residues could not be safely archived."""


class SecurityRecoveryIncompleteError(StartupSecurityError):
    """Tamper tombstone or security state remains active after recovery."""


class SecurityTamperDetectedError(StartupSecurityError):
    """Profile security files were tampered with or substituted."""


class ProfileSecurityState(StrEnum):
    NEW = "new"
    LEGACY_UNPROTECTED = "legacy_unprotected"
    CONFIGURED = "configured"
    MISSING_ENVELOPE = "missing_envelope"
    CORRUPT_ENVELOPE = "corrupt_envelope"
    TAMPERED = "tampered"
    RECOVERY_REQUIRED = "recovery_required"


class DatabaseProtectionInspection(StrEnum):
    CONFIRMED_PROTECTED = "confirmed_protected"
    CONFIRMED_LEGACY = "confirmed_legacy"
    INDETERMINATE = "indeterminate"
    ABSENT = "absent"


@dataclass(slots=True)
class SessionSecurityContext:
    """In-memory security session; owns the zeroizable master key.

    BUG-SEC-001-HEAP: the master key is kept in a zeroizable ``bytearray``
    and wiped by ``close()`` — explicit call, ``with`` block, or interpreter
    teardown via ``__del__``.  Documented Python limitation: immutable
    ``str``/``bytes`` copies of secrets (credential strings, transient
    ``bytes(master)`` snapshots handed to crypto backends) cannot be
    zeroized and remain a residual exposure outside this fix's scope.
    """

    profile_id: str
    credential_kind: str
    default_unlock_method: str
    backup_master_key: bytearray
    envelope_path: Path
    _closed: bool = field(default=False, repr=False)

    def close(self) -> None:
        if not self._closed:
            secure_zero(self.backup_master_key)
            self._closed = True

    def __enter__(self) -> "SessionSecurityContext":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __del__(self) -> None:  # best-effort zeroization
        try:
            self.close()
        except Exception:
            pass


# Active session registry (one per running application instance) and concurrency locks.
_SESSION_LOCK = threading.RLock()
_ROTATION_LOCK = threading.RLock()
_ACTIVE_SESSION: SessionSecurityContext | None = None


def set_active_session(session: SessionSecurityContext | None) -> None:
    """Own exactly one active session: zeroize any previous distinct one under lock."""

    global _ACTIVE_SESSION
    with _SESSION_LOCK:
        previous = _ACTIVE_SESSION
        _ACTIVE_SESSION = None
        if previous is not None and previous is not session:
            previous.close()
        _ACTIVE_SESSION = session


def get_active_session() -> SessionSecurityContext | None:
    """Return active session only if present and not already closed/zeroized."""

    with _SESSION_LOCK:
        if _ACTIVE_SESSION is not None and not _ACTIVE_SESSION._closed:
            return _ACTIVE_SESSION
        return None


def clear_active_session() -> None:
    global _ACTIVE_SESSION
    with _SESSION_LOCK:
        previous = _ACTIVE_SESSION
        _ACTIVE_SESSION = None
        if previous is not None:
            previous.close()


def retry_delay_seconds(failures: int) -> int:
    """Exponential backoff after repeated failures (0/0/0/2/4/8… capped)."""

    if failures < 3:
        return 0
    return min(MAX_THROTTLE_SECONDS, 2 ** (failures - 3))


# ---------------------------------------------------------------------------
# Credential policy (inspection only — never transform the secret)
# ---------------------------------------------------------------------------


def validate_credential(
    kind: str,
    secret: str,
    *,
    weak_pin_confirmed: bool = False,
) -> None:
    """Validate without transforming: no strip/lower/normalize/truncation."""

    value = str(secret)
    if kind == CredentialKind.PIN:
        if not value.isascii() or not value.isdigit():
            raise CredentialPolicyError("O PIN deve conter apenas dígitos (0-9).")
        if len(value) < LOCAL_PIN_MIN_DIGITS:
            raise CredentialPolicyError(
                f"O PIN deve ter pelo menos {LOCAL_PIN_MIN_DIGITS} dígitos."
            )
        if len(value) < PORTABLE_BACKUP_PIN_MIN_DIGITS and not weak_pin_confirmed:
            raise CredentialPolicyError(
                "Um PIN curto também protege os backups portáteis. Usa pelo "
                f"menos {PORTABLE_BACKUP_PIN_MIN_DIGITS} dígitos, prefere uma "
                "palavra-passe para os backups, ou confirma que aceitas o "
                "risco de um PIN curto."
            )
        return
    if kind == CredentialKind.PASSWORD:
        if len(value) < PASSWORD_MIN_CHARACTERS:
            raise CredentialPolicyError(
                "A palavra-passe deve ter pelo menos "
                f"{PASSWORD_MIN_CHARACTERS} caracteres."
            )
        return
    raise CredentialPolicyError(f"Tipo de credencial inválido: {kind!r}.")


def is_weak_portable_pin(kind: str, secret: str) -> bool:
    """True when a PIN below the portable threshold would be stored."""

    return (
        kind == CredentialKind.PIN and len(str(secret)) < PORTABLE_BACKUP_PIN_MIN_DIGITS
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StartupSecurityService:
    """Owns envelope lifecycle and credential verification (no UI)."""

    def __init__(
        self,
        *,
        database_file: Path | None = None,
        configuration_dir: Path | None = None,
        envelope_path: Path | None = None,
        marker_path: Path | None = None,
    ) -> None:
        self._paths = paths_for_database(database_file)
        self.configuration_dir = (
            Path(configuration_dir)
            if configuration_dir is not None
            else self._paths.configuration
        )
        self.envelope_path = (
            Path(envelope_path)
            if envelope_path is not None
            else self.configuration_dir / ENVELOPE_FILE_NAME
        )
        self.marker_path = (
            Path(marker_path)
            if marker_path is not None
            else self.configuration_dir / SECURITY_MARKER_FILE_NAME
        )
        self._failures = 0

    # -- profile inspection --------------------------------------------------

    def _database_file(self) -> Path:
        database = self._paths.database
        if database.is_file():
            return database
        if self._paths.uses_stable_profile:
            app_dir_database = Path(__file__).resolve().parent.parent / (
                "financas_pessoais.sqlite3"
            )
            if app_dir_database.is_file():
                return app_dir_database
        return database

    def _inspect_database_protection(self) -> DatabaseProtectionInspection:
        """Inspect the database to establish its protection history and schema validity.

        Distinguishes confirmed presence of a protection tombstone, confirmed
        absence in a recognized legacy schema, and indeterminate/failed inspection
        (unreadable, locked, corrupt, or unrecognizable schema).
        """
        database = self._database_file()
        if not database.is_file():
            return DatabaseProtectionInspection.ABSENT

        try:
            import sqlite3

            connection = sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0
            )
            try:
                cursor = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                tables = {str(row[0]) for row in cursor.fetchall()}
                known_app_tables = {
                    "transactions",
                    "categories",
                    "financial_accounts",
                    "holdings",
                    "subscriptions",
                    "settings",
                    "goals",
                    "schema_migrations",
                    "liabilities",
                }
                if tables and not tables.intersection(known_app_tables):
                    return DatabaseProtectionInspection.INDETERMINATE

                if "settings" in tables:
                    row = connection.execute(
                        "SELECT value FROM settings WHERE key='security_tombstone'"
                    ).fetchone()
                    if row and str(row[0] or "").strip():
                        return DatabaseProtectionInspection.CONFIRMED_PROTECTED
                    return DatabaseProtectionInspection.CONFIRMED_LEGACY
                return DatabaseProtectionInspection.CONFIRMED_LEGACY
            finally:
                connection.close()
        except Exception:
            return DatabaseProtectionInspection.INDETERMINATE

    def _tamper_tombstone_present(self) -> bool:
        """True when the live database carries the protection tombstone indicator.

        Note: the tombstone is an anti-sabotage indicator for accidental or
        uncoordinated file deletions, not database encryption or protection
        against an adversary capable of modifying or rewriting the plaintext
        SQLite database file.
        """
        return (
            self._inspect_database_protection()
            == DatabaseProtectionInspection.CONFIRMED_PROTECTED
        )

    def inspect_profile(self) -> ProfileSecurityState:
        """Classify the profile security state without ever crashing on I/O.

        BUG-SEC-06: every filesystem probe below (``is_file``, envelope and
        marker reads) can raise ``OSError`` under hostile storage conditions
        (denied permissions, antivirus locks, failing disks).  The startup
        gate must degrade to a safe state instead of crashing the app: an
        unreadable *existence probe* maps to RECOVERY_REQUIRED (the
        protection state cannot be trusted either way), while an unreadable
        *read* of an existing file keeps the existing CORRUPT_ENVELOPE
        mapping.
        """
        from services.backup_crypto import read_security_marker

        try:
            envelope_exists = self.envelope_path.is_file()
            marker_exists = self.marker_path.is_file()
        except OSError:
            log_exception(
                "Security files are unreadable (permissions/disk/antivirus); "
                "startup gate falls back to RECOVERY_REQUIRED"
            )
            return ProfileSecurityState.RECOVERY_REQUIRED
        if not envelope_exists and not marker_exists:
            inspection = self._inspect_database_protection()
            if inspection == DatabaseProtectionInspection.CONFIRMED_PROTECTED:
                return ProfileSecurityState.TAMPERED
            if inspection == DatabaseProtectionInspection.INDETERMINATE:
                return ProfileSecurityState.RECOVERY_REQUIRED
            if inspection == DatabaseProtectionInspection.CONFIRMED_LEGACY:
                return ProfileSecurityState.LEGACY_UNPROTECTED
            return ProfileSecurityState.NEW

        if envelope_exists:
            try:
                envelope = read_envelope(self.envelope_path)
            except CryptoFormatError, OSError, ValueError, TypeError:
                return ProfileSecurityState.CORRUPT_ENVELOPE
            # A missing, malformed or mismatched marker means the protection
            # state cannot be trusted: never present it as plain CONFIGURED.
            if not marker_exists:
                return ProfileSecurityState.CORRUPT_ENVELOPE
            try:
                marker = read_security_marker(self.marker_path)
            except CryptoFormatError, OSError, ValueError, TypeError:
                return ProfileSecurityState.CORRUPT_ENVELOPE
            if not marker_matches_envelope_static(marker, envelope):
                return ProfileSecurityState.CORRUPT_ENVELOPE

            # BUG-SEC-NEW-01 (C49): Cross-validate with database security tombstone if present.
            # Substituting envelope + marker with a foreign pair (even if they match each other)
            # must be detected as tampering when the live database already records a tombstone.
            raw_tombstone = self._read_tamper_tombstone_raw()
            if raw_tombstone:
                try:
                    from services.backup_crypto import envelope_identity

                    tombstone_data = json.loads(raw_tombstone)
                    expected_identity = tombstone_data.get("envelope_identity")
                    current_identity = envelope_identity(envelope)
                    if expected_identity and expected_identity != current_identity:
                        return ProfileSecurityState.TAMPERED
                except Exception:
                    return ProfileSecurityState.TAMPERED

            return ProfileSecurityState.CONFIGURED

        if marker_exists:
            # Deleting the envelope must never bypass security.
            return ProfileSecurityState.MISSING_ENVELOPE

        inspection = self._inspect_database_protection()
        if inspection == DatabaseProtectionInspection.INDETERMINATE:
            return ProfileSecurityState.RECOVERY_REQUIRED
        if inspection == DatabaseProtectionInspection.CONFIRMED_LEGACY:
            return ProfileSecurityState.LEGACY_UNPROTECTED
        return ProfileSecurityState.NEW

    def _write_tamper_tombstone(self, envelope: SecurityEnvelope) -> None:
        """Record inside the live DB that this profile is protected.

        Called after successful envelope writes (onboarding).  Best-effort:
        the marker file remains the primary tamper evidence; the tombstone
        is the second line of defence inside the data itself.
        """

        database = self._database_file()
        if not database.is_file():
            # NEW profile: the database is created later in startup; the
            # tombstone is recorded by refresh_tamper_tombstone() once
            # main has constructed the app (never fail onboarding here).
            return
        try:
            import sqlite3

            from services.backup_crypto import envelope_identity

            connection = sqlite3.connect(database, timeout=2.0)
            try:
                connection.execute(
                    "INSERT INTO settings(key, value) "
                    "VALUES('security_tombstone', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (
                        json.dumps(
                            {
                                "protected": True,
                                "envelope_identity": envelope_identity(envelope),
                                "recorded_at": datetime.now().isoformat(),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
        except Exception:
            log_exception("Failed to record the security tombstone")

    def acknowledge_tamper_recovery(self, confirmation: str) -> Path:
        """Explicitly acknowledge that protection was removed (user action).

        Requires typing the exact confirmation phrase, archives leftover
        security residue into ``recovery-archive-<ts>/`` (so any encrypted
        backups remain recoverable) and returns the archive path. The next
        inspect_profile() reports NEW or LEGACY_UNPROTECTED and onboarding can legitimately run.
        """

        if not hmac.compare_digest(
            str(confirmation).strip().encode("utf-8"),
            TAMPER_RECOVERY_CONFIRMATION.encode("utf-8"),
        ):
            raise CredentialPolicyError(
                "Frase de confirmação incorreta; recuperação cancelada."
            )

        residues = [p for p in (self.envelope_path, self.marker_path) if p.is_file()]
        archive: Path
        if residues:
            archive_name = "recovery-archive-" + datetime.now().strftime(
                "%Y%m%d-%H%M%S"
            )
            archive = self.configuration_dir / archive_name
            if archive.exists():
                archive = self.configuration_dir / (
                    archive_name + f"-{datetime.now().strftime('%f')}"
                )
            try:
                archive.mkdir(parents=True, exist_ok=True)
                for residue in residues:
                    target_file = archive / residue.name
                    if target_file.exists():
                        target_file = (
                            archive
                            / f"{residue.stem}-{datetime.now().strftime('%f')}{residue.suffix}"
                        )
                    shutil.move(str(residue), str(target_file))
            except OSError as exc:
                raise SecurityRecoveryStorageError(
                    f"Falha de armazenamento ao arquivar resíduos de segurança: {exc}"
                ) from exc
        else:
            existing_archives = sorted(
                self.configuration_dir.glob("recovery-archive-*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if existing_archives:
                archive = existing_archives[0]
            else:
                archive = self.configuration_dir / (
                    "recovery-archive-" + datetime.now().strftime("%Y%m%d-%H%M%S")
                )
                try:
                    archive.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise SecurityRecoveryStorageError(
                        f"Falha de armazenamento ao criar pasta de arquivo de recuperação: {exc}"
                    ) from exc

        try:
            self._clear_tamper_tombstone()
        except Exception as exc:
            log_exception("Failed to clear security tombstone during recovery")
            raise SecurityRecoveryIncompleteError(
                f"Falha ao limpar o registo de proteção da base de dados: {exc}"
            ) from exc

        if self._tamper_tombstone_present():
            raise SecurityRecoveryIncompleteError(
                "O registo de proteção da base de dados permanece ativo após tentativa de limpeza."
            )

        resulting_state = self.inspect_profile()
        if resulting_state not in {
            ProfileSecurityState.NEW,
            ProfileSecurityState.LEGACY_UNPROTECTED,
        }:
            raise SecurityRecoveryIncompleteError(
                f"Recuperação incompleta; perfil permanece no estado {resulting_state.value}."
            )

        return archive

    @property
    def tamper_recovery_confirmation_phrase(self) -> str:
        return TAMPER_RECOVERY_CONFIRMATION

    def _clear_tamper_tombstone(self) -> None:
        """Remove the tombstone after an explicit recovery acknowledgment."""

        database = self._database_file()
        if not database.is_file():
            return
        import sqlite3

        connection = sqlite3.connect(database, timeout=2.0)
        try:
            connection.execute("DELETE FROM settings WHERE key='security_tombstone'")
            connection.commit()
        finally:
            connection.close()

    def refresh_tamper_tombstone(self) -> None:
        """Bind the current envelope identity inside the live database.

        Called once per successful startup (after the database exists) and
        after credential rotation.  Best-effort by design: on a NEW profile
        where the database does not exist yet, this is a no-op and the
        tombstone is written by the next refresh once main creates it.
        """

        if not self._database_file().is_file():
            return
        envelope = self.load_envelope()
        raw_tombstone = self._read_tamper_tombstone_raw()
        if raw_tombstone:
            try:
                from services.backup_crypto import envelope_identity

                tombstone_data = json.loads(raw_tombstone)
                expected_identity = tombstone_data.get("envelope_identity")
                current_identity = envelope_identity(envelope)
                if expected_identity and expected_identity != current_identity:
                    raise SecurityTamperDetectedError(
                        "Identidade do envelope não coincide com a lápide registada na base de dados."
                    )
            except SecurityTamperDetectedError:
                raise
            except Exception:
                pass
        self._write_tamper_tombstone(envelope)

    def resync_tamper_tombstone_after_restore(self) -> None:
        """Re-bind the tombstone to the *current* envelope after a restore.

        BUG-SEC-04: a restored database carries the tombstone of the profile
        that produced the backup.  On a new machine (or after an envelope
        replacement) that identity no longer matches the live envelope, and
        the C49 cross-validation in ``inspect_profile()`` would then lock the
        user out of their own legitimately restored data.

        Unlike :meth:`refresh_tamper_tombstone` — whose tamper gate must
        stay strict (BUG-SEC-C49) — this method is only invoked by the
        restore flow right after the database file has been replaced, so
        re-graving the tombstone with the live envelope identity is the
        documented, intentional policy.  When no envelope exists the
        tombstone is cleared instead so the profile reports
        LEGACY_UNPROTECTED/NEW and onboarding can run.
        """

        database = self._database_file()
        if not database.is_file():
            return
        if not self.envelope_path.is_file():
            # No live envelope: the stale restored tombstone would force the
            # next startup into TAMPERED with no way back, so it is cleared
            # and the profile reports LEGACY_UNPROTECTED (onboarding can run).
            self._clear_tamper_tombstone()
            return
        try:
            envelope = self.load_envelope()
        except Exception as exc:
            # A corrupt envelope must NOT silently downgrade the tombstone;
            # the profile already reports CORRUPT_ENVELOPE and is blocked.
            log_exception(
                "Skipping tombstone resync after restore: envelope unreadable"
            )
            raise SecurityEnvelopeError(
                "Não foi possível confirmar o envelope de segurança após o restauro."
            ) from exc
        self._write_tamper_tombstone(envelope)

    def _read_tamper_tombstone_raw(self) -> str | None:
        """Read the raw security tombstone value from settings table, if present."""
        database = self._database_file()
        if not database.is_file():
            return None
        try:
            import sqlite3

            connection = sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0
            )
            try:
                row = connection.execute(
                    "SELECT value FROM settings WHERE key='security_tombstone'"
                ).fetchone()
                return str(row[0]) if row and row[0] is not None else None
            finally:
                connection.close()
        except Exception:
            return None

    def _restore_tamper_tombstone(self, raw_value: str | None) -> None:
        """Restore previous tombstone value in live database during rollback."""
        database = self._database_file()
        if not database.is_file():
            return
        try:
            import sqlite3

            connection = sqlite3.connect(database, timeout=2.0)
            try:
                if raw_value is None:
                    cursor = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
                    )
                    if cursor.fetchone():
                        connection.execute(
                            "DELETE FROM settings WHERE key='security_tombstone'"
                        )
                else:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS settings ("
                        "key TEXT PRIMARY KEY, "
                        "value TEXT"
                        ")"
                    )
                    connection.execute(
                        "INSERT INTO settings(key, value) VALUES('security_tombstone', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (raw_value,),
                    )
                connection.commit()
            finally:
                connection.close()
        except Exception:
            log_exception("Failed to restore security tombstone during rollback")
            raise

    def _create_marker_backup(self, marker_backup_path: Path) -> bool:
        """Durably copy the marker file for rollback."""
        if not self.marker_path.is_file():
            return False
        marker_backup_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker_backup_path.with_name(f".{marker_backup_path.name}.tmp")
        try:
            with (
                self.marker_path.open("rb") as source,
                temporary.open("wb") as target,
            ):
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, marker_backup_path)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def _legacy_database_exists(self) -> bool:
        return (
            self._inspect_database_protection()
            == DatabaseProtectionInspection.CONFIRMED_LEGACY
        )

    def load_envelope(self) -> SecurityEnvelope:
        try:
            return read_envelope(self.envelope_path)
        except CryptoFormatError as exc:
            raise SecurityEnvelopeError(str(exc)) from exc
        except OSError as exc:
            raise SecurityEnvelopeError(
                "Não foi possível ler o envelope de segurança."
            ) from exc

    # -- onboarding ------------------------------------------------------------

    def configure_first_run(
        self,
        kind: str,
        secret: str,
        *,
        extra_kind: str | None = None,
        extra_secret: str | None = None,
        weak_pin_confirmed: bool = False,
    ) -> SessionSecurityContext:
        """First-run setup: create master key + envelope, unlock the app."""

        profile_state = self.inspect_profile()
        if profile_state == ProfileSecurityState.RECOVERY_REQUIRED:
            raise SecurityRecoveryRequiredError(
                "O perfil de segurança requer recuperação manual antes de nova configuração."
            )
        if profile_state not in {
            ProfileSecurityState.NEW,
            ProfileSecurityState.LEGACY_UNPROTECTED,
        }:
            raise SecurityEnvelopeError(
                "Este perfil já tem proteção configurada; usa o desbloqueio."
            )
        credentials = self._validated_credentials(
            primary=(kind, secret),
            extra=(extra_kind, extra_secret),
            weak_pin_confirmed=weak_pin_confirmed,
        )
        master_key = bytearray(_random_master_key())
        try:
            try:
                envelope = build_envelope(
                    profile_id=_new_profile_id(),
                    default_unlock_method=kind,
                    master_key=master_key,
                    credentials=credentials,
                )
                write_envelope_atomically(self.envelope_path, envelope)
                self._write_security_marker(envelope)
                # LEGACY onboarding: the database already exists, so the
                # tombstone can be recorded immediately.  For a genuinely NEW
                # profile the database is created later in startup;
                # refresh_tamper_tombstone() is invoked by main after
                # LiquidFinanceApp is constructed.
                if self._database_file().is_file():
                    self._write_tamper_tombstone(envelope)
            except BackupCryptoError as exc:
                raise StartupSecurityError(str(exc)) from exc
        except BaseException:
            # BUG-SEC-001-HEAP: onboarding failed before the session took
            # ownership of the fresh master key; wipe it before propagating.
            secure_zero(master_key)
            raise
        return SessionSecurityContext(
            profile_id=envelope.profile_id,
            credential_kind=kind,
            default_unlock_method=envelope.default_unlock_method,
            backup_master_key=master_key,
            envelope_path=self.envelope_path,
        )

    def _validated_credentials(
        self,
        *,
        primary: tuple[str | None, str | None],
        extra: tuple[str | None, str | None],
        weak_pin_confirmed: bool,
    ) -> dict[str, bytes]:
        kind, secret = primary
        if kind not in {CredentialKind.PIN, CredentialKind.PASSWORD}:
            raise CredentialPolicyError(
                "Escolhe um PIN ou uma palavra-passe para desbloquear."
            )
        if not str(secret or ""):
            raise CredentialPolicyError("Introduz a credencial escolhida.")
        validate_credential(
            kind,
            str(secret),
            weak_pin_confirmed=weak_pin_confirmed,
        )
        credentials = {str(kind): str(secret).encode("utf-8")}
        extra_kind, extra_secret = extra
        if extra_kind is not None and str(extra_secret or ""):
            if extra_kind == kind:
                raise CredentialPolicyError(
                    "As duas credenciais têm de ser de tipos diferentes."
                )
            validate_credential(
                extra_kind,
                str(extra_secret),
                weak_pin_confirmed=weak_pin_confirmed,
            )
            credentials[str(extra_kind)] = str(extra_secret).encode("utf-8")
        return credentials

    def _write_security_marker(self, envelope: SecurityEnvelope) -> None:

        write_security_marker_atomically(self.marker_path, envelope)

    def verify_credential_matches(self, secret: str) -> None:
        """Raise InvalidCredentialError unless ``secret`` is a live credential.

        Used by the backup layer to guarantee an explicitly supplied password
        is never silently ignored when an LGBK2 session owns encryption.
        """

        envelope = self.load_envelope()
        try:
            _verify_current(envelope, secret)
        except BackupCryptoError as exc:
            raise InvalidCredentialError(
                "A credencial indicada não corresponde à segurança ativa."
            ) from exc

    def verify_marker_consistency(self) -> None:
        """Raise unless the marker exists, parses and binds this envelope.

        MISSING_ENVELOPE/CORRUPT_ENVELOPE states must never silently accept a
        protected data profile as new/unprotected.
        """

        from services.backup_crypto import (
            marker_matches_envelope,
            read_security_marker,
        )

        envelope = self.load_envelope()
        if not self.marker_path.is_file():
            raise SecurityEnvelopeError("O marcador de proteção da app foi removido.")
        try:
            marker = read_security_marker(self.marker_path)
        except CryptoFormatError as exc:
            raise SecurityEnvelopeError(str(exc)) from exc
        if not marker_matches_envelope(marker, envelope):
            raise SecurityEnvelopeError(
                "O marcador de proteção não corresponde ao envelope de segurança atual."
            )

    # -- unlock ----------------------------------------------------------------

    def unlock(self, kind: str, secret: str) -> SessionSecurityContext:
        """Verify one credential and return the unlocked session context."""

        if kind not in {CredentialKind.PIN, CredentialKind.PASSWORD}:
            raise InvalidCredentialError("Tipo de desbloqueio inválido.")
        envelope = self.load_envelope()
        unlock = envelope.unlocks.get(str(kind))
        if unlock is None:
            raise InvalidCredentialError(
                "Este perfil não tem desbloqueio por esse método."
            )
        try:
            master_key = unlock_with_credential(
                envelope.profile_id,
                str(kind),
                unlock,
                str(secret).encode("utf-8"),
            )
        except BackupCryptoError as exc:
            self.register_failure()
            raise InvalidCredentialError(
                "Não foi possível desbloquear. Verifica a credencial e tenta novamente."
            ) from exc
        self.reset_failures()
        try:
            return SessionSecurityContext(
                profile_id=envelope.profile_id,
                credential_kind=str(kind),
                default_unlock_method=envelope.default_unlock_method,
                backup_master_key=master_key,
                envelope_path=self.envelope_path,
            )
        except BaseException:
            # BUG-SEC-001-HEAP: the unwrapped master key never reached a
            # session owner; wipe it before propagating.
            secure_zero(master_key)
            raise

    # -- rotation --------------------------------------------------------------

    def change_credential(
        self,
        current_secret: str,
        new_kind: str,
        new_secret: str,
        *,
        weak_pin_confirmed: bool = False,
    ) -> None:
        """Rotate a credential transactionally, keeping the same master key.

        Every failure after the live envelope has been touched restores and
        revalidates the previous envelope before a typed error propagates.
        Pre-mutation failures (verification/policy) never touch the disk.
        """

        with _ROTATION_LOCK:
            envelope = self.load_envelope()
            backup_path = self.envelope_path.with_name(
                self.envelope_path.name + ".rotation-backup"
            )
            marker_backup_path = self.marker_path.with_name(
                self.marker_path.name + ".rotation-backup"
            )
            transition_path = self.envelope_path.parent / (
                self.envelope_path.name + ".rotation-tmp"
            )
            if any(
                path.exists()
                for path in (backup_path, marker_backup_path, transition_path)
            ):
                raise SecurityRecoveryRequiredError(
                    "Existem ficheiros de recuperação de uma rotação anterior. "
                    "É necessário recuperá-los antes de alterar a credencial."
                )
            mutated = False
            committed = False
            master_before: bytearray | None = None
            prior_marker_existed = False
            prior_tombstone: str | None = None
            # BUG-AUD-C120: transition directory for the atomic pair swap.
            transition: Path | None = None
            try:
                # 1. Verify the current credential before any mutation.
                try:
                    master_before = _master_key_of(envelope, current_secret)
                except (BackupCryptoError, InvalidCredentialError) as exc:
                    raise InvalidCredentialError(
                        "A credencial atual não está correta."
                    ) from exc
                # 2. Validate the new credential.
                if new_kind not in {
                    CredentialKind.PIN,
                    CredentialKind.PASSWORD,
                }:
                    raise CredentialPolicyError(
                        f"Tipo de credencial inválido: {new_kind!r}."
                    )
                validate_credential(
                    new_kind,
                    new_secret,
                    weak_pin_confirmed=weak_pin_confirmed,
                )
                # 3. Create and durably verify the rollback copy.
                try:
                    self._create_rotation_backup(backup_path)
                    prior_marker_existed = self._create_marker_backup(
                        marker_backup_path
                    )
                    prior_tombstone = self._read_tamper_tombstone_raw()
                except Exception:
                    backup_path.unlink(missing_ok=True)
                    marker_backup_path.unlink(missing_ok=True)
                    raise
                mutated = True

                # 4. Build the candidate and write it to a transition directory.
                # BUG-AUD-C120: the Argon2id KDF (0.5-1.5 s) between writing the
                # new envelope and recomputing the marker used to leave the two
                # files desynchronized on a crash, blocking later startups.  Both
                # files are now materialized in a transition subdirectory and
                # swapped in with two consecutive atomic renames.
                updated = replace_credential_unlock(
                    envelope,
                    kind=str(new_kind),
                    current_secret=current_secret.encode("utf-8"),
                    new_secret=new_secret.encode("utf-8"),
                )
                transition = transition_path
                transition.mkdir(parents=True, exist_ok=True)
                transition_envelope = transition / self.envelope_path.name
                transition_marker = transition / self.marker_path.name
                write_envelope_atomically(transition_envelope, updated)
                write_security_marker_atomically(transition_marker, updated)

                # 5. Reopen and verify the new credential + unchanged master key.
                reopened = read_envelope(transition_envelope)
                session_probe = StartupSecurityService(
                    database_file=None,
                    envelope_path=transition_envelope,
                    marker_path=transition_marker,
                )
                session = session_probe.unlock(str(new_kind), new_secret)
                try:
                    if not hmac.compare_digest(
                        session.backup_master_key,
                        master_before,
                    ):
                        raise CredentialRotationError(
                            "A rotação não confirmou a chave mestra."
                        )
                finally:
                    session.close()
                # 6. Two consecutive atomic renames publish the new pair.
                # BUG-R6-SEC-01: Publish envelope and marker files FIRST before updating
                # database tombstone, eliminating false-positive TAMPERED lockouts if os.replace fails.
                os.replace(transition_envelope, self.envelope_path)
                if transition_marker.exists():
                    os.replace(transition_marker, self.marker_path)
                else:
                    self._write_security_marker(reopened)
                self._write_tamper_tombstone(reopened)

                # 7. Verify committed state.
                if self.inspect_profile() != ProfileSecurityState.CONFIGURED:
                    raise CredentialRotationError(
                        "O perfil não ficou em estado CONFIGURED após rotação."
                    )
                self.verify_marker_consistency()
                committed = True

                # Committed state verified: clean up recovery material.
                shutil.rmtree(transition)
                backup_path.unlink(missing_ok=True)
                marker_backup_path.unlink(missing_ok=True)

            except SecurityRecoveryRequiredError:
                # Rollback itself failed; retain recovery material and propagate.
                raise
            except Exception as exc:
                if committed:
                    raise SecurityRecoveryStorageError(
                        "A nova credencial ficou confirmada, mas a limpeza dos "
                        "ficheiros de recuperação não terminou."
                    ) from exc
                if not mutated:
                    # Pre-mutation failures keep their precise typed category
                    # (InvalidCredentialError, CredentialPolicyError, …); nothing
                    # was written to disk yet.
                    if isinstance(exc, StartupSecurityError):
                        raise
                    raise CredentialRotationError(
                        "A alteração de credencial falhou antes de alterar o perfil."
                    ) from exc
                try:
                    self._rollback_rotation(
                        backup_path,
                        envelope,
                        marker_backup_path=marker_backup_path,
                        prior_marker_existed=prior_marker_existed,
                        prior_tombstone=prior_tombstone,
                        current_secret=current_secret,
                        master_before=master_before,
                    )
                except SecurityRecoveryRequiredError:
                    raise
                except Exception as roll_exc:
                    from utils.app_logging import log_exception

                    log_exception(
                        "CRITICAL: credential-rotation rollback failed; the security "
                        "envelope may be inconsistent and must be recovered manually."
                    )
                    raise SecurityRecoveryRequiredError(
                        "A recuperação após falha na rotação de credencial falhou. "
                        "Os ficheiros de recuperação foram preservados para intervenção manual."
                    ) from roll_exc
                backup_path.unlink(missing_ok=True)
                marker_backup_path.unlink(missing_ok=True)
                if transition is not None:
                    try:
                        shutil.rmtree(transition)
                    except FileNotFoundError:
                        pass
                    except OSError as cleanup_exc:
                        raise SecurityRecoveryStorageError(
                            "O estado anterior foi restaurado, mas ficaram ficheiros "
                            "temporários da rotação para recuperação manual."
                        ) from cleanup_exc
                raise CredentialRotationError(
                    "A alteração de credencial falhou; o estado anterior foi restaurado."
                ) from exc
            finally:
                if master_before is not None:
                    secure_zero(master_before)

    def _create_rotation_backup(self, backup_path: Path) -> None:
        """Durably copy the live envelope for rollback and verify the bytes."""

        backup_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = backup_path.with_name(f".{backup_path.name}.tmp")
        try:
            with (
                self.envelope_path.open("rb") as source,
                temporary.open("wb") as target,
            ):
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, backup_path)
            # Verify the rollback copy byte-for-byte before mutating anything.
            if backup_path.read_bytes() != self.envelope_path.read_bytes():
                raise SecurityEnvelopeError(
                    "A cópia de segurança da rotação não ficou íntegra."
                )
        finally:
            temporary.unlink(missing_ok=True)

    def _rollback_rotation(
        self,
        backup_path: Path,
        previous_envelope: SecurityEnvelope,
        *,
        marker_backup_path: Path | None = None,
        prior_marker_existed: bool = False,
        prior_tombstone: str | None = None,
        current_secret: str | None = None,
        master_before: bytearray | None = None,
    ) -> None:
        """Restore previous envelope, marker, and tombstone; log and raise on failure."""

        from utils.app_logging import log_exception

        try:
            if not backup_path.is_file():
                raise SecurityEnvelopeError(
                    "A cópia de rollback da rotação não existe."
                )
            restore_temporary = self.envelope_path.with_name(
                f".{self.envelope_path.name}.rollback-tmp"
            )
            try:
                with (
                    backup_path.open("rb") as source,
                    restore_temporary.open("wb") as target,
                ):
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(restore_temporary, self.envelope_path)
            finally:
                restore_temporary.unlink(missing_ok=True)

            restored = read_envelope(self.envelope_path)
            if restored.to_json_dict() != previous_envelope.to_json_dict():
                raise SecurityEnvelopeError(
                    "O envelope restaurado não coincide com o estado anterior."
                )

            # Restore marker
            if marker_backup_path is not None and marker_backup_path.is_file():
                marker_restore_tmp = self.marker_path.with_name(
                    f".{self.marker_path.name}.rollback-tmp"
                )
                try:
                    with (
                        marker_backup_path.open("rb") as source,
                        marker_restore_tmp.open("wb") as target,
                    ):
                        shutil.copyfileobj(source, target)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(marker_restore_tmp, self.marker_path)
                finally:
                    marker_restore_tmp.unlink(missing_ok=True)
            elif not prior_marker_existed:
                self.marker_path.unlink(missing_ok=True)

            # Restore tombstone
            self._restore_tamper_tombstone(prior_tombstone)

            # Re-verify restored state
            if self.marker_path.is_file():
                from services.backup_crypto import (
                    marker_matches_envelope,
                    read_security_marker,
                )

                marker = read_security_marker(self.marker_path)
                if not marker_matches_envelope(marker, restored):
                    raise SecurityEnvelopeError(
                        "O marcador restaurado não coincide com o envelope restaurado."
                    )

            if current_secret is not None:
                restored_master = _master_key_of(restored, current_secret)
                try:
                    if master_before is not None and not hmac.compare_digest(
                        restored_master, master_before
                    ):
                        raise SecurityEnvelopeError(
                            "A chave mestra do envelope restaurado não coincide com a original."
                        )
                finally:
                    secure_zero(restored_master)

            if self.inspect_profile() != ProfileSecurityState.CONFIGURED:
                raise SecurityEnvelopeError(
                    "O perfil restaurado não ficou em estado CONFIGURED."
                )

        except Exception as exc:
            log_exception(
                "CRITICAL: credential-rotation rollback failed; the security "
                "envelope may be inconsistent and must be recovered manually."
            )
            raise SecurityRecoveryRequiredError(
                "A recuperação após falha na rotação de credencial falhou. "
                "Os ficheiros de recuperação foram preservados para intervenção manual."
            ) from exc

    # -- UI adapters (called from the Liquid Glass dialog worker) ----------

    def validate_credential_for_ui(
        self,
        kind: str,
        secret: str,
        *,
        weak_pin_confirmed: bool = False,
    ) -> None:
        """Policy check without any persistence (dialog live validation)."""

        validate_credential(
            kind,
            secret,
            weak_pin_confirmed=weak_pin_confirmed,
        )

    def configure_first_run_ui(
        self,
        kind: str,
        secret: str,
        *,
        weak_pin_confirmed: bool = False,
    ) -> SessionSecurityContext:
        """Onboarding entry point for the dialog; sets the active session."""

        context = self.configure_first_run(
            kind,
            secret,
            weak_pin_confirmed=weak_pin_confirmed,
        )
        set_active_session(context)
        return context

    def unlock_ui(self, kind: str, secret: str) -> SessionSecurityContext:
        """Unlock entry point for the dialog; sets the active session."""

        context = self.unlock(kind, secret)
        set_active_session(context)
        return context

    # -- throttle ----------------------------------------------------------

    @property
    def failures(self) -> int:
        return self._failures

    def register_failure(self) -> int:
        self._failures += 1
        return self._failures

    def reset_failures(self) -> None:
        self._failures = 0

    def current_retry_delay(self) -> int:
        return retry_delay_seconds(self._failures)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _random_master_key() -> bytes:
    import os

    return os.urandom(MASTER_KEY_BYTES)


def _new_profile_id() -> str:
    import uuid

    return str(uuid.uuid4())


def marker_matches_envelope_static(
    marker: dict[str, Any],
    envelope: SecurityEnvelope,
) -> bool:
    """Module-level re-export so tests and callers avoid circular imports."""

    from services.backup_crypto import marker_matches_envelope

    return marker_matches_envelope(marker, envelope)


def _verify_current(envelope: SecurityEnvelope, current_secret: str) -> None:
    """Verify without keeping the unwrapped master key alive (BUG-SEC-001-HEAP)."""

    master_key = _master_key_of(envelope, current_secret)
    secure_zero(master_key)


def _master_key_of(envelope: SecurityEnvelope, current_secret: str) -> bytearray:
    from services.backup_crypto import _unwrap_with_any_slot

    return _unwrap_with_any_slot(envelope, current_secret.encode("utf-8"))


def ensure_kdf_ready() -> bool:
    """True when the preferred KDF (Argon2id) is available."""

    from services.backup_crypto import argon2id_available

    return argon2id_available()


def preferred_kdf_name() -> str:

    return str(new_kdf_parameters().name)


__all__ = [
    "CredentialPolicyError",
    "CredentialKind",
    "CredentialRotationError",
    "DatabaseProtectionInspection",
    "LOCAL_PIN_MIN_DIGITS",
    "InvalidCredentialError",
    "MAX_THROTTLE_SECONDS",
    "PASSWORD_MIN_CHARACTERS",
    "PORTABLE_BACKUP_PIN_MIN_DIGITS",
    "ProfileSecurityState",
    "SecurityEnvelopeError",
    "SecurityRecoveryIncompleteError",
    "SecurityRecoveryRequiredError",
    "SecurityRecoveryStorageError",
    "SessionSecurityContext",
    "StartupSecurityError",
    "StartupSecurityService",
    "TAMPER_RECOVERY_CONFIRMATION",
    "UnsupportedSecurityFormatError",
    "clear_active_session",
    "ensure_kdf_ready",
    "get_active_session",
    "is_weak_portable_pin",
    "preferred_kdf_name",
    "retry_delay_seconds",
    "set_active_session",
    "validate_credential",
]
