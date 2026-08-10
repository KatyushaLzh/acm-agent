from __future__ import annotations

from dataclasses import replace
import json
import hashlib
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools.acm_agent.config import Paths
from tools.acm_agent.storage import Database
from tools.acm_agent.stress import (
    GeneratorCapabilityError,
    HelperPreflightError,
    LayeredStressRunner,
    SandboxCapability,
    SandboxProcessResult,
    SandboxUnavailableError,
    StressExecutables,
    StressRunConfig,
    _GENERATOR_OPTIONAL_CASE,
    _GENERATOR_REQUIRED_CASES,
)
from tools.acm_agent.stress_runtime import (
    STRESS_ARTIFACT_PROMPT_VERSION,
    STRESS_BLUEPRINT_PROMPT_VERSION,
    STRESS_CONTRACT_PROMPT_VERSION,
    STRESS_RECIPE_PROMPT_VERSION,
    StressCoordinator,
    StressRuntimeError,
    _contract_probe_repair_diagnostic,
    _generator_safe_seed_families,
    _generator_record_count_hint,
    _generator_repair_invariants,
    _initial_repair_counts,
    _next_external_reference,
    normalize_stress_failure,
    _preflight_repair_witness,
    _preflight_repair_from_scratch,
    _preflight_role_repair_limit,
    _persistent_generator_seed_requirement,
    _run_locally_confirmed_gate,
    _role_failure_details,
    _preparation_failure_label,
    _stress_config,
)
from tools.acm_agent.stress_ai import GeneratedArtifact, StressPreparation
from tools.acm_agent.stress_budget import PreparationBudget, PreparationBudgetExhausted
from tests.helpers.fakes import FakeChatResult as Result


SAFE = "#include <iostream>\nint main(){return 0;}\n"
SAFE_SECONDARY = "#include <iostream>\n// independently generated secondary reference\nint main(){return 0;}\n"
GENERATOR = (
    "#include <iostream>\n#include <string>\n"
    "void acm_generate_case(unsigned long long,const std::string&,"
    "const std::string&,std::ostream&){}\n"
)
VALIDATOR = SAFE


BLUEPRINT = {
    "schema_version": 1,
    # Policy: only the three core pairs are *required*.  ``small/lower_bound``
    # is declarable-optional, so it must never appear here -- it lives in
    # ``cases`` below, which is what the capability handshake advertises.
    # Derived from the source constant so a policy change moves this fixture.
    "required_cases": [dict(pair) for pair in _GENERATOR_REQUIRED_CASES],
    "dimensions": [{"name": "n", "lower": 1, "upper": 100}],
    "operation_families": ["update", "query"],
    "required_coverage_tags": ["update", "query", "boundary"],
    "large_required_coverage_tags": ["update", "query"],
    "cases": [
        {"profile": "small", "case_kind": "lower_bound", "dimensions": {"n": 1}, "operation_families": [], "coverage_tags": ["boundary"], "uses_seed": False, "construction": "minimum", "total_complexity": "O(output_size)"},
        {"profile": "small", "case_kind": "random", "dimensions": {"n": "1..8"}, "operation_families": ["update", "query"], "coverage_tags": ["update", "query", "boundary"], "uses_seed": True, "construction": "seeded coverage", "total_complexity": "O(output_size)"},
        {"profile": "large", "case_kind": "upper_bound", "dimensions": {"n": 100}, "operation_families": ["update", "query"], "coverage_tags": ["update", "query"], "uses_seed": False, "construction": "stream upper", "total_complexity": "O(output_size)"},
        {"profile": "large", "case_kind": "random", "dimensions": {"n": "80..100"}, "operation_families": ["update", "query"], "coverage_tags": ["update", "query"], "uses_seed": True, "construction": "seeded stream", "total_complexity": "O(output_size log n)"},
    ],
}


class StressFailureNormalizationTests(unittest.TestCase):
    def test_helper_preflight_stderr_reaches_safe_primary_failure(self) -> None:
        exc = HelperPreflightError(
            "generator preflight failed: generator_runtime_error",
            artifact="generator",
            profile="small",
            case_kind="random",
            seed=17,
            stderr="terminate: generator recipe byte bucket is unreachable\n",
        )
        primary = _role_failure_details(
            exc,
            role="generator",
            stage="preflight_helpers",
            substage="preflight",
            elapsed=1.5,
            attempts=2,
        )
        failure = normalize_stress_failure(
            StressRuntimeError(
                exc.code,
                "generator repair exhausted",
                details={"primary_failure": primary},
            ),
            phase="preparation",
            stage="preflight_helpers",
        )

        self.assertEqual(
            failure["primary_failure"]["stderr"],
            "terminate: generator recipe byte bucket is unreachable",
        )

    def test_reference_sample_witness_is_bounded_and_role_isolated(self) -> None:
        exc = HelperPreflightError(
            "reference_primary output disagrees with official sample",
            artifact="reference_primary",
            profile="sample:official-1",
            case_kind="official_sample",
            seed=7,
            code="stress_reference_sample_mismatch",
        )
        exc.details.update(
            {
                "sample_name": "s" * 300,
                "input_excerpt": "i" * 3000,
                "expected_stdout": "expected\n",
                "actual_stdout": "actual\n",
                "reference_secondary": "must-not-leak",
            }
        )

        witness = _preflight_repair_witness(exc)

        self.assertEqual(witness["kind"], "official_sample_mismatch")
        self.assertEqual(len(witness["sample_name"]), 200)
        self.assertEqual(len(witness["input_excerpt"]), 2000)
        self.assertEqual(witness["expected_stdout"], "expected\n")
        self.assertEqual(witness["actual_stdout"], "actual\n")
        self.assertNotIn("secondary", json.dumps(witness))
        failure = normalize_stress_failure(
            exc, phase="preparation", stage="preflight_helpers"
        )
        self.assertEqual(failure["category"], "artifact")
        self.assertEqual(
            failure["root_cause_code"], "stress_reference_sample_mismatch"
        )

    def test_reference_sample_timeout_witness_includes_bounded_input_and_expected(self) -> None:
        exc = HelperPreflightError(
            "reference_primary preflight failed: reference_primary_timeout",
            artifact="reference_primary",
            profile="sample:official-timeout",
            case_kind="official_sample",
            seed=9,
            expected={"returncode": 0, "failure": None},
            actual={"returncode": -9, "failure": "reference_primary_timeout"},
        )
        exc.details.update(
            {
                "sample_name": "official-timeout",
                "input_excerpt": "3 1\n",
                "expected_stdout": "answer\n",
                "reference_secondary": "must-not-leak",
            }
        )

        witness = _preflight_repair_witness(exc)

        self.assertEqual(witness["kind"], "official_sample_execution_failure")
        self.assertEqual(witness["execution_failure"], "reference_primary_timeout")
        self.assertEqual(witness["input_excerpt"], "3 1\n")
        self.assertEqual(witness["expected_stdout"], "answer\n")
        self.assertNotIn("secondary", json.dumps(witness))
        self.assertEqual(
            _preflight_role_repair_limit("reference_primary", exc), 2
        )
        self.assertTrue(
            _preflight_repair_from_scratch(
                "reference_primary", exc, repair_attempt=1
            )
        )

        mismatch = HelperPreflightError(
            "reference_primary output disagrees with official sample",
            artifact="reference_primary",
            profile="sample:official-timeout",
            case_kind="official_sample",
            seed=9,
            code="stress_reference_sample_mismatch",
        )
        self.assertFalse(
            _preflight_repair_from_scratch(
                "reference_primary", mismatch, repair_attempt=2
            )
        )

    def test_source_safety_reference_gets_two_independent_repair_attempts(self) -> None:
        exc = HelperPreflightError(
            "C++ source uses forbidden filesystem API",
            artifact="reference_secondary",
            profile="build",
            case_kind="source_safety",
            seed=0,
        )
        exc.details["source_safety"] = {
            "rule_id": "filesystem_api",
            "matched_token": "freopen",
            "line": 8,
            "column": 3,
            "excerpt": 'freopen("<redacted>", "<redacted>", stdin);',
            "ignored": "must-not-leak",
        }

        self.assertEqual(
            _preflight_role_repair_limit("reference_secondary", exc), 2
        )
        witness = _preflight_repair_witness(exc)
        self.assertEqual(
            witness,
            {
                "kind": "source_safety",
                "rule_id": "filesystem_api",
                "matched_token": "freopen",
                "excerpt": 'freopen("<redacted>", "<redacted>", stdin);',
                "line": 8,
                "column": 3,
            },
        )
        ordinary = HelperPreflightError(
            "runtime failure",
            artifact="reference_secondary",
            profile="small",
            case_kind="random",
            seed=1,
        )
        self.assertEqual(
            _preflight_role_repair_limit("reference_secondary", ordinary), 1
        )

    def test_failure_category_matrix_is_stable(self) -> None:
        cases = (
            (
                StressRuntimeError("invalid_generator_recipe", "invalid recipe"),
                "preparation",
                "prepare_generator",
                "artifact",
                "invalid_generator_recipe",
            ),
            (
                StressRuntimeError("network_error", "provider network failed"),
                "preparation",
                "provider_request",
                "provider",
                "network_error",
            ),
            (
                StressRuntimeError("sandbox_unavailable", "sandbox unavailable"),
                "preparation",
                "check_isolation",
                "environment",
                "sandbox_unavailable",
            ),
            (
                RuntimeError("unexpected controlled-run failure"),
                "execution",
                "controlled_run",
                "internal",
                "stress_internal_error",
            ),
            (
                StressRuntimeError("stress_pre_apply_gate", "gold conflict"),
                "preparation",
                "gold_pre_apply_gate",
                "oracle",
                "stress_pre_apply_gate",
            ),
        )

        for error, phase, stage, category, root_code in cases:
            with self.subTest(category=category, root_code=root_code):
                failure = normalize_stress_failure(
                    error,
                    phase=phase,
                    stage=stage,
                )
                self.assertEqual(failure["category"], category)
                self.assertEqual(failure["root_cause_code"], root_code)
                self.assertEqual(failure["failure_phase"], phase)
                self.assertEqual(failure["failure_stage"], stage)

    def test_unstructured_exception_has_stable_internal_envelope(self) -> None:
        failure = normalize_stress_failure(
            TypeError("Object of type mappingproxy is not JSON serializable"),
            phase="preparation",
            stage="setup",
        )

        self.assertEqual(failure["code"], "stress_internal_error")
        self.assertEqual(failure["root_cause_code"], "stress_internal_error")
        self.assertEqual(failure["category"], "internal")
        self.assertEqual(failure["cause_type"], "TypeError")
        self.assertEqual(failure["failure_phase"], "preparation")
        self.assertEqual(failure["failure_stage"], "setup")
        self.assertIsNone(failure["primary_failure"])

    def test_structured_failure_preserves_outer_code_and_redacts_root(self) -> None:
        error = StressRuntimeError(
            "stress_artifact_stage_failed",
            'helper failed: {"secret":"RAW_TOP_LEVEL"}',
            details={
                "primary_failure": {
                    "role": "generator",
                    "stage": "prepare_generator",
                    "substage": "recipe",
                    "code": "invalid_generator_recipe",
                    "path": "$.cases[1]",
                    "message": 'invalid field {"secret":"RAW_NESTED"}',
                    "stderr": (
                        "generator recipe byte bucket is unreachable at "
                        r"C:\Users\private\generator.cpp"
                    ),
                    "attempts": 1,
                    "secret": "MUST_NOT_LEAK",
                    "details": {"prompt": "MUST_NOT_LEAK"},
                }
            },
        )

        failure = normalize_stress_failure(
            error,
            phase="preparation",
            stage="stale_budget_stage",
        )

        self.assertEqual(failure["code"], "stress_artifact_stage_failed")
        self.assertEqual(failure["root_cause_code"], "invalid_generator_recipe")
        self.assertEqual(failure["category"], "artifact")
        self.assertEqual(failure["failure_stage"], "prepare_generator")
        self.assertNotIn("RAW_TOP_LEVEL", failure["message"])
        serialized = json.dumps(failure, ensure_ascii=False)
        self.assertNotIn("RAW_NESTED", serialized)
        self.assertNotIn("MUST_NOT_LEAK", serialized)
        self.assertEqual(
            set(failure["primary_failure"]),
            {
                "role",
                "stage",
                "substage",
                "code",
                "path",
                "message",
                "stderr",
                "attempts",
            },
        )
        self.assertEqual(
            failure["primary_failure"]["stderr"],
            "generator recipe byte bucket is unreachable at [path omitted]",
        )

    def test_primary_internal_category_and_cause_type_override_name_heuristics(self) -> None:
        error = StressRuntimeError(
            "stress_artifact_stage_failed",
            "generator recipe prompt serialization failed",
            details={
                "primary_failure": {
                    "role": "generator",
                    "stage": "prepare_generator",
                    "substage": "recipe_prompt_serialization",
                    "code": "stress_recipe_prompt_serialization_failed",
                    "category": "internal",
                    "cause_type": "TypeError",
                    "message": "mappingproxy is not JSON serializable",
                }
            },
        )

        failure = normalize_stress_failure(
            error,
            phase="preparation",
            stage="setup",
        )

        self.assertEqual(failure["code"], "stress_artifact_stage_failed")
        self.assertEqual(
            failure["root_cause_code"],
            "stress_recipe_prompt_serialization_failed",
        )
        self.assertEqual(failure["category"], "internal")
        self.assertEqual(failure["cause_type"], "TypeError")
        self.assertEqual(failure["failure_stage"], "prepare_generator")
        self.assertEqual(failure["primary_failure"]["category"], "internal")
        self.assertEqual(failure["primary_failure"]["cause_type"], "TypeError")


