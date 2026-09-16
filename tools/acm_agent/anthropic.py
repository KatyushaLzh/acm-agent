"""Anthropic Messages adapter; HTTP and wire parsing are independent of DeepSeek."""
from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode, urlsplit

from .provider import AIResult, AIJsonResult, AIStreamEvent, CapabilityProfile, ProviderConfigurationError, ProviderError, ProviderHealth, ProviderProtocolError
from .provider_config import endpoint_origin, normalize_base_url, validate_auth, validate_model_id, validate_provider_headers
from .provider_http import safe_https_open, open_response, managed_response, read_json, iter_sse
from .usage import normalize_usage


def _base(value):
    base = normalize_base_url(value)
    if base.endswith("/messages"):
        base = base[:-9]
    return base + "/v1" if not urlsplit(base).path else base


def _headers(key, auth=None, headers=None):
    if not key or len(key.encode("utf-8")) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ProviderConfigurationError("missing_api_key", "A valid API key is required")
    mode = validate_auth(auth or {"type": "header", "header": "x-api-key"})
    result = {"Accept": "application/json", "Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    for name, value in validate_provider_headers(headers).items():
        result[str(name).lower()] = value
    result["Authorization" if mode["type"] == "bearer" else mode["header"]] = "Bearer " + key if mode["type"] == "bearer" else key
    return result


def anthropic_usage(raw):
    if not isinstance(raw, Mapping):
        return {}
    clean = normalize_usage(raw)
    details = raw.get("output_tokens_details")
    thinking_tokens = details.get("thinking_tokens") if isinstance(details, Mapping) else None
    if isinstance(thinking_tokens, int) and not isinstance(thinking_tokens, bool) and thinking_tokens >= 0:
        clean["reasoning_tokens"] = thinking_tokens
    for original, canonical in (("cache_read_input_tokens", "cache_read_tokens"), ("cache_creation_input_tokens", "cache_write_tokens")):
        value = raw.get(original)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            clean[canonical] = value
    if "input_tokens" in clean:
        clean["cache_miss_tokens"] = clean["input_tokens"]
        clean["input_tokens"] += clean.get("cache_read_tokens", 0) + clean.get("cache_write_tokens", 0)
    clean.pop("total_tokens", None)
    return normalize_usage(clean)


def discover_anthropic_models(*, base_url, api_key, auth=None, headers=None, timeout=15.0, transport=None, include_metadata=False, request_callback=None):
    endpoint = _base(base_url) + "/models"
    ids, metadata, cursors = [], {}, set()
    cursor = None
    deadline = time.monotonic() + min(float(timeout), 120.0)
    for _ in range(12):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("deadline_exceeded", "Model discovery deadline exceeded", retryable=True)
        url = endpoint + ("?" + urlencode({"after_id": cursor}) if cursor else "")
        if request_callback:
            request_callback()
        request = urllib.request.Request(url, headers=_headers(api_key, auth, headers), method="GET")
        with managed_response(open_response(request, remaining, transport or safe_https_open, secret=api_key)) as response:
            document = read_json(response, 1_048_576)
        if not isinstance(document.get("data"), list):
            raise ProviderProtocolError("invalid_models_response", "Model discovery requires data[]")
        for item in document["data"]:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                raise ProviderProtocolError("invalid_models_response", "Invalid model entry")
            model = validate_model_id(item["id"])
            if model not in metadata:
                ids.append(model)
                # Capability metadata only; do not persist arbitrary provider strings.
                capabilities = item.get("capabilities")
                def safe(value):
                    if isinstance(value, Mapping):
                        return {k: safe(v) for k, v in value.items() if k in {"thinking", "adaptive", "supported", "enabled", "types", "effort", "low", "medium", "high", "max", "structured_outputs", "max_input_tokens", "max_tokens"}}
                    if isinstance(value, list):
                        return [v for v in value if isinstance(v, str) and v in {"adaptive", "enabled", "disabled", "manual", "low", "medium", "high", "max"}]
                    return value if isinstance(value, (bool, int)) else None
                metadata[model] = {"capabilities": safe(capabilities)} if isinstance(capabilities, Mapping) else {}
                for name in ("max_input_tokens", "max_tokens"):
                    value = item.get(name)
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                        metadata[model][name] = value
            if len(ids) > 512:
                raise ProviderProtocolError("too_many_models", "Model discovery exceeds 512 models")
        if not document.get("has_more"):
            return {"ids": ids, "metadata": metadata} if include_metadata else ids
        cursor = document.get("last_id")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ProviderProtocolError("invalid_models_response", "Invalid model pagination cursor")
        cursors.add(cursor)
    raise ProviderError("request_budget_exceeded", "Model discovery request budget exhausted", retryable=True)


class AnthropicClient:
    def __init__(self, api_key, *, provider_id, base_url, models, auth=None, credential_origin=None, timeout=60, retries=0, transport=None, wire_profile=None, headers=None, **options):
        self._api_key = str(api_key or "").strip()
        self.provider_id, self.base_url = str(provider_id), _base(base_url)
        self.endpoint = self.base_url + "/messages"
        self.origin = endpoint_origin(self.base_url)
        self.credential_origin = endpoint_origin(credential_origin or self.base_url)
        self.auth, self.headers = auth, dict(headers or {})
        self._models, self.wire_profile = dict(models), dict(wire_profile or {})
        self.timeout, self.retries = float(timeout), 0
        self._transport = transport or safe_https_open
        self.provider_request_count = 0

    @property
    def request_attempts(self):
        return self.provider_request_count

    @property
    def key_detected(self):
        return bool(self._api_key)

    def capabilities(self, model):
        selected = validate_model_id(model)
        if selected not in self._models:
            raise ProviderConfigurationError("invalid_model", "Model is not declared for this provider")
        return self._models[selected]

    def _payload(self, messages, *, model, thinking=False, reasoning_effort="auto", max_tokens=None, temperature=None, stream=False, **options):
        self.capabilities(model)
        payload = {"model": model, "messages": [], "max_tokens": max_tokens if max_tokens is not None else 4096, "stream": stream}
        if isinstance(payload["max_tokens"], bool) or not isinstance(payload["max_tokens"], int) or payload["max_tokens"] <= 0:
            raise ProviderConfigurationError("invalid_max_tokens", "max_tokens must be positive")
        systems = []
        for message in messages:
            role, content = message.get("role"), message.get("content")
            if not isinstance(content, str) or role not in {"system", "developer", "user", "assistant"}:
                raise ProviderConfigurationError("invalid_messages", "Only text system/user/assistant messages are supported")
            if role in {"system", "developer"}:
                systems.append(content)
            else:
                payload["messages"].append({"role": role, "content": content})
        if systems:
            payload["system"] = "\n\n".join(systems)
        if not payload["messages"]:
            raise ProviderConfigurationError("invalid_messages", "At least one conversation message is required")
        if thinking and reasoning_effort == "none":
            payload["thinking"] = {"type": "disabled"}
        elif thinking and reasoning_effort != "auto":
            if reasoning_effort not in {"low", "medium", "high", "max"}:
                raise ProviderConfigurationError("invalid_reasoning_effort", "Unsupported thinking effort")
            mode = self.wire_profile.get("reasoning_mode", "adaptive")
            if mode == "adaptive":
                payload["thinking"] = {"type": "adaptive"}
                payload["output_config"] = {"effort": reasoning_effort}
            elif mode == "manual" and reasoning_effort in {"low", "medium", "high"}:
                budget = {"low": 1024, "medium": 4096, "high": 8192}[reasoning_effort]
                if payload["max_tokens"] < budget + 1024:
                    raise ProviderConfigurationError("thinking_budget_exceeded", "Output budget must reserve 1024 tokens beyond thinking budget")
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            else:
                raise ProviderConfigurationError("unsupported_reasoning_effort", "Selected thinking effort is unsupported")
        if temperature is not None and payload.get("thinking", {}).get("type") not in {"adaptive", "enabled"} and not self.wire_profile.get("omit_temperature"):
            payload["temperature"] = temperature
        return payload

    def _open(self, payload, request_timeout=None):
        if self.origin != self.credential_origin:
            raise ProviderConfigurationError("credential_origin_mismatch", "Credential origin does not match provider")
        headers = _headers(self._api_key, self.auth, self.headers)
        if payload.get("stream"):
            headers["Accept"] = "text/event-stream"
        data = json.dumps(payload).encode()
        if len(data) > 8_388_608:
            raise ProviderConfigurationError("request_too_large", "Provider request exceeds the size limit")
        request = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
        self.provider_request_count += 1
        return open_response(request, min(self.timeout, float(request_timeout or self.timeout)), self._transport, secret=self._api_key)

    def _metadata(self, usage, **extra):
        return {"transport_api": "anthropic", "usage_complete": "input_tokens" in usage and "output_tokens" in usage, **extra}

    def _finish(self, reason, usage, model):
        if reason not in {"end_turn", "stop_sequence"}:
            code = {"max_tokens": "output_token_limit", "refusal": "refusal", "tool_use": "unsupported_tool_call"}.get(reason, "incomplete_response")
            raise ProviderProtocolError(code, "Anthropic response did not complete normally", finish_reason="length" if reason == "max_tokens" else reason, usage=usage, requested_model=model)

    def _nonstream(self, payload, request_timeout=None):
        started = time.monotonic()
        before = self.provider_request_count
        try:
            with managed_response(self._open(payload, request_timeout)) as response:
                document = read_json(response)
            usage = anthropic_usage(document.get("usage"))
            usage.update(provider_requests=1, latency_ms=round((time.monotonic() - started) * 1000))
            self._finish(document.get("stop_reason"), usage, payload["model"])
            blocks = document.get("content")
            if not isinstance(blocks, list) or any(not isinstance(b, Mapping) or b.get("type") not in {"text", "thinking", "redacted_thinking"} for b in blocks):
                raise ProviderProtocolError("invalid_response", "Unsupported Anthropic content", usage=usage)
            texts = [b.get("text") for b in blocks if b.get("type") == "text"]
            if not texts or any(not isinstance(text, str) for text in texts):
                raise ProviderProtocolError("empty_response", "Anthropic returned no text", usage=usage)
            return AIResult("".join(texts), "stop", usage, str(document.get("model") or payload["model"]), response_id=document.get("id"), requested_model=payload["model"], provider_metadata=self._metadata(usage))
        except ProviderError as exc:
            exc.usage.setdefault("provider_requests", self.provider_request_count - before)
            raise
        except OSError:
            raise ProviderError("network_error", "Provider response read failed", retryable=True, usage={"provider_requests": self.provider_request_count - before}) from None

    def chat(self, messages, **options):
        return self._nonstream(self._payload(messages, **options), options.get("request_timeout"))

    def chat_json(self, messages, **options):
        return self.structured(messages, json_schema={"type": "object"}, schema_name="response", **options)

    def structured(self, messages, *, json_schema, schema_name, **options):
        payload = self._payload(messages, **options)
        mode = self.wire_profile.get("structured_output", "native_schema")
        if mode == "native_schema":
            payload.setdefault("output_config", {})["format"] = {"type": "json_schema", "schema": dict(json_schema)}
        else:
            payload["system"] = payload.get("system", "") + "\nReturn only a JSON object matching this schema: " + json.dumps(json_schema)
            mode = "prompt_json"
        result = self._nonstream(payload, options.get("request_timeout"))
        content = result.content.strip()
        fence = re.fullmatch(r"```json\s*\n([\s\S]*?)\n```", content, re.I)
        try:
            data = json.loads(fence.group(1) if fence else content)
            if not isinstance(data, dict):
                raise ValueError()
        except ValueError:
            raise ProviderProtocolError("invalid_json_output", "Provider did not return a JSON object", usage=result.usage) from None
        return AIJsonResult(result.content, result.finish_reason, result.usage, result.model, data, response_id=result.response_id, requested_model=result.requested_model, provider_metadata={**result.provider_metadata, "structured_format": mode})

    def stream_chat(self, messages, **options):
        if self.wire_profile.get("streaming") == "buffered":
            result = self.chat(messages, **options)
            yield AIStreamEvent("delta", content=result.content, model=result.model, requested_model=result.requested_model, provider_metadata={"streaming": "buffered"})
            yield AIStreamEvent("done", finish_reason="stop", usage=result.usage, model=result.model, requested_model=result.requested_model, provider_metadata={**result.provider_metadata, "streaming": "buffered"})
            return
        payload = self._payload(messages, stream=True, **options)
        raw_usage, reason, model, response_id = {}, None, payload["model"], None
        started, delta_seen, emitted = False, False, False
        blocks = {}
        before = self.provider_request_count
        deadline = time.monotonic() + min(self.timeout, float(options.get("request_timeout") or self.timeout))
        try:
            with managed_response(self._open(payload, options.get("request_timeout"))) as response:
                for event, value in iter_sse(response, deadline=deadline):
                    kind = value.get("type", event)
                    if kind == "ping":
                        continue
                    if kind == "error":
                        error_type = value.get("error", {}).get("type") if isinstance(value.get("error"), Mapping) else None
                        raise ProviderError("server_error" if error_type == "overloaded_error" else "stream_error", "Anthropic stream returned an error", retryable=not emitted and error_type == "overloaded_error")
                    if kind == "message_start":
                        if started:
                            raise ProviderProtocolError("invalid_stream", "Duplicate message_start")
                        message = value.get("message", {})
                        started = True
                        model, response_id = message.get("model", model), message.get("id")
                        raw_usage.update(message.get("usage") or {})
                    elif not started:
                        raise ProviderProtocolError("invalid_stream", "Missing message_start")
                    elif kind == "content_block_start":
                        block = value.get("content_block", {})
                        if block.get("type") not in {"text", "thinking", "redacted_thinking"}:
                            raise ProviderProtocolError("unsupported_tool_call", "Unsupported stream content block")
                        if not isinstance(value.get("index"), int) or isinstance(value.get("index"), bool) or value["index"] < 0:
                            raise ProviderProtocolError("invalid_stream", "Invalid block index")
                        if value.get("index") in blocks or delta_seen:
                            raise ProviderProtocolError("invalid_stream", "Duplicate or late block start")
                        blocks[value.get("index")] = block.get("type")
                        if block.get("type") == "text" and block.get("text"):
                            if not isinstance(block["text"], str):
                                raise ProviderProtocolError("invalid_stream", "Invalid initial block text")
                            emitted = True
                            yield AIStreamEvent("delta", content=block["text"], model=model, requested_model=payload["model"])
                    elif kind == "content_block_delta":
                        if value.get("index") not in blocks:
                            raise ProviderProtocolError("invalid_stream", "Delta for unopened block")
                        delta = value.get("delta", {})
                        if delta.get("type") == "input_json_delta":
                            raise ProviderProtocolError("unsupported_tool_call", "Tool input is unsupported")
                        if delta.get("type") == "text_delta":
                            if blocks[value.get("index")] != "text":
                                raise ProviderProtocolError("invalid_stream", "Text delta in non-text block")
                            if not isinstance(delta.get("text"), str):
                                raise ProviderProtocolError("invalid_stream", "Invalid text delta")
                            emitted = True
                            yield AIStreamEvent("delta", content=delta["text"], model=model, response_id=response_id, requested_model=payload["model"])
                    elif kind == "content_block_stop":
                        if value.get("index") not in blocks:
                            raise ProviderProtocolError("invalid_stream", "Stop for unopened block")
                        del blocks[value.get("index")]
                    elif kind == "message_delta":
                        if blocks:
                            raise ProviderProtocolError("invalid_stream", "Message delta before blocks complete")
                        delta_seen = True
                        reason = value.get("delta", {}).get("stop_reason", reason)
                        raw_usage.update(value.get("usage") or {})
                    elif kind == "message_stop":
                        usage = {**anthropic_usage(raw_usage), "provider_requests": 1}
                        if blocks or not delta_seen:
                            raise ProviderProtocolError("incomplete_stream", "Stream ended before all blocks completed")
                        self._finish(reason, usage, payload["model"])
                        if not emitted:
                            raise ProviderProtocolError("empty_response", "Stream returned no text")
                        yield AIStreamEvent("done", finish_reason="stop", usage=usage, model=model, response_id=response_id, requested_model=payload["model"], provider_metadata=self._metadata(usage, streaming="native"))
                        return
            raise ProviderProtocolError("incomplete_stream", "Stream ended before message_stop")
        except (OSError, ValueError, TypeError, AttributeError):
            raise ProviderProtocolError("invalid_stream", "Malformed or interrupted Anthropic stream", usage={**anthropic_usage(raw_usage), "provider_requests": self.provider_request_count - before}, protocol_details={"emitted_content": emitted}) from None
        except ProviderError as exc:
            exc.usage = {**anthropic_usage(raw_usage), **exc.usage, "provider_requests": self.provider_request_count - before}
            exc.protocol_details["emitted_content"] = emitted
            if emitted:
                exc.retryable = False
            raise

    def test_connection(self, model):
        try:
            result = self.chat([{"role": "user", "content": "Reply with OK."}], model=model, max_tokens=32)
            return ProviderHealth(True, model, result.model, result.response_id, result.usage)
        except ProviderError as exc:
            return ProviderHealth(False, model, usage=exc.usage, error=exc.as_dict())
