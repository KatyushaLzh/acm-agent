"""Safe Chat Completions conformance probes and redacted evidence reports."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
import time
from copy import deepcopy
from typing import Any, Mapping, Sequence

from .provider import ProviderConfigurationError, ProviderError, ProviderPort
from .provider_config import default_ai_policy, validate_model_id, validate_reasoning_strength
from .provider_registry import ProviderRoute, provider_definition_hash, model_adapter
from .provider_compatibility import compatibility_adjustment
from .provider_output_limits import next_output_token_limit
from .usage import merge_usage


CONFORMANCE_VERSION = 4
_EVIDENCE_ISSUER = object()


class _TrustedConformanceReport(dict[str, Any]):
    """Dict-compatible report carrying a process-local, non-serializable trust mark."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        super().__init__(value)
        self._issuer = _EVIDENCE_ISSUER


def _usage_summary(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
        "cache_write_tokens", "cache_miss_tokens", "reasoning_tokens",
        "provider_requests", "protocol_repairs", "latency_ms",
    }
    return {key: value[key] for key in sorted(allowed & set(value))}


class _OutputLimitRetry(Exception):
    def __init__(self, limit: int, usage: Mapping[str, Any]) -> None:
        self.limit = limit
        self.usage = dict(usage)


class _TransientRetry(Exception):
    def __init__(self, usage: Mapping[str, Any]) -> None:
        self.usage = dict(usage)


class _CompatibilityRetry(Exception):
    def __init__(self, update: dict[str, Any], usage: Mapping[str, Any]) -> None:
        self.update = update
        self.usage = dict(usage)


_ERROR_HINTS = {
    "server_error": "中转站或供应商 HTTP 服务失败，请稍后重试或检查上游服务状态。",
    "network_error": "无法连接中转站或供应商，请检查网络、代理与服务地址。",
    "timeout": "中转站或供应商响应超时，请稍后重试或检查服务状态。",
    "rate_limited": "中转站或供应商限流，请稍后重试。",
    "invalid_stream": "供应商返回了无效的流式响应，请检查中转站的 Chat Completions 流式协议兼容性。",
    "incomplete_stream": "供应商的流式响应未完成，请检查中转站或上游连接是否中断。",
    "response_incomplete": "供应商响应未完成，请检查输出预算或上游服务状态。",
    "authentication_failed": "供应商鉴权失败，请检查当前连接的凭据。",
    "permission_denied": "供应商拒绝访问，请检查当前连接的模型权限。",
    "insufficient_balance": "供应商账户额度不足，请检查当前连接的可用额度。",
    "invalid_request": "供应商拒绝了请求参数，请检查该模型的协议与参数兼容性。",
}


