"""Strict local-template compiler for AI stress generator recipes.

The model-facing object handled here is deliberately data, not source code.  A
validated ``generator_recipe/v1`` can only select audited template identifiers,
integer/enum parameters and a static serializer.  The resulting translation
unit keeps the existing trusted-harness ABI and never defines ``main``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from itertools import combinations
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .canonical import canonical_json_bytes as _canonical_json

GENERATOR_RECIPE_SCHEMA_VERSION = 1
GENERATOR_RECIPE_ENGINE = "local_templates_v1"
GENERATOR_RECIPE_COMPOSER_VERSION = 2
# Exhaustive enumeration deliberately stays tiny; generated small-profile
# cases use the normal sandbox stream ceiling instead.  These are independent
# policies: raising the recipe ceiling must never make enumeration explode.
SMALL_EXHAUSTIVE_MAX_BYTES = 100
SMALL_RECIPE_HARD_MAX_BYTES = 2 * 1024 * 1024
LARGE_RECIPE_HARD_MAX_BYTES = 32 * 1024 * 1024
SMALL_RECIPE_BUCKET_RENDER_ATTEMPTS = 32
LARGE_RECIPE_BUCKET_RENDER_ATTEMPTS = 4
DEFAULT_CATALOG_ROOT = Path(__file__).with_name("generator_templates")

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_SAFE_TARGET = re.compile(r"^[a-z][a-z0-9_]*(?:\.[A-Za-z0-9_]+)*$")
_SAFE_ENUM = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

_SUPPORTED_CASES = frozenset(
    {
        ("small", "lower_bound"),
        ("small", "random"),
        ("large", "upper_bound"),
        ("large", "random"),
    }
)
_SUPPORTED_SECTION_KINDS = frozenset(
    {"scalar", "list", "string", "matrix", "interval", "intervals", "edge_list"}
)
_UNSUPPORTED_SECTION_KINDS = frozenset({"raw", "operation_stream"})
_SERIALIZER_KINDS = {
    "list_n": "array",
    "string_n": "string",
    "matrix_n_m": "matrix",
    "intervals_n": "interval",
    "edge_list_n_m_u_v": "edge",
    "edge_list_n_m_u_v_w": "edge",
}
_STRUCTURE_PREFIX_KINDS = {
    "scalar": "scalar",
    "array": "array",
    "list": "array",
    "string": "string",
    "matrix": "matrix",
    "interval": "interval",
    "graph": "edge",
    "tree": "edge",
}
_SEMANTIC_GOALS = frozenset(
    {
        "connected",
        "disconnected",
        "single_vertex",
        "early_threshold_connects",
        "last_threshold_connects",
        "never_connects",
        "equal_labels",
        "distinct_labels",
        "boundary_values",
        "duplicate_values",
        "self_loop",
        "parallel_edges",
        "acyclic",
        "one_cycle",
        "bipartite",
        "nested",
        "disjoint",
        "high_overlap",
        "endpoint_heavy",
        "monotone",
        "permutation",
        "periodic",
        "long_runs",
        "seed_variation",
        "lower_bound",
        "upper_bound",
    }
)
_PARAMETER_KEYS = frozenset(
    {
        "n",
        "n_min",
        "n_max",
        "m",
        "m_min",
        "m_max",
        "rows",
        "rows_min",
        "rows_max",
        "cols",
        "cols_min",
        "cols_max",
        "length",
        "length_min",
        "length_max",
        "value",
        "value_min",
        "value_max",
        "label_min",
        "label_max",
        "alphabet_size",
        "distinct",
        "period",
        "run_length",
        "components",
        "k",
        "spine",
        "elongation",
        "index_base",
        "allow_self_loops",
        "allow_parallel_edges",
        "directed",
        "order",
        "policy",
        "seed",
        "lo",
        "hi",
        "base",
        "value_count",
        "run_count",
        "alphabet",
        "pattern",
        "component_count",
        "count",
        "n_left",
        "n_right",
        "nondecreasing",
        "spine_length",
    }
)


class RecipeError(ValueError):
    """Base error for the provider-free recipe path."""

    code = "generator_recipe_error"

    def __init__(self, message: str, *, path: str = "$", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.path = path
        self.details = dict(details or {})


class RecipeValidationError(RecipeError):
    code = "invalid_generator_recipe"


class RecipeCatalogError(RecipeError):
    code = "invalid_generator_template_catalog"


class UnsupportedRecipeError(RecipeValidationError):
    code = "unsupported_generator_recipe"

    def __init__(self, reason: str, message: str | None = None, *, path: str = "$") -> None:
        self.reason = reason
        super().__init__(message or reason, path=path, details={"reason": reason})


@dataclass(frozen=True)
class CatalogTemplate:
    template_id: str
    kind: str
    parameters: Mapping[str, Mapping[str, Any]]
    source: str | None
    symbol: str | None
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class RecipeCatalog:
    root: Path
    raw: Mapping[str, Any]
    templates: Mapping[str, CatalogTemplate]
    serializers: Mapping[str, Mapping[str, Any]]
    sha256: str
    source_fragments: tuple[str, ...]

    @classmethod
    def load(cls, root: str | Path | None = None) -> "RecipeCatalog":
        catalog_root = Path(root) if root is not None else DEFAULT_CATALOG_ROOT
        try:
            resolved_root = catalog_root.resolve(strict=True)
        except OSError as exc:
            raise RecipeCatalogError(f"template catalog directory is unavailable: {exc}") from exc
        if not resolved_root.is_dir():
            raise RecipeCatalogError("template catalog root is not a directory")
        catalog_path = resolved_root / "catalog.json"
        try:
            catalog_bytes = catalog_path.read_bytes()
            raw = json.loads(catalog_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecipeCatalogError(f"cannot read catalog.json: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise RecipeCatalogError("catalog.json must contain an object")
        if raw.get("schema_version") != 1:
            raise RecipeCatalogError("catalog schema_version must be 1", path="$.schema_version")

        templates = _load_catalog_entries(raw.get("templates"), kind="template")
        serializers_raw = _load_plain_catalog_entries(raw.get("serializers"), kind="serializer")
        if not templates:
            raise RecipeCatalogError("catalog must declare at least one template", path="$.templates")
        if not serializers_raw:
            raise RecipeCatalogError("catalog must declare at least one serializer", path="$.serializers")
        required_metadata = {
            "parameters", "preconditions", "invariants", "complexity", "index_base",
            "self_loops", "parallel_edges", "profiles", "source", "symbol",
        }
        for template_id, entry in templates.items():
            missing = required_metadata - set(entry.metadata)
            if missing:
                raise RecipeCatalogError(
                    f"template metadata is incomplete: {', '.join(sorted(missing))}",
                    path=f"$.templates.{template_id}",
                )
            for field in ("preconditions", "invariants", "profiles"):
                item = entry.metadata.get(field)
                if not isinstance(item, Sequence) or isinstance(item, (str, bytes)):
                    raise RecipeCatalogError(
                        f"template {field} must be an array",
                        path=f"$.templates.{template_id}.{field}",
                    )
            if not isinstance(entry.metadata.get("complexity"), str):
                raise RecipeCatalogError(
                    "template complexity must be a string",
                    path=f"$.templates.{template_id}.complexity",
                )

        digest = hashlib.sha256()
        fragments: list[str] = []
        allowed_suffixes = {".json", ".hpp", ".h", ".inc", ".cpp", ".md", ".txt"}
        total = 0
        files = sorted((path for path in resolved_root.rglob("*") if path.is_file()), key=lambda p: p.relative_to(resolved_root).as_posix())
        source_texts: dict[str, str] = {}
        for path in files:
            if path.is_symlink():
                raise RecipeCatalogError("catalog files may not be symlinks")
            rel = path.relative_to(resolved_root).as_posix()
            if path.suffix.casefold() not in allowed_suffixes and path.name not in {"LICENSE", "NOTICE"}:
                raise RecipeCatalogError(f"unsupported catalog file type: {rel}")
            data = path.read_bytes()
            total += len(data)
            if len(data) > 512 * 1024 or total > 4 * 1024 * 1024:
                raise RecipeCatalogError("template catalog is too large")
            digest.update(len(rel.encode("utf-8")).to_bytes(4, "big"))
            digest.update(rel.encode("utf-8"))
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
            if path.suffix.casefold() in {".hpp", ".h", ".inc", ".cpp"}:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise RecipeCatalogError(f"non-UTF-8 source asset: {rel}") from exc
                if "\x00" in text or re.search(r"^\s*#\s*include\s*[\"']", text, re.MULTILINE):
                    raise RecipeCatalogError(f"unsafe quoted include in source asset: {rel}")
                source_texts[rel] = text

        for entry in templates.values():
            if entry.source is not None:
                _safe_catalog_member(resolved_root, entry.source)
        declared_order = raw.get("source_order", raw.get("inline_order"))
        ordered_sources: list[str] = []
        if isinstance(declared_order, Sequence) and not isinstance(declared_order, (str, bytes)):
            for item in declared_order:
                if not isinstance(item, str) or item not in source_texts:
                    raise RecipeCatalogError("catalog source_order references an unknown source")
                if item in ordered_sources:
                    raise RecipeCatalogError("catalog source_order contains a duplicate source")
                ordered_sources.append(item)
        priority = {"rng.hpp": 0, "structures.hpp": 1, "labels.hpp": 2, "serializers.hpp": 3}
        ordered_sources.extend(
            sorted(
                (rel for rel in source_texts if rel not in ordered_sources),
                key=lambda rel: (priority.get(Path(rel).name, 100), rel),
            )
        )
        fragments = [source_texts[rel] for rel in ordered_sources]
        return cls(
            root=resolved_root,
            raw=MappingProxyType(dict(raw)),
            templates=MappingProxyType(templates),
            serializers=MappingProxyType(serializers_raw),
            sha256=digest.hexdigest(),
            source_fragments=tuple(fragments),
        )


@dataclass(frozen=True)
class ComposedGenerator:
    source: str
    recipe: Mapping[str, Any]
    recipe_sha256: str
    catalog_sha256: str
    composer_version: int
    metadata: Mapping[str, Any]


def _expect_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecipeValidationError("expected an object", path=path)
    return value


def _strict_keys(value: Mapping[str, Any], *, required: set[str], optional: set[str] = frozenset(), path: str) -> None:
    keys = set(value)
    unknown = sorted(keys - required - optional)
    missing = sorted(required - keys)
    if unknown:
        raise RecipeValidationError(f"unknown field(s): {', '.join(unknown)}", path=path)
    if missing:
        raise RecipeValidationError(f"missing field(s): {', '.join(missing)}", path=path)


def _integer(value: Any, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecipeValidationError("expected an integer", path=path)
    if minimum is not None and value < minimum:
        raise RecipeValidationError(f"integer must be >= {minimum}", path=path)
    if maximum is not None and value > maximum:
        raise RecipeValidationError(f"integer must be <= {maximum}", path=path)
    return value


def _safe_catalog_member(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise RecipeCatalogError("catalog source must be a non-empty POSIX relative path")
    try:
        candidate = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise RecipeCatalogError("catalog source does not exist") from exc
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RecipeCatalogError("catalog source escapes the catalog root") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise RecipeCatalogError("catalog source must be a regular non-symlink file")
    return candidate


def _load_catalog_entries(value: Any, *, kind: str) -> dict[str, CatalogTemplate]:
    plain = _load_plain_catalog_entries(value, kind=kind)
    result: dict[str, CatalogTemplate] = {}
    for template_id, entry in plain.items():
        entry_kind = str(entry.get("kind") or template_id.split(".", 1)[0])
        parameters_raw = entry.get("parameters", {})
        if isinstance(parameters_raw, Sequence) and not isinstance(parameters_raw, (str, bytes)):
            parameters_raw = {str(name): {} for name in parameters_raw}
        if not isinstance(parameters_raw, Mapping):
            raise RecipeCatalogError("template parameters must be an object or name list", path=f"$.templates.{template_id}.parameters")
        parameters: dict[str, Mapping[str, Any]] = {}
        for name, spec in parameters_raw.items():
            if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
                raise RecipeCatalogError("unsafe template parameter name")
            if not isinstance(spec, Mapping):
                spec = {"type": str(spec)}
            parameters[name] = MappingProxyType(dict(spec))
        result[template_id] = CatalogTemplate(
            template_id=template_id,
            kind=entry_kind,
            parameters=MappingProxyType(parameters),
            source=str(entry["source"]) if entry.get("source") is not None else None,
            symbol=str(entry["symbol"]) if entry.get("symbol") is not None else None,
            metadata=MappingProxyType(dict(entry)),
        )
    return result


def _load_plain_catalog_entries(value: Any, *, kind: str) -> dict[str, Mapping[str, Any]]:
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = []
        for index, item in enumerate(value):
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                raise RecipeCatalogError(f"catalog {kind} list entries require id", path=f"$.{kind}s[{index}]")
            items.append((item["id"], item))
    else:
        raise RecipeCatalogError(f"catalog {kind}s must be an object or array")
    result: dict[str, Mapping[str, Any]] = {}
    for raw_id, raw_entry in items:
        entry_id = str(raw_id)
        if not _SAFE_ID.fullmatch(entry_id):
            raise RecipeCatalogError(f"unsafe catalog {kind} id: {entry_id}")
        if entry_id in result:
            raise RecipeCatalogError(f"duplicate catalog {kind} id: {entry_id}")
        if not isinstance(raw_entry, Mapping):
            raw_entry = {}
        result[entry_id] = MappingProxyType(dict(raw_entry))
    return result


def _field_path(section: Mapping[str, Any], field: Mapping[str, Any]) -> str:
    return f"{section.get('id')}.{field.get('name')}"


def _plain_fields(section: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    fields = section.get("fields")
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        return None
    if any(not isinstance(field, Mapping) for field in fields):
        return None
    return list(fields)


def static_contract_capabilities(
    contract: Mapping[str, Any] | None,
    *,
    catalog: RecipeCatalog | None = None,
) -> dict[str, dict[str, Any]]:
    """Return exact composer wire formats that can represent ``contract``.

    A recipe family emits exactly one structure through exactly one serializer.
    Merely recognizing every section kind is therefore insufficient: accepting
    a scalar-only or multi-payload contract would produce extra or missing
    tokens even when the recipe itself validates.  Keep this matcher deliberately
    conservative; an unknown shape uses the legacy generator instead of claiming
    a static representation that the current ABI cannot provide.
    """

    if contract is None:
        return {}
    if not isinstance(contract, Mapping):
        return {}
    syntax = contract.get("syntax")
    if not isinstance(syntax, Mapping):
        return {}
    if syntax.get("mode") != "single_case":
        return {}
    sections = syntax.get("sections")
    if (
        not isinstance(sections, Sequence)
        or isinstance(sections, (str, bytes))
        or len(sections) != 2
        or any(not isinstance(section, Mapping) for section in sections)
    ):
        return {}
    header, payload = sections
    if header.get("kind") != "scalar":
        return {}
    header_fields = _plain_fields(header)
    payload_fields = _plain_fields(payload)
    if header_fields is None or payload_fields is None:
        return {}
    if header.get("variants") or payload.get("variants"):
        return {}
    if any(field.get("type") != "int" for field in header_fields):
        return {}

    candidates: dict[str, dict[str, Any]] = {}
    payload_kind = str(payload.get("kind") or "")
    if len(header_fields) == 1:
        count_target = _field_path(header, header_fields[0])
        if payload.get("count_from") == count_target and len(payload_fields) == 1:
            payload_type = payload_fields[0].get("type")
            if payload_kind == "list" and payload_type == "int":
                candidates["list_n"] = {
                    "structure_kind": "array",
                    "bindings": {"n": count_target},
                }
            elif payload_kind == "string" and payload_type in {
                "string", "token", "char"
            }:
                candidates["string_n"] = {
                    "structure_kind": "string",
                    "bindings": {"n": count_target},
                }
        if payload.get("count_from") == count_target and len(payload_fields) == 2:
            names = tuple(str(field.get("name") or "").casefold() for field in payload_fields)
            interval_names = {("l", "r"), ("left", "right"), ("start", "end")}
            if (
                all(field.get("type") == "int" for field in payload_fields)
                and (
                    payload_kind in {"interval", "intervals"}
                    or (payload_kind == "list" and names in interval_names)
                )
            ):
                candidates["intervals_n"] = {
                    "structure_kind": "interval",
                    "bindings": {"n": count_target},
                }
    elif len(header_fields) == 2 and payload_kind == "matrix":
        rows_target = _field_path(header, header_fields[0])
        cols_target = _field_path(header, header_fields[1])
        if (
            payload.get("count_from") in {None, rows_target}
            and len(payload_fields) == 1
            and payload_fields[0].get("type") == "int"
        ):
            candidates["matrix_n_m"] = {
                "structure_kind": "matrix",
                "bindings": {"rows": rows_target, "cols": cols_target},
            }
    elif len(header_fields) == 2 and payload_kind == "edge_list":
        n_target = _field_path(header, header_fields[0])
        m_target = _field_path(header, header_fields[1])
        if (
            payload.get("count_from") == m_target
            and len(payload_fields) in {2, 3}
            and all(field.get("type") == "int" for field in payload_fields)
        ):
            format_id = (
                "edge_list_n_m_u_v"
                if len(payload_fields) == 2
                else "edge_list_n_m_u_v_w"
            )
            candidates[format_id] = {
                "structure_kind": "edge",
                "bindings": {
                    "n": n_target,
                    "m": m_target,
                    **(
                        {"label": _field_path(payload, payload_fields[2])}
                        if len(payload_fields) == 3
                        else {}
                    ),
                },
            }

    active_catalog = catalog or RecipeCatalog.load()
    return {
        format_id: capability
        for format_id, capability in candidates.items()
        if format_id in active_catalog.serializers
    }


def supports_static_contract(
    contract: Mapping[str, Any] | None,
    *,
    catalog: RecipeCatalog | None = None,
) -> tuple[bool, str | None]:
    if contract is None:
        return False, "contract_missing"
    if not isinstance(contract, Mapping):
        return False, "contract_not_object"
    syntax = contract.get("syntax")
    if not isinstance(syntax, Mapping):
        return False, "contract_missing_structured_syntax"
    sections = syntax.get("sections")
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)) or not sections:
        return False, "contract_missing_structured_sections"
    for section in sections:
        if not isinstance(section, Mapping):
            return False, "contract_invalid_section"
        section_kind = str(section.get("kind") or "")
        if section_kind in _UNSUPPORTED_SECTION_KINDS:
            return False, f"unsupported_contract_section:{section_kind}"
        if section_kind not in _SUPPORTED_SECTION_KINDS:
            return False, f"unknown_contract_section:{section_kind or 'missing'}"
    if syntax.get("mode") != "single_case":
        return False, "contract_mode_not_single_case"
    capabilities = static_contract_capabilities(contract, catalog=catalog)
    if not capabilities:
        return False, "contract_wire_shape_not_representable"
    ranges = _contract_ranges(contract)
    if any(
        target not in ranges
        for capability in capabilities.values()
        for target in capability["bindings"].values()
    ):
        return False, "contract_binding_range_missing"
    return True, None


def _contract_targets(contract: Mapping[str, Any] | None) -> set[str]:
    result: set[str] = set()
    if not contract:
        return result
    syntax = contract.get("syntax")
    sections = syntax.get("sections", []) if isinstance(syntax, Mapping) else []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        section_id = str(section.get("id") or "")
        if section_id:
            result.add(section_id)
        for field in section.get("fields", []):
            if isinstance(field, Mapping) and section_id and isinstance(field.get("name"), str):
                result.add(f"{section_id}.{field['name']}")
    return result


def _contract_ranges(contract: Mapping[str, Any] | None) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    if not contract:
        return result
    constraints = contract.get("constraints", [])
    if not isinstance(constraints, Sequence) or isinstance(constraints, (str, bytes)):
        return result
    for constraint in constraints:
        if not isinstance(constraint, Mapping) or constraint.get("kind") != "range":
            continue
        target = constraint.get("target")
        args = constraint.get("args")
        if not isinstance(target, str) or not isinstance(args, Mapping):
            continue
        minimum, maximum = args.get("minimum"), args.get("maximum")
        if type(minimum) is int and type(maximum) is int and minimum <= maximum:
            result[target] = (minimum, maximum)
    return result


def _required_binding_keys(serializer: str) -> tuple[str, ...]:
    if serializer in {"list_n", "string_n", "intervals_n"}:
        return ("n",)
    if serializer == "matrix_n_m":
        return ("rows", "cols")
    if serializer.startswith("edge_list_n_m_"):
        return ("n", "m")
    return ()


def _infer_binding(key: str, ranges: Mapping[str, tuple[int, int]], used: set[str]) -> str | None:
    names = {
        "n": {"n", "length", "count"},
        "m": {"m", "edges", "count"},
        "rows": {"n", "rows"},
        "cols": {"m", "cols"},
    }.get(key, {key})
    candidates = [
        target for target in ranges
        if target not in used and target.rsplit(".", 1)[-1].casefold() in names
    ]
    return candidates[0] if len(candidates) == 1 else None


def _repair_random_simple_graph_domain(
    structure: Mapping[str, Any],
    *,
    case_kind: str,
) -> None:
    """Narrow independently sampled random n/m ranges to a feasible product."""

    if case_kind != "random":
        return
    template_id = str(structure.get("template_id") or "")
    if not template_id.startswith("graph.") or template_id in {
        "graph.self_loops", "graph.parallel_edges", "graph.bipartite"
    }:
        return
    params = structure.get("parameters")
    if not isinstance(params, dict):
        return
    n_min = params.get("n_min", params.get("n"))
    n_max = params.get("n_max", params.get("n"))
    m_min = params.get("m_min", params.get("m"))
    m_max = params.get("m_max", params.get("m"))
    if not all(type(item) is int for item in (n_min, n_max, m_min, m_max)):
        return
    n_min, n_max, m_min, m_max = int(n_min), int(n_max), int(m_min), int(m_max)
    if template_id == "graph.components":
        raw_count = params.get("component_count", params.get("components"))
        if type(raw_count) is not int or n_max < 2:
            return
        count = min(max(2, int(raw_count)), n_max)
        feasible_n_min = max(n_min, count)
        if m_min > 0 and feasible_n_min == count:
            feasible_n_min += 1
        if feasible_n_min > n_max:
            return
        # A c-component graph needs at least n-c forest edges.  Random
        # profiles may narrow n_max; upper-bound profiles are handled by the
        # exact boundary path and are never rewritten here.
        feasible_n_max = min(n_max, m_max + count)
        if feasible_n_min > feasible_n_max:
            return
        required_m_min = max(m_min, feasible_n_max - count)

        def component_capacity(n: int) -> int:
            quotient, remainder = divmod(n, count)
            return (
                remainder * (quotient + 1) * quotient // 2
                + (count - remainder) * quotient * (quotient - 1) // 2
            )

        while (
            feasible_n_min <= feasible_n_max
            and component_capacity(feasible_n_min) < required_m_min
        ):
            feasible_n_min += 1
        if feasible_n_min > feasible_n_max:
            return
        feasible_m_max = min(m_max, component_capacity(feasible_n_min))
        if required_m_min > feasible_m_max:
            return
        if "component_count" in params:
            params["component_count"] = count
        elif "components" in params:
            params["components"] = count
        if "n" in params and "n_min" not in params and "n_max" not in params:
            params["n"] = feasible_n_min
        else:
            if "n_min" in params:
                params["n_min"] = feasible_n_min
            if "n_max" in params:
                params["n_max"] = feasible_n_max
        if "m" in params and "m_min" not in params and "m_max" not in params:
            params["m"] = required_m_min
        else:
            if "m_min" in params:
                params["m_min"] = required_m_min
            if "m_max" in params:
                params["m_max"] = feasible_m_max
        return
    required_m_min = max(m_min, n_max - 1) if template_id == "graph.connected" else m_min
    feasible_n_min = n_min
    while (
        feasible_n_min <= n_max
        and feasible_n_min * (feasible_n_min - 1) // 2 < required_m_min
    ):
        feasible_n_min += 1
    if feasible_n_min > n_max:
        return
    capacity = feasible_n_min * (feasible_n_min - 1) // 2
    feasible_m_max = min(m_max, capacity)
    if required_m_min > feasible_m_max:
        return
    if "n" in params and "n_min" not in params and "n_max" not in params:
        params["n"] = feasible_n_min
    else:
        if "n_min" in params:
            params["n_min"] = feasible_n_min
    if "m" in params and "m_min" not in params and "m_max" not in params:
        params["m"] = required_m_min
    else:
        if "m_min" in params:
            params["m_min"] = required_m_min
        if "m_max" in params:
            params["m_max"] = feasible_m_max


def _apply_contract_bindings(
    families: Sequence[dict[str, Any]],
    bindings: Mapping[str, str],
    ranges: Mapping[str, tuple[int, int]],
    *,
    profile: str,
    case_kind: str,
    catalog: RecipeCatalog,
    path: str,
) -> None:
    dimension_keys = {"n", "m", "rows", "cols", "length"}
    aliases = {
        "n": ("n", "n_min", "n_max"),
        "m": ("m", "m_min", "m_max"),
        "rows": ("rows", "rows_min", "rows_max"),
        "cols": ("cols", "cols_min", "cols_max"),
        "length": ("length", "length_min", "length_max", "n", "n_min", "n_max"),
        "value": ("value", "value_min", "value_max", "lo", "hi"),
    }
    for family_index, family in enumerate(families):
        structure = family["structure"]
        for key, target in bindings.items():
            if target not in ranges:
                raise RecipeValidationError("binding target lacks a machine-checkable integer range", path=f"{path}.{key}")
            minimum, maximum = ranges[target]
            if key == "label":
                refs = family["labels"]
                names = ("label_min", "label_max", "lo", "hi")
            else:
                refs = (structure,)
                names = aliases.get(key, (key, f"{key}_min", f"{key}_max"))
            for ref in refs:
                ref_params = ref["parameters"]
                entry = catalog.templates[ref["template_id"]]
                supported_names = [name for name in names if name in entry.parameters]
                if key == "label":
                    # Label values are part of the serialized wire format, not
                    # advisory recipe metadata.  Materialize the certified
                    # contract range locally so omitted provider parameters can
                    # never fall back to a catalog runtime default outside the
                    # problem domain.
                    if "label_min" in entry.parameters:
                        ref_params["label_min"] = minimum
                    elif "value_min" in entry.parameters:
                        ref_params["value_min"] = minimum
                    elif "lo" in entry.parameters:
                        ref_params["lo"] = minimum
                    if "label_max" in entry.parameters:
                        ref_params["label_max"] = maximum
                    elif "value_max" in entry.parameters:
                        ref_params["value_max"] = maximum
                    elif "hi" in entry.parameters:
                        ref_params["hi"] = maximum
                derived_value: int | None = None
                derived_range: tuple[int, int] | None = None
                if not supported_names and key == "n" and ref["template_id"] == "graph.bipartite":
                    left, right = ref_params.get("n_left"), ref_params.get("n_right")
                    if isinstance(left, int) and isinstance(right, int):
                        derived_value = left + right
                elif not supported_names and key == "m" and (
                    ref["template_id"].startswith("tree.")
                    or ref["template_id"] in {"graph.cycle", "graph.unicyclic"}
                ):
                    node_count = ref_params.get("n")
                    if isinstance(node_count, int):
                        derived_value = (
                            node_count - 1
                            if ref["template_id"].startswith("tree.")
                            else node_count
                        )
                    else:
                        node_min = ref_params.get("n_min")
                        node_max = ref_params.get("n_max")
                        if isinstance(node_min, int) and isinstance(node_max, int):
                            adjustment = -1 if ref["template_id"].startswith("tree.") else 0
                            derived_range = (node_min + adjustment, node_max + adjustment)
                if not supported_names and derived_value is None and derived_range is None and key in dimension_keys:
                    raise RecipeValidationError(
                        f"binding role {key} cannot be materialized by {ref['template_id']}",
                        path=f"{path}.{key}",
                    )
                if derived_value is not None and not minimum <= derived_value <= maximum:
                    raise RecipeValidationError(
                        f"derived {key} is outside contract range",
                        path=f"{path}.{key}",
                    )
                if derived_range is not None and not (
                    minimum <= derived_range[0] <= derived_range[1] <= maximum
                ):
                    raise RecipeValidationError(
                        f"derived {key} range is outside contract range",
                        path=f"{path}.{key}",
                    )
                for name in names:
                    value = ref_params.get(name)
                    if not isinstance(value, int):
                        continue
                    if name.endswith("_min") or name in {"lo", "label_min"}:
                        valid = minimum <= value <= maximum
                    elif name.endswith("_max") or name in {"hi", "label_max"}:
                        valid = minimum <= value <= maximum
                    else:
                        valid = minimum <= value <= maximum
                    if not valid:
                        raise RecipeValidationError(f"bound parameter {name} is outside contract range", path=f"{path}.{key}")
                if key in dimension_keys and case_kind in {"lower_bound", "upper_bound"}:
                    boundary = minimum if case_kind == "lower_bound" else maximum
                    if derived_value is not None and derived_value != boundary:
                        raise RecipeValidationError(
                            f"{profile}/{case_kind} derived {key} does not hit the contract boundary",
                            path=f"{path}.{key}",
                        )
                    if derived_range is not None and boundary not in derived_range:
                        raise RecipeValidationError(
                            f"{profile}/{case_kind} derived {key} range does not hit the contract boundary",
                            path=f"{path}.{key}",
                        )
                    exact_name = next((name for name in names if name in entry.parameters and not name.endswith(("_min", "_max"))), None)
                    if exact_name is not None:
                        prior = ref_params.get(exact_name)
                        if prior is not None and prior != boundary:
                            raise RecipeValidationError(f"{profile}/{case_kind} must use contract boundary for {key}", path=f"{path}.{key}")
                        ref_params[exact_name] = boundary
                    for name in names:
                        if name in entry.parameters and name.endswith(("_min", "_max")):
                            ref_params[name] = boundary
    for family_index, family in enumerate(families):
        structure = family["structure"]
        _repair_random_simple_graph_domain(structure, case_kind=case_kind)
        _validate_template_preconditions(
            structure["template_id"],
            structure["parameters"],
            path=f"{path}.family[{family_index}].structure",
        )
        for label_index, label in enumerate(family["labels"]):
            _validate_template_preconditions(
                label["template_id"],
                label["parameters"],
                path=f"{path}.family[{family_index}].labels[{label_index}]",
            )


def _normalize_parameter(name: str, value: Any, spec: Mapping[str, Any] | None, path: str) -> int | str:
    if not _SAFE_NAME.fullmatch(name) or (spec is None and name not in _PARAMETER_KEYS):
        raise RecipeValidationError(f"unknown or unsafe parameter: {name}", path=path)
    declared = spec or {}
    if "const" in declared:
        constant = declared["const"]
        if value != constant or type(value) is not type(constant):
            raise RecipeValidationError("parameter does not equal its catalog constant", path=path)
        if isinstance(value, str) and not _SAFE_ENUM.fullmatch(value):
            raise RecipeValidationError("catalog constant contains unsafe characters", path=path)
        if isinstance(value, bool):
            return int(value)
        if not isinstance(value, (str, int)):
            raise RecipeValidationError("unsupported catalog constant type", path=path)
        return value
    declared_type = str(declared.get("type") or "integer")
    if declared_type in {"int", "integer", "number"}:
        minimum = (spec or {}).get("minimum")
        maximum = (spec or {}).get("maximum")
        return _integer(
            value,
            path,
            minimum=int(minimum) if isinstance(minimum, int) else -(1 << 62),
            maximum=int(maximum) if isinstance(maximum, int) else (1 << 62),
        )
    if declared_type in {"uint64", "unsigned", "unsigned_integer"}:
        return _integer(value, path, minimum=0, maximum=(1 << 64) - 1)
    if declared_type in {"bool", "boolean"}:
        if isinstance(value, bool):
            return int(value)
        return _integer(value, path, minimum=0, maximum=1)
    if declared_type == "enum":
        if not isinstance(value, str) or not _SAFE_ENUM.fullmatch(value):
            raise RecipeValidationError("enum parameter contains unsafe characters", path=path)
        choices = (spec or {}).get("enum")
        if isinstance(choices, Sequence) and value not in choices:
            raise RecipeValidationError("enum parameter is not allowlisted", path=path)
        return value
    if declared_type == "string":
        if not isinstance(value, str):
            raise RecipeValidationError("string parameter must be a string", path=path)
        minimum_length = declared.get("minLength")
        maximum_length = declared.get("maxLength", 4096)
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            raise RecipeValidationError("string parameter is too short", path=path)
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            raise RecipeValidationError("string parameter is too long", path=path)
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise RecipeValidationError("string parameter contains control characters", path=path)
        return value
    raise RecipeValidationError(f"unsupported catalog parameter type: {declared_type}", path=path)


def _normalize_template_ref(value: Any, *, catalog: RecipeCatalog, path: str, expected_label: bool = False) -> dict[str, Any]:
    obj = _expect_mapping(value, path)
    _strict_keys(obj, required={"template_id", "parameters"}, path=path)
    template_id = obj.get("template_id")
    if not isinstance(template_id, str) or not _SAFE_ID.fullmatch(template_id):
        raise RecipeValidationError("invalid template_id", path=f"{path}.template_id")
    entry = catalog.templates.get(template_id)
    if entry is None:
        raise RecipeValidationError(f"unknown template_id: {template_id}", path=f"{path}.template_id")
    is_label = entry.kind in {"label", "labels", "edge_time"} or template_id.startswith(("label.", "edge_time."))
    if expected_label != is_label:
        expected = "label" if expected_label else "structure"
        raise RecipeValidationError(f"{template_id} is not a {expected} template", path=f"{path}.template_id")
    params_obj = _expect_mapping(obj.get("parameters"), f"{path}.parameters")
    unknown_params = sorted(set(params_obj) - set(entry.parameters)) if entry.parameters else sorted(set(params_obj) - _PARAMETER_KEYS)
    if unknown_params:
        raise RecipeValidationError(f"unknown template parameter(s): {', '.join(unknown_params)}", path=f"{path}.parameters")
    params = {
        name: _normalize_parameter(name, raw, entry.parameters.get(name), f"{path}.parameters.{name}")
        for name, raw in sorted(params_obj.items())
    }
    for name, spec in entry.parameters.items():
        if bool(spec.get("required")) and name not in params:
            raise RecipeValidationError(f"missing template parameter: {name}", path=f"{path}.parameters")
    _validate_template_preconditions(template_id, params, path=f"{path}.parameters")
    return {"template_id": template_id, "parameters": params}


def _validate_template_preconditions(template_id: str, params: Mapping[str, int | str], *, path: str) -> None:
    for lower_name, upper_name in (
        ("lo", "hi"),
        ("n_min", "n_max"),
        ("m_min", "m_max"),
        ("rows_min", "rows_max"),
        ("cols_min", "cols_max"),
        ("value_min", "value_max"),
        ("label_min", "label_max"),
    ):
        lower, upper = params.get(lower_name), params.get(upper_name)
        if isinstance(lower, int) and isinstance(upper, int) and lower > upper:
            raise RecipeValidationError(f"{lower_name} must not exceed {upper_name}", path=path)
    n = params.get("n")
    if isinstance(n, int):
        for count_name in ("run_count", "spine_length", "component_count"):
            count = params.get(count_name)
            if isinstance(count, int) and count > n:
                raise RecipeValidationError(f"{count_name} must not exceed n", path=path)
    sampled_n_min = params.get("n_min", params.get("n"))
    sampled_n_max = params.get("n_max", params.get("n"))
    if isinstance(sampled_n_min, int):
        run_count = params.get("run_count")
        if template_id in {"array.runs", "string.runs"} and isinstance(run_count, int) and run_count > sampled_n_min:
            raise RecipeValidationError("run_count must fit every sampled n", path=path)
        spine = params.get("spine_length", params.get("spine"))
        if template_id == "tree.caterpillar" and isinstance(spine, int) and spine > sampled_n_min:
            raise RecipeValidationError("spine length must fit every sampled n", path=path)
    if template_id == "string.permutation":
        pattern = params.get("pattern")
        if not isinstance(pattern, str) or not all(
            isinstance(item, int) and item == len(pattern)
            for item in (sampled_n_min, sampled_n_max)
        ):
            raise RecipeValidationError("string.permutation requires n_min=n_max=pattern length", path=path)
    if template_id in {"interval.nested", "interval.disjoint"}:
        lo = params.get("lo", params.get("value_min"))
        hi = params.get("hi", params.get("value_max"))
        if all(isinstance(item, int) for item in (sampled_n_max, lo, hi)):
            required = 2 * max(0, sampled_n_max - 1) if template_id == "interval.nested" else max(0, sampled_n_max - 1)
            if hi - lo < required:
                raise RecipeValidationError("interval endpoint domain is too small for the selected family", path=path)
    if template_id == "matrix.runs":
        rows_min = params.get("rows_min", params.get("rows"))
        cols_min = params.get("cols_min", params.get("cols"))
        run_count = params.get("run_count")
        if all(isinstance(item, int) for item in (rows_min, cols_min, run_count)) and run_count > rows_min * cols_min:
            raise RecipeValidationError("run_count must fit every sampled matrix", path=path)
    lo, hi = params.get("lo"), params.get("hi")
    if not isinstance(lo, int):
        lo = params.get("value_min")
    if not isinstance(hi, int):
        hi = params.get("value_max")
    value_count = params.get("value_count")
    if all(isinstance(item, int) for item in (lo, hi, value_count)) and value_count > hi - lo + 1:
        raise RecipeValidationError("value_count exceeds the declared value domain", path=path)
    if template_id == "string.few_chars":
        alphabet, distinct = params.get("alphabet"), params.get("distinct")
        if not isinstance(alphabet, str) or not isinstance(distinct, int) or distinct > len(alphabet):
            raise RecipeValidationError("distinct character count exceeds alphabet", path=path)
    if template_id == "string.equal":
        value = params.get("value")
        if not isinstance(value, str) or len(value) != 1:
            raise RecipeValidationError("string.equal value must be one character", path=path)
    if template_id.startswith("graph.") and template_id not in {
        "graph.self_loops", "graph.parallel_edges"
    }:
        graph_n_min = params.get("n_min", params.get("n"))
        graph_n_max = params.get("n_max", params.get("n"))
        m_min = params.get("m_min", params.get("m"))
        m_max = params.get("m_max", params.get("m"))
        if all(isinstance(item, int) for item in (graph_n_min, graph_n_max, m_min, m_max)):
            if graph_n_min < 0 or m_min < 0:
                raise RecipeValidationError("graph dimensions must be non-negative", path=path)
            # n and m are sampled independently by v1.  Validate the complete
            # Cartesian domain, not just its upper corner.
            maximum = graph_n_min * (graph_n_min - 1) // 2
            if m_max > maximum:
                raise RecipeValidationError("m range exceeds the simple undirected graph capacity", path=path)
            if template_id == "graph.connected" and (
                graph_n_min < 1 or m_min < graph_n_max - 1
            ):
                raise RecipeValidationError("connected graph domain requires m >= n-1 for every sampled pair", path=path)
        if template_id == "graph.components" and all(
            isinstance(item, int)
            for item in (graph_n_min, graph_n_max, m_min, m_max, params.get("component_count", params.get("components")))
        ):
            count = int(params.get("component_count", params.get("components")))
            if count < 1 or count > graph_n_min or m_min < graph_n_max - count:
                raise RecipeValidationError("component graph domain cannot realize the requested component count", path=path)
            q, r = divmod(graph_n_min, count)
            capacity = r * (q + 1) * q // 2 + (count - r) * q * (q - 1) // 2
            if m_max > capacity:
                raise RecipeValidationError("m range exceeds the component-preserving graph capacity", path=path)
    if template_id == "graph.bipartite":
        n_left, n_right = params.get("n_left"), params.get("n_right")
        m_max = params.get("m_max", params.get("m"))
        if all(isinstance(item, int) for item in (n_left, n_right, m_max)) and m_max > n_left * n_right:
            raise RecipeValidationError("m exceeds the simple bipartite edge limit", path=path)


def _validate_family_semantics(
    structure: Mapping[str, Any],
    labels: Sequence[Mapping[str, Any]],
    goal: str,
    *,
    path: str,
) -> None:
    structure_id = str(structure["template_id"])
    label_ids = {str(label["template_id"]) for label in labels}
    params = structure["parameters"]
    if labels and not (
        structure_id.startswith("tree.") or structure_id.startswith("graph.")
    ):
        raise RecipeValidationError(
            "v1 label policies apply only to graph/tree edge records", path=path
        )
    if label_ids.intersection({"label.distinct", "label.permutation"}):
        n_max = params.get("n_max", params.get("n"))
        m_max = params.get("m_max", params.get("m"))
        max_records: int | None = None
        if isinstance(n_max, int) and structure_id.startswith("tree."):
            max_records = max(0, n_max - 1)
        elif isinstance(n_max, int) and structure_id in {"graph.cycle", "graph.unicyclic"}:
            max_records = n_max
        elif isinstance(m_max, int):
            max_records = m_max
        label_ref = next(
            label
            for label in labels
            if label["template_id"] in {"label.distinct", "label.permutation"}
        )
        label_params = label_ref["parameters"]
        label_min = label_params.get("label_min", label_params.get("lo"))
        label_max = label_params.get("label_max", label_params.get("hi"))
        if not all(isinstance(item, int) for item in (max_records, label_min, label_max)):
            raise RecipeValidationError(
                "distinct labels require bounded record count and label domain", path=path
            )
        if max_records > label_max - label_min + 1:
            raise RecipeValidationError(
                "distinct label domain is smaller than the maximum record count", path=path
            )
    if goal == "self_loop" and structure_id != "graph.self_loops":
        raise RecipeValidationError("self_loop requires graph.self_loops", path=path)
    if goal == "parallel_edges" and structure_id != "graph.parallel_edges":
        raise RecipeValidationError("parallel_edges requires graph.parallel_edges", path=path)
    if goal == "single_vertex" and isinstance(params.get("n"), int) and params["n"] != 1:
        raise RecipeValidationError("single_vertex requires n=1", path=path)
    if goal == "connected" and not (
        structure_id.startswith("tree.")
        or structure_id in {"graph.connected", "graph.cycle", "graph.unicyclic"}
    ):
        raise RecipeValidationError("connected requires a connectivity-preserving structure", path=path)
    if goal in {"disconnected", "never_connects"} and structure_id != "graph.components":
        raise RecipeValidationError(f"{goal} requires graph.components", path=path)
    if goal == "one_cycle" and structure_id not in {"graph.cycle", "graph.unicyclic"}:
        raise RecipeValidationError("one_cycle requires graph.cycle or graph.unicyclic", path=path)
    if goal == "bipartite" and not (structure_id == "graph.bipartite" or structure_id.startswith("tree.")):
        raise RecipeValidationError("bipartite requires graph.bipartite or a tree", path=path)
    if goal == "acyclic" and not structure_id.startswith("tree."):
        raise RecipeValidationError("acyclic requires a tree template", path=path)
    if goal == "monotone" and not structure_id.endswith(".monotone"):
        raise RecipeValidationError("monotone requires a monotone template", path=path)
    if goal == "boundary_values" and not (
        structure_id.endswith(".extreme") or "label.extreme" in label_ids
    ):
        raise RecipeValidationError("boundary_values requires an extreme template or label policy", path=path)
    if goal == "duplicate_values" and not structure_id.endswith(
        (".equal", ".few_values", ".few_chars", ".runs", ".periodic")
    ):
        raise RecipeValidationError("duplicate_values requires a duplicate-producing template", path=path)
    if goal == "seed_variation":
        variable_range = any(
            isinstance(params.get(lower), int)
            and isinstance(params.get(upper), int)
            and params[lower] < params[upper]
            for lower, upper in (
                ("n_min", "n_max"), ("m_min", "m_max"),
                ("rows_min", "rows_max"), ("cols_min", "cols_max"),
            )
        )
        randomized = structure_id in {
            "array.uniform", "array.few_values", "array.monotone", "array.permutation", "array.extreme",
            "string.uniform", "string.few_chars", "string.monotone", "string.permutation", "string.extreme",
            "matrix.uniform", "matrix.few_values", "matrix.monotone", "matrix.permutation", "matrix.extreme",
            "interval.random", "interval.points", "interval.high_overlap", "interval.endpoint_heavy",
            "graph.random_simple", "graph.connected", "graph.components", "graph.bipartite", "graph.unicyclic",
            "graph.self_loops", "graph.parallel_edges", "tree.caterpillar", "tree.prufer", "tree.prim_biased",
        }
        if not (variable_range or randomized or "label.uniform" in label_ids):
            raise RecipeValidationError("seed_variation requires a seed-sensitive template domain", path=path)
    expected_structure = {
        "nested": "interval.nested",
        "disjoint": "interval.disjoint",
        "high_overlap": "interval.high_overlap",
        "endpoint_heavy": "interval.endpoint_heavy",
        "permutation": "array.permutation",
        "periodic": "array.periodic",
        "long_runs": "array.runs",
    }.get(goal)
    if expected_structure is not None and structure_id != expected_structure:
        raise RecipeValidationError(f"{goal} requires {expected_structure}", path=path)
    if goal == "equal_labels" and not label_ids.intersection({"label.equal"}):
        raise RecipeValidationError("equal_labels requires label.equal", path=path)
    if goal == "distinct_labels" and not label_ids.intersection({"label.distinct", "label.permutation"}):
        raise RecipeValidationError("distinct_labels requires a distinct label policy", path=path)
    if goal == "early_threshold_connects" and not (
        structure_id.startswith("tree.") and "label.equal" in label_ids
    ):
        raise RecipeValidationError("early_threshold_connects requires a tree with label.equal", path=path)
    if goal == "last_threshold_connects" and not (
        structure_id.startswith("tree.")
        and label_ids.intersection({"label.distinct", "label.permutation"})
    ):
        raise RecipeValidationError("last_threshold_connects requires a tree with distinct labels", path=path)


def validate_generator_recipe(
    value: Mapping[str, Any],
    *,
    contract: Mapping[str, Any] | None = None,
    catalog: RecipeCatalog | None = None,
    catalog_root: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and return a canonical provider-independent recipe value."""

    active_catalog = catalog or RecipeCatalog.load(catalog_root)
    # Contract-free validation/composition remains a supported low-level API:
    # callers may validate a recipe against the catalog alone.  Selection of
    # the static path, however, must always be fail-closed when no contract is
    # available (see ``supports_static_contract`` and ``generate_generator_recipe``).
    if contract is not None:
        supported, reason = supports_static_contract(contract, catalog=active_catalog)
        if not supported:
            raise UnsupportedRecipeError(reason or "unsupported_contract", path="$.contract")
    root = _expect_mapping(value, "$")
    _strict_keys(root, required={"schema_version", "engine", "cases"}, path="$")
    if root.get("schema_version") != GENERATOR_RECIPE_SCHEMA_VERSION or isinstance(root.get("schema_version"), bool):
        raise RecipeValidationError("schema_version must be 1", path="$.schema_version")
    if root.get("engine") != GENERATOR_RECIPE_ENGINE:
        raise RecipeValidationError("engine must be local_templates_v1", path="$.engine")
    raw_cases = root.get("cases")
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)) or len(raw_cases) != 4:
        raise RecipeValidationError("cases must contain exactly the four profile-v2 entries", path="$.cases")
    normalized_cases: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    targets = _contract_targets(contract)
    ranges = _contract_ranges(contract)
    for case_index, raw_case in enumerate(raw_cases):
        path = f"$.cases[{case_index}]"
        case = _expect_mapping(raw_case, path)
        _strict_keys(case, required={"profile", "case_kind", "families", "selection", "serialization", "byte_budget"}, path=path)
        profile = case.get("profile")
        case_kind = case.get("case_kind")
        pair = (profile, case_kind)
        if pair not in _SUPPORTED_CASES:
            raise RecipeValidationError(
                "unsupported profile/case_kind pair",
                path=path,
                details={"profile": profile, "case_kind": case_kind},
            )
        if pair in seen:
            raise RecipeValidationError("duplicate profile/case_kind pair", path=path)
        seen.add(pair)

        raw_families = case.get("families")
        if not isinstance(raw_families, Sequence) or isinstance(raw_families, (str, bytes)) or not 1 <= len(raw_families) <= 64:
            raise RecipeValidationError("families must contain 1..64 entries", path=f"{path}.families")
        families: list[dict[str, Any]] = []
        structure_kinds: set[str] = set()
        seen_semantic_goals: set[str] = set()
        for family_index, raw_family in enumerate(raw_families):
            family_path = f"{path}.families[{family_index}]"
            family = _expect_mapping(raw_family, family_path)
            _strict_keys(family, required={"structure", "labels", "semantic_goals"}, path=family_path)
            structure = _normalize_template_ref(family.get("structure"), catalog=active_catalog, path=f"{family_path}.structure")
            declared_profiles = active_catalog.templates[structure["template_id"]].metadata.get("profiles")
            if isinstance(declared_profiles, Sequence) and not isinstance(declared_profiles, (str, bytes)) and profile not in declared_profiles:
                raise RecipeValidationError("template does not support this profile", path=f"{family_path}.structure.template_id")
            structure_kind = _STRUCTURE_PREFIX_KINDS.get(structure["template_id"].split(".", 1)[0])
            if structure_kind is None:
                raise UnsupportedRecipeError("unsupported_static_structure", f"unsupported structure template: {structure['template_id']}", path=f"{family_path}.structure")
            structure_kinds.add(structure_kind)
            raw_labels = family.get("labels")
            if not isinstance(raw_labels, Sequence) or isinstance(raw_labels, (str, bytes)) or len(raw_labels) > 1:
                raise RecipeValidationError("v1 supports at most one label policy per family", path=f"{family_path}.labels")
            labels = [
                _normalize_template_ref(item, catalog=active_catalog, path=f"{family_path}.labels[{index}]", expected_label=True)
                for index, item in enumerate(raw_labels)
            ]
            raw_goals = family.get("semantic_goals")
            if not isinstance(raw_goals, Sequence) or isinstance(raw_goals, (str, bytes)) or len(raw_goals) != 1:
                raise RecipeValidationError("v1 requires exactly one semantic goal per family", path=f"{family_path}.semantic_goals")
            goals: list[str] = []
            for goal_index, goal in enumerate(raw_goals):
                if not isinstance(goal, str) or goal not in _SEMANTIC_GOALS:
                    raise RecipeValidationError(f"unknown semantic goal: {goal!r}", path=f"{family_path}.semantic_goals[{goal_index}]")
                if goal in goals:
                    raise RecipeValidationError("duplicate semantic goal", path=f"{family_path}.semantic_goals[{goal_index}]")
                if goal in seen_semantic_goals:
                    raise RecipeValidationError("semantic goals must be unique across families in one case", path=f"{family_path}.semantic_goals[{goal_index}]")
                goals.append(goal)
                seen_semantic_goals.add(goal)
            _validate_family_semantics(structure, labels, goals[0], path=family_path)
            if goals[0] == "lower_bound" and case_kind != "lower_bound":
                raise RecipeValidationError("lower_bound semantic belongs to lower_bound case", path=family_path)
            if goals[0] == "upper_bound" and case_kind != "upper_bound":
                raise RecipeValidationError("upper_bound semantic belongs to upper_bound case", path=family_path)
            families.append({"structure": structure, "labels": labels, "semantic_goals": goals})

        selection = _expect_mapping(case.get("selection"), f"{path}.selection")
        _strict_keys(selection, required={"policy", "seed_stride"}, optional={"schedule"}, path=f"{path}.selection")
        if selection.get("policy") != "balanced_round_robin_v1":
            raise RecipeValidationError("unsupported selection policy", path=f"{path}.selection.policy")
        stride = _integer(selection.get("seed_stride"), f"{path}.selection.seed_stride", minimum=1, maximum=1 << 31)
        expected_stride = 1 if profile == "small" else 5
        if stride != expected_stride:
            raise RecipeValidationError(f"{profile} seed_stride must be {expected_stride}", path=f"{path}.selection.seed_stride")

        serialization = _expect_mapping(case.get("serialization"), f"{path}.serialization")
        _strict_keys(serialization, required={"format_id"}, optional={"bindings"}, path=f"{path}.serialization")
        format_id = serialization.get("format_id")
        if not isinstance(format_id, str) or not _SAFE_ID.fullmatch(format_id.replace("_", ".")):
            raise RecipeValidationError("invalid serializer format_id", path=f"{path}.serialization.format_id")
        if format_id not in active_catalog.serializers:
            raise RecipeValidationError(f"unknown serializer: {format_id}", path=f"{path}.serialization.format_id")
        declared_serializer_kind = str(active_catalog.serializers.get(format_id, {}).get("kind") or "")
        serializer_kind = _SERIALIZER_KINDS.get(format_id) or ("" if declared_serializer_kind == "serializer" else declared_serializer_kind)
        if not serializer_kind:
            raise RecipeCatalogError(
                f"catalog serializer has no local structure-kind binding: {format_id}",
                path=f"$.serializers.{format_id}",
            )
        if serializer_kind and any(kind != serializer_kind for kind in structure_kinds):
            raise RecipeValidationError("serializer is incompatible with a structure family", path=f"{path}.serialization.format_id")
        weighted_edges = format_id == "edge_list_n_m_u_v_w"
        if weighted_edges and any(len(family["labels"]) != 1 for family in families):
            raise RecipeValidationError(
                "weighted edge serializer requires exactly one label policy per family",
                path=f"{path}.families",
            )
        if not weighted_edges and any(family["labels"] for family in families):
            raise RecipeValidationError(
                "unweighted serializer does not accept label policies",
                path=f"{path}.families",
            )
        bindings_obj = serialization.get("bindings", {})
        if not isinstance(bindings_obj, Mapping):
            raise RecipeValidationError("bindings must be an object", path=f"{path}.serialization.bindings")
        bindings: dict[str, str] = {}
        for name, target in sorted(bindings_obj.items()):
            if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
                raise RecipeValidationError("unsafe binding name", path=f"{path}.serialization.bindings")
            if name not in {"n", "m", "rows", "cols", "length", "value", "label"}:
                raise RecipeValidationError("unknown binding role", path=f"{path}.serialization.bindings.{name}")
            if not isinstance(target, str) or not _SAFE_TARGET.fullmatch(target):
                raise RecipeValidationError("binding target is not a field path", path=f"{path}.serialization.bindings.{name}")
            if contract is not None and target not in targets:
                raise RecipeValidationError(f"binding target is absent from contract: {target}", path=f"{path}.serialization.bindings.{name}")
            bindings[name] = target
        if contract is not None:
            used_targets = set(bindings.values())
            for required_key in _required_binding_keys(format_id):
                if required_key in bindings:
                    continue
                inferred = _infer_binding(required_key, ranges, used_targets)
                if inferred is None:
                    raise RecipeValidationError(f"cannot uniquely bind serializer dimension {required_key}", path=f"{path}.serialization.bindings")
                bindings[required_key] = inferred
                used_targets.add(inferred)
            _apply_contract_bindings(
                families,
                bindings,
                ranges,
                profile=profile,
                case_kind=case_kind,
                catalog=active_catalog,
                path=f"{path}.serialization.bindings",
            )

        budget = _expect_mapping(case.get("byte_budget"), f"{path}.byte_budget")
        # active_buckets is compiler-owned derived metadata.  Accept it only so
        # canonical recipes can be validated again, and deliberately ignore
        # the supplied value before recomputing it below.
        _strict_keys(budget, required={"hard_max", "buckets"}, optional={"active_buckets"}, path=f"{path}.byte_budget")
        policy_max = SMALL_RECIPE_HARD_MAX_BYTES if profile == "small" else LARGE_RECIPE_HARD_MAX_BYTES
        hard_max = _integer(budget.get("hard_max"), f"{path}.byte_budget.hard_max", minimum=1, maximum=policy_max)
        if hard_max != policy_max:
            raise RecipeValidationError(f"{profile} hard_max must equal policy limit {policy_max}", path=f"{path}.byte_budget.hard_max")
        raw_buckets = budget.get("buckets")
        if not isinstance(raw_buckets, Sequence) or isinstance(raw_buckets, (str, bytes)) or not raw_buckets:
            raise RecipeValidationError("buckets must be a non-empty array", path=f"{path}.byte_budget.buckets")
        buckets: list[list[int]] = []
        expected_low = 1
        for bucket_index, raw_bucket in enumerate(raw_buckets):
            bucket_path = f"{path}.byte_budget.buckets[{bucket_index}]"
            if not isinstance(raw_bucket, Sequence) or isinstance(raw_bucket, (str, bytes)) or len(raw_bucket) != 2:
                raise RecipeValidationError("bucket must be [minimum, maximum]", path=bucket_path)
            low = _integer(raw_bucket[0], f"{bucket_path}[0]", minimum=1, maximum=hard_max)
            high = _integer(raw_bucket[1], f"{bucket_path}[1]", minimum=low, maximum=hard_max)
            if low != expected_low:
                raise RecipeValidationError("buckets must be ordered, contiguous and start at 1", path=bucket_path)
            expected_low = high + 1
            buckets.append([low, high])
        if buckets[-1][1] != hard_max:
            raise RecipeValidationError("buckets must end at hard_max", path=f"{path}.byte_budget.buckets")
        active_buckets = _derive_active_buckets(families, format_id, buckets)
        if profile == "large" and case_kind == "upper_bound":
            oversized = [
                family["structure"]["template_id"]
                for family in families
                if _family_size_range(family, format_id)[1] > hard_max
            ]
            if oversized:
                raise RecipeValidationError(
                    "contract upper bound cannot be serialized within the 32 MiB policy limit",
                    path=f"{path}.byte_budget.hard_max",
                    details={"templates": oversized, "hard_max": hard_max},
                )
        if not active_buckets:
            raise RecipeValidationError("no byte bucket is reachable from the declared family bounds", path=f"{path}.byte_budget.buckets")
        schedule = _balanced_schedule(
            families,
            format_id,
            buckets,
            profile=profile,
            case_kind=case_kind,
        )
        normalized_cases.append(
            {
                "profile": profile,
                "case_kind": case_kind,
                "families": families,
                "selection": {
                    "policy": "balanced_round_robin_v1",
                    "seed_stride": stride,
                    "schedule": schedule,
                },
                "serialization": {"format_id": format_id, **({"bindings": bindings} if bindings else {})},
                "byte_budget": {
                    "hard_max": hard_max,
                    "buckets": buckets,
                    "active_buckets": active_buckets,
                },
            }
        )
    if seen != _SUPPORTED_CASES:
        raise RecipeValidationError("recipe must declare every profile-v2 case exactly once", path="$.cases")
    order = {pair: index for index, pair in enumerate((("small", "lower_bound"), ("small", "random"), ("large", "upper_bound"), ("large", "random")))}
    normalized_cases.sort(key=lambda item: order[(item["profile"], item["case_kind"])])
    return {"schema_version": 1, "engine": GENERATOR_RECIPE_ENGINE, "cases": normalized_cases}