def _declared_supported_cases(blueprint) -> list[dict[str, str]]:
    """Capability-handshake mirror of a blueprint's *declared* cases.

    The handshake advertises what the generator actually supports, which is the
    blueprint's ``cases`` list -- not the policy's ``required_cases``.  Since
    ``small/lower_bound`` is optional it is advertised only when a fixture
    declares it, and that advertisement is precisely what
    ``LayeredStressRunner._require_generator_v2`` turns into
    ``_supports_lower_bound``.
    """
    return [
        {"profile": case["profile"], "case_kind": case["case_kind"]}
        for case in blueprint["cases"]
    ]


def _declared_case_kinds(blueprint) -> list[str]:
    """Case kinds implied by the declared cases, in first-seen order.

    Deriving this from the same source as ``supported_cases`` keeps the
    handshake invariant ``("lower_bound" in case_kinds) == (optional case is
    supported)`` true by construction instead of by hand-maintained duplication.
    """
    kinds: list[str] = []
    for case in _declared_supported_cases(blueprint):
        if case["case_kind"] not in kinds:
            kinds.append(case["case_kind"])
    return kinds


PROBE_CONTRACT = {
    "schema_version": 3,
    "input_summary": "one integer n then one operation line",
    "small_profile": "n <= 8",
    "small_lower_boundary": "n = 1",
    "large_profile": "n >= 80 and n <= 100",
    "large_upper_boundary": "n = 100",
    "output_compare": "token",
    "generator_requirements": ["use a fixed seed"],
    "syntax": {
        "mode": "single_case",
        "eof": "required",
        "sections": [
            {
                "id": "header",
                "kind": "scalar",
                "fields": [
                    {"name": "n", "type": "int"},
                    {"name": "m", "type": "int"},
                ],
                "evidence_ids": ["e1"],
            },
            {
                "id": "permutation",
                "kind": "list",
                "count_from": "header.n",
                "fields": [{"name": "values", "type": "int"}],
                "evidence_ids": ["e1"],
            },
            {
                "id": "operations",
                "kind": "operation_stream",
                "count_from": "header.m",
                "fields": [],
                "variants": [
                    {
                        "name": "Insert",
                        "fields": [
                            {"name": "s", "type": "int"},
                            {"name": "t", "type": "int"},
                        ],
                    }
                ],
                "evidence_ids": ["e1"],
            },
        ],
    },
    "constraints": [
        {
            "id": "c_dynamic",
            "kind": "state_precondition",
            "target": "operations",
            "args": {
                "condition": "t in {-1,0,1} and the swap does not cross the current top/bottom boundary",
                "field": "t",
                "operation": "Insert",
            },
            "evidence_ids": ["e1"],
        }
    ],
    "evidence": [{"id": "e1", "quote": "statement", "start": 0, "end": 9}],
    "validator_probes": [
        {
            "id": "vp_dynamic",
            "constraint_id": "c_dynamic",
            "valid_input": "3 1\n1 2 3\nInsert 2 1\n",
            "invalid_input": "3 1\n1 2 3\nInsert 2 -1\n",
            "evidence_ids": ["e1"],
        }
    ],
}


class GeneratorRepairDiagnosticTests(unittest.TestCase):
    def test_contract_probe_validator_repair_diagnostic_never_leaks_probe(self) -> None:
        secret_valid = "3 2\n1 2 3\nTop 3\nInsert 3 -1\n"
        exc = HelperPreflightError(
            "hidden probe rejected",
            artifact="validator",
            profile="contract_probe",
            case_kind="vp3:valid",
            seed=17,
            stderr="ERR_INSERT_TOP",
            code="stress_validator_positive_probe_failed",
            expected={"constraint_id": "c7", "probe_id": "vp3"},
            actual={
                "constraint_id": "c7",
                "valid_accepted": False,
                "invalid_accepted": True,
                "generated_input_excerpt": secret_valid,
                "valid_input_sha256": "a" * 64,
                "probe_id": "vp3",
            },
        )

        diagnostic = _contract_probe_repair_diagnostic(exc, repair_attempt=1)
        serialized = json.dumps(diagnostic, ensure_ascii=False)

        self.assertEqual(diagnostic["expected"], {"constraint_id": "c7"})
        self.assertEqual(diagnostic["case_kind"], "hidden_contract_probe")
        self.assertNotIn(secret_valid, serialized)
        self.assertNotIn("vp3", serialized)
        self.assertNotIn("ERR_INSERT_TOP", serialized)
        self.assertNotIn("a" * 64, serialized)

    def test_safe_seed_families_come_only_from_large_random_blueprint(self) -> None:
        blueprint = dict(BLUEPRINT)
        blueprint["cases"] = [dict(item) for item in BLUEPRINT["cases"]]
        blueprint["cases"][1]["operation_families"] = ["unsafe_mutation"]
        blueprint["cases"][3]["operation_families"] = ["read", "lookup", "read"]

        self.assertEqual(
            _generator_safe_seed_families(blueprint),
            ("read", "lookup"),
        )

    def test_seed_obligation_survives_a_later_unrelated_machine_failure(self) -> None:
        requirement = _persistent_generator_seed_requirement(
            [
                {
                    "code": "stress_generator_seed_variation_failed",
                    "seed": 17,
                },
                {"code": "stress_generator_runtime_failed", "seed": 18},
            ],
            BLUEPRINT,
        )

        self.assertIn("不得只修最新错误", requirement)
        self.assertIn("update, query", requirement)
        self.assertIn("至少两种合法 stdout", requirement)

    def test_no_seed_obligation_is_invented_without_machine_evidence(self) -> None:
        self.assertEqual(
            _persistent_generator_seed_requirement(
                [{"code": "stress_generated_input_invalid"}], BLUEPRINT
            ),
            "",
        )

    def test_every_generator_repair_keeps_blueprint_seed_invariant(self) -> None:
        invariants = _generator_repair_invariants(BLUEPRINT)

        self.assertTrue(any("连续 seed" in item for item in invariants))
        self.assertTrue(any("update, query" in item for item in invariants))
        self.assertTrue(any("coverage_tags" in item for item in invariants))
        self.assertTrue(any("已知错误的实际数量" in item for item in invariants))
        self.assertTrue(any("候选策略" in item for item in invariants))

    def test_record_count_hint_reports_exact_declared_vs_emitted_mismatch(self) -> None:
        contract = {
            "syntax": {
                "sections": [
                    {
                        "id": "header",
                        "kind": "scalar",
                        "fields": [
                            {"name": "n", "type": "int"},
                            {"name": "m", "type": "int"},
                        ],
                    },
                    {
                        "id": "operations",
                        "kind": "operation_stream",
                        "count_from": "header.m",
                        "variants": [
                            {"tag": tag, "fields": []}
                            for tag in ("Top", "Bottom", "Insert", "Ask", "Query")
                        ],
                    },
                ]
            }
        }
        generated = (
            "5 8\n1 2 3 4 5\nTop 3\nBottom 1\nInsert 2 -1\nInsert 4 1\n"
            "Insert 5 0\nAsk 5\nAsk 5\nQuery 5\nTop 5\n"
        )
        hint = _generator_record_count_hint(
            contract,
            {
                "actual": {
                    "generated_input_excerpt": generated,
                    "generated_input_truncated": False,
                }
            },
        )
        self.assertEqual(hint["declared_records"], 8)
        self.assertEqual(hint["observed_tagged_records"], 9)
        self.assertEqual(hint["count_from"], "header.m")

    def test_local_gate_transient_is_confirmed_without_source_repair(self) -> None:
        calls = 0

        def run():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HelperPreflightError(
                    "transient coverage result",
                    artifact="generator",
                    profile="small",
                    case_kind="random",
                    seed=17,
                    code="stress_generator_coverage_failed",
                )
            return {"compiled": True}

        confirmations: set[tuple[str, str, str, str, str, int]] = set()
        result = _run_locally_confirmed_gate(
            run,
            gate_name="machine",
            source_identity="source-a",
            confirmations=confirmations,
        )

        self.assertEqual(calls, 2)
        self.assertTrue(result["local_confirmation"]["recovered_transient"])
        self.assertFalse(result["local_confirmation"]["reproduced"])
        self.assertEqual(len(confirmations), 1)

    def test_local_gate_repair_diagnostic_requires_reproduced_failure(self) -> None:
        calls = 0

        def run():
            nonlocal calls
            calls += 1
            raise HelperPreflightError(
                f"confirmed failure {calls}",
                artifact="generator",
                profile="small",
                case_kind="random",
                seed=23,
                code="stress_generator_seed_variation_failed",
            )

        with self.assertRaises(HelperPreflightError) as caught:
            _run_locally_confirmed_gate(
                run,
                gate_name="machine",
                source_identity="source-b",
                confirmations=set(),
            )

        self.assertEqual(calls, 2)
        self.assertTrue(
            caught.exception.details["local_confirmation"]["reproduced"]
        )


class StressCoordinatorShutdownTests(unittest.TestCase):
    def test_shutdown_waits_for_worker_cleanup_after_requesting_stop(self) -> None:
        class BlockingRunner:
            def __init__(self) -> None:
                self.stop_requested = threading.Event()

            def request_stop(self) -> None:
                self.stop_requested.set()

        with tempfile.TemporaryDirectory() as temp:
            paths = Paths.for_root(Path(temp))
            paths.ensure()
            with Database(paths.database):
                pass
            coordinator = StressCoordinator(paths, sandbox_factory=FakeSandbox)
            runner = BlockingRunner()
            cleanup_finished = threading.Event()

            def worker() -> None:
                runner.stop_requested.wait(timeout=1.0)
                time.sleep(0.02)
                cleanup_finished.set()

            thread = threading.Thread(target=worker, daemon=True)
            with coordinator._lock:
                coordinator._runners["run-cleanup"] = runner
                coordinator._threads["run-cleanup"] = thread
            thread.start()

            live_workers = coordinator.shutdown()

            self.assertTrue(runner.stop_requested.is_set())
            self.assertTrue(cleanup_finished.is_set())
            self.assertFalse(thread.is_alive())
            self.assertEqual(live_workers, ())

    def test_shutdown_reports_worker_that_did_not_exit(self) -> None:
        class BlockingRunner:
            def request_stop(self) -> None:
                pass

        class StuckThread:
            def __init__(self) -> None:
                self.join_calls: list[float | None] = []

            def join(self, timeout=None) -> None:
                self.join_calls.append(timeout)

            def is_alive(self) -> bool:
                return True

        with tempfile.TemporaryDirectory() as temp:
            paths = Paths.for_root(Path(temp))
            paths.ensure()
            with Database(paths.database):
                pass
            coordinator = StressCoordinator(paths, sandbox_factory=FakeSandbox)
            stuck = StuckThread()
            with coordinator._lock:
                coordinator._runners["run-stuck"] = BlockingRunner()
                coordinator._threads["run-stuck"] = stuck

            live_workers = coordinator.shutdown()

            self.assertEqual(live_workers, ("run-stuck",))
            self.assertEqual(len(stuck.join_calls), 1)


