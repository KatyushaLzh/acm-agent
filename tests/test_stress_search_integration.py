"""End-to-end: the search driver wired into ``LayeredStressRunner``.

``test_stress_search.py`` covers the driver alone.  These tests exercise the
``stress.py`` seam — that synthesized inputs really reach the solution, that the
generator is skipped for them, that a validator rejection does not look like a
generator bug, and that a run without a contract behaves exactly as it did
before the driver existed.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.stress import (
    LayeredStressRunner,
    SandboxCapability,
    SandboxProcessResult,
    StressExecutables,
    StressRunConfig,
)
from tools.acm_agent.stress_search import DEFAULT_SEED_CASES

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

_REQUIRED_CASES = [
    {"profile": "small", "case_kind": "lower_bound"},
    {"profile": "small", "case_kind": "random"},
    {"profile": "large", "case_kind": "upper_bound"},
    {"profile": "large", "case_kind": "random"},
]


def parse_array(data: bytes) -> list[int] | None:
    try:
        tokens = [int(token) for token in data.split()]
    except ValueError:
        return None
    if not tokens or tokens[0] != len(tokens) - 1:
        return None
    return tokens[1:]


class _ArraySandbox:
    """Fake sandbox emitting valid arrays, recording every input it is handed."""

    def __init__(self, *, reject_synthetic: bool = False, with_validator: bool = False) -> None:
        self.reject_synthetic = reject_synthetic
        self.with_validator = with_validator
        self.generator_seeds: list[int] = []
        self.solution_inputs: list[bytes] = []
        self.validator_inputs: list[bytes] = []

    def probe(self) -> SandboxCapability:
        return SandboxCapability(True, "", "fake")

    def cancel(self) -> None:
        pass

    def _generated(self, seed: int, profile: str) -> bytes:
        count = 4 if profile == "small" else 12
        values = [(seed * 7 + index * 3) % 10 for index in range(count)]
        return f"{count}\n{' '.join(str(value) for value in values)}\n".encode()

    def run(self, command, *, cwd, input_data=None, env=None, limits=None):
        role = Path(command[0]).stem
        if "generator" in role:
            if "--capabilities" in command:
                payload = {
                    "profile_version": 2,
                    "manifest_version": 1,
                    "profiles": ["small", "large"],
                    "case_kinds": ["lower_bound", "random", "upper_bound"],
                    "supported_cases": _REQUIRED_CASES,
                }
                return SandboxProcessResult(list(command), 0, json.dumps(payload).encode())
            if len(command) == 5 and command[1] == "--manifest":
                seed, profile, case_kind = int(command[2]), command[3], command[4]
                generated = self._generated(seed, profile)
                payload = {
                    "manifest_version": 1,
                    "profile": profile,
                    "case_kind": case_kind,
                    "seed": seed,
                    "input_sha256": hashlib.sha256(generated).hexdigest(),
                    "dimensions": {"n": len(generated.split()) - 1},
                    "coverage_tags": [],
                    "records": len(generated.split()) - 1,
                    "total_complexity": "linear_output",
                }
                return SandboxProcessResult(list(command), 0, json.dumps(payload).encode())
            seed, profile = int(command[1]), command[2]
            self.generator_seeds.append(seed)
            return SandboxProcessResult(list(command), 0, self._generated(seed, profile))
        if "validator" in role:
            self.validator_inputs.append(input_data or b"")
            values = parse_array(input_data or b"")
            ok = values is not None and all(0 <= value <= 9 for value in values)
            if ok and self.reject_synthetic and self.solution_inputs:
                # Reject anything that did not come straight from the generator.
                generated = {self._generated(seed, "small") for seed in self.generator_seeds}
                if (input_data or b"") not in generated:
                    ok = False
            if ok:
                payload: dict[str, object] = {
                    "valid": True,
                    "dimensions": {"n": len(values or [])},
                    "coverage_tags": [],
                    "records": len(values or []),
                }
            else:
                # The only rejection shape trusted code accepts.
                payload = {
                    "valid": False,
                    "dimensions": {},
                    "coverage_tags": [],
                    "records": 0,
                }
            return SandboxProcessResult(list(command), 0, json.dumps(payload).encode())
        if "solution" in role:
            self.solution_inputs.append(input_data or b"")
        values = parse_array(input_data or b"") or []
        runs = sum(1 for i in range(1, len(values)) if values[i] != values[i - 1]) + 1
        return SandboxProcessResult(list(command), 0, f"{runs}\n".encode())


def executables(root: Path, *, validator: bool = False) -> StressExecutables:
    names = ["solution", "generator", "reference_primary", "reference_secondary"]
    paths = {name: root / f"{name}.exe" for name in names}
    if validator:
        paths["validator"] = root / "validator.exe"
    for path in paths.values():
        path.write_bytes(b"fake executable")
    kwargs: dict[str, Path] = {
        "solution": paths["solution"],
        "generator": paths["generator"],
        "reference_primary": paths["reference_primary"],
        "reference_secondary": paths["reference_secondary"],
    }
    if validator:
        kwargs["validator"] = paths["validator"]
    return StressExecutables(**kwargs)


class RunnerWiringTests(unittest.TestCase):
    def run_stress(
        self,
        *,
        contract: object | None,
        max_cases: int,
        sandbox: _ArraySandbox | None = None,
        validator: bool = False,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            box = sandbox or _ArraySandbox()
            runner = LayeredStressRunner(
                root,
                "CF1A",
                executables(root, validator=validator),
                box,
                contract=contract,  # type: ignore[arg-type]
            )
            config = StressRunConfig(
                first_seed=1,
                max_cases=max_cases,
                include_large=False,
                large_per_cycle=0,
                warmup_small_cases=0,
            )
            result = runner.run(config)
            return result, box

    def test_no_contract_keeps_pre_driver_behaviour(self) -> None:
        result, sandbox = self.run_stress(contract=None, max_cases=30)
        self.assertEqual(result.status, "limit_reached")
        self.assertEqual(result.search, {})
        # Every case came from the generator, one seed each, contiguous.
        self.assertEqual(len(sandbox.generator_seeds), 30)
        self.assertEqual(sandbox.generator_seeds, list(range(1, 31)))
        self.assertEqual(result.next_seed, 31)

    def test_unparseable_contract_falls_back_to_recipe_only(self) -> None:
        result, sandbox = self.run_stress(
            contract={"syntax": {"mode": "operation_stream", "sections": []}}, max_cases=20
        )
        self.assertEqual(result.status, "limit_reached")
        self.assertEqual(result.search, {})
        self.assertEqual(len(sandbox.generator_seeds), 20)

    def test_synthesized_inputs_reach_the_solution(self) -> None:
        cases = DEFAULT_SEED_CASES + 60
        result, sandbox = self.run_stress(contract=ARRAY_CONTRACT, max_cases=cases)
        self.assertEqual(result.status, "limit_reached")
        self.assertEqual(len(sandbox.solution_inputs), cases)
        # Fewer generator invocations than cases proves the driver supplied some.
        self.assertLess(len(sandbox.generator_seeds), cases)
        stats = result.search["search"]
        self.assertEqual(
            stats["recipe"] + stats["mutated"] + stats["enumerated"], cases
        )
        self.assertGreater(stats["mutated"] + stats["enumerated"], 0)

    def test_generator_seed_count_matches_recipe_case_count(self) -> None:
        cases = DEFAULT_SEED_CASES + 60
        result, sandbox = self.run_stress(contract=ARRAY_CONTRACT, max_cases=cases)
        self.assertEqual(len(sandbox.generator_seeds), result.search["search"]["recipe"])

    def test_every_solution_input_is_contract_valid(self) -> None:
        _result, sandbox = self.run_stress(
            contract=ARRAY_CONTRACT, max_cases=DEFAULT_SEED_CASES + 80
        )
        for data in sandbox.solution_inputs:
            values = parse_array(data)
            self.assertIsNotNone(values, msg=f"unparseable input: {data!r}")
            assert values is not None
            self.assertTrue(all(0 <= value <= 9 for value in values))
            self.assertTrue(1 <= len(values) <= 40)

    def test_seeds_stay_unique_and_monotonic(self) -> None:
        result, sandbox = self.run_stress(
            contract=ARRAY_CONTRACT, max_cases=DEFAULT_SEED_CASES + 40
        )
        self.assertEqual(
            len(sandbox.generator_seeds), len(set(sandbox.generator_seeds))
        )
        self.assertEqual(sandbox.generator_seeds, sorted(sandbox.generator_seeds))
        self.assertEqual(result.next_seed, 1 + DEFAULT_SEED_CASES + 40)

    def test_diversity_report_is_attached(self) -> None:
        result, _sandbox = self.run_stress(
            contract=ARRAY_CONTRACT, max_cases=DEFAULT_SEED_CASES + 40
        )
        diversity = result.search["diversity"]
        self.assertGreater(diversity["cells"], 1)
        self.assertIn("starved_axes", diversity)

    def test_driver_beats_recipe_only_on_cells(self) -> None:
        cases = DEFAULT_SEED_CASES + 120
        with_driver, _ = self.run_stress(contract=ARRAY_CONTRACT, max_cases=cases)
        # Recipe-only run scored by the same archive, for an apples-to-apples
        # comparison of what the two case streams actually cover.
        from tools.acm_agent.stress_archive import Archive, output_class
        from tools.acm_agent.stress_corpus import Corpus
        from tools.acm_agent.stress_profiler import Profiler

        _result, sandbox = self.run_stress(contract=None, max_cases=cases)
        archive = Archive(Corpus(Profiler(ARRAY_CONTRACT), max_bytes=100, seed=1))
        for data in sandbox.solution_inputs:
            values = parse_array(data) or []
            runs = sum(1 for i in range(1, len(values)) if values[i] != values[i - 1]) + 1
            archive.observe(data, origin="recipe", signal=output_class(str(runs).encode()))
        self.assertGreater(
            with_driver.search["diversity"]["cells"], archive.report(target_limit=0)["cells"]
        )


class ValidatorRejectionTests(unittest.TestCase):
    def test_rejected_synthetic_input_is_not_a_generator_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = _ArraySandbox(reject_synthetic=True, with_validator=True)
            runner = LayeredStressRunner(
                root,
                "CF1A",
                executables(root, validator=True),
                sandbox,
                contract=ARRAY_CONTRACT,  # type: ignore[arg-type]
            )
            config = StressRunConfig(
                first_seed=1,
                max_cases=DEFAULT_SEED_CASES + 60,
                include_large=False,
                large_per_cycle=0,
                warmup_small_cases=0,
            )
            result = runner.run(config)
            # The run must finish normally: a mutation the validator dislikes is
            # discarded, never reported as `generated_input_rejected`.
            self.assertEqual(result.status, "limit_reached")
            self.assertIsNone(result.failure_dir)
            self.assertGreater(result.search["search"]["rejected"], 0)

    def test_sustained_rejection_disables_mutation_and_run_survives(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = _ArraySandbox(reject_synthetic=True, with_validator=True)
            runner = LayeredStressRunner(
                root,
                "CF1A",
                executables(root, validator=True),
                sandbox,
                contract=ARRAY_CONTRACT,  # type: ignore[arg-type]
            )
            config = StressRunConfig(
                first_seed=1,
                max_cases=DEFAULT_SEED_CASES + 200,
                include_large=False,
                large_per_cycle=0,
                warmup_small_cases=0,
            )
            result = runner.run(config)
            self.assertEqual(result.status, "limit_reached")
            self.assertTrue(result.search["search"]["disabled_reason"])


class ArchivePersistenceTests(unittest.TestCase):
    def test_archive_survives_the_run_directory_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = _ArraySandbox()
            runner = LayeredStressRunner(
                root, "CF1A", executables(root), sandbox,
                contract=ARRAY_CONTRACT,  # type: ignore[arg-type]
            )
            config = StressRunConfig(
                first_seed=1,
                max_cases=DEFAULT_SEED_CASES + 40,
                include_large=False,
                large_per_cycle=0,
                warmup_small_cases=0,
            )
            runner.run(config)
            archive = root / ".acm" / "stress-archives" / "CF1A.json"
            self.assertTrue(archive.is_file())
            payload = json.loads(archive.read_text(encoding="utf-8"))
            self.assertTrue(payload["entries"])

    def test_second_run_warm_starts_from_the_saved_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = StressRunConfig(
                first_seed=1,
                max_cases=DEFAULT_SEED_CASES + 40,
                include_large=False,
                large_per_cycle=0,
                warmup_small_cases=0,
            )
            paths = executables(root)
            first = LayeredStressRunner(
                root, "CF1A", paths, _ArraySandbox(),
                contract=ARRAY_CONTRACT,  # type: ignore[arg-type]
            )
            first_result = first.run(config)

            second_sandbox = _ArraySandbox()
            second = LayeredStressRunner(
                root, "CF1A", paths, second_sandbox,
                contract=ARRAY_CONTRACT,  # type: ignore[arg-type]
            )
            second_result = second.run(config)
            # Restored elites mean the second run starts from a populated
            # archive, so it reaches at least as many cells as the first.
            self.assertGreaterEqual(
                second_result.search["diversity"]["cells"],
                first_result.search["diversity"]["cells"],
            )

    def test_corrupt_archive_does_not_break_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / ".acm" / "stress-archives" / "CF1A.json"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_text("{ not valid json", encoding="utf-8")
            runner = LayeredStressRunner(
                root, "CF1A", executables(root), _ArraySandbox(),
                contract=ARRAY_CONTRACT,  # type: ignore[arg-type]
            )
            config = StressRunConfig(
                first_seed=1, max_cases=30, include_large=False,
                large_per_cycle=0, warmup_small_cases=0,
            )
            result = runner.run(config)
            self.assertEqual(result.status, "limit_reached")


if __name__ == "__main__":
    unittest.main()
