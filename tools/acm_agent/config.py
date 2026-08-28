from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

from .ai_policy import DEFAULT_MODEL, DEFAULT_REASONING_EFFORT
from .provider_config import (
    default_ai_policy,
    default_cache_policy,
    default_provider_config,
    default_credential_slots,
    default_task_profiles,
    validate_ai_catalog,
    validate_ai_policy,
    validate_cache_policy,
    validate_coaching_delivery_mode,
    validate_credential_slots,
)


CONFIG_VERSION = 16
# Compatibility-only conversion for explicitly configured v14 USD guardrails.
# DeepSeek usage itself is always priced from the native CNY catalog.
LEGACY_LIMIT_USD_TO_CNY_RATE = 7.2

_V15_DEFAULT_TOKEN_BUDGETS: dict[str, dict[str, int]] = {
    "recommendation": {"max_output_tokens": 2_400, "max_total_tokens": 240_000},
    "plan_organize": {"max_output_tokens": 12_000, "max_total_tokens": 120_000},
    "plan_generate": {"max_output_tokens": 24_000, "max_total_tokens": 300_000},
    "coaching": {"max_output_tokens": 4_096, "max_total_tokens": 150_000},
    "patch": {"max_output_tokens": 8_192, "max_total_tokens": 200_000},
    "summary": {"max_output_tokens": 6_000, "max_total_tokens": 180_000},
}

_REASONING_STRENGTHS = frozenset({"auto", "off", "low", "medium", "high"})


@dataclass(frozen=True)
class Paths:
    root: Path
    state_dir: Path
    config: Path
    database: Path
    cache: Path
    build: Path
    cases: Path
    reports: Path
    failures: Path
    plan: Path
    plan_readme: Path

    @classmethod
    def for_root(cls, root: Path) -> "Paths":
        root = root.resolve()
        state = root / ".acm"
        return cls(
            root=root,
            state_dir=state,
            config=state / "config.json",
            database=state / "state.db",
            cache=state / "cache",
            build=state / "build",
            cases=state / "cases",
            reports=state / "reports",
            failures=state / "failures",
            plan=root / "training" / "data-structures-30d" / "plan.json",
            plan_readme=root / "training" / "data-structures-30d" / "README.md",
        )

    def ensure(self) -> None:
        for path in (
            self.state_dir,
            self.cache,
            self.build,
            self.cases,
            self.reports,
            self.failures,
        ):
            path.mkdir(parents=True, exist_ok=True)


DEFAULT_CONFIG: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "accounts": {
        "codeforces": {"handle": ""},
        "luogu": {"uid": ""},
    },
    "recommendation": {
        "mode": "plan_first",
        "count": 3,
        "target_cf_rating": None,
        "platform_ratio": {"codeforces": 0.6, "luogu": 0.4},
    },
    "sync": {
        "status_ttl_hours": 6,
        "catalog_ttl_hours": 24,
        "timeout_seconds": 20,
    },
    "ai": {
        "recommendation_model": DEFAULT_MODEL,
        "coaching_model": DEFAULT_MODEL,
        "summary_model": DEFAULT_MODEL,
        "recommendation_thinking": False,
        "coaching_thinking": True,
        "summary_thinking": True,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "summary_reasoning_effort": DEFAULT_REASONING_EFFORT,
        "providers": default_provider_config(),
        "profiles": default_task_profiles(),
        "credential_slots": default_credential_slots(),
        "policy": default_ai_policy(),
        "cache": default_cache_policy(),
        "coaching_delivery_mode": "resilient",
    },
}


def _is_sensitive_ai_key(key: Any) -> bool:
    normalized = "".join(
        character for character in str(key).casefold() if character.isalnum()
    )
    return normalized in {
        "apikey", "authorization", "accesstoken", "authtoken", "bearertoken",
        "clientsecret", "secret", "secretkey", "token",
    } or normalized.endswith(("apikey", "accesstoken", "clientsecret", "secretkey"))


