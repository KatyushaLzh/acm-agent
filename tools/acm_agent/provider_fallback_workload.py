"""Isolated, auditable cross-provider fallback acceptance workload.

The workload never changes the configured policy, credential vault, or SQLite
state.  It builds an in-memory policy overlay, performs one real request on the
configured primary, converts the observed response into a controlled retryable
provider error, and requires the governed fallback leg to succeed on a second
verified provider.  Reports contain no prompts, response content, credentials,
provider origins, response ids, or filesystem paths.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Sequence

from .config import Paths, load_config
from .credentials import CredentialVault, create_platform_credential_vault
from .provider import AIJsonResult, ProviderConfigurationError, ProviderError, ProviderPort
from .provider_governance import GovernedProviderClient, governance_from_result
from .provider_registry import ProviderRegistry, ProviderRoute
from .usage import normalize_usage


WORKLOAD_VERSION = 1
PROFILE_ID = "plan_organize"
FAILURE_CODE = "workload_injected_retryable"
_EXPECTED_DATA = {"fallback_e2e": "ok"}
_MESSAGES: tuple[dict[str, str], ...] = (
    {
        "role": "system",
        "content": "Return exactly one JSON object and no other text.",
    },
    {
        "role": "user",
        "content": 'Return {"fallback_e2e":"ok"}.',
    },
)
_SOURCE_UNITS = {
    "workload": "provider_fallback_workload.py",
    "governance": "provider_governance.py",
    "registry": "provider_registry.py",
    "provider_contract": "provider.py",
}
_FORBIDDEN_REPORT_KEYS = frozenset(
    {
        "authorization",
        "base_url",
        "content",
        "credential",
        "credential_slot",
        "message",
        "origin",
        "path",
        "prompt",
        "response_id",
        "secret",
    }
)


class FallbackWorkloadError(RuntimeError):
    """Safe, finite failure raised when the acceptance gate fails closed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = str(code)


def _route_fact(route: ProviderRoute, *, resolved_model: str | None = None) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "provider_id": route.provider_id,
        "model": route.model,
        "reasoning_strength": route.reasoning_strength,
    }
    if resolved_model is not None:
        fact["resolved_model"] = str(resolved_model)
    return fact


