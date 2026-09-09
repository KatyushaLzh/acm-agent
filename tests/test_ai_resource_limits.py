"""Resource exhaustion must preserve failures without same-budget retries."""

from pathlib import Path
import json
import tempfile
import unittest

from tools.acm_agent.config import load_config, save_config
from tools.acm_agent.provider import AIJsonResult, ProviderError
from tools.acm_agent.provider_config import (
    default_ai_policy, default_credential_slots, default_provider_config, default_task_profiles,
)
from tools.acm_agent.provider_governance import GovernedProviderClient
from tools.acm_agent.provider_registry import ProviderRegistry
from tools.acm_agent.service import AcmService
from tools.acm_agent.service_knowledge import _observed_summary_repairs
from tools.acm_agent.storage import Database


class ScriptedStructuredClient:
    key_detected = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.request_attempts = 0
        self.calls = []

    def structured(self, messages, **options):
        self.request_attempts += 1
        self.calls.append(options)
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def successful_result(*, finish_reason="stop"):
    return AIJsonResult('{"ok":true}', finish_reason, {"total_tokens": 4},
                        "deepseek-v4-flash", {"ok": True})


def resource_error(*, finish_reason="length", reason="max_output_tokens", retryable=False):
    return ProviderError("response_incomplete", "output limit", finish_reason=finish_reason,
                         retryable=retryable, model="deepseek-v4-flash",
                         usage={"input_tokens": 2, "output_tokens": 8, "total_tokens": 10},
                         protocol_details={"response_status": "incomplete", "incomplete_reason": reason})


class AiResourceLimitTests(unittest.TestCase):
    def governed(self, client, *, fallback=False):
        policy = default_ai_policy()
        policy["budgets"]["recommendation"].update(max_requests=4, max_retries=1, max_validation_repairs=1)
        policy["fallbacks"]["recommendation"] = ([{
            "provider_id": "deepseek", "model": "deepseek-v4-pro", "reasoning_strength": "auto"
        }] if fallback else [])
        registry = ProviderRegistry({
            "providers": default_provider_config(), "profiles": default_task_profiles(),
            "credential_slots": default_credential_slots(), "policy": policy,
        })
        return GovernedProviderClient(registry.route_plan("recommendation"),
                                      lambda _route, _timeout: client, sleep=lambda _: None)

    def call(self, governed):
        return governed.structured([], json_schema={"type": "object"}, schema_name="resource_fixture")

    def test_token_limit_does_not_repair_retry_or_fallback_at_the_same_cap(self):
        for finish, reason in (("length", "unknown"), (None, "max_output_tokens"), ("max_tokens", "unknown")):
            with self.subTest(finish=finish, reason=reason):
                initial = resource_error(finish_reason=finish, reason=reason, retryable=True)
                client = ScriptedStructuredClient([initial, successful_result()])
                with self.assertRaises(ProviderError) as raised:
                    self.call(self.governed(client, fallback=True))
                self.assertIs(raised.exception, initial)
                self.assertEqual(client.request_attempts, 1)
                self.assertEqual(initial.usage["total_tokens"], 10)
                audit = initial.protocol_details["governance"]
                self.assertEqual(audit["validation_repairs"], 0)
                self.assertEqual(audit["fallbacks"], [])
                self.assertEqual(len(audit["legs"]), 1)
                self.assertEqual(audit["legs"][0]["error_code"], "response_incomplete")

    def test_parseable_json_with_length_finish_remains_incomplete(self):
        client = ScriptedStructuredClient([successful_result(finish_reason="length"), successful_result()])
        with self.assertRaises(ProviderError) as raised:
            self.call(self.governed(client))
        self.assertEqual(raised.exception.code, "response_incomplete")
        self.assertEqual(client.request_attempts, 1)
        self.assertEqual(raised.exception.usage["total_tokens"], 4)
        self.assertEqual(raised.exception.protocol_details["governance"]["validation_repairs"], 0)

    def test_non_resource_json_failure_still_repairs(self):
        client = ScriptedStructuredClient([
            ProviderError("invalid_json_output", "malformed", usage={"total_tokens": 3}), successful_result()
        ])
        result = self.call(self.governed(client))
        self.assertTrue(result.data["ok"])
        self.assertEqual(client.request_attempts, 2)
        self.assertEqual(result.usage["total_tokens"], 7)
        self.assertEqual(result.provider_metadata["governance"]["validation_repairs"], 1)

    def summary_service(self, responses):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        client = ScriptedStructuredClient(responses)
        service = AcmService(Path(temporary.name), provider_client_factory=lambda: client)
        service.setup("fixture", "42", skip_validate=True)
        attempt = service.start("CF1A")["attempt_id"]
        service.problem_context_save("CF1A", content="Read two integers and output their sum.")
        service.close("CF1A", result="AC", minutes=1, hint_level=0)
        target = service.knowledge_target_create(str(Path(temporary.name) / "summary.md"),
                                                 preset="algorithms-v1", allow_create=True)
        return service, client, attempt, target["target_id"]

    def test_failed_internal_summary_repair_is_counted_and_keeps_all_legs(self):
        service, client, attempt, target = self.summary_service([
            ProviderError("invalid_json_output", "malformed", usage={"total_tokens": 3}),
            resource_error(),
        ])
        result = service.knowledge_preview(attempt, target)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["proposal"])
        self.assertEqual(result["error"]["code"], "response_incomplete")
        self.assertEqual(result["ai"]["outcome"]["repair_attempts"], 1)
        self.assertEqual(client.request_attempts, 2)
        with Database(service.paths.database) as db:
            runs = db.query("SELECT repair_attempts,usage_json FROM ai_runs WHERE profile_id='summary'")
            self.assertEqual(runs[0]["repair_attempts"], 1)
            usage = json.loads(runs[0]["usage_json"])
            self.assertEqual(usage["total_tokens"], 13)
            self.assertEqual(usage["provider_requests"], 2)
            self.assertEqual([row["purpose"] for row in db.query("SELECT purpose FROM ai_run_legs ORDER BY ordinal")],
                             ["initial", "validation_repair"])
            self.assertEqual(db.ai_cache_status()["entries"], 0)

    def test_initial_summary_output_exhaustion_stays_one_request_without_repair(self):
        service, client, attempt, target = self.summary_service([resource_error()])
        result = service.knowledge_preview(attempt, target)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "response_incomplete")
        self.assertEqual(result["ai"]["outcome"]["repair_attempts"], 0)
        self.assertEqual(client.request_attempts, 1)
        self.assertEqual(client.calls[0]["max_tokens"], load_config(service.paths)["ai"]["policy"]["budgets"]["summary"]["max_output_tokens"])

    def test_summary_repair_blocked_before_provider_call_is_not_counted(self):
        service, client, attempt, target = self.summary_service([successful_result()])
        config = load_config(service.paths)
        config["ai"]["policy"]["budgets"]["summary"]["max_requests"] = 1
        config["ai"]["policy"]["budgets"]["summary"]["max_retries"] = 0
        save_config(service.paths, config)
        result = service.knowledge_preview(attempt, target)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "budget_exceeded")
        self.assertEqual(result["ai"]["outcome"]["repair_attempts"], 0)
        self.assertEqual(client.request_attempts, 1)

    def test_error_repair_count_can_be_recovered_from_actual_leg_evidence(self):
        error = ProviderError("server_error", "failure", protocol_details={"governance": {
            "validation_repairs": 0, "legs": [{"purpose": "initial"}, {"purpose": "validation_repair"}]
        }})
        self.assertEqual(_observed_summary_repairs(error), 1)


if __name__ == "__main__":
    unittest.main()
