"""Generator blueprint: structured case plan derived from a contract.

Validates and repairs the model's blueprint against the contract's declared
dimensions, tags and operations before any C++ is generated."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping
from .deepseek import DeepSeekError
from .stress_budget import PreparationBudget

from .stress_ai_schema import (
    _GENERATOR_CASE_ORDER,
    _OPTIONAL_GENERATOR_CASES,
    _REQUIRED_GENERATOR_CASES,
    _SUPPORTED_GENERATOR_CASES,
    GENERATOR_BLUEPRINT_SCHEMA_VERSION,
)
from .stress_ai_core import (
    _artifact_prefix,
    _effective_thinking,
    _generate_json,
    _generation_mode,
    _generation_policy,
    _retry_progress,
    _usage_add,
    StressPreparationError,
    StressProgress,
)
from .stress_ai_contract import _compact_generator_contract

def _blueprint_error(message: str, *, path: str = "") -> StressPreparationError:
    return StressPreparationError(
        "stress_blueprint_invalid",
        message,
        details={"path": path} if path else None,
    )


def _blueprint_string_list(
    value: Any,
    *,
    path: str,
    allow_empty: bool = True,
    limit: int = 64,
) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise _blueprint_error(f"generator blueprint 的 {path} 必须是字符串数组", path=path)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = str(item).strip() if isinstance(item, str) else ""
        if not text or len(text) > 120 or text in seen:
            raise _blueprint_error(
                f"generator blueprint 的 {path} 含空值、重复值或超长值",
                path=f"{path}[{index}]",
            )
        seen.add(text)
        result.append(text)
    if not allow_empty and not result:
        raise _blueprint_error(f"generator blueprint 的 {path} 不能为空", path=path)
    return result


def _blueprint_case_pair(value: Any) -> tuple[str, str] | None:
    if isinstance(value, str) and "/" in value:
        profile, case_kind = value.split("/", 1)
    elif isinstance(value, Mapping):
        profile = str(value.get("profile") or "")
        case_kind = str(value.get("case_kind") or "")
    else:
        return None
    return profile.strip().casefold(), case_kind.strip().casefold()


def _large_complexity_is_safe(value: str) -> bool:
    expression = value.strip().casefold().replace("×", "*")
    if not expression.startswith("o(") or not expression.endswith(")"):
        return False
    if any(
        marker in expression
        for marker in ("^2", "²", "quadratic", "指数", "exponential", "factorial", "2^")
    ):
        return False
    body = re.sub(r"\s+", "", expression[2:-1]).replace("_", "")
    variable = r"(?:outputsize|totaloutput|totalrecords|records?|items?|n|m|q)"
    term = rf"{variable}(?:\*?log{variable})?"
    # A sum of linear/n-log-n terms is allowed. Products of independent size
    # variables (n*m, n*q, ...) and per-record linear work are rejected.
    return re.fullmatch(rf"{term}(?:\+{term})*", body) is not None


def _normalize_generator_blueprint_shape(value: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize provider shape variants without inventing new semantics."""

    normalized = dict(value)
    raw_dimensions = value.get("dimensions")
    if isinstance(raw_dimensions, Mapping):
        dimensions: list[dict[str, Any]] = []
        for raw_name in sorted(raw_dimensions, key=lambda item: str(item)):
            name = str(raw_name).strip()
            raw_spec = raw_dimensions[raw_name]
            if isinstance(raw_spec, Mapping):
                dimension = dict(raw_spec)
            else:
                dimension = {"description": raw_spec}
            # The mapping key is authoritative. A nested/conflicting name must
            # not make equivalent provider JSON normalize differently.
            dimension["name"] = name
            dimensions.append(dimension)
        normalized["dimensions"] = dimensions
    # ``required_coverage_tags`` and ``operation_families`` already declare
    # the semantic obligations of the 16-seed small/random collection.  Some
    # providers repeat only a subset in the nested case even though the prompt
    # defines that field as the collection manifest.  Propagating existing
    # top-level strings is lossless canonicalization; the later manifest union
    # still proves that generated code actually realizes every obligation.
    raw_cases = value.get("cases")
    required_tags = value.get("required_coverage_tags")
    operation_families = value.get("operation_families")
    if isinstance(raw_cases, list):
        cases = [dict(case) if isinstance(case, Mapping) else case for case in raw_cases]
        for case in cases:
            if not isinstance(case, dict) or _blueprint_case_pair(case) != (
                "small",
                "random",
            ):
                continue
            for field, declared in (
                ("coverage_tags", required_tags),
                ("operation_families", operation_families),
            ):
                nested = case.get(field)
                if not isinstance(nested, list) or not nested or not isinstance(declared, list):
                    continue
                merged = list(nested)
                for item in declared:
                    if item not in merged:
                        merged.append(item)
                case[field] = merged
        normalized["cases"] = cases
    return normalized


