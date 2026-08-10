"""Persistent orchestration for explicit AI-assisted continuous stress runs."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import threading
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from .canonical import canonical_json_bytes as _canonical_json
from .config import Paths
from .deepseek import DeepSeekCancelScope
from .storage import Database, StressRunRevisionConflict
from .stress import (
    HELPER_COMPILE_FLAG_SETS,
    HELPER_PREFLIGHT_VERSION,
    HelperBundle,
    HelperBundleManager,
    HelperPreflightConfig,
    HelperPreflightError,
    HelperSources,
    LEGACY_TRIO_PROTOCOL,
    LayeredStressRunner,
    SampleCase,
    SandboxBackend,
    SandboxUnavailableError,
    StagedHelperBundle,
    StopToken,
    StressExecutables,
    StressRunConfig,
    StressRunResult,
    TRUSTED_GENERATOR_HARNESS_VERSION,
    WindowsAppContainerBackend,
    cpp_compiler_fingerprint,
    portable_cpp_flags,
    resolve_cpp_compiler,
    sha256_bytes,
    sha256_file,
)
from .stress_ai import (
    ARTIFACT_AUDIT_TOTAL_SECONDS,
    GeneratedArtifact,
    StressPreparation,
    StressProgress,
    audit_generated_artifact,
    extract_contract,
    generate_artifact,
    generate_generator_blueprint,
    generate_generator_recipe,
    compose_generator_recipe_artifact,
    prepare_stress,
    validate_generator_blueprint,
    validate_generator_recipe,
)
from .stress_recipe import (
    GENERATOR_RECIPE_COMPOSER_VERSION,
    GENERATOR_RECIPE_ENGINE,
    GENERATOR_RECIPE_SCHEMA_VERSION,
    RecipeCatalog,
    UnsupportedRecipeError,
)
from .stress_recipe_v2 import (
    GENERATOR_RECIPE_V2_ENGINE,
    compile_static_contract_v2,
    recipe_v2_identity,
)
from .stress_sources import AllowlistedCrawler, source_order_labels
from .usage import merge_usage
from .stress_budget import (
    DEFAULT_PREPARATION_TIMEOUT_SECONDS,
    PreparationBudget,
    PreparationBudgetExhausted,
)
from .stress_checkpoint import (
    CandidateRef,
    StressCheckpointStore,
    generation_identity,
    generation_key,
)


REFERENCE_ROLES = ("reference_primary", "reference_secondary")
DUAL_REFERENCE_PROTOCOL = "dual_reference_v1"


def _next_external_reference(
    artifact: GeneratedArtifact | None,
    *,
    role: str | None = None,
) -> GeneratedArtifact | None:
    """Return the next already-vetted allowlisted source without provider work."""

    if artifact is None or artifact.origin == "ai_generated":
        return None
    alternates = tuple(artifact.source_alternates or ())
    if not alternates:
        return None
    raw = alternates[0]
    if not isinstance(raw, Mapping):
        return None
    code = str(raw.get("code") or "").strip()
    tier = str(raw.get("tier") or "").strip()
    source_kind = {
        "codeforces_official": "codeforces_editorial",
        "luogu_solutions": "luogu_solution",
        "cnblogs": "cnblogs",
        "csdn": "csdn",
    }.get(tier)
    if not code or source_kind is None:
        return None
    static_audit = raw.get("static_audit")
    selected_role = role or artifact.kind
    return GeneratedArtifact(
        kind=selected_role,
        code=code,
        origin=source_kind,
        notes="前一白名单参考解未通过本地门禁，切换同层下一份完整候选。",
        source_url=str(raw.get("url") or "") or None,
        source_title=str(raw.get("title") or "") or None,
        source_sha256=str(raw.get("content_sha256") or "") or None,
        license=str(raw.get("license") or "unknown"),
        static_audit=(dict(static_audit) if isinstance(static_audit, Mapping) else None),
        source_alternates=alternates[1:],
    )


def _use_reference_alternate(
    preparation: StressPreparation,
    alternate: GeneratedArtifact,
    *,
    role: str,
) -> StressPreparation:
    metadata = dict(preparation.generation_metadata)
    counter = f"{role}_alternates_used"
    metadata[counter] = (
        int(metadata.get(counter) or 0) + 1
    )
    return replace(preparation, **{role: alternate}, generation_metadata=metadata)


def _generator_safe_seed_families(
    blueprint: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Return the contract-derived operation families safe for seed variation.

    ``large/random`` is deliberately restricted by blueprint validation to the
    read-only or independently legal streaming subset.  Reusing that subset in
    repair diagnostics keeps the policy problem-agnostic and avoids teaching a
    repair prompt operation names from any benchmark problem.
    """

    if not isinstance(blueprint, Mapping):
        return ()
    raw_cases = blueprint.get("cases")
    if not isinstance(raw_cases, list):
        return ()
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping):
            continue
        if (
            str(raw_case.get("profile") or "").strip().casefold() != "large"
            or str(raw_case.get("case_kind") or "").strip().casefold()
            != "random"
        ):
            continue
        raw_families = raw_case.get("operation_families")
        if not isinstance(raw_families, list):
            return ()
        families: list[str] = []
        for raw_family in raw_families:
            family = str(raw_family or "").strip()
            if family and family not in families:
                families.append(family)
        return tuple(families)
    return ()


def _persistent_generator_seed_requirement(
    history: list[Mapping[str, Any]],
    blueprint: Mapping[str, Any] | None,
) -> str:
    if not any(
        item.get("code") == "stress_generator_seed_variation_failed"
        for item in history
    ):
        return ""
    safe_seed_families = _generator_safe_seed_families(blueprint)
    safe_seed_target = (
        ", ".join(safe_seed_families[:12])
        if safe_seed_families
        else "一个契约明确独立合法的标量字段"
    )
    return (
        "上一轮机器门禁已经证明 seed variation 未满足；当前修复必须同时保留"
        "这一义务，不得只修最新错误。维持确定性的合法状态骨架，并让消费 seed "
        "的 PRNG 实际决定至少一个输出字段。优先使用 blueprint 的安全字段族："
        + safe_seed_target
        + "。连续 seed 窗口仍必须产生至少两种合法 stdout。"
    )


def _generator_repair_invariants(
    blueprint: Mapping[str, Any] | None,
) -> list[str]:
    safe_seed_families = _generator_safe_seed_families(blueprint)
    seed_target = (
        ", ".join(safe_seed_families[:12])
        if safe_seed_families
        else "blueprint 中契约明确独立合法的实际输出字段"
    )
    return [
        "保留每个 case 的 dimensions、声明记录数、operation_families 和 coverage_tags；"
        "实际记录数必须重新计数并与声明相等，已知错误的实际数量不得被当成待保留不变量。"
        "不得通过删除操作族或伪造 observation/manifest 来修复另一项门禁。",
        "所有 uses_seed=true 的 random case 必须消费 seed 初始化的 PRNG，并让连续 seed "
        "窗口产生至少两种合法 stdout；优先让安全字段族决定实际输出参数："
        + seed_target
        + "。",
        "同一 seed 输出必须逐字节确定；有状态操作必须按最终输出顺序校验和更新，"
        "不得随机化、shuffle 或重排有状态操作。",
        "blueprint construction 是未经机器证明的候选策略；若独立 validator 证明其中具体"
        "状态参数或顺序非法，必须在 append/output 前替换，同时保持结构化不变量。",
    ]


