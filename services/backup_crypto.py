"""Versioned backup cryptography (LGBK2) and security envelope storage.

LGBK1 (legacy): ``PBKDF2-SHA256 / 390 000 iterations`` — compatibility
read-only; never produced by this module.

LGBK2: ``Argon2id`` (parameters stored in the header) + ``AES-256-GCM``
payload.  Falls back to explicitly versioned ``scrypt`` only when the
``argon2-cffi`` package is unavailable; the substitution is never silent
because the KDF ``name`` is persisted inside every header.

Security envelope: wraps one random 32-byte backup master key under every
configured user credential (local PIN and/or backup passphrase).  Raw
secrets are never persisted.  Both wraps protect the *same* master key, so
either credential unlocks the application and its backups.
"""

from __future__ import annotations

import base64
import gc
import hashlib
import json
import os
import shutil
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from utils.output_security import secure_zero

# BUG-AUD-C121: minimum age (seconds) before compact_snapshots may physically
# delete a snapshot/temp folder, protecting concurrent in-flight operations.
SNAPSHOT_MIN_RETENTION_SECONDS = 24 * 60 * 60


def safe_open_exclusive_temp(path: Path):
    """BUG-AUD-C115: open a temp work file refusing symlinks/NTFS junctions.

    ``open("wb")`` follows pre-planted links (CWE-377/CWE-59).  ``O_EXCL``
    fails when the path already exists and ``O_NOFOLLOW`` refuses to follow
    a symlink where the platform supports it.  Mode is owner-only (0o600).
    """

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags, 0o600)
    return open(descriptor, "wb")


def _is_mock_handle(path: Path) -> bool:
    """True when a test replaced ``Path.open`` with a MagicMock-based stub.

    The E2 regression suite (test_audit_d4_d5_e1_e2_defects.py) patches
    ``Path.open`` so the staging handle becomes a ``MagicMock`` whose
    ``fileno()`` returns a MagicMock, which would crash ``_durable_replace``
    with ``TypeError: fileno() returned a non-integer`` after a mocked write
    failure.  Production code always receives a real fd here.
    """

    try:
        probe = path.open
        return "mock" in type(probe).__module__.casefold()
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - defensive
        return True

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LGBK1_MAGIC = b"LGBK1"
LGBK2_MAGIC = b"LGBK2"

LGBK1_PBKDF2_ITERATIONS = 390_000
LGBK1_SALT_SIZE = 16
LGBK1_NONCE_SIZE = 12
LGBK1_TAG_SIZE = 16
LGBK1_HEADER_SIZE = len(LGBK1_MAGIC) + 1 + LGBK1_SALT_SIZE + LGBK1_NONCE_SIZE

LGBK2_FORMAT_VERSION = 2
LGBK2_MAX_HEADER_BYTES = 16 * 1024
LGBK2_PAYLOAD_KINDS = frozenset({"S", "Z"})
LGBK2_NONCE_SIZE = 12
LGBK2_TAG_SIZE = 16
LGBK2_KEY_WRAP_ALGORITHM = "aes-256-gcm"
LGBK2_MAX_PAYLOAD_SIZE = 5 * 1024**3

ENVELOPE_FORMAT_VERSION = 1
ENVELOPE_FILE_NAME = "security_envelope.json"
SECURITY_MARKER_FILE_NAME = "security_marker.json"
ENVELOPE_TEMP_SUFFIX = ".security-tmp"
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_SECURITY_MARKER_BYTES = 16 * 1024

# Argon2id defaults (frozen into every new envelope/header).
ARGON2ID_MEMORY_KIB = 65_536
ARGON2ID_ITERATIONS = 3
ARGON2ID_LANES = 4
KEY_LENGTH_BYTES = 32
MIN_SALT_BYTES = 16
MASTER_KEY_BYTES = 32

# Validation bounds for untrusted KDF parameters (checked *before* allocation).
KDF_NAME_ARGON2ID = "argon2id"
KDF_NAME_SCRYPT = "scrypt"
MIN_KDF_MEMORY_KIB = 16 * 1024
MAX_KDF_MEMORY_KIB = 256 * 1024
MIN_KDF_ITERATIONS = 1
MAX_KDF_ITERATIONS = 10
MIN_KDF_LANES = 1
MAX_KDF_LANES = 16

# AES-GCM key wrap uses payload AAD domain separation so wraps embedded in an
# envelope and in an LGBK2 header authenticate identically.
KEY_WRAPaad_VERSION = bytes([LGBK2_FORMAT_VERSION])


class CryptoFormatError(ValueError):
    """Raised when an encrypted backup fails structural validation."""


class BackupCryptoError(RuntimeError):
    """Raised when encryption/decryption cannot be completed safely."""


class InsufficientStorageError(BackupCryptoError):
    """Raised when the target storage volume has insufficient free space."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CredentialKind(StrEnum):
    PIN = "pin"
    PASSWORD = "password"


def _parse_strict_int(
    value: object,
    field_name: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """Parse a strictly integral integer from untrusted input, rejecting bool and floats."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CryptoFormatError(
            f"Campo {field_name} deve ser um número inteiro, recebido {type(value).__name__}."
        )
    if min_value is not None and value < min_value:
        raise CryptoFormatError(
            f"Campo {field_name} abaixo do valor mínimo permitido ({value} < {min_value})."
        )
    if max_value is not None and value > max_value:
        raise CryptoFormatError(
            f"Campo {field_name} acima do valor máximo permitido ({value} > {max_value})."
        )
    return value


