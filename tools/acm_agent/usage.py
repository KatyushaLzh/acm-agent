"""Provider-usage normalization and accumulation policy."""

from __future__ import annotations

from typing import Any, Collection, Mapping


_CANONICAL_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cache_read_tokens": ("cache_read_tokens", "prompt_cache_hit_tokens"),
    "cache_write_tokens": ("cache_write_tokens",),
    "cache_miss_tokens": ("cache_miss_tokens", "prompt_cache_miss_tokens"),
}

_SAFE_NUMERIC_FIELDS = frozenset({
    "input_tokens", "prompt_tokens", "output_tokens", "completion_tokens",
    "total_tokens", "cache_read_tokens", "prompt_cache_hit_tokens",
    "cache_write_tokens", "cache_miss_tokens", "prompt_cache_miss_tokens",
    "reasoning_tokens", "provider_requests", "protocol_repairs", "latency_ms",
})
_SAFE_DETAIL_FIELDS = {
    "prompt_tokens_details": frozenset({"cached_tokens"}),
    "completion_tokens_details": frozenset({"reasoning_tokens"}),
    "input_tokens_details": frozenset({"cached_tokens"}),
    "output_tokens_details": frozenset({"reasoning_tokens"}),
}


def _number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _first_number(source: Mapping[str, Any], keys: Collection[str]) -> int | float | None:
    for key in keys:
        value = _number(source.get(key))
        if value is not None:
            return value
    return None


def normalize_usage(
    source: Mapping[str, Any],
    *,
    flatten_reasoning: bool = True,
) -> dict[str, Any]:
    """Return a strict numeric allowlist of non-content provider telemetry.

    Provider responses are untrusted.  Unknown scalar or nested values must not
    cross this boundary: a relay could otherwise echo credentials or prompts in
    ``usage`` and have callers persist them as telemetry.
    """

    normalized: dict[str, Any] = {}
    for key in _SAFE_NUMERIC_FIELDS:
        value = _number(source.get(key))
        if value is not None:
            normalized[key] = value
    for key, allowed in _SAFE_DETAIL_FIELDS.items():
        raw_details = source.get(key)
        if not isinstance(raw_details, Mapping):
            continue
        details = {
            detail: value
            for detail in allowed
            if (value := _number(raw_details.get(detail))) is not None
        }
        if details:
            normalized[key] = details

    for canonical, aliases in _CANONICAL_ALIASES.items():
        value = _first_number(normalized, aliases)
        if value is not None:
            normalized[canonical] = value

    if "cache_read_tokens" not in normalized:
        for detail_key in ("input_tokens_details", "prompt_tokens_details"):
            prompt_details = normalized.get(detail_key)
            if isinstance(prompt_details, Mapping):
                cached = _number(prompt_details.get("cached_tokens"))
                if cached is not None:
                    normalized["cache_read_tokens"] = cached
                    break

    if "total_tokens" not in normalized:
        input_tokens = _number(normalized.get("input_tokens"))
        output_tokens = _number(normalized.get("output_tokens"))
        if input_tokens is not None and output_tokens is not None:
            normalized["total_tokens"] = input_tokens + output_tokens

    direct = normalized.get("reasoning_tokens")
    if flatten_reasoning and (
        not isinstance(direct, (int, float)) or isinstance(direct, bool)
    ):
        nested_total = 0

        def collect(value: Mapping[str, Any]) -> None:
            nonlocal nested_total
            for key, item in value.items():
                if (
                    key == "reasoning_tokens"
                    and isinstance(item, (int, float))
                    and not isinstance(item, bool)
                ):
                    nested_total += item
                elif isinstance(item, Mapping):
                    collect(item)

        collect(normalized)
        if nested_total:
            normalized["reasoning_tokens"] = nested_total
    return normalized


def merge_usage(
    target: dict[str, Any],
    source: Mapping[str, Any],
    *,
    flatten_reasoning: bool = True,
    preserve_scalars: bool = True,
    bool_or_keys: Collection[str] = (),
) -> None:
    """Merge normalized telemetry with numeric addition and explicit bool OR."""

    bool_or = frozenset(str(key) for key in bool_or_keys)

    def merge_normalized(destination: dict[str, Any], values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            if isinstance(value, Mapping):
                previous = destination.get(key)
                nested = dict(previous) if isinstance(previous, Mapping) else {}
                merge_normalized(nested, value)
                destination[key] = nested
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                previous = destination.get(key, 0)
                destination[key] = (
                    previous + value
                    if isinstance(previous, (int, float))
                    and not isinstance(previous, bool)
                    else value
                )
            elif key in bool_or and isinstance(value, bool):
                destination[key] = bool(destination.get(key, False)) or value
            elif preserve_scalars and (
                isinstance(value, (str, bool)) or value is None
            ):
                destination[key] = value

    merge_normalized(
        target,
        normalize_usage(source, flatten_reasoning=flatten_reasoning),
    )


__all__ = ["merge_usage", "normalize_usage"]
