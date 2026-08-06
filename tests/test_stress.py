from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools.acm_agent.stress import (
    BundleConflictError,
    HelperBundleManager,
    HelperPreflightConfig,
    HelperPreflightError,
    HelperSources,
    GeneratorCapabilityError,
    LayeredStressRunner,
    SampleCase,
    SandboxCapability,
    SandboxLimits,
    SandboxProcessResult,
    SandboxUnavailableError,
    SourceSafetyError,
    StopToken,
    StressExecutables,
    StressError,
    StressRunConfig,
    WindowsAppContainerBackend,
    classify_three_way,
    compose_trusted_generator_harness,
    probe_generator_v2,
    validate_cpp_source,
    _parse_validator_observation,
)


SAFE_SOURCE = "#include <iostream>\nint main(){return 0;}\n"


GENERATOR_CAPABILITIES = {
    "profile_version": 2,
    "manifest_version": 1,
    "profiles": ["small", "large"],
    "case_kinds": ["lower_bound", "upper_bound", "random"],
    "supported_cases": [
        {"profile": "small", "case_kind": "lower_bound"},
        {"profile": "small", "case_kind": "random"},
        {"profile": "large", "case_kind": "upper_bound"},
        {"profile": "large", "case_kind": "random"},
    ],
}


def generator_manifest(
    seed: int,
    profile: str,
    case_kind: str,
    generated: bytes,
    *,
    coverage_tags: list[str] | None = None,
    total_complexity: str = "linear_output",
) -> dict[str, object]:
    return {
        "manifest_version": 1,
        "profile": profile,
        "case_kind": case_kind,
        "seed": seed,
        "input_sha256": hashlib.sha256(generated).hexdigest(),
        "dimensions": {"records": 1},
        "coverage_tags": coverage_tags or [f"{profile}/{case_kind}"],
        "records": 1,
        "total_complexity": total_complexity,
    }


class FakeSandbox:
    def __init__(
        self,
        handler=None,
        *,
        available: bool = True,
        generator_v2: bool = True,
        capabilities=None,
        auto_manifest: bool = True,
        manifest_factory=None,
    ) -> None:
        self.handler = handler
        self.available = available
        self.generator_v2 = generator_v2
        self.capabilities = capabilities
        self.auto_manifest = auto_manifest
        self.manifest_factory = manifest_factory
        self.calls: list[tuple[list[str], bytes | None, dict[str, str]]] = []
        self.cancelled = False
        self.generated_outputs: dict[tuple[int, str, str], bytes] = {}

    def probe(self) -> SandboxCapability:
        return SandboxCapability(self.available, "disabled for test" if not self.available else "", "fake")

    def run(
        self,
        command,
        *,
        cwd: Path,
        input_data: bytes | None = None,
        env=None,
        limits: SandboxLimits | None = None,
    ) -> SandboxProcessResult:
        argv = [str(item) for item in command]
        environment = dict(env or {})
        self.calls.append((argv, input_data, environment))
        if len(argv) == 2 and argv[1] == "--capabilities":
            if self.generator_v2:
                return SandboxProcessResult(
                    argv,
                    0,
                    json.dumps(
                        self.capabilities or GENERATOR_CAPABILITIES,
                        separators=(",", ":"),
                    ).encode(),
                )
            return SandboxProcessResult(argv, 0, b"{}")
        generator = Path(argv[0]).stem.split(".")[0] == "generator"
        if (
            generator
            and len(argv) == 5
            and argv[1] == "--manifest"
            and self.auto_manifest
        ):
            seed = int(argv[2])
            profile = argv[3]
            case_kind = argv[4]
            generated = self.generated_outputs[(seed, profile, case_kind)]
            if self.manifest_factory is not None:
                manifest = self.manifest_factory(
                    seed, profile, case_kind, generated
                )
                if isinstance(manifest, SandboxProcessResult):
                    return manifest
                payload = (
                    manifest
                    if isinstance(manifest, bytes)
                    else json.dumps(manifest, separators=(",", ":")).encode()
                )
            else:
                payload = json.dumps(
                    generator_manifest(seed, profile, case_kind, generated),
                    separators=(",", ":"),
                ).encode()
            return SandboxProcessResult(argv, 0, payload)
        if self.handler:
            result = self.handler(argv, input_data, environment, limits)
        elif generator:
            profile = environment.get("ACM_STRESS_PROFILE", "small")
            case_kind = environment.get("ACM_STRESS_CASE_KIND", "random")
            seed = environment.get("ACM_STRESS_SEED", "0")
            result = SandboxProcessResult(
                argv, 0, f"{profile}:{case_kind}:{seed}\n".encode()
            )
        elif Path(argv[0]).stem.split(".")[0] in {"brute", "reference"}:
            result = SandboxProcessResult(argv, 0, b"ok\n")
        else:
            result = SandboxProcessResult(argv, 0)
        if generator and len(argv) == 4 and result.ok:
            self.generated_outputs[
                (int(argv[1]), str(argv[2]), str(argv[3]))
            ] = result.stdout
        return result

    def cancel(self) -> None:
        self.cancelled = True