def _bind_structured_blueprint_defaults(
    value: Mapping[str, Any], contract: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind fields that are exact projections of a structured contract.

    The model still chooses concrete per-case dimensions and construction.
    Fixed protocol combinations, operation variants, seed flags and coverage
    obligation placement are deterministic contract facts and are safer to
    compute locally than to ask the provider to repeat verbatim.
    """

    bound = dict(value)
    syntax = contract.get("syntax")
    sections = (
        syntax.get("sections", []) if isinstance(syntax, Mapping) else []
    )
    operation_families: list[str] = []
    dimension_names: list[str] = []
    dimension_ranges: dict[str, tuple[int | None, int | None]] = {}
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        for variant in section.get("variants", []):
            if not isinstance(variant, Mapping):
                continue
            tag = str(variant.get("tag") or "").strip()
            if tag and tag not in operation_families:
                operation_families.append(tag)
        if str(section.get("kind") or "") != "scalar":
            continue
        for field in section.get("fields", []):
            if not isinstance(field, Mapping):
                continue
            name = str(field.get("name") or "").strip()
            if name and name not in dimension_names:
                dimension_names.append(name)
    if not dimension_names:
        for constraint in contract.get("constraints", []):
            if not isinstance(constraint, Mapping):
                continue
            target = str(constraint.get("target") or "").strip()
            name = target.rsplit(".", 1)[-1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or "") and name not in dimension_names:
                dimension_names.append(name)
    for constraint in contract.get("constraints", []):
        if not isinstance(constraint, Mapping) or constraint.get("kind") != "range":
            continue
        name = str(constraint.get("target") or "").rsplit(".", 1)[-1]
        if name not in dimension_names:
            continue
        raw_args = constraint.get("args")
        args = dict(raw_args) if isinstance(raw_args, Mapping) else {}
        minimum = args.get("minimum")
        maximum = args.get("maximum")
        dimension_ranges[name] = (
            minimum if type(minimum) is int else None,
            maximum if type(maximum) is int else None,
        )
    operation_count_dimensions = {
        str(section.get("count_from") or "").rsplit(".", 1)[-1]
        for section in sections
        if isinstance(section, Mapping)
        and str(section.get("kind") or "") == "operation_stream"
        and str(section.get("count_from") or "").strip()
    }
    bound["schema_version"] = GENERATOR_BLUEPRINT_SCHEMA_VERSION
    bound["required_cases"] = [
        {"profile": profile, "case_kind": case_kind}
        for profile, case_kind in _REQUIRED_GENERATOR_CASES
    ]
    if dimension_names:
        bound["dimensions"] = [{"name": name} for name in dimension_names]
    if operation_families:
        bound["operation_families"] = operation_families
    else:
        bound.setdefault("operation_families", [])
    bound.setdefault("required_coverage_tags", [])
    bound.setdefault("large_required_coverage_tags", [])
    large_profile_text = str(contract.get("large_profile") or "")

    def mentioned_in_large_profile(operation: str) -> bool:
        if not operation:
            return False
        if operation.isascii():
            return re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(operation)}(?![A-Za-z0-9_])",
                large_profile_text,
                flags=re.IGNORECASE,
            ) is not None
        return operation.casefold() in large_profile_text.casefold()

    # Per-case operation metadata is an implementation constraint presented to
    # the code generator.  Small/random owns exhaustive operation coverage;
    # large/random should contain only the operation families explicitly used
    # by the contract's large construction.  Forcing every modifying operation
    # into large made safe streaming profiles unnecessarily stateful and could
    # turn O(output_size) generation into O(n*m).
    large_profile_operations = [
        operation
        for operation in operation_families
        if mentioned_in_large_profile(operation)
    ]
    raw_cases = bound.get("cases")
    if isinstance(raw_cases, list):
        cases: list[Any] = []
        for raw_case in raw_cases:
            if not isinstance(raw_case, Mapping):
                cases.append(raw_case)
                continue
            case = dict(raw_case)
            pair = _blueprint_case_pair(case)
            if pair is not None:
                raw_dimensions = case.get("dimensions")
                dimensions = (
                    dict(raw_dimensions) if isinstance(raw_dimensions, Mapping) else {}
                )
                for name in dimension_names:
                    minimum, maximum = dimension_ranges.get(name, (None, None))
                    supplied = dimensions.get(name)
                    supplied = supplied if type(supplied) is int else None
                    if pair == ("small", "lower_bound"):
                        selected = minimum if minimum is not None else supplied
                    elif pair == ("large", "upper_bound"):
                        selected = maximum if maximum is not None else supplied
                    elif pair == ("small", "random"):
                        cap = 20 if name in operation_count_dimensions else 8
                        if minimum is not None:
                            cap = max(cap, minimum)
                        selected = supplied if supplied is not None else cap
                        selected = min(selected, cap)
                        if minimum is not None:
                            selected = max(selected, minimum)
                        if maximum is not None:
                            selected = min(selected, maximum)
                    else:
                        selected = maximum if maximum is not None else supplied
                    if selected is not None:
                        dimensions[name] = selected
                if dimensions:
                    case["dimensions"] = dimensions
                    authority = "Authoritative dimensions: " + ", ".join(
                        f"{name}={dimensions[name]}"
                        for name in dimension_names
                        if name in dimensions
                    )
                    construction = str(
                        case.get("construction") or case.get("strategy") or ""
                    ).strip()
                    # Free-form profile prose and blueprint constructions are
                    # unverified candidate strategies.  Treating either as an
                    # immutable operation skeleton made source repair impossible
                    # when an independently generated contract happened to put a
                    # stateful operation after an incompatible transition.  Only
                    # the locally bound dimensions, record count, operation
                    # families and coverage obligations are authoritative.
                    construction = (
                        "Candidate strategy: "
                        + construction
                        + ". Re-simulate stateful records in final output order; "
                        "replace illegal concrete arguments or ordering while preserving "
                        "dimensions, declared record count, required operation families "
                        "and coverage."
                    )
                    case["construction"] = (
                        authority + "; " + construction
                    )[:800]
                if pair[1] == "random":
                    safe_seed_families = ", ".join(large_profile_operations)
                    random_policy = (
                        "Keep the emitted initial state deterministic. Build and sequentially "
                        "simulate a legal skeleton for every state-changing operation. If a "
                        "candidate concrete record is illegal, substitute its argument or "
                        "order before emission while preserving the structured obligations. "
                        "After the skeleton is legal, never randomly shuffle or reorder it. "
                        "Seed must change at least one emitted read-only or unconditionally "
                        "legal semantic field after the skeleton is valid, but must not "
                        "change state-dependent operation identifiers or parameters. "
                        + (
                            "The contract-derived safe seed families are: "
                            + safe_seed_families
                            + ". Assign at least one actually emitted argument in one of "
                            "those families from the consumed PRNG."
                            if safe_seed_families
                            else "Assign one actually emitted, independently legal field "
                            "from the consumed PRNG."
                        )
                        if pair[0] == "small"
                        else "Stream only read-only or unconditionally legal operations; "
                        "avoid state-dependent mutations unless the contract provides a "
                        "no-op parameter. Seed changes only safe read-only fields."
                    )
                    case["construction"] = (
                        random_policy + " " + str(case.get("construction") or "")
                    )[:800]
                case["uses_seed"] = pair[1] == "random"
                # Complexity is a harness policy field, not problem semantics.
                # Bind it locally so a structurally correct blueprint is not
                # rejected merely because the model paraphrased the only two
                # accepted spellings.  The generated source is still checked
                # independently by compilation, runtime limits and audit.
                case["total_complexity"] = (
                    "O(output_size log n)"
                    if pair == ("large", "random")
                    else "O(output_size)"
                )
                if operation_families:
                    case["operation_families"] = (
                        list(operation_families)
                        if pair == ("small", "random")
                        else list(large_profile_operations)
                        if pair == ("large", "random")
                        else []
                    )
                else:
                    case.setdefault("operation_families", [])
                case["coverage_tags"] = []
            cases.append(case)
        bound["cases"] = cases
    return bound


def validate_generator_blueprint(
    value: Any,
    *,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and canonicalize the provider's generator blueprint locally."""
    if not isinstance(value, Mapping):
        raise _blueprint_error("generator blueprint 必须是 JSON 对象")
    structured_contract = bool(
        contract is not None
        and str(contract.get("validation_level") or "") == "structured"
    )
    if structured_contract:
        value = _bind_structured_blueprint_defaults(value, contract)
    value = _normalize_generator_blueprint_shape(value)
    try:
        encoded_size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise _blueprint_error("generator blueprint 必须只包含 JSON 值") from None
    if encoded_size > 128 * 1024:
        raise _blueprint_error("generator blueprint 超过本地大小上限")
    if value.get("schema_version") != GENERATOR_BLUEPRINT_SCHEMA_VERSION:
        raise _blueprint_error("generator blueprint schema_version 必须为 1", path="schema_version")

    raw_dimensions = value.get("dimensions")
    if not isinstance(raw_dimensions, list) or not raw_dimensions or len(raw_dimensions) > 32:
        raise _blueprint_error("generator blueprint dimensions 必须是非空数组", path="dimensions")
    dimensions: list[dict[str, Any]] = []
    dimension_names: set[str] = set()
    for index, item in enumerate(raw_dimensions):
        if isinstance(item, str):
            normalized = {"name": item.strip()}
        elif isinstance(item, Mapping):
            normalized = dict(item)
            normalized["name"] = str(item.get("name") or "").strip()
        else:
            raise _blueprint_error("dimension 必须是字符串或对象", path=f"dimensions[{index}]")
        name = str(normalized.get("name") or "")
        if not name or len(name) > 80 or name in dimension_names:
            raise _blueprint_error("dimension name 为空、重复或超长", path=f"dimensions[{index}].name")
        dimension_names.add(name)
        dimensions.append(normalized)

    operation_families = _blueprint_string_list(
        value.get("operation_families"), path="operation_families"
    )
    structured_binding = structured_contract or value.get(
        "coverage_binding_version"
    ) == 1
    required_tags = _blueprint_string_list(
        value.get("required_coverage_tags"),
        path="required_coverage_tags",
        allow_empty=structured_binding,
        limit=128,
    )
    large_required_tags = _blueprint_string_list(
        value.get("large_required_coverage_tags"),
        path="large_required_coverage_tags",
        limit=128,
    )
    contract_tags_by_case: dict[tuple[str, str], list[str]] = {
        pair: [] for pair in _GENERATOR_CASE_ORDER
    }
    operation_count_dimensions: set[str] = set()
    legal_minimums: dict[str, int] = {}
    if structured_contract:
        structured_syntax = contract.get("syntax")
        structured_sections = (
            structured_syntax.get("sections", [])
            if isinstance(structured_syntax, Mapping)
            else []
        )
        for section in structured_sections:
            if not isinstance(section, Mapping) or section.get("kind") != "operation_stream":
                continue
            count_from = str(section.get("count_from") or "")
            name = count_from.rsplit(".", 1)[-1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
                operation_count_dimensions.add(name)
        for constraint in contract.get("constraints", []):
            if not isinstance(constraint, Mapping) or constraint.get("kind") != "range":
                continue
            name = str(constraint.get("target") or "").rsplit(".", 1)[-1]
            args = constraint.get("args")
            args = dict(args) if isinstance(args, Mapping) else {}
            minimum = args.get("minimum")
            if type(minimum) is int and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", name or ""
            ):
                legal_minimums[name] = minimum
        for obligation in contract.get("coverage_obligations", []):
            if not isinstance(obligation, Mapping):
                continue
            obligation_id = str(obligation.get("id") or "").strip()
            predicate = obligation.get("predicate")
            predicate = dict(predicate) if isinstance(predicate, Mapping) else {}
            args = predicate.get("args")
            args = dict(args) if isinstance(args, Mapping) else {}
            scope = str(obligation.get("scope") or "small").casefold()
            side = str(args.get("side") or "").casefold()
            if not obligation_id:
                continue
            if predicate.get("kind") == "constraint_boundary" and side == "minimum":
                pair = ("small", "lower_bound")
            elif predicate.get("kind") == "constraint_boundary" and side == "maximum":
                pair = ("large", "upper_bound")
            elif scope == "large":
                pair = ("large", "random")
            else:
                pair = ("small", "random")
            if obligation_id not in contract_tags_by_case[pair]:
                contract_tags_by_case[pair].append(obligation_id)
        # Structured obligation ids are the only tags an independent validator
        # can emit.  Seed sensitivity is proved separately from output hashes;
        # never manufacture a pseudo coverage tag for it.
        required_tags = list(contract_tags_by_case[("small", "random")])
        large_required_tags = list(contract_tags_by_case[("large", "random")])

    raw_required_cases = value.get("required_cases")
    if raw_required_cases is not None:
        if not isinstance(raw_required_cases, list):
            raise _blueprint_error("required_cases 必须是数组", path="required_cases")
        required_pairs = [_blueprint_case_pair(item) for item in raw_required_cases]
        accepted_required_sets = (
            set(_REQUIRED_GENERATOR_CASES),
            set(_GENERATOR_CASE_ORDER),
        )
        if (
            len(required_pairs) != len(set(required_pairs))
            or set(required_pairs) not in accepted_required_sets
        ):
            raise _blueprint_error(
                "required_cases 必须包含四个 profile-v2 组合",
                path="required_cases",
            )

    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) not in {3, 4}:
        raise _blueprint_error(
            "cases 必须包含四个 profile-v2 case",
            path="cases",
        )
    cases: list[dict[str, Any]] = []
    seen_cases: set[tuple[str, str]] = set()
    covered_tags: set[str] = set()
    large_covered_tags: set[str] = set()
    covered_operations: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise _blueprint_error("case 必须是对象", path=f"cases[{index}]")
        case = dict(raw_case)
        pair = _blueprint_case_pair(case)
        if pair not in _SUPPORTED_GENERATOR_CASES or pair in seen_cases:
            raise _blueprint_error(
                "cases 只能覆盖四个 profile-v2 组合，且不得重复",
                path=f"cases[{index}]",
            )
        assert pair is not None
        profile, case_kind = pair
        seen_cases.add(pair)
        case["profile"] = profile
        case["case_kind"] = case_kind

        case_dimensions = case.get("dimensions")
        if isinstance(case_dimensions, Mapping):
            case_dimension_names = {str(key) for key in case_dimensions}
            case["dimensions"] = dict(case_dimensions)
        elif isinstance(case_dimensions, list):
            normalized_case_dimensions = _blueprint_string_list(
                case_dimensions, path=f"cases[{index}].dimensions", allow_empty=False
            )
            case_dimension_names = set(normalized_case_dimensions)
            case["dimensions"] = normalized_case_dimensions
        else:
            raise _blueprint_error(
                "case dimensions 必须是对象或名称数组", path=f"cases[{index}].dimensions"
            )
        missing_dimensions = dimension_names - case_dimension_names
        if missing_dimensions:
            raise _blueprint_error(
                "case dimensions 未覆盖所有顶层维度",
                path=f"cases[{index}].dimensions",
            )
        if structured_contract and profile == "small" and isinstance(
            case["dimensions"], Mapping
        ):
            for name, raw_dimension in case["dimensions"].items():
                if type(raw_dimension) is not int:
                    continue
                policy_cap = 20 if name in operation_count_dimensions else 8
                policy_cap = max(policy_cap, legal_minimums.get(str(name), policy_cap))
                if raw_dimension > policy_cap:
                    raise _blueprint_error(
                        f"small case 维度 {name}={raw_dimension} 超过本地上限 {policy_cap}",
                        path=f"cases[{index}].dimensions.{name}",
                    )

        case_operations = _blueprint_string_list(
            case.get("operation_families"), path=f"cases[{index}].operation_families"
        )
        if not set(case_operations).issubset(operation_families):
            raise _blueprint_error(
                "case 引用了未声明的 operation family",
                path=f"cases[{index}].operation_families",
            )
        declared_case_tags = _blueprint_string_list(
            case.get("coverage_tags"),
            path=f"cases[{index}].coverage_tags",
            allow_empty=structured_binding,
            limit=128,
        )
        case_tags = (
            list(contract_tags_by_case[pair])
            if structured_contract
            else declared_case_tags
        )
        uses_seed = case.get("uses_seed")
        if case_kind == "random" and uses_seed is not True:
            raise _blueprint_error(
                "random case 必须显式 uses_seed=true",
                path=f"cases[{index}].uses_seed",
            )
        if uses_seed not in {None, True, False}:
            raise _blueprint_error("uses_seed 必须是布尔值", path=f"cases[{index}].uses_seed")
        case["uses_seed"] = bool(uses_seed)
        construction = str(case.get("construction") or case.get("strategy") or "").strip()
        if not construction:
            raise _blueprint_error("case construction 不能为空", path=f"cases[{index}].construction")
        # Construction is non-binding planning prose.  Retaining an unbounded
        # essay only increases the following code-generation prompt.
        construction = construction[:800]
        case["construction"] = construction
        case.pop("strategy", None)
        complexity = str(case.get("total_complexity") or "").strip()
        if not complexity:
            raise _blueprint_error("case total_complexity 不能为空", path=f"cases[{index}].total_complexity")
        if profile == "large" and not _large_complexity_is_safe(complexity):
            raise _blueprint_error(
                "large case total_complexity 必须是相对输出规模的线性或 n-log-n 总复杂度",
                path=f"cases[{index}].total_complexity",
            )
        case["total_complexity"] = complexity
        case["operation_families"] = case_operations
        case["coverage_tags"] = case_tags
        covered_operations.update(case_operations)
        covered_tags.update(case_tags)
        if profile == "large":
            large_covered_tags.update(case_tags)
        cases.append(case)

    if not set(_REQUIRED_GENERATOR_CASES).issubset(seen_cases):
        raise _blueprint_error("cases 未闭合四个 profile-v2 组合", path="cases")
    cases_by_pair = {
        (str(case["profile"]), str(case["case_kind"])): case for case in cases
    }
    cases = [
        cases_by_pair[pair]
        for pair in _GENERATOR_CASE_ORDER
        if pair in cases_by_pair
    ]
    small_random = next(
        case
        for case in cases
        if case["profile"] == "small" and case["case_kind"] == "random"
    )
    if not set(operation_families).issubset(small_random["operation_families"]):
        raise _blueprint_error(
            "small/random 必须覆盖全部 operation family",
            path="cases[small/random].operation_families",
        )
    if not set(required_tags).issubset(small_random["coverage_tags"]):
        raise _blueprint_error(
            "small/random 的 16 个 seed 覆盖并集必须声明全部 required coverage tag",
            path="cases[small/random].coverage_tags",
        )
    random_description = str(small_random.get("construction") or "").casefold()
    if any(
        phrase in random_description
        for phrase in ("无需随机", "不使用 seed", "不使用seed", "no randomness", "ignore seed")
    ):
        raise _blueprint_error(
            "small/random construction 与 uses_seed=true 自相矛盾",
            path="cases[small/random].construction",
        )
    query_prefixes = ("ask", "query", "get", "count", "询问", "查询")
    query_operations = {
        operation
        for operation in operation_families
        if operation.strip().casefold().startswith(query_prefixes)
    }
    modifying_operations = set(operation_families) - query_operations
    if not structured_binding and query_operations and modifying_operations:
        large_random = next(
            case
            for case in cases
            if case["profile"] == "large" and case["case_kind"] == "random"
        )
        large_operations = set(large_random["operation_families"])
        if not (large_operations & query_operations) or not (
            large_operations & modifying_operations
        ):
            raise _blueprint_error(
                "large/random 必须同时包含主要修改与查询 operation family",
                path="cases[large/random].operation_families",
            )
    if contract is not None:
        if not structured_contract:
            # Legacy prose contracts have no typed operation variants, so a
            # narrow literal check remains useful.  Never apply it to v3:
            # profile examples such as ``Insert 3 -1`` would otherwise treat
            # the book id as a required operation family named ``Insert 3``.
            contract_text = json.dumps(
                dict(contract), ensure_ascii=False, sort_keys=True
            )
            blueprint_text = json.dumps(
                value, ensure_ascii=False, sort_keys=True
            )
            required_literals = re.findall(
                r"(?i)\b(?:Top|Bottom|Ask|Query)\b|Insert\s+(?:t\s*=\s*)?[+-]?\d+",
                contract_text,
            )
            def normalize(text: str) -> str:
                return re.sub(r"\s+|t\s*=\s*", "", text.casefold())
            normalized_blueprint = normalize(blueprint_text)
            missing_literals = sorted(
                {
                    literal
                    for literal in required_literals
                    if normalize(literal) not in normalized_blueprint
                }
            )
            if missing_literals:
                raise _blueprint_error(
                    "generator blueprint 遗漏 contract 中的离散操作/参数："
                    + "、".join(missing_literals),
                    path="operation_families",
                )
        if structured_contract:
            structured_syntax = contract.get("syntax")
            structured_sections = (
                structured_syntax.get("sections", [])
                if isinstance(structured_syntax, Mapping)
                else []
            )
            declared_variants = {
                str(variant.get("tag") or "").strip()
                for section in structured_sections
                if isinstance(section, Mapping)
                and section.get("kind") == "operation_stream"
                for variant in section.get("variants", [])
                if isinstance(variant, Mapping)
                and str(variant.get("tag") or "").strip()
            }
            missing_variants = declared_variants - set(operation_families)
            if missing_variants:
                raise _blueprint_error(
                    "generator recipe 遗漏 contract-v3 operation variants："
                    + "、".join(sorted(missing_variants)),
                    path="operation_families",
                )
    missing_operations = set(operation_families) - covered_operations
    missing_tags = set(required_tags) - covered_tags
    missing_large_tags = set(large_required_tags) - large_covered_tags
    if missing_operations or missing_tags or missing_large_tags:
        raise _blueprint_error(
            "generator blueprint coverage 未闭合",
            path="cases",
        )
    result = {
        "schema_version": GENERATOR_BLUEPRINT_SCHEMA_VERSION,
        "required_cases": [
            {"profile": profile, "case_kind": case_kind}
            for profile, case_kind in _REQUIRED_GENERATOR_CASES
        ],
        "dimensions": dimensions,
        "operation_families": operation_families,
        "required_coverage_tags": required_tags,
        "large_required_coverage_tags": large_required_tags,
        "cases": cases,
    }
    if structured_binding:
        result["coverage_binding_version"] = 1
    return result


