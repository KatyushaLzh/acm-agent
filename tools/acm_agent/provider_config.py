"""Validated provider, task-profile, and cost-governance configuration."""

from __future__ import annotations

from copy import deepcopy
import ipaddress
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .ai_policy import DEFAULT_MODEL, DEFAULT_REASONING_EFFORT
from .ai_cache import (
    DEFAULT_CACHE_MAX_BYTES,
    DEFAULT_CACHE_MAX_ENTRIES,
    DEFAULT_CACHE_MAX_ENTRY_BYTES,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_FLIGHT_FAILURE_COOLDOWN_SECONDS,
    DEFAULT_FLIGHT_LEASE_SECONDS,
    DEFAULT_FLIGHT_WAIT_TIMEOUT_SECONDS,
    EXACT_CACHE_PROFILES,
)
from .provider import CapabilityProfile, ProviderConfigurationError


TASK_PROFILE_IDS = (
    "recommendation",
    "plan_organize",
    "plan_generate",
    "coaching",
    "patch",
    "summary",
)
REASONING_STRENGTHS = ("auto", "off", "low", "medium", "high")
COACHING_DELIVERY_MODES = ("resilient", "low_latency")
_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_HEADER_PATTERN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}\Z")
_CAPABILITY_KEYS = {
    "text_chat",
    "streaming",
    "json_object",
    "json_schema",
    "function_tools",
    "thinking",
    "prompt_cache",
    "usage_cache_tokens",
    "usage",
    "stream_usage",
}
_DEEPSEEK_MODEL_IDS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})

_DEFAULT_TASK_BUDGETS: dict[str, dict[str, int | float]] = {
    "recommendation": {
        "max_output_tokens": 4_096,
        "request_timeout_seconds": 120.0,
        "max_retries": 1,
        "max_validation_repairs": 1,
        "max_requests": 3,
        # The sanitized recommendation context can include hundreds of
        # distinct accepted-problem summaries plus the bounded candidate pool.
        # Live OpenAI-compatible usage is currently about 88k tokens for a
        # mature workspace.  Keep enough headroom for one governed retry or
        # validation repair without rejecting an already-paid response.
        "max_total_tokens": 300_000,
    },
    "plan_organize": {
        "max_output_tokens": 16_000,
        "request_timeout_seconds": 120.0,
        "max_retries": 1,
        "max_validation_repairs": 1,
        "max_requests": 3,
        "max_total_tokens": 160_000,
    },
    "plan_generate": {
        "max_output_tokens": 32_000,
        "request_timeout_seconds": 300.0,
        "max_retries": 1,
        "max_validation_repairs": 1,
        "max_requests": 6,
        "max_total_tokens": 400_000,
    },
    "coaching": {
        "max_output_tokens": 8_192,
        "request_timeout_seconds": 120.0,
        "max_retries": 1,
        "max_validation_repairs": 1,
        "max_requests": 3,
        "max_total_tokens": 200_000,
    },
    "patch": {
        "max_output_tokens": 12_000,
        "request_timeout_seconds": 240.0,
        "max_retries": 1,
        "max_validation_repairs": 1,
        "max_requests": 3,
        "max_total_tokens": 260_000,
    },
    "summary": {
        "max_output_tokens": 8_192,
        "request_timeout_seconds": 180.0,
        "max_retries": 1,
        "max_validation_repairs": 1,
        "max_requests": 3,
        "max_total_tokens": 240_000,
    },
}


DEEPSEEK_CAPABILITIES: dict[str, Any] = {
    "text_chat": True,
    "streaming": True,
    "json_object": True,
    "json_schema": False,
    "function_tools": True,
    "thinking": True,
    "prompt_cache": True,
    "usage_cache_tokens": True,
    "usage": True,
    "stream_usage": True,
    "max_context_tokens": 1_000_000,
    "max_output_tokens": 384_000,
}


