"""Small, dependency-free DeepSeek Chat Completions and Responses client.

Protocol references:
- Chat/SSE shapes and ``stream_options.include_usage``:
  https://api-docs.deepseek.com/api/create-chat-completion/
- JSON Output requires ``response_format`` plus an explicit JSON instruction:
  https://api-docs.deepseek.com/guides/json_mode/
- Thinking mode uses ``thinking.type`` and ``reasoning_effort``:
  https://api-docs.deepseek.com/guides/thinking_mode/
- Current model IDs and fixed OpenAI-compatible base URL:
  https://api-docs.deepseek.com/quick_start/pricing/
- Provider HTTP status meanings used by the retry/error mapping:
  https://api-docs.deepseek.com/quick_start/error_codes/
- Flash Responses structured output and status/usage shapes:
  https://api-docs.deepseek.com/api/create-response/
"""

from __future__ import annotations

import json
import http.client
import math
import os
import random as _random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from .ai_policy import (
    ALLOWED_MODELS,
    ALLOWED_REASONING_EFFORTS,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
)
from .provider import (
    AIJsonResult as JsonChatResult,
    AIResult as ChatResult,
    AIStreamEvent as StreamEvent,
    AIToolResult as ToolChatResult,
    CapabilityProfile,
    ProviderError,
    ProviderConfigurationError,
    ProviderHealth,
    ProviderProtocolError,
    RetryCallback,
)
from .usage import merge_usage, normalize_usage


DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_RESPONSES_ENDPOINT = "https://api.deepseek.com/responses"
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
MAX_RETRY_BACKOFF_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 60.0
# Transport-level ceiling for a single HTTP request.
MAX_TRANSPORT_REQUEST_SECONDS = 300.0
_NETWORK_EXCEPTIONS = (
    urllib.error.URLError,
    TimeoutError,
    socket.timeout,
    OSError,
    http.client.IncompleteRead,
    http.client.HTTPException,
)
_ACTIVE_TELEMETRY: ContextVar[dict[str, int] | None] = ContextVar(
    "deepseek_call_telemetry", default=None
)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_RejectRedirects())


class Transport(Protocol):
    def __call__(self, request: urllib.request.Request, timeout: float) -> Any: ...


class DeepSeekError(ProviderError):
    """A safe-to-display DeepSeek failure.

    ``message`` is sanitized before it reaches this object. The exception never
    retains the API key, the Authorization header, or the raw response body.
    Provider-neutral callers catch :class:`ProviderError`; this subclass stays
    public so existing integrations and tests keep their import path.
    """

    pass


class DeepSeekConfigurationError(DeepSeekError, ProviderConfigurationError):
    pass


class DeepSeekProtocolError(DeepSeekError, ProviderProtocolError):
    pass


def validate_model(model: str) -> str:
    model = str(model).strip()
    if model not in ALLOWED_MODELS:
        allowed = ", ".join(sorted(ALLOWED_MODELS))
        raise DeepSeekConfigurationError(
            "invalid_model", f"Unsupported DeepSeek model; allowed: {allowed}"
        )
    return model


def validate_reasoning_effort(reasoning_effort: str) -> str:
    effort = str(reasoning_effort).strip().lower()
    if effort not in ALLOWED_REASONING_EFFORTS:
        raise DeepSeekConfigurationError(
            "invalid_reasoning_effort", "reasoning_effort must be high or max"
        )
    return effort


def _sanitize(value: Any, secret: str | None = None) -> str:
    text = str(value or "DeepSeek request failed")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)\bsk-[a-z0-9_-]{8,}\b", "[REDACTED]", text)
    text = " ".join(text.split())
    return text[:500]


def _error_code_for_status(status: int) -> str:
    return {
        408: "timeout",
        400: "invalid_request",
        401: "authentication_failed",
        402: "insufficient_balance",
        422: "invalid_request",
        429: "rate_limited",
        500: "server_error",
        502: "server_error",
        503: "server_error",
        504: "server_error",
    }.get(status, "http_error")


def _provider_message(body: bytes, status: int) -> str:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"DeepSeek request failed with HTTP {status}"
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping) and error.get("message"):
            return str(error["message"])
    return f"DeepSeek request failed with HTTP {status}"


def _retry_after_seconds(response: Any, *, now: float) -> float | None:
    headers = getattr(response, "headers", None)
    raw: Any = None
    if headers is not None:
        getter = getattr(headers, "get", None)
        if callable(getter):
            raw = getter("Retry-After")
    if raw is None:
        getter = getattr(response, "getheader", None)
        if callable(getter):
            raw = getter("Retry-After")
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
            seconds = parsed.timestamp() - float(now)
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return min(MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))


def _http_error(
    status: int,
    body: bytes,
    secret: str | None,
    *,
    retry_after_seconds: float | None = None,
) -> DeepSeekError:
    protocol_details: dict[str, Any] = {}
    if retry_after_seconds is not None:
        protocol_details["retry_after_seconds"] = float(retry_after_seconds)
    usage: dict[str, Any] = {}
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, Mapping) and isinstance(payload.get("usage"), Mapping):
        usage = normalize_usage(payload["usage"])
    return DeepSeekError(
        _error_code_for_status(status),
        _sanitize(_provider_message(body, status), secret),
        status=status,
        retryable=status in RETRYABLE_HTTP_STATUSES,
        retry_after=retry_after_seconds,
        usage=usage,
        protocol_details=protocol_details,
    )


@contextmanager
def _managed_response(response: Any) -> Iterator[Any]:
    enter = getattr(response, "__enter__", None)
    if callable(enter):
        with response as opened:
            yield opened
        return
    try:
        yield response
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else 200
    return int(status or 200)


