from __future__ import annotations

from datetime import date
import unittest

from tools.acm_agent.recommend import plan_fairness_order, recommend


TODAY = date(2026, 8, 4)


def catalog(problem_id: str, **overrides):
    value = {
        "problem_id": problem_id,
        "platform": "codeforces",
        "title": f"Catalog {problem_id}",
        "rating": 1650,
        "source": "catalog",
    }
    value.update(overrides)
    return value


def plan_task(problem_id: str, plan_id: str, **overrides):
    value = {
        "problem_id": problem_id,
        "platform": "codeforces",
        "title": f"Plan {problem_id}",
        "rating": 1650,
        "source": "plan",
        "plan_id": plan_id,
        "plan_title": plan_id.upper(),
        "stage_key": "stage-1",
        "stage_order": 0,
        "task_key": f"{plan_id}-{problem_id}",
        "due_date": TODAY.isoformat(),
        "plan_level": "B",
    }
    value.update(overrides)
    return value


class RecommendationSourceTests(unittest.TestCase):
    def test_source_modes_are_strict(self):
        catalogs = [catalog("CF10A"), catalog("CF11A")]
        plans = [plan_task("CF20A", "p1"), plan_task("CF21A", "p1")]

        catalog_result = recommend(
            catalogs,
            plan_tasks=plans,
            source_mode="catalog_only",
            now=TODAY,
            count=4,
        )
        self.assertEqual({item.problem_id for item in catalog_result}, {"CF10A", "CF11A"})
        self.assertTrue(all(item.source == "catalog" for item in catalog_result))

        plan_result = recommend(
            catalogs,
            plan_tasks=plans,
            source_mode="plan_only",
            now=TODAY,
            count=4,
        )
        self.assertEqual({item.problem_id for item in plan_result}, {"CF20A", "CF21A"})
        self.assertTrue(all(item.source == "plan" for item in plan_result))

        with self.assertRaisesRegex(ValueError, "source_mode"):
            recommend(catalogs, source_mode="anything")

    def test_balanced_is_an_uncapped_union(self):
        plans = [plan_task(f"CF{100 + index}A", "p1") for index in range(6)]
        for count in (1, 2, 3, 4, 5):
            result = recommend([], plan_tasks=plans, now=TODAY, count=count)
            self.assertEqual(len(result), min(len(plans), count))

        result = recommend(
            [catalog("CF999A")],
            plan_tasks=plans,
            source_mode="balanced",
            now=TODAY,
            count=3,
        )
        self.assertEqual(len(result), 3)
        self.assertEqual(sum(item.source == "plan" for item in result), 3)

    def test_duplicate_problem_merges_catalog_metadata_and_every_plan_source(self):
        result = recommend(
            [catalog("CF50A", tags=["implementation"])],
            plan_tasks=[
                plan_task("CF50A", "alpha", tags=["greedy"]),
                plan_task(
                    "CF50A",
                    "beta",
                    stage_key="stage-2",
                    stage_order=1,
                    task_key="beta-task",
                    due_date="2026-08-03",
                    tags=["math"],
                ),
            ],
            source_mode="balanced",
            now=TODAY,
            count=3,
        )
        self.assertEqual(len(result), 1)
        item = result[0]
        self.assertEqual(item.problem_id, "CF50A")
        self.assertEqual(item.title, "Catalog CF50A")
        self.assertEqual(set(item.tags), {"implementation", "greedy", "math"})
        self.assertEqual({source.plan_id for source in item.plan_sources}, {"alpha", "beta"})
        self.assertEqual(item.task_key, "beta-task")
        self.assertEqual(
            {source["plan_id"] for source in item.to_dict()["plan_sources"]},
            {"alpha", "beta"},
        )

    def test_stable_tie_break_does_not_depend_on_input_order(self):
        values = [catalog("CF100A"), catalog("CF99A")]
        forward = recommend(values, source_mode="catalog_only", now=TODAY, count=2)
        backward = recommend(list(reversed(values)), source_mode="catalog_only", now=TODAY, count=2)
        self.assertEqual([item.problem_id for item in forward], ["CF99A", "CF100A"])
        self.assertEqual(
            [item.problem_id for item in forward],
            [item.problem_id for item in backward],
        )

    def test_plan_fairness_helper_is_not_a_recommendation_tie_break(self):
        history = [
            {"plan_sources": [{"plan_id": "alpha"}]},
            {"plan_id": "gamma"},
            {"plan_sources": '[{"plan_id":"alpha"}]'},
        ]
        self.assertEqual(
            plan_fairness_order(["gamma", "beta", "alpha"], history),
            ("beta", "gamma", "alpha"),
        )

        result = recommend(
            [],
            plan_tasks=[plan_task("CF60A", "alpha"), plan_task("CF61A", "beta")],
            recommendation_history=history,
            source_mode="plan_only",
            now=TODAY,
            count=1,
        )
        self.assertEqual(result[0].plan_sources[0].plan_id, "alpha")

    def test_plan_ids_filter_applies_before_duplicate_merge(self):
        result = recommend(
            [],
            plan_tasks=[plan_task("CF70A", "alpha"), plan_task("CF70A", "beta")],
            plan_ids=["beta"],
            source_mode="plan_only",
            now=TODAY,
            count=1,
        )
        self.assertEqual([source.plan_id for source in result[0].plan_sources], ["beta"])


if __name__ == "__main__":
    unittest.main()
