"""Secure local storage for the user's own API keys.

On Windows the key is kept in the Windows Credential Manager ("Generic
credential"), which stores it encrypted for the signed-in Windows user. It is
never written to settings.json, the log, the translation cache or anywhere in
the application folder. Users can also see or delete it themselves in
Control Panel > Credential Manager > Windows Credentials.

On other systems (development only) a non-persistent in-memory store is used.
"""

from __future__ import annotations

import logging
import sys
import threading

from app import APP_ID

log = logging.getLogger(__name__)

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2  # survives restarts; still private to this Windows user
_ERROR_NOT_FOUND = 1168
_MAX_BLOB_BYTES = 2560


class CredentialStoreError(Exception):
    """The operating system refused to read or write a credential."""


class SecretStore:
    """Interface: get/set/delete a secret by name."""

    persistent = False
    description = ""

    def get(self, name: str) -> str | None:
        raise NotImplementedError

    def set(self, name: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> bool:
        raise NotImplementedError


class MemorySecretStore(SecretStore):
    """Keeps secrets only while the program runs (tests, non-Windows)."""

    description = "memory only (not saved)"

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> str | None:
        with self._lock:
            return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        with self._lock:
            self._values[name] = value

    def delete(self, name: str) -> bool:
        with self._lock:
            return self._values.pop(name, None) is not None


class WindowsCredentialStore(SecretStore):
    """Windows Credential Manager via advapi32 (CredWriteW / CredReadW / CredDeleteW)."""

    persistent = True
    description = "Windows Credential Manager"

    def __init__(self, prefix: str = APP_ID):
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self._ctypes = ctypes
        self._CREDENTIALW = CREDENTIALW
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._write = advapi32.CredWriteW
        self._write.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self._write.restype = wintypes.BOOL
        self._read = advapi32.CredReadW
        self._read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        self._read.restype = wintypes.BOOL
        self._delete = advapi32.CredDeleteW
        self._delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._delete.restype = wintypes.BOOL
        self._free = advapi32.CredFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None
        self._prefix = prefix

    def target(self, name: str) -> str:
        return f"{self._prefix}/{name}"

    def get(self, name: str) -> str | None:
        ctypes = self._ctypes
        pointer = ctypes.POINTER(self._CREDENTIALW)()
        if not self._read(self.target(name), _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError(f"Windows could not read the saved key (error {error}).")
        try:
            cred = pointer.contents
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        finally:
            self._free(pointer)
        return blob.decode("utf-8")

    def set(self, name: str, value: str) -> None:
        ctypes = self._ctypes
        data = value.encode("utf-8")
        if len(data) > _MAX_BLOB_BYTES:
            raise CredentialStoreError("The key is too long to be stored in Windows Credential Manager.")
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        cred = self._CREDENTIALW()
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = self.target(name)
        cred.Comment = "Saved by Media Toolkit (AI translation). Delete it here or in the app."
        cred.CredentialBlobSize = len(data)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = name
        if not self._write(ctypes.byref(cred), 0):
            raise CredentialStoreError(f"Windows could not save the key (error {ctypes.get_last_error()}).")

    def delete(self, name: str) -> bool:
        ctypes = self._ctypes
        if self._delete(self.target(name), _CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return False
        raise CredentialStoreError(f"Windows could not delete the saved key (error {error}).")


def default_store() -> SecretStore:
    if sys.platform == "win32":
        try:
            return WindowsCredentialStore()
        except (OSError, AttributeError):
            log.warning("Windows Credential Manager is not available; keys will not be saved", exc_info=True)
    return MemorySecretStore()


def mask_secret(value: str | None) -> str:
    """'AIzaSyD...xyz9' style preview - never the whole key."""
    if not value:
        return ""
    if len(value) <= 12:
        return "•" * 8
    return f"{value[:4]}{'•' * 8}{value[-4:]}"
