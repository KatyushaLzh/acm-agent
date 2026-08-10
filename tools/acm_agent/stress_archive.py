"""MAP-Elites archive: diversity as a number, and empty cells as targets.

Layer 2 already keeps one elite per behaviour cell.  What it cannot do is answer
"how much of the space is left" or "which specific input shape have we never
produced" — and those two questions are the whole point.  Without them the
search has no stopping rule and the LLM has nothing concrete to aim at.

Two deliberate choices about honesty of measurement:

* **No joint fill rate.**  The product of axis cardinalities counts cells that
  are logically impossible (all-distinct *and* one value dominating), so a
  percentage against it would understate coverage by an unknown factor and
  invite exactly the kind of number-chasing that produced the old tag scheme.
  The headline is **marginal coverage per axis**, which is well defined.
* **Reachability comes from the contract.**  A log axis is bounded by the
  declared range of the scalar behind it (``n``'s range bounds the count axis),
  so "missing values" are values the problem actually permits.

The behavioural signal (:func:`output_class`, :class:`EdgeCoverage`) is what
distinguishes inputs whose *bytes* differ in no interesting way but whose effect
on the program does.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from .stress_corpus import Corpus, CorpusEntry
from .stress_profiler import (
    Axis,
    Profiler,
    _bucket,
    SectionSpec,
    describe_cell,
    grid_axes,
)

ARCHIVE_SCHEMA_VERSION = 1

#: Observations without a new cell before the search is called stagnant.
DEFAULT_STAGNATION_LIMIT = 200
#: Frontier entries returned by default; a prompt cannot use more than a handful.
DEFAULT_TARGET_LIMIT = 12
#: Genuine failed attempts before a cell is treated as unreachable.
MISSES_BEFORE_INFEASIBLE = 2


def output_class(stdout: bytes | str, *, limit: int = 24) -> str:
    """Classify a reference/brute output into a coarse behavioural bucket.

    This is the cheapest useful behavioural signal and the one that immediately
    exposes the failure the tag scheme hid: if two hundred inputs all drive the
    brute force to print ``0``, the archive collapses to a single cell no matter
    how varied the inputs look.

    Kept coarse on purpose — the exact answer value would make every input
    novel, which is the same degenerate outcome as measuring nothing.
    """

    text = stdout.decode("ascii", "replace") if isinstance(stdout, bytes) else stdout
    stripped = text.strip()
    if not stripped:
        return "empty"
    lines = stripped.splitlines()
    if len(lines) > 1:
        return f"lines:{_log(len(lines))}"
    token = lines[0].strip()
    folded = token.casefold()
    if folded in {"yes", "no", "true", "false", "impossible", "possible"}:
        return f"word:{folded}"
    try:
        value = int(token)
    except ValueError:
        return f"text:{_log(len(token))}" if len(token) <= limit else "text:long"
    if value < 0:
        return "int:negative"
    if value == 0:
        return "int:zero"
    return f"int:{_log(value)}"


def _log(value: int) -> int:
    return max(0, int(value)).bit_length()


def _payload_spec(profiler: Profiler) -> SectionSpec | None:
    """The repeated section, chosen statically rather than per input."""

    for spec in profiler.specs:
        if spec.kind != "scalar":
            return spec
    return None


def axis_domains(
    profiler: Profiler, *, max_bytes: int | None = None
) -> dict[str, tuple[int, ...]]:
    """Reachable values per axis name, using the contract to bound log axes.

    Bucket, flag, and clamp axes always know their own domain.  A log axis only
    becomes finite once something bounds the underlying quantity — the count
    axis from ``n``'s declared range, the matrix width from the column scalar's.
    Axes left unbounded are simply absent, and coverage reporting skips them
    rather than inventing a denominator.
    """

    domains: dict[str, tuple[int, ...]] = {}
    payload = _payload_spec(profiler)
    for shape_axes in {id(axes): axes for axes in _shape_axes(profiler)}.values():
        for axis in shape_axes:
            if axis.name in domains:
                continue
            if axis.cardinality:
                domains[axis.name] = tuple(range(axis.cardinality))
                continue
            bound = _log_axis_bound(profiler, payload, axis)
            if bound is None:
                continue
            low, high = bound
            if axis.feature == "count":
                # A parsed shape is only assigned when the payload has at least
                # one record, so count bucket 0 is unreachable for every shape
                # that has a count axis at all.
                low = max(1, low)
            if max_bytes is not None and axis.feature == "count" and payload is not None:
                # The byte budget bounds reachability every bit as hard as the
                # contract does: 100 bytes cannot hold 128 edges whatever the
                # constraint permits.  Calling those buckets "missing" would be
                # the same false precision the tag scheme was guilty of.  Two
                # bytes per column is the floor (one digit plus a separator), so
                # this is a sound upper bound on the record count.
                per_record = max(2, 2 * payload.arity)
                high = min(high, max(low, max_bytes // per_record))
            domains[axis.name] = tuple(range(_log(low), _log(high) + 1))
    return domains


def _shape_axes(profiler: Profiler) -> list[tuple[Axis, ...]]:
    payload = _payload_spec(profiler)
    if payload is None:
        return [grid_axes("array")]
    if payload.is_edge_list:
        return [grid_axes("graph")]
    if payload.is_interval:
        return [grid_axes("interval")]
    if payload.kind == "string":
        return [grid_axes("string")]
    if payload.kind == "matrix":
        return [grid_axes("matrix")]
    return [grid_axes("array")]


def _log_axis_bound(
    profiler: Profiler, payload: SectionSpec | None, axis: Axis
) -> tuple[int, int] | None:
    if payload is None:
        return None
    if axis.feature == "count":
        target = payload.count_from
        if isinstance(target, str):
            return profiler.ranges.get(target)
        if type(target) is int:
            return (target, target)
        return None
    if axis.feature == "cols":
        for target, bounds in profiler.ranges.items():
            if target.rsplit(".", 1)[-1].casefold() in {"cols", "m", "k", "c"}:
                return bounds
    return None


def _cell_reachable(cell: tuple[Any, ...]) -> bool:
    """Are all of a cell's axis values jointly consistent with its own count?

    Checking only the axis being changed is not enough: lowering ``count`` to 1
    leaves the parent's ``max_mult=0%-33%`` in place, and no one-element array
    has a most-frequent share below 100%.  Validate the whole cell.
    """

    if len(cell) < 2:
        return True
    axes = grid_axes(str(cell[0]))
    count_bucket = int(cell[1])
    return all(
        _bucket_reachable(axis, int(value), count_bucket)
        for axis, value in zip(axes, cell[1:])
    )


def _bucket_reachable(axis: Axis, value: int, count_bucket: int) -> bool:
    """Can a payload in ``count_bucket`` produce ``value`` on this ratio axis?

    Every ratio here is ``k / c`` for integer ``k`` and payload size ``c``, so
    the axis is quantized at ``1/c``.  A one-element array therefore *cannot*
    have "most frequent value share 0%-33%" — the only possible value is 100%.
    Offering such a cell as a target wastes a provider call and looks broken, so
    filter it out arithmetically instead of waiting to be told.
    """

    if axis.kind != "bucket" or count_bucket <= 0:
        return True
    low = 1 << (count_bucket - 1)
    high = (1 << count_bucket) - 1
    for count in range(low, min(high, low + 8) + 1):
        for denominator in (count, max(1, count - 1)):
            for numerator in range(axis.min_numerator, denominator + 1):
                if _bucket(numerator / denominator, axis.size) == value:
                    return True
    return False


@dataclass(frozen=True)
class AxisCoverage:
    """Marginal coverage of one axis: the headline diversity number."""

    name: str
    description: str
    reached: tuple[Any, ...]
    missing: tuple[Any, ...]
    labels: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.reached) + len(self.missing)

    @property
    def ratio(self) -> float:
        return round(len(self.reached) / self.total, 4) if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "axis": self.name,
            "description": self.description,
            "reached": len(self.reached),
            "total": self.total,
            "ratio": self.ratio,
            "missing_labels": list(self.labels),
        }


@dataclass(frozen=True)
class Target:
    """An empty cell worth aiming at, with the evidence that it is reachable."""

    cell: tuple[Any, ...]
    description: str
    #: Filled cell this target is one axis-step away from.
    neighbour: tuple[Any, ...]
    neighbour_description: str
    #: Axis whose value differs from ``neighbour``.
    axis: str
    #: Bytes of the neighbour's elite — the concrete thing to perturb.
    example: bytes
    priority: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.description,
            "axis_to_change": self.axis,
            "nearest_reached_cell": self.neighbour_description,
            "nearest_reached_input": self.example.decode("ascii", "replace"),
            "priority": self.priority,
        }


class Archive:
    """A :class:`Corpus` plus grid accounting, behavioural signal, and targets.

    Wraps rather than subclasses the corpus so layer 2 stays usable on its own
    and has no idea layer 3 exists.
    """

    def __init__(
        self,
        corpus: Corpus,
        *,
        stagnation_limit: int = DEFAULT_STAGNATION_LIMIT,
    ) -> None:
        self.corpus = corpus
        self.profiler = corpus.profiler
        self.domains = axis_domains(corpus.profiler, max_bytes=corpus.max_bytes)
        self.stagnation_limit = stagnation_limit
        self.observations = 0
        self.last_new_cell = 0
        self.signals: set[Any] = set()
        # Cells that were aimed at and not reached.  Neighbour-adjacency makes a
        # target plausible, not possible: a one-element array cannot be
        # partially sorted, and no amount of asking will change that.  Without
        # this memo the same impossible cell heads the list forever.
        self.infeasible: set[tuple[Any, ...]] = set()
        self.misses: dict[tuple[Any, ...], int] = {}

    def __len__(self) -> int:
        return len(self.corpus)

    @property
    def entries(self) -> tuple[CorpusEntry, ...]:
        return self.corpus.entries

    @property
    def stagnant(self) -> bool:
        """True when the search has stopped finding new cells."""

        return self.observations - self.last_new_cell >= self.stagnation_limit

    def observe(self, data: bytes, *, signal: Any = None, **kwargs: Any) -> str:
        self.observations += 1
        if signal is not None:
            self.signals.add(signal)
        verdict = self.corpus.observe(data, signal=signal, **kwargs)
        if verdict == "new":
            self.last_new_cell = self.observations
        return verdict

    def propose(self) -> tuple[bytes, str, str] | None:
        return self.corpus.propose()

    def cells(self) -> tuple[tuple[Any, ...], ...]:
        return tuple(entry.cell for entry in self.entries)

    def note_miss(self, cell: tuple[Any, ...]) -> bool:
        """Record a genuine failed attempt at ``cell``; retire it after enough.

        One miss is weak evidence — a model can fail a reachable cell for
        reasons that have nothing to do with the cell.  Requiring
        :data:`MISSES_BEFORE_INFEASIBLE` attempts keeps a merely-hard target in
        the pool while still ending the loop on a genuinely impossible one.
        Returns True when this miss retired the cell.
        """

        key = tuple(cell)
        self.misses[key] = self.misses.get(key, 0) + 1
        if self.misses[key] >= MISSES_BEFORE_INFEASIBLE:
            self.infeasible.add(key)
            return True
        return False

    def mark_infeasible(self, cells: Iterable[tuple[Any, ...]]) -> int:
        """Retire cells that were targeted and missed. Returns how many are new."""

        before = len(self.infeasible)
        for cell in cells:
            self.infeasible.add(tuple(cell))
        return len(self.infeasible) - before

    def axis_coverage(self) -> tuple[AxisCoverage, ...]:
        """Per-axis marginal coverage over every shape present in the archive."""

        result: list[AxisCoverage] = []
        seen: set[str] = set()
        for shape in sorted({str(cell[0]) for cell in self.cells()}):
            axes = grid_axes(shape)
            for position, axis in enumerate(axes, start=1):
                if axis.name in seen or axis.name not in self.domains:
                    continue
                seen.add(axis.name)
                domain = self.domains[axis.name]
                reached = {
                    cell[position]
                    for cell in self.cells()
                    if str(cell[0]) == shape and len(cell) > position
                }
                # Flag axes carry bools; the domain is 0/1.  Compare by int.
                reached_ints = {int(value) for value in reached}
                missing = tuple(v for v in domain if int(v) not in reached_ints)
                result.append(
                    AxisCoverage(
                        name=axis.name,
                        description=axis.description,
                        reached=tuple(sorted(reached_ints)),
                        missing=missing,
                        labels=tuple(axis.label(value) for value in missing),
                    )
                )
        return tuple(result)

    def frontier(self, *, limit: int = DEFAULT_TARGET_LIMIT) -> tuple[Target, ...]:
        """Empty cells one axis-step from a filled one, best first.

        Restricting to neighbours matters: an arbitrary empty cell is usually
        empty because it is infeasible, while a neighbour of something already
        produced is usually reachable by changing one property of a concrete
        input we are holding.  That difference is what makes the target list
        worth handing to a model.
        """

        filled = {entry.cell: entry for entry in self.entries}
        # An axis value never seen at all is worth far more than a fresh
        # combination of values already seen: it is a hole in a dimension we can
        # prove the contract permits, so it is both reachable and informative.
        wanted = {
            coverage.name: {int(value) for value in coverage.missing}
            for coverage in self.axis_coverage()
        }
        candidates: dict[tuple[Any, ...], Target] = {}
        for cell, entry in filled.items():
            axes = grid_axes(str(cell[0]))
            for position, axis in enumerate(axes, start=1):
                if position >= len(cell) or axis.name not in self.domains:
                    continue
                current = int(cell[position])
                for value in self.domains[axis.name]:
                    if int(value) == current:
                        continue
                    neighbour = cell[:position] + (value,) + cell[position + 1 :]
                    if neighbour in filled or neighbour in candidates:
                        continue
                    if neighbour in self.infeasible:
                        continue
                    # Arithmetically impossible combinations are not targets.
                    if not _cell_reachable(neighbour):
                        continue
                    novel = int(value) in wanted.get(axis.name, set())
                    candidates[neighbour] = Target(
                        cell=neighbour,
                        description=describe_cell(neighbour),
                        neighbour=cell,
                        neighbour_description=describe_cell(cell),
                        axis=axis.name,
                        example=entry.data,
                        # Unseen axis value first, then the smallest elite to
                        # perturb: small inputs are easier to reason about.
                        priority=(0 if novel else 1) * 10_000 + entry.size,
                    )
        ordered = sorted(candidates.values(), key=lambda t: (t.priority, t.description))
        return tuple(ordered[:limit])

    def report(self, *, target_limit: int = DEFAULT_TARGET_LIMIT) -> dict[str, Any]:
        """The diversity dashboard: what is covered, what is not, what to try."""

        coverage = self.axis_coverage()
        ratios = [axis.ratio for axis in coverage]
        corpus_report = self.corpus.report()
        starved = [axis.name for axis in coverage if axis.missing]
        if starved:
            action = "target_missing_axis_values"
        elif not self.stagnant:
            action = "keep_mutating"
        else:
            # Every axis saturated and no new cells: remaining empty cells are
            # mostly axis combinations that cannot co-occur.  Say so rather than
            # implying there is coverage left to chase.
            action = "diminishing_returns"
        return {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "cells": len(self.corpus),
            "observations": self.observations,
            # Mean marginal coverage, deliberately *not* a joint fill rate.
            "axis_coverage_mean": round(sum(ratios) / len(ratios), 4) if ratios else 0.0,
            "axes": [axis.as_dict() for axis in coverage],
            "starved_axes": starved,
            "next_action": action,
            "infeasible_cells": len(self.infeasible),
            "shapes": sorted({str(cell[0]) for cell in self.cells()}),
            "behaviour_signals": len(self.signals),
            "stagnant": self.stagnant,
            "observations_since_new_cell": self.observations - self.last_new_cell,
            "smallest": corpus_report["smallest"],
            "largest": corpus_report["largest"],
            "origins": corpus_report["origins"],
            "propose": corpus_report["propose"],
            "targets": [target.as_dict() for target in self.frontier(limit=target_limit)],
        }

    def save(self, path: str | Path) -> None:
        """Persist the archive atomically: mkstemp, fsync, replace."""

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "observations": self.observations,
            "last_new_cell": self.last_new_cell,
            "signals": sorted(str(signal) for signal in self.signals),
            "entries": [
                {
                    "cell": list(entry.cell),
                    "description": describe_cell(entry.cell),
                    "input": entry.data.decode("ascii", "replace"),
                    "origin": entry.origin,
                    "discovered_at": entry.discovered_at,
                    "features": dict(entry.features),
                }
                for entry in sorted(self.entries, key=lambda e: (e.discovered_at, e.size))
            ],
        }
        handle, temporary = tempfile.mkstemp(
            dir=str(destination.parent), prefix=destination.name, suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def load(self, path: str | Path) -> int:
        """Re-observe a saved archive's inputs. Returns how many were admitted.

        Inputs are replayed through :meth:`observe` rather than trusted, so a
        stale file recorded under different axes or a different contract cannot
        inject cells that the current profiler would not assign.
        """

        source = Path(path)
        if not source.is_file():
            return 0
        payload = json.loads(source.read_text(encoding="utf-8"))
        admitted = 0
        for record in payload.get("entries", []):
            if not isinstance(record, Mapping):
                continue
            raw = record.get("input")
            if not isinstance(raw, str):
                continue
            verdict = self.observe(
                raw.encode("ascii", "replace"),
                origin=str(record.get("origin") or "restored"),
            )
            if verdict != "duplicate":
                admitted += 1
        return admitted


@dataclass
class EdgeCoverage:
    """Line-coverage signal for the brute force, via ``--coverage`` + ``gcov``.

    The strongest signal available: reaching a line no earlier input reached is
    the fuzzing field's evidence that an input is genuinely new, and it is the
    only signal that can tell you *which* branch of the brute has never run —
    which is exactly what layer 4 needs in order to ask a useful question.

    Two constraints shape the design:

    * The brute is frequently LLM-authored, so it must execute through the
      injected sandbox.  ``runner`` is that seam; this class never spawns a
      process itself and there is no unsandboxed fallback.
    * ``gcov`` may be absent, the toolchain may not emit ``.gcda`` under the
      sandbox's file restrictions, and POSIX has no sandbox at all.  Every such
      case sets :attr:`unavailable` and the archive simply loses this axis.
    """

    #: ``(argv, cwd, stdin) -> (exit_code, stdout, stderr)``, sandbox-backed.
    runner: Callable[[Sequence[str], Path, bytes | None], tuple[int, bytes, bytes]]
    workdir: Path
    #: Instrumented executable, already built with ``--coverage``.
    executable: str
    #: Source basename gcov reports on, e.g. ``brute.cpp``.
    source_name: str
    gcov: str = "gcov"
    unavailable: str | None = None

    def signature(self, data: bytes) -> str | None:
        """Coverage signature for ``data``, or ``None`` when unavailable.

        The signature is the set of executed line numbers, hashed to a short
        stable string.  Line identity beats hit *counts*: counts change with
        input size and would make every input look novel.
        """

        if self.unavailable is not None:
            return None
        try:
            self._reset()
            code, _, _ = self.runner([self.executable], self.workdir, data)
            if code != 0:
                # A crashing brute is itself a behaviour worth separating.
                return f"exit:{code}"
            lines = self._collect()
        except OSError as error:
            self.unavailable = f"coverage_runner_failed:{error}"
            return None
        if lines is None:
            return None
        digest = 0
        for line in sorted(lines):
            digest = (digest * 1000003 ^ line) & 0xFFFFFFFFFFFFFFFF
        return f"cov:{len(lines)}:{digest:016x}"

    def _reset(self) -> None:
        for stale in self.workdir.glob("*.gcda"):
            stale.unlink(missing_ok=True)

    def _collect(self) -> set[int] | None:
        """Run gcov and parse executed line numbers from its annotated output."""

        try:
            code, _, _ = self.runner(
                [self.gcov, "-b", "-i", self.source_name], self.workdir, None
            )
        except OSError as error:
            self.unavailable = f"gcov_unavailable:{error}"
            return None
        if code != 0:
            self.unavailable = f"gcov_exit:{code}"
            return None
        executed: set[int] = set()
        found = False
        for report in self.workdir.glob("*.gcov.json*"):
            found = True
            executed |= _lines_from_gcov_json(report)
        for report in self.workdir.glob("*.gcov"):
            found = True
            executed |= _lines_from_gcov_text(report)
        if not found:
            self.unavailable = "gcov_no_output"
            return None
        return executed


def _lines_from_gcov_json(path: Path) -> set[int]:
    """Executed lines from ``gcov -i`` JSON (gzipped or plain)."""

    try:
        raw = path.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            import gzip

            raw = gzip.decompress(raw)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return set()
    executed: set[int] = set()
    for source in payload.get("files", []) or []:
        if not isinstance(source, Mapping):
            continue
        for line in source.get("lines", []) or []:
            if isinstance(line, Mapping) and line.get("count"):
                number = line.get("line_number")
                if type(number) is int:
                    executed.add(number)
    return executed


def _lines_from_gcov_text(path: Path) -> set[int]:
    """Executed lines from a textual ``.gcov`` file.

    Each line is ``count:lineno:source``; ``-`` means non-executable and
    ``#####`` means executable but never run.
    """

    executed: set[int] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return executed
    for row in text.splitlines():
        parts = row.split(":", 2)
        if len(parts) < 3:
            continue
        count, number = parts[0].strip(), parts[1].strip()
        if not count or count.startswith("-") or count.startswith("#"):
            continue
        try:
            executed.add(int(number))
        except ValueError:
            continue
    return executed


def combined_signal(*parts: Any) -> tuple[Any, ...] | None:
    """Bundle several behavioural signals into one cell key component."""

    kept = tuple(part for part in parts if part is not None)
    return kept or None


__all__ = [
    "ARCHIVE_SCHEMA_VERSION",
    "Archive",
    "AxisCoverage",
    "DEFAULT_STAGNATION_LIMIT",
    "DEFAULT_TARGET_LIMIT",
    "EdgeCoverage",
    "MISSES_BEFORE_INFEASIBLE",
    "Target",
    "axis_domains",
    "combined_signal",
    "output_class",
]
