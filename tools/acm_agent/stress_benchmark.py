"""Deterministic gold gates and reporting helpers for AI stress benchmarks.

The live model never receives these validators, expected outputs, or fixture
solutions.  They are an independent acceptance oracle for benchmark inputs.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


class GoldValidationError(ValueError):
    pass


GoldOracle = Callable[[bytes], bytes]


class ApplicationColdGateError(RuntimeError):
    """A provider-free or workspace-isolation invariant was violated."""


class LiveColdFixtureError(ValueError):
    """A checked-in live benchmark fixture is incomplete or was modified."""


class ManagedProcess(Protocol):
    """Small ``subprocess.Popen``-compatible surface used by the cold runner."""

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


Cleanup = Callable[[], None]


@dataclass(slots=True)
class ApplicationColdContext:
    """Fresh, disposable state handed to one provider-free cold attempt.

    The callback must create all of its state below ``workspace`` and use
    ``database_path`` as the new application database.  The database file is
    intentionally absent when the callback starts.  Long-lived children and
    non-process resources must be registered so the runner can verify cleanup.
    """

    attempt_index: int
    workspace: Path
    database_path: Path
    _processes: list[ManagedProcess] = field(default_factory=list, repr=False)
    _cleanups: list[Cleanup] = field(default_factory=list, repr=False)

    def register_process(self, process: ManagedProcess) -> ManagedProcess:
        self._processes.append(process)
        return process

    def register_cleanup(self, cleanup: Cleanup) -> Cleanup:
        self._cleanups.append(cleanup)
        return cleanup


@dataclass(frozen=True, slots=True)
class LiveColdProblem:
    """Everything a paid cold attempt may copy into its disposable workspace.

    ``primary_source`` is used only by the local controlled runner.  It is never
    included in model prompts.  Samples are kept structured so the production
    database/preflight path, rather than a benchmark-only shortcut, consumes
    them.
    """

    platform: str
    problem_id: str
    title: str
    statement: str
    primary_source: str
    samples: tuple[tuple[str, bytes, bytes], ...] = ()


LIVE_COLD_FIXTURE_SCHEMA_VERSION = 1


def _fixture_file(
    fixture_directory: Path,
    relative_name: object,
    expected_sha256: object,
    *,
    label: str,
) -> bytes:
    """Read one hash-locked fixture file without allowing path escapes."""

    name = str(relative_name or "").strip()
    digest = str(expected_sha256 or "").strip().casefold()
    if not name:
        raise LiveColdFixtureError(f"{label} file is required")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise LiveColdFixtureError(f"{label} sha256 must be a lowercase hex digest")
    relative = Path(name)
    if relative.is_absolute():
        raise LiveColdFixtureError(f"{label} file must be relative to its fixture")
    root = fixture_directory.resolve(strict=True)
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise LiveColdFixtureError(f"{label} file escapes or is missing") from exc
    if not path.is_file():
        raise LiveColdFixtureError(f"{label} path is not a regular file")
    payload = path.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise LiveColdFixtureError(
            f"{label} sha256 mismatch: expected {digest}, got {actual}"
        )
    return payload


def _fixture_text(payload: bytes, *, label: str) -> str:
    if b"\0" in payload:
        raise LiveColdFixtureError(f"{label} contains NUL")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LiveColdFixtureError(f"{label} is not UTF-8") from exc


def load_live_cold_problem_fixture(fixture_directory: Path) -> LiveColdProblem:
    """Load one immutable paid-benchmark fixture from ``problem.json``.

    The manifest pins the statement, trusted primary source, and every official
    sample by SHA-256.  This keeps a formal cold batch reproducible and prevents
    it from silently borrowing a user's current solution or mutable database
    context.  The primary source remains local to the controlled runner and is
    not part of any model prompt.

    Manifest schema v1::

        {
          "schema_version": 1,
          "platform": "luogu",
          "problem_id": "P1001",
          "title": "A+B Problem",
          "statement_file": "statement.md",
          "statement_sha256": "...",
          "primary_source_file": "primary.cpp",
          "primary_source_sha256": "...",
          "samples": [{
            "name": "official-1",
            "input_file": "samples/1.in",
            "input_sha256": "...",
            "output_file": "samples/1.out",
            "output_sha256": "..."
          }]
        }
    """

    root = Path(fixture_directory).resolve(strict=True)
    manifest_path = root / "problem.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveColdFixtureError("problem.json is missing or invalid UTF-8 JSON") from exc
    if not isinstance(raw, Mapping):
        raise LiveColdFixtureError("problem.json must contain one JSON object")
    if raw.get("schema_version") != LIVE_COLD_FIXTURE_SCHEMA_VERSION:
        raise LiveColdFixtureError(
            f"problem.json schema_version must be {LIVE_COLD_FIXTURE_SCHEMA_VERSION}"
        )

    statement = _fixture_text(
        _fixture_file(
            root,
            raw.get("statement_file"),
            raw.get("statement_sha256"),
            label="statement",
        ),
        label="statement",
    )
    primary_source = _fixture_text(
        _fixture_file(
            root,
            raw.get("primary_source_file"),
            raw.get("primary_source_sha256"),
            label="primary source",
        ),
        label="primary source",
    )

    raw_samples = raw.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise LiveColdFixtureError("at least one official sample is required")
    samples: list[tuple[str, bytes, bytes]] = []
    sample_names: set[str] = set()
    for index, item in enumerate(raw_samples, 1):
        if not isinstance(item, Mapping):
            raise LiveColdFixtureError(f"sample {index} must be one JSON object")
        name = str(item.get("name") or "").strip()
        if not name or name in sample_names:
            raise LiveColdFixtureError("official sample names must be non-empty and unique")
        sample_names.add(name)
        input_data = _fixture_file(
            root,
            item.get("input_file"),
            item.get("input_sha256"),
            label=f"sample {name} input",
        )
        output_data = _fixture_file(
            root,
            item.get("output_file"),
            item.get("output_sha256"),
            label=f"sample {name} output",
        )
        samples.append((name, input_data, output_data))

    problem = LiveColdProblem(
        platform=str(raw.get("platform") or "").strip().casefold(),
        problem_id=str(raw.get("problem_id") or "").strip().upper(),
        title=str(raw.get("title") or "").strip(),
        statement=statement,
        primary_source=primary_source,
        samples=tuple(samples),
    )
    if not problem.title:
        raise LiveColdFixtureError("live benchmark title is required")
    try:
        _validate_live_problem(problem)
    except (GoldValidationError, ValueError) as exc:
        raise LiveColdFixtureError(str(exc)) from exc
    return problem


def load_core8_live_cold_fixtures(root: Path) -> tuple[LiveColdProblem, ...]:
    """Load exactly one hash-locked fixture for every independent core8 oracle."""

    directory = Path(root).resolve(strict=True)
    problems = tuple(
        load_live_cold_problem_fixture(directory / gold.problem_id)
        for gold in CORE8
    )
    expected = tuple(gold.problem_id for gold in CORE8)
    actual = tuple(problem.problem_id for problem in problems)
    if actual != expected:
        raise LiveColdFixtureError(
            f"core8 fixture identities must be {expected}, got {actual}"
        )
    return problems


def build_core8_live_cold_attempt_plan(
    problems: Sequence[LiveColdProblem],
    *,
    p2596_attempts: int = 20,
    attempts_per_other_problem: int = 3,
) -> tuple[LiveColdProblem, ...]:
    """Build the formal 41-attempt plan with P2596's core8 runs reused.

    With the release defaults this returns 20 P2596 attempts followed by three
    attempts for each of the other seven problems: 20 + 7 * 3 = 41 paid setups.
    The first three P2596 attempts therefore serve simultaneously as its core8
    sample; no duplicate P2596 provider calls are scheduled.
    """

    if p2596_attempts < 3:
        raise ValueError("P2596 needs at least three attempts for core8 reuse")
    if attempts_per_other_problem < 1:
        raise ValueError("attempts per other problem must be positive")
    by_id: dict[str, LiveColdProblem] = {}
    for problem in problems:
        _validate_live_problem(problem)
        if problem.problem_id in by_id:
            raise ValueError(f"duplicate live benchmark problem {problem.problem_id}")
        by_id[problem.problem_id] = problem
    expected = {gold.problem_id for gold in CORE8}
    if set(by_id) != expected:
        missing = sorted(expected - set(by_id))
        extra = sorted(set(by_id) - expected)
        raise ValueError(f"core8 fixtures mismatch: missing={missing}, extra={extra}")

    plan: list[LiveColdProblem] = [by_id["P2596"]] * int(p2596_attempts)
    for gold in CORE8:
        if gold.problem_id == "P2596":
            continue
        plan.extend([by_id[gold.problem_id]] * int(attempts_per_other_problem))
    return tuple(plan)


class CountingProviderClient:
    """Thread-safe logical provider-call counter around a production client."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}
        self._provider_request_baseline = int(
            getattr(client, "provider_request_count", 0) or 0
        )

    @property
    def calls(self) -> dict[str, int]:
        actual = getattr(self._client, "provider_request_count", None)
        if isinstance(actual, int):
            return {"http": max(0, actual - self._provider_request_baseline)}
        with self._lock:
            return dict(self._calls)

    def _count(self, name: str) -> None:
        with self._lock:
            self._calls[name] = self._calls.get(name, 0) + 1

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        self._count("chat")
        return self._client.chat(*args, **kwargs)

    def chat_json(self, *args: Any, **kwargs: Any) -> Any:
        self._count("chat_json")
        return self._client.chat_json(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _tokens(data: bytes) -> list[str]:
    try:
        return data.decode("utf-8").split()
    except UnicodeDecodeError as exc:
        raise GoldValidationError("input is not UTF-8") from exc


def _ints(data: bytes) -> list[int]:
    values: list[int] = []
    for token in _tokens(data):
        try:
            values.append(int(token))
        except ValueError as exc:
            raise GoldValidationError(f"not an integer: {token!r}") from exc
    return values


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise GoldValidationError(message)


def _output(values: Iterable[int | str]) -> bytes:
    rows = [str(value) for value in values]
    return (("\n".join(rows) + "\n") if rows else "").encode()


def gold_p1001(data: bytes) -> bytes:
    values = _ints(data)
    _need(len(values) == 2, "P1001 requires exactly two integers")
    _need(all(-10**9 <= value <= 10**9 for value in values), "integer out of range")
    return _output([values[0] + values[1]])


def gold_p1111(data: bytes) -> bytes:
    values = _ints(data)
    _need(len(values) >= 2, "missing n,m")
    n, m = values[:2]
    _need(1 <= n <= 1000 and 0 <= m <= 100000, "n or m out of range")
    _need(len(values) == 2 + 3 * m, "edge count mismatch")
    edges: list[tuple[int, int, int]] = []
    for index in range(m):
        x, y, t = values[2 + 3 * index : 5 + 3 * index]
        _need(1 <= x <= n and 1 <= y <= n and 1 <= t <= 100000, "invalid edge")
        edges.append((t, x - 1, y - 1))
    if n == 1:
        return _output([0])
    parent = list(range(n))
    size = [1] * n

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    components = n
    for t, x, y in sorted(edges):
        x, y = find(x), find(y)
        if x != y:
            if size[x] < size[y]:
                x, y = y, x
            parent[y] = x
            size[x] += size[y]
            components -= 1
            if components == 1:
                return _output([t])
    return _output([-1])


def gold_p3379(data: bytes) -> bytes:
    values = _ints(data)
    _need(len(values) >= 3, "missing n,m,root")
    n, m, root = values[:3]
    _need(1 <= n <= 500000 and 0 <= m <= 500000 and 1 <= root <= n, "invalid header")
    _need(len(values) == 3 + 2 * (n - 1) + 2 * m, "record count mismatch")
    graph = [[] for _ in range(n)]
    offset = 3
    for _ in range(n - 1):
        u, v = values[offset : offset + 2]
        offset += 2
        _need(1 <= u <= n and 1 <= v <= n and u != v, "invalid tree edge")
        graph[u - 1].append(v - 1)
        graph[v - 1].append(u - 1)
    parent = [-2] * n
    depth = [0] * n
    parent[root - 1] = -1
    stack = [root - 1]
    for u in stack:
        for v in graph[u]:
            if v == parent[u]:
                continue
            _need(parent[v] == -2, "cycle in tree")
            parent[v] = u
            depth[v] = depth[u] + 1
            stack.append(v)
    _need(len(stack) == n, "tree is disconnected")
    answers: list[int] = []
    for _ in range(m):
        u, v = values[offset : offset + 2]
        offset += 2
        _need(1 <= u <= n and 1 <= v <= n, "invalid query endpoint")
        u -= 1
        v -= 1
        while depth[u] > depth[v]:
            u = parent[u]
        while depth[v] > depth[u]:
            v = parent[v]
        while u != v:
            u = parent[u]
            v = parent[v]
        answers.append(u + 1)
    return _output(answers)


def gold_p3834(data: bytes) -> bytes:
    values = _ints(data)
    _need(len(values) >= 2, "missing n,m")
    n, m = values[:2]
    _need(1 <= n <= 200000 and 0 <= m <= 200000, "invalid n,m")
    _need(len(values) == 2 + n + 3 * m, "record count mismatch")
    array = values[2 : 2 + n]
    offset = 2 + n
    answers: list[int] = []
    for _ in range(m):
        left, right, kth = values[offset : offset + 3]
        offset += 3
        _need(1 <= left <= right <= n, "invalid interval")
        _need(1 <= kth <= right - left + 1, "invalid kth")
        answers.append(sorted(array[left - 1 : right])[kth - 1])
    return _output(answers)


def gold_cf380c(data: bytes) -> bytes:
    tokens = _tokens(data)
    _need(len(tokens) >= 2, "missing string or q")
    brackets = tokens[0]
    _need(brackets and set(brackets) <= {"(", ")"}, "invalid bracket string")
    try:
        q = int(tokens[1])
    except ValueError as exc:
        raise GoldValidationError("invalid q") from exc
    _need(q >= 0 and len(tokens) == 2 + 2 * q, "query count mismatch")
    answers: list[int] = []
    offset = 2
    for _ in range(q):
        try:
            left, right = int(tokens[offset]), int(tokens[offset + 1])
        except ValueError as exc:
            raise GoldValidationError("invalid interval") from exc
        offset += 2
        _need(1 <= left <= right <= len(brackets), "invalid interval")
        opened = matched = 0
        for character in brackets[left - 1 : right]:
            if character == "(":
                opened += 1
            elif opened:
                opened -= 1
                matched += 1
        answers.append(2 * matched)
    return _output(answers)


def gold_p3373(data: bytes) -> bytes:
    values = _ints(data)
    _need(len(values) >= 3, "missing n,m,p")
    n, m, modulus = values[:3]
    _need(1 <= n <= 100000 and 0 <= m <= 100000 and modulus >= 1, "invalid header")
    _need(len(values) >= 3 + n, "missing array")
    array = [value % modulus for value in values[3 : 3 + n]]
    offset = 3 + n
    answers: list[int] = []
    for _ in range(m):
        _need(offset < len(values), "missing operation")
        op = values[offset]
        offset += 1
        _need(op in {1, 2, 3}, "invalid operation")
        arity = 3 if op in {1, 2} else 2
        _need(offset + arity <= len(values), "truncated operation")
        left, right = values[offset], values[offset + 1]
        _need(1 <= left <= right <= n, "invalid interval")
        if op in {1, 2}:
            value = values[offset + 2] % modulus
            for index in range(left - 1, right):
                array[index] = (
                    array[index] * value if op == 1 else array[index] + value
                ) % modulus
        else:
            answers.append(sum(array[left - 1 : right]) % modulus)
        offset += arity
    _need(offset == len(values), "trailing tokens")
    return _output(answers)


def gold_cf1354d(data: bytes) -> bytes:
    values = _ints(data)
    _need(len(values) >= 2, "missing n,q")
    n, q = values[:2]
    _need(0 <= n <= 1000000 and 0 <= q <= 1000000, "invalid n,q")
    _need(len(values) == 2 + n + q, "record count mismatch")
    multiset = sorted(values[2 : 2 + n])
    _need(all(value > 0 for value in multiset), "initial values must be positive")
    for value in values[2 + n :]:
        if value > 0:
            import bisect

            bisect.insort(multiset, value)
        else:
            kth = -value
            _need(1 <= kth <= len(multiset), "delete rank exceeds current size")
            multiset.pop(kth - 1)
    return _output([multiset[0] if multiset else 0])


def gold_p2596(data: bytes) -> bytes:
    tokens = _tokens(data)
    _need(len(tokens) >= 2, "missing n,m")
    try:
        n, m = int(tokens[0]), int(tokens[1])
    except ValueError as exc:
        raise GoldValidationError("invalid n,m") from exc
    _need(1 <= n <= 80000 and 0 <= m <= 80000, "invalid n,m")
    _need(len(tokens) >= 2 + n, "missing initial permutation")
    try:
        order = [int(token) for token in tokens[2 : 2 + n]]
    except ValueError as exc:
        raise GoldValidationError("invalid permutation") from exc
    _need(sorted(order) == list(range(1, n + 1)), "initial books are not a permutation")
    offset = 2 + n
    answers: list[int] = []
    for _ in range(m):
        _need(offset + 1 < len(tokens), "truncated operation")
        op = tokens[offset]
        try:
            value = int(tokens[offset + 1])
        except ValueError as exc:
            raise GoldValidationError("invalid operation argument") from exc
        offset += 2
        _need(op in {"Top", "Bottom", "Insert", "Ask", "Query"}, "unknown operation")
        if op == "Query":
            _need(1 <= value <= n, "query rank out of range")
            answers.append(order[value - 1])
            continue
        _need(1 <= value <= n, "book id out of range")
        position = order.index(value)
        if op == "Top":
            order.pop(position)
            order.insert(0, value)
        elif op == "Bottom":
            order.pop(position)
            order.append(value)
        elif op == "Ask":
            answers.append(position)
        else:
            _need(offset < len(tokens), "Insert is missing t")
            try:
                delta = int(tokens[offset])
            except ValueError as exc:
                raise GoldValidationError("invalid Insert delta") from exc
            offset += 1
            _need(delta in {-1, 0, 1}, "Insert delta must be -1, 0, or 1")
            target = position + delta
            _need(0 <= target < n, "Insert crosses the current boundary")
            if delta:
                order[position], order[target] = order[target], order[position]
    _need(offset == len(tokens), "trailing tokens")
    return _output(answers)


@dataclass(frozen=True, slots=True)
class GoldProblem:
    problem_id: str
    oracle: GoldOracle
    valid_cases: tuple[bytes, ...]
    invalid_cases: tuple[bytes, ...]


CORE8: tuple[GoldProblem, ...] = (
    GoldProblem("P1001", gold_p1001, (b"1 2\n", b"-1000000000 1000000000\n"), (b"1\n", b"1 2 3\n", b"1000000001 0\n")),
    GoldProblem("P1111", gold_p1111, (b"1 0\n", b"3 3\n1 2 5\n2 3 5\n1 1 1\n", b"3 1\n1 2 7\n"), (b"3 1\n1 4 2\n", b"2 2\n1 2 1\n")),
    GoldProblem("P3379", gold_p3379, (b"3 3 1\n1 2\n1 3\n2 3\n2 2\n1 3\n",), (b"3 1 1\n1 2\n1 2\n1 3\n", b"2 1 3\n1 2\n1 2\n")),
    GoldProblem("P3834", gold_p3834, (b"5 3\n2 -1 2 5 0\n1 5 1\n1 5 5\n2 4 2\n",), (b"2 1\n1 2\n2 1 1\n", b"2 1\n1 2\n1 2 3\n")),
    GoldProblem("CF380C", gold_cf380c, (b"(()())\n3\n1 6\n2 5\n1 1\n",), (b"abc\n0\n", b"()\n1\n2 1\n")),
    GoldProblem("P3373", gold_p3373, (b"3 5 7\n1 2 3\n3 1 3\n1 1 2 0\n2 2 3 1\n3 1 3\n3 2 2\n", b"1 1 1\n9\n3 1 1\n"), (b"1 1 7\n1\n4 1 1\n", b"1 1 7\n1\n3 2 1\n")),
    GoldProblem("CF1354D", gold_cf1354d, (b"3 4\n1 2 3\n-1 4 -3 -1\n", b"0 2\n5 -1\n"), (b"1 1\n1\n-2\n", b"1 0\n0\n")),
    GoldProblem(
        "P2596",
        gold_p2596,
        (
            b"3 7\n1 2 3\nAsk 1\nQuery 2\nInsert 2 0\nInsert 2 -1\nAsk 2\nBottom 2\nTop 3\n",
        ),
        (
            b"3 1\n1 2 3\nInsert 1 -1\n",
            b"3 1\n1 2 3\nInsert 2 2\n",
            b"3 1\n1 1 3\nAsk 1\n",
            b"3 2\n1 2 3\nTop 2\nInsert 2 -1\n",
            b"3 2\n1 2 3\nBottom 2\nInsert 2 1\n",
            b"3 2\n1 2 3\nInsert 2 -1\nInsert 2 -1\n",
        ),
    ),
)


def core8_random_valid_corpus(
    problem_id: str,
    *,
    seed: int = 0xC0DE_0008,
    case_count: int = 32,
) -> tuple[bytes, ...]:
    """Create deterministic legal inputs for independent primary validation.

    These small cases emphasize semantic state transitions and degenerate
    boundaries.  Expected outputs always come from the independent gold oracle;
    the corpus and oracle are never included in a paid model prompt.
    """

    if case_count < 1:
        raise ValueError("case_count must be positive")
    gold = next((item for item in CORE8 if item.problem_id == problem_id), None)
    if gold is None:
        raise ValueError(f"unknown core8 problem {problem_id}")
    salt = sum((index + 1) * ord(ch) for index, ch in enumerate(problem_id))
    rng = random.Random(int(seed) ^ salt)
    cases: list[bytes] = []

    for case_index in range(case_count):
        if problem_id == "P1001":
            values = [-10**9, -1, 0, 1, 10**9]
            a = rng.choice(values) if case_index < len(values) else rng.randint(-10**9, 10**9)
            b = rng.choice(values) if case_index < len(values) else rng.randint(-10**9, 10**9)
            text = f"{a} {b}\n"

        elif problem_id == "P1111":
            n = 1 if case_index == 0 else rng.randint(2, 24)
            edges: list[tuple[int, int, int]] = []
            connected = case_index % 3 != 1
            if connected:
                for vertex in range(2, n + 1):
                    edges.append((rng.randint(1, vertex - 1), vertex, rng.randint(1, 100000)))
            extra = rng.randint(0, 2 * n)
            component_limit = n if connected else max(1, n // 2)
            for _ in range(extra):
                x = rng.randint(1, component_limit)
                y = rng.randint(1, component_limit)
                edges.append((x, y, rng.randint(1, 100000)))
            rng.shuffle(edges)
            rows = [f"{n} {len(edges)}", *(f"{x} {y} {t}" for x, y, t in edges)]
            text = "\n".join(rows) + "\n"

        elif problem_id == "P3379":
            n = 1 if case_index == 0 else rng.randint(2, 60)
            query_count = rng.randint(0, 60)
            root = rng.randint(1, n)
            edges = [(rng.randint(1, vertex - 1), vertex) for vertex in range(2, n + 1)]
            rng.shuffle(edges)
            queries = [(rng.randint(1, n), rng.randint(1, n)) for _ in range(query_count)]
            rows = [
                f"{n} {query_count} {root}",
                *(f"{u} {v}" for u, v in edges),
                *(f"{u} {v}" for u, v in queries),
            ]
            text = "\n".join(rows) + "\n"

        elif problem_id == "P3834":
            n = 1 if case_index == 0 else rng.randint(2, 60)
            query_count = rng.randint(0, 60)
            pool = [-10**9, -7, -1, 0, 1, 7, 10**9]
            array = [
                rng.choice(pool) if rng.randrange(3) == 0 else rng.randint(-100, 100)
                for _ in range(n)
            ]
            queries: list[tuple[int, int, int]] = []
            for _ in range(query_count):
                left = rng.randint(1, n)
                right = rng.randint(left, n)
                queries.append((left, right, rng.randint(1, right - left + 1)))
            rows = [
                f"{n} {query_count}",
                " ".join(map(str, array)),
                *(f"{left} {right} {k}" for left, right, k in queries),
            ]
            text = "\n".join(rows) + "\n"

        elif problem_id == "CF380C":
            length = 1 if case_index == 0 else rng.randint(2, 100)
            brackets = "".join(rng.choice("()") for _ in range(length))
            query_count = rng.randint(0, 60)
            queries: list[tuple[int, int]] = []
            for _ in range(query_count):
                left = rng.randint(1, length)
                queries.append((left, rng.randint(left, length)))
            rows = [brackets, str(query_count), *(f"{left} {right}" for left, right in queries)]
            text = "\n".join(rows) + "\n"

        elif problem_id == "P3373":
            n = 1 if case_index == 0 else rng.randint(2, 35)
            operation_count = rng.randint(0, 70)
            modulus = 1 if case_index == 0 else rng.choice([1, 2, 7, 97, 10**9])
            array = [rng.randint(-10**9, 10**9) for _ in range(n)]
            operations: list[str] = []
            for operation_index in range(operation_count):
                kind = 3 if operation_index == 0 else rng.randint(1, 3)
                left = rng.randint(1, n)
                right = rng.randint(left, n)
                if kind == 3:
                    operations.append(f"3 {left} {right}")
                else:
                    operations.append(f"{kind} {left} {right} {rng.randint(-10**9, 10**9)}")
            rows = [
                f"{n} {operation_count} {modulus}",
                " ".join(map(str, array)),
                *operations,
            ]
            text = "\n".join(rows) + "\n"

        elif problem_id == "CF1354D":
            n = 0 if case_index == 0 else rng.randint(0, 35)
            initial = [rng.randint(1, 100) for _ in range(n)]
            current_size = n
            operation_count = rng.randint(0, 70)
            commands: list[int] = []
            for _ in range(operation_count):
                if current_size == 0 or rng.randrange(2) == 0:
                    commands.append(rng.randint(1, 100))
                    current_size += 1
                else:
                    commands.append(-rng.randint(1, current_size))
                    current_size -= 1
            rows = [
                f"{n} {operation_count}",
                " ".join(map(str, initial)),
                " ".join(map(str, commands)),
            ]
            text = "\n".join(rows) + "\n"

        else:
            n = 1 if case_index == 0 else rng.randint(2, 35)
            order = list(range(1, n + 1))
            rng.shuffle(order)
            operation_count = rng.randint(0, 70)
            operations: list[str] = []
            for operation_index in range(operation_count):
                kind = ["Top", "Bottom", "Insert", "Ask", "Query"][operation_index % 5]
                if operation_index >= 5:
                    kind = rng.choice(["Top", "Bottom", "Insert", "Ask", "Query"])
                if kind == "Query":
                    operations.append(f"Query {rng.randint(1, n)}")
                    continue
                book = rng.randint(1, n)
                position = order.index(book)
                if kind == "Top":
                    order.pop(position); order.insert(0, book)
                    operations.append(f"Top {book}")
                elif kind == "Bottom":
                    order.pop(position); order.append(book)
                    operations.append(f"Bottom {book}")
                elif kind == "Ask":
                    operations.append(f"Ask {book}")
                else:
                    deltas = [0]
                    if position > 0: deltas.append(-1)
                    if position + 1 < n: deltas.append(1)
                    delta = rng.choice(deltas)
                    if delta:
                        order[position], order[position + delta] = (
                            order[position + delta], order[position]
                        )
                    operations.append(f"Insert {book} {delta}")
            initial = list(range(1, n + 1))
            rng.shuffle(initial)
            # Re-simulate operations from the emitted initial order.  Generation
            # above changed a private state only to choose legal deltas, so build
            # a fresh legal sequence tied to the actual printed permutation.
            order = initial[:]
            legal_operations: list[str] = []
            for raw in operations:
                parts = raw.split()
                kind, value = parts[0], int(parts[1])
                if kind == "Query":
                    legal_operations.append(raw)
                    continue
                position = order.index(value)
                if kind == "Top":
                    order.pop(position); order.insert(0, value)
                elif kind == "Bottom":
                    order.pop(position); order.append(value)
                elif kind == "Insert":
                    delta = int(parts[2])
                    if not 0 <= position + delta < n:
                        delta = 0
                    if delta:
                        order[position], order[position + delta] = (
                            order[position + delta], order[position]
                        )
                    raw = f"Insert {value} {delta}"
                legal_operations.append(raw)
            rows = [
                f"{n} {len(legal_operations)}",
                " ".join(map(str, initial)),
                *legal_operations,
            ]
            text = "\n".join(rows) + "\n"

        encoded = text.encode("utf-8")
        gold.oracle(encoded)
        cases.append(encoded)
    return tuple(cases)


def run_core8_gold_gate() -> dict[str, object]:
    problems: list[dict[str, object]] = []
    accepted = rejected = false_accepts = false_rejects = 0
    for problem in CORE8:
        valid_results: list[str] = []
        for case in problem.valid_cases:
            try:
                first = problem.oracle(case)
                second = problem.oracle(case)
                _need(first == second, "gold oracle is nondeterministic")
                accepted += 1
                valid_results.append(first.decode("utf-8"))
            except GoldValidationError:
                false_rejects += 1
        for case in problem.invalid_cases:
            try:
                problem.oracle(case)
            except GoldValidationError:
                rejected += 1
            else:
                false_accepts += 1
        problems.append(
            {
                "problem_id": problem.problem_id,
                "valid_cases": len(problem.valid_cases),
                "invalid_cases": len(problem.invalid_cases),
                "valid_outputs": valid_results,
            }
        )
    return {
        "ok": false_accepts == 0 and false_rejects == 0,
        "accepted_valid": accepted,
        "rejected_invalid": rejected,
        "legal_corpus_accept_rate": (
            accepted / (accepted + false_rejects)
            if accepted + false_rejects
            else 1.0
        ),
        "invalid_corpus_reject_rate": (
            rejected / (rejected + false_accepts)
            if rejected + false_accepts
            else 1.0
        ),
        "mutation_kill_rate": (
            rejected / (rejected + false_accepts)
            if rejected + false_accepts
            else 1.0
        ),
        "false_accepts": false_accepts,
        "false_rejects": false_rejects,
        "problems": problems,
    }


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _numeric_mapping(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float] = {}
    for key, raw in value.items():
        number = _number(raw)
        if number is not None:
            result[str(key)] = number
    return result


def _numeric_total(value: object) -> float | None:
    number = _number(value)
    if number is not None:
        return number
    values = _numeric_mapping(value)
    return sum(values.values()) if values else None


def _repair_roles(item: Mapping[str, object]) -> list[str]:
    raw = item.get("repair_roles", item.get("repair_role", []))
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Mapping):
        values = [str(key) for key, count in raw.items() if _number(count)]
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        values = [str(value) for value in raw]
    else:
        values = []
    if not values:
        error = item.get("error")
        details = error.get("details") if isinstance(error, Mapping) else None
        if isinstance(details, Mapping):
            attempts = details.get("attempts")
            if isinstance(attempts, Mapping):
                values.extend(
                    str(role)
                    for role, count in attempts.items()
                    if (_number(count) or 0) > 0
                )
    return sorted({value.strip() for value in values if value.strip()})


def _attempt_tokens(item: Mapping[str, object]) -> dict[str, float]:
    return _numeric_mapping(item.get("tokens", item.get("token_usage", {})))


def _attempt_stages(item: Mapping[str, object]) -> dict[str, float]:
    return _numeric_mapping(
        item.get("stage_seconds", item.get("stage_durations", item.get("phase_seconds", {})))
    )


def _attempt_retries(item: Mapping[str, object]) -> float:
    direct = _number(item.get("retries"))
    if direct is not None:
        return direct
    return _numeric_total(item.get("retry_counts")) or 0.0


def _attempt_provider_requests(item: Mapping[str, object]) -> float:
    return _numeric_total(item.get("provider_requests")) or 0.0


def _attempt_total_tokens(item: Mapping[str, object]) -> float | None:
    direct = _number(item.get("total_tokens"))
    if direct is not None:
        return direct
    tokens = _attempt_tokens(item)
    return sum(tokens.values()) if tokens else None


def _stats(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0 if values else None,
    }


def _counter(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


def _canonical_attempt(item: Mapping[str, object], index: int) -> dict[str, object]:
    result = dict(item)
    ok = bool(item.get("ok"))
    explicit_first_round = item.get("first_round_success", item.get("first_pass_success"))
    first_round = (
        ok and not _repair_roles(item)
        if explicit_first_round is None
        else ok and bool(explicit_first_round)
    )
    stages = _attempt_stages(item)
    tokens = _attempt_tokens(item)
    provider_requests = item.get("provider_requests", 0)
    total_tokens = _attempt_total_tokens(item)
    wall_seconds = _number(item.get("wall_seconds"))
    result.update(
        {
            "attempt_index": int(item.get("attempt_index", index)),
            "ok": ok,
            "first_round_success": first_round,
            "repair_roles": _repair_roles(item),
            "failure_stage": None if ok else str(item.get("failure_stage") or "unknown"),
            "stage_seconds": stages,
            "tokens": tokens,
            "total_tokens": total_tokens,
            "provider_requests": provider_requests,
            "provider_request_count": _attempt_provider_requests(item),
            "retries": _attempt_retries(item),
            "unsafe_apply": bool(
                item.get("unsafe_apply", item.get("invalid_bundle_applied", False))
            ),
            "wall_seconds": wall_seconds,
        }
    )
    return result


def summarize_attempts(
    attempts: Sequence[Mapping[str, object]], *, _include_by_problem: bool = True
) -> dict[str, object]:
    canonical = [_canonical_attempt(item, index + 1) for index, item in enumerate(attempts)]
    successful = [item for item in canonical if item["ok"]]
    wall = [value for item in canonical if (value := _number(item["wall_seconds"])) is not None]
    successful_wall = [
        value for item in successful if (value := _number(item["wall_seconds"])) is not None
    ]
    total_tokens = [
        value for item in canonical if (value := _number(item["total_tokens"])) is not None
    ]
    successful_tokens = [
        value for item in successful if (value := _number(item["total_tokens"])) is not None
    ]

    stage_names = sorted({name for item in canonical for name in _attempt_stages(item)})
    token_names = sorted({name for item in canonical for name in _attempt_tokens(item)})
    repair_roles = _counter(role for item in canonical for role in _repair_roles(item))
    failure_stages = _counter(
        str(item["failure_stage"]) for item in canonical if not item["ok"]
    )
    first_round = sum(bool(item["first_round_success"]) for item in canonical)
    provider_requests = [_attempt_provider_requests(item) for item in canonical]
    retries = [_attempt_retries(item) for item in canonical]

    result: dict[str, object] = {
        "attempts": len(canonical),
        "successes": len(successful),
        "failures": len(canonical) - len(successful),
        "success_rate": len(successful) / len(canonical) if canonical else 0.0,
        "first_round_successes": first_round,
        "first_round_success_rate": first_round / len(canonical) if canonical else 0.0,
        "unsafe_applies": sum(bool(item["unsafe_apply"]) for item in canonical),
        "repair_roles": repair_roles,
        "failure_stages": failure_stages,
        "wall_seconds": _stats(wall),
        "successful_wall_seconds": _stats(successful_wall),
        "total_tokens": _stats(total_tokens),
        "successful_total_tokens": _stats(successful_tokens),
        "stage_seconds": {
            name: _stats(
                [
                    stages[name]
                    for item in canonical
                    if name in (stages := _attempt_stages(item))
                ]
            )
            for name in stage_names
        },
        "token_categories": {
            name: _stats(
                [
                    tokens[name]
                    for item in canonical
                    if name in (tokens := _attempt_tokens(item))
                ]
            )
            for name in token_names
        },
        "provider_requests": {
            "total": sum(provider_requests),
            "stats": _stats(provider_requests),
        },
        "retries": {"total": sum(retries), "stats": _stats(retries)},
    }
    if _include_by_problem:
        problem_ids = sorted(
            {str(item["problem_id"]) for item in canonical if item.get("problem_id")}
        )
        result["by_problem"] = {
            problem_id: summarize_attempts(
                [item for item in canonical if str(item.get("problem_id")) == problem_id],
                _include_by_problem=False,
            )
            for problem_id in problem_ids
        }
    return result


def evaluate_reliability_release(
    ai_attempts: Sequence[Mapping[str, object]],
    application_cold_attempts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Evaluate every quantitative release gate from the reliability plan."""

    expected_ids = [gold.problem_id for gold in CORE8]
    by_problem = {
        problem_id: sorted(
            [item for item in ai_attempts if item.get("problem_id") == problem_id],
            key=lambda item: int(_number(item.get("attempt_index")) or 0),
        )
        for problem_id in expected_ids
    }
    p2596 = by_problem["P2596"]
    core8: list[Mapping[str, object]] = p2596[:3]
    for problem_id in expected_ids:
        if problem_id != "P2596":
            core8.extend(by_problem[problem_id][:3])

    def successful(items: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
        return [item for item in items if bool(item.get("ok"))]

    def metric(
        items: Sequence[Mapping[str, object]], key: str, fraction: float
    ) -> float | None:
        values = [
            value
            for item in successful(items)
            if (value := _number(item.get(key))) is not None
        ]
        return percentile(values, fraction)

    def within(value: float | None, limit: float) -> bool:
        return value is not None and value <= limit

    p2596_successes = len(successful(p2596))
    core8_successes = len(successful(core8))
    p2596_wall_p50 = metric(p2596, "wall_seconds", 0.50)
    p2596_wall_p95 = metric(p2596, "wall_seconds", 0.95)
    p2596_token_p50 = metric(p2596, "total_tokens", 0.50)
    p2596_token_p95 = metric(p2596, "total_tokens", 0.95)
    core8_wall_p50 = metric(core8, "wall_seconds", 0.50)
    core8_wall_p95 = metric(core8, "wall_seconds", 0.95)
    core8_token_p50 = metric(core8, "total_tokens", 0.50)
    core8_token_p95 = metric(core8, "total_tokens", 0.95)

    local_wall = [
        value
        for item in application_cold_attempts
        if (value := _number(item.get("wall_seconds"))) is not None
    ]
    local_provider_requests = sum(
        _numeric_total(item.get("provider_requests")) or 0
        for item in application_cold_attempts
    )
    local_tokens = sum(
        _number(item.get("total_tokens")) or 0
        for item in application_cold_attempts
    )
    warm_by_problem = {
        problem_id: any(
            _warm_cache_is_zero_provider(item.get("warm_cache"))
            for item in by_problem[problem_id]
        )
        for problem_id in expected_ids
    }
    per_problem_core_successes = {
        problem_id: len(successful(by_problem[problem_id][:3]))
        for problem_id in expected_ids
    }
    exact_ai_counts = len(p2596) == 20 and all(
        len(by_problem[problem_id]) == 3
        for problem_id in expected_ids
        if problem_id != "P2596"
    )
    checks = {
        "ai_attempt_counts": exact_ai_counts and len(ai_attempts) == 41,
        "p2596_successes_at_least_16_of_20": p2596_successes >= 16,
        "core8_successes_at_least_20_of_24": core8_successes >= 20,
        "each_core8_problem_at_least_2_of_3": all(
            count >= 2 for count in per_problem_core_successes.values()
        ),
        "zero_unsafe_applies": not any(
            bool(item.get("unsafe_apply")) for item in ai_attempts
        ),
        "p2596_success_wall_p50_at_most_240": within(p2596_wall_p50, 240),
        "p2596_success_wall_p95_at_most_480": within(p2596_wall_p95, 480),
        "core8_success_wall_p50_at_most_180": within(core8_wall_p50, 180),
        "core8_success_wall_p95_at_most_360": within(core8_wall_p95, 360),
        "p2596_success_tokens_p50_at_most_45000": within(p2596_token_p50, 45000),
        "p2596_success_tokens_p95_at_most_80000": within(p2596_token_p95, 80000),
        "core8_success_tokens_p50_at_most_35000": within(core8_token_p50, 35000),
        "core8_success_tokens_p95_at_most_60000": within(core8_token_p95, 60000),
        "successful_ai_attempts_at_most_100000_tokens": all(
            (_number(item.get("total_tokens")) or 0) <= 100000
            for item in successful(ai_attempts)
        ),
        "warm_cache_zero_provider_for_every_problem": all(warm_by_problem.values()),
        "local_application_cold_exactly_20": len(application_cold_attempts) == 20,
        "local_application_cold_20_of_20": (
            len(application_cold_attempts) == 20
            and all(bool(item.get("ok")) for item in application_cold_attempts)
        ),
        "local_application_cold_provider_free": (
            local_provider_requests == 0 and local_tokens == 0
        ),
        "local_application_cold_cleaned": all(
            bool(item.get("application_cold"))
            and bool(item.get("registered_processes_cleaned"))
            and not bool(item.get("unsafe_apply"))
            for item in application_cold_attempts
        ),
        "local_application_cold_wall_p95_at_most_90": within(
            percentile(local_wall, 0.95), 90
        ),
        "local_application_cold_wall_max_at_most_105": (
            bool(local_wall) and max(local_wall) <= 105
        ),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "metrics": {
            "p2596_attempts": len(p2596),
            "p2596_successes": p2596_successes,
            "core8_attempts": len(core8),
            "core8_successes": core8_successes,
            "core8_successes_by_problem": per_problem_core_successes,
            "p2596_success_wall_seconds": {
                "p50": p2596_wall_p50,
                "p95": p2596_wall_p95,
            },
            "core8_success_wall_seconds": {
                "p50": core8_wall_p50,
                "p95": core8_wall_p95,
            },
            "p2596_success_tokens": {
                "p50": p2596_token_p50,
                "p95": p2596_token_p95,
            },
            "core8_success_tokens": {
                "p50": core8_token_p50,
                "p95": core8_token_p95,
            },
            "warm_cache_by_problem": warm_by_problem,
            "local_application_cold_wall_seconds": {
                "p95": percentile(local_wall, 0.95),
                "max": max(local_wall) if local_wall else None,
            },
        },
    }


def _csv_rows(attempts: Sequence[Mapping[str, object]]) -> tuple[list[str], list[dict[str, object]]]:
    canonical = [_canonical_attempt(item, index + 1) for index, item in enumerate(attempts)]
    stage_names = sorted({name for item in canonical for name in _attempt_stages(item)})
    token_names = sorted({name for item in canonical for name in _attempt_tokens(item)})
    provider_names = sorted(
        {
            name
            for item in canonical
            for name in _numeric_mapping(item.get("provider_requests"))
        }
    )
    fields = [
        "attempt_index",
        "problem_id",
        "ok",
        "first_round_success",
        "repair_roles",
        "failure_stage",
        "wall_seconds",
        "total_tokens",
        "provider_request_count",
        "retries",
        "unsafe_apply",
    ]
    fields += [f"stage_seconds.{name}" for name in stage_names]
    fields += [f"tokens.{name}" for name in token_names]
    fields += [f"provider_requests.{name}" for name in provider_names]
    fields += ["stage_seconds_json", "tokens_json", "provider_requests_json", "error"]
    rows: list[dict[str, object]] = []
    for item in canonical:
        stages = _attempt_stages(item)
        tokens = _attempt_tokens(item)
        providers = _numeric_mapping(item.get("provider_requests"))
        row: dict[str, object] = {
            key: item.get(key, "")
            for key in fields
            if not key.startswith(("stage_seconds.", "tokens.", "provider_requests."))
        }
        row["repair_roles"] = ";".join(_repair_roles(item))
        row["stage_seconds_json"] = json.dumps(stages, ensure_ascii=False, sort_keys=True)
        row["tokens_json"] = json.dumps(tokens, ensure_ascii=False, sort_keys=True)
        row["provider_requests_json"] = json.dumps(
            item.get("provider_requests", 0), ensure_ascii=False, sort_keys=True
        )
        for name in stage_names:
            row[f"stage_seconds.{name}"] = stages.get(name, "")
        for name in token_names:
            row[f"tokens.{name}"] = tokens.get(name, "")
        for name in provider_names:
            row[f"provider_requests.{name}"] = providers.get(name, "")
        rows.append(row)
    return fields, rows


def _markdown(summary: Mapping[str, object]) -> str:
    wall = summary["successful_wall_seconds"]
    tokens = summary["successful_total_tokens"]
    assert isinstance(wall, Mapping) and isinstance(tokens, Mapping)

    def percent(value: object) -> str:
        return f"{100.0 * float(value):.1f}%"

    def scalar(value: object) -> str:
        return "n/a" if value is None else f"{float(value):.3f}"

    lines = [
        "# AI Stress Benchmark Report",
        "",
        "## Outcome",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Attempts | {summary['attempts']} |",
        f"| Successes | {summary['successes']} |",
        f"| Success rate | {percent(summary['success_rate'])} |",
        f"| First-round success rate | {percent(summary['first_round_success_rate'])} |",
        f"| Unsafe applies | {summary['unsafe_applies']} |",
        f"| Successful time p50 / p95 | {scalar(wall['p50'])} / {scalar(wall['p95'])} s |",
        f"| Successful tokens p50 / p95 | {scalar(tokens['p50'])} / {scalar(tokens['p95'])} |",
        f"| Provider requests | {scalar(summary['provider_requests']['total'])} |",  # type: ignore[index]
        f"| Retries | {scalar(summary['retries']['total'])} |",  # type: ignore[index]
        "",
        "## Failure stages",
        "",
        "| Stage | Attempts |",
        "|---|---:|",
    ]
    failures = summary["failure_stages"]
    assert isinstance(failures, Mapping)
    lines.extend(
        f"| {str(stage).replace('|', '\\|')} | {count} |" for stage, count in failures.items()
    )
    if not failures:
        lines.append("| none | 0 |")
    lines += ["", "## Repair roles", "", "| Role | Attempts |", "|---|---:|"]
    roles = summary["repair_roles"]
    assert isinstance(roles, Mapping)
    lines.extend(f"| {str(role).replace('|', '\\|')} | {count} |" for role, count in roles.items())
    if not roles:
        lines.append("| none | 0 |")
    lines += ["", "## Stage duration percentiles", "", "| Stage | p50 | p95 | max |", "|---|---:|---:|---:|"]
    stage_seconds = summary["stage_seconds"]
    assert isinstance(stage_seconds, Mapping)
    for stage, raw_stats in stage_seconds.items():
        assert isinstance(raw_stats, Mapping)
        lines.append(
            f"| {str(stage).replace('|', '\\|')} | {scalar(raw_stats['p50'])} | "
            f"{scalar(raw_stats['p95'])} | {scalar(raw_stats['max'])} |"
        )
    if not stage_seconds:
        lines.append("| none | n/a | n/a | n/a |")
    lines += ["", "## Token category percentiles", "", "| Category | p50 | p95 | max |", "|---|---:|---:|---:|"]
    categories = summary["token_categories"]
    assert isinstance(categories, Mapping)
    for category, raw_stats in categories.items():
        assert isinstance(raw_stats, Mapping)
        lines.append(
            f"| {str(category).replace('|', '\\|')} | {scalar(raw_stats['p50'])} | "
            f"{scalar(raw_stats['p95'])} | {scalar(raw_stats['max'])} |"
        )
    if not categories:
        lines.append("| none | n/a | n/a | n/a |")
    return "\n".join(lines) + "\n"


def write_benchmark_report(
    directory: Path, attempts: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Write immutable JSONL, CSV, JSON, and Markdown benchmark evidence."""

    # A live benchmark creates the directory first and appends a crash-safe raw
    # journal after every paid attempt.  The four final artifacts remain
    # immutable because every file is still opened with mode ``x``.
    directory.mkdir(parents=True, exist_ok=True)
    canonical = [_canonical_attempt(item, index + 1) for index, item in enumerate(attempts)]
    with (directory / "attempts.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for item in canonical:
            stream.write(
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) + "\n"
            )
    fields, rows = _csv_rows(canonical)
    with (directory / "attempts.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary = summarize_attempts(canonical)
    with (directory / "summary.json").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    with (directory / "report.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_markdown(summary))
    return summary


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_fingerprint(root: Path) -> dict[str, tuple[object, ...]]:
    """Content fingerprint that does not follow links outside the workspace."""

    if not root.is_dir():
        raise ApplicationColdGateError(f"protected workspace is not a directory: {root}")
    result: dict[str, tuple[object, ...]] = {}
    try:
        paths = sorted(root.rglob("*"), key=lambda path: path.as_posix())
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                result[relative] = ("directory",)
            elif path.is_file():
                stat = path.stat()
                result[relative] = (
                    "file",
                    stat.st_size,
                    stat.st_mode,
                    _hash_file(path),
                )
            else:
                result[relative] = ("other", path.stat().st_mode)
    except OSError as exc:
        raise ApplicationColdGateError("protected workspace changed during fingerprinting") from exc
    return result


def _selected_workspace_fingerprint(
    root: Path, selected_paths: Sequence[Path]
) -> dict[str, tuple[object, ...]]:
    result: dict[str, tuple[object, ...]] = {}
    for requested in selected_paths:
        path = requested if requested.is_absolute() else root / requested
        resolved_parent = path.parent.resolve()
        if not resolved_parent.is_relative_to(root):
            raise ApplicationColdGateError("protected path escapes protected workspace")
        relative = path.absolute().relative_to(root.absolute()).as_posix()
        if not path.exists():
            result[relative] = ("missing",)
        elif path.is_symlink():
            result[relative] = ("link", os.readlink(path))
        elif path.is_file():
            stat = path.stat()
            result[relative] = ("file", stat.st_size, stat.st_mode, _hash_file(path))
        elif path.is_dir():
            nested = _workspace_fingerprint(path)
            result[relative] = (
                "directory",
                hashlib.sha256(
                    json.dumps(
                        nested, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest(),
            )
        else:
            result[relative] = ("other",)
    return result


def _stop_registered_processes(processes: Sequence[ManagedProcess]) -> list[str]:
    errors: list[str] = []
    for process in reversed(processes):
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3.0)
                except Exception:
                    process.kill()
                    process.wait(timeout=3.0)
            if process.poll() is None:
                errors.append("registered process remained alive")
        except Exception as exc:
            errors.append(f"registered process cleanup failed: {type(exc).__name__}")
    return errors


def _run_registered_cleanups(cleanups: Sequence[Cleanup]) -> list[str]:
    errors: list[str] = []
    for cleanup in reversed(cleanups):
        try:
            cleanup()
        except Exception as exc:
            errors.append(f"registered cleanup failed: {type(exc).__name__}")
    return errors


def _provider_free_evidence(result: Mapping[str, object]) -> None:
    if "provider_requests" not in result:
        raise ApplicationColdGateError("attempt omitted provider_requests evidence")
    if _attempt_provider_requests(result) != 0:
        raise ApplicationColdGateError("application-cold attempt made a provider request")
    if "total_tokens" not in result and not _attempt_tokens(result):
        raise ApplicationColdGateError("attempt omitted provider token evidence")
    if (_attempt_total_tokens(result) or 0.0) != 0:
        raise ApplicationColdGateError("application-cold attempt consumed provider tokens")


ApplicationColdAttempt = Callable[[ApplicationColdContext], Mapping[str, object]]


def run_application_cold(
    attempt_count: int,
    attempt: ApplicationColdAttempt,
    *,
    protected_workspace: Path,
    temp_parent: Path | None = None,
    protected_paths: Sequence[Path] | None = None,
) -> list[dict[str, object]]:
    """Run application-cold attempts without provider access or durable writes.

    This orchestration layer deliberately has no provider object or credential.
    The injected callback must return explicit zero ``provider_requests`` and
    ``total_tokens`` evidence.  Any missing evidence, leaked registered child,
    cleanup failure, or protected-workspace mutation aborts the batch.
    """

    if attempt_count <= 0:
        raise ValueError("attempt_count must be positive")
    protected = protected_workspace.resolve(strict=True)
    parent = temp_parent.resolve(strict=True) if temp_parent is not None else None
    fingerprint = (
        (lambda: _selected_workspace_fingerprint(protected, protected_paths))
        if protected_paths is not None
        else (lambda: _workspace_fingerprint(protected))
    )
    baseline = fingerprint()
    results: list[dict[str, object]] = []
    used_workspaces: set[Path] = set()

    for index in range(1, attempt_count + 1):
        disposable_path: Path | None = None
        cleanup_errors: list[str] = []
        pending_error: BaseException | None = None
        canonical: dict[str, object] | None = None
        with tempfile.TemporaryDirectory(
            prefix=f"acm-application-cold-{index:03d}-",
            dir=str(parent) if parent is not None else None,
        ) as temporary:
            disposable_path = Path(temporary).resolve(strict=True)
            try:
                disposable_path.relative_to(protected)
            except ValueError:
                pass
            else:
                raise ApplicationColdGateError(
                    "application-cold workspace must be outside the protected workspace"
                )
            if disposable_path in used_workspaces:
                raise ApplicationColdGateError("application-cold workspace was reused")
            used_workspaces.add(disposable_path)

            workspace = disposable_path / "workspace"
            workspace.mkdir()
            (workspace / ".acm").mkdir()
            database_path = workspace / ".acm" / "state.db"
            if database_path.exists():
                raise ApplicationColdGateError("application-cold database is not empty")
            context = ApplicationColdContext(index, workspace, database_path)
            try:
                try:
                    raw_result = attempt(context)
                    if not isinstance(raw_result, Mapping):
                        raise ApplicationColdGateError(
                            "attempt callback did not return a mapping"
                        )
                    _provider_free_evidence(raw_result)
                    canonical = _canonical_attempt(raw_result, index)
                    canonical.update(
                        {
                            "attempt_index": index,
                            "application_cold": True,
                            "workspace_fresh": True,
                            "database_fresh": True,
                        }
                    )
                except BaseException as exc:
                    pending_error = exc
            finally:
                cleanup_errors.extend(_run_registered_cleanups(context._cleanups))
                cleanup_errors.extend(_stop_registered_processes(context._processes))

        assert disposable_path is not None
        if disposable_path.exists():
            raise ApplicationColdGateError("temporary application-cold workspace leaked")
        if fingerprint() != baseline:
            raise ApplicationColdGateError("protected workspace changed during cold attempt")
        if cleanup_errors:
            raise ApplicationColdGateError("; ".join(cleanup_errors)) from pending_error
        if pending_error is not None:
            raise pending_error
        assert canonical is not None
        canonical["registered_processes_cleaned"] = True
        results.append(canonical)

    return results


LiveClientFactory = Callable[[], Any]
LiveProgress = Callable[[Mapping[str, object]], None]


def _stored_problem_id(platform: str, problem_id: str) -> str:
    if platform == "codeforces" and problem_id.upper().startswith("CF"):
        return problem_id[2:]
    return problem_id


def _live_problem_identity(problem: LiveColdProblem) -> dict[str, object]:
    return {
        "platform": problem.platform,
        "problem_id": problem.problem_id,
        "title": problem.title,
        "statement_sha256": hashlib.sha256(problem.statement.encode("utf-8")).hexdigest(),
        "primary_source_sha256": hashlib.sha256(
            problem.primary_source.encode("utf-8")
        ).hexdigest(),
        "samples": [
            {
                "name": name,
                "input_sha256": hashlib.sha256(input_data).hexdigest(),
                "output_sha256": hashlib.sha256(output_data).hexdigest(),
            }
            for name, input_data, output_data in problem.samples
        ],
    }


def _live_protected_paths(
    protected: Path,
    problems: Sequence[LiveColdProblem],
    explicit: Sequence[Path] | None,
) -> tuple[Path, ...]:
    """Bind real database/config and every problem-named user artifact."""

    selected = {
        Path(".acm/state.db"),
        Path(".acm/state.db-wal"),
        Path(".acm/state.db-shm"),
        Path(".acm/config.json"),
    }
    selected.update(Path(path) for path in explicit or ())
    prefixes = tuple(
        sorted({problem.problem_id.casefold() for problem in problems}, key=len, reverse=True)
    )
    for path in protected.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.casefold()
        if any(
            name == prefix
            or name.startswith(prefix + ".")
            or name.startswith(prefix + "_")
            or name.startswith(prefix + "-")
            for prefix in prefixes
        ):
            selected.add(path.relative_to(protected))
    return tuple(sorted(selected, key=lambda path: str(path).casefold()))


def _warm_cache_is_zero_provider(value: object) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("ok")
        and _numeric_total(value.get("provider_requests")) == 0
        and _number(value.get("total_tokens")) == 0
    )


def _read_live_batch_journal(
    journal: Path,
    problems: Sequence[LiveColdProblem],
) -> list[dict[str, object]]:
    attempts: list[dict[str, object]] = []
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ApplicationColdGateError("live cold journal is unreadable") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ApplicationColdGateError(
                f"live cold journal line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(raw, Mapping):
            raise ApplicationColdGateError(
                f"live cold journal line {line_number} is not an object"
            )
        index = len(attempts) + 1
        if index > len(problems):
            raise ApplicationColdGateError("live cold journal has extra attempts")
        expected = problems[index - 1]
        if (
            raw.get("attempt_index") != index
            or raw.get("problem_id") != expected.problem_id
            or raw.get("platform") != expected.platform
        ):
            raise ApplicationColdGateError(
                f"live cold journal attempt {index} disagrees with the batch plan"
            )
        attempts.append(dict(raw))
    return attempts


def _numeric_usage(usage: Mapping[str, object]) -> dict[str, float]:
    return {
        str(key): float(value)
        for key, value in usage.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _token_usage(usage: Mapping[str, object]) -> dict[str, float]:
    return {
        key: value
        for key, value in _numeric_usage(usage).items()
        if "token" in key.casefold()
    }


def _total_tokens(usage: Mapping[str, object]) -> float:
    numeric = _numeric_usage(usage)
    if "total_tokens" in numeric:
        return numeric["total_tokens"]
    # Reasoning tokens are normally a subset of completion tokens.  Avoid
    # charging them twice when a provider omits total_tokens.
    prompt = numeric.get("prompt_tokens", numeric.get("input_tokens", 0.0))
    completion = numeric.get(
        "completion_tokens", numeric.get("output_tokens", 0.0)
    )
    if prompt or completion:
        return prompt + completion
    return sum(
        value
        for key, value in numeric.items()
        if key.casefold() in {"cached_tokens", "uncached_tokens"}
    )


def _wait_for_live_run(
    coordinator: Any,
    run_id: str,
    *,
    timeout_seconds: float,
    progress: LiveProgress | None,
    attempt_index: int,
    attempt_count: int,
) -> dict[str, object]:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    last_signature: tuple[object, ...] | None = None
    while True:
        row = dict(coordinator.run(run_id))
        signature = (
            row.get("status"),
            row.get("phase"),
            row.get("small_count"),
            row.get("large_count"),
            row.get("total_count"),
        )
        if progress is not None and signature != last_signature:
            progress(
                {
                    "event": "run_progress",
                    "attempt_index": attempt_index,
                    "attempt_count": attempt_count,
                    "run_id": run_id,
                    "status": row.get("status"),
                    "phase": row.get("phase"),
                    "small_cases": row.get("small_count"),
                    "large_cases": row.get("large_count"),
                    "total_cases": row.get("total_count"),
                }
            )
            last_signature = signature
        if str(row.get("status")) not in {
            "pending",
            "preparing",
            "running",
            "stop_requested",
        }:
            return row
        if time.monotonic() >= deadline:
            coordinator.stop(run_id)
            raise ApplicationColdGateError(
                f"controlled stress run exceeded {timeout_seconds:g} seconds"
            )
        time.sleep(0.1)


def _validate_live_problem(problem: LiveColdProblem) -> None:
    if problem.platform not in {"luogu", "codeforces"}:
        raise ValueError("live benchmark platform must be luogu or codeforces")
    if not problem.problem_id.strip() or not problem.statement.strip():
        raise ValueError("live benchmark problem id and statement are required")
    if "int main" not in problem.primary_source and "signed main" not in problem.primary_source:
        raise ValueError("live benchmark primary source must define main")
    gold = next((item for item in CORE8 if item.problem_id == problem.problem_id), None)
    if gold is None:
        raise ValueError(f"no independent gold oracle for {problem.problem_id}")
    for name, input_data, expected_output in problem.samples:
        actual = gold.oracle(bytes(input_data))
        if actual.split() != bytes(expected_output).split():
            raise ValueError(f"official sample {name!r} disagrees with gold oracle")


def _certify_live_validator_with_gold(
    problem: LiveColdProblem,
    *,
    bundle: Mapping[str, object],
    workspace: Path,
    sandbox_factory: Callable[[], object],
) -> dict[str, object]:
    """Verify the generated validator against hidden benchmark corpora.

    This runs after the production gates only in the isolated live benchmark.
    The model never receives these cases.  It closes the reporting gap where a
    bundle could be transactionally safe yet semantically accept an invalid
    input because its state-precondition check was deleted during repair.
    """

    from .stress import SandboxLimits

    gold = next((item for item in CORE8 if item.problem_id == problem.problem_id), None)
    if gold is None:
        raise ApplicationColdGateError(
            f"no hidden validator corpus for {problem.problem_id}"
        )
    artifacts = bundle.get("artifacts")
    validator_row = next(
        (
            item
            for item in artifacts
            if isinstance(item, Mapping) and item.get("kind") == "validator"
        ),
        None,
    ) if isinstance(artifacts, Sequence) else None
    source_path = (
        Path(str(validator_row.get("target_path"))).resolve()
        if isinstance(validator_row, Mapping) and validator_row.get("target_path")
        else None
    )
    if source_path is None or not source_path.is_file():
        raise ApplicationColdGateError("generated validator source is unavailable")
    compiler = shutil.which("g++")
    if compiler is None:
        raise ApplicationColdGateError("g++ is unavailable for gold validator gate")
    executable = workspace / ".acm" / "gold-validator-certification.exe"
    compiled = subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-static",
            str(source_path),
            "-o",
            str(executable),
        ],
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if compiled.returncode != 0 or not executable.is_file():
        raise ApplicationColdGateError(
            "generated validator failed hidden gold compilation: "
            + compiled.stderr.decode("utf-8", errors="replace")[:500]
        )
    sandbox = sandbox_factory()

    def observe(input_data: bytes) -> Mapping[str, object]:
        result = sandbox.run(
            [str(executable)],
            cwd=workspace,
            input_data=input_data,
            limits=SandboxLimits(
                timeout_seconds=2.0,
                stdout_bytes=256 * 1024,
                stderr_bytes=16 * 1024,
            ),
        )
        if not result.ok:
            raise ApplicationColdGateError(
                "generated validator failed hidden gold execution"
            )
        try:
            decoded = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ApplicationColdGateError(
                "generated validator returned malformed hidden-gate JSON"
            ) from exc
        if not isinstance(decoded, Mapping) or set(decoded) != {
            "valid",
            "dimensions",
            "coverage_tags",
            "records",
        }:
            raise ApplicationColdGateError(
                "generated validator returned wrong hidden-gate observation shape"
            )
        return decoded

    valid_corpus = tuple(gold.valid_cases) + core8_random_valid_corpus(
        problem.problem_id, case_count=8
    )
    for index, input_data in enumerate(valid_corpus, 1):
        if observe(input_data).get("valid") is not True:
            raise ApplicationColdGateError(
                f"generated validator rejected hidden valid corpus item {index}"
            )
    for index, input_data in enumerate(gold.invalid_cases, 1):
        observation = observe(input_data)
        if not (
            observation.get("valid") is False
            and observation.get("dimensions") == {}
            and observation.get("coverage_tags") == []
            and observation.get("records") == 0
        ):
            raise ApplicationColdGateError(
                f"generated validator accepted hidden invalid corpus item {index}"
            )
    return {
        "valid_accepted": len(valid_corpus),
        "invalid_rejected": len(gold.invalid_cases),
        "semantic_zero_misrelease": True,
    }


def _live_attempt(
    problem: LiveColdProblem,
    *,
    attempt_index: int,
    attempt_count: int,
    client_factory: LiveClientFactory,
    preparation_timeout_seconds: int,
    model_settings: Mapping[str, object],
    progress: LiveProgress | None,
    run_timeout_seconds: float,
    verify_warm_cache: bool,
    temp_parent: Path | None,
    evidence_directory: Path,
) -> dict[str, object]:
    """Run one paid setup in a fresh workspace and return complete evidence."""

    from .config import Paths
    from .storage import Database
    from .stress import WindowsAppContainerBackend
    from .stress_budget import PreparationBudget
    from .stress_runtime import StressCoordinator

    _validate_live_problem(problem)
    started_at = time.monotonic()
    raw_client = client_factory()
    client = CountingProviderClient(raw_client)
    retries = 0
    setup_result: Mapping[str, object] | None = None
    final_run: Mapping[str, object] | None = None
    live_run_id: str | None = None
    warm_cache: dict[str, object] | None = None
    error: dict[str, object] | None = None
    failure_stage = ""
    applied_bundle_count = 0
    budget = PreparationBudget(int(preparation_timeout_seconds))
    database_evidence: dict[str, object] = {}
    cleanup_worker_ids: tuple[str, ...] = ()
    failure_evidence_directory: str | None = None
    gold_validator_certification: dict[str, object] | None = None

    with tempfile.TemporaryDirectory(
        prefix=f"acm-ai-cold-{attempt_index:03d}-{problem.problem_id.lower()}-",
        dir=str(temp_parent) if temp_parent is not None else None,
        # A stuck native process must be reported as a cleanup failure without
        # replacing the paid attempt's journal/evidence with a Windows unlink
        # exception.  Successful attempts still remove the directory normally.
        ignore_cleanup_errors=True,
    ) as temporary:
        workspace = Path(temporary).resolve(strict=True) / "workspace"
        workspace.mkdir()
        paths = Paths.for_root(workspace)
        paths.ensure()
        stored_id = _stored_problem_id(problem.platform, problem.problem_id)
        with Database(paths.database) as database:
            database.upsert_problem(
                {
                    "platform": problem.platform,
                    "problem_id": stored_id,
                    "name": problem.title,
                }
            )
            database.replace_problem_samples(
                problem.platform,
                stored_id,
                [
                    {"name": name, "input": input_data, "output": output_data}
                    for name, input_data, output_data in problem.samples
                ],
                source="benchmark_official",
                metadata={"independent_gold_checked": True},
            )
        source = workspace / "benchmark" / f"{problem.problem_id}.cpp"
        source.parent.mkdir(parents=True)
        source.write_text(problem.primary_source, encoding="utf-8", newline="\n")
        coordinator = StressCoordinator(
            paths,
            sandbox_factory=lambda: WindowsAppContainerBackend(root=workspace),
        )

        def on_prepare(stage: str, label: str, step: int, total: int) -> None:
            nonlocal retries
            if "重试" in label:
                retries += 1
            if progress is not None:
                progress(
                    {
                        "event": "setup_progress",
                        "attempt_index": attempt_index,
                        "attempt_count": attempt_count,
                        "problem_id": problem.problem_id,
                        "stage": stage,
                        "label": label,
                        "step": step,
                        "total": total,
                        "elapsed_seconds": round(time.monotonic() - started_at, 3),
                    }
                )

        try:
            if progress is not None:
                progress(
                    {
                        "event": "attempt_started",
                        "attempt_index": attempt_index,
                        "attempt_count": attempt_count,
                        "problem_id": problem.problem_id,
                    }
                )
            setup_result = coordinator.start(
                client=client,
                platform=problem.platform,
                problem_id=problem.problem_id,
                title=problem.title,
                statement=problem.statement,
                primary_source=source,
                attempt_id=None,
                ai_run_id=None,
                model_settings=dict(model_settings),
                compare="token",
                seed=attempt_index * 1_000_003 + 17,
                include_generator=True,
                include_brute=True,
                include_reference=True,
                include_large=True,
                preparation_timeout_seconds=int(preparation_timeout_seconds),
                cache_mode="cold",
                generation_mode="hybrid",
                preparation_budget=budget,
                timeout=2.0,
                brute_timeout=5.0,
                run_max_cases=20,
                progress_callback=on_prepare,
            )
            setup_bundle = setup_result.get("bundle")
            if not isinstance(setup_bundle, Mapping):
                raise ApplicationColdGateError("setup did not return an applied bundle")
            try:
                gold_validator_certification = _certify_live_validator_with_gold(
                    problem,
                    bundle=setup_bundle,
                    workspace=workspace,
                    sandbox_factory=lambda: WindowsAppContainerBackend(root=workspace),
                )
            except ApplicationColdGateError:
                failure_stage = "gold_validator_certification"
                raise
            run = setup_result.get("run")
            if not isinstance(run, Mapping) or not run.get("id"):
                raise ApplicationColdGateError("setup did not return a stress run")
            live_run_id = str(run["id"])
            final_run = _wait_for_live_run(
                coordinator,
                live_run_id,
                timeout_seconds=run_timeout_seconds,
                progress=progress,
                attempt_index=attempt_index,
                attempt_count=attempt_count,
            )
            ok = (
                final_run.get("status") == "completed"
                and int(final_run.get("small_count") or 0) == 16
                and int(final_run.get("large_count") or 0) == 4
                and int(final_run.get("total_count") or 0) == 20
            )
            if not ok:
                failure_stage = f"run:{final_run.get('status') or 'unknown'}"

            if ok and verify_warm_cache:
                calls_before = sum(client.calls.values())
                warm_budget = PreparationBudget(int(preparation_timeout_seconds))
                warm_started = coordinator.start(
                    client=client,
                    platform=problem.platform,
                    problem_id=problem.problem_id,
                    title=problem.title,
                    statement=problem.statement,
                    primary_source=source,
                    attempt_id=None,
                    ai_run_id=None,
                    model_settings=dict(model_settings),
                    compare="token",
                    seed=attempt_index * 1_000_003 + 29,
                    include_generator=True,
                    include_brute=True,
                    include_reference=True,
                    include_large=True,
                    preparation_timeout_seconds=int(preparation_timeout_seconds),
                    cache_mode="reuse",
                    generation_mode="hybrid",
                    preparation_budget=warm_budget,
                    timeout=2.0,
                    brute_timeout=5.0,
                    run_max_cases=0,
                    progress_callback=None,
                )
                warm_run = warm_started.get("run")
                if not isinstance(warm_run, Mapping) or not warm_run.get("id"):
                    raise ApplicationColdGateError("warm cache check returned no run")
                warm_final = _wait_for_live_run(
                    coordinator,
                    str(warm_run["id"]),
                    timeout_seconds=run_timeout_seconds,
                    progress=None,
                    attempt_index=attempt_index,
                    attempt_count=attempt_count,
                )
                calls_after = sum(client.calls.values())
                warm_usage = warm_started.get("usage")
                warm_tokens = (
                    _total_tokens(warm_usage)
                    if isinstance(warm_usage, Mapping)
                    else 0.0
                )
                warm_cache = {
                    "ok": warm_final.get("status") == "completed",
                    "provider_requests": calls_after - calls_before,
                    "total_tokens": warm_tokens,
                    "cache_result": (
                        warm_started.get("preparation", {}).get("cache_result")
                        if isinstance(warm_started.get("preparation"), Mapping)
                        else None
                    ),
                }
                if (
                    not warm_cache["ok"]
                    or warm_cache["provider_requests"] != 0
                    or warm_cache["total_tokens"] != 0
                ):
                    ok = False
                    failure_stage = "warm_cache"
        except Exception as exc:
            ok = False
            if (
                isinstance(exc, ApplicationColdGateError)
                and str(exc).startswith("controlled stress run exceeded")
            ):
                failure_stage = "controlled_run_timeout"
            else:
                failure_stage = failure_stage or str(
                    budget.snapshot().get("last_stage") or "setup"
                )
            error = {
                "code": str(getattr(exc, "code", type(exc).__name__)),
                "message": str(exc)[:1000],
                "details": dict(getattr(exc, "details", {}) or {}),
            }
        finally:
            cleanup_worker_ids = coordinator.shutdown()
            # Preserve the terminal state produced by stop/shutdown even when
            # _wait_for_live_run raised at its watchdog boundary.
            if live_run_id is not None:
                try:
                    final_run = dict(coordinator.run(live_run_id))
                except (KeyError, RuntimeError):
                    pass
            if cleanup_worker_ids:
                ok = False
                failure_stage = "cleanup_timeout"
                if error is None:
                    error = {
                        "code": "cleanup_timeout",
                        "message": "stress worker did not stop before workspace cleanup",
                        "details": {},
                    }
                details = error.setdefault("details", {})
                if isinstance(details, dict):
                    details["run_ids"] = list(cleanup_worker_ids)
            with Database(paths.database) as database:
                applied_bundle_count = int(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM stress_artifact_bundles WHERE status='applied'"
                    ).fetchone()[0]
                )
                candidates: list[dict[str, object]] = []
                evidence_directory.mkdir(parents=True, exist_ok=False)
                for row in database.stress_artifact_candidates(limit=1000):
                    candidate = dict(row)
                    source_code = str(candidate.pop("source_code"))
                    role = str(candidate.get("role") or "artifact")
                    source_hash = str(candidate.get("source_hash") or "unknown")
                    source_name = f"{role}-{source_hash[:16]}.cpp"
                    source_path = evidence_directory / source_name
                    source_path.write_text(source_code, encoding="utf-8", newline="\n")
                    candidate["source_file"] = source_name
                    candidates.append(candidate)

                def table_rows(table: str) -> list[dict[str, object]]:
                    return [
                        dict(row)
                        for row in database.connection.execute(
                            f"SELECT * FROM {table} ORDER BY rowid"
                        ).fetchall()
                    ]

                database_evidence = {
                    "preparation_cache": table_rows("stress_preparation_cache"),
                    "candidates": candidates,
                    "proofs": table_rows("stress_artifact_proofs"),
                    "certifications": table_rows("stress_bundle_certifications"),
                    "aliases": table_rows("stress_cache_aliases"),
                    "bundles": table_rows("stress_artifact_bundles"),
                    "artifacts": table_rows("stress_artifacts"),
                    "runs": table_rows("stress_runs"),
                }
            raw_failure_path = (
                final_run.get("failure_path")
                if isinstance(final_run, Mapping)
                else None
            )
            if raw_failure_path:
                try:
                    failure_source = Path(str(raw_failure_path)).resolve(strict=True)
                    failures_root = paths.failures.resolve(strict=True)
                except OSError:
                    failure_source = None
                if (
                    failure_source is not None
                    and failure_source.is_dir()
                    and failure_source.is_relative_to(failures_root)
                ):
                    failure_target = evidence_directory / "failure-assets"
                    shutil.copytree(failure_source, failure_target)
                    failure_evidence_directory = failure_target.name

        snapshot = budget.snapshot()
        usage = snapshot.get("provider_usage")
        if not isinstance(usage, Mapping):
            usage = {}
        attempts = snapshot.get("attempts")
        repair_counts = (
            {
                str(role): int(count)
                for role, count in attempts.items()
                if isinstance(count, (int, float)) and int(count) > 0
            }
            if isinstance(attempts, Mapping)
            else {}
        )
        # PreparationBudget tracks provider telemetry, while semantic repair
        # counters live in the coordinator result.  Merge both so successful
        # attempts are not incorrectly reported as first-round successes.
        setup_usage = setup_result.get("usage") if isinstance(setup_result, Mapping) else None
        if isinstance(setup_usage, Mapping):
            for key, count in setup_usage.items():
                name = str(key)
                if name.endswith("_transport_repairs_used"):
                    try:
                        retries += max(0, int(count))
                    except (TypeError, ValueError):
                        pass
                    continue
                if not name.endswith("_repairs_used"):
                    continue
                try:
                    numeric = int(count)
                except (TypeError, ValueError):
                    continue
                if numeric <= 0:
                    continue
                role = name[: -len("_repairs_used")]
                if role == "blueprint":
                    role = "recipe"
                repair_counts[role] = max(repair_counts.get(role, 0), numeric)
        if error is not None:
            error_details = error.get("details", {})
            error_attempts = (
                error_details.get("attempts")
                if isinstance(error_details, Mapping)
                else None
            )
            if isinstance(error_attempts, Mapping):
                for role, count in error_attempts.items():
                    if isinstance(count, (int, float)) and int(count) > 0:
                        role_name = str(role)
                        repair_counts[role_name] = max(
                            repair_counts.get(role_name, 0), int(count)
                        )
        stage_seconds = snapshot.get("stage_timings")
        if not isinstance(stage_seconds, Mapping):
            stage_seconds = {}
        wall_seconds = time.monotonic() - started_at
        controlled_run_ok = bool(
            final_run
            and final_run.get("status") == "completed"
            and int(final_run.get("small_count") or 0) == 16
            and int(final_run.get("large_count") or 0) == 4
            and int(final_run.get("total_count") or 0) == 20
        )
        result = {
            "attempt_index": attempt_index,
            "problem_id": problem.problem_id,
            "platform": problem.platform,
            "ok": bool(ok),
            "first_round_success": bool(ok and not repair_counts),
            "repair_roles": repair_counts,
            "failure_stage": "" if ok else failure_stage or "unknown",
            "wall_seconds": wall_seconds,
            "stage_seconds": dict(stage_seconds),
            "tokens": _token_usage(usage),
            "total_tokens": _total_tokens(usage),
            "provider_requests": client.calls,
            "retries": retries,
            "unsafe_apply": bool(
                applied_bundle_count and not controlled_run_ok
                and failure_stage != "warm_cache"
            ),
            "applied_bundle_count": applied_bundle_count,
            "run": dict(final_run or {}),
            "warm_cache": warm_cache,
            "gold_validator_certification": gold_validator_certification,
            "error": error,
            "workspace_fresh": True,
            "database_fresh": True,
            "cache_mode": "cold",
            "evidence_directory": evidence_directory.name,
            "cleanup_worker_ids": list(cleanup_worker_ids),
            "failure_evidence_directory": failure_evidence_directory,
        }
        evidence_payload = {
            "attempt": result,
            "budget_snapshot": snapshot,
            "setup_result": dict(setup_result or {}),
            "database": database_evidence,
        }
        (evidence_directory / "evidence.json").write_text(
            json.dumps(
                evidence_payload, ensure_ascii=False, indent=2, sort_keys=True,
                default=str,
            ) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if progress is not None:
            progress(
                {
                    "event": "attempt_finished",
                    "attempt_index": attempt_index,
                    "attempt_count": attempt_count,
                    "problem_id": problem.problem_id,
                    "ok": bool(ok),
                    "wall_seconds": round(wall_seconds, 3),
                    "total_tokens": result["total_tokens"],
                    "failure_stage": result["failure_stage"],
                }
            )
        return result


def run_live_ai_cold_batch(
    problems: Sequence[LiveColdProblem],
    *,
    client_factory: LiveClientFactory,
    report_directory: Path,
    protected_workspace: Path,
    preparation_timeout_seconds: int = 600,
    model_settings: Mapping[str, object] | None = None,
    progress: LiveProgress | None = None,
    run_timeout_seconds: float = 180.0,
    verify_warm_cache: bool = True,
    temp_parent: Path | None = None,
    resume: bool = False,
    protected_paths: Sequence[Path] | None = None,
) -> list[dict[str, object]]:
    """Execute resumable-evidence paid cold attempts without touching real state.

    A distinct temporary workspace and empty SQLite database are used for every
    element of ``problems``.  The raw journal is flushed after each attempt so
    a process interruption cannot erase earlier failures or paid evidence.
    """

    if not problems:
        raise ValueError("at least one live cold problem is required")
    if not 60 <= int(preparation_timeout_seconds) <= 1800:
        raise ValueError("preparation timeout must be between 60 and 1800 seconds")
    protected = protected_workspace.resolve(strict=True)
    parent = temp_parent.resolve(strict=True) if temp_parent is not None else None
    selected_paths = _live_protected_paths(protected, problems, protected_paths)
    baseline = _selected_workspace_fingerprint(protected, selected_paths)
    journal = report_directory / "attempts.raw.jsonl"
    settings = dict(
        model_settings
        or {
            "model": "deepseek-v4-flash",
            "thinking": False,
            "reasoning_effort": "high",
        }
    )
    plan_payload = {
        "schema_version": 1,
        "problems": [_live_problem_identity(problem) for problem in problems],
        "preparation_timeout_seconds": int(preparation_timeout_seconds),
        "run_timeout_seconds": float(run_timeout_seconds),
        "verify_warm_cache": bool(verify_warm_cache),
        "model_settings": settings,
    }
    plan_text = json.dumps(
        plan_payload, ensure_ascii=False, indent=2, sort_keys=True, default=str
    ) + "\n"
    plan_path = report_directory / "batch-plan.json"
    if resume:
        if not report_directory.is_dir() or not journal.is_file() or not plan_path.is_file():
            raise ApplicationColdGateError(
                "resume requires an existing batch-plan.json and attempts.raw.jsonl"
            )
        try:
            stored_plan = plan_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ApplicationColdGateError("live cold batch plan is unreadable") from exc
        if stored_plan != plan_text:
            raise ApplicationColdGateError(
                "live cold batch identity changed; refusing to resume"
            )
        attempts = _read_live_batch_journal(journal, problems)
    else:
        report_directory.mkdir(parents=True, exist_ok=False)
        plan_path.write_text(plan_text, encoding="utf-8", newline="\n")
        journal.touch(exist_ok=False)
        attempts = []

    warm_verified: set[tuple[str, str]] = {
        (str(item.get("platform")), str(item.get("problem_id")))
        for item in attempts
        if _warm_cache_is_zero_provider(item.get("warm_cache"))
    }
    completed_count = len(attempts)
    for index, problem in enumerate(problems[completed_count:], completed_count + 1):
        problem_key = (problem.platform, problem.problem_id)
        result = _live_attempt(
            problem,
            attempt_index=index,
            attempt_count=len(problems),
            client_factory=client_factory,
            preparation_timeout_seconds=int(preparation_timeout_seconds),
            model_settings=settings,
            progress=progress,
            run_timeout_seconds=run_timeout_seconds,
            verify_warm_cache=bool(
                verify_warm_cache and problem_key not in warm_verified
            ),
            temp_parent=parent,
            evidence_directory=report_directory / f"attempt-{index:03d}-evidence",
        )
        warm = result.get("warm_cache")
        if _warm_cache_is_zero_provider(warm):
            warm_verified.add(problem_key)
        attempts.append(result)
        with journal.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        if _selected_workspace_fingerprint(protected, selected_paths) != baseline:
            raise ApplicationColdGateError(
                "protected workspace changed during live cold benchmark"
            )
    if resume:
        # These four files are derived views of the append-only raw journal.
        # Rebuild them after a resumed or already-complete batch while preserving
        # every paid attempt and evidence directory.
        for name in ("attempts.jsonl", "attempts.csv", "summary.json", "report.md"):
            (report_directory / name).unlink(missing_ok=True)
    write_benchmark_report(report_directory, attempts)
    return attempts


_LOCAL_SUM_SOURCE = r'''#include <iostream>
int main(){long long a,b;if(!(std::cin>>a>>b)) return 1;std::cout<<a+b<<'\n';}
'''

_LOCAL_SUM_GENERATOR = r'''#include <cstdint>
#include <iostream>
#include <random>
#include <string>
int main(int argc,char** argv){
    if(argc!=4) return 2;
    unsigned long long seed=std::stoull(argv[1]);
    std::string profile=argv[2],kind=argv[3];
    long long a=0,b=0;
    if(kind=="lower_bound"){a=b=-1000000000LL;}
    else if(kind=="upper_bound"){a=b=1000000000LL;}
    else{
        std::mt19937_64 rng(seed ^ (profile=="large"?0x9e3779b97f4a7c15ULL:0ULL));
        a=static_cast<long long>(rng()%2000000001ULL)-1000000000LL;
        b=static_cast<long long>(rng()%2000000001ULL)-1000000000LL;
    }
    std::cout<<a<<' '<<b<<'\n';
}
'''

_LOCAL_SUM_VALIDATOR = r'''#include <iostream>
#include <string>
int main(){
    long long a,b; if(!(std::cin>>a>>b)){std::cout<<"{\"valid\":false,\"dimensions\":{},\"coverage_tags\":[],\"records\":0}";return 0;}
    std::string extra; bool valid=!(std::cin>>extra)&&a>=-1000000000LL&&a<=1000000000LL&&b>=-1000000000LL&&b<=1000000000LL;
    if(!valid){std::cout<<"{\"valid\":false,\"dimensions\":{},\"coverage_tags\":[],\"records\":0}";return 0;}
    std::cout<<"{\"valid\":true,\"dimensions\":{\"records\":2},\"coverage_tags\":[";
    if(a==-1000000000LL&&b==-1000000000LL) std::cout<<"\"lower\"";
    else if(a==1000000000LL&&b==1000000000LL) std::cout<<"\"upper\"";
    else std::cout<<"\"lower\",\"seeded\",\"upper\",\"large_random\"";
    std::cout<<"],\"records\":2}";
}
'''

_LOCAL_SUM_BLUEPRINT: dict[str, object] = {
    "required_coverage_tags": ["lower", "seeded"],
    "large_required_coverage_tags": ["upper", "large_random"],
    "cases": [
        {"profile": "small", "case_kind": "lower_bound", "dimensions": {"records": 2}, "total_complexity": "O(output_size)"},
        {"profile": "small", "case_kind": "random", "dimensions": {"records": 2}, "total_complexity": "O(output_size)"},
        {"profile": "large", "case_kind": "upper_bound", "dimensions": {"records": 2}, "total_complexity": "O(output_size)"},
        {"profile": "large", "case_kind": "random", "dimensions": {"records": 2}, "total_complexity": "O(output_size)"},
    ],
}


def run_local_application_cold_batch(
    attempt_count: int,
    *,
    protected_workspace: Path,
    temp_parent: Path | None = None,
    report_directory: Path | None = None,
) -> list[dict[str, object]]:
    """Exercise the real provider-free apply/preflight/run path repeatedly."""

    from .config import Paths
    from .storage import Database
    from .stress import (
        HelperBundleManager,
        HelperPreflightConfig,
        HelperSources,
        LayeredStressRunner,
        SampleCase,
        StressExecutables,
        StressRunConfig,
        WindowsAppContainerBackend,
    )

    def attempt(context: ApplicationColdContext) -> Mapping[str, object]:
        started = time.monotonic()
        paths = Paths.for_root(context.workspace)
        paths.ensure()
        with Database(context.database_path) as database:
            database.upsert_problem({"platform": "luogu", "problem_id": "P1001"})
        primary = context.workspace / "cold" / "P1001.cpp"
        primary.parent.mkdir(parents=True)
        primary.write_text(_LOCAL_SUM_SOURCE, encoding="utf-8")
        sandbox_factory = lambda: WindowsAppContainerBackend(root=context.workspace)
        manager = HelperBundleManager(
            context.workspace,
            sandbox_factory(),
            sandbox_factory=sandbox_factory,
        )
        staged = manager.stage(
            primary,
            HelperSources(
                _LOCAL_SUM_GENERATOR,
                _LOCAL_SUM_SOURCE,
                _LOCAL_SUM_SOURCE,
                _LOCAL_SUM_VALIDATOR,
            ),
        )
        preflight = manager.preflight(
            staged,
            HelperPreflightConfig(
                contract_hash="provider-free-p1001-v1",
                samples=[SampleCase("official", b"1 2\n", b"3\n")],
                generator_blueprint=_LOCAL_SUM_BLUEPRINT,
                include_large=True,
                small_random_cases=16,
            ),
        )
        bundle = manager.apply(staged)
        compiler = shutil.which("g++")
        if compiler is None:
            raise ApplicationColdGateError("g++ is unavailable")
        solution = Path(staged.staging_dir) / "solution.release.exe"
        compiled = subprocess.run(
            [compiler, "-std=c++17", "-O2", "-static", str(primary), "-o", str(solution)],
            cwd=staged.staging_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        if compiled.returncode != 0:
            raise ApplicationColdGateError("application-cold solution compile failed")
        runner = LayeredStressRunner(
            context.workspace,
            "P1001",
            StressExecutables(
                solution,
                Path(bundle.release_executables["generator"]),
                Path(bundle.release_executables["brute"]),
                Path(bundle.release_executables["reference"]),
                Path(bundle.release_executables["validator"]),
            ),
            sandbox_factory(),
        )
        result = runner.run(
            StressRunConfig(
                first_seed=context.attempt_index * 1000,
                include_large=True,
                max_cases=20,
                warmup_small_cases=0,
            ),
            samples=[SampleCase("official", b"1 2\n", b"3\n")],
        )
        ok = (
            result.status == "limit_reached"
            and result.small_cases == 16
            and result.large_cases == 4
            and result.total_cases == 20
        )
        return {
            "problem_id": "P1001",
            "ok": ok,
            "first_round_success": ok,
            "wall_seconds": time.monotonic() - started,
            "total_tokens": 0,
            "provider_requests": 0,
            "retries": 0,
            "stage_seconds": {},
            "tokens": {},
            "preflight_cases": len(preflight.get("cases", [])),
            "small_cases": result.small_cases,
            "large_cases": result.large_cases,
            "failure_stage": "" if ok else result.status,
            "unsafe_apply": False,
        }

    results = run_application_cold(
        attempt_count,
        attempt,
        protected_workspace=protected_workspace,
        temp_parent=temp_parent,
        protected_paths=(
            Path(".acm/state.db"),
            Path(".acm/config.json"),
            Path("2026/8/5/P2596.cpp"),
            Path("2026/8/5/P2596.gen.cpp"),
            Path("2026/8/5/P2596.bf.cpp"),
            Path("2026/8/5/P2596.ref.cpp"),
        ),
    )
    if report_directory is not None:
        write_benchmark_report(report_directory, results)
    return results


__all__ = [
    "ApplicationColdContext",
    "ApplicationColdGateError",
    "CORE8",
    "CountingProviderClient",
    "GoldProblem",
    "GoldValidationError",
    "LIVE_COLD_FIXTURE_SCHEMA_VERSION",
    "LiveColdFixtureError",
    "LiveColdProblem",
    "build_core8_live_cold_attempt_plan",
    "core8_random_valid_corpus",
    "evaluate_reliability_release",
    "load_core8_live_cold_fixtures",
    "load_live_cold_problem_fixture",
    "run_application_cold",
    "run_live_ai_cold_batch",
    "run_local_application_cold_batch",
    "run_core8_gold_gate",
    "summarize_attempts",
    "write_benchmark_report",
]
