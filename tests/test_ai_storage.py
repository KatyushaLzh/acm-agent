from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.ai_reliability import build_ai_outcome

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
    def test_v24_accepts_off_and_preserves_parent_rows_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            connection = sqlite3.connect(path)
            try:
                for version in range(1, 24):
                    connection.executescript(MIGRATIONS[version])
                    connection.execute(f"PRAGMA user_version={version}")
                connection.execute(
                    """INSERT INTO problems(
                           platform,problem_id,tags_json,source_json,updated_at)
                       VALUES('codeforces','1A','[]','{}','2026-08-26T00:00:00+00:00')"""
                )
                connection.execute(
                    """INSERT INTO ai_conversations(
                           id,platform,problem_id,status,created_at,updated_at,
                           reasoning_strength)
                       VALUES('conversation','codeforces','1A','active',
                              '2026-08-26T00:00:00+00:00',
                              '2026-08-26T00:00:00+00:00','auto')"""
                )
                connection.execute(
                    """INSERT INTO ai_runs(
                           id,kind,model,conversation_id,request_summary_json,status,
                           usage_json,error_json,created_at,telemetry_json,
                           estimated_cost_json,governance_json,cache_validation_json,
                           requested_reasoning_strength,resolved_reasoning_strength)
                       VALUES('run','coaching','model','conversation','{}','complete',
                              '{}','{}','2026-08-26T00:00:00+00:00','{}','{}','{}','{}',
                              'auto','auto')"""
                )
                connection.commit()
            finally:
                connection.close()

            with Database(path) as database:
                database.connection.execute(
                    "UPDATE ai_conversations SET reasoning_strength='off' WHERE id='conversation'"
                )
                database.connection.execute(
                    """UPDATE ai_runs
                          SET requested_reasoning_strength='off',
                              resolved_reasoning_strength='off'
                        WHERE id='run'"""
                )
                self.assertEqual(database.ai_run("run")["resolved_reasoning_strength"], "off")
                self.assertEqual(
                    database.ai_conversation("conversation")["reasoning_strength"], "off"
                )
                self.assertEqual(list(database.connection.execute("PRAGMA foreign_key_check")), [])
            self.assertTrue(path.with_name("state.db.v23.bak").exists())

    def test_v22_to_v23_preserves_rows_and_reopen_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            connection = sqlite3.connect(path)
            try:
                for version in range(1, 23):
                    connection.executescript(MIGRATIONS[version])
                    connection.execute(f"PRAGMA user_version={version}")
                connection.execute(
                    """INSERT INTO ai_runs(
                           id,kind,model,request_summary_json,status,usage_json,
                           error_json,created_at,telemetry_json,estimated_cost_json,
                           governance_json,cache_validation_json)
                       VALUES('old','recommendation','model','{}','complete','{}','{}',
                              '2026-08-26T00:00:00+00:00','{}','{}','{}','{}')"""
                )
                connection.execute(
                    """INSERT INTO ai_run_legs(
                           run_id,ordinal,route_kind,status,usage_json,
                           estimated_cost_json)
                       VALUES('old',0,'legacy','complete','{}','{}')"""
                )
                connection.commit()
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                self.assertIsNone(database.ai_run("old")["business_outcome"])
                self.assertEqual(database.ai_run("old")["repair_attempts"], 0)
                self.assertEqual(database.ai_run_legs("old")[0]["purpose"], "legacy")
            with Database(path) as reopened:
                self.assertEqual(
                    reopened.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )

    def test_failed_v23_migration_rolls_back_then_restart_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            connection = sqlite3.connect(path)
            try:
                for version in range(1, 23):
                    connection.executescript(MIGRATIONS[version])
                    connection.execute(f"PRAGMA user_version={version}")
                connection.commit()
            finally:
                connection.close()
            original = MIGRATIONS[23]
            try:
                MIGRATIONS[23] = original + "\nINSERT INTO missing_v23_table VALUES(1);"
                with self.assertRaises(sqlite3.OperationalError):
                    Database(path)
            finally:
                MIGRATIONS[23] = original
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 22)
                self.assertNotIn(
                    "provider_outcome",
                    {row[1] for row in connection.execute("PRAGMA table_info(ai_runs)")},
                )
            finally:
                connection.close()
            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )

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

    def test_v18_adds_unknown_telemetry_without_zero_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            connection = sqlite3.connect(path)
            try:
                for version in range(1, 18):
                    connection.executescript(MIGRATIONS[version])
                    connection.execute(f"PRAGMA user_version = {version}")
                connection.execute(
                    """INSERT INTO ai_runs(
                           id,kind,model,request_summary_json,status,usage_json,
                           error_json,created_at)
                       VALUES('historical','recommendation','deepseek-v4-flash',
                              '{}','failed','{}','{}','2026-08-25T00:00:00+00:00')"""
                )
                connection.commit()
            finally:
                connection.close()

            with Database(path) as database:
                row = database.ai_run("historical")
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                self.assertEqual(json.loads(row["telemetry_json"]), {})
                self.assertEqual(json.loads(row["estimated_cost_json"]), {})

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
                self.assertEqual(SCHEMA_VERSION, 25)
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

    def test_v24_refuses_valid_backup_from_a_different_v23_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "state.db"
            other_path = root / "other.db"

            for database_path, problem_id in (
                (path, "source-problem"),
                (other_path, "other-problem"),
            ):
                connection = sqlite3.connect(database_path)
                try:
                    for version in range(1, 24):
                        connection.executescript(MIGRATIONS[version])
                        connection.execute(f"PRAGMA user_version = {version}")
                    connection.execute(
                        """INSERT INTO problems(
                               platform,problem_id,tags_json,source_json,updated_at)
                           VALUES('codeforces',?,'[]','{}',
                                  '2026-08-27T00:00:00+00:00')""",
                        (problem_id,),
                    )
                    connection.commit()
                finally:
                    connection.close()

            backup = path.with_name("state.db.v23.bak")
            source = sqlite3.connect(other_path)
            destination = sqlite3.connect(backup)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
                source.close()
            original_backup = backup.read_bytes()

            with self.assertRaisesRegex(
                RuntimeError, "backup does not match source database"
            ):
                Database(path)

            self.assertEqual(backup.read_bytes(), original_backup)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 23
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT problem_id FROM problems"
                    ).fetchone()[0],
                    "source-problem",
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
                    SCHEMA_VERSION,
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

    def test_v20_adds_nullable_reasoning_and_conversation_route_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            connection = sqlite3.connect(path)
            try:
                for version in range(1, 20):
                    connection.executescript(MIGRATIONS[version])
                    connection.execute(f"PRAGMA user_version = {version}")
                connection.execute(
                    """INSERT INTO problems(
                           platform,problem_id,tags_json,source_json,updated_at)
                       VALUES('codeforces','1A','[]','{}','2026-08-25T00:00:00+00:00')"""
                )
                connection.execute(
                    """INSERT INTO ai_conversations(
                           id,platform,problem_id,status,created_at,updated_at)
                       VALUES('historical-conversation','codeforces','1A','closed',
                              '2026-08-25T00:00:00+00:00',
                              '2026-08-25T00:00:00+00:00')"""
                )
                connection.execute(
                    """INSERT INTO ai_runs(
                           id,kind,model,request_summary_json,status,usage_json,
                           error_json,created_at)
                       VALUES('historical-run','patch','model','{}','complete','{}','{}',
                              '2026-08-25T00:00:00+00:00')"""
                )
                connection.commit()
            finally:
                connection.close()

            with Database(path) as database:
                run = database.ai_run("historical-run")
                conversation = database.ai_conversation("historical-conversation")
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                self.assertIsNone(run["requested_reasoning_strength"])
                self.assertIsNone(run["resolved_reasoning_strength"])
                for column in (
                    "provider_id", "model", "reasoning_strength",
                    "provider_definition_hash",
                ):
                    self.assertIsNone(conversation[column])

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
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

    def test_outcome_and_leg_purpose_are_persisted_and_audited(self):
        outcome = build_ai_outcome(
            provider_outcome="succeeded",
            artifact_outcome="repaired",
            business_outcome="complete",
            usable=True,
            apply_ready=True,
            degraded=False,
            repair_attempts=1,
        )
        self.db.create_ai_run(
            "outcome-run", kind="summary", model="deepseek-v4-flash",
            status="complete", profile_id="summary", outcome=outcome,
        )
        self.db.update_ai_run(
            "outcome-run",
            governance={
                "legs": [
                    {
                        "provider_id": "deepseek",
                        "model": "deepseek-v4-flash",
                        "resolved_model": "deepseek-v4-flash",
                        "reasoning_strength": "medium",
                        "status": "complete",
                        "usage": {"provider_requests": 1, "total_tokens": 2},
                        "purpose": "validation_repair",
                        "validation_code": "invalid_summary_entry",
                    }
                ]
            },
        )
        row = self.db.ai_run("outcome-run")
        self.assertEqual(row["business_outcome"], "complete")
        self.assertEqual(row["repair_attempts"], 1)
        leg = self.db.ai_run_legs("outcome-run")[0]
        self.assertEqual(leg["purpose"], "validation_repair")
        self.assertEqual(leg["validation_code"], "invalid_summary_entry")
        metrics = self.db.ai_cost_audit()["outcome_metrics"]
        self.assertEqual(metrics["observed_runs"], 1)
        self.assertEqual(metrics["provider_leg_success_rate_percent"], 100.0)
        self.assertEqual(metrics["provider_valid_artifact_rate_percent"], 100.0)
        self.assertEqual(metrics["repair_recovery_rate_percent"], 100.0)
        self.assertEqual(metrics["full_business_success_rate_percent"], 100.0)

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

    def test_conversation_route_is_bound_once(self) -> None:
        conversation, created = self.db.get_or_create_ai_conversation(
            "conversation-route",
            self.attempt_id,
            "codeforces",
            "1A",
            provider_id="relay-a",
            model="shared-model",
            reasoning_strength="medium",
            provider_definition_hash="definition-v1",
        )
        self.assertTrue(created)
        self.assertEqual(conversation["provider_id"], "relay-a")
        self.assertEqual(conversation["model"], "shared-model")
        self.assertEqual(conversation["reasoning_strength"], "medium")
        self.assertEqual(conversation["provider_definition_hash"], "definition-v1")

        same, created = self.db.get_or_create_ai_conversation(
            "ignored",
            self.attempt_id,
            "codeforces",
            "1A",
            provider_id="relay-a",
            model="shared-model",
            reasoning_strength="medium",
            provider_definition_hash="definition-v1",
        )
        self.assertFalse(created)
        self.assertEqual(same["id"], conversation["id"])
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.db.bind_ai_conversation_route(
                conversation["id"], model="different-model"
            )

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
            requested_reasoning_strength="high",
        )
        self.assertEqual(run["status"], "pending")
        run = self.db.update_ai_run(
            "run-1",
            request_summary={"rounds": 2, "accepted_count": 12},
            status="complete",
            finish_reason="stop",
            usage={"total_tokens": 42},
            telemetry={"provider_requests": 2, "protocol_repairs": 1},
            completed_at="2026-08-04T01:02:00+00:00",
            resolved_reasoning_strength="medium",
        )
        self.assertEqual(json.loads(run["usage_json"])["total_tokens"], 42)
        self.assertEqual(json.loads(run["telemetry_json"])["provider_requests"], 2)
        self.assertEqual(
            json.loads(run["estimated_cost_json"])["unknown_reason"],
            "usage_incomplete",
        )
        self.assertEqual(json.loads(run["request_summary_json"])["rounds"], 2)
        self.assertEqual(run["requested_reasoning_strength"], "high")
        self.assertEqual(run["resolved_reasoning_strength"], "medium")

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


