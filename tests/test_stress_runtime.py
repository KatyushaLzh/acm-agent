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
    SandboxCapability,
    SandboxProcessResult,
    SandboxUnavailableError,
)
from tools.acm_agent.stress_runtime import (
    STRESS_ARTIFACT_PROMPT_VERSION,
    STRESS_BLUEPRINT_PROMPT_VERSION,
    STRESS_CONTRACT_PROMPT_VERSION,
    StressCoordinator,
    StressRuntimeError,
    _contract_probe_repair_diagnostic,
    _generator_safe_seed_families,
    _generator_record_count_hint,
    _generator_repair_invariants,
    _initial_repair_counts,
    _next_external_reference,
    _persistent_generator_seed_requirement,
    _run_locally_confirmed_gate,
    _preparation_failure_label,
    _stress_config,
)
from tools.acm_agent.stress_ai import GeneratedArtifact, StressPreparation
from tools.acm_agent.stress_budget import PreparationBudget, PreparationBudgetExhausted


SAFE = "#include <iostream>\nint main(){return 0;}\n"
GENERATOR = (
    "#include <iostream>\n#include <string>\n"
    "void acm_generate_case(unsigned long long,const std::string&,"
    "const std::string&,std::ostream&){}\n"
)
VALIDATOR = SAFE


BLUEPRINT = {
    "schema_version": 1,
    "required_cases": [
        {"profile": "small", "case_kind": "lower_bound"},
        {"profile": "small", "case_kind": "random"},
        {"profile": "large", "case_kind": "upper_bound"},
        {"profile": "large", "case_kind": "random"},
    ],
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


class Result:
    def __init__(self, data=None, content="{}"):
        self.data = data or {}
        self.content = content
        self.usage = {"total_tokens": 1}


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
        time.sleep({"generator": 0.05, "brute": 0.01, "reference": 0.08}[role])
        if role == "brute":
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
                payload = (
                    {
                        "profile_version": 2,
                        "manifest_version": 1,
                        "profiles": ["small", "large"],
                        "case_kinds": ["lower_bound", "upper_bound", "random"],
                        "supported_cases": list(BLUEPRINT["required_cases"]),
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


@unittest.skipUnless(shutil.which("g++"), "g++ is required")
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
            artifact = lambda role: GeneratedArtifact(
                role, SAFE, "ai_generated", "fixture"
            )
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
                model_settings={"model": "deepseek-v4-flash"},
                progress_callback=None,
            )
            self.assertEqual(client.max_active, 3)
            self.assertEqual(set(reports), {"generator", "brute", "reference"})
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
                    model_settings={"model": "deepseek-v4-flash"},
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
            artifact = lambda role: GeneratedArtifact(role, SAFE, "ai_generated", "")
            with self.assertRaises(StressRuntimeError) as caught:
                coordinator._audit_and_repair_generated_artifacts(
                    RoleFailureClient(),
                    StressPreparation(
                        {"input_summary": "n"},
                        artifact("generator"),
                        artifact("brute"),
                        artifact("reference"),
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
            self.assertEqual(set(caught.exception.details["roles"]), {"brute"})
            self.assertEqual(caught.exception.details["roles"]["brute"]["role"], "brute")
            self.assertIn("10054", caught.exception.details["roles"]["brute"]["message"])

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
            self.assertEqual(len(started["bundle"]["artifacts"]), 3)
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
            self.assertTrue(source.with_name("CF1A.ref.cpp").is_file())
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
                    "generate_brute",
                    "generate_validator",
                    "prepare_reference",
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
            self.assertEqual(
                generated_cases,
                [["42", "small", "lower_bound"], ["43", "large", "upper_bound"]],
            )
            brute_calls = [
                command
                for command, _env, _limits in FakeSandbox.calls
                if Path(command[0]).stem.endswith("brute")
                and "stress-runs" in Path(command[0]).parts
            ]
            self.assertEqual(len(brute_calls), 1)

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
                model_settings={"model": "deepseek-v4-flash"},
                generation_mode="hybrid",
            )
            self.assertEqual(contract_identity["prompt_version"], 5)
            self.assertEqual(STRESS_CONTRACT_PROMPT_VERSION, 5)

            contract = {"output_compare": "token", "small_profile": "n<=8"}
            _blueprint_key, blueprint_identity = coordinator._blueprint_cache_identity(
                problem_id="P2596",
                statement="statement",
                contract=contract,
                model_settings={"model": "deepseek-v4-flash"},
                generation_mode="hybrid",
            )
            self.assertEqual(
                blueprint_identity["prompt_version"], STRESS_BLUEPRINT_PROMPT_VERSION
            )
            self.assertNotEqual(
                blueprint_identity["prompt_version"],
                contract_identity["prompt_version"],
            )
            _bundle_key, bundle_identity = coordinator._preparation_cache_identity(
                platform="luogu",
                problem_id="P2596",
                statement="statement",
                compare="token",
                include_generator=True,
                include_brute=True,
                include_reference=True,
                include_large=True,
                model_settings={"model": "deepseek-v4-flash"},
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
                    "artifact": STRESS_ARTIFACT_PROMPT_VERSION,
                },
            )

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
        stage, label = _preparation_failure_label(
            {
                "role": "generator",
                "substage": "preflight",
                "code": "stress_generator_coverage_failed",
                "profile": "small",
                "case_kind": "lower_bound",
                "attempts": 2,
                "message": "generator manifest query failed",
            }
        )
        self.assertEqual(stage, "preflight_helpers")
        self.assertIn("small/lower_bound", label)
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
            {"generator": 0, "validator": 0, "brute": 0, "reference": 0},
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


if __name__ == "__main__":
    unittest.main()
