"""Stress contract normalization and prompt compaction.

Turns a raw model response into a canonical schema v3 contract: field and
syntax normalization, verbatim statement evidence, constraints, validator
probes and computable coverage obligations."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .stress_ai_schema import (
    _BASE_GENERATOR_REQUIREMENTS,
    _CONTRACT_CONSTRAINT_KINDS,
    _CONTRACT_COVERAGE_PREDICATES,
    _CONTRACT_FIELD_TYPE_ALIASES,
    _CONTRACT_FIELD_TYPES,
    _CONTRACT_SECTION_KINDS,
    CONTRACT_SCHEMA_VERSION,
)
from .stress_ai_core import StressPreparationError

def _compact_audit_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    head = max(1, (limit * 2) // 3)
    tail = max(1, limit - head)
    return text[:head] + "\n...[中间内容因快速审查预算省略]...\n" + text[-tail:]


def _compact_audit_contract(
    contract: Mapping[str, Any], *, kind: str | None = None
) -> dict[str, Any]:
    """Project a verified contract onto the facts needed by one audit role.

    Evidence bindings and generator prose have already been checked while the
    contract was normalized.  Repeating them for every helper audit is both
    expensive and distracting: the auditor needs the resulting executable
    syntax/constraints, not the source offsets that proved where they came
    from.  Keep descriptions because they can carry syntax semantics that are
    not representable by the small structural vocabulary.
    """

    def compact_field(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        keys = (
            "name",
            "type",
            "minimum",
            "maximum",
            "count",
            "count_from",
            "description",
        )
        return {key: value[key] for key in keys if key in value}

    def compact_syntax(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        result = {key: value[key] for key in ("mode", "eof") if key in value}
        sections: list[dict[str, Any]] = []
        raw_sections = value.get("sections")
        if isinstance(raw_sections, Sequence) and not isinstance(
            raw_sections, (str, bytes)
        ):
            for raw_section in raw_sections:
                if not isinstance(raw_section, Mapping):
                    continue
                section = {
                    key: raw_section[key]
                    for key in (
                        "id",
                        "kind",
                        "count_from",
                        "alphabet",
                        "description",
                    )
                    if key in raw_section
                }
                raw_fields = raw_section.get("fields")
                if isinstance(raw_fields, Sequence) and not isinstance(
                    raw_fields, (str, bytes)
                ):
                    section["fields"] = [
                        item
                        for raw_field in raw_fields
                        if (item := compact_field(raw_field)) is not None
                    ]
                raw_variants = raw_section.get("variants")
                if isinstance(raw_variants, Sequence) and not isinstance(
                    raw_variants, (str, bytes)
                ):
                    variants: list[dict[str, Any]] = []
                    for raw_variant in raw_variants:
                        if not isinstance(raw_variant, Mapping):
                            continue
                        variant = {
                            key: raw_variant[key]
                            for key in ("tag", "name", "description")
                            if key in raw_variant
                        }
                        variant_fields = raw_variant.get("fields")
                        if isinstance(variant_fields, Sequence) and not isinstance(
                            variant_fields, (str, bytes)
                        ):
                            variant["fields"] = [
                                item
                                for raw_field in variant_fields
                                if (item := compact_field(raw_field)) is not None
                            ]
                        variants.append(variant)
                    section["variants"] = variants
                sections.append(section)
        result["sections"] = sections
        return result

    def compact_items(value: Any, keys: Sequence[str]) -> list[dict[str, Any]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return []
        return [
            {key: item[key] for key in keys if key in item}
            for item in value
            if isinstance(item, Mapping)
        ]

    role = str(kind or "reference").strip().casefold()
    common = (
        "schema_version",
        "validation_level",
        "input_summary",
        "small_profile",
        "small_lower_boundary",
    )
    if role == "brute":
        keys = common + ("output_compare",)
    elif role in {"generator", "validator"}:
        keys = common + ("large_profile", "large_upper_boundary")
    else:
        # References are executed on both profiles and compared as an answer.
        keys = common + (
            "large_profile",
            "large_upper_boundary",
            "output_compare",
        )
    result = {key: contract[key] for key in keys if key in contract}
    syntax = compact_syntax(contract.get("syntax"))
    if syntax is not None:
        result["syntax"] = syntax
    if "constraints" in contract:
        result["constraints"] = compact_items(
            contract.get("constraints"), ("id", "kind", "target", "args")
        )
    if role in {"generator", "validator"} and "coverage_obligations" in contract:
        result["coverage_obligations"] = compact_items(
            contract.get("coverage_obligations"),
            ("id", "scope", "predicate", "minimum_witnesses"),
        )
    return result


def _compact_generator_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only contract-v3 facts needed to plan and generate input cases.

    Evidence quotes and compatibility prose have already been verified locally.
    Repeating them in recipe/generator calls costs tokens without giving those
    roles any additional executable obligation.
    """

    result: dict[str, Any] = {
        key: contract[key]
        for key in (
            "schema_version",
            "validation_level",
            "input_summary",
            "small_profile",
            "small_lower_boundary",
            "large_profile",
            "large_upper_boundary",
            "output_compare",
        )
        if key in contract
    }
    syntax = contract.get("syntax")
    if isinstance(syntax, Mapping):
        compact_syntax = {
            key: syntax[key] for key in ("mode", "eof") if key in syntax
        }
        compact_sections: list[dict[str, Any]] = []
        for raw_section in syntax.get("sections", []):
            if not isinstance(raw_section, Mapping):
                continue
            section = {
                key: raw_section[key]
                for key in ("id", "kind", "count_from", "fields", "variants")
                if key in raw_section
            }
            compact_sections.append(section)
        compact_syntax["sections"] = compact_sections
        result["syntax"] = compact_syntax
    result["constraints"] = [
        {
            key: item[key]
            for key in ("id", "kind", "target", "args")
            if key in item
        }
        for item in contract.get("constraints", [])
        if isinstance(item, Mapping)
    ]
    result["coverage_obligations"] = [
        {
            key: item[key]
            for key in ("id", "scope", "predicate", "minimum_witnesses")
            if key in item
        }
        for item in contract.get("coverage_obligations", [])
        if isinstance(item, Mapping)
    ]
    # Operation-stream evidence often contains the semantics of stateful
    # parameters (for example, whether an argument is a signed displacement or
    # another identifier).  Syntax names and numeric ranges alone are not
    # enough for a generator to preserve dynamic legality.  Include only the
    # bounded quotes referenced by operation/state facts instead of resending
    # the complete statement.
    semantic_evidence_ids: set[str] = set()
    if isinstance(syntax, Mapping):
        for raw_section in syntax.get("sections", []):
            if not isinstance(raw_section, Mapping):
                continue
            if str(raw_section.get("kind") or "") == "operation_stream":
                semantic_evidence_ids.update(
                    str(item) for item in raw_section.get("evidence_ids", [])
                )
                for variant in raw_section.get("variants", []):
                    if isinstance(variant, Mapping):
                        semantic_evidence_ids.update(
                            str(item)
                            for item in variant.get("evidence_ids", [])
                        )
    for item in contract.get("constraints", []):
        if (
            isinstance(item, Mapping)
            and str(item.get("kind") or "")
            in {"state_precondition", "dependent_bound", "custom_text"}
        ):
            semantic_evidence_ids.update(
                str(ref) for ref in item.get("evidence_ids", [])
            )
    semantic_evidence = [
        {"id": str(item.get("id") or ""), "quote": str(item.get("quote") or "")}
        for item in contract.get("evidence", [])
        if isinstance(item, Mapping)
        and str(item.get("id") or "") in semantic_evidence_ids
    ][:4]
    if semantic_evidence:
        result["semantic_evidence"] = semantic_evidence
    return result


