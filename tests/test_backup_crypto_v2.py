"""LGBK2 codec, envelope tamper and LGBK1 compatibility tests."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from services.backup_crypto import (
    ARGON2ID_ITERATIONS,
    ARGON2ID_LANES,
    ARGON2ID_MEMORY_KIB,
    BackupCryptoCodec,
    BackupCryptoError,
    CredentialKind,
    CryptoFormatError,
    KdfParameters,
    LGBK1_PBKDF2_ITERATIONS,
    LGBK2_MAGIC,
    LGBK2_MAX_HEADER_BYTES,
    SecurityEnvelope,
    build_envelope,
    read_envelope,
    replace_credential_unlock,
    unlock_with_credential,
    validate_kdf_parameters,
    write_envelope_atomically,
)

PROFILE_ID = "22222222-2222-2222-2222-222222222222"
PASSWORD = b"correct horse battery staple"
PIN = b"1234567890"


@pytest.fixture()
def envelope() -> SecurityEnvelope:
    master = bytearray(range(32))
    return build_envelope(
        profile_id=PROFILE_ID,
        default_unlock_method=CredentialKind.PASSWORD,
        master_key=master,
        credentials={"password": PASSWORD, "pin": PIN},
    )


@pytest.fixture()
def master_key() -> bytearray:
    return bytearray(range(32))


def _payload(tmp_path: Path, size: int = 5000) -> Path:
    source = tmp_path / "payload.zip"
    source.write_bytes(b"PAYLOAD-" + bytes(range(256)) * (size // 256))
    return source


def _encrypt(
    tmp_path: Path,
    source: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> Path:
    target = tmp_path / "backup.lgbak"
    BackupCryptoCodec.encrypt_v2(
        source,
        target,
        master_key=bytes(master_key),
        envelope=envelope,
        payload_kind="Z",
    )
    return target


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


def test_lgbk2_roundtrip_with_password(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    out = tmp_path / "out.zip"
    kind = BackupCryptoCodec.decrypt(
        target,
        out,
        credential=PASSWORD,
        credential_kind="password",
    )
    assert kind == "Z"
    assert out.read_bytes() == source.read_bytes()


def test_lgbk2_roundtrip_with_pin(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    out = tmp_path / "out.zip"
    kind = BackupCryptoCodec.decrypt(
        target,
        out,
        credential=PIN,
        credential_kind="pin",
    )
    assert kind == "Z"
    assert out.read_bytes() == source.read_bytes()


def test_lgbk2_roundtrip_with_session_key(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    out = tmp_path / "out.zip"
    kind = BackupCryptoCodec.decrypt(
        target,
        out,
        session_master_key=bytes(master_key),
    )
    assert kind == "Z"
    assert out.read_bytes() == source.read_bytes()


def test_lgbk2_auto_discovers_credential_kind(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    out = tmp_path / "out.zip"
    kind = BackupCryptoCodec.decrypt(target, out, credential=PIN)
    assert kind == "Z"


# ---------------------------------------------------------------------------
# Header structure
# ---------------------------------------------------------------------------


def test_header_contains_portable_wraps_and_kdf(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    header = BackupCryptoCodec.inspect_header(target)
    assert header.format_version == 2
    assert header.profile_id == PROFILE_ID
    assert header.payload_kind == "Z"
    assert set(header.unlocks) == {"pin", "password"}
    assert header.default_unlock_method == "password"
    for unlock in header.unlocks.values():
        assert unlock.kdf.name in {"argon2id", "scrypt"}
        assert unlock.kdf.iterations >= 1
        assert len(unlock.kdf.salt) >= 16


def test_oversized_header_rejected(tmp_path: Path) -> None:
    target = tmp_path / "bad.lgbak"
    huge_header = b"x" * (LGBK2_MAX_HEADER_BYTES + 1)
    target.write_bytes(
        LGBK2_MAGIC + b"Z" + struct.pack(">I", len(huge_header)) + huge_header
    )
    with pytest.raises(CryptoFormatError):
        BackupCryptoCodec.inspect_header(target)


def test_unknown_kdf_rejected(tmp_path: Path) -> None:
    params = KdfParameters(
        name="bcrypt",
        salt=bytes(16),
        memory_kib=ARGON2ID_MEMORY_KIB,
        iterations=ARGON2ID_ITERATIONS,
        lanes=ARGON2ID_LANES,
    )
    with pytest.raises(CryptoFormatError):
        validate_kdf_parameters(params)


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_kib": 8 * 1024},  # below 16 MiB floor
        {"memory_kib": 300 * 1024},  # above 256 MiB ceiling
        {"iterations": 0},
        {"iterations": 11},
        {"lanes": 0},
        {"lanes": 17},
        {"length": 16},
    ],
)
def test_untrusted_kdf_parameters_rejected(overrides: dict) -> None:
    values = {
        "name": "argon2id",
        "salt": bytes(16),
        "memory_kib": ARGON2ID_MEMORY_KIB,
        "iterations": ARGON2ID_ITERATIONS,
        "lanes": ARGON2ID_LANES,
        "length": 32,
    }
    values.update(overrides)
    params = KdfParameters(**values)
    with pytest.raises(CryptoFormatError):
        validate_kdf_parameters(params)


def test_short_salt_rejected() -> None:
    params = KdfParameters(
        name="argon2id",
        salt=bytes(8),
        memory_kib=ARGON2ID_MEMORY_KIB,
        iterations=ARGON2ID_ITERATIONS,
        lanes=ARGON2ID_LANES,
        length=32,
    )
    with pytest.raises(CryptoFormatError):
        validate_kdf_parameters(params)


# ---------------------------------------------------------------------------
# Tamper / failure modes
# ---------------------------------------------------------------------------


def test_tampered_payload_rejected(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    raw = bytearray(target.read_bytes())
    raw[-1] ^= 0x01
    target.write_bytes(bytes(raw))
    with pytest.raises(CryptoFormatError):
        BackupCryptoCodec.decrypt(
            target,
            tmp_path / "out.zip",
            credential=PASSWORD,
        )


def test_tampered_header_rejected(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    raw = bytearray(target.read_bytes())
    header_length = struct.unpack(">I", raw[6:10])[0]
    raw[10] ^= 0x01  # flip a byte inside the canonical JSON header
    target.write_bytes(bytes(raw))
    with pytest.raises((CryptoFormatError, ValueError)):
        BackupCryptoCodec.decrypt(
            target,
            tmp_path / "out.zip",
            credential=PASSWORD,
            credential_kind="password",
        )
    assert header_length > 0


def test_kdf_downgrade_rejected(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    raw = bytearray(target.read_bytes())
    header_length = struct.unpack(">I", raw[6:10])[0]
    header = json.loads(bytes(raw[10 : 10 + header_length]).decode("utf-8"))
    header["unlocks"]["pin"]["kdf"]["iterations"] = 1
    header["unlocks"]["pin"]["kdf"]["memory_kib"] = 16 * 1024
    forged = json.dumps(
        header, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    target.write_bytes(
        bytes(raw[:6])
        + struct.pack(">I", len(forged))
        + forged
        + bytes(raw[10 + header_length :])
    )
    with pytest.raises((CryptoFormatError, ValueError)):
        BackupCryptoCodec.decrypt(
            target,
            tmp_path / "out.zip",
            credential=PIN,
            credential_kind="pin",
        )


def test_wrong_credential_rejected(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    with pytest.raises((CryptoFormatError, BackupCryptoError)):
        BackupCryptoCodec.decrypt(
            target,
            tmp_path / "out.zip",
            credential=b"wrong credential",
        )


def test_truncated_payload_rejected(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    raw = target.read_bytes()
    target.write_bytes(raw[: len(raw) - 8])  # strip tag + part of ciphertext
    with pytest.raises((CryptoFormatError, ValueError)):
        BackupCryptoCodec.decrypt(
            target,
            tmp_path / "out.zip",
            session_master_key=bytes(master_key),
        )


def test_unknown_magic_rejected(tmp_path: Path) -> None:
    source = tmp_path / "mystery.lgbak"
    source.write_bytes(b"NOPE!" + bytes(64))
    with pytest.raises(CryptoFormatError):
        BackupCryptoCodec.decrypt(
            source,
            tmp_path / "out.zip",
            credential=b"whatever",
        )


def test_missing_session_and_credential_rejected(
    tmp_path: Path,
    envelope: SecurityEnvelope,
    master_key: bytearray,
) -> None:
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master_key)
    with pytest.raises(BackupCryptoError):
        BackupCryptoCodec.decrypt(target, tmp_path / "out.zip")


# ---------------------------------------------------------------------------
# LGBK1 compatibility
# ---------------------------------------------------------------------------


def test_lgbk1_derivation_matches_frozen_parameters() -> None:
    from services.backup import BackupService

    salt = bytes(range(16))
    legacy = BackupService._derive_encryption_key("pw", salt)
    codec = BackupCryptoCodec.derive_lgbk1_key(b"pw", salt)
    assert bytes(legacy) == bytes(codec)


def test_lgbk1_iterations_frozen() -> None:
    assert LGBK1_PBKDF2_ITERATIONS == 390_000


def test_lgbk1_file_decrypts_through_codec(tmp_path: Path) -> None:
    from services.backup import BackupService

    source = _payload(tmp_path)
    legacy = tmp_path / "legacy.lgbak"
    BackupService.encrypt_file(
        source,
        legacy,
        "legacy password",
        payload_kind="Z",
    )
    out = tmp_path / "legacy_out.zip"
    kind = BackupCryptoCodec.decrypt(
        legacy,
        out,
        credential=b"legacy password",
    )
    assert kind == "Z"
    assert out.read_bytes() == source.read_bytes()


def test_lgbk1_wrong_password_rejected(tmp_path: Path) -> None:
    from services.backup import BackupService

    source = _payload(tmp_path)
    legacy = tmp_path / "legacy.lgbak"
    BackupService.encrypt_file(source, legacy, "legacy password", payload_kind="Z")
    with pytest.raises(CryptoFormatError):
        BackupCryptoCodec.decrypt(
            legacy,
            tmp_path / "out.zip",
            credential=b"incorrect password",
        )


# ---------------------------------------------------------------------------
# Envelope persistence and rotation
# ---------------------------------------------------------------------------


def test_envelope_atomic_write_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "security_envelope.json"
    envelope = build_envelope(
        profile_id=PROFILE_ID,
        default_unlock_method=CredentialKind.PASSWORD,
        master_key=bytearray(range(32)),
        credentials={"password": PASSWORD},
    )
    write_envelope_atomically(path, envelope)
    loaded = read_envelope(path)
    assert loaded.profile_id == envelope.profile_id
    assert loaded.default_unlock_method == "password"
    master = unlock_with_credential(
        loaded.profile_id,
        "password",
        loaded.unlocks["password"],
        PASSWORD,
    )
    assert bytes(master) == bytes(range(32))


def test_rotation_preserves_master_key_and_slots(tmp_path: Path) -> None:
    master = bytearray(range(32))
    envelope = build_envelope(
        profile_id=PROFILE_ID,
        default_unlock_method=CredentialKind.PASSWORD,
        master_key=master,
        credentials={"password": PASSWORD, "pin": PIN},
    )
    new_pin = b"9876543210"
    updated = replace_credential_unlock(
        envelope,
        kind="pin",
        current_secret=PIN,
        new_secret=new_pin,
    )
    assert bytes(updated.unlocks["pin"].key_wrap.ciphertext) != bytes(
        envelope.unlocks["pin"].key_wrap.ciphertext
    )
    # Same master key under the new PIN...
    master_new = unlock_with_credential(
        updated.profile_id,
        "pin",
        updated.unlocks["pin"],
        new_pin,
    )
    assert bytes(master_new) == bytes(range(32))
    # ...and the untouched password slot still works.
    master_pwd = unlock_with_credential(
        updated.profile_id,
        "password",
        updated.unlocks["password"],
        PASSWORD,
    )
    assert bytes(master_pwd) == bytes(range(32))
    # The old PIN no longer unlocks.
    with pytest.raises(BackupCryptoError):
        unlock_with_credential(
            updated.profile_id,
            "pin",
            updated.unlocks["pin"],
            PIN,
        )


def test_lgbk2_files_never_reuse_lgbk1_magic(tmp_path: Path) -> None:
    master = bytearray(range(32))
    envelope = build_envelope(
        profile_id=PROFILE_ID,
        default_unlock_method=CredentialKind.PASSWORD,
        master_key=master,
        credentials={"password": PASSWORD},
    )
    source = _payload(tmp_path)
    target = _encrypt(tmp_path, source, envelope, master)
    assert target.read_bytes()[:5] == LGBK2_MAGIC
    assert BackupCryptoCodec.detect_magic(target) != b"LGBK1"
