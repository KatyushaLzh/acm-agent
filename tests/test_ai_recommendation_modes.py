from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.acm_agent.deepseek import JsonChatResult
from tools.acm_agent.service import AcmService
from tools.acm_agent.service_ai import ServiceAIMixin, _topic_third
from tools.acm_agent.storage import Database
from tools.acm_agent.topic_taxonomy import TAXONOMY_VERSION


class _TopicAwareDeepSeek:
    key_detected = True

    def __init__(self) -> None:
        self.request: dict | None = None
        self.invalid = False

    def chat_json(self, messages, **kwargs):
        request = json.loads(messages[-1]["content"].splitlines()[-1])
        self.request = request
        if self.invalid:
            data = {
                "focus_topics": request["eligible_focus_topics"][:1],
                "ranked": [{"problem_key": "codeforces:999999Z", "topic": "forged"}],
                "risk_warning": "",
            }
            return JsonChatResult(
                json.dumps(data), "stop", {"total_tokens": 5}, kwargs["model"], data
            )
        count = request["requested_count"]
        focus = request["eligible_focus_topics"][: min(3, count)]
        ranked = []
        seen: set[str] = set()
        for topic in focus:
            for candidate in request["candidates"]:
                if candidate["problem_key"] in seen or topic not in candidate["knowledge_topics"]:
                    continue
                seen.add(candidate["problem_key"])
                ranked.append(
                    {
                        "problem_key": candidate["problem_key"],
                        "topic": topic,
                        "ai_reason": "覆盖统计",
                        "training_focus": topic,
                    }
                )
                break
        for candidate in request["candidates"]:
            if len(ranked) >= count:
                break
            if candidate["problem_key"] in seen:
                continue
            topic = next(
                (value for value in candidate["knowledge_topics"] if value in focus),
                None,
            )
            if topic is None:
                continue
            seen.add(candidate["problem_key"])
            ranked.append(
                {
                    "problem_key": candidate["problem_key"],
                    "topic": topic,
                    "ai_reason": "覆盖统计",
                    "training_focus": topic,
                }
            )
        data = {"focus_topics": focus, "ranked": ranked, "risk_warning": ""}
        return JsonChatResult(
            json.dumps(data), "stop", {"total_tokens": 5}, kwargs["model"], data
        )


