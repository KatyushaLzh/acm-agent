from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.provider import AIJsonResult, ProviderError
from tools.acm_agent.provider_config import (
    default_ai_policy,
    default_credential_slots,
    default_provider_config,
    default_task_profiles,
)
from tools.acm_agent.provider_fallback_workload import (
    FAILURE_CODE,
    FallbackWorkloadError,
    prepare_route_plan,
    run_provider_fallback_workload,
    write_report,
)
from tools.acm_agent.provider_registry import provider_definition_hash


def _config() -> dict:
    providers = default_provider_config()
    relay = {
        "name": "Verified relay",
        "adapter": "openai_compatible",
        "base_url": "https://relay.example/v1",
        "credential_slot": "relay",
        "auth": {"type": "bearer"},
        "enabled": True,
        "models": {
            "relay-model": {
                "capabilities": {
                    "text_chat": True,
                    "streaming": False,
                    "json_object": True,
                    "function_tools": False,
                    "thinking": True,
                    "prompt_cache": False,
                    "usage_cache_tokens": False,
                    "usage": True,
                    "stream_usage": False,
                    "json_schema": False,
                    "max_context_tokens": None,
                    "max_output_tokens": None,
                },
                "evidence": "verified_live",
                "evidence_hash": None,
                "verified_at": "2026-08-26T00:00:00+00:00",
                "verified_capabilities": ["json_object", "text_chat", "thinking", "usage"],
                "verified_reasoning_strengths": ["off"],
                "available": True,
            }
        },
    }
    relay["models"]["relay-model"]["evidence_hash"] = provider_definition_hash(
        "relay", relay, "relay-model"
    )
    providers["relay"] = relay
    slots = default_credential_slots()
    slots["relay"] = {
        "provider_id": "relay",
        "origin": "https://relay.example",
        "auth": {"type": "bearer"},
        "environment_variable": "TEST_RELAY_KEY",
    }
    profiles = default_task_profiles()
    profiles["plan_organize"].update(
        provider_id="deepseek",
        model="deepseek-v4-flash",
        reasoning_strength="off",
    )
    return {
        "providers": providers,
        "profiles": profiles,
        "credential_slots": slots,
        "policy": default_ai_policy(),
    }


class _FakeClient:
    def __init__(self, model: str, *, data: dict | None = None) -> None:
        self.model = model
        self.data = {"fallback_e2e": "ok"} if data is None else data
        self.key_detected = True
        self.request_attempts = 0
        self.options: list[dict] = []

    def chat_json(self, messages, **options):
        self.request_attempts += 1
        self.options.append(dict(options))
        return AIJsonResult(
            content=json.dumps(self.data),
            finish_reason="stop",
            usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            model=self.model,
            data=dict(self.data),
            requested_model=str(options.get("model") or ""),
        )


class ProviderFallbackWorkloadTests(unittest.TestCase):
    def test_route_plan_uses_in_memory_two_provider_overlay(self):
        config = _config()
        before = deepcopy(config)

        _registry, routes = prepare_route_plan(config)

        self.assertEqual(config, before)
        self.assertEqual([route.provider_id for route in routes], ["deepseek", "relay"])
        self.assertEqual(routes[0].budget["max_retries"], 0)
        self.assertEqual(routes[0].budget["max_requests"], 2)
        self.assertEqual(routes[0].budget["max_total_tokens"], 16_384)
        self.assertEqual(routes[1].reasoning_strength, "off")

    def test_fake_transport_proves_failed_primary_and_valid_fallback(self):
        clients = {}

        def factory(route, _timeout):
            client = _FakeClient(route.model)
            clients[route.provider_id] = client
            return client

        with tempfile.TemporaryDirectory() as temporary:
            report = run_provider_fallback_workload(
                Path(temporary), _config(), client_factory=factory
            )

        self.assertTrue(report["passed"])
        self.assertEqual(report["failure_injection"]["code"], FAILURE_CODE)
        self.assertEqual(report["governance"]["provider_requests"], 2)
        self.assertEqual(
            [(leg["route_kind"], leg["status"], leg["error_code"]) for leg in report["governance"]["legs"]],
            [("primary", "failed", FAILURE_CODE), ("fallback", "complete", None)],
        )
        self.assertEqual(clients["deepseek"].request_attempts, 1)
        self.assertEqual(clients["relay"].request_attempts, 1)
        self.assertEqual(clients["deepseek"].options[0]["model"], "deepseek-v4-flash")
        self.assertEqual(clients["relay"].options[0]["model"], "relay-model")
        self.assertEqual(clients["relay"].options[0]["reasoning_effort"], "none")
        serialized = json.dumps(report)
        self.assertNotIn("Return exactly one JSON", serialized)
        self.assertNotIn("relay.example", serialized)

    def test_invalid_fallback_response_fails_closed(self):
        def factory(route, _timeout):
            return _FakeClient(
                route.model,
                data={"fallback_e2e": "wrong"} if route.provider_id == "relay" else None,
            )

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                FallbackWorkloadError, "fallback_response_validation_failed"
            ):
                run_provider_fallback_workload(
                    Path(temporary), _config(), client_factory=factory
                )

    def test_unverified_second_provider_is_rejected(self):
        config = _config()
        definition = config["providers"]["relay"]["models"]["relay-model"]
        definition.update(
            evidence="declared",
            evidence_hash=None,
            verified_capabilities=[],
            verified_reasoning_strengths=[],
        )
        with self.assertRaisesRegex(
            FallbackWorkloadError, "verified_cross_provider_route_unavailable"
        ):
            prepare_route_plan(config)

    def test_written_report_contains_no_absolute_path(self):
        clients = {}

        def factory(route, _timeout):
            clients[route.provider_id] = _FakeClient(route.model)
            return clients[route.provider_id]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = run_provider_fallback_workload(
                root, _config(), client_factory=factory
            )
            report_id = write_report(root, report)
            payload = (root / ".acm" / "reports" / "provider-fallback" / f"{report_id}.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(str(root.resolve()), payload)
            self.assertNotIn("content", payload.casefold())
            self.assertNotIn("credential", payload.casefold())


if __name__ == "__main__":
    unittest.main()
