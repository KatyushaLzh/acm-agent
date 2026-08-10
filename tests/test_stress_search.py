"""Tests for the stress case-selection driver."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.stress_search import (
    DEFAULT_ENUMERATION_LIMIT,
    DEFAULT_RECIPE_MIX,
    MUTABLE_PROFILES,
    CaseRequest,
    SearchDriver,
    enumerate_tiny,
)
from tools.acm_agent.stress_profiler import Profiler

ARRAY_CONTRACT = {
    "syntax": {
        "mode": "single_case",
        "sections": [
            {"id": "header", "kind": "scalar", "fields": [{"name": "n", "type": "int"}]},
            {
                "id": "values",
                "kind": "list",
                "count_from": "header.n",
                "fields": [{"name": "a", "type": "int", "minimum": 0, "maximum": 9}],
            },
        ],
    },
    "constraints": [
        {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 40}}
    ],
}

BINARY_CONTRACT = {
    "syntax": {
        "mode": "single_case",
        "sections": [
            {"id": "header", "kind": "scalar", "fields": [{"name": "n", "type": "int"}]},
            {
                "id": "values",
                "kind": "list",
                "count_from": "header.n",
                "fields": [{"name": "a", "type": "int", "minimum": 0, "maximum": 1}],
            },
        ],
    },
    "constraints": [
        {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 20}}
    ],
}

OPAQUE_CONTRACT = {"syntax": {"mode": "operation_stream", "sections": []}}


def array_input(values: list[int]) -> bytes:
    return f"{len(values)}\n{' '.join(str(v) for v in values)}\n".encode()


class EnumerateTinyTests(unittest.TestCase):
    def test_enumerates_complete_sweeps_only(self) -> None:
        result = enumerate_tiny(Profiler(ARRAY_CONTRACT), array_input([1, 2, 3]), limit=512)
        counts: dict[int, int] = {}
        for data in result:
            counts.setdefault(int(data.split()[0]), 0)
            counts[int(data.split()[0])] += 1
        # n=1 has 10 inputs, n=2 has 100; n=3 needs 1000 and must not appear at
        # all rather than appear partially.
        self.assertEqual(counts, {1: 10, 2: 100})

    def test_every_enumerated_input_is_distinct(self) -> None:
        result = enumerate_tiny(Profiler(ARRAY_CONTRACT), array_input([5]), limit=512)
        self.assertEqual(len(result), len(set(result)))

    def test_declared_count_matches_payload(self) -> None:
        for data in enumerate_tiny(Profiler(ARRAY_CONTRACT), array_input([1, 2]), limit=200):
            tokens = data.split()
            self.assertEqual(int(tokens[0]), len(tokens) - 1)

    def test_preserves_template_layout(self) -> None:
        # Template puts one value per line; enumeration must not reflow onto one.
        result = enumerate_tiny(Profiler(ARRAY_CONTRACT), b"2\n3\n4\n", limit=200)
        two_row = [data for data in result if int(data.split()[0]) == 2]
        self.assertTrue(two_row)
        self.assertTrue(all(data.count(b"\n") == 3 for data in two_row))

    def test_preserves_inline_template_layout(self) -> None:
        result = enumerate_tiny(Profiler(ARRAY_CONTRACT), b"2\n3 4\n", limit=200)
        two_row = [data for data in result if int(data.split()[0]) == 2]
        self.assertTrue(two_row)
        self.assertTrue(all(data.count(b"\n") == 2 for data in two_row))

    def test_respects_byte_budget(self) -> None:
        result = enumerate_tiny(
            Profiler(BINARY_CONTRACT), array_input([0, 1]), limit=4096, max_bytes=12
        )
        self.assertTrue(all(len(data) <= 12 for data in result))

    def test_limit_zero_yields_nothing(self) -> None:
        self.assertEqual(enumerate_tiny(Profiler(ARRAY_CONTRACT), array_input([1]), limit=0), [])

    def test_unparseable_contract_yields_nothing(self) -> None:
        self.assertEqual(enumerate_tiny(Profiler(OPAQUE_CONTRACT), b"anything", limit=64), [])

    def test_unparseable_template_yields_nothing(self) -> None:
        self.assertEqual(
            enumerate_tiny(Profiler(ARRAY_CONTRACT), b"not an array at all", limit=64), []
        )

    def test_wide_domain_declines_to_enumerate(self) -> None:
        wide = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {"id": "header", "kind": "scalar",
                     "fields": [{"name": "n", "type": "int"}]},
                    {"id": "values", "kind": "list", "count_from": "header.n",
                     "fields": [{"name": "a", "type": "int",
                                 "minimum": 0, "maximum": 10**9}]},
                ],
            },
            "constraints": [
                {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 40}}
            ],
        }
        self.assertEqual(enumerate_tiny(Profiler(wide), array_input([7]), limit=512), [])

    def test_undeclared_value_domain_declines_to_enumerate(self) -> None:
        # Range lives only in `constraints`, so the field bound stays None and
        # row_domain would infer the domain from the template.  Enumeration must
        # not claim a complete sweep of an inferred domain.
        undeclared = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {"id": "header", "kind": "scalar",
                     "fields": [{"name": "n", "type": "int"}]},
                    {"id": "values", "kind": "list", "count_from": "header.n",
                     "fields": [{"name": "a", "type": "int"}]},
                ],
            },
            "constraints": [
                {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 10}},
                {"kind": "range", "target": "values.a", "args": {"minimum": 0, "maximum": 3}},
            ],
        }
        self.assertEqual(
            enumerate_tiny(Profiler(undeclared), array_input([2, 2]), limit=512), []
        )


class DriverRoutingTests(unittest.TestCase):
    def driver(self, contract: object = ARRAY_CONTRACT, **kwargs: object) -> SearchDriver:
        options: dict[str, object] = {
            "first_seed": 1,
            "max_bytes": 100,
            "rng_seed": 11,
            "seed_cases": 5,
            "enumeration_limit": 0,
        }
        options.update(kwargs)
        return SearchDriver(contract, **options)  # type: ignore[arg-type]

    def feed(self, driver: SearchDriver, count: int, *, profile: str = "small",
             case_kind: str = "random") -> list[CaseRequest]:
        seen: list[CaseRequest] = []
        for index in range(count):
            request = driver.next_case(profile, case_kind)
            seen.append(request)
            data = request.data if request.data is not None else array_input(
                [(index + offset) % 10 for offset in range(3 + index % 5)]
            )
            driver.record(request, data, reference_output=b"1")
        return seen

    def test_opaque_contract_is_recipe_only(self) -> None:
        driver = self.driver(OPAQUE_CONTRACT)
        self.assertFalse(driver.active)
        self.assertTrue(driver.stats.disabled_reason)
        sources = {request.source for request in self.feed(driver, 30)}
        self.assertEqual(sources, {"recipe"})

    def test_seed_phase_is_recipe_only(self) -> None:
        driver = self.driver(seed_cases=12)
        sources = [request.source for request in self.feed(driver, 12)]
        self.assertEqual(set(sources), {"recipe"})

    def test_mutation_starts_after_seed_phase(self) -> None:
        driver = self.driver(seed_cases=5)
        sources = [request.source for request in self.feed(driver, 80)]
        self.assertIn("mutate", sources[5:])

    def test_recipe_share_tracks_configured_mix(self) -> None:
        driver = self.driver(seed_cases=5, recipe_mix=0.4)
        requests = self.feed(driver, 405)
        post_seed = requests[5:]
        share = sum(r.source == "recipe" for r in post_seed) / len(post_seed)
        # Stalls also fall back to the recipe, so the share is a floor, not an
        # equality.  The point of the assertion is that mutation neither takes
        # over completely nor fails to start.
        self.assertGreater(share, 0.3)
        self.assertLess(share, 0.6)

    def test_large_profile_never_mutates(self) -> None:
        driver = self.driver(seed_cases=2)
        self.assertNotIn("large", MUTABLE_PROFILES)
        sources = {r.source for r in self.feed(driver, 40, profile="large")}
        self.assertEqual(sources, {"recipe"})

    def test_boundary_case_kinds_never_mutate(self) -> None:
        driver = self.driver(seed_cases=2)
        self.feed(driver, 40)
        for kind in ("lower_bound", "upper_bound"):
            request = driver.next_case("small", kind)
            self.assertEqual(request.source, "recipe")
            self.assertIsNone(request.data)

    def test_every_case_consumes_a_distinct_seed(self) -> None:
        driver = self.driver(seed_cases=5, enumeration_limit=64)
        seeds = [request.seed for request in self.feed(driver, 120)]
        self.assertEqual(len(seeds), len(set(seeds)))
        self.assertEqual(seeds, sorted(seeds))

    def test_enumeration_runs_after_seeding_then_stops(self) -> None:
        driver = self.driver(seed_cases=5, enumeration_limit=64)
        sources = [request.source for request in self.feed(driver, 200)]
        self.assertEqual(sources[:5], ["recipe"] * 5)
        enumerated = [i for i, s in enumerate(sources) if s == "enumerate"]
        self.assertTrue(enumerated)
        # One contiguous block, never revisited.
        self.assertEqual(enumerated, list(range(enumerated[0], enumerated[-1] + 1)))

    def test_synthetic_requests_carry_data_and_origin(self) -> None:
        driver = self.driver(seed_cases=5)
        for request in self.feed(driver, 120):
            if request.source == "recipe":
                self.assertIsNone(request.data)
                self.assertFalse(request.synthetic)
            else:
                self.assertIsNotNone(request.data)
                self.assertTrue(request.synthetic)
                self.assertTrue(request.origin)


class DriverFeedbackTests(unittest.TestCase):
    def driver(self, **kwargs: object) -> SearchDriver:
        options: dict[str, object] = {
            "first_seed": 1,
            "max_bytes": 100,
            "rng_seed": 3,
            "seed_cases": 4,
            "enumeration_limit": 0,
        }
        options.update(kwargs)
        return SearchDriver(ARRAY_CONTRACT, **options)  # type: ignore[arg-type]

    def drive(self, driver: SearchDriver, count: int) -> None:
        for index in range(count):
            request = driver.next_case("small", "random")
            data = request.data or array_input([(index * 3 + i) % 10 for i in range(4)])
            driver.record(request, data, reference_output=b"%d" % (index % 3))

    def test_report_counts_sources(self) -> None:
        driver = self.driver()
        self.drive(driver, 60)
        stats = driver.report()["search"]
        self.assertEqual(stats["recipe"] + stats["mutated"] + stats["enumerated"], 60)
        self.assertGreater(stats["mutated"], 0)

    def test_diversity_report_present_when_active(self) -> None:
        driver = self.driver()
        self.drive(driver, 40)
        report = driver.report()
        self.assertIn("diversity", report)
        self.assertGreater(report["diversity"]["cells"], 1)

    def test_no_diversity_report_when_inactive(self) -> None:
        driver = SearchDriver(OPAQUE_CONTRACT, first_seed=0)
        self.assertNotIn("diversity", driver.report())

    def test_unparseable_input_does_not_raise(self) -> None:
        driver = self.driver()
        request = driver.next_case("small", "random")
        driver.record(request, b"total garbage not an array")
        self.assertTrue(driver.active)

    def test_signal_is_optional(self) -> None:
        driver = self.driver()
        request = driver.next_case("small", "random")
        driver.record(request, array_input([1, 2, 3]))
        self.assertEqual(driver.report()["search"]["recipe"], 1)

    def test_reject_counts_without_disabling_early(self) -> None:
        driver = self.driver()
        self.drive(driver, 40)
        mutated = driver.next_case("small", "random")
        driver.reject(mutated)
        self.assertEqual(driver.report()["search"]["rejected"], 1)
        self.assertTrue(driver.active)

    def test_sustained_rejection_disables_mutation(self) -> None:
        driver = self.driver()
        self.drive(driver, 40)
        for _ in range(60):
            request = driver.next_case("small", "random")
            if request.source == "recipe":
                driver.record(request, array_input([1, 1, 1]), reference_output=b"1")
                continue
            driver.reject(request)
        self.assertFalse(driver.active)
        self.assertTrue(driver.stats.disabled_reason)
        # Falls back cleanly rather than stalling the run.
        self.assertEqual(driver.next_case("small", "random").source, "recipe")

    def test_rejecting_recipe_case_never_disables_mutation(self) -> None:
        driver = self.driver()
        self.drive(driver, 40)
        for _ in range(50):
            driver.reject(CaseRequest("recipe", 1, origin="recipe"))
        self.assertTrue(driver.active)


class DriverPersistenceTests(unittest.TestCase):
    def test_round_trip_restores_cells(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "archive.json")
            first = SearchDriver(ARRAY_CONTRACT, max_bytes=100, rng_seed=5, seed_cases=4,
                                 enumeration_limit=0)
            for index in range(50):
                request = first.next_case("small", "random")
                data = request.data or array_input([(index + i) % 10 for i in range(5)])
                first.record(request, data, reference_output=b"1")
            before = first.report()["diversity"]["cells"]
            first.save(path)

            second = SearchDriver(ARRAY_CONTRACT, max_bytes=100, rng_seed=5, seed_cases=4,
                                  enumeration_limit=0)
            admitted = second.load(path)
            self.assertGreater(admitted, 0)
            self.assertEqual(second.report()["diversity"]["cells"], before)

    def test_restored_driver_mutates_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "archive.json")
            first = SearchDriver(ARRAY_CONTRACT, max_bytes=100, rng_seed=5, seed_cases=30,
                                 enumeration_limit=0)
            for index in range(40):
                request = first.next_case("small", "random")
                data = request.data or array_input([(index + i) % 10 for i in range(6)])
                first.record(request, data, reference_output=b"1")
            first.save(path)

            second = SearchDriver(ARRAY_CONTRACT, max_bytes=100, rng_seed=5, seed_cases=30,
                                  enumeration_limit=0)
            second.load(path)
            sources = set()
            for _ in range(30):
                request = second.next_case("small", "random")
                sources.add(request.source)
                second.record(request, request.data or array_input([1, 2, 3]),
                              reference_output=b"1")
            # Resume must not repeat the seeding phase it already paid for.
            self.assertIn("mutate", sources)

    def test_save_is_noop_when_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "archive.json")
            driver = SearchDriver(OPAQUE_CONTRACT)
            driver.save(path)
            self.assertFalse(Path(path).exists())
            self.assertEqual(driver.load(path), 0)


class DriverConfigTests(unittest.TestCase):
    def test_default_mix_inside_measured_band(self) -> None:
        # Detection measured at 0%: 82%, 25%: 86%, 50%: 88%, 100%: 44%.
        self.assertGreaterEqual(DEFAULT_RECIPE_MIX, 0.25)
        self.assertLessEqual(DEFAULT_RECIPE_MIX, 0.50)

    def test_enumeration_default_is_opt_in(self) -> None:
        self.assertEqual(DEFAULT_ENUMERATION_LIMIT, 0)

    def test_enumeration_is_off_by_default(self) -> None:
        # Measured negative on an ordinary array contract: 110 of 1000 cases
        # spent, detection 86% -> 84%.  Opt-in, not default.
        driver = SearchDriver(ARRAY_CONTRACT, max_bytes=100, rng_seed=2, seed_cases=3)
        for index in range(120):
            request = driver.next_case("small", "random")
            self.assertNotEqual(request.source, "enumerate")
            driver.record(
                request,
                request.data or array_input([(index + i) % 10 for i in range(4)]),
                reference_output=b"1",
            )
        self.assertEqual(driver.report()["search"]["enumerated"], 0)

    def test_invalid_mix_rejected(self) -> None:
        for bad in (-0.1, 1.5):
            with self.assertRaises(ValueError):
                SearchDriver(ARRAY_CONTRACT, recipe_mix=bad)

    def test_pure_recipe_mix_never_mutates(self) -> None:
        driver = SearchDriver(ARRAY_CONTRACT, max_bytes=100, rng_seed=1, seed_cases=2,
                              recipe_mix=1.0, enumeration_limit=0)
        for index in range(50):
            request = driver.next_case("small", "random")
            self.assertEqual(request.source, "recipe")
            driver.record(request, array_input([index % 10, 3, 4]), reference_output=b"1")

    def test_first_seed_is_honoured(self) -> None:
        driver = SearchDriver(ARRAY_CONTRACT, first_seed=7182, max_bytes=100)
        self.assertEqual(driver.next_case("small", "random").seed, 7182)


if __name__ == "__main__":
    unittest.main()
