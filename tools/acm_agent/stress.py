"""Safe building blocks for AI-assisted differential stress testing.

This module deliberately has no dependency on the web, service, or storage
layers.  In particular it never falls back to ``subprocess.run`` for generated
or downloaded programs: callers must supply a capable sandbox backend.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from typing import Callable, Mapping, Protocol, Sequence
import uuid

from .canonical import canonical_json_bytes
from .stress_recipe import SMALL_EXHAUSTIVE_MAX_BYTES
from .stress_search import CaseRequest, SearchDriver


MAX_CPP_BYTES = 256 * 1024
MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_LARGE_INPUT_BYTES = 32 * 1024 * 1024
MAX_LARGE_OUTPUT_BYTES = 16 * 1024 * 1024
# Small inputs get exactly two attempts: the initial human-checkable budget,
# then the global stream ceiling.  Escalating in small steps would let a
# structurally broken generator walk from 100 bytes to 2 MiB, spending a
# sandboxed generator run per step until the local-validation window is gone and
# the only diagnostic left is "budget exhausted" instead of the real defect.
# One jump keeps the legal-but-larger case working while a genuinely broken
# generator fails closed after two runs with its own failure attached.
SMALL_INPUT_INITIAL_BYTES = 100
SMALL_INPUT_CEILING_BYTES = MAX_STREAM_BYTES
DUAL_REFERENCE_PROTOCOL = "dual_reference_v1"
LEGACY_TRIO_PROTOCOL = "legacy_trio"
DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024
_LAUNCHER_BUILD_LOCK = threading.Lock()
_DETAIL_UNSET = object()
_GENERATOR_REQUIRED_CASES = [
    {"profile": "small", "case_kind": "lower_bound"},
    {"profile": "small", "case_kind": "random"},
    {"profile": "large", "case_kind": "upper_bound"},
    {"profile": "large", "case_kind": "random"},
]
# Kept as a named case for scheduling/backward-compatible imports.  It is no
# longer optional: minimal verification always exercises it, so capability and
# generation policy must require it end-to-end.
_GENERATOR_OPTIONAL_CASE = {"profile": "small", "case_kind": "lower_bound"}
_GENERATOR_SUPPORTED_CASES = list(_GENERATOR_REQUIRED_CASES)
_GENERATOR_CAPABILITY_EXPECTED = {
    "profile_version": 2,
    # v1 remains the compatibility baseline; the accepted-version list records
    # the additive v2 protocol for newer local recipe generators.
    "manifest_version": 1,
    "manifest_version_contains": [1, 2],
    "profiles_contains": ["small", "large"],
    "case_kinds_contains": ["upper_bound", "random"],
    "required_supported_cases": _GENERATOR_REQUIRED_CASES,
    "small_lower_bound_required": True,
}
_GENERATOR_MANIFEST_V1_KEYS = {
    "manifest_version",
    "profile",
    "case_kind",
    "seed",
    "input_sha256",
    "dimensions",
    "coverage_tags",
    "records",
    "total_complexity",
}
_GENERATOR_MANIFEST_V2_EXTRA_KEYS = {
    "engine",
    "recipe_source",
    "state_machine",
    "section_family",
    "operation_family",
    "planned_byte_bucket",
    "actual_byte_bucket",
}
_GENERATOR_MANIFEST_V2_KEYS = (
    _GENERATOR_MANIFEST_V1_KEYS | _GENERATOR_MANIFEST_V2_EXTRA_KEYS
)
# Historical private alias kept for v1-focused tests and diagnostics.
_GENERATOR_MANIFEST_KEYS = _GENERATOR_MANIFEST_V1_KEYS


class StressError(RuntimeError):
    """Base class for structured stress failures."""


class SourceSafetyError(StressError):
    """Raised when generated or downloaded C++ violates the source policy."""

    def __init__(
        self,
        message: str,
        *,
        rule_id: str = "source_policy",
        matched_token: str = "",
        line: int | None = None,
        column: int | None = None,
        excerpt: str = "",
    ) -> None:
        super().__init__(message)
        self.details: dict[str, object] = {"rule_id": str(rule_id)[:80]}
        if matched_token:
            self.details["matched_token"] = str(matched_token)[:80]
        if line is not None:
            self.details["line"] = max(1, int(line))
        if column is not None:
            self.details["column"] = max(1, int(column))
        if excerpt:
            self.details["excerpt"] = str(excerpt)[:240]


class SandboxUnavailableError(StressError):
    """Raised instead of executing code without the required isolation."""


class BundleConflictError(StressError):
    """Raised when a helper changed since preview/apply."""


class GeneratorCapabilityError(StressError):
    """Raised when a new run is given a legacy/non-conforming generator."""

    code = "generator_capability_missing"

    def __init__(
        self,
        message: str,
        *,
        expected: object = _DETAIL_UNSET,
        actual: object = _DETAIL_UNSET,
    ) -> None:
        super().__init__(message)
        self.details: dict[str, object] = {}
        if expected is not _DETAIL_UNSET:
            self.details["expected"] = expected
        if actual is not _DETAIL_UNSET:
            self.details["actual"] = actual


class HelperPreflightError(StressError):
    """A staged helper failed before any managed file was changed."""

    code = "stress_helper_preflight_failed"

    def __init__(
        self,
        message: str,
        *,
        artifact: str,
        profile: str,
        case_kind: str,
        seed: int,
        stderr: bytes | str = b"",
        code: str | None = None,
        expected: object = _DETAIL_UNSET,
        actual: object = _DETAIL_UNSET,
    ) -> None:
        super().__init__(message)
        self.code = code or type(self).code
        error_text = (
            stderr.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes)
            else str(stderr)
        )
        self.artifact = artifact
        self.profile = profile
        self.case_kind = case_kind
        self.seed = int(seed)
        self.details: dict[str, object] = {
            "artifact": artifact,
            "profile": profile,
            "case_kind": case_kind,
            "seed": int(seed),
            "stderr": error_text[-2000:],
        }
        if expected is not _DETAIL_UNSET:
            self.details["expected"] = expected
        if actual is not _DETAIL_UNSET:
            self.details["actual"] = actual


@dataclass(frozen=True, slots=True)
class SandboxCapability:
    available: bool
    reason: str = ""
    backend: str = "unknown"


@dataclass(slots=True)
class SandboxLimits:
    timeout_seconds: float = 2.0
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    stdin_bytes: int = MAX_STREAM_BYTES
    stdout_bytes: int = MAX_STREAM_BYTES
    stderr_bytes: int = MAX_STREAM_BYTES


@dataclass(slots=True)
class SandboxProcessResult:
    command: list[str]
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    elapsed_ms: int = 0
    timed_out: bool = False
    output_limited: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.output_limited


_SANDBOX_ENV_NAME_RE = re.compile(r"\A[A-Z][A-Z0-9_]*\Z")
_SANDBOX_ENV_ALLOWED_PREFIX = "ACM_STRESS_"
_SANDBOX_ENV_MAX_VALUE_BYTES = 4096


def _sandbox_env_entries(
    env: Mapping[str, str] | None,
) -> list[tuple[str, str]]:
    """Validate caller-supplied sandbox environment entries.

    The launcher hands the child a deliberately minimal, launcher-controlled
    environment.  Callers may add deterministic stress knobs but nothing else:
    only ``ACM_STRESS_*`` names are accepted, and values may not contain NUL
    (which would truncate the Windows environment block) or a newline (which
    would corrupt meta/diagnostic parsing).  Anything else raises rather than
    being silently dropped, because a seed that fails to reach the generator
    produces a non-reproducible run that looks successful.
    """
    if not env:
        return []
    entries: list[tuple[str, str]] = []
    for raw_name, raw_value in env.items():
        name = str(raw_name)
        value = str(raw_value)
        if not _SANDBOX_ENV_NAME_RE.match(name):
            raise ValueError(f"invalid sandbox environment name: {name!r}")
        if not name.startswith(_SANDBOX_ENV_ALLOWED_PREFIX):
            raise ValueError(
                f"sandbox environment name must start with "
                f"{_SANDBOX_ENV_ALLOWED_PREFIX}: {name!r}"
            )
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"invalid sandbox environment value for {name}")
        if len(value.encode("utf-8")) > _SANDBOX_ENV_MAX_VALUE_BYTES:
            raise ValueError(f"sandbox environment value too large for {name}")
        entries.append((name, value))
    return sorted(entries)


class SandboxBackend(Protocol):
    """Execution boundary for untrusted helper and reference programs."""

    def probe(self) -> SandboxCapability: ...

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        env: Mapping[str, str] | None = None,
        limits: SandboxLimits | None = None,
    ) -> SandboxProcessResult: ...

    def cancel(self) -> None: ...


def probe_generator_v2(
    sandbox: SandboxBackend,
    executable: str | Path,
    *,
    cwd: Path,
    timeout: float = 2.0,
) -> dict[str, object]:
    """Require the deterministic generator profile-v2 capability handshake."""
    queried = sandbox.run(
        [str(executable), "--capabilities"],
        cwd=cwd,
        input_data=None,
        env={
            "ACM_STRESS_QUERY": "capabilities",
            "ACM_STRESS_PROFILE_VERSION": "2",
        },
        limits=SandboxLimits(timeout_seconds=timeout, stdout_bytes=64 * 1024),
    )
    failure = _result_failure_kind("generator", queried)
    try:
        capabilities = json.loads(queried.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        capabilities = None
    profiles = capabilities.get("profiles") if isinstance(capabilities, dict) else None
    case_kinds = capabilities.get("case_kinds") if isinstance(capabilities, dict) else None
    supported_cases = (
        capabilities.get("supported_cases")
        if isinstance(capabilities, dict)
        else None
    )
    valid_supported_cases = (
        isinstance(supported_cases, list)
        and supported_cases == _GENERATOR_REQUIRED_CASES
    )
    declared_case_kinds = (
        {str(item) for item in case_kinds} if isinstance(case_kinds, list) else set()
    )
    valid = (
        failure is None
        and isinstance(capabilities, dict)
        and type(capabilities.get("profile_version")) is int
        and capabilities.get("profile_version") == 2
        and type(capabilities.get("manifest_version")) is int
        and capabilities.get("manifest_version") in {1, 2}
        and isinstance(profiles, list)
        and {"small", "large"}.issubset({str(item) for item in profiles})
        and isinstance(case_kinds, list)
        and {"lower_bound", "upper_bound", "random"}.issubset(declared_case_kinds)
        and valid_supported_cases
    )
    if not valid:
        raise GeneratorCapabilityError(
            "generator does not declare the complete stress profile-v2 manifest "
            "capability; regenerate helpers",
            # Deep-copied: details travel into diagnostics, JSON payloads and
            # persisted validation records, and a caller mutating them must not
            # corrupt the module constant (nor the nested case lists it shares
            # with _GENERATOR_REQUIRED_CASES) for the rest of the process.
            expected=copy.deepcopy(_GENERATOR_CAPABILITY_EXPECTED),
            actual=capabilities,
        )
    return dict(capabilities)


# Static linking is a correctness and security requirement for the launcher, not
# an optimisation.  The launcher is the trusted half of the execution boundary, so
# it must not resolve libstdc++/libgcc from the ambient PATH: when a different
# MinGW runtime shadows the toolchain that compiled it (Git-for-Windows ships a
# copy that commonly precedes the real toolchain), the launcher faults inside
# std::ifstream before it can write its meta file.  The caller then sees only an
# opaque ``launcher exited 3221225477`` and every sandboxed run on the machine
# fails closed.
_LAUNCHER_COMPILE_FLAGS: tuple[str, ...] = (
    "-std=c++17", "-O2", "-municode", "-static",
)
_LAUNCHER_LINK_FLAGS: tuple[str, ...] = ("-ladvapi32", "-lole32")


class WindowsAppContainerBackend:
    """Adapter for a trusted AppContainer + Job Object launcher.

    The launcher is intentionally external to Python: it is responsible for
    creating an AppContainer with no network capabilities, granting access only
    to ``cwd``, assigning the complete process tree to a kill-on-close Job
    Object, enforcing memory/CPU limits, and bounding captured output.  If that
    trusted launcher is absent, execution fails closed.

    The small native launcher is built from the checked-in source on first use.
    Target commands are passed as an argv array and are never interpreted by a
    shell.
    """

    def __init__(
        self,
        launcher: str | Path | None = None,
        *,
        root: str | Path | None = None,
    ) -> None:
        configured = launcher or os.environ.get("ACM_AGENT_APPCONTAINER_RUNNER")
        self.launcher = Path(configured).resolve() if configured else None
        self.root = Path(root).resolve() if root is not None else None
        self._build_error = ""
        self._active: subprocess.Popen[bytes] | None = None
        self._active_cancel_path: Path | None = None
        self._lock = threading.Lock()
        self._cancel_requested = threading.Event()

    def _ensure_launcher(self) -> None:
        if self.launcher is not None:
            return
        with _LAUNCHER_BUILD_LOCK:
            if self.launcher is None:
                self._ensure_launcher_locked()

    def _ensure_launcher_locked(self) -> None:
        if self.launcher is not None:
            return
        if self.root is None:
            self._build_error = "trusted AppContainer launcher is not configured"
            return
        source = Path(__file__).with_name("native") / "appcontainer_runner.cpp"
        if not source.is_file():
            self._build_error = "bundled AppContainer launcher source is missing"
            return
        native_dir = self.root / ".acm" / "native"
        native_dir.mkdir(parents=True, exist_ok=True)
        output = native_dir / "appcontainer_runner.exe"
        stamp = native_dir / "appcontainer_runner.sha256"
        # The stamp covers the build flags as well as the source.  A launcher
        # produced by an older flag set is otherwise indistinguishable from a
        # current one, so a flag correction would never reach a machine that
        # already holds a stale binary.
        expected = sha256_bytes(
            b"\x00".join(
                [
                    sha256_file(source).encode("ascii"),
                    *[flag.encode("utf-8") for flag in _LAUNCHER_COMPILE_FLAGS],
                    *[flag.encode("utf-8") for flag in _LAUNCHER_LINK_FLAGS],
                ]
            )
        )
        if output.is_file() and stamp.is_file() and stamp.read_text(encoding="ascii").strip() == expected:
            self.launcher = output
            return
        temporary = native_dir / f"appcontainer_runner.{uuid.uuid4().hex}.exe"
        try:
            built = subprocess.run(
                [
                    "g++", *_LAUNCHER_COMPILE_FLAGS, str(source),
                    "-o", str(temporary), *_LAUNCHER_LINK_FLAGS,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60.0,
                check=False,
                shell=False,
            )
            if built.returncode != 0 or not temporary.is_file():
                detail = built.stderr.decode(errors="replace")[-500:].strip()
                self._build_error = detail or "failed to build bundled AppContainer launcher"
                return
            os.replace(temporary, output)
            stamp.write_text(expected + "\n", encoding="ascii")
            self.launcher = output
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._build_error = f"failed to build bundled AppContainer launcher: {exc}"
        finally:
            temporary.unlink(missing_ok=True)

    def probe(self) -> SandboxCapability:
        if os.name != "nt":
            return SandboxCapability(False, "AppContainer is only available on Windows", "appcontainer")
        self._ensure_launcher()
        if self.launcher is None:
            return SandboxCapability(
                False,
                self._build_error or "trusted AppContainer launcher is not configured",
                "appcontainer",
            )
        if not self.launcher.is_file():
            return SandboxCapability(False, "trusted AppContainer launcher does not exist", "appcontainer")
        try:
            probe = subprocess.run(
                [str(self.launcher), "--probe"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5.0,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SandboxCapability(False, f"AppContainer probe failed: {exc}", "appcontainer")
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout).decode(errors="replace")[:300].strip()
            return SandboxCapability(False, detail or "AppContainer probe rejected", "appcontainer")
        return SandboxCapability(True, backend="appcontainer")

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        env: Mapping[str, str] | None = None,
        limits: SandboxLimits | None = None,
    ) -> SandboxProcessResult:
        if self._cancel_requested.is_set():
            return SandboxProcessResult(list(command), 130)
        capability = self.probe()
        if not capability.available:
            raise SandboxUnavailableError(capability.reason)
        if self._cancel_requested.is_set():
            return SandboxProcessResult(list(command), 130)
        assert self.launcher is not None
        sandbox_dir = cwd.resolve()
        if not sandbox_dir.is_dir():
            raise ValueError("sandbox cwd must be an existing directory")
        selected = limits or SandboxLimits()
        if len(input_data or b"") > selected.stdin_bytes:
            raise StressError(f"sandbox input exceeds {selected.stdin_bytes} bytes")
        request_id = uuid.uuid4().hex
        stdin_path = sandbox_dir / f".{request_id}.stdin"
        stdout_path = sandbox_dir / f".{request_id}.stdout"
        stderr_path = sandbox_dir / f".{request_id}.stderr"
        meta_path = sandbox_dir / f".{request_id}.meta"
        cancel_path = sandbox_dir / f".{request_id}.cancel"
        stdin_path.write_bytes(input_data or b"")
        argv = [
            str(self.launcher), "--run",
            "--stdin", str(stdin_path),
            "--stdout", str(stdout_path),
            "--stderr", str(stderr_path),
            "--meta", str(meta_path),
            "--cancel", str(cancel_path),
            "--timeout-ms", str(max(1, round(selected.timeout_seconds * 1000))),
            "--memory", str(selected.memory_bytes),
            "--stdout-limit", str(selected.stdout_bytes),
            "--stderr-limit", str(selected.stderr_bytes),
        ]
        for name, value in _sandbox_env_entries(env):
            argv += ["--env", f"{name}={value}"]
        argv += ["--", *[str(item) for item in command]]
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        with self._lock:
            self._active = process
            self._active_cancel_path = cancel_path
            # ``cancel`` may race with Popen between the early cancellation
            # check and publishing the marker path.  Recheck while holding the
            # same lock used by cancel so every interleaving leaves a marker
            # for the launcher to consume after it has safely assigned the
            # AppContainer child to its kill-on-close Job Object.
            cancel_after_launch = self._cancel_requested.is_set()
        if cancel_after_launch:
            try:
                cancel_path.write_bytes(b"cancel\n")
            except OSError:
                pass
        try:
            launcher_stdout, launcher_stderr = process.communicate(
                timeout=max(5.0, selected.timeout_seconds + 3.0)
            )
        except subprocess.TimeoutExpired:
            try:
                cancel_path.write_bytes(b"cancel\n")
            except OSError:
                pass
            try:
                launcher_stdout, launcher_stderr = process.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                launcher_stdout, launcher_stderr = process.communicate()
            cancelled = self._cancel_requested.is_set()
            for path in (stdin_path, stdout_path, stderr_path, meta_path, cancel_path):
                path.unlink(missing_ok=True)
            return SandboxProcessResult(
                list(command), 130 if cancelled else 124, b"",
                launcher_stderr[: selected.stderr_bytes],
                round((time.monotonic() - started) * 1000),
                timed_out=not cancelled,
            )
        finally:
            with self._lock:
                self._active = None
                self._active_cancel_path = None
        try:
            if process.returncode != 0 or not meta_path.is_file():
                if self._cancel_requested.is_set():
                    return SandboxProcessResult(
                        list(command),
                        130,
                        b"",
                        launcher_stderr[: selected.stderr_bytes],
                        round((time.monotonic() - started) * 1000),
                    )
                detail = launcher_stderr or launcher_stdout or f"launcher exited {process.returncode}".encode()
                raise SandboxUnavailableError(detail.decode(errors="replace")[:500])
            metadata = {}
            for line in meta_path.read_text(encoding="ascii").splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    metadata[key] = value
            stdout = stdout_path.read_bytes() if stdout_path.is_file() else b""
            stderr = stderr_path.read_bytes() if stderr_path.is_file() else b""
            return SandboxProcessResult(
                command=list(command),
                returncode=int(metadata.get("returncode", 127)),
                stdout=stdout[: selected.stdout_bytes],
                stderr=stderr[: selected.stderr_bytes],
                elapsed_ms=int(metadata.get("elapsed_ms", 0)),
                timed_out=metadata.get("timed_out") == "1",
                output_limited=metadata.get("output_limited") == "1"
                or len(stdout) > selected.stdout_bytes
                or len(stderr) > selected.stderr_bytes,
            )
        except (ValueError, TypeError, OSError) as exc:
            raise SandboxUnavailableError(f"invalid sandbox launcher response: {exc}") from exc
        finally:
            for path in (stdin_path, stdout_path, stderr_path, meta_path, cancel_path):
                path.unlink(missing_ok=True)

    def cancel(self) -> None:
        self._cancel_requested.set()
        with self._lock:
            cancel_path = self._active_cancel_path
        if cancel_path is not None:
            try:
                cancel_path.write_bytes(b"cancel\n")
            except OSError:
                # The launcher may have completed and removed the request
                # boundary between reading the path and writing the marker.
                pass


_ALLOWED_HEADERS = frozenset(
    {
        "algorithm", "array", "bit", "bitset", "cassert", "cctype", "cerrno",
        "cfloat", "chrono", "climits", "cmath", "cstddef", "cstdint", "cstdio", "cstdlib", "ctime",
        "cstring", "deque", "functional", "iomanip", "iostream", "iterator",
        "limits", "map", "numeric", "optional", "queue", "random", "set",
        "sstream", "istream", "ostream", "ios", "iosfwd", "streambuf",
        "list", "forward_list", "memory", "stack", "stdexcept", "string", "string_view",
        "tuple", "type_traits",
        "unordered_map", "unordered_set", "utility", "vector", "bits/stdc++.h",
    }
)
_DANGEROUS_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("preprocessor_directive", r"(?m)^\s*#\s*(?:pragma|line|import|embed)\b", "dangerous preprocessor directive"),
    ("quoted_include", r"(?m)^\s*#\s*include\s*[\"']", "quoted include"),
    ("process_api", r"\b(?:system|popen|_popen|fork|vfork|exec[lvpe]*|spawn[lvpe]*|CreateProcess\w*|WinExec|ShellExecute\w*)\s*\(", "process API"),
    ("network_api", r"\b(?:socket|connect|bind|listen|accept|send|recv|WSAStartup|InternetOpen\w*|URLDownloadToFile\w*)\s*\(", "network API"),
    ("filesystem_api", r"\b(?:fopen|freopen|open|creat|remove|rename|unlink|rmdir|mkdir|CreateFile\w*|DeleteFile\w*|MoveFile\w*)\s*\(", "filesystem API"),
    ("file_stream", r"\b(?:ifstream|ofstream|fstream)\b", "file stream"),
    ("dynamic_loading", r"\b(?:dlopen|dlsym|LoadLibrary\w*|GetProcAddress)\s*\(", "dynamic loading"),
    ("inline_assembly", r"\b(?:asm|__asm|__asm__)\b", "inline assembly"),
)

TRUSTED_GENERATOR_HARNESS_VERSION = 3
HELPER_PREFLIGHT_VERSION = 9
_TRUSTED_GENERATOR_MARKER = "// ACM_TRUSTED_GENERATOR_HARNESS_V3"
_LEGACY_TRUSTED_GENERATOR_MARKERS = (
    "// ACM_TRUSTED_GENERATOR_HARNESS_V2",
    "// ACM_TRUSTED_GENERATOR_HARNESS_V1",
)
_LOCAL_RECIPE_GENERATOR_V1_MARKER = "// ACM_LOCAL_RECIPE_GENERATOR_V1"
_LOCAL_RECIPE_GENERATOR_V2_MARKER = "// ACM_LOCAL_RECIPE_GENERATOR_V2"
_LOCAL_RECIPE_GENERATOR_MARKERS = (
    _LOCAL_RECIPE_GENERATOR_V1_MARKER,
    _LOCAL_RECIPE_GENERATOR_V2_MARKER,
)
# Historical private alias.
_LOCAL_RECIPE_GENERATOR_MARKER = _LOCAL_RECIPE_GENERATOR_V1_MARKER
_GENERATOR_ADAPTER_SYMBOL = "acm_generate_case"


def _ensure_explicit_main_return(source: str) -> str:
    """Preserve main's implicit return after it is renamed by the harness."""

    masked = list(source)
    index = 0
    state = "normal"
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if state == "normal":
            if char == "/" and following == "/":
                masked[index] = masked[index + 1] = " "
                state = "line"
                index += 2
                continue
            if char == "/" and following == "*":
                masked[index] = masked[index + 1] = " "
                state = "block"
                index += 2
                continue
            if char == '"':
                masked[index] = " "
                state = "string"
            elif char == "'":
                masked[index] = " "
                state = "char"
        elif state == "line":
            if char == "\n":
                state = "normal"
            else:
                masked[index] = " "
        elif state == "block":
            masked[index] = " "
            if char == "*" and following == "/":
                masked[index + 1] = " "
                state = "normal"
                index += 2
                continue
        else:
            masked[index] = " "
            if char == "\\":
                if index + 1 < len(source):
                    masked[index + 1] = " "
                    index += 2
                    continue
            elif (state == "string" and char == '"') or (
                state == "char" and char == "'"
            ):
                state = "normal"
        index += 1
    visible = "".join(masked)
    match = re.search(r"\bmain\s*\(", visible)
    if match is None:
        raise SourceSafetyError("generator source has no main entry point")
    opening = visible.find("{", match.end())
    if opening < 0:
        raise SourceSafetyError("generator main has no function body")
    depth = 0
    closing = -1
    for position in range(opening, len(visible)):
        if visible[position] == "{":
            depth += 1
        elif visible[position] == "}":
            depth -= 1
            if depth == 0:
                closing = position
                break
    if closing < 0:
        raise SourceSafetyError("generator main body is not balanced")
    return source[:closing] + "\nreturn 0;\n" + source[closing:]


