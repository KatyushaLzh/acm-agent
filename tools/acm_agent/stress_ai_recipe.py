"""Model-facing generator recipe generation.

Wraps the strict local template compiler in :mod:`stress_recipe` with the
provider call, prompt serialization and repair loop. The compiler itself
stays local and deterministic; only the recipe JSON comes from the model."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Sequence
from .stress_budget import PreparationBudget
from .stress_recipe import (
    _repair_random_simple_graph_domain,
    _SEMANTIC_GOALS,
    compose_generator_recipe,
    GENERATOR_RECIPE_ENGINE,
    RecipeCatalog,
    RecipeError,
    static_contract_capabilities,
    supports_static_contract,
    UnsupportedRecipeError,
    validate_generator_recipe as validate_local_generator_recipe,
)
from .stress_recipe_v2 import (
    GENERATOR_RECIPE_V2_ENGINE,
    compose_generator_recipe_v2,
    validate_generator_recipe_v2,
)

from .stress_ai_schema import _GENERATOR_CASE_ORDER
from .stress_ai_core import (
    _artifact_prefix,
    _effective_thinking,
    _generate_json,
    _generation_mode,
    _generation_policy,
    _retry_progress,
    _usage_add,
    GeneratedArtifact,
    StressPreparationError,
    StressProgress,
)
from .stress_ai_contract import _compact_generator_contract
from .stress_ai_blueprint import validate_generator_blueprint

def validate_generator_recipe(
    value: Any, *, contract: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validate the new local-template recipe or a historical blueprint."""

    if isinstance(value, Mapping) and value.get("engine") == GENERATOR_RECIPE_ENGINE:
        return validate_local_generator_recipe(value, contract=contract)
    if isinstance(value, Mapping) and value.get("engine") == GENERATOR_RECIPE_V2_ENGINE:
        return validate_generator_recipe_v2(value, contract=contract)
    return validate_generator_blueprint(value, contract=contract)


def _recipe_prompt_serialization_error(
    *, path: str, cause_type: str, message: str | None = None
) -> StressPreparationError:
    return StressPreparationError(
        "stress_recipe_prompt_serialization_failed",
        message or "generator recipe prompt 包含不可序列化的本地值",
        details={
            "category": "internal",
            "failure_phase": "preparation",
            "stage": "prepare_generator",
            "substage": "recipe_prompt_serialization",
            "path": path,
            "cause_type": cause_type,
        },
    )


def _recipe_prompt_json_value(value: Any, *, path: str = "$") -> Any:
    """Project immutable catalog data into strict JSON-native containers.

    Catalog mappings deliberately use ``MappingProxyType`` so local template
    metadata stays read-only.  The model boundary must recursively materialize
    those mappings instead of relying on ``json.dumps(default=...)``, which
    would silently change the prompt schema when a new unsupported type leaks
    into the catalog projection.
    """

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _recipe_prompt_serialization_error(
                    path=path,
                    cause_type=type(key).__name__,
                    message="generator recipe prompt object key 必须是字符串",
                )
            result[key] = _recipe_prompt_json_value(
                item, path=f"{path}.{key}" if path != "$" else f"$.{key}"
            )
        return result
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _recipe_prompt_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise _recipe_prompt_serialization_error(
        path=path, cause_type=type(value).__name__
    )


def _generator_recipe_prompt_content(payload: Mapping[str, Any]) -> str:
    projected = _recipe_prompt_json_value(payload)
    try:
        return json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _recipe_prompt_serialization_error(
            path="$",
            cause_type=type(exc).__name__,
            message="generator recipe prompt JSON 编码失败",
        ) from exc


