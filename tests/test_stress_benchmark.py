from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import tools.acm_agent.stress_benchmark as stress_benchmark
from unittest import mock

from tools.acm_agent.stress_benchmark import (
    ApplicationColdGateError,
    CORE8,
    LiveColdFixtureError,
    LiveColdProblem,
    build_core8_live_cold_attempt_plan,
    core8_random_valid_corpus,
    evaluate_reliability_release,
    load_core8_live_cold_fixtures,
    load_live_cold_problem_fixture,
    run_application_cold,
    run_core8_gold_gate,
    summarize_attempts,
    _wait_for_live_run,
    write_benchmark_report,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class LiveColdFixtureTests(unittest.TestCase):
    def test_checked_in_core8_primaries_match_random_gold_corpus(self) -> None:
        compiler = shutil.which("g++")
        self.assertIsNotNone(compiler, "formal core8 fixture verification requires g++")
        root = Path(__file__).parent / "fixtures" / "stress_live" / "core8"
        problems = load_core8_live_cold_fixtures(root)
        gold_by_id = {gold.problem_id: gold for gold in CORE8}
        with tempfile.TemporaryDirectory() as temp:
            build = Path(temp)
            for problem in problems:
                executable = build / f"{problem.problem_id}.exe"
                compiled = subprocess.run(
                    [
                        str(compiler),
                        "-std=c++17",
                        "-O2",
                        str(root / problem.problem_id / "primary.cpp"),
                        "-o",
                        str(executable),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    compiled.returncode,
                    0,
                    f"{problem.problem_id} compile failed:\n"
                    + compiled.stderr.decode("utf-8", errors="replace"),
                )
                corpus = (
                    *gold_by_id[problem.problem_id].valid_cases,
                    *core8_random_valid_corpus(problem.problem_id, case_count=40),
                )
                for case_index, input_data in enumerate(corpus):
                    expected = gold_by_id[problem.problem_id].oracle(input_data)
                    completed = subprocess.run(
                        [str(executable)],
                        input=input_data,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=3,
                        check=False,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        f"{problem.problem_id} case {case_index} exited "
                        f"{completed.returncode}: "
                        + completed.stderr.decode("utf-8", errors="replace"),
                    )
                    self.assertEqual(
                        completed.stdout.split(),
                        expected.split(),
                        f"{problem.problem_id} case {case_index} disagrees with gold",
                    )

    def test_hash_locked_fixture_loads_and_checks_sample_with_gold(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = Path(temp) / "P1001"
            samples = fixture / "samples"
            samples.mkdir(parents=True)
            statement = b"Add two signed integers in [-1000000000, 1000000000].\n"
            source = (
                b"#include <iostream>\n"
                b"int main(){long long a,b;std::cin>>a>>b;"
                b"std::cout<<a+b<<'\\n';}\n"
            )
            input_data = b"1 2\n"
            output_data = b"3\n"
            (fixture / "statement.md").write_bytes(statement)
            (fixture / "primary.cpp").write_bytes(source)
            (samples / "1.in").write_bytes(input_data)
            (samples / "1.out").write_bytes(output_data)
            manifest = {
                "schema_version": 1,
                "platform": "luogu",
                "problem_id": "P1001",
                "title": "A+B Problem",
                "statement_file": "statement.md",
                "statement_sha256": _sha256(statement),
                "primary_source_file": "primary.cpp",
                "primary_source_sha256": _sha256(source),
                "samples": [
                    {
                        "name": "official-1",
                        "input_file": "samples/1.in",
                        "input_sha256": _sha256(input_data),
                        "output_file": "samples/1.out",
                        "output_sha256": _sha256(output_data),
                    }
                ],
            }
            (fixture / "problem.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            loaded = load_live_cold_problem_fixture(fixture)
            self.assertEqual(loaded.problem_id, "P1001")
            self.assertEqual(loaded.samples, (("official-1", b"1 2\n", b"3\n"),))
            self.assertEqual(loaded.primary_source, source.decode())

            (samples / "1.out").write_bytes(b"4\n")
            with self.assertRaisesRegex(LiveColdFixtureError, "sha256 mismatch"):
                load_live_cold_problem_fixture(fixture)

    def test_fixture_rejects_paths_outside_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "P1001"
            fixture.mkdir()
            outside = root / "statement.md"
            outside.write_text("statement", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "platform": "luogu",
                "problem_id": "P1001",
                "title": "A+B Problem",
                "statement_file": "../statement.md",
                "statement_sha256": _sha256(outside.read_bytes()),
                "primary_source_file": "primary.cpp",
                "primary_source_sha256": "0" * 64,
                "samples": [],
            }
            (fixture / "problem.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(LiveColdFixtureError, "escapes or is missing"):
                load_live_cold_problem_fixture(fixture)

    def test_formal_plan_reuses_first_three_p2596_attempts(self) -> None:
        fixtures: list[LiveColdProblem] = []
        for gold in CORE8:
            sample_input = gold.valid_cases[0]
            platform = "codeforces" if gold.problem_id.startswith("CF") else "luogu"
            fixtures.append(
                LiveColdProblem(
                    platform=platform,
                    problem_id=gold.problem_id,
                    title=gold.problem_id,
                    statement=f"Complete benchmark statement for {gold.problem_id}",
                    primary_source="int main(){return 0;}\n",
                    samples=(("gold-checked", sample_input, gold.oracle(sample_input)),),
                )
            )

        plan = build_core8_live_cold_attempt_plan(fixtures)
        self.assertEqual(len(plan), 41)
        self.assertEqual([item.problem_id for item in plan[:20]], ["P2596"] * 20)
        counts = {
            problem_id: sum(item.problem_id == problem_id for item in plan)
            for problem_id in {gold.problem_id for gold in CORE8}
        }
        self.assertEqual(counts["P2596"], 20)
        self.assertTrue(
            all(count == 3 for problem_id, count in counts.items() if problem_id != "P2596")
        )

    def test_live_batch_warm_checks_each_problem_once_and_resumes(self) -> None:
        problems = [
            LiveColdProblem(
                platform="luogu",
                problem_id=problem_id,
                title=problem_id,
                statement=f"statement {problem_id}",
                primary_source="int main(){return 0;}\n",
                samples=(),
            )
            for problem_id in ("P1001", "P1001", "P1111", "P1111")
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            protected = root / "protected"
            protected.mkdir()
            report = root / "report"
            warm_flags: list[bool] = []

            def fake_attempt(problem, **kwargs):
                warm = bool(kwargs["verify_warm_cache"])
                warm_flags.append(warm)
                return {
                    "attempt_index": kwargs["attempt_index"],
                    "problem_id": problem.problem_id,
                    "platform": problem.platform,
                    "ok": True,
                    "wall_seconds": 1,
                    "provider_requests": 1,
                    "total_tokens": 1,
                    "warm_cache": (
                        {"ok": True, "provider_requests": 0, "total_tokens": 0}
                        if warm
                        else None
                    ),
                }

            with mock.patch.object(stress_benchmark, "_live_attempt", side_effect=fake_attempt):
                first = stress_benchmark.run_live_ai_cold_batch(
                    problems,
                    client_factory=lambda: object(),
                    report_directory=report,
                    protected_workspace=protected,
                    verify_warm_cache=True,
                )
            self.assertEqual(len(first), 4)
            self.assertEqual(warm_flags, [True, False, True, False])

            with mock.patch.object(stress_benchmark, "_live_attempt") as resumed_call:
                resumed = stress_benchmark.run_live_ai_cold_batch(
                    problems,
                    client_factory=lambda: object(),
                    report_directory=report,
                    protected_workspace=protected,
                    verify_warm_cache=True,
                    resume=True,
                )
            self.assertEqual(resumed, first)
            resumed_call.assert_not_called()

    def test_live_batch_dynamic_protection_catches_other_problem_source(self) -> None:
        problem = LiveColdProblem(
            platform="luogu",
            problem_id="P1111",
            title="P1111",
            statement="statement P1111",
            primary_source="int main(){return 0;}\n",
            samples=(),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            protected = root / "protected"
            source = protected / "2026" / "P1111.gen.cpp"
            source.parent.mkdir(parents=True)
            source.write_text("before\n", encoding="utf-8")

            def mutate(_problem, **kwargs):
                source.write_text("after\n", encoding="utf-8")
                return {
                    "attempt_index": kwargs["attempt_index"],
                    "problem_id": "P1111",
                    "platform": "luogu",
                    "ok": True,
                    "provider_requests": 1,
                    "total_tokens": 1,
                }

            with mock.patch.object(stress_benchmark, "_live_attempt", side_effect=mutate):
                with self.assertRaisesRegex(
                    ApplicationColdGateError, "protected workspace changed"
                ):
                    stress_benchmark.run_live_ai_cold_batch(
                        [problem],
                        client_factory=lambda: object(),
                        report_directory=root / "report",
                        protected_workspace=protected,
                        verify_warm_cache=False,
                    )

    def test_release_gate_evaluates_full_41_plus_20_plan(self) -> None:
        attempts: list[dict[str, object]] = []

        def add(problem_id: str, ok: bool, warm: bool) -> None:
            attempts.append(
                {
                    "attempt_index": len(attempts) + 1,
                    "problem_id": problem_id,
                    "ok": ok,
                    "wall_seconds": 100 if ok else 500,
                    "total_tokens": 20000 if ok else 50000,
                    "unsafe_apply": False,
                    "warm_cache": (
                        {"ok": True, "provider_requests": 0, "total_tokens": 0}
                        if warm
                        else None
                    ),
                }
            )

        for index in range(20):
            add("P2596", index < 16, index == 0)
        for gold in CORE8:
            if gold.problem_id == "P2596":
                continue
            for index in range(3):
                add(gold.problem_id, True, index == 0)
        local = [
            {
                "ok": True,
                "wall_seconds": 50,
                "provider_requests": 0,
                "total_tokens": 0,
                "application_cold": True,
                "registered_processes_cleaned": True,
                "unsafe_apply": False,
            }
            for _ in range(20)
        ]

        release = evaluate_reliability_release(attempts, local)
        self.assertTrue(release["ok"], release)
        self.assertEqual(release["metrics"]["p2596_successes"], 16)
        self.assertEqual(release["metrics"]["core8_successes"], 24)
        self.assertTrue(
            release["checks"]["p2596_successes_at_least_16_of_20"]
        )
        self.assertTrue(
            release["checks"]["core8_successes_at_least_20_of_24"]
        )
        self.assertTrue(
            release["checks"]["successful_ai_attempts_at_most_100000_tokens"]
        )

        attempts[0]["total_tokens"] = 100001
        token_rejected = evaluate_reliability_release(attempts, local)
        self.assertFalse(token_rejected["ok"])
        self.assertFalse(
            token_rejected["checks"]["successful_ai_attempts_at_most_100000_tokens"]
        )
        attempts[0]["total_tokens"] = 20000

        attempts[-1]["unsafe_apply"] = True
        rejected = evaluate_reliability_release(attempts, local)
        self.assertFalse(rejected["ok"])
        self.assertFalse(rejected["checks"]["zero_unsafe_applies"])


class GoldGateTests(unittest.TestCase):
    def test_core8_accepts_all_valid_and_rejects_all_mutants(self) -> None:
        result = run_core8_gold_gate()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["false_accepts"], 0)
        self.assertEqual(result["false_rejects"], 0)
        self.assertEqual(len(result["problems"]), 8)

    def test_statistics_use_nearest_rank_and_report_is_durable(self) -> None:
        attempts = [
            {
                "problem_id": "P2596",
                "ok": True,
                "first_round_success": True,
                "wall_seconds": 1,
                "total_tokens": 10,
                "tokens": {"contract": 2, "generator": 8},
                "stage_seconds": {"contract": 0.25, "preflight": 0.75},
                "provider_requests": {"contract": 1, "generator": 1},
                "retries": 0,
            },
            {
                "problem_id": "P2596",
                "ok": False,
                "wall_seconds": 4,
                "total_tokens": 40,
                "tokens": {"contract": 4, "generator": 36},
                "stage_seconds": {"contract": 1, "preflight": 3},
                "provider_requests": 3,
                "retries": 1,
                "repair_roles": ["generator"],
                "failure_stage": "preflight",
            },
            {
                "problem_id": "P1001",
                "ok": True,
                "wall_seconds": 2,
                "total_tokens": 20,
                "tokens": {"contract": 5, "generator": 15},
                "stage_seconds": {"contract": 1, "preflight": 1},
                "provider_requests": 2,
                "retries": 0,
                "repair_roles": ["validator"],
            },
        ]
        summary = summarize_attempts(attempts)
        self.assertEqual(summary["success_rate"], 2 / 3)
        self.assertEqual(summary["first_round_success_rate"], 1 / 3)
        self.assertEqual(summary["wall_seconds"]["p50"], 2)
        self.assertEqual(summary["wall_seconds"]["p95"], 4)
        self.assertEqual(summary["successful_wall_seconds"]["p95"], 2)
        self.assertEqual(summary["failure_stages"], {"preflight": 1})
        self.assertEqual(summary["repair_roles"], {"generator": 1, "validator": 1})
        self.assertEqual(summary["provider_requests"]["total"], 7)
        self.assertEqual(summary["retries"]["total"], 1)
        self.assertEqual(summary["stage_seconds"]["preflight"]["p50"], 1)
        self.assertEqual(summary["token_categories"]["generator"]["p95"], 36)
        self.assertEqual(summary["by_problem"]["P2596"]["attempts"], 2)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "report"
            persisted = write_benchmark_report(directory, attempts)
            self.assertEqual(persisted, summary)
            self.assertEqual(
                len((directory / "attempts.jsonl").read_text(encoding="utf-8").splitlines()),
                3,
            )
            self.assertEqual(
                json.loads((directory / "summary.json").read_text(encoding="utf-8")),
                summary,
            )
            with (directory / "attempts.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["tokens.generator"], "8.0")
            self.assertEqual(rows[1]["failure_stage"], "preflight")
            markdown = (directory / "report.md").read_text(encoding="utf-8")
            self.assertIn("First-round success rate", markdown)
            self.assertIn("Token category percentiles", markdown)

    def test_audit_attempt_is_not_reported_as_semantic_repair(self) -> None:
        summary = summarize_attempts(
            [
                {
                    "problem_id": "P2596",
                    "ok": False,
                    "error": {
                        "details": {
                            "primary_failure": {
                                "role": "generator",
                                "stage": "audit_helpers",
                                "attempts": 1,
                            },
                            "roles": {
                                "generator": {
                                    "stage": "audit_helpers",
                                    "attempts": 1,
                                }
                            },
                        }
                    },
                }
            ]
        )
        self.assertEqual(summary["repair_roles"], {})


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        assert self.returncode is not None
        return self.returncode


class ApplicationColdRunnerTests(unittest.TestCase):
    def test_controlled_run_timeout_requests_stop_before_reporting_failure(self) -> None:
        class RunningCoordinator:
            def __init__(self) -> None:
                self.stopped: list[str] = []

            def run(self, run_id: str):
                return {
                    "id": run_id,
                    "status": "running",
                    "phase": "large",
                    "small_count": 16,
                    "large_count": 2,
                    "total_count": 18,
                }

            def stop(self, run_id: str) -> None:
                self.stopped.append(run_id)

        coordinator = RunningCoordinator()
        with mock.patch(
            "tools.acm_agent.stress_benchmark.time.monotonic",
            side_effect=[100.0, 102.0],
        ), self.assertRaisesRegex(
            ApplicationColdGateError,
            "controlled stress run exceeded 1 seconds",
        ):
            _wait_for_live_run(
                coordinator,
                "run-timeout",
                timeout_seconds=1.0,
                progress=None,
                attempt_index=3,
                attempt_count=20,
            )

        self.assertEqual(coordinator.stopped, ["run-timeout"])

    def test_each_attempt_gets_fresh_database_and_is_fully_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            protected = root / "real"
            protected.mkdir()
            marker = protected / "solution.cpp"
            marker.write_text("int main() {}\n", encoding="utf-8")
            temp_parent = root / "isolated"
            temp_parent.mkdir()
            seen: list[Path] = []
            cleanups: list[int] = []
            processes: list[FakeProcess] = []

            def attempt(context):
                self.assertFalse(context.database_path.exists())
                self.assertTrue(context.workspace.is_dir())
                self.assertNotIn(context.workspace, seen)
                seen.append(context.workspace)
                context.database_path.write_bytes(b"")
                (context.workspace / "helper.gen.cpp").write_text("generated", encoding="utf-8")
                context.register_cleanup(lambda: cleanups.append(context.attempt_index))
                process = context.register_process(FakeProcess())
                processes.append(process)
                return {
                    "problem_id": "P2596",
                    "ok": True,
                    "wall_seconds": context.attempt_index,
                    "provider_requests": 0,
                    "total_tokens": 0,
                }

            results = run_application_cold(
                2,
                attempt,
                protected_workspace=protected,
                temp_parent=temp_parent,
            )
            self.assertEqual(len(results), 2)
            self.assertEqual([item["attempt_index"] for item in results], [1, 2])
            self.assertTrue(all(item["application_cold"] for item in results))
            self.assertTrue(all(item["registered_processes_cleaned"] for item in results))
            self.assertTrue(all(process.terminated for process in processes))
            self.assertEqual(cleanups, [1, 2])
            self.assertTrue(all(not workspace.exists() for workspace in seen))
            self.assertEqual(marker.read_text(encoding="utf-8"), "int main() {}\n")
            self.assertEqual(list(temp_parent.iterdir()), [])

    def test_provider_evidence_is_mandatory_and_nonzero_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            protected = root / "real"
            protected.mkdir()
            temp_parent = root / "isolated"
            temp_parent.mkdir()

            with self.assertRaisesRegex(ApplicationColdGateError, "provider request"):
                run_application_cold(
                    1,
                    lambda _context: {
                        "ok": False,
                        "provider_requests": 1,
                        "total_tokens": 0,
                    },
                    protected_workspace=protected,
                    temp_parent=temp_parent,
                )
            self.assertEqual(list(temp_parent.iterdir()), [])

    def test_protected_workspace_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            protected = root / "real"
            protected.mkdir()
            marker = protected / "state.db"
            marker.write_bytes(b"before")
            temp_parent = root / "isolated"
            temp_parent.mkdir()

            def mutate(_context):
                marker.write_bytes(b"after")
                return {"ok": True, "provider_requests": 0, "total_tokens": 0}

            with self.assertRaisesRegex(ApplicationColdGateError, "protected workspace changed"):
                run_application_cold(
                    1,
                    mutate,
                    protected_workspace=protected,
                    temp_parent=temp_parent,
                )


if __name__ == "__main__":
    unittest.main()
