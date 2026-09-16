"""Bounded, evidence-driven wire adjustments for provider verification."""
from __future__ import annotations

from typing import Any, Mapping
from .provider import ProviderError


def compatibility_adjustment(error: ProviderError, wire: Mapping[str, Any], *, adapter: str,
                             case: str) -> dict[str, Any] | None:
    if error.code not in {"invalid_request", "unsupported_capability"} or error.status not in {None, 400, 422}:
        return None
    parameter = str(error.protocol_details.get("unsupported_parameter") or "")
    message = str(error).lower() + (" unsupported " + parameter if parameter else "")
    rejected = any(word in message for word in ("unsupported", "not support", "not allowed", "unknown", "unrecognized", "not permitted", "does not", "不支持", "不允许"))
    if not rejected:
        return None
    if adapter == "openai_compatible" and "max_tokens" in message and wire.get("token_parameter") != "max_completion_tokens":
        return {"token_parameter": "max_completion_tokens"}
    if "temperature" in message and not wire.get("omit_temperature"):
        return {"omit_temperature": True}
    if "stream_options" in message and wire.get("stream_usage", True):
        return {"stream_usage": False}
    if adapter == "anthropic" and parameter == "thinking" and wire.get("reasoning_mode", "adaptive") in {"auto", "adaptive"}:
        return {"reasoning_mode": "manual"}
    if any(word in message for word in ("response_format", "json_schema", "json_object", "output_config.format", "structured output")) or (case == "json_object" and parameter == "output_config"):
        mode = wire.get("structured_output", "native_schema" if adapter == "anthropic" else "json_object")
        if mode == "native_schema" and adapter != "anthropic":
            return {"structured_output": "json_object"}
        if mode != "prompt_json":
            return {"structured_output": "prompt_json"}
    if case == "stream" and any(word in message for word in ("stream", "sse")) and wire.get("streaming") != "buffered":
        return {"streaming": "buffered"}
    return None