def _find_sensitive_key(value: Any, *, path: str = "ai") -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            current = f"{path}.{key}"
            if _is_sensitive_ai_key(key):
                return current
            nested = _find_sensitive_key(item, path=current)
            if nested is not None:
                return nested
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            nested = _find_sensitive_key(item, path=f"{path}[{index}]")
            if nested is not None:
                return nested
    return None


def _project_legacy_ai_settings(ai: dict[str, Any]) -> None:
    profiles = ai.get("profiles")
    if not isinstance(profiles, dict):
        return
    recommendation = profiles.get("recommendation") or {}
    coaching = profiles.get("coaching") or {}
    summary = profiles.get("summary") or {}

    def legacy_reasoning(profile: Mapping[str, Any]) -> tuple[bool, str]:
        strength = str(profile.get("reasoning_strength") or "auto").strip().lower()
        if strength not in _REASONING_STRENGTHS:
            strength = "auto"
        return strength not in {"auto", "off"}, "max" if strength == "high" else "high"

    recommendation_thinking, _ = legacy_reasoning(recommendation)
    coaching_thinking, coaching_effort = legacy_reasoning(coaching)
    summary_thinking, summary_effort = legacy_reasoning(summary)
    ai.update(
        recommendation_model=recommendation.get("model", DEFAULT_MODEL),
        recommendation_thinking=recommendation_thinking,
        coaching_model=coaching.get("model", DEFAULT_MODEL),
        coaching_thinking=coaching_thinking,
        reasoning_effort=coaching_effort,
        summary_model=summary.get("model", DEFAULT_MODEL),
        summary_thinking=summary_thinking,
        summary_reasoning_effort=summary_effort,
    )


def _upgrade_profile_reasoning(profile: dict[str, Any]) -> None:
    """Add the v10 authority while retaining fields consumed by v9 callers."""

    raw_strength = profile.get("reasoning_strength")
    if raw_strength is None:
        thinking = profile.get("thinking", False)
        if not isinstance(thinking, bool):
            raise ValueError("profile thinking must be a boolean")
        effort = str(profile.get("reasoning_effort") or DEFAULT_REASONING_EFFORT).lower()
        if effort not in {"high", "max"}:
            raise ValueError("profile reasoning_effort must be high or max")
        strength = "off" if not thinking else ("high" if effort == "max" else "medium")
    else:
        strength = str(raw_strength).strip().lower()
        if strength not in _REASONING_STRENGTHS:
            raise ValueError(
                "profile reasoning_strength must be auto, off, low, medium, or high"
            )
    profile["reasoning_strength"] = strength
    # Keep a deterministic compatibility projection for old CLI/service paths.
    profile["thinking"] = strength not in {"auto", "off"}
    profile["reasoning_effort"] = "max" if strength == "high" else "high"


def _upgrade_profiles_reasoning(profiles: Any) -> None:
    if not isinstance(profiles, dict):
        return
    for profile in profiles.values():
        if isinstance(profile, dict):
            _upgrade_profile_reasoning(profile)


def _migrate_pre_v14_deepseek_auto(ai: dict[str, Any]) -> None:
    """Preserve the old DeepSeek ``auto`` wire behavior as explicit ``off``."""

    providers = ai.get("providers")
    if not isinstance(providers, Mapping):
        return

    def migrate_route(route: Any) -> None:
        if not isinstance(route, dict):
            return
        provider = providers.get(str(route.get("provider_id") or ""))
        if (
            isinstance(provider, Mapping)
            and provider.get("adapter") == "deepseek"
            and str(route.get("reasoning_strength") or "auto").strip().lower() == "auto"
        ):
            route["reasoning_strength"] = "off"

    profiles = ai.get("profiles")
    if isinstance(profiles, dict):
        for profile in profiles.values():
            migrate_route(profile)
    policy = ai.get("policy")
    fallbacks = policy.get("fallbacks") if isinstance(policy, dict) else None
    if isinstance(fallbacks, dict):
        for routes in fallbacks.values():
            if isinstance(routes, list):
                for route in routes:
                    migrate_route(route)


