"""Security settings controller/ViewModel bridging Settings UI and services.

Owns all envelope/backup-security coordination so the Settings page never
compares credentials, derives keys, opens SQLite or writes security files.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from services.backup import BackupService
from services.backup_crypto import (
    SecurityEnvelope,
)
from services.startup_security import (
    CredentialPolicyError,
    CredentialRotationError,
    InvalidCredentialError,
    ProfileSecurityState,
    SecurityEnvelopeError,
    StartupSecurityError,
)


class SecurityServiceLike(Protocol):
    """Minimal structural type of the startup-security dependency."""

    def inspect_profile(self) -> ProfileSecurityState: ...

    def load_envelope(self) -> SecurityEnvelope: ...

    def verify_credential_matches(self, secret: str) -> None: ...

    def change_credential(
        self,
        current_secret: str,
        new_kind: str,
        new_secret: str,
        *,
        weak_pin_confirmed: bool = False,
    ) -> None: ...


class BackupVerificationStatus(str, Enum):
    """Typed verification status for backup archives."""

    NO_BACKUP = "no_backup"
    RECORDED_PREVIOUSLY = "recorded_previously"
    CURRENT_VERIFIED = "current_verified"
    VERIFICATION_FAILED = "verification_failed"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True, frozen=True)
class MethodState:
    """One configured unlock method as presented by the UI."""

    kind: str
    configured: bool
    is_default: bool


@dataclass(slots=True, frozen=True)
class SecurityStatus:
    """Aggregated security status for the Settings cards."""

    state: str
    methods: tuple[MethodState, ...]
    default_method: str
    format_active: str
    latest_backup: str
    verified: bool
    detail: str = ""
    verification_status: BackupVerificationStatus = BackupVerificationStatus.NO_BACKUP
    configured_format: str = "LGBK2"
    latest_format: str = "nenhum"


@dataclass(slots=True, frozen=True)
class CredentialChangeResult:
    """Typed outcome of a credential change attempt."""

    ok: bool
    error_category: str = ""
    message: str = ""
    method: str = ""


class SecuritySettingsController:
    """Coordinate security status and credential changes for Settings."""

    def __init__(
        self,
        *,
        security_service: SecurityServiceLike,
        backup_service: BackupService,
    ) -> None:
        self._security = security_service
        self._backup = backup_service

    # -- status -------------------------------------------------------------

    def security_status(
        self,
        *,
        cached_verification: dict[str, Any] | None = None,
        cached_fingerprint: tuple[str, int, int] | None = None,
    ) -> SecurityStatus:
        """Build the aggregated status shown by the security cards.

        BUG-SEC-06: this runs on every Settings page render; a failing
        profile inspection must degrade to the recovery card, never raise
        into the UI thread.
        """

        try:
            state = self._security.inspect_profile()
        except Exception:
            from utils.app_logging import log_exception

            log_exception("Security profile inspection failed; degrading status")
            state = ProfileSecurityState.RECOVERY_REQUIRED
        methods: tuple[MethodState, ...] = ()
        default_method = ""
        detail = ""
        if state == ProfileSecurityState.CONFIGURED:
            try:
                envelope = self._security.load_envelope()
                default_method = envelope.default_unlock_method
                methods = tuple(
                    MethodState(
                        kind=kind,
                        configured=True,
                        is_default=kind == default_method,
                    )
                    for kind in sorted(envelope.unlocks)
                )
            except SecurityEnvelopeError as exc:
                detail = str(exc)
                state = ProfileSecurityState.CORRUPT_ENVELOPE

        latest = self._backup.latest_complete_backup()
        latest_name = latest.name if latest is not None else ""

        # Configured output format
        configured_format = (
            "LGBK2"
            if state == ProfileSecurityState.CONFIGURED
            else "ZIP (não cifrado)"
        )
        if hasattr(self._backup, "configured_backup_format") and callable(
            self._backup.configured_backup_format
        ):
            try:
                configured_format = self._backup.configured_backup_format()
            except Exception:
                pass

        # Latest archive format detection with safe error handling
        if latest is None:
            latest_format = "nenhum"
        else:
            if hasattr(self._backup, "detect_archive_format") and callable(
                self._backup.detect_archive_format
            ):
                latest_format = self._backup.detect_archive_format(latest)
            else:
                try:
                    if not latest.is_file():
                        latest_format = "nenhum"
                    else:
                        with latest.open("rb") as handle:
                            magic = handle.read(5)
                        if magic == b"LGBK2":
                            latest_format = "LGBK2"
                        elif magic == b"LGBK1":
                            latest_format = "LGBK1"
                        elif magic.startswith(b"PK"):
                            latest_format = "ZIP"
                        elif len(magic) == 0:
                            latest_format = "truncated"
                        else:
                            latest_format = "unknown"
                except OSError as exc:
                    latest_format = "unreadable"
                    if not detail:
                        detail = str(exc)

        # Verification status determination
        verified = False
        if latest is None or latest_format == "nenhum":
            verification_status = BackupVerificationStatus.NO_BACKUP
        elif latest_format == "unreadable":
            verification_status = BackupVerificationStatus.UNAVAILABLE
            if not detail:
                detail = "Ficheiro de backup inacessível ou sem permissão de leitura."
        elif latest_format in {"truncated", "unknown"}:
            verification_status = BackupVerificationStatus.UNAVAILABLE
            if not detail:
                detail = f"Formato de arquivo inválido ({latest_format})."
        else:
            # Check cached async verification matching current file identity
            current_fp = None
            if hasattr(self._backup, "latest_backup_fingerprint") and callable(
                self._backup.latest_backup_fingerprint
            ):
                current_fp = self._backup.latest_backup_fingerprint()

            if (
                cached_verification is not None
                and cached_fingerprint is not None
                and current_fp is not None
                and cached_fingerprint == current_fp
            ):
                if bool(cached_verification.get("verified", False)):
                    verification_status = BackupVerificationStatus.CURRENT_VERIFIED
                    verified = True
                else:
                    verification_status = BackupVerificationStatus.VERIFICATION_FAILED
                    verified = False
            else:
                # Fast metadata check
                try:
                    health = self._backup.protection_health_snapshot()
                    if health.get("metadata_verified") or health.get(
                        "verification_recorded"
                    ):
                        verification_status = (
                            BackupVerificationStatus.RECORDED_PREVIOUSLY
                        )
                    else:
                        verification_status = BackupVerificationStatus.UNAVAILABLE
                except Exception:
                    verification_status = BackupVerificationStatus.UNAVAILABLE
                verified = False

        if state == ProfileSecurityState.CONFIGURED:
            status_label = "protected"
        elif state in {
            ProfileSecurityState.MISSING_ENVELOPE,
            ProfileSecurityState.CORRUPT_ENVELOPE,
            ProfileSecurityState.TAMPERED,
            ProfileSecurityState.RECOVERY_REQUIRED,
        }:
            status_label = "recovery_required"
        else:
            status_label = "attention"

        return SecurityStatus(
            state=status_label,
            methods=methods,
            default_method=default_method,
            format_active=configured_format,
            latest_backup=latest_name,
            verified=verified,
            detail=detail,
            verification_status=verification_status,
            configured_format=configured_format,
            latest_format=latest_format,
        )

    # -- credential change ----------------------------------------------------

    def change_credential(
        self,
        current_secret: str,
        new_kind: str,
        new_secret: str,
        *,
        weak_pin_confirmed: bool = False,
    ) -> CredentialChangeResult:
        """Delegate to the security service and map errors to categories."""

        try:
            self._security.change_credential(
                current_secret,
                new_kind,
                new_secret,
                weak_pin_confirmed=weak_pin_confirmed,
            )
            return CredentialChangeResult(ok=True)
        except InvalidCredentialError as exc:
            return CredentialChangeResult(
                ok=False, error_category="invalid_credential", message=str(exc)
            )
        except CredentialPolicyError as exc:
            return CredentialChangeResult(
                ok=False, error_category="policy", message=str(exc)
            )
        except CredentialRotationError as exc:
            return CredentialChangeResult(
                ok=False, error_category="rotation", message=str(exc)
            )
        except SecurityEnvelopeError as exc:
            return CredentialChangeResult(
                ok=False, error_category="envelope", message=str(exc)
            )
        except StartupSecurityError as exc:
            return CredentialChangeResult(
                ok=False, error_category="security", message=str(exc)
            )


__all__ = [
    "BackupVerificationStatus",
    "CredentialChangeResult",
    "MethodState",
    "SecuritySettingsController",
    "SecurityStatus",
]