def _configuration_hash(routes: Sequence[ProviderRoute]) -> str:
    document = {
        "profile_id": PROFILE_ID,
        "routes": [
            {
                **_route_fact(route),
                "adapter": str(route.provider["adapter"]),
                "capability_evidence": route.capabilities.evidence,
                "capability_evidence_hash": route.capabilities.evidence_hash,
                "verified_capabilities": list(route.capabilities.verified_capabilities),
                "verified_reasoning_strengths": list(
                    route.capabilities.verified_reasoning_strengths
                ),
            }
            for route in routes
        ],
        "budget": dict(routes[0].budget),
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git_head(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip().lower()
    return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None


def _source_evidence(root: Path) -> dict[str, Any]:
    module_dir = Path(__file__).resolve().parent
    hashes: dict[str, str] = {}
    bundle = hashlib.sha256()
    for label, filename in sorted(_SOURCE_UNITS.items()):
        payload = (module_dir / filename).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        hashes[label] = digest
        bundle.update(label.encode("ascii"))
        bundle.update(b"\0")
        bundle.update(payload)
        bundle.update(b"\0")
    return {
        "git_head": _git_head(root),
        "source_units": hashes,
        "source_bundle_sha256": bundle.hexdigest(),
    }


def _verified_cross_provider_candidate(
    registry: ProviderRegistry, primary: ProviderRoute
) -> ProviderRoute:
    candidates: list[ProviderRoute] = []
    for provider_id in sorted(registry.providers):
        if provider_id == primary.provider_id:
            continue
        provider = registry.providers[provider_id]
        if not bool(provider.get("enabled")):
            continue
        for model in sorted((provider.get("models") or {})):
            try:
                candidate = registry.route(
                    PROFILE_ID,
                    model_ref={"provider_id": provider_id, "model": model},
                    reasoning_strength="off",
                    require_verified=True,
                )
            except ProviderError:
                continue
            candidates.append(candidate)
    if not candidates:
        raise FallbackWorkloadError("verified_cross_provider_route_unavailable")
    return candidates[0]


def prepare_route_plan(
    ai_config: Mapping[str, Any],
    *,
    credential_vault: CredentialVault | None = None,
) -> tuple[ProviderRegistry, tuple[ProviderRoute, ProviderRoute]]:
    """Return a two-leg verified route plan without mutating ``ai_config``."""

    original = ProviderRegistry(ai_config, credential_vault=credential_vault)
    primary = original.route(PROFILE_ID, require_verified=True)
    fallback = _verified_cross_provider_candidate(original, primary)
    overlay = deepcopy(dict(ai_config))
    policy = deepcopy(dict(original.policy))
    policy["budgets"][PROFILE_ID] = {
        "max_output_tokens": 64,
        "request_timeout_seconds": 90.0,
        "max_retries": 0,
        "max_validation_repairs": 0,
        "max_requests": 2,
        # Some OpenAI-compatible relays report a fixed protocol/context
        # envelope of roughly 4.4k input tokens even for this tiny canary.
        # Keep the output and request counts tight while leaving enough shared
        # observed-token budget for both real legs.
        "max_total_tokens": 16_384,
    }
    policy["fallbacks"][PROFILE_ID] = [
        {
            "provider_id": fallback.provider_id,
            "model": fallback.model,
            "reasoning_strength": fallback.reasoning_strength,
        }
    ]
    overlay["policy"] = policy
    registry = ProviderRegistry(overlay, credential_vault=credential_vault)
    routes = registry.route_plan(PROFILE_ID)
    if len(routes) != 2 or routes[0].provider_id == routes[1].provider_id:
        raise FallbackWorkloadError("invalid_cross_provider_route_plan")
    return registry, (routes[0], routes[1])


class _RetryableFailureAfterResponse:
    """Turn one observed primary response into a controlled provider failure."""

    def __init__(self, inner: ProviderPort) -> None:
        self.inner = inner
        self.observed_response = False

    @property
    def key_detected(self) -> bool:
        return bool(self.inner.key_detected)

    @property
    def request_attempts(self) -> int:
        value = getattr(self.inner, "request_attempts", 0)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    def chat_json(self, messages: Sequence[Mapping[str, Any]], **options: Any) -> AIJsonResult:
        result = self.inner.chat_json(messages, **options)
        self.observed_response = True
        raise ProviderError(
            FAILURE_CODE,
            "controlled fallback workload failure",
            retryable=True,
            usage=result.usage,
            finish_reason=result.finish_reason,
            model=result.resolved_model,
            requested_model=result.requested_model,
        )


def _validate_result(result: AIJsonResult) -> None:
    if type(result.data) is not dict or result.data != _EXPECTED_DATA:
        raise FallbackWorkloadError("fallback_response_validation_failed")
    if result.finish_reason not in {None, "stop"}:
        raise FallbackWorkloadError("fallback_response_incomplete")


def _assert_report_safe(value: Any, *, root: Path) -> None:
    root_text = str(root.resolve()).casefold()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).strip().casefold()
                if normalized in _FORBIDDEN_REPORT_KEYS or any(
                    token in normalized
                    for token in ("api_key", "access_token", "auth_token", "client_secret")
                ):
                    raise FallbackWorkloadError("unsafe_report_key")
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
        elif isinstance(item, str):
            folded = item.casefold()
            if root_text and root_text in folded:
                raise FallbackWorkloadError("unsafe_report_value")
            if "fallback e2e canary" in folded or "return exactly one json" in folded:
                raise FallbackWorkloadError("unsafe_report_value")

    visit(value)


def run_provider_fallback_workload(
    root: Path,
    ai_config: Mapping[str, Any],
    *,
    credential_vault: CredentialVault | None = None,
    client_factory: Callable[[ProviderRoute, float], ProviderPort] | None = None,
) -> dict[str, Any]:
    """Run the two-leg gate and return a sanitized success report.

    Supplying ``client_factory`` is the only test seam.  The live CLI omits it,
    requires both configured credentials, and uses the production registry.
    """

    selected_root = root.resolve()
    registry, routes = prepare_route_plan(ai_config, credential_vault=credential_vault)
    if client_factory is None:
        for route in routes:
            if registry.credential_source(route.provider_id) == "none":
                raise FallbackWorkloadError("provider_credential_unavailable")
        selected_factory = lambda route, timeout: registry.client_for_route(
            route, timeout=timeout
        )
    else:
        selected_factory = client_factory

    primary_wrapper: _RetryableFailureAfterResponse | None = None

    def governed_factory(route: ProviderRoute, timeout: float) -> ProviderPort:
        nonlocal primary_wrapper
        client = selected_factory(route, timeout)
        if route is routes[0]:
            primary_wrapper = _RetryableFailureAfterResponse(client)
            return primary_wrapper
        return client

    governed = GovernedProviderClient(routes, governed_factory, sleep=lambda _delay: None)
    result = governed.chat_json(
        _MESSAGES,
        model=routes[0].model,
        thinking=routes[0].thinking,
        reasoning_effort=routes[0].reasoning_effort,
        max_tokens=64,
        temperature=0,
    )
    _validate_result(result)
    governance = governance_from_result(result)
    if governance is None:
        raise FallbackWorkloadError("governance_evidence_missing")
    legs = governance.get("legs")
    fallbacks = governance.get("fallbacks")
    if (
        primary_wrapper is None
        or not primary_wrapper.observed_response
        or governance.get("outcome") != "complete"
        or governance.get("provider_requests") != 2
        or not isinstance(legs, list)
        or len(legs) != 2
        or not isinstance(fallbacks, list)
        or len(fallbacks) != 1
        or legs[0].get("route_kind") != "primary"
        or legs[0].get("status") != "failed"
        or legs[0].get("error_code") != FAILURE_CODE
        or legs[1].get("route_kind") != "fallback"
        or legs[1].get("status") != "complete"
        or legs[0].get("provider_id") == legs[1].get("provider_id")
        or legs[0].get("provider_id") != routes[0].provider_id
        or legs[0].get("model") != routes[0].model
        or legs[1].get("provider_id") != routes[1].provider_id
        or legs[1].get("model") != routes[1].model
        or legs[1].get("resolved_model") != result.resolved_model
        or governance.get("actual") != _route_fact(routes[1])
        or fallbacks[0].get("from") != _route_fact(routes[0])
        or fallbacks[0].get("to") != _route_fact(routes[1])
    ):
        raise FallbackWorkloadError("fallback_governance_validation_failed")
    if result.requested_model != routes[0].model:
        raise FallbackWorkloadError("requested_route_evidence_mismatch")

    report = {
        "report_version": 1,
        "workload_version": WORKLOAD_VERSION,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": True,
        "profile_id": PROFILE_ID,
        "failure_injection": {
            "code": FAILURE_CODE,
            "retryable": True,
            "after_primary_response": True,
        },
        "requested_route": _route_fact(routes[0]),
        "resolved_route": _route_fact(routes[1], resolved_model=result.resolved_model),
        "local_validator": {
            "version": 1,
            "status": "passed",
            "contract_sha256": hashlib.sha256(
                json.dumps(_EXPECTED_DATA, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        },
        "usage": normalize_usage(result.usage),
        "governance": governance,
        "routing_configuration_sha256": _configuration_hash(routes),
        "source_evidence": _source_evidence(selected_root),
    }
    _assert_report_safe(report, root=selected_root)
    return report


def write_report(root: Path, report: Mapping[str, Any]) -> str:
    _assert_report_safe(report, root=root)
    report_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root.resolve() / ".acm" / "reports" / "provider-fallback"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{report_id}.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(dict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, destination)
    return report_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the isolated cross-provider fallback acceptance workload"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--live",
        action="store_true",
        help="allow exactly two real provider requests",
    )
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required; no provider request was sent")
    root = args.root.resolve()
    paths = Paths.for_root(root)
    try:
        ai_config = load_config(paths)["ai"]
        vault = create_platform_credential_vault(paths.state_dir)
        report = run_provider_fallback_workload(
            root, ai_config, credential_vault=vault
        )
        report_id = write_report(root, report)
    except (FallbackWorkloadError, ProviderError) as exc:
        code = exc.code if isinstance(exc, (FallbackWorkloadError, ProviderError)) else "workload_failed"
        print(json.dumps({"ok": False, "error_code": code}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error_code": "workload_failed"}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "report_id": report_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FAILURE_CODE",
    "FallbackWorkloadError",
    "prepare_route_plan",
    "run_provider_fallback_workload",
    "write_report",
]
