from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tools.acm_agent.service import AcmService
from tools.acm_agent.storage import Database
from tools.acm_agent.config import Paths


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _VerifyResult:
    problem_id: str = "CF1A"
    source: str = "fixture.cpp"
    passed: bool = True
    compiled: bool = True
    sanitizer: str = "not_requested"
    failure_dir: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "problem_id": self.problem_id,
            "source": self.source,
            "passed": self.passed,
            "compiled": self.compiled,
            "compile_command": [],
            "compile_output": "",
            "sanitizer": self.sanitizer,
            "cases": [],
            "stress": "not_available",
            "stress_iterations": 0,
            "failure_dir": self.failure_dir,
            "warnings": [],
        }


class AcmServiceTests(unittest.TestCase):
    def prepare_root(self, root: Path) -> None:
        target = root / "training" / "data-structures-30d"
        target.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/plan.json", target / "plan.json")
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/README.md", target / "README.md")

    def test_bootstrap_before_setup_is_non_failing_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = AcmService(root)
            payload = service.bootstrap()
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["configured"])
            self.assertEqual(payload["active_sessions"], [])
            self.assertEqual(payload["recent_sessions"], [])
            self.assertEqual(payload["review_due"], [])
            self.assertEqual(
                payload["accepted_by_platform"], {"codeforces": 0, "luogu": 0}
            )
            self.assertFalse((root / ".acm/state.db").exists())

    def test_setup_start_close_and_bootstrap_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            setup = service.setup("fixture", "42", target_rating=1700, skip_validate=True)
            self.assertTrue(setup["ok"])

            started = service.start("CF1A")
            bootstrap = service.bootstrap()
            self.assertTrue(bootstrap["configured"])
            self.assertEqual(
                bootstrap["active_sessions"][0]["attempt_id"], started["attempt_id"]
            )
            self.assertEqual(bootstrap["active_sessions"][0]["problem_id"], "CF1A")
            self.assertEqual(bootstrap["active_sessions"][0]["source"], started["source"])

            closed = service.close(
                "CF1A",
                result="AC",
                minutes=20,
                hint_level=2,
                failure="modeling",
                notes="fixture",
            )
            self.assertIsNotNone(closed["review_due"])
            bootstrap = service.bootstrap()
            self.assertEqual(bootstrap["active_sessions"], [])
            self.assertEqual(bootstrap["recent_sessions"][0]["problem_id"], "CF1A")
            self.assertEqual(bootstrap["review_due"], [])  # first review is seven days away

    def test_recommend_and_verify_have_structured_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            calls: list[dict[str, object]] = []

            def fake_verify(root_arg, problem, **kwargs):
                calls.append({"root": root_arg, "problem": problem, **kwargs})
                return _VerifyResult(problem_id=str(problem))

            service = AcmService(root, verify_fn=fake_verify)
            service.setup("fixture", "42", skip_validate=True)
            recommendations = service.recommendations(count=3, mode="new")
            self.assertEqual(recommendations["recommendation_basis"], "plan_only")
            # Balanced mode never exceeds the plan quota when no catalog
            # snapshot is available: ceil(2 * 3 / 3) == 2.
            self.assertEqual(len(recommendations["recommendations"]), 2)
            self.assertTrue(recommendations["warnings"])
            self.assertTrue(all("breakdown" in row for row in recommendations["recommendations"]))

            verified = service.verify("CF1A", exact=True, timeout=3.5, stress_iterations=7, seed=9)
            self.assertTrue(verified["ok"])
            self.assertEqual(calls[0]["problem"], "CF1A")
            self.assertEqual(calls[0]["timeout"], 3.5)
            self.assertEqual(calls[0]["stress_iterations"], 7)

    def test_recommendations_use_configured_target_rating(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", target_rating=2100, skip_validate=True)
            captured: dict[str, object] = {}

            def fake_recommend(*args, **kwargs):
                captured.update(kwargs)
                return []

            with patch("tools.acm_agent.service.recommend", fake_recommend):
                service.recommendations(
                    count=1,
                    mode="new",
                    source_mode="plan_only",
                )
            self.assertEqual(captured["target_cf_rating"], 2100)

    def test_skip_is_global_idempotent_and_does_not_create_attempt_or_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", skip_validate=True)

            for plan_id in ("global-one", "global-two"):
                document = service.plan_template()["plan"]
                document["plan_id"] = plan_id
                document["title"] = plan_id
                service.plan_import(plan=document)

            first = service.problem_skip("CF1A", notes="already mastered")
            second = service.problem_skip("CF1A", notes="updated note")
            self.assertEqual(first["status"], "skipped")
            self.assertEqual(first["disposition"], "skipped_mastered")
            self.assertEqual(first["reason"], "idea_clear_without_editorial")
            self.assertEqual(second["notes"], "updated note")

            skipped = service.skipped_problems()
            self.assertEqual(skipped["count"], 1)
            self.assertEqual(skipped["problems"][0]["problem_key"], "codeforces:CF1A")
            for plan_id in ("global-one", "global-two"):
                for mode in ("new", "mixed", "review"):
                    recommendations = service.recommendations(
                        count=100,
                        mode=mode,
                        source_mode="plan_only",
                        plan_ids=[plan_id],
                    )["recommendations"]
                    self.assertEqual(recommendations, [])
            summaries = {
                row["plan_id"]: row for row in service.plans()["plans"]
            }
            for plan_id in ("global-one", "global-two"):
                self.assertEqual(summaries[plan_id]["skipped_count"], 1)
                self.assertEqual(summaries[plan_id]["completed_count"], 1)
                self.assertEqual(summaries[plan_id]["progress"], 1.0)

            with Database(Paths.for_root(root).database) as db:
                self.assertEqual(db.attempts("codeforces", "1A"), [])
                self.assertEqual(
                    [row["action"] for row in db.problem_disposition_events("codeforces", "1A")],
                    ["skip", "skip"],
                )
            self.assertEqual(service.bootstrap()["review_due"], [])

            removed = service.problem_unskip("CF1A", notes="restore")
            duplicate = service.problem_unskip("CF1A", notes="duplicate")
            self.assertTrue(removed["unskipped"])
            self.assertIsNone(removed["disposition"])
            self.assertFalse(duplicate["unskipped"])
            with Database(Paths.for_root(root).database) as db:
                self.assertEqual(
                    [row["action"] for row in db.problem_disposition_events("codeforces", "1A")],
                    ["unskip", "skip", "skip"],
                )

    def test_skip_rejects_active_session_and_accepted_problem(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", skip_validate=True)

            service.start("CF1A")
            with self.assertRaisesRegex(ValueError, "存在 active session，不能 Skip"):
                service.problem_skip("CF1A")
            service.close(
                "CF1A",
                result="AC",
                minutes=10,
                hint_level=0,
                failure="none",
            )
            with self.assertRaisesRegex(ValueError, "已 AC，不能 Skip"):
                service.problem_skip("CF1A")

    def test_plan_tag_preview_and_atomic_apply(self) -> None:
        class FakeCf:
            def problemset(self):
                raise AssertionError("cached CF tags should be preferred")

        class FakeLuogu:
            def tags(self):
                return {3: "线段树"}

            def problem(self, problem_id, *, tag_names=None):
                return {
                    "platform": "luogu",
                    "problem_id": problem_id,
                    "name": "线段树 1",
                    "tags": [tag_names[3]],
                }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(
                root,
                codeforces_client_factory=FakeCf,
                luogu_client_factory=FakeLuogu,
            )
            service.setup("fixture", "42", skip_validate=True)
            document = service.plan_template()["plan"]
            document["plan_id"] = "tag-plan"
            document["title"] = "Tags"
            document["stages"][0]["tasks"].append(
                {
                    "task_key": "luogu-task",
                    "platform": "luogu",
                    "problem_id": "P3372",
                    "name": "线段树 1",
                    "level": "A",
                    "tags": [],
                }
            )
            imported = service.plan_import(plan=document)
            with Database(Paths.for_root(root).database) as db:
                db.upsert_problem(
                    {"platform": "codeforces", "problem_id": "1A", "tags": ["math"]}
                )

            preview = service.plan_tags_preview("tag-plan")
            self.assertEqual(preview["base_revision"], imported["revision"])
            self.assertEqual(preview["coverage"]["suggested"], 2)
            proposals = preview["proposals"]
            proposals[0]["suggested_tags"] = [" math ", "MATH", "implementation"]
            applied = service.plan_tags_apply(
                "tag-plan",
                expected_revision=preview["base_revision"],
                proposals=proposals,
            )
            self.assertEqual(applied["updated"], 2)
            by_key = {
                task["task_key"]: task
                for task in applied["plan"]["stages"][0]["tasks"]
            }
            self.assertEqual(by_key["stage-1-task-1"]["tags"], ["math", "implementation"])

            with self.assertRaisesRegex(ValueError, "不存在 task_key"):
                service.plan_tags_apply(
                    "tag-plan",
                    expected_revision=applied["revision"],
                    proposals=[{"task_key": "removed", "suggested_tags": ["x"]}],
                )
            self.assertEqual(service.plan_detail("tag-plan")["revision"], applied["revision"])


if __name__ == "__main__":
    unittest.main()
