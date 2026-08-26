"""Run-scoped retry, fallback, deadline, token, and audit governance."""

from __future__ import annotations

from dataclasses import replace
import inspect
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from .provider import (
    AIJsonResult,
    AIResult,
    AIStreamEvent,
    CapabilityProfile,
    ProviderConfigurationError,
    ProviderError,
    ProviderHealth,
    ProviderPort,
)
from .provider_registry import ProviderRoute
from .usage import merge_usage, normalize_usage


def _safe_route(route: ProviderRoute) -> dict[str, str]:
    return {
        "provider_id": route.provider_id,
        "model": route.model,
        "reasoning_strength": route.reasoning_strength,
    }


def _accepts_keyword(function: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == name
        for parameter in parameters
    )


_STRUCTURED_REPAIR_CODES = frozenset(
    {"invalid_json_output", "empty_json_output", "response_incomplete"}
)


def _structured_repair_code(error: ProviderError) -> str | None:
    """Return a finite audit code for failures safe to repair semantically."""

    if isinstance(error, ProviderConfigurationError) or error.status in {
        400, 401, 402, 422
    }:
        return None
    code = str(error.code or "").strip().lower()
    if code == "content_filter" or error.finish_reason == "content_filter":
        return None
    if code in _STRUCTURED_REPAIR_CODES:
        return code
    if error.finish_reason == "length":
        return "response_incomplete"
    return None


def _structured_repair_messages(
    messages: Sequence[Mapping[str, Any]], *, validation_code: str
) -> list[dict[str, Any]]:
    """Append a stable repair instruction without echoing invalid provider data."""

    repaired = [dict(message) for message in messages]
    repaired.append(
        {
            "role": "user",
            "content": (
                "STRUCTURED_VALIDATION_REPAIR_V1\n"
                f"validation_code={validation_code}\n"
                "Return exactly one complete JSON object conforming to the same "
                "response schema. Do not include Markdown, commentary, or reasoning."
            ),
        }
    )
    return repaired


