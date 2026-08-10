"""Prompt-facing JSON schemas, token budgets and contract vocabularies.

Pure data: every name here is a literal or a schema builder, so this module
sits at the bottom of the stress_ai layering and imports nothing from its
siblings."""

from __future__ import annotations

from typing import Any
from .config import STRESS_GENERATION_MODES

STATIC_COMPILE_TIMEOUT_SECONDS = 3.0


LUOGU_AUDIT_TOTAL_SECONDS = 28.0


LUOGU_AUDIT_REQUEST_SECONDS = 24.0


LUOGU_AUDIT_MAX_SOURCE_CHARS = 32_000


LUOGU_AUDIT_MAX_STATEMENT_CHARS = 6_000


LUOGU_AUDIT_MAX_TOKENS = 512


LUOGU_AUDIT_MAX_CANDIDATES = 2


ARTIFACT_AUDIT_TOTAL_SECONDS = 50.0


ARTIFACT_AUDIT_MAX_TOKENS = 1024


ARTIFACT_AUDIT_MAX_STATEMENT_CHARS = 6_000


GENERATION_MODES = frozenset(STRESS_GENERATION_MODES)


CONTRACT_SCHEMA_VERSION = 3


GENERATOR_BLUEPRINT_SCHEMA_VERSION = 1


CONTRACT_MAX_TOKENS = 2_048


CONTRACT_REPAIR_MAX_TOKENS = 4_096


VALIDATOR_PROBE_CERTIFICATION_MAX_TOKENS = 1_536


GENERATOR_RECIPE_MAX_TOKENS = 6_144


GENERATOR_RECIPE_REPAIR_MAX_TOKENS = 8_192


GENERATOR_MAX_TOKENS = 8_192


GENERATOR_REPAIR_MAX_TOKENS = 12_288


BRUTE_MAX_TOKENS = 4_096


BRUTE_REPAIR_MAX_TOKENS = 6_144


VALIDATOR_MAX_TOKENS = 6_144


VALIDATOR_REPAIR_MAX_TOKENS = 8_192


REFERENCE_MAX_TOKENS = 8_192


REFERENCE_REPAIR_MAX_TOKENS = 12_288


_REQUIRED_GENERATOR_CASES = (
    ("small", "lower_bound"),
    ("small", "random"),
    ("large", "upper_bound"),
    ("large", "random"),
)


_OPTIONAL_GENERATOR_CASES: tuple[tuple[str, str], ...] = ()


_GENERATOR_CASE_ORDER = _OPTIONAL_GENERATOR_CASES + _REQUIRED_GENERATOR_CASES


_SUPPORTED_GENERATOR_CASES = frozenset(_GENERATOR_CASE_ORDER)


def _generator_case_schema(profile: str, case_kind: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "profile",
            "case_kind",
            "dimensions",
            "operation_families",
            "coverage_tags",
            "uses_seed",
            "construction",
            "total_complexity",
        ],
        "properties": {
            "profile": {"const": profile},
            "case_kind": {"const": case_kind},
            "dimensions": {
                "oneOf": [
                    {"type": "object", "minProperties": 1},
                    {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                ]
            },
            "operation_families": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
            "coverage_tags": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
            "uses_seed": {"const": case_kind == "random"},
            "construction": {"type": "string", "minLength": 1},
            "total_complexity": {"type": "string", "minLength": 1},
        },
    }


_GENERATOR_BLUEPRINT_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "required_cases",
        "dimensions",
        "operation_families",
        "required_coverage_tags",
        "large_required_coverage_tags",
        "cases",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "required_cases": {
            "oneOf": [
                {
                    "type": "array",
                    "minItems": len(case_pairs),
                    "maxItems": len(case_pairs),
                    "prefixItems": [
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["profile", "case_kind"],
                            "properties": {
                                "profile": {"const": profile},
                                "case_kind": {"const": case_kind},
                            },
                        }
                        for profile, case_kind in case_pairs
                    ],
                }
                for case_pairs in (_REQUIRED_GENERATOR_CASES, _GENERATOR_CASE_ORDER)
            ],
        },
        "dimensions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string", "minLength": 1}},
            },
        },
        "operation_families": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "required_coverage_tags": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "large_required_coverage_tags": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "cases": {
            "type": "array",
            "minItems": 3,
            "maxItems": 4,
            "items": {
                "oneOf": [
                    _generator_case_schema(profile, case_kind)
                    for profile, case_kind in _GENERATOR_CASE_ORDER
                ]
            },
            "contains": {
                "type": "object",
                "required": ["profile", "case_kind"],
                "properties": {
                    "profile": {"const": "small"},
                    "case_kind": {"const": "random"},
                },
            },
        },
    },
}


