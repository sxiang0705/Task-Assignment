"""Windows Credential Manager storage for the Gmail refresh token."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from task_assignment.infrastructure.gmail.contracts import (
    CredentialRecord,
    CredentialStoreError,
)

CREDENTIAL_TARGET = "TaskAssignment/GmailOAuth"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Persist one refresh token outside SQLite and ordinary app files."""

    def __init__(self, target: str = CREDENTIAL_TARGET) -> None:
        self.target = target

    def read(self) -> CredentialRecord | None:
        api = _credential_api()
        pointer = ctypes.POINTER(_Credential)()
        if not api.CredReadW(self.target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError("無法讀取 Windows 中保存的 Gmail 授權。")
        try:
            credential = pointer.contents
            raw = ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
            token = raw.decode("utf-8")
            account = credential.UserName or ""
            if not token or not account:
                raise CredentialStoreError("Windows 中保存的 Gmail 授權不完整。")
            return CredentialRecord(account_email=account, refresh_token=token)
        except UnicodeDecodeError as exc:
            raise CredentialStoreError("Windows 中保存的 Gmail 授權無法讀取。") from exc
        finally:
            api.CredFree(pointer)

    def write(self, record: CredentialRecord) -> None:
        if not record.account_email or not record.refresh_token:
            raise CredentialStoreError("Gmail 授權內容不完整，無法安全保存。")
        api = _credential_api()
        raw = record.refresh_token.encode("utf-8")
        blob = ctypes.create_string_buffer(raw)
        credential = _Credential()
        credential.Type = CRED_TYPE_GENERIC
        credential.TargetName = self.target
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(wintypes.BYTE))
        credential.Persist = CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = record.account_email
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialStoreError("無法將 Gmail 授權保存到 Windows。")

    def delete(self) -> None:
        api = _credential_api()
        if api.CredDeleteW(self.target, CRED_TYPE_GENERIC, 0):
            return
        if ctypes.get_last_error() != ERROR_NOT_FOUND:
            raise CredentialStoreError("無法從 Windows 移除 Gmail 授權。")


def _credential_api():
    if os.name != "nt":
        raise CredentialStoreError("Gmail 安全授權儲存目前只支援 Windows。")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


class MemoryCredentialStore:
    """In-memory test adapter that never touches a real credential vault."""

    def __init__(self, record: CredentialRecord | None = None) -> None:
        self.record = record

    def read(self) -> CredentialRecord | None:
        return self.record

    def write(self, record: CredentialRecord) -> None:
        self.record = record

    def delete(self) -> None:
        self.record = None
