from copy import deepcopy
import hashlib
import json
import unittest

from tools.acm_agent.provider_config import (
    capability_profile, default_ai_policy, default_credential_slots,
    default_provider_config, default_task_profiles, validate_capabilities,
)
from tools.acm_agent.provider_registry import ProviderRegistry, provider_definition_hash


def provider(base="https://provider.example/v1", model="new-model", limit=None):
    return {
        "name": "Test", "adapter": "openai_compatible", "base_url": base,
        "enabled": True, "credential_slot": "test", "auth": {"type": "bearer"},
        "models": {model: {"evidence": "declared", "capabilities": {
            "text_chat": True, "json_object": True, "streaming": True,
            "usage": True, "stream_usage": True, "max_output_tokens": limit,
        }}},
    }


def registry(definition, budget=200_000):
    ai = {
        "providers": default_provider_config(), "profiles": default_task_profiles(),
        "credential_slots": default_credential_slots(), "policy": default_ai_policy(),
    }
    ai["providers"]["test"] = definition
    from urllib.parse import urlsplit
    url = urlsplit(definition["base_url"])
    ai["credential_slots"]["test"] = {
        "provider_id": "test", "origin": f"{url.scheme}://{url.netloc}",
        "auth": {"type": "bearer"}, "environment_variable": "",
    }
    ai["policy"]["budgets"]["coaching"]["max_output_tokens"] = budget
    return ProviderRegistry(ai), ai


class ProviderOutputLimitTests(unittest.TestCase):
    def test_declared_limits_apply_without_mutating_definition(self):
        for base in ("https://provider.example/v1", "https://relay.example/custom"):
            for declared in (4096, 131_072, 200_000):
                with self.subTest(base=base, declared=declared):
                    definition = provider(base=base, limit=declared)
                    original = deepcopy(definition)
                    self.assertEqual(capability_profile(definition, "new-model").max_output_tokens, declared)
                    self.assertEqual(definition, original)

    def test_missing_limits_are_not_inferred_from_endpoint_or_model_name(self):
        for base, model in (
            ("https://provider.example/v1", "new-model"),
            ("https://open.bigmodel.cn/api/coding/paas/v4", "glm-5.3"),
            ("https://open.bigmodel.cn/api/anthropic", "glm-5.3"),
            ("https://open.bigmodel.cn/api/coding/paas/v4", "glm-other"),
        ):
            with self.subTest(base=base, model=model):
                self.assertIsNone(capability_profile(provider(base, model), model).max_output_tokens)

    def test_same_model_name_has_provider_specific_limits(self):
        first = provider(limit=4096)
        second = provider("https://other.example/v1", limit=8192)
        reg, ai = registry(first)
        ai["providers"]["other"] = second
        second["credential_slot"] = "other"
        ai["credential_slots"]["other"] = {
            "provider_id": "other", "origin": "https://other.example",
            "auth": {"type": "bearer"}, "environment_variable": "",
        }
        reg = ProviderRegistry(ai)
        self.assertEqual(reg.probe_route("test", "new-model", profile_id="coaching").budget["max_output_tokens"], 4096)
        self.assertEqual(reg.probe_route("other", "new-model", profile_id="coaching").budget["max_output_tokens"], 8192)

    def test_production_and_probe_use_same_clamped_budget_without_changing_saved_policy(self):
        for definition, task_limit, expected in (
            (provider(limit=131_072), 200_000, 131_072),
            (provider(limit=131_072), 4096, 4096),
            (provider("https://relay.example/v1", limit=8192), 200_000, 8192),
            (provider("https://relay.example/v1"), 200_000, 200_000),
        ):
            with self.subTest(base=definition["base_url"], task_limit=task_limit, expected=expected):
                reg, ai = registry(definition, task_limit)
                original = deepcopy(ai)
                route = reg.route("coaching", model_ref={"provider_id": "test", "model": "new-model"}, reasoning_strength="auto", require_verified=False)
                probe = reg.probe_route("test", "new-model", profile_id="coaching")
                self.assertEqual(route.budget["max_output_tokens"], expected)
                self.assertEqual(probe.budget, route.budget)
                self.assertEqual(reg.policy["budgets"]["coaching"]["max_output_tokens"], task_limit)
                self.assertEqual(ai, original)

    def test_hash_uses_effective_limits_and_keeps_unrelated_hashes_stable(self):
        def legacy_hash(definition):
            document = {
                "provider_id": "test", "adapter": definition["adapter"],
                "base_url": definition["base_url"], "auth": definition["auth"],
                "model": "new-model",
                "capabilities": validate_capabilities(definition["models"]["new-model"]["capabilities"]),
                "reasoning_wire": "openai_reasoning_effort", "conformance_version": 3,
            }
            return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        definition = provider()
        self.assertNotEqual(provider_definition_hash("test", definition, "new-model"), legacy_hash(definition))
        self.assertNotEqual(provider_definition_hash("test", definition, "new-model"), provider_definition_hash("test", provider(limit=131_072), "new-model"))
        self.assertNotEqual(provider_definition_hash("test", provider(limit=131_072), "new-model"), provider_definition_hash("test", provider(limit=4096), "new-model"))
        relay = provider("https://relay.example/v1")
        self.assertNotEqual(provider_definition_hash("test", relay, "new-model"), legacy_hash(relay))


if __name__ == "__main__":
    unittest.main()
