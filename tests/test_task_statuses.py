from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.config import Paths
from tools.acm_agent.service import AcmService
from tools.acm_agent.storage import Database


def status_plan() -> dict[str, object]:
    problems = [
        ("t-platform-ac", "codeforces", "CF1A"),
        ("t-local-ac", "codeforces", "CF2A"),
        ("t-wa", "codeforces", "CF3A"),
        ("t-tle", "codeforces", "CF4A"),
        ("t-re", "codeforces", "CF5A"),
        ("t-mle", "codeforces", "CF6A"),
        ("t-ce", "codeforces", "CF7A"),
        ("t-abandoned", "codeforces", "CF8A"),
        ("t-active", "codeforces", "CF9A"),
        ("t-local-only", "codeforces", "CF10A"),
        ("t-not-started", "codeforces", "CF11A"),
        ("t-skipped-wa", "codeforces", "CF12A"),
        ("t-unknown", "codeforces", "CF13A"),
        ("t-luogu-ac", "luogu", "P1000"),
        ("t-luogu-local", "luogu", "P1001"),
        ("t-luogu-platform-wa", "luogu", "P1002"),
    ]
    return {
        "schema_version": 2,
        "plan_id": "status-plan",
        "title": "Task statuses",
        "description": "derived state fixture",
        "schedule_mode": "dated",
        "stages": [
            {
                "stage_key": "all",
                "topic": "all",
                "kind": "practice",
                "due_date": "2026-08-31",
                "tasks": [
                    {
                        "task_key": task_key,
                        "platform": platform,
                        "problem_id": problem_id,
                        "level": "A",
                        "tags": [],
                    }
                    for task_key, platform, problem_id in problems
                ],
            }
        ],
    }