def _merge_defaults(defaults: Any, current: Any) -> Any:
    """Return ``current`` overlaid on defaults without dropping unknown keys."""
    if not isinstance(defaults, dict) or not isinstance(current, dict):
        return current
    merged = {key: json.loads(json.dumps(value)) for key, value in defaults.items()}
    for key, value in current.items():
        if key in defaults:
            merged[key] = _merge_defaults(defaults[key], value)
        else:
            merged[key] = value
    return merged


def _upgrade_v15_default_token_budgets(
    ai: dict[str, Any], source_ai: Mapping[str, Any]
) -> None:
    """Raise untouched v15 token defaults without overwriting user tuning."""

    policy = ai.get("policy")
    source_policy = source_ai.get("policy")
    if not isinstance(policy, dict) or not isinstance(source_policy, Mapping):
        return
    budgets = policy.get("budgets")
    source_budgets = source_policy.get("budgets")
    if not isinstance(budgets, dict) or not isinstance(source_budgets, Mapping):
        return
    new_budgets = default_ai_policy()["budgets"]
    for profile_id, old_values in _V15_DEFAULT_TOKEN_BUDGETS.items():
        budget = budgets.get(profile_id)
        source_budget = source_budgets.get(profile_id)
        if not isinstance(budget, dict) or not isinstance(source_budget, Mapping):
            continue
        for field, old_value in old_values.items():
            if source_budget.get(field) == old_value:
                budget[field] = new_budgets[profile_id][field]