class FakeClient:
    def __init__(self):
        self.generated = 0
        self.prompts = []

    def chat_json(self, messages, **kwargs):
        self.prompts.append(messages)
        self.generated += 1
        request = json.loads(messages[-1]["content"])
        if request.get("type") == "acm_stress_contract":
            return Result(
                {
                    "input_summary": "one integer",
                    "small_profile": "n <= 8",
                    "small_lower_boundary": "n = 1",
                    "large_profile": "n >= 80 and n <= 100",
                    "large_upper_boundary": "n = 100",
                    "output_compare": "token",
                    "generator_requirements": [],
                }
            )
        if request.get("type") == "acm_stress_generator_blueprint_v1":
            return Result(dict(BLUEPRINT))
        if request.get("type") == "ai_stress_artifact_static_audit":
            return Result(
                {
                    "verdict": "accept",
                    "confidence": 0.99,
                    "issues": [],
                    "summary": "fixture accepted",
                }
            )
        if request.get("type") == "acm_stress_validator":
            return Result({"code": VALIDATOR, "notes": "validator"})
        if request.get("type") == "acm_stress_generator" or (
            request.get("type") == "acm_stress_artifact_repair"
            and request.get("artifact_kind") == "generator"
        ):
            return Result({"code": GENERATOR, "notes": "generator"})
        if request.get("artifact_kind") == "reference_secondary":
            return Result({"code": SAFE_SECONDARY, "notes": "generated secondary"})
        return Result({"code": SAFE, "notes": "generated"})

    def chat_with_tools(self, messages, *, tool_handler, **kwargs):
        self.prompts.append(messages)
        request = json.loads(messages[-1]["content"])
        tool_handler(
            "search_source",
            {
                "tier": request["tier"],
                "problem_id": request["problem_id"],
                "title": request["title"],
            },
        )
        return Result(content='{"selected_candidate_id":null}')


class EmptyCrawler:
    def search(self, tier, *, problem_id, title):
        return []


class ConcurrentAuditClient:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def chat_json(self, messages, **kwargs):
        request = json.loads(messages[-1]["content"])
        self.assert_request = request
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        return Result(
            {
                "verdict": "accept",
                "confidence": 0.99,
                "issues": [],
                "summary": "parallel fixture accepted",
            }
        )


class RejectThenRepairClient:
    def __init__(self) -> None:
        self.audit_calls = 0
        self.repair_payload = None

    def chat_json(self, messages, **kwargs):
        request = json.loads(messages[-1]["content"])
        if request.get("type") == "ai_stress_artifact_static_audit_verification":
            audited_code = str(
                request.get("audit_context", {}).get("artifact_code") or ""
            )
            return Result(
                {
                    "verdict": "confirm",
                    "confidence": 0.99,
                    "witness": {
                        "code_expression": (
                            "acm_generate_case"
                            if "acm_generate_case" in audited_code
                            else "int main"
                        ),
                        "input_excerpt": "1",
                        "trace": "fixture concrete trace exceeds twenty chars",
                        "seed": 1,
                    },
                    "reason": "fixture",
                }
            )
        if request.get("type") == "ai_stress_artifact_static_audit":
            self.audit_calls += 1
            if self.audit_calls == 1:
                audited_code = str(request.get("artifact_code") or "")
                return Result(
                    {
                        "verdict": "reject",
                        "confidence": 0.99,
                        "issues": [
                            {
                                "category": "bounds",
                                "severity": "critical",
                                "evidence": "old generator marker is broken",
                            }
                        ],
                        "summary": "定点修复 generator",
                        "witness": {
                            "code_expression": (
                                "acm_generate_case"
                                if "acm_generate_case" in audited_code
                                else "int main"
                            ),
                            "input_excerpt": "1",
                            "trace": "fixture concrete trace exceeds twenty chars",
                            "seed": 1,
                            "failure_confirmed": True,
                        },
                    }
                )
            return Result(
                {
                    "verdict": "accept",
                    "confidence": 0.99,
                    "issues": [],
                    "summary": "accepted after repair",
                }
            )
        self.repair_payload = request
        return Result({"code": GENERATOR, "notes": "repaired"})


class RejectTwiceThenRepairClient:
    def __init__(self) -> None:
        self.audit_calls = 0
        self.repair_calls = 0
        self.repair_kwargs = []

    def chat_json(self, messages, **kwargs):
        request = json.loads(messages[-1]["content"])
        if request.get("type") == "ai_stress_artifact_static_audit_verification":
            audited_code = str(
                request.get("audit_context", {}).get("artifact_code") or ""
            )
            return Result(
                {
                    "verdict": "confirm",
                    "confidence": 0.99,
                    "witness": {
                        "code_expression": (
                            "acm_generate_case"
                            if "acm_generate_case" in audited_code
                            else "int main"
                        ),
                        "input_excerpt": "1",
                        "trace": "fixture concrete trace exceeds twenty chars",
                        "seed": 1,
                    },
                    "reason": "fixture",
                }
            )
        if request.get("type") == "ai_stress_artifact_static_audit":
            self.audit_calls += 1
            if self.audit_calls <= 2:
                audited_code = str(request.get("artifact_code") or "")
                return Result(
                    {
                        "verdict": "reject",
                        "confidence": 0.99,
                        "fault_origin": "implementation",
                        "issues": [{"category": "logic", "severity": "critical", "evidence": f"bad version {self.audit_calls}"}],
                        "summary": "repair generator again",
                        "witness": {
                            "code_expression": (
                                "acm_generate_case"
                                if "acm_generate_case" in audited_code
                                else "int main"
                            ),
                            "input_excerpt": "1",
                            "trace": "fixture concrete trace exceeds twenty chars",
                            "seed": 1,
                            "failure_confirmed": True,
                        },
                    }
                )
            return Result(
                {"verdict": "accept", "confidence": 0.99, "fault_origin": "implementation", "issues": [], "summary": "accepted"}
            )
        self.repair_calls += 1
        self.repair_kwargs.append(kwargs)
        return Result({"code": GENERATOR + f"// repair {self.repair_calls}\n", "notes": "repaired"})


class RoleFailureClient:
    def chat_json(self, messages, **kwargs):
        request = json.loads(messages[-1]["content"])
        role = request.get("artifact_kind")
        time.sleep({"generator": 0.05, "reference_primary": 0.01, "reference_secondary": 0.08}[role])
        if role == "reference_primary":
            raise OSError("WinError 10054")
        return Result(
            {
                "verdict": "accept",
                "confidence": 0.99,
                "issues": [],
                "summary": "accepted",
            }
        )


class FakeSandbox:
    calls = []
    valid_capability = True

    def __init__(self, *, available=True):
        self.available = available
        self.cancelled = False

    def probe(self):
        return SandboxCapability(self.available, "missing" if not self.available else "", "fake")

    def run(self, command, *, cwd, input_data=None, env=None, limits=None):
        type(self).calls.append((list(command), dict(env or {}), limits))
        role = Path(command[0]).stem
        if "validator" in role:
            profile = str((env or {}).get("ACM_STRESS_PROFILE") or "small")
            case_kind = str((env or {}).get("ACM_STRESS_CASE_KIND") or "random")
            tags = (
                ["update", "query", "boundary"]
                if profile == "small" and case_kind == "random"
                else ["boundary"]
                if profile in {"small", "sample"}
                else ["update", "query"]
            )
            payload = {
                "valid": True,
                "dimensions": {"n": 1 if profile in {"small", "sample"} else 100},
                "coverage_tags": tags,
                "records": 1,
            }
            return SandboxProcessResult(
                list(command), 0, json.dumps(payload).encode("utf-8")
            )
        if "generator" in role:
            if "--capabilities" in command:
                # Advertise the *declared* cases, not ``required_cases``:
                # ``small/lower_bound`` is no longer required but this fixture
                # still declares it, so this sandbox exercises the
                # "declared -> still runs" path end to end.
                payload = (
                    {
                        "profile_version": 2,
                        "manifest_version": 1,
                        "profiles": ["small", "large"],
                        "case_kinds": _declared_case_kinds(BLUEPRINT),
                        "supported_cases": _declared_supported_cases(BLUEPRINT),
                    }
                    if type(self).valid_capability
                    else {"profile_version": 1}
                )
                return SandboxProcessResult(
                    list(command), 0, json.dumps(payload).encode("utf-8")
                )
            if len(command) == 5 and command[1] == "--manifest":
                seed, profile, case_kind = command[2], command[3], command[4]
                generated = f"{profile}:{case_kind}:{seed}\n".encode()
                tags = (
                    ["update", "query", "boundary"]
                    if profile == "small" and case_kind == "random"
                    else ["boundary"]
                    if profile == "small"
                    else ["update", "query"]
                )
                payload = {
                    "manifest_version": 1,
                    "profile": profile,
                    "case_kind": case_kind,
                    "seed": int(seed),
                    "input_sha256": hashlib.sha256(generated).hexdigest(),
                    "dimensions": {"n": 1 if profile == "small" else 100},
                    "coverage_tags": tags,
                    "records": 1,
                    "total_complexity": (
                        "output_log_n"
                        if profile == "large" and case_kind == "random"
                        else "linear_output"
                    ),
                }
                return SandboxProcessResult(
                    list(command), 0, json.dumps(payload).encode("utf-8")
                )
            environment = dict(env or {})
            generated = (
                f"{environment.get('ACM_STRESS_PROFILE', 'small')}:"
                f"{environment.get('ACM_STRESS_CASE_KIND', 'random')}:"
                f"{environment.get('ACM_STRESS_SEED', '0')}\n"
            ).encode()
            return SandboxProcessResult(list(command), 0, generated)
        if "solution" in role:
            return SandboxProcessResult(list(command), 0, b"wrong\n")
        return SandboxProcessResult(list(command), 0, b"correct\n")

    def cancel(self):
        self.cancelled = True


PROBE_BLUEPRINT = {
    "schema_version": 1,
    # Same policy as BLUEPRINT: three core required pairs only.  The declared
    # ``cases`` list below still carries ``small/lower_bound`` because that case
    # remains optional-but-valid to declare.
    "required_cases": [dict(pair) for pair in _GENERATOR_REQUIRED_CASES],
    "dimensions": [{"name": "n"}, {"name": "m"}],
    "operation_families": ["Insert"],
    "required_coverage_tags": [],
    "large_required_coverage_tags": [],
    "cases": [
        {"profile": "small", "case_kind": "lower_bound", "dimensions": {"n": 1, "m": 0}, "operation_families": [], "coverage_tags": [], "uses_seed": False, "construction": "minimum", "total_complexity": "O(output_size)"},
        {"profile": "small", "case_kind": "random", "dimensions": {"n": 3, "m": 1}, "operation_families": ["Insert"], "coverage_tags": [], "uses_seed": True, "construction": "seeded", "total_complexity": "O(output_size)"},
        {"profile": "large", "case_kind": "upper_bound", "dimensions": {"n": 100, "m": 100}, "operation_families": ["Insert"], "coverage_tags": [], "uses_seed": False, "construction": "upper", "total_complexity": "O(output_size)"},
        {"profile": "large", "case_kind": "random", "dimensions": {"n": 80, "m": 80}, "operation_families": ["Insert"], "coverage_tags": [], "uses_seed": True, "construction": "seeded", "total_complexity": "O(output_size log n)"},
    ],
}


class ProbeContractClient(FakeClient):
    """FakeClient whose contract carries an independently certified probe."""

    def chat_json(self, messages, **kwargs):
        request = json.loads(messages[-1]["content"])
        if request.get("type") == "acm_stress_contract":
            self.prompts.append(messages)
            self.generated += 1
            return Result(dict(PROBE_CONTRACT))
        if request.get("type") == "acm_stress_generator_blueprint_v1":
            self.prompts.append(messages)
            self.generated += 1
            return Result(dict(PROBE_BLUEPRINT))
        if request.get("type") == "acm_stress_validator_probe_certification_v1":
            self.prompts.append(messages)
            self.generated += 1
            return Result(
                {"validator_probes": list(PROBE_CONTRACT["validator_probes"])}
            )
        return super().chat_json(messages, **kwargs)


class ProbeFailingSandbox(FakeSandbox):
    """Validator rejects every hidden contract-probe input."""

    def run(self, command, *, cwd, input_data=None, env=None, limits=None):
        if (
            "validator" in Path(command[0]).stem
            and (env or {}).get("ACM_STRESS_PROFILE") == "contract_probe"
        ):
            payload = {
                "valid": False,
                "dimensions": {},
                "coverage_tags": [],
                "records": 0,
            }
            return SandboxProcessResult(
                list(command), 0, json.dumps(payload).encode("utf-8")
            )
        return super().run(
            command, cwd=cwd, input_data=input_data, env=env, limits=limits
        )


@unittest.skipUnless(shutil.which("g++"), "g++ is required")


class DegradedCoverageSandbox(FakeSandbox):
    """Generator claims the operation-family tag without an independent validator.

    In degraded mode the small/random coverage obligation is satisfied from the
    generator's own manifest instead of a certified validator observation, so
    the fake generator must claim ``auto_operation_insert`` on that profile.
    """

    def run(self, command, *, cwd, input_data=None, env=None, limits=None):
        role = Path(command[0]).stem
        if (
            "generator" in role
            and len(command) == 5
            and command[1] == "--manifest"
            and command[3] == "small"
            and command[4] == "random"
        ):
            seed = int(command[2])
            generated = f"small:random:{seed}\n".encode()
            payload = {
                "manifest_version": 1,
                "profile": "small",
                "case_kind": "random",
                "seed": seed,
                "input_sha256": hashlib.sha256(generated).hexdigest(),
                "dimensions": {"n": 3, "m": 1},
                "coverage_tags": [
                    "update",
                    "query",
                    "boundary",
                    "auto_operation_insert",
                ],
                "records": 1,
                "total_complexity": "linear_output",
            }
            return SandboxProcessResult(
                list(command), 0, json.dumps(payload).encode("utf-8")
            )
        return super().run(
            command, cwd=cwd, input_data=input_data, env=env, limits=limits
        )


