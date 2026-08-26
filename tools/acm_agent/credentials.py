"""OS-protected persistence for user-supplied provider API credentials.

Windows uses current-user DPAPI files.  macOS and Linux use explicit system
keyring backends (Keychain and Freedesktop Secret Service respectively).  No
platform may fall back to a plaintext credential file.

API contract sources:
- https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata
- https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import base64
import hashlib
import hmac
import os
from pathlib import Path
import tempfile
import json
import sys
import time
from uuid import UUID, uuid4
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from .provider_config import endpoint_origin, validate_auth, validate_identifier


_FILE_MAGIC = b"ACM-DS-KEY-V1\0"
_INNER_MAGIC = b"acm-agent:deepseek:v1\0"
_ENTROPY = b"acm-agent/deepseek-key/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_VAULT_FILE_MAGIC = b"ACM-PROVIDER-CREDENTIAL-V1\0"
_VAULT_INNER_MAGIC = b"acm-agent:provider-credential:v1\0"


class CredentialStoreError(RuntimeError):
    """A safe credential persistence failure that never embeds the API key."""

    def __init__(self, message: str, *, code: str = "credential_store_error") -> None:
        super().__init__(message)
        self.code = code


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


@dataclass(frozen=True, slots=True)
class ProviderCredential:
    slot: str
    provider_id: str
    origin: str
    auth: dict[str, str]
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class StagedProviderCredential:
    """Opaque, validated credential version awaiting an explicit commit."""

    slot: str
    credential: ProviderCredential = field(repr=False)
    _vault_id: int = field(repr=False)
    _temporary: Path | None = field(default=None, repr=False)
    _target: Path | None = field(default=None, repr=False)
    _version: str | None = field(default=None, repr=False)
    _previous_version: str | None = field(default=None, repr=False)
    _marker: Path | None = field(default=None, repr=False)


@runtime_checkable
class CredentialVault(Protocol):
    """Provider credential contract consumed by services and registries."""

    backend_name: str

    def stage(
        self,
        slot: str,
        secret: str,
        *,
        provider_id: str,
        origin: str,
        auth: dict[str, str],
    ) -> StagedProviderCredential: ...

    def commit(self, staged: StagedProviderCredential) -> ProviderCredential: ...

    def discard(self, staged: StagedProviderCredential) -> bool: ...

    def save(
        self,
        slot: str,
        secret: str,
        *,
        provider_id: str,
        origin: str,
        auth: dict[str, str],
    ) -> ProviderCredential: ...

    def load(self, slot: str) -> ProviderCredential | None: ...

    def load_bound(
        self,
        slot: str,
        *,
        provider_id: str,
        origin: str,
        auth: dict[str, str],
    ) -> ProviderCredential | None: ...

    def clear(self, slot: str) -> bool: ...

    def migrate_legacy_deepseek(self) -> bool: ...

    def secure_store_status(self) -> dict[str, object]: ...


class ProviderCredentialVault:
    """Named, origin-bound provider credentials encrypted with Windows DPAPI.

    The provider id, origin and authentication shape are inside the encrypted
    envelope.  A credential can therefore never be silently rebound to another
    endpoint by editing ``config.json``.
    """

    backend_name = "dpapi"

    def __init__(
        self,
        directory: str | Path,
        *,
        legacy_deepseek_path: str | Path | None = None,
        protect: Callable[[bytes], bytes] = protect_with_dpapi,
        unprotect: Callable[[bytes], bytes] = unprotect_with_dpapi,
    ) -> None:
        self.directory = Path(directory)
        self.legacy_deepseek_path = (
            Path(legacy_deepseek_path) if legacy_deepseek_path is not None else None
        )
        self._protect = protect
        self._unprotect = unprotect

    def secure_store_status(self) -> dict[str, object]:
        if os.name != "nt" and self._protect is protect_with_dpapi:
            return {
                "backend": "unavailable",
                "available": False,
                "error_code": "credential_store_unavailable",
                "message": "Windows DPAPI 在当前平台不可用。",
            }
        return {
            "backend": self.backend_name,
            "available": True,
            "error_code": None,
            "message": None,
        }

    def _path(self, slot: str) -> Path:
        selected = validate_identifier(slot, label="credential_slot")
        return self.directory / f"{selected}.dpapi"

    @staticmethod
    def _validated_secret(secret: str) -> str:
        if not isinstance(secret, str):
            raise CredentialStoreError("API Key 必须是字符串。")
        selected = secret.strip()
        if not selected:
            raise CredentialStoreError("API Key 不能为空。")
        if len(selected.encode("utf-8")) > 2048:
            raise CredentialStoreError("API Key 不能超过 2048 字节。")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in selected):
            raise CredentialStoreError("API Key 不能包含控制字符。")
        return selected

    @classmethod
    def _encode(
        cls,
        *,
        slot: str,
        provider_id: str,
        origin: str,
        auth: dict[str, str],
        secret: str,
    ) -> bytes:
        document = {
            "slot": validate_identifier(slot, label="credential_slot"),
            "provider_id": validate_identifier(provider_id, label="provider_id"),
            "origin": endpoint_origin(origin),
            "auth": validate_auth(auth),
            "secret": cls._validated_secret(secret),
        }
        serialized = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return _VAULT_INNER_MAGIC + hashlib.sha256(serialized).digest() + serialized

    @staticmethod
    def _decode(payload: bytes) -> ProviderCredential:
        header_size = len(_VAULT_INNER_MAGIC) + hashlib.sha256().digest_size
        if len(payload) <= header_size or not payload.startswith(_VAULT_INNER_MAGIC):
            raise CredentialStoreError("Provider 凭据格式无效或已损坏。")
        digest = payload[len(_VAULT_INNER_MAGIC) : header_size]
        serialized = payload[header_size:]
        if not hmac.compare_digest(digest, hashlib.sha256(serialized).digest()):
            raise CredentialStoreError("Provider 凭据完整性校验失败。")
        try:
            document = json.loads(serialized.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("Provider 凭据内容无效。") from exc
        if not isinstance(document, dict):
            raise CredentialStoreError("Provider 凭据内容无效。")
        try:
            return ProviderCredential(
                slot=validate_identifier(document.get("slot"), label="credential_slot"),
                provider_id=validate_identifier(document.get("provider_id"), label="provider_id"),
                origin=endpoint_origin(document.get("origin")),
                auth=validate_auth(document.get("auth")),
                secret=ProviderCredentialVault._validated_secret(document.get("secret")),
            )
        except Exception as exc:
            if isinstance(exc, CredentialStoreError):
                raise
            raise CredentialStoreError("Provider 凭据元数据无效。") from exc

    def stage(
        self,
        slot: str,
        secret: str,
        *,
        provider_id: str,
        origin: str,
        auth: dict[str, str],
    ) -> StagedProviderCredential:
        selected_slot = validate_identifier(slot, label="credential_slot")
        encoded = self._encode(
            slot=selected_slot,
            provider_id=provider_id,
            origin=origin,
            auth=auth,
            secret=secret,
        )
        encrypted = self._protect(encoded)
        path = self._path(selected_slot)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_VAULT_FILE_MAGIC + encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            # Validate the exact ciphertext staged for replacement.  A failed
            # DPAPI round trip must leave an existing credential untouched.
            staged = temporary.read_bytes()
            if not staged.startswith(_VAULT_FILE_MAGIC):
                raise CredentialStoreError("Provider 凭据临时文件格式无效。")
            try:
                loaded = self._decode(self._unprotect(staged[len(_VAULT_FILE_MAGIC) :]))
            except CredentialStoreError:
                raise
            except Exception as exc:
                raise CredentialStoreError("Provider 凭据写入前校验失败。") from exc
            if (
                loaded.slot != selected_slot
                or loaded.provider_id != validate_identifier(provider_id, label="provider_id")
                or loaded.origin != endpoint_origin(origin)
                or loaded.auth != validate_auth(auth)
                or not hmac.compare_digest(loaded.secret, self._validated_secret(secret))
            ):
                raise CredentialStoreError("Provider 凭据写入前校验失败。")
            return StagedProviderCredential(
                slot=selected_slot,
                credential=loaded,
                _temporary=temporary,
                _target=path,
                _vault_id=id(self),
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _validate_staged(self, staged: StagedProviderCredential) -> ProviderCredential:
        if not isinstance(staged, StagedProviderCredential) or staged._vault_id != id(self):
            raise CredentialStoreError("Provider 凭据 staging 不属于当前 vault。")
        expected_target = self._path(staged.slot)
        if staged._target != expected_target or staged._temporary.parent != expected_target.parent:
            raise CredentialStoreError("Provider 凭据 staging 路径无效。")
        try:
            payload = staged._temporary.read_bytes()
        except OSError as exc:
            raise CredentialStoreError("无法读取 Provider 凭据 staging 文件。") from exc
        if not payload.startswith(_VAULT_FILE_MAGIC):
            raise CredentialStoreError("Provider 凭据 staging 文件格式无效。")
        try:
            loaded = self._decode(self._unprotect(payload[len(_VAULT_FILE_MAGIC) :]))
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreError("Provider 凭据 staging 回读失败。") from exc
        expected = staged.credential
        if (
            loaded.slot != expected.slot
            or loaded.provider_id != expected.provider_id
            or loaded.origin != expected.origin
            or loaded.auth != expected.auth
            or not hmac.compare_digest(loaded.secret, expected.secret)
        ):
            raise CredentialStoreError("Provider 凭据 staging 回读不匹配。")
        return loaded

    def commit(self, staged: StagedProviderCredential) -> ProviderCredential:
        """Atomically replace the live blob after revalidating the staged bytes."""

        loaded = self._validate_staged(staged)
        try:
            os.replace(staged._temporary, staged._target)
        except OSError as exc:
            raise CredentialStoreError("无法提交 Provider 凭据 staging 文件。") from exc
        return loaded

    def discard(self, staged: StagedProviderCredential) -> bool:
        """Discard an uncommitted candidate without touching the live blob."""

        self._validate_staged(staged)
        try:
            staged._temporary.unlink()
        except OSError as exc:
            raise CredentialStoreError("无法清理 Provider 凭据 staging 文件。") from exc
        return True

    def save(
        self,
        slot: str,
        secret: str,
        *,
        provider_id: str,
        origin: str,
        auth: dict[str, str],
    ) -> ProviderCredential:
        staged = self.stage(
            slot, secret, provider_id=provider_id, origin=origin, auth=auth
        )
        try:
            return self.commit(staged)
        except Exception:
            staged._temporary.unlink(missing_ok=True)
            raise

    def load(self, slot: str) -> ProviderCredential | None:
        path = self._path(slot)
        if not path.exists():
            return None
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise CredentialStoreError("无法读取 Provider 凭据文件。") from exc
        if not payload.startswith(_VAULT_FILE_MAGIC) or len(payload) == len(_VAULT_FILE_MAGIC):
            raise CredentialStoreError("Provider 凭据文件格式无效。")
        try:
            decrypted = self._unprotect(payload[len(_VAULT_FILE_MAGIC) :])
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreError("无法解密 Provider 凭据。") from exc
        credential = self._decode(decrypted)
        if credential.slot != validate_identifier(slot, label="credential_slot"):
            raise CredentialStoreError("Provider 凭据槽位绑定不匹配。")
        return credential

    def load_bound(
        self,
        slot: str,
        *,
        provider_id: str,
        origin: str,
        auth: dict[str, str],
    ) -> ProviderCredential | None:
        credential = self.load(slot)
        if credential is None:
            return None
        expected = (
            validate_identifier(provider_id, label="provider_id"),
            endpoint_origin(origin),
            validate_auth(auth),
        )
        actual = (credential.provider_id, credential.origin, credential.auth)
        if actual != expected:
            raise CredentialStoreError(
                "Provider 凭据与当前 provider/origin/auth 配置不匹配；拒绝发送。"
            )
        return credential

    def migrate_legacy_deepseek(self) -> bool:
        """Copy the V1 DeepSeek key after verified encryption; keep rollback data."""

        def archive_legacy() -> None:
            assert self.legacy_deepseek_path is not None
            migrated = self.legacy_deepseek_path.with_suffix(
                self.legacy_deepseek_path.suffix + ".migrated"
            )
            try:
                if migrated.exists():
                    if migrated.read_bytes() != self.legacy_deepseek_path.read_bytes():
                        raise CredentialStoreError("旧 DeepSeek 凭据归档存在冲突；拒绝覆盖。")
                    self.legacy_deepseek_path.unlink()
                    return
                os.replace(self.legacy_deepseek_path, migrated)
            except CredentialStoreError:
                raise
            except OSError as exc:
                raise CredentialStoreError("旧 DeepSeek 凭据归档失败。") from exc

        if self.legacy_deepseek_path is None or not self.legacy_deepseek_path.exists():
            return False
        if self._path("deepseek").exists():
            self.load_bound(
                "deepseek",
                provider_id="deepseek",
                origin="https://api.deepseek.com",
                auth={"type": "bearer"},
            )
            archive_legacy()
            return True
        legacy = DeepSeekCredentialStore(
            self.legacy_deepseek_path,
            protect=self._protect,
            unprotect=self._unprotect,
        )
        secret = legacy.load()
        if secret is None:
            return False
        self.save(
            "deepseek",
            secret,
            provider_id="deepseek",
            origin="https://api.deepseek.com",
            auth={"type": "bearer"},
        )
        try:
            archive_legacy()
        except CredentialStoreError:
            self._path("deepseek").unlink(missing_ok=True)
            raise
        return True

    def status(self, slot: str) -> dict[str, object]:
        credential = self.load(slot)
        if credential is None:
            return {"slot": validate_identifier(slot, label="credential_slot"), "persisted": False}
        return {
            "slot": credential.slot,
            "persisted": True,
            "provider_id": credential.provider_id,
            "origin": credential.origin,
            "auth": dict(credential.auth),
        }

    def clear(self, slot: str) -> bool:
        path = self._path(slot)
        existed = path.exists()
        if validate_identifier(slot, label="credential_slot") == "deepseek" and self.legacy_deepseek_path is not None:
            try:
                self.legacy_deepseek_path.unlink(missing_ok=True)
                self.legacy_deepseek_path.with_suffix(
                    self.legacy_deepseek_path.suffix + ".migrated"
                ).unlink(missing_ok=True)
            except OSError as exc:
                raise CredentialStoreError("无法清理旧 DeepSeek 凭据文件。") from exc
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialStoreError("无法删除 Provider 凭据文件。") from exc
        return existed


_SYSTEM_KEYRING_SERVICE = "ACM Agent Provider Credentials"
_SYSTEM_POINTER_VERSION = 1


def _atomic_private_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        raise CredentialStoreError("无法原子写入系统凭据引用。") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _read_safe_json(path: Path, *, label: str) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialStoreError(f"{label}格式无效或已损坏。") from exc
    if not isinstance(document, dict):
        raise CredentialStoreError(f"{label}格式无效或已损坏。")
    return document


def _safe_document_version(document: dict[str, object], *, label: str) -> int:
    try:
        return int(document.get("version") or 0)
    except (TypeError, ValueError) as exc:
        raise CredentialStoreError(f"{label}版本无效或已损坏。") from exc


def _keyring_failure(exc: Exception, *, action: str) -> CredentialStoreError:
    name = type(exc).__name__.lower()
    details = str(exc).lower()
    if "locked" in name or "denied" in name or "auth" in name:
        return CredentialStoreError(
            f"系统安全凭据库已锁定或拒绝{action}；请先解锁后重试。",
            code="credential_store_locked",
        )
    if (
        any(marker in name for marker in (
            "init", "nokeyring", "runtime", "import", "module", "dbus", "secretstorage"
        ))
        or any(marker in details for marker in (
            "secret service", "secretservice", "d-bus", "dbus", "no recommended backend"
        ))
    ):
        return CredentialStoreError(
            f"系统安全凭据库不可用，无法{action}。",
            code="credential_store_unavailable",
        )
    return CredentialStoreError(f"系统安全凭据库{action}失败。")


class SystemKeyringCredentialVault:
    """Origin-bound provider credentials stored in Keychain/Secret Service.

    Keyring entries are immutable versions.  A non-secret, atomically replaced
    pointer selects the current version, so a failed candidate never destroys a
    known-good credential.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        keyring_backend: Any,
        backend_name: str,
        service_name: str = _SYSTEM_KEYRING_SERVICE,
    ) -> None:
        if backend_name not in {"keychain", "secret_service"}:
            raise ValueError("unsupported system credential backend")
        self.directory = Path(directory)
        self.workspace_id_path = self.directory.parent / "credential-vault-id"
        self.keyring_backend = keyring_backend
        self.backend_name = backend_name
        self.service_name = service_name
        self._startup_error: CredentialStoreError | None = None
        try:
            self.recover()
        except CredentialStoreError as exc:
            self._startup_error = exc

    def secure_store_status(self) -> dict[str, object]:
        error = self._startup_error
        return {
            "backend": self.backend_name,
            "available": error is None or error.code not in {
                "credential_store_unavailable",
                "credential_store_locked",
            },
            "error_code": error.code if error is not None else None,
            "message": str(error) if error is not None else None,
        }

    @staticmethod
    def _validated_secret(secret: str) -> str:
        return ProviderCredentialVault._validated_secret(secret)

    @staticmethod
    def _encode(**values: Any) -> bytes:
        return ProviderCredentialVault._encode(**values)

    @staticmethod
    def _decode(payload: bytes) -> ProviderCredential:
        return ProviderCredentialVault._decode(payload)

    def _workspace_id(self, *, create: bool) -> str | None:
        if not self.workspace_id_path.exists():
            if not create:
                return None
            selected = str(uuid4())
            _atomic_private_json(
                self.workspace_id_path,
                {"version": _SYSTEM_POINTER_VERSION, "workspace_id": selected},
            )
            return selected
        document = _read_safe_json(self.workspace_id_path, label="系统凭据 workspace 标识")
        try:
            selected = str(UUID(str(document.get("workspace_id") or "")))
        except (ValueError, AttributeError) as exc:
            raise CredentialStoreError("系统凭据 workspace 标识无效。") from exc
        if _safe_document_version(
            document, label="系统凭据 workspace 标识"
        ) != _SYSTEM_POINTER_VERSION:
            raise CredentialStoreError("系统凭据 workspace 标识版本不受支持。")
        return selected

    def _pointer_path(self, slot: str) -> Path:
        return self.directory / f"{validate_identifier(slot, label='credential_slot')}.keyring-ref"

    def _stage_path(self, slot: str, version: str) -> Path:
        return self.directory / f".{slot}.{version}.stage.json"

    def _gc_path(self, slot: str, version: str) -> Path:
        return self.directory / f".{slot}.{version}.gc.json"

    def _clear_path(self, slot: str, version: str) -> Path:
        return self.directory / f".{slot}.{version}.clear.json"

    @staticmethod
    def _account(workspace_id: str, slot: str, version: str) -> str:
        return f"{workspace_id}:{slot}:{version}"

    def _set_password(self, account: str, value: str) -> None:
        try:
            self.keyring_backend.set_password(self.service_name, account, value)
        except Exception as exc:
            error = _keyring_failure(exc, action="写入")
            self._startup_error = error
            raise error from exc

    def _get_password(self, account: str) -> str | None:
        try:
            value = self.keyring_backend.get_password(self.service_name, account)
        except Exception as exc:
            error = _keyring_failure(exc, action="读取")
            self._startup_error = error
            raise error from exc
        if value is not None and not isinstance(value, str):
            raise CredentialStoreError("系统安全凭据库返回了无效内容。")
        return value

    def _delete_password(self, account: str, *, missing_ok: bool = False) -> bool:
        try:
            self.keyring_backend.delete_password(self.service_name, account)
            return True
        except Exception as exc:
            name = type(exc).__name__.lower()
            if missing_ok and (
                "delete" in name or "notfound" in name or "not_found" in name or name == "keyerror"
            ):
                return False
            error = _keyring_failure(exc, action="删除")
            self._startup_error = error
            raise error from exc

    @classmethod
    def _keyring_value(cls, encoded: bytes) -> str:
        return base64.urlsafe_b64encode(encoded).decode("ascii")

    @classmethod
    def _credential_from_value(cls, value: str) -> ProviderCredential:
        try:
            payload = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise CredentialStoreError("系统安全凭据内容无效或已损坏。") from exc
        return cls._decode(payload)

    def _read_pointer(self, slot: str) -> str | None:
        path = self._pointer_path(slot)
        if not path.exists():
            return None
        document = _read_safe_json(path, label="系统凭据引用")
        if (
            _safe_document_version(document, label="系统凭据引用")
            != _SYSTEM_POINTER_VERSION
            or document.get("slot") != validate_identifier(slot, label="credential_slot")
        ):
            raise CredentialStoreError("系统凭据引用绑定无效。")
        try:
            return str(UUID(str(document.get("item_version") or "")))
        except (ValueError, AttributeError) as exc:
            raise CredentialStoreError("系统凭据引用版本无效。") from exc

    def _write_pointer(self, slot: str, version: str) -> None:
        _atomic_private_json(
            self._pointer_path(slot),
            {
                "version": _SYSTEM_POINTER_VERSION,
                "slot": validate_identifier(slot, label="credential_slot"),
                "item_version": str(UUID(version)),
            },
        )

    def stage(
        self,
        slot: str,
        secret: str,
        *,
        provider_id: str,
        origin: str,
        auth: dict[str, str],
    ) -> StagedProviderCredential:
        selected_slot = validate_identifier(slot, label="credential_slot")
        encoded = self._encode(
            slot=selected_slot,
            provider_id=provider_id,
            origin=origin,
            auth=auth,
            secret=secret,
        )
        workspace_id = self._workspace_id(create=True)
        assert workspace_id is not None
        version = str(uuid4())
        previous = self._read_pointer(selected_slot)
        marker = self._stage_path(selected_slot, version)
        _atomic_private_json(
            marker,
            {
                "version": _SYSTEM_POINTER_VERSION,
                "workspace_id": workspace_id,
                "slot": selected_slot,
                "item_version": version,
                "previous_version": previous,
                "created_at": int(time.time()),
            },
        )
        account = self._account(workspace_id, selected_slot, version)
        try:
            self._set_password(account, self._keyring_value(encoded))
            stored = self._get_password(account)
            if stored is None:
                raise CredentialStoreError("系统安全凭据写入前回读失败。")
            loaded = self._credential_from_value(stored)
            expected = self._decode(encoded)
            if (
                loaded.slot != expected.slot
                or loaded.provider_id != expected.provider_id
                or loaded.origin != expected.origin
                or loaded.auth != expected.auth
                or not hmac.compare_digest(loaded.secret, expected.secret)
            ):
                raise CredentialStoreError("系统安全凭据写入前回读不匹配。")
            self._startup_error = None
            return StagedProviderCredential(
                slot=selected_slot,
                credential=loaded,
                _vault_id=id(self),
                _version=version,
                _previous_version=previous,
                _marker=marker,
            )
        except Exception:
            try:
                self._delete_password(account, missing_ok=True)
                marker.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def _validate_staged(self, staged: StagedProviderCredential) -> ProviderCredential:
        if (
            not isinstance(staged, StagedProviderCredential)
            or staged._vault_id != id(self)
            or staged._version is None
            or staged._marker is None
        ):
            raise CredentialStoreError("Provider 凭据 staging 不属于当前 vault。")
        document = _read_safe_json(staged._marker, label="系统凭据 staging 引用")
        workspace_id = self._workspace_id(create=False)
        if (
            workspace_id is None
            or document.get("workspace_id") != workspace_id
            or document.get("slot") != staged.slot
            or document.get("item_version") != staged._version
        ):
            raise CredentialStoreError("系统凭据 staging 绑定无效。")
        value = self._get_password(self._account(workspace_id, staged.slot, staged._version))
        if value is None:
            raise CredentialStoreError("系统凭据 staging 已不存在。")
        loaded = self._credential_from_value(value)
        expected = staged.credential
        if (
            loaded.slot != expected.slot
            or loaded.provider_id != expected.provider_id
            or loaded.origin != expected.origin
            or loaded.auth != expected.auth
            or not hmac.compare_digest(loaded.secret, expected.secret)
        ):
            raise CredentialStoreError("系统凭据 staging 回读不匹配。")
        return loaded

    def commit(self, staged: StagedProviderCredential) -> ProviderCredential:
        loaded = self._validate_staged(staged)
        assert staged._version is not None and staged._marker is not None
        workspace_id = self._workspace_id(create=False)
        assert workspace_id is not None
        self._write_pointer(staged.slot, staged._version)
        try:
            staged._marker.unlink(missing_ok=True)
        except OSError:
            # The pointer switch is the commit point.  Recovery can retry this
            # non-secret marker cleanup; surfacing an error here would make the
            # caller roll config back after the credential already went live.
            pass
        if staged._previous_version and staged._previous_version != staged._version:
            old_account = self._account(workspace_id, staged.slot, staged._previous_version)
            try:
                self._delete_password(old_account, missing_ok=True)
            except CredentialStoreError:
                try:
                    _atomic_private_json(
                        self._gc_path(staged.slot, staged._previous_version),
                        {
                            "version": _SYSTEM_POINTER_VERSION,
                            "workspace_id": workspace_id,
                            "slot": staged.slot,
                            "item_version": staged._previous_version,
                        },
                    )
                except CredentialStoreError:
                    # The new pointer is already committed and valid.  A failed
                    # best-effort cleanup must not make the service roll config
                    # back to a binding that no longer matches the live item.
                    pass
        self._startup_error = None
        return loaded

    def discard(self, staged: StagedProviderCredential) -> bool:
        self._validate_staged(staged)
        assert staged._version is not None and staged._marker is not None
        workspace_id = self._workspace_id(create=False)
        assert workspace_id is not None
        self._delete_password(
            self._account(workspace_id, staged.slot, staged._version), missing_ok=True
        )
        staged._marker.unlink(missing_ok=True)
        return True

    def save(
        self,
        slot: str,
        secret: str,
        *,
        provider_id: str,
        origin: str,
        auth: dict[str, str],
    ) -> ProviderCredential:
        staged = self.stage(slot, secret, provider_id=provider_id, origin=origin, auth=auth)
        try:
            return self.commit(staged)
        except Exception:
            try:
                if staged._marker is not None and staged._marker.exists():
                    self.discard(staged)
            except Exception:
                pass
            raise

    def load(self, slot: str) -> ProviderCredential | None:
        selected_slot = validate_identifier(slot, label="credential_slot")
        version = self._read_pointer(selected_slot)
        if version is None:
            return None
        workspace_id = self._workspace_id(create=False)
        if workspace_id is None:
            raise CredentialStoreError("系统凭据 workspace 标识缺失。")
        value = self._get_password(self._account(workspace_id, selected_slot, version))
        if value is None:
            raise CredentialStoreError("系统凭据引用的安全存储条目不存在。")
        credential = self._credential_from_value(value)
        if credential.slot != selected_slot:
            raise CredentialStoreError("Provider 凭据槽位绑定不匹配。")
        self._startup_error = None
        return credential

    def load_bound(
        self,
        slot: str,
        *,
        provider_id: str,
        origin: str,
        auth: dict[str, str],
    ) -> ProviderCredential | None:
        credential = self.load(slot)
        if credential is None:
            return None
        expected = (
            validate_identifier(provider_id, label="provider_id"),
            endpoint_origin(origin),
            validate_auth(auth),
        )
        if (credential.provider_id, credential.origin, credential.auth) != expected:
            raise CredentialStoreError(
                "Provider 凭据与当前 provider/origin/auth 配置不匹配；拒绝发送。"
            )
        return credential

    def status(self, slot: str) -> dict[str, object]:
        credential = self.load(slot)
        if credential is None:
            return {"slot": validate_identifier(slot, label="credential_slot"), "persisted": False}
        return {
            "slot": credential.slot,
            "persisted": True,
            "provider_id": credential.provider_id,
            "origin": credential.origin,
            "auth": dict(credential.auth),
        }

    def clear(self, slot: str) -> bool:
        selected_slot = validate_identifier(slot, label="credential_slot")
        version = self._read_pointer(selected_slot)
        if version is None:
            return False
        workspace_id = self._workspace_id(create=False)
        if workspace_id is None:
            raise CredentialStoreError("系统凭据 workspace 标识缺失。")
        pointer = self._pointer_path(selected_slot)
        marker = self._clear_path(selected_slot, version)
        try:
            os.replace(pointer, marker)
        except OSError as exc:
            raise CredentialStoreError("无法暂存系统凭据删除引用。") from exc
        try:
            self._delete_password(
                self._account(workspace_id, selected_slot, version), missing_ok=True
            )
        except Exception:
            try:
                os.replace(marker, pointer)
            except OSError as restore_exc:
                raise CredentialStoreError(
                    "系统凭据删除失败，且无法恢复原引用；拒绝继续。"
                ) from restore_exc
            raise
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            # The pointer is already absent and the keyring item is gone.
            # Startup recovery can finish deleting this non-secret marker.
            pass
        return True

    def migrate_legacy_deepseek(self) -> bool:
        return False

    def recover(self) -> None:
        if not self.directory.exists() or not self.workspace_id_path.exists():
            return
        workspace_id = self._workspace_id(create=False)
        assert workspace_id is not None
        for marker in self.directory.glob(".*.stage.json"):
            document = _read_safe_json(marker, label="系统凭据 staging 引用")
            if document.get("workspace_id") != workspace_id:
                continue
            slot = validate_identifier(document.get("slot"), label="credential_slot")
            version = str(UUID(str(document.get("item_version") or "")))
            if self._read_pointer(slot) != version:
                self._delete_password(self._account(workspace_id, slot, version), missing_ok=True)
            marker.unlink(missing_ok=True)
        for marker in self.directory.glob(".*.gc.json"):
            document = _read_safe_json(marker, label="系统凭据 GC 引用")
            if document.get("workspace_id") != workspace_id:
                continue
            slot = validate_identifier(document.get("slot"), label="credential_slot")
            version = str(UUID(str(document.get("item_version") or "")))
            if self._read_pointer(slot) != version:
                self._delete_password(self._account(workspace_id, slot, version), missing_ok=True)
                marker.unlink(missing_ok=True)
        for marker in self.directory.glob(".*.clear.json"):
            document = _read_safe_json(marker, label="系统凭据删除引用")
            slot = validate_identifier(document.get("slot"), label="credential_slot")
            version = str(UUID(str(document.get("item_version") or "")))
            if self._read_pointer(slot) is None:
                self._delete_password(
                    self._account(workspace_id, slot, version), missing_ok=True
                )
                marker.unlink(missing_ok=True)


