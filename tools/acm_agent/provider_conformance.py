"""Safe Chat Completions conformance probes and redacted evidence reports."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from copy import deepcopy
from typing import Any, Mapping, Sequence

from .provider import ProviderConfigurationError, ProviderError, ProviderPort
from .provider_config import validate_model_id, validate_reasoning_strength
from .provider_registry import ProviderRoute, provider_definition_hash
from .usage import merge_usage


CONFORMANCE_VERSION = 3
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


def run_live_conformance(
    client: ProviderPort,
    route: ProviderRoute,
    *,
    required_capabilities: Sequence[str] | None = None,
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

    cases: list[dict[str, Any]] = []
    observed_usage: dict[str, Any] = {}

    def record(name: str, ok: bool, *, usage: dict[str, Any] | None = None, code: str | None = None) -> None:
        nonlocal observed_usage
        if usage:
            merge_usage(observed_usage, _usage_summary(usage))
        item: dict[str, Any] = {"name": name, "ok": bool(ok)}
        if usage:
            item["usage"] = _usage_summary(usage)
        if code:
            item["error_code"] = code
        cases.append(item)

    try:
        text = client.chat(
            [
                {"role": "system", "content": "Reply with exactly OK."},
                {"role": "user", "content": "Protocol conformance test."},
            ],
            model=route.model,
            thinking=route.thinking,
            reasoning_effort=route.reasoning_effort,
            max_tokens=8,
            temperature=0,
        )
        # This case proves the declared text-chat wire contract: the provider
        # returned a parseable, non-empty assistant message.  Exact phrasing is
        # a model-quality property, not a protocol capability; reasoning-first
        # models may include an explanation even when asked for a terse marker.
        record("text", bool(text.content.strip()), usage=text.usage)
        record("usage", bool(text.usage.get("total_tokens") is not None))
    except ProviderError as exc:
        record("text", False, usage=dict(exc.usage), code=exc.code)

    if route.capabilities.json_object and (requested is None or "json_object" in requested):
        try:
            structured = client.chat_json(
                [
                    {"role": "system", "content": "Return JSON only."},
                    {"role": "user", "content": 'Return exactly this JSON object: {"ok":true}'},
                ],
                model=route.model,
                thinking=route.thinking,
                reasoning_effort=route.reasoning_effort,
                max_tokens=32,
                temperature=0,
                json_retries=0,
            )
            record("json_object", structured.data.get("ok") is True, usage=structured.usage)
        except ProviderError as exc:
            record("json_object", False, usage=dict(exc.usage), code=exc.code)

    if route.capabilities.streaming and (
        requested is None or {"streaming", "stream_usage"} & requested
    ):
        stream_usage: dict[str, Any] = {}
        saw_content = False
        saw_done = False
        try:
            for event in client.stream_chat(
                [
                    {"role": "system", "content": "Reply with exactly OK."},
                    {"role": "user", "content": "Streaming protocol conformance test."},
                ],
                model=route.model,
                thinking=route.thinking,
                reasoning_effort=route.reasoning_effort,
                max_tokens=8,
                temperature=0,
            ):
                saw_content = saw_content or (event.kind == "delta" and bool(event.content))
                if event.usage:
                    stream_usage = dict(event.usage)
                saw_done = saw_done or event.kind == "done"
            record("stream", saw_content and saw_done, usage=stream_usage)
            if route.capabilities.stream_usage and (
                requested is None or "stream_usage" in requested
            ):
                record("stream_usage", saw_done and stream_usage.get("total_tokens") is not None)
        except ProviderError as exc:
            record("stream", False, usage=dict(exc.usage), code=exc.code)

    if route.capabilities.usage_cache_tokens and (
        requested is None or "usage_cache_tokens" in requested
    ):
        record(
            "cache_telemetry",
            any("cache_read_tokens" in item.get("usage", {}) for item in cases),
        )

    required = {"text", "usage"}
    if route.capabilities.json_object and (requested is None or "json_object" in requested):
        required.add("json_object")
    if route.capabilities.streaming and (requested is None or "streaming" in requested):
        required.add("stream")
    if route.capabilities.stream_usage and (requested is None or "stream_usage" in requested):
        required.add("stream_usage")
    if route.capabilities.usage_cache_tokens and (
        requested is None or "usage_cache_tokens" in requested
    ):
        required.add("cache_telemetry")
    by_name = {item["name"]: item for item in cases}
    passed = all(bool(by_name.get(name, {}).get("ok")) for name in required)
    verified_capabilities = ["text_chat", "usage"] if passed else []
    if passed and route.thinking:
        # A successful probe with the requested non-auto strength is also the
        # evidence that this provider/model accepts its reasoning dialect.
        verified_capabilities.append("thinking")
    if passed and by_name.get("json_object", {}).get("ok"):
        verified_capabilities.append("json_object")
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
        "adapter": route.provider["adapter"],
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