def default_provider_config() -> dict[str, Any]:
    flash_capabilities = deepcopy(DEEPSEEK_CAPABILITIES)
    flash_capabilities["json_schema"] = True
    pro_capabilities = deepcopy(DEEPSEEK_CAPABILITIES)
    return {
        "deepseek": {
            "name": "DeepSeek Official",
            "adapter": "deepseek",
            "base_url": "https://api.deepseek.com",
            "credential_slot": "deepseek",
            "auth": {"type": "bearer"},
            "enabled": True,
            "models": {
                "deepseek-v4-flash": {
                    "capabilities": flash_capabilities,
                    "evidence": "verified_builtin",
                    "verified_capabilities": sorted(
                        key for key, enabled in flash_capabilities.items()
                        if isinstance(enabled, bool) and enabled
                    ),
                    "verified_reasoning_strengths": ["off", "medium", "high"],
                },
                "deepseek-v4-pro": {
                    "capabilities": pro_capabilities,
                    "evidence": "verified_builtin",
                    "verified_capabilities": sorted(
                        key for key, enabled in pro_capabilities.items()
                        if isinstance(enabled, bool) and enabled
                    ),
                    "verified_reasoning_strengths": ["off", "medium", "high"],
                },
            },
        }
    }


def default_task_profiles() -> dict[str, Any]:
    base = {
        "provider_id": "deepseek",
        "model": DEFAULT_MODEL,
        "thinking": False,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "reasoning_strength": "auto",
    }
    result = {profile_id: dict(base) for profile_id in TASK_PROFILE_IDS}
    for profile_id in ("plan_generate", "coaching", "patch", "summary"):
        result[profile_id]["thinking"] = True
        result[profile_id]["reasoning_strength"] = "medium"
    return result


def default_credential_slots() -> dict[str, Any]:
    return {
        "deepseek": {
            "provider_id": "deepseek",
            "origin": "https://api.deepseek.com",
            "auth": {"type": "bearer"},
            "environment_variable": "DEEPSEEK_API_KEY",
        }
    }


def default_ai_policy() -> dict[str, Any]:
    """Return conservative per-run limits; monetary hard limits stay opt-in."""

    return {
        "budgets": deepcopy(_DEFAULT_TASK_BUDGETS),
        "fallbacks": {profile_id: [] for profile_id in TASK_PROFILE_IDS},
        "hard_limits": {"daily_cny": None, "monthly_cny": None},
    }


def default_cache_policy() -> dict[str, Any]:
    """Return the fail-closed v12 local-cache policy."""

    return {
        "exact_profiles": list(EXACT_CACHE_PROFILES),
        "ttl_seconds": DEFAULT_CACHE_TTL_SECONDS,
        "max_entries": DEFAULT_CACHE_MAX_ENTRIES,
        "max_bytes": DEFAULT_CACHE_MAX_BYTES,
        "max_entry_bytes": DEFAULT_CACHE_MAX_ENTRY_BYTES,
        "flight_lease_seconds": DEFAULT_FLIGHT_LEASE_SECONDS,
        "flight_wait_timeout_seconds": DEFAULT_FLIGHT_WAIT_TIMEOUT_SECONDS,
        "flight_failure_cooldown_seconds": DEFAULT_FLIGHT_FAILURE_COOLDOWN_SECONDS,
        "semantic_enabled": False,
    }