class ManifestCrashingSandbox(FakeSandbox):
    """Generator crashes only on the preflight ``--manifest`` subcommand.

    The plain generation path works for every profile/case_kind, exactly like
    the real P2596 generators that failed ``stress_generator_coverage_failed``
    in the paid degraded batch.  The minimal-verification mode must still pass
    preflight and launch a run; the full mode must keep rejecting it.
    """

    def run(self, command, *, cwd, input_data=None, env=None, limits=None):
        role = Path(command[0]).stem
        if (
            "generator" in role
            and len(command) == 5
            and command[1] == "--manifest"
        ):
            return SandboxProcessResult(
                list(command), 64, b"", b"manifest query failed"
            )
        return super().run(
            command, cwd=cwd, input_data=input_data, env=env, limits=limits
        )


class ProbeAcceptingSandbox(FakeSandbox):
    """Validator accepts valid probes, rejects invalid ones, and covers tags.

    The structured probe contract declares an ``Insert`` operation variant, so
    the machine gates derive an ``auto_operation_insert`` coverage obligation
    that the fake validator observation must satisfy on small/random cases.
    """

    def run(self, command, *, cwd, input_data=None, env=None, limits=None):
        role = Path(command[0]).stem
        profile = str((env or {}).get("ACM_STRESS_PROFILE") or "")
        case_kind = str((env or {}).get("ACM_STRESS_CASE_KIND") or "")
        if (
            "validator" in role
            and profile == "contract_probe"
        ):
            valid = case_kind.endswith(":valid")
            payload = {
                "valid": valid,
                "dimensions": {} if not valid else {"n": 1},
                "coverage_tags": [],
                "records": 0,
            }
            return SandboxProcessResult(
                list(command), 0, json.dumps(payload).encode("utf-8")
            )
        result = super().run(
            command, cwd=cwd, input_data=input_data, env=env, limits=limits
        )
        if (
            "validator" in role
            and profile == "small"
            and case_kind == "random"
        ):
            try:
                payload = json.loads(result.stdout.decode("utf-8"))
            except (UnicodeError, ValueError, json.JSONDecodeError):
                return result
            if isinstance(payload, dict):
                tags = payload.get("coverage_tags")
                if isinstance(tags, list):
                    payload["coverage_tags"] = sorted(
                        set(tags) | {"auto_operation_insert"}
                    )
                    return SandboxProcessResult(
                        list(command), 0, json.dumps(payload).encode("utf-8")
                    )
        return result