def _cpp_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _cpp_int(params: Mapping[str, Any], name: str, default: int) -> int:
    value = params.get(name, default)
    return value if isinstance(value, int) else default


def _digits(value: int) -> int:
    return len(str(value))


def _family_size_range(family: Mapping[str, Any], serializer: str) -> tuple[int, int]:
    params = family["structure"]["parameters"]
    goals = family["semantic_goals"]
    exact_n = _cpp_int(params, "rows", _cpp_int(params, "n", 1 if "single_vertex" in goals else 2))
    n_lo = max(1, _cpp_int(params, "rows_min", _cpp_int(params, "n_min", exact_n)))
    n_hi = max(n_lo, _cpp_int(params, "rows_max", _cpp_int(params, "n_max", _cpp_int(params, "rows", _cpp_int(params, "n", 10)))))
    m_lo = max(0, _cpp_int(params, "m_min", _cpp_int(params, "m", max(0, n_lo - 1))))
    m_hi = max(m_lo, _cpp_int(params, "m_max", _cpp_int(params, "m", max(0, n_hi - 1))))
    v_lo = _cpp_int(params, "value_min", _cpp_int(params, "lo", -9))
    v_hi = max(v_lo, _cpp_int(params, "value_max", _cpp_int(params, "hi", 9)))
    value_digits = max(_digits(v_lo), _digits(v_hi))
    kind = _STRUCTURE_PREFIX_KINDS[family["structure"]["template_id"].split(".", 1)[0]]
    crlf_extra = 1 if os.name == "nt" else 0
    if kind == "scalar":
        return 2 + crlf_extra, value_digits + 1 + crlf_extra
    if kind == "array":
        header_lo = _digits(n_lo) + 1 if serializer == "list_n" else 0
        header_hi = _digits(n_hi) + 1 if serializer == "list_n" else 0
        newlines = 2 if serializer == "list_n" else 1
        return header_lo + n_lo * 2 + newlines * crlf_extra, header_hi + n_hi * (value_digits + 1) + newlines * crlf_extra
    if kind == "string":
        header_lo = _digits(n_lo) + 1 if serializer == "string_n" else 0
        header_hi = _digits(n_hi) + 1 if serializer == "string_n" else 0
        newlines = 2 if serializer == "string_n" else 1
        return header_lo + n_lo + 1 + newlines * crlf_extra, header_hi + n_hi + 1 + newlines * crlf_extra
    if kind == "matrix":
        cols_lo = max(1, _cpp_int(params, "cols_min", _cpp_int(params, "cols", m_lo or 1)))
        cols_hi = max(cols_lo, _cpp_int(params, "cols_max", _cpp_int(params, "cols", m_hi or n_hi)))
        return _digits(n_lo) + _digits(cols_lo) + 2 + n_lo * cols_lo * 2 + (n_lo + 1) * crlf_extra, _digits(n_hi) + _digits(cols_hi) + 2 + n_hi * cols_hi * (value_digits + 1) + (n_hi + 1) * crlf_extra
    if kind == "interval":
        return _digits(n_lo) + 1 + n_lo * 4 + (n_lo + 1) * crlf_extra, _digits(n_hi) + 1 + n_hi * (2 * value_digits + 2) + (n_hi + 1) * crlf_extra
    weighted = serializer.endswith("_w")
    vertex_digits = max(1, _digits(n_hi))
    label_digits = 1
    if family["labels"]:
        lp = family["labels"][0]["parameters"]
        label_digits = max(_digits(_cpp_int(lp, "label_min", 1)), _digits(_cpp_int(lp, "label_max", 100)))
    tree_serializer = serializer.startswith("tree_")
    header_lo = _digits(n_lo) + 1 + (0 if tree_serializer else _digits(m_lo) + 1)
    header_hi = _digits(n_hi) + 1 + (0 if tree_serializer else _digits(m_hi) + 1)
    edge_lo = 2 * vertex_digits + (label_digits + 1 if weighted else 0) + 2
    edge_hi = edge_lo
    return header_lo + m_lo * edge_lo + (m_lo + 1) * crlf_extra, header_hi + m_hi * edge_hi + (m_hi + 1) * crlf_extra


