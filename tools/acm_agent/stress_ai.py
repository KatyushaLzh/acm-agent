"""DeepSeek orchestration for explicit AI-assisted stress preparation.

This module keeps the public entry points -- ``extract_contract``,
``generate_artifact``, ``search_reference`` and ``prepare_stress`` -- and
re-exports the layered internals so callers still import everything from
``stress_ai``. The layering is strictly one-way::

    stress_ai_schema -> stress_ai_core -> stress_ai_contract
        -> stress_ai_blueprint -> {stress_ai_recipe, stress_ai_audit}
            -> stress_ai"""

from __future__ import annotations

from dataclasses import replace
from concurrent.futures import as_completed, ThreadPoolExecutor
import hashlib
import json
import re
import time
from typing import Any, Callable, Mapping, Sequence
from .deepseek import DeepSeekError
from .stress import SourceSafetyError, validate_cpp_source
from .stress_budget import PreparationBudget, PreparationBudgetExhausted
from .stress_recipe import (
    GENERATOR_RECIPE_COMPOSER_VERSION,
    GENERATOR_RECIPE_ENGINE,
    GENERATOR_RECIPE_SCHEMA_VERSION,
    supports_static_contract,
    UnsupportedRecipeError,
)
from .stress_recipe_v2 import (
    GENERATOR_RECIPE_V2_ENGINE,
    GENERATOR_RECIPE_V2_SCHEMA_VERSION,
    compile_static_contract_v2,
)
from .stress_sources import (
    AllowlistedCrawler,
    source_order_for_platform,
    SourceCandidate,
    SourceSearchError,
)

# Re-exported so importers of this module keep a single entry point.
from .stress_ai_schema import (
    _OPTIONAL_GENERATOR_CASES,
    _REQUIRED_GENERATOR_CASES,
    ARTIFACT_AUDIT_TOTAL_SECONDS,
    BRUTE_MAX_TOKENS,
    CONTRACT_SCHEMA_VERSION,
    GENERATION_MODES,
    GENERATOR_BLUEPRINT_SCHEMA_VERSION,
    GENERATOR_MAX_TOKENS,
    LUOGU_AUDIT_MAX_CANDIDATES,
    LUOGU_AUDIT_REQUEST_SECONDS,
    LUOGU_AUDIT_TOTAL_SECONDS,
    REFERENCE_MAX_TOKENS,
    STATIC_COMPILE_TIMEOUT_SECONDS,
    VALIDATOR_MAX_TOKENS,
)
from .stress_ai_core import (
    _artifact_prefix,
    _cancel_scope,
    _canonical_problem_prefix,
    _COMMON_STRESS_SYSTEM,
    _compact_unfinished_source_for_repair,
    _compile_reference_source,
    _effective_thinking,
    _generate_json,
    _generation_mode,
    _generation_policy,
    _progress,
    _require_code,
    _retry_progress,
    _usage_add,
    ArtifactAuditResult,
    CodeCompletionResult,
    complete_cpp_code,
    GeneratedArtifact,
    StressPreparation,
    StressPreparationError,
    StressProgress,
)
from .stress_ai_contract import (
    _compact_audit_contract,
    _compact_generator_contract,
    _compact_repair_contract,
    normalize_stress_contract,
)
from .stress_ai_blueprint import (
    generate_generator_blueprint,
    validate_generator_blueprint,
)
from .stress_ai_recipe import (
    _canonicalize_recipe_case_slots,
    _generator_recipe_prompt_content,
    _materialize_recipe_boundary_parameters,
    _normalize_recipe_case_identity,
    _normalize_recipe_semantic_goal,
    compose_generator_recipe_artifact,
    generate_generator_recipe as _generate_generator_recipe,
    validate_generator_recipe,
)
from .stress_ai_audit import (
    _certify_contract_validator_probes,
    audit_generated_artifact,
    audit_luogu_reference,
)


