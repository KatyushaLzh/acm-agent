"""Shared primitives and compatibility exceptions for storage repositories."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping

_UNSET = object()


def _process_is_alive(pid: int | None) -> bool:
    if not pid or int(pid) <= 0:
        return False
    if int(pid) == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PlanRevisionConflict(RuntimeError):
    """Raised when optimistic plan revision checking rejects a stale write."""

    def __init__(self, plan_id: str, expected: int | None, actual: int | None):
        self.plan_id = plan_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"plan {plan_id!r} revision conflict: expected {expected}, current {actual}"
        )


class TagOverrideRevisionConflict(RuntimeError):
    """Raised when a cleanup preview targets stale global tag decisions."""

    def __init__(self, expected: int | None, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"tag override revision conflict: expected {expected}, current {actual}"
        )


class ProblemContextConflict(RuntimeError):
    """Raised when a problem statement edit targets stale cached content."""

    def __init__(self, expected: str | None, actual: str | None):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"problem context conflict: expected hash {expected!r}, current {actual!r}"
        )


class MarkdownSummaryTargetRevisionConflict(RuntimeError):
    """Raised when a saved Markdown target is edited from a stale revision."""

    def __init__(self, target_id: str, expected: int | None, actual: int | None):
        self.target_id = target_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"markdown summary target {target_id!r} revision conflict: "
            f"expected {expected}, current {actual}"
        )


class MarkdownSummaryProposalRevisionConflict(RuntimeError):
    """Raised when an archive action targets a stale proposal preview."""

    def __init__(self, proposal_id: str, expected: int | None, actual: int | None):
        self.proposal_id = proposal_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"markdown summary proposal {proposal_id!r} revision conflict: "
            f"expected {expected}, current {actual}"
        )


class StressArtifactBundleRevisionConflict(RuntimeError):
    """Raised when a helper bundle is updated from a stale revision."""

    def __init__(self, bundle_id: str, expected: int | None, actual: int | None):
        self.bundle_id = bundle_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stress artifact bundle {bundle_id!r} revision conflict: "
            f"expected {expected}, current {actual}"
        )


class StressRunRevisionConflict(RuntimeError):
    """Raised when a stress runner writes state from a stale revision."""

    def __init__(self, run_id: str, expected: int | None, actual: int | None):
        self.run_id = run_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stress run {run_id!r} revision conflict: "
            f"expected {expected}, current {actual}"
        )


class StressSetupSlotConflict(RuntimeError):
    """Raised when another AI stress preparation already owns the global slot."""

    code = "stress_setup_active"

    def __init__(self, run_id: str, active_run_id: str | None):
        self.run_id = str(run_id)
        self.active_run_id = (
            str(active_run_id) if active_run_id is not None else None
        )
        self.details = {"active_run_id": self.active_run_id}
        active = self.active_run_id or "unknown"
        super().__init__(
            f"stress setup {self.run_id!r} conflicts with active run {active!r}"
        )


class StressPreparationCacheConflict(RuntimeError):
    """Raised when a content-addressed cache key maps to different payloads."""

    code = "stress_preparation_cache_conflict"

    def __init__(self, cache_key: str):
        self.cache_key = str(cache_key)
        self.details = {"cache_key": self.cache_key}
        super().__init__(
            f"stress preparation cache key {self.cache_key!r} already has "
            "a different payload"
        )


class StressArtifactCandidateConflict(RuntimeError):
    """Raised when an immutable candidate id maps to different content."""

    code = "stress_artifact_candidate_conflict"

    def __init__(self, candidate_id: str):
        self.candidate_id = str(candidate_id)
        self.details = {"candidate_id": self.candidate_id}
        super().__init__(
            f"stress artifact candidate {self.candidate_id!r} already has "
            "different immutable content"
        )


class StressArtifactProofConflict(RuntimeError):
    """Raised when an immutable proof key maps to different content."""

    code = "stress_artifact_proof_conflict"

    def __init__(self, proof_key: str):
        self.proof_key = str(proof_key)
        self.details = {"proof_key": self.proof_key}
        super().__init__(
            f"stress artifact proof {self.proof_key!r} already has "
            "different immutable content"
        )


class StressBundleCertificationConflict(RuntimeError):
    """Raised when an immutable certification key maps to different content."""

    code = "stress_bundle_certification_conflict"

    def __init__(self, certification_key: str):
        self.certification_key = str(certification_key)
        self.details = {"certification_key": self.certification_key}
        super().__init__(
            f"stress bundle certification {self.certification_key!r} already has "
            "different immutable content"
        )


class StressCacheAliasRevisionConflict(RuntimeError):
    """Raised when publishing a stress cache alias from a stale revision."""

    code = "stress_cache_alias_revision_conflict"

    def __init__(self, alias_key: str, expected: int | None, actual: int | None):
        self.alias_key = str(alias_key)
        self.expected = expected
        self.actual = actual
        self.details = {
            "alias_key": self.alias_key,
            "expected_revision": expected,
            "actual_revision": actual,
        }
        super().__init__(
            f"stress cache alias {self.alias_key!r} revision conflict: "
            f"expected {expected}, current {actual}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_object(value: Any) -> dict[str, Any]:
    """Return a mutable JSON object, treating legacy/corrupt values as empty."""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if value is None:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(key): item for key, item in decoded.items()}


def _merge_json_objects(
    current: Mapping[str, Any] | str | None,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Recursively merge object fields while replacing arrays and scalars."""

    merged = _json_object(current)
    for key, value in updates.items():
        name = str(key)
        previous = merged.get(name)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[name] = _merge_json_objects(previous, value)
        else:
            merged[name] = value
    return merged


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_VERDICT_ALIASES = {
    "OK": "AC",
    "AC": "AC",
    "ACCEPTED": "AC",
    "WRONG_ANSWER": "WA",
    "WA": "WA",
    "TIME_LIMIT_EXCEEDED": "TLE",
    "TLE": "TLE",
    "RUNTIME_ERROR": "RE",
    "RE": "RE",
    "MEMORY_LIMIT_EXCEEDED": "MLE",
    "MLE": "MLE",
    "COMPILATION_ERROR": "CE",
    "COMPILE_ERROR": "CE",
    "CE": "CE",
    "ABANDONED": "ABANDONED",
}


def _normalize_verdict(value: Any) -> str | None:
    """Return a compact, stable judge-result label for UI/API consumers."""
    if value is None:
        return None
    normalized = re.sub(r"[^A-Z0-9]+", "_", str(value).strip().upper()).strip("_")
    if not normalized:
        return None
    return _VERDICT_ALIASES.get(normalized, normalized)


def _timestamp_key(value: Any) -> datetime:
    """Parse ISO timestamps for ordering, normalizing offsets to UTC.

    Snapshots created by older versions can contain missing or malformed dates;
    those remain usable and simply sort behind valid evidence.
    """
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return datetime.min.replace(tzinfo=timezone.utc)
