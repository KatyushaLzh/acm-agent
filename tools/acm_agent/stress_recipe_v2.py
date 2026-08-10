"""Provider-free declarative stress recipes for composite input contracts.

Version 2 deliberately has only two audited contract-shape compilers.  The
contract supplies field paths and integer bounds; executable source is always
selected locally.  Recipe JSON cannot contain source snippets or expressions.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .canonical import canonical_json_bytes
from .stress_recipe import (
    ComposedGenerator,
    LARGE_RECIPE_HARD_MAX_BYTES,
    SMALL_RECIPE_HARD_MAX_BYTES,
    RecipeValidationError,
    UnsupportedRecipeError,
    _SHA256_CPP,
)

GENERATOR_RECIPE_V2_SCHEMA_VERSION = 2
GENERATOR_RECIPE_V2_ENGINE = "local_templates_v2"
GENERATOR_RECIPE_V2_COMPOSER_VERSION = 1
GENERATOR_RECIPE_V2_SOURCE = "deterministic_contract_shape_v2"

_CASES = (
    ("small", "lower_bound"),
    ("small", "random"),
    ("large", "upper_bound"),
    ("large", "random"),
)
_PATH = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_MACHINES = frozenset({"mutable_permutation", "bracket_interval_queries"})


def _strict(value: Mapping[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required)
    if missing:
        raise RecipeValidationError(f"missing field(s): {', '.join(missing)}", path=path)
    if unknown:
        raise RecipeValidationError(f"unknown field(s): {', '.join(unknown)}", path=path)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecipeValidationError("expected an object", path=path)
    return value


def _integer(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecipeValidationError("expected an integer", path=path)
    if minimum is not None and value < minimum:
        raise RecipeValidationError(f"integer must be >= {minimum}", path=path)
    return value


def _path(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _PATH.fullmatch(value):
        raise RecipeValidationError("expected a bound section.field path", path=path)
    return value


def _tag(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _TAG.fullmatch(value):
        raise RecipeValidationError("unsafe operation tag", path=path)
    return value


def _field_path(section: Mapping[str, Any], field: Mapping[str, Any]) -> str:
    return f"{section['id']}.{field['name']}"


def _fields(section: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = section.get("fields", [])
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _sections(contract: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    syntax = contract.get("syntax")
    if not isinstance(syntax, Mapping) or syntax.get("mode", "single_case") != "single_case":
        raise UnsupportedRecipeError("contract_mode_not_single_case")
    raw = syntax.get("sections")
    if not isinstance(raw, list) or not raw:
        raise UnsupportedRecipeError("contract_sections_missing")
    if not all(isinstance(item, Mapping) for item in raw):
        raise UnsupportedRecipeError("contract_section_invalid")
    return list(raw)


def _ranges(contract: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for item in contract.get("constraints", []):
        if not isinstance(item, Mapping) or item.get("kind") != "range":
            continue
        target, args = item.get("target"), item.get("args")
        if not isinstance(target, str) or not isinstance(args, Mapping):
            continue
        lo = args.get("minimum", args.get("min", args.get("lower")))
        hi = args.get("maximum", args.get("max", args.get("upper")))
        if type(lo) is int and type(hi) is int and lo <= hi:
            result[target] = (lo, hi)
    for section in _sections(contract):
        for field in _fields(section):
            lo, hi = field.get("minimum"), field.get("maximum")
            if type(lo) is int and type(hi) is int and lo <= hi:
                result.setdefault(_field_path(section, field), (lo, hi))
    return result


def _constraint(contract: Mapping[str, Any], kind: str, target: str) -> bool:
    for item in contract.get("constraints", []):
        if isinstance(item, Mapping) and item.get("kind") == kind and item.get("target") == target:
            return True
    return False


def _constraint_args(contract: Mapping[str, Any], kind: str, target: str) -> Mapping[str, Any] | None:
    matches = [
        item.get("args")
        for item in contract.get("constraints", [])
        if isinstance(item, Mapping) and item.get("kind") == kind and item.get("target") == target
    ]
    if len(matches) != 1 or not isinstance(matches[0], Mapping):
        return None
    return matches[0]


def _constraint_args_any(contract: Mapping[str, Any], kinds: set[str], targets: set[str]) -> Mapping[str, Any] | None:
    matches = [
        item.get("args")
        for item in contract.get("constraints", [])
        if isinstance(item, Mapping) and item.get("kind") in kinds and item.get("target") in targets
    ]
    if len(matches) != 1 or not isinstance(matches[0], Mapping):
        return None
    return matches[0]


def _bound_is(args: Mapping[str, Any], key: str, value: Any) -> bool:
    aliases = {
        "minimum": ("minimum", "minimum_from", "min", "lower", "lower_from"),
        "maximum": ("maximum", "maximum_from", "max", "upper", "upper_from"),
    }[key]
    return any(args.get(alias) == value for alias in aliases)


def _canonical_header_ref(
    value: Any,
    *,
    header: Mapping[str, Any],
    fallback_name: str,
) -> str | None:
    """Resolve the model's sectionless count aliases without guessing roles."""

    fields = _fields(header)
    aliases: dict[str, str] = {}
    for field in fields:
        name = str(field.get("name") or "")
        path = _field_path(header, field)
        aliases[path] = path
        if name:
            aliases[name] = path
    if isinstance(value, str) and value in aliases:
        return aliases[value]
    if value is None:
        matches = [
            _field_path(header, field)
            for field in fields
            if field.get("name") == fallback_name
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _section_range(
    ranges: Mapping[str, tuple[int, int]],
    *,
    section: Mapping[str, Any],
    field: Mapping[str, Any],
) -> tuple[int, int] | None:
    """Resolve a range uniquely bound to a one-field typed section."""

    path = _field_path(section, field)
    section_id = str(section.get("id") or "")
    direct = ranges.get(path) or ranges.get(section_id)
    if direct is not None:
        return direct
    matches = {
        bounds
        for target, bounds in ranges.items()
        if section_id and target.startswith(f"{section_id}.")
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _record_bound_args(
    contract: Mapping[str, Any],
    *,
    section: Mapping[str, Any],
    field: Mapping[str, Any],
    role_aliases: set[str],
) -> Mapping[str, Any] | None:
    section_id = str(section.get("id") or "")
    targets = {
        _field_path(section, field),
        *(f"{section_id}.{alias}" for alias in role_aliases),
    }
    constrained = _constraint_args_any(
        contract, {"dependent_bound", "range"}, targets
    )
    if constrained is not None:
        return constrained
    # Field min/max are already typed schema members, not expressions.  Return
    # them only when at least one bound is present so absence cannot masquerade
    # as a complete interval contract.
    if any(
        key in field
        for key in (
            "minimum",
            "minimum_from",
            "lower",
            "lower_from",
            "maximum",
            "maximum_from",
            "upper",
            "upper_from",
        )
    ):
        return field
    return None


def _case(profile: str, case_kind: str, dimensions: Mapping[str, int], coverage: Sequence[str]) -> dict[str, Any]:
    return {
        "profile": profile,
        "case_kind": case_kind,
        "dimensions": dict(dimensions),
        "byte_budget": {
            "hard_max": SMALL_RECIPE_HARD_MAX_BYTES if profile == "small" else LARGE_RECIPE_HARD_MAX_BYTES,
            "planned_bucket": 0 if profile == "small" else 3,
        },
        "coverage_tags": list(coverage),
    }


def _base_recipe(contract: Mapping[str, Any], machine: Mapping[str, Any], sections: Sequence[Mapping[str, str]], cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": GENERATOR_RECIPE_V2_SCHEMA_VERSION,
        "engine": GENERATOR_RECIPE_V2_ENGINE,
        "recipe_source": GENERATOR_RECIPE_V2_SOURCE,
        "contract_sha256": hashlib.sha256(canonical_json_bytes(contract)).hexdigest(),
        "machine": dict(machine),
        "sections": [dict(item) for item in sections],
        "cases": [dict(item) for item in cases],
    }


def _compile_permutation(contract: Mapping[str, Any]) -> dict[str, Any] | None:
    sections = _sections(contract)
    scalar = [s for s in sections if s.get("kind") == "scalar" and len(_fields(s)) == 2]
    lists = [s for s in sections if s.get("kind") == "list" and len(_fields(s)) == 1]
    streams = [s for s in sections if s.get("kind") == "operation_stream"]
    if len(scalar) != 1 or len(lists) != 1 or len(streams) != 1 or len(sections) != 3:
        return None
    header, initial, operations = scalar[0], lists[0], streams[0]
    header_fields = _fields(header)
    if any(field.get("type") != "int" for field in header_fields + _fields(initial)):
        return None
    n_ref = _canonical_header_ref(
        initial.get("count_from"), header=header, fallback_name="n"
    )
    m_ref = _canonical_header_ref(
        operations.get("count_from"), header=header, fallback_name="m"
    )
    header_paths = {_field_path(header, field) for field in header_fields}
    if n_ref is None or m_ref is None or n_ref == m_ref or {n_ref, m_ref} != header_paths:
        return None
    item_ref = _field_path(initial, _fields(initial)[0])
    ranges = _ranges(contract)
    if n_ref not in ranges or m_ref not in ranges:
        return None
    n_lo, n_hi = ranges[n_ref]
    m_lo, m_hi = ranges[m_ref]
    has_bound_permutation_constraint = any(
        isinstance(item, Mapping)
        and item.get("kind") == "permutation"
        and str(item.get("target") or "")
        in {item_ref, str(initial.get("id"))}
        for item in contract.get("constraints", [])
    )
    named_permutation_domain = "permutation" in str(initial.get("id") or "").casefold()
    if not (has_bound_permutation_constraint or named_permutation_domain):
        return None
    variants = operations.get("variants")
    if not isinstance(variants, list) or len(variants) != 5:
        return None
    by_tag = {str(v.get("tag")): v for v in variants if isinstance(v, Mapping)}
    if set(by_tag) != {"Top", "Bottom", "Insert", "Ask", "Query"}:
        return None
    arities = {"Top": 1, "Bottom": 1, "Insert": 2, "Ask": 1, "Query": 1}
    for name, arity in arities.items():
        fields = _fields(by_tag[name])
        if len(fields) != arity or any(field.get("type") != "int" for field in fields):
            return None
    insert_fields = _fields(by_tag["Insert"])
    insert_t_target = f"{operations['id']}.Insert.{insert_fields[1]['name']}"
    insert_t_args = _constraint_args_any(
        contract,
        {"range"},
        {
            insert_t_target,
            _field_path(operations, insert_fields[1]),
            f"{operations['id']}.*.{insert_fields[1]['name']}",
            f"{operations['id']}.arg2",
        },
    )
    if insert_t_args is None:
        for constraint in contract.get("constraints", []):
            if not isinstance(constraint, Mapping) or constraint.get("kind") != "range":
                continue
            target = str(constraint.get("target") or "")
            if not target.startswith(f"{operations['id']}."):
                continue
            if target.rsplit(".", 1)[-1] not in {
                str(insert_fields[1]["name"]),
                "arg2",
                "t",
            }:
                continue
            args = constraint.get("args")
            if isinstance(args, Mapping):
                insert_t_args = args
                break
    inline_insert_range = insert_fields[1].get("minimum") == -1 and insert_fields[1].get("maximum") == 1
    constrained_insert_range = insert_t_args is not None and _bound_is(insert_t_args, "minimum", -1) and _bound_is(insert_t_args, "maximum", 1)
    if not (inline_insert_range or constrained_insert_range):
        return None
    for name in ("Top", "Bottom", "Insert", "Ask", "Query"):
        field = _fields(by_tag[name])[0]
        targets = {
            _field_path(operations, field),
            f"{operations['id']}.{name}.{field['name']}",
            f"{operations['id']}.*.{field['name']}",
            f"{operations['id']}.arg1",
        }
        args = _constraint_args_any(contract, {"range", "dependent_bound"}, targets)
        allowed_maxima = {n_ref, n_hi, n_ref.rsplit(".", 1)[-1]}
        if args is not None and (
            not _bound_is(args, "minimum", 1)
            or not any(_bound_is(args, "maximum", value) for value in allowed_maxima)
        ):
            return None
    if n_lo < 1 or m_lo < 0:
        return None
    if n_hi > 5_000_000 or m_hi > 5_000_000 or 24 * n_hi + 40 * m_hi > LARGE_RECIPE_HARD_MAX_BYTES:
        raise RecipeValidationError("contract upper bound cannot fit the 32 MiB recipe limit")
    # The trusted preflight's first small-case bucket is capped at 100 bytes.
    # Seven operations are enough to exercise all five tags and Insert -1/0/1;
    # keep every small/random output inside that exact bucket.
    small_n_lo, small_n_hi = max(n_lo, 2 if n_hi >= 2 else n_lo), min(n_hi, 8)
    small_m_lo = max(m_lo, 7 if m_hi >= 7 else m_lo)
    small_m_hi = min(m_hi, 7 if m_hi >= 7 else m_hi)
    if small_n_lo > small_n_hi:
        small_n_lo = small_n_hi = n_lo
    if small_m_lo > small_m_hi:
        small_m_lo = small_m_hi = m_lo
    operation_tags = {key: key for key in ("Top", "Bottom", "Insert", "Ask", "Query")}
    machine = {
        "kind": "mutable_permutation",
        "bindings": {"n": n_ref, "m": m_ref, "items": item_ref, "operations": str(operations["id"])},
        "limits": {"n_min": n_lo, "n_max": n_hi, "m_min": m_lo, "m_max": m_hi},
        "operation_tags": operation_tags,
    }
    coverage = ["section_family:permutation_operation_stream", "state_machine:mutable_permutation"]
    complete_ops = n_hi >= 2 and m_hi >= 7
    cases = [
        _case("small", "lower_bound", {"n": n_lo, "m": m_lo}, coverage + ["boundary:lower"]),
        _case("small", "random", {"n_min": small_n_lo, "n_max": small_n_hi, "m_min": small_m_lo, "m_max": small_m_hi}, coverage + (["operations:all", "insert:-1,0,1", "boundary:positions"] if complete_ops else ["operations:feasible_subset"])),
        _case("large", "upper_bound", {"n": n_hi, "m": m_hi}, coverage + ["boundary:upper"]),
        _case("large", "random", {"n_min": n_lo, "n_max": n_hi, "m_min": m_lo, "m_max": m_hi}, coverage + ["seed_variation"]),
    ]
    return _base_recipe(
        contract,
        machine,
        [
            {"kind": "scalar", "binding": n_ref},
            {"kind": "scalar", "binding": m_ref},
            {"kind": "list", "binding": item_ref},
            {"kind": "operation_stream", "binding": str(operations["id"])},
        ],
        cases,
    )


def _exact_bracket_alphabet(
    section: Mapping[str, Any], contract: Mapping[str, Any]
) -> bool:
    value = section.get("alphabet")
    if isinstance(value, list) and len(value) == 2 and set(value) == {"(", ")"}:
        return True
    fields = _fields(section)
    field_path = (
        _field_path(section, fields[0])
        if len(fields) == 1
        else f"{section['id']}.value"
    )
    for item in contract.get("constraints", []):
        if not isinstance(item, Mapping) or item.get("kind") != "custom_text":
            continue
        if item.get("target") not in {str(section.get("id")), field_path}:
            continue
        args = item.get("args")
        if isinstance(args, Mapping) and args.get("pattern") in {
            "^[()]+$",
            "^[()]*$",
        }:
            return True
    return False


def _compile_brackets(contract: Mapping[str, Any]) -> dict[str, Any] | None:
    sections = _sections(contract)
    string_section: Mapping[str, Any] | None = None
    q_section: Mapping[str, Any] | None = None
    records: Mapping[str, Any] | None = None
    raw_like = False
    for section in sections:
        fields = _fields(section)
        explicit_string = (
            len(fields) == 1
            and fields[0].get("type") in {"string", "token", "char"}
        )
        implicit_string = section.get("kind") == "string" and not fields
        if section.get("kind") in {"string", "raw"} and (
            explicit_string or implicit_string
        ):
            string_section = section
            raw_like = section.get("kind") == "raw"
        elif section.get("kind") == "scalar" and len(fields) == 1 and fields[0].get("type") == "int":
            q_section = section
        elif section.get("kind") in {"list", "interval", "intervals", "raw"} and len(fields) == 2 and all(field.get("type") == "int" for field in fields):
            records = section
            raw_like = raw_like or section.get("kind") == "raw"
    if string_section is None or q_section is None or records is None or len(sections) != 3:
        # A single raw section is accepted only when all repetition bindings are explicit.
        if len(sections) != 1 or sections[0].get("kind") != "raw":
            return None
        raw = sections[0]
        fields = _fields(raw)
        if len(fields) != 4 or fields[0].get("type") not in {"string", "token"} or any(field.get("type") != "int" for field in fields[1:]):
            return None
        q_ref = _field_path(raw, fields[1])
        if fields[2].get("count_from") != q_ref or fields[3].get("count_from") != q_ref:
            return None
        string_section = q_section = records = raw
        string_field, q_field, left_field, right_field = fields
        raw_like = True
    else:
        string_fields, q_fields, record_fields = _fields(string_section), _fields(q_section), _fields(records)
        string_field = (
            string_fields[0]
            if string_fields
            else {"name": "value", "type": "string"}
        )
        q_field = q_fields[0]
        left_field, right_field = record_fields
        q_ref = _field_path(q_section, q_field)
        count_from = records.get("count_from")
        count_aliases = {
            q_ref,
            str(q_section.get("id") or ""),
            str(q_field.get("name") or ""),
        }
        section_count_alias = bool(
            isinstance(count_from, str)
            and re.fullmatch(
                rf"{re.escape(str(q_section.get('id') or ''))}\.[a-z][a-z0-9_]*",
                count_from,
            )
        )
        inferred_unique_count = (
            count_from is None
            and records.get("kind") in {"interval", "intervals"}
            and sections.index(q_section) + 1 == sections.index(records)
        )
        if (
            count_from not in count_aliases
            and not section_count_alias
            and not inferred_unique_count
        ):
            return None
    if not _exact_bracket_alphabet(string_section, contract):
        return None
    string_ref = _field_path(string_section, string_field)
    q_ref = _field_path(q_section, q_field)
    left_ref, right_ref = _field_path(records, left_field), _field_path(records, right_field)
    ranges = _ranges(contract)
    q_range = _section_range(ranges, section=q_section, field=q_field)
    string_range = _section_range(
        ranges, section=string_section, field=string_field
    )
    if string_range is None:
        for item in contract.get("constraints", []):
            if isinstance(item, Mapping) and item.get("kind") in {"length_equals", "range"} and str(item.get("target")) in {string_ref, f"{string_ref}.length", f"length({string_ref})", f"|{string_ref}|", f"{string_section.get('id')}.length"}:
                args = item.get("args", {})
                if isinstance(args, Mapping) and type(args.get("minimum")) is int and type(args.get("maximum")) is int:
                    string_range = (args["minimum"], args["maximum"])
                    break
    if q_range is None or string_range is None:
        return None
    left_args = _record_bound_args(
        contract,
        section=records,
        field=left_field,
        role_aliases={"l", "left"},
    )
    right_args = _record_bound_args(
        contract,
        section=records,
        field=right_field,
        role_aliases={"r", "right"},
    )
    combined_args = _constraint_args_any(
        contract,
        {"dependent_bound"},
        {f"{left_ref},{right_ref}", f"{left_ref}, {right_ref}"},
    )
    string_bound_aliases = {
        string_ref,
        f"{string_ref}.length",
        f"{string_section.get('id')}.length",
        # Closed conventional aliases accepted only inside this exact
        # one-string interval compiler.  They canonicalize to the unique
        # string length and never enter the declarative recipe.
        "n",
        "N",
    }
    left_bound_aliases = {left_ref, str(left_field.get("name") or ""), "l", "left"}
    combined_interval_bound = (
        combined_args is not None
        and records.get("kind") in {"interval", "intervals"}
        and combined_args.get("lower") == 1
        and combined_args.get("upper") in string_bound_aliases
    )
    if (
        not combined_interval_bound
        and (
            left_args is None
            or right_args is None
            or not _bound_is(left_args, "minimum", 1)
            or not any(
                _bound_is(left_args, "maximum", alias)
                for alias in string_bound_aliases
            )
            or not any(
                _bound_is(right_args, "minimum", alias)
                for alias in left_bound_aliases
            )
            or not any(
                _bound_is(right_args, "maximum", alias)
                for alias in string_bound_aliases
            )
        )
    ):
        return None
    n_lo, n_hi = string_range
    q_lo, q_hi = q_range
    if n_lo < 1 or q_lo < 0:
        return None
    if n_hi > 10_000_000 or q_hi > 5_000_000 or 2 * n_hi + 32 * q_hi > LARGE_RECIPE_HARD_MAX_BYTES:
        raise RecipeValidationError("contract upper bound cannot fit the 32 MiB recipe limit")
    machine = {
        "kind": "bracket_interval_queries",
        "bindings": {"string": string_ref, "q": q_ref, "left": left_ref, "right": right_ref},
        "limits": {"length_min": n_lo, "length_max": n_hi, "q_min": q_lo, "q_max": q_hi},
        "alphabet": "()",
        "raw_like": raw_like,
    }
    coverage = ["section_family:string_interval_records", "alphabet:brackets"]
    small_n_hi = min(n_hi, 16)
    small_q_lo = max(q_lo, 3 if q_hi >= 3 else q_lo)
    small_q_hi = min(q_hi, 6 if q_hi >= 3 else q_hi)
    if small_q_lo > small_q_hi:
        small_q_lo = small_q_hi = q_lo
    cases = [
        _case("small", "lower_bound", {"length": n_lo, "q": q_lo}, coverage + ["boundary:lower"]),
        _case("small", "random", {"length_min": n_lo, "length_max": small_n_hi, "q_min": small_q_lo, "q_max": small_q_hi}, coverage + ["interval:point,full,nested", "seed_variation"]),
        _case("large", "upper_bound", {"length": n_hi, "q": q_hi}, coverage + ["boundary:upper"]),
        _case("large", "random", {"length_min": n_lo, "length_max": n_hi, "q_min": q_lo, "q_max": q_hi}, coverage + ["seed_variation"]),
    ]
    return _base_recipe(
        contract,
        machine,
        [
            {"kind": "string", "binding": string_ref},
            {"kind": "scalar", "binding": q_ref},
            {"kind": "record_stream", "binding": f"{left_ref},{right_ref}"},
        ],
        cases,
    )


def compile_static_contract_v2(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Compile one of the two exact, problem-id-independent contract shapes."""

    if not isinstance(contract, Mapping):
        raise UnsupportedRecipeError("contract_missing")
    for compiler in (_compile_permutation, _compile_brackets):
        recipe = compiler(contract)
        if recipe is not None:
            return validate_generator_recipe_v2(recipe, contract=contract)
    raise UnsupportedRecipeError("contract_wire_shape_not_representable_v2")


def supports_static_contract_v2(contract: Mapping[str, Any] | None) -> tuple[bool, str | None]:
    try:
        compile_static_contract_v2(contract)  # type: ignore[arg-type]
    except UnsupportedRecipeError as exc:
        return False, exc.reason
    except RecipeValidationError as exc:
        return False, str(exc)
    return True, None


def validate_generator_recipe_v2(value: Mapping[str, Any], *, contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    root = _mapping(value, "$")
    _strict(root, {"schema_version", "engine", "recipe_source", "contract_sha256", "machine", "sections", "cases"}, "$")
    if root["schema_version"] != 2 or root["engine"] != GENERATOR_RECIPE_V2_ENGINE or root["recipe_source"] != GENERATOR_RECIPE_V2_SOURCE:
        raise RecipeValidationError("not a generator_recipe/v2 local template recipe")
    digest = root["contract_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RecipeValidationError("invalid contract sha256", path="$.contract_sha256")
    if contract is not None and digest != hashlib.sha256(canonical_json_bytes(contract)).hexdigest():
        raise RecipeValidationError("recipe is bound to a different contract", path="$.contract_sha256")
    machine = _mapping(root["machine"], "$.machine")
    kind = machine.get("kind")
    if kind not in _MACHINES:
        raise RecipeValidationError("unknown state semantics", path="$.machine.kind")
    common = {"kind", "bindings", "limits"}
    required = common | ({"operation_tags"} if kind == "mutable_permutation" else {"alphabet", "raw_like"})
    _strict(machine, required, "$.machine")
    bindings = _mapping(machine["bindings"], "$.machine.bindings")
    limits = _mapping(machine["limits"], "$.machine.limits")
    if kind == "mutable_permutation":
        _strict(bindings, {"n", "m", "items", "operations"}, "$.machine.bindings")
        _strict(limits, {"n_min", "n_max", "m_min", "m_max"}, "$.machine.limits")
        tags = _mapping(machine["operation_tags"], "$.machine.operation_tags")
        _strict(tags, {"Top", "Bottom", "Insert", "Ask", "Query"}, "$.machine.operation_tags")
        for key, tag in tags.items():
            if _tag(tag, f"$.machine.operation_tags.{key}") != key:
                raise RecipeValidationError("operation role/tag mismatch", path=f"$.machine.operation_tags.{key}")
    else:
        _strict(bindings, {"string", "q", "left", "right"}, "$.machine.bindings")
        _strict(limits, {"length_min", "length_max", "q_min", "q_max"}, "$.machine.limits")
        if machine["alphabet"] != "()" or type(machine["raw_like"]) is not bool:
            raise RecipeValidationError("bracket machine has invalid closed configuration", path="$.machine")
    for key, item in bindings.items():
        if key == "operations":
            if not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", item):
                raise RecipeValidationError("invalid section binding", path=f"$.machine.bindings.{key}")
        else:
            _path(item, f"$.machine.bindings.{key}")
    integer_limits = {key: _integer(item, f"$.machine.limits.{key}", minimum=0) for key, item in limits.items()}
    keys = ("n_min", "n_max", "m_min", "m_max") if kind == "mutable_permutation" else ("length_min", "length_max", "q_min", "q_max")
    if integer_limits[keys[0]] > integer_limits[keys[1]] or integer_limits[keys[2]] > integer_limits[keys[3]] or integer_limits[keys[0]] < 1:
        raise RecipeValidationError("invalid machine limit interval", path="$.machine.limits")
    sections = root["sections"]
    if not isinstance(sections, list) or not sections:
        raise RecipeValidationError("sections must be a non-empty array", path="$.sections")
    for index, raw in enumerate(sections):
        item = _mapping(raw, f"$.sections[{index}]")
        _strict(item, {"kind", "binding"}, f"$.sections[{index}]")
        if item["kind"] not in {"scalar", "string", "list", "record_stream", "operation_stream"}:
            raise RecipeValidationError("unsupported section kind", path=f"$.sections[{index}].kind")
        binding = item["binding"]
        if not isinstance(binding, str) or any(token in binding.casefold() for token in ("cpp", "expression", "#include", ";", "{")):
            raise RecipeValidationError("unsafe section binding", path=f"$.sections[{index}].binding")
    cases = root["cases"]
    if not isinstance(cases, list) or len(cases) != 4:
        raise RecipeValidationError("exactly four profile-v2 cases are required", path="$.cases")
    normalized_cases: list[dict[str, Any]] = []
    for index, expected in enumerate(_CASES):
        case = _mapping(cases[index], f"$.cases[{index}]")
        _strict(case, {"profile", "case_kind", "dimensions", "byte_budget", "coverage_tags"}, f"$.cases[{index}]")
        if (case["profile"], case["case_kind"]) != expected:
            raise RecipeValidationError("profile-v2 case order mismatch", path=f"$.cases[{index}]")
        dimensions = _mapping(case["dimensions"], f"$.cases[{index}].dimensions")
        exact = case["case_kind"] in {"lower_bound", "upper_bound"}
        dimension_keys = ({"n", "m"} if kind == "mutable_permutation" else {"length", "q"}) if exact else ({"n_min", "n_max", "m_min", "m_max"} if kind == "mutable_permutation" else {"length_min", "length_max", "q_min", "q_max"})
        _strict(dimensions, dimension_keys, f"$.cases[{index}].dimensions")
        dims = {key: _integer(item, f"$.cases[{index}].dimensions.{key}", minimum=0) for key, item in dimensions.items()}
        budget = _mapping(case["byte_budget"], f"$.cases[{index}].byte_budget")
        _strict(budget, {"hard_max", "planned_bucket"}, f"$.cases[{index}].byte_budget")
        expected_hard = SMALL_RECIPE_HARD_MAX_BYTES if case["profile"] == "small" else LARGE_RECIPE_HARD_MAX_BYTES
        if budget["hard_max"] != expected_hard or _integer(budget["planned_bucket"], f"$.cases[{index}].byte_budget.planned_bucket", minimum=0) > 3:
            raise RecipeValidationError("invalid byte budget", path=f"$.cases[{index}].byte_budget")
        tags = case["coverage_tags"]
        if not isinstance(tags, list) or not tags or any(not isinstance(tag, str) or not tag or len(tag) > 80 for tag in tags):
            raise RecipeValidationError("invalid coverage tags", path=f"$.cases[{index}].coverage_tags")
        normalized_cases.append({**dict(case), "dimensions": dims, "byte_budget": dict(budget), "coverage_tags": list(tags)})
    normalized = {**dict(root), "machine": {**dict(machine), "bindings": dict(bindings), "limits": integer_limits}, "sections": [dict(item) for item in sections], "cases": normalized_cases}
    expected_sections = (
        [
            {"kind": "scalar", "binding": bindings["n"]},
            {"kind": "scalar", "binding": bindings["m"]},
            {"kind": "list", "binding": bindings["items"]},
            {"kind": "operation_stream", "binding": bindings["operations"]},
        ]
        if kind == "mutable_permutation"
        else [
            {"kind": "string", "binding": bindings["string"]},
            {"kind": "scalar", "binding": bindings["q"]},
            {"kind": "record_stream", "binding": f"{bindings['left']},{bindings['right']}"},
        ]
    )
    if normalized["sections"] != expected_sections:
        raise RecipeValidationError("section pipeline does not match machine bindings", path="$.sections")
    if contract is not None:
        expected = None
        for compiler in (_compile_permutation, _compile_brackets):
            expected = compiler(contract)
            if expected is not None:
                break
        if expected is None or canonical_json_bytes(normalized) != canonical_json_bytes(expected):
            raise RecipeValidationError("recipe does not exactly match deterministic contract compilation")
    return normalized


_CPP_RUNTIME = r'''
namespace acm_recipe_v2_local {
struct Rng {
    unsigned long long state;
    explicit Rng(unsigned long long seed):state(seed){}
    unsigned long long next(){unsigned long long z=(state+=0x9e3779b97f4a7c15ULL);z=(z^(z>>30))*0xbf58476d1ce4e5b9ULL;z=(z^(z>>27))*0x94d049bb133111ebULL;return z^(z>>31);}
    unsigned long long bounded(unsigned long long n){if(!n)return 0;unsigned long long t=(0ULL-n)%n;for(;;){auto x=next();if(x>=t)return x%n;}}
    long long between(long long lo,long long hi){return hi<=lo?lo:lo+static_cast<long long>(bounded(static_cast<unsigned long long>(hi-lo)+1));}
};
static std::size_t wire_size(const std::string& s){
#ifdef _WIN32
return s.size()+static_cast<std::size_t>(std::count(s.begin(),s.end(),'\n'));
#else
return s.size();
#endif
}
static int byte_bucket(std::size_t n){return n<=100?0:n<=1024?1:n<=1048576?2:3;}
struct Result{std::string data,family,state,operations;std::vector<std::string> tags;std::size_t planned_bucket;};
struct Permutation {
    std::vector<int> previous,next;int head,tail;
    explicit Permutation(const std::vector<int>& order):previous(order.size()+1),next(order.size()+1),head(order.front()),tail(order.back()){
        for(std::size_t i=0;i<order.size();++i){previous[order[i]]=i?order[i-1]:0;next[order[i]]=i+1<order.size()?order[i+1]:0;}
    }
    void detach(int x){int a=previous[x],b=next[x];if(a)next[a]=b;else head=b;if(b)previous[b]=a;else tail=a;}
    void front(int x){if(x==head)return;detach(x);previous[x]=0;next[x]=head;previous[head]=x;head=x;}
    void back(int x){if(x==tail)return;detach(x);next[x]=0;previous[x]=tail;next[tail]=x;tail=x;}
    void up(int x){int y=previous[x];if(!y)return;int a=previous[y],b=next[x];if(a)next[a]=x;else head=x;previous[x]=a;next[x]=y;previous[y]=x;next[y]=b;if(b)previous[b]=y;else tail=y;}
    void down(int x){int y=next[x];if(y)up(y);}
};
static Result permutation_case(unsigned long long seed,const std::string& profile,const std::string& kind,long long nlo,long long nhi,long long mlo,long long mhi,std::size_t planned){
    Rng rng(seed^0xa0761d6478bd642fULL);long long n=kind=="lower_bound"||kind=="upper_bound"?nlo:rng.between(nlo,nhi);long long m=kind=="lower_bound"||kind=="upper_bound"?mlo:rng.between(mlo,mhi);
    std::vector<int> order(static_cast<std::size_t>(n));for(int i=0;i<n;++i)order[i]=i+1;for(std::size_t i=order.size();i>1;--i)std::swap(order[i-1],order[rng.bounded(i)]);
    Permutation state(order);std::ostringstream out;out<<n<<' '<<m<<'\n';for(std::size_t i=0;i<order.size();++i)out<<order[i]<<(i+1==order.size()?'\n':' ');
    for(long long i=0;i<m;++i){int role=static_cast<int>((i+seed)%7ULL);int x=1+static_cast<int>(rng.bounded(n));
        if(role==0){out<<"Top "<<x<<'\n';state.front(x);}
        else if(role==1){out<<"Bottom "<<x<<'\n';state.back(x);}
        else if(role==2){out<<"Insert "<<x<<" 0\n";}
        else if(role==3){if(state.head==state.tail){out<<"Insert "<<x<<" 0\n";}else{x=state.next[state.head];out<<"Insert "<<x<<" -1\n";state.up(x);}}
        else if(role==4){if(state.head==state.tail){out<<"Insert "<<x<<" 0\n";}else{x=state.previous[state.tail];out<<"Insert "<<x<<" 1\n";state.down(x);}}
        else if(role==5){out<<"Ask "<<x<<'\n';}
        else {long long k=(i&1)?1:n;out<<"Query "<<k<<'\n';}
    }
    std::vector<std::string> tags={"dynamic_preconditions:checked"};if(n>=2&&m>=7){tags.push_back("operations:Top,Bottom,Insert,Ask,Query");tags.push_back("insert:-1,0,1");}else tags.push_back("operations:feasible_subset");
    return {out.str(),"permutation_operation_stream","mutable_permutation","Top,Bottom,Insert,Ask,Query",tags,planned};
}
static Result bracket_case(unsigned long long seed,const std::string& profile,const std::string& kind,long long nlo,long long nhi,long long qlo,long long qhi,std::size_t planned){
    Rng rng(seed^0xe7037ed1a0b428dbULL);long long n=kind=="lower_bound"||kind=="upper_bound"?nlo:rng.between(nlo,nhi);long long q=kind=="lower_bound"||kind=="upper_bound"?qlo:rng.between(qlo,qhi);
    std::string s(static_cast<std::size_t>(n),'(');for(long long i=0;i<n;++i){unsigned long long mode=(seed+i)%5ULL;s[static_cast<std::size_t>(i)]=mode<2?'(':')';}std::ostringstream out;out<<s<<'\n'<<q<<'\n';
    for(long long i=0;i<q;++i){long long l=1,r=n;if(i%4==1)r=l=1+rng.bounded(n);else if(i%4==2){l=1;r=n;}else if(i%4==3){l=1+rng.bounded(n);r=l+rng.bounded(n-l+1);}out<<l<<' '<<r<<'\n';}
    return {out.str(),"string_interval_records","none","interval_query",{"alphabet:()","interval:1<=l<=r<=length","interval:point,full,nested"},planned};
}
}
'''

_CATALOG_DESCRIPTOR = {
    "engine": GENERATOR_RECIPE_V2_ENGINE,
    "machines": {
        "mutable_permutation": {"complexity": "O(n+m)", "roles": ["Top", "Bottom", "Insert", "Ask", "Query"]},
        "bracket_interval_queries": {"complexity": "O(length+q)", "alphabet": "()"},
    },
    "runtime_sha256": hashlib.sha256(_CPP_RUNTIME.encode("utf-8")).hexdigest(),
}
GENERATOR_RECIPE_V2_CATALOG_SHA256 = hashlib.sha256(canonical_json_bytes(_CATALOG_DESCRIPTOR)).hexdigest()


def recipe_v2_identity() -> Mapping[str, Any]:
    return MappingProxyType({
        "engine": GENERATOR_RECIPE_V2_ENGINE,
        "recipe_schema_version": GENERATOR_RECIPE_V2_SCHEMA_VERSION,
        "composer_version": GENERATOR_RECIPE_V2_COMPOSER_VERSION,
        "catalog_sha256": GENERATOR_RECIPE_V2_CATALOG_SHA256,
    })


def _cpp_case(case: Mapping[str, Any], machine: str) -> str:
    dims, budget = case["dimensions"], case["byte_budget"]
    if machine == "mutable_permutation":
        nlo = dims.get("n", dims.get("n_min")); nhi = dims.get("n", dims.get("n_max")); mlo = dims.get("m", dims.get("m_min")); mhi = dims.get("m", dims.get("m_max"))
        call = f"permutation_case(seed,profile,case_kind,{nlo}LL,{nhi}LL,{mlo}LL,{mhi}LL,{budget['planned_bucket']}ULL)"
    else:
        nlo = dims.get("length", dims.get("length_min")); nhi = dims.get("length", dims.get("length_max")); qlo = dims.get("q", dims.get("q_min")); qhi = dims.get("q", dims.get("q_max"))
        call = f"bracket_case(seed,profile,case_kind,{nlo}LL,{nhi}LL,{qlo}LL,{qhi}LL,{budget['planned_bucket']}ULL)"
    return (
        f'if(profile=="{case["profile"]}"&&case_kind=="{case["case_kind"]}"){{'
        f'auto result={call};if(acm_recipe_v2_local::wire_size(result.data)>{budget["hard_max"]}ULL)'
        'throw std::runtime_error("generator recipe hard byte limit exceeded");return result;}'
    )


def compose_generator_recipe_v2(value: Mapping[str, Any], *, contract: Mapping[str, Any] | None = None) -> ComposedGenerator:
    recipe = validate_generator_recipe_v2(value, contract=contract)
    recipe_hash = hashlib.sha256(canonical_json_bytes(recipe)).hexdigest()
    machine = recipe["machine"]["kind"]
    dispatch = "\n".join(_cpp_case(case, machine) for case in recipe["cases"])
    source = (
        "// ACM_LOCAL_RECIPE_GENERATOR_V2\n"
        "#include <algorithm>\n#include <cstddef>\n#include <cstdint>\n#include <iostream>\n#include <sstream>\n#include <stdexcept>\n#include <string>\n#include <utility>\n#include <vector>\n"
        f"// generator_recipe_sha256: {recipe_hash}\n// generator_catalog_sha256: {GENERATOR_RECIPE_V2_CATALOG_SHA256}\n"
        + _CPP_RUNTIME + "\n" + _SHA256_CPP
        + "\nnamespace acm_recipe_v2_generated {\nstatic acm_recipe_v2_local::Result build(unsigned long long seed,const std::string& profile,const std::string& case_kind){using namespace acm_recipe_v2_local;\n"
        + dispatch + '\nthrow std::invalid_argument("unsupported recipe profile/case_kind");}\n}\n'
        + "void acm_generate_case(unsigned long long seed,const std::string& profile,const std::string& case_kind,std::ostream& out){out<<acm_recipe_v2_generated::build(seed,profile,case_kind).data;}\n"
        + "void acm_generate_manifest(unsigned long long seed,const std::string& profile,const std::string& case_kind,std::ostream& out){const auto r=acm_recipe_v2_generated::build(seed,profile,case_kind);const auto bytes=acm_recipe_v2_local::wire_size(r.data);out<<\"{\\\"manifest_version\\\":2,\\\"profile\\\":\\\"\"<<profile<<\"\\\",\\\"case_kind\\\":\\\"\"<<case_kind<<\"\\\",\\\"seed\\\":\"<<seed<<\",\\\"input_sha256\\\":\\\"\"<<acm_recipe_sha256::wire_digest(r.data)<<\"\\\",\\\"dimensions\\\":{\\\"bytes\\\":\"<<bytes<<\"},\\\"coverage_tags\\\":[\\\"family:\"<<r.family<<\"\\\",\\\"state_machine:\"<<r.state<<\"\\\",\\\"byte_bucket:\"<<acm_recipe_v2_local::byte_bucket(bytes);for(const auto& tag:r.tags)out<<\"\\\",\\\"\"<<tag;out<<\"\\\"],\\\"records\\\":\"<<std::count(r.data.begin(),r.data.end(),'\\n')<<\",\\\"total_complexity\\\":\\\"linear_output\\\",\\\"engine\\\":\\\"local_templates_v2\\\",\\\"recipe_source\\\":\\\"deterministic_contract_shape_v2\\\",\\\"state_machine\\\":\\\"\"<<r.state<<\"\\\",\\\"section_family\\\":\\\"\"<<r.family<<\"\\\",\\\"operation_family\\\":\\\"\"<<r.operations<<\"\\\",\\\"planned_byte_bucket\\\":\"<<r.planned_bucket<<\",\\\"actual_byte_bucket\\\":\"<<acm_recipe_v2_local::byte_bucket(bytes)<<\"}\";}\n"
    )
    if re.search(r"\b(?:int|auto)\s+main\s*\(", source):
        raise RecipeValidationError("audited v2 template unexpectedly defines main")
    metadata = {
        **dict(recipe_v2_identity()),
        "recipe_sha256": recipe_hash,
        "recipe_source": GENERATOR_RECIPE_V2_SOURCE,
        "state_machine": machine,
        "section_family": "permutation_operation_stream" if machine == "mutable_permutation" else "string_interval_records",
        "hard_small_bytes": SMALL_RECIPE_HARD_MAX_BYTES,
    }
    return ComposedGenerator(
        source=source,
        recipe=MappingProxyType(recipe),
        recipe_sha256=recipe_hash,
        catalog_sha256=GENERATOR_RECIPE_V2_CATALOG_SHA256,
        composer_version=GENERATOR_RECIPE_V2_COMPOSER_VERSION,
        metadata=MappingProxyType(metadata),
    )


__all__ = [
    "GENERATOR_RECIPE_V2_CATALOG_SHA256",
    "GENERATOR_RECIPE_V2_COMPOSER_VERSION",
    "GENERATOR_RECIPE_V2_ENGINE",
    "GENERATOR_RECIPE_V2_SCHEMA_VERSION",
    "GENERATOR_RECIPE_V2_SOURCE",
    "compile_static_contract_v2",
    "compose_generator_recipe_v2",
    "recipe_v2_identity",
    "supports_static_contract_v2",
    "validate_generator_recipe_v2",
]
