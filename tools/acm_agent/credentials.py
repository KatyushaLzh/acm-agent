"""OS-protected persistence for the user-supplied DeepSeek API key.

Only Windows DPAPI is supported. The persisted blob is scoped to the current
Windows logon user and never contains plaintext key bytes.

API contract sources:
- https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata
- https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import hmac
import os
from pathlib import Path
import tempfile
from typing import Callable


_FILE_MAGIC = b"ACM-DS-KEY-V1\0"
_INNER_MAGIC = b"acm-agent:deepseek:v1\0"
_ENTROPY = b"acm-agent/deepseek-key/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CredentialStoreError(RuntimeError):
    """A safe credential persistence failure that never embeds the API key."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_ubyte]]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _windows_libraries() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    if os.name != "nt":
        raise CredentialStoreError(
            "持久化 DeepSeek API Key 仅支持 Windows DPAPI；不会退化为明文存储。"
        )
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _dpapi_call(data: bytes, *, protect: bool) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    output_blob = _DataBlob()
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    try:
        if protect:
            succeeded = function(
                ctypes.byref(input_blob),
                "ACM Agent DeepSeek API Key",
                ctypes.byref(entropy_blob),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        else:
            succeeded = function(
                ctypes.byref(input_blob),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        if not succeeded:
            error = ctypes.get_last_error()
            operation = "加密" if protect else "解密"
            raise CredentialStoreError(f"Windows DPAPI {operation}失败（错误码 {error}）。")
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.memset(input_buffer, 0, len(input_buffer))
        ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
        if output_blob.pbData:
            ctypes.memset(output_blob.pbData, 0, output_blob.cbData)
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def protect_with_dpapi(data: bytes) -> bytes:
    return _dpapi_call(bytes(data), protect=True)


def unprotect_with_dpapi(data: bytes) -> bytes:
    return _dpapi_call(bytes(data), protect=False)


class DeepSeekCredentialStore:
    """Persist one API key as a versioned, integrity-checked DPAPI blob."""

    def __init__(
        self,
        path: str | Path,
        *,
        protect: Callable[[bytes], bytes] = protect_with_dpapi,
        unprotect: Callable[[bytes], bytes] = unprotect_with_dpapi,
    ) -> None:
        self.path = Path(path)
        self._protect = protect
        self._unprotect = unprotect

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @staticmethod
    def _encode(key: str) -> bytes:
        secret = key.encode("utf-8")
        return _INNER_MAGIC + hashlib.sha256(secret).digest() + secret

    @staticmethod
    def _decode(payload: bytes) -> str:
        header_size = len(_INNER_MAGIC) + hashlib.sha256().digest_size
        if len(payload) <= header_size or not payload.startswith(_INNER_MAGIC):
            raise CredentialStoreError("DeepSeek 凭据格式无效或已损坏。")
        digest = payload[len(_INNER_MAGIC) : header_size]
        secret = payload[header_size:]
        if not hmac.compare_digest(digest, hashlib.sha256(secret).digest()):
            raise CredentialStoreError("DeepSeek 凭据完整性校验失败。")
        try:
            key = secret.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialStoreError("DeepSeek 凭据不是有效的 UTF-8。") from exc
        if not key:
            raise CredentialStoreError("DeepSeek 凭据为空。")
        return key

    def save(self, key: str) -> None:
        encrypted = self._protect(self._encode(key))
        payload = _FILE_MAGIC + encrypted
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise CredentialStoreError("无法读取 DeepSeek 凭据文件。") from exc
        if not payload.startswith(_FILE_MAGIC) or len(payload) == len(_FILE_MAGIC):
            raise CredentialStoreError("DeepSeek 凭据文件格式无效。")
        try:
            decrypted = self._unprotect(payload[len(_FILE_MAGIC) :])
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreError("无法解密 DeepSeek 凭据。") from exc
        return self._decode(decrypted)

    def clear(self) -> bool:
        existed = self.path.exists()
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialStoreError("无法删除 DeepSeek 凭据文件。") from exc
        return existed


__all__ = [
    "CredentialStoreError",
    "DeepSeekCredentialStore",
    "protect_with_dpapi",
    "unprotect_with_dpapi",
]
