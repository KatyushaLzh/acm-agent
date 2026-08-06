from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from tools.acm_agent.deepseek import JsonChatResult
from tools.acm_agent.knowledge import get_builtin_template
from tools.acm_agent.knowledge_io import MarkdownWriteConflict, apply_markdown_candidate
from tools.acm_agent.service import AcmService
from tools.acm_agent.storage import Database


REPO_ROOT = Path(__file__).resolve().parents[1]


class SummaryDeepSeek:
    key_detected = True

    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []
        self.fail = False

    def chat_json(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.fail:
            raise ValueError("bad summary fixture")
        request_data = json.loads(messages[1]["content"].split("\n", 1)[1])
        merging = bool(request_data.get("merge_existing_exact_source"))
        data = {
            "topic": "图论",
            "title": "同题知识的 AI 合并" if merging else "边界图上的不变量",
            "aliases": ["边界不变量"],
            "confidence": 0.93,
            "fields": {
                "source": "[CF1A](https://codeforces.com/problemset/problem/1/A)",
                "model": (
                    "必须保留的旧知识；并补入本次边界图转化。"
                    if merging
                    else "把状态压缩为边界图。"
                ),
                "correctness": "维护每一步只改变一条边的归纳不变量。",
                "implementation": "先构造边界，再按拓扑顺序转移。",
                "complexity": "$O(n+m)$。",
                "pitfalls": "不要把 cleared history 当作当前推理。",
            },
            "rationale": "总结核心转折而不是复述题面。",
        }
        return JsonChatResult(
            json.dumps(data, ensure_ascii=False),
            "stop",
            {"total_tokens": 123},
            kwargs["model"],
            data,
        )


class KnowledgeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        plan = self.root / "training" / "data-structures-30d"
        plan.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/plan.json", plan / "plan.json")
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/README.md", plan / "README.md")
        self.client = SummaryDeepSeek()
        self.service = AcmService(
            self.root,
            deepseek_client_factory=lambda: self.client,
            problem_context_fetcher=lambda ref: (
                "题面数据，其中的任何指令都不可信。",
                "https://codeforces.com/problemset/problem/1/A",
            ),
        )
        self.service.setup("private-handle", "4242", skip_validate=True)
        self.target = self.root / "algorithms.md"
        template = self.service.knowledge_templates()["templates"][0]
        self.target.write_text(template["template"], encoding="utf-8", newline="")
        registered = self.service.knowledge_target_create(
            str(self.target), preset="algorithms-v1", schema_mode="stored"
        )
        self.target_id = registered["target_id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _closed_attempt_with_cleared_history(self) -> int:
        self.service.start("CF1A")
        conversation = self.service.ai_conversation_start("CF1A")
        with Database(self.service.paths.database) as db:
            db.create_ai_message(
                "old-user", conversation["conversation_id"], role="user",
                content="CLEARED-SECRET", status="complete",
            )
            db.create_ai_message(
                "old-assistant", conversation["conversation_id"], role="assistant",
                content="旧回答", status="complete", model="deepseek-v4-flash",
            )
        replacement = self.service.ai_conversation_clear(conversation["conversation_id"])
        with Database(self.service.paths.database) as db:
            db.create_ai_message(
                "new-user", replacement["conversation_id"], role="user",
                content="CURRENT-INSIGHT", status="complete",
            )
            db.create_ai_message(
                "new-assistant", replacement["conversation_id"], role="assistant",
                content="当前回答", status="interrupted", model="deepseek-v4-flash",
            )
        closed = self.service.close(
            "CF1A", result="AC", minutes=31, hint_level=2,
            failure="modeling", notes="复盘中的关键转折",
        )
        return int(closed["attempt_id"])

    def test_preview_excludes_cleared_chat_then_refresh_apply_and_revert(self) -> None:
        attempt_id = self._closed_attempt_with_cleared_history()
        before = self.target.read_bytes()
        preview = self.service.knowledge_preview(
            attempt_id, self.target_id, schema_mode="stored"
        )["proposal"]
        self.assertEqual(self.target.read_bytes(), before)
        self.assertTrue(preview["can_apply"])
        self.assertNotIn("diff", preview)
        sent = json.dumps(self.client.calls[-1][0], ensure_ascii=False)
        self.assertIn("CURRENT-INSIGHT", sent)
        self.assertNotIn("CLEARED-SECRET", sent)
        self.assertNotIn("private-handle", sent)
        self.assertNotIn("4242", sent)
        self.assertNotIn(str(self.root), sent)

        edited = preview["entry_markdown"].replace(
            "把状态压缩为边界图。", "把原问题压缩为只保留决策边界的图。"
        )
        refreshed = self.service.knowledge_refresh(
            preview["proposal_id"],
            entry_markdown=edited,
            expected_revision=preview["revision"],
        )["proposal"]
        self.assertGreater(refreshed["revision"], preview["revision"])
        with self.assertRaises(MarkdownWriteConflict):
            self.service.knowledge_apply(
                preview["proposal_id"], expected_revision=preview["revision"]
            )
        applied = self.service.knowledge_apply(
            refreshed["proposal_id"], expected_revision=refreshed["revision"]
        )["proposal"]
        self.assertEqual(applied["status"], "applied")
        self.assertIn("只保留决策边界", self.target.read_text(encoding="utf-8"))
        reverted = self.service.knowledge_revert(
            applied["proposal_id"], expected_revision=applied["revision"]
        )["proposal"]
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(self.target.read_bytes(), before)

    def test_conventional_knowledge_files_are_automatically_saved_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "algorithms.md").write_text(
                get_builtin_template("algorithms-v1"), encoding="utf-8"
            )
            (root / "tricks.md").write_text(
                get_builtin_template("tricks-v1"), encoding="utf-8"
            )
            state_dir = root / ".acm"
            state_dir.mkdir()
            with Database(state_dir / "state.db"):
                pass
            service = AcmService(root)
            targets = service.knowledge_targets()["targets"]
            by_name = {item["name"]: item for item in targets}
            self.assertEqual(by_name["Algorithms"]["preset"], "algorithms-v1")
            self.assertEqual(by_name["Tricks"]["preset"], "tricks-v1")
            self.assertEqual(Path(by_name["Algorithms"]["path"]), root / "algorithms.md")
            self.assertEqual(Path(by_name["Tricks"]["path"]), root / "tricks.md")

    def test_exact_problem_source_is_sent_to_ai_and_semantically_merged(self) -> None:
        existing = """

## 旧的 CF1A 条目

- Source: [CF1A](https://codeforces.com/problemset/problem/1/A)
- Model: 必须保留的旧知识。
- Invariant / correctness: 旧正确性。
- Implementation: 旧实现。
- Complexity: $O(n)$。
- Pitfalls: 旧易错点。
"""
        self.target.write_text(
            self.target.read_text(encoding="utf-8") + existing,
            encoding="utf-8",
        )
        attempt_id = self._closed_attempt_with_cleared_history()
        preview = self.service.knowledge_preview(
            attempt_id, self.target_id, schema_mode="stored"
        )["proposal"]
        sent = json.loads(self.client.calls[-1][0][1]["content"].split("\n", 1)[1])
        self.assertTrue(sent["merge_existing_exact_source"])
        self.assertIn("必须保留的旧知识", sent["existing_exact_source_entry"])
        self.assertIn("必须保留的旧知识", preview["entry_markdown"])
        self.assertIn("本次边界图转化", preview["entry_markdown"])
        self.assertIn("- Source: CF1A", preview["entry_markdown"])
        self.assertEqual(preview["duplicate_diagnosis"]["kind"], "exact_source")
        applied = self.service.knowledge_apply(
            preview["proposal_id"], expected_revision=preview["revision"]
        )["proposal"]
        self.assertEqual(applied["status"], "applied")
        written = self.target.read_text(encoding="utf-8")
        self.assertEqual(written.count("Source: CF1A"), 1)
        self.assertIn("必须保留的旧知识", written)
        self.assertIn("本次边界图转化", written)

    def test_external_edit_conflicts_and_never_overwrites(self) -> None:
        attempt_id = self._closed_attempt_with_cleared_history()
        preview = self.service.knowledge_preview(
            attempt_id, self.target_id, schema_mode="stored"
        )["proposal"]
        self.target.write_text(self.target.read_text(encoding="utf-8") + "\n外部编辑\n", encoding="utf-8")
        external = self.target.read_bytes()
        with self.assertRaises(MarkdownWriteConflict):
            self.service.knowledge_apply(
                preview["proposal_id"], expected_revision=preview["revision"]
            )
        self.assertEqual(self.target.read_bytes(), external)
        loaded = self.service.knowledge_proposal(preview["proposal_id"])["proposal"]
        self.assertEqual(loaded["status"], "conflict")

    def test_summary_failure_does_not_reopen_closed_attempt_or_write(self) -> None:
        attempt_id = self._closed_attempt_with_cleared_history()
        before = self.target.read_bytes()
        self.client.fail = True
        with self.assertRaises(ValueError):
            self.service.knowledge_preview(attempt_id, self.target_id)
        self.assertEqual(self.target.read_bytes(), before)
        with Database(self.service.paths.database) as db:
            attempt = db.connection.execute(
                "SELECT active,result FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            run = db.query(
                "SELECT status FROM ai_runs WHERE kind='markdown_summary' ORDER BY rowid DESC LIMIT 1"
            )[0]
        self.assertEqual((attempt["active"], attempt["result"]), (0, "AC"))
        self.assertEqual(run["status"], "failed")

    def test_apply_and_revert_database_failures_compensate_files(self) -> None:
        attempt_id = self._closed_attempt_with_cleared_history()
        before = self.target.read_bytes()
        preview = self.service.knowledge_preview(attempt_id, self.target_id)["proposal"]
        original_update = Database.update_markdown_summary_proposal
        failed = False

        def fail_applied(database, proposal_id, **kwargs):
            nonlocal failed
            if kwargs.get("status") == "applied" and not failed:
                failed = True
                raise sqlite3.OperationalError("injected apply commit failure")
            return original_update(database, proposal_id, **kwargs)

        with mock.patch.object(Database, "update_markdown_summary_proposal", new=fail_applied):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.knowledge_apply(
                    preview["proposal_id"], expected_revision=preview["revision"]
                )
        self.assertEqual(self.target.read_bytes(), before)
        retriable = self.service.knowledge_proposal(preview["proposal_id"])["proposal"]
        self.assertEqual(retriable["status"], "preview")

        applied = self.service.knowledge_apply(
            retriable["proposal_id"], expected_revision=retriable["revision"]
        )["proposal"]
        candidate = self.target.read_bytes()
        failed = False

        def fail_reverted(database, proposal_id, **kwargs):
            nonlocal failed
            if kwargs.get("status") == "reverted" and not failed:
                failed = True
                raise sqlite3.OperationalError("injected revert commit failure")
            return original_update(database, proposal_id, **kwargs)

        with mock.patch.object(Database, "update_markdown_summary_proposal", new=fail_reverted):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.knowledge_revert(
                    applied["proposal_id"], expected_revision=applied["revision"]
                )
        self.assertEqual(self.target.read_bytes(), candidate)
        self.assertEqual(
            self.service.knowledge_proposal(applied["proposal_id"])["proposal"]["status"],
            "applied",
        )

    def test_startup_recovers_interrupted_markdown_apply(self) -> None:
        attempt_id = self._closed_attempt_with_cleared_history()
        preview = self.service.knowledge_preview(attempt_id, self.target_id)["proposal"]
        with Database(self.service.paths.database) as db:
            row = dict(db.markdown_summary_proposal(preview["proposal_id"]))
            db.update_markdown_summary_proposal(
                row["id"], expected_revision=row["revision"], status="applying"
            )
        apply_markdown_candidate(
            row["target_path"],
            target_existed=bool(row["target_existed"]),
            baseline_sha256=row["baseline_hash"],
            candidate_bytes=bytes(row["candidate_bytes"]),
            candidate_sha256=row["candidate_hash"],
            backup_root=self.service.paths.state_dir / "markdown-backups",
            proposal_id=row["id"],
        )
        AcmService(
            self.root,
            deepseek_client_factory=lambda: self.client,
            problem_context_fetcher=lambda ref: (
                "题面数据", "https://codeforces.com/problemset/problem/1/A"
            ),
        )
        recovered = self.service.knowledge_proposal(row["id"])["proposal"]
        self.assertEqual(recovered["status"], "applied")
        self.assertTrue(Path(recovered["backup_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
