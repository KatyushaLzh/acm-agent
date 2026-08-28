from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.ai_plan_import import (
    AIPlanImportError,
    build_generation_candidates,
    deterministic_generated_ir,
    extract_problem_refs,
    filter_generated_problem_ids,
    fit_stages_to_task_count,
    lower_plan,
    make_plan_id,
    validate_generation_intent,
    validate_generation_selection,
    validate_generated_problem_ids,
    validate_organize_ir,
)
from tools.acm_agent.deepseek import DeepSeekError, JsonChatResult
from tools.acm_agent.service import AcmService
from tools.acm_agent.storage import Database
from tools.acm_agent.web import AcmRequestHandler


class _PlanDeepSeek:
    key_detected = True

    def __init__(self, responses: list[dict[str, object] | Exception]):
        self.responses = list(responses)
        self.calls: list[tuple[object, dict[str, object]]] = []

    def chat_json(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return JsonChatResult(
            json.dumps(response, ensure_ascii=False),
            "stop",
            {"total_tokens": 7},
            kwargs["model"],
            response,
        )

    def structured(self, messages, **kwargs):
        return self.chat_json(messages, **kwargs)


class AIPlanImportLogicTests(unittest.TestCase):
    def test_extract_mixed_refs_preserves_order_and_deduplicates(self) -> None:
        result = extract_problem_refs(
            "先 CF1A，再 https://www.luogu.com.cn/problem/P3374，最后 cf1a。"
        )
        self.assertEqual(
            [item["problem_key"] for item in result["problems"]],
            ["codeforces:CF1A", "luogu:P3374"],
        )
        self.assertEqual(result["duplicates"], ["codeforces:CF1A"])
        self.assertEqual(result["invalid_links"], [])

    def test_extract_reports_invalid_official_links_mixed_with_valid_refs(self) -> None:
        result = extract_problem_refs(
            "CF1A https://www.luogu.com.cn/problem/not-P3374"
        )
        self.assertEqual(
            [item["problem_key"] for item in result["problems"]],
            ["codeforces:CF1A"],
        )
        self.assertEqual(
            result["invalid_links"],
            ["https://www.luogu.com.cn/problem/not-P3374"],
        )

    def test_extract_rejects_problem_urls_with_junk_suffixes(self) -> None:
        for link in (
            "https://codeforces.com/problemset/problem/1/Afoo",
            "https://codeforces.com/problemset/problem/1/A-extra",
            "https://www.luogu.com.cn/problem/P3374foo",
        ):
            with self.subTest(link=link):
                result = extract_problem_refs(f"CF2A {link}")
                self.assertEqual(
                    [item["problem_key"] for item in result["problems"]],
                    ["codeforces:CF2A"],
                )
                self.assertEqual(result["invalid_links"], [link])

    def test_extract_ignores_non_problem_official_pages(self) -> None:
        result = extract_problem_refs(
            "CF1A https://codeforces.com/blog/entry/123 https://www.luogu.com.cn/user/1"
        )
        self.assertEqual(result["invalid_links"], [])

    def test_extract_accepts_valid_urls_followed_by_ascii_punctuation(self) -> None:
        for punctuation in (",", ".", ";", ":", "!"):
            with self.subTest(punctuation=punctuation):
                result = extract_problem_refs(
                    f"https://codeforces.com/problemset/problem/1/A{punctuation} P3374"
                )
                self.assertEqual(
                    [item["problem_key"] for item in result["problems"]],
                    ["codeforces:CF1A", "luogu:P3374"],
                )
                self.assertEqual(result["invalid_links"], [])

    def test_extract_zero_and_over_limit_are_rejected(self) -> None:
        with self.assertRaisesRegex(AIPlanImportError, "没有识别"):
            extract_problem_refs("只是一段描述")
        with self.assertRaisesRegex(AIPlanImportError, "没有识别"):
            extract_problem_refs("https://codeforces.com/problemset/problem/not-a-number/A")
        exact_limit = extract_problem_refs(" ".join(f"P{1000 + index}" for index in range(200)))
        self.assertEqual(len(exact_limit["problems"]), 200)
        with self.assertRaisesRegex(AIPlanImportError, "最多整理 200"):
            extract_problem_refs(" ".join(f"P{1000 + index}" for index in range(201)))

    def test_organize_ir_is_a_strict_permutation(self) -> None:
        keys = ["codeforces:CF1A", "luogu:P3374"]
        valid = {
            "title": "入门",
            "groups": [
                {
                    "topic": "第一阶段",
                    "due_date": None,
                    "problem_keys": keys,
                }
            ],
        }
        self.assertEqual(validate_organize_ir(valid, allowed_problem_keys=keys)["title"], "入门")
        invalid = json.loads(json.dumps(valid))
        invalid["groups"][0]["problem_keys"][1] = keys[0]
        with self.assertRaisesRegex(AIPlanImportError, "重复"):
            validate_organize_ir(invalid, allowed_problem_keys=keys)
        invalid = json.loads(json.dumps(valid))
        invalid["unexpected"] = True
        with self.assertRaisesRegex(AIPlanImportError, "不允许字段"):
            validate_organize_ir(invalid, allowed_problem_keys=keys)

    def test_dates_and_deterministic_lowering(self) -> None:
        intent = validate_generation_intent(
            {
                "title": "树结构",
                "description": "",
                "platforms": ["codeforces", "luogu"],
                "topics": [],
                "difficulty_min": None,
                "difficulty_max": None,
                "stages": [
                    {"topic": "基础", "due_date": "2026-08-26"},
                    {"topic": "进阶", "due_date": "2026-08-27"},
                ],
            }
        )
        selected = validate_generation_selection(
            {
                "stages": [
                    {
                        "topic": "基础",
                        "due_date": "2026-08-26",
                        "problem_keys": ["codeforces:CF1A"],
                    },
                    {
                        "topic": "进阶",
                        "due_date": "2026-08-27",
                        "problem_keys": ["luogu:P3374"],
                    },
                ]
            },
            candidates=[
                {"problem_key": "codeforces:CF1A"},
                {"problem_key": "luogu:P3374"},
            ],
            expected_count=2,
        )
        plan = lower_plan(
            mode="generate",
            text="树结构",
            controls={"task_count": 2, "include_completed": False},
            ir={"title": intent["title"], "description": "", "stages": selected["stages"]},
            catalog={"codeforces:CF1A": {"name": "Theatre Square"}},
        )
        self.assertEqual(plan["schedule_mode"], "dated")
        self.assertEqual(plan["stages"][0]["stage_key"], "stage-01")
        self.assertEqual(plan["stages"][0]["tasks"][0]["task_key"], "stage-01-task-001")
        self.assertEqual(plan["stages"][0]["tasks"][0]["url"], "https://codeforces.com/problemset/problem/1/A")
        self.assertEqual(
            plan["plan_id"],
            make_plan_id("generate", "树结构", {"task_count": 2, "include_completed": False}),
        )
        self.assertEqual(plan["stages"][1]["tasks"][0]["url"], "https://www.luogu.com.cn/problem/P3374")
        self.assertEqual(plan["stages"][0]["tasks"][0]["tags"], [])

    def test_dates_reject_mixed_or_decreasing_values(self) -> None:
        base = {
            "title": "排期",
            "description": "",
            "platforms": ["codeforces"],
            "topics": [],
            "difficulty_min": None,
            "difficulty_max": None,
        }
        with self.assertRaisesRegex(AIPlanImportError, "混合填写"):
            validate_generation_intent({
                **base,
                "stages": [
                    {"topic": "一", "due_date": "2026-08-27"},
                    {"topic": "二", "due_date": None},
                ],
            })
        with self.assertRaisesRegex(AIPlanImportError, "非递减"):
            validate_generation_intent({
                **base,
                "stages": [
                    {"topic": "一", "due_date": "2026-08-28"},
                    {"topic": "二", "due_date": "2026-08-27"},
                ],
            })

    def test_generation_selection_rejects_unknown_duplicate_and_wrong_count(self) -> None:
        candidates = [
            {"problem_key": "codeforces:CF1A"},
            {"problem_key": "luogu:P3374"},
        ]
        def selection(keys):
            return {"stages": [{"topic": "综合", "due_date": None, "problem_keys": keys}]}
        with self.assertRaisesRegex(AIPlanImportError, "越权"):
            validate_generation_selection(
                selection(["codeforces:CF999A"]), candidates=candidates, expected_count=1
            )
        with self.assertRaisesRegex(AIPlanImportError, "重复"):
            validate_generation_selection(
                selection(["codeforces:CF1A", "codeforces:CF1A"]),
                candidates=candidates,
                expected_count=2,
            )
        with self.assertRaisesRegex(AIPlanImportError, "数量不符"):
            validate_generation_selection(
                selection(["codeforces:CF1A"]), candidates=candidates, expected_count=2
            )

    def test_generated_problem_ids_are_the_only_allowed_model_field(self) -> None:
        self.assertEqual(
            validate_generated_problem_ids({"problem_ids": ["cf1a", "P3374"]}),
            ["CF1A", "P3374"],
        )
        with self.assertRaisesRegex(AIPlanImportError, "不允许字段"):
            validate_generated_problem_ids(
                {"problem_ids": ["CF1A"], "explanation": "because"}
            )
        self.assertEqual(
            validate_generated_problem_ids({"problem_ids": ["codeforces:CF1A"]}),
            ["CODEFORCES:CF1A"],
        )
        with self.assertRaisesRegex(AIPlanImportError, "必须是字符串"):
            validate_generated_problem_ids({"problem_ids": [123]})

    def test_generated_problem_ids_apply_all_local_eligibility_gates(self) -> None:
        catalog = {
            "codeforces:CF1A": {
                "problem_id": "CF1A", "status": "available", "name": "a",
            },
            "codeforces:CF2A": {
                "problem_id": "CF2A", "status": "active", "name": "b",
            },
            "luogu:P1001": {
                "problem_id": "P1001", "status": "accepted", "name": "c",
            },
            "luogu:P1002": {
                "problem_id": "P1002", "status": "skipped", "name": "d",
            },
        }
        accepted, rejected = filter_generated_problem_ids(
            ["CF1A", "CF2A", "P1001", "P1002", "CF999A", "CF1A", "BAD"],
            catalog=catalog,
            already_selected=[],
            include_completed=False,
            remaining_count=10,
        )
        self.assertEqual(accepted, ["codeforces:CF1A"])
        self.assertEqual(
            {item["problem_id"]: item["reason"] for item in rejected},
            {
                "CF2A": "active",
                "P1001": "accepted",
                "P1002": "skipped",
                "CF999A": "not_in_local_catalog",
                "CF1A": "duplicate",
                "BAD": "invalid_problem_id",
            },
        )
        included, _ = filter_generated_problem_ids(
            ["P1001", "P1002", "CF2A"],
            catalog=catalog,
            already_selected=[],
            include_completed=True,
            remaining_count=10,
        )
        self.assertEqual(included, ["luogu:P1001", "luogu:P1002"])

    def test_generated_ir_is_single_stage_and_preserves_target_in_memory(self) -> None:
        ir = deterministic_generated_ir(
            ["codeforces:CF1A", "luogu:P3374"], target_text="线段树专项"
        )
        self.assertEqual(ir["title"], "AI 目标题单")
        self.assertEqual(ir["description"], "线段树专项")
        self.assertEqual(len(ir["stages"]), 1)
        self.assertEqual(
            [item["problem_key"] for item in ir["stages"][0]["problems"]],
            ["codeforces:CF1A", "luogu:P3374"],
        )

    def test_excess_stages_are_merged_to_fit_available_tasks(self) -> None:
        stages = [
            {"topic": "基础", "due_date": "2026-08-26"},
            {"topic": "进阶", "due_date": "2026-08-27"},
            {"topic": "综合", "due_date": "2026-08-28"},
        ]
        fitted = fit_stages_to_task_count(stages, 2)
        self.assertEqual(len(fitted), 2)
        self.assertEqual(fitted[1], {"topic": "进阶 / 综合", "due_date": "2026-08-28"})
        self.assertEqual(len(stages), 3)

    def test_candidate_gate_excludes_completion_and_active(self) -> None:
        rows = [
            {
                "platform": "codeforces", "problem_id": "1A", "name": "a",
                "rating": 800, "difficulty": None, "tags_json": "[]",
            },
            {
                "platform": "codeforces", "problem_id": "2A", "name": "b",
                "rating": 900, "difficulty": None, "tags_json": "[]",
            },
            {
                "platform": "luogu", "problem_id": "P1001", "name": "c",
                "rating": None, "difficulty": 1, "tags_json": "[]",
            },
        ]
        intent = {
            "platforms": ["codeforces", "luogu"],
            "topics": [],
            "difficulty_min": None,
            "difficulty_max": None,
        }
        statuses = {
            "codeforces:CF1A": "accepted",
            "codeforces:CF2A": "active",
            "luogu:P1001": "skipped",
        }
        self.assertEqual(
            build_generation_candidates(rows, intent=intent, statuses=statuses, include_completed=False),
            [],
        )
        included = build_generation_candidates(
            rows, intent=intent, statuses=statuses, include_completed=True
        )
        self.assertEqual(
            {item["problem_key"] for item in included},
            {"codeforces:CF1A", "luogu:P1001"},
        )

    def test_candidate_pool_is_capped_at_120(self) -> None:
        rows = [
            {
                "platform": "luogu",
                "problem_id": f"P{1000 + index}",
                "name": str(index),
                "rating": None,
                "difficulty": 2,
                "tags_json": "[]",
            }
            for index in range(150)
        ]
        candidates = build_generation_candidates(
            rows,
            intent={
                "platforms": ["luogu"],
                "topics": [],
                "difficulty_min": None,
                "difficulty_max": None,
            },
            statuses={},
            include_completed=False,
        )
        self.assertEqual(len(candidates), 120)


class AIPlanImportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self, client: _PlanDeepSeek) -> AcmService:
        service = AcmService(self.root, deepseek_client_factory=lambda: client)
        service.setup("fixture", "42", target_rating=1600, skip_validate=True)
        with Database(service.paths.database) as db:
            db.upsert_problems(
                [
                    {
                        "platform": "codeforces", "problem_id": "1A",
                        "name": "Theatre Square", "rating": 800,
                        "tags": ["implementation"],
                    },
                    {"platform": "luogu", "problem_id": "P3374", "name": "树状数组 1", "difficulty": 3, "tags": ["树状数组"]},
                ]
            )
        return service

    def test_organize_preview_is_non_mutating_and_audited_without_raw_text(self) -> None:
        client = _PlanDeepSeek(
            [
                {
                    "title": "混合训练",
                    "groups": [
                        {
                            "topic": "基础",
                            "due_date": None,
                            "problem_keys": ["codeforces:CF1A", "luogu:P3374"],
                        }
                    ],
                }
            ]
        )
        service = self.service(client)
        secret_text = "私人描述 CF1A P3374"
        result = service.ai_plan_preview(mode="organize", text=secret_text)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["title"], "混合训练")
        self.assertIsNone(result["ai"]["fallback"])
        with Database(service.paths.database) as db:
            self.assertEqual(db.query("SELECT COUNT(*) FROM plans")[0][0], 0)
            row = db.query("SELECT kind,status,request_summary_json FROM ai_runs")[0]
            self.assertEqual((row["kind"], row["status"]), ("plan_import", "complete"))
            self.assertNotIn(secret_text, row["request_summary_json"])
        self.assertEqual(list((self.root / ".acm" / "plans").glob("*.json")), [])

    def test_organize_exact_cache_hit_and_force_refresh(self) -> None:
        response = {
            "title": "混合训练",
            "groups": [{
                "topic": "基础",
                "due_date": None,
                "problem_keys": ["codeforces:CF1A", "luogu:P3374"],
            }],
        }
        client = _PlanDeepSeek([response, response])
        service = self.service(client)

        cold = service.ai_plan_preview(mode="organize", text="CF1A P3374")
        hit = service.ai_plan_preview(mode="organize", text="CF1A P3374")
        refreshed = service.ai_plan_preview(
            mode="organize", text="CF1A P3374", force_refresh=True
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(cold["ai"]["local_cache"]["status"], "miss")
        self.assertEqual(hit["ai"]["local_cache"]["status"], "hit")
        self.assertEqual(hit["ai"]["usage"], {})
        self.assertEqual(refreshed["ai"]["local_cache"]["status"], "refresh")

    def test_organize_failed_force_refresh_preserves_old_entry_and_returns_failure(self) -> None:
        response = {
            "title": "旧缓存",
            "groups": [{
                "topic": "基础",
                "due_date": None,
                "problem_keys": ["codeforces:CF1A"],
            }],
        }
        client = _PlanDeepSeek([response, DeepSeekError("network_error", "offline")])
        service = self.service(client)
        service.ai_plan_preview(mode="organize", text="CF1A")

        with self.assertRaisesRegex(DeepSeekError, "offline"):
            service.ai_plan_preview(mode="organize", text="CF1A", force_refresh=True)
        recovered = service.ai_plan_preview(mode="organize", text="CF1A")

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(recovered["plan"]["title"], "旧缓存")
        self.assertEqual(recovered["ai"]["local_cache"]["status"], "hit")

    def test_organize_invalid_ir_uses_single_stage_fallback(self) -> None:
        bad = {
            "title": "bad",
            "groups": [{
                "topic": "x",
                "due_date": None,
                "problem_keys": ["codeforces:CF1A", "codeforces:CF1A"],
            }],
        }
        client = _PlanDeepSeek([bad, bad])
        service = self.service(client)
        result = service.ai_plan_preview(mode="organize", text="CF1A P3374")
        self.assertEqual(result["plan"]["stages"][0]["topic"], "全部题目")
        self.assertEqual(result["ai"]["fallback"]["code"], "invalid_ai_plan_ir")
        self.assertEqual(result["ai"]["outcome"]["repair_attempts"], 1)
        with Database(service.paths.database) as db:
            self.assertEqual(db.query("SELECT status FROM ai_runs")[0][0], "complete")

    def test_organize_network_error_uses_deterministic_fallback(self) -> None:
        client = _PlanDeepSeek([DeepSeekError("network_error", "offline")])
        service = self.service(client)
        result = service.ai_plan_preview(mode="organize", text="P3374 CF1A")
        tasks = result["plan"]["stages"][0]["tasks"]
        self.assertEqual([task["problem_id"] for task in tasks], ["P3374", "CF1A"])
        self.assertEqual(result["ai"]["fallback"]["code"], "network_error")

    def test_organize_mixed_dates_are_cleared_with_warning(self) -> None:
        client = _PlanDeepSeek(
            [{
                "title": "错误排期",
                "groups": [
                    {
                        "topic": "一",
                        "due_date": "2026-08-26",
                        "problem_keys": ["codeforces:CF1A"],
                    },
                    {
                        "topic": "二",
                        "due_date": None,
                        "problem_keys": ["luogu:P3374"],
                    },
                ],
            }]
        )
        service = self.service(client)
        result = service.ai_plan_preview(mode="organize", text="CF1A P3374")
        self.assertEqual(result["plan"]["schedule_mode"], "progressive")
        self.assertTrue(any("日期" in item and "清空" in item for item in result["warnings"]))

    def test_organize_surfaces_invalid_official_link_as_unresolved(self) -> None:
        client = _PlanDeepSeek(
            [{
                "title": "单题",
                "groups": [{
                    "topic": "基础",
                    "due_date": None,
                    "problem_keys": ["codeforces:CF1A"],
                }],
            }]
        )
        service = self.service(client)
        result = service.ai_plan_preview(
            mode="organize",
            text="CF1A https://codeforces.com/problemset/problem/not-a-number/A",
        )
        self.assertIn(
            {
                "input": "https://codeforces.com/problemset/problem/not-a-number/A",
                "reason": "invalid_official_link",
            },
            result["unresolved"],
        )
        self.assertTrue(any("官方链接无法解析" in item for item in result["warnings"]))

    def test_missing_key_does_not_fallback(self) -> None:
        client = _PlanDeepSeek([DeepSeekError("missing_api_key", "missing")])
        service = self.service(client)
        with self.assertRaisesRegex(DeepSeekError, "missing"):
            service.ai_plan_preview(mode="organize", text="CF1A")

    def test_generate_accumulates_strict_ids_and_returns_partial_single_stage(self) -> None:
        client = _PlanDeepSeek(
            [
                {"problem_ids": ["CF1A", "CF999A"]},
                {"problem_ids": ["P3374"]},
                {"problem_ids": ["CF999B"]},
                {"problem_ids": ["CF999C"]},
            ]
        )
        service = self.service(client)
        progress: list[dict[str, object]] = []
        target = "线段树专项，不要无关题"
        result = service.ai_plan_preview(
            mode="generate",
            text=target,
            task_count=3,
            include_completed=False,
            _progress_callback=progress.append,
        )
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(sum(len(stage["tasks"]) for stage in result["plan"]["stages"]), 2)
        self.assertEqual(len(result["plan"]["stages"]), 1)
        self.assertEqual(result["plan"]["title"], "AI 目标题单")
        self.assertEqual(result["plan"]["description"], target)
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][-1]["code"], "insufficient_valid_problems")
        self.assertEqual(result["ai"]["usage"]["total_tokens"], 28)
        self.assertEqual(result["ai"]["rounds"], 4)
        self.assertEqual(result["ai"]["accepted_count"], 2)
        self.assertEqual(result["ai"]["rejected_count"], 3)
        self.assertFalse(result["ai"]["complete"])
        self.assertEqual(result["ai"]["stop_reason"], "no_progress")
        self.assertEqual(len(progress), 8)
        self.assertEqual(progress[0]["message"], "第 1/5 轮，已确定 0/3 题")
        self.assertEqual(progress[-1]["message"], "第 4/5 轮，已确定 2/3 题")
        for _messages, kwargs in client.calls:
            self.assertIs(kwargs["thinking"], True)
            self.assertEqual(kwargs["reasoning_effort"], "high")
            self.assertEqual(kwargs["json_retries"], 0)
            self.assertEqual(kwargs["max_tokens"], 32000)
        first_request = json.loads(client.calls[0][0][-1]["content"].split("\n", 1)[1])
        self.assertEqual(
            set(first_request),
            {"user_text", "requested_count", "supported_platforms", "response_schema"},
        )
        self.assertNotIn("include_completed", first_request)
        second_request = json.loads(client.calls[1][0][-1]["content"].split("\n", 1)[1])
        self.assertIn("remaining_count", second_request)
        self.assertIn("accepted_problem_ids", second_request)
        self.assertIn("excluded_problem_ids", second_request)
        self.assertNotIn("include_completed", second_request)
        with Database(service.paths.database) as db:
            row = db.query(
                "SELECT status,request_summary_json FROM ai_runs WHERE kind='plan_import'"
            )[0]
        summary = json.loads(row["request_summary_json"])
        self.assertEqual(row["status"], "complete")
        self.assertEqual(summary["accepted_count"], 2)
        self.assertEqual(summary["rounds"], 4)
        self.assertTrue(summary["thinking"])
        self.assertEqual(summary["error_code"], "insufficient_valid_problems")
        self.assertNotIn(target, row["request_summary_json"])
        self.assertNotIn("CF1A", row["request_summary_json"])
        self.assertNotIn("text_bytes", summary)
        self.assertNotIn("include_completed", summary)

    def test_generate_completes_in_one_round(self) -> None:
        client = _PlanDeepSeek([{"problem_ids": ["CF1A", "P3374"]}])
        service = self.service(client)
        result = service.ai_plan_preview(
            mode="generate", text="基础训练", task_count=2, include_completed=False
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["ai"]["complete"])
        self.assertEqual(result["ai"]["rounds"], 1)
        self.assertEqual(result["ai"]["stop_reason"], "complete")
        self.assertEqual(
            [task["problem_id"] for task in result["plan"]["stages"][0]["tasks"]],
            ["CF1A", "P3374"],
        )

    def test_generate_treats_json_and_ir_protocol_errors_as_no_progress_rounds(self) -> None:
        client = _PlanDeepSeek([
            DeepSeekError(
                "invalid_json_output", "bad json", usage={"total_tokens": 3},
                finish_reason="stop",
            ),
            {"problem_ids": ["CF1A"]},
            {"problem_ids": ["P3374"], "unexpected": True},
            {"problem_ids": ["P3374"]},
        ])
        service = self.service(client)
        result = service.ai_plan_preview(
            mode="generate", text="基础训练", task_count=2
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["ai"]["rounds"], 2)
        self.assertEqual(result["ai"]["stop_reason"], "validation_failed")
        self.assertEqual(result["ai"]["accepted_count"], 1)
        self.assertEqual(result["ai"]["outcome"]["business_outcome"], "partial")
        self.assertEqual(result["ai"]["outcome"]["repair_attempts"], 1)
        for _messages, kwargs in client.calls:
            self.assertTrue(kwargs["thinking"])
            self.assertEqual(kwargs["reasoning_effort"], "high")
            self.assertEqual(kwargs["json_retries"], 0)

    def test_generate_fatal_provider_failure_is_redacted_in_audit_summary(self) -> None:
        client = _PlanDeepSeek([DeepSeekError("network_error", "offline")])
        service = self.service(client)
        target = "不要写入审计的私密目标"
        result = service.ai_plan_preview(mode="generate", text=target, task_count=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["ai"]["outcome"]["business_outcome"], "unavailable")
        with Database(service.paths.database) as db:
            row = db.query(
                "SELECT status,request_summary_json FROM ai_runs WHERE kind='plan_import'"
            )[0]
        summary = json.loads(row["request_summary_json"])
        self.assertEqual(row["status"], "complete")
        self.assertEqual(summary["error_code"], "network_error")
        self.assertNotIn(target, row["request_summary_json"])
        self.assertNotIn("text_bytes", summary)
        self.assertNotIn("include_completed", summary)

    def test_generate_rejects_more_than_30_tasks_before_model_call(self) -> None:
        client = _PlanDeepSeek([])
        service = self.service(client)
        with self.assertRaisesRegex(ValueError, "1 到 30"):
            service.ai_plan_preview(mode="generate", text="综合训练", task_count=31)
        self.assertEqual(client.calls, [])

    def test_generate_rejects_non_integer_task_count(self) -> None:
        client = _PlanDeepSeek([])
        service = self.service(client)
        for value in (12.9, "12"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "必须是整数"):
                    service.ai_plan_preview(mode="generate", text="综合训练", task_count=value)
        self.assertEqual(client.calls, [])

    def test_generate_stops_after_two_consecutive_rounds_without_progress(self) -> None:
        client = _PlanDeepSeek([
            {"problem_ids": ["CF999A"]},
            {"problem_ids": ["CF999B"]},
        ])
        service = self.service(client)
        result = service.ai_plan_preview(
            mode="generate", text="不存在于本地的专项", task_count=3
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result["ai"]["accepted_count"], 0)
        self.assertEqual(result["ai"]["stop_reason"], "no_progress")
        self.assertEqual(result["plan"]["stages"][0]["tasks"], [])
        self.assertEqual(result["errors"][-1]["code"], "insufficient_valid_problems")

    def test_job_route_is_registered(self) -> None:
        self.assertEqual(
            AcmRequestHandler._job_routes["/api/jobs/ai/plans/preview"],
            "ai_plan_preview",
        )


if __name__ == "__main__":
    unittest.main()