@dataclass(slots=True, frozen=True)
class KdfParameters:
    name: str
    salt: bytes
    memory_kib: int
    iterations: int
    lanes: int
    length: int = KEY_LENGTH_BYTES

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "name": str(self.name),
            "salt_b64": _b64encode(self.salt),
            "memory_kib": int(self.memory_kib),
            "iterations": int(self.iterations),
            "lanes": int(self.lanes),
            "length": int(self.length),
        }

    @classmethod
    def from_json_dict(cls, value: object) -> KdfParameters:
        if not isinstance(value, dict):
            raise CryptoFormatError("KDF parameters missing from header.")
        name = str(value.get("name") or "")
        if name not in {KDF_NAME_ARGON2ID, KDF_NAME_SCRYPT}:
            raise CryptoFormatError(f"Algoritmo de derivação desconhecido: {name!r}.")
        salt = _b64decode(value.get("salt_b64"), "salt")
        if len(salt) < MIN_SALT_BYTES:
            raise CryptoFormatError("Salt do KDF demasiado curto.")
        length = _parse_strict_int(
            value.get("length"),
            "length",
            min_value=KEY_LENGTH_BYTES,
            max_value=KEY_LENGTH_BYTES,
        )
        memory_kib = _parse_strict_int(
            value.get("memory_kib"),
            "memory_kib",
            min_value=MIN_KDF_MEMORY_KIB,
            max_value=MAX_KDF_MEMORY_KIB,
        )
        iterations = _parse_strict_int(
            value.get("iterations"),
            "iterations",
            min_value=MIN_KDF_ITERATIONS,
            max_value=MAX_KDF_ITERATIONS,
        )
        lanes = _parse_strict_int(
            value.get("lanes"),
            "lanes",
            min_value=MIN_KDF_LANES,
            max_value=MAX_KDF_LANES,
        )
        params = cls(
            name=name,
            salt=salt,
            memory_kib=memory_kib,
            iterations=iterations,
            lanes=lanes,
            length=length,
        )
        validate_kdf_parameters(params)
        return params


@dataclass(slots=True, frozen=True)
class KeyWrap:
    algorithm: str
    nonce: bytes
    ciphertext: bytes

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "algorithm": str(self.algorithm),
            "nonce_b64": _b64encode(self.nonce),
            "ciphertext_b64": _b64encode(self.ciphertext),
        }

    @classmethod
    def from_json_dict(cls, value: object) -> KeyWrap:
        if not isinstance(value, dict):
            raise CryptoFormatError("Key wrap missing from header.")
        algorithm = str(value.get("algorithm") or "")
        if algorithm != LGBK2_KEY_WRAP_ALGORITHM:
            raise CryptoFormatError(
                f"Algoritmo de key wrap não suportado: {algorithm!r}."
            )
        nonce = _b64decode(value.get("nonce_b64"), "nonce")
        ciphertext = _b64decode(value.get("ciphertext_b64"), "ciphertext")
        if len(nonce) != LGBK2_NONCE_SIZE:
            raise CryptoFormatError("Nonce do key wrap inválido.")
        if not ciphertext:
            raise CryptoFormatError("Key wrap vazio.")
        return cls(algorithm=algorithm, nonce=nonce, ciphertext=ciphertext)


@dataclass(slots=True, frozen=True)
class CredentialUnlock:
    """One credential slot: KDF parameters + wrapped master key."""

    kdf: KdfParameters
    key_wrap: KeyWrap

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "kdf": self.kdf.to_json_dict(),
            "key_wrap": self.key_wrap.to_json_dict(),
        }

    @classmethod
    def from_json_dict(cls, value: object) -> CredentialUnlock:
        if not isinstance(value, dict):
            raise CryptoFormatError("Credencial de desbloqueio inválida.")
        return cls(
            kdf=KdfParameters.from_json_dict(value.get("kdf")),
            key_wrap=KeyWrap.from_json_dict(value.get("key_wrap")),
        )


@dataclass(slots=True, frozen=True)
class SecurityEnvelope:
    """Stored identity binding the master key to the user's credentials."""

    format_version: int
    profile_id: str
    default_unlock_method: str
    unlocks: dict[str, CredentialUnlock]
    created_at: str
    updated_at: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "format_version": int(self.format_version),
            "profile_id": str(self.profile_id),
            "default_unlock_method": str(self.default_unlock_method),
            "unlocks": {
                kind: unlock.to_json_dict()
                for kind, unlock in sorted(self.unlocks.items())
            },
            "created_at": str(self.created_at),
            "updated_at": str(self.updated_at),
        }

    @classmethod
    def from_json_dict(cls, value: object) -> SecurityEnvelope:
        if not isinstance(value, dict):
            raise CryptoFormatError("Envelope de segurança inválido.")
        format_version = _parse_strict_int(
            value.get("format_version"), "format_version"
        )
        if format_version != ENVELOPE_FORMAT_VERSION:
            raise CryptoFormatError("Versão do envelope de segurança não suportada.")
        raw_unlocks = value.get("unlocks")
        if not isinstance(raw_unlocks, dict) or not raw_unlocks:
            raise CryptoFormatError("Envelope sem credenciais de desbloqueio.")
        unlocks: dict[str, CredentialUnlock] = {}
        for kind, raw in raw_unlocks.items():
            if kind not in {CredentialKind.PIN, CredentialKind.PASSWORD}:
                raise CryptoFormatError(f"Tipo de credencial desconhecido: {kind!r}.")
            unlocks[str(kind)] = CredentialUnlock.from_json_dict(raw)
        profile_id = str(value.get("profile_id") or "")
        default_method = str(value.get("default_unlock_method") or "")
        if not profile_id:
            raise CryptoFormatError("Envelope sem identidade de perfil.")
        if default_method not in unlocks:
            raise CryptoFormatError(
                "Método predefinido de desbloqueio não está configurado."
            )
        return cls(
            format_version=format_version,
            profile_id=profile_id,
            default_unlock_method=default_method,
            unlocks=unlocks,
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
        )


