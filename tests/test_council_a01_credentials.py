"""Council A01 / QA-16: tests must never use persistent OS credentials."""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from services import secret_store
from services.ai import AIService
from services.backup import BackupService
from services.secret_store import (
    BACKUP_CREDENTIAL_TARGET,
    GEMINI_CREDENTIAL_TARGET,
    MemorySecretStore,
    SecretStoreError,
    WindowsCredentialStore,
    default_secret_store,
)


@pytest.fixture
def reject_native_loader(monkeypatch):
    def reject(*_args, **_kwargs):
        pytest.fail("A test attempted to load the Windows credential API")

    monkeypatch.setattr(ctypes, "WinDLL", reject, raising=False)


@pytest.mark.parametrize("target", [GEMINI_CREDENTIAL_TARGET, BACKUP_CREDENTIAL_TARGET])
def test_default_store_is_ephemeral_without_native_api(target, reject_native_loader):
    store = default_secret_store(target)
    assert isinstance(store, MemorySecretStore)
    assert store.read() == ""
    store.write("  senha sintética ç  ")
    assert store.read() == "  senha sintética ç  "
    # New services cannot observe secrets from another test/profile instance.
    assert default_secret_store(target).read() == ""
    store.delete()
    assert store.read() == ""


def test_native_store_is_blocked_before_loading_dll(reject_native_loader):
    with pytest.raises(SecretStoreError, match="bloqueado nos testes"):
        WindowsCredentialStore()


def test_unsetting_testing_flag_cannot_enable_native_store(
    monkeypatch, reject_native_loader
):
    monkeypatch.delenv("FINANCAS_PESSOAIS_TESTING")
    assert isinstance(default_secret_store(), MemorySecretStore)
    with pytest.raises(SecretStoreError, match="bloqueado nos testes"):
        WindowsCredentialStore()


@pytest.mark.parametrize(
    "method,args", [("read", ()), ("write", ("test",)), ("delete", ())]
)
def test_preexisting_native_store_cannot_access_credentials_in_tests(method, args):
    # Simulate an instance created before the test flag was enabled. No DLL is
    # attached: a method that forgets its guard fails before any native access.
    store = object.__new__(WindowsCredentialStore)
    with pytest.raises(SecretStoreError, match="bloqueado nos testes"):
        getattr(store, method)(*args)


def test_backup_and_ai_defaults_use_separate_memory_stores(
    tmp_path, reject_native_loader
):
    repo = SimpleNamespace(db=SimpleNamespace(path=tmp_path / "synthetic.sqlite3"))
    backup = BackupService(repo)
    ai = AIService(
        repo,
        backup_service=backup,
        analytics_service=object(),
        payments_service=object(),
    )
    stores = [backup.secret_store, backup.password_history_store, ai.secret_store]
    assert all(isinstance(store, MemorySecretStore) for store in stores)
    assert len({id(store) for store in stores}) == 3
    backup.secret_store.write("synthetic backup password")
    assert backup.password_history_store.read() == ""
    assert ai.secret_store.read() == ""


def test_explicit_stores_remain_injected(tmp_path, reject_native_loader):
    repo = SimpleNamespace(db=SimpleNamespace(path=tmp_path / "synthetic.sqlite3"))
    password = MemorySecretStore("backup")
    history = MemorySecretStore("history")
    api = MemorySecretStore("api")
    backup = BackupService(repo, secret_store=password, password_history_store=history)
    ai = AIService(
        repo,
        secret_store=api,
        backup_service=backup,
        analytics_service=object(),
        payments_service=object(),
    )
    assert backup.secret_store is password
    assert backup.password_history_store is history
    assert ai.secret_store is api


def test_production_factory_selects_native_store_without_calling_native_api(
    monkeypatch,
):
    created = []

    def fake_native(target):
        created.append(target)
        return MemorySecretStore("fake native")

    monkeypatch.setattr(secret_store, "_TESTING_PROCESS", False)
    monkeypatch.setenv("FINANCAS_PESSOAIS_TESTING", "0")
    monkeypatch.setattr(secret_store, "WindowsCredentialStore", fake_native)
    assert default_secret_store(BACKUP_CREDENTIAL_TARGET).read() == "fake native"
    assert created == [BACKUP_CREDENTIAL_TARGET]


def test_native_utf16_roundtrip_uses_fake_backend_and_preserves_spaces(monkeypatch):
    # Exercise the real serializer with an injected API double. The native
    # loader and Credential Manager are never called, even on Windows hosts.
    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    class FakeAdvapi:
        encoded = b""

        def __init__(self):
            self.CredWriteW = Function(self.write)
            self.CredReadW = Function(self.read)
            self.CredDeleteW = Function(self.delete)
            self.CredFree = Function(lambda *_args: None)

        def write(self, pointer, _flags):
            value = ctypes.cast(
                pointer, ctypes.POINTER(secret_store._CREDENTIALW)
            ).contents
            self.encoded = ctypes.string_at(
                value.CredentialBlob, value.CredentialBlobSize
            )
            return True

        def read(self, _target, _kind, _flags, result):
            self.blob = (ctypes.c_ubyte * len(self.encoded)).from_buffer_copy(
                self.encoded
            )
            self.credential = secret_store._CREDENTIALW()
            self.credential.CredentialBlobSize = len(self.encoded)
            self.credential.CredentialBlob = ctypes.cast(
                self.blob, ctypes.POINTER(ctypes.c_ubyte)
            )
            output = ctypes.cast(result, ctypes.POINTER(secret_store._PCREDENTIALW))
            output[0] = ctypes.pointer(self.credential)
            return True

        def delete(self, *_args):
            self.encoded = b""
            return True

    backend = FakeAdvapi()
    store = object.__new__(WindowsCredentialStore)
    store.target_name = "synthetic-only"
    store._advapi32 = backend
    monkeypatch.setattr(store, "_guard_native_access", lambda: None)
    secret = "  senha com espaços ç 🔑  "
    store.write(secret)
    assert backend.encoded == secret.encode("utf-16-le")
    assert store.read() == secret
    assert bytes(backend.blob) == bytes(len(backend.blob))
    store.delete()
    assert backend.encoded == b""
