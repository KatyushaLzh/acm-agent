from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from tools.acm_agent.plan import PlanError, convert_v1_to_v2, load_plan_data
from tools.acm_agent.plan_manager import (
    DuplicatePlanError,
    PlanManager,
    RevisionConflict,
)
from tools.acm_agent.storage import Database, MIGRATIONS, SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
BUILTIN_PLAN = ROOT / "training" / "data-structures-30d" / "plan.json"


def sample_plan(plan_id: str = "sample-plan") -> dict[str, object]:
    return {
        "schema_version": 2,
        "plan_id": plan_id,
        "title": "Sample",
        "description": "fixture",
        "schedule_mode": "progressive",
        "stages": [
            {
                "stage_key": "s1",
                "topic": "first",
                "kind": "practice",
                "tasks": [
                    {
                        "task_key": "t1",
                        "platform": "codeforces",
                        "problem_id": "CF1A",
                        "level": "A",
                        "tags": ["implementation"],
                        "required": False,
                    }
                ],
            },
            {
                "stage_key": "s2",
                "topic": "second",
                "kind": "practice",
                "tasks": [
                    {
                        "task_key": "t2",
                        "platform": "luogu",
                        "problem_id": "P1001",
                        "level": "B",
                        "tags": [],
                        "required": True,
                    }
                ],
                "replacements": [
                    {
                        "condition": {
                            "type": "ac",
                            "mode": "any",
                            "problem_keys": ["codeforces:CF1A"],
                        },
                        "replace_task_keys": ["t2"],
                        "task": {
                            "task_key": "t2-replacement",
                            "platform": "luogu",
                            "problem_id": "P1000",
                            "level": "B",
                            "tags": [],
                            "required": True,
                        },
                    }
                ],
            },
        ],
    }


class PlanSchemaTests(unittest.TestCase):
    def test_builtin_plan_is_progressive_without_stage_deadlines(self) -> None:
        source = json.loads(BUILTIN_PLAN.read_text(encoding="utf-8"))
        self.assertEqual(source["schema_version"], 2)
        self.assertEqual(source["schedule_mode"], "progressive")
        self.assertEqual(len(source["stages"]), 30)
        for stage in source["stages"]:
            self.assertFalse({"date", "due_date", "unlock_at"} & stage.keys())

        converted = convert_v1_to_v2(source)
        self.assertEqual(converted["schema_version"], 2)
        self.assertNotIn("platform_target", converted)
        self.assertEqual(converted["plan_id"], "data-structures-30d")
        self.assertEqual(len(converted["stages"]), 30)
        replacement = converted["stages"][27]["replacements"][0]
        self.assertIsInstance(replacement["condition"], dict)
        self.assertEqual(
            replacement["condition"]["problem_keys"], ["codeforces:CF1797D"]
        )
        load_plan_data(converted)

    def test_duplicate_keys_and_date_conflicts_are_rejected(self) -> None:
        duplicate = sample_plan()
        duplicate["stages"][1]["tasks"][0]["task_key"] = "t1"
        with self.assertRaisesRegex(PlanError, "duplicate task_key"):
            load_plan_data(duplicate)

        dated = sample_plan()
        dated["schedule_mode"] = "dated"
        dated["stages"][0]["due_date"] = "2026-08-10"
        dated["stages"][0]["unlock_at"] = "2026-08-11"
        dated["stages"][1]["due_date"] = "2026-08-12"
        with self.assertRaisesRegex(PlanError, "unlock_at"):
            load_plan_data(dated)

    def test_v2_rejects_natural_language_replacement_conditions(self) -> None:
        document = sample_plan()
        document["stages"][1]["replacements"][0]["condition"] = "if CF1A is AC"
        with self.assertRaisesRegex(PlanError, "structured AC condition"):
            load_plan_data(document)

    def test_legacy_platform_target_is_read_but_removed_from_canonical_output(self) -> None:
        legacy = sample_plan()
        legacy["platform_target"] = {"codeforces": 0.5, "luogu": 0.5}
        canonical = convert_v1_to_v2(load_plan_data(legacy))
        self.assertNotIn("platform_target", canonical)

    def test_legacy_task_dates_lift_only_when_lossless(self) -> None:
        safe = sample_plan()
        for task in safe["stages"][0]["tasks"]:
            task["due_date"] = "2026-08-20"
            task["unlock_at"] = "2026-08-10"
        normalized = convert_v1_to_v2(load_plan_data(safe))
        self.assertEqual(normalized["stages"][0]["due_date"], "2026-08-20")
        self.assertEqual(normalized["stages"][0]["unlock_at"], "2026-08-10")
        self.assertNotIn("due_date", normalized["stages"][0]["tasks"][0])

        unsafe = sample_plan()
        unsafe["stages"][0]["tasks"][0]["due_date"] = "2026-08-20"
        unsafe["stages"][0]["tasks"].append(
            {
                "task_key": "t1b",
                "platform": "codeforces",
                "problem_id": "CF2A",
                "level": "A",
                "tags": [],
            }
        )
        with self.assertRaisesRegex(PlanError, "cannot be safely lifted"):
            load_plan_data(unsafe)