def validate_cache_policy(value: Any) -> dict[str, Any]:
    """Validate exact-cache bounds and reject semantic caching outright."""

    if not isinstance(value, Mapping):
        raise ProviderConfigurationError(
            "invalid_cache_policy", "ai.cache must be an object"
        )
    source = dict(value)
    expected = set(default_cache_policy())
    if set(source) != expected:
        raise ProviderConfigurationError(
            "invalid_cache_policy", "ai.cache has unknown or missing fields"
        )
    semantic_enabled = source.get("semantic_enabled")
    if not isinstance(semantic_enabled, bool):
        raise ProviderConfigurationError(
            "invalid_cache_policy", "semantic_enabled must be a boolean"
        )
    if semantic_enabled:
        raise ProviderConfigurationError(
            "semantic_cache_forbidden", "semantic response cache is not supported"
        )
    profiles = source.get("exact_profiles")
    if not isinstance(profiles, list) or any(not isinstance(item, str) for item in profiles):
        raise ProviderConfigurationError(
            "invalid_cache_policy", "exact_profiles must be a list of profile ids"
        )
    normalized_profiles: list[str] = []
    for raw_profile in profiles:
        profile = str(raw_profile).strip().lower()
        if profile not in EXACT_CACHE_PROFILES:
            raise ProviderConfigurationError(
                "invalid_cache_policy",
                "exact cache is limited to recommendation, plan_organize, and summary",
            )
        if profile in normalized_profiles:
            raise ProviderConfigurationError(
                "invalid_cache_policy", "exact_profiles must be unique"
            )
        normalized_profiles.append(profile)

    def positive_integer(name: str) -> int:
        raw = source.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise ProviderConfigurationError(
                "invalid_cache_policy", f"{name} must be a positive integer"
            )
        return raw

    ttl_seconds = positive_integer("ttl_seconds")
    max_entries = positive_integer("max_entries")
    max_bytes = positive_integer("max_bytes")
    max_entry_bytes = positive_integer("max_entry_bytes")
    lease_seconds = positive_integer("flight_lease_seconds")
    wait_seconds = positive_integer("flight_wait_timeout_seconds")
    failure_cooldown_seconds = positive_integer("flight_failure_cooldown_seconds")
    if max_entry_bytes > max_bytes:
        raise ProviderConfigurationError(
            "invalid_cache_policy", "max_entry_bytes cannot exceed max_bytes"
        )
    return {
        "exact_profiles": normalized_profiles,
        "ttl_seconds": ttl_seconds,
        "max_entries": max_entries,
        "max_bytes": max_bytes,
        "max_entry_bytes": max_entry_bytes,
        "flight_lease_seconds": lease_seconds,
        "flight_wait_timeout_seconds": wait_seconds,
        "flight_failure_cooldown_seconds": failure_cooldown_seconds,
        "semantic_enabled": False,
    }


