"""Deterministic JSON encoding shared by persisted stress identities."""

from __future__ import annotations

import json
from typing import Any, Mapping


def canonical_json_bytes(value: Any) -> bytes:
    payload = dict(value) if isinstance(value, Mapping) else value
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = ["canonical_json_bytes"]
