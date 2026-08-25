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


STRESS_TABLES = (
    "stress_artifact_bundles",
    "stress_artifacts",
    "stress_runs",
    "stress_preparation_cache",
    "stress_artifact_candidates",
    "stress_artifact_proofs",
    "stress_bundle_certifications",
    "stress_cache_aliases",
)


def create_legacy_database(path: Path, version: int) -> None:
    """Build the relevant shape of a pre-retirement schema."""

    connection = sqlite3.connect(path)
    try:
        for migration_version in range(1, min(version, 7) + 1):
            connection.executescript(MIGRATIONS[migration_version])
        if version >= 8:
            for table in STRESS_TABLES[:3]:
                connection.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
                connection.execute(
                    f"INSERT INTO {table}(id) VALUES(?)", (f"legacy-{table}",)
                )
        if version >= 11:
            connection.execute(
                "ALTER TABLE ai_runs ADD COLUMN preparation_meta_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )
            connection.execute(
                "CREATE TABLE stress_preparation_cache(id TEXT PRIMARY KEY)"
            )
            connection.execute(
                "INSERT INTO stress_preparation_cache(id) VALUES('legacy-preparation')"
            )
            connection.execute(
                "CREATE UNIQUE INDEX ai_runs_single_running_stress_setup_idx "
                "ON ai_runs((1)) WHERE kind='stress_setup' AND status='running'"
            )
        if version >= 12:
            connection.executescript(MIGRATIONS[12])
            for table in STRESS_TABLES[4:]:
                connection.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
                connection.execute(
                    f"INSERT INTO {table}(id) VALUES(?)", (f"legacy-{table}",)
                )

        connection.execute(
            """INSERT INTO problems(
                   platform,problem_id,tags_json,source_json,updated_at)
               VALUES('codeforces','1A','[]','{}','2026-08-24T00:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO ai_runs(
                   id,kind,model,request_summary_json,status,usage_json,
                   error_json,created_at)
               VALUES('keep-run','patch','model','{}','complete','{}','{}',
                      '2026-08-24T00:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO ai_runs(
                   id,kind,model,request_summary_json,status,usage_json,
                   error_json,created_at)
               VALUES('remove-run','stress_setup','model','{}','running','{}','{}',
                      '2026-08-24T00:00:01+00:00')"""
        )
        if version >= 12:
            connection.execute(
                """INSERT INTO problem_samples(
                       platform,problem_id,sample_key,input_data,expected_output,
                       content_hash,source,metadata_json,created_at,updated_at)
                   VALUES('codeforces','1A','sample1',X'31',X'32','hash',
                          'problem_context','{}','2026-08-24T00:00:00+00:00',
                          '2026-08-24T00:00:00+00:00')"""
            )
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


class AiStorageMigrationTests(unittest.TestCase):
    def assert_v16_retirement_backup(self, backup: Path) -> None:
        self.assertTrue(backup.exists())
        connection = sqlite3.connect(backup)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 16)
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(set(STRESS_TABLES).issubset(names))
            for table in STRESS_TABLES:
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    1,
                )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_runs WHERE kind='stress_setup'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

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
                self.assertEqual(SCHEMA_VERSION, 17)
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
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
                    SCHEMA_VERSION,
                )

    def test_v17_retires_stress_schema_and_preserves_other_data(self) -> None:
        for version in (7, 8, 10, 12, 16):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "state.db"
                create_legacy_database(path, version)
                with Database(path) as database:
                    objects = {
                        row[0]
                        for row in database.connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type IN ('table','index')"
                        )
                    }
                    self.assertTrue(set(STRESS_TABLES).isdisjoint(objects))
                    self.assertNotIn(
                        "ai_runs_single_running_stress_setup_idx", objects
                    )
                    self.assertEqual(
                        [row[0] for row in database.connection.execute(
                            "SELECT id FROM ai_runs ORDER BY id"
                        )],
                        ["keep-run"],
                    )
                    ai_columns = {
                        row[1]
                        for row in database.connection.execute(
                            "PRAGMA table_info(ai_runs)"
                        )
                    }
                    self.assertNotIn("preparation_meta_json", ai_columns)
                    if version >= 12:
                        self.assertEqual(
                            database.connection.execute(
                                "SELECT COUNT(*) FROM problem_samples"
                            ).fetchone()[0],
                            1,
                        )
                    self.assertEqual(
                        list(database.connection.execute("PRAGMA foreign_key_check")),
                        [],
                    )

    def test_v17_creates_idempotent_v16_backup_for_direct_and_intermediate_upgrade(
        self,
    ) -> None:
        for source_version in (12, 16):
            with (
                self.subTest(source_version=source_version),
                tempfile.TemporaryDirectory() as temp,
            ):
                path = Path(temp) / "state.db"
                create_legacy_database(path, source_version)

                with Database(path):
                    pass

                backup = path.with_name("state.db.v16.bak")
                self.assert_v16_retirement_backup(backup)
                original_backup = backup.read_bytes()

                with Database(path):
                    pass
                self.assertEqual(backup.read_bytes(), original_backup)

    def test_v17_refuses_to_overwrite_an_unreadable_existing_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_legacy_database(path, 16)
            backup = path.with_name("state.db.v16.bak")
            sentinel = b"existing backup must not be overwritten"
            backup.write_bytes(sentinel)

            with self.assertRaisesRegex(RuntimeError, "backup is not readable"):
                Database(path)

            self.assertEqual(backup.read_bytes(), sentinel)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 16)
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM ai_runs WHERE kind='stress_setup'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_failed_v17_retirement_rolls_back_all_destructive_ddl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_legacy_database(path, 16)
            original = MIGRATIONS[17]
            try:
                MIGRATIONS[17] = (
                    original + "\nINSERT INTO table_that_does_not_exist VALUES(1);"
                )
                with self.assertRaises(sqlite3.OperationalError):
                    Database(path)
            finally:
                MIGRATIONS[17] = original

            backup = path.with_name("state.db.v16.bak")
            self.assert_v16_retirement_backup(backup)
            original_backup = backup.read_bytes()

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 16)
                names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(set(STRESS_TABLES).issubset(names))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0], 2)
                self.assertIn(
                    "preparation_meta_json",
                    {row[1] for row in connection.execute("PRAGMA table_info(ai_runs)")},
                )
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    17,
                )
            self.assertEqual(backup.read_bytes(), original_backup)

    def test_fresh_schema_contains_only_problem_samples_from_v8_to_v16(self) -> None:
        self.assertEqual(SCHEMA_VERSION, max(MIGRATIONS))
        for version in (*range(8, 12), *range(13, 17)):
            self.assertEqual(MIGRATIONS[version], "")
        self.assertNotIn("stress_", MIGRATIONS[12])

        with tempfile.TemporaryDirectory() as temp:
            with Database(Path(temp) / "state.db") as database:
                names = {
                    row[0]
                    for row in database.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("problem_samples", names)
                self.assertTrue(set(STRESS_TABLES).isdisjoint(names))
                self.assertNotIn(
                    "preparation_meta_json",
                    {
                        row[1]
                        for row in database.connection.execute(
                            "PRAGMA table_info(ai_runs)"
                        )
                    },
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

    def test_problem_samples_deduplicate_content_and_merge_metadata(self) -> None:
        first = self.db.upsert_problem_sample(
            "codeforces",
            "1A",
            "sample1",
            input_data="1 2\n",
            expected_output="3\n",
            metadata={"parser": {"version": 1}},
        )
        duplicate = self.db.upsert_problem_sample(
            "codeforces",
            "1A",
            "renamed",
            input_data=b"1 2\n",
            expected_output=b"3\n",
            metadata={"parser": {"confidence": 1.0}},
        )
        self.assertEqual(first["id"], duplicate["id"])
        rows = self.db.problem_samples("codeforces", "1A")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            json.loads(rows[0]["metadata_json"])["parser"],
            {"confidence": 1.0, "version": 1},
        )

    def test_replace_problem_samples_is_source_scoped_and_atomic(self) -> None:
        self.db.upsert_problem_sample(
            "codeforces",
            "1A",
            "manual",
            input_data="9\n",
            expected_output="9\n",
            source="manual",
        )
        rows = self.db.replace_problem_samples(
            "codeforces",
            "1A",
            [
                {"name": "sample1", "input": "1\n", "output": "1\n"},
                {"name": "sample2", "input": "2\n", "output": "2\n"},
            ],
        )
        self.assertEqual({row["source"] for row in rows}, {"manual", "problem_context"})

        with self.assertRaises(TypeError):
            self.db.replace_problem_samples(
                "codeforces", "1A", [{"name": "replacement"}, object()]
            )
        rows = self.db.problem_samples("codeforces", "1A")
        self.assertEqual([row["sample_key"] for row in rows], ["manual", "sample1", "sample2"])

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
            request_summary={"rounds": 2, "accepted_count": 12},
            status="complete",
            finish_reason="stop",
            usage={"total_tokens": 42},
            completed_at="2026-08-04T01:02:00+00:00",
        )
        self.assertEqual(json.loads(run["usage_json"])["total_tokens"], 42)
        self.assertEqual(json.loads(run["request_summary_json"])["rounds"], 2)

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