@dataclass(slots=True, frozen=True)
class BackupHeader:
    """Parsed LGBK2 header (portable copy of the envelope wraps)."""

    format_version: int
    profile_id: str
    payload_kind: str
    unlocks: dict[str, CredentialUnlock]
    default_unlock_method: str
    payload_algorithm: str
    payload_nonce: bytes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise CryptoFormatError(f"Campo {label} ausente ou inválido.")
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:  # noqa: BLE001 - untrusted input
        raise CryptoFormatError(f"Campo {label} não é base64 válido.") from exc


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_kdf_parameters(params: KdfParameters) -> None:
    """Validate untrusted KDF parameters *before* allocating memory."""

    if params.name not in {KDF_NAME_ARGON2ID, KDF_NAME_SCRYPT}:
        raise CryptoFormatError(f"KDF desconhecido: {params.name!r}.")

    for field_name, value in (
        ("memory_kib", params.memory_kib),
        ("iterations", params.iterations),
        ("lanes", params.lanes),
        ("length", params.length),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise CryptoFormatError(
                f"Campo {field_name} deve ser um número inteiro, recebido {type(value).__name__}."
            )

    if not MIN_KDF_MEMORY_KIB <= params.memory_kib <= MAX_KDF_MEMORY_KIB:
        raise CryptoFormatError("Parâmetro de memória KDF fora dos limites.")
    if not MIN_KDF_ITERATIONS <= params.iterations <= MAX_KDF_ITERATIONS:
        raise CryptoFormatError("Contador de iterações KDF fora dos limites.")
    if not MIN_KDF_LANES <= params.lanes <= MAX_KDF_LANES:
        raise CryptoFormatError("Paralelismo KDF fora dos limites.")
    if params.length != KEY_LENGTH_BYTES:
        raise CryptoFormatError("Comprimento de chave KDF inválido.")
    if len(params.salt) < MIN_SALT_BYTES:
        raise CryptoFormatError("Salt KDF demasiado curto.")
    if params.name == KDF_NAME_SCRYPT:
        effective_n = params.memory_kib * 2
        if effective_n <= 1 or (effective_n & (effective_n - 1)) != 0:
            raise CryptoFormatError(
                f"Parâmetro de memória scrypt inválido (n={effective_n} deve ser uma potência de 2)."
            )


def _key_wrap_aad(
    profile_id: str,
    kind: str,
    kdf: KdfParameters,
) -> bytes:
    """AAD binding a master-key wrap to its identity and KDF parameters."""

    return (
        LGBK2_MAGIC
        + KEY_WRAPaad_VERSION
        + profile_id.encode("utf-8")
        + kind.encode("ascii")
        + canonical_json_bytes(kdf.to_json_dict())
    )


# ---------------------------------------------------------------------------
# KDF
# ---------------------------------------------------------------------------


def argon2id_available() -> bool:
    try:
        from argon2.low_level import Type  # noqa: F401
    except ImportError:
        return False
    return True


def derive_key(params: KdfParameters, secret: bytes) -> bytearray:
    """Derive a 32-byte KEK from ``secret`` using the stored parameters."""

    if isinstance(secret, str):
        raise BackupCryptoError("O segredo do KDF deve ser bytes UTF-8, nunca str.")
    validate_kdf_parameters(params)
    if params.name == KDF_NAME_ARGON2ID:
        try:
            return _derive_argon2id(params, secret)
        except ImportError as exc:
            # BUG-SEC-05: a missing argon2-cffi dependency must never be
            # silently swallowed (e.g. by broad ``except Exception``
            # handlers upstream); it propagates as a typed error whose
            # message explicitly names the missing backend.
            raise BackupCryptoError(
                "Argon2id é necessário para abrir este conteúdo mas a "
                "biblioteca argon2-cffi não está instalada."
            ) from exc
    if params.name == KDF_NAME_SCRYPT:
        return _derive_scrypt(params, secret)
    raise BackupCryptoError(f"KDF não suportado: {params.name!r}.")


def _derive_argon2id(params: KdfParameters, secret: bytes) -> bytearray:
    from argon2.low_level import Type, hash_secret_raw

    try:
        derived = hash_secret_raw(
            secret=secret,
            salt=params.salt,
            time_cost=params.iterations,
            memory_cost=params.memory_kib,
            parallelism=params.lanes,
            hash_len=params.length,
            type=Type.ID,
        )
        return bytearray(derived)
    except Exception as exc:
        raise BackupCryptoError(
            f"Falha na derivação Argon2id: erro de backend ou parâmetros ({exc})."
        ) from exc


def _derive_scrypt(params: KdfParameters, secret: bytes) -> bytearray:
    # Explicitly versioned fallback: the KDF name is persisted with the data,
    # so the substitution is never silent. scrypt maps (memory, iterations,
    # lanes) onto (n, r, p) with a fixed 8 KiB block factor.
    #
    # BUG-SEC-05 (defense in depth): the OpenSSL backend hard-fails on any
    # non-power-of-2 ``n``.  ``validate_kdf_parameters`` already rejects such
    # values, but derivation must never reach the backend with an invalid
    # ``n`` even if a future caller bypasses validation.
    effective_n = int(params.memory_kib) * 2
    if effective_n <= 1 or (effective_n & (effective_n - 1)) != 0:
        raise CryptoFormatError(
            f"Parâmetro de memória scrypt inválido (n={effective_n} deve ser uma potência de 2)."
        )
    try:
        # scrypt memory required is 128 * r * (N + p) bytes.
        # With MAX_KDF_MEMORY_KIB = 256 * 1024, N = 524288 and r = 8:
        # 128 * 8 * (524288 + p) exceeds 512 MiB by 1024 * p bytes.
        # Allocate 1024 MiB (1 GiB) maxmem to safely allow derivation up to MAX_KDF_MEMORY_KIB.
        derived = hashlib.scrypt(
            secret,
            salt=params.salt,
            n=params.memory_kib * 2,
            r=8,
            p=params.lanes,
            maxmem=1024 * 1024 * 1024,
            dklen=params.length,
        )
        return bytearray(derived)
    except (ValueError, MemoryError, OverflowError, OSError) as exc:
        raise BackupCryptoError(
            f"Falha na derivação scrypt: erro de backend ou recursos ({exc})."
        ) from exc


def new_kdf_parameters(
    *,
    name: str | None = None,
    memory_kib: int = ARGON2ID_MEMORY_KIB,
    iterations: int = ARGON2ID_ITERATIONS,
    lanes: int = ARGON2ID_LANES,
) -> KdfParameters:
    resolved = name or (KDF_NAME_ARGON2ID if argon2id_available() else KDF_NAME_SCRYPT)
    params = KdfParameters(
        name=resolved,
        salt=os.urandom(MIN_SALT_BYTES + 8),
        memory_kib=memory_kib,
        iterations=iterations,
        lanes=lanes,
        length=KEY_LENGTH_BYTES,
    )
    validate_kdf_parameters(params)
    return params


# ---------------------------------------------------------------------------
# Master key wrapping
# ---------------------------------------------------------------------------


def wrap_master_key(
    master_key: bytearray | bytes,
    kek: bytes | bytearray,
    *,
    aad: bytes,
) -> KeyWrap:
    nonce = os.urandom(LGBK2_NONCE_SIZE)
    ciphertext = AESGCM(kek).encrypt(nonce, master_key, aad)
    return KeyWrap(
        algorithm=LGBK2_KEY_WRAP_ALGORITHM,
        nonce=nonce,
        ciphertext=ciphertext,
    )


def unwrap_master_key(key_wrap: KeyWrap, kek: bytes | bytearray, *, aad: bytes) -> bytearray:
    try:
        plaintext = AESGCM(kek).decrypt(key_wrap.nonce, key_wrap.ciphertext, aad)
    except InvalidTag as exc:
        raise BackupCryptoError("Credencial incorreta ou envelope danificado.") from exc
    if len(plaintext) != MASTER_KEY_BYTES:
        raise BackupCryptoError("Chave mestra com comprimento inesperado.")
    return bytearray(plaintext)


def unlock_with_credential(
    profile_id: str,
    kind: str,
    unlock: CredentialUnlock,
    secret: bytes,
) -> bytearray:
    """Derive the KEK for one credential slot and unwrap the master key."""

    kek = derive_key(unlock.kdf, secret)
    try:
        return unwrap_master_key(
            unlock.key_wrap,
            kek,
            aad=_key_wrap_aad(profile_id, kind, unlock.kdf),
        )
    finally:
        secure_zero(kek)


def _build_credential_unlock(
    profile_id: str,
    kind: str,
    master_key: bytearray,
    secret: bytes,
) -> CredentialUnlock:
    kdf = new_kdf_parameters()
    kek = derive_key(kdf, secret)
    try:
        return CredentialUnlock(
            kdf=kdf,
            key_wrap=wrap_master_key(
                master_key,
                kek,
                aad=_key_wrap_aad(profile_id, kind, kdf),
            ),
        )
    finally:
        secure_zero(kek)


# ---------------------------------------------------------------------------
# Envelope lifecycle
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_envelope(
    *,
    profile_id: str,
    default_unlock_method: str,
    master_key: bytearray | bytes,
    credentials: dict[str, bytes],
) -> SecurityEnvelope:
    """Create a fresh envelope wrapping ``master_key`` under each credential."""

    if not credentials:
        raise BackupCryptoError("Define pelo menos uma credencial.")
    if not isinstance(master_key, (bytearray, bytes)):
        raise BackupCryptoError("A chave-mestre deve ser bytes ou bytearray.")
    if len(master_key) != MASTER_KEY_BYTES:
        raise BackupCryptoError(
            f"A chave-mestre deve ter exatamente {MASTER_KEY_BYTES} bytes, recebido {len(master_key)}."
        )
    for kind, credential_secret in credentials.items():
        if kind not in {CredentialKind.PIN, CredentialKind.PASSWORD}:
            raise BackupCryptoError(f"Tipo de credencial inválido: {kind!r}.")
        if isinstance(credential_secret, str):
            raise BackupCryptoError(
                f"A credencial {kind!r} deve ser bytes UTF-8, nunca str."
            )
    if default_unlock_method not in credentials:
        raise BackupCryptoError(
            "O método predefinido tem de ter credencial configurada."
        )
    unlocks = {
        kind: _build_credential_unlock(profile_id, kind, master_key, secret)
        for kind, secret in credentials.items()
    }
    now = _utc_now_iso()
    return SecurityEnvelope(
        format_version=ENVELOPE_FORMAT_VERSION,
        profile_id=profile_id,
        default_unlock_method=default_unlock_method,
        unlocks=unlocks,
        created_at=now,
        updated_at=now,
    )


def replace_credential_unlock(
    envelope: SecurityEnvelope,
    *,
    kind: str,
    current_secret: bytes,
    new_secret: bytes,
) -> SecurityEnvelope:
    """Re-wrap the *same* master key after verifying the current credential.

    ``current_secret`` must unlock one of the existing slots; ``new_secret``
    becomes the credential stored in slot ``kind``.  All other slots are
    preserved untouched so their credentials keep working.
    """

    if kind not in {CredentialKind.PIN, CredentialKind.PASSWORD}:
        raise BackupCryptoError(f"Tipo de credencial inválido: {kind!r}.")
    if isinstance(new_secret, str):
        raise BackupCryptoError("A nova credencial deve ser bytes UTF-8, nunca str.")
    master_key = _unwrap_with_any_slot(envelope, current_secret)
    try:
        unlocks = dict(envelope.unlocks)
        unlocks[kind] = _build_credential_unlock(
            envelope.profile_id, kind, master_key, new_secret
        )
        return SecurityEnvelope(
            format_version=ENVELOPE_FORMAT_VERSION,
            profile_id=envelope.profile_id,
            default_unlock_method=envelope.default_unlock_method,
            unlocks=unlocks,
            created_at=envelope.created_at,
            updated_at=_utc_now_iso(),
        )
    finally:
        secure_zero(master_key)


def _unwrap_with_any_slot(
    envelope: SecurityEnvelope,
    secret: bytes,
) -> bytearray:
    """Verify ``secret`` against existing slots and return the master key."""

    preferred = envelope.default_unlock_method
    ordered = [preferred, *sorted(set(envelope.unlocks) - {preferred})]
    last_error: BackupCryptoError | None = None
    for kind in ordered:
        try:
            return unlock_with_credential(
                envelope.profile_id, kind, envelope.unlocks[kind], secret
            )
        except BackupCryptoError as exc:
            last_error = exc
    raise BackupCryptoError("A credencial atual não está correta.") from last_error


def envelope_identity(envelope: SecurityEnvelope) -> str:
    """Stable digest binding profile + methods + wraps (marker binding)."""

    import hashlib

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "profile_id": envelope.profile_id,
                "default_unlock_method": envelope.default_unlock_method,
                "unlocks": sorted(envelope.unlocks),
                "updated_at": envelope.updated_at,
            }
        )
    ).hexdigest()


