from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.plan import check_plan, load_plan, load_plan_data, plan_task_records
from tools.acm_agent.recommend import (
    compute_weakness,
    estimate_cf_baseline,
    luogu_equivalent,
    recommend,
)


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "training" / "data-structures-30d" / "README.md"
PLAN = ROOT / "training" / "data-structures-30d" / "plan.json"


def candidate(problem_id: str, **overrides):
    platform = "codeforces" if problem_id.startswith("CF") else "luogu"
    value = {
        "problem_id": problem_id,
        "platform": platform,
        "title": problem_id,
        "url": "",
        "source": "catalog",
    }
    value.update(overrides)
    return value


class PlanTests(unittest.TestCase):
    def test_plan_matches_readme_and_preserves_confirmed_metrics(self):
        result = check_plan(README, PLAN)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.stats["days"], 30)
        self.assertEqual(result.stats["task_occurrences"], 91)
        self.assertEqual(result.stats["replacement_occurrences"], 2)
        # v2 simplification treats every stage task as part of progressive
        # completion; legacy per-task ``required`` flags are ignored.
        self.assertEqual(result.stats["required_tasks"], 91)
        self.assertEqual(result.stats["required_by_platform"], {"codeforces": 50, "luogu": 41})

    def test_flattened_contest_tasks_keep_unlock_date(self):
        raw = {
            "schema_version": 2,
            "plan_id": "synthetic-dated-plan",
            "title": "Synthetic dated plan",
            "description": "Exercises stage date propagation without repository-local dates.",
            "schedule_mode": "dated",
            "stages": [
                {
                    "stage_key": "contest",
                    "topic": "Synthetic contest",
                    "kind": "virtual_contest",
                    "due_date": "2030-01-02",
                    "unlock_at": "2030-01-02",
                    "tasks": [
                        {
                            "task_key": f"contest-{index}",
                            "problem_id": f"CF{index}A",
                            "platform": "codeforces",
                        }
                        for index in range(1, 5)
                    ],
                }
            ],
        }
        records = plan_task_records(load_plan_data(raw))
        self.assertEqual(len(records), 4)
        self.assertTrue(all(row["unlock_at"] == "2030-01-02" for row in records))

    def test_check_detects_readme_drift(self):
        text = README.read_text(encoding="utf-8-sig").replace("problem/P3374", "problem/P3375", 1)
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "README.md"
            changed.write_text(text, encoding="utf-8")
            result = check_plan(changed, PLAN)
        self.assertFalse(result.ok)
        self.assertTrue(any("README-only" in error for error in result.errors))
        self.assertTrue(any("plan-only" in error for error in result.errors))


