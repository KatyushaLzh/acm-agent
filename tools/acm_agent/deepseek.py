"""Small, dependency-free DeepSeek Chat Completions client.

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
"""

from __future__ import annotations

import json
import http.client
import math
import os
import random as _random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from .ai_policy import (
    ALLOWED_MODELS,
    ALLOWED_REASONING_EFFORTS,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
)
from .usage import merge_usage, normalize_usage


DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
MAX_RETRY_BACKOFF_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 60.0
_CANCELLABLE_SLEEP_SLICE_SECONDS = 0.1
# Transport-level ceiling for a single HTTP request.  This is deliberately
# looser than the stress preparation policy caps in stress_budget
# (MAX_THINKING_REQUEST_SECONDS / MAX_NON_THINKING_REQUEST_SECONDS /
# MAX_AUDIT_REQUEST_SECONDS), which clamp the value before it ever reaches this
# client.  Keep the names distinct: this one bounds the socket, those bound the
# preparation budget.
MAX_TRANSPORT_REQUEST_SECONDS = 300.0
_READ_CHUNK_BYTES = 64 * 1024
_NETWORK_EXCEPTIONS = (
    urllib.error.URLError,
    TimeoutError,
    socket.timeout,
    OSError,
    http.client.IncompleteRead,
    http.client.HTTPException,
)


class Transport(Protocol):
    def __call__(self, request: urllib.request.Request, timeout: float) -> Any: ...


RetryCallback = Callable[[int, int, str, float], None]


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    finish_reason: str | None
    usage: dict[str, Any]
    model: str
    response_id: str | None = None


@dataclass(frozen=True, slots=True)
class JsonChatResult:
    content: str
    finish_reason: str | None
    usage: dict[str, Any]
    model: str
    data: dict[str, Any]
    response_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolChatResult:
    content: str
    finish_reason: str | None
    usage: dict[str, Any]
    model: str
    tool_rounds: int
    tool_calls: int
    response_id: str | None = None


@dataclass(frozen=True, slots=True)
class StreamEvent:
    kind: str
    content: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    model: str | None = None
    response_id: str | None = None


class DeepSeekError(RuntimeError):
    """A safe-to-display DeepSeek failure.

    ``message`` is sanitized before it reaches this object. The exception never
    retains the API key, the Authorization header, or the raw response body.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        usage: Mapping[str, Any] | None = None,
        finish_reason: str | None = None,
        model: str | None = None,
        response_id: str | None = None,
        protocol_details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.usage = dict(usage or {})
        self.finish_reason = finish_reason
        self.model = model
        self.response_id = response_id
        self.protocol_details = dict(protocol_details or {})

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "status": self.status,
            "retryable": self.retryable,
        }
        if self.usage:
            result["usage"] = dict(self.usage)
        if self.finish_reason is not None:
            result["finish_reason"] = self.finish_reason
        if self.model is not None:
            result["model"] = self.model
        if self.response_id is not None:
            result["response_id"] = self.response_id
        if self.protocol_details:
            result["protocol_details"] = dict(self.protocol_details)
        return result


class DeepSeekConfigurationError(DeepSeekError):
    pass


class DeepSeekProtocolError(DeepSeekError):
    pass


class DeepSeekCancelledError(DeepSeekError):
    def __init__(
        self,
        *,
        usage: Mapping[str, Any] | None = None,
        finish_reason: str | None = None,
        model: str | None = None,
        response_id: str | None = None,
        protocol_details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            "request_cancelled",
            "DeepSeek request was cancelled",
            retryable=False,
            usage=usage,
            finish_reason=finish_reason,
            model=model,
            response_id=response_id,
            protocol_details=protocol_details,
        )


def _abort_response(response: Any) -> None:
    """Interrupt a blocking urllib read before closing its response wrapper.

    On Windows, ``HTTPResponse.close()`` alone does not reliably wake another
    thread blocked in ``recv``. Shutting down the owned socket first makes the
    absolute preparation deadline observable by that reader.
    """

    for chain in (
        ("fp", "raw", "_sock"),
        ("fp", "_sock"),
        ("raw", "_sock"),
        ("_sock",),
        ("sock",),
    ):
        candidate = response
        for attribute in chain:
            candidate = getattr(candidate, attribute, None)
            if candidate is None:
                break
        shutdown = getattr(candidate, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            break
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class DeepSeekCancelScope:
    """Thread-safe cancellation shared by one logical operation tree."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._responses: dict[int, Any] = {}

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def event(self) -> threading.Event:
        """Cancellation event for read-only ``is_set``/``wait`` integration."""
        return self._cancelled

    @property
    def cancellation_event(self) -> threading.Event:
        """Deprecated compatibility alias for :attr:`event`."""

        return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled.set()
            responses = list(self._responses.values())
            self._responses.clear()
        for response in responses:
            self._close(response)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise DeepSeekCancelledError()

    def register_response(self, response: Any) -> None:
        """Track a cancellable response owned by this logical operation."""
        self._register(response)

    def unregister_response(self, response: Any) -> None:
        """Stop tracking a response once its owner has closed it."""
        self._unregister(response)

    def _register(self, response: Any) -> None:
        with self._lock:
            if self._cancelled.is_set():
                should_cancel = True
            else:
                self._responses[id(response)] = response
                should_cancel = False
        if should_cancel:
            self._close(response)
            raise DeepSeekCancelledError()

    def _unregister(self, response: Any) -> None:
        with self._lock:
            self._responses.pop(id(response), None)

    @staticmethod
    def _close(response: Any) -> None:
        _abort_response(response)


