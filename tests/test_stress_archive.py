from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.stress_archive import (
    Archive,
    EdgeCoverage,
    MISSES_BEFORE_INFEASIBLE,
    axis_domains,
    combined_signal,
    output_class,
)
from tools.acm_agent.stress_corpus import Corpus
from tools.acm_agent.stress_profiler import Profiler, describe_cell, grid_axes
from tests.test_stress_corpus import array_validator, graph_validator
from tests.test_stress_profiler import GRAPH_CONTRACT, list_contract


def build(contract: dict, *, seed: int = 11, max_bytes: int | None = 100, validator=None):
    return Archive(
        Corpus(Profiler(contract), max_bytes=max_bytes, seed=seed, validator=validator)
    )


def explore(archive: Archive, seed_input: bytes, *, budget: int = 200) -> None:
    archive.observe(seed_input, origin="seed")
    for _ in range(budget):
        proposal = archive.propose()
        if proposal is None:
            break
        data, origin, parent = proposal
        archive.observe(data, origin=origin, parent=parent)


class OutputClassTest(unittest.TestCase):
    def test_distinguishes_the_shapes_that_matter(self) -> None:
        self.assertEqual(output_class(b""), "empty")
        self.assertEqual(output_class(b"0\n"), "int:zero")
        self.assertEqual(output_class(b"-4"), "int:negative")
        self.assertEqual(output_class(b"YES"), "word:yes")
        self.assertEqual(output_class(b"1 2\n3 4\n"), "lines:2")

    def test_magnitudes_are_bucketed_not_exact(self) -> None:
        # An exact answer value would make every input novel, which is the same
        # degenerate outcome as measuring nothing at all.
        self.assertEqual(output_class(b"64"), output_class(b"100"))
        self.assertNotEqual(output_class(b"64"), output_class(b"5000"))

    def test_combined_signal_drops_missing_parts(self) -> None:
        self.assertEqual(combined_signal("int:zero", None), ("int:zero",))
        self.assertIsNone(combined_signal(None, None))


class AxisDomainTest(unittest.TestCase):
    def test_count_domain_excludes_zero(self) -> None:
        # A parsed shape always has at least one payload record.
        domains = axis_domains(Profiler(list_contract()), max_bytes=100)
        self.assertNotIn(0, domains["count"])

    def test_byte_budget_narrows_the_count_domain(self) -> None:
        profiler = Profiler(GRAPH_CONTRACT)
        wide = axis_domains(profiler, max_bytes=None)["count"]
        narrow = axis_domains(profiler, max_bytes=100)["count"]
        # 200 edges are permitted by the contract but cannot fit in 100 bytes.
        self.assertLess(len(narrow), len(wide))

    def test_bucket_axes_always_know_their_domain(self) -> None:
        domains = axis_domains(Profiler(list_contract()), max_bytes=100)
        self.assertEqual(domains["distinct"], (0, 1, 2, 3))
        self.assertEqual(domains["negative"], (0, 1))


class CoverageReportTest(unittest.TestCase):
    def test_marginal_coverage_is_the_headline_not_a_fill_rate(self) -> None:
        archive = build(list_contract(), validator=array_validator)
        explore(archive, b"5\n1 2 3 4 5\n")
        report = archive.report()
        # Mutation saturates every axis long before it enumerates the joint
        # space, so a joint fill rate would understate coverage badly.  This is
        # the asymmetry that made "percent of cells filled" the wrong metric.
        self.assertEqual(report["axis_coverage_mean"], 1.0)
        self.assertLess(report["cells"], 4 * 4 * 4 * 3 * 3 * 2)
        self.assertNotIn("fill_rate", report)

    def test_starved_axes_are_named(self) -> None:
        archive = build(list_contract(), validator=array_validator)
        archive.observe(b"3\n1 2 3\n", origin="seed")
        report = archive.report()
        self.assertIn("distinct", report["starved_axes"])
        self.assertEqual(report["next_action"], "target_missing_axis_values")

    def test_next_action_reports_diminishing_returns_when_stuck(self) -> None:
        archive = Archive(
            Corpus(Profiler(list_contract()), max_bytes=100, seed=2, validator=array_validator),
            stagnation_limit=1,
        )
        explore(archive, b"5\n1 2 3 4 5\n")
        report = archive.report()
        self.assertTrue(report["stagnant"])
        self.assertEqual(report["next_action"], "diminishing_returns")

    def test_behaviour_signal_splits_identical_shapes(self) -> None:
        archive = build(list_contract())
        archive.observe(b"5\n1 2 3 4 5\n", signal="int:zero")
        archive.observe(b"5\n2 3 4 5 6\n", signal="int:6")
        self.assertEqual(len(archive), 2)
        self.assertEqual(archive.report()["behaviour_signals"], 2)