class AiRecommendationModeTests(unittest.TestCase):
    @staticmethod
    def _focus_keys(result: dict) -> set[str]:
        return {
            str(topic.get("key") if isinstance(topic, dict) else topic)
            for topic in result["ai"]["focus_topics"]
        }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.client = _TopicAwareDeepSeek()
        self.service = AcmService(
            self.root,
            deepseek_client_factory=lambda: self.client,
        )
        self.service.setup("secret-handle", "4242", target_rating=1800, skip_validate=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_submission_profile_uses_unique_platform_ac_and_is_sanitized(self) -> None:
        with Database(self.service.paths.database) as db:
            db.upsert_problem(
                {"platform": "codeforces", "problem_id": "1A", "rating": 800, "tags": ["dp"]}
            )
            db.upsert_problem(
                {"platform": "luogu", "problem_id": "P1001", "difficulty": 1, "tags": ["贪心"]}
            )
            for submission in (
                {"platform": "codeforces", "submission_id": "private-id-1", "problem_id": "1A", "verdict": "OK", "submitted_at": "2026-01-02", "language": "secret-language", "raw": {"token": "secret-token"}},
                {"platform": "codeforces", "submission_id": "private-id-2", "problem_id": "1A", "verdict": "OK", "submitted_at": "2026-01-03"},
                {"platform": "codeforces", "submission_id": "private-id-3", "problem_id": "1A", "verdict": "WRONG_ANSWER", "submitted_at": "2026-01-01"},
                {"platform": "luogu", "submission_id": "accepted:P1001", "problem_id": "P1001", "verdict": "AC", "submitted_at": "2026-01-04"},
            ):
                db.upsert_submission(submission)
            profile = ServiceAIMixin._submission_topic_profile(db)

        self.assertEqual(profile["accepted_problem_count"], 2)
        self.assertEqual(profile["topic_counts"]["dynamic_programming"], 1)
        self.assertEqual(profile["topic_counts"]["greedy"], 1)
        self.assertEqual(profile["coverage"]["codeforces"], {"total": 1, "resolved": 1, "unresolved": 0})
        encoded = json.dumps(profile, ensure_ascii=False)
        for secret in ("private-id", "secret-language", "secret-token", "secret-handle", "4242"):
            self.assertNotIn(secret, encoded)

    def test_top_and_bottom_thirds_keep_boundary_ties(self) -> None:
        counts = {"a": 0, "b": 1, "c": 1, "d": 2, "e": 3, "f": 3}
        self.assertEqual(set(_topic_third(counts, ai_mode="gap_fill")), {"a", "b", "c"})
        self.assertEqual(set(_topic_third(counts, ai_mode="specialization")), {"e", "f"})

    def test_topic_rotation_happens_before_the_sixty_candidate_cap(self) -> None:
        with Database(self.service.paths.database) as db:
            for index in range(60):
                db.upsert_problem(
                    {
                        "platform": "codeforces",
                        "problem_id": f"{1000 + index}A",
                        "rating": 1800,
                        "tags": ["dp"],
                    }
                )
            for index in range(3):
                db.upsert_problem(
                    {
                        "platform": "codeforces",
                        "problem_id": f"{2000 + index}A",
                        "rating": 800,
                        "tags": ["geometry"],
                    }
                )

        with mock.patch.object(
            self.service, "_record_recommendation_output"
        ), mock.patch(
            "tools.acm_agent.platforms.enrich_luogu_accepted_problem_tags",
            return_value={"attempted": 0, "resolved": 0, "failed": 0, "remaining": 0},
        ) as enrich_tags:
            result = self.service.ai_recommendations(count=2)

        enrich_tags.assert_not_called()
        self.assertIsNone(result["ai"]["fallback"])
        self.assertLessEqual(len(self.client.request["candidates"]), 60)
        candidate_topics = {
            topic
            for candidate in self.client.request["candidates"]
            for topic in candidate["knowledge_topics"]
        }
        self.assertEqual(candidate_topics, {"dynamic_programming", "geometry"})

    def test_fallback_warns_when_topic_cap_must_be_relaxed(self) -> None:
        candidates = []
        for index in range(9):
            tags = ["dp", "greedy"] if index < 3 else ["dp"]
            candidates.append(
                {
                    "slot": "main",
                    "problem_key": f"codeforces:{3000 + index}A",
                    "problem_id": f"CF{3000 + index}A",
                    "platform": "codeforces",
                    "title": "",
                    "url": "",
                    "equivalent_rating": 1800,
                    "score": 100.0,
                    "breakdown": {"plan_urgency": 0.0, "difficulty_fit": 100.0},
                    "reasons": [],
                    "tags": tags,
                    "plan_sources": [],
                }
            )

        self.client.invalid = True
        with mock.patch.object(
            self.service,
            "recommendations",
            return_value={"ok": True, "recommendations": candidates},
        ), mock.patch.object(
            self.service, "_record_recommendation_output"
        ), mock.patch(
            "tools.acm_agent.platforms.enrich_luogu_accepted_problem_tags",
            return_value={"attempted": 0, "resolved": 0, "failed": 0, "remaining": 0},
        ):
            result = self.service.ai_recommendations(count=9)

        keys = [row["knowledge_topic_key"] for row in result["recommendations"]]
        self.assertGreater(keys.count("dynamic_programming"), 5)
        self.assertIn("已放宽", result["ai"]["risk_warning"])

    def test_modes_restrict_candidates_and_send_only_sanitized_ac_summary(self) -> None:
        topic_tags = [
            ("dynamic_programming", "dp", 0),
            ("greedy", "greedy", 1),
            ("search", "brute force", 2),
            ("string_algorithms", "strings", 3),
            ("number_theory", "number theory", 4),
            ("geometry", "geometry", 5),
        ]
        candidates = []
        with Database(self.service.paths.database) as db:
            serial = 100
            for _topic, tag, ac_count in topic_tags:
                for index in range(3):
                    problem_id = f"{serial + index}A"
                    db.upsert_problem(
                        {"platform": "codeforces", "problem_id": problem_id, "rating": 1400, "tags": [tag]}
                    )
                    candidates.append(
                        {
                            "slot": "main",
                            "problem_key": f"codeforces:{problem_id}",
                            "problem_id": f"CF{problem_id}",
                            "platform": "codeforces",
                            "title": problem_id,
                            "url": "",
                            "equivalent_rating": 1400,
                            "score": 100.0,
                            "breakdown": {"plan_urgency": 0.0, "difficulty_fit": 100.0},
                            "reasons": [],
                            "tags": [tag],
                            "plan_sources": [],
                        }
                    )
                for accepted_index in range(ac_count):
                    accepted_id = f"{serial + 20 + accepted_index}B"
                    db.upsert_problem(
                        {"platform": "codeforces", "problem_id": accepted_id, "rating": 1200, "tags": [tag]}
                    )
                    db.upsert_submission(
                        {
                            "platform": "codeforces",
                            "submission_id": f"submission-secret-{serial}-{accepted_index}",
                            "problem_id": accepted_id,
                            "verdict": "OK",
                            "submitted_at": f"2026-01-{accepted_index + 1:02d}",
                            "language": "private-language",
                            "raw": {"account": "secret-handle"},
                        }
                    )
                serial += 100

        def deterministic_result(**_kwargs):
            return {"ok": True, "recommendations": [dict(item) for item in candidates]}

        with mock.patch.object(self.service, "recommendations", side_effect=deterministic_result), mock.patch.object(
            self.service, "_record_recommendation_output"
        ), mock.patch(
            "tools.acm_agent.platforms.enrich_luogu_accepted_problem_tags",
            return_value={"attempted": 0, "resolved": 0, "failed": 0, "remaining": 0},
        ) as enrich_tags:
            gap = self.service.ai_recommendations(count=2)
            self.assertEqual(gap["ai"]["mode"], "gap_fill")
            self.assertEqual(gap["ai"]["taxonomy_version"], TAXONOMY_VERSION)
            self.assertTrue(self._focus_keys(gap) <= {"dynamic_programming", "greedy"})
            self.assertEqual(
                {row["knowledge_topic_key"] for row in gap["recommendations"]},
                {"dynamic_programming", "greedy"},
            )
            self.assertEqual(
                {row["knowledge_topic"] for row in gap["recommendations"]},
                {"动态规划", "贪心"},
            )
            self.assertEqual(
                [row["difficulty_band"] for row in gap["recommendations"]],
                ["current_plus_100", "recent_solved_average"],
            )
            self.assertEqual(
                set(self.client.request["candidates"][0]["slot_scores"]),
                {"recovery", "main", "stretch"},
            )

            strong = self.service.ai_recommendations(count=2, ai_mode="specialization")
            self.assertTrue(self._focus_keys(strong) <= {"number_theory", "geometry"})
            self.assertEqual(
                {row["knowledge_topic_key"] for row in strong["recommendations"]},
                {"number_theory", "geometry"},
                strong,
            )

            self.client.invalid = True
            fallback = self.service.ai_recommendations(count=2, ai_mode="gap_fill")
            self.assertEqual(fallback["ai"]["fallback"]["code"], "invalid_ai_ranking")
            self.assertEqual(
                {row["knowledge_topic_key"] for row in fallback["recommendations"]},
                {"dynamic_programming", "greedy"},
            )
            self.assertTrue(
                all(row["ranking_basis"] == "deterministic_fallback" for row in fallback["recommendations"])
            )
            enrich_tags.assert_not_called()

        outbound = json.dumps(self.client.request, ensure_ascii=False)
        for forbidden in (
            "submission-secret",
            "private-language",
            "secret-handle",
            "4242",
            "raw_json",
            "submission_id",
            "notes",
        ):
            self.assertNotIn(forbidden, outbound)
        with Database(self.service.paths.database) as db:
            audit = dict(
                db.query(
                    "SELECT request_summary_json FROM ai_runs "
                    "WHERE kind='recommendation' ORDER BY created_at DESC LIMIT 1"
                )[0]
            )
        audit_summary = str(audit["request_summary_json"])
        self.assertNotIn("problem_key", audit_summary)
        self.assertNotIn("accepted_problem_summary", audit_summary)

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "ai_mode"):
            self.service.ai_recommendations(ai_mode="unknown")


if __name__ == "__main__":
    unittest.main()
