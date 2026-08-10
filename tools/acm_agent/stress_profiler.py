"""Contract-guided input profiling.

The stress pipeline used to measure diversity through ``coverage_tags`` that the
generator emitted about itself.  That signal is self-referential: a generator can
emit sixteen near-identical arrays and still report sixteen distinct tags, so the
balance checks in :mod:`tools.acm_agent.stress` pass while the realized inputs
stay monotone.

This module derives features *only* from the input bytes, using the contract
syntax as a parsing guide.  Nothing here reads a recipe, a manifest, or a
generator-declared tag, so the resulting vector cannot be gamed by the component
under measurement.  Parsing is deliberately conservative: an input that does not
match its contract degrades to generic byte features rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Mapping, Sequence

FEATURE_SCHEMA_VERSION = 1

_NUMERIC_TYPES = frozenset({"int", "float"})
_INTERVAL_NAME_PAIRS = (("l", "r"), ("left", "right"), ("start", "end"))
_LIST_KINDS = frozenset({"list", "interval", "intervals", "edge_list"})
_ALL_KINDS = frozenset({"scalar", "string", "matrix"}) | _LIST_KINDS


class ProfileError(ValueError):
    """Raised when an input cannot be parsed under its contract."""


def contract_ranges(contract: Mapping[str, Any] | None) -> dict[str, tuple[int, int]]:
    """Collect ``range`` constraints as ``{"section.field": (min, max)}``.

    Mirrors the recipe validator's view of the contract without importing its
    private helper, so the profiler stays usable on its own.
    """

    result: dict[str, tuple[int, int]] = {}
    if not isinstance(contract, Mapping):
        return result
    constraints = contract.get("constraints", [])
    if not isinstance(constraints, Sequence) or isinstance(constraints, (str, bytes)):
        return result
    for constraint in constraints:
        if not isinstance(constraint, Mapping) or constraint.get("kind") != "range":
            continue
        target, args = constraint.get("target"), constraint.get("args")
        if not isinstance(target, str) or not isinstance(args, Mapping):
            continue
        minimum, maximum = args.get("minimum"), args.get("maximum")
        if type(minimum) is int and type(maximum) is int and minimum <= maximum:
            result[target] = (minimum, maximum)
    return result


@dataclass(frozen=True)
class FieldSpec:
    """One column of a section, with whatever numeric domain we could resolve."""

    name: str
    type: str
    minimum: int | None = None
    maximum: int | None = None
    #: Original dependency expression, when the bound was a field reference
    #: (``"maximum": "header.n"``).  ``minimum``/``maximum`` hold the *global*
    #: constraint range for that target, which is a safe but very loose bound —
    #: a mutator holding a parsed input should prefer the instance value.
    minimum_ref: str | None = None
    maximum_ref: str | None = None

    @property
    def numeric(self) -> bool:
        return self.type in _NUMERIC_TYPES


@dataclass(frozen=True)
class SectionSpec:
    """A contract section reduced to what the parser and mutators need."""

    id: str
    kind: str
    fields: tuple[FieldSpec, ...]
    count_from: str | int | None = None

    @property
    def arity(self) -> int:
        return len(self.fields)

    @property
    def is_interval(self) -> bool:
        if self.kind in {"interval", "intervals"}:
            return True
        if self.kind != "list" or self.arity != 2:
            return False
        names = tuple(spec.name.casefold() for spec in self.fields)
        return names in _INTERVAL_NAME_PAIRS

    @property
    def is_edge_list(self) -> bool:
        return self.kind == "edge_list"


@dataclass
class SectionData:
    """Parsed rows for one section plus the wire layout we must reproduce."""

    spec: SectionSpec
    rows: list[list[str]] = dataclass_field(default_factory=list)
    inline: bool = False

    def numeric_column(self, index: int) -> list[int] | None:
        if index >= self.spec.arity or not self.spec.fields[index].numeric:
            return None
        try:
            return [int(row[index]) for row in self.rows]
        except (ValueError, IndexError):
            return None


@dataclass
class ParsedInput:
    """Structural view of one input, sufficient to re-serialize it verbatim."""

    sections: list[SectionData] = dataclass_field(default_factory=list)
    scalars: dict[str, int] = dataclass_field(default_factory=dict)
    trailing_newline: bool = True

    def section(self, section_id: str) -> SectionData | None:
        for data in self.sections:
            if data.spec.id == section_id:
                return data
        return None

    def render(self) -> bytes:
        """Rebuild the wire form, preserving each section's original layout."""

        lines: list[str] = []
        for data in self.sections:
            if not data.rows:
                continue
            if data.inline:
                lines.append(" ".join(token for row in data.rows for token in row))
            else:
                lines.extend(" ".join(row) for row in data.rows)
        text = "\n".join(lines)
        if self.trailing_newline and text:
            text += "\n"
        return text.encode("ascii")


