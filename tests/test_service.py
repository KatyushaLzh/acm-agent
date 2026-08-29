from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tools.acm_agent.service import AcmService
from tools.acm_agent.platforms import SyncResult
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

    def test_start_saves_and_uses_global_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", target_rating=1700, skip_validate=True)
            template = "// custom\nint main() {}\n"
            started = service.start("CF1A", template=template)
            global_file = root / ".acm" / "template.cpp"
            self.assertEqual(global_file.read_text(encoding="utf-8"), template)
            self.assertEqual(
                Path(started["source"]).read_text(encoding="utf-8"), template
            )
            payload = service.workspace_template()
            self.assertEqual(payload["template"], template)
            self.assertEqual(payload["source"], "global")
            self.assertTrue(payload["builtin"])
            with self.assertRaises(ValueError):
                service.start("CF1B", template="bad\x00template")

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

    def test_status_exposes_luogu_tagless_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = AcmService(root)
            service.setup("fixture", "42", skip_validate=True)
            with Database(service.paths.database) as db:
                db.connection.execute(
                    """INSERT INTO sync_state(platform,status,last_success_at,metadata_json)
                       VALUES('luogu','fresh','2026-08-25T00:00:00+00:00',?)""",
                    (
                        json.dumps(
                            {
                                "tag_enrichment_tagless": {
                                    "P5705": {
                                        "observed_at": "2026-08-25T00:00:00+00:00"
                                    }
                                }
                            }
                        ),
                    ),
                )
                db.connection.commit()

            payload = service.status()

        self.assertEqual(payload["sources"]["luogu"]["tagless"], 1)
        self.assertEqual(payload["sources"]["luogu"]["last_attempt_status"], "fresh")

    def test_validated_setup_immediately_syncs_both_platforms_and_tags(self) -> None:
        class IdentityClient:
            def user_info(self, identifier):
                return {
                    "handle": str(identifier),
                    "name": str(identifier),
                    "rating": 1600,
                }

        calls: list[tuple[str, str, bool]] = []
        luogu_difficulties: list[int] = []
        luogu_full_catalog: list[bool] = []

        def sync_cf(db, handle, *, refresh_catalog=None):
            calls.append(("codeforces", str(handle), bool(refresh_catalog)))
            return SyncResult("codeforces", "fresh", problems=100)

        def sync_lg(
            db,
            uid,
            *,
            refresh_catalog=None,
            candidate_queries=None,
            full_catalog=False,
        ):
            calls.append(("luogu", str(uid), bool(refresh_catalog)))
            luogu_full_catalog.append(bool(full_catalog))
            luogu_difficulties.extend(
                int(query["difficulty"]) for query in candidate_queries or []
            )
            return SyncResult(
                "luogu",
                "partial",
                accepted=55,
                warnings=["one public problem page failed"],
                tag_enrichment={
                    "attempted": 55,
                    "resolved": 54,
                    "failed": 1,
                    "remaining": 1,
                    "cursor": "P1054",
                    "errors": [],
                },
            )

        with tempfile.TemporaryDirectory() as temp:
            service = AcmService(
                Path(temp),
                codeforces_client_factory=IdentityClient,
                luogu_client_factory=IdentityClient,
                sync_codeforces_fn=sync_cf,
                sync_luogu_fn=sync_lg,
            )
            payload = service.setup("fixture", "42", target_rating=2100)

        self.assertEqual(
            calls,
            [("codeforces", "fixture", False), ("luogu", "42", False)],
        )
        self.assertTrue(payload["initial_sync"]["ok"])
        self.assertEqual(luogu_full_catalog, [True])
        self.assertEqual(luogu_difficulties, [])
        self.assertEqual(payload["tag_enrichment"]["attempted"], 55)
        self.assertEqual(payload["tag_enrichment"]["failed"], 1)

    def test_offline_setup_does_not_sync_or_complete_tags(self) -> None:
        def unexpected_sync(*args, **kwargs):
            self.fail("offline setup must not perform platform I/O")

        with tempfile.TemporaryDirectory() as temp:
            service = AcmService(
                Path(temp),
                sync_codeforces_fn=unexpected_sync,
                sync_luogu_fn=unexpected_sync,
            )
            payload = service.setup("fixture", "42", skip_validate=True)

        self.assertIsNone(payload["initial_sync"])
        self.assertIsNone(payload["tag_enrichment"])

    def test_sync_reports_structured_progress_and_aggregate_partial_status(self) -> None:
        progress: list[dict[str, object]] = []

        def sync_cf(db, handle, *, refresh_catalog=None):
            return SyncResult("codeforces", "fresh")

        def sync_lg(db, uid, *, refresh_catalog=None, candidate_queries=None):
            return SyncResult("luogu", "failed", error="fixture outage")

        with tempfile.TemporaryDirectory() as temp:
            service = AcmService(
                Path(temp),
                sync_codeforces_fn=sync_cf,
                sync_luogu_fn=sync_lg,
            )
            service.setup("fixture", "42", skip_validate=True)
            result = service.sync("all", _progress_callback=progress.append)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["status"] for item in result["results"]], ["fresh", "failed"])
        self.assertTrue(progress[-1]["usable"])
        self.assertEqual(
            set(progress[-1]),
            {
                "phase", "platform", "step", "total", "completed", "failed",
                "message", "started_at", "last_activity_at", "usable",
            },
        )

    def test_deferred_setup_validates_and_saves_without_syncing(self) -> None:
        class IdentityClient:
            def user_info(self, identifier):
                return {
                    "handle": str(identifier),
                    "name": str(identifier),
                    "rating": 1600,
                }

        def unexpected_sync(*args, **kwargs):
            self.fail("deferred web setup must leave synchronization to its job")

        with tempfile.TemporaryDirectory() as temp:
            service = AcmService(
                Path(temp),
                codeforces_client_factory=IdentityClient,
                luogu_client_factory=IdentityClient,
                sync_codeforces_fn=unexpected_sync,
                sync_luogu_fn=unexpected_sync,
            )
            payload = service.setup("fixture", "42", defer_sync=True)
            with Database(Path(temp) / ".acm" / "state.db") as db:
                self.assertEqual(db.account("codeforces")["identifier"], "fixture")
                self.assertEqual(db.account("luogu")["identifier"], "42")

        self.assertTrue(payload["validated"])
        self.assertTrue(payload["initial_sync_deferred"])
        self.assertIsNone(payload["initial_sync"])
        self.assertIsNone(payload["tag_enrichment"])

    def test_recent_solved_difficulty_uses_latest_fifty_from_each_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp, Database(
            Path(temp) / "state.db"
        ) as db:
            for index in range(51):
                cf_id = f"{1000 + index}A"
                luogu_id = f"P{1000 + index}"
                db.upsert_problem(
                    {
                        "platform": "codeforces",
                        "problem_id": cf_id,
                        "rating": 1000 + index * 10,
                    }
                )
                db.upsert_submission(
                    {
                        "platform": "codeforces",
                        "submission_id": str(index),
                        "problem_id": cf_id,
                        "verdict": "OK",
                        "submitted_at": f"2026-01-01T00:00:{index:02d}+00:00",
                    }
                )
                db.upsert_problem(
                    {
                        "platform": "luogu",
                        "problem_id": luogu_id,
                        "difficulty": 1 if index == 0 else 5,
                    }
                )
                db.upsert_submission(
                    {
                        "platform": "luogu",
                        "submission_id": f"accepted:{luogu_id}",
                        "problem_id": luogu_id,
                        "verdict": "AC",
                        "submitted_at": None,
                    }
                )

            profile = AcmService._recent_solved_difficulty_profile(db)

        self.assertEqual(profile["platforms"]["codeforces"]["selected"], 50)
        self.assertEqual(profile["platforms"]["luogu"]["selected"], 50)
        self.assertEqual(profile["sample_count"], 100)
        self.assertEqual(profile["platforms"]["codeforces"]["average"], 1255)
        self.assertEqual(profile["platforms"]["luogu"]["average"], 1900)
        self.assertEqual(profile["average"], 1578)

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

    def test_hint_level_alone_does_not_schedule_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", target_rating=1700, skip_validate=True)

            service.start("CF1A")
            closed = service.close(
                "CF1A",
                result="AC",
                minutes=20,
                hint_level=4,
                failure="none",
            )

            self.assertIsNone(closed["review_due"])
            self.assertEqual(closed["close"]["review_stage"], 0)
            self.assertEqual(service.review_queue()["items"], [])

    def test_manual_review_queue_is_one_shot_and_requires_accepted_problem(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", target_rating=1700, skip_validate=True)

            service.start("CF1A")
            service.close("CF1A", result="AC", minutes=10, hint_level=0, failure="none")
            added = service.review_queue_add("CF1A", review_due="2099-01-01")
            self.assertTrue(added["created"])
            self.assertEqual(added["items"][0]["queue_type"], "manual_once")
            self.assertEqual(added["items"][0]["review_stage"], 0)
            self.assertEqual(service.recommendations(mode="review")["recommendations"], [])

            rescheduled = service.review_queue_add("CF1A", review_due="2026-08-01")
            self.assertFalse(rescheduled["created"])
            self.assertEqual(
                [item["problem_id"] for item in service.recommendations(mode="review")["recommendations"]],
                ["CF1A"],
            )

            service.start("CF1A")
            failed = service.close(
                "CF1A", result="WA", minutes=10, hint_level=4, failure="implementation"
            )
            self.assertEqual(failed["review_due"], "2026-08-01")
            self.assertEqual(service.review_queue()["counts"]["due"], 1)

            service.start("CF1A")
            completed = service.close(
                "CF1A", result="AC", minutes=10, hint_level=0, failure="none"
            )
            self.assertIsNone(completed["review_due"])
            self.assertEqual(service.review_queue()["items"], [])
            self.assertEqual(service.recommendations(mode="review")["recommendations"], [])

            service.start("P1000")
            service.close("P1000", result="WA", minutes=10, hint_level=0, failure="none")
            with self.assertRaisesRegex(ValueError, "尚未 AC"):
                service.review_queue_add("P1000", review_due=date.today().isoformat())

    def test_automatic_review_queue_progresses_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", target_rating=1700, skip_validate=True)

            for result in ("WA", "WA", "AC"):
                service.start("CF1A")
                closed = service.close(
                    "CF1A", result=result, minutes=10, hint_level=4, failure="none"
                )
            self.assertEqual(closed["close"]["review_stage"], 1)
            self.assertEqual(service.review_queue()["items"][0]["queue_type"], "automatic")

            expected_delays = (30, 90)
            for expected_stage, delay in enumerate(expected_delays, start=2):
                service.start("CF1A")
                closed = service.close(
                    "CF1A", result="AC", minutes=10, hint_level=4, failure="none"
                )
                self.assertEqual(closed["close"]["review_stage"], expected_stage)
                self.assertEqual(
                    closed["review_due"],
                    (date.today() + timedelta(days=delay)).isoformat(),
                )

            service.start("CF1A")
            completed = service.close(
                "CF1A", result="AC", minutes=10, hint_level=4, failure="none"
            )
            self.assertIsNone(completed["review_due"])
            self.assertEqual(service.review_queue()["items"], [])

    def test_review_queue_remove_resets_old_evidence_but_allows_new_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", target_rating=1700, skip_validate=True)

            for result in ("WA", "WA", "AC"):
                service.start("CF1A")
                service.close("CF1A", result=result, minutes=10, hint_level=0, failure="none")
            self.assertEqual(service.review_queue()["counts"]["total"], 1)

            removed = service.review_queue_remove("CF1A")
            self.assertTrue(removed["removed"])

            service.start("CF1A")
            service.close("CF1A", result="AC", minutes=10, hint_level=0, failure="none")
            self.assertEqual(service.review_queue()["items"], [])

            for index, result in enumerate(("WA", "WA"), start=1):
                service.start("CF1A")
                service.close("CF1A", result=result, minutes=10, hint_level=0, failure="none")
                self.assertEqual(
                    service.review_queue()["counts"]["total"],
                    1 if index == 2 else 0,
                )
            self.assertEqual(service.review_queue()["counts"]["total"], 1)

            service.review_queue_remove("CF1A")
            service.start("CF1A")
            service.close(
                "CF1A", result="ABANDONED", minutes=10, hint_level=4, failure="none"
            )
            self.assertEqual(service.review_queue()["counts"]["total"], 1)

            service.review_queue_remove("CF1A")
            service.start("CF1A")
            service.close(
                "CF1A", result="WA", minutes=10, hint_level=4, failure="invariant"
            )
            self.assertEqual(service.review_queue()["counts"]["total"], 1)

    def test_review_queue_clear_preserves_attempts_and_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            service = AcmService(root)
            service.setup("fixture", "42", target_rating=1700, skip_validate=True)

            for problem in ("CF1A", "P1000"):
                service.start(problem)
                service.close(problem, result="AC", minutes=10, hint_level=0, failure="none")
                service.review_queue_add(problem, review_due="2026-09-01")
            with self.assertRaisesRegex(ValueError, "confirm=true"):
                service.review_queue_clear()
            cleared = service.review_queue_clear(confirm=True)
            self.assertEqual(cleared["removed_count"], 2)
            self.assertEqual(cleared["items"], [])
            with Database(Paths.for_root(root).database) as db:
                self.assertEqual(len(db.attempts()), 2)

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
            # Balanced mode is an ordinary union; plan provenance does not
            # impose a quota even when only plan candidates are available.
            self.assertEqual(len(recommendations["recommendations"]), 3)
            self.assertTrue(recommendations["warnings"])
            self.assertTrue(all("breakdown" in row for row in recommendations["recommendations"]))

            user_file = root / "picked-user.cpp"
            generator_file = root / "picked-generator.cpp"
            reference_file = root / "picked-reference.cpp"
            verified = service.verify(
                "CF1A",
                exact=True,
                timeout=3.5,
                stress_iterations=7,
                seed=9,
                user_file=str(user_file),
                generator_file=str(generator_file),
                reference_file=str(reference_file),
            )
            self.assertTrue(verified["ok"])
            self.assertEqual(calls[0]["problem"], user_file)
            self.assertEqual(calls[0]["timeout"], 3.5)
            self.assertEqual(calls[0]["stress_iterations"], 7)
            self.assertEqual(calls[0]["generator_file"], str(generator_file))
            self.assertEqual(calls[0]["reference_file"], str(reference_file))

            for timeout in (0, -1, float("inf"), float("nan")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    service.verify("CF1A", timeout=timeout)
            for iterations in (0, -1):
                with self.subTest(iterations=iterations), self.assertRaises(ValueError):
                    service.verify("CF1A", stress_iterations=iterations)

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

    def test_recommendations_support_explicit_dependency_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_root(root)
            captured: dict[str, object] = {}

            def fake_recommend(*args, **kwargs):
                captured.update(kwargs)
                return []

            service = AcmService(root, recommend_fn=fake_recommend)
            service.setup("fixture", "42", target_rating=1900, skip_validate=True)
            service.recommendations(count=1, mode="new", source_mode="plan_only")

            self.assertEqual(captured["target_cf_rating"], 1900)

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
