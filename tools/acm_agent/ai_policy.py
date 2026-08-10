"""Provider-facing AI model and reasoning policy constants.

This module intentionally has no runtime dependencies so configuration, CLI,
service, and transport layers can share one authority without importing each
other.
"""

from __future__ import annotations


ALLOWED_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
DEFAULT_MODEL = "deepseek-v4-flash"
ALLOWED_REASONING_EFFORTS = frozenset({"high", "max"})
DEFAULT_REASONING_EFFORT = "high"


__all__ = [
    "ALLOWED_MODELS",
    "ALLOWED_REASONING_EFFORTS",
    "DEFAULT_MODEL",
    "DEFAULT_REASONING_EFFORT",
]