def compose_trusted_generator_harness(source: str | bytes) -> str:
    """Wrap topic-specific generator code in the locally owned v2 protocol."""

    model_source = validate_cpp_source(source)
    if (
        _TRUSTED_GENERATOR_MARKER in model_source
        or any(marker in model_source for marker in _LEGACY_TRUSTED_GENERATOR_MARKERS)
    ):
        return model_source
    uses_adapter = bool(
        re.search(rf"\b{re.escape(_GENERATOR_ADAPTER_SYMBOL)}\s*\(", model_source)
    )
    uses_local_recipe_manifest = uses_adapter and any(
        marker in model_source for marker in _LOCAL_RECIPE_GENERATOR_MARKERS
    )
    local_recipe_manifest_version = (
        2 if _LOCAL_RECIPE_GENERATOR_V2_MARKER in model_source else 1
    )
    if not uses_adapter:
        # Compatibility is deliberately limited to already-existing/manual
        # helpers.  New AI artifacts are required to expose the adapter by the
        # generation layer, so provider repairs cannot rewrite argv/protocol
        # handling owned by this harness.
        model_source = _ensure_explicit_main_return(model_source)
    harness = r'''
#undef main
#include <cstdlib>
#include <cstring>
#include <functional>
#include <iostream>
#include <limits>
#include <string>
#include <type_traits>
namespace acm_trusted_harness {
bool decimal_seed(const char* text, unsigned long long& value) {
    if (text == nullptr || *text == '\0') return false;
    value = 0;
    for (const unsigned char* p = reinterpret_cast<const unsigned char*>(text); *p; ++p) {
        if (*p < '0' || *p > '9') return false;
        const unsigned digit = *p - '0';
        if (value > (std::numeric_limits<unsigned long long>::max() - digit) / 10) return false;
        value = value * 10 + digit;
    }
    return value <= static_cast<unsigned long long>(std::numeric_limits<long long>::max());
}
bool supported(const std::string& profile, const std::string& kind) {
    return (profile == "small" && (kind == "lower_bound" || kind == "random")) ||
           (profile == "large" && (kind == "upper_bound" || kind == "random"));
}
void set_protocol_env(const char* seed, const char* profile, const char* kind) {
#ifdef _WIN32
    ::_putenv_s("ACM_STRESS_SEED", seed);
    ::_putenv_s("ACM_STRESS_PROFILE", profile);
    ::_putenv_s("ACM_STRESS_CASE_KIND", kind);
    ::_putenv_s("ACM_STRESS_PROFILE_VERSION", "2");
#else
    ::setenv("ACM_STRESS_SEED", seed, 1);
    ::setenv("ACM_STRESS_PROFILE", profile, 1);
    ::setenv("ACM_STRESS_CASE_KIND", kind, 1);
    ::setenv("ACM_STRESS_PROFILE_VERSION", "2", 1);
#endif
}
template<class Function>
int invoke_model(Function function, int argc, char** argv) {
    if constexpr (std::is_invocable_r_v<int, Function, int, char**>) {
        return std::invoke(function, argc, argv);
    } else if constexpr (std::is_invocable_r_v<int, Function>) {
        return std::invoke(function);
    } else {
        return 70;
    }
}
}
int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "--capabilities") == 0) {
        std::cout << "{\"profile_version\":2,\"manifest_version\":ACM_TRUSTED_MANIFEST_VERSION,"
                     "\"profiles\":[\"small\",\"large\"],"
                     "\"case_kinds\":[\"lower_bound\",\"upper_bound\",\"random\"],"
                     "\"supported_cases\":[{\"profile\":\"small\",\"case_kind\":\"lower_bound\"},"
                     "{\"profile\":\"small\",\"case_kind\":\"random\"},"
                     "{\"profile\":\"large\",\"case_kind\":\"upper_bound\"},"
                     "{\"profile\":\"large\",\"case_kind\":\"random\"}]}";
        return 0;
    }
    ACM_TRUSTED_GENERATOR_MANIFEST_QUERY
    unsigned long long seed = 0;
    if (argc != 4 || !acm_trusted_harness::decimal_seed(argv[1], seed) ||
        !acm_trusted_harness::supported(argv[2], argv[3])) return 64;
    acm_trusted_harness::set_protocol_env(argv[1], argv[2], argv[3]);
    ACM_TRUSTED_GENERATOR_INVOKE
}
'''
    invocation = (
        "acm_generate_case(seed, std::string(argv[2]), std::string(argv[3]), std::cout);\n"
        "    return std::cout.good() ? 0 : 74;"
        if uses_adapter
        else "return acm_trusted_harness::invoke_model(&acm_generated_main, argc, argv);"
    )
    manifest_query = (
        'if (argc == 5 && std::strcmp(argv[1], "--manifest") == 0) {\n'
        '        unsigned long long manifest_seed = 0;\n'
        '        if (!acm_trusted_harness::decimal_seed(argv[2], manifest_seed) ||\n'
        '            !acm_trusted_harness::supported(argv[3], argv[4])) return 64;\n'
        '        acm_trusted_harness::set_protocol_env(argv[2], argv[3], argv[4]);\n'
        '        acm_generate_manifest(manifest_seed, std::string(argv[3]), '\
        'std::string(argv[4]), std::cout);\n'
        '        return std::cout.good() ? 0 : 74;\n'
        '    }'
        if uses_local_recipe_manifest
        else ""
    )
    harness = harness.replace(
        "ACM_TRUSTED_GENERATOR_MANIFEST_QUERY", manifest_query
    )
    harness = harness.replace(
        "ACM_TRUSTED_MANIFEST_VERSION", str(local_recipe_manifest_version)
    )
    harness = harness.replace("ACM_TRUSTED_GENERATOR_INVOKE", invocation)
    source_prefix = "" if uses_adapter else "#define main acm_generated_main\n"
    combined = (
        f"{_TRUSTED_GENERATOR_MARKER}\n"
        f"{source_prefix}"
        f"{model_source.rstrip()}\n"
        f"{harness.lstrip()}"
    )
    return validate_cpp_source(combined)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_GENERATOR_VALID_SEED_SEARCH_ATTEMPTS = 8
_SEED_MIX = 0x9E3779B97F4A7C15


