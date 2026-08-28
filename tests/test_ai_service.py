from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from tools.acm_agent.ai_context import (
    AIContextError,
    apply_source_patch,
    content_sha256,
    expected_patch_backup_path,
)
from tools.acm_agent.deepseek import ChatResult, JsonChatResult, StreamEvent
from tools.acm_agent.provider import ProviderError
from tools.acm_agent.credentials import DeepSeekCredentialStore
from tools.acm_agent.config import load_config, save_config
from tools.acm_agent.service import AcmService
from tools.acm_agent.service_common import AIConversationConflict
from tools.acm_agent.storage import Database


REPO_ROOT = Path(__file__).resolve().parents[1]
CHINESE_EXPLANATION_RULE = "除非用户显式要求其他语言，否则解释性内容使用简体中文"


@dataclass
class _VerifyResult:
    passed: bool = False

    def to_dict(self):
        return {
            "problem_id": "CF1A",
            "source": "hidden",
            "passed": self.passed,
            "compiled": True,
            "compile_command": [],
            "compile_output": "",
            "sanitizer": "not_requested",
            "cases": [],
            "stress": "not_available",
            "stress_iterations": 0,
            "failure_dir": None,
            "warnings": [],
        }


class FakeDeepSeek:
    key_detected = True

    def __init__(self):
        self.calls = []
        self.patch_code = (
            "#include <bits/stdc++.h>\n"
            "int main(){\n"
            "  // 原代码错误：返回非零值会表示异常结束；改为 0 表示正常结束。\n"
            "  return 0;\n"
            "}\n"
        )

    def chat(self, messages, **kwargs):
        self.calls.append(("chat", messages, kwargs))
        return ChatResult("检查边界并维护循环不变量。", "stop", {"total_tokens": 9}, kwargs["model"])

    def chat_json(self, messages, **kwargs):
        self.calls.append(("json", messages, kwargs))
        prompt = "\n".join(item["content"] for item in messages)
        if '"replacement_code"' in prompt:
            data = {
                "diagnosis": "返回值错误",
                "replacement_code": self.patch_code,
            }
        else:
            request = next(
                value
                for item in reversed(messages)
                for line in reversed(str(item["content"]).splitlines())
                if line.lstrip().startswith("{")
                for value in [json.loads(line)]
                if isinstance(value, dict) and "eligible_focus_topics" in value
            )
            focus_topics = request["eligible_focus_topics"][: min(3, request["requested_count"])]
            ranked = []
            seen = set()
            for topic in focus_topics:
                item = next(
                    item
                    for item in request["candidates"]
                    if item["problem_key"] not in seen and topic in item["knowledge_topics"]
                )
                seen.add(item["problem_key"])
                ranked.append(
                    {
                        "problem_key": item["problem_key"],
                        "topic": topic,
                        "ai_reason": "针对近期薄弱标签",
                        "training_focus": "先写出不变量",
                    }
                )
            for item in request["candidates"]:
                if len(ranked) >= request["requested_count"]:
                    break
                topic = next(
                    (topic for topic in item["knowledge_topics"] if topic in focus_topics),
                    None,
                )
                if item["problem_key"] in seen or topic is None:
                    continue
                seen.add(item["problem_key"])
                ranked.append(
                    {
                        "problem_key": item["problem_key"],
                        "topic": topic,
                        "ai_reason": "针对近期薄弱标签",
                        "training_focus": "先写出不变量",
                    }
                )
            data = {
                "focus_topics": focus_topics,
                "ranked": ranked,
                "risk_warning": "难度上浮",
            }
        return JsonChatResult(
            json.dumps(data, ensure_ascii=False),
            "stop",
            {"total_tokens": 17},
            kwargs["model"],
            data,
        )

    def stream_chat(self, messages, **kwargs):
        self.calls.append(("stream", messages, kwargs))
        yield StreamEvent("delta", content="第一段")
        yield StreamEvent("delta", content="第二段")
        yield StreamEvent("usage", usage={"total_tokens": 12})
        yield StreamEvent("done", finish_reason="stop", usage={"total_tokens": 12})

    def structured(self, messages, **kwargs):
        return self.chat_json(messages, **kwargs)


class AiServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        target = self.root / "training" / "data-structures-30d"
        target.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/plan.json", target / "plan.json")
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/README.md", target / "README.md")
        plan_document = json.loads((target / "plan.json").read_text(encoding="utf-8"))
        for task in plan_document["stages"][0]["tasks"][:3]:
            task["tags"] = ["segment tree"]
        (target / "plan.json").write_text(
            json.dumps(plan_document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.client = FakeDeepSeek()
        self.verify_calls = []

        def fake_verify(root, problem, **kwargs):
            self.verify_calls.append(Path(problem))
            return _VerifyResult(False)

        self.service = AcmService(
            self.root,
            deepseek_client_factory=lambda: self.client,
            problem_context_fetcher=lambda ref: ("题目描述\n输入输出", f"https://example.test/{ref.problem_id}"),
            verify_fn=fake_verify,
        )
        self.service.setup("private-handle", "4242", target_rating=1800, skip_validate=True)

    def tearDown(self):
        self.temporary.cleanup()

    def test_settings_status_and_recommendation_payload_privacy(self):
        status = self.service.ai_settings(
            recommendation_model="deepseek-v4-pro",
            coaching_model="deepseek-v4-flash",
            coaching_thinking=True,
            reasoning_effort="max",
        )
        self.assertTrue(status["api_key_detected"])
        self.assertEqual(set(status) & {"api_key", "token", "authorization"}, set())
        self.assertEqual(
            set(status["settings"]),
            {
                "recommendation_model",
                "coaching_model",
                "recommendation_thinking",
                "coaching_thinking",
                "reasoning_effort",
                "summary_model",
                "summary_thinking",
                "summary_reasoning_effort",
            },
        )

        self.service.start("CF1A")
        self.service.close(
            "CF1A",
            result="WA",
            minutes=33,
            hint_level=2,
            failure="implementation",
            notes="private note must not leave",
        )
        payload = self.service.ai_recommendations(count=3, source_mode="plan_only")
        self.assertIsNone(payload["ai"]["fallback"])
        self.assertTrue(all(row["ranking_basis"] == "deepseek_reranked" for row in payload["recommendations"]))
        sent = json.dumps(self.client.calls[-1][1], ensure_ascii=False)
        self.assertIn(CHINESE_EXPLANATION_RULE, sent)
        self.assertIn("代码、算法名和复杂度表达无需翻译", sent)
        self.assertNotIn("private-handle", sent)
        self.assertNotIn("4242", sent)
        self.assertNotIn("private note", sent)
        self.assertNotIn(str(self.root), sent)
        self.assertNotIn("#include", sent)
        self.assertEqual(self.client.calls[-1][2]["max_tokens"], 2_400)

    def test_cache_clear_allows_safe_profile_disabled_by_current_policy(self):
        config = load_config(self.service.paths)
        config["ai"]["cache"]["exact_profiles"] = []
        save_config(self.service.paths, config)

        result = self.service.ai_cache_clear(profile_ids=["recommendation"])

        self.assertTrue(result["ok"])
        with self.assertRaisesRegex(ValueError, "plan_generate"):
            self.service.ai_cache_clear(profile_ids=["plan_generate"])

    def test_chat_persists_stream_and_raises_close_hint_level(self):
        self.service.start("CF1A")
        conversation = self.service.ai_conversation_start("CF1A")
        events = list(
            self.service.ai_chat_stream(
                conversation["conversation_id"],
                message="给我关键性质",
                mode="hint",
                hint_level=2,
                delivery_mode="low_latency",
            )
        )
        self.assertEqual([row["event"] for row in events], ["meta", "delta", "delta", "usage", "done"])
        sent = json.dumps(self.client.calls[-1][1], ensure_ascii=False)
        self.assertIn(CHINESE_EXPLANATION_RULE, sent)
        self.assertIn("代码、算法名和复杂度表达无需翻译", sent)
        self.assertEqual(self.client.calls[-1][2]["max_tokens"], 4_096)
        loaded = self.service.ai_conversation(conversation["conversation_id"])
        self.assertEqual(loaded["messages"][-1]["content"], "第一段第二段")
        self.assertEqual(loaded["messages"][-1]["status"], "complete")
        closed = self.service.close(
            "CF1A",
            result="AC",
            minutes=20,
            hint_level=0,
            failure="none",
        )
        self.assertEqual(closed["close"]["hint_level"], 2)
        loaded = self.service.ai_conversation(conversation["conversation_id"])
        self.assertEqual(loaded["conversation"]["status"], "closed")

    def test_coaching_keeps_stable_prefix_across_hint_levels(self):
        self.service.start("CF1A")
        conversation = self.service.ai_conversation_start("CF1A")
        conversation_id = conversation["conversation_id"]

        self.service.ai_chat(
            "CF1A",
            conversation_id=conversation_id,
            message="先问我一个反例问题",
            mode="hint",
            hint_level=1,
        )
        first_messages = self.client.calls[-1][1]
        self.service.ai_chat(
            "CF1A",
            conversation_id=conversation_id,
            message="现在给出核心转化",
            mode="hint",
            hint_level=3,
        )
        second_messages = self.client.calls[-1][1]

        self.assertEqual(second_messages[: len(first_messages)], first_messages)
        self.assertEqual(first_messages[0], second_messages[0])
        self.assertNotIn("started_at", json.dumps(second_messages, ensure_ascii=False))
        first_turn = json.loads(first_messages[-1]["content"])
        second_turn = json.loads(second_messages[-1]["content"])
        self.assertEqual(first_turn["hint_level"], 1)
        self.assertEqual(second_turn["hint_level"], 3)

    def test_close_counts_streaming_hint_before_first_content(self):
        self.service.start("CF1A")
        conversation = self.service.ai_conversation_start("CF1A")
        stream = self.service.ai_chat_stream(
            conversation["conversation_id"],
            message="给我关键性质",
            mode="hint",
            hint_level=2,
        )
        self.assertEqual(next(stream)["event"], "meta")
        closed = self.service.close(
            "CF1A", result="AC", minutes=10, hint_level=0, failure="none"
        )
        self.assertEqual(closed["close"]["hint_level"], 2)
        stream.close()

    def test_conversation_allows_only_one_in_flight_turn_and_releases_on_close(self):
        self.service.start("CF1A")
        conversation = self.service.ai_conversation_start("CF1A")
        conversation_id = conversation["conversation_id"]
        stream = self.service.ai_chat_stream(
            conversation_id,
            message="第一个请求",
            mode="hint",
            hint_level=1,
        )

        with self.assertRaises(AIConversationConflict) as raised:
            self.service.ai_chat_stream(
                conversation_id,
                message="并发请求",
                mode="hint",
                hint_level=1,
            )
        self.assertEqual(raised.exception.code, "conversation_busy")

        self.assertEqual(next(stream)["event"], "meta")
        stream.close()
        with Database(self.service.paths.database) as db:
            in_flight = db.connection.execute(
                """SELECT COUNT(*) FROM ai_messages
                   WHERE conversation_id=? AND status IN ('pending','streaming')""",
                (conversation_id,),
            ).fetchone()[0]
            running = db.connection.execute(
                """SELECT COUNT(*) FROM ai_runs
                   WHERE conversation_id=? AND status IN ('pending','running')""",
                (conversation_id,),
            ).fetchone()[0]
        self.assertEqual((in_flight, running), (0, 0))

        events = list(
            self.service.ai_chat_stream(
                conversation_id,
                message="中断后重试",
                mode="hint",
                hint_level=1,
            )
        )
        self.assertEqual(events[-1]["event"], "done")

    def test_identical_coaching_requests_coalesce_and_replay_terminal_result(self):
        self.service.start("CF1A")
        conversation_id = self.service.ai_conversation_start("CF1A")["conversation_id"]
        started = threading.Event()
        release = threading.Event()
        provider_calls = 0

        def blocking_chat(messages, **kwargs):
            nonlocal provider_calls
            provider_calls += 1
            started.set()
            self.assertTrue(release.wait(5))
            return ChatResult(
                "共享终态",
                "stop",
                {"total_tokens": 9},
                kwargs["model"],
            )

        self.client.chat = blocking_chat
        request = {
            "message": "给我一个反例问题",
            "mode": "hint",
            "hint_level": 1,
            "conversation_id": conversation_id,
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            leader = executor.submit(self.service.ai_chat, "CF1A", **request)
            self.assertTrue(started.wait(5))
            follower = executor.submit(self.service.ai_chat, "CF1A", **request)
            time.sleep(0.1)
            release.set()
            leader_result = leader.result(timeout=5)
            follower_result = follower.result(timeout=5)

        self.assertEqual(provider_calls, 1)
        self.assertEqual(leader_result["content"], "共享终态")
        self.assertEqual(follower_result["content"], "共享终态")
        self.assertEqual(follower_result["local_cache"]["status"], "coalesced")
        with Database(self.service.paths.database) as db:
            follower_run = db.ai_run(follower_result["ai_run_id"])
            metrics = db.ai_cache_status()
        self.assertEqual(follower_run["local_cache_status"], "coalesced")
        self.assertEqual(follower_run["status"], "complete")
        self.assertGreaterEqual(metrics["coalesced_followers"], 1)

    def test_identical_stream_follower_receives_live_delta_before_leader_done(self):
        self.service.start("CF1A")
        conversation_id = self.service.ai_conversation_start("CF1A")["conversation_id"]
        delta_ready = threading.Event()
        release = threading.Event()
        provider_calls = 0

        def blocking_stream(messages, **kwargs):
            nonlocal provider_calls
            provider_calls += 1
            delta_ready.set()
            yield StreamEvent("delta", content="实时片段")
            self.assertTrue(release.wait(5))
            yield StreamEvent("done", finish_reason="stop", usage={"total_tokens": 4})

        self.client.stream_chat = blocking_stream
        request = {
            "message": "给我一个反例问题",
            "mode": "hint",
            "hint_level": 1,
            "delivery_mode": "low_latency",
        }
        leader_stream = self.service.ai_chat_stream(conversation_id, **request)
        leader_events: list[dict] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            leader = executor.submit(lambda: leader_events.extend(list(leader_stream)))
            self.assertTrue(delta_ready.wait(5))
            follower_stream = self.service.ai_chat_stream(conversation_id, **request)
            follower_meta = next(follower_stream)
            follower_delta = next(follower_stream)
            self.assertFalse(release.is_set())
            self.assertEqual(follower_meta["event"], "meta")
            self.assertEqual(follower_delta, {"event": "delta", "data": {"content": "实时片段"}})
            release.set()
            follower_tail = list(follower_stream)
            leader.result(timeout=5)

        self.assertEqual(provider_calls, 1)
        self.assertEqual(follower_tail[-1]["event"], "done")

    def test_coalesced_failure_returns_unavailable_without_second_provider_call(self):
        self.service.start("CF1A")
        conversation_id = self.service.ai_conversation_start("CF1A")["conversation_id"]
        provider_started = threading.Event()
        release = threading.Event()
        provider_calls = 0

        def failing_chat(messages, **kwargs):
            nonlocal provider_calls
            provider_calls += 1
            provider_started.set()
            self.assertTrue(release.wait(5))
            raise ProviderError("server_error", "offline", retryable=False)

        self.client.chat = failing_chat
        request = {
            "message": "只给一个反例问题",
            "mode": "hint",
            "hint_level": 1,
            "conversation_id": conversation_id,
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            leader_future = executor.submit(self.service.ai_chat, "CF1A", **request)
            self.assertTrue(provider_started.wait(5))
            follower_future = executor.submit(self.service.ai_chat, "CF1A", **request)
            time.sleep(0.05)
            release.set()
            leader = leader_future.result(timeout=5)
            follower = follower_future.result(timeout=5)

        self.assertEqual(provider_calls, 1)
        self.assertFalse(leader["ok"])
        self.assertFalse(follower["ok"])
        self.assertEqual(leader["ai"]["outcome"]["business_outcome"], "unavailable")
        self.assertEqual(follower["ai"]["outcome"]["business_outcome"], "unavailable")
        self.assertEqual(follower["ai"]["outcome"]["provider_outcome"], "not_called")
        with Database(self.service.paths.database) as db:
            follower_run = db.ai_run(follower["ai_run_id"])
        self.assertEqual(follower_run["status"], "complete")
        self.assertEqual(follower_run["provider_outcome"], "not_called")

    def test_patch_preview_apply_verify_failure_and_revert(self):
        started = self.service.start("CF1A")
        source = Path(started["source"])
        original = source.read_text(encoding="utf-8")
        preview = self.service.ai_patch_preview("CF1A", instruction="修复返回值")
        sent = json.dumps(self.client.calls[-1][1], ensure_ascii=False)
        self.assertIn(CHINESE_EXPLANATION_RULE, sent)
        self.assertIn("diagnosis 属于解释性内容", sent)
        self.assertIn("代码、算法名和复杂度表达无需翻译", sent)
        self.assertIn("每个实质修复点附近加入简短的中文 C++ 注释", sent)
        self.assertIn("原代码哪里错误以及该修改为何正确", sent)
        self.assertEqual(self.client.calls[-1][2]["max_tokens"], 8_192)
        self.assertIn("proposal_id", preview)
        self.assertEqual(preview["candidate_code"], self.client.patch_code)
        self.assertIn("// 原代码错误", preview["candidate_code"])
        self.assertIn("--- a/", preview["diff"])
        applied = self.service.ai_patch_apply(preview["proposal_id"])
        self.assertFalse(applied["verify"]["passed"])
        self.assertEqual(self.verify_calls[-1].resolve(), source.resolve())
        self.assertNotEqual(source.read_text(encoding="utf-8"), original)
        reverted = self.service.ai_patch_revert(preview["proposal_id"])
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        with Database(self.service.paths.database) as db:
            self.assertEqual(db.ai_patch_proposal(preview["proposal_id"])["status"], "reverted")

    def test_invalid_patch_marks_message_and_run_failed(self):
        self.service.start("CF1A")
        self.client.patch_code = "```cpp\nint main(){}\n```"
        result = self.service.ai_patch_preview("CF1A", instruction="修复")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["proposal_id"])
        self.assertEqual(result["ai"]["outcome"]["business_outcome"], "partial")
        self.assertEqual(result["ai"]["outcome"]["repair_attempts"], 1)
        with Database(self.service.paths.database) as db:
            assistant = db.query(
                "SELECT status FROM ai_messages WHERE role='assistant' ORDER BY rowid DESC LIMIT 1"
            )[0]
            run = db.query(
                "SELECT status FROM ai_runs WHERE kind='patch' ORDER BY rowid DESC LIMIT 1"
            )[0]
            self.assertEqual(assistant["status"], "error")
            self.assertEqual(run["status"], "complete")

    def test_patch_apply_db_failure_restores_source_and_preview_state(self):
        started = self.service.start("CF1A")
        source = Path(started["source"])
        original_source = source.read_bytes()
        preview = self.service.ai_patch_preview("CF1A", instruction="修复返回值")
        original_update = Database.update_ai_patch_proposal
        failed = False

        def flaky_update(database, proposal_id, **kwargs):
            nonlocal failed
            if kwargs.get("status") == "applied" and not failed:
                failed = True
                raise sqlite3.OperationalError("injected final update failure")
            return original_update(database, proposal_id, **kwargs)

        with mock.patch.object(Database, "update_ai_patch_proposal", new=flaky_update):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.ai_patch_apply(preview["proposal_id"])
        self.assertEqual(source.read_bytes(), original_source)
        with Database(self.service.paths.database) as db:
            self.assertEqual(db.ai_patch_proposal(preview["proposal_id"])["status"], "preview")

    def test_patch_revert_db_failure_reapplies_candidate_and_keeps_applied(self):
        started = self.service.start("CF1A")
        source = Path(started["source"])
        preview = self.service.ai_patch_preview("CF1A", instruction="修复返回值")
        self.service.ai_patch_apply(preview["proposal_id"])
        candidate_source = source.read_bytes()
        original_update = Database.update_ai_patch_proposal
        failed = False

        def flaky_update(database, proposal_id, **kwargs):
            nonlocal failed
            if kwargs.get("status") == "reverted" and not failed:
                failed = True
                raise sqlite3.OperationalError("injected final update failure")
            return original_update(database, proposal_id, **kwargs)

        with mock.patch.object(Database, "update_ai_patch_proposal", new=flaky_update):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.ai_patch_revert(preview["proposal_id"])
        self.assertEqual(source.read_bytes(), candidate_source)
        with Database(self.service.paths.database) as db:
            self.assertEqual(db.ai_patch_proposal(preview["proposal_id"])["status"], "applied")

    def test_service_startup_recovers_interrupted_patch_apply(self):
        self.service.start("CF1A")
        preview = self.service.ai_patch_preview("CF1A", instruction="修复返回值")
        with Database(self.service.paths.database) as db:
            proposal = dict(db.ai_patch_proposal(preview["proposal_id"]))
            backup = expected_patch_backup_path(
                self.root,
                proposal["source_path"],
                backup_id=proposal["id"],
                original_sha256=proposal["baseline_hash"],
            )
            db.update_ai_patch_proposal(
                proposal["id"], status="applying", backup_path=backup
            )
        apply_source_patch(
            self.root,
            proposal["source_path"],
            proposal["candidate_code"],
            expected_sha256=proposal["baseline_hash"],
            backup_id=proposal["id"],
        )
        AcmService(
            self.root,
            deepseek_client_factory=lambda: self.client,
            problem_context_fetcher=lambda ref: (
                "题目描述\n输入输出",
                f"https://example.test/{ref.problem_id}",
            ),
            verify_fn=lambda *args, **kwargs: _VerifyResult(False),
        )
        with Database(self.service.paths.database) as db:
            recovered = db.ai_patch_proposal(preview["proposal_id"])
            self.assertEqual(recovered["status"], "applied")
            self.assertEqual(
                recovered["applied_hash"], content_sha256(proposal["candidate_code"])
            )

    def test_luogu_context_fetch_uses_raw_payload_even_with_numeric_tags(self):
        class FakeLuogu:
            def problem_payload(inner_self, problem_id):
                return {
                    "problem": {
                        "pid": problem_id,
                        "tags": [1, 7],
                        "content": {
                            "description": "计算答案。",
                            "inputFormat": "输入。",
                            "outputFormat": "输出。",
                        },
                    }
                }

            def problem(inner_self, problem_id, **kwargs):
                raise AssertionError("parsed problem API must not be used for context")

        service = AcmService(
            self.root,
            deepseek_client_factory=lambda: self.client,
            luogu_client_factory=FakeLuogu,
            verify_fn=lambda *args, **kwargs: _VerifyResult(False),
        )
        fetched = service.problem_context_fetch("P1000", force=True)
        self.assertTrue(fetched["available"])
        self.assertIn("计算答案", fetched["content"])

    def test_problem_context_persists_structured_samples(self):
        statement = """## 题目描述
求和。

### 样例输入 1
```text
1 2
```
### 样例输出 1
```text
3
```
"""
        self.service.problem_context_save("P1000", content=statement)
        with Database(self.service.paths.database) as db:
            rows = db.problem_samples("luogu", "P1000")
        self.assertEqual(len(rows), 1)
        self.assertEqual(bytes(rows[0]["input_data"]), b"1 2\n")
        self.assertEqual(bytes(rows[0]["expected_output"]), b"3\n")


class AiCredentialServiceTests(unittest.TestCase):
    def test_web_credential_persists_across_service_restart_without_echo(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {}, clear=True
        ):
            root = Path(directory)
            path = root / ".acm" / "deepseek-key.dpapi"

            def protect(value: bytes) -> bytes:
                return b"cipher:" + value[::-1]

            def unprotect(value: bytes) -> bytes:
                return value[len(b"cipher:") :][::-1]

            first = AcmService(
                root,
                credential_store=DeepSeekCredentialStore(
                    path, protect=protect, unprotect=unprotect
                ),
            )
            self.assertFalse(first.ai_status()["api_key_detected"])
            secret = "sk-browser-persistence-fixture"
            enabled = first.ai_credential(api_key=secret)
            self.assertTrue(enabled["api_key_detected"])
            self.assertTrue(enabled["credential_persisted"])
            self.assertEqual(enabled["credential_source"], "secure_store")
            self.assertNotIn(secret, json.dumps(enabled))
            self.assertNotIn(secret.encode(), path.read_bytes())

            restarted = AcmService(
                root,
                credential_store=DeepSeekCredentialStore(
                    path, protect=protect, unprotect=unprotect
                ),
            )
            self.assertTrue(restarted.ai_status()["api_key_detected"])
            self.assertEqual(
                restarted.ai_status()["credential_source"], "secure_store"
            )
            cleared = restarted.ai_credential(clear=True)
            self.assertFalse(cleared["api_key_detected"])
            self.assertFalse(path.exists())

    def test_invalid_browser_credential_is_rejected_without_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".acm" / "deepseek-key.dpapi"
            service = AcmService(
                root,
                credential_store=DeepSeekCredentialStore(
                    path, protect=lambda value: value, unprotect=lambda value: value
                ),
            )
            for invalid in ("", "line\nbreak", "x" * 513):
                with self.subTest(invalid_length=len(invalid)):
                    with self.assertRaises(ValueError):
                        service.ai_credential(api_key=invalid)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
