"""Configured Responses API adapter with explicit terminal-state validation.

Wire contract: https://platform.openai.com/docs/api-reference/responses
Events: https://platform.openai.com/docs/api-reference/responses-streaming
Authentication, origin binding and redirect rejection use the shared relay core.
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import replace
from typing import Any, Iterator, Mapping, Sequence

from .deepseek import _NETWORK_EXCEPTIONS, _envelope_error, _iter_sse_data, _managed_response, _sanitize
from .openai_compatible import OpenAICompatibleClient
from .provider import AIResult, AIJsonResult, AIStreamEvent, ProviderError, ProviderProtocolError, ProviderConfigurationError
from .provider_config import normalize_base_url
from .usage import merge_usage, normalize_usage


class OpenAIResponsesClient(OpenAICompatibleClient):
    """ProviderPort implementation for explicitly selected Responses endpoints."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.endpoint = normalize_base_url(self.base_url).rstrip("/") + "/responses"

    def _payload(self, messages: Sequence[Mapping[str, Any]], **options: Any) -> dict[str, Any]:
        # Validate common options/capabilities without transmitting Chat fields.
        chat = super()._payload(messages, **options)
        payload: dict[str, Any] = {
            "model": chat["model"], "stream": chat["stream"], "store": False,
            "input": [{
                "type": "message",
                "role": "developer" if item["role"] == "system" else item["role"],
                "content": [{
                    "type": "output_text" if item["role"] == "assistant" else "input_text",
                    "text": item["content"],
                }],
            } for item in chat["messages"]],
        }
        if "reasoning_effort" in chat:
            payload["reasoning"] = {"effort": chat["reasoning_effort"]}
        if "max_tokens" in chat or "max_completion_tokens" in chat:
            payload["max_output_tokens"] = chat.get("max_tokens", chat.get("max_completion_tokens"))
        if "temperature" in chat:
            payload["temperature"] = chat["temperature"]
        if "response_format" in chat:
            payload["text"] = {"format": chat["response_format"]}
        return payload

    def _parse_chat_result(self, data: Any, *, requested_model: str | None = None) -> AIResult:
        if not isinstance(data, Mapping):
            raise ProviderProtocolError("invalid_response", "Responses result must be an object")
        context = {
            "usage": normalize_usage(data["usage"]) if isinstance(data.get("usage"), Mapping) else {},
            "model": str(data.get("model") or ""),
            "response_id": str(data["id"]) if data.get("id") is not None else None,
            "requested_model": requested_model,
        }
        error = _envelope_error(data, self._api_key)
        if error is not None:
            for key, value in context.items():
                setattr(error, key, value)
            raise error
        status = data.get("status")
        if status == "incomplete":
            details = data.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, Mapping) else None
            filtered = reason == "content_filter"
            raise ProviderProtocolError(
                "content_filter" if filtered else "response_incomplete",
                "Responses generation was incomplete",
                finish_reason="content_filter" if filtered else "length", **context,
            )
        if status != "completed":
            raise ProviderProtocolError("invalid_response_status", "Responses result did not complete", **context)
        output = data.get("output")
        if not isinstance(output, list):
            raise ProviderProtocolError("invalid_response", "Responses result has no output array", **context)
        text: list[str] = []
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            parts = item.get("content")
            if not isinstance(parts, list):
                raise ProviderProtocolError("invalid_response", "Responses message has invalid content", **context)
            for part in parts:
                if not isinstance(part, Mapping):
                    raise ProviderProtocolError("invalid_response", "Responses content part is invalid", **context)
                if part.get("type") == "refusal":
                    raise ProviderProtocolError("content_filter", "Provider refused this request", finish_reason="content_filter", **context)
                if part.get("type") == "output_text":
                    if not isinstance(part.get("text"), str):
                        raise ProviderProtocolError("invalid_response", "Responses output text is invalid", **context)
                    text.append(part["text"])
        return AIResult(content="".join(text), finish_reason="stop", **context,
                        provider_metadata={"transport_api": "responses"})

    def _nonstream(self, *args: Any, **kwargs: Any) -> AIResult:
        result = super()._nonstream(*args, **kwargs)
        return replace(result, provider_metadata={**result.provider_metadata, "transport_api": "responses"})

    def chat_json(self, messages: Sequence[Mapping[str, Any]], *, json_retries: int = 1, **options: Any) -> AIJsonResult:
        if isinstance(json_retries, bool) or json_retries not in {0, 1}:
            raise ProviderConfigurationError("invalid_json_retries", "json_retries must be 0 or 1")
        wire_options = dict(options)
        transport_options = {key: wire_options.pop(key) for key in (
            "retry_callback", "request_timeout", "request_retries") if key in wire_options}
        wire_options.setdefault("thinking", False)
        wire_options.setdefault("reasoning_effort", "auto")
        current_messages = list(messages)
        usage: dict[str, Any] = {}
        for attempt in range(json_retries + 1):
            payload = self._payload(current_messages, stream=False, json_object=True, **wire_options)
            try:
                result = self._nonstream(payload, **transport_options)
            except ProviderError as exc:
                prior = dict(usage)
                merge_usage(prior, exc.usage)
                exc.usage = prior
                raise
            merge_usage(usage, result.usage)
            try:
                parsed, repaired = self._decode_json_output(result.content)
            except json.JSONDecodeError:
                parsed, repaired = None, False
            if isinstance(parsed, dict):
                usage["protocol_repairs"] = attempt + int(repaired)
                return AIJsonResult(
                    content=result.content, data=parsed, finish_reason=result.finish_reason,
                    model=result.model, requested_model=result.requested_model,
                    response_id=result.response_id, usage=usage,
                    provider_metadata={**result.provider_metadata, "protocol_repairs": usage["protocol_repairs"]},
                )
            current_messages.append({"role": "user", "content": "Return one complete JSON object, without commentary."})
        raise ProviderProtocolError("invalid_json_output", "Responses output must be one JSON object", usage=usage,
                                    model=result.model, requested_model=result.requested_model, response_id=result.response_id)

    def structured(self, messages: Sequence[Mapping[str, Any]], *, json_schema: Mapping[str, Any], schema_name: str, **options: Any) -> AIJsonResult:
        if self.wire_profile.get("structured_output") in {"native_schema", "prompt_json"}:
            return super().structured(messages, json_schema=json_schema, schema_name=schema_name, **options)
        if not isinstance(json_schema, Mapping) or not str(schema_name or "").strip():
            raise ProviderConfigurationError("invalid_json_schema", "json_schema and schema_name are required")
        options.pop("json_retries", None)
        result = self.chat_json(messages, json_retries=0, **options)
        return replace(result, provider_metadata={**result.provider_metadata, "structured_format": self.wire_profile.get("structured_output", "json_object")})

    def chat_with_tools(self, *args: Any, **kwargs: Any) -> Any:
        raise ProviderConfigurationError("unsupported_capability", "Responses function tools are not supported by this adapter")

    def _stream(self, payload: Mapping[str, Any]) -> Iterator[AIStreamEvent]:
        requested = str(payload.get("model") or "")
        started = time.perf_counter()
        count_before = self.provider_request_count
        prior_usage: dict[str, Any] = {}
        emitted = False

        def telemetry() -> dict[str, Any]:
            return {"provider_requests": self.provider_request_count - count_before,
                    "latency_ms": max(0, round((time.perf_counter() - started) * 1000)), "protocol_repairs": 0}

        for attempt in range(self.retries + 1):
            model = response_id = None
            chunks: list[str] = []
            text_size = 0
            usage: dict[str, Any] = {}
            try:
                deadline = time.monotonic() + self.timeout
                with _managed_response(self._open(payload)) as response:
                    for raw in _iter_sse_data(response, deadline=deadline):
                        if len(raw) > 16 * 1024 * 1024:
                            raise ProviderProtocolError("invalid_stream", "Responses SSE event exceeds size limit")
                        if raw == "[DONE]":
                            break  # Only response.completed proves successful completion.
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            raise ProviderProtocolError("invalid_stream", "Responses SSE is not valid JSON") from None
                        if not isinstance(event, Mapping):
                            raise ProviderProtocolError("invalid_stream", "Responses SSE event must be an object")
                        kind = event.get("type")
                        if kind == "error":
                            error = _envelope_error({"error": event}, self._api_key)
                            raise error or ProviderError("provider_error", "Provider stream failed")
                        error = _envelope_error(event, self._api_key)
                        if error is not None:
                            raise error
                        snapshot = event.get("response")
                        if isinstance(snapshot, Mapping):
                            model = str(snapshot.get("model") or model or "")
                            response_id = str(snapshot.get("id") or response_id or "")
                            if isinstance(snapshot.get("usage"), Mapping):
                                usage = normalize_usage(snapshot["usage"])
                        if kind in {"response.completed", "response.failed", "response.incomplete"}:
                            result = self._parse_chat_result(snapshot, requested_model=requested)
                            if kind != "response.completed":
                                raise ProviderProtocolError("invalid_stream", "Responses terminal event conflicts with status")
                            final_text = result.content
                            streamed_text = "".join(chunks)
                            if streamed_text and final_text != streamed_text:
                                raise ProviderProtocolError("invalid_stream", "Responses final text differs from streamed output")
                            if not streamed_text and final_text:
                                emitted = True
                                yield AIStreamEvent("delta", content=final_text, model=model, response_id=response_id, requested_model=requested)
                            merge_usage(prior_usage, result.usage)
                            merge_usage(prior_usage, telemetry())
                            yield AIStreamEvent("done", finish_reason="stop", usage=prior_usage, model=model,
                                                response_id=response_id, requested_model=requested,
                                                provider_metadata={"transport_api": "responses", **telemetry()})
                            return
                        if kind == "response.output_text.delta":
                            delta = event.get("delta")
                            if not isinstance(delta, str):
                                raise ProviderProtocolError("invalid_stream", "Responses text delta is invalid")
                            if delta:
                                text_size += len(delta)
                                if text_size > 16 * 1024 * 1024:
                                    raise ProviderProtocolError("invalid_stream", "Responses stream exceeds text limit")
                                chunks.append(delta)
                                emitted = True
                                yield AIStreamEvent("delta", content=delta, model=model, response_id=response_id, requested_model=requested)
                        elif kind in {"response.refusal.delta", "response.refusal.done"}:
                            raise ProviderProtocolError("content_filter", "Provider refused this request", finish_reason="content_filter")
                        else:
                            yield AIStreamEvent("heartbeat", model=model, response_id=response_id, requested_model=requested)
                raise ProviderProtocolError("incomplete_stream", "Responses stream ended before response.completed")
            except (ProviderError, *_NETWORK_EXCEPTIONS) as exc:
                if not isinstance(exc, ProviderError):
                    exc = ProviderError("timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "network_error",
                                        _sanitize(str(exc), self._api_key), retryable=True)
                # Terminal error snapshots and the snapshot usage describe the
                # same request; do not count them twice.
                attempt_usage = dict(usage)
                attempt_usage.update(exc.usage)
                merge_usage(prior_usage, attempt_usage)
                if emitted or not exc.retryable or attempt >= self.retries:
                    exc.usage = dict(prior_usage)
                    merge_usage(exc.usage, telemetry())
                    exc.requested_model = requested
                    exc.model = exc.model or model
                    exc.response_id = exc.response_id or response_id
                    raise exc
                self._sleep_before_retry(attempt, code=exc.code,
                    retry_after_seconds=exc.protocol_details.get("retry_after_seconds"))
