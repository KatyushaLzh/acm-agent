from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from tools.acm_agent.plan_manager import RevisionConflict
from tools.acm_agent.service import AcmService


REPO_ROOT = Path(__file__).resolve().parents[1]


class ServicePlanTests(unittest.TestCase):
    def prepare(self, root: Path) -> AcmService:
        target = root / "training" / "data-structures-30d"
        target.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/plan.json", target / "plan.json")
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/README.md", target / "README.md")
        service = AcmService(root)
        service.setup("fixture", "42", skip_validate=True)
        return service

    @staticmethod
    def managed_plan() -> dict:
        return {
            "schema_version": 2,
            "plan_id": "graphs-week",
            "title": "图论一周",
            "description": "service fixture",
            "schedule_mode": "dated",
            "stages": [
                {
                    "stage_key": "shortest-path",
                    "topic": "最短路",
                    "kind": "practice",
                    "due_date": "2026-08-10",
                    "tasks": [
                        {
                            "task_key": "sp-1",
                            "platform": "codeforces",
                            "problem_id": "CF1A",
                            "name": "Theatre Square",
                            "level": "A",
                            "tags": ["graphs"],
                        }
                    ],
                }
            ],
        }

    def test_import_edit_state_restore_and_delete_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = self.prepare(root)
            document = self.managed_plan()

            preview = service.plan_preview(plan=document)
            self.assertTrue(preview["ok"])
            self.assertFalse(preview["duplicate"])
            self.assertNotIn("platform_target", preview["plan"])
            self.assertEqual(preview["platform_counts"], {"codeforces": 1})
            imported = service.plan_import(plan=document)
            self.assertEqual(imported["plan_id"], "graphs-week")
            self.assertTrue((root / ".acm/plans/graphs-week.json").is_file())

            edited_document = imported["plan"]
            edited_document["stages"][0]["due_date"] = "2026-08-09"
            edited = service.plan_edit(
                "graphs-week", imported["revision"], plan=edited_document
            )
            self.assertEqual(
                edited["plan"]["stages"][0]["due_date"], "2026-08-09"
            )
            self.assertNotIn("due_date", edited["plan"]["stages"][0]["tasks"][0])
            with self.assertRaises(RevisionConflict):
                service.plan_state(
                    "graphs-week", enabled=False, expected_revision=imported["revision"]
                )

            disabled = service.plan_state(
                "graphs-week", enabled=False, expected_revision=edited["revision"]
            )
            revisions = service.plan_revisions("graphs-week")["revisions"]
            self.assertGreaterEqual(len(revisions), 2)
            restored = service.plan_restore(
                "graphs-week",
                disabled["revision"],
                revision=revisions[-1]["revision"],
            )
            self.assertGreater(restored["revision"], disabled["revision"])
            deleted = service.plan_delete(
                "graphs-week", expected_revision=restored["revision"]
            )
            self.assertTrue(deleted["history_preserved"])
            self.assertFalse((root / ".acm/plans/graphs-week.json").exists())

    def test_duplicate_import_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = self.prepare(Path(temp))
            document = self.managed_plan()
            first = service.plan_import(plan=document)
            preview = service.plan_preview(plan=document)
            self.assertTrue(preview["duplicate"])
            with self.assertRaises(ValueError):
                service.plan_import(plan=document)
            replaced = service.plan_import(
                plan=document,
                confirm_replace=True,
                expected_revision=first["revision"],
            )
            self.assertGreater(replaced["revision"], first["revision"])

    def test_plan_listing_template_and_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = self.prepare(Path(temp))
            rows = service.plans()["plans"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "builtin")
            self.assertEqual(rows[0]["platform_counts"], {"codeforces": 50, "luogu": 41})
            self.assertAlmostEqual(rows[0]["platform_ratio"]["codeforces"], 50 / 91)
            template = service.plan_template()["plan"]
            self.assertEqual(template["schema_version"], 2)
            self.assertNotIn("platform_target", template)
            checked = service.plan_check()
            self.assertTrue(checked["ok"], checked)


if __name__ == "__main__":
    unittest.main()