class StressRuntimeTests(unittest.TestCase):
    def test_external_reference_alternate_preserves_remaining_pool(self) -> None:
        artifact = GeneratedArtifact(
            "reference",
            SAFE,
            "cnblogs",
            "",
            source_alternates=(
                {
                    "tier": "luogu_solutions",
                    "code": SAFE + "// second\n",
                    "url": "https://www.luogu.com.cn/article/second",
                    "title": "second",
                    "content_sha256": "b" * 64,
                    "license": "unknown",
                },
                {
                    "tier": "csdn",
                    "code": SAFE + "// third\n",
                    "url": "https://blog.csdn.net/x/article/details/3",
                    "title": "third",
                    "content_sha256": "c" * 64,
                    "license": "unknown",
                },
            ),
        )
        selected = _next_external_reference(artifact)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.origin, "luogu_solution")
        self.assertIn("second", selected.code)
        self.assertEqual(len(selected.source_alternates), 1)
        self.assertEqual(
            _next_external_reference(selected).origin, "csdn"  # type: ignore[union-attr]
        )

    def setUp(self) -> None:
        FakeSandbox.calls = []
        FakeSandbox.valid_capability = True

    def _workspace(self, root: Path):
        paths = Paths.for_root(root)
        paths.ensure()
        with Database(paths.database):
            pass
        source = root / "2026" / "8" / "5" / "CF1A.cpp"
        source.parent.mkdir(parents=True)
        source.write_text("#define USER_PRIVATE_MARKER 1\n" + SAFE, encoding="utf-8")
        return paths, source

    def test_generated_artifact_audits_share_deadline_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = Paths.for_root(Path(temp))
            client = ConcurrentAuditClient()
            coordinator = StressCoordinator(paths, sandbox_factory=FakeSandbox)
            def artifact(role):
                return GeneratedArtifact(role, SAFE, "ai_generated", "fixture")
            preparation = StressPreparation(
                {"input_summary": "n", "small_profile": "n<=8"},
                artifact("generator"),
                artifact("brute"),
                artifact("reference"),
                {},
            )
            audited, reports, _counts = coordinator._audit_and_repair_generated_artifacts(
                client,
                preparation,
                problem_id="P2596",
                statement="statement",
                model_settings={"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "high"},
                progress_callback=None,
            )
            self.assertEqual(client.max_active, 3)
            self.assertEqual(
                set(reports),
                {"generator", "reference_primary", "reference_secondary"},
            )
            self.assertTrue(audited.generator.static_audit["accepted"])

    def test_repair_prompt_contains_only_rejected_old_code_and_one_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = Paths.for_root(Path(temp))
            client = RejectThenRepairClient()
            coordinator = StressCoordinator(paths, sandbox_factory=FakeSandbox)
            generator = GeneratedArtifact(
                "generator",
                SAFE.replace("return 0", "return 7") + "// OLD_GENERATOR_MARKER\n",
                "ai_generated",
                "",
            )
            preparation = StressPreparation(
                {"input_summary": "n", "output_compare": "token"},
                generator,
                GeneratedArtifact("brute", SAFE + "// SIBLING_BRUTE\n", "local_existing", ""),
                GeneratedArtifact(
                    "reference", SAFE + "// SIBLING_REFERENCE\n", "luogu_solution", ""
                ),
                {},
            )
            audited, reports, counts = coordinator._audit_and_repair_generated_artifacts(
                client,
                preparation,
                problem_id="P1",
                statement="public statement only",
                model_settings={
                    "model": "deepseek-v4-flash",
                    "reasoning_effort": "high",
                },
                progress_callback=None,
            )
            self.assertEqual(counts["generator"], 1)
            self.assertEqual(client.audit_calls, 2)
            self.assertTrue(reports["generator"]["accepted"])
            self.assertTrue(audited.generator.static_audit["accepted"])
            serialized = json.dumps(client.repair_payload, ensure_ascii=False)
            self.assertIn("OLD_GENERATOR_MARKER", serialized)
            self.assertNotIn("SIBLING_BRUTE", serialized)
            self.assertNotIn("SIBLING_REFERENCE", serialized)
            self.assertEqual(
                client.repair_payload["type"], "acm_stress_artifact_repair"
            )
            self.assertIsInstance(
                client.repair_payload["structured_diagnostic"], dict
            )
            self.assertEqual(
                client.repair_payload["structured_diagnostic"]["witness"]["seed"],
                1,
            )

    def test_exhausted_audit_preserves_usage_and_structured_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            coordinator = StressCoordinator(
                Paths.for_root(Path(temp)), sandbox_factory=FakeSandbox
            )
            artifact = GeneratedArtifact("generator", SAFE, "ai_generated", "")
            with self.assertRaises(StressRuntimeError) as caught:
                coordinator._audit_and_repair_generated_artifacts(
                    RejectThenRepairClient(),
                    StressPreparation(
                        {"input_summary": "n", "small_profile": "n<=8"},
                        artifact,
                        None,
                        None,
                        {"total_tokens": 7},
                        generator_blueprint=dict(BLUEPRINT),
                    ),
                    problem_id="P2596",
                    statement="statement",
                    model_settings={"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "high"},
                    progress_callback=None,
                    repair_counts={"generator": 2, "brute": 0, "reference": 0},
                )
            self.assertEqual(caught.exception.code, "stress_artifact_audit_failed")
            self.assertEqual(caught.exception.usage["total_tokens"], 8)
            self.assertEqual(caught.exception.details["attempts"], 2)
            self.assertEqual(
                caught.exception.details["audit"]["issues"][0]["category"],
                "bounds",
            )

    def test_generator_gets_fast_then_bounded_thinking_hybrid_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = Paths.for_root(Path(temp))
            client = RejectTwiceThenRepairClient()
            coordinator = StressCoordinator(paths, sandbox_factory=FakeSandbox)
            preparation = StressPreparation(
                {"input_summary": "n", "output_compare": "token"},
                GeneratedArtifact("generator", SAFE, "ai_generated", ""),
                GeneratedArtifact("brute", SAFE, "local_existing", ""),
                GeneratedArtifact("reference", SAFE, "local_existing", ""),
                {},
                BLUEPRINT,
            )
            audited, reports, counts = coordinator._audit_and_repair_generated_artifacts(
                client,
                preparation,
                problem_id="P2596",
                statement="statement",
                model_settings={"model": "deepseek-v4-flash", "reasoning_effort": "high"},
                generation_mode="hybrid",
                progress_callback=None,
            )
            self.assertEqual(counts["generator"], 2)
            self.assertEqual(client.audit_calls, 3)
            self.assertEqual(client.repair_calls, 2)
            self.assertEqual(
                [item["thinking"] for item in client.repair_kwargs],
                [False, True],
            )
            self.assertEqual(client.repair_kwargs[0]["max_tokens"], 8192)
            self.assertEqual(client.repair_kwargs[1]["max_tokens"], 4096)
            self.assertTrue(reports["generator"]["accepted"])
            self.assertTrue(audited.generator.static_audit["accepted"])

    def test_parallel_audit_failure_is_attributed_to_completed_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = Paths.for_root(Path(temp))
            coordinator = StressCoordinator(paths, sandbox_factory=FakeSandbox)
            def artifact(role):
                return GeneratedArtifact(role, SAFE, "ai_generated", "")
            with self.assertRaises(StressRuntimeError) as caught:
                coordinator._audit_and_repair_generated_artifacts(
                    RoleFailureClient(),
                    StressPreparation(
                        {"input_summary": "n"},
                        artifact("generator"),
                        artifact("reference_primary"),
                        artifact("reference_secondary"),
                        {},
                    ),
                    problem_id="P1",
                    statement="statement",
                    model_settings={
                        "model": "deepseek-v4-flash",
                        "reasoning_effort": "high",
                    },
                    progress_callback=None,
                )
            self.assertEqual(caught.exception.code, "stress_artifact_stage_failed")
            self.assertIn("reference_primary", caught.exception.details["roles"])
            self.assertEqual(
                caught.exception.details["roles"]["reference_primary"]["role"],
                "reference_primary",
            )
            self.assertIn(
                "10054",
                caught.exception.details["roles"]["reference_primary"]["message"],
            )

    def test_start_persists_bundle_and_stops_on_confirmed_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            client = FakeClient()
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            progress = []
            started = coordinator.start(
                client=client,
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={
                    "model": "deepseek-v4-flash",
                    "thinking": True,
                    "reasoning_effort": "high",
                },
                seed=42,
                progress_callback=lambda stage, label, step, total: progress.append(
                    (stage, label, step, total)
                ),
            )
            run_id = started["run"]["id"]
            deadline = time.time() + 5
            while time.time() < deadline:
                current = coordinator.run(run_id)
                if current["status"] not in {"pending", "preparing", "running", "stop_requested"}:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "mismatch")
            self.assertEqual(current["next_seed"], 43)
            self.assertEqual(current["profile_version"], 2)
            self.assertEqual(current["small_count"], 1)
            self.assertEqual(current["large_count"], 0)
            self.assertTrue(Path(current["failure_path"]).is_dir())
            self.assertEqual(
                {item["kind"] for item in started["bundle"]["artifacts"]},
                {
                    "generator",
                    "validator",
                    "reference_primary",
                    "reference_secondary",
                },
            )
            self.assertTrue(current["validator_requested"])
            self.assertTrue(current["validator_certified"])
            generator_validation = next(
                item["validation"]
                for item in started["bundle"]["artifacts"]
                if item["kind"] == "generator"
            )
            self.assertTrue(generator_validation["ai_audit"]["accepted"])
            self.assertEqual(
                generator_validation["preflight"]["small_random_cases"], 16
            )
            self.assertTrue(
                generator_validation["preflight"]["deterministic_generator"]
            )
            self.assertTrue(source.with_name("CF1A.ref1.cpp").is_file())
            self.assertTrue(source.with_name("CF1A.ref2.cpp").is_file())
            self.assertNotIn("USER_PRIVATE_MARKER", json.dumps(client.prompts))
            stage_transitions = []
            for item in progress:
                if not stage_transitions or stage_transitions[-1][0] != item[0]:
                    stage_transitions.append(item)
            self.assertEqual(
                [item[0] for item in stage_transitions],
                [
                    "check_isolation",
                    "extract_contract",
                    "generate_generator",
                    "generate_validator",
                    "prepare_reference_primary",
                    "prepare_reference_secondary",
                    "audit_helpers",
                    "preflight_helpers",
                    "apply_helpers",
                    "create_stress_run",
                ],
            )
            self.assertEqual([item[2] for item in stage_transitions], list(range(1, 11)))
            self.assertTrue(all(item[3] == 10 for item in progress))
            run_root = paths.state_dir / "stress-runs"
            self.assertEqual(list(run_root.iterdir()) if run_root.is_dir() else [], [])

            generated_before_resume = client.generated
            previous_source_hash = current["user_source_hash"]
            source.write_text("#define USER_PRIVATE_MARKER 2\n" + SAFE, encoding="utf-8")
            resumed = coordinator.resume(run_id)
            self.assertIn(
                resumed["run"]["status"],
                {"pending", "running", "mismatch"},
            )
            deadline = time.time() + 5
            while time.time() < deadline:
                current = coordinator.run(run_id)
                if current["status"] not in {
                    "pending",
                    "preparing",
                    "running",
                    "stop_requested",
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "mismatch")
            self.assertEqual(current["next_seed"], 44)
            self.assertEqual(current["total_count"], 2)
            self.assertEqual(current["small_count"], 1)
            self.assertEqual(current["large_count"], 1)
            self.assertEqual(current["config"]["rate_base_total"], 1)
            self.assertNotEqual(current["user_source_hash"], previous_source_hash)
            self.assertEqual(client.generated, generated_before_resume)
            generated_cases = [
                command[1:]
                for command, _env, _limits in FakeSandbox.calls
                if Path(command[0]).stem.endswith("generator")
                and "--capabilities" not in command
                and "stress-runs" in Path(command[0]).parts
            ]
            # Declared-path coverage.  ``small/lower_bound`` is no longer in
            # ``required_cases``, but BLUEPRINT's ``cases`` still declares it, so
            # the capability handshake advertises it and the runner must schedule
            # it at index 0 with ``large/upper_bound`` right behind.  Declaration,
            # not requirement, is what keeps this case running.
            self.assertIn(
                _GENERATOR_OPTIONAL_CASE, _declared_supported_cases(BLUEPRINT)
            )
            self.assertIn(_GENERATOR_OPTIONAL_CASE, BLUEPRINT["required_cases"])
            optional_argv = [
                _GENERATOR_OPTIONAL_CASE["profile"],
                _GENERATOR_OPTIONAL_CASE["case_kind"],
            ]
            self.assertEqual(
                generated_cases,
                [["42", *optional_argv], ["43", "large", "upper_bound"]],
            )
            reference_calls = [
                command
                for command, _env, _limits in FakeSandbox.calls
                if any(
                    Path(command[0]).stem.endswith(role)
                    for role in ("reference_primary", "reference_secondary")
                )
                and "stress-runs" in Path(command[0]).parts
            ]
            self.assertEqual(len(reference_calls), 4)

    def test_identical_second_setup_reuses_validated_bundle_without_provider_or_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            first_client = FakeClient()
            first = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            initial = first.start(
                client=first_client,
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={
                    "model": "deepseek-v4-flash",
                    "thinking": True,
                    "reasoning_effort": "high",
                },
                seed=101,
            )
            self.assertTrue(initial["run"]["validator_requested"])
            self.assertTrue(initial["run"]["validator_certified"])
            validator_artifacts = [
                artifact
                for artifact in initial["bundle"]["artifacts"]
                if artifact["kind"] == "validator"
            ]
            self.assertEqual(len(validator_artifacts), 1)
            self.assertTrue(validator_artifacts[0]["source_hash"])
            deadline = time.time() + 5
            while time.time() < deadline:
                current = first.run(initial["run"]["id"])
                if current["status"] not in {
                    "pending", "preparing", "running", "stop_requested"
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "mismatch")

            class NeverClient:
                def chat_json(self, messages, **kwargs):
                    raise AssertionError("warm setup must not call DeepSeek")

            def no_crawler():
                raise AssertionError("warm setup must not instantiate a crawler")

            FakeSandbox.calls = []
            warm = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=no_crawler,
            ).start(
                client=NeverClient(),
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={
                    "model": "deepseek-v4-flash",
                    "thinking": True,
                    "reasoning_effort": "high",
                },
                seed=102,
            )
            self.assertEqual(warm["preparation"]["cache_result"], "bundle_hit")
            self.assertEqual(warm["preparation"]["provider_requests"], 0)
            self.assertEqual(warm["preparation"]["helper_compiles"], 0)
            self.assertEqual(warm["preparation"]["preflight_runs"], 0)
            self.assertEqual(warm["bundle"]["id"], initial["bundle"]["id"])
            self.assertFalse(
                any(".audit" in Path(call[0][0]).name for call in FakeSandbox.calls)
            )
            deadline = time.time() + 5
            while time.time() < deadline:
                with Database(paths.database) as db:
                    row = db.stress_run(warm["run"]["id"])
                    status = str(row["status"]) if row is not None else "missing"
                if status not in {"pending", "preparing", "running", "stop_requested"}:
                    break
                time.sleep(0.02)
            self.assertEqual(status, "mismatch")
            helper = Path(warm["bundle"]["artifacts"][0]["target_path"])
            helper.write_text(helper.read_text(encoding="utf-8") + "// user edit\n", encoding="utf-8")
            self.assertIsNone(
                first._cached_preparation(
                    warm["preparation"]["cache_key"],
                    warm["bundle"]["preparation_meta"]["cache_identity"],
                    platform="codeforces",
                    problem_id="CF1A",
                )
            )
            third_client = FakeClient()
            regenerated = first.start(
                client=third_client,
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={
                    "model": "deepseek-v4-flash",
                    "thinking": True,
                    "reasoning_effort": "high",
                },
                seed=103,
            )
            self.assertEqual(
                regenerated["preparation"]["contract_cache_result"], "hit"
            )
            prompt_types = [
                json.loads(messages[-1]["content"]).get("type")
                for messages in third_client.prompts
            ]
            self.assertNotIn("acm_stress_contract", prompt_types)
            deadline = time.time() + 5
            while time.time() < deadline:
                current = first.run(regenerated["run"]["id"])
                if current["status"] not in {
                    "pending", "preparing", "running", "stop_requested"
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "mismatch")

    def test_ordinary_bundle_reuses_only_without_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            model_settings = {
                "model": "deepseek-v4-flash",
                "thinking": True,
                "reasoning_effort": "high",
            }
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            ordinary = coordinator.start(
                client=FakeClient(),
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings=model_settings,
                include_validator=False,
                seed=201,
                run_max_cases=0,
            )
            self.assertFalse(ordinary["run"]["validator_requested"])
            self.assertFalse(ordinary["run"]["validator_certified"])
            self.assertNotIn(
                "validator",
                {item["kind"] for item in ordinary["bundle"]["artifacts"]},
            )
            deadline = time.time() + 5
            while time.time() < deadline:
                current = coordinator.run(ordinary["run"]["id"])
                if current["status"] not in {
                    "pending", "preparing", "running", "stop_requested"
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "completed")

            ordinary_meta = ordinary["bundle"]["preparation_meta"]
            strict_key, strict_identity = coordinator._preparation_cache_identity(
                platform="codeforces",
                problem_id="CF1A",
                statement="statement",
                compare="token",
                include_generator=True,
                include_validator=True,
                include_reference_primary=True,
                include_reference_secondary=True,
                include_large=True,
                model_settings=model_settings,
                sandbox=FakeSandbox(),
                generation_mode="hybrid",
                contract=ordinary["bundle"]["contract"],
                generator_blueprint=ordinary_meta["generator_blueprint"],
            )
            self.assertNotEqual(strict_key, ordinary["preparation"]["cache_key"])
            self.assertIsNone(
                coordinator._cached_preparation(
                    strict_key,
                    strict_identity,
                    platform="codeforces",
                    problem_id="CF1A",
                )
            )

            class NeverClient:
                def chat_json(self, messages, **kwargs):
                    raise AssertionError("ordinary cache reuse must not call DeepSeek")

            warm = coordinator.start(
                client=NeverClient(),
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings=model_settings,
                include_validator=False,
                seed=202,
                run_max_cases=0,
            )
            self.assertEqual(warm["preparation"]["cache_result"], "bundle_hit")
            self.assertEqual(warm["bundle"]["id"], ordinary["bundle"]["id"])
            self.assertFalse(warm["run"]["validator_requested"])
            self.assertFalse(warm["run"]["validator_certified"])
            coordinator.shutdown()

    def test_cache_identity_invalidates_each_external_correctness_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, _source = self._workspace(Path(temp))
            coordinator = StressCoordinator(paths, sandbox_factory=FakeSandbox)
            base = {
                "platform": "codeforces",
                "problem_id": "CF1A",
                "statement": "statement-a",
                "compare": "token",
                "include_generator": True,
                "include_brute": True,
                "include_reference": True,
                "include_large": True,
                "model_settings": {"model": "deepseek-v4-flash"},
                "sandbox": FakeSandbox(),
                "generation_mode": "hybrid",
                "contract": {"output_compare": "token", "small_profile": "n<=8"},
                "generator_blueprint": BLUEPRINT,
            }
            with mock.patch(
                "tools.acm_agent.stress_runtime._compiler_fingerprint",
                return_value="compiler-a",
            ):
                key, _meta = coordinator._preparation_cache_identity(**base)
                strict_key, strict_meta = coordinator._preparation_cache_identity(
                    **base, include_validator=True
                )
                ordinary_key, ordinary_meta = coordinator._preparation_cache_identity(
                    **base, include_validator=False
                )
                self.assertEqual(key, strict_key)
                self.assertNotEqual(strict_key, ordinary_key)
                self.assertTrue(strict_meta["roles"]["validator"])
                self.assertFalse(ordinary_meta["roles"]["validator"])
                self.assertEqual(strict_meta["cache_version"], 5)
                variants = []
                for name, value in (
                    ("statement", "statement-b"),
                    ("compare", "exact"),
                    ("include_large", False),
                    ("model_settings", {"model": "deepseek-v4-pro"}),
                    ("generation_mode", "fast"),
                    ("contract", {"output_compare": "token", "small_profile": "n<=7"}),
                    (
                        "generator_blueprint",
                        {**BLUEPRINT, "required_coverage_tags": ["update", "query"]},
                    ),
                ):
                    changed = dict(base)
                    changed[name] = value
                    variants.append(coordinator._preparation_cache_identity(**changed)[0])
                class OtherFakeSandbox(FakeSandbox):
                    pass

                changed = dict(base)
                changed["sandbox"] = OtherFakeSandbox()
                variants.append(coordinator._preparation_cache_identity(**changed)[0])
                with mock.patch(
                    "tools.acm_agent.stress_runtime.STRESS_SAFETY_POLICY_VERSION", 99
                ):
                    variants.append(coordinator._preparation_cache_identity(**base)[0])
                case_dir = paths.cases / "CF1A"
                case_dir.mkdir(parents=True)
                (case_dir / "sample.in").write_text("1\n", encoding="utf-8")
                (case_dir / "sample.out").write_text("1\n", encoding="utf-8")
                variants.append(coordinator._preparation_cache_identity(**base)[0])
            with mock.patch(
                "tools.acm_agent.stress_runtime._compiler_fingerprint",
                return_value="compiler-b",
            ):
                variants.append(coordinator._preparation_cache_identity(**base)[0])
            self.assertTrue(all(candidate != key for candidate in variants))

    def test_prompt_versions_preserve_contract_and_invalidate_blueprint_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, _source = self._workspace(Path(temp))
            coordinator = StressCoordinator(paths, sandbox_factory=FakeSandbox)
            _contract_key, contract_identity = coordinator._contract_cache_identity(
                problem_id="P2596",
                statement="statement",
                compare="token",
                model_settings={"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "high"},
                generation_mode="hybrid",
            )
            self.assertEqual(contract_identity["prompt_version"], 10)
            self.assertEqual(STRESS_CONTRACT_PROMPT_VERSION, 10)
            self.assertEqual(STRESS_ARTIFACT_PROMPT_VERSION, 14)

            contract = {"output_compare": "token", "small_profile": "n<=8"}
            _blueprint_key, blueprint_identity = coordinator._blueprint_cache_identity(
                problem_id="P2596",
                statement="statement",
                contract=contract,
                model_settings={"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "high"},
                generation_mode="hybrid",
            )
            self.assertEqual(
                blueprint_identity["prompt_version"], STRESS_BLUEPRINT_PROMPT_VERSION
            )
            self.assertNotEqual(blueprint_identity, contract_identity)
            _bundle_key, bundle_identity = coordinator._preparation_cache_identity(
                platform="luogu",
                problem_id="P2596",
                statement="statement",
                compare="token",
                include_generator=True,
                include_brute=True,
                include_reference=True,
                include_large=True,
                model_settings={"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "high"},
                sandbox=FakeSandbox(),
                generation_mode="hybrid",
                contract=contract,
                generator_blueprint=BLUEPRINT,
            )
            self.assertEqual(
                bundle_identity["prompt_versions"],
                {
                    "contract": STRESS_CONTRACT_PROMPT_VERSION,
                    "blueprint": STRESS_BLUEPRINT_PROMPT_VERSION,
                    "recipe": STRESS_RECIPE_PROMPT_VERSION,
                    "artifact": STRESS_ARTIFACT_PROMPT_VERSION,
                },
            )
            self.assertEqual(bundle_identity["recipe_schema_version"], 1)
            self.assertEqual(bundle_identity["composer_version"], 2)
            self.assertEqual(len(bundle_identity["catalog_sha256"]), 64)

    def test_generator_blueprint_failure_label_wins_over_parallel_reference(self) -> None:
        stage, label = _preparation_failure_label(
            {
                "role": "generator",
                "substage": "blueprint",
                "code": "stress_blueprint_invalid",
                "path": "dimensions",
                "attempts": 2,
                "message": "generator blueprint dimensions 必须是非空数组",
            }
        )
        self.assertEqual(stage, "generate_generator")
        self.assertIn("dimensions", label)
        self.assertIn("2/2", label)
        self.assertNotIn("洛谷题解", label)

    def test_generator_preflight_failure_label_keeps_scope_and_shared_attempts(self) -> None:
        # Covers the *declared* ``small/lower_bound`` path: the case is optional
        # policy-wise, but once a blueprint declares it the preflight scope is
        # reportable, so the label must still name the profile/case pair.
        self.assertIn(_GENERATOR_OPTIONAL_CASE, _declared_supported_cases(BLUEPRINT))
        stage, label = _preparation_failure_label(
            {
                "role": "generator",
                "substage": "preflight",
                "code": "stress_generator_coverage_failed",
                "profile": _GENERATOR_OPTIONAL_CASE["profile"],
                "case_kind": _GENERATOR_OPTIONAL_CASE["case_kind"],
                "attempts": 2,
                "message": "generator manifest query failed",
            }
        )
        self.assertEqual(stage, "preflight_helpers")
        self.assertIn(
            f"{_GENERATOR_OPTIONAL_CASE['profile']}/"
            f"{_GENERATOR_OPTIONAL_CASE['case_kind']}",
            label,
        )
        self.assertIn("2/2", label)
        self.assertIn("manifest query failed", label)

    def test_blueprint_repairs_do_not_consume_generator_source_ledger(self) -> None:
        preparation = StressPreparation(
            {},
            None,
            None,
            None,
            {},
            None,
            {"blueprint_repairs_used": 1},
        )
        self.assertEqual(
            _initial_repair_counts(preparation),
            {
                "generator": 0,
                "validator": 0,
                "reference_primary": 0,
                "reference_secondary": 0,
            },
        )
        exhausted = replace(
            preparation, generation_metadata={"blueprint_repairs_used": 99}
        )
        self.assertEqual(_initial_repair_counts(exhausted)["generator"], 0)
        malformed_source = replace(
            preparation, usage={"generator_repairs_used": 1}
        )
        self.assertEqual(_initial_repair_counts(malformed_source)["generator"], 1)

    def test_validated_blueprint_cache_survives_downstream_failure_and_can_be_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, _source = self._workspace(Path(temp))
            coordinator = StressCoordinator(paths, sandbox_factory=FakeSandbox)
            contract = {"output_compare": "token", "small_profile": "n<=8"}
            key, identity = coordinator._blueprint_cache_identity(
                problem_id="P2596",
                statement="statement",
                contract=contract,
                model_settings={"model": "deepseek-v4-flash", "reasoning_effort": "high"},
                generation_mode="hybrid",
            )
            coordinator._save_generator_blueprint(key, identity, BLUEPRINT)
            cached = coordinator._cached_generator_blueprint(key, identity)
            self.assertIsNotNone(cached)
            self.assertEqual(cached[0], BLUEPRINT)

            replacement = json.loads(json.dumps(BLUEPRINT))
            replacement["cases"][1]["construction"] = "new seeded coverage"
            coordinator._save_generator_blueprint(
                key, identity, replacement, replace_alias=True
            )
            refreshed = coordinator._cached_generator_blueprint(key, identity)
            self.assertEqual(
                refreshed[0]["cases"][1]["construction"], "new seeded coverage"
            )
            coordinator._invalidate_generator_blueprint(key)
            self.assertIsNone(coordinator._cached_generator_blueprint(key, identity))

    def test_invalid_generator_capability_reverts_bundle_before_run_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            FakeSandbox.valid_capability = False
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            old_helpers = {}
            for suffix in (".gen.cpp", ".bf.cpp", ".ref.cpp"):
                helper = source.with_name(f"CF1A{suffix}")
                helper.write_text(f"// old {suffix}\n" + SAFE, encoding="utf-8")
                old_helpers[helper] = helper.read_bytes()
            with self.assertRaises(StressRuntimeError) as caught:
                coordinator.start(
                    client=FakeClient(),
                    platform="codeforces",
                    problem_id="CF1A",
                    title="A",
                    statement="statement",
                    primary_source=source,
                    attempt_id=None,
                    model_settings={
                        "model": "deepseek-v4-flash",
                        "thinking": True,
                        "reasoning_effort": "high",
                    },
                    seed=42,
                )
            self.assertEqual(caught.exception.code, "generator_capability_missing")
            with Database(paths.database) as db:
                self.assertEqual(db.stress_runs(), [])
            for helper, old_bytes in old_helpers.items():
                self.assertEqual(helper.read_bytes(), old_bytes)

    def test_budget_exhaustion_after_contract_keeps_old_helpers_and_creates_no_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            old_helpers = {}
            for suffix in (".gen.cpp", ".bf.cpp", ".ref.cpp"):
                helper = source.with_name(f"CF1A{suffix}")
                helper.write_text(f"// old {suffix}\n" + SAFE, encoding="utf-8")
                old_helpers[helper] = helper.read_bytes()

            class Clock:
                now = 100.0

                def __call__(self):
                    return self.now

            clock = Clock()
            budget = PreparationBudget(60, clock=clock)

            class ExpiringClient(FakeClient):
                def chat_json(self, messages, **kwargs):
                    result = super().chat_json(messages, **kwargs)
                    request = json.loads(messages[-1]["content"])
                    if request.get("type") == "acm_stress_contract":
                        clock.now = budget.work_deadline
                    return result

            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            with self.assertRaises(PreparationBudgetExhausted) as caught:
                coordinator.start(
                    client=ExpiringClient(),
                    platform="codeforces",
                    problem_id="CF1A",
                    title="A",
                    statement="statement",
                    primary_source=source,
                    attempt_id=None,
                    model_settings={
                        "model": "deepseek-v4-flash",
                        "thinking": True,
                        "reasoning_effort": "high",
                    },
                    preparation_budget=budget,
                )
            self.assertEqual(caught.exception.code, "stress_prepare_budget_exhausted")
            with Database(paths.database) as db:
                self.assertEqual(db.stress_runs(), [])
            for helper, old_bytes in old_helpers.items():
                self.assertEqual(helper.read_bytes(), old_bytes)

    def test_retired_profile_is_rejected_instead_of_resumed(self) -> None:
        with self.assertRaises(StressRuntimeError) as caught:
            _stress_config(
                {
                    "first_seed": 41,
                    "profile_version": 1,
                    "warmup_small_cases": 200,
                },
                first_seed=53,
                schedule_offset=11,
            )
        self.assertEqual(caught.exception.code, "stress_profile_unsupported")

    def test_unavailable_sandbox_fails_before_writing_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=lambda: FakeSandbox(available=False),
                crawler_factory=EmptyCrawler,
            )
            with self.assertRaises(SandboxUnavailableError):
                coordinator.start(
                    client=FakeClient(),
                    platform="codeforces",
                    problem_id="CF1A",
                    title="A",
                    statement="statement",
                    primary_source=source,
                    attempt_id=None,
                    model_settings={
                        "model": "deepseek-v4-flash",
                        "thinking": True,
                        "reasoning_effort": "high",
                    },
                )
            self.assertFalse(source.with_name("CF1A.gen.cpp").exists())

    def test_deprecated_brute_file_maps_to_secondary_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths, source = self._workspace(root)
            manual_dir = root / "manual"
            manual_dir.mkdir()
            manual_brute = manual_dir / "brute.cpp"
            manual_brute.write_text(SAFE, encoding="utf-8")
            client = FakeClient()
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            started = coordinator.start(
                client=client,
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={
                    "model": "deepseek-v4-flash",
                    "thinking": True,
                    "reasoning_effort": "high",
                },
                brute_file=manual_brute,
                run_max_cases=0,
            )
            self.assertTrue(started["ok"])
            bundle_id = started["bundle"]["id"]
            with Database(paths.database) as db:
                artifacts = db.stress_artifacts(bundle_id)
                secondary = next(
                    item
                    for item in artifacts
                    if item["kind"] == "reference_secondary"
                )
                self.assertEqual(secondary["source_kind"], "user_specified")
                bundle_row = db.stress_artifact_bundle(bundle_id)
                meta = json.loads(bundle_row["preparation_meta_json"] or "{}")
                self.assertNotIn("user_participation", meta)
            brute_prompts = [
                messages
                for messages in client.prompts
                if json.loads(messages[-1]["content"]).get("type")
                in {"acm_stress_brute", "acm_stress_brute_code_only"}
            ]
            self.assertEqual(brute_prompts, [])
            run_id = started["run"]["id"]
            deadline = time.time() + 5
            while time.time() < deadline:
                current = coordinator.run(run_id)
                if current["status"] not in {
                    "pending",
                    "preparing",
                    "running",
                    "stop_requested",
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "completed")
            coordinator.shutdown()

    def test_manual_generator_skips_blueprint_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths, source = self._workspace(root)
            manual_dir = root / "manual"
            manual_dir.mkdir()
            manual_generator = manual_dir / "generator.cpp"
            manual_generator.write_text(GENERATOR, encoding="utf-8")
            client = FakeClient()
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            started = coordinator.start(
                client=client,
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={
                    "model": "deepseek-v4-flash",
                    "thinking": True,
                    "reasoning_effort": "high",
                },
                generator_file=manual_generator,
                run_max_cases=0,
            )
            self.assertTrue(started["ok"])
            blueprint_prompts = [
                messages
                for messages in client.prompts
                if json.loads(messages[-1]["content"]).get("type")
                == "acm_stress_generator_blueprint_v1"
            ]
            self.assertEqual(blueprint_prompts, [])
            with Database(paths.database) as db:
                artifacts = db.stress_artifacts(started["bundle"]["id"])
                generator = next(
                    item for item in artifacts if item["kind"] == "generator"
                )
                self.assertEqual(generator["source_kind"], "user_specified")
            run_id = started["run"]["id"]
            deadline = time.time() + 5
            while time.time() < deadline:
                current = coordinator.run(run_id)
                if current["status"] not in {
                    "pending",
                    "preparing",
                    "running",
                    "stop_requested",
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "completed")
            coordinator.shutdown()

    def test_manual_helper_allows_absolute_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            container = Path(temp)
            paths, source = self._workspace(container / "workspace")
            outside = container / "outside-brute.cpp"
            outside.write_text(SAFE, encoding="utf-8")
            before = outside.read_bytes()
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            started = coordinator.start(
                client=FakeClient(),
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={
                    "model": "deepseek-v4-flash",
                    "thinking": True,
                    "reasoning_effort": "high",
                },
                brute_file=outside,
                run_max_cases=0,
            )
            self.assertTrue(started["ok"])
            self.assertEqual(outside.read_bytes(), before)
            coordinator.shutdown()

    def test_manual_helper_rejects_missing_suffix_symlink_and_network_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            wrong = paths.root / "manual.txt"
            wrong.write_text(SAFE, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"\.cpp"):
                coordinator._resolve_manual_helper(wrong, role="generator")
            with self.assertRaisesRegex(ValueError, r"\.cpp"):
                coordinator._resolve_manual_helper("missing.cpp", role="generator")
            for raw in (r"\\server\share\helper.cpp", r"\\?\C:\helper.cpp", r"\\.\C:\helper.cpp"):
                with self.subTest(raw=raw), self.assertRaisesRegex(
                    ValueError, "UNC/设备路径"
                ):
                    coordinator._resolve_manual_helper(raw, role="generator")
            target = paths.root / "target.cpp"
            target.write_text(SAFE, encoding="utf-8")
            link = paths.root / "link.cpp"
            try:
                link.symlink_to(target)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(ValueError, "符号链接"):
                    coordinator._resolve_manual_helper(link, role="generator")

    def test_pre_apply_gate_rejects_non_benchmark_callable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            client = FakeClient()

            def foreign_gate(staged):
                return None

            with self.assertRaisesRegex(
                ValueError, "only available to the local stress benchmark"
            ):
                coordinator.start(
                    client=client,
                    platform="codeforces",
                    problem_id="CF1A",
                    title="A",
                    statement="statement",
                    primary_source=source,
                    attempt_id=None,
                    model_settings={"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "high"},
                    _pre_apply_gate=foreign_gate,
                )
            self.assertEqual(client.generated, 0)
            self.assertFalse(source.with_name("CF1A.gen.cpp").exists())

    def test_pre_apply_gate_failure_discards_staged_and_never_applies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            gate_calls: list[str] = []

            def failing_gate(staged):
                gate_calls.append(staged.bundle_id)
                raise RuntimeError(
                    "generated validator rejected hidden valid corpus item 1"
                )

            failing_gate.__module__ = "tests.manual.benchmarks.stress_benchmark"
            with self.assertRaises(StressRuntimeError) as caught:
                coordinator.start(
                    client=FakeClient(),
                    platform="codeforces",
                    problem_id="CF1A",
                    title="A",
                    statement="statement",
                    primary_source=source,
                    attempt_id=None,
                    model_settings={"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "high"},
                    _pre_apply_gate=failing_gate,
                )
            self.assertEqual(caught.exception.code, "stress_pre_apply_gate")
            self.assertIn(
                "rejected hidden valid corpus",
                str(caught.exception.details.get("message") or ""),
            )
            self.assertEqual(len(gate_calls), 1)
            with Database(paths.database) as db:
                applied = db.connection.execute(
                    "SELECT COUNT(*) FROM stress_artifact_bundles WHERE status='applied'"
                ).fetchone()[0]
                runs = db.connection.execute(
                    "SELECT COUNT(*) FROM stress_runs"
                ).fetchone()[0]
            self.assertEqual(int(applied), 0)
            self.assertEqual(int(runs), 0)
            for suffix in (".gen.cpp", ".bf.cpp", ".ref.cpp", ".validator.cpp"):
                self.assertFalse(source.with_name(f"CF1A{suffix}").exists())
            staging = paths.state_dir / "stress-staging"
            self.assertEqual(
                list(staging.iterdir()) if staging.is_dir() else [], []
            )
            run_root = paths.state_dir / "stress-runs"
            self.assertEqual(
                list(run_root.iterdir()) if run_root.is_dir() else [], []
            )

    def test_pre_apply_gate_success_applies_and_surfaces_gate_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            gate_events: list[tuple[str, bool, bool]] = []

            def passing_gate(staged):
                validator_ready = Path(staged.staged_paths["validator"]).is_file()
                gate_events.append(
                    (staged.bundle_id, validator_ready, staged.applied)
                )
                return {
                    "valid_accepted": 8,
                    "invalid_rejected": 3,
                    "semantic_zero_misrelease": True,
                }

            passing_gate.__module__ = "tests.manual.benchmarks.stress_benchmark"
            started = coordinator.start(
                client=FakeClient(),
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "high"},
                _pre_apply_gate=passing_gate,
                run_max_cases=0,
            )
            self.assertTrue(started["ok"])
            self.assertEqual(
                started["pre_apply_gate_result"],
                {
                    "valid_accepted": 8,
                    "invalid_rejected": 3,
                    "semantic_zero_misrelease": True,
                },
            )
            self.assertEqual(len(gate_events), 1)
            bundle_id, validator_ready, applied_at_gate = gate_events[0]
            self.assertTrue(validator_ready)
            self.assertFalse(applied_at_gate)
            self.assertEqual(started["bundle"]["id"], bundle_id)
            with Database(paths.database) as db:
                applied = db.connection.execute(
                    "SELECT COUNT(*) FROM stress_artifact_bundles WHERE status='applied'"
                ).fetchone()[0]
                runs = db.connection.execute(
                    "SELECT COUNT(*) FROM stress_runs"
                ).fetchone()[0]
            self.assertEqual(int(applied), 1)
            self.assertEqual(int(runs), 1)
            self.assertTrue(source.with_name("CF1A.gen.cpp").is_file())
            run_id = started["run"]["id"]
            deadline = time.time() + 5
            while time.time() < deadline:
                current = coordinator.run(run_id)
                if current["status"] not in {
                    "pending",
                    "preparing",
                    "running",
                    "stop_requested",
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "completed")
            coordinator.shutdown()

    def test_warm_cache_reuse_skips_pre_apply_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=FakeSandbox,
                crawler_factory=EmptyCrawler,
            )
            gate_calls: list[str] = []

            def passing_gate(staged):
                gate_calls.append(staged.bundle_id)
                return {"semantic_zero_misrelease": True}

            passing_gate.__module__ = "tests.manual.benchmarks.stress_benchmark"
            client = FakeClient()
            first = coordinator.start(
                client=client,
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "high"},
                _pre_apply_gate=passing_gate,
                run_max_cases=0,
            )
            calls_after_cold = client.generated
            first_run = first["run"]["id"]
            deadline = time.time() + 5
            while time.time() < deadline:
                current = coordinator.run(first_run)
                if current["status"] not in {
                    "pending",
                    "preparing",
                    "running",
                    "stop_requested",
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "completed")
            warm = coordinator.start(
                client=client,
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "high"},
                cache_mode="reuse",
                _pre_apply_gate=passing_gate,
                run_max_cases=0,
            )
            self.assertTrue(warm["ok"])
            self.assertEqual(client.generated, calls_after_cold)
            self.assertIsNone(warm.get("pre_apply_gate_result"))
            self.assertEqual(len(gate_calls), 1)
            coordinator.shutdown()

    def test_validator_contract_probe_repair_never_crashes_and_keeps_probes_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            client = ProbeContractClient()
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=ProbeFailingSandbox,
                crawler_factory=EmptyCrawler,
            )
            with self.assertRaises(StressRuntimeError) as caught:
                coordinator.start(
                    client=client,
                    platform="codeforces",
                    problem_id="CF1A",
                    title="A",
                    statement="statement",
                    primary_source=source,
                    attempt_id=None,
                    model_settings={
                        "model": "deepseek-v4-flash",
                        "thinking": True,
                        "reasoning_effort": "high",
                    },
                )
            self.assertEqual(
                caught.exception.code, "stress_validator_positive_probe_failed"
            )
            self.assertEqual(
                int(caught.exception.usage.get("validator_repairs_used") or 0), 1
            )
            with Database(paths.database) as db:
                applied = db.connection.execute(
                    "SELECT COUNT(*) FROM stress_artifact_bundles WHERE status='applied'"
                ).fetchone()[0]
                runs = db.connection.execute(
                    "SELECT COUNT(*) FROM stress_runs"
                ).fetchone()[0]
            self.assertEqual(int(applied), 0)
            self.assertEqual(int(runs), 0)
            for suffix in (".gen.cpp", ".bf.cpp", ".ref.cpp", ".validator.cpp"):
                self.assertFalse(source.with_name(f"CF1A{suffix}").exists())
            staging = paths.state_dir / "stress-staging"
            self.assertEqual(
                list(staging.iterdir()) if staging.is_dir() else [], []
            )
            validator_prompts: list[str] = []
            for messages in client.prompts:
                request = json.loads(messages[-1]["content"])
                rtype = str(request.get("type") or "")
                is_validator_prompt = (
                    rtype in {"acm_stress_validator", "acm_stress_validator_code_only"}
                    or (
                        rtype == "acm_stress_artifact_repair"
                        and request.get("artifact_kind") == "validator"
                    )
                    or rtype == "ai_stress_artifact_static_audit"
                )
                if is_validator_prompt:
                    validator_prompts.append(json.dumps(messages, ensure_ascii=False))
            joined = "".join(validator_prompts)
            self.assertNotIn("Insert 2 -1", joined)
            self.assertNotIn("vp_dynamic", joined)


