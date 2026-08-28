from __future__ import annotations

from dataclasses import replace
from contextlib import redirect_stdout
import argparse
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.acm_agent.ai_telemetry import estimate_cost, load_price_catalog
from tools.acm_agent import cli
from tools.acm_agent.config import CONFIG_VERSION, Paths, load_config
from tools.acm_agent.provider import (
    AIJsonResult,
    AIResult,
    AIStreamEvent,
    OUTPUT_TOKEN_LIMIT_MESSAGE,
    ProviderConfigurationError,
    ProviderError,
    ProviderHealth,
)
from tools.acm_agent.provider_config import (
    default_ai_policy,
    default_credential_slots,
    default_provider_config,
    default_task_profiles,
    validate_ai_policy,
)
from tools.acm_agent.provider_governance import GovernedProviderClient
from tools.acm_agent.provider_registry import ProviderRegistry
from tools.acm_agent.service_core import ServiceCoreMixin
from tools.acm_agent.storage import Database, SCHEMA_VERSION


class _ScriptedClient:
    def __init__(self, script):
        self.script = list(script)
        self.request_attempts = 0
        self.key_detected = True

    def chat(self, messages, **options):
        self.request_attempts += 1
        action = self.script.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    chat_json = chat

    def stream_chat(self, messages, **options):
        raise NotImplementedError

    def test_connection(self, model):
        return ProviderHealth(ok=True, requested_model=model, resolved_model=model)

    def capabilities(self, model):
        raise NotImplementedError


class _StreamingScriptedClient(_ScriptedClient):
    def __init__(self, script):
        super().__init__(script)
        self.retries = 2

    def stream_chat(self, messages, **options):
        self.request_attempts += 1
        action = self.script.pop(0)
        if isinstance(action, BaseException):
            raise action
        yield from action


class _JsonRetryAwareClient(_ScriptedClient):
    def __init__(self, script):
        super().__init__(script)
        self.json_retry_options = []

    def chat_json(self, messages, *, json_retries=1, **options):
        self.json_retry_options.append(json_retries)
        return self.chat(messages, **options)


class _OptionsCapturingClient(_ScriptedClient):
    def __init__(self, script):
        super().__init__(script)
        self.calls = []

    def chat(self, messages, **options):
        self.calls.append(dict(options))
        return super().chat(messages, **options)


class _CapturingStructuredClient(_ScriptedClient):
    def __init__(self, script):
        super().__init__(script)
        self.structured_calls = []

    def structured(self, messages, **options):
        self.request_attempts += 1
        self.structured_calls.append(([dict(item) for item in messages], dict(options)))
        action = self.script.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class _StrictStructuredClient(_ScriptedClient):
    def structured(
        self,
        messages,
        *,
        json_schema,
        schema_name,
        model=None,
        max_tokens=None,
        request_timeout=None,
        request_retries=None,
    ):
        self.request_attempts += 1
        action = self.script.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


def _ai_config(*, max_requests=3, max_retries=1, max_total_tokens=100):
    policy = default_ai_policy()
    policy["budgets"]["recommendation"].update(
        max_requests=max_requests,
        max_retries=max_retries,
        max_total_tokens=max_total_tokens,
    )
    policy["fallbacks"]["recommendation"] = [
        {
            "provider_id": "deepseek",
            "model": "deepseek-v4-pro",
            "reasoning_strength": "auto",
        }
    ]
    return {
        "providers": default_provider_config(),
        "profiles": default_task_profiles(),
        "credential_slots": default_credential_slots(),
        "policy": policy,
    }


