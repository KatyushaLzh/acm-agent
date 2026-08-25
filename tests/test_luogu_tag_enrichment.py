from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.platforms import enrich_luogu_accepted_problem_tags, sync_luogu
from tools.acm_agent.storage import Database


class FakeLuoguClient:
    def __init__(self, failures: set[str] | None = None):
        self.failures = failures or set()
        self.problem_calls: list[str] = []
        self.tags_calls = 0

    def tags(self) -> dict[int, str]:
        self.tags_calls += 1
        return {1: "动态规划"}

    def problem(self, problem_id: str, *, tag_names=None):
        self.problem_calls.append(problem_id)
        if problem_id in self.failures:
            raise RuntimeError(f"public page unavailable for {problem_id}")
        return {
            "platform": "luogu",
            "problem_id": problem_id,
            "name": f"Problem {problem_id}",
            "url": f"https://www.luogu.com.cn/problem/{problem_id}",
            "difficulty": 3,
            "rating": None,
            "tags": ["动态规划"],
            "source": {"pid": problem_id, "tags": [1]},
        }


class SyncingFakeLuoguClient(FakeLuoguClient):
    def __init__(self, passed: set[str] | None = None):
        super().__init__()
        self.passed = passed or {"P1000"}

    def practice(self, uid):
        return self.passed


def seed_accepted(db: Database, problem_ids: list[str], *, tagged: set[str] | None = None) -> None:
    tagged = tagged or set()
    with db.atomic():
        for problem_id in problem_ids:
            db.upsert_problem(
                {
                    "platform": "luogu",
                    "problem_id": problem_id,
                    "tags": ["已有标签"] if problem_id in tagged else [],
                }
            )
            db.upsert_submission(
                {
                    "platform": "luogu",
                    "submission_id": f"accepted:{problem_id}",
                    "problem_id": problem_id,
                    "verdict": "AC",
                }
            )


class LuoguTagEnrichmentTests(unittest.TestCase):
    def test_batch_limit_cursor_and_cached_metadata(self):
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            problem_ids = [f"P{1000 + index}" for index in range(55)]
            seed_accepted(db, problem_ids)
            client = FakeLuoguClient()

            first = enrich_luogu_accepted_problem_tags(db, client, batch_size=100)
            self.assertEqual(first["attempted"], 50)
            self.assertEqual(first["resolved"], 50)
            self.assertEqual(first["remaining"], 5)
            self.assertEqual(first["cursor"], "P1049")
            self.assertEqual(client.problem_calls, problem_ids[:50])

            stored = db.connection.execute(
                "SELECT tags_json,source_json FROM problems WHERE platform='luogu' AND problem_id='P1000'"
            ).fetchone()
            self.assertEqual(json.loads(stored["tags_json"]), ["动态规划"])
            self.assertEqual(json.loads(stored["source_json"])["pid"], "P1000")
            metadata = json.loads(db.sync_state("luogu")["metadata_json"])
            self.assertEqual(metadata["tag_enrichment_cursor"], "P1049")

            second = enrich_luogu_accepted_problem_tags(db, client)
            self.assertEqual(second["attempted"], 5)
            self.assertEqual(second["remaining"], 0)
            self.assertEqual(client.problem_calls[-5:], problem_ids[50:])
            self.assertEqual(client.tags_calls, 1)

    def test_partial_failure_advances_and_rotates(self):
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            seed_accepted(db, ["P1000", "P1001", "P1002"])
            first_client = FakeLuoguClient({"P1000"})

            first = enrich_luogu_accepted_problem_tags(db, first_client, batch_size=2)
            self.assertEqual(first["attempted"], 2)
            self.assertEqual(first["resolved"], 1)
            self.assertEqual(first["failed"], 1)
            self.assertEqual(first["remaining"], 2)
            self.assertEqual(first["cursor"], "P1001")
            self.assertEqual(
                first["errors"][-1],
                {
                    "platform": "luogu",
                    "problem_id": "P1000",
                    "message": "public page unavailable for P1000",
                },
            )

            second_client = FakeLuoguClient()
            second = enrich_luogu_accepted_problem_tags(db, second_client, batch_size=2)
            self.assertEqual(second_client.problem_calls, ["P1002", "P1000"])
            self.assertEqual(second["resolved"], 2)
            self.assertEqual(second["remaining"], 0)

    def test_full_mode_attempts_every_missing_problem_in_one_call(self):
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            problem_ids = [f"P{1000 + index}" for index in range(55)]
            seed_accepted(db, problem_ids)
            client = FakeLuoguClient()

            result = enrich_luogu_accepted_problem_tags(
                db, client, batch_size=1, full=True
            )

            self.assertEqual(result["attempted"], 55)
            self.assertEqual(result["resolved"], 55)
            self.assertEqual(result["remaining"], 0)
            self.assertEqual(client.problem_calls, problem_ids)

    def test_only_distinct_accepted_problems_without_existing_tags_are_selected(self):
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            seed_accepted(db, ["P1000", "P1001", "P1003"], tagged={"P1001"})
            with db.atomic():
                db.replace_problem_tag_overrides(
                    "luogu", "P1000", additions=["人工有效标签"]
                )
                db.upsert_submission(
                    {
                        "platform": "luogu",
                        "submission_id": "duplicate:P1003",
                        "problem_id": "P1003",
                        "verdict": "AC",
                    }
                )
                db.upsert_problem({"platform": "luogu", "problem_id": "P1002"})
                db.upsert_submission(
                    {
                        "platform": "luogu",
                        "submission_id": "failed:P1002",
                        "problem_id": "P1002",
                        "verdict": "WA",
                    }
                )
            client = FakeLuoguClient()

            result = enrich_luogu_accepted_problem_tags(db, client)
            self.assertEqual(client.problem_calls, ["P1003"])
            self.assertEqual(result["attempted"], 1)
            self.assertEqual(result["remaining"], 0)

    def test_sync_luogu_invokes_enrichment_and_exposes_progress(self):
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            client = SyncingFakeLuoguClient()
            result = sync_luogu(db, 42, client, refresh_catalog=False)

            self.assertEqual(result.status, "fresh")
            self.assertEqual(client.problem_calls, ["P1000"])
            self.assertEqual(
                result.tag_enrichment,
                {
                    "attempted": 1,
                    "resolved": 1,
                    "failed": 0,
                    "remaining": 0,
                    "cursor": "P1000",
                    "errors": [],
                },
            )

    def test_sync_luogu_uses_full_enrichment_not_the_incremental_cap(self):
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            problem_ids = {f"P{1000 + index}" for index in range(55)}
            client = SyncingFakeLuoguClient(problem_ids)

            result = sync_luogu(db, 42, client, refresh_catalog=False)

            self.assertEqual(result.status, "fresh")
            self.assertEqual(result.tag_enrichment["attempted"], 55)
            self.assertEqual(result.tag_enrichment["remaining"], 0)
            self.assertEqual(set(client.problem_calls), problem_ids)


if __name__ == "__main__":
    unittest.main()