class SourceSafetyTests(unittest.TestCase):
    def test_trusted_harness_invokes_generated_adapter_without_model_main(self) -> None:
        adapter = (
            "#include <iostream>\n#include <string>\n"
            "void acm_generate_case(unsigned long long,const std::string&,"
            "const std::string&,std::ostream&){}\n"
        )
        composed = compose_trusted_generator_harness(adapter)
        self.assertIn("ACM_TRUSTED_GENERATOR_HARNESS_V2", composed)
        self.assertIn("acm_generate_case(seed", composed)
        self.assertNotIn("#define main acm_generated_main", composed)

    def test_accepts_competitive_programming_subset(self) -> None:
        source = "#include <bits/stdc++.h>\nusing namespace std;\nint main(){vector<int>a;cout<<a.size();}\n"
        self.assertEqual(validate_cpp_source(source), source)

    def test_accepts_safe_standard_adapter_headers(self) -> None:
        source = (
            "#include <ostream>\n#include <string>\n#include <list>\n"
            "void acm_generate_case(unsigned long long,const std::string&,"
            "const std::string&,std::ostream&){}\n"
        )
        self.assertEqual(validate_cpp_source(source), source)

    def test_rejects_files_network_process_and_asm(self) -> None:
        rejected = [
            '#include "mine.h"\nint main(){}',
            "#include <fstream>\nint main(){}",
            "int main(){system(\"whoami\");}",
            "int main(){socket(0,0,0);}",
            "int main(){fopen(\"x\",\"w\");}",
            "int main(){asm(\"nop\");}",
            "#pragma comment(lib, \"x\")\nint main(){}",
            "#include <windows.h>\nint main(){}",
        ]
        for source in rejected:
            with self.subTest(source=source), self.assertRaises(SourceSafetyError):
                validate_cpp_source(source)

    def test_rejects_nul_invalid_utf8_and_oversized_source(self) -> None:
        for source in (b"int main(){}\0", b"\xff", b"x" * (256 * 1024 + 1)):
            with self.subTest(length=len(source)), self.assertRaises(SourceSafetyError):
                validate_cpp_source(source)

    def test_native_backend_fails_closed_without_launcher(self) -> None:
        backend = WindowsAppContainerBackend()
        capability = backend.probe()
        self.assertFalse(capability.available)
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(SandboxUnavailableError):
            backend.run(["untrusted.exe"], cwd=Path(temp))

    def test_native_backend_rejects_oversized_input_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backend = WindowsAppContainerBackend(launcher=Path(temp) / "missing.exe")
            backend.probe = lambda: SandboxCapability(True, backend="fixture")
            with self.assertRaises(StressError):
                backend.run(
                    ["untrusted.exe"],
                    cwd=Path(temp),
                    input_data=b"x" * (2 * 1024 * 1024 + 1),
                )

    def test_native_backend_closes_cancel_race_after_process_launch(self) -> None:
        class RacingProcess:
            def __init__(self) -> None:
                self.returncode: int | None = None
                self.killed = False

            def poll(self):
                return self.returncode

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            def communicate(self, timeout=None):
                return b"", b""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend = WindowsAppContainerBackend(launcher=root / "runner.exe")
            backend.probe = lambda: SandboxCapability(True, backend="fixture")
            process = RacingProcess()

            def launch(*args, **kwargs):
                # This is the previously lost interleaving: cancellation lands
                # after Popen returns but before run publishes _active.
                backend.cancel()
                return process

            with mock.patch("tools.acm_agent.stress.subprocess.Popen", side_effect=launch):
                result = backend.run(["untrusted.exe"], cwd=root)

            self.assertTrue(process.killed)
            self.assertEqual(result.returncode, 130)