class FrontierTest(unittest.TestCase):
    def test_targets_are_one_axis_step_from_something_reached(self) -> None:
        archive = build(list_contract(), validator=array_validator)
        explore(archive, b"5\n1 2 3 4 5\n", budget=60)
        reached = {entry.cell for entry in archive.entries}
        for target in archive.frontier(limit=8):
            self.assertIn(target.neighbour, reached)
            self.assertNotIn(target.cell, reached)
            differing = [
                index
                for index in range(1, min(len(target.cell), len(target.neighbour)))
                if target.cell[index] != target.neighbour[index]
            ]
            self.assertEqual(len(differing), 1)

    def test_arithmetically_impossible_cells_are_not_offered(self) -> None:
        # A one-element array always has max_mult == 100%; offering a cell that
        # says otherwise wastes a provider call.
        archive = build(list_contract(), validator=array_validator)
        archive.observe(b"3\n1 2 3\n", origin="seed")
        for target in archive.frontier(limit=40):
            axes = {
                "count": target.cell[1],
                "max_mult": target.cell[4],
            }
            if axes["count"] <= 1:
                self.assertGreater(axes["max_mult"], 0)

    def test_unseen_axis_values_are_ranked_first(self) -> None:
        archive = build(list_contract(), validator=array_validator)
        archive.observe(b"3\n1 2 3\n", origin="seed")
        missing = {
            coverage.name: {int(value) for value in coverage.missing}
            for coverage in archive.axis_coverage()
        }
        targets = archive.frontier(limit=6)
        self.assertTrue(targets)
        first = targets[0]
        names = [axis.name for axis in grid_axes(str(first.cell[0]))]
        changed = int(first.cell[names.index(first.axis) + 1])
        self.assertIn(changed, missing[first.axis])


class InfeasibilityTest(unittest.TestCase):
    def test_one_miss_keeps_the_cell_in_play(self) -> None:
        # A model can fail a reachable cell for reasons unrelated to the cell,
        # so a single miss must not shrink the search space.
        archive = build(list_contract(), validator=array_validator)
        archive.observe(b"3\n1 2 3\n", origin="seed")
        target = archive.frontier(limit=1)[0]
        self.assertFalse(archive.note_miss(target.cell))
        self.assertNotIn(target.cell, archive.infeasible)

    def test_repeated_misses_retire_the_cell(self) -> None:
        archive = build(list_contract(), validator=array_validator)
        archive.observe(b"3\n1 2 3\n", origin="seed")
        target = archive.frontier(limit=1)[0]
        for _ in range(MISSES_BEFORE_INFEASIBLE - 1):
            archive.note_miss(target.cell)
        self.assertTrue(archive.note_miss(target.cell))
        self.assertIn(target.cell, archive.infeasible)
        self.assertNotIn(
            target.cell, {candidate.cell for candidate in archive.frontier(limit=40)}
        )

    def test_report_counts_retired_cells(self) -> None:
        archive = build(list_contract(), validator=array_validator)
        archive.observe(b"3\n1 2 3\n", origin="seed")
        archive.mark_infeasible([archive.frontier(limit=1)[0].cell])
        self.assertEqual(archive.report()["infeasible_cells"], 1)


