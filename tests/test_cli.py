from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from tools.acm_agent.cli import main
from tools.acm_agent.config import Paths
from tools.acm_agent.storage import Database
from tools.acm_agent.platforms import SyncResult


REPO_ROOT = Path(__file__).resolve().parents[1]


class CliEndToEndTests(unittest.TestCase):
    def run_json(self, root: Path, *args: str, expected: int = 0) -> dict:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([*args, "--json"], root=root)
        self.assertEqual(code, expected, output.getvalue())
        return json.loads(output.getvalue())

    def prepare_root(self, root: Path) -> None:
        target = root / "training" / "data-structures-30d"
        target.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/plan.json", target / "plan.json")
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/README.md", target / "README.md")

    def test_human_sync_reports_bounded_progress_on_stderr(self) -> None:
        class FakeService:
            def sync(self, platform="all", *, force=False, _progress_callback=None):
                for step in range(1, 101):
                    _progress_callback(
                        {
                            "phase": "catalog",
                            "platform": "luogu",
                            "step": step,
                            "total": 100,
                            "message": f"洛谷题库 {step}/100 页",
                        }
                    )
                return {
                    "ok": True,
                    "results": [
                        {
                            "platform": "luogu",
                            "status": "fresh",
                            "accepted": 1,
                            "submissions": 1,
                        }
                    ],
                }

        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "tools.acm_agent.cli._service", return_value=FakeService()
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["sync", "--platform", "luogu"], root=Path(temp))

        self.assertEqual(code, 0)
        self.assertIn("luogu: fresh", stdout.getvalue())
        progress_lines = [line for line in stderr.getvalue().splitlines() if line]
        self.assertGreaterEqual(len(progress_lines), 2)
        self.assertLessEqual(len(progress_lines), 25)
        self.assertIn("洛谷题库 100/100 页", progress_lines[-1])

    def test_verify_cli_rejects_non_positive_runtime_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for arguments in (
                ["verify", "CF1A", "--timeout", "0"],
                ["verify", "CF1A", "--timeout", "-1"],
                ["verify", "CF1A", "--stress-iterations", "0"],
                ["verify", "CF1A", "--stress-iterations", "-1"],
            ):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as raised:
                    main(arguments, root=root)
                self.assertEqual(raised.exception.code, 2)

    def test_init_next_start_close_review_without_archiving(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            algorithms = root / "algorithms.md"
            tricks = root / "tricks.md"
            algorithms.write_text("algorithms sentinel\n", encoding="utf-8")
            tricks.write_text("tricks sentinel\n", encoding="utf-8")

            initialized = self.run_json(
                root,
                "init",
                "--codeforces", "fixture_cf",
                "--luogu", "42",
                "--target-rating", "1800",
                "--skip-validate",
                "--non-interactive",
            )
            self.assertFalse(initialized["validated"])

            recommended = self.run_json(root, "next", "--count", "3", "--mode", "new")
            self.assertEqual(len(recommended["recommendations"]), 3)
            self.assertTrue(all("breakdown" in row for row in recommended["recommendations"]))
            self.assertEqual(recommended["recommendation_basis"], "plan_only")
            self.assertTrue(recommended["warnings"])

            started = self.run_json(root, "start", "CF1A")
            source = Path(started["source"])
            self.assertTrue(source.is_file())
            source.write_text(
                "#include <iostream>\nint main(){std::cout << 1 << '\\n';}\n",
                encoding="utf-8",
            )
            case_dir = root / ".acm/cases/CF1A"
            case_dir.mkdir(parents=True)
            (case_dir / "sample.in").write_text("", encoding="utf-8")
            (case_dir / "sample.out").write_text("1\n", encoding="utf-8")
            verified = self.run_json(root, "verify", "CF1A")
            self.assertTrue(verified["passed"])

            closed = self.run_json(
                root,
                "close", "CF1A",
                "--result", "AC",
                "--minutes", "25",
                "--hint-level", "2",
                "--failure", "modeling",
                "--notes", "missed invariant",
                "--non-interactive",
            )
            self.assertEqual(closed["status"], "accepted")
            self.assertIsNotNone(closed["review_due"])
            self.assertTrue(Path(closed["archive_candidate"]).is_file())
            self.assertEqual(algorithms.read_text(encoding="utf-8"), "algorithms sentinel\n")
            self.assertEqual(tricks.read_text(encoding="utf-8"), "tricks sentinel\n")

            review = self.run_json(root, "review", "week")
            self.assertEqual(review["results"]["AC"], 1)
            self.assertTrue(Path(review["report"]).is_file())

    def test_imported_cpp_is_local_only_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            source = root / "2026/8/3/P1000.cpp"
            source.parent.mkdir(parents=True)
            source.write_text("int main(){}\n", encoding="utf-8")
            initialized = self.run_json(
                root, "init", "--codeforces", "fixture_cf", "--luogu", "42",
                "--skip-validate", "--non-interactive",
            )
            self.assertEqual(initialized["local_files_imported"], 1)
            with Database(Paths.for_root(root).database) as db:
                self.assertEqual(db.problem_status("luogu", "P1000"), "local_only")

    def test_plan_check_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            payload = self.run_json(root, "plan", "check")
            self.assertTrue(payload["ok"])

    def test_skip_unskip_and_global_list_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            self.run_json(
                root,
                "init",
                "--codeforces", "fixture_cf",
                "--luogu", "42",
                "--skip-validate",
                "--non-interactive",
            )

            skipped = self.run_json(
                root,
                "skip", "CF1A",
                "--notes", "mastered",
                "--context-json", '{"plan_id":"cli-plan"}',
            )
            self.assertEqual(skipped["status"], "skipped")
            self.assertEqual(skipped["disposition"], "skipped_mastered")
            self.assertEqual(skipped["reason"], "idea_clear_without_editorial")

            repeated = self.run_json(root, "skip", "CF1A", "--notes", "updated")
            self.assertEqual(repeated["status"], "skipped")
            listed = self.run_json(root, "skipped")
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["problems"][0]["problem_id"], "CF1A")
            with Database(Paths.for_root(root).database) as db:
                disposition = db.problem_disposition("codeforces", "1A")
                self.assertEqual(disposition["source"], "cli")
                self.assertEqual(disposition["notes"], "updated")
                self.assertEqual(
                    [row["action"] for row in db.problem_disposition_events("codeforces", "1A")],
                    ["skip", "skip"],
                )

            removed = self.run_json(root, "unskip", "CF1A", "--notes", "restore")
            duplicate = self.run_json(root, "unskip", "CF1A")
            self.assertTrue(removed["unskipped"])
            self.assertFalse(duplicate["unskipped"])
            self.assertEqual(self.run_json(root, "skipped")["problems"], [])

    def test_plan_management_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            self.run_json(
                root, "init", "--codeforces", "fixture_cf", "--luogu", "42",
                "--skip-validate", "--non-interactive",
            )
            plan_file = root / "example-plan.json"
            template = self.run_json(root, "plan", "template", "--output", str(plan_file))
            self.assertTrue(plan_file.is_file())
            document = template["plan"]
            document["plan_id"] = "cli-plan"
            document["title"] = "CLI 题单"
            plan_file.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            imported = self.run_json(root, "plan", "import", str(plan_file))
            self.assertEqual(imported["plan_id"], "cli-plan")
            with Database(Paths.for_root(root).database) as db:
                db.upsert_problem(
                    {"platform": "codeforces", "problem_id": "1A", "tags": ["math"]}
                )
            proposal_file = root / "tag-proposals.json"
            preview = self.run_json(
                root,
                "plan", "tags", "preview", "cli-plan",
                "--mode", "cleanup", "--no-refresh", "--output", str(proposal_file),
            )
            self.assertEqual(preview["mode"], "cleanup")
            self.assertIsInstance(preview["override_revision"], int)
            self.assertEqual(preview["coverage"]["suggested"], 1)
            self.assertTrue(proposal_file.is_file())
            saved_preview = json.loads(proposal_file.read_text(encoding="utf-8"))
            self.assertEqual(
                saved_preview["override_revision"], preview["override_revision"]
            )
            applied_tags = self.run_json(
                root, "plan", "tags", "apply", "cli-plan", str(proposal_file)
            )
            self.assertGreaterEqual(
                applied_tags["override_revision"], preview["override_revision"]
            )
            self.assertEqual(applied_tags["updated"], 1)
            self.assertEqual(
                applied_tags["plan"]["stages"][0]["tasks"][0]["tags"], ["math"]
            )
            listed = self.run_json(root, "plan", "list")
            self.assertIn("cli-plan", {row["plan_id"] for row in listed["plans"]})
            disabled = self.run_json(root, "plan", "disable", "cli-plan")
            self.assertFalse(disabled["enabled"])
            enabled = self.run_json(root, "plan", "enable", "cli-plan")
            self.assertTrue(enabled["enabled"])

            exported_path = root / "exported.json"
            exported = self.run_json(
                root, "plan", "export", "cli-plan", "--output", str(exported_path)
            )
            exported_document = json.loads(exported_path.read_text(encoding="utf-8"))
            self.assertEqual(exported_document["plan_id"], "cli-plan")
            self.assertNotIn("task_statuses", exported_document)
            self.assertIn("task_statuses", exported)
            self.assertEqual(exported["output"], str(exported_path.resolve()))
            deleted = self.run_json(root, "plan", "delete", "cli-plan")
            self.assertTrue(deleted["history_preserved"])

    def test_sync_cli_reports_platform_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            self.run_json(
                root, "init", "--codeforces", "fixture_cf", "--luogu", "42",
                "--skip-validate", "--non-interactive",
            )

            def fake_cf(db, handle, refresh_catalog=None):
                db.upsert_problem({"platform": "codeforces", "problem_id": "1A", "rating": 800})
                db.record_sync_attempt("codeforces", status="fresh", success=True)
                return SyncResult("codeforces", "fresh", problems=1)

            def fake_luogu(db, uid, refresh_catalog=None, candidate_queries=None):
                db.upsert_problem({"platform": "luogu", "problem_id": "P1000", "difficulty": 1})
                db.record_sync_attempt("luogu", status="fresh", success=True)
                self.assertTrue(candidate_queries)
                self.assertTrue(all("difficulty" in row for row in candidate_queries))
                return SyncResult("luogu", "fresh", problems=1)

            with mock.patch("tools.acm_agent.cli.sync_codeforces", side_effect=fake_cf), mock.patch(
                "tools.acm_agent.cli.sync_luogu", side_effect=fake_luogu
            ):
                payload = self.run_json(root, "sync")
            self.assertTrue(payload["ok"])
            self.assertEqual([row["freshness"] for row in payload["results"]], ["fresh", "fresh"])

    def test_review_queue_finishes_after_7_30_90_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            self.run_json(
                root, "init", "--codeforces", "fixture_cf", "--luogu", "42",
                "--skip-validate", "--non-interactive",
            )
            due_dates = []
            for round_index in range(4):
                self.run_json(root, "start", "P1000")
                closed = self.run_json(
                    root, "close", "P1000", "--result", "AC", "--minutes", "10",
                    "--hint-level", "2" if round_index == 0 else "0",
                    "--failure", "none", "--non-interactive",
                )
                due_dates.append(closed["review_due"])
            self.assertTrue(all(due_dates[index] for index in range(3)))
            self.assertIsNone(due_dates[3])
            status = self.run_json(root, "status")
            self.assertEqual(status["review_due"], 0)

    def test_nonstandard_historical_cf_index_does_not_break_next(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            self.run_json(
                root, "init", "--codeforces", "fixture_cf", "--luogu", "42",
                "--skip-validate", "--non-interactive",
            )
            with Database(Paths.for_root(root).database) as db:
                db.upsert_problem({"platform": "codeforces", "problem_id": "9201", "rating": 800})
            payload = self.run_json(root, "next", "--count", "1")
            self.assertEqual(len(payload["recommendations"]), 1)

    def test_ai_context_and_patch_cli_forward_structured_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            statement = root / "statement.md"
            statement.write_text("fixture statement", encoding="utf-8")

            with mock.patch.object(
                __import__("tools.acm_agent.service", fromlist=["AcmService"]).AcmService,
                "ai_status",
                return_value={"ok": True, "api_key_detected": False},
            ):
                status = self.run_json(root, "ai", "status")
            self.assertFalse(status["api_key_detected"])

            with mock.patch.object(
                __import__("tools.acm_agent.service", fromlist=["AcmService"]).AcmService,
                "ai_recommendations",
                return_value={"ok": True, "warnings": [], "recommendations": []},
            ) as rerank:
                self.run_json(
                    root,
                    "next",
                    "--ai",
                    "--ai-mode",
                    "specialization",
                    "--model",
                    "deepseek-v4-pro",
                )
            self.assertEqual(rerank.call_args.kwargs["model"], "deepseek-v4-pro")
            self.assertEqual(rerank.call_args.kwargs["ai_mode"], "specialization")

            with mock.patch.object(
                __import__("tools.acm_agent.service", fromlist=["AcmService"]).AcmService,
                "ai_recommendations",
                return_value={"ok": True, "warnings": [], "recommendations": []},
            ) as default_mode:
                self.run_json(root, "next", "--ai")
            self.assertEqual(default_mode.call_args.kwargs["ai_mode"], "gap_fill")

            with mock.patch.object(
                __import__("tools.acm_agent.service", fromlist=["AcmService"]).AcmService,
                "problem_context_save",
                return_value={"ok": True, "problem_id": "CF1A"},
            ) as save_context:
                self.run_json(
                    root,
                    "context",
                    "set",
                    "CF1A",
                    "--file",
                    str(statement),
                    "--expected-hash",
                    "abc",
                )
            self.assertEqual(save_context.call_args.kwargs["content"], "fixture statement")
            self.assertEqual(save_context.call_args.kwargs["expected_hash"], "abc")

            with mock.patch.object(
                __import__("tools.acm_agent.service", fromlist=["AcmService"]).AcmService,
                "ai_patch_revert",
                return_value={"ok": True, "proposal_id": "p1"},
            ) as revert:
                self.run_json(root, "patch", "revert", "p1")
            revert.assert_called_once_with("p1")

    def test_retired_persistent_verify_cli_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(SystemExit) as stress_flag:
                main(["verify", "CF1A", "--ai-stress"], root=root)
            self.assertEqual(stress_flag.exception.code, 2)
            with self.assertRaises(SystemExit) as stress_command:
                main(["stress", "status"], root=root)
            self.assertEqual(stress_command.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
