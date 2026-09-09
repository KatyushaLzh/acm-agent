from __future__ import annotations

from dataclasses import replace
import json
import unittest
from unittest import mock

from tests import test_ai_plan_import as plans
from tests import test_ai_recommendation_modes as recommendations
from tests import test_stage3_governance as governance
from tests import test_ai_service as coaching
from tools.acm_agent.provider import AIStreamEvent, ProviderError
from tools.acm_agent.provider_governance import GovernedProviderClient
from tools.acm_agent.provider_registry import ProviderRegistry
from tools.acm_agent.storage import Database


class AIReviewRegressionTests(unittest.TestCase):
    def coaching_fixture(self):
        fixture = coaching.AiServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.service.start("CF1A")
        return fixture

    def test_coaching_semantic_repair_aggregates_usage(self):
        fixture = self.coaching_fixture()
        with mock.patch("tools.acm_agent.service_ai._validate_coaching_content",
                        side_effect=[ValueError("invalid fixture"), "correct"]):
            result = fixture.service.ai_chat("CF1A", message="hint", mode="hint", hint_level=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["usage"]["total_tokens"], 18)
        self.assertEqual(result["usage"]["provider_requests"], 2)
        with Database(fixture.service.paths.database) as db:
            run = db.ai_run(result["ai_run_id"])
        self.assertEqual(json.loads(run["usage_json"]), result["usage"])

    def test_coaching_fatal_error_releases_conversation_claim(self):
        fixture = self.coaching_fixture()
        conversation = fixture.service.ai_conversation_start("CF1A")["conversation_id"]
        with mock.patch.object(fixture.client, "chat", side_effect=ProviderError("budget_exceeded", "fixture")):
            with self.assertRaises(ProviderError):
                fixture.service.ai_chat("CF1A", message="first", conversation_id=conversation)
        result = fixture.service.ai_chat("CF1A", message="second", conversation_id=conversation)
        self.assertTrue(result["ok"])

    def plan_case(self, second):
        fixture = plans.AIPlanImportServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        bad = {"title": "bad", "groups": [{"topic": "x", "due_date": None,
            "problem_keys": ["codeforces:CF1A", "codeforces:CF1A"]}]}
        fake = plans._PlanDeepSeek([bad, second])
        service = fixture.service(fake)
        result = service.ai_plan_preview(mode="organize", text="CF1A P3374")
        with Database(service.paths.database) as db:
            row = dict(db.query("SELECT usage_json,governance_json FROM ai_runs")[0])
        return fake, result, json.loads(row["usage_json"]), json.loads(row["governance_json"])

    def test_organize_repair_success_counts_both_calls(self):
        good = {"title": "good", "groups": [{"topic": "x", "due_date": None,
            "problem_keys": ["codeforces:CF1A", "luogu:P3374"]}]}
        fake, result, usage, ledger = self.plan_case(good)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(usage["total_tokens"], 14)
        self.assertEqual(usage["provider_requests"], 2)
        self.assertEqual(result["ai"]["usage"], usage)
        self.assertEqual(sum(leg["usage"]["total_tokens"] for leg in ledger["legs"]), 14)

    def test_organize_repair_failure_preserves_error_leg(self):
        fake, result, usage, ledger = self.plan_case(ProviderError(
            "network_error", "fixture failure", retryable=False, usage={"total_tokens": 11}))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(usage["total_tokens"], 18)
        self.assertEqual(result["ai"]["usage"], usage)
        self.assertEqual(ledger["provider_requests"], 2)
        self.assertEqual([leg["status"] for leg in ledger["legs"]], ["complete", "failed"])

    def test_stream_checks_total_before_publishing_done(self):
        for total, accepted in [(5, True), (7, False)]:
            with self.subTest(total=total):
                route = ProviderRegistry(governance._ai_config()).route("coaching")
                route = replace(route, budget={**route.budget, "max_total_tokens": 5})
                client = governance._StreamingScriptedClient([[
                    AIStreamEvent("delta", content="ok"),
                    AIStreamEvent("done", finish_reason="stop", model=route.model,
                        usage={"input_tokens": total - 1, "output_tokens": 1, "total_tokens": total}),
                ]])
                governor = GovernedProviderClient([route], lambda _route, _timeout: client)
                events = []
                try:
                    for event in governor.stream_chat([]):
                        events.append(event.kind)
                        if event.kind == "done":
                            self.assertEqual(governor.request_attempts, 1)
                except ProviderError as exc:
                    self.assertFalse(accepted)
                    self.assertEqual(exc.code, "budget_exceeded")
                    self.assertEqual(exc.usage["total_tokens"], total)
                self.assertEqual(events, ["delta", "done"] if accepted else ["delta"])

    def test_recommendation_history_payload_is_bounded_but_counts_are_complete(self):
        fixture = recommendations.AiRecommendationModeTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with Database(fixture.service.paths.database) as db:
            for index in range(6):
                db.upsert_problem({"platform": "codeforces", "problem_id": f"{1000+index}A",
                    "rating": 1800, "tags": ["dp"]})
        profile = {
            "accepted_problem_count": 700,
            "accepted_summaries": [{"problem_key": f"codeforces:{9000+i}A", "accepted_date": "2026-08-01",
                "platform": "codeforces", "difficulty": 1600, "knowledge_topics": ["dynamic_programming"]}
                for i in range(700)],
            "topic_counts": {"dynamic_programming": 700},
            "coverage": {}, "unclassified_tags": [],
        }
        with mock.patch.object(fixture.service, "_submission_topic_profile", return_value=profile):
            fixture.service.ai_recommendations(count=1, force_refresh=True)
        sent = fixture.client.request
        self.assertEqual(len(sent["accepted_problem_summary"]), 12)
        self.assertEqual(sent["accepted_topic_counts"]["dynamic_programming"], 700)
        self.assertEqual(sent["accepted_problem_count"], 700)
        self.assertNotIn("breakdown", json.dumps(sent["candidates"]))
        self.assertLess(len(json.dumps(sent)), 15000)


if __name__ == "__main__":
    unittest.main()