def _derive_active_buckets(families: Sequence[Mapping[str, Any]], serializer: str, buckets: Sequence[Sequence[int]]) -> list[list[int]]:
    ranges = [_family_size_range(family, serializer) for family in families]
    return [
        [int(bucket[0]), int(bucket[1])]
        for bucket in buckets
        if any(int(bucket[1]) >= low and int(bucket[0]) <= high for low, high in ranges)
    ]


def _balanced_schedule(
    families: Sequence[Mapping[str, Any]],
    serializer: str,
    buckets: Sequence[Sequence[int]],
    *,
    profile: str,
    case_kind: str,
) -> list[dict[str, int]]:
    ranges = [_family_size_range(family, serializer) for family in families]
    feasible = [
        [
            index
            for index, (bucket_low, bucket_high) in enumerate(buckets)
            if int(bucket_high) >= low and int(bucket_low) <= high
        ]
        for low, high in ranges
    ]
    if any(not choices for choices in feasible):
        raise RecipeValidationError("a family has no reachable byte bucket")
    if profile == "small" and case_kind == "random":
        if len(families) > 16:
            raise RecipeValidationError("small/random supports at most 16 families")
        slots = 16
    else:
        slots = max(1, len(families))
    if profile == "small" and case_kind == "random":
        reachable = sorted({index for choices in feasible for index in choices})
        if not reachable:
            raise RecipeValidationError("small/random has no schedulable byte bucket")
        base, extra = divmod(slots, len(reachable))

        # The old one-pass greedy could paint itself into a corner: an early
        # flexible family consumed a scarce bucket needed by a later family,
        # then a perfectly feasible 16-slot balanced schedule was rejected.
        # Enumerate the at-most-four policy bucket target distributions and use
        # memoized exact assignment.  This is bounded local planning, not
        # provider-controlled search.
        schedule: list[dict[str, int]] | None = None
        for elevated in combinations(reachable, extra):
            target = [0] * len(buckets)
            elevated_set = set(elevated)
            for bucket_index in reachable:
                target[bucket_index] = base + int(bucket_index in elevated_set)
            memo: set[tuple[int, tuple[int, ...]]] = set()

            def assign(slot: int, counts: tuple[int, ...]) -> list[int] | None:
                state = (slot, counts)
                if state in memo:
                    return None
                if slot == slots:
                    return [] if list(counts) == target else None
                family_index = slot % len(families)
                choices = sorted(
                    (
                        bucket_index
                        for bucket_index in feasible[family_index]
                        if counts[bucket_index] < target[bucket_index]
                    ),
                    key=lambda bucket_index: (
                        target[bucket_index] - counts[bucket_index],
                        -bucket_index,
                    ),
                    reverse=True,
                )
                for bucket_index in choices:
                    next_counts = list(counts)
                    next_counts[bucket_index] += 1
                    suffix = assign(slot + 1, tuple(next_counts))
                    if suffix is not None:
                        return [bucket_index, *suffix]
                memo.add(state)
                return None

            assigned = assign(0, tuple(0 for _ in buckets))
            if assigned is not None:
                schedule = [
                    {
                        "family_index": slot % len(families),
                        "bucket_index": bucket_index,
                    }
                    for slot, bucket_index in enumerate(assigned)
                ]
                break
        if schedule is None:
            # Some semantically distinct fixed-size families make perfect byte
            # balance mathematically impossible (for example, two mandatory
            # tiny cases and six medium cases, each repeated equally).  Keep
            # family balance as the hard invariant, cover every reachable
            # bucket, and choose the minimum-spread feasible assignment instead
            # of rejecting the whole static path.
            states: dict[tuple[int, ...], list[int]] = {
                tuple(0 for _ in buckets): []
            }
            for slot in range(slots):
                family_index = slot % len(families)
                next_states: dict[tuple[int, ...], list[int]] = {}
                for counts, assigned in states.items():
                    for bucket_index in feasible[family_index]:
                        next_counts = list(counts)
                        next_counts[bucket_index] += 1
                        key = tuple(next_counts)
                        next_states.setdefault(key, [*assigned, bucket_index])
                states = next_states
            candidates = [
                (counts, assigned)
                for counts, assigned in states.items()
                if all(counts[bucket_index] > 0 for bucket_index in reachable)
            ]
            if not candidates:
                raise RecipeValidationError("small/random has no schedulable byte bucket")
            counts, assigned = min(
                candidates,
                key=lambda item: (
                    max(item[0][index] for index in reachable)
                    - min(item[0][index] for index in reachable),
                    sum(item[0][index] ** 2 for index in reachable),
                    item[0],
                ),
            )
            schedule = [
                {
                    "family_index": slot % len(families),
                    "bucket_index": bucket_index,
                }
                for slot, bucket_index in enumerate(assigned)
            ]
        family_counts = [0] * len(families)
        for item in schedule:
            family_counts[item["family_index"]] += 1
        if min(family_counts) < 1 or max(family_counts) - min(family_counts) > 1:
            raise RecipeValidationError("small/random families cannot be balanced in 16 seeds")
        return schedule

    counts = [0] * len(buckets)
    schedule = []
    for slot in range(slots):
        family_index = slot % len(families)
        choices = feasible[family_index]
        bucket_index = min(
            choices,
            key=lambda index: (counts[index], (index - slot) % len(buckets)),
        )
        counts[bucket_index] += 1
        schedule.append({"family_index": family_index, "bucket_index": bucket_index})
    return schedule