class RecommendationTests(unittest.TestCase):
    def test_cf_baseline_prefers_target_then_profile_and_distinct_median(self):
        attempts = [
            {"problem_key": "codeforces:CF1A", "rating": 1200, "result": "AC", "accepted_at": "2026-08-03"},
            {"problem_key": "codeforces:CF1A", "rating": 2200, "result": "AC", "accepted_at": "2026-08-02"},
            {"problem_key": "codeforces:CF2A", "rating": 1800, "result": "OK", "accepted_at": "2026-08-01"},
        ]
        self.assertEqual(
            estimate_cf_baseline(1712, attempts, target_rating=2000), 2000
        )
        self.assertEqual(estimate_cf_baseline(1712, attempts), 1712)
        self.assertEqual(estimate_cf_baseline(None, attempts), 1500)
        self.assertEqual(estimate_cf_baseline(None, []), 1600)

    def test_recommendation_difficulty_uses_target_rating(self):
        choices = [
            candidate("CF200A", rating=1650),
            candidate("CF201A", rating=2050),
        ]
        result = recommend(
            choices,
            count=1,
            cf_rating=1600,
            target_cf_rating=2000,
        )
        self.assertEqual(result[0].problem_id, "CF201A")
        self.assertTrue(any("main 目标 2050" in reason for reason in result[0].reasons))

    def test_luogu_mapping(self):
        self.assertEqual([luogu_equivalent(level) for level in range(1, 8)], [800, 1000, 1300, 1600, 1900, 2200, 2500])
        self.assertIsNone(luogu_equivalent(0))

    def test_future_contest_is_never_leaked(self):
        today = date(2026, 8, 3)
        choices = [
            candidate("CF100A", rating=1600, unlock_at="2026-08-04", due_date="2026-08-04"),
            candidate("CF101A", rating=1600),
        ]
        result = recommend(choices, now=today, count=3)
        self.assertEqual([item.problem_id for item in result], ["CF101A"])

    def test_storage_style_codeforces_id_is_supported(self):
        result = recommend(
            [{"platform": "codeforces", "problem_id": "1042D", "rating": 1800}],
            now=date(2026, 8, 3),
            count=1,
        )
        self.assertEqual(result[0].problem_key, "codeforces:CF1042D")
        self.assertEqual(result[0].problem_id, "1042D")

    def test_overdue_then_today_then_catalog_priority(self):
        today = date(2026, 8, 3)
        choices = [
            candidate("CF300A", rating=1650),
            candidate("CF301A", rating=1650, due_date=today.isoformat(), plan_level="B"),
            candidate("CF302A", rating=1650, due_date=(today - timedelta(days=1)).isoformat(), plan_level="B"),
        ]
        first = recommend(choices, now=today, count=1)[0]
        self.assertEqual(first.problem_id, "CF302A")
        self.assertGreater(first.breakdown["plan_urgency"], 1000)

    def test_new_excludes_ac_and_review_selects_only_due_ac(self):
        today = date(2026, 8, 3)
        choices = [
            candidate("CF400A", rating=1600, review_due=today.isoformat()),
            candidate("CF401A", rating=1600, review_due="2026-08-04"),
            candidate("CF402A", rating=1600),
        ]
        accepted = {"codeforces:CF400A", "CF401A"}
        self.assertEqual(
            [item.problem_id for item in recommend(choices, accepted_keys=accepted, now=today, mode="new")],
            ["CF402A"],
        )
        self.assertEqual(
            [item.problem_id for item in recommend(choices, accepted_keys=accepted, now=today, mode="review")],
            ["CF400A"],
        )

    def test_skipped_problem_is_excluded_from_new_mixed_and_review(self):
        today = date(2026, 8, 3)
        choices = [
            candidate("CF410A", rating=1600, review_due=today.isoformat()),
            candidate("CF411A", rating=1600),
        ]
        for mode in ("new", "mixed", "review"):
            with self.subTest(mode=mode):
                result = recommend(
                    choices,
                    accepted_keys={"codeforces:CF410A"},
                    skipped_keys={"codeforces:CF410A", "codeforces:CF411A"},
                    now=today,
                    mode=mode,
                )
                self.assertEqual(result, [])

        storage_key_result = recommend(
            [candidate("CF412A", rating=1600)],
            skipped_keys={"codeforces:412A"},
            now=today,
            mode="new",
        )
        self.assertEqual(storage_key_result, [])

    def test_weakness_uses_recent_failures_hints_and_independent_ac(self):
        attempts = [
            {"date": "2026-08-01", "result": "WA", "hint_level": 2, "tags": ["segment tree"]},
            {"date": "2026-08-02", "result": "AC", "hint_level": 0, "tags": ["segment tree"]},
            {"date": "2026-04-01", "result": "ABANDONED", "tags": ["segment tree"]},
        ]
        weakness = compute_weakness(attempts, now=date(2026, 8, 3))
        self.assertEqual(weakness["segment tree"], 2.5)

    def test_platform_balance_breakdown_and_natural_tie_break(self):
        history = [{"problem_key": f"codeforces:CF{i}A", "platform": "codeforces"} for i in range(20)]
        choices = [
            candidate("CF100A", rating=1650),
            candidate("CF99A", rating=1650),
            candidate("P1000", difficulty=4),
        ]
        result = recommend(choices, recommendation_history=history, now=date(2026, 8, 3), count=1)
        self.assertEqual(result[0].problem_id, "P1000")
        self.assertGreater(result[0].breakdown["platform_balance"], 0)

        tied = recommend(choices[:2], now=date(2026, 8, 3), count=1)
        self.assertEqual(tied[0].problem_id, "CF99A")

    def test_score_breakdown_is_complete_and_sums_to_score(self):
        choice = candidate("P2000", difficulty=4, tags=["DSU"], due_date="2026-08-03", plan_level="B")
        result = recommend(
            [choice],
            attempts=[{"date": "2026-08-02", "result": "ABANDONED", "tags": ["DSU"]}],
            now=date(2026, 8, 3),
            count=1,
        )[0]
        self.assertEqual(
            set(result.breakdown),
            {"plan_urgency", "review_due", "difficulty_fit", "weakness", "platform_balance", "recent_repeat"},
        )
        self.assertAlmostEqual(result.score, sum(result.breakdown.values()))
        self.assertEqual(result.to_dict()["slot"], "main")


if __name__ == "__main__":
    unittest.main()
