from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.deepseek import ChatResult, DeepSeekError, DeepSeekClient
from tools.acm_agent.provider import AIResult, ProviderError
from tools.acm_agent.service import AcmService


class ProviderContractTests(unittest.TestCase):
    def test_legacy_result_import_is_provider_neutral_type(self) -> None:
        result = ChatResult("ok", "stop", {"total_tokens": 1}, "resolved", "r1", "requested")
        self.assertIsInstance(result, AIResult)
        self.assertEqual(result.requested_model, "requested")
        self.assertEqual(result.resolved_model, "resolved")

    def test_legacy_error_is_caught_by_provider_error(self) -> None:
        error = DeepSeekError(
            "rate_limited", "later", status=429, retryable=True,
            retry_after=2.5, requested_model="requested", model="resolved",
        )
        self.assertIsInstance(error, ProviderError)
        payload = error.as_dict()
        self.assertEqual(payload["retry_after"], 2.5)
        self.assertEqual(payload["requested_model"], "requested")
        self.assertEqual(payload["resolved_model"], "resolved")

    def test_deepseek_adapter_declares_capabilities_without_network(self) -> None:
        profile = DeepSeekClient(api_key="unused").capabilities("deepseek-v4-flash")
        self.assertTrue(profile.streaming)
        self.assertTrue(profile.json_object)
        self.assertTrue(profile.prompt_cache)

    def test_new_and_legacy_factories_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                AcmService(
                    Path(temp), provider_client_factory=lambda: object(),
                    deepseek_client_factory=lambda: object(),
                )

    def test_provider_factory_injection_does_not_construct_default_adapter(self) -> None:
        class FakeProvider:
            key_detected = False

        fake = FakeProvider()
        with tempfile.TemporaryDirectory() as temp:
            service = AcmService(
                Path(temp), provider_client_factory=lambda: fake
            )
            status = service.ai_status()
        self.assertEqual(status["credential_source"], "none")
        self.assertFalse(status["api_key_detected"])


if __name__ == "__main__":
    unittest.main()
