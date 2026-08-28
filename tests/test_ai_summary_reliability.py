from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tools.acm_agent.provider import AIJsonResult, ProviderError
from tools.acm_agent.knowledge import get_builtin_schema
from tools.acm_agent.service import AcmService
from tools.acm_agent.storage import Database


REPO_ROOT = Path(__file__).resolve().parents[1]


def valid_summary(*, confidence: float = 0.92) -> dict[str, object]:
    return {
        "topic": "基础实现",
        "title": "输入输出边界",
        "aliases": [],
        "confidence": confidence,
        "fields": {
            "source": "CF1A",
            "model": "读取输入并构造答案。",
            "correctness": "输出满足题目定义。",
            "implementation": "按输入顺序处理。",
            "complexity": "O(1) 时间，O(1) 空间。",
            "pitfalls": "检查边界输入。",
        },
        "rationale": "来自本次已结束 attempt。",
    }


class FakeStructuredProvider:
    key_detected = True

    def __init__(self, responses: list[dict[str, object] | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def structured(self, messages, **options):
        self.calls.append({"messages": list(messages), **dict(options)})
        data = self.responses.pop(0)
        if isinstance(data, BaseException):
            raise data
        return AIJsonResult(
            json.dumps(data, ensure_ascii=False),
            "stop",
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            str(options["model"]),
            data,
        )


class SummaryReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        target = self.root / "training" / "data-structures-30d"
        target.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/plan.json", target / "plan.json")
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/README.md", target / "README.md")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service_with(self, responses: list[dict[str, object] | BaseException]):
        provider = FakeStructuredProvider(responses)
        service = AcmService(
            self.root,
            provider_client_factory=lambda: provider,
            problem_context_fetcher=lambda ref: ("题目描述与输入输出", f"https://example.test/{ref.problem_id}"),
        )
        service.setup("private", "1", skip_validate=True)
        attempt_id = int(service.start("CF1A")["attempt_id"])
        service.close(
            "CF1A", result="AC", minutes=5, hint_level=0,
            failure="none", notes="边界已检查",
        )
        target = service.knowledge_target_create(
            str(self.root / "algorithms.md"),
            preset="algorithms-v1",
            allow_create=True,
        )
        return service, provider, attempt_id, str(target["target_id"])

    def cache_entries(self) -> int:
        with Database(self.root / ".acm" / "state.db") as db:
            return int(db.ai_cache_status()["entries"])

    def test_invalid_artifact_repairs_once_then_caches_only_repaired_result(self) -> None:
        service, provider, attempt_id, target_id = self.service_with(
            [{"topic": "broken"}, valid_summary()]
        )
        result = service.knowledge_preview(attempt_id, target_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["ai"]["outcome"]["artifact_outcome"], "repaired")
        self.assertEqual(result["ai"]["outcome"]["repair_attempts"], 1)
        self.assertTrue(result["proposal"]["can_apply"])
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(all(call["max_tokens"] == 8_192 for call in provider.calls))
        self.assertEqual(self.cache_entries(), 1)
        with Database(self.root / ".acm" / "state.db") as db:
            run = db.ai_run(str(result["proposal"]["ai_run_id"]))
            self.assertEqual(run["artifact_outcome"], "repaired")
            legs = db.query(
                "SELECT purpose,validation_code FROM ai_run_legs WHERE run_id=? ORDER BY ordinal",
                (str(result["proposal"]["ai_run_id"]),),
            )
        self.assertEqual([row["purpose"] for row in legs], ["initial", "validation_repair"])
        self.assertEqual(legs[1]["validation_code"], "summary_entry_invalid")

        cached = service.knowledge_preview(attempt_id, target_id)
        self.assertEqual(cached["ai"]["outcome"]["business_outcome"], "cache")
        self.assertEqual(len(provider.calls), 2)

    def test_low_confidence_is_applyable_with_soft_warning_and_cached(self) -> None:
        service, provider, attempt_id, target_id = self.service_with(
            [valid_summary(confidence=0.5)]
        )
        result = service.knowledge_preview(attempt_id, target_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["ai"]["outcome"]["artifact_outcome"], "valid")
        self.assertEqual(result["ai"]["outcome"]["business_outcome"], "complete")
        self.assertTrue(result["ai"]["outcome"]["apply_ready"])
        self.assertTrue(result["proposal"]["can_apply"])
        self.assertEqual(result["proposal"]["warnings"], ["模型置信度低，需人工核对"])
        self.assertEqual(self.cache_entries(), 1)

        with Database(self.root / ".acm" / "state.db") as db:
            db.connection.execute(
                "UPDATE markdown_summary_proposals SET warnings_json=? WHERE id=?",
                (
                    json.dumps(
                        ["模型置信度低于 0.75，当前预览只供检查且禁止写入"],
                        ensure_ascii=False,
                    ),
                    result["proposal"]["proposal_id"],
                ),
            )
        normalized = service.knowledge_proposal(result["proposal"]["proposal_id"])
        self.assertEqual(normalized["proposal"]["warnings"], ["模型置信度低，需人工核对"])
        self.assertTrue(normalized["proposal"]["can_apply"])

        cached = service.knowledge_preview(attempt_id, target_id)
        self.assertTrue(cached["ok"])
        self.assertEqual(cached["ai"]["outcome"]["business_outcome"], "cache")
        self.assertTrue(cached["proposal"]["can_apply"])
        self.assertIn("模型置信度低，需人工核对", cached["proposal"]["warnings"])
        self.assertEqual(len(provider.calls), 1)

    def test_low_confidence_can_be_applied_directly_and_reverted(self) -> None:
        service, _, attempt_id, target_id = self.service_with(
            [valid_summary(confidence=0.5)]
        )
        preview = service.knowledge_preview(attempt_id, target_id)["proposal"]
        applied = service.knowledge_apply(
            preview["proposal_id"], expected_revision=preview["revision"]
        )["proposal"]
        self.assertEqual(applied["status"], "applied")
        self.assertIn(
            "输入输出边界",
            Path(applied["target_path"]).read_text(encoding="utf-8-sig"),
        )

        reverted = service.knowledge_revert(
            applied["proposal_id"], expected_revision=applied["revision"]
        )
        self.assertEqual(reverted["proposal"]["status"], "reverted")

    def test_low_confidence_can_be_edited_refreshed_and_applied(self) -> None:
        service, _, attempt_id, target_id = self.service_with(
            [valid_summary(confidence=0.5)]
        )
        preview = service.knowledge_preview(attempt_id, target_id)["proposal"]
        edited_markdown = preview["entry_markdown"].replace(
            "按输入顺序处理。", "按输入顺序处理，并保留人工修改。"
        )
        refreshed = service.knowledge_refresh(
            preview["proposal_id"],
            entry_markdown=edited_markdown,
            expected_revision=preview["revision"],
        )["proposal"]
        self.assertTrue(refreshed["can_apply"])
        self.assertIn("模型置信度低，需人工核对", refreshed["warnings"])
        self.assertIn("保留人工修改", refreshed["entry_markdown"])

        applied = service.knowledge_apply(
            refreshed["proposal_id"], expected_revision=refreshed["revision"]
        )["proposal"]
        self.assertEqual(applied["status"], "applied")
        self.assertIn(
            "保留人工修改",
            Path(applied["target_path"]).read_text(encoding="utf-8-sig"),
        )

    def test_prompt_calibrates_confidence_for_safe_applyability(self) -> None:
        service, provider, attempt_id, target_id = self.service_with([valid_summary()])
        result = service.knowledge_preview(attempt_id, target_id)
        self.assertTrue(result["ok"])
        system = str(provider.calls[0]["messages"][0]["content"])
        self.assertIn("剩余知识卡的可验证置信度", system)
        self.assertIn("confidence 必须设为 0.85 到 1", system)
        self.assertIn("source 会由服务端规范化为当前题号", system)
        self.assertIn("删除冲突细节，只保留共同可证事实", system)
        self.assertIn("仍有 required field 无法可靠填写", system)
        self.assertIn("所有 required fields 非空", system)
        self.assertIn("不得为了提高 confidence 虚构", system)
        schema = dict(provider.calls[0]["json_schema"])
        confidence = schema["properties"]["confidence"]
        self.assertEqual(confidence["minimum"], 0)
        self.assertEqual(confidence["maximum"], 1)
        self.assertIn("不是题目难度或内容篇幅评分", confidence["description"])
        self.assertIn("应为 0.85 到 1", confidence["description"])

    def test_confidence_threshold_is_inclusive_at_point_seven_five(self) -> None:
        service, _, attempt_id, target_id = self.service_with(
            [valid_summary(confidence=0.75)]
        )
        result = service.knowledge_preview(attempt_id, target_id)
        self.assertTrue(result["ok"])
        self.assertTrue(result["proposal"]["can_apply"])
        self.assertNotIn("模型置信度低，需人工核对", result["proposal"]["warnings"])

    def test_failed_repair_returns_unavailable_without_proposal_or_cache(self) -> None:
        service, provider, attempt_id, target_id = self.service_with(
            [{"topic": "broken"}, {"topic": "still-broken"}]
        )
        result = service.knowledge_preview(attempt_id, target_id)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["proposal"])
        self.assertEqual(result["ai"]["outcome"]["business_outcome"], "unavailable")
        self.assertEqual(result["ai"]["outcome"]["repair_attempts"], 1)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(self.cache_entries(), 0)

    def test_provider_failure_is_structured_unavailable(self) -> None:
        service, _, attempt_id, target_id = self.service_with(
            [ProviderError("provider_timeout", "provider temporarily unavailable")]
        )
        result = service.knowledge_preview(attempt_id, target_id)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["proposal"])
        self.assertEqual(result["ai"]["outcome"]["provider_outcome"], "failed")
        self.assertEqual(result["ai"]["outcome"]["business_outcome"], "unavailable")
        self.assertEqual(self.cache_entries(), 0)

    def test_ai_inferred_schema_uses_strict_key_value_field_wire_shape(self) -> None:
        data = valid_summary()
        data["schema"] = get_builtin_schema("algorithms-v1")
        data["fields"] = [
            {"key": key, "value": value}
            for key, value in dict(data["fields"]).items()
        ]
        service, _, attempt_id, target_id = self.service_with([data])
        result = service.knowledge_preview(
            attempt_id, target_id, schema_mode="ai"
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["proposal"]["can_apply"])

    def test_failed_force_refresh_preserves_previous_valid_entry(self) -> None:
        service, provider, attempt_id, target_id = self.service_with(
            [
                valid_summary(),
                ProviderError("provider_timeout", "provider temporarily unavailable"),
            ]
        )
        first = service.knowledge_preview(attempt_id, target_id)
        self.assertTrue(first["ok"])
        refreshed = service.knowledge_preview(
            attempt_id, target_id, force_refresh=True
        )
        self.assertFalse(refreshed["ok"])
        self.assertEqual(
            refreshed["ai"]["outcome"]["business_outcome"], "unavailable"
        )
        cached = service.knowledge_preview(attempt_id, target_id)
        self.assertTrue(cached["ok"])
        self.assertEqual(cached["ai"]["outcome"]["business_outcome"], "cache")
        self.assertEqual(len(provider.calls), 2)


if __name__ == "__main__":
    unittest.main()