# Deprecated compatibility aliases.  Keep them for one transition cycle so
# embedders importing the old names do not break during the cleanup release.
CancellationScope = DeepSeekCancelScope
CancelScope = DeepSeekCancelScope
CancellationToken = DeepSeekCancelScope


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
    return DeepSeekError(
        _error_code_for_status(status),
        _sanitize(_provider_message(body, status), secret),
        status=status,
        retryable=status in RETRYABLE_HTTP_STATUSES,
        protocol_details=protocol_details,
    )


@contextmanager
def _managed_response(
    response: Any,
    cancel_scope: DeepSeekCancelScope | None = None,
) -> Iterator[Any]:
    enter = getattr(response, "__enter__", None)
    if callable(enter):
        try:
            with response as opened:
                yield opened
        finally:
            if cancel_scope is not None:
                cancel_scope._unregister(response)
        return
    try:
        yield response
    finally:
        close = getattr(response, "close", None)
        try:
            if callable(close):
                close()
        finally:
            if cancel_scope is not None:
                cancel_scope._unregister(response)


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
        if isinstance(raw, bytes):
            line = raw.decode("utf-8-sig")
        else:
            line = str(raw)
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
        monotonic: Callable[[], float] = time.monotonic,
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
        self._monotonic = monotonic
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

    @staticmethod
    def _default_transport(request: urllib.request.Request, timeout: float) -> Any:
        return urllib.request.urlopen(request, timeout=timeout)

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

    def _operation_deadline(
        self,
        *,
        deadline: float | None,
        total_timeout: float | None,
    ) -> float | None:
        resolved: float | None = None
        if deadline is not None:
            resolved = float(deadline)
            if not math.isfinite(resolved):
                raise DeepSeekConfigurationError(
                    "invalid_timeout", "deadline must be a finite monotonic timestamp"
                )
        if total_timeout is not None:
            total = float(total_timeout)
            if not math.isfinite(total) or total <= 0:
                raise DeepSeekConfigurationError(
                    "invalid_timeout", "total_timeout must be positive and finite"
                )
            total_deadline = self._monotonic() + total
            resolved = total_deadline if resolved is None else min(resolved, total_deadline)
        return resolved

    def _remaining(self, deadline: float | None) -> float | None:
        return None if deadline is None else deadline - self._monotonic()

    def _deadline_error(
        self,
        *,
        status: int | None = None,
        usage: Mapping[str, Any] | None = None,
        finish_reason: str | None = None,
        model: str | None = None,
        response_id: str | None = None,
        protocol_details: Mapping[str, Any] | None = None,
    ) -> DeepSeekError:
        return DeepSeekError(
            "timeout",
            "DeepSeek operation deadline exceeded",
            status=status,
            retryable=False,
            usage=usage,
            finish_reason=finish_reason,
            model=model,
            response_id=response_id,
            protocol_details=protocol_details,
        )

    @staticmethod
    def _check_cancel(cancel_scope: DeepSeekCancelScope | None) -> None:
        if cancel_scope is not None:
            cancel_scope.raise_if_cancelled()

    @staticmethod
    def _cancelled_error(
        *,
        usage: Mapping[str, Any] | None = None,
        finish_reason: str | None = None,
        model: str | None = None,
        response_id: str | None = None,
        protocol_details: Mapping[str, Any] | None = None,
    ) -> DeepSeekCancelledError:
        return DeepSeekCancelledError(
            usage=usage,
            finish_reason=finish_reason,
            model=model,
            response_id=response_id,
            protocol_details=protocol_details,
        )

    def _provider_timeout(
        self,
        request_timeout: float | None,
        deadline: float | None,
    ) -> float:
        configured = self.timeout if request_timeout is None else float(request_timeout)
        if not math.isfinite(configured) or configured <= 0:
            raise DeepSeekConfigurationError(
                "invalid_timeout", "request_timeout must be positive and finite"
            )
        timeout = min(MAX_TRANSPORT_REQUEST_SECONDS, configured)
        remaining = self._remaining(deadline)
        if remaining is not None:
            if remaining <= 0:
                raise self._deadline_error()
            timeout = min(timeout, remaining)
        return timeout

    @staticmethod
    def _close_response(response: Any) -> None:
        _abort_response(response)

    def _read_response_body(
        self,
        response: Any,
        *,
        deadline: float | None,
        status: int | None = None,
        cancel_scope: DeepSeekCancelScope | None = None,
    ) -> bytes:
        """Read a body without allowing repeated chunks to reset the deadline.

        The provider socket timeout bounds each blocking read. The watchdog also
        closes a live response at the absolute deadline so a read already in
        progress cannot continue indefinitely on keep-alive traffic.
        """
        expired = threading.Event()
        timer: threading.Timer | None = None
        if deadline is not None:
            remaining = self._remaining(deadline)
            if remaining is None or remaining <= 0:
                self._close_response(response)
                raise self._deadline_error(status=status)

            def close_at_deadline() -> None:
                expired.set()
                self._close_response(response)

            timer = threading.Timer(remaining, close_at_deadline)
            timer.daemon = True
            timer.start()

        chunks: list[bytes] = []

        def available_details(extra: Any = None) -> dict[str, Any]:
            available = list(chunks)
            if isinstance(extra, str):
                available.append(extra.encode("utf-8"))
            elif isinstance(extra, (bytes, bytearray, memoryview)):
                available.append(bytes(extra))
            if available:
                try:
                    decoded = json.loads(b"".join(available).decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                    return {}
                else:
                    return _response_error_details(decoded)
            return {}

        def deadline_with_available_details(extra: Any = None) -> DeepSeekError:
            return self._deadline_error(status=status, **available_details(extra))

        def cancelled_with_available_details(extra: Any = None) -> DeepSeekCancelledError:
            return self._cancelled_error(**available_details(extra))

        try:
            while True:
                if cancel_scope is not None and cancel_scope.cancelled:
                    self._close_response(response)
                    raise cancelled_with_available_details()
                remaining = self._remaining(deadline)
                if expired.is_set() or (remaining is not None and remaining <= 0):
                    self._close_response(response)
                    raise deadline_with_available_details()
                try:
                    chunk = response.read(_READ_CHUNK_BYTES)
                except Exception:
                    if cancel_scope is not None and cancel_scope.cancelled:
                        self._close_response(response)
                        raise cancelled_with_available_details() from None
                    remaining = self._remaining(deadline)
                    if expired.is_set() or (remaining is not None and remaining <= 0):
                        self._close_response(response)
                        raise deadline_with_available_details() from None
                    raise
                if cancel_scope is not None and cancel_scope.cancelled:
                    self._close_response(response)
                    raise cancelled_with_available_details(chunk)
                remaining = self._remaining(deadline)
                if expired.is_set() or (remaining is not None and remaining <= 0):
                    self._close_response(response)
                    raise deadline_with_available_details(chunk)
                if chunk in (b"", "", None):
                    break
                if isinstance(chunk, str):
                    chunks.append(chunk.encode("utf-8"))
                elif isinstance(chunk, (bytes, bytearray, memoryview)):
                    chunks.append(bytes(chunk))
                else:
                    raise TypeError("DeepSeek response read returned a non-byte chunk")
            return b"".join(chunks)
        finally:
            if timer is not None:
                timer.cancel()

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
        effort = validate_reasoning_effort(reasoning_effort)
        payload: dict[str, Any] = {
            "model": model,
            "messages": normalized_messages,
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "stream": stream,
        }
        if thinking:
            payload["reasoning_effort"] = effort
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
        deadline: float | None = None,
        cancel_scope: DeepSeekCancelScope | None = None,
    ) -> Any:
        self._check_cancel(cancel_scope)
        request = self._request(payload)
        timeout = self._provider_timeout(request_timeout, deadline)
        with self._request_count_lock:
            self._request_count += 1
        try:
            response = self._transport(request, timeout)
        except urllib.error.HTTPError as exc:
            retry_after = _retry_after_seconds(exc, now=self._wall_time())
            if cancel_scope is not None:
                cancel_scope._register(exc)
            try:
                body = self._read_response_body(
                    exc,
                    deadline=deadline,
                    status=int(exc.code),
                    cancel_scope=cancel_scope,
                )
            except DeepSeekError:
                raise
            except Exception:
                body = b""
            finally:
                self._close_response(exc)
                if cancel_scope is not None:
                    cancel_scope._unregister(exc)
            raise _http_error(
                int(exc.code),
                body,
                self._api_key,
                retry_after_seconds=retry_after,
            ) from None
        except _NETWORK_EXCEPTIONS as exc:
            self._check_cancel(cancel_scope)
            remaining = self._remaining(deadline)
            if remaining is not None and remaining <= 0:
                raise self._deadline_error() from None
            code = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "network_error"
            raise DeepSeekError(
                code,
                _sanitize(f"DeepSeek network request failed: {exc}", self._api_key),
                retryable=True,
            ) from None
        if cancel_scope is not None:
            cancel_scope._register(response)
        self._check_cancel(cancel_scope)
        remaining = self._remaining(deadline)
        if remaining is not None and remaining <= 0:
            self._close_response(response)
            if cancel_scope is not None:
                cancel_scope._unregister(response)
            raise self._deadline_error(status=_response_status(response))
        status = _response_status(response)
        self._check_cancel(cancel_scope)
        if status >= 400:
            retry_after = _retry_after_seconds(response, now=self._wall_time())
            with _managed_response(response, cancel_scope) as opened:
                try:
                    body = self._read_response_body(
                        opened,
                        deadline=deadline,
                        status=status,
                        cancel_scope=cancel_scope,
                    )
                except DeepSeekError:
                    raise
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
        deadline: float | None = None,
        cancel_scope: DeepSeekCancelScope | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self._check_cancel(cancel_scope)
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
        remaining = self._remaining(deadline)
        if remaining is not None:
            if remaining <= 0:
                raise self._deadline_error()
            delay = min(delay, remaining)
        if retry_callback is not None:
            try:
                retry_callback(attempt + 1, total, code, delay)
            except Exception:
                # Observability callbacks must never change provider behavior.
                pass
        self._check_cancel(cancel_scope)
        if cancel_scope is None:
            self._sleep(delay)
        else:
            sleep_deadline = self._monotonic() + delay
            while True:
                self._check_cancel(cancel_scope)
                remaining_sleep = sleep_deadline - self._monotonic()
                if remaining_sleep <= 0:
                    break
                self._sleep(
                    min(_CANCELLABLE_SLEEP_SLICE_SECONDS, remaining_sleep)
                )
        self._check_cancel(cancel_scope)
        remaining = self._remaining(deadline)
        if remaining is not None and remaining <= 0:
            raise self._deadline_error()

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
        deadline: float | None = None,
        cancel_scope: DeepSeekCancelScope | None = None,
    ) -> Mapping[str, Any]:
        retry_limit = self.retries if request_retries is None else int(request_retries)
        if retry_limit < 0:
            raise DeepSeekConfigurationError(
                "invalid_retries", "request_retries must not be negative"
            )
        for attempt in range(retry_limit + 1):
            try:
                self._check_cancel(cancel_scope)
                response = self._open(
                    payload,
                    request_timeout=request_timeout,
                    deadline=deadline,
                    cancel_scope=cancel_scope,
                )
                with _managed_response(response, cancel_scope) as opened:
                    raw = self._read_response_body(
                        opened,
                        deadline=deadline,
                        cancel_scope=cancel_scope,
                    )
                try:
                    data = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
                    raise DeepSeekProtocolError(
                        "invalid_response",
                        _sanitize(f"DeepSeek returned invalid JSON: {exc}"),
                    ) from None
                if not isinstance(data, Mapping):
                    raise DeepSeekProtocolError(
                        "invalid_response", "DeepSeek response must be a JSON object"
                    )
                if cancel_scope is not None and cancel_scope.cancelled:
                    raise self._cancelled_error(**_response_error_details(data))
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise self._deadline_error(**_response_error_details(data))
                return data
            except DeepSeekError as exc:
                if not exc.retryable or attempt >= retry_limit:
                    raise self._retry_exhausted(
                        exc, retry_limit=retry_limit
                    ) from None
                self._sleep_before_retry(
                    attempt,
                    code=exc.code,
                    retry_callback=retry_callback,
                    retry_limit=retry_limit,
                    deadline=deadline,
                    cancel_scope=cancel_scope,
                    retry_after_seconds=exc.protocol_details.get(
                        "retry_after_seconds"
                    ),
                )
            except _NETWORK_EXCEPTIONS as exc:
                self._check_cancel(cancel_scope)
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise self._deadline_error() from None
                code = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "network_error"
                mapped = DeepSeekError(
                    code,
                    _sanitize(f"DeepSeek response read failed: {exc}", self._api_key),
                    retryable=True,
                )
                if attempt >= retry_limit:
                    raise self._retry_exhausted(
                        mapped, retry_limit=retry_limit
                    ) from None
                self._sleep_before_retry(
                    attempt,
                    code=mapped.code,
                    retry_callback=retry_callback,
                    retry_limit=retry_limit,
                    deadline=deadline,
                    cancel_scope=cancel_scope,
                )
            except Exception:
                self._check_cancel(cancel_scope)
                raise
        raise AssertionError("unreachable")

    def _nonstream(
        self,
        payload: Mapping[str, Any],
        *,
        retry_callback: RetryCallback | None = None,
        request_timeout: float | None = None,
        request_retries: int | None = None,
        deadline: float | None = None,
        cancel_scope: DeepSeekCancelScope | None = None,
    ) -> ChatResult:
        return self._parse_chat_result(
            self._nonstream_data(
                payload,
                retry_callback=retry_callback,
                request_timeout=request_timeout,
                request_retries=request_retries,
                deadline=deadline,
                cancel_scope=cancel_scope,
            )
        )

    @staticmethod
    def _parse_chat_result(data: Any) -> ChatResult:
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
        deadline: float | None = None,
        total_timeout: float | None = None,
        cancel_scope: DeepSeekCancelScope | None = None,
    ) -> ChatResult:
        self._check_cancel(cancel_scope)
        payload = self._payload(
            messages,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            stream=False,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        operation_deadline = self._operation_deadline(
            deadline=deadline, total_timeout=total_timeout
        )
        return self._nonstream(
            payload,
            retry_callback=retry_callback,
            request_timeout=request_timeout,
            request_retries=request_retries,
            deadline=operation_deadline,
            cancel_scope=cancel_scope,
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
        deadline: float | None = None,
        total_timeout: float | None = None,
        cancel_scope: DeepSeekCancelScope | None = None,
    ) -> JsonChatResult:
        self._check_cancel(cancel_scope)
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
        operation_deadline = self._operation_deadline(
            deadline=deadline, total_timeout=total_timeout
        )
        last_error = "DeepSeek JSON Output was empty"
        current_payload = payload
        total_usage: dict[str, Any] = {}
        last_result: ChatResult | None = None
        for json_attempt in range(protocol_retry_limit + 1):
            try:
                self._check_cancel(cancel_scope)
                result = self._nonstream(
                    current_payload,
                    retry_callback=retry_callback,
                    request_timeout=request_timeout,
                    request_retries=request_retries,
                    deadline=operation_deadline,
                    cancel_scope=cancel_scope,
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
            try:
                parsed = json.loads(result.content) if result.content.strip() else None
            except json.JSONDecodeError as exc:
                parsed = None
                last_error = f"DeepSeek JSON Output was invalid: {exc.msg}"
            remaining = self._remaining(operation_deadline)
            if remaining is not None and remaining <= 0:
                raise self._deadline_error(
                    usage=total_usage,
                    finish_reason=result.finish_reason,
                    model=result.model,
                    response_id=result.response_id,
                    protocol_details={"json_attempts_completed": json_attempt + 1},
                )
            if cancel_scope is not None and cancel_scope.cancelled:
                raise self._cancelled_error(
                    usage=total_usage,
                    finish_reason=result.finish_reason,
                    model=result.model,
                    response_id=result.response_id,
                    protocol_details={"json_attempts_completed": json_attempt + 1},
                )
            if isinstance(parsed, dict):
                return JsonChatResult(
                    content=result.content,
                    finish_reason=result.finish_reason,
                    usage=total_usage,
                    model=result.model,
                    response_id=result.response_id,
                    data=parsed,
                )
            if parsed is not None:
                last_error = "DeepSeek JSON Output must be an object"
            elif result.finish_reason == "length":
                last_error = "DeepSeek JSON Output was empty because the completion token limit was reached"
            if json_attempt < protocol_retry_limit:
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
        cancel_scope: DeepSeekCancelScope | None = None,
    ) -> ToolChatResult:
        """Run a bounded function-tool loop using caller-owned safe tools."""
        self._check_cancel(cancel_scope)
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
                cancel_scope=cancel_scope,
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
        cancel_scope: DeepSeekCancelScope | None = None,
    ) -> Iterator[StreamEvent]:
        self._check_cancel(cancel_scope)
        payload = self._payload(
            messages,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            stream=True,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return self._stream(payload, cancel_scope=cancel_scope)

    def _stream(
        self,
        payload: Mapping[str, Any],
        *,
        cancel_scope: DeepSeekCancelScope | None = None,
    ) -> Iterator[StreamEvent]:
        emitted_content = False
        for attempt in range(self.retries + 1):
            finish_reason: str | None = None
            usage: dict[str, Any] | None = None
            response_model: str | None = None
            response_id: str | None = None
            try:
                self._check_cancel(cancel_scope)
                response = self._open(payload, cancel_scope=cancel_scope)
                with _managed_response(response, cancel_scope) as opened:
                    for event_data in _iter_sse_data(opened):
                        self._check_cancel(cancel_scope)
                        if event_data == "[DONE]":
                            yield StreamEvent(
                                "done",
                                finish_reason=finish_reason,
                                usage=usage,
                                model=response_model,
                                response_id=response_id,
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
                            # reasoning_content is intentionally neither returned nor persisted.
                            reasoning = delta.get("reasoning_content")
                            if isinstance(reasoning, str) and reasoning:
                                # A content-free pulse lets the downstream SSE
                                # writer notice disconnects during long thinking.
                                yield StreamEvent(
                                    "heartbeat",
                                    model=response_model,
                                    response_id=response_id,
                                )
                            content = delta.get("content")
                            if isinstance(content, str) and content:
                                emitted_content = True
                                yield StreamEvent(
                                    "delta",
                                    content=content,
                                    model=response_model,
                                    response_id=response_id,
                                )
                self._check_cancel(cancel_scope)
                raise DeepSeekProtocolError(
                    "incomplete_stream", "DeepSeek SSE stream ended before [DONE]"
                )
            except DeepSeekError as exc:
                if emitted_content or not exc.retryable or attempt >= self.retries:
                    raise
                self._sleep_before_retry(
                    attempt,
                    code=exc.code,
                    cancel_scope=cancel_scope,
                    retry_after_seconds=exc.protocol_details.get(
                        "retry_after_seconds"
                    ),
                )
            except _NETWORK_EXCEPTIONS as exc:
                self._check_cancel(cancel_scope)
                code = (
                    "timeout"
                    if isinstance(exc, (TimeoutError, socket.timeout))
                    else "network_error"
                )
                mapped = DeepSeekError(
                    code,
                    _sanitize(
                        f"DeepSeek stream failed: {exc}", self._api_key
                    ),
                    retryable=True,
                )
                if emitted_content or attempt >= self.retries:
                    raise mapped from None
                self._sleep_before_retry(
                    attempt,
                    code=mapped.code,
                    cancel_scope=cancel_scope,
                )
            except Exception:
                self._check_cancel(cancel_scope)
                raise
        raise AssertionError("unreachable")
