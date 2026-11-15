"""Secure secret storage backed by Windows Credential Manager."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Protocol

GEMINI_CREDENTIAL_TARGET = "FinancasPessoaisLiquidGlass/GeminiAPIKey"
BACKUP_CREDENTIAL_TARGET = "FinancasPessoaisLiquidGlass/BackupPassword"
BACKUP_PASSWORD_HISTORY_TARGET = "FinancasPessoaisLiquidGlass/BackupPasswordHistory"

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512
# A test process cannot regain native credential access by unsetting its flag.
_TESTING_PROCESS = os.environ.get("FINANCAS_PESSOAIS_TESTING") == "1"


class SecretStoreError(RuntimeError):
    """Raised when the operating-system secret store cannot complete an action."""


class SecretStore(Protocol):
    def read(self) -> str: ...

    def write(self, secret: str) -> None: ...

    def delete(self) -> None: ...


class MemorySecretStore:
    """Non-persistent secret store for isolated tests and explicit injection."""

    def __init__(self, secret: str = "") -> None:
        self._secret = str(secret or "")

    def read(self) -> str:
        return self._secret

    def write(self, secret: str) -> None:
        self._secret = str(secret or "")

    def delete(self) -> None:
        self._secret = ""


def _testing_process() -> bool:
    return _TESTING_PROCESS or os.environ.get("FINANCAS_PESSOAIS_TESTING") == "1"


def default_secret_store(target_name: str = GEMINI_CREDENTIAL_TARGET) -> SecretStore:
    """Choose an ephemeral test store without ever loading the native API."""
    if _testing_process():
        return MemorySecretStore()
    return WindowsCredentialStore(target_name)


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", wintypes.LPVOID),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


class WindowsCredentialStore:
    """Store one UTF-16 secret in the current user's Windows credentials."""

    def __init__(self, target_name: str = GEMINI_CREDENTIAL_TARGET):
        self._guard_native_access()
        if os.name != "nt":
            raise SecretStoreError(
                "O Gestor de Credenciais só está disponível no Windows."
            )
        self.target_name = str(target_name or "").strip()
        if not self.target_name:
            raise SecretStoreError("O identificador da credencial está vazio.")

        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi32.CredWriteW.argtypes = [
            ctypes.POINTER(_CREDENTIALW),
            wintypes.DWORD,
        ]
        self._advapi32.CredWriteW.restype = wintypes.BOOL
        self._advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_PCREDENTIALW),
        ]
        self._advapi32.CredReadW.restype = wintypes.BOOL
        self._advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._advapi32.CredDeleteW.restype = wintypes.BOOL
        self._advapi32.CredFree.argtypes = [wintypes.LPVOID]
        self._advapi32.CredFree.restype = None

    @staticmethod
    def _guard_native_access() -> None:
        if _testing_process():
            raise SecretStoreError(
                "O acesso a credenciais do Windows está bloqueado nos testes."
            )

    @staticmethod
    def _raise_last_error(action: str) -> None:
        code = ctypes.get_last_error()
        if code == 5:  # ERROR_ACCESS_DENIED
            hint = " (Acesso negado: a conta de utilizador do Windows não tem permissões para persistir credenciais locais)."
        elif code == 1359:  # ERROR_INTERNAL_ERROR
            hint = " (Erro interno do subsistema de segurança do Windows)."
        else:
            hint = f" (código de erro {code})."
        raise SecretStoreError(f"{action} falhou no Gestor de Credenciais{hint}")

    def read(self) -> str:
        self._guard_native_access()
        pointer = _PCREDENTIALW()
        if not self._advapi32.CredReadW(
            self.target_name,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return ""
            self._raise_last_error("Ler a credencial")
        try:
            credential = pointer.contents
            size = int(credential.CredentialBlobSize or 0)
            if size <= 0 or not credential.CredentialBlob:
                return ""
            raw = ctypes.string_at(credential.CredentialBlob, size)
            return raw.decode("utf-16-le")
        except (UnicodeDecodeError, ValueError) as exc:
            raise SecretStoreError("A credencial guardada está corrompida.") from exc
        finally:
            try:
                if (
                    pointer
                    and pointer.contents
                    and pointer.contents.CredentialBlob
                    and pointer.contents.CredentialBlobSize
                ):
                    ctypes.memset(
                        pointer.contents.CredentialBlob,
                        0,
                        pointer.contents.CredentialBlobSize,
                    )
            except Exception:
                pass
            self._advapi32.CredFree(ctypes.cast(pointer, wintypes.LPVOID))

    def write(self, secret: str) -> None:
        self._guard_native_access()
        # Preserve the secret exactly: leading/trailing spaces and Unicode are
        # significant. Never strip/lower/normalize stored credentials.
        secret = str(secret or "")
        if secret == "":
            self.delete()
            return
        encoded = secret.encode("utf-16-le")
        if len(encoded) > _MAX_CREDENTIAL_BLOB_SIZE:
            raise SecretStoreError(
                "A chave é demasiado longa para o Gestor de Credenciais."
            )

        target_suffix = (
            self.target_name.split("/")[-1]
            if "/" in self.target_name
            else self.target_name
        )
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = self.target_name
        credential.Comment = f"Finanças Pessoais Liquid Glass - {target_suffix}"
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = target_suffix

        try:
            if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
                self._raise_last_error("Guardar a credencial")
        finally:
            ctypes.memset(ctypes.byref(blob), 0, len(encoded))

    def delete(self) -> None:
        self._guard_native_access()
        if self._advapi32.CredDeleteW(
            self.target_name,
            _CRED_TYPE_GENERIC,
            0,
        ):
            return
        error = ctypes.get_last_error()
        if error != _ERROR_NOT_FOUND:
            self._raise_last_error("Eliminar a credencial")
