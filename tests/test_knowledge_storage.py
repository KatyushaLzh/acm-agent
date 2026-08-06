from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.config import CONFIG_VERSION, Paths, load_config
from tools.acm_agent.storage import (
    Database,
    MIGRATIONS,
    MarkdownSummaryProposalRevisionConflict,
    MarkdownSummaryTargetRevisionConflict,
    SCHEMA_VERSION,
)


def create_v6_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for version in range(1, 7):
            connection.executescript(MIGRATIONS[version])
            connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


class SummaryConfigMigrationTests(unittest.TestCase):
    def test_v2_summary_settings_inherit_existing_coaching_choices(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = Paths.for_root(Path(temp))
            paths.ensure()
            paths.config.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "accounts": {},
                        "ai": {
                            "coaching_model": "deepseek-v4-pro",
                            "coaching_thinking": False,
                            "reasoning_effort": "max",
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(paths)

            self.assertEqual(CONFIG_VERSION, 7)
            self.assertEqual(config["version"], 7)
            self.assertEqual(config["ai"]["summary_model"], "deepseek-v4-pro")
            self.assertFalse(config["ai"]["summary_thinking"])
            self.assertEqual(config["ai"]["summary_reasoning_effort"], "max")

    def test_new_config_defaults_summary_to_flash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(Paths.for_root(Path(temp)), required=False)
            self.assertEqual(config["ai"]["summary_model"], "deepseek-v4-flash")
            self.assertTrue(config["ai"]["summary_thinking"])
            self.assertEqual(config["ai"]["summary_reasoning_effort"], "high")


class KnowledgeStorageMigrationTests(unittest.TestCase):
    def test_v6_migration_backs_up_once_backfills_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v6_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """INSERT INTO problems(
                           platform,problem_id,tags_json,source_json,updated_at)
                       VALUES('codeforces','1A','[]','{}','2026-08-05T00:00:00+00:00')"""
                )
                attempt_id = connection.execute(
                    """INSERT INTO attempts(
                           platform,problem_id,started_at,closed_at,result,active)
                       VALUES('codeforces','1A','2026-08-05T00:00:00+00:00',
                              '2026-08-05T00:30:00+00:00','AC',0)"""
                ).lastrowid
                connection.execute(
                    """INSERT INTO ai_conversations(
                           id,attempt_id,platform,problem_id,status,created_at,
                           updated_at,closed_at)
                       VALUES('legacy-conversation',?,'codeforces','1A','closed',
                              '2026-08-05T00:05:00+00:00',
                              '2026-08-05T00:20:00+00:00',
                              '2026-08-05T00:20:00+00:00')""",
                    (attempt_id,),
                )
                connection.commit()
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(SCHEMA_VERSION, 12)
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    12,
                )
                self.assertEqual(
                    database.ai_conversation("legacy-conversation")["closed_reason"],
                    "legacy",
                )
                tables = {
                    row[0]
                    for row in database.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {
                        "markdown_summary_targets",
                        "markdown_summary_proposals",
                    }.issubset(tables)
                )
                self.assertEqual(
                    list(database.connection.execute("PRAGMA foreign_key_check")), []
                )

            backup = path.with_name("state.db.v6.bak")
            self.assertTrue(backup.is_file())
            backup_before = backup.read_bytes()
            backup_connection = sqlite3.connect(backup)
            try:
                self.assertEqual(
                    backup_connection.execute("PRAGMA user_version").fetchone()[0], 6
                )
                self.assertIsNone(
                    backup_connection.execute(
                        """SELECT name FROM sqlite_master
                           WHERE name='markdown_summary_targets'"""
                    ).fetchone()
                )
                columns = {
                    row[1]
                    for row in backup_connection.execute(
                        "PRAGMA table_info(ai_conversations)"
                    )
                }
                self.assertNotIn("closed_reason", columns)
            finally:
                backup_connection.close()

            with Database(path):
                pass
            self.assertEqual(backup.read_bytes(), backup_before)

    def test_new_database_does_not_create_v6_upgrade_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            with Database(path):
                pass
            self.assertFalse(path.with_name("state.db.v6.bak").exists())

    def test_failed_v7_migration_rolls_back_and_restart_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v6_database(path)
            original = MIGRATIONS[7]
            try:
                MIGRATIONS[7] = (
                    original + "\nINSERT INTO table_that_does_not_exist VALUES(1);"
                )
                with self.assertRaises(sqlite3.OperationalError):
                    Database(path)
            finally:
                MIGRATIONS[7] = original

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertIsNone(
                    connection.execute(
                        """SELECT name FROM sqlite_master
                           WHERE name='markdown_summary_targets'"""
                    ).fetchone()
                )
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0], 12
                )


class KnowledgeStorageRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.db"
        self.db = Database(self.path)
        self.attempt_id = self.db.start_attempt("codeforces", "1A")

    def tearDown(self) -> None:
        self.db.close()
        self.temporary.cleanup()

    def test_summary_conversation_excludes_user_cleared_history(self) -> None:
        old, _ = self.db.get_or_create_ai_conversation(
            "old", self.attempt_id, "codeforces", "1A"
        )
        self.assertTrue(
            self.db.close_ai_conversation(
                old["id"], closed_reason="user_cleared", superseded_by="current"
            )
        )
        current, created = self.db.get_or_create_ai_conversation(
            "current", self.attempt_id, "codeforces", "1A"
        )
        self.assertTrue(created)
        self.assertEqual(old["id"], "old")
        self.assertEqual(
            self.db.ai_conversation("old")["superseded_by"], "current"
        )
        self.assertEqual(
            self.db.latest_summary_ai_conversation(self.attempt_id)["id"], "current"
        )
        self.assertTrue(self.db.close_ai_conversation(current["id"]))
        self.assertEqual(
            self.db.ai_conversation("current")["closed_reason"], "attempt_closed"
        )
        self.assertEqual(
            self.db.latest_summary_ai_conversation(self.attempt_id)["id"], "current"
        )

    def test_target_crud_uses_optimistic_revision_and_keeps_proposal_audit(self) -> None:
        schema = {"version": "summary-schema-v1", "fields": [{"key": "model"}]}
        target = self.db.create_markdown_summary_target(
            "target-1",
            name="Algorithms",
            path="D:/notes/algorithms.md",
            preset="algorithms-v1",
            schema=schema,
        )
        self.assertEqual(target["revision"], 1)
        self.assertEqual(
            target["schema_hash"],
            hashlib.sha256(target["schema_json"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            self.db.markdown_summary_target_by_path("D:/notes/algorithms.md")["id"],
            "target-1",
        )

        target = self.db.update_markdown_summary_target(
            "target-1", expected_revision=1, name="算法卡片", enabled=False
        )
        self.assertEqual(target["revision"], 2)
        self.assertFalse(target["enabled"])
        with self.assertRaises(MarkdownSummaryTargetRevisionConflict):
            self.db.update_markdown_summary_target(
                "target-1", expected_revision=1, name="stale"
            )
        self.assertEqual(len(self.db.markdown_summary_targets(enabled=False)), 1)

        proposal = self.db.create_markdown_summary_proposal(
            "proposal-1",
            attempt_id=self.attempt_id,
            target_id="target-1",
            target_revision=2,
            target_path=target["path"],
            target_existed=True,
            baseline_hash="baseline",
            schema=schema,
            entry={"topic": "数学", "title": "快速幂"},
            entry_markdown="## 快速幂\n",
            candidate_bytes=b"# TOC\r\n\r\n[TOC]\r\n",
            diff_text="--- before\n+++ after\n",
            confidence=0.9,
            warnings=["possible duplicate"],
            duplicate={"kind": "fuzzy"},
            rationale="可复用模型",
        )
        self.assertEqual(proposal["status"], "preview")
        self.assertEqual(proposal["revision"], 1)
        self.assertEqual(bytes(proposal["candidate_bytes"]), b"# TOC\r\n\r\n[TOC]\r\n")

        replacement = b"# TOC\r\n\r\n[TOC]\r\n\r\n# math\r\n"
        proposal = self.db.update_markdown_summary_proposal(
            "proposal-1",
            expected_revision=1,
            entry={"topic": "数学", "title": "快速幂", "fields": {}},
            candidate_bytes=replacement,
            diff_text="updated diff",
        )
        self.assertEqual(proposal["revision"], 2)
        self.assertEqual(
            proposal["candidate_hash"], hashlib.sha256(replacement).hexdigest()
        )
        with self.assertRaises(MarkdownSummaryProposalRevisionConflict):
            self.db.update_markdown_summary_proposal(
                "proposal-1", expected_revision=1, status="applying"
            )
        proposal = self.db.update_markdown_summary_proposal(
            "proposal-1",
            expected_revision=2,
            status="applied",
            applied_hash=proposal["candidate_hash"],
            backup_path=".acm/markdown-backups/proposal-1/original.md",
            applied_at="2026-08-05T01:00:00+00:00",
        )
        self.assertEqual(proposal["revision"], 3)
        self.assertEqual(
            self.db.markdown_summary_proposals(
                attempt_id=self.attempt_id, status="applied"
            )[0]["id"],
            "proposal-1",
        )

        self.db.delete_markdown_summary_target("target-1", expected_revision=2)
        proposal = self.db.markdown_summary_proposal("proposal-1")
        self.assertIsNone(proposal["target_id"])
        self.assertEqual(proposal["target_path"], "D:/notes/algorithms.md")


if __name__ == "__main__":
    unittest.main()
