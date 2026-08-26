from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.ai_cache import (
    CacheArtifactTooLarge,
    build_cache_key,
    validate_cached_artifact,
)
from tools.acm_agent.ai_reliability import build_ai_outcome, validate_ai_outcome
from tools.acm_agent.config import CONFIG_VERSION, DEFAULT_CONFIG, Paths, load_config
from tools.acm_agent.provider import ProviderConfigurationError
from tools.acm_agent.provider_config import validate_cache_policy
from tools.acm_agent.storage import Database, MIGRATIONS, SCHEMA_VERSION


def request_key(**changes: object):
    values = {
        "profile_id": "recommendation",
        "provider_id": "deepseek",
        "model": "deepseek-v4-flash",
        "provider_definition_hash": "definition-v1",
        "generation": {"temperature": 0, "max_tokens": 100},
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "private-input"},
        ],
        "prompt_version": "prompt-v1",
        "schema_version": "schema-v1",
        "validator_version": "validator-v1",
        "lowering_version": "lowering-v1",
        "taxonomy_version": "taxonomy-v1",
        "correctness_inputs": {
            "statement": {"sha256": "statement-v1"},
            "candidates": ["1A", "2B"],
        },
    }
    values.update(changes)
    return build_cache_key(**values)


class Stage4CanonicalCacheTests(unittest.TestCase):
    def test_canonical_key_ignores_mapping_order(self) -> None:
        first = request_key()
        second = request_key(
            generation={"max_tokens": 100, "temperature": 0},
            correctness_inputs={
                "candidates": ["1A", "2B"],
                "statement": {"sha256": "statement-v1"},
            },
        )
        self.assertEqual(first.key, second.key)
        self.assertEqual(first.manifest_hash, first.key)

    def test_every_correctness_dimension_invalidates_key(self) -> None:
        base = request_key().key
        variants = (
            {"profile_id": "summary"},
            {"provider_id": "relay"},
            {"model": "deepseek-v4-pro"},
            {"provider_definition_hash": "definition-v2"},
            {"generation": {"temperature": 0.1, "max_tokens": 100}},
            {"messages": [{"role": "system", "content": "changed"}]},
            {"prompt_version": "prompt-v2"},
            {"schema_version": "schema-v2"},
            {"validator_version": "validator-v2"},
            {"lowering_version": "lowering-v2"},
            {"taxonomy_version": "taxonomy-v2"},
            {"transport_api": "responses"},
            {"response_schema_hash": "schema-hash-v2"},
            {"repair_version": "repair-v2"},
            {"correctness_inputs": {"statement": "changed"}},
        )
        for change in variants:
            with self.subTest(change=change):
                self.assertNotEqual(request_key(**change).key, base)