class _TokenCursor:
    """Whitespace tokenizer that remembers which line each token came from."""

    def __init__(self, text: str) -> None:
        self._tokens: list[str] = []
        self._lines: list[int] = []
        for line_index, line in enumerate(text.splitlines()):
            for token in line.split():
                self._tokens.append(token)
                self._lines.append(line_index)
        self._position = 0

    @property
    def remaining(self) -> int:
        return len(self._tokens) - self._position

    @property
    def total(self) -> int:
        return len(self._tokens)

    def take(self, count: int) -> tuple[list[str], bool]:
        """Consume ``count`` tokens, reporting whether they shared one line."""

        if count < 0 or count > self.remaining:
            raise ProfileError("input ended before the contract did")
        start = self._position
        self._position += count
        taken = self._tokens[start : self._position]
        spanned = set(self._lines[start : self._position])
        return taken, len(spanned) <= 1


def _resolve_bound(value: Any, ranges: Mapping[str, tuple[int, int]], slot: int) -> int | None:
    """Resolve a field bound; dependency expressions fall back to constraints."""

    if type(value) is int:
        return value
    if isinstance(value, str) and value in ranges:
        return ranges[value][slot]
    return None


def build_specs(contract: Mapping[str, Any] | None) -> tuple[SectionSpec, ...]:
    """Reduce a normalized contract to parseable section specs.

    Raises :class:`ProfileError` for shapes the profiler cannot parse
    unambiguously — tagged operation streams, raw blobs, and multi-case modes.
    Callers treat that as "fall back to generic byte features".
    """

    if not isinstance(contract, Mapping):
        raise ProfileError("contract is not an object")
    syntax = contract.get("syntax")
    if not isinstance(syntax, Mapping):
        raise ProfileError("contract has no structured syntax")
    if syntax.get("mode") != "single_case":
        raise ProfileError(f"unsupported contract mode: {syntax.get('mode')!r}")
    raw_sections = syntax.get("sections")
    if (
        not isinstance(raw_sections, Sequence)
        or isinstance(raw_sections, (str, bytes))
        or not raw_sections
    ):
        raise ProfileError("contract has no sections")

    ranges = contract_ranges(contract)
    specs: list[SectionSpec] = []
    for raw_section in raw_sections:
        if not isinstance(raw_section, Mapping):
            raise ProfileError("contract section is not an object")
        kind = str(raw_section.get("kind") or "")
        if kind not in _ALL_KINDS:
            raise ProfileError(f"unsupported section kind: {kind or 'missing'}")
        if raw_section.get("variants"):
            raise ProfileError("tagged variants are not parseable")
        section_id = str(raw_section.get("id") or "")
        raw_fields = raw_section.get("fields")
        if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
            raise ProfileError("contract section has no fields")
        fields: list[FieldSpec] = []
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping):
                raise ProfileError("contract field is not an object")
            raw_minimum = raw_field.get("minimum")
            raw_maximum = raw_field.get("maximum")
            fields.append(
                FieldSpec(
                    name=str(raw_field.get("name") or ""),
                    type=str(raw_field.get("type") or ""),
                    minimum=_resolve_bound(raw_minimum, ranges, 0),
                    maximum=_resolve_bound(raw_maximum, ranges, 1),
                    minimum_ref=raw_minimum if isinstance(raw_minimum, str) else None,
                    maximum_ref=raw_maximum if isinstance(raw_maximum, str) else None,
                )
            )
        if not fields:
            raise ProfileError("contract section has no fields")
        count_from = raw_section.get("count_from")
        if kind in _LIST_KINDS or kind == "matrix":
            if not isinstance(count_from, (int, str)) or isinstance(count_from, bool):
                raise ProfileError(f"section {section_id!r} has no resolvable count")
        specs.append(
            SectionSpec(
                id=section_id,
                kind=kind,
                fields=tuple(fields),
                count_from=count_from if isinstance(count_from, (int, str)) else None,
            )
        )
    return tuple(specs)


