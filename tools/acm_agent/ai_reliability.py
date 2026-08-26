"""Provider-neutral, auditable AI business outcome contracts.

HTTP success, provider success, artifact validity, and business usability are
deliberately separate facts.  Keeping the normalization here gives live,
cache, repair, and deterministic-fallback paths one fail-closed vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


AI_OUTCOME_VERSION = 1
PROVIDER_OUTCOMES = frozenset({"not_called", "succeeded", "failed", "mixed"})
ARTIFACT_OUTCOMES = frozenset(
    {"valid", "repaired", "partial", "invalid", "not_applicable"}
)
BUSINESS_OUTCOMES = frozenset(
    {
        "complete",
        "cache",
        "hybrid",
        "deterministic_fallback",
        "partial",
        "unavailable",
    }
)
USABLE_BUSINESS_OUTCOMES = frozenset(
    {"complete", "cache", "hybrid", "deterministic_fallback"}
)


def _choice(value: Any, allowed: frozenset[str], *, label: str) -> str:
    selected = str(value or "").strip().lower()
    if selected not in allowed:
        raise ValueError(f"invalid {label}: {selected!r}")
    return selected


def _flag(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def build_ai_outcome(
    *,
    provider_outcome: str,
    artifact_outcome: str,
    business_outcome: str,
    usable: bool | None = None,
    apply_ready: bool = False,
    degraded: bool | None = None,
    repair_attempts: int = 0,
) -> dict[str, Any]:
    """Build a normalized v1 outcome while enforcing cross-field invariants."""

    provider = _choice(provider_outcome, PROVIDER_OUTCOMES, label="provider_outcome")
    artifact = _choice(artifact_outcome, ARTIFACT_OUTCOMES, label="artifact_outcome")
    business = _choice(business_outcome, BUSINESS_OUTCOMES, label="business_outcome")
    repairs = repair_attempts
    if isinstance(repairs, bool) or not isinstance(repairs, int) or repairs < 0:
        raise ValueError("repair_attempts must be a non-negative integer")
    inferred_usable = business in USABLE_BUSINESS_OUTCOMES
    selected_usable = inferred_usable if usable is None else _flag(usable, label="usable")
    selected_apply_ready = _flag(apply_ready, label="apply_ready")
    inferred_degraded = business in {
        "hybrid", "deterministic_fallback", "partial", "unavailable"
    }
    selected_degraded = (
        inferred_degraded if degraded is None else _flag(degraded, label="degraded")
    )
    if selected_usable != inferred_usable:
        raise ValueError("usable conflicts with business_outcome")
    if selected_apply_ready and not selected_usable:
        raise ValueError("apply_ready requires a usable business outcome")
    if business in {"partial", "unavailable"} and selected_apply_ready:
        raise ValueError("partial or unavailable outcomes cannot be apply-ready")
    if artifact == "repaired" and repairs < 1:
        raise ValueError("a repaired artifact requires at least one repair attempt")
    if artifact == "valid" and business in {"partial", "unavailable"}:
        raise ValueError("a valid artifact cannot produce a partial or unavailable outcome")
    return {
        "version": AI_OUTCOME_VERSION,
        "provider_outcome": provider,
        "artifact_outcome": artifact,
        "business_outcome": business,
        "usable": selected_usable,
        "apply_ready": selected_apply_ready,
        "degraded": selected_degraded,
        "repair_attempts": repairs,
    }


def validate_ai_outcome(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an outcome received from another subsystem or persisted row."""

    if not isinstance(value, Mapping):
        raise ValueError("AI outcome must be an object")
    source = dict(value)
    if source.get("version") != AI_OUTCOME_VERSION:
        raise ValueError(f"AI outcome version must be {AI_OUTCOME_VERSION}")
    expected = {
        "version", "provider_outcome", "artifact_outcome", "business_outcome",
        "usable", "apply_ready", "degraded", "repair_attempts",
    }
    if set(source) != expected:
        raise ValueError("AI outcome has unknown or missing fields")
    return build_ai_outcome(
        provider_outcome=source["provider_outcome"],
        artifact_outcome=source["artifact_outcome"],
        business_outcome=source["business_outcome"],
        usable=source["usable"],
        apply_ready=source["apply_ready"],
        degraded=source["degraded"],
        repair_attempts=source["repair_attempts"],
    )


__all__ = [
    "AI_OUTCOME_VERSION",
    "ARTIFACT_OUTCOMES",
    "BUSINESS_OUTCOMES",
    "PROVIDER_OUTCOMES",
    "USABLE_BUSINESS_OUTCOMES",
    "build_ai_outcome",
    "validate_ai_outcome",
]
