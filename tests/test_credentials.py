from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.acm_agent.credentials import (
    CredentialStoreError,
    DeepSeekCredentialStore,
    SystemKeyringCredentialVault,
    UnavailableCredentialVault,
    create_platform_credential_vault,
)


def fake_protect(value: bytes) -> bytes:
    return b"fixture-cipher:" + value[::-1]


def fake_unprotect(value: bytes) -> bytes:
    if not value.startswith(b"fixture-cipher:"):
        raise CredentialStoreError("invalid fixture cipher")
    return value[len(b"fixture-cipher:") :][::-1]


class DeepSeekCredentialStoreTests(unittest.TestCase):
    def test_round_trip_is_atomic_and_plaintext_never_reaches_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deepseek-key.dpapi"
            store = DeepSeekCredentialStore(
                path, protect=fake_protect, unprotect=fake_unprotect
            )
            secret = "sk-fixture-never-persist-plaintext"
            store.save(secret)
            self.assertTrue(store.exists)
            self.assertNotIn(secret.encode("utf-8"), path.read_bytes())
            self.assertEqual(store.load(), secret)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

            self.assertTrue(store.clear())
            self.assertFalse(path.exists())
            self.assertIsNone(store.load())

    def test_corruption_is_rejected_without_returning_partial_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deepseek-key.dpapi"
            store = DeepSeekCredentialStore(
                path, protect=fake_protect, unprotect=fake_unprotect
            )
            store.save("sk-fixture")
            payload = bytearray(path.read_bytes())
            payload[-1] ^= 0x01
            path.write_bytes(payload)
            with self.assertRaises(CredentialStoreError):
                store.load()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI integration test")
    def test_windows_dpapi_round_trip_survives_store_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deepseek-key.dpapi"
            secret = "sk-dpapi-integration-fixture"
            DeepSeekCredentialStore(path).save(secret)
            self.assertNotIn(secret.encode("utf-8"), path.read_bytes())
            self.assertEqual(DeepSeekCredentialStore(path).load(), secret)


class FakeKeyring:
    priority = 5

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_set: Exception | None = None
        self.fail_get: Exception | None = None
        self.fail_delete: Exception | None = None

    def set_password(self, service: str, account: str, value: str) -> None:
        if self.fail_set is not None:
            raise self.fail_set
        self.values[(service, account)] = value

    def get_password(self, service: str, account: str) -> str | None:
        if self.fail_get is not None:
            raise self.fail_get
        return self.values.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        if self.fail_delete is not None:
            raise self.fail_delete
        key = (service, account)
        if key not in self.values:
            raise KeyError("not found")
        del self.values[key]


class KeyringLocked(Exception):
    pass