class Stage4ConfigTests(unittest.TestCase):
    def test_outcome_contract_separates_usable_from_partial(self) -> None:
        complete = build_ai_outcome(
            provider_outcome="failed",
            artifact_outcome="not_applicable",
            business_outcome="deterministic_fallback",
            degraded=True,
        )
        self.assertTrue(complete["usable"])
        self.assertFalse(complete["apply_ready"])
        self.assertEqual(validate_ai_outcome(complete), complete)
        partial = build_ai_outcome(
            provider_outcome="failed",
            artifact_outcome="partial",
            business_outcome="partial",
        )
        self.assertFalse(partial["usable"])
        with self.assertRaisesRegex(ValueError, "usable conflicts"):
            build_ai_outcome(
                provider_outcome="failed",
                artifact_outcome="partial",
                business_outcome="partial",
                usable=True,
            )

    def test_v12_defaults_are_exact_only_and_bounded(self) -> None:
        self.assertEqual(CONFIG_VERSION, 15)
        cache = DEFAULT_CONFIG["ai"]["cache"]
        self.assertEqual(
            cache["exact_profiles"],
            ["recommendation", "plan_organize", "summary"],
        )
        self.assertEqual(cache["ttl_seconds"], 7 * 24 * 60 * 60)
        self.assertEqual(cache["max_entries"], 512)
        self.assertEqual(cache["max_bytes"], 64 * 1024 * 1024)
        self.assertEqual(cache["max_entry_bytes"], 256 * 1024)
        self.assertEqual(cache["flight_lease_seconds"], 180)
        self.assertEqual(cache["flight_wait_timeout_seconds"], 180)
        self.assertEqual(cache["flight_failure_cooldown_seconds"], 30)
        self.assertEqual(DEFAULT_CONFIG["ai"]["coaching_delivery_mode"], "resilient")
        self.assertTrue(all(
            budget["max_validation_repairs"] == 1
            for budget in DEFAULT_CONFIG["ai"]["policy"]["budgets"].values()
        ))
        self.assertFalse(cache["semantic_enabled"])

    def test_v11_migrates_without_changing_existing_ai_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            paths.ensure()
            old = deepcopy(DEFAULT_CONFIG)
            old["version"] = 11
            old["ai"].pop("cache")
            old["ai"]["policy"]["budgets"]["summary"]["max_requests"] = 3
            paths.config.write_text(json.dumps(old), encoding="utf-8")
            loaded = load_config(paths)
            self.assertEqual(loaded["version"], CONFIG_VERSION)
            self.assertEqual(
                loaded["ai"]["policy"]["budgets"]["summary"]["max_requests"], 3
            )
            self.assertFalse(loaded["ai"]["cache"]["semantic_enabled"])

    def test_semantic_cache_and_unsafe_exact_profiles_fail_closed(self) -> None:
        semantic = deepcopy(DEFAULT_CONFIG["ai"]["cache"])
        semantic["semantic_enabled"] = True
        with self.assertRaisesRegex(ProviderConfigurationError, "not supported"):
            validate_cache_policy(semantic)
        unsafe = deepcopy(DEFAULT_CONFIG["ai"]["cache"])
        unsafe["exact_profiles"].append("coaching")
        with self.assertRaisesRegex(ProviderConfigurationError, "limited"):
            validate_cache_policy(unsafe)