def _flat_ints(rows: Sequence[Sequence[str]]) -> list[int] | None:
    try:
        return [int(token) for row in rows for token in row]
    except ValueError:
        return None


class Profiler:
    """Parses inputs under one contract and derives byte-only feature vectors."""

    def __init__(self, contract: Mapping[str, Any] | None) -> None:
        try:
            self.specs: tuple[SectionSpec, ...] = build_specs(contract)
            self.reason: str | None = None
        except ProfileError as error:
            self.specs = ()
            self.reason = str(error)
        self.ranges = contract_ranges(contract)

    @property
    def structural(self) -> bool:
        """True when inputs can be parsed structurally (mutation is available)."""

        return bool(self.specs)

    def parse(self, data: bytes) -> ParsedInput:
        """Parse ``data`` into records. Raises :class:`ProfileError` on mismatch."""

        if not self.specs:
            raise ProfileError(self.reason or "contract is not parseable")
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError as error:
            raise ProfileError("input is not ASCII") from error
        cursor = _TokenCursor(text)
        parsed = ParsedInput(trailing_newline=text.endswith(("\n", "\r\n")))
        for spec in self.specs:
            if spec.kind == "scalar":
                tokens, spanned = cursor.take(spec.arity)
                data_section = SectionData(
                    spec=spec,
                    rows=[list(tokens)] if spanned else [[token] for token in tokens],
                    inline=spanned,
                )
                for field_spec, token in zip(spec.fields, tokens):
                    if field_spec.type == "int":
                        try:
                            parsed.scalars[f"{spec.id}.{field_spec.name}"] = int(token)
                        except ValueError as error:
                            raise ProfileError(
                                f"section {spec.id!r} field {field_spec.name!r} is not an integer"
                            ) from error
                parsed.sections.append(data_section)
                continue
            if spec.kind == "string":
                tokens, _ = cursor.take(1)
                parsed.sections.append(SectionData(spec=spec, rows=[list(tokens)]))
                continue
            count = self._resolve_count(spec, parsed)
            width = self._row_width(spec, parsed)
            tokens, spanned = cursor.take(count * width)
            rows = [list(tokens[index : index + width]) for index in range(0, len(tokens), width)]
            parsed.sections.append(
                SectionData(spec=spec, rows=rows, inline=spanned and len(rows) > 1)
            )
        if cursor.remaining:
            raise ProfileError(
                f"input has {cursor.remaining} trailing tokens the contract does not describe"
            )
        return parsed

    def _resolve_count(self, spec: SectionSpec, parsed: ParsedInput) -> int:
        target = spec.count_from
        if type(target) is int:
            return max(0, target)
        if isinstance(target, str) and target in parsed.scalars:
            return max(0, parsed.scalars[target])
        raise ProfileError(f"section {spec.id!r} count {target!r} is unresolved")

    def cols_target(self, spec: SectionSpec, parsed: ParsedInput) -> str | None:
        """Scalar path supplying a matrix section's column count, if any.

        A matrix declares one cell field, so its width lives in a sibling
        scalar.  Prefer an explicitly cols-like name and never reuse the scalar
        that already supplied the row count.  Public because a mutator changing
        the width has to update the same scalar the parser reads.
        """

        if spec.kind != "matrix":
            return None
        rows_target = spec.count_from if isinstance(spec.count_from, str) else None
        for name in ("cols", "m", "k", "c"):
            for path in parsed.scalars:
                if path == rows_target:
                    continue
                if path.rsplit(".", 1)[-1].casefold() == name:
                    return path
        return None

    def _row_width(self, spec: SectionSpec, parsed: ParsedInput) -> int:
        if spec.kind != "matrix":
            return spec.arity
        target = self.cols_target(spec, parsed)
        if target is None:
            raise ProfileError(f"matrix {spec.id!r} has no resolvable column count")
        return max(1, parsed.scalars[target])

    def features(self, data: bytes) -> dict[str, Any]:
        """Derive a feature vector from ``data`` alone.

        Always succeeds.  When the bytes do not match the contract the vector
        degrades to size/token statistics and ``parsed`` is ``False``; callers
        can still bucket on it, they just lose structural mutation.
        """

        text = data.decode("ascii", "replace")
        cursor = _TokenCursor(text)
        vector: dict[str, Any] = {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "bytes": len(data),
            "tokens": cursor.total,
            "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
            "parsed": False,
        }
        try:
            parsed = self.parse(data)
        except ProfileError as error:
            vector["parse_error"] = str(error)
            return vector
        vector["parsed"] = True
        vector.update(self._structural_features(parsed))
        return vector

    def _structural_features(self, parsed: ParsedInput) -> dict[str, Any]:
        primary = primary_section(parsed)
        if primary is None:
            return {"shape": "scalar_only", "count": 0}
        spec = primary.spec
        vector: dict[str, Any] = {"count": len(primary.rows)}
        if spec.kind == "string":
            vector["shape"] = "string"
            vector.update(_string_features(primary.rows[0][0]))
            # A string section is one row, so the row count carries no
            # information.  Report its length instead, keeping ``count`` the
            # uniform "payload size" axis across every shape.
            vector["count"] = vector.get("length", 0)
            return vector
        if spec.is_edge_list:
            vector["shape"] = "graph"
            vertices = self._vertex_count(spec, parsed, primary)
            vector.update(_graph_features(primary.rows, vertices))
            labels = primary.numeric_column(2) if spec.arity >= 3 else None
            if labels:
                vector.update(_numeric_features(labels, spec.fields[2], prefix="label_"))
            return vector
        if spec.is_interval:
            vector["shape"] = "interval"
            bounds = _flat_ints(primary.rows)
            if bounds is not None:
                vector.update(_interval_features(primary.rows))
                vector.update(_numeric_features(bounds, spec.fields[0]))
            return vector
        values = _flat_ints(primary.rows) if spec.fields[0].numeric else None
        vector["shape"] = "matrix" if spec.kind == "matrix" else "array"
        if spec.kind == "matrix":
            vector["cols"] = len(primary.rows[0])
        if values is not None:
            vector.update(_numeric_features(values, spec.fields[0]))
        return vector

    def _vertex_count(
        self, spec: SectionSpec, parsed: ParsedInput, primary: SectionData
    ) -> int:
        """Vertex count for an edge list: the header scalar, else the max label."""

        edge_target = spec.count_from if isinstance(spec.count_from, str) else None
        for path, value in parsed.scalars.items():
            if path != edge_target and path.rsplit(".", 1)[-1].casefold() in {"n", "v", "vertices"}:
                return max(1, value)
        endpoints = _flat_ints([row[:2] for row in primary.rows]) or [1]
        return max(1, max(endpoints))


