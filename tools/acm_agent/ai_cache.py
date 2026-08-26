"""Content-addressed primitives for validated local AI artifacts.

The canonical request manifest is deliberately returned to the caller but is
never required by the persistence layer.  SQLite stores its digest only, so a
cache cannot accidentally become a second prompt, source-code, or path log.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any


CACHE_KEY_FORMAT_VERSION = "acm-exact-cache-v2"
DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_CACHE_MAX_ENTRIES = 512
DEFAULT_CACHE_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_CACHE_MAX_ENTRY_BYTES = 256 * 1024
# Exact-cache provider calls can legitimately consume a 90-second timeout plus
# one governed retry.  A shorter lease lets a follower steal live work.
DEFAULT_FLIGHT_LEASE_SECONDS = 180
DEFAULT_FLIGHT_WAIT_TIMEOUT_SECONDS = 180
DEFAULT_FLIGHT_FAILURE_COOLDOWN_SECONDS = 30
EXACT_CACHE_PROFILES = ("recommendation", "plan_organize", "summary")

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_PERSISTED_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "accesstoken",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "secret",
        "secretkey",
        "token",
        "prompt",
        "prompts",
        "message",
        "messages",
        "reasoning",
        "reasoningcontent",
        "thinking",
        "rawresponse",
        "rawerror",
        "sourcepath",
        "targetpath",
        "filepath",
        "localpath",
        "path",
    }
)


class CacheIntegrityError(ValueError):
    """A persisted entry does not match its content-addressed proof."""


class CacheArtifactTooLarge(ValueError):
    """A validated artifact cannot fit within the configured entry limit."""


@dataclass(frozen=True)
class CacheKey:
    key: str
    manifest_hash: str
    manifest: dict[str, Any]


@dataclass(frozen=True)
class CacheValidation:
    artifact: Any
    lowered: Any
    proof: dict[str, Any]
    manifest_hash: str
    artifact_hash: str


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically and reject non-portable float values."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: Any, label: str) -> str:
    selected = str(value or "").strip()
    if not selected:
        raise ValueError(f"{label} must not be empty")
    return selected


def _version(value: Any, label: str) -> str:
    selected = _required_text(value, label)
    if len(selected) > 128:
        raise ValueError(f"{label} is too long")
    return selected


def build_cache_key(
    *,
    profile_id: str,
    provider_id: str,
    model: str,
    provider_definition_hash: str,
    generation: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    prompt_version: str,
    schema_version: str,
    validator_version: str,
    lowering_version: str,
    taxonomy_version: str,
    correctness_inputs: Mapping[str, Any],
    transport_api: str = "chat_completions",
    response_schema_hash: str = "none",
    repair_version: str = "none",
) -> CacheKey:
    """Bind every correctness-relevant request input to one SHA-256 key.

    ``messages`` and ``correctness_inputs`` may contain private outbound data;
    they are intentionally present only in the ephemeral returned manifest.
    Callers persist ``manifest_hash`` and ``key``, never ``manifest``.
    """

    if not isinstance(generation, Mapping):
        raise TypeError("generation must be an object")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        raise TypeError("messages must be a sequence")
    if not all(isinstance(message, Mapping) for message in messages):
        raise TypeError("each message must be an object")
    if not isinstance(correctness_inputs, Mapping):
        raise TypeError("correctness_inputs must be an object")
    manifest = {
        "format": CACHE_KEY_FORMAT_VERSION,
        "profile_id": _required_text(profile_id, "profile_id"),
        "route": {
            "provider_id": _required_text(provider_id, "provider_id"),
            "model": _required_text(model, "model"),
            "provider_definition_hash": _required_text(
                provider_definition_hash, "provider_definition_hash"
            ),
            "transport_api": _version(transport_api, "transport_api"),
        },
        "generation": dict(generation),
        "messages_hash": canonical_hash(list(messages)),
        "versions": {
            "prompt": _version(prompt_version, "prompt_version"),
            "schema": _version(schema_version, "schema_version"),
            "validator": _version(validator_version, "validator_version"),
            "lowering": _version(lowering_version, "lowering_version"),
            "taxonomy": _version(taxonomy_version, "taxonomy_version"),
            "response_schema_hash": _version(
                response_schema_hash, "response_schema_hash"
            ),
            "repair": _version(repair_version, "repair_version"),
        },
        "correctness_inputs": {
            str(name): canonical_hash(value)
            for name, value in sorted(correctness_inputs.items(), key=lambda item: str(item[0]))
        },
    }
    manifest_hash = canonical_hash(manifest)
    return CacheKey(key=manifest_hash, manifest_hash=manifest_hash, manifest=manifest)


def _normalized_key(value: Any) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def assert_cache_safe(value: Any, *, location: str = "artifact") -> None:
    """Reject fields that could turn the cache into a sensitive-data log."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in _FORBIDDEN_PERSISTED_KEYS or normalized.endswith(
                ("apikey", "accesstoken", "clientsecret", "secretkey")
            ):
                raise ValueError(f"forbidden persisted cache field: {location}.{key}")
            assert_cache_safe(item, location=f"{location}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            assert_cache_safe(item, location=f"{location}[{index}]")


def encode_cache_artifact(
    artifact: Any,
    proof: Mapping[str, Any],
    *,
    manifest_hash: str,
    max_entry_bytes: int = DEFAULT_CACHE_MAX_ENTRY_BYTES,
) -> dict[str, Any]:
    """Encode a validated artifact and bind its proof to artifact + request."""

    if not _SHA256_PATTERN.fullmatch(str(manifest_hash)):
        raise ValueError("manifest_hash must be a lowercase SHA-256 digest")
    if not isinstance(proof, Mapping):
        raise TypeError("proof must be an object")
    if isinstance(max_entry_bytes, bool) or not isinstance(max_entry_bytes, int) or max_entry_bytes <= 0:
        raise ValueError("max_entry_bytes must be a positive integer")
    assert_cache_safe(artifact)
    assert_cache_safe(proof, location="proof")
    artifact_json = canonical_json(artifact)
    artifact_hash = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
    bound_proof = dict(proof)
    bound_proof.update(
        {
            "artifact_hash": artifact_hash,
            "manifest_hash": str(manifest_hash),
        }
    )
    proof_json = canonical_json(bound_proof)
    proof_hash = hashlib.sha256(proof_json.encode("utf-8")).hexdigest()
    size_bytes = len(artifact_json.encode("utf-8")) + len(proof_json.encode("utf-8"))
    if size_bytes > max_entry_bytes:
        raise CacheArtifactTooLarge(
            f"cache artifact is {size_bytes} bytes; limit is {max_entry_bytes}"
        )
    return {
        "artifact_json": artifact_json,
        "artifact_hash": artifact_hash,
        "proof_json": proof_json,
        "proof_hash": proof_hash,
        "size_bytes": size_bytes,
    }


def validate_cached_artifact(
    row: Mapping[str, Any],
    *,
    validator: Callable[[Any], Any] | None = None,
    lowering: Callable[[Any], Any] | None = None,
) -> CacheValidation:
    """Verify persisted hashes, then rerun the current validator and lowering."""

    try:
        artifact_json = str(row["artifact_json"])
        proof_json = str(row["proof_json"])
        artifact_hash = str(row["artifact_hash"])
        proof_hash = str(row["proof_hash"])
        manifest_hash = str(row["manifest_hash"])
        stored_size = int(row["size_bytes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CacheIntegrityError("cache entry shape is invalid") from exc
    actual_artifact_hash = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
    actual_proof_hash = hashlib.sha256(proof_json.encode("utf-8")).hexdigest()
    actual_size = len(artifact_json.encode("utf-8")) + len(proof_json.encode("utf-8"))
    if (
        artifact_hash != actual_artifact_hash
        or proof_hash != actual_proof_hash
        or stored_size != actual_size
    ):
        raise CacheIntegrityError("cache artifact or proof hash is invalid")
    try:
        artifact = json.loads(artifact_json)
        proof = json.loads(proof_json)
    except json.JSONDecodeError as exc:
        raise CacheIntegrityError("cache artifact or proof JSON is invalid") from exc
    if not isinstance(proof, dict) or (
        proof.get("artifact_hash") != artifact_hash
        or proof.get("manifest_hash") != manifest_hash
    ):
        raise CacheIntegrityError("cache proof is not bound to this artifact and request")
    assert_cache_safe(artifact)
    assert_cache_safe(proof, location="proof")
    validated = validator(artifact) if validator is not None else artifact
    if validated is None:
        validated = artifact
    lowered = lowering(validated) if lowering is not None else validated
    return CacheValidation(
        artifact=validated,
        lowered=lowered,
        proof=proof,
        manifest_hash=manifest_hash,
        artifact_hash=artifact_hash,
    )


__all__ = [
    "CACHE_KEY_FORMAT_VERSION",
    "DEFAULT_CACHE_MAX_BYTES",
    "DEFAULT_CACHE_MAX_ENTRIES",
    "DEFAULT_CACHE_MAX_ENTRY_BYTES",
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_FLIGHT_FAILURE_COOLDOWN_SECONDS",
    "DEFAULT_FLIGHT_LEASE_SECONDS",
    "DEFAULT_FLIGHT_WAIT_TIMEOUT_SECONDS",
    "EXACT_CACHE_PROFILES",
    "CacheArtifactTooLarge",
    "CacheIntegrityError",
    "CacheKey",
    "CacheValidation",
    "assert_cache_safe",
    "build_cache_key",
    "canonical_hash",
    "canonical_json",
    "encode_cache_artifact",
    "validate_cached_artifact",
]