class HelperBundleTests(unittest.TestCase):
    def test_validator_observation_canonicalizes_duplicate_coverage_tags(self) -> None:
        observation = _parse_validator_observation(
            b'{"valid":true,"dimensions":{"n":5},"coverage_tags":["c4_max","c4_max","op"],"records":8}'
        )
        self.assertEqual(observation["coverage_tags"], ["c4_max", "op"])

    def test_hidden_validator_probe_rejects_deleted_state_precondition(self) -> None:
        probe = {
            "id": "move_above_top",
            "constraint_id": "c_state",
            "valid_input": "2 1\n1 2\nMove 1 0\n",
            "invalid_input": "2 1\n1 2\nMove 1 -1\n",
        }

        def strict_handler(argv, input_data, env, limits):
            valid = b"Move 1 -1" not in (input_data or b"")
            observation = (
                {
                    "valid": True,
                    "dimensions": {"n": 2},
                    "coverage_tags": [],
                    "records": 1,
                }
                if valid
                else {
                    "valid": False,
                    "dimensions": {},
                    "coverage_tags": [],
                    "records": 0,
                }
            )
            return SandboxProcessResult(
                argv, 0, json.dumps(observation, separators=(",", ":")).encode()
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = HelperBundleManager(root, FakeSandbox(strict_handler))
            result = manager._preflight_validator_probes(
                "validator",
                cwd=root,
                probes=[probe],
                seed=17,
                timeout=1.0,
            )
            self.assertTrue(result[0]["invalid_rejected"])

            def permissive_handler(argv, input_data, env, limits):
                return SandboxProcessResult(
                    argv,
                    0,
                    b'{"valid":true,"dimensions":{"n":2},"coverage_tags":[],"records":1}',
                )

            weak = HelperBundleManager(root, FakeSandbox(permissive_handler))
            with self.assertRaises(HelperPreflightError) as raised:
                weak._preflight_validator_probes(
                    "validator",
                    cwd=root,
                    probes=[probe],
                    seed=17,
                    timeout=1.0,
                )
            self.assertEqual(
                raised.exception.code, "stress_validator_negative_probe_failed"
            )

            def reversed_handler(argv, input_data, env, limits):
                accepts = b"Move 1 -1" in (input_data or b"")
                observation = {
                    "valid": accepts,
                    "dimensions": {"n": 2} if accepts else {},
                    "coverage_tags": [],
                    "records": 1 if accepts else 0,
                }
                return SandboxProcessResult(
                    argv, 0, json.dumps(observation, separators=(",", ":")).encode()
                )

            reversed_validator = HelperBundleManager(
                root, FakeSandbox(reversed_handler)
            )
            with self.assertRaises(HelperPreflightError) as reversed_error:
                reversed_validator._preflight_validator_probes(
                    "validator",
                    cwd=root,
                    probes=[probe],
                    seed=17,
                    timeout=1.0,
                )
            self.assertEqual(
                reversed_error.exception.code,
                "stress_validator_positive_probe_failed",
            )
            self.assertEqual(reversed_error.exception.case_kind, "hidden:valid")
            actual = reversed_error.exception.details["actual"]
            self.assertFalse(actual["valid_accepted"])
            self.assertTrue(actual["invalid_accepted"])
            self.assertNotIn("probe_input_excerpt", actual)
            self.assertNotIn("generated_input_excerpt", actual)

    def _workspace(self, root: Path) -> Path:
        source = root / "2026" / "8" / "5" / "CF1A.cpp"
        source.parent.mkdir(parents=True)
        source.write_text(SAFE_SOURCE, encoding="utf-8")
        return source

    def test_stage_apply_backup_and_revert(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            old_generator = primary.with_name("CF1A.gen.cpp")
            old_generator.write_text("// old generator\n", encoding="utf-8")
            backend = FakeSandbox()
            bundle = HelperBundleManager(root, backend).stage_and_apply(
                primary,
                HelperSources(
                    generator=SAFE_SOURCE.replace("return 0", "return 1"),
                    brute=SAFE_SOURCE.replace("return 0", "return 2"),
                    reference=SAFE_SOURCE.replace("return 0", "return 3"),
                ),
            )
            # Compilation stays trusted/local, while staged executables must
            # pass the sandboxed capability and oracle preflight before apply.
            self.assertTrue(backend.calls)
            self.assertIn("return 1", old_generator.read_text(encoding="utf-8"))
            self.assertTrue(primary.with_name("CF1A.bf.cpp").is_file())
            self.assertTrue(Path(bundle.backup_dir, "manifest.json").is_file())

            HelperBundleManager(root, backend).revert(bundle)
            self.assertEqual(old_generator.read_text(encoding="utf-8"), "// old generator\n")
            self.assertFalse(primary.with_name("CF1A.bf.cpp").exists())
            self.assertFalse(primary.with_name("CF1A.ref.cpp").exists())

    def test_apply_requires_sandbox_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            with self.assertRaises(SandboxUnavailableError):
                HelperBundleManager(root, FakeSandbox(available=False)).stage_and_apply(
                    primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
                )
            self.assertFalse(primary.with_name("CF1A.gen.cpp").exists())

    def test_revert_refuses_to_overwrite_later_user_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(root, FakeSandbox())
            bundle = manager.stage_and_apply(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            primary.with_name("CF1A.bf.cpp").write_text("// user edit\n", encoding="utf-8")
            with self.assertRaises(BundleConflictError):
                manager.revert(bundle)
            self.assertEqual(
                primary.with_name("CF1A.bf.cpp").read_text(encoding="utf-8"),
                "// user edit\n",
            )

    def test_rejects_unsafe_source_before_any_compile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            backend = FakeSandbox()
            with self.assertRaises(HelperPreflightError) as raised:
                HelperBundleManager(root, backend).stage_and_apply(
                    primary,
                    HelperSources('int main(){system("x");}', SAFE_SOURCE, SAFE_SOURCE),
                )
            self.assertEqual(raised.exception.artifact, "generator")
            self.assertEqual(raised.exception.case_kind, "source_safety")
            self.assertEqual(backend.calls, [])

    def test_stage_does_not_change_helpers_until_preflight_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            old = primary.with_name("CF1A.gen.cpp")
            old.write_text("// old generator\n", encoding="utf-8")
            manager = HelperBundleManager(root, FakeSandbox())
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            self.assertEqual(old.read_text(encoding="utf-8"), "// old generator\n")
            with self.assertRaisesRegex(StressError, "must pass preflight"):
                manager.apply(staged)
            manager.preflight(
                staged, HelperPreflightConfig(contract_hash="contract")
            )
            self.assertEqual(old.read_text(encoding="utf-8"), "// old generator\n")
            manager.apply(staged)
            applied = old.read_text(encoding="utf-8")
            self.assertIn("ACM_TRUSTED_GENERATOR_HARNESS_V2", applied)
            self.assertIn("acm_generated_main", applied)

    def test_discard_removes_only_unapplied_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            old = primary.with_name("CF1A.gen.cpp")
            old.write_text("// keep me\n", encoding="utf-8")
            manager = HelperBundleManager(root, FakeSandbox())
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            staging = Path(staged.staging_dir)
            self.assertTrue(staging.is_dir())
            manager.discard(staged)
            self.assertFalse(staging.exists())
            self.assertEqual(old.read_text(encoding="utf-8"), "// keep me\n")

    def test_preflight_runs_fixed_boundary_and_random_sequence_without_solution(self) -> None:
        observed: list[tuple[str, str, int]] = []

        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"sanitizer unavailable")
            if stem.startswith("generator"):
                if argv[1:] == ["--capabilities"]:
                    return SandboxProcessResult(
                        argv, 0,
                        b'{"profile_version":2,"profiles":["small","large"],'
                        b'"case_kinds":["lower_bound","upper_bound","random"]}',
                    )
                observed.append(
                    (env["ACM_STRESS_PROFILE"], env["ACM_STRESS_CASE_KIND"], int(env["ACM_STRESS_SEED"]))
                )
                payload = (
                    f"{env['ACM_STRESS_PROFILE']}:{env['ACM_STRESS_CASE_KIND']}:"
                    f"{env['ACM_STRESS_SEED']}\n"
                ).encode()
                return SandboxProcessResult(argv, 0, payload)
            return SandboxProcessResult(argv, 0, b"ok\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            backend = FakeSandbox(handler)
            manager = HelperBundleManager(root, backend)
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            validation = manager.preflight(
                staged,
                HelperPreflightConfig(
                    contract_hash="contract",
                    samples=[SampleCase("official", b"sample input\n", b"ok\n")],
                    small_random_cases=16,
                ),
            )
            # Two deterministic-generator probes precede the actual cases.
            actual = observed[2:]
            self.assertEqual(actual[0][:2], ("small", "lower_bound"))
            self.assertEqual(
                [item[:2] for item in actual[1:17]],
                [("small", "random")] * 16,
            )
            self.assertEqual(actual[17][:2], ("large", "upper_bound"))
            self.assertEqual(actual[18][:2], ("large", "random"))
            executed_names = [Path(call[0][0]).stem for call in backend.calls]
            self.assertFalse(any(name.startswith("solution") for name in executed_names))
            sample_call = next(
                index for index, call in enumerate(backend.calls)
                if call[1] == b"sample input\n"
            )
            lower_call = next(
                index for index, call in enumerate(backend.calls)
                if call[2].get("ACM_STRESS_CASE_KIND") == "lower_bound"
            )
            self.assertLess(sample_call, lower_call)
            self.assertEqual(validation["small_random_cases"], 16)

    def test_preflight_manifest_coverage_is_bound_to_generated_inputs(self) -> None:
        def manifest_factory(seed, profile, case_kind, generated):
            tags = {
                ("small", "lower_bound"): ["minimum_dimensions"],
                ("small", "random"): ["duplicate_values", "mixed_operations"],
                ("large", "upper_bound"): ["maximum_dimensions"],
                ("large", "random"): ["adversarial_large"],
            }[(profile, case_kind)]
            return generator_manifest(
                seed,
                profile,
                case_kind,
                generated,
                coverage_tags=tags,
                total_complexity=(
                    "output_log_n"
                    if (profile, case_kind) == ("large", "random")
                    else "linear_output"
                ),
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            backend = FakeSandbox(manifest_factory=manifest_factory)
            manager = HelperBundleManager(root, backend)
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            validation = manager.preflight(
                staged,
                HelperPreflightConfig(
                    contract_hash="manifest-coverage",
                    generator_blueprint={
                        "required_coverage_tags": [
                            "duplicate_values",
                            "mixed_operations",
                        ],
                        "large_required_coverage_tags": [
                            "maximum_dimensions",
                            "adversarial_large",
                        ],
                    },
                ),
            )

            self.assertEqual(validation["preflight_version"], 5)
            self.assertEqual(validation["generator_manifest_version"], 1)
            self.assertEqual(
                validation["generator_coverage"]["small_random"]["observed_tags"],
                ["duplicate_values", "mixed_operations"],
            )
            self.assertEqual(
                validation["generator_coverage"]["large"]["observed_tags"],
                ["adversarial_large", "maximum_dimensions"],
            )
            generated_records = [
                record
                for record in validation["cases"]
                if record["profile"] in {"small", "large"}
            ]
            self.assertEqual(len(generated_records), 19)
            self.assertTrue(
                all("generator_manifest" in record for record in generated_records)
            )
            manifest_calls = [
                call[0]
                for call in backend.calls
                if len(call[0]) == 5 and call[0][1] == "--manifest"
            ]
            self.assertEqual(len(manifest_calls), 19)
            self.assertTrue(
                all(
                    argv[2].isdigit()
                    and argv[3] in {"small", "large"}
                    and argv[4] in {"lower_bound", "upper_bound", "random"}
                    for argv in manifest_calls
                )
            )

    def test_independent_validator_owns_manifest_observation(self) -> None:
        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem.split(".")[0]
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"unavailable")
            if stem == "generator":
                return SandboxProcessResult(
                    argv, 0,
                    f"{env.get('ACM_STRESS_PROFILE')}:{env.get('ACM_STRESS_CASE_KIND')}:{env.get('ACM_STRESS_SEED')}\n".encode(),
                )
            if stem == "validator":
                profile = env.get("ACM_STRESS_PROFILE", "small")
                case_kind = env.get("ACM_STRESS_CASE_KIND", "random")
                tags = ["small-required"] if profile == "small" else ["large-required"]
                return SandboxProcessResult(
                    argv, 0,
                    json.dumps(
                        {
                            "valid": True,
                            "dimensions": {"bytes": len(input_data or b"")},
                            "coverage_tags": tags,
                            "records": 1,
                        },
                        separators=(",", ":"),
                    ).encode(),
                )
            return SandboxProcessResult(argv, 0, b"ok\n")

        blueprint = {
            "required_coverage_tags": ["small-required"],
            "large_required_coverage_tags": ["large-required"],
            "cases": [
                {
                    "profile": profile,
                    "case_kind": kind,
                    "total_complexity": "O(output_size)",
                }
                for profile, kind in (
                    ("small", "lower_bound"),
                    ("small", "random"),
                    ("large", "upper_bound"),
                    ("large", "random"),
                )
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            backend = FakeSandbox(handler=handler)
            manager = HelperBundleManager(root, backend)
            staged = manager.stage(
                primary,
                HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE),
            )
            validation = manager.preflight(
                staged,
                HelperPreflightConfig(
                    contract_hash="independent-validator",
                    generator_blueprint=blueprint,
                ),
            )
            self.assertTrue(validation["independent_input_validator"])
            self.assertFalse(
                any(len(argv) > 1 and argv[1] == "--manifest" for argv, _, _ in backend.calls)
            )
            self.assertTrue(
                any(Path(argv[0]).stem.startswith("validator") for argv, _, _ in backend.calls)
            )

    def test_preflight_rejects_forged_manifest_hash_with_attribution(self) -> None:
        def forged_manifest(seed, profile, case_kind, generated):
            manifest = generator_manifest(seed, profile, case_kind, generated)
            manifest["input_sha256"] = "0" * 64
            return manifest

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(
                root, FakeSandbox(manifest_factory=forged_manifest)
            )
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.preflight(
                    staged,
                    HelperPreflightConfig(
                        contract_hash="forged-manifest", include_large=False
                    ),
                )
            error = raised.exception
            self.assertEqual(error.code, "stress_generator_coverage_failed")
            self.assertEqual(error.artifact, "generator")
            self.assertEqual((error.profile, error.case_kind), ("small", "lower_bound"))
            self.assertEqual(error.details["actual"], {"input_sha256": "0" * 64})
            self.assertIn("input_sha256", error.details["expected"])

    def test_preflight_rejects_manifest_with_missing_field(self) -> None:
        def incomplete_manifest(seed, profile, case_kind, generated):
            manifest = generator_manifest(seed, profile, case_kind, generated)
            manifest.pop("records")
            return manifest

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(
                root, FakeSandbox(manifest_factory=incomplete_manifest)
            )
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.preflight(
                    staged,
                    HelperPreflightConfig(
                        contract_hash="missing-manifest-field", include_large=False
                    ),
                )
            error = raised.exception
            self.assertEqual(error.code, "stress_generator_coverage_failed")
            self.assertEqual((error.profile, error.case_kind), ("small", "lower_bound"))
            self.assertIn("records", error.details["expected"])
            self.assertNotIn("records", error.details["actual"])

    def test_preflight_rejects_missing_blueprint_coverage_with_union_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(root, FakeSandbox())
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.preflight(
                    staged,
                    HelperPreflightConfig(
                        contract_hash="missing-coverage",
                        include_large=False,
                        generator_blueprint={
                            "required_coverage_tags": ["must_cover_duplicates"]
                        },
                    ),
                )
            error = raised.exception
            self.assertEqual(error.code, "stress_generator_coverage_failed")
            self.assertEqual((error.artifact, error.profile, error.case_kind), (
                "generator", "small", "random"
            ))
            self.assertEqual(
                error.details["expected"],
                {"required_coverage_tags": ["must_cover_duplicates"]},
            )
            self.assertEqual(
                error.details["actual"]["missing_coverage_tags"],
                ["must_cover_duplicates"],
            )

    def test_preflight_rejects_non_linear_large_upper_manifest(self) -> None:
        def unsafe_manifest(seed, profile, case_kind, generated):
            return generator_manifest(
                seed,
                profile,
                case_kind,
                generated,
                total_complexity=(
                    "output_log_n"
                    if (profile, case_kind) == ("large", "upper_bound")
                    else "linear_output"
                ),
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(
                root, FakeSandbox(manifest_factory=unsafe_manifest)
            )
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.preflight(
                    staged, HelperPreflightConfig(contract_hash="unsafe-large")
                )
            error = raised.exception
            self.assertEqual(error.code, "stress_generator_coverage_failed")
            self.assertEqual((error.profile, error.case_kind), ("large", "upper_bound"))
            self.assertEqual(
                error.details["expected"], {"total_complexity": ["linear_output"]}
            )
            self.assertEqual(
                error.details["actual"], {"total_complexity": "output_log_n"}
            )

    def test_preflight_rejects_generator_that_ignores_random_seed(self) -> None:
        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"sanitizer unavailable")
            if stem.startswith("generator"):
                if argv[1:] == ["--capabilities"]:
                    return SandboxProcessResult(
                        argv, 0,
                        b'{"profile_version":2,"profiles":["small","large"],'
                        b'"case_kinds":["lower_bound","upper_bound","random"]}',
                    )
                return SandboxProcessResult(argv, 0, b"fixed random case\n")
            return SandboxProcessResult(argv, 0, b"ok\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            old = primary.with_name("P1000.gen.cpp")
            old.write_text("// existing helper\n", encoding="utf-8")
            manager = HelperBundleManager(root, FakeSandbox(handler))
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.preflight(
                    staged,
                    HelperPreflightConfig(contract_hash="contract", include_large=False),
                )
            self.assertEqual(raised.exception.artifact, "generator")
            self.assertEqual(raised.exception.profile, "small")
            self.assertEqual(raised.exception.case_kind, "random")
            self.assertIn("ignores the seed", str(raised.exception))
            self.assertEqual(old.read_text(encoding="utf-8"), "// existing helper\n")

    def test_preflight_allows_adjacent_seed_collision_when_window_varies(self) -> None:
        first_random_seed: list[int] = []

        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"unavailable")
            if stem.startswith("generator"):
                seed = int(env["ACM_STRESS_SEED"])
                case_kind = env["ACM_STRESS_CASE_KIND"]
                if case_kind == "random":
                    if not first_random_seed:
                        first_random_seed.append(seed)
                    # The determinism probe and the first two preflight cases
                    # intentionally collide; a later seed proves sensitivity.
                    bucket = b"A\n" if seed <= first_random_seed[0] + 2 else b"B\n"
                    return SandboxProcessResult(argv, 0, bucket)
                return SandboxProcessResult(argv, 0, f"{case_kind}\n".encode())
            return SandboxProcessResult(argv, 0, b"ok\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(root, FakeSandbox(handler))
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            validation = manager.preflight(
                staged,
                HelperPreflightConfig(contract_hash="seed-window", include_large=False),
            )
            self.assertTrue(validation["deterministic_generator"])

    def test_preflight_rejects_requested_seed_instead_of_substituting(self) -> None:
        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"unavailable")
            if stem.startswith("generator"):
                seed = int(env["ACM_STRESS_SEED"])
                return SandboxProcessResult(argv, 0, f"{seed}\n".encode())
            if stem.startswith("validator"):
                seed = int((input_data or b"0").strip() or b"0")
                observation = (
                    {"valid": False, "dimensions": {}, "coverage_tags": [], "records": 0}
                    if seed % 4 == 0
                    else {
                        "valid": True,
                        "dimensions": {"records": 1},
                        "coverage_tags": [],
                        "records": 1,
                    }
                )
                return SandboxProcessResult(
                    argv, 0, json.dumps(observation, separators=(",", ":")).encode()
                )
            return SandboxProcessResult(argv, 0, b"ok\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(root, FakeSandbox(handler))
            staged = manager.stage(
                primary,
                HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE),
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.preflight(
                    staged,
                    HelperPreflightConfig(
                        contract_hash="seed-search", include_large=False
                    ),
                )
            self.assertEqual(raised.exception.code, "stress_generated_input_invalid")
            self.assertEqual(raised.exception.details["seed_search"]["attempts"], 1)

    def test_preflight_never_deletes_records_to_make_generated_input_valid(self) -> None:
        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem.split(".")[0]
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"unavailable")
            if stem == "generator":
                seed = int(env["ACM_STRESS_SEED"])
                if env["ACM_STRESS_CASE_KIND"] == "random":
                    payload = (
                        f"3 3\n1 2 3\nAsk {seed % 3 + 1}\nBottom 3\nQuery 1\n"
                    )
                else:
                    payload = "1 1\n1\nAsk 1\n"
                return SandboxProcessResult(argv, 0, payload.encode())
            if stem == "validator":
                text = (input_data or b"").decode()
                lines = text.splitlines()
                declared = int(lines[0].split()[-1]) if lines else -1
                actual = max(0, len(lines) - 2)
                valid = declared == actual and not any(
                    line.startswith("Bottom") for line in lines[2:]
                )
                observation = (
                    {
                        "valid": True,
                        "dimensions": {"records": actual},
                        "coverage_tags": [],
                        "records": actual,
                    }
                    if valid
                    else {
                        "valid": False,
                        "dimensions": {},
                        "coverage_tags": [],
                        "records": 0,
                    }
                )
                return SandboxProcessResult(
                    argv,
                    0,
                    json.dumps(observation, separators=(",", ":")).encode(),
                    b"" if valid else b"ERR_BOTTOM_ALREADY_BOTTOM 1",
                )
            return SandboxProcessResult(argv, 0, b"ok\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(root, FakeSandbox(handler))
            staged = manager.stage(
                primary,
                HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE),
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.preflight(
                    staged,
                    HelperPreflightConfig(
                        contract_hash="validator-guided-sanitizer", include_large=False
                    ),
                )
            self.assertEqual(raised.exception.code, "stress_generated_input_invalid")

    def test_qualification_never_edits_validator_rejected_operation(self) -> None:
        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem.split(".")[0]
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"unavailable")
            if stem == "generator":
                seed = int(env["ACM_STRESS_SEED"])
                if env["ACM_STRESS_CASE_KIND"] == "random":
                    payload = f"3 3\n1 2 3\nAsk {seed % 3 + 1}\nInsert 3 1\nQuery 1\n"
                else:
                    payload = "3 3\n1 2 3\nAsk 1\nQuery 1\nTop 1\n"
                return SandboxProcessResult(argv, 0, payload.encode())
            if stem == "validator":
                text = (input_data or b"").decode()
                invalid = "Insert 3 1" in text
                tags = ["auto_operation_insert"] if "Insert" in text else []
                observation = (
                    {"valid": False, "dimensions": {}, "coverage_tags": [], "records": 0}
                    if invalid
                    else {
                        "valid": True,
                        "dimensions": {"n": 3, "m": 3},
                        "coverage_tags": tags,
                        "records": 3,
                    }
                )
                return SandboxProcessResult(
                    argv,
                    0,
                    json.dumps(observation, separators=(",", ":")).encode(),
                    b"ERR_INSERT_POS 1" if invalid else b"",
                )
            return SandboxProcessResult(argv, 0, b"ok\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = HelperBundleManager(root, FakeSandbox(handler))
            staged = manager.stage(
                self._workspace(root),
                HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE),
            )
            config = HelperPreflightConfig(
                contract_hash="validator-guided-value-edit",
                include_large=False,
                generator_blueprint={
                    "required_coverage_tags": ["auto_operation_insert"]
                },
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.qualify(staged, config)
            self.assertEqual(raised.exception.code, "stress_generated_input_invalid")

    def test_independent_validator_rejection_is_attributed_to_generator(self) -> None:
        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem.split(".")[0]
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"unavailable")
            if stem == "generator":
                return SandboxProcessResult(
                    argv, 0,
                    f"{env['ACM_STRESS_PROFILE']}:{env['ACM_STRESS_CASE_KIND']}:"
                    f"{env['ACM_STRESS_SEED']}\n".encode(),
                )
            if stem == "validator":
                return SandboxProcessResult(
                    argv,
                    0,
                    b'{"valid":false,"dimensions":{},"coverage_tags":[],"records":0}',
                    b"ERR_SEMANTIC",
                )
            return SandboxProcessResult(argv, 0, b"ok\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(root, FakeSandbox(handler))
            staged = manager.stage(
                primary,
                HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE),
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.qualify(
                    staged,
                    HelperPreflightConfig(contract_hash="validator-reject"),
                )
            self.assertEqual(raised.exception.artifact, "generator")
            self.assertEqual(
                raised.exception.code, "stress_generated_input_invalid"
            )
            self.assertEqual(raised.exception.details["stderr"], "ERR_SEMANTIC")

    def test_qualification_runs_real_large_random_smoke_before_audit(self) -> None:
        generated_cases: list[tuple[str, str]] = []

        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem.split(".")[0]
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"unavailable")
            if stem == "generator":
                if argv[1:] == ["--capabilities"]:
                    return SandboxProcessResult(
                        argv,
                        0,
                        b'{"profile_version":2,"profiles":["small","large"],'
                        b'"case_kinds":["lower_bound","upper_bound","random"]}',
                    )
                profile = env["ACM_STRESS_PROFILE"]
                case_kind = env["ACM_STRESS_CASE_KIND"]
                generated_cases.append((profile, case_kind))
                seed = env["ACM_STRESS_SEED"]
                return SandboxProcessResult(
                    argv, 0, f"{profile} {case_kind} {seed}\n".encode()
                )
            if stem == "validator":
                return SandboxProcessResult(
                    argv,
                    0,
                    b'{"valid":true,"dimensions":{},"coverage_tags":[],"records":1}',
                )
            return SandboxProcessResult(argv, 0, b"ok\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = HelperBundleManager(root, FakeSandbox(handler))
            staged = manager.stage(
                self._workspace(root),
                HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE),
            )
            result = manager.qualify(
                staged,
                HelperPreflightConfig(
                    contract_hash="large-smoke-before-audit",
                    include_large=True,
                ),
            )
            self.assertTrue(result["large_smoke"])
            self.assertIn(("large", "random"), generated_cases)

    def test_parallel_small_preflight_uses_four_independent_sandboxes(self) -> None:
        created: list[int] = []
        used: set[int] = set()
        lock = threading.Lock()

        def response(worker_id, argv, input_data, env, limits):
            stem = Path(argv[0]).stem
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"unavailable")
            if stem.startswith("generator"):
                if argv[1:] == ["--capabilities"]:
                    return SandboxProcessResult(
                        argv,
                        0,
                        b'{"profile_version":2,"profiles":["small","large"],'
                        b'"case_kinds":["lower_bound","upper_bound","random"]}',
                    )
                if env.get("ACM_STRESS_CASE_KIND") == "random" and worker_id:
                    with lock:
                        used.add(worker_id)
                    time.sleep(0.02)
                return SandboxProcessResult(
                    argv,
                    0,
                    f"{env.get('ACM_STRESS_PROFILE')}:{env.get('ACM_STRESS_CASE_KIND')}:"
                    f"{env.get('ACM_STRESS_SEED')}\n".encode(),
                )
            return SandboxProcessResult(argv, 0, b"ok\n")

        initial = FakeSandbox(
            lambda argv, input_data, env, limits: response(
                0, argv, input_data, env, limits
            )
        )

        def factory():
            with lock:
                worker_id = len(created) + 1
                created.append(worker_id)
            return FakeSandbox(
                lambda argv, input_data, env, limits, worker_id=worker_id: response(
                    worker_id, argv, input_data, env, limits
                )
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(
                root, initial, sandbox_factory=factory
            )
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            validation = manager.preflight(
                staged,
                HelperPreflightConfig(
                    contract_hash="parallel-sandboxes", include_large=False
                ),
            )
            self.assertEqual(validation["small_random_cases"], 16)
            self.assertEqual(len(created), 4)
            self.assertEqual(used, set(created))
            self.assertFalse(initial.cancelled)

    def test_p2596_parallel_array_oob_is_rejected_before_old_helper_changes(self) -> None:
        buggy_generator = r'''#include <bits/stdc++.h>
using namespace std;
int main(int argc,char**argv){
 if(argc>1 && string(argv[1])=="--capabilities"){
  cout<<R"({"profile_version":2,"profiles":["small","large"],"case_kinds":["lower_bound","upper_bound","random"]})"; return 0;
 }
 vector<string> op={"Top","Insert"}; vector<int> b={3};
 for(size_t i=0;i<op.size();++i) cout<<op[i]<<' '<<b[i]<<'\n';
}'''

        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"sanitizer unavailable")
            if stem.startswith("generator") and argv[1:] == ["--capabilities"]:
                return SandboxProcessResult(
                    argv, 0,
                    b'{"profile_version":2,"profiles":["small","large"],'
                    b'"case_kinds":["lower_bound","upper_bound","random"]}',
                )
            if stem == "generator.audit":
                return SandboxProcessResult(
                    argv, 3, stderr=b"vector::operator[] assertion failed"
                )
            return SandboxProcessResult(argv, 0, b"ok\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            primary = primary.with_name("P2596.cpp")
            primary.write_text(SAFE_SOURCE, encoding="utf-8")
            old = primary.with_name("P2596.gen.cpp")
            old.write_text("// known old helper\n", encoding="utf-8")
            manager = HelperBundleManager(root, FakeSandbox(handler))
            staged = manager.stage(
                primary, HelperSources(buggy_generator, SAFE_SOURCE, SAFE_SOURCE)
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.preflight(
                    staged, HelperPreflightConfig(contract_hash="p2596-contract")
                )
            self.assertEqual(raised.exception.artifact, "generator")
            self.assertEqual(raised.exception.profile, "small")
            self.assertEqual(raised.exception.case_kind, "random")
            self.assertIn("seed", raised.exception.details)
            self.assertEqual(old.read_text(encoding="utf-8"), "// known old helper\n")
            self.assertFalse(primary.with_name("P2596.bf.cpp").exists())
            self.assertFalse(primary.with_name("P2596.ref.cpp").exists())

    def test_illegal_insert_is_rejected_when_debug_brute_aborts(self) -> None:
        def handler(argv, input_data, env, limits):
            stem = Path(argv[0]).stem
            if stem == "sanitizer-probe":
                return SandboxProcessResult(argv, 127, stderr=b"sanitizer unavailable")
            if stem.startswith("generator") and argv[1:] == ["--capabilities"]:
                return SandboxProcessResult(
                    argv, 0,
                    b'{"profile_version":2,"profiles":["small","large"],'
                    b'"case_kinds":["lower_bound","upper_bound","random"]}',
                )
            if stem.startswith("generator"):
                return SandboxProcessResult(argv, 0, b"1 1\n1\nInsert 3 -470992076\n")
            if stem == "brute.audit":
                return SandboxProcessResult(
                    argv, 3, stderr=b"vector::insert invalid iterator assertion"
                )
            return SandboxProcessResult(argv, 0, b"ok\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            primary = self._workspace(root)
            manager = HelperBundleManager(root, FakeSandbox(handler))
            staged = manager.stage(
                primary, HelperSources(SAFE_SOURCE, SAFE_SOURCE, SAFE_SOURCE)
            )
            with self.assertRaises(HelperPreflightError) as raised:
                manager.preflight(
                    staged, HelperPreflightConfig(contract_hash="illegal-insert")
                )
            self.assertEqual(raised.exception.artifact, "brute")
            self.assertEqual(raised.exception.profile, "small")
            self.assertEqual(raised.exception.case_kind, "lower_bound")
            self.assertIn("invalid iterator", raised.exception.details["stderr"])
            self.assertFalse(primary.with_name("CF1A.gen.cpp").exists())


class ClassificationTests(unittest.TestCase):
    def test_three_way_truth_table(self) -> None:
        self.assertEqual(classify_three_way(b"1\n", b"1 ", b"1\n"), "agree")
        self.assertEqual(classify_three_way(b"0", b"1", b"1"), "mismatch")
        self.assertEqual(classify_three_way(b"0", b"0", b"1"), "oracle_conflict")
        self.assertEqual(classify_three_way(b"0", b"1", b"2"), "oracle_conflict")
        self.assertEqual(classify_three_way(b"1\n", b"1 ", b"1\n", exact=True), "oracle_conflict")


class RunnerTests(unittest.TestCase):
    @staticmethod
    def _executables(root: Path) -> StressExecutables:
        executables = StressExecutables(
            solution=root / "solution.exe",
            generator=root / "generator.exe",
            brute=root / "brute.exe",
            reference=root / "reference.exe",
        )
        for path in (
            executables.solution,
            executables.generator,
            executables.brute,
            executables.reference,
        ):
            path.write_bytes(b"test executable")
        return executables

    def test_small_mismatch_saves_reproduction_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem_dir = root / "2026" / "8" / "5"
            problem_dir.mkdir(parents=True)

            def handler(argv, input_data, env, limits):
                name = Path(argv[0]).stem
                if name == "generator":
                    return SandboxProcessResult(argv, 0, f"{env['ACM_STRESS_SEED']}\n".encode())
                if name == "solution":
                    return SandboxProcessResult(argv, 0, b"wrong\n")
                return SandboxProcessResult(argv, 0, b"correct\n")

            backend = FakeSandbox(handler)
            runner = LayeredStressRunner(
                root, "CF1A", self._executables(root), backend,
                source_hashes={"solution": "abc"},
                reference_source={"url": "https://codeforces.com/blog/entry/1"},
                conflict_export_dir=problem_dir,
            )
            result = runner.run(StressRunConfig(first_seed=42, max_cases=10))
            self.assertEqual(result.status, "mismatch")
            self.assertEqual(result.next_seed, 43)
            self.assertEqual(result.small_cases, 1)
            failure = Path(result.failure_dir or "")
            self.assertEqual((failure / "input.txt").read_text(), "42\n")
            self.assertEqual((failure / "solution.stdout.txt").read_text(), "wrong\n")
            self.assertTrue((failure / "metadata.json").is_file())
            self.assertEqual((problem_dir / "CF1A_input.in").read_bytes(), b"42\n")
            self.assertEqual((problem_dir / "CF1A_current.out").read_bytes(), b"wrong\n")
            self.assertEqual((problem_dir / "CF1A_brute.out").read_bytes(), b"correct\n")
            self.assertEqual((problem_dir / "CF1A_reference.out").read_bytes(), b"correct\n")

    def test_oracle_conflict_exports_case_and_all_outputs_beside_solution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem_dir = root / "2026" / "8" / "5"
            problem_dir.mkdir(parents=True)

            def handler(argv, input_data, env, limits):
                name = Path(argv[0]).stem
                if name == "generator":
                    return SandboxProcessResult(argv, 0, b"conflict input\n")
                return SandboxProcessResult(
                    argv,
                    0,
                    {
                        "solution": b"current output\n",
                        "brute": b"brute output\n",
                        "reference": b"reference output\n",
                    }[name],
                )

            result = LayeredStressRunner(
                root,
                "P2596",
                self._executables(root),
                FakeSandbox(handler),
                conflict_export_dir=problem_dir,
            ).run(StressRunConfig(first_seed=19, max_cases=1))
            self.assertEqual(result.status, "oracle_conflict")
            self.assertEqual((problem_dir / "P2596_input.in").read_bytes(), b"conflict input\n")
            self.assertEqual((problem_dir / "P2596_current.out").read_bytes(), b"current output\n")
            self.assertEqual((problem_dir / "P2596_brute.out").read_bytes(), b"brute output\n")
            self.assertEqual((problem_dir / "P2596_reference.out").read_bytes(), b"reference output\n")

    def test_oracle_conflict_does_not_export_when_current_matches_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem_dir = root / "2026" / "8" / "5"
            problem_dir.mkdir(parents=True)

            def handler(argv, input_data, env, limits):
                name = Path(argv[0]).stem
                if name == "generator":
                    return SandboxProcessResult(argv, 0, b"oracle conflict\n")
                return SandboxProcessResult(
                    argv,
                    0,
                    b"different brute\n" if name == "brute" else b"same answer\n",
                )

            result = LayeredStressRunner(
                root,
                "P2596",
                self._executables(root),
                FakeSandbox(handler),
                conflict_export_dir=problem_dir,
            ).run(StressRunConfig(first_seed=20, max_cases=1))
            self.assertEqual(result.status, "oracle_conflict")
            self.assertFalse((problem_dir / "P2596_input.in").exists())
            self.assertFalse((problem_dir / "P2596_current.out").exists())
            self.assertFalse((problem_dir / "P2596_brute.out").exists())
            self.assertFalse((problem_dir / "P2596_reference.out").exists())

    def test_v2_runs_boundaries_then_four_small_to_one_large(self) -> None:
        cases: list[tuple[str, str, tuple[str, ...]]] = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def handler(argv, input_data, env, limits):
                name = Path(argv[0]).stem
                if name == "generator" and argv[1:] == ["--capabilities"]:
                    return SandboxProcessResult(
                        argv,
                        0,
                        b'{"profile_version":2,"profiles":["small","large"],'
                        b'"case_kinds":["lower_bound","upper_bound","random"]}',
                    )
                if name == "generator":
                    cases.append(
                        (
                            env["ACM_STRESS_PROFILE"],
                            env["ACM_STRESS_CASE_KIND"],
                            tuple(argv[1:]),
                        )
                    )
                    return SandboxProcessResult(argv, 0, b"1\n")
                return SandboxProcessResult(argv, 0, b"ok\n")

            result = LayeredStressRunner(
                root, "P1", self._executables(root), FakeSandbox(handler)
            ).run(
                StressRunConfig(
                    first_seed=7,
                    profile_version=2,
                    max_cases=7,
                )
            )
            self.assertEqual(result.status, "limit_reached")
            self.assertEqual(
                [(profile, kind) for profile, kind, _ in cases],
                [
                    ("small", "lower_bound"),
                    ("large", "upper_bound"),
                    ("small", "random"),
                    ("small", "random"),
                    ("small", "random"),
                    ("small", "random"),
                    ("large", "random"),
                ],
            )
            self.assertEqual(cases[0][2], ("7", "small", "lower_bound"))
            self.assertEqual((result.small_cases, result.large_cases), (5, 2))

    def test_v2_schedule_offset_does_not_repeat_boundary_cases(self) -> None:
        observed: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def handler(argv, input_data, env, limits):
                if Path(argv[0]).stem == "generator" and argv[1:] == ["--capabilities"]:
                    return SandboxProcessResult(
                        argv, 0,
                        b'{"profile_version":2,"profiles":["small","large"],'
                        b'"case_kinds":["lower_bound","upper_bound","random"]}',
                    )
                if Path(argv[0]).stem == "generator":
                    observed.append((env["ACM_STRESS_PROFILE"], env["ACM_STRESS_CASE_KIND"]))
                    return SandboxProcessResult(argv, 0, b"1\n")
                return SandboxProcessResult(argv, 0, b"ok\n")

            LayeredStressRunner(
                root, "P1", self._executables(root), FakeSandbox(handler)
            ).run(
                StressRunConfig(
                    first_seed=99,
                    profile_version=2,
                    schedule_offset=6,
                    max_cases=2,
                )
            )
            self.assertEqual(observed, [("large", "random"), ("small", "random")])

    def test_v2_rejects_generator_without_capability_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(GeneratorCapabilityError, "regenerate helpers"):
                LayeredStressRunner(
                    root,
                    "P1",
                    self._executables(root),
                    FakeSandbox(generator_v2=False),
                ).run(StressRunConfig(first_seed=1, profile_version=2, max_cases=1))

    def test_v2_capability_requires_manifest_version_and_exact_case_order(self) -> None:
        for label, mutate in (
            (
                "missing manifest version",
                lambda value: value.pop("manifest_version"),
            ),
            (
                "wrong case order",
                lambda value: value["supported_cases"].reverse(),
            ),
            (
                "extra case",
                lambda value: value["supported_cases"].append(
                    {"profile": "small", "case_kind": "upper_bound"}
                ),
            ),
        ):
            capabilities = json.loads(json.dumps(GENERATOR_CAPABILITIES))
            mutate(capabilities)
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                backend = FakeSandbox(capabilities=capabilities)
                with self.assertRaises(GeneratorCapabilityError) as raised:
                    probe_generator_v2(
                        backend,
                        "generator.exe",
                        cwd=Path(temp),
                    )
                self.assertEqual(
                    raised.exception.details["expected"]["supported_cases"],
                    GENERATOR_CAPABILITIES["supported_cases"],
                )
                self.assertEqual(
                    raised.exception.details["actual"], capabilities
                )

    def test_only_profile_version_two_is_supported(self) -> None:
        with self.assertRaisesRegex(ValueError, "only stress profile version 2"):
            StressRunConfig(first_seed=1, profile_version=1, max_cases=1)

    def test_large_mismatch_skips_brute_and_uses_large_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            problem_dir = root / "2026" / "8" / "5"
            problem_dir.mkdir(parents=True)
            calls: list[tuple[str, int, int, int]] = []

            def handler(argv, input_data, env, limits):
                name = Path(argv[0]).stem
                if name == "generator" and argv[1:] == ["--capabilities"]:
                    return SandboxProcessResult(
                        argv, 0,
                        b'{"profile_version":2,"profiles":["small","large"],'
                        b'"case_kinds":["lower_bound","upper_bound","random"]}',
                    )
                calls.append((name, limits.stdin_bytes, limits.stdout_bytes, limits.stderr_bytes))
                if name == "generator":
                    return SandboxProcessResult(argv, 0, b"large input\n")
                return SandboxProcessResult(
                    argv, 0, b"current\n" if name == "solution" else b"reference\n"
                )

            result = LayeredStressRunner(
                root,
                "P9",
                self._executables(root),
                FakeSandbox(handler),
                conflict_export_dir=problem_dir,
            ).run(
                StressRunConfig(
                    first_seed=3,
                    profile_version=2,
                    schedule_offset=1,
                    max_cases=1,
                )
            )
            self.assertEqual(result.status, "mismatch")
            self.assertNotIn("brute", [name for name, *_ in calls])
            self.assertEqual(calls[0][2], 32 * 1024 * 1024)
            for name, stdin_limit, stdout_limit, stderr_limit in calls[1:]:
                self.assertIn(name, {"solution", "reference"})
                self.assertEqual(stdin_limit, 32 * 1024 * 1024)
                self.assertEqual(stdout_limit, 16 * 1024 * 1024)
                self.assertEqual(stderr_limit, 16 * 1024 * 1024)
            self.assertEqual(
                (problem_dir / "P9_brute.out").read_bytes(),
                b"BRUTE_NOT_RUN_FOR_LARGE_PROFILE\n",
            )
            metadata = json.loads(Path(result.failure_dir, "metadata.json").read_text())
            self.assertEqual(metadata["brute_status"], "skipped_large_profile")
            self.assertEqual(metadata["case_kind"], "upper_bound")

    def test_stop_token_stops_before_execution_and_cancel_delegates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend = FakeSandbox()
            token = StopToken()
            token.request_stop()
            runner = LayeredStressRunner(
                root, "P1", self._executables(root), backend, stop_token=token
            )
            result = runner.run(StressRunConfig(first_seed=9))
            self.assertEqual(result.status, "stopped")
            self.assertEqual(result.next_seed, 9)
            self.assertEqual(backend.calls, [])
            token.reset()
            runner.request_stop()
            self.assertTrue(backend.cancelled)

    def test_stop_during_a_process_is_not_reported_as_runtime_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            token = StopToken()

            def handler(argv, input_data, env, limits):
                token.request_stop()
                return SandboxProcessResult(argv, 127, stderr=b"cancelled")

            result = LayeredStressRunner(
                root,
                "P1",
                self._executables(root),
                FakeSandbox(handler),
                stop_token=token,
            ).run(StressRunConfig(first_seed=9, max_cases=1))
            self.assertEqual(result.status, "stopped")
            self.assertEqual(result.next_seed, 9)
            self.assertIsNone(result.failure_dir)

    def test_samples_run_before_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def handler(argv, input_data, env, limits):
                name = Path(argv[0]).stem
                if name == "solution":
                    return SandboxProcessResult(argv, 0, b"wrong")
                return SandboxProcessResult(argv, 0, b"expected")

            backend = FakeSandbox(handler)
            result = LayeredStressRunner(
                root, "P1", self._executables(root), backend
            ).run(
                StressRunConfig(first_seed=1, max_cases=1),
                samples=[SampleCase("sample1", b"in", b"expected")],
            )
            self.assertEqual(result.status, "sample_mismatch")
            self.assertNotIn("generator", [Path(call[0][0]).stem for call in backend.calls])


if __name__ == "__main__":
    unittest.main()