def _run_live_conformance(
    client: ProviderPort,
    route: ProviderRoute,
    *,
    required_capabilities: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Negotiate rejected output limits, then prove the normal wire contract.

    Unknown compatible models bootstrap at the normal task default, not another
    model's arbitrarily large saved budget. Success establishes a conservative
    operational ceiling, never a claim about the vendor's physical maximum.
    """
    current = route
    bootstrap = None
    if model_adapter(route.provider, route.model) in {"openai_compatible", "openai_responses", "anthropic"} and route.capabilities.max_output_tokens is None:
        probe_limit = min(
            int(route.budget["max_output_tokens"]),
            int(default_ai_policy()["budgets"][route.profile_id]["max_output_tokens"]),
        )
        bootstrap = {"requested_max_tokens": int(route.budget["max_output_tokens"]), "probe_max_tokens": probe_limit}
        current = replace(route, budget={**route.budget, "max_output_tokens": probe_limit})
    negotiations: list[dict[str, int]] = []
    rejected_usage: dict[str, Any] = {}
    transient_retries = 0
    max_transient_retries = min(2, max(0, int(route.budget.get("max_retries", 0))))
    deadline = time.monotonic() + float(route.budget["request_timeout_seconds"])
    successful_cases: dict[str, Any] = {}
    compatibility_changes: list[dict[str, Any]] = []
    for attempt in range(19 + max_transient_retries):
        remaining_tokens = int(route.budget["max_total_tokens"]) - int(rejected_usage.get("total_tokens", 0))
        if remaining_tokens <= 0:
            raise ProviderError("budget_exceeded", "模型验证 Token 预算已用尽。", usage=rejected_usage)
        current = replace(current, budget={**current.budget, "max_total_tokens": remaining_tokens})
        original_timeout = getattr(client, "timeout", None)
        if attempt:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("timeout", "模型能力验证超时。", usage=rejected_usage)
            current = replace(current, budget={**current.budget, "request_timeout_seconds": remaining})
            # stream_chat reads the adapter timeout rather than a call keyword.
            if isinstance(original_timeout, (int, float)):
                client.timeout = min(original_timeout, remaining)
        try:
            report = _run_conformance_once(
                client, current, required_capabilities=required_capabilities,
                negotiate_output_limit=len(negotiations) < 10,
                retry_transient=transient_retries < max_transient_retries,
                deadline=deadline,
                successful_cases=successful_cases,
            )
        except _CompatibilityRetry as retry:
            merge_usage(rejected_usage, retry.usage)
            if retry.update in compatibility_changes:
                raise ProviderError("compatibility_exhausted", "兼容参数协商未能收敛。", usage=rejected_usage)
            compatibility_changes.append(retry.update)
            client.wire_profile.update(retry.update)
            continue
        except _TransientRetry as retry:
            successful_cases.clear()
            merge_usage(rejected_usage, retry.usage)
            transient_retries += 1
            continue
        except _OutputLimitRetry as retry:
            successful_cases.clear()
            merge_usage(rejected_usage, retry.usage)
            negotiations.append({
                "requested_max_tokens": int(current.budget["max_output_tokens"]),
                "retry_max_tokens": retry.limit,
            })
            current = replace(current, budget={**current.budget, "max_output_tokens": retry.limit})
            continue
        finally:
            if attempt and isinstance(original_timeout, (int, float)):
                client.timeout = original_timeout
        if negotiations:
            report["output_limit_negotiation"] = negotiations
            if report["passed"]:
                report["negotiated_max_output_tokens"] = int(current.budget["max_output_tokens"])
        if bootstrap is not None:
            report["output_budget_bootstrap"] = bootstrap
            if report["passed"]:
                report["verified_max_output_tokens"] = int(current.budget["max_output_tokens"])
        if transient_retries:
            report["transient_retries"] = transient_retries
        if hasattr(client, "wire_profile"):
            report["negotiated_wire_profile"] = dict(client.wire_profile)
        report["compatibility_changes"] = compatibility_changes
        merge_usage(rejected_usage, report["usage"])
        report["usage"] = rejected_usage
        if rejected_usage.get("total_tokens", 0) > int(route.budget["max_total_tokens"]):
            raise ProviderError("budget_exceeded", "模型验证 Token 预算已用尽。", usage=rejected_usage)
        return report
    raise AssertionError("conformance retry loop must terminate")


def run_live_conformance(client: ProviderPort, route: ProviderRoute, *,
                         required_capabilities: Sequence[str] | None = None) -> dict[str, Any]:
    """All physical requests, including negotiation, share one deadline and cap."""
    original = getattr(client, "_transport", None)
    deadline = time.monotonic() + float(route.budget["request_timeout_seconds"])
    attempts = 0
    def transport(request: Any, timeout: float) -> Any:
        nonlocal attempts
        remaining = deadline - time.monotonic()
        if attempts >= int(route.budget["max_requests"]) or remaining <= 0:
            raise ProviderError("budget_exceeded", "模型验证调用预算已用尽，请重试或调整预算。", retryable=True)
        attempts += 1
        return original(request, min(timeout, remaining))
    if callable(original):
        client._transport = transport
    try:
        report = _run_live_conformance(client, route, required_capabilities=required_capabilities)
        if callable(original):
            report["usage"]["provider_requests"] = attempts
        return report
    finally:
        if callable(original):
            client._transport = original


def _run_conformance_once(
    client: ProviderPort,
    route: ProviderRoute,
    *,
    required_capabilities: Sequence[str] | None = None,
    negotiate_output_limit: bool = False,
    retry_transient: bool = False,
    deadline: float | None = None,
    successful_cases: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run only bounded, content-free probes; never retain prompts or output."""

    supported_requirements = {
        "text_chat", "json_object", "streaming", "usage", "stream_usage",
        "usage_cache_tokens",
    }
    requested = (
        set(required_capabilities) if required_capabilities is not None else None
    )
    if requested is not None and (
        not requested or requested - supported_requirements or "text_chat" not in requested
    ):
        raise ValueError("required_capabilities contains unsupported conformance cases")
    if requested is not None:
        missing = sorted(
            name for name in requested if not bool(getattr(route.capabilities, name, False))
        )
        if missing:
            raise ProviderConfigurationError(
                "unsupported_capability",
                "provider/model does not declare required capability: " + ", ".join(missing),
            )

    successful_cases = successful_cases if successful_cases is not None else {}
    cases: list[dict[str, Any]] = [dict(item) for item in successful_cases.values()]
    observed_usage: dict[str, Any] = {}
    # Reasoning tokens share the completion budget. Reuse the task's normal
    # limits, including for auto where the provider may enable thinking.
    max_tokens = int(route.budget["max_output_tokens"])
    request_timeout = float(route.budget["request_timeout_seconds"])

    def remaining_timeout(usage: Mapping[str, Any] | None = None) -> float:
        if observed_usage.get("total_tokens", 0) >= int(route.budget["max_total_tokens"]):
            raise ProviderError("budget_exceeded", "模型验证 Token 预算已用尽。")
        remaining = request_timeout if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("timeout", "模型能力验证超时。", usage=dict(usage or {}))
        return min(request_timeout, remaining)

    def record(name: str, ok: bool, *, usage: dict[str, Any] | None = None, code: str | None = None) -> None:
        nonlocal observed_usage
        if usage:
            merge_usage(observed_usage, _usage_summary(usage))
        item: dict[str, Any] = {"name": name, "ok": bool(ok)}
        if usage:
            item["usage"] = _usage_summary(usage)
        if code:
            item["error_code"] = code
            item["error_hint"] = _ERROR_HINTS.get(code, "供应商能力验证失败，请检查协议兼容性与服务状态。")
        cases.append(item)
        if ok or name in {"usage", "stream_usage", "cache_telemetry"}:
            successful_cases[name] = dict(item)

    def record_error(name: str, exc: ProviderError) -> None:
        record(name, False, usage=dict(exc.usage), code=exc.code)
        wire = getattr(client, "wire_profile", None)
        if isinstance(wire, dict):
            update = compatibility_adjustment(exc, wire, adapter=model_adapter(route.provider, route.model), case=name)
            if update is not None:
                raise _CompatibilityRetry(update, observed_usage)
        limit = next_output_token_limit(exc, max_tokens) if negotiate_output_limit else None
        if isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit < max_tokens:
            raise _OutputLimitRetry(limit, observed_usage)
        if retry_transient and exc.retryable and (deadline is None or time.monotonic() < deadline) and exc.code in {
            "server_error", "network_error", "timeout", "rate_limited",
        }:
            raise _TransientRetry(observed_usage)
        if isinstance(exc.status, int) and not isinstance(exc.status, bool) and 100 <= exc.status <= 599:
            cases[-1]["error_http_status"] = exc.status
        # Persist only a fixed diagnostic, never provider-controlled messages
        # which can reflect credentials or request content.
        if exc.code == "invalid_request" and any(
            marker in str(exc).lower()
            for marker in ("max_tokens", "max_output_tokens", "max_completion_tokens")
        ):
            cases[-1]["error_hint"] = (
                "供应商拒绝了最大输出 Token 参数，请检查该模型的输出上限和任务预算。"
            )

    if "text" not in successful_cases:
        try:
            text = client.chat(
                [
                    {"role": "system", "content": "Reply with exactly OK."},
                    {"role": "user", "content": "Protocol conformance test."},
                ],
                model=route.model,
                thinking=route.thinking,
                reasoning_effort=route.reasoning_effort,
                max_tokens=max_tokens,
                request_timeout=remaining_timeout(),
                temperature=0,
            )
            # This case proves the declared text-chat wire contract: the provider
            # returned a parseable, non-empty assistant message.  Exact phrasing is
            # a model-quality property, not a protocol capability; reasoning-first
            # models may include an explanation even when asked for a terse marker.
            remaining_timeout(text.usage)
            incomplete = text.finish_reason == "length"
            record(
                "text", bool(text.content.strip()) and not incomplete, usage=text.usage,
                code="response_incomplete" if incomplete else None,
            )
            record("usage", bool(text.usage.get("total_tokens") is not None))
        except ProviderError as exc:
            record_error("text", exc)

    fatal = any(item.get("error_code") in {"authentication_failed", "permission_denied", "insufficient_balance", "budget_exceeded", "endpoint_not_found"} for item in cases)
    if not fatal and "json_object" not in successful_cases and route.capabilities.json_object and (requested is None or "json_object" in requested):
        try:
            structured_options = {}
            structured_call = client.chat_json
            if getattr(client, "wire_profile", {}).get("structured_output") == "native_schema":
                structured_call = client.structured
                structured_options = {"json_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                      "required": ["ok"], "additionalProperties": False},
                                      "schema_name": "connection_check"}
            structured = structured_call(
                [
                    {"role": "system", "content": "Return JSON only."},
                    {"role": "user", "content": 'Return exactly this JSON object: {"ok":true}'},
                ],
                model=route.model,
                thinking=route.thinking,
                reasoning_effort=route.reasoning_effort,
                max_tokens=max_tokens,
                request_timeout=remaining_timeout(),
                temperature=0,
                json_retries=0,
                **structured_options,
            )
            remaining_timeout(structured.usage)
            incomplete = structured.finish_reason == "length"
            record(
                "json_object", structured.data.get("ok") is True and not incomplete,
                usage=structured.usage, code="response_incomplete" if incomplete else None,
            )
        except ProviderError as exc:
            record_error("json_object", exc)

    if not fatal and "stream" not in successful_cases and route.capabilities.streaming and (
        requested is None or {"streaming", "stream_usage"} & requested
    ):
        stream_usage: dict[str, Any] = {}
        saw_content = False
        saw_done = False
        incomplete = False
        original_timeout = getattr(client, "timeout", None)
        try:
            stream_timeout = remaining_timeout()
            if isinstance(original_timeout, (int, float)):
                client.timeout = min(original_timeout, stream_timeout)
            for event in client.stream_chat(
                [
                    {"role": "system", "content": "Reply with exactly OK."},
                    {"role": "user", "content": "Streaming protocol conformance test."},
                ],
                model=route.model,
                thinking=route.thinking,
                reasoning_effort=route.reasoning_effort,
                max_tokens=max_tokens,
                temperature=0,
            ):
                saw_content = saw_content or (event.kind == "delta" and bool(event.content))
                if event.usage:
                    stream_usage = dict(event.usage)
                remaining_timeout(stream_usage)
                saw_done = saw_done or event.kind == "done"
                incomplete = incomplete or event.finish_reason == "length"
            remaining_timeout(stream_usage)
            record(
                "stream", saw_content and saw_done and not incomplete, usage=stream_usage,
                code="response_incomplete" if incomplete else None,
            )
            if route.capabilities.stream_usage and (
                requested is None or "stream_usage" in requested
            ):
                record("stream_usage", saw_done and stream_usage.get("total_tokens") is not None)
        except ProviderError as exc:
            record_error("stream", exc)
        finally:
            if isinstance(original_timeout, (int, float)):
                client.timeout = original_timeout

    if route.capabilities.usage_cache_tokens and (
        requested is None or "usage_cache_tokens" in requested
    ):
        record(
            "cache_telemetry",
            any("cache_read_tokens" in item.get("usage", {}) for item in cases),
        )

    required = {"text"}
    if route.capabilities.json_object and (requested is None or "json_object" in requested):
        required.add("json_object")
    if route.capabilities.streaming and (requested is None or "streaming" in requested):
        required.add("stream")
    by_name = {item["name"]: item for item in cases}
    passed = all(bool(by_name.get(name, {}).get("ok")) for name in required)
    verified_capabilities = ["text_chat"] if passed else []
    if passed and by_name.get("usage", {}).get("ok"):
        verified_capabilities.append("usage")
    if passed and route.thinking:
        # A successful probe with the requested non-auto strength is also the
        # evidence that this provider/model accepts its reasoning dialect.
        verified_capabilities.append("thinking")
    if passed and by_name.get("json_object", {}).get("ok"):
        verified_capabilities.append("json_object")
        if getattr(client, "wire_profile", {}).get("structured_output") == "native_schema":
            verified_capabilities.append("json_schema")
    if passed and by_name.get("stream", {}).get("ok"):
        verified_capabilities.append("streaming")
    if passed and by_name.get("stream_usage", {}).get("ok"):
        verified_capabilities.append("stream_usage")
    if passed and by_name.get("cache_telemetry", {}).get("ok"):
        verified_capabilities.append("usage_cache_tokens")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return _TrustedConformanceReport({
        "schema": "provider-conformance-v1",
        "conformance_version": CONFORMANCE_VERSION,
        "provider_id": route.provider_id,
        "origin": route.provider["base_url"],
        "model": route.model,
        "reasoning_strength": route.reasoning_strength,
        "profile_id": route.profile_id,
        "budget": {
            "max_output_tokens": max_tokens,
            "request_timeout_seconds": request_timeout,
        },
        "adapter": model_adapter(route.provider, route.model),
        "definition_hash": provider_definition_hash(route.provider_id, route.provider, route.model),
        "verified_at": stamp,
        "passed": passed,
        "verified_capabilities": sorted(set(verified_capabilities)),
        "verified_reasoning_strengths": (
            [route.reasoning_strength]
            if passed and route.reasoning_strength != "auto" else []
        ),
        "cases": cases,
        "usage": observed_usage,
        "offline_contracts": {
            "errors": "unit-tested-v1",
            "retry": "unit-tested-v1",
            "redirects": "unit-tested-v1",
            "credential_no_echo": "unit-tested-v1",
        },
    })


def verified_definition_from_report(
    provider_id: str,
    provider: Mapping[str, Any],
    model: str,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Issue persisted evidence only from a report produced in this process."""

    if not isinstance(report, _TrustedConformanceReport) or report._issuer is not _EVIDENCE_ISSUER:
        raise ValueError("untrusted conformance report")
    selected_model = validate_model_id(model)
    expected_hash = provider_definition_hash(provider_id, provider, selected_model)
    if (
        not bool(report.get("passed"))
        or report.get("provider_id") != provider_id
        or report.get("model") != selected_model
        or report.get("definition_hash") != expected_hash
        or report.get("conformance_version") != CONFORMANCE_VERSION
    ):
        raise ValueError("conformance report does not match the provider definition")
    source = (provider.get("models") or {}).get(selected_model)
    if not isinstance(source, Mapping):
        raise ValueError("model is not declared by provider")
    definition = deepcopy(dict(source))
    capabilities = report.get("verified_capabilities")
    if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
        raise ValueError("conformance capability evidence is invalid")
    strength = validate_reasoning_strength(report.get("reasoning_strength"))
    evidence_is_current = definition.get("evidence_hash") == expected_hash
    prior_strengths = (
        list(definition.get("verified_reasoning_strengths") or ())
        if evidence_is_current else []
    )
    prior_capabilities = (
        list(definition.get("verified_capabilities") or ())
        if evidence_is_current else []
    )
    if strength != "auto":
        prior_strengths.append(strength)
    wire = report.get("negotiated_wire_profile")
    if isinstance(wire, Mapping):
        if dict(wire) != dict(definition.get("wire_profile") or {}):
            prior_capabilities = []
            prior_strengths = [strength] if strength != "auto" else []
        definition["wire_profile"] = dict(wire)
    cases = {item["name"]: item for item in report.get("cases", [])}
    native = dict(definition.get("capabilities") or {})
    effective = dict(definition.get("effective_capabilities") or native)
    for case, capability in (("text", "text_chat"), ("json_object", "json_object"),
                             ("stream", "streaming"), ("usage", "usage"), ("stream_usage", "stream_usage")):
        if case in cases:
            native[capability] = effective[capability] = bool(cases[case].get("ok"))
    if isinstance(wire, Mapping):
        if wire.get("structured_output") == "native_schema" and "json_schema" in capabilities:
            native["json_schema"] = effective["json_schema"] = True
        if wire.get("structured_output") == "prompt_json":
            native["json_object"] = native["json_schema"] = False
        if wire.get("streaming") == "buffered":
            native["streaming"] = native["stream_usage"] = False
            effective["stream_usage"] = False
    definition["capabilities"] = native
    definition["effective_capabilities"] = effective
    negotiated = report.get("negotiated_max_output_tokens", report.get("verified_max_output_tokens"))
    if negotiated is not None:
        if (
            isinstance(negotiated, bool) or not isinstance(negotiated, int)
            or negotiated <= 0
            or negotiated != (report.get("budget") or {}).get("max_output_tokens")
            or not (report.get("output_limit_negotiation") or report.get("output_budget_bootstrap"))
        ):
            raise ValueError("invalid negotiated output limit")
        declared = (definition.get("capabilities") or {}).get("max_output_tokens")
        if declared is not None and negotiated > declared:
            raise ValueError("negotiation must not raise the declared output limit")
        definition["capabilities"] = {
            **dict(definition.get("capabilities") or {}),
            "max_output_tokens": negotiated,
        }
        definition["effective_capabilities"]["max_output_tokens"] = negotiated
        updated_provider = deepcopy(dict(provider))
        updated_provider["models"][selected_model] = definition
        expected_hash = provider_definition_hash(provider_id, updated_provider, selected_model)
        # Re-issue only the evidence actually proved at the new output budget.
        prior_capabilities = []
        prior_strengths = [strength] if strength != "auto" else []
    updated_provider = deepcopy(dict(provider))
    updated_provider["models"][selected_model] = definition
    expected_hash = provider_definition_hash(provider_id, updated_provider, selected_model)
    definition.update(
        evidence="verified_live",
        evidence_hash=expected_hash,
        verified_at=str(report.get("verified_at") or ""),
        verified_capabilities=sorted(set(prior_capabilities + capabilities)),
        verified_reasoning_strengths=sorted(
            set(prior_strengths), key=("auto", "off", "low", "medium", "high").index
        ),
    )
    return definition


__all__ = [
    "CONFORMANCE_VERSION", "run_live_conformance", "verified_definition_from_report",
]