class GovernedProviderClient:
    """ProviderPort proxy sharing one budget across retries, rounds and fallback legs."""

    def __init__(
        self,
        routes: Sequence[ProviderRoute],
        client_factory: Callable[[ProviderRoute, float], ProviderPort],
        *,
        allow_request: Callable[[ProviderRoute], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not routes:
            raise ValueError("at least one provider route is required")
        self.routes = tuple(routes)
        self.budget = dict(self.routes[0].budget)
        self._client_factory = client_factory
        self._allow_request = allow_request
        self._monotonic = monotonic
        self._sleep = sleep
        self._started = monotonic()
        self._deadline = self._started + float(self.budget["request_timeout_seconds"])
        self._requests = 0
        self._usage: dict[str, Any] = {}
        self._legs: list[dict[str, Any]] = []
        self._fallbacks: list[dict[str, Any]] = []
        self._validation_repairs = 0
        self._clients: dict[tuple[str, str], ProviderPort] = {}

    @property
    def key_detected(self) -> bool:
        return bool(self._client(self.routes[0]).key_detected)

    @property
    def request_attempts(self) -> int:
        return self._requests

    @property
    def governance_snapshot(self) -> dict[str, Any]:
        actual = None
        for route in reversed(self.routes):
            if any(
                leg.get("provider_id") == route.provider_id
                and leg.get("model") == route.model
                and leg.get("status") == "complete"
                for leg in self._legs
            ):
                actual = route
                break
        return self.snapshot(
            outcome="complete" if actual is not None else "interrupted",
            actual_route=actual,
        )

    def _remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._monotonic())

    def _remaining_requests(self) -> int:
        return max(0, int(self.budget["max_requests"]) - self._requests)

    def _observed_tokens(self) -> int:
        value = self._usage.get("total_tokens")
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    def _client(self, route: ProviderRoute) -> ProviderPort:
        key = (route.provider_id, route.model)
        client = self._clients.get(key)
        if client is None:
            remaining = self._remaining_seconds()
            if remaining <= 0:
                raise self._budget_error("time_budget_exhausted")
            client = self._client_factory(route, remaining)
            # The governor owns retry accounting.  Older streaming adapter
            # signatures do not accept ``request_retries`` per call, so turn
            # off their private retry loop on this run-scoped client as well;
            # otherwise a hidden stream retry could bypass _before_request().
            internal_retries = getattr(client, "retries", None)
            if isinstance(internal_retries, int) and not isinstance(
                internal_retries, bool
            ):
                try:
                    setattr(client, "retries", 0)
                except (AttributeError, TypeError):
                    pass
            self._clients[key] = client
        return client

    def _budget_error(
        self, reason: str, *, usage: Mapping[str, Any] | None = None
    ) -> ProviderConfigurationError:
        return ProviderConfigurationError(
            "budget_exceeded",
            f"AI task budget blocked a provider request: {reason}",
            # Business workflows merge the usage returned by each logical
            # call.  A pre-request block therefore reports no new usage;
            # cumulative facts remain available in the governance snapshot.
            usage=normalize_usage(usage or {}),
            protocol_details={"governance": self.snapshot(outcome="blocked", blocked_reason=reason)},
        )

    def _before_request(self, route: ProviderRoute) -> None:
        if self._remaining_requests() < 1:
            raise self._budget_error("request_budget_exhausted")
        if self._remaining_seconds() <= 0:
            raise self._budget_error("time_budget_exhausted")
        if self._observed_tokens() >= int(self.budget["max_total_tokens"]):
            raise self._budget_error("token_budget_exhausted")
        if self._allow_request is not None:
            self._allow_request(route)

    @staticmethod
    def _request_counter(client: ProviderPort) -> int | None:
        value = getattr(client, "request_attempts", None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None

    def _charge(
        self,
        usage: Mapping[str, Any],
        *,
        request_delta: int | None,
    ) -> dict[str, Any]:
        normalized = normalize_usage(usage)
        reported = normalized.get("provider_requests")
        attempts = (
            int(request_delta)
            if isinstance(request_delta, int) and request_delta > 0
            else int(reported)
            if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported > 0
            else 1
        )
        if "provider_requests" not in normalized:
            normalized["provider_requests"] = attempts
        self._requests += attempts
        merge_usage(self._usage, normalized)
        # The request counter is authoritative when available; do not add a
        # provider-reported counter to the run total a second time.
        self._usage["provider_requests"] = self._requests
        return normalized

    def _leg(
        self,
        route: ProviderRoute,
        *,
        status: str,
        usage: Mapping[str, Any],
        error_code: str | None = None,
        resolved_model: str | None = None,
        purpose: str = "initial",
        validation_code: str | None = None,
    ) -> None:
        normalized = normalize_usage(usage)
        self._legs.append(
            {
                "ordinal": len(self._legs),
                "route_kind": "primary" if not self._legs and route is self.routes[0] else "fallback" if route is not self.routes[0] else "retry",
                **_safe_route(route),
                "resolved_model": resolved_model,
                "status": status,
                "error_code": error_code,
                "purpose": purpose,
                "validation_code": validation_code,
                "usage": normalized,
            }
        )

    def snapshot(
        self,
        *,
        outcome: str,
        actual_route: ProviderRoute | None = None,
        blocked_reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "profile_id": self.routes[0].profile_id,
            "primary": _safe_route(self.routes[0]),
            "actual": _safe_route(actual_route) if actual_route is not None else None,
            "outcome": outcome,
            "blocked_reason": blocked_reason,
            "provider_requests": self._requests,
            "validation_repairs": self._validation_repairs,
            "elapsed_ms": max(0, round((self._monotonic() - self._started) * 1000)),
            "budget": dict(self.budget),
            "fallbacks": list(self._fallbacks),
            "legs": list(self._legs),
        }

    def _options(self, method: Any, options: Mapping[str, Any]) -> dict[str, Any]:
        selected = dict(options)
        remaining_tokens = max(1, int(self.budget["max_total_tokens"]) - self._observed_tokens())
        cap = min(int(self.budget["max_output_tokens"]), remaining_tokens)
        requested = selected.get("max_tokens")
        if requested is None or int(requested) > cap:
            selected["max_tokens"] = cap
        if _accepts_keyword(method, "request_retries"):
            selected["request_retries"] = 0
        else:
            selected.pop("request_retries", None)
        if _accepts_keyword(method, "request_timeout"):
            selected["request_timeout"] = self._remaining_seconds()
        else:
            selected.pop("request_timeout", None)
        if _accepts_keyword(method, "json_retries"):
            # A JSON repair is another provider request.  Do not let the
            # adapter perform it invisibly outside this run ledger.
            selected["json_retries"] = 0
        else:
            selected.pop("json_retries", None)
        return selected

    @staticmethod
    def _route_options(
        method: Any, route: ProviderRoute, options: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Bind provider-specific wire options to the leg being executed.

        Business callers describe the primary route.  Reusing those values on
        a fallback leg can send the primary model or reasoning controls to a
        different provider, so every leg must be rebound from its validated
        ``ProviderRoute`` immediately before invoking the adapter.
        """

        selected = dict(options)
        for name, value in (
            ("model", route.model),
            ("thinking", route.thinking),
            ("reasoning_effort", route.reasoning_effort),
        ):
            if _accepts_keyword(method, name):
                selected[name] = value
            else:
                selected.pop(name, None)
        return selected

    def _invoke(
        self,
        method_name: str,
        messages: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any],
        *,
        purpose: str = "initial",
        validation_code: str | None = None,
    ) -> Any:
        selected_purpose = str(purpose or "initial").strip().lower()
        if selected_purpose not in {"initial", "validation_repair"}:
            raise ProviderConfigurationError(
                "invalid_attempt_purpose",
                "attempt purpose must be initial or validation_repair",
            )
        selected_validation_code = (
            str(validation_code or "").strip().lower()[:128] or None
        )
        if selected_validation_code is not None and not all(
            character.isalnum() or character in {"_", "-", "."}
            for character in selected_validation_code
        ):
            raise ProviderConfigurationError(
                "invalid_validation_code", "validation_code is not audit-safe"
            )
        repair_registered = False
        if selected_purpose == "validation_repair" and self._validation_repairs >= int(
            self.budget.get("max_validation_repairs", 0)
        ):
            raise self._budget_error("validation_repair_budget_exhausted")
        last_error: ProviderError | None = None
        call_usage: dict[str, Any] = {}
        for route_index, route in enumerate(self.routes):
            retry_index = 0
            while True:
                self._before_request(route)
                if selected_purpose == "validation_repair" and not repair_registered:
                    self._validation_repairs += 1
                    repair_registered = True
                client = self._client(route)
                method = getattr(client, method_name, None)
                method_options = dict(options)
                if not callable(method) and method_name == "structured":
                    method = getattr(client, "chat_json")
                    method_options.pop("json_schema", None)
                    method_options.pop("schema_name", None)
                if not callable(method):
                    raise ProviderConfigurationError(
                        "unsupported_capability",
                        f"provider client does not implement {method_name}",
                    )
                before = self._request_counter(client)
                try:
                    route_options = self._route_options(method, route, method_options)
                    result = method(messages, **self._options(method, route_options))
                except ProviderError as exc:
                    after = self._request_counter(client)
                    delta = after - before if before is not None and after is not None else None
                    leg_usage = self._charge(exc.usage, request_delta=delta)
                    merge_usage(call_usage, leg_usage)
                    self._leg(
                        route,
                        status="failed",
                        usage=leg_usage,
                        error_code=exc.code,
                        resolved_model=exc.resolved_model,
                        purpose=(
                            "transport_retry" if retry_index > 0
                            else "fallback" if route_index > 0
                            else selected_purpose
                        ),
                        validation_code=selected_validation_code,
                    )
                    last_error = exc
                    retry_allowed = (
                        exc.retryable
                        and retry_index < int(self.budget["max_retries"])
                        and self._remaining_requests() > 0
                        and self._remaining_seconds() > 0
                        and self._observed_tokens() < int(self.budget["max_total_tokens"])
                    )
                    if retry_allowed:
                        retry_index += 1
                        delay = min(
                            self._remaining_seconds(),
                            max(float(exc.retry_after or 0), min(2.0, float(2 ** (retry_index - 1)))),
                        )
                        if delay > 0:
                            self._sleep(delay)
                        continue
                    if exc.retryable and route_index + 1 < len(self.routes) and self._remaining_requests() > 0:
                        next_route = self.routes[route_index + 1]
                        self._fallbacks.append(
                            {
                                "from": _safe_route(route),
                                "to": _safe_route(next_route),
                                "error_code": exc.code,
                                "provider_requests_before": self._requests,
                            }
                        )
                        break
                    exc.usage = dict(call_usage)
                    exc.protocol_details["governance"] = self.snapshot(outcome="failed")
                    raise
                else:
                    after = self._request_counter(client)
                    delta = after - before if before is not None and after is not None else None
                    leg_usage = self._charge(result.usage, request_delta=delta)
                    merge_usage(call_usage, leg_usage)
                    self._leg(
                        route,
                        status="complete",
                        usage=leg_usage,
                        resolved_model=result.resolved_model,
                        purpose=(
                            "transport_retry" if retry_index > 0
                            else "fallback" if route_index > 0
                            else selected_purpose
                        ),
                        validation_code=selected_validation_code,
                    )
                    if self._requests > int(self.budget["max_requests"]):
                        raise self._budget_error(
                            "provider_request_count_exceeded", usage=call_usage
                        )
                    if self._observed_tokens() > int(self.budget["max_total_tokens"]):
                        raise self._budget_error(
                            "observed_token_budget_exceeded", usage=call_usage
                        )
                    metadata = dict(result.provider_metadata)
                    metadata["governance"] = self.snapshot(outcome="complete", actual_route=route)
                    return replace(
                        result,
                        usage=dict(call_usage),
                        requested_model=self.routes[0].model,
                        provider_metadata=metadata,
                    )
        assert last_error is not None
        last_error.usage = dict(call_usage)
        last_error.protocol_details["governance"] = self.snapshot(outcome="failed")
        raise last_error

    def chat(self, messages: Sequence[Mapping[str, Any]], **options: Any) -> AIResult:
        return self._invoke("chat", messages, options)

    def chat_json(self, messages: Sequence[Mapping[str, Any]], **options: Any) -> AIJsonResult:
        purpose = options.pop("purpose", "initial")
        validation_code = options.pop("validation_code", None)
        return self._invoke(
            "chat_json", messages, options,
            purpose=purpose, validation_code=validation_code,
        )

    def structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        purpose: str = "initial",
        validation_code: str | None = None,
        **options: Any,
    ) -> AIJsonResult:
        invoke_options = {
            **options, "json_schema": json_schema, "schema_name": schema_name
        }
        try:
            return self._invoke(
                "structured",
                messages,
                invoke_options,
                purpose=purpose,
                validation_code=validation_code,
            )
        except ProviderError as initial_error:
            repair_code = (
                _structured_repair_code(initial_error)
                if str(purpose or "initial").strip().lower() == "initial"
                else None
            )
            if repair_code is None:
                raise
            initial_usage = normalize_usage(initial_error.usage)
            try:
                repaired = self._invoke(
                    "structured",
                    _structured_repair_messages(
                        messages, validation_code=repair_code
                    ),
                    invoke_options,
                    purpose="validation_repair",
                    validation_code=repair_code,
                )
            except ProviderError as repair_error:
                combined = dict(initial_usage)
                merge_usage(combined, repair_error.usage)
                if combined:
                    combined["provider_requests"] = self._requests
                    if self._validation_repairs > 0:
                        combined["protocol_repairs"] = max(
                            1, int(combined.get("protocol_repairs") or 0)
                        )
                repair_error.usage = combined
                repair_error.protocol_details.setdefault(
                    "governance", self.snapshot(outcome="failed")
                )
                repair_error.protocol_details["validation_repair"] = {
                    "attempted": self._validation_repairs > 0,
                    "validation_code": repair_code,
                }
                raise
            combined = dict(initial_usage)
            merge_usage(combined, repaired.usage)
            combined["provider_requests"] = self._requests
            combined["protocol_repairs"] = max(
                1, int(combined.get("protocol_repairs") or 0)
            )
            metadata = dict(repaired.provider_metadata)
            metadata.setdefault("governance", self.snapshot(outcome="complete"))
            metadata["validation_repair"] = {
                "attempted": True,
                "validation_code": repair_code,
            }
            return replace(repaired, usage=combined, provider_metadata=metadata)

    def stream_chat(
        self, messages: Sequence[Mapping[str, Any]], **options: Any
    ) -> Iterator[AIStreamEvent]:
        # Coaching has no cross-route fallback. Retries are safe only before
        # any content is emitted, and they consume the same run ledger.
        route = self.routes[0]
        retry_index = 0
        while True:
            self._before_request(route)
            client = self._client(route)
            method = client.stream_chat
            before = self._request_counter(client)
            emitted = False
            usage: dict[str, Any] = {}
            terminal_model: str | None = None
            try:
                route_options = self._route_options(method, route, options)
                for event in method(messages, **self._options(method, route_options)):
                    emitted = emitted or bool(event.content)
                    if event.usage:
                        usage = normalize_usage(event.usage)
                    terminal_model = event.model or terminal_model
                    yield event
            except ProviderError as exc:
                after = self._request_counter(client)
                delta = after - before if before is not None and after is not None else None
                leg_usage = self._charge(exc.usage or usage, request_delta=delta)
                self._leg(
                    route, status="failed", usage=leg_usage,
                    error_code=exc.code, resolved_model=exc.resolved_model,
                    purpose="transport_retry" if retry_index > 0 else "initial",
                )
                if (
                    not emitted
                    and exc.retryable
                    and retry_index < int(self.budget["max_retries"])
                    and self._remaining_requests() > 0
                    and self._remaining_seconds() > 0
                ):
                    retry_index += 1
                    continue
                exc.usage = dict(self._usage)
                exc.protocol_details["governance"] = self.snapshot(outcome="failed")
                raise
            else:
                after = self._request_counter(client)
                delta = after - before if before is not None and after is not None else None
                leg_usage = self._charge(usage, request_delta=delta)
                self._leg(
                    route, status="complete", usage=leg_usage,
                    resolved_model=terminal_model,
                    purpose="transport_retry" if retry_index > 0 else "initial",
                )
                return

    def test_connection(self, model: str) -> ProviderHealth:
        return self._client(self.routes[0]).test_connection(model)

    def capabilities(self, model: str) -> CapabilityProfile:
        return self._client(self.routes[0]).capabilities(model)


def governance_from_result(value: Any) -> dict[str, Any] | None:
    metadata = getattr(value, "provider_metadata", None)
    governance = metadata.get("governance") if isinstance(metadata, Mapping) else None
    return dict(governance) if isinstance(governance, Mapping) else None


def governance_from_error(value: Any) -> dict[str, Any] | None:
    details = getattr(value, "protocol_details", None)
    governance = details.get("governance") if isinstance(details, Mapping) else None
    return dict(governance) if isinstance(governance, Mapping) else None


__all__ = ["GovernedProviderClient", "governance_from_error", "governance_from_result"]
