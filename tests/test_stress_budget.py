from __future__ import annotations

import unittest

from tools.acm_agent.stress_budget import (
    MAX_PROVIDER_TOKENS_PER_PREPARATION,
    PreparationBudget,
    PreparationBudgetExhausted,
    PreparationTokenBudgetExhausted,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class PreparationBudgetTests(unittest.TestCase):
    def test_pause_resume_excludes_user_wait_from_every_deadline(self) -> None:
        clock = FakeClock()
        budget = PreparationBudget(clock=clock)
        remaining_before = budget.remaining(include_cleanup_reserve=True)
        phase_before = dict(budget.phase_deadline_offsets())
        budget.pause()
        clock.now += 120.0
        budget.resume()
        self.assertAlmostEqual(
            budget.remaining(include_cleanup_reserve=True), remaining_before, places=6
        )
        phase_after = budget.phase_deadline_offsets()
        for phase, offset in phase_before.items():
            self.assertAlmostEqual(phase_after[phase] - offset, 120.0, places=6)
        self.assertEqual(budget.paused_total_seconds(), 120.0)
        self.assertAlmostEqual(budget.elapsed(), 0.0, places=6)

        # Nested pause/resume is idempotent.
        budget.pause()
        budget.pause()
        clock.now += 30.0
        budget.resume()
        budget.resume()
        self.assertEqual(budget.paused_total_seconds(), 150.0)
        self.assertAlmostEqual(
            budget.remaining(include_cleanup_reserve=True), remaining_before, places=6
        )

        # After the window elapses, remaining really drops.
        clock.now += 599.0
        self.assertLessEqual(budget.remaining(include_cleanup_reserve=True), 1.0)

    def test_documented_bounds_and_scaled_soft_budgets(self) -> None:
        for invalid in (59, 1801, True, 300.0):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                PreparationBudget(invalid)  # type: ignore[arg-type]
        for valid in (60, 300, 1800):
            with self.subTest(valid=valid):
                clock = FakeClock()
                budget = PreparationBudget(valid, clock=clock)
                self.assertEqual(budget.timeout_seconds, valid)
                self.assertEqual(
                    budget.soft_budget("contract"), 65.0 * valid / 600.0
                )

    def test_default_phase_deadlines_are_cumulative_and_exact(self) -> None:
        clock = FakeClock()
        budget = PreparationBudget(clock=clock)
        self.assertEqual(
            budget.phase_deadline_offsets(),
            {
                "context": 20.0,
                "contract": 85.0,
                "initial_prepare": 300.0,
                "audit_initial": 345.0,
                "repair_1": 425.0,
                "provider": 480.0,
                "local_validation": 590.0,
                "total": 600.0,
            },
        )
        self.assertEqual(budget.provider_deadline, 580.0)
        self.assertEqual(budget.local_deadline, 690.0)
        self.assertEqual(budget.deadline, 700.0)
        self.assertEqual(budget.phase_deadline("repair_1"), 525.0)
        self.assertEqual(budget.phase_remaining("repair_1"), 425.0)
        with self.assertRaisesRegex(ValueError, "unknown preparation phase"):
            budget.phase_deadline("missing")

    def test_provider_request_caps_and_minimum_windows(self) -> None:
        clock = FakeClock()
        budget = PreparationBudget(clock=clock)
        self.assertEqual(
            budget.provider_timeout("extract_contract", soft_stage="contract"),
            85.0,
        )
        self.assertEqual(
            budget.provider_timeout("generate_brute", soft_stage="prepare_helpers"),
            120.0,
        )
        self.assertEqual(
            budget.provider_timeout(
                "generate_blueprint",
                soft_stage="prepare_helpers",
                minimum_seconds=30.0,
            ),
            180.0,
        )
        self.assertEqual(
            budget.provider_timeout("audit_helpers", soft_stage="audit_helpers"),
            50.0,
        )
        clock.now = 180.0
        with self.assertRaises(PreparationBudgetExhausted) as caught:
            budget.provider_timeout("extract_contract", soft_stage="contract")
        self.assertEqual(caught.exception.details["minimum_request_seconds"], 8.0)

    def test_non_default_totals_scale_the_same_cumulative_schedule(self) -> None:
        for configured, provider, local, cleanup in (
            (300, 240.0, 295.0, 5.0),
            (450, 360.0, 442.5, 7.5),
            (900, 720.0, 885.0, 15.0),
        ):
            with self.subTest(configured=configured):
                clock = FakeClock()
                budget = PreparationBudget(configured, clock=clock)
                self.assertEqual(
                    budget.provider_deadline - budget.started_at, provider
                )
                self.assertEqual(budget.local_deadline - budget.started_at, local)
                self.assertEqual(budget.cleanup_reserve_seconds, cleanup)

    def test_provider_hard_stop_cannot_borrow_local_validation(self) -> None:
        clock = FakeClock()
        budget = PreparationBudget(600, clock=clock)
        clock.now = budget.provider_deadline
        with self.assertRaises(PreparationBudgetExhausted) as caught:
            budget.provider_timeout(
                "generate_generator",
                soft_stage="repair_helpers",
            )
        self.assertEqual(caught.exception.details["available_seconds"], 0.0)
        self.assertEqual(budget.remaining(), 0.0)
        self.assertEqual(budget.remaining(include_cleanup_reserve=True), 120.0)
        self.assertEqual(budget.require("preflight_helpers"), 110.0)
        self.assertEqual(budget.remaining(), 110.0)

    def test_cleanup_reserve_is_part_of_one_absolute_deadline(self) -> None:
        clock = FakeClock()
        budget = PreparationBudget(600, clock=clock)
        self.assertEqual(budget.remaining(), 480.0)
        self.assertEqual(budget.remaining(include_cleanup_reserve=True), 600.0)
        clock.now = budget.local_deadline
        with self.assertRaises(PreparationBudgetExhausted) as caught:
            budget.require("preflight_helpers")
        self.assertEqual(caught.exception.code, "stress_prepare_budget_exhausted")
        self.assertEqual(caught.exception.details["configured_timeout_seconds"], 600)
        self.assertEqual(caught.exception.details["last_stage"], "preflight_helpers")
        self.assertEqual(budget.remaining(include_cleanup_reserve=True), 10.0)

    def test_progress_and_stage_timings_use_the_same_monotonic_clock(self) -> None:
        clock = FakeClock()
        budget = PreparationBudget(60, clock=clock)
        budget.mark_stage("contract")
        clock.now += 3.0
        progress = budget.progress("contract", soft_stage="contract")
        self.assertEqual(progress["elapsed_seconds"], 3.0)
        self.assertEqual(progress["stage_elapsed_seconds"], 3.0)
        self.assertEqual(progress["soft_budget_seconds"], 6.5)
        budget.mark_stage("prepare_helpers")
        clock.now += 7.0
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["stage_timings"]["contract"], 3.0)
        self.assertEqual(snapshot["stage_timings"]["prepare_helpers"], 7.0)

    def test_provider_token_hard_limit_rejects_overrun_and_future_requests(self) -> None:
        budget = PreparationBudget(clock=FakeClock())
        budget.add_usage({"total_tokens": MAX_PROVIDER_TOKENS_PER_PREPARATION})
        with self.assertRaises(PreparationTokenBudgetExhausted) as before_request:
            budget.provider_timeout("generate_reference")
        self.assertEqual(
            before_request.exception.details["provider_tokens_used"],
            MAX_PROVIDER_TOKENS_PER_PREPARATION,
        )

        overrun = PreparationBudget(clock=FakeClock())
        overrun.add_usage({"total_tokens": MAX_PROVIDER_TOKENS_PER_PREPARATION - 1})
        with self.assertRaises(PreparationTokenBudgetExhausted) as after_response:
            overrun.add_usage({"total_tokens": 2})
        self.assertEqual(after_response.exception.code, "stress_prepare_token_budget_exhausted")
        self.assertEqual(
            after_response.exception.usage["total_tokens"],
            MAX_PROVIDER_TOKENS_PER_PREPARATION + 1,
        )


if __name__ == "__main__":
    unittest.main()