def generate_generator_recipe(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compatibility facade for the recipe provider workflow.

    Keep the historical ``stress_ai._generator_recipe_prompt_content`` seam
    injectable.  Tests and downstream diagnostics patch that name to exercise
    serialization failures; forwarding it explicitly avoids a hidden module
    global after the implementation moved to :mod:`stress_ai_recipe`.
    """

    kwargs["_prompt_content"] = _generator_recipe_prompt_content
    return _generate_generator_recipe(*args, **kwargs)


def _rebind_minimal_contract_evidence(
    value: Any, *, statement: str
) -> dict[str, Any] | None:
    """Replace only broken citations with exact, bounded statement slices.

    Minimal verification does not consume model-authored probes.  After all
    bounded evidence repairs fail, preserve the provider's typed syntax and
    constraints while binding their citations to the actual immutable source.
    """

    if not isinstance(value, Mapping) or not statement.strip():
        return None
    chunks: list[dict[str, Any]] = []
    for start in range(0, len(statement), 1900):
        quote = statement[start : start + 1900]
        if not quote.strip():
            continue
        chunks.append(
            {
                "id": f"local_statement_{len(chunks) + 1:02d}",
                "quote": quote,
                "start": start,
                "end": start + len(quote),
            }
        )
    if not chunks or len(chunks) > 16:
        return None
    rebound = json.loads(json.dumps(value, ensure_ascii=False))
    refs = [str(item["id"]) for item in chunks]
    rebound["evidence"] = chunks
    syntax = rebound.get("syntax")
    if isinstance(syntax, dict):
        sections = syntax.get("sections")
        if isinstance(sections, list):
            for section in sections:
                if not isinstance(section, dict):
                    continue
                section["evidence_ids"] = refs
                variants = section.get("variants")
                if isinstance(variants, list):
                    for variant in variants:
                        if isinstance(variant, dict):
                            variant["evidence_ids"] = refs
    for key in ("constraints", "coverage_obligations"):
        items = rebound.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    item["evidence_ids"] = refs
    rebound["validator_probes"] = []
    return rebound


def _contract_wire_shape(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return compact typed-contract telemetry without statement/evidence text."""

    syntax = contract.get("syntax")
    raw_sections = syntax.get("sections", []) if isinstance(syntax, Mapping) else []
    sections: list[dict[str, Any]] = []
    if isinstance(raw_sections, list):
        for raw in raw_sections:
            if not isinstance(raw, Mapping):
                continue
            fields = raw.get("fields", [])
            sections.append(
                {
                    "id": str(raw.get("id") or ""),
                    "kind": str(raw.get("kind") or ""),
                    "count_from": raw.get("count_from"),
                    "alphabet": list(raw.get("alphabet", []))
                    if isinstance(raw.get("alphabet"), list)
                    else [],
                    "fields": [
                        {
                            key: field[key]
                            for key in (
                                "name",
                                "type",
                                "minimum",
                                "maximum",
                                "minimum_from",
                                "maximum_from",
                                "lower",
                                "upper",
                                "lower_from",
                                "upper_from",
                            )
                            if key in field
                        }
                        for field in fields
                        if isinstance(field, Mapping)
                    ]
                    if isinstance(fields, list)
                    else [],
                    "variants": [
                        {
                            "tag": str(variant.get("tag") or ""),
                            "fields": [
                                {
                                    "name": str(field.get("name") or ""),
                                    "type": str(field.get("type") or ""),
                                }
                                for field in variant.get("fields", [])
                                if isinstance(field, Mapping)
                            ],
                        }
                        for variant in raw.get("variants", [])
                        if isinstance(variant, Mapping)
                    ]
                    if isinstance(raw.get("variants"), list)
                    else [],
                }
            )
    constraints = [
        {
            "kind": str(item.get("kind") or ""),
            "target": str(item.get("target") or ""),
            "args": dict(item.get("args", {}))
            if isinstance(item.get("args"), Mapping)
            else {},
        }
        for item in contract.get("constraints", [])
        if isinstance(item, Mapping)
    ]
    return {"sections": sections, "constraints": constraints}

def extract_contract(
    client: Any,
    *,
    problem_id: str,
    statement: str,
    compare: str,
    settings: Mapping[str, Any],
    generation_mode: str | None = None,
    provider_reserve_seconds: float = 0.0,
    budget: PreparationBudget | None = None,
    progress_callback: StressProgress | None = None,
    cancel_scope: Any | None = None,
    require_complete_probes: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    mode = _generation_mode(settings, generation_mode)
    base_messages = _canonical_problem_prefix(
        problem_id=problem_id,
        statement=statement,
        compare=compare,
    )
    base_messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "type": "acm_stress_contract",
                    "instructions": (
                        "提取可执行对拍契约，只返回 schema_version=3 的 JSON 对象。保留兼容字段 "
                        "input_summary、small_profile、small_lower_boundary、large_profile、"
                        "large_upper_boundary、output_compare、generator_requirements，并新增 syntax、"
                        "constraints、evidence、coverage_obligations、validator_probes。syntax 描述 token/record 语法，"
                        "只使用 scalar/list/string/matrix/interval/intervals/edge_list/operation_stream/raw section；"
                        "operation_stream 必须列出 tagged variants 及各自 fields。constraints 每项"
                        "包含稳定 id、kind、target、args、非空 evidence_ids；kind 只能是 range、"
                        "count_equals、length_equals、sum_limit、unique、permutation、"
                        "dependent_bound、graph_predicate、state_precondition、custom_text。"
                        "evidence 每项给出当前题面中逐字存在的 quote 及 Python 字符 start/end；"
                        "不得引用提示代码或发明题面未写的限制。coverage_obligations 只输出无法"
                        "受限字符集的 string/raw section 必须用 alphabet 列出题面允许的全部单字符；"
                        "q 后重复 q 次的 record/interval section 必须设置 count_from 到该 q 字段。"
                        "operation_stream 的重复次数也必须用 count_from 绑定计数字段；有限整数参数"
                        "（例如位移集合）必须在字段 minimum/maximum 或独立 range constraint 中"
                        "机器绑定，不能只写进 description/state_precondition 自然语言。"
                        "区间记录必须分别机器绑定 l 的下界/上界、r 的下界来自 l、r 的上界来自"
                        "字符串长度，不能只写未绑定的自然语言。"
                        "从 range constraint、operation variant 或字段 min/max/跨零区间机械推导的"
                        "额外语义覆盖，通常返回 []，最多 2 项；本地会自动生成边界、操作出现及"
                        "特殊值覆盖。额外 predicate.kind 只能是 state_transition、graph_shape、"
                        "custom_text。兼容字段中 input_summary、small_profile、"
                        "small_lower_boundary、large_profile、large_upper_boundary、output_compare "
                        "必须是字符串，generator_requirements 必须是非空字符串数组，"
                        "不得返回嵌套对象。"
                        "small_profile 必须小到用户可手工核对：通常每个主要维度不超过 8、"
                        "总元素或操作数不超过 20、完整输入不超过约 200 tokens，并保证朴素暴力"
                        "可在 5 秒内运行；题目合法下界不允许时才采用最小合法规模。"
                        "small/lower_bound 必须精确实现 small_lower_boundary。"
                        "small/random 必须覆盖全部操作族、边界位置和特殊合法参数；允许仅在 small"
                        " 内使用朴素状态模拟。large_profile 的主要规模必须取合法上界的 80% 到 "
                        "100%，用于用户解与 reference 双向比较，不运行 brute。large_upper_boundary "
                        "必须说明达到全局最大总规模的构造；多个参数受总和约束时给出满足约束的"
                        "极限构造。所有 large 构造都必须能以 O(输出规模 log n) 或更低复杂度生成；"
                        "优先选择恒等/不移动参数或由不变量保证合法的操作子集，禁止每条操作线性"
                        "删除、插入、扫描或重建位置。generator_requirements 必须逐项列出这些"
                        "profile、合法性、seed 确定性和复杂度要求。"
                        "保持 contract 紧凑且完整 JSON 不超过约 1500 tokens：只保留影响输入"
                        "合法性或生成覆盖的 3..6 个关键 constraints、1..3 条可复用 evidence、"
                        "0..2 个不可推导 coverage_obligations 和 1..3 条简短 generator_requirements；"
                        "优先让多个 constraint 共享一段连续的约束 evidence，绝不为同一句题面"
                        "创建重复 evidence。字段 description 非必要不要输出。"
                        "validator_probes 由独立 contract 分支提供给可信 harness，绝不会展示给"
                        "validator 生成分支。对于每个 state_precondition、dependent_bound 或"
                        "graph_predicate constraint，至少给出一组很小的 valid_input/invalid_input；"
                        "若不存在这三类 constraint，validator_probes 必须返回 []，不得输出占位项。"
                        "两份完整输入 token 数必须相同且只改 1 到 2 个 token，valid_input 与 "
                        "invalid_input 必须逐 token 不同，严禁输出完全相同的一对；相同则视为未提供"
                        "该探针。invalid_input 只违反"
                        "该 constraint。存在前后/上下/双向边界时应分别给 probe，并至少有一组在"
                        "先执行一次合法状态变更后触发非法前置条件，以杀死删除状态机或错误更新"
                        "状态映射的 validator。每份输入不超过 512 tokens。"
                    ),
                    "shape": {
                        "schema_version": 3,
                        "input_summary": "string",
                        "small_profile": "string",
                        "small_lower_boundary": "string",
                        "large_profile": "string",
                        "large_upper_boundary": "string",
                        "output_compare": str(compare).casefold(),
                        "generator_requirements": ["string"],
                        "syntax": {
                            "mode": "single_case|multi_case|until_eof",
                            "eof": "required|allowed",
                            "sections": [
                                {
                                    "id": "stable_id",
                        "kind": "scalar|list|string|matrix|interval|intervals|edge_list|operation_stream|raw",
                                    "count_from": "optional field reference",
                                    "alphabet": ["optional", "single-character", "tokens"],
                                    "fields": [
                                        {
                                            "name": "values",
                                            "type": "int",
                                        }
                                    ],
                                    "variants": [],
                                    "evidence_ids": ["e1"],
                                }
                            ],
                        },
                        "constraints": [
                            {
                                "id": "c1",
                                "kind": "range",
                                "target": "section.field",
                                "args": {"minimum": 1, "maximum": "N"},
                                "evidence_ids": ["e1"],
                            }
                        ],
                        "evidence": [
                            {"id": "e1", "quote": "exact statement text", "start": 0, "end": 20}
                        ],
                        "coverage_obligations": [],
                        "validator_probes": [
                            {
                                "id": "vp1",
                                "constraint_id": "dynamic_constraint_id",
                                "valid_input": "complete small valid input",
                                "invalid_input": "same tokens except the violating argument",
                                "evidence_ids": ["e1"],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    )
    usage: dict[str, Any] = {}
    previous_data: Any = None
    diagnostic: dict[str, Any] | None = None
    contract_attempts: list[dict[str, Any]] = []
    # One normal pass plus three bounded schema repairs.  After one narrow
    # evidence-only correction, later evidence failures force a full contract
    # rewrite so repeated paraphrases cannot poison a paid cold attempt.
    for attempt in range(4):
        is_repair = attempt > 0
        requested_thinking, max_tokens = _generation_policy(
            mode, "contract", repair=is_repair
        )
        protocol_only_repair = bool(
            is_repair
            and isinstance(diagnostic, Mapping)
            and diagnostic.get("code") == "invalid_json_output"
        )
        # A length/protocol failure contains no semantic contract defect to
        # reason about.  Hybrid repairs are diagnostic-driven non-thinking
        # rewrites; only explicit full_thinking mode enables reasoning.
        if protocol_only_repair:
            requested_thinking = False
        thinking = _effective_thinking(
            mode,
            requested_thinking,
            budget,
            stage="extract_contract",
            provider_reserve_seconds=provider_reserve_seconds,
        )
        messages = list(base_messages)
        if is_repair:
            evidence_only_repair = bool(
                isinstance(diagnostic, Mapping)
                and str(diagnostic.get("path") or "").startswith("evidence[")
                and attempt == 1
            )
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "type": "acm_stress_contract_v3_repair",
                            "instructions": (
                                (
                                    "只修 previous_raw_json 的 evidence 以及为保持引用一致必须调整的"
                                    "evidence_ids，其他 syntax/constraints/profile 字段原样保留。每个"
                                    "quote 必须直接复制当前题面中一段连续的 8 到 480 字符原文；禁止"
                                    "翻译、改写、总结或拼接不连续句子。start/end 若不能精确计算可填"
                                    "-1，本地会按逐字 quote 定位。每个 quote 必须真实支持引用它的"
                                    "constraint。返回完整 schema_version=3 JSON。"
                                    if evidence_only_repair
                                    else "上一份 contract 未通过本地 schema/evidence 校验。根据 path/message "
                                    "完整重写 schema_version=3 JSON；所有 evidence quote/offset 必须逐字"
                                    "对应当前题面，不得删掉兼容 profile 字段。"
                                )
                            ),
                            "repair_scope": (
                                "evidence_only" if evidence_only_repair else "diagnostic_path"
                            ),
                            "structured_diagnostic": diagnostic,
                            "previous_raw_json": previous_data,
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
                stage="extract_contract",
                soft_stage="contract",
                max_tokens=max_tokens,
                thinking=thinking,
                provider_reserve_seconds=provider_reserve_seconds,
                request_retries=0 if requested_thinking and not thinking else 1,
                # A repeated identical JSON request is not a contract repair.
                # Surface protocol/length failures to the explicit second pass
                # so hybrid can use its observable 4096-token thinking budget.
                json_retries=1 if is_repair else 0,
                retry_callback=_retry_progress(
                    progress_callback,
                    "extract_contract",
                    "让 DeepSeek 提取对拍契约" if not is_repair else "修复对拍契约",
                    2,
                ),
                cancel_scope=cancel_scope,
            )
        except DeepSeekError as exc:
            _usage_add(usage, dict(getattr(exc, "usage", {}) or {}))
            if not is_repair and str(getattr(exc, "code", "")) == "invalid_json_output":
                previous_data = None
                diagnostic = {
                    "code": "invalid_json_output",
                    "path": "$",
                    "message": str(exc)[:4000],
                    "finish_reason": str(getattr(exc, "finish_reason", "") or ""),
                }
                continue
            usage["contract_repairs_used"] = attempt
            exc.usage = dict(usage)
            if diagnostic is not None:
                try:
                    exc.details = {"prior_contract_diagnostic": dict(diagnostic)}
                except Exception:
                    pass
            raise
        _usage_add(usage, dict(getattr(result, "usage", {}) or {}))
        raw_contract = json.dumps(
            result.data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        contract_attempts.append(
            {
                "attempt": attempt + 1,
                "sha256": hashlib.sha256(raw_contract.encode("utf-8")).hexdigest(),
                "raw_json_excerpt": raw_contract[:16_000],
                "truncated": len(raw_contract) > 16_000,
            }
        )
        try:
            contract = normalize_stress_contract(
                result.data,
                compare=compare,
                statement=statement,
                require_complete_probes=require_complete_probes,
            )
            if not require_complete_probes:
                contract["validator_probes"] = []
                probe_usage: dict[str, Any] = {}
            else:
                contract, probe_usage = _certify_contract_validator_probes(
                    client,
                    problem_id=problem_id,
                    statement=statement,
                    contract=contract,
                    settings=settings,
                    provider_reserve_seconds=provider_reserve_seconds,
                    budget=budget,
                    progress_callback=progress_callback,
                    cancel_scope=cancel_scope,
                )
            _usage_add(usage, probe_usage)
        except StressPreparationError as exc:
            if is_repair:
                evidence_failure = str(exc.details.get("path") or "").startswith(
                    "evidence["
                )
                if attempt < 3 and evidence_failure:
                    previous_data = result.data
                    diagnostic = {
                        "code": exc.code,
                        "path": str(exc.details.get("path") or ""),
                        "message": str(exc)[:4000],
                        "details": dict(exc.details),
                    }
                    continue
                if evidence_failure and not require_complete_probes:
                    rebound = _rebind_minimal_contract_evidence(
                        result.data, statement=statement
                    )
                    if rebound is not None:
                        try:
                            contract = normalize_stress_contract(
                                rebound,
                                compare=compare,
                                statement=statement,
                                require_complete_probes=False,
                            )
                        except StressPreparationError:
                            pass
                        else:
                            contract["validator_probes"] = []
                            usage["contract_repairs_used"] = attempt
                            usage["contract_evidence_rebound"] = True
                            if requested_thinking and not thinking:
                                usage["fast_fallback_used"] = True
                            return contract, usage
                usage["contract_repairs_used"] = attempt
                exc.details["contract_attempts"] = list(contract_attempts)
                exc.usage = dict(usage)
                raise
            previous_data = result.data
            diagnostic = {
                "code": exc.code,
                "path": str(exc.details.get("path") or ""),
                "message": str(exc)[:4000],
                "details": dict(exc.details),
            }
            continue
        usage["contract_repairs_used"] = attempt
        if requested_thinking and not thinking:
            usage["fast_fallback_used"] = True
        return contract, usage
    raise AssertionError("contract repair loop must return or raise")


def generate_artifact(
    client: Any,
    *,
    kind: str,
    problem_id: str,
    statement: str,
    contract: Mapping[str, Any],
    settings: Mapping[str, Any],
    generator_blueprint: Mapping[str, Any] | None = None,
    generation_mode: str | None = None,
    diagnostic: str = "",
    previous_code: str = "",
    repair_from_scratch: bool = False,
    provider_reserve_seconds: float = 0.0,
    budget: PreparationBudget | None = None,
    progress_callback: StressProgress | None = None,
    cancel_scope: Any | None = None,
) -> tuple[GeneratedArtifact, dict[str, Any]]:
    if kind not in {
        "generator", "brute", "validator", "reference",
        "reference_primary", "reference_secondary",
    }:
        raise ValueError("unknown stress artifact kind")
    mode = _generation_mode(settings, generation_mode)
    try:
        early_diagnostic = json.loads(diagnostic) if diagnostic else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        early_diagnostic = {}
    repair_context = bool(previous_code or repair_from_scratch)
    generator_targeted_patch = bool(
        previous_code
        and kind == "generator"
        and isinstance(early_diagnostic, Mapping)
        and early_diagnostic.get("stage")
        in {"pre_audit_machine_gate", "local_preflight", "static_audit"}
        and early_diagnostic.get("repair_attempt") == 1
        and bool(early_diagnostic.get("code"))
    )
    prefer_patch = bool(
        previous_code
        and callable(getattr(client, "chat", None))
        and (
            generator_targeted_patch
            or (
                kind != "generator"
                and not (
                    isinstance(early_diagnostic, Mapping)
                    and early_diagnostic.get("stage")
                    == "pre_audit_machine_gate"
                    and kind != "validator"
                )
            )
        )
        and not (
            isinstance(early_diagnostic, Mapping)
            and early_diagnostic.get("code") == "invalid_generated_code"
        )
    )
    requested_thinking, max_tokens = _generation_policy(
        mode, kind, repair=repair_context
    )
    if (
        kind == "generator"
        and previous_code
        and isinstance(early_diagnostic, Mapping)
        and early_diagnostic.get("stage")
        in {"pre_audit_machine_gate", "local_preflight", "static_audit"}
        and early_diagnostic.get("repair_attempt") == 1
    ):
        # The first machine-gate failure already supplies an exact seed/input
        # witness.  A deterministic exact patch is usually enough and avoids
        # making a 12k reasoning call the common path.  If the
        # independent validator rejects it again, attempt two keeps the normal
        # hybrid thinking policy and the larger repair ceiling.
        requested_thinking = False
        max_tokens = GENERATOR_MAX_TOKENS
    generator_thinking_preface_tokens: int | None = None
    if kind == "generator" and repair_from_scratch and requested_thinking:
        # A second generator repair has a concrete prior machine diagnosis but
        # deliberately omits the failed architecture.  Providers otherwise can
        # spend all 12,288 tokens in hidden reasoning and emit no C++.  Bound
        # that reasoning phase to 4,096; complete_cpp_code then has an 8,192
        # deterministic finalizer for an empty body, keeping the combined
        # repair ceiling at the contracted 12,288 tokens.
        generator_thinking_preface_tokens = 4096
    if (
        isinstance(early_diagnostic, Mapping)
        and early_diagnostic.get("code") == "invalid_generated_code"
    ):
        # Shape/protocol repair does not need semantic reasoning.
        requested_thinking = False
        # A transport-only rewrite should not inherit the larger semantic
        # repair ceiling.  The initial role ceiling is sufficient to emit a
        # complete source file and prevents malformed first responses from
        # dominating the token tail.
        max_tokens = {
            "generator": GENERATOR_MAX_TOKENS,
            "validator": VALIDATOR_MAX_TOKENS,
            "brute": BRUTE_MAX_TOKENS,
            "reference": REFERENCE_MAX_TOKENS,
            "reference_primary": REFERENCE_MAX_TOKENS,
            "reference_secondary": REFERENCE_MAX_TOKENS,
        }[kind]
    if (
        kind == "validator"
        and previous_code
        and isinstance(early_diagnostic, Mapping)
        and (
            (
                early_diagnostic.get("stage") == "static_audit"
                and isinstance(early_diagnostic.get("witness"), Mapping)
                and str(
                    early_diagnostic["witness"].get("code_expression") or ""
                ).strip()
            )
            or (
                early_diagnostic.get("stage") == "pre_audit_machine_gate"
                and early_diagnostic.get("repair_attempt") == 1
                and bool(early_diagnostic.get("code"))
            )
        )
    ):
        # Audit rejects have a reproducible source witness, and the independent
        # pre-audit machine gate supplies an exact failing case/diagnostic.
        # Both make the common validator repair a small exact patch; a full 8k
        # reasoning trace only delays the same edit and dominates the token
        # tail.  Keep the contracted 8,192 output ceiling but execute the
        # concrete patch fast-first.
        requested_thinking = False
    # Exact-patch transport reduces visible output, not hidden reasoning.  Keep
    # the role-specific repair ceilings from the reliability contract: a
    # generator/reference repair may legitimately need 12,288 tokens to reach
    # the tiny JSON patch after reasoning.  Capping every patch at 8,192 caused
    # providers to spend the whole allowance on reasoning and return no patch.
    thinking = _effective_thinking(
        mode,
        requested_thinking,
        budget,
        stage=f"generate_{kind}",
        provider_reserve_seconds=provider_reserve_seconds,
    )
    normalized_blueprint = (
        validate_generator_blueprint(generator_blueprint, contract=contract)
        if kind == "generator" and generator_blueprint is not None
        else None
    )
    rules = {
        "generator": (
            "生成题目相关的确定性 C++17 数据 adapter。可信本地 harness 已独占 main、argv "
            "校验、--capabilities、seed/profile/case_kind 协议和环境同步。你的源码不得定义 main，"
            "必须且只需定义下面的全局函数："
            "void acm_generate_case(unsigned long long seed,const std::string& profile,"
            "const std::string& case_kind,std::ostream& out)。不得改名、改参数或放进 namespace。"
            "通过参数读取 seed/profile/case_kind，只向 out 输出；不要读取 argv/stdin/getenv，"
            "不要实现 --capabilities 或 --manifest，不要输出 manifest JSON；"
            "必须实现 blueprint 声明的 small/random、large/upper_bound、large/random；"
            "仅当 blueprint 声明时兼容 small/lower_bound。每次只向 out 输出一个完整测试输入。"
            "large/upper_bound 必须恰好达到契约允许的全局规模上界。"
            "每一个 random 分支都必须真实使用 seed 驱动 PRNG，并让至少一个实际输出的语义字段"
            "（例如排列、操作类型或合法参数）依赖 PRNG 结果；只构造但不消费 RNG 不算使用 seed。"
            "同一 seed 必须逐字节确定，连续 seed 的有界窗口必须出现至少两种均合法的输入（允许"
            "个别相邻 seed 碰撞）。禁止把固定样例伪装成 random，large/random 也不得与"
            "large/upper_bound 完全相同。"
            "对于合法状态空间明显大于 16 的 small/random，连续 16 个 seed 应目标产生至少 12 份"
            "不同的完整输入；不得只用 seed%2 或单个布尔分支。用 mt19937_64(seed) 混合完整 seed，"
            "并在保持动态合法性的前提下变化多个独立语义字段。只有数学上确实更小的状态空间"
            "才允许穷举循环重复。"
            "随机性不要求扰动所有字段。存在状态前置条件时，优先保留一个与 seed 无关、逐条"
            "证明合法的操作骨架，只让 seed 改变 Query/Ask 等只读参数、恒等/无条件合法参数"
            "或其他不会影响后续合法性的字段；禁止随机化初始结构后复用为固定初态设计的操作 ID。"
            "输入 SHA、dimensions、coverage_tags、records 和复杂度由本地 harness 与独立 validator"
            "根据真实 stdout 计算，源码不得自报这些字段。"
            "不得设置全局可变状态；同一进程内重复调用函数也必须只由本次参数决定。"
            "每条操作必须使用单一结构体或一条完整字符串保存，禁止用长度可能不同的并行"
            "数组分别保存操作名和参数后再按同一索引输出。必须保存不可变的初始输入状态，"
            "并使用独立的可变模拟状态保证后续操作参数合法，禁止把最终模拟状态误作初态输出。"
            "凡操作合法性依赖当前状态，必须严格按最终 out 顺序逐条选择操作、校验参数、"
            "追加记录并更新状态；禁止先按一种顺序验证后再 shuffle/reorder 最终操作列表。"
            "输出前必须校验声明的操作数、实际操作记录数、每条记录的参数数和相关容器长度一致。"
            "small 的严格规模上限内若需状态模拟，只允许使用拥有值语义的朴素容器并在每步重新"
            "查找位置；禁止自制 prev/next 链、缓存 vector 下标、保存 iterator/pointer 或同时"
            "维护多份位置映射。宁可对 n<=8 的 small 用 std::find 加 erase/insert，也不要优化。"
            "若候选状态操作非法，必须在输出前改用无条件合法/只读操作，不能先追加再 continue。"
            "所有 large 分支中，每个输出元素或操作的生成开销必须为摊销 O(log n) 或更低，"
            "禁止为每条操作线性 erase/insert、全量重建位置数组、全量扫描或复制整个当前序列；"
            "large 构造总开销不得二次方退化。large 应优先使用无需动态验证也天然合法"
            "的构造，例如存在恒等/不移动参数时可使用该参数，并用其他无条件合法操作补足规模。"
            "generator_blueprint 中每个 case 的 operation_families 是该 case 允许实际输出的"
            "操作子集；不得为了增加随机性或覆盖率擅自扩展。尤其 small/random 负责完整操作族"
            "覆盖，large/random 只实现其 construction 与 operation_families 给出的安全流式子集。"
            "construction 是候选实现策略，不是已经证明合法的逐条输入。先从不可变初态按最终"
            "输出顺序重放；若某个具体状态参数或顺序非法，必须在 append/output 前最小替换，"
            "不能因为 prose 写了该记录就保留非法输入。不得改成随机选择全部操作或引入 blueprint"
            " 未要求的数据结构。dimensions、声明记录数、operation_families 和 coverage_tags 才是"
            "权威验收条件：实际记录数必须与声明完全相等，允许替换但不得删减记录或操作族。"
            "不得重新解释字段含义；参数关系以 contract constraint 为准，不能把位移、下标或权值"
            "猜成另一个 ID。"
            "small 的覆盖义务不得强行复制到每个 large 分支；若无需模拟即可保证 large 合法，"
            "不得维护完整动态序列。应尽可能先输出不可变初始实例，"
            "再流式输出操作；只有输出格式确实要求延后输出时才保存操作记录。"
        ),
        "brute": "生成只需覆盖 small_profile 的独立朴素/穷举 C++17 标准答案。",
        "validator": (
            "生成独立 C++17 输入观察器。它只读取 stdin 中的一份测试输入，严格按 contract-v3 "
            "syntax/constraints 解析并观察，不得读取 generator、brute、reference 或用户主解源码，"
            "不得执行或推断任何解法。stdout 必须恰好输出一个紧凑 JSON 对象，不能输出日志或"
            "额外文本。对象的键必须恰好为：{\"valid\":bool,\"dimensions\":{},"
            "\"coverage_tags\":[],\"records\":0}，禁止增加 schema_version、coverage 或 errors。"
            "dimensions 的键来自 contract 中可重算的维度，值只允许非负 JSON 整数；"
            "coverage_tags 是当前输入实际满足的 coverage_obligations.id 的无重复字符串数组；"
            "records 是输入中的顶层记录/操作总数。语法错误、约束违反、溢出、缺 token 或"
            "多余 token 必须 valid=false、dimensions={}、coverage_tags=[]、records=0；如需"
            "诊断只向 stderr 输出不超过 200 字节的确定性单行信息：短错误码、第一处失败记录"
            "的零基索引以及判断该前置条件所需的有界状态/参数；不得回显整份输入。"
            "合法输入必须 valid=true。必须自行实现 JSON "
            "字符串转义，输出有效 UTF-8 JSON；"
            "不得把 stdin 原文或任意未设上限的字符串回显到 JSON。"
            "只维护判断输入合法性和 coverage 所必需的状态，不计算题目查询答案。若动态序列"
            "的合法性只依赖成员存在、首尾或相邻元素，优先使用 std::list、稳定 iterator 映射"
            "与 splice/swap 做 O(1) 更新；Ask/Query 等只读命令只检查参数边界，不要为了求"
            "排名或答案实现 treap、父指针或整套题解。仅当 contract 的合法性前置条件确实"
            "要求全局顺序统计时才引入更复杂结构。最终源码不得包含‘稍后重写/需要修复/"
            "当前实现不完整’之类自我修订说明，也不得把 coverage 称为 optional、approximate、"
            "not exact 或 acceptable approximation；成功路径和每条失败路径都必须实际输出四键 JSON。"
            "对 list 邻位移动：splice 保持被移动元素及其他元素 iterator 有效；向前一位可"
            "splice 到 prev(it) 之前，向后一位应先确认 next(it)!=end，再以 next(next(it))"
            "（允许结果等于 end）作为目标位置。只能禁止对 end 本身再 next，不能把 end 作为"
            "合法 splice 位置误判为错误。splice 后原来的被移动 iterator 仍指向该元素，"
            "映射必须保留它；禁止把映射改成 target、next 或 prev(target)，这些通常是邻居。"
        ),
        "reference": (
            "生成覆盖原题完整约束的独立 C++17 标准答案。必须从空白架构完整实现，不复制"
            "generator/brute/validator；所有循环必须对最小合法输入有严格进展与终止条件。"
            "若使用平衡树、哨兵或第 k 个元素，先固定内部索引与题面索引的偏移，禁止让 kth/"
            "查找在空子树中无限循环。复杂度必须通过最大规模。"
        ),
        "reference_primary": (
            "生成覆盖原题最大约束的独立、正确且高效的 C++17 标准答案。必须从空白架构完整实现，"
            "不得使用只适用于小数据的暴力、穷举或指数级算法，不复制或假设任何 sibling reference、"
            "generator、validator 或用户主解；复杂度必须通过题面最大规模。"
        ),
        "reference_secondary": (
            "生成覆盖原题最大约束的独立、正确且高效的 C++17 标准答案。必须从空白架构完整实现，"
            "不得使用只适用于小数据的暴力、穷举或指数级算法，不复制或假设任何 sibling reference、"
            "generator、validator 或用户主解；复杂度必须通过题面最大规模。"
        ),
    }[kind]
    if kind in {"reference", "reference_primary", "reference_secondary"}:
        rules += (
            "在进入一般循环或把答案初始化为无解之前，必须从题意单独推导并处理所有"
            "平凡初态与中性边界：空集合、单元素、零操作，以及目标谓词在读取任何"
            "记录前已经成立的情况。若答案表示最早时刻/最少操作数，初态满足时必须"
            "返回题意规定的零时刻或零操作答案，不能被通用的未找到哨兵覆盖。"
            "若题目包含动态顺序与第 k 个元素操作，任何 kth(k) 循环都必须先证明"
            "1<=k<=当前元素数，并在空节点时显式失败而非无限循环；尤其重新推导"
            "第二个元素上移、倒数第二个元素下移等相邻边界。相邻交换若可通过交换"
            "节点 payload 实现，必须同时更新 ID 到节点的映射，避免不必要的拆树重插。"
            "若 contract 恰好同时包含移到首尾、相邻位移、查询元素排名和查询第 k 个元素，"
            "优先使用离线预留两端坐标加 Fenwick 占用树：按最大操作数预留左右空坐标，"
            "移到首尾分配新坐标，相邻位移交换前驱/后继占用坐标上的 ID，排名用前缀和，"
            "第 k 个元素用 Fenwick kth；不要为这些操作使用无哨兵的 splay 区间拆接或反转。"
        )
    if previous_code:
        messages = [
            {"role": "system", "content": _COMMON_STRESS_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "type": "acm_stress_artifact_repair_context_v1",
                        "problem_id": problem_id,
                        "artifact_kind": kind,
                        "contract_summary": _compact_repair_contract(
                            contract, kind=kind
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]
    else:
        messages = _artifact_prefix(
            problem_id=problem_id,
            statement=statement,
            contract=(
                _compact_generator_contract(contract)
                if kind == "generator"
                else _compact_repair_contract(contract, kind="validator")
                if kind == "validator"
                else contract
            ),
            include_statement=kind not in {"generator", "validator"},
        )
    role_request = {
        "type": f"acm_stress_{kind}",
        "artifact_kind": kind,
        "rules": rules,
        "output_schema": {
            "code": "完整纯 C++17 源码，不含 Markdown 围栏",
            "notes": "简短中文说明",
        },
        "safety": (
            "不得访问文件、网络、进程或环境变量；generator 只能使用 adapter 的四个参数，"
            "不得读取 argv/stdin 或调用 getenv 读取 ACM_STRESS_*。不得调用 "
            "system、popen、fork、exec、WinAPI、动态加载"
            "或内联汇编。"
        ),
    }
    if kind == "generator" and normalized_blueprint is not None:
        role_request["generator_blueprint"] = normalized_blueprint
        role_request["hard_random_seed_acceptance"] = {
            "applies_to": ["small/random", "large/random"],
            "required_in_each_branch": [
                "initialize a PRNG from the seed parameter inside or before the branch",
                "consume that PRNG",
                "write at least one RNG-derived semantic input field to out",
            ],
            "recommended_safe_field": (
                "a read-only query argument or an unconditionally legal operation argument"
            ),
            "rejected": [
                "fixed random output",
                "unused PRNG",
                "seed only in comments or unreachable code",
                "whitespace-only variation",
            ],
        }
    messages.append(
        {
            "role": "user",
            "content": json.dumps(
                role_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    )
    if repair_context:
        try:
            parsed_diagnostic: Any = json.loads(diagnostic)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_diagnostic = str(diagnostic or "")[:4000]
        checklist = [
            "逐项消除 structured_diagnostic 中的每个 critical/warning 证据；不得忽略或争辩。",
            (
                "只返回精确 SEARCH/REPLACE 块并保留无关源码；若旧架构妨碍修复，用少量较大的精确替换块完成。"
                if prefer_patch
                else "返回可独立编译的完整替换源码，不返回补丁；旧架构妨碍修复时应直接重写。"
            ),
            "保持原角色、输入输出协议和安全限制，不读取用户主解或兄弟 helper。",
        ]
        if kind == "generator":
            checklist.extend(
                [
                    "保留唯一全局 acm_generate_case(seed,profile,case_kind,out) adapter，禁止添加 main、argv、stdin、getenv 或 capability/manifest 代码。",
                    "small 与 large 使用显式分离的构造路径；small 可朴素模拟，large 不得复用线性状态更新。",
                    "large 每条记录只做 O(log n) 或更低工作，禁止 vector/deque 中部 erase/insert、全序列扫描或全量位置更新。",
                    "特殊操作参数与边界覆盖放入 small/random；large 使用恒等/不移动或其他天然合法参数并流式输出。",
                    "重新核对 blueprint 的四个 profile/case_kind 组合，以及声明行数与实际输出行数；不要实现 harness 已接管的 main/capability/manifest。",
                    "每个 random 分支必须消费由 seed 参数初始化的 PRNG，并让至少一个实际输出字段依赖其结果；仅声明或构造未使用的 RNG 仍会被 seed-window 门禁拒绝。",
                    "状态相关操作必须按最终输出顺序逐条生成并同步模拟；已经验证过的操作列表不得再次 shuffle/reorder，否则所有前置条件验证作废。",
                    "blueprint construction 仅是候选策略；机器诊断证明其中的具体记录非法时，必须替换该参数或顺序，同时保持 dimensions、声明记录数、operation_families 与 coverage_tags。",
                    "记录列表是操作数的唯一事实源；先完整构造并校验 ops，最后用 ops.size() 输出声明计数，禁止 header 常量与 push_back 数量分开维护。",
                ]
            )
        elif kind == "validator":
            checklist.extend(
                [
                    "重新核对 stdin 完整解析、EOF 检查及 contract-v3 每条 constraint；不得读取任何 helper 或用户源码。",
                    "stdout 恰好为仅含 valid/dimensions/coverage_tags/records 四个键的 observation JSON；coverage_tags 只列实际满足的 obligation id。",
                    "coverage_tags 按集合维护并只输出一次；多个操作命中同一 obligation 时不得重复 push 同一 tag。",
                    "错误路径也必须输出同一四键 JSON，诊断写入 stderr，且不得回显未设上限的输入文本。",
                    "删除计算题目答案所需但输入合法性不需要的数据结构；局部相邻/首尾前置条件优先用稳定 iterator 容器与 O(1) splice/swap，不要实现排名 treap。",
                    "std::list::splice 后被移动 iterator 仍有效且仍指向原元素；ID 映射不得改成 target/next/prev(target) 邻居。",
                ]
            )
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "type": "acm_stress_artifact_repair",
                        "artifact_kind": kind,
                        "instructions": (
                            "这是一次有界定点修复机会。把诊断当作必须通过的验收条件，"
                            "先重新选择正确架构，再返回与原 output_schema 相同的 JSON。"
                        ),
                        "acceptance_checklist": checklist,
                        "structured_diagnostic": parsed_diagnostic,
                            **(
                                {"previous_code": previous_code[:256 * 1024]}
                                if previous_code
                                else {
                                    "rewrite_from_scratch": True,
                                    "previous_architecture_deliberately_omitted": True,
                                }
                            ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
    json_messages = list(messages)
    code_messages: list[dict[str, str]] = []
    for message in messages:
        transformed = dict(message)
        if message.get("role") == "user":
            try:
                payload = json.loads(message.get("content") or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, Mapping):
                payload = dict(payload)
                request_type = str(payload.get("type") or "")
                if request_type == f"acm_stress_{kind}":
                    if prefer_patch:
                        # The final repair request contains the compact role
                        # checklist.  Keeping the initial full-source request
                        # here contradicts patch transport and repeats rules.
                        continue
                    payload["type"] = f"acm_stress_{kind}_code_only"
                    payload["output_schema"] = (
                        "只输出完整纯 C++17 源码；不要 JSON、notes、解释或 Markdown 围栏"
                    )
                elif request_type == "acm_stress_artifact_repair":
                    payload["transport"] = "code_only"
                    if prefer_patch:
                        payload["transport"] = "exact_search_replace_json"
                        payload["instructions"] = (
                            "这是定点修复。保留所有无关源码，只输出单个紧凑 JSON 对象："
                            '{"patches":[{"search":"旧源码中唯一匹配的连续原文",'
                            '"replace":"替换后的原文，可为空"}]}。不得输出完整源码、解释、'
                            "Markdown 或额外键。search 必须逐字复制 previous_code；用最小修改"
                            "消除全部诊断。"
                        )
                        payload["patch_constraints"] = {
                            "maximum_blocks": 6,
                            "search_must_match_exactly_once": True,
                            "preserve_unrelated_code": True,
                        }
                    else:
                        payload["instructions"] = (
                            "这是定点修复机会。把诊断当作必须通过的验收条件，先重新选择正确架构，"
                            "然后只输出完整替换 C++17 源码；不要 JSON、解释或 Markdown 围栏。"
                        )
                transformed["content"] = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
        code_messages.append(transformed)
    try:
        completion = complete_cpp_code(
            client,
            code_messages,
            settings,
            json_messages=json_messages,
            budget=budget,
            stage=f"generate_{kind}",
            soft_stage="repair_helpers" if previous_code else "prepare_helpers",
            max_tokens=max_tokens,
            thinking=thinking,
            provider_reserve_seconds=provider_reserve_seconds,
            request_retries=0 if requested_thinking and not thinking else 1,
            json_retries=0 if requested_thinking and not thinking else 1,
            retry_callback=_retry_progress(
                progress_callback,
                f"generate_{kind}" if not kind.startswith("reference") else f"prepare_{kind}",
                {
                    "generator": "生成 generator",
                    "brute": "生成 brute",
                    "validator": "生成 validator",
                    "reference": "搜索或生成对拍代码",
                    "reference_primary": "搜索或生成 primary reference",
                    "reference_secondary": "搜索或生成 secondary reference",
                }[kind],
                {
                    "generator": 3, "brute": 4, "validator": 3, "reference": 5,
                    "reference_primary": 5, "reference_secondary": 6,
                }[kind],
            ),
            cancel_scope=cancel_scope,
            required_symbol="acm_generate_case" if kind == "generator" else "main",
            previous_code=previous_code,
            prefer_patch=prefer_patch,
            thinking_preface_max_tokens=generator_thinking_preface_tokens,
        )
    except StressPreparationError as exc:
        if repair_context or exc.code != "invalid_generated_code":
            raise
        first_usage = dict(getattr(exc, "usage", {}) or {})
        excerpt = str(exc.details.get("content_excerpt") or "").strip()
        unfinished_markers = list(exc.details.get("unfinished_markers") or [])[:16]
        repair_excerpt = (
            _compact_unfinished_source_for_repair(
                excerpt, unfinished_markers=unfinished_markers
            )
            if unfinished_markers
            else excerpt
        )
        first_usage[f"{kind}_transport_failure"] = {
            "code": str(exc.code),
            "message": str(exc)[:300],
            "content_chars": len(excerpt),
            "content_sha256": hashlib.sha256(
                excerpt.encode("utf-8", errors="replace")
            ).hexdigest(),
            "starts_markdown_fence": excerpt.startswith("```"),
            "required_symbol": str(exc.details.get("required_symbol") or ""),
            "unfinished_markers": unfinished_markers,
            "repair_context_chars": len(repair_excerpt),
        }
        machine_diagnostic = json.dumps(
            {
                "code": exc.code,
                "message": str(exc),
                "path": "$output",
                "required": "complete standalone C++17 source",
                "details": {
                    key: value
                    for key, value in exc.details.items()
                    if key != "content_excerpt"
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            repaired, repaired_usage = generate_artifact(
                client,
                kind=kind,
                problem_id=problem_id,
                statement=statement,
                contract=contract,
                settings=settings,
                generator_blueprint=generator_blueprint,
                # This is a transport/output-shape rewrite, not a semantic code
                # repair.  Use the repair token cap without spending the shared
                # allowance on high reasoning that has no source to inspect.
                generation_mode="fast" if mode == "hybrid" else mode,
                diagnostic=machine_diagnostic,
                previous_code=(
                    repair_excerpt or "// provider returned no complete source"
                ),
                provider_reserve_seconds=provider_reserve_seconds,
                budget=budget,
                progress_callback=progress_callback,
                cancel_scope=cancel_scope,
            )
        except Exception as repaired_exc:
            merged = dict(first_usage)
            _usage_add(merged, dict(getattr(repaired_exc, "usage", {}) or {}))
            merged[f"{kind}_transport_repairs_used"] = 1
            try:
                repaired_exc.usage = merged
            except Exception:
                pass
            raise
        merged = dict(first_usage)
        _usage_add(merged, repaired_usage)
        merged[f"{kind}_transport_repairs_used"] = 1
        return repaired, merged
    usage = dict(completion.usage)
    usage["completion_transport"] = completion.transport
    if requested_thinking and not thinking:
        usage["fast_fallback_used"] = True
    return (
        GeneratedArtifact(
            kind=kind,
            code=completion.code,
            origin="ai_generated",
            notes=completion.notes,
        ),
        usage,
    )


def search_reference(
    client: Any,
    crawler: AllowlistedCrawler,
    *,
    platform: str,
    problem_id: str,
    title: str,
    statement: str,
    contract: Mapping[str, Any],
    settings: Mapping[str, Any],
    budget: PreparationBudget | None = None,
    compile_checker: Callable[[str], tuple[bool, str]] = _compile_reference_source,
    progress_callback: StressProgress | None = None,
    cancel_scope: Any | None = None,
    candidate_pool: list[SourceCandidate] | None = None,
    max_candidates: int = 2,
    exclude_source_urls: Sequence[str] = (),
    exclude_code_hashes: Sequence[str] = (),
    audit_external_sources: bool = True,
) -> tuple[SourceCandidate | None, dict[str, Any]]:
    usage: dict[str, Any] = {}
    # PLATFORM_SOURCE_ORDER in stress_sources is the single source of truth for
    # this order and documents why it differs per platform; stress_runtime's
    # reported labels derive from the same table.
    tiers = source_order_for_platform(platform)
    seen_urls = {
        str(item.url).strip() for item in (candidate_pool or []) if str(item.url).strip()
    }
    seen_urls.update(str(url).strip() for url in exclude_source_urls if str(url).strip())
    seen_code_hashes = {
        hashlib.sha256(str(item.code).encode("utf-8")).hexdigest()
        for item in (candidate_pool or [])
        if item.code
    }
    seen_code_hashes.update(str(value).strip() for value in exclude_code_hashes if str(value).strip())
    for tier in tiers:
        if budget is not None:
            budget.require("prepare_reference")
        try:
            ordered = crawler.search(tier, problem_id=problem_id, title=title)
        except SourceSearchError as exc:
            if exc.code == "request_cancelled":
                raise
            if exc.code == "stress_prepare_budget_exhausted" and budget is not None:
                raise PreparationBudgetExhausted(budget, "prepare_reference") from exc
            continue
        total_complete = sum(bool(item.complete_cpp and item.code) for item in ordered)
        complete_index = 0
        audit_deadline = (
            min(
                time.monotonic() + LUOGU_AUDIT_TOTAL_SECONDS,
                budget.work_deadline if budget is not None else float("inf"),
            )
            if tier == "luogu_solutions" and audit_external_sources
            else None
        )
        for item in ordered:
            if not item.complete_cpp or not item.code:
                continue
            complete_index += 1
            if (
                tier == "luogu_solutions"
                and audit_external_sources
                and complete_index > LUOGU_AUDIT_MAX_CANDIDATES
            ):
                break
            try:
                validate_cpp_source(item.code)
            except SourceSafetyError:
                continue
            if tier == "luogu_solutions" and audit_external_sources:
                remaining = float(audit_deadline or 0.0) - time.monotonic()
                if remaining <= STATIC_COMPILE_TIMEOUT_SECONDS + 1.0:
                    break
                try:
                    audit, audit_usage = audit_luogu_reference(
                        client,
                        item,
                        problem_id=problem_id,
                        statement=statement,
                        contract=contract,
                        settings=settings,
                        compile_checker=compile_checker,
                        progress_callback=progress_callback,
                        candidate_index=complete_index,
                        candidate_total=max(1, total_complete),
                        request_timeout=min(
                            LUOGU_AUDIT_REQUEST_SECONDS,
                            remaining - STATIC_COMPILE_TIMEOUT_SECONDS,
                        ),
                        deadline=(
                            min(audit_deadline, budget.work_deadline)
                            if budget is not None and audit_deadline is not None
                            else audit_deadline
                        ),
                        cancel_scope=cancel_scope,
                    )
                except DeepSeekError as exc:
                    if exc.code in {"timeout", "invalid_json_output", "invalid_response"}:
                        break
                    raise
                _usage_add(usage, audit_usage)
                if not audit["accepted"]:
                    continue
                accepted_item = replace(item, static_audit=audit)
                if candidate_pool is None:
                    return accepted_item, usage
                code_hash = hashlib.sha256(accepted_item.code.encode("utf-8")).hexdigest()
                if accepted_item.url in seen_urls or code_hash in seen_code_hashes:
                    continue
                candidate_pool.append(accepted_item)
                seen_urls.add(accepted_item.url)
                seen_code_hashes.add(code_hash)
                if len(candidate_pool) >= max_candidates:
                    return candidate_pool[0], usage
                continue
            if candidate_pool is None:
                return item, usage
            code_hash = hashlib.sha256(item.code.encode("utf-8")).hexdigest()
            if item.url in seen_urls or code_hash in seen_code_hashes:
                continue
            candidate_pool.append(item)
            seen_urls.add(item.url)
            seen_code_hashes.add(code_hash)
            if len(candidate_pool) >= max_candidates:
                return candidate_pool[0], usage
    if candidate_pool:
        return candidate_pool[0], usage
    # Explanations and truncated snippets are search evidence, not executable
    # references. Returning one here prevents prepare_stress from invoking its
    # AI fallback and later fails at _require_code(None).
    return None, usage


def prepare_stress(
    client: Any,
    crawler: AllowlistedCrawler,
    *,
    platform: str,
    problem_id: str,
    title: str,
    statement: str,
    compare: str,
    settings: Mapping[str, Any],
    generation_mode: str | None = None,
    include_generator: bool = True,
    include_reference_primary: bool = True,
    include_reference_secondary: bool = True,
    include_validator: bool = False,
    allow_external_references: bool = True,
    require_complete_probes: bool = True,
    repair_diagnostic: str = "",
    budget: PreparationBudget | None = None,
    prepared_contract: Mapping[str, Any] | None = None,
    prepared_generator_blueprint: Mapping[str, Any] | None = None,
    prepared_artifacts: Mapping[str, GeneratedArtifact] | None = None,
    blueprint_repair_limit: int = 0,
    provider_reserve_seconds: float = 0.0,
    initial_usage: Mapping[str, Any] | None = None,
    progress_callback: StressProgress | None = None,
    artifact_callback: Callable[
        [str, GeneratedArtifact, Mapping[str, Any], Mapping[str, Any] | None], None
    ]
    | None = None,
    cancel_scope: Any | None = None,
) -> StressPreparation:
    mode = _generation_mode(settings, generation_mode)
    usage: dict[str, Any] = {}
    _usage_add(usage, initial_usage or {})
    if prepared_contract is None:
        _progress(progress_callback, "extract_contract", "让 DeepSeek 提取对拍契约", 2)
        try:
            contract, contract_usage = extract_contract(
                client,
                problem_id=problem_id,
                statement=statement,
                compare=compare,
                settings=settings,
                generation_mode=mode,
                provider_reserve_seconds=provider_reserve_seconds,
                budget=budget,
                progress_callback=progress_callback,
                cancel_scope=cancel_scope,
                require_complete_probes=require_complete_probes,
            )
        except Exception:
            _cancel_scope(cancel_scope)
            raise
        _usage_add(usage, contract_usage)
    else:
        contract = normalize_stress_contract(
            prepared_contract,
            compare=compare,
            statement=statement,
            require_complete_probes=require_complete_probes,
        )
        if not require_complete_probes:
            contract["validator_probes"] = []
    generator = validator = reference_primary = reference_secondary = None
    prepared: dict[str, GeneratedArtifact] = {}
    for role, artifact in dict(prepared_artifacts or {}).items():
        if role not in {
            "generator", "validator", "reference_primary", "reference_secondary"
        }:
            raise ValueError(f"unknown prepared artifact role: {role}")
        if not isinstance(artifact, GeneratedArtifact) or artifact.kind != role:
            raise ValueError("prepared artifact kind must match its role")
        prepared[role] = artifact
    deterministic_v2: dict[str, Any] | None = None
    try:
        deterministic_v2 = compile_static_contract_v2(contract)
    except UnsupportedRecipeError:
        pass
    generator_blueprint: dict[str, Any] | None
    if deterministic_v2 is not None and include_generator:
        # Contract-shape recipes are authoritative over historical v1/legacy
        # blueprint caches.  This guarantees that a newly supported shape
        # cannot be hidden by a stale provider-produced generator plan.
        generator_blueprint = deterministic_v2
        blueprint_source = "deterministic_contract"
    else:
        generator_blueprint = (
            validate_generator_recipe(
                prepared_generator_blueprint,
                contract=contract,
            )
            if prepared_generator_blueprint is not None
            else None
        )
        blueprint_source = (
            "reused" if generator_blueprint is not None else "not_requested"
        )
    recipe_supported, recipe_fallback_reason = supports_static_contract(contract)
    recipe_fallback_error: dict[str, Any] | None = None
    if generator_blueprint is not None:
        recipe_supported = generator_blueprint.get("engine") in {
            GENERATOR_RECIPE_ENGINE,
            GENERATOR_RECIPE_V2_ENGINE,
        }
        if recipe_supported:
            recipe_fallback_reason = None
    blueprint_repairs_used = 0
    recipe_repairs_used = 0
    if include_generator and generator_blueprint is None:
        # Complete the small recipe request before fan-out.  It shares the
        # canonical statement+contract prefix with every code role, so this
        # establishes the provider prefix cache and avoids four simultaneous
        # cache misses.  It also fails before spending helper calls when the
        # recipe itself is invalid.
        _progress(
            progress_callback,
            "generate_generator",
            (
                "生成 typed generator recipe 并预热共享前缀缓存"
                if recipe_supported
                else "规划 legacy generator blueprint 并预热共享前缀缓存"
            ),
            3,
        )
        try:
            if recipe_supported:
                try:
                    generator_blueprint, blueprint_usage = generate_generator_recipe(
                        client,
                        problem_id=problem_id,
                        statement=statement,
                        contract=contract,
                        settings=settings,
                        generation_mode=mode,
                        provider_reserve_seconds=provider_reserve_seconds,
                        budget=budget,
                        progress_callback=progress_callback,
                        repair_limit=blueprint_repair_limit,
                        cancel_scope=cancel_scope,
                    )
                except StressPreparationError as recipe_exc:
                    if recipe_exc.code != "stress_recipe_invalid":
                        raise
                    # A provider that cannot satisfy the narrow recipe schema
                    # must not make an otherwise supported problem unusable.
                    # Keep local/internal and provider failures fail-closed; only
                    # exhausted artifact validation falls back to the established
                    # legacy generator path.
                    _usage_add(usage, recipe_exc.usage)
                    recipe_repairs_used = int(
                        recipe_exc.usage.get("recipe_repairs_used", 0) or 0
                    )
                    recipe_supported = False
                    recipe_fallback_reason = "recipe_validation_failed"
                    recipe_fallback_error = {
                        "code": recipe_exc.code,
                        "message": str(recipe_exc)[:1000],
                        **{
                            key: recipe_exc.details[key]
                            for key in (
                                "path", "attempts", "reason", "profile", "case_kind"
                            )
                            if key in recipe_exc.details
                        },
                    }
                    _progress(
                        progress_callback,
                        "generate_generator",
                        "typed recipe 校验未收敛，回退 legacy generator blueprint",
                        3,
                    )
                    generator_blueprint, blueprint_usage = generate_generator_blueprint(
                        client,
                        problem_id=problem_id,
                        statement=statement,
                        contract=contract,
                        settings=settings,
                        generation_mode=mode,
                        provider_reserve_seconds=provider_reserve_seconds,
                        budget=budget,
                        progress_callback=progress_callback,
                        repair_limit=blueprint_repair_limit,
                        cancel_scope=cancel_scope,
                    )
            else:
                generator_blueprint, blueprint_usage = generate_generator_blueprint(
                    client,
                    problem_id=problem_id,
                    statement=statement,
                    contract=contract,
                    settings=settings,
                    generation_mode=mode,
                    provider_reserve_seconds=provider_reserve_seconds,
                    budget=budget,
                    progress_callback=progress_callback,
                    repair_limit=blueprint_repair_limit,
                    cancel_scope=cancel_scope,
                )
        except Exception as exc:
            _cancel_scope(cancel_scope)
            if isinstance(exc, PreparationBudgetExhausted):
                raise
            nested = getattr(exc, "details", None)
            nested = dict(nested) if isinstance(nested, Mapping) else {}
            declared_code = str(getattr(exc, "code", "") or "").strip()
            code = declared_code or "stress_internal_error"
            cause_type = str(nested.get("cause_type") or type(exc).__name__)
            category = str(nested.get("category") or "").strip().casefold()
            if category not in {
                "internal", "environment", "provider", "artifact",
                "execution", "oracle",
            }:
                if not declared_code or code in {
                    "stress_internal_error",
                    "stress_recipe_prompt_serialization_failed",
                }:
                    category = "internal"
                elif isinstance(exc, DeepSeekError):
                    category = "provider"
                else:
                    category = "artifact"
            try:
                attempts = int(
                    nested.get("repairs_used", nested.get("attempts", 0)) or 0
                )
            except (TypeError, ValueError):
                attempts = 0
            if recipe_supported:
                if code == "stress_recipe_prompt_serialization_failed":
                    substage = "recipe_prompt_serialization"
                elif code == "stress_recipe_invalid":
                    substage = "recipe_validation"
                else:
                    substage = str(nested.get("substage") or "recipe_generation")
            else:
                substage = str(nested.get("substage") or "blueprint")
            failure = {
                "stage": "prepare_generator",
                "substage": substage,
                "role": "generator",
                "elapsed": 0.0,
                "code": code,
                "category": category,
                "cause_type": cause_type,
                "message": str(exc)[:500],
                "usage": dict(getattr(exc, "usage", {}) or {}),
                "attempts": max(0, attempts),
            }
            path = str(nested.get("path") or "").strip()
            if path:
                failure["path"] = path[:200]
            if recipe_supported and code == "stress_recipe_invalid":
                message = "generator recipe 校验失败"
            elif recipe_supported and category == "internal":
                message = "generator recipe 本地准备失败"
            elif recipe_supported:
                message = "generator recipe 生成失败"
            else:
                message = "generator blueprint 校验失败"
            if path:
                message += f"（{path}）"
            message += f"：{failure['message']}"
            if attempts and code in {"stress_recipe_invalid", "stress_blueprint_invalid"}:
                message += f"；结构修复 {attempts}/{blueprint_repair_limit}"
            raise StressPreparationError(
                "stress_artifact_stage_failed",
                message,
                details={
                    "roles": {"generator": failure},
                    "primary_failure": failure,
                },
                usage=failure["usage"],
            ) from exc
        blueprint_source = "generated"
        if generator_blueprint.get("engine") == GENERATOR_RECIPE_ENGINE:
            recipe_repairs_used = int(
                blueprint_usage.get("recipe_repairs_used", 0) or 0
            )
        else:
            blueprint_repairs_used = int(
                blueprint_usage.get("blueprint_repairs_used", 0) or 0
            )
        _usage_add(usage, blueprint_usage)

    reference_candidates: list[SourceCandidate] = []
    reference_search_usage: dict[str, Any] = {}
    requested_reference_roles = [
        role
        for role, enabled_role in (
            ("reference_primary", include_reference_primary),
            ("reference_secondary", include_reference_secondary),
        )
        if enabled_role and role not in prepared
    ]
    if (
        requested_reference_roles
        and not repair_diagnostic
        and allow_external_references
    ):
        _, reference_search_usage = search_reference(
            client,
            crawler,
            platform=platform,
            problem_id=problem_id,
            title=title,
            statement=statement,
            contract=contract,
            settings=settings,
            budget=budget,
            progress_callback=progress_callback,
            cancel_scope=cancel_scope,
            candidate_pool=reference_candidates,
            max_candidates=len(requested_reference_roles),
            exclude_source_urls=tuple(
                artifact.source_url
                for role, artifact in prepared.items()
                if role.startswith("reference") and artifact.source_url
            ),
            exclude_code_hashes=tuple(
                hashlib.sha256(artifact.code.encode("utf-8")).hexdigest()
                for role, artifact in prepared.items()
                if role.startswith("reference")
            ),
            audit_external_sources=False,
        )
        _usage_add(usage, reference_search_usage)

    candidate_by_role = {
        role: reference_candidates[index]
        for index, role in enumerate(requested_reference_roles)
        if index < len(reference_candidates)
    }

    def prepare_role(
        kind: str,
    ) -> tuple[str, GeneratedArtifact, dict[str, Any], dict[str, Any] | None, str]:
        if budget is not None:
            budget.require(f"prepare_{kind}")
        if kind == "generator":
            local_usage: dict[str, Any] = {}
            blueprint = generator_blueprint
            source = blueprint_source
            if blueprint is None:
                raise StressPreparationError(
                    "stress_blueprint_missing",
                    "generator recipe 未准备完成",
                )
            if blueprint.get("engine") in {
                GENERATOR_RECIPE_ENGINE,
                GENERATOR_RECIPE_V2_ENGINE,
            }:
                artifact = compose_generator_recipe_artifact(
                    blueprint, contract=contract
                )
                return kind, artifact, local_usage, blueprint, source
            try:
                artifact, artifact_usage = generate_artifact(
                    client,
                    kind=kind,
                    problem_id=problem_id,
                    statement=statement,
                    contract=contract,
                    settings=settings,
                    generator_blueprint=blueprint,
                    generation_mode=mode,
                    diagnostic=repair_diagnostic,
                    provider_reserve_seconds=provider_reserve_seconds,
                    budget=budget,
                    progress_callback=progress_callback,
                    cancel_scope=cancel_scope,
                )
            except Exception as exc:
                combined = dict(local_usage)
                _usage_add(combined, dict(getattr(exc, "usage", {}) or {}))
                try:
                    exc.usage = combined
                except Exception:
                    pass
                raise
            _usage_add(local_usage, artifact_usage)
            return kind, artifact, local_usage, blueprint, source
        if kind == "validator":
            artifact, artifact_usage = generate_artifact(
                client,
                kind=kind,
                problem_id=problem_id,
                statement=statement,
                contract=contract,
                settings=settings,
                generation_mode=mode,
                diagnostic=repair_diagnostic,
                provider_reserve_seconds=provider_reserve_seconds,
                budget=budget,
                progress_callback=progress_callback,
                cancel_scope=cancel_scope,
            )
            return kind, artifact, artifact_usage, None, "not_applicable"
        candidate = candidate_by_role.get(kind)
        if candidate is not None:
            source_kind = {
                "codeforces_official": "codeforces_editorial",
                "luogu_solutions": "luogu_solution",
                "cnblogs": "cnblogs",
                "csdn": "csdn",
            }[candidate.tier]
            selected = GeneratedArtifact(
                kind=kind,
                code=_require_code(candidate.code),
                origin=source_kind,
                notes="从固定白名单题解来源提取的完整 C++；执行前仍需本地安全与交叉验证。",
                source_url=candidate.url,
                source_title=candidate.title,
                source_sha256=hashlib.sha256(candidate.code.encode("utf-8")).hexdigest(),
                license=candidate.license or "unknown",
                static_audit=candidate.static_audit,
                source_alternates=tuple(
                    alternate.to_dict(include_content=True)
                    for alternate in reference_candidates
                    if alternate.candidate_id != candidate.candidate_id
                ),
            )
            artifact_usage: dict[str, Any] = {}
        else:
            selected, artifact_usage = generate_artifact(
                client,
                kind=kind,
                problem_id=problem_id,
                statement=statement,
                contract=contract,
                settings=settings,
                # Source search failure is not a code-level diagnostic.  Keep
                # hybrid fast-first; only a concrete compile/runtime/oracle
                # witness is allowed to enable thinking on the repair call.
                generation_mode=mode,
                diagnostic=(
                    "独立生成一份与其他 reference 隔离的完整标准答案；"
                    f"不得使用暴力算法。上次验证失败：{repair_diagnostic[:4000]}"
                ),
                provider_reserve_seconds=provider_reserve_seconds,
                budget=budget,
                progress_callback=progress_callback,
                cancel_scope=cancel_scope,
            )
        return kind, selected, artifact_usage, None, "not_applicable"
    enabled = {
        "generator": include_generator,
        "validator": include_validator,
        "reference_primary": include_reference_primary,
        "reference_secondary": include_reference_secondary,
    }
    labels = {
        "generator": "生成 generator",
        "validator": "生成 validator",
        "reference_primary": "搜索或生成 primary reference",
        "reference_secondary": "搜索或生成 secondary reference",
    }
    steps = {
        "generator": 3, "validator": 4,
        "reference_primary": 5, "reference_secondary": 6,
    }
    roles = [
        role for role, selected in enabled.items()
        if selected and role not in prepared
    ]
    progress_roles = ["generator"]
    if include_validator:
        progress_roles.append("validator")
    progress_roles.extend(("reference_primary", "reference_secondary"))
    for role in progress_roles:
        _progress(
            progress_callback,
            f"generate_{role}" if not role.startswith("reference") else f"prepare_{role}",
            (
                f"命中角色 checkpoint：{role}"
                if role in prepared
                else labels[role]
                if enabled[role]
                else f"检查已有 {role}"
            ),
            steps[role],
        )
    failures: dict[str, dict[str, Any]] = {}
    primary_failure: dict[str, Any] | None = None
    if roles:
        with ThreadPoolExecutor(
            max_workers=len(roles), thread_name_prefix="stress-prepare"
        ) as executor:
            futures = {}
            for role in roles:
                submitted_at = time.monotonic()
                futures[executor.submit(prepare_role, role)] = (role, submitted_at)
            for future in as_completed(futures):
                role, started = futures[future]
                try:
                    (
                        finished_role,
                        artifact,
                        artifact_usage,
                        finished_blueprint,
                        finished_blueprint_source,
                    ) = future.result()
                    prepared[finished_role] = artifact
                    if finished_role == "generator":
                        generator_blueprint = finished_blueprint
                        blueprint_source = finished_blueprint_source
                    if artifact_callback is not None:
                        artifact_callback(
                            finished_role,
                            artifact,
                            artifact_usage,
                            finished_blueprint,
                        )
                    _usage_add(usage, artifact_usage)
                except Exception as exc:
                    nested = getattr(exc, "details", None)
                    nested = dict(nested) if isinstance(nested, Mapping) else {}
                    declared_code = str(getattr(exc, "code", "") or "").strip()
                    code = declared_code or "stress_internal_error"
                    cause_type = str(nested.get("cause_type") or type(exc).__name__)
                    category = str(nested.get("category") or "").strip().casefold()
                    if category not in {
                        "internal", "environment", "provider", "artifact",
                        "execution", "oracle",
                    }:
                        if not declared_code or code == "stress_internal_error":
                            category = "internal"
                        elif isinstance(exc, DeepSeekError):
                            category = "provider"
                        else:
                            category = "artifact"
                    is_sibling_cancel = (
                        code == "request_cancelled" and primary_failure is not None
                    )
                    _cancel_scope(cancel_scope)
                    for sibling in futures:
                        if sibling is not future:
                            sibling.cancel()
                    if isinstance(exc, PreparationBudgetExhausted):
                        raise
                    try:
                        attempts = int(
                            nested.get("repairs_used")
                            if code == "stress_blueprint_invalid"
                            else nested.get("attempts") or 1
                        )
                    except (TypeError, ValueError):
                        attempts = 1
                    failure = {
                        "stage": f"prepare_{role}",
                        "substage": (
                            "blueprint"
                            if code == "stress_blueprint_invalid"
                            else "preparation"
                        ),
                        "role": role,
                        "elapsed": round(time.monotonic() - started, 3),
                        "code": code,
                        "category": category,
                        "cause_type": cause_type,
                        "message": str(exc)[:500],
                        "usage": dict(getattr(exc, "usage", {}) or {}),
                        "attempts": max(0, attempts),
                    }
                    path = str(nested.get("path") or "").strip()
                    if path:
                        failure["path"] = path[:200]
                    if "repairs_used" in nested:
                        failure["repairs_used"] = max(0, attempts)
                    if primary_failure is None:
                        primary_failure = dict(failure)
                    if not is_sibling_cancel:
                        failures[role] = failure
                    _usage_add(usage, failure["usage"])
        if failures:
            _cancel_scope(cancel_scope)
            assert primary_failure is not None
            if budget is not None:
                budget.set_context(
                    generation_attempt=int(primary_failure.get("attempts") or 0),
                    attempts={
                        str(primary_failure.get("role") or "helper"): int(
                            primary_failure.get("attempts") or 0
                        )
                    },
                    last_diagnostic=str(primary_failure.get("message") or "")[:500],
                )
            if (
                primary_failure.get("role") == "generator"
                and primary_failure.get("code") == "stress_blueprint_invalid"
            ):
                path = str(primary_failure.get("path") or "").strip()
                attempts = int(primary_failure.get("attempts") or 0)
                message = "generator blueprint 校验失败"
                if path:
                    message += f"（{path}）"
                message += f"：{primary_failure['message']}"
                if attempts:
                    message += f"；结构修复 {attempts}/{blueprint_repair_limit}"
            else:
                message = (
                    f"{primary_failure.get('role') or 'helper'} 准备失败："
                    f"{primary_failure.get('message') or '未知错误'}"
                )
            raise StressPreparationError(
                "stress_artifact_stage_failed",
                message,
                details={
                    "roles": failures,
                    "primary_failure": primary_failure,
                },
                usage=usage,
            )
    reference_primary = prepared.get("reference_primary")
    reference_secondary = prepared.get("reference_secondary")
    if reference_primary is not None and reference_secondary is not None:
        primary_hash = hashlib.sha256(reference_primary.code.encode("utf-8")).hexdigest()
        secondary_hash = hashlib.sha256(reference_secondary.code.encode("utf-8")).hexdigest()
        duplicate_url = bool(
            reference_primary.source_url
            and reference_secondary.source_url
            and reference_primary.source_url.strip() == reference_secondary.source_url.strip()
        )
        if primary_hash == secondary_hash or duplicate_url:
            retry_role = next(
                (
                    role
                    for role in ("reference_secondary", "reference_primary")
                    if prepared[role].origin == "ai_generated"
                ),
                None,
            )
            if retry_role is not None:
                other_role = (
                    "reference_primary"
                    if retry_role == "reference_secondary"
                    else "reference_secondary"
                )
                other_code = prepared[other_role].code
                require_stdio = not bool(
                    re.search(r"\b(?:scanf|printf)\s*\(|<cstdio>", other_code)
                )
                diversity_rule = (
                    "必须使用 C stdio（scanf/printf）完成输入输出，且不得使用 iostream/cin/cout；"
                    if require_stdio
                    else "必须使用 iostream（cin/cout）完成输入输出，且不得使用 scanf/printf；"
                )
                replacement, replacement_usage = generate_artifact(
                    client,
                    kind=retry_role,
                    problem_id=problem_id,
                    statement=statement,
                    contract=contract,
                    settings=settings,
                    generation_mode=mode,
                    diagnostic=json.dumps(
                        {
                            "stage": "reference_independence",
                            "code": "duplicate_source_hash",
                            "message": (
                                "生成结果与另一独立 reference 的源码哈希重复。"
                                "从空白重新独立实现，不得引用、复述或猜测另一份源码。"
                            ),
                            "required_structural_diversity": diversity_rule,
                        },
                        ensure_ascii=False,
                    ),
                    repair_from_scratch=True,
                    provider_reserve_seconds=provider_reserve_seconds,
                    budget=budget,
                    progress_callback=progress_callback,
                    cancel_scope=cancel_scope,
                )
                _usage_add(usage, replacement_usage)
                prepared[retry_role] = replacement
                if artifact_callback is not None:
                    artifact_callback(retry_role, replacement, replacement_usage, None)
                reference_primary = prepared.get("reference_primary")
                reference_secondary = prepared.get("reference_secondary")
                primary_hash = hashlib.sha256(
                    reference_primary.code.encode("utf-8")
                ).hexdigest()
                secondary_hash = hashlib.sha256(
                    reference_secondary.code.encode("utf-8")
                ).hexdigest()
                duplicate_url = bool(
                    reference_primary.source_url
                    and reference_secondary.source_url
                    and reference_primary.source_url.strip()
                    == reference_secondary.source_url.strip()
                )
            if primary_hash == secondary_hash or duplicate_url:
                raise StressPreparationError(
                    "stress_reference_duplicate",
                    "两份 reference 的来源 URL 或源码哈希重复，独立重试后仍无法建立双 reference",
                    details={
                        "roles": ["reference_primary", "reference_secondary"],
                        "source_sha256": primary_hash,
                        "duplicate_url": duplicate_url,
                    },
                    usage=usage,
                )

    generator = prepared.get("generator")
    validator = prepared.get("validator")
    request_metadata: dict[str, Any] = {}
    for stage in (
        "contract",
        "blueprint",
        "generator",
        "validator",
        "reference_primary",
        "reference_secondary",
    ):
        stage_thinking, stage_max_tokens = _generation_policy(mode, stage)
        request_metadata[stage] = {
            "thinking": stage_thinking,
            "max_tokens": stage_max_tokens,
        }
    repair_thinking, repair_tokens = _generation_policy(
        mode, "generator", repair=True
    )
    request_metadata["generator_repair"] = {
        "thinking": repair_thinking,
        "max_tokens": repair_tokens,
    }
    repair_thinking, repair_tokens = _generation_policy(
        mode, "validator", repair=True
    )
    request_metadata["validator_repair"] = {
        "thinking": repair_thinking,
        "max_tokens": repair_tokens,
    }
    generation_metadata = {
        "mode": mode,
        "generation_mode": mode,
        "contract_source": "generated" if prepared_contract is None else "reused",
        "contract_wire_shape": _contract_wire_shape(contract),
        "blueprint_source": blueprint_source,
        "recipe_source": blueprint_source,
        "blueprint_schema_version": (
            GENERATOR_BLUEPRINT_SCHEMA_VERSION
            if generator_blueprint is not None
            and generator_blueprint.get("engine") not in {
                GENERATOR_RECIPE_ENGINE,
                GENERATOR_RECIPE_V2_ENGINE,
            }
            else None
        ),
        "recipe_schema_version": (
            (
                GENERATOR_RECIPE_V2_SCHEMA_VERSION
                if generator_blueprint.get("engine") == GENERATOR_RECIPE_V2_ENGINE
                else GENERATOR_RECIPE_SCHEMA_VERSION
            )
            if generator_blueprint is not None
            and generator_blueprint.get("engine") in {
                GENERATOR_RECIPE_ENGINE,
                GENERATOR_RECIPE_V2_ENGINE,
            }
            else None
        ),
        "generator_engine": (
            str(generator_blueprint.get("engine"))
            if generator_blueprint is not None
            and generator_blueprint.get("engine") in {
                GENERATOR_RECIPE_ENGINE,
                GENERATOR_RECIPE_V2_ENGINE,
            }
            else f"legacy_ai_cpp:{recipe_fallback_reason or 'unsupported_contract'}"
            if generator_blueprint is not None
            else "not_requested"
        ),
        "recipe_fallback_reason": (
            recipe_fallback_reason
            if generator_blueprint is not None
            and generator_blueprint.get("engine") not in {
                GENERATOR_RECIPE_ENGINE,
                GENERATOR_RECIPE_V2_ENGINE,
            }
            else None
        ),
        "state_machine": (
            str(generator_blueprint.get("machine", {}).get("kind") or "")
            if generator_blueprint is not None
            and generator_blueprint.get("engine") == GENERATOR_RECIPE_V2_ENGINE
            and isinstance(generator_blueprint.get("machine"), Mapping)
            else None
        ),
        "recipe_fallback_error": recipe_fallback_error,
        "fast_fallback_used": bool(usage.get("fast_fallback_used", False)),
        "blueprint_repairs_used": blueprint_repairs_used,
        "recipe_repairs_used": recipe_repairs_used,
        "requests": request_metadata,
    }
    return StressPreparation(
        contract,
        generator,
        reference_primary,
        reference_secondary,
        usage,
        generator_blueprint,
        generation_metadata,
        validator,
    )


__all__ = [
    "_OPTIONAL_GENERATOR_CASES",
    "_REQUIRED_GENERATOR_CASES",
    "_canonicalize_recipe_case_slots",
    "_compact_audit_contract",
    "_materialize_recipe_boundary_parameters",
    "_normalize_recipe_case_identity",
    "_normalize_recipe_semantic_goal",
    "ARTIFACT_AUDIT_TOTAL_SECONDS",
    "ArtifactAuditResult",
    "CONTRACT_SCHEMA_VERSION",
    "CodeCompletionResult",
    "GENERATION_MODES",
    "GENERATOR_BLUEPRINT_SCHEMA_VERSION",
    "GENERATOR_RECIPE_COMPOSER_VERSION",
    "GENERATOR_RECIPE_ENGINE",
    "GENERATOR_RECIPE_SCHEMA_VERSION",
    "GeneratedArtifact",
    "StressPreparation",
    "StressPreparationError",
    "audit_luogu_reference",
    "audit_generated_artifact",
    "compose_generator_recipe_artifact",
    "complete_cpp_code",
    "extract_contract",
    "generate_artifact",
    "generate_generator_blueprint",
    "generate_generator_recipe",
    "normalize_stress_contract",
    "prepare_stress",
    "search_reference",
    "validate_generator_blueprint",
    "validate_generator_recipe",
]
