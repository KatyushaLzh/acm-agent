from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.platforms import (
    CodeforcesClient,
    LuoguClient,
    RemoteAPIError,
    ResponseShapeError,
    parse_luogu_passed,
    parse_luogu_problem,
    parse_luogu_problems,
    parse_luogu_tags,
    parse_luogu_user,
    sync_codeforces,
    sync_luogu,
    preview_plan_task_tags,
)
from tools.acm_agent.storage import Database, SCHEMA_VERSION


FIXTURES = Path(__file__).parent / "fixtures" / "platforms"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Router:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        key = url.rsplit("/", 1)[-1]
        value = self.routes[key]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(dict(params or {}))
        return value


class StorageTests(unittest.TestCase):
    def test_migration_and_repository_queries(self):
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            self.assertEqual(db.connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            expected = {
                "accounts", "problems", "submissions", "local_files", "attempts",
                "sync_state", "recommendation_runs",
            }
            tables = {
                row[0] for row in db.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(expected <= tables)
            db.upsert_problem({"platform": "codeforces", "problem_id": "1A"})
            db.upsert_local_file("2026/8/3/CF1A.cpp", "codeforces", "1A")
            attempt_id = db.start_attempt("codeforces", "1A")
            db.close_attempt(attempt_id, result="WA", hint_level=1)
            self.assertEqual(db.problem_status("codeforces", "1A"), "attempted")
            self.assertEqual(len(db.local_files()), 1)
            self.assertEqual(db.attempts("codeforces", "1A")[0]["result"], "WA")


class CodeforcesTests(unittest.TestCase):
    def test_failed_payload_and_throttle(self):
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        router = Router({
            "user.info": json.loads(fixture("cf_user.json")),
            "user.status": {"status": "FAILED", "comment": "limit exceeded"},
        })
        client = CodeforcesClient(router, sleep=sleep, monotonic=lambda: clock[0])
        client.user_info("fixture_user")
        with self.assertRaises(RemoteAPIError):
            client.user_status_page("fixture_user", 1, 10)
        self.assertEqual(sleeps, [2.1])

    def test_incremental_stops_at_known_submission(self):
        router = Router({
            "user.status": json.loads(fixture("cf_status_new.json")),
        })
        client = CodeforcesClient(router, sleep=lambda _: None, throttle_seconds=0)
        rows = client.new_submissions("fixture_user", {"11"}, page_size=2)
        self.assertEqual([row["id"] for row in rows], [12])
        self.assertEqual(len(router.calls), 1)

    def test_sync_failure_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            db.upsert_problem({"platform": "codeforces", "problem_id": "100A"})
            db.upsert_submission({
                "platform": "codeforces", "submission_id": "11", "problem_id": "100A",
                "verdict": "OK",
            })
            db.connection.commit()
            router = Router({"user.info": RuntimeError("offline")})
            result = sync_codeforces(db, "fixture_user", CodeforcesClient(router))
            self.assertEqual(result.status, "failed")
            self.assertEqual(len(db.submissions("codeforces")), 1)
            self.assertEqual(db.problem_status("codeforces", "100A"), "accepted")

    def test_full_sync_commits_account_catalog_and_new_submission(self):
        router = Router({
            "user.info": json.loads(fixture("cf_user.json")),
            "user.status": json.loads(fixture("cf_status_new.json")),
            "problemset.problems": json.loads(fixture("cf_problemset.json")),
        })
        client = CodeforcesClient(router, sleep=lambda _: None, throttle_seconds=0)
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            result = sync_codeforces(db, "fixture_user", client, refresh_catalog=True)
            self.assertEqual(result.status, "fresh")
            self.assertEqual(db.account("codeforces")["rating"], 1732)
            self.assertEqual(db.problem_status("codeforces", "100B"), "accepted")
            self.assertEqual(len(db.problems("codeforces")), 2)

    def test_catalog_failure_is_partial_and_keeps_new_submissions(self):
        router = Router({
            "user.info": json.loads(fixture("cf_user.json")),
            "user.status": json.loads(fixture("cf_status_new.json")),
            "problemset.problems": RuntimeError("catalog offline"),
        })
        client = CodeforcesClient(router, sleep=lambda _: None, throttle_seconds=0)
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            result = sync_codeforces(db, "fixture_user", client, refresh_catalog=True)
            self.assertEqual(result.status, "partial")
            self.assertTrue(result.warnings)
            self.assertEqual(db.problem_status("codeforces", "100B"), "accepted")
            self.assertEqual(len(db.submissions("codeforces")), 2)


class LuoguTests(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(parse_luogu_passed(fixture("luogu_practice.html")), {"P1000", "P3372"})
        with self.assertRaises(ResponseShapeError):
            parse_luogu_passed(fixture("luogu_hidden.html"))
        tags = parse_luogu_tags(fixture("luogu_tags.json"))
        self.assertEqual(tags[3], "线段树")
        problems = parse_luogu_problems(fixture("luogu_problems.json"), tags)
        self.assertEqual([row["problem_id"] for row in problems], ["P1000", "P3372"])
        self.assertEqual(problems[1]["tags"], ["线段树"])
        self.assertEqual(parse_luogu_user(fixture("luogu_user.json"), 42)["name"], "fixture_luogu")

    def test_public_problem_page_requires_exact_unique_pid(self):
        tags = parse_luogu_tags(fixture("luogu_tags.json"))
        problem = parse_luogu_problem(fixture("luogu_problems.json"), "P3372", tags)
        self.assertEqual(problem["tags"], ["线段树"])
        with self.assertRaisesRegex(ResponseShapeError, "0 exact matches"):
            parse_luogu_problem(fixture("luogu_problems.json"), "P9999", tags)
        duplicate = {
            "data": [
                {"pid": "P3372", "title": "one", "tags": [3]},
                {"pid": "P3372", "title": "two", "tags": [3]},
            ]
        }
        with self.assertRaisesRegex(ResponseShapeError, "2 exact matches"):
            parse_luogu_problem(duplicate, "P3372", tags)
        with self.assertRaisesRegex(ResponseShapeError, "unknown tag id 3"):
            parse_luogu_problem(fixture("luogu_problems.json"), "P3372", {})

    def test_plan_tag_preview_keeps_partial_results(self):
        class FakeCf:
            def problemset(self):
                payload = json.loads(fixture("cf_problemset.json"))["result"]
                return payload["problems"], payload["problemStatistics"]

        class FakeLuogu:
            def tags(self):
                return parse_luogu_tags(fixture("luogu_tags.json"))

            def problem(self, problem_id, *, tag_names=None):
                if problem_id == "P9999":
                    raise RuntimeError("fixture partial failure")
                return parse_luogu_problem(
                    fixture("luogu_problems.json"), problem_id, tag_names
                )

        tasks = [
            {"task_key": "cf-cache", "platform": "codeforces", "problem_id": "CF1A", "name": "cached", "tags": []},
            {"task_key": "cf-refresh", "platform": "codeforces", "problem_id": "CF100B", "name": "new", "tags": []},
            {"task_key": "lg-ok", "platform": "luogu", "problem_id": "P3372", "name": "tree", "tags": []},
            {"task_key": "lg-fail", "platform": "luogu", "problem_id": "P9999", "name": "missing", "tags": []},
            {"task_key": "skip", "platform": "luogu", "problem_id": "P1000", "name": "tagged", "tags": ["existing"]},
        ]
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            db.upsert_problem({
                "platform": "codeforces", "problem_id": "1A", "tags": ["math"]
            })
            result = preview_plan_task_tags(
                db,
                tasks,
                codeforces_client=FakeCf(),
                luogu_client=FakeLuogu(),
            )
            proposals = {row["task_key"]: row for row in result["proposals"]}
            self.assertEqual(proposals["cf-cache"]["source"], "sqlite_catalog")
            self.assertEqual(proposals["cf-refresh"]["suggested_tags"], ["greedy"])
            self.assertEqual(proposals["lg-ok"]["source"], "luogu_problem")
            self.assertEqual(proposals["lg-fail"]["source"], "unresolved")
            self.assertEqual(result["coverage"]["suggested"], 3)
            self.assertEqual(result["coverage"]["skipped_nonempty"], 1)
            self.assertEqual(result["errors"][0]["problem_id"], "P9999")

    def test_partial_catalog_failure_keeps_public_accepts(self):
        router = Router({
            "practice": fixture("luogu_practice.html"),
            "zh-CN": RuntimeError("tag endpoint unavailable"),
        })
        client = LuoguClient(router)
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            result = sync_luogu(db, 42, client, refresh_catalog=True)
            self.assertEqual(result.status, "partial")
            self.assertEqual(db.problem_status("luogu", "P3372"), "accepted")
            self.assertEqual(len(db.submissions("luogu")), 2)

    def test_directed_candidate_queries_keep_filters(self):
        router = Router({
            "practice": fixture("luogu_practice.html"),
            "zh-CN": fixture("luogu_tags.json"),
            "list": fixture("luogu_problems.json"),
        })
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            result = sync_luogu(
                db,
                42,
                LuoguClient(router),
                refresh_catalog=True,
                candidate_queries=[{"page": 2, "difficulty": 4, "tag": 3}],
            )
            self.assertEqual(result.status, "fresh")
            problem_calls = [params for url, params in router.calls if url.endswith("/problem/list")]
            self.assertEqual(problem_calls[0]["page"], 2)
            self.assertEqual(problem_calls[0]["difficulty"], 4)
            self.assertEqual(problem_calls[0]["tag"], 3)

    def test_hidden_profile_failure_preserves_old_accepts(self):
        router = Router({"practice": fixture("luogu_hidden.html")})
        with tempfile.TemporaryDirectory() as tmp, Database(Path(tmp) / "state.db") as db:
            db.upsert_problem({"platform": "luogu", "problem_id": "P1000"})
            db.upsert_submission({
                "platform": "luogu", "submission_id": "accepted:P1000",
                "problem_id": "P1000", "verdict": "AC",
            })
            db.connection.commit()
            result = sync_luogu(db, 42, LuoguClient(router), refresh_catalog=False)
            self.assertEqual(result.status, "failed")
            self.assertEqual(len(db.submissions("luogu")), 1)


if __name__ == "__main__":
    unittest.main()