def _generator_record_count_hint(
    contract: Mapping[str, Any], details: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Derive a narrow declared-vs-emitted record diagnostic from contract-v3.

    This never accepts or edits generated input.  It only makes an already
    confirmed validator rejection actionable when the complete bounded input
    excerpt has an operation-stream count bound to a first-line scalar field.
    """

    actual = details.get("actual")
    if not isinstance(actual, Mapping) or actual.get("generated_input_truncated"):
        return None
    text = actual.get("generated_input_excerpt")
    if not isinstance(text, str) or not text.strip():
        return None
    syntax = contract.get("syntax")
    sections = syntax.get("sections") if isinstance(syntax, Mapping) else None
    if not isinstance(sections, list) or not sections:
        return None
    operation_sections = [
        section
        for section in sections
        if isinstance(section, Mapping)
        and str(section.get("kind") or "") == "operation_stream"
        and str(section.get("count_from") or "").strip()
    ]
    if len(operation_sections) != 1:
        return None
    operation = operation_sections[0]
    count_from = str(operation.get("count_from") or "").strip()
    section_id, separator, field_name = count_from.partition(".")
    if not separator or not section_id or not field_name:
        return None
    header = sections[0]
    if (
        not isinstance(header, Mapping)
        or str(header.get("id") or "") != section_id
        or str(header.get("kind") or "") != "scalar"
    ):
        return None
    fields = header.get("fields")
    if not isinstance(fields, list):
        return None
    names = [
        str(field.get("name") or "") if isinstance(field, Mapping) else ""
        for field in fields
    ]
    if field_name not in names:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    header_tokens = lines[0].split()
    field_index = names.index(field_name)
    if field_index >= len(header_tokens) or not re.fullmatch(
        r"[+-]?\d+", header_tokens[field_index]
    ):
        return None
    declared = int(header_tokens[field_index])
    variants = operation.get("variants")
    if not isinstance(variants, list):
        return None
    tags = {
        str(variant.get("tag") or "")
        for variant in variants
        if isinstance(variant, Mapping) and str(variant.get("tag") or "")
    }
    if not tags:
        return None
    observed = sum(line.split()[0] in tags for line in lines[1:] if line.split())
    if observed == declared:
        return None
    return {
        "count_from": count_from,
        "declared_records": declared,
        "observed_tagged_records": observed,
        "known_operation_tags": sorted(tags),
    }


_CONFIRMABLE_LOCAL_GATE_CODES = frozenset(
    {
        "stress_generator_coverage_failed",
        "stress_generator_seed_variation_failed",
        "stress_generated_input_invalid",
        "stress_input_validation_failed",
        "stress_validator_positive_probe_failed",
        "stress_validator_negative_probe_failed",
    }
)


def _contract_probe_repair_diagnostic(
    exc: HelperPreflightError, *, repair_attempt: int
) -> dict[str, Any]:
    """Return the only contract-probe facts allowed into a validator prompt.

    Full probe inputs, hashes, IDs, seeds and stderr remain in local evidence.
    A validator repair receives only the bound constraint and the two boolean
    outcomes, so it cannot hard-code a hidden certification case.
    """

    expected = exc.details.get("expected")
    actual = exc.details.get("actual")
    expected_map = expected if isinstance(expected, Mapping) else {}
    actual_map = actual if isinstance(actual, Mapping) else {}
    constraint_id = str(
        expected_map.get("constraint_id")
        or actual_map.get("constraint_id")
        or ""
    )
    return {
        "stage": "pre_audit_machine_gate",
        "artifact": "validator",
        "repair_attempt": int(repair_attempt),
        "profile": "contract_probe",
        "case_kind": "hidden_contract_probe",
        "code": str(exc.code),
        "message": "validator disagreed with an independently certified dynamic-constraint probe",
        "expected": {"constraint_id": constraint_id},
        "actual": {
            "probe_source": "independently_certified_contract",
            "constraint_id": constraint_id,
            "valid_accepted": bool(actual_map.get("valid_accepted")),
            "invalid_accepted": bool(actual_map.get("invalid_accepted")),
        },
    }


def _preflight_repair_witness(exc: HelperPreflightError) -> dict[str, Any] | None:
    """Project only the bounded failing-role witness into an AI repair prompt."""

    if (
        str(exc.artifact) in REFERENCE_ROLES
        and str(exc.case_kind) == "official_sample"
        and isinstance(exc.details.get("input_excerpt"), str)
    ):
        witness = {
            "kind": (
                "official_sample_mismatch"
                if str(exc.code) == "stress_reference_sample_mismatch"
                else "official_sample_execution_failure"
            ),
            "failure_code": str(exc.code)[:120],
            "sample_name": str(exc.details.get("sample_name") or "")[:200],
            "input_excerpt": str(exc.details.get("input_excerpt") or "")[:2000],
            "expected_stdout": str(
                exc.details.get("expected_stdout") or ""
            )[:2000],
        }
        if str(exc.code) == "stress_reference_sample_mismatch":
            witness["actual_stdout"] = str(
                exc.details.get("actual_stdout") or ""
            )[:2000]
        else:
            actual = exc.details.get("actual")
            if isinstance(actual, Mapping):
                failure = actual.get("failure")
                returncode = actual.get("returncode")
                if isinstance(failure, str):
                    witness["execution_failure"] = failure[:120]
                if isinstance(returncode, int) and not isinstance(returncode, bool):
                    witness["returncode"] = returncode
        return witness
    source_safety = exc.details.get("source_safety")
    if (
        exc.profile == "build"
        and exc.case_kind == "source_safety"
        and isinstance(source_safety, Mapping)
    ):
        witness: dict[str, Any] = {"kind": "source_safety"}
        for key, limit in (
            ("rule_id", 80),
            ("matched_token", 80),
            ("excerpt", 240),
        ):
            value = source_safety.get(key)
            if isinstance(value, str) and value:
                witness[key] = value[:limit]
        for key in ("line", "column"):
            value = source_safety.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                witness[key] = value
        return witness
    return None


def _preflight_role_repair_limit(role: str, exc: HelperPreflightError) -> int:
    if role == "generator":
        return 2
    if (
        role in REFERENCE_ROLES
        and (
            (exc.profile == "build" and exc.case_kind == "source_safety")
            or exc.case_kind == "official_sample"
        )
    ):
        # Safety and official-sample failures both have exact, bounded local
        # witnesses.  Permit one additional targeted generation; every
        # replacement still returns through source-safety, compile and the
        # complete helper preflight before it can be applied.
        return 2
    return 1


def _preflight_repair_from_scratch(
    role: str, exc: HelperPreflightError, *, repair_attempt: int
) -> bool:
    if role == "generator" and repair_attempt >= 2:
        return True
    if role not in REFERENCE_ROLES:
        return False
    if exc.profile == "build" and exc.case_kind == "source_safety":
        return True
    failure = str(exc).casefold()
    return exc.case_kind == "official_sample" and (
        "timeout" in failure or "runtime" in failure
    )


def _bundle_source_identity(artifacts: Mapping[str, GeneratedArtifact | None]) -> str:
    digest = hashlib.sha256()
    for role in ("generator", "validator", *REFERENCE_ROLES):
        artifact = artifacts.get(role)
        digest.update(role.encode("ascii"))
        digest.update(b"\0")
        digest.update((artifact.code if artifact is not None else "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _run_locally_confirmed_gate(
    run: Callable[[], Mapping[str, Any]],
    *,
    gate_name: str,
    source_identity: str,
    confirmations: set[tuple[str, str, str, str, str, int]],
) -> dict[str, Any]:
    """Repeat a narrow semantic gate once before paying for source repair.

    The confirmation uses the exact same compiled bundle and seed.  A repeated
    failure remains authoritative; a non-repeated failure is recorded as a
    local transient and never consumes an AI repair allowance.
    """

    try:
        return dict(run())
    except HelperPreflightError as first:
        artifact = str(first.artifact)
        code = str(first.code)
        key = (
            gate_name,
            source_identity,
            artifact,
            code,
            f"{first.profile}/{first.case_kind}",
            int(first.seed),
        )
        if (
            artifact not in {"generator", "validator"}
            or code not in _CONFIRMABLE_LOCAL_GATE_CODES
            or key in confirmations
        ):
            raise
        confirmations.add(key)
        first_summary = {
            "code": code,
            "artifact": artifact,
            "profile": first.profile,
            "case_kind": first.case_kind,
            "seed": first.seed,
            "message": str(first)[:500],
        }
        try:
            result = dict(run())
        except HelperPreflightError as confirmed:
            confirmed.details.setdefault(
                "local_confirmation",
                {"attempted": True, "reproduced": True, "first": first_summary},
            )
            raise confirmed from first
        result["local_confirmation"] = {
            "attempted": True,
            "reproduced": False,
            "recovered_transient": first_summary,
        }
        return result


STRESS_PREPARATION_CACHE_VERSION = 5
STRESS_CONTRACT_PROMPT_VERSION = 10
STRESS_BLUEPRINT_PROMPT_VERSION = 7
STRESS_RECIPE_PROMPT_VERSION = 1
STRESS_ARTIFACT_PROMPT_VERSION = 14
STRESS_PREFLIGHT_VERSION = HELPER_PREFLIGHT_VERSION
STRESS_SAFETY_POLICY_VERSION = 2
STRESS_SANDBOX_POLICY_VERSION = 2
STRESS_BLUEPRINT_POLICY_VERSION = 4
STRESS_GENERATOR_COVERAGE_POLICY_VERSION = 2


def _validate_generator_plan(
    value: Mapping[str, Any], *, contract: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if value.get("engine") in {
        GENERATOR_RECIPE_ENGINE,
        GENERATOR_RECIPE_V2_ENGINE,
    }:
        return validate_generator_recipe(value, contract=contract)
    return validate_generator_blueprint(value, contract=contract)


def _recipe_identity_fields() -> dict[str, Any]:
    catalog = RecipeCatalog.load()
    v2 = dict(recipe_v2_identity())
    return {
        "recipe_schema_version": GENERATOR_RECIPE_SCHEMA_VERSION,
        "recipe_prompt_version": STRESS_RECIPE_PROMPT_VERSION,
        "catalog_sha256": catalog.sha256,
        "composer_version": GENERATOR_RECIPE_COMPOSER_VERSION,
        "recipe_engines": [GENERATOR_RECIPE_ENGINE, GENERATOR_RECIPE_V2_ENGINE],
        "recipe_v2_schema_version": v2["recipe_schema_version"],
        "recipe_v2_catalog_sha256": v2["catalog_sha256"],
        "recipe_v2_composer_version": v2["composer_version"],
    }


def _validator_preflight_succeeded(preflight: Mapping[str, Any]) -> bool:
    return bool(
        preflight.get("independent_input_validator") is True
        and isinstance(preflight.get("validator_probes"), list)
    )

# The benchmark-only pre-apply gate is an internal capability of the local
# stress benchmark.  Public Service/CLI/web entry points never expose it; the
# runtime rejects any gate whose owner module is not the benchmark, so arbitrary
# callbacks cannot be injected through production callers.
_BENCHMARK_PRE_APPLY_GATE_MODULE = "tests.manual.benchmarks.stress_benchmark"


def _compiler_fingerprint() -> str:
    return cpp_compiler_fingerprint("g++", flag_sets=HELPER_COMPILE_FLAG_SETS)


class StressRuntimeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
        self.usage = dict(usage or {})
        for key in ("artifact", "profile", "case_kind", "seed"):
            if key in self.details:
                setattr(self, key, self.details[key])


_STRESS_FAILURE_CATEGORIES = frozenset(
    {"internal", "environment", "provider", "artifact", "execution", "oracle"}
)
_STRESS_FAILURE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:$\[\]-]{1,160}$")
_STRESS_EXCEPTION_CODE = re.compile(r"^[A-Z][A-Za-z0-9_]*(?:Error|Exception)$")


def _safe_stress_failure_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    selected = value.strip()
    return selected if _STRESS_FAILURE_IDENTIFIER.fullmatch(selected) else None


def _safe_stress_failure_message(value: Any) -> str:
    selected = " ".join(str(value or "stress operation failed").split())
    fence = selected.find("```")
    if fence >= 0:
        selected = selected[:fence].rstrip() + " [details omitted]"
    brace = selected.find("{")
    if brace >= 0:
        selected = selected[:brace].rstrip(" :=") + " [details omitted]"
    selected = re.sub(r"\b[A-Za-z]:[\\/]\S+", "[path omitted]", selected)
    selected = re.sub(
        r"(?<!\w)/(?:home|Users|tmp|var|etc)/\S+",
        "[path omitted]",
        selected,
        flags=re.IGNORECASE,
    )
    return selected[:500] or "stress operation failed"


def _safe_stress_primary_failure(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in (
        "role",
        "stage",
        "substage",
        "code",
        "profile",
        "case_kind",
        "artifact",
    ):
        selected = _safe_stress_failure_identifier(value.get(key))
        if selected is not None:
            result[key] = selected
    path = _safe_stress_failure_identifier(value.get("path"))
    if path is not None:
        result["path"] = path
    category = value.get("category")
    if isinstance(category, str) and category in _STRESS_FAILURE_CATEGORIES:
        result["category"] = category
    cause_type = value.get("cause_type")
    if (
        isinstance(cause_type, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,119}", cause_type)
    ):
        result["cause_type"] = cause_type
    if "message" in value:
        result["message"] = _safe_stress_failure_message(value.get("message"))[:320]
    if "stderr" in value:
        stderr = _safe_stress_failure_message(value.get("stderr"))[:500]
        if stderr and stderr != "stress operation failed":
            result["stderr"] = stderr
    attempts = value.get("attempts")
    if isinstance(attempts, int) and not isinstance(attempts, bool):
        if 0 <= attempts <= 100:
            result["attempts"] = attempts
    elif isinstance(attempts, Mapping):
        safe_attempts: dict[str, int] = {}
        for role, count in list(attempts.items())[:4]:
            safe_role = _safe_stress_failure_identifier(role)
            if (
                safe_role is not None
                and isinstance(count, int)
                and not isinstance(count, bool)
                and 0 <= count <= 100
            ):
                safe_attempts[safe_role] = count
        if safe_attempts:
            result["attempts"] = safe_attempts
    elapsed = value.get("elapsed")
    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
        result["elapsed"] = max(0.0, min(float(elapsed), 86400.0))
    seed = value.get("seed")
    if isinstance(seed, int) and not isinstance(seed, bool):
        result["seed"] = seed
    return result or None


def _stress_failure_category(
    *, code: str, root_cause_code: str, cause_type: str, phase: str, stage: str
) -> str:
    combined = " ".join(
        (code, root_cause_code, cause_type, phase, stage)
    ).casefold()
    if root_cause_code == "stress_internal_error" or cause_type in {
        "AssertionError",
        "AttributeError",
        "KeyError",
        "TypeError",
    }:
        return "internal"
    if any(token in combined for token in ("oracle", "gold_", "pre_apply_gate")):
        return "oracle"
    if any(
        token in combined
        for token in (
            "deepseek",
            "provider",
            "network_error",
            "rate_limit",
            "server_error",
            "request_cancelled",
            "protocol_error",
        )
    ):
        return "provider"
    if any(
        token in combined
        for token in (
            "sandbox",
            "compiler",
            "compile_",
            "filesystem",
            "environment",
            "setup_active",
            "cleanup",
            "unavailable",
            "resource",
        )
    ):
        return "environment"
    if any(
        token in combined
        for token in (
            "artifact",
            "recipe",
            "blueprint",
            "contract",
            "generator",
            "reference",
            "validator",
            "preflight",
            "certification",
            "source_",
        )
    ):
        return "artifact"
    if any(
        token in combined
        for token in (
            "execution",
            "controlled_run",
            "run:",
            "mismatch",
            "fault",
            "crash",
            "timeout",
            "output",
        )
    ):
        return "execution"
    return "internal"


def normalize_stress_failure(
    exc: BaseException, *, phase: str, stage: str
) -> dict[str, Any]:
    """Return a stable, redacted failure envelope shared by all entry points."""

    outer_code = _safe_stress_failure_identifier(getattr(exc, "code", None))
    code = outer_code or "stress_internal_error"
    raw_primary = _primary_preparation_failure(exc)
    primary = _safe_stress_primary_failure(raw_primary)
    nested_code = (
        _safe_stress_failure_identifier(primary.get("code"))
        if primary is not None
        else None
    )
    cause_type = (
        str(primary.get("cause_type"))
        if primary is not None and primary.get("cause_type")
        else exc.__class__.__name__
    )
    if nested_code is not None and _STRESS_EXCEPTION_CODE.fullmatch(nested_code):
        cause_type = nested_code
        root_cause_code = "stress_internal_error"
    else:
        root_cause_code = nested_code or code
    selected_phase = _safe_stress_failure_identifier(phase) or "unknown"
    selected_stage = (
        _safe_stress_failure_identifier(primary.get("stage"))
        if primary is not None
        else None
    )
    if selected_stage is None:
        details = getattr(exc, "details", None)
        if isinstance(details, Mapping):
            selected_stage = _safe_stress_failure_identifier(
                details.get("failure_stage") or details.get("stage")
            )
    selected_stage = selected_stage or _safe_stress_failure_identifier(stage) or "unknown"
    primary_category = primary.get("category") if primary is not None else None
    category = (
        str(primary_category)
        if primary_category in _STRESS_FAILURE_CATEGORIES
        else _stress_failure_category(
            code=code,
            root_cause_code=root_cause_code,
            cause_type=cause_type,
            phase=selected_phase,
            stage=selected_stage,
        )
    )
    assert category in _STRESS_FAILURE_CATEGORIES
    return {
        "code": code,
        "root_cause_code": root_cause_code,
        "category": category,
        "cause_type": cause_type,
        "message": _safe_stress_failure_message(exc),
        "failure_phase": selected_phase,
        "failure_stage": selected_stage,
        "primary_failure": primary,
    }


def _add_usage(total: dict[str, Any], usage: Mapping[str, Any]) -> None:
    merge_usage(
        total,
        usage,
        flatten_reasoning=False,
        preserve_scalars=False,
        bool_or_keys={"fast_fallback_used"},
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_field(row: Mapping[str, Any], key: str) -> Any:
    try:
        return json.loads(str(row[key] or "{}"))
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}


def _row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in list(result):
        if key.endswith("_json"):
            result[key[: -len("_json")]] = _json_field(row, key)
    config = result.get("config")
    if isinstance(config, Mapping):
        try:
            result["profile_version"] = int(config.get("profile_version") or 1)
        except (TypeError, ValueError):
            result["profile_version"] = 1
        result["unvalidated"] = bool(config.get("unvalidated", False))
        result["degraded_reason"] = str(config.get("degraded_reason") or "")
        result["validator_requested"] = bool(
            config.get("validator_requested", False)
        )
        result["validator_certified"] = bool(
            config.get("validator_certified", False)
        )
    return result


def _primary_preparation_failure(exc: BaseException) -> dict[str, Any] | None:
    """Return the safe, displayable root cause of a parallel helper failure."""
    details = getattr(exc, "details", None)
    if not isinstance(details, Mapping):
        return None
    explicit = details.get("primary_failure")
    if isinstance(explicit, Mapping):
        return dict(explicit)
    roles = details.get("roles")
    if not isinstance(roles, Mapping) or not roles:
        return None
    # A single role is unambiguous. Multi-role producers should supply
    # primary_failure in completion order; falling back to insertion order is
    # deterministic and avoids relabelling a failure as the last UI callback.
    role, raw = next(iter(roles.items()))
    failure = dict(raw) if isinstance(raw, Mapping) else {"message": str(raw)}
    failure.setdefault("role", str(role))
    return failure


def _preparation_failure_label(failure: Mapping[str, Any]) -> tuple[str, str]:
    role = str(failure.get("role") or "helper").strip()
    code = str(failure.get("code") or "").strip()
    raw_path = failure.get("path")
    nested = failure.get("details")
    if not raw_path and isinstance(nested, Mapping):
        raw_path = nested.get("path")
    path = str(raw_path or "").strip()
    message = str(failure.get("message") or code or "准备失败").strip()[:320]
    try:
        attempts = int(failure.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if role == "generator" and code == "stress_blueprint_invalid":
        stage = "generate_generator"
        prefix = "generator blueprint 校验失败"
        if path:
            prefix += f"（{path}）"
        if attempts:
            prefix += f"；修复尝试 {attempts}/2"
        return stage, f"{prefix}：{message}"
    if str(failure.get("substage") or "") == "preflight":
        stage = "preflight_helpers"
        profile = str(failure.get("profile") or "").strip()
        case_kind = str(failure.get("case_kind") or "").strip()
        location = "/".join(part for part in (profile, case_kind) if part)
        prefix = f"{role} 写入前预验失败"
        if location:
            prefix += f"（{location}）"
        if attempts:
            limit = 2 if role == "generator" else 1
            prefix += f"；修复尝试 {attempts}/{limit}"
        return stage, f"{prefix}：{message}"
    stage = "prepare_reference" if role in REFERENCE_ROLES else f"generate_{role}"
    suffix = f"；尝试 {attempts}" if attempts else ""
    return stage, f"{role} 准备失败{suffix}：{message}"


def _role_failure_details(
    exc: BaseException,
    *,
    role: str,
    stage: str,
    substage: str,
    elapsed: float,
    attempts: int,
) -> dict[str, Any]:
    nested = getattr(exc, "details", None)
    path = str(nested.get("path") or "") if isinstance(nested, Mapping) else ""
    result: dict[str, Any] = {
        "stage": stage,
        "substage": substage,
        "role": role,
        "elapsed": round(max(0.0, elapsed), 3),
        "code": str(getattr(exc, "code", exc.__class__.__name__)),
        "message": str(exc)[:500],
        "usage": dict(getattr(exc, "usage", {}) or {}),
        "attempts": max(0, int(attempts)),
    }
    if path:
        result["path"] = path[:200]
    for key in ("profile", "case_kind", "seed"):
        value = getattr(exc, key, None)
        if value is not None:
            result[key] = value
    if isinstance(nested, Mapping):
        stderr = nested.get("stderr")
        if isinstance(stderr, (str, bytes)):
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            stderr = str(stderr).strip()
            if stderr:
                result["stderr"] = stderr[-2000:]
        for key in (
            "expected",
            "actual",
            "protocol_error",
            "prior_protocol_error",
        ):
            value = nested.get(key)
            if isinstance(value, (Mapping, list, tuple, str, int, float, bool)):
                result[key] = value
    return result


def _initial_repair_counts(preparation: StressPreparation) -> dict[str, int]:
    """Seed source-role repair counts without charging recipe normalization.

    Recipe/blueprint repair has its own prompt, token cap, and evidence.  It is
    not a failed generator source attempt, so charging it against the two
    generator code repairs can leave a newly generated source with zero chance
    to respond to deterministic compile/capability diagnostics.
    """
    def role_repairs(role: str) -> int:
        try:
            return max(0, int(preparation.usage.get(f"{role}_repairs_used") or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "generator": min(2, role_repairs("generator")),
        "validator": min(1, role_repairs("validator")),
        "reference_primary": min(1, role_repairs("reference_primary")),
        "reference_secondary": min(1, role_repairs("reference_secondary")),
    }


def _stress_config(
    raw: Mapping[str, Any],
    *,
    first_seed: int,
    schedule_offset: int = 0,
) -> StressRunConfig:
    """Load the current profile-v2 config and reject retired protocols."""
    try:
        profile_version = int(raw.get("profile_version") or 0)
    except (TypeError, ValueError):
        profile_version = 0
    if profile_version != 2:
        raise StressRuntimeError(
            "stress_profile_unsupported",
            "该持续对拍使用已停用的旧协议，不能继续；请重新执行 AI 准备",
        )
    values = {
        key: value
        for key, value in dict(raw).items()
        if key in StressRunConfig.__dataclass_fields__
    }
    values["profile_version"] = 2
    values["oracle_protocol"] = str(
        raw.get("oracle_protocol") or LEGACY_TRIO_PROTOCOL
    )
    values["first_seed"] = int(first_seed)
    values["schedule_offset"] = int(schedule_offset)
    return StressRunConfig(**values)


class StressCoordinator:
    """Own background stress threads while SQLite remains the durable truth."""

    def __init__(
        self,
        paths: Paths,
        *,
        sandbox_factory: Callable[[], SandboxBackend] = WindowsAppContainerBackend,
        crawler_factory: Callable[[], AllowlistedCrawler] = AllowlistedCrawler,
    ) -> None:
        self.paths = paths
        self._sandbox_factory = sandbox_factory
        self._crawler_factory = crawler_factory
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._runners: dict[str, LayeredStressRunner] = {}

    def status(self) -> dict[str, Any]:
        capability = self._sandbox_factory().probe()
        active = None
        if self.paths.database.is_file():
            with Database(self.paths.database) as db:
                row = db.active_stress_run()
                active = _row_dict(row) if row is not None else None
        return {
            "ok": True,
            "sandbox": {
                "available": capability.available,
                "backend": capability.backend,
                "reason": capability.reason,
            },
            # Derived from stress_sources so the reported order always matches
            # the order search_reference actually attempts.  It is per-platform:
            # a Codeforces problem never reads a Luogu editorial, and a Luogu
            # problem tries cnblogs before the Luogu solution index.
            "source_order": {
                platform: source_order_labels(platform)
                for platform in ("codeforces", "luogu")
            },
            "active_run": active,
        }

    def _checkpoint_candidates(
        self,
        preparation: StressPreparation,
        *,
        platform: str,
        problem_id: str,
        statement: str,
        model_settings: Mapping[str, Any],
        generation_mode: str,
        ai_run_id: str | None,
    ) -> dict[str, CandidateRef]:
        """Persist every completed role independently of sibling outcomes."""

        stored_problem_id = (
            problem_id[2:]
            if platform == "codeforces" and problem_id.upper().startswith("CF")
            else problem_id
        )
        artifacts = {
            "generator": preparation.generator,
            "validator": preparation.validator,
            "reference_primary": preparation.reference_primary,
            "reference_secondary": preparation.reference_secondary,
        }
        references: dict[str, CandidateRef] = {}
        with Database(self.paths.database) as db:
            db.upsert_problem(
                {"platform": platform, "problem_id": stored_problem_id}
            )
            store = StressCheckpointStore(
                db,
                platform=platform,
                problem_id=stored_problem_id,
                producer_ai_run_id=ai_run_id,
            )
            for role, artifact in artifacts.items():
                if artifact is None:
                    continue
                identity = generation_identity(
                    platform=platform,
                    problem_id=stored_problem_id,
                    role=role,
                    model=str(model_settings.get("model") or ""),
                    mode=generation_mode,
                    prompt=f"stress-artifact-v{STRESS_ARTIFACT_PROMPT_VERSION}:{role}",
                    prompt_version=STRESS_ARTIFACT_PROMPT_VERSION,
                    statement=statement,
                    semantic_inputs={
                        "contract": preparation.contract,
                        "generator_blueprint": (
                            preparation.generator_blueprint
                            if role == "generator"
                            else None
                        ),
                        "generator_recipe_runtime": (
                            _recipe_identity_fields() if role == "generator" else None
                        ),
                    },
                )
                references[role] = store.save_candidate(
                    role=role,
                    source_code=artifact.code,
                    identity=identity,
                    source_kind=artifact.origin,
                    provenance={
                        "source_url": artifact.source_url or "",
                        "source_title": artifact.source_title or "",
                        "source_sha256": artifact.source_sha256 or "",
                    },
                    usage=preparation.usage,
                )
        return references

    def _load_checkpoint_artifacts(
        self,
        *,
        platform: str,
        problem_id: str,
        statement: str,
        contract: Mapping[str, Any],
        generator_blueprint: Mapping[str, Any] | None,
        model_settings: Mapping[str, Any],
        generation_mode: str,
    ) -> dict[str, GeneratedArtifact]:
        """Reuse only role candidates with a passed local compile proof."""

        stored_problem_id = (
            problem_id[2:]
            if platform == "codeforces" and problem_id.upper().startswith("CF")
            else problem_id
        )
        loaded: dict[str, GeneratedArtifact] = {}
        with Database(self.paths.database) as db:
            for role in ("generator", "validator", *REFERENCE_ROLES):
                identity = generation_identity(
                    platform=platform,
                    problem_id=stored_problem_id,
                    role=role,
                    model=str(model_settings.get("model") or ""),
                    mode=generation_mode,
                    prompt=f"stress-artifact-v{STRESS_ARTIFACT_PROMPT_VERSION}:{role}",
                    prompt_version=STRESS_ARTIFACT_PROMPT_VERSION,
                    statement=statement,
                    semantic_inputs={
                        "contract": dict(contract),
                        "generator_blueprint": (
                            dict(generator_blueprint or {})
                            if role == "generator"
                            else None
                        ),
                        "generator_recipe_runtime": (
                            _recipe_identity_fields() if role == "generator" else None
                        ),
                    },
                )
                rows = db.stress_artifact_candidates(
                    generation_key=generation_key(identity),
                    platform=platform,
                    problem_id=stored_problem_id,
                    role=role,
                    status="generated",
                    limit=10,
                )
                for row in rows:
                    proofs = db.stress_artifact_proofs(candidate_id=str(row["id"]))
                    if any(str(proof["status"]) == "failed" for proof in proofs):
                        continue
                    if not any(
                        str(proof["proof_kind"]) == "source_safety_compile"
                        and str(proof["status"]) == "passed"
                        for proof in proofs
                    ):
                        continue
                    provenance = _json_field(row, "provenance_json")
                    loaded[role] = GeneratedArtifact(
                        role,
                        str(row["source_code"]),
                        str(row["source_kind"]),
                        "复用已通过源码安全与本地编译的角色 checkpoint",
                        source_url=(
                            str(provenance.get("source_url") or "") or None
                            if isinstance(provenance, Mapping)
                            else None
                        ),
                        source_title=(
                            str(provenance.get("source_title") or "") or None
                            if isinstance(provenance, Mapping)
                            else None
                        ),
                        source_sha256=(
                            str(provenance.get("source_sha256") or "") or None
                            if isinstance(provenance, Mapping)
                            else None
                        ),
                    )
                    break
        return loaded

    def _preparation_cache_identity(
        self,
        *,
        platform: str,
        problem_id: str,
        statement: str,
        compare: str,
        include_generator: bool,
        include_validator: bool = True,
        include_reference_primary: bool = True,
        include_reference_secondary: bool = True,
        include_large: bool,
        model_settings: Mapping[str, Any],
        sandbox: SandboxBackend,
        generation_mode: str = "hybrid",
        contract: Mapping[str, Any] | None = None,
        generator_blueprint: Mapping[str, Any] | None = None,
        include_brute: bool | None = None,
        include_reference: bool | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if include_brute is not None:
            include_reference_secondary = bool(include_brute)
        if include_reference is not None:
            include_reference_primary = bool(include_reference)
        sample_digest = hashlib.sha256()
        for sample in self._samples(problem_id, platform=platform):
            sample_digest.update(sample.name.encode("utf-8"))
            sample_digest.update(b"\0")
            sample_digest.update(sample.input_data)
            sample_digest.update(b"\0")
            sample_digest.update(sample.expected_output)
            sample_digest.update(b"\0")
        metadata: dict[str, Any] = {
            "kind": "stress_bundle",
            "cache_version": STRESS_PREPARATION_CACHE_VERSION,
            "prompt_versions": {
                "contract": STRESS_CONTRACT_PROMPT_VERSION,
                "blueprint": STRESS_BLUEPRINT_PROMPT_VERSION,
                "recipe": STRESS_RECIPE_PROMPT_VERSION,
                "artifact": STRESS_ARTIFACT_PROMPT_VERSION,
            },
            **_recipe_identity_fields(),
            "preflight_version": STRESS_PREFLIGHT_VERSION,
            "safety_policy_version": STRESS_SAFETY_POLICY_VERSION,
            "sandbox_policy_version": STRESS_SANDBOX_POLICY_VERSION,
            "platform": platform,
            "problem_id": problem_id,
            "statement_sha256": sha256_bytes(statement.encode("utf-8")),
            "compare": compare,
            "include_large": bool(include_large),
            "roles": {
                "generator": bool(include_generator),
                "validator": bool(include_validator),
                "reference_primary": bool(include_reference_primary),
                "reference_secondary": bool(include_reference_secondary),
            },
            "oracle_protocol": DUAL_REFERENCE_PROTOCOL,
            "model": str(model_settings["model"]),
            "generation_mode": str(generation_mode),
            "reasoning_effort": str(model_settings.get("reasoning_effort") or "high"),
            "contract_sha256": (
                sha256_bytes(_canonical_json(contract)) if contract is not None else ""
            ),
            "generator_blueprint_sha256": (
                sha256_bytes(_canonical_json(generator_blueprint))
                if generator_blueprint is not None
                else ""
            ),
            "blueprint_policy_version": STRESS_BLUEPRINT_POLICY_VERSION,
            "coverage_policy_version": STRESS_GENERATOR_COVERAGE_POLICY_VERSION,
            "trusted_generator_harness_version": TRUSTED_GENERATOR_HARNESS_VERSION,
            "compiler_fingerprint": _compiler_fingerprint(),
            "sandbox": f"{sandbox.__class__.__module__}.{sandbox.__class__.__qualname__}",
            "samples_sha256": sample_digest.hexdigest(),
        }
        return sha256_bytes(_canonical_json(metadata)), metadata

    @staticmethod
    def _contract_cache_identity(
        *,
        problem_id: str,
        statement: str,
        compare: str,
        model_settings: Mapping[str, Any],
        generation_mode: str = "hybrid",
    ) -> tuple[str, dict[str, Any]]:
        metadata = {
            "kind": "stress_contract",
            # Contract and blueprint identities evolve independently so a
            # blueprint-only repair does not invalidate provider contract work.
            "prompt_version": STRESS_CONTRACT_PROMPT_VERSION,
            "problem_id": problem_id,
            "statement_sha256": sha256_bytes(statement.encode("utf-8")),
            "compare": compare,
            "model": str(model_settings["model"]),
            "generation_mode": str(generation_mode),
            "reasoning_effort": str(model_settings.get("reasoning_effort") or "high"),
        }
        return sha256_bytes(_canonical_json(metadata)), metadata

    @staticmethod
    def _blueprint_cache_identity(
        *,
        problem_id: str,
        statement: str,
        contract: Mapping[str, Any],
        model_settings: Mapping[str, Any],
        generation_mode: str,
    ) -> tuple[str, dict[str, Any]]:
        metadata = {
            "kind": "stress_generator_plan",
            "prompt_version": STRESS_BLUEPRINT_PROMPT_VERSION,
            **_recipe_identity_fields(),
            "blueprint_policy_version": STRESS_BLUEPRINT_POLICY_VERSION,
            "coverage_policy_version": STRESS_GENERATOR_COVERAGE_POLICY_VERSION,
            "problem_id": problem_id,
            "statement_sha256": sha256_bytes(statement.encode("utf-8")),
            "contract_sha256": sha256_bytes(_canonical_json(contract)),
            "model": str(model_settings["model"]),
            "generation_mode": str(generation_mode),
            "reasoning_effort": str(model_settings.get("reasoning_effort") or "high"),
        }
        return sha256_bytes(_canonical_json(metadata)), metadata

    def _cached_generator_blueprint(
        self,
        cache_key: str,
        expected_identity: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str] | None:
        with Database(self.paths.database) as db:
            alias = db.stress_preparation_cache(cache_key)
            if alias is None:
                return None
            alias_metadata = _json_field(alias, "metadata_json")
            if not isinstance(alias_metadata, Mapping):
                return None
            replacement_key = str(
                alias_metadata.get("replacement_cache_key") or ""
            ).strip()
            selected_key = replacement_key or cache_key
            selected = (
                db.stress_preparation_cache(selected_key)
                if selected_key != cache_key
                else alias
            )
        if selected is None:
            return None
        payload = _json_field(selected, "payload_json")
        metadata = _json_field(selected, "metadata_json")
        candidate = payload.get("blueprint") if isinstance(payload, Mapping) else None
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("status") != "validated"
            or metadata.get("cache_identity") != dict(expected_identity)
            or not isinstance(candidate, Mapping)
        ):
            return None
        try:
            return _validate_generator_plan(candidate), selected_key
        except Exception:
            return None

    def _save_generator_blueprint(
        self,
        cache_key: str,
        cache_identity: Mapping[str, Any],
        blueprint: Mapping[str, Any],
        *,
        replace_alias: bool = False,
    ) -> str:
        validated = _validate_generator_plan(blueprint)
        content_hash = sha256_bytes(_canonical_json(validated))
        with Database(self.paths.database) as db:
            alias = db.stress_preparation_cache(cache_key)
            if alias is None and not replace_alias:
                db.save_stress_preparation_cache(
                    cache_key,
                    payload={"blueprint": validated},
                    metadata={
                        "cache_identity": dict(cache_identity),
                        "status": "validated",
                        "blueprint_sha256": content_hash,
                    },
                )
                return cache_key
            content_key = sha256_bytes(
                _canonical_json(
                    {
                        "kind": "stress_generator_blueprint_content",
                        "identity": dict(cache_identity),
                        "blueprint_sha256": content_hash,
                    }
                )
            )
            db.save_stress_preparation_cache(
                content_key,
                payload={"blueprint": validated},
                metadata={
                    "cache_identity": dict(cache_identity),
                    "status": "validated",
                    "blueprint_sha256": content_hash,
                },
            )
            if alias is None:
                db.save_stress_preparation_cache(
                    cache_key,
                    payload={"blueprint_alias": True},
                    metadata={
                        "cache_identity": dict(cache_identity),
                        "status": "validated",
                        "replacement_cache_key": content_key,
                    },
                )
            else:
                db.merge_stress_preparation_cache_metadata(
                    cache_key,
                    {
                        "cache_identity": dict(cache_identity),
                        "status": "validated",
                        "replacement_cache_key": content_key,
                    },
                )
            return content_key

    def _invalidate_generator_blueprint(self, cache_key: str) -> None:
        with Database(self.paths.database) as db:
            if db.stress_preparation_cache(cache_key) is not None:
                db.merge_stress_preparation_cache_metadata(
                    cache_key,
                    {
                        "status": "invalidated",
                        "replacement_cache_key": "",
                    },
                )

    def _cached_preparation(
        self,
        cache_key: str,
        expected_meta: Mapping[str, Any],
        *,
        platform: str,
        problem_id: str,
    ) -> tuple[
        HelperBundle,
        StressPreparation,
        dict[str, Any],
        dict[str, dict[str, Any]],
    ] | None:
        stored_problem_id = (
            problem_id[2:]
            if platform == "codeforces" and problem_id.upper().startswith("CF")
            else problem_id
        )
        with Database(self.paths.database) as db:
            cache = db.stress_preparation_cache(cache_key)
            row = db.stress_artifact_bundle_for_cache_key(
                cache_key, platform=platform, problem_id=stored_problem_id
            )
            if cache is None or row is None or str(row["status"]) != "applied":
                return None
            cache_payload = _json_field(cache, "payload_json")
            preparation_meta = _json_field(row, "preparation_meta_json")
            manifest = _json_field(row, "baseline_manifest_json")
            contract = _json_field(row, "contract_json")
            artifacts = db.stress_artifacts(str(row["id"]))
            certification_key = str(row["certification_key"] or "")
            certification = (
                db.stress_bundle_certification(certification_key)
                if certification_key
                else None
            )
        if not isinstance(cache_payload, Mapping) or cache_payload.get(
            "cache_identity"
        ) != dict(expected_meta):
            return None
        if not isinstance(preparation_meta, Mapping) or preparation_meta.get(
            "cache_identity"
        ) != dict(expected_meta):
            return None
        if not isinstance(manifest, Mapping) or not isinstance(contract, Mapping):
            return None
        if str(preparation_meta.get("contract_sha256") or "") != sha256_bytes(
            _canonical_json(contract)
        ):
            return None
        blueprint = preparation_meta.get("generator_blueprint")
        expected_blueprint_hash = str(
            preparation_meta.get("generator_blueprint_sha256") or ""
        )
        if expected_blueprint_hash:
            if not isinstance(blueprint, Mapping) or sha256_bytes(
                _canonical_json(blueprint)
            ) != expected_blueprint_hash:
                return None
        helper_paths = dict(manifest.get("helper_paths") or {})
        applied_hashes = dict(manifest.get("applied_hashes") or {})
        release_executables = dict(manifest.get("release_executables") or {})
        executable_hashes = dict(
            preparation_meta.get("release_executable_hashes") or {}
        )
        expected_roles = expected_meta.get("roles")
        validator_required = bool(
            isinstance(expected_roles, Mapping)
            and expected_roles.get("validator") is True
        )
        if validator_required:
            if certification is None or str(certification["status"]) != "valid":
                return None
            certification_identity = _json_field(
                certification, "certification_identity_json"
            )
            certification_preflight = _json_field(
                certification, "preflight_json"
            )
            validator_identity = (
                certification_identity.get("validator")
                if isinstance(certification_identity, Mapping)
                else None
            )
            if (
                not isinstance(validator_identity, Mapping)
                or str(validator_identity.get("candidate_id") or "")
                in {"", "unvalidated"}
                or not isinstance(certification_preflight, Mapping)
                or not _validator_preflight_succeeded(certification_preflight)
            ):
                return None
        if set(helper_paths) != {"generator", *REFERENCE_ROLES}:
            return None
        expected_release_roles = {"generator", *REFERENCE_ROLES}
        if validator_required:
            expected_release_roles.add("validator")
        if set(release_executables) != expected_release_roles:
            return None
        for role in ("generator", *REFERENCE_ROLES):
            source = Path(str(helper_paths.get(role) or ""))
            executable = Path(str(release_executables.get(role) or ""))
            if (
                not source.is_file()
                or sha256_file(source) != str(applied_hashes.get(role) or "")
                or not executable.is_file()
                or sha256_file(executable)
                != str(executable_hashes.get(role) or "")
            ):
                return None
        if validator_required:
            validator_executable = Path(
                str(release_executables.get("validator") or "")
            )
            if (
                not validator_executable.is_file()
                or sha256_file(validator_executable)
                != str(executable_hashes.get("validator") or "")
            ):
                return None
        if dict(preparation_meta.get("helper_hashes") or {}) != applied_hashes:
            return None
        rows = {str(item["kind"]): item for item in artifacts}
        expected_artifact_roles = {"generator", *REFERENCE_ROLES}
        if validator_required:
            expected_artifact_roles.add("validator")
        if set(rows) != expected_artifact_roles:
            return None
        generated: dict[str, GeneratedArtifact] = {}
        audits: dict[str, dict[str, Any]] = {}
        preflight: dict[str, Any] = {}
        for role, artifact_row in rows.items():
            code = str(artifact_row["source_code"])
            if sha256_bytes(code.encode("utf-8")) != str(artifact_row["source_hash"]):
                return None
            validation = _json_field(artifact_row, "validation_json")
            if not isinstance(validation, Mapping):
                return None
            role_preflight = validation.get("preflight")
            if not isinstance(role_preflight, Mapping) or int(
                role_preflight.get("preflight_version") or 0
            ) != STRESS_PREFLIGHT_VERSION:
                return None
            if not preflight:
                preflight = dict(role_preflight)
            audit = validation.get("ai_audit")
            origin = str(artifact_row["source_kind"])
            if origin == "local_existing":
                return None
            if origin == "ai_generated" and not (
                isinstance(audit, Mapping) and audit.get("accepted") is True
            ):
                return None
            if isinstance(audit, Mapping):
                audits[role] = dict(audit)
            metadata = _json_field(artifact_row, "metadata_json")
            generated[role] = GeneratedArtifact(
                role,
                code,
                origin,
                str(metadata.get("notes") or "") if isinstance(metadata, Mapping) else "",
                source_url=artifact_row["source_url"],
                source_title=artifact_row["source_title"],
                source_sha256=artifact_row["source_content_hash"],
                license=artifact_row["source_license"],
                static_audit=(
                    dict(metadata.get("static_audit") or {})
                    if isinstance(metadata, Mapping)
                    else None
                ),
            )
        if validator_required and not _validator_preflight_succeeded(preflight):
            return None
        try:
            bundle = HelperBundle(
                bundle_id=str(manifest["bundle_id"]),
                problem_id=str(manifest["problem_id"]),
                primary_source=str(manifest["primary_source"]),
                helper_paths=helper_paths,
                baseline_hashes=dict(manifest.get("baseline_hashes") or {}),
                applied_hashes=applied_hashes,
                backup_dir=str(manifest["backup_dir"]),
                staging_dir=str(manifest["staging_dir"]),
                created_at=str(manifest["created_at"]),
                release_executables=release_executables,
                validation=dict(manifest.get("validation") or {}),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return (
            bundle,
            StressPreparation(
                dict(contract),
                generated["generator"],
                generated["reference_primary"],
                generated["reference_secondary"],
                {},
                dict(blueprint) if isinstance(blueprint, Mapping) else None,
                {
                    "generation_mode": str(
                        preparation_meta.get("generation_mode") or "hybrid"
                    ),
                    "fast_fallback_used": bool(
                        preparation_meta.get("fast_fallback_used", False)
                    ),
                },
                generated.get("validator"),
            ),
            preflight,
            audits,
        )

    def _audit_and_repair_generated_artifacts(
        self,
        client: Any,
        preparation: StressPreparation,
        *,
        problem_id: str,
        statement: str,
        model_settings: Mapping[str, Any],
        generation_mode: str = "hybrid",
        progress_callback: StressProgress | None,
        budget: PreparationBudget | None = None,
        repair_counts: dict[str, int] | None = None,
        blueprint_cache_key: str | None = None,
        blueprint_cache_identity: Mapping[str, Any] | None = None,
        machine_gate_evidence: Mapping[str, Any] | None = None,
        machine_gate_codes: Mapping[str, str] | None = None,
        cancel_scope: DeepSeekCancelScope | None = None,
    ) -> tuple[StressPreparation, dict[str, dict[str, Any]], dict[str, int]]:
        """Audit each AI helper independently and repair only the rejected role."""
        artifacts: dict[str, GeneratedArtifact | None] = {
            "generator": preparation.generator,
            "validator": preparation.validator,
            "reference_primary": preparation.reference_primary,
            "reference_secondary": preparation.reference_secondary,
        }
        counts = repair_counts if repair_counts is not None else {
            "generator": 0,
            "validator": 0,
            "reference_primary": 0,
            "reference_secondary": 0,
        }
        usage = dict(preparation.usage)
        audits: dict[str, dict[str, Any]] = {}
        while True:
            generated_roles = [
                role
                for role in ("generator", "validator", *REFERENCE_ROLES)
                if (
                    artifacts[role] is not None
                    and artifacts[role].origin == "ai_generated"
                    and not bool((artifacts[role].static_audit or {}).get("accepted"))
                )
            ]
            if progress_callback is not None:
                progress_callback(
                    "audit_helpers",
                    (
                        f"并行 AI 静态复核 {len(generated_roles)} 个 helper"
                        if generated_roles
                        else "检查 helper 来源"
                    ),
                    7,
                    10,
                )
            if not generated_roles:
                break
            # Each request contains exactly one helper, but all requests share
            # one wall-clock deadline.  Parallelism makes the documented
            # 30-second budget meaningful instead of giving the first role the
            # entire window and starving the remaining roles.
            audit_window = (
                budget.soft_budget("audit_helpers")
                if budget is not None
                else ARTIFACT_AUDIT_TOTAL_SECONDS
            )
            deadline = time.monotonic() + audit_window
            if budget is not None:
                budget.require("audit_helpers")
                available_for_audit = budget.available_after_reserve(65.0)
                if available_for_audit < 0.1:
                    budget.provider_timeout(
                        "audit_helpers",
                        reserve_seconds=65.0,
                    )
                deadline = min(
                    deadline,
                    time.monotonic() + available_for_audit,
                )
            with ThreadPoolExecutor(
                max_workers=len(generated_roles),
                thread_name_prefix="stress-audit",
            ) as executor:
                futures = {}
                for role in generated_roles:
                    started = budget.clock() if budget is not None else time.monotonic()
                    future = executor.submit(
                        audit_generated_artifact,
                        client,
                        artifacts[role],
                        problem_id=problem_id,
                        statement=statement,
                        contract=preparation.contract,
                        settings=model_settings,
                        deadline=deadline,
                        generator_blueprint=(
                            preparation.generator_blueprint
                            if role == "generator"
                            else None
                        ),
                        machine_gate_evidence=(
                            machine_gate_evidence
                            if machine_gate_codes is not None
                            and machine_gate_codes.get(role)
                            == artifacts[role].code
                            else None
                        ),
                        cancel_scope=cancel_scope,
                    )
                    futures[future] = (role, started)
                round_results: dict[str, Any] = {}
                round_errors: dict[str, dict[str, Any]] = {}
                primary_failure: dict[str, Any] | None = None
                for future in as_completed(futures):
                    role, started = futures[future]
                    try:
                        round_results[role] = future.result()
                    except Exception as exc:
                        is_sibling_cancel = (
                            str(getattr(exc, "code", "")) == "request_cancelled"
                            and primary_failure is not None
                        )
                        if cancel_scope is not None:
                            cancel_scope.cancel()
                        for sibling in futures:
                            if sibling is not future:
                                sibling.cancel()
                        failure = _role_failure_details(
                            exc,
                            role=role,
                            stage="audit_helpers",
                            substage="audit",
                            elapsed=(budget.elapsed() if budget is not None else 0.0),
                            attempts=counts.get(role, 0) + 1,
                        )
                        if primary_failure is None:
                            primary_failure = dict(failure)
                        if not is_sibling_cancel:
                            round_errors[role] = failure
                        _add_usage(usage, failure["usage"])
                    finally:
                        if budget is not None:
                            budget.record_span(
                                f"audit_{role}_attempt_{counts.get(role, 0) + 1}",
                                max(0.0, budget.clock() - started),
                            )
                # Successful siblings may have completed before a different
                # role failed.  Consume their provider usage before raising so
                # cancellation never erases already-billed tokens.
                for _audit, audit_usage in round_results.values():
                    _add_usage(usage, audit_usage)
                    if budget is not None:
                        budget.add_usage(dict(audit_usage))
                if round_errors:
                    raise StressRuntimeError(
                        "stress_artifact_stage_failed",
                        "helper 并行审查失败：" + "、".join(sorted(round_errors)),
                        details={
                            "roles": round_errors,
                            "primary_failure": primary_failure,
                        },
                        usage=usage,
                    )
            rejected: list[tuple[str, Any]] = []
            for role in generated_roles:
                artifact = artifacts[role]
                assert artifact is not None
                audit, audit_usage = round_results[role]
                audits[role] = audit.to_dict()
                if audit.accepted:
                    artifacts[role] = replace(
                        artifact,
                        static_audit=audit.to_dict(),
                    )
                else:
                    rejected.append((role, audit))
            if not rejected:
                continue
            repairs: dict[Any, tuple[str, Any, GeneratedArtifact, float]] = {}
            for role, audit in rejected:
                artifact = artifacts[role]
                assert artifact is not None
                repair_limit = 2 if role == "generator" else 1
                if counts.get(role, 0) >= repair_limit:
                    evidence = "；".join(
                        str(item.get("evidence") or "")
                        for item in audit.issues[:2]
                        if str(item.get("evidence") or "").strip()
                    )
                    audit_detail = "；".join(
                        part for part in (audit.summary, evidence) if part
                    )[:320]
                    raise StressRuntimeError(
                        "stress_artifact_audit_failed",
                        f"{role} 在 {repair_limit} 次针对性修复后仍未通过 AI 静态复核"
                        + (f"：{audit_detail}" if audit_detail else ""),
                        details={
                            "artifact": role,
                            "attempts": counts.get(role, 0),
                            "audit": audit.to_dict(),
                            "artifact_code": artifact.code,
                        },
                        usage=usage,
                    )
            def repair_role(
                role: str,
                audit: Any,
                artifact: GeneratedArtifact,
                diagnostic: str,
                attempt: int,
            ) -> tuple[GeneratedArtifact, dict[str, Any], dict[str, Any] | None]:
                local_usage: dict[str, Any] = {}
                try:
                    blueprint = preparation.generator_blueprint
                    reserve = 115.0 if role == "generator" and attempt == 1 else 75.0
                    if role != "generator":
                        reserve = 65.0
                    if role == "generator" and audit.fault_origin in {"blueprint", "both"}:
                        if blueprint_cache_key:
                            self._invalidate_generator_blueprint(blueprint_cache_key)
                        blueprint, blueprint_usage = generate_generator_blueprint(
                            client,
                            problem_id=problem_id,
                            statement=statement,
                            contract=preparation.contract,
                            settings=model_settings,
                            generation_mode=generation_mode,
                            diagnostic=diagnostic,
                            previous_blueprint=preparation.generator_blueprint,
                            provider_reserve_seconds=reserve,
                            budget=budget,
                            progress_callback=None,
                            cancel_scope=cancel_scope,
                        )
                        _add_usage(local_usage, blueprint_usage)
                        if blueprint_cache_key and blueprint_cache_identity:
                            self._save_generator_blueprint(
                                blueprint_cache_key,
                                blueprint_cache_identity,
                                blueprint,
                                replace_alias=True,
                            )
                    replacement, replacement_usage = generate_artifact(
                        client,
                        kind=role,
                        problem_id=problem_id,
                        statement=statement,
                        contract=preparation.contract,
                        settings=model_settings,
                        generator_blueprint=blueprint if role == "generator" else None,
                        generation_mode=generation_mode,
                        diagnostic=diagnostic,
                        previous_code=(
                            ""
                            if role == "generator" and attempt >= 2
                            else artifact.code
                        ),
                        repair_from_scratch=(
                            role == "generator" and attempt >= 2
                        ),
                        provider_reserve_seconds=reserve,
                        budget=budget,
                        progress_callback=None,
                        cancel_scope=cancel_scope,
                    )
                    _add_usage(local_usage, replacement_usage)
                    return replacement, local_usage, blueprint
                except Exception as exc:
                    details = getattr(exc, "details", None)
                    if isinstance(details, dict):
                        details.setdefault("prior_audit_diagnostic", diagnostic)
                        details.setdefault("repair_role", role)
                        details.setdefault("repair_attempt", attempt)
                    combined = dict(local_usage)
                    _add_usage(combined, dict(getattr(exc, "usage", {}) or {}))
                    try:
                        exc.usage = combined
                    except (AttributeError, TypeError):
                        pass
                    raise

            with ThreadPoolExecutor(
                max_workers=len(rejected), thread_name_prefix="stress-repair"
            ) as executor:
                for role, audit in rejected:
                    artifact = artifacts[role]
                    assert artifact is not None
                    counts[role] = counts.get(role, 0) + 1
                    if budget is not None:
                        budget.set_context(
                            generation_attempt=max(counts.values()),
                            attempts=dict(counts),
                            last_diagnostic=audit.summary[:500],
                        )
                    audit_payload = {
                        "stage": "static_audit",
                        "artifact": role,
                        "repair_attempt": counts[role],
                        "code": "stress_artifact_audit_rejected",
                        "verdict": audit.verdict,
                        "confidence": audit.confidence,
                        "issues": [dict(item) for item in audit.issues],
                        "witness": dict(audit.witness),
                        "summary": audit.summary,
                    }
                    diagnostic = json.dumps(
                        audit_payload,
                        ensure_ascii=False,
                    )[:4000]
                    future = executor.submit(
                        repair_role,
                        role,
                        audit,
                        artifact,
                        diagnostic,
                        counts[role],
                    )
                    started = budget.clock() if budget is not None else time.monotonic()
                    repairs[future] = (role, audit, artifact, started)
                repair_errors: dict[str, dict[str, Any]] = {}
                primary_failure: dict[str, Any] | None = None
                for future in as_completed(repairs):
                    role, _audit, artifact, started = repairs[future]
                    try:
                        replacement, repair_usage, repaired_blueprint = future.result()
                    except Exception as exc:
                        is_sibling_cancel = (
                            str(getattr(exc, "code", "")) == "request_cancelled"
                            and primary_failure is not None
                        )
                        if cancel_scope is not None:
                            cancel_scope.cancel()
                        for sibling in repairs:
                            if sibling is not future:
                                sibling.cancel()
                        failure = _role_failure_details(
                            exc,
                            role=role,
                            stage="repair_helpers",
                            substage="repair",
                            elapsed=(budget.elapsed() if budget is not None else 0.0),
                            attempts=counts[role],
                        )
                        if primary_failure is None:
                            primary_failure = dict(failure)
                        if not is_sibling_cancel:
                            repair_errors[role] = failure
                            _add_usage(usage, failure["usage"])
                        continue
                    if role in REFERENCE_ROLES:
                        replacement = replace(
                            replacement,
                            source_url=artifact.source_url,
                            source_title=artifact.source_title,
                            source_sha256=artifact.source_sha256,
                            license=artifact.license,
                        )
                    artifacts[role] = replacement
                    if role == "generator" and repaired_blueprint is not None:
                        preparation = replace(
                            preparation,
                            generator_blueprint=dict(repaired_blueprint),
                        )
                    _add_usage(usage, repair_usage)
                    if budget is not None:
                        budget.record_span(
                            f"repair_{role}_attempt_{counts[role]}",
                            max(0.0, budget.clock() - started),
                        )
                if repair_errors:
                    raise StressRuntimeError(
                        "stress_artifact_stage_failed",
                        "helper 定点修复失败：" + "、".join(sorted(repair_errors)),
                        details={
                            "roles": repair_errors,
                            "primary_failure": primary_failure,
                        },
                        usage=usage,
                    )
        if budget is not None:
            budget.set_context(
                fast_fallback_used=bool(
                    usage.get("fast_fallback_used", False)
                ),
                generation_attempt=max(counts.values(), default=0),
                attempts=dict(counts),
            )
        for role, count in counts.items():
            if count > 0:
                usage[f"{role}_repairs_used"] = max(
                    int(usage.get(f"{role}_repairs_used") or 0),
                    int(count),
                )
        return (
            StressPreparation(
                dict(preparation.contract),
                artifacts["generator"],
                artifacts["reference_primary"],
                artifacts["reference_secondary"],
                usage,
                preparation.generator_blueprint,
                dict(preparation.generation_metadata),
                artifacts["validator"],
            ),
            audits,
            counts,
        )

    def _resolve_manual_helper(self, path: Path | str, *, role: str) -> Path:
        """Validate a local C++ helper without restricting it to the workspace."""

        raw_path = os.fspath(path)
        portable = str(raw_path).replace("\\", "/")
        if portable.startswith("//"):
            raise ValueError(f"{role} 手动文件必须是本机路径，不能使用 UNC/设备路径")
        requested = Path(raw_path)
        if not requested.is_absolute():
            requested = self.paths.root / requested
        if requested.suffix.lower() != ".cpp" or not requested.is_file():
            raise ValueError(f"{role} 手动文件必须是存在的 .cpp 文件")
        if requested.is_symlink():
            raise ValueError(f"{role} 手动文件不能是符号链接")
        resolved = requested.resolve(strict=True)
        if str(resolved).replace("\\", "/").startswith("//"):
            raise ValueError(f"{role} 手动文件必须是本机路径，不能使用 UNC/设备路径")
        return resolved

    def start(
        self,
        *,
        client: Any,
        platform: str,
        problem_id: str,
        title: str,
        statement: str,
        primary_source: Path,
        attempt_id: int | None,
        ai_run_id: str | None = None,
        model_settings: Mapping[str, Any],
        compare: str = "token",
        seed: int | None = None,
        include_generator: bool = True,
        include_reference_primary: bool = True,
        include_reference_secondary: bool = True,
        include_brute: bool | None = None,
        include_reference: bool | None = None,
        # Library-level default: a complete certification includes the validator,
        # so direct callers of this coordinator get the strongest bundle unless
        # they opt out.  This deliberately differs from the *policy* default in
        # AgentService.ai_stress_start, which is off to avoid spending provider
        # budget on a role the escalation path no longer depends on; that entry
        # point always passes this argument explicitly, so the two never race.
        # An intentionally absent validator is not a degradation: `degraded`
        # additionally requires a non-None degraded_reason, so a bundle prepared
        # without a validator stays cacheable.
        include_validator: bool = True,
        include_large: bool = True,
        allow_validator_degradation: bool = False,
        unvalidated_large: bool = False,
        minimal_verification: bool = False,
        preparation_timeout_seconds: int = DEFAULT_PREPARATION_TIMEOUT_SECONDS,
        force_regenerate: bool = False,
        cache_mode: str = "reuse",
        generation_mode: str = "hybrid",
        preparation_budget: PreparationBudget | None = None,
        timeout: float = 2.0,
        reference_secondary_timeout: float | None = None,
        brute_timeout: float | None = None,
        run_max_cases: int | None = None,
        progress_callback: StressProgress | None = None,
        _pre_apply_gate: Callable[[StagedHelperBundle], Mapping[str, Any] | None] | None = None,
        reference_primary_file: Path | str | None = None,
        reference_secondary_file: Path | str | None = None,
        brute_file: Path | str | None = None,
        reference_file: Path | str | None = None,
        generator_file: Path | str | None = None,
    ) -> dict[str, Any]:
        if include_brute is not None:
            include_reference_secondary = bool(include_brute)
        if include_reference is not None:
            include_reference_primary = bool(include_reference)
        if reference_primary_file is not None and reference_file is not None:
            raise ValueError("reference_primary_file 与 reference_file 不能同时提供")
        if reference_secondary_file is not None and brute_file is not None:
            raise ValueError("reference_secondary_file 与 brute_file 不能同时提供")
        reference_primary_file = reference_primary_file or reference_file
        reference_secondary_file = reference_secondary_file or brute_file
        if reference_secondary_timeout is None and brute_timeout is not None:
            reference_secondary_timeout = brute_timeout
        if _pre_apply_gate is not None:
            gate_module = str(getattr(_pre_apply_gate, "__module__", "") or "")
            if gate_module != _BENCHMARK_PRE_APPLY_GATE_MODULE:
                raise ValueError(
                    "pre_apply_gate is only available to the local stress benchmark"
                )
        budget = preparation_budget or PreparationBudget(
            int(preparation_timeout_seconds)
        )
        cancel_scope = DeepSeekCancelScope()
        selected_cache_mode = str(cache_mode).strip().casefold()
        if selected_cache_mode not in {"reuse", "refresh_helpers", "cold"}:
            raise ValueError("cache_mode must be reuse, refresh_helpers, or cold")
        if force_regenerate:
            if selected_cache_mode not in {"reuse", "cold"}:
                raise ValueError("force_regenerate conflicts with cache_mode")
            selected_cache_mode = "cold"
        force_regenerate = selected_cache_mode == "cold"
        if generation_mode not in {"fast", "hybrid", "full_thinking"}:
            raise ValueError("generation_mode must be fast, hybrid, or full_thinking")
        budget.set_context(
            generation_mode=generation_mode,
            fast_fallback_used=False,
            generation_attempt=0,
            cache_mode=selected_cache_mode,
            validation_reserve_seconds=round(budget.scaled_reserve(65.0), 3),
        )
        raw_progress = progress_callback

        def budgeted_progress(stage: str, label: str, step: int, total: int) -> None:
            budget.mark_stage(stage)
            if raw_progress is None:
                return
            metadata = budget.progress(
                stage,
                soft_stage={
                    "check_isolation": "isolation_context",
                    "extract_contract": "contract",
                    "generate_generator": "prepare_helpers",
                    "generate_reference_primary": "prepare_helpers",
                    "generate_reference_secondary": "prepare_helpers",
                    "prepare_reference": "prepare_helpers",
                    "audit_helpers": "audit_helpers",
                    "preflight_helpers": "local_validation",
                    "apply_helpers": "local_validation",
                }.get(stage, stage),
            )
            try:
                raw_progress(stage, label, step, total, metadata)  # type: ignore[misc]
            except TypeError:
                raw_progress(stage, label, step, total)

        progress_callback = budgeted_progress
        budget.require("check_isolation")
        if progress_callback is not None:
            progress_callback("check_isolation", "检查隔离环境", 1, 10)
        sandbox = self._sandbox_factory()
        capability = sandbox.probe()
        if not capability.available:
            raise SandboxUnavailableError(capability.reason)
        with Database(self.paths.database) as db:
            active = db.active_stress_run()
            if active is not None:
                raise StressRuntimeError(
                    "stress_run_active", f"已有持续对拍正在运行：{active['id']}"
                )
        if not statement.strip():
            raise StressRuntimeError("missing_problem_context", "AI 持续对拍需要题面上下文")
        primary = primary_source.resolve()
        existing = {
            "generator": primary.with_name(f"{problem_id}.gen.cpp"),
            "reference_primary": primary.with_name(f"{problem_id}.ref1.cpp"),
            "reference_secondary": primary.with_name(f"{problem_id}.ref2.cpp"),
        }
        manual_files: dict[str, Path] = {}
        for role in ("generator", *REFERENCE_ROLES):
            manual = {
                "generator": generator_file,
                "reference_primary": reference_primary_file,
                "reference_secondary": reference_secondary_file,
            }[role]
            if manual is not None:
                manual_files[role] = self._resolve_manual_helper(manual, role=role)
        manual_override = bool(manual_files)
        contract_cache_key, contract_cache_identity = self._contract_cache_identity(
            problem_id=problem_id,
            statement=statement,
            compare=compare,
            model_settings=model_settings,
            generation_mode=generation_mode,
        )
        prepared_contract: dict[str, Any] | None = None
        contract_usage: dict[str, Any] = {}
        if not force_regenerate:
            with Database(self.paths.database) as db:
                contract_row = db.stress_preparation_cache(contract_cache_key)
            if contract_row is not None:
                payload = _json_field(contract_row, "payload_json")
                metadata = _json_field(contract_row, "metadata_json")
                candidate = payload.get("contract") if isinstance(payload, Mapping) else None
                if (
                    isinstance(candidate, Mapping)
                    and isinstance(metadata, Mapping)
                    and metadata.get("cache_identity") == contract_cache_identity
                    and str(candidate.get("output_compare") or "") == compare
                ):
                    prepared_contract = dict(candidate)
        if prepared_contract is None:
            if progress_callback is not None:
                progress_callback("extract_contract", "让 DeepSeek 提取对拍契约", 2, 10)
            prepared_contract, contract_usage = extract_contract(
                client,
                problem_id=problem_id,
                statement=statement,
                compare=compare,
                settings=model_settings,
                generation_mode=generation_mode,
                provider_reserve_seconds=80.0,
                budget=budget,
                progress_callback=progress_callback,
                cancel_scope=cancel_scope,
                require_complete_probes=not minimal_verification,
            )
            with Database(self.paths.database) as db:
                if db.stress_preparation_cache(contract_cache_key) is None:
                    db.save_stress_preparation_cache(
                        contract_cache_key,
                        payload={"contract": prepared_contract},
                        metadata={
                            "cache_identity": contract_cache_identity,
                            "status": "validated",
                        },
                    )
            contract_cache_result = "miss"
        else:
            if progress_callback is not None:
                progress_callback("extract_contract", "命中本地 contract 缓存", 2, 10)
            contract_cache_result = "hit"
        prepared_blueprint: dict[str, Any] | None = None
        blueprint_cache_result = "not_requested"
        blueprint_cache_key: str | None = None
        blueprint_cache_identity: dict[str, Any] | None = None
        if include_generator and generator_file is None:
            try:
                prepared_blueprint = compile_static_contract_v2(prepared_contract)
                blueprint_cache_result = "deterministic_contract"
            except UnsupportedRecipeError:
                pass
        if include_generator and generator_file is None:
            blueprint_cache_key, blueprint_cache_identity = (
                self._blueprint_cache_identity(
                    problem_id=problem_id,
                    statement=statement,
                    contract=prepared_contract,
                    model_settings=model_settings,
                    generation_mode=generation_mode,
                )
            )
            if prepared_blueprint is None and not force_regenerate:
                cached_blueprint = self._cached_generator_blueprint(
                    blueprint_cache_key,
                    blueprint_cache_identity,
                )
                if cached_blueprint is not None:
                    prepared_blueprint, _selected_blueprint_key = cached_blueprint
                    blueprint_cache_result = "hit"
            if prepared_blueprint is None:
                blueprint_cache_result = "forced" if force_regenerate else "miss"

        cache_key, cache_identity = self._preparation_cache_identity(
            platform=platform,
            problem_id=problem_id,
            statement=statement,
            compare=compare,
            include_generator=include_generator,
            include_validator=bool(include_validator and not minimal_verification),
            include_reference_primary=include_reference_primary,
            include_reference_secondary=include_reference_secondary,
            include_large=include_large,
            model_settings=model_settings,
            sandbox=sandbox,
            generation_mode=generation_mode,
            contract=prepared_contract,
            generator_blueprint=prepared_blueprint,
        )
        cached = (
            None
            if selected_cache_mode != "reuse"
            or manual_override
            or (include_generator and prepared_blueprint is None)
            else self._cached_preparation(
                cache_key,
                cache_identity,
                platform=platform,
                problem_id=problem_id,
            )
        )
        if cached is not None:
            bundle, preparation, preflight_validation, audit_reports = cached
            budget.require("compile_user_solution")
            executables = self._compile_run_executables(
                bundle, primary, budget=budget, reuse_helpers=True
            )
            run_id = str(uuid4())
            first_seed = (
                int(seed)
                if seed is not None
                else random.SystemRandom().randrange(1, 2**63)
            )
            config = StressRunConfig(
                first_seed=first_seed,
                profile_version=2,
                schedule_offset=0,
                include_large=bool(include_large),
                small_per_cycle=4,
                large_per_cycle=1 if include_large else 0,
                solution_timeout=float(timeout),
                generator_timeout=float(timeout),
                reference_primary_timeout=float(timeout),
                reference_secondary_timeout=float(
                    reference_secondary_timeout or timeout
                ),
                validator_timeout=float(timeout),
                oracle_protocol=DUAL_REFERENCE_PROTOCOL,
                max_cases=run_max_cases,
                exact_output=compare == "exact",
            )
            budget.require("create_stress_run")
            self._persist_cached_run(
                bundle,
                platform=platform,
                problem_id=problem_id,
                attempt_id=attempt_id,
                primary=primary,
                run_id=run_id,
                config=config,
                validator_requested=bool(
                    include_validator and not minimal_verification
                ),
                validator_certified=(
                    preparation.validator is not None
                    and _validator_preflight_succeeded(preflight_validation)
                ),
            )
            self._launch(
                run_id, bundle, executables, config, preparation, platform=platform
            )
            return {
                "ok": True,
                "run": self.run(run_id),
                "bundle": self.bundle(bundle.bundle_id),
                "usage": {},
                "preparation": {
                    **budget.snapshot(),
                    "cache_result": "bundle_hit",
                    "cache_mode": selected_cache_mode,
                    "contract_cache_result": contract_cache_result,
                    "blueprint_cache_result": blueprint_cache_result,
                    "cache_key": cache_key,
                    "provider_requests": 0,
                    "source_fetches": 0,
                    "helper_compiles": 0,
                    "preflight_runs": 0,
                },
            }

        checkpoint_artifacts = (
            self._load_checkpoint_artifacts(
                platform=platform,
                problem_id=problem_id,
                statement=statement,
                contract=prepared_contract,
                generator_blueprint=prepared_blueprint,
                model_settings=model_settings,
                generation_mode=generation_mode,
            )
            if (
                selected_cache_mode == "reuse"
                and prepared_blueprint is not None
                and not manual_override
            )
            else {}
        )
        crawler = self._crawler_factory()
        if hasattr(crawler, "timeout"):
            crawler.timeout = min(15.0, max(1.0, 6.0 * budget.scale))
        if hasattr(crawler, "deadline"):
            crawler.deadline = budget.work_deadline
        if hasattr(crawler, "cancel_scope"):
            crawler.cancel_scope = cancel_scope

        def checkpoint_completed_role(
            role: str,
            artifact: GeneratedArtifact,
            role_usage: Mapping[str, Any],
            role_blueprint: Mapping[str, Any] | None,
        ) -> None:
            completed_artifacts[role] = artifact
            role_artifacts: dict[str, GeneratedArtifact | None] = {
                "generator": None,
                "validator": None,
                "reference_primary": None,
                "reference_secondary": None,
            }
            role_artifacts[role] = artifact
            partial = StressPreparation(
                dict(prepared_contract),
                role_artifacts["generator"],
                role_artifacts["reference_primary"],
                role_artifacts["reference_secondary"],
                dict(role_usage),
                (
                    dict(role_blueprint)
                    if role == "generator" and role_blueprint is not None
                    else prepared_blueprint
                ),
                {"generation_mode": generation_mode},
                role_artifacts["validator"],
            )
            self._checkpoint_candidates(
                partial,
                platform=platform,
                problem_id=problem_id,
                statement=statement,
                model_settings=model_settings,
                generation_mode=generation_mode,
                ai_run_id=ai_run_id,
            )
        completed_artifacts: dict[str, GeneratedArtifact] = {}
        degraded_reason: str | None = "minimal_verification" if minimal_verification else None
        effective_include_validator = include_validator and not minimal_verification

        def prepare_with_validator(enable_validator: bool) -> StressPreparation:
            return prepare_stress(
                client,
                crawler,
                platform=platform,
                problem_id=problem_id,
                title=title,
                statement=statement,
                compare=compare,
                settings=model_settings,
                generation_mode=generation_mode,
                include_generator=include_generator and generator_file is None,
                include_validator=enable_validator,
                include_reference_primary=(
                    include_reference_primary and reference_primary_file is None
                ),
                include_reference_secondary=(
                    include_reference_secondary and reference_secondary_file is None
                ),
                # Minimal mode deliberately skips independent artifact audits.
                # Do not treat unaudited web snippets as trusted references in
                # that mode; generate isolated references under the same local
                # source-safety and preflight gates instead.
                allow_external_references=not minimal_verification,
                require_complete_probes=not minimal_verification,
                budget=budget,
                prepared_contract=prepared_contract,
                prepared_generator_blueprint=prepared_blueprint,
                prepared_artifacts={**checkpoint_artifacts, **completed_artifacts},
                provider_reserve_seconds=80.0,
                initial_usage=contract_usage,
                progress_callback=progress_callback,
                artifact_callback=checkpoint_completed_role,
                blueprint_repair_limit=2,
                cancel_scope=cancel_scope,
            )

        try:
            preparation = prepare_with_validator(effective_include_validator)
        except Exception as exc:
            primary_failure = _primary_preparation_failure(exc)
            if (
                effective_include_validator
                and allow_validator_degradation
                and primary_failure is not None
                and str(primary_failure.get("role") or "") == "validator"
            ):
                degraded_reason = "validator_generation_failed"
                merged_usage = dict(contract_usage)
                _add_usage(merged_usage, dict(getattr(exc, "usage", {}) or {}))
                contract_usage = merged_usage
                # prepare_stress cancels the logical operation tree on a
                # sibling failure.  A degradation retry is a new operation
                # tree and must never inherit that sticky cancellation bit.
                cancel_scope = DeepSeekCancelScope()
                if hasattr(crawler, "cancel_scope"):
                    crawler.cancel_scope = cancel_scope
                preparation = prepare_with_validator(False)
            else:
                if primary_failure is not None and progress_callback is not None:
                    stage, label = _preparation_failure_label(primary_failure)
                    # Pin the terminal progress after all worker callbacks so a
                    # slower reference audit cannot masquerade as the root cause.
                    progress_callback(stage, label, 3, 10)
                raise
        budget.set_context(
            fast_fallback_used=bool(
                preparation.usage.get("fast_fallback_used", False)
            )
        )
        if include_generator and generator_file is None:
            if preparation.generator_blueprint is None:
                raise StressRuntimeError(
                    "stress_blueprint_invalid",
                    "generator 准备完成但缺少已验证 blueprint",
                )
            prepared_blueprint = _validate_generator_plan(
                preparation.generator_blueprint,
                contract=preparation.contract,
            )
            assert blueprint_cache_key is not None
            assert blueprint_cache_identity is not None
            if blueprint_cache_result != "hit":
                self._save_generator_blueprint(
                    blueprint_cache_key,
                    blueprint_cache_identity,
                    prepared_blueprint,
                    replace_alias=force_regenerate,
                )
            cache_key, cache_identity = self._preparation_cache_identity(
                platform=platform,
                problem_id=problem_id,
                statement=statement,
                compare=compare,
                include_generator=include_generator,
                include_validator=preparation.validator is not None,
                include_reference_primary=include_reference_primary,
                include_reference_secondary=include_reference_secondary,
                include_large=include_large,
                model_settings=model_settings,
                sandbox=sandbox,
                generation_mode=generation_mode,
                contract=prepared_contract,
                generator_blueprint=prepared_blueprint,
            )
        artifacts: dict[str, GeneratedArtifact | None] = {
            "generator": preparation.generator,
            "validator": preparation.validator,
            "reference_primary": preparation.reference_primary,
            "reference_secondary": preparation.reference_secondary,
        }
        enabled_roles = {
            "generator": bool(include_generator) and generator_file is None,
            "validator": bool(include_validator),
            "reference_primary": (
                bool(include_reference_primary) and reference_primary_file is None
            ),
            "reference_secondary": (
                bool(include_reference_secondary) and reference_secondary_file is None
            ),
        }
        for role, enabled in enabled_roles.items():
            if role == "validator":
                continue
            if enabled:
                continue
            if role in manual_files:
                path = manual_files[role]
            else:
                path = existing[role]
                if not path.is_file():
                    raise StressRuntimeError(
                        "missing_stress_helper", f"未生成 {role} 且本地文件不存在"
                    )
            artifacts[role] = GeneratedArtifact(
                role,
                path.read_text(encoding="utf-8"),
                "user_specified" if role in manual_files else "local_existing",
                "用户手动指定 helper" if role in manual_files else "使用已有 helper",
            )
        assert artifacts["generator"] is not None
        assert artifacts["reference_primary"] is not None
        assert artifacts["reference_secondary"] is not None
        preparation = StressPreparation(
            dict(preparation.contract),
            artifacts["generator"],
            artifacts["reference_primary"],
            artifacts["reference_secondary"],
            dict(preparation.usage),
            prepared_blueprint,
            dict(preparation.generation_metadata),
            artifacts["validator"],
        )
        candidate_refs = self._checkpoint_candidates(
            preparation,
            platform=platform,
            problem_id=problem_id,
            statement=statement,
            model_settings=model_settings,
            generation_mode=generation_mode,
            ai_run_id=ai_run_id,
        )
        repair_counts = _initial_repair_counts(preparation)
        blueprint_repairs_used = repair_counts["generator"]
        budget.set_context(
            generation_attempt=blueprint_repairs_used,
            attempts=dict(repair_counts),
        )
        manager = HelperBundleManager(
            self.paths.root,
            sandbox,
            sandbox_factory=self._sandbox_factory,
        )
        contract_bytes = json.dumps(
            preparation.contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        qualification_config = HelperPreflightConfig(
            contract_hash=sha256_bytes(contract_bytes),
            generator_blueprint=preparation.generator_blueprint,
            validator_probes=(
                tuple(preparation.contract.get("validator_probes", []))
                if preparation.validator is not None
                else ()
            ),
            include_large=bool(include_large),
            exact_output=compare == "exact",
            small_random_cases=16,
            generator_timeout=float(timeout),
            reference_primary_timeout=float(timeout),
            reference_secondary_timeout=float(
                reference_secondary_timeout or timeout
            ),
            validator_timeout=float(timeout),
            deadline=budget.work_deadline,
            clock=budget.clock,
        )
        qualified_staged = None
        qualified_codes: dict[str, str] = {}
        qualification_result: dict[str, Any] = {}
        last_machine_diagnostics: dict[str, dict[str, Any]] = {}
        machine_diagnostic_history: dict[str, list[dict[str, Any]]] = {}
        local_gate_confirmations: set[
            tuple[str, str, str, str, str, int]
        ] = set()
        stored_problem_id = (
            problem_id[2:]
            if platform == "codeforces" and problem_id.upper().startswith("CF")
            else problem_id
        )
        proof_identity = {
            "compiler_fingerprint": _compiler_fingerprint(),
            "sandbox": (
                f"{sandbox.__class__.__module__}."
                f"{sandbox.__class__.__qualname__}"
            ),
            "safety_policy_version": STRESS_SAFETY_POLICY_VERSION,
            "protocol": {"profile": 2, "preflight": STRESS_PREFLIGHT_VERSION},
        }
        while qualified_staged is None and not minimal_verification:
            machine_artifacts = {
                "generator": preparation.generator,
                "validator": preparation.validator,
                "reference_primary": preparation.reference_primary,
                "reference_secondary": preparation.reference_secondary,
            }
            assert machine_artifacts["generator"] is not None
            assert machine_artifacts["reference_primary"] is not None
            assert machine_artifacts["reference_secondary"] is not None
            machine_sources = HelperSources(
                machine_artifacts["generator"].code,
                machine_artifacts["reference_primary"].code,
                machine_artifacts["reference_secondary"].code,
                (
                    machine_artifacts["validator"].code
                    if machine_artifacts["validator"] is not None
                    else None
                ),
            )
            try:
                budget.require("prepare_helpers")
                qualified_staged = manager.stage(
                    primary,
                    machine_sources,
                    compile_timeout=max(
                        0.1,
                        min(
                            15.0,
                            budget.remaining_for_stage("prepare_helpers"),
                        ),
                    ),
                )
                qualification_result = _run_locally_confirmed_gate(
                    lambda: manager.qualify(
                        qualified_staged, qualification_config
                    ),
                    gate_name="pre_audit_machine_gate",
                    source_identity=_bundle_source_identity(machine_artifacts),
                    confirmations=local_gate_confirmations,
                )
            except HelperPreflightError as exc:
                if qualified_staged is not None:
                    manager.discard(qualified_staged)
                    qualified_staged = None
                failed_role = str(exc.artifact)
                if failed_role == "oracle":
                    primary_failure = _role_failure_details(
                        exc,
                        role="oracle",
                        stage="prepare_helpers",
                        substage="pre_audit_machine_gate",
                        elapsed=budget.elapsed(),
                        attempts=0,
                    )
                    raise StressRuntimeError(
                        str(exc.code),
                        "两份 reference 在写入前输出不一致；不自动猜测或修复任一方",
                        details={
                            **exc.details,
                            "artifact": "oracle",
                            "primary_failure": primary_failure,
                        },
                        usage=preparation.usage,
                    ) from exc
                if failed_role in REFERENCE_ROLES:
                    current_reference = getattr(preparation, failed_role)
                    alternate = _next_external_reference(
                        current_reference, role=failed_role
                    )
                    if alternate is not None:
                        preparation = _use_reference_alternate(
                            preparation, alternate, role=failed_role
                        )
                        candidate_refs = self._checkpoint_candidates(
                            preparation,
                            platform=platform,
                            problem_id=problem_id,
                            statement=statement,
                            model_settings=model_settings,
                            generation_mode=generation_mode,
                            ai_run_id=ai_run_id,
                        )
                        continue
                if (
                    failed_role == "generator"
                    and isinstance(preparation.generator_blueprint, Mapping)
                    and preparation.generator_blueprint.get("engine")
                    == GENERATOR_RECIPE_V2_ENGINE
                ):
                    raise StressRuntimeError(
                        "stress_local_recipe_v2_preflight_failed",
                        "确定性 generator_recipe/v2 未通过本地预验；禁止回退或调用 AI generator",
                        details={
                            **exc.details,
                            "root_cause_code": str(exc.code),
                            "generator_engine": GENERATOR_RECIPE_V2_ENGINE,
                        },
                        usage=preparation.usage,
                    ) from exc
                repair_limit = _preflight_role_repair_limit(failed_role, exc)
                if repair_counts.get(failed_role, 0) >= repair_limit:
                    if (
                        failed_role == "validator"
                        and allow_validator_degradation
                        and qualified_staged is not None
                    ):
                        manager.discard(qualified_staged)
                        qualified_staged = None
                    if (
                        failed_role == "validator"
                        and allow_validator_degradation
                        and degraded_reason is None
                    ):
                        # The validator cannot be certified against the hidden
                        # probes.  Drop the role and continue without input
                        # certification: small cases stay protected by the
                        # triple oracle and large cases are disabled unless the
                        # user explicitly opted into unvalidated large runs.
                        degraded_reason = str(exc.code)
                        preparation = StressPreparation(
                            dict(preparation.contract),
                            machine_artifacts["generator"],
                            machine_artifacts["reference_primary"],
                            machine_artifacts["reference_secondary"],
                            preparation.usage,
                            preparation.generator_blueprint,
                            dict(preparation.generation_metadata),
                            None,
                        )
                        break
                    primary_failure = _role_failure_details(
                        exc,
                        role=failed_role,
                        stage="prepare_helpers",
                        substage="pre_audit_machine_gate",
                        elapsed=budget.elapsed(),
                        attempts=repair_counts.get(failed_role, 0),
                    )
                    raise StressRuntimeError(
                        str(exc.code),
                        f"{failed_role} 未通过 audit 前机器门禁，修复额度已用尽",
                        details={
                            **exc.details,
                            "primary_failure": primary_failure,
                            "attempts": dict(repair_counts),
                            "last_machine_diagnostic": last_machine_diagnostics.get(
                                failed_role, {}
                            ),
                            "machine_diagnostic_history": machine_diagnostic_history.get(
                                failed_role, []
                            ),
                        },
                        usage=preparation.usage,
                    ) from exc
                repair_counts[failed_role] = repair_counts.get(failed_role, 0) + 1
                prior = machine_artifacts[failed_role]
                assert prior is not None
                diagnostic_payload = {
                    "stage": "pre_audit_machine_gate",
                    "artifact": failed_role,
                    "repair_attempt": repair_counts[failed_role],
                    "profile": exc.profile,
                    "case_kind": exc.case_kind,
                    "seed": exc.seed,
                    "code": str(exc.code),
                    "message": str(exc),
                    "stderr": str(exc.details.get("stderr") or "")[-2000:],
                    "expected": exc.details.get("expected"),
                    "actual": exc.details.get("actual"),
                }
                if failed_role == "validator" and exc.profile == "contract_probe":
                    diagnostic_payload = _contract_probe_repair_diagnostic(
                        exc,
                        repair_attempt=repair_counts[failed_role],
                    )
                repair_witness = _preflight_repair_witness(exc)
                if repair_witness is not None:
                    diagnostic_payload["witness"] = repair_witness
                prior_diagnostics = list(
                    machine_diagnostic_history.get(failed_role, [])[-2:]
                )
                if prior_diagnostics:
                    diagnostic_payload["previous_machine_diagnostics"] = (
                        prior_diagnostics
                    )
                if failed_role == "generator":
                    diagnostic_payload["invariants_to_preserve"] = (
                        _generator_repair_invariants(
                            preparation.generator_blueprint
                        )
                    )
                if (
                    failed_role == "validator"
                    and "ERR_TRAILING"
                    in str(diagnostic_payload.get("stderr") or "")
                    and re.search(r"\.peek\s*\(\s*\)\s*!=\s*EOF", prior.code)
                ):
                    diagnostic_payload["required_change"] = (
                        "旧 validator 用 cin.peek()!=EOF，把合法输入结尾的换行误判为 trailing token。"
                        "先执行 cin >> ws，再用 cin.eof() 判断；同时检查成功路径确实输出"
                        "valid=true 的四键 JSON，不得只 return 0。"
                    )
                if (
                    failed_role == "validator"
                    and "singular mutable iterator"
                    in str(diagnostic_payload.get("stderr") or "")
                    and "splice" in prior.code
                ):
                    diagnostic_payload["required_change"] = (
                        "_GLIBCXX_DEBUG 已证明某个 ID 映射持有 singular iterator。"
                        "std::list::splice 保持被移动元素的原 iterator 有效；splice 后保留原映射，"
                        "不要把 it[id] 赋成 target、next 或 prev(target)，它们指向目标邻居而非"
                        "被移动元素。逐个检查 splice 分支，并删除这种错误重绑定。"
                    )
                if (
                    failed_role == "validator"
                    and str(exc.code)
                    in {
                        "stress_validator_positive_probe_failed",
                        "stress_validator_negative_probe_failed",
                    }
                ):
                    diagnostic_payload["required_change"] = (
                        "独立认证后的隐藏 paired probe 证明 validator 对绑定的动态 constraint "
                        "发生过度拒绝或漏检。不得请求、猜测或硬编码 probe；只根据 contract 中"
                        "expected.constraint_id 对应的 evidence/args 核对完整前置条件、边界方向和"
                        "每次状态修改后的映射。禁止删除状态机、跳过 constraint 或无条件返回。"
                    )
                if (
                    failed_role == "generator"
                    and str(exc.code) == "stress_generated_input_invalid"
                ):
                    required_change = (
                        "独立 validator 已给出一个确定失败 seed。逐条重放最终输出顺序；"
                        "每个有状态前置条件的操作都必须在追加到输出前按当前状态验证并随后"
                        "更新状态。禁止在验证后 shuffle/reorder 操作列表；若重排，必须从初态"
                        "按重排后的最终顺序重新验证并替换非法操作。blueprint construction 是"
                        "候选策略；机器门禁证明具体记录非法时允许替换参数或顺序，但必须保持"
                        "dimensions、声明记录数、operation_families 和 coverage_tags。"
                    )
                    count_hint = _generator_record_count_hint(
                        preparation.contract, exc.details
                    )
                    if count_hint is not None:
                        diagnostic_payload["record_count_hint"] = count_hint
                        required_change += (
                            " 本次完整输入中 contract 字段 "
                            + str(count_hint["count_from"])
                            + " 声明 "
                            + str(count_hint["declared_records"])
                            + " 条记录，但按已知 operation tag 实际发现 "
                            + str(count_hint["observed_tagged_records"])
                            + " 条。用单一 ops 容器作为事实源，完成构造后输出 ops.size()，"
                            "删除额外记录或补齐缺失记录，并在返回前重新计数。"
                        )
                    diagnostic_payload["required_change"] = required_change
                if (
                    failed_role == "generator"
                    and str(exc.code)
                    == "stress_generator_seed_variation_failed"
                ):
                    safe_seed_families = _generator_safe_seed_families(
                        preparation.generator_blueprint
                    )
                    safe_seed_target = (
                        "blueprint 的 large/random 安全集合中的一个实际输出参数（"
                        + ", ".join(safe_seed_families[:12])
                        + "）"
                        if safe_seed_families
                        else "blueprint 中一个契约明确独立合法的实际输出标量字段"
                    )
                    diagnostic_payload["required_change"] = (
                        "失败门禁明确位于 profile="
                        + str(exc.profile)
                        + "/case_kind="
                        + str(exc.case_kind)
                        + "；必须修改该分支实际执行的源码路径。只修改其他 profile/case_kind "
                        "分支不算修复。"
                        "只做最小源码修改：保留 construction 的初始状态、所有状态修改操作、"
                        "操作顺序、声明记录数和实际记录数；仅让"
                        + safe_seed_target
                        + "由 seed 初始化的 PRNG 派生。不得随机化任何有状态操作的标识、参数"
                        "或顺序。修复后 failing random"
                        "分支必须消费 PRNG，并在连续 seed 窗口产生至少两种合法 stdout。"
                    )
                if (
                    failed_role == "generator"
                    and (
                        "runtime" in str(exc).casefold()
                        or "out-of-bounds"
                        in str(exc.details.get("stderr") or "").casefold()
                    )
                ):
                    diagnostic_payload["required_change"] = (
                        "不要局部修补崩溃下标；完整替换 small/random 的状态构造。small 仅使用"
                        "值语义容器并在每步重新查找，禁止 prev/next 链、缓存下标、iterator 或"
                        "pointer；只在确认合法后追加操作。large/random 不维护动态序列，只输出"
                        "契约中无条件合法或只读的操作。"
                    )
                if failed_role in REFERENCE_ROLES and (
                    "timeout" in str(exc).casefold()
                    or "runtime" in str(exc).casefold()
                ):
                    diagnostic_payload["required_change"] = (
                        "这是最小 small 输入上的终止性失败。放弃旧架构并完整重写，不做局部"
                        "SEARCH/REPLACE；所有查找循环必须在空节点/越界前终止，并重新核对哨兵、"
                        "第 k 个元素和题面一基/零基索引。"
                    )
                if failed_role == "generator":
                    persistent_seed_requirement = (
                        _persistent_generator_seed_requirement(
                            prior_diagnostics,
                            preparation.generator_blueprint,
                        )
                    )
                    if persistent_seed_requirement:
                        diagnostic_payload["still_required_changes"] = (
                            persistent_seed_requirement
                        )
                diagnostic = json.dumps(
                    diagnostic_payload,
                    ensure_ascii=False,
                )[:4000]
                if failed_role == "validator" and exc.profile == "contract_probe":
                    last_machine_diagnostics[failed_role] = {
                        key: value
                        for key, value in diagnostic_payload.items()
                        if key not in {"required_change", "previous_machine_diagnostics"}
                    }
                else:
                    last_machine_diagnostics[failed_role] = {
                        "repair_attempt": repair_counts[failed_role],
                        "code": str(exc.code),
                        "message": str(exc)[:500],
                        "profile": exc.profile,
                        "case_kind": exc.case_kind,
                        "seed": exc.seed,
                        "stderr": str(exc.details.get("stderr") or "")[-2000:],
                        "expected": exc.details.get("expected"),
                        "actual": exc.details.get("actual"),
                    }
                machine_diagnostic_history.setdefault(failed_role, []).append(
                    dict(last_machine_diagnostics[failed_role])
                )
                machine_diagnostic_history[failed_role] = (
                    machine_diagnostic_history[failed_role][-3:]
                )
                repaired_generator_plan: dict[str, Any] | None = None
                rewrite_from_scratch = _preflight_repair_from_scratch(
                    failed_role,
                    exc,
                    repair_attempt=repair_counts[failed_role],
                )
                try:
                    if (
                        failed_role == "generator"
                        and isinstance(preparation.generator_blueprint, Mapping)
                        and preparation.generator_blueprint.get("engine")
                        == GENERATOR_RECIPE_ENGINE
                    ):
                        repaired_generator_plan, repair_usage = (
                            generate_generator_recipe(
                                client,
                                problem_id=problem_id,
                                statement=statement,
                                contract=preparation.contract,
                                settings=model_settings,
                                generation_mode=generation_mode,
                                diagnostic=diagnostic,
                                previous_recipe=preparation.generator_blueprint,
                                repair_limit=0,
                                provider_reserve_seconds=65.0,
                                budget=budget,
                                progress_callback=None,
                                cancel_scope=cancel_scope,
                            )
                        )
                        replacement = compose_generator_recipe_artifact(
                            repaired_generator_plan,
                            contract=preparation.contract,
                        )
                    else:
                        replacement, repair_usage = generate_artifact(
                            client,
                            kind=failed_role,
                            problem_id=problem_id,
                            statement=statement,
                            contract=preparation.contract,
                            settings=model_settings,
                            generator_blueprint=(
                                preparation.generator_blueprint
                                if failed_role == "generator"
                                else None
                            ),
                            generation_mode=generation_mode,
                            diagnostic=diagnostic,
                            previous_code=(
                                "" if rewrite_from_scratch else prior.code
                            ),
                            repair_from_scratch=rewrite_from_scratch,
                            provider_reserve_seconds=65.0,
                            budget=budget,
                            progress_callback=None,
                            cancel_scope=cancel_scope,
                        )
                except Exception as repair_exc:
                    details = getattr(repair_exc, "details", None)
                    if isinstance(details, dict):
                        details.setdefault(
                            "prior_machine_diagnostic", diagnostic_payload
                        )
                        details.setdefault("repair_role", failed_role)
                        details.setdefault(
                            "repair_attempt", repair_counts[failed_role]
                        )
                    combined_usage = dict(preparation.usage)
                    _add_usage(
                        combined_usage,
                        dict(getattr(repair_exc, "usage", {}) or {}),
                    )
                    try:
                        repair_exc.usage = combined_usage
                    except (AttributeError, TypeError):
                        pass
                    raise
                if failed_role in REFERENCE_ROLES:
                    replacement = replace(
                        replacement,
                        source_url=prior.source_url,
                        source_title=prior.source_title,
                        source_sha256=prior.source_sha256,
                        license=prior.license,
                    )
                machine_artifacts[failed_role] = replacement
                usage = dict(preparation.usage)
                _add_usage(usage, repair_usage)
                usage[f"{failed_role}_repairs_used"] = max(
                    int(usage.get(f"{failed_role}_repairs_used") or 0),
                    int(repair_counts[failed_role]),
                )
                preparation = StressPreparation(
                    dict(preparation.contract),
                    machine_artifacts["generator"],
                    machine_artifacts["reference_primary"],
                    machine_artifacts["reference_secondary"],
                    usage,
                    (
                        repaired_generator_plan
                        if repaired_generator_plan is not None
                        else preparation.generator_blueprint
                    ),
                    dict(preparation.generation_metadata),
                    machine_artifacts["validator"],
                )
                if (
                    repaired_generator_plan is not None
                    and blueprint_cache_key
                    and blueprint_cache_identity
                ):
                    self._save_generator_blueprint(
                        blueprint_cache_key,
                        blueprint_cache_identity,
                        repaired_generator_plan,
                        replace_alias=True,
                    )
                candidate_refs = self._checkpoint_candidates(
                    preparation,
                    platform=platform,
                    problem_id=problem_id,
                    statement=statement,
                    model_settings=model_settings,
                    generation_mode=generation_mode,
                    ai_run_id=ai_run_id,
                )
                continue
            qualified_codes = {
                role: artifact.code
                for role, artifact in machine_artifacts.items()
                if artifact is not None
            }
            with Database(self.paths.database) as db:
                qualified_store = StressCheckpointStore(
                    db,
                    platform=platform,
                    problem_id=stored_problem_id,
                    producer_ai_run_id=ai_run_id,
                )
                for role, candidate in candidate_refs.items():
                    qualified_store.save_proof(
                        candidate=candidate,
                        proof_kind="source_safety_compile",
                        identity=proof_identity,
                        status="passed",
                        result={"source_safe": True, "compiled": True},
                    )
        if minimal_verification:
            audit_reports: dict[str, dict[str, Any]] = {}
            repair_counts = _initial_repair_counts(preparation)
        else:
            preparation, audit_reports, repair_counts = (
                self._audit_and_repair_generated_artifacts(
                    client,
                    preparation,
                    problem_id=problem_id,
                    statement=statement,
                    model_settings=model_settings,
                    generation_mode=generation_mode,
                    progress_callback=progress_callback,
                    budget=budget,
                    repair_counts=repair_counts,
                    blueprint_cache_key=blueprint_cache_key,
                    blueprint_cache_identity=blueprint_cache_identity,
                    machine_gate_evidence=qualification_result,
                    machine_gate_codes=qualified_codes,
                    cancel_scope=cancel_scope,
                )
            )
        candidate_refs = self._checkpoint_candidates(
            preparation,
            platform=platform,
            problem_id=problem_id,
            statement=statement,
            model_settings=model_settings,
            generation_mode=generation_mode,
            ai_run_id=ai_run_id,
        )
        post_audit_codes = {
            "generator": preparation.generator.code if preparation.generator else "",
            "validator": preparation.validator.code if preparation.validator else "",
            "reference_primary": (
                preparation.reference_primary.code if preparation.reference_primary else ""
            ),
            "reference_secondary": (
                preparation.reference_secondary.code if preparation.reference_secondary else ""
            ),
        }
        if post_audit_codes != qualified_codes:
            if qualified_staged is not None:
                manager.discard(qualified_staged)
            qualified_staged = None
        preflight_validation: dict[str, Any] = {}
        bundle: HelperBundle | None = None
        certification_row: Mapping[str, Any] | None = None
        pre_apply_gate_result: Mapping[str, Any] | None = None
        degraded = degraded_reason is not None and preparation.validator is None
        if degraded and not unvalidated_large:
            include_large = False
        effective_include_large = bool(include_large)
        while bundle is None:
            current_artifacts = {
                "generator": preparation.generator,
                "validator": preparation.validator,
                "reference_primary": preparation.reference_primary,
                "reference_secondary": preparation.reference_secondary,
            }
            assert current_artifacts["generator"] is not None
            assert current_artifacts["reference_primary"] is not None
            assert current_artifacts["reference_secondary"] is not None
            candidate_refs = self._checkpoint_candidates(
                preparation,
                platform=platform,
                problem_id=problem_id,
                statement=statement,
                model_settings=model_settings,
                generation_mode=generation_mode,
                ai_run_id=ai_run_id,
            )
            sources = HelperSources(
                current_artifacts["generator"].code,
                current_artifacts["reference_primary"].code,
                current_artifacts["reference_secondary"].code,
                (
                    current_artifacts["validator"].code
                    if current_artifacts["validator"] is not None
                    else None
                ),
            )
            def report_preflight(
                profile_name: str,
                case_name: str,
                current: int,
                total: int,
            ) -> None:
                if progress_callback is None:
                    return
                if profile_name == "small" and case_name == "random":
                    label = f"small 随机预验 {current}/{total}"
                elif profile_name == "small" and case_name == "lower_bound":
                    label = "small 下界预验"
                elif profile_name == "large" and case_name == "upper_bound":
                    label = "large 上界预验"
                elif profile_name == "large":
                    label = "large 随机预验"
                elif profile_name == "sample":
                    label = f"官方样例预验 {current}/{max(1, total)}"
                else:
                    label = "调试构建与逐 case 预验"
                progress_callback("preflight_helpers", label, 8, 10)

            staged = qualified_staged
            qualified_staged = None
            try:
                if staged is None:
                    budget.require("compile_helpers")
                    staged = manager.stage(
                        primary,
                        sources,
                        compile_timeout=budget.subprocess_timeout(
                            "compile_helpers", 15.0
                        ),
                    )
                    with Database(self.paths.database) as db:
                        checkpoint_store = StressCheckpointStore(
                            db,
                            platform=platform,
                            problem_id=stored_problem_id,
                            producer_ai_run_id=ai_run_id,
                        )
                        for role, candidate in candidate_refs.items():
                            checkpoint_store.save_proof(
                                candidate=candidate,
                                proof_kind="source_safety_compile",
                                identity=proof_identity,
                                status="passed",
                                result={"source_safe": True, "compiled": True},
                            )
                budget.require("preflight_helpers")
                if progress_callback is not None:
                    progress_callback(
                        "preflight_helpers", "调试构建与逐 case 预验", 8, 10
                    )
                preflight_samples = self._samples(problem_id, platform=platform)
                local_recipe_preflight = bool(
                    isinstance(preparation.generator_blueprint, Mapping)
                    and preparation.generator_blueprint.get("engine")
                    in {GENERATOR_RECIPE_ENGINE, GENERATOR_RECIPE_V2_ENGINE}
                )
                preflight_config = HelperPreflightConfig(
                    contract_hash=sha256_bytes(contract_bytes),
                    samples=preflight_samples,
                    generator_blueprint=preparation.generator_blueprint,
                    validator_probes=(
                        tuple(preparation.contract.get("validator_probes", []))
                        if preparation.validator is not None
                        else ()
                    ),
                    include_large=effective_include_large,
                    exact_output=compare == "exact",
                    small_random_cases=16,
                    generator_timeout=float(timeout),
                    reference_primary_timeout=float(timeout),
                    reference_secondary_timeout=float(
                        reference_secondary_timeout or timeout
                    ),
                    validator_timeout=float(timeout),
                    require_manifest=(
                        local_recipe_preflight or not minimal_verification
                    ),
                    require_coverage=(
                        local_recipe_preflight or not minimal_verification
                    ),
                    # The 16-case small/random window is already executed in
                    # minimal mode.  Always use it to reject a seed-insensitive
                    # generator; this adds no provider calls or sandbox cases.
                    require_seed_variation=True,
                    deadline=budget.work_deadline,
                    clock=budget.clock,
                )
                preflight_validation = _run_locally_confirmed_gate(
                    lambda: manager.preflight(
                        staged,
                        preflight_config,
                        progress=report_preflight,
                    ),
                    gate_name="joint_preflight",
                    source_identity=_bundle_source_identity(current_artifacts),
                    confirmations=local_gate_confirmations,
                )
                with Database(self.paths.database) as db:
                    checkpoint_store = StressCheckpointStore(
                        db,
                        platform=platform,
                        problem_id=stored_problem_id,
                        producer_ai_run_id=ai_run_id,
                    )
                    certification_row = (
                        checkpoint_store.save_dual_reference_certification(
                            generator=candidate_refs["generator"],
                            validator=(
                                candidate_refs["validator"]
                                if "validator" in candidate_refs
                                else None
                            ),
                            reference_primary=candidate_refs["reference_primary"],
                            reference_secondary=candidate_refs["reference_secondary"],
                            compiler={"fingerprint": _compiler_fingerprint()},
                            sandbox={
                                "backend": (
                                    f"{sandbox.__class__.__module__}."
                                    f"{sandbox.__class__.__qualname__}"
                                ),
                                "policy_version": STRESS_SANDBOX_POLICY_VERSION,
                            },
                            samples=preflight_samples,
                            protocol={
                                "oracle_protocol": DUAL_REFERENCE_PROTOCOL,
                                "profile_version": 2,
                                "manifest_version": (
                                    2
                                    if isinstance(
                                        preparation.generator_blueprint, Mapping
                                    )
                                    and preparation.generator_blueprint.get("engine")
                                    == GENERATOR_RECIPE_V2_ENGINE
                                    else 1
                                ),
                                "trusted_harness_version": TRUSTED_GENERATOR_HARNESS_VERSION,
                                "contract_schema_version": 3,
                                **_recipe_identity_fields(),
                            },
                            gate={
                                "preflight_version": STRESS_PREFLIGHT_VERSION,
                                "small_random_cases": 16,
                                "include_large": effective_include_large,
                                "unvalidated": bool(degraded),
                                "degraded_reason": degraded_reason or "",
                            },
                            scope={
                                "official_samples": len(preflight_samples),
                                "small_random_cases": 16,
                                "large_cases": 2 if effective_include_large else 0,
                            },
                            preflight=preflight_validation,
                        )
                    )
                    certification_identity = _json_field(
                        certification_row, "certification_identity_json"
                    )
                    for candidate in candidate_refs.values():
                        checkpoint_store.save_proof(
                            candidate=candidate,
                            proof_kind="joint_preflight",
                            identity=certification_identity,
                            status="passed",
                            result={"certification_key": certification_row["certification_key"]},
                        )
            except HelperPreflightError as exc:
                failed_artifact = str(exc.artifact)
                failed_candidates = (
                    REFERENCE_ROLES
                    if failed_artifact == "oracle"
                    else (failed_artifact,)
                )
                if staged is not None:
                    with Database(self.paths.database) as db:
                        failed_store = StressCheckpointStore(
                            db,
                            platform=platform,
                            problem_id=stored_problem_id,
                            producer_ai_run_id=ai_run_id,
                        )
                        for failed_candidate_role in failed_candidates:
                            candidate = candidate_refs.get(failed_candidate_role)
                            if candidate is None:
                                continue
                            failed_store.save_proof(
                                candidate=candidate,
                                proof_kind=(
                                    "preflight_failure:"
                                    f"{exc.profile}:{exc.case_kind}:{exc.seed}"
                                ),
                                identity=proof_identity,
                                status="failed",
                                result={
                                    "code": str(exc.code),
                                    "message": str(exc)[:500],
                                },
                            )
                if staged is not None:
                    manager.discard(staged)
                if str(exc.code) == "stress_prepare_budget_exhausted":
                    raise PreparationBudgetExhausted(
                        budget, "preflight_helpers"
                    ) from exc
                failed_role = str(exc.artifact)
                if failed_role == "oracle":
                    primary_failure = _role_failure_details(
                        exc,
                        role="oracle",
                        stage="preflight_helpers",
                        substage="preflight",
                        elapsed=budget.elapsed(),
                        attempts=0,
                    )
                    raise StressRuntimeError(
                        "stress_oracle_preflight_conflict",
                        "两份 reference 在写入前输出不一致；旧 helper 未修改，run 未创建",
                        details={
                            **exc.details,
                            "primary_failure": primary_failure,
                        },
                        usage=preparation.usage,
                    ) from exc
                if failed_role in REFERENCE_ROLES:
                    alternate = _next_external_reference(
                        getattr(preparation, failed_role), role=failed_role
                    )
                    if alternate is not None:
                        preparation = _use_reference_alternate(
                            preparation, alternate, role=failed_role
                        )
                        continue
                if failed_role not in enabled_roles or not enabled_roles[failed_role]:
                    raise StressRuntimeError(
                        str(exc.code),
                        f"已有 {failed_role} 未通过写入前预验；旧 helper 未修改，run 未创建",
                        details=exc.details,
                        usage=preparation.usage,
                    ) from exc
                if (
                    failed_role == "generator"
                    and isinstance(preparation.generator_blueprint, Mapping)
                    and preparation.generator_blueprint.get("engine")
                    == GENERATOR_RECIPE_V2_ENGINE
                ):
                    raise StressRuntimeError(
                        "stress_local_recipe_v2_preflight_failed",
                        "确定性 generator_recipe/v2 未通过写入前预验；禁止回退或调用 AI generator",
                        details={
                            **exc.details,
                            "root_cause_code": str(exc.code),
                            "generator_engine": GENERATOR_RECIPE_V2_ENGINE,
                        },
                        usage=preparation.usage,
                    ) from exc
                repair_limit = _preflight_role_repair_limit(failed_role, exc)
                if repair_counts.get(failed_role, 0) >= repair_limit:
                    primary_failure = _role_failure_details(
                        exc,
                        role=failed_role,
                        stage="preflight_helpers",
                        substage="preflight",
                        elapsed=budget.elapsed(),
                        attempts=repair_counts.get(failed_role, 0),
                    )
                    raise StressRuntimeError(
                        str(exc.code),
                        f"{failed_role} 在 {repair_limit} 次针对性修复后仍未通过写入前预验；旧 helper 未修改，run 未创建",
                        details={
                            **exc.details,
                            "primary_failure": primary_failure,
                            "attempts": dict(repair_counts),
                            "machine_diagnostic_history": machine_diagnostic_history.get(
                                failed_role, []
                            ),
                            "remaining_seconds": round(
                                budget.remaining(include_cleanup_reserve=True), 3
                            ),
                            "reserved_gate_seconds": round(
                                budget.scaled_reserve(65.0), 3
                            ),
                        },
                        usage=preparation.usage,
                    ) from exc
                repair_counts[failed_role] = repair_counts.get(failed_role, 0) + 1
                budget.set_context(
                    generation_attempt=repair_counts[failed_role],
                    attempts=dict(repair_counts),
                    last_diagnostic=str(exc)[:500],
                )
                diagnostic_payload = {
                    "stage": "local_preflight",
                    "artifact": failed_role,
                    "repair_attempt": repair_counts[failed_role],
                    "profile": exc.profile,
                    "case_kind": exc.case_kind,
                    "seed": exc.seed,
                    "code": str(exc.code),
                    "message": str(exc),
                    "stderr": str(exc.details.get("stderr") or "")[-2000:],
                    "expected": exc.details.get("expected"),
                    "actual": exc.details.get("actual"),
                    "previous_machine_diagnostics": machine_diagnostic_history.get(
                        failed_role, []
                    )[-3:],
                }
                repair_witness = _preflight_repair_witness(exc)
                if repair_witness is not None:
                    diagnostic_payload["witness"] = repair_witness
                if failed_role == "generator":
                    diagnostic_payload["invariants_to_preserve"] = (
                        _generator_repair_invariants(
                            preparation.generator_blueprint
                        )
                    )
                diagnostic = json.dumps(
                    diagnostic_payload,
                    ensure_ascii=False,
                )[:4000]
                prior = current_artifacts[failed_role]
                repaired_generator_plan: dict[str, Any] | None = None
                rewrite_from_scratch = _preflight_repair_from_scratch(
                    failed_role,
                    exc,
                    repair_attempt=repair_counts[failed_role],
                )
                try:
                    if (
                        failed_role == "generator"
                        and isinstance(preparation.generator_blueprint, Mapping)
                        and preparation.generator_blueprint.get("engine")
                        == GENERATOR_RECIPE_ENGINE
                    ):
                        repaired_generator_plan, repair_usage = (
                            generate_generator_recipe(
                                client,
                                problem_id=problem_id,
                                statement=statement,
                                contract=preparation.contract,
                                settings=model_settings,
                                generation_mode=generation_mode,
                                diagnostic=diagnostic,
                                previous_recipe=preparation.generator_blueprint,
                                repair_limit=0,
                                provider_reserve_seconds=115.0,
                                budget=budget,
                                progress_callback=None,
                                cancel_scope=cancel_scope,
                            )
                        )
                        replacement = compose_generator_recipe_artifact(
                            repaired_generator_plan,
                            contract=preparation.contract,
                        )
                    else:
                        replacement, repair_usage = generate_artifact(
                            client,
                            kind=failed_role,
                            problem_id=problem_id,
                            statement=statement,
                            contract=preparation.contract,
                            settings=model_settings,
                            generator_blueprint=(
                                preparation.generator_blueprint
                                if failed_role == "generator"
                                else None
                            ),
                            generation_mode=generation_mode,
                            diagnostic=diagnostic,
                            previous_code=(
                                ""
                                if rewrite_from_scratch
                                else prior.code
                                if prior
                                else ""
                            ),
                            repair_from_scratch=rewrite_from_scratch,
                            provider_reserve_seconds=(
                                75.0
                                if failed_role == "generator"
                                and repair_counts[failed_role] >= 2
                                else 115.0
                                if failed_role == "generator"
                                else 65.0
                            ),
                            budget=budget,
                            progress_callback=None,
                            cancel_scope=cancel_scope,
                        )
                except Exception as repair_exc:
                    details = getattr(repair_exc, "details", None)
                    if isinstance(details, dict):
                        details.setdefault(
                            "prior_machine_diagnostic", diagnostic
                        )
                        details.setdefault("repair_role", failed_role)
                        details.setdefault(
                            "repair_attempt", repair_counts[failed_role]
                        )
                    combined_usage = dict(preparation.usage)
                    _add_usage(
                        combined_usage,
                        dict(getattr(repair_exc, "usage", {}) or {}),
                    )
                    try:
                        repair_exc.usage = combined_usage
                    except (AttributeError, TypeError):
                        pass
                    raise
                assert prior is not None
                if failed_role in REFERENCE_ROLES:
                    replacement = replace(
                        replacement,
                        source_url=prior.source_url,
                        source_title=prior.source_title,
                        source_sha256=prior.source_sha256,
                        license=prior.license,
                    )
                current_artifacts[failed_role] = replacement
                usage = dict(preparation.usage)
                _add_usage(usage, repair_usage)
                usage[f"{failed_role}_repairs_used"] = max(
                    int(usage.get(f"{failed_role}_repairs_used") or 0),
                    int(repair_counts[failed_role]),
                )
                preparation = StressPreparation(
                    dict(preparation.contract),
                    current_artifacts["generator"],
                    current_artifacts["reference_primary"],
                    current_artifacts["reference_secondary"],
                    usage,
                    (
                        repaired_generator_plan
                        if repaired_generator_plan is not None
                        else preparation.generator_blueprint
                    ),
                    dict(preparation.generation_metadata),
                    current_artifacts["validator"],
                )
                if (
                    repaired_generator_plan is not None
                    and blueprint_cache_key
                    and blueprint_cache_identity
                ):
                    self._save_generator_blueprint(
                        blueprint_cache_key,
                        blueprint_cache_identity,
                        repaired_generator_plan,
                        replace_alias=True,
                    )
                if not minimal_verification:
                    preparation, refreshed_audits, repair_counts = (
                        self._audit_and_repair_generated_artifacts(
                            client,
                            preparation,
                            problem_id=problem_id,
                            statement=statement,
                            model_settings=model_settings,
                            generation_mode=generation_mode,
                            progress_callback=progress_callback,
                            budget=budget,
                            repair_counts=repair_counts,
                            blueprint_cache_key=blueprint_cache_key,
                            blueprint_cache_identity=blueprint_cache_identity,
                            machine_gate_evidence=qualification_result,
                            machine_gate_codes=qualified_codes,
                            cancel_scope=cancel_scope,
                        )
                    )
                    audit_reports.update(refreshed_audits)
                continue
            if progress_callback is not None:
                progress_callback(
                    "apply_helpers", "安全替换 helper", 9, 10
                )
            assert staged is not None
            if _pre_apply_gate is not None and preparation.validator is not None:
                try:
                    pre_apply_gate_result = _pre_apply_gate(staged)
                except Exception as exc:
                    manager.discard(staged)
                    details = dict(getattr(exc, "details", None) or {})
                    details.setdefault(
                        "code", str(getattr(exc, "code", type(exc).__name__))
                    )
                    details.setdefault("message", str(exc)[:1000])
                    raise StressRuntimeError(
                        "stress_pre_apply_gate",
                        "模型不可见的发布前认证未通过；旧 helper 未修改，run 未创建",
                        details=details,
                        usage=preparation.usage,
                    ) from exc
            strict_validator_requested = bool(
                include_validator
                and not minimal_verification
                and not allow_validator_degradation
            )
            if strict_validator_requested and (
                preparation.validator is None
                or not _validator_preflight_succeeded(preflight_validation)
            ):
                manager.discard(staged)
                raise StressRuntimeError(
                    "stress_validator_certification_missing",
                    "validator 严格认证未生成完整的 validator/probe 证据",
                    details={
                        "validator_strict_certification_failed": True,
                        "validator_present": preparation.validator is not None,
                        "validator_preflight_succeeded": (
                            _validator_preflight_succeeded(preflight_validation)
                        ),
                        "validator_probes_recorded": isinstance(
                            preflight_validation.get("validator_probes"), list
                        ),
                    },
                    usage=preparation.usage,
                )
            budget.require("apply_helpers")
            bundle = manager.apply(staged)
        try:
            executables = self._compile_run_executables(
                bundle, primary, budget=budget, reuse_helpers=True
            )
            cache_key, cache_identity = self._preparation_cache_identity(
                platform=platform,
                problem_id=problem_id,
                statement=statement,
                compare=compare,
                include_generator=include_generator,
                include_validator=preparation.validator is not None,
                include_reference_primary=include_reference_primary,
                include_reference_secondary=include_reference_secondary,
                include_large=include_large,
                model_settings=model_settings,
                sandbox=sandbox,
                generation_mode=generation_mode,
                contract=preparation.contract,
                generator_blueprint=preparation.generator_blueprint,
            )
            generator_capabilities = dict(
                preflight_validation.get("generator_capabilities") or {}
            )
            if progress_callback is not None:
                progress_callback("create_stress_run", "创建持续对拍 run", 10, 10)
            run_id = str(uuid4())
            first_seed = int(seed) if seed is not None else random.SystemRandom().randrange(1, 2**63)
            config = StressRunConfig(
                first_seed=first_seed,
                profile_version=2,
                schedule_offset=0,
                include_large=bool(include_large),
                small_per_cycle=4,
                large_per_cycle=1 if include_large else 0,
                solution_timeout=float(timeout),
                generator_timeout=float(timeout),
                reference_primary_timeout=float(timeout),
                reference_secondary_timeout=float(
                    reference_secondary_timeout or timeout
                ),
                oracle_protocol=DUAL_REFERENCE_PROTOCOL,
                validator_timeout=float(timeout),
                max_cases=run_max_cases,
                exact_output=compare == "exact",
            )
            budget.require("create_stress_run")
            if certification_row is None:
                raise StressRuntimeError(
                    "stress_certification_missing",
                    "完整 preflight 未生成联合认证，拒绝应用 helper",
                )
            self._persist_bundle_and_run(
                bundle,
                preparation,
                platform=platform,
                problem_id=problem_id,
                attempt_id=attempt_id,
                ai_run_id=ai_run_id,
                primary=primary,
                run_id=run_id,
                config=config,
                generator_capabilities=generator_capabilities,
                preflight_validation=preflight_validation,
                audit_reports=audit_reports,
                preparation_cache_key=cache_key,
                certification_key=str(certification_row["certification_key"]),
                cacheable=not manual_override and not degraded,
                unvalidated=bool(degraded),
                degraded_reason=degraded_reason,
                validator_requested=bool(
                    include_validator and not minimal_verification
                ),
                validator_certified=(
                    preparation.validator is not None
                    and _validator_preflight_succeeded(preflight_validation)
                ),
                preparation_meta={
                    "cache_identity": cache_identity,
                    "contract_sha256": sha256_bytes(contract_bytes),
                    "generation_mode": generation_mode,
                    "generator_blueprint": (
                        dict(preparation.generator_blueprint)
                        if preparation.generator_blueprint is not None
                        else None
                    ),
                    "generator_blueprint_sha256": (
                        sha256_bytes(_canonical_json(preparation.generator_blueprint))
                        if preparation.generator_blueprint is not None
                        else ""
                    ),
                    "generator_recipe": (
                        dict(preparation.generator_blueprint)
                        if isinstance(preparation.generator_blueprint, Mapping)
                        and preparation.generator_blueprint.get("engine")
                        in {GENERATOR_RECIPE_ENGINE, GENERATOR_RECIPE_V2_ENGINE}
                        else None
                    ),
                    "recipe_sha256": (
                        sha256_bytes(_canonical_json(preparation.generator_blueprint))
                        if isinstance(preparation.generator_blueprint, Mapping)
                        and preparation.generator_blueprint.get("engine")
                        in {GENERATOR_RECIPE_ENGINE, GENERATOR_RECIPE_V2_ENGINE}
                        else ""
                    ),
                    "generator_engine": str(
                        preparation.generation_metadata.get("generator_engine")
                        or "legacy_ai_cpp"
                    ),
                    **_recipe_identity_fields(),
                    "blueprint_cache_result": blueprint_cache_result,
                    "generation": dict(preparation.generation_metadata),
                    "fast_fallback_used": bool(
                        preparation.usage.get("fast_fallback_used", False)
                    ),
                    "helper_hashes": dict(bundle.applied_hashes),
                    "release_executable_hashes": {
                        role: sha256_file(Path(path))
                        for role, path in bundle.release_executables.items()
                    },
                    "budget": budget.snapshot(),
                    "cache_result": "miss" if not force_regenerate else "forced",
                    "contract_cache_result": contract_cache_result,
                    "validator_candidate_id": (
                        candidate_refs["validator"].id
                        if "validator" in candidate_refs
                        else "unvalidated"
                    ),
                    "unvalidated": bool(degraded),
                    "degraded_reason": degraded_reason or "",
                },
            )
        except Exception:
            HelperBundleManager(self.paths.root, self._sandbox_factory()).revert(bundle)
            raise
        self._launch(
            run_id, bundle, executables, config, preparation, platform=platform
        )
        return {
            "ok": True,
            "run": self.run(run_id),
            "bundle": self.bundle(bundle.bundle_id),
            "usage": preparation.usage,
            "pre_apply_gate_result": (
                dict(pre_apply_gate_result) if pre_apply_gate_result is not None else None
            ),
            "preparation": {
                **budget.snapshot(),
                "cache_result": "miss" if not force_regenerate else "forced",
                "contract_cache_result": contract_cache_result,
                "blueprint_cache_result": blueprint_cache_result,
                "generation_mode": generation_mode,
                "generation": dict(preparation.generation_metadata),
                "fast_fallback_used": bool(
                    preparation.usage.get("fast_fallback_used", False)
                ),
                "cache_key": cache_key,
            },
        }

    def _compile_run_executables(
        self,
        bundle: HelperBundle,
        primary: Path,
        *,
        budget: PreparationBudget | None = None,
        reuse_helpers: bool = False,
    ) -> StressExecutables:
        compiler = resolve_cpp_compiler("g++")
        if compiler is None:
            raise StressRuntimeError("compiler_missing", "找不到可用的 C++17 编译器")
        staging = Path(bundle.staging_dir).resolve()
        targets = {"solution": primary}
        outputs: dict[str, Path] = {}
        helper_roles = (
            ("generator", *REFERENCE_ROLES)
            if set(REFERENCE_ROLES).issubset(bundle.helper_paths)
            else ("generator", "brute", "reference")
        )
        for role in helper_roles:
            cached = Path(str(bundle.release_executables.get(role) or ""))
            if reuse_helpers and cached.is_file():
                outputs[role] = cached
            else:
                targets[role] = Path(bundle.helper_paths[role])
        validator_executable = Path(
            str(bundle.release_executables.get("validator") or "")
        )
        if validator_executable.is_file():
            outputs["validator"] = validator_executable

        def compile_target(role: str, source: Path) -> tuple[str, Path]:
            if budget is not None:
                compile_timeout = budget.subprocess_timeout(
                    f"compile_{role}", 20.0
                )
            else:
                compile_timeout = 20.0
            output = staging / (f"run-{role}.exe" if os.name == "nt" else f"run-{role}")
            command = [
                compiler,
                "-std=c++17",
                *portable_cpp_flags(("-O2", "-static")),
                str(source),
                "-o",
                str(output),
            ]
            try:
                compiled = subprocess.run(
                    command,
                    cwd=staging,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=compile_timeout,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise StressRuntimeError("stress_compile_failed", f"{role} 编译失败：{exc}") from None
            if compiled.returncode != 0:
                detail = (compiled.stdout + compiled.stderr).decode(errors="replace")[:2000]
                raise StressRuntimeError("stress_compile_failed", f"{role} 编译失败：{detail}")
            return role, output

        with ThreadPoolExecutor(
            max_workers=min(4, len(targets)), thread_name_prefix="stress-run-build"
        ) as executor:
            futures = {
                executor.submit(compile_target, role, source): role
                for role, source in targets.items()
            }
            for future in as_completed(futures):
                role, output = future.result()
                outputs[role] = output
        if set(REFERENCE_ROLES).issubset(outputs):
            return StressExecutables(
                outputs["solution"],
                outputs["generator"],
                reference_primary=outputs["reference_primary"],
                reference_secondary=outputs["reference_secondary"],
                validator=outputs.get("validator"),
            )
        return StressExecutables(
            outputs["solution"],
            outputs["generator"],
            brute=outputs["brute"],
            reference=outputs["reference"],
            validator=outputs.get("validator"),
        )

    def _persist_bundle_and_run(
        self,
        bundle: HelperBundle,
        preparation: StressPreparation,
        *,
        platform: str,
        problem_id: str,
        attempt_id: int | None,
        ai_run_id: str | None,
        primary: Path,
        run_id: str,
        config: StressRunConfig,
        generator_capabilities: Mapping[str, Any],
        preflight_validation: Mapping[str, Any],
        audit_reports: Mapping[str, Mapping[str, Any]],
        preparation_cache_key: str,
        certification_key: str,
        preparation_meta: Mapping[str, Any],
        cacheable: bool = True,
        unvalidated: bool = False,
        degraded_reason: str | None = None,
        validator_requested: bool = False,
        validator_certified: bool = False,
    ) -> None:
        stored_problem_id = (
            problem_id[2:]
            if platform == "codeforces" and problem_id.upper().startswith("CF")
            else problem_id
        )
        artifacts = {
            "generator": preparation.generator,
            "validator": preparation.validator,
            "reference_primary": preparation.reference_primary,
            "reference_secondary": preparation.reference_secondary,
        }
        with Database(self.paths.database) as db:
            with db.atomic():
                db.upsert_problem({"platform": platform, "problem_id": stored_problem_id})
                if cacheable:
                    db.save_stress_preparation_cache(
                        preparation_cache_key,
                        payload={
                            "cache_identity": dict(
                                preparation_meta.get("cache_identity") or {}
                            )
                        },
                        metadata={
                            "status": "validated",
                            "last_used_at": _utc_now(),
                        },
                    )
                db.create_stress_artifact_bundle(
                    bundle.bundle_id,
                    platform=platform,
                    problem_id=stored_problem_id,
                    attempt_id=attempt_id,
                    contract=preparation.contract,
                    baseline_manifest=bundle.to_dict(),
                    preparation_cache_key=(
                        preparation_cache_key if cacheable else None
                    ),
                    certification_key=certification_key,
                    preparation_meta=preparation_meta,
                    status="applied",
                )
                db.update_stress_artifact_bundle(
                    bundle.bundle_id,
                    status="applied",
                    backup_path=bundle.backup_dir,
                    applied_at=_utc_now(),
                )
                for role, generated in artifacts.items():
                    if generated is None:
                        if role == "validator":
                            continue
                        path = Path(bundle.helper_paths[role])
                        generated = GeneratedArtifact(
                            role, path.read_text(encoding="utf-8"), "local_existing", "使用已有 helper"
                        )
                    if role == "validator":
                        target_path = (
                            Path(bundle.staging_dir)
                            / f"{bundle.problem_id}.validator.cpp"
                        )
                        baseline_hash = None
                    else:
                        target_path = bundle.helper_paths[role]
                        baseline_hash = bundle.baseline_hashes[role]
                    db.save_stress_artifact(
                        str(uuid4()),
                        bundle_id=bundle.bundle_id,
                        kind=role,
                        source_code=generated.code,
                        target_path=target_path,
                        source_kind=generated.origin,
                        ai_run_id=ai_run_id,
                        baseline_hash=baseline_hash,
                        source_url=generated.source_url,
                        source_title=generated.source_title,
                        source_license=generated.license,
                        source_content_hash=generated.source_sha256,
                        status="applied",
                        validation={
                            "sandbox_required": True,
                            "compiled": True,
                            "preflight": dict(preflight_validation),
                            "ai_audit": dict(audit_reports.get(role) or {}),
                            **(
                                {
                                    "profile_version": 2,
                                    "generator_capabilities": dict(generator_capabilities),
                                }
                                if role == "generator"
                                else {}
                            ),
                        },
                        metadata={
                            "notes": generated.notes,
                            "static_audit": generated.static_audit or {},
                        },
                    )
                db.create_stress_run(
                    run_id,
                    bundle_id=bundle.bundle_id,
                    platform=platform,
                    problem_id=stored_problem_id,
                    user_source_path=primary,
                    user_source_hash=sha256_file(primary),
                    attempt_id=attempt_id,
                    config={
                        **asdict(config),
                        "rate_base_total": 0,
                        "unvalidated": bool(unvalidated),
                        "degraded_reason": degraded_reason or "",
                        "validator_requested": bool(validator_requested),
                        "validator_certified": bool(validator_certified),
                    },
                    status="pending",
                    phase="preparing",
                    start_seed=config.first_seed,
                )
                certification = db.stress_bundle_certification(certification_key)
                if certification is None:
                    raise StressRuntimeError(
                        "stress_certification_missing",
                        "联合认证记录不存在，拒绝发布缓存 alias",
                    )
                checkpoint_store = StressCheckpointStore(
                    db,
                    platform=platform,
                    problem_id=stored_problem_id,
                    producer_ai_run_id=ai_run_id,
                )
                if not cacheable:
                    return
                certification_alias_key = f"bundle:{preparation_cache_key}"
                current_alias = db.stress_cache_alias(certification_alias_key)
                if (
                    current_alias is None
                    or str(current_alias["target_id"]) != certification_key
                ):
                    checkpoint_store.publish_certification_alias(
                        certification_alias_key,
                        certification,
                        succeeded=True,
                        expected_revision=(
                            int(current_alias["revision"])
                            if current_alias is not None
                            else None
                        ),
                    )

    def _persist_cached_run(
        self,
        bundle: HelperBundle,
        *,
        platform: str,
        problem_id: str,
        attempt_id: int | None,
        primary: Path,
        run_id: str,
        config: StressRunConfig,
        validator_requested: bool = False,
        validator_certified: bool = False,
    ) -> None:
        stored_problem_id = (
            problem_id[2:]
            if platform == "codeforces" and problem_id.upper().startswith("CF")
            else problem_id
        )
        with Database(self.paths.database) as db:
            db.create_stress_run(
                run_id,
                bundle_id=bundle.bundle_id,
                platform=platform,
                problem_id=stored_problem_id,
                user_source_path=primary,
                user_source_hash=sha256_file(primary),
                attempt_id=attempt_id,
                config={
                    **asdict(config),
                    "rate_base_total": 0,
                    "validator_requested": bool(validator_requested),
                    "validator_certified": bool(validator_certified),
                },
                status="pending",
                phase="preparing",
                start_seed=config.first_seed,
            )

    def _samples(
        self, problem_id: str, *, platform: str | None = None
    ) -> list[SampleCase]:
        directory = self.paths.cases / problem_id
        samples: list[SampleCase] = []
        seen: set[str] = set()

        def append_sample(name: str, input_data: bytes, expected_output: bytes) -> None:
            digest = hashlib.sha256()
            digest.update(len(input_data).to_bytes(8, "big"))
            digest.update(input_data)
            digest.update(len(expected_output).to_bytes(8, "big"))
            digest.update(expected_output)
            key = digest.hexdigest()
            if key in seen:
                return
            seen.add(key)
            samples.append(SampleCase(name, input_data, expected_output))

        if platform is not None and self.paths.database.is_file():
            stored_problem_id = (
                problem_id[2:]
                if platform == "codeforces" and problem_id.upper().startswith("CF")
                else problem_id
            )
            with Database(self.paths.database) as db:
                rows = db.problem_samples(platform, stored_problem_id)
            for row in rows:
                append_sample(
                    str(row["sample_key"]),
                    bytes(row["input_data"]),
                    bytes(row["expected_output"]),
                )
        if directory.is_dir():
            for input_path in sorted(directory.glob("*.in")):
                output_path = input_path.with_suffix(".out")
                if output_path.is_file():
                    append_sample(
                        input_path.stem,
                        input_path.read_bytes(),
                        output_path.read_bytes(),
                    )
        return samples

    def _run_contract(
        self, bundle: HelperBundle, preparation: StressPreparation | None
    ) -> Mapping[str, Any] | None:
        """The contract the search driver profiles inputs against.

        ``preparation`` is ``None`` on the resume path, so fall back to the copy
        persisted with the bundle.  Returning ``None`` is safe — the runner then
        stays recipe-only, which is the pre-driver behaviour.
        """

        if preparation is not None and preparation.contract:
            return preparation.contract
        try:
            with Database(self.paths.database) as db:
                row = db.stress_artifact_bundle(bundle.bundle_id)
        except (OSError, sqlite3.Error):
            return None
        if row is None:
            return None
        try:
            contract = json.loads(row["contract_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return contract if isinstance(contract, Mapping) and contract else None

    def _launch(
        self,
        run_id: str,
        bundle: HelperBundle,
        executables: StressExecutables,
        config: StressRunConfig,
        preparation: StressPreparation | None,
        *,
        platform: str,
        base_small: int = 0,
        base_large: int = 0,
        base_total: int = 0,
        reference_source_override: Mapping[str, str] | None = None,
        reference_sources_override: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        with self._lock:
            existing = self._threads.get(run_id)
            if existing is not None and existing.is_alive():
                raise StressRuntimeError("stress_run_active", "该持续对拍已经在运行")
            token = StopToken()
            validation_goal = config.warmup_small_cases
            helpers_validated = base_small >= validation_goal and validation_goal > 0

            def progress(state: StressRunResult) -> None:
                nonlocal helpers_validated
                small_count = base_small + state.small_cases
                large_count = base_large + state.large_cases
                total_count = base_total + state.total_cases
                phase = (
                    f"{state.current_profile}:{state.case_kind}"
                    if state.current_profile and state.case_kind
                    else ("small_validation" if small_count < validation_goal else "mixed")
                )
                with Database(self.paths.database) as db:
                    row = db.stress_run(run_id)
                    if row is None:
                        token.request_stop()
                        return
                    if row["status"] == "stop_requested":
                        token.request_stop()
                        return
                    if row["status"] == "interrupted":
                        token.request_stop()
                        return
                    db.update_stress_run(
                        run_id,
                        status="running",
                        phase=phase,
                        current_seed=max(config.first_seed, state.next_seed - 1),
                        next_seed=state.next_seed,
                        small_count=small_count,
                        large_count=large_count,
                        total_count=total_count,
                    )
                    if not helpers_validated and small_count >= validation_goal and validation_goal > 0:
                        for artifact in db.stress_artifacts(bundle.bundle_id):
                            db.update_stress_artifact(
                                str(artifact["id"]),
                                status="validated",
                                validation={
                                    "sandbox_required": True,
                                    "compiled": True,
                                    "small_validation_cases": small_count,
                                    "profile_version": config.profile_version,
                                },
                            )
                        helpers_validated = True

            reference_source = dict(reference_source_override or {})
            reference_sources = {
                str(role): dict(source)
                for role, source in dict(reference_sources_override or {}).items()
            }
            if preparation is not None:
                for role in REFERENCE_ROLES:
                    reference = getattr(preparation, role)
                    if reference is not None:
                        reference_sources[role] = {
                            "url": reference.source_url or "",
                            "title": reference.source_title or "",
                            "origin": reference.origin,
                        }
            helper_roles = (
                ("generator", *REFERENCE_ROLES)
                if set(REFERENCE_ROLES).issubset(bundle.helper_paths)
                else ("generator", "brute", "reference")
            )
            source_hashes = {"solution": sha256_file(executables.solution)}
            source_hashes.update(
                {
                    role: sha256_file(Path(bundle.helper_paths[role]))
                    for role in helper_roles
                }
            )
            conflict_role = (
                "reference_primary"
                if "reference_primary" in bundle.helper_paths
                else "reference"
            )
            runner = LayeredStressRunner(
                self.paths.root,
                bundle.problem_id,
                executables,
                self._sandbox_factory(),
                stop_token=token,
                progress=progress,
                source_hashes=source_hashes,
                reference_source=reference_source,
                reference_sources=reference_sources,
                conflict_export_dir=Path(bundle.helper_paths[conflict_role]).resolve().parent,
                contract=self._run_contract(bundle, preparation),
            )

            def target() -> None:
                try:
                    with Database(self.paths.database) as db:
                        current = db.stress_run(run_id)
                        if current is not None and current["status"] == "interrupted":
                            return
                        if current is not None and current["status"] == "stop_requested":
                            token.request_stop()
                        else:
                            db.update_stress_run(
                                run_id,
                                status="running",
                                phase="samples",
                                started_at=_utc_now(),
                            )
                    result = runner.run(
                        config,
                        samples=self._samples(
                            bundle.problem_id, platform=platform
                        ),
                    )
                    terminal = {
                        "stopped": "stopped",
                        "mismatch": "mismatch",
                        "oracle_conflict": "oracle_conflict",
                        "limit_reached": "completed",
                    }.get(result.status, "fault")
                    with Database(self.paths.database) as db:
                        current = db.stress_run(run_id)
                        if current is not None and current["status"] == "interrupted":
                            return
                        user_finished = bool(
                            current is not None
                            and current["stop_reason"] == "user_finished"
                        )
                        db.update_stress_run(
                            run_id,
                            status="completed" if user_finished else terminal,
                            phase="complete",
                            current_seed=max(config.first_seed, result.next_seed - 1),
                            next_seed=result.next_seed,
                            small_count=base_small + result.small_cases,
                            large_count=base_large + result.large_cases,
                            total_count=base_total + result.total_cases,
                            mismatch_seed=(
                                result.next_seed - 1
                                if result.failure_dir and result.total_cases > 0
                                else None
                            ),
                            failure_path=result.failure_dir,
                            stop_reason=(
                                "user_finished"
                                if user_finished
                                else result.detail or result.status
                            ),
                            completed_at=_utc_now(),
                        )
                except Exception as exc:
                    with Database(self.paths.database) as db:
                        current = db.stress_run(run_id)
                        if current is not None and current["status"] == "interrupted":
                            return
                        db.update_stress_run(
                            run_id,
                            status="fault",
                            phase="complete",
                            error={"code": getattr(exc, "code", "stress_failed"), "message": str(exc)[:500]},
                            stop_reason="stress_failed",
                            completed_at=_utc_now(),
                        )
                finally:
                    runner.cleanup()
                    with self._lock:
                        self._runners.pop(run_id, None)
                        self._threads.pop(run_id, None)

            thread = threading.Thread(target=target, name=f"acm-stress-{run_id[:8]}", daemon=True)
            self._runners[run_id] = runner
            self._threads[run_id] = thread
            thread.start()

    def runs(self, *, problem_id: str | None = None) -> list[dict[str, Any]]:
        with Database(self.paths.database) as db:
            return [
                _row_dict(row)
                for row in db.stress_runs(problem_id=problem_id, limit=100)
            ]

    def run(self, run_id: str) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            row = db.stress_run(run_id)
        if row is None:
            raise KeyError(f"Stress run {run_id!r} not found")
        return _row_dict(row)

    def stop(self, run_id: str) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            row = db.request_stress_run_stop(run_id)
        with self._lock:
            runner = self._runners.get(run_id)
        if runner is not None:
            runner.request_stop()
        return {"ok": True, "run": _row_dict(row)}

    def finish(self, run_id: str) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            row = db.request_stress_run_finish(run_id)
        with self._lock:
            runner = self._runners.get(run_id)
        if runner is not None:
            runner.request_stop()
        return {"ok": True, "run": _row_dict(row)}

    def resume(self, run_id: str) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            active = db.active_stress_run()
            if active is not None and active["id"] != run_id:
                raise StressRuntimeError("stress_run_active", f"已有持续对拍正在运行：{active['id']}")
            row = db.stress_run(run_id)
            if row is None:
                raise KeyError(f"Stress run {run_id!r} not found")
            if row["status"] not in {
                "interrupted",
                "stopped",
                "mismatch",
                "oracle_conflict",
                "fault",
            }:
                raise StressRuntimeError(
                    "stress_run_not_resumable",
                    f"持续对拍不能从 {row['status']} 状态继续",
                )
            bundle_row = db.stress_artifact_bundle(str(row["bundle_id"]))
            artifact_rows = (
                db.stress_artifacts(str(row["bundle_id"]))
                if bundle_row is not None
                else []
            )
            config_data = _json_field(row, "config_json")
            base_small = int(row["small_count"])
            base_large = int(row["large_count"])
            base_total = int(row["total_count"])
            config = _stress_config(
                config_data,
                first_seed=int(row["next_seed"]),
                schedule_offset=base_total,
            )
        if bundle_row is None:
            raise KeyError("Stress artifact bundle not found")
        if str(bundle_row["status"]) != "applied":
            raise StressRuntimeError("stress_bundle_not_applied", "helper bundle 已回退，不能继续")
        manifest = _json_field(bundle_row, "baseline_manifest_json")
        bundle = HelperBundle(
            **{
                key: manifest[key]
                for key in HelperBundle.__dataclass_fields__
                if key in manifest
            }
        )
        primary = Path(str(row["user_source_path"]))
        if not primary.is_file():
            raise StressRuntimeError("stress_source_missing", "用户源码不存在，不能继续")
        current_source_hash = sha256_file(primary)
        executables = self._compile_run_executables(bundle, primary)
        with Database(self.paths.database) as db:
            active = db.active_stress_run()
            if active is not None and active["id"] != run_id:
                raise StressRuntimeError(
                    "stress_run_active", f"已有持续对拍正在运行：{active['id']}"
                )
            row = db.resume_stress_run(
                run_id,
                user_source_hash=current_source_hash,
                rate_base_total=base_total,
            )
        reference_row = next(
            (item for item in artifact_rows if str(item["kind"]) == "reference"),
            None,
        )
        reference_source = (
            {
                "url": str(reference_row["source_url"] or ""),
                "title": str(reference_row["source_title"] or ""),
                "origin": str(reference_row["source_kind"] or ""),
            }
            if reference_row is not None
            else {}
        )
        reference_sources = {
            role: {
                "url": str(item["source_url"] or ""),
                "title": str(item["source_title"] or ""),
                "origin": str(item["source_kind"] or ""),
            }
            for role in REFERENCE_ROLES
            for item in artifact_rows
            if str(item["kind"]) == role
        }
        self._launch(
            run_id,
            bundle,
            executables,
            config,
            None,
            platform=str(row["platform"]),
            base_small=base_small,
            base_large=base_large,
            base_total=base_total,
            reference_source_override=reference_source,
            reference_sources_override=reference_sources,
        )
        return {"ok": True, "run": self.run(run_id)}

    def bundle(self, bundle_id: str) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            row = db.stress_artifact_bundle(bundle_id)
            artifacts = db.stress_artifacts(bundle_id) if row is not None else []
        if row is None:
            raise KeyError(f"Stress artifact bundle {bundle_id!r} not found")
        return {
            **_row_dict(row),
            "artifacts": [_row_dict(item) for item in artifacts],
        }

    def revert_bundle(self, bundle_id: str) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            row = db.stress_artifact_bundle(bundle_id)
            if row is None:
                raise KeyError(f"Stress artifact bundle {bundle_id!r} not found")
            runs = db.stress_runs(limit=100)
            if any(item["bundle_id"] == bundle_id and item["status"] in {"pending", "preparing", "running", "stop_requested"} for item in runs):
                raise StressRuntimeError("stress_bundle_active", "运行中的 bundle 不能回退")
            manifest = _json_field(row, "baseline_manifest_json")
        bundle = HelperBundle(
            **{
                key: manifest[key]
                for key in HelperBundle.__dataclass_fields__
                if key in manifest
            }
        )
        HelperBundleManager(self.paths.root, self._sandbox_factory()).revert(bundle)
        with Database(self.paths.database) as db:
            updated = db.update_stress_artifact_bundle(
                bundle_id,
                status="reverted",
                reverted_at=_utc_now(),
            )
            for artifact in db.stress_artifacts(bundle_id):
                db.update_stress_artifact(str(artifact["id"]), status="reverted")
        return {"ok": True, "bundle": _row_dict(updated)}

    def shutdown(self) -> tuple[str, ...]:
        with self._lock:
            active = list(self._runners.items())
            threads = list(self._threads.items())
        for _run_id, runner in active:
            runner.request_stop()
        # request_stop cancels the active sandbox process.  Wait for each
        # worker's finally block to remove its run directory and release the
        # copied AppContainer launcher before a disposable workspace is
        # deleted.  Threads remain daemonized as a final process-exit guard.
        deadline = time.monotonic() + 10.0
        for _run_id, thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        # A worker that observed request_stop owns its final state transition.
        # Only force an interrupted state for workers still alive after the
        # cleanup deadline, avoiding an optimistic-revision race with normal
        # worker completion.
        live_run_ids = tuple(
            run_id for run_id, thread in threads if thread.is_alive()
        )
        for run_id in live_run_ids:
            with Database(self.paths.database) as db:
                for _attempt in range(3):
                    row = db.stress_run(run_id)
                    if row is None or row["status"] not in {
                        "pending", "preparing", "running", "stop_requested"
                    }:
                        break
                    try:
                        db.update_stress_run(
                            run_id,
                            expected_revision=int(row["revision"]),
                            status="interrupted",
                            phase="complete",
                            stop_reason="service_shutdown",
                            completed_at=_utc_now(),
                        )
                        break
                    except StressRunRevisionConflict:
                        continue
        # Callers that own a disposable workspace need to know whether it is
        # safe to remove that workspace.  Returning the still-live run ids is
        # backward compatible with callers that ignore shutdown's result.
        return live_run_ids


__all__ = ["StressCoordinator", "StressRuntimeError", "normalize_stress_failure"]
