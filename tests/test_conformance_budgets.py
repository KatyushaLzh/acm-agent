"""Regression coverage for reasoning-aware model verification budgets."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tools.acm_agent.provider import (
    AIJsonResult, AIResult, AIStreamEvent, ProviderConfigurationError, ProviderError,
)
from tools.acm_agent.provider_config import (
    TASK_PROFILE_IDS, default_ai_policy, default_credential_slots,
    default_provider_config, default_task_profiles,
)
from tools.acm_agent.provider_conformance import run_live_conformance
from tools.acm_agent.provider_registry import ProviderRegistry
from tools.acm_agent.service_ai import ServiceAIMixin


class RecordingClient:
    def __init__(self, *, finish_reason="stop", content="The protocol check succeeded."):
        self.calls = []
        self.finish_reason = finish_reason
        self.content = content
        self.usage = {"total_tokens": 25, "cache_read_tokens": 0}

    def chat(self, messages, **kwargs):
        self.calls.append(("text", kwargs))
        return AIResult(self.content, self.finish_reason, self.usage, kwargs["model"])

    def chat_json(self, messages, **kwargs):
        self.calls.append(("json_object", kwargs))
        return AIJsonResult('{"ok":true}', self.finish_reason, self.usage,
                            kwargs["model"], {"ok": True})

    def stream_chat(self, messages, **kwargs):
        self.calls.append(("stream", kwargs))
        yield AIStreamEvent("delta", content=self.content)
        yield AIStreamEvent("done", finish_reason=self.finish_reason, usage=self.usage)


class ConformanceBudgetTests(unittest.TestCase):
    def registry(self, policy=None):
        return ProviderRegistry({
            "policy": policy or default_ai_policy(),
            "providers": default_provider_config(),
            "profiles": default_task_profiles(),
            "credential_slots": default_credential_slots(),
        })

    def assert_call_budget(self, client, budget):
        for name, kwargs in client.calls:
            with self.subTest(case=name):
                self.assertEqual(kwargs["max_tokens"], budget["max_output_tokens"])
                if name != "stream":
                    self.assertGreater(kwargs["request_timeout"], 0)
                    self.assertLessEqual(kwargs["request_timeout"], budget["request_timeout_seconds"])
                if name == "json_object":
                    self.assertEqual(kwargs["json_retries"], 0)

    def test_all_task_profiles_and_reasoning_strengths_use_normal_task_budgets(self):
        registry = self.registry()
        for profile_id in TASK_PROFILE_IDS:
            for strength in ("auto", "off", "medium", "high"):
                with self.subTest(profile=profile_id, strength=strength):
                    route = registry.probe_route("deepseek", profile_id=profile_id,
                                                 reasoning_strength=strength)
                    normal = registry.route(profile_id, reasoning_strength=strength,
                                            require_verified=False)
                    self.assertEqual(route.budget, normal.budget)
                    self.assertEqual(route.profile_id, profile_id)
                    client = RecordingClient()
                    report = run_live_conformance(client, route)
                    self.assertTrue(report["passed"], report)
                    self.assertEqual([name for name, _ in client.calls],
                                     ["text", "json_object", "stream"])
                    self.assert_call_budget(client, normal.budget)
                    for _, kwargs in client.calls:
                        self.assertEqual(kwargs["thinking"], normal.thinking)
                        self.assertEqual(kwargs["reasoning_effort"], normal.reasoning_effort)

    def test_custom_budget_propagates_without_mutating_policy(self):
        policy = default_ai_policy()
        policy["budgets"]["patch"].update(max_output_tokens=12345,
                                            request_timeout_seconds=173.0)
        registry = self.registry(policy)
        route = registry.probe_route("deepseek", profile_id="patch")
        client = RecordingClient()
        self.assertTrue(run_live_conformance(client, route)["passed"])
        self.assert_call_budget(client, policy["budgets"]["patch"])
        route.budget["max_output_tokens"] = 1
        self.assertEqual(registry.policy["budgets"]["patch"]["max_output_tokens"], 12345)

    def test_output_parameter_error_has_safe_hint_without_reflected_content(self):
        route = self.registry().probe_route("deepseek")
        error = ProviderError("invalid_request", "max_tokens invalid reflected-secret", status=400)
        client = SimpleNamespace(
            chat=Mock(side_effect=error), chat_json=Mock(side_effect=error),
            stream_chat=Mock(side_effect=error),
        )
        report = run_live_conformance(client, route)
        self.assertFalse(report["passed"])
        for case in report["cases"]:
            if case["name"] in {"text", "json_object", "stream"}:
                self.assertIn("最大输出 Token", case["error_hint"])
        self.assertNotIn("reflected-secret", str(report))

    def test_probe_default_is_recommendation_and_invalid_profile_is_rejected(self):
        registry = self.registry()
        self.assertEqual(registry.probe_route("deepseek").budget,
                         registry.policy["budgets"]["recommendation"])
        with self.assertRaises(ProviderConfigurationError) as raised:
            registry.probe_route("deepseek", profile_id="unknown")
        self.assertEqual(raised.exception.code, "invalid_profile")

    def test_truncation_is_not_success_even_with_nonempty_valid_content(self):
        route = self.registry().probe_route("deepseek", reasoning_strength="medium")
        for content in ("", "The protocol check succeeded."):
            with self.subTest(content=content):
                report = run_live_conformance(
                    RecordingClient(finish_reason="length", content=content), route
                )
                self.assertFalse(report["passed"])
                self.assertEqual(report["verified_capabilities"], [])
                cases = {item["name"]: item for item in report["cases"]}
                for name in ("text", "json_object", "stream"):
                    self.assertFalse(cases[name]["ok"])
                    self.assertEqual(cases[name]["error_code"], "response_incomplete")

    def test_model_verification_service_preserves_selected_profile_budget(self):
        registry = self.registry()
        for profile_id in TASK_PROFILE_IDS:
            with self.subTest(profile=profile_id):
                client = RecordingClient()
                registry.client_for_route = Mock(return_value=client)
                service = SimpleNamespace(
                    _provider_registry=lambda: registry,
                    _finish_model_verification=lambda route, report: {"ok": report["passed"]},
                )
                service._connection_model_source_hash = ServiceAIMixin._connection_model_source_hash
                service._verify_connection_model = lambda *args, **kwargs: ServiceAIMixin._verify_connection_model(service, *args, **kwargs)
                model = registry.profiles[profile_id]["model"]
                result = ServiceAIMixin.ai_model_verify(
                    service, profile_id=profile_id,
                    model_ref={"provider_id": "deepseek", "model": model},
                    reasoning_strength="medium",
                )
                self.assertTrue(result["ok"])
                self.assert_call_budget(client, registry.policy["budgets"][profile_id])
                call = registry.client_for_route.call_args
                self.assertEqual(call.kwargs["timeout"],
                                 registry.policy["budgets"][profile_id]["request_timeout_seconds"])

    def test_provider_verification_service_uses_recommendation_budget(self):
        registry = self.registry()
        client = RecordingClient()
        registry.client_for_route = Mock(return_value=client)
        service = SimpleNamespace(
            _provider_registry=lambda: registry,
            _finish_model_verification=lambda route, report: {"ok": report["passed"]},
        )
        service._connection_model_source_hash = ServiceAIMixin._connection_model_source_hash
        service._verify_connection_model = lambda *args, **kwargs: ServiceAIMixin._verify_connection_model(service, *args, **kwargs)
        self.assertTrue(ServiceAIMixin.ai_provider_test(service, provider_id="deepseek")["ok"])
        self.assert_call_budget(client, registry.policy["budgets"]["recommendation"])
        self.assertEqual(registry.client_for_route.call_args.kwargs["timeout"],
                         registry.policy["budgets"]["recommendation"]["request_timeout_seconds"])


if __name__ == "__main__":
    unittest.main()
