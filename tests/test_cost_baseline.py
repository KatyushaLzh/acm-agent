from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.acm_agent.ai_telemetry import estimate_cost, load_price_catalog
from tools.acm_agent.cost_baseline import WORKLOAD_PATH, build_report, read_runs


class CostBaselineTests(unittest.TestCase):
    @staticmethod
    def _create_runs_database(path: Path, *, run_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA user_version = 24")
            connection.execute(
                """
                CREATE TABLE ai_runs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    model TEXT,
                    request_summary_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    finish_reason TEXT,
                    usage_json TEXT NOT NULL,
                    error_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO ai_runs (
                    id, kind, model, request_summary_json, status, finish_reason,
                    usage_json, error_json, created_at, completed_at
                ) VALUES (?, 'recommendation', 'test-model', '{}', 'complete',
                          'stop', '{}', '{}', '2026-08-27T00:00:00+00:00',
                          '2026-08-27T00:00:01+00:00')
                """,
                (run_id,),
            )
            connection.commit()
        finally:
            connection.close()

    def test_read_runs_encodes_special_characters_in_read_only_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory_name in ("hash#directory", "space directory", "中文目录"):
                with self.subTest(directory_name=directory_name):
                    database = root / directory_name / "state # 中文.db"
                    self._create_runs_database(database, run_id=directory_name)

                    schema_version, rows = read_runs(database)

                    self.assertEqual(schema_version, 24)
                    self.assertEqual([row["id"] for row in rows], [directory_name])

    def test_read_runs_missing_database_fails_without_creating_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "missing # 中文" / "state.db"

            with self.assertRaises(sqlite3.OperationalError):
                read_runs(database)

            self.assertFalse(database.exists())

    def test_price_estimate_is_versioned_and_cache_aware(self) -> None:
        estimate = estimate_cost(
            model="deepseek-v4-flash",
            usage={
                "prompt_tokens": 1000, "completion_tokens": 100,
                "prompt_cache_hit_tokens": 400, "prompt_cache_miss_tokens": 600,
            },
            created_at="2026-08-25T02:00:00+00:00",
        )
        self.assertEqual(estimate["status"], "known")
        self.assertEqual(estimate["rate_band"], "peak")
        self.assertEqual(estimate["currency"], "CNY")
        self.assertEqual(estimate["price_version"], "deepseek-2026-08-26-cny-v1")
        self.assertEqual(estimate["amount_decimal"], "0.002740000000")
        self.assertGreater(estimate["cache_savings"], 0)

    def test_incomplete_usage_produces_unknown_cost(self) -> None:
        estimate = estimate_cost(
            model="deepseek-v4-flash", usage={},
            created_at="2026-08-25T02:00:00+00:00",
        )
        self.assertEqual(estimate["status"], "unknown")
        self.assertEqual(estimate["unknown_reason"], "usage_incomplete")

    def test_fixed_workload_covers_all_profiles_without_copying_prompts(self) -> None:
        workload = json.loads(WORKLOAD_PATH.read_text(encoding="utf-8"))
        rows = []
        specs = [
            ("recommendation", {}), ("plan_import", {"mode": "organize"}),
            ("plan_import", {"mode": "generate"}), ("coaching", {}),
            ("patch", {}), ("markdown_summary", {}),
        ]
        for index, (kind, request) in enumerate(specs):
            request["private_prompt"] = f"secret-{index}"
            rows.append({
                "id": f"run-{index}", "kind": kind, "model": "deepseek-v4-flash",
                "request_summary_json": json.dumps(request), "status": "complete",
                "finish_reason": "stop",
                "usage_json": json.dumps({
                    "prompt_tokens": 10, "completion_tokens": 2,
                    "prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 9,
                    "provider_requests": 1, "protocol_repairs": 0,
                }),
                "error_json": "{}", "telemetry_json": "{}",
                "created_at": "2026-08-25T02:00:00+00:00",
                "completed_at": "2026-08-25T02:00:01+00:00",
            })
        safe_runs, summary = build_report(
            rows, workload=workload, price_catalog=load_price_catalog()
        )
        self.assertTrue(summary["coverage"]["complete"])
        self.assertEqual(summary["provider_requests"]["known_total"], 6)
        self.assertEqual(summary["protocol_repairs"]["known_total"], 0)
        self.assertNotIn("secret-", json.dumps(safe_runs))


if __name__ == "__main__":
    unittest.main()