class StorageMigrationTests(unittest.TestCase):
    def test_schema_one_database_migrates_without_losing_existing_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE sentinel(value TEXT)")
            connection.execute("INSERT INTO sentinel VALUES('kept')")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
            connection.close()
            with Database(path) as database:
                version = database.connection.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, SCHEMA_VERSION)
                self.assertEqual(database.query("SELECT value FROM sentinel")[0][0], "kept")
                self.assertEqual(database.plans(), [])

    def test_schema_two_plan_tasks_migrate_to_stage_only_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            connection = sqlite3.connect(path)
            connection.executescript(MIGRATIONS[1])
            connection.executescript(MIGRATIONS[2])
            connection.execute(
                """INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "old-plan", "Old", "", "dated", 1, "managed", None, None,
                    1, "{}", "2026-08-04", "2026-08-04",
                ),
            )
            connection.execute(
                "INSERT INTO plan_stages VALUES(?,?,?,?,?,?,?)",
                ("old-plan", "s1", 0, "stage", "practice", "2026-08-05", "2026-08-01"),
            )
            connection.execute(
                """INSERT INTO plan_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "old-plan", "t1", "s1", 0, "codeforces", "CF1A",
                    "https://codeforces.com/problemset/problem/1/A", "B", "[]",
                    0, "2026-08-04", "2026-08-02", 0, None,
                ),
            )
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
            connection.close()

            with Database(path) as database:
                columns = {
                    row["name"] for row in database.query("PRAGMA table_info(plan_tasks)")
                }
                self.assertFalse({"required", "due_date", "unlock_at"} & columns)
                self.assertTrue({"name", "title"} <= columns)
                task = database.plan_task_rows("old-plan")[0]
                self.assertEqual(task["name"], "CF1A")
                self.assertEqual(task["level"], "B")

    def test_schema_three_migrates_through_current_schema_with_skip_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            connection = sqlite3.connect(path)
            for version in range(1, 4):
                connection.executescript(MIGRATIONS[version])
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
            connection.close()

            with Database(path) as database:
                version = database.connection.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, SCHEMA_VERSION)
                tables = {
                    row["name"]
                    for row in database.query(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {"problem_dispositions", "problem_disposition_events"} <= tables
                )
                disposition_columns = {
                    row["name"]
                    for row in database.query("PRAGMA table_info(problem_dispositions)")
                }
                self.assertTrue(
                    {
                        "platform",
                        "problem_id",
                        "disposition",
                        "reason",
                        "notes",
                        "source",
                        "context_json",
                        "created_at",
                        "updated_at",
                    }
                    <= disposition_columns
                )

    def test_skip_lifecycle_is_audited_and_accepted_status_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with Database(Path(temp) / "state.db") as database:
                database.upsert_problem(
                    {"platform": "codeforces", "problem_id": "1A", "name": "Theatre Square"}
                )
                database.skip_problem("codeforces", "1A", notes="first")
                self.assertEqual(database.problem_status("codeforces", "1A"), "skipped")
                disposition = database.problem_disposition("codeforces", "1A")
                self.assertEqual(disposition["disposition"], "skipped_mastered")
                self.assertEqual(disposition["reason"], "idea_clear_without_editorial")

                database.skip_problem("codeforces", "1A", notes="updated")
                events = database.problem_disposition_events("codeforces", "1A")
                self.assertEqual([row["action"] for row in events], ["skip", "skip"])
                self.assertEqual(database.problem_disposition("codeforces", "1A")["notes"], "updated")

                self.assertTrue(database.unskip_problem("codeforces", "1A", notes="undo"))
                self.assertFalse(database.unskip_problem("codeforces", "1A", notes="duplicate"))
                events = database.problem_disposition_events("codeforces", "1A")
                self.assertEqual([row["action"] for row in events], ["unskip", "skip", "skip"])
                self.assertEqual(database.problem_status("codeforces", "1A"), "not_started")

                database.skip_problem("codeforces", "1A")
                attempt_id = database.start_attempt("codeforces", "1A")
                database.close_attempt(attempt_id, result="AC")
                self.assertEqual(database.problem_status("codeforces", "1A"), "accepted")


class PlanManagerTests(unittest.TestCase):
    def prepare_root(self, root: Path) -> None:
        target = root / "training" / "data-structures-30d"
        target.mkdir(parents=True)
        shutil.copy2(BUILTIN_PLAN, target / "plan.json")
        shutil.copy2(BUILTIN_PLAN.with_name("README.md"), target / "README.md")

    def test_builtin_is_registered_without_modifying_source_and_edit_creates_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            source = root / "training/data-structures-30d/plan.json"
            before = source.read_bytes()
            with PlanManager(root) as manager:
                builtin = manager.get_plan("data-structures-30d")
                self.assertEqual(builtin["source"], "builtin")
                self.assertFalse(builtin["has_override"])
                self.assertEqual(
                    builtin["plan"]["stages"][0]["tasks"][0]["name"], "树状数组 1"
                )
                edited = manager.edit_plan(
                    "data-structures-30d",
                    builtin["revision"],
                    operations={
                        "action": "plan_update",
                        "patch": {"title": "edited builtin"},
                    },
                )
                self.assertTrue(edited["has_override"])
                self.assertTrue(Path(edited["managed_path"]).is_file())
                restored = manager.restore_builtin(
                    "data-structures-30d", expected_revision=edited["revision"]
                )
                self.assertFalse(restored["has_override"])
                self.assertNotEqual(restored["plan"]["title"], "edited builtin")
            self.assertEqual(source.read_bytes(), before)

    def test_import_preview_edit_conflict_and_normalized_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with PlanManager(root, bootstrap=False) as manager:
                preview = manager.preview(sample_plan())
                self.assertTrue(preview["ok"])
                self.assertFalse(preview["duplicate"])
                self.assertEqual(preview["platform_counts"], {"codeforces": 1, "luogu": 1})
                self.assertEqual(preview["replacement_count"], 1)
                self.assertFalse(preview["platform_count_includes_replacements"])
                imported = manager.import_plan(sample_plan())
                self.assertEqual(imported["revision"], 1)
                self.assertNotIn("platform_target", imported["plan"])
                self.assertEqual(imported["platform_ratio"], {"codeforces": 0.5, "luogu": 0.5})
                self.assertNotIn("required", imported["plan"]["stages"][0]["tasks"][0])
                self.assertNotIn("due_date", imported["plan"]["stages"][0]["tasks"][0])
                self.assertEqual(imported["plan"]["stages"][0]["tasks"][0]["name"], "CF1A")
                self.assertEqual(len(manager.db.plan_stage_rows("sample-plan")), 2)
                self.assertEqual(len(manager.db.plan_task_rows("sample-plan")), 3)
                self.assertTrue(manager.preview(sample_plan())["duplicate"])
                with self.assertRaises(DuplicatePlanError):
                    manager.import_plan(sample_plan())
                with self.assertRaisesRegex(PlanError, "cannot be edited"):
                    manager.edit_plan(
                        "sample-plan",
                        1,
                        operations={
                            "action": "plan_update",
                            "patch": {"platform_target": {"codeforces": 1.0}},
                        },
                    )

                edited = manager.edit_plan(
                    "sample-plan",
                    1,
                    operations=[
                        {
                            "action": "stage_update",
                            "stage_key": "s1",
                            "patch": {"due_date": "2026-08-20"},
                        },
                        {
                            "action": "task_move",
                            "task_key": "t2",
                            "stage_key": "s1",
                            "index": 0,
                        },
                    ],
                )
                self.assertEqual(edited["revision"], 2)
                self.assertEqual(edited["plan"]["stages"][0]["tasks"][0]["task_key"], "t2")
                self.assertEqual(edited["plan"]["stages"][0]["due_date"], "2026-08-20")
                with self.assertRaises(RevisionConflict):
                    manager.edit_plan(
                        "sample-plan",
                        1,
                        operations={
                            "action": "plan_update",
                            "patch": {"title": "stale"},
                        },
                    )
                self.assertEqual(manager.get_plan("sample-plan")["revision"], 2)

    def test_revisions_are_trimmed_to_five_and_restore_creates_new_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with PlanManager(Path(temp), bootstrap=False) as manager:
                current = manager.import_plan(sample_plan())
                for index in range(6):
                    current = manager.edit_plan(
                        "sample-plan",
                        current["revision"],
                        operations={
                            "action": "plan_update",
                            "patch": {"title": f"revision-{index}"},
                        },
                    )
                revisions = manager.revisions("sample-plan")
                self.assertEqual(len(revisions), 5)
                restore_target = revisions[-1]
                restored = manager.restore(
                    "sample-plan",
                    restore_target["revision"],
                    expected_revision=current["revision"],
                )
                self.assertEqual(restored["revision"], current["revision"] + 1)
                self.assertEqual(restored["plan"]["title"], restore_target["plan"]["title"])
                self.assertEqual(len(manager.revisions("sample-plan")), 5)

    def test_platform_ratio_changes_immediately_after_task_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with PlanManager(Path(temp), bootstrap=False) as manager:
                current = manager.import_plan(sample_plan())
                changed = manager.edit_plan(
                    "sample-plan",
                    current["revision"],
                    operations={"action": "task_delete", "task_key": "t2"},
                )
                self.assertEqual(changed["task_count"], 1)
                self.assertEqual(changed["platform_counts"], {"codeforces": 1})
                self.assertEqual(changed["platform_ratio"], {"codeforces": 1.0})
                self.assertEqual(changed["replacement_count"], 1)

    def test_progressive_unlock_and_plan_delete_preserve_attempt_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with PlanManager(Path(temp), bootstrap=False) as manager:
                imported = manager.import_plan(sample_plan())
                records = manager.recommendation_records(plan_ids=["sample-plan"])
                self.assertEqual({row["stage_key"] for row in records}, {"s1"})
                manager.db.start_attempt("codeforces", "1A")
                attempt_id = manager.db.attempts("codeforces", "1A")[0]["id"]
                manager.db.close_attempt(attempt_id, result="AC")
                records = manager.recommendation_records(plan_ids=["sample-plan"])
                self.assertEqual({row["stage_key"] for row in records}, {"s1", "s2"})
                self.assertIn("t2-replacement", {row["task_key"] for row in records})
                result = manager.delete_plan(
                    "sample-plan", expected_revision=imported["revision"]
                )
                self.assertTrue(result["deleted"])
                self.assertEqual(len(manager.db.attempts("codeforces", "1A")), 1)

    def test_skipped_task_unlocks_progressive_stage_but_not_ac_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with PlanManager(Path(temp), bootstrap=False) as manager:
                manager.import_plan(sample_plan())
                manager.db.skip_problem("codeforces", "1A")

                records = manager.recommendation_records(plan_ids=["sample-plan"])
                self.assertEqual({row["stage_key"] for row in records}, {"s1", "s2"})
                task_keys = {row["task_key"] for row in records}
                self.assertIn("t2", task_keys)
                self.assertNotIn("t2-replacement", task_keys)

                summary = manager.list_plans()[0]
                detail = manager.get_plan("sample-plan")
                for payload in (summary, detail):
                    self.assertEqual(payload["accepted_count"], 0)
                    self.assertEqual(payload["skipped_count"], 1)
                    self.assertEqual(payload["completed_count"], 1)
                    self.assertEqual(payload["progress"], 0.5)


if __name__ == "__main__":
    unittest.main()
