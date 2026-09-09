from __future__ import annotations

import json
import unittest

from tests import test_ai_service as service_fixture
from tests import test_stage3_governance as governor_fixture
from tools.acm_agent.provider import AIJsonResult, AIStreamEvent, ProviderError
from tools.acm_agent.provider_governance import GovernedProviderClient
from tools.acm_agent.provider_registry import ProviderRegistry
from tools.acm_agent.storage import Database


class IntegratedAccountingTests(unittest.TestCase):
    def service_case(self):
        case = service_fixture.AiServiceTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        return case

    def test_later_structured_repair_returns_only_this_logical_call_requests(self):
        for repair_fails in (False, True):
            with self.subTest(repair_fails=repair_fails):
                route = ProviderRegistry(governor_fixture._ai_config(
                    max_requests=3, max_retries=0
                )).route("recommendation")
                repaired = (
                    ProviderError("permission_denied", "fixture", status=403,
                        usage={"total_tokens": 3})
                    if repair_fails else governor_fixture._structured_result(route.model)
                )
                client = governor_fixture._CapturingStructuredClient([
                    governor_fixture._structured_result(route.model),
                    ProviderError("invalid_json_output", "fixture", usage={"total_tokens": 2}),
                    repaired,
                ])
                governed = GovernedProviderClient([route], lambda _route, _timeout: client)
                first = governed.structured([], json_schema={"type": "object"}, schema_name="fixture")
                if repair_fails:
                    with self.assertRaises(ProviderError) as captured:
                        governed.structured([], json_schema={"type": "object"}, schema_name="fixture")
                    second_usage = captured.exception.usage
                else:
                    second_usage = governed.structured(
                        [], json_schema={"type": "object"}, schema_name="fixture"
                    ).usage
                self.assertEqual(client.request_attempts, 3)
                self.assertEqual(first.usage["provider_requests"], 1)
                self.assertEqual(second_usage["provider_requests"], 2)
                self.assertEqual(
                    first.usage["total_tokens"] + second_usage["total_tokens"],
                    governed.usage_snapshot["total_tokens"],
                )

    def test_stream_retry_final_usage_matches_persisted_run_and_legs(self):
        case = self.service_case()
        attempts = 0

        def stream(messages, **options):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ProviderError("timeout", "fixture retry", retryable=True,
                    usage={"input_tokens": 5, "output_tokens": 0, "total_tokens": 5})
            yield AIStreamEvent("delta", content="检查边界条件。")
            yield AIStreamEvent("done", finish_reason="stop",
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                model=options["model"])

        case.client.stream_chat = stream
        case.service.start("CF1A")
        conversation = case.service.ai_conversation_start("CF1A")
        events = list(case.service.ai_chat_stream(conversation["conversation_id"],
            message="请给一点提示", hint_level=1, delivery_mode="low_latency"))
        self.assertEqual(attempts, 2)
        self.assertEqual(events[-1]["data"]["status"], "complete")
        with Database(case.service.paths.database) as db:
            run = db.query("SELECT * FROM ai_runs WHERE kind='coaching'")[0]
            usage = json.loads(run["usage_json"])
            governance = json.loads(run["governance_json"])
            self.assertEqual(usage["total_tokens"], 7)
            self.assertEqual(usage["provider_requests"], 2)
            self.assertEqual(sum(leg["usage"]["total_tokens"] for leg in governance["legs"]), 7)
            assistant = db.query("SELECT usage_json FROM ai_messages WHERE role='assistant'")[0]
            self.assertEqual(json.loads(assistant["usage_json"]), usage)

    def test_stream_close_after_content_commits_interruption_and_releases_claim(self):
        case = self.service_case()
        case.service.start("CF1A")
        conversation_id = case.service.ai_conversation_start("CF1A")["conversation_id"]
        stream = case.service.ai_chat_stream(conversation_id, message="第一个提示", delivery_mode="low_latency")
        self.assertEqual(next(stream)["event"], "meta")
        delta = next(stream)
        self.assertEqual(delta["event"], "delta")
        stream.close()
        with Database(case.service.paths.database) as db:
            run = db.query("SELECT status,usage_json,governance_json FROM ai_runs WHERE kind='coaching'")[0]
            message = db.query("SELECT status,content FROM ai_messages WHERE role='assistant'")[0]
            self.assertEqual(run["status"], "interrupted")
            self.assertEqual(message["status"], "interrupted")
            self.assertEqual(message["content"], delta["data"]["content"])
            usage = json.loads(run["usage_json"])
            governance = json.loads(run["governance_json"])
            self.assertEqual(usage["provider_requests"], 1)
            self.assertNotIn("total_tokens", usage)
            self.assertEqual(governance["provider_requests"], 1)
            self.assertEqual(len(governance["legs"]), 1)
            self.assertEqual(governance["legs"][0]["error_code"], "stream_interrupted")
        next_stream = case.service.ai_chat_stream(conversation_id, message="第二个提示", delivery_mode="low_latency")
        next_stream.close()

    def test_patch_failed_repair_persists_complete_usage_and_latest_governance(self):
        case = self.service_case()
        attempts = 0

        def structured(messages, **options):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise ProviderError("permission_denied", "fixture failure", status=403,
                    usage={"total_tokens": 11})
            return AIJsonResult(content="fixture", finish_reason="stop",
                usage={"total_tokens": 7}, model=options["model"],
                data={"diagnosis": "需要修复", "replacement_code": "```cpp\nint main(){}\n```"})

        case.client.structured = structured
        case.service.start("CF1A")
        result = case.service.ai_patch_preview("CF1A", instruction="修复代码")
        self.assertFalse(result["ok"])
        self.assertEqual(attempts, 2)
        self.assertEqual(result["usage"]["total_tokens"], 18)
        with Database(case.service.paths.database) as db:
            run = db.query("SELECT usage_json,governance_json FROM ai_runs WHERE kind='patch'")[0]
            self.assertEqual(json.loads(run["usage_json"]), result["usage"])
            governance = json.loads(run["governance_json"])
            self.assertEqual(governance["provider_requests"], 2)
            self.assertEqual([leg["usage"]["total_tokens"] for leg in governance["legs"]], [7, 11])


if __name__ == "__main__":
    unittest.main()
