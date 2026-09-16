from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from tests.test_stage2_provider import Response, QueueTransport, completion
from tools.acm_agent.config import load_config
from tools.acm_agent.credentials import ProviderCredentialVault
from tools.acm_agent.deepseek import DeepSeekClient
from tools.acm_agent.provider import ProviderConfigurationError
from tools.acm_agent.service import AcmService


class DynamicDeepSeekModelsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = ProviderCredentialVault(
            self.root / ".acm" / "credentials",
            protect=lambda value: b"P" + value,
            unprotect=lambda value: value[1:],
        )
        self.service = AcmService(self.root, credential_vault=self.vault)
        self.service.setup("fixture", "42", skip_validate=True)
        with patch(
            "tools.acm_agent.service_ai.discover_openai_compatible_models",
            return_value=["legacy-" + uuid4().hex],
        ):
            self.service.ai_connection_upsert(
                connection_id="deepseek", display_name="DeepSeek Official",
                base_url="https://api.deepseek.com", api_key="fixture-secret",
            )

    @staticmethod
    def probe_transport():
        stream = b"\n".join([
            b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}',
            b'',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_cache_hit_tokens":2,"prompt_cache_miss_tokens":1}}',
            b'', b'data: [DONE]', b'',
        ])
        return QueueTransport(
            Response(completion()), Response(completion('{"ok":true}')),
            Response(stream), Response(completion("OK")),
        )

    def test_future_ids_refresh_verify_route_and_reach_wire_unchanged(self):
        future_ids = ["future-family-" + uuid4().hex for _ in range(2)]
        profiles = load_config(self.service.paths)["ai"]["profiles"]
        with patch(
            "tools.acm_agent.service_ai.discover_openai_compatible_models",
            return_value=future_ids,
        ) as discover:
            refreshed = self.service.ai_connection_refresh(connection_id="deepseek")
        discover.assert_called_once_with(
            base_url="https://api.deepseek.com", api_key="fixture-secret",
        )
        self.assertEqual(refreshed["models_discovered"], 2)
        self.assertEqual(load_config(self.service.paths)["ai"]["profiles"], profiles)
        self.assertEqual(set(self.service.ai_status()["allowed_models"]), set(future_ids))

        for model in future_ids:
            with self.subTest(model=model):
                config = load_config(self.service.paths)
                definition = config["ai"]["providers"]["deepseek"]["models"][model]
                self.assertTrue(definition["available"])
                self.assertEqual(definition["evidence"], "declared")
                self.assertFalse(definition["capabilities"]["json_schema"])
                self.assertIsNone(definition["capabilities"]["max_context_tokens"])
                model_ref = {"provider_id": "deepseek", "model": model}
                with self.assertRaises(ProviderConfigurationError) as captured:
                    self.service._provider_registry().route(
                        "recommendation", model_ref=model_ref, reasoning_strength="auto",
                    )
                self.assertEqual(captured.exception.code, "unverified_capability")

                transport = self.probe_transport()
                with patch.object(DeepSeekClient, "_default_transport", side_effect=transport):
                    verified = self.service.ai_provider_test(provider_id="deepseek", model=model)
                    self.assertTrue(verified["ok"], verified)
                    registry = self.service._provider_registry()
                    for profile_id in profiles:
                        route = registry.route(profile_id, model_ref=model_ref, reasoning_strength="auto")
                        self.assertEqual(route.model, model)
                        self.assertEqual(route.capabilities.evidence, "verified_live")
                    result = registry.client_for_route(route).chat(
                        [{"role": "user", "content": "Reply OK"}], model=model,
                        thinking=False, reasoning_effort="auto", max_tokens=8,
                    )
                self.assertEqual(result.content, "OK")
                self.assertEqual(len(transport.requests), 4)
                self.assertTrue(all(json.loads(request.data)["model"] == model for request in transport.requests))
                self.assertTrue(all(request.full_url == "https://api.deepseek.com/chat/completions" for request in transport.requests))
                persisted = load_config(self.service.paths)["ai"]["providers"]["deepseek"]["models"][model]
                self.assertEqual(persisted["evidence"], "verified_live")
                self.assertTrue(persisted["evidence_hash"])

        before = load_config(self.service.paths)["ai"]["providers"]["deepseek"]["models"]
        with patch(
            "tools.acm_agent.service_ai.discover_openai_compatible_models", return_value=future_ids,
        ):
            self.service.ai_connection_refresh(connection_id="deepseek")
        after = load_config(self.service.paths)["ai"]["providers"]["deepseek"]["models"]
        for model in future_ids:
            self.assertEqual(after[model], before[model])

    def test_failed_refresh_and_upsert_preserve_config_and_secret(self):
        before = self.service.paths.config.read_bytes()
        credential_files = {
            path.name: path.read_bytes()
            for path in (self.root / ".acm" / "credentials").iterdir() if path.is_file()
        }
        with patch(
            "tools.acm_agent.service_ai.discover_openai_compatible_models",
            side_effect=ProviderConfigurationError("model_discovery_failed", "fixture failure"),
        ):
            with self.assertRaises(ProviderConfigurationError):
                self.service.ai_connection_refresh(connection_id="deepseek")
            with self.assertRaises(ProviderConfigurationError):
                self.service.ai_connection_upsert(
                    connection_id="deepseek", display_name="Renamed",
                    base_url="https://api.deepseek.com", api_key="replacement-secret",
                )
        self.assertEqual(self.service.paths.config.read_bytes(), before)
        self.assertEqual(self.vault.load("deepseek").secret, "fixture-secret")
        self.assertEqual({
            path.name: path.read_bytes()
            for path in (self.root / ".acm" / "credentials").iterdir() if path.is_file()
        }, credential_files)


if __name__ == "__main__":
    unittest.main()