_GENERATOR_BLUEPRINT_TEMPLATE = {
    "schema_version": 1,
    "required_cases": [
        {"profile": profile, "case_kind": case_kind}
        for profile, case_kind in _REQUIRED_GENERATOR_CASES
    ],
    "dimensions": [
        {"name": "n", "minimum": "legal minimum", "maximum": "legal maximum"}
    ],
    "operation_families": ["plain_input_or_operation_name"],
    "required_coverage_tags": [
        "seed_variation",
        "legal_upper_bound",
    ],
    "large_required_coverage_tags": ["legal_upper_bound", "large_random"],
    "cases": [
        {
            "profile": profile,
            "case_kind": case_kind,
            "dimensions": {"n": "minimum" if case_kind == "lower_bound" else "bounded"},
            "operation_families": ["plain_input_or_operation_name"],
            "coverage_tags": [
                (
                    "legal_lower_bound"
                    if case_kind == "lower_bound"
                    else "legal_upper_bound"
                    if case_kind == "upper_bound"
                    else "seed_variation"
                    if profile == "small"
                    else "large_random"
                )
            ],
            "uses_seed": case_kind == "random",
            "construction": "describe the exact bounded construction",
            "total_complexity": (
                "O(output_size)" if case_kind != "random" else "O(output_size log n)"
            ),
        }
        for profile, case_kind in _REQUIRED_GENERATOR_CASES
    ],
}


_BASE_GENERATOR_REQUIREMENTS = (
    "实现 profile-v2 capability 协议，并支持必需的 small/lower_bound、small/random、"
    "large/upper_bound、large/random。",
    "small/lower_bound 必须精确达到所有相容规模参数的合法下界。",
    "large/upper_bound 必须精确达到契约允许的全局上界。",
    "random 必须真实使用 seed；同一 seed 必须逐字节确定，在连续 seed 的有界窗口中"
    "必须产生至少两种不同且合法的输入（允许个别相邻 seed 碰撞）。",
    "small/random 必须覆盖题面中的操作族、边界位置和特殊合法参数；"
    "允许在 small 的严格规模上限内使用朴素状态模拟。",
    "存在动态合法性前置条件时，必须严格按最终输出顺序逐条选择、校验并更新状态；"
    "禁止先按一种顺序模拟后再 shuffle/reorder 操作序列。",
    "所有 large 分支的总构造复杂度必须为 O(输出规模 log n) 或更低；"
    "禁止每条操作执行线性 erase/insert、全序列扫描或位置数组重建。",
    "large 优先使用恒等、无移动或其他由不变量保证合法的参数，"
    "不得为了覆盖 small 专属边界而维护昂贵的完整动态序列。",
)


_CONTRACT_SECTION_KINDS = frozenset(
    {
        "scalar",
        "list",
        "string",
        "matrix",
        "interval",
        "intervals",
        "edge_list",
        "operation_stream",
        "raw",
    }
)


_CONTRACT_FIELD_TYPES = frozenset({"int", "float", "string", "token", "char"})


_CONTRACT_FIELD_TYPE_ALIASES = {
    "integer": "int",
    "signed_integer": "int",
    "int32": "int",
    "int64": "int",
    "long": "int",
    "long_long": "int",
    "double": "float",
    "real": "float",
    "decimal": "float",
    "str": "string",
    "text": "string",
    "word": "token",
    "identifier": "token",
    "enum": "token",
    "keyword": "token",
    "command": "token",
    "operation": "token",
    "op": "token",
    "character": "char",
}


_CONTRACT_CONSTRAINT_KINDS = frozenset(
    {
        "range",
        "count_equals",
        "length_equals",
        "sum_limit",
        "unique",
        "permutation",
        "dependent_bound",
        "graph_predicate",
        "state_precondition",
        "custom_text",
    }
)


_CONTRACT_COVERAGE_PREDICATES = frozenset(
    {
        "constraint_boundary",
        "operation_variant",
        "value_class",
        "state_transition",
        "graph_shape",
        "custom_text",
    }
)