class PlanTaskStatusesTest(unittest.TestCase):
    @staticmethod
    def submission(
        db: Database,
        platform: str,
        problem_id: str,
        submission_id: str,
        verdict: str,
        submitted_at: str,
    ) -> None:
        db.upsert_problem({"platform": platform, "problem_id": problem_id})
        db.upsert_submission(
            {
                "platform": platform,
                "problem_id": problem_id,
                "submission_id": submission_id,
                "verdict": verdict,
                "submitted_at": submitted_at,
            }
        )

    @staticmethod
    def closed_attempt(
        db: Database,
        platform: str,
        problem_id: str,
        result: str,
        stamp: str,
    ) -> None:
        attempt_id = db.start_attempt(platform, problem_id, started_at=stamp)
        db.close_attempt(attempt_id, result=result, closed_at=stamp)

    def test_task_statuses_merge_judge_workflow_and_skip_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = AcmService(root)
            document = status_plan()
            document["task_statuses"] = {"poison": {"workflow_status": "accepted"}}
            imported = service.plan_import(plan=document)
            self.assertNotIn("task_statuses", imported["plan"])
            persisted = json.loads(
                (root / ".acm/plans/status-plan.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("task_statuses", persisted)

            with Database(Paths.for_root(root).database) as db:
                db.skip_problem("codeforces", "1A", notes="historical skip")
                self.submission(db, "codeforces", "1A", "1", "OK", "2026-08-01T09:00:00Z")
                self.submission(
                    db, "codeforces", "1A", "2", "WRONG_ANSWER", "2026-08-02T09:00:00Z"
                )

                self.closed_attempt(db, "codeforces", "2A", "AC", "2026-08-01T09:00:00Z")
                self.closed_attempt(db, "codeforces", "2A", "WA", "2026-08-02T09:00:00Z")

                self.submission(
                    db, "codeforces", "3A", "3", "TIME_LIMIT_EXCEEDED", "2026-08-01T09:00:00Z"
                )
                self.submission(
                    db, "codeforces", "3A", "4", "WRONG_ANSWER", "2026-08-02T09:00:00Z"
                )
                self.submission(
                    db, "codeforces", "4A", "5", "WRONG_ANSWER", "2026-08-01T09:00:00Z"
                )
                self.closed_attempt(db, "codeforces", "4A", "TLE", "2026-08-02T09:00:00Z")
                self.submission(
                    db, "codeforces", "5A", "6", "RUNTIME_ERROR", "2026-08-02T09:00:00Z"
                )
                self.submission(
                    db, "codeforces", "6A", "7", "MEMORY_LIMIT_EXCEEDED", "2026-08-02T09:00:00Z"
                )
                self.submission(
                    db, "codeforces", "7A", "8", "COMPILATION_ERROR", "2026-08-02T09:00:00Z"
                )
                self.closed_attempt(
                    db, "codeforces", "8A", "ABANDONED", "2026-08-02T09:00:00Z"
                )
                db.start_attempt("codeforces", "9A", started_at="2026-08-02T09:00:00Z")
                db.upsert_problem({"platform": "codeforces", "problem_id": "10A"})
                db.upsert_local_file(root / "CF10A.cpp", "codeforces", "10A")
                db.upsert_problem({"platform": "codeforces", "problem_id": "11A"})

                self.submission(
                    db, "codeforces", "12A", "9", "WRONG_ANSWER", "2026-08-02T09:00:00Z"
                )
                db.skip_problem("codeforces", "12A")

                self.submission(db, "luogu", "P1000", "10", "AC", "2026-08-02T09:00:00Z")
                self.submission(
                    db, "luogu", "P1001", "11", "WRONG_ANSWER", "2026-08-03T09:00:00Z"
                )
                self.closed_attempt(db, "luogu", "P1001", "RE", "2026-08-02T09:00:00Z")
                self.submission(
                    db, "luogu", "P1002", "12", "WRONG_ANSWER", "2026-08-03T09:00:00Z"
                )

            detail = service.plan_detail("status-plan")
            statuses = detail["task_statuses"]
            self.assertEqual(
                set(statuses),
                {
                    "t-platform-ac", "t-local-ac", "t-wa", "t-tle", "t-re",
                    "t-mle", "t-ce", "t-abandoned", "t-active", "t-local-only",
                    "t-not-started", "t-skipped-wa", "t-luogu-ac",
                    "t-luogu-local", "t-luogu-platform-wa", "t-unknown",
                },
            )

            expected_judge = {
                "t-platform-ac": "AC",
                "t-local-ac": "AC",
                "t-wa": "WA",
                "t-tle": "TLE",
                "t-re": "RE",
                "t-mle": "MLE",
                "t-ce": "CE",
                "t-abandoned": "ABANDONED",
                "t-skipped-wa": "WA",
                "t-luogu-ac": "AC",
                "t-luogu-local": "RE",
                "t-luogu-platform-wa": None,
            }
            for task_key, judge_result in expected_judge.items():
                with self.subTest(task_key=task_key):
                    self.assertEqual(statuses[task_key]["judge_result"], judge_result)

            for task_key in ("t-platform-ac", "t-local-ac", "t-luogu-ac"):
                self.assertEqual(statuses[task_key]["workflow_status"], "accepted")
                self.assertFalse(statuses[task_key]["skipped"])
            self.assertEqual(statuses["t-platform-ac"]["evidence_source"], "platform")
            self.assertEqual(statuses["t-local-ac"]["evidence_source"], "local")

            self.assertEqual(statuses["t-active"]["workflow_status"], "active")
            self.assertIsNone(statuses["t-active"]["judge_result"])
            self.assertEqual(statuses["t-local-only"]["workflow_status"], "local_only")
            self.assertEqual(statuses["t-not-started"]["workflow_status"], "not_started")
            self.assertEqual(statuses["t-unknown"]["workflow_status"], "unknown")

            skipped = statuses["t-skipped-wa"]
            self.assertEqual(skipped["judge_result"], "WA")
            self.assertEqual(skipped["workflow_status"], "skipped")
            self.assertTrue(skipped["skipped"])
            self.assertEqual(skipped["evidence_source"], "platform")

            self.assertEqual(statuses["t-luogu-local"]["evidence_source"], "local")
            self.assertEqual(statuses["t-luogu-local"]["workflow_status"], "attempted")
            self.assertEqual(statuses["t-luogu-platform-wa"]["workflow_status"], "not_started")
            self.assertEqual(statuses["t-luogu-platform-wa"]["evidence_source"], "none")
            self.assertTrue(all("updated_at" in status for status in statuses.values()))
            self.assertNotIn("task_statuses", detail["plan"])


if __name__ == "__main__":
    unittest.main()