class StressValidatorDegradationTests(unittest.TestCase):
    """Validator probe failure degrades to an unvalidated triple-oracle run."""

    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_probe_failure_degrades_to_unvalidated_small_only_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            client = ProbeContractClient()
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=DegradedCoverageSandbox,
                crawler_factory=EmptyCrawler,
            )
            started = coordinator.start(
                client=client,
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={
                    "model": "deepseek-v4-flash",
                    "thinking": True,
                    "reasoning_effort": "high",
                },
                allow_validator_degradation=True,
                seed=42,
            )
            run = started["run"]
            self.assertTrue(run["unvalidated"])
            self.assertTrue(
                run["degraded_reason"].startswith("stress_validator_")
                and run["degraded_reason"].endswith("_probe_failed")
            )
            self.assertFalse(run["config"]["include_large"])
            self.assertNotIn("validator", started["bundle"]["artifacts"])
            with Database(paths.database) as db:
                certifications = db.connection.execute(
                    "SELECT certification_identity_json"
                    " FROM stress_bundle_certifications"
                ).fetchall()
                runs = db.connection.execute(
                    "SELECT config_json FROM stress_runs"
                ).fetchall()
            self.assertEqual(len(certifications), 1)
            identity = json.loads(certifications[0][0])
            self.assertEqual(
                identity["validator"],
                {"candidate_id": "unvalidated", "source_sha256": "none"},
            )
            self.assertEqual(len(runs), 1)
            run_config = json.loads(runs[0][0])
            self.assertTrue(run_config["unvalidated"])
            self.assertTrue(
                str(run_config["degraded_reason"]).startswith("stress_validator_")
            )
            # The run still launches and executes the triple oracle on smalls.
            deadline = time.time() + 8
            while time.time() < deadline:
                current = coordinator.run(run["id"])
                if current["status"] not in {
                    "pending", "preparing", "running", "stop_requested",
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "mismatch")
            self.assertEqual(current["small_count"], 1)
            self.assertEqual(current["large_count"], 0)
            self.assertTrue(current["unvalidated"])
            coordinator.shutdown()

    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_unvalidated_run_keeps_large_only_when_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            client = ProbeContractClient()
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=DegradedCoverageSandbox,
                crawler_factory=EmptyCrawler,
            )
            started = coordinator.start(
                client=client,
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={
                    "model": "deepseek-v4-flash",
                    "thinking": True,
                    "reasoning_effort": "high",
                },
                allow_validator_degradation=True,
                unvalidated_large=True,
                run_max_cases=2,
            )
            run = started["run"]
            self.assertTrue(run["unvalidated"])
            self.assertTrue(run["config"]["include_large"])
            deadline = time.time() + 8
            while time.time() < deadline:
                current = coordinator.run(run["id"])
                if current["status"] not in {
                    "pending", "preparing", "running", "stop_requested",
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "mismatch")
            self.assertTrue(current["config"]["include_large"])
            coordinator.shutdown()

    def test_strict_mode_still_raises_when_degradation_disallowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            client = ProbeContractClient()
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=ProbeFailingSandbox,
                crawler_factory=EmptyCrawler,
            )
            with self.assertRaises(StressRuntimeError) as caught:
                coordinator.start(
                    client=client,
                    platform="codeforces",
                    problem_id="CF1A",
                    title="A",
                    statement="statement",
                    primary_source=source,
                    attempt_id=None,
                    model_settings={
                        "model": "deepseek-v4-flash",
                        "thinking": True,
                        "reasoning_effort": "high",
                    },
                    include_validator=True,
                    allow_validator_degradation=False,
                )
            self.assertEqual(
                caught.exception.code, "stress_validator_positive_probe_failed"
            )
            with Database(paths.database) as db:
                bundle_count = db.connection.execute(
                    "SELECT COUNT(*) FROM stress_artifact_bundles"
                ).fetchone()[0]
                run_count = db.connection.execute(
                    "SELECT COUNT(*) FROM stress_runs"
                ).fetchone()[0]
            self.assertEqual(bundle_count, 0)
            self.assertEqual(run_count, 0)
            for suffix in (".gen.cpp", ".ref1.cpp", ".ref2.cpp"):
                self.assertFalse(source.with_name(f"{source.stem}{suffix}").exists())
            coordinator.shutdown()

    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_minimal_mode_passes_manifest_crashing_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            client = ProbeContractClient()
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=ManifestCrashingSandbox,
                crawler_factory=EmptyCrawler,
            )
            started = coordinator.start(
                client=client,
                platform="codeforces",
                problem_id="CF1A",
                title="A",
                statement="statement",
                primary_source=source,
                attempt_id=None,
                model_settings={
                    "model": "deepseek-v4-flash",
                    "thinking": True,
                    "reasoning_effort": "high",
                },
                minimal_verification=True,
                seed=42,
            )
            run = started["run"]
            self.assertTrue(run["unvalidated"])
            self.assertEqual(run["degraded_reason"], "minimal_verification")
            self.assertFalse(run["config"]["include_large"])
            # No validator and no AI audit in minimal mode.
            self.assertNotIn("validator", started["bundle"]["artifacts"])
            for item in started["bundle"]["artifacts"]:
                self.assertFalse(item["validation"].get("ai_audit"))
            # The run still launches and executes the triple oracle on smalls.
            deadline = time.time() + 8
            while time.time() < deadline:
                current = coordinator.run(run["id"])
                if current["status"] not in {
                    "pending", "preparing", "running", "stop_requested",
                }:
                    break
                time.sleep(0.02)
            self.assertEqual(current["status"], "mismatch")
            self.assertEqual(current["small_count"], 1)
            self.assertEqual(current["large_count"], 0)
            coordinator.shutdown()

    @unittest.skipUnless(shutil.which("g++"), "g++ is required")
    def test_full_mode_still_rejects_manifest_crashing_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths, source = self._workspace(Path(temp))
            client = ProbeContractClient()
            coordinator = StressCoordinator(
                paths,
                sandbox_factory=ManifestCrashingSandbox,
                crawler_factory=EmptyCrawler,
            )
            with self.assertRaises(StressRuntimeError) as caught:
                coordinator.start(
                    client=client,
                    platform="codeforces",
                    problem_id="CF1A",
                    title="A",
                    statement="statement",
                    primary_source=source,
                    attempt_id=None,
                    model_settings={
                        "model": "deepseek-v4-flash",
                        "thinking": True,
                        "reasoning_effort": "high",
                    },
                    minimal_verification=False,
                    allow_validator_degradation=True,
                )
            self.assertEqual(
                caught.exception.code, "stress_generator_coverage_failed"
            )
            coordinator.shutdown()

    def _workspace(self, temp: Path):
        paths = Paths.for_root(temp / "root")
        paths.ensure()
        source = paths.root / "2026" / "8" / "7" / "CF1A.cpp"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(SAFE, encoding="utf-8")
        return paths, source


