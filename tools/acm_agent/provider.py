"""Provider-neutral contracts for model clients and business services.

The concrete adapter owns wire-format quirks.  Service code consumes only the
types and protocol in this module so adding another provider does not require
provider-specific exception checks or result parsing in the business layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence, TypedDict


OUTPUT_TOKEN_LIMIT_MESSAGE = (
    "输出达到 Token 上限，结果不可用。请在设置中提高对应任务的最大输出 Token。"
)


class AIUsage(TypedDict, total=False):
    """Canonical usage keys; adapters may retain additional safe scalar fields."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_miss_tokens: int
    reasoning_tokens: int
    provider_requests: int
    protocol_repairs: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class AIResult:
    content: str
    finish_reason: str | None
    usage: dict[str, Any]
    model: str
    response_id: str | None = None
    requested_model: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved_model(self) -> str:
        return self.model


@dataclass(frozen=True, slots=True)
class AIJsonResult:
    content: str
    finish_reason: str | None
    usage: dict[str, Any]
    model: str
    data: dict[str, Any]
    response_id: str | None = None
    requested_model: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved_model(self) -> str:
        return self.model


@dataclass(frozen=True, slots=True)
class AIToolResult:
    content: str
    finish_reason: str | None
    usage: dict[str, Any]
    model: str
    tool_rounds: int
    tool_calls: int
    response_id: str | None = None
    requested_model: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved_model(self) -> str:
        return self.model


@dataclass(frozen=True, slots=True)
class AIStreamEvent:
    kind: str
    content: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    model: str | None = None
    response_id: str | None = None
    requested_model: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved_model(self) -> str | None:
        return self.model


class ProviderError(RuntimeError):
    """Safe provider failure shared by adapters and the application layer."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        usage: Mapping[str, Any] | None = None,
        finish_reason: str | None = None,
        model: str | None = None,
        requested_model: str | None = None,
        response_id: str | None = None,
        protocol_details: Mapping[str, Any] | None = None,
    ) -> None:
        normalized_finish_reason = str(finish_reason or "").strip().lower()
        safe_message = (
            OUTPUT_TOKEN_LIMIT_MESSAGE
            if normalized_finish_reason == "length"
            else message
        )
        super().__init__(safe_message)
        self.code = str(code)
        self.status = status
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.usage = dict(usage or {})
        self.finish_reason = finish_reason
        self.model = model
        self.requested_model = requested_model
        self.response_id = response_id
        self.protocol_details = dict(protocol_details or {})

    @property
    def resolved_model(self) -> str | None:
        return self.model

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "status": self.status,
            "retryable": self.retryable,
        }
        if self.retry_after is not None:
            result["retry_after"] = self.retry_after
        if self.usage:
            result["usage"] = dict(self.usage)
        if self.finish_reason is not None:
            result["finish_reason"] = self.finish_reason
        if self.model is not None:
            result["model"] = self.model
            result["resolved_model"] = self.model
        if self.requested_model is not None:
            result["requested_model"] = self.requested_model
        if self.response_id is not None:
            result["response_id"] = self.response_id
        if self.protocol_details:
            result["protocol_details"] = dict(self.protocol_details)
        return result


class ProviderConfigurationError(ProviderError):
    pass


class ProviderProtocolError(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    text_chat: bool
    streaming: bool
    json_object: bool
    function_tools: bool
    thinking: bool
    prompt_cache: bool
    usage_cache_tokens: bool
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None
    usage: bool = True
    stream_usage: bool = True
    json_schema: bool = False
    evidence: str = "declared"
    evidence_hash: str | None = None
    verified_at: str | None = None
    verified_capabilities: tuple[str, ...] = ()
    verified_reasoning_strengths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    ok: bool
    requested_model: str
    resolved_model: str | None = None
    response_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None


RetryCallback = Callable[[int, int, str, float], None]


class ProviderPort(Protocol):
    @property
    def key_detected(self) -> bool: ...

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        **options: Any,
    ) -> AIResult: ...

    def chat_json(
        self,
        messages: Sequence[Mapping[str, Any]],
        **options: Any,
    ) -> AIJsonResult: ...

    def structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        **options: Any,
    ) -> AIJsonResult: ...

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        **options: Any,
    ) -> Iterator[AIStreamEvent]: ...

    def test_connection(self, model: str) -> ProviderHealth: ...

    def capabilities(self, model: str) -> CapabilityProfile: ...


__all__ = [
    "AIJsonResult",
    "AIResult",
    "AIStreamEvent",
    "AIToolResult",
    "AIUsage",
    "CapabilityProfile",
    "OUTPUT_TOKEN_LIMIT_MESSAGE",
    "ProviderError",
    "ProviderConfigurationError",
    "ProviderHealth",
    "ProviderPort",
    "ProviderProtocolError",
    "RetryCallback",
]
