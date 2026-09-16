"""Provider routing, capability gates and credential-bound client creation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from typing import Any, Callable, Mapping

from .credentials import CredentialStoreError, CredentialVault
from .deepseek import DeepSeekClient
from .openai_compatible import OpenAICompatibleClient
from .openai_responses import OpenAIResponsesClient
from .provider import CapabilityProfile, ProviderConfigurationError, ProviderPort
from .provider_config import (
    TASK_PROFILE_IDS,
    capability_profile,
    default_ai_policy,
    effective_capabilities,
    endpoint_origin,
    validate_ai_catalog,
    validate_ai_policy,
    validate_credential_slots,
    validate_identifier,
    validate_model_id,
    validate_capabilities,
    validate_reasoning_strength,
)


_PROFILE_CAPABILITIES = {
    "recommendation": ("text_chat", "json_object"),
    "plan_organize": ("text_chat", "json_object"),
    "plan_generate": ("text_chat", "json_object"),
    "coaching": ("text_chat", "streaming"),
    "patch": ("text_chat", "json_object"),
    "summary": ("text_chat", "json_object"),
}


def model_wire(provider: Mapping[str, Any], model: str) -> dict[str, Any]:
    return dict(((provider.get("models") or {}).get(model) or {}).get("wire_profile") or {})


def model_adapter(provider: Mapping[str, Any], model: str) -> str:
    return str(model_wire(provider, model).get("adapter") or provider.get("adapter") or "openai_compatible")


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    profile_id: str
    provider_id: str
    model: str
    reasoning_strength: str
    thinking: bool
    reasoning_effort: str
    provider: dict[str, Any]
    capabilities: CapabilityProfile
    budget: dict[str, Any]


def provider_definition_hash(provider_id: str, provider: Mapping[str, Any], model: str) -> str:
    document = {
        "provider_id": provider_id,
        "adapter": provider.get("adapter"),
        "base_url": provider.get("base_url"),
        "auth": provider.get("auth"),
        "headers": provider.get("headers") or {},
        "wire_profile": model_wire(provider, model),
        "native_capabilities": validate_capabilities(((provider.get("models") or {}).get(model) or {}).get("capabilities")),
        "model": model,
        "capabilities": effective_capabilities(provider, model),
        "reasoning_wire": (
            "deepseek_thinking" if provider.get("adapter") == "deepseek"
            else "responses_reasoning_effort" if provider.get("adapter") == "openai_responses"
            else "openai_reasoning_effort"
        ),
        "conformance_version": 4,
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_reasoning_options(adapter: str, reasoning_strength: str) -> tuple[bool, str]:
    """Map the public contract to one adapter's call options.

    ``auto`` is a strict provider default: adapters must omit reasoning controls.
    ``off`` is explicit and therefore remains capability/conformance gated.
    """

    strength = validate_reasoning_strength(reasoning_strength)
    selected_adapter = str(adapter or "").strip().lower()
    if selected_adapter == "deepseek":
        if strength == "low":
            raise ProviderConfigurationError(
                "unsupported_reasoning_strength",
                "DeepSeek does not support low reasoning strength",
            )
        return {
            "auto": (False, "auto"),
            "off": (False, "high"),
            "medium": (True, "high"),
            "high": (True, "max"),
        }[strength]
    if selected_adapter in {"openai_compatible", "openai_responses", "anthropic", "auto"}:
        if strength == "auto":
            return False, "auto"
        if strength == "off":
            return True, "none"
        return True, strength
    raise ProviderConfigurationError("invalid_provider", "unknown provider adapter")


def required_capabilities(profile_id: str) -> tuple[str, ...]:
    selected = str(profile_id or "").strip().lower()
    try:
        return tuple(_PROFILE_CAPABILITIES[selected])
    except KeyError:
        raise ProviderConfigurationError("invalid_profile", "unknown task profile") from None


def _model_budget(budget: Mapping[str, Any], capabilities: CapabilityProfile) -> dict[str, Any]:
    result = dict(budget)
    if capabilities.max_output_tokens is not None:
        result["max_output_tokens"] = min(
            int(result["max_output_tokens"]), capabilities.max_output_tokens
        )
    return result


class ProviderRegistry:
    def __init__(
        self,
        ai_config: Mapping[str, Any],
        *,
        credential_vault: CredentialVault | None = None,
        injected_factory: Callable[[], Any] | None = None,
    ) -> None:
        providers, profiles = validate_ai_catalog(
            ai_config.get("providers"), ai_config.get("profiles")
        )
        slots = validate_credential_slots(providers, ai_config.get("credential_slots"))
        self.providers = providers
        self.profiles = profiles
        self.credential_slots = slots
        self.policy = validate_ai_policy(ai_config.get("policy") or default_ai_policy())
        self.credential_vault = credential_vault
        self.injected_factory = injected_factory

    def route(
        self,
        profile_id: str,
        *,
        model_ref: Mapping[str, Any] | None = None,
        reasoning_strength: str | None = None,
        model_override: str | None = None,
        require_verified: bool = True,
    ) -> ProviderRoute:
        selected_profile = str(profile_id).strip().lower()
        if selected_profile not in TASK_PROFILE_IDS:
            raise ProviderConfigurationError("invalid_profile", "unknown task profile")
        profile = dict(self.profiles[selected_profile])
        if model_ref is not None and not isinstance(model_ref, Mapping):
            raise ProviderConfigurationError("invalid_model_ref", "model_ref must be an object")
        if model_ref is not None and model_override is not None:
            raise ProviderConfigurationError(
                "invalid_model_ref", "model_ref and legacy model_override are mutually exclusive"
            )
        if model_ref is not None and set(model_ref) != {"provider_id", "model"}:
            raise ProviderConfigurationError(
                "invalid_model_ref", "model_ref must contain exactly provider_id and model"
            )
        provider_id = validate_identifier(
            model_ref.get("provider_id") if model_ref is not None else profile["provider_id"],
            label="provider_id",
        )
        provider = self.providers.get(provider_id)
        if not isinstance(provider, Mapping) or not provider.get("enabled"):
            raise ProviderConfigurationError(
                "invalid_provider", "selected provider does not exist or is disabled"
            )
        provider = dict(provider)
        model = validate_model_id(
            model_ref.get("model") if model_ref is not None else (model_override or profile["model"])
        )
        model_definition = (provider.get("models") or {}).get(model)
        if not isinstance(model_definition, Mapping):
            raise ProviderConfigurationError(
                "invalid_model", "所选模型已不在连接的模型列表中，请重新选择模型"
            )
        if not bool(model_definition.get("available", True)):
            raise ProviderConfigurationError(
                "model_unavailable", "selected model is no longer advertised by its provider"
            )
        strength = validate_reasoning_strength(
            reasoning_strength if reasoning_strength is not None
            else profile.get("reasoning_strength", "auto")
        )
        thinking, effort = resolve_reasoning_options(model_adapter(provider, model), strength)
        capabilities = capability_profile(provider, model)
        required = list(_PROFILE_CAPABILITIES[selected_profile])
        if thinking:
            required.append("thinking")
        missing = [name for name in required if not bool(getattr(capabilities, name, False))]
        if missing:
            raise ProviderConfigurationError(
                "unsupported_capability",
                f"task profile {selected_profile} lacks: {', '.join(missing)}",
            )
        if require_verified and capabilities.evidence not in {"verified_builtin", "verified_live"}:
            raise ProviderConfigurationError(
                "unverified_capability",
                "provider/model capabilities must pass conformance before production routing",
            )
        if capabilities.evidence == "verified_live":
            unverified = [
                name for name in required if name not in capabilities.verified_capabilities
            ]
            if unverified:
                raise ProviderConfigurationError(
                    "unverified_capability",
                    "task profile capabilities were not covered by live conformance: "
                    + ", ".join(unverified),
                )
        if require_verified and strength != "auto" and (
            strength not in capabilities.verified_reasoning_strengths
        ):
            raise ProviderConfigurationError(
                "unverified_reasoning_strength",
                "the selected reasoning strength has not passed live conformance",
            )
        if capabilities.evidence == "verified_live":
            expected = provider_definition_hash(provider_id, provider, model)
            if capabilities.evidence_hash != expected:
                raise ProviderConfigurationError(
                    "stale_capability_evidence",
                    "provider/model conformance evidence no longer matches its definition",
                )
        return ProviderRoute(
            profile_id=selected_profile,
            provider_id=provider_id,
            model=model,
            reasoning_strength=strength,
            thinking=thinking,
            reasoning_effort=effort,
            provider=provider,
            capabilities=capabilities,
            budget=_model_budget(self.policy["budgets"][selected_profile], capabilities),
        )

    def route_plan(
        self,
        profile_id: str,
        *,
        primary: ProviderRoute | None = None,
        model_ref: Mapping[str, Any] | None = None,
        reasoning_strength: str | None = None,
        model_override: str | None = None,
    ) -> tuple[ProviderRoute, ...]:
        """Resolve and capability-check the primary plus configured fallbacks."""

        selected = primary or self.route(
            profile_id,
            model_ref=model_ref,
            reasoning_strength=reasoning_strength,
            model_override=model_override,
        )
        routes = [selected]
        seen = {(selected.provider_id, selected.model, selected.reasoning_strength)}
        for definition in self.policy["fallbacks"][selected.profile_id]:
            route = self.route(
                selected.profile_id,
                model_ref={
                    "provider_id": definition["provider_id"],
                    "model": definition["model"],
                },
                reasoning_strength=definition["reasoning_strength"],
            )
            identity = (route.provider_id, route.model, route.reasoning_strength)
            if identity in seen:
                raise ProviderConfigurationError(
                    "invalid_fallback", "fallback route duplicates the primary or an earlier route"
                )
            seen.add(identity)
            routes.append(route)
        return tuple(routes)

    def _secret(self, provider_id: str, provider: Mapping[str, Any]) -> tuple[str | None, str]:
        slot = str(provider["credential_slot"])
        definition = self.credential_slots[slot]
        if self.credential_vault is not None:
            try:
                credential = self.credential_vault.load_bound(
                    slot,
                    provider_id=provider_id,
                    origin=str(definition["origin"]),
                    auth=dict(definition["auth"]),
                )
                if credential is not None:
                    return credential.secret, "secure_store"
            except CredentialStoreError as exc:
                if exc.code not in {
                    "credential_store_unavailable",
                    "credential_store_locked",
                }:
                    raise
        variable = str(definition.get("environment_variable") or "")
        secret = str(os.environ.get(variable) or "").strip() if variable else ""
        return (secret or None), ("environment" if secret else "none")

    def client_for_route(self, route: ProviderRoute, *, timeout: float = 60.0) -> ProviderPort:
        if self.injected_factory is not None:
            return self.injected_factory()
        provider = route.provider
        secret, _source = self._secret(route.provider_id, provider)
        wire = model_wire(provider, route.model)
        adapter = model_adapter(provider, route.model)
        if provider.get("protocol_mode") == "auto" or adapter == "anthropic":
            wire.setdefault("structured_output", "native_schema")
        models = {
            model: capability_profile(provider, model)
            for model in (provider.get("models") or {})
        }
        if adapter == "deepseek":
            if endpoint_origin(provider["base_url"]) != "https://api.deepseek.com":
                raise ProviderConfigurationError(
                    "invalid_endpoint", "the DeepSeek adapter is pinned to the official origin"
                )
            return DeepSeekClient(api_key=secret, models=models, timeout=timeout, retries=0)
        if adapter == "auto":
            raise ProviderConfigurationError("unverified_protocol", "连接协议尚未验证。")
        if adapter == "anthropic":
            from .anthropic import AnthropicClient
            client_type = AnthropicClient
        else:
            client_type = OpenAIResponsesClient if adapter == "openai_responses" else OpenAICompatibleClient
        return client_type(
            api_key=secret,
            provider_id=route.provider_id,
            base_url=str(wire.get("base_url") or provider["base_url"]),
            auth=dict(wire.get("auth") or provider["auth"]),
            headers={**dict(provider.get("headers") or {}), **dict(wire.get("headers") or {})},
            wire_profile=wire,
            models=models,
            credential_origin=str(self.credential_slots[provider["credential_slot"]]["origin"]),
            thinking_wire="none",
            timeout=timeout,
            retries=0,
        )

    def probe_route(
        self,
        provider_id: str,
        model: str | None = None,
        *,
        reasoning_strength: str = "auto",
        profile_id: str = "recommendation",
    ) -> ProviderRoute:
        selected_profile = str(profile_id).strip().lower()
        if selected_profile not in TASK_PROFILE_IDS:
            raise ProviderConfigurationError("invalid_profile", "unknown task profile")
        selected_provider = validate_identifier(provider_id, label="provider_id")
        provider = self.providers.get(selected_provider)
        if not isinstance(provider, Mapping) or not provider.get("enabled"):
            raise ProviderConfigurationError("invalid_provider", "provider does not exist or is disabled")
        models = list((provider.get("models") or {}).keys())
        selected_model = validate_model_id(model or (models[0] if models else ""))
        capabilities = capability_profile(provider, selected_model)
        if not capabilities.text_chat:
            raise ProviderConfigurationError("unsupported_capability", "model does not declare text_chat")
        strength = validate_reasoning_strength(reasoning_strength)
        thinking, effort = resolve_reasoning_options(model_adapter(provider, selected_model), strength)
        if thinking and not capabilities.thinking:
            raise ProviderConfigurationError(
                "unsupported_capability", "model does not declare reasoning support"
            )
        return ProviderRoute(
            profile_id=selected_profile,
            provider_id=selected_provider,
            model=selected_model,
            reasoning_strength=strength,
            thinking=thinking,
            reasoning_effort=effort,
            provider=dict(provider),
            capabilities=capabilities,
            budget=_model_budget(self.policy["budgets"][selected_profile], capabilities),
        )

    def credential_source(self, provider_id: str) -> str:
        provider = self.providers.get(provider_id)
        if not isinstance(provider, Mapping):
            raise ProviderConfigurationError("invalid_provider", "provider does not exist")
        return self._secret(provider_id, provider)[1]


__all__ = [
    "ProviderRegistry", "ProviderRoute", "provider_definition_hash",
    "required_capabilities", "resolve_reasoning_options",
]
