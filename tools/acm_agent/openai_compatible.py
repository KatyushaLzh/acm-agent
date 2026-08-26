"""Fail-closed OpenAI Chat Completions adapter for configured HTTPS relays.

Only the common wire subset is assumed.  Optional JSON, streaming, cache
telemetry, tools and thinking behaviour are gated by the selected model's
explicit capability profile.

Protocol references:
- https://platform.openai.com/docs/api-reference/chat/create
- https://docs.python.org/3/library/urllib.request.html
"""

from __future__ import annotations

import json
import ipaddress
import re
import socket
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .ai_policy import DEFAULT_REASONING_EFFORT
from .deepseek import (
    DeepSeekClient,
    DeepSeekConfigurationError,
    DeepSeekError,
    Transport,
)
from .provider import CapabilityProfile, ProviderConfigurationError
from .provider_config import (
    chat_completions_url,
    endpoint_origin,
    models_url,
    validate_auth,
    validate_model_id,
)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_RejectRedirects())
_MAX_MODELS_RESPONSE_BYTES = 1_048_576
_MAX_DISCOVERED_MODELS = 512
_JSON_FENCE = re.compile(
    r"\A```json[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE,
)


def _safe_https_open(request: urllib.request.Request, timeout: float) -> Any:
    parsed = urlsplit(request.full_url)
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
            )
        }
    except OSError as exc:
        raise ProviderConfigurationError(
            "endpoint_resolution_failed", "provider endpoint DNS resolution failed"
        ) from exc
    try:
        unsafe = not addresses or any(
            not ipaddress.ip_address(address.split("%", 1)[0]).is_global
            for address in addresses
        )
    except ValueError:
        unsafe = True
    if unsafe:
        raise ProviderConfigurationError(
            "unsafe_endpoint", "provider endpoint resolved to a non-public address"
        )
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def discover_openai_compatible_models(
    *,
    base_url: str,
    api_key: str,
    timeout: float = 15.0,
    transport: Transport | None = None,
) -> list[str]:
    """Discover bounded model ids through the standard authenticated ``/models`` API."""

    endpoint = models_url(base_url)
    secret = str(api_key or "").strip()
    if not secret or len(secret.encode("utf-8")) > 2048 or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in secret
    ):
        raise ProviderConfigurationError("missing_api_key", "a valid API key is required")
    request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {secret}",
            "User-Agent": "acm-agent/2.0 (+local learning tool)",
        },
        method="GET",
    )
    opener = transport or _safe_https_open
    try:
        response = opener(request, float(timeout))
        with response:
            getter = getattr(response, "geturl", None)
            final_url = getter() if callable(getter) else endpoint
            if str(final_url).rstrip("/") != endpoint:
                raise ProviderConfigurationError(
                    "redirect_blocked", "provider response URL differs from the configured /models endpoint"
                )
            body = response.read(_MAX_MODELS_RESPONSE_BYTES + 1)
    except ProviderConfigurationError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        status = int(exc.code) if isinstance(exc, urllib.error.HTTPError) else None
        raise ProviderConfigurationError(
            "model_discovery_failed",
            "provider /models request failed",
            status=status,
            retryable=status is None or status == 429 or bool(status and status >= 500),
        ) from None
    if len(body) > _MAX_MODELS_RESPONSE_BYTES:
        raise ProviderConfigurationError(
            "model_discovery_response_too_large", "provider /models response is too large"
        )
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderConfigurationError(
            "invalid_models_response", "provider /models response is not valid UTF-8 JSON"
        ) from None
    data = document.get("data") if isinstance(document, Mapping) else None
    if not isinstance(data, list):
        raise ProviderConfigurationError(
            "invalid_models_response", "provider /models response must contain data[]"
        )
    if len(data) > _MAX_DISCOVERED_MODELS:
        raise ProviderConfigurationError(
            "too_many_models", "provider advertised too many models"
        )
    discovered: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            raise ProviderConfigurationError(
                "invalid_models_response", "each provider model must have a valid id"
            )
        model = validate_model_id(item["id"])
        if model not in seen:
            seen.add(model)
            discovered.append(model)
    if not discovered:
        raise ProviderConfigurationError(
            "no_models", "provider /models response did not advertise any models"
        )
    return discovered