def _emit_family(family: Mapping[str, Any], serializer: str, *, small: bool, family_count: int, family_index: int) -> str:
    structure = family["structure"]
    template_id = structure["template_id"]
    params = structure["parameters"]
    bipartite_n = _cpp_int(params, "n_left", 0) + _cpp_int(params, "n_right", 0)
    exact_n = _cpp_int(params, "rows", _cpp_int(params, "n", bipartite_n or (1 if "single_vertex" in family["semantic_goals"] else 2)))
    n_lo = max(1, _cpp_int(params, "rows_min", _cpp_int(params, "n_min", exact_n)))
    n_hi_default = 10 if small else 1000
    n_hi = max(n_lo, _cpp_int(params, "rows_max", _cpp_int(params, "n_max", _cpp_int(params, "rows", _cpp_int(params, "n", n_hi_default)))))
    exact_m = _cpp_int(params, "cols", _cpp_int(params, "m", max(0, n_lo - 1)))
    m_lo = max(0, _cpp_int(params, "cols_min", _cpp_int(params, "m_min", exact_m)))
    m_hi = max(m_lo, _cpp_int(params, "cols_max", _cpp_int(params, "m_max", _cpp_int(params, "cols", _cpp_int(params, "m", max(0, n_hi - 1))))))
    v_lo = _cpp_int(params, "value", _cpp_int(params, "value_min", _cpp_int(params, "lo", -9)))
    v_hi = max(v_lo, _cpp_int(params, "value", _cpp_int(params, "value_max", _cpp_int(params, "hi", 9))))
    index_base = _cpp_int(params, "index_base", _cpp_int(params, "base", 1))
    prefix = template_id.split(".", 1)[0]
    variant = template_id.split(".", 1)[1]
    aux1 = {
        "few_values": _cpp_int(params, "value_count", 3),
        "few_chars": _cpp_int(params, "distinct", 2),
        "periodic": _cpp_int(params, "period", 3),
        "runs": _cpp_int(params, "run_count", 2),
        "kary": _cpp_int(params, "k", 2),
        "caterpillar": _cpp_int(params, "spine_length", _cpp_int(params, "spine", max(1, n_lo // 2))),
        "prim_biased": _cpp_int(params, "elongation", 70),
        "components": _cpp_int(params, "component_count", _cpp_int(params, "components", 2)),
        "bipartite": _cpp_int(params, "n_left", max(1, n_lo // 2)),
    }.get(variant, 0)
    aux2 = _cpp_int(params, "n_right", max(0, n_lo - aux1)) if variant == "bipartite" else _cpp_int(params, "nondecreasing", 1)
    text_param = str(
        params.get("alphabet")
        or params.get("pattern")
        or params.get("value")
        or "abc"
    )
    label_policy = family["labels"][0]["template_id"].split(".", 1)[-1] if family["labels"] else "uniform"
    label_params = family["labels"][0]["parameters"] if family["labels"] else {}
    label_lo = _cpp_int(label_params, "label_min", _cpp_int(label_params, "value_min", _cpp_int(label_params, "lo", 1)))
    label_hi = max(label_lo, _cpp_int(label_params, "label_max", _cpp_int(label_params, "value_max", _cpp_int(label_params, "hi", 100))))
    return (
        "{\n"
        f"  const std::string structure_id={_cpp_string(template_id)};\n"
        f"  const std::string variant={_cpp_string(variant)};\n"
        f"  const std::string serializer={_cpp_string(serializer)};\n"
        f"  const std::string label_policy={_cpp_string(label_policy)};\n"
        f"  const long long n_lo={n_lo}LL,n_hi={n_hi}LL,m_lo={m_lo}LL,m_hi={m_hi}LL;\n"
        f"  const long long value_lo={v_lo}LL,value_hi={v_hi}LL,label_lo={label_lo}LL,label_hi={label_hi}LL,index_base={index_base}LL;\n"
        f"  const long long aux1={aux1}LL,aux2={aux2}LL; const std::string text_param={_cpp_string(text_param)};\n"
        f"  data=acm_recipe_local::render({_cpp_string(prefix)},variant,serializer,label_policy,n_lo,n_hi,m_lo,m_hi,value_lo,value_hi,label_lo,label_hi,index_base,aux1,aux2,text_param,rng,bucket_low,bucket_high);\n"
        f"  family_tag={_cpp_string('family:' + template_id + '#' + str(family_index))};\n"
        "  static const char* goals[]={"
        + ",".join(_cpp_string("semantic:" + goal) for goal in family["semantic_goals"])
        + "};\n"
        f"  semantic_tag=goals[(round/{family_count}ULL)%{len(family['semantic_goals'])}ULL];\n"
        "}"
    )


_LOCAL_CPP_RUNTIME = r'''
namespace acm_recipe_local {
struct Rng {
    unsigned long long state;
    explicit Rng(unsigned long long seed): state(seed) {}
    unsigned long long next() {
        unsigned long long z=(state+=0x9e3779b97f4a7c15ULL);
        z=(z^(z>>30))*0xbf58476d1ce4e5b9ULL;
        z=(z^(z>>27))*0x94d049bb133111ebULL;
        return z^(z>>31);
    }
    unsigned long long bounded(unsigned long long n) {
        if (!n) return 0;
        unsigned long long threshold=(0ULL-n)%n;
        for (;;) { auto x=next(); if (x>=threshold) return x%n; }
    }
    long long between(long long lo,long long hi) {
        if (hi<=lo) return lo;
        return lo+static_cast<long long>(bounded(static_cast<unsigned long long>(hi-lo)+1));
    }
};
struct Edge { long long u,v,w; };
static std::size_t wire_size(const std::string& value) {
#ifdef _WIN32
    return value.size()+static_cast<std::size_t>(std::count(value.begin(),value.end(),'\n'));
#else
    return value.size();
#endif
}
static void shuffle_edges(std::vector<Edge>& a,Rng& rng) {
    for (std::size_t i=a.size();i>1;--i) std::swap(a[i-1],a[rng.bounded(i)]);
}
static std::vector<long long> sample_without_replacement(long long universe,long long count,Rng& rng) {
    if(universe<0 || count<0 || count>universe) throw std::invalid_argument("invalid sample size");
    const bool complement=count>universe/2;
    const long long selected_count=complement?universe-count:count;
    std::set<long long> selected;
    for(long long j=universe-selected_count;j<universe;++j){
        long long candidate=static_cast<long long>(rng.bounded(static_cast<unsigned long long>(j)+1));
        if(!selected.insert(candidate).second)selected.insert(j);
    }
    std::vector<long long> result;result.reserve(static_cast<std::size_t>(count));
    if(!complement)result.assign(selected.begin(),selected.end());
    else{
        auto excluded=selected.begin();
        for(long long rank=0;rank<universe;++rank){
            if(excluded!=selected.end() && *excluded==rank)++excluded;
            else result.push_back(rank);
        }
    }
    for(std::size_t i=result.size();i>1;--i)std::swap(result[i-1],result[rng.bounded(i)]);
    return result;
}
static long long simple_edge_capacity(long long n){return n*(n-1)/2;}
static std::pair<long long,long long> simple_edge_from_rank(long long n,long long rank){
    if(rank<0 || rank>=simple_edge_capacity(n))throw std::invalid_argument("invalid simple edge rank");
    long long lo=0,hi=n-1;
    while(lo+1<hi){long long mid=lo+(hi-lo)/2,prefix=mid*(2*n-mid-1)/2;if(prefix<=rank)lo=mid;else hi=mid;}
    long long prefix=lo*(2*n-lo-1)/2;return {lo,lo+1+(rank-prefix)};
}
static long long simple_edge_rank(long long n,long long u,long long v){
    if(u>v)std::swap(u,v);if(u<0 || v>=n || u==v)throw std::invalid_argument("invalid simple edge");
    return u*(2*n-u-1)/2+(v-u-1);
}
static long long rank_excluding_sorted(long long allowed,long long universe,const std::vector<long long>& excluded){
    if(allowed<0 || allowed>=universe-static_cast<long long>(excluded.size()))throw std::invalid_argument("invalid allowed edge rank");
    std::size_t lo=0,hi=excluded.size();
    while(lo<hi){
        std::size_t mid=lo+(hi-lo)/2;
        if(excluded[mid]-static_cast<long long>(mid)<=allowed)lo=mid+1;else hi=mid;
    }
    return allowed+static_cast<long long>(lo);
}
static std::vector<Edge> make_edges(const std::string& prefix,const std::string& variant,long long n,long long m,long long base,long long aux1,long long aux2,Rng& rng) {
    std::vector<Edge> e;
    auto add=[&](long long u,long long v){ e.push_back({u+base,v+base,0}); };
    if(variant=="self_loops"){add(rng.bounded(std::max(1LL,n)),rng.bounded(std::max(1LL,n)));e[0].v=e[0].u;while(static_cast<long long>(e.size())<m)add(rng.bounded(n),rng.bounded(n));return e;}
    if (n<=1) return e;
    if(variant=="parallel_edges"){long long u=rng.bounded(n),v=rng.bounded(n-1);if(v>=u)++v;add(u,v);add(u,v);while(static_cast<long long>(e.size())<m){u=rng.bounded(n);v=rng.bounded(n-1);if(v>=u)++v;add(u,v);}return e;}
    if (prefix=="tree") {
        if (variant=="star") for(long long v=1;v<n;++v) add(0,v);
        else if (variant=="binary") for(long long v=1;v<n;++v) add((v-1)/2,v);
        else if (variant=="kary") { long long k=std::max(1LL,aux1); for(long long v=1;v<n;++v) add((v-1)/k,v); }
        else if (variant=="caterpillar") { long long spine=std::max(1LL,std::min(n,aux1)); for(long long v=1;v<spine;++v)add(v-1,v); for(long long v=spine;v<n;++v)add(rng.bounded(spine),v); }
        else if (variant=="prim_biased") { long long elongation=std::max(0LL,std::min(100LL,aux1));for(long long v=1;v<n;++v){long long parent=rng.bounded(100)<elongation?v-1:rng.bounded(v);add(parent,v);} }
        else if (variant=="prufer" && n>2) {
            std::vector<long long> degree(n,1),code(n-2); for(auto& x:code){x=rng.bounded(n);++degree[x];}
            for(long long x:code){long long leaf=0;while(degree[leaf]!=1)++leaf;add(leaf,x);--degree[leaf];--degree[x];}
            long long a=-1,b=-1;for(long long i=0;i<n;++i)if(degree[i]==1){if(a<0)a=i;else b=i;}add(a,b);
        } else { for(long long v=1;v<n;++v)add(v-1,v); }
        return e;
    }
    if (variant=="cycle" || variant=="unicyclic") {
        for(long long v=1;v<n;++v)add(v-1,v); add(n-1,0);
    } else if (variant=="bipartite") {
        long long left=std::max(0LL,std::min(n,aux1)),right=std::max(0LL,std::min(n-left,aux2));if(left+right!=n){left=n/2;right=n-left;}
        for(long long rank:sample_without_replacement(left*right,m,rng))add(rank/right,left+rank%right);
    } else if (variant=="components") {
        long long count=std::max(1LL,std::min(n,aux1));std::vector<long long> first(count),size(count,n/count);for(long long i=0;i<n%count;++i)++size[i];for(long long i=1;i<count;++i)first[i]=first[i-1]+size[i-1];
        std::vector<long long> offsets(count+1),tree_ranks;
        for(long long c=0;c<count;++c){offsets[c+1]=offsets[c]+simple_edge_capacity(size[c]);for(long long x=1;x<size[c];++x){add(first[c]+x-1,first[c]+x);tree_ranks.push_back(offsets[c]+simple_edge_rank(size[c],x-1,x));}}
        std::sort(tree_ranks.begin(),tree_ranks.end());
        long long capacity=offsets.back();
        for(long long allowed:sample_without_replacement(capacity-static_cast<long long>(tree_ranks.size()),m-static_cast<long long>(tree_ranks.size()),rng)){
            long long rank=rank_excluding_sorted(allowed,capacity,tree_ranks);
            long long c=static_cast<long long>(std::upper_bound(offsets.begin(),offsets.end(),rank)-offsets.begin())-1;
            auto edge=simple_edge_from_rank(size[c],rank-offsets[c]);add(first[c]+edge.first,first[c]+edge.second);
        }
    } else if (variant=="random_simple") {
        for(long long rank:sample_without_replacement(simple_edge_capacity(n),m,rng)){auto edge=simple_edge_from_rank(n,rank);add(edge.first,edge.second);}
    } else {
        for(long long v=1;v<n && static_cast<long long>(e.size())<m;++v)add(rng.bounded(v),v);
        std::vector<long long> tree_ranks;tree_ranks.reserve(e.size());for(auto edge:e)tree_ranks.push_back(simple_edge_rank(n,edge.u-base,edge.v-base));std::sort(tree_ranks.begin(),tree_ranks.end());
        long long capacity=simple_edge_capacity(n);
        for(long long allowed:sample_without_replacement(capacity-static_cast<long long>(tree_ranks.size()),m-static_cast<long long>(tree_ranks.size()),rng)){long long rank=rank_excluding_sorted(allowed,capacity,tree_ranks);auto edge=simple_edge_from_rank(n,rank);add(edge.first,edge.second);}
    }
    if (static_cast<long long>(e.size())>m && variant!="unicyclic" && variant!="cycle") e.resize(static_cast<std::size_t>(m));
    return e;
}
static void label(std::vector<Edge>& e,const std::string& policy,long long lo,long long hi,Rng& rng) {
    for(std::size_t i=0;i<e.size();++i){
        if(policy=="constant" || policy=="equal") e[i].w=lo;
        else if(policy=="distinct" || policy=="permutation") e[i].w=lo+static_cast<long long>(i);
        else if(policy=="monotone") e[i].w=std::min(hi,lo+static_cast<long long>(i));
        else if(policy=="extremes" || policy=="extreme") e[i].w=(i&1)?hi:lo;
        else if(policy=="layered") e[i].w=lo+static_cast<long long>(i%std::max(1LL,std::min(4LL,hi-lo+1)));
        else e[i].w=rng.between(lo,hi);
    }
}
static std::string render_once(const std::string& prefix,const std::string& variant,const std::string& serializer,const std::string& label_policy,long long n,long long m,long long value_lo,long long value_hi,long long label_lo,long long label_hi,long long base,long long aux1,long long aux2,const std::string& text_param,Rng& rng) {
    std::ostringstream out;
    if(prefix=="scalar") { out<<rng.between(value_lo,value_hi)<<'\n'; }
    else if(prefix=="array" || prefix=="list") {
        std::vector<long long>a(static_cast<std::size_t>(n)); for(long long i=0;i<n;++i)a[i]=rng.between(value_lo,value_hi);
        if(variant=="constant" || variant=="equal")std::fill(a.begin(),a.end(),value_lo);
        else if(variant=="permutation"){for(long long i=0;i<n;++i)a[i]=base+i;for(std::size_t i=a.size();i>1;--i)std::swap(a[i-1],a[rng.bounded(i)]);}
        else if(variant=="monotone"){std::sort(a.begin(),a.end());if(!aux2)std::reverse(a.begin(),a.end());}
        else if(variant=="periodic")for(long long i=0;i<n;++i)a[i]=value_lo+(i%std::max(1LL,aux1))%std::max(1LL,value_hi-value_lo+1);
        else if(variant=="long_runs" || variant=="runs")for(long long i=0;i<n;++i)a[i]=value_lo+(i*std::max(1LL,aux1)/std::max(1LL,n))%std::max(1LL,value_hi-value_lo+1);
        else if(variant=="extremes" || variant=="extreme")for(long long i=0;i<n;++i)a[i]=(i&1)?value_hi:value_lo;
        else if(variant=="few_values")for(long long i=0;i<n;++i)a[i]=value_lo+i%std::max(1LL,std::min(aux1,value_hi-value_lo+1));
        if(serializer=="list_n")out<<n<<'\n'; for(std::size_t i=0;i<a.size();++i)out<<a[i]<<(i+1==a.size()?'\n':' ');
    } else if(prefix=="string") {
        const std::string alphabet=text_param.empty()?std::string("a"):text_param;std::string s(static_cast<std::size_t>(n),alphabet.front()); for(long long i=0;i<n;++i)s[i]=alphabet[rng.bounded(alphabet.size())];
        if(variant=="constant" || variant=="equal")std::fill(s.begin(),s.end(),alphabet.front());
        else if(variant=="few_chars"){long long used=std::max(1LL,std::min(aux1,static_cast<long long>(alphabet.size())));for(long long i=0;i<n;++i)s[i]=alphabet[rng.bounded(used)];}
        else if(variant=="monotone"){std::sort(s.begin(),s.end());if(!aux2)std::reverse(s.begin(),s.end());}
        else if(variant=="permutation"){s=alphabet;for(std::size_t i=s.size();i>1;--i)std::swap(s[i-1],s[rng.bounded(i)]);}
        else if(variant=="extreme"){auto bounds=std::minmax_element(alphabet.begin(),alphabet.end());for(long long i=0;i<n;++i)s[i]=rng.bounded(2)?*bounds.first:*bounds.second;}
        else if(variant=="periodic")for(long long i=0;i<n;++i)s[i]=alphabet[i%alphabet.size()]; else if(variant=="long_runs" || variant=="runs")for(long long i=0;i<n;++i)s[i]=alphabet[(i*std::max(1LL,aux1)/std::max(1LL,n))%alphabet.size()];
        if(serializer=="string_n")out<<n<<'\n';out<<s<<'\n';
    } else if(prefix=="matrix") {
        long long rows=n,cols=std::max(1LL,m),count=rows*cols;std::vector<long long>a(static_cast<std::size_t>(count));for(long long i=0;i<count;++i)a[i]=rng.between(value_lo,value_hi);
        if(variant=="constant" || variant=="equal")std::fill(a.begin(),a.end(),value_lo);
        else if(variant=="few_values")for(long long i=0;i<count;++i)a[i]=value_lo+i%std::max(1LL,std::min(aux1,value_hi-value_lo+1));
        else if(variant=="monotone"){std::sort(a.begin(),a.end());if(!aux2)std::reverse(a.begin(),a.end());}
        else if(variant=="permutation"){for(long long i=0;i<count;++i)a[i]=base+i;for(std::size_t i=a.size();i>1;--i)std::swap(a[i-1],a[rng.bounded(i)]);}
        else if(variant=="periodic")for(long long i=0;i<count;++i)a[i]=value_lo+(i%std::max(1LL,aux1))%std::max(1LL,value_hi-value_lo+1);
        else if(variant=="runs")for(long long i=0;i<count;++i)a[i]=value_lo+(i*std::max(1LL,aux1)/std::max(1LL,count))%std::max(1LL,value_hi-value_lo+1);
        else if(variant=="extreme")for(long long i=0;i<count;++i)a[i]=rng.bounded(2)?value_lo:value_hi;
        out<<rows<<' '<<cols<<'\n';for(long long i=0;i<rows;++i)for(long long j=0;j<cols;++j)out<<a[static_cast<std::size_t>(i*cols+j)]<<(j+1==cols?'\n':' ');
    } else if(prefix=="interval") {
        out<<n<<'\n';for(long long i=0;i<n;++i){long long l=value_lo,r=value_hi;if(variant=="point" || variant=="points")r=l=rng.between(value_lo,value_hi);else if(variant=="disjoint"){l=value_lo+i;r=l;}else if(variant=="nested"){l=value_lo+i;r=value_hi-i;if(l>r)l=r;}else if(variant=="high_overlap"){long long center=value_lo+(value_hi-value_lo)/2;l=rng.between(value_lo,center);r=rng.between(center,value_hi);}else if(variant=="endpoint_heavy"){l=(i&1)?value_lo:rng.between(value_lo,value_hi);r=(i&2)?value_hi:rng.between(l,value_hi);}else{l=rng.between(value_lo,value_hi);r=rng.between(l,value_hi);}out<<l<<' '<<r<<'\n';}
    } else {
        auto e=make_edges(prefix,variant,n,m,base,aux1,aux2,rng);label(e,label_policy,label_lo,label_hi,rng);shuffle_edges(e,rng);
        bool tree_serializer=serializer.rfind("tree_",0)==0;out<<n;if(!tree_serializer)out<<' '<<e.size();out<<'\n';bool weighted=serializer.size()>=2 && serializer.substr(serializer.size()-2)=="_w";for(auto x:e){out<<x.u<<' '<<x.v;if(weighted)out<<' '<<x.w;out<<'\n';}
    }
    return out.str();
}
static std::string render(const std::string& prefix,const std::string& variant,const std::string& serializer,const std::string& label_policy,long long n_lo,long long n_hi,long long m_lo,long long m_hi,long long value_lo,long long value_hi,long long label_lo,long long label_hi,long long base,long long aux1,long long aux2,const std::string& text_param,Rng& rng,std::size_t bucket_low,std::size_t bucket_high) {
    std::string nearest;std::size_t nearest_distance=std::numeric_limits<std::size_t>::max();
    // A bucket is a diversity target, not a validity condition.  Bound full
    // materializations tightly: exercise both dimension extremes first, then
    // a small seeded sample.  This preserves deterministic output per seed
    // without allowing an unreachable 32 MiB bucket to allocate 4096 times.
    const int attempt_limit=bucket_high<=ACM_SMALL_RECIPE_MAX_BYTES?ACM_SMALL_RECIPE_BUCKET_ATTEMPTS:ACM_LARGE_RECIPE_BUCKET_ATTEMPTS;
    for(int attempt=0;attempt<attempt_limit;++attempt){long long n=attempt==0?n_lo:(attempt==1?n_hi:rng.between(n_lo,n_hi)),m=attempt==0?m_lo:(attempt==1?m_hi:rng.between(m_lo,m_hi));auto data=render_once(prefix,variant,serializer,label_policy,n,m,value_lo,value_hi,label_lo,label_hi,base,aux1,aux2,text_param,rng);const auto size=wire_size(data);if(size>=bucket_low&&size<=bucket_high)return data;const auto distance=size<bucket_low?bucket_low-size:size-bucket_high;if(nearest.empty()||distance<nearest_distance){nearest=std::move(data);nearest_distance=distance;}}
    // Size estimates intentionally over-approximate variable-width integers
    // and correlated structure parameters.  A byte bucket is a diversity
    // target, never an input-correctness condition: retain the closest legal
    // rendering and let the manifest report its actual bucket.
    return nearest;
}
}
'''

_LOCAL_CPP_RUNTIME = _LOCAL_CPP_RUNTIME.replace(
    "ACM_SMALL_RECIPE_BUCKET_ATTEMPTS", str(SMALL_RECIPE_BUCKET_RENDER_ATTEMPTS)
).replace(
    "ACM_LARGE_RECIPE_BUCKET_ATTEMPTS", str(LARGE_RECIPE_BUCKET_RENDER_ATTEMPTS)
).replace(
    "ACM_SMALL_RECIPE_MAX_BYTES", str(SMALL_RECIPE_HARD_MAX_BYTES)
)

_SHA256_CPP = r'''
namespace acm_recipe_sha256 {
static inline std::uint32_t rotr(std::uint32_t x, unsigned n) { return (x >> n) | (x << (32U - n)); }
struct Hash {
    std::uint32_t h[8] = {0x6a09e667U,0xbb67ae85U,0x3c6ef372U,0xa54ff53aU,0x510e527fU,0x9b05688cU,0x1f83d9abU,0x5be0cd19U};
    unsigned char block[64] = {};
    std::size_t used = 0;
    std::uint64_t bytes = 0;
    void compress() {
        static const std::uint32_t k[64] = {
            0x428a2f98U,0x71374491U,0xb5c0fbcfU,0xe9b5dba5U,0x3956c25bU,0x59f111f1U,0x923f82a4U,0xab1c5ed5U,
            0xd807aa98U,0x12835b01U,0x243185beU,0x550c7dc3U,0x72be5d74U,0x80deb1feU,0x9bdc06a7U,0xc19bf174U,
            0xe49b69c1U,0xefbe4786U,0x0fc19dc6U,0x240ca1ccU,0x2de92c6fU,0x4a7484aaU,0x5cb0a9dcU,0x76f988daU,
            0x983e5152U,0xa831c66dU,0xb00327c8U,0xbf597fc7U,0xc6e00bf3U,0xd5a79147U,0x06ca6351U,0x14292967U,
            0x27b70a85U,0x2e1b2138U,0x4d2c6dfcU,0x53380d13U,0x650a7354U,0x766a0abbU,0x81c2c92eU,0x92722c85U,
            0xa2bfe8a1U,0xa81a664bU,0xc24b8b70U,0xc76c51a3U,0xd192e819U,0xd6990624U,0xf40e3585U,0x106aa070U,
            0x19a4c116U,0x1e376c08U,0x2748774cU,0x34b0bcb5U,0x391c0cb3U,0x4ed8aa4aU,0x5b9cca4fU,0x682e6ff3U,
            0x748f82eeU,0x78a5636fU,0x84c87814U,0x8cc70208U,0x90befffaU,0xa4506cebU,0xbef9a3f7U,0xc67178f2U};
        std::uint32_t w[64];
        for(int i=0;i<16;++i) w[i]=(static_cast<std::uint32_t>(block[4*i])<<24)|(static_cast<std::uint32_t>(block[4*i+1])<<16)|(static_cast<std::uint32_t>(block[4*i+2])<<8)|block[4*i+3];
        for(int i=16;i<64;++i){std::uint32_t s0=rotr(w[i-15],7)^rotr(w[i-15],18)^(w[i-15]>>3);std::uint32_t s1=rotr(w[i-2],17)^rotr(w[i-2],19)^(w[i-2]>>10);w[i]=w[i-16]+s0+w[i-7]+s1;}
        std::uint32_t a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],hh=h[7];
        for(int i=0;i<64;++i){std::uint32_t s1=rotr(e,6)^rotr(e,11)^rotr(e,25);std::uint32_t ch=(e&f)^((~e)&g);std::uint32_t t1=hh+s1+ch+k[i]+w[i];std::uint32_t s0=rotr(a,2)^rotr(a,13)^rotr(a,22);std::uint32_t maj=(a&b)^(a&c)^(b&c);std::uint32_t t2=s0+maj;hh=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;}
        h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=hh;
    }
    void raw(unsigned char byte) { block[used++]=byte; if(used==64){compress();used=0;} }
    void update(const std::string& value) { bytes+=value.size(); for(unsigned char byte:value) raw(byte); }
    std::string finish() {
        const std::uint64_t bits=bytes*8ULL; raw(0x80U); while(used!=56)raw(0); for(int i=7;i>=0;--i)raw(static_cast<unsigned char>((bits>>(8*i))&0xffU));
        static const char hex[]="0123456789abcdef";std::string out;out.reserve(64);for(std::uint32_t word:h)for(int shift=28;shift>=0;shift-=4)out.push_back(hex[(word>>shift)&15U]);return out;
    }
};
static std::string digest(const std::string& value){Hash hash;hash.update(value);return hash.finish();}
static std::string wire_digest(const std::string& value){
#ifdef _WIN32
    std::string wire;wire.reserve(value.size()+static_cast<std::size_t>(std::count(value.begin(),value.end(),'\n')));for(char ch:value){if(ch=='\n')wire.push_back('\r');wire.push_back(ch);}return digest(wire);
#else
    return digest(value);
#endif
}
}
'''


def compose_generator_recipe(
    value: Mapping[str, Any],
    *,
    contract: Mapping[str, Any] | None = None,
    catalog: RecipeCatalog | None = None,
    catalog_root: str | Path | None = None,
) -> ComposedGenerator:
    """Compile a validated recipe into a self-contained C++17 adapter."""

    active_catalog = catalog or RecipeCatalog.load(catalog_root)
    recipe = validate_generator_recipe(value, contract=contract, catalog=active_catalog)
    recipe_bytes = _canonical_json(recipe)
    recipe_hash = hashlib.sha256(recipe_bytes).hexdigest()
    dispatch_cases: list[str] = []
    for case in recipe["cases"]:
        family_branches = [
            _emit_family(
                family,
                case["serialization"]["format_id"],
                small=case["profile"] == "small",
                family_count=len(case["families"]),
                family_index=family_index,
            )
            for family_index, family in enumerate(case["families"])
        ]
        all_buckets = case["byte_budget"]["buckets"]
        bucket_rows = ",".join(f"{{{low}ULL,{high}ULL}}" for low, high in all_buckets)
        schedule = case["selection"]["schedule"]
        family_schedule_rows = ",".join(f"{item['family_index']}ULL" for item in schedule)
        bucket_schedule_rows = ",".join(f"{item['bucket_index']}ULL" for item in schedule)
        branch_code = "\n".join(
            f"if(family_index=={index}ULL){body}" for index, body in enumerate(family_branches)
        )
        dispatch_cases.append(
            f"""if(profile=={_cpp_string(case['profile'])} && case_kind=={_cpp_string(case['case_kind'])}) {{
    const unsigned long long round=seed/{case['selection']['seed_stride']}ULL;
    const std::size_t schedule_slot=round%{len(schedule)}ULL;
    const std::size_t family_schedule[]={{ {family_schedule_rows} }};
    const std::size_t bucket_schedule[]={{ {bucket_schedule_rows} }};
    const unsigned long long family_index=family_schedule[schedule_slot];
    const std::pair<std::size_t,std::size_t> buckets[]={{ {bucket_rows} }};
    const std::size_t scheduled_bucket_index=bucket_schedule[schedule_slot];
    const auto bucket=buckets[scheduled_bucket_index];
    const std::size_t bucket_low=bucket.first,bucket_high=bucket.second;
    acm_recipe_local::Rng rng(seed ^ 0xd1b54a32d192ed03ULL);
    std::string data,family_tag,semantic_tag;
    {branch_code}
    const std::size_t data_size=acm_recipe_local::wire_size(data);
    if(data.empty() || data_size>{case['byte_budget']['hard_max']}ULL) throw std::runtime_error("generator recipe hard byte limit exceeded");
    std::size_t bucket_index=scheduled_bucket_index;
    for(std::size_t index=0;index<sizeof(buckets)/sizeof(buckets[0]);++index)if(data_size>=buckets[index].first&&data_size<=buckets[index].second){{bucket_index=index;break;}}
    return {{data,family_tag,semantic_tag,family_index,bucket_index,scheduled_bucket_index}};
}}"""
        )
    # Assets are concatenated before the local dispatcher.  They are immutable
    # catalog inputs and therefore participate in catalog_sha256/cache identity.
    # The local namespace deliberately avoids relying on undocumented asset
    # signatures; catalog primitives can evolve without changing this ABI.
    asset_source = "\n".join(active_catalog.source_fragments)
    source = (
        "// ACM_LOCAL_RECIPE_GENERATOR_V1\n"
        "#include <algorithm>\n#include <cstddef>\n#include <cstdint>\n#include <iostream>\n#include <limits>\n"
        "#include <set>\n#include <sstream>\n#include <stdexcept>\n"
        "#include <string>\n#include <utility>\n#include <vector>\n"
        f"// generator_recipe_sha256: {recipe_hash}\n"
        f"// generator_catalog_sha256: {active_catalog.sha256}\n"
        + asset_source
        + "\n"
        + _LOCAL_CPP_RUNTIME
        + "\n"
        + _SHA256_CPP
        + "\nnamespace acm_recipe_generated {\n"
        + "struct Result{std::string data,family_tag,semantic_tag;std::size_t family_index,bucket_index,scheduled_bucket_index;};\n"
        + "static Result build(unsigned long long seed,const std::string& profile,const std::string& case_kind){\n"
        + "\n".join(dispatch_cases)
        + '\nthrow std::invalid_argument("unsupported recipe profile/case_kind");\n}\n}\n'
        + "void acm_generate_case(unsigned long long seed,const std::string& profile,const std::string& case_kind,std::ostream& out){out<<acm_recipe_generated::build(seed,profile,case_kind).data;}\n"
        + "void acm_generate_manifest(unsigned long long seed,const std::string& profile,const std::string& case_kind,std::ostream& out){"
        + "const auto result=acm_recipe_generated::build(seed,profile,case_kind);"
        + "const std::size_t records=static_cast<std::size_t>(std::count(result.data.begin(),result.data.end(),'\\n'));"
        + "out<<\"{\\\"manifest_version\\\":1,\\\"profile\\\":\\\"\"<<profile<<\"\\\",\\\"case_kind\\\":\\\"\"<<case_kind<<\"\\\",\\\"seed\\\":\"<<seed"
        + "<<\",\\\"input_sha256\\\":\\\"\"<<acm_recipe_sha256::wire_digest(result.data)<<\"\\\",\\\"dimensions\\\":{\\\"bytes\\\":\"<<acm_recipe_local::wire_size(result.data)<<\",\\\"family\\\":\"<<result.family_index<<\",\\\"byte_bucket\\\":\"<<result.bucket_index<<\",\\\"scheduled_byte_bucket\\\":\"<<result.scheduled_bucket_index<<\"},\\\"coverage_tags\\\":[\\\"\"<<result.family_tag<<\"\\\",\\\"byte_bucket:\"<<result.bucket_index<<\"\\\",\\\"\"<<result.semantic_tag<<\"\\\"],\\\"records\\\":\"<<records<<\",\\\"total_complexity\\\":\\\"linear_output\\\"}\";}\n"
    )
    if re.search(r"\b(?:int|auto)\s+main\s*\(", source):
        raise RecipeCatalogError("template assets must not define main")
    metadata = MappingProxyType(
        {
            "engine": GENERATOR_RECIPE_ENGINE,
            "recipe_schema_version": GENERATOR_RECIPE_SCHEMA_VERSION,
            "composer_version": GENERATOR_RECIPE_COMPOSER_VERSION,
            "recipe_sha256": recipe_hash,
            "catalog_sha256": active_catalog.sha256,
            "hard_small_bytes": SMALL_RECIPE_HARD_MAX_BYTES,
        }
    )
    return ComposedGenerator(
        source=source,
        recipe=MappingProxyType(recipe),
        recipe_sha256=recipe_hash,
        catalog_sha256=active_catalog.sha256,
        composer_version=GENERATOR_RECIPE_COMPOSER_VERSION,
        metadata=metadata,
    )


__all__ = [
    "ComposedGenerator",
    "DEFAULT_CATALOG_ROOT",
    "GENERATOR_RECIPE_COMPOSER_VERSION",
    "GENERATOR_RECIPE_ENGINE",
    "GENERATOR_RECIPE_SCHEMA_VERSION",
    "LARGE_RECIPE_HARD_MAX_BYTES",
    "RecipeCatalog",
    "RecipeCatalogError",
    "RecipeError",
    "RecipeValidationError",
    "LARGE_RECIPE_BUCKET_RENDER_ATTEMPTS",
    "SMALL_RECIPE_BUCKET_RENDER_ATTEMPTS",
    "SMALL_EXHAUSTIVE_MAX_BYTES",
    "SMALL_RECIPE_HARD_MAX_BYTES",
    "UnsupportedRecipeError",
    "compose_generator_recipe",
    "supports_static_contract",
    "static_contract_capabilities",
    "validate_generator_recipe",
]
