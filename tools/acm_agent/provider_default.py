"""Lazy composition root for the compatibility default provider."""

from __future__ import annotations

from typing import Any


def create_default_provider(*, api_key: str | None, timeout: float) -> Any:
    # Keep the adapter import out of config and business-service modules.
    from .deepseek import DeepSeekClient

    return DeepSeekClient(api_key=api_key, timeout=timeout)


__all__ = ["create_default_provider"]
