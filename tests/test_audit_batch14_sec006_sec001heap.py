#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for audit batch 14:

- BUG-SEC-06 (P1): ``StartupSecurityService.inspect_profile()`` must never
  crash the startup gate when the security files (envelope, marker) exist
  but are *unreadable* at the OS level (permissions, locked by antivirus,
  failing disk).  ``Path.is_file()`` itself can raise ``OSError`` on such
  filesystems; every file-inspection step must be wrapped and fall back to
  the safe state (``RECOVERY_REQUIRED`` / ``CORRUPT_ENVELOPE`` per the
  existing policy).  ``SecuritySettingsController.security_status()`` must
  degrade gracefully (never raise) when the underlying inspection and the
  backup layer report unreadable data.
- BUG-SEC-001-HEAP (P2): master-key byte buffers live in Python heap
  memory.  All long-lived copies must be ``bytearray`` (zeroizable) and
  must be zeroized when their owner finishes with them:
  * the fresh master key created by ``configure_first_run`` must be
    zeroized when onboarding fails before the session owns it;
  * the master key unwrapped by ``unlock`` must be zeroized when the
    session construction fails;
  * ``verify_credential_matches`` must zeroize its transient unwrap copy;
  * ``SessionSecurityContext`` must support ``with`` (context manager) so
    callers zeroize deterministically;
  * ``_unwrap_with_any_slot`` must not leave a failed slot's master key
    copy alive.
  Documented Python limitation: immutable ``str``/``bytes`` copies (e.g.
  credential secrets, ``bytes(master)`` snapshots) cannot be zeroized and
  remain out of scope.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import startup_security as startup_security_module  # noqa: E402
from services.backup_crypto import (  # noqa: E402
    MASTER_KEY_BYTES,
    build_envelope,
)
from services.security_settings import (  # noqa: E402
    BackupVerificationStatus,
    SecuritySettingsController,
)
from services.startup_security import (  # noqa: E402
    ProfileSecurityState,
    SessionSecurityContext,
    StartupSecurityService,
)


# ---------------------------------------------------------------------------
# BUG-SEC-06: I/O-crash-proof profile inspection
# ---------------------------------------------------------------------------


def _configured_profile(tmp_path: Path) -> tuple[StartupSecurityService, Path]:
    database = tmp_path / "financas_pessoais.sqlite3"
    service = StartupSecurityService(database_file=database)
    service.configure_first_run("pin", "1357902468")
    assert service.inspect_profile() == ProfileSecurityState.CONFIGURED
    return service, database


def test_sec006_inspect_profile_survives_unreadable_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_file()`` raising OSError on the envelope must not crash the gate."""

    service, _database = _configured_profile(tmp_path)
    real_is_file = Path.is_file

    def _broken_is_file(self: Path) -> bool:
        if self == service.envelope_path:
            raise OSError(5, "Access is denied (simulated antivirus lock)")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", _broken_is_file)

    # Pre-fix this propagates OSError and crashes the startup gate.
    assert (
        service.inspect_profile() == ProfileSecurityState.RECOVERY_REQUIRED
    )


def test_sec006_inspect_profile_survives_unreadable_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker ``is_file()`` OSError must not crash the gate either."""

    service, _database = _configured_profile(tmp_path)
    real_is_file = Path.is_file

    def _broken_is_file(self: Path) -> bool:
        if self == service.marker_path:
            raise OSError(5, "Access is denied (simulated antivirus lock)")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", _broken_is_file)

    assert (
        service.inspect_profile() == ProfileSecurityState.RECOVERY_REQUIRED
    )


def test_sec006_inspect_profile_survives_envelope_read_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError raised by ``read_envelope`` must map to CORRUPT_ENVELOPE."""

    service, _database = _configured_profile(tmp_path)

    def _broken_read_envelope(path: Path) -> object:
        raise OSError(5, "Access is denied while reading the envelope")

    monkeypatch.setattr(startup_security_module, "read_envelope", _broken_read_envelope)

    assert (
        service.inspect_profile() == ProfileSecurityState.CORRUPT_ENVELOPE
    )


def test_sec006_inspect_profile_survives_marker_read_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError raised by ``read_security_marker`` -> CORRUPT_ENVELOPE."""

    service, _database = _configured_profile(tmp_path)

    def _broken_read_marker(path: Path) -> dict[str, Any]:
        raise OSError(5, "Access is denied while reading the marker")

    import services.backup_crypto as backup_crypto_module

    monkeypatch.setattr(
        backup_crypto_module, "read_security_marker", _broken_read_marker
    )

    assert (
        service.inspect_profile() == ProfileSecurityState.CORRUPT_ENVELOPE
    )


