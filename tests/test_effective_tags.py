from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tools.acm_agent.config import Paths
from tools.acm_agent.service import AcmService
from tools.acm_agent.storage import (
    Database,
    MIGRATIONS,
    SCHEMA_VERSION,
    TagOverrideRevisionConflict,
)
from tools.acm_agent.tag_policy import (
    effective_tags,
    meta_tag_reason,
    normalize_tags,
    split_meta_tags,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _single_problem_plan(
    plan_id: str,
    *,
    tags: list[str],
    topic: str = "专题",
    problem_id: str = "CF1A",
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "plan_id": plan_id,
        "title": plan_id,
        "description": "effective-tag fixture",
        "schedule_mode": "progressive",
        "stages": [
            {
                "stage_key": "stage-1",
                "topic": topic,
                "kind": "practice",
                "tasks": [
                    {
                        "task_key": "task-1",
                        "platform": "codeforces",
                        "problem_id": problem_id,
                        "name": problem_id,
                        "level": "A",
                        "tags": tags,
                    }
                ],
            }
        ],
    }


class TagPolicyTests(unittest.TestCase):
    def test_normalization_metadata_filter_and_explicit_add_precedence(self) -> None:
        raw = [
            "  Dynamic   Programming ",
            "dynamic programming",
            "2024",
            "NOI",
            "各省省选",
            "O2优化",
        ]
        self.assertEqual(
            normalize_tags(raw),
            ["Dynamic Programming", "2024", "NOI", "各省省选", "O2优化"],
        )
        subject, ignored = split_meta_tags(raw)
        self.assertEqual(subject, ["Dynamic Programming"])
        self.assertEqual(
            {row["reason"] for row in ignored},
            {"year", "event_source", "region", "compiler_option"},
        )
        self.assertEqual(meta_tag_reason("USACO"), "event_source")
        self.assertEqual(meta_tag_reason("集训队"), "event_source")
        self.assertEqual(meta_tag_reason("四川"), "region")
        self.assertEqual(meta_tag_reason("浙江"), "region")
        self.assertEqual(meta_tag_reason("WC"), "event_source")
        self.assertEqual(meta_tag_reason("CTSC"), "event_source")
        self.assertIsNone(meta_tag_reason("树状数组"))

        resolved = effective_tags(
            raw,
            [
                {"tag": "dynamic programming", "action": "suppress"},
                {"tag": "NOI", "action": "add"},
            ],
        )
        self.assertEqual(resolved, ["NOI"])


class StorageV5Tests(unittest.TestCase):
    def test_v4_migration_creates_tables_and_backfills_closed_attempt_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.db"
            connection = sqlite3.connect(path)
            for version in range(1, 5):
                connection.executescript(MIGRATIONS[version])
            connection.execute(
                """INSERT INTO problems(platform,problem_id,name,tags_json,source_json,updated_at)
                   VALUES('codeforces','1A','fixture',?,'{}','2026-08-04T00:00:00+00:00')""",
                (json.dumps(["math", "2024", "O2优化"], ensure_ascii=False),),
            )
            connection.execute(
                """INSERT INTO attempts(platform,problem_id,started_at,closed_at,result,active)
                   VALUES('codeforces','1A','2026-08-01T00:00:00+00:00',
                          '2026-08-01T01:00:00+00:00','WA',0)"""
            )
            attempt_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.execute("PRAGMA user_version = 4")
            connection.commit()
            connection.close()

            with Database(path) as database:
                self.assertEqual(SCHEMA_VERSION, 6)
                self.assertEqual(
                    database.connection.execute("PRAGMA user_version").fetchone()[0], 6
                )
                tables = {
                    row["name"]
                    for row in database.query(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(
                    {
                        "problem_tag_overrides",
                        "tag_override_state",
                        "attempt_tag_snapshots",
                    }
                    <= tables
                )
                snapshot = database.attempt_tag_snapshot(attempt_id)
                self.assertIsNotNone(snapshot)
                self.assertEqual(json.loads(snapshot["tags_json"]), ["math"])
                self.assertEqual(snapshot["source"], "migration_v5")
            backup = path.with_name(f"{path.name}.v4.bak")
            self.assertTrue(backup.is_file())
            backup_before = backup.read_bytes()
            connection = sqlite3.connect(backup)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            finally:
                connection.close()

            # Reopening is idempotent: the snapshot is neither removed nor duplicated.
            with Database(path) as database:
                rows = database.query(
                    "SELECT * FROM attempt_tag_snapshots WHERE attempt_id=?", (attempt_id,)
                )
                self.assertEqual(len(rows), 1)
            self.assertEqual(backup.read_bytes(), backup_before)


class EffectiveTagServiceTests(unittest.TestCase):
    def prepare_root(self, root: Path) -> None:
        target = root / "training" / "data-structures-30d"
        target.mkdir(parents=True)
        shutil.copy2(
            REPO_ROOT / "training/data-structures-30d/plan.json", target / "plan.json"
        )
        shutil.copy2(
            REPO_ROOT / "training/data-structures-30d/README.md", target / "README.md"
        )

    def test_cleanup_can_delete_to_empty_and_override_revision_is_optimistic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", skip_validate=True)
            imported = service.plan_import(
                plan=_single_problem_plan(
                    "cleanup-plan", tags=["2024", "NOI", "O2优化"]
                )
            )

            preview = service.plan_tags_preview(
                "cleanup-plan",
                expected_revision=imported["revision"],
                mode="cleanup",
                refresh=False,
            )
            self.assertEqual(preview["base_revision"], imported["revision"])
            self.assertIsInstance(preview["override_revision"], int)
            self.assertEqual(len(preview["proposals"]), 1)
            proposal = preview["proposals"][0]
            self.assertEqual(proposal["raw_tags"], [])
            self.assertEqual(proposal["current_tags"], ["2024", "NOI", "O2优化"])
            self.assertEqual(proposal["suggested_tags"], [])
            self.assertEqual(proposal["added_tags"], [])
            self.assertEqual(proposal["removed_tags"], ["2024", "NOI", "O2优化"])
            self.assertEqual(
                {row["tag"] for row in proposal["ignored_meta_tags"]},
                {"2024", "NOI", "O2优化"},
            )

            applied = service.plan_tags_apply(
                "cleanup-plan",
                expected_revision=preview["base_revision"],
                expected_override_revision=preview["override_revision"],
                proposals=[{"task_key": "task-1", "tags": []}],
            )
            task = applied["plan"]["stages"][0]["tasks"][0]
            self.assertEqual(task["tags"], [])

            # A preview from before an unrelated override write must not clobber it.
            stale_preview = service.plan_tags_preview(
                "cleanup-plan", mode="cleanup", refresh=False
            )
            with Database(Paths.for_root(root).database) as db:
                revision = db.tag_override_revision()
                with db.atomic():
                    db.replace_problem_tag_overrides(
                        "codeforces", "1A", additions=["math"]
                    )
                    db.bump_tag_override_revision(revision)
            with self.assertRaisesRegex(TagOverrideRevisionConflict, "revision conflict"):
                service.plan_tags_apply(
                    "cleanup-plan",
                    expected_revision=stale_preview["base_revision"],
                    expected_override_revision=stale_preview["override_revision"],
                    proposals=[{"task_key": "task-1", "tags": []}],
                )

    def test_global_suppress_and_plan_tags_feed_recommendations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", skip_validate=True)
            first = service.plan_import(
                plan=_single_problem_plan(
                    "first-plan", tags=["plan-only-tag", "remove-me"], topic="stage-tag"
                )
            )
            service.plan_import(
                plan=_single_problem_plan(
                    "second-plan", tags=["remove-me"], topic="other-stage"
                )
            )
            with Database(Paths.for_root(root).database) as db:
                db.upsert_problem(
                    {
                        "platform": "codeforces",
                        "problem_id": "1A",
                        "tags": ["catalog-tag", "remove-me", "2024"],
                    }
                )

            preview = service.plan_tags_preview(
                "first-plan",
                expected_revision=first["revision"],
                mode="cleanup",
                refresh=False,
            )
            service.plan_tags_apply(
                "first-plan",
                expected_revision=preview["base_revision"],
                expected_override_revision=preview["override_revision"],
                proposals=[
                    {
                        "task_key": "task-1",
                        "tags": ["plan-only-tag", "catalog-tag", "stage-tag"],
                    }
                ],
            )
            with Database(Paths.for_root(root).database) as db:
                raw = db.query(
                    "SELECT tags_json FROM problems WHERE platform='codeforces' AND problem_id='1A'"
                )[0]["tags_json"]
                self.assertEqual(
                    json.loads(raw), ["catalog-tag", "remove-me", "2024"]
                )

            second = service.recommendations(
                count=1,
                mode="new",
                source_mode="plan_only",
                plan_ids=["second-plan"],
            )["recommendations"][0]
            self.assertNotIn("remove-me", second["tags"])
            self.assertNotIn("2024", second["tags"])

            first_rec = service.recommendations(
                count=1,
                mode="new",
                source_mode="plan_only",
                plan_ids=["first-plan"],
            )["recommendations"][0]
            self.assertTrue(
                {"plan-only-tag", "catalog-tag", "stage-tag"} <= set(first_rec["tags"])
            )
            for source_mode in ("catalog_only", "balanced"):
                with self.subTest(source_mode=source_mode):
                    recommendation = service.recommendations(
                        count=1,
                        mode="new",
                        source_mode=source_mode,
                        plan_ids=["first-plan"],
                    )["recommendations"][0]
                    self.assertIn("catalog-tag", recommendation["tags"])
                    self.assertNotIn("remove-me", recommendation["tags"])
                    self.assertNotIn("2024", recommendation["tags"])

    def test_apply_failure_rolls_back_plan_file_and_override_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", skip_validate=True)
            imported = service.plan_import(
                plan=_single_problem_plan("rollback-plan", tags=["old-tag"])
            )
            managed_path = Path(imported["managed_path"])
            file_before = managed_path.read_bytes()
            preview = service.plan_tags_preview(
                "rollback-plan", mode="cleanup", refresh=False
            )

            with patch.object(
                Database,
                "bump_tag_override_revision",
                side_effect=RuntimeError("injected override revision failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    service.plan_tags_apply(
                        "rollback-plan",
                        expected_revision=preview["base_revision"],
                        expected_override_revision=preview["override_revision"],
                        proposals=[{"task_key": "task-1", "tags": ["new-tag"]}],
                    )

            self.assertEqual(managed_path.read_bytes(), file_before)
            detail = service.plan_detail("rollback-plan")
            self.assertEqual(detail["revision"], imported["revision"])
            with Database(Paths.for_root(root).database) as db:
                self.assertEqual(db.tag_override_revision(), preview["override_revision"])
                self.assertEqual(db.problem_tag_overrides("codeforces", "1A"), [])

    def test_close_freezes_effective_tags_for_weakness_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", skip_validate=True)
            with Database(Paths.for_root(root).database) as db:
                db.upsert_problem(
                    {
                        "platform": "codeforces",
                        "problem_id": "1A",
                        "tags": ["old-topic", "2024"],
                    }
                )

            service.start("CF1A")
            closed = service.close(
                "CF1A",
                result="WA",
                minutes=10,
                hint_level=0,
                failure="modeling",
            )
            with Database(Paths.for_root(root).database) as db:
                snapshot = db.attempt_tag_snapshot(closed["attempt_id"])
                self.assertEqual(json.loads(snapshot["tags_json"]), ["old-topic"])
                revision = db.tag_override_revision()
                with db.atomic():
                    db.replace_problem_tag_overrides(
                        "codeforces",
                        "1A",
                        additions=["new-topic"],
                        suppressions=["old-topic"],
                    )
                    db.bump_tag_override_revision(revision)

            weakness = service.weekly_review()["weak_topics"]
            self.assertIn("old-topic", weakness)
            self.assertNotIn("new-topic", weakness)

            with Database(Paths.for_root(root).database) as db:
                legacy_id = db.start_attempt("codeforces", "1A")
                db.close_attempt(legacy_id, result="WA")
                rows = service._attempt_rows_with_tags(db)
            row = next(item for item in rows if item["id"] == closed["attempt_id"])
            self.assertEqual(row["tags"], ["old-topic"])
            self.assertEqual(row["tag_source"], "snapshot")
            legacy = next(item for item in rows if item["id"] == legacy_id)
            self.assertEqual(legacy["tags"], ["new-topic"])
            self.assertEqual(legacy["tag_source"], "legacy_fallback")


if __name__ == "__main__":
    unittest.main()
