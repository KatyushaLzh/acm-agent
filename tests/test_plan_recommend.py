from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.plan import check_plan, load_plan, plan_task_records
from tools.acm_agent.recommend import (
    compute_weakness,
    estimate_cf_baseline,
    luogu_equivalent,
    recommend,
    recommendation_difficulty_targets,
    recommendation_slots,
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
        records = plan_task_records(load_plan(PLAN))
        d14 = [row for row in records if row["day"] == 14]
        self.assertEqual(len(d14), 4)
        self.assertTrue(all(row["unlock_at"] == "2026-08-09" for row in d14))

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

    def test_recommendation_difficulty_is_split_across_three_targets(self):
        choices = [
            candidate("CF200A", rating=1500),
            candidate("CF201A", rating=1700),
            candidate("CF202A", rating=2000),
        ]
        result = recommend(
            choices,
            count=3,
            cf_rating=1600,
            target_cf_rating=2000,
            recent_solved_equivalents=[1400, 1600],
        )
        self.assertEqual(
            [item.problem_id for item in result],
            ["CF201A", "CF200A", "CF202A"],
        )
        self.assertEqual(
            [item.difficulty_target for item in result], [1700, 1500, 2000]
        )
        self.assertEqual(
            [item.difficulty_band for item in result],
            ["current_plus_100", "recent_solved_average", "target_rating"],
        )

    def test_slot_cycle_keeps_each_complete_group_one_third(self):
        self.assertEqual(
            recommendation_slots(6),
            ["recovery", "main", "stretch", "recovery-2", "main-2", "stretch-2"],
        )
        self.assertEqual(
            recommendation_difficulty_targets(
                1600, [1200, 1400, 1600], target_rating=2100
            ),
            {"recovery": 1700, "main": 1400, "stretch": 2100},
        )

    def test_slot_uses_absolute_distance_even_outside_scoring_window(self):
        result = recommend(
            [candidate("CF1A", rating=3000), candidate("CF999A", rating=2400)],
            count=1,
            cf_rating=1600,
        )
        self.assertEqual(result[0].problem_id, "CF999A")
        self.assertEqual(result[0].difficulty_target, 1700)

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

    def test_plan_schedule_and_level_never_change_priority(self):
        today = date(2026, 8, 3)
        choices = [
            candidate("CF300A", rating=1650),
            candidate("CF301A", rating=1650, due_date=today.isoformat(), plan_level="B"),
            candidate("CF302A", rating=1650, due_date="2026-07-01", plan_level="B"),
        ]
        first = recommend(choices, now=today, count=1)[0]
        self.assertEqual(first.problem_id, "CF300A")
        self.assertEqual(first.breakdown["plan_urgency"], 0)
        self.assertFalse(any("题单" in reason for reason in first.reasons))

    def test_balanced_is_an_unweighted_union_without_a_plan_cap(self):
        today = date(2026, 8, 3)
        result = recommend(
            [candidate("CF399A", rating=800)],
            plan_tasks=[
                candidate("CF400A", rating=1850, source="plan", plan_id="p", task_key="a"),
                candidate("CF401A", rating=2050, source="plan", plan_id="p", task_key="b"),
                candidate("CF402A", rating=2250, source="plan", plan_id="p", task_key="c"),
            ],
            source_mode="balanced",
            target_cf_rating=2000,
            now=today,
            count=3,
        )
        self.assertEqual(
            {item.problem_id for item in result}, {"CF400A", "CF401A", "CF402A"}
        )
        self.assertTrue(all(item.plan_sources for item in result))

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

    def test_weakness_uses_recent_failures_and_ignores_hint_level(self):
        attempts = [
            {"date": "2026-08-01", "result": "WA", "hint_level": 2, "tags": ["segment tree"]},
            {"date": "2026-08-02", "result": "AC", "hint_level": 0, "tags": ["segment tree"]},
            {"date": "2026-08-02", "result": "AC", "hint_level": 4, "tags": ["hinted AC"]},
            {"date": "2026-04-01", "result": "ABANDONED", "tags": ["segment tree"]},
        ]
        weakness = compute_weakness(attempts, now=date(2026, 8, 3))
        self.assertEqual(weakness["segment tree"], 1.0)
        self.assertEqual(weakness["hinted AC"], 0.0)

    def test_platform_balance_breakdown_and_natural_tie_break(self):
        history = [{"problem_key": f"codeforces:CF{i}A", "platform": "codeforces"} for i in range(20)]
        choices = [
            candidate("CF100A", rating=1600),
            candidate("CF99A", rating=1600),
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
        self.assertEqual(result.to_dict()["slot"], "recovery")
        self.assertEqual(result.to_dict()["difficulty_band"], "current_plus_100")


if __name__ == "__main__":
    unittest.main()
