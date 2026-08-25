"""Compile, sample-test, and stress-test local competitive-programming code."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import ctypes
import json
import math
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import threading
import time
from typing import Sequence
import uuid

from .workspace import ProblemRef, find_solution, parse_problem_ref


# ASan/UBSan instrumentation costs several times the release runtime.  Reusing
# the caller's wall-clock budget unchanged made ``--debug`` runs report TLE for
# solutions that are comfortably in limit without sanitizers, which reads as a
# real performance failure rather than as measurement overhead.
_SANITIZER_TIMEOUT_SCALE = 4.0
_MAX_STDOUT_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 1024 * 1024
_WINDOWS_PROCESS_MEMORY_LIMIT = 512 * 1024 * 1024
_WINDOWS_ACTIVE_PROCESS_LIMIT = 16


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
    verification_level: str = "compile_failed"
    verification_status: str = "failed"

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
    # A unique name per probe: a fixed name lets two concurrent verifies race on
    # the same path, where one process can execute the other's half-written
    # binary.  The probe is also removed in ``finally`` so it never accumulates
    # in the managed build directory -- a leftover sanitizer-instrumented
    # executable is both dead weight and a false positive for antivirus.
    output = Path(build_dir) / (
        f"sanitizer-probe-{os.getpid()}-{uuid.uuid4().hex}"
        f"{'.exe' if os.name == 'nt' else ''}"
    )
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
    finally:
        try:
            output.unlink(missing_ok=True)
        except OSError:
            # Windows can hold the image briefly after the child exits; a
            # leftover probe is harmless next to failing the capability check.
            pass


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
    generator_file: str | Path | None = None,
    reference_file: str | Path | None = None,
) -> VerifyResult:
    """Compile and verify a problem while keeping artifacts under ``.acm``."""
    timeout = float(timeout)
    stress_iterations = int(stress_iterations)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite number greater than 0")
    if stress_iterations < 1:
        raise ValueError("stress_iterations must be at least 1")
    root_path = Path(root).resolve()
    source, ref = _resolve_source(root_path, problem)
    build_dir = (
        root_path
        / ".acm"
        / "build"
        / "runs"
        / f"{ref.problem_id}-{uuid.uuid4().hex}"
    )
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

    # Sanitizer flags reach the solution, brute force and generator alike, so
    # every timed run in this call needs the widened budget.
    runtime_timeout = timeout
    if sanitizer == "enabled":
        runtime_timeout = timeout * _SANITIZER_TIMEOUT_SCALE
        warnings.append(
            f"sanitizers enabled; per-run timeout widened {timeout:.1f}s -> "
            f"{runtime_timeout:.1f}s so instrumentation overhead is not "
            f"reported as TLE"
        )

    command = [resolved_compiler, *flags, str(source), "-o", str(executable)]
    compile_process = _run(command, timeout=max(15.0, timeout))
    _append_process_warnings(warnings, compile_process)
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
        result.cases = _run_cases(
            executable,
            cases_dir,
            exact=exact,
            timeout=runtime_timeout,
            warnings=result.warnings,
        )

    brute_source = _resolve_optional_cpp_source(
        root_path,
        reference_file,
        fallback=source.with_name(f"{ref.problem_id}.bf.cpp"),
        role="reference",
    )
    generator_source = _resolve_optional_cpp_source(
        root_path,
        generator_file,
        fallback=source.with_name(f"{ref.problem_id}.gen.cpp"),
        role="generator",
    )
    if brute_source.is_file() and generator_source.is_file():
        stress_status, completed, failure, stress_warnings = _run_stress(
            ref,
            executable,
            brute_source,
            generator_source,
            resolved_compiler,
            flags,
            build_dir,
            timeout=runtime_timeout,
            iterations=stress_iterations,
            seed=seed,
        )
        result.stress = stress_status
        result.stress_iterations = completed
        result.failure_dir = str(failure) if failure else None
        result.warnings.extend(stress_warnings)

    has_sample_evidence = bool(result.cases)
    has_stress_evidence = result.stress == "passed" and result.stress_iterations > 0
    samples_passed = has_sample_evidence and all(case.passed for case in result.cases)
    stress_passed = has_stress_evidence
    any_failure = (
        (has_sample_evidence and not samples_passed)
        or result.stress not in {"not_available", "passed"}
    )
    if any_failure:
        result.verification_level = "failed"
        result.verification_status = "failed"
        result.passed = False
    elif samples_passed and stress_passed:
        result.verification_level = "samples_and_stress_passed"
        result.verification_status = "passed"
        result.passed = True
    elif samples_passed:
        result.verification_level = "samples_passed"
        result.verification_status = "passed"
        result.passed = True
    elif stress_passed:
        result.verification_level = "stress_passed"
        result.verification_status = "passed"
        result.passed = True
    else:
        result.verification_level = "compiled_only"
        result.verification_status = "inconclusive"
        result.passed = False
    return result


def _resolve_optional_cpp_source(
    root: Path,
    selected: str | Path | None,
    *,
    fallback: Path,
    role: str,
) -> Path:
    if selected is None or not str(selected).strip():
        return fallback
    candidate = Path(selected)
    source = candidate if candidate.is_absolute() else root / candidate
    source = source.resolve()
    if source.suffix.lower() != ".cpp" or not source.is_file():
        raise ValueError(f"{role} source must be an existing .cpp file: {source}")
    return source


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
    """Run an untrusted local child with bounded output and tree cleanup.

    The ordinary ``subprocess.run(..., PIPE)`` path buffers without a limit and
    only guarantees termination of the immediate child.  Contest programs can
    therefore exhaust host memory with output or leave descendants alive after
    TLE.  This runner drains both pipes concurrently, caps each stream, and
    owns a process group (POSIX) or Job Object (Windows).
    """
    command_list = list(command)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite number greater than 0")
    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
    }
    warnings: list[str] = []
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
        warnings.append(
            "degraded isolation: POSIX memory/process-count limits are unavailable "
            "in the concurrent stdlib runner; timeout, output, and process-group "
            "limits remain enforced"
        )
    try:
        process = subprocess.Popen(command_list, **popen_kwargs)  # type: ignore[arg-type]
    except OSError as exc:
        return subprocess.CompletedProcess(command_list, 127, b"", str(exc).encode())

    job_handle: int | None = None
    if os.name == "nt":
        job_handle, job_warning = _assign_windows_job(process)
        if job_warning:
            warnings.append(job_warning)

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    overflow = threading.Event()
    overflow_stream: list[str] = []

    def drain(
        pipe: object,
        chunks: list[bytes],
        limit: int,
        stream_name: str,
    ) -> None:
        used = 0
        try:
            while True:
                chunk = pipe.read(64 * 1024)  # type: ignore[attr-defined]
                if not chunk:
                    break
                remaining = limit - used
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                    used += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    if not overflow_stream:
                        overflow_stream.append(stream_name)
                    overflow.set()
                    break
        finally:
            pipe.close()  # type: ignore[attr-defined]

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout_chunks, _MAX_STDOUT_BYTES, "stdout"),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr_chunks, _MAX_STDERR_BYTES, "stderr"),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    writer: threading.Thread | None = None
    if input_data is not None:
        assert process.stdin is not None

        def write_input() -> None:
            try:
                process.stdin.write(input_data)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()

        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()

    timed_out = False
    output_limited = False
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if overflow.is_set():
            output_limited = True
            _terminate_process_tree(process, job_handle)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _terminate_process_tree(process, job_handle)
            break
        time.sleep(0.01)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    for reader in readers:
        reader.join(timeout=2.0)
    if writer is not None:
        writer.join(timeout=2.0)
    # A short-lived process can exit before the main polling loop observes the
    # reader's overflow event.  The cap remains a failure even when no kill was
    # needed because the child had already exited.
    if overflow.is_set() and not timed_out:
        output_limited = True
    if job_handle is not None:
        ctypes.windll.kernel32.CloseHandle(job_handle)

    stdout = b"".join(stdout_chunks)
    stderr = b"".join(stderr_chunks)
    returncode = process.returncode
    if timed_out:
        returncode = 124
        stderr += b"\nprocess timed out"
    elif output_limited:
        returncode = 125
        stream = overflow_stream[0] if overflow_stream else "output"
        stderr += f"\n{stream} limit exceeded".encode()
    result = subprocess.CompletedProcess(command_list, returncode, stdout, stderr)
    result.acm_timed_out = timed_out  # type: ignore[attr-defined]
    result.acm_output_limited = output_limited  # type: ignore[attr-defined]
    result.acm_output_limit_stream = (  # type: ignore[attr-defined]
        overflow_stream[0] if overflow_stream else None
    )
    result.acm_warnings = warnings  # type: ignore[attr-defined]
    return result


def _assign_windows_job(
    process: subprocess.Popen[bytes],
) -> tuple[int | None, str | None]:
    """Assign a child to a kill-on-close Job Object with conservative limits."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None, (
            "degraded isolation: Windows Job Object creation failed; timeout, "
            "output, and process-group limits remain enforced"
        )
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x2000 | 0x0100 | 0x0008
    information.BasicLimitInformation.ActiveProcessLimit = _WINDOWS_ACTIVE_PROCESS_LIMIT
    information.ProcessMemoryLimit = _WINDOWS_PROCESS_MEMORY_LIMIT
    configured = kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(information), ctypes.sizeof(information)
    )
    assigned = configured and kernel32.AssignProcessToJobObject(job, process._handle)
    if not assigned:
        kernel32.CloseHandle(job)
        return None, (
            "degraded isolation: Windows Job Object assignment failed; timeout, "
            "output, and process-group limits remain enforced"
        )
    return int(job), None


