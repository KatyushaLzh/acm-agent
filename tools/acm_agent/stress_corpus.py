"""Descriptor-keyed corpus with structure-aware mutation.

The legacy generator is *generative*: every seed builds an input from scratch
through a schedule frozen at compile time, so the realized inputs are a fixed
set of isolated points rather than a space.  This module makes exploration
*evolutionary* instead — keep the inputs that reached a new behaviour cell, then
mutate them.

Two properties matter:

* **Mutation is structure-aware, not byte-level.**  The contract tells us the
  input is "an ``n`` plus ``n`` integers", so operators edit records and counts
  and re-serialize.  Byte-level flips would mostly produce inputs the validator
  rejects.
* **Mutation stays inside the declared domain.**  Every write clamps to the
  field's contract bounds; when a bound is only a dependency expression the
  operator falls back to the parent's observed range, which is valid by
  induction.  A parent that passed the validator therefore yields children that
  almost always pass too.

Novelty is judged by :func:`tools.acm_agent.stress_profiler.descriptor`, which
reads only the input bytes.  Nothing here consults a generator-declared tag.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
import hashlib
import random
from typing import Any, Callable, Mapping

from .stress_profiler import (
    ParsedInput,
    ProfileError,
    Profiler,
    SectionData,
    descriptor,
    primary_section,
)

#: Fresh finds get this multiplier while they sit inside the recency window.
FRESH_BONUS = 2.0
#: How many observations an entry counts as "recently discovered" for energy.
RECENT_WINDOW = 32
#: Mutation attempts per :meth:`Corpus.propose` before giving up.
MAX_PROPOSE_ATTEMPTS = 24


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class CorpusEntry:
    """One elite: the smallest input seen for its behaviour cell."""

    data: bytes
    cell: tuple[Any, ...]
    features: Mapping[str, Any]
    origin: str
    discovered_at: int
    parent: str | None = None
    selections: int = 0

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class ProposeStats:
    """Why proposals failed, so a stalled search is diagnosable."""

    attempts: int = 0
    unparsable: int = 0
    no_operator: int = 0
    over_budget: int = 0
    rejected: int = 0
    duplicate: int = 0
    produced: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "attempts": self.attempts,
            "unparsable": self.unparsable,
            "no_operator": self.no_operator,
            "over_budget": self.over_budget,
            "rejected": self.rejected,
            "duplicate": self.duplicate,
            "produced": self.produced,
        }


@dataclass
class _Grid:
    """Mutable numeric view of a section's rows, plus its clamping domain."""

    section: SectionData
    values: list[list[int]] = dataclass_field(default_factory=list)
    bounds: list[tuple[int, int]] = dataclass_field(default_factory=list)
    interval: bool = False

    @property
    def rows(self) -> int:
        return len(self.values)

    @property
    def width(self) -> int:
        return len(self.values[0]) if self.values else 0

    def clamp(self, column: int, value: int) -> int:
        low, high = self.bounds[column]
        return max(low, min(high, value))

    def write(self) -> None:
        """Push the numeric grid back into the section's token rows.

        Interval rows get their ``l <= r`` invariant repaired here rather than in
        each operator: a generic numeric operator has no reason to know that two
        columns are correlated, and repairing centrally keeps the validator from
        rejecting otherwise useful children.
        """

        if self.interval and self.width == 2:
            for row in self.values:
                if row[0] > row[1]:
                    row[0], row[1] = row[1], row[0]
        self.section.rows = [[str(value) for value in row] for row in self.values]


