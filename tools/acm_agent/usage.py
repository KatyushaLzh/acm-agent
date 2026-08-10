"""Provider-usage normalization and accumulation policy."""

from __future__ import annotations

from typing import Any, Collection, Mapping


def normalize_usage(
    source: Mapping[str, Any],
    *,
    flatten_reasoning: bool = True,
) -> dict[str, Any]:
    """Copy safe telemetry fields and optionally expose nested reasoning tokens."""

    normalized: dict[str, Any] = {}
    for raw_key, item in source.items():
        key = str(raw_key)
        if key.casefold() == "reasoning_content":
            continue
        if isinstance(item, Mapping):
            normalized[key] = normalize_usage(item, flatten_reasoning=False)
        elif isinstance(item, (str, int, float, bool)) or item is None:
            normalized[key] = item

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