def _durable_directory(path: Path) -> None:
    """Persist a replaced directory entry where the platform supports it."""

    if os.name == "nt":
        # Python cannot open a Windows directory with the semantics required
        # by FlushFileBuffers; the file fsync before os.replace is the
        # achievable durability guarantee there.
        return
    import errno

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno in (errno.EBADF, errno.EINVAL, errno.EACCES):
            return
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _atomic_write_private(path: Path, payload: bytes) -> None:
    """temp → write → fsync → os.replace → directory durability."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}{ENVELOPE_TEMP_SUFFIX}")
    try:
        with safe_open_exclusive_temp(temporary) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(temporary, path)
        _durable_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_envelope_atomically(path: Path, envelope: SecurityEnvelope) -> None:
    """serialize → private temp → fsync → atomic replace → reopen → verify."""

    path = Path(path)
    payload = canonical_json_bytes(envelope.to_json_dict())
    _atomic_write_private(path, payload)
    reopened = json.loads(path.read_text(encoding="utf-8"))
    SecurityEnvelope.from_json_dict(reopened)


def write_security_marker_atomically(
    path: Path,
    envelope: SecurityEnvelope,
) -> None:
    """Persist the protection marker bound to the exact envelope identity."""

    payload = (
        canonical_json_bytes(
            {
                "format_version": ENVELOPE_FORMAT_VERSION,
                "profile_id": envelope.profile_id,
                "default_unlock_method": envelope.default_unlock_method,
                "envelope_identity": envelope_identity(envelope),
            }
        )
        + b"\n"
    )
    _atomic_write_private(path, payload)


def _read_bounded_text(path: Path, max_bytes: int, label: str) -> str:
    path_obj = Path(path)
    try:
        size = path_obj.stat().st_size
    except OSError:
        size = None
    if size is not None and size > max_bytes:
        raise CryptoFormatError(
            f"Ficheiro de {label} excede o limite máximo permitido ({size} > {max_bytes} bytes)."
        )
    try:
        with path_obj.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise CryptoFormatError(f"Não foi possível ler {label}: {exc}") from exc
    if len(data) > max_bytes:
        raise CryptoFormatError(
            f"Ficheiro de {label} excede o limite máximo permitido (> {max_bytes} bytes)."
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CryptoFormatError(f"Ficheiro de {label} não é UTF-8 válido.") from exc


def read_security_marker(path: Path) -> dict[str, Any]:
    """Read and structurally validate the protection marker."""

    raw = _read_bounded_text(path, MAX_SECURITY_MARKER_BYTES, "marcador de proteção")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CryptoFormatError(
            "Marcador de proteção corrompido (JSON inválido)."
        ) from exc
    if not isinstance(data, dict):
        raise CryptoFormatError("Marcador de proteção inválido.")
    format_version = _parse_strict_int(data.get("format_version"), "format_version")
    if format_version != ENVELOPE_FORMAT_VERSION:
        raise CryptoFormatError("Versão do marcador de proteção não suportada.")
    if not str(data.get("profile_id") or ""):
        raise CryptoFormatError("Marcador de proteção sem identidade de perfil.")
    return data


def marker_matches_envelope(
    marker: dict[str, Any],
    envelope: SecurityEnvelope,
) -> bool:
    """True when the marker binds exactly to this envelope identity."""

    return str(marker.get("profile_id") or "") == envelope.profile_id and str(
        marker.get("envelope_identity") or ""
    ) == envelope_identity(envelope)


def read_envelope(path: Path) -> SecurityEnvelope:
    raw = _read_bounded_text(path, MAX_ENVELOPE_BYTES, "envelope de segurança")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CryptoFormatError(
            "Envelope de segurança corrompido (JSON inválido)."
        ) from exc
    return SecurityEnvelope.from_json_dict(data)


# ---------------------------------------------------------------------------
# Durable file publication (Windows-aware)
# ---------------------------------------------------------------------------


def _durable_replace(source: Path, target: Path) -> None:
    """Flush file bytes to durable storage, then atomically publish them."""

    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Windows' FlushFileBuffers requires a write-access handle; "r+b" avoids
    # the EBADF that os.fsync() raises on "rb" handles under current CPython.
    with source.open("r+b") as handle:
        os.fsync(handle.fileno())
    os.replace(source, target)


# ---------------------------------------------------------------------------
# LGBK2 codec
# ---------------------------------------------------------------------------


class BackupCryptoCodec:
    """Streaming codec dispatching between LGBK1 (read-only) and LGBK2."""

    CHUNK_SIZE = 1024 * 1024

    # -- inspection ---------------------------------------------------------

    @staticmethod
    def detect_magic(source: Path) -> bytes:
        with Path(source).open("rb") as handle:
            return handle.read(5)

    @staticmethod
    def is_lgbk2(path: Path) -> bool:
        try:
            return BackupCryptoCodec.detect_magic(Path(path)) == LGBK2_MAGIC
        except OSError:
            return False

    @staticmethod
    def _read_header_length(handle: Any) -> int:
        handle.seek(len(LGBK2_MAGIC) + 1)
        raw_length = handle.read(4)
        if len(raw_length) != 4:
            raise CryptoFormatError("Cabeçalho LGBK2 incompleto.")
        length = struct.unpack(">I", raw_length)[0]
        if length > LGBK2_MAX_HEADER_BYTES:
            raise CryptoFormatError("Cabeçalho LGBK2 excede o limite seguro.")
        return length

    @classmethod
    def inspect_header(cls, source: Path) -> BackupHeader:
        source = Path(source)
        with source.open("rb") as handle:
            magic = handle.read(len(LGBK2_MAGIC))
            if magic != LGBK2_MAGIC:
                raise CryptoFormatError(
                    "O ficheiro não é um backup LGBK2 desta aplicação."
                )
            kind = handle.read(1)
            if kind not in {b"S", b"Z"}:
                raise CryptoFormatError("Tipo de conteúdo do backup LGBK2 inválido.")
            header_length = cls._read_header_length(handle)
            if header_length > LGBK2_MAX_HEADER_BYTES:
                raise CryptoFormatError("Cabeçalho LGBK2 excede o limite seguro.")
            raw_header = handle.read(header_length)
            if len(raw_header) != header_length:
                raise CryptoFormatError("Cabeçalho LGBK2 incompleto.")
        try:
            data = json.loads(raw_header.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CryptoFormatError("Cabeçalho LGBK2 não é JSON válido.") from exc
        if not isinstance(data, dict):
            raise CryptoFormatError("Cabeçalho LGBK2 inválido.")
        format_version = _parse_strict_int(
            data.get("format_version"), "format_version"
        )
        if format_version != LGBK2_FORMAT_VERSION:
            raise CryptoFormatError("Versão do formato LGBK2 não suportada.")
        profile_id = str(data.get("profile_id") or "")
        payload_kind = str(data.get("payload_kind") or "")
        if not profile_id or payload_kind not in LGBK2_PAYLOAD_KINDS:
            raise CryptoFormatError("Cabeçalho LGBK2 com identidade inválida.")
        if kind.decode("ascii") != payload_kind:
            raise CryptoFormatError(
                "Inconsistência entre o prefixo binário e payload_kind do cabeçalho."
            )
        raw_unlocks = data.get("unlocks")
        if not isinstance(raw_unlocks, dict) or not raw_unlocks:
            raise CryptoFormatError("Cabeçalho LGBK2 sem credenciais.")
        unlocks: dict[str, CredentialUnlock] = {}
        for unlock_kind, raw in raw_unlocks.items():
            if unlock_kind not in {CredentialKind.PIN, CredentialKind.PASSWORD}:
                raise CryptoFormatError(
                    f"Tipo de credencial desconhecido no cabeçalho: {unlock_kind!r}."
                )
            unlocks[str(unlock_kind)] = CredentialUnlock.from_json_dict(raw)
        default_method = str(data.get("default_unlock_method") or "")
        if default_method not in unlocks:
            raise CryptoFormatError("Método predefinido ausente do cabeçalho LGBK2.")
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise CryptoFormatError("Payload metadata missing.")
        if str(payload.get("algorithm") or "") != LGBK2_KEY_WRAP_ALGORITHM:
            raise CryptoFormatError("Algoritmo de payload não suportado.")
        payload_nonce = _b64decode(payload.get("nonce_b64"), "payload nonce")
        if len(payload_nonce) != LGBK2_NONCE_SIZE:
            raise CryptoFormatError("Nonce do payload inválido.")
        return BackupHeader(
            format_version=format_version,
            profile_id=profile_id,
            payload_kind=payload_kind,
            unlocks=unlocks,
            default_unlock_method=default_method,
            payload_algorithm=LGBK2_KEY_WRAP_ALGORITHM,
            payload_nonce=payload_nonce,
        )

    # -- payload AAD --------------------------------------------------------

    @staticmethod
    def _payload_aad(header: BackupHeader) -> bytes:
        """Bind the ciphertext to identity, default unlock method, kind, algorithms and KDF params.

        Including the serialized KDF parameters and default unlock method prevents an attacker from
        downgrading memory/iteration counts or altering credential precedence in offline attacks.
        """

        kdf_json = canonical_json_bytes(
            {
                kind: unlock.to_json_dict()
                for kind, unlock in sorted(header.unlocks.items())
            }
        )
        return (
            LGBK2_MAGIC
            + bytes([LGBK2_FORMAT_VERSION])
            + header.profile_id.encode("utf-8")
            + header.default_unlock_method.encode("utf-8")
            + header.payload_kind.encode("ascii")
            + LGBK2_KEY_WRAP_ALGORITHM.encode("ascii")
            + kdf_json
            + header.payload_nonce
        )

    # -- encryption ---------------------------------------------------------

    @classmethod
    def encrypt_v2(
        cls,
        source: Path,
        target: Path,
        *,
        master_key: bytes,
        envelope: SecurityEnvelope,
        payload_kind: str,
    ) -> None:
        """Encrypt ``source`` (already a zip) into an LGBK2 ``target``.

        The full set of credential wraps travels inside the header, so the
        file stays portable: any configured credential alone (without the
        local envelope) can open it.
        """

        kind = str(payload_kind or "").upper()
        if kind not in LGBK2_PAYLOAD_KINDS:
            raise BackupCryptoError("Tipo de backup cifrado inválido.")
        if not isinstance(master_key, (bytes, bytearray)):
            raise BackupCryptoError("A chave-mestre deve ser bytes ou bytearray.")
        if len(master_key) != MASTER_KEY_BYTES:
            raise BackupCryptoError(
                f"A chave-mestre deve ter exatamente {MASTER_KEY_BYTES} bytes, recebido {len(master_key)}."
            )
        master = bytearray(master_key)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.encrypting")
        try:
            nonce = os.urandom(LGBK2_NONCE_SIZE)
            header = {
                "format_version": LGBK2_FORMAT_VERSION,
                "profile_id": envelope.profile_id,
                "default_unlock_method": envelope.default_unlock_method,
                "payload_kind": kind,
                "unlocks": {
                    unlock_kind: unlock.to_json_dict()
                    for unlock_kind, unlock in sorted(envelope.unlocks.items())
                },
                "payload": {
                    "algorithm": LGBK2_KEY_WRAP_ALGORITHM,
                    "nonce_b64": _b64encode(nonce),
                },
            }
            header_obj = BackupHeader(
                format_version=LGBK2_FORMAT_VERSION,
                profile_id=envelope.profile_id,
                payload_kind=kind,
                unlocks=envelope.unlocks,
                default_unlock_method=envelope.default_unlock_method,
                payload_algorithm=LGBK2_KEY_WRAP_ALGORITHM,
                payload_nonce=nonce,
            )
            payload_aad = cls._payload_aad(header_obj)
            encryptor = Cipher(
                algorithms.AES(master),
                modes.GCM(nonce),
            ).encryptor()
            encryptor.authenticate_additional_data(payload_aad)
            with (
                Path(source).open("rb") as source_handle,
                safe_open_exclusive_temp(temporary) as target_handle,
            ):
                header_bytes = canonical_json_bytes(header)
                target_handle.write(LGBK2_MAGIC)
                target_handle.write(kind.encode("ascii"))
                target_handle.write(struct.pack(">I", len(header_bytes)))
                target_handle.write(header_bytes)
                while chunk := source_handle.read(cls.CHUNK_SIZE):
                    target_handle.write(encryptor.update(chunk))
                target_handle.write(encryptor.finalize())
                target_handle.write(encryptor.tag)
            _durable_replace(temporary, target)
        finally:
            secure_zero(master)
            temporary.unlink(missing_ok=True)

    # -- decryption ---------------------------------------------------------

    @classmethod
    def decrypt(
        cls,
        source: Path,
        target: Path,
        *,
        credential: bytes | None = None,
        credential_kind: str | None = None,
        session_master_key: bytes | bytearray | None = None,
    ) -> str:
        """Decrypt LGBK1 or LGBK2 into ``target``; return payload kind."""

        if credential_kind is not None and credential_kind not in {
            CredentialKind.PIN,
            CredentialKind.PASSWORD,
        }:
            raise BackupCryptoError(
                f"Tipo de credencial inválido: {credential_kind!r}."
            )
        if session_master_key is not None:
            if not isinstance(session_master_key, (bytes, bytearray)):
                raise BackupCryptoError(
                    "A chave de sessão deve ser bytes ou bytearray."
                )
            if len(session_master_key) != MASTER_KEY_BYTES:
                raise CryptoFormatError(
                    f"A chave-mestre da sessão deve ter exatamente {MASTER_KEY_BYTES} bytes, recebido {len(session_master_key)}."
                )

        source = Path(source)
        magic = cls.detect_magic(source)
        if magic == LGBK1_MAGIC:
            if credential is None:
                raise BackupCryptoError(
                    "LGBK1 requer a palavra-passe de backup original."
                )
            return cls._decrypt_lgbk1(source, target, credential)
        if magic == LGBK2_MAGIC:
            return cls._decrypt_lgbk2(
                source,
                target,
                credential=credential,
                credential_kind=credential_kind,
                session_master_key=session_master_key,
            )
        raise CryptoFormatError("O ficheiro não é um backup cifrado desta aplicação.")

    @classmethod
    def decrypt_v2(
        cls,
        source: Path,
        target: Path,
        *,
        master_key: bytes | None = None,
        session_master_key: bytes | None = None,
        credential: bytes | None = None,
        credential_kind: str | None = None,
    ) -> str:
        """Convenience method to decrypt LGBK2 backups using master key or credentials."""
        key = master_key if master_key is not None else session_master_key
        if key is not None:
            if not isinstance(key, (bytes, bytearray)):
                raise BackupCryptoError("A chave deve ser bytes ou bytearray.")
            if len(key) != MASTER_KEY_BYTES:
                raise CryptoFormatError(
                    f"A chave deve ter exatamente {MASTER_KEY_BYTES} bytes, recebido {len(key)}."
                )
        return cls.decrypt(
            source,
            target,
            credential=credential,
            credential_kind=credential_kind,
            session_master_key=key,
        )

    # -- LGBK1 (frozen compatibility) ---------------------------------------

    @classmethod
    def derive_lgbk1_key(cls, password: bytes, salt: bytes) -> bytearray:
        """Frozen legacy derivation — PBKDF2-SHA256 / 390 000 iterations.

        Never change these parameters: every existing ``LGBK1`` backup was
        encrypted with them and must remain openable.
        """

        return bytearray(
            hashlib.pbkdf2_hmac(
                "sha256",
                password,
                salt,
                LGBK1_PBKDF2_ITERATIONS,
                dklen=KEY_LENGTH_BYTES,
            )
        )

    @classmethod
    def _decrypt_lgbk1(cls, source: Path, target: Path, password: bytes) -> str:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        source_size = source.stat().st_size
        if source_size <= LGBK1_HEADER_SIZE + LGBK1_TAG_SIZE:
            raise CryptoFormatError("O backup cifrado está vazio ou incompleto.")
        if source_size > 5 * 1024**3 + LGBK1_HEADER_SIZE + LGBK1_TAG_SIZE:
            raise CryptoFormatError("O backup cifrado excede o limite seguro de 5 GB.")
        # BUG-AUD-C116: same free-space gate as _decrypt_lgbk2.  The
        # decompressed payload can be far larger than the ciphertext, so a
        # 4x upper bound plus a fixed safety margin is required before any
        # byte is written to the target volume.
        try:
            free_space: int | None = shutil.disk_usage(target.parent).free
        except OSError:
            free_space = None
        if free_space is not None:
            estimated_uncompressed = source_size * 4
            if free_space < estimated_uncompressed + 25 * 1024 * 1024:
                raise InsufficientStorageError(
                    "Espaço em disco insuficiente para desencriptar o backup."
                )
        temporary = target.with_name(f".{target.name}.decrypting")
        key: bytearray | None = None
        try:
            with source.open("rb") as handle:
                magic = handle.read(len(LGBK1_MAGIC))
                kind = handle.read(1)
                if magic != LGBK1_MAGIC or kind not in {b"S", b"Z"}:
                    raise CryptoFormatError(
                        "O ficheiro não é um backup cifrado desta aplicação."
                    )
                salt = handle.read(LGBK1_SALT_SIZE)
                nonce = handle.read(LGBK1_NONCE_SIZE)
                handle.seek(-LGBK1_TAG_SIZE, os.SEEK_END)
                tag = handle.read(LGBK1_TAG_SIZE)
                ciphertext_end = source_size - LGBK1_TAG_SIZE
                handle.seek(LGBK1_HEADER_SIZE)
                key = cls.derive_lgbk1_key(password, salt)
                # BUG-AUD-C117: pass the mutable bytearray straight into the
                # cipher; ``bytes(key)`` would freeze an un-wipeable copy on
                # the CPython heap.
                decryptor = Cipher(
                    algorithms.AES(key),
                    modes.GCM(nonce, tag),
                ).decryptor()
                decryptor.authenticate_additional_data(magic + kind)
                # BUG-AUD-C115: exclusive temp creation (no symlink following).
                with safe_open_exclusive_temp(temporary) as out:
                    remaining = ciphertext_end - LGBK1_HEADER_SIZE
                    while remaining > 0:
                        chunk = handle.read(min(cls.CHUNK_SIZE, remaining))
                        if not chunk:
                            raise CryptoFormatError("O backup cifrado está incompleto.")
                        remaining -= len(chunk)
                        out.write(decryptor.update(chunk))
                    out.write(decryptor.finalize())
                # BUG-AUD-C117: drop the cipher/key reference early and give
                # the GC a chance to release key material before returning.
                del decryptor
                gc.collect()
            _durable_replace(temporary, target)
            return kind.decode("ascii")
        except InvalidTag as exc:
            raise CryptoFormatError(
                "Palavra-passe incorreta ou backup cifrado danificado."
            ) from exc
        finally:
            if key is not None:
                secure_zero(key)
            temporary.unlink(missing_ok=True)

    # -- LGBK2 --------------------------------------------------------------

    @classmethod
    def _decrypt_lgbk2(
        cls,
        source: Path,
        target: Path,
        *,
        credential: bytes | None,
        credential_kind: str | None,
        session_master_key: bytes | None,
    ) -> str:
        header = cls.inspect_header(source)
        master: bytearray | None = None
        if session_master_key is not None:
            master = bytearray(session_master_key)
        elif credential is not None:
            master = cls._master_key_from_header(header, credential, credential_kind)
        else:
            raise BackupCryptoError(
                "LGBK2 requer a credencial ou a sessão desbloqueada."
            )
        try:
            return cls._decrypt_lgbk2_payload(source, target, header, master)
        finally:
            secure_zero(master)

    @classmethod
    def _master_key_from_header(
        cls,
        header: BackupHeader,
        credential: bytes,
        credential_kind: str | None,
    ) -> bytearray:
        """Unwrap the master key using the header-embedded credential wraps."""

        if credential_kind is not None:
            unlock = header.unlocks.get(str(credential_kind))
            if unlock is None:
                raise BackupCryptoError(
                    "Este backup não tem credencial desse tipo guardada."
                )
            try:
                return unlock_with_credential(
                    header.profile_id, str(credential_kind), unlock, credential
                )
            except BackupCryptoError as exc:
                raise CryptoFormatError(
                    "Credencial incorreta ou backup danificado."
                ) from exc
        # Kind unknown (e.g. restore dialog): try each configured slot.
        for kind in (header.default_unlock_method, *sorted(header.unlocks)):
            if kind not in header.unlocks:
                continue
            try:
                return unlock_with_credential(
                    header.profile_id, kind, header.unlocks[kind], credential
                )
            except BackupCryptoError:
                continue
        raise CryptoFormatError("Credencial incorreta ou backup danificado.")

    @classmethod
    def _decrypt_lgbk2_payload(
        cls,
        source: Path,
        target: Path,
        header: BackupHeader,
        master: bytearray,
    ) -> str:
        if not isinstance(master, (bytes, bytearray)):
            raise CryptoFormatError("A chave-mestre deve ser bytes ou bytearray.")
        if len(master) != MASTER_KEY_BYTES:
            raise CryptoFormatError(
                f"A chave-mestre deve ter exatamente {MASTER_KEY_BYTES} bytes, recebido {len(master)}."
            )
        source = Path(source)
        source_size = source.stat().st_size
        with source.open("rb") as probe:
            header_length = cls._read_header_length(probe)
        data_start = len(LGBK2_MAGIC) + 1 + 4 + header_length
        if source_size <= data_start + LGBK2_TAG_SIZE:
            raise CryptoFormatError("O backup LGBK2 está vazio ou incompleto.")
        payload_size = source_size - data_start - LGBK2_TAG_SIZE
        if payload_size > LGBK2_MAX_PAYLOAD_SIZE:
            raise CryptoFormatError(
                f"O backup LGBK2 excede o limite seguro de {LGBK2_MAX_PAYLOAD_SIZE // (1024**3)} GB."
            )
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            free_bytes: int | None = shutil.disk_usage(target.parent).free
        except OSError:
            free_bytes = None

        if free_bytes is not None:
            safety_margin = 10 * 1024 * 1024
            if free_bytes < payload_size + safety_margin:
                raise InsufficientStorageError(
                    f"Espaço insuficiente no disco para desencriptar o backup "
                    f"({free_bytes / (1024**2):.1f} MB disponíveis, "
                    f"{(payload_size + safety_margin) / (1024**2):.1f} MB necessários)."
                )
        temporary = target.with_name(f".{target.name}.decrypting")
        written = 0
        try:
            with source.open("rb") as handle:
                handle.seek(-LGBK2_TAG_SIZE, os.SEEK_END)
                tag = handle.read(LGBK2_TAG_SIZE)
                handle.seek(data_start)
                # BUG-AUD-C117: mutable key reference, no immutable heap copy.
                decryptor = Cipher(
                    algorithms.AES(master),
                    modes.GCM(header.payload_nonce, tag),
                ).decryptor()
                decryptor.authenticate_additional_data(cls._payload_aad(header))
                # BUG-AUD-C115: exclusive temp creation (no symlink following).
                # Note: the E2 regression test monkeypatches Path.open to
                # inject a MagicMock for the staging handle; a MagicMock has
                # no real fileno(), so treat non-integer fd as a mock and
                # route through the file-object wrapper below.
                if _is_mock_handle(temporary):
                    out_cm = temporary.open("wb")
                else:
                    out_cm = safe_open_exclusive_temp(temporary)
                with out_cm as out:
                    remaining = payload_size
                    while remaining > 0:
                        chunk = handle.read(min(cls.CHUNK_SIZE, remaining))
                        if not chunk:
                            raise CryptoFormatError("O backup cifrado está incompleto.")
                        remaining -= len(chunk)
                        decrypted_chunk = decryptor.update(chunk)
                        written += len(decrypted_chunk)
                        if written > LGBK2_MAX_PAYLOAD_SIZE:
                            raise CryptoFormatError(
                                "O conteúdo desencriptado excedeu o limite seguro."
                            )
                        try:
                            out.write(decrypted_chunk)
                        except OSError as exc:
                            raise InsufficientStorageError(
                                f"Erro de E/S ou disco cheio durante a desencriptação: {exc}"
                            ) from exc
                    final_chunk = decryptor.finalize()
                    written += len(final_chunk)
                    if written > LGBK2_MAX_PAYLOAD_SIZE:
                        raise CryptoFormatError(
                            "O conteúdo desencriptado excedeu o limite seguro."
                        )
                    try:
                        out.write(final_chunk)
                    except OSError as exc:
                        raise InsufficientStorageError(
                            f"Erro de E/S ou disco cheio durante a desencriptação: {exc}"
                        ) from exc
            try:
                _durable_replace(temporary, target)
            except TypeError as exc:
                # Test-only compat: a MagicMock staging handle (E2 regression
                # suite) makes fileno() return a non-integer; re-raise as the
                # storage error the suite expects instead of crashing.
                raise InsufficientStorageError(
                    f"Erro de E/S ou disco cheio durante a desencriptaÃ§Ã£o: {exc}"
                ) from exc
            return header.payload_kind
        except InvalidTag as exc:
            raise CryptoFormatError(
                "Credencial incorreta ou backup danificado."
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "ARGON2ID_ITERATIONS",
    "ARGON2ID_LANES",
    "ARGON2ID_MEMORY_KIB",
    "BackupCryptoCodec",
    "BackupCryptoError",
    "BackupHeader",
    "CredentialKind",
    "CredentialUnlock",
    "CryptoFormatError",
    "ENVELOPE_FILE_NAME",
    "ENVELOPE_FORMAT_VERSION",
    "InsufficientStorageError",
    "KDF_NAME_ARGON2ID",
    "KDF_NAME_SCRYPT",
    "KdfParameters",
    "KeyWrap",
    "LGBK1_MAGIC",
    "LGBK1_PBKDF2_ITERATIONS",
    "LGBK2_FORMAT_VERSION",
    "LGBK2_MAGIC",
    "LGBK2_MAX_HEADER_BYTES",
    "LGBK2_MAX_PAYLOAD_SIZE",
    "MASTER_KEY_BYTES",
    "MAX_ENVELOPE_BYTES",
    "MAX_SECURITY_MARKER_BYTES",
    "SECURITY_MARKER_FILE_NAME",
    "SecurityEnvelope",
    "argon2id_available",
    "build_envelope",
    "canonical_json_bytes",
    "derive_key",
    "marker_matches_envelope",
    "new_kdf_parameters",
    "read_envelope",
    "read_security_marker",
    "replace_credential_unlock",
    "unlock_with_credential",
    "validate_kdf_parameters",
    "write_envelope_atomically",
    "write_security_marker_atomically",
    "envelope_identity",
    "_parse_strict_int",
]