class _ScheduleSandbox:
    """Profile-v2 sandbox that records the schedule the runner actually asks for.

    ``declare_lower_bound=False`` simulates an obsolete generator missing the
    now-required ``small/lower_bound`` capability. Solution and both references
    always agree, letting a run reach ``max_cases`` instead of stopping on the
    first mismatch.
    """

    def __init__(self, *, declare_lower_bound: bool) -> None:
        self.declare_lower_bound = declare_lower_bound
        self.generated: list[tuple[int, str, str]] = []

    def probe(self) -> SandboxCapability:
        return SandboxCapability(True, "", "fake")

    def cancel(self) -> None:  # pragma: no cover - runner only calls on stop
        pass

    def _capabilities(self) -> dict[str, object]:
        supported = [dict(pair) for pair in _GENERATOR_REQUIRED_CASES]
        if not self.declare_lower_bound:
            supported = [pair for pair in supported if pair != _GENERATOR_OPTIONAL_CASE]
        kinds: list[str] = []
        for pair in supported:
            if pair["case_kind"] not in kinds:
                kinds.append(pair["case_kind"])
        return {
            "profile_version": 2,
            "manifest_version": 1,
            "profiles": ["small", "large"],
            "case_kinds": kinds,
            "supported_cases": supported,
        }

    def run(self, command, *, cwd, input_data=None, env=None, limits=None):
        role = Path(command[0]).stem
        if "generator" in role:
            if "--capabilities" in command:
                return SandboxProcessResult(
                    list(command),
                    0,
                    json.dumps(self._capabilities()).encode("utf-8"),
                )
            if len(command) == 5 and command[1] == "--manifest":
                seed, profile, case_kind = command[2], command[3], command[4]
                generated = f"{profile}:{case_kind}:{seed}\n".encode()
                payload = {
                    "manifest_version": 1,
                    "profile": profile,
                    "case_kind": case_kind,
                    "seed": int(seed),
                    "input_sha256": hashlib.sha256(generated).hexdigest(),
                    "dimensions": {"n": 1 if profile == "small" else 100},
                    "coverage_tags": [],
                    "records": 1,
                    "total_complexity": "linear_output",
                }
                return SandboxProcessResult(
                    list(command), 0, json.dumps(payload).encode("utf-8")
                )
            seed, profile, case_kind = command[1], command[2], command[3]
            self.generated.append((int(seed), profile, case_kind))
            return SandboxProcessResult(
                list(command), 0, f"{profile}:{case_kind}:{seed}\n".encode()
            )
        return SandboxProcessResult(list(command), 0, b"agreed\n")