def _compact_repair_contract(
    contract: Mapping[str, Any], *, kind: str
) -> dict[str, Any]:
    """Keep repairs diagnostic-local instead of resending the full statement."""

    compact = _compact_audit_contract(contract, kind=kind)
    # Repairs still need the bounded statement quotes that justify the facts;
    # audits do not.  Add those quotes here instead of making every audit pay
    # for them through the shared compact projection.
    evidence = contract.get("evidence")
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)):
        compact["evidence"] = [
            {
                key: item[key]
                for key in ("id", "quote", "supports")
                if key in item
            }
            for item in evidence[:16]
            if isinstance(item, Mapping)
        ]
    return compact


def _contract_text(value: Any) -> str:
    """Keep model-produced structured profile fields stable and unambiguous."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return str(value or "").strip()


def _generator_requirements(value: Any) -> list[str]:
    supplied = value if isinstance(value, list) else []
    result: list[str] = []
    seen: set[str] = set()
    for item in (*_BASE_GENERATOR_REQUIREMENTS, *supplied):
        text = _contract_text(item)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= 20:
            break
    return result


def _contract_error(
    message: str,
    *,
    path: str = "",
    details: Mapping[str, Any] | None = None,
) -> StressPreparationError:
    payload = dict(details or {})
    if path:
        payload["path"] = path
    return StressPreparationError(
        "invalid_stress_contract",
        message,
        details=payload or None,
    )


def _contract_identifier(value: Any, *, path: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 120 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", text):
        raise _contract_error(
            f"contract {path} 必须是稳定的 ASCII identifier", path=path
        )
    return text


def _contract_string_list(
    value: Any, *, path: str, allow_empty: bool = True, limit: int = 64
) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise _contract_error(f"contract {path} 必须是字符串数组", path=path)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = str(item).strip() if isinstance(item, str) else ""
        if not text or len(text) > 240 or text in seen:
            raise _contract_error(
                f"contract {path} 含空值、重复值或超长值",
                path=f"{path}[{index}]",
            )
        seen.add(text)
        result.append(text)
    if not allow_empty and not result:
        raise _contract_error(f"contract {path} 不能为空", path=path)
    return result


def _contract_json_object(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _contract_error(
            f"contract {path} 必须是 JSON 对象",
            path=path,
            details={
                "actual_type": type(value).__name__,
                "actual": str(value)[:240],
                "expected": {"name": "field_name", "type": "int"}
                if ".fields[" in path
                else "JSON object",
            },
        )
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raise _contract_error(
            f"contract {path} 必须只包含 JSON 值", path=path
        ) from None
    if len(encoded.encode("utf-8")) > 32 * 1024:
        raise _contract_error(f"contract {path} 超过大小上限", path=path)
    return dict(value)


def _normalize_contract_field(value: Any, *, path: str) -> dict[str, Any]:
    field = _contract_json_object(value, path=path)
    allowed = {
        "name",
        "type",
        "minimum",
        "maximum",
        "count",
        "count_from",
        "description",
        # Some providers attach a prose provenance label to a syntax field.
        # Evidence ids carry the trusted provenance; a bounded string here has
        # no executable semantics and can be projected away safely.
        "source",
    }
    unexpected = set(field) - allowed
    if unexpected:
        raise _contract_error(
            f"contract {path} 含未知字段：{sorted(unexpected)}", path=path
        )
    name = _contract_identifier(field.get("name"), path=f"{path}.name")
    source = field.get("source")
    if source is not None and (
        not isinstance(source, str) or len(source.encode("utf-8")) > 1000
    ):
        raise _contract_error(
            f"contract {path}.source 必须是有界字符串",
            path=f"{path}.source",
            details={"actual_type": type(source).__name__},
        )
    raw_field_type = str(field.get("type") or "").strip().casefold()
    alias_key = re.sub(r"[\s-]+", "_", raw_field_type)
    field_type = _CONTRACT_FIELD_TYPE_ALIASES.get(alias_key, raw_field_type)
    if field_type not in _CONTRACT_FIELD_TYPES:
        raise _contract_error(
            f"contract {path}.type 不受支持",
            path=f"{path}.type",
            details={"actual_type": raw_field_type[:120]},
        )
    normalized: dict[str, Any] = {"name": name, "type": field_type}
    count = field.get("count")
    count_from = field.get("count_from")
    if count is not None and count_from is not None and count != count_from:
        raise _contract_error(
            f"contract {path}.count/count_from 冲突", path=f"{path}.count_from"
        )
    selected_count = count_from if count_from is not None else count
    if selected_count is not None:
        if not isinstance(selected_count, (int, str)) or isinstance(
            selected_count, bool
        ):
            raise _contract_error(
                f"contract {path}.count_from 必须是整数或字段引用",
                path=f"{path}.count_from",
            )
        normalized["count_from"] = selected_count
    for key in ("minimum", "maximum"):
        if key in field:
            bound = field[key]
            if not isinstance(bound, (int, float, str)) or isinstance(bound, bool):
                raise _contract_error(
                    f"contract {path}.{key} 必须是数值或依赖表达式",
                    path=f"{path}.{key}",
                )
            normalized[key] = bound
    description = str(field.get("description") or "").strip()
    if description:
        normalized["description"] = description[:1000]
    return normalized


def _operation_evidence_signature(
    evidence: Sequence[Mapping[str, Any]], tag: str
) -> list[str]:
    for item in evidence:
        quote = str(item.get("quote") or "")
        for segment in re.findall(r"`([^`\r\n]+)`", quote):
            tokens = segment.strip().split()
            if not tokens or tokens[0].casefold() != tag.casefold():
                continue
            fields = [
                token
                for token in tokens[1:]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token)
            ]
            if len(fields) == len(tokens) - 1:
                return fields
    return []


def _operation_field_is_numeric(
    constraints: Sequence[Mapping[str, Any]], tag: str, name: str
) -> bool:
    numeric_kinds = {
        "range",
        "count_equals",
        "length_equals",
        "sum_limit",
        "dependent_bound",
        "state_precondition",
    }
    allowed_targets = {
        f"operations.*.{name}".casefold(),
        f"operations.{tag}.{name}".casefold(),
    }
    return any(
        str(item.get("kind") or "").casefold() in numeric_kinds
        and str(item.get("target") or "").casefold() in allowed_targets
        for item in constraints
    )


def _normalize_operation_contract_field(
    value: Any,
    *,
    path: str,
    tag: str,
    index: int,
    signature: Sequence[str],
    constraints: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _normalize_contract_field(value, path=path)
    if not isinstance(value, str):
        return _normalize_contract_field(value, path=path)
    shorthand = value.strip()
    explicit = re.fullmatch(
        r"(?:(int|float|string|token|char)\s+([A-Za-z_][A-Za-z0-9_]*)|"
        r"([A-Za-z_][A-Za-z0-9_]*):(int|float|string|token|char))",
        shorthand,
        flags=re.IGNORECASE,
    )
    if explicit:
        field_type = str(explicit.group(1) or explicit.group(4)).casefold()
        name = str(explicit.group(2) or explicit.group(3))
    else:
        name = _contract_identifier(shorthand, path=f"{path}.name")
        if re.fullmatch(r"arg[0-9]+", name, flags=re.IGNORECASE) and index < len(
            signature
        ):
            name = signature[index]
        field_type = (
            "int"
            if _operation_field_is_numeric(constraints, tag, name)
            else "token"
        )
    return _normalize_contract_field(
        {"name": name, "type": field_type}, path=path
    )


def _normalize_contract_syntax(
    value: Any,
    *,
    evidence: Sequence[Mapping[str, Any]] = (),
    constraints: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    syntax = _contract_json_object(value, path="syntax")
    mode = str(syntax.get("mode") or "").strip().casefold()
    if mode not in {"single_case", "multi_case", "until_eof"}:
        raise _contract_error("contract syntax.mode 不受支持", path="syntax.mode")
    eof = str(syntax.get("eof") or "required").strip().casefold()
    if eof not in {"required", "allowed"}:
        raise _contract_error("contract syntax.eof 不受支持", path="syntax.eof")
    raw_sections = syntax.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections or len(raw_sections) > 64:
        raise _contract_error(
            "contract syntax.sections 必须是非空数组", path="syntax.sections"
        )
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_section in enumerate(raw_sections):
        path = f"syntax.sections[{index}]"
        section = _contract_json_object(raw_section, path=path)
        allowed = {
            "id",
            "kind",
            "count_from",
            "fields",
            "variants",
            "alphabet",
            "description",
            "evidence_ids",
        }
        unexpected = set(section) - allowed
        if unexpected:
            raise _contract_error(
                f"contract {path} 含未知字段：{sorted(unexpected)}", path=path
            )
        section_id = _contract_identifier(section.get("id"), path=f"{path}.id")
        if section_id in seen:
            raise _contract_error("contract section id 重复", path=f"{path}.id")
        seen.add(section_id)
        kind = str(section.get("kind") or "").strip().casefold()
        if kind == "line":
            # Providers commonly use ``line`` for an ordinary fixed record even
            # though contract-v3 calls that shape ``scalar``.  Canonicalize only
            # when the surrounding structure makes the intended v3 kind
            # unambiguous; repeated/token-counted lines are lists and tagged
            # lines are operation streams.  This preserves supplied semantics
            # without inventing a section or a field.
            raw_variants = section.get("variants")
            raw_fields = section.get("fields")
            has_variants = isinstance(raw_variants, list) and bool(raw_variants)
            has_repetition = section.get("count_from") is not None or any(
                isinstance(field, Mapping)
                and (field.get("count") is not None or field.get("count_from") is not None)
                for field in (raw_fields if isinstance(raw_fields, list) else [])
            )
            kind = (
                "operation_stream"
                if has_variants
                else "list"
                if has_repetition
                else "scalar"
            )
        if kind not in _CONTRACT_SECTION_KINDS:
            raise _contract_error(
                "contract section kind 不受支持",
                path=f"{path}.kind",
                details={
                    "kind": kind,
                    "allowed": sorted(_CONTRACT_SECTION_KINDS),
                },
            )
        normalized: dict[str, Any] = {"id": section_id, "kind": kind}
        if "count_from" in section and section["count_from"] is not None:
            count_from = section["count_from"]
            if not isinstance(count_from, (int, str)) or isinstance(count_from, bool):
                raise _contract_error(
                    "contract count_from 必须是整数或字段引用",
                    path=f"{path}.count_from",
                )
            normalized["count_from"] = count_from
        raw_fields = section.get("fields", [])
        if not isinstance(raw_fields, list) or len(raw_fields) > 32:
            raise _contract_error("contract fields 必须是数组", path=f"{path}.fields")
        normalized["fields"] = [
            _normalize_contract_field(item, path=f"{path}.fields[{field_index}]")
            for field_index, item in enumerate(raw_fields)
        ]
        variants = section.get("variants", [])
        if not isinstance(variants, list) or len(variants) > 32:
            raise _contract_error("contract variants 必须是数组", path=f"{path}.variants")
        normalized_variants: list[dict[str, Any]] = []
        variant_tags: set[str] = set()
        for variant_index, raw_variant in enumerate(variants):
            variant_path = f"{path}.variants[{variant_index}]"
            variant = _contract_json_object(raw_variant, path=variant_path)
            if "tag" not in variant and "name" not in variant:
                alias = variant.get("op") or variant.get("id")
                if alias is not None:
                    variant["tag"] = alias
            variant.pop("op", None)
            variant.pop("id", None)
            unexpected_variant = set(variant) - {
                "tag",
                "name",
                "fields",
                "description",
                "evidence_ids",
            }
            if unexpected_variant:
                raise _contract_error(
                    f"contract operation variant 含未知字段：{sorted(unexpected_variant)}",
                    path=variant_path,
                    details={"unexpected_fields": sorted(unexpected_variant)},
                )
            raw_tag = str(variant.get("tag") or "").strip()
            raw_name = str(variant.get("name") or "").strip()
            if raw_tag and raw_name and raw_tag != raw_name:
                raise _contract_error(
                    "contract operation variant tag/name 冲突",
                    path=variant_path,
                )
            tag = raw_tag or raw_name
            if not tag or len(tag) > 80 or tag in variant_tags:
                raise _contract_error(
                    "contract operation variant tag 为空、重复或超长",
                    path=f"{variant_path}.tag",
                )
            variant_tags.add(tag)
            variant_fields = variant.get("fields", [])
            if not isinstance(variant_fields, list) or len(variant_fields) > 16:
                raise _contract_error(
                    "contract variant fields 必须是数组",
                    path=f"{variant_path}.fields",
                )
            signature = _operation_evidence_signature(evidence, tag)
            item: dict[str, Any] = {
                "tag": tag,
                "fields": [
                    _normalize_operation_contract_field(
                        field,
                        path=f"{variant_path}.fields[{field_index}]",
                        tag=tag,
                        index=field_index,
                        signature=signature,
                        constraints=constraints,
                    )
                    for field_index, field in enumerate(variant_fields)
                ],
            }
            description = str(variant.get("description") or "").strip()
            if description:
                item["description"] = description[:1000]
            item["evidence_ids"] = _contract_string_list(
                variant.get("evidence_ids", []),
                path=f"{variant_path}.evidence_ids",
                limit=16,
            )
            normalized_variants.append(item)
        normalized["variants"] = normalized_variants
        if kind == "operation_stream" and not normalized_variants:
            raise _contract_error(
                "operation_stream 必须声明 variants", path=f"{path}.variants"
            )
        alphabet = section.get("alphabet", [])
        if alphabet:
            normalized["alphabet"] = _contract_string_list(
                alphabet, path=f"{path}.alphabet", allow_empty=False, limit=128
            )
        evidence_ids = section.get("evidence_ids", [])
        normalized["evidence_ids"] = _contract_string_list(
            evidence_ids, path=f"{path}.evidence_ids", limit=16
        )
        description = str(section.get("description") or "").strip()
        if description:
            normalized["description"] = description[:1000]
        sections.append(normalized)
    return {"mode": mode, "eof": eof, "sections": sections}


def _normalize_contract_evidence(value: Any, *, statement: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 128:
        raise _contract_error("contract evidence 必须是数组", path="evidence")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(value):
        path = f"evidence[{index}]"
        item = _contract_json_object(raw_item, path=path)
        if set(item) - {"id", "quote", "start", "end"}:
            raise _contract_error("contract evidence 含未知字段", path=path)
        evidence_id = _contract_identifier(item.get("id"), path=f"{path}.id")
        if evidence_id in seen:
            raise _contract_error("contract evidence id 重复", path=f"{path}.id")
        seen.add(evidence_id)
        quote = str(item.get("quote") or "")
        if not quote.strip() or len(quote) > 2000:
            raise _contract_error("contract evidence quote 为空或超长", path=f"{path}.quote")
        start = item.get("start")
        end = item.get("end")
        if type(start) is not int or type(end) is not int:
            start = end = -1
        supplied_binding_is_exact = (
            start >= 0
            and end == start + len(quote)
            and statement[start:end] == quote
        )
        if not supplied_binding_is_exact:
            offset = statement.find(quote)
            if offset < 0:
                def markdown_view(text: str) -> tuple[str, list[tuple[int, int]]]:
                    rendered: list[str] = []
                    source_offsets: list[tuple[int, int]] = []
                    pending_space: int | None = None
                    latex = {
                        r"\leq": "≤",
                        r"\le": "≤",
                        r"\geq": "≥",
                        r"\ge": "≥",
                        r"\times": "×",
                        r"\cdot": "·",
                        r"\neq": "≠",
                        r"\infty": "∞",
                        r"\%": "%",
                    }
                    source_index = 0
                    at_line_start = True
                    while source_index < len(text):
                        replacement = next(
                            (
                                (token, rendered_token)
                                for token, rendered_token in latex.items()
                                if text.startswith(token, source_index)
                            ),
                            None,
                        )
                        if replacement is not None:
                            token, rendered_token = replacement
                            if pending_space is not None:
                                rendered.append(" ")
                                source_offsets.append((pending_space, pending_space + 1))
                                pending_space = None
                            rendered.append(rendered_token)
                            source_offsets.append(
                                (source_index, source_index + len(token))
                            )
                            source_index += len(token)
                            at_line_start = False
                            continue
                        character = text[source_index]
                        if character in {"$", "`"}:
                            source_index += 1
                            continue
                        if character == "-" and source_index + 1 < len(text):
                            next_index = source_index + 1
                            while next_index < len(text) and text[next_index].isspace():
                                next_index += 1
                            flattened_code_bullet = (
                                source_index > 0
                                and text[source_index - 1].isspace()
                                and next_index < len(text)
                                and text[next_index] == "`"
                            )
                            if text[source_index + 1].isspace() and (
                                at_line_start or flattened_code_bullet
                            ):
                                source_index += 1
                                continue
                        if character.isspace():
                            if rendered and pending_space is None:
                                pending_space = source_index
                            if character in {"\r", "\n"}:
                                at_line_start = True
                            source_index += 1
                            continue
                        if pending_space is not None:
                            rendered.append(" ")
                            source_offsets.append((pending_space, pending_space + 1))
                            pending_space = None
                        rendered.append(character)
                        source_offsets.append((source_index, source_index + 1))
                        source_index += 1
                        at_line_start = False
                    return "".join(rendered), source_offsets

                statement_view, offsets = markdown_view(statement)
                quote_view, _ = markdown_view(quote)
                # Models often flatten markdown bullets into mid-line "- "
                # dashes that the statement view strips at line starts.  Bind
                # a second, equally verbatim candidate with those dashes
                # removed before falling back to clause anchors.
                stripped_quote_view = re.sub(
                    r"(^|\.\s+)-\s+", r"\1", quote_view
                )
                quote_variants = [quote_view]
                if stripped_quote_view and stripped_quote_view != quote_view:
                    quote_variants.append(stripped_quote_view)
                bound_view: str | None = None
                normalized_offset = -1
                for candidate in quote_variants:
                    candidate_offset = statement_view.find(candidate)
                    repeated = (
                        statement_view.find(candidate, candidate_offset + 1)
                        if candidate_offset >= 0 and candidate
                        else -1
                    )
                    if candidate_offset >= 0 and repeated < 0:
                        bound_view = candidate
                        normalized_offset = candidate_offset
                        break
                if bound_view is None:
                    fallback_view = stripped_quote_view or quote_view
                    clauses = [
                        clause.strip().rstrip(".")
                        for clause in re.split(r"\.\s+", fallback_view)
                        if clause.strip().rstrip(".")
                    ]
                    positions: list[tuple[int, int]] = []
                    cursor = 0
                    for clause in clauses:
                        # Numeric boundary clauses such as
                        # ``3 <= n,m <= 8e4`` are often shorter than prose but
                        # remain strong when followed by another ordered exact
                        # source clause inside the bounded span.
                        if len(clause) < 12:
                            positions = []
                            break
                        clause_start = statement_view.find(clause, cursor)
                        if clause_start < 0 or (
                            positions and clause_start - positions[-1][1] > 500
                        ):
                            positions = []
                            break
                        clause_end = clause_start + len(clause)
                        positions.append((clause_start, clause_end))
                        cursor = clause_end
                    if len(positions) < 2 and re.search(r"\.{3,}|…", fallback_view):
                        positions = []
                        cursor = 0
                        anchors = [
                            anchor.strip().strip(".,;:")
                            for anchor in re.split(r"\.{3,}|…", fallback_view)
                            if anchor.strip().strip(".,;:")
                        ]
                        for anchor in anchors:
                            if len(anchor) < 12:
                                positions = []
                                break
                            anchor_start = statement_view.find(anchor, cursor)
                            if anchor_start < 0 or (
                                positions and anchor_start - positions[-1][1] > 800
                            ):
                                positions = []
                                break
                            anchor_end = anchor_start + len(anchor)
                            positions.append((anchor_start, anchor_end))
                            cursor = anchor_end
                    if len(positions) < 2 and "," in fallback_view:
                        # Models sometimes summarize a grammar as a comma list
                        # of exact operation signatures while the statement
                        # documents those signatures in consecutive bullets.
                        # Four ordered anchors are strong enough to bind the
                        # whole source span without accepting a fabricated
                        # single phrase or numeric constraint.
                        positions = []
                        cursor = 0
                        anchors = [
                            anchor.strip().strip(".,;:")
                            for anchor in re.split(r",|\band\b", fallback_view)
                            if anchor.strip().strip(".,;:")
                        ]
                        if len(anchors) >= 4:
                            for anchor in anchors:
                                if len(anchor) < 5:
                                    positions = []
                                    break
                                anchor_start = statement_view.find(anchor, cursor)
                                if anchor_start < 0 or (
                                    positions
                                    and anchor_start - positions[-1][1] > 800
                                ):
                                    positions = []
                                    break
                                anchor_end = anchor_start + len(anchor)
                                positions.append((anchor_start, anchor_end))
                                cursor = anchor_end
                    if (
                        len(positions) < 2
                        or positions[-1][1] - positions[0][0] > 4000
                    ):
                        raise _contract_error(
                            "contract evidence 无法精确绑定当前题面",
                            path=path,
                            details={"quote": quote[:500]},
                        )
                    normalized_offset = positions[0][0]
                    normalized_end = positions[-1][1] - 1
                    if (
                        fallback_view.rstrip().endswith(".")
                        and positions[-1][1] < len(statement_view)
                        and statement_view[positions[-1][1]] == "."
                    ):
                        normalized_end = positions[-1][1]
                else:
                    normalized_end = normalized_offset + len(bound_view) - 1
                start = offsets[normalized_offset][0]
                end = offsets[normalized_end][1]
                quote = statement[start:end]
                supplied_binding_is_exact = True
            else:
                # Identical quote occurrences carry identical evidence text.
                # The location is redundant metadata, so canonicalize an invalid model
                # offset to the first exact occurrence instead of spending a model
                # repair or inventing a different quote.
                start = offset
                end = start + len(quote)
        result.append({"id": evidence_id, "quote": quote, "start": start, "end": end})
    return result


def _normalize_contract_constraints(
    value: Any, *, evidence_ids: set[str]
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 128:
        raise _contract_error(
            "contract constraints 必须是非空数组", path="constraints"
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(value):
        path = f"constraints[{index}]"
        item = _contract_json_object(raw_item, path=path)
        if set(item) - {"id", "kind", "target", "args", "evidence_ids"}:
            raise _contract_error("contract constraint 含未知字段", path=path)
        constraint_id = _contract_identifier(item.get("id"), path=f"{path}.id")
        if constraint_id in seen:
            raise _contract_error("contract constraint id 重复", path=f"{path}.id")
        seen.add(constraint_id)
        kind = str(item.get("kind") or "").strip().casefold()
        if kind not in _CONTRACT_CONSTRAINT_KINDS:
            raise _contract_error("contract constraint kind 不受支持", path=f"{path}.kind")
        target = str(item.get("target") or "").strip()
        if not target or len(target) > 240:
            raise _contract_error("contract constraint target 为空或超长", path=f"{path}.target")
        args = _contract_json_object(item.get("args", {}), path=f"{path}.args")
        refs = _contract_string_list(
            item.get("evidence_ids", []),
            path=f"{path}.evidence_ids",
            allow_empty=False,
            limit=16,
        )
        missing = set(refs) - evidence_ids
        if missing:
            raise _contract_error(
                f"contract constraint 引用了不存在的 evidence：{sorted(missing)}",
                path=f"{path}.evidence_ids",
            )
        result.append(
            {
                "id": constraint_id,
                "kind": kind,
                "target": target,
                "args": args,
                "evidence_ids": refs,
            }
        )
    return result


def _normalize_validator_probes(
    value: Any,
    *,
    constraints: Sequence[Mapping[str, Any]],
    evidence_ids: set[str],
    require_complete: bool = True,
) -> list[dict[str, Any]]:
    """Validate independent positive/negative inputs for semantic validator gates.

    These probes are produced by the contract branch, never shown to the
    validator branch, and executed only by the trusted harness.  Requiring the
    pair to differ in at most two tokens prevents a syntactically unrelated
    invalid input from masquerading as evidence for a dynamic precondition.
    """

    if value is None:
        value = []
    if not isinstance(value, list) or len(value) > 6:
        raise _contract_error(
            "contract validator_probes 必须是最多 6 项的数组",
            path="validator_probes",
        )
    constraints_by_id = {
        str(item.get("id") or ""): item
        for item in constraints
        if isinstance(item, Mapping)
    }
    dynamic_ids = {
        constraint_id
        for constraint_id, item in constraints_by_id.items()
        if str(item.get("kind") or "")
        in {"state_precondition", "dependent_bound", "graph_predicate"}
    }
    dynamic_constraint_kinds = {
        constraint_id: str(constraints_by_id[constraint_id].get("kind") or "")
        for constraint_id in sorted(dynamic_ids)
    }
    statically_checkable_kinds = {
        "range",
        "count_equals",
        "length_equals",
        "sum_limit",
        "unique",
        "permutation",
    }

    def dynamic_diagnostic(**extra: Any) -> dict[str, Any]:
        return {
            "allowed_dynamic_constraint_ids": sorted(dynamic_ids),
            "dynamic_constraint_kinds": dynamic_constraint_kinds,
            **extra,
        }

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    covered: set[str] = set()
    for index, raw_item in enumerate(value):
        path = f"validator_probes[{index}]"
        item = _contract_json_object(raw_item, path=path)
        allowed = {
            "id",
            "constraint_id",
            "valid_input",
            "invalid_input",
            "evidence_ids",
            "description",
        }
        unexpected = set(item) - allowed
        if unexpected:
            raise _contract_error(
                f"contract {path} 含未知字段：{sorted(unexpected)}", path=path
            )
        constraint_id = _contract_identifier(
            item.get("constraint_id"), path=f"{path}.constraint_id"
        )
        constraint = constraints_by_id.get(constraint_id)
        constraint_kind = (
            str(constraint.get("kind") or "")
            if isinstance(constraint, Mapping)
            else ""
        )
        if constraint_kind in statically_checkable_kinds:
            # Range/count/set-shape constraints are certified directly from the
            # parsed contract.  A provider sometimes emits placeholder probes
            # for them even though probes are reserved for dynamic semantics;
            # discard those redundant rows without weakening the mandatory
            # coverage check for every dynamic constraint below.
            continue
        if constraint_id not in dynamic_ids:
            raise _contract_error(
                "validator probe 只能绑定动态前置条件或依赖约束",
                path=f"{path}.constraint_id",
                details=dynamic_diagnostic(
                    constraint_id=constraint_id,
                    constraint_kind=constraint_kind or None,
                ),
            )
        probe_id = _contract_identifier(item.get("id"), path=f"{path}.id")
        if probe_id in seen:
            raise _contract_error("contract validator probe id 重复", path=f"{path}.id")
        seen.add(probe_id)
        constraint = constraints_by_id[constraint_id]
        raw_refs = item.get("evidence_ids")
        refs = (
            _contract_string_list(raw_refs, path=f"{path}.evidence_ids", limit=8)
            if raw_refs is not None
            else [str(ref) for ref in constraint.get("evidence_ids", [])]
        )
        if not refs or not set(refs).issubset(evidence_ids):
            raise _contract_error(
                "validator probe 必须引用存在的题面 evidence",
                path=f"{path}.evidence_ids",
            )
        inputs: dict[str, str] = {}
        for key in ("valid_input", "invalid_input"):
            raw = item.get(key)
            if not isinstance(raw, str):
                raise _contract_error(
                    f"contract {path}.{key} 必须是字符串", path=f"{path}.{key}"
                )
            normalized_input = raw.strip() + "\n"
            encoded = normalized_input.encode("utf-8")
            if not normalized_input.strip() or len(encoded) > 8192 or b"\0" in encoded:
                raise _contract_error(
                    f"contract {path}.{key} 为空或超过 8 KiB",
                    path=f"{path}.{key}",
                )
            inputs[key] = normalized_input
        valid_tokens = inputs["valid_input"].split()
        invalid_tokens = inputs["invalid_input"].split()
        differences = (
            sum(left != right for left, right in zip(valid_tokens, invalid_tokens))
            if len(valid_tokens) == len(invalid_tokens)
            else 999
        )
        if not (3 <= len(valid_tokens) <= 512 and 1 <= differences <= 2):
            if differences == 0 and 3 <= len(valid_tokens) <= 512:
                # Degenerate pair: valid_input == invalid_input.  It tests
                # nothing and cannot be certified, so drop it exactly as the
                # independent branch is instructed to delete unprovable pairs.
                # Constraints left without any probe still fail closed through
                # the per-constraint coverage check below.
                continue
            raise _contract_error(
                "validator probe 的正负输入必须 token 数相同且只差 1 到 2 个 token",
                path=path,
                details={
                    "valid_tokens": len(valid_tokens),
                    "invalid_tokens": len(invalid_tokens),
                    "different_tokens": differences,
                },
            )
        description = str(item.get("description") or "").strip()
        normalized = {
            "id": probe_id,
            "constraint_id": constraint_id,
            **inputs,
            "evidence_ids": refs,
        }
        if description:
            normalized["description"] = description[:500]
        result.append(normalized)
        covered.add(constraint_id)
    missing = sorted(dynamic_ids - covered)
    if missing and require_complete:
        raise _contract_error(
            "每个动态前置条件或依赖约束都需要独立正负 validator probe",
            path="validator_probes",
            details=dynamic_diagnostic(missing_constraint_ids=missing),
        )
    return result


def _normalize_contract_coverage(
    value: Any, *, evidence_ids: set[str]
) -> list[dict[str, Any]]:
    # Model-supplied coverage is optional by contract.  Standard, computable
    # obligations are derived locally from validated syntax/constraints below.
    # Consequently an unverifiable custom hint must be ignored instead of
    # making an otherwise sound input contract fail or inventing semantics.
    if not isinstance(value, list):
        return []
    value = value[:128]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(value):
        path = f"coverage_obligations[{index}]"
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        if "id" not in item and "name" in item:
            item["id"] = item.get("name")
        item = {
            key: item[key]
            for key in ("id", "scope", "predicate", "minimum_witnesses", "evidence_ids")
            if key in item
        }
        raw_obligation_id = str(item.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", raw_obligation_id):
            raw_obligation_id = f"custom_coverage_{index + 1}"
        obligation_id = _contract_identifier(
            raw_obligation_id, path=f"{path}.id"
        )
        if obligation_id in seen:
            obligation_id = f"custom_coverage_{index + 1}"
            if obligation_id in seen:
                continue
        seen.add(obligation_id)
        scope = str(item.get("scope") or "").strip().casefold()
        if scope not in {"small", "large", "all"}:
            if "small" in scope and "large" in scope or scope in {"both", "global"}:
                scope = "all"
            elif "small" in scope:
                scope = "small"
            elif "large" in scope:
                scope = "large"
        if scope not in {"small", "large", "all"}:
            continue
        raw_predicate = item.get("predicate")
        if not isinstance(raw_predicate, Mapping):
            continue
        predicate = dict(raw_predicate)
        if "kind" not in predicate and "type" in predicate:
            predicate["kind"] = predicate.get("type")
        predicate = {
            key: predicate[key]
            for key in ("kind", "target", "args")
            if key in predicate
        }
        predicate_kind = str(predicate.get("kind") or "").strip().casefold()
        if predicate_kind not in _CONTRACT_COVERAGE_PREDICATES:
            continue
        target = str(predicate.get("target") or "").strip()
        if not target or len(target) > 240:
            continue
        raw_args = predicate.get("args", {})
        if not isinstance(raw_args, Mapping):
            continue
        args = dict(raw_args)
        minimum = item.get("minimum_witnesses", 1)
        if type(minimum) is not int or not 1 <= minimum <= 64:
            continue
        raw_refs = item.get("evidence_ids", [])
        if not isinstance(raw_refs, list):
            continue
        refs = [str(ref).strip() for ref in raw_refs[:16] if str(ref).strip()]
        if set(refs) - evidence_ids:
            continue
        result.append(
            {
                "id": obligation_id,
                "scope": scope,
                "predicate": {
                    "kind": predicate_kind,
                    "target": target,
                    "args": args,
                },
                "minimum_witnesses": minimum,
                "evidence_ids": refs,
            }
        )
    return result


def _derive_contract_coverage(
    syntax: Mapping[str, Any],
    constraints: Sequence[Mapping[str, Any]],
    supplied: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Add deterministic coverage obligations from validated contract facts."""

    result = [dict(item) for item in supplied]
    seen_ids = {str(item.get("id") or "") for item in result}
    seen_predicates = {
        json.dumps(item.get("predicate"), sort_keys=True, separators=(",", ":"))
        for item in result
    }

    def stable_id(*parts: object) -> str:
        body = "_".join(
            re.sub(r"[^A-Za-z0-9_]+", "_", str(part)).strip("_").casefold()
            for part in parts
        ).strip("_")
        candidate = ("auto_" + body)[:110] or "auto_coverage"
        base = candidate
        suffix = 2
        while candidate in seen_ids:
            candidate = f"{base[:104]}_{suffix}"
            suffix += 1
        return candidate

    def add(
        parts: tuple[object, ...],
        *,
        scope: str,
        kind: str,
        target: str,
        args: Mapping[str, Any],
        evidence_ids: Sequence[str],
    ) -> None:
        predicate = {"kind": kind, "target": target, "args": dict(args)}
        key = json.dumps(predicate, sort_keys=True, separators=(",", ":"))
        if key in seen_predicates:
            return
        obligation_id = stable_id(*parts)
        seen_ids.add(obligation_id)
        seen_predicates.add(key)
        result.append(
            {
                "id": obligation_id,
                "scope": scope,
                "predicate": predicate,
                "minimum_witnesses": 1,
                "evidence_ids": list(dict.fromkeys(str(item) for item in evidence_ids if item)),
            }
        )

    for constraint in constraints:
        if str(constraint.get("kind") or "") != "range":
            continue
        constraint_id = str(constraint.get("id") or "")
        args = constraint.get("args")
        args = dict(args) if isinstance(args, Mapping) else {}
        refs = constraint.get("evidence_ids")
        refs = list(refs) if isinstance(refs, list) else []
        if "minimum" in args:
            add(
                (constraint_id, "minimum"),
                scope="small",
                kind="constraint_boundary",
                target=constraint_id,
                args={"side": "minimum"},
                evidence_ids=refs,
            )
        if "maximum" in args:
            add(
                (constraint_id, "maximum"),
                scope="large",
                kind="constraint_boundary",
                target=constraint_id,
                args={"side": "maximum"},
                evidence_ids=refs,
            )
    for section in syntax.get("sections", []):
        if not isinstance(section, Mapping):
            continue
        section_refs = section.get("evidence_ids")
        section_refs = list(section_refs) if isinstance(section_refs, list) else []
        for variant in section.get("variants", []):
            if not isinstance(variant, Mapping):
                continue
            tag = str(variant.get("tag") or "")
            refs = variant.get("evidence_ids")
            refs = list(refs) if isinstance(refs, list) else section_refs
            add(
                ("operation", tag),
                scope="small",
                kind="operation_variant",
                target=tag,
                args={},
                evidence_ids=refs,
            )
            for field in variant.get("fields", []):
                if not isinstance(field, Mapping):
                    continue
                name = str(field.get("name") or "")
                minimum = field.get("minimum")
                maximum = field.get("maximum")
                values: list[tuple[str, Any]] = []
                if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
                    values.append(("minimum", minimum))
                if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
                    values.append(("maximum", maximum))
                if (
                    isinstance(minimum, (int, float))
                    and isinstance(maximum, (int, float))
                    and not isinstance(minimum, bool)
                    and not isinstance(maximum, bool)
                    and minimum < 0 < maximum
                ):
                    values.append(("zero", 0))
                for label, value in values:
                    add(
                        (tag, name, label),
                        scope="small",
                        kind="value_class",
                        target=f"{tag}.{name}",
                        args={"value": value},
                        evidence_ids=refs,
                    )
    return result


