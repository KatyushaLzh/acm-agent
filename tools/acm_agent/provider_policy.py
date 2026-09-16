"""Provider-neutral validation over the current configured policy catalog."""

from __future__ import annotations

from .ai_policy import ALLOWED_REASONING_EFFORTS
from .provider import ProviderConfigurationError
from .provider_config import validate_model_id


def validate_model(model: str) -> str:
    # Catalog membership and availability are enforced by ProviderRegistry.
    return validate_model_id(model)


def validate_reasoning_effort(reasoning_effort: str) -> str:
    effort = str(reasoning_effort).strip().lower()
    if effort not in ALLOWED_REASONING_EFFORTS:
        raise ProviderConfigurationError(
            "invalid_reasoning_effort", "reasoning_effort must be high or max"
        )
    return effort


__all__ = ["validate_model", "validate_reasoning_effort"]
