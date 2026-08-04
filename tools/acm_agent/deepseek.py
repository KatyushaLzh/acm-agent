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
import os
import re
import socket
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence


DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
ALLOWED_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
DEFAULT_MODEL = "deepseek-v4-flash"
ALLOWED_REASONING_EFFORTS = frozenset({"high", "max"})
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 503})
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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "status": self.status,
            "retryable": self.retryable,
        }


class DeepSeekConfigurationError(DeepSeekError):
    pass


class DeepSeekProtocolError(DeepSeekError):
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
        400: "invalid_request",
        401: "authentication_failed",
        402: "insufficient_balance",
        422: "invalid_request",
        429: "rate_limited",
        500: "server_error",
        503: "server_error",
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


def _http_error(status: int, body: bytes, secret: str | None) -> DeepSeekError:
    return DeepSeekError(
        _error_code_for_status(status),
        _sanitize(_provider_message(body, status), secret),
        status=status,
        retryable=status in RETRYABLE_HTTP_STATUSES,
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

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(key_detected={self.key_detected}, "
            f"timeout={self.timeout!r}, retries={self.retries!r})"
        )

    @property
    def key_detected(self) -> bool:
        return bool(self._api_key)

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
        if temperature is not None:
            value = float(temperature)
            if not 0 <= value <= 2:
                raise DeepSeekConfigurationError(
                    "invalid_temperature", "temperature must be between 0 and 2"
                )
            payload["temperature"] = value
        return payload

    def _open(self, payload: Mapping[str, Any]) -> Any:
        request = self._request(payload)
        try:
            response = self._transport(request, self.timeout)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:
                body = b""
            raise _http_error(int(exc.code), body, self._api_key) from None
        except _NETWORK_EXCEPTIONS as exc:
            code = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "network_error"
            raise DeepSeekError(
                code,
                _sanitize(f"DeepSeek network request failed: {exc}", self._api_key),
                retryable=True,
            ) from None
        status = _response_status(response)
        if status >= 400:
            with _managed_response(response) as opened:
                try:
                    body = opened.read()
                except Exception:
                    body = b""
            raise _http_error(status, body, self._api_key)
        return response

    def _sleep_before_retry(self, attempt: int) -> None:
        self._sleep(float(2**attempt))

    def _nonstream(self, payload: Mapping[str, Any]) -> ChatResult:
        for attempt in range(self.retries + 1):
            try:
                response = self._open(payload)
                with _managed_response(response) as opened:
                    raw = opened.read()
                try:
                    data = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
                    raise DeepSeekProtocolError(
                        "invalid_response",
                        _sanitize(f"DeepSeek returned invalid JSON: {exc}"),
                    ) from None
                return self._parse_chat_result(data)
            except DeepSeekError as exc:
                if not exc.retryable or attempt >= self.retries:
                    raise
                self._sleep_before_retry(attempt)
            except _NETWORK_EXCEPTIONS as exc:
                code = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "network_error"
                mapped = DeepSeekError(
                    code,
                    _sanitize(f"DeepSeek response read failed: {exc}", self._api_key),
                    retryable=True,
                )
                if attempt >= self.retries:
                    raise mapped from None
                self._sleep_before_retry(attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _parse_chat_result(data: Any) -> ChatResult:
        if not isinstance(data, Mapping):
            raise DeepSeekProtocolError(
                "invalid_response", "DeepSeek response must be a JSON object"
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise DeepSeekProtocolError(
                "invalid_response", "DeepSeek response has no completion choice"
            )
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise DeepSeekProtocolError(
                "invalid_response", "DeepSeek completion has no assistant message"
            )
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise DeepSeekProtocolError(
                "invalid_response", "DeepSeek assistant content is not text"
            )
        usage = data.get("usage")
        return ChatResult(
            content=content,
            finish_reason=(
                str(choice["finish_reason"])
                if choice.get("finish_reason") is not None
                else None
            ),
            usage=dict(usage) if isinstance(usage, Mapping) else {},
            model=str(data.get("model") or ""),
            response_id=str(data["id"]) if data.get("id") is not None else None,
        )

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str = DEFAULT_MODEL,
        thinking: bool = False,
        reasoning_effort: str = "high",
        max_tokens: int | None = None,
        temperature: float | None = None,
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
        return self._nonstream(payload)

    def chat_json(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str = DEFAULT_MODEL,
        thinking: bool = False,
        reasoning_effort: str = "high",
        max_tokens: int | None = None,
        temperature: float | None = None,
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
        last_error = "DeepSeek JSON Output was empty"
        for json_attempt in range(2):
            result = self._nonstream(payload)
            try:
                parsed = json.loads(result.content) if result.content.strip() else None
            except json.JSONDecodeError as exc:
                parsed = None
                last_error = f"DeepSeek JSON Output was invalid: {exc.msg}"
            if isinstance(parsed, dict):
                return JsonChatResult(
                    content=result.content,
                    finish_reason=result.finish_reason,
                    usage=result.usage,
                    model=result.model,
                    response_id=result.response_id,
                    data=parsed,
                )
            if parsed is not None:
                last_error = "DeepSeek JSON Output must be an object"
            if json_attempt == 0:
                continue
        raise DeepSeekProtocolError("invalid_json_output", _sanitize(last_error))

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str = DEFAULT_MODEL,
        thinking: bool = True,
        reasoning_effort: str = "high",
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
                            usage = dict(chunk_usage)
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
                raise DeepSeekProtocolError(
                    "incomplete_stream", "DeepSeek SSE stream ended before [DONE]"
                )
            except DeepSeekError as exc:
                if emitted_content or not exc.retryable or attempt >= self.retries:
                    raise
                self._sleep_before_retry(attempt)
            except _NETWORK_EXCEPTIONS as exc:
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
                self._sleep_before_retry(attempt)
        raise AssertionError("unreachable")