def _normalize_recipe_case_identity(case: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize a narrow, closed set of provider enum aliases."""

    normalized = dict(case)
    profile_aliases = {
        "small": "small",
        "small_profile": "small",
        "tiny": "small",
        "large": "large",
        "large_profile": "large",
        "stress": "large",
    }
    kind_aliases = {
        "lower_bound": "lower_bound",
        "lower": "lower_bound",
        "minimum": "lower_bound",
        "min": "lower_bound",
        "boundary_min": "lower_bound",
        "upper_bound": "upper_bound",
        "upper": "upper_bound",
        "maximum": "upper_bound",
        "max": "upper_bound",
        "boundary_max": "upper_bound",
        "random": "random",
        "seeded_random": "random",
        "randomized": "random",
    }
    profile = normalized.get("profile")
    case_kind = normalized.get("case_kind")
    if isinstance(profile, str):
        profile_key = profile.strip().casefold().replace("-", "_").replace(" ", "_")
        normalized["profile"] = profile_aliases.get(profile_key, profile)
    if isinstance(case_kind, str):
        kind_key = case_kind.strip().casefold().replace("-", "_").replace(" ", "_")
        normalized["case_kind"] = kind_aliases.get(kind_key, case_kind)
    return normalized


def _canonicalize_recipe_case_slots(raw_cases: Sequence[Any]) -> list[Any]:
    """Own the four fixed recipe slots locally when provider enums drift."""

    normalized = [
        _normalize_recipe_case_identity(case) if isinstance(case, Mapping) else case
        for case in raw_cases
    ]
    if len(normalized) != len(_GENERATOR_CASE_ORDER):
        return normalized
    expected = list(_GENERATOR_CASE_ORDER)
    supported = set(expected)
    seen: set[tuple[str, str]] = set()
    result: list[Any] = []
    for index, case in enumerate(normalized):
        if not isinstance(case, Mapping):
            result.append(case)
            continue
        local_case = dict(case)
        pair = (local_case.get("profile"), local_case.get("case_kind"))
        if pair not in supported or pair in seen:
            preferred = expected[index]
            replacement = preferred if preferred not in seen else next(
                (candidate for candidate in expected if candidate not in seen),
                preferred,
            )
            local_case["profile"], local_case["case_kind"] = replacement
            pair = replacement
        seen.add(pair)  # type: ignore[arg-type]
        result.append(local_case)
    return result


def _normalize_recipe_semantic_goal(value: Any, *, case_kind: str) -> Any:
    """Project common structured provider intent onto the closed goal enum."""

    if isinstance(value, Mapping):
        template_id = str(value.get("template_id") or "").casefold()
        policy = str(value.get("policy") or "").casefold()
        kind = str(value.get("kind") or "").casefold()
        if template_id.endswith(".equal") or policy == "equal":
            return "equal_labels"
        if template_id.endswith((".distinct", ".permutation")) or policy in {
            "distinct", "permutation"
        }:
            return "distinct_labels"
        if kind in {"random", "seeded_random", "uniform"} or policy in {
            "random", "uniform"
        }:
            return "seed_variation"
        for key in ("goal", "name", "id"):
            if isinstance(value.get(key), str):
                return _normalize_recipe_semantic_goal(
                    value[key], case_kind=case_kind
                )
        return {
            "lower_bound": "lower_bound",
            "upper_bound": "upper_bound",
            "random": "seed_variation",
        }.get(case_kind, value)
    if not isinstance(value, str):
        return value
    key = value.strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "random": "seed_variation",
        "seeded_random": "seed_variation",
        "label.equal": "equal_labels",
        "equal": "equal_labels",
        "label.distinct": "distinct_labels",
        "label.permutation": "distinct_labels",
        "distinct": "distinct_labels",
        "minimum": "lower_bound",
        "maximum": "upper_bound",
    }
    return aliases.get(key, value)


def _contract_explicitly_allows_self_loops(
    statement: str, contract: Mapping[str, Any]
) -> bool:
    texts = [statement]
    evidence = contract.get("evidence")
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)):
        texts.extend(
            str(item.get("quote") or "")
            for item in evidence
            if isinstance(item, Mapping)
        )
    joined = "\n".join(texts).casefold()
    return bool(
        re.search(
            r"self[- ]?loops?.{0,48}(?:allowed|permitted|harmless|legal)",
            joined,
        )
        or re.search(r"自环.{0,24}(?:允许|可以|合法|无害)", joined)
    )


def _materialize_recipe_boundary_parameters(
    structure: Mapping[str, Any],
    *,
    capability: Mapping[str, Any],
    ranges: Mapping[str, tuple[int, int]],
    case_kind: str,
    catalog: RecipeCatalog,
) -> dict[str, Any]:
    """Inject compiler-owned dimension boundaries before catalog validation."""

    local_structure = dict(structure)
    template_id = local_structure.get("template_id")
    parameters = local_structure.get("parameters")
    if not isinstance(template_id, str) or template_id not in catalog.templates:
        return local_structure
    local_parameters = dict(parameters) if isinstance(parameters, Mapping) else {}
    if case_kind not in {"lower_bound", "upper_bound"}:
        local_structure["parameters"] = local_parameters
        return local_structure
    entry = catalog.templates[template_id]
    aliases = {
        "n": ("n", "n_min", "n_max"),
        "m": ("m", "m_min", "m_max"),
        "rows": ("rows", "rows_min", "rows_max"),
        "cols": ("cols", "cols_min", "cols_max"),
        "length": ("length", "length_min", "length_max", "n", "n_min", "n_max"),
    }
    side = 0 if case_kind == "lower_bound" else 1
    for role, target in capability.get("bindings", {}).items():
        bound = ranges.get(str(target))
        if bound is None or role not in aliases:
            continue
        value = bound[side]
        supported = [name for name in aliases[role] if name in entry.parameters]
        exact = next(
            (name for name in supported if not name.endswith(("_min", "_max"))),
            None,
        )
        if exact is not None:
            local_parameters[exact] = value
        for name in supported:
            if name.endswith(("_min", "_max")):
                local_parameters[name] = value
    local_structure["parameters"] = local_parameters
    return local_structure


def generate_generator_recipe(
    client: Any,
    *,
    problem_id: str,
    statement: str,
    contract: Mapping[str, Any],
    settings: Mapping[str, Any],
    generation_mode: str | None = None,
    diagnostic: str = "",
    previous_recipe: Mapping[str, Any] | None = None,
    previous_blueprint: Mapping[str, Any] | None = None,
    repair_limit: int = 0,
    provider_reserve_seconds: float = 0.0,
    budget: PreparationBudget | None = None,
    progress_callback: StressProgress | None = None,
    cancel_scope: Any | None = None,
    _prompt_content: Callable[[Mapping[str, Any]], str] = _generator_recipe_prompt_content,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask the provider only for typed recipe data; all C++ stays local."""

    catalog = RecipeCatalog.load()
    supported, unsupported_reason = supports_static_contract(
        contract, catalog=catalog
    )
    if not supported:
        raise UnsupportedRecipeError(unsupported_reason or "unsupported_contract")
    if type(repair_limit) is not int or repair_limit < 0:
        raise ValueError("repair_limit must be a non-negative integer")
    capabilities = static_contract_capabilities(contract, catalog=catalog)
    allowed_structure_kinds = {
        str(capability["structure_kind"])
        for capability in capabilities.values()
    }
    catalog_kinds_by_structure = {
        "array": {"array", "list"},
        "string": {"string"},
        "matrix": {"matrix"},
        "interval": {"interval"},
        "edge": {"graph", "tree"},
    }
    allowed_catalog_kinds = {
        catalog_kind
        for structure_kind in allowed_structure_kinds
        for catalog_kind in catalog_kinds_by_structure.get(structure_kind, set())
    }
    structure_templates = [
        {
            "template_id": template_id,
            "kind": entry.kind,
            "parameters": entry.parameters,
        }
        for template_id, entry in sorted(catalog.templates.items())
        if entry.kind in allowed_catalog_kinds
    ]
    label_templates = [
        {
            "template_id": template_id,
            "kind": entry.kind,
            "parameters": entry.parameters,
        }
        for template_id, entry in sorted(catalog.templates.items())
        if entry.kind in {"label", "labels", "edge_time"}
        and "edge" in allowed_structure_kinds
    ]
    label_ranges: dict[str, tuple[int, int]] = {}
    for constraint in contract.get("constraints", []):
        if not isinstance(constraint, Mapping) or constraint.get("kind") != "range":
            continue
        target = constraint.get("target")
        args = constraint.get("args")
        if not isinstance(target, str) or not isinstance(args, Mapping):
            continue
        minimum, maximum = args.get("minimum"), args.get("maximum")
        if type(minimum) is int and type(maximum) is int and minimum <= maximum:
            label_ranges[target] = (minimum, maximum)
    serializer_summary = [
        {
            "format_id": serializer_id,
            "structure_kind": capability["structure_kind"],
            "required_binding_roles": sorted(capability["bindings"]),
            "local_bindings": capability["bindings"],
            "binding_ranges": {
                role: list(label_ranges[target])
                for role, target in capability["bindings"].items()
                if target in label_ranges
            },
        }
        for serializer_id, capability in sorted(capabilities.items())
    ]
    mode = _generation_mode(settings, generation_mode)
    base_messages = _artifact_prefix(
        problem_id=problem_id,
        statement=statement,
        contract=_compact_generator_contract(contract),
        include_statement=True,
    )
    base_messages.append(
        {
            "role": "user",
            "content": _prompt_content(
                {
                    "type": "acm_stress_generator_recipe_v1",
                    "instructions": (
                        "只返回严格 generator_recipe/v1 JSON，不生成、引用或解释 C++。"
                        "顶层必须且只能含 schema_version=1、engine=local_templates_v1、cases。"
                        "cases 必须恰好覆盖 small/lower_bound、small/random、"
                        "large/upper_bound、large/random，并严格按这个顺序输出。每个 family 只能引用 allowlist template_id，"
                        "parameters 只能使用 catalog 声明的类型；labels 字段必须显式给出，"
                        "非 graph/tree structure 的 labels 必须为 []；使用加权 edge serializer 时"
                        "每个 graph/tree family 必须恰好一个 label，非加权 serializer 不得有 label；"
                        "每个 family 的 semantic_goals 必须恰好一个；多语义要拆成多个 family。"
                        "semantic_goals 元素只能是 semantic_goals_allowed 中的字符串，严禁放入"
                        "template/label/parameters 对象。"
                        "selection.policy 固定 balanced_round_robin_v1，small seed_stride=1，large=5。"
                        "small byte_budget 固定 hard_max=2097152、buckets=[[1,25],[26,50],[51,75],[76,100],[101,2097152]]；"
                        "large hard_max=33554432、buckets=[[1,33554432]]。serializer 只能从"
                        "serializer_candidates 选择。serialization 不要输出 bindings；绑定由本地"
                        "根据 contract 确定性注入。small/lower_bound 对绑定维度取题面 minimum，"
                        "large/upper_bound 取 maximum，其余参数也必须落在 contract range 内。"
                        "small/random 至少规划两种结构或语义 family；禁止自由表达式、源码、include、"
                        "printf 格式及 catalog 外字段。"
                    ),
                    "output_contract": {
                        "top_level_allowed_keys": [
                            "schema_version", "engine", "cases"
                        ],
                        "case_allowed_keys": [
                            "profile", "case_kind", "families", "selection",
                            "serialization", "byte_budget"
                        ],
                        "family_allowed_keys": [
                            "structure", "labels", "semantic_goals"
                        ],
                        "serialization_allowed_keys": ["format_id"],
                        "fixed_values": {
                            "schema_version": 1,
                            "engine": "local_templates_v1",
                            "selection.policy": "balanced_round_robin_v1",
                        },
                    },
                    "structure_templates": structure_templates,
                    "label_templates": label_templates,
                    "serializer_candidates": serializer_summary,
                    "semantic_goals_allowed": sorted(_SEMANTIC_GOALS),
                }
            ),
        }
    )
    prior_raw: Any = dict(previous_recipe or previous_blueprint or {}) or None
    repair_diagnostic: Any = diagnostic[:4000] if diagnostic else None
    repairs_used = 1 if prior_raw is not None else 0
    automatic_repairs_used = 0
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
            stage="generate_recipe",
            provider_reserve_seconds=provider_reserve_seconds,
        )
        messages = list(base_messages)
        if prior_raw is not None:
            messages.append(
                {
                    "role": "user",
                    "content": _prompt_content(
                        {
                            "type": "acm_stress_generator_recipe_repair_v1",
                            "instructions": (
                                "根据 structured_diagnostic 修正 recipe，并返回完整严格 JSON。"
                                "不得输出或修改 C++，不得增加 allowlist 外字段。"
                            ),
                            "structured_diagnostic": repair_diagnostic,
                            "previous_raw_json": prior_raw,
                        }
                    ),
                }
            )
        result = _generate_json(
            client,
            messages,
            settings,
            budget=budget,
            stage="generate_recipe",
            soft_stage="prepare_helpers",
            max_tokens=max_tokens,
            thinking=thinking,
            provider_reserve_seconds=provider_reserve_seconds,
            request_retries=0 if requested_thinking and not thinking else 1,
            json_retries=0 if requested_thinking and not thinking else 1,
            retry_callback=_retry_progress(
                progress_callback,
                "generate_generator",
                "生成 typed generator recipe",
                3,
            ),
            cancel_scope=cancel_scope,
        )
        attempts += 1
        _usage_add(usage, dict(getattr(result, "usage", {}) or {}))
        try:
            candidate_data: Any = result.data
            if isinstance(candidate_data, Mapping):
                candidate_data = dict(candidate_data)
                raw_cases = candidate_data.get("cases")
                if isinstance(raw_cases, Sequence) and not isinstance(
                    raw_cases, (str, bytes)
                ):
                    local_cases: list[Any] = []
                    for raw_case in _canonicalize_recipe_case_slots(raw_cases):
                        if not isinstance(raw_case, Mapping):
                            local_cases.append(raw_case)
                            continue
                        local_case = dict(raw_case)
                        serialization = local_case.get("serialization")
                        if isinstance(serialization, Mapping):
                            local_serialization = dict(serialization)
                            format_id = local_serialization.get("format_id")
                            capability = capabilities.get(str(format_id))
                            if capability is not None:
                                local_serialization["bindings"] = dict(
                                    capability["bindings"]
                                )
                                label_target = capability["bindings"].get("label")
                                label_range = label_ranges.get(str(label_target))
                                raw_families = local_case.get("families")
                                if (
                                    str(format_id) == "edge_list_n_m_u_v_w"
                                    and label_range is not None
                                    and isinstance(raw_families, Sequence)
                                    and not isinstance(raw_families, (str, bytes))
                                ):
                                    normalized_families: list[Any] = []
                                    for raw_family in raw_families:
                                        if not isinstance(raw_family, Mapping):
                                            normalized_families.append(raw_family)
                                            continue
                                        local_family = dict(raw_family)
                                        raw_goals = local_family.get("semantic_goals")
                                        if isinstance(raw_goals, Sequence) and not isinstance(raw_goals, (str, bytes)):
                                            local_family["semantic_goals"] = [
                                                _normalize_recipe_semantic_goal(
                                                    goal,
                                                    case_kind=str(
                                                        local_case.get("case_kind") or ""
                                                    ),
                                                )
                                                for goal in raw_goals
                                            ]
                                        if (
                                            str(format_id) == "edge_list_n_m_u_v_w"
                                            and str(local_case.get("case_kind")) == "lower_bound"
                                            and _contract_explicitly_allows_self_loops(
                                                statement, contract
                                            )
                                        ):
                                            n_target = capability["bindings"].get("n")
                                            m_target = capability["bindings"].get("m")
                                            n_range = label_ranges.get(str(n_target))
                                            m_range = label_ranges.get(str(m_target))
                                            if (
                                                n_range is not None
                                                and m_range is not None
                                                and n_range[0] == 1
                                                and m_range[0] > 0
                                            ):
                                                local_family["structure"] = {
                                                    "template_id": "graph.self_loops",
                                                    "parameters": {
                                                        "n": 1,
                                                        "m": m_range[0],
                                                    },
                                                }
                                                local_family["semantic_goals"] = [
                                                    "single_vertex"
                                                ]
                                        structure = local_family.get("structure")
                                        if isinstance(structure, Mapping):
                                            local_structure = _materialize_recipe_boundary_parameters(
                                                structure,
                                                capability=capability,
                                                ranges=label_ranges,
                                                case_kind=str(
                                                    local_case.get("case_kind") or ""
                                                ),
                                                catalog=catalog,
                                            )
                                            parameters = local_structure.get("parameters")
                                            if isinstance(parameters, dict):
                                                _repair_random_simple_graph_domain(
                                                    local_structure,
                                                    case_kind=str(
                                                        local_case.get("case_kind") or ""
                                                    ),
                                                )
                                            local_family["structure"] = local_structure
                                        labels = local_family.get("labels")
                                        if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes)) and not labels:
                                            goals = local_family.get("semantic_goals")
                                            goal_set = {
                                                str(goal)
                                                for goal in goals
                                            } if isinstance(goals, Sequence) and not isinstance(goals, (str, bytes)) else set()
                                            if goal_set.intersection({"equal_labels", "early_threshold_connects"}):
                                                label_id = "label.equal"
                                            elif goal_set.intersection({"distinct_labels", "last_threshold_connects"}):
                                                label_id = "label.distinct"
                                            else:
                                                label_id = "label.uniform"
                                            local_family["labels"] = [
                                                {
                                                    "template_id": label_id,
                                                    "parameters": {
                                                        "label_min": label_range[0],
                                                        "label_max": label_range[1],
                                                    },
                                                }
                                            ]
                                        normalized_families.append(local_family)
                                    local_case["families"] = normalized_families
                            local_case["serialization"] = local_serialization
                        local_cases.append(local_case)
                    candidate_data["cases"] = local_cases
            recipe = validate_local_generator_recipe(
                candidate_data, contract=contract, catalog=catalog
            )
        except RecipeError as exc:
            if automatic_repairs_used >= repair_limit:
                usage["recipe_repairs_used"] = repairs_used
                usage["blueprint_repairs_used"] = repairs_used
                raise StressPreparationError(
                    "stress_recipe_invalid",
                    str(exc),
                    details={
                        "path": exc.path,
                        "attempts": attempts,
                        "repairs_used": repairs_used,
                        **dict(exc.details),
                    },
                    usage=usage,
                ) from exc
            automatic_repairs_used += 1
            repairs_used += 1
            prior_raw = result.data
            repair_diagnostic = {
                "code": exc.code,
                "path": exc.path,
                "message": str(exc)[:4000],
                "attempt": attempts,
            }
            continue
        usage["recipe_repairs_used"] = repairs_used
        usage["blueprint_repairs_used"] = repairs_used
        if requested_thinking and not thinking:
            usage["fast_fallback_used"] = True
        return recipe, usage


def compose_generator_recipe_artifact(
    recipe: Mapping[str, Any], *, contract: Mapping[str, Any]
) -> GeneratedArtifact:
    """Create the auditable local generator artifact without a provider call."""

    engine = recipe.get("engine") if isinstance(recipe, Mapping) else None
    composed = (
        compose_generator_recipe_v2(recipe, contract=contract)
        if engine == GENERATOR_RECIPE_V2_ENGINE
        else compose_generator_recipe(recipe, contract=contract)
    )
    local_v2 = engine == GENERATOR_RECIPE_V2_ENGINE
    return GeneratedArtifact(
        kind="generator",
        code=composed.source,
        origin="ai_recipe_composed",
        notes=(
            "contract shape 由本地确定性编译；C++ 由本地审计模板组合。"
            if local_v2
            else "AI 仅选择 typed recipe；C++ 由本地审计模板确定性组合。"
        ),
        source_sha256=composed.recipe_sha256,
        license="MIT",
        static_audit={"accepted": True, **dict(composed.metadata)},
    )