def _build_grid(section: SectionData, scalars: Mapping[str, int]) -> _Grid | None:
    """Numeric view of ``section``, or ``None`` when it is not all integers.

    ``scalars`` are this input's header values.  A bound written as a dependency
    expression (``"maximum": "header.n"``) resolves against them, so an edge
    endpoint is clamped to *this* graph's vertex count rather than to the
    problem-wide maximum.  Without that, mutations on a 4-vertex graph would
    happily emit vertex 10000 and the validator would reject every child.
    """

    spec = section.spec
    values: list[list[int]] = []
    for row in section.rows:
        try:
            values.append([int(token) for token in row])
        except ValueError:
            return None
    if not values:
        return None
    width = len(values[0])
    if any(len(row) != width for row in values):
        return None
    bounds: list[tuple[int, int]] = []
    for column in range(width):
        # A matrix declares one cell field for every column, so clamp the index.
        field = spec.fields[min(column, len(spec.fields) - 1)]
        if not field.numeric:
            return None
        observed = [row[column] for row in values]
        low = field.minimum
        high = field.maximum
        if field.minimum_ref is not None and field.minimum_ref in scalars:
            low = scalars[field.minimum_ref]
        if field.maximum_ref is not None and field.maximum_ref in scalars:
            high = scalars[field.maximum_ref]
        # An unresolved bound falls back to the parent's own range.  The parent
        # passed the validator, so staying inside it keeps children valid too.
        if low is None:
            low = min(observed)
        if high is None:
            high = max(observed)
        bounds.append((low, high) if low <= high else (high, low))
    return _Grid(section=section, values=values, bounds=bounds, interval=spec.is_interval)


def _set_scalar(parsed: ParsedInput, path: str, value: int) -> bool:
    """Write ``value`` back to a scalar field, both parsed and on the wire."""

    if "." not in path:
        return False
    section_id, field_name = path.split(".", 1)
    data = parsed.section(section_id)
    if data is None:
        return False
    for index, field in enumerate(data.spec.fields):
        if field.name != field_name:
            continue
        if len(data.rows) == 1 and index < len(data.rows[0]):
            data.rows[0][index] = str(value)
        elif index < len(data.rows) and data.rows[index]:
            data.rows[index][0] = str(value)
        else:
            return False
        parsed.scalars[path] = value
        return True
    return False


def row_domain(
    section: SectionData, scalars: Mapping[str, int]
) -> tuple[tuple[int, int], ...] | None:
    """Per-column legal ranges for ``section``'s rows, or ``None``.

    The public half of :func:`_build_grid`: exhaustive enumeration needs the
    clamping domain without needing a mutable grid, and reaching into the
    private helper from another module would freeze it as an accidental API.
    """

    grid = _build_grid(section, scalars)
    return tuple(grid.bounds) if grid is not None else None


def set_scalar(parsed: ParsedInput, path: str, value: int) -> bool:
    """Write ``value`` to a scalar field, in the parse tree and on the wire."""

    return _set_scalar(parsed, path, value)


def _slice(rng: random.Random, rows: int) -> tuple[int, int]:
    """A random non-empty row range ``[start, stop)``."""

    if rows <= 0:
        return 0, 0
    length = rng.randint(1, rows)
    start = rng.randrange(rows - length + 1)
    return start, start + length


def _palette(rng: random.Random, low: int, high: int, size: int) -> list[int]:
    """Up to ``size`` values from ``[low, high]``, distinct where possible."""

    span = high - low + 1
    size = max(1, min(size, span))
    if span <= 4096:
        return rng.sample(range(low, high + 1), size)
    return [rng.randint(low, high) for _ in range(size)]


def op_set_constant(rng: random.Random, grid: _Grid) -> bool:
    """Flatten a slice to one value — drives max_multiplicity up."""

    column = rng.randrange(grid.width)
    low, high = grid.bounds[column]
    value = rng.randint(low, high)
    start, stop = _slice(rng, grid.rows)
    for index in range(start, stop):
        grid.values[index][column] = value
    return True


def op_set_boundary(rng: random.Random, grid: _Grid) -> bool:
    """Pin a slice to a declared domain bound — drives boundary_ratio."""

    column = rng.randrange(grid.width)
    low, high = grid.bounds[column]
    value = low if rng.random() < 0.5 else high
    start, stop = _slice(rng, grid.rows)
    for index in range(start, stop):
        grid.values[index][column] = value
    return True