def _upgrade_config(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    version = data.get("version")
    if version not in tuple(range(1, CONFIG_VERSION + 1)):
        raise ValueError(f"Unsupported config version: {version!r}")
    upgraded = _merge_defaults(DEFAULT_CONFIG, data)
    ai = upgraded.get("ai")
    if isinstance(ai, dict):
        source_ai = data.get("ai") if isinstance(data.get("ai"), dict) else {}
        if version in (1, 2):
            # Phase-three summaries are coaching-like calls.  Existing users
            # therefore keep their explicit coaching model/thinking choices
            # instead of silently reverting to the new-install defaults.
            if "summary_model" not in source_ai:
                ai["summary_model"] = ai.get(
                    "coaching_model", DEFAULT_CONFIG["ai"]["summary_model"]
                )
            if "summary_thinking" not in source_ai:
                ai["summary_thinking"] = ai.get(
                    "coaching_thinking", DEFAULT_CONFIG["ai"]["summary_thinking"]
                )
            if "summary_reasoning_effort" not in source_ai:
                ai["summary_reasoning_effort"] = ai.get(
                    "reasoning_effort",
                    DEFAULT_CONFIG["ai"]["summary_reasoning_effort"],
                )
        for retired_key in (
            "validation_model",
            "validation_thinking",
            "validation_reasoning_effort",
            "stress_prepare_timeout_seconds",
            "stress_generation_mode",
        ):
            ai.pop(retired_key, None)
        if version <= 8 or not isinstance(source_ai.get("profiles"), dict):
            profiles = default_task_profiles()
            legacy_model = str(ai.get("recommendation_model") or DEFAULT_MODEL)
            profiles["recommendation"]["model"] = legacy_model
            profiles["plan_organize"]["model"] = legacy_model
            profiles["plan_generate"]["model"] = legacy_model
            profiles["coaching"].update(
                model=str(ai.get("coaching_model") or DEFAULT_MODEL),
                thinking=bool(ai.get("coaching_thinking", True)),
                reasoning_effort=str(ai.get("reasoning_effort") or DEFAULT_REASONING_EFFORT),
            )
            profiles["patch"].update(profiles["coaching"])
            profiles["summary"].update(
                model=str(ai.get("summary_model") or DEFAULT_MODEL),
                thinking=bool(ai.get("summary_thinking", True)),
                reasoning_effort=str(
                    ai.get("summary_reasoning_effort") or DEFAULT_REASONING_EFFORT
                ),
            )
            ai["profiles"] = profiles
        if version <= 9 and isinstance(ai.get("profiles"), dict):
            source_profiles = (
                source_ai.get("profiles")
                if isinstance(source_ai.get("profiles"), dict)
                else {}
            )
            for profile_id, profile in ai["profiles"].items():
                source_profile = source_profiles.get(profile_id)
                if (
                    version <= 8
                    or not isinstance(source_profile, dict)
                    or "reasoning_strength" not in source_profile
                ) and isinstance(profile, dict):
                    # ``_merge_defaults`` may have injected the v10 default;
                    # derive from the persisted v9 thinking/effort instead.
                    profile.pop("reasoning_strength", None)
        _upgrade_profiles_reasoning(ai.get("profiles"))
        if version <= 13:
            _migrate_pre_v14_deepseek_auto(ai)
        if version <= 14:
            policy = ai.get("policy")
            if isinstance(policy, dict):
                limits = policy.get("hard_limits")
                if isinstance(limits, dict) and (
                    "daily_usd" in limits or "monthly_usd" in limits
                ):
                    policy["hard_limits"] = {
                        "daily_cny": (
                            None if limits.get("daily_usd") is None
                            else float(limits["daily_usd"]) * LEGACY_LIMIT_USD_TO_CNY_RATE
                        ),
                        "monthly_cny": (
                            None if limits.get("monthly_usd") is None
                            else float(limits["monthly_usd"]) * LEGACY_LIMIT_USD_TO_CNY_RATE
                        ),
                    }
        if version <= 15:
            _upgrade_v15_default_token_budgets(ai, source_ai)
        providers, profiles = validate_ai_catalog(ai.get("providers"), ai.get("profiles"))
        _upgrade_profiles_reasoning(profiles)
        ai["providers"] = providers
        ai["profiles"] = profiles
        ai["credential_slots"] = validate_credential_slots(
            providers, ai.get("credential_slots")
        )
        ai["policy"] = validate_ai_policy(ai.get("policy"))
        ai["cache"] = validate_cache_policy(ai.get("cache"))
        ai["coaching_delivery_mode"] = validate_coaching_delivery_mode(
            ai.get("coaching_delivery_mode")
        )
        _project_legacy_ai_settings(ai)
        for key in list(ai):
            if _is_sensitive_ai_key(key):
                del ai[key]
    upgraded["version"] = CONFIG_VERSION
    return upgraded, upgraded != data


def load_config(paths: Paths, *, required: bool = True) -> dict[str, Any]:
    if not paths.config.exists():
        if required:
            raise FileNotFoundError(
                f"Configuration not found: {paths.config}. Run '.\\acm.ps1 init' first."
            )
        return json.loads(json.dumps(DEFAULT_CONFIG))
    data = json.loads(paths.config.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a JSON object")
    sensitive = _find_sensitive_key(data.get("ai"))
    if sensitive is not None and data.get("version") == CONFIG_VERSION:
        raise ValueError(f"Sensitive AI credential field is forbidden in config: {sensitive}")
    config, changed = _upgrade_config(data)
    if changed:
        save_config(paths, config)
    return config


def save_config(paths: Paths, config: dict[str, Any]) -> None:
    sensitive = _find_sensitive_key(config.get("ai"))
    if sensitive is not None:
        raise ValueError(f"Sensitive AI credential field is forbidden in config: {sensitive}")
    ai = config.get("ai")
    if isinstance(ai, dict):
        _upgrade_profiles_reasoning(ai.get("profiles"))
        providers, profiles = validate_ai_catalog(ai.get("providers"), ai.get("profiles"))
        _upgrade_profiles_reasoning(profiles)
        ai["providers"] = providers
        ai["profiles"] = profiles
        ai["credential_slots"] = validate_credential_slots(
            providers, ai.get("credential_slots")
        )
        ai["policy"] = validate_ai_policy(ai.get("policy"))
        ai["cache"] = validate_cache_policy(ai.get("cache"))
        ai["coaching_delivery_mode"] = validate_coaching_delivery_mode(
            ai.get("coaching_delivery_mode")
        )
        _project_legacy_ai_settings(ai)
    config["version"] = CONFIG_VERSION
    paths.ensure()
    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix="config-", suffix=".json", dir=paths.state_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, paths.config)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