class UnavailableCredentialVault:
    backend_name = "unavailable"

    def __init__(self, message: str, *, code: str = "credential_store_unavailable") -> None:
        self.error = CredentialStoreError(message, code=code)

    def secure_store_status(self) -> dict[str, object]:
        return {
            "backend": self.backend_name,
            "available": False,
            "error_code": self.error.code,
            "message": str(self.error),
        }

    def _raise(self) -> None:
        raise CredentialStoreError(str(self.error), code=self.error.code)

    def stage(self, *args: Any, **kwargs: Any) -> StagedProviderCredential:
        self._raise()

    def commit(self, staged: StagedProviderCredential) -> ProviderCredential:
        self._raise()

    def discard(self, staged: StagedProviderCredential) -> bool:
        self._raise()

    def save(self, *args: Any, **kwargs: Any) -> ProviderCredential:
        self._raise()

    def load(self, slot: str) -> ProviderCredential | None:
        self._raise()

    def load_bound(self, *args: Any, **kwargs: Any) -> ProviderCredential | None:
        self._raise()

    def clear(self, slot: str) -> bool:
        self._raise()

    def migrate_legacy_deepseek(self) -> bool:
        return False

    def status(self, slot: str) -> dict[str, object]:
        self._raise()


def create_platform_credential_vault(
    state_dir: str | Path,
    *,
    platform_name: str | None = None,
    keyring_backend: Any | None = None,
) -> CredentialVault:
    """Create only the explicitly approved secure backend for this platform."""

    root = Path(state_dir)
    selected = platform_name or ("windows" if os.name == "nt" else sys.platform)
    if selected in {"windows", "win32", "nt"}:
        return ProviderCredentialVault(
            root / "credentials", legacy_deepseek_path=root / "deepseek-key.dpapi"
        )
    if selected == "darwin":
        backend_name = "keychain"
        try:
            if keyring_backend is None:
                from keyring.backends.macOS import Keyring

                keyring_backend = Keyring()
            _ = keyring_backend.priority
            return SystemKeyringCredentialVault(
                root / "credentials", keyring_backend=keyring_backend, backend_name=backend_name
            )
        except Exception as exc:
            error = _keyring_failure(exc, action="初始化")
            return UnavailableCredentialVault(str(error), code=error.code)
    if selected.startswith("linux"):
        backend_name = "secret_service"
        try:
            if keyring_backend is None:
                from keyring.backends.SecretService import Keyring

                keyring_backend = Keyring()
            _ = keyring_backend.priority
            return SystemKeyringCredentialVault(
                root / "credentials", keyring_backend=keyring_backend, backend_name=backend_name
            )
        except Exception as exc:
            error = _keyring_failure(exc, action="初始化")
            message = (
                "Linux Secret Service 不可用；请确认当前桌面会话的 D-Bus 与密钥环已运行并解锁。"
            )
            return UnavailableCredentialVault(message, code=error.code)
    return UnavailableCredentialVault("当前 Unix 平台不支持自动安全凭据存储。")


__all__ = [
    "CredentialVault",
    "CredentialStoreError",
    "DeepSeekCredentialStore",
    "ProviderCredential",
    "ProviderCredentialVault",
    "StagedProviderCredential",
    "SystemKeyringCredentialVault",
    "UnavailableCredentialVault",
    "create_platform_credential_vault",
    "protect_with_dpapi",
    "unprotect_with_dpapi",
]