def op_nudge(rng: random.Random, grid: _Grid) -> bool:
    """Perturb a few cells by a small delta — fine-grained local search."""

    column = rng.randrange(grid.width)
    for _ in range(rng.randint(1, 3)):
        row = rng.randrange(grid.rows)
        delta = rng.choice((-2, -1, 1, 2))
        grid.values[row][column] = grid.clamp(column, grid.values[row][column] + delta)
    return True


def op_randomize(rng: random.Random, grid: _Grid) -> bool:
    """Refill a slice from a bounded palette — drives distinct_ratio up."""

    column = rng.randrange(grid.width)
    low, high = grid.bounds[column]
    start, stop = _slice(rng, grid.rows)
    palette = _palette(rng, low, high, rng.randint(1, max(1, stop - start)))
    for index in range(start, stop):
        grid.values[index][column] = rng.choice(palette)
    return True


def op_collapse_values(rng: random.Random, grid: _Grid) -> bool:
    """Map a whole column onto one or two values — drives distinct_ratio down."""

    column = rng.randrange(grid.width)
    low, high = grid.bounds[column]
    palette = _palette(rng, low, high, rng.randint(1, 2))
    for row in grid.values:
        row[column] = rng.choice(palette)
    return True


def op_sort_slice(rng: random.Random, grid: _Grid) -> bool:
    """Sort a slice ascending or descending — moves sorted_ratio to an extreme."""

    column = rng.randrange(grid.width)
    start, stop = _slice(rng, grid.rows)
    chunk = sorted(
        (grid.values[index][column] for index in range(start, stop)),
        reverse=rng.random() < 0.5,
    )
    for offset, value in enumerate(chunk):
        grid.values[start + offset][column] = value
    return True


def op_duplicate_slice(rng: random.Random, grid: _Grid) -> bool:
    """Copy a slice elsewhere — manufactures runs, periods, and repeats."""

    if grid.rows < 2:
        return False
    start, stop = _slice(rng, grid.rows)
    length = stop - start
    destination = rng.randrange(grid.rows)
    # Snapshot first: source and destination may overlap.
    source = [list(grid.values[index]) for index in range(start, stop)]
    for offset in range(length):
        target = destination + offset
        if target >= grid.rows:
            break
        grid.values[target] = list(source[offset])
    return True


def op_shuffle_rows(rng: random.Random, grid: _Grid) -> bool:
    """Permute rows — breaks monotonicity without touching the value multiset."""

    if grid.rows < 2:
        return False
    rng.shuffle(grid.values)
    return True


def op_star_rewire(rng: random.Random, grid: _Grid) -> bool:
    """Point many edges at one hub — drives max_degree_ratio and degree_gini."""

    if grid.width < 2:
        return False
    low, high = grid.bounds[0]
    hub = rng.randint(low, high)
    start, stop = _slice(rng, grid.rows)
    for index in range(start, stop):
        grid.values[index][0] = hub
    return True


def op_path_rewire(rng: random.Random, grid: _Grid) -> bool:
    """Rewire edges into a chain — the connected/tree corner of the space."""

    if grid.width < 2:
        return False
    low, high = grid.bounds[0]
    span = high - low + 1
    for index, row in enumerate(grid.values):
        row[0] = low + (index % span)
        row[1] = grid.clamp(1, low + ((index + 1) % span))
    return True


