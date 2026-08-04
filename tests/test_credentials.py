from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.credentials import (
    CredentialStoreError,
    DeepSeekCredentialStore,
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


if __name__ == "__main__":
    unittest.main()