def _derived_candidate_seed(requested_seed: int, attempt: int) -> int:
    """Return a deterministic 64-bit candidate seed for local validation."""

    if attempt <= 0:
        return int(requested_seed) & ((1 << 64) - 1)
    mixed = (int(requested_seed) + attempt * _SEED_MIX) & ((1 << 64) - 1)
    mixed ^= mixed >> 30
    mixed = (mixed * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
    mixed ^= mixed >> 27
    mixed = (mixed * 0x94D049BB133111EB) & ((1 << 64) - 1)
    return (mixed ^ (mixed >> 31)) & ((1 << 64) - 1)


def _small_record_deletion_candidates(
    payload: bytes, validator_stderr: bytes | str
) -> list[bytes]:
    """Propose bounded record edits; the validator remains authoritative."""

    if len(payload) > 256 * 1024:
        return []
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if len(lines) < 3 or len(lines) > 64:
        return []
    diagnostic = (
        validator_stderr.decode("utf-8", errors="replace")
        if isinstance(validator_stderr, bytes)
        else str(validator_stderr)
    )
    line_order: list[int] = []
    for operation in ("insert", "bottom", "top", "query", "ask"):
        if operation in diagnostic.casefold():
            line_order.extend(
                index
                for index, line in enumerate(lines[1:], 1)
                if line.strip().casefold().startswith(operation)
            )
    numbers = [int(item) for item in re.findall(r"(?<![A-Za-z])\d+", diagnostic)]
    if numbers:
        record_index = numbers[-1]
        for offset in (2, 1, 3, 0, 4):
            candidate_index = record_index + offset
            if 1 <= candidate_index < len(lines):
                line_order.append(candidate_index)
    line_order.extend(range(1, len(lines)))
    line_order = list(dict.fromkeys(line_order))

    count_slots: list[tuple[int, int, int]] = []
    for header_index in range(min(3, len(lines))):
        matches = list(re.finditer(r"(?<![A-Za-z0-9_])-?\d+", lines[header_index]))
        for match in reversed(matches):
            value = int(match.group())
            if value > 0:
                count_slots.append((header_index, match.start(), match.end()))
    candidates: list[bytes] = []
    seen: set[bytes] = set()
    # Preserve the operation family before considering deletion.  Replacing a
    # numeric argument with a small boundary value is deliberately
    # problem-agnostic; only the independent validator can accept the result.
    for changed_index in line_order:
        matches = list(re.finditer(r"(?<![A-Za-z0-9_])-?\d+", lines[changed_index]))
        for match in reversed(matches):
            original = int(match.group())
            for replacement in (0, 1, -1):
                if replacement == original:
                    continue
                changed = list(lines)
                changed[changed_index] = (
                    changed[changed_index][: match.start()]
                    + str(replacement)
                    + changed[changed_index][match.end() :]
                )
                candidate = ("\n".join(changed) + "\n").encode("utf-8")
                if candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
                if len(candidates) >= 32:
                    break
            if len(candidates) >= 32:
                break
        if len(candidates) >= 32:
            break
    for removed_index in line_order:
        for header_index, start, end in count_slots:
            if header_index == removed_index:
                continue
            changed = list(lines)
            value = int(changed[header_index][start:end])
            changed[header_index] = (
                changed[header_index][:start]
                + str(value - 1)
                + changed[header_index][end:]
            )
            del changed[removed_index]
            candidate = ("\n".join(changed) + "\n").encode("utf-8")
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
            if len(candidates) >= 64:
                return candidates
    return candidates


class _GeneratorManifestValidationError(ValueError):
    def __init__(self, message: str, *, expected: object, actual: object) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


def _strict_json_object(payload: bytes) -> dict[str, object]:
    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    text = payload.decode("utf-8")
    decoded = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    if not isinstance(decoded, dict):
        raise ValueError("top-level JSON value must be an object")
    return decoded


class _ValidatorRejectedInput(ValueError):
    def __init__(self, observation: Mapping[str, object]):
        self.observation = dict(observation)
        super().__init__("independent validator rejected the generated input")


def _parse_validator_observation(payload: bytes) -> dict[str, object]:
    """Parse the only validator success shape accepted by trusted code."""

    observation = _strict_json_object(payload)
    expected_keys = {"valid", "dimensions", "coverage_tags", "records"}
    if set(observation) != expected_keys:
        raise ValueError("validator rejected input or returned an invalid observation")
    if observation.get("valid") is False:
        if (
            observation.get("dimensions") == {}
            and observation.get("coverage_tags") == []
            and observation.get("records") == 0
        ):
            raise _ValidatorRejectedInput(observation)
        raise ValueError("validator returned a malformed rejection observation")
    if observation.get("valid") is not True:
        raise ValueError("validator observation valid flag must be boolean")
    dimensions = observation.get("dimensions")
    tags = observation.get("coverage_tags")
    records = observation.get("records")
    if (
        not isinstance(dimensions, dict)
        or any(
            not isinstance(key, str)
            or not key.strip()
            or type(value) is not int
            or value < 0
            for key, value in dimensions.items()
        )
        or not isinstance(tags, list)
        or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
        or type(records) is not int
        or records < 0
    ):
        raise ValueError("validator observation has invalid dimensions, tags, or records")
    # Coverage is set-valued.  Duplicate strings do not create witnesses and
    # are a presentation defect that the trusted harness can canonicalize
    # without inventing or accepting any new tag.  Unknown/missing tags are
    # still rejected by the downstream exact coverage gates.
    normalized = dict(observation)
    normalized["coverage_tags"] = list(dict.fromkeys(tags))
    return normalized


def _parse_generator_manifest(
    payload: bytes,
    *,
    profile: str,
    case_kind: str,
    seed: int,
    generated_input: bytes,
) -> dict[str, object]:
    try:
        manifest = _strict_json_object(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _GeneratorManifestValidationError(
            f"generator manifest is not strict JSON: {exc}",
            expected={"manifest_version": [1, 2]},
            actual=payload.decode("utf-8", errors="replace")[:1000],
        ) from exc

    manifest_version = manifest.get("manifest_version")
    if type(manifest_version) is not int or manifest_version not in {1, 2}:
        raise _GeneratorManifestValidationError(
            "generator manifest version is unsupported",
            expected={"manifest_version": [1, 2]},
            actual={"manifest_version": manifest_version},
        )
    expected_keys = (
        _GENERATOR_MANIFEST_V2_KEYS
        if manifest_version == 2
        else _GENERATOR_MANIFEST_V1_KEYS
    )
    expected_schema: dict[str, object] = {
        "keys": sorted(expected_keys),
        "manifest_version": manifest_version,
        "profile": profile,
        "case_kind": case_kind,
        "seed": int(seed),
        "input_sha256": sha256_bytes(generated_input),
        "dimensions": "object<string, non-negative integer>",
        "coverage_tags": "unique non-empty string array",
        "records": "non-negative integer",
        "total_complexity": "non-empty string",
    }
    if set(manifest) != expected_keys:
        raise _GeneratorManifestValidationError(
            "generator manifest has missing or unexpected fields",
            expected=sorted(expected_keys),
            actual=sorted(str(key) for key in manifest),
        )
    exact_fields = {
        "manifest_version": manifest_version,
        "profile": profile,
        "case_kind": case_kind,
        "seed": int(seed),
        "input_sha256": sha256_bytes(generated_input),
    }
    for key, expected in exact_fields.items():
        actual = manifest.get(key)
        type_valid = (
            type(actual) is int
            if key in {"manifest_version", "seed"}
            else isinstance(actual, str)
        )
        if not type_valid or actual != expected:
            raise _GeneratorManifestValidationError(
                f"generator manifest field {key!r} is inconsistent",
                expected={key: expected},
                actual={key: actual},
            )

    if manifest_version == 2:
        exact_v2_strings = {
            "engine": "local_templates_v2",
            "recipe_source": "deterministic_contract_shape_v2",
        }
        for key, expected in exact_v2_strings.items():
            if manifest.get(key) != expected:
                raise _GeneratorManifestValidationError(
                    f"generator manifest field {key!r} is inconsistent",
                    expected={key: expected},
                    actual={key: manifest.get(key)},
                )
        for key in ("state_machine", "section_family", "operation_family"):
            value = manifest.get(key)
            if not isinstance(value, str) or not value.strip():
                raise _GeneratorManifestValidationError(
                    f"generator manifest field {key!r} must be a non-empty string",
                    expected={key: "non-empty string"},
                    actual={key: value},
                )
        for key in ("planned_byte_bucket", "actual_byte_bucket"):
            value = manifest.get(key)
            if type(value) is not int or not 0 <= value <= 3:
                raise _GeneratorManifestValidationError(
                    f"generator manifest field {key!r} is invalid",
                    expected={key: "integer in [0,3]"},
                    actual={key: value},
                )

    dimensions = manifest.get("dimensions")
    if (
        not isinstance(dimensions, dict)
        or any(not isinstance(key, str) or not key for key in dimensions)
        or any(type(value) is not int or value < 0 for value in dimensions.values())
    ):
        raise _GeneratorManifestValidationError(
            "generator manifest dimensions must be non-negative integers",
            expected=expected_schema["dimensions"],
            actual=dimensions,
        )
    coverage_tags = manifest.get("coverage_tags")
    if (
        not isinstance(coverage_tags, list)
        or any(not isinstance(tag, str) or not tag.strip() for tag in coverage_tags)
        or len(set(coverage_tags)) != len(coverage_tags)
    ):
        raise _GeneratorManifestValidationError(
            "generator manifest coverage_tags are invalid",
            expected=expected_schema["coverage_tags"],
            actual=coverage_tags,
        )
    records = manifest.get("records")
    if type(records) is not int or records < 0:
        raise _GeneratorManifestValidationError(
            "generator manifest records must be a non-negative integer",
            expected=expected_schema["records"],
            actual=records,
        )
    total_complexity = manifest.get("total_complexity")
    if not isinstance(total_complexity, str) or not total_complexity.strip():
        raise _GeneratorManifestValidationError(
            "generator manifest total_complexity must be a non-empty string",
            expected=expected_schema["total_complexity"],
            actual=total_complexity,
        )
    if profile == "large":
        allowed = (
            ["linear_output"]
            if case_kind == "upper_bound"
            else ["linear_output", "output_log_n"]
        )
        if total_complexity not in allowed:
            raise _GeneratorManifestValidationError(
                "generator manifest declares an unsafe large-case complexity",
                expected={"total_complexity": allowed},
                actual={"total_complexity": total_complexity},
            )
    return manifest


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def _canonical_path_text(path: str | Path) -> str:
    """Return a comparison key that also normalizes Windows 8.3 aliases."""
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _path_is_within(path: str | Path, root: str | Path) -> bool:
    candidate = _canonical_path_text(path)
    boundary = _canonical_path_text(root)
    try:
        return os.path.commonpath((candidate, boundary)) == boundary
    except ValueError:
        return False


def _reject_link_components(path: Path, root: Path) -> None:
    """Reject symlinks and Windows junctions below the trusted workspace root."""
    absolute = path.absolute()
    root_absolute = root.absolute()
    boundary = root_absolute
    try:
        relative = absolute.relative_to(boundary)
    except ValueError:
        boundary = next(
            (
                ancestor
                for ancestor in (absolute, *absolute.parents)
                if not _is_link_or_reparse(ancestor)
                and ancestor.exists()
                and root_absolute.exists()
                and ancestor.samefile(root_absolute)
            ),
            None,
        )
        if boundary is None:
            raise ValueError("path escapes workspace")
        relative = absolute.relative_to(boundary)

    current = boundary
    for part in relative.parts:
        current /= part
        if _is_link_or_reparse(current):
            raise ValueError(f"managed path contains a link or reparse point: {current.name}")


def validate_cpp_source(source: str | bytes, *, max_bytes: int = MAX_CPP_BYTES) -> str:
    """Return normalized text or raise for code outside the safe subset."""
    raw = source.encode("utf-8") if isinstance(source, str) else bytes(source)
    if not raw:
        raise SourceSafetyError("C++ source is empty", rule_id="empty_source")
    if len(raw) > max_bytes:
        raise SourceSafetyError(
            f"C++ source exceeds {max_bytes} bytes", rule_id="source_size"
        )
    if b"\0" in raw:
        raise SourceSafetyError("C++ source contains NUL", rule_id="nul_byte")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceSafetyError(
            "C++ source must be UTF-8", rule_id="invalid_utf8"
        ) from exc

    def safety_location(match: re.Match[str]) -> dict[str, object]:
        start = match.start()
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", start)
        if line_end < 0:
            line_end = len(text)
        raw_excerpt = text[line_start:line_end]
        sanitized = re.sub(r'"(?:\\.|[^"\\])*"', '"<redacted>"', raw_excerpt)
        sanitized = re.sub(r"'(?:\\.|[^'\\])*'", "'<redacted>'", sanitized)
        sanitized = re.sub(r"//.*$", "// <redacted>", sanitized)
        excerpt = " ".join(sanitized.replace("\r", " ").split())[:240]
        token_match = re.search(r"[A-Za-z_]\w*", match.group(0))
        token = token_match.group(0) if token_match is not None else match.group(0).strip()
        return {
            "matched_token": token[:80],
            "line": text.count("\n", 0, start) + 1,
            "column": start - line_start + 1,
            "excerpt": excerpt,
        }

    for match in re.finditer(r"(?m)^\s*#\s*include\s*<([^>]+)>", text):
        header = match.group(1).strip()
        if header not in _ALLOWED_HEADERS:
            raise SourceSafetyError(
                f"non-standard or unsafe include: <{header}>",
                rule_id="unsafe_include",
                **safety_location(match),
            )
    include_lines = re.findall(r"(?m)^\s*#\s*include\b[^\r\n]*", text)
    parsed_lines = re.findall(r"(?m)^\s*#\s*include\s*<[^>]+>\s*(?://.*)?$", text)
    if len(include_lines) != len(parsed_lines):
        match = re.search(r"(?m)^\s*#\s*include\b[^\r\n]*", text)
        raise SourceSafetyError(
            "malformed or unsupported include",
            rule_id="malformed_include",
            **(safety_location(match) if match is not None else {}),
        )
    for rule_id, pattern, label in _DANGEROUS_PATTERNS:
        match = re.search(pattern, text)
        if match is not None:
            raise SourceSafetyError(
                f"C++ source uses forbidden {label}",
                rule_id=rule_id,
                **safety_location(match),
            )
    return text


@dataclass(frozen=True, slots=True, init=False)
class HelperSources:
    generator: str | bytes
    reference_primary: str | bytes
    reference_secondary: str | bytes
    validator: str | bytes | None = None

    def __init__(
        self,
        generator: str | bytes,
        reference_primary: str | bytes | None = None,
        reference_secondary: str | bytes | None = None,
        validator: str | bytes | None = None,
        *,
        brute: str | bytes | None = None,
        reference: str | bytes | None = None,
    ) -> None:
        """Create a dual-reference source set.

        ``brute`` and ``reference`` remain accepted as deprecated construction
        aliases so an in-flight legacy setup can be decoded, but staging always
        writes the values as ref1/ref2 and never creates a new legacy bundle.
        """
        primary = reference_primary if reference_primary is not None else brute
        secondary = reference_secondary if reference_secondary is not None else reference
        if primary is None or secondary is None:
            raise TypeError("both reference_primary and reference_secondary are required")
        if reference_primary is not None and brute is not None:
            raise TypeError("reference_primary and deprecated brute are mutually exclusive")
        if reference_secondary is not None and reference is not None:
            raise TypeError("reference_secondary and deprecated reference are mutually exclusive")
        object.__setattr__(self, "generator", generator)
        object.__setattr__(self, "reference_primary", primary)
        object.__setattr__(self, "reference_secondary", secondary)
        object.__setattr__(self, "validator", validator)

    @property
    def brute(self) -> str | bytes:
        return self.reference_primary

    @property
    def reference(self) -> str | bytes:
        return self.reference_secondary


@dataclass(slots=True)
class HelperBundle:
    bundle_id: str
    problem_id: str
    primary_source: str
    helper_paths: dict[str, str]
    baseline_hashes: dict[str, str | None]
    applied_hashes: dict[str, str]
    backup_dir: str
    staging_dir: str
    created_at: str
    oracle_protocol: str = DUAL_REFERENCE_PROTOCOL
    release_executables: dict[str, str] = field(default_factory=dict)
    validation: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class HelperPreflightConfig:
    contract_hash: str
    samples: Sequence["SampleCase"] = ()
    generator_blueprint: Mapping[str, object] | None = None
    validator_probes: Sequence[Mapping[str, object]] = ()
    include_large: bool = True
    exact_output: bool = False
    small_random_cases: int = 16
    generator_timeout: float = 2.0
    reference_timeout: float = 2.0
    brute_timeout: float = 5.0
    reference_primary_timeout: float | None = None
    reference_secondary_timeout: float | None = None
    validator_timeout: float = 2.0
    require_manifest: bool = True
    require_coverage: bool = True
    require_seed_variation: bool = True
    deadline: float | None = None
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if not str(self.contract_hash).strip():
            raise ValueError("preflight contract hash is required")
        if self.small_random_cases != 16:
            raise ValueError("helper preflight requires exactly 16 small random cases")
        if self.generator_blueprint is not None:
            if not isinstance(self.generator_blueprint, Mapping):
                raise ValueError("generator_blueprint must be a mapping")
            for key in (
                "required_coverage_tags",
                "large_required_coverage_tags",
            ):
                tags = self.generator_blueprint.get(key, [])
                if (
                    isinstance(tags, (str, bytes))
                    or not isinstance(tags, Sequence)
                    or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
                    or len({str(tag) for tag in tags}) != len(tags)
                ):
                    raise ValueError(
                        f"generator_blueprint.{key} must be a list of unique "
                        "non-empty strings"
                    )
        if isinstance(self.validator_probes, (str, bytes)) or not isinstance(
            self.validator_probes, Sequence
        ):
            raise ValueError("validator_probes must be a sequence")
        if len(self.validator_probes) > 6:
            raise ValueError("validator_probes must contain at most 6 pairs")
        probe_ids: set[str] = set()
        for probe in self.validator_probes:
            if not isinstance(probe, Mapping):
                raise ValueError("validator probe must be a mapping")
            probe_id = str(probe.get("id") or "").strip()
            constraint_id = str(probe.get("constraint_id") or "").strip()
            valid_input = probe.get("valid_input")
            invalid_input = probe.get("invalid_input")
            if (
                not probe_id
                or probe_id in probe_ids
                or not constraint_id
                or not isinstance(valid_input, str)
                or not isinstance(invalid_input, str)
                or not valid_input.strip()
                or not invalid_input.strip()
                or len(valid_input.encode("utf-8")) > 8192
                or len(invalid_input.encode("utf-8")) > 8192
            ):
                raise ValueError("validator probe is malformed")
            probe_ids.add(probe_id)
        if min(
            self.generator_timeout,
            (
                self.reference_primary_timeout
                if self.reference_primary_timeout is not None
                else self.brute_timeout
            ),
            (
                self.reference_secondary_timeout
                if self.reference_secondary_timeout is not None
                else self.reference_timeout
            ),
            self.validator_timeout,
        ) <= 0:
            raise ValueError("helper preflight timeouts must be positive")

    def reference_timeout_for(self, role: str) -> float:
        if role == "reference_primary":
            return float(
                self.reference_primary_timeout
                if self.reference_primary_timeout is not None
                else self.brute_timeout
            )
        if role == "reference_secondary":
            return float(
                self.reference_secondary_timeout
                if self.reference_secondary_timeout is not None
                else self.reference_timeout
            )
        raise KeyError(role)


@dataclass(slots=True)
class StagedHelperBundle:
    bundle_id: str
    problem_id: str
    primary_source: str
    helper_paths: dict[str, str]
    staged_paths: dict[str, str]
    release_executables: dict[str, str]
    audit_executables: dict[str, str]
    sanitizer_executables: dict[str, str]
    sanitizer_probe: str | None
    baseline_hashes: dict[str, str | None]
    applied_hashes: dict[str, str]
    backup_dir: str
    staging_dir: str
    created_at: str
    oracle_protocol: str = DUAL_REFERENCE_PROTOCOL
    preflight_completed: bool = False
    validation: dict[str, object] | None = None
    applied: bool = False


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_CPP_COMPILER_FALLBACKS: tuple[str, ...] = ("g++", "clang++", "c++")


def resolve_cpp_compiler(compiler: str) -> str | None:
    """Resolve ``compiler``, then any other C++ driver available locally.

    The configured default is ``g++``, which macOS does not ship; there the
    usable driver is ``clang++``.  Probing the alternatives keeps a missing
    ``g++`` from failing every compile on a machine whose toolchain is fine.
    """
    resolved = shutil.which(compiler)
    if resolved is not None:
        return resolved
    for candidate in _CPP_COMPILER_FALLBACKS:
        if candidate == compiler:
            continue
        fallback = shutil.which(candidate)
        if fallback is not None:
            return fallback
    return None


def portable_cpp_flags(flags: Sequence[str]) -> tuple[str, ...]:
    """Drop compile flags the local platform cannot honour.

    macOS has no static libc, so ``-static`` fails the link outright rather
    than degrading to a dynamic one.  Dropping it only there keeps the shared
    flag sets intact and leaves Windows and Linux byte-identical.
    """
    if sys.platform == "darwin":
        return tuple(flag for flag in flags if flag != "-static")
    return tuple(flags)


# The exact flag sets handed to the C++ driver for generated helpers.  The
# persisted preparation identity fingerprints these (see
# ``cpp_compiler_fingerprint``), so a cached bundle is only reused for a build
# that still matches — which requires the compile sites and the fingerprint to
# read the same constants rather than parallel copies.
CPP_STANDARD_FLAG = "-std=c++17"
RELEASE_COMPILE_FLAGS = ("-O2", "-static")
AUDIT_COMPILE_FLAGS = (
    "-O0", "-g", "-static", "-D_GLIBCXX_DEBUG", "-D_GLIBCXX_ASSERTIONS",
)
SANITIZER_COMPILE_FLAGS = (
    "-O1", "-g", "-fsanitize=address,undefined",
    "-fno-omit-frame-pointer", "-D_GLIBCXX_ASSERTIONS",
)
HELPER_COMPILE_FLAG_SETS = (
    (CPP_STANDARD_FLAG, *RELEASE_COMPILE_FLAGS),
    (CPP_STANDARD_FLAG, *AUDIT_COMPILE_FLAGS),
    (CPP_STANDARD_FLAG, *SANITIZER_COMPILE_FLAGS),
)


def cpp_compiler_fingerprint(
    compiler: str = "g++",
    *,
    flag_sets: Sequence[Sequence[str]] = (),
) -> str:
    """Fingerprint the driver that compilation will actually execute."""

    resolved = resolve_cpp_compiler(compiler)
    effective_flags = tuple(tuple(portable_cpp_flags(flags)) for flags in flag_sets)
    if resolved is None:
        payload = {
            "configured": str(compiler),
            "resolved": None,
            "flag_sets": effective_flags,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    path = Path(resolved)
    try:
        result = subprocess.run(
            [resolved, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=3.0,
            check=False,
            shell=False,
        )
        version = result.stdout[:4096]
    except (OSError, subprocess.TimeoutExpired):
        version = b"unavailable"
    try:
        file_stat = path.stat()
        identity = {
            "path": str(path.resolve()),
            "size": int(file_stat.st_size),
            "mtime_ns": int(file_stat.st_mtime_ns),
        }
    except OSError:
        identity = {"path": str(path)}
    payload = canonical_json_bytes(
        {
            "configured": str(compiler),
            "resolved": identity,
            "flag_sets": effective_flags,
        }
    )
    return hashlib.sha256(payload + b"\0" + version).hexdigest()


def _compile_cpp(
    compiler: str,
    source: Path,
    output: Path,
    *,
    timeout: float,
    flags: Sequence[str] = RELEASE_COMPILE_FLAGS,
) -> subprocess.CompletedProcess[bytes]:
    """Compile a statically checked source with a trusted local toolchain.

    Compilation is not routed through AppContainer because a no-capability
    container scoped to the staging directory cannot load an external GCC
    installation and its runtime dependencies.  The generated executable is
    still never run here; all execution remains behind ``SandboxBackend``.
    """
    resolved = resolve_cpp_compiler(compiler)
    if resolved is None:
        return subprocess.CompletedProcess(
            [compiler], 127, b"", f"compiler not found: {compiler}".encode()
        )
    command = [
        resolved, CPP_STANDARD_FLAG, *portable_cpp_flags(flags),
        str(source), "-o", str(output),
    ]
    try:
        return subprocess.run(
            command,
            cwd=source.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command,
            124,
            (exc.stdout or b"")[:MAX_STREAM_BYTES],
            ((exc.stderr or b"") + b"\ncompiler timed out")[:MAX_STREAM_BYTES],
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, b"", str(exc).encode())


class HelperBundleManager:
    """Validate, sandbox-compile, backup, and transactionally apply helpers."""

    def __init__(
        self,
        root: str | Path,
        sandbox: SandboxBackend,
        *,
        sandbox_factory: Callable[[], SandboxBackend] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.sandbox = sandbox
        self._sandbox_factory = sandbox_factory

    def _resolve_primary(self, primary_source: str | Path) -> Path:
        requested_primary = Path(primary_source)
        if not requested_primary.is_absolute():
            requested_primary = self.root / requested_primary
        _reject_link_components(requested_primary, self.root)
        primary = requested_primary.resolve()
        if not primary.is_file() or primary.suffix.lower() != ".cpp":
            raise ValueError("primary source must be an existing .cpp file")
        if not _path_is_within(primary, self.root) or primary.is_symlink():
            raise ValueError("primary source must be a non-symlink inside workspace")
        if primary.stem.endswith((".gen", ".bf", ".ref", ".ref1", ".ref2")):
            raise ValueError("primary source cannot be a stress helper")
        return primary

    @staticmethod
    def _compile_failure(
        role: str,
        mode: str,
        compiled: subprocess.CompletedProcess[bytes],
    ) -> HelperPreflightError:
        detail = (compiled.stdout + compiled.stderr).decode(errors="replace")[-2000:]
        return HelperPreflightError(
            f"{role} {mode} compilation failed: {detail}",
            artifact=role,
            profile="build",
            case_kind=mode,
            seed=0,
            stderr=compiled.stderr,
        )

    def stage(
        self,
        primary_source: str | Path,
        sources: HelperSources,
        *,
        compiler: str = "g++",
        compile_timeout: float = 15.0,
    ) -> StagedHelperBundle:
        """Validate and build helpers without changing any managed helper file."""
        capability = self.sandbox.probe()
        if not capability.available:
            raise SandboxUnavailableError(capability.reason)
        primary = self._resolve_primary(primary_source)
        problem_id = primary.stem

        bundle_id = uuid.uuid4().hex
        staging = self.root / ".acm" / "stress-staging" / bundle_id
        backup = self.root / ".acm" / "ai-backups" / "stress" / bundle_id
        staging.mkdir(parents=True, exist_ok=False)
        names = {
            "generator": f"{problem_id}.gen.cpp",
            "reference_primary": f"{problem_id}.ref1.cpp",
            "reference_secondary": f"{problem_id}.ref2.cpp",
        }
        staging_names = dict(names)
        raw_sources = {
            "generator": sources.generator,
            "reference_primary": sources.reference_primary,
            "reference_secondary": sources.reference_secondary,
        }
        if sources.validator is not None:
            staging_names["validator"] = f"{problem_id}.validator.cpp"
            raw_sources["validator"] = sources.validator
        staged_paths: dict[str, Path] = {}
        target_paths: dict[str, Path] = {}
        baseline: dict[str, str | None] = {}
        applied: dict[str, str] = {}
        release_executables: dict[str, str] = {}
        audit_executables: dict[str, str] = {}
        sanitizer_executables: dict[str, str] = {}
        for role, filename in staging_names.items():
            try:
                validated_text = validate_cpp_source(raw_sources[role])
                if role == "generator":
                    validated_text = compose_trusted_generator_harness(validated_text)
                validated = validated_text.encode("utf-8")
            except SourceSafetyError as exc:
                shutil.rmtree(staging, ignore_errors=True)
                failure = HelperPreflightError(
                    str(exc),
                    artifact=role,
                    profile="build",
                    case_kind="source_safety",
                    seed=0,
                )
                failure.details["source_safety"] = dict(exc.details)
                raise failure from exc
            staged = staging / filename
            staged.write_bytes(validated)
            staged_paths[role] = staged
            if role in names:
                target = primary.with_name(filename)
                if target.is_symlink():
                    shutil.rmtree(staging, ignore_errors=True)
                    raise ValueError(f"helper target is a symlink: {filename}")
                target_paths[role] = target
                baseline[role] = sha256_file(target) if target.is_file() else None
                applied[role] = sha256_bytes(validated)

        def compile_role(role: str) -> tuple[str, str, str]:
            suffix = ".exe" if os.name == "nt" else ""
            staged = staged_paths[role]
            release = staging / f"{role}.release{suffix}"
            compiled = _compile_cpp(
                compiler,
                staged,
                release,
                timeout=compile_timeout,
                flags=RELEASE_COMPILE_FLAGS,
            )
            if compiled.returncode != 0:
                raise self._compile_failure(role, "release", compiled)
            audit = staging / f"{role}.audit{suffix}"
            compiled = _compile_cpp(
                compiler,
                staged,
                audit,
                timeout=compile_timeout,
                flags=AUDIT_COMPILE_FLAGS,
            )
            if compiled.returncode != 0:
                raise self._compile_failure(role, "audit", compiled)
            return role, str(release), str(audit)

        try:
            with ThreadPoolExecutor(
                max_workers=len(staging_names), thread_name_prefix="stress-helper-build"
            ) as executor:
                futures = [executor.submit(compile_role, role) for role in staging_names]
                for future in as_completed(futures):
                    role, release, audit = future.result()
                    release_executables[role] = release
                    audit_executables[role] = audit
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        sanitizer_probe: str | None = None
        suffix = ".exe" if os.name == "nt" else ""
        probe_source = staging / "sanitizer-probe.cpp"
        probe_source.write_text("int main(){return 0;}\n", encoding="utf-8")
        probe_executable = staging / f"sanitizer-probe{suffix}"
        sanitizer_flags = SANITIZER_COMPILE_FLAGS
        probe_compile = _compile_cpp(
            compiler,
            probe_source,
            probe_executable,
            timeout=compile_timeout,
            flags=sanitizer_flags,
        )
        if probe_compile.returncode == 0:
            candidates: dict[str, str] = {}
            for role, staged in staged_paths.items():
                candidate = staging / f"{role}.san{suffix}"
                compiled = _compile_cpp(
                    compiler,
                    staged,
                    candidate,
                    timeout=compile_timeout,
                    flags=sanitizer_flags,
                )
                if compiled.returncode != 0:
                    candidates.clear()
                    break
                candidates[role] = str(candidate)
            if len(candidates) == len(staged_paths):
                sanitizer_probe = str(probe_executable)
                sanitizer_executables = candidates

        return StagedHelperBundle(
            bundle_id=bundle_id,
            problem_id=problem_id,
            primary_source=str(primary),
            helper_paths={role: str(path) for role, path in target_paths.items()},
            staged_paths={role: str(path) for role, path in staged_paths.items()},
            release_executables=release_executables,
            audit_executables=audit_executables,
            sanitizer_executables=sanitizer_executables,
            sanitizer_probe=sanitizer_probe,
            baseline_hashes=baseline,
            applied_hashes=applied,
            backup_dir=str(backup),
            staging_dir=str(staging),
            created_at=datetime.now().astimezone().isoformat(),
            oracle_protocol=DUAL_REFERENCE_PROTOCOL,
        )

    @staticmethod
    def _preflight_seed(problem_id: str, contract_hash: str) -> int:
        digest = hashlib.sha256(
            f"{problem_id}\0{contract_hash}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big") % ((1 << 63) - 64)

    def _preflight_run(
        self,
        executable: str,
        *,
        cwd: Path,
        artifact: str,
        profile: str,
        case_kind: str,
        seed: int,
        timeout: float,
        input_data: bytes | None = None,
        env: Mapping[str, str] | None = None,
        args: Sequence[str] = (),
        stdin_bytes: int = MAX_STREAM_BYTES,
        stdout_bytes: int = MAX_STREAM_BYTES,
        stderr_bytes: int = MAX_STREAM_BYTES,
        sandbox: SandboxBackend | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> SandboxProcessResult:
        if deadline is not None:
            remaining = float(deadline) - float(clock())
            if remaining <= 0:
                raise HelperPreflightError(
                    "helper preflight exceeded the preparation deadline",
                    artifact=artifact,
                    profile=profile,
                    case_kind=case_kind,
                    seed=seed,
                    code="stress_prepare_budget_exhausted",
                )
            timeout = min(float(timeout), remaining)
        selected_sandbox = sandbox or self.sandbox
        input_size = len(input_data or b"")
        # The generator may legally emit up to MAX_LARGE_INPUT_BYTES for large
        # profiles, and every downstream validator/oracle run feeds that output
        # as its stdin.  Size the stdin budget to the actual input (capped at
        # the generator's own output ceiling) instead of leaving the 2 MiB
        # default that turns a legal large case into an unrepaired StressError.
        selected_stdin_bytes = min(
            MAX_LARGE_INPUT_BYTES,
            max(int(stdin_bytes), input_size + 64 * 1024),
        )
        result = selected_sandbox.run(
            [str(executable), *args],
            cwd=cwd,
            input_data=input_data,
            env=env,
            limits=SandboxLimits(
                timeout_seconds=timeout,
                stdin_bytes=selected_stdin_bytes,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
            ),
        )
        failure = _result_failure_kind(artifact, result)
        if failure is not None:
            raw_input = bytes(input_data or b"")
            if len(raw_input) <= 2_400:
                input_excerpt = raw_input.decode("utf-8", errors="replace")
            else:
                input_excerpt = (
                    raw_input[:1_600].decode("utf-8", errors="replace")
                    + "\n...[middle omitted]...\n"
                    + raw_input[-800:].decode("utf-8", errors="replace")
                )
            raise HelperPreflightError(
                f"{artifact} preflight failed: {failure}",
                artifact=artifact,
                profile=profile,
                case_kind=case_kind,
                seed=seed,
                stderr=result.stderr,
                expected={"returncode": 0, "failure": None},
                actual={
                    "returncode": int(result.returncode),
                    "failure": failure,
                    "input_sha256": sha256_bytes(raw_input),
                    "input_excerpt": input_excerpt,
                    "input_truncated": len(raw_input) > 2_400,
                },
            )
        return result

    def _preflight_generate(
        self,
        executable: str,
        *,
        cwd: Path,
        profile: str,
        case_kind: str,
        seed: int,
        timeout: float,
        sandbox: SandboxBackend | None = None,
        max_input_bytes: int | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> SandboxProcessResult:
        large = profile == "large"
        output_limit = (
            MAX_LARGE_INPUT_BYTES
            if large
            else int(max_input_bytes or MAX_STREAM_BYTES)
        )
        environment = {
            "ACM_STRESS_SEED": str(seed),
            "ACM_STRESS_PROFILE": profile,
            "ACM_STRESS_CASE_KIND": case_kind,
            "ACM_STRESS_PROFILE_VERSION": "2",
        }
        if not large and max_input_bytes is not None:
            environment["ACM_STRESS_MAX_INPUT_BYTES"] = str(max_input_bytes)
        result = self._preflight_run(
            executable,
            cwd=cwd,
            artifact="generator",
            profile=profile,
            case_kind=case_kind,
            seed=seed,
            timeout=timeout,
            env=environment,
            args=(str(seed), profile, case_kind),
            stdout_bytes=output_limit,
            sandbox=sandbox,
            deadline=deadline,
            clock=clock,
        )
        if not result.stdout:
            raise HelperPreflightError(
                "generator preflight produced empty input",
                artifact="generator",
                profile=profile,
                case_kind=case_kind,
                seed=seed,
                stderr=result.stderr,
            )
        return result

    def _preflight_generator_manifest(
        self,
        executable: str,
        *,
        cwd: Path,
        profile: str,
        case_kind: str,
        seed: int,
        generated_input: bytes,
        max_input_bytes: int | None = None,
        timeout: float,
        sandbox: SandboxBackend | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, object]:
        args = ("--manifest", str(seed), profile, case_kind)
        environment = {
            "ACM_STRESS_QUERY": "manifest",
            "ACM_STRESS_SEED": str(seed),
            "ACM_STRESS_PROFILE": profile,
            "ACM_STRESS_CASE_KIND": case_kind,
            "ACM_STRESS_PROFILE_VERSION": "2",
        }
        if profile == "small" and max_input_bytes is not None:
            environment["ACM_STRESS_MAX_INPUT_BYTES"] = str(max_input_bytes)
        try:
            result = self._preflight_run(
                executable,
                cwd=cwd,
                artifact="generator",
                profile=profile,
                case_kind=case_kind,
                seed=seed,
                timeout=timeout,
                env=environment,
                args=args,
                stdout_bytes=256 * 1024,
                sandbox=sandbox,
                deadline=deadline,
                clock=clock,
            )
        except HelperPreflightError as exc:
            raise HelperPreflightError(
                "generator manifest query failed",
                artifact="generator",
                profile=profile,
                case_kind=case_kind,
                seed=seed,
                stderr=str(exc.details.get("stderr") or ""),
                code="stress_generator_coverage_failed",
                expected={"command": list(args), "returncode": 0},
                actual={"failure": str(exc), **exc.details},
            ) from exc
        try:
            return _parse_generator_manifest(
                result.stdout,
                profile=profile,
                case_kind=case_kind,
                seed=seed,
                generated_input=generated_input,
            )
        except _GeneratorManifestValidationError as exc:
            raise HelperPreflightError(
                str(exc),
                artifact="generator",
                profile=profile,
                case_kind=case_kind,
                seed=seed,
                stderr=result.stderr,
                code="stress_generator_coverage_failed",
                expected=exc.expected,
                actual=exc.actual,
            ) from exc

    def _preflight_validator_observation(
        self,
        executable: str,
        *,
        cwd: Path,
        profile: str,
        case_kind: str,
        seed: int,
        generated_input: bytes,
        timeout: float,
        sandbox: SandboxBackend | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, object]:
        result = self._preflight_run(
            executable,
            cwd=cwd,
            artifact="validator",
            profile=profile,
            case_kind=case_kind,
            seed=seed,
            timeout=timeout,
            input_data=generated_input,
            env={
                "ACM_STRESS_SEED": str(seed),
                "ACM_STRESS_PROFILE": profile,
                "ACM_STRESS_CASE_KIND": case_kind,
                "ACM_STRESS_PROFILE_VERSION": "2",
            },
            stdout_bytes=256 * 1024,
            sandbox=sandbox,
            deadline=deadline,
            clock=clock,
        )
        try:
            observation = _parse_validator_observation(result.stdout)
        except _ValidatorRejectedInput as exc:
            # A well-formed independent rejection is evidence against the
            # generated input.  Official samples do not involve the generator,
            # so their rejection still belongs to the validator.
            generated_case = profile in {"small", "large"}
            raise HelperPreflightError(
                "independent validator rejected the generated input",
                artifact="generator" if generated_case else "validator",
                profile=profile,
                case_kind=case_kind,
                seed=seed,
                stderr=result.stderr,
                code=(
                    "stress_generated_input_invalid"
                    if generated_case
                    else "stress_input_validation_failed"
                ),
                expected={"valid": True},
                actual={
                    **exc.observation,
                    "generated_input_sha256": sha256_bytes(generated_input),
                    "generated_input_excerpt": (
                        generated_input.decode("utf-8", errors="replace")
                        if len(generated_input) <= 2_400
                        else (
                            generated_input[:1_600].decode(
                                "utf-8", errors="replace"
                            )
                            + "\n...[middle omitted]...\n"
                            + generated_input[-800:].decode(
                                "utf-8", errors="replace"
                            )
                        )
                    ),
                    "generated_input_truncated": len(generated_input) > 2_400,
                },
            ) from exc
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise HelperPreflightError(
                str(exc) or "validator did not return strict JSON",
                artifact="validator",
                profile=profile,
                case_kind=case_kind,
                seed=seed,
                stderr=result.stderr,
                code="stress_input_validation_failed",
            ) from exc
        return dict(observation)

    def _preflight_validator_probes(
        self,
        executable: str,
        *,
        cwd: Path,
        probes: Sequence[Mapping[str, object]],
        seed: int,
        timeout: float,
        sandbox: SandboxBackend | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> list[dict[str, object]]:
        """Require both sides of every independently certified hidden probe."""

        results: list[dict[str, object]] = []
        for index, probe in enumerate(probes):
            probe_id = str(probe.get("id") or f"probe-{index + 1}")
            constraint_id = str(probe.get("constraint_id") or "")
            probe_seed = seed + index
            valid_input = str(probe.get("valid_input") or "").encode("utf-8")
            invalid_input = str(probe.get("invalid_input") or "").encode("utf-8")

            def observe(label: str, input_data: bytes) -> tuple[bool, dict[str, object], bytes]:
                run = self._preflight_run(
                    executable,
                    cwd=cwd,
                    artifact="validator",
                    profile="contract_probe",
                    case_kind=f"{probe_id}:{label}",
                    seed=probe_seed,
                    timeout=timeout,
                    input_data=input_data,
                    env={
                        "ACM_STRESS_SEED": str(probe_seed),
                        "ACM_STRESS_PROFILE": "contract_probe",
                        "ACM_STRESS_CASE_KIND": f"{probe_id}:{label}",
                        "ACM_STRESS_PROFILE_VERSION": "2",
                    },
                    stdout_bytes=256 * 1024,
                    sandbox=sandbox,
                    deadline=deadline,
                    clock=clock,
                )
                try:
                    return True, dict(_parse_validator_observation(run.stdout)), run.stderr
                except _ValidatorRejectedInput as exc:
                    return False, dict(exc.observation), run.stderr
                except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                    raise HelperPreflightError(
                        str(exc) or "validator returned malformed contract-probe output",
                        artifact="validator",
                        profile="contract_probe",
                        case_kind="hidden:malformed",
                        seed=probe_seed,
                        stderr=run.stderr,
                        code="stress_input_validation_failed",
                        expected={"constraint_id": constraint_id},
                        actual={
                            "probe_source": "independently_certified_contract",
                            "side": label,
                            "stdout_excerpt": run.stdout.decode(
                                "utf-8", errors="replace"
                            )[:1000],
                        },
                    ) from exc

            valid_accepted, valid_actual, valid_stderr = observe("valid", valid_input)
            invalid_accepted, invalid_actual, invalid_stderr = observe(
                "invalid", invalid_input
            )
            truth_table = {
                "probe_source": "independently_certified_contract",
                "constraint_id": constraint_id,
                "valid_accepted": valid_accepted,
                "invalid_accepted": invalid_accepted,
                "valid_observation": valid_actual,
                "invalid_observation": invalid_actual,
                "valid_input_sha256": sha256_bytes(valid_input),
                "invalid_input_sha256": sha256_bytes(invalid_input),
            }
            if not valid_accepted:
                raise HelperPreflightError(
                    "validator rejected the valid side of an independently certified dynamic-constraint probe",
                    artifact="validator",
                    profile="contract_probe",
                    case_kind="hidden:valid",
                    seed=probe_seed,
                    stderr=valid_stderr,
                    code="stress_validator_positive_probe_failed",
                    expected={
                        "valid_accepted": True,
                        "constraint_id": constraint_id,
                    },
                    actual=truth_table,
                )
            if invalid_accepted:
                raise HelperPreflightError(
                    "validator accepted the invalid side of an independently certified dynamic-constraint probe",
                    artifact="validator",
                    profile="contract_probe",
                    case_kind="hidden:invalid",
                    seed=probe_seed,
                    stderr=invalid_stderr,
                    code="stress_validator_negative_probe_failed",
                    expected={
                        "invalid_rejected": True,
                        "constraint_id": constraint_id,
                    },
                    actual=truth_table,
                )
            results.append(
                {
                    "id": probe_id,
                    "constraint_id": constraint_id,
                    "valid_accepted": True,
                    "invalid_rejected": True,
                }
            )
        return results

    def _preflight_sanitize_small_input(
        self,
        executable: str,
        *,
        cwd: Path,
        case_kind: str,
        seed: int,
        generated_input: bytes,
        rejection: HelperPreflightError,
        timeout: float,
        sandbox: SandboxBackend | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[bytes, dict[str, object], dict[str, object]] | None:
        """Delete bounded small-case records only when the validator proves validity."""

        current = generated_input
        current_rejection = rejection
        seen = {sha256_bytes(generated_input)}
        for edit_round in range(1, 9):
            diagnostic = str(current_rejection.details.get("stderr") or "")
            actual = current_rejection.details.get("actual")
            if actual is not None:
                diagnostic += "\n" + json.dumps(actual, ensure_ascii=False, default=str)
            next_rejection: tuple[bytes, HelperPreflightError] | None = None
            for candidate in _small_record_deletion_candidates(current, diagnostic):
                digest = sha256_bytes(candidate)
                if digest in seen:
                    continue
                seen.add(digest)
                try:
                    observation = self._preflight_validator_observation(
                        executable,
                        cwd=cwd,
                        profile="small",
                        case_kind=case_kind,
                        seed=seed,
                        generated_input=candidate,
                        timeout=timeout,
                        sandbox=sandbox,
                        deadline=deadline,
                        clock=clock,
                    )
                except HelperPreflightError as exc:
                    if exc.code == "stress_prepare_budget_exhausted":
                        raise
                    if exc.code == "stress_generated_input_invalid" and next_rejection is None:
                        next_rejection = (candidate, exc)
                    continue
                return candidate, observation, {
                    "kind": "validator_guided_record_deletion",
                    "edits": edit_round,
                    "original_input_sha256": sha256_bytes(generated_input),
                    "accepted_input_sha256": digest,
                    "validator_checked_candidates": len(seen) - 1,
                }
            if next_rejection is None:
                return None
            current, current_rejection = next_rejection
        return None

    @staticmethod
    def _blueprint_case_complexity(
        blueprint: Mapping[str, object] | None, profile: str, case_kind: str
    ) -> str:
        cases = blueprint.get("cases") if isinstance(blueprint, Mapping) else None
        if isinstance(cases, Sequence) and not isinstance(cases, (str, bytes)):
            for item in cases:
                if (
                    isinstance(item, Mapping)
                    and item.get("profile") == profile
                    and item.get("case_kind") == case_kind
                    and isinstance(item.get("total_complexity"), str)
                ):
                    value = str(item["total_complexity"]).casefold()
                    return "output_log_n" if "log" in value else "linear_output"
        return "linear_output"

    def preflight(
        self,
        staged: StagedHelperBundle,
        config: HelperPreflightConfig,
        *,
        progress: Callable[[str, str, int, int], None] | None = None,
    ) -> dict[str, object]:
        """Execute staged oracles only; user solution code is never involved."""
        capability = self.sandbox.probe()
        if not capability.available:
            raise SandboxUnavailableError(capability.reason)
        cwd = Path(staged.staging_dir).resolve()
        seed0 = self._preflight_seed(staged.problem_id, config.contract_hash)
        audit = dict(staged.audit_executables)
        sanitizer_status = "unavailable"
        if staged.sanitizer_probe and staged.sanitizer_executables:
            smoke = self.sandbox.run(
                [staged.sanitizer_probe],
                cwd=cwd,
                limits=SandboxLimits(timeout_seconds=2.0, stdout_bytes=64 * 1024),
            )
            if smoke.ok:
                audit = dict(staged.sanitizer_executables)
                sanitizer_status = "enabled"
            else:
                sanitizer_status = "sandbox_probe_failed"

        try:
            capability_timeout = config.generator_timeout
            if config.deadline is not None:
                remaining = float(config.deadline) - float(config.clock())
                if remaining <= 0:
                    raise HelperPreflightError(
                        "helper preflight exceeded the preparation deadline",
                        artifact="generator",
                        profile="capability",
                        case_kind="capability",
                        seed=seed0,
                        code="stress_prepare_budget_exhausted",
                    )
                capability_timeout = min(capability_timeout, remaining)
            capabilities = probe_generator_v2(
                self.sandbox,
                audit["generator"],
                cwd=cwd,
                timeout=capability_timeout,
            )
        except GeneratorCapabilityError as exc:
            raise HelperPreflightError(
                str(exc),
                artifact="generator",
                profile="capability",
                case_kind="capability",
                seed=seed0,
                code=exc.code,
                expected=exc.details.get("expected", _DETAIL_UNSET),
                actual=exc.details.get("actual", _DETAIL_UNSET),
            ) from exc

        validator_probe_results: list[dict[str, object]] = []
        if config.validator_probes:
            if "validator" not in audit:
                raise HelperPreflightError(
                    "contract validator probes require an independent validator",
                    artifact="validator",
                    profile="contract_probe",
                    case_kind="missing",
                    seed=seed0,
                    code="stress_input_validation_failed",
                )
            validator_probe_results = self._preflight_validator_probes(
                audit["validator"],
                cwd=cwd,
                probes=config.validator_probes,
                seed=seed0,
                timeout=config.validator_timeout,
                deadline=config.deadline,
                clock=config.clock,
            )

        records: list[dict[str, object]] = []
        blueprint = dict(config.generator_blueprint or {})
        local_recipe = blueprint.get("engine") in {
            "local_templates_v1", "local_templates_v2"
        }
        small_required_tags = {
            str(tag) for tag in blueprint.get("required_coverage_tags", [])
        }
        large_required_tags = {
            str(tag) for tag in blueprint.get("large_required_coverage_tags", [])
        }
        small_observed_tags: set[str] = set()
        large_observed_tags: set[str] = set()
        for sample_index, sample in enumerate(config.samples, 1):
            if progress:
                progress("sample", sample.name, sample_index, len(config.samples))
            if "validator" in audit:
                self._preflight_validator_observation(
                    audit["validator"],
                    cwd=cwd,
                    profile="sample",
                    case_kind="official_sample",
                    seed=seed0,
                    generated_input=sample.input_data,
                    timeout=config.validator_timeout,
                    deadline=config.deadline,
                    clock=config.clock,
                )
            for role in ("reference_primary", "reference_secondary"):
                try:
                    result = self._preflight_run(
                        audit[role], cwd=cwd, artifact=role,
                        profile=f"sample:{sample.name}", case_kind="official_sample",
                        seed=seed0, timeout=config.reference_timeout_for(role),
                        input_data=sample.input_data,
                        deadline=config.deadline, clock=config.clock,
                    )
                except HelperPreflightError as exc:
                    exc.details.update(
                        {
                            "sample_name": str(sample.name)[:200],
                            "input_excerpt": sample.input_data.decode(
                                "utf-8", errors="replace"
                            )[:2000],
                            "expected_stdout": sample.expected_output.decode(
                                "utf-8", errors="replace"
                            )[:2000],
                        }
                    )
                    raise
                if not _equal(result.stdout, sample.expected_output, config.exact_output):
                    sample_name = str(sample.name)[:200]
                    input_excerpt = sample.input_data.decode(
                        "utf-8", errors="replace"
                    )[:2000]
                    expected_stdout = sample.expected_output.decode(
                        "utf-8", errors="replace"
                    )[:2000]
                    actual_stdout = result.stdout.decode(
                        "utf-8", errors="replace"
                    )[:2000]
                    failure = HelperPreflightError(
                        f"{role} output disagrees with official sample",
                        artifact=role,
                        profile=f"sample:{sample.name}",
                        case_kind="official_sample",
                        seed=seed0,
                        stderr=result.stderr,
                        code="stress_reference_sample_mismatch",
                        expected={
                            "sample_name": sample_name,
                            "stdout": expected_stdout,
                        },
                        actual={
                            "sample_name": sample_name,
                            "stdout": actual_stdout,
                        },
                    )
                    failure.details.update(
                        {
                            "sample_name": sample_name,
                            "input_excerpt": input_excerpt,
                            "expected_stdout": expected_stdout,
                            "actual_stdout": actual_stdout,
                        }
                    )
                    raise failure
            records.append({"profile": "sample", "case_kind": sample.name})

        def run_case(
            profile: str,
            case_kind: str,
            seed: int,
            *,
            sandbox: SandboxBackend,
        ) -> tuple[bytes, dict[str, object]]:
            requested_seed = seed
            # Certification must prove the generator's requested seed itself.
            # Searching another seed or deleting rejected records would certify
            # an input the generator did not actually produce, allowing the
            # same bundle to fault immediately after application.
            seed_attempts = 1
            input_transform: dict[str, object] | None = None
            for seed_attempt in range(seed_attempts):
                seed = _derived_candidate_seed(requested_seed, seed_attempt)
                max_input_bytes = (
                    SMALL_INPUT_INITIAL_BYTES if profile == "small" else None
                )
                size_attempts = 0
                while True:
                    size_attempts += 1
                    try:
                        generated = self._preflight_generate(
                            audit["generator"], cwd=cwd, profile=profile,
                            case_kind=case_kind, seed=seed,
                            timeout=config.generator_timeout, sandbox=sandbox,
                            max_input_bytes=max_input_bytes,
                            deadline=config.deadline, clock=config.clock,
                        )
                    except HelperPreflightError as exc:
                        retryable = (
                            profile == "small"
                            and not local_recipe
                            and (
                                "generator_output_limit" in str(exc)
                                or "empty input" in str(exc)
                            )
                        )
                        if not retryable or max_input_bytes is None:
                            raise
                        if max_input_bytes >= MAX_STREAM_BYTES:
                            exc.details["small_input_attempts"] = size_attempts
                            exc.details["max_input_bytes"] = max_input_bytes
                            raise
                        max_input_bytes = SMALL_INPUT_CEILING_BYTES
                        continue
                    generated_input = generated.stdout
                    input_transform = None
                    if "validator" not in audit:
                        observation = None
                        break
                    try:
                        observation = self._preflight_validator_observation(
                            audit["validator"], cwd=cwd, profile=profile,
                            case_kind=case_kind, seed=seed,
                            generated_input=generated_input,
                            timeout=config.validator_timeout, sandbox=sandbox,
                            deadline=config.deadline, clock=config.clock,
                        )
                        break
                    except HelperPreflightError as exc:
                        if (
                            exc.code != "stress_generated_input_invalid"
                            or profile != "small"
                            or local_recipe
                            or max_input_bytes is None
                        ):
                            raise
                        if max_input_bytes >= MAX_STREAM_BYTES:
                            exc.details["small_input_attempts"] = size_attempts
                            exc.details["max_input_bytes"] = max_input_bytes
                            raise
                        max_input_bytes = SMALL_INPUT_CEILING_BYTES
                break
            else:  # pragma: no cover - the loop always breaks or raises
                raise AssertionError("seed search must break or raise")
            if local_recipe:
                manifest = self._preflight_generator_manifest(
                    audit["generator"],
                    cwd=cwd,
                    profile=profile,
                    case_kind=case_kind,
                    seed=seed,
                    generated_input=generated_input,
                    max_input_bytes=max_input_bytes,
                    timeout=config.generator_timeout,
                    sandbox=sandbox,
                    deadline=config.deadline,
                    clock=config.clock,
                )
            elif "validator" in audit:
                assert observation is not None
                manifest_payload = {
                    "manifest_version": 1,
                    "seed": seed,
                    "profile": profile,
                    "case_kind": case_kind,
                    "input_sha256": sha256_bytes(generated_input),
                    "dimensions": observation["dimensions"],
                    "coverage_tags": observation["coverage_tags"],
                    "records": observation["records"],
                    "total_complexity": self._blueprint_case_complexity(
                        blueprint, profile, case_kind
                    ),
                }
                try:
                    manifest = _parse_generator_manifest(
                        json.dumps(
                            manifest_payload, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8"),
                        profile=profile,
                        case_kind=case_kind,
                        seed=seed,
                        generated_input=generated_input,
                    )
                except _GeneratorManifestValidationError as exc:
                    raise HelperPreflightError(
                        str(exc),
                        artifact="validator",
                        profile=profile,
                        case_kind=case_kind,
                        seed=seed,
                        code="stress_input_validation_failed",
                        expected=exc.expected,
                        actual=exc.actual,
                    ) from exc
            else:
                if not config.require_manifest:
                    manifest = None
                else:
                    manifest = self._preflight_generator_manifest(
                        audit["generator"],
                        cwd=cwd,
                        profile=profile,
                        case_kind=case_kind,
                        seed=seed,
                        generated_input=generated_input,
                        max_input_bytes=max_input_bytes,
                        timeout=config.generator_timeout,
                        sandbox=sandbox,
                        deadline=config.deadline,
                        clock=config.clock,
                    )
            limits = MAX_LARGE_INPUT_BYTES if profile == "large" else MAX_STREAM_BYTES
            output_limit = MAX_LARGE_OUTPUT_BYTES if profile == "large" else MAX_STREAM_BYTES
            reference_primary = self._preflight_run(
                audit["reference_primary"], cwd=cwd, artifact="reference_primary",
                profile=profile, case_kind=case_kind, seed=seed,
                timeout=config.reference_timeout_for("reference_primary"),
                input_data=generated_input,
                stdin_bytes=limits, stdout_bytes=output_limit, stderr_bytes=output_limit,
                sandbox=sandbox,
                deadline=config.deadline, clock=config.clock,
            )
            reference_secondary = self._preflight_run(
                audit["reference_secondary"], cwd=cwd, artifact="reference_secondary",
                profile=profile, case_kind=case_kind, seed=seed,
                timeout=config.reference_timeout_for("reference_secondary"),
                input_data=generated_input,
                stdin_bytes=limits, stdout_bytes=output_limit,
                stderr_bytes=output_limit,
                sandbox=sandbox,
                deadline=config.deadline, clock=config.clock,
            )
            if not _equal(
                reference_primary.stdout,
                reference_secondary.stdout,
                config.exact_output,
            ):
                raise HelperPreflightError(
                    "references disagree during preflight",
                    artifact="oracle",
                    profile=profile,
                    case_kind=case_kind,
                    seed=seed,
                    stderr=(reference_primary.stderr + b"\n" + reference_secondary.stderr),
                    code="stress_oracle_preflight_conflict",
                    expected={
                        "oracle": "reference_primary",
                        "stdout": reference_primary.stdout.decode(
                            "utf-8", errors="replace"
                        )[:2000],
                        "input_excerpt": generated_input.decode(
                            "utf-8", errors="replace"
                        )[:2000],
                    },
                    actual={
                        "oracle": "reference_secondary",
                        "stdout": reference_secondary.stdout.decode(
                            "utf-8", errors="replace"
                        )[:2000],
                    },
                )
            record: dict[str, object] = {
                "profile": profile,
                "case_kind": case_kind,
                "seed": seed,
                "requested_seed": requested_seed,
                "seed_search_attempts": seed_attempt + 1,
                "small_input_attempts": size_attempts if profile == "small" else 1,
                "max_input_bytes": (
                    max_input_bytes if profile == "small" else MAX_LARGE_INPUT_BYTES
                ),
                "input_bytes": len(generated_input),
                "input_sha256": sha256_bytes(generated_input),
                "generator_manifest": manifest,
            }
            if input_transform is not None:
                record["input_transform"] = input_transform
            return generated_input, record

        blueprint_cases = blueprint.get("cases", [])
        has_lower_bound = any(
            isinstance(item, Mapping)
            and item.get("profile") == "small"
            and item.get("case_kind") == "lower_bound"
            for item in blueprint_cases
        ) if isinstance(blueprint_cases, Sequence) else False
        total_cases = (
            config.small_random_cases
            + (1 if has_lower_bound else 0)
            + (2 if config.include_large else 0)
        )
        if has_lower_bound:
            if progress:
                progress("small", "lower_bound", 1, total_cases)
            _lower_output, lower_record = run_case(
                "small", "lower_bound", seed0, sandbox=self.sandbox
            )
            records.append(lower_record)

        first_output, _first_record = run_case(
            "small", "random", seed0, sandbox=self.sandbox
        )
        repeated_output, _repeated_record = run_case(
            "small", "random", seed0, sandbox=self.sandbox
        )
        if first_output != repeated_output:
            raise HelperPreflightError(
                "generator output is not deterministic for the same seed",
                artifact="generator", profile="small", case_kind="determinism",
                seed=seed0,
            )

        random_results: dict[int, tuple[bytes, dict[str, object]]] = {}
        if self._sandbox_factory is None:
            for offset in range(1, config.small_random_cases + 1):
                if progress:
                    progress("small", "random", offset, config.small_random_cases)
                random_results[offset] = run_case(
                    "small", "random", seed0 + offset, sandbox=self.sandbox
                )
        else:
            worker_state = threading.local()

            def run_random(offset: int) -> tuple[int, bytes, dict[str, object]]:
                sandbox = getattr(worker_state, "sandbox", None)
                if sandbox is None:
                    sandbox = self._sandbox_factory()
                    worker_state.sandbox = sandbox
                result, record = run_case(
                    "small", "random", seed0 + offset, sandbox=sandbox
                )
                return offset, result, record

            # The Windows AppContainer launcher uses a shared restricted
            # profile/ACL boundary; concurrent profile setup can fail with
            # launcher exit 6.  Keep that backend sequential and retain four
            # workers for independently safe backends.
            preflight_workers = (
                1
                if self.sandbox.probe().backend == "appcontainer"
                else 4
            )
            with ThreadPoolExecutor(
                max_workers=preflight_workers,
                thread_name_prefix="stress-small-preflight",
            ) as executor:
                futures = {
                    executor.submit(run_random, offset): offset
                    for offset in range(1, config.small_random_cases + 1)
                }
                for future in as_completed(futures):
                    offset, result, record = future.result()
                    random_results[offset] = (result, record)
                    if progress:
                        progress(
                            "small", "random", len(random_results),
                            config.small_random_cases,
                        )
        small_random_outputs: set[bytes] = set()
        for offset in range(1, config.small_random_cases + 1):
            result, record = random_results[offset]
            small_random_outputs.add(result)
            manifest = record["generator_manifest"]
            if manifest is not None:
                assert isinstance(manifest, Mapping)
                tags = manifest["coverage_tags"]
                assert isinstance(tags, list)
                small_observed_tags.update(str(tag) for tag in tags)
            records.append(record)

        # Adjacent seeds are allowed to collide: tiny legal state spaces and
        # deliberately weighted distributions make that a valid outcome.  A
        # generator is seed-insensitive only when the whole bounded window is
        # constant.  The same-seed probe above remains the determinism gate.
        minimum_distinct_outputs = 12 if local_recipe else 2
        if (
            config.require_seed_variation
            and config.small_random_cases >= 2
            and len(small_random_outputs) < minimum_distinct_outputs
        ):
            raise HelperPreflightError(
                "generator ignores the seed across the small random window",
                artifact="generator",
                profile="small",
                case_kind="random",
                seed=seed0 + config.small_random_cases,
                code="stress_generator_seed_variation_failed",
                expected={"distinct_outputs_minimum": minimum_distinct_outputs},
                actual={
                    "distinct_outputs": len(small_random_outputs),
                    "window_size": config.small_random_cases,
                },
            )

        if local_recipe:
            random_case = next(
                (
                    item
                    for item in blueprint.get("cases", [])
                    if isinstance(item, Mapping)
                    and item.get("profile") == "small"
                    and item.get("case_kind") == "random"
                ),
                None,
            )
            if not isinstance(random_case, Mapping):
                raise HelperPreflightError(
                    "local recipe has no small/random case",
                    artifact="generator",
                    profile="small",
                    case_kind="random",
                    seed=seed0,
                    code="stress_generator_distribution_failed",
                )

            def require_balanced_counts(
                label: str, expected_values: Sequence[str], observed_values: Sequence[str]
            ) -> None:
                expected = list(dict.fromkeys(str(value) for value in expected_values))
                if not expected:
                    return
                counts = {value: 0 for value in expected}
                for value in observed_values:
                    if value in counts:
                        counts[value] += 1
                if min(counts.values()) < 1 or max(counts.values()) - min(counts.values()) > 1:
                    raise HelperPreflightError(
                        f"local recipe {label} distribution is not balanced",
                        artifact="generator",
                        profile="small",
                        case_kind="random",
                        seed=seed0 + config.small_random_cases,
                        code="stress_generator_distribution_failed",
                        expected={"values": expected, "maximum_count_delta": 1},
                        actual={"counts": counts},
                    )

            budget = random_case.get("byte_budget")
            budget = dict(budget) if isinstance(budget, Mapping) else {}
            raw_all_buckets = budget.get("buckets", [])
            all_buckets: list[tuple[int, int]] = []
            if isinstance(raw_all_buckets, Sequence) and not isinstance(
                raw_all_buckets, (str, bytes)
            ):
                for item in raw_all_buckets:
                    if (
                        isinstance(item, Sequence)
                        and not isinstance(item, (str, bytes))
                        and len(item) == 2
                        and type(item[0]) is int
                        and type(item[1]) is int
                    ):
                        all_buckets.append((int(item[0]), int(item[1])))
            raw_buckets = budget.get("active_buckets", raw_all_buckets)
            active_buckets: list[tuple[int, int]] = []
            if isinstance(raw_buckets, Sequence) and not isinstance(raw_buckets, (str, bytes)):
                for item in raw_buckets:
                    if (
                        isinstance(item, Sequence)
                        and not isinstance(item, (str, bytes))
                        and len(item) == 2
                        and type(item[0]) is int
                        and type(item[1]) is int
                    ):
                        active_buckets.append((int(item[0]), int(item[1])))
            if not all_buckets:
                all_buckets = list(active_buckets)
            active_bucket_indices = {
                index
                for index, bucket in enumerate(all_buckets)
                if bucket in active_buckets
            }
            bucket_values: list[str] = []
            tag_values: list[str] = []
            scheduled_bucket_fallbacks = 0
            for offset in range(1, config.small_random_cases + 1):
                result, record = random_results[offset]
                matches = [
                    index
                    for index, (lower, upper) in enumerate(all_buckets)
                    if lower <= len(result) <= upper
                ]
                if all_buckets and len(matches) != 1:
                    raise HelperPreflightError(
                        "local recipe output is outside its declared byte buckets",
                        artifact="generator",
                        profile="small",
                        case_kind="random",
                        seed=seed0 + offset,
                        code="stress_generator_distribution_failed",
                        expected={"buckets": all_buckets},
                        actual={"input_bytes": len(result)},
                    )
                if matches:
                    bucket_values.append(str(matches[0]))
                manifest = record.get("generator_manifest")
                if isinstance(manifest, Mapping):
                    dimensions = manifest.get("dimensions", {})
                    if isinstance(dimensions, Mapping) and matches:
                        scheduled_bucket = dimensions.get("scheduled_byte_bucket")
                        if (
                            type(scheduled_bucket) is int
                            and int(scheduled_bucket) != matches[0]
                        ):
                            scheduled_bucket_fallbacks += 1
                        if matches[0] not in active_bucket_indices:
                            scheduled_bucket_fallbacks += 1
                    tags = manifest.get("coverage_tags", [])
                    if isinstance(tags, Sequence) and not isinstance(tags, (str, bytes)):
                        tag_values.extend(str(tag) for tag in tags)
            # Compiler size ranges conservatively over-approximate digit widths
            # and correlated n/m domains.  Preserve strict balance when every
            # planned bucket was realizable; otherwise the generator reports
            # the actual bucket and byte stratification remains an observable
            # quality metric rather than an input-correctness failure.
            if scheduled_bucket_fallbacks == 0:
                require_balanced_counts(
                    "byte bucket",
                    [str(index) for index in sorted(active_bucket_indices)],
                    bucket_values,
                )
            records.append(
                {
                    "profile": "small",
                    "case_kind": "recipe_distribution",
                    "scheduled_byte_bucket_fallbacks": scheduled_bucket_fallbacks,
                    "actual_byte_bucket_counts": {
                        bucket: bucket_values.count(bucket)
                        for bucket in sorted(set(bucket_values))
                    },
                }
            )
            if blueprint.get("engine") == "local_templates_v1":
                families = random_case.get("families", [])
                family_ids: list[str] = []
                semantic_ids: list[str] = []
                if isinstance(families, Sequence) and not isinstance(
                    families, (str, bytes)
                ):
                    for family_index, family in enumerate(families):
                        if not isinstance(family, Mapping):
                            continue
                        structure = family.get("structure")
                        if isinstance(structure, Mapping):
                            family_id = str(structure.get("template_id") or "")
                            if family_id:
                                family_ids.append(
                                    f"family:{family_id}#{family_index}"
                                )
                        goals = family.get("semantic_goals", [])
                        if isinstance(goals, Sequence) and not isinstance(
                            goals, (str, bytes)
                        ):
                            semantic_ids.extend(
                                f"semantic:{goal}"
                                for goal in goals
                                if str(goal).strip()
                            )
                require_balanced_counts("family", family_ids, tag_values)
                require_balanced_counts(
                    "semantic family",
                    semantic_ids or ["semantic:general"],
                    tag_values,
                )

        missing_small_tags = small_required_tags - small_observed_tags
        if config.require_coverage and missing_small_tags:
            raise HelperPreflightError(
                "small random generator manifests do not cover the required blueprint tags",
                artifact="generator",
                profile="small",
                case_kind="random",
                seed=seed0 + config.small_random_cases,
                code="stress_generator_coverage_failed",
                expected={"required_coverage_tags": sorted(small_required_tags)},
                actual={
                    "observed_coverage_tags": sorted(small_observed_tags),
                    "missing_coverage_tags": sorted(missing_small_tags),
                },
            )

        if config.include_large:
            upper_seed = seed0 + config.small_random_cases + 1
            random_seed = upper_seed + 1
            if progress:
                progress("large", "upper_bound", total_cases - 1, total_cases)
            large_upper_bound, upper_record = run_case(
                "large", "upper_bound", upper_seed, sandbox=self.sandbox
            )
            upper_manifest = upper_record["generator_manifest"]
            if upper_manifest is not None:
                assert isinstance(upper_manifest, Mapping)
                upper_tags = upper_manifest["coverage_tags"]
                assert isinstance(upper_tags, list)
                large_observed_tags.update(str(tag) for tag in upper_tags)
            records.append(upper_record)
            if progress:
                progress("large", "random", total_cases, total_cases)
            large_random, large_record = run_case(
                "large", "random", random_seed, sandbox=self.sandbox
            )
            if large_random == large_upper_bound:
                raise HelperPreflightError(
                    "generator does not distinguish large random from upper-bound data",
                    artifact="generator",
                    profile="large",
                    case_kind="random",
                    seed=random_seed,
                )
            large_manifest = large_record["generator_manifest"]
            if large_manifest is not None:
                assert isinstance(large_manifest, Mapping)
                large_tags = large_manifest["coverage_tags"]
                assert isinstance(large_tags, list)
                large_observed_tags.update(str(tag) for tag in large_tags)
            records.append(large_record)

            missing_large_tags = large_required_tags - large_observed_tags
            if config.require_coverage and missing_large_tags:
                raise HelperPreflightError(
                    "large generator manifests do not cover the required blueprint tags",
                    artifact="generator",
                    profile="large",
                    case_kind="random",
                    seed=random_seed,
                    code="stress_generator_coverage_failed",
                    expected={
                        "large_required_coverage_tags": sorted(large_required_tags)
                    },
                    actual={
                        "observed_coverage_tags": sorted(large_observed_tags),
                        "missing_coverage_tags": sorted(missing_large_tags),
                    },
                )

        validation: dict[str, object] = {
            "preflight_version": HELPER_PREFLIGHT_VERSION,
            "oracle_protocol": staged.oracle_protocol,
            "seed": seed0,
            "contract_hash": config.contract_hash,
            "generator_capabilities": capabilities,
            "build_modes": ["release_static", "libstdcxx_debug"],
            "sanitizer": sanitizer_status,
            "deterministic_generator": True,
            "generator_manifest_version": int(
                capabilities.get("manifest_version", 1)
            ),
            "validator_probes": validator_probe_results,
            "independent_input_validator": "validator" in audit,
            "generator_blueprint": blueprint,
            "generator_coverage": {
                "small_random": {
                    "required_tags": sorted(small_required_tags),
                    "observed_tags": sorted(small_observed_tags),
                    "satisfied": small_required_tags.issubset(
                        small_observed_tags
                    ),
                },
                "large": {
                    "required_tags": sorted(large_required_tags),
                    "observed_tags": sorted(large_observed_tags),
                    "satisfied": (
                        not config.include_large
                        or large_required_tags.issubset(large_observed_tags)
                    ),
                    "skipped": not config.include_large,
                },
            },
            "small_random_cases": config.small_random_cases,
            "include_large": config.include_large,
            "cases": records,
        }
        staged.validation = validation
        staged.preflight_completed = True
        return validation

    def qualify(
        self,
        staged: StagedHelperBundle,
        config: HelperPreflightConfig,
    ) -> dict[str, object]:
        """Run cheap machine gates before spending tokens on AI audit."""

        cwd = Path(staged.staging_dir).resolve()
        executables = dict(staged.audit_executables)
        seed = self._preflight_seed(staged.problem_id, config.contract_hash)
        try:
            capabilities = probe_generator_v2(
                self.sandbox,
                executables["generator"],
                cwd=cwd,
                timeout=config.generator_timeout,
            )
        except GeneratorCapabilityError as exc:
            raise HelperPreflightError(
                str(exc),
                artifact="generator",
                profile="capability",
                case_kind="capability",
                seed=seed,
                code=exc.code,
            ) from exc
        validator_probe_results: list[dict[str, object]] = []
        if config.validator_probes:
            if "validator" not in executables:
                raise HelperPreflightError(
                    "contract validator probes require an independent validator",
                    artifact="validator",
                    profile="contract_probe",
                    case_kind="missing",
                    seed=seed,
                    code="stress_input_validation_failed",
                )
            validator_probe_results = self._preflight_validator_probes(
                executables["validator"],
                cwd=cwd,
                probes=config.validator_probes,
                seed=seed,
                timeout=config.validator_timeout,
                deadline=config.deadline,
                clock=config.clock,
            )
        local_recipe = bool(
            isinstance(config.generator_blueprint, Mapping)
            and config.generator_blueprint.get("engine")
            in {"local_templates_v1", "local_templates_v2"}
        )

        def qualified_random(
            requested_seed: int,
        ) -> tuple[SandboxProcessResult, int, int]:
            effective_seed = requested_seed
            max_input_bytes = SMALL_INPUT_INITIAL_BYTES
            size_attempts = 0
            while True:
                size_attempts += 1
                try:
                    generated = self._preflight_generate(
                        executables["generator"],
                        cwd=cwd,
                        profile="small",
                        case_kind="random",
                        seed=effective_seed,
                        timeout=config.generator_timeout,
                        max_input_bytes=max_input_bytes,
                        deadline=config.deadline,
                        clock=config.clock,
                    )
                except HelperPreflightError as exc:
                    if not (
                        not local_recipe
                        and (
                        "generator_output_limit" in str(exc)
                        or "empty input" in str(exc)
                        )
                    ):
                        raise
                    if max_input_bytes >= MAX_STREAM_BYTES:
                        exc.details["small_input_attempts"] = size_attempts
                        exc.details["max_input_bytes"] = max_input_bytes
                        raise
                    max_input_bytes = SMALL_INPUT_CEILING_BYTES
                    continue
                if "validator" not in executables:
                    return generated, effective_seed, size_attempts
                try:
                    self._preflight_validator_observation(
                        executables["validator"],
                        cwd=cwd,
                        profile="small",
                        case_kind="random",
                        seed=effective_seed,
                        generated_input=generated.stdout,
                        timeout=config.validator_timeout,
                        deadline=config.deadline,
                        clock=config.clock,
                    )
                except HelperPreflightError as exc:
                    if exc.code != "stress_generated_input_invalid" or local_recipe:
                        raise
                    if max_input_bytes >= MAX_STREAM_BYTES:
                        exc.details["small_input_search"] = {
                            "requested_seed": requested_seed,
                            "attempts": size_attempts,
                            "max_input_bytes": max_input_bytes,
                        }
                        raise
                    max_input_bytes = SMALL_INPUT_CEILING_BYTES
                    continue
                return generated, effective_seed, size_attempts

        first, first_effective_seed, _first_attempts = qualified_random(seed)
        repeated, repeated_effective_seed, _repeated_attempts = qualified_random(seed)
        if (
            not first.stdout
            or first_effective_seed != repeated_effective_seed
            or first.stdout != repeated.stdout
        ):
            raise HelperPreflightError(
                "generator failed deterministic smoke gate",
                artifact="generator",
                profile="small",
                case_kind="determinism",
                seed=seed,
            )
        # Exercise the same number of seeded small cases used by the final
        # preflight before spending on AI audit.  This catches seed-dependent
        # legality/UB bugs and supplies an exact failing seed to source repair;
        # the final 16-case pass remains mandatory after audit.
        variation_window = [first]
        for offset in range(1, 16):
            generated, _effective_seed, _attempts = qualified_random(seed + offset)
            variation_window.append(generated)
        minimum_distinct_outputs = 12 if local_recipe else 2
        if len({result.stdout for result in variation_window}) < minimum_distinct_outputs:
            raise HelperPreflightError(
                "generator ignored every seed in the qualification window",
                artifact="generator",
                profile="small",
                case_kind="seed_variation",
                seed=seed,
                code="stress_generator_seed_variation_failed",
                expected={"distinct_outputs_minimum": minimum_distinct_outputs},
                actual={"distinct_outputs": 1, "window_size": len(variation_window)},
            )
        if "validator" in executables:
            if len({result.stdout for result in variation_window}) < 2:
                raise HelperPreflightError(
                    "validator-guided qualification collapsed every random seed",
                    artifact="generator",
                    profile="small",
                    case_kind="seed_variation",
                    seed=seed,
                    code="stress_generator_seed_variation_failed",
                    expected={"distinct_outputs_minimum": 2},
                    actual={"distinct_outputs": 1, "window_size": len(variation_window)},
                )
        large_smoke_checked = False
        if config.include_large:
            # Audit may rely on the fact that the generated large construction
            # has actually completed inside the sandbox.  Run one exact-seed
            # large/random generator + independent-validator smoke here, before
            # any provider audit.  The later joint preflight still repeats all
            # large/boundary cases and executes the reference; this early gate
            # exists to reject quadratic or invalid generator adapters without
            # spending audit/repair tokens under a false premise.
            large_seed = seed + config.small_random_cases
            large_generated = self._preflight_generate(
                executables["generator"],
                cwd=cwd,
                profile="large",
                case_kind="random",
                seed=large_seed,
                timeout=config.generator_timeout,
                deadline=config.deadline,
                clock=config.clock,
            )
            if "validator" in executables:
                self._preflight_validator_observation(
                    executables["validator"],
                    cwd=cwd,
                    profile="large",
                    case_kind="random",
                    seed=large_seed,
                    generated_input=large_generated.stdout,
                    timeout=config.validator_timeout,
                    deadline=config.deadline,
                    clock=config.clock,
                )
            large_smoke_checked = True
        oracle_outputs: dict[str, bytes] = {}
        for role in ("reference_primary", "reference_secondary"):
            result = self._preflight_run(
                executables[role],
                cwd=cwd,
                artifact=role,
                profile="small",
                case_kind="smoke",
                seed=seed,
                timeout=config.reference_timeout_for(role),
                input_data=first.stdout,
                deadline=config.deadline,
                clock=config.clock,
            )
            oracle_outputs[role] = result.stdout
        if not _equal(
            oracle_outputs["reference_primary"],
            oracle_outputs["reference_secondary"],
            config.exact_output,
        ):
            raise HelperPreflightError(
                "references disagree during smoke gate",
                artifact="oracle",
                profile="small",
                case_kind="smoke",
                seed=seed,
                code="stress_oracle_preflight_conflict",
                expected={
                    "oracle": "reference_primary",
                    "stdout": oracle_outputs["reference_primary"].decode(
                        "utf-8", errors="replace"
                    )[:2000],
                    "input_excerpt": first.stdout.decode(
                        "utf-8", errors="replace"
                    )[:2000],
                },
                actual={
                    "oracle": "reference_secondary",
                    "stdout": oracle_outputs["reference_secondary"].decode(
                        "utf-8", errors="replace"
                    )[:2000],
                },
            )
        return {
            "compiled": True,
            "generator_capabilities": capabilities,
            "deterministic": True,
            "seed_sensitive": True,
            "seed_variation_window": len(variation_window),
            "seed_variation_distinct": len(
                {result.stdout for result in variation_window}
            ),
            "validator_checked": "validator" in executables,
            "validator_probes": validator_probe_results,
            "oracle_smoke": True,
            "large_smoke": large_smoke_checked,
        }

    def apply(self, staged: StagedHelperBundle) -> HelperBundle:
        """Atomically apply only a staged bundle that passed preflight."""
        if not staged.preflight_completed or staged.validation is None:
            raise StressError("helper bundle must pass preflight before apply")
        backup = Path(staged.backup_dir)
        backup.mkdir(parents=True, exist_ok=False)
        target_paths = {role: Path(path) for role, path in staged.helper_paths.items()}
        staged_paths = {role: Path(path) for role, path in staged.staged_paths.items()}
        names = {role: path.name for role, path in target_paths.items()}
        replaced: list[str] = []
        try:
            for role, target in target_paths.items():
                if target.is_file():
                    shutil.copy2(target, backup / names[role])
            manifest = {
                "bundle_id": staged.bundle_id,
                "problem_id": staged.problem_id,
                "oracle_protocol": staged.oracle_protocol,
                "baseline_hashes": staged.baseline_hashes,
                "applied_hashes": staged.applied_hashes,
                "files": names,
                "validation": staged.validation,
            }
            _atomic_write(
                backup / "manifest.json",
                (json.dumps(manifest, indent=2) + "\n").encode(),
            )
            for role, target in target_paths.items():
                current = sha256_file(target) if target.is_file() else None
                if current != staged.baseline_hashes[role]:
                    raise BundleConflictError(f"{names[role]} changed before apply")
            for role, target in target_paths.items():
                _atomic_write(target, staged_paths[role].read_bytes())
                replaced.append(role)
            for role, target in target_paths.items():
                if sha256_file(target) != staged.applied_hashes[role]:
                    raise StressError(f"write verification failed: {target.name}")
        except Exception:
            for role in reversed(replaced):
                target = target_paths[role]
                saved = backup / names[role]
                if staged.baseline_hashes[role] is None:
                    target.unlink(missing_ok=True)
                elif saved.is_file():
                    _atomic_write(target, saved.read_bytes())
            raise
        staged.applied = True
        return HelperBundle(
            bundle_id=staged.bundle_id,
            problem_id=staged.problem_id,
            primary_source=staged.primary_source,
            helper_paths=dict(staged.helper_paths),
            baseline_hashes=dict(staged.baseline_hashes),
            applied_hashes=dict(staged.applied_hashes),
            backup_dir=staged.backup_dir,
            staging_dir=staged.staging_dir,
            created_at=staged.created_at,
            oracle_protocol=staged.oracle_protocol,
            release_executables=dict(staged.release_executables),
            validation=dict(staged.validation or {}),
        )

    def discard(self, staged: StagedHelperBundle) -> None:
        """Remove an unapplied staging tree without ever touching helper targets."""
        if staged.applied:
            raise StressError("cannot discard staging for an applied helper bundle")
        staging = Path(staged.staging_dir).resolve()
        expected_staging_parent = (self.root / ".acm" / "stress-staging").resolve()
        if staging.parent != expected_staging_parent:
            raise ValueError("staging path escapes managed stress-staging directory")
        if staging.exists():
            if not staging.is_dir() or staging.is_symlink() or _is_link_or_reparse(staging):
                raise ValueError("staging path is not a regular managed directory")
            shutil.rmtree(staging)

        backup = Path(staged.backup_dir).resolve()
        expected_backup_parent = (self.root / ".acm" / "ai-backups" / "stress").resolve()
        if backup.parent != expected_backup_parent:
            raise ValueError("backup path escapes managed stress backup directory")
        if backup.exists():
            if not backup.is_dir() or backup.is_symlink() or _is_link_or_reparse(backup):
                raise ValueError("backup path is not a regular managed directory")
            # A non-empty backup may be the only recoverable copy after an
            # interrupted apply; leave it in place for startup recovery.
            if not any(backup.iterdir()):
                backup.rmdir()

    def stage_and_apply(
        self,
        primary_source: str | Path,
        sources: HelperSources,
        *,
        preflight_config: HelperPreflightConfig | None = None,
        progress: Callable[[str, str, int, int], None] | None = None,
        compiler: str = "g++",
        compile_timeout: float = 15.0,
    ) -> HelperBundle:
        """Compatibility convenience wrapper that still enforces preflight."""
        staged = self.stage(
            primary_source, sources, compiler=compiler, compile_timeout=compile_timeout
        )
        if preflight_config is None:
            raw = b"\0".join(
                validate_cpp_source(source).encode("utf-8")
                for source in (
                    sources.generator,
                    sources.reference_primary,
                    sources.reference_secondary,
                )
            )
            preflight_config = HelperPreflightConfig(contract_hash=sha256_bytes(raw))
        self.preflight(staged, preflight_config, progress=progress)
        return self.apply(staged)

    def revert(self, bundle: HelperBundle | Mapping[str, object]) -> None:
        data = bundle.to_dict() if isinstance(bundle, HelperBundle) else dict(bundle)
        backup = Path(str(data["backup_dir"])).resolve()
        if not backup.is_relative_to(self.root / ".acm" / "ai-backups" / "stress"):
            raise ValueError("backup path escapes managed stress backup directory")
        helper_paths = {str(k): Path(str(v)).resolve() for k, v in dict(data["helper_paths"]).items()}
        baseline = dict(data["baseline_hashes"])
        applied = dict(data["applied_hashes"])
        for role, target in helper_paths.items():
            if not target.is_relative_to(self.root) or target.is_symlink():
                raise ValueError("helper target is outside workspace or is a symlink")
            current = sha256_file(target) if target.is_file() else None
            if current != applied.get(role):
                raise BundleConflictError(f"{target.name} changed after AI apply")
        for role, target in helper_paths.items():
            old_hash = baseline.get(role)
            backup_file = backup / target.name
            if old_hash is None:
                target.unlink(missing_ok=True)
            else:
                if not backup_file.is_file() or sha256_file(backup_file) != old_hash:
                    raise StressError(f"invalid backup for {target.name}")
                _atomic_write(target, backup_file.read_bytes())


class StopToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def request_stop(self) -> None:
        self._event.set()

    def is_stopped(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()


@dataclass(frozen=True, slots=True, init=False)
class StressExecutables:
    solution: Path
    generator: Path
    reference_primary: Path
    reference_secondary: Path
    validator: Path | None = None

    def __init__(
        self,
        solution: Path,
        generator: Path,
        reference_primary: Path | None = None,
        reference_secondary: Path | None = None,
        validator: Path | None = None,
        *,
        brute: Path | None = None,
        reference: Path | None = None,
    ) -> None:
        primary = reference_primary if reference_primary is not None else brute
        secondary = reference_secondary if reference_secondary is not None else reference
        if primary is None or secondary is None:
            raise TypeError("both reference executables are required")
        if reference_primary is not None and brute is not None:
            raise TypeError("reference_primary and deprecated brute are mutually exclusive")
        if reference_secondary is not None and reference is not None:
            raise TypeError("reference_secondary and deprecated reference are mutually exclusive")
        object.__setattr__(self, "solution", Path(solution))
        object.__setattr__(self, "generator", Path(generator))
        object.__setattr__(self, "reference_primary", Path(primary))
        object.__setattr__(self, "reference_secondary", Path(secondary))
        object.__setattr__(self, "validator", Path(validator) if validator is not None else None)

    @property
    def brute(self) -> Path:
        return self.reference_primary

    @property
    def reference(self) -> Path:
        return self.reference_secondary


@dataclass(frozen=True, slots=True)
class SampleCase:
    name: str
    input_data: bytes
    expected_output: bytes


@dataclass(slots=True)
class StressRunConfig:
    first_seed: int
    profile_version: int = 2
    schedule_offset: int = 0
    include_large: bool = True
    large_per_cycle: int = 1
    warmup_small_cases: int = 200
    small_per_cycle: int = 4
    solution_timeout: float = 2.0
    generator_timeout: float = 2.0
    reference_timeout: float = 2.0
    brute_timeout: float = 5.0
    reference_primary_timeout: float | None = None
    reference_secondary_timeout: float | None = None
    validator_timeout: float = 2.0
    oracle_protocol: str = DUAL_REFERENCE_PROTOCOL
    exact_output: bool = False
    max_cases: int | None = None

    def __post_init__(self) -> None:
        if self.profile_version != 2:
            raise ValueError("only stress profile version 2 is supported")
        if self.oracle_protocol not in {DUAL_REFERENCE_PROTOCOL, LEGACY_TRIO_PROTOCOL}:
            raise ValueError("unsupported stress oracle protocol")
        if min(
            self.reference_primary_timeout
            if self.reference_primary_timeout is not None
            else self.brute_timeout,
            self.reference_secondary_timeout
            if self.reference_secondary_timeout is not None
            else self.reference_timeout,
        ) <= 0:
            raise ValueError("reference timeouts must be positive")

    def reference_timeout_for(self, role: str) -> float:
        if role == "reference_primary":
            return float(
                self.reference_primary_timeout
                if self.reference_primary_timeout is not None
                else self.brute_timeout
            )
        if role == "reference_secondary":
            return float(
                self.reference_secondary_timeout
                if self.reference_secondary_timeout is not None
                else self.reference_timeout
            )
        raise KeyError(role)


@dataclass(slots=True)
class StressRunResult:
    status: str
    next_seed: int
    small_cases: int = 0
    large_cases: int = 0
    total_cases: int = 0
    failure_dir: str | None = None
    detail: str = ""
    elapsed_ms: int = 0
    current_profile: str = ""
    case_kind: str = ""
    #: Search-driver counters and the diversity report, empty when the run stayed
    #: recipe-only (no contract, or a contract the profiler cannot parse).
    search: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _equal(left: bytes, right: bytes, exact: bool) -> bool:
    return left == right if exact else left.split() == right.split()


def classify_dual_reference(
    solution: bytes,
    reference_primary: bytes,
    reference_secondary: bytes,
    *,
    exact: bool = False,
) -> str:
    """Classify output only after the two independent references agree."""
    references_agree = _equal(reference_primary, reference_secondary, exact)
    if references_agree and _equal(solution, reference_primary, exact):
        return "agree"
    if references_agree:
        return "mismatch"
    return "oracle_conflict"


def classify_three_way(
    solution: bytes, brute: bytes, reference: bytes, *, exact: bool = False
) -> str:
    """Deprecated alias for reading legacy trio runs."""
    return classify_dual_reference(solution, brute, reference, exact=exact)


def _result_failure_kind(role: str, result: SandboxProcessResult) -> str | None:
    if result.timed_out:
        return f"{role}_timeout"
    if result.output_limited:
        return f"{role}_output_limit"
    if result.returncode != 0:
        return f"{role}_runtime_error"
    return None


def save_failure_assets(
    root: str | Path,
    problem_id: str,
    seed: int,
    *,
    reason: str,
    profile: str,
    input_data: bytes,
    results: Mapping[str, SandboxProcessResult],
    source_hashes: Mapping[str, str],
    reference_source: Mapping[str, str] | None = None,
    reference_sources: Mapping[str, Mapping[str, str]] | None = None,
    conflict_export_dir: str | Path | None = None,
    exact_output: bool = False,
    input_limit: int = MAX_STREAM_BYTES,
    output_limit: int = MAX_STREAM_BYTES,
    brute_status: str | None = None,
    case_kind: str = "random",
    oracle_protocol: str = DUAL_REFERENCE_PROTOCOL,
) -> Path:
    root_path = Path(root).resolve()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_problem = re.sub(r"[^A-Za-z0-9_.-]", "_", problem_id)
    directory = root_path / ".acm" / "failures" / safe_problem / f"{timestamp}-seed-{seed}"
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "input.txt").write_bytes(input_data[:input_limit])
    metadata_results: dict[str, object] = {}
    for role, result in results.items():
        (directory / f"{role}.stdout.txt").write_bytes(result.stdout[:output_limit])
        (directory / f"{role}.stderr.txt").write_bytes(result.stderr[:output_limit])
        metadata_results[role] = {
            "command": result.command,
            "returncode": result.returncode,
            "elapsed_ms": result.elapsed_ms,
            "timed_out": result.timed_out,
            "output_limited": result.output_limited,
        }
    conflict_exports: dict[str, str] = {}
    solution_result = results.get("solution")
    primary_result = results.get("reference_primary")
    secondary_result = results.get("reference_secondary")
    if oracle_protocol == LEGACY_TRIO_PROTOCOL:
        primary_result = results.get("brute")
        secondary_result = results.get("reference")
        should_export = (
            solution_result is not None
            and secondary_result is not None
            and not _equal(solution_result.stdout, secondary_result.stdout, exact_output)
        )
    else:
        should_export = (
            solution_result is not None
            and primary_result is not None
            and secondary_result is not None
            and (
                not _equal(primary_result.stdout, secondary_result.stdout, exact_output)
                or not _equal(solution_result.stdout, primary_result.stdout, exact_output)
            )
        )
    if conflict_export_dir is not None and should_export:
        export_dir = Path(conflict_export_dir).resolve()
        try:
            export_dir.relative_to(root_path)
        except ValueError as exc:
            raise StressError("Conflict export directory must stay inside the workspace") from exc
        if not export_dir.is_dir():
            raise StressError("Conflict export directory is not available")
        exports: dict[str, tuple[str, bytes]] = {
            "input": (f"{safe_problem}_input.in", input_data[:input_limit]),
        }
        export_roles = (
            (("solution", "current"), ("brute", "brute"), ("reference", "reference"))
            if oracle_protocol == LEGACY_TRIO_PROTOCOL
            else (
                ("solution", "current"),
                ("reference_primary", "ref1"),
                ("reference_secondary", "ref2"),
            )
        )
        for role, label in export_roles:
            result = results.get(role)
            if result is not None:
                exports[label] = (
                    f"{safe_problem}_{label}.out",
                    result.stdout[:output_limit],
                )
        for label, (name, content) in exports.items():
            _atomic_write(export_dir / name, content)
            conflict_exports[label] = name
    metadata = {
        "problem_id": problem_id,
        "seed": seed,
        "profile": profile,
        "case_kind": case_kind,
        "reason": reason,
        "source_hashes": dict(source_hashes),
        "reference_source": dict(reference_source or {}),
        "reference_sources": {
            str(role): dict(source)
            for role, source in dict(reference_sources or {}).items()
        },
        "oracle_protocol": oracle_protocol,
        "results": metadata_results,
        "brute_status": brute_status,
        "conflict_exports": conflict_exports,
        "reproduce": {
            "seed": seed,
            "profile": profile,
            "input": "input.txt",
            "command": ["acm.ps1", "verify", problem_id, "--ai-stress", "--seed", str(seed)],
        },
    }
    _atomic_write(directory / "metadata.json", (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode())
    return directory.resolve()


class LayeredStressRunner:
    """Run samples and the profile-v2 small/large stress schedule."""

    def __init__(
        self,
        root: str | Path,
        problem_id: str,
        executables: StressExecutables,
        sandbox: SandboxBackend,
        *,
        stop_token: StopToken | None = None,
        progress: Callable[[StressRunResult], None] | None = None,
        source_hashes: Mapping[str, str] | None = None,
        reference_source: Mapping[str, str] | None = None,
        reference_sources: Mapping[str, Mapping[str, str]] | None = None,
        conflict_export_dir: str | Path | None = None,
        contract: Mapping[str, object] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.problem_id = problem_id
        self.executables = executables
        self.sandbox = sandbox
        # Optional: without it the search driver stays recipe-only, which is
        # exactly the pre-driver behaviour.
        self.contract = dict(contract) if contract is not None else None
        self.stop_token = stop_token or StopToken()
        self.progress = progress
        self.source_hashes = dict(source_hashes or {})
        self.reference_source = dict(reference_source or {})
        self.reference_sources = {
            str(role): dict(source)
            for role, source in dict(reference_sources or {}).items()
        }
        self.conflict_export_dir: Path | None = None
        if conflict_export_dir is not None:
            export_dir = Path(conflict_export_dir).resolve()
            try:
                export_dir.relative_to(self.root)
            except ValueError as exc:
                raise ValueError("conflict_export_dir must stay inside the workspace") from exc
            if not export_dir.is_dir():
                raise ValueError("conflict_export_dir must be an existing directory")
            self.conflict_export_dir = export_dir
        self._active_run_dir: Path | None = None
        self._supports_lower_bound = False
        self._driver: SearchDriver | None = None

    def request_stop(self) -> None:
        self.stop_token.request_stop()
        self.sandbox.cancel()

    def run(self, config: StressRunConfig, *, samples: Sequence[SampleCase] = ()) -> StressRunResult:
        capability = self.sandbox.probe()
        if not capability.available:
            raise SandboxUnavailableError(capability.reason)
        if config.first_seed < 0:
            raise ValueError("first_seed must be non-negative")
        if config.profile_version != 2:
            raise ValueError("only stress profile version 2 is supported")
        started = time.monotonic()
        state = StressRunResult("running", next_seed=config.first_seed)
        run_dir = self.root / ".acm" / "stress-runs" / uuid.uuid4().hex
        run_dir.mkdir(parents=True, exist_ok=False)
        self._active_run_dir = run_dir
        # AppContainer receives access only to this per-run directory.  Copy
        # every executable into that boundary instead of asking the sandbox to
        # read the dated source tree or shared build/staging directories.
        local_executables: dict[str, Path] = {}
        oracle_roles = (
            (
                ("brute", self.executables.reference_primary),
                ("reference", self.executables.reference_secondary),
            )
            if config.oracle_protocol == LEGACY_TRIO_PROTOCOL
            else (
                ("reference_primary", self.executables.reference_primary),
                ("reference_secondary", self.executables.reference_secondary),
            )
        )
        for role, source in (
            ("solution", self.executables.solution),
            ("generator", self.executables.generator),
            *oracle_roles,
        ):
            suffix = source.suffix if source.suffix else (".exe" if os.name == "nt" else "")
            target = run_dir / f"{role}{suffix}"
            shutil.copy2(source, target)
            local_executables[role] = target
        if self.executables.validator is not None:
            source = self.executables.validator
            suffix = source.suffix if source.suffix else (".exe" if os.name == "nt" else "")
            target = run_dir / f"validator{suffix}"
            shutil.copy2(source, target)
            local_executables["validator"] = target
        oracle_names = (
            ("brute", "reference")
            if config.oracle_protocol == LEGACY_TRIO_PROTOCOL
            else ("reference_primary", "reference_secondary")
        )
        self.executables = StressExecutables(
            local_executables["solution"],
            local_executables["generator"],
            local_executables[oracle_names[0]],
            local_executables[oracle_names[1]],
            local_executables.get("validator"),
        )

        for sample in samples:
            if self.stop_token.is_stopped():
                return self._finish(state, "stopped", started)
            results: dict[str, SandboxProcessResult] = {}
            validation_failure = self._validate_input(
                sample.input_data,
                config,
                run_dir,
                seed=config.first_seed,
                profile="sample",
                case_kind=sample.name,
            )
            if validation_failure is not None:
                reason, validator_result = validation_failure
                directory = save_failure_assets(
                    self.root,
                    self.problem_id,
                    config.first_seed,
                    reason=reason,
                    profile=f"sample:{sample.name}",
                    case_kind="official_sample",
                    input_data=sample.input_data,
                    results={"validator": validator_result},
                    source_hashes=self.source_hashes,
                    reference_source=self.reference_source,
                    reference_sources=self.reference_sources,
                    exact_output=config.exact_output,
                    oracle_protocol=config.oracle_protocol,
                )
                state.failure_dir = str(directory)
                return self._finish(state, reason, started)
            sample_roles = (
                (
                    ("solution", self.executables.solution),
                    ("brute", self.executables.reference_primary),
                    ("reference", self.executables.reference_secondary),
                )
                if config.oracle_protocol == LEGACY_TRIO_PROTOCOL
                else (
                    ("solution", self.executables.solution),
                    ("reference_primary", self.executables.reference_primary),
                    ("reference_secondary", self.executables.reference_secondary),
                )
            )
            for role, path in sample_roles:
                results[role] = self._execute(
                    path, sample.input_data, self._timeout(role, config), run_dir
                )
                if self.stop_token.is_stopped():
                    return self._finish(state, "stopped", started)
            if config.oracle_protocol == DUAL_REFERENCE_PROTOCOL:
                failure = next(
                    (
                        _result_failure_kind(role, results[role])
                        for role in ("reference_primary", "reference_secondary")
                        if _result_failure_kind(role, results[role])
                    ),
                    None,
                )
            else:
                failure = next((_result_failure_kind(role, result) for role, result in results.items() if _result_failure_kind(role, result)), None)
            dual_conflict = (
                config.oracle_protocol == DUAL_REFERENCE_PROTOCOL
                and not _equal(
                    results["reference_primary"].stdout,
                    results["reference_secondary"].stdout,
                    config.exact_output,
                )
            )
            if (
                config.oracle_protocol == DUAL_REFERENCE_PROTOCOL
                and failure is None
                and not dual_conflict
            ):
                failure = _result_failure_kind("solution", results["solution"])
            if failure or dual_conflict or any(not _equal(result.stdout, sample.expected_output, config.exact_output) for result in results.values()):
                reason = failure or ("oracle_conflict" if dual_conflict else "sample_mismatch")
                directory = save_failure_assets(
                    self.root, self.problem_id, config.first_seed, reason=reason,
                    profile=f"sample:{sample.name}", input_data=sample.input_data,
                    results=results, source_hashes=self.source_hashes,
                    reference_source=self.reference_source,
                    reference_sources=self.reference_sources,
                    conflict_export_dir=self.conflict_export_dir,
                    exact_output=config.exact_output,
                    oracle_protocol=config.oracle_protocol,
                )
                state.failure_dir = str(directory)
                return self._finish(state, reason, started)

        if self.stop_token.is_stopped():
            return self._finish(state, "stopped", started)
        try:
            self._require_generator_v2(config, run_dir)
        except GeneratorCapabilityError:
            if self.stop_token.is_stopped():
                return self._finish(state, "stopped", started)
            raise
        self._driver = self._build_driver(config, run_dir)

        driver = self._driver
        while config.max_cases is None or state.total_cases < config.max_cases:
            if self.stop_token.is_stopped():
                return self._finish(state, "stopped", started)
            profile, case_kind = self._case_for_index(state.total_cases, config)
            state.current_profile = profile
            state.case_kind = case_kind
            request = (
                driver.next_case(profile, case_kind)
                if driver is not None
                else CaseRequest("recipe", state.next_seed, origin="recipe")
            )
            outcome = self._run_generated_case(
                request.seed, profile, case_kind, config, run_dir, request=request
            )
            if outcome is not None and outcome[0] == "stopped":
                return self._finish(state, "stopped", started)
            # The driver owns seed allocation once it exists, so that a
            # synthesized case still advances the seed the dashboard and resume
            # bookkeeping are keyed by.
            state.next_seed = driver.next_seed if driver is not None else state.next_seed + 1
            state.total_cases += 1
            if profile == "small":
                state.small_cases += 1
            else:
                state.large_cases += 1
            if outcome is not None:
                status, failure_dir, detail = outcome
                state.failure_dir = str(failure_dir) if failure_dir is not None else None
                state.detail = detail
                return self._finish(state, status, started)
            state.elapsed_ms = round((time.monotonic() - started) * 1000)
            if self.progress:
                self.progress(state)
        return self._finish(state, "limit_reached", started)

    def _build_driver(
        self, config: StressRunConfig, run_dir: Path
    ) -> SearchDriver | None:
        """Construct the case-selection driver, or ``None`` to stay recipe-only.

        Recipe-only is the pre-driver behaviour, so every failure to build one is
        a silent degradation rather than an error: no contract, an unparseable
        contract, or a resume whose archive cannot be restored all just mean the
        loop keeps asking the generator for seeds.
        """

        if self.contract is None:
            return None
        driver = SearchDriver(
            self.contract,
            first_seed=config.first_seed,
            max_bytes=SMALL_EXHAUSTIVE_MAX_BYTES,
            rng_seed=config.first_seed,
        )
        if not driver.active:
            return None
        archive_path = self._archive_path
        if archive_path is not None and archive_path.is_file():
            try:
                driver.load(str(archive_path))
            except (OSError, ValueError, json.JSONDecodeError):
                # A corrupt or stale archive must not abort a run that would
                # otherwise work.  Start from an empty one.
                pass
        return driver

    @property
    def _archive_path(self) -> Path | None:
        """Where this problem's archive persists across runs.

        Outside the per-run directory on purpose: the run directory is deleted by
        :meth:`cleanup`, and the whole point of the archive is to survive.
        """

        safe = re.sub(r"[^A-Za-z0-9._-]", "_", self.problem_id).strip("._-")
        if not safe:
            return None
        return self.root / ".acm" / "stress-archives" / f"{safe}.json"

    def _persist_archive(self) -> None:
        driver = self._driver
        archive_path = self._archive_path
        if driver is None or archive_path is None or not driver.active:
            return
        try:
            driver.save(str(archive_path))
        except OSError:
            # Losing the archive costs the next run its warm start, nothing more.
            pass

    def _case_for_index(self, index: int, config: StressRunConfig) -> tuple[str, str]:
        if config.profile_version != 2:
            raise ValueError("only stress profile version 2 is supported")
        absolute = config.schedule_offset + index
        if self._supports_lower_bound and absolute == 0:
            return "small", "lower_bound"
        upper_index = 1 if self._supports_lower_bound else 0
        if config.include_large and absolute == upper_index:
            return "large", "upper_bound"
        prelude = int(self._supports_lower_bound) + int(config.include_large)
        position = max(0, absolute - prelude)
        if not config.include_large or config.large_per_cycle <= 0:
            return "small", "random"
        cycle = max(1, config.small_per_cycle + config.large_per_cycle)
        return (
            ("small", "random")
            if position % cycle < config.small_per_cycle
            else ("large", "random")
        )

    def _require_generator_v2(
        self, config: StressRunConfig, cwd: Path
    ) -> None:
        capabilities = probe_generator_v2(
            self.sandbox,
            self.executables.generator,
            cwd=cwd,
            timeout=config.generator_timeout,
        )
        self._supports_lower_bound = (
            _GENERATOR_OPTIONAL_CASE in capabilities["supported_cases"]
        )

    @staticmethod
    def _timeout(role: str, config: StressRunConfig) -> float:
        fixed = {
            "solution": config.solution_timeout,
            "generator": config.generator_timeout,
            "reference": config.reference_timeout,
            "brute": config.brute_timeout,
            "validator": config.validator_timeout,
        }
        if role in {"reference_primary", "reference_secondary"}:
            return config.reference_timeout_for(role)
        return fixed[role]

    def _record_case(
        self,
        request: CaseRequest | None,
        input_data: bytes,
        results: Mapping[str, SandboxProcessResult],
        config: StressRunConfig,
    ) -> None:
        """Feed a completed, non-failing case back into the search driver.

        The behavioural signal must come from a reference, never from the
        solution under test: keying archive cells on the solution's output would
        make the search reward the very disagreement it is hunting for, and a
        wrong solution would steer its own test data.
        """

        driver = self._driver
        if driver is None or request is None:
            return
        role = (
            "reference_primary"
            if config.oracle_protocol == DUAL_REFERENCE_PROTOCOL
            else ("brute" if "brute" in results else "reference")
        )
        reference = results.get(role)
        driver.record(
            request,
            input_data,
            reference_output=reference.stdout if reference is not None else None,
        )

    def _validate_input(
        self,
        input_data: bytes,
        config: StressRunConfig,
        cwd: Path,
        *,
        seed: int,
        profile: str,
        case_kind: str,
    ) -> tuple[str, SandboxProcessResult] | None:
        validator = self.executables.validator
        if validator is None:
            return None
        result = self._execute(
            validator,
            input_data,
            config.validator_timeout,
            cwd,
            env={
                "ACM_STRESS_SEED": str(seed),
                "ACM_STRESS_PROFILE": profile,
                "ACM_STRESS_CASE_KIND": case_kind,
                "ACM_STRESS_PROFILE_VERSION": "2",
            },
            stdout_bytes=256 * 1024,
        )
        if _result_failure_kind("validator", result) is not None:
            return "input_validation_failed", result
        try:
            _parse_validator_observation(result.stdout)
        except _ValidatorRejectedInput:
            return "generated_input_rejected", result
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return "input_validation_failed", result
        return None

    def _sanitize_small_input(
        self,
        input_data: bytes,
        rejection_result: SandboxProcessResult,
        config: StressRunConfig,
        cwd: Path,
        *,
        seed: int,
        case_kind: str,
    ) -> bytes | None:
        """Use the independent validator as the sole acceptance oracle for edits."""

        current = input_data
        current_result = rejection_result
        seen = {sha256_bytes(input_data)}
        sanitize_deadline = time.monotonic() + min(
            3.0, max(0.5, float(config.validator_timeout) * 1.5)
        )
        checked_candidates = 0
        for _edit_round in range(1, 9):
            diagnostic = current_result.stderr + b"\n" + current_result.stdout
            next_rejection: tuple[bytes, SandboxProcessResult] | None = None
            for candidate in _small_record_deletion_candidates(current, diagnostic):
                if checked_candidates >= 8 or time.monotonic() >= sanitize_deadline:
                    return None
                digest = sha256_bytes(candidate)
                if digest in seen:
                    continue
                seen.add(digest)
                checked_candidates += 1
                failure = self._validate_input(
                    candidate,
                    config,
                    cwd,
                    seed=seed,
                    profile="small",
                    case_kind=case_kind,
                )
                if failure is None:
                    return candidate
                reason, result = failure
                if reason == "generated_input_rejected" and next_rejection is None:
                    next_rejection = (candidate, result)
            if next_rejection is None:
                return None
            current, current_result = next_rejection
        return None

    def _execute(
        self,
        executable: Path,
        input_data: bytes | None,
        timeout: float,
        cwd: Path,
        *,
        env: Mapping[str, str] | None = None,
        args: Sequence[str] = (),
        stdin_bytes: int = MAX_STREAM_BYTES,
        stdout_bytes: int = MAX_STREAM_BYTES,
        stderr_bytes: int = MAX_STREAM_BYTES,
    ) -> SandboxProcessResult:
        return self.sandbox.run(
            [str(executable), *args], cwd=cwd, input_data=input_data, env=env,
            limits=SandboxLimits(
                timeout_seconds=timeout,
                stdin_bytes=stdin_bytes,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
            ),
        )

    def _run_generated_case(
        self,
        seed: int,
        profile: str,
        case_kind: str,
        config: StressRunConfig,
        cwd: Path,
        *,
        request: CaseRequest | None = None,
    ) -> tuple[str, Path | None, str] | None:
        requested_seed = seed
        environment = {"ACM_STRESS_SEED": str(seed), "ACM_STRESS_PROFILE": profile}
        generator_stdout = MAX_STREAM_BYTES
        environment.update(
            {
                "ACM_STRESS_CASE_KIND": case_kind,
                "ACM_STRESS_PROFILE_VERSION": "2",
            }
        )
        generator_args = (str(seed), profile, case_kind)
        if profile == "large":
            generator_stdout = MAX_LARGE_INPUT_BYTES
        # Runtime executes exactly the requested seed.  Seed substitution or
        # validator-guided record deletion changes the tested input and can
        # hide an invalid generator after a bundle has been certified.
        seed_attempts = 1
        validator_result: SandboxProcessResult | None = None
        synthesized = request is not None and request.data is not None
        for seed_attempt in range(seed_attempts):
            seed = _derived_candidate_seed(requested_seed, seed_attempt)
            environment["ACM_STRESS_SEED"] = str(seed)
            generator_args = (str(seed), profile, case_kind)
            if synthesized:
                # The input came from the search driver, so there is no generator
                # process for this case.  A synthetic result keeps the failure
                # assets and reporting paths below unchanged.
                assert request is not None and request.data is not None
                generated = SandboxProcessResult(
                    command=[f"<search:{request.origin or request.source}>"],
                    returncode=0,
                    stdout=request.data,
                )
            else:
                generated = self._execute(
                    self.executables.generator, None, config.generator_timeout, cwd,
                    env=environment, args=generator_args, stdout_bytes=generator_stdout,
                )
            if self.stop_token.is_stopped():
                return "stopped", None, "stopped by user"
            gen_failure = _result_failure_kind("generator", generated)
            if gen_failure is None and not generated.stdout:
                gen_failure = "generator_empty_output"
            if gen_failure:
                if (
                    case_kind == "random"
                    and gen_failure == "generator_runtime_error"
                    and seed_attempt + 1 < seed_attempts
                ):
                    continue
                directory = save_failure_assets(
                    self.root, self.problem_id, seed, reason=gen_failure, profile=profile,
                    input_data=generated.stdout, results={"generator": generated},
                    source_hashes=self.source_hashes, reference_source=self.reference_source,
                    reference_sources=self.reference_sources,
                    conflict_export_dir=self.conflict_export_dir,
                    exact_output=config.exact_output,
                    input_limit=generator_stdout,
                    output_limit=(
                        MAX_LARGE_OUTPUT_BYTES if profile == "large" else MAX_STREAM_BYTES
                    ),
                    case_kind=case_kind,
                    oracle_protocol=config.oracle_protocol,
                )
                return gen_failure, directory, "generator did not produce a valid case"
            input_data = generated.stdout
            validation_failure = self._validate_input(
                input_data,
                config,
                cwd,
                seed=seed,
                profile=profile,
                case_kind=case_kind,
            )
            if validation_failure is None:
                break
            reason, validator_result = validation_failure
            if synthesized and reason == "generated_input_rejected":
                # The certified generator is not implicated: the driver's
                # contract-clamped mutation cannot see cross-field constraints, so
                # it can propose something the real validator refuses.  Discard
                # the case, tell the driver, and keep the run going.  Saving
                # failure assets here would report a generator bug that is not
                # there and stop a healthy run.
                if self._driver is not None:
                    assert request is not None
                    self._driver.reject(request)
                return None
            if reason == "generated_input_rejected" and seed_attempt + 1 < seed_attempts:
                continue
            directory = save_failure_assets(
                self.root,
                self.problem_id,
                seed,
                reason=reason,
                profile=profile,
                case_kind=case_kind,
                input_data=input_data,
                results={"generator": generated, "validator": validator_result},
                source_hashes=self.source_hashes,
                reference_source=self.reference_source,
                reference_sources=self.reference_sources,
                exact_output=config.exact_output,
                input_limit=generator_stdout,
                output_limit=(
                    MAX_LARGE_OUTPUT_BYTES if profile == "large" else MAX_STREAM_BYTES
                ),
                oracle_protocol=config.oracle_protocol,
            )
            return reason, directory, "validator rejected generated input"
        if config.oracle_protocol == LEGACY_TRIO_PROTOCOL:
            roles = [
                ("solution", self.executables.solution),
                ("reference", self.executables.reference_secondary),
            ]
            if profile == "small":
                roles.insert(1, ("brute", self.executables.reference_primary))
        else:
            roles = [
                ("solution", self.executables.solution),
                ("reference_primary", self.executables.reference_primary),
                ("reference_secondary", self.executables.reference_secondary),
            ]
        large = profile == "large"
        program_input_limit = MAX_LARGE_INPUT_BYTES if large else MAX_STREAM_BYTES
        program_output_limit = MAX_LARGE_OUTPUT_BYTES if large else MAX_STREAM_BYTES
        results: dict[str, SandboxProcessResult] = {}
        for role, path in roles:
            results[role] = self._execute(
                path,
                input_data,
                self._timeout(role, config),
                cwd,
                stdin_bytes=program_input_limit,
                stdout_bytes=program_output_limit,
                stderr_bytes=program_output_limit,
            )
            if self.stop_token.is_stopped():
                return "stopped", None, "stopped by user"
        failure_roles = (
            ("reference_primary", "reference_secondary")
            if config.oracle_protocol == DUAL_REFERENCE_PROTOCOL
            else tuple(results)
        )
        for role in failure_roles:
            result = results[role]
            failure = _result_failure_kind(role, result)
            if failure:
                all_results = {"generator": generated, **results}
                directory = save_failure_assets(
                    self.root, self.problem_id, seed, reason=failure, profile=profile,
                    input_data=input_data, results=all_results,
                    source_hashes=self.source_hashes, reference_source=self.reference_source,
                    reference_sources=self.reference_sources,
                    conflict_export_dir=self.conflict_export_dir,
                    exact_output=config.exact_output,
                    input_limit=program_input_limit,
                    output_limit=program_output_limit,
                    case_kind=case_kind,
                    oracle_protocol=config.oracle_protocol,
                )
                return failure, directory, f"{role} execution failed"
        self._record_case(request, input_data, results, config)
        if config.oracle_protocol == DUAL_REFERENCE_PROTOCOL:
            classification = classify_dual_reference(
                results["solution"].stdout,
                results["reference_primary"].stdout,
                results["reference_secondary"].stdout,
                exact=config.exact_output,
            )
            if classification != "oracle_conflict":
                solution_failure = _result_failure_kind("solution", results["solution"])
                if solution_failure:
                    directory = save_failure_assets(
                        self.root, self.problem_id, seed, reason=solution_failure,
                        profile=profile, input_data=input_data,
                        results={"generator": generated, **results},
                        source_hashes=self.source_hashes,
                        reference_source=self.reference_source,
                        reference_sources=self.reference_sources,
                        conflict_export_dir=self.conflict_export_dir,
                        exact_output=config.exact_output,
                        input_limit=program_input_limit,
                        output_limit=program_output_limit,
                        case_kind=case_kind,
                        oracle_protocol=config.oracle_protocol,
                    )
                    return solution_failure, directory, "solution execution failed"
            if classification == "agree":
                return None
            directory = save_failure_assets(
                self.root, self.problem_id, seed, reason=classification, profile=profile,
                input_data=input_data, results={"generator": generated, **results},
                source_hashes=self.source_hashes, reference_source=self.reference_source,
                reference_sources=self.reference_sources,
                conflict_export_dir=self.conflict_export_dir,
                exact_output=config.exact_output,
                case_kind=case_kind,
                oracle_protocol=config.oracle_protocol,
            )
            return classification, directory, "dual-reference outputs disagree"
        if profile == "small":
            classification = classify_three_way(
                results["solution"].stdout, results["brute"].stdout,
                results["reference"].stdout, exact=config.exact_output,
            )
            if classification == "agree":
                return None
            directory = save_failure_assets(
                self.root, self.problem_id, seed, reason=classification, profile=profile,
                input_data=input_data, results={"generator": generated, **results},
                source_hashes=self.source_hashes, reference_source=self.reference_source,
                reference_sources=self.reference_sources,
                conflict_export_dir=self.conflict_export_dir,
                exact_output=config.exact_output,
                case_kind=case_kind,
                oracle_protocol=config.oracle_protocol,
            )
            return classification, directory, "legacy three-way outputs disagree"
        if _equal(results["solution"].stdout, results["reference"].stdout, config.exact_output):
            return None

        if large:
            directory = save_failure_assets(
                self.root,
                self.problem_id,
                seed,
                reason="mismatch",
                profile=profile,
                input_data=input_data,
                results={"generator": generated, **results},
                source_hashes=self.source_hashes,
                reference_source=self.reference_source,
                reference_sources=self.reference_sources,
                conflict_export_dir=self.conflict_export_dir,
                exact_output=config.exact_output,
                input_limit=MAX_LARGE_INPUT_BYTES,
                output_limit=MAX_LARGE_OUTPUT_BYTES,
                brute_status="skipped_large_profile",
                case_kind=case_kind,
                oracle_protocol=config.oracle_protocol,
            )
            return "mismatch", directory, "large solution/reference disagreement"

        raise StressError(f"unsupported generated profile: {profile}")

    def _finish(self, state: StressRunResult, status: str, started: float) -> StressRunResult:
        state.status = status
        state.elapsed_ms = round((time.monotonic() - started) * 1000)
        driver = self._driver
        if driver is not None:
            state.search = driver.report()
        self._persist_archive()
        if self.progress:
            self.progress(state)
        self.cleanup()
        return state

    def cleanup(self) -> None:
        run_dir = self._active_run_dir
        self._active_run_dir = None
        expected_parent = (self.root / ".acm" / "stress-runs").resolve()
        if run_dir is not None:
            resolved = run_dir.resolve()
            if resolved.parent == expected_parent and resolved.is_dir() and not resolved.is_symlink():
                try:
                    shutil.rmtree(resolved)
                except OSError:
                    pass


__all__ = [
    "BundleConflictError", "HelperBundle", "HelperBundleManager", "HelperPreflightConfig",
    "HelperPreflightError", "HelperSources", "StagedHelperBundle",
    "DUAL_REFERENCE_PROTOCOL", "LEGACY_TRIO_PROTOCOL", "GeneratorCapabilityError",
    "LayeredStressRunner", "MAX_CPP_BYTES",
    "MAX_LARGE_INPUT_BYTES", "MAX_LARGE_OUTPUT_BYTES", "MAX_STREAM_BYTES", "SampleCase",
    "SandboxBackend", "SandboxCapability", "SandboxLimits", "SandboxProcessResult",
    "SandboxUnavailableError", "SourceSafetyError", "StopToken", "StressError",
    "StressExecutables", "StressRunConfig", "StressRunResult",
    "WindowsAppContainerBackend", "classify_dual_reference", "classify_three_way",
    "cpp_compiler_fingerprint", "probe_generator_v2", "save_failure_assets",
    "sha256_bytes", "sha256_file", "validate_cpp_source",
]