def primary_section(parsed: ParsedInput) -> SectionData | None:
    """The section carrying the payload: the widest repeated block.

    Feature extraction and mutation must agree on which section matters, so
    both go through this one rule.
    """

    candidates = [data for data in parsed.sections if data.spec.kind != "scalar" and data.rows]
    if not candidates:
        return None
    return max(candidates, key=lambda data: (len(data.rows), data.spec.arity))


def _ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _numeric_features(
    values: Sequence[int], spec: FieldSpec, *, prefix: str = ""
) -> dict[str, Any]:
    if not values:
        return {}
    total = len(values)
    counts: dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    nondecreasing = sum(
        1 for index in range(1, total) if values[index - 1] <= values[index]
    )
    at_min = counts.get(spec.minimum, 0) if spec.minimum is not None else 0
    at_max = counts.get(spec.maximum, 0) if spec.maximum is not None else 0
    return {
        f"{prefix}value_min": min(values),
        f"{prefix}value_max": max(values),
        f"{prefix}distinct_ratio": _ratio(len(counts), total),
        f"{prefix}sorted_ratio": 1.0 if total < 2 else _ratio(nondecreasing, total - 1),
        f"{prefix}max_multiplicity_ratio": _ratio(max(counts.values()), total),
        f"{prefix}zero_ratio": _ratio(counts.get(0, 0), total),
        f"{prefix}negative_ratio": _ratio(sum(1 for v in values if v < 0), total),
        # Fraction of cells sitting exactly on a declared domain bound.  This is
        # the boundary-value signal the tag-based scheme could only assert.
        f"{prefix}boundary_ratio": _ratio(at_min + at_max, total),
    }


