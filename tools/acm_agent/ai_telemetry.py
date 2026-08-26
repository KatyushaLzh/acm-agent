"""Safe model-run telemetry and versioned cost estimation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .usage import normalize_usage


PRICE_CATALOG_PATH = Path(__file__).with_name("pricing.v1.json")


def load_price_catalog(path: str | Path = PRICE_CATALOG_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not (
        isinstance(data.get("providers"), dict) or isinstance(data.get("models"), dict)
    ):
        raise ValueError("price catalog must contain providers or legacy models")
    if not str(data.get("catalog_version") or "").strip():
        raise ValueError("price catalog must have a catalog_version")
    if str(data.get("currency") or "") != "CNY":
        raise ValueError("DeepSeek price catalog currency must be CNY")
    return data


def price_catalog_hash(catalog: Mapping[str, Any]) -> str:
    encoded = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rate_band(catalog: Mapping[str, Any], created_at: str | None) -> str | None:
    stamp = _parse_time(created_at)
    schedule = catalog.get("schedule")
    if stamp is None or not isinstance(schedule, Mapping):
        return None
    weekdays = schedule.get("peak_weekdays")
    intervals = schedule.get("peak_intervals")
    if not isinstance(weekdays, list) or not isinstance(intervals, list):
        return None
    if stamp.weekday() not in weekdays:
        return "off_peak"
    hour = stamp.hour + stamp.minute / 60 + stamp.second / 3600
    for interval in intervals:
        if (
            isinstance(interval, list)
            and len(interval) == 2
            and all(isinstance(item, (int, float)) for item in interval)
            and float(interval[0]) <= hour < float(interval[1])
        ):
            return "peak"
    return "off_peak"


def estimate_cost(
    *,
    model: str,
    provider_id: str | None = "deepseek",
    usage: Mapping[str, Any],
    created_at: str | None,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected_catalog = dict(catalog or load_price_catalog())
    normalized = normalize_usage(usage)
    result: dict[str, Any] = {
        "estimated": True,
        "currency": str(selected_catalog.get("currency") or "CNY"),
        "price_version": selected_catalog.get("catalog_version"),
        "price_source": selected_catalog.get("source"),
        "model": str(model),
        "provider_id": str(provider_id) if provider_id is not None else None,
        "catalog_sha256": price_catalog_hash(selected_catalog),
    }
    providers = selected_catalog.get("providers")
    if isinstance(providers, Mapping):
        provider_prices = providers.get(str(provider_id or ""))
        model_prices = (
            (provider_prices.get("models") or {}).get(str(model))
            if isinstance(provider_prices, Mapping) else None
        )
    else:
        model_prices = (
            (selected_catalog.get("models") or {}).get(str(model))
            if provider_id in {None, "deepseek"} else None
        )
    if not isinstance(model_prices, Mapping):
        return {**result, "status": "unknown", "unknown_reason": "model_price_missing"}
    band = _rate_band(selected_catalog, created_at)
    if band is None:
        return {**result, "status": "unknown", "unknown_reason": "timestamp_missing"}
    rates = model_prices.get(band)
    if not isinstance(rates, Mapping):
        return {**result, "status": "unknown", "unknown_reason": "rate_band_missing"}

    input_tokens = _number(normalized.get("input_tokens"))
    output_tokens = _number(normalized.get("output_tokens"))
    cache_read = _number(normalized.get("cache_read_tokens"))
    uncached = _number(normalized.get("cache_miss_tokens"))
    if uncached is None and input_tokens is not None and cache_read is not None:
        uncached = max(0, input_tokens - cache_read)
    if input_tokens is None or output_tokens is None or cache_read is None or uncached is None:
        return {**result, "status": "unknown", "unknown_reason": "usage_incomplete", "rate_band": band}

    unit = _number(selected_catalog.get("unit_tokens"))
    read_rate = _number(rates.get("cache_read_input"))
    miss_rate = _number(rates.get("uncached_input"))
    output_rate = _number(rates.get("output"))
    if not unit or read_rate is None or miss_rate is None or output_rate is None:
        return {**result, "status": "unknown", "unknown_reason": "price_catalog_invalid", "rate_band": band}
    amount = (
        Decimal(str(cache_read)) * Decimal(str(read_rate))
        + Decimal(str(uncached)) * Decimal(str(miss_rate))
        + Decimal(str(output_tokens)) * Decimal(str(output_rate))
    ) / Decimal(str(unit))
    savings = (
        Decimal(str(cache_read))
        * max(Decimal(0), Decimal(str(miss_rate)) - Decimal(str(read_rate)))
        / Decimal(str(unit))
    )
    return {
        **result,
        "status": "known",
        "rate_band": band,
        "amount": float(round(amount, 12)),
        "amount_decimal": format(amount.quantize(Decimal("0.000000000001")), "f"),
        "cache_savings": float(round(savings, 12)),
        "cache_savings_decimal": format(savings.quantize(Decimal("0.000000000001")), "f"),
        "tokens": {"cache_read": cache_read, "uncached_input": uncached, "output": output_tokens},
    }


__all__ = ["PRICE_CATALOG_PATH", "estimate_cost", "load_price_catalog", "price_catalog_hash"]
