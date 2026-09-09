from __future__ import annotations

import json
import unittest

from tests import test_ai_service as service_fixture
from tools.acm_agent.deepseek import JsonChatResult
from tools.acm_agent.provider import ProviderError
from tools.acm_agent.service_ai import (
    AI_COACHING_PROMPT_VERSION,
    AI_RECOMMENDATION_PROMPT_VERSION,
    _recommendation_topic_constraints,
    _validate_coaching_content,
    _validate_recommendation_payload,
)
from tools.acm_agent.storage import Database


class LiveFailureRegressionTests(unittest.TestCase):
    def service_case(self):
        case = service_fixture.AiServiceTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        return case

    def validate(self, *, count, eligible, focus, selected):
        outbound = [
            {"problem_key": f"fixture:{i}", "knowledge_topics": eligible,
             "equivalent_rating": None}
            for i in range(len(selected))
        ]
        return _validate_recommendation_payload(
            {"focus_topics": focus, "ranked": [
                {"problem_key": item["problem_key"], "topic": topic}
                for item, topic in zip(outbound, selected)
            ]},
            outbound=outbound, tier_topics=eligible, selected_count=count,
            difficulty_targets={},
        )

    def test_focus_feedback_reports_effective_bounds(self):
        for count, eligible, focus, expected in (
            (1, ["a", "b"], ["a", "b"], "恰好 1 个"),
            (2, ["a", "b", "c"], ["a", "b", "c"], "恰好 2 个"),
            (4, ["a", "b", "c", "d"], ["a", "b", "c", "d"], "2 至 3 个"),
        ):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, expected):
                self.validate(count=count, eligible=eligible, focus=focus, selected=["a"] * count)
        output, _, focus = self.validate(
            count=4, eligible=["a"], focus=["a"], selected=["a"] * 4,
        )
        self.assertEqual(len(output), 4)
        self.assertEqual(focus, ["a"])
        self.assertEqual(_recommendation_topic_constraints(4, 1), {
            "min_focus_topics": 1, "max_focus_topics": 1, "max_problems_per_topic": None,
        })

    def test_diversity_cap_and_quantity_remain_enforced_with_precise_feedback(self):
        for selected, expected in (
            (["a"] * 4, "至少覆盖 2 个板块，实际覆盖 1 个"),
            (["a", "a", "a", "b"], "最多 2 题，实际最多 3 题"),
            (["a", "b"], "需要 4 题，实际为 2 题"),
        ):
            with self.subTest(selected=selected), self.assertRaisesRegex(ValueError, expected):
                self.validate(count=4, eligible=["a", "b"], focus=["a", "b"], selected=selected)
        with self.assertRaisesRegex(ValueError, "每个声明板块都必须由入选题覆盖"):
            self.validate(count=3, eligible=["a", "b", "c"],
                focus=["a", "b", "c"], selected=["a", "b", "a"])

    @staticmethod
    def request_from(messages):
        return next(
            value for message in messages for line in str(message["content"]).splitlines()
            if line.startswith("{") for value in [json.loads(line)]
            if isinstance(value, dict) and "eligible_focus_topics" in value
        )

    def install_recommendation_client(self, case, *, repair_valid, resolved_model="deepseek-v4-flash"):
        calls = []

        def structured(messages, **options):
            calls.append(messages)
            request = self.request_from(messages)
            self.assertEqual(request["focus_topic_constraints"], {
                "min_focus_topics": 1, "max_focus_topics": 1, "max_problems_per_topic": None,
            })
            topics = request["eligible_focus_topics"]
            self.assertGreaterEqual(len(topics), 2)
            slot = request["slot_sequence"][0]["slot"].split("-", 1)[0]
            candidate = next(item for item in request["candidates"]
                if (item["difficulty"] is None or slot in item["eligible_slots"])
                and set(item["knowledge_topics"]) & set(topics))
            topic = next(topic for topic in topics if topic in candidate["knowledge_topics"])
            focus = [topic]
            if len(calls) == 1 or not repair_valid:
                focus.append(next(other for other in topics if other != topic))
            data = {"focus_topics": focus, "ranked": [{
                "problem_key": candidate["problem_key"], "topic": topic,
                "ai_reason": "fixture", "training_focus": "fixture",
            }], "risk_warning": ""}
            return JsonChatResult(json.dumps(data), "stop", {"total_tokens": 17}, resolved_model, data)

        case.client.structured = structured
        return calls

    def test_single_problem_repair_receives_correct_feedback_and_conserves_usage(self):
        case = self.service_case()
        calls = self.install_recommendation_client(case, repair_valid=True)
        result = case.service.ai_recommendations(count=1, source_mode="plan_only")
        self.assertIsNone(result["ai"]["fallback"])
        self.assertEqual(len(calls), 2)
        feedback = json.loads(calls[1][-1]["content"])["validation_feedback"]
        self.assertIn("恰好 1 个", feedback)
        self.assertIn("实际为 2 个", feedback)
        self.assertNotIn("2 至 3", feedback)
        with Database(case.service.paths.database) as db:
            run = db.query("SELECT * FROM ai_runs WHERE kind='recommendation'")[0]
        usage = json.loads(run["usage_json"])
        governance = json.loads(run["governance_json"])
        self.assertEqual(run["status"], "complete")
        self.assertEqual(usage["total_tokens"], 34)
        self.assertEqual(usage["provider_requests"], 2)
        self.assertEqual(sum(leg["usage"]["total_tokens"] for leg in governance["legs"]), 34)
        self.assertEqual(AI_RECOMMENDATION_PROMPT_VERSION, "recommendation-prompt-v3-dynamic-constraints")

    def test_local_validation_failure_preserves_actual_resolved_model(self):
        for force_refresh in (True, False):
            with self.subTest(force_refresh=force_refresh):
                case = self.service_case()
                calls = self.install_recommendation_client(case, repair_valid=False)
                if force_refresh:
                    with self.assertRaisesRegex(ValueError, "恰好 1 个"):
                        case.service.ai_recommendations(count=1, source_mode="plan_only", force_refresh=True)
                else:
                    result = case.service.ai_recommendations(count=1, source_mode="plan_only")
                    self.assertEqual(result["ai"]["fallback"]["code"], "invalid_ai_ranking")
                self.assertEqual(len(calls), 2)
                with Database(case.service.paths.database) as db:
                    run = db.query("SELECT * FROM ai_runs WHERE kind='recommendation'")[0]
                self.assertEqual(run["status"], "failed" if force_refresh else "complete")
                self.assertEqual(run["resolved_model"], "deepseek-v4-flash")
                self.assertEqual(json.loads(run["usage_json"])["total_tokens"], 34)
                self.assertEqual(len(json.loads(run["governance_json"])["legs"]), 2)

    def test_failure_without_model_evidence_does_not_invent_resolved_model(self):
        case = self.service_case()

        def structured(messages, **options):
            raise ProviderError("permission_denied", "fixture", status=403)

        case.client.structured = structured
        with self.assertRaises(ProviderError):
            case.service.ai_recommendations(count=1, source_mode="plan_only", force_refresh=True)
        with Database(case.service.paths.database) as db:
            run = db.query("SELECT * FROM ai_runs WHERE kind='recommendation'")[0]
        self.assertEqual(run["status"], "failed")
        self.assertIsNone(run["resolved_model"])

    def test_coaching_sent_prompt_separates_spec_result_and_source_behavior(self):
        case = self.service_case()
        case.service.start("CF1A")
        conversation = case.service.ai_conversation_start("CF1A")
        list(case.service.ai_chat_stream(conversation["conversation_id"],
            message="请检查这个反例", hint_level=1, delivery_mode="low_latency"))
        messages = case.client.calls[-1][1]
        anchor = next(message["content"] for message in messages if message["role"] == "system")
        self.assertIn("按题意推导的正确结果与当前源码的实际行为", anchor)
        self.assertIn("不能把其输出当作题意的正确答案", anchor)
        self.assertIn("涉及 UB 或信息不足时，不得断言唯一输出", anchor)
        self.assertIn("level=1 只提出寻找反例的问题或引导性问题", anchor)
        self.assertIn("上述分析仍须遵守当前提示披露等级", anchor)
        self.assertEqual(AI_COACHING_PROMPT_VERSION, "coaching-prefix-v2-result-provenance")
        for level in (1, 2):
            with self.subTest(level=level), self.assertRaisesRegex(ValueError, "披露等级"):
                _validate_coaching_content("```cpp\nint main() {}\n```", hint_level=level)


if __name__ == "__main__":
    unittest.main()