def _gini(values: Sequence[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    total = sum(ordered)
    if total <= 0:
        return 0.0
    count = len(ordered)
    weighted = sum((2 * index - count + 1) * value for index, value in enumerate(ordered))
    return _ratio(weighted, count * total)


def _graph_features(rows: Sequence[Sequence[str]], vertices: int) -> dict[str, Any]:
    edges: list[tuple[int, int]] = []
    for row in rows:
        try:
            edges.append((int(row[0]), int(row[1])))
        except (ValueError, IndexError):
            return {"vertices": vertices}
    degree: dict[int, int] = {}
    seen: set[tuple[int, int]] = set()
    self_loops = parallel = 0
    parent = {vertex: vertex for edge in edges for vertex in edge}

    def find(vertex: int) -> int:
        while parent[vertex] != vertex:
            parent[vertex] = parent[parent[vertex]]
            vertex = parent[vertex]
        return vertex

    for u, v in edges:
        degree[u] = degree.get(u, 0) + 1
        degree[v] = degree.get(v, 0) + 1
        if u == v:
            self_loops += 1
        key = (u, v) if u <= v else (v, u)
        if key in seen:
            parallel += 1
        seen.add(key)
        root_u, root_v = find(u), find(v)
        if root_u != root_v:
            parent[root_u] = root_v
    # Isolated vertices never appear in the edge list, so count them explicitly.
    touched = len(parent)
    components = len({find(vertex) for vertex in parent}) + max(0, vertices - touched)
    degrees = [degree.get(vertex, 0) for vertex in range(1, vertices + 1)] or [0]
    return {
        "vertices": vertices,
        "density": _ratio(len(edges), max(1, vertices * (vertices - 1) / 2)),
        "self_loop_count": self_loops,
        "parallel_edge_count": parallel,
        "max_degree_ratio": _ratio(max(degrees), max(1, vertices - 1)),
        "degree_gini": _gini(degrees),
        "components": components,
        "connected": components == 1,
        "is_tree": components == 1 and len(edges) == vertices - 1,
    }


def _string_features(value: str) -> dict[str, Any]:
    if not value:
        return {"length": 0}
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    runs = 1 + sum(
        1 for index in range(1, len(value)) if value[index] != value[index - 1]
    )
    return {
        "length": len(value),
        "alphabet_size": len(counts),
        "distinct_ratio": _ratio(len(counts), len(value)),
        "max_multiplicity_ratio": _ratio(max(counts.values()), len(value)),
        "run_ratio": _ratio(runs, len(value)),
        "sorted_ratio": _ratio(
            sum(1 for i in range(1, len(value)) if value[i - 1] <= value[i]),
            max(1, len(value) - 1),
        ),
    }


def _interval_features(rows: Sequence[Sequence[str]]) -> dict[str, Any]:
    try:
        pairs = [(int(row[0]), int(row[1])) for row in rows]
    except (ValueError, IndexError):
        return {}
    events: list[tuple[int, int]] = []
    for low, high in pairs:
        events.append((low, 1))
        events.append((high + 1, -1))
    events.sort()
    depth = peak = 0
    for _, delta in events:
        depth += delta
        peak = max(peak, depth)
    nested = 0
    for index, (low, high) in enumerate(pairs):
        if any(
            other_low <= low and high <= other_high and other != index
            for other, (other_low, other_high) in enumerate(pairs)
        ):
            nested += 1
    return {
        "max_overlap": peak,
        "overlap_ratio": _ratio(peak, len(pairs)),
        "nested_ratio": _ratio(nested, len(pairs)),
        "point_ratio": _ratio(sum(1 for low, high in pairs if low == high), len(pairs)),
    }


def _bucket(value: float, buckets: int) -> int:
    """Quantize a ratio in [0, 1] into ``buckets`` equal-width cells."""

    if buckets <= 1:
        return 0
    index = int(float(value) * buckets)
    return max(0, min(buckets - 1, index))


def _log_bucket(value: Any) -> int:
    try:
        return max(0, int(value)).bit_length()
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class Axis:
    """One dimension of the behaviour grid.

    Declaring axes instead of hand-writing :func:`descriptor` is what makes the
    space *enumerable*: layer 3 has to name an empty cell, label it for a human,
    and count how much of each dimension has been reached.  A hand-rolled tuple
    supports none of that without drifting out of sync with the cell key.
    """

    name: str
    kind: str
    feature: str
    size: int = 0
    cap: int = 0
    description: str = ""
    #: Smallest numerator this ratio can take.  ``distinct`` and ``max_mult``
    #: are never zero — a payload always has at least one distinct value and at
    #: least one occurrence of it — so bucket 0 is unreachable for them.
    min_numerator: int = 0

    def value(self, features: Mapping[str, Any]) -> Any:
        raw = features.get(self.feature)
        if self.kind == "log":
            return _log_bucket(raw)
        if self.kind == "bucket":
            return _bucket(raw if raw is not None else 0.0, self.size)
        if self.kind == "flag":
            return bool(raw)
        return min(self.cap, max(0, int(raw or 0)))

    @property
    def cardinality(self) -> int:
        """Number of reachable values, or 0 when only the problem bounds it."""

        if self.kind == "bucket":
            return self.size
        if self.kind == "flag":
            return 2
        if self.kind == "clamp":
            return self.cap + 1
        return 0

    def label(self, value: Any) -> str:
        if self.kind == "log":
            index = int(value)
            if index <= 0:
                return "0"
            low = 1 << (index - 1)
            return str(low) if index == 1 else f"{low}..{(1 << index) - 1}"
        if self.kind == "bucket":
            index = int(value)
            return f"{index / self.size:.0%}-{(index + 1) / self.size:.0%}"
        if self.kind == "flag":
            return "yes" if value else "no"
        index = int(value)
        return f"{index}+" if index >= self.cap else str(index)


_COUNT_AXIS = Axis("count", "log", "count", description="payload size")

_ARRAY_AXES = (
    _COUNT_AXIS,
    Axis("distinct", "bucket", "distinct_ratio", size=4, min_numerator=1, description="distinct values / total"),
    Axis("sorted", "bucket", "sorted_ratio", size=4, description="non-decreasing adjacent pairs"),
    Axis("max_mult", "bucket", "max_multiplicity_ratio", size=3, min_numerator=1, description="most frequent value share"),
    Axis("boundary", "bucket", "boundary_ratio", size=3, description="cells on a declared domain bound"),
    Axis("negative", "bucket", "negative_ratio", size=2, description="negative value share"),
)

_GRAPH_AXES = (
    _COUNT_AXIS,
    Axis("density", "bucket", "density", size=4, description="edges / complete graph"),
    Axis("max_degree", "bucket", "max_degree_ratio", size=3, description="hub concentration"),
    Axis("degree_gini", "bucket", "degree_gini", size=3, description="degree inequality"),
    Axis("connected", "flag", "connected", description="single component"),
    Axis("tree", "flag", "is_tree", description="connected and acyclic"),
    Axis("self_loops", "clamp", "self_loop_count", cap=2, description="self loop count"),
    Axis("parallel", "clamp", "parallel_edge_count", cap=2, description="parallel edge count"),
)

_INTERVAL_AXES = (
    _COUNT_AXIS,
    Axis("overlap", "bucket", "overlap_ratio", size=4, min_numerator=1, description="peak stack depth / count"),
    Axis("nested", "bucket", "nested_ratio", size=3, description="contained intervals share"),
    Axis("points", "bucket", "point_ratio", size=3, description="zero-length share"),
)

_STRING_AXES = (
    _COUNT_AXIS,
    Axis("alphabet", "log", "alphabet_size", description="distinct characters"),
    Axis("runs", "bucket", "run_ratio", size=4, description="run boundaries / length"),
    Axis("sorted", "bucket", "sorted_ratio", size=3, description="non-decreasing adjacent pairs"),
)

_MATRIX_AXES = (
    _COUNT_AXIS,
    Axis("cols", "log", "cols", description="row width"),
    Axis("distinct", "bucket", "distinct_ratio", size=4, min_numerator=1, description="distinct values / total"),
    Axis("sorted", "bucket", "sorted_ratio", size=3, description="non-decreasing adjacent pairs"),
    Axis("max_mult", "bucket", "max_multiplicity_ratio", size=3, min_numerator=1, description="most frequent value share"),
    Axis("boundary", "bucket", "boundary_ratio", size=3, description="cells on a declared domain bound"),
)

_UNPARSED_AXES = (
    Axis("bytes", "log", "bytes", description="input size"),
    Axis("tokens", "log", "tokens", description="whitespace token count"),
)

_GRID_AXES: dict[str, tuple[Axis, ...]] = {
    "array": _ARRAY_AXES,
    "graph": _GRAPH_AXES,
    "interval": _INTERVAL_AXES,
    "string": _STRING_AXES,
    "matrix": _MATRIX_AXES,
    "unparsed": _UNPARSED_AXES,
    # An empty payload (m=0) has no shape to measure.  Give it no axes so it
    # stays a single honest cell instead of borrowing the array axes and
    # spawning targets like "scalar_only with 128 elements".
    "scalar_only": (),
}


def descriptor(features: Mapping[str, Any]) -> tuple[Any, ...]:
    """Project a feature vector onto a coarse, hashable behaviour cell.

    This is the novelty key: two inputs sharing a descriptor are considered
    behaviourally equivalent, so the corpus keeps only the smaller one.  Cells
    are deliberately coarse — a descriptor that tracked every feature bit would
    make every input novel and the archive would degenerate into a log.
    """

    shape = cell_shape(features)
    return (shape,) + tuple(axis.value(features) for axis in grid_axes(shape))


def cell_shape(features: Mapping[str, Any]) -> str:
    """The shape name a feature vector's cell belongs to."""

    if not features.get("parsed"):
        return "unparsed"
    return str(features.get("shape") or "unknown")


def grid_axes(shape: str) -> tuple[Axis, ...]:
    """Axes for ``shape``. Unknown shapes reuse the array axes."""

    return _GRID_AXES.get(shape, _ARRAY_AXES)


def describe_cell(cell: tuple[Any, ...]) -> str:
    """Render a cell as ``shape[axis=label, ...]`` for humans and for prompts."""

    if not cell:
        return "<empty>"
    shape = str(cell[0])
    axes = grid_axes(shape)
    parts = [axis.name + "=" + axis.label(value) for axis, value in zip(axes, cell[1:])]
    return f"{shape}[" + ", ".join(parts) + "]"


__all__ = [
    "Axis",
    "FEATURE_SCHEMA_VERSION",
    "FieldSpec",
    "cell_shape",
    "describe_cell",
    "grid_axes",
    "ParsedInput",
    "ProfileError",
    "Profiler",
    "SectionData",
    "SectionSpec",
    "build_specs",
    "contract_ranges",
    "descriptor",
    "primary_section",
]
