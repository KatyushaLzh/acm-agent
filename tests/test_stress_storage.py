from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from tools.acm_agent.storage import (
    Database,
    MIGRATIONS,
    SCHEMA_VERSION,
    StressArtifactCandidateConflict,
    StressArtifactProofConflict,
    StressPreparationCacheConflict,
    StressArtifactBundleRevisionConflict,
    StressBundleCertificationConflict,
    StressCacheAliasRevisionConflict,
    StressRunRevisionConflict,
    StressSetupSlotConflict,
)


def create_v7_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for version in range(1, 8):
            connection.executescript(MIGRATIONS[version])
            connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


def create_v8_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for version in range(1, 9):
            connection.executescript(MIGRATIONS[version])
            connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


def create_v9_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for version in range(1, 10):
            connection.executescript(MIGRATIONS[version])
            connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


def create_v10_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for version in range(1, 11):
            connection.executescript(MIGRATIONS[version])
            connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


def create_v11_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for version in range(1, 12):
            connection.executescript(MIGRATIONS[version])
            connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


class StressStorageMigrationTests(unittest.TestCase):
    def test_v7_migration_backs_up_once_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v7_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """INSERT INTO problems(
                           platform,problem_id,tags_json,source_json,updated_at)
                       VALUES('codeforces','1A','[]','{}','2026-08-05T00:00:00+00:00')"""
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
                tables = {
                    row[0]
                    for row in database.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {
                        "stress_artifact_bundles",
                        "stress_artifacts",
                        "stress_runs",
                    }.issubset(tables)
                )
                self.assertEqual(
                    list(database.connection.execute("PRAGMA foreign_key_check")), []
                )

            backup = path.with_name("state.db.v7.bak")
            self.assertTrue(backup.is_file())
            backup_before = backup.read_bytes()
            backup_connection = sqlite3.connect(backup)
            try:
                self.assertEqual(
                    backup_connection.execute("PRAGMA user_version").fetchone()[0], 7
                )
                self.assertIsNone(
                    backup_connection.execute(
                        """SELECT name FROM sqlite_master
                           WHERE name='stress_runs'"""
                    ).fetchone()
                )
                self.assertEqual(
                    backup_connection.execute(
                        "SELECT COUNT(*) FROM problems WHERE problem_id='1A'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                backup_connection.close()

            with Database(path):
                pass
            self.assertEqual(backup.read_bytes(), backup_before)

    def test_new_database_does_not_create_v7_upgrade_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            with Database(path):
                pass
            self.assertFalse(path.with_name("state.db.v7.bak").exists())
            self.assertFalse(path.with_name("state.db.v8.bak").exists())
            self.assertFalse(path.with_name("state.db.v9.bak").exists())
            self.assertFalse(path.with_name("state.db.v10.bak").exists())

    def test_v8_migration_removes_medium_and_backs_up_v9_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v8_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """INSERT INTO problems(
                           platform,problem_id,tags_json,source_json,updated_at)
                       VALUES('codeforces','1A','[]','{}','2026-08-05T00:00:00+00:00')"""
                )
                connection.execute(
                    """INSERT INTO stress_artifact_bundles(
                           id,platform,problem_id,created_at,updated_at)
                       VALUES('legacy-bundle','codeforces','1A',
                              '2026-08-05T00:00:00+00:00',
                              '2026-08-05T00:00:00+00:00')"""
                )
                connection.execute(
                    """INSERT INTO stress_runs(
                           id,bundle_id,platform,problem_id,user_source_path,
                           user_source_hash,owner_pid,config_json,status,phase,
                           start_seed,current_seed,next_seed,small_count,
                           medium_count,total_count,mismatch_seed,failure_path,
                           stop_reason,error_json,revision,created_at,updated_at,
                           started_at,completed_at)
                       VALUES('legacy-run','legacy-bundle','codeforces','1A',
                              '2026/8/5/CF1A.cpp','old-hash',12345,
                              '{"profile_version":1,"medium_enabled":true}',
                              'stopped','medium',41,52,53,8,3,11,
                              52,'.acm/failures/legacy','legacy_complete',
                              '{"code":"legacy"}',4,
                              '2026-08-05T00:00:00+00:00',
                              '2026-08-05T00:01:00+00:00',
                              '2026-08-05T00:00:10+00:00',
                              '2026-08-05T00:01:00+00:00')"""
                )
                connection.commit()
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    12,
                )
                run = database.stress_run("legacy-run")
                self.assertIsNotNone(run)
                self.assertNotIn("medium_count", dict(run))
                self.assertEqual(run["large_count"], 0)
                self.assertEqual(run["next_seed"], 53)
                self.assertEqual(run["status"], "stopped")
                self.assertEqual(run["mismatch_seed"], 52)
                self.assertEqual(run["failure_path"], ".acm/failures/legacy")
                self.assertEqual(run["stop_reason"], "legacy_complete")
                self.assertEqual(json.loads(run["error_json"])["code"], "legacy")
                self.assertEqual(run["revision"], 4)
                self.assertEqual(run["started_at"], "2026-08-05T00:00:10+00:00")
                self.assertEqual(run["completed_at"], "2026-08-05T00:01:00+00:00")
                self.assertEqual(json.loads(run["config_json"])["profile_version"], 1)
                self.assertEqual(
                    list(database.connection.execute("PRAGMA foreign_key_check")), []
                )
                indexes = {
                    row[0]
                    for row in database.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertTrue(
                    {
                        "stress_runs_problem_idx",
                        "stress_runs_status_idx",
                        "stress_runs_single_active_idx",
                    }.issubset(indexes)
                )

            backup = path.with_name("state.db.v9.bak")
            self.assertTrue(backup.is_file())
            backup_before = backup.read_bytes()
            backup_connection = sqlite3.connect(backup)
            try:
                self.assertEqual(
                    backup_connection.execute("PRAGMA user_version").fetchone()[0], 9
                )
                columns = {
                    row[1]
                    for row in backup_connection.execute("PRAGMA table_info(stress_runs)")
                }
                self.assertIn("medium_count", columns)
                self.assertIn("large_count", columns)
                self.assertEqual(
                    backup_connection.execute(
                        "SELECT medium_count FROM stress_runs WHERE id='legacy-run'"
                    ).fetchone()[0],
                    3,
                )
            finally:
                backup_connection.close()

            with Database(path):
                pass
            self.assertEqual(backup.read_bytes(), backup_before)

    def test_v10_migration_preserves_data_and_recovers_duplicate_setup_slots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v10_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """INSERT INTO problems(
                           platform,problem_id,tags_json,source_json,updated_at)
                       VALUES('codeforces','1A','[]','{}',
                              '2026-08-05T00:00:00+00:00')"""
                )
                connection.execute(
                    """INSERT INTO stress_artifact_bundles(
                           id,platform,problem_id,created_at,updated_at)
                       VALUES('legacy-bundle','codeforces','1A',
                              '2026-08-05T00:00:00+00:00',
                              '2026-08-05T00:00:00+00:00')"""
                )
                for run_id, stamp in (
                    ("old-setup", "2026-08-05T00:00:00+00:00"),
                    ("new-setup", "2026-08-05T00:01:00+00:00"),
                ):
                    connection.execute(
                        """INSERT INTO ai_runs(
                               id,kind,model,status,created_at)
                           VALUES(?, 'stress_setup', 'deepseek-chat',
                                  'running', ?)""",
                        (run_id, stamp),
                    )
                connection.commit()
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    12,
                )
                ai_columns = {
                    row[1]
                    for row in database.connection.execute("PRAGMA table_info(ai_runs)")
                }
                bundle_columns = {
                    row[1]
                    for row in database.connection.execute(
                        "PRAGMA table_info(stress_artifact_bundles)"
                    )
                }
                self.assertIn("preparation_meta_json", ai_columns)
                self.assertTrue(
                    {"preparation_cache_key", "preparation_meta_json"}.issubset(
                        bundle_columns
                    )
                )
                self.assertIsNotNone(
                    database.connection.execute(
                        """SELECT name FROM sqlite_master
                           WHERE type='table' AND name='stress_preparation_cache'"""
                    ).fetchone()
                )
                self.assertEqual(database.ai_run("old-setup")["status"], "interrupted")
                self.assertEqual(database.ai_run("new-setup")["status"], "running")
                self.assertEqual(database.active_stress_setup_run()["id"], "new-setup")
                indexes = {
                    row[0]
                    for row in database.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertTrue(
                    {
                        "stress_artifact_bundles_preparation_cache_idx",
                        "ai_runs_single_running_stress_setup_idx",
                    }.issubset(indexes)
                )
                self.assertEqual(
                    list(database.connection.execute("PRAGMA foreign_key_check")), []
                )

            backup = path.with_name("state.db.v10.bak")
            self.assertTrue(backup.is_file())
            backup_before = backup.read_bytes()
            backup_connection = sqlite3.connect(backup)
            try:
                self.assertEqual(
                    backup_connection.execute("PRAGMA user_version").fetchone()[0], 10
                )
                self.assertNotIn(
                    "preparation_meta_json",
                    {
                        row[1]
                        for row in backup_connection.execute(
                            "PRAGMA table_info(ai_runs)"
                        )
                    },
                )
                self.assertEqual(
                    backup_connection.execute(
                        """SELECT COUNT(*) FROM ai_runs
                           WHERE kind='stress_setup' AND status='running'"""
                    ).fetchone()[0],
                    2,
                )
            finally:
                backup_connection.close()

            with Database(path):
                pass
            self.assertEqual(backup.read_bytes(), backup_before)

    def test_v11_migration_adds_proof_cache_and_backs_up_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v11_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """INSERT INTO problems(
                           platform,problem_id,tags_json,source_json,updated_at)
                       VALUES('codeforces','1A','[]','{}',
                              '2026-08-06T00:00:00+00:00')"""
                )
                connection.execute(
                    """INSERT INTO stress_artifact_bundles(
                           id,platform,problem_id,created_at,updated_at)
                       VALUES('legacy-v11','codeforces','1A',
                              '2026-08-06T00:00:00+00:00',
                              '2026-08-06T00:00:00+00:00')"""
                )
                connection.commit()
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    12,
                )
                tables = {
                    row[0]
                    for row in database.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {
                        "problem_samples",
                        "stress_artifact_candidates",
                        "stress_artifact_proofs",
                        "stress_bundle_certifications",
                        "stress_cache_aliases",
                    }.issubset(tables)
                )
                bundle_columns = {
                    row[1]
                    for row in database.connection.execute(
                        "PRAGMA table_info(stress_artifact_bundles)"
                    )
                }
                self.assertIn("certification_key", bundle_columns)
                self.assertIsNotNone(database.stress_artifact_bundle("legacy-v11"))
                self.assertEqual(
                    list(database.connection.execute("PRAGMA foreign_key_check")), []
                )

            backup = path.with_name("state.db.v11.bak")
            self.assertTrue(backup.is_file())
            before = backup.read_bytes()
            backup_connection = sqlite3.connect(backup)
            try:
                self.assertEqual(
                    backup_connection.execute("PRAGMA user_version").fetchone()[0], 11
                )
                self.assertNotIn(
                    "certification_key",
                    {
                        row[1]
                        for row in backup_connection.execute(
                            "PRAGMA table_info(stress_artifact_bundles)"
                        )
                    },
                )
            finally:
                backup_connection.close()
            with Database(path):
                pass
            self.assertEqual(backup.read_bytes(), before)

    def test_failed_v12_migration_rolls_back_and_restart_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v11_database(path)
            original = MIGRATIONS[12]
            try:
                MIGRATIONS[12] = (
                    original + "\nINSERT INTO table_that_does_not_exist VALUES(1);"
                )
                with self.assertRaises(sqlite3.OperationalError):
                    Database(path)
            finally:
                MIGRATIONS[12] = original

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 11
                )
                self.assertIsNone(
                    connection.execute(
                        """SELECT name FROM sqlite_master
                           WHERE name='stress_artifact_candidates'"""
                    ).fetchone()
                )
                self.assertNotIn(
                    "certification_key",
                    {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(stress_artifact_bundles)"
                        )
                    },
                )
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    12,
                )

    def test_failed_v11_migration_rolls_back_and_restart_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v10_database(path)
            original = MIGRATIONS[11]
            try:
                MIGRATIONS[11] = (
                    original + "\nINSERT INTO table_that_does_not_exist VALUES(1);"
                )
                with self.assertRaises(sqlite3.OperationalError):
                    Database(path)
            finally:
                MIGRATIONS[11] = original

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 10
                )
                self.assertNotIn(
                    "preparation_meta_json",
                    {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(ai_runs)")
                    },
                )
                self.assertIsNone(
                    connection.execute(
                        """SELECT name FROM sqlite_master
                           WHERE name='stress_preparation_cache'"""
                    ).fetchone()
                )
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    12,
                )

    def test_failed_v10_migration_rolls_back_and_restart_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v9_database(path)
            original = MIGRATIONS[10]
            try:
                MIGRATIONS[10] = (
                    original + "\nINSERT INTO table_that_does_not_exist VALUES(1);"
                )
                with self.assertRaises(sqlite3.OperationalError):
                    Database(path)
            finally:
                MIGRATIONS[10] = original

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 9
                )
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(stress_runs)")
                }
                self.assertIn("medium_count", columns)
                self.assertIn("large_count", columns)
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    12,
                )
                columns = {
                    row[1]
                    for row in database.connection.execute(
                        "PRAGMA table_info(stress_runs)"
                    )
                }
                self.assertNotIn("medium_count", columns)
                self.assertIn("large_count", columns)

    def test_failed_v9_migration_rolls_back_and_restart_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v8_database(path)
            original = MIGRATIONS[9]
            try:
                MIGRATIONS[9] = (
                    original + "\nINSERT INTO table_that_does_not_exist VALUES(1);"
                )
                with self.assertRaises(sqlite3.OperationalError):
                    Database(path)
            finally:
                MIGRATIONS[9] = original

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 8
                )
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(stress_runs)")
                }
                self.assertNotIn("large_count", columns)
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0],
                    12,
                )
                columns = {
                    row[1]
                    for row in database.connection.execute(
                        "PRAGMA table_info(stress_runs)"
                    )
                }
                self.assertIn("large_count", columns)

    def test_failed_v8_migration_rolls_back_and_restart_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            create_v7_database(path)
            original = MIGRATIONS[8]
            try:
                MIGRATIONS[8] = (
                    original + "\nINSERT INTO table_that_does_not_exist VALUES(1);"
                )
                with self.assertRaises(sqlite3.OperationalError):
                    Database(path)
            finally:
                MIGRATIONS[8] = original

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 7
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='stress_runs'"
                    ).fetchone()
                )
            finally:
                connection.close()

            with Database(path) as database:
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0], 12
                )


class StressStorageRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.db"
        self.db = Database(self.path)
        self.db.upsert_problem(
            {"platform": "codeforces", "problem_id": "1A", "name": "Theatre Square"}
        )
        self.attempt_id = self.db.start_attempt("codeforces", "1A")
        self.bundle = self.db.create_stress_artifact_bundle(
            "bundle-1",
            attempt_id=self.attempt_id,
            platform="codeforces",
            problem_id="1A",
            contract={"comparison": "tokens", "small": {"n": 8}},
            baseline_manifest={"generator": None, "brute": "old-hash"},
        )

    def tearDown(self) -> None:
        self.db.close()
        self.temporary.cleanup()

    def test_bundle_and_artifact_lifecycle(self) -> None:
        self.assertEqual(json.loads(self.bundle["contract_json"])["comparison"], "tokens")
        generator_code = "#include <bits/stdc++.h>\nint main(){}\n"
        artifact = self.db.save_stress_artifact(
            "artifact-generator",
            bundle_id="bundle-1",
            kind="generator",
            source_code=generator_code,
            target_path="2026/8/5/CF1A.gen.cpp",
            source_kind="ai_generated",
            validation={"compiled": False, "checks": {"static": True}},
            metadata={"provider": {"name": "deepseek"}},
        )
        self.assertEqual(
            artifact["source_hash"],
            hashlib.sha256(generator_code.encode("utf-8")).hexdigest(),
        )
        reference = self.db.save_stress_artifact(
            "artifact-reference",
            bundle_id="bundle-1",
            kind="reference",
            source_code="int main(){}\n",
            target_path="2026/8/5/CF1A.ref.cpp",
            source_kind="codeforces_editorial",
            source_url="https://codeforces.com/blog/entry/1",
            source_title="Tutorial",
            source_license="unknown",
            source_content_hash="page-hash",
        )
        self.assertEqual(reference["source_kind"], "codeforces_editorial")
        local = self.db.save_stress_artifact(
            "artifact-brute",
            bundle_id="bundle-1",
            kind="brute",
            source_code="int main(){}\n",
            target_path="2026/8/5/CF1A.bf.cpp",
            source_kind="local_existing",
        )
        self.assertEqual(local["source_kind"], "local_existing")
        self.assertEqual(
            [row["kind"] for row in self.db.stress_artifacts("bundle-1")],
            ["generator", "brute", "reference"],
        )
        artifact = self.db.update_stress_artifact(
            artifact["id"],
            status="compiled",
            validation={"compiled": True, "checks": {"profile_v2": True}},
            metadata={"provider": {"cached": False}},
        )
        self.assertEqual(artifact["status"], "compiled")
        validation = json.loads(artifact["validation_json"])
        metadata = json.loads(artifact["metadata_json"])
        self.assertTrue(validation["compiled"])
        self.assertEqual(validation["checks"], {"profile_v2": True, "static": True})
        self.assertEqual(metadata["provider"], {"cached": False, "name": "deepseek"})

        bundle = self.db.update_stress_artifact_bundle(
            "bundle-1",
            expected_revision=1,
            status="applied",
            backup_path=".acm/ai-backups/stress/bundle-1",
            applied_at="2026-08-05T01:00:00+00:00",
        )
        self.assertEqual(bundle["revision"], 2)
        with self.assertRaises(StressArtifactBundleRevisionConflict):
            self.db.update_stress_artifact_bundle(
                "bundle-1", expected_revision=1, status="failed"
            )

    def test_preparation_cache_bundle_lookup_and_metadata_merges(self) -> None:
        cached = self.db.save_stress_preparation_cache(
            "cache-v1-key",
            payload={
                "contract": {"comparison": "tokens"},
                "artifacts": {"generator": {"code": "int main(){}\n"}},
            },
            metadata={"validation": {"compiled": True}, "hits": 0},
            created_at="2026-08-05T01:00:00+00:00",
        )
        self.assertTrue(json.loads(cached["metadata_json"])["validation"]["compiled"])
        cached = self.db.save_stress_preparation_cache(
            "cache-v1-key",
            payload={
                "artifacts": {"generator": {"code": "int main(){}\n"}},
                "contract": {"comparison": "tokens"},
            },
            metadata={"validation": {"profile_v2": True}, "hits": 1},
        )
        cache_meta = json.loads(cached["metadata_json"])
        self.assertEqual(cache_meta["hits"], 1)
        self.assertEqual(
            cache_meta["validation"], {"compiled": True, "profile_v2": True}
        )
        with self.assertRaises(StressPreparationCacheConflict):
            self.db.save_stress_preparation_cache(
                "cache-v1-key", payload={"contract": {"comparison": "exact"}}
            )

        bundle = self.db.update_stress_artifact_bundle(
            "bundle-1",
            preparation_cache_key="cache-v1-key",
            preparation_meta={
                "cache": {"result": "miss"},
                "validation": {"compiled": True},
            },
        )
        bundle = self.db.update_stress_artifact_bundle(
            "bundle-1",
            preparation_meta={
                "cache": {"result": "stored"},
                "validation": {"profile_v2": True},
            },
        )
        self.assertEqual(
            self.db.stress_artifact_bundle_for_cache_key("cache-v1-key")["id"],
            "bundle-1",
        )
        bundle_meta = json.loads(bundle["preparation_meta_json"])
        self.assertEqual(bundle_meta["cache"]["result"], "stored")
        self.assertEqual(
            bundle_meta["validation"], {"compiled": True, "profile_v2": True}
        )

    def test_problem_samples_upsert_and_content_deduplication(self) -> None:
        first = self.db.upsert_problem_sample(
            "codeforces",
            "1A",
            "sample-1",
            input_data="1 2\n",
            expected_output="3\n",
            metadata={"ordinal": 1},
        )
        duplicate = self.db.upsert_problem_sample(
            "codeforces",
            "1A",
            "copied-name",
            input_data=b"1 2\n",
            expected_output=b"3\n",
            metadata={"verified": True},
        )
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(len(self.db.problem_samples("codeforces", "1A")), 1)
        metadata = json.loads(duplicate["metadata_json"])
        self.assertEqual(metadata, {"ordinal": 1, "verified": True})

        replaced = self.db.upsert_problem_sample(
            "codeforces",
            "1A",
            "sample-1",
            input_data="2 2\n",
            expected_output="4\n",
        )
        self.assertEqual(replaced["id"], first["id"])
        self.assertEqual(bytes(replaced["input_data"]), b"2 2\n")
        self.assertEqual(bytes(replaced["expected_output"]), b"4\n")

    def test_problem_samples_replace_is_atomic_and_source_scoped(self) -> None:
        self.db.upsert_problem_sample(
            "codeforces",
            "1A",
            "external",
            input_data="9\n",
            expected_output="9\n",
            source="external_fixture",
        )
        rows = self.db.replace_problem_samples(
            "codeforces",
            "1A",
            [
                {"name": "sample1", "input": "1 2\n", "output": "3\n"},
                {"name": "sample2", "input": "2 3\n", "output": "5\n"},
            ],
            metadata={"statement_hash": "first"},
        )
        self.assertEqual(len(rows), 3)
        rows = self.db.replace_problem_samples(
            "codeforces",
            "1A",
            [{"name": "sample1", "input": "4 5\n", "output": "9\n"}],
            metadata={"statement_hash": "second"},
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {str(row["source"]) for row in rows},
            {"problem_context", "external_fixture"},
        )

    def _save_candidate(self, role: str, suffix: str = ""):
        return self.db.save_stress_artifact_candidate(
            f"candidate-{role}{suffix}",
            generation_key=f"generation-{role}{suffix}",
            platform="codeforces",
            problem_id="1A",
            role=role,
            source_code=f"int main(){{return {len(suffix)};}}\n",
            source_kind="ai_generated",
            generation_identity={"model": "deepseek-v4-flash", "role": role},
            provenance={"prompt_version": 4},
            usage={"total_tokens": 10},
        )

    def test_candidate_and_proof_saves_are_immutable_and_queryable(self) -> None:
        candidate = self._save_candidate("generator")
        again = self._save_candidate("generator")
        self.assertEqual(again["id"], candidate["id"])
        self.assertEqual(
            [row["id"] for row in self.db.stress_artifact_candidates(
                generation_key="generation-generator"
            )],
            ["candidate-generator"],
        )
        with self.assertRaises(StressArtifactCandidateConflict):
            self.db.save_stress_artifact_candidate(
                "candidate-generator",
                generation_key="generation-generator",
                platform="codeforces",
                problem_id="1A",
                role="generator",
                source_code="int main(){return 9;}\n",
                source_kind="ai_generated",
                generation_identity={"model": "deepseek-v4-flash", "role": "generator"},
            )

        proof = self.db.save_stress_artifact_proof(
            "proof-generator-compile",
            candidate_id=candidate["id"],
            proof_kind="compile",
            certification_identity={"compiler": "g++-fingerprint"},
            status="passed",
            result={"compiled": True},
            executable_path=".acm/build/gen.exe",
            executable_hash="e" * 64,
        )
        same = self.db.save_stress_artifact_proof(
            "proof-generator-compile",
            candidate_id=candidate["id"],
            proof_kind="compile",
            certification_identity={"compiler": "g++-fingerprint"},
            status="passed",
            result={"compiled": True},
            executable_path=".acm/build/gen.exe",
            executable_hash="e" * 64,
        )
        self.assertEqual(same["proof_key"], proof["proof_key"])
        self.assertEqual(
            len(self.db.stress_artifact_proofs(candidate_id=candidate["id"])), 1
        )
        with self.assertRaises(StressArtifactProofConflict):
            self.db.save_stress_artifact_proof(
                "proof-generator-compile",
                candidate_id=candidate["id"],
                proof_kind="compile",
                certification_identity={"compiler": "g++-fingerprint"},
                status="failed",
                result={"compiled": False},
            )

    def test_certification_is_immutable_and_bundle_can_reference_it(self) -> None:
        generator = self._save_candidate("generator", "-cert")
        brute = self._save_candidate("brute", "-cert")
        reference = self._save_candidate("reference", "-cert")
        certification = self.db.save_stress_bundle_certification(
            "certification-1",
            platform="codeforces",
            problem_id="1A",
            generator_candidate_id=generator["id"],
            brute_candidate_id=brute["id"],
            reference_candidate_id=reference["id"],
            certification_identity={"preflight_version": 3},
            scope={"profiles": ["small", "large"]},
            preflight={"passed": True},
        )
        same = self.db.save_stress_bundle_certification(
            "certification-1",
            platform="codeforces",
            problem_id="1A",
            generator_candidate_id=generator["id"],
            brute_candidate_id=brute["id"],
            reference_candidate_id=reference["id"],
            certification_identity={"preflight_version": 3},
            scope={"profiles": ["small", "large"]},
            preflight={"passed": True},
        )
        self.assertEqual(same["certification_key"], certification["certification_key"])
        with self.assertRaises(StressBundleCertificationConflict):
            self.db.save_stress_bundle_certification(
                "certification-1",
                platform="codeforces",
                problem_id="1A",
                generator_candidate_id=generator["id"],
                brute_candidate_id=brute["id"],
                reference_candidate_id=reference["id"],
                certification_identity={"preflight_version": 3},
                scope={"profiles": ["small"]},
                preflight={"passed": True},
            )
        bundle = self.db.update_stress_artifact_bundle(
            "bundle-1", certification_key="certification-1"
        )
        self.assertEqual(bundle["certification_key"], "certification-1")
        self.assertEqual(
            self.db.stress_bundle_certifications(
                platform="codeforces", problem_id="1A", status="valid"
            )[0]["certification_key"],
            "certification-1",
        )

    def test_cache_alias_publish_uses_compare_and_swap(self) -> None:
        created = self.db.publish_stress_cache_alias(
            "problem:CF1A:certified",
            alias_kind="certification",
            target_id="certification-1",
        )
        self.assertEqual(created["revision"], 1)
        with self.assertRaises(StressCacheAliasRevisionConflict):
            self.db.publish_stress_cache_alias(
                "problem:CF1A:certified",
                alias_kind="certification",
                target_id="certification-2",
            )
        updated = self.db.publish_stress_cache_alias(
            "problem:CF1A:certified",
            alias_kind="certification",
            target_id="certification-2",
            expected_revision=1,
        )
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["target_id"], "certification-2")
        with self.assertRaises(StressCacheAliasRevisionConflict):
            self.db.publish_stress_cache_alias(
                "problem:CF1A:certified",
                alias_kind="certification",
                target_id="certification-3",
                expected_revision=1,
            )

        run = self.db.acquire_stress_setup_slot(
            "setup-meta",
            model="deepseek-chat",
            preparation_meta={
                "timeout_seconds": 300,
                "stage_timings_ms": {"source": 20},
                "provider_usage": {"deepseek": {"calls": 1}},
            },
        )
        run = self.db.update_ai_run(
            run["id"],
            preparation_meta={
                "stage_timings_ms": {"preflight": 40},
                "local_cache": {"result": "hit"},
                "failure": {"provider": "local_cache"},
            },
        )
        run_meta = json.loads(run["preparation_meta_json"])
        self.assertEqual(
            run_meta["stage_timings_ms"], {"preflight": 40, "source": 20}
        )
        self.assertEqual(run_meta["local_cache"]["result"], "hit")
        self.assertEqual(run_meta["provider_usage"]["deepseek"]["calls"], 1)

    def test_setup_slot_is_atomic_across_connections(self) -> None:
        barrier = threading.Barrier(2)
        results: list[tuple[str, str, str | None]] = []
        result_lock = threading.Lock()

        def acquire(run_id: str) -> None:
            with Database(self.path) as database:
                barrier.wait(timeout=5)
                try:
                    row = database.acquire_stress_setup_slot(
                        run_id, model="deepseek-chat"
                    )
                except StressSetupSlotConflict as exc:
                    outcome = ("conflict", run_id, exc.active_run_id)
                else:
                    outcome = ("acquired", str(row["id"]), None)
                with result_lock:
                    results.append(outcome)

        threads = [
            threading.Thread(target=acquire, args=("setup-a",)),
            threading.Thread(target=acquire, args=("setup-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        acquired = [result for result in results if result[0] == "acquired"]
        conflicts = [result for result in results if result[0] == "conflict"]
        self.assertEqual(len(acquired), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0][2], acquired[0][1])
        self.assertEqual(self.db.active_stress_setup_run()["id"], acquired[0][1])

    def test_run_progress_stop_recovery_and_resume(self) -> None:
        run = self.db.create_stress_run(
            "stress-1",
            bundle_id="bundle-1",
            attempt_id=self.attempt_id,
            platform="codeforces",
            problem_id="1A",
            user_source_path="2026/8/5/CF1A.cpp",
            user_source_hash="solution-hash",
            config={"profile_version": 2, "warmup_cases": 200, "include_large": True},
            start_seed=41,
            large_count=1,
        )
        self.assertEqual(run["next_seed"], 41)
        self.assertEqual(run["large_count"], 1)
        self.assertIn("large_count", dict(run))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.create_stress_run(
                "stress-2",
                bundle_id="bundle-1",
                platform="codeforces",
                problem_id="1A",
                user_source_path="2026/8/5/CF1A.cpp",
                user_source_hash="solution-hash",
            )

        run = self.db.update_stress_run(
            "stress-1",
            expected_revision=1,
            status="running",
            phase="warmup",
            current_seed=45,
            next_seed=46,
            small_count=5,
            large_count=3,
            total_count=5,
            started_at="2026-08-05T01:00:00+00:00",
        )
        self.assertEqual(run["revision"], 2)
        self.assertEqual(json.loads(run["config_json"])["warmup_cases"], 200)
        self.assertNotIn("medium_count", dict(run))
        self.assertEqual(run["large_count"], 3)
        with self.assertRaises(StressRunRevisionConflict):
            self.db.update_stress_run(
                "stress-1", expected_revision=1, current_seed=99
            )

        requested = self.db.request_stress_run_stop("stress-1")
        self.assertEqual(requested["status"], "stop_requested")
        self.assertEqual(requested["stop_reason"], "user_requested")
        stopped = self.db.update_stress_run(
            "stress-1",
            expected_revision=requested["revision"],
            status="stopped",
            mismatch_seed=45,
            failure_path=".acm/failures/old",
            error={"code": "old"},
            completed_at="2026-08-05T01:01:00+00:00",
        )
        self.assertIsNone(self.db.active_stress_run())
        resumed = self.db.resume_stress_run(
            "stress-1", user_source_hash="new-solution-hash"
        )
        self.assertEqual(resumed["status"], "pending")
        self.assertEqual(resumed["current_seed"], 46)
        self.assertEqual(resumed["user_source_hash"], "new-solution-hash")
        self.assertNotIn("medium_count", dict(resumed))
        self.assertEqual(resumed["large_count"], 3)
        self.assertIsNone(resumed["mismatch_seed"])
        self.assertIsNone(resumed["failure_path"])
        self.assertEqual(json.loads(resumed["error_json"]), {})
        self.assertIsNone(resumed["completed_at"])
        self.assertEqual(self.db.reconcile_interrupted_stress_state(), 0)

        self.db.update_stress_run("stress-1", owner_pid=2147483647)
        self.assertEqual(self.db.reconcile_interrupted_stress_state(), 1)
        interrupted = self.db.stress_run("stress-1")
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["stop_reason"], "service_restart")
        self.assertEqual(interrupted["next_seed"], 46)
        self.assertEqual(
            self.db.stress_runs(problem_id="1A", status="interrupted")[0]["id"],
            "stress-1",
        )

    def test_combined_startup_reconciliation_interrupts_stress_run(self) -> None:
        self.db.create_stress_run(
            "stress-1",
            bundle_id="bundle-1",
            platform="codeforces",
            problem_id="1A",
            user_source_path="2026/8/5/CF1A.cpp",
            user_source_hash="solution-hash",
            owner_pid=2147483647,
        )

        self.db.reconcile_interrupted_ai_state()

        self.assertEqual(self.db.stress_run("stress-1")["status"], "interrupted")

    def test_startup_reconciliation_preserves_live_stress_setup_owner(self) -> None:
        self.db.acquire_stress_setup_slot(
            "live-setup",
            model="deepseek-v4-flash",
            preparation_meta={"owner_pid": os.getpid()},
        )
        self.db.reconcile_interrupted_ai_state()
        self.assertEqual(self.db.ai_run("live-setup")["status"], "running")
        self.db.update_ai_run(
            "live-setup", status="failed", completed_at="2026-08-05T00:00:00+00:00"
        )

    def test_finish_active_or_paused_run_is_permanent(self) -> None:
        self.db.create_stress_run(
            "stress-finish",
            bundle_id="bundle-1",
            attempt_id=self.attempt_id,
            platform="codeforces",
            problem_id="1A",
            user_source_path="2026/8/5/CF1A.cpp",
            user_source_hash="solution-hash",
            config={"profile_version": 2, "rate_base_total": 0},
            start_seed=41,
        )
        ending = self.db.request_stress_run_finish("stress-finish")
        self.assertEqual(ending["status"], "stop_requested")
        self.assertEqual(ending["stop_reason"], "user_finished")
        paused = self.db.update_stress_run(
            "stress-finish",
            expected_revision=int(ending["revision"]),
            status="stopped",
            completed_at="2026-08-05T01:01:00+00:00",
        )
        finished = self.db.request_stress_run_finish("stress-finish")
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["stop_reason"], "user_finished")
        self.assertIsNone(self.db.active_stress_run())
        with self.assertRaises(ValueError):
            self.db.resume_stress_run("stress-finish")


if __name__ == "__main__":
    unittest.main()