class SystemKeyringCredentialVaultTests(unittest.TestCase):
    def _vault(self, root: Path, backend: FakeKeyring) -> SystemKeyringCredentialVault:
        return SystemKeyringCredentialVault(
            root / ".acm" / "credentials",
            keyring_backend=backend,
            backend_name="secret_service",
        )

    def test_versioned_round_trip_recreation_and_no_plaintext_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = FakeKeyring()
            vault = self._vault(root, backend)
            secret = "system-keyring-secret-never-on-disk"
            saved = vault.save(
                "relay",
                secret,
                provider_id="relay",
                origin="https://relay.example",
                auth={"type": "bearer"},
            )
            self.assertEqual(saved.secret, secret)
            self.assertNotIn(secret, repr(saved))
            disk = b"".join(
                path.read_bytes()
                for path in (root / ".acm").rglob("*")
                if path.is_file()
            )
            self.assertNotIn(secret.encode(), disk)
            recreated = self._vault(root, backend)
            self.assertEqual(recreated.load("relay").secret, secret)
            with self.assertRaisesRegex(CredentialStoreError, "不匹配"):
                recreated.load_bound(
                    "relay",
                    provider_id="relay",
                    origin="https://other.example",
                    auth={"type": "bearer"},
                )
            self.assertTrue(recreated.clear("relay"))
            self.assertIsNone(recreated.load("relay"))

    def test_corrupt_pointer_version_is_typed_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = FakeKeyring()
            vault = self._vault(root, backend)
            vault.save(
                "relay", "safe-secret", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            pointer = root / ".acm" / "credentials" / "relay.keyring-ref"
            document = json.loads(pointer.read_text(encoding="utf-8"))
            document["version"] = "corrupt"
            pointer.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(CredentialStoreError) as caught:
                vault.load("relay")
            self.assertEqual(caught.exception.code, "credential_store_error")
            self.assertNotIn("safe-secret", str(caught.exception))

    def test_stage_discard_commit_and_startup_recovery_preserve_live_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = FakeKeyring()
            vault = self._vault(root, backend)
            vault.save(
                "relay", "old-secret", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            staged = vault.stage(
                "relay", "discarded-secret", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            vault.discard(staged)
            self.assertEqual(vault.load("relay").secret, "old-secret")

            vault.stage(
                "relay", "crash-candidate", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            recovered = self._vault(root, backend)
            self.assertEqual(recovered.load("relay").secret, "old-secret")
            self.assertEqual(list((root / ".acm" / "credentials").glob("*.stage.json")), [])

            committed = recovered.stage(
                "relay", "new-secret", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            recovered.commit(committed)
            self.assertEqual(recovered.load("relay").secret, "new-secret")
            self.assertEqual(len(backend.values), 1)

    def test_locked_and_unavailable_backends_are_typed_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = FakeKeyring()
            backend.fail_set = KeyringLocked("fixture secret must not appear")
            vault = self._vault(root, backend)
            with self.assertRaises(CredentialStoreError) as caught:
                vault.save(
                    "relay", "never-persist-this", provider_id="relay",
                    origin="https://relay.example", auth={"type": "bearer"},
                )
            self.assertEqual(caught.exception.code, "credential_store_locked")
            self.assertNotIn("never-persist-this", str(caught.exception))
            disk = b"".join(
                path.read_bytes()
                for path in (root / ".acm").rglob("*")
                if path.is_file()
            )
            self.assertNotIn(b"never-persist-this", disk)

            unavailable = UnavailableCredentialVault("fixture unavailable")
            with self.assertRaises(CredentialStoreError) as unavailable_error:
                unavailable.load("deepseek")
            self.assertEqual(
                unavailable_error.exception.code, "credential_store_unavailable"
            )

    def test_pointer_commit_failure_keeps_old_live_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = FakeKeyring()
            vault = self._vault(root, backend)
            vault.save(
                "relay", "old-secret", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            with patch.object(
                vault,
                "_write_pointer",
                side_effect=CredentialStoreError("fixture pointer failure"),
            ):
                with self.assertRaisesRegex(CredentialStoreError, "pointer failure"):
                    vault.save(
                        "relay", "candidate-secret", provider_id="relay",
                        origin="https://relay.example", auth={"type": "bearer"},
                    )
            self.assertEqual(vault.load("relay").secret, "old-secret")
            self.assertEqual(len(backend.values), 1)

    def test_clear_failure_restores_pointer_and_interrupted_clear_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = FakeKeyring()
            vault = self._vault(root, backend)
            vault.save(
                "relay", "old-secret", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            backend.fail_delete = KeyringLocked("fixture locked")
            with self.assertRaises(CredentialStoreError) as caught:
                vault.clear("relay")
            self.assertEqual(caught.exception.code, "credential_store_locked")
            backend.fail_delete = None
            self.assertEqual(vault.load("relay").secret, "old-secret")

            version = vault._read_pointer("relay")
            assert version is not None
            os.replace(
                vault._pointer_path("relay"), vault._clear_path("relay", version)
            )
            recovered = self._vault(root, backend)
            self.assertIsNone(recovered.load("relay"))
            self.assertEqual(backend.values, {})

    def test_platform_factory_uses_only_explicit_approved_backends(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".acm"
            fake = FakeKeyring()
            linux = create_platform_credential_vault(
                root, platform_name="linux", keyring_backend=fake
            )
            mac = create_platform_credential_vault(
                root, platform_name="darwin", keyring_backend=fake
            )
            unsupported = create_platform_credential_vault(root, platform_name="freebsd")
            self.assertEqual(linux.backend_name, "secret_service")
            self.assertEqual(mac.backend_name, "keychain")
            self.assertEqual(unsupported.backend_name, "unavailable")
            self.assertFalse(unsupported.secure_store_status()["available"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux Secret Service check")
    def test_linux_without_dbus_is_safely_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {"DBUS_SESSION_BUS_ADDRESS": "", "XDG_RUNTIME_DIR": ""},
        ):
            vault = create_platform_credential_vault(Path(temporary) / ".acm")
            status = vault.secure_store_status()
            self.assertEqual(status["backend"], "unavailable")
            self.assertFalse(status["available"])
            self.assertEqual(status["error_code"], "credential_store_unavailable")
            with self.assertRaises(CredentialStoreError):
                vault.save(
                    "relay", "must-not-persist", provider_id="relay",
                    origin="https://relay.example", auth={"type": "bearer"},
                )
            disk = b"".join(
                path.read_bytes()
                for path in (Path(temporary) / ".acm").rglob("*")
                if path.is_file()
            )
            self.assertNotIn(b"must-not-persist", disk)

    @unittest.skipUnless(
        sys.platform == "darwin" and os.environ.get("ACM_TEST_SYSTEM_KEYRING") == "1",
        "opt-in macOS Keychain integration test",
    )
    def test_macos_keychain_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = create_platform_credential_vault(Path(temporary) / ".acm")
            secret = "macos-keychain-integration-fixture"
            saved = False
            try:
                vault.save(
                    "integration", secret, provider_id="integration",
                    origin="https://integration.example", auth={"type": "bearer"},
                )
                saved = True
                self.assertEqual(vault.load("integration").secret, secret)
            finally:
                if saved:
                    self.assertTrue(vault.clear("integration"))
                    self.assertIsNone(vault.load("integration"))


if __name__ == "__main__":
    unittest.main()