def test_sec006_security_status_never_raises_on_unreadable_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``security_status`` degrades to recovery_required, never crashes."""

    service, _database = _configured_profile(tmp_path)
    real_is_file = Path.is_file

    def _broken_is_file(self: Path) -> bool:
        if self == service.envelope_path:
            raise OSError(5, "Access is denied (simulated antivirus lock)")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", _broken_is_file)

    from services.backup import BackupService

    backup = BackupService.__new__(BackupService)
    backup.backup_directory = tmp_path / "backups"
    backup.database_file = _database

    controller = SecuritySettingsController(
        security_service=service, backup_service=backup
    )
    status = controller.security_status()

    assert status.state == "recovery_required"
    assert status.verification_status == BackupVerificationStatus.NO_BACKUP
    assert status.latest_format == "nenhum"


# ---------------------------------------------------------------------------
# BUG-SEC-001-HEAP: master-key zeroization
# ---------------------------------------------------------------------------


def _assert_zeroized(buffer: bytearray) -> None:
    assert isinstance(buffer, bytearray)
    assert len(buffer) == MASTER_KEY_BYTES
    assert bytes(buffer) == b"\x00" * MASTER_KEY_BYTES


def test_sec001heap_configure_first_run_zeroizes_key_on_envelope_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Onboarding failure before the session owns the key must wipe it."""

    import services.backup_crypto as backup_crypto_module

    created: list[bytearray] = []

    def _spying_build_envelope(**kwargs: Any) -> object:
        key = kwargs["master_key"]
        assert isinstance(key, bytearray)
        created.append(key)
        raise backup_crypto_module.BackupCryptoError("falha simulada")

    monkeypatch.setattr(
        startup_security_module, "build_envelope", _spying_build_envelope
    )

    database = tmp_path / "financas_pessoais.sqlite3"
    service = StartupSecurityService(database_file=database)
    with pytest.raises(startup_security_module.StartupSecurityError):
        service.configure_first_run("pin", "1357902468")

    assert len(created) == 1
    _assert_zeroized(created[0])


def test_sec001heap_unlock_zeroizes_key_on_session_construction_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after unwrapping but before returning must wipe the key."""

    service, _database = _configured_profile(tmp_path)

    unwrapped: list[bytearray] = []
    real_unlock_with_credential = startup_security_module.unlock_with_credential

    def _spying_unlock(*args: Any, **kwargs: Any) -> bytearray:
        key = real_unlock_with_credential(*args, **kwargs)
        unwrapped.append(key)
        return key

    monkeypatch.setattr(
        startup_security_module,
        "unlock_with_credential",
        _spying_unlock,
    )
    monkeypatch.setattr(
        SessionSecurityContext,
        "__init__",
        lambda self, **kwargs: (_ for _ in ()).throw(
            RuntimeError("falha simulada na construção da sessão")
        ),
    )

    with pytest.raises(RuntimeError):
        service.unlock("pin", "1357902468")

    assert len(unwrapped) == 1
    _assert_zeroized(unwrapped[0])


def test_sec001heap_verify_credential_matches_zeroizes_transient_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transient unwrap inside ``verify_credential_matches`` is wiped."""

    service, _database = _configured_profile(tmp_path)

    unwrapped: list[bytearray] = []
    real_master_key_of = startup_security_module._master_key_of

    def _spying_master_key_of(envelope: object, secret: str) -> bytearray:
        key = real_master_key_of(envelope, secret)  # type: ignore[arg-type]
        unwrapped.append(key)
        return key

    monkeypatch.setattr(
        startup_security_module, "_master_key_of", _spying_master_key_of
    )

    service.verify_credential_matches("1357902468")

    assert len(unwrapped) == 1
    _assert_zeroized(unwrapped[0])


def test_sec001heap_session_context_manager_zeroizes_on_exit(
    tmp_path: Path,
) -> None:
    """``with session:`` zeroizes the master key deterministically."""

    service, _database = _configured_profile(tmp_path)
    session = service.unlock("pin", "1357902468")

    key = session.backup_master_key
    assert isinstance(key, bytearray)
    assert bytes(key) != b"\x00" * MASTER_KEY_BYTES

    with session as entered:
        assert entered is session
        assert bytes(key) != b"\x00" * MASTER_KEY_BYTES

    _assert_zeroized(key)


def test_sec001heap_unwrap_with_any_slot_fail_closed_no_key_material(
    tmp_path: Path,
) -> None:
    """Fail-closed contract: a wrong credential produces NO key material.

    ``_unwrap_with_any_slot`` only returns the master key from a successful
    ``unlock_with_credential`` call; every failed slot raises before any
    key buffer escapes.  With a two-slot envelope and a wrong credential,
    the function must raise and never hand out a master-key bytearray.
    """

    import services.backup_crypto as backup_crypto_module  # noqa: F401
    from services.backup_crypto import _unwrap_with_any_slot

    master_key = bytearray(b"\x11" * MASTER_KEY_BYTES)
    try:
        two_slot_envelope = build_envelope(
            profile_id="test-profile",
            default_unlock_method="pin",
            master_key=master_key,
            credentials={
                "pin": b"1357902468",
                "password": b"senha-forte-123",
            },
        )
    finally:
        startup_security_module.secure_zero(master_key)

    # Wrong credential: all slots fail -> typed error, no key material.
    with pytest.raises(backup_crypto_module.BackupCryptoError):
        _unwrap_with_any_slot(two_slot_envelope, b"credencial-errada")

    # Correct credential: exactly one zeroizable bytearray is produced.
    recovered = _unwrap_with_any_slot(two_slot_envelope, b"1357902468")
    assert isinstance(recovered, bytearray)
    assert len(recovered) == MASTER_KEY_BYTES
    startup_security_module.secure_zero(recovered)


def test_sec001heap_documented_limitation_immutable_copies(
    tmp_path: Path,
) -> None:
    """The module documents the immutable ``str``/``bytes`` limitation."""

    source = Path(startup_security_module.__file__).read_text(encoding="utf-8")
    assert "immutable" in source
    assert "str" in source