def normalize_stress_contract(
    value: Any,
    *,
    compare: str | None = None,
    statement: str = "",
    require_complete_probes: bool = True,
) -> dict[str, Any]:
    """Return canonical contract-v3 while accepting cached legacy contracts.

    Legacy free-text contracts remain executable, but are explicitly marked as
    ``legacy_text`` so a future validator adapter cannot mistake them for a
    machine-verifiable input specification.
    """

    if not isinstance(value, Mapping):
        raise _contract_error("对拍契约必须是 JSON 对象")
    data = dict(value)
    required_text = (
        "input_summary",
        "small_profile",
        "small_lower_boundary",
        "large_profile",
        "large_upper_boundary",
    )
    if any(not _contract_text(data.get(key)) for key in required_text):
        raise _contract_error("对拍契约缺少必要字段")
    requested_compare = str(compare if compare is not None else data.get("output_compare") or "").casefold()
    if requested_compare not in {"token", "exact"}:
        raise _contract_error("输出比较方式无效", path="output_compare")
    requirements = _generator_requirements(data.get("generator_requirements"))
    structured_keys = {
        "syntax",
        "constraints",
        "evidence",
        "coverage_obligations",
        "validator_probes",
    }
    has_structured = any(key in data for key in structured_keys)
    canonical_legacy = (
        data.get("validation_level") == "legacy_text"
        or (
            isinstance(data.get("syntax"), Mapping)
            and data["syntax"].get("mode") == "legacy_text"
        )
    )
    if canonical_legacy:
        has_structured = False
    if not has_structured and data.get("schema_version") not in {None, 2}:
        if data.get("schema_version") != CONTRACT_SCHEMA_VERSION or not canonical_legacy:
            raise _contract_error("未知 contract schema_version", path="schema_version")
    if not has_structured:
        return {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "source_schema_version": int(
                data.get("source_schema_version")
                if canonical_legacy
                else data.get("schema_version") or 2
            ),
            "validation_level": "legacy_text",
            "profile_version": 2,
            "input_summary": _contract_text(data["input_summary"]),
            "small_profile": _contract_text(data["small_profile"]),
            "small_lower_boundary": _contract_text(data["small_lower_boundary"]),
            "large_profile": _contract_text(data["large_profile"]),
            "large_upper_boundary": _contract_text(data["large_upper_boundary"]),
            "output_compare": requested_compare,
            "generator_requirements": requirements,
            "syntax": {
                "mode": "legacy_text",
                "eof": "required",
                "summary": _contract_text(data["input_summary"]),
                "sections": [],
            },
            "constraints": [],
            "evidence": [],
            "coverage_obligations": [],
            "validator_probes": [],
        }
    if data.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise _contract_error(
            "结构化 contract schema_version 必须为 3", path="schema_version"
        )
    evidence = _normalize_contract_evidence(data.get("evidence"), statement=statement)
    evidence_ids = {str(item["id"]) for item in evidence}
    constraints = _normalize_contract_constraints(
        data.get("constraints"), evidence_ids=evidence_ids
    )
    validator_probes = (
        _normalize_validator_probes(
            data.get("validator_probes"),
            constraints=constraints,
            evidence_ids=evidence_ids,
            require_complete=True,
        )
        if require_complete_probes
        else []
    )
    syntax = _normalize_contract_syntax(
        data.get("syntax"), evidence=evidence, constraints=constraints
    )
    section_refs = {
        ref for section in syntax["sections"] for ref in section.get("evidence_ids", [])
    }
    section_refs.update(
        ref
        for section in syntax["sections"]
        for variant in section.get("variants", [])
        for ref in variant.get("evidence_ids", [])
    )
    missing_section_refs = section_refs - evidence_ids
    if missing_section_refs:
        raise _contract_error(
            f"contract syntax 引用了不存在的 evidence：{sorted(missing_section_refs)}",
            path="syntax.sections",
        )
    coverage = _normalize_contract_coverage(
        data.get("coverage_obligations", []), evidence_ids=evidence_ids
    )
    coverage = _derive_contract_coverage(syntax, constraints, coverage)
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "source_schema_version": CONTRACT_SCHEMA_VERSION,
        "validation_level": "structured",
        "profile_version": 2,
        "input_summary": _contract_text(data["input_summary"]),
        "small_profile": _contract_text(data["small_profile"]),
        "small_lower_boundary": _contract_text(data["small_lower_boundary"]),
        "large_profile": _contract_text(data["large_profile"]),
        "large_upper_boundary": _contract_text(data["large_upper_boundary"]),
        "output_compare": requested_compare,
        "generator_requirements": requirements,
        "syntax": syntax,
        "constraints": constraints,
        "evidence": evidence,
        "coverage_obligations": coverage,
        "validator_probes": validator_probes,
    }