class AiCostScopeTests(unittest.TestCase):
    @staticmethod
    def _record_run(
        database: Database,
        run_id: str,
        *,
        provider_id: str,
        model: str,
        usage: dict[str, int],
        legs: list[dict[str, object]],
        route_fallbacks: list[dict[str, object]] | None = None,
        business_fallback: dict[str, object] | None = None,
        created_at: str = "2026-08-26T01:00:00+00:00",
    ) -> None:
        database.create_ai_run(
            run_id,
            kind="recommendation",
            model=model,
            provider_id=provider_id,
            profile_id="recommendation",
            requested_model=model,
            created_at=created_at,
        )
        governance = {
            "version": 1,
            "profile_id": "recommendation",
            "outcome": "complete",
            "actual": {
                "provider_id": str(legs[-1]["provider_id"]),
                "model": str(legs[-1]["resolved_model"]),
            },
            "fallbacks": list(route_fallbacks or []),
            "legs": legs,
        }
        database.update_ai_run(
            run_id,
            status="complete",
            usage=usage,
            resolved_provider_id=str(legs[-1]["provider_id"]),
            resolved_model=str(legs[-1]["resolved_model"]),
            governance=governance,
            fallback=business_fallback,
            completed_at=created_at,
        )

    @staticmethod
    def _leg(
        provider_id: str,
        model: str,
        usage: dict[str, int],
        ordinal: int,
    ) -> dict[str, object]:
        return {
            "ordinal": ordinal,
            "route_kind": "primary" if ordinal == 0 else "fallback",
            "provider_id": provider_id,
            "model": model,
            "resolved_model": model,
            "reasoning_strength": "auto",
            "status": "complete",
            "usage": usage,
        }

    def test_deepseek_cost_is_leg_scoped_while_tokens_cover_all_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Database(Path(temporary) / "state.db") as database:
                deepseek_usage = {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "total_tokens": 110,
                    "cache_read_tokens": 20,
                    "cache_miss_tokens": 80,
                }
                relay_usage = {
                    "input_tokens": 200,
                    "output_tokens": 20,
                    "total_tokens": 220,
                }
                self._record_run(
                    database,
                    "deepseek-only",
                    provider_id="deepseek",
                    model="deepseek-v4-flash",
                    usage=deepseek_usage,
                    legs=[self._leg("deepseek", "deepseek-v4-flash", deepseek_usage, 0)],
                )
                self._record_run(
                    database,
                    "relay-only",
                    provider_id="relay",
                    model="relay-model",
                    usage=relay_usage,
                    legs=[self._leg("relay", "relay-model", relay_usage, 0)],
                )

                mixed_one_usage = {
                    "input_tokens": 300,
                    "output_tokens": 30,
                    "total_tokens": 330,
                    "cache_read_tokens": 60,
                }
                self._record_run(
                    database,
                    "deepseek-to-relay",
                    provider_id="deepseek",
                    model="deepseek-v4-flash",
                    usage=mixed_one_usage,
                    legs=[
                        self._leg(
                            "deepseek",
                            "deepseek-v4-flash",
                            {**deepseek_usage, "input_tokens": 120, "cache_read_tokens": 30, "cache_miss_tokens": 90},
                            0,
                        ),
                        self._leg("relay", "relay-model", relay_usage, 1),
                    ],
                )
                mixed_two_usage = {
                    "input_tokens": 400,
                    "output_tokens": 40,
                    "total_tokens": 440,
                    "cache_read_tokens": 100,
                }
                self._record_run(
                    database,
                    "relay-to-deepseek",
                    provider_id="relay",
                    model="relay-model",
                    usage=mixed_two_usage,
                    legs=[
                        self._leg("relay", "relay-model", relay_usage, 0),
                        self._leg(
                            "deepseek",
                            "deepseek-v4-flash",
                            {**deepseek_usage, "input_tokens": 140, "cache_read_tokens": 40, "cache_miss_tokens": 100},
                            1,
                        ),
                    ],
                )

                audit = database.ai_cost_audit(days=30)
                self.assertEqual(audit["deepseek_cost"]["runs"], 3)
                self.assertEqual(audit["deepseek_cost"]["unknown_cost_runs"], 0)
                self.assertGreater(audit["deepseek_cost"]["known_estimated_cny"], 0)
                self.assertEqual(audit["deepseek_cost"]["currency"], "CNY")
                self.assertEqual(audit["all_model_tokens"]["total_tokens_known"], 1100)
                self.assertEqual(audit["all_model_tokens"]["unknown_runs"], 0)
                self.assertEqual(audit["cache_metrics"]["eligible_input_tokens"], 800)
                self.assertEqual(audit["cache_metrics"]["cache_read_tokens_known"], 180)
                self.assertEqual(audit["cache_metrics"]["hit_rate_percent"], 22.5)
                self.assertEqual(audit["cache_metrics"]["observed_runs"], 3)
                self.assertEqual(audit["cache_metrics"]["unknown_runs"], 1)
                self.assertEqual(audit["totals"]["provider_route_fallbacks"], 2)
                self.assertEqual(audit["totals"]["route_fallbacks"], 2)
                self.assertEqual(audit["totals"]["business_fallbacks"], 0)
                recent = {row["id"]: row for row in audit["recent_runs"]}
                self.assertEqual(
                    recent["relay-only"]["deepseek_cost"]["status"], "out_of_scope"
                )
                self.assertEqual(
                    recent["deepseek-to-relay"]["deepseek_cost"]["status"], "known"
                )
                spend = database.ai_cost_spend(now="2026-08-26T12:00:00+00:00")
                self.assertEqual(spend["daily"]["provider_id"], "deepseek")
                self.assertEqual(spend["daily"]["runs"], 3)
                self.assertEqual(spend["daily"]["unknown_runs"], 0)
                self.assertEqual(
                    spend["daily"]["known_cny"],
                    audit["deepseek_cost"]["known_estimated_cny"],
                )

    def test_provider_route_and_business_fallback_metrics_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Database(Path(temporary) / "state.db") as database:
                failed_usage = {"provider_requests": 1}
                relay_usage = {
                    "provider_requests": 1,
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                }
                primary = self._leg(
                    "deepseek", "deepseek-v4-flash", failed_usage, 0
                )
                primary["status"] = "failed"
                primary["error_code"] = "network_error"
                fallback_leg = self._leg("relay", "relay-model", relay_usage, 1)
                fallback_retry = self._leg("relay", "relay-model", relay_usage, 2)
                route_event = {
                    "from": {
                        "provider_id": "deepseek",
                        "model": "deepseek-v4-flash",
                    },
                    "to": {"provider_id": "relay", "model": "relay-model"},
                    "error_code": "network_error",
                }
                self._record_run(
                    database,
                    "provider-route-fallback",
                    provider_id="deepseek",
                    model="deepseek-v4-flash",
                    usage=relay_usage,
                    legs=[primary, fallback_leg, fallback_retry],
                    route_fallbacks=[route_event],
                    # The storage snapshot mirrors provider routing here; it must
                    # not also become a business fallback.
                    business_fallback={
                        "version": 1,
                        "outcome": "complete",
                        "events": [route_event],
                    },
                )
                self._record_run(
                    database,
                    "business-fallback",
                    provider_id="deepseek",
                    model="deepseek-v4-flash",
                    usage=failed_usage,
                    legs=[primary],
                    business_fallback={
                        "version": 1,
                        "outcome": "deterministic_fallback",
                        "events": [
                            {
                                "error_code": "invalid_ai_ranking",
                                "target": "deterministic_ranking",
                            }
                        ],
                    },
                )

                audit = database.ai_cost_audit(days=30)
                self.assertEqual(audit["totals"]["provider_route_fallbacks"], 1)
                self.assertEqual(audit["totals"]["route_fallbacks"], 1)
                self.assertEqual(audit["totals"]["business_fallbacks"], 1)
                recent = {row["id"]: row for row in audit["recent_runs"]}
                self.assertEqual(recent["provider-route-fallback"]["fallback_count"], 1)
                self.assertEqual(
                    recent["provider-route-fallback"]["provider_route_fallback_count"],
                    1,
                )
                self.assertEqual(
                    recent["provider-route-fallback"]["business_fallback_count"], 0
                )
                self.assertEqual(recent["business-fallback"]["fallback_count"], 0)
                self.assertEqual(
                    recent["business-fallback"]["provider_route_fallback_count"], 0
                )
                self.assertEqual(
                    recent["business-fallback"]["business_fallback_count"], 1
                )

    def test_cache_rate_distinguishes_zero_denominator_missing_and_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with Database(Path(temporary) / "state.db") as database:
                for run_id, usage in (
                    (
                        "zero",
                        {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                            "cache_read_tokens": 0,
                        },
                    ),
                    (
                        "invalid",
                        {
                            "input_tokens": 10,
                            "output_tokens": 1,
                            "total_tokens": 11,
                            "cache_read_tokens": 11,
                        },
                    ),
                    ("missing", {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}),
                ):
                    self._record_run(
                        database,
                        run_id,
                        provider_id="relay",
                        model="relay-model",
                        usage=usage,
                        legs=[self._leg("relay", "relay-model", usage, 0)],
                    )
                metrics = database.ai_cost_audit(days=30)["cache_metrics"]
                self.assertIsNone(metrics["hit_rate_percent"])
                self.assertEqual(metrics["observed_runs"], 1)
                self.assertEqual(metrics["invalid_runs"], 1)
                self.assertEqual(metrics["unknown_runs"], 1)


