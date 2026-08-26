"""Provider-neutral validation over the current configured policy catalog."""

from __future__ import annotations

from .ai_policy import ALLOWED_MODELS, ALLOWED_REASONING_EFFORTS
from .provider import ProviderConfigurationError


def validate_model(model: str) -> str:
    selected = str(model).strip()
    if selected not in ALLOWED_MODELS:
        allowed = ", ".join(sorted(ALLOWED_MODELS))
        raise ProviderConfigurationError(
            "invalid_model", f"Unsupported DeepSeek model; allowed: {allowed}"
        )
    return selected


def validate_reasoning_effort(reasoning_effort: str) -> str:
    effort = str(reasoning_effort).strip().lower()
    if effort not in ALLOWED_REASONING_EFFORTS:
        raise ProviderConfigurationError(
            "invalid_reasoning_effort", "reasoning_effort must be high or max"
        )
    return effort


__all__ = ["validate_model", "validate_reasoning_effort"]