class PersistenceTest(unittest.TestCase):
    def test_save_then_load_restores_the_same_cells(self) -> None:
        archive = build(list_contract(), validator=array_validator)
        explore(archive, b"5\n1 2 3 4 5\n", budget=60)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "archive.json"
            archive.save(path)
            self.assertTrue(path.is_file())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["entries"]), len(archive))

            restored = build(list_contract(), validator=array_validator)
            restored.load(path)
            self.assertEqual(
                {entry.cell for entry in restored.entries},
                {entry.cell for entry in archive.entries},
            )

    def test_load_reassigns_cells_instead_of_trusting_the_file(self) -> None:
        # A file written under a different contract must not inject cells the
        # current profiler would never assign.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "archive.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "entries": [
                            {"cell": ["array", 99, 99], "input": "2\n1 2\n", "origin": "x"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            archive = build(list_contract(), validator=array_validator)
            self.assertEqual(archive.load(path), 1)
            self.assertNotIn(("array", 99, 99), {e.cell for e in archive.entries})
            self.assertEqual(archive.entries[0].features["count"], 2)

    def test_load_of_a_missing_file_is_a_noop(self) -> None:
        archive = build(list_contract())
        self.assertEqual(archive.load(Path("does-not-exist.json")), 0)


class EdgeCoverageTest(unittest.TestCase):
    def runner_returning(self, code: int):
        def run(argv, cwd, stdin):
            return code, b"", b""

        return run

    def test_missing_gcov_degrades_instead_of_falling_back(self) -> None:
        # Losing coverage must lose an axis, never bypass the sandbox.
        with tempfile.TemporaryDirectory() as folder:
            coverage = EdgeCoverage(
                runner=self.runner_returning(0),
                workdir=Path(folder),
                executable="brute",
                source_name="brute.cpp",
            )
            self.assertIsNone(coverage.signature(b"1\n"))
            self.assertEqual(coverage.unavailable, "gcov_no_output")

    def test_a_crashing_program_is_its_own_signature(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            coverage = EdgeCoverage(
                runner=self.runner_returning(134),
                workdir=Path(folder),
                executable="brute",
                source_name="brute.cpp",
            )
            self.assertEqual(coverage.signature(b"1\n"), "exit:134")

    def test_executed_lines_become_a_stable_signature(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            (work / "brute.cpp.gcov").write_text(
                "        -:    0:Source:brute.cpp\n"
                "        1:    1:int main() {\n"
                "    #####:    2:  unreachable();\n"
                "        3:    3:  return 0;\n",
                encoding="utf-8",
            )
            coverage = EdgeCoverage(
                runner=self.runner_returning(0),
                workdir=work,
                executable="brute",
                source_name="brute.cpp",
            )
            first = coverage.signature(b"1\n")
            self.assertIsNotNone(first)
            # Two executed lines; the never-run line is excluded.
            self.assertTrue(first.startswith("cov:2:"))
            self.assertEqual(first, coverage.signature(b"1\n"))

    def test_unavailable_provider_stays_silent(self) -> None:
        coverage = EdgeCoverage(
            runner=self.runner_returning(0),
            workdir=Path("."),
            executable="brute",
            source_name="brute.cpp",
            unavailable="sandbox_unavailable",
        )
        self.assertIsNone(coverage.signature(b"1\n"))


class DescribeCellTest(unittest.TestCase):
    def test_cells_render_for_humans(self) -> None:
        archive = build(list_contract(), validator=array_validator)
        archive.observe(b"5\n1 2 3 4 5\n")
        rendered = describe_cell(archive.entries[0].cell)
        self.assertTrue(rendered.startswith("array["))
        self.assertIn("distinct=", rendered)


if __name__ == "__main__":
    unittest.main()
