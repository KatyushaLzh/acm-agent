from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from tools.acm_agent.ai_context import (
    AIContextError,
    apply_source_patch,
    content_sha256,
    expected_patch_backup_path,
)
from tools.acm_agent.deepseek import ChatResult, JsonChatResult, StreamEvent
from tools.acm_agent.credentials import DeepSeekCredentialStore
from tools.acm_agent.service import AcmService
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
            request = json.loads(prompt.splitlines()[-1])
            ranked = [
                {
                    "problem_key": item["problem_key"],
                    "ai_reason": "针对近期薄弱标签",
                    "training_focus": "先写出不变量",
                }
                for item in reversed(request["candidates"])
            ]
            data = {"ranked": ranked, "risk_warning": "难度上浮"}
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


class AiServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        target = self.root / "training" / "data-structures-30d"
        target.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/plan.json", target / "plan.json")
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/README.md", target / "README.md")
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

    def test_chat_persists_stream_and_raises_close_hint_level(self):
        self.service.start("CF1A")
        conversation = self.service.ai_conversation_start("CF1A")
        events = list(
            self.service.ai_chat_stream(
                conversation["conversation_id"],
                message="给我关键性质",
                mode="hint",
                hint_level=2,
            )
        )
        self.assertEqual([row["event"] for row in events], ["meta", "delta", "delta", "usage", "done"])
        sent = json.dumps(self.client.calls[-1][1], ensure_ascii=False)
        self.assertIn(CHINESE_EXPLANATION_RULE, sent)
        self.assertIn("代码、算法名和复杂度表达无需翻译", sent)
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
        with self.assertRaises(AIContextError):
            self.service.ai_patch_preview("CF1A", instruction="修复")
        with Database(self.service.paths.database) as db:
            assistant = db.query(
                "SELECT status FROM ai_messages WHERE role='assistant' ORDER BY rowid DESC LIMIT 1"
            )[0]
            run = db.query(
                "SELECT status FROM ai_runs WHERE kind='patch' ORDER BY rowid DESC LIMIT 1"
            )[0]
            self.assertEqual(assistant["status"], "error")
            self.assertEqual(run["status"], "failed")

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