class ReviewQueueStorageTests(unittest.TestCase):
    @staticmethod
    def _add_problem(
        database: Database,
        platform: str,
        problem_id: str,
        *,
        name: str = "",
    ) -> None:
        database.upsert_problem(
            {
                "platform": platform,
                "problem_id": problem_id,
                "name": name,
                "url": f"https://example.test/{platform}/{problem_id}",
            }
        )

    def test_v25_starts_with_an_empty_queue_and_resets_only_latest_legacy_due(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            connection = sqlite3.connect(path)
            try:
                for version in range(1, 25):
                    connection.executescript(MIGRATIONS[version])
                    connection.execute(f"PRAGMA user_version={version}")
                for platform, problem_id in (
                    ("codeforces", "1A"),
                    ("codeforces", "2A"),
                    ("luogu", "P1001"),
                ):
                    connection.execute(
                        """INSERT INTO problems(
                               platform,problem_id,tags_json,source_json,updated_at)
                           VALUES(?,?, '[]','{}','2026-08-28T00:00:00+00:00')""",
                        (platform, problem_id),
                    )
                connection.executemany(
                    """INSERT INTO attempts(
                           platform,problem_id,started_at,closed_at,result,
                           review_stage,review_due,active)
                       VALUES(?,?,?,?,?,?,?,0)""",
                    (
                        (
                            "codeforces",
                            "1A",
                            "2026-08-20T00:00:00+00:00",
                            "2026-08-20T01:00:00+00:00",
                            "AC",
                            1,
                            "2026-08-27",
                        ),
                        (
                            "codeforces",
                            "2A",
                            "2026-08-20T00:00:00+00:00",
                            "2026-08-20T01:00:00+00:00",
                            "AC",
                            1,
                            "2026-08-27",
                        ),
                        (
                            "codeforces",
                            "2A",
                            "2026-08-21T00:00:00+00:00",
                            "2026-08-21T01:00:00+00:00",
                            "AC",
                            0,
                            None,
                        ),
                        (
                            "luogu",
                            "P1001",
                            "2026-08-22T00:00:00+00:00",
                            "2026-08-22T01:00:00+00:00",
                            "AC",
                            2,
                            "2026-09-21",
                        ),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    25,
                )
                self.assertEqual(database.review_queue_entries(), [])
                self.assertEqual(
                    database.review_reset_at("codeforces", "1A"),
                    "2026-08-20T01:00:00+00:00",
                )
                self.assertEqual(database.review_reset_attempt_id("codeforces", "1A"), 1)
                self.assertIsNone(database.review_reset_at("codeforces", "2A"))
                self.assertEqual(database.review_reset_attempt_id("codeforces", "2A"), 0)
                self.assertEqual(
                    database.review_reset_at("luogu", "P1001"),
                    "2026-08-22T01:00:00+00:00",
                )
                self.assertEqual(database.review_reset_attempt_id("luogu", "P1001"), 4)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM attempts WHERE review_due IS NOT NULL"
                    ).fetchone()[0],
                    3,
                )

            backup = path.with_name("state.db.v24.bak")
            self.assertTrue(backup.exists())
            backup_connection = sqlite3.connect(backup)
            try:
                self.assertEqual(
                    backup_connection.execute("PRAGMA user_version").fetchone()[0],
                    24,
                )
                self.assertIsNone(
                    backup_connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='review_queue'"
                    ).fetchone()
                )
            finally:
                backup_connection.close()

    def test_review_queue_crud_orders_entries_and_preserves_problem_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Database(Path(temporary) / "state.db") as database:
                self._add_problem(database, "codeforces", "2A", name="Second")
                self._add_problem(database, "codeforces", "1A", name="First")
                self._add_problem(database, "luogu", "P1001", name="A+B")

                database.upsert_review_queue(
                    "luogu",
                    "P1001",
                    review_due="2026-09-01",
                    queue_type="manual_once",
                    created_at="2026-08-01T00:00:00+00:00",
                    updated_at="2026-08-01T00:00:00+00:00",
                )
                database.upsert_review_queue(
                    "codeforces",
                    "2A",
                    review_due="2026-08-30",
                    queue_type="automatic",
                    review_stage=2,
                )
                database.upsert_review_queue(
                    "codeforces",
                    "1A",
                    review_due="2026-08-30",
                    queue_type="automatic",
                    review_stage=1,
                )

                rows = database.review_queue_entries()
                self.assertEqual(
                    [(row["platform"], row["problem_id"]) for row in rows],
                    [
                        ("codeforces", "1A"),
                        ("codeforces", "2A"),
                        ("luogu", "P1001"),
                    ],
                )
                self.assertEqual(rows[0]["name"], "First")
                self.assertEqual(rows[0]["url"], "https://example.test/codeforces/1A")

                updated = database.upsert_review_queue(
                    "luogu",
                    "P1001",
                    review_due="2026-08-29",
                    queue_type="manual_once",
                    updated_at="2026-08-02T00:00:00+00:00",
                )
                self.assertEqual(updated["review_due"], "2026-08-29")
                self.assertEqual(updated["created_at"], "2026-08-01T00:00:00+00:00")
                self.assertEqual(updated["updated_at"], "2026-08-02T00:00:00+00:00")
                self.assertIsNone(database.review_reset_at("luogu", "P1001"))

    def test_remove_and_clear_write_reset_cutoffs_without_touching_problems(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Database(Path(temporary) / "state.db") as database:
                for problem_id in ("1A", "2A", "3A"):
                    self._add_problem(database, "codeforces", problem_id)
                    attempt_id = database.start_attempt(
                        "codeforces",
                        problem_id,
                        started_at="2026-08-29T00:00:00+00:00",
                    )
                    database.close_attempt(
                        attempt_id,
                        result="WA",
                        closed_at="2026-08-29T01:00:00+00:00",
                    )
                    database.upsert_review_queue(
                        "codeforces",
                        problem_id,
                        review_due="2026-08-29",
                        queue_type="automatic",
                        review_stage=1,
                    )

                self.assertTrue(
                    database.remove_review_queue(
                        "codeforces",
                        "1A",
                        reset_at="2026-08-29T01:00:00+00:00",
                    )
                )
                self.assertFalse(database.remove_review_queue("codeforces", "missing"))
                self.assertEqual(
                    database.review_reset_at("codeforces", "1A"),
                    "2026-08-29T01:00:00+00:00",
                )
                reset_attempt_id = database.review_reset_attempt_id(
                    "codeforces", "1A"
                )
                self.assertGreater(reset_attempt_id, 0)
                self.assertIsNone(database.review_reset_at("codeforces", "missing"))

                same_second_attempt = database.start_attempt(
                    "codeforces",
                    "1A",
                    started_at="2026-08-29T01:00:00+00:00",
                )
                database.close_attempt(
                    same_second_attempt,
                    result="WA",
                    closed_at="2026-08-29T01:00:00+00:00",
                )
                self.assertGreater(same_second_attempt, reset_attempt_id)

                self.assertEqual(
                    database.clear_review_queue(
                        reset_at="2026-08-29T02:00:00+00:00"
                    ),
                    2,
                )
                self.assertEqual(database.review_queue_entries(), [])
                for problem_id in ("2A", "3A"):
                    self.assertEqual(
                        database.review_reset_at("codeforces", problem_id),
                        "2026-08-29T02:00:00+00:00",
                    )
                self.assertEqual(
                    len(database.problems("codeforces")),
                    3,
                )

    def test_review_queue_enforces_type_stage_invariant(self):
        with tempfile.TemporaryDirectory() as temporary:
            with Database(Path(temporary) / "state.db") as database:
                self._add_problem(database, "codeforces", "1A")
                for queue_type, stage in (("automatic", 0), ("manual_once", 1)):
                    with self.subTest(queue_type=queue_type, stage=stage):
                        with self.assertRaises(sqlite3.IntegrityError):
                            database.upsert_review_queue(
                                "codeforces",
                                "1A",
                                review_due="2026-08-29",
                                queue_type=queue_type,
                                review_stage=stage,
                            )


if __name__ == "__main__":
    unittest.main()