class Stage4StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.db"
        self.database = Database(self.path)
        self.key = request_key()

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def put(self, *, key=None, profile_id="recommendation", now=None, artifact=None):
        selected = key or self.key
        return self.database.put_ai_cache_entry(
            key=selected.key,
            profile_id=profile_id,
            artifact=artifact or {"ranking": ["1A"], "explanation": "valid"},
            proof={"validator_version": "validator-v1", "accepted": True},
            manifest_hash=selected.manifest_hash,
            ttl_seconds=60,
            source_run_id=None,
            now=now,
        )

    def test_schema_v22_contains_cache_and_separate_run_audit(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 24)
        names = {
            row[0]
            for row in self.database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("ai_cache_entries", names)
        self.assertIn("ai_request_flights", names)
        columns = {
            row[1]
            for row in self.database.connection.execute("PRAGMA table_info(ai_runs)")
        }
        self.assertTrue(
            {
                "local_cache_status",
                "local_cache_key",
                "cache_source_run_id",
                "cache_validation_json",
            }.issubset(columns)
        )

    def test_v21_to_v22_migration_preserves_run_and_is_idempotent(self) -> None:
        self.database.close()
        self.path.unlink()
        connection = sqlite3.connect(self.path)
        try:
            for version in range(1, 22):
                connection.executescript(MIGRATIONS[version])
                connection.execute(f"PRAGMA user_version={version}")
            connection.execute(
                """INSERT INTO ai_runs(
                       id,kind,model,request_summary_json,status,usage_json,error_json,
                       created_at,telemetry_json,estimated_cost_json,governance_json)
                   VALUES('old','recommendation','model','{}','complete','{}','{}',
                          '2026-08-26T00:00:00+00:00','{}','{}','{}')"""
            )
            connection.commit()
        finally:
            connection.close()
        with Database(self.path) as migrated:
            self.assertIsNotNone(migrated.ai_run("old"))
            self.assertEqual(
                json.loads(migrated.ai_run("old")["cache_validation_json"]), {}
            )
        self.database = Database(self.path)
        self.assertEqual(
            self.database.connection.execute("PRAGMA user_version").fetchone()[0], 24
        )

    def test_failed_v22_migration_rolls_back_and_restart_succeeds(self) -> None:
        self.database.close()
        self.path.unlink()
        connection = sqlite3.connect(self.path)
        try:
            for version in range(1, 22):
                connection.executescript(MIGRATIONS[version])
                connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        finally:
            connection.close()
        original = MIGRATIONS[22]
        try:
            MIGRATIONS[22] = original + "\nINSERT INTO missing_cache_table VALUES(1);"
            with self.assertRaises(sqlite3.OperationalError):
                Database(self.path)
        finally:
            MIGRATIONS[22] = original
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 21)
            self.assertNotIn(
                "local_cache_status",
                {row[1] for row in connection.execute("PRAGMA table_info(ai_runs)")},
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='ai_cache_entries'"
                ).fetchone()
            )
        finally:
            connection.close()
        self.database = Database(self.path)
        self.assertEqual(
            self.database.connection.execute("PRAGMA user_version").fetchone()[0], 24
        )

    def test_hit_reruns_validator_and_lowering(self) -> None:
        self.put()
        calls = []

        def validator(value):
            calls.append("validator")
            self.assertEqual(value["ranking"], ["1A"])
            return {**value, "validated": True}

        result = self.database.read_validated_ai_cache_entry(
            self.key.key,
            validator=validator,
            lowering=lambda value: tuple(value["ranking"]),
        )
        self.assertEqual(calls, ["validator"])
        self.assertTrue(result.artifact["validated"])
        self.assertEqual(result.lowered, ("1A",))
        row = self.database.get_ai_cache_entry(self.key.key, touch=False)
        self.assertEqual(row["hit_count"], 1)

    def test_tamper_and_validator_failure_evict_entry(self) -> None:
        self.put()
        self.database.connection.execute(
            "UPDATE ai_cache_entries SET artifact_json='{}' WHERE cache_key=?",
            (self.key.key,),
        )
        self.assertIsNone(self.database.read_validated_ai_cache_entry(self.key.key))
        self.assertIsNone(self.database.get_ai_cache_entry(self.key.key))

        self.put()
        self.assertIsNone(
            self.database.read_validated_ai_cache_entry(
                self.key.key,
                validator=lambda _value: (_ for _ in ()).throw(ValueError("stale")),
            )
        )
        self.assertIsNone(self.database.get_ai_cache_entry(self.key.key))

    def test_expiry_lru_pruning_and_profile_clear(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        first = request_key(correctness_inputs={"id": 1})
        second = request_key(correctness_inputs={"id": 2})
        third = request_key(correctness_inputs={"id": 3})
        self.put(key=first, now=base)
        self.put(key=second, now=base + timedelta(seconds=1))
        self.put(
            key=third,
            profile_id="summary",
            now=base + timedelta(seconds=2),
        )
        result = self.database.prune_ai_cache(
            now=base + timedelta(seconds=3), max_entries=2, max_bytes=10_000
        )
        self.assertEqual(result["removed_entries"], 1)
        self.assertIsNone(self.database.get_ai_cache_entry(first.key, now=base + timedelta(seconds=3)))
        self.assertEqual(
            self.database.clear_ai_cache(["summary"])["removed_entries"], 1
        )
        self.assertIsNotNone(
            self.database.get_ai_cache_entry(
                second.key, now=base + timedelta(seconds=3)
            )
        )
        self.assertIsNone(
            self.database.get_ai_cache_entry(second.key, now=base + timedelta(seconds=62))
        )

    def test_failed_flight_has_30_second_cooldown_then_allows_takeover(self) -> None:
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        self.assertEqual(
            self.database.acquire_ai_request_flight(
                self.key.key, owner_id="leader", profile_id="recommendation", now=base
            ),
            "leader",
        )
        self.assertTrue(
            self.database.release_ai_request_flight(
                self.key.key, owner_id="leader", status="failed",
                error_code="invalid_json_output", now=base + timedelta(seconds=1),
            )
        )
        self.assertEqual(
            self.database.acquire_ai_request_flight(
                self.key.key, owner_id="follower", profile_id="recommendation",
                now=base + timedelta(seconds=30),
            ),
            "failed",
        )
        self.assertEqual(
            self.database.acquire_ai_request_flight(
                self.key.key, owner_id="next", profile_id="recommendation",
                now=base + timedelta(seconds=31),
            ),
            "leader",
        )

    def test_artifact_limit_and_sensitive_fields_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "not eligible"):
            self.database.put_ai_cache_entry(
                key=self.key.key,
                profile_id="coaching",
                artifact={"answer": "unsafe persistence"},
                proof={"accepted": True},
                manifest_hash=self.key.manifest_hash,
                ttl_seconds=60,
            )
        with self.assertRaises(CacheArtifactTooLarge):
            self.database.put_ai_cache_entry(
                key=self.key.key,
                profile_id="recommendation",
                artifact={"value": "x" * 100},
                proof={"accepted": True},
                manifest_hash=self.key.manifest_hash,
                ttl_seconds=60,
                max_entry_bytes=10,
            )
        for artifact in (
            {"prompt": "do not persist"},
            {"metadata": {"source_path": "C:/private/file.cpp"}},
            {"reasoning_content": "hidden"},
        ):
            with self.subTest(artifact=artifact), self.assertRaisesRegex(
                ValueError, "forbidden"
            ):
                self.put(artifact=artifact)

    def test_manifest_private_inputs_are_not_persisted(self) -> None:
        self.put()
        payload = "\n".join(
            str(value)
            for row in self.database.connection.execute(
                "SELECT * FROM ai_cache_entries"
            )
            for value in row
        )
        self.assertNotIn("private-input", payload)
        self.assertNotIn("stable", payload)

    def test_cross_connection_flight_follower_release_and_lease_steal(self) -> None:
        other = Database(self.path)
        try:
            base = datetime(2026, 8, 26, tzinfo=timezone.utc)
            self.assertEqual(
                self.database.acquire_ai_request_flight(
                    self.key.key,
                    owner_id="leader",
                    profile_id="recommendation",
                    lease_seconds=10,
                    now=base,
                ),
                "leader",
            )
            self.assertEqual(
                other.acquire_ai_request_flight(
                    self.key.key,
                    owner_id="follower",
                    profile_id="recommendation",
                    lease_seconds=10,
                    now=base + timedelta(seconds=1),
                ),
                "follower",
            )
            self.assertTrue(
                self.database.release_ai_request_flight(
                    self.key.key, owner_id="leader", status="complete", now=base
                )
            )
            self.assertEqual(
                other.wait_ai_request_flight(self.key.key, timeout_seconds=0),
                "complete",
            )

            stolen_key = request_key(correctness_inputs={"flight": "stale"})
            self.assertEqual(
                self.database.acquire_ai_request_flight(
                    stolen_key.key,
                    owner_id="dead",
                    profile_id="summary",
                    lease_seconds=1,
                    now=base,
                ),
                "leader",
            )
            self.assertEqual(
                other.acquire_ai_request_flight(
                    stolen_key.key,
                    owner_id="replacement",
                    profile_id="summary",
                    lease_seconds=10,
                    now=base + timedelta(seconds=2),
                ),
                "stolen",
            )
        finally:
            other.close()

    def test_ai_run_keeps_provider_and_local_cache_audit_separate(self) -> None:
        row = self.database.create_ai_run(
            "cache-hit-run",
            kind="recommendation",
            model="deepseek-v4-flash",
            profile_id="recommendation",
            local_cache_status="hit",
            local_cache_key=self.key.key,
            cache_source_run_id="source-run",
            cache_validation={"validator_version": "validator-v1", "accepted": True},
        )
        self.assertEqual(row["local_cache_status"], "hit")
        self.assertIsNone(row["cache_status"])
        self.assertEqual(json.loads(row["cache_validation_json"])["accepted"], True)
        status = self.database.ai_cache_status()
        self.assertEqual(status["exact_hits"], 1)
        self.assertEqual(status["provider_avoidance"], 1.0)

        self.database.create_ai_run(
            "bypass-run",
            kind="coaching",
            model="deepseek-v4-flash",
            profile_id="coaching",
            local_cache_status="bypass",
        )
        status = self.database.ai_cache_status()
        self.assertEqual(status["eligible_lookups"], 1)
        self.assertEqual(status["logical_requests"], 2)
        self.assertEqual(status["exact_hit_rate"], 1.0)
        self.assertEqual(status["provider_avoidance"], 0.5)


if __name__ == "__main__":
    unittest.main()