class OpenAICompatibleClient(DeepSeekClient):
    """Configured adapter that reuses the proven parser/retry wire core.

    DeepSeek remains a separate public adapter; this class deliberately does
    not inherit its fixed model allowlist or automatically emit its extensions.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        provider_id: str,
        base_url: str,
        auth: Mapping[str, Any] | None,
        models: Mapping[str, CapabilityProfile],
        credential_origin: str | None = None,
        thinking_wire: str = "none",
        transport: Transport | None = None,
        **options: Any,
    ) -> None:
        self.provider_id = str(provider_id)
        self.base_url = str(base_url)
        self.endpoint = chat_completions_url(base_url)
        self.origin = endpoint_origin(base_url)
        self.credential_origin = endpoint_origin(credential_origin or base_url)
        self.auth = validate_auth(auth)
        self._models = dict(models)
        self.thinking_wire = str(thinking_wire or "none").strip().lower()
        if self.thinking_wire not in {"none", "deepseek"}:
            raise ProviderConfigurationError(
                "invalid_provider", "thinking_wire must be none or deepseek"
            )
        super().__init__(api_key=api_key, transport=transport, **options)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider_id={self.provider_id!r}, "
            f"origin={self.origin!r}, key_detected={self.key_detected})"
        )

    @staticmethod
    def _default_transport(request: urllib.request.Request, timeout: float) -> Any:
        return _safe_https_open(request, timeout)

    def _capabilities(self, model: str) -> CapabilityProfile:
        selected = validate_model_id(model)
        profile = self._models.get(selected)
        if not isinstance(profile, CapabilityProfile):
            raise ProviderConfigurationError(
                "invalid_model", f"model {selected!r} is not declared for provider {self.provider_id!r}"
            )
        return profile

    def capabilities(self, model: str) -> CapabilityProfile:
        return self._capabilities(model)

    def test_connection(self, model: str) -> Any:
        from .provider import ProviderError, ProviderHealth

        selected = validate_model_id(model)
        self._require_capability(selected, "text_chat")
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

    def _require_capability(self, model: str, name: str) -> CapabilityProfile:
        profile = self._capabilities(model)
        if not bool(getattr(profile, name, False)):
            raise ProviderConfigurationError(
                "unsupported_capability",
                f"provider/model does not declare required capability: {name}",
            )
        return profile

    def _request(self, payload: Mapping[str, Any]) -> urllib.request.Request:
        key = self._require_key()
        if self.origin != self.credential_origin:
            raise ProviderConfigurationError(
                "credential_origin_mismatch",
                "credential origin does not match the configured provider endpoint",
            )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "acm-agent/2.0 (+local learning tool)",
        }
        if self.auth["type"] == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        else:
            headers[self.auth["header"]] = key
        return urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def _open(
        self,
        payload: Mapping[str, Any],
        *,
        request_timeout: float | None = None,
        request_factory: Any = None,
    ) -> Any:
        if request_factory is not None:
            raise ProviderConfigurationError(
                "unsupported_transport", "relay providers use Chat Completions"
            )
        response = super()._open(payload, request_timeout=request_timeout)
        getter = getattr(response, "geturl", None)
        final_url = getter() if callable(getter) else self.endpoint
        if str(final_url).rstrip("/") != self.endpoint:
            self._close_response(response)
            raise ProviderConfigurationError(
                "redirect_blocked", "provider response URL differs from the configured endpoint"
            )
        return response

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
        selected = validate_model_id(model)
        capabilities = self._require_capability(selected, "text_chat")
        if stream and not capabilities.streaming:
            self._require_capability(selected, "streaming")
        if json_object and not capabilities.json_object:
            self._require_capability(selected, "json_object")
        if thinking and not capabilities.thinking:
            self._require_capability(selected, "thinking")
        normalized_messages: list[dict[str, str]] = []
        for message in messages:
            role = str(message.get("role") or "").strip()
            content = message.get("content")
            if role not in {"system", "user", "assistant"} or not isinstance(content, str):
                raise ProviderConfigurationError(
                    "invalid_messages", "each message needs a system/user/assistant role and string content"
                )
            normalized_messages.append({"role": role, "content": content})
        if not normalized_messages:
            raise ProviderConfigurationError("invalid_messages", "at least one message is required")
        payload: dict[str, Any] = {
            "model": selected,
            "messages": normalized_messages,
            "stream": bool(stream),
        }
        effort = str(reasoning_effort or DEFAULT_REASONING_EFFORT).strip().lower()
        if thinking and self.thinking_wire == "deepseek":
            if effort not in {"high", "max"}:
                raise ProviderConfigurationError(
                    "invalid_reasoning_effort", "DeepSeek reasoning_effort must be high or max"
                )
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = effort
        elif thinking:
            if effort not in {"none", "low", "medium", "high"}:
                raise ProviderConfigurationError(
                    "invalid_reasoning_effort", "reasoning_effort must be none, low, medium or high"
                )
            payload["reasoning_effort"] = effort
        elif self.thinking_wire == "deepseek" and effort != "auto":
            payload["thinking"] = {"type": "disabled"}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if json_object:
            if not any("json" in item["content"].casefold() for item in normalized_messages):
                raise ProviderConfigurationError(
                    "missing_json_instruction", "JSON Output requires an explicit JSON instruction"
                )
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            if isinstance(max_tokens, bool) or int(max_tokens) <= 0:
                raise ProviderConfigurationError("invalid_max_tokens", "max_tokens must be positive")
            if capabilities.max_output_tokens is not None and int(max_tokens) > capabilities.max_output_tokens:
                raise ProviderConfigurationError("invalid_max_tokens", "max_tokens exceeds declared model limit")
            payload["max_tokens"] = int(max_tokens)
        if temperature is not None and (not thinking or effort == "none"):
            value = float(temperature)
            if not 0 <= value <= 2:
                raise ProviderConfigurationError("invalid_temperature", "temperature must be between 0 and 2")
            payload["temperature"] = value
        return payload

    @staticmethod
    def _decode_json_output(content: str) -> tuple[Any, bool]:
        """Normalize only a single, whole-response Markdown JSON fence.

        Some Claude-compatible relays ignore ``response_format`` and wrap the
        otherwise valid object in a Markdown fence.  Accepting only the exact
        envelope keeps the structured-output boundary fail closed: prose,
        multiple fences, trailing content, JSON5 and non-object values still
        fail in the shared ``chat_json`` validator.
        """

        try:
            return json.loads(content), False
        except json.JSONDecodeError as original:
            match = _JSON_FENCE.fullmatch(content.strip())
            if match is None:
                raise original
            return json.loads(match.group("body")), True

    def structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        **options: Any,
    ) -> Any:
        """Use the declared Chat JSON subset; relays do not imply Responses."""

        if not isinstance(json_schema, Mapping) or not str(schema_name or "").strip():
            raise ProviderConfigurationError(
                "invalid_json_schema", "json_schema and schema_name are required"
            )
        result = self.chat_json(messages, json_retries=0, **options)
        metadata = dict(result.provider_metadata)
        metadata.update(
            transport_api="chat_completions",
            structured_format="json_object",
        )
        return replace(result, provider_metadata=metadata)

    def chat_with_tools(self, *args: Any, model: str, **kwargs: Any) -> Any:
        self._require_capability(model, "function_tools")
        return super().chat_with_tools(*args, model=model, **kwargs)


OpenAICompatibleError = DeepSeekError
OpenAICompatibleConfigurationError = DeepSeekConfigurationError


__all__ = [
    "discover_openai_compatible_models",
    "OpenAICompatibleClient",
    "OpenAICompatibleConfigurationError",
    "OpenAICompatibleError",
]
