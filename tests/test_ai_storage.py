from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.storage import (
    Database,
    MIGRATIONS,
    ProblemContextConflict,
    SCHEMA_VERSION,
)


def create_v5_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for version in range(1, 6):
            connection.executescript(MIGRATIONS[version])
            connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


class AiStorageMigrationTests(unittest.TestCase):
    def test_v5_migration_creates_one_consistent_backup_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v5_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """INSERT INTO problems(
                           platform,problem_id,tags_json,source_json,updated_at)
                       VALUES('codeforces','1A','[]','{}','2026-08-04T00:00:00+00:00')"""
                )
                connection.commit()
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(SCHEMA_VERSION, 6)
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    6,
                )
                tables = {
                    row[0]
                    for row in database.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {
                        "problem_contexts",
                        "ai_conversations",
                        "ai_messages",
                        "ai_runs",
                        "ai_patch_proposals",
                    }.issubset(tables)
                )
                self.assertEqual(
                    list(database.connection.execute("PRAGMA foreign_key_check")), []
                )

            backup = path.with_name("state.db.v5.bak")
            self.assertTrue(backup.exists())
            before = backup.read_bytes()
            backup_connection = sqlite3.connect(backup)
            try:
                self.assertEqual(
                    backup_connection.execute("PRAGMA user_version").fetchone()[0], 5
                )
                self.assertEqual(
                    backup_connection.execute(
                        "SELECT COUNT(*) FROM problems WHERE problem_id='1A'"
                    ).fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    backup_connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='ai_runs'"
                    ).fetchone()
                )
            finally:
                backup_connection.close()

            with Database(path):
                pass
            self.assertEqual(backup.read_bytes(), before)

    def test_new_database_does_not_create_upgrade_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            with Database(path):
                pass
            self.assertFalse(path.with_name("state.db.v5.bak").exists())

    def test_failed_v6_migration_rolls_back_and_restart_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v5_database(path)
            original = MIGRATIONS[6]
            try:
                MIGRATIONS[6] = original + "\nINSERT INTO table_that_does_not_exist VALUES(1);"
                with self.assertRaises(sqlite3.OperationalError):
                    Database(path)
            finally:
                MIGRATIONS[6] = original

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
                self.assertIsNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='ai_runs'"
                    ).fetchone()
                )
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    6,
                )


class AiStorageRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.db = Database(self.path)
        self.db.upsert_problem(
            {"platform": "codeforces", "problem_id": "1A", "name": "Theatre Square"}
        )
        self.attempt_id = self.db.start_attempt(
            "codeforces", "1A", started_at="2026-08-04T01:00:00+00:00"
        )

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def test_context_manual_precedence_and_hash_guard(self) -> None:
        automatic = self.db.save_problem_context(
            "codeforces",
            "1A",
            "automatic statement v1",
            source="codeforces_auto",
            source_url="https://codeforces.com/problemset/problem/1/A",
        )
        self.assertEqual(
            self.db.problem_context("codeforces", "1A")["source"],
            "codeforces_auto",
        )

        manual = self.db.save_problem_context(
            "codeforces",
            "1A",
            "manual statement",
            source="manual",
            expected_hash=automatic["content_hash"],
        )
        self.db.save_problem_context(
            "codeforces",
            "1A",
            "automatic statement v2",
            source="codeforces_auto",
        )
        effective = self.db.problem_context("codeforces", "1A")
        self.assertEqual(effective["source"], "manual")
        self.assertEqual(effective["content"], "manual statement")
        self.assertEqual(len(self.db.problem_context_rows("codeforces", "1A")), 2)

        with self.assertRaises(ProblemContextConflict):
            self.db.save_problem_context(
                "codeforces",
                "1A",
                "stale manual edit",
                source="manual",
                expected_hash="stale",
            )
        self.assertTrue(
            self.db.delete_manual_problem_context(
                "codeforces", "1A", expected_hash=manual["content_hash"]
            )
        )
        self.assertEqual(
            self.db.problem_context("codeforces", "1A")["content"],
            "automatic statement v2",
        )

    def test_conversation_messages_and_max_hint(self) -> None:
        conversation, created = self.db.get_or_create_ai_conversation(
            "conversation-1", self.attempt_id, "codeforces", "1A"
        )
        self.assertTrue(created)
        same, created = self.db.get_or_create_ai_conversation(
            "conversation-ignored", self.attempt_id, "codeforces", "1A"
        )
        self.assertFalse(created)
        self.assertEqual(same["id"], conversation["id"])

        self.db.create_ai_message(
            "message-user",
            conversation["id"],
            role="user",
            mode="hint",
            hint_level=2,
            content="help",
            status="complete",
        )
        self.db.create_ai_message(
            "message-assistant",
            conversation["id"],
            role="assistant",
            mode="hint",
            hint_level=2,
            status="streaming",
            model="deepseek-v4-flash",
        )
        self.assertEqual(self.db.max_ai_hint_level(self.attempt_id), 2)
        updated = self.db.update_ai_message(
            "message-assistant",
            content="consider the invariant",
            status="complete",
            usage={"prompt_tokens": 10, "completion_tokens": 4},
            completed_at="2026-08-04T01:01:00+00:00",
        )
        self.assertEqual(json.loads(updated["usage_json"])["completion_tokens"], 4)
        self.assertEqual(self.db.max_ai_hint_level(self.attempt_id), 2)
        self.assertEqual(
            [row["id"] for row in self.db.ai_messages(conversation["id"])],
            ["message-user", "message-assistant"],
        )
        self.assertTrue(self.db.close_ai_conversation(conversation["id"]))

    def test_ai_run_and_patch_lifecycle(self) -> None:
        conversation, _ = self.db.get_or_create_ai_conversation(
            "conversation-1", self.attempt_id, "codeforces", "1A"
        )
        run = self.db.create_ai_run(
            "run-1",
            kind="patch",
            model="deepseek-v4-flash",
            conversation_id=conversation["id"],
            request_summary={"problem": "codeforces:1A"},
        )
        self.assertEqual(run["status"], "pending")
        run = self.db.update_ai_run(
            "run-1",
            status="complete",
            finish_reason="stop",
            usage={"total_tokens": 42},
            completed_at="2026-08-04T01:02:00+00:00",
        )
        self.assertEqual(json.loads(run["usage_json"])["total_tokens"], 42)

        proposal = self.db.create_ai_patch_proposal(
            "patch-1",
            run_id=run["id"],
            conversation_id=conversation["id"],
            attempt_id=self.attempt_id,
            platform="codeforces",
            problem_id="1A",
            source_path="2026/8/04/CF1A.cpp",
            baseline_hash="old-hash",
            candidate_code="int main() {}\n",
            diff_text="--- old\n+++ new\n",
            diagnosis="missing implementation",
        )
        self.assertEqual(proposal["status"], "preview")
        proposal = self.db.update_ai_patch_proposal(
            "patch-1",
            status="applied",
            applied_hash="new-hash",
            backup_path=".acm/ai-backups/patch-1.cpp",
            verify={"ok": True},
            applied_at="2026-08-04T01:03:00+00:00",
        )
        self.assertEqual(proposal["status"], "applied")
        self.assertTrue(json.loads(proposal["verify_json"])["ok"])
        proposal = self.db.update_ai_patch_proposal(
            "patch-1",
            status="reverted",
            reverted_at="2026-08-04T01:04:00+00:00",
        )
        self.assertEqual(proposal["status"], "reverted")


if __name__ == "__main__":
    unittest.main()
