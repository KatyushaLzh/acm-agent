"""Training close retries and review evidence reset boundaries."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest
from unittest.mock import patch

from tools.acm_agent.service import AcmService
from tools.acm_agent.storage import Database


class ReviewConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = AcmService(Path(self.temporary.name))
        self.service.setup("fixture", "42", skip_validate=True)
        self.close_args = dict(result="AC", minutes=20, hint_level=0, failure="modeling")

    def test_report_failure_returns_committed_result_and_retry_repairs_only_report(self) -> None:
        attempt_id = self.service.start("CF1A")["attempt_id"]
        original_write = Path.write_text

        def fail_report(path: Path, *args, **kwargs):
            if path.name.startswith("archive-candidate-"):
                raise PermissionError("fixture report write denied")
            return original_write(path, *args, **kwargs)

        with patch.object(Path, "write_text", fail_report):
            first = self.service.close("CF1A", attempt_id=attempt_id, **self.close_args)
        self.assertTrue(first["ok"])
        self.assertTrue(first["committed"])
        self.assertFalse(first["replayed"])
        self.assertEqual(first["report_status"], "failed")
        self.assertIsNone(first["archive_candidate"])
        self.assertEqual(first["warnings"][0]["code"], "archive_report_write_failed")
        self.assertEqual(first["warnings"][0]["retry_attempt_id"], attempt_id)

        second = self.service.close("CF1A", attempt_id=attempt_id, **self.close_args)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["report_status"], "written")
        self.assertEqual(second["warnings"], [])
        self.assertEqual(second["close"], first["close"])
        self.assertEqual(json.loads(Path(second["archive_candidate"]).read_text("utf-8")), first["close"])
        with Database(self.service.paths.database) as db:
            self.assertEqual(len(db.attempts()), 1)
            self.assertEqual(db.review_queue_entry("codeforces", "1A")["review_stage"], 1)

    def test_retry_old_attempt_does_not_close_a_new_active_training(self) -> None:
        first_id = self.service.start("CF1A")["attempt_id"]
        first = self.service.close("CF1A", attempt_id=first_id, **self.close_args)
        second_id = self.service.start("CF1A")["attempt_id"]
        replay = self.service.close("CF1A", attempt_id=first_id, **self.close_args)
        self.assertEqual(replay["close"], first["close"])
        with Database(self.service.paths.database) as db:
            self.assertTrue(db.connection.execute("SELECT active FROM attempts WHERE id=?", (second_id,)).fetchone()[0])
            self.assertEqual(db.review_queue_entry("codeforces", "1A")["review_stage"], 1)
        second = self.service.close("CF1A", attempt_id=second_id, **self.close_args)
        self.assertFalse(second["replayed"])
        self.assertEqual(second["close"]["review_stage"], 2)

    def test_legacy_unbound_close_can_still_create_distinct_trainings(self) -> None:
        first = self.service.close("CF1A", **self.close_args)
        second = self.service.close("CF1A", **self.close_args)
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(second["close"]["review_stage"], 2)

    def test_concurrent_retries_commit_the_identified_attempt_once(self) -> None:
        attempt_id = self.service.start("CF1A")["attempt_id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.service.close, "CF1A", attempt_id=attempt_id, **self.close_args)
                       for _ in range(2)]
            results = [future.result() for future in futures]
        self.assertEqual(sorted(result["replayed"] for result in results), [False, True])
        self.assertEqual(results[0]["close"], results[1]["close"])
        with Database(self.service.paths.database) as db:
            self.assertEqual(len(db.attempts()), 1)
            self.assertEqual(db.review_queue_entry("codeforces", "1A")["review_stage"], 1)

    def test_explicit_close_rejects_missing_mismatched_and_changed_attempts(self) -> None:
        first = self.service.close("CF1A", **self.close_args)
        for attempt_id in (0, True, "1", 999):
            with self.subTest(attempt_id=attempt_id), self.assertRaises(ValueError):
                self.service.close("CF1A", attempt_id=attempt_id, **self.close_args)
        with self.assertRaisesRegex(ValueError, "不匹配"):
            self.service.close("CF1B", attempt_id=first["attempt_id"], **self.close_args)
        with self.assertRaisesRegex(ValueError, "结果不同"):
            self.service.close("CF1A", attempt_id=first["attempt_id"], **{**self.close_args, "result": "WA"})
        with Database(self.service.paths.database) as db:
            self.assertEqual(len(db.attempts()), 1)
            self.assertEqual(db.review_queue_entry("codeforces", "1A")["review_stage"], 1)

    def test_remove_and_clear_preserve_failures_completed_after_reset(self) -> None:
        for clear_all in (False, True):
            with self.subTest(clear_all=clear_all):
                problem = "CF1B" if clear_all else "CF1A"
                db_id = "1B" if clear_all else "1A"
                self.service.close(problem, result="AC", minutes=10, hint_level=0)
                self.service.review_queue_add(problem, review_due="2026-09-09")
                active_id = self.service.start(problem)["attempt_id"]
                if clear_all:
                    self.service.review_queue_clear(confirm=True)
                else:
                    self.service.review_queue_remove(problem)
                with Database(self.service.paths.database) as db:
                    self.assertLess(db.review_reset_attempt_id("codeforces", db_id), active_id)
                for number in range(2):
                    self.service.close(problem, result="WA", minutes=10, hint_level=0)
                    with Database(self.service.paths.database) as db:
                        queued = db.review_queue_entry("codeforces", db_id)
                    self.assertEqual(queued is not None, number == 1)


if __name__ == "__main__":
    unittest.main()