def _retryable(code="timeout", tokens=5):
    return ProviderError(
        code,
        "temporary failure",
        retryable=True,
        usage={"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens},
    )


def _structured_result(model: str, *, tokens: int = 4) -> AIJsonResult:
    return AIJsonResult(
        content='{"ok":true}',
        finish_reason="stop",
        usage={"input_tokens": tokens - 1, "output_tokens": 1, "total_tokens": tokens},
        model=model,
        data={"ok": True},
    )


class Stage3PolicyTests(unittest.TestCase):
    def test_default_task_budgets_match_paid_calibration(self):
        budgets = default_ai_policy()["budgets"]

        expected = {
            "recommendation": (1, 120.0, 4_096, 300_000, 3),
            "plan_organize": (1, 120.0, 16_000, 160_000, 3),
            "plan_generate": (1, 300.0, 32_000, 400_000, 6),
            "coaching": (1, 120.0, 8_192, 200_000, 3),
            "patch": (1, 240.0, 12_000, 260_000, 3),
            "summary": (1, 180.0, 8_192, 240_000, 3),
        }
        for profile_id, values in expected.items():
            budget = budgets[profile_id]
            self.assertEqual(
                (
                    budget["max_retries"],
                    budget["request_timeout_seconds"],
                    budget["max_output_tokens"],
                    budget["max_total_tokens"],
                    budget["max_requests"],
                ),
                values,
            )

    def test_v10_migrates_to_six_budgets_and_disabled_money_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = Paths.for_root(Path(temporary))
            paths.ensure()
            old = {
                "version": 10,
                "accounts": {"codeforces": {"handle": ""}, "luogu": {"uid": ""}},
                "recommendation": {},
                "sync": {},
                "ai": {
                    "providers": default_provider_config(),
                    "profiles": default_task_profiles(),
                    "credential_slots": default_credential_slots(),
                },
            }
            paths.config.write_text(json.dumps(old), encoding="utf-8")
            config = load_config(paths)
            self.assertEqual(config["version"], CONFIG_VERSION)
            self.assertEqual(set(config["ai"]["policy"]["budgets"]), set(default_task_profiles()))
            self.assertEqual(
                config["ai"]["policy"]["hard_limits"],
                {"daily_cny": None, "monthly_cny": None},
            )

    def test_coaching_cross_route_fallback_is_rejected(self):
        policy = default_ai_policy()
        policy["fallbacks"]["coaching"] = [
            {"provider_id": "deepseek", "model": "deepseek-v4-pro"}
        ]
        with self.assertRaisesRegex(ProviderError, "route pinning"):
            validate_ai_policy(policy)

    def test_fallback_route_is_capability_gated_before_use(self):
        config = _ai_config()
        config["providers"]["deepseek"]["models"]["deepseek-v4-pro"]["capabilities"]["json_object"] = False
        registry = ProviderRegistry(config)
        with self.assertRaisesRegex(ProviderError, "lacks"):
            registry.route_plan("recommendation")


class Stage3GovernorTests(unittest.TestCase):
    def test_structured_protocol_failure_gets_one_safe_validation_repair(self):
        config = _ai_config(max_requests=2, max_retries=0)
        config["policy"]["fallbacks"]["recommendation"] = []
        route = ProviderRegistry(config).route("recommendation")
        client = _CapturingStructuredClient([
            ProviderError(
                "invalid_json_output", "raw-private-provider-output",
                retryable=False,
                usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            ),
            _structured_result(route.model, tokens=4),
        ])
        governed = GovernedProviderClient([route], lambda _route, _timeout: client)
        result = governed.structured(
            [{"role": "system", "content": "stable"}],
            json_schema={"type": "object"}, schema_name="result",
        )
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(result.usage["total_tokens"], 7)
        self.assertEqual(result.usage["provider_requests"], 2)
        self.assertEqual(result.usage["protocol_repairs"], 1)
        self.assertEqual(len(client.structured_calls), 2)
        repair_message = client.structured_calls[1][0][-1]["content"]
        self.assertIn("STRUCTURED_VALIDATION_REPAIR_V1", repair_message)
        self.assertIn("validation_code=invalid_json_output", repair_message)
        self.assertNotIn("raw-private-provider-output", repair_message)
        audit = result.provider_metadata["governance"]
        self.assertEqual(audit["validation_repairs"], 1)
        self.assertEqual(
            [leg["purpose"] for leg in audit["legs"]],
            ["initial", "validation_repair"],
        )
        self.assertEqual(audit["legs"][1]["validation_code"], "invalid_json_output")

    def test_structured_repairs_empty_and_length_codes_but_not_content_filter(self):
        for error in (
            ProviderError("empty_json_output", "empty", retryable=False),
            ProviderError(
                "response_incomplete", "length", retryable=False,
                finish_reason="length",
            ),
            ProviderError(
                "unexpected_empty_code", "length", retryable=False,
                finish_reason="length",
            ),
        ):
            with self.subTest(code=error.code):
                config = _ai_config(max_requests=2, max_retries=0)
                config["policy"]["fallbacks"]["recommendation"] = []
                route = ProviderRegistry(config).route("recommendation")
                client = _CapturingStructuredClient([
                    error, _structured_result(route.model)
                ])
                result = GovernedProviderClient(
                    [route], lambda _route, _timeout: client
                ).structured([], json_schema={"type": "object"}, schema_name="result")
                self.assertTrue(result.data["ok"])
                self.assertEqual(client.request_attempts, 2)

        for error in (
            ProviderError("content_filter", "blocked", retryable=False),
            ProviderError(
                "response_incomplete", "blocked", retryable=False,
                finish_reason="content_filter",
            ),
            ProviderError("authentication_failed", "auth", status=401, retryable=False),
            ProviderError("invalid_json_output", "auth-shaped", status=401, retryable=False),
            ProviderError("insufficient_balance", "balance", status=402, retryable=False),
            ProviderError("invalid_request", "bad", status=422, retryable=False),
            ProviderConfigurationError("invalid_json_schema", "config"),
        ):
            with self.subTest(no_repair=error.code):
                config = _ai_config(max_requests=2, max_retries=0)
                config["policy"]["fallbacks"]["recommendation"] = []
                route = ProviderRegistry(config).route("recommendation")
                client = _CapturingStructuredClient([
                    error, _structured_result(route.model)
                ])
                with self.assertRaises(ProviderError) as captured:
                    GovernedProviderClient(
                        [route], lambda _route, _timeout: client
                    ).structured([], json_schema={"type": "object"}, schema_name="result")
                self.assertEqual(captured.exception.code, error.code)
                self.assertEqual(client.request_attempts, 1)

    def test_structured_success_with_length_finish_is_repaired_or_rejected(self):
        config = _ai_config(max_requests=2, max_retries=0)
        config["policy"]["fallbacks"]["recommendation"] = []
        route = ProviderRegistry(config).route("recommendation")
        truncated = replace(_structured_result(route.model), finish_reason="length")
        client = _CapturingStructuredClient([
            truncated, _structured_result(route.model),
        ])

        result = GovernedProviderClient(
            [route], lambda _route, _timeout: client
        ).structured([], json_schema={"type": "object"}, schema_name="result")

        self.assertTrue(result.data["ok"])
        self.assertEqual(client.request_attempts, 2)
        self.assertIn(
            "validation_code=response_incomplete",
            client.structured_calls[1][0][-1]["content"],
        )

        always_truncated = _CapturingStructuredClient([truncated, truncated])
        with self.assertRaises(ProviderError) as captured:
            GovernedProviderClient(
                [route], lambda _route, _timeout: always_truncated
            ).structured([], json_schema={"type": "object"}, schema_name="result")
        self.assertEqual(captured.exception.code, "response_incomplete")
        self.assertEqual(captured.exception.finish_reason, "length")
        self.assertEqual(str(captured.exception), OUTPUT_TOKEN_LIMIT_MESSAGE)

    def test_transport_retry_exhaustion_blocks_semantic_repair_and_preserves_usage(self):
        config = _ai_config(max_requests=2, max_retries=1)
        config["policy"]["fallbacks"]["recommendation"] = []
        route = ProviderRegistry(config).route("recommendation")
        client = _CapturingStructuredClient([
            _retryable(tokens=5),
            ProviderError(
                "invalid_json_output", "invalid", retryable=False,
                usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            ),
        ])
        governed = GovernedProviderClient(
            [route], lambda _route, _timeout: client, sleep=lambda _delay: None
        )
        with self.assertRaises(ProviderError) as captured:
            governed.structured(
                [], json_schema={"type": "object"}, schema_name="result"
            )
        self.assertEqual(captured.exception.code, "budget_exceeded")
        self.assertEqual(client.request_attempts, 2)
        self.assertEqual(captured.exception.usage["provider_requests"], 2)
        self.assertEqual(captured.exception.usage["total_tokens"], 8)
        governance = captured.exception.protocol_details["governance"]
        self.assertEqual(governance["blocked_reason"], "request_budget_exhausted")
        self.assertEqual(governance["validation_repairs"], 0)

    def test_validation_repair_uses_shared_request_budget_and_audits_purpose(self):
        config = _ai_config(max_requests=2, max_retries=0)
        config["policy"]["fallbacks"]["recommendation"] = []
        route = ProviderRegistry(config).route("recommendation")
        client = _ScriptedClient([
            AIResult(content='{"ok":false}', finish_reason="stop", usage={"total_tokens": 2}, model=route.model),
            AIResult(content='{"ok":true}', finish_reason="stop", usage={"total_tokens": 2}, model=route.model),
        ])
        governed = GovernedProviderClient([route], lambda _route, _timeout: client)
        governed.structured(
            [], json_schema={"type": "object"}, schema_name="result"
        )
        repaired = governed.structured(
            [], json_schema={"type": "object"}, schema_name="result",
            purpose="validation_repair", validation_code="missing_problem_keys",
        )
        audit = repaired.provider_metadata["governance"]
        self.assertEqual(audit["validation_repairs"], 1)
        self.assertEqual(
            [leg["purpose"] for leg in audit["legs"]],
            ["initial", "validation_repair"],
        )
        self.assertEqual(audit["legs"][1]["validation_code"], "missing_problem_keys")
        with self.assertRaisesRegex(ProviderError, "budget"):
            governed.structured(
                [], json_schema={"type": "object"}, schema_name="result",
                purpose="validation_repair", validation_code="second_repair",
            )
        self.assertEqual(client.request_attempts, 2)

    def test_transport_retry_can_exhaust_budget_before_validation_repair(self):
        config = _ai_config(max_requests=2, max_retries=1)
        config["policy"]["fallbacks"]["recommendation"] = []
        route = ProviderRegistry(config).route("recommendation")
        client = _ScriptedClient([
            _retryable(),
            AIResult(content='{}', finish_reason="stop", usage={"total_tokens": 2}, model=route.model),
        ])
        governed = GovernedProviderClient(
            [route], lambda _route, _timeout: client, sleep=lambda _delay: None
        )
        result = governed.structured(
            [], json_schema={"type": "object"}, schema_name="result"
        )
        self.assertEqual(
            [leg["purpose"] for leg in result.provider_metadata["governance"]["legs"]],
            ["initial", "transport_retry"],
        )
        with self.assertRaisesRegex(ProviderError, "request_budget_exhausted"):
            governed.structured(
                [], json_schema={"type": "object"}, schema_name="result",
                purpose="validation_repair", validation_code="invalid_schema",
            )
        self.assertEqual(client.request_attempts, 2)

    def test_retry_and_model_fallback_share_one_request_and_token_ledger(self):
        registry = ProviderRegistry(_ai_config())
        routes = registry.route_plan("recommendation")
        primary = _ScriptedClient([_retryable(), _retryable()])
        fallback = _ScriptedClient([
            AIResult(
                content="OK",
                finish_reason="stop",
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                model="deepseek-v4-pro",
            )
        ])
        clients = {"deepseek-v4-flash": primary, "deepseek-v4-pro": fallback}
        governed = GovernedProviderClient(
            routes, lambda route, timeout: clients[route.model], sleep=lambda _: None
        )
        result = governed.chat([], model="deepseek-v4-flash", max_tokens=99)
        self.assertEqual(result.model, "deepseek-v4-pro")
        self.assertEqual(result.usage["provider_requests"], 3)
        self.assertEqual(result.usage["total_tokens"], 22)
        audit = result.provider_metadata["governance"]
        self.assertEqual(len(audit["legs"]), 3)
        self.assertEqual(len(audit["fallbacks"]), 1)
        self.assertEqual(audit["actual"]["model"], "deepseek-v4-pro")

    def test_each_fallback_leg_rebinds_model_and_reasoning_options(self):
        config = _ai_config(max_requests=2, max_retries=0)
        routes = ProviderRegistry(config).route_plan("recommendation")
        primary = _OptionsCapturingClient([_retryable()])
        fallback = _OptionsCapturingClient(
            [
                AIResult(
                    content="OK",
                    finish_reason="stop",
                    usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    model=routes[1].model,
                )
            ]
        )
        clients = {routes[0].model: primary, routes[1].model: fallback}

        result = GovernedProviderClient(
            routes,
            lambda route, _timeout: clients[route.model],
            sleep=lambda _delay: None,
        ).chat(
            [],
            model="caller-primary-model",
            thinking=not routes[0].thinking,
            reasoning_effort="caller-primary-effort",
        )

        self.assertEqual(result.model, routes[1].model)
        self.assertEqual(primary.calls[0]["model"], routes[0].model)
        self.assertEqual(primary.calls[0]["thinking"], routes[0].thinking)
        self.assertEqual(
            primary.calls[0]["reasoning_effort"], routes[0].reasoning_effort
        )
        self.assertEqual(fallback.calls[0]["model"], routes[1].model)
        self.assertEqual(fallback.calls[0]["thinking"], routes[1].thinking)
        self.assertEqual(
            fallback.calls[0]["reasoning_effort"], routes[1].reasoning_effort
        )

    def test_request_budget_never_allows_n_plus_one(self):
        config = _ai_config(max_requests=2, max_retries=1)
        config["policy"]["fallbacks"]["recommendation"] = []
        route = ProviderRegistry(config).route("recommendation")
        client = _ScriptedClient([_retryable(), _retryable(), _retryable()])
        governed = GovernedProviderClient([route], lambda _route, _timeout: client, sleep=lambda _: None)
        with self.assertRaises(ProviderError) as captured:
            governed.chat([], model=route.model)
        self.assertEqual(client.request_attempts, 2)
        self.assertEqual(captured.exception.usage["provider_requests"], 2)

    def test_later_logical_call_budget_block_does_not_repeat_prior_usage(self):
        config = _ai_config(max_requests=1, max_retries=0)
        config["policy"]["fallbacks"]["recommendation"] = []
        route = ProviderRegistry(config).route("recommendation")
        client = _ScriptedClient([
            AIResult(
                content="OK",
                finish_reason="stop",
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                model=route.model,
            )
        ])
        governed = GovernedProviderClient([route], lambda _route, _timeout: client)
        first = governed.chat([], model=route.model)
        self.assertEqual(first.usage["total_tokens"], 12)
        with self.assertRaises(ProviderError) as captured:
            governed.chat([], model=route.model)
        self.assertEqual(captured.exception.usage, {})
        self.assertEqual(
            captured.exception.protocol_details["governance"]["provider_requests"], 1
        )

    def test_observed_token_limit_prevents_retry_and_fallback(self):
        registry = ProviderRegistry(_ai_config(max_total_tokens=5))
        routes = registry.route_plan("recommendation")
        primary = _ScriptedClient([_retryable(tokens=5), _retryable(tokens=1)])
        fallback = _ScriptedClient([])
        clients = {"deepseek-v4-flash": primary, "deepseek-v4-pro": fallback}
        governed = GovernedProviderClient(routes, lambda route, timeout: clients[route.model], sleep=lambda _: None)
        with self.assertRaises(ProviderError):
            governed.chat([], model=routes[0].model)
        self.assertEqual(primary.request_attempts, 1)
        self.assertEqual(fallback.request_attempts, 0)

    def test_nonretryable_error_never_crosses_model(self):
        registry = ProviderRegistry(_ai_config())
        routes = registry.route_plan("recommendation")
        primary = _ScriptedClient([ProviderError("auth_failed", "no", status=401)])
        fallback = _ScriptedClient([])
        clients = {"deepseek-v4-flash": primary, "deepseek-v4-pro": fallback}
        with self.assertRaises(ProviderError):
            GovernedProviderClient(routes, lambda route, timeout: clients[route.model]).chat([])
        self.assertEqual(fallback.request_attempts, 0)

    def test_allow_request_receives_route_for_primary_retry_and_fallback(self):
        routes = ProviderRegistry(_ai_config()).route_plan("recommendation")
        primary = _ScriptedClient([_retryable(), _retryable()])
        fallback = _ScriptedClient([
            AIResult(
                content="OK",
                finish_reason="stop",
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                model=routes[1].model,
            )
        ])
        clients = {routes[0].model: primary, routes[1].model: fallback}
        checked_routes = []
        governed = GovernedProviderClient(
            routes,
            lambda route, _timeout: clients[route.model],
            allow_request=checked_routes.append,
            sleep=lambda _: None,
        )
        governed.chat([])
        self.assertEqual(
            [(route.provider_id, route.model) for route in checked_routes],
            [
                (routes[0].provider_id, routes[0].model),
                (routes[0].provider_id, routes[0].model),
                (routes[1].provider_id, routes[1].model),
            ],
        )

    def test_stream_retries_are_governed_and_each_runs_route_check(self):
        config = _ai_config(max_requests=2, max_retries=1)
        config["policy"]["fallbacks"]["recommendation"] = []
        route = ProviderRegistry(config).route("recommendation")
        client = _StreamingScriptedClient([
            _retryable(),
            [
                AIStreamEvent(
                    "done",
                    usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    model=route.model,
                )
            ],
        ])
        checked_routes = []
        governed = GovernedProviderClient(
            [route],
            lambda _route, _timeout: client,
            allow_request=checked_routes.append,
        )
        events = list(governed.stream_chat([]))
        self.assertEqual([event.kind for event in events], ["done"])
        self.assertEqual(client.request_attempts, 2)
        self.assertEqual(client.retries, 0)
        self.assertEqual(checked_routes, [route, route])

    def test_adapter_json_repairs_are_disabled_outside_governance_ledger(self):
        config = _ai_config(max_requests=1, max_retries=0)
        config["policy"]["fallbacks"]["recommendation"] = []
        route = ProviderRegistry(config).route("recommendation")
        client = _JsonRetryAwareClient([
            AIResult(
                content="{}",
                finish_reason="stop",
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                model=route.model,
            )
        ])
        GovernedProviderClient(
            [route], lambda _route, _timeout: client
        ).chat_json([], json_retries=1)
        self.assertEqual(client.json_retry_options, [0])
        self.assertEqual(client.request_attempts, 1)

    def test_structured_strips_json_retry_option_when_adapter_rejects_it(self):
        config = _ai_config(max_requests=1, max_retries=0)
        config["policy"]["fallbacks"]["recommendation"] = []
        route = ProviderRegistry(config).route("recommendation")
        client = _StrictStructuredClient([_structured_result(route.model)])

        result = GovernedProviderClient(
            [route], lambda _route, _timeout: client
        ).structured(
            [],
            json_schema={"type": "object"},
            schema_name="strict_test",
            json_retries=0,
        )

        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(client.request_attempts, 1)


class Stage3DeepSeekHardLimitTests(unittest.TestCase):
    @staticmethod
    def _service() -> ServiceCoreMixin:
        service = ServiceCoreMixin()
        service.paths = SimpleNamespace(database=Path("unused-stage3-test.db"))
        return service

    @staticmethod
    def _routes():
        routes = ProviderRegistry(_ai_config(max_retries=0)).route_plan(
            "recommendation"
        )
        relay = replace(
            routes[0],
            provider_id="relay",
            model="relay-model",
            budget={**routes[0].budget, "max_retries": 0},
        )
        return relay, routes[1]

    def test_non_deepseek_route_does_not_read_or_apply_deepseek_spend(self):
        service = self._service()
        relay, _deepseek = self._routes()
        with mock.patch(
            "tools.acm_agent.service_core.load_config",
            side_effect=AssertionError("non-DeepSeek route must not read cost policy"),
        ), mock.patch(
            "tools.acm_agent.service_core.Database",
            side_effect=AssertionError("non-DeepSeek route must not query spend"),
        ):
            service._assert_ai_spend_available(relay)

    def test_deepseek_unknown_cost_fails_closed(self):
        service = self._service()
        _relay, deepseek = self._routes()
        database = mock.MagicMock()
        database.return_value.__enter__.return_value.ai_cost_spend.return_value = {
            "daily": {"known_cny": 0.0, "unknown_runs": 1, "runs": 1},
            "monthly": {"known_cny": 0.0, "unknown_runs": 1, "runs": 1},
        }
        config = {
            "ai": {
                "policy": {
                    "hard_limits": {"daily_cny": 1.0, "monthly_cny": None}
                }
            }
        }
        with mock.patch(
            "tools.acm_agent.service_core.load_config", return_value=config
        ), mock.patch("tools.acm_agent.service_core.Database", database):
            with self.assertRaises(ProviderConfigurationError) as captured:
                service._assert_ai_spend_available(deepseek)
        self.assertEqual(captured.exception.code, "cost_limit_unknown")

    def test_deepseek_reached_cost_limit_is_blocked(self):
        service = self._service()
        _relay, deepseek = self._routes()
        database = mock.MagicMock()
        database.return_value.__enter__.return_value.ai_cost_spend.return_value = {
            "daily": {"known_cny": 1.0, "unknown_runs": 0, "runs": 2},
            "monthly": {"known_cny": 1.0, "unknown_runs": 0, "runs": 2},
        }
        config = {
            "ai": {
                "policy": {
                    "hard_limits": {"daily_cny": 1.0, "monthly_cny": None}
                }
            }
        }
        with mock.patch(
            "tools.acm_agent.service_core.load_config", return_value=config
        ), mock.patch("tools.acm_agent.service_core.Database", database):
            with self.assertRaises(ProviderConfigurationError) as captured:
                service._assert_ai_spend_available(deepseek)
        self.assertEqual(captured.exception.code, "cost_limit_exceeded")

    def test_fallback_into_deepseek_checks_limit_before_request(self):
        service = self._service()
        relay_route, deepseek_route = self._routes()
        relay = _ScriptedClient([_retryable()])
        deepseek = _ScriptedClient([])
        clients = {"relay": relay, "deepseek": deepseek}
        database = mock.MagicMock()
        database.return_value.__enter__.return_value.ai_cost_spend.return_value = {
            "daily": {"known_cny": 0.0, "unknown_runs": 1, "runs": 1},
            "monthly": {"known_cny": 0.0, "unknown_runs": 1, "runs": 1},
        }
        config = {
            "ai": {
                "policy": {
                    "hard_limits": {"daily_cny": 1.0, "monthly_cny": None}
                }
            }
        }
        governed = GovernedProviderClient(
            [relay_route, deepseek_route],
            lambda route, _timeout: clients[route.provider_id],
            allow_request=service._assert_ai_spend_available,
            sleep=lambda _: None,
        )
        with mock.patch(
            "tools.acm_agent.service_core.load_config", return_value=config
        ), mock.patch("tools.acm_agent.service_core.Database", database):
            with self.assertRaises(ProviderConfigurationError) as captured:
                governed.chat([])
        self.assertEqual(captured.exception.code, "cost_limit_unknown")
        self.assertEqual(relay.request_attempts, 1)
        self.assertEqual(deepseek.request_attempts, 0)
        database.assert_called_once()


class Stage3CostStorageTests(unittest.TestCase):
    def test_provider_identity_prevents_same_named_relay_from_using_deepseek_price(self):
        usage = {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_tokens": 0,
            "cache_miss_tokens": 10,
        }
        estimate = estimate_cost(
            provider_id="relay",
            model="deepseek-v4-flash",
            usage=usage,
            created_at="2026-08-25T02:00:00+00:00",
        )
        self.assertEqual(estimate["status"], "unknown")
        self.assertEqual(estimate["unknown_reason"], "model_price_missing")

    def test_governance_legs_and_versioned_reprice_are_append_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            with Database(path) as db:
                self.assertEqual(SCHEMA_VERSION, 24)
                db.create_ai_run(
                    "run-stage3",
                    kind="recommendation",
                    model="deepseek-v4-flash",
                    provider_id="deepseek",
                    profile_id="recommendation",
                    requested_model="deepseek-v4-flash",
                    created_at="2026-08-25T02:00:00+00:00",
                )
                usage = {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                    "cache_read_tokens": 4,
                    "cache_miss_tokens": 6,
                    "provider_requests": 1,
                }
                governance = {
                    "version": 1,
                    "profile_id": "recommendation",
                    "outcome": "complete",
                    "actual": {"provider_id": "deepseek", "model": "deepseek-v4-flash"},
                    "fallbacks": [],
                    "legs": [
                        {
                            "ordinal": 0,
                            "route_kind": "primary",
                            "provider_id": "deepseek",
                            "model": "deepseek-v4-flash",
                            "resolved_model": "deepseek-v4-flash",
                            "reasoning_strength": "auto",
                            "status": "complete",
                            "usage": usage,
                        }
                    ],
                }
                before_usage = json.dumps(usage, sort_keys=True)
                row = db.update_ai_run(
                    "run-stage3",
                    status="complete",
                    usage=usage,
                    resolved_model="deepseek-v4-flash",
                    resolved_provider_id="deepseek",
                    governance=governance,
                    fallback={"version": 1, "outcome": "complete", "events": []},
                )
                self.assertEqual(row["cache_status"], "hit")
                self.assertEqual(len(db.ai_run_legs("run-stage3")), 1)
                self.assertEqual(json.loads(row["estimated_cost_json"])["status"], "known")
                result = db.reprice_ai_runs(catalog=load_price_catalog())
                self.assertEqual(result["catalog_version"], "deepseek-2026-08-26-cny-v1")
                self.assertEqual(
                    json.dumps(json.loads(db.ai_run("run-stage3")["usage_json"]), sort_keys=True),
                    before_usage,
                )
                estimates = db.query(
                    "SELECT * FROM ai_run_cost_estimates WHERE run_id=?", ("run-stage3",)
                )
                self.assertEqual(len(estimates), 1)

                legacy_usd = {
                    "status": "known",
                    "currency": "USD",
                    "amount": 999.0,
                    "price_version": "legacy-usd",
                    "catalog_sha256": "legacy-usd-hash",
                }
                db.connection.execute(
                    "UPDATE ai_runs SET estimated_cost_json=? WHERE id=?",
                    (json.dumps(legacy_usd), "run-stage3"),
                )
                audit = db.ai_cost_audit(days=30)
                expected = estimate_cost(
                    provider_id="deepseek",
                    model="deepseek-v4-flash",
                    usage=usage,
                    created_at="2026-08-25T02:00:00+00:00",
                )
                self.assertEqual(audit["deepseek_cost"]["currency"], "CNY")
                self.assertEqual(
                    audit["deepseek_cost"]["known_estimated_cny"],
                    expected["amount"],
                )


class Stage3CostCliTests(unittest.TestCase):
    @staticmethod
    def _payload():
        return {
            "ok": True,
            "audit": {
                "window_days": 30,
                "deepseek_cost": {
                    "provider_id": "deepseek",
                    "runs": 2,
                    "known_estimated_cny": 0.25,
                    "unknown_cost_runs": 1,
                    "partial_cost_runs": 0,
                },
                "all_model_tokens": {
                    "runs": 3,
                    "total_tokens_known": 12345,
                    "input_tokens_known": 10000,
                    "output_tokens_known": 2345,
                    "unknown_runs": 1,
                },
                "cache_metrics": {
                    "cache_read_tokens_known": 2500,
                    "eligible_input_tokens": 10000,
                    "hit_rate_percent": 25.0,
                    "observed_runs": 2,
                    "unknown_runs": 1,
                    "invalid_runs": 0,
                },
            },
        }

    def test_ai_costs_human_output_separates_deepseek_cost_and_all_model_tokens(self):
        payload = self._payload()
        service = mock.MagicMock()
        service.ai_costs.return_value = payload
        output = io.StringIO()
        args = argparse.Namespace(reprice=False, days=30, json=False)
        with mock.patch("tools.acm_agent.cli._service", return_value=service), redirect_stdout(output):
            self.assertEqual(cli.command_ai_costs(args, object()), 0)
        rendered = output.getvalue()
        self.assertIn("DeepSeek 估算费用：至少 ¥0.250000", rendered)
        self.assertIn("全模型 Tokens：12,345", rendered)
        self.assertIn("全模型缓存命中率：25.0%", rendered)

    def test_ai_costs_json_output_preserves_payload(self):
        payload = self._payload()
        service = mock.MagicMock()
        service.ai_costs.return_value = payload
        output = io.StringIO()
        args = argparse.Namespace(reprice=False, days=30, json=True)
        with mock.patch("tools.acm_agent.cli._service", return_value=service), redirect_stdout(output):
            self.assertEqual(cli.command_ai_costs(args, object()), 0)
        self.assertEqual(json.loads(output.getvalue()), payload)


if __name__ == "__main__":
    unittest.main()