def _contract_schedule(
    count: int, *, declared: bool, config: StressRunConfig
) -> list[tuple[str, str]]:
    """Independent restatement of the documented profile-v2 schedule policy.

    Written from the policy rather than from ``_case_for_index``'s body: an
    optional ``small/lower_bound`` prelude entry *only when the generator
    declares it*, then ``large/upper_bound`` when large is enabled, then a
    ``small_per_cycle``:``large_per_cycle`` cycle of random cases.  Indices are
    absolute, so ``schedule_offset`` shifts the prelude the same way a resume
    does.
    """
    prelude: list[tuple[str, str]] = []
    if declared:
        prelude.append(("small", "lower_bound"))
    if config.include_large:
        prelude.append(("large", "upper_bound"))
    schedule: list[tuple[str, str]] = []
    for index in range(count):
        absolute = config.schedule_offset + index
        if absolute < len(prelude):
            schedule.append(prelude[absolute])
            continue
        position = absolute - len(prelude)
        if not config.include_large or config.large_per_cycle <= 0:
            schedule.append(("small", "random"))
            continue
        cycle = config.small_per_cycle + config.large_per_cycle
        schedule.append(
            ("small", "random")
            if position % cycle < config.small_per_cycle
            else ("large", "random")
        )
    return schedule


class LowerBoundSchedulePolicyTests(unittest.TestCase):
    """``small/lower_bound`` is required and always leads the v2 schedule."""

    OPTIONAL_PAIR = (
        _GENERATOR_OPTIONAL_CASE["profile"],
        _GENERATOR_OPTIONAL_CASE["case_kind"],
    )

    @staticmethod
    def _executables(root: Path) -> StressExecutables:
        executables = StressExecutables(
            solution=root / "solution.exe",
            generator=root / "generator.exe",
            reference_primary=root / "reference_primary.exe",
            reference_secondary=root / "reference_secondary.exe",
        )
        for path in (
            executables.solution,
            executables.generator,
            executables.reference_primary,
            executables.reference_secondary,
        ):
            path.write_bytes(b"test executable")
        return executables

    def _runner(self, root: Path, sandbox: _ScheduleSandbox) -> LayeredStressRunner:
        return LayeredStressRunner(root, "CF1A", self._executables(root), sandbox)

    def _run_schedule(
        self, *, declared: bool, config: StressRunConfig
    ) -> tuple[list[tuple[int, str, str]], object]:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sandbox = _ScheduleSandbox(declare_lower_bound=declared)
            result = self._runner(root, sandbox).run(config)
            return list(sandbox.generated), result

    def test_lower_bound_case_is_in_the_required_policy_set(self) -> None:
        self.assertIn(_GENERATOR_OPTIONAL_CASE, _GENERATOR_REQUIRED_CASES)
        self.assertEqual(len(_GENERATOR_REQUIRED_CASES), 4)
        self.assertEqual(
            {(pair["profile"], pair["case_kind"]) for pair in _GENERATOR_REQUIRED_CASES},
            {("small", "lower_bound"), ("small", "random"), ("large", "upper_bound"), ("large", "random")},
        )
        self.assertEqual(BLUEPRINT["required_cases"], list(_GENERATOR_REQUIRED_CASES))
        self.assertEqual(PROBE_BLUEPRINT["required_cases"], list(_GENERATOR_REQUIRED_CASES))

    def test_missing_lower_bound_capability_is_rejected(self) -> None:
        config = StressRunConfig(first_seed=42, max_cases=12, include_large=True)
        with self.assertRaises(GeneratorCapabilityError):
            self._run_schedule(declared=False, config=config)

    def test_missing_lower_bound_is_rejected_even_without_large(self) -> None:
        config = StressRunConfig(
            first_seed=7, max_cases=6, include_large=False, large_per_cycle=0
        )
        with self.assertRaises(GeneratorCapabilityError):
            self._run_schedule(declared=False, config=config)

    def test_declared_lower_bound_still_runs_and_stays_first(self) -> None:
        config = StressRunConfig(first_seed=42, max_cases=12, include_large=True)
        generated, result = self._run_schedule(declared=True, config=config)

        self.assertEqual(result.status, "limit_reached")
        # Declared -> executed, exactly once, and ahead of large/upper_bound.
        self.assertEqual(generated[0][1:], self.OPTIONAL_PAIR)
        self.assertEqual(generated[1][1:], ("large", "upper_bound"))
        self.assertEqual(
            [(profile, kind) for _seed, profile, kind in generated].count(
                self.OPTIONAL_PAIR
            ),
            1,
        )
        self.assertEqual(
            [(profile, kind) for _seed, profile, kind in generated],
            _contract_schedule(config.max_cases, declared=True, config=config),
        )
        self.assertEqual(generated[0][0], config.first_seed)

    def test_declared_lower_bound_leads_even_without_large(self) -> None:
        config = StressRunConfig(
            first_seed=42, max_cases=5, include_large=False, large_per_cycle=0
        )
        generated, result = self._run_schedule(declared=True, config=config)

        self.assertEqual(result.status, "limit_reached")
        self.assertEqual(generated[0][1:], self.OPTIONAL_PAIR)
        # Large disabled: the prelude is the optional case alone.
        self.assertEqual(
            [(profile, kind) for _seed, profile, kind in generated],
            [self.OPTIONAL_PAIR] + [("small", "random")] * (config.max_cases - 1),
        )
        self.assertNotIn("large", {profile for _seed, profile, _kind in generated})

    def test_case_for_index_prelude_arithmetic_pins_the_small_large_cycle(self) -> None:
        """Directly pin the prelude length and the post-prelude 4:1 cycle."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self._runner(root, _ScheduleSandbox(declare_lower_bound=False))
            for declared in (True, False):
                for include_large in (True, False):
                    with self.subTest(declared=declared, include_large=include_large):
                        config = StressRunConfig(
                            first_seed=0,
                            include_large=include_large,
                            large_per_cycle=1 if include_large else 0,
                        )
                        runner._supports_lower_bound = declared
                        observed = [
                            runner._case_for_index(index, config) for index in range(12)
                        ]
                        self.assertEqual(
                            observed,
                            _contract_schedule(12, declared=declared, config=config),
                        )

                        # Prelude length is exactly the two independent flags.
                        prelude = int(declared) + int(include_large)
                        expected_prelude = []
                        if declared:
                            expected_prelude.append(self.OPTIONAL_PAIR)
                        if include_large:
                            expected_prelude.append(("large", "upper_bound"))
                        self.assertEqual(observed[:prelude], expected_prelude)
                        # Boundary cases appear only in the prelude.
                        self.assertNotIn(
                            "lower_bound", {kind for _p, kind in observed[prelude:]}
                        )
                        self.assertNotIn(
                            "upper_bound", {kind for _p, kind in observed[prelude:]}
                        )

                        tail = observed[prelude:]
                        if include_large:
                            cycle = config.small_per_cycle + config.large_per_cycle
                            self.assertEqual(cycle, 5)
                            # One large/random per cycle, and it lands last.
                            first_cycle = tail[:cycle]
                            self.assertEqual(
                                first_cycle,
                                [("small", "random")] * config.small_per_cycle
                                + [("large", "random")] * config.large_per_cycle,
                            )
                            # The cycle repeats, so it is genuinely periodic.
                            self.assertEqual(tail[cycle : 2 * cycle], first_cycle)
                        else:
                            self.assertEqual(tail, [("small", "random")] * len(tail))

    def test_schedule_offset_does_not_replay_the_prelude_on_resume(self) -> None:
        """``absolute = schedule_offset + index`` keeps resumes past the prelude."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = self._runner(root, _ScheduleSandbox(declare_lower_bound=True))
            runner._supports_lower_bound = True
            config = StressRunConfig(
                first_seed=0, include_large=True, schedule_offset=2
            )
            observed = [runner._case_for_index(index, config) for index in range(10)]
            self.assertNotIn("lower_bound", {kind for _profile, kind in observed})
            self.assertNotIn("upper_bound", {kind for _profile, kind in observed})
            self.assertEqual(
                observed, _contract_schedule(10, declared=True, config=config)
            )


if __name__ == "__main__":
    unittest.main()
