"""Compile, sample-test, and stress-test local competitive-programming code."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import time
from typing import Sequence

from .workspace import ProblemRef, find_solution, parse_problem_ref


@dataclass(slots=True)
class CaseResult:
    name: str
    passed: bool
    reason: str
    elapsed_ms: int


@dataclass(slots=True)
class VerifyResult:
    problem_id: str
    source: str
    passed: bool
    compiled: bool
    compile_command: list[str]
    compile_output: str = ""
    sanitizer: str = "not_requested"
    cases: list[CaseResult] = field(default_factory=list)
    stress: str = "not_available"
    stress_iterations: int = 0
    failure_dir: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        return result


def outputs_equal(actual: bytes, expected: bytes, *, exact: bool = False) -> bool:
    """Compare outputs byte-for-byte or by whitespace-delimited tokens."""
    if exact:
        return actual == expected
    return actual.split() == expected.split()


def sanitizer_supported(
    compiler: str,
    build_dir: str | Path,
    *,
    timeout: float = 15.0,
) -> tuple[bool, str]:
    """Probe both sanitizer linking and runtime before enabling debug flags."""
    output = Path(build_dir) / ("sanitizer-probe.exe" if os.name == "nt" else "sanitizer-probe")
    command = [
        compiler,
        "-x",
        "c++",
        "-std=c++17",
        "-fsanitize=address,undefined",
        "-fno-omit-frame-pointer",
        "-o",
        str(output),
        "-",
    ]
    try:
        compiled = subprocess.run(
            command,
            input=b"int main(){return 0;}\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        if compiled.returncode != 0:
            return False, compiled.stdout.decode(errors="replace")
        ran = subprocess.run(
            [str(output)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        if ran.returncode != 0:
            return False, ran.stdout.decode(errors="replace")
        return True, ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def verify_problem(
    root: str | Path,
    problem: str | ProblemRef | Path,
    *,
    exact: bool = False,
    debug: bool = False,
    compiler: str = "g++",
    timeout: float = 2.0,
    stress_iterations: int = 100,
    seed: int | None = None,
) -> VerifyResult:
    """Compile and verify a problem while keeping artifacts under ``.acm``."""
    root_path = Path(root).resolve()
    source, ref = _resolve_source(root_path, problem)
    build_dir = root_path / ".acm" / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    resolved_compiler = shutil.which(compiler)
    executable = build_dir / (f"{ref.problem_id}.exe" if os.name == "nt" else ref.problem_id)
    flags = ["-std=c++17", "-O2", "-Wall", "-Wextra"]
    sanitizer = "not_requested"
    warnings: list[str] = []
    if resolved_compiler is None:
        command = [compiler, *flags, str(source), "-o", str(executable)]
        return VerifyResult(
            problem_id=ref.problem_id,
            source=str(source),
            passed=False,
            compiled=False,
            compile_command=command,
            compile_output=f"compiler not found: {compiler}",
        )

    if debug:
        supported, diagnostic = sanitizer_supported(resolved_compiler, build_dir)
        if supported:
            flags.extend(["-fsanitize=address,undefined", "-fno-omit-frame-pointer"])
            sanitizer = "enabled"
        else:
            sanitizer = "unsupported"
            warnings.append(
                "ASan/UBSan probe failed; compiled without sanitizers. "
                + diagnostic.strip()[:500]
            )

    command = [resolved_compiler, *flags, str(source), "-o", str(executable)]
    compile_process = _run(command, timeout=max(15.0, timeout))
    compile_output = (compile_process.stdout + compile_process.stderr).decode(errors="replace")
    result = VerifyResult(
        problem_id=ref.problem_id,
        source=str(source),
        passed=False,
        compiled=compile_process.returncode == 0,
        compile_command=command,
        compile_output=compile_output,
        sanitizer=sanitizer,
        warnings=warnings,
    )
    if not result.compiled:
        return result

    cases_dir = root_path / ".acm" / "cases" / ref.key
    if cases_dir.is_dir():
        result.cases = _run_cases(executable, cases_dir, exact=exact, timeout=timeout)

    brute_source = source.with_name(f"{ref.problem_id}.bf.cpp")
    generator_source = source.with_name(f"{ref.problem_id}.gen.cpp")
    if brute_source.is_file() and generator_source.is_file():
        stress_status, completed, failure, stress_warnings = _run_stress(
            ref,
            executable,
            brute_source,
            generator_source,
            resolved_compiler,
            flags,
            build_dir,
            timeout=timeout,
            iterations=stress_iterations,
            seed=seed,
        )
        result.stress = stress_status
        result.stress_iterations = completed
        result.failure_dir = str(failure) if failure else None
        result.warnings.extend(stress_warnings)

    cases_passed = all(case.passed for case in result.cases)
    result.passed = result.compiled and cases_passed and result.stress not in {"failed", "compile_failed", "timeout"}
    return result


def _resolve_source(
    root: Path, problem: str | ProblemRef | Path
) -> tuple[Path, ProblemRef]:
    if isinstance(problem, Path) or (
        isinstance(problem, str) and problem.lower().endswith(".cpp")
    ):
        candidate = Path(problem)
        source = candidate if candidate.is_absolute() else root / candidate
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        name = source.name
        for suffix in (".bf.cpp", ".gen.cpp", ".cpp"):
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)]
                break
        ref = parse_problem_ref(name)
        return source, ref
    ref = parse_problem_ref(problem)
    return find_solution(root, ref), ref


def _run(
    command: Sequence[str],
    *,
    input_data: bytes | None = None,
    timeout: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(command),
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            list(command),
            124,
            exc.stdout or b"",
            (exc.stderr or b"") + b"\nprocess timed out",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(list(command), 127, b"", str(exc).encode())


def _run_cases(
    executable: Path,
    cases_dir: Path,
    *,
    exact: bool,
    timeout: float,
) -> list[CaseResult]:
    results: list[CaseResult] = []
    for input_path in sorted(cases_dir.glob("*.in"), key=lambda path: path.name):
        output_path = input_path.with_suffix(".out")
        if not output_path.is_file():
            results.append(CaseResult(input_path.stem, False, "missing .out file", 0))
            continue
        started = time.monotonic()
        process = _run(
            [str(executable)], input_data=input_path.read_bytes(), timeout=timeout
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if process.returncode == 124:
            results.append(CaseResult(input_path.stem, False, "timeout", elapsed_ms))
        elif process.returncode != 0:
            results.append(
                CaseResult(
                    input_path.stem,
                    False,
                    f"runtime error ({process.returncode})",
                    elapsed_ms,
                )
            )
        elif outputs_equal(process.stdout, output_path.read_bytes(), exact=exact):
            results.append(CaseResult(input_path.stem, True, "ok", elapsed_ms))
        else:
            results.append(CaseResult(input_path.stem, False, "wrong answer", elapsed_ms))
    return results


def _run_stress(
    ref: ProblemRef,
    solution: Path,
    brute_source: Path,
    generator_source: Path,
    compiler: str,
    flags: list[str],
    build_dir: Path,
    *,
    timeout: float,
    iterations: int,
    seed: int | None,
) -> tuple[str, int, Path | None, list[str]]:
    brute = build_dir / (f"{ref.problem_id}.bf.exe" if os.name == "nt" else f"{ref.problem_id}.bf")
    generator = build_dir / (f"{ref.problem_id}.gen.exe" if os.name == "nt" else f"{ref.problem_id}.gen")
    commands = [
        [compiler, *flags, str(brute_source), "-o", str(brute)],
        [compiler, *flags, str(generator_source), "-o", str(generator)],
    ]
    warnings: list[str] = []
    for command in commands:
        process = _run(command, timeout=max(15.0, timeout))
        if process.returncode != 0:
            warnings.append(
                "stress helper compilation failed: "
                + (process.stdout + process.stderr).decode(errors="replace")[:1000]
            )
            return "compile_failed", 0, None, warnings

    first_seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**63)
    for offset in range(max(0, iterations)):
        current_seed = first_seed + offset
        env = os.environ.copy()
        env["ACM_STRESS_SEED"] = str(current_seed)
        generator_command = [str(generator), str(current_seed)]
        generated = _run(generator_command, timeout=timeout, env=env)
        if generated.returncode != 0:
            warnings.append(f"generator failed for seed {current_seed}")
            return "failed", offset, None, warnings
        fast = _run([str(solution)], input_data=generated.stdout, timeout=timeout)
        brute_run = _run([str(brute)], input_data=generated.stdout, timeout=timeout)
        if fast.returncode == 124 or brute_run.returncode == 124:
            failure = _save_failure(
                build_dir,
                ref,
                current_seed,
                generated.stdout,
                fast.stdout,
                brute_run.stdout,
                generator_command,
                "timeout",
            )
            return "timeout", offset + 1, failure, warnings
        if (
            fast.returncode != 0
            or brute_run.returncode != 0
            or not outputs_equal(fast.stdout, brute_run.stdout)
        ):
            failure = _save_failure(
                build_dir,
                ref,
                current_seed,
                generated.stdout,
                fast.stdout,
                brute_run.stdout,
                generator_command,
                "mismatch",
            )
            return "failed", offset + 1, failure, warnings
    return "passed", max(0, iterations), None, warnings


def _save_failure(
    build_dir: Path,
    ref: ProblemRef,
    seed: int,
    input_data: bytes,
    actual: bytes,
    expected: bytes,
    generator_command: Sequence[str],
    reason: str,
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    failure_dir = build_dir.parent / "failures" / ref.key / f"{timestamp}-seed-{seed}"
    failure_dir.mkdir(parents=True, exist_ok=False)
    (failure_dir / "input.txt").write_bytes(input_data)
    (failure_dir / "actual.txt").write_bytes(actual)
    (failure_dir / "expected.txt").write_bytes(expected)
    metadata = {
        "problem_id": ref.problem_id,
        "seed": seed,
        "reason": reason,
        "generator_command": list(generator_command),
        "commands": {
            "generator": list(generator_command),
            "solution": [str(build_dir / (f"{ref.problem_id}.exe" if os.name == "nt" else ref.problem_id))],
            "brute_force": [str(build_dir / (f"{ref.problem_id}.bf.exe" if os.name == "nt" else f"{ref.problem_id}.bf"))],
        },
    }
    (failure_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    return failure_dir.resolve()


__all__ = [
    "CaseResult",
    "VerifyResult",
    "outputs_equal",
    "sanitizer_supported",
    "verify_problem",
]