def _positive_number(value: Any, *, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProviderConfigurationError("invalid_budget", f"{label} must be positive")
    return value


def validate_ai_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderConfigurationError("invalid_policy", "ai.policy must be an object")
    source = dict(value)
    budgets = source.get("budgets")
    fallbacks = source.get("fallbacks")
    limits = source.get("hard_limits")
    if not isinstance(budgets, Mapping) or set(budgets) != set(TASK_PROFILE_IDS):
        raise ProviderConfigurationError(
            "invalid_budget", "all six task budgets must be configured exactly once"
        )
    normalized_budgets: dict[str, Any] = {}
    for profile_id in TASK_PROFILE_IDS:
        raw = budgets.get(profile_id)
        if not isinstance(raw, Mapping):
            raise ProviderConfigurationError("invalid_budget", "task budget must be an object")
        budget = dict(raw)
        max_retries = budget.get("max_retries")
        max_validation_repairs = budget.get("max_validation_repairs")
        max_requests = budget.get("max_requests")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ProviderConfigurationError("invalid_budget", "max_retries must be a non-negative integer")
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise ProviderConfigurationError("invalid_budget", "max_requests must be a positive integer")
        if (
            isinstance(max_validation_repairs, bool)
            or not isinstance(max_validation_repairs, int)
            or max_validation_repairs < 0
            or max_validation_repairs > 1
        ):
            raise ProviderConfigurationError(
                "invalid_budget", "max_validation_repairs must be 0 or 1"
            )
        if max_retries >= max_requests:
            raise ProviderConfigurationError("invalid_budget", "max_retries must be smaller than max_requests")
        normalized_budgets[profile_id] = {
            "max_output_tokens": int(_positive_number(budget.get("max_output_tokens"), label="max_output_tokens")),
            "request_timeout_seconds": float(_positive_number(budget.get("request_timeout_seconds"), label="request_timeout_seconds")),
            "max_retries": max_retries,
            "max_validation_repairs": max_validation_repairs,
            "max_requests": max_requests,
            "max_total_tokens": int(_positive_number(budget.get("max_total_tokens"), label="max_total_tokens")),
        }
    if not isinstance(fallbacks, Mapping) or set(fallbacks) != set(TASK_PROFILE_IDS):
        raise ProviderConfigurationError(
            "invalid_fallback", "all six fallback lists must be configured exactly once"
        )
    normalized_fallbacks: dict[str, list[dict[str, str]]] = {}
    for profile_id in TASK_PROFILE_IDS:
        raw_routes = fallbacks.get(profile_id)
        if not isinstance(raw_routes, list) or len(raw_routes) > 3:
            raise ProviderConfigurationError("invalid_fallback", "fallback must be a list of at most three routes")
        if profile_id == "coaching" and raw_routes:
            raise ProviderConfigurationError(
                "invalid_fallback", "coaching forbids cross-route fallback to preserve route pinning"
            )
        routes: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for raw_route in raw_routes:
            if not isinstance(raw_route, Mapping) or set(raw_route) - {
                "provider_id", "model", "reasoning_strength"
            }:
                raise ProviderConfigurationError("invalid_fallback", "fallback route is invalid")
            route = {
                "provider_id": validate_identifier(raw_route.get("provider_id"), label="provider_id"),
                "model": validate_model_id(raw_route.get("model")),
                "reasoning_strength": validate_reasoning_strength(raw_route.get("reasoning_strength", "auto")),
            }
            identity = (route["provider_id"], route["model"], route["reasoning_strength"])
            if identity in seen:
                raise ProviderConfigurationError("invalid_fallback", "fallback routes must be unique")
            seen.add(identity)
            routes.append(route)
        normalized_fallbacks[profile_id] = routes
    if not isinstance(limits, Mapping) or set(limits) != {"daily_cny", "monthly_cny"}:
        raise ProviderConfigurationError("invalid_budget", "hard_limits must contain daily_cny and monthly_cny")
    normalized_limits: dict[str, float | None] = {}
    for name in ("daily_cny", "monthly_cny"):
        raw = limits.get(name)
        normalized_limits[name] = None if raw is None else float(_positive_number(raw, label=name))
    daily = normalized_limits["daily_cny"]
    monthly = normalized_limits["monthly_cny"]
    if daily is not None and monthly is not None and daily > monthly:
        raise ProviderConfigurationError("invalid_budget", "daily_cny cannot exceed monthly_cny")
    return {
        "budgets": normalized_budgets,
        "fallbacks": normalized_fallbacks,
        "hard_limits": normalized_limits,
    }


def validate_identifier(value: Any, *, label: str) -> str:
    selected = str(value or "").strip().lower()
    if not _ID_PATTERN.fullmatch(selected):
        raise ProviderConfigurationError(
            "invalid_identifier",
            f"{label} must match [a-z][a-z0-9_-] and be at most 64 characters",
        )
    return selected


def validate_model_id(value: Any) -> str:
    selected = str(value or "").strip()
    if not _MODEL_PATTERN.fullmatch(selected):
        raise ProviderConfigurationError(
            "invalid_model", "model id contains unsupported characters or is too long"
        )
    return selected


def validate_reasoning_strength(value: Any) -> str:
    selected = str(value or "auto").strip().lower()
    if selected not in REASONING_STRENGTHS:
        raise ProviderConfigurationError(
            "invalid_reasoning_strength",
            "reasoning_strength must be auto, off, low, medium or high",
        )
    return selected


def validate_coaching_delivery_mode(value: Any) -> str:
    selected = str(value or "resilient").strip().lower()
    if selected not in COACHING_DELIVERY_MODES:
        raise ProviderConfigurationError(
            "invalid_delivery_mode",
            "coaching_delivery_mode must be resilient or low_latency",
        )
    return selected


def _legacy_reasoning_fields(strength: str) -> tuple[bool, str]:
    """Project the public v10 strength onto the legacy DeepSeek-shaped fields."""

    return {
        "auto": (False, "high"),
        "off": (False, "high"),
        "low": (True, "low"),
        "medium": (True, "high"),
        "high": (True, "max"),
    }[validate_reasoning_strength(strength)]


def reasoning_strength_from_profile(value: Mapping[str, Any]) -> str:
    """Read v10 strength, or deterministically migrate a v9 profile in memory."""

    if value.get("reasoning_strength") is not None:
        return validate_reasoning_strength(value.get("reasoning_strength"))
    thinking = _strict_bool(value.get("thinking", False), label="thinking")
    if not thinking:
        return "off"
    legacy = str(value.get("reasoning_effort") or DEFAULT_REASONING_EFFORT).strip().lower()
    if legacy == "low":
        return "low"
    if legacy == "high":
        return "medium"
    if legacy == "max":
        return "high"
    raise ProviderConfigurationError(
        "invalid_reasoning_effort", "reasoning_effort must be low, high or max"
    )


def normalize_base_url(value: Any) -> str:
    untrimmed = str(value or "")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in untrimmed):
        raise ProviderConfigurationError("invalid_endpoint", "provider base_url contains control characters")
    raw = untrimmed.strip()
    if "\\" in raw or re.search(r"%(?:2e|2f|5c)", raw, flags=re.IGNORECASE):
        raise ProviderConfigurationError("invalid_endpoint", "provider base_url contains an ambiguous path")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ProviderConfigurationError("invalid_endpoint", "provider base_url is invalid") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ProviderConfigurationError(
            "invalid_endpoint", "provider base_url must use HTTPS and include a host"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ProviderConfigurationError(
            "invalid_endpoint", "provider base_url must not contain user information"
        )
    if parsed.query or parsed.fragment:
        raise ProviderConfigurationError(
            "invalid_endpoint", "provider base_url must not contain a query or fragment"
        )
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ProviderConfigurationError("unsafe_endpoint", "localhost provider endpoints are blocked")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ProviderConfigurationError(
            "unsafe_endpoint", "private, loopback, link-local and reserved provider addresses are blocked"
        )
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ProviderConfigurationError("invalid_endpoint", "provider host is invalid") from exc
    netloc = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    if port is not None and port != 443:
        netloc += f":{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise ProviderConfigurationError("invalid_endpoint", "provider base_url contains dot segments")
    if path.endswith("/chat/completions"):
        raise ProviderConfigurationError(
            "invalid_endpoint", "base_url must not include /chat/completions"
        )
    return urlunsplit(("https", netloc, path, "", ""))


def endpoint_origin(base_url: Any) -> str:
    raw = str(base_url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ProviderConfigurationError("invalid_endpoint", "provider URL is invalid") from exc
    origin = normalize_base_url(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))
    return origin


def chat_completions_url(base_url: Any) -> str:
    return normalize_base_url(base_url).rstrip("/") + "/chat/completions"


def models_url(base_url: Any) -> str:
    return normalize_base_url(base_url).rstrip("/") + "/models"


def validate_auth(value: Any) -> dict[str, str]:
    auth = dict(value) if isinstance(value, Mapping) else {}
    auth_type = str(auth.get("type") or "bearer").strip().lower()
    if auth_type == "bearer":
        return {"type": "bearer"}
    if auth_type != "header":
        raise ProviderConfigurationError(
            "invalid_auth", "auth.type must be bearer or header"
        )
    header = str(auth.get("header") or "").strip()
    blocked = {
        "authorization", "cookie", "host", "content-length", "content-type",
        "connection", "transfer-encoding", "proxy-authorization",
        "proxy-authenticate", "keep-alive", "te", "trailer", "upgrade",
        "set-cookie",
    }
    if not _HEADER_PATTERN.fullmatch(header) or header.casefold() in blocked:
        raise ProviderConfigurationError("invalid_auth", "custom credential header is unsafe")
    return {"type": "header", "header": header}


def validate_capabilities(value: Any) -> dict[str, Any]:
    source = dict(value) if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {
        key: _strict_bool(source.get(key, False), label=key)
        for key in sorted(_CAPABILITY_KEYS)
    }
    for key in ("max_context_tokens", "max_output_tokens"):
        raw = source.get(key)
        if raw is None:
            result[key] = None
        elif isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise ProviderConfigurationError("invalid_capability", f"{key} must be a positive integer")
        else:
            result[key] = raw
    if not result["text_chat"]:
        for dependent in (
            "streaming", "json_object", "json_schema", "function_tools", "thinking"
        ):
            if result[dependent]:
                raise ProviderConfigurationError(
                    "invalid_capability", f"{dependent} requires text_chat"
                )
    return result


def validate_provider(provider_id: Any, value: Any) -> dict[str, Any]:
    selected_id = validate_identifier(provider_id, label="provider_id")
    if not isinstance(value, Mapping):
        raise ProviderConfigurationError("invalid_provider", "provider must be an object")
    source = dict(value)
    adapter = str(source.get("adapter") or "openai_compatible").strip().lower()
    if adapter not in {"deepseek", "openai_compatible"}:
        raise ProviderConfigurationError(
            "invalid_provider", "adapter must be deepseek or openai_compatible"
        )
    base_url = normalize_base_url(source.get("base_url"))
    auth = validate_auth(source.get("auth"))
    if adapter == "deepseek" and (
        selected_id != "deepseek"
        or base_url != "https://api.deepseek.com"
        or auth != {"type": "bearer"}
    ):
        raise ProviderConfigurationError(
            "invalid_provider",
            "the DeepSeek adapter is reserved for the official HTTPS root with bearer auth",
        )
    models = source.get("models")
    if not isinstance(models, Mapping) or not models:
        raise ProviderConfigurationError("invalid_provider", "provider must declare at least one model")
    normalized_models: dict[str, Any] = {}
    for raw_model, raw_definition in models.items():
        model = validate_model_id(raw_model)
        definition = dict(raw_definition) if isinstance(raw_definition, Mapping) else {}
        evidence = str(definition.get("evidence") or "declared").strip().lower()
        if evidence not in {"declared", "verified_builtin", "verified_live"}:
            raise ProviderConfigurationError("invalid_capability", "invalid capability evidence")
        if evidence == "verified_builtin" and adapter != "deepseek":
            raise ProviderConfigurationError(
                "invalid_capability", "verified_builtin is reserved for the official DeepSeek adapter"
            )
        verified_capabilities = definition.get("verified_capabilities") or []
        if not isinstance(verified_capabilities, list) or any(
            item not in _CAPABILITY_KEYS for item in verified_capabilities
        ):
            raise ProviderConfigurationError("invalid_capability", "verified_capabilities is invalid")
        verified_reasoning = definition.get("verified_reasoning_strengths") or []
        if not isinstance(verified_reasoning, list) or any(
            item not in REASONING_STRENGTHS[1:] for item in verified_reasoning
        ):
            raise ProviderConfigurationError(
                "invalid_capability", "verified_reasoning_strengths is invalid"
            )
        if verified_reasoning and evidence not in {"verified_builtin", "verified_live"}:
            raise ProviderConfigurationError(
                "invalid_capability", "reasoning evidence requires verified capability evidence"
            )
        normalized_models[model] = {
            "capabilities": validate_capabilities(definition.get("capabilities")),
            "evidence": evidence,
            "evidence_hash": (
                str(definition["evidence_hash"])
                if definition.get("evidence_hash") is not None else None
            ),
            "verified_at": (
                str(definition["verified_at"])
                if definition.get("verified_at") is not None else None
            ),
            "verified_capabilities": sorted(set(verified_capabilities)),
            "verified_reasoning_strengths": sorted(
                set(verified_reasoning), key=REASONING_STRENGTHS.index
            ),
            "available": _strict_bool(definition.get("available", True), label="available"),
        }
    if adapter == "deepseek":
        if set(normalized_models) - _DEEPSEEK_MODEL_IDS:
            raise ProviderConfigurationError(
                "invalid_model", "the official DeepSeek adapter received an unsupported model"
            )
        for definition in normalized_models.values():
            definition["evidence"] = "verified_builtin"
            definition["verified_capabilities"] = sorted(
                key for key, value in definition["capabilities"].items()
                if isinstance(value, bool) and value
            )
            definition["verified_reasoning_strengths"] = ["off", "medium", "high"]
    slot = validate_identifier(
        source.get("credential_slot") or selected_id, label="credential_slot"
    )
    name = str(source.get("name") or selected_id).strip()
    if not name or len(name) > 80:
        raise ProviderConfigurationError("invalid_provider", "provider name is invalid")
    return {
        "name": name,
        "adapter": adapter,
        "base_url": base_url,
        "credential_slot": slot,
        "auth": auth,
        "enabled": _strict_bool(source.get("enabled", True), label="enabled"),
        "models": normalized_models,
    }


def validate_profile(profile_id: Any, value: Any, providers: Mapping[str, Any]) -> dict[str, Any]:
    selected_id = validate_identifier(profile_id, label="profile_id")
    if selected_id not in TASK_PROFILE_IDS:
        raise ProviderConfigurationError("invalid_profile", "unknown task profile")
    if not isinstance(value, Mapping):
        raise ProviderConfigurationError("invalid_profile", "task profile must be an object")
    source = dict(value)
    provider_id = validate_identifier(source.get("provider_id"), label="provider_id")
    provider = providers.get(provider_id)
    if not isinstance(provider, Mapping) or not provider.get("enabled"):
        raise ProviderConfigurationError("invalid_profile", "task profile provider is missing or disabled")
    model = validate_model_id(source.get("model"))
    model_definition = (provider.get("models") or {}).get(model)
    if not isinstance(model_definition, Mapping):
        raise ProviderConfigurationError("invalid_profile", "task profile model is not declared by provider")
    capabilities = validate_capabilities(model_definition.get("capabilities"))
    strength = reasoning_strength_from_profile(source)
    thinking, effort = _legacy_reasoning_fields(strength)
    if not capabilities["text_chat"] or (thinking and not capabilities["thinking"]):
        raise ProviderConfigurationError(
            "unsupported_capability", "task profile requests a capability not declared by its model"
        )
    if provider.get("adapter") == "deepseek" and strength == "low":
        raise ProviderConfigurationError(
            "unsupported_reasoning_strength", "DeepSeek does not support low reasoning strength"
        )
    return {
        "provider_id": provider_id,
        "model": model,
        "thinking": thinking,
        "reasoning_effort": effort,
        "reasoning_strength": strength,
    }


def validate_ai_catalog(providers: Any, profiles: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(providers, Mapping) or not providers:
        raise ProviderConfigurationError("invalid_provider", "at least one provider is required")
    normalized_providers: dict[str, Any] = {}
    for key, value in providers.items():
        selected = validate_identifier(key, label="provider_id")
        if selected in normalized_providers:
            raise ProviderConfigurationError("invalid_provider", "provider ids collide after normalization")
        normalized_providers[selected] = validate_provider(key, value)
    source_profiles = dict(profiles) if isinstance(profiles, Mapping) else {}
    if set(source_profiles) != set(TASK_PROFILE_IDS):
        raise ProviderConfigurationError(
            "invalid_profile", "all six task profiles must be configured exactly once"
        )
    normalized_profiles = {
        profile_id: validate_profile(profile_id, source_profiles[profile_id], normalized_providers)
        for profile_id in TASK_PROFILE_IDS
    }
    return normalized_providers, normalized_profiles


def _strict_bool(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ProviderConfigurationError("invalid_provider", f"{label} must be a boolean")
    return value


def capability_profile(provider: Mapping[str, Any], model: str) -> CapabilityProfile:
    definition = (provider.get("models") or {}).get(model)
    if not isinstance(definition, Mapping):
        raise ProviderConfigurationError("invalid_model", "model is not declared by provider")
    value = validate_capabilities(definition.get("capabilities"))
    return CapabilityProfile(
        **value,
        evidence=str(definition.get("evidence") or "declared"),
        evidence_hash=(
            str(definition["evidence_hash"])
            if definition.get("evidence_hash") is not None else None
        ),
        verified_at=(
            str(definition["verified_at"])
            if definition.get("verified_at") is not None else None
        ),
        verified_capabilities=tuple(definition.get("verified_capabilities") or ()),
        verified_reasoning_strengths=tuple(
            definition.get("verified_reasoning_strengths") or ()
        ),
    )


def validate_credential_slots(providers: Mapping[str, Any], value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderConfigurationError("invalid_credential_slot", "credential_slots must be an object")
    result: dict[str, Any] = {}
    for raw_slot, raw_definition in value.items():
        slot = validate_identifier(raw_slot, label="credential_slot")
        if slot in result:
            raise ProviderConfigurationError("invalid_credential_slot", "credential slots collide after normalization")
        if not isinstance(raw_definition, Mapping):
            raise ProviderConfigurationError("invalid_credential_slot", "credential slot must be an object")
        definition = dict(raw_definition)
        provider_id = validate_identifier(definition.get("provider_id"), label="provider_id")
        provider = providers.get(provider_id)
        if not isinstance(provider, Mapping):
            raise ProviderConfigurationError("invalid_credential_slot", "credential slot provider is missing")
        origin = endpoint_origin(definition.get("origin"))
        auth = validate_auth(definition.get("auth"))
        if origin != endpoint_origin(provider.get("base_url")) or auth != provider.get("auth"):
            raise ProviderConfigurationError(
                "invalid_credential_slot", "credential slot binding differs from provider origin/auth"
            )
        environment_variable = str(definition.get("environment_variable") or "").strip()
        if environment_variable and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", environment_variable):
            raise ProviderConfigurationError("invalid_credential_slot", "environment variable name is invalid")
        result[slot] = {
            "provider_id": provider_id,
            "origin": origin,
            "auth": auth,
            "environment_variable": environment_variable,
        }
    for provider_id, provider in providers.items():
        slot = str(provider.get("credential_slot") or "")
        definition = result.get(slot)
        if not isinstance(definition, Mapping) or definition.get("provider_id") != provider_id:
            raise ProviderConfigurationError("invalid_credential_slot", "provider credential slot is missing or bound elsewhere")
    return result


__all__ = [
    "COACHING_DELIVERY_MODES", "DEEPSEEK_CAPABILITIES", "REASONING_STRENGTHS", "TASK_PROFILE_IDS", "capability_profile",
    "chat_completions_url", "models_url", "default_ai_policy", "default_cache_policy", "default_credential_slots", "default_provider_config", "default_task_profiles",
    "endpoint_origin", "normalize_base_url", "validate_ai_catalog", "validate_auth",
    "reasoning_strength_from_profile", "validate_capabilities", "validate_identifier", "validate_model_id",
    "validate_reasoning_strength", "validate_coaching_delivery_mode", "validate_ai_policy", "validate_cache_policy",
    "validate_credential_slots", "validate_profile", "validate_provider",
]