def generate_generator_blueprint(
    client: Any,
    *,
    problem_id: str,
    statement: str,
    contract: Mapping[str, Any],
    settings: Mapping[str, Any],
    generation_mode: str | None = None,
    diagnostic: str = "",
    previous_blueprint: Mapping[str, Any] | None = None,
    repair_limit: int = 0,
    provider_reserve_seconds: float = 0.0,
    budget: PreparationBudget | None = None,
    progress_callback: StressProgress | None = None,
    cancel_scope: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if type(repair_limit) is not int or repair_limit < 0:
        raise ValueError("repair_limit must be a non-negative integer")
    mode = _generation_mode(settings, generation_mode)
    base_messages = _artifact_prefix(
        problem_id=problem_id,
        statement=statement,
        contract=_compact_generator_contract(contract),
        include_statement=False,
    )
    base_messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "type": "acm_stress_generator_blueprint_v1",
                    "instructions": (
                        "只规划四个必需 generator case，不生成源码。返回紧凑 JSON；顶层只需"
                        "schema_version=1 和 cases。每个 case 只需 profile、case_kind、dimensions、"
                        "construction、total_complexity；construction 最多 240 字，写清具体规模、"
                        "合法操作骨架和 seed 改变的安全输出字段，不写证明长文。random 必须消费"
                        "seed；状态相关操作按最终输出顺序维护，禁止验证后再 shuffle。large 总构造"
                        "复杂度只能为 O(output_size) 或 O(output_size log n)，不得逐操作线性更新。"
                        "required_cases、维度名、operation families、coverage tags 和 uses_seed"
                        "均由本地从 contract-v3 精确绑定，不要重复输出。"
                    ),
                    "fixed_cases": [
                        {"profile": profile, "case_kind": case_kind}
                        for profile, case_kind in _REQUIRED_GENERATOR_CASES
                    ],
                    "optional_compatibility_cases": [
                        {"profile": profile, "case_kind": case_kind}
                        for profile, case_kind in _OPTIONAL_GENERATOR_CASES
                    ],
                    "case_shape": {
                        "profile": "small|large",
                        "case_kind": "lower_bound|upper_bound|random",
                        "dimensions": {"contract_dimension": "concrete integer"},
                        "construction": "<=240 chars",
                        "total_complexity": "O(output_size)|O(output_size log n)",
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    )

    def parse_diagnostic(raw: str) -> Any:
        try:
            return json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return str(raw or "")[:4000]

    prior_raw: Any = (
        dict(previous_blueprint) if previous_blueprint is not None else None
    )
    repair_diagnostic: dict[str, Any] | None = None
    repairs_used = 1 if previous_blueprint is not None else 0
    automatic_repairs_used = 0
    if previous_blueprint is not None:
        supplied = parse_diagnostic(diagnostic)
        repair_diagnostic = {
            "path": (
                str(supplied.get("path") or "")
                if isinstance(supplied, Mapping)
                else ""
            ),
            "message": (
                str(supplied.get("message") or supplied)[:4000]
                if isinstance(supplied, Mapping)
                else str(supplied)[:4000]
            ),
            "source": "caller",
            "details": supplied,
        }

    usage: dict[str, Any] = {}
    attempts = 0
    while True:
        is_repair = prior_raw is not None
        requested_thinking, max_tokens = _generation_policy(
            mode, "blueprint", repair=is_repair
        )
        thinking = _effective_thinking(
            mode,
            requested_thinking,
            budget,
            stage="generate_blueprint",
            provider_reserve_seconds=provider_reserve_seconds,
        )
        messages = list(base_messages)
        if prior_raw is not None:
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "type": "acm_stress_generator_blueprint_repair_v1",
                            "instructions": (
                                "只修正结构化 path/message 指出的 recipe 字段并返回完整紧凑 JSON；"
                                "不得生成源码或增加固定四组合之外的 case，construction 不超过240字。"
                            ),
                            "structured_diagnostic": repair_diagnostic,
                            "previous_raw_json": prior_raw,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
        try:
            result = _generate_json(
                client,
                messages,
                settings,
                budget=budget,
                stage="generate_blueprint",
                soft_stage="prepare_helpers",
                max_tokens=max_tokens,
                thinking=thinking,
                provider_reserve_seconds=provider_reserve_seconds,
                request_retries=0 if requested_thinking and not thinking else 1,
                json_retries=0 if requested_thinking and not thinking else 1,
                retry_callback=_retry_progress(
                    progress_callback,
                    "generate_generator",
                    "规划 generator blueprint",
                    3,
                ),
                cancel_scope=cancel_scope,
            )
        except DeepSeekError as exc:
            _usage_add(usage, dict(getattr(exc, "usage", {}) or {}))
            if (
                str(getattr(exc, "code", "") or "") == "invalid_json_output"
                and automatic_repairs_used < repair_limit
            ):
                automatic_repairs_used += 1
                repairs_used += 1
                prior_raw = {}
                repair_diagnostic = {
                    "code": "invalid_json_output",
                    "path": "$",
                    "message": str(exc)[:4000],
                    "required_change": (
                        "上一响应因长度或 JSON 协议失败；使用 repair token budget "
                        "重新返回完整、紧凑的 blueprint JSON。"
                    ),
                }
                continue
            usage["blueprint_repairs_used"] = repairs_used
            try:
                exc.usage = dict(usage)
            except Exception:
                pass
            raise
        except Exception as exc:
            _usage_add(usage, dict(getattr(exc, "usage", {}) or {}))
            usage["blueprint_repairs_used"] = repairs_used
            try:
                exc.usage = dict(usage)
            except Exception:
                pass
            raise
        attempts += 1
        _usage_add(usage, dict(getattr(result, "usage", {}) or {}))
        try:
            blueprint = validate_generator_blueprint(result.data, contract=contract)
        except StressPreparationError as exc:
            path = str(exc.details.get("path") or "")
            if automatic_repairs_used >= repair_limit:
                exc.details.update(
                    {
                        "path": path,
                        "attempts": attempts,
                        "repairs_used": repairs_used,
                    }
                )
                usage["blueprint_repairs_used"] = repairs_used
                exc.usage = dict(usage)
                raise
            automatic_repairs_used += 1
            repairs_used += 1
            prior_raw = result.data
            repair_diagnostic = {
                "code": exc.code,
                "path": path,
                "message": str(exc)[:4000],
                "attempt": attempts,
            }
            if path == "cases[small/random].coverage_tags":
                repair_diagnostic["required_change"] = (
                    "完整复制顶层 required_coverage_tags 到 small/random.coverage_tags；"
                    "若其中存在仅 large 可满足的规模 tag，先从 required_coverage_tags 移到 "
                    "large_required_coverage_tags。不得遗漏任何其余 tag。"
                )
            elif path == "cases[small/random].operation_families":
                repair_diagnostic["required_change"] = (
                    "完整复制顶层 operation_families 到 small/random.operation_families。"
                )
            continue
        usage["blueprint_repairs_used"] = repairs_used
        if requested_thinking and not thinking:
            usage["fast_fallback_used"] = True
        return blueprint, usage