def _response_error_details(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        return {}
    details: dict[str, Any] = {}
    usage = data.get("usage")
    if isinstance(usage, Mapping):
        details["usage"] = normalize_usage(usage)
    if data.get("model") is not None:
        details["model"] = str(data["model"])
    if data.get("id") is not None:
        details["response_id"] = str(data["id"])
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        finish_reason = choices[0].get("finish_reason")
        if finish_reason is not None:
            details["finish_reason"] = str(finish_reason)
    return details


def _iter_sse_data(lines: Any) -> Iterator[str]:
    """Parse data-only SSE while ignoring blank lines and keep-alive comments."""
    data_lines: list[str] = []
    for raw in lines:
        try:
            if isinstance(raw, bytes):
                line = raw.decode("utf-8-sig")
            else:
                line = str(raw)
        except UnicodeDecodeError as exc:
            raise DeepSeekProtocolError(
                "invalid_stream", "DeepSeek SSE data is not valid UTF-8"
            ) from exc
        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        yield "\n".join(data_lines)


class DeepSeekClient:
    """DeepSeek-only client with a fixed endpoint and an injectable transport."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        transport: Transport | None = None,
        timeout: float = 60.0,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        wall_time: Callable[[], float] = time.time,
        random_value: Callable[[], float] = _random.random,
    ) -> None:
        self._api_key = (
            str(api_key).strip()
            if api_key is not None
            else str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        )
        self._transport = transport or self._default_transport
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self._sleep = sleep
        self._wall_time = wall_time
        self._random_value = random_value
        self._request_count = 0
        self._request_count_lock = threading.Lock()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(key_detected={self.key_detected}, "
            f"timeout={self.timeout!r}, retries={self.retries!r})"
        )

    @property
    def key_detected(self) -> bool:
        return bool(self._api_key)

    @property
    def provider_request_count(self) -> int:
        """Actual HTTP attempts, including network and JSON protocol retries."""

        with self._request_count_lock:
            return self._request_count

    def capabilities(self, model: str) -> CapabilityProfile:
        selected = validate_model(model)
        return CapabilityProfile(
            text_chat=True,
            streaming=True,
            json_object=True,
            function_tools=True,
            thinking=True,
            prompt_cache=True,
            usage_cache_tokens=True,
            max_context_tokens=1_000_000,
            max_output_tokens=384_000,
            usage=True,
            stream_usage=True,
            json_schema=selected == "deepseek-v4-flash",
            evidence="verified_builtin",
        )

    def test_connection(self, model: str) -> ProviderHealth:
        selected = validate_model(model)
        try:
            result = self.chat(
                [
                    {"role": "system", "content": "Reply with exactly OK."},
                    {"role": "user", "content": "Connection test."},
                ],
                model=selected,
                thinking=False,
                max_tokens=8,
                temperature=0,
            )
        except ProviderError as exc:
            return ProviderHealth(
                ok=False,
                requested_model=selected,
                resolved_model=exc.resolved_model,
                response_id=exc.response_id,
                usage=dict(exc.usage),
                error=exc.as_dict(),
            )
        return ProviderHealth(
            ok=True,
            requested_model=selected,
            resolved_model=result.resolved_model,
            response_id=result.response_id,
            usage=dict(result.usage),
        )

    @staticmethod
    def _default_transport(request: urllib.request.Request, timeout: float) -> Any:
        return _NO_REDIRECT_OPENER.open(request, timeout=timeout)

    def _require_key(self) -> str:
        if not self._api_key:
            raise DeepSeekConfigurationError(
                "missing_api_key",
                "DEEPSEEK_API_KEY is not set",
                retryable=False,
            )
        return self._api_key

    def _request(self, payload: Mapping[str, Any]) -> urllib.request.Request:
        key = self._require_key()
        return urllib.request.Request(
            DEEPSEEK_ENDPOINT,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "acm-agent/2.0 (+local learning tool)",
            },
            method="POST",
        )

    def _responses_request(self, payload: Mapping[str, Any]) -> urllib.request.Request:
        key = self._require_key()
        return urllib.request.Request(
            DEEPSEEK_RESPONSES_ENDPOINT,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "acm-agent/2.0 (+local learning tool)",
            },
            method="POST",
        )

    def _provider_timeout(self, request_timeout: float | None) -> float:
        configured = self.timeout if request_timeout is None else float(request_timeout)
        if not math.isfinite(configured) or configured <= 0:
            raise DeepSeekConfigurationError(
                "invalid_timeout", "request_timeout must be positive and finite"
            )
        return min(MAX_TRANSPORT_REQUEST_SECONDS, configured)

    @staticmethod
    def _close_response(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    @staticmethod
    def _read_response_body(response: Any) -> bytes:
        raw = response.read()
        if isinstance(raw, str):
            return raw.encode("utf-8")
        if isinstance(raw, (bytes, bytearray, memoryview)):
            return bytes(raw)
        raise TypeError("DeepSeek response read returned a non-byte chunk")

    def _payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        thinking: bool,
        reasoning_effort: str,
        stream: bool,
        json_object: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        normalized_messages: list[dict[str, str]] = []
        for message in messages:
            role = str(message.get("role") or "").strip()
            content = message.get("content")
            if role not in {"system", "user", "assistant"} or not isinstance(content, str):
                raise DeepSeekConfigurationError(
                    "invalid_messages",
                    "Each message needs a system/user/assistant role and string content",
                )
            normalized_messages.append({"role": role, "content": content})
        if not normalized_messages:
            raise DeepSeekConfigurationError(
                "invalid_messages", "At least one message is required"
            )
        model = validate_model(model)
        raw_effort = str(reasoning_effort or DEFAULT_REASONING_EFFORT).strip().lower()
        payload: dict[str, Any] = {
            "model": model,
            "messages": normalized_messages,
            "stream": stream,
        }
        if thinking:
            effort = validate_reasoning_effort(raw_effort)
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = effort
        elif raw_effort != "auto":
            validate_reasoning_effort(raw_effort)
            payload["thinking"] = {"type": "disabled"}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if json_object:
            if not any("json" in item["content"].casefold() for item in normalized_messages):
                raise DeepSeekConfigurationError(
                    "missing_json_instruction",
                    "JSON Output requires an explicit JSON instruction in the messages",
                )
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            if int(max_tokens) <= 0:
                raise DeepSeekConfigurationError(
                    "invalid_max_tokens", "max_tokens must be positive"
                )
            payload["max_tokens"] = int(max_tokens)
        if temperature is not None and not thinking:
            value = float(temperature)
            if not 0 <= value <= 2:
                raise DeepSeekConfigurationError(
                    "invalid_temperature", "temperature must be between 0 and 2"
                )
            payload["temperature"] = value
        return payload

    @staticmethod
    def _validated_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in tools:
            function_value = item.get("function")
            if item.get("type") != "function" or not isinstance(function_value, Mapping):
                raise DeepSeekConfigurationError(
                    "invalid_tools", "Each DeepSeek tool must be a function definition"
                )
            function = dict(function_value)
            name = str(function.get("name") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in seen:
                raise DeepSeekConfigurationError(
                    "invalid_tools", "Tool names must be unique and API-compatible"
                )
            parameters = function.get(
                "parameters", {"type": "object", "properties": {}}
            )
            if not isinstance(parameters, Mapping):
                raise DeepSeekConfigurationError(
                    "invalid_tools", "Tool parameters must be a JSON schema object"
                )
            function["parameters"] = dict(parameters)
            seen.add(name)
            normalized.append({"type": "function", "function": function})
        if not normalized:
            raise DeepSeekConfigurationError("invalid_tools", "At least one tool is required")
        return normalized

    def _open(
        self,
        payload: Mapping[str, Any],
        *,
        request_timeout: float | None = None,
        request_factory: Callable[[Mapping[str, Any]], urllib.request.Request] | None = None,
    ) -> Any:
        request = (request_factory or self._request)(payload)
        timeout = self._provider_timeout(request_timeout)
        active_telemetry = _ACTIVE_TELEMETRY.get()
        if active_telemetry is not None:
            active_telemetry["provider_requests"] += 1
        with self._request_count_lock:
            self._request_count += 1
        try:
            response = self._transport(request, timeout)
        except urllib.error.HTTPError as exc:
            retry_after = _retry_after_seconds(exc, now=self._wall_time())
            try:
                with _managed_response(exc) as opened:
                    body = self._read_response_body(opened)
            except Exception:
                body = b""
            raise _http_error(
                int(exc.code),
                body,
                self._api_key,
                retry_after_seconds=retry_after,
            ) from None
        except _NETWORK_EXCEPTIONS as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (ssl.SSLCertVerificationError, ssl.CertificateError)):
                raise DeepSeekError(
                    "tls_verification_failed",
                    "Provider TLS certificate verification failed",
                    retryable=False,
                ) from None
            code = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "network_error"
            raise DeepSeekError(
                code,
                _sanitize(f"DeepSeek network request failed: {exc}", self._api_key),
                retryable=True,
            ) from None
        status = _response_status(response)
        if status >= 400:
            retry_after = _retry_after_seconds(response, now=self._wall_time())
            with _managed_response(response) as opened:
                try:
                    body = self._read_response_body(opened)
                except Exception:
                    body = b""
            raise _http_error(
                status,
                body,
                self._api_key,
                retry_after_seconds=retry_after,
            )
        return response

    def _sleep_before_retry(
        self,
        attempt: int,
        *,
        code: str,
        retry_callback: RetryCallback | None = None,
        retry_limit: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        total = self.retries if retry_limit is None else int(retry_limit)
        base_delay = min(MAX_RETRY_BACKOFF_SECONDS, float(2**attempt))
        try:
            random_value = float(self._random_value())
        except Exception:
            random_value = 0.5
        if not math.isfinite(random_value):
            random_value = 0.5
        random_value = min(1.0, max(0.0, random_value))
        delay = base_delay * (0.5 + random_value)
        if retry_after_seconds is not None:
            retry_after = min(
                MAX_RETRY_AFTER_SECONDS,
                max(0.0, float(retry_after_seconds)),
            )
            delay = max(delay, retry_after)
        delay = min(MAX_RETRY_AFTER_SECONDS, delay)
        if retry_callback is not None:
            try:
                retry_callback(attempt + 1, total, code, delay)
            except Exception:
                pass
        self._sleep(delay)

    def _retry_exhausted(
        self,
        error: DeepSeekError,
        *,
        retry_limit: int | None = None,
    ) -> DeepSeekError:
        total = self.retries if retry_limit is None else int(retry_limit)
        if not error.retryable or total <= 0:
            return error
        return DeepSeekError(
            error.code,
            _sanitize(
                f"{error} (automatic retries exhausted: {total})",
                self._api_key,
            ),
            status=error.status,
            retryable=True,
            retry_after=error.retry_after,
            usage=error.usage,
            finish_reason=error.finish_reason,
            model=error.model,
            response_id=error.response_id,
            protocol_details=error.protocol_details,
        )

    def _nonstream_data(
        self,
        payload: Mapping[str, Any],
        *,
        retry_callback: RetryCallback | None = None,
        request_timeout: float | None = None,
        request_retries: int | None = None,
        request_factory: Callable[[Mapping[str, Any]], urllib.request.Request] | None = None,
    ) -> Mapping[str, Any]:
        retry_limit = self.retries if request_retries is None else int(request_retries)
        if retry_limit < 0:
            raise DeepSeekConfigurationError(
                "invalid_retries", "request_retries must not be negative"
            )
        for attempt in range(retry_limit + 1):
            try:
                response = self._open(
                    payload,
                    request_timeout=request_timeout,
                    request_factory=request_factory,
                )
                with _managed_response(response) as opened:
                    raw = self._read_response_body(opened)
                try:
                    data = json.loads(raw.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
                    raise DeepSeekProtocolError(
                        "invalid_response",
                        _sanitize(f"DeepSeek returned invalid JSON: {exc}"),
                    ) from None
                if not isinstance(data, Mapping):
                    raise DeepSeekProtocolError(
                        "invalid_response", "DeepSeek response must be a JSON object"
                    )
                return data
            except DeepSeekError as exc:
                if not exc.retryable or attempt >= retry_limit:
                    raise self._retry_exhausted(exc, retry_limit=retry_limit) from None
                self._sleep_before_retry(
                    attempt,
                    code=exc.code,
                    retry_callback=retry_callback,
                    retry_limit=retry_limit,
                    retry_after_seconds=exc.protocol_details.get("retry_after_seconds"),
                )
            except _NETWORK_EXCEPTIONS as exc:
                code = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "network_error"
                mapped = DeepSeekError(
                    code,
                    _sanitize(f"DeepSeek response read failed: {exc}", self._api_key),
                    retryable=True,
                )
                if attempt >= retry_limit:
                    raise self._retry_exhausted(mapped, retry_limit=retry_limit) from None
                self._sleep_before_retry(
                    attempt,
                    code=mapped.code,
                    retry_callback=retry_callback,
                    retry_limit=retry_limit,
                )
        raise AssertionError("unreachable")
    def _nonstream(
        self,
        payload: Mapping[str, Any],
        *,
        retry_callback: RetryCallback | None = None,
        request_timeout: float | None = None,
        request_retries: int | None = None,
    ) -> ChatResult:
        telemetry = {"provider_requests": 0}
        token = _ACTIVE_TELEMETRY.set(telemetry)
        started = time.perf_counter()
        try:
            result = self._parse_chat_result(
                self._nonstream_data(
                    payload,
                    retry_callback=retry_callback,
                    request_timeout=request_timeout,
                    request_retries=request_retries,
                ),
                requested_model=str(payload.get("model") or "") or None,
            )
        except ProviderError as exc:
            if exc.requested_model is None:
                exc.requested_model = str(payload.get("model") or "") or None
            merge_usage(
                exc.usage,
                {
                    **telemetry,
                    "latency_ms": max(0, round((time.perf_counter() - started) * 1000)),
                },
            )
            raise
        finally:
            _ACTIVE_TELEMETRY.reset(token)
        call_telemetry = {
            **telemetry,
            "latency_ms": max(0, round((time.perf_counter() - started) * 1000)),
        }
        usage = dict(result.usage)
        merge_usage(usage, call_telemetry)
        return replace(result, usage=usage, provider_metadata=call_telemetry)

    @staticmethod
    def _parse_chat_result(
        data: Any, *, requested_model: str | None = None
    ) -> ChatResult:
        error_details = _response_error_details(data)
        if not isinstance(data, Mapping):
            raise DeepSeekProtocolError(
                "invalid_response",
                "DeepSeek response must be a JSON object",
                **error_details,
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise DeepSeekProtocolError(
                "invalid_response",
                "DeepSeek response has no completion choice",
                **error_details,
            )
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise DeepSeekProtocolError(
                "invalid_response",
                "DeepSeek completion has no assistant message",
                **error_details,
            )
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise DeepSeekProtocolError(
                "invalid_response",
                "DeepSeek assistant content is not text",
                **error_details,
            )
        usage = data.get("usage")
        return ChatResult(
            content=content,
            finish_reason=(
                str(choice["finish_reason"])
                if choice.get("finish_reason") is not None
                else None
            ),
            usage=normalize_usage(usage) if isinstance(usage, Mapping) else {},
            model=str(data.get("model") or ""),
            response_id=str(data["id"]) if data.get("id") is not None else None,
            requested_model=requested_model,
        )

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str = DEFAULT_MODEL,
        thinking: bool = False,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        max_tokens: int | None = None,
        temperature: float | None = None,
        retry_callback: RetryCallback | None = None,
        request_timeout: float | None = None,
        request_retries: int | None = None,
    ) -> ChatResult:
        payload = self._payload(
            messages,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            stream=False,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return self._nonstream(
            payload,
            retry_callback=retry_callback,
            request_timeout=request_timeout,
            request_retries=request_retries,
        )

    def chat_json(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str = DEFAULT_MODEL,
        thinking: bool = False,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        max_tokens: int | None = None,
        temperature: float | None = None,
        retry_callback: RetryCallback | None = None,
        request_timeout: float | None = None,
        request_retries: int | None = None,
        json_retries: int = 1,
    ) -> JsonChatResult:
        payload = self._payload(
            messages,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            stream=False,
            json_object=True,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        protocol_retry_limit = int(json_retries)
        if protocol_retry_limit not in {0, 1}:
            raise DeepSeekConfigurationError(
                "invalid_json_retries", "json_retries must be 0 or 1"
            )
        last_error = "DeepSeek JSON Output was empty"
        current_payload = payload
        total_usage: dict[str, Any] = {}
        last_result: ChatResult | None = None
        for json_attempt in range(protocol_retry_limit + 1):
            try:
                        result = self._nonstream(
                    current_payload,
                    retry_callback=retry_callback,
                    request_timeout=request_timeout,
                    request_retries=request_retries,
                )
            except DeepSeekError as exc:
                if total_usage:
                    combined = dict(total_usage)
                    merge_usage(combined, exc.usage)
                    exc.usage = combined
                if last_result is not None:
                    if exc.finish_reason is None:
                        exc.finish_reason = last_result.finish_reason
                    if exc.model is None:
                        exc.model = last_result.model
                    if exc.response_id is None:
                        exc.response_id = last_result.response_id
                    exc.protocol_details.setdefault("json_attempts_completed", json_attempt)
                raise
            last_result = result
            merge_usage(total_usage, result.usage)
            normalized_envelope = False
            try:
                if result.content.strip():
                    parsed, normalized_envelope = self._decode_json_output(result.content)
                else:
                    parsed = None
            except json.JSONDecodeError as exc:
                parsed = None
                last_error = f"DeepSeek JSON Output was invalid: {exc.msg}"
            if isinstance(parsed, dict):
                protocol_repairs = json_attempt + int(normalized_envelope)
                total_usage["protocol_repairs"] = max(
                    int(total_usage.get("protocol_repairs") or 0), protocol_repairs
                )
                return JsonChatResult(
                    content=result.content,
                    finish_reason=result.finish_reason,
                    usage=total_usage,
                    model=result.model,
                    response_id=result.response_id,
                    data=parsed,
                    requested_model=result.requested_model,
                    provider_metadata={
                        "provider_requests": int(total_usage.get("provider_requests") or 0),
                        "protocol_repairs": protocol_repairs,
                        "latency_ms": int(total_usage.get("latency_ms") or 0),
                    },
                )
            if parsed is not None:
                last_error = "DeepSeek JSON Output must be an object"
            elif result.finish_reason == "length":
                last_error = "DeepSeek JSON Output was empty because the completion token limit was reached"
            if json_attempt < protocol_retry_limit:
                total_usage["protocol_repairs"] = json_attempt + 1
                # DeepSeek documents that JSON Output can occasionally return an
                # empty body. Repeating an identical thinking request can spend
                # the same budget on reasoning again, so make the single allowed
                # protocol retry a compact non-thinking request with an explicit
                # final-output reminder. The first request still honours the
                # caller's thinking setting.
                current_payload = dict(payload)
                retry_messages = [dict(item) for item in payload["messages"]]
                retry_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The previous response had no usable JSON body. "
                            "Return exactly one complete JSON object now, with no Markdown or commentary."
                        ),
                    }
                )
                current_payload["messages"] = retry_messages
                current_payload["thinking"] = {"type": "disabled"}
                current_payload.pop("reasoning_effort", None)
                current_payload["temperature"] = 0
                continue
        raise DeepSeekProtocolError(
            "invalid_json_output",
            _sanitize(last_error),
            usage=total_usage,
            finish_reason=last_result.finish_reason if last_result is not None else None,
            model=last_result.model if last_result is not None else None,
            response_id=last_result.response_id if last_result is not None else None,
            protocol_details={"json_attempts_completed": protocol_retry_limit + 1},
        )

    def structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        model: str = DEFAULT_MODEL,
        thinking: bool = False,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        max_tokens: int | None = None,
        temperature: float | None = None,
        retry_callback: RetryCallback | None = None,
        request_timeout: float | None = None,
        request_retries: int | None = None,
    ) -> JsonChatResult:
        """Generate a schema-constrained object through Flash Responses API.

        DeepSeek documents Responses JSON Schema only for v4 Flash.  Pro stays
        on the existing Chat JSON Output path so its wire contract is not
        guessed or silently ignored.
        """

        selected_model = validate_model(model)
        if selected_model != "deepseek-v4-flash":
            fallback_result = self.chat_json(
                messages,
                model=selected_model,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
                temperature=temperature,
                retry_callback=retry_callback,
                request_timeout=request_timeout,
                request_retries=request_retries,
                json_retries=0,
            )
            fallback_metadata = dict(fallback_result.provider_metadata)
            fallback_metadata.update(
                transport_api="chat_completions",
                structured_format="json_object",
            )
            return replace(fallback_result, provider_metadata=fallback_metadata)
        name = str(schema_name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise DeepSeekConfigurationError(
                "invalid_json_schema", "schema_name must be 1-64 API-safe characters"
            )
        if not isinstance(json_schema, Mapping):
            raise DeepSeekConfigurationError(
                "invalid_json_schema", "json_schema must be an object"
            )
        try:
            safe_schema = json.loads(
                json.dumps(dict(json_schema), ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise DeepSeekConfigurationError(
                "invalid_json_schema", "json_schema must contain portable JSON values"
            ) from exc
        normalized_input: list[dict[str, str]] = []
        for message in messages:
            role = str(message.get("role") or "").strip()
            content = message.get("content")
            if role not in {"system", "user", "assistant"} or not isinstance(content, str):
                raise DeepSeekConfigurationError(
                    "invalid_messages",
                    "Each message needs a system/user/assistant role and string content",
                )
            normalized_input.append({"role": role, "content": content})
        if not normalized_input:
            raise DeepSeekConfigurationError(
                "invalid_messages", "At least one message is required"
            )
        raw_effort = str(reasoning_effort or DEFAULT_REASONING_EFFORT).strip().lower()
        payload: dict[str, Any] = {
            "model": selected_model,
            "input": normalized_input,
            "text": {
                "format": {"type": "json_schema", "name": name, "schema": safe_schema}
            },
            "stream": False,
        }
        if thinking:
            payload["reasoning"] = {"effort": validate_reasoning_effort(raw_effort)}
        elif raw_effort != "auto":
            validate_reasoning_effort(raw_effort)
            payload["reasoning"] = {"effort": "none"}
        if max_tokens is not None:
            if isinstance(max_tokens, bool) or int(max_tokens) <= 0:
                raise DeepSeekConfigurationError(
                    "invalid_max_tokens", "max_tokens must be positive"
                )
            payload["max_output_tokens"] = int(max_tokens)
        if temperature is not None and not thinking:
            selected_temperature = float(temperature)
            if not 0 <= selected_temperature <= 2:
                raise DeepSeekConfigurationError(
                    "invalid_temperature", "temperature must be between 0 and 2"
                )
            payload["temperature"] = selected_temperature

        telemetry = {"provider_requests": 0}
        token = _ACTIVE_TELEMETRY.set(telemetry)
        started = time.perf_counter()
        try:
            data = self._nonstream_data(
                payload,
                retry_callback=retry_callback,
                request_timeout=request_timeout,
                request_retries=request_retries,
                request_factory=self._responses_request,
            )
            result = self._parse_responses_json_result(
                data, requested_model=selected_model
            )
        except ProviderError as exc:
            if exc.requested_model is None:
                exc.requested_model = selected_model
            merge_usage(
                exc.usage,
                {
                    **telemetry,
                    "latency_ms": max(
                        0, round((time.perf_counter() - started) * 1000)
                    ),
                },
            )
            raise
        finally:
            _ACTIVE_TELEMETRY.reset(token)
        call_telemetry = {
            **telemetry,
            "latency_ms": max(0, round((time.perf_counter() - started) * 1000)),
        }
        usage = dict(result.usage)
        merge_usage(usage, call_telemetry)
        metadata = dict(result.provider_metadata)
        metadata.update(call_telemetry)
        return replace(result, usage=usage, provider_metadata=metadata)

    @staticmethod
    def _parse_responses_json_result(
        data: Any, *, requested_model: str
    ) -> JsonChatResult:
        details = _response_error_details(data)
        if not isinstance(data, Mapping):
            raise DeepSeekProtocolError(
                "invalid_response", "DeepSeek Responses result must be an object"
            )
        status = str(data.get("status") or "")
        usage = normalize_usage(data.get("usage") if isinstance(data.get("usage"), Mapping) else {})
        model = str(data.get("model") or "")
        response_id = str(data["id"]) if data.get("id") is not None else None
        if status == "failed":
            error = data.get("error")
            raw_error_code = (
                str(error.get("code") or "response_failed")
                if isinstance(error, Mapping) else "response_failed"
            )
            normalized_error_code = raw_error_code.casefold()
            retryable = normalized_error_code in {
                "server_error", "resource_exhausted", "rate_limit_exceeded",
                "timeout", "temporarily_unavailable",
            }
            error_code = {
                "server_error": "server_error",
                "resource_exhausted": "resource_failure",
                "rate_limit_exceeded": "rate_limited",
                "timeout": "timeout",
                "temporarily_unavailable": "server_error",
            }.get(normalized_error_code, "response_failed")
            message = (
                str(error.get("message") or "DeepSeek Responses request failed")
                if isinstance(error, Mapping) else "DeepSeek Responses request failed"
            )
            raise DeepSeekError(
                error_code,
                _sanitize(message),
                retryable=retryable,
                usage=usage,
                model=model or None,
                requested_model=requested_model,
                response_id=response_id,
                protocol_details={"response_status": status},
            )
        if status == "incomplete":
            incomplete = data.get("incomplete_details")
            reason = (
                str(incomplete.get("reason") or "unknown")
                if isinstance(incomplete, Mapping) else "unknown"
            )
            code = "content_filter" if reason == "content_filter" else "response_incomplete"
            finish = "content_filter" if reason == "content_filter" else "length"
            raise DeepSeekProtocolError(
                code,
                f"DeepSeek Responses result was incomplete: {reason}",
                retryable=False,
                usage=usage,
                finish_reason=finish,
                model=model or None,
                requested_model=requested_model,
                response_id=response_id,
                protocol_details={"response_status": status, "incomplete_reason": reason},
            )
        if status != "completed":
            raise DeepSeekProtocolError(
                "invalid_response_status",
                "DeepSeek Responses result has an invalid terminal status",
                usage=usage,
                model=model or None,
                requested_model=requested_model,
                response_id=response_id,
                protocol_details={"response_status": status, **details},
            )
        content_parts: list[str] = []
        output = data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, Mapping) or item.get("type") != "message":
                    continue
                parts = item.get("content")
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if (
                        isinstance(part, Mapping)
                        and part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)
                    ):
                        content_parts.append(str(part["text"]))
        content = "".join(content_parts)
        try:
            parsed = json.loads(content) if content.strip() else None
        except json.JSONDecodeError as exc:
            raise DeepSeekProtocolError(
                "invalid_json_output",
                _sanitize(f"DeepSeek structured output was invalid JSON: {exc.msg}"),
                usage=usage,
                finish_reason="stop",
                model=model or None,
                requested_model=requested_model,
                response_id=response_id,
                protocol_details={"response_status": status},
            ) from None
        if not isinstance(parsed, dict):
            raise DeepSeekProtocolError(
                "invalid_json_output",
                "DeepSeek structured output must be one JSON object",
                usage=usage,
                finish_reason="stop",
                model=model or None,
                requested_model=requested_model,
                response_id=response_id,
                protocol_details={"response_status": status},
            )
        return JsonChatResult(
            content=content,
            finish_reason="stop",
            usage=usage,
            model=model,
            data=parsed,
            response_id=response_id,
            requested_model=requested_model,
            provider_metadata={
                "transport_api": "responses",
                "response_status": status,
            },
        )

    @staticmethod
    def _decode_json_output(content: str) -> tuple[Any, bool]:
        """Decode the provider's exact JSON body without envelope repair."""

        return json.loads(content), False

    def chat_with_tools(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]],
        tool_handler: Callable[[str, dict[str, Any]], Any],
        model: str = DEFAULT_MODEL,
        thinking: bool = False,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_rounds: int = 6,
        max_tool_calls: int = 12,
        retry_callback: RetryCallback | None = None,
    ) -> ToolChatResult:
        """Run a bounded function-tool loop using caller-owned safe tools."""
        if not callable(tool_handler):
            raise DeepSeekConfigurationError("invalid_tools", "tool_handler must be callable")
        rounds_limit = int(max_rounds)
        calls_limit = int(max_tool_calls)
        if not 1 <= rounds_limit <= 12 or not 1 <= calls_limit <= 64:
            raise DeepSeekConfigurationError(
                "invalid_tools", "Tool round/call limits are outside the supported range"
            )
        normalized_tools = self._validated_tools(tools)
        allowed = {str(item["function"]["name"]) for item in normalized_tools}
        initial = self._payload(
            messages,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            stream=False,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        history: list[dict[str, Any]] = [dict(item) for item in initial["messages"]]
        total_usage: dict[str, Any] = {}
        call_count = 0
        response_id: str | None = None
        response_model = validate_model(model)
        for round_index in range(rounds_limit + 1):
            payload = {key: value for key, value in initial.items() if key != "messages"}
            payload.update(
                {"messages": history, "tools": normalized_tools, "tool_choice": "auto"}
            )
            data = self._nonstream_data(
                payload,
                retry_callback=retry_callback,
            )
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                raise DeepSeekProtocolError(
                    "invalid_response", "DeepSeek tool response has no completion choice"
                )
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, Mapping):
                raise DeepSeekProtocolError(
                    "invalid_response", "DeepSeek tool response has no assistant message"
                )
            usage = data.get("usage")
            if isinstance(usage, Mapping):
                merge_usage(total_usage, usage)
            if data.get("id") is not None:
                response_id = str(data["id"])
            if data.get("model") is not None:
                response_model = str(data["model"])
            finish_reason = (
                str(choice["finish_reason"])
                if choice.get("finish_reason") is not None
                else None
            )
            raw_calls = message.get("tool_calls")
            if not raw_calls:
                content = message.get("content")
                if content is None:
                    content = ""
                if not isinstance(content, str):
                    raise DeepSeekProtocolError(
                        "invalid_response", "DeepSeek assistant content is not text"
                    )
                return ToolChatResult(
                    content=content,
                    finish_reason=finish_reason,
                    usage=total_usage,
                    model=response_model,
                    tool_rounds=round_index,
                    tool_calls=call_count,
                    response_id=response_id,
                    requested_model=model,
                )
            if round_index >= rounds_limit or not isinstance(raw_calls, list):
                raise DeepSeekProtocolError(
                    "tool_limit_exceeded", "DeepSeek tool loop exceeded its configured limit"
                )
            assistant_calls: list[dict[str, Any]] = []
            pending: list[tuple[str, str, dict[str, Any]]] = []
            for raw_call in raw_calls:
                if not isinstance(raw_call, Mapping) or not isinstance(raw_call.get("function"), Mapping):
                    raise DeepSeekProtocolError("invalid_tool_call", "Malformed DeepSeek tool call")
                call_id = str(raw_call.get("id") or "")
                function = raw_call["function"]
                name = str(function.get("name") or "")
                if not call_id or name not in allowed:
                    raise DeepSeekProtocolError("invalid_tool_call", "Unknown or missing tool call")
                try:
                    arguments = json.loads(str(function.get("arguments") or "{}"))
                except json.JSONDecodeError as exc:
                    raise DeepSeekProtocolError(
                        "invalid_tool_call",
                        _sanitize(f"Tool arguments were invalid JSON: {exc.msg}"),
                    ) from None
                if not isinstance(arguments, dict):
                    raise DeepSeekProtocolError(
                        "invalid_tool_call", "Tool arguments must be a JSON object"
                    )
                call_count += 1
                if call_count > calls_limit:
                    raise DeepSeekProtocolError(
                        "tool_limit_exceeded", "DeepSeek emitted too many tool calls"
                    )
                encoded_arguments = json.dumps(arguments, ensure_ascii=False)
                assistant_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": encoded_arguments},
                    }
                )
                pending.append((call_id, name, arguments))
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": str(message.get("content") or ""),
                "tool_calls": assistant_calls,
            }
            # Thinking tool calls require reasoning_content to be sent back in
            # the following tool-result request. It remains ephemeral here: it
            # is neither exposed in ToolChatResult nor persisted by callers.
            reasoning_content = message.get("reasoning_content")
            if thinking and isinstance(reasoning_content, str) and reasoning_content:
                assistant_message["reasoning_content"] = reasoning_content
            history.append(assistant_message)
            for call_id, name, arguments in pending:
                output = tool_handler(name, arguments)
                content = output if isinstance(output, str) else json.dumps(
                    output, ensure_ascii=False, default=str
                )
                history.append(
                    {"role": "tool", "tool_call_id": call_id, "content": content}
                )
        raise DeepSeekProtocolError(
            "tool_limit_exceeded", "DeepSeek tool loop exceeded its configured limit"
        )

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str = DEFAULT_MODEL,
        thinking: bool = True,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Iterator[StreamEvent]:
        payload = self._payload(
            messages,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            stream=True,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return self._stream(payload)

    def _stream(self, payload: Mapping[str, Any]) -> Iterator[StreamEvent]:
        emitted_content = False
        accumulated_usage: dict[str, Any] = {}
        requested_model = str(payload.get("model") or "") or None
        request_count_before = self.provider_request_count
        started = time.perf_counter()

        def stream_telemetry() -> dict[str, int]:
            return {
                "provider_requests": max(
                    0, self.provider_request_count - request_count_before
                ),
                "protocol_repairs": 0,
                "latency_ms": max(
                    0, round((time.perf_counter() - started) * 1000)
                ),
            }

        for attempt in range(self.retries + 1):
            finish_reason: str | None = None
            usage: dict[str, Any] | None = None
            response_model: str | None = None
            response_id: str | None = None
            try:
                response = self._open(payload)
                with _managed_response(response) as opened:
                    for event_data in _iter_sse_data(opened):
                        if event_data == "[DONE]":
                            final_usage = dict(accumulated_usage)
                            merge_usage(final_usage, usage or {})
                            telemetry = stream_telemetry()
                            merge_usage(final_usage, telemetry)
                            yield StreamEvent(
                                "done",
                                finish_reason=finish_reason,
                                usage=final_usage,
                                model=response_model,
                                response_id=response_id,
                                requested_model=requested_model,
                                provider_metadata=telemetry,
                            )
                            return
                        try:
                            chunk = json.loads(event_data)
                        except json.JSONDecodeError as exc:
                            raise DeepSeekProtocolError(
                                "invalid_stream",
                                _sanitize(f"DeepSeek returned invalid SSE JSON: {exc}"),
                            ) from None
                        if not isinstance(chunk, Mapping):
                            raise DeepSeekProtocolError(
                                "invalid_stream", "DeepSeek SSE chunk must be a JSON object"
                            )
                        if chunk.get("model") is not None:
                            response_model = str(chunk["model"])
                        if chunk.get("id") is not None:
                            response_id = str(chunk["id"])
                        chunk_usage = chunk.get("usage")
                        if isinstance(chunk_usage, Mapping):
                            usage = normalize_usage(chunk_usage)
                            yield StreamEvent(
                                "usage",
                                usage=usage,
                                model=response_model,
                                response_id=response_id,
                                requested_model=requested_model,
                            )
                        choices = chunk.get("choices")
                        if not isinstance(choices, list):
                            raise DeepSeekProtocolError(
                                "invalid_stream", "DeepSeek SSE chunk has invalid choices"
                            )
                        for choice in choices[:1]:
                            if not isinstance(choice, Mapping):
                                continue
                            if choice.get("finish_reason") is not None:
                                finish_reason = str(choice["finish_reason"])
                            delta = choice.get("delta")
                            if not isinstance(delta, Mapping):
                                continue
                            reasoning = delta.get("reasoning_content")
                            if isinstance(reasoning, str) and reasoning:
                                yield StreamEvent(
                                    "heartbeat",
                                    model=response_model,
                                    response_id=response_id,
                                    requested_model=requested_model,
                                )
                            content = delta.get("content")
                            if isinstance(content, str) and content:
                                emitted_content = True
                                yield StreamEvent(
                                    "delta",
                                    content=content,
                                    model=response_model,
                                    response_id=response_id,
                                    requested_model=requested_model,
                                )
                raise DeepSeekProtocolError(
                    "incomplete_stream",
                    "DeepSeek SSE stream ended before [DONE]",
                    finish_reason=finish_reason,
                    model=response_model,
                    requested_model=requested_model,
                    response_id=response_id,
                )
            except DeepSeekError as exc:
                if emitted_content or not exc.retryable or attempt >= self.retries:
                    prior_usage = dict(accumulated_usage)
                    merge_usage(prior_usage, usage or {})
                    merge_usage(prior_usage, exc.usage)
                    exc.usage = prior_usage
                    if exc.requested_model is None:
                        exc.requested_model = requested_model
                    merge_usage(exc.usage, stream_telemetry())
                    raise
                merge_usage(accumulated_usage, usage or {})
                merge_usage(accumulated_usage, exc.usage)
                self._sleep_before_retry(
                    attempt,
                    code=exc.code,
                    retry_after_seconds=exc.protocol_details.get(
                        "retry_after_seconds"
                    ),
                )
            except _NETWORK_EXCEPTIONS as exc:
                code = (
                    "timeout"
                    if isinstance(exc, (TimeoutError, socket.timeout))
                    else "network_error"
                )
                mapped = DeepSeekError(
                    code,
                    _sanitize(f"DeepSeek stream failed: {exc}", self._api_key),
                    retryable=True,
                )
                if emitted_content or attempt >= self.retries:
                    merge_usage(mapped.usage, accumulated_usage)
                    merge_usage(mapped.usage, usage or {})
                    merge_usage(mapped.usage, stream_telemetry())
                    raise mapped from None
                merge_usage(accumulated_usage, usage or {})
                self._sleep_before_retry(attempt, code=mapped.code)
        raise AssertionError("unreachable")