def _terminate_process_tree(
    process: subprocess.Popen[bytes], job_handle: int | None
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        if job_handle is not None:
            ctypes.windll.kernel32.TerminateJobObject(job_handle, 1)
            return
        # Job assignment can be unavailable when the host itself uses a
        # restrictive Job Object.  ``taskkill /T`` is the Windows tree-kill
        # fallback and its output is deliberately discarded.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _output_limited(process: subprocess.CompletedProcess[bytes]) -> bool:
    return bool(getattr(process, "acm_output_limited", False))


def _append_process_warnings(
    warnings: list[str], process: subprocess.CompletedProcess[bytes]
) -> None:
    for warning in getattr(process, "acm_warnings", []):
        if warning not in warnings:
            warnings.append(warning)


def _timed_out(process: subprocess.CompletedProcess[bytes]) -> bool:
    """Report whether ``_run`` aborted this process on its own deadline.

    The synthesized timeout exit code (124) is indistinguishable from a child
    that genuinely exited with 124, so timeout detection must read the marker
    ``_run`` attaches rather than the exit code.  ``124`` is still returned for
    compatibility with anything matching on it.
    """
    return bool(getattr(process, "acm_timed_out", False))


def _run_cases(
    executable: Path,
    cases_dir: Path,
    *,
    exact: bool,
    timeout: float,
    warnings: list[str],
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
        _append_process_warnings(warnings, process)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if _timed_out(process):
            results.append(CaseResult(input_path.stem, False, "timeout", elapsed_ms))
        elif _output_limited(process):
            stream = getattr(process, "acm_output_limit_stream", "output")
            results.append(
                CaseResult(
                    input_path.stem,
                    False,
                    f"output limit exceeded ({stream})",
                    elapsed_ms,
                )
            )
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
        _append_process_warnings(warnings, process)
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
        _append_process_warnings(warnings, generated)
        if _output_limited(generated):
            stream = getattr(generated, "acm_output_limit_stream", "output")
            warnings.append(
                f"generator output limit exceeded ({stream}) for seed {current_seed}"
            )
            return "output_limit", offset + 1, None, warnings
        if generated.returncode != 0:
            warnings.append(f"generator failed for seed {current_seed}")
            return "failed", offset, None, warnings
        fast = _run([str(solution)], input_data=generated.stdout, timeout=timeout)
        brute_run = _run([str(brute)], input_data=generated.stdout, timeout=timeout)
        _append_process_warnings(warnings, fast)
        _append_process_warnings(warnings, brute_run)
        if _timed_out(fast) or _timed_out(brute_run):
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
        if _output_limited(fast) or _output_limited(brute_run):
            failure = _save_failure(
                build_dir,
                ref,
                current_seed,
                generated.stdout,
                fast.stdout,
                brute_run.stdout,
                generator_command,
                "output_limit",
            )
            warnings.append(f"solution output limit exceeded for seed {current_seed}")
            return "output_limit", offset + 1, failure, warnings
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
    return "passed", iterations, None, warnings


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
    # ``build_dir`` is a per-run ``.acm/build/runs/<uuid>`` directory; failure
    # assets remain in the stable public location documented by the workflow.
    failure_dir = build_dir.parents[2] / "failures" / ref.key / f"{timestamp}-seed-{seed}"
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