def op_contract_vertices(rng: random.Random, grid: _Grid) -> bool:
    """Fold endpoints into a narrower vertex range.

    Raises density and manufactures parallel edges and self-loops without
    changing the edge count, which is the cheapest way to reach the dense
    corner of the descriptor grid.
    """

    if grid.width < 2:
        return False
    low, high = grid.bounds[0]
    span = high - low + 1
    if span < 2:
        return False
    ceiling = rng.randint(1, max(1, span // 2))
    for row in grid.values:
        for column in (0, 1):
            row[column] = grid.clamp(column, low + (row[column] - low) % ceiling)
    return True


def op_self_loop(rng: random.Random, grid: _Grid) -> bool:
    """Force an edge's endpoints equal."""

    if grid.width < 2:
        return False
    row = grid.values[rng.randrange(grid.rows)]
    row[1] = grid.clamp(1, row[0])
    return True


def op_duplicate_edge(rng: random.Random, grid: _Grid) -> bool:
    """Copy one edge over another — manufactures parallel edges."""

    if grid.rows < 2 or grid.width < 2:
        return False
    source, target = rng.sample(range(grid.rows), 2)
    grid.values[target][0] = grid.values[source][0]
    grid.values[target][1] = grid.values[source][1]
    return True


def op_nest_intervals(rng: random.Random, grid: _Grid) -> bool:
    """Make one interval strictly contain another — drives nested_ratio."""

    if grid.rows < 2 or grid.width != 2:
        return False
    outer, inner = rng.sample(range(grid.rows), 2)
    low, high = grid.values[outer]
    if high - low < 2:
        low = grid.bounds[0][0]
        high = grid.bounds[1][1]
        grid.values[outer] = [low, high]
    if high - low < 2:
        return False
    left = rng.randint(low, high - 1)
    grid.values[inner] = [left, rng.randint(left, high)]
    return True


def op_make_points(rng: random.Random, grid: _Grid) -> bool:
    """Degenerate a slice to zero-length intervals — drives point_ratio."""

    if grid.width != 2:
        return False
    start, stop = _slice(rng, grid.rows)
    for index in range(start, stop):
        grid.values[index][1] = grid.clamp(1, grid.values[index][0])
    return True


def op_spread_intervals(rng: random.Random, grid: _Grid) -> bool:
    """Tile intervals disjointly across the domain — drives overlap_ratio down."""

    if grid.width != 2 or grid.rows < 1:
        return False
    low, high = grid.bounds[0][0], grid.bounds[1][1]
    span = max(1, (high - low + 1) // grid.rows)
    for index, row in enumerate(grid.values):
        left = grid.clamp(0, low + index * span)
        row[0] = left
        row[1] = grid.clamp(1, left + max(0, span - 1))
    return True


# --- string operators -------------------------------------------------------
# A string section is a single token, so these edit characters directly instead
# of going through the numeric grid.  The alphabet comes from the parent, which
# keeps children inside whatever character set the validator accepts.


def _string_ops(
    rng: random.Random, text: str, bounds: tuple[int, int] | None
) -> tuple[str, str] | None:
    """Edit ``text``, returning ``(new_text, operator_name)``.

    ``bounds`` is the legal length range; the two resizing branches need it
    because a string's length *is* its count axis.
    """

    if not text:
        return None
    alphabet = sorted(set(text))
    length = len(text)
    low, high = bounds if bounds is not None else (length, length)
    choice = rng.randrange(7)
    if choice == 0:  # shrink the alphabet
        keep = rng.sample(alphabet, max(1, min(len(alphabet), rng.randint(1, 2))))
        return "".join(rng.choice(keep) for _ in text), "shrink_alphabet"
    if choice == 1:  # make it periodic
        period = rng.randint(1, max(1, min(length, 4)))
        motif = "".join(rng.choice(alphabet) for _ in range(period))
        return (motif * (length // period + 1))[:length], "make_periodic"
    if choice == 2:  # long runs
        pieces: list[str] = []
        while len("".join(pieces)) < length:
            pieces.append(rng.choice(alphabet) * rng.randint(1, max(1, length // 2)))
        return "".join(pieces)[:length], "make_runs"
    if choice == 3:  # sort, forward or reverse
        return "".join(sorted(text, reverse=rng.random() < 0.5)), "sort_string"
    if choice == 4:  # truncate
        if length <= max(1, low):
            return None
        return text[: rng.randint(max(1, low), length - 1)], "truncate_string"
    if choice == 5:  # extend, reusing the parent's alphabet so it stays valid
        if high <= length:
            return None
        target = rng.randint(length + 1, high)
        return text + "".join(rng.choice(alphabet) for _ in range(target - length)), "extend_string"
    position = rng.randrange(length)  # single-character substitution
    return text[:position] + rng.choice(alphabet) + text[position + 1 :], "substitute_char"


#: Operators that apply to any all-integer section.
_NUMERIC_OPS: tuple[Callable[[random.Random, _Grid], bool], ...] = (
    op_set_constant,
    op_set_boundary,
    op_nudge,
    op_randomize,
    op_collapse_values,
    op_sort_slice,
    op_duplicate_slice,
    op_shuffle_rows,
)

_GRAPH_OPS: tuple[Callable[[random.Random, _Grid], bool], ...] = (
    op_star_rewire,
    op_path_rewire,
    op_contract_vertices,
    op_self_loop,
    op_duplicate_edge,
)

_INTERVAL_OPS: tuple[Callable[[random.Random, _Grid], bool], ...] = (
    op_nest_intervals,
    op_make_points,
    op_spread_intervals,
)


def _operators_for(section: SectionData) -> tuple[Callable[[random.Random, _Grid], bool], ...]:
    if section.spec.is_edge_list:
        return _NUMERIC_OPS + _GRAPH_OPS
    if section.spec.is_interval:
        return _NUMERIC_OPS + _INTERVAL_OPS
    return _NUMERIC_OPS


class Corpus:
    """An archive of elites keyed by behaviour cell, with a mutation proposer.

    ``observe`` decides admission; ``propose`` returns the next input to run.
    The two together form the loop that replaces "seed += 1": the corpus is the
    memory that the old open-loop generator never had.
    """

    def __init__(
        self,
        profiler: Profiler,
        *,
        max_bytes: int | None = None,
        seed: int = 0,
        validator: Callable[[bytes], bool] | None = None,
    ) -> None:
        self.profiler = profiler
        self.max_bytes = max_bytes
        self.rng = random.Random(seed)
        self.validator = validator
        self._entries: dict[tuple[Any, ...], CorpusEntry] = {}
        self._digests: set[str] = set()
        self._observations = 0
        self.stats = ProposeStats()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[CorpusEntry, ...]:
        return tuple(self._entries.values())

    def observe(
        self,
        data: bytes,
        *,
        origin: str = "seed",
        parent: str | None = None,
        signal: Any = None,
    ) -> str:
        """Score ``data`` and admit it if it reached a new cell.

        ``signal`` extends the behaviour key with anything observed outside the
        input itself — a brute-force output class, later a coverage signature.
        Passing it is how the cheap "all 200 outputs were 0" proxy becomes a
        real diversity axis without touching the profiler.

        Returns ``"new"``, ``"improved"``, or ``"duplicate"``.
        """

        self._observations += 1
        self._digests.add(_digest(data))
        features = self.profiler.features(data)
        cell = descriptor(features)
        if signal is not None:
            cell = cell + (signal,)
        existing = self._entries.get(cell)
        if existing is not None and len(data) >= existing.size:
            return "duplicate"
        self._entries[cell] = CorpusEntry(
            data=data,
            cell=cell,
            features=features,
            origin=origin,
            discovered_at=self._observations,
            parent=parent,
        )
        return "improved" if existing is not None else "new"

    def _energy(self, entry: CorpusEntry) -> float:
        """Selection weight: prefer under-explored, fresh, and small parents.

        Small parents matter for two reasons — they leave room under the byte
        budget for growth operators, and a failure found on a small input is
        already close to minimal.
        """

        freshness = (
            FRESH_BONUS
            if entry.discovered_at > self._observations - RECENT_WINDOW
            else 1.0
        )
        size_bias = 1.0
        if self.max_bytes:
            size_bias += 1.0 - min(1.0, entry.size / self.max_bytes)
        return freshness * size_bias / (1.0 + entry.selections)

    def _pick_parent(self) -> CorpusEntry | None:
        entries = [entry for entry in self._entries.values() if entry.features.get("parsed")]
        if not entries:
            return None
        weights = [self._energy(entry) for entry in entries]
        return self.rng.choices(entries, weights=weights, k=1)[0]

    def propose(self) -> tuple[bytes, str, str] | None:
        """Mutate an elite into a fresh input.

        Returns ``(data, origin, parent_digest)`` or ``None`` when no attempt
        produced something new, valid, and inside the byte budget.  Feed the
        result straight back into :meth:`observe` after running it.
        """

        for _ in range(MAX_PROPOSE_ATTEMPTS):
            self.stats.attempts += 1
            parent = self._pick_parent()
            if parent is None:
                self.stats.unparsable += 1
                return None
            parent.selections += 1
            try:
                parsed = self.profiler.parse(parent.data)
            except ProfileError:
                self.stats.unparsable += 1
                continue
            origin = self._mutate(parsed)
            if origin is None:
                self.stats.no_operator += 1
                continue
            candidate = parsed.render()
            if not candidate or candidate == parent.data:
                self.stats.duplicate += 1
                continue
            if self.max_bytes is not None and len(candidate) > self.max_bytes:
                self.stats.over_budget += 1
                continue
            if _digest(candidate) in self._digests:
                self.stats.duplicate += 1
                continue
            if self.validator is not None and not self.validator(candidate):
                self.stats.rejected += 1
                continue
            self.stats.produced += 1
            return candidate, origin, _digest(parent.data)
        return None

    def _mutate(self, parsed: ParsedInput) -> str | None:
        """Apply one operator in place; returns its name, or ``None``."""

        section = primary_section(parsed)
        if section is None:
            return None
        if section.spec.kind == "string":
            edited = _string_ops(self.rng, section.rows[0][0], self._count_range(section))
            if edited is None:
                return None
            text, name = edited
            section.rows[0][0] = text
            return self._resize_count(parsed, section, len(text), name)
        grid = _build_grid(section, parsed.scalars)
        if grid is None:
            return None
        # A quarter of proposals resize the payload; the rest edit values in
        # place.  Resizing is the only way to move the count axis, but doing it
        # too often starves the value-shape axes.
        roll = self.rng.random()
        if roll < 0.25:
            name = self._resize_rows(parsed, section, grid)
            if name is not None:
                return name
        elif roll < 0.35:
            name = self._resize_domain(parsed, section, grid)
            if name is None:
                name = self._resize_cols(parsed, section, grid)
            if name is not None:
                return name
        operators = _operators_for(section)
        operator = operators[self.rng.randrange(len(operators))]
        if not operator(self.rng, grid):
            return None
        grid.write()
        return operator.__name__.removeprefix("op_")

    def _count_range(self, section: SectionData) -> tuple[int, int] | None:
        """Legal row-count range for ``section``, from its count constraint."""

        target = section.spec.count_from
        if not isinstance(target, str):
            return None
        return self.profiler.ranges.get(target)

    def _resize_count(
        self, parsed: ParsedInput, section: SectionData, count: int, name: str
    ) -> str | None:
        """Sync the scalar that declares ``section``'s size, if there is one."""

        target = section.spec.count_from
        if isinstance(target, str) and target in parsed.scalars:
            bounds = self._count_range(section)
            if bounds is not None and not bounds[0] <= count <= bounds[1]:
                return None
            if not _set_scalar(parsed, target, count):
                return None
        return name

    def _resize_rows(
        self, parsed: ParsedInput, section: SectionData, grid: _Grid
    ) -> str | None:
        """Grow or shrink the payload, keeping the declared count in step.

        Growth clones existing rows rather than inventing them, so new rows are
        automatically inside the valid domain.
        """

        bounds = self._count_range(section)
        low, high = bounds if bounds is not None else (grid.rows, grid.rows)
        if low >= high:
            return None
        grow = self.rng.random() < 0.5
        if grow:
            headroom = high - grid.rows
            if headroom <= 0:
                return None
            target = grid.rows + self.rng.randint(1, min(headroom, max(1, grid.rows)))
            while grid.rows < target:
                grid.values.append(list(grid.values[self.rng.randrange(grid.rows)]))
            name = "grow_rows"
        else:
            if grid.rows <= low:
                return None
            target = self.rng.randint(low, grid.rows - 1)
            del grid.values[target:]
            name = "shrink_rows"
        grid.write()
        return self._resize_count(parsed, section, grid.rows, name)

    def _resize_domain(
        self, parsed: ParsedInput, section: SectionData, grid: _Grid
    ) -> str | None:
        """Move the scalar that bounds the payload's values, e.g. a vertex count.

        Density is the ratio between the payload size and its domain, so with
        only :meth:`_resize_rows` half of that plane is unreachable: a sparse
        graph can never become dense without either adding edges or shrinking
        the vertex set.  This is the second half.
        """

        # Find a scalar that some column's bound depends on.
        targets = {
            field.maximum_ref
            for field in section.spec.fields
            if field.maximum_ref is not None and field.maximum_ref in parsed.scalars
        }
        if not targets:
            return None
        target = sorted(targets)[self.rng.randrange(len(targets))]
        bounds = self.profiler.ranges.get(target)
        if bounds is None or bounds[0] >= bounds[1]:
            return None
        current = parsed.scalars[target]
        # Bias toward small domains: that is where density, collisions, and
        # parallel edges live, and small inputs are cheaper to run and read.
        candidate = self.rng.choice(
            (
                bounds[0],
                max(bounds[0], min(bounds[1], current // 2)),
                max(bounds[0], min(bounds[1], current * 2)),
                self.rng.randint(bounds[0], bounds[1]),
            )
        )
        if candidate == current:
            return None
        if not _set_scalar(parsed, target, candidate):
            return None
        # Every column bounded by this scalar must be folded back into range.
        for column, field in enumerate(section.spec.fields[: grid.width]):
            if field.maximum_ref != target:
                continue
            low = grid.bounds[column][0]
            span = candidate - low + 1
            if span <= 0:
                return None
            for row in grid.values:
                row[column] = low + (row[column] - low) % span
        grid.write()
        return "resize_domain"

    def _resize_cols(
        self, parsed: ParsedInput, section: SectionData, grid: _Grid
    ) -> str | None:
        """Change a matrix's width, resizing every row to match.

        Without this the row count is the only reachable size axis and every
        descendant of the seed keeps its width forever.  New cells are cloned
        from the same row, so they are inside the domain by construction.
        """

        target = self.profiler.cols_target(section.spec, parsed)
        if target is None:
            return None
        bounds = self.profiler.ranges.get(target)
        if bounds is None:
            return None
        low, high = max(1, bounds[0]), bounds[1]
        if low >= high:
            return None
        candidate = self.rng.randint(low, high)
        if candidate == grid.width:
            return None
        for row in grid.values:
            if candidate < len(row):
                del row[candidate:]
            else:
                while len(row) < candidate:
                    row.append(row[self.rng.randrange(len(row))])
        if not _set_scalar(parsed, target, candidate):
            return None
        grid.write()
        return "resize_cols"

    def report(self) -> dict[str, Any]:
        """Diversity summary: cell count is the number the old scheme lacked."""

        entries = self.entries
        origins: dict[str, int] = {}
        for entry in entries:
            origins[entry.origin] = origins.get(entry.origin, 0) + 1
        return {
            "cells": len(entries),
            "observations": self._observations,
            "total_bytes": sum(entry.size for entry in entries),
            "smallest": min((entry.size for entry in entries), default=0),
            "largest": max((entry.size for entry in entries), default=0),
            "origins": dict(sorted(origins.items())),
            "propose": self.stats.as_dict(),
        }


__all__ = [
    "Corpus",
    "CorpusEntry",
    "MAX_PROPOSE_ATTEMPTS",
    "ProposeStats",
    "row_domain",
    "set_scalar",
]
