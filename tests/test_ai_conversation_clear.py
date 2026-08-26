from __future__ import annotations

import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from tools.acm_agent.service import AIConversationConflict, AcmService
from tools.acm_agent.storage import Database
from tools.acm_agent.web import create_server


REPO_ROOT = Path(__file__).resolve().parents[1]


class AIConversationClearServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        target = self.root / "training" / "data-structures-30d"
        target.mkdir(parents=True)
        shutil.copy2(
            REPO_ROOT / "training/data-structures-30d/plan.json",
            target / "plan.json",
        )
        shutil.copy2(
            REPO_ROOT / "training/data-structures-30d/README.md",
            target / "README.md",
        )
        self.service = AcmService(self.root)
        self.service.setup(
            "private-handle", "4242", target_rating=1800, skip_validate=True
        )
        self.service.start("CF1A")
        self.started = self.service.ai_conversation_start("CF1A")
        self.conversation_id = str(self.started["conversation_id"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_conflict(self, code: str, callback) -> None:
        with self.assertRaises(AIConversationConflict) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_clear_archives_history_and_creates_empty_current_conversation(self) -> None:
        with Database(self.service.paths.database) as db:
            db.create_ai_message(
                "user-complete",
                self.conversation_id,
                role="user",
                content="旧问题",
                status="complete",
            )
            db.create_ai_message(
                "assistant-complete",
                self.conversation_id,
                role="assistant",
                content="旧回答",
                hint_level=3,
                status="complete",
            )

        result = self.service.ai_conversation_clear(self.conversation_id)

        self.assertNotEqual(result["conversation_id"], self.conversation_id)
        self.assertEqual(result["cleared_conversation_id"], self.conversation_id)
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["cleared_message_count"], 2)
        self.assertTrue(result["preserved_history"])
        with Database(self.service.paths.database) as db:
            old = db.ai_conversation(self.conversation_id)
            replacement = db.ai_conversation(result["conversation_id"])
            self.assertEqual(old["status"], "closed")
            self.assertEqual(len(db.ai_messages(self.conversation_id)), 2)
            self.assertEqual(replacement["status"], "active")
            self.assertEqual(replacement["attempt_id"], old["attempt_id"])
            self.assertEqual(db.ai_messages(result["conversation_id"]), [])
            self.assertEqual(
                db.active_ai_conversation(int(old["attempt_id"]))["id"],
                result["conversation_id"],
            )
            self.assertEqual(db.max_ai_hint_level(int(old["attempt_id"])), 3)

        closed = self.service.close(
            "CF1A", result="AC", minutes=20, hint_level=0, failure="none"
        )
        self.assertEqual(closed["close"]["hint_level"], 3)

    def test_each_active_problem_restores_its_own_persisted_conversation(self) -> None:
        self.service.start("P1000")
        luogu = self.service.ai_conversation_start("P1000")
        with Database(self.service.paths.database) as db:
            db.create_ai_message(
                "cf-history",
                self.conversation_id,
                role="user",
                content="CF 的历史",
                status="complete",
            )
            db.create_ai_message(
                "luogu-history",
                luogu["conversation_id"],
                role="user",
                content="洛谷的历史",
                status="complete",
            )

        restored_cf = self.service.ai_conversation_start("CF1A")
        restored_luogu = self.service.ai_conversation_start("P1000")
        self.assertEqual(restored_cf["conversation_id"], self.conversation_id)
        self.assertEqual(restored_luogu["conversation_id"], luogu["conversation_id"])
        self.assertEqual(
            [message["content"] for message in restored_cf["messages"]],
            ["CF 的历史"],
        )
        self.assertEqual(
            [message["content"] for message in restored_luogu["messages"]],
            ["洛谷的历史"],
        )

    def test_clear_rejects_pending_or_streaming_work(self) -> None:
        with Database(self.service.paths.database) as db:
            db.create_ai_message(
                "assistant-streaming",
                self.conversation_id,
                role="assistant",
                content="partial",
                status="streaming",
            )
        self._assert_conflict(
            "conversation_busy",
            lambda: self.service.ai_conversation_clear(self.conversation_id),
        )
        with Database(self.service.paths.database) as db:
            self.assertEqual(db.ai_conversation(self.conversation_id)["status"], "active")

        with Database(self.service.paths.database) as db:
            db.update_ai_message("assistant-streaming", status="interrupted")
            db.create_ai_run(
                "run-running",
                kind="coaching",
                model="deepseek-v4-flash",
                conversation_id=self.conversation_id,
                status="running",
            )
        self._assert_conflict(
            "conversation_busy",
            lambda: self.service.ai_conversation_clear(self.conversation_id),
        )
        with Database(self.service.paths.database) as db:
            self.assertEqual(db.ai_conversation(self.conversation_id)["status"], "active")

    def test_clear_rejects_inactive_session_and_missing_conversation(self) -> None:
        with Database(self.service.paths.database) as db:
            db.connection.execute(
                "UPDATE attempts SET active=0 WHERE id=?", (self.started["attempt_id"],)
            )
        self._assert_conflict(
            "session_not_active",
            lambda: self.service.ai_conversation_clear(self.conversation_id),
        )
        with self.assertRaises(KeyError):
            self.service.ai_conversation_clear("missing-conversation")

    def test_old_or_wrong_problem_conversation_cannot_silently_switch(self) -> None:
        result = self.service.ai_conversation_clear(self.conversation_id)
        replacement_id = str(result["conversation_id"])

        self._assert_conflict(
            "conversation_not_active",
            lambda: self.service.ai_chat(
                "CF1A", message="继续", conversation_id=self.conversation_id
            ),
        )
        self._assert_conflict(
            "conversation_not_active",
            lambda: self.service.ai_chat_stream(
                self.conversation_id, message="继续"
            ),
        )
        self._assert_conflict(
            "conversation_not_current",
            lambda: self.service.ai_conversation_start(
                "CF1A", conversation_id=self.conversation_id
            ),
        )
        self._assert_conflict(
            "conversation_problem_mismatch",
            lambda: self.service.ai_chat(
                "P1000", message="错误绑定", conversation_id=replacement_id
            ),
        )
        with Database(self.service.paths.database) as db:
            self.assertEqual(db.ai_messages(replacement_id), [])

        self._assert_conflict(
            "conversation_not_active",
            lambda: self.service.ai_conversation_clear(self.conversation_id),
        )
        with Database(self.service.paths.database) as db:
            current = db.active_ai_conversation(int(self.started["attempt_id"]))
            self.assertEqual(current["id"], replacement_id)

    def test_clear_rolls_back_close_if_replacement_creation_fails(self) -> None:
        with mock.patch.object(
            Database,
            "get_or_create_ai_conversation",
            side_effect=RuntimeError("injected replacement failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected replacement failure"):
                self.service.ai_conversation_clear(self.conversation_id)

        with Database(self.service.paths.database) as db:
            conversation = db.ai_conversation(self.conversation_id)
            self.assertEqual(conversation["status"], "active")
            self.assertEqual(
                db.active_ai_conversation(int(conversation["attempt_id"]))["id"],
                self.conversation_id,
            )

    def test_route_switch_archives_old_conversation_and_clear_preserves_pin(self) -> None:
        selection = {
            "model_ref": {
                "provider_id": "deepseek",
                "model": "deepseek-v4-flash",
            },
            "reasoning_strength": "medium",
        }
        switched = self.service.ai_conversation_switch(
            self.conversation_id, **selection
        )
        self.assertNotEqual(switched["conversation_id"], self.conversation_id)
        with Database(self.service.paths.database) as db:
            old = db.ai_conversation(self.conversation_id)
            current = db.ai_conversation(switched["conversation_id"])
            self.assertEqual(old["status"], "closed")
            self.assertEqual(old["closed_reason"], "user_cleared")
            self.assertEqual(current["provider_id"], "deepseek")
            self.assertEqual(current["model"], "deepseek-v4-flash")
            self.assertEqual(current["reasoning_strength"], "medium")

        cleared = self.service.ai_conversation_clear(switched["conversation_id"])
        with Database(self.service.paths.database) as db:
            replacement = db.ai_conversation(cleared["conversation_id"])
            self.assertEqual(replacement["provider_id"], "deepseek")
            self.assertEqual(replacement["model"], "deepseek-v4-flash")
            self.assertEqual(replacement["reasoning_strength"], "medium")

    def test_route_switch_rejects_streaming_conversation_with_409_conflict(self) -> None:
        with Database(self.service.paths.database) as db:
            db.create_ai_message(
                "assistant-streaming-switch",
                self.conversation_id,
                role="assistant",
                status="streaming",
            )
        self._assert_conflict(
            "conversation_busy",
            lambda: self.service.ai_conversation_switch(
                self.conversation_id,
                model_ref={
                    "provider_id": "deepseek",
                    "model": "deepseek-v4-flash",
                },
                reasoning_strength="auto",
            ),
        )


class _ClearWebService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ai_conversation_clear(self, conversation_id: str):
        self.calls.append(conversation_id)
        if conversation_id == "busy":
            raise AIConversationConflict("conversation_busy", "conversation is busy")
        if conversation_id == "missing":
            raise KeyError("conversation not found")
        return {
            "ok": True,
            "cleared_conversation_id": conversation_id,
            "conversation_id": "replacement",
            "messages": [],
        }


class AIConversationClearWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<h1>ACM</h1>", encoding="utf-8")
        self.service = _ClearWebService()
        self.server = create_server(
            self.root,
            service=self.service,
            port=0,
            max_port=0,
            token="clear-token",
            static_dir=self.static,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.server.cleanup()
        self.temporary.cleanup()

    def request(self, path: str, payload: object) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.port, timeout=2
        )
        body = json.dumps(payload).encode("utf-8")
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-ACM-Token": "clear-token",
            },
        )
        response = connection.getresponse()
        decoded = json.loads(response.read())
        connection.close()
        return response.status, decoded

    def test_clear_route_success_and_path_validation(self) -> None:
        status, payload = self.request("/api/ai/conversations/old/clear", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["conversation_id"], "replacement")
        self.assertEqual(self.service.calls, ["old"])

        status, payload = self.request(
            "/api/ai/conversations/old/clear", {"force": True}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        status, payload = self.request("/api/ai/conversations/a%2Fb/clear", {})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_clear_route_maps_lifecycle_conflict_and_missing_to_http(self) -> None:
        status, payload = self.request("/api/ai/conversations/busy/clear", {})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "conversation_busy")

        status, payload = self.request("/api/ai/conversations/missing/clear", {})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


class AIConversationSwitchFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        static = REPO_ROOT / "tools/acm_agent/web_static"
        cls.html = (static / "index.html").read_text(encoding="utf-8")
        cls.script = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(static.glob("*.js"))
        )

    def test_workbench_switches_by_problem_and_exposes_clear_action(self) -> None:
        self.assertIn('id="ai-chat-clear"', self.html)
        self.assertIn("清除本题对话", self.html)
        self.assertIn("生成AI修改代码", self.html)
        self.assertNotIn("生成修复 Diff", self.html)
        self.assertIn('$("#ai-problem").addEventListener("change"', self.script)
        self.assertIn("function switchAiProblem", self.script)
        self.assertIn("aiConversationProblemKey", self.script)
        self.assertIn("aiOperationIsCurrent", self.script)
        self.assertIn("new AbortController()", self.script)
        self.assertIn("/clear`, { body: {} }", self.script)

if __name__ == "__main__":
    unittest.main()
